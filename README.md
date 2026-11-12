# fomo-sapiens — IBKR Options Analyzer & Dashboard

A local, single-user, **strictly read-only** options analytics dashboard for Interactive Brokers.
It pulls a filtered option chain from IBKR Gateway / TWS, archives every pull as an immutable CSV
snapshot, fills missing Greeks with a vectorized Black-Scholes-Merton fallback, and renders Put/Call
Walls, unusual activity, multi-DTE IV/Greeks profiles, a Wall Strike Corridor, and a multi-leg
Strategy Lab in a dual-theme (NYT Print / Obsidian Terminal) web UI.

> **Safety:** the app never places, modifies, or cancels orders and never queries account data.
> Order APIs are trapped at runtime (`ReadOnlyIBGuard`) and banned statically (`make security-guard`).
> Still enable **Read-Only API** in Gateway (see below).

## Features
- Weekly expirations in a `30–120` DTE window, `±10–50%` moneyness, `Auto` / `$1–$25` strike step.
- IBKR `DELAYED` (Type 3 → 4) by default or `LIVE` OPRA (Type 1 → 2, auto-downgrade on `10089`/`354`).
- Paged fetching (`<= 80` contracts/page, `<= 400` per refresh) that respects IBKR pacing limits.
- Offline-first: the UI works from cached snapshots while Gateway is closed.
- Put/Call Walls (OI with Volume fallback), `Volume > OI` and Z-score anomalies, IV smile and Greeks overlays.
- Wall Corridor: `<= 60` targeted live quotes around the walls (one-shot or SSE stream).
- Strategy Lab: spreads, iron condors, diagonals, custom legs; risk, POP, expected move, `T+n` payoff curves.

## Quickstart

Requirements: Python 3.11+, [`uv`](https://docs.astral.sh/uv/), IBKR Gateway or TWS (optional for offline use). Node.js is optional (`make js-check`).

    make setup     # create .venv from uv.lock (runtime + dev) and .env from .env.example
    make run       # http://127.0.0.1:8000  ->  IBKR at 127.0.0.1:4001

Docker (Windows / macOS / Linux):

    make docker-up     # http://127.0.0.1:8000  ->  IBKR at host.docker.internal:4001
    make docker-down

The image is built from `uv.lock` (runtime dependencies only), runs as a non-root user, and keeps
snapshots in the bind-mounted `./data` directory.

### IBKR Gateway settings
**Configure → Settings → API → Settings:** enable *Read-Only API*, set *Socket Port* to `IBKR_PORT`
(default `4001`), and keep client id `77` free. For Docker, also add the Docker bridge IP to
*Trusted IPs* (details in `docs/SECURITY_AND_SAFETY.md` §3).

### Configuration
Defaults live in `config/settings.yaml`; override with `.env` / environment variables:
`IBKR_HOST`, `IBKR_PORT`, `IBKR_CLIENT_ID`, `MARKET_DATA_MODE` (`DELAYED` | `LIVE`),
`DEFAULT_MARKET_DATA_TYPE`, `FALLBACK_MARKET_DATA_TYPE`, `RISK_FREE_RATE`, `LOG_LEVEL`,
`APP_HOST`, `APP_PORT`.

Snapshots are written to `data/snapshots/{TICKER}/`, the latest cache to `data/latest/`, and the index
to `data/metadata/snapshot_index.json` (created automatically on startup).

## Development

    make check      # ruff lint+format, mypy --strict, security guard, JS syntax, agent-spec sync, pytest
    make test       # pytest only
    make coverage   # pytest with branch coverage report

Project layout:

    src/core/       config, exceptions, read-only guard, ticker validation
    src/storage/    CSV schema + Mode 1 archive repository
    src/broker/     ib-async client, chain discovery, paged fetcher, Wall Corridor
    src/analytics/  BSM fallback, walls, anomalies, multi-DTE profiles, Strategy Lab
    src/web/        FastAPI routes, Jinja2 templates, CSS tokens, dashboard JS
    tests/          unit/, integration/, support/ helpers, fixtures/ golden snapshots
    scripts/        security guard, JS check, agent-spec generator
    .agent/         AI coding-agent specs (Claude Code / Gemini CLI adapters are generated)

## Documentation
| Document | Contents |
| :--- | :--- |
| `AGENTS.md` | Operating manual for AI coding agents (stack, policies, module map, anti-patterns) |
| `docs/ARCHITECTURE.md` | System architecture and component contracts |
| `docs/DATA_SCHEMA.md` | CSV schema, sanitization, snapshot index |
| `docs/SECURITY_AND_SAFETY.md` | Read-only enforcement, input validation, network binding |
| `docs/UI_UX_GUIDELINES.md` | Themes, typography, layout, Plotly styling |
| `docs/CODING_STANDARDS.md` | Layering, typing, logging, testing protocol |
| `docs/FUTURE_IMPROVEMENTS.md` | Roadmap |
| `docs/reference/IMPLEMENTATION_NOTES.md` | Per-module implementation details |
| `CHANGELOG.md` | Release history |
