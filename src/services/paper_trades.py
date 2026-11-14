"""Paper Trade orchestration service: registration, auto-expiry, stop, and refresh quoting."""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, Final

import polars as pl
from pydantic import ValidationError

from src.analytics.bsm_fallback import BSMFallbackEngine
from src.analytics.ledger import (
    LedgerAnalyticsReport,
    LedgerCampaignRow,
    LedgerOpenPositionView,
    LedgerStrategyType,
)
from src.analytics.strategies import StrategyKind, StrategyLegInput, evaluate_strategy
from src.broker.fetcher import ExactContractSpec, PagedOptionChainFetcher
from src.core.calendar import US_EASTERN, et_date
from src.core.config import Settings
from src.core.exceptions import (
    AnalyzerError,
    BacktestValidationError,
    DuplicatePaperTradeError,
    InvalidRequestError,
    InvalidTickerError,
    OptionChainFetchError,
    PaperTradeCapError,
    PaperTradeStateError,
    SnapshotNotFoundError,
)
from src.core.security import TickerValidator
from src.services.schemas import (
    PaperTradesLedgerSyncResponse,
    PaperTradesListResponse,
    RegisterPaperTradeRequest,
)
from src.storage.csv_repository import CSVRepository
from src.storage.paper_trade_store import (
    MAX_PAPER_TRADE_LEGS,
    PaperQuoteRecord,
    PaperTrade,
    PaperTradeLeg,
    PaperTradeStatus,
    PaperTradeStore,
)
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, SNAPSHOT_ID_RE, SnapshotMeta

logger = logging.getLogger(__name__)

PAPER_TRADE_ID_RE: Final = re.compile(r"^[A-Z]{1,5}-\d{8}-[0-9a-f]{8}$")
_VALID_STATUSES: Final[frozenset[str]] = frozenset({"ACTIVE", "EXPIRED", "STOPPED", "CLOSED"})

_LEDGER_STRATEGY_MAP: Final[dict[str, tuple[StrategyKind, str]]] = {
    "BULL_PUT_SPREAD": ("BULL_PUT_SPREAD", "BULL PUT SPREAD"),
    "BEAR_CALL_SPREAD": ("CUSTOM", "BEAR CALL SPREAD"),
    "BULL_CALL_SPREAD": ("BULL_CALL_SPREAD", "BULL CALL SPREAD"),
    "BEAR_PUT_SPREAD": ("CUSTOM", "BEAR PUT SPREAD"),
    "IRON_CONDOR": ("IRON_CONDOR", "IRON CONDOR"),
    "IRON_BUTTERFLY": ("CUSTOM", "IRON BUTTERFLY"),
    "DIAGONAL_SPREAD": ("DIAGONAL_SPREAD", "DIAGONAL SPREAD"),
    "CALENDAR_SPREAD": ("CUSTOM", "CALENDAR SPREAD"),
    "STRANGLE": ("CUSTOM", "STRANGLE"),
    "STRADDLE": ("CUSTOM", "STRADDLE"),
    "COVERED_CALL": ("CUSTOM", "COVERED CALL"),
    "NAKED_CALL": ("CUSTOM", "NAKED CALL"),
    "SHORT_PUT": ("CUSTOM", "SHORT PUT"),
    "LONG_CALL": ("CUSTOM", "LONG CALL"),
    "LONG_PUT": ("CUSTOM", "LONG PUT"),
    "CUSTOM_COMBO": ("CUSTOM", "CUSTOM COMBO"),
    "STOCK_LOT": ("CUSTOM", "STOCK LOT"),
}


def map_ledger_strategy(strategy_type: LedgerStrategyType | str) -> tuple[StrategyKind, str]:
    """Map `LedgerStrategyType` to `(StrategyKind, readable_label)`."""
    mapped = _LEDGER_STRATEGY_MAP.get(strategy_type)
    if mapped is not None:
        return mapped
    return "CUSTOM", strategy_type.replace("_", " ").strip() or "CUSTOM"


def _to_iso_expiry(raw: str) -> str:
    cleaned = raw.strip()
    if re.fullmatch(r"\d{8}", cleaned):
        iso = f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]}"
        date.fromisoformat(iso)
        return iso
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        date.fromisoformat(cleaned)
        return cleaned
    raise ValueError(f"Invalid option expiry {raw!r}")


def _parse_opened_dt(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        dt_et = datetime.combine(date.fromisoformat(text), time(10, 0), tzinfo=US_EASTERN)
        return dt_et.astimezone(UTC)
    iso_text = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def resolve_tracked_dir(repo: CSVRepository, tracked_dir: str) -> Path:
    """Resolve `tracker.tracked_dir` relative to `repo.root` (never `PROJECT_ROOT`)."""
    path = Path(tracked_dir)
    return path if path.is_absolute() else (repo.root / path).resolve()


def _parse_iso_date(raw: str, *, label: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise BacktestValidationError(f"Invalid {label} {raw!r}; expected YYYY-MM-DD")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise BacktestValidationError(f"Invalid {label} {raw!r}: {exc}") from exc


class PaperTradeService:
    """Coordinates paper trade lifecycle and tracked quote collection on refreshes."""

    def __init__(
        self,
        settings: Settings,
        repo: CSVRepository,
        fetcher: PagedOptionChainFetcher,
        *,
        store: PaperTradeStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.fetcher = fetcher
        self.clock = clock or fetcher.clock
        self._lock = threading.RLock()
        if store is not None:
            self.store = store
        else:
            tracked_dir = resolve_tracked_dir(repo, settings.tracker.tracked_dir)
            self.store = PaperTradeStore(
                metadata_dir=repo.index_path.parent,
                tracked_dir=tracked_dir,
            )

    def _now_utc(self) -> datetime:
        dt = self.clock()
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

    def _now_iso(self) -> str:
        return self._now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")

    def _today_et(self) -> date:
        return et_date(self._now_utc())

    def ensure_layout(self) -> None:
        """Provision metadata and tracked directories idempotently."""
        self.store.ensure_layout()

    def _load_synced_trades(self) -> list[PaperTrade]:
        """Load trades and atomically expire any `ACTIVE` trade with `expiry < today_et`."""
        with self._lock:
            trades = self.store.load_trades()
            today_et = self._today_et()
            now_iso = self._now_iso()
            changed = False
            synced: list[PaperTrade] = []
            for trade in trades:
                if trade.status == "ACTIVE" and date.fromisoformat(trade.expiry) < today_et:
                    synced.append(
                        trade.model_copy(
                            update={"status": "EXPIRED", "status_changed_utc": now_iso}
                        )
                    )
                    changed = True
                else:
                    synced.append(trade)
            if changed:
                self.store.save_trades(synced)
            return synced

    def list_trades(
        self,
        *,
        status: PaperTradeStatus | None = None,
        ticker: str | None = None,
    ) -> tuple[list[PaperTrade], int]:
        """Return `(filtered_trades, total_active_count)`, auto-expiring past-due trades."""
        if status is not None and status not in _VALID_STATUSES:
            raise InvalidRequestError(
                f"Invalid paper trade status {status!r}; expected one of {sorted(_VALID_STATUSES)}"
            )
        symbol = TickerValidator.validate(ticker) if ticker is not None else None
        all_trades = self._load_synced_trades()
        active_count = sum(1 for t in all_trades if t.status == "ACTIVE")
        filtered = [
            t
            for t in all_trades
            if (status is None or t.status == status) and (symbol is None or t.ticker == symbol)
        ]
        filtered.sort(key=lambda t: (t.created_utc, t.id), reverse=True)
        return filtered, active_count

    def list_trades_response(
        self,
        *,
        status: PaperTradeStatus | None = None,
        ticker: str | None = None,
    ) -> PaperTradesListResponse:
        """Return structured `PaperTradesListResponse` for API consumers."""
        trades, active_count = self.list_trades(status=status, ticker=ticker)
        return PaperTradesListResponse(
            active_count=active_count,
            max_active_trades=self.settings.tracker.max_active_trades,
            trades=trades,
        )

    def register_trade(
        self,
        ticker: str,
        snapshot_id: str,
        strategy_kind: StrategyKind,
        legs: Sequence[StrategyLegInput],
        qty: int = 1,
    ) -> PaperTrade:
        """Validate against snapshot CSV, price entry mid cash, and persist atomically."""
        symbol = TickerValidator.validate(ticker)
        snap_id = snapshot_id.strip()
        if SNAPSHOT_ID_RE.fullmatch(snap_id) is None or not snap_id.startswith(f"{symbol}_"):
            raise BacktestValidationError(
                f"Invalid snapshot_id {snapshot_id!r} for ticker {symbol}"
            )
        if not legs or len(legs) > MAX_PAPER_TRADE_LEGS:
            raise BacktestValidationError(
                f"Paper trade requires between 1 and {MAX_PAPER_TRADE_LEGS} legs"
            )
        if qty < 1:
            raise BacktestValidationError("qty must be >= 1")
        leg_qtys = {leg.qty for leg in legs}
        if len(leg_qtys) > 1:
            raise BacktestValidationError(
                "Paper trade legs must share a uniform per-leg qty; use trade qty multiplier."
            )

        today_et = self._today_et()
        for leg in legs:
            exp_day = _parse_iso_date(leg.expiry, label="leg expiry")
            if exp_day < today_et:
                raise BacktestValidationError(
                    f"Leg expiry {leg.expiry} is already in the past "
                    f"(today ET: {today_et.isoformat()})"
                )

        df = self.repo.load_snapshot(symbol, snap_id)
        try:
            evaluation = evaluate_strategy(
                df,
                strategy_kind=strategy_kind,
                legs=list(legs),
                qty_multiplier=qty,
                risk_free_rate=self.settings.analytics.risk_free_rate,
            )
        except ValueError as exc:
            raise BacktestValidationError(str(exc)) from exc

        trade_legs: list[PaperTradeLeg] = []
        for spec, eval_leg in zip(legs, evaluation.legs, strict=True):
            con_id = eval_leg.option_conid
            if con_id is None or con_id <= 0:
                raise BacktestValidationError(
                    f"Missing qualified IBKR option_conid for {spec.expiry} "
                    f"{spec.strike:g}{spec.right}"
                )
            trade_legs.append(
                PaperTradeLeg(
                    side=spec.side,
                    right=spec.right,
                    expiry=eval_leg.expiry,
                    strike=eval_leg.strike,
                    con_id=con_id,
                )
            )

        first_row = df.row(0, named=True)
        now_utc = self._now_utc()
        effective_qty = qty * legs[0].qty
        new_trade = PaperTrade(
            id=f"{symbol}-{now_utc.strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}",
            ticker=symbol,
            strategy_kind=strategy_kind,
            status="ACTIVE",
            legs=trade_legs,
            qty=effective_qty,
            underlying_conid=int(first_row["underlying_conid"]),
            created_from_snapshot_id=snap_id,
            created_utc=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            market_date=str(first_row["market_date"]),
            entry_net_cash=float(evaluation.pricing.mid_cash_total),
        )

        with self._lock:
            existing = self._load_synced_trades()
            cand_key = new_trade.legs_key()
            if any(
                t.status == "ACTIVE" and t.ticker == symbol and t.legs_key() == cand_key
                for t in existing
            ):
                raise DuplicatePaperTradeError(
                    f"An ACTIVE paper trade for {symbol} with the exact same legs already exists."
                )
            active_count = sum(1 for t in existing if t.status == "ACTIVE")
            max_active = self.settings.tracker.max_active_trades
            if active_count >= max_active:
                raise PaperTradeCapError(
                    f"Active paper trade cap reached ({active_count}/{max_active}); "
                    "stop an existing trade first."
                )
            self.store.save_trades([*existing, new_trade])
        return new_trade

    def register_from_request(self, req: RegisterPaperTradeRequest) -> PaperTrade:
        """Convenience adapter accepting `RegisterPaperTradeRequest`."""
        return self.register_trade(
            req.ticker,
            req.snapshot_id,
            req.strategy_kind,
            req.legs,
            qty=req.qty,
        )

    def stop_trade(self, trade_id: str) -> PaperTrade:
        """Transition an `ACTIVE` paper trade to `STOPPED` atomically."""
        tid = trade_id.strip()
        if PAPER_TRADE_ID_RE.fullmatch(tid) is None:
            raise InvalidRequestError(f"Invalid paper trade ID format: {trade_id!r}")

        with self._lock:
            trades = self._load_synced_trades()
            idx = next((i for i, t in enumerate(trades) if t.id == tid), None)
            if idx is None:
                raise SnapshotNotFoundError(f"Paper trade not found: {tid}")
            target = trades[idx]
            if target.status != "ACTIVE":
                raise PaperTradeStateError(
                    f"Cannot stop paper trade {tid}: status is {target.status}"
                )
            stopped = target.model_copy(
                update={"status": "STOPPED", "status_changed_utc": self._now_iso()}
            )
            trades[idx] = stopped
            self.store.save_trades(trades)
            return stopped

    def _build_ledger_paper_trade(
        self,
        group_key: str,
        group: Sequence[LedgerOpenPositionView],
        *,
        campaign: LedgerCampaignRow | None,
        snapshots_by_symbol: dict[str, list[SnapshotMeta]],
        now_utc: datetime,
    ) -> PaperTrade | None:
        if not group or len(group) > MAX_PAPER_TRADE_LEGS:
            logger.warning(
                "Skipping ledger group %s with %d option leg(s) (limit %d)",
                group_key,
                len(group),
                MAX_PAPER_TRADE_LEGS,
            )
            return None
        try:
            symbol = TickerValidator.validate(group[0].underlying)
            for op in group[1:]:
                if TickerValidator.validate(op.underlying) != symbol:
                    logger.warning(
                        "Skipping ledger group %s spanning multiple underlyings", group_key
                    )
                    return None
        except InvalidTickerError as exc:
            logger.warning(
                "Skipping ledger group %s with unsupported ticker %r: %s",
                group_key,
                group[0].underlying,
                exc,
            )
            return None

        leg_counts = [max(1, int(round(abs(op.quantity)))) for op in group]
        trade_qty = max(1, math.gcd(*leg_counts))
        trade_legs: list[PaperTradeLeg] = []
        try:
            for op, count_i in zip(group, leg_counts, strict=True):
                assert op.expiry is not None and op.strike is not None and op.right is not None
                avg_px = float(op.avg_cost_price)
                entry_px = max(0.0, round(avg_px, 4)) if math.isfinite(avg_px) else None
                trade_legs.append(
                    PaperTradeLeg(
                        side="BUY" if op.quantity > 0 else "SELL",
                        right=op.right,
                        expiry=_to_iso_expiry(op.expiry),
                        strike=float(op.strike),
                        con_id=max(0, int(op.conid or 0)),
                        qty_ratio=max(1, count_i // trade_qty),
                        entry_price=entry_px,
                    )
                )
        except (ValueError, ValidationError) as exc:
            logger.warning("Skipping invalid ledger group %s leg: %s", group_key, exc)
            return None

        raw_strategy = campaign.strategy_type if campaign is not None else group[0].strategy_type
        strategy_kind, strategy_label = map_ledger_strategy(raw_strategy)

        opened_dt = _parse_opened_dt(campaign.opened_at) if campaign is not None else None
        if opened_dt is None:
            parsed_dates = [
                dt for op in group if (dt := _parse_opened_dt(op.opened_at)) is not None
            ]
            opened_dt = min(parsed_dates) if parsed_dates else now_utc

        created_utc = opened_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        market_date = et_date(opened_dt).isoformat()

        if symbol not in snapshots_by_symbol:
            snapshots_by_symbol[symbol] = self.repo.list_snapshots(symbol)
        day_snaps = [
            meta for meta in snapshots_by_symbol[symbol] if meta.market_date == market_date
        ]
        created_from_snapshot_id = (
            max(day_snaps, key=lambda m: (m.snapshot_ts_utc, m.snapshot_id)).snapshot_id
            if day_snaps
            else None
        )
        if (
            created_from_snapshot_id is not None
            and SNAPSHOT_ID_RE.fullmatch(created_from_snapshot_id) is None
        ):
            created_from_snapshot_id = None

        entry_net_cash = round(-sum(float(op.cost_basis_ex_fee) for op in group), 2) or 0.0
        digest = hashlib.sha1(f"{symbol}:{group_key}".encode()).hexdigest()[:8]
        trade_id = f"{symbol}-{market_date.replace('-', '')}-{digest}"

        try:
            return PaperTrade(
                id=trade_id,
                ticker=symbol,
                strategy_kind=strategy_kind,
                status="ACTIVE",
                legs=trade_legs,
                qty=trade_qty,
                underlying_conid=0,
                created_from_snapshot_id=created_from_snapshot_id,
                created_utc=created_utc,
                market_date=market_date,
                entry_net_cash=entry_net_cash,
                source="LEDGER",
                ledger_campaign_id=group_key,
                ledger_strategy_label=strategy_label,
            )
        except (ValueError, ValidationError) as exc:
            logger.warning("Skipping ledger group %s: %s", group_key, exc)
            return None

    def sync_from_ledger_report(
        self, report: LedgerAnalyticsReport
    ) -> PaperTradesLedgerSyncResponse:
        """Reconcile open Trade Ledger option campaigns/positions into `paper_trades.json`."""
        groups: dict[str, list[LedgerOpenPositionView]] = {}
        for op in report.open_positions:
            if op.asset_class != "OPT" or abs(op.quantity) <= 1e-6:
                continue
            if op.expiry is None or op.strike is None or op.right not in ("C", "P"):
                continue
            if not math.isfinite(op.strike) or op.strike <= 0.0:
                continue
            raw_cid = op.campaign_id.strip() if op.campaign_id else ""
            group_key = raw_cid or op.position_key
            groups.setdefault(group_key, []).append(op)

        campaigns_by_id: dict[str, LedgerCampaignRow] = {c.campaign_id: c for c in report.campaigns}
        snapshots_by_symbol: dict[str, list[SnapshotMeta]] = {}
        now_utc = self._now_utc()

        incoming: list[PaperTrade] = []
        for group_key, group in groups.items():
            trade = self._build_ledger_paper_trade(
                group_key,
                group,
                campaign=campaigns_by_id.get(group_key),
                snapshots_by_symbol=snapshots_by_symbol,
                now_utc=now_utc,
            )
            if trade is not None:
                incoming.append(trade)

        with self._lock:
            created_count, updated_count, closed_count, _ledger_trades = (
                self.store.sync_ledger_trades(incoming, now_utc=now_utc)
            )
            trades, active_count = self.list_trades(ticker=None)

        active_ledger_count = sum(
            1 for t in trades if t.source == "LEDGER" and t.status == "ACTIVE"
        )
        return PaperTradesLedgerSyncResponse(
            created_count=created_count,
            updated_count=updated_count,
            closed_count=closed_count,
            active_ledger_count=active_ledger_count,
            active_count=active_count,
            trades=trades,
        )

    async def quote_active_trades_for_refresh(
        self,
        ticker: str,
        *,
        underlying_conid: int = 0,
    ) -> tuple[PaperQuoteRecord | None, list[str]]:
        """Quote unique legs of `ACTIVE` trades for `ticker` after chain refresh cleanup."""
        symbol = TickerValidator.validate(ticker)
        trades = self._load_synced_trades()
        active_for_ticker = [t for t in trades if t.status == "ACTIVE" and t.ticker == symbol]
        if not active_for_ticker:
            return None, []

        spec_map: dict[tuple[str, float, str], ExactContractSpec] = {}
        for trade in active_for_ticker:
            for leg in trade.legs:
                spec = ExactContractSpec(
                    option_conid=leg.con_id,
                    expiry=leg.expiry,
                    strike=leg.strike,
                    right=leg.right,
                )
                key = spec.key()
                prev = spec_map.get(key)
                if prev is None or (prev.option_conid <= 0 and spec.option_conid > 0):
                    spec_map[key] = spec

        specs = list(spec_map.values())
        trade_ids = [t.id for t in active_for_ticker]
        try:
            result = await self.fetcher.fetch_exact_contracts(
                symbol,
                specs,
                underlying_conid=underlying_conid,
            )
            if not result.rows:
                raise OptionChainFetchError(
                    f"No quotes returned for active {symbol} paper-trade legs"
                )
            raw_df = pl.DataFrame(
                [r.model_dump() for r in result.rows],
                schema=OPTION_CHAIN_POLARS_SCHEMA,
            )
            needs_bsm = (
                raw_df.filter((pl.col("greeks_source") == "NONE") | pl.col("iv").is_null()).height
                > 0
            )
            rows_to_save: Sequence[Any] = (
                BSMFallbackEngine.enrich_missing_greeks(
                    raw_df, risk_free_rate=self.settings.analytics.risk_free_rate
                ).to_dicts()
                if needs_bsm
                else result.rows
            )
            self.store.save_quotes(symbol, rows_to_save)

            quoted_keys = {(r.expiry, round(r.strike, 4), r.right) for r in result.rows}
            quoted_trade_ids = [
                t.id
                for t in active_for_ticker
                if any(
                    (leg.expiry, round(leg.strike, 4), leg.right) in quoted_keys for leg in t.legs
                )
            ]
            record = PaperQuoteRecord(
                quote_id=f"{result.snapshot_id}-{uuid.uuid4().hex[:6]}",
                quoted_at_utc=result.snapshot_ts_utc,
                market_date=result.market_date,
                ticker=symbol,
                trade_ids=quoted_trade_ids or trade_ids,
                status="QUOTED",
                tracked_snapshot_id=result.snapshot_id,
                spot=result.underlying_price,
                spot_source=result.spot_source,
                market_data_type=result.market_data_type,
            )
            self.store.append_quote_record(record)
            if quoted_trade_ids:
                quoted_set = set(quoted_trade_ids)
                with self._lock:
                    latest = self._load_synced_trades()
                    updated = [
                        t.model_copy(update={"last_quote_utc": result.snapshot_ts_utc})
                        if (t.id in quoted_set and t.status == "ACTIVE")
                        else t
                        for t in latest
                    ]
                    self.store.save_trades(updated)

            notices = list(result.notices)
            if result.missing:
                notices.append(
                    f"{len(result.missing)} active {symbol} paper-trade contract(s) were "
                    "unqualified or missing during tracked quote refresh."
                )
            notices.append(
                f"Quoted {len(result.rows)} active paper-trade contract(s) for {symbol} "
                f"({result.snapshot_id})."
            )
            return record, notices
        except (AnalyzerError, OSError, ValueError) as exc:
            logger.warning("paper trade quote refresh failed for %s: %s", symbol, exc)
            now_utc = self._now_utc()
            err_rec = PaperQuoteRecord(
                quote_id=f"{symbol}-{now_utc.strftime('%Y%m%d_%H%M%S')}-{uuid.uuid4().hex[:6]}",
                quoted_at_utc=now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                market_date=self._today_et().isoformat(),
                ticker=symbol,
                trade_ids=trade_ids,
                status="ERROR",
                error=str(exc),
            )
            try:
                self.store.append_quote_record(err_rec)
            except OSError as log_exc:  # pragma: no cover - defensive
                logger.warning("failed to append paper quote error log: %s", log_exc)
            return err_rec, [f"Paper trade leg quote refresh warning for {symbol}: {exc}"]
