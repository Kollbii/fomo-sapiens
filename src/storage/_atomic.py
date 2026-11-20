"""Shared atomic temp-file write/copy and stat-signature helpers for storage modules."""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
import threading
from pathlib import Path

import polars as pl

StatSig = tuple[int, int, int, int]


def path_stat_sig(path: Path) -> StatSig | None:
    """Return `(mtime_ns, size, ino, ctime_ns)` for a regular file, or `None` if absent."""
    try:
        st = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)


def _discard_tmp(tmp: Path) -> None:
    # Never let cleanup failure mask the original exception (or a successful replace).
    with contextlib.suppress(OSError):
        tmp.unlink(missing_ok=True)


def _tmp_path(path: Path, *, unique_tmp: bool = False) -> Path:
    if unique_tmp:
        return path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    return path.with_name(f".{path.name}.tmp")


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    fsync: bool = False,
    unique_tmp: bool = False,
    ensure_parent: bool = True,
) -> None:
    """Write `data` to a sibling hidden `.tmp` file (optionally fsynced) and `os.replace`."""
    if ensure_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path, unique_tmp=unique_tmp)
    try:
        if fsync:
            with tmp.open("wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        else:
            tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        _discard_tmp(tmp)


def atomic_copy_file(
    src: Path,
    dest: Path,
    *,
    fsync: bool = False,
    ensure_parent: bool = True,
) -> None:
    """Copy `src` to a sibling hidden `.tmp` file (optionally fsynced) and `os.replace`."""
    if ensure_parent:
        dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(dest)
    try:
        shutil.copyfile(src, tmp)
        if fsync:
            with tmp.open("r+b") as fh:
                os.fsync(fh.fileno())
        os.replace(tmp, dest)
    finally:
        _discard_tmp(tmp)


def atomic_write_csv(
    df: pl.DataFrame,
    path: Path,
    *,
    ensure_parent: bool = True,
) -> None:
    """Write a Polars DataFrame to a sibling hidden `.tmp` CSV and `os.replace`."""
    if ensure_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    try:
        df.write_csv(tmp)
        os.replace(tmp, path)
    finally:
        _discard_tmp(tmp)
