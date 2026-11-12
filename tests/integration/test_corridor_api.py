from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.responses import StreamingResponse

from src.broker.client import IBKRBrokerClient
from src.broker.corridor import CORRIDOR_MAX_CONTRACTS, CorridorQuoteFrame
from src.broker.fetcher import PagedOptionChainFetcher
from src.core.config import Settings, StorageSettings
from src.main import create_app
from src.storage.csv_repository import CSVRepository
from src.web.api import api_corridor_stream
from tests.support.aio import noop_sleep, run_coro
from tests.support.ib_fakes import FakeEvent


class _MockCorridorIB:
    """Mock `ib_async.IB` for Wall Corridor REST & SSE integration tests."""

    def __init__(self, *, offline: bool = False) -> None:
        self.offline = offline
        self._connected = False
        self.errorEvent = FakeEvent()
        self.wrapper = SimpleNamespace(clientId=0)
        self.market_data_types: list[int] = []
        self.qualify_calls = 0
        self.subscribed: list[Any] = []
        self.cancelled: list[Any] = []
        self.tickers_by_conid: dict[int, Any] = {}

        async def _connect_async(host: str, port: int, client_id: int, timeout_sec: float) -> None:
            if self.offline:
                raise ConnectionRefusedError("port 4001 closed")
            self._connected = True

        self.client = SimpleNamespace(connectAsync=_connect_async)

    def isConnected(self) -> bool:  # noqa: N802
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def reqMarketDataType(self, data_type: int) -> None:  # noqa: N802
        self.market_data_types.append(data_type)

    async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]:  # noqa: N802
        self.qualify_calls += 1
        return list(contracts)

    def reqMktData(  # noqa: N802
        self,
        contract: Any,
        genericTickList: str = "",  # noqa: N803
        snapshot: bool = False,
        regulatorySnapshot: bool = False,  # noqa: N803
    ) -> Any:
        self.subscribed.append(contract)
        con_id = int(getattr(contract, "conId", 0) or 0)
        if getattr(contract, "secType", "") == "STK":
            tk = SimpleNamespace(
                contract=contract,
                marketPrice=lambda: 120.0,
                last=120.0,
                close=119.5,
                bid=119.9,
                ask=120.1,
                delayedLast=120.0,
                delayedClose=119.5,
                delayedBid=119.9,
                delayedAsk=120.1,
            )
        else:
            # Slightly richer live quotes than seeded CSV so overlay price impact is measurable.
            tk = SimpleNamespace(
                contract=contract,
                bid=6.20,
                ask=6.60,
                last=6.40,
                close=6.30,
                volume=1850,
                callOpenInterest=2400,
                putOpenInterest=2300,
                modelGreeks=None,
            )
        self.tickers_by_conid[con_id] = tk
        return tk

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        self.cancelled.append(contract)


def _seed_chain_rows(ts: datetime) -> list[dict[str, Any]]:
    ts_str = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    market_date = ts.strftime("%Y-%m-%d")
    snap_id = "NVDA_20261111_150000"
    spot = 120.0
    strikes = [95.0, 100.0, 105.0, 110.0, 115.0, 120.0, 125.0, 130.0, 135.0, 140.0]
    expiries = [("2026-12-19", 38), ("2027-01-16", 66)]
    rows: list[dict[str, Any]] = []
    conid = 800_000
    for expiry, dte in expiries:
        for strike in strikes:
            for right in ("C", "P"):
                conid += 1
                is_put_wall = right == "P" and math.isclose(strike, 110.0)
                is_call_wall = right == "C" and math.isclose(strike, 130.0)
                oi = 24_000 if (is_put_wall or is_call_wall) else 500
                vol = 5_000 if (is_put_wall or is_call_wall) else 150
                rows.append(
                    {
                        "snapshot_id": snap_id,
                        "snapshot_ts_utc": ts_str,
                        "market_date": market_date,
                        "market_data_type": 3,
                        "underlying": "NVDA",
                        "underlying_conid": 4815,
                        "underlying_price": spot,
                        "option_conid": conid,
                        "expiry": expiry,
                        "dte": dte,
                        "strike": strike,
                        "moneyness_pct": ((strike - spot) / spot) * 100.0,
                        "right": right,
                        "bid": 2.10,
                        "ask": 2.30,
                        "mid": 2.20,
                        "last": 2.20,
                        "close": 2.15,
                        "mark_price": 2.20,
                        "volume": vol,
                        "open_interest": oi,
                        "iv": 0.42,
                        "delta": 0.35 if right == "C" else -0.35,
                        "gamma": 0.02,
                        "theta": -0.05,
                        "vega": 0.14,
                        "greeks_source": "BSM_FALLBACK",
                    }
                )
    return rows


def _build_corridor_test_app(
    tmp_path: Path, *, offline: bool = False
) -> tuple[Any, CSVRepository, _MockCorridorIB]:
    settings = Settings(storage=StorageSettings())
    repo = CSVRepository(settings.storage, root=tmp_path)
    ts = datetime(2026, 11, 11, 15, 0, 0, tzinfo=UTC)
    repo.save_snapshot(
        "NVDA",
        _seed_chain_rows(ts),
        range_pct=0.25,
        strike_step=5.0,
        requested_strike_step=5.0,
    )
    mock_ib = _MockCorridorIB(offline=offline)
    client = IBKRBrokerClient(settings, ib_factory=lambda: mock_ib)
    fetcher = PagedOptionChainFetcher(client, settings, clock=lambda: ts, sleep=noop_sleep)
    app = create_app(settings=settings, repo=repo, broker_client=client, fetcher=fetcher)
    app.state.service.corridor.sleep = noop_sleep
    return app, repo, mock_ib


def _parse_sse_block(raw_event: str) -> tuple[str, dict[str, Any]]:
    event_name = "message"
    data_lines: list[str] = []
    for line in raw_event.strip().splitlines():
        if line.startswith("event:"):
            event_name = line.partition(":")[2].strip()
        elif line.startswith("data:"):
            data_lines.append(line.partition(":")[2].strip())
    return event_name, dict(json.loads("\n".join(data_lines)))


def test_corridor_offline_and_validation_errors(tmp_path: Path) -> None:
    app, _repo, _mock_ib = _build_corridor_test_app(tmp_path, offline=True)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r_bad = await client.post("/api/corridor/quotes", json={"ticker": "bad!"})
                assert r_bad.status_code == 422
                assert r_bad.json()["error_code"] == "INVALID_TICKER"

                r_over = await client.post(
                    "/api/corridor/quotes", json={"ticker": "NVDA", "max_contracts": 61}
                )
                assert r_over.status_code == 422

                r_off = await client.post("/api/corridor/quotes", json={"ticker": "NVDA"})
                assert r_off.status_code == 503
                assert r_off.json()["error_code"] == "GATEWAY_OFFLINE"

                r_stream_bad = await client.get("/api/corridor/stream/bad!")
                assert r_stream_bad.status_code == 422

                r_stream_over = await client.get("/api/corridor/stream/NVDA?max_contracts=61")
                assert r_stream_over.status_code == 422

                r_stream_off = await client.get("/api/corridor/stream/NVDA")
                assert r_stream_off.status_code == 503
                assert r_stream_off.json()["error_code"] == "GATEWAY_OFFLINE"

    run_coro(_exercise())


def test_corridor_quotes_overlay_preserves_csv_and_streams_cleanly(tmp_path: Path) -> None:
    app, _repo, mock_ib = _build_corridor_test_app(tmp_path, offline=False)
    latest_csv_path = tmp_path / "data" / "latest" / "NVDA_latest.csv"
    csv_before = latest_csv_path.read_bytes()

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                # Baseline Strategy evaluation from cached CSV
                r_strat_before = await client.post(
                    "/api/strategy/evaluate",
                    json={
                        "ticker": "NVDA",
                        "strategy_kind": "BULL_PUT_SPREAD",
                        "target_expiry": "2026-12-19",
                    },
                )
                assert r_strat_before.status_code == 200
                mid_before = r_strat_before.json()["pricing"]["mid_cash_total"]

                # One-shot corridor refresh reusing cached conIds + 100% cancelMktData cleanup
                r_corridor = await client.post(
                    "/api/corridor/quotes",
                    json={
                        "ticker": "NVDA",
                        "target_expiry": "2026-12-19",
                        "window_strikes": 3,
                        "max_contracts": 40,
                        "priority_legs": [
                            {"expiry": "2026-12-19", "strike": 110.0, "right": "P"},
                            {"expiry": "2026-12-19", "strike": 105.0, "right": "P"},
                        ],
                    },
                )
                assert r_corridor.status_code == 200
                payload = r_corridor.json()
                assert payload["ticker"] == "NVDA"
                assert payload["mode"] == "ONE_SHOT"
                assert 1 <= payload["subscribed_count"] <= 40 <= CORRIDOR_MAX_CONTRACTS
                assert payload["subscription_cap"] == CORRIDOR_MAX_CONTRACTS
                assert mock_ib.qualify_calls == 0
                assert len(mock_ib.cancelled) == payload["subscribed_count"]

                # Distinguish live bid/ask between short & long legs so re-priced mid changes
                # Even equal quotes at 6.2/6.6 change bid/ask per leg (`2.1/2.3` -> `6.2/6.6`)
                r_strat_after = await client.post(
                    "/api/strategy/evaluate",
                    json={
                        "ticker": "NVDA",
                        "strategy_kind": "BULL_PUT_SPREAD",
                        "target_expiry": "2026-12-19",
                    },
                )
                assert r_strat_after.status_code == 200
                strat_after = r_strat_after.json()
                assert strat_after["legs"][0]["bid"] == pytest.approx(6.20)
                assert strat_after["legs"][0]["ask"] == pytest.approx(6.60)
                # Natural pricing worsens when spread width per leg widens (0.20 -> 0.40)
                assert (
                    strat_after["pricing"]["slippage_dollars"]
                    > (r_strat_before.json()["pricing"]["slippage_dollars"])
                )
                assert mid_before is not None

                # Mode 1 CSV file on disk MUST remain immutable / byte-identical
                assert latest_csv_path.read_bytes() == csv_before

                # Exercise SSE Streaming route directly with fake Request (`_run` safe on Windows)
                mock_ib.subscribed.clear()
                mock_ib.cancelled.clear()

                class _FakeRequest:
                    async def is_disconnected(self) -> bool:
                        return False

                resp = await api_corridor_stream(
                    request=_FakeRequest(),  # type: ignore[arg-type]
                    ticker="NVDA",
                    snapshot_id=None,
                    target_expiry="2026-12-19",
                    window_strikes=2,
                    max_contracts=30,
                    priority_legs="2026-12-19|110|P,2026-12-19|105|P",
                    max_frames=2,
                    svc=app.state.service,
                )
                assert isinstance(resp, StreamingResponse)
                assert resp.media_type == "text/event-stream"

                chunks: list[str] = []
                async for raw_chunk in resp.body_iterator:
                    text_chunk = (
                        raw_chunk.decode("utf-8")
                        if isinstance(raw_chunk, bytes)
                        else str(raw_chunk)
                    )
                    chunks.append(text_chunk)
                    if len(chunks) == 1:
                        # Bump tick quote so frame 2 emits with changed_keys
                        ev1, d1 = _parse_sse_block(text_chunk)
                        assert ev1 == "frame"
                        opt_cid = d1["quotes"][0]["option_conid"]
                        mock_ib.tickers_by_conid[opt_cid].bid = 7.10
                        mock_ib.tickers_by_conid[opt_cid].ask = 7.50

                assert len(chunks) == 2
                ev_name_2, frame2_raw = _parse_sse_block(chunks[1])
                assert ev_name_2 == "frame"
                frame2 = CorridorQuoteFrame.model_validate(frame2_raw)
                assert frame2.sequence == 2
                assert frame2.mode == "STREAM"
                assert frame2.quotes[0].bid == pytest.approx(7.10)
                assert len(mock_ib.cancelled) == frame2.subscribed_count

                # Mid-stream Gateway disconnect yields SSE `error` (GATEWAY_OFFLINE) + 100% cleanup
                mock_ib.subscribed.clear()
                mock_ib.cancelled.clear()
                resp_disc = await api_corridor_stream(
                    request=_FakeRequest(),  # type: ignore[arg-type]
                    ticker="NVDA",
                    snapshot_id=None,
                    target_expiry="2026-12-19",
                    window_strikes=2,
                    max_contracts=30,
                    priority_legs=None,
                    max_frames=None,
                    svc=app.state.service,
                )
                disc_events: list[tuple[str, dict[str, Any]]] = []
                async for raw_chunk in resp_disc.body_iterator:
                    text_chunk = (
                        raw_chunk.decode("utf-8")
                        if isinstance(raw_chunk, bytes)
                        else str(raw_chunk)
                    )
                    ev_name, data = _parse_sse_block(text_chunk)
                    disc_events.append((ev_name, data))
                    if ev_name == "frame":
                        mock_ib.disconnect()

                assert [ev for ev, _ in disc_events] == ["frame", "error"]
                assert disc_events[1][1]["error_code"] == "GATEWAY_OFFLINE"
                assert disc_events[1][1]["status"] == 503
                assert len(mock_ib.cancelled) == disc_events[0][1]["subscribed_count"]

                # Stop endpoint cleanly returns active_streams == 0
                r_stop = await client.post("/api/corridor/stop")
                assert r_stop.status_code == 200
                assert r_stop.json()["active_streams"] == 0

    run_coro(_exercise())
    assert mock_ib.isConnected() is False
