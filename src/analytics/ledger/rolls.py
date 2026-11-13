"""FIFO lot accounting, 1-day roll-chain stitching, and Covered Call share pairing."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from src.analytics.ledger.classifier import (
    AggregatedLeg,
    NormalizedExecution,
    OptionContractKey,
    aggregate_legs,
    classify_option_legs,
    cluster_option_episodes,
    compute_initial_stk_shares,
    format_legs_position_label,
    format_legs_strike_summary,
    format_step_summary,
)
from src.analytics.ledger.models import CampaignStatus, LedgerStrategyType, RollAction, RollStep
from src.storage.ledger_schema import LedgerEventCode, LedgerSide

_EPS = 1e-6

_ROLL_COMPATIBLE_FAMILIES: dict[LedgerStrategyType, frozenset[LedgerStrategyType]] = {
    "COVERED_CALL": frozenset({"COVERED_CALL", "NAKED_CALL"}),
    "NAKED_CALL": frozenset({"COVERED_CALL", "NAKED_CALL"}),
    "CALENDAR_SPREAD": frozenset({"CALENDAR_SPREAD", "DIAGONAL_SPREAD"}),
    "DIAGONAL_SPREAD": frozenset({"CALENDAR_SPREAD", "DIAGONAL_SPREAD"}),
}


@dataclass(slots=True)
class OptLot:
    """Open FIFO option lot."""

    lot_id: int
    exec_id: str
    underlying: str
    expiry: str
    strike: float
    right: Literal["C", "P"]
    multiplier: int
    direction: Literal["LONG", "SHORT"]
    initial_qty: float
    remaining_qty: float
    open_cashflow_total: float
    open_fee_total: float
    open_fee_remaining: float
    conid: int | None
    symbol: str
    opened_at: str
    opened_at_dt: datetime
    campaign_id: str = ""

    @property
    def contract_key(self) -> OptionContractKey:
        """Return canonical contract tuple `(underlying, expiry, strike, right)`."""
        return (self.underlying, self.expiry, self.strike, self.right)


@dataclass(slots=True)
class StkLot:
    """Open FIFO stock lot."""

    exec_id: str
    underlying: str
    direction: Literal["LONG", "SHORT"]
    initial_qty: float
    remaining_qty: float
    open_cashflow_total: float
    open_fee_total: float
    open_fee_remaining: float
    conid: int | None
    symbol: str
    executed_at: str
    executed_at_dt: datetime


@dataclass(slots=True)
class StkRealization:
    """Realized stock P&L slice from closing a FIFO `StkLot`."""

    close_exec_id: str
    open_exec_id: str
    underlying: str
    close_side: LedgerSide
    quantity: float
    close_price: float
    realized_pnl_ex_fee: float
    open_fee_slice: float
    close_fee_slice: float
    open_executed_at: str
    open_executed_at_dt: datetime
    close_executed_at: str
    close_executed_at_dt: datetime
    ib_order_id: str | None
    event_code: LedgerEventCode
    campaign_id: str | None = None


@dataclass(slots=True)
class _CloseSlice:
    campaign_id: str | None
    leg: AggregatedLeg
    realized_pnl_ex_fee: float
    event_code: LedgerEventCode
    exec_id: str


@dataclass(slots=True)
class _PendingRollClose:
    closed_at_dt: datetime
    closed_legs: list[AggregatedLeg]
    contracts: float
    cashflow: float


@dataclass(slots=True)
class AssignRecord:
    """Assignment or exercise event metadata used for stock-gain linkage."""

    underlying: str
    strike: float
    right: Literal["C", "P"]
    contracts: float
    executed_at_dt: datetime
    ib_order_id: str | None
    event_code: LedgerEventCode


@dataclass(slots=True)
class CampaignRecord:
    """Full chronological campaign state before window attribution."""

    campaign_id: str
    underlying: str
    strategy_type: LedgerStrategyType
    opened_at: str
    opened_at_dt: datetime
    last_activity_at: str
    last_activity_at_dt: datetime
    initial_summary: str
    current_summary: str
    contracts_total: float
    steps: list[RollStep] = field(default_factory=list)
    active_legs: list[AggregatedLeg] = field(default_factory=list)
    terminal_event: RollAction | None = None
    last_roll_candidate_close: _PendingRollClose | None = None
    assign_events: list[AssignRecord] = field(default_factory=list)


@dataclass(slots=True)
class ExecutionAttribution:
    """Per-execution FIFO realized P&L and campaign/strategy attribution."""

    realized_pnl_ex_fee: float = 0.0
    strategy_type: LedgerStrategyType = "STOCK_LOT"
    campaign_id: str | None = None


@dataclass(slots=True)
class UnderlyingLedgerResult:
    """Complete reconstructed ledger state for one underlying symbol."""

    campaigns: list[CampaignRecord]
    open_opt_lots: list[OptLot]
    open_stk_lots: list[StkLot]
    stk_realizations: list[StkRealization]
    exec_attribution: dict[str, ExecutionAttribution]


def compute_roll_label(
    closed_legs: Sequence[AggregatedLeg],
    opened_legs: Sequence[AggregatedLeg],
) -> str:
    """Compute descriptive roll label (`Roll Up & Out`, `Roll Down & Out`, etc.)."""
    if not closed_legs or not opened_legs:
        return "Roll"

    old_avg_strike = sum(leg.strike for leg in closed_legs) / len(closed_legs)
    new_avg_strike = sum(leg.strike for leg in opened_legs) / len(opened_legs)
    strike_diff = new_avg_strike - old_avg_strike

    old_exp = max(leg.expiry for leg in closed_legs)
    new_exp = max(leg.expiry for leg in opened_legs)

    strike_dir = "Up" if strike_diff > _EPS else ("Down" if strike_diff < -_EPS else "")
    exp_dir = "Out" if new_exp > old_exp else ("In" if new_exp < old_exp else "")

    if strike_dir and exp_dir:
        return f"Roll {strike_dir} & {exp_dir}"
    if strike_dir:
        return f"Roll {strike_dir}"
    if exp_dir:
        return f"Roll {exp_dir}"
    return "Roll"


def _is_direction_flip(
    closed_legs: Sequence[AggregatedLeg],
    opened_legs: Sequence[AggregatedLeg],
) -> bool:
    """Return True when the close and open hit the exact same contract in opposite sides."""
    if len(closed_legs) != 1 or len(opened_legs) != 1:
        return False
    c, o = closed_legs[0], opened_legs[0]
    return c.contract_key == o.contract_key and c.side == o.side


def _are_roll_compatible(
    campaign_strategy: LedgerStrategyType,
    closed_legs: Sequence[AggregatedLeg],
    opened_strategy: LedgerStrategyType,
    opened_legs: Sequence[AggregatedLeg],
    *,
    same_order: bool,
) -> bool:
    if _is_direction_flip(closed_legs, opened_legs):
        return False
    if same_order:
        return True
    allowed = _ROLL_COMPATIBLE_FAMILIES.get(campaign_strategy, frozenset({campaign_strategy}))
    if opened_strategy not in allowed:
        return False
    closed_rights = {leg.right for leg in closed_legs}
    opened_rights = {leg.right for leg in opened_legs}
    return closed_rights == opened_rights


def _campaign_open_contracts(lots: Sequence[OptLot]) -> float:
    if not lots:
        return 0.0
    by_leg: dict[tuple[OptionContractKey, str], float] = defaultdict(float)
    for lot in lots:
        if lot.remaining_qty > _EPS:
            by_leg[(lot.contract_key, lot.direction)] += lot.remaining_qty
    if not by_leg:
        return 0.0
    return max(by_leg.values())


def _legs_from_open_lots(lots: Sequence[OptLot]) -> list[AggregatedLeg]:
    raw = [
        AggregatedLeg(
            underlying=lot.underlying,
            expiry=lot.expiry,
            strike=lot.strike,
            right=lot.right,
            side="SLD" if lot.direction == "SHORT" else "BOT",
            quantity=lot.remaining_qty,
            price=(
                abs(lot.open_cashflow_total) / (lot.initial_qty * lot.multiplier)
                if lot.initial_qty > _EPS and lot.multiplier > 0
                else 0.0
            ),
            cashflow_ex_fee=lot.open_cashflow_total * (lot.remaining_qty / lot.initial_qty)
            if lot.initial_qty > _EPS
            else 0.0,
            fees=lot.open_fee_remaining,
            multiplier=lot.multiplier,
            conid=lot.conid,
            symbol=lot.symbol,
        )
        for lot in lots
        if lot.remaining_qty > _EPS
    ]
    return aggregate_legs(raw)


def _invert_legs_for_classification(legs: Sequence[AggregatedLeg]) -> list[AggregatedLeg]:
    """Invert closing leg sides and cashflows to implied opening direction for classification."""
    inverted: list[AggregatedLeg] = []
    for leg in legs:
        open_side: LedgerSide = "SLD" if leg.side == "BOT" else "BOT"
        inverted.append(replace(leg, side=open_side, cashflow_ex_fee=-leg.cashflow_ex_fee))
    return inverted


def _match_stk_execution(
    ex: NormalizedExecution,
    stk_queues: dict[Literal["LONG", "SHORT"], deque[StkLot]],
    realizations: list[StkRealization],
    exec_attribution: dict[str, ExecutionAttribution],
    *,
    pre_ledger_shares: list[float],
) -> None:
    """Apply FIFO stock execution; update open `stk_queues` and record realizations."""
    remaining = ex.quantity
    close_dir: Literal["LONG", "SHORT"] = "LONG" if ex.side == "SLD" else "SHORT"
    open_dir: Literal["LONG", "SHORT"] = "LONG" if ex.side == "BOT" else "SHORT"
    realized_total = 0.0

    # Match against open FIFO stock lots of opposite direction.
    opp_queue = stk_queues[close_dir]
    while remaining > _EPS and opp_queue:
        lot = opp_queue[0]
        matched = min(remaining, lot.remaining_qty)
        lot_ratio = matched / lot.initial_qty if lot.initial_qty > _EPS else 0.0
        ex_ratio = matched / ex.quantity if ex.quantity > _EPS else 0.0
        open_cash_slice = lot.open_cashflow_total * lot_ratio
        close_cash_slice = ex.proceeds_ex_fee * ex_ratio
        pnl_slice = open_cash_slice + close_cash_slice
        open_fee_slice = lot.open_fee_total * lot_ratio
        close_fee_slice = ex.commission_base * ex_ratio

        lot.remaining_qty -= matched
        lot.open_fee_remaining = max(0.0, lot.open_fee_remaining - open_fee_slice)
        remaining -= matched
        realized_total += pnl_slice

        realizations.append(
            StkRealization(
                close_exec_id=ex.exec_id,
                open_exec_id=lot.exec_id,
                underlying=ex.underlying,
                close_side=ex.side,
                quantity=matched,
                close_price=ex.price,
                realized_pnl_ex_fee=pnl_slice,
                open_fee_slice=open_fee_slice,
                close_fee_slice=close_fee_slice,
                open_executed_at=lot.executed_at,
                open_executed_at_dt=lot.executed_at_dt,
                close_executed_at=ex.executed_at,
                close_executed_at_dt=ex.executed_at_dt,
                ib_order_id=ex.ib_order_id,
                event_code=ex.event_code,
            )
        )
        if lot.remaining_qty <= _EPS:
            opp_queue.popleft()

    # If pre-ledger shares existed (e.g. bought >365d ago), consume them before creating a short.
    if remaining > _EPS and ex.side == "SLD" and pre_ledger_shares[0] > _EPS:
        absorbed = min(remaining, pre_ledger_shares[0])
        pre_ledger_shares[0] -= absorbed
        remaining -= absorbed

    if remaining > _EPS and ex.open_close != "C":
        ratio = remaining / ex.quantity if ex.quantity > _EPS else 1.0
        stk_queues[open_dir].append(
            StkLot(
                exec_id=ex.exec_id,
                underlying=ex.underlying,
                direction=open_dir,
                initial_qty=remaining,
                remaining_qty=remaining,
                open_cashflow_total=ex.proceeds_ex_fee * ratio,
                open_fee_total=ex.commission_base * ratio,
                open_fee_remaining=ex.commission_base * ratio,
                conid=ex.conid,
                symbol=ex.symbol,
                executed_at=ex.executed_at,
                executed_at_dt=ex.executed_at_dt,
            )
        )

    exec_attribution[ex.exec_id] = ExecutionAttribution(
        realized_pnl_ex_fee=realized_total,
        strategy_type="STOCK_LOT",
        campaign_id=None,
    )


def _current_net_stk_shares(
    stk_queues: dict[Literal["LONG", "SHORT"], deque[StkLot]],
    pre_ledger_shares: float,
) -> float:
    long_qty = sum(lot.remaining_qty for lot in stk_queues["LONG"])
    short_qty = sum(lot.remaining_qty for lot in stk_queues["SHORT"])
    return pre_ledger_shares + long_qty - short_qty


def _open_short_call_contracts(opt_queues: dict[OptionContractKey, deque[OptLot]]) -> float:
    total = 0.0
    for (_u, _exp, _k, right), q in opt_queues.items():
        if right == "C":
            for lot in q:
                if lot.direction == "SHORT" and lot.remaining_qty > _EPS:
                    total += lot.remaining_qty
    return total


def _link_stock_realizations_to_assignments(
    campaigns: Sequence[CampaignRecord],
    realizations: Sequence[StkRealization],
) -> None:
    """Attach FIFO stock realizations caused by option assignment/exercise to their campaign."""
    candidates: list[tuple[CampaignRecord, AssignRecord]] = [
        (camp, rec) for camp in campaigns for rec in camp.assign_events
    ]
    if not candidates or not realizations:
        return

    for real in realizations:
        best_cid: str | None = None
        best_score = float("inf")
        for camp, assign in candidates:
            if camp.underlying != real.underlying:
                continue
            # Covered Call / Short Call assignment -> SLD 100 * N shares at strike.
            # Long Put exercise -> SLD 100 * N shares at strike.
            expected_side: LedgerSide = (
                "SLD"
                if (assign.right == "C" and camp.strategy_type in ("COVERED_CALL", "NAKED_CALL"))
                or (assign.right == "P" and camp.strategy_type == "LONG_PUT")
                else "BOT"
            )
            if real.close_side != expected_side:
                continue
            price_diff = abs(real.close_price - assign.strike)
            if price_diff > 0.05 and real.event_code not in ("ASSIGNED", "EXERCISED"):
                continue
            day_gap = (
                abs((real.close_executed_at_dt - assign.executed_at_dt).total_seconds()) / 86400.0
            )
            if day_gap > 3.5:
                continue
            same_order_bonus = (
                -10.0 if (real.ib_order_id and real.ib_order_id == assign.ib_order_id) else 0.0
            )
            score = same_order_bonus + day_gap + price_diff
            if score < best_score:
                best_score = score
                best_cid = camp.campaign_id
        real.campaign_id = best_cid


def build_underlying_ledger(
    underlying: str,
    execs: Sequence[NormalizedExecution],
    pos_stk_shares: float | None,
    *,
    roll_max_gap_hours: float = 24.0,
) -> UnderlyingLedgerResult:
    """Reconstruct campaigns, rolls, FIFO option/stock lots, and attributions for one ticker."""
    stk_execs = sorted(
        (ex for ex in execs if ex.asset_class == "STK"),
        key=lambda ex: (ex.executed_at_dt, ex.seq_index),
    )
    opt_execs = sorted(
        (ex for ex in execs if ex.asset_class == "OPT"),
        key=lambda ex: (
            ex.executed_at_dt,
            0 if ex.open_close == "O" else 1,
            ex.seq_index,
        ),
    )

    pre_ledger_shares = [compute_initial_stk_shares(stk_execs, pos_stk_shares)]
    stk_queues: dict[Literal["LONG", "SHORT"], deque[StkLot]] = {
        "LONG": deque(),
        "SHORT": deque(),
    }
    stk_realizations: list[StkRealization] = []
    exec_attribution: dict[str, ExecutionAttribution] = {}

    opt_episodes = cluster_option_episodes(opt_execs)
    opt_queues: dict[OptionContractKey, deque[OptLot]] = defaultdict(deque)
    lots_by_campaign: dict[str, list[OptLot]] = defaultdict(list)
    campaigns: list[CampaignRecord] = []
    campaigns_by_id: dict[str, CampaignRecord] = {}

    lot_seq = 0
    camp_seq = 0
    stk_idx = 0
    n_stk = len(stk_execs)

    for ep in opt_episodes:
        ep_min_dt = min(e.executed_at_dt for e in ep)
        ep_max_dt = max(e.executed_at_dt for e in ep)
        ep_order_ids = {e.ib_order_id for e in ep if e.ib_order_id}
        ep_ib_order_id = next(iter(ep_order_ids)) if len(ep_order_ids) == 1 else None

        # Advance stock fills up to episode timestamp (+ <=2s or shared ib_order_id buy-writes).
        while stk_idx < n_stk:
            stk_ex = stk_execs[stk_idx]
            dt_diff = (stk_ex.executed_at_dt - ep_max_dt).total_seconds()
            is_buywrite_pair = (stk_ex.side == "BOT" and 0.0 < dt_diff <= 2.0) or (
                stk_ex.ib_order_id is not None and stk_ex.ib_order_id in ep_order_ids
            )
            if dt_diff <= 0.0 or is_buywrite_pair:
                _match_stk_execution(
                    stk_ex,
                    stk_queues,
                    stk_realizations,
                    exec_attribution,
                    pre_ledger_shares=pre_ledger_shares,
                )
                stk_idx += 1
            else:
                break

        # Separate closing slices and new opening lots via FIFO per leg in episode.
        close_slices: list[_CloseSlice] = []
        new_open_lots: list[OptLot] = []
        new_open_legs_raw: list[AggregatedLeg] = []

        for ex in ep:
            assert ex.expiry is not None and ex.strike is not None and ex.right is not None
            ckey: OptionContractKey = (ex.underlying, ex.expiry, ex.strike, ex.right)
            q = opt_queues[ckey]
            is_lifecycle_close = ex.event_code in ("EXPIRED", "ASSIGNED", "EXERCISED")
            expected_lot_dir: Literal["LONG", "SHORT"] = "SHORT" if ex.side == "BOT" else "LONG"

            close_qty_target = 0.0
            open_qty_target = 0.0
            if is_lifecycle_close or ex.open_close == "C":
                close_qty_target = ex.quantity
            elif ex.open_close == "O":
                open_qty_target = ex.quantity
            else:
                # open_close is None (e.g. GATEWAY_LIVE): match opposite open lots first.
                avail_opp = sum(
                    lot.remaining_qty
                    for lot in q
                    if lot.direction == expected_lot_dir and lot.remaining_qty > _EPS
                )
                close_qty_target = min(ex.quantity, avail_opp)
                open_qty_target = max(0.0, ex.quantity - close_qty_target)

            ex_realized = 0.0
            ex_campaign_id: str | None = None

            if close_qty_target > _EPS:
                rem_close = close_qty_target
                # Prefer opposite direction lots; fallback for lifecycle closes if same side.
                for target_dir in (
                    (expected_lot_dir,)
                    if not is_lifecycle_close
                    else (expected_lot_dir, "LONG" if expected_lot_dir == "SHORT" else "SHORT")
                ):
                    if rem_close <= _EPS:
                        break
                    # Walk FIFO queue matching target_dir.
                    kept: deque[OptLot] = deque()
                    while q:
                        lot = q.popleft()
                        if lot.direction != target_dir or rem_close <= _EPS:
                            kept.append(lot)
                            continue
                        matched = min(rem_close, lot.remaining_qty)
                        lot_ratio = matched / lot.initial_qty if lot.initial_qty > _EPS else 0.0
                        ex_ratio = matched / ex.quantity if ex.quantity > _EPS else 0.0
                        open_cash = lot.open_cashflow_total * lot_ratio
                        close_cash = ex.proceeds_ex_fee * ex_ratio
                        pnl = open_cash + close_cash
                        open_fee = lot.open_fee_total * lot_ratio
                        close_fee = ex.commission_base * ex_ratio

                        lot.remaining_qty -= matched
                        lot.open_fee_remaining = max(0.0, lot.open_fee_remaining - open_fee)
                        rem_close -= matched
                        ex_realized += pnl
                        ex_campaign_id = lot.campaign_id

                        close_slices.append(
                            _CloseSlice(
                                campaign_id=lot.campaign_id,
                                leg=AggregatedLeg(
                                    underlying=ex.underlying,
                                    expiry=ex.expiry,
                                    strike=ex.strike,
                                    right=ex.right,
                                    side=ex.side,
                                    quantity=matched,
                                    price=ex.price,
                                    cashflow_ex_fee=close_cash,
                                    fees=close_fee,
                                    multiplier=ex.multiplier,
                                    conid=ex.conid or lot.conid,
                                    symbol=ex.symbol,
                                ),
                                realized_pnl_ex_fee=pnl,
                                event_code=ex.event_code,
                                exec_id=ex.exec_id,
                            )
                        )
                        if lot.remaining_qty > _EPS:
                            kept.append(lot)
                    opt_queues[ckey] = kept
                    q = opt_queues[ckey]

                # Unmatched close (opened prior to history window) — do not open phantom short/long!
                if rem_close > _EPS:
                    ex_ratio = rem_close / ex.quantity if ex.quantity > _EPS else 0.0
                    unmatched_pnl = (
                        ex.realized_pnl_ib * ex_ratio if ex.realized_pnl_ib is not None else 0.0
                    )
                    ex_realized += unmatched_pnl
                    close_slices.append(
                        _CloseSlice(
                            campaign_id=None,
                            leg=AggregatedLeg(
                                underlying=ex.underlying,
                                expiry=ex.expiry,
                                strike=ex.strike,
                                right=ex.right,
                                side=ex.side,
                                quantity=rem_close,
                                price=ex.price,
                                cashflow_ex_fee=ex.proceeds_ex_fee * ex_ratio,
                                fees=ex.commission_base * ex_ratio,
                                multiplier=ex.multiplier,
                                conid=ex.conid,
                                symbol=ex.symbol,
                            ),
                            realized_pnl_ex_fee=unmatched_pnl,
                            event_code=ex.event_code,
                            exec_id=ex.exec_id,
                        )
                    )

            if open_qty_target > _EPS:
                lot_seq += 1
                ex_ratio = open_qty_target / ex.quantity if ex.quantity > _EPS else 1.0
                open_cash = ex.proceeds_ex_fee * ex_ratio
                open_fee = ex.commission_base * ex_ratio
                new_lot = OptLot(
                    lot_id=lot_seq,
                    exec_id=ex.exec_id,
                    underlying=ex.underlying,
                    expiry=ex.expiry,
                    strike=ex.strike,
                    right=ex.right,
                    multiplier=ex.multiplier,
                    direction="LONG" if ex.side == "BOT" else "SHORT",
                    initial_qty=open_qty_target,
                    remaining_qty=open_qty_target,
                    open_cashflow_total=open_cash,
                    open_fee_total=open_fee,
                    open_fee_remaining=open_fee,
                    conid=ex.conid,
                    symbol=ex.symbol,
                    opened_at=ex.executed_at,
                    opened_at_dt=ex.executed_at_dt,
                )
                q.append(new_lot)
                new_open_lots.append(new_lot)
                new_open_legs_raw.append(
                    AggregatedLeg(
                        underlying=ex.underlying,
                        expiry=ex.expiry,
                        strike=ex.strike,
                        right=ex.right,
                        side=ex.side,
                        quantity=open_qty_target,
                        price=ex.price,
                        cashflow_ex_fee=open_cash,
                        fees=open_fee,
                        multiplier=ex.multiplier,
                        conid=ex.conid,
                        symbol=ex.symbol,
                    )
                )

            exec_attribution[ex.exec_id] = ExecutionAttribution(
                realized_pnl_ex_fee=ex_realized,
                strategy_type="CUSTOM_COMBO",
                campaign_id=ex_campaign_id,
            )

        # Compute available uncovered shares AFTER closes, BEFORE classifying new opens.
        net_shares = _current_net_stk_shares(stk_queues, pre_ledger_shares[0])
        already_covered_calls = _open_short_call_contracts(opt_queues)
        # Subtract newly added short calls in this episode because they are being classified now.
        new_short_call_qty = sum(
            lot.remaining_qty
            for lot in new_open_lots
            if lot.right == "C" and lot.direction == "SHORT"
        )
        other_short_calls = max(0.0, already_covered_calls - new_short_call_qty)
        available_shares = net_shares - 100.0 * other_short_calls

        open_legs = aggregate_legs(new_open_legs_raw)
        open_strategy: LedgerStrategyType | None = (
            classify_option_legs(open_legs, available_stk_shares=available_shares)
            if open_legs
            else None
        )

        # Now resolve Campaign updates across all 3 structural patterns.
        slices_by_camp: dict[str | None, list[_CloseSlice]] = defaultdict(list)
        for sl in close_slices:
            slices_by_camp[sl.campaign_id].append(sl)

        # Check Pattern 1: Simultaneous ROLL (1 campaign closed + new legs opened in same episode).
        linked_campaigns = [cid for cid in slices_by_camp if cid is not None]
        if (
            len(linked_campaigns) == 1
            and None not in slices_by_camp
            and open_legs
            and open_strategy is not None
        ):
            target_cid = linked_campaigns[0]
            assert target_cid is not None
            camp = campaigns_by_id[target_cid]
            camp_slices = slices_by_camp[target_cid]
            closed_legs = aggregate_legs([s.leg for s in camp_slices])
            same_order = ep_ib_order_id is not None and all(
                e.ib_order_id == ep_ib_order_id for e in ep
            )
            if _are_roll_compatible(
                camp.strategy_type,
                closed_legs,
                open_strategy,
                open_legs,
                same_order=same_order,
            ):
                for lot in new_open_lots:
                    lot.campaign_id = camp.campaign_id
                    lots_by_campaign[camp.campaign_id].append(lot)
                for ex in ep:
                    exec_attribution[ex.exec_id].campaign_id = camp.campaign_id
                    exec_attribution[ex.exec_id].strategy_type = camp.strategy_type

                roll_contracts = max(leg.quantity for leg in open_legs)
                step_cash = sum(s.leg.cashflow_ex_fee for s in camp_slices) + sum(
                    leg.cashflow_ex_fee for leg in open_legs
                )
                step_realized = sum(s.realized_pnl_ex_fee for s in camp_slices)
                step_fees = sum(s.leg.fees for s in camp_slices) + sum(
                    leg.fees for leg in open_legs
                )
                roll_label = compute_roll_label(closed_legs, open_legs)
                summary = format_step_summary(
                    "ROLL",
                    open_legs,
                    cashflow_ex_fee=step_cash,
                    contracts=roll_contracts,
                    roll_old_legs=closed_legs,
                )
                exp_val = max(leg.expiry for leg in open_legs)
                camp.steps.append(
                    RollStep(
                        step_index=len(camp.steps) + 1,
                        executed_at=ep_max_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        action="ROLL",
                        roll_label=roll_label,
                        summary=summary,
                        expiry=exp_val,
                        strike_summary=format_legs_strike_summary(open_legs),
                        contracts=roll_contracts,
                        cashflow_ex_fee=step_cash,
                        realized_pnl_ex_fee=step_realized,
                        fees=step_fees,
                        ib_order_id=ep_ib_order_id,
                    )
                )
                camp.last_activity_at = ep_max_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                camp.last_activity_at_dt = ep_max_dt
                camp.active_legs = _legs_from_open_lots(lots_by_campaign[camp.campaign_id])
                camp.current_summary = format_legs_position_label(camp.active_legs or open_legs)
                camp.contracts_total = max(
                    camp.contracts_total,
                    _campaign_open_contracts(lots_by_campaign[camp.campaign_id]),
                )
                camp.terminal_event = None
                camp.last_roll_candidate_close = None
                continue

        # Pattern 2 (or non-compatible close part): process close slices per campaign.
        for cid, camp_slices in slices_by_camp.items():
            closed_legs = aggregate_legs([s.leg for s in camp_slices])
            step_cash = sum(s.leg.cashflow_ex_fee for s in camp_slices)
            step_realized = sum(s.realized_pnl_ex_fee for s in camp_slices)
            step_fees = sum(s.leg.fees for s in camp_slices)
            closed_contracts = max(leg.quantity for leg in closed_legs)
            event_codes = {s.event_code for s in camp_slices}

            if cid is None:
                # Pre-window orphan close: create a standalone closed campaign record.
                camp_seq += 1
                orphan_cid = f"{underlying}-{camp_seq:04d}"
                implied_open_legs = _invert_legs_for_classification(closed_legs)
                orphan_strategy = classify_option_legs(
                    implied_open_legs, available_stk_shares=net_shares
                )
                action: RollAction = (
                    "ASSIGN"
                    if "ASSIGNED" in event_codes
                    else (
                        "EXPIRE"
                        if "EXPIRED" in event_codes
                        else ("EXERCISE" if "EXERCISED" in event_codes else "CLOSE")
                    )
                )
                ts_iso = ep_max_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                pos_lbl = format_legs_position_label(closed_legs)
                step = RollStep(
                    step_index=1,
                    executed_at=ts_iso,
                    action=action,
                    summary=format_step_summary(
                        action,
                        closed_legs,
                        cashflow_ex_fee=step_cash,
                        contracts=closed_contracts,
                    ),
                    expiry=max(leg.expiry for leg in closed_legs),
                    strike_summary=format_legs_strike_summary(closed_legs),
                    contracts=closed_contracts,
                    cashflow_ex_fee=step_cash,
                    realized_pnl_ex_fee=step_realized,
                    fees=step_fees,
                    ib_order_id=ep_ib_order_id,
                )
                orphan_camp = CampaignRecord(
                    campaign_id=orphan_cid,
                    underlying=underlying,
                    strategy_type=orphan_strategy,
                    opened_at=ts_iso,
                    opened_at_dt=ep_min_dt,
                    last_activity_at=ts_iso,
                    last_activity_at_dt=ep_max_dt,
                    initial_summary=pos_lbl,
                    current_summary=pos_lbl,
                    contracts_total=closed_contracts,
                    steps=[step],
                    active_legs=[],
                    terminal_event=action,
                )
                for s in camp_slices:
                    exec_attribution[s.exec_id].campaign_id = orphan_cid
                    exec_attribution[s.exec_id].strategy_type = orphan_strategy
                campaigns.append(orphan_camp)
                campaigns_by_id[orphan_cid] = orphan_camp
                continue

            camp = campaigns_by_id[cid]
            open_after = _campaign_open_contracts(lots_by_campaign[cid])
            if "ASSIGNED" in event_codes:
                action = "ASSIGN"
            elif "EXPIRED" in event_codes:
                action = "EXPIRE"
            elif "EXERCISED" in event_codes:
                action = "EXERCISE"
            else:
                action = "TRIM" if open_after > _EPS else "CLOSE"

            ts_iso = ep_max_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            camp.steps.append(
                RollStep(
                    step_index=len(camp.steps) + 1,
                    executed_at=ts_iso,
                    action=action,
                    summary=format_step_summary(
                        action,
                        closed_legs,
                        cashflow_ex_fee=step_cash,
                        contracts=closed_contracts,
                    ),
                    expiry=max(leg.expiry for leg in closed_legs),
                    strike_summary=format_legs_strike_summary(closed_legs),
                    contracts=closed_contracts,
                    cashflow_ex_fee=step_cash,
                    realized_pnl_ex_fee=step_realized,
                    fees=step_fees,
                    ib_order_id=ep_ib_order_id,
                )
            )
            camp.last_activity_at = ts_iso
            camp.last_activity_at_dt = ep_max_dt
            remaining_active = _legs_from_open_lots(lots_by_campaign[cid])
            camp.active_legs = remaining_active
            if remaining_active:
                camp.current_summary = format_legs_position_label(remaining_active)
            else:
                camp.current_summary = format_legs_position_label(closed_legs)

            if action in ("ASSIGN", "EXERCISE"):
                for cleg in closed_legs:
                    ev_code: LedgerEventCode = "ASSIGNED" if action == "ASSIGN" else "EXERCISED"
                    camp.assign_events.append(
                        AssignRecord(
                            underlying=underlying,
                            strike=cleg.strike,
                            right=cleg.right,
                            contracts=cleg.quantity,
                            executed_at_dt=ep_max_dt,
                            ib_order_id=ep_ib_order_id,
                            event_code=ev_code,
                        )
                    )

            if open_after <= _EPS:
                camp.terminal_event = action
            if action in ("CLOSE", "TRIM") and (open_after <= _EPS or len(camp.active_legs) <= 1):
                camp.last_roll_candidate_close = _PendingRollClose(
                    closed_at_dt=ep_max_dt,
                    closed_legs=closed_legs,
                    contracts=closed_contracts,
                    cashflow=step_cash,
                )
            else:
                camp.last_roll_candidate_close = None

            for s in camp_slices:
                exec_attribution[s.exec_id].campaign_id = cid
                exec_attribution[s.exec_id].strategy_type = camp.strategy_type

        # Pattern 3: Handle remaining `open_legs` (1-day Roll Stitch, Scale-In ADD, or New OPEN).
        if open_legs and open_strategy is not None:
            open_contracts = max(leg.quantity for leg in open_legs)
            open_cash = sum(leg.cashflow_ex_fee for leg in open_legs)
            open_fees = sum(leg.fees for leg in open_legs)
            ts_iso = ep_max_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

            # 3a. Search for 1-day roll candidate.
            best_camp: CampaignRecord | None = None
            best_score: tuple[float, float] = (float("inf"), float("inf"))
            if roll_max_gap_hours > 0.0:
                max_gap_td = timedelta(hours=roll_max_gap_hours)
                for cand in campaigns:
                    pending = cand.last_roll_candidate_close
                    if pending is None:
                        continue
                    gap_td = ep_min_dt - pending.closed_at_dt
                    if gap_td < timedelta(0) or gap_td > max_gap_td:
                        continue
                    if not _are_roll_compatible(
                        cand.strategy_type,
                        pending.closed_legs,
                        open_strategy,
                        open_legs,
                        same_order=False,
                    ):
                        continue
                    q1, q2 = pending.contracts, open_contracts
                    if min(q1, q2) <= _EPS or max(q1, q2) > 4.0 * min(q1, q2) + _EPS:
                        continue
                    score = (abs(q1 - q2), gap_td.total_seconds())
                    if score < best_score:
                        best_score = score
                        best_camp = cand

            if best_camp is not None and best_camp.last_roll_candidate_close is not None:
                pending = best_camp.last_roll_candidate_close
                for lot in new_open_lots:
                    lot.campaign_id = best_camp.campaign_id
                    lots_by_campaign[best_camp.campaign_id].append(lot)
                for lot in new_open_lots:
                    exec_attribution[lot.exec_id].campaign_id = best_camp.campaign_id
                    exec_attribution[lot.exec_id].strategy_type = best_camp.strategy_type

                roll_label = compute_roll_label(pending.closed_legs, open_legs)
                summary = format_step_summary(
                    "ROLL",
                    open_legs,
                    cashflow_ex_fee=pending.cashflow + open_cash,
                    contracts=open_contracts,
                    roll_old_legs=pending.closed_legs,
                )
                best_camp.steps.append(
                    RollStep(
                        step_index=len(best_camp.steps) + 1,
                        executed_at=ts_iso,
                        action="ROLL",
                        roll_label=roll_label,
                        summary=summary,
                        expiry=max(leg.expiry for leg in open_legs),
                        strike_summary=format_legs_strike_summary(open_legs),
                        contracts=open_contracts,
                        cashflow_ex_fee=open_cash,
                        realized_pnl_ex_fee=0.0,
                        fees=open_fees,
                        ib_order_id=ep_ib_order_id,
                    )
                )
                best_camp.last_activity_at = ts_iso
                best_camp.last_activity_at_dt = ep_max_dt
                best_camp.active_legs = _legs_from_open_lots(
                    lots_by_campaign[best_camp.campaign_id]
                )
                best_camp.current_summary = format_legs_position_label(
                    best_camp.active_legs or open_legs
                )
                best_camp.contracts_total = max(
                    best_camp.contracts_total,
                    _campaign_open_contracts(lots_by_campaign[best_camp.campaign_id]),
                )
                best_camp.terminal_event = None
                best_camp.last_roll_candidate_close = None
                continue

            # 3b. Check scale-in ADD on an active campaign with identical leg keys & strategy.
            open_sig = {(leg.contract_key, leg.side) for leg in open_legs}
            add_camp: CampaignRecord | None = None
            for cand in reversed(campaigns):
                if _campaign_open_contracts(lots_by_campaign[cand.campaign_id]) <= _EPS:
                    continue
                if cand.strategy_type != open_strategy:
                    continue
                cand_sig = {(leg.contract_key, leg.side) for leg in cand.active_legs}
                if cand_sig == open_sig:
                    add_camp = cand
                    break

            if add_camp is not None:
                for lot in new_open_lots:
                    lot.campaign_id = add_camp.campaign_id
                    lots_by_campaign[add_camp.campaign_id].append(lot)
                    exec_attribution[lot.exec_id].campaign_id = add_camp.campaign_id
                    exec_attribution[lot.exec_id].strategy_type = add_camp.strategy_type

                add_camp.steps.append(
                    RollStep(
                        step_index=len(add_camp.steps) + 1,
                        executed_at=ts_iso,
                        action="ADD",
                        summary=format_step_summary(
                            "ADD",
                            open_legs,
                            cashflow_ex_fee=open_cash,
                            contracts=open_contracts,
                        ),
                        expiry=max(leg.expiry for leg in open_legs),
                        strike_summary=format_legs_strike_summary(open_legs),
                        contracts=open_contracts,
                        cashflow_ex_fee=open_cash,
                        realized_pnl_ex_fee=0.0,
                        fees=open_fees,
                        ib_order_id=ep_ib_order_id,
                    )
                )
                add_camp.last_activity_at = ts_iso
                add_camp.last_activity_at_dt = ep_max_dt
                add_camp.active_legs = _legs_from_open_lots(lots_by_campaign[add_camp.campaign_id])
                add_camp.current_summary = format_legs_position_label(add_camp.active_legs)
                add_camp.contracts_total += open_contracts
                add_camp.last_roll_candidate_close = None
                continue

            # 3c. New campaign OPEN.
            camp_seq += 1
            new_cid = f"{underlying}-{camp_seq:04d}"
            pos_lbl = format_legs_position_label(open_legs)
            for lot in new_open_lots:
                lot.campaign_id = new_cid
                lots_by_campaign[new_cid].append(lot)
                exec_attribution[lot.exec_id].campaign_id = new_cid
                exec_attribution[lot.exec_id].strategy_type = open_strategy

            new_camp = CampaignRecord(
                campaign_id=new_cid,
                underlying=underlying,
                strategy_type=open_strategy,
                opened_at=ts_iso,
                opened_at_dt=ep_min_dt,
                last_activity_at=ts_iso,
                last_activity_at_dt=ep_max_dt,
                initial_summary=pos_lbl,
                current_summary=pos_lbl,
                contracts_total=open_contracts,
                active_legs=list(open_legs),
                steps=[
                    RollStep(
                        step_index=1,
                        executed_at=ts_iso,
                        action="OPEN",
                        summary=format_step_summary(
                            "OPEN",
                            open_legs,
                            cashflow_ex_fee=open_cash,
                            contracts=open_contracts,
                        ),
                        expiry=max(leg.expiry for leg in open_legs),
                        strike_summary=format_legs_strike_summary(open_legs),
                        contracts=open_contracts,
                        cashflow_ex_fee=open_cash,
                        realized_pnl_ex_fee=0.0,
                        fees=open_fees,
                        ib_order_id=ep_ib_order_id,
                    )
                ],
            )
            campaigns.append(new_camp)
            campaigns_by_id[new_cid] = new_camp

    # Flush remaining stock fills occurring after the last option episode.
    while stk_idx < n_stk:
        _match_stk_execution(
            stk_execs[stk_idx],
            stk_queues,
            stk_realizations,
            exec_attribution,
            pre_ledger_shares=pre_ledger_shares,
        )
        stk_idx += 1

    # Link realized stock assignments/exercises to their option campaigns in a post-pass.
    _link_stock_realizations_to_assignments(campaigns, stk_realizations)

    open_opt_lots = [lot for q in opt_queues.values() for lot in q if lot.remaining_qty > _EPS]
    open_stk_lots = [lot for q in stk_queues.values() for lot in q if lot.remaining_qty > _EPS]
    return UnderlyingLedgerResult(
        campaigns=campaigns,
        open_opt_lots=open_opt_lots,
        open_stk_lots=open_stk_lots,
        stk_realizations=stk_realizations,
        exec_attribution=exec_attribution,
    )


def resolve_campaign_status(
    campaign: CampaignRecord,
    contracts_open: float,
) -> CampaignStatus:
    """Resolve current `CampaignStatus` from remaining contracts and terminal step."""
    if contracts_open > _EPS:
        return "OPEN"
    if campaign.terminal_event == "ASSIGN":
        return "ASSIGNED"
    if campaign.terminal_event == "EXPIRE":
        return "EXPIRED"
    return "CLOSED"
