"""Vectorized Black-Scholes-Merton (BSM) Implied Volatility and Greeks fallback engine."""

from __future__ import annotations

from typing import NamedTuple

import numpy as np
import numpy.typing as npt
import polars as pl
from scipy.special import erf as _scipy_erf  # type: ignore[import-untyped]

from src.storage.schema import COLUMNS, OPTION_CHAIN_POLARS_SCHEMA

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

DEFAULT_RISK_FREE_RATE: float = 0.0425
IV_LOWER_BOUND: float = 0.01
IV_UPPER_BOUND: float = 5.00
MIN_DTE_YEARS_NUMERATOR: float = 0.5
DAYS_PER_YEAR: float = 365.0
INTRINSIC_TOLERANCE: float = 1e-6
PRICE_CONVERGENCE_TOL: float = 1e-8
MIN_RAW_VEGA: float = 1e-8
SOLVER_ITERATIONS: int = 25
FALLBACK_IV_MIN: float = 0.01
FALLBACK_IV_MAX: float = 10.0
_PLACEHOLDER_IV: float = 0.30

_INV_SQRT_2: float = 1.0 / float(np.sqrt(2.0))
_INV_SQRT_2PI: float = 1.0 / float(np.sqrt(2.0 * np.pi))


def norm_pdf(x: FloatArray) -> FloatArray:
    """Standard normal probability density function $\\phi(x)$."""
    res: FloatArray = np.exp(-0.5 * np.square(x)) * _INV_SQRT_2PI
    return res


def norm_cdf(x: FloatArray) -> FloatArray:
    """Standard normal cumulative distribution function $N(x)$ via `scipy.special.erf`."""
    erf_vals: FloatArray = np.asarray(_scipy_erf(x * _INV_SQRT_2), dtype=np.float64)
    return 0.5 * (1.0 + erf_vals)


def _d1_d2(
    spot: FloatArray,
    strike: FloatArray,
    t_years: FloatArray,
    risk_free_rate: float,
    iv: FloatArray,
) -> tuple[FloatArray, FloatArray, FloatArray]:
    sqrt_t: FloatArray = np.sqrt(t_years)
    vol_sqrt_t: FloatArray = iv * sqrt_t
    d1: FloatArray = (np.log(spot / strike) + (risk_free_rate + 0.5 * np.square(iv)) * t_years) / (
        vol_sqrt_t
    )
    d2: FloatArray = d1 - vol_sqrt_t
    return d1, d2, sqrt_t


def bsm_price(
    spot: FloatArray,
    strike: FloatArray,
    t_years: FloatArray,
    risk_free_rate: float,
    iv: FloatArray,
    is_call: BoolArray,
) -> FloatArray:
    """Vectorized European Black-Scholes-Merton option price (zero dividend yield)."""
    d1, d2, _ = _d1_d2(spot, strike, t_years, risk_free_rate, iv)
    df: FloatArray = np.exp(-risk_free_rate * t_years)
    call_price: FloatArray = spot * norm_cdf(d1) - strike * df * norm_cdf(d2)
    put_price: FloatArray = strike * df * norm_cdf(-d2) - spot * norm_cdf(-d1)
    return np.where(is_call, call_price, put_price)


def _bsm_price_and_raw_vega(
    spot: FloatArray,
    strike: FloatArray,
    t_years: FloatArray,
    risk_free_rate: float,
    iv: FloatArray,
    is_call: BoolArray,
) -> tuple[FloatArray, FloatArray]:
    """Return `(option_price, dPrice/dSigma)` for one Newton-Raphson iteration."""
    d1, d2, sqrt_t = _d1_d2(spot, strike, t_years, risk_free_rate, iv)
    df: FloatArray = np.exp(-risk_free_rate * t_years)
    call_price: FloatArray = spot * norm_cdf(d1) - strike * df * norm_cdf(d2)
    put_price: FloatArray = strike * df * norm_cdf(-d2) - spot * norm_cdf(-d1)
    price: FloatArray = np.where(is_call, call_price, put_price)
    raw_vega: FloatArray = spot * norm_pdf(d1) * sqrt_t
    return price, raw_vega


def bsm_greeks(
    spot: FloatArray,
    strike: FloatArray,
    t_years: FloatArray,
    risk_free_rate: float,
    iv: FloatArray,
    is_call: BoolArray,
) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray]:
    """Compute vectorized BSM `(delta, gamma, theta_daily, vega_per_1pct)`."""
    d1, d2, sqrt_t = _d1_d2(spot, strike, t_years, risk_free_rate, iv)
    pdf_d1 = norm_pdf(d1)
    nd1 = norm_cdf(d1)
    nd2 = norm_cdf(d2)
    n_neg_d2 = norm_cdf(-d2)

    delta: FloatArray = np.where(is_call, nd1, nd1 - 1.0)
    gamma: FloatArray = pdf_d1 / (spot * iv * sqrt_t)

    df_strike: FloatArray = strike * np.exp(-risk_free_rate * t_years)
    common_decay: FloatArray = -(spot * pdf_d1 * iv) / (2.0 * sqrt_t)
    theta_annual: FloatArray = np.where(
        is_call,
        common_decay - risk_free_rate * df_strike * nd2,
        common_decay + risk_free_rate * df_strike * n_neg_d2,
    )
    theta: FloatArray = theta_annual / DAYS_PER_YEAR
    vega: FloatArray = (spot * pdf_d1 * sqrt_t) / 100.0
    return delta, gamma, theta, vega


def solve_implied_volatility(
    mark_price: FloatArray,
    spot: FloatArray,
    strike: FloatArray,
    t_years: FloatArray,
    risk_free_rate: float,
    is_call: BoolArray,
) -> tuple[FloatArray, BoolArray]:
    """Hybrid Bisection + Newton-Raphson solver on `[0.01, 5.00]` (25 iterations).

    Returns `(iv_array, solved_mask)`. Unsolvable entries (`mark_price <= intrinsic + 1e-6`
    or outside `[price(0.01), price(5.00)]`) have `solved_mask == False`.
    """
    n = mark_price.shape[0]
    if n == 0:
        return np.empty(0, dtype=np.float64), np.empty(0, dtype=np.bool_)

    intrinsic: FloatArray = np.where(
        is_call,
        np.maximum(0.0, spot - strike),
        np.maximum(0.0, strike - spot),
    )
    low: FloatArray = np.full(n, IV_LOWER_BOUND, dtype=np.float64)
    high: FloatArray = np.full(n, IV_UPPER_BOUND, dtype=np.float64)

    p_low = bsm_price(spot, strike, t_years, risk_free_rate, low, is_call)
    p_high = bsm_price(spot, strike, t_years, risk_free_rate, high, is_call)

    solvable: BoolArray = (
        np.isfinite(mark_price)
        & np.isfinite(spot)
        & np.isfinite(strike)
        & (spot > 0.0)
        & (strike > 0.0)
        & (mark_price > intrinsic + INTRINSIC_TOLERANCE)
        & (mark_price >= p_low - PRICE_CONVERGENCE_TOL)
        & (mark_price <= p_high + PRICE_CONVERGENCE_TOL)
    )

    m = int(np.count_nonzero(solvable))
    solved_iv: FloatArray = np.full(n, np.nan, dtype=np.float64)
    if m == 0:
        return solved_iv, solvable

    s_sub = spot[solvable]
    k_sub = strike[solvable]
    t_sub = t_years[solvable]
    p_sub = mark_price[solvable]
    c_sub = is_call[solvable]

    lo = np.full(m, IV_LOWER_BOUND, dtype=np.float64)
    hi = np.full(m, IV_UPPER_BOUND, dtype=np.float64)

    # Brenner-Subrahmanyam starting approximation clamped safely inside (lo, hi)
    brenner = np.sqrt(2.0 * np.pi / t_sub) * (p_sub / s_sub)
    iv: FloatArray = np.clip(brenner, 0.05, 1.50)

    for _ in range(SOLVER_ITERATIONS):
        price, raw_vega = _bsm_price_and_raw_vega(s_sub, k_sub, t_sub, risk_free_rate, iv, c_sub)
        diff: FloatArray = price - p_sub
        if bool(np.all(np.abs(diff) <= PRICE_CONVERGENCE_TOL)):
            break

        hi = np.where(diff > 0.0, np.minimum(hi, iv), hi)
        lo = np.where(diff < 0.0, np.maximum(lo, iv), lo)

        safe_vega: FloatArray = np.where(raw_vega >= MIN_RAW_VEGA, raw_vega, 1.0)
        newton_step: FloatArray = iv - diff / safe_vega
        bisect_step: FloatArray = 0.5 * (lo + hi)

        use_newton: BoolArray = (raw_vega >= MIN_RAW_VEGA) & (newton_step > lo) & (newton_step < hi)
        next_iv: FloatArray = np.where(use_newton, newton_step, bisect_step)
        converged: BoolArray = np.abs(diff) <= PRICE_CONVERGENCE_TOL
        iv = np.where(converged, iv, next_iv)

    iv = np.clip(iv, IV_LOWER_BOUND, IV_UPPER_BOUND)
    solved_iv[solvable] = iv
    return solved_iv, solvable


class QuoteGreeks(NamedTuple):
    """Per-quote BSM results; `usable[i] == False` means no trustworthy IV was available."""

    iv: FloatArray
    delta: FloatArray
    gamma: FloatArray
    theta: FloatArray
    vega: FloatArray
    usable: BoolArray


def greeks_for_quotes(
    *,
    mark_price: FloatArray,
    spot: FloatArray,
    strike: FloatArray,
    dte: FloatArray,
    is_call: BoolArray,
    fallback_iv: FloatArray,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> QuoteGreeks:
    """BSM Greeks for live quotes lacking `modelGreeks`.

    IV is solved from `mark_price`; where that fails, a previously known `fallback_iv`
    (e.g. from the cached snapshot, NaN when unknown) inside `[0.01, 10.0]` is reused.
    Rows with neither are flagged `usable=False` and their Greeks must be ignored.
    """
    t_years: FloatArray = np.maximum(dte, MIN_DTE_YEARS_NUMERATOR) / DAYS_PER_YEAR
    solved_iv, solved = solve_implied_volatility(
        mark_price=mark_price,
        spot=spot,
        strike=strike,
        t_years=t_years,
        risk_free_rate=risk_free_rate,
        is_call=is_call,
    )
    in_range: BoolArray = (fallback_iv >= FALLBACK_IV_MIN) & (fallback_iv <= FALLBACK_IV_MAX)
    fallback_ok: BoolArray = np.isfinite(fallback_iv) & in_range
    usable: BoolArray = solved | fallback_ok
    eff_iv: FloatArray = np.where(
        solved, solved_iv, np.where(fallback_ok, fallback_iv, _PLACEHOLDER_IV)
    )
    delta, gamma, theta, vega = bsm_greeks(spot, strike, t_years, risk_free_rate, eff_iv, is_call)
    return QuoteGreeks(iv=eff_iv, delta=delta, gamma=gamma, theta=theta, vega=vega, usable=usable)


def enrich_missing_greeks(
    df: pl.DataFrame,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
) -> pl.DataFrame:
    """Populate missing IV and Greeks via vectorized BSM while preserving `IBKR_MODEL` rows."""
    if df.height == 0:
        return df.select(COLUMNS).cast(OPTION_CHAIN_POLARS_SCHEMA)

    n = df.height
    source_list = df["greeks_source"].to_list()
    iv_list = df["iv"].to_list()
    delta_list = df["delta"].to_list()
    gamma_list = df["gamma"].to_list()
    theta_list = df["theta"].to_list()
    vega_list = df["vega"].to_list()

    preserve_ibkr = np.array(
        [
            src == "IBKR_MODEL" and iv_val is not None and not np.isnan(float(iv_val))
            for src, iv_val in zip(source_list, iv_list, strict=True)
        ],
        dtype=np.bool_,
    )
    candidate_mask: BoolArray = ~preserve_ibkr

    if not bool(np.any(candidate_mask)):
        return df.select(COLUMNS).cast(OPTION_CHAIN_POLARS_SCHEMA)

    mark_arr: FloatArray = df["mark_price"].fill_null(np.nan).to_numpy().astype(np.float64)
    spot_arr: FloatArray = df["underlying_price"].fill_null(np.nan).to_numpy().astype(np.float64)
    strike_arr: FloatArray = df["strike"].fill_null(np.nan).to_numpy().astype(np.float64)
    dte_arr: FloatArray = df["dte"].fill_null(0).to_numpy().astype(np.float64)
    right_list = df["right"].to_list()
    is_call_arr: BoolArray = np.array([r == "C" for r in right_list], dtype=np.bool_)

    cand_indices = np.flatnonzero(candidate_mask)
    t_years_cand: FloatArray = (
        np.maximum(dte_arr[cand_indices], MIN_DTE_YEARS_NUMERATOR) / DAYS_PER_YEAR
    )

    solved_iv_cand, solved_submask = solve_implied_volatility(
        mark_price=mark_arr[cand_indices],
        spot=spot_arr[cand_indices],
        strike=strike_arr[cand_indices],
        t_years=t_years_cand,
        risk_free_rate=risk_free_rate,
        is_call=is_call_arr[cand_indices],
    )

    # Clear unsolvable candidate rows so stale/partial values never linger
    for idx in cand_indices.tolist():
        iv_list[idx] = None
        delta_list[idx] = None
        gamma_list[idx] = None
        theta_list[idx] = None
        vega_list[idx] = None
        source_list[idx] = "NONE"

    if bool(np.any(solved_submask)):
        solved_global_indices = cand_indices[solved_submask]
        iv_good = solved_iv_cand[solved_submask]
        s_good = spot_arr[solved_global_indices]
        k_good = strike_arr[solved_global_indices]
        t_good = t_years_cand[solved_submask]
        c_good = is_call_arr[solved_global_indices]

        d_good, g_good, th_good, v_good = bsm_greeks(
            s_good, k_good, t_good, risk_free_rate, iv_good, c_good
        )

        for pos, row_idx in enumerate(solved_global_indices.tolist()):
            iv_list[row_idx] = float(iv_good[pos])
            delta_list[row_idx] = float(d_good[pos])
            gamma_list[row_idx] = float(g_good[pos])
            theta_list[row_idx] = float(th_good[pos])
            vega_list[row_idx] = float(v_good[pos])
            source_list[row_idx] = "BSM_FALLBACK"

    assert len(source_list) == n
    enriched = df.with_columns(
        pl.Series("iv", iv_list, dtype=pl.Float64),
        pl.Series("delta", delta_list, dtype=pl.Float64),
        pl.Series("gamma", gamma_list, dtype=pl.Float64),
        pl.Series("theta", theta_list, dtype=pl.Float64),
        pl.Series("vega", vega_list, dtype=pl.Float64),
        pl.Series("greeks_source", source_list, dtype=pl.Utf8),
    )
    return enriched.select(COLUMNS).cast(OPTION_CHAIN_POLARS_SCHEMA)


# Alias referenced in docs/DATA_SCHEMA.md §3
compute_missing_greeks = enrich_missing_greeks


class BSMFallbackEngine:
    """Stateful/stateless facade for vectorized Black-Scholes-Merton fallback enrichment."""

    def __init__(self, risk_free_rate: float = DEFAULT_RISK_FREE_RATE) -> None:
        self.risk_free_rate = risk_free_rate

    @staticmethod
    def enrich_missing_greeks(
        df: pl.DataFrame,
        risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    ) -> pl.DataFrame:
        return enrich_missing_greeks(df, risk_free_rate=risk_free_rate)

    def enrich(self, df: pl.DataFrame) -> pl.DataFrame:
        return enrich_missing_greeks(df, risk_free_rate=self.risk_free_rate)
