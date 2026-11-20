"""HTTP contract of the watchlist and batch refresh routes (fakes only, no sockets)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.core.config import WatchlistSettings
from src.core.exceptions import GatewayConnectionError, GatewayTimeoutError
from src.main import create_app
from src.services.analyzer import AnalyzerService
from src.services.batch_refresh import BatchRefreshService
from tests.support.aio import run_virtual_loop
from tests.unit.test_batch_refresh_service import _make

BATCH_ID = "ab" * 16
SEED = list(WatchlistSettings().hardcoded_tickers)
SECRET = "token=SUPERSECRET at C:\\Users\\someone\\stack.py line 42"


async def _inline(func: Callable[[], Any]) -> Any:
    return func()


def _app(
    tmp_path: Path,
    *,
    pause_sec: float = 1.0,
    failures: dict[str, BaseException] | None = None,
    watchlist: WatchlistSettings | None = None,
) -> tuple[FastAPI, AnalyzerService, BatchRefreshService]:
    svc, batch, _fetcher, _corridor, _quoted = _make(
        tmp_path, pause_sec=pause_sec, failures=failures, watchlist=watchlist
    )
    svc.watchlist.runner = _inline
    app = create_app(settings=svc.settings, repo=svc.repo)
    app.state.service = svc
    app.state.batch_refresh = batch
    return app, svc, batch


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


async def _settle(batch: BatchRefreshService) -> None:
    task = batch._task
    if task is not None:
        await asyncio.wait({task})


# ------------------------------------------------------------------ watchlist CRUD
def test_watchlist_get_add_dedupe_remove(tmp_path: Path) -> None:
    app, _svc, _batch = _app(tmp_path)

    async def _scenario() -> None:
        async with _client(app) as client:
            r = await client.get("/api/watchlist")
            assert r.status_code == 200
            body = r.json()
            assert body["tickers"] == SEED
            assert body["count"] == len(SEED)
            assert body["max_size"] == 20
            assert [e["ticker"] for e in body["entries"]] == SEED

            r = await client.post("/api/watchlist/tickers", json={"ticker": " aapl "})
            assert r.status_code == 200
            assert r.json()["tickers"] == [*SEED, "AAPL"]
            assert r.json()["changed"] is True

            r = await client.post("/api/watchlist/tickers", json={"ticker": "AAPL"})
            assert r.status_code == 200
            assert r.json()["tickers"] == [*SEED, "AAPL"]
            assert r.json()["changed"] is False

            r = await client.delete("/api/watchlist/tickers/aapl")
            assert r.status_code == 200
            assert r.json()["tickers"] == SEED

            r = await client.get("/api/watchlist")
            assert r.json()["tickers"] == SEED

    run_virtual_loop(_scenario())
    assert (tmp_path / "data" / "watchlist.json").is_file()


def test_watchlist_cap_invalid_ticker_and_extra_fields(tmp_path: Path) -> None:
    app, _svc, _batch = _app(tmp_path)

    async def _scenario() -> None:
        async with _client(app) as client:
            for sym in ("AA", "BB", "CC", "DD"):
                assert (
                    await client.post("/api/watchlist/tickers", json={"ticker": sym})
                ).status_code == 200
            full = await client.post("/api/watchlist/tickers", json={"ticker": "EE"})
            assert full.status_code == 409
            assert full.json()["error_code"] == "WATCHLIST_FULL"
            assert (await client.get("/api/watchlist")).json()["is_full"] is True

            bad = await client.post("/api/watchlist/tickers", json={"ticker": "BAD$"})
            assert bad.status_code == 422

            bad_del = await client.delete("/api/watchlist/tickers/..%2Fetc")
            assert bad_del.status_code in (404, 422)
            bad_del2 = await client.delete("/api/watchlist/tickers/BAD$")
            assert bad_del2.status_code == 422
            assert bad_del2.json()["error_code"] == "INVALID_TICKER"

            extra = await client.post("/api/watchlist/tickers", json={"ticker": "ZZ", "note": "x"})
            assert extra.status_code == 422

    run_virtual_loop(_scenario())


def test_watchlist_unreadable_store_returns_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, svc, _batch = _app(tmp_path)
    run_virtual_loop(svc.watchlist.add_ticker("MU"))
    locked = svc.watchlist.store.path
    real_open = Path.open

    def _open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self == locked:
            raise PermissionError(13, "The process cannot access the file", str(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)

    async def _scenario() -> None:
        async with _client(app) as client:
            r = await client.post("/api/watchlist/tickers", json={"ticker": "AMD"})
            assert r.status_code == 503
            assert r.json()["error_code"] == "STORAGE_UNAVAILABLE"
            assert "cannot access" not in r.text

    run_virtual_loop(_scenario())


# ------------------------------------------------------------------ batch refresh
def test_batch_lifecycle_get_current_and_errors(tmp_path: Path) -> None:
    app, svc, batch = _app(tmp_path)

    async def _scenario() -> None:
        async with _client(app) as client:
            r = await client.get("/api/refresh/batch/current")
            assert r.status_code == 200
            assert r.json() is None

            r = await client.post(
                "/api/refresh/batch",
                json={"tickers": ["nvda", "AVGO"], "range_pct": 0.2},
            )
            assert r.status_code == 202
            started = r.json()
            assert started["batch_id"] == BATCH_ID
            assert started["state"] == "running"
            assert started["total"] == 2

            cur = await client.get("/api/refresh/batch/current")
            assert cur.json()["batch_id"] == BATCH_ID

            got = await client.get(f"/api/refresh/batch/{BATCH_ID}")
            assert got.status_code == 200
            assert got.json()["total"] == 2

            # Second batch and single refresh while running -> 409 REFRESH_BUSY.
            busy = await client.post("/api/refresh/batch", json={"tickers": ["CRDO"]})
            assert busy.status_code == 409
            assert busy.json()["error_code"] == "REFRESH_BUSY"
            single = await client.post("/api/refresh", json={"ticker": "CRDO"})
            assert single.status_code == 409
            assert single.json()["error_code"] == "REFRESH_BUSY"

            bad_pattern = await client.get("/api/refresh/batch/not-a-batch-id")
            assert bad_pattern.status_code == 422
            bad_cancel = await client.post("/api/refresh/batch/XYZ/cancel")
            assert bad_cancel.status_code == 422
            unknown = await client.get(f"/api/refresh/batch/{'0' * 32}")
            assert unknown.status_code == 404
            assert unknown.json()["error_code"] == "BATCH_NOT_FOUND"
            unknown_cancel = await client.post(f"/api/refresh/batch/{'0' * 32}/cancel")
            assert unknown_cancel.status_code == 404

            await asyncio.sleep(0.1)
            cancelled = await client.post(f"/api/refresh/batch/{BATCH_ID}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["state"] == "cancelling"

            await _settle(batch)
            final = (await client.get(f"/api/refresh/batch/{BATCH_ID}")).json()
            assert final["state"] == "cancelled"
            # Cancel stops at once: NVDA (no finished page yet) is cancelled mid-fetch.
            assert [i["state"] for i in final["items"]] == ["cancelled", "cancelled"]
            assert [i["partial"] for i in final["items"]] == [False, False]
            assert final["not_started_count"] == 0

    run_virtual_loop(_scenario())
    assert not svc.refresh_lock.locked()


def test_batch_validation_and_sanitized_failure(tmp_path: Path) -> None:
    app, _svc, batch = _app(
        tmp_path,
        pause_sec=0.0,
        failures={
            "AVGO": RuntimeError(SECRET),
            "CRDO": GatewayConnectionError("Lost during chain_fetch"),
        },
    )

    async def _scenario() -> None:
        async with _client(app) as client:
            bad = await client.post("/api/refresh/batch", json={"tickers": ["NVDA", "BAD$"]})
            assert bad.status_code == 422
            empty = await client.post("/api/refresh/batch", json={"tickers": []})
            assert empty.status_code == 422
            extra = await client.post(
                "/api/refresh/batch", json={"tickers": ["NVDA"], "strike_step": 5.0}
            )
            assert extra.status_code == 422

            ok = await client.post("/api/refresh/batch", json={"tickers": ["AVGO", "CRDO", "NVDA"]})
            assert ok.status_code == 202
            await _settle(batch)
            final = (await client.get(f"/api/refresh/batch/{BATCH_ID}")).json()
            # Gateway connection loss on CRDO stops the batch: NVDA is never started.
            assert final["state"] == "aborted"
            assert final["not_started_count"] == 1
            assert final["notices"][-1].startswith("Stopped: IBKR Gateway connection lost")
            items = {i["ticker"]: i for i in final["items"]}
            assert items["NVDA"]["state"] == "not_started"
            assert items["AVGO"]["state"] == "failed"
            assert items["AVGO"]["error_code"] == "INTERNAL_ERROR"
            assert items["CRDO"]["state"] == "failed"
            assert items["CRDO"]["error_code"] == "GATEWAY_OFFLINE"
            assert items["CRDO"]["partial"] is True  # its finished page was archived
            assert "SUPERSECRET" not in str(final)
            assert "Traceback" not in str(final)

    run_virtual_loop(_scenario())


def test_single_refresh_gateway_timeout_vs_offline_codes(tmp_path: Path) -> None:
    app, _svc, _batch = _app(
        tmp_path,
        pause_sec=0.0,
        failures={
            "NVDA": GatewayTimeoutError("IBKR Gateway did not answer during chain_fetch"),
            "AVGO": GatewayConnectionError("Lost during chain_fetch"),
        },
    )

    async def _scenario() -> None:
        async with _client(app) as client:
            r = await client.post("/api/refresh", json={"ticker": "NVDA"})
            assert r.status_code == 503
            assert r.json()["error_code"] == "GATEWAY_TIMEOUT"
            assert "not answering" in r.json()["message"]
            r2 = await client.post("/api/refresh", json={"ticker": "AVGO"})
            assert r2.status_code == 503
            assert r2.json()["error_code"] == "GATEWAY_OFFLINE"
            assert "unreachable" in r2.json()["message"]

    run_virtual_loop(_scenario())


def test_index_bootstrap_uses_store_watchlist(tmp_path: Path) -> None:
    app, svc, _batch = _app(tmp_path, pause_sec=0.0)
    run_virtual_loop(svc.watchlist.add_ticker("AAPL"))

    async def _scenario() -> str:
        async with _client(app) as client:
            r = await client.get("/")
            assert r.status_code == 200
            return r.text

    html = run_virtual_loop(_scenario())
    assert '"AAPL"' in html
    assert 'id="watchlist-active-logo"' in html
    for sym in SEED:
        assert f'"{sym}"' in html
    assert svc.watchlist.cached_tickers()[-1] == "AAPL"


def test_logo_route_caches_serves_headers_and_handles_missing_and_invalid(tmp_path: Path) -> None:
    from src.services.logos import UpstreamFetchResult

    app, svc, _batch = _app(tmp_path)
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
        b'<rect width="16" height="16"/></svg>'
    )
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    calls: list[str] = []
    mode = {"nvda": "svg"}

    def _fetcher(symbol: str) -> UpstreamFetchResult:
        calls.append(symbol)
        if symbol == "NVDA":
            if mode["nvda"] == "png":
                return UpstreamFetchResult(status="ok", data=png, ext="png")
            return UpstreamFetchResult(status="ok", data=svg, ext="svg")
        if symbol == "SPCX":
            return UpstreamFetchResult(status="not_found")
        return UpstreamFetchResult(status="error")

    svc.logos.fetcher = _fetcher
    svc.logos.runner = _inline

    async def _scenario() -> None:
        async with _client(app) as client:
            r1 = await client.get("/api/logo/nvda")
            assert r1.status_code == 200
            assert r1.headers["content-type"].startswith("image/svg+xml")
            assert r1.headers["x-content-type-options"] == "nosniff"
            assert r1.headers["content-security-policy"] == "sandbox"
            assert r1.content == svg

            # Second request served from local cache
            r2 = await client.get("/api/logo/NVDA")
            assert r2.status_code == 200
            assert calls == ["NVDA"]

            # Forced refresh updates file
            mode["nvda"] = "png"
            r3 = await client.get("/api/logo/NVDA", params={"refresh": "true"})
            assert r3.status_code == 200
            assert r3.headers["content-type"].startswith("image/png")
            assert r3.content == png

            # Missing / offline -> 404
            r_miss = await client.get("/api/logo/SPCX")
            assert r_miss.status_code == 404

            # Invalid ticker -> 422
            r_bad = await client.get("/api/logo/BAD$TICKER")
            assert r_bad.status_code == 422
            assert r_bad.json()["error_code"] == "INVALID_TICKER"

    run_virtual_loop(_scenario())


# ------------------------------------------------------------------ shutdown order
@pytest.mark.parametrize("aclose_error", [None, RuntimeError("boom"), asyncio.CancelledError()])
def test_lifespan_shutdown_order_and_disconnect_always_runs(
    tmp_path: Path, aclose_error: BaseException | None
) -> None:
    base_svc, _b, *_ = _make(tmp_path, pause_sec=0.0)
    app = create_app(settings=base_svc.settings, repo=base_svc.repo)
    svc: AnalyzerService = app.state.service  # the lifespan closure owns these instances
    batch: BatchRefreshService = app.state.batch_refresh
    svc.watchlist.runner = _inline
    calls: list[str] = []

    async def _aclose() -> None:
        calls.append("aclose")
        if aclose_error is not None:
            raise aclose_error

    async def _stop() -> Any:
        calls.append("corridor_stop")

    def _disconnect() -> None:
        calls.append("disconnect")

    batch.aclose = _aclose  # type: ignore[method-assign]
    svc.stop_corridor_streams = _stop  # type: ignore[method-assign]
    svc.broker_client.disconnect = _disconnect  # type: ignore[method-assign]

    async def _scenario() -> None:
        async with app.router.lifespan_context(app):
            pass

    if isinstance(aclose_error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            run_virtual_loop(_scenario())
        assert calls == ["aclose", "disconnect"]
    else:
        run_virtual_loop(_scenario())
        assert calls == ["aclose", "corridor_stop", "disconnect"]
