"""Main Trade Ledger analytics engine (Polars -> campaigns, breakdowns, chart & MTM)."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import polars as pl

from src.analytics.ledger.classifier import (
    AggregatedLeg,
    NormalizedExecution,
    NormalizedPosition,
    classify_option_legs,
    format_expiry_short,
)
from src.analytics.ledger.models import (
    LedgerAnalyticsReport,
    LedgerBreakdownRow,
    LedgerCampaignRow,
    LedgerChartPoint,
    LedgerExecutionView,
    LedgerGroupBy,
    LedgerKPIs,
    LedgerOpenPositionView,
    LedgerStrategyType,
    LedgerView,
    LedgerWindow,
    MarkSource,
)
from src.analytics.ledger.rolls import (
    CampaignRecord,
    OptLot,
    StkLot,
    StkRealization,
    build_underlying_ledger,
    resolve_campaign_status,
)
from src.storage.ledger_schema import (
    LedgerAssetClass,
    LedgerEventCode,
    LedgerOpenClose,
    LedgerSide,
    normalize_expiry_yyyymmdd,
    normalize_right_optional,
    normalize_underlying,
    normalize_utc_timestamp,
)

_EPS = 1e-6

_STRATEGY_LABELS: dict[LedgerStrategyType, str] = {
    "BULL_PUT_SPREAD": "Bull Put Spread",
    "BEAR_CALL_SPREAD": "Bear Call Spread",
    "BULL_CALL_SPREAD": "Bull Call Spread",
    "BEAR_PUT_SPREAD": "Bear Put Spread",
    "IRON_CONDOR": "Iron Condor",
    "IRON_BUTTERFLY": "Iron Butterfly",
    "DIAGONAL_SPREAD": "Diagonal Spread",
    "CALENDAR_SPREAD": "Calendar Spread",
    "STRANGLE": "Strangle",
    "STRADDLE": "Straddle",
    "COVERED_CALL": "Covered Call",
    "NAKED_CALL": "Naked Call",
    "SHORT_PUT": "Short Put",
    "LONG_CALL": "Long Call",
    "LONG_PUT": "Long Put",
    "CUSTOM_COMBO": "Custom Combo",
    "STOCK_LOT": "Stock Lot",
}


def compute_window_start(window: LedgerWindow, as_of: datetime) -> datetime | None:
    """Compute inclusive UTC lower bound for `window`, or `None` for `ALL`."""
    as_of_utc = as_of.replace(tzinfo=UTC) if as_of.tzinfo is None else as_of.astimezone(UTC)
    if window == "1W":
        return as_of_utc - timedelta(days=7)
    if window == "1M":
        return as_of_utc - timedelta(days=30)
    if window == "3M":
        return as_of_utc - timedelta(days=90)
    if window == "YTD":
        return datetime(as_of_utc.year, 1, 1, 0, 0, 0, tzinfo=UTC)
    if window == "1Y":
        return as_of_utc - timedelta(days=365)
    return None


def _parse_utc_dt(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=UTC) if raw.tzinfo is None else raw.astimezone(UTC)
    text = str(raw).strip()
    if text.endswith("Z") and "T" in text and len(text) >= 20:
        try:
            return datetime.fromisoformat(text[:-1] + "+00:00").astimezone(UTC)
        except ValueError:
            pass
    iso = normalize_utc_timestamp(text)
    return datetime.fromisoformat(iso[:-1] + "+00:00").astimezone(UTC)


def _finite_float(value: Any, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    num = float(value)
    return num if math.isfinite(num) else default


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        num = int(value)
    else:
        num = int(value)
    return num if num > 0 else None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _extract_executions(
    executions_df: pl.DataFrame, as_of_utc: datetime
) -> list[NormalizedExecution]:
    if executions_df.is_empty():
        return []

    sorted_df = executions_df.sort(["executed_at", "exec_id"])
    rows: list[NormalizedExecution] = []
    for idx, row in enumerate(sorted_df.iter_rows(named=True)):
        dt = _parse_utc_dt(row["executed_at"])
        if dt > as_of_utc:
            continue
        underlying = normalize_underlying(str(row["underlying"]))
        ac_raw = str(row["asset_class"]).strip().upper()
        asset_class: LedgerAssetClass = "STK" if ac_raw == "STK" else "OPT"
        side_raw = str(row["side"]).strip().upper()
        side: LedgerSide = "BOT" if side_raw in ("BOT", "BUY") else "SLD"
        qty = _finite_float(row.get("quantity"), default=0.0) or 0.0
        if qty <= _EPS:
            continue
        price = max(0.0, _finite_float(row.get("price"), default=0.0) or 0.0)
        mult_raw = row.get("multiplier")
        mult = int(mult_raw) if mult_raw is not None else (1 if asset_class == "STK" else 100)
        if mult <= 0:
            mult = 1 if asset_class == "STK" else 100
        fx = _finite_float(row.get("fx_rate_to_base"), default=1.0)
        fx_rate = fx if fx is not None and fx > 0.0 else 1.0
        proceeds_raw = _finite_float(row.get("proceeds"))
        if proceeds_raw is None:
            proceeds_raw = (-qty * price * mult) if side == "BOT" else (qty * price * mult)
        commission_raw = abs(_finite_float(row.get("commission"), default=0.0) or 0.0)
        realized_ib = _finite_float(row.get("realized_pnl_ib"))
        oc_raw = _opt_str(row.get("open_close"))
        open_close: LedgerOpenClose | None = (
            "O" if oc_raw in ("O", "OPEN") else ("C" if oc_raw in ("C", "CLOSE") else None)
        )
        ev_raw = str(row.get("event_code") or "TRADE").strip().upper()
        event_code: LedgerEventCode = (
            ev_raw  # type: ignore[assignment]
            if ev_raw in ("TRADE", "EXPIRED", "ASSIGNED", "EXERCISED")
            else "TRADE"
        )
        expiry = normalize_expiry_yyyymmdd(row.get("expiry")) if asset_class == "OPT" else None
        strike = _finite_float(row.get("strike")) if asset_class == "OPT" else None
        right = normalize_right_optional(row.get("right")) if asset_class == "OPT" else None
        if asset_class == "OPT" and (expiry is None or strike is None or right is None):
            continue

        rows.append(
            NormalizedExecution(
                exec_id=str(row["exec_id"]),
                ib_order_id=_opt_str(row.get("ib_order_id")),
                conid=_opt_int(row.get("conid")),
                underlying_conid=_opt_int(row.get("underlying_conid")),
                symbol=str(row.get("symbol") or underlying),
                underlying=underlying,
                asset_class=asset_class,
                expiry=expiry,
                strike=strike,
                right=right,
                multiplier=mult,
                side=side,
                quantity=qty,
                price=price,
                proceeds_ex_fee=proceeds_raw * fx_rate,
                commission_base=commission_raw * fx_rate,
                realized_pnl_ib=(realized_ib * fx_rate) if realized_ib is not None else None,
                open_close=open_close,
                event_code=event_code,
                executed_at=dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                executed_at_dt=dt,
                source=str(row.get("source") or "FLEX_IMPORT"),
                seq_index=idx,
            )
        )
    return rows


def _extract_positions(positions_df: pl.DataFrame) -> list[NormalizedPosition]:
    if positions_df.is_empty():
        return []

    rows: list[NormalizedPosition] = []
    for row in positions_df.iter_rows(named=True):
        pos_qty = _finite_float(row.get("position"), default=0.0) or 0.0
        if abs(pos_qty) <= _EPS:
            continue
        underlying = normalize_underlying(str(row["underlying"]))
        ac_raw = str(row["asset_class"]).strip().upper()
        asset_class: LedgerAssetClass = "STK" if ac_raw == "STK" else "OPT"
        mult_raw = row.get("multiplier")
        mult = int(mult_raw) if mult_raw is not None else (1 if asset_class == "STK" else 100)
        if mult <= 0:
            mult = 1 if asset_class == "STK" else 100
        expiry = normalize_expiry_yyyymmdd(row.get("expiry")) if asset_class == "OPT" else None
        strike = _finite_float(row.get("strike")) if asset_class == "OPT" else None
        right = normalize_right_optional(row.get("right")) if asset_class == "OPT" else None
        if asset_class == "OPT" and (expiry is None or strike is None or right is None):
            continue
        mark_raw = _finite_float(row.get("mark_price"))
        mark_price = mark_raw if mark_raw is not None and mark_raw >= 0.0 else None

        rows.append(
            NormalizedPosition(
                conid=_opt_int(row.get("conid")),
                symbol=str(row.get("symbol") or underlying),
                underlying=underlying,
                asset_class=asset_class,
                expiry=expiry,
                strike=strike,
                right=right,
                multiplier=mult,
                position=pos_qty,
                avg_cost=abs(_finite_float(row.get("avg_cost"), default=0.0) or 0.0),
                mark_price=mark_price,
                market_value=_finite_float(row.get("market_value")),
                unrealized_pnl=_finite_float(row.get("unrealized_pnl")),
                source=str(row.get("source") or "FLEX_IMPORT"),
                updated_at=str(row.get("updated_at") or ""),
            )
        )
    return rows


def _opt_pos_key(
    underlying: str, expiry: str, strike: float, right: Literal["C", "P"]
) -> tuple[str, str, str, float, Literal["C", "P"]]:
    return (underlying, "OPT", expiry, round(strike, 6), right)


def _stk_pos_key(underlying: str) -> tuple[str, str, None, None, None]:
    return (underlying, "STK", None, None, None)


def _resolve_mark(
    conid: int | None,
    pos_row: NormalizedPosition | None,
    live_marks: dict[int, float] | None,
) -> tuple[float | None, MarkSource]:
    if live_marks is not None and conid is not None and conid in live_marks:
        live_val = _finite_float(live_marks[conid])
        if live_val is not None and live_val >= 0.0:
            return live_val, "LIVE_GATEWAY"
    if pos_row is not None and pos_row.mark_price is not None and pos_row.mark_price >= 0.0:
        return pos_row.mark_price, "FLEX_MARK"
    return None, "NONE"


def _build_open_positions(
    *,
    open_opt_lots_all: list[OptLot],
    open_stk_lots_all: list[StkLot],
    positions_list: list[NormalizedPosition],
    campaigns_by_id: dict[str, CampaignRecord],
    campaign_open_contracts_map: dict[str, float],
    include_stock_gain: bool,
    live_marks: dict[int, float] | None,
) -> list[LedgerOpenPositionView]:
    pos_opt_map: dict[tuple[str, str, str, float, Literal["C", "P"]], NormalizedPosition] = {}
    pos_stk_map: dict[tuple[str, str, None, None, None], NormalizedPosition] = {}
    for pos in positions_list:
        if pos.asset_class == "OPT":
            assert pos.expiry is not None and pos.strike is not None and pos.right is not None
            pos_opt_map[_opt_pos_key(pos.underlying, pos.expiry, pos.strike, pos.right)] = pos
        else:
            pos_stk_map[_stk_pos_key(pos.underlying)] = pos

    # Identify active covered-call campaign per underlying for linking covered stock shares.
    active_cc_by_underlying: dict[str, str] = {}
    for camp in campaigns_by_id.values():
        if (
            camp.strategy_type == "COVERED_CALL"
            and campaign_open_contracts_map.get(camp.campaign_id, 0.0) > _EPS
            and camp.underlying not in active_cc_by_underlying
        ):
            active_cc_by_underlying[camp.underlying] = camp.campaign_id

    open_views: list[LedgerOpenPositionView] = []
    matched_opt_keys: set[tuple[str, str, str, float, Literal["C", "P"]]] = set()
    matched_stk_keys: set[tuple[str, str, None, None, None]] = set()

    # 1. Option open FIFO lots grouped by contract.
    opt_groups: dict[tuple[str, str, str, float, Literal["C", "P"]], list[OptLot]] = defaultdict(
        list
    )
    for lot in open_opt_lots_all:
        opt_groups[_opt_pos_key(lot.underlying, lot.expiry, lot.strike, lot.right)].append(lot)

    for key, lots in sorted(opt_groups.items()):
        net_qty = sum(
            lot.remaining_qty if lot.direction == "LONG" else -lot.remaining_qty for lot in lots
        )
        if abs(net_qty) <= _EPS:
            continue
        matched_opt_keys.add(key)
        pos_row = pos_opt_map.get(key)
        first = lots[0]
        mult = first.multiplier
        conid = (pos_row.conid if pos_row is not None else None) or next(
            (lot.conid for lot in lots if lot.conid is not None), None
        )
        # Cost basis uses position sign: negative for short liability, positive for long asset.
        cost_basis = -sum(
            lot.open_cashflow_total * (lot.remaining_qty / lot.initial_qty)
            for lot in lots
            if lot.initial_qty > _EPS
        )
        avg_cost_price = abs(cost_basis) / (abs(net_qty) * mult) if mult > 0 else 0.0
        fees = sum(lot.open_fee_remaining for lot in lots)

        camp_ids = {lot.campaign_id for lot in lots if lot.campaign_id}
        campaign_id = (
            next(iter(camp_ids))
            if len(camp_ids) == 1
            else (next(iter(sorted(camp_ids))) if camp_ids else None)
        )
        if campaign_id is not None and campaign_id in campaigns_by_id:
            strategy_type = campaigns_by_id[campaign_id].strategy_type
        else:
            stk_anchor = pos_stk_map.get(_stk_pos_key(first.underlying))
            avail_shares = stk_anchor.position if stk_anchor is not None else 0.0
            strategy_type = classify_option_legs(
                [
                    AggregatedLeg(
                        underlying=first.underlying,
                        expiry=first.expiry,
                        strike=first.strike,
                        right=first.right,
                        side="BOT" if net_qty > 0 else "SLD",
                        quantity=abs(net_qty),
                        price=avg_cost_price,
                        cashflow_ex_fee=-cost_basis,
                        fees=fees,
                        multiplier=mult,
                        conid=conid,
                        symbol=first.symbol,
                    )
                ],
                available_stk_shares=avail_shares,
            )

        mark_price, mark_source = _resolve_mark(conid, pos_row, live_marks)
        market_value = (net_qty * mark_price * mult) if mark_price is not None else None
        unrealized = (market_value - cost_basis) if market_value is not None else None
        pos_key = f"OPT:{first.underlying}:{first.expiry}:{first.strike:g}:{first.right}"

        open_views.append(
            LedgerOpenPositionView(
                position_key=pos_key,
                underlying=first.underlying,
                asset_class="OPT",
                strategy_type=strategy_type,
                symbol=pos_row.symbol if pos_row is not None else first.symbol,
                expiry=first.expiry,
                strike=first.strike,
                right=first.right,
                quantity=net_qty,
                avg_cost_price=avg_cost_price,
                mark_price=mark_price,
                mark_source=mark_source,
                cost_basis_ex_fee=cost_basis,
                market_value=market_value,
                unrealized_pnl_ex_fee=unrealized,
                fees=fees,
                campaign_id=campaign_id,
                conid=conid,
                opened_at=min(lot.opened_at for lot in lots),
            )
        )

    # 2. Unmatched open OPT rows from `positions_df` (opened before execution history).
    for key, pos_row in sorted(pos_opt_map.items()):
        if key in matched_opt_keys:
            continue
        assert (
            pos_row.expiry is not None and pos_row.strike is not None and pos_row.right is not None
        )
        stk_anchor = pos_stk_map.get(_stk_pos_key(pos_row.underlying))
        avail_shares = stk_anchor.position if stk_anchor is not None else 0.0
        strategy_type = classify_option_legs(
            [
                AggregatedLeg(
                    underlying=pos_row.underlying,
                    expiry=pos_row.expiry,
                    strike=pos_row.strike,
                    right=pos_row.right,
                    side="BOT" if pos_row.position > 0 else "SLD",
                    quantity=abs(pos_row.position),
                    price=pos_row.avg_cost,
                    cashflow_ex_fee=-pos_row.position * pos_row.avg_cost * pos_row.multiplier,
                    fees=0.0,
                    multiplier=pos_row.multiplier,
                    conid=pos_row.conid,
                    symbol=pos_row.symbol,
                )
            ],
            available_stk_shares=avail_shares,
        )
        cost_basis = pos_row.position * pos_row.avg_cost * pos_row.multiplier
        mark_price, mark_source = _resolve_mark(pos_row.conid, pos_row, live_marks)
        market_value = (
            (pos_row.position * mark_price * pos_row.multiplier) if mark_price is not None else None
        )
        unrealized = (market_value - cost_basis) if market_value is not None else None
        pos_key = f"OPT:{pos_row.underlying}:{pos_row.expiry}:{pos_row.strike:g}:{pos_row.right}"
        open_views.append(
            LedgerOpenPositionView(
                position_key=pos_key,
                underlying=pos_row.underlying,
                asset_class="OPT",
                strategy_type=strategy_type,
                symbol=pos_row.symbol,
                expiry=pos_row.expiry,
                strike=pos_row.strike,
                right=pos_row.right,
                quantity=pos_row.position,
                avg_cost_price=pos_row.avg_cost,
                mark_price=mark_price,
                mark_source=mark_source,
                cost_basis_ex_fee=cost_basis,
                market_value=market_value,
                unrealized_pnl_ex_fee=unrealized,
                fees=0.0,
                campaign_id=None,
                conid=pos_row.conid,
                opened_at=_opt_str(pos_row.updated_at),
            )
        )

    # 3. Covered / open stock positions when `include_stock_gain=True`.
    if include_stock_gain:
        stk_groups: dict[tuple[str, str, None, None, None], list[StkLot]] = defaultdict(list)
        for stk_lot in open_stk_lots_all:
            stk_groups[_stk_pos_key(stk_lot.underlying)].append(stk_lot)

        for stk_key, stk_lots in sorted(stk_groups.items()):
            net_shares = sum(
                sl.remaining_qty if sl.direction == "LONG" else -sl.remaining_qty for sl in stk_lots
            )
            if abs(net_shares) <= _EPS:
                continue
            matched_stk_keys.add(stk_key)
            first_stk = stk_lots[0]
            pos_row = pos_stk_map.get(stk_key)
            conid = (pos_row.conid if pos_row is not None else None) or next(
                (sl.conid for sl in stk_lots if sl.conid is not None), None
            )
            cost_basis = -sum(
                sl.open_cashflow_total * (sl.remaining_qty / sl.initial_qty)
                for sl in stk_lots
                if sl.initial_qty > _EPS
            )
            avg_cost_price = abs(cost_basis) / abs(net_shares)
            fees = sum(sl.open_fee_remaining for sl in stk_lots)
            cc_cid = active_cc_by_underlying.get(first_stk.underlying)
            stk_strategy: LedgerStrategyType = (
                "COVERED_CALL"
                if (cc_cid is not None and net_shares >= 100.0 - _EPS)
                else "STOCK_LOT"
            )
            mark_price, mark_source = _resolve_mark(conid, pos_row, live_marks)
            market_value = (net_shares * mark_price) if mark_price is not None else None
            unrealized = (market_value - cost_basis) if market_value is not None else None

            open_views.append(
                LedgerOpenPositionView(
                    position_key=f"STK:{first_stk.underlying}",
                    underlying=first_stk.underlying,
                    asset_class="STK",
                    strategy_type=stk_strategy,
                    symbol=pos_row.symbol if pos_row is not None else first_stk.symbol,
                    expiry=None,
                    strike=None,
                    right=None,
                    quantity=net_shares,
                    avg_cost_price=avg_cost_price,
                    mark_price=mark_price,
                    mark_source=mark_source,
                    cost_basis_ex_fee=cost_basis,
                    market_value=market_value,
                    unrealized_pnl_ex_fee=unrealized,
                    fees=fees,
                    campaign_id=cc_cid if stk_strategy == "COVERED_CALL" else None,
                    conid=conid,
                    opened_at=min(sl.executed_at for sl in stk_lots),
                )
            )

        for stk_key, pos_row in sorted(pos_stk_map.items()):
            if stk_key in matched_stk_keys:
                continue
            cc_cid = active_cc_by_underlying.get(pos_row.underlying)
            pos_stk_strategy: LedgerStrategyType = (
                "COVERED_CALL"
                if (cc_cid is not None and pos_row.position >= 100.0 - _EPS)
                else "STOCK_LOT"
            )
            cost_basis = pos_row.position * pos_row.avg_cost * pos_row.multiplier
            mark_price, mark_source = _resolve_mark(pos_row.conid, pos_row, live_marks)
            market_value = (
                (pos_row.position * mark_price * pos_row.multiplier)
                if mark_price is not None
                else None
            )
            unrealized = (market_value - cost_basis) if market_value is not None else None
            open_views.append(
                LedgerOpenPositionView(
                    position_key=f"STK:{pos_row.underlying}",
                    underlying=pos_row.underlying,
                    asset_class="STK",
                    strategy_type=pos_stk_strategy,
                    symbol=pos_row.symbol,
                    expiry=None,
                    strike=None,
                    right=None,
                    quantity=pos_row.position,
                    avg_cost_price=pos_row.avg_cost,
                    mark_price=mark_price,
                    mark_source=mark_source,
                    cost_basis_ex_fee=cost_basis,
                    market_value=market_value,
                    unrealized_pnl_ex_fee=unrealized,
                    fees=0.0,
                    campaign_id=cc_cid if pos_stk_strategy == "COVERED_CALL" else None,
                    conid=pos_row.conid,
                    opened_at=_opt_str(pos_row.updated_at),
                )
            )

    open_views.sort(key=lambda p: (p.underlying, p.asset_class, p.expiry or "", p.strike or 0.0))
    return open_views


def _format_chart_date_label(date_str: str, *, include_year: bool) -> str:
    # `date_str` is `YYYY-MM-DD`.
    if len(date_str) == 10 and date_str[4] == "-" and date_str[7] == "-":
        pseudo_expiry = date_str.replace("-", "")
        short = format_expiry_short(pseudo_expiry)
        if include_year:
            return f"{short} '{date_str[2:4]}"
        return short
    return date_str


def build_ledger_report(
    executions_df: pl.DataFrame,
    positions_df: pl.DataFrame,
    *,
    window: LedgerWindow = "1M",
    ticker: str = "ALL",
    group_by: LedgerGroupBy = "campaigns",
    view: LedgerView = "realized",
    include_stock_gain: bool = False,
    roll_max_gap_hours: float = 24.0,
    live_marks: dict[int, float] | None = None,
    as_of: datetime | None = None,
) -> LedgerAnalyticsReport:
    """Run Trade Ledger classification, FIFO/roll stitching, windowing, and MTM valuation."""
    resolved_as_of = as_of if as_of is not None else datetime.now(UTC)
    as_of_utc = (
        resolved_as_of.replace(tzinfo=UTC)
        if resolved_as_of.tzinfo is None
        else resolved_as_of.astimezone(UTC)
    )
    window_start = compute_window_start(window, as_of_utc)

    raw_ticker = ticker.strip().upper()
    ticker_norm = "ALL" if raw_ticker in ("", "ALL") else normalize_underlying(raw_ticker)

    all_execs = _extract_executions(executions_df, as_of_utc)
    all_positions = _extract_positions(positions_df)
    available_tickers = sorted(
        {ex.underlying for ex in all_execs} | {pos.underlying for pos in all_positions}
    )

    filtered_execs = (
        all_execs
        if ticker_norm == "ALL"
        else [ex for ex in all_execs if ex.underlying == ticker_norm]
    )
    filtered_positions = (
        all_positions
        if ticker_norm == "ALL"
        else [pos for pos in all_positions if pos.underlying == ticker_norm]
    )

    execs_by_u: dict[str, list[NormalizedExecution]] = defaultdict(list)
    for ex in filtered_execs:
        execs_by_u[ex.underlying].append(ex)

    pos_stk_by_u: dict[str, float] = {}
    for pos in filtered_positions:
        if pos.asset_class == "STK":
            pos_stk_by_u[pos.underlying] = pos_stk_by_u.get(pos.underlying, 0.0) + pos.position

    underlyings = sorted(set(execs_by_u) | {pos.underlying for pos in filtered_positions})

    all_campaigns: list[CampaignRecord] = []
    campaigns_by_id: dict[str, CampaignRecord] = {}
    open_opt_lots_all: list[OptLot] = []
    open_stk_lots_all: list[StkLot] = []
    all_stk_realizations: list[StkRealization] = []
    exec_attribution: dict[str, tuple[float, LedgerStrategyType, str | None]] = {}

    for u in underlyings:
        u_execs = execs_by_u.get(u, [])
        if not u_execs:
            continue
        u_pos_stk = pos_stk_by_u.get(u)
        res = build_underlying_ledger(
            u,
            u_execs,
            u_pos_stk,
            roll_max_gap_hours=roll_max_gap_hours,
        )
        all_campaigns.extend(res.campaigns)
        for camp in res.campaigns:
            campaigns_by_id[camp.campaign_id] = camp
        open_opt_lots_all.extend(res.open_opt_lots)
        open_stk_lots_all.extend(res.open_stk_lots)
        all_stk_realizations.extend(res.stk_realizations)
        for eid, attr in res.exec_attribution.items():
            exec_attribution[eid] = (
                attr.realized_pnl_ex_fee,
                attr.strategy_type,
                attr.campaign_id,
            )

    # Track open contract quantity per campaign from open option FIFO lots.
    open_lots_by_camp: dict[str, list[OptLot]] = defaultdict(list)
    for lot in open_opt_lots_all:
        if lot.campaign_id:
            open_lots_by_camp[lot.campaign_id].append(lot)

    campaign_open_contracts_map: dict[str, float] = {}
    for camp in all_campaigns:
        lots = open_lots_by_camp.get(camp.campaign_id, [])
        by_leg: dict[tuple[tuple[str, str, float, Literal["C", "P"]], str], float] = defaultdict(
            float
        )
        for lot in lots:
            if lot.remaining_qty > _EPS:
                by_leg[(lot.contract_key, lot.direction)] += lot.remaining_qty
        campaign_open_contracts_map[camp.campaign_id] = max(by_leg.values()) if by_leg else 0.0

    # Build open positions for View 2 (and campaign-level MTM linkage when `view == "open_mtm"`).
    open_positions_built: list[LedgerOpenPositionView] = []
    camp_mtm_sums: dict[str, float] = {}
    if view == "open_mtm":
        open_positions_built = _build_open_positions(
            open_opt_lots_all=open_opt_lots_all,
            open_stk_lots_all=open_stk_lots_all,
            positions_list=filtered_positions,
            campaigns_by_id=campaigns_by_id,
            campaign_open_contracts_map=campaign_open_contracts_map,
            include_stock_gain=include_stock_gain,
            live_marks=live_marks,
        )
        mtm_acc: dict[str, list[float]] = defaultdict(list)
        for op in open_positions_built:
            if op.campaign_id is not None and op.unrealized_pnl_ex_fee is not None:
                mtm_acc[op.campaign_id].append(op.unrealized_pnl_ex_fee)
        for cid, vals in mtm_acc.items():
            camp_mtm_sums[cid] = sum(vals)

    def _in_window(dt: datetime) -> bool:
        return window_start is None or (window_start <= dt <= as_of_utc)

    # Attribute stock realizations in window per campaign (when `include_stock_gain=True`).
    stk_reals_by_camp: dict[str, list[StkRealization]] = defaultdict(list)
    standalone_stk_by_u: dict[str, list[StkRealization]] = defaultdict(list)
    for real in all_stk_realizations:
        if not _in_window(real.close_executed_at_dt):
            continue
        if real.campaign_id is not None:
            stk_reals_by_camp[real.campaign_id].append(real)
        else:
            standalone_stk_by_u[real.underlying].append(real)

    # Build window-attributed `LedgerCampaignRow` list.
    window_campaigns: list[LedgerCampaignRow] = []
    daily_pnl: dict[str, float] = defaultdict(float)
    daily_premium: dict[str, float] = defaultdict(float)
    daily_fees: dict[str, float] = defaultdict(float)

    for camp in all_campaigns:
        steps_in_win = [s for s in camp.steps if _in_window(_parse_utc_dt(s.executed_at))]
        camp_stk_reals = stk_reals_by_camp.get(camp.campaign_id, []) if include_stock_gain else []
        if not steps_in_win and not camp_stk_reals:
            continue

        contracts_open = campaign_open_contracts_map.get(camp.campaign_id, 0.0)
        status = resolve_campaign_status(camp, contracts_open)

        net_prem = sum(s.cashflow_ex_fee for s in steps_in_win)
        opt_realized = sum(s.realized_pnl_ex_fee for s in steps_in_win)
        opt_fees = sum(s.fees for s in steps_in_win)
        rolls_in_win = sum(1 for s in steps_in_win if s.action == "ROLL")

        stk_pnl = sum(r.realized_pnl_ex_fee for r in camp_stk_reals) if include_stock_gain else 0.0
        stk_fees = 0.0
        if include_stock_gain:
            for r in camp_stk_reals:
                stk_fees += r.close_fee_slice
                if _in_window(r.open_executed_at_dt):
                    stk_fees += r.open_fee_slice

        for s in steps_in_win:
            d_key = s.executed_at[:10]
            daily_pnl[d_key] += s.realized_pnl_ex_fee
            daily_premium[d_key] += s.cashflow_ex_fee
            daily_fees[d_key] += s.fees

        if include_stock_gain:
            for r in camp_stk_reals:
                close_d = r.close_executed_at[:10]
                daily_pnl[close_d] += r.realized_pnl_ex_fee
                daily_fees[close_d] += r.close_fee_slice
                if _in_window(r.open_executed_at_dt):
                    daily_fees[r.open_executed_at[:10]] += r.open_fee_slice

        window_campaigns.append(
            LedgerCampaignRow(
                campaign_id=camp.campaign_id,
                underlying=camp.underlying,
                strategy_type=camp.strategy_type,
                status=status,
                opened_at=camp.opened_at,
                last_activity_at=camp.last_activity_at,
                roll_count=rolls_in_win,
                initial_summary=camp.initial_summary,
                current_summary=camp.current_summary,
                contracts_total=camp.contracts_total,
                contracts_open=contracts_open,
                net_premium_ex_fee=net_prem,
                realized_pnl_ex_fee=opt_realized,
                stock_realized_pnl_ex_fee=stk_pnl,
                unrealized_mtm_ex_fee=(
                    camp_mtm_sums.get(camp.campaign_id) if view == "open_mtm" else None
                ),
                fees=opt_fees + stk_fees,
                steps=steps_in_win,
            )
        )

    # Emit standalone `STOCK_LOT` campaign rows when `include_stock_gain=True` & realized.
    if include_stock_gain:
        for u, reals in sorted(standalone_stk_by_u.items()):
            stk_pnl = sum(r.realized_pnl_ex_fee for r in reals)
            stk_fees = 0.0
            total_qty = sum(r.quantity for r in reals)
            opened_dt = min(r.open_executed_at_dt for r in reals)
            closed_dt = max(r.close_executed_at_dt for r in reals)
            for r in reals:
                stk_fees += r.close_fee_slice
                daily_pnl[r.close_executed_at[:10]] += r.realized_pnl_ex_fee
                daily_fees[r.close_executed_at[:10]] += r.close_fee_slice
                if _in_window(r.open_executed_at_dt):
                    stk_fees += r.open_fee_slice
                    daily_fees[r.open_executed_at[:10]] += r.open_fee_slice

            open_stk_qty = sum(
                lot.remaining_qty for lot in open_stk_lots_all if lot.underlying == u
            )
            curr_sum = f"{u} {open_stk_qty:g} shs" if open_stk_qty > _EPS else "Closed"
            window_campaigns.append(
                LedgerCampaignRow(
                    campaign_id=f"{u}-STK",
                    underlying=u,
                    strategy_type="STOCK_LOT",
                    status="OPEN" if open_stk_qty > _EPS else "CLOSED",
                    opened_at=opened_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    last_activity_at=closed_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    roll_count=0,
                    initial_summary=f"{u} {total_qty:g} shs",
                    current_summary=curr_sum,
                    contracts_total=total_qty,
                    contracts_open=open_stk_qty,
                    net_premium_ex_fee=0.0,
                    realized_pnl_ex_fee=0.0,
                    stock_realized_pnl_ex_fee=stk_pnl,
                    unrealized_mtm_ex_fee=None,
                    fees=stk_fees,
                    steps=[],
                )
            )

    window_campaigns.sort(key=lambda c: (c.last_activity_at, c.campaign_id), reverse=True)

    # Breakdowns (`strategy` or `ticker`).
    breakdowns: list[LedgerBreakdownRow] = []
    if group_by in ("strategy", "ticker"):
        grouped_camps: dict[str, list[LedgerCampaignRow]] = defaultdict(list)
        for camp_row in window_campaigns:
            g_key = camp_row.strategy_type if group_by == "strategy" else camp_row.underlying
            grouped_camps[g_key].append(camp_row)

        for g_key, items in grouped_camps.items():
            closed_items = [c for c in items if c.status != "OPEN"]
            wins = sum(
                1
                for c in closed_items
                if (c.realized_pnl_ex_fee + c.stock_realized_pnl_ex_fee) > _EPS
            )
            losses = sum(
                1
                for c in closed_items
                if (c.realized_pnl_ex_fee + c.stock_realized_pnl_ex_fee) < -_EPS
            )
            win_rate = (100.0 * wins / len(closed_items)) if closed_items else None
            label = (
                _STRATEGY_LABELS.get(items[0].strategy_type, g_key)
                if group_by == "strategy"
                else g_key
            )
            breakdowns.append(
                LedgerBreakdownRow(
                    key=g_key,
                    label=label,
                    trade_count=len(items),
                    roll_count=sum(c.roll_count for c in items),
                    win_count=wins,
                    loss_count=losses,
                    win_rate_pct=win_rate,
                    net_premium_ex_fee=sum(c.net_premium_ex_fee for c in items),
                    realized_pnl_ex_fee=sum(c.realized_pnl_ex_fee for c in items),
                    stock_realized_pnl_ex_fee=sum(c.stock_realized_pnl_ex_fee for c in items),
                    fees=sum(c.fees for c in items),
                )
            )
        breakdowns.sort(
            key=lambda b: (-(b.realized_pnl_ex_fee + b.stock_realized_pnl_ex_fee), b.key)
        )

    # Normalized executions view (`group_by == "executions"`).
    execution_views: list[LedgerExecutionView] = []
    if group_by == "executions":
        for ex in reversed(filtered_execs):
            if not _in_window(ex.executed_at_dt):
                continue
            if ex.asset_class == "STK" and not include_stock_gain:
                continue
            default_strat: LedgerStrategyType = (
                "STOCK_LOT" if ex.asset_class == "STK" else "CUSTOM_COMBO"
            )
            default_attr: tuple[float, LedgerStrategyType, str | None] = (
                0.0,
                default_strat,
                None,
            )
            realized_val, strat, exec_cid = exec_attribution.get(ex.exec_id, default_attr)
            execution_views.append(
                LedgerExecutionView(
                    exec_id=ex.exec_id,
                    ib_order_id=ex.ib_order_id,
                    executed_at=ex.executed_at,
                    underlying=ex.underlying,
                    asset_class=ex.asset_class,
                    symbol=ex.symbol,
                    expiry=ex.expiry,
                    strike=ex.strike,
                    right=ex.right,
                    side=ex.side,
                    quantity=ex.quantity,
                    price=ex.price,
                    proceeds_ex_fee=ex.proceeds_ex_fee,
                    commission=ex.commission_base,
                    realized_pnl_ex_fee=realized_val,
                    open_close=ex.open_close,
                    event_code=ex.event_code,
                    strategy_type=strat,
                    campaign_id=exec_cid,
                    source=ex.source,
                )
            )

    # Chart points (daily sorted cumulative curve).
    chart_points: list[LedgerChartPoint] = []
    all_dates = sorted(set(daily_pnl) | set(daily_premium) | set(daily_fees))
    if all_dates:
        multi_year = len({d[:4] for d in all_dates}) > 1
        cum_pnl = 0.0
        cum_prem = 0.0
        for d in all_dates:
            p_pnl = daily_pnl.get(d, 0.0)
            p_prem = daily_premium.get(d, 0.0)
            p_fee = daily_fees.get(d, 0.0)
            cum_pnl += p_pnl
            cum_prem += p_prem
            chart_points.append(
                LedgerChartPoint(
                    date=d,
                    label=_format_chart_date_label(d, include_year=multi_year),
                    cumulative_realized_ex_fee=cum_pnl,
                    cumulative_premium_ex_fee=cum_prem,
                    period_pnl_ex_fee=p_pnl,
                    fees=p_fee,
                )
            )

    # KPIs (strict separation between View 1 realized/cashflow and View 2 unrealized MTM).
    realized_total = sum(c.realized_pnl_ex_fee for c in window_campaigns)
    stock_total = sum(c.stock_realized_pnl_ex_fee for c in window_campaigns)
    net_premium_total = sum(c.net_premium_ex_fee for c in window_campaigns)
    fees_total = sum(c.fees for c in window_campaigns)
    roll_total = sum(c.roll_count for c in window_campaigns)

    closed_camps = [c for c in window_campaigns if c.status != "OPEN"]
    closed_count = len(closed_camps)
    win_camps = sum(
        1 for c in closed_camps if (c.realized_pnl_ex_fee + c.stock_realized_pnl_ex_fee) > _EPS
    )
    win_rate_pct = (100.0 * win_camps / closed_count) if closed_count > 0 else None

    if view == "open_mtm":
        open_camp_ids = {
            camp.campaign_id
            for camp in all_campaigns
            if campaign_open_contracts_map.get(camp.campaign_id, 0.0) > _EPS
        }
        unmatched_pos = sum(1 for op in open_positions_built if op.campaign_id is None)
        open_count = len(open_camp_ids) + unmatched_pos
        valid_mtms = [
            op.unrealized_pnl_ex_fee
            for op in open_positions_built
            if op.unrealized_pnl_ex_fee is not None
        ]
        unrealized_mtm: float | None = sum(valid_mtms) if valid_mtms else None
    else:
        open_count = sum(1 for c in window_campaigns if c.status == "OPEN")
        unrealized_mtm = None

    kpis = LedgerKPIs(
        realized_pnl_ex_fee=realized_total,
        stock_pnl_ex_fee=stock_total,
        total_pnl_ex_fee=realized_total + stock_total,
        net_premium_ex_fee=net_premium_total,
        fees=fees_total,
        win_rate_pct=win_rate_pct,
        closed_count=closed_count,
        open_count=open_count,
        roll_count=roll_total,
        unrealized_mtm_ex_fee=unrealized_mtm,
    )

    return LedgerAnalyticsReport(
        as_of=as_of_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        window=window,
        window_start=window_start.strftime("%Y-%m-%dT%H:%M:%SZ") if window_start else None,
        ticker=ticker_norm,
        group_by=group_by,
        view=view,
        include_stock_gain=include_stock_gain,
        available_tickers=available_tickers,
        kpis=kpis,
        campaigns=window_campaigns,
        breakdowns=breakdowns,
        executions=execution_views,
        open_positions=open_positions_built if view == "open_mtm" else [],
        chart_points=chart_points,
    )
