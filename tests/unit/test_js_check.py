"""Tests for the zero-dependency ES-module graph check in `scripts/js_check.py`."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import js_check


def test_repo_frontend_module_graph_resolves() -> None:
    assert js_check.check_module_graph(js_check.js_files()) == []


def test_module_graph_flags_missing_export_and_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(js_check, "ROOT", tmp_path)
    (tmp_path / "a.js").write_text(
        'import { present, absent } from "./b.js";\nimport { x } from "./missing.js";\n',
        encoding="utf-8",
    )
    (tmp_path / "b.js").write_text("export function present() {}\n", encoding="utf-8")
    errors = js_check.check_module_graph([tmp_path / "a.js", tmp_path / "b.js"])
    assert any("does not export 'absent'" in e for e in errors)
    assert any("import target not found: ./missing.js" in e for e in errors)
    assert len(errors) == 2
