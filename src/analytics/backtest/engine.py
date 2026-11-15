"""Strict chronological backtest engine for systematic runs and single-trade replays."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

import polars as pl

from src.analytics.backtest.context import BarContext, clean_positive_price, extract_bar_context
from src.analytics.backtest.metrics import (
    build_portfolio_summary,
    finalize_trade_record,
    pct_or_none,
)
from src.analytics.backtest.models import (
    FLAG_BROKER_FILL_ENTRY,
    FLAG_LEG_FALLBACK_PRICE,
    FLAG_NO_USABLE_DAILY_SNAPSHOT,
    FLAG_TRACKED_LEG_USED,
    FLAG_UNRESOLVED_EXPIRY_LEG,
    FLAG_UNRESOLVED_EXPIRY_SPOT,
    BacktestConfig,
    BacktestMode,
    BacktestSimulationResult,
    BacktestTradeRecord,
    ClosePriceMode,
    EquityCurvePoint,
    ExitReason,
    LegPriceSource,
    StepMarkSource,
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
    BrokerFillAnchor,
    CadenceState,
    EntryBuildResult,
    build_broker_fill_entry,
    build_entry_candidate_outcome,
    evaluate_exit_rule,
    parse_anchor_ts_utc,
    record_entry_attempt,
    should_attempt_entry,
)
from src.analytics.strategies.models import StrategyKind, StrategyLegInput
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
) -> tuple[TradeDailyStep, float | None, float | None]:
    if step_data.active_close_value_dollars is not None and not mark_unresolved:
        gross_val = round(entry_cash_mid_dollars + step_data.active_close_value_dollars, 2)
        net_val = round(gross_val - commission_deducted, 2)
        gross_pnl: float | None = gross_val
        net_pnl: float | None = net_val
    else:
        gross_pnl = net_pnl = None

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
        close_price_mode=config.close_price_mode,
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
        close_price_mode=config.close_price_mode,
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


def _finalize(
    state: _OpenTradeState, last_bar: DailyChainBar, config: BacktestConfig
) -> BacktestTradeRecord:
    return finalize_trade_record(
        trade_id=state.trade_id,
        entry_date=state.entry_date,
        entry_snapshot_id=state.entry_snapshot_id,
        strategy_kind=state.strategy_kind,
        qty=state.qty,
        config=config,
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


@dataclass
class _SimRunState:
    peak_equity: float
    all_trades: list[_OpenTradeState] = field(default_factory=list)
    active_open: list[_OpenTradeState] = field(default_factory=list)
    equity_curve: list[EquityCurvePoint] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    cadence: CadenceState = field(default_factory=CadenceState)
    realized_pnl: float = 0.0
    ok_bars: int = 0
    flagged_bars: int = 0
    unusable_bars: int = 0
    trade_seq: int = 0


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
    unrealized = sum(
        (
            t.steps[-1].net_pnl_dollars
            if t.steps[-1].net_pnl_dollars is not None
            else t.last_known_unrealized_net
        )
        for t in state.active_open
    )
    if any(t.status == "UNRESOLVED" for t in state.all_trades):
        bar_flags.append("UNRESOLVED_TRADE")

    equity = round(config.initial_capital + state.realized_pnl + unrealized, 2)
    state.peak_equity = max(state.peak_equity, equity)
    dd_dollars = round(max(0.0, state.peak_equity - equity), 2)
    dd_pct = (
        round((dd_dollars / state.peak_equity) * 100.0, 2) if state.peak_equity > DENOM_EPS else 0.0
    )

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
        t_state = _start_trade_state(resolved_tid, anchor_bar, anchor_ctx, anchor_entry, config)
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
        bar_ctx = extract_bar_context(bar)
        bar_flags: list[str] = list(bar_ctx.flags)
        if FLAG_NO_USABLE_DAILY_SNAPSHOT in bar_ctx.flags:
            state.unusable_bars += 1

        still_open: list[_OpenTradeState] = []
        for t in state.active_open:
            stays_open, realized_delta = _step_open_trade(t, bar, bar_ctx, config, state.notes)
            state.realized_pnl += realized_delta
            bar_flags.extend(t.steps[-1].flags)
            if t.status == "UNRESOLVED":
                bar_flags.append("UNRESOLVED_TRADE")
                state.notes.append(
                    f"{t.trade_id} kept UNRESOLVED on {bar.market_date}: "
                    "missing settlement spot/leg."
                )
            if stays_open:
                still_open.append(t)
        state.active_open = still_open

        want_entry = (
            (bar_idx == 0 and broker_fill_anchor is None)
            if mode == "SINGLE_TRADE"
            else should_attempt_entry(bar, bar_idx, config, len(state.active_open), state.cadence)
        )
        if want_entry:
            entry_res, _entry_notes, skip_reason = build_entry_candidate_outcome(
                bar, config, legs=entry_legs, raise_on_error=(mode == "SINGLE_TRADE"), ctx=bar_ctx
            )
            if entry_res is not None:
                if mode == "SYSTEMATIC":
                    record_entry_attempt(bar, bar_idx, state.cadence)
                state.trade_seq += 1
                resolved_tid = (
                    trade_id
                    if (mode == "SINGLE_TRADE" and trade_id)
                    else f"{ticker}_BT_{state.trade_seq:04d}"
                )
                t_state = _start_trade_state(resolved_tid, bar, bar_ctx, entry_res, config)
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

    missing_quote_steps = sum(
        1
        for t in state.all_trades
        for s in t.steps
        if s.quote_missing
        and FLAG_UNRESOLVED_EXPIRY_SPOT not in s.flags
        and FLAG_UNRESOLVED_EXPIRY_LEG not in s.flags
    )
    if missing_quote_steps > 0:
        state.notes.append(
            f"{missing_quote_steps} trade daily step(s) had missing leg quotes "
            "(MTM omitted; prior unrealized P&L carried forward on equity curve)."
        )

    fallback_quote_steps = sum(
        1
        for t in state.all_trades
        for s in t.steps
        if not s.quote_missing
        and (FLAG_LEG_FALLBACK_PRICE in s.flags or FLAG_TRACKED_LEG_USED in s.flags)
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

    final_trades = [_finalize(t, active_bars[-1], config) for t in state.all_trades]
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
