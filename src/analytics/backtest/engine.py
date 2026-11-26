"""Strict chronological backtest engine for systematic runs and single-trade replays."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import polars as pl

from src.analytics.backtest.context import BarContext, clean_positive_price, extract_bar_context
from src.analytics.backtest.fast_cache import FastBarCache
from src.analytics.backtest.metrics import (
    build_portfolio_summary,
    finalize_trade_record,
    pct_or_none,
)
from src.analytics.backtest.models import (
    FLAG_BROKER_FILL_ENTRY,
    FLAG_CALLED_AWAY,
    FLAG_CAPITAL_DEPLETED,
    FLAG_LEG_FALLBACK_PRICE,
    FLAG_NO_USABLE_DAILY_SNAPSHOT,
    FLAG_PUT_ASSIGNED,
    FLAG_ROLL_SKIPPED,
    FLAG_STOCK_LIQUIDATED_ON_SL,
    FLAG_SYNTHETIC_STOCK_ENTRY,
    FLAG_TRACKED_LEG_USED,
    FLAG_UNRESOLVED_EXPIRY_LEG,
    FLAG_UNRESOLVED_EXPIRY_SPOT,
    BacktestConfig,
    BacktestMode,
    BacktestSimulationResult,
    BacktestStrategyKind,
    BacktestTradeRecord,
    ClosePriceMode,
    EquityCurvePoint,
    ExitReason,
    LegPriceSource,
    StepMarkSource,
    TradeDailyStep,
    TradeStatus,
    WheelPhase,
    deduplicate_flags,
)
from src.analytics.backtest.pricing import (
    EvaluatedStepData,
    evaluate_trade_step_on_bar,
)
from src.analytics.backtest.rules import (
    DENOM_EPS,
    BrokerFillAnchor,
    CadenceState,
    EntryBuildResult,
    build_broker_fill_entry,
    build_entry_candidate_outcome,
    evaluate_exit_rule,
    find_defensive_roll_candidate,
    parse_anchor_ts_utc,
    record_entry_attempt,
    resolve_min_call_strike,
    should_attempt_entry,
)
from src.analytics.strategies.models import StrategyLegInput
from src.core.calendar import fmt_et
from src.core.exceptions import BacktestValidationError
from src.core.security import TickerValidator
from src.storage.backtest_store import DailyChainBar
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA

BACKTEST_ASSUMPTIONS: tuple[str, ...] = (
    "Chronological execution: exits and expiry settlements on bar i evaluate strictly "
    "before new entries on bar i.",
    "Entry prices honor configured price_basis; active close/MTM valuations follow "
    "configured close_price_mode (MID or NATURAL_SPREAD).",
    "Commissions charge commission_per_contract * sum(leg.qty) on open and on market close; "
    "expired legs incur $0 closing commission.",
    "TP/SL triggers evaluate gross option P&L against configured tp_sl_basis; "
    "net P&L and portfolio equity deduct commissions.",
    "Expiry settlement uses session_for(expiry) + 15m DATA_DELAY and requires non-CLOSE "
    "spot (LAST or MID), else marks UNRESOLVED.",
    "COVERED_CALL and WHEEL simulate 100 * qty synthetic shares marked to market daily at spot, "
    "with $0 close fee on Put assignment or Call-away.",
)


@dataclass
class _SyntheticStockLot:
    shares: int
    cost_basis: float
    acquired_date: str
    acquired_snapshot_id: str
    cycle_credits_per_share: float = 0.0
    last_unrealized_pnl: float = 0.0


@dataclass
class _OpenTradeState:
    trade_id: str
    entry_date: str
    entry_snapshot_id: str
    strategy_kind: BacktestStrategyKind
    qty: int
    entry: EntryBuildResult
    open_commission: float
    total_contracts: int
    wheel_phase: WheelPhase | None = None
    stock_shares: int = 0
    stock_cost_basis: float | None = None
    stock_exit_price: float | None = None
    option_net_pnl_dollars: float | None = None
    stock_pnl_dollars: float | None = None
    steps: list[TradeDailyStep] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    status: TradeStatus = "OPEN"
    exit_reason: ExitReason = "END_OF_DATA"
    exit_date: str = ""
    exit_snapshot_id: str = ""
    exit_value_dollars: float | None = None
    commissions_dollars: float = 0.0
    gross_pnl_dollars: float | None = None
    net_pnl_dollars: float | None = None
    last_known_unrealized_net: float = 0.0
    last_step_flags: list[str] = field(default_factory=list)
    last_step_gross_pnl: float | None = None
    last_step_net_pnl: float | None = None


_SINGLE_PRICE_TO_MARK: dict[LegPriceSource, StepMarkSource] = {
    "MID": "MID",
    "LAST": "LAST",
    "CLOSE": "CLOSE",
    "BROKER_FILL": "BROKER_FILL",
}


def _derive_step_mark_source(
    step_data: EvaluatedStepData,
    close_price_mode: ClosePriceMode,
    *,
    quote_missing: bool,
) -> StepMarkSource:
    if quote_missing:
        return "MISSING"
    if FLAG_BROKER_FILL_ENTRY in step_data.flags or (
        step_data.legs and all(leg.quote_source == "BROKER_FILL" for leg in step_data.legs)
    ):
        return "BROKER_FILL"
    if step_data.all_legs_settled:
        return "SETTLEMENT"
    if FLAG_TRACKED_LEG_USED in step_data.flags:
        return "TRACKED"
    if step_data.any_leg_settled:
        return "PARTIAL_SETTLEMENT"
    if close_price_mode == "NATURAL_SPREAD":
        return (
            "NATURAL_FALLBACK" if FLAG_LEG_FALLBACK_PRICE in step_data.flags else "NATURAL_SPREAD"
        )
    sources = {leg.price_source for leg in step_data.legs}
    if len(sources) == 1:
        return _SINGLE_PRICE_TO_MARK.get(next(iter(sources)), "MIXED")
    return "MIXED"


def _stock_step_mtm(
    stock_lot: _SyntheticStockLot | None, spot: float | None
) -> tuple[int, float | None, float | None]:
    if stock_lot is None:
        return 0, None, None
    unrealized = (
        round((spot - stock_lot.cost_basis) * float(stock_lot.shares), 2)
        if (spot is not None and spot > 0.0)
        else stock_lot.last_unrealized_pnl
    )
    return stock_lot.shares, stock_lot.cost_basis, unrealized


def _compute_step_pnls(
    step_data: EvaluatedStepData,
    *,
    entry_cash_mid_dollars: float,
    commission_deducted: float,
    mark_unresolved: bool = False,
) -> tuple[float | None, float | None]:
    if step_data.active_close_value_dollars is not None and not mark_unresolved:
        gross_val = round(entry_cash_mid_dollars + step_data.active_close_value_dollars, 2)
        net_val = round(gross_val - commission_deducted, 2)
        return gross_val, net_val
    return None, None


def _make_daily_step(
    bar: DailyChainBar,
    bar_ctx: BarContext,
    step_data: EvaluatedStepData,
    *,
    entry_cash_mid_dollars: float,
    entry_basis_dollars: float,
    commission_deducted: float,
    close_price_mode: ClosePriceMode,
    extra_flags: Sequence[str] = (),
    mark_unresolved: bool = False,
    stock_shares: int = 0,
    stock_cost_basis: float | None = None,
    stock_unrealized_pnl_dollars: float | None = None,
) -> tuple[TradeDailyStep, float | None, float | None]:
    gross_pnl, net_pnl = _compute_step_pnls(
        step_data,
        entry_cash_mid_dollars=entry_cash_mid_dollars,
        commission_deducted=commission_deducted,
        mark_unresolved=mark_unresolved,
    )

    quote_missing = net_pnl is None
    mark_source = _derive_step_mark_source(step_data, close_price_mode, quote_missing=quote_missing)
    step = TradeDailyStep(
        market_date=bar.market_date,
        snapshot_id=bar.snapshot_id,
        ts_utc=bar.ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        ts_et=fmt_et(bar.ts_utc),
        dte_min_leg=step_data.dte_min_leg,
        spot=bar_ctx.spot,
        spot_source=bar_ctx.spot_source,
        market_data_type=bar_ctx.market_data_type,
        quote_coverage=bar_ctx.quote_coverage,
        legs=step_data.legs,
        mark_value_dollars=step_data.mark_value_dollars,
        natural_close_value_dollars=step_data.natural_close_value_dollars,
        gross_pnl_dollars=gross_pnl,
        net_pnl_dollars=net_pnl,
        pnl_pct_of_basis=pct_or_none(net_pnl, entry_basis_dollars),
        quote_missing=quote_missing,
        mark_source=mark_source,
        status=step_data.status,
        flags=deduplicate_flags([*step_data.flags, *extra_flags]),
        tracked_snapshot_ids=step_data.tracked_snapshot_ids,
        stock_shares=stock_shares,
        stock_cost_basis=stock_cost_basis,
        stock_unrealized_pnl_dollars=stock_unrealized_pnl_dollars,
    )
    return step, gross_pnl, net_pnl


def _record_step_diagnostics(
    run_state: _SimRunState, step_flags: Sequence[str], *, quote_missing: bool
) -> None:
    if quote_missing:
        if (
            FLAG_UNRESOLVED_EXPIRY_SPOT not in step_flags
            and FLAG_UNRESOLVED_EXPIRY_LEG not in step_flags
        ):
            run_state.missing_quote_steps += 1
    elif FLAG_LEG_FALLBACK_PRICE in step_flags or FLAG_TRACKED_LEG_USED in step_flags:
        run_state.fallback_quote_steps += 1


def _start_trade_state(
    trade_id: str,
    bar: DailyChainBar,
    bar_ctx: BarContext,
    entry: EntryBuildResult,
    config: BacktestConfig,
    *,
    wheel_phase: WheelPhase | None = None,
    stock_lot: _SyntheticStockLot | None = None,
    collect_steps: bool = True,
    run_state: _SimRunState | None = None,
) -> _OpenTradeState:
    total_contracts = sum(leg.qty for leg in entry.legs)
    open_comm = round(config.commission_per_contract * float(total_contracts), 2)
    stk_shares, stk_basis, stk_mtm = _stock_step_mtm(stock_lot, bar_ctx.spot)
    if collect_steps:
        step0, gross0, net0 = _make_daily_step(
            bar,
            bar_ctx,
            entry.initial_step,
            entry_cash_mid_dollars=entry.entry_cash_mid_dollars,
            entry_basis_dollars=entry.entry_basis_dollars,
            commission_deducted=open_comm,
            close_price_mode=config.close_price_mode,
            extra_flags=entry.flags,
            stock_shares=stk_shares,
            stock_cost_basis=stk_basis,
            stock_unrealized_pnl_dollars=stk_mtm,
        )
        steps_list = [step0]
        step0_flags = list(step0.flags)
    else:
        gross0, net0 = _compute_step_pnls(
            entry.initial_step,
            entry_cash_mid_dollars=entry.entry_cash_mid_dollars,
            commission_deducted=open_comm,
        )
        steps_list = []
        step0_flags = deduplicate_flags([*entry.initial_step.flags, *entry.flags])
        if run_state is not None:
            _record_step_diagnostics(run_state, step0_flags, quote_missing=(net0 is None))
    return _OpenTradeState(
        trade_id=trade_id,
        entry_date=bar.market_date,
        entry_snapshot_id=bar.snapshot_id,
        strategy_kind=config.strategy_kind,
        qty=config.qty,
        entry=entry,
        open_commission=open_comm,
        total_contracts=total_contracts,
        wheel_phase=wheel_phase,
        stock_shares=stk_shares,
        stock_cost_basis=stk_basis,
        steps=steps_list,
        flags=list(entry.flags),
        commissions_dollars=open_comm,
        gross_pnl_dollars=gross0,
        net_pnl_dollars=net0,
        last_known_unrealized_net=net0 if net0 is not None else -open_comm,
        last_step_flags=step0_flags,
        last_step_gross_pnl=gross0,
        last_step_net_pnl=net0,
    )


def _try_defensive_roll(
    t: _OpenTradeState,
    bar: DailyChainBar,
    bar_ctx: BarContext,
    step_data: EvaluatedStepData,
    config: BacktestConfig,
    stock_lot: _SyntheticStockLot | None,
) -> tuple[EntryBuildResult | None, list[str]]:
    """Attempt an ITM-near-expiry roll for single-leg COVERED_CALL / WHEEL positions."""
    if not config.roll_enabled or config.strategy_kind not in ("COVERED_CALL", "WHEEL"):
        return None, []
    if len(t.entry.legs) != 1 or bar_ctx.spot is None or bar_ctx.spot <= 0.0:
        return None, []
    leg0 = t.entry.legs[0]
    is_itm = (
        bar_ctx.spot > leg0.strike + 1e-6
        if leg0.right == "C"
        else bar_ctx.spot < leg0.strike - 1e-6
    )
    roll_window_dte = config.exit_dte if config.exit_dte is not None else 7
    near_expiry = step_data.any_leg_settled or step_data.dte_min_leg <= roll_window_dte
    if (
        not is_itm
        or not near_expiry
        or step_data.active_close_value_dollars is None
        or step_data.settlement_blocked
    ):
        return None, []

    current_close_px = round(
        abs(step_data.active_close_value_dollars) / (100.0 * float(leg0.qty)), 4
    )
    min_call_strike = (
        resolve_min_call_strike(config, stock_lot.cost_basis, stock_lot.cycle_credits_per_share)
        if (leg0.right == "C" and stock_lot is not None)
        else None
    )
    roll_entry = find_defensive_roll_candidate(
        bar,
        bar_ctx,
        config,
        current_leg=leg0,
        current_close_price_per_share=current_close_px,
        stock_cost_basis=(stock_lot.cost_basis if stock_lot is not None else None),
        min_call_strike=min_call_strike,
    )
    if roll_entry is None:
        return None, [FLAG_ROLL_SKIPPED]
    return roll_entry, []


def _apply_wheel_or_cc_exit(
    t: _OpenTradeState,
    bar: DailyChainBar,
    bar_ctx: BarContext,
    step_data: EvaluatedStepData,
    reason: ExitReason,
    config: BacktestConfig,
    run_state: _SimRunState,
    *,
    close_comm: float,
    opt_gross: float,
    opt_net: float,
) -> float:
    """Resolve option/stock exit for COVERED_CALL / WHEEL; returns `realized_delta`."""
    if config.strategy_kind not in ("COVERED_CALL", "WHEEL") or len(t.entry.legs) != 1:
        t.exit_reason = reason
        t.gross_pnl_dollars = opt_gross
        t.net_pnl_dollars = opt_net
        return opt_net

    leg0 = t.entry.legs[0]
    spot = bar_ctx.spot
    lot = run_state.stock_lot
    last_step = t.steps[-1] if t.steps else None

    if reason == "EXPIRY_SETTLED":
        if (
            leg0.right == "P"
            and config.strategy_kind == "WHEEL"
            and spot is not None
            and spot < leg0.strike - 1e-6
        ):
            # Synthetic Put assignment: keep option premium, open 100 * qty shares @ strike K_p.
            shares = leg0.qty * 100
            opt_gross_keep = round(t.entry.entry_cash_mid_dollars, 2)
            opt_net_keep = round(opt_gross_keep - t.open_commission, 2)
            stk_unrealized = round((spot - leg0.strike) * float(shares), 2)
            new_lot = _SyntheticStockLot(
                shares=shares,
                cost_basis=leg0.strike,
                acquired_date=bar.market_date,
                acquired_snapshot_id=bar.snapshot_id,
                cycle_credits_per_share=round(opt_net_keep / float(shares), 4),
                last_unrealized_pnl=stk_unrealized,
            )
            run_state.stock_lot = new_lot
            t.exit_reason = "PUT_ASSIGNED"
            t.exit_value_dollars = 0.0
            t.commissions_dollars = t.open_commission
            t.gross_pnl_dollars = opt_gross_keep
            t.net_pnl_dollars = opt_net_keep
            t.option_net_pnl_dollars = opt_net_keep
            t.stock_pnl_dollars = 0.0
            t.stock_shares = shares
            t.stock_cost_basis = leg0.strike
            t.flags = deduplicate_flags([*t.flags, FLAG_PUT_ASSIGNED])
            t.last_step_gross_pnl = opt_gross_keep
            t.last_step_net_pnl = opt_net_keep
            t.last_step_flags = deduplicate_flags([*t.last_step_flags, FLAG_PUT_ASSIGNED])
            if last_step is not None:
                t.steps[-1] = last_step.model_copy(
                    update={
                        "gross_pnl_dollars": opt_gross_keep,
                        "net_pnl_dollars": opt_net_keep,
                        "pnl_pct_of_basis": pct_or_none(opt_net_keep, t.entry.entry_basis_dollars),
                        "stock_shares": shares,
                        "stock_cost_basis": leg0.strike,
                        "stock_unrealized_pnl_dollars": stk_unrealized,
                        "flags": t.last_step_flags,
                    }
                )
            return opt_net_keep

        if (
            leg0.right == "C"
            and lot is not None
            and (config.strategy_kind == "WHEEL" or config.cc_itm_mode == "CALL_AWAY_AND_REBUY")
            and spot is not None
            and spot > leg0.strike + 1e-6
        ):
            # Covered Call called away: keep option credit + realize stock gain/loss at K_c.
            opt_gross_keep = round(t.entry.entry_cash_mid_dollars, 2)
            opt_net_keep = round(opt_gross_keep - t.open_commission, 2)
            stk_pnl = round((leg0.strike - lot.cost_basis) * float(lot.shares), 2)
            total_gross = round(opt_gross_keep + stk_pnl, 2)
            total_net = round(opt_net_keep + stk_pnl, 2)
            t.exit_reason = "CALLED_AWAY"
            t.exit_value_dollars = 0.0
            t.commissions_dollars = t.open_commission
            t.gross_pnl_dollars = total_gross
            t.net_pnl_dollars = total_net
            t.option_net_pnl_dollars = opt_net_keep
            t.stock_pnl_dollars = stk_pnl
            t.stock_shares = lot.shares
            t.stock_cost_basis = lot.cost_basis
            t.stock_exit_price = leg0.strike
            t.flags = deduplicate_flags([*t.flags, FLAG_CALLED_AWAY])
            t.last_step_gross_pnl = total_gross
            t.last_step_net_pnl = total_net
            t.last_step_flags = deduplicate_flags([*t.last_step_flags, FLAG_CALLED_AWAY])
            if last_step is not None:
                t.steps[-1] = last_step.model_copy(
                    update={
                        "gross_pnl_dollars": total_gross,
                        "net_pnl_dollars": total_net,
                        "pnl_pct_of_basis": pct_or_none(total_net, t.entry.entry_basis_dollars),
                        "stock_shares": 0,
                        "stock_unrealized_pnl_dollars": 0.0,
                        "flags": t.last_step_flags,
                    }
                )
            run_state.stock_lot = None
            return total_net

    if (
        reason == "STOP_LOSS"
        and lot is not None
        and config.stock_stop_mode == "LIQUIDATE_STOCK_ON_SL"
        and spot is not None
        and spot > 0.0
    ):
        stk_pnl = round((spot - lot.cost_basis) * float(lot.shares), 2)
        total_gross = round(opt_gross + stk_pnl, 2)
        total_net = round(opt_net + stk_pnl, 2)
        t.exit_reason = "STOP_LOSS"
        t.gross_pnl_dollars = total_gross
        t.net_pnl_dollars = total_net
        t.option_net_pnl_dollars = opt_net
        t.stock_pnl_dollars = stk_pnl
        t.stock_shares = lot.shares
        t.stock_cost_basis = lot.cost_basis
        t.stock_exit_price = spot
        t.flags = deduplicate_flags([*t.flags, FLAG_STOCK_LIQUIDATED_ON_SL])
        t.last_step_gross_pnl = total_gross
        t.last_step_net_pnl = total_net
        t.last_step_flags = deduplicate_flags([*t.last_step_flags, FLAG_STOCK_LIQUIDATED_ON_SL])
        if last_step is not None:
            t.steps[-1] = last_step.model_copy(
                update={
                    "gross_pnl_dollars": total_gross,
                    "net_pnl_dollars": total_net,
                    "pnl_pct_of_basis": pct_or_none(total_net, t.entry.entry_basis_dollars),
                    "stock_shares": 0,
                    "stock_unrealized_pnl_dollars": 0.0,
                    "flags": t.last_step_flags,
                }
            )
        run_state.stock_lot = None
        return total_net

    # Option-only exit (OTM expiry, TP, Exit-DTE, ROLLED, Option-Only SL, or Cash-Settle Call).
    t.exit_reason = reason
    t.gross_pnl_dollars = opt_gross
    t.net_pnl_dollars = opt_net
    t.option_net_pnl_dollars = opt_net
    if lot is not None:
        t.stock_shares = lot.shares
        t.stock_cost_basis = lot.cost_basis
        t.stock_pnl_dollars = 0.0
        lot.cycle_credits_per_share = round(
            lot.cycle_credits_per_share + (opt_net / float(lot.shares)), 4
        )
    return opt_net


def _step_open_trade(
    t: _OpenTradeState,
    bar: DailyChainBar,
    bar_ctx: BarContext,
    config: BacktestConfig,
    run_state: _SimRunState,
    *,
    fast_cache: FastBarCache | None = None,
    bar_idx: int | None = None,
    need_greeks: bool = True,
    collect_steps: bool = True,
) -> tuple[bool, float, EntryBuildResult | None]:
    """Advance one open trade by one bar; returns `(stays_open, realized_delta, rolled_entry)`."""
    step_data = evaluate_trade_step_on_bar(
        bar,
        t.entry.legs,
        config,
        ctx=bar_ctx,
        fast_cache=fast_cache,
        bar_idx=bar_idx,
        need_greeks=need_greeks,
        collect_leg_marks=collect_steps,
    )
    roll_entry, roll_flags = _try_defensive_roll(
        t, bar, bar_ctx, step_data, config, run_state.stock_lot
    )

    if roll_entry is not None:
        close_comm = round(config.commission_per_contract * float(t.total_contracts), 2)
        if collect_steps:
            stk_shares, stk_basis, stk_mtm = _stock_step_mtm(run_state.stock_lot, bar_ctx.spot)
            step, gross_pnl, net_pnl = _make_daily_step(
                bar,
                bar_ctx,
                step_data,
                entry_cash_mid_dollars=t.entry.entry_cash_mid_dollars,
                entry_basis_dollars=t.entry.entry_basis_dollars,
                commission_deducted=t.open_commission + close_comm,
                close_price_mode=config.close_price_mode,
                extra_flags=["ROLLED"],
                stock_shares=stk_shares,
                stock_cost_basis=stk_basis,
                stock_unrealized_pnl_dollars=stk_mtm,
            )
            t.steps.append(step)
            step_flags = list(step.flags)
        else:
            gross_pnl, net_pnl = _compute_step_pnls(
                step_data,
                entry_cash_mid_dollars=t.entry.entry_cash_mid_dollars,
                commission_deducted=t.open_commission + close_comm,
            )
            step_flags = deduplicate_flags([*step_data.flags, "ROLLED"])
            _record_step_diagnostics(run_state, step_flags, quote_missing=(net_pnl is None))
        t.last_step_flags = step_flags
        t.last_step_gross_pnl = gross_pnl
        t.last_step_net_pnl = net_pnl
        t.flags = deduplicate_flags([*t.flags, *step_flags])
        t.exit_date, t.exit_snapshot_id = bar.market_date, bar.snapshot_id
        t.status = "CLOSED"
        t.exit_value_dollars = step_data.active_close_value_dollars
        t.commissions_dollars = round(t.open_commission + close_comm, 2)
        assert gross_pnl is not None and net_pnl is not None
        realized_delta = _apply_wheel_or_cc_exit(
            t,
            bar,
            bar_ctx,
            step_data,
            "ROLLED",
            config,
            run_state,
            close_comm=close_comm,
            opt_gross=gross_pnl,
            opt_net=net_pnl,
        )
        return False, realized_delta, roll_entry

    exit_dec = evaluate_exit_rule(
        step_data,
        config,
        entry_cash_mid_dollars=t.entry.entry_cash_mid_dollars,
        entry_basis_dollars=t.entry.entry_basis_dollars,
        max_loss_dollars=t.entry.max_loss_dollars,
        max_loss_unbounded=t.entry.max_loss_unbounded,
    )
    close_comm = 0.0
    if exit_dec.should_exit and not exit_dec.unresolved:
        contracts = (
            step_data.unexpired_close_contracts
            if exit_dec.reason == "EXPIRY_SETTLED"
            else t.total_contracts
        )
        close_comm = round(config.commission_per_contract * float(contracts), 2)

    if collect_steps:
        stk_shares, stk_basis, stk_mtm = _stock_step_mtm(run_state.stock_lot, bar_ctx.spot)
        step, gross_pnl, net_pnl = _make_daily_step(
            bar,
            bar_ctx,
            step_data,
            entry_cash_mid_dollars=t.entry.entry_cash_mid_dollars,
            entry_basis_dollars=t.entry.entry_basis_dollars,
            commission_deducted=t.open_commission + close_comm,
            close_price_mode=config.close_price_mode,
            extra_flags=[*exit_dec.flags, *roll_flags],
            mark_unresolved=exit_dec.unresolved,
            stock_shares=stk_shares,
            stock_cost_basis=stk_basis,
            stock_unrealized_pnl_dollars=stk_mtm,
        )
        t.steps.append(step)
        step_flags = list(step.flags)
    else:
        gross_pnl, net_pnl = _compute_step_pnls(
            step_data,
            entry_cash_mid_dollars=t.entry.entry_cash_mid_dollars,
            commission_deducted=t.open_commission + close_comm,
            mark_unresolved=exit_dec.unresolved,
        )
        step_flags = deduplicate_flags([*step_data.flags, *exit_dec.flags, *roll_flags])
        _record_step_diagnostics(run_state, step_flags, quote_missing=(net_pnl is None))
    t.last_step_flags = step_flags
    t.last_step_gross_pnl = gross_pnl
    t.last_step_net_pnl = net_pnl
    t.flags = deduplicate_flags([*t.flags, *step_flags])
    run_state.notes.extend(exit_dec.notes)

    if not exit_dec.should_exit:
        if net_pnl is not None:
            t.last_known_unrealized_net = net_pnl
        return True, 0.0, None

    assert exit_dec.reason is not None
    t.exit_reason, t.exit_date, t.exit_snapshot_id = (
        exit_dec.reason,
        bar.market_date,
        bar.snapshot_id,
    )
    if exit_dec.unresolved or net_pnl is None or gross_pnl is None:
        t.status, t.commissions_dollars = "UNRESOLVED", t.open_commission
        t.exit_value_dollars = t.gross_pnl_dollars = t.net_pnl_dollars = None
        return False, -t.open_commission, None

    t.status = "CLOSED"
    t.exit_value_dollars = step_data.active_close_value_dollars
    t.commissions_dollars = round(t.open_commission + close_comm, 2)
    realized_delta = _apply_wheel_or_cc_exit(
        t,
        bar,
        bar_ctx,
        step_data,
        exit_dec.reason,
        config,
        run_state,
        close_comm=close_comm,
        opt_gross=gross_pnl,
        opt_net=net_pnl,
    )
    return False, realized_delta, None


def _finalize(
    trade_state: _OpenTradeState,
    last_bar: DailyChainBar,
    config: BacktestConfig,
    *,
    open_stock_lot: _SyntheticStockLot | None = None,
) -> BacktestTradeRecord:
    attach_stk_mtm: float | None = None
    stk_shares = trade_state.stock_shares
    stk_basis = trade_state.stock_cost_basis
    if trade_state.status == "OPEN" and open_stock_lot is not None:
        attach_stk_mtm = open_stock_lot.last_unrealized_pnl
        stk_shares = open_stock_lot.shares
        stk_basis = open_stock_lot.cost_basis
    return finalize_trade_record(
        trade_id=trade_state.trade_id,
        entry_date=trade_state.entry_date,
        entry_snapshot_id=trade_state.entry_snapshot_id,
        strategy_kind=trade_state.strategy_kind,
        wheel_phase=trade_state.wheel_phase,
        stock_shares=stk_shares,
        stock_cost_basis=stk_basis,
        stock_exit_price=trade_state.stock_exit_price,
        option_net_pnl_dollars=trade_state.option_net_pnl_dollars,
        stock_pnl_dollars=trade_state.stock_pnl_dollars,
        open_stock_unrealized_pnl=attach_stk_mtm,
        qty=trade_state.qty,
        config=config,
        entry=trade_state.entry,
        open_commission=trade_state.open_commission,
        steps=trade_state.steps,
        flags=trade_state.flags,
        status=trade_state.status,
        exit_reason=trade_state.exit_reason,
        exit_date=trade_state.exit_date,
        exit_snapshot_id=trade_state.exit_snapshot_id,
        exit_value_dollars=trade_state.exit_value_dollars,
        commissions_dollars=trade_state.commissions_dollars,
        gross_pnl_dollars=trade_state.gross_pnl_dollars,
        net_pnl_dollars=trade_state.net_pnl_dollars,
        last_bar=last_bar,
        open_opt_gross=trade_state.last_step_gross_pnl,
        open_opt_net=trade_state.last_step_net_pnl,
    )


@dataclass
class _SimRunState:
    peak_equity: float
    all_trades: list[_OpenTradeState] = field(default_factory=list)
    active_open: list[_OpenTradeState] = field(default_factory=list)
    stock_lot: _SyntheticStockLot | None = None
    equity_curve: list[EquityCurvePoint] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    cadence: CadenceState = field(default_factory=CadenceState)
    realized_pnl: float = 0.0
    ok_bars: int = 0
    flagged_bars: int = 0
    unusable_bars: int = 0
    trade_seq: int = 0
    missing_quote_steps: int = 0
    fallback_quote_steps: int = 0
    capital_depleted: bool = False


def _as_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)


def _prepare_anchor_bars(
    ordered_bars: Sequence[DailyChainBar],
    anchor: BrokerFillAnchor,
) -> tuple[list[DailyChainBar], datetime, list[str]]:
    """Filter bars to on/after `anchor.entry_ts_utc`, or keep latest same/later-day bar."""
    try:
        entry_day = date.fromisoformat(anchor.entry_date)
    except ValueError as exc:
        raise BacktestValidationError(f"Invalid entry_date {anchor.entry_date!r}: {exc}") from exc
    anchor_ts = parse_anchor_ts_utc(anchor.entry_ts_utc)
    after_anchor = [b for b in ordered_bars if _as_utc(b.ts_utc) >= anchor_ts]
    if after_anchor:
        return after_anchor, anchor_ts, []

    last_bar = ordered_bars[-1]
    if date.fromisoformat(last_bar.market_date) < entry_day:
        raise BacktestValidationError(
            f"All provided daily bars precede broker fill entry_date {anchor.entry_date}."
        )
    fallback_ts = _as_utc(last_bar.ts_utc)
    aligned_ts = min(anchor_ts, fallback_ts)
    note = (
        f"Only prior-timestamp snapshot {last_bar.snapshot_id} "
        f"({fallback_ts.strftime('%Y-%m-%dT%H:%M:%SZ')}) was available on/after "
        f"{anchor.entry_date}; Day-0 anchor step timestamp aligned for chronological order."
    )
    return [last_bar], aligned_ts, [note]


def _build_anchor_bar_and_context(
    anchor: BrokerFillAnchor,
    first_bar: DailyChainBar,
    *,
    anchor_ts_override: datetime | None = None,
) -> tuple[DailyChainBar, BarContext]:
    anchor_ts = (
        anchor_ts_override
        if anchor_ts_override is not None
        else parse_anchor_ts_utc(anchor.entry_ts_utc)
    )
    anchor_bar = DailyChainBar(
        snapshot_id=anchor.entry_snapshot_id,
        ts_utc=anchor_ts,
        market_date=anchor.entry_date,
        df=pl.DataFrame(schema=OPTION_CHAIN_POLARS_SCHEMA),
        meta=None,
    )
    first_ctx = extract_bar_context(first_bar)
    same_day = first_bar.market_date == anchor.entry_date
    anchor_spot = first_ctx.spot
    if (
        same_day
        and anchor_spot is None
        and first_bar.tracked_df is not None
        and first_bar.tracked_df.height > 0
    ):
        anchor_spot = clean_positive_price(first_bar.tracked_df["underlying_price"][0])
    anchor_ctx = BarContext(
        spot=anchor_spot if same_day else None,
        spot_source=first_ctx.spot_source if same_day else None,
        market_data_type=first_ctx.market_data_type if same_day else None,
        quote_coverage=first_ctx.quote_coverage if same_day else None,
        flags=(),
        bar_reliable=True,
    )
    return anchor_bar, anchor_ctx


def _emit_equity_point(
    state: _SimRunState,
    bar: DailyChainBar,
    spot: float | None,
    bar_flags: list[str],
    config: BacktestConfig,
) -> None:
    opt_unrealized = sum(
        (t.last_step_net_pnl if t.last_step_net_pnl is not None else t.last_known_unrealized_net)
        for t in state.active_open
    )
    stk_unrealized = 0.0
    if state.stock_lot is not None:
        if spot is not None and spot > 0.0 and FLAG_NO_USABLE_DAILY_SNAPSHOT not in bar_flags:
            stk_unrealized = round(
                (spot - state.stock_lot.cost_basis) * float(state.stock_lot.shares), 2
            )
            state.stock_lot.last_unrealized_pnl = stk_unrealized
        else:
            stk_unrealized = state.stock_lot.last_unrealized_pnl
    unrealized = round(opt_unrealized + stk_unrealized, 2)
    if any(t.status == "UNRESOLVED" for t in state.all_trades):
        bar_flags.append("UNRESOLVED_TRADE")

    equity = round(config.initial_capital + state.realized_pnl + unrealized, 2)
    state.peak_equity = max(state.peak_equity, equity)
    dd_dollars = round(max(0.0, state.peak_equity - equity), 2)
    capped_eq = max(0.0, equity)
    dd_pct = (
        round(min(100.0, ((state.peak_equity - capped_eq) / state.peak_equity) * 100.0), 2)
        if state.peak_equity > DENOM_EPS
        else 0.0
    )
    if equity <= 0.0:
        bar_flags.append(FLAG_CAPITAL_DEPLETED)
        state.capital_depleted = True

    deduped_bar_flags = deduplicate_flags(bar_flags)
    if deduped_bar_flags:
        state.flagged_bars += 1
    else:
        state.ok_bars += 1

    state.equity_curve.append(
        EquityCurvePoint(
            market_date=bar.market_date,
            snapshot_id=bar.snapshot_id,
            ts_utc=bar.ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            equity_dollars=equity,
            realized_pnl_dollars=round(state.realized_pnl, 2),
            unrealized_pnl_dollars=round(unrealized, 2),
            drawdown_dollars=dd_dollars,
            drawdown_pct=dd_pct,
            open_trades=len(state.active_open),
            underlying_price=spot,
            flags=deduped_bar_flags,
        )
    )


def _simulate_bars(
    ordered_bars: Sequence[DailyChainBar],
    config: BacktestConfig,
    *,
    ticker: str,
    mode: BacktestMode,
    entry_legs: Sequence[StrategyLegInput] | None = None,
    broker_fill_anchor: BrokerFillAnchor | None = None,
    trade_id: str | None = None,
    fast_cache: FastBarCache | None = None,
    need_greeks: bool = True,
    collect_steps: bool = True,
) -> BacktestSimulationResult:
    if not ordered_bars:
        return BacktestSimulationResult(
            ticker=ticker,
            mode=mode,
            config=config,
            summary=build_portfolio_summary(config, [], [], 0, 0),
            notes=["No daily bars provided for backtest."],
            assumptions=list(BACKTEST_ASSUMPTIONS),
        )

    state = _SimRunState(peak_equity=config.initial_capital)
    active_bars: Sequence[DailyChainBar] = ordered_bars

    if broker_fill_anchor is not None:
        active_bars, anchor_ts_eff, anchor_notes = _prepare_anchor_bars(
            ordered_bars, broker_fill_anchor
        )
        state.notes.extend(anchor_notes)
        anchor_entry = build_broker_fill_entry(
            entry_legs or (),
            config,
            broker_fill_anchor,
            reference_bar=active_bars[0],
        )
        anchor_bar, anchor_ctx = _build_anchor_bar_and_context(
            broker_fill_anchor,
            active_bars[0],
            anchor_ts_override=anchor_ts_eff,
        )
        state.trade_seq += 1
        resolved_tid = trade_id or f"{ticker}_BT_{state.trade_seq:04d}"
        t_state = _start_trade_state(
            resolved_tid,
            anchor_bar,
            anchor_ctx,
            anchor_entry,
            config,
            collect_steps=collect_steps,
            run_state=state,
        )
        state.all_trades.append(t_state)
        state.active_open.append(t_state)
        _emit_equity_point(
            state,
            anchor_bar,
            anchor_ctx.spot,
            [*anchor_ctx.flags, *t_state.flags],
            config,
        )

    for bar_idx, bar in enumerate(active_bars):
        bar_ctx = (
            fast_cache.entries[bar_idx].ctx
            if (fast_cache is not None and broker_fill_anchor is None)
            else extract_bar_context(bar)
        )
        bar_flags: list[str] = list(bar_ctx.flags)
        if FLAG_NO_USABLE_DAILY_SNAPSHOT in bar_ctx.flags:
            state.unusable_bars += 1

        still_open: list[_OpenTradeState] = []
        for t in state.active_open:
            stays_open, realized_delta, rolled_entry = _step_open_trade(
                t,
                bar,
                bar_ctx,
                config,
                state,
                fast_cache=fast_cache if broker_fill_anchor is None else None,
                bar_idx=bar_idx if broker_fill_anchor is None else None,
                need_greeks=need_greeks,
                collect_steps=collect_steps,
            )
            state.realized_pnl += realized_delta
            bar_flags.extend(t.last_step_flags)
            if t.status == "UNRESOLVED":
                bar_flags.append("UNRESOLVED_TRADE")
                state.notes.append(
                    f"{t.trade_id} kept UNRESOLVED on {bar.market_date}: "
                    "missing settlement spot/leg."
                )
            if stays_open:
                still_open.append(t)
            elif rolled_entry is not None:
                if mode == "SYSTEMATIC":
                    record_entry_attempt(bar, bar_idx, state.cadence)
                state.trade_seq += 1
                rolled_tid = f"{ticker}_BT_{state.trade_seq:04d}"
                t_rolled = _start_trade_state(
                    rolled_tid,
                    bar,
                    bar_ctx,
                    rolled_entry,
                    config,
                    wheel_phase=t.wheel_phase,
                    stock_lot=state.stock_lot,
                    collect_steps=collect_steps,
                    run_state=state,
                )
                state.all_trades.append(t_rolled)
                still_open.append(t_rolled)
                bar_flags.extend(t_rolled.flags)
        state.active_open = still_open

        is_wheel_or_cc = config.strategy_kind in ("COVERED_CALL", "WHEEL")
        want_entry = (
            (bar_idx == 0 and broker_fill_anchor is None)
            if mode == "SINGLE_TRADE"
            else (
                False
                if (is_wheel_or_cc and state.active_open)
                else should_attempt_entry(
                    bar, bar_idx, config, len(state.active_open), state.cadence
                )
            )
        )
        if want_entry:
            entry_wheel_phase: WheelPhase | None = None
            if config.strategy_kind == "COVERED_CALL":
                entry_wheel_phase = "CC"
            elif config.strategy_kind == "WHEEL":
                entry_wheel_phase = "CC" if state.stock_lot is not None else "CSP"

            entry_res, _entry_notes, skip_reason = build_entry_candidate_outcome(
                bar,
                config,
                legs=entry_legs,
                raise_on_error=(mode == "SINGLE_TRADE"),
                ctx=bar_ctx,
                wheel_phase=entry_wheel_phase,
                stock_cost_basis=(
                    state.stock_lot.cost_basis if state.stock_lot is not None else bar_ctx.spot
                ),
                cycle_credits_per_share=(
                    state.stock_lot.cycle_credits_per_share if state.stock_lot is not None else 0.0
                ),
                fast_cache=fast_cache if broker_fill_anchor is None else None,
                bar_idx=bar_idx if broker_fill_anchor is None else None,
            )
            if entry_res is not None:
                if (
                    config.strategy_kind == "COVERED_CALL"
                    and state.stock_lot is None
                    and bar_ctx.spot is not None
                    and bar_ctx.spot > 0.0
                ):
                    shares = 100 * config.qty
                    open_comm_est = round(
                        config.commission_per_contract
                        * float(sum(leg.qty for leg in entry_res.legs)),
                        2,
                    )
                    state.stock_lot = _SyntheticStockLot(
                        shares=shares,
                        cost_basis=bar_ctx.spot,
                        acquired_date=bar.market_date,
                        acquired_snapshot_id=bar.snapshot_id,
                        cycle_credits_per_share=round(
                            (entry_res.entry_cash_mid_dollars - open_comm_est) / float(shares), 4
                        ),
                        last_unrealized_pnl=0.0,
                    )
                    entry_res = EntryBuildResult(
                        legs=entry_res.legs,
                        entry_cash_mid_dollars=entry_res.entry_cash_mid_dollars,
                        entry_cash_natural_dollars=entry_res.entry_cash_natural_dollars,
                        entry_basis_dollars=entry_res.entry_basis_dollars,
                        max_profit_dollars=entry_res.max_profit_dollars,
                        max_loss_dollars=entry_res.max_loss_dollars,
                        max_loss_unbounded=entry_res.max_loss_unbounded,
                        initial_step=entry_res.initial_step,
                        selection_notes=deduplicate_flags(
                            [
                                f"Acquired {shares} synthetic shares @ ${bar_ctx.spot:.2f}.",
                                *entry_res.selection_notes,
                            ]
                        ),
                        flags=deduplicate_flags([FLAG_SYNTHETIC_STOCK_ENTRY, *entry_res.flags]),
                    )
                if mode == "SYSTEMATIC":
                    record_entry_attempt(bar, bar_idx, state.cadence)
                state.trade_seq += 1
                resolved_tid = (
                    trade_id
                    if (mode == "SINGLE_TRADE" and trade_id)
                    else f"{ticker}_BT_{state.trade_seq:04d}"
                )
                t_state = _start_trade_state(
                    resolved_tid,
                    bar,
                    bar_ctx,
                    entry_res,
                    config,
                    wheel_phase=entry_wheel_phase,
                    stock_lot=state.stock_lot,
                    collect_steps=collect_steps,
                    run_state=state,
                )
                state.all_trades.append(t_state)
                state.active_open.append(t_state)
                bar_flags.extend(t_state.flags)
            else:
                reason_key = skip_reason or "ENTRY_SKIPPED"
                state.skipped_by_reason[reason_key] = state.skipped_by_reason.get(reason_key, 0) + 1
                bar_flags.append("ENTRY_SKIPPED")

        _emit_equity_point(state, bar, bar_ctx.spot, bar_flags, config)

    if state.unusable_bars > 0:
        state.notes.append(
            f"{state.unusable_bars} daily bar(s) had no usable snapshot (NO_USABLE_DAILY_SNAPSHOT)."
        )

    if collect_steps:
        missing_quote_steps = sum(
            1
            for t in state.all_trades
            for s in t.steps
            if s.quote_missing
            and FLAG_UNRESOLVED_EXPIRY_SPOT not in s.flags
            and FLAG_UNRESOLVED_EXPIRY_LEG not in s.flags
        )
        fallback_quote_steps = sum(
            1
            for t in state.all_trades
            for s in t.steps
            if not s.quote_missing
            and (FLAG_LEG_FALLBACK_PRICE in s.flags or FLAG_TRACKED_LEG_USED in s.flags)
        )
    else:
        missing_quote_steps = state.missing_quote_steps
        fallback_quote_steps = state.fallback_quote_steps
    if missing_quote_steps > 0:
        state.notes.append(
            f"{missing_quote_steps} trade daily step(s) had missing leg quotes "
            "(MTM omitted; prior unrealized P&L carried forward on equity curve)."
        )

    if fallback_quote_steps > 0:
        state.notes.append(
            f"{fallback_quote_steps} trade daily step(s) used fallback or tracked-leg quotes."
        )

    if state.skipped_by_reason:
        skip_parts = ", ".join(f"{k}={v}" for k, v in sorted(state.skipped_by_reason.items()))
        state.notes.append(
            f"Skipped {sum(state.skipped_by_reason.values())} entry attempt(s): {skip_parts}."
        )
    elif not state.all_trades:
        state.notes.append("No trades opened during the simulation window.")

    if state.stock_lot is not None:
        state.notes.append(
            f"End-of-window synthetic stock inventory open: {state.stock_lot.shares} shs @ "
            f"${state.stock_lot.cost_basis:.2f} (MTM ${state.stock_lot.last_unrealized_pnl:+.2f})."
        )

    if state.capital_depleted:
        state.notes.append(
            "Portfolio equity dropped to <= $0.00 (CAPITAL_DEPLETED: position notional losses "
            "exceeded initial_capital; percentage drawdown capped at 100%)."
        )

    final_trades = [
        _finalize(t, active_bars[-1], config, open_stock_lot=state.stock_lot)
        for t in state.all_trades
    ]
    summary = build_portfolio_summary(
        config,
        final_trades,
        state.equity_curve,
        state.ok_bars,
        state.flagged_bars,
        skipped_by_reason=state.skipped_by_reason,
    )
    return BacktestSimulationResult(
        ticker=ticker,
        mode=mode,
        config=config,
        summary=summary,
        equity_curve=state.equity_curve,
        trades=final_trades,
        notes=deduplicate_flags(state.notes),
        assumptions=list(BACKTEST_ASSUMPTIONS),
    )


def run_systematic_backtest(
    bars: Sequence[DailyChainBar],
    config: BacktestConfig,
    *,
    ticker: str,
    legs: Sequence[StrategyLegInput] | None = None,
    fast_cache: FastBarCache | None = None,
    need_greeks: bool = True,
    collect_steps: bool = True,
) -> BacktestSimulationResult:
    """Run a pure, chronological systematic options backtest over `bars`."""
    symbol = TickerValidator.validate(ticker)
    if config.strategy_kind == "CUSTOM" and legs is None:
        raise BacktestValidationError("CUSTOM strategy requires explicit legs.")
    if fast_cache is None:
        ordered = sorted(bars, key=lambda b: (b.ts_utc, b.snapshot_id))
        cache = FastBarCache.from_bars(ordered)
    else:
        cache = fast_cache
        ordered = list(cache.bars)
    return _simulate_bars(
        ordered,
        config,
        ticker=symbol,
        mode="SYSTEMATIC",
        entry_legs=legs,
        fast_cache=cache,
        need_greeks=need_greeks,
        collect_steps=collect_steps,
    )


def run_single_trade_replay(
    bars: Sequence[DailyChainBar],
    config: BacktestConfig,
    *,
    ticker: str,
    entry_snapshot_id: str | None = None,
    legs: Sequence[StrategyLegInput] | None = None,
    end_snapshot_id: str | None = None,
    broker_fill_anchor: BrokerFillAnchor | None = None,
    trade_id: str | None = None,
) -> BacktestSimulationResult:
    """Replay a single trade from `entry_snapshot_id` or `broker_fill_anchor` through exit."""
    symbol = TickerValidator.validate(ticker)
    ordered = sorted(bars, key=lambda b: (b.ts_utc, b.snapshot_id))

    if broker_fill_anchor is not None:
        if not ordered:
            raise BacktestValidationError(
                "At least one daily bar is required when broker_fill_anchor is provided."
            )
        if end_snapshot_id is not None:
            end_idx = next(
                (idx for idx, b in enumerate(ordered) if b.snapshot_id == end_snapshot_id), None
            )
            if end_idx is None:
                raise BacktestValidationError(
                    f"end_snapshot_id {end_snapshot_id!r} not found in provided bars."
                )
            window_bars = ordered[: end_idx + 1]
        else:
            window_bars = ordered
        return _simulate_bars(
            window_bars,
            config,
            ticker=symbol,
            mode="SINGLE_TRADE",
            entry_legs=legs,
            broker_fill_anchor=broker_fill_anchor,
            trade_id=trade_id,
        )

    if entry_snapshot_id is None:
        raise BacktestValidationError(
            "entry_snapshot_id is required when broker_fill_anchor is not provided."
        )
    entry_idx = next(
        (idx for idx, b in enumerate(ordered) if b.snapshot_id == entry_snapshot_id), None
    )
    if entry_idx is None:
        raise BacktestValidationError(
            f"entry_snapshot_id {entry_snapshot_id!r} not found in provided bars."
        )

    if end_snapshot_id is not None:
        end_idx = next(
            (idx for idx, b in enumerate(ordered) if b.snapshot_id == end_snapshot_id), None
        )
        if end_idx is None:
            raise BacktestValidationError(
                f"end_snapshot_id {end_snapshot_id!r} not found in provided bars."
            )
        if end_idx < entry_idx:
            raise BacktestValidationError(
                f"end_snapshot_id {end_snapshot_id!r} precedes entry {entry_snapshot_id!r}."
            )
        window_bars = ordered[entry_idx : end_idx + 1]
    else:
        window_bars = ordered[entry_idx:]

    return _simulate_bars(
        window_bars,
        config,
        ticker=symbol,
        mode="SINGLE_TRADE",
        entry_legs=legs,
        trade_id=trade_id,
    )
