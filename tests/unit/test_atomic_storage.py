"""Unit tests for shared atomic file and stat-signature helpers (`src/storage/_atomic.py`)."""

from __future__ import annotations

import os
from pathlib import Path

import polars as pl
import pytest

from src.storage._atomic import (
    atomic_copy_file,
    atomic_write_bytes,
    atomic_write_csv,
    path_stat_sig,
)


def test_path_stat_sig_handles_missing_dirs_and_updates(tmp_path: Path) -> None:
    missing = tmp_path / "missing.txt"
    assert path_stat_sig(missing) is None
    assert path_stat_sig(tmp_path) is None

    file_path = tmp_path / "sample.txt"
    file_path.write_bytes(b"abc")
    sig1 = path_stat_sig(file_path)
    assert sig1 is not None
    assert len(sig1) == 4
    assert sig1[1] == 3

    file_path.write_bytes(b"abcdef")
    sig2 = path_stat_sig(file_path)
    assert sig2 is not None
    assert sig2 != sig1
    assert sig2[1] == 6


def test_atomic_write_bytes_creates_parents_fsyncs_and_cleans_unique_tmp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "nested" / "payload.bin"
    events: list[str] = []
    seen_tmps: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def _fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def _replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        seen_tmps.append(Path(src).name)
        events.append(f"replace:{Path(dst).name}")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "replace", _replace)

    atomic_write_bytes(target, b"hello", fsync=True, unique_tmp=True)
    assert target.read_bytes() == b"hello"
    assert events == ["fsync", "replace:payload.bin"]
    assert seen_tmps and seen_tmps[0].startswith(".payload.bin.") and seen_tmps[0].endswith(".tmp")
    assert sorted(target.parent.iterdir()) == [target]


def test_atomic_write_bytes_preserves_existing_on_failure_and_ignores_unlink_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b'{"ok":true}')

    def _fail_replace(_src: object, _dst: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", _fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_bytes(target, b'{"ok":false}')
    assert target.read_bytes() == b'{"ok":true}'
    assert not list(tmp_path.glob(".*.tmp"))

    def _locked_unlink(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError("tmp locked")

    monkeypatch.setattr(Path, "unlink", _locked_unlink)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_bytes(target, b'{"ok":false}')
    assert target.read_bytes() == b'{"ok":true}'


def test_atomic_copy_file_copies_fsyncs_and_cleans_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src.bin"
    dest = tmp_path / "sub" / "dest.bin"
    src.write_bytes(b"\x01\x02\x03")

    events: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def _fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def _replace(s: str | os.PathLike[str], d: str | os.PathLike[str]) -> None:
        events.append("replace")
        real_replace(s, d)

    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(os, "replace", _replace)

    atomic_copy_file(src, dest, fsync=True)
    assert dest.read_bytes() == b"\x01\x02\x03"
    assert events == ["fsync", "replace"]
    assert not list(dest.parent.glob(".*.tmp"))

    def _boom(_s: object, _d: object) -> None:
        raise OSError("copy replace failed")

    monkeypatch.setattr(os, "replace", _boom)
    src.write_bytes(b"updated")
    with pytest.raises(OSError, match="copy replace failed"):
        atomic_copy_file(src, dest)
    assert dest.read_bytes() == b"\x01\x02\x03"
    assert not list(dest.parent.glob(".*.tmp"))


def test_atomic_write_csv_writes_and_cleans_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "csv" / "rows.csv"
    df = pl.DataFrame({"a": [1, 2], "b": ["x", "y"]})

    atomic_write_csv(df, target)
    assert pl.read_csv(target).to_dicts() == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert not list(target.parent.glob(".*.tmp"))

    def _boom(_s: object, _d: object) -> None:
        raise OSError("csv replace failed")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="csv replace failed"):
        atomic_write_csv(pl.DataFrame({"a": [9], "b": ["z"]}), target)
    assert pl.read_csv(target).to_dicts() == [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    assert not list(target.parent.glob(".*.tmp"))
