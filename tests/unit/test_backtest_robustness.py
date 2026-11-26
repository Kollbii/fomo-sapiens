"""Unit tests for the Backtester Robustness Suite (cohorts, sensitivity, WFO, Monte Carlo)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest
from pydantic import ValidationError

from src.analytics.backtest import (
    BacktestConfig,
    CohortConfig,
    FastBarCache,
    MonteCarloConfig,
    RobustnessConfig,
    SensitivityConfig,
    WalkForwardConfig,
    build_spread_cohort_matrix,
    evaluate_cohort_matrix,
    run_cohort_analysis,
    run_monte_carlo_analysis,
    run_robustness_suite,
    run_sensitivity_grid,
    run_walk_forward_analysis,
)
from src.analytics.bsm_fallback import DAYS_PER_YEAR, DEFAULT_RISK_FREE_RATE, bsm_greeks, bsm_price
from src.core.exceptions import BacktestValidationError
from src.storage.backtest_store import DailyChainBar
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, OptionRight, SnapshotMeta


def _trading_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    cur = start
    while len(days) < count:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _make_chain_df(
    *,
    snapshot_id: str,
    ts_utc: datetime,
    market_date: str,
    spot: float = 100.0,
    strikes: tuple[float, ...] = (85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0),
    expiries: tuple[tuple[str, int], ...],
    iv: float = 0.32,
    spread_half: float = 0.05,
    put_quote_bias: float = 0.0,
) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    conid = 40_000
    ts_str = ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    for expiry, dte in expiries:
        t_years = max(float(dte), 0.5) / DAYS_PER_YEAR
        for strike in strikes:
            for right in ("C", "P"):
                right_lit: OptionRight = "C" if right == "C" else "P"
                conid += 1
                s_arr = np.array([spot], dtype=np.float64)
                k_arr = np.array([strike], dtype=np.float64)
                t_arr = np.array([t_years], dtype=np.float64)
                iv_arr = np.array([iv], dtype=np.float64)
                c_arr = np.array([right_lit == "C"], dtype=np.bool_)
                theo = float(
                    bsm_price(s_arr, k_arr, t_arr, DEFAULT_RISK_FREE_RATE, iv_arr, c_arr)[0]
                )
                if right_lit == "P":
                    theo = max(0.10, theo + put_quote_bias)
                d_arr, g_arr, th_arr, v_arr = bsm_greeks(
                    s_arr, k_arr, t_arr, DEFAULT_RISK_FREE_RATE, iv_arr, c_arr
                )
                bid = round(max(0.05, theo - spread_half), 2)
                ask = round(max(0.10, theo + spread_half), 2)
                mid = round((bid + ask) / 2.0, 2)
                oi = 800
                if (right_lit == "P" and abs(strike - 95.0) < 0.01) or (
                    right_lit == "C" and abs(strike - 105.0) < 0.01
                ):
                    oi = 12_000
                rows.append(
                    {
                        "snapshot_id": snapshot_id,
                        "snapshot_ts_utc": ts_str,
                        "market_date": market_date,
                        "market_data_type": 3,
                        "underlying": "NVDA",
                        "underlying_conid": 4815,
                        "underlying_price": spot,
                        "option_conid": conid,
                        "expiry": expiry,
                        "dte": dte,
                        "strike": strike,
                        "moneyness_pct": round((strike - spot) / spot * 100.0, 4),
                        "right": right_lit,
                        "bid": bid,
                        "ask": ask,
                        "mid": mid,
                        "last": mid,
                        "close": round(max(0.05, mid - 0.03), 2),
                        "mark_price": mid,
                        "volume": 250,
                        "open_interest": oi,
                        "iv": iv,
                        "delta": float(d_arr[0]),
                        "gamma": float(g_arr[0]),
                        "theta": float(th_arr[0]),
                        "vega": float(v_arr[0]),
                        "greeks_source": "IBKR_MODEL",
                    }
                )
    return pl.DataFrame(rows, schema=OPTION_CHAIN_POLARS_SCHEMA)


def _build_bars(
    num_days: int,
    *,
    spot_fn: Any | None = None,
    put_bias_fn: Any | None = None,
    spread_half: float = 0.08,
) -> list[DailyChainBar]:
    start = date(2026, 1, 5)
    days = _trading_days(start, num_days)
    # Weekly Friday expiries covering the entire horizon + 70 days beyond
    fridays: list[date] = []
    cur = start
    end_horizon = days[-1] + timedelta(days=75)
    while cur <= end_horizon:
        if cur.weekday() == 4:
            fridays.append(cur)
        cur += timedelta(days=1)

    bars: list[DailyChainBar] = []
    for idx, day_d in enumerate(days):
        spot = float(
            spot_fn(idx, day_d)
            if spot_fn
            else round(100.0 + 0.08 * idx + 1.5 * np.sin(idx * 0.3), 2)
        )
        bias = float(put_bias_fn(idx, day_d) if put_bias_fn else 0.0)
        market_date = day_d.isoformat()
        ts_utc = datetime(day_d.year, day_d.month, day_d.day, 20, 30, tzinfo=UTC)
        snapshot_id = f"NVDA_{day_d.strftime('%Y%m%d')}_203000"
        exps = tuple(
            (e.isoformat(), (e - day_d).days) for e in fridays if 0 <= (e - day_d).days <= 75
        )
        df = _make_chain_df(
            snapshot_id=snapshot_id,
            ts_utc=ts_utc,
            market_date=market_date,
            spot=spot,
            expiries=exps,
            spread_half=spread_half,
            put_quote_bias=bias,
        )
        meta = SnapshotMeta(
            snapshot_id=snapshot_id,
            file_path=f"{snapshot_id}.csv",
            snapshot_ts_utc=ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            market_date=market_date,
            market_data_type=3,
            underlying_price=spot,
            contract_count=df.height,
            expirations=sorted({e[0] for e in exps}),
            spot_source="LAST",
            quoted_contract_count=df.height,
            quote_coverage=1.0,
            prior_close_only_count=0,
        )
        bars.append(
            DailyChainBar(
                snapshot_id=snapshot_id,
                ts_utc=ts_utc,
                market_date=market_date,
                df=df,
                meta=meta,
            )
        )
    return bars


def test_robustness_config_validation_and_custom_rejection() -> None:
    with pytest.raises(ValidationError):
        SensitivityConfig(x_param="profit_target_pct", y_param="profit_target_pct")
    with pytest.raises(ValidationError):
        SensitivityConfig(x_values=[50.0])
    with pytest.raises(ValidationError):
        SensitivityConfig(
            x_values=[10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0],
            grid_density=7,
        )
    with pytest.raises(ValidationError):
        MonteCarloConfig(slippage_min=0.8, slippage_max=0.2)

    bars = _build_bars(25)
    with pytest.raises(BacktestValidationError):
        run_robustness_suite(bars, BacktestConfig(strategy_kind="CUSTOM"), ticker="NVDA")


def test_all_days_spread_cohorts_and_matrix_stages() -> None:
    bars = _build_bars(65)
    cache = FastBarCache.from_bars(bars)
    cfg = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        target_dte_min=20,
        target_dte_max=45,
        profit_target_pct=50.0,
        stop_loss_pct=100.0,
        min_entry_credit_dollars=0.0,
    )
    matrix = build_spread_cohort_matrix(cache, cfg)
    out = evaluate_cohort_matrix(matrix, cfg, cache=cache)
    rep = out.report

    assert rep.mode == "ALL_DAYS_SPREADS"
    assert rep.total_bars == 65
    assert rep.entered_cohorts > 20
    assert rep.closed_cohorts > 10
    assert rep.avg_concurrent_positions >= 1.0
    assert len(rep.equity_curve) == 65
    assert len(out.daily_returns) == 65
    assert len(rep.weekday_stability) == 5
    assert [r.weekday_name for r in rep.weekday_stability] == ["Mon", "Tue", "Wed", "Thu", "Fri"]
    assert rep.portfolio_sharpe is not None
    assert rep.expectancy_dollars is not None


def test_weekday_skew_detection() -> None:
    # Friday entries occur near local troughs (spot=96, rebound to 105 on Mon-Thu),
    # while Mon-Thu entries occur at 105 and experience Friday drops to 96 (hitting SL).
    def _skew_spot(_idx: int, d: date) -> float:
        return 96.0 if d.weekday() == 4 else 105.0

    bars = _build_bars(60, spot_fn=_skew_spot)
    cache = FastBarCache.from_bars(bars)
    cfg = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        target_dte_min=20,
        target_dte_max=40,
        profit_target_pct=25.0,
        stop_loss_pct=60.0,
        min_entry_credit_dollars=0.0,
    )
    out = run_cohort_analysis(cache, cfg, ticker="NVDA")
    rep = out.report
    fri = rep.weekday_stability[4]
    mon = rep.weekday_stability[0]
    assert fri.closed_trades >= 2
    assert mon.closed_trades >= 2
    assert (fri.expectancy_dollars or 0.0) > (mon.expectancy_dollars or 0.0)
    assert rep.weekday_skew_detected is True
    assert rep.weekday_skew_message is not None


@pytest.mark.parametrize("strategy_kind", ["COVERED_CALL", "WHEEL"])
def test_stateful_rolling_cohorts_cc_and_wheel(strategy_kind: Any) -> None:
    bars = _build_bars(65)
    cache = FastBarCache.from_bars(bars)
    cfg = BacktestConfig(
        strategy_kind=strategy_kind,
        target_dte_min=20,
        target_dte_max=40,
        min_entry_credit_dollars=0.0,
    )
    out = run_cohort_analysis(
        cache,
        cfg,
        ticker="NVDA",
        cohort_config=CohortConfig(rolling_horizon_bars=25, max_rolling_starts=12),
    )
    rep = out.report
    assert rep.mode == "ROLLING_START_STATEFUL"
    assert len(rep.rolling_cohorts) == 12
    assert rep.rolling_return_p10_pct is not None
    assert rep.rolling_return_p50_pct is not None
    assert rep.rolling_return_p90_pct is not None
    assert rep.rolling_return_p10_pct <= rep.rolling_return_p50_pct <= rep.rolling_return_p90_pct
    assert rep.rolling_max_dd_p50_pct is not None
    assert rep.rolling_max_dd_p90_pct is not None
    assert rep.rolling_max_dd_p50_pct <= rep.rolling_max_dd_p90_pct


def test_sensitivity_grid_plateau_and_dsr() -> None:
    bars = _build_bars(65)
    cache = FastBarCache.from_bars(bars)
    cfg = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        target_dte_min=20,
        target_dte_max=45,
        min_entry_credit_dollars=0.0,
    )
    sens_cfg = SensitivityConfig(
        x_param="profit_target_pct",
        y_param="dte_bucket",
        grid_density=5,
        objective="expectancy_dollars",
    )
    rep, opt_cell = run_sensitivity_grid(cache, cfg, sens_cfg, ticker="NVDA")
    assert rep.total_cells_count == 25
    assert len(rep.cells) == 25
    assert rep.valid_cells_count > 0
    assert 0.0 <= rep.plateau_stability_score <= 100.0
    assert 0.0 <= rep.profitable_cells_pct <= 100.0
    assert opt_cell is not None and opt_cell.is_optimal is True
    assert sum(1 for c in rep.cells if c.is_optimal) == 1
    assert sum(1 for c in rep.cells if c.is_baseline) == 1
    assert rep.deflated_sharpe_ratio is not None
    assert 0.0 <= rep.deflated_sharpe_ratio <= 1.0


def test_purged_wfo_zero_lookahead_proof() -> None:
    bars = _build_bars(85)
    cfg = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        target_dte_min=20,
        target_dte_max=45,
        min_entry_credit_dollars=0.0,
    )
    wf_cfg = WalkForwardConfig(
        mode="PURGED_WFO",
        window_type="ROLLING",
        is_bars=40,
        oos_bars=15,
        embargo_bars=5,
        objective="expectancy_dollars",
    )
    sens_cfg = SensitivityConfig(grid_density=5)

    cache_orig = FastBarCache.from_bars(bars)
    rep_orig = run_walk_forward_analysis(
        cache_orig, cfg, wf_cfg, ticker="NVDA", sensitivity_config=sens_cfg
    )
    assert len(rep_orig.folds) >= 2
    assert len(rep_orig.stitched_oos_curve) == sum(f.oos_bars for f in rep_orig.folds)

    # Mutate all bars at index >= 40 (embargo + OOS of fold 0): crash spot to 40.0 and wipe chains!
    mutated_bars = list(bars)
    for idx in range(40, len(mutated_bars)):
        b = mutated_bars[idx]
        mutated_bars[idx] = DailyChainBar(
            snapshot_id=b.snapshot_id,
            ts_utc=b.ts_utc,
            market_date=b.market_date,
            df=b.df.head(0),
            meta=b.meta.model_copy(update={"underlying_price": 40.0, "contract_count": 0})
            if b.meta
            else None,
        )
    cache_mut = FastBarCache.from_bars(mutated_bars)
    rep_mut = run_walk_forward_analysis(
        cache_mut, cfg, wf_cfg, ticker="NVDA", sensitivity_config=sens_cfg
    )
    # Fold 0's IS window [0:40] MUST produce bit-identical selected_params and IS metrics!
    assert rep_mut.folds[0].selected_params == rep_orig.folds[0].selected_params
    assert rep_mut.folds[0].is_objective_score == rep_orig.folds[0].is_objective_score
    assert rep_mut.folds[0].is_return_pct == rep_orig.folds[0].is_return_pct

    # Also verify ANCHORED and FIXED_WINDOWS modes
    rep_anchored = run_walk_forward_analysis(
        cache_orig,
        cfg,
        wf_cfg.model_copy(update={"window_type": "ANCHORED"}),
        ticker="NVDA",
        sensitivity_config=sens_cfg,
    )
    assert len(rep_anchored.folds) >= 2
    assert rep_anchored.folds[1].is_bars > rep_anchored.folds[0].is_bars

    rep_fixed = run_walk_forward_analysis(
        cache_orig,
        cfg,
        WalkForwardConfig(mode="FIXED_WINDOWS", fixed_window_bars=25),
        ticker="NVDA",
    )
    assert len(rep_fixed.folds) >= 3


def test_monte_carlo_determinism_clustering_and_slippage_monotonicity() -> None:
    # 1. Determinism & fan downsampling <= 250
    rng_src = np.random.default_rng(7)
    daily_r = rng_src.normal(0.001, 0.01, size=80)
    trade_pnls = np.array([45.0, -30.0, 55.0, 40.0, -50.0, 60.0, 35.0, -20.0, 50.0, 42.0])

    mc1 = run_monte_carlo_analysis(
        daily_r,
        trade_pnls,
        initial_capital=10_000.0,
        mc_config=MonteCarloConfig(num_paths=400, block_size=5, horizon_bars=400, seed=42),
    )
    mc2 = run_monte_carlo_analysis(
        daily_r,
        trade_pnls,
        initial_capital=10_000.0,
        mc_config=MonteCarloConfig(num_paths=400, block_size=5, horizon_bars=400, seed=42),
    )
    mc3 = run_monte_carlo_analysis(
        daily_r,
        trade_pnls,
        initial_capital=10_000.0,
        mc_config=MonteCarloConfig(num_paths=400, block_size=5, horizon_bars=400, seed=99),
    )
    assert mc1.terminal_return_p50_pct == mc2.terminal_return_p50_pct
    assert mc1.fan_series == mc2.fan_series
    assert mc1.max_dd_p95_pct == mc2.max_dd_p95_pct
    assert mc1.terminal_return_p50_pct != mc3.terminal_return_p50_pct
    assert len(mc1.fan_series.bar_offsets) <= 250
    assert mc1.trade_expectancy_ci_low_dollars is not None
    assert mc1.trade_expectancy_ci_high_dollars is not None
    mean_pnl = float(np.mean(trade_pnls))
    assert mc1.trade_expectancy_ci_low_dollars <= mean_pnl <= mc1.trade_expectancy_ci_high_dollars
    assert mc1.max_losing_streak_p95 is not None and mc1.max_losing_streak_p95 >= 1

    # 2. Block clustering check: autocorrelated regimes [+0.01]*15 + [-0.01]*15
    # With block_size=5, streaks stay clustered so terminal spread is wider than IID.
    clustered_r = np.array([0.01] * 15 + [-0.01] * 15, dtype=np.float64)
    mc_iid = run_monte_carlo_analysis(
        clustered_r,
        trade_pnls,
        initial_capital=10_000.0,
        mc_config=MonteCarloConfig(num_paths=1000, block_size=1, horizon_bars=30, seed=42),
    )
    mc_blk = run_monte_carlo_analysis(
        clustered_r,
        trade_pnls,
        initial_capital=10_000.0,
        mc_config=MonteCarloConfig(num_paths=1000, block_size=5, horizon_bars=30, seed=42),
    )
    spread_iid = mc_iid.terminal_return_p95_pct - mc_iid.terminal_return_p05_pct
    spread_blk = mc_blk.terminal_return_p95_pct - mc_blk.terminal_return_p05_pct
    assert spread_blk > spread_iid * 1.25

    # 3. Slippage monotonicity across real spread cohorts (MID and NATURAL_SPREAD modes)
    bars = _build_bars(60, spread_half=0.12)
    cache = FastBarCache.from_bars(bars)
    for close_mode in ("MID", "NATURAL_SPREAD"):
        cfg = BacktestConfig(
            strategy_kind="BULL_PUT_SPREAD",
            target_dte_min=20,
            target_dte_max=45,
            close_price_mode=close_mode,
            min_entry_credit_dollars=0.0,
        )
        cohort_out = run_cohort_analysis(cache, cfg, ticker="NVDA")
        assert cohort_out.slippage_pairs
        mc_slip = run_monte_carlo_analysis(
            cohort_out.daily_returns,
            cohort_out.closed_trade_pnls,
            initial_capital=cfg.initial_capital,
            mc_config=MonteCarloConfig(num_paths=200, seed=42),
            slippage_pairs=cohort_out.slippage_pairs,
        )
        assert mc_slip.slippage_stress is not None
        mid_exp = mc_slip.slippage_stress.mid_expectancy_dollars
        blend_exp = mc_slip.slippage_stress.blended_expectancy_dollars
        nat_exp = mc_slip.slippage_stress.natural_expectancy_dollars
        assert mid_exp is not None and blend_exp is not None and nat_exp is not None
        assert nat_exp <= blend_exp <= mid_exp
        assert nat_exp < mid_exp


def test_short_history_graceful_and_selective_sections() -> None:
    short_bars = _build_bars(25)
    cfg = BacktestConfig(
        strategy_kind="IRON_CONDOR",
        target_dte_min=15,
        target_dte_max=45,
        min_entry_credit_dollars=0.0,
    )
    res = run_robustness_suite(
        short_bars,
        cfg,
        ticker="NVDA",
        robustness_config=RobustnessConfig(
            monte_carlo=MonteCarloConfig(num_paths=200, seed=42),
        ),
    )
    assert res.total_bars == 25
    assert res.data_sufficiency_tier == "LIMITED"
    assert res.compute_ms >= 0.0
    assert res.cohorts is not None
    assert res.sensitivity is not None
    assert res.walk_forward is not None
    assert res.walk_forward.folds == []
    assert res.walk_forward.sample_depth_warning is True
    assert res.monte_carlo is not None
    assert len(res.verdicts) == 4
    assert res.verdicts[2].status == "WARN"

    # Selective sections
    partial = run_robustness_suite(
        short_bars,
        cfg,
        ticker="NVDA",
        robustness_config=RobustnessConfig(
            monte_carlo=MonteCarloConfig(num_paths=200, seed=42),
        ),
        sections=["COHORTS", "MONTE_CARLO"],
    )
    assert partial.requested_sections == ["COHORTS", "MONTE_CARLO"]
    assert partial.cohorts is not None
    assert partial.monte_carlo is not None
    assert partial.sensitivity is None
    assert partial.walk_forward is None
    verdict_map = {v.section: v.status for v in partial.verdicts}
    assert verdict_map["SENSITIVITY"] == "SKIPPED"
    assert verdict_map["WALK_FORWARD"] == "SKIPPED"


def test_spread_cohort_matrix_slice_exact_parity() -> None:
    bars = _build_bars(65)
    cache = FastBarCache.from_bars(bars)
    cfg = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        target_dte_min=20,
        target_dte_max=45,
        profit_target_pct=50.0,
        stop_loss_pct=100.0,
        min_entry_credit_dollars=0.0,
    )
    full_matrix = build_spread_cohort_matrix(cache, cfg)

    for start, end in ((0, 35), (7, 42), (19, 65), (25, 40), (10, 11)):
        sliced_matrix = full_matrix.slice(start, end)
        sub_cache = cache.slice(start, end)
        rebuilt_matrix = build_spread_cohort_matrix(sub_cache, cfg)

        assert sliced_matrix == rebuilt_matrix

        out_sliced = evaluate_cohort_matrix(sliced_matrix, cfg, cache=sub_cache)
        out_rebuilt = evaluate_cohort_matrix(rebuilt_matrix, cfg, cache=sub_cache)
        assert out_sliced.report == out_rebuilt.report
        assert np.array_equal(out_sliced.daily_returns, out_rebuilt.daily_returns)
        assert np.array_equal(out_sliced.closed_trade_pnls, out_rebuilt.closed_trade_pnls)
        assert out_sliced.slippage_pairs == out_rebuilt.slippage_pairs


def test_wheel_and_spread_default_sensitivity_axes() -> None:
    from src.analytics.backtest.sensitivity import resolve_sensitivity_axes

    default_sens = SensitivityConfig()
    assert resolve_sensitivity_axes(default_sens) == ("profit_target_pct", "stop_loss_pct")
    assert resolve_sensitivity_axes(default_sens, strategy_kind="BULL_PUT_SPREAD") == (
        "profit_target_pct",
        "stop_loss_pct",
    )
    assert resolve_sensitivity_axes(default_sens, strategy_kind="COVERED_CALL") == (
        "target_delta",
        "profit_target_pct",
    )
    assert resolve_sensitivity_axes(default_sens, strategy_kind="WHEEL") == (
        "target_delta",
        "profit_target_pct",
    )

    bars = _build_bars(30)
    cache = FastBarCache.from_bars(bars)
    wheel_cfg = BacktestConfig(
        strategy_kind="WHEEL",
        target_dte_min=15,
        target_dte_max=45,
        min_entry_credit_dollars=0.0,
    )
    rep, _opt = run_sensitivity_grid(
        cache,
        wheel_cfg,
        SensitivityConfig(grid_density=5),
        ticker="NVDA",
    )
    assert rep.x_param == "target_delta"
    assert rep.y_param == "profit_target_pct"


def test_target_delta_sensitivity_overrides_wall_anchored_and_emits_inactive_notes() -> None:
    from src.analytics.backtest.sensitivity import apply_sensitivity_params

    bars = _build_bars(45)
    cache = FastBarCache.from_bars(bars)

    cc_wall_cfg = BacktestConfig(
        strategy_kind="COVERED_CALL",
        strike_selection_mode="WALL_ANCHORED",
        cc_strike_floor="NONE",
        target_dte_min=15,
        target_dte_max=45,
        min_entry_credit_dollars=0.0,
        stop_loss_pct=None,
    )
    overridden = apply_sensitivity_params(cc_wall_cfg, {"target_delta": 0.15})
    assert overridden.strike_selection_mode == "TARGET_DELTA"
    assert overridden.target_delta == pytest.approx(0.15)

    cc_rep, _opt = run_sensitivity_grid(
        cache,
        cc_wall_cfg,
        SensitivityConfig(
            x_param="target_delta",
            y_param="profit_target_pct",
            x_values=[0.10, 0.25, 0.45],
            y_values=[30.0, 60.0],
        ),
        ticker="NVDA",
    )
    # Cells across target_delta must NOT be completely identical since WALL_ANCHORED is overridden
    row_x_returns = {
        cell.x_value: cell.total_return_pct for cell in cc_rep.cells if cell.y_index == 0
    }
    assert len(set(row_x_returns.values())) > 1
    assert not any("Axis 'target_delta'" in n for n in cc_rep.notes)

    bps_cfg = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        target_dte_min=15,
        target_dte_max=45,
        min_entry_credit_dollars=0.0,
    )
    bps_rep, _ = run_sensitivity_grid(
        cache,
        bps_cfg,
        SensitivityConfig(
            x_param="target_delta",
            y_param="profit_target_pct",
            x_values=[0.15, 0.30],
            y_values=[40.0, 60.0],
        ),
        ticker="NVDA",
    )
    assert any(
        "Axis 'target_delta' only affects COVERED_CALL and WHEEL; "
        "cells across this axis are invariant." in n
        for n in bps_rep.notes
    )

    ror_rep, _ = run_sensitivity_grid(
        cache,
        cc_wall_cfg,
        SensitivityConfig(
            x_param="min_entry_ror_pct",
            y_param="profit_target_pct",
            x_values=[0.0, 20.0],
            y_values=[40.0, 60.0],
        ),
        ticker="NVDA",
    )
    assert any(
        "Axis 'min_entry_ror_pct' only filters BULL_PUT_SPREAD entries; "
        "cells across this axis are invariant." in n
        for n in ror_rep.notes
    )

    ic_cfg = BacktestConfig(
        strategy_kind="IRON_CONDOR",
        target_dte_min=15,
        target_dte_max=45,
        min_entry_credit_dollars=0.0,
    )
    credit_rep, _ = run_sensitivity_grid(
        cache,
        ic_cfg,
        SensitivityConfig(
            x_param="min_entry_credit_dollars",
            y_param="profit_target_pct",
            x_values=[0.0, 100.0],
            y_values=[40.0, 60.0],
        ),
        ticker="NVDA",
    )
    assert any(
        "Axis 'min_entry_credit_dollars' only filters BULL_PUT_SPREAD, "
        "COVERED_CALL, and WHEEL entries." in n
        for n in credit_rep.notes
    )


def test_stateful_cohorts_negative_equity_caps_returns_and_propagates_flag() -> None:
    def crash_spot(idx: int, _day: date) -> float:
        if idx == 0:
            return 100.0
        return max(25.0, 100.0 - 20.0 * idx)

    bars = _build_bars(20, spot_fn=crash_spot)
    cache = FastBarCache.from_bars(bars)
    cfg = BacktestConfig(
        strategy_kind="COVERED_CALL",
        initial_capital=1000.0,
        target_dte_min=15,
        target_dte_max=45,
        profit_target_pct=None,
        stop_loss_pct=None,
        min_entry_credit_dollars=0.0,
    )
    out = run_cohort_analysis(cache, cfg, ticker="NVDA", include_rolling=False)
    assert out.report.portfolio_max_drawdown_pct == pytest.approx(100.0)
    assert any("CAPITAL_DEPLETED" in n for n in out.report.notes)
    assert np.all(out.daily_returns >= -0.9999)
    assert np.all(np.isfinite(out.daily_returns))
    # Subsequent depleted bars after the crash bar must be 0.0 (not sign-inverted positive returns)
    depleted_indices = [
        i for i, pt in enumerate(out.report.equity_curve) if pt.equity_dollars <= 0.0
    ]
    assert len(depleted_indices) >= 2
    assert out.daily_returns[depleted_indices[0]] == pytest.approx(-0.9999)
    for idx in depleted_indices[1:]:
        assert out.daily_returns[idx] == pytest.approx(0.0)
