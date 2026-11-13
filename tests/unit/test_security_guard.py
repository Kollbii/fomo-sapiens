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
        "await ib.reqPositionsAsync()",
        "ib.reqExecutions()",
        "await ib.reqExecutionsAsync(filt)",
        "ib.reqAccountSummary()",
        "ib.reqAccountUpdates(True, 'U123')",
        "ib.reqPnL(1, 'U123', '')",
        "ib.managedAccounts()",
        "ib.reqOpenOrders()",
        "ib.reqAllOpenOrders()",
        "time.sleep(1)",
        "import pandas as pd",
    ],
)
def test_security_guard_flags_forbidden_patterns(tmp_path: Path, line: str) -> None:
    (tmp_path / "mod.py").write_text(f"{line}\n", encoding="utf-8")
    hits = security_guard.scan(tmp_path)
    assert len(hits) == 1, hits


def test_security_guard_scoped_allowlist_only_for_ledger_sync(tmp_path: Path) -> None:
    broker_dir = tmp_path / "src" / "broker"
    services_dir = tmp_path / "src" / "services"
    broker_dir.mkdir(parents=True)
    services_dir.mkdir(parents=True)

    # Allowed inside src/broker/ledger_sync.py
    ledger_sync = broker_dir / "ledger_sync.py"
    ledger_sync.write_text(
        "await broker.ib.reqExecutionsAsync(filt)\nawait broker.ib.reqPositionsAsync()\n",
        encoding="utf-8",
    )
    assert security_guard.scan(tmp_path / "src", root=tmp_path) == []

    # Same calls MUST be flagged in every other src/ module
    other_file = services_dir / "ledger.py"
    other_file.write_text(
        "await ib.reqExecutionsAsync(filt)\nawait ib.reqPositionsAsync()\n",
        encoding="utf-8",
    )
    hits_other = security_guard.scan(tmp_path / "src", root=tmp_path)
    assert len(hits_other) == 2

    # Even inside ledger_sync.py, sync variants, order APIs, account APIs, sleep, pandas fail
    other_file.unlink()
    ledger_sync.write_text(
        "await broker.ib.reqPositionsAsync(); broker.ib.placeOrder(c, o)\n"
        "broker.ib.reqPositions()\n"
        "broker.ib.reqExecutions()\n"
        "broker.ib.reqAccountSummary()\n"
        "time.sleep(0.5)\n"
        "import pandas\n",
        encoding="utf-8",
    )
    hits_sync = security_guard.scan(tmp_path / "src", root=tmp_path)
    assert len(hits_sync) == 6
