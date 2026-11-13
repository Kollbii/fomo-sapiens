"""Strict chronological backtest engine for systematic runs and single-trade replays."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from src.analytics.backtest.context import BarContext, extract_bar_context
from src.analytics.backtest.metrics import (
    build_portfolio_summary,
    finalize_trade_record,
    pct_or_none,
)
from src.analytics.backtest.models import (
    BacktestConfig,
    BacktestMode,
    BacktestSimulationResult,
    BacktestTradeRecord,
    EquityCurvePoint,
    ExitReason,
    TradeDailyStep,
    TradeStatus,
    deduplicate_flags,
)
from src.analytics.backtest.pricing import (
    EvaluatedStepData,
    evaluate_trade_step_on_bar,
)
from src.analytics.backtest.rules import (
    DENOM_EPS,
    CadenceState,
    EntryBuildResult,
    build_entry_candidate,
    evaluate_exit_rule,
    record_entry_attempt,
    should_attempt_entry,
)
from src.analytics.strategies.models import StrategyKind, StrategyLegInput
from src.core.calendar import fmt_et
from src.core.exceptions import BacktestValidationError
from src.core.security import TickerValidator
from src.storage.backtest_store import DailyChainBar

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
)


@dataclass
class _OpenTradeState:
    trade_id: str
    entry_date: str
    entry_snapshot_id: str
    strategy_kind: StrategyKind
    qty: int
    entry: EntryBuildResult
    open_commission: float
    total_contracts: int
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


def _make_daily_step(
    bar: DailyChainBar,
    bar_ctx: BarContext,
    step_data: EvaluatedStepData,
    *,
    entry_cash_mid_dollars: float,
    entry_basis_dollars: float,
    commission_deducted: float,
    extra_flags: Sequence[str] = (),
    mark_unresolved: bool = False,
) -> tuple[TradeDailyStep, float | None, float | None]:
    if step_data.active_close_value_dollars is not None and not mark_unresolved:
        gross_val = round(entry_cash_mid_dollars + step_data.active_close_value_dollars, 2)
        net_val = round(gross_val - commission_deducted, 2)
        gross_pnl: float | None = gross_val
        net_pnl: float | None = net_val
    else:
        gross_pnl = net_pnl = None

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
        status=step_data.status,
        flags=deduplicate_flags([*step_data.flags, *extra_flags]),
        tracked_snapshot_ids=step_data.tracked_snapshot_ids,
    )
    return step, gross_pnl, net_pnl


def _start_trade_state(
    trade_id: str,
    bar: DailyChainBar,
    bar_ctx: BarContext,
    entry: EntryBuildResult,
    config: BacktestConfig,
) -> _OpenTradeState:
    total_contracts = sum(leg.qty for leg in entry.legs)
    open_comm = round(config.commission_per_contract * float(total_contracts), 2)
    step0, gross0, net0 = _make_daily_step(
        bar,
        bar_ctx,
        entry.initial_step,
        entry_cash_mid_dollars=entry.entry_cash_mid_dollars,
        entry_basis_dollars=entry.entry_basis_dollars,
        commission_deducted=open_comm,
        extra_flags=entry.flags,
    )
    return _OpenTradeState(
        trade_id=trade_id,
        entry_date=bar.market_date,
        entry_snapshot_id=bar.snapshot_id,
        strategy_kind=config.strategy_kind,
        qty=config.qty,
        entry=entry,
        open_commission=open_comm,
        total_contracts=total_contracts,
        steps=[step0],
        flags=list(entry.flags),
        commissions_dollars=open_comm,
        gross_pnl_dollars=gross0,
        net_pnl_dollars=net0,
        last_known_unrealized_net=net0 if net0 is not None else -open_comm,
    )


def _step_open_trade(
    t: _OpenTradeState,
    bar: DailyChainBar,
    bar_ctx: BarContext,
    config: BacktestConfig,
    notes: list[str],
) -> tuple[bool, float]:
    """Advance one open trade by one bar; returns `(stays_open, realized_delta)`."""
    step_data = evaluate_trade_step_on_bar(bar, t.entry.legs, config, ctx=bar_ctx)
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

    step, gross_pnl, net_pnl = _make_daily_step(
        bar,
        bar_ctx,
        step_data,
        entry_cash_mid_dollars=t.entry.entry_cash_mid_dollars,
        entry_basis_dollars=t.entry.entry_basis_dollars,
        commission_deducted=t.open_commission + close_comm,
        extra_flags=exit_dec.flags,
        mark_unresolved=exit_dec.unresolved,
    )
    t.steps.append(step)
    t.flags = deduplicate_flags([*t.flags, *step.flags])
    notes.extend(exit_dec.notes)

    if not exit_dec.should_exit:
        if net_pnl is not None:
            t.last_known_unrealized_net = net_pnl
        return True, 0.0

    assert exit_dec.reason is not None
    t.exit_reason, t.exit_date, t.exit_snapshot_id = (
        exit_dec.reason,
        bar.market_date,
        bar.snapshot_id,
    )
    if exit_dec.unresolved or net_pnl is None:
        t.status, t.commissions_dollars = "UNRESOLVED", t.open_commission
        t.exit_value_dollars = t.gross_pnl_dollars = t.net_pnl_dollars = None
        return False, -t.open_commission

    t.status = "CLOSED"
    t.exit_value_dollars = step_data.active_close_value_dollars
    t.commissions_dollars = round(t.open_commission + close_comm, 2)
    t.gross_pnl_dollars, t.net_pnl_dollars = gross_pnl, net_pnl
    return False, net_pnl


def _finalize(state: _OpenTradeState, last_bar: DailyChainBar) -> BacktestTradeRecord:
    return finalize_trade_record(
        trade_id=state.trade_id,
        entry_date=state.entry_date,
        entry_snapshot_id=state.entry_snapshot_id,
        strategy_kind=state.strategy_kind,
        qty=state.qty,
        entry=state.entry,
        open_commission=state.open_commission,
        steps=state.steps,
        flags=state.flags,
        status=state.status,
        exit_reason=state.exit_reason,
        exit_date=state.exit_date,
        exit_snapshot_id=state.exit_snapshot_id,
        exit_value_dollars=state.exit_value_dollars,
        commissions_dollars=state.commissions_dollars,
        gross_pnl_dollars=state.gross_pnl_dollars,
        net_pnl_dollars=state.net_pnl_dollars,
        last_bar=last_bar,
    )


def _simulate_bars(
    ordered_bars: Sequence[DailyChainBar],
    config: BacktestConfig,
    *,
    ticker: str,
    mode: BacktestMode,
    entry_legs: Sequence[StrategyLegInput] | None = None,
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

    all_trades: list[_OpenTradeState] = []
    active_open: list[_OpenTradeState] = []
    equity_curve: list[EquityCurvePoint] = []
    notes: list[str] = []
    cadence = CadenceState()
    realized_pnl, peak_equity = 0.0, config.initial_capital
    ok_bars = flagged_bars = trade_seq = 0

    for bar_idx, bar in enumerate(ordered_bars):
        bar_ctx = extract_bar_context(bar)
        bar_flags: list[str] = list(bar_ctx.flags)

        still_open: list[_OpenTradeState] = []
        for t in active_open:
            stays_open, realized_delta = _step_open_trade(t, bar, bar_ctx, config, notes)
            realized_pnl += realized_delta
            bar_flags.extend(t.steps[-1].flags)
            if t.status == "UNRESOLVED":
                bar_flags.append("UNRESOLVED_TRADE")
                notes.append(
                    f"{t.trade_id} kept UNRESOLVED on {bar.market_date}: "
                    "missing settlement spot/leg."
                )
            if stays_open:
                still_open.append(t)
        active_open = still_open

        want_entry = (
            bar_idx == 0
            if mode == "SINGLE_TRADE"
            else should_attempt_entry(bar, bar_idx, config, len(active_open), cadence)
        )
        if want_entry:
            if mode == "SYSTEMATIC":
                record_entry_attempt(bar, bar_idx, cadence)
            entry_res, entry_notes = build_entry_candidate(
                bar, config, legs=entry_legs, raise_on_error=(mode == "SINGLE_TRADE"), ctx=bar_ctx
            )
            notes.extend(entry_notes)
            if entry_res is not None:
                trade_seq += 1
                t_state = _start_trade_state(
                    f"{ticker}_BT_{trade_seq:04d}", bar, bar_ctx, entry_res, config
                )
                all_trades.append(t_state)
                active_open.append(t_state)
                bar_flags.extend(t_state.flags)
                notes.extend(entry_res.notes)
            elif entry_notes:
                bar_flags.append("ENTRY_SKIPPED")

        unrealized = sum(
            (
                t.steps[-1].net_pnl_dollars
                if t.steps[-1].net_pnl_dollars is not None
                else t.last_known_unrealized_net
            )
            for t in active_open
        )
        if any(t.status == "UNRESOLVED" for t in all_trades):
            bar_flags.append("UNRESOLVED_TRADE")

        equity = round(config.initial_capital + realized_pnl + unrealized, 2)
        peak_equity = max(peak_equity, equity)
        dd_dollars = round(max(0.0, peak_equity - equity), 2)
        dd_pct = round((dd_dollars / peak_equity) * 100.0, 2) if peak_equity > DENOM_EPS else 0.0

        deduped_bar_flags = deduplicate_flags(bar_flags)
        if deduped_bar_flags:
            flagged_bars += 1
        else:
            ok_bars += 1

        equity_curve.append(
            EquityCurvePoint(
                market_date=bar.market_date,
                snapshot_id=bar.snapshot_id,
                ts_utc=bar.ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                equity_dollars=equity,
                realized_pnl_dollars=round(realized_pnl, 2),
                unrealized_pnl_dollars=round(unrealized, 2),
                drawdown_dollars=dd_dollars,
                drawdown_pct=dd_pct,
                open_trades=len(active_open),
                underlying_price=bar_ctx.spot,
                flags=deduped_bar_flags,
            )
        )

    final_trades = [_finalize(t, ordered_bars[-1]) for t in all_trades]
    summary = build_portfolio_summary(config, final_trades, equity_curve, ok_bars, flagged_bars)
    return BacktestSimulationResult(
        ticker=ticker,
        mode=mode,
        config=config,
        summary=summary,
        equity_curve=equity_curve,
        trades=final_trades,
        notes=deduplicate_flags(notes),
        assumptions=list(BACKTEST_ASSUMPTIONS),
    )


def run_systematic_backtest(
    bars: Sequence[DailyChainBar],
    config: BacktestConfig,
    *,
    ticker: str,
    legs: Sequence[StrategyLegInput] | None = None,
) -> BacktestSimulationResult:
    """Run a pure, chronological systematic options backtest over `bars`."""
    symbol = TickerValidator.validate(ticker)
    if config.strategy_kind == "CUSTOM" and legs is None:
        raise BacktestValidationError("CUSTOM strategy requires explicit legs.")
    ordered = sorted(bars, key=lambda b: (b.ts_utc, b.snapshot_id))
    return _simulate_bars(ordered, config, ticker=symbol, mode="SYSTEMATIC", entry_legs=legs)


def run_single_trade_replay(
    bars: Sequence[DailyChainBar],
    config: BacktestConfig,
    *,
    ticker: str,
    entry_snapshot_id: str,
    legs: Sequence[StrategyLegInput] | None = None,
    end_snapshot_id: str | None = None,
) -> BacktestSimulationResult:
    """Replay a single trade from `entry_snapshot_id` through exit or `end_snapshot_id`."""
    symbol = TickerValidator.validate(ticker)
    ordered = sorted(bars, key=lambda b: (b.ts_utc, b.snapshot_id))
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

    return _simulate_bars(window_bars, config, ticker=symbol, mode="SINGLE_TRADE", entry_legs=legs)
