"""Unit tests for `LogoStore` and `LogoService` (disk caching, validation, offline fallback)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from src.core.config import StorageSettings
from src.core.exceptions import InvalidTickerError
from src.services.logos import LogoService, UpstreamFetchResult
from src.storage.logo_store import (
    MAX_LOGO_BYTES,
    LogoStore,
    resolve_logo_cache_dir,
    validate_logo_payload,
)
from tests.support.aio import run_virtual_loop

_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    b'<circle cx="8" cy="8" r="6"/></svg>'
)
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
_WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"\x00" * 8


async def _inline(func: object) -> object:
    assert callable(func)
    return func()


def test_resolve_logo_cache_dir_and_payload_validation(tmp_path: Path) -> None:
    resolved = resolve_logo_cache_dir(StorageSettings(), root=tmp_path)
    assert resolved == (tmp_path / "data" / "cache" / "logos").resolve()

    assert validate_logo_payload(_SVG, "svg") == _SVG
    assert validate_logo_payload(_PNG, "png") == _PNG
    assert validate_logo_payload(_WEBP, "webp") == _WEBP

    with pytest.raises(ValueError):
        validate_logo_payload(b"", "svg")
    with pytest.raises(ValueError):
        validate_logo_payload(b"x" * (MAX_LOGO_BYTES + 1), "png")
    with pytest.raises(ValueError):
        validate_logo_payload(b"<html>not png</html>", "png")
    with pytest.raises(ValueError):
        validate_logo_payload(b"RIFF1234XXXX", "webp")
    for bad_svg in (
        b"<svg><script>alert(1)</script></svg>",
        b'<svg onload="alert(1)"></svg>',
        b'<svg><a href="javascript:alert(1)">x</a></svg>',
        b"<svg><foreignObject></foreignObject></svg>",
    ):
        with pytest.raises(ValueError):
            validate_logo_payload(bad_svg, "svg")


def test_logo_store_roundtrip_manual_override_and_ttl(tmp_path: Path) -> None:
    now = {"t": 1_000_000.0}
    store = LogoStore(
        tmp_path / "logos",
        logo_ttl_sec=100.0,
        miss_ttl_sec=50.0,
        clock=lambda: now["t"],
    )
    store.ensure_layout()
    assert store.cache_dir.is_dir()

    # Empty read
    assert store.read("nvda").logo is None
    assert store.read("nvda").miss_active is False

    # Missing marker active until TTL expires
    store.mark_missing("spcx")
    assert store.read("SPCX").miss_active is True

    # Manual offline image file placed directly into cache dir overrides `.miss`
    manual_png = store.cache_dir / "SPCX.png"
    manual_png.write_bytes(_PNG)
    os.utime(manual_png, (now["t"], now["t"]))
    manual_res = store.read("SPCX")
    assert manual_res.logo is not None
    assert manual_res.logo.ext == "png"
    assert manual_res.logo.media_type == "image/png"
    assert manual_res.logo.is_stale is False
    assert manual_res.miss_active is False

    # Saving SVG replaces older PNG and clears `.miss`
    saved = store.save("NVDA", _SVG, "svg")
    os.utime(store.cache_dir / "NVDA.svg", (now["t"], now["t"]))
    assert saved.media_type == "image/svg+xml"
    store.save("NVDA", _PNG, "png")
    assert not (store.cache_dir / "NVDA.svg").exists()
    assert (store.cache_dir / "NVDA.png").is_file()

    # Staleness flips past TTL
    now["t"] += 200.0
    os.utime(store.cache_dir / "NVDA.png", (1_000_000.0, 1_000_000.0))
    stale_res = store.read("NVDA")
    assert stale_res.logo is not None
    assert stale_res.logo.is_stale is True

    # Invalid ticker rejected
    with pytest.raises(InvalidTickerError):
        store.read("../etc")


def test_logo_service_caching_dedupe_refresh_and_offline_stale_fallback(tmp_path: Path) -> None:
    now = {"t": 2_000_000.0}
    store = LogoStore(
        tmp_path / "logos",
        logo_ttl_sec=60.0,
        miss_ttl_sec=60.0,
        clock=lambda: now["t"],
    )
    calls: list[str] = []
    next_result: dict[str, UpstreamFetchResult] = {
        "NVDA": UpstreamFetchResult(status="ok", data=_SVG, ext="svg"),
        "SPCX": UpstreamFetchResult(status="not_found"),
    }

    def _fetcher(symbol: str) -> UpstreamFetchResult:
        calls.append(symbol)
        return next_result.get(symbol, UpstreamFetchResult(status="error"))

    svc = LogoService(store, fetcher=_fetcher, runner=_inline)  # type: ignore[arg-type]

    # 1. Cold fetch caches upstream result; subsequent call hits cache
    res1 = run_virtual_loop(svc.get_logo("nvda"))
    assert res1 is not None and res1.source == "upstream" and res1.data == _SVG
    res2 = run_virtual_loop(svc.get_logo("NVDA"))
    assert res2 is not None and res2.source == "cache"
    assert calls == ["NVDA"]

    # 2. Missing ticker writes `.miss` and skips upstream while active
    assert run_virtual_loop(svc.get_logo("SPCX")) is None
    assert run_virtual_loop(svc.get_logo("SPCX")) is None
    assert calls == ["NVDA", "SPCX"]

    # 3. Transient offline error does NOT write `.miss`
    assert run_virtual_loop(svc.get_logo("AVGO")) is None
    assert not (store.cache_dir / "AVGO.miss").exists()

    # 4. Stale logo tries upstream and falls back to stale cache if offline
    os.utime(store.cache_dir / "NVDA.svg", (1_000_000.0, 1_000_000.0))
    next_result["NVDA"] = UpstreamFetchResult(status="error")
    stale_hit = run_virtual_loop(svc.get_logo("NVDA"))
    assert stale_hit is not None
    assert stale_hit.source == "stale_cache"
    assert stale_hit.data == _SVG

    # 5. Forced `?refresh=true` updates cached logo when upstream responds
    next_result["NVDA"] = UpstreamFetchResult(status="ok", data=_PNG, ext="png")
    refreshed = run_virtual_loop(svc.get_logo("NVDA", refresh=True))
    assert refreshed is not None
    assert refreshed.source == "upstream"
    assert refreshed.media_type == "image/png"
    assert (store.cache_dir / "NVDA.png").is_file()

    # 6. Concurrent cold fetches for same ticker deduplicate to one upstream call
    calls.clear()
    next_result["META"] = UpstreamFetchResult(status="ok", data=_SVG, ext="svg")

    async def _concurrent() -> list[object]:
        return list(await asyncio.gather(svc.get_logo("META"), svc.get_logo("META")))

    conc = run_virtual_loop(_concurrent())
    assert len(conc) == 2
    assert calls == ["META"]
