from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.core.config import StorageSettings, WatchlistSettings, resolve_project_path
from src.core.exceptions import (
    InvalidRequestError,
    InvalidTickerError,
    StorageUnavailableError,
    WatchlistCapError,
)
from src.core.security import TickerValidator
from src.storage.watchlist_store import (
    CORRUPT_WATCHLIST_NOTICE,
    MAX_WATCHLIST_FILE_BYTES,
    UNREADABLE_WATCHLIST_NOTICE,
    WATCHLIST_CORRUPT_FILENAME,
    WATCHLIST_FILENAME,
    WatchlistFile,
    WatchlistStore,
    resolve_watchlist_path,
)

FIXED_TS = datetime(2026, 11, 14, 15, 30, 45, tzinfo=UTC)
SEED = ("NVDA", "AVGO", "CRDO")
OLD_TS = "2026-01-01T00:00:00Z"


def _store(
    tmp_path: Path,
    *,
    seed: tuple[str, ...] = SEED,
    max_size: int = 5,
    allow_custom: bool = True,
) -> WatchlistStore:
    settings = WatchlistSettings(
        hardcoded_tickers=seed, max_size=max_size, allow_custom_ticker=allow_custom
    )
    return WatchlistStore(tmp_path / "data" / WATCHLIST_FILENAME, settings, clock=lambda: FIXED_TS)


def _write_raw(store: WatchlistStore, text: str) -> None:
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(text, encoding="utf-8")


def _read_json(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _stray_tmp(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if p.name.endswith(".tmp"))


# ---------------------------------------------------------------------- paths & seed
def test_resolve_watchlist_path_relative_and_absolute(tmp_path: Path) -> None:
    rel = resolve_watchlist_path(StorageSettings(), root=tmp_path)
    assert rel == (tmp_path / "data").resolve() / "watchlist.json"
    abs_dir = tmp_path / "elsewhere"
    absolute = resolve_watchlist_path(StorageSettings(data_dir=str(abs_dir)), root=tmp_path)
    assert absolute == abs_dir.resolve() / "watchlist.json"
    default = resolve_watchlist_path(StorageSettings())
    assert default == resolve_project_path("data") / "watchlist.json"
    assert default.is_absolute()


def test_missing_file_serves_seed_without_writing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    state = store.load()
    assert state.tickers == SEED
    assert state.notice is None
    assert state.max_size == 5
    assert state.count == 3
    assert state.is_full is False
    assert not store.path.exists()
    store.ensure_layout()
    assert store.path.parent.is_dir()
    assert not store.path.exists()


def test_default_settings_seed_matches_16_ticker_list(tmp_path: Path) -> None:
    store = WatchlistStore(tmp_path / WATCHLIST_FILENAME, WatchlistSettings())
    state = store.load()
    assert state.tickers == WatchlistSettings().hardcoded_tickers
    assert len(state.tickers) == 16
    assert state.max_size == 20


def test_seed_is_normalized_and_deduped(tmp_path: Path) -> None:
    store = _store(tmp_path, seed=("nvda", " AVGO ", "NVDA"))
    assert store.load().tickers == ("NVDA", "AVGO")


# ---------------------------------------------------------------------- add / order / dedupe
def test_add_appends_persists_and_uses_clock(tmp_path: Path) -> None:
    store = _store(tmp_path)
    update = store.add(" mrvl ")
    assert update.changed is True
    assert update.ticker == "MRVL"
    assert update.state.tickers == (*SEED, "MRVL")
    data = _read_json(store.path)
    assert data == {
        "version": 1,
        "tickers": ["NVDA", "AVGO", "CRDO", "MRVL"],
        "updated_at": "2026-11-14T15:30:45Z",
    }
    assert _stray_tmp(store.path.parent) == []
    # A fresh store instance reads the persisted list back in the same order.
    assert _store(tmp_path).load().tickers == (*SEED, "MRVL")


def test_add_duplicate_is_noop_without_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    update = store.add("avgo")
    assert update.changed is False
    assert update.ticker == "AVGO"
    assert update.state.tickers == SEED
    assert not store.path.exists()


def test_naive_clock_is_treated_as_utc(tmp_path: Path) -> None:
    settings = WatchlistSettings(hardcoded_tickers=SEED, max_size=5)
    store = WatchlistStore(
        tmp_path / WATCHLIST_FILENAME, settings, clock=lambda: datetime(2026, 1, 2, 3, 4, 5)
    )
    store.add("MU")
    assert _read_json(store.path)["updated_at"] == "2026-01-02T03:04:05Z"


def test_loaded_file_is_deduped_in_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_raw(
        store,
        json.dumps({"version": 1, "tickers": ["mu", "NVDA", "MU", "amd"], "updated_at": OLD_TS}),
    )
    state = store.load()
    assert state.tickers == ("MU", "NVDA", "AMD")
    assert state.notice is None


# ---------------------------------------------------------------------- cap
def test_cap_refuses_extra_ticker(tmp_path: Path) -> None:
    store = _store(tmp_path, max_size=4)
    store.add("MU")
    assert store.load().is_full is True
    with pytest.raises(WatchlistCapError, match=r"full \(4/4\)"):
        store.add("AMD")
    assert issubclass(WatchlistCapError, InvalidRequestError)
    assert store.load().tickers == (*SEED, "MU")
    # Duplicate at full capacity is still a no-op, not an error.
    assert store.add("NVDA").changed is False


def test_settings_reject_seed_larger_than_max_size() -> None:
    with pytest.raises(ValueError, match="max_size"):
        WatchlistSettings(hardcoded_tickers=SEED, max_size=2)


# ---------------------------------------------------------------------- validation & guard
@pytest.mark.parametrize("bad", ["../etc", "BRK.B", "", "ABCDEF", "NV DA", "ＮＶＤＡ"])
def test_invalid_ticker_rejected_on_add(tmp_path: Path, bad: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(InvalidTickerError):
        store.add(bad)
    assert not store.path.exists()


@pytest.mark.parametrize("bad", ["../etc", "a/b", "BRK-B"])
def test_invalid_ticker_rejected_on_remove(tmp_path: Path, bad: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(InvalidTickerError):
        store.remove(bad)


def test_allow_custom_false_guard_on_add(tmp_path: Path) -> None:
    store = _store(tmp_path, allow_custom=False)
    with pytest.raises(InvalidTickerError, match="Custom tickers are disabled"):
        store.add("SPY")
    store.remove("AVGO")
    # Re-adding a configured ticker is still allowed and is appended at the end.
    assert store.add("avgo").state.tickers == ("NVDA", "CRDO", "AVGO")


def test_allow_custom_false_guard_on_load(tmp_path: Path) -> None:
    store = _store(tmp_path, allow_custom=False)
    _write_raw(
        store,
        json.dumps({"version": 1, "tickers": ["NVDA", "SPY"], "updated_at": OLD_TS}),
    )
    state = store.load()
    assert state.tickers == SEED
    assert state.notice == CORRUPT_WATCHLIST_NOTICE


def test_injected_validator_is_used(tmp_path: Path) -> None:
    settings = WatchlistSettings(hardcoded_tickers=SEED, max_size=5)
    validator = TickerValidator(SEED, allow_custom=False)
    store = WatchlistStore(tmp_path / WATCHLIST_FILENAME, settings, validator=validator)
    assert store.validator is validator
    with pytest.raises(InvalidTickerError):
        store.add("SPY")


# ---------------------------------------------------------------------- remove / empty
def test_remove_persists_and_absent_is_noop(tmp_path: Path) -> None:
    store = _store(tmp_path)
    update = store.remove("avgo")
    assert update.changed is True
    assert update.state.tickers == ("NVDA", "CRDO")
    assert _read_json(store.path)["tickers"] == ["NVDA", "CRDO"]
    noop = store.remove("AVGO")
    assert noop.changed is False
    assert noop.state.tickers == ("NVDA", "CRDO")


def test_empty_list_persists_and_is_not_reseeded(tmp_path: Path) -> None:
    store = _store(tmp_path)
    for sym in SEED:
        store.remove(sym)
    assert _read_json(store.path)["tickers"] == []
    reloaded = _store(tmp_path).load()
    assert reloaded.tickers == ()
    assert reloaded.notice is None
    assert reloaded.count == 0
    assert store.add("MU").state.tickers == ("MU",)


def test_remove_never_touches_snapshot_csvs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data_dir = store.path.parent
    snap = data_dir / "snapshots" / "AVGO" / "AVGO_20261114_150000.csv"
    latest = data_dir / "latest" / "AVGO_latest.csv"
    for path in (snap, latest):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sentinel", encoding="utf-8")
    store.remove("AVGO")
    assert snap.read_text(encoding="utf-8") == "sentinel"
    assert latest.read_text(encoding="utf-8") == "sentinel"


# ---------------------------------------------------------------------- atomic write
def test_failed_replace_keeps_previous_file_and_cleans_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.add("MU")
    before = store.path.read_bytes()

    def _boom(_src: object, _dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="disk full"):
        store.add("AMD")
    assert store.path.read_bytes() == before
    assert _stray_tmp(store.path.parent) == []


def test_failing_tmp_cleanup_does_not_mask_original_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.add("MU")
    before = store.path.read_bytes()

    def _boom(_src: object, _dst: object) -> None:
        raise OSError("disk full")

    def _locked_unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("tmp locked")

    monkeypatch.setattr(os, "replace", _boom)
    monkeypatch.setattr(Path, "unlink", _locked_unlink)
    with pytest.raises(OSError, match="disk full"):
        store.add("AMD")
    assert store.path.read_bytes() == before


def test_tmp_is_fsynced_before_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(tmp_path)
    _write_raw(store, "{corrupt")
    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def _fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def _replace(src: str, dst: str) -> None:
        events.append(f"replace:{Path(dst).name}")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "replace", _replace)
    store.add("MU")
    # Corrupt backup copy first, then the new file; each tmp is fsynced before its replace.
    assert events == [
        "fsync",
        f"replace:{WATCHLIST_CORRUPT_FILENAME}",
        "fsync",
        f"replace:{WATCHLIST_FILENAME}",
    ]
    assert _stray_tmp(store.path.parent) == []


# ---------------------------------------------------------------------- corrupt file
@pytest.mark.parametrize(
    "raw",
    [
        "{not json",
        "[]",
        json.dumps({"tickers": ["NVDA"]}),
        json.dumps({"version": 2, "tickers": ["NVDA"], "updated_at": "2026-01-01T00:00:00Z"}),
        json.dumps({"version": 1, "tickers": "NVDA", "updated_at": "2026-01-01T00:00:00Z"}),
        json.dumps({"version": 1, "tickers": ["../x"], "updated_at": "2026-01-01T00:00:00Z"}),
        json.dumps({"version": 1, "tickers": [1, 2], "updated_at": "2026-01-01T00:00:00Z"}),
        json.dumps({"version": 1, "tickers": [], "updated_at": "2026-01-01T00:00:00"}),
        json.dumps({"version": 1, "tickers": [], "updated_at": "2026-01-01T00:00:00Z", "extra": 1}),
        json.dumps(
            {
                "version": 1,
                "tickers": ["NVDA", "AVGO", "CRDO", "MU", "AMD", "MRVL"],
                "updated_at": "2026-01-01T00:00:00Z",
            }
        ),
        json.dumps({"version": True, "tickers": [], "updated_at": OLD_TS}),
        json.dumps({"version": 1.0, "tickers": [], "updated_at": OLD_TS}),
        "[" * 100_000,
        "[" * 50_000,
        '{"a":' * 12_000,
        json.dumps({"version": 1, "tickers": ["NVDA"], "updated_at": OLD_TS})
        + " " * MAX_WATCHLIST_FILE_BYTES,
    ],
    ids=[
        "bad-json",
        "not-object",
        "missing-keys",
        "wrong-version",
        "tickers-string",
        "invalid-ticker",
        "non-string",
        "naive-ts",
        "extra-key",
        "over-cap",
        "version-true",
        "version-float",
        "deep-nesting",
        "deep-nesting-under-size-cap",
        "deep-object-nesting",
        "oversize",
    ],
)
def test_corrupt_file_falls_back_to_seed_with_notice(tmp_path: Path, raw: str) -> None:
    store = _store(tmp_path)
    _write_raw(store, raw)
    state = store.load()
    assert state.tickers == SEED
    assert state.notice == CORRUPT_WATCHLIST_NOTICE
    assert state.status == "corrupt"
    assert state.writable is True
    # Reading never modifies or backs up the corrupt file.
    assert store.path.read_text(encoding="utf-8") == raw
    assert not store.corrupt_path.exists()


@pytest.mark.parametrize("raw", ["[" * 50_000, '{"a":' * 12_000], ids=["array", "object"])
def test_deep_nesting_under_size_cap_hits_recursion_error(raw: str) -> None:
    # Guards the premise of the `deep-*-nesting` corrupt cases: they reach `json.loads`.
    assert len(raw) <= MAX_WATCHLIST_FILE_BYTES
    with pytest.raises(RecursionError):
        json.loads(raw)


def test_oversize_corrupt_file_is_backed_up_on_next_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    raw = "[" * (MAX_WATCHLIST_FILE_BYTES + 10)
    _write_raw(store, raw)
    assert store.load().status == "corrupt"
    store.add("MU")
    assert store.corrupt_path.read_text(encoding="utf-8") == raw
    assert _read_json(store.path)["tickers"] == [*SEED, "MU"]


# ---------------------------------------------------------------------- unreadable file (I/O)
def _lock_file_reads(monkeypatch: pytest.MonkeyPatch, locked: Path) -> None:
    real_open = Path.open

    def _open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == locked:
            raise PermissionError(13, "The process cannot access the file", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)


def test_unreadable_file_serves_seed_and_refuses_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    store.add("MU")
    saved = store.path.read_bytes()
    with monkeypatch.context() as mp:
        _lock_file_reads(mp, store.path)
        state = store.load()
        assert state.tickers == SEED
        assert state.notice == UNREADABLE_WATCHLIST_NOTICE
        assert state.notice != CORRUPT_WATCHLIST_NOTICE
        assert "cannot access" not in state.notice
        assert state.status == "unreadable"
        assert state.writable is False
        attempts: list[Callable[[], object]] = [
            lambda: store.add("AMD"),
            lambda: store.add("NVDA"),
            lambda: store.remove("MU"),
            lambda: store.remove("AVGO"),
        ]
        for attempt in attempts:
            with pytest.raises(StorageUnavailableError, match="was not saved"):
                attempt()
        # Invalid input is still rejected first, before any file access.
        with pytest.raises(InvalidTickerError):
            store.add("../x")
    # The user's list was never replaced and no backup was made.
    assert store.path.read_bytes() == saved
    assert not store.corrupt_path.exists()
    assert _stray_tmp(store.path.parent) == []
    recovered = store.load()
    assert recovered.tickers == (*SEED, "MU")
    assert recovered.status == "ok"
    assert recovered.notice is None


def test_directory_at_watchlist_path_is_unreadable_not_corrupt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.mkdir(parents=True)
    state = store.load()
    assert state.status == "unreadable"
    assert state.notice == UNREADABLE_WATCHLIST_NOTICE
    with pytest.raises(StorageUnavailableError):
        store.add("MU")
    assert store.path.is_dir()
    assert not store.corrupt_path.exists()


def test_non_utf8_file_falls_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(b"\xff\xfe\x00garbage")
    assert store.load().notice == CORRUPT_WATCHLIST_NOTICE


def test_next_write_backs_up_corrupt_file_then_heals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    raw = "{corrupt"
    _write_raw(store, raw)
    update = store.add("MU")
    assert update.changed is True
    assert update.state.tickers == (*SEED, "MU")
    assert update.state.notice is None
    assert store.corrupt_path.name == WATCHLIST_CORRUPT_FILENAME
    assert store.corrupt_path.parent == store.path.parent
    assert store.corrupt_path.read_text(encoding="utf-8") == raw
    assert _read_json(store.path)["tickers"] == [*SEED, "MU"]
    assert _stray_tmp(store.path.parent) == []
    healed = store.load()
    assert healed.notice is None
    # A later write on a healthy file does not overwrite the backup.
    store.remove("MU")
    assert store.corrupt_path.read_text(encoding="utf-8") == raw


def test_noop_on_corrupt_file_keeps_notice_and_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_raw(store, "{corrupt")
    update = store.add("NVDA")
    assert update.changed is False
    assert update.state.notice == CORRUPT_WATCHLIST_NOTICE
    assert store.path.read_text(encoding="utf-8") == "{corrupt"
    assert not store.corrupt_path.exists()


def test_remove_on_corrupt_file_backs_up_and_writes_seed_minus_ticker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _write_raw(store, "null")
    update = store.remove("CRDO")
    assert update.state.tickers == ("NVDA", "AVGO")
    assert store.corrupt_path.read_text(encoding="utf-8") == "null"


def test_watchlist_file_model_roundtrip() -> None:
    model = WatchlistFile(tickers=("nvda", "MU"), updated_at="2026-11-14T15:30:45Z")
    assert model.version == 1
    assert model.tickers == ("NVDA", "MU")
    assert model.model_dump(mode="json")["tickers"] == ["NVDA", "MU"]
    for bad_version in (True, 1.0, "1", 2):
        with pytest.raises(ValueError, match="version"):
            WatchlistFile.model_validate(
                {"version": bad_version, "tickers": [], "updated_at": OLD_TS}
            )
