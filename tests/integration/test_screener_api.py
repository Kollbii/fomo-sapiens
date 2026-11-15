"""Best Option screener routes: run, live SSE stream, reallocate, stop, shutdown order."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.core.config import Settings, StorageSettings
from src.core.exceptions import (
    GatewayConnectionError,
    GatewayTimeoutError,
    LiveStreamNotFoundError,
    OptionChainFetchError,
    PacingViolationError,
    RefreshBusyError,
    ScreenerLiveBusyError,
)
from src.main import create_app
from src.services.analyzer import AnalyzerService
from src.services.schemas import (
    ScreenerLiveFrame,
    ScreenerLiveReallocateRequest,
    ScreenerLiveReallocateResponse,
    ScreenerLiveRequest,
    ScreenerLiveStopResponse,
)
from src.storage.csv_repository import CSVRepository
from tests.support.aio import run_virtual_loop

SECRET = "token=SUPERSECRET at C:\\Users\\someone\\stack.py line 42"


def _app(tmp_path: Path) -> tuple[FastAPI, AnalyzerService]:
    settings = Settings(storage=StorageSettings())
    repo = CSVRepository(settings.storage, root=tmp_path)
    app = create_app(settings=settings, repo=repo)
    svc: AnalyzerService = app.state.service
    return app, svc


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


def _frame(seq: int, *, stopped: bool = False) -> ScreenerLiveFrame:
    return ScreenerLiveFrame(
        stream_id="screener-abc",
        sequence=seq,
        version=0,
        updated_ts_utc="2026-11-15T15:00:00+00:00",
        market_data_type=3,
        market_data_type_label="Delayed",
        lines_used=3,
        lines_budget=90,
        option_lines_budget=88,
        stopped=stopped,
        stop_reason="refresh_chain" if stopped else None,
    )


class _FakeStream:
    """Stands in for `ScreenerLiveStream`: yields `items` (frames or exceptions to raise)."""

    def __init__(self, items: list[Any]) -> None:
        self.stream_id = "screener-abc"
        self._items = list(items)
        self.closed = 0

    def __aiter__(self) -> _FakeStream:
        return self

    async def __anext__(self) -> ScreenerLiveFrame:
        if not self._items:
            raise StopAsyncIteration
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert isinstance(item, ScreenerLiveFrame)
        return item

    async def aclose(self) -> None:
        self.closed += 1


def _install_stream(svc: AnalyzerService, stream: _FakeStream) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def _open(request: ScreenerLiveRequest, **kwargs: Any) -> _FakeStream:
        seen.append({"request": request, **kwargs})
        return stream

    svc.screener_live.open_live_stream = _open  # type: ignore[method-assign,assignment]
    return seen


def _q(**fields: Any) -> dict[str, str]:
    return {"q": json.dumps(fields)}


def _events(text: str) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for block in text.strip().split("\n\n"):
        name, data = "", ""
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.partition(":")[2].strip()
            elif line.startswith("data:"):
                data = line.partition(":")[2].strip()
        out.append((name, json.loads(data)))
    return out


def _run(app: FastAPI, scenario: Callable[[httpx.AsyncClient], Awaitable[Any]]) -> Any:
    async def _go() -> Any:
        async with _client(app) as client:
            return await scenario(client)

    return asyncio.run(_go())


# ------------------------------------------------------------------ run
def test_run_modes_and_validation(tmp_path: Path) -> None:
    app, _svc = _app(tmp_path)

    async def _scenario(c: httpx.AsyncClient) -> None:
        for mode in ("best", "all"):
            resp = await c.post("/api/screener/run", json={"mode": mode})
            assert resp.status_code == 200, resp.text
            assert resp.json()["mode"] == mode
        resp = await c.post(
            "/api/screener/run", json={"mode": "selected", "strategy_kind": "BULL_PUT_SPREAD"}
        )
        assert resp.status_code == 200
        assert resp.json()["params"]["strategy_kind"] == "BULL_PUT_SPREAD"
        assert (await c.post("/api/screener/run", json={})).status_code == 200
        # selected without strategy, extra field, bad bounds
        assert (await c.post("/api/screener/run", json={"mode": "selected"})).status_code == 422
        assert (await c.post("/api/screener/run", json={"bogus": 1})).status_code == 422
        assert (await c.post("/api/screener/run", json={"min_pop_pct": 101})).status_code == 422
        assert (
            await c.post("/api/screener/run", json={"dte_min": 90, "dte_max": 40})
        ).status_code == 422

    _run(app, _scenario)


# ------------------------------------------------------------------ stream
def test_stream_open_frames_and_stopped_events(tmp_path: Path) -> None:
    app, svc = _app(tmp_path)
    stream = _FakeStream([_frame(1), _frame(2), _frame(3, stopped=True), _frame(4)])
    seen = _install_stream(svc, stream)

    async def _scenario(c: httpx.AsyncClient) -> httpx.Response:
        params = {**_q(mode="best", row_ids=["NVDA:BULL_PUT_SPREAD"]), "max_frames": "5"}
        return await c.get("/api/screener/stream", params=params)

    resp = _run(app, _scenario)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _events(resp.text)
    assert [name for name, _ in events] == ["open", "frame", "frame", "stopped"]
    assert events[0][1] == {
        "stream_id": "screener-abc",
        "lines_budget": 90,
        "option_lines_budget": 88,
    }
    assert events[1][1]["sequence"] == 1
    assert events[3][1]["stopped"] is True
    assert events[3][1]["stop_reason"] == "refresh_chain"
    assert stream.closed == 1
    assert seen[0]["request"].row_ids == ["NVDA:BULL_PUT_SPREAD"]
    assert seen[0]["max_frames"] == 5


@pytest.mark.parametrize(
    ("exc", "code", "status"),
    [
        (GatewayTimeoutError("IBKR Gateway did not answer"), "GATEWAY_TIMEOUT", 503),
        (GatewayConnectionError("lost"), "GATEWAY_OFFLINE", 503),
        (ScreenerLiveBusyError("busy fixed message"), "REFRESH_BUSY", 409),
        (OptionChainFetchError("fetch failed"), "SCREENER_STREAM_ERROR", 502),
        (PacingViolationError("pacing"), "SCREENER_STREAM_ERROR", 502),
    ],
)
def test_stream_in_stream_error_codes(
    tmp_path: Path, exc: Exception, code: str, status: int
) -> None:
    app, svc = _app(tmp_path)
    stream = _FakeStream([_frame(1), exc])
    _install_stream(svc, stream)

    async def _scenario(c: httpx.AsyncClient) -> httpx.Response:
        return await c.get("/api/screener/stream", params=_q(mode="best"))

    events = _events(_run(app, _scenario).text)
    assert [name for name, _ in events] == ["open", "frame", "error"]
    assert events[-1][1]["error_code"] == code
    assert events[-1][1]["status"] == status
    assert stream.closed == 1


def test_stream_unexpected_exception_has_generic_message(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app, svc = _app(tmp_path)
    stream = _FakeStream([RuntimeError(SECRET)])
    _install_stream(svc, stream)

    async def _scenario(c: httpx.AsyncClient) -> httpx.Response:
        return await c.get("/api/screener/stream", params=_q())

    with caplog.at_level(logging.ERROR):
        resp = _run(app, _scenario)
    assert "SUPERSECRET" not in resp.text
    assert "stack.py" not in resp.text
    err = _events(resp.text)[-1]
    assert err[0] == "error"
    assert err[1]["error_code"] == "INTERNAL_ERROR"
    assert err[1]["status"] == 500
    assert "SUPERSECRET" in caplog.text  # logged server-side only
    assert stream.closed == 1


def test_stream_client_disconnect_closes_stream(tmp_path: Path) -> None:
    """Cancelling the response body iteration (disconnect) still closes the stream."""
    from src.web.api import api_screener_stream

    _app_obj, svc = _app(tmp_path)

    class _Endless(_FakeStream):
        async def __anext__(self) -> ScreenerLiveFrame:
            return _frame(1)

    stream = _Endless([])
    _install_stream(svc, stream)

    class _Req:
        async def is_disconnected(self) -> bool:
            return False

    async def _scenario() -> None:
        resp = await api_screener_stream(
            request=_Req(),  # type: ignore[arg-type]
            q=json.dumps({"mode": "best"}),
            max_frames=None,
            svc=svc,
        )
        body: Any = resp.body_iterator
        assert "event: open" in str(await body.__anext__())
        assert "event: frame" in str(await body.__anext__())
        await body.aclose()

    asyncio.run(_scenario())
    assert stream.closed == 1


def test_stream_validation_and_prestream_errors(tmp_path: Path) -> None:
    app, svc = _app(tmp_path)

    async def _scenario(c: httpx.AsyncClient) -> None:
        get = c.get
        assert (await get("/api/screener/stream")).status_code == 422  # q missing
        for raw in (
            "not json",
            json.dumps({"bogus": 1}),
            json.dumps({"mode": "selected"}),
            json.dumps({"row_ids": [f"T{i}" for i in range(121)]}),
        ):
            resp = await get("/api/screener/stream", params={"q": raw})
            assert resp.status_code == 422, raw
            assert "stack" not in resp.text
        marker = "ZZMARKERZZ"
        too_big = await get("/api/screener/stream", params={"q": marker * 1300})
        assert too_big.status_code == 422
        assert too_big.json()["error_code"] == "INVALID_REQUEST"
        assert marker not in too_big.text
        short = await get("/api/screener/stream", params={"q": "{"})
        assert short.status_code == 422
        assert short.json()["error_code"] == "INVALID_REQUEST"
        for bad in (
            json.dumps({"<img src=x onerror=alert(1)>": 1}),
            json.dumps({"mode": "best", "row_ids": [{marker: 1}]}),
            json.dumps({"mode": marker}),
        ):
            resp = await get("/api/screener/stream", params={"q": bad})
            assert resp.status_code == 422
            assert resp.json()["error_code"] == "INVALID_REQUEST"
            assert "<img" not in resp.text
            assert marker not in resp.text
        assert (
            await get("/api/screener/stream", params={**_q(), "max_frames": "0"})
        ).status_code == 422

    _run(app, _scenario)

    # Real service: a held refresh_lock refuses the stream with 409 before any event.
    async def _busy(c: httpx.AsyncClient) -> httpx.Response:
        assert await svc.acquire_refresh("batch:1/3:NVDA")
        try:
            return await c.get("/api/screener/stream", params=_q(mode="best"))
        finally:
            svc.release_refresh()

    resp = _run(app, _busy)
    assert resp.status_code == 409
    assert resp.json()["error_code"] == "REFRESH_BUSY"


def test_stream_prestream_gateway_errors_use_existing_handlers(tmp_path: Path) -> None:
    app, svc = _app(tmp_path)

    async def _scenario(c: httpx.AsyncClient) -> None:
        for exc, code in (
            (GatewayConnectionError("down"), "GATEWAY_OFFLINE"),
            (GatewayTimeoutError("slow"), "GATEWAY_TIMEOUT"),
        ):

            async def _open(*_a: Any, _exc: Exception = exc, **_k: Any) -> Any:
                raise _exc

            svc.screener_live.open_live_stream = _open  # type: ignore[method-assign]
            resp = await c.get("/api/screener/stream", params=_q())
            assert resp.status_code == 503
            assert resp.json()["error_code"] == code

    _run(app, _scenario)


# ------------------------------------------------------------------ reallocate / stop
def test_reallocate_ok_404_and_409(tmp_path: Path) -> None:
    app, svc = _app(tmp_path)
    reply = ScreenerLiveReallocateResponse(
        stream_id="screener-abc",
        version=1,
        lines_planned=3,
        lines_budget=90,
        option_lines_budget=88,
        rows=[],
    )
    busy_exc = RefreshBusyError("busy", owner="x", retry_after_sec=2)
    outcome: list[Any] = [reply, LiveStreamNotFoundError("gone"), busy_exc]

    async def _realloc(request: ScreenerLiveReallocateRequest) -> ScreenerLiveReallocateResponse:
        item = outcome.pop(0)
        if isinstance(item, BaseException):
            raise item
        assert request.stream_id == "screener-abc"
        assert isinstance(item, ScreenerLiveReallocateResponse)
        return item

    svc.screener_live.reallocate = _realloc  # type: ignore[method-assign]
    body = {"stream_id": "screener-abc", "row_ids": ["NVDA:BULL_PUT_SPREAD"]}

    async def _scenario(c: httpx.AsyncClient) -> None:
        ok = await c.post("/api/screener/stream/reallocate", json=body)
        assert ok.status_code == 200
        assert ok.json()["version"] == 1
        missing = await c.post("/api/screener/stream/reallocate", json=body)
        assert missing.status_code == 404
        assert missing.json()["error_code"] == "LIVE_STREAM_NOT_FOUND"
        busy = await c.post("/api/screener/stream/reallocate", json=body)
        assert busy.status_code == 409
        assert busy.json()["error_code"] == "REFRESH_BUSY"
        assert (
            await c.post("/api/screener/stream/reallocate", json={"stream_id": ""})
        ).status_code == 422
        assert (
            await c.post("/api/screener/stream/reallocate", json={**body, "x": 1})
        ).status_code == 422

    _run(app, _scenario)


def test_stop_route(tmp_path: Path) -> None:
    app, svc = _app(tmp_path)
    reasons: list[str] = []

    async def _stop(*, reason: str = "manual_stop") -> ScreenerLiveStopResponse:
        reasons.append(reason)
        return ScreenerLiveStopResponse(stopped_subscriptions=4, active_streams=0)

    svc.screener_live.stop_live_streams = _stop  # type: ignore[method-assign]

    async def _scenario(c: httpx.AsyncClient) -> httpx.Response:
        return await c.post("/api/screener/stop")

    resp = _run(app, _scenario)
    assert resp.status_code == 200
    assert resp.json() == {"stopped_subscriptions": 4, "active_streams": 0}
    assert reasons == ["manual_stop"]


# ------------------------------------------------------------------ shutdown order
def test_lifespan_shutdown_stops_screener_before_disconnect(tmp_path: Path) -> None:
    app, svc = _app(tmp_path)
    calls: list[str] = []

    async def _corridor() -> Any:
        calls.append("corridor_stop")

    async def _screener(*, reason: str = "manual_stop") -> Any:
        calls.append(f"screener_stop:{reason}")

    def _now(*, reason: str) -> int:
        calls.append(f"screener_stop_now:{reason}")
        return 0

    svc.screener_live.stop_now = _now  # type: ignore[method-assign]

    svc.stop_corridor_streams = _corridor  # type: ignore[method-assign]
    svc.screener_live.stop_live_streams = _screener  # type: ignore[method-assign]
    svc.broker_client.disconnect = lambda: calls.append("disconnect")  # type: ignore[method-assign]

    async def _scenario() -> None:
        async with app.router.lifespan_context(app):
            pass

    run_virtual_loop(_scenario())
    assert calls == [
        "corridor_stop",
        "screener_stop:shutdown",
        "screener_stop_now:shutdown",
        "disconnect",
    ]


def test_lifespan_cancelled_batch_close_still_force_stops_screener(tmp_path: Path) -> None:
    app, svc = _app(tmp_path)
    calls: list[str] = []

    async def _aclose() -> None:
        raise asyncio.CancelledError()

    app.state.batch_refresh.aclose = _aclose

    def _now(*, reason: str) -> int:
        calls.append(f"now:{reason}")
        return 0

    svc.screener_live.stop_now = _now  # type: ignore[method-assign]
    svc.broker_client.disconnect = lambda: calls.append("disconnect")  # type: ignore[method-assign]

    async def _scenario() -> None:
        async with app.router.lifespan_context(app):
            pass

    with pytest.raises(asyncio.CancelledError):
        run_virtual_loop(_scenario())
    assert calls == ["now:shutdown", "disconnect"]


def test_sse_response_closes_stream_when_send_is_cancelled(tmp_path: Path) -> None:
    from src.web.api import api_screener_stream

    _app_obj, svc = _app(tmp_path)

    class _Endless(_FakeStream):
        async def __anext__(self) -> ScreenerLiveFrame:
            return _frame(1)

    stream = _Endless([])
    _install_stream(svc, stream)

    class _Req:
        async def is_disconnected(self) -> bool:
            return False

    async def _scenario() -> None:
        resp = await api_screener_stream(
            request=_Req(),  # type: ignore[arg-type]
            q=json.dumps({"mode": "best"}),
            max_frames=None,
            svc=svc,
        )
        sent = 0

        async def _receive() -> Any:
            await asyncio.sleep(3600)
            return {"type": "http.disconnect"}

        async def _send(_msg: Any) -> None:
            nonlocal sent
            sent += 1
            if sent >= 4:
                raise asyncio.CancelledError()

        # Starlette may or may not re-raise the cancellation; the stream must close either way.
        with contextlib.suppress(BaseException):
            await resp({"type": "http", "asgi": {"spec_version": "2.3"}}, _receive, _send)

    asyncio.run(_scenario())
    assert stream.closed >= 1
