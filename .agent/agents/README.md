# Sub-Agent Registry, Model Routing & Multi-Platform Protocol (`.agent/`)

## 1. Layout (single source of truth → generated adapters)

    .agent/
    ├── models.yaml                 # model/reasoning profiles + abstract tool map (Claude + Gemini)
    └── agents/
        ├── README.md               # this file (routing matrix)
        ├── _shared/protocol.md     # hard invariants + HANDOFF format, appended to every agent
        └── <agent>.md              # canonical, platform-neutral spec
    .claude/agents/<agent>.md       # GENERATED for Claude Code   (do not edit)
    .gemini/agents/<agent>.md       # GENERATED for Gemini CLI    (do not edit)
    .gemini/settings.json           # GENERATED agents.overrides (thinkingConfig); other keys preserved
    CLAUDE.md / GEMINI.md           # platform entry files, both import AGENTS.md

Edit `.agent/` only, then `make agents-sync`. `make check` runs `make agents-check` and fails if adapters are stale. Agents without native sub-agent support (pi, Codex, Cursor, etc.) can read `.agent/agents/<agent>.md` directly as a role prompt.

Canonical frontmatter keys: `name` (= filename), `description` (routing trigger, starts with "Use …"), `profile` (key in `models.yaml`), `tools` (`read|edit|write|bash|grep|glob`), `owns`, `reads`, `forbidden`, optional `color`.

---

## 2. Model & Reasoning Profiles (`models.yaml`)

| Profile | Claude Code | Gemini CLI | Reasoning | Why |
| :--- | :--- | :--- | :--- | :--- |
| `deep-reasoning` | `opus` | `gemini-4-argon` (`thinkingLevel: HIGH`, T=1.0; alt: `gemini-4-hydrogen` `XHIGH`) | high | Silent-failure domains: async socket lifecycle & pacing, read-only guard, numerical finance, adversarial review. A wrong answer costs more than the extra tokens. |
| `balanced-coding` | `sonnet` | `gemini-4-hydrogen` (`thinkingLevel: MEDIUM`, T=1.0) | medium | Large surface, moderate depth (FastAPI + `js/dashboard/*.js` ES modules + per-row CSS). Hydrogen at `MEDIUM` gives strong multi-module code-gen without burning Argon quota. |
| `precise-writer` | `sonnet` | `gemini-4-hydrogen` (`thinkingLevel: MEDIUM`, T=1.0) | low (Claude) / medium (Gemini) | Diff → docs. Invented identifiers poison every later agent's context; Hydrogen at `MEDIUM` enforces tight citation fidelity. |
| `fast-executor` | `haiku` | `gemini-4-hydrogen` (`thinkingLevel: LOW`, T=1.0) | minimal (Claude) / low (Gemini) | Deterministic command runner; escalates instead of reasoning. Hydrogen on `LOW` minimizes latency and token cost. |

Gemini model ids live once at the top of `models.yaml` (`gemini_models.argon`, `gemini_models.hydrogen`, legacy `gemini_models.flash`); upgrading = change one line + `make agents-sync`.

Gemini reasoning effort is applied through `.gemini/settings.json` → `agents.overrides.<agent>.modelConfig.generateContentConfig.thinkingConfig` (generated and merged; other user settings are preserved), because Gemini agent frontmatter is schema-strict. Gemini 4 accepts `thinkingLevel: LOW | MEDIUM | HIGH` (plus `XHIGH` on `gemini-4-hydrogen`; if an older Gemini CLI schema rejects `MEDIUM`, fall back to `HIGH` on `balanced-coding`/`precise-writer`). All Gemini profiles use temperature `1.0`, as Google recommends for Gemini 3.x+/4. Claude's reasoning effort is set by its model tier plus the prompt guidance.

---

## 3. Routing Matrix

| Agent | Profile | Tools | Use when the change touches… |
| :--- | :--- | :--- | :--- |
| `broker-storage-eng` | deep-reasoning | rw + bash | `src/core/` (except `security.py`), `src/storage/`, `src/broker/`, `src/services/` (orchestration, corridor lifecycle), `config/settings.yaml`, `.env.example` |
| `quant-analytics-eng` | deep-reasoning | rw + bash | `src/analytics/` (BSM, walls, anomalies, term structure, Strategy Lab, roadmap GEX/diff/skew/max-pain) |
| `ui-frontend-eng` | balanced-coding | rw + bash | `src/main.py`, `src/web/` (thin routes, `errors.py`, SSE, templates, `css/*.css`, `js/dashboard/*.js`) |
| `doc-sync-auditor` | precise-writer | edit + bash | after any code change: `AGENTS.md`, `docs/*.md`, `CHANGELOG.md`, `README.md`, `.agent/agents/*.md` |
| `safety-reviewer` | deep-reasoning | **read-only** + bash | review gate for diffs in `src/core/`, `src/services/`, `src/broker/`, `src/storage/`, `src/web/api.py`, `src/web/errors.py`, `scripts/security_guard.py` |
| `qa-git-runner` | fast-executor | read + bash | `make check`, commit, tag, push (on request) |

Cross-layer features (e.g. GEX: analytics + service + API + chart) are split by layer: `quant-analytics-eng` first (defines the Pydantic report), `broker-storage-eng` wires it into `src/services/`, then `ui-frontend-eng` (route + chart), each with its own HANDOFF.

`src/core/security.py` has no owner: changes require an explicit user request, `broker-storage-eng` to implement, and `safety-reviewer` APPROVE.

---

## 4. Standard Workflow

    1. Build   -> domain agent (one per layer, in dependency order: core/storage/broker -> analytics -> web)
    2. Docs    -> doc-sync-auditor        (uses HANDOFF.docs_impact + git diff)
    3. Review  -> safety-reviewer         (mandatory if core/broker/storage/services/web/{api,errors}.py changed; optional otherwise)
    4. Verify  -> qa-git-runner           (make check, commit; push only on explicit request)

Every agent ends with the `[HANDOFF …]` block from `_shared/protocol.md`; the orchestrator passes only that block (not the full transcript) to the next agent.

Escalation: `fast-executor` / `precise-writer` agents STOP and hand off rather than reason through logic failures; a `REQUEST_CHANGES` from `safety-reviewer` routes back to the domain agent named in its findings.
