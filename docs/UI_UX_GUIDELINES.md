# UI/UX Design Contract (`UI_UX_GUIDELINES.md`)

> **Contract, not inventory.** This file holds the rules every frontend change must respect.
> Element-by-element implementation details (helper names, selectors, animations, exact
> controls) live in `docs/reference/IMPLEMENTATION_NOTES.md` §5.1. Token values are defined
> only in `src/web/static/css/tokens.css`.

## 1. Design Philosophy: NYT Editorial Meets High-Density Terminal
1. **NYT editorial hierarchy:** high-contrast serif mastheads and section titles, double editorial rules (`--border-editorial`), clear separation between analytical blocks, journalistic framing of key levels.
2. **Financial-terminal density:** monospaced tabular numerals, compact table padding, pill/keyboard navigation, consistent semantic colors for Calls, Puts, and anomalies.

---

## 2. Dual Themes & Design Tokens
- `<html data-theme="print">` (default, **NYT Print Edition**) or `data-theme="terminal"` (**Obsidian Terminal**). The choice persists in `localStorage("ibkr_options_theme")` and is applied before first paint (inline script in `base.html`).
- Toggling the theme must re-skin every Plotly chart in place (no refetch, no reload).
- All colors, fonts, borders, and shadows come from CSS custom properties in `tokens.css`; row stylesheets and JS never hard-code theme colors (JS fallbacks in `themeVars()` mirror the token values). Every `var(--x)` must reference a token defined for **both** themes (enforced by `tests/unit/test_docs_consistency.py`).

| Token | `print` | `terminal` | Purpose |
| :--- | :--- | :--- | :--- |
| `--bg-canvas` | `#FBF9F5` | `#0A0D12` | Page background |
| `--bg-surface` | `#F3EFE6` | `#11161F` | Cards, control bars, table headers |
| `--bg-elevated` | `#FFFFFF` | `#171D29` | Inputs, active tabs |
| `--bg-table-stripe` / `--bg-hover` | `#F7F4EC` / `#ECE7DA` | `#0F141C` / `#1E2636` | Table striping, hover |
| `--text-primary` | `#111111` | `#EAECEF` | Headlines, primary numbers |
| `--text-secondary` | `#4A4A4A` | `#A0AAB8` | Subtitles, column headers |
| `--text-muted` | `#727272` | `#6C7687` | Timestamps, metadata, chart captions |
| `--border-subtle` / `--border-strong` | `#DFDCD2` / `#111111` | `#252E3E` / `#3B475C` | Hairlines / emphasis borders |
| `--border-editorial` | `3px double #111111` | `1px solid #3B475C` | Major section separators |
| `--accent-primary` | `#1A1A1A` | `#F59E0B` | Primary actions |
| `--accent-call` | `#0F6E4F` | `#10B981` | Calls, Call Walls |
| `--accent-put` | `#A82020` | `#EF4444` | Puts, Put Walls |
| `--accent-warn` | `#B45309` | `#F59E0B` | Delayed/frozen feed badges, warnings |
| `--accent-anomaly` | `#C2410C` | `#FB923C` | `Volume > OI` / Z-score anomalies |
| `--accent-focus` | `#1D4ED8` | `#38BDF8` | Sorted column, active filters, focus rings (matches the first DTE palette color) |
| `--plotly-bg` / `--plotly-grid` / `--plotly-ink` | `#FFFFFF` / `#E8E4DA` / `#111111` | `#11161F` / `#1E2636` / `#EAECEF` | Plotly paper, grid, text |
| `--corridor-itm-call-bg` | `rgba(15, 110, 79, 0.045)` | `rgba(16, 185, 129, 0.065)` | Call-side ITM tint in T-Chain corridor cells |
| `--corridor-itm-put-bg` | `rgba(168, 32, 32, 0.045)` | `rgba(239, 68, 68, 0.065)` | Put-side ITM tint in T-Chain corridor cells |
| `--corridor-leg-buy` | `#B45309` | `#F59E0B` | Long (`BUY`) Strategy Lab leg accent in Wall Corridor borders, badges & chart markers |
| `--corridor-leg-sell` | `#EA580C` | `#FB923C` | Short (`SELL`) Strategy Lab leg accent in Wall Corridor borders, badges & chart markers |
| `--corridor-leg-buy-bg` | `rgba(180, 83, 9, 0.14)` | `rgba(245, 158, 11, 0.18)` | Long (`BUY`) Strategy Lab leg tint in Wall Corridor cells & badges |
| `--corridor-leg-sell-bg` | `rgba(234, 88, 12, 0.15)` | `rgba(251, 146, 60, 0.20)` | Short (`SELL`) Strategy Lab leg tint in Wall Corridor cells & badges |
| `--wl-marquee-bg` / `--wl-marquee-border` | `rgba(29, 78, 216, 0.14)` / `var(--accent-focus)` | `rgba(56, 189, 248, 0.16)` / `var(--accent-focus)` | Watchlist marquee selection overlay & row highlight |
| `--wl-logo-bg` / `--wl-logo-fallback-ink` | `#FFFFFF` / `#FFFFFF` | `#EEF2F7` / `#FFFFFF` | Watchlist company logo tile background (light backing for dark monochrome SVGs) & 2-letter monogram fallback text |
| `--wl-active-bg` / `--wl-active-border` | `rgba(26, 26, 26, 0.06)` / `var(--accent-primary)` | `rgba(245, 158, 11, 0.11)` / `var(--accent-primary)` | Active watchlist row background tint & `3px` left indicator border |

Translucent `--accent-*-bg` variants and `--accent-atm-bg` back highlighted rows and badges.

---

## 3. Typography
Google Fonts `Newsreader`, `JetBrains Mono`, `Inter` with system fallbacks:
- **`--font-editorial`** (`Newsreader`, Georgia): masthead, editorial banner, section and panel titles, wall callouts.
- **`--font-mono`** (`JetBrains Mono`): **mandatory** for every price, strike, DTE, percentage, Greek, volume, OI, timestamp, and control input, always with `font-variant-numeric: tabular-nums` (`.tabular`, `.mono`, `td.num`).
- **`--font-sans`** (`Inter`): compact labels, tooltips, filter descriptions.

---

## 4. Page Layout Contract (`src/web/templates/index.html`)
12-column CSS Grid shell (`max-width: 1680px`, centered). Rows appear in this order:

1. **Masthead & telemetry:** serif title; Gateway badge `#gateway-badge` (`host:port` target & diagnostic notice in tooltip; online vs. amber timeout socket reset vs. link lost vs. offline cache-only); market-data badge `#md-type-badge` showing the *observed* type (`TYPE 1: LIVE OPRA`, `TYPE 2: FROZEN`, `TYPE 3: 15M DELAYED`, `TYPE 4: DELAYED-FROZEN`); Greeks source badge; theme switch.
2. **Reserved editorial banner `#editorial-banner-slot`:** full width, editorial double rules, kept structurally ready for future narrative modules (`docs/FUTURE_IMPROVEMENTS.md` §6); currently shows feed/DTE summary and snapshot metadata in `#editorial-banner-copy`.
3. **Control strip** (watchlist rules: the server-persisted list is the only ticker source; one batch at a time; the displayed view never changes during a batch; `LOAD SELECTED` and `REFRESH FROM IBKR` are disabled while a batch runs, and `×` is disabled for queued/running rows; Enter on the add box adds, runs a 1-ticker batch and switches the view when it finishes only if that ticker is done, at the cap it loads without adding and shows a notice, during a batch it only adds and shows a notice): collapsible watchlist panel `#watchlist-picker` (toggle `#watchlist-toggle` shows `#watchlist-active-logo`, active ticker `#watchlist-active`, and `count/max`; rows pair a checkbox, `[data-wl-view]` button containing a `20×20` company/ETF logo badge `.wl-logo`—or deterministic 2-letter colored monogram fallback—plus ticker text, freshness marker and `×` remove, with `.wl-row.is-active` highlighting via `--wl-active-bg` and a `3px` `--wl-active-border` left bar; supports Windows-style drag marquee selection (`Ctrl`/`Cmd` additive check, `Esc` cancel) and `Shift+Click` checkbox range toggle; footer `All` / `None` / `Stale only`, Elbstream logo attribution link, and `LOAD SELECTED`) plus an add box `#watchlist-add-form` (auto-uppercase, `ADD` button or Enter; errors and notices appear in `#alert-banner`) and a bar-level `#watchlist-load-bar`; a batch progress strip `#watchlist-progress` with Cancel (disables itself and shows `Stopping…` while the batch is `cancelling`; polling continues to the summary), and a summary (`n loaded, m failed[, k not started][, p partial][, c cancelled]`) with `Retry (n)` (re-runs every non-done item, hidden when `n = 0`) / Dismiss; an `aborted` batch shows its last notice as a warning banner; partial snapshots carry a `PARTIAL` tag in the snapshot picker (the `LATEST` tag and default selection follow `latest_snapshot_id`) and the chain view warns that they are not used by the Backtest; Range % (default `±20%`), Strike Step (default `Auto`), DTE window selector `#dte-max-select` (default `30–120d`, plus `0–29d (Short)` and `0–120d (All)` local inspection views), `0–29d Backtest` sequential-sync toggle `#include-short-dte-toggle` — options the exchange cannot serve are shown disabled `(N/A)`; ET-day grouped historical snapshot selector (`#snapshot-picker`); `[REFRESH FROM IBKR]`; dismissible `#alert-banner`; `PANELS:` bar.
4. **KPI strip (4 cards):** spot + pull scope + active expirations; Primary Call Wall; Primary Put Wall; unusual-activity count.
5. **Panel grid (snap-to-grid, resizable):** `#chart-walls` (Put/Call Walls), IV smile, unusual activity, Greeks structure, `#wall-corridor-card` (title `"Wall Strike Corridor"`), `#strategy-lab-card` (Strategy Lab), `#trade-ledger-card` (Options Trade Ledger & Roll Chains, with `Replay` row actions cross-linking open option campaigns/positions into `#backtest-card`), `#backtest-card` (Daily Options Backtester & Paper Tracker, including `[Sync from Ledger]` `#backtest-paper-sync-ledger-btn`, `Closed (Ledger)` status filtering, and `LEDGER` badges), `#best-option-card` (Best Option screener: best strategy per watchlist ticker with sortable/filterable columns, optional Live +/-1-strike updates, `Open in Strategy Lab`; full width like Backtest). Spans snap to `4/6/8/12` columns; layout persists in `localStorage`. `#walls-dte-select` governs the target expiration resolved by Strategy Lab when `#strategy-expiry-select` is in `Auto (Follow Walls)` mode; when `#corridor-expiry-select` is in Auto mode, it follows active Strategy Lab expirations (`Auto (Follow Strategy • Xd)` when Strategy Lab overrides or places legs outside the governing Walls expiry, else `Auto (Follow Walls • Xd)` fallback) while manual per-tile expiry overrides and `CUSTOM` Strategy Lab legs remain preserved.
   - **Row 4b Wall Strike Corridor (`#wall-corridor-card`):** Displays `#corridor-subs-badge` (`0 / 90 SUBS` idle; `{total} / 90 SUBS` active), `#corridor-expiry-select` (Auto follows Strategy Lab when active or Walls fallback), and `#corridor-window-select` (`±5` / `±7` / `±10` Strikes, default `±10`), pairing the 2-row 11-column `CALLS | STRIKE | PUTS` T-Chain table (`#corridor-table` / `#corridor-tbody` with Green/Red Call/Put ITM shading via `--corridor-itm-call-bg` / `--corridor-itm-put-bg` and solid-bordered `CALL WALL` / `PUT WALL` row accents, contrasting against warm amber/orange `BUY`/`SELL` Strategy Lab leg cell shading & `1.5px dotted` borders via `--corridor-leg-buy`/`--corridor-leg-sell` & `--corridor-leg-buy-bg`/`--corridor-leg-sell-bg`, dashed-border `▲ BUY` / `▼ SELL` strike badges, off-expiry rows, and per-leg native hover tooltips) alongside the `#chart-corridor-ladder` Plotly Bid-Ask Ladder (rendering `SPOT` / `PW` / `CW` plus amber/orange dotted vertical leg markers and `▲`/`▼` on-expiry and off-expiry Strategy Leg annotations), stacking vertically below `1200px`. Strategy Lab leg/expiry edits synchronize `#wall-corridor-card` locally with zero IBKR calls when idle or `ONE_SHOT` (re-flagging legs and preserving existing `ONE_SHOT` quotes whenever the target expiry and preview contracts stay covered, without calling `POST /api/strategy/evaluate`); when `#corridor-stream-toggle` is active, leg/expiry edits update highlights immediately and trigger a debounced (`400ms`) stop-before-start SSE restart only when the subscription signature (target expiry, window, or unique leg contracts) changes.
6. **Option chain table `#chain-table`:** 18 monospaced columns, sortable headers (nulls last), per-column filters, DTE bucket tabs, `+Leg` buttons feeding Strategy Lab.

**Defaults:** panels `walls`, `anomalies`, `corridor`, `strategy`, `trade_ledger`, `backtest`, `best_option` visible; `iv_smile`, `greeks` hidden (`9` toggle pills in `PANELS:`).

---

## 5. Behavioral Rules (Must Hold)
1. **Network discipline:** the only calls that reach IBKR (or Flex Web Service) are explicit user actions — `[REFRESH FROM IBKR]`, `[Refresh Corridor Quotes]`, the opt-in `Enable Live Constant Streaming` switch, `[Sync Flex / Gateway]`, `[Refresh Open MTM]`, and the opt-in `Live` checkbox of `#best-option-card` (streams screener legs over IBKR and can auto-resume after a refresh/batch/ledger lock is released while the card is visible); the screener table itself is computed from cached snapshots with zero IBKR calls. Active Paper Trade legs (`#backtest-card`) are quoted strictly as part of manual `[REFRESH FROM IBKR]` (zero background collector daemon). Changing `#dte-max-select` reloads the cached snapshot locally via `GET /api/chain/{ticker}` (with `dte_min`/`dte_max`) without hitting IBKR; `[REFRESH FROM IBKR]` optionally archives `0–29d` sequentially (`#include-short-dte-toggle`) while keeping the returned dashboard view isolated to the selected DTE window (`display_dte_min`/`display_dte_max`). Strategy Lab auto-build, Backtest simulation / Paper Trade registration, stop & Trade Ledger reconciliation (`POST /api/backtest/paper-trades/sync-ledger`, plus `Replay` triggers from `#trade-ledger-card`), Trade Ledger filtering/window/view/grouping switches (`GET /api/ledger`), local file imports (`POST /api/ledger/import`), and every snapshot/table/chart interaction are cache/local-only.
2. **Streaming teardown:** hiding the corridor panel, toggling streaming off, or leaving the page (`pagehide`) stops the SSE stream and calls `POST /api/corridor/stop` (and corridor stream starts/restarts await any in-flight `POST /api/corridor/stop` before opening a replacement `EventSource`); the same applies to the screener live stream (hiding `#best-option-card` or `pagehide` closes the `EventSource` and POSTs `/api/screener/stop` with `keepalive`, without unticking `Live`).
3. **Immutable archives:** live corridor quotes and open-position MTM refreshes (`POST /api/ledger/mtm`) update in-memory overlays only; refreshing appends immutable CSVs (`data/snapshots/` and `data/tracked/`); the UI never implies historical chain or ledger CSVs changed.
4. **Panel toggles** use `.panel-toggle-pill` (with `.active`) exclusively; no one-off toggle styles.
5. **Persistence:** every user preference uses a versioned `localStorage` key (`ibkr_*_v1` including `ibkr_ledger_prefs_v1` and `ibkr_screener_prefs_v1`, plus `ibkr_options_theme` and `ibkr_walls_*`); changing a stored shape requires a new version suffix.
6. **Errors:** API failures show a human-readable message (`detail`, then `message`) in `#alert-banner`; tracebacks are never rendered. HTTP/Gateway failures (Stage 1) are reported separately from client render failures (Stage 2 `UI_RENDER`), so a render bug is never blamed on the Gateway.
7. **Plotly lifecycle:** update charts with `Plotly.react`; clear a container's `innerHTML` only when it has no `el.data`; call `Plotly.purge(el)` before replacing a chart with a placeholder; call `Plotly.Plots.resize` after a panel is revealed or resized.
8. **JS strings** never contain HTML entities (`&bull;`, `&mdash;`); use `\u2022` / `\u2014`.
9. **Cache-busting:** after any CSS/JS change, bump the `?v=` values in `base.html` (stylesheet loop) and `index.html` (`dashboard/main.js`).
10. **Accessibility & focus:** sortable headers expose `aria-sort`; focus stays on table filter inputs while live corridor updates re-render rows.
11. **Trade Ledger view, fee & stock-gain separation (`#trade-ledger-card`):** `Realized & Credits` (`realized`) and `Open & Unrealized MTM` (`open_mtm`) are strictly separate views — switching to `Open & Unrealized MTM` disables grouping pills (`Roll Campaigns | By Strategy | By Ticker | Executions`) so open MTM is never commingled with realized cashflows. Headline realized/unrealized P&L is always displayed **ex-fees**, with total commissions surfaced in the sidecar `Total Fees Paid` KPI card / `Fees` column (and `After Fees:` subtext). `1W | 1M | 3M | YTD | 1Y | ALL` filters the realized timeframe, and `Include Stock Gain (Covered Calls)` explicitly toggles inclusion of stock lot realizations / assignment capital gains and open `STK` MTM.

---

## 6. Plotly Chart Styling (`src/web/static/js/dashboard/charts.js`)
- Never use Plotly's default theme. Every layout reads colors and fonts from `themeVars()` (`--plotly-bg`, `--plotly-grid`, `--plotly-ink`, `--text-muted`, `--accent-call|put|anomaly|warn`, `--font-mono`) and is re-applied on theme toggle.
- Axis ticks and hover labels use `--font-mono`; the walls strike axis labels only strikes present in the chain.
- Always draw framed reference markers for `SPOT` (`underlying_price`), **Primary Call Wall**, and **Primary Put Wall** (horizontal on the walls ladder view, vertical on the other charts), annotated with DTE attribution on `#chart-walls`.
- Calls use `--accent-call`, Puts `--accent-put`, anomalies `--accent-anomaly`; multi-DTE series use the theme's DTE palette consistently across charts.
