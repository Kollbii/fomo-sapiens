"""Unit tests for lightweight strategy bounds and FastBarCache parity/isolation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
import pytest

from src.analytics.backtest import (
    BacktestConfig,
    FastBarCache,
    run_systematic_backtest,
)
from src.analytics.backtest.engine import _simulate_bars
from src.analytics.backtest.pricing import evaluate_trade_step_on_bar, lookup_legs_on_bar
from src.analytics.bsm_fallback import DAYS_PER_YEAR, DEFAULT_RISK_FREE_RATE, bsm_greeks, bsm_price
from src.analytics.strategies import evaluate_strategy
from src.analytics.strategies.engine import strategy_risk_metrics
from src.storage.backtest_store import DailyChainBar
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, OptionRight, SnapshotMeta


def _make_chain_df(
    *,
    snapshot_id: str,
    ts_utc: datetime,
    market_date: str,
    spot: float = 100.0,
    strikes: tuple[float, ...] = (85.0, 90.0, 95.0, 100.0, 105.0, 110.0, 115.0),
    expiries: tuple[tuple[str, int], ...] = (("2026-06-19", 38), ("2026-07-17", 66)),
    iv: float = 0.35,
    omit_greeks: bool = False,
) -> pl.DataFrame:
    rows: list[dict[str, Any]] = []
    conid = 30_000
    ts_str = ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    day_d = date.fromisoformat(market_date)
    for expiry, explicit_dte in expiries:
        dte = (
            explicit_dte if explicit_dte >= 0 else max(0, (date.fromisoformat(expiry) - day_d).days)
        )
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
                d_arr, g_arr, th_arr, v_arr = bsm_greeks(
                    s_arr, k_arr, t_arr, DEFAULT_RISK_FREE_RATE, iv_arr, c_arr
                )
                bid = round(max(0.05, theo - 0.05), 2)
                ask = round(max(0.10, theo + 0.05), 2)
                mid = round((bid + ask) / 2.0, 2)
                oi = 800
                if right_lit == "P" and abs(strike - 95.0) < 0.01:
                    oi = 10_000
                elif right_lit == "C" and abs(strike - 105.0) < 0.01:
                    oi = 11_000
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
                        "close": round(max(0.05, mid - 0.04), 2),
                        "mark_price": mid,
                        "volume": 200,
                        "open_interest": oi,
                        "iv": None if omit_greeks else iv,
                        "delta": None if omit_greeks else float(d_arr[0]),
                        "gamma": None if omit_greeks else float(g_arr[0]),
                        "theta": None if omit_greeks else float(th_arr[0]),
                        "vega": None if omit_greeks else float(v_arr[0]),
                        "greeks_source": "NONE" if omit_greeks else "IBKR_MODEL",
                    }
                )
    return pl.DataFrame(rows, schema=OPTION_CHAIN_POLARS_SCHEMA)


def _trading_days(start: date, count: int) -> list[date]:
    days: list[date] = []
    cur = start
    while len(days) < count:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


def _build_multi_bar_series(
    num_days: int = 35, *, omit_greeks: bool = False
) -> list[DailyChainBar]:
    start = date(2026, 4, 6)
    days = _trading_days(start, num_days)
    # Build two monthly expiries ~38d and ~66d out so entries and expirations occur
    exp1 = date(2026, 5, 1)
    exp2 = date(2026, 5, 29)
    exp3 = date(2026, 6, 26)
    bars: list[DailyChainBar] = []
    for idx, day_d in enumerate(days):
        spot = round(100.0 + 3.0 * np.sin(idx * 0.25) + 0.05 * idx, 2)
        market_date = day_d.isoformat()
        ts_utc = datetime(day_d.year, day_d.month, day_d.day, 20, 30, tzinfo=UTC)
        snapshot_id = f"NVDA_{day_d.strftime('%Y%m%d')}_203000"
        exps = tuple(
            (e.isoformat(), (e - day_d).days) for e in (exp1, exp2, exp3) if (e - day_d).days >= 0
        )
        df = _make_chain_df(
            snapshot_id=snapshot_id,
            ts_utc=ts_utc,
            market_date=market_date,
            spot=spot,
            expiries=exps,
            omit_greeks=omit_greeks,
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


def test_evaluate_strategy_lightweight_bounds_match_full() -> None:
    ts = datetime(2026, 5, 11, 20, 0, tzinfo=UTC)
    df = _make_chain_df(
        snapshot_id="NVDA_20260511_200000",
        ts_utc=ts,
        market_date="2026-05-11",
    )
    full = evaluate_strategy(df, strategy_kind="BULL_PUT_SPREAD", target_expiry="2026-06-19")
    light = evaluate_strategy(
        df,
        strategy_kind="BULL_PUT_SPREAD",
        target_expiry="2026-06-19",
        include_curves=False,
        include_pop=False,
    )
    assert light.price_grid == []
    assert light.curves == []
    assert light.risk.prob_of_profit_pct is None
    assert light.risk.expected_return_on_risk_pct is None

    assert light.pricing == full.pricing
    assert light.net_greeks == full.net_greeks
    assert light.legs == full.legs
    assert light.risk.max_profit == full.risk.max_profit
    assert light.risk.max_loss == full.risk.max_loss
    assert light.risk.max_loss_unbounded == full.risk.max_loss_unbounded
    assert light.risk.return_on_risk_pct == full.risk.return_on_risk_pct
    assert light.risk.breakevens == full.risk.breakevens
    assert light.risk.expected_move_1sigma_pct == full.risk.expected_move_1sigma_pct
    assert light.risk.expected_move_1sigma_low == full.risk.expected_move_1sigma_low
    assert light.risk.expected_move_1sigma_high == full.risk.expected_move_1sigma_high
    assert light.risk.nearest_breakeven_distance_pct == full.risk.nearest_breakeven_distance_pct


def test_strategy_risk_metrics_default_pop_grid_none() -> None:
    from src.analytics.strategies.chain import find_row, prepare_rows, row_lookup_map
    from src.analytics.strategies.engine import build_leg_states
    from src.analytics.strategies.models import StrategyLegInput

    ts = datetime(2026, 5, 11, 20, 0, tzinfo=UTC)
    df = _make_chain_df(snapshot_id="NVDA_20260511_200000", ts_utc=ts, market_date="2026-05-11")
    _, spot, rows = prepare_rows(df)
    lookup = row_lookup_map(rows)
    legs = [
        StrategyLegInput(side="SELL", right="P", expiry="2026-06-19", strike=95.0, qty=1),
        StrategyLegInput(side="BUY", right="P", expiry="2026-06-19", strike=90.0, qty=1),
    ]
    matched = [(leg, find_row(lookup, leg.expiry, leg.strike, leg.right)) for leg in legs]
    states = build_leg_states(matched, spot, DEFAULT_RISK_FREE_RATE, 0.35)
    risk = strategy_risk_metrics(
        states,
        spot=spot,
        short_dte=38,
        ref_iv=0.35,
        mid_cash_total=120.0,
        qty_multiplier=1,
        risk_free_rate=DEFAULT_RISK_FREE_RATE,
    )
    assert risk.prob_of_profit_pct is None
    assert risk.expected_return_on_risk_pct is None
    assert risk.max_loss > 0.0


def test_fast_bar_cache_tie_breaking_and_tracked_fallback() -> None:
    ts = datetime(2026, 5, 11, 20, 0, tzinfo=UTC)
    df_base = _make_chain_df(
        snapshot_id="NVDA_20260511_190000",
        ts_utc=ts,
        market_date="2026-05-11",
        strikes=(95.0,),
        expiries=(("2026-06-19", 38),),
        omit_greeks=True,
    )
    # Create duplicate row with higher snapshot_ts_utc & snapshot_id in tracked_df
    tracked_early = df_base.with_columns(
        pl.lit("NVDA_20260511_180000").alias("snapshot_id"),
        pl.lit("2026-05-11T18:00:00Z").alias("snapshot_ts_utc"),
        pl.lit(2.00).alias("bid"),
        pl.lit(2.20).alias("ask"),
        pl.lit(2.10).alias("mid"),
    )
    tracked_late = df_base.with_columns(
        pl.lit("NVDA_20260511_203000").alias("snapshot_id"),
        pl.lit("2026-05-11T20:30:00Z").alias("snapshot_ts_utc"),
        pl.lit(3.00).alias("bid"),
        pl.lit(3.20).alias("ask"),
        pl.lit(3.10).alias("mid"),
    )
    tracked_df = pl.concat([tracked_late, tracked_early], how="vertical")

    # Empty chain -> falls back to tracked_df and picks highest (snapshot_ts_utc, snapshot_id)
    bar = DailyChainBar(
        snapshot_id="NVDA_20260511_203000",
        ts_utc=ts,
        market_date="2026-05-11",
        df=df_base.head(0),
        meta=None,
        tracked_df=tracked_df,
    )
    from src.analytics.strategies.models import StrategyLegInput

    legs = [StrategyLegInput(side="SELL", right="P", expiry="2026-06-19", strike=95.0, qty=1)]
    cache = FastBarCache.from_bars([bar])
    cfg = BacktestConfig()

    legacy_found, legacy_sids = lookup_legs_on_bar(
        bar,
        legs,
        price_basis=cfg.price_basis,
        close_price_mode=cfg.close_price_mode,
        risk_free_rate=cfg.risk_free_rate,
    )
    fast_found, fast_sids = cache.lookup_legs_fast(
        0,
        legs,
        price_basis=cfg.price_basis,
        close_price_mode=cfg.close_price_mode,
        risk_free_rate=cfg.risk_free_rate,
        need_greeks=True,
    )
    assert fast_sids == legacy_sids == ["NVDA_20260511_203000"]
    assert fast_found[0][1] == legacy_found[0][1] == "TRACKED"
    assert fast_found[0][0]["bid"] == 3.00
    assert fast_found[0][0]["greeks_source"] == legacy_found[0][0]["greeks_source"]
    assert fast_found[0][0]["iv"] == pytest.approx(legacy_found[0][0]["iv"], abs=1e-6)

    # need_greeks=False skips BSM enrichment
    fast_nogk, _ = cache.lookup_legs_fast(
        0,
        legs,
        price_basis=cfg.price_basis,
        close_price_mode=cfg.close_price_mode,
        risk_free_rate=cfg.risk_free_rate,
        need_greeks=False,
    )
    assert fast_nogk[0][0]["greeks_source"] == "NONE"

    # Without snapshot_ts_utc column (with native greeks), keeps FIRST row just like _match_row
    tracked_no_ts = (
        _make_chain_df(
            snapshot_id="NVDA_20260511_190000",
            ts_utc=ts,
            market_date="2026-05-11",
            strikes=(95.0,),
            expiries=(("2026-06-19", 38),),
            omit_greeks=False,
        )
        .drop("snapshot_ts_utc")
        .with_columns(pl.lit(4.00).alias("bid"), pl.lit(4.20).alias("ask"))
    )
    tracked_no_ts_second = tracked_no_ts.with_columns(
        pl.lit(1.00).alias("bid"), pl.lit(1.20).alias("ask")
    )
    bar_no_ts = DailyChainBar(
        snapshot_id="NVDA_20260511_203000",
        ts_utc=ts,
        market_date="2026-05-11",
        df=df_base.head(0),
        meta=None,
        tracked_df=pl.concat([tracked_no_ts, tracked_no_ts_second], how="vertical"),
    )
    cache_no_ts = FastBarCache.from_bars([bar_no_ts])
    step_legacy = evaluate_trade_step_on_bar(bar_no_ts, legs, cfg)
    step_fast = evaluate_trade_step_on_bar(bar_no_ts, legs, cfg, fast_cache=cache_no_ts, bar_idx=0)
    assert step_fast == step_legacy
    assert step_fast.legs[0].price_used == pytest.approx(4.10, abs=1e-4)


@pytest.mark.parametrize(
    ("strategy_kind", "close_mode"),
    [
        ("BULL_PUT_SPREAD", "MID"),
        ("BULL_PUT_SPREAD", "NATURAL_SPREAD"),
        ("IRON_CONDOR", "MID"),
        ("DIAGONAL_SPREAD", "MID"),
        ("COVERED_CALL", "MID"),
        ("WHEEL", "MID"),
    ],
)
def test_fast_bar_cache_exact_backtest_parity(
    strategy_kind: Any,
    close_mode: Any,
) -> None:
    bars = _build_multi_bar_series(num_days=35, omit_greeks=(strategy_kind == "BULL_PUT_SPREAD"))
    is_stateful = strategy_kind in ("COVERED_CALL", "WHEEL")
    cfg = BacktestConfig(
        strategy_kind=strategy_kind,
        target_dte_min=15,
        target_dte_max=45,
        entry_cadence="WHEN_FLAT" if is_stateful else "EVERY_N_DAYS",
        entry_every_n_days=5,
        max_open_trades=1 if is_stateful else 3,
        close_price_mode=close_mode,
        min_entry_credit_dollars=0.0,
        profit_target_pct=50.0,
        stop_loss_pct=100.0,
    )
    ordered = sorted(bars, key=lambda b: (b.ts_utc, b.snapshot_id))
    legacy = _simulate_bars(ordered, cfg, ticker="NVDA", mode="SYSTEMATIC", fast_cache=None)
    cache = FastBarCache.from_bars(bars)
    fast_with_cache = run_systematic_backtest(bars, cfg, ticker="NVDA", fast_cache=cache)
    fast_auto = run_systematic_backtest(bars, cfg, ticker="NVDA")

    assert fast_with_cache.summary == legacy.summary
    assert fast_with_cache.equity_curve == legacy.equity_curve
    assert fast_with_cache.trades == legacy.trades
    assert fast_auto.summary == legacy.summary
    assert fast_auto.trades == legacy.trades


def test_fast_bar_cache_slice_isolation() -> None:
    bars = _build_multi_bar_series(num_days=12)
    cache = FastBarCache.from_bars(bars)
    assert len(cache) == 12
    sub = cache.slice(3, 8)
    assert len(sub) == 5
    assert [b.snapshot_id for b in sub.bars] == [b.snapshot_id for b in cache.bars[3:8]]
    # Verify per-bar scope caching works across slices without crossing bar bounds
    scope = sub.get_entry_scope(
        0,
        dte_min=15,
        dte_max=45,
        risk_free_rate=DEFAULT_RISK_FREE_RATE,
        walls_top_n=3,
        is_diagonal=False,
    )
    assert scope is not None
    scope_full = cache.get_entry_scope(
        3,
        dte_min=15,
        dte_max=45,
        risk_free_rate=DEFAULT_RISK_FREE_RATE,
        walls_top_n=3,
        is_diagonal=False,
    )
    assert scope_full is scope
    front_expiry, enriched_df, walls, underlying, spot, prepared_rows = scope
    assert isinstance(front_expiry, str)
    assert enriched_df.height > 0
    assert walls.underlying == "NVDA"
    assert underlying == "NVDA"
    assert spot > 0.0
    assert len(prepared_rows) == enriched_df.height
    assert sub.entry_candidate_cache is cache.entry_candidate_cache


@pytest.mark.parametrize("strategy_kind", ["COVERED_CALL", "WHEEL"])
def test_fast_metrics_only_mode_exact_parity(strategy_kind: Any) -> None:
    bars = _build_multi_bar_series(num_days=35, omit_greeks=True)
    cfg = BacktestConfig(
        strategy_kind=strategy_kind,
        target_dte_min=15,
        target_dte_max=45,
        entry_cadence="WHEN_FLAT",
        max_open_trades=1,
        min_entry_credit_dollars=0.0,
        profit_target_pct=50.0,
        stop_loss_pct=100.0,
        roll_enabled=(strategy_kind == "WHEEL"),
        roll_credit_mode="ZERO_OR_CREDIT",
    )
    cache = FastBarCache.from_bars(bars)
    full_res = run_systematic_backtest(bars, cfg, ticker="NVDA", fast_cache=cache)
    fast_res = run_systematic_backtest(
        bars,
        cfg,
        ticker="NVDA",
        fast_cache=cache,
        need_greeks=False,
        collect_steps=False,
    )

    assert fast_res.summary == full_res.summary
    assert fast_res.summary.net_pnl_dollars == full_res.summary.net_pnl_dollars
    assert fast_res.summary.total_return_pct == full_res.summary.total_return_pct
    assert fast_res.summary.trade_count == full_res.summary.trade_count
    assert fast_res.summary.closed_trades == full_res.summary.closed_trades
    assert fast_res.summary.win_rate_pct == full_res.summary.win_rate_pct
    assert fast_res.summary.max_drawdown_pct == full_res.summary.max_drawdown_pct
    assert fast_res.summary.expectancy_dollars == full_res.summary.expectancy_dollars
    assert fast_res.summary.profit_factor == full_res.summary.profit_factor
    assert fast_res.equity_curve == full_res.equity_curve
    assert fast_res.notes == full_res.notes
    assert len(fast_res.trades) == len(full_res.trades)
    for fast_tr, full_tr in zip(fast_res.trades, full_res.trades, strict=True):
        assert fast_tr.steps == []
        assert fast_tr.model_copy(update={"steps": full_tr.steps}) == full_tr


def test_single_bar_entry_candidate_memoization_ignores_exit_params() -> None:
    from src.analytics.backtest.rules import build_entry_candidate_outcome

    bars = _build_multi_bar_series(num_days=8)
    cache = FastBarCache.from_bars(bars)
    cfg1 = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        target_dte_min=15,
        target_dte_max=45,
        profit_target_pct=40.0,
        stop_loss_pct=100.0,
        commission_per_contract=0.65,
        min_entry_credit_dollars=0.0,
    )
    cfg2 = cfg1.model_copy(
        update={
            "profit_target_pct": 75.0,
            "stop_loss_pct": 150.0,
            "exit_dte": 14,
            "commission_per_contract": 1.25,
        }
    )
    res1, _, skip1 = build_entry_candidate_outcome(
        cache.entries[0].bar,
        cfg1,
        ctx=cache.entries[0].ctx,
        fast_cache=cache,
        bar_idx=0,
    )
    assert res1 is not None and skip1 is None
    assert len(cache.entry_candidate_cache) == 1

    res2, _, skip2 = build_entry_candidate_outcome(
        cache.entries[0].bar,
        cfg2,
        ctx=cache.entries[0].ctx,
        fast_cache=cache,
        bar_idx=0,
    )
    assert res2 is res1
    assert skip2 is None
    assert len(cache.entry_candidate_cache) == 1


def test_fast_bar_cache_with_fresh_memo_shares_entries_and_isolates_caches() -> None:
    from src.analytics.backtest.rules import build_entry_candidate_outcome
    from src.analytics.strategies.models import StrategyLegInput

    bars = _build_multi_bar_series(num_days=4, omit_greeks=True)
    f1 = FastBarCache.from_bars(bars)
    f2 = f1.with_fresh_memo()

    assert f2 is not f1
    assert f2.entries is f1.entries
    assert f2.bars == f1.bars

    cfg = BacktestConfig(
        strategy_kind="BULL_PUT_SPREAD",
        target_dte_min=15,
        target_dte_max=45,
        min_entry_credit_dollars=0.0,
    )
    assert bars[0].meta is not None
    leg = [
        StrategyLegInput(
            side="SELL",
            right="P",
            expiry=bars[0].meta.expirations[0],
            strike=95.0,
            qty=1,
        )
    ]
    f1.lookup_legs_fast(
        0,
        leg,
        price_basis=cfg.price_basis,
        close_price_mode=cfg.close_price_mode,
        risk_free_rate=cfg.risk_free_rate,
        need_greeks=True,
    )
    f1.get_entry_scope(
        0,
        dte_min=cfg.target_dte_min,
        dte_max=cfg.target_dte_max,
        risk_free_rate=cfg.risk_free_rate,
        walls_top_n=cfg.walls_top_n,
        is_diagonal=False,
    )
    build_entry_candidate_outcome(
        f1.entries[0].bar,
        cfg,
        ctx=f1.entries[0].ctx,
        fast_cache=f1,
        bar_idx=0,
    )

    assert len(f1._enriched_quote_cache) > 0
    assert len(f1._entry_scope_cache) > 0
    assert len(f1._entry_candidate_cache) > 0

    assert f2._enriched_quote_cache == {}
    assert f2._entry_scope_cache == {}
    assert f2._entry_candidate_cache == {}
    assert f2.entry_candidate_cache is not f1.entry_candidate_cache
    assert f2._enriched_quote_cache is not f1._enriched_quote_cache
    assert f2._entry_scope_cache is not f1._entry_scope_cache

    # Mutating f2's memo caches does not affect f1 either
    f2._enriched_quote_cache[("probe",)] = {"probe": 1}
    f2._entry_scope_cache[("probe",)] = None
    f2._entry_candidate_cache[("probe",)] = (None, "PROBE")
    assert ("probe",) not in f1._enriched_quote_cache
    assert ("probe",) not in f1._entry_scope_cache
    assert ("probe",) not in f1._entry_candidate_cache
