# Sub-Agent Registry & Token-Optimization Protocol (`.agent/agents/`)

## 1. Purpose
To minimize token usage, prevent context window pollution, and allow smaller/faster models to execute routine tasks, this repository uses **6 specialized sub-agents**. Each sub-agent operates under a strict **Context Diet** (allowed to read only specific files and directories).

---

## 2. Sub-Agent Routing Matrix

| Agent File | Recommended Model Tier | Token Budget | Primary Responsibility | Allowed Context Scope |
| :--- | :--- | :--- | :--- | :--- |
| `qa-git-runner.md` | Fast / Lightweight (Haiku / Flash / Mini) | Ultra-Low | Runs `make check`, `pytest`, `ruff`, `mypy`, security grep, stages files, writes conventional commits, and pushes. | CLI output, `Makefile`, `git status`, `git diff --stat`. Never reads full `src/` or `docs/` unless debugging a test failure line. |
| `doc-sync-auditor.md` | Fast / Lightweight (Haiku / Flash / Mini) | Low | Audits `git diff` against `AGENTS.md` and `docs/*.md` to enforce the Mandatory Documentation-Sync Policy. | `git diff`, `AGENTS.md`, and the specific `docs/*.md` affected by the diff. |
| `broker-storage-eng.md` | High-Reasoning (Sonnet / Pro / GPT-4o) | Medium | Implements Phases 1–3: `src/core/`, `src/storage/`, and `src/broker/` (`ib-async`, Port `4001`, Type 3/4 delayed cascade, paged fetcher, CSV writer). | `docs/ARCHITECTURE.md`, `docs/SECURITY_AND_SAFETY.md`, `docs/DATA_SCHEMA.md`, `config/settings.yaml`. |
| `quant-analytics-eng.md`| High-Reasoning (Sonnet / Pro / o3-mini) | Low-Medium | Implements Phase 4: `src/analytics/` (Polars vectorization, BSM Greeks fallback, Put/Call walls, `Volume > OI` anomalies, Multi-DTE structures). | `docs/DATA_SCHEMA.md`, `docs/FUTURE_IMPROVEMENTS.md`, `src/analytics/`, `tests/unit/test_bsm_and_analytics.py`. |
| `ui-frontend-eng.md` | High-Reasoning (Sonnet / Pro) | Medium | Implements Phase 5: `src/web/` (FastAPI routes, Jinja2 HTML, NYT Print vs. Obsidian Terminal CSS tokens, Plotly.js multi-DTE charts). | `docs/UI_UX_GUIDELINES.md`, `src/web/`. Never loads `src/broker/` or `ib-async` internals. |

---

## 3. Standard Multi-Agent Workflow per Feature Phase
1. **Build:** Invoke the domain specialist (`broker-storage-eng`, `quant-analytics-eng`, or `ui-frontend-eng`) to write the module and unit tests.
2. **Audit Docs:** Invoke `doc-sync-auditor` to synchronize `AGENTS.md` and `docs/*.md` based on `git diff`.
3. **Verify & Commit:** Invoke `qa-git-runner` to run `make check`, verify zero lint/type/test/security errors, and create the git commit.