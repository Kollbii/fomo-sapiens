"""Repository-wide pytest fixtures.

Reusable *helpers* (coroutine drivers, ib-async fakes) live in `tests/support/` and are imported
explicitly; this module only exposes fixtures that need pytest's lifecycle (tmp dirs, copies).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from src.core.config import StorageSettings
from src.storage.csv_repository import CSVRepository

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    """Read-only directory of committed golden test data."""
    return FIXTURES_DIR


@pytest.fixture
def fixture_repo(tmp_path: Path) -> CSVRepository:
    """`CSVRepository` rooted in `tmp_path`, pre-seeded with the committed golden snapshots.

    Fixtures are copied so tests may rebuild the index or write new snapshots without ever
    mutating files under `tests/fixtures/`.
    """
    repo = CSVRepository(StorageSettings(), root=tmp_path)
    shutil.copytree(FIXTURES_DIR / "snapshots", repo.snapshots_dir)
    repo.ensure_layout()
    repo.rebuild_index()
    return repo
