"""Guard against documentation drift for identifiers that can be checked mechanically.

Docs and agent specs are read by every coding agent; a stale element id or route silently
misdirects them. These tests keep the cheap-to-verify references honest.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.main import create_app

ROOT = Path(__file__).resolve().parents[2]
DOC_FILES = sorted(
    [ROOT / "AGENTS.md", *(ROOT / "docs").rglob("*.md"), *(ROOT / ".agent" / "agents").glob("*.md")]
)
WEB_DIR = ROOT / "src" / "web"
ELEMENT_ID_RE = re.compile(r"`#([a-z][a-z0-9-]+)`")
ROUTE_RE = re.compile(r"\b(GET|POST) (/api/[A-Za-z0-9_/{}-]+)")


def _web_corpus() -> str:
    return "\n".join(
        p.read_text(encoding="utf-8")
        for p in WEB_DIR.rglob("*")
        if p.suffix in {".html", ".js", ".css"} and "vendor" not in p.parts
    )


@pytest.mark.parametrize("doc", DOC_FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_documented_element_ids_exist_in_web_sources(doc: Path) -> None:
    corpus = _web_corpus()
    ids = set(ELEMENT_ID_RE.findall(doc.read_text(encoding="utf-8")))
    missing = sorted(i for i in ids if i not in corpus)
    assert missing == [], f"{doc.name} references unknown element ids: {missing}"


def _route_pattern(path: str) -> re.Pattern[str]:
    """`/api/chain/{ticker}` -> regex matching both the template and concrete examples."""
    parts = re.split(r"(\{[^}]+\})", path)
    body = "".join(r"[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
    return re.compile(f"^{body}$")


def test_documented_api_routes_exist() -> None:
    schema = create_app().openapi()
    served = [
        (method.upper(), _route_pattern(path))
        for path, ops in schema["paths"].items()
        for method in ops
    ]
    documented = {
        (method, path)
        for doc in DOC_FILES
        for method, path in ROUTE_RE.findall(doc.read_text(encoding="utf-8"))
    }
    assert documented, "no documented routes found - regex drifted?"
    missing = sorted(
        (method, path)
        for method, path in documented
        if not any(m == method and pat.match(path) for m, pat in served)
    )
    assert missing == [], f"documented routes not served by the app: {missing}"


def test_documented_design_tokens_are_defined_in_css() -> None:
    css = "\n".join(
        p.read_text(encoding="utf-8") for p in (WEB_DIR / "static" / "css").glob("*.css")
    )
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
    guide = (ROOT / "docs" / "UI_UX_GUIDELINES.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`(--[a-z0-9-]+)`", guide))
    assert documented, "no tokens found in UI_UX_GUIDELINES.md - regex drifted?"
    assert sorted(documented - defined) == []
