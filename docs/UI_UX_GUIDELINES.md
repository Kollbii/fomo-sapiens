# UI/UX Design Contract (`UI_UX_GUIDELINES.md`)

> **Contract, not inventory.** This file holds the rules every frontend change must respect.
> Element-by-element implementation details (helper names, selectors, animations, exact
> controls) live in `docs/reference/IMPLEMENTATION_NOTES.md` §5.1. Token values are defined
> only in `src/web/static/css/tokens.css`.

## 1. Design Philosophy & Golden UI/UX Standards
1. **NYT editorial hierarchy:** high-contrast serif mastheads and section titles, double editorial rules (`--border-editorial`), clear separation between analytical blocks, journalistic framing of key levels.
2. **Financial-terminal density:** monospaced tabular numerals, compact table padding, pill/keyboard navigation, consistent semantic colors for Calls, Puts, and anomalies.
3. **Golden UX heuristics (Nielsen & Shneiderman):**
   - **Visibility of system status:** every network pull, background batch, SSE stream, or local calculation surfaces its source, progress, and freshness without ambiguity; interactive controls expose immediate hover, pressed, dirty, and `:focus-visible` states.
   - **Match between system and real world:** never surface internal enum names or raw error codes without plain-English explanation; reserve technical diagnostic details for collapsible `<details>` sections or hover tooltips.
   - **Consistency & standards (Canonical Component Registry):** reuse shared panel headers, button tiers, badges, segmented controls, and KPI cards rather than introducing ad-hoc CSS variants or dead class names.
   - **Overview first, filter/zoom, then details-on-demand:** keep high-level metrics and top-ranked opportunities scannable; tuck optional diagnostics, secondary inputs, and leg breakdowns behind expandable drawers or inline detail rows (`Progressive Disclosure`).
   - **Error prevention & Network-vs-Cache clarity:** visually distinguish IBKR/Flex network actions (gateway-dependent) from zero-network local cache operations (instant), disable unsupported options explicitly, and surface non-blocking inline banners on failures.
   - **Accessibility & legibility (WCAG 2.1 AA):** normal-text semantic foregrounds and primary CTA text clear `>= 4.5:1` contrast against all three surfaces (`--bg-canvas`, `--bg-surface`, `--bg-elevated`) in both themes; readable prose stays `>= 12px` and UI badges/pills never fall below `10px`; every interactive control shows a clear `2px solid` `--accent-focus` outline on keyboard `:focus-visible`. Enforced mechanically by `tests/unit/test_ui_integrity.py`.

### Canonical Component Registry

| Primitive | Canonical Classes | Avoid / Notes |
| :--- | :--- | :--- |
| Panel Card & Header | `.panel-card` > `header.panel-header` > `.panel-title-stack` + `.panel-controls` | Never use dead `.panel-head`, `.panel-head-left`, or `.panel-actions`. |
| Buttons & Scope Cues | `.btn` (default `11px`), `.btn-primary`, `.btn-sm` (`11px`, `28px`), `.btn-xs` (`11px`, `26px`), `.btn--network` (`data-action-scope="network"`) | `.wl-btn-small` applies strictly inside `.wl-picker__footer` / `.wl-progress`; `.btn--network` marks IBKR/Flex requests visually. |
| Status & Scope Badges | `.badge`, `.badge--online`, `.badge--offline`, `.badge--info`, `.badge--live`, `.badge--amber`, `.badge--warn`, `.badge--delayed`, `.badge--muted`, `.scope-tag--network`, `.scope-tag--local` | Reserve `--accent-warn` strictly for warnings, delayed state, and open flags—never primary CTAs. |
| Panel & Workspace Toggles | `.panel-toggle-pill` (with `.active` + `aria-pressed`); `.workspace-tab` (with `.is-active` + `aria-selected`) | Exclusive panel & workspace switcher styles; no ad-hoc tab variants. |
| Segmented Controls | `.ledger-seg-group` / `.ledger-seg-btn`, `.backtest-seg-group` / `.backtest-seg-btn`, `.dte-bucket-tabs` / `.dte-tab-btn` | Keep `11px` `--font-mono` labels and `--accent-focus` `:focus-visible` outlines. |

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
| `--text-muted` | `#5C5C5C` | `#8B97AA` | Timestamps, metadata, chart captions (WCAG AA `>= 4.5:1`) |
| `--border-subtle` / `--border-strong` | `#DFDCD2` / `#111111` | `#252E3E` / `#3B475C` | Hairlines / emphasis borders |
| `--border-editorial` | `3px double #111111` | `1px solid #3B475C` | Major section separators |
| `--accent-primary` | `#1A1A1A` | `#38BDF8` | Primary actions & active tabs (Terminal Cyan `9.08:1` on canvas) |
| `--accent-call` | `#0F6E4F` | `#10B981` | Calls, Call Walls |
| `--accent-put` | `#A82020` | `#F25555` | Puts, Put Walls (WCAG AA `>= 4.5:1` on all surfaces) |
| `--accent-warn` | `#9A4306` | `#F59E0B` | Delayed/frozen feed badges, warnings (never mixed with Terminal primary CTA) |
| `--accent-anomaly` | `#B83B0A` | `#FB923C` | `Volume > OI` / Z-score anomalies |
| `--accent-focus` / `--accent-focus-bg` | `#1D4ED8` / `rgba(29, 78, 216, 0.10)` | `#38BDF8` / `rgba(56, 189, 248, 0.14)` | Sorted column, active tabs/filters, `:focus-visible` outlines |
| `--badge-delayed-ink` | `#1E3A8A` | `#38BDF8` | High-contrast delayed badge text |
| `--accent-atm-bg` | `rgba(17, 17, 17, 0.06)` | `rgba(56, 189, 248, 0.10)` | ATM strike row highlight & expanded screener row |
| `--plotly-bg` / `--plotly-grid` / `--plotly-ink` | `#FFFFFF` / `#E8E4DA` / `#111111` | `#11161F` / `#1E2636` / `#EAECEF` | Plotly paper, grid, text |
| `--corridor-itm-call-bg` | `rgba(15, 110, 79, 0.045)` | `rgba(16, 185, 129, 0.065)` | Call-side ITM tint in T-Chain corridor cells |
| `--corridor-itm-put-bg` | `rgba(168, 32, 32, 0.045)` | `rgba(239, 68, 68, 0.065)` | Put-side ITM tint in T-Chain corridor cells |
| `--corridor-leg-buy` | `#B45309` | `#F59E0B` | Long (`BUY`) Strategy Lab leg accent in Wall Corridor borders, badges & chart markers |
| `--corridor-leg-sell` | `#EA580C` | `#FB923C` | Short (`SELL`) Strategy Lab leg accent in Wall Corridor borders, badges & chart markers |
| `--corridor-leg-buy-bg` | `rgba(180, 83, 9, 0.14)` | `rgba(245, 158, 11, 0.18)` | Long (`BUY`) Strategy Lab leg tint in Wall Corridor cells & badges |
| `--corridor-leg-sell-bg` | `rgba(234, 88, 12, 0.15)` | `rgba(251, 146, 60, 0.20)` | Short (`SELL`) Strategy Lab leg tint in Wall Corridor cells & badges |
| `--wl-marquee-bg` / `--wl-marquee-border` | `rgba(29, 78, 216, 0.14)` / `var(--accent-focus)` | `rgba(56, 189, 248, 0.16)` / `var(--accent-focus)` | Watchlist marquee selection overlay & row highlight |
| `--wl-logo-bg` / `--wl-logo-fallback-ink` | `#FFFFFF` / `#FFFFFF` | `#EEF2F7` / `#FFFFFF` | Watchlist company logo tile background (light backing for dark monochrome SVGs) & 2-letter monogram fallback text |
| `--wl-active-bg` / `--wl-active-border` | `rgba(26, 26, 26, 0.06)` / `var(--accent-primary)` | `rgba(56, 189, 248, 0.12)` / `var(--accent-primary)` | Active watchlist row background tint & `3px` left indicator border |
| `--shadow-card` / `--shadow-popover` | `none` / `0 8px 24px rgba(0, 0, 0, 0.25)` | `0 1px 3px rgba(0, 0, 0, 0.45)` / `0 8px 24px rgba(0, 0, 0, 0.45)` | Card elevation & popover dropdown depth |
| `--alert-pulse-ring` / `--alert-pulse-ring-end` | `rgba(217, 119, 6, 0.35)` / `rgba(217, 119, 6, 0)` | `rgba(245, 158, 11, 0.35)` / `rgba(245, 158, 11, 0)` | Fresh alert banner pulse keyframe rings |
| `--hover-tint-subtle` | `rgba(128, 128, 128, 0.14)` | `rgba(148, 163, 184, 0.16)` | Subtle neutral hover/close button wash |

Translucent `--accent-*-bg` variants back highlighted rows and badges. `--shadow-popover`, `--alert-pulse-ring`, `--alert-pulse-ring-end`, `--hover-tint-subtle`, and all theme colors must stay inside `tokens.css` (enforced by `tests/unit/test_ui_integrity.py`).

---

## 3. Typography (Pure Two-Voice Editorial Terminal)
Google Fonts `Newsreader` + `JetBrains Mono` (plus `Inter` reserved strictly for 2-letter monogram avatars) with system fallbacks:
- **`--font-editorial`** (`Newsreader`, Georgia): masthead, editorial banner copy, section and panel titles, wall callouts.
- **`--font-mono`** (`JetBrains Mono`): **unified single voice** across all interactive chrome (`.btn`, toolbar labels, toggle pills, segmented tabs, checkboxes), notices/rationales/warnings/assumptions/empty states (`12px`), AND every price, strike, DTE, percentage, Greek, volume, OI, timestamp, ticker symbol, data table, numeric/ticker input, and status badge, with `font-variant-numeric: tabular-nums` (`.tabular`, `.mono`, `td.num`). Keep UI badges `>= 10px`.
- **`--font-sans`** (`Inter`): reserved strictly for compact circular/square `.wl-logo__fallback` avatar monograms (enforced by `tests/unit/test_ui_integrity.py`). Never introduce `--font-sans` into toolbars, buttons, or panels alongside `--font-mono`.

---

## 4. Page Layout Contract (`src/web/templates/index.html`)
12-column CSS Grid shell (`max-width: 1680px`, centered). Rows appear in this order:

1. **Masthead & telemetry:** serif title; Gateway badge `#gateway-badge` (`host:port` target & diagnostic notice in tooltip; online vs. amber timeout socket reset vs. link lost vs. offline cache-only); market-data badge `#md-type-badge` showing the *observed* type (`TYPE 1: LIVE OPRA`, `TYPE 2: FROZEN`, `TYPE 3: 15M DELAYED`, `TYPE 4: DELAYED-FROZEN`); Greeks source badge; theme switch. Paired with `#sticky-context-bar` (sliding into view once `.control-bar` scrolls above the viewport) showing active ticker/logo, spot, Primary Call/Put Walls, snapshot metadata, compact workspace tabs, `[REFRESH]`, and `[TOP]`.
2. **Reserved editorial banner `#editorial-banner-slot`:** full width, editorial double rules, kept structurally ready for future narrative modules (`docs/FUTURE_IMPROVEMENTS.md` §6); currently shows feed/DTE summary and snapshot metadata in `#editorial-banner-copy`.
3. **Control strip** (watchlist rules: the server-persisted list is the only ticker source; one batch at a time; the displayed view never changes during a batch; `LOAD SELECTED` and `REFRESH FROM IBKR` are disabled while a batch runs, and `×` is disabled for queued/running rows; Enter on the add box adds, runs a 1-ticker batch and switches the view when it finishes only if that ticker is done, at the cap it loads without adding and shows a notice, during a batch it only adds and shows a notice): top **Workspace Mode Tabs** (`#workspace-tabs`: `Discover & Desk` default combining Screener-first `#best-option-card` via CSS `order: -1` + Trade Desk cards & `#chain-table`; `Portfolio` showing only `#trade-ledger-card` without top KPI strip or chain table; `Backtest` showing only `#backtest-card` without top KPI strip or chain table; `All Panels` classic full scroll) + Network-vs-Local legend; collapsible watchlist panel `#watchlist-picker` (toggle `#watchlist-toggle` shows `#watchlist-active-logo`, active ticker `#watchlist-active`, and `count/max`; rows pair a checkbox, `[data-wl-view]` button containing a `20×20` company/ETF logo badge `.wl-logo`—or deterministic 2-letter colored monogram fallback—plus ticker text, freshness marker and `×` remove, with `.wl-row.is-active` highlighting via `--wl-active-bg` and a `3px` `--wl-active-border` left bar; supports Windows-style drag marquee selection (`Ctrl`/`Cmd` additive check, `Esc` cancel) and `Shift+Click` checkbox range toggle; footer `All` / `None` / `Stale only`, Elbstream logo attribution link, and `LOAD SELECTED`) plus an add box `#watchlist-add-form` (auto-uppercase, `ADD` button or Enter; errors and notices appear in `#alert-banner`), ET-day grouped historical snapshot selector (`#snapshot-picker`), and local `EXPORT CSV`; right network action cluster (`control-group--network`) pairing a collapsible **Pull Scope Popover** (`#pull-scope-picker` / `#pull-scope-toggle` / `#pull-scope-summary` / `#pull-scope-panel`, cleanly separating **IBKR Pull Scope** [Range % default `±20%`, Strike Step default `Auto`, `0–29d Backtest` sequential-sync `#include-short-dte-toggle` — unsupported values disabled `(N/A)`] from **Local Dashboard View** [DTE `#dte-max-select` `30–120d` default / `0–29d (Short)` / `0–120d (All)`]) with `[REFRESH FROM IBKR]` (`.btn--network`) and bar-level `[LOAD SELECTED]` (`.btn--network`); batch progress strip `#watchlist-progress` with Cancel (`Stopping…` while `cancelling`), summary (`Retry (n)` / Dismiss; `aborted` shows last notice as warning, partial snapshots carry `PARTIAL` tag); dismissible `#alert-banner`; `PANELS:` bar (visible in `Discover & Desk` and `All Panels` modes, filtering toggle pills to the active workspace).
4. **KPI strip (4 cards):** spot + pull scope + active expirations; Primary Call Wall; Primary Put Wall; unusual-activity count.
5. **Panel grid (snap-to-grid, resizable):** `#chart-walls` (Put/Call Walls), IV smile, unusual activity, Greeks structure, `#wall-corridor-card` (title `"Wall Strike Corridor"`), `#strategy-lab-card` (Strategy Lab), `#trade-ledger-card` (Options Trade Ledger & Roll Chains, with `Replay` row actions cross-linking open option campaigns/positions into `#backtest-card`), `#backtest-card` (Daily Options Backtester & Paper Tracker, including a default-collapsed inline Paper Trades drawer `#backtest-paper-toggle` with aligned table, ticker filter, auto-collapse on `Replay`, `[Sync from Ledger]` `#backtest-paper-sync-ledger-btn`, `Closed (Ledger)` status filtering, and `LEDGER` badges, plus a draggable `62% / 38%` Trade & Legs vs. Daily MTM split `#backtest-tables-splitter`), `#best-option-card` (Best Option screener: best strategy per watchlist ticker with sortable/filterable columns, optional Live +/-1-strike updates, `Open in Strategy Lab`; full width like Backtest). Spans snap to `4/6/8/12` columns; layout persists in `localStorage`. `#walls-dte-select` governs the target expiration resolved by Strategy Lab when `#strategy-expiry-select` is in `Auto (Follow Walls)` mode; when `#corridor-expiry-select` is in Auto mode, it follows active Strategy Lab expirations (`Auto (Follow Strategy • Xd)` when Strategy Lab overrides or places legs outside the governing Walls expiry, else `Auto (Follow Walls • Xd)` fallback) while manual per-tile expiry overrides and `CUSTOM` Strategy Lab legs remain preserved.
   - **Row 4b Wall Strike Corridor (`#wall-corridor-card`):** Displays `#corridor-subs-badge` (`0 / 90 SUBS` idle; `{total} / 90 SUBS` active), `#corridor-expiry-select` (Auto follows Strategy Lab when active or Walls fallback), and `#corridor-window-select` (`±5` / `±7` / `±10` Strikes, default `±10`), pairing the 2-row 11-column `CALLS | STRIKE | PUTS` T-Chain table (`#corridor-table` / `#corridor-tbody` with Green/Red Call/Put ITM shading via `--corridor-itm-call-bg` / `--corridor-itm-put-bg` and solid-bordered `CALL WALL` / `PUT WALL` row accents, contrasting against warm amber/orange `BUY`/`SELL` Strategy Lab leg cell shading & `1.5px dotted` borders via `--corridor-leg-buy`/`--corridor-leg-sell` & `--corridor-leg-buy-bg`/`--corridor-leg-sell-bg`, dashed-border `▲ BUY` / `▼ SELL` strike badges, off-expiry rows, and per-leg native hover tooltips) alongside the `#chart-corridor-ladder` Plotly Bid-Ask Ladder (rendering `SPOT` / `PW` / `CW` plus amber/orange dotted vertical leg markers and `▲`/`▼` on-expiry and off-expiry Strategy Leg annotations), stacking vertically below `1200px`. Strategy Lab leg/expiry edits synchronize `#wall-corridor-card` locally with zero IBKR calls when idle or `ONE_SHOT` (re-flagging legs and preserving existing `ONE_SHOT` quotes whenever the target expiry and preview contracts stay covered, without calling `POST /api/strategy/evaluate`); when `#corridor-stream-toggle` is active, leg/expiry edits update highlights immediately and trigger a debounced (`400ms`) stop-before-start SSE restart only when the subscription signature (target expiry, window, or unique leg contracts) changes.
6. **Option chain table `#chain-table`:** 18 monospaced columns, sortable headers (nulls last), per-column filters, DTE bucket tabs, `+Leg` buttons feeding Strategy Lab.

**Defaults:** panels `walls`, `anomalies`, `corridor`, `strategy`, `trade_ledger`, `backtest`, `best_option` visible; `iv_smile`, `greeks` hidden (`9` toggle pills in `PANELS:`).

---

## 5. Behavioral Rules (Must Hold)
1. **Network discipline:** the only calls that reach IBKR (or Flex Web Service) are explicit user actions — `[REFRESH FROM IBKR]`, `[Refresh Corridor Quotes]`, the opt-in `Enable Live Constant Streaming` switch, `[Sync Flex / Gateway]`, `[Refresh Open MTM]`, and the opt-in `Live` checkbox of `#best-option-card` (streams screener legs over IBKR and can auto-resume after a refresh/batch/ledger lock is released while the card is visible); the screener table itself is computed from cached snapshots with zero IBKR calls. Active Paper Trade legs (`#backtest-card`) are quoted strictly as part of manual `[REFRESH FROM IBKR]` (zero background collector daemon). Changing `#dte-max-select` reloads the cached snapshot locally via `GET /api/chain/{ticker}` (with `dte_min`/`dte_max`) without hitting IBKR; `[REFRESH FROM IBKR]` optionally archives `0–29d` sequentially (`#include-short-dte-toggle`) while keeping the returned dashboard view isolated to the selected DTE window (`display_dte_min`/`display_dte_max`). Strategy Lab auto-build, Backtest simulation / Paper Trade registration, stop & Trade Ledger reconciliation (`POST /api/backtest/paper-trades/sync-ledger`, plus `Replay` triggers from `#trade-ledger-card`), Trade Ledger filtering/window/view/grouping switches (`GET /api/ledger`), local file imports (`POST /api/ledger/import`), and every snapshot/table/chart interaction are cache/local-only.
2. **Streaming teardown & workspace switching:** hiding the corridor panel, switching to a workspace tab without `corridor` (`Portfolio` / `Backtest`), toggling streaming off, or leaving the page (`pagehide`) stops the SSE stream and calls `POST /api/corridor/stop` (and corridor stream starts/restarts await any in-flight `POST /api/corridor/stop` before opening a replacement `EventSource`); the same applies to the screener live stream (hiding `#best-option-card`, switching to `Portfolio`/`Backtest` workspace, or `pagehide` closes the `EventSource` and POSTs `/api/screener/stop` with `keepalive`, without unticking `Live`, and auto-resumes when returning to `Discover & Desk` or `All Panels`). Cross-panel actions (`Open in Strategy Lab`, Trade Ledger `Replay`) automatically switch workspace tabs via `ensurePanelVisible()`.
3. **Immutable archives:** live corridor quotes and open-position MTM refreshes (`POST /api/ledger/mtm`) update in-memory overlays only; refreshing appends immutable CSVs (`data/snapshots/` and `data/tracked/`); the UI never implies historical chain or ledger CSVs changed.
4. **Panel & workspace toggles** use `.panel-toggle-pill` (with `.active`) and `.workspace-tab` (with `.is-active`) exclusively; no one-off toggle styles.
5. **Persistence:** every user preference uses a versioned `localStorage` key (`ibkr_*_v1` including `ibkr_workspace_mode_v1`, `ibkr_ledger_prefs_v1`, `ibkr_screener_prefs_v1`, and `ibkr_backtest_split_v1`, plus `ibkr_options_theme` and `ibkr_walls_*`); changing a stored shape requires a new version suffix.
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
