"""Offline end-to-end smoke test over the committed golden snapshot CSV.

Guards backward compatibility of on-disk Mode 1 archives: if the schema or readers change in a
way that can no longer load an existing snapshot, this test fails before users lose history.
"""

from __future__ import annotations

import httpx

from src.broker.client import IBKRBrokerClient
from src.core.config import Settings, StorageSettings
from src.main import create_app
from src.storage.csv_repository import CSVRepository
from src.storage.schema import COLUMNS
from tests.support.aio import run_coro
from tests.support.ib_fakes import FakeEvent

GOLDEN_SNAPSHOT_ID = "NVDA_20261111_155004"


class _OfflineIB:
    """Gateway that is always closed: every connect attempt is refused."""

    def __init__(self) -> None:
        self.errorEvent = FakeEvent()
        self.client = self

    def isConnected(self) -> bool:  # noqa: N802 - ib_async API name
        return False

    async def connectAsync(self, *_args: object, **_kwargs: object) -> None:  # noqa: N802
        raise ConnectionRefusedError("gateway closed")

    def disconnect(self) -> None:
        return None


def test_golden_snapshot_loads_with_current_schema(fixture_repo: CSVRepository) -> None:
    metas = fixture_repo.list_snapshots("NVDA")
    assert [m.snapshot_id for m in metas] == [GOLDEN_SNAPSHOT_ID]

    df = fixture_repo.load_snapshot("NVDA", GOLDEN_SNAPSHOT_ID)
    assert tuple(df.columns) == COLUMNS
    assert df.height == 32
    assert df["open_interest"].null_count() == df.height  # Type 3 feed without OI


def test_offline_ui_and_chain_render_from_golden_snapshot(fixture_repo: CSVRepository) -> None:
    settings = Settings(storage=StorageSettings())
    client = IBKRBrokerClient(settings, ib_factory=_OfflineIB)
    app = create_app(settings=settings, repo=fixture_repo, broker_client=client)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                index = await http.get("/")
                assert index.status_code == 200
                assert "editorial-banner-slot" in index.text
                assert 'id="backtest-card"' in index.text
                assert 'id="trade-ledger-card"' in index.text
                assert 'id="snapshot-picker"' in index.text

                chain = await http.get("/api/chain/NVDA")
                assert chain.status_code == 200
                body = chain.json()
                assert body["meta"]["snapshot_id"] == GOLDEN_SNAPSHOT_ID
                assert body["walls"]["primary_basis"] == "VOLUME"  # OI is null

    run_coro(_exercise())
