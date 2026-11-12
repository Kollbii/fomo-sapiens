"""Enforce the layer dependency rules of docs/CODING_STANDARDS.md §1 via AST import scanning.

core <- storage <- broker
core, storage(schema) <- analytics
services -> {core, storage, broker, analytics}
web -> {core, services}
main.py (composition root) -> anything
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"

ALLOWED: dict[str, frozenset[str]] = {
    "core": frozenset({"core"}),
    "storage": frozenset({"core", "storage"}),
    "broker": frozenset({"core", "storage", "broker"}),
    "analytics": frozenset({"core", "storage", "analytics"}),
    "services": frozenset({"core", "storage", "broker", "analytics", "services"}),
    "web": frozenset({"core", "services", "web"}),
}
THIRD_PARTY_BANS: dict[str, frozenset[str]] = {
    "core": frozenset({"ib_async", "fastapi", "polars"}),
    "storage": frozenset({"ib_async", "fastapi"}),
    "broker": frozenset({"fastapi"}),
    "analytics": frozenset({"ib_async", "fastapi"}),
    "services": frozenset({"fastapi", "starlette"}),
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _layer_files() -> list[tuple[str, Path]]:
    return [(layer, p) for layer in ALLOWED for p in sorted((SRC / layer).rglob("*.py"))]


@pytest.mark.parametrize(
    ("layer", "path"), _layer_files(), ids=lambda v: v if isinstance(v, str) else v.name
)
def test_layer_imports_respect_dependency_rules(layer: str, path: Path) -> None:
    violations: list[str] = []
    for name in _imports(path):
        parts = name.split(".")
        if parts[0] == "src" and len(parts) > 1 and parts[1] not in ALLOWED[layer]:
            violations.append(name)
        if parts[0] in THIRD_PARTY_BANS.get(layer, frozenset()):
            violations.append(name)
    assert violations == [], f"{path.relative_to(SRC)} ({layer}) imports {sorted(violations)}"
