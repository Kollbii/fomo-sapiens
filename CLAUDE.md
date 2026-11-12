@AGENTS.md

## Claude Code Notes
- Project sub-agents live in `.claude/agents/` and are **generated** from `.agent/agents/` + `.agent/models.yaml`. Edit the canonical spec, then run `make agents-sync`. Never hand-edit `.claude/agents/*.md`.
- Route work per `.agent/agents/README.md`. Delegate domain work to the owning sub-agent; after edits chain `doc-sync-auditor` → `safety-reviewer` (if core/broker/storage/api touched) → `qa-git-runner`.
- Deep-reasoning agents (`broker-storage-eng`, `quant-analytics-eng`, `safety-reviewer`) run on `opus`; when delegating to them, include "think hard" in the task prompt for non-trivial changes.
