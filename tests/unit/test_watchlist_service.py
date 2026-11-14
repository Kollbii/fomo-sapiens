"""WatchlistService: archive freshness (ET day), add / remove / cap / unreadable propagation."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.core.config import WatchlistSettings
from src.core.exceptions import (
    InvalidTickerError,
    StorageSchemaError,
    StorageUnavailableError,
    WatchlistCapError,
)
from src.core.security import TickerValidator
from src.services.watchlist import FRESHNESS_UNAVAILABLE_NOTICE, WatchlistService
from src.storage.csv_repository import CSVRepository
from src.storage.watchlist_store import (
    CORRUPT_WATCHLIST_NOTICE,
    UNREADABLE_WATCHLIST_NOTICE,
    WatchlistStore,
    resolve_watchlist_path,
)
from tests.support.aio import run_coro

_FIXTURE_ID = "NVDA_20261111_155004"
_FIXTURE_TS = "2026-11-11T15:50:04Z"  # 10:50 ET on 2026-11-11


async def _inline(func: Callable[[], Any]) -> Any:
    return func()


def _service(
    repo: CSVRepository,
    *,
    now: datetime,
    settings: WatchlistSettings | None = None,
) -> WatchlistService:
    wl = settings or WatchlistSettings(hardcoded_tickers=("NVDA", "AVGO"), max_size=3)
    store = WatchlistStore(
        resolve_watchlist_path(repo.storage, root=repo.root), wl, clock=lambda: now
    )
    return WatchlistService(
        store,
        repo,
        clock=lambda: now,
        runner=_inline,
    )


@pytest.mark.parametrize(
    ("now", "today_et", "is_today"),
    [
        (datetime(2026, 11, 11, 20, 0, tzinfo=UTC), "2026-11-11", True),
        # 22:00 ET on Nov 11 is already Nov 12 in UTC but still the same ET day.
        (datetime(2026, 11, 12, 3, 0, tzinfo=UTC), "2026-11-11", True),
        (datetime(2026, 11, 12, 14, 0, tzinfo=UTC), "2026-11-12", False),
        (datetime(2026, 11, 11, 4, 0), "2026-11-10", False),  # naive -> UTC; 23:00 ET Nov 10
    ],
)
def test_freshness_uses_et_calendar_day(
    fixture_repo: CSVRepository, now: datetime, today_et: str, is_today: bool
) -> None:
    svc = _service(fixture_repo, now=now)
    resp = run_coro(svc.get_watchlist())
    assert resp.today_et == today_et
    assert resp.tickers == ["NVDA", "AVGO"]
    nvda, avgo = resp.entries
    assert nvda.ticker == "NVDA"
    assert nvda.latest_snapshot_id == _FIXTURE_ID
    assert nvda.latest_snapshot_utc == _FIXTURE_TS
    assert nvda.is_today_et is is_today
    assert avgo.latest_snapshot_id is None
    assert avgo.latest_snapshot_utc is None
    assert avgo.is_today_et is False
    assert (resp.count, resp.max_size, resp.is_full) == (2, 3, False)
    assert resp.allow_custom is True
    assert resp.writable is True
    assert resp.notice is None
    assert (resp.ticker, resp.changed) == (None, False)


def test_add_remove_cap_and_snapshots_untouched(fixture_repo: CSVRepository) -> None:
    svc = _service(fixture_repo, now=datetime(2026, 11, 11, 20, 0, tzinfo=UTC))
    added = run_coro(svc.add_ticker(" crdo "))
    assert (added.ticker, added.changed) == ("CRDO", True)
    assert added.tickers == ["NVDA", "AVGO", "CRDO"]
    assert added.is_full is True

    dup = run_coro(svc.add_ticker("nvda"))
    assert (dup.ticker, dup.changed) == ("NVDA", False)
    assert dup.tickers == ["NVDA", "AVGO", "CRDO"]

    with pytest.raises(WatchlistCapError, match=r"full \(3/3\)"):
        run_coro(svc.add_ticker("PLTR"))
    with pytest.raises(InvalidTickerError):
        run_coro(svc.add_ticker("../etc"))

    removed = run_coro(svc.remove_ticker("NVDA"))
    assert (removed.ticker, removed.changed) == ("NVDA", True)
    assert removed.tickers == ["AVGO", "CRDO"]
    assert [e.ticker for e in removed.entries] == ["AVGO", "CRDO"]
    absent = run_coro(svc.remove_ticker("NVDA"))
    assert absent.changed is False
    with pytest.raises(InvalidTickerError):
        run_coro(svc.remove_ticker("bad ticker"))

    # Removing a ticker never touches its archive.
    assert [m.snapshot_id for m in fixture_repo.list_snapshots("NVDA")] == [_FIXTURE_ID]
    assert fixture_repo.snapshot_path("NVDA", _FIXTURE_ID).is_file()
    assert svc.cached_tickers() == ["AVGO", "CRDO"]
    on_disk = json.loads(svc.store.path.read_text(encoding="utf-8"))
    assert on_disk["tickers"] == ["AVGO", "CRDO"]


def test_empty_list_is_valid(tmp_path: Path) -> None:
    repo = CSVRepository(root=tmp_path)
    svc = _service(repo, now=datetime(2026, 11, 11, 20, 0, tzinfo=UTC))
    run_coro(svc.remove_ticker("NVDA"))
    resp = run_coro(svc.remove_ticker("AVGO"))
    assert resp.tickers == [] and resp.entries == [] and resp.count == 0
    assert run_coro(svc.get_watchlist()).tickers == []


def test_custom_guard_blocks_unlisted_add(tmp_path: Path) -> None:
    repo = CSVRepository(root=tmp_path)
    wl = WatchlistSettings(hardcoded_tickers=("NVDA",), allow_custom_ticker=False, max_size=5)
    svc = _service(repo, now=datetime(2026, 11, 11, 20, 0, tzinfo=UTC), settings=wl)
    assert run_coro(svc.get_watchlist()).allow_custom is False
    with pytest.raises(InvalidTickerError, match="Custom tickers are disabled"):
        run_coro(svc.add_ticker("AAPL"))


def test_allow_custom_follows_store_validator_not_settings(tmp_path: Path) -> None:
    """`allow_custom` is derived from the store's validator, so it cannot drift from the guard."""
    repo = CSVRepository(root=tmp_path)
    now = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)
    wl = WatchlistSettings(hardcoded_tickers=("NVDA",), allow_custom_ticker=True, max_size=5)
    store = WatchlistStore(
        resolve_watchlist_path(repo.storage, root=repo.root),
        wl,
        validator=TickerValidator(("NVDA",), allow_custom=False),
        clock=lambda: now,
    )
    svc = WatchlistService(store, repo, clock=lambda: now, runner=_inline)
    assert svc.allow_custom is False
    assert run_coro(svc.get_watchlist()).allow_custom is False
    with pytest.raises(InvalidTickerError, match="Custom tickers are disabled"):
        run_coro(svc.add_ticker("AAPL"))


def test_unreadable_file_serves_seed_and_refuses_writes(tmp_path: Path) -> None:
    repo = CSVRepository(root=tmp_path)
    svc = _service(repo, now=datetime(2026, 11, 11, 20, 0, tzinfo=UTC))
    svc.store.path.mkdir(parents=True)  # opening a directory -> OSError -> "unreadable"
    resp = run_coro(svc.get_watchlist())
    assert resp.tickers == ["NVDA", "AVGO"]
    assert resp.writable is False
    assert resp.notice == UNREADABLE_WATCHLIST_NOTICE
    with pytest.raises(StorageUnavailableError):
        run_coro(svc.add_ticker("CRDO"))
    with pytest.raises(StorageUnavailableError):
        run_coro(svc.remove_ticker("NVDA"))


def test_corrupt_file_notice_and_freshness_failure_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = CSVRepository(root=tmp_path)
    svc = _service(repo, now=datetime(2026, 11, 11, 20, 0, tzinfo=UTC))
    svc.ensure_layout()
    svc.store.path.write_text("{not json", encoding="utf-8")

    def _broken(_tickers: Any, **_kw: Any) -> Any:
        raise StorageSchemaError("snapshot index corrupt: secret detail")

    monkeypatch.setattr(repo, "latest_snapshots", _broken)
    resp = run_coro(svc.get_watchlist())
    assert resp.writable is True
    assert resp.notice == f"{CORRUPT_WATCHLIST_NOTICE} {FRESHNESS_UNAVAILABLE_NOTICE}"
    assert "secret detail" not in resp.model_dump_json()
    assert all(e.latest_snapshot_id is None and not e.is_today_et for e in resp.entries)


def test_latest_snapshots_single_index_read(fixture_repo: CSVRepository) -> None:
    calls = 0
    original = fixture_repo.load_index

    def _counting() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return original()

    fixture_repo.load_index = _counting  # type: ignore[method-assign]
    latest = fixture_repo.latest_snapshots(["NVDA", "AVGO", "CRDO"])
    assert calls == 1
    assert list(latest) == ["NVDA"]
    assert latest["NVDA"].snapshot_id == _FIXTURE_ID
    with pytest.raises(InvalidTickerError):
        fixture_repo.latest_snapshots(["../x"])
