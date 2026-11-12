# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Version labels below match the `docs(sync): … for X.Y.Z`
milestones in git history.

## [Unreleased]

### Added
- Static read-only security guard `scripts/security_guard.py` (`make security-guard`): blocks order/mutation APIs, account/position queries, `time.sleep`, and `pandas` imports in `src/`.
- Multi-platform sub-agents: canonical specs in `.agent/`, generated Claude Code (`.claude/agents/`) and Gemini CLI (`.gemini/agents/`, `.gemini/settings.json`) adapters with per-agent model/reasoning routing (`make agents-sync`, `make agents-check`); new read-only `safety-reviewer` agent.
- Shared test support (`tests/support/`), `tests/conftest.py` fixtures, golden snapshot fixture with an offline end-to-end smoke test, `make coverage` (pytest-cov), and `make js-check` (`node --check`).
- `tests/unit/test_docs_consistency.py` guarding documented element ids and API routes.
- `README.md` quickstart, this changelog, and `docs/reference/IMPLEMENTATION_NOTES.md`.

### Changed
- The FastAPI lifespan creates the `data/` storage layout (`CSVRepository.ensure_layout()`); Makefile mkdir one-liners removed.
- `AGENTS.md` phase history condensed into a module/status map (history moved here); per-module implementation notes moved out of `docs/ARCHITECTURE.md` / `docs/DATA_SCHEMA.md`.
- `pyproject.toml` package readme is now `README.md`.
- Reproducible environments: `make setup` runs `uv sync --locked --extra dev`; the Docker image is multi-stage, installs runtime dependencies only from `uv.lock`, runs as non-root user `app`, ships a `HEALTHCHECK`, and uses an allow-list `.dockerignore` (no docs/tests/dev tools in the image).

- Introduced the `src/services/` layer (`AnalyzerService`, corridor glue, request/response schemas); `src/web/` is now thin routes (`api.py`) + exception mapping (`errors.py`). The broker no longer imports analytics (walls passed as `WallAnchors`, BSM Greeks injected via `GreeksFallback`), and service-level validation errors return `422 INVALID_REQUEST` with `error_code`/`message`/`detail`. Import rules are enforced by `tests/unit/test_layering.py`.

- Split oversized files along responsibility seams: `dashboard.js` (7.4k lines) into 14 native ES modules under `src/web/static/js/dashboard/` (entry `main.js`), `tokens.css` into 8 per-row stylesheets, `src/broker/corridor.py` and `src/analytics/strategies.py` into packages, and `test_contracts_and_pagination.py` into client/contracts/parser/fetcher tests. Behavior unchanged (statement/AST-identical splits; identical rendered DOM, Plotly calls and Firefox screenshot). Static files are served with `Cache-Control: no-cache`; `make js-check` also validates the ES-module import graph.

- `docs/UI_UX_GUIDELINES.md` is now a compact contract (20 kB -> 8 kB: tokens, typography, layout rows, 10 behavioral rules, Plotly styling); the element-by-element inventory moved verbatim to `docs/reference/IMPLEMENTATION_NOTES.md` §5.1.

### Fixed
- Chain table sorted-column, active-filter and focus highlights were invisible: `--accent-focus` (11 uses) was never defined; it is now a theme token (`#1D4ED8` print / `#38BDF8` terminal). The undefined `--border-hairline` header rule now uses `--border-subtle`.
- `docs/UI_UX_GUIDELINES.md` token table listed tokens that do not exist (`--bg-primary`, `--border-hairline`, `--accent-focus`) and stale values/fonts; it now matches `tokens.css`.
- Documentation no longer claims unused tooling (Tailwind, HTMX, `scipy.optimize`) or non-existent identifiers.

## [0.2.1] - 2026-11-11

### Fixed
- Market data types and gateway address are derived from session and env: `FALLBACK_MARKET_DATA_TYPE` override, `MARKET_DATA_MODE` / `DEFAULT_MARKET_DATA_TYPE` reconciliation, observed `Ticker.marketDataType`, and `LIVE 1 -> 2` / `DELAYED 3 -> 4` / `10089`/`354` downgrade parity across fetcher and corridor.
- Dynamic `host:port`, feed labels, and negotiated market data types propagated through `/api/status`, `#app-bootstrap`, and the editorial banner (`• Snap T<N>` attribution).

## [0.2.0] - 2026-11-11

### Added
- Weekly expiration window expanded to `[30, 120]` DTE (one expiry per ISO week, Friday preferred) with exchange-validated strike discovery via per-expiry `reqContractDetailsAsync` pre-fetch and TTL cache.
- Wall Strike Corridor (`src/broker/corridor.py`): `<= 60`-contract targeted quotes around Put/Call Walls, one-shot pull and SSE live streaming with guaranteed `cancelMktData` teardown.
- Strategy Lab engine (`src/analytics/strategies.py`): Bull Put / Bull Call / Diagonal / Iron Condor presets and `CUSTOM` legs, Mid/Natural/slippage/limit ladder, risk metrics, POP, `±1σ` expected move, net Greeks, `T+n` / `ΔIV` payoff curves.
- UI: Corridor and Strategy Lab cards, snap-to-grid 12-column resizable panels, panel visibility bar, sortable/filterable 18-column chain table with `+Leg` buttons, Polish AI-prompt export, resilient alert banner.

### Changed
- Auto strike step selection with grid-aligned canonical steps and dynamic exchange capability detection.

## [0.1.0] - 2026-11-11

### Added
- Phase 1 — Core: Pydantic v2 settings (`config/settings.yaml` + env overrides), domain exceptions, `ReadOnlyIBGuard` (traps `placeOrder`, `cancelOrder`, `reqGlobalCancel`, `exerciseOptions`, `whatIfOrder[Async]`) and `TickerValidator` (`^[A-Z]{1,5}$`).
- Phase 2 — Storage: 27-column `OptionContractRow` / Polars schema, Mode 1 immutable snapshot archive with atomic `latest` cache and self-healing `snapshot_index.json`.
- Phase 3 — Broker: read-only ib-async client, Type 3 -> 4 delayed cascade, filtered chain discovery, `<= 80`-contract paged fetcher with pacing and disconnect recovery.
- Phase 4 — Analytics: vectorized BSM IV/Greeks fallback, OI/Volume Put & Call Walls, `Volume > OI` and Z-score anomalies, multi-DTE profiles.
- Phase 5 — Web: FastAPI endpoints and NYT Print / Obsidian Terminal dashboard with Plotly multi-DTE charts.
- Tooling: `uv`-based cross-platform Makefile (`make setup|run|check|test|docker-up`), Docker image and compose file bound to `127.0.0.1`.
