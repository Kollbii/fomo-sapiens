"""Application service: refresh, cached chain analytics, Strategy Lab and Wall Corridor.

Composes broker (`src/broker/`), storage (`src/storage/`) and analytics (`src/analytics/`);
it is the only module allowed to do so. It knows nothing about HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import math
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
    CORRIDOR_MAX_CONTRACTS,
    DEFAULT_CORRIDOR_MAX_CONTRACTS,
    DEFAULT_WINDOW_STRIKES,
    CorridorLegRef,
    CorridorQuoteFrame,
    CorridorQuoteService,
    CorridorSelection,
    select_wall_corridor,
)
from src.broker.fetcher import PagedOptionChainFetcher
from src.core.config import Settings, get_settings
from src.core.exceptions import (
    InvalidRequestError,
    InvalidTickerError,
    RefreshBusyError,
    SnapshotNotFoundError,
)
from src.core.security import TickerValidator
from src.services.corridor import (
    CorridorQuoteCache,
    make_bsm_greeks_fallback,
    wall_anchors_from_report,
)
from src.services.ledger import LedgerService
from src.services.paper_trades import PaperTradeService
from src.services.schemas import (
    ChainAnalyticsResponse,
    ChainMetadataPayload,
    CorridorQuotesRequest,
    CorridorStopResponse,
    GatewayStatusResponse,
    LedgerDashboardResponse,
    LedgerImportResponse,
    LedgerMTMRefreshResponse,
    LedgerSyncResponse,
    PaperTradesLedgerSyncResponse,
    RefreshRequest,
    SnapshotsListResponse,
    StrategyEvaluateRequest,
    StrategySuggestRequest,
    StrategySuggestResponse,
)
from src.storage.csv_repository import CSVRepository
from src.storage.ledger_repository import LedgerRepository
from src.storage.quality import measure_quote_quality
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, SpotSource, infer_chain_step_and_range

logger = logging.getLogger(__name__)


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
        ledger_service: LedgerService | None = None,
        paper_trades: PaperTradeService | None = None,
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
        self.refresh_lock = asyncio.Lock()
        self.refresh_owner: str | None = None
        self.last_corridor_stop: dict[str, Any] | None = None
        ledger_repo = LedgerRepository(self.settings.ledger.ledger_dir, root=self.repo.root)
        self.ledger = ledger_service or LedgerService(
            settings=self.settings,
            repo=ledger_repo,
            broker_client=self.broker_client,
            sleep=self.fetcher.sleep,
            corridor_stopper=self._stop_corridor_for_ledger,
            refresh_acquire=self.acquire_refresh,
            refresh_release=self.release_refresh,
            refresh_owner_getter=lambda: self.refresh_owner,
            on_ledger_changed=self._handle_ledger_changed,
        )
        self.ledger.ensure_layout()
        self.paper_trades = paper_trades or PaperTradeService(
            self.settings,
            self.repo,
            self.fetcher,
            clock=self.fetcher.clock,
        )
        self.validator = TickerValidator(
            watchlist=self.settings.watchlist.hardcoded_tickers,
            allow_custom=self.settings.watchlist.allow_custom_ticker,
        )
        self._ticker_capabilities: dict[str, tuple[list[float], list[float], int]] = {}
        self.corridor_cache = CorridorQuoteCache()

    @property
    def tracked_dir(self) -> Path:
        """Tracked paper-trade quote directory resolved strictly under `repo.root`."""
        return self.paper_trades.store.tracked_dir

    def _utc_now_iso(self) -> str:
        dt = self.fetcher.clock()
        aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
        return aware.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def acquire_refresh(self, owner: str, timeout_sec: float = 0.0) -> bool:
        """Acquire the IBKR 100-line budget lock; waits up to `timeout_sec` when `> 0`."""
        if not self.refresh_lock.locked():
            await self.refresh_lock.acquire()
            self.refresh_owner = owner
            return True
        if timeout_sec <= 0.0:
            return False
        try:
            await asyncio.wait_for(self.refresh_lock.acquire(), timeout=timeout_sec)
        except TimeoutError:
            return False
        self.refresh_owner = owner
        return True

    def release_refresh(self) -> None:
        """Release the IBKR refresh lock if currently held."""
        self.refresh_owner = None
        if self.refresh_lock.locked():
            self.refresh_lock.release()

    def _busy_error(self) -> RefreshBusyError:
        holder = self.refresh_owner or "another operation"
        return RefreshBusyError(
            f"Another IBKR refresh ({holder}) is currently in progress; retry shortly.",
            owner=holder,
            retry_after_sec=10,
        )

    async def _stop_corridor(self, *, reason: str, ticker: str | None = None) -> int:
        if self.corridor.active_stream_count <= 0:
            return 0
        stopped = await self.corridor.stop_all_streams()
        self.last_corridor_stop = {
            "reason": reason,
            "ticker": ticker,
            "stopped_subscriptions": stopped,
            "stopped_at_utc": self._utc_now_iso(),
        }
        return stopped

    async def _stop_corridor_for_ledger(self) -> int:
        return await self._stop_corridor(reason="ledger")

    def _handle_ledger_changed(self) -> None:
        if hasattr(self, "paper_trades"):
            self.sync_paper_trades_from_ledger()

    def _safe_sync_paper_trades_from_ledger(self, context: str) -> None:
        try:
            self.sync_paper_trades_from_ledger()
        except Exception as exc:
            logger.warning("%s ledger -> paper trade sync failed: %s", context, exc)

    def sync_paper_trades_from_ledger(self) -> PaperTradesLedgerSyncResponse:
        """Reconcile open Trade Ledger option positions into the Paper Trade registry."""
        report = self.ledger.build_full_open_report()
        return self.paper_trades.sync_from_ledger_report(report)

    def ensure_layout(self) -> None:
        """Ensure chain CSV, ledger archive, and paper-trade directories exist."""
        self.repo.ensure_layout()
        self.ledger.ensure_layout()
        self.paper_trades.ensure_layout()
        if hasattr(self.ledger, "repo") and self.ledger.repo.load_positions().height > 0:
            self._safe_sync_paper_trades_from_ledger("startup")

    def validate_ticker(self, raw_ticker: str) -> str:
        return self.validator.validate_selection(raw_ticker)

    def get_status(self, ticker: str | None = None) -> GatewayStatusResponse:
        ibkr = self.settings.ibkr
        wl = self.settings.watchlist
        oc = self.settings.options_chain
        corridor = self.settings.corridor
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
        ledger_status = self.ledger.get_ledger_status()
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
            include_short_dte_for_backtest=oc.include_short_dte_for_backtest,
            available_strike_steps=avail_steps,
            available_range_pcts=avail_ranges,
            active_capabilities_ticker=cap_sym if caps else None,
            flex_configured=ledger_status.flex_configured,
            flex_monthly_configured=ledger_status.flex_monthly_configured,
            flex_trade_conf_configured=ledger_status.flex_trade_conf_configured,
            gateway_ledger_sync_enabled=ledger_status.gateway_sync_enabled,
            corridor_default_window_strikes=corridor.default_window_strikes,
            corridor_default_max_contracts=corridor.default_max_contracts,
            corridor_hard_max_contracts=corridor.hard_max_contracts,
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
        quoted_contract_count: int | None = None,
        quote_coverage: float | None = None,
        prior_close_only_count: int | None = None,
    ) -> ChainAnalyticsResponse:
        enriched = self._enrich_if_needed(df)
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

        if (
            quoted_contract_count is None
            or quote_coverage is None
            or prior_close_only_count is None
        ):
            quality = measure_quote_quality(enriched)
            resolved_quoted = quality.quoted_contract_count
            resolved_coverage = quality.quote_coverage
            resolved_prior_close = quality.prior_close_only_count
        else:
            resolved_quoted = quoted_contract_count
            resolved_coverage = quote_coverage
            resolved_prior_close = prior_close_only_count

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
            quoted_contract_count=resolved_quoted,
            quote_coverage=resolved_coverage,
            prior_close_only_count=resolved_prior_close,
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

    def _isolate_dashboard_dte(
        self,
        df: pl.DataFrame,
        *,
        dte_min: int | None = None,
        dte_max: int | None = None,
        preserve_expiry: str | None = None,
    ) -> tuple[pl.DataFrame, bool]:
        """Filter `df` to requested DTE bounds or isolate `>= oc.dte_min` on merged snapshots.

        When `dte_min` and `dte_max` are omitted, short-DTE rows (`< oc.dte_min`) are hidden
        ONLY when the snapshot contains BOTH `< oc.dte_min` and `>= oc.dte_min` rows (and
        `preserve_expiry` does not explicitly point to a short-DTE expiry). Synthetic
        short-DTE-only frames are left intact.
        """
        if dte_min is not None and dte_max is not None and dte_min > dte_max:
            raise InvalidRequestError("dte_min must be <= dte_max")
        if dte_min is not None or dte_max is not None:
            filtered = df
            if dte_min is not None:
                filtered = filtered.filter(pl.col("dte") >= dte_min)
            if dte_max is not None:
                filtered = filtered.filter(pl.col("dte") <= dte_max)
            return filtered, True

        oc = self.settings.options_chain
        if preserve_expiry is not None:
            target_rows = df.filter(pl.col("expiry") == preserve_expiry)
            if target_rows.height > 0 and target_rows.filter(pl.col("dte") < oc.dte_min).height > 0:
                return df, False

        has_short = df.filter(pl.col("dte") < oc.dte_min).height > 0
        if not has_short:
            return df, False
        primary = df.filter(pl.col("dte").is_between(oc.dte_min, oc.dte_max))
        if primary.height == 0:
            primary = df.filter(pl.col("dte") >= oc.dte_min)
        if primary.height > 0:
            return primary, True
        return df, False

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
        df, explicit_filter = self._isolate_dashboard_dte(df, dte_min=dte_min, dte_max=dte_max)
        if explicit_filter and df.height == 0:
            raise SnapshotNotFoundError(f"No contracts found in requested DTE range for {symbol}")
        snap_meta = next(
            (m for m in self.repo.list_snapshots(symbol) if m.snapshot_id == loaded_snap_id),
            None,
        )
        has_meta_quality = (
            snap_meta is not None
            and df.height == snap_meta.contract_count
            and (
                (snap_meta.quoted_contract_count or 0) > 0
                or (snap_meta.prior_close_only_count or 0) > 0
                or snap_meta.contract_count == 0
            )
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
            quoted_contract_count=(
                snap_meta.quoted_contract_count if snap_meta and has_meta_quality else None
            ),
            quote_coverage=(snap_meta.quote_coverage if snap_meta and has_meta_quality else None),
            prior_close_only_count=(
                snap_meta.prior_close_only_count if snap_meta and has_meta_quality else None
            ),
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
        wall_df, isolated = self._isolate_dashboard_dte(enriched, preserve_expiry=target_expiry)
        walls = compute_walls(wall_df, top_n=self.settings.analytics.top_walls_count)
        resolved_target = target_expiry
        if resolved_target is None and isolated and wall_df.height > 0:
            resolved_target = str(wall_df.sort(["dte", "expiry"])["expiry"][0])
        try:
            return select_wall_corridor(
                enriched,
                wall_anchors=wall_anchors_from_report(walls),
                target_expiry=resolved_target,
                window_strikes=window_strikes,
                max_contracts=max_contracts,
                priority_legs=priority_legs,
            )
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc

    async def fetch_corridor_quotes(self, payload: CorridorQuotesRequest) -> CorridorQuoteFrame:
        symbol, snap_id, enriched = self._load_enriched_chain(
            payload.ticker,
            payload.snapshot_id,
            apply_corridor_overlay=False,
        )
        hard_cap = min(self.settings.corridor.hard_max_contracts, CORRIDOR_MAX_CONTRACTS)
        effective_max = min(payload.max_contracts, hard_cap)
        selection = self._select_corridor(
            enriched,
            target_expiry=payload.target_expiry,
            window_strikes=payload.window_strikes,
            max_contracts=effective_max,
            priority_legs=payload.priority_legs,
        )
        if not await self.acquire_refresh(f"corridor:{symbol}"):
            raise self._busy_error()
        try:
            await self._stop_corridor(reason="corridor_snapshot", ticker=symbol)
            return await self.corridor.fetch_corridor_quotes(
                selection,
                snapshot_id=snap_id,
                on_frame=self.corridor_cache.record,
            )
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc
        finally:
            self.release_refresh()

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
        if self.refresh_lock.locked():
            raise self._busy_error()
        _, snap_id, enriched = self._load_enriched_chain(
            raw_ticker,
            snapshot_id,
            apply_corridor_overlay=False,
        )
        hard_cap = min(self.settings.corridor.hard_max_contracts, CORRIDOR_MAX_CONTRACTS)
        effective_max = min(max_contracts, hard_cap)
        selection = self._select_corridor(
            enriched,
            target_expiry=target_expiry,
            window_strikes=window_strikes,
            max_contracts=effective_max,
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
        if stopped > 0:
            self.last_corridor_stop = {
                "reason": "manual_stop",
                "ticker": None,
                "stopped_subscriptions": stopped,
                "stopped_at_utc": self._utc_now_iso(),
            }
        return CorridorStopResponse(
            stopped_subscriptions=stopped,
            active_streams=self.corridor.active_stream_count,
        )

    def _normalize_strategy_filters(
        self,
        min_net_credit_dollars: float | None,
        min_ror_pct: float | None,
    ) -> tuple[float | None, float | None]:
        def _clean(field_name: str, val: float | None, upper: float | None = None) -> float | None:
            if val is None:
                return None
            if not math.isfinite(val) or val < 0.0 or (upper is not None and val > upper):
                bound = f"[0, {upper:g}]" if upper is not None else ">= 0"
                raise InvalidRequestError(f"{field_name} must be finite and {bound} (got {val!r}).")
            return None if val == 0.0 else val

        return (
            _clean("min_net_credit_dollars", min_net_credit_dollars),
            _clean("min_ror_pct", min_ror_pct, upper=1000.0),
        )

    def suggest_strategies(self, payload: StrategySuggestRequest) -> StrategySuggestResponse:
        min_credit, min_ror = self._normalize_strategy_filters(
            payload.min_net_credit_dollars, payload.min_ror_pct
        )
        symbol, snap_id, enriched = self._load_enriched_chain(payload.ticker, payload.snapshot_id)
        scoped_df, explicit_filter = self._isolate_dashboard_dte(
            enriched,
            dte_min=payload.dte_min,
            dte_max=payload.dte_max,
            preserve_expiry=payload.target_expiry,
        )
        if explicit_filter and scoped_df.height == 0:
            raise InvalidRequestError(f"No contracts found in requested DTE range for {symbol}")
        walls = compute_walls(scoped_df, top_n=self.settings.analytics.top_walls_count)
        kinds: tuple[PresetStrategyKind, ...] = (
            (payload.strategy_kind,) if payload.strategy_kind is not None else PRESET_STRATEGIES
        )
        evaluations: list[StrategyEvaluationResponse] = []
        errors: list[str] = []
        for kind in kinds:
            try:
                evaluations.append(
                    evaluate_strategy_engine(
                        scoped_df,
                        strategy_kind=kind,
                        target_expiry=payload.target_expiry,
                        days_forward=payload.days_forward,
                        iv_shift=payload.iv_shift,
                        qty_multiplier=payload.qty_multiplier,
                        risk_free_rate=self.settings.analytics.risk_free_rate,
                        walls_report=walls,
                        min_net_credit_dollars=min_credit,
                        min_ror_pct=min_ror,
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
        min_credit, min_ror = self._normalize_strategy_filters(
            payload.min_net_credit_dollars, payload.min_ror_pct
        )
        symbol, _, enriched = self._load_enriched_chain(payload.ticker, payload.snapshot_id)
        if payload.dte_min is not None or payload.dte_max is not None:
            scoped_df, _ = self._isolate_dashboard_dte(
                enriched, dte_min=payload.dte_min, dte_max=payload.dte_max
            )
            if scoped_df.height == 0:
                raise InvalidRequestError(f"No contracts found in requested DTE range for {symbol}")
        elif payload.legs is None:
            scoped_df, _ = self._isolate_dashboard_dte(
                enriched, preserve_expiry=payload.target_expiry
            )
        else:
            scoped_df = enriched
        try:
            return evaluate_strategy_engine(
                scoped_df,
                strategy_kind=payload.strategy_kind,
                target_expiry=payload.target_expiry,
                legs=payload.legs,
                days_forward=payload.days_forward,
                iv_shift=payload.iv_shift,
                qty_multiplier=payload.qty_multiplier,
                risk_free_rate=self.settings.analytics.risk_free_rate,
                min_net_credit_dollars=min_credit,
                min_ror_pct=min_ror,
            )
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc

    async def refresh_chain(self, req: RefreshRequest) -> ChainAnalyticsResponse:
        symbol = self.validate_ticker(req.ticker)
        oc = self.settings.options_chain
        eff_dte_min = oc.dte_min if req.dte_min is None else int(req.dte_min)
        eff_dte_max = oc.dte_max if req.dte_max is None else int(req.dte_max)
        if eff_dte_min > eff_dte_max:
            raise InvalidRequestError("dte_min must be <= dte_max")
        disp_min = eff_dte_min if req.display_dte_min is None else int(req.display_dte_min)
        disp_max = eff_dte_max if req.display_dte_max is None else int(req.display_dte_max)
        if disp_min > disp_max:
            raise InvalidRequestError("display_dte_min must be <= display_dte_max")
        include_short = (
            oc.include_short_dte_for_backtest
            if req.include_short_dte is None
            else bool(req.include_short_dte)
        )

        if not await self.acquire_refresh(f"refresh:{symbol}"):
            raise self._busy_error()
        try:
            notices: list[str] = []
            stopped_lines = await self._stop_corridor(reason="refresh_chain", ticker=symbol)
            if stopped_lines > 0:
                notices.append(
                    f"Stopped {stopped_lines} active Wall Corridor quote stream(s) "
                    "before chain refresh to stay within IBKR's 100-line ceiling."
                )
            fetch_res = await self.fetcher.fetch_option_chain(
                symbol,
                range_pct=req.range_pct,
                strike_step=req.strike_step,
                dte_min=req.dte_min,
                dte_max=req.dte_max,
                include_short_dte=include_short,
            )
            raw_df = pl.DataFrame(
                [r.model_dump() for r in fetch_res.rows],
                schema=OPTION_CHAIN_POLARS_SCHEMA,
            )
            enriched_df = BSMFallbackEngine.enrich_missing_greeks(
                raw_df, risk_free_rate=self.settings.analytics.risk_free_rate
            )
            inferred_step, _ = infer_chain_step_and_range(enriched_df)
            plan = fetch_res.plan
            plan_step = plan.strikes.effective_step
            actual_step = (
                inferred_step
                if (inferred_step is not None and inferred_step > plan_step + 1e-6)
                else plan_step
            )
            req_step = plan.strikes.requested_step
            notices.extend(fetch_res.notices)
            if actual_step > plan_step + 1e-6:
                notices.append(
                    f"IBKR strikes for {symbol} spaced at ${actual_step:g} "
                    f"(requested ${req_step:g})."
                )
            avail_steps = (
                list(plan.available_strike_steps)
                if plan.available_strike_steps
                else list(oc.allowed_strike_steps)
            )
            avail_ranges = (
                list(plan.available_range_pcts)
                if plan.available_range_pcts
                else list(oc.allowed_range_pcts)
            )
            validated_count = (
                int(plan.exchange_validated_strike_count)
                if plan.exchange_validated_strike_count > 0
                else len({float(r.strike) for r in fetch_res.rows})
            )
            self._ticker_capabilities[symbol] = (avail_steps, avail_ranges, validated_count)
            meta = self.repo.save_snapshot(
                symbol,
                enriched_df.to_dicts(),
                range_pct=plan.range_pct,
                strike_step=actual_step,
                requested_strike_step=req_step,
                spot_source=plan.underlying.spot_source,
            )
            _, quote_notices = await self.paper_trades.quote_active_trades_for_refresh(
                symbol,
                underlying_conid=plan.underlying.conid,
            )
            notices.extend(quote_notices)

            display_df = enriched_df.filter(pl.col("dte").is_between(disp_min, disp_max))
            if display_df.height == 0 and enriched_df.height > 0:
                display_df = enriched_df
                notices.append(
                    f"No contracts in display DTE window [{disp_min}, {disp_max}]; "
                    f"showing all {enriched_df.height} archived contract(s)."
                )
            same_slice = display_df.height == enriched_df.height
            return self.build_analytics_payload(
                display_df,
                source="LIVE_REFRESH",
                notices=notices,
                range_pct=plan.range_pct,
                strike_step=actual_step,
                requested_strike_step=req_step,
                exchange_validated_strike_count=validated_count,
                exchange_native_verified=bool(plan.exchange_native_verified),
                available_strike_steps=avail_steps,
                available_range_pcts=avail_ranges,
                spot_source=meta.spot_source,
                quoted_contract_count=meta.quoted_contract_count if same_slice else None,
                quote_coverage=meta.quote_coverage if same_slice else None,
                prior_close_only_count=meta.prior_close_only_count if same_slice else None,
            )
        finally:
            self.release_refresh()

    def get_ledger_dashboard(
        self,
        *,
        window: str = "1M",
        ticker: str = "ALL",
        group_by: str = "campaigns",
        view: str = "realized",
        include_stock_gain: bool = False,
    ) -> LedgerDashboardResponse:
        """Return filtered ledger analytics report and sync status."""
        return self.ledger.get_ledger_dashboard(
            window=window,
            ticker=ticker,
            group_by=group_by,
            view=view,
            include_stock_gain=include_stock_gain,
        )

    async def sync_ledger(
        self,
        *,
        mode: str = "auto",
        include_gateway: bool | None = None,
    ) -> LedgerSyncResponse:
        """Synchronize the trade ledger via Flex Web Service and/or Gateway."""
        if getattr(self.ledger, "_refresh_acquire", None) is not None:
            res = await self.ledger.sync_ledger(mode=mode, include_gateway=include_gateway)
        else:
            run_gateway = self.settings.ledger.gateway_sync_enabled and include_gateway is not False
            if not run_gateway:
                res = await self.ledger.sync_ledger(mode=mode, include_gateway=include_gateway)
            else:
                if not await self.acquire_refresh("ledger:sync"):
                    raise self._busy_error()
                try:
                    await self._stop_corridor(reason="ledger_sync")
                    res = await self.ledger.sync_ledger(mode=mode, include_gateway=include_gateway)
                finally:
                    self.release_refresh()
        if getattr(self.ledger, "_on_ledger_changed", None) is None:
            self._safe_sync_paper_trades_from_ledger("sync_ledger")
        return res

    def import_ledger_file(self, filename: str, raw_bytes: bytes) -> LedgerImportResponse:
        """Import a manual IBKR Flex XML or Activity CSV file into the trade ledger."""
        res = self.ledger.import_ledger_file(filename, raw_bytes)
        if getattr(self.ledger, "_on_ledger_changed", None) is None:
            self._safe_sync_paper_trades_from_ledger("import_ledger_file")
        return res

    async def refresh_open_ledger_mtm(self) -> LedgerMTMRefreshResponse:
        """Refresh live mark-to-market quotes for open ledger positions."""
        if getattr(self.ledger, "_refresh_acquire", None) is not None:
            return await self.ledger.refresh_open_mtm()
        if not await self.acquire_refresh("ledger:mtm"):
            raise self._busy_error()
        try:
            await self._stop_corridor(reason="ledger_mtm")
            return await self.ledger.refresh_open_mtm()
        finally:
            self.release_refresh()
