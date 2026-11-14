"""Historical backtest orchestration service: candidate entries and simulation runner."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import polars as pl

from src.analytics.backtest import (
    BacktestConfig,
    BacktestSimulationResult,
    BrokerFillAnchor,
    resolve_broker_leg_prices,
    run_single_trade_replay,
    run_systematic_backtest,
)
from src.analytics.ledger import LedgerOpenPositionView
from src.analytics.strategies import EvaluatedLeg, StrategyLegInput
from src.core.calendar import US_EASTERN, et_date, fmt_et
from src.core.exceptions import (
    AnalyzerError,
    BacktestValidationError,
    InvalidRequestError,
    SnapshotNotFoundError,
    StorageSchemaError,
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
from src.storage.schema import (
    OPTION_CHAIN_POLARS_SCHEMA,
    PRICE_SENTINEL_MAX,
    SNAPSHOT_ID_RE,
    SnapshotMeta,
    SpotSource,
)

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


def _to_iso_expiry(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if re.fullmatch(r"\d{8}", cleaned):
        return f"{cleaned[:4]}-{cleaned[4:6]}-{cleaned[6:8]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", cleaned):
        return cleaned
    return None


def _clean_positive(val: object) -> float | None:
    if val is None:
        return None
    try:
        num = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) and 0.0 < num < PRICE_SENTINEL_MAX else None


def _resolve_synthetic_leg_marks(
    paper_trade: PaperTrade,
    open_positions: Sequence[LedgerOpenPositionView],
    *,
    eff_legs: Sequence[StrategyLegInput],
    eff_qty: int,
    scaled_entry_net_cash: float,
) -> list[float | None]:
    camp_by_key: dict[tuple[str, float, str], LedgerOpenPositionView] = {}
    camp_by_conid: dict[int, LedgerOpenPositionView] = {}
    any_by_key: dict[tuple[str, float, str], LedgerOpenPositionView] = {}
    any_by_conid: dict[int, LedgerOpenPositionView] = {}
    cid = paper_trade.ledger_campaign_id
    for op in open_positions:
        if op.asset_class != "OPT" or op.underlying != paper_trade.ticker:
            continue
        iso_exp = _to_iso_expiry(op.expiry)
        if iso_exp is None or op.strike is None or op.right not in ("C", "P"):
            continue
        key = (iso_exp, round(float(op.strike), 4), op.right)
        any_by_key.setdefault(key, op)
        if op.conid is not None and op.conid > 0:
            any_by_conid.setdefault(int(op.conid), op)
        if cid and (op.campaign_id == cid or op.position_key == cid):
            camp_by_key.setdefault(key, op)
            if op.conid is not None and op.conid > 0:
                camp_by_conid.setdefault(int(op.conid), op)

    fallback_entry = resolve_broker_leg_prices(
        eff_legs,
        eff_qty,
        scaled_entry_net_cash,
        [leg.entry_price for leg in paper_trade.legs],
    )
    marks: list[float | None] = []
    for leg, fb_px in zip(paper_trade.legs, fallback_entry, strict=True):
        key = (leg.expiry, round(float(leg.strike), 4), leg.right)
        matched: LedgerOpenPositionView | None = None
        if leg.con_id > 0:
            matched = camp_by_conid.get(leg.con_id)
        if matched is None:
            matched = camp_by_key.get(key)
        if matched is None and leg.con_id > 0:
            matched = any_by_conid.get(leg.con_id)
        if matched is None:
            matched = any_by_key.get(key)

        px: float | None = None
        if matched is not None:
            px = _clean_positive(matched.mark_price)
        if px is None:
            px = _clean_positive(leg.entry_price)
        if px is None:
            px = _clean_positive(fb_px)
        marks.append(round(px, 4) if px is not None else None)
    return marks


def _resolve_synthetic_spot(
    svc: AnalyzerService,
    symbol: str,
    paper_trade: PaperTrade,
    metas: Sequence[SnapshotMeta],
) -> tuple[float, SpotSource]:
    for meta in metas:
        cand = _clean_positive(meta.underlying_price)
        if cand is not None:
            src: SpotSource = meta.spot_source if meta.spot_source in ("LAST", "MID") else "MID"
            return cand, src
    try:
        latest_df = svc.repo.load_latest(symbol)
        if latest_df.height > 0:
            cand = _clean_positive(latest_df["underlying_price"][0])
            if cand is not None:
                return cand, "MID"
    except (SnapshotNotFoundError, StorageSchemaError):
        pass
    mean_strike = sum(float(leg.strike) for leg in paper_trade.legs) / float(len(paper_trade.legs))
    return mean_strike, "MID"


def _build_synthetic_ledger_mtm_bar(
    svc: AnalyzerService,
    symbol: str,
    paper_trade: PaperTrade,
    *,
    eff_legs: Sequence[StrategyLegInput],
    eff_qty: int,
    scaled_entry_net_cash: float,
    metas: Sequence[SnapshotMeta],
    end_date: date | None,
) -> tuple[DailyChainBar, str]:
    now_utc = svc.paper_trades._now_utc()
    today_et = et_date(now_utc)
    entry_day = date.fromisoformat(paper_trade.market_date)
    anchor_ts = _parse_utc_ts(paper_trade.created_utc)
    upper_day = (
        min(today_et, end_date) if (end_date is not None and end_date >= entry_day) else today_et
    )
    mtm_day = max(entry_day, upper_day)
    mtm_date_str = mtm_day.isoformat()
    if mtm_day == today_et:
        ts_utc = max(now_utc, anchor_ts)
    else:
        ts_utc = max(
            datetime.combine(mtm_day, time(16, 0), tzinfo=US_EASTERN).astimezone(UTC),
            anchor_ts,
        )
    ts_utc_str = ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    snapshot_id = f"{symbol}_{mtm_date_str}_LEDGER_MTM"

    spot, spot_source = _resolve_synthetic_spot(svc, symbol, paper_trade, metas)
    try:
        open_positions = svc.ledger.build_full_open_report().open_positions
    except Exception:
        open_positions = []
    leg_marks = _resolve_synthetic_leg_marks(
        paper_trade,
        open_positions,
        eff_legs=eff_legs,
        eff_qty=eff_qty,
        scaled_entry_net_cash=scaled_entry_net_cash,
    )

    und_conid = paper_trade.underlying_conid if paper_trade.underlying_conid > 0 else 1
    syn_rows: list[dict[str, object]] = []
    seen_contracts: set[tuple[str, float, str]] = set()
    for idx, (leg, px) in enumerate(zip(paper_trade.legs, leg_marks, strict=True), start=1):
        c_key = (leg.expiry, round(float(leg.strike), 4), leg.right)
        if c_key in seen_contracts:
            continue
        seen_contracts.add(c_key)
        dte = max(0, (date.fromisoformat(leg.expiry) - mtm_day).days)
        syn_rows.append(
            {
                "snapshot_id": snapshot_id,
                "snapshot_ts_utc": ts_utc_str,
                "market_date": mtm_date_str,
                "market_data_type": 3,
                "underlying": symbol,
                "underlying_conid": und_conid,
                "underlying_price": spot,
                "option_conid": leg.con_id if leg.con_id > 0 else idx,
                "expiry": leg.expiry,
                "dte": dte,
                "strike": float(leg.strike),
                "moneyness_pct": round(((float(leg.strike) - spot) / spot) * 100.0, 4),
                "right": leg.right,
                "bid": px,
                "ask": px,
                "mid": px,
                "last": px,
                "close": px,
                "mark_price": px,
                "volume": None,
                "open_interest": None,
                "iv": None,
                "delta": None,
                "gamma": None,
                "theta": None,
                "vega": None,
                "greeks_source": "NONE",
            }
        )

    syn_df = pl.DataFrame(syn_rows, schema=OPTION_CHAIN_POLARS_SCHEMA)
    quoted_count = sum(1 for row in syn_rows if row["mark_price"] is not None)
    coverage = (quoted_count / syn_df.height) if syn_df.height > 0 else 0.0
    meta = SnapshotMeta(
        snapshot_id=snapshot_id,
        file_path="",
        snapshot_ts_utc=ts_utc_str,
        market_date=mtm_date_str,
        market_data_type=3,
        underlying_price=spot,
        contract_count=syn_df.height,
        expirations=sorted({leg.expiry for leg in paper_trade.legs}),
        spot_source=spot_source,
        quoted_contract_count=quoted_count,
        quote_coverage=coverage,
        prior_close_only_count=0,
    )
    bar = DailyChainBar(
        snapshot_id=snapshot_id,
        ts_utc=ts_utc,
        market_date=mtm_date_str,
        df=syn_df,
        meta=meta,
        selection_flags=("SYNTHETIC_LEDGER_MTM",),
    )
    warning = (
        f"No local chain snapshots existed for {symbol} on or after {paper_trade.market_date}; "
        f"replay uses synthetic MTM bar {snapshot_id} from current Ledger mark / entry price."
    )
    return bar, warning


def _run_single_trade(
    svc: AnalyzerService,
    payload: BacktestRunRequest,
    all_trades: Sequence[PaperTrade],
) -> BacktestRunResponse:
    source = _resolve_single_trade_source(payload)
    cfg: BacktestConfig = payload.config
    eff_start_date = payload.start_date
    eff_legs: list[StrategyLegInput] | None = None
    eff_qty: int = cfg.qty
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
                qty=leg.qty_ratio,
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

    if (
        payload.end_date is not None
        and eff_start_date is not None
        and eff_start_date > payload.end_date
    ):
        raise BacktestValidationError(
            f"start_date ({eff_start_date.isoformat()}) must be <= "
            f"end_date ({payload.end_date.isoformat()})."
        )

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

    use_broker_anchor = paper_trade is not None and (
        paper_trade.source == "LEDGER"
        or paper_trade.created_from_snapshot_id is None
        or any(leg.entry_price is not None for leg in paper_trade.legs)
    )

    end_snapshot_id = payload.end_snapshot_id.strip() if payload.end_snapshot_id else None
    if use_broker_anchor:
        assert paper_trade is not None
        assert eff_legs is not None
        if payload.end_date is not None and payload.end_date < date.fromisoformat(
            paper_trade.market_date
        ):
            raise BacktestValidationError(
                f"end_date ({payload.end_date.isoformat()}) precedes trade entry date "
                f"({paper_trade.market_date})."
            )
        protected_ids: set[str] = set()
        cleaned_entry_id = entry_snapshot_id.strip() if entry_snapshot_id else None
        ensure_entry_id: str | None = None
        if (
            cleaned_entry_id is not None
            and cleaned_entry_id in metas_by_id
            and (
                payload.entry_snapshot_id is not None
                or _snapshot_in_window(
                    metas_by_id[cleaned_entry_id], eff_start_date, payload.end_date
                )
            )
        ):
            ensure_entry_id = cleaned_entry_id
            protected_ids.add(cleaned_entry_id)
        if end_snapshot_id is not None:
            protected_ids.add(end_snapshot_id)

        if ensure_entry_id is not None:
            bars = _ensure_snapshot_bar(
                svc,
                symbol,
                ensure_entry_id,
                bars,
                metas_by_id=metas_by_id,
                protected_ids=protected_ids,
                start_date=eff_start_date,
                end_date=payload.end_date,
                warnings=warnings,
                field_name="entry_snapshot_id",
            )
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
        scaled_entry_net_cash = (
            round(paper_trade.entry_net_cash * (float(eff_qty) / float(paper_trade.qty)), 2)
            if (paper_trade.qty > 0 and eff_qty != paper_trade.qty)
            else round(paper_trade.entry_net_cash, 2)
        ) or 0.0
        if not bars:
            syn_bar, syn_warning = _build_synthetic_ledger_mtm_bar(
                svc,
                symbol,
                paper_trade,
                eff_legs=eff_legs,
                eff_qty=eff_qty,
                scaled_entry_net_cash=scaled_entry_net_cash,
                metas=metas,
                end_date=payload.end_date,
            )
            bars = [syn_bar]
            warnings.append(syn_warning)

        anchor = BrokerFillAnchor(
            entry_date=paper_trade.market_date,
            entry_ts_utc=paper_trade.created_utc,
            entry_snapshot_id=f"{symbol}_{paper_trade.market_date.replace('-', '')}_LEDGER",
            entry_net_cash_dollars=scaled_entry_net_cash,
            leg_entry_prices=[leg.entry_price for leg in paper_trade.legs],
        )
        try:
            simulation: BacktestSimulationResult = run_single_trade_replay(
                bars,
                cfg,
                ticker=symbol,
                entry_snapshot_id=None,
                legs=eff_legs,
                end_snapshot_id=end_snapshot_id,
                broker_fill_anchor=anchor,
                trade_id=paper_trade.id,
            )
        except AnalyzerError:
            raise
        except ValueError as exc:
            raise BacktestValidationError(str(exc)) from exc
    else:
        assert entry_snapshot_id is not None
        protected_ids = {entry_snapshot_id.strip()}
        if end_snapshot_id is not None:
            protected_ids.add(end_snapshot_id)

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

        try:
            simulation = run_single_trade_replay(
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
                entry_snapshot_id.strip() if entry_snapshot_id else None,
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
