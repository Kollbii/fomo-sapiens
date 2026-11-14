from __future__ import annotations

import math
from typing import Any

import numpy as np
import polars as pl
import pytest

from src.analytics.anomalies import AnomalyScanner, scan_anomalies
from src.analytics.bsm_fallback import (
    BSMFallbackEngine,
    bsm_greeks,
    bsm_price,
    enrich_missing_greeks,
    greeks_for_quotes,
)
from src.analytics.term_greeks import MultiDTEStructureAnalyzer, build_multi_dte_profiles
from src.analytics.walls import WallsAnalyzer, compute_walls
from src.storage.schema import COLUMNS, OPTION_CHAIN_POLARS_SCHEMA, OptionContractRow


def _make_row(
    *,
    conid: int,
    right: str = "C",
    spot: float = 100.0,
    strike: float = 100.0,
    dte: int = 37,
    expiry: str = "2026-12-18",
    mark_price: float | None = 3.60,
    volume: int | None = 100,
    open_interest: int | None = None,
    greeks_source: str = "NONE",
    iv: float | None = None,
    delta: float | None = None,
    gamma: float | None = None,
    theta: float | None = None,
    vega: float | None = None,
) -> dict[str, Any]:
    row = OptionContractRow(
        snapshot_id="NVDA_20261111_153000",
        snapshot_ts_utc="2026-11-11T15:30:00Z",
        market_date="2026-11-11",
        market_data_type=3,
        underlying="NVDA",
        underlying_conid=4815,
        underlying_price=spot,
        option_conid=conid,
        expiry=expiry,
        dte=dte,
        strike=strike,
        moneyness_pct=((strike - spot) / spot) * 100.0,
        right=right,  # type: ignore[arg-type]
        bid=mark_price,
        ask=mark_price,
        mid=mark_price,
        last=mark_price,
        close=mark_price,
        mark_price=mark_price,
        volume=volume,
        open_interest=open_interest,
        iv=iv,
        delta=delta,
        gamma=gamma,
        theta=theta,
        vega=vega,
        greeks_source=greeks_source,  # type: ignore[arg-type]
    )
    return row.model_dump()


def _to_df(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=OPTION_CHAIN_POLARS_SCHEMA)


def test_textbook_bsm_call_and_put_roundtrip_and_greeks() -> None:
    s = 100.0
    k = 100.0
    dte = 30
    r = 0.0425
    target_iv = 0.30
    t_years = dte / 365.0

    spot_arr = np.array([s, s], dtype=np.float64)
    strike_arr = np.array([k, k], dtype=np.float64)
    t_arr = np.array([t_years, t_years], dtype=np.float64)
    iv_arr = np.array([target_iv, target_iv], dtype=np.float64)
    is_call_arr = np.array([True, False], dtype=np.bool_)

    prices = bsm_price(spot_arr, strike_arr, t_arr, r, iv_arr, is_call_arr)
    exp_deltas, exp_gammas, exp_thetas, exp_vegas = bsm_greeks(
        spot_arr, strike_arr, t_arr, r, iv_arr, is_call_arr
    )

    # Put-Call parity check on theoretical prices: C - P == S - K * exp(-r * T)
    assert (prices[0] - prices[1]) == pytest.approx(s - k * math.exp(-r * t_years), abs=1e-10)

    df = _to_df(
        [
            _make_row(
                conid=1,
                right="C",
                spot=s,
                strike=k,
                dte=dte,
                mark_price=float(prices[0]),
            ),
            _make_row(
                conid=2,
                right="P",
                spot=s,
                strike=k,
                dte=dte,
                mark_price=float(prices[1]),
            ),
        ]
    )

    enriched = BSMFallbackEngine.enrich_missing_greeks(df, risk_free_rate=r)
    assert enriched.schema == OPTION_CHAIN_POLARS_SCHEMA
    assert tuple(enriched.columns) == COLUMNS

    call_row = enriched.row(0, named=True)
    put_row = enriched.row(1, named=True)

    assert call_row["greeks_source"] == "BSM_FALLBACK"
    assert put_row["greeks_source"] == "BSM_FALLBACK"

    assert call_row["iv"] == pytest.approx(target_iv, abs=1e-4)
    assert put_row["iv"] == pytest.approx(target_iv, abs=1e-4)

    assert call_row["delta"] == pytest.approx(float(exp_deltas[0]), abs=1e-5)
    assert put_row["delta"] == pytest.approx(float(exp_deltas[1]), abs=1e-5)
    assert (call_row["delta"] - put_row["delta"]) == pytest.approx(1.0, abs=1e-6)

    assert call_row["gamma"] == pytest.approx(float(exp_gammas[0]), abs=1e-5)
    assert put_row["gamma"] == pytest.approx(float(exp_gammas[1]), abs=1e-5)
    assert call_row["gamma"] == pytest.approx(put_row["gamma"], abs=1e-6)

    assert call_row["theta"] == pytest.approx(float(exp_thetas[0]), abs=1e-5)
    assert put_row["theta"] == pytest.approx(float(exp_thetas[1]), abs=1e-5)
    assert call_row["theta"] < 0.0

    assert call_row["vega"] == pytest.approx(float(exp_vegas[0]), abs=1e-5)
    assert put_row["vega"] == pytest.approx(float(exp_vegas[1]), abs=1e-5)
    assert call_row["vega"] == pytest.approx(put_row["vega"], abs=1e-6)


def test_preserves_native_ibkr_model_greeks() -> None:
    native_row = _make_row(
        conid=10,
        right="C",
        spot=100.0,
        strike=100.0,
        dte=30,
        mark_price=9.99,  # intentionally different from iv=0.22 BSM price
        greeks_source="IBKR_MODEL",
        iv=0.22,
        delta=0.51,
        gamma=0.04,
        theta=-0.08,
        vega=0.11,
    )
    fallback_price = float(
        bsm_price(
            np.array([100.0]),
            np.array([105.0]),
            np.array([45.0 / 365.0]),
            0.0425,
            np.array([0.40]),
            np.array([True]),
        )[0]
    )
    missing_row = _make_row(
        conid=11,
        right="C",
        spot=100.0,
        strike=105.0,
        dte=45,
        mark_price=fallback_price,
        greeks_source="NONE",
    )

    df = _to_df([native_row, missing_row])
    engine = BSMFallbackEngine(risk_free_rate=0.0425)
    enriched = engine.enrich(df)

    r0 = enriched.row(0, named=True)
    assert r0["greeks_source"] == "IBKR_MODEL"
    assert r0["iv"] == pytest.approx(0.22)
    assert r0["delta"] == pytest.approx(0.51)
    assert r0["gamma"] == pytest.approx(0.04)
    assert r0["theta"] == pytest.approx(-0.08)
    assert r0["vega"] == pytest.approx(0.11)

    r1 = enriched.row(1, named=True)
    assert r1["greeks_source"] == "BSM_FALLBACK"
    assert r1["iv"] == pytest.approx(0.40, abs=1e-4)


def test_unsolvable_and_below_intrinsic_rows_stay_none_without_nan() -> None:
    df = _to_df(
        [
            # 1. mark_price is None
            _make_row(conid=20, right="C", spot=100.0, strike=100.0, mark_price=None),
            # 2. Call mark_price <= intrinsic (intrinsic = 100 - 80 = 20.0)
            _make_row(conid=21, right="C", spot=100.0, strike=80.0, mark_price=19.5),
            _make_row(conid=22, right="C", spot=100.0, strike=80.0, mark_price=20.0),
            # 3. Put mark_price <= intrinsic (intrinsic = 120 - 100 = 20.0)
            _make_row(conid=23, right="P", spot=100.0, strike=120.0, mark_price=19.9),
            # 4. Option mark_price above max possible BSM price at iv=5.00
            _make_row(conid=24, right="C", spot=100.0, strike=100.0, mark_price=99.99),
        ]
    )

    enriched = enrich_missing_greeks(df, risk_free_rate=0.0425)
    assert enriched.schema == OPTION_CHAIN_POLARS_SCHEMA
    for idx in range(enriched.height):
        row = enriched.row(idx, named=True)
        assert row["greeks_source"] == "NONE"
        for col in ("iv", "delta", "gamma", "theta", "vega"):
            assert row[col] is None


def test_empty_dataframe_preserves_schema() -> None:
    empty_df = pl.DataFrame(schema=OPTION_CHAIN_POLARS_SCHEMA)
    res = enrich_missing_greeks(empty_df)
    assert res.height == 0
    assert res.schema == OPTION_CHAIN_POLARS_SCHEMA
    assert compute_walls(empty_df).by_dte == []
    assert scan_anomalies(empty_df).total_flagged == 0
    assert build_multi_dte_profiles(empty_df).expiries == []


# ------------------------------------------------------------------ Phase 4B: Walls
def test_compute_walls_open_interest_primary_mode_and_tiebreak() -> None:
    rows = [
        # DTE 37 Calls: 110 has OI=5000, 105 has OI=3000 (vol=900), 115 has OI=3000 (vol=200)
        _make_row(conid=101, right="C", strike=105.0, open_interest=3000, volume=900),
        _make_row(conid=102, right="C", strike=110.0, open_interest=5000, volume=400),
        _make_row(conid=103, right="C", strike=115.0, open_interest=3000, volume=200),
        # DTE 37 Puts: 90 has OI=4000, 95 has OI=2000
        _make_row(conid=104, right="P", strike=90.0, open_interest=4000, volume=500),
        _make_row(conid=105, right="P", strike=95.0, open_interest=2000, volume=300),
        # DTE 43 Call at 110 adds another 1000 OI -> aggregate 110 Call OI = 6000
        _make_row(
            conid=106,
            right="C",
            strike=110.0,
            dte=43,
            expiry="2026-12-24",
            open_interest=1000,
            volume=100,
        ),
    ]
    report = WallsAnalyzer(top_n=3).analyze(_to_df(rows))
    assert report.primary_basis == "OPEN_INTEREST"
    assert report.total_call_oi == 12000
    assert report.total_put_oi == 6000
    assert report.aggregate_primary_call_wall is not None
    assert report.aggregate_primary_call_wall.strike == 110.0
    assert report.aggregate_primary_call_wall.metric_value == 6000
    assert report.aggregate_primary_call_wall.distance_from_spot_pct == pytest.approx(10.0)
    assert report.aggregate_primary_call_wall.concentration_ratio == pytest.approx(6000 / 12000)

    # Tie-break check: 105 (OI=3000, vol=900) ranks ahead of 115 (OI=3000, vol=200)
    assert [w.strike for w in report.aggregate_top_call_walls] == [110.0, 105.0, 115.0]
    assert report.aggregate_primary_put_wall is not None
    assert report.aggregate_primary_put_wall.strike == 90.0
    assert report.aggregate_primary_put_wall.distance_from_spot_pct == pytest.approx(-10.0)
    assert len(report.by_dte) == 2


def test_compute_walls_automatic_volume_fallback_when_oi_null() -> None:
    rows = [
        _make_row(conid=201, right="C", strike=105.0, open_interest=None, volume=450),
        _make_row(conid=202, right="C", strike=110.0, open_interest=None, volume=1200),
        _make_row(conid=203, right="P", strike=95.0, open_interest=None, volume=800),
        _make_row(conid=204, right="P", strike=90.0, open_interest=None, volume=1600),
    ]
    report = compute_walls(_to_df(rows), top_n=2)
    assert report.primary_basis == "VOLUME"
    assert report.total_call_oi == 0
    assert report.total_put_oi == 0
    assert report.total_call_volume == 1650
    assert report.total_put_volume == 2400
    assert report.aggregate_primary_call_wall is not None
    assert report.aggregate_primary_call_wall.strike == 110.0
    assert report.aggregate_primary_call_wall.metric_value == 1200
    assert report.aggregate_primary_put_wall is not None
    assert report.aggregate_primary_put_wall.strike == 90.0
    assert report.aggregate_primary_put_wall.metric_value == 1600


# ------------------------------------------------------------------ Phase 4B: Anomalies
def test_scan_anomalies_vol_gt_oi_and_zscore_outliers() -> None:
    rows = [
        # 5 Call strikes in 2026-12-11: 4 normal volume=20, 1 spike volume=500 (ddof=0 zscore=2.0)
        # Strike 100 also has open_interest=100 -> vol_oi_ratio=5.0, is_vol_gt_oi=True
        _make_row(conid=301, right="C", strike=95.0, volume=20, open_interest=200),
        _make_row(conid=302, right="C", strike=100.0, volume=500, open_interest=100),
        _make_row(conid=303, right="C", strike=105.0, volume=20, open_interest=200),
        _make_row(conid=304, right="C", strike=110.0, volume=20, open_interest=200),
        _make_row(conid=305, right="C", strike=115.0, volume=20, open_interest=200),
        # 5 Put strikes in 2026-12-11 with open_interest=None (Type 3 delayed feed):
        # 4 strikes have volume=10, 1 strike (90.0) has volume=400 -> zscore=2.0 >= 2.0
        _make_row(conid=306, right="P", strike=85.0, volume=10, open_interest=None),
        _make_row(conid=307, right="P", strike=90.0, volume=400, open_interest=None),
        _make_row(conid=308, right="P", strike=95.0, volume=10, open_interest=None),
        _make_row(conid=309, right="P", strike=100.0, volume=10, open_interest=None),
        _make_row(conid=310, right="P", strike=105.0, volume=10, open_interest=None),
        # Call with vol > OI (vol=150, OI=50 -> ratio=3.0) in DTE 43 (single row -> zscore=0)
        _make_row(
            conid=311,
            right="C",
            strike=120.0,
            dte=43,
            expiry="2026-12-24",
            volume=150,
            open_interest=50,
        ),
    ]
    report = AnomalyScanner(zscore_threshold=2.0).scan(_to_df(rows))
    assert report.total_flagged == 3
    assert report.vol_gt_oi_count == 2
    assert report.high_zscore_count == 2

    # Sorted by (is_vol_gt_oi desc, vol_oi_ratio desc nulls last, volume_zscore desc):
    # 1st: conid 302 (is_vol_gt_oi=True, ratio=5.0, zscore=2.0)
    # 2nd: conid 311 (is_vol_gt_oi=True, ratio=3.0, zscore=0.0)
    # 3rd: conid 307 (is_vol_gt_oi=False, ratio=None, zscore=2.0)
    assert [a.option_conid for a in report.anomalies] == [302, 311, 307]
    assert report.anomalies[0].vol_oi_ratio == pytest.approx(5.0)
    assert report.anomalies[0].is_high_vol_zscore is True
    assert report.anomalies[2].open_interest is None
    assert report.anomalies[2].vol_oi_ratio is None
    assert report.anomalies[2].is_high_vol_zscore is True


# ------------------------------------------------------------------ Phase 4B: Multi-DTE Profiles
def test_build_multi_dte_profiles_across_weekly_expiries() -> None:
    dtes_and_expiries = [
        (37, "2026-12-18"),
        (43, "2026-12-24"),
        (50, "2026-12-31"),
        (58, "2027-01-08"),
    ]
    rows: list[dict[str, Any]] = []
    conid = 500
    spot = 142.0
    for dte, expiry in dtes_and_expiries:
        for strike in (135.0, 140.0, 145.0):
            conid += 1
            rows.append(
                _make_row(
                    conid=conid,
                    right="C",
                    spot=spot,
                    strike=strike,
                    dte=dte,
                    expiry=expiry,
                    mark_price=6.0,
                    volume=120,
                    open_interest=500,
                    greeks_source="IBKR_MODEL" if dte == 37 else "BSM_FALLBACK",
                    iv=0.40 + dte * 0.001,
                    delta=0.52,
                    gamma=0.03,
                    theta=-0.10,
                    vega=0.18,
                )
            )
            conid += 1
            rows.append(
                _make_row(
                    conid=conid,
                    right="P",
                    spot=spot,
                    strike=strike,
                    dte=dte,
                    expiry=expiry,
                    mark_price=5.5,
                    volume=90,
                    open_interest=450,
                    greeks_source="IBKR_MODEL" if dte == 37 else "BSM_FALLBACK",
                    iv=0.42 + dte * 0.001,
                    delta=-0.48,
                    gamma=0.03,
                    theta=-0.09,
                    vega=0.18,
                )
            )

    profile_report = MultiDTEStructureAnalyzer().analyze(_to_df(rows))
    assert profile_report.underlying == "NVDA"
    assert profile_report.underlying_price == 142.0
    assert profile_report.greeks_source_counts == {"IBKR_MODEL": 6, "BSM_FALLBACK": 18}
    assert [ex.dte for ex in profile_report.expiries] == [37, 43, 50, 58]

    e37 = profile_report.expiries[0]
    # Strikes are 135, 140, 145 -> closest to spot 142.0 is 140.0
    assert e37.atm_strike == 140.0
    # call_iv=0.437, put_iv=0.457 -> atm_iv = 0.447
    assert e37.atm_iv == pytest.approx(0.447)
    assert [pt.strike for pt in e37.strikes] == [135.0, 140.0, 145.0]
    assert e37.strikes[1].call_delta == pytest.approx(0.52)
    assert e37.strikes[1].put_delta == pytest.approx(-0.48)
    assert e37.strikes[1].call_oi == 500
    assert e37.strikes[1].put_oi == 450


def test_greeks_for_quotes_prefers_solved_iv_then_fallback_iv_then_flags_unusable() -> None:
    spot = np.full(3, 100.0)
    strike = np.full(3, 100.0)
    is_call = np.ones(3, dtype=np.bool_)
    theo = bsm_price(
        spot[:1], strike[:1], np.array([30 / 365.0]), 0.0425, np.array([0.40]), is_call[:1]
    )
    marks = np.array([float(theo[0]), 0.0, 0.0])  # row 0 solvable; rows 1-2 have no usable mark
    result = greeks_for_quotes(
        mark_price=marks,
        spot=spot,
        strike=strike,
        dte=np.full(3, 30.0),
        is_call=is_call,
        fallback_iv=np.array([np.nan, 0.55, np.nan]),
        risk_free_rate=0.0425,
    )
    assert result.usable.tolist() == [True, True, False]
    assert result.iv[0] == pytest.approx(0.40, abs=1e-4)
    assert result.iv[1] == pytest.approx(0.55)
    assert 0.0 < result.delta[0] < 1.0


def test_short_and_zero_dte_bsm_walls_anomalies_and_term_profiles() -> None:
    t_zero = np.array([0.5 / 365.0], dtype=np.float64)
    theo_0d = float(
        bsm_price(
            np.array([100.0]),
            np.array([100.0]),
            t_zero,
            0.0425,
            np.array([0.45]),
            np.array([True]),
        )[0]
    )
    rows = [
        _make_row(
            conid=901,
            right="C",
            spot=100.0,
            strike=100.0,
            dte=0,
            expiry="2026-11-11",
            mark_price=theo_0d,
            volume=900,
            open_interest=100,
        ),
        _make_row(
            conid=902,
            right="P",
            spot=100.0,
            strike=95.0,
            dte=0,
            expiry="2026-11-11",
            mark_price=0.05,
            volume=40,
            open_interest=200,
        ),
        _make_row(
            conid=903,
            right="C",
            spot=100.0,
            strike=105.0,
            dte=14,
            expiry="2026-11-25",
            mark_price=0.80,
            volume=300,
            open_interest=500,
        ),
        _make_row(
            conid=904,
            right="P",
            spot=100.0,
            strike=95.0,
            dte=14,
            expiry="2026-11-25",
            mark_price=0.75,
            volume=250,
            open_interest=600,
        ),
    ]
    enriched = enrich_missing_greeks(_to_df(rows), risk_free_rate=0.0425)
    r0 = enriched.row(0, named=True)
    assert r0["dte"] == 0
    assert r0["greeks_source"] == "BSM_FALLBACK"
    assert r0["iv"] == pytest.approx(0.45, abs=1e-4)
    assert all(math.isfinite(float(r0[c])) for c in ("delta", "gamma", "theta", "vega"))

    q_res = greeks_for_quotes(
        mark_price=np.array([theo_0d]),
        spot=np.array([100.0]),
        strike=np.array([100.0]),
        dte=np.array([0.0]),
        is_call=np.array([True]),
        fallback_iv=np.array([np.nan]),
    )
    assert q_res.usable.tolist() == [True]
    assert q_res.iv[0] == pytest.approx(0.45, abs=1e-4)

    walls = compute_walls(enriched)
    assert [w.dte for w in walls.by_dte] == [0, 14]
    assert walls.by_dte[0].primary_put_wall is not None
    assert walls.by_dte[0].primary_put_wall.strike == 95.0

    anomalies = scan_anomalies(enriched)
    assert anomalies.vol_gt_oi_count == 1
    assert anomalies.anomalies[0].dte == 0

    profiles = build_multi_dte_profiles(enriched)
    assert [e.dte for e in profiles.expiries] == [0, 14]
    assert profiles.expiries[0].atm_strike == 100.0
    assert profiles.expiries[0].atm_iv == pytest.approx(0.45, abs=1e-4)
