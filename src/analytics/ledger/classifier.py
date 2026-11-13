"""Episode clustering, option combo classification, and share-coverage checks."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from src.analytics.ledger.models import LedgerStrategyType, RollAction
from src.storage.ledger_schema import (
    LedgerAssetClass,
    LedgerEventCode,
    LedgerOpenClose,
    LedgerSide,
)

OptionContractKey = tuple[str, str, float, Literal["C", "P"]]

_MONTH_ABBR: tuple[str, ...] = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


@dataclass(frozen=True, slots=True)
class NormalizedExecution:
    """Validated, timezone-aware execution record derived from Polars input."""

    exec_id: str
    ib_order_id: str | None
    conid: int | None
    underlying_conid: int | None
    symbol: str
    underlying: str
    asset_class: LedgerAssetClass
    expiry: str | None
    strike: float | None
    right: Literal["C", "P"] | None
    multiplier: int
    side: LedgerSide
    quantity: float
    price: float
    proceeds_ex_fee: float
    commission_base: float
    realized_pnl_ib: float | None
    open_close: LedgerOpenClose | None
    event_code: LedgerEventCode
    executed_at: str
    executed_at_dt: datetime
    source: str
    seq_index: int


@dataclass(frozen=True, slots=True)
class NormalizedPosition:
    """Validated open position snapshot row derived from Polars input."""

    conid: int | None
    symbol: str
    underlying: str
    asset_class: LedgerAssetClass
    expiry: str | None
    strike: float | None
    right: Literal["C", "P"] | None
    multiplier: int
    position: float
    avg_cost: float
    mark_price: float | None
    market_value: float | None
    unrealized_pnl: float | None
    source: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class AggregatedLeg:
    """Aggregated option leg within a single open or close leg-group."""

    underlying: str
    expiry: str
    strike: float
    right: Literal["C", "P"]
    side: LedgerSide
    quantity: float
    price: float
    cashflow_ex_fee: float
    fees: float
    multiplier: int
    conid: int | None
    symbol: str

    @property
    def contract_key(self) -> OptionContractKey:
        """Return canonical `(underlying, expiry, strike, right)` contract key."""
        return (self.underlying, self.expiry, self.strike, self.right)


def compute_initial_stk_shares(
    stk_execs: Sequence[NormalizedExecution],
    pos_stk_shares: float | None,
) -> float:
    """Reconstruct pre-ledger share inventory from `open_positions` anchor and `STK` fills."""
    net_delta = 0.0
    min_cum_delta = 0.0
    for ex in stk_execs:
        signed_qty = ex.quantity if ex.side == "BOT" else -ex.quantity
        net_delta += signed_qty
        if net_delta < min_cum_delta:
            min_cum_delta = net_delta

    if pos_stk_shares is not None:
        return pos_stk_shares - net_delta
    return max(0.0, -min_cum_delta)


def _episode_lifecycle_kind(ep: Sequence[NormalizedExecution]) -> bool | None:
    """Return `True` if all fills are `TRADE`, `False` if all are lifecycle events, else `None`."""
    kinds = {ex.event_code == "TRADE" for ex in ep}
    if len(kinds) != 1:
        return None
    return next(iter(kinds))


def cluster_option_episodes(
    opt_execs: Sequence[NormalizedExecution],
    *,
    max_cluster_gap_sec: float = 2.0,
) -> list[list[NormalizedExecution]]:
    """Cluster same-underlying option executions into atomic multi-leg episodes.

    Executions sharing a non-empty `ib_order_id` are grouped together regardless of
    sub-second or multi-second fill delays. Remaining fills without `ib_order_id`
    are clustered when consecutive timestamps are within `<= max_cluster_gap_sec`
    and share the same lifecycle type (`TRADE` vs `EXPIRED`/`ASSIGNED`/`EXERCISED`).
    Adjacent chronological clusters within `<= max_cluster_gap_sec` of the same
    lifecycle kind are then merged so simultaneous multi-leg fills with distinct
    `ib_order_id` values form one episode.
    """
    if not opt_execs:
        return []

    by_order: dict[str, list[NormalizedExecution]] = defaultdict(list)
    unlinked: list[NormalizedExecution] = []

    for ex in opt_execs:
        if ex.ib_order_id:
            by_order[ex.ib_order_id].append(ex)
        else:
            unlinked.append(ex)

    episodes: list[list[NormalizedExecution]] = []
    for group in by_order.values():
        group.sort(key=lambda e: (e.executed_at_dt, e.seq_index))
        episodes.append(group)

    unlinked.sort(key=lambda e: (e.executed_at_dt, e.seq_index))
    current: list[NormalizedExecution] = []
    for ex in unlinked:
        if not current:
            current = [ex]
            continue
        prev = current[-1]
        gap_sec = (ex.executed_at_dt - prev.executed_at_dt).total_seconds()
        same_lifecycle_kind = (ex.event_code == "TRADE") == (prev.event_code == "TRADE")
        if same_lifecycle_kind and 0.0 <= gap_sec <= max_cluster_gap_sec:
            current.append(ex)
        else:
            episodes.append(current)
            current = [ex]
    if current:
        episodes.append(current)

    episodes.sort(
        key=lambda ep: (
            min(e.executed_at_dt for e in ep),
            min(e.seq_index for e in ep),
        )
    )

    merged: list[list[NormalizedExecution]] = []
    for ep in episodes:
        if not merged:
            merged.append(list(ep))
            continue
        prev_ep = merged[-1]
        prev_max_dt = max(e.executed_at_dt for e in prev_ep)
        next_min_dt = min(e.executed_at_dt for e in ep)
        gap_sec = (next_min_dt - prev_max_dt).total_seconds()
        prev_kind = _episode_lifecycle_kind(prev_ep)
        next_kind = _episode_lifecycle_kind(ep)
        if prev_kind is not None and prev_kind == next_kind and gap_sec <= max_cluster_gap_sec:
            prev_ep.extend(ep)
            prev_ep.sort(key=lambda e: (e.executed_at_dt, e.seq_index))
        else:
            merged.append(list(ep))

    return merged


def aggregate_legs(legs: Sequence[AggregatedLeg]) -> list[AggregatedLeg]:
    """Collapse partial fills on the same `(contract_key, side)` into distinct legs."""
    grouped: dict[tuple[OptionContractKey, LedgerSide], list[AggregatedLeg]] = defaultdict(list)
    for leg in legs:
        grouped[(leg.contract_key, leg.side)].append(leg)

    merged: list[AggregatedLeg] = []
    for items in grouped.values():
        first = items[0]
        total_qty = sum(item.quantity for item in items)
        total_cash = sum(item.cashflow_ex_fee for item in items)
        total_fees = sum(item.fees for item in items)
        conid = next((item.conid for item in items if item.conid is not None), None)
        if total_qty > 1e-9 and first.multiplier > 0 and abs(total_cash) > 1e-12:
            avg_price = abs(total_cash) / (total_qty * first.multiplier)
        elif total_qty > 1e-9:
            avg_price = sum(item.price * item.quantity for item in items) / total_qty
        else:
            avg_price = first.price
        merged.append(
            AggregatedLeg(
                underlying=first.underlying,
                expiry=first.expiry,
                strike=first.strike,
                right=first.right,
                side=first.side,
                quantity=total_qty,
                price=avg_price,
                cashflow_ex_fee=total_cash,
                fees=total_fees,
                multiplier=first.multiplier,
                conid=conid,
                symbol=first.symbol,
            )
        )

    merged.sort(key=lambda leg: (leg.expiry, leg.right, leg.strike, leg.side))
    return merged


def classify_option_legs(
    legs: Sequence[AggregatedLeg],
    *,
    available_stk_shares: float = 0.0,
) -> LedgerStrategyType:
    """Classify an option leg-group into a canonical `LedgerStrategyType`."""
    distinct = aggregate_legs(legs)
    n_legs = len(distinct)
    if n_legs == 0:
        return "CUSTOM_COMBO"

    if n_legs == 1:
        leg = distinct[0]
        if leg.right == "P":
            return "SHORT_PUT" if leg.side == "SLD" else "LONG_PUT"
        if leg.side == "BOT":
            return "LONG_CALL"
        required_shares = 100.0 * leg.quantity
        if available_stk_shares >= required_shares - 1e-6:
            return "COVERED_CALL"
        return "NAKED_CALL"

    if n_legs == 2:
        a, b = distinct[0], distinct[1]
        same_qty = abs(a.quantity - b.quantity) <= 1e-6
        if a.right == b.right:
            if a.side != b.side and same_qty:
                long_leg = a if a.side == "BOT" else b
                short_leg = b if a.side == "BOT" else a
                if long_leg.expiry == short_leg.expiry:
                    if abs(long_leg.strike - short_leg.strike) <= 1e-6:
                        return "CUSTOM_COMBO"
                    if long_leg.right == "P":
                        return (
                            "BULL_PUT_SPREAD"
                            if short_leg.strike > long_leg.strike
                            else "BEAR_PUT_SPREAD"
                        )
                    return (
                        "BULL_CALL_SPREAD"
                        if long_leg.strike < short_leg.strike
                        else "BEAR_CALL_SPREAD"
                    )
                if abs(long_leg.strike - short_leg.strike) <= 1e-6:
                    return "CALENDAR_SPREAD"
                return "DIAGONAL_SPREAD"
            return "CUSTOM_COMBO"

        # One Call + One Put
        if a.side == b.side and a.expiry == b.expiry and same_qty:
            if abs(a.strike - b.strike) <= 1e-6:
                return "STRADDLE"
            return "STRANGLE"
        return "CUSTOM_COMBO"

    if n_legs == 4:
        expiries = {leg.expiry for leg in distinct}
        qtys = [leg.quantity for leg in distinct]
        same_qty = max(qtys) - min(qtys) <= 1e-6
        if len(expiries) == 1 and same_qty:
            puts = sorted(
                (leg for leg in distinct if leg.right == "P"),
                key=lambda leg: leg.strike,
            )
            calls = sorted(
                (leg for leg in distinct if leg.right == "C"),
                key=lambda leg: leg.strike,
            )
            if len(puts) == 2 and len(calls) == 2:
                low_p, high_p = puts
                low_c, high_c = calls
                put_sides = {low_p.side, high_p.side}
                call_sides = {low_c.side, high_c.side}
                if (
                    put_sides == {"BOT", "SLD"}
                    and call_sides == {"BOT", "SLD"}
                    and low_p.strike < high_p.strike - 1e-6
                    and high_p.strike <= low_c.strike + 1e-6
                    and low_c.strike < high_c.strike - 1e-6
                ):
                    short_body_pair = (
                        low_p.side == "BOT"
                        and high_p.side == "SLD"
                        and low_c.side == "SLD"
                        and high_c.side == "BOT"
                    )
                    long_body_pair = (
                        low_p.side == "SLD"
                        and high_p.side == "BOT"
                        and low_c.side == "BOT"
                        and high_c.side == "SLD"
                    )
                    if short_body_pair or long_body_pair:
                        if abs(high_p.strike - low_c.strike) <= 1e-6:
                            return "IRON_BUTTERFLY"
                        return "IRON_CONDOR"
        return "CUSTOM_COMBO"

    return "CUSTOM_COMBO"


def format_expiry_short(expiry: str | None) -> str:
    """Format `YYYYMMDD` into concise `Dec20` style label."""
    if not expiry:
        return ""
    text = expiry.strip()
    if len(text) == 8 and text.isdigit():
        month = int(text[4:6])
        day = int(text[6:8])
        if 1 <= month <= 12:
            return f"{_MONTH_ABBR[month]}{day:02d}"
    return text


def format_strike(strike: float) -> str:
    """Format strike price concisely (e.g. `$135` or `$132.5`)."""
    rounded = round(strike)
    if abs(strike - rounded) <= 1e-6:
        return f"${int(rounded)}"
    return f"${strike:g}"


def format_legs_strike_summary(legs: Sequence[AggregatedLeg]) -> str:
    """Build a compact strike summary string across 1..N option legs."""
    distinct = aggregate_legs(legs)
    if not distinct:
        return "-"
    if len(distinct) == 1:
        leg = distinct[0]
        return f"{format_strike(leg.strike)}{leg.right}"

    rights = {leg.right for leg in distinct}
    if len(rights) == 1:
        right = next(iter(rights))
        ordered = sorted(
            distinct,
            key=lambda leg: leg.strike,
            reverse=(right == "P"),
        )
        strikes_txt = "/".join(format_strike(leg.strike) for leg in ordered)
        return f"{strikes_txt}{right}"

    puts = sorted((leg for leg in distinct if leg.right == "P"), key=lambda leg: leg.strike)
    calls = sorted((leg for leg in distinct if leg.right == "C"), key=lambda leg: leg.strike)
    if len(puts) == 1 and len(calls) == 1 and abs(puts[0].strike - calls[0].strike) <= 1e-6:
        return f"{format_strike(puts[0].strike)}P/C"
    parts: list[str] = []
    if puts:
        parts.append("/".join(format_strike(leg.strike) for leg in puts) + "P")
    if calls:
        parts.append("/".join(format_strike(leg.strike) for leg in calls) + "C")
    return "-".join(parts)


def format_legs_position_label(legs: Sequence[AggregatedLeg]) -> str:
    """Format concise expiry + strike summary (e.g. `Dec20 $135C` or `Dec20 $135/$130P`)."""
    distinct = aggregate_legs(legs)
    if not distinct:
        return "-"
    expiries = sorted({leg.expiry for leg in distinct})
    exp_label = "/".join(format_expiry_short(e) for e in expiries)
    strike_label = format_legs_strike_summary(distinct)
    return f"{exp_label} {strike_label}".strip()


def format_step_summary(
    action: RollAction,
    legs: Sequence[AggregatedLeg],
    *,
    cashflow_ex_fee: float,
    contracts: float,
    roll_old_legs: Sequence[AggregatedLeg] | None = None,
) -> str:
    """Build a human-readable summary string for a `RollStep`."""
    distinct = aggregate_legs(legs)
    pos_label = format_legs_position_label(distinct)
    strike_label = format_legs_strike_summary(distinct)
    mult = distinct[0].multiplier if distinct else 100
    per_contract_price = (
        abs(cashflow_ex_fee) / (contracts * mult)
        if contracts > 1e-9 and mult > 0
        else (distinct[0].price if distinct else 0.0)
    )

    if action == "ROLL" and roll_old_legs:
        old_strikes = format_legs_strike_summary(roll_old_legs)
        net_per_share = (
            cashflow_ex_fee / (contracts * mult) if contracts > 1e-9 and mult > 0 else 0.0
        )
        if net_per_share >= 0.0:
            net_txt = f"+${net_per_share:.2f} cr"
        else:
            net_txt = f"-${abs(net_per_share):.2f} db"
        return f"Rolled {old_strikes} -> {strike_label} ({net_txt})"

    if action in ("OPEN", "ADD"):
        if len(distinct) == 1:
            verb = "STO" if distinct[0].side == "SLD" else "BTO"
            return f"{verb} {pos_label} @ ${distinct[0].price:.2f}"
        verb = "STO" if cashflow_ex_fee >= 0.0 else "BTO"
        tag = "cr" if cashflow_ex_fee >= 0.0 else "db"
        return f"{verb} {pos_label} @ ${per_contract_price:.2f} {tag}"

    if action in ("CLOSE", "TRIM"):
        if len(distinct) == 1:
            verb = "BTC" if distinct[0].side == "BOT" else "STC"
            return f"{verb} {pos_label} @ ${distinct[0].price:.2f}"
        verb = "BTC" if cashflow_ex_fee <= 0.0 else "STC"
        tag = "cr" if cashflow_ex_fee > 0.0 else "db"
        return f"{verb} {pos_label} @ ${per_contract_price:.2f} {tag}"

    if action == "EXPIRE":
        return f"Expired {pos_label}"
    if action == "ASSIGN":
        return f"Assigned {pos_label}"
    if action == "EXERCISE":
        return f"Exercised {pos_label}"
    return f"Rolled -> {pos_label}"
