"""Historical backtest orchestration service: candidate entries and simulation runner."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from src.analytics.backtest import (
    BacktestConfig,
    BacktestSimulationResult,
    run_single_trade_replay,
    run_systematic_backtest,
)
from src.analytics.strategies import EvaluatedLeg, StrategyLegInput
from src.core.calendar import fmt_et
from src.core.exceptions import (
    AnalyzerError,
    BacktestValidationError,
    InvalidRequestError,
    SnapshotNotFoundError,
)
from src.services.analyzer import AnalyzerService
from src.services.paper_trades import PAPER_TRADE_ID_RE
from src.services.schemas import (
    BacktestEntriesResponse,
    BacktestEntry,
    BacktestRunRequest,
    BacktestRunResponse,
    BacktestSingleTradeSource,
)
from src.storage.backtest_store import (
    DailyChainBar,
    load_daily_chain_bars,
    load_tracked_quotes_by_date,
)
from src.storage.paper_trade_store import PaperTrade
from src.storage.quality import evaluate_snapshot
from src.storage.schema import SNAPSHOT_ID_RE, SnapshotMeta

_POLAND_TZ = ZoneInfo("Europe/Warsaw")

LegKey = tuple[tuple[str, str, float, str], ...]


def fmt_poland(ts: datetime) -> str:
    """Format a timestamp in Europe/Warsaw (`YYYY-MM-DD HH:MM CET`/`CEST`)."""
    aware = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
    return aware.astimezone(_POLAND_TZ).strftime("%Y-%m-%d %H:%M %Z")


def _parse_utc_ts(raw_ts: str) -> datetime:
    normalized = f"{raw_ts[:-1]}+00:00" if raw_ts.endswith("Z") else raw_ts
    parsed = datetime.fromisoformat(normalized)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _snapshot_in_window(
    meta: SnapshotMeta,
    start_date: date | None,
    end_date: date | None,
) -> bool:
    mkt_date = date.fromisoformat(meta.market_date)
    if start_date is not None and mkt_date < start_date:
        return False
    return not (end_date is not None and mkt_date > end_date)


def _validate_snapshot_id(snapshot_id: str, *, field_name: str) -> str:
    cleaned = snapshot_id.strip()
    if SNAPSHOT_ID_RE.fullmatch(cleaned) is None:
        raise BacktestValidationError(
            f"Invalid {field_name} {snapshot_id!r}; expected {{TICKER}}_{{YYYYMMDD}}_{{HHMMSS}}"
        )
    return cleaned


def _legs_key_from_inputs(legs: Sequence[StrategyLegInput | EvaluatedLeg]) -> LegKey:
    return tuple(
        sorted((leg.side, leg.right, round(float(leg.strike), 4), leg.expiry) for leg in legs)
    )


def _find_matching_paper_trade(
    trades: Sequence[PaperTrade],
    ticker: str,
    legs: Sequence[StrategyLegInput | EvaluatedLeg],
    entry_snapshot_id: str | None,
) -> PaperTrade | None:
    target_key = _legs_key_from_inputs(legs)
    matches = [t for t in trades if t.ticker == ticker and t.legs_key() == target_key]
    if not matches:
        return None
    exact_active = next(
        (
            t
            for t in matches
            if t.status == "ACTIVE" and t.created_from_snapshot_id == entry_snapshot_id
        ),
        None,
    )
    if exact_active is not None:
        return exact_active
    any_active = next((t for t in matches if t.status == "ACTIVE"), None)
    if any_active is not None:
        return any_active
    exact_snap = next(
        (t for t in matches if t.created_from_snapshot_id == entry_snapshot_id),
        None,
    )
    return exact_snap if exact_snap is not None else matches[0]


def _resolve_single_trade_source(payload: BacktestRunRequest) -> BacktestSingleTradeSource:
    if payload.source is not None:
        return payload.source
    if payload.paper_trade_id is not None:
        return "PAPER_TRADE"
    if payload.legs is not None:
        return "LEGS"
    return "AUTO_BUILD"


def _ensure_snapshot_bar(
    svc: AnalyzerService,
    symbol: str,
    snapshot_id: str,
    bars: list[DailyChainBar],
    *,
    metas_by_id: dict[str, SnapshotMeta],
    protected_ids: set[str],
    start_date: date | None,
    end_date: date | None,
    warnings: list[str],
    field_name: str,
) -> list[DailyChainBar]:
    """Ensure a user-requested `SINGLE_TRADE` snapshot ID is present in `bars`."""
    cleaned_id = _validate_snapshot_id(snapshot_id, field_name=field_name)
    # Verifies existence on disk via SnapshotNotFoundError -> 404 when unknown.
    svc.repo.snapshot_path(symbol, cleaned_id)

    if any(bar.snapshot_id == cleaned_id for bar in bars):
        return bars

    meta = metas_by_id.get(cleaned_id)
    if meta is not None and not _snapshot_in_window(meta, start_date, end_date):
        raise BacktestValidationError(
            f"{field_name} {cleaned_id!r} ({meta.market_date}) falls outside "
            f"[{start_date}, {end_date}]."
        )

    df = svc.repo.load_snapshot(symbol, cleaned_id)
    if meta is not None:
        ts_utc = _parse_utc_ts(meta.snapshot_ts_utc)
        mkt_date = meta.market_date
        verdict = evaluate_snapshot(meta)
        flags: tuple[str, ...] = () if verdict.usable else ("REQUESTED_UNUSABLE_SNAPSHOT",)
        if not verdict.usable:
            warnings.append(
                f"Requested snapshot {cleaned_id} failed strict daily-bar quality gate "
                f"({', '.join(verdict.reasons)}); included explicitly for SINGLE_TRADE replay."
            )
    else:
        raw_ts = str(df["snapshot_ts_utc"][0])
        ts_utc = _parse_utc_ts(raw_ts)
        mkt_date = str(df["market_date"][0])
        flags = ("INDEX_METADATA_MISSING",)

    mkt_day = date.fromisoformat(mkt_date)
    if (start_date is not None and mkt_day < start_date) or (
        end_date is not None and mkt_day > end_date
    ):
        raise BacktestValidationError(
            f"{field_name} {cleaned_id!r} ({mkt_date}) falls outside [{start_date}, {end_date}]."
        )

    same_day = [bar for bar in bars if bar.market_date == mkt_date]
    if same_day:
        tracked_df = same_day[0].tracked_df
        tracked_ids = same_day[0].tracked_snapshot_ids
        filtered = [
            bar
            for bar in bars
            if not (bar.market_date == mkt_date and bar.snapshot_id not in protected_ids)
        ]
    else:
        tracked_by_day = load_tracked_quotes_by_date(
            symbol,
            target_dates=(mkt_day,),
            tracked_dir=svc.tracked_dir,
        )
        tracked_day = tracked_by_day.get(mkt_date)
        tracked_df = tracked_day.df if tracked_day is not None else None
        tracked_ids = tracked_day.snapshot_ids if tracked_day is not None else ()
        filtered = list(bars)

    inserted = DailyChainBar(
        snapshot_id=cleaned_id,
        ts_utc=ts_utc,
        market_date=mkt_date,
        df=df,
        meta=meta,
        selection_flags=flags,
        tracked_df=tracked_df,
        tracked_snapshot_ids=tracked_ids,
    )
    filtered.append(inserted)
    filtered.sort(key=lambda bar: (bar.ts_utc, bar.snapshot_id))
    return filtered


def list_backtest_entries(svc: AnalyzerService, ticker: str) -> BacktestEntriesResponse:
    """List archived chain snapshots for `ticker` with ET/Poland timestamps and quality flags."""
    symbol = svc.validate_ticker(ticker)
    metas = svc.repo.list_snapshots(symbol)
    entries: list[BacktestEntry] = []
    for meta in metas:
        ts_utc = _parse_utc_ts(meta.snapshot_ts_utc)
        verdict = evaluate_snapshot(meta)
        entries.append(
            BacktestEntry(
                snapshot_id=meta.snapshot_id,
                ts_utc=meta.snapshot_ts_utc,
                ts_et=fmt_et(ts_utc),
                ts_poland=fmt_poland(ts_utc),
                contract_count=meta.contract_count,
                expirations=list(meta.expirations),
                quote_coverage=meta.quote_coverage,
                spot_source=meta.spot_source,
                market_data_type=meta.market_data_type,
                usable=verdict.usable,
                quality_reasons=list(verdict.reasons),
            )
        )
    return BacktestEntriesResponse(ticker=symbol, entries=entries)


def _validate_backtest_config(cfg: BacktestConfig) -> None:
    checks: tuple[tuple[str, float | None, float | None], ...] = (
        ("min_entry_credit_dollars", cfg.min_entry_credit_dollars, None),
        ("min_entry_ror_pct", cfg.min_entry_ror_pct, 1000.0),
    )
    for field_name, val, upper in checks:
        if val is None:
            continue
        if not math.isfinite(val) or val < 0.0 or (upper is not None and val > upper):
            bound = f"[0, {upper:g}]" if upper is not None else ">= 0"
            raise BacktestValidationError(f"{field_name} must be finite and {bound} (got {val!r}).")


def run_backtest(svc: AnalyzerService, payload: BacktestRunRequest) -> BacktestRunResponse:
    """Run a `SYSTEMATIC` or `SINGLE_TRADE` historical options backtest over archived snapshots."""
    if (
        payload.start_date is not None
        and payload.end_date is not None
        and payload.start_date > payload.end_date
    ):
        raise BacktestValidationError(
            f"start_date ({payload.start_date.isoformat()}) must be <= "
            f"end_date ({payload.end_date.isoformat()})."
        )
    _validate_backtest_config(payload.config)

    all_trades, _ = svc.paper_trades.list_trades()
    if payload.mode == "SYSTEMATIC":
        return _run_systematic(svc, payload)

    return _run_single_trade(svc, payload, all_trades)


def _run_systematic(svc: AnalyzerService, payload: BacktestRunRequest) -> BacktestRunResponse:
    if not payload.ticker:
        raise BacktestValidationError("ticker is required for SYSTEMATIC backtest mode.")
    symbol = svc.validate_ticker(payload.ticker)
    metas = svc.repo.list_snapshots(symbol)
    history_count = sum(
        1 for m in metas if _snapshot_in_window(m, payload.start_date, payload.end_date)
    )
    bars, warnings = load_daily_chain_bars(
        svc.repo,
        symbol,
        start_date=payload.start_date,
        end_date=payload.end_date,
        tracked_dir=svc.tracked_dir,
    )
    try:
        simulation = run_systematic_backtest(
            bars,
            payload.config,
            ticker=symbol,
            legs=payload.legs,
        )
    except AnalyzerError:
        raise
    except ValueError as exc:
        raise BacktestValidationError(str(exc)) from exc

    return BacktestRunResponse(
        mode="SYSTEMATIC",
        source=None,
        paper_trade_id=None,
        simulation=simulation,
        matching_paper_trade=None,
        history_snapshot_count=history_count,
        daily_bar_count=len(bars),
        warnings=warnings,
    )


def _run_single_trade(
    svc: AnalyzerService,
    payload: BacktestRunRequest,
    all_trades: Sequence[PaperTrade],
) -> BacktestRunResponse:
    source = _resolve_single_trade_source(payload)
    cfg: BacktestConfig = payload.config
    eff_start_date = payload.start_date
    eff_legs: list[StrategyLegInput] | None = None
    entry_snapshot_id = payload.entry_snapshot_id
    paper_trade: PaperTrade | None = None

    if source == "PAPER_TRADE":
        if not payload.paper_trade_id:
            raise BacktestValidationError("paper_trade_id is required when source='PAPER_TRADE'.")
        cleaned_trade_id = payload.paper_trade_id.strip()
        if PAPER_TRADE_ID_RE.fullmatch(cleaned_trade_id) is None:
            raise InvalidRequestError(f"Invalid paper_trade_id {payload.paper_trade_id!r}")
        paper_trade = next((t for t in all_trades if t.id == cleaned_trade_id), None)
        if paper_trade is None:
            raise SnapshotNotFoundError(f"Paper trade {cleaned_trade_id!r} not found.")
        if payload.ticker:
            symbol = svc.validate_ticker(payload.ticker)
            if symbol != paper_trade.ticker:
                raise BacktestValidationError(
                    f"Paper trade {paper_trade.id} belongs to {paper_trade.ticker}, not {symbol}."
                )
        else:
            symbol = svc.validate_ticker(paper_trade.ticker)
        entry_snapshot_id = entry_snapshot_id or paper_trade.created_from_snapshot_id
        eff_legs = [
            StrategyLegInput(
                side=leg.side,
                right=leg.right,
                expiry=leg.expiry,
                strike=leg.strike,
                qty=1,
            )
            for leg in paper_trade.legs
        ]
        if eff_start_date is None:
            eff_start_date = date.fromisoformat(paper_trade.market_date)
        eff_qty = cfg.qty if "qty" in cfg.model_fields_set else paper_trade.qty
        try:
            cfg = BacktestConfig.model_validate(
                {
                    **cfg.model_dump(),
                    "strategy_kind": paper_trade.strategy_kind,
                    "qty": eff_qty,
                }
            )
        except ValueError as exc:
            raise BacktestValidationError(str(exc)) from exc
    else:
        if not payload.ticker:
            raise BacktestValidationError(f"ticker is required when source={source!r}.")
        symbol = svc.validate_ticker(payload.ticker)
        if not entry_snapshot_id:
            raise BacktestValidationError("entry_snapshot_id is required for SINGLE_TRADE mode.")
        if source == "LEGS":
            if not payload.legs:
                raise BacktestValidationError("legs are required when source='LEGS'.")
            eff_legs = list(payload.legs)
        elif payload.legs is not None:
            raise BacktestValidationError("legs must not be supplied when source='AUTO_BUILD'.")

    metas = svc.repo.list_snapshots(symbol)
    metas_by_id = {meta.snapshot_id: meta for meta in metas}
    history_count = sum(
        1 for m in metas if _snapshot_in_window(m, eff_start_date, payload.end_date)
    )
    bars, warnings = load_daily_chain_bars(
        svc.repo,
        symbol,
        start_date=eff_start_date,
        end_date=payload.end_date,
        tracked_dir=svc.tracked_dir,
    )

    assert entry_snapshot_id is not None
    protected_ids: set[str] = {entry_snapshot_id.strip()}
    if payload.end_snapshot_id is not None:
        protected_ids.add(payload.end_snapshot_id.strip())

    bars = _ensure_snapshot_bar(
        svc,
        symbol,
        entry_snapshot_id,
        bars,
        metas_by_id=metas_by_id,
        protected_ids=protected_ids,
        start_date=eff_start_date,
        end_date=payload.end_date,
        warnings=warnings,
        field_name="entry_snapshot_id",
    )
    end_snapshot_id = payload.end_snapshot_id
    if end_snapshot_id is not None:
        bars = _ensure_snapshot_bar(
            svc,
            symbol,
            end_snapshot_id,
            bars,
            metas_by_id=metas_by_id,
            protected_ids=protected_ids,
            start_date=eff_start_date,
            end_date=payload.end_date,
            warnings=warnings,
            field_name="end_snapshot_id",
        )
        end_snapshot_id = end_snapshot_id.strip()

    try:
        simulation: BacktestSimulationResult = run_single_trade_replay(
            bars,
            cfg,
            ticker=symbol,
            entry_snapshot_id=entry_snapshot_id.strip(),
            legs=eff_legs,
            end_snapshot_id=end_snapshot_id,
        )
    except AnalyzerError:
        raise
    except ValueError as exc:
        raise BacktestValidationError(str(exc)) from exc

    matching_trade = paper_trade
    if matching_trade is None:
        candidate_legs: Sequence[StrategyLegInput | EvaluatedLeg] | None = eff_legs
        if candidate_legs is None and simulation.trades:
            candidate_legs = simulation.trades[0].legs
        if candidate_legs:
            matching_trade = _find_matching_paper_trade(
                all_trades,
                symbol,
                candidate_legs,
                entry_snapshot_id.strip(),
            )

    return BacktestRunResponse(
        mode="SINGLE_TRADE",
        source=source,
        paper_trade_id=paper_trade.id if paper_trade is not None else None,
        simulation=simulation,
        matching_paper_trade=matching_trade,
        history_snapshot_count=history_count,
        daily_bar_count=len(bars),
        warnings=warnings,
    )
