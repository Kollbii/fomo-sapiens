"""Entry strategy construction, trading-calendar cadence checks, and TP/SL/DTE/expiry exit rules."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Final

import polars as pl

from src.analytics.backtest.context import BarContext, extract_bar_context, is_leg_settled
from src.analytics.backtest.models import (
    FLAG_MISSING_PRICE,
    FLAG_NO_LAST_PRINT,
    BacktestConfig,
    ExitReason,
    deduplicate_flags,
)
from src.analytics.backtest.pricing import (
    EvaluatedStepData,
    enrich_filtered_greeks,
    evaluate_trade_step_on_bar,
    filter_df_for_dte_window,
    lookup_legs_on_bar,
    resolve_row_prices,
)
from src.analytics.strategies.engine import evaluate_strategy
from src.analytics.strategies.models import EvaluatedLeg, StrategyLegInput
from src.analytics.walls import compute_walls
from src.core.calendar import is_trading_day, trading_days_between
from src.core.exceptions import BacktestValidationError
from src.storage.backtest_store import DailyChainBar
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA

DENOM_EPS: Final = 1e-9
TARGET_DTE_CENTER: Final = 38


@dataclass(frozen=True)
class EntryBuildResult:
    """Validated entry legs, cash/risk baselines, and day-0 step evaluation."""

    legs: list[EvaluatedLeg]
    entry_cash_mid_dollars: float
    entry_cash_natural_dollars: float
    entry_basis_dollars: float
    max_profit_dollars: float | None
    max_loss_dollars: float | None
    max_loss_unbounded: bool
    initial_step: EvaluatedStepData
    notes: list[str]
    flags: list[str]


@dataclass
class CadenceState:
    """Mutable cadence cursor updated strictly on past/current bars."""

    triggered_weeks: set[tuple[int, int]] = field(default_factory=set)
    last_attempt_bar_idx: int | None = None


@dataclass(frozen=True)
class ExitDecision:
    """Result of evaluating exit criteria for one open trade on the current bar."""

    should_exit: bool
    reason: ExitReason | None = None
    unresolved: bool = False
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _pick_front_expiry(df: pl.DataFrame) -> str:
    exp_rows = df.select(["expiry", "dte"]).unique(subset=["expiry"]).iter_rows(named=True)
    best = min(
        exp_rows,
        key=lambda r: (abs(int(r["dte"]) - TARGET_DTE_CENTER), int(r["dte"]), str(r["expiry"])),
    )
    return str(best["expiry"])


def _fail_or_skip(msg: str, *, raise_on_error: bool) -> tuple[None, list[str]]:
    if raise_on_error:
        raise BacktestValidationError(msg)
    return None, [msg]


def build_entry_candidate(
    bar: DailyChainBar,
    config: BacktestConfig,
    *,
    legs: Sequence[StrategyLegInput] | None = None,
    raise_on_error: bool = False,
    ctx: BarContext | None = None,
) -> tuple[EntryBuildResult | None, list[str]]:
    """Construct and validate a new trade candidate on `bar` using contract-filtered BSM."""
    bar_ctx = ctx if ctx is not None else extract_bar_context(bar)
    if bar_ctx.spot is None or bar_ctx.spot <= 0.0:
        return _fail_or_skip(
            f"Skipped entry on {bar.market_date}: invalid underlying spot price.",
            raise_on_error=raise_on_error,
        )

    eval_notes: list[str] = []
    if legs is not None:
        if not legs:
            raise BacktestValidationError("At least one option leg is required.")
        if any(is_leg_settled(leg.expiry, bar.ts_utc) for leg in legs):
            return _fail_or_skip(
                f"Cannot enter trade on {bar.market_date}: one or more legs are already expired.",
                raise_on_error=raise_on_error,
            )
        matched_raw, _ = lookup_legs_on_bar(
            bar,
            legs,
            price_basis=config.price_basis,
            close_price_mode=config.close_price_mode,
            risk_free_rate=config.risk_free_rate,
        )
        if len(matched_raw) < len(legs):
            missing_desc = ", ".join(
                f"{leg.expiry} {leg.strike:g}{leg.right}"
                for i, leg in enumerate(legs)
                if i not in matched_raw
            )
            return _fail_or_skip(
                f"Contract(s) not found on {bar.market_date}: {missing_desc}",
                raise_on_error=raise_on_error,
            )
        leg_rows = [dict(matched_raw[i][0]) for i in range(len(legs))]
        for row in leg_rows:
            row["underlying_price"] = bar_ctx.spot
        leg_df = pl.DataFrame(leg_rows, schema=OPTION_CHAIN_POLARS_SCHEMA)
        walls = compute_walls(bar.df, top_n=config.walls_top_n) if bar.df.height > 0 else None
        try:
            ev = evaluate_strategy(
                leg_df,
                strategy_kind=config.strategy_kind,
                legs=list(legs),
                qty_multiplier=config.qty,
                risk_free_rate=config.risk_free_rate,
                walls_report=walls,
            )
        except ValueError as exc:
            return _fail_or_skip(
                f"Strategy evaluation failed on {bar.market_date}: {exc}",
                raise_on_error=raise_on_error,
            )
    else:
        if config.strategy_kind == "CUSTOM":
            raise BacktestValidationError("CUSTOM strategy requires explicit legs.")
        if bar.df.height == 0:
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: empty option chain.",
                raise_on_error=raise_on_error,
            )
        front_df = filter_df_for_dte_window(bar.df, config.target_dte_min, config.target_dte_max)
        if front_df.height == 0:
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: no contracts in "
                f"[{config.target_dte_min}, {config.target_dte_max}] DTE.",
                raise_on_error=raise_on_error,
            )
        front_expiry = _pick_front_expiry(front_df)
        scope_df = (
            filter_df_for_dte_window(bar.df, config.target_dte_min, max(config.target_dte_max, 120))
            if config.strategy_kind == "DIAGONAL_SPREAD"
            else front_df
        )
        enriched_df = enrich_filtered_greeks(scope_df, risk_free_rate=config.risk_free_rate)
        walls = compute_walls(enriched_df, top_n=config.walls_top_n)
        try:
            ev = evaluate_strategy(
                enriched_df,
                strategy_kind=config.strategy_kind,
                target_expiry=front_expiry,
                qty_multiplier=config.qty,
                risk_free_rate=config.risk_free_rate,
                walls_report=walls,
            )
        except ValueError as exc:
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: {exc}",
                raise_on_error=raise_on_error,
            )
        eval_notes.extend(ev.rationale)

    if any(is_leg_settled(leg.expiry, bar.ts_utc) for leg in ev.legs):
        return _fail_or_skip(
            f"Skipped entry on {bar.market_date}: "
            "selected expiry already reached settlement cutoff.",
            raise_on_error=raise_on_error,
        )

    entry_rows, _ = lookup_legs_on_bar(
        bar,
        ev.legs,
        price_basis=config.price_basis,
        close_price_mode=config.close_price_mode,
        risk_free_rate=config.risk_free_rate,
    )
    entry_cash_mid = 0.0
    entry_cash_nat = 0.0
    updated_legs: list[EvaluatedLeg] = []
    entry_flags: list[str] = []

    for idx, leg in enumerate(ev.legs):
        found_entry = entry_rows.get(idx)
        if found_entry is None:
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: missing leg "
                f"{leg.expiry} {leg.strike:g}{leg.right}.",
                raise_on_error=raise_on_error,
            )
        raw_row, _ = found_entry
        res = resolve_row_prices(
            raw_row,
            side=leg.side,
            price_basis=config.price_basis,
            close_price_mode=config.close_price_mode,
        )
        if res.price_used is None or res.no_last_print:
            reason_flag = FLAG_NO_LAST_PRINT if res.no_last_print else FLAG_MISSING_PRICE
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: unusable price ({reason_flag}) for "
                f"{leg.expiry} {leg.strike:g}{leg.right}.",
                raise_on_error=raise_on_error,
            )
        cash_sign = -1.0 if leg.side == "BUY" else 1.0
        entry_cash_mid += cash_sign * float(leg.qty) * 100.0 * res.price_used
        nat_open = res.natural_open_price if res.natural_open_price is not None else res.price_used
        entry_cash_nat += cash_sign * float(leg.qty) * 100.0 * nat_open
        if res.used_fallback:
            entry_flags.append("LEG_FALLBACK_PRICE")
        updated_legs.append(
            leg.model_copy(
                update={
                    "mid": round(res.price_used, 4),
                    "natural_fill": round(nat_open, 4),
                }
            )
        )

    entry_cash_mid_r = round(entry_cash_mid, 2)
    entry_cash_nat_r = round(entry_cash_nat, 2)
    entry_basis = round(abs(entry_cash_mid_r), 2)
    delta_cash = entry_cash_mid_r - ev.pricing.mid_cash_total

    max_profit_dollars = round(max(0.0, ev.risk.max_profit + delta_cash), 2)
    if ev.risk.max_loss_unbounded:
        max_loss_dollars: float | None = None
    else:
        max_loss_dollars = round(max(0.0, ev.risk.max_loss - delta_cash), 2)

    initial_step = evaluate_trade_step_on_bar(bar, updated_legs, config, ctx=bar_ctx)
    all_flags = deduplicate_flags([*entry_flags, *initial_step.flags])
    return (
        EntryBuildResult(
            legs=updated_legs,
            entry_cash_mid_dollars=entry_cash_mid_r,
            entry_cash_natural_dollars=entry_cash_nat_r,
            entry_basis_dollars=entry_basis,
            max_profit_dollars=max_profit_dollars,
            max_loss_dollars=max_loss_dollars,
            max_loss_unbounded=ev.risk.max_loss_unbounded,
            initial_step=initial_step,
            notes=eval_notes,
            flags=all_flags,
        ),
        [],
    )


def should_attempt_entry(
    bar: DailyChainBar,
    bar_idx: int,
    config: BacktestConfig,
    open_count: int,
    state: CadenceState,
) -> bool:
    """Evaluate whether `entry_cadence` triggers an entry attempt on `bar`."""
    if open_count >= config.max_open_trades:
        return False

    if config.entry_cadence == "WHEN_FLAT":
        return open_count == 0

    if config.entry_cadence == "EVERY_N_DAYS":
        if state.last_attempt_bar_idx is None:
            return True
        return (bar_idx - state.last_attempt_bar_idx) >= config.entry_every_n_days

    bar_day = date.fromisoformat(bar.market_date)
    iso_year, iso_week, _ = bar_day.isocalendar()
    week_key = (iso_year, iso_week)
    if week_key in state.triggered_weeks:
        return False

    target = date.fromisocalendar(iso_year, iso_week, config.entry_weekday + 1)
    if bar_day == target:
        return True
    if bar_day > target and not is_trading_day(target):
        friday = date.fromisocalendar(iso_year, iso_week, 5)
        candidates = trading_days_between(target, friday)
        return bool(candidates and bar_day == candidates[0])
    return False


def record_entry_attempt(bar: DailyChainBar, bar_idx: int, state: CadenceState) -> None:
    """Update cadence state once an entry attempt executes on `bar`."""
    bar_day = date.fromisoformat(bar.market_date)
    iso_year, iso_week, _ = bar_day.isocalendar()
    state.triggered_weeks.add((iso_year, iso_week))
    state.last_attempt_bar_idx = bar_idx


def evaluate_exit_rule(
    step: EvaluatedStepData,
    config: BacktestConfig,
    *,
    entry_cash_mid_dollars: float,
    entry_basis_dollars: float,
    max_loss_dollars: float | None,
    max_loss_unbounded: bool,
) -> ExitDecision:
    """Evaluate `EXPIRY_SETTLED`, `EXIT_DTE`, `PROFIT_TARGET`, and `STOP_LOSS` in priority order."""
    if step.any_leg_settled:
        if step.settlement_blocked or step.active_close_value_dollars is None:
            return ExitDecision(
                should_exit=True,
                reason="EXPIRY_SETTLED",
                unresolved=True,
                notes=[
                    "Trade kept UNRESOLVED at expiry due to missing non-CLOSE spot or leg price."
                ],
            )
        return ExitDecision(should_exit=True, reason="EXPIRY_SETTLED", unresolved=False)

    if step.active_close_value_dollars is None:
        flags = (
            ["EXIT_SKIPPED_MISSING_PRICE"]
            if (config.exit_dte is not None and step.dte_min_leg <= config.exit_dte)
            else []
        )
        return ExitDecision(should_exit=False, flags=flags)

    if config.exit_dte is not None and step.dte_min_leg <= config.exit_dte:
        return ExitDecision(should_exit=True, reason="EXIT_DTE")

    gross_pnl = entry_cash_mid_dollars + step.active_close_value_dollars
    if config.tp_sl_basis == "CREDIT_DEBIT_PCT":
        if entry_basis_dollars <= DENOM_EPS:
            return ExitDecision(
                should_exit=False,
                flags=["ZERO_ENTRY_BASIS"],
                notes=["TP/SL skipped: entry basis is zero under CREDIT_DEBIT_PCT."],
            )
        denom = entry_basis_dollars
    else:
        if max_loss_unbounded or max_loss_dollars is None or max_loss_dollars <= DENOM_EPS:
            return ExitDecision(
                should_exit=False,
                flags=["MAX_RISK_UNBOUNDED_SKIP_TPSL"],
                notes=["TP/SL skipped: max loss is unbounded or zero under MAX_RISK_PCT."],
            )
        denom = max_loss_dollars

    if (
        config.profit_target_pct is not None
        and (gross_pnl / denom) * 100.0 >= config.profit_target_pct
    ):
        return ExitDecision(should_exit=True, reason="PROFIT_TARGET")
    if config.stop_loss_pct is not None and (-gross_pnl / denom) * 100.0 >= config.stop_loss_pct:
        return ExitDecision(should_exit=True, reason="STOP_LOSS")
    return ExitDecision(should_exit=False)
