# Post-Core Roadmap & Future Improvements (`FUTURE_IMPROVEMENTS.md`)

> **Instructions for Coding Agents:** Do NOT implement these features until Phases 1–5 of the Core Architecture in `AGENTS.md` are 100% complete and verified. When the user requests any feature below, implement it following the specification here, add unit tests, and move its section to `docs/ARCHITECTURE.md` marked `[IMPLEMENTED]`.

---

## 1. Live OPRA Subscription Upgrade (`MARKET_DATA_MODE=LIVE`)
- **Status:** `[PARTIALLY IMPLEMENTED IN CORE — BID-ASK QUALITY FILTER PLANNED]`
- **Context:** The core system dynamically honors `MARKET_DATA_MODE=LIVE` / `DEFAULT_MARKET_DATA_TYPE=1|2` / `FALLBACK_MARKET_DATA_TYPE` in `.env` or `config/settings.yaml` across `IBKRBrokerClient`, `PagedOptionChainFetcher._fetch_page_live_opra`, `CorridorQuoteService`, FastAPI `/api/status`, and the UI (with `10089`/`354` fallback to Delayed `Type 3 -> 4`).
- **Activated & Remaining Items:**
  1. `[IMPLEMENTED IN CORE]` Setting `MARKET_DATA_MODE=LIVE` or `DEFAULT_MARKET_DATA_TYPE=1` (with optional `FALLBACK_MARKET_DATA_TYPE=2`) reconciles config cleanly across broker, API, and UI (`src/broker/fetcher.py` `_fetch_page_live_opra` polls `genericTickList="100,101,104,106"` for `modelGreeks` and tick `101` OI, cascading `1 -> 2` and downgrading `10089`/`354` to Delayed).
  2. `[IMPLEMENTED IN CORE — MODULE 2]` Targeted `<= 90`-contract **Wall Strike Corridor & Optional Live Constant Streaming (`src/broker/corridor/`, `GET /api/corridor/stream/{ticker}`, `#wall-corridor-card`)** dynamically honors `LIVE` (`Type 1 -> 2`) and `DELAYED` (`Type 3 -> 4`).
  3. `[PLANNED]` Add an optional **Bid-Ask Spread Quality Filter** flagging illiquid strikes where `(ask - bid) / mid > 0.25`.

---

## 2. Net Gamma Exposure (GEX) Profile & Zero-Gamma Flip Level
- **Status:** `[PLANNED — PHASE 6]`
- **Quantitative Specification:**
  - Using `open_interest`, `gamma`, and `underlying_price` ($S$), compute per-strike Dealer Net Gamma Exposure assuming standard customer positioning (dealers long calls, short puts):
    - $\text{Call GEX}_K = +\text{OI}_{\text{call}, K} \times \Gamma_{\text{call}, K} \times 100 \times S^2 \times 0.01$
    - $\text{Put GEX}_K = -\text{OI}_{\text{put}, K} \times \Gamma_{\text{put}, K} \times 100 \times S^2 \times 0.01$
    - $\text{Net GEX}_K = \text{Call GEX}_K + \text{Put GEX}_K$
  - **Zero-Gamma Flip Level:** Interpolate the exact underlying spot price $S_{\text{flip}}$ where cumulative Net GEX across the `[30, 120]` weekly chain crosses zero (transitioning from positive mean-reverting gamma to negative volatility-amplifying gamma).
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
  - For each weekly expiration in the `[30, 120]` DTE window:
    - Extract **ATM IV** (interpolated at `abs(delta) == 0.50` or `strike == spot`).
    - Extract **25-Delta Put IV** (`delta == -0.25`) and **25-Delta Call IV** (`delta == +0.25`).
    - Compute **25-Delta Risk Reversal:** $\text{RR}_{25} = \text{IV}_{25\Delta \text{ Call}} - \text{IV}_{25\Delta \text{ Put}}$.
    - Compute **25-Delta Butterfly (Smile Curvature):** $\text{BF}_{25} = 0.5 \times (\text{IV}_{25\Delta \text{ Call}} + \text{IV}_{25\Delta \text{ Put}}) - \text{IV}_{\text{ATM}}$.
- **Module Target:** `src/analytics/skew_term.py`.

---

## 5. Max Pain & Implied Expected Move Cone ($\pm 1\sigma / \pm 2\sigma$)
- **Status:** `[PARTIALLY IMPLEMENTED IN STRATEGY LAB; MAX PAIN + WALLS CONE PLANNED — PHASE 7]`
- **Quantitative Specification:**
  - **Implemented (`src/analytics/strategies/` + `#chart-strategy-payoff`):** Computes `±1σ` Expected Move ($\Delta S_{1\sigma} = S \times \text{IV}_{\text{ATM}} \times \sqrt{\text{DTE}/365}$) in Strategy Risk KPIs and renders a shaded `±1σ` Expected Move vertical band on `#chart-strategy-payoff`.
  - **Remaining Planned Items:**
    - **Max Pain Strike:** For each expiration, evaluate candidate settlement prices $S_T \in \text{Strikes}$ and find the strike that minimizes total intrinsic payout to option holders:
      $$\text{Payout}(S_T) = \sum_K \text{OI}_{\text{call}, K} \max(0, S_T - K) + \sum_K \text{OI}_{\text{put}, K} \max(0, K - S_T)$$
    - Render $\pm 1\sigma / \pm 2\sigma$ expected move bands directly onto the main Put/Call Wall charts.

---

## 6. Automated NYT Executive Market Brief Generator (Top Banner Content)
- **Status:** `[PLANNED — POPULATES RESERVED #editorial-banner-slot]`
- **Specification:**
  - Deterministic rule-based synthesis engine (`src/analytics/narrative.py`) that populates the reserved `#editorial-banner-slot` in `index.html` with a 2-paragraph journalistic summary of the current ticker's Primary Call/Put Walls, dominant DTE volume concentration, and highest Z-score strike anomalies.

---

## 7. Frontend UX & Navigation Modernization Roadmap (Phases 2 & 3)
- **Status:** `[PLANNED — PHASE 1 & DESIGN-SYSTEM HYGIENE COMPLETED; PHASES 2–3 PENDING BROWSER REVIEW]`
- **Context:** Following the Golden UI/UX Standards pass (Phase 0 & Phase 1: canonical components, zero hardcoded CSS colors, WCAG 2.1 AA contrast, `Inter` prose/labels vs. `JetBrains Mono` data, Terminal Cyan primary accent, and `ux-ui-reviewer` gate), two follow-up UX phases were scoped:
  - **Phase 2 — Focused Workspace Navigation & Row 3 Declutter:**
    - Workspace Mode Tabs on the main canvas paired with a sticky mini context bar when scrolled: default landing tab combines Screener-first Discover (`#best-option-card` at the top) + Trade Desk (KPIs, Put/Call Walls, Wall Strike Corridor, Strategy Lab, Option Chain) while placing Trade Ledger and Backtester in dedicated Portfolio / Backtest workspace tabs alongside an All Panels classic scroll mode.
    - Move advanced pull-scope controls (`Range %`, `Strike Step`, DTE fetch span, `0–29d Backtest` sync checkbox) into a clean popover beside `[REFRESH FROM IBKR]` with crisp summary telemetry (`±20% • Auto • 30–120d`) and explicit Network (IBKR/Flex) vs. Local-Cache visual indicators.
  - **Phase 3 — Backtest Polish & Power-User Ergonomics:**
    - Plain-English Backtest quality/trade flag pills with explanatory hover tooltips and a collapsible Advanced Portfolio Rules drawer for secondary backtest settings.
    - Keyboard command palette (`Ctrl+K` / `/`) for quick ticker switching and panel navigation, plus subtle non-blocking action toast feedback when adding `+Leg` contracts or sending positions to Backtest Replay.