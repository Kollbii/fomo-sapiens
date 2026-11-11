# UI/UX Design Paradigm & Styling Specification (`UI_UX_GUIDELINES.md`)

## 1. Core Design Philosophy: NYT Editorial Meets High-Density Terminal
The interface merges two distinct visual languages into a unified, zero-fluff workspace:
1. **New York Times Editorial Hierarchy:** High-contrast serif mastheads and section titles, double-line horizontal rules (`border-top: 3px double`), generous structural clarity between analytical blocks, and journalistic framing of key levels.
2. **Bloomberg / CLI Financial Terminal Density:** Monospaced tabular numerals (`font-variant-numeric: tabular-nums`), compact padding on option chain tables, instant keyboard/pill navigation, and crisp semantic color coding for Calls, Puts, and Volume/OI anomalies.

---

## 2. Dual-Theme Polarity (`Variant 3: Print Edition <-> Obsidian Terminal`)
The root `<html>` element toggles `data-theme="print"` (default) and `data-theme="terminal"` via a persistent header switch stored in `localStorage`. All CSS rules in `src/web/static/css/tokens.css` and all Plotly chart layouts MUST read from these exact design tokens:

| Design Token | `data-theme="print"` (NYT Print Edition) | `data-theme="terminal"` (Obsidian Terminal) | Purpose |
| :--- | :--- | :--- | :--- |
| `--bg-primary` | `#FBF9F5` (Warm Newsprint) | `#0B0D10` (Deep Obsidian) | Main page background |
| `--bg-surface` | `#F3EFE6` (Muted Paper Card) | `#12161C` (Terminal Panel) | Metric cards, control bars, table headers |
| `--bg-elevated` | `#FFFFFF` (Crisp Ink Paper) | `#181D25` (Active Row/Input) | Inputs, active tabs, hover states |
| `--text-primary` | `#111111` (Stark Black Ink) | `#ECEFF4` (Crisp Ivory/White) | Headlines, primary numbers |
| `--text-secondary` | `#4A4A48` (Charcoal Subtext) | `#94A3B8` (Muted Slate) | Subtitles, column headers, axis labels |
| `--text-muted` | `#767672` (Editorial Caption) | `#64748B` (Dim Telemetry) | Timestamps, metadata badges |
| `--border-hairline` | `#D8D3C8` (`1px solid`) | `#222934` (`1px solid`) | Cell dividers, grid hairlines |
| `--border-editorial`| `#111111` (`3px double`) | `#334155` (`3px double`) | Major section separators |
| `--accent-call` | `#0F6E4F` (Forest Emerald) | `#10B981` (Terminal Emerald) | Call Walls, Call OI/Volume bars |
| `--accent-put` | `#A82020` (Editorial Crimson) | `#F43F5E` (Terminal Rose/Red) | Put Walls, Put OI/Volume bars |
| `--accent-anomaly` | `#B45309` (Amber Ochre) | `#F59E0B` (Terminal Amber) | `Volume > OI` anomalies, Type 3/4 badges |
| `--accent-focus` | `#1D4ED8` (Editorial Cobalt) | `#38BDF8` (Terminal Cyan) | Active DTE curve highlight, focus ring |

---

## 3. Typography System
Load Google Fonts (`Newsreader` or `Playfair Display` + `JetBrains Mono` + `Inter`) with clean local system fallbacks:
- **`--font-editorial` (`'Newsreader', 'Playfair Display', Georgia, serif`):**
  - Used for the Top Masthead, Reserved Editorial Banner headlines, Section Headers (`Put & Call Wall Structure`, `Multi-DTE Volatility & Greeks`), and Primary Wall Callout Titles.
- **`--font-mono` (`'JetBrains Mono', 'SFMono-Regular', Consolas, monospace`):**
  - Mandatory for **all** prices, strikes, DTE badges, percentages, Greeks, volumes, open interest, timestamps, and control inputs. Always enforce `font-variant-numeric: tabular-nums`.
- **`--font-sans` (`'Inter', -apple-system, BlinkMacSystemFont, sans-serif`):**
  - Used for compact UI labels, tooltips, and filter descriptions.

---

## 4. Page Layout Architecture (`src/web/templates/index.html`)
The viewport uses a disciplined 12-column CSS Grid (`max-width: 1680px`, `margin: 0 auto`, `padding: 1rem 1.5rem`):

1. **Row 1 — Editorial Masthead & System Telemetry Bar:**
   - Left: Serif Masthead title (`IBKR OPTIONS ANALYZER`) + edition date & active snapshot timestamp.
   - Right: Connection status badge (`PORT 4001: CONNECTED` or `OFFLINE CACHE`), Market Data badge (`TYPE 3: 15M DELAYED` / `TYPE 4: FROZEN`), Greeks source badge (`IBKR MODEL` / `BSM FALLBACK`), and **Theme Switcher (`Print / Terminal`)**.
2. **Row 2 — Reserved Top Editorial Banner Slot (`#editorial-banner-slot`):**
   - **Mandatory Architectural Requirement:** A dedicated full-width structural container (`min-height: 64px`, collapsible via a subtle corner toggle, styled with top and bottom editorial double rules) reserved above the control bar for future executive narrative/banner modules. In Phase 1, it renders a clean, minimal placeholder frame showing key snapshot metadata without clutter.
3. **Row 3 — Command & Filter Control Strip:**
   - **Watchlist Quick-Pills:** `[NVDA]` `[AVGO]` `[CRDO]` `[PLTR]` (1-click switching).
   - **Validated Custom Ticker Input:** Uppercase monospaced input (`maxlength="5"`, pattern `[A-Z]{1,5}`) + `Load` / `Refresh` button.
   - **Chain Parameters:**
     - **Moneyness Range (`%`):** Dropdown/segmented control (`+-10%`, `+-15%`, `+-20%` [default], `+-25%`, `+-35%`, `+-50%`).
     - **Strike Step (`$`):** Dropdown (`Auto`, `$1`, `$2.5`, `$5` [default], `$10`, `$25`).
   - **Historical Snapshot Selector:** Dropdown populated from `snapshot_index.json` allowing instant switching between any archived CSV for the active ticker.
   - **Manual Refresh Trigger:** Prominent `[REFRESH FROM IBKR]` button with page-progress indicator (`Fetching page 1/2...`).
4. **Row 4 — High-Density KPI Summary Strip (4 Columns):**
   - **Card 1:** Underlying Spot Price + Active Expirations in `[30, 60)` window.
   - **Card 2:** Primary Call Wall (Strike, OI / Volume concentration, `%` distance from spot).
   - **Card 3:** Primary Put Wall (Strike, OI / Volume concentration, `%` distance from spot).
   - **Card 4:** Unusual Activity Count (`Volume > OI` contracts & top Volume Z-Score strike/DTE).
5. **Row 5 — Interactive Multi-DTE Plotly Canvas (2x2 Grid):**
   - **Chart A (Top-Left):** Put Walls & Call Walls by Strike (Stacked/Grouped Bar or Butterfly Profile across `[30, 35, 40, 45, 60]` DTE, toggleable between OI and Volume).
   - **Chart B (Top-Right):** Multi-DTE Implied Volatility Smile / Skew Overlay (lines colored distinctly per DTE bucket with vertical ATM spot marker).
   - **Chart C (Bottom-Left):** Unusual Activity Heatmap / Scatter (`Volume / OI Ratio` or `Volume Z-Score` by Strike and DTE).
   - **Chart D (Bottom-Right):** Multi-DTE Greeks Structure (`Delta`, `Gamma`, `Theta`, `Vega` selector overlaid across the 5 DTE buckets).
6. **Row 6 — High-Density Tabular Option Chain & Anomaly Filter:**
   - DTE tabs (`ALL DTEs`, `~30d`, `~35d`, `~40d`, `~45d`, `~60d`) + `[Unusual Activity Only]` toggle.
   - Compact monospaced table (`padding: 4px 8px`, `font-size: 12px`) with sticky header, ATM row highlight, inline horizontal volume/OI bars, and amber badges on `Volume > OI` rows.

---

## 5. Plotly Chart Styling Rules (`src/web/static/js/dashboard.js`)
- Never use Plotly's default white/blue theme.
- All charts must dynamically apply `--bg-surface`, `--text-primary`, `--border-hairline`, and `--font-mono` for axis ticks whenever the user toggles between **Print Edition** and **Obsidian Terminal**.
- Always draw a vertical dashed reference line at `underlying_price` (`SPOT`) and subtle vertical shaded markers at the **Primary Call Wall** and **Primary Put Wall**.