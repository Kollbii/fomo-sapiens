# Post-Core Roadmap & Future Improvements (`FUTURE_IMPROVEMENTS.md`)

> **Instructions for Coding Agents:** Do NOT implement these features until Phases 1–5 of the Core Architecture in `AGENTS.md` are 100% complete and verified. When the user requests any feature below, implement it following the specification here, add unit tests, and move its section to `docs/ARCHITECTURE.md` marked `[IMPLEMENTED]`.

---

## 1. Live OPRA Subscription Upgrade (`MARKET_DATA_MODE=LIVE`)
- **Status:** `[SCAFFOLDED IN CORE — READY FOR ACTIVATION]`
- **Context:** The core system runs on **Type 3 (15-min Delayed)** and **Type 4 (Delayed-Frozen)** data with local BSM Greeks fallback. Once the user subscribes to live US Options (OPRA) feeds in IBKR:
- **Activation & Enhancement Plan:**
  1. Switch `MARKET_DATA_MODE=LIVE` (or `default_market_data_type: 1` in `config/settings.yaml`).
  2. In `src/broker/fetcher.py` (`_fetch_page_live_opra`), subscribe with `genericTickList="100,101,104,106"` and wait in `0.15s` increments (up to `2.0s` max per page) specifically until both `ticker.modelGreeks` and `ticker.callOpenInterest` / `ticker.putOpenInterest` (tick `101`) populate.
  3. Add an optional **Bid-Ask Spread Quality Filter** flagging illiquid strikes where `(ask - bid) / mid > 0.25`.

---

## 2. Net Gamma Exposure (GEX) Profile & Zero-Gamma Flip Level
- **Status:** `[PLANNED — PHASE 6]`
- **Quantitative Specification:**
  - Using `open_interest`, `gamma`, and `underlying_price` ($S$), compute per-strike Dealer Net Gamma Exposure assuming standard customer positioning (dealers long calls, short puts):
    - $\text{Call GEX}_K = +\text{OI}_{\text{call}, K} \times \Gamma_{\text{call}, K} \times 100 \times S^2 \times 0.01$
    - $\text{Put GEX}_K = -\text{OI}_{\text{put}, K} \times \Gamma_{\text{put}, K} \times 100 \times S^2 \times 0.01$
    - $\text{Net GEX}_K = \text{Call GEX}_K + \text{Put GEX}_K$
  - **Zero-Gamma Flip Level:** Interpolate the exact underlying spot price $S_{\text{flip}}$ where cumulative Net GEX across the `[30, 60)` weekly chain crosses zero (transitioning from positive mean-reverting gamma to negative volatility-amplifying gamma).
- **Module Target:** `src/analytics/gex.py` + new GEX Bar Profile tab in Chart A.

---

## 3. Historical Snapshot Diffing Engine ($\Delta\text{OI}$, $\Delta\text{Volume}$ & $\Delta\text{IV}$)
- **Status:** `[PLANNED — PHASE 6]`
- **Architectural Specification:**
  - Leverage **Mode 1 Archival CSVs** in `data/snapshots/{TICKER}/` to join the active snapshot `T_now` against any prior snapshot `T_prev` on `(option_conid)` or `(expiry, strike, right)` in `Polars`.
  - Compute:
    - `delta_oi = open_interest_now - open_interest_prev`
    - `delta_iv = iv_now - iv_prev`
    - `delta_mark = mark_price_now - mark_price_prev`
  - Classify positioning changes into four institutional flow regimes:
    1. **Long Buildup:** `delta_oi > 0` and `delta_mark > 0`
    2. **Short Buildup:** `delta_oi > 0` and `delta_mark < 0`
    3. **Short Covering:** `delta_oi < 0` and `delta_mark > 0`
    4. **Long Liquidation:** `delta_oi < 0` and `delta_mark < 0`
- **Module Target:** `src/analytics/diff_engine.py` + `"Compare With Snapshot..."` selector in the UI control bar.

---

## 4. 25-Delta Risk Reversal (Put-Call Skew) & Term Structure Curve
- **Status:** `[PLANNED — PHASE 7]`
- **Quantitative Specification:**
  - For each weekly expiration in the `[30, 60)` DTE window:
    - Extract **ATM IV** (interpolated at `abs(delta) == 0.50` or `strike == spot`).
    - Extract **25-Delta Put IV** (`delta == -0.25`) and **25-Delta Call IV** (`delta == +0.25`).
    - Compute **25-Delta Risk Reversal:** $\text{RR}_{25} = \text{IV}_{25\Delta \text{ Call}} - \text{IV}_{25\Delta \text{ Put}}$.
    - Compute **25-Delta Butterfly (Smile Curvature):** $\text{BF}_{25} = 0.5 \times (\text{IV}_{25\Delta \text{ Call}} + \text{IV}_{25\Delta \text{ Put}}) - \text{IV}_{\text{ATM}}$.
- **Module Target:** `src/analytics/skew_term.py`.

---

## 5. Max Pain & Implied Expected Move Cone ($\pm 1\sigma / \pm 2\sigma$)
- **Status:** `[PLANNED — PHASE 7]`
- **Quantitative Specification:**
  - **Max Pain Strike:** For each expiration, evaluate candidate settlement prices $S_T \in \text{Strikes}$ and find the strike that minimizes total intrinsic payout to option holders:
    $$\text{Payout}(S_T) = \sum_K \text{OI}_{\text{call}, K} \max(0, S_T - K) + \sum_K \text{OI}_{\text{put}, K} \max(0, K - S_T)$$
  - **Implied Expected Move ($\pm 1\sigma$):**
    $$\Delta S_{1\sigma} = S \times \text{IV}_{\text{ATM}} \times \sqrt{\frac{\text{DTE}}{365}}$$
  - Render shaded $\pm 1\sigma$ expected move bands onto the Put/Call Wall charts.

---

## 6. Automated NYT Executive Market Brief Generator (Top Banner Content)
- **Status:** `[PLANNED — POPULATES RESERVED #editorial-banner-slot]`
- **Specification:**
  - Deterministic rule-based synthesis engine (`src/analytics/narrative.py`) that populates the reserved `#editorial-banner-slot` in `index.html` with a 2-paragraph journalistic summary of the current ticker's Primary Call/Put Walls, dominant DTE volume concentration, and highest Z-score strike anomalies.