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

1. **Masthead & telemetry:** serif title; Gateway badge (`host:port`, connected vs. `OFFLINE CACHE`); market-data badge `#md-type-badge` showing the *observed* type (`TYPE 1: LIVE OPRA`, `TYPE 2: FROZEN`, `TYPE 3: 15M DELAYED`, `TYPE 4: DELAYED-FROZEN`); Greeks source badge; theme switch.
2. **Reserved editorial banner `#editorial-banner-slot`:** full width, editorial double rules, kept structurally ready for future narrative modules (`docs/FUTURE_IMPROVEMENTS.md` §6); currently shows feed/DTE summary and snapshot metadata in `#editorial-banner-copy`.
3. **Control strip:** watchlist pills (`NVDA`, `AVGO`, `CRDO`, `PLTR`); custom ticker input (`maxlength="5"`, `pattern="[A-Za-z]{1,5}"`, auto-uppercase, inline `422` error); Range % (default `±20%`), Strike Step (default `Auto`), DTE ceiling (default `30–120d`) — options the exchange cannot serve are shown disabled `(N/A)`; historical snapshot selector; `[REFRESH FROM IBKR]`; dismissible `#alert-banner`; `PANELS:` bar. Scheduled-collection control `#collector-control` (toggle + settings/status popover); historical snapshot picker `#snapshot-picker` grouping snapshots by exchange day, with off-session snapshots marked.
4. **KPI strip (4 cards):** spot + pull scope + active expirations; Primary Call Wall; Primary Put Wall; unusual-activity count.
5. **Panel grid (snap-to-grid, resizable):** `#chart-walls` (Put/Call Walls), IV smile, unusual activity, Greeks structure, `#wall-corridor-card` (Wall Corridor), `#strategy-lab-card` (Strategy Lab), `#backtest-card` (Option Backtest Analyzer: one bull put spread held to expiry, P&L per time step on an evenly spaced, not time-proportional axis; click-only). Spans snap to `4/6/8/12` columns; layout persists in `localStorage`.
6. **Option chain table `#chain-table`:** 18 monospaced columns, sortable headers (nulls last), per-column filters, DTE bucket tabs, `+Leg` buttons feeding Strategy Lab.

**Defaults:** panels `walls`, `anomalies`, `corridor`, `strategy`, `backtest` visible; `iv_smile`, `greeks` hidden.

---

## 5. Behavioral Rules (Must Hold)
1. **Network discipline:** the only calls that reach IBKR are explicit user actions — `[REFRESH FROM IBKR]`, `[Refresh Corridor Quotes]`, and the opt-in `Enable Live Constant Streaming` switch. Strategy Lab auto-build and every snapshot/table/chart interaction are cache-only.
2. **Streaming teardown:** hiding the corridor panel, toggling streaming off, or leaving the page (`pagehide`) stops the SSE stream and calls `POST /api/corridor/stop`.
3. **Immutable archives:** live corridor quotes update the in-memory chain only; the UI never implies archived CSVs changed.
4. **Panel toggles** use `.panel-toggle-pill` (with `.active`) exclusively; no one-off toggle styles.
5. **Persistence:** every user preference uses a versioned `localStorage` key (`ibkr_*_v1`, plus `ibkr_options_theme` and `ibkr_walls_*`); changing a stored shape requires a new version suffix.
6. **Errors:** API failures show a human-readable message (`detail`, then `message`) in `#alert-banner`; tracebacks are never rendered. HTTP/Gateway failures (Stage 1) are reported separately from client render failures (Stage 2 `UI_RENDER`), so a render bug is never blamed on the Gateway.
7. **Plotly lifecycle:** update charts with `Plotly.react`; clear a container's `innerHTML` only when it has no `el.data`; call `Plotly.purge(el)` before replacing a chart with a placeholder; call `Plotly.Plots.resize` after a panel is revealed or resized.
8. **JS strings** never contain HTML entities (`&bull;`, `&mdash;`); use `\u2022` / `\u2014`.
9. **Cache-busting:** after any CSS/JS change, bump the `?v=` values in `base.html` (stylesheet loop) and `index.html` (`dashboard/main.js`).
10. **Accessibility & focus:** sortable headers expose `aria-sort`; focus stays on table filter inputs while live corridor updates re-render rows.

---

## 6. Plotly Chart Styling (`src/web/static/js/dashboard/charts.js`)
- Never use Plotly's default theme. Every layout reads colors and fonts from `themeVars()` (`--plotly-bg`, `--plotly-grid`, `--plotly-ink`, `--text-muted`, `--accent-call|put|anomaly|warn`, `--font-mono`) and is re-applied on theme toggle.
- Axis ticks and hover labels use `--font-mono`; the walls strike axis labels only strikes present in the chain.
- Always draw framed reference markers for `SPOT` (`underlying_price`), **Primary Call Wall**, and **Primary Put Wall** (horizontal on the walls ladder view, vertical on the other charts), annotated with DTE attribution on `#chart-walls`.
- Calls use `--accent-call`, Puts `--accent-put`, anomalies `--accent-anomaly`; multi-DTE series use the theme's DTE palette consistently across charts.
