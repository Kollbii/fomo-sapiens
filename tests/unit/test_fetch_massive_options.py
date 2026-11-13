"""Unit tests for multi-key scheduling, sharding, and caching in `fetch_massive_options.py`."""

from __future__ import annotations

import json
import os
import sys
import threading
from email.message import Message
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from scripts import fetch_massive_options as fmo


def test_resolve_api_keys_deduplicates_and_prioritizes_cli() -> None:
    env = {
        "MASSIVE_API_KEYS": "env1, env2 ; env1",
        "MASSIVE_API_KEY": "legacy",
    }
    assert fmo._resolve_api_keys(None, None, env) == ["env1", "env2"]
    assert fmo._resolve_api_keys(None, None, {"MASSIVE_API_KEY": "single"}) == ["single"]
    assert fmo._resolve_api_keys(None, None, {"POLYGON_API_KEY": "poly"}) == ["poly"]

    cli_keys = fmo._resolve_api_keys(
        ["k1,k2", "k3"],
        "k2;k4 k1",
        env,
    )
    assert cli_keys == ["k1", "k2", "k3", "k4"]
    assert fmo._mask_key("abcd1234") == "...1234"
    assert fmo._mask_key("1234") == "***"


def test_key_pool_balances_across_2_3_4_keys_and_enforces_pacing() -> None:
    now = [100.0]
    pool = fmo.KeyPool(["key-1", "key-2", "key-3", "key-4"], clock=lambda: now[0])

    # All 4 keys can be leased concurrently at t=100.0
    leased = [pool.acquire() for _ in range(4)]
    assert [slot.api_key for slot in leased] == ["key-1", "key-2", "key-3", "key-4"]

    # Release key-1..key-4 with staggered delays; earliest ready slot is picked next
    pool.release(leased[0], delay_sec=12.5)
    pool.release(leased[1], delay_sec=5.0)
    pool.release(leased[2], delay_sec=12.5)
    pool.release(leased[3], delay_sec=12.5)

    now[0] = 105.0
    nxt = pool.acquire()
    assert nxt.api_key == "key-2"
    pool.release(nxt, delay_sec=12.5)


def test_api_get_rotates_on_429_and_disables_403_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = fmo.KeyPool(["bad-key", "throttled-key", "good-key"])
    calls: list[str] = []

    def fake_once(
        url: str,
        api_key: str,
        *,
        key_label: str,
        debug: bool = False,
    ) -> dict[str, Any]:
        del url, key_label, debug
        calls.append(api_key)
        if api_key == "bad-key":
            raise HTTPError(
                "https://api.massive.com/x",
                403,
                "Forbidden",
                Message(),
                BytesIO(b"not authorized"),
            )
        if api_key == "throttled-key":
            hdrs = Message()
            hdrs["Retry-After"] = "30"
            raise HTTPError(
                "https://api.massive.com/x",
                429,
                "Too Many Requests",
                hdrs,
                BytesIO(b"rate limit"),
            )
        return {"status": "OK", "results": [{"c": 12.34}]}

    monkeypatch.setattr(fmo, "_api_get_once", fake_once)
    payload = fmo._api_get(
        "https://api.massive.com",
        "/v2/aggs/test",
        {},
        pool,
        delay_sec=0.0,
    )
    assert payload["status"] == "OK"
    assert calls == ["bad-key", "throttled-key", "good-key"]
    assert pool.slots[0].disabled_reason == "HTTP 403"
    assert pool.slots[1].rate_limits_hit == 1
    assert pool.active_count() == 2


def test_api_get_raises_when_all_keys_fail_403(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = fmo.KeyPool(["k1", "k2"])

    def fake_once(
        url: str,
        api_key: str,
        *,
        key_label: str,
        debug: bool = False,
    ) -> dict[str, Any]:
        del url, api_key, key_label, debug
        raise HTTPError("https://api.massive.com/x", 403, "Forbidden", Message(), BytesIO(b"nope"))

    monkeypatch.setattr(fmo, "_api_get_once", fake_once)
    with pytest.raises(RuntimeError, match="HTTP 403 NOT_AUTHORIZED"):
        fmo._api_get("https://api.massive.com", "/v2/aggs/test", {}, pool, delay_sec=0.0)


def test_select_shard_and_deterministic_ordering() -> None:
    contracts = [
        {
            "ticker": "O:NVDA261218C00150000",
            "expiration_date": "2026-12-18",
            "strike_price": 150.0,
            "contract_type": "call",
        },
        {
            "ticker": "O:NVDA261120P00140000",
            "expiration_date": "2026-11-20",
            "strike_price": 140.0,
            "contract_type": "put",
        },
        {
            "ticker": "O:NVDA261218P00140000",
            "expiration_date": "2026-12-18",
            "strike_price": 140.0,
            "contract_type": "put",
        },
        {
            "ticker": "O:NVDA261120C00150000",
            "expiration_date": "2026-11-20",
            "strike_price": 150.0,
            "contract_type": "call",
        },
        {
            "ticker": "O:NVDA261218C00140000",
            "expiration_date": "2026-12-18",
            "strike_price": 140.0,
            "contract_type": "call",
        },
    ]
    ordered = fmo._order_contracts_deterministically(contracts)
    assert [c["ticker"] for c in ordered] == [
        "O:NVDA261218C00140000",
        "O:NVDA261218P00140000",
        "O:NVDA261218C00150000",
        "O:NVDA261120P00140000",
        "O:NVDA261120C00150000",
    ]

    for shard_count in (2, 3, 4):
        combined: list[str] = []
        for idx in range(shard_count):
            shard = fmo._select_shard(ordered, idx, shard_count)
            combined.extend(str(c["ticker"]) for c in shard)
        assert sorted(combined) == sorted(str(c["ticker"]) for c in ordered)
        assert len(combined) == len(ordered)

    with pytest.raises(ValueError):
        fmo._select_shard(ordered, 3, 3)


def test_plan_cache_fetch_supports_hit_and_tail_extension() -> None:
    entry = {
        "from": "2026-10-01",
        "to": "2026-11-10",
        "bars": {"2026-10-01": [5.25, 100], "2026-11-10": [6.10, 42]},
    }
    bars_hit, fetch_hit = fmo._plan_cache_fetch(entry, "2026-10-01", "2026-11-10", use_cache=True)
    assert fetch_hit is None
    assert bars_hit == {"2026-10-01": (5.25, 100), "2026-11-10": (6.10, 42)}

    bars_tail, fetch_tail = fmo._plan_cache_fetch(entry, "2026-10-01", "2026-11-12", use_cache=True)
    assert fetch_tail == "2026-11-11"
    assert bars_tail == bars_hit

    bars_off, fetch_off = fmo._plan_cache_fetch(entry, "2026-10-01", "2026-11-10", use_cache=False)
    assert fetch_off == "2026-10-01"
    assert bars_off == {}


def test_fetch_bars_with_pool_distributes_across_4_keys_and_merges_csvs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = fmo.KeyPool(["keyA", "keyB", "keyC", "keyD"])
    seen_keys_lock = threading.Lock()
    used_api_keys: set[str] = set()

    def fake_once(
        url: str,
        api_key: str,
        *,
        key_label: str,
        debug: bool = False,
    ) -> dict[str, Any]:
        del url, key_label, debug
        with seen_keys_lock:
            used_api_keys.add(api_key)
        # 2026-10-15T16:00 ET -> 2026-10-15T20:00Z -> 1792094400000 ms (approx; compute exact)
        from datetime import UTC, datetime

        ts_ms = int(datetime(2026, 10, 15, 20, 0, tzinfo=UTC).timestamp() * 1000)
        return {"status": "OK", "results": [{"t": ts_ms, "c": 4.5, "v": 120}]}

    monkeypatch.setattr(fmo, "_api_get_once", fake_once)

    contracts: list[dict[str, Any]] = [
        {
            "ticker": f"O:NVDA261120C{100 + i * 5:05d}000",
            "expiration_date": "2026-11-20",
            "strike_price": float(100 + i * 5),
            "contract_type": "call",
        }
        for i in range(8)
    ]
    work_items: list[tuple[dict[str, Any], dict[str, tuple[float, int]], str]] = [
        (c, {}, "2026-10-15") for c in contracts
    ]
    results: list[tuple[str, dict[str, tuple[float, int]]]] = []

    fmo._fetch_bars_with_pool(
        work_items,
        base_url="https://api.massive.com",
        bars_end_date="2026-10-15",
        key_pool=pool,
        delay_sec=0.0,
        debug=False,
        on_result=lambda c, bars, _tail: results.append((str(c["ticker"]), bars)),
    )

    assert len(results) == 8
    assert len(used_api_keys) >= 2
    assert sum(s.requests_made for s in pool.slots) == 8

    # Verify locked cache save + day snapshot write
    cache_path = fmo._cache_path_for(tmp_path, "NVDA")
    bar_cache = {
        t: {"from": "2026-10-15", "to": "2026-10-15", "bars": {"2026-10-15": [4.5, 120]}}
        for t, _ in results
    }
    fmo._save_bar_cache(cache_path, "NVDA", bar_cache, updated_keys={t for t, _ in results})
    loaded = fmo._load_bar_cache(cache_path)
    assert len(loaded) == 8

    ticker_dir = tmp_path / "NVDA"
    ticker_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        fmo._build_day_row(
            ticker="NVDA",
            u_conid=1000001,
            opt_conid=fmo._stable_conid(str(c["ticker"])),
            day_str="2026-10-15",
            spot_px=135.0,
            expiry=str(c["expiration_date"]),
            dte=36,
            strike=float(c["strike_price"]),
            right="C",
            close_px=4.5,
            volume=120,
        )
        for c in contracts
    ]
    out_file, appended, updated = fmo._write_day_snapshot(
        tmp_path,
        ticker_dir,
        "NVDA",
        "2026-10-15",
        135.0,
        rows,
        overwrite=False,
        range_pct=0.2,
        strike_step=5.0,
    )
    assert out_file is not None and out_file.is_file()
    assert appended == 8 and updated == 0
    idx_data = json.loads((tmp_path / "metadata" / "snapshot_index.json").read_text("utf-8"))
    assert idx_data["tickers"]["NVDA"]["snapshots"][0]["contract_count"] == 8


def test_atomic_replace_retries_transient_permission_error_and_cleans_tmp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / ".cache.tmp"
    dst = tmp_path / ".cache.json"
    src.write_text('{"ok":true}', encoding="utf-8")

    real_replace = os.replace
    attempts = [0]
    sleeps: list[float] = []

    def flaky_replace(s: Path | str, d: Path | str) -> None:
        attempts[0] += 1
        if attempts[0] <= 3:
            raise PermissionError(13, "Access is denied")
        real_replace(s, d)

    monkeypatch.setattr(os, "replace", flaky_replace)
    fmo._atomic_replace(src, dst, max_attempts=5, initial_delay_sec=0.05, sleep=sleeps.append)

    assert attempts[0] == 4
    assert sleeps == [0.05, 0.1, 0.2]
    assert dst.read_text(encoding="utf-8") == '{"ok":true}'
    assert not src.exists()

    # When retries exhaust, PermissionError propagates and stale tmp file is unlinked
    src2 = tmp_path / ".cache2.tmp"
    src2.write_text("stale", encoding="utf-8")

    def always_fail(s: Path | str, d: Path | str) -> None:
        del s, d
        raise PermissionError(13, "Locked")

    monkeypatch.setattr(os, "replace", always_fail)
    with pytest.raises(PermissionError):
        fmo._atomic_replace(src2, dst, max_attempts=3, initial_delay_sec=0.05, sleep=lambda _: None)
    assert not src2.exists()


def test_main_batches_bar_cache_flushes_and_flushes_in_finally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contracts: list[dict[str, Any]] = [
        {
            "ticker": f"O:NVDA261120C{100 + i * 5:05d}000",
            "expiration_date": "2026-11-20",
            "strike_price": float(100 + i * 5),
            "contract_type": "call",
        }
        for i in range(13)
    ]

    monkeypatch.setattr(
        fmo,
        "fetch_stock_daily_bars",
        lambda *a, **kw: {"2026-10-15": 130.0},
    )
    monkeypatch.setattr(
        fmo,
        "fetch_contracts_for_window",
        lambda *a, **kw: contracts,
    )
    monkeypatch.setattr(
        fmo,
        "fetch_option_daily_bars",
        lambda *a, **kw: {"2026-10-15": (5.0, 10)},
    )

    save_calls: list[int] = []
    real_save = fmo._save_bar_cache

    def tracked_save(
        cache_path: Path,
        ticker: str,
        cache_dict: dict[str, dict[str, Any]],
        *,
        updated_keys: set[str] | None = None,
    ) -> None:
        save_calls.append(len(updated_keys or ()))
        real_save(cache_path, ticker, cache_dict, updated_keys=updated_keys)

    monkeypatch.setattr(fmo, "_save_bar_cache", tracked_save)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fetch_massive_options.py",
            "--ticker",
            "NVDA",
            "--date",
            "2026-10-15",
            "--spot",
            "130.0",
            "--no-until-expiry",
            "--api-key",
            "k1,k2",
            "--delay-sec",
            "0",
            "--range-pct",
            "0.5",
            "--output-dir",
            str(tmp_path / "snapshots"),
        ],
    )

    fmo.main()
    # 13 contracts -> 1 flush at batch size 10 + 1 final flush for remaining 3
    assert save_calls == [10, 3]
    cache_path = fmo._cache_path_for(tmp_path / "snapshots", "NVDA")
    assert len(fmo._load_bar_cache(cache_path)) == 13
