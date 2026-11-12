"""Tests for `scripts/security_guard.py` (static read-only / anti-pattern gate)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import security_guard


def test_security_guard_passes_on_repo_src() -> None:
    assert security_guard.scan() == []


@pytest.mark.parametrize(
    "line",
    [
        "ib.placeOrder(contract, order)",
        "await ib.whatIfOrderAsync(c, o)",
        "ib.reqPositions()",
        "ib.reqAccountSummary()",
        "time.sleep(1)",
        "import pandas as pd",
    ],
)
def test_security_guard_flags_forbidden_patterns(tmp_path: Path, line: str) -> None:
    (tmp_path / "mod.py").write_text(f"{line}\n", encoding="utf-8")
    hits = security_guard.scan(tmp_path)
    assert len(hits) == 1, hits
