@./AGENTS.md

## Gemini CLI Notes
- Project sub-agents live in `.gemini/agents/` and are **generated** from `.agent/agents/` + `.agent/models.yaml`. Edit the canonical spec, then run `make agents-sync`. Never hand-edit `.gemini/agents/*.md`.
- Sub-agents are on by default (Gemini CLI >= 0.68, `experimental.enableAgents` defaults to `true`). Per-agent reasoning effort is set in the generated `agents.overrides` block of `.gemini/settings.json`; edit `.agent/models.yaml`, never the JSON block by hand. If your CLI version renamed a tool, fix the `tools:` map in `.agent/models.yaml` and re-sync.
- Route work per `.agent/agents/README.md`. After edits chain `doc-sync-auditor` → `safety-reviewer` (if core/broker/storage/api touched) → `qa-git-runner`.
