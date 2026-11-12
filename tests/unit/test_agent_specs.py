"""Tests for `scripts/sync_agents.py` (canonical `.agent/` specs -> Claude/Gemini adapters)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import sync_agents


def test_generated_agent_adapters_are_in_sync() -> None:
    rendered = sync_agents.render_all()
    stale = sync_agents.stale_files(rendered)
    assert stale == [], f"run `make agents-sync`: {stale}"


def test_every_agent_renders_for_both_platforms() -> None:
    agents = sync_agents.load_agents()
    names = {a.name for a in agents}
    assert {"broker-storage-eng", "quant-analytics-eng", "ui-frontend-eng"} <= names
    assert {"doc-sync-auditor", "qa-git-runner", "safety-reviewer"} <= names
    rendered = sync_agents.render_all()
    assert len(rendered) == 2 * len(agents) + 1  # + .gemini/settings.json
    for path, content in rendered.items():
        if path.suffix == ".json":
            continue
        assert content.startswith("---\n"), path
        assert "[HANDOFF" in content, path


def test_read_only_agents_have_no_mutation_tools() -> None:
    by_name = {a.name: a for a in sync_agents.load_agents()}
    for name in ("safety-reviewer", "qa-git-runner"):
        assert not {"edit", "write"} & set(by_name[name].tools), name


def test_unknown_profile_is_rejected(tmp_path: Path) -> None:
    spec = tmp_path / "bad-agent.md"
    spec.write_text(
        "---\nname: bad-agent\ndescription: x\nprofile: nope\ntools: [read]\n"
        "owns: []\nforbidden: []\n---\nbody\n",
        encoding="utf-8",
    )
    agents = [sync_agents.parse_agent(spec)]
    with pytest.raises(sync_agents.AgentSpecError, match="unknown profile"):
        sync_agents.validate(agents, sync_agents.load_models())


def test_name_must_match_filename(tmp_path: Path) -> None:
    spec = tmp_path / "file-name.md"
    spec.write_text(
        "---\nname: other-name\ndescription: x\nprofile: fast-executor\ntools: []\n"
        "owns: []\nforbidden: []\n---\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(sync_agents.AgentSpecError, match="match filename"):
        sync_agents.parse_agent(spec)


def test_gemini_thinking_overrides_follow_profiles() -> None:
    models = sync_agents.load_models()
    agents = sync_agents.load_agents()
    settings = json.loads(sync_agents.render_gemini_settings(agents, models, {"ui": {"x": 1}}))
    assert settings["ui"] == {"x": 1}  # unrelated user settings preserved
    overrides = settings["agents"]["overrides"]
    for agent in agents:
        gem = models["profiles"][agent.profile]["gemini"]
        cfg = overrides[agent.name]["modelConfig"]["generateContentConfig"]["thinkingConfig"]
        assert cfg == gem["thinking_config"]
        if gem["model"] == "gemini-3.8-flash":
            assert cfg == {"thinkingLevel": "HIGH"}
        assert set(cfg) == {"thinkingLevel"}  # all profiles use the Gemini 3.x/4 level style
        assert cfg["thinkingLevel"] in {"LOW", "HIGH"}
