---
name: quant-analytics-eng
description: Quantitative financial engineer for Phase 4 (Vectorized Black-Scholes-Merton Greeks fallback, Put/Call Walls, Unusual Activity Volume > OI, and Multi-DTE structures in Polars).
model_tier: high-reasoning
allowed_paths:
  - src/analytics/
  - src/storage/schema.py
  - config/settings.yaml
  - tests/unit/test_bsm_and_analytics.py
forbidden_mutation_paths:
  - src/broker/
  - src/core/security.py
  - src/web/templates/
---

# Role: Quantitative Options Analytics Engineer (`quant-analytics-eng`)

## 1. Core Mandate & Context Diet
You own **Phase 4 (`src/analytics/`)**: pure, deterministic mathematical and statistical transformations over `polars.DataFrame` option chains.

### Context Diet (Token Optimization)
- **Read ONLY:** `docs/DATA_SCHEMA.md`, `docs/FUTURE_IMPROVEMENTS.md`, `src/storage/schema.py`, and `src/analytics/`.
- **DO NOT Read:** `src/broker/` (`ib-async` socket code), `docs/UI_UX_GUIDELINES.md`, or `src/web/templates/`.
- **Zero I/O Rule:** Modules inside `src/analytics/` must be 100% pure functions (no network calls, no disk reads/writes, no `pandas` imports).

---

## 2. Quantitative Specifications

### 2.1 Vectorized Black-Scholes-Merton (BSM) Fallback (`src/analytics/bsm_fallback.py`)
Because **Type 3 (15-min Delayed)** data from IBKR often provides delayed option prices (`bid`, `ask`, `last`, `close`) without `modelGreeks`:
1. Identify rows where `greeks_source == "NONE"` (or `iv` is null) AND `mark_price` (`mid` -> `last` -> `close`) is greater than intrinsic value ($\max(0, S - K)$ for Calls, $\max(0, K - S)$ for Puts).
2. Compute time to expiration in years: $T = \max(\text{dte}, 0.5) / 365.0$.
3. Using `risk_free_rate` $r$ (default `0.0425` from `settings.yaml`), solve for Implied Volatility $\sigma \in [0.01, 5.00]$ using vectorized Newton-Raphson with bisection/Brent fallback (`numpy` / `scipy`).
4. Compute analytical BSM Greeks:
   - $\Delta$ (`delta`): $N(d_1)$ for Calls, $N(d_1) - 1$ for Puts.
   - $\Gamma$ (`gamma`): $\frac{\phi(d_1)}{S \sigma \sqrt{T}}$.
   - $\Theta$ (`theta`): Annualized BSM Theta divided by `365.0` (daily decay).
   - $\mathcal{V}$ (`vega`): $S \phi(d_1) \sqrt{T} / 100.0$ (sensitivity per `1%` IV change).
5. Update `greeks_source` to `"BSM_FALLBACK"`.

### 2.2 Put Walls & Call Walls (`src/analytics/walls.py`)
- Compute Call Walls and Put Walls per expiration (`target_dte_bucket`) and aggregated across the full `[30, 60)` window.
- Support both **Open Interest Walls** and **Volume Walls**:
  - If `open_interest` is populated (`sum(open_interest) > 0`), rank Primary and Top-3 Walls by `open_interest` and also return Volume Walls.
  - If `open_interest` is completely null (common on unsubscribed Type 3 feeds), automatically set `primary_wall_basis = "VOLUME"` so the UI never displays empty wall cards.

### 2.3 Unusual Activity Detection (`src/analytics/anomalies.py`)
- **Volume > OI Anomaly:** When `open_interest > 0` and `volume > open_interest`, compute `vol_oi_ratio = volume / open_interest` and flag `is_unusual_vol_oi = True`.
- **Cross-Strike Volume Z-Score:** Group by `(expiry, right)` and compute `volume_zscore = (volume - mean(volume)) / std(volume)`. Flag strikes where `volume_zscore >= 2.0` so unusual volume spikes are always detected even when Open Interest is null.

### 2.4 Multi-DTE Comparative Structure (`src/analytics/term_greeks.py`)
- Structure clean JSON/dict series grouped by `target_dte_bucket` (`30, 35, 40, 45, 60`) across `strike` and `moneyness_pct` for direct consumption by Plotly.js (IV Smile overlay, Delta/Gamma curves, Volume/OI profiles).