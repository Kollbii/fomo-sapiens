"""Application service: refresh, cached chain analytics, Strategy Lab and Wall Corridor.

Composes broker (`src/broker/`), storage (`src/storage/`) and analytics (`src/analytics/`);
it is the only module allowed to do so. It knows nothing about HTTP.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from datetime import UTC
from pathlib import Path
from typing import Any, Literal

import polars as pl

from src.analytics.anomalies import scan_anomalies
from src.analytics.bsm_fallback import BSMFallbackEngine
from src.analytics.strategies import (
    PRESET_STRATEGIES,
    PresetStrategyKind,
    StrategyEvaluationResponse,
)
from src.analytics.strategies import evaluate_strategy as evaluate_strategy_engine
from src.analytics.term_greeks import build_multi_dte_profiles
from src.analytics.walls import compute_walls
from src.broker.client import IBKRBrokerClient
from src.broker.corridor import (
    DEFAULT_CORRIDOR_MAX_CONTRACTS,
    DEFAULT_WINDOW_STRIKES,
    CorridorLegRef,
    CorridorQuoteFrame,
    CorridorQuoteService,
    CorridorSelection,
    select_wall_corridor,
)
from src.broker.fetcher import PagedOptionChainFetcher
from src.core.calendar import ALLOWED_INTERVALS_MIN
from src.core.config import Settings, get_settings
from src.core.exceptions import (
    InvalidRequestError,
    InvalidTickerError,
    RefreshBusyError,
    SnapshotNotFoundError,
)
from src.core.security import TickerValidator
from src.services.collector import CollectorService, CollectorStatus
from src.services.corridor import (
    CorridorQuoteCache,
    make_bsm_greeks_fallback,
    wall_anchors_from_report,
)
from src.services.schemas import (
    ChainAnalyticsResponse,
    ChainMetadataPayload,
    CollectorSettingsRequest,
    CorridorQuotesRequest,
    CorridorStopResponse,
    GatewayStatusResponse,
    RefreshRequest,
    SnapshotsListResponse,
    StrategyEvaluateRequest,
    StrategySuggestRequest,
    StrategySuggestResponse,
)
from src.services.tracker import TrackerService
from src.storage.collector_store import CollectorSettings, CollectorStore, TickerPin
from src.storage.csv_repository import CSVRepository
from src.storage.quality import evaluate_snapshot, measure_quote_quality
from src.storage.schema import (
    OPTION_CHAIN_POLARS_SCHEMA,
    CollectionMode,
    SpotSource,
    infer_chain_step_and_range,
)
from src.storage.tracker_store import TrackerStore

logger = logging.getLogger(__name__)

# A manual refresh waits this long for another manual refresh; a running scheduled
# collection cycle is reported immediately (HTTP 409 COLLECTOR_BUSY) so the UI can show it.
MANUAL_REFRESH_WAIT_SEC = 90.0
REFRESH_OWNER_MANUAL = "manual"
REFRESH_OWNER_COLLECTOR = "collector"
# Rough time per ticker for the busy hint (5 pages, see docs/ARCHITECTURE.md).
REFRESH_EST_SEC_PER_TICKER = 40


class AnalyzerService:
    """Coordinates Broker -> BSM Fallback -> CSV Storage -> Analytics payloads.

    Framework-agnostic: raises domain exceptions (`src/core/exceptions.py`) that
    `src/web/errors.py` maps to HTTP responses.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        repo: CSVRepository | None = None,
        broker_client: IBKRBrokerClient | None = None,
        fetcher: PagedOptionChainFetcher | None = None,
        corridor: CorridorQuoteService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repo = repo or CSVRepository(self.settings.storage)
        self.broker_client = broker_client or IBKRBrokerClient(self.settings)
        self.fetcher = fetcher or PagedOptionChainFetcher(self.broker_client, self.settings)
        self.corridor = corridor or CorridorQuoteService(
            self.broker_client,
            self.settings,
            clock=self.fetcher.clock,
            sleep=self.fetcher.sleep,
            greeks_fallback=make_bsm_greeks_fallback(self.settings.analytics.risk_free_rate),
        )
        self.validator = TickerValidator(
            watchlist=self.settings.watchlist.hardcoded_tickers,
            allow_custom=self.settings.watchlist.allow_custom_ticker,
        )
        self._ticker_capabilities: dict[str, tuple[list[float], list[float], int]] = {}
        self.corridor_cache = CorridorQuoteCache()
        # One chain refresh at a time (manual or scheduled collection).
        self.refresh_lock = asyncio.Lock()
        self.refresh_owner: str | None = None
        self.refresh_busy_hint_sec = REFRESH_EST_SEC_PER_TICKER
        # Last time a refresh stopped corridor streams: {at_utc, lines, by, ticker}.
        self.last_corridor_stop: dict[str, Any] | None = None
        self._collector: CollectorService | None = None
        self._tracker: TrackerService | None = None

    def get_tracker(self) -> TrackerService:
        """The position tracker (runs inside the collector's slots)."""
        if self._tracker is None:
            tracked = Path(self.settings.tracker.tracked_dir)
            if not tracked.is_absolute():
                tracked = self.repo.root / tracked
            self._tracker = TrackerService(
                settings=self.settings,
                repo=self.repo,
                fetcher=self.fetcher,
                store=TrackerStore(self.repo.index_path.parent, tracked),
                clock=self.fetcher.clock,
                validate_ticker=self.validate_ticker,
            )
        return self._tracker

    def get_collector(self) -> CollectorService:
        """The scheduled collector (created on first use; the loop starts in the lifespan)."""
        if self._collector is None:
            self._collector = CollectorService(
                self,
                CollectorStore(self.repo.index_path.parent),
                clock=self.fetcher.clock,
                tracker=self.get_tracker(),
            )
        return self._collector

    def validate_ticker(self, raw_ticker: str) -> str:
        return self.validator.validate_selection(raw_ticker)

    def get_status(self, ticker: str | None = None) -> GatewayStatusResponse:
        ibkr = self.settings.ibkr
        wl = self.settings.watchlist
        oc = self.settings.options_chain
        active_md = (
            self.broker_client.get_active_market_data_type()
            if hasattr(self.broker_client, "get_active_market_data_type")
            else int(self.broker_client.current_type)
        )
        cap_sym: str | None = None
        if ticker is not None and ticker.strip():
            try:
                cap_sym = self.validate_ticker(ticker)
            except InvalidTickerError:
                cap_sym = None
        caps = self._ticker_capabilities.get(cap_sym) if cap_sym else None
        avail_steps = list(caps[0]) if caps else list(oc.allowed_strike_steps)
        avail_ranges = list(caps[1]) if caps else list(oc.allowed_range_pcts)
        return GatewayStatusResponse(
            connected=self.broker_client.is_connected,
            host=ibkr.host,
            port=ibkr.port,
            client_id=ibkr.client_id,
            market_data_mode=ibkr.market_data_mode,
            default_market_data_type=ibkr.default_market_data_type,
            fallback_market_data_type=ibkr.fallback_market_data_type,
            active_market_data_type=active_md,
            live_subscription_missing=bool(
                getattr(self.broker_client, "live_subscription_missing", False)
            ),
            default_ticker=wl.default_ticker,
            watchlist=list(wl.hardcoded_tickers),
            allow_custom_ticker=wl.allow_custom_ticker,
            default_range_pct=oc.default_range_pct,
            allowed_range_pcts=list(oc.allowed_range_pcts),
            default_strike_step=oc.default_strike_step,
            allowed_strike_steps=list(oc.allowed_strike_steps),
            dte_min=oc.dte_min,
            dte_max=oc.dte_max,
            available_strike_steps=avail_steps,
            available_range_pcts=avail_ranges,
            active_capabilities_ticker=cap_sym if caps else None,
        )

    def list_snapshots(self, raw_ticker: str) -> SnapshotsListResponse:
        symbol = self.validate_ticker(raw_ticker)
        snaps = self.repo.list_snapshots(symbol)
        return SnapshotsListResponse(
            ticker=symbol,
            latest_snapshot_id=snaps[0].snapshot_id if snaps else None,
            snapshots=snaps,
        )

    def _enrich_if_needed(self, df: pl.DataFrame) -> pl.DataFrame:
        needs_bsm = (
            df.filter((pl.col("greeks_source") == "NONE") | pl.col("iv").is_null()).height > 0
        )
        if not needs_bsm:
            return df
        return BSMFallbackEngine.enrich_missing_greeks(
            df, risk_free_rate=self.settings.analytics.risk_free_rate
        )

    def build_analytics_payload(
        self,
        df: pl.DataFrame,
        *,
        source: Literal["CACHE", "LIVE_REFRESH"],
        notices: list[str] | None = None,
        range_pct: float | None = None,
        strike_step: float | None = None,
        requested_strike_step: float | None = None,
        exchange_validated_strike_count: int | None = None,
        exchange_native_verified: bool | None = None,
        available_strike_steps: list[float] | None = None,
        available_range_pcts: list[float] | None = None,
        spot_source: SpotSource | None = None,
        collection_mode: CollectionMode | None = None,
    ) -> ChainAnalyticsResponse:
        enriched = self._enrich_if_needed(df)
        quality = measure_quote_quality(enriched)
        first = enriched.row(0, named=True)
        source_counts = dict(Counter(str(s) for s in enriched["greeks_source"].to_list()))
        expirations = sorted(str(e) for e in enriched["expiry"].unique().to_list())
        dtes = sorted(int(d) for d in enriched["dte"].unique().to_list())

        inferred_step, inferred_range = infer_chain_step_and_range(enriched)
        resolved_range = range_pct if range_pct is not None else inferred_range
        resolved_step: float | None
        if inferred_step is not None and (
            strike_step is None or inferred_step > strike_step + 1e-6
        ):
            resolved_step = inferred_step
        else:
            resolved_step = strike_step if strike_step is not None else inferred_step
        resolved_requested_step = (
            requested_strike_step if requested_strike_step is not None else strike_step
        )

        underlying_sym = str(first["underlying"])
        caps = self._ticker_capabilities.get(underlying_sym)
        oc = self.settings.options_chain
        resolved_avail_steps = (
            list(available_strike_steps)
            if available_strike_steps is not None
            else (list(caps[0]) if caps else list(oc.allowed_strike_steps))
        )
        resolved_avail_ranges = (
            list(available_range_pcts)
            if available_range_pcts is not None
            else (list(caps[1]) if caps else list(oc.allowed_range_pcts))
        )
        resolved_validated_count = (
            exchange_validated_strike_count
            if exchange_validated_strike_count is not None
            else (caps[2] if caps else None)
        )
        resolved_verified = (
            exchange_native_verified if exchange_native_verified is not None else (caps is not None)
        )

        meta = ChainMetadataPayload(
            snapshot_id=str(first["snapshot_id"]),
            snapshot_ts_utc=str(first["snapshot_ts_utc"]),
            market_date=str(first["market_date"]),
            underlying=underlying_sym,
            underlying_conid=int(first["underlying_conid"]),
            underlying_price=float(first["underlying_price"]),
            market_data_type=int(first["market_data_type"]),
            contract_count=enriched.height,
            expirations=expirations,
            dtes=dtes,
            range_pct=resolved_range,
            strike_step=resolved_step,
            requested_strike_step=resolved_requested_step,
            exchange_validated_strike_count=resolved_validated_count,
            exchange_native_verified=resolved_verified,
            available_strike_steps=resolved_avail_steps,
            available_range_pcts=resolved_avail_ranges,
            greeks_source_counts=source_counts,
            source=source,
            notices=list(notices or []),
            spot_source=spot_source,
            quoted_contract_count=quality.quoted_contract_count,
            quote_coverage=quality.quote_coverage,
            prior_close_only_count=quality.prior_close_only_count,
            collection_mode=collection_mode,
        )

        walls = compute_walls(enriched, top_n=self.settings.analytics.top_walls_count)
        anomalies = scan_anomalies(
            enriched,
            zscore_threshold=self.settings.analytics.unusual_volume_zscore_threshold,
        )
        profiles = build_multi_dte_profiles(
            enriched,
            default_market_data_type=self.settings.ibkr.default_market_data_type,
        )
        contracts = enriched.sort(["dte", "strike", "right"]).to_dicts()

        return ChainAnalyticsResponse(
            meta=meta,
            walls=walls,
            anomalies=anomalies,
            profiles=profiles,
            contracts=contracts,
        )

    def load_chain_analytics(
        self,
        raw_ticker: str,
        snapshot_id: str | None = None,
        *,
        dte_min: int | None = None,
        dte_max: int | None = None,
    ) -> ChainAnalyticsResponse:
        symbol = self.validate_ticker(raw_ticker)
        if dte_min is not None and dte_max is not None and dte_min > dte_max:
            raise InvalidRequestError("dte_min must be <= dte_max")
        if snapshot_id is not None and snapshot_id.strip():
            df = self.repo.load_snapshot(symbol, snapshot_id.strip())
        else:
            df = self.repo.load_latest(symbol)
        loaded_snap_id = str(df["snapshot_id"][0])
        if dte_min is not None:
            df = df.filter(pl.col("dte") >= dte_min)
        if dte_max is not None:
            df = df.filter(pl.col("dte") <= dte_max)
        if df.height == 0:
            raise SnapshotNotFoundError(f"No contracts found in requested DTE range for {symbol}")
        snap_meta = next(
            (m for m in self.repo.list_snapshots(symbol) if m.snapshot_id == loaded_snap_id),
            None,
        )
        return self.build_analytics_payload(
            df,
            source="CACHE",
            range_pct=snap_meta.range_pct if snap_meta else None,
            strike_step=snap_meta.strike_step if snap_meta else None,
            requested_strike_step=snap_meta.requested_strike_step if snap_meta else None,
            exchange_validated_strike_count=(
                snap_meta.exchange_validated_strike_count if snap_meta else None
            ),
            exchange_native_verified=(snap_meta.exchange_native_verified if snap_meta else None),
            available_strike_steps=(snap_meta.available_strike_steps if snap_meta else None),
            available_range_pcts=(snap_meta.available_range_pcts if snap_meta else None),
            spot_source=snap_meta.spot_source if snap_meta else None,
            collection_mode=snap_meta.collection_mode if snap_meta else None,
        )

    def resolve_csv_path(self, raw_ticker: str, snapshot_id: str | None = None) -> Path:
        symbol = self.validate_ticker(raw_ticker)
        if snapshot_id is not None and snapshot_id.strip():
            path = self.repo.snapshot_path(symbol, snapshot_id.strip())
            if not path.is_file():
                raise SnapshotNotFoundError(f"Snapshot not found: {snapshot_id.strip()}")
            return path
        latest = self.repo.latest_path(symbol)
        if latest.is_file():
            return latest
        for meta in self.repo.list_snapshots(symbol):
            candidate = self.repo.snapshot_path(symbol, meta.snapshot_id)
            if candidate.is_file():
                return candidate
        raise SnapshotNotFoundError(f"No snapshots available for {symbol}")

    def _load_enriched_chain(
        self,
        raw_ticker: str,
        snapshot_id: str | None = None,
        *,
        apply_corridor_overlay: bool = True,
    ) -> tuple[str, str, pl.DataFrame]:
        symbol = self.validate_ticker(raw_ticker)
        df = (
            self.repo.load_snapshot(symbol, snapshot_id.strip())
            if snapshot_id is not None and snapshot_id.strip()
            else self.repo.load_latest(symbol)
        )
        loaded_snap_id = str(df["snapshot_id"][0])
        enriched = self._enrich_if_needed(df)
        if apply_corridor_overlay:
            enriched = self.corridor_cache.overlay(enriched, symbol, loaded_snap_id)
        return symbol, loaded_snap_id, enriched

    def _select_corridor(
        self,
        enriched: pl.DataFrame,
        *,
        target_expiry: str | None,
        window_strikes: int,
        max_contracts: int,
        priority_legs: Sequence[CorridorLegRef],
    ) -> CorridorSelection:
        walls = compute_walls(enriched, top_n=self.settings.analytics.top_walls_count)
        try:
            return select_wall_corridor(
                enriched,
                wall_anchors=wall_anchors_from_report(walls),
                target_expiry=target_expiry,
                window_strikes=window_strikes,
                max_contracts=max_contracts,
                priority_legs=priority_legs,
            )
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc

    async def fetch_corridor_quotes(self, payload: CorridorQuotesRequest) -> CorridorQuoteFrame:
        _, snap_id, enriched = self._load_enriched_chain(
            payload.ticker,
            payload.snapshot_id,
            apply_corridor_overlay=False,
        )
        selection = self._select_corridor(
            enriched,
            target_expiry=payload.target_expiry,
            window_strikes=payload.window_strikes,
            max_contracts=payload.max_contracts,
            priority_legs=payload.priority_legs,
        )
        try:
            return await self.corridor.fetch_corridor_quotes(
                selection,
                snapshot_id=snap_id,
                on_frame=self.corridor_cache.record,
            )
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc

    async def open_corridor_stream(
        self,
        raw_ticker: str,
        *,
        snapshot_id: str | None = None,
        target_expiry: str | None = None,
        window_strikes: int = DEFAULT_WINDOW_STRIKES,
        max_contracts: int = DEFAULT_CORRIDOR_MAX_CONTRACTS,
        priority_legs: Sequence[CorridorLegRef] = (),
        is_disconnected: Callable[[], bool | Awaitable[bool]] | None = None,
        max_frames: int | None = None,
    ) -> AsyncGenerator[CorridorQuoteFrame, None]:
        """Validate inputs and Gateway connectivity, then return an un-started frame stream.

        Raising here (before streaming begins) lets the HTTP layer answer `422` / `503`
        instead of opening an SSE response that immediately errors.
        """
        _, snap_id, enriched = self._load_enriched_chain(
            raw_ticker,
            snapshot_id,
            apply_corridor_overlay=False,
        )
        selection = self._select_corridor(
            enriched,
            target_expiry=target_expiry,
            window_strikes=window_strikes,
            max_contracts=max_contracts,
            priority_legs=priority_legs,
        )
        await self.broker_client.ensure_connected()
        return self.corridor.stream_corridor_quotes(
            selection,
            snapshot_id=snap_id,
            is_disconnected=is_disconnected,
            max_frames=max_frames,
            on_frame=self.corridor_cache.record,
        )

    async def stop_corridor_streams(self) -> CorridorStopResponse:
        stopped = await self.corridor.stop_all_streams()
        return CorridorStopResponse(
            stopped_subscriptions=stopped,
            active_streams=self.corridor.active_stream_count,
        )

    def suggest_strategies(self, payload: StrategySuggestRequest) -> StrategySuggestResponse:
        symbol, snap_id, enriched = self._load_enriched_chain(payload.ticker, payload.snapshot_id)
        walls = compute_walls(enriched, top_n=self.settings.analytics.top_walls_count)
        kinds: tuple[PresetStrategyKind, ...] = (
            (payload.strategy_kind,) if payload.strategy_kind is not None else PRESET_STRATEGIES
        )
        evaluations: list[StrategyEvaluationResponse] = []
        errors: list[str] = []
        for kind in kinds:
            try:
                evaluations.append(
                    evaluate_strategy_engine(
                        enriched,
                        strategy_kind=kind,
                        target_expiry=payload.target_expiry,
                        days_forward=payload.days_forward,
                        iv_shift=payload.iv_shift,
                        qty_multiplier=payload.qty_multiplier,
                        risk_free_rate=self.settings.analytics.risk_free_rate,
                        walls_report=walls,
                    )
                )
            except ValueError as exc:
                if payload.strategy_kind is not None:
                    raise InvalidRequestError(str(exc)) from exc
                errors.append(f"{kind}: {exc}")

        if not evaluations:
            detail = "; ".join(errors) or "Unable to construct any preset strategy from snapshot"
            raise InvalidRequestError(detail)

        return StrategySuggestResponse(
            ticker=symbol,
            snapshot_id=snap_id,
            evaluations=evaluations,
        )

    def evaluate_strategy(self, payload: StrategyEvaluateRequest) -> StrategyEvaluationResponse:
        _, _, enriched = self._load_enriched_chain(payload.ticker, payload.snapshot_id)
        try:
            return evaluate_strategy_engine(
                enriched,
                strategy_kind=payload.strategy_kind,
                target_expiry=payload.target_expiry,
                legs=payload.legs,
                days_forward=payload.days_forward,
                iv_shift=payload.iv_shift,
                qty_multiplier=payload.qty_multiplier,
                risk_free_rate=self.settings.analytics.risk_free_rate,
            )
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc

    async def acquire_refresh(self, owner: str, timeout_sec: float) -> bool:
        """Take the single refresh lock; False if it stayed busy for `timeout_sec`."""
        if not self.refresh_lock.locked():
            await self.refresh_lock.acquire()  # free: returns at once, no timer needed
        else:
            try:
                await asyncio.wait_for(self.refresh_lock.acquire(), timeout=timeout_sec)
            except TimeoutError:
                return False
        self.refresh_owner = owner
        return True

    def release_refresh(self) -> None:
        self.refresh_owner = None
        self.refresh_lock.release()

    async def refresh_chain(self, req: RefreshRequest) -> ChainAnalyticsResponse:
        """Manual refresh (`POST /api/refresh`)."""
        self.validate_ticker(req.ticker)  # invalid tickers fail with 422 even while busy
        if self.refresh_lock.locked() and self.refresh_owner == REFRESH_OWNER_COLLECTOR:
            raise RefreshBusyError(
                "Waiting for scheduled collection: a collection cycle is running.",
                owner=REFRESH_OWNER_COLLECTOR,
                retry_after_sec=self.refresh_busy_hint_sec,
            )
        if not await self.acquire_refresh(REFRESH_OWNER_MANUAL, MANUAL_REFRESH_WAIT_SEC):
            owner = self.refresh_owner or REFRESH_OWNER_MANUAL
            raise RefreshBusyError(
                f"Another refresh ({owner}) is still running.",
                owner=owner,
                retry_after_sec=self.refresh_busy_hint_sec,
            )
        try:
            return await self.refresh_chain_locked(req, collection_mode="MANUAL")
        finally:
            self.release_refresh()

    async def refresh_chain_locked(
        self,
        req: RefreshRequest,
        *,
        collection_mode: CollectionMode,
    ) -> ChainAnalyticsResponse:
        """Refresh one ticker; the caller must hold `refresh_lock` (see `acquire_refresh`)."""
        symbol = self.validate_ticker(req.ticker)
        # IBKR allows 100 simultaneous market-data lines by default (shared with TWS). A page of
        # 80 contracts plus a corridor stream of up to 60 would exceed it, so stop streams first.
        stopped_lines = 0
        if self.corridor.active_stream_count:
            stopped_lines = await self.corridor.stop_all_streams()
            logger.info("refresh %s stopped corridor streams (%d lines)", symbol, stopped_lines)
            self.last_corridor_stop = {
                "at_utc": self.fetcher.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lines": stopped_lines,
                "by": self.refresh_owner or REFRESH_OWNER_MANUAL,
                "ticker": symbol,
            }
        fetch_res = await self.fetcher.fetch_option_chain(
            symbol,
            range_pct=req.range_pct,
            strike_step=req.strike_step,
            dte_min=req.dte_min,
            dte_max=req.dte_max,
        )
        raw_df = pl.DataFrame(
            [r.model_dump() for r in fetch_res.rows],
            schema=OPTION_CHAIN_POLARS_SCHEMA,
        )
        enriched_df = BSMFallbackEngine.enrich_missing_greeks(
            raw_df, risk_free_rate=self.settings.analytics.risk_free_rate
        )
        inferred_step, _ = infer_chain_step_and_range(enriched_df)
        plan_step = fetch_res.plan.strikes.effective_step
        actual_step = (
            inferred_step
            if (inferred_step is not None and inferred_step > plan_step + 1e-6)
            else plan_step
        )
        req_step = fetch_res.plan.strikes.requested_step
        notices = list(fetch_res.notices)
        if stopped_lines:
            notices.append(
                f"Stopped the corridor stream ({stopped_lines} market-data lines) before the "
                "refresh to stay under the IBKR line limit."
            )
        if actual_step > plan_step + 1e-6:
            notices.append(
                f"IBKR strikes for {symbol} spaced at ${actual_step:g} (requested ${req_step:g})."
            )
        plan = fetch_res.plan
        avail_steps = (
            list(plan.available_strike_steps)
            if plan.available_strike_steps
            else list(self.settings.options_chain.allowed_strike_steps)
        )
        avail_ranges = (
            list(plan.available_range_pcts)
            if plan.available_range_pcts
            else list(self.settings.options_chain.allowed_range_pcts)
        )
        validated_count = (
            int(plan.exchange_validated_strike_count)
            if plan.exchange_validated_strike_count > 0
            else len({float(r.strike) for r in fetch_res.rows})
        )
        self._ticker_capabilities[symbol] = (avail_steps, avail_ranges, validated_count)
        saved_meta = self.repo.save_snapshot(
            symbol,
            enriched_df.to_dicts(),
            range_pct=plan.range_pct,
            strike_step=actual_step,
            requested_strike_step=req_step,
            spot_source=plan.underlying.spot_source,
            collection_mode=collection_mode,
        )
        quality_verdict = evaluate_snapshot(saved_meta)
        if not quality_verdict.usable:
            notices.append(
                "Snapshot saved but not usable for history: " + "; ".join(quality_verdict.reasons)
            )
        return self.build_analytics_payload(
            enriched_df,
            source="LIVE_REFRESH",
            notices=notices,
            range_pct=plan.range_pct,
            strike_step=actual_step,
            requested_strike_step=req_step,
            exchange_validated_strike_count=validated_count,
            exchange_native_verified=bool(plan.exchange_native_verified),
            available_strike_steps=avail_steps,
            available_range_pcts=avail_ranges,
            spot_source=saved_meta.spot_source,
            collection_mode=saved_meta.collection_mode,
        )

    # ------------------------------------------------------------------ scheduled collection
    def update_collector_settings(self, payload: CollectorSettingsRequest) -> CollectorStatus:
        """Validate and apply `PUT /api/collector/settings` (bad input: `InvalidRequestError`)."""
        oc = self.settings.options_chain
        if payload.interval_min not in ALLOWED_INTERVALS_MIN:
            raise InvalidRequestError(
                f"interval_min must be one of {list(ALLOWED_INTERVALS_MIN)} (0 = closing only)"
            )
        pins: list[TickerPin] = []
        for item in payload.tickers:
            symbol = self.validate_ticker(item.symbol)
            if not any(abs(item.range_pct - r) < 1e-9 for r in oc.allowed_range_pcts):
                raise InvalidRequestError(
                    f"{symbol}: range_pct must be one of {list(oc.allowed_range_pcts)}"
                )
            if not any(abs(item.strike_step - s) < 1e-9 for s in oc.allowed_strike_steps):
                raise InvalidRequestError(
                    f"{symbol}: strike_step must be one of {list(oc.allowed_strike_steps)}"
                )
            if item.dte_min > item.dte_max:
                raise InvalidRequestError(f"{symbol}: dte_min must be <= dte_max")
            pins.append(
                TickerPin(
                    symbol=symbol,
                    range_pct=item.range_pct,
                    strike_step=item.strike_step,
                    dte_min=item.dte_min,
                    dte_max=item.dte_max,
                )
            )
        try:
            new = CollectorSettings(
                enabled=payload.enabled, interval_min=payload.interval_min, tickers=pins
            )
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc
        collector = self.get_collector()
        collector.update_settings(new)
        return collector.status()
