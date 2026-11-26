"""All-Days entry-date invariance cohorts (spreads) and rolling-start cohorts (CC/Wheel)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
import numpy.typing as npt

from src.analytics.backtest.engine import run_systematic_backtest
from src.analytics.backtest.fast_cache import FastBarCache
from src.analytics.backtest.models import (
    FLAG_CAPITAL_DEPLETED,
    BacktestConfig,
    BacktestStrategyKind,
    EquityCurvePoint,
)
from src.analytics.backtest.pricing import evaluate_trade_step_on_bar
from src.analytics.backtest.robustness_models import (
    AllDaysCohortReport,
    CohortConfig,
    RollingStartCohortPoint,
    WeekdayStabilityRow,
    clean_float,
    finite_float,
)
from src.analytics.backtest.rules import (
    DENOM_EPS,
    build_entry_candidate_outcome,
    resolve_tp_sl_thresholds,
)
from src.core.exceptions import BacktestValidationError

SPREAD_STRATEGIES: Final[frozenset[BacktestStrategyKind]] = frozenset(
    {
        "BULL_PUT_SPREAD",
        "BEAR_CALL_SPREAD",
        "IRON_CONDOR",
        "BULL_CALL_SPREAD",
        "BEAR_PUT_SPREAD",
        "DIAGONAL_SPREAD",
    }
)
STATEFUL_STRATEGIES: Final[frozenset[BacktestStrategyKind]] = frozenset({"COVERED_CALL", "WHEEL"})
WEEKDAY_NAMES: Final[tuple[str, ...]] = ("Mon", "Tue", "Wed", "Thu", "Fri")

FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True)
class SpreadCohortEntry:
    """Pre-evaluated Stage A MTM trajectory for one bar's spread candidate."""

    entry_idx: int
    entry_weekday: int
    entry_cash_mid: float
    entry_cash_nat: float
    entry_basis: float
    max_profit: float | None
    max_loss: float | None
    max_loss_unbounded: bool
    total_contracts: int
    open_comm: float
    step_dte_min: tuple[int, ...]
    step_mark_val: tuple[float | None, ...]
    step_nat_val: tuple[float | None, ...]
    step_active_val: tuple[float | None, ...]
    step_any_settled: tuple[bool, ...]
    step_settlement_blocked: tuple[bool, ...]
    step_unexpired_contracts: tuple[int, ...]


@dataclass(frozen=True)
class SpreadCohortMatrix:
    """Stage A matrix of all entered spread cohorts and skip tallies across `T` bars."""

    total_bars: int
    cohorts: tuple[SpreadCohortEntry, ...]
    attempted_by_weekday: tuple[int, int, int, int, int]
    skipped_by_reason: dict[str, int]
    bar_weekdays: tuple[int, ...] = ()
    bar_skip_reasons: tuple[str | None, ...] = ()

    def slice(self, start: int, end: int) -> SpreadCohortMatrix:
        """Return a zero-lookahead sub-window `[start, end)` view of this matrix."""
        if start < 0 or end > self.total_bars or start > end:
            raise ValueError(
                f"Invalid matrix slice bounds [{start}, {end}) for total_bars={self.total_bars}."
            )
        sub_total = end - start
        attempted_wd = [0, 0, 0, 0, 0]
        for wd in self.bar_weekdays[start:end]:
            if 0 <= wd <= 4:
                attempted_wd[wd] += 1

        skipped: dict[str, int] = {}
        for reason in self.bar_skip_reasons[start:end]:
            if reason is not None:
                skipped[reason] = skipped.get(reason, 0) + 1

        sliced_cohorts: list[SpreadCohortEntry] = []
        for c in self.cohorts:
            if c.entry_idx < start:
                continue
            if c.entry_idx >= end:
                break
            horizon = end - c.entry_idx
            assert horizon >= 1
            sliced_cohorts.append(
                SpreadCohortEntry(
                    entry_idx=c.entry_idx - start,
                    entry_weekday=c.entry_weekday,
                    entry_cash_mid=c.entry_cash_mid,
                    entry_cash_nat=c.entry_cash_nat,
                    entry_basis=c.entry_basis,
                    max_profit=c.max_profit,
                    max_loss=c.max_loss,
                    max_loss_unbounded=c.max_loss_unbounded,
                    total_contracts=c.total_contracts,
                    open_comm=c.open_comm,
                    step_dte_min=c.step_dte_min[:horizon],
                    step_mark_val=c.step_mark_val[:horizon],
                    step_nat_val=c.step_nat_val[:horizon],
                    step_active_val=c.step_active_val[:horizon],
                    step_any_settled=c.step_any_settled[:horizon],
                    step_settlement_blocked=c.step_settlement_blocked[:horizon],
                    step_unexpired_contracts=c.step_unexpired_contracts[:horizon],
                )
            )

        return SpreadCohortMatrix(
            total_bars=sub_total,
            cohorts=tuple(sliced_cohorts),
            attempted_by_weekday=(
                attempted_wd[0],
                attempted_wd[1],
                attempted_wd[2],
                attempted_wd[3],
                attempted_wd[4],
            ),
            skipped_by_reason=dict(sorted(skipped.items())),
            bar_weekdays=self.bar_weekdays[start:end],
            bar_skip_reasons=self.bar_skip_reasons[start:end],
        )


@dataclass(frozen=True)
class CohortRunOutput:
    """Internal cohort output bundle carrying arrays needed by sensitivity, WFO, and MC."""

    report: AllDaysCohortReport
    daily_returns: FloatArray
    closed_trade_pnls: FloatArray
    slippage_pairs: list[tuple[float, float, float, float, float, float]] | None


def compute_return_risk_ratios(
    daily_returns: FloatArray,
    *,
    total_return_pct: float,
    max_drawdown_pct: float,
) -> tuple[float | None, float | None, float | None]:
    """Compute annualized `(sharpe, sortino, calmar)` from daily portfolio return series."""
    n = len(daily_returns)
    sharpe: float | None = None
    sortino: float | None = None
    calmar: float | None = None

    if n >= 2:
        mean_r = float(np.mean(daily_returns))
        std_r = float(np.std(daily_returns, ddof=1))
        if math.isfinite(std_r) and std_r > 1e-9:
            sharpe = clean_float((mean_r / std_r) * math.sqrt(252.0), 4)
        downside = np.minimum(daily_returns, 0.0)
        down_dev = math.sqrt(float(np.mean(downside * downside)))
        if math.isfinite(down_dev) and down_dev > 1e-9:
            sortino = clean_float((mean_r / down_dev) * math.sqrt(252.0), 4)

    if n >= 1 and max_drawdown_pct > 1e-6:
        gross_factor = 1.0 + total_return_pct / 100.0
        if gross_factor > 0.0:
            ann_ret_pct = (gross_factor ** (252.0 / float(n)) - 1.0) * 100.0
        else:
            ann_ret_pct = -100.0
        calmar = clean_float(ann_ret_pct / max_drawdown_pct, 4)

    return sharpe, sortino, calmar


def _detect_weekday_skew(
    rows: list[WeekdayStabilityRow], overall_exp: float | None
) -> tuple[bool, str | None]:
    active = [r for r in rows if r.closed_trades >= 2 and r.expectancy_dollars is not None]
    if len(active) < 2:
        return False, None
    exps = [r.expectancy_dollars for r in active if r.expectancy_dollars is not None]
    win_rates = [r.win_rate_pct for r in active if r.win_rate_pct is not None]
    max_exp = max(exps)
    min_exp = min(exps)
    exp_range = max_exp - min_exp
    wr_gap = (max(win_rates) - min(win_rates)) if len(win_rates) >= 2 else 0.0
    ref_scale = max(10.0, 0.5 * abs(overall_exp or 0.0))

    sign_flip = (
        max_exp > 0.0
        and min_exp < 0.0
        and exp_range > ref_scale
        and (wr_gap >= 15.0 or exp_range >= 20.0)
    )
    extreme_disp = exp_range > max(25.0, 1.5 * abs(float(np.mean(exps)))) and wr_gap >= 25.0
    if not (sign_flip or extreme_disp):
        return False, None

    best_row = max(
        active, key=lambda r: r.expectancy_dollars if r.expectancy_dollars is not None else -1e18
    )
    worst_row = min(
        active, key=lambda r: r.expectancy_dollars if r.expectancy_dollars is not None else 1e18
    )
    msg = (
        f"Entry-weekday skew detected: {best_row.weekday_name} "
        f"avg ${best_row.expectancy_dollars:+.2f} "
        f"vs {worst_row.weekday_name} avg ${worst_row.expectancy_dollars:+.2f} "
        f"(win-rate spread {wr_gap:.1f}pp)."
    )
    return True, msg


def _weekday_rows(
    attempted_by_wd: tuple[int, int, int, int, int],
    closed_pnls_by_wd: dict[int, list[float]],
    open_by_wd: dict[int, int],
    rors_by_wd: dict[int, list[float]],
) -> list[WeekdayStabilityRow]:
    rows: list[WeekdayStabilityRow] = []
    for wd in range(5):
        pnls = closed_pnls_by_wd.get(wd, [])
        rors = rors_by_wd.get(wd, [])
        wins = [p for p in pnls if p > 0.0]
        losses = [p for p in pnls if p < 0.0]
        sum_wins = sum(wins)
        sum_losses_abs = sum(-p for p in losses)
        pf = clean_float(sum_wins / sum_losses_abs, 4) if sum_losses_abs > DENOM_EPS else None
        rows.append(
            WeekdayStabilityRow(
                weekday=wd,
                weekday_name=WEEKDAY_NAMES[wd],
                attempted_entries=attempted_by_wd[wd],
                closed_trades=len(pnls),
                open_trades=open_by_wd.get(wd, 0),
                win_rate_pct=clean_float((len(wins) / len(pnls)) * 100.0, 2) if pnls else None,
                expectancy_dollars=clean_float(sum(pnls) / len(pnls), 2) if pnls else None,
                total_net_pnl_dollars=finite_float(sum(pnls), 2),
                avg_return_on_risk_pct=clean_float(sum(rors) / len(rors), 2) if rors else None,
                profit_factor=pf,
            )
        )
    return rows


def build_spread_cohort_matrix(
    cache: FastBarCache,
    config: BacktestConfig,
) -> SpreadCohortMatrix:
    """Stage A: build each bar's spread candidate and pre-evaluate its MTM path until expiry."""
    if config.strategy_kind not in SPREAD_STRATEGIES:
        raise BacktestValidationError(
            f"Spread cohort matrix requires a spread strategy, got {config.strategy_kind}."
        )
    entries = cache.entries
    total_bars = len(entries)
    cohorts: list[SpreadCohortEntry] = []
    attempted_wd = [0, 0, 0, 0, 0]
    skipped_by_reason: dict[str, int] = {}
    bar_weekdays: list[int] = []
    bar_skip_reasons: list[str | None] = []

    for t_idx in range(total_bars):
        entry_bar = entries[t_idx]
        wd = entry_bar.bar_et.weekday()
        bar_weekdays.append(wd)
        if 0 <= wd <= 4:
            attempted_wd[wd] += 1

        entry_res, _notes, skip_reason = build_entry_candidate_outcome(
            entry_bar.bar,
            config,
            raise_on_error=False,
            ctx=entry_bar.ctx,
            fast_cache=cache,
            bar_idx=t_idx,
        )
        if entry_res is None:
            key = skip_reason or "ENTRY_SKIPPED"
            skipped_by_reason[key] = skipped_by_reason.get(key, 0) + 1
            bar_skip_reasons.append(key)
            continue
        bar_skip_reasons.append(None)

        total_contracts = sum(leg.qty for leg in entry_res.legs)
        open_comm = round(config.commission_per_contract * float(total_contracts), 2)
        step0 = entry_res.initial_step

        dte_mins: list[int] = [step0.dte_min_leg]
        mark_vals: list[float | None] = [step0.mark_value_dollars]
        nat_vals: list[float | None] = [step0.natural_close_value_dollars]
        act_vals: list[float | None] = [step0.active_close_value_dollars]
        any_settled: list[bool] = [step0.any_leg_settled]
        settle_blocked: list[bool] = [step0.settlement_blocked]
        unexp_contracts: list[int] = [step0.unexpired_close_contracts]

        for k_idx in range(t_idx + 1, total_bars):
            k_entry = entries[k_idx]
            step_k = evaluate_trade_step_on_bar(
                k_entry.bar,
                entry_res.legs,
                config,
                ctx=k_entry.ctx,
                fast_cache=cache,
                bar_idx=k_idx,
                need_greeks=False,
                collect_leg_marks=False,
            )
            dte_mins.append(step_k.dte_min_leg)
            mark_vals.append(step_k.mark_value_dollars)
            nat_vals.append(step_k.natural_close_value_dollars)
            act_vals.append(step_k.active_close_value_dollars)
            any_settled.append(step_k.any_leg_settled)
            settle_blocked.append(step_k.settlement_blocked)
            unexp_contracts.append(step_k.unexpired_close_contracts)
            if step_k.any_leg_settled:
                break

        cohorts.append(
            SpreadCohortEntry(
                entry_idx=t_idx,
                entry_weekday=wd,
                entry_cash_mid=entry_res.entry_cash_mid_dollars,
                entry_cash_nat=entry_res.entry_cash_natural_dollars,
                entry_basis=entry_res.entry_basis_dollars,
                max_profit=entry_res.max_profit_dollars,
                max_loss=entry_res.max_loss_dollars,
                max_loss_unbounded=entry_res.max_loss_unbounded,
                total_contracts=total_contracts,
                open_comm=open_comm,
                step_dte_min=tuple(dte_mins),
                step_mark_val=tuple(mark_vals),
                step_nat_val=tuple(nat_vals),
                step_active_val=tuple(act_vals),
                step_any_settled=tuple(any_settled),
                step_settlement_blocked=tuple(settle_blocked),
                step_unexpired_contracts=tuple(unexp_contracts),
            )
        )

    return SpreadCohortMatrix(
        total_bars=total_bars,
        cohorts=tuple(cohorts),
        attempted_by_weekday=(
            attempted_wd[0],
            attempted_wd[1],
            attempted_wd[2],
            attempted_wd[3],
            attempted_wd[4],
        ),
        skipped_by_reason=dict(sorted(skipped_by_reason.items())),
        bar_weekdays=tuple(bar_weekdays),
        bar_skip_reasons=tuple(bar_skip_reasons),
    )


def spread_matrix_cache_key(cache: FastBarCache, config: BacktestConfig) -> tuple[object, ...]:
    """Return a cache key capturing window bounds and every entry-affecting spread config field."""
    first_sid = cache.entries[0].bar.snapshot_id if cache.entries else ""
    last_sid = cache.entries[-1].bar.snapshot_id if cache.entries else ""
    return (
        first_sid,
        last_sid,
        len(cache),
        config.strategy_kind,
        config.target_dte_min,
        config.target_dte_max,
        config.qty,
        config.commission_per_contract,
        config.price_basis,
        config.close_price_mode,
        config.risk_free_rate,
        config.walls_top_n,
        float(config.min_entry_credit_dollars or 0.0),
        float(config.min_entry_ror_pct or 0.0),
    )


def get_or_build_spread_matrix(
    cache: FastBarCache,
    config: BacktestConfig,
    matrix_cache: dict[tuple[object, ...], SpreadCohortMatrix] | None = None,
) -> SpreadCohortMatrix:
    """Lookup or build a `SpreadCohortMatrix` for `(cache, config)` in `matrix_cache`."""
    if matrix_cache is None:
        return build_spread_cohort_matrix(cache, config)
    key = spread_matrix_cache_key(cache, config)
    matrix = matrix_cache.get(key)
    if matrix is None:
        matrix = build_spread_cohort_matrix(cache, config)
        matrix_cache[key] = matrix
    return matrix


def evaluate_cohort_matrix(
    matrix: SpreadCohortMatrix,
    config: BacktestConfig,
    *,
    cache: FastBarCache,
) -> CohortRunOutput:
    """Stage B: apply TP/SL/exit-DTE rules over a Stage A `SpreadCohortMatrix`."""
    total_bars = matrix.total_bars
    entries = cache.entries
    daily_delta_pnl = np.zeros(total_bars, dtype=np.float64)
    active_diff = np.zeros(total_bars + 1, dtype=np.int64)

    closed_pnls: list[float] = []
    closed_rors: list[float] = []
    slippage_pairs: list[tuple[float, float, float, float, float, float]] = []
    closed_by_wd: dict[int, list[float]] = {wd: [] for wd in range(5)}
    open_by_wd: dict[int, int] = {wd: 0 for wd in range(5)}
    rors_by_wd: dict[int, list[float]] = {wd: [] for wd in range(5)}

    open_cohorts = 0
    unresolved_cohorts = 0
    sum_active_bars = 0

    for c in matrix.cohorts:
        denom, _tp_d, _sl_d, _skip_f, _skip_n = resolve_tp_sl_thresholds(
            config,
            entry_basis_dollars=c.entry_basis,
            max_loss_dollars=c.max_loss,
            max_loss_unbounded=c.max_loss_unbounded,
        )
        act0 = c.step_active_val[0]
        prev_cum = (
            round(c.entry_cash_mid + act0 - c.open_comm, 2) if act0 is not None else -c.open_comm
        )
        daily_delta_pnl[c.entry_idx] += prev_cum

        status = "OPEN"
        exit_idx = c.entry_idx + len(c.step_dte_min) - 1
        final_net = prev_cum

        for j in range(1, len(c.step_dte_min)):
            d = c.entry_idx + j
            if c.step_any_settled[j]:
                act_j = c.step_active_val[j]
                if c.step_settlement_blocked[j] or act_j is None:
                    status = "UNRESOLVED"
                    exit_idx = d
                    final_net = -c.open_comm
                    daily_delta_pnl[d] += final_net - prev_cum
                else:
                    close_comm = round(
                        config.commission_per_contract * float(c.step_unexpired_contracts[j]), 2
                    )
                    gross = round(c.entry_cash_mid + act_j, 2)
                    net = round(gross - (c.open_comm + close_comm), 2)
                    status = "CLOSED"
                    exit_idx = d
                    final_net = net
                    daily_delta_pnl[d] += net - prev_cum
                    mark_j = c.step_mark_val[j]
                    nat_j = c.step_nat_val[j]
                    if mark_j is not None and nat_j is not None:
                        slippage_pairs.append(
                            (
                                c.entry_cash_mid,
                                c.entry_cash_nat,
                                mark_j,
                                nat_j,
                                c.open_comm,
                                close_comm,
                            )
                        )
                break

            act_j = c.step_active_val[j]
            if act_j is None:
                continue

            gross_raw = c.entry_cash_mid + act_j
            should_close = (
                config.exit_dte is not None and c.step_dte_min[j] <= config.exit_dte
            ) or (
                denom is not None
                and (
                    (
                        config.profit_target_pct is not None
                        and (gross_raw / denom) * 100.0 >= config.profit_target_pct
                    )
                    or (
                        config.stop_loss_pct is not None
                        and (-gross_raw / denom) * 100.0 >= config.stop_loss_pct
                    )
                )
            )

            if should_close:
                close_comm = round(config.commission_per_contract * float(c.total_contracts), 2)
                gross = round(gross_raw, 2)
                net = round(gross - (c.open_comm + close_comm), 2)
                status = "CLOSED"
                exit_idx = d
                final_net = net
                daily_delta_pnl[d] += net - prev_cum
                mark_j = c.step_mark_val[j]
                nat_j = c.step_nat_val[j]
                if mark_j is not None and nat_j is not None:
                    slippage_pairs.append(
                        (
                            c.entry_cash_mid,
                            c.entry_cash_nat,
                            mark_j,
                            nat_j,
                            c.open_comm,
                            close_comm,
                        )
                    )
                break

            cur_mtm = round(gross_raw - c.open_comm, 2)
            daily_delta_pnl[d] += cur_mtm - prev_cum
            prev_cum = cur_mtm

        sum_active_bars += exit_idx - c.entry_idx + 1
        active_diff[c.entry_idx] += 1
        active_diff[exit_idx + 1] -= 1

        if status == "CLOSED":
            closed_pnls.append(final_net)
            if 0 <= c.entry_weekday <= 4:
                closed_by_wd[c.entry_weekday].append(final_net)
            if not c.max_loss_unbounded and c.max_loss is not None and c.max_loss > DENOM_EPS:
                ror = (final_net / c.max_loss) * 100.0
                closed_rors.append(ror)
                if 0 <= c.entry_weekday <= 4:
                    rors_by_wd[c.entry_weekday].append(ror)
        elif status == "UNRESOLVED":
            unresolved_cohorts += 1
        else:
            open_cohorts += 1
            if 0 <= c.entry_weekday <= 4:
                open_by_wd[c.entry_weekday] += 1

    avg_concurrent = max(1.0, float(sum_active_bars) / float(max(1, total_bars)))
    weight = 1.0 / avg_concurrent

    equity_curve: list[EquityCurvePoint] = []
    daily_returns = np.zeros(total_bars, dtype=np.float64)
    cum_scaled_pnl = 0.0
    equity_raw = config.initial_capital
    peak_eq = config.initial_capital
    max_dd_pct = 0.0
    open_running = 0
    capital_depleted = False

    for d in range(total_bars):
        open_running += int(active_diff[d])
        prev_eq = equity_raw
        cum_scaled_pnl += weight * float(daily_delta_pnl[d])
        equity_raw = config.initial_capital + cum_scaled_pnl
        eq_r = finite_float(equity_raw, 2)
        peak_eq = max(peak_eq, eq_r)
        dd_d = finite_float(max(0.0, peak_eq - eq_r), 2)
        dd_p = finite_float(
            min(100.0, ((peak_eq - max(0.0, eq_r)) / peak_eq) * 100.0)
            if peak_eq > DENOM_EPS
            else 0.0,
            2,
        )
        max_dd_pct = max(max_dd_pct, dd_p)
        if prev_eq > 1e-6:
            daily_returns[d] = max(-0.9999, (equity_raw - prev_eq) / prev_eq)
        else:
            daily_returns[d] = 0.0

        bar_flags: list[str] = []
        if eq_r <= 0.0:
            capital_depleted = True
            bar_flags.append(FLAG_CAPITAL_DEPLETED)

        bar = entries[d].bar
        equity_curve.append(
            EquityCurvePoint(
                market_date=bar.market_date,
                snapshot_id=bar.snapshot_id,
                ts_utc=bar.ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                equity_dollars=eq_r,
                realized_pnl_dollars=finite_float(cum_scaled_pnl, 2),
                unrealized_pnl_dollars=0.0,
                drawdown_dollars=dd_d,
                drawdown_pct=dd_p,
                open_trades=max(0, open_running),
                underlying_price=clean_float(entries[d].ctx.spot, 4),
                flags=bar_flags,
            )
        )

    wins = [p for p in closed_pnls if p > 0.0]
    losses = [p for p in closed_pnls if p < 0.0]
    sum_wins = sum(wins)
    sum_losses_abs = sum(-p for p in losses)
    total_closed_pnl = sum(closed_pnls)
    expectancy = clean_float(total_closed_pnl / len(closed_pnls), 2) if closed_pnls else None
    win_rate = clean_float((len(wins) / len(closed_pnls)) * 100.0, 2) if closed_pnls else None
    avg_ror = clean_float(sum(closed_rors) / len(closed_rors), 2) if closed_rors else None
    profit_factor = (
        clean_float(sum_wins / sum_losses_abs, 4) if sum_losses_abs > DENOM_EPS else None
    )
    portfolio_total_ret = finite_float(
        ((equity_raw - config.initial_capital) / config.initial_capital) * 100.0, 2
    )
    sharpe, sortino, calmar = compute_return_risk_ratios(
        daily_returns,
        total_return_pct=portfolio_total_ret,
        max_drawdown_pct=max_dd_pct,
    )

    wd_rows = _weekday_rows(matrix.attempted_by_weekday, closed_by_wd, open_by_wd, rors_by_wd)
    skew_detected, skew_msg = _detect_weekday_skew(wd_rows, expectancy)

    notes: list[str] = []
    if len(matrix.cohorts) == 0:
        notes.append("No spread cohorts entered across the evaluation window.")
    elif not closed_pnls:
        notes.append("Entered cohorts remained open or unresolved at end of window.")
    if capital_depleted:
        notes.append(
            "Portfolio equity dropped to <= $0.00 (CAPITAL_DEPLETED: position notional losses "
            "exceeded initial_capital; percentage drawdown capped at 100%)."
        )

    report = AllDaysCohortReport(
        mode="ALL_DAYS_SPREADS",
        total_bars=total_bars,
        attempted_entries=total_bars,
        entered_cohorts=len(matrix.cohorts),
        closed_cohorts=len(closed_pnls),
        open_cohorts=open_cohorts,
        unresolved_cohorts=unresolved_cohorts,
        skipped_entries_count=sum(matrix.skipped_by_reason.values()),
        skipped_by_reason=matrix.skipped_by_reason,
        avg_concurrent_positions=finite_float(avg_concurrent, 2, default=1.0),
        win_rate_pct=win_rate,
        expectancy_dollars=expectancy,
        avg_return_on_risk_pct=avg_ror,
        profit_factor=profit_factor,
        total_net_pnl_dollars=finite_float(total_closed_pnl, 2),
        portfolio_total_return_pct=portfolio_total_ret,
        portfolio_max_drawdown_pct=finite_float(max_dd_pct, 2),
        portfolio_sharpe=sharpe,
        portfolio_sortino=sortino,
        portfolio_calmar=calmar,
        equity_curve=equity_curve,
        weekday_stability=wd_rows,
        weekday_skew_detected=skew_detected,
        weekday_skew_message=skew_msg,
        notes=notes,
    )
    return CohortRunOutput(
        report=report,
        daily_returns=daily_returns,
        closed_trade_pnls=np.array(closed_pnls, dtype=np.float64),
        slippage_pairs=slippage_pairs,
    )


def _run_stateful_rolling_cohorts(
    cache: FastBarCache,
    config: BacktestConfig,
    *,
    ticker: str,
    cohort_config: CohortConfig,
    include_rolling: bool = True,
) -> CohortRunOutput:
    baseline_cfg = config.model_copy(update={"entry_cadence": "WHEN_FLAT"})
    base_res = run_systematic_backtest(
        cache.bars,
        baseline_cfg,
        ticker=ticker,
        fast_cache=cache,
        need_greeks=False,
        collect_steps=False,
    )
    total_bars = len(cache)

    daily_returns = np.zeros(len(base_res.equity_curve), dtype=np.float64)
    prev_eq = config.initial_capital
    capital_depleted = False
    for idx, pt in enumerate(base_res.equity_curve):
        if pt.equity_dollars <= 0.0 or FLAG_CAPITAL_DEPLETED in pt.flags:
            capital_depleted = True
        if prev_eq > 1e-6:
            daily_returns[idx] = (
                -0.9999
                if pt.equity_dollars <= 0.0
                else max(-0.9999, (pt.equity_dollars - prev_eq) / prev_eq)
            )
        else:
            daily_returns[idx] = 0.0
        prev_eq = max(0.0, pt.equity_dollars)

    closed_trades = [t for t in base_res.trades if t.status == "CLOSED"]
    closed_pnls = [float(t.net_pnl_dollars) for t in closed_trades if t.net_pnl_dollars is not None]
    closed_rors = [
        float(t.return_on_risk_pct) for t in closed_trades if t.return_on_risk_pct is not None
    ]

    attempted_wd = [0, 0, 0, 0, 0]
    closed_by_wd: dict[int, list[float]] = {wd: [] for wd in range(5)}
    open_by_wd: dict[int, int] = {wd: 0 for wd in range(5)}
    rors_by_wd: dict[int, list[float]] = {wd: [] for wd in range(5)}
    sid_to_wd = {entry.bar.snapshot_id: entry.bar_et.weekday() for entry in cache.entries}
    default_wd = cache.entries[0].bar_et.weekday() if cache.entries else 0
    for t in base_res.trades:
        wd = sid_to_wd.get(t.entry_snapshot_id, default_wd)
        if 0 <= wd <= 4:
            attempted_wd[wd] += 1
            if t.status == "CLOSED" and t.net_pnl_dollars is not None:
                closed_by_wd[wd].append(float(t.net_pnl_dollars))
                if t.return_on_risk_pct is not None:
                    rors_by_wd[wd].append(float(t.return_on_risk_pct))
            elif t.status == "OPEN":
                open_by_wd[wd] += 1

    wd_rows = _weekday_rows(
        (attempted_wd[0], attempted_wd[1], attempted_wd[2], attempted_wd[3], attempted_wd[4]),
        closed_by_wd,
        open_by_wd,
        rors_by_wd,
    )
    skew_detected, skew_msg = _detect_weekday_skew(wd_rows, base_res.summary.expectancy_dollars)

    rolling_points: list[RollingStartCohortPoint] = []
    notes: list[str] = []
    if capital_depleted:
        notes.append(
            "Portfolio equity dropped to <= $0.00 (CAPITAL_DEPLETED: position notional losses "
            "exceeded initial_capital; percentage drawdown capped at 100%)."
        )
    if include_rolling:
        if total_bars < 15:
            notes.append(
                f"Need at least 15 bars for rolling start-date cohorts (got {total_bars})."
            )
        else:
            horizon = (
                cohort_config.rolling_horizon_bars
                if cohort_config.rolling_horizon_bars is not None
                else min(max(20, total_bars // 2), 60)
            )
            max_start = total_bars - 15
            if max_start + 1 <= cohort_config.max_rolling_starts:
                starts = list(range(0, max_start + 1))
            else:
                raw_idx = np.linspace(0, max_start, cohort_config.max_rolling_starts)
                starts = sorted({int(round(float(x))) for x in raw_idx})

            for s_idx in starts:
                end_idx = min(total_bars, s_idx + horizon)
                sub_cache = cache.slice(s_idx, end_idx)
                sub_res = run_systematic_backtest(
                    sub_cache.bars,
                    baseline_cfg,
                    ticker=ticker,
                    fast_cache=sub_cache,
                    need_greeks=False,
                    collect_steps=False,
                )
                rolling_points.append(
                    RollingStartCohortPoint(
                        start_idx=s_idx,
                        start_date=sub_cache.entries[0].bar.market_date,
                        end_date=sub_cache.entries[-1].bar.market_date,
                        bars_count=len(sub_cache),
                        trade_count=sub_res.summary.trade_count,
                        closed_trades=sub_res.summary.closed_trades,
                        total_return_pct=finite_float(sub_res.summary.total_return_pct, 2),
                        net_pnl_dollars=finite_float(sub_res.summary.net_pnl_dollars, 2),
                        max_drawdown_pct=finite_float(sub_res.summary.max_drawdown_pct, 2),
                        win_rate_pct=clean_float(sub_res.summary.win_rate_pct, 2),
                        expectancy_dollars=clean_float(sub_res.summary.expectancy_dollars, 2),
                    )
                )

    ret_p10 = ret_p50 = ret_p90 = dd_p50 = dd_p90 = None
    if rolling_points:
        rets = np.array([p.total_return_pct for p in rolling_points], dtype=np.float64)
        dds = np.array([p.max_drawdown_pct for p in rolling_points], dtype=np.float64)
        ret_p10 = clean_float(float(np.percentile(rets, 10.0)), 2)
        ret_p50 = clean_float(float(np.percentile(rets, 50.0)), 2)
        ret_p90 = clean_float(float(np.percentile(rets, 90.0)), 2)
        dd_p50 = clean_float(float(np.percentile(dds, 50.0)), 2)
        dd_p90 = clean_float(float(np.percentile(dds, 90.0)), 2)

    sharpe, sortino, calmar = compute_return_risk_ratios(
        daily_returns,
        total_return_pct=base_res.summary.total_return_pct,
        max_drawdown_pct=base_res.summary.max_drawdown_pct,
    )
    report = AllDaysCohortReport(
        mode="ROLLING_START_STATEFUL",
        total_bars=total_bars,
        attempted_entries=base_res.summary.trade_count + base_res.summary.skipped_entries_count,
        entered_cohorts=base_res.summary.trade_count,
        closed_cohorts=base_res.summary.closed_trades,
        open_cohorts=base_res.summary.open_trades,
        unresolved_cohorts=sum(1 for t in base_res.trades if t.status == "UNRESOLVED"),
        skipped_entries_count=base_res.summary.skipped_entries_count,
        skipped_by_reason=base_res.summary.skipped_by_reason,
        avg_concurrent_positions=1.0,
        win_rate_pct=clean_float(base_res.summary.win_rate_pct, 2),
        expectancy_dollars=clean_float(base_res.summary.expectancy_dollars, 2),
        avg_return_on_risk_pct=clean_float(sum(closed_rors) / len(closed_rors), 2)
        if closed_rors
        else None,
        profit_factor=clean_float(base_res.summary.profit_factor, 4),
        total_net_pnl_dollars=finite_float(base_res.summary.net_pnl_dollars, 2),
        portfolio_total_return_pct=finite_float(base_res.summary.total_return_pct, 2),
        portfolio_max_drawdown_pct=finite_float(base_res.summary.max_drawdown_pct, 2),
        portfolio_sharpe=sharpe,
        portfolio_sortino=sortino,
        portfolio_calmar=calmar,
        equity_curve=base_res.equity_curve,
        weekday_stability=wd_rows,
        weekday_skew_detected=skew_detected,
        weekday_skew_message=skew_msg,
        rolling_cohorts=rolling_points,
        rolling_return_p10_pct=ret_p10,
        rolling_return_p50_pct=ret_p50,
        rolling_return_p90_pct=ret_p90,
        rolling_max_dd_p50_pct=dd_p50,
        rolling_max_dd_p90_pct=dd_p90,
        notes=notes,
    )
    return CohortRunOutput(
        report=report,
        daily_returns=daily_returns,
        closed_trade_pnls=np.array(closed_pnls, dtype=np.float64),
        slippage_pairs=None,
    )


def run_cohort_analysis(
    cache: FastBarCache,
    config: BacktestConfig,
    *,
    ticker: str,
    cohort_config: CohortConfig | None = None,
    include_rolling: bool = True,
    matrix_cache: dict[tuple[object, ...], SpreadCohortMatrix] | None = None,
) -> CohortRunOutput:
    """Run All-Days spread cohorts or stateful rolling-start cohorts over `cache`."""
    if config.strategy_kind == "CUSTOM":
        raise BacktestValidationError(
            "Backtester Robustness Suite does not support CUSTOM strategy kind."
        )
    c_cfg = cohort_config or CohortConfig()
    if config.strategy_kind in STATEFUL_STRATEGIES:
        return _run_stateful_rolling_cohorts(
            cache, config, ticker=ticker, cohort_config=c_cfg, include_rolling=include_rolling
        )
    matrix = get_or_build_spread_matrix(cache, config, matrix_cache)
    return evaluate_cohort_matrix(matrix, config, cache=cache)
