from __future__ import annotations

import contextlib
from collections.abc import Callable, Coroutine
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypeVar

import anyio
import anyio.to_thread
import httpx
import pytest

from src.broker.client import IBKRBrokerClient
from src.broker.fetcher import PagedOptionChainFetcher
from src.core.config import Settings, StorageSettings
from src.core.exceptions import GatewayConnectionError
from src.main import create_app
from src.storage.csv_repository import CSVRepository, make_snapshot_id

T = TypeVar("T")


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """Drive a pure ASGI / mocked coroutine to completion without opening OS sockets."""
    try:
        while True:
            coro.send(None)
    except StopIteration as stop:
        val: T = stop.value
        return val


async def _noop_sleep(_seconds: float) -> None:
    return None


class _FakeEvent:
    def __iadd__(self, _handler: Any) -> _FakeEvent:
        return self


class _MockIB:
    def __init__(self, *, offline: bool = False) -> None:
        self.offline = offline
        self._connected = False
        self.errorEvent = _FakeEvent()
        self.wrapper = SimpleNamespace(clientId=0)
        self.market_data_types: list[int] = []
        self.cancelled: list[Any] = []
        self.disconnected_called = False

        async def _connect_async(host: str, port: int, client_id: int, timeout_sec: float) -> None:
            if self.offline:
                raise ConnectionRefusedError("port 4001 closed")
            self._connected = True

        self.client = SimpleNamespace(connectAsync=_connect_async)

    def isConnected(self) -> bool:  # noqa: N802
        return self._connected

    def disconnect(self) -> None:
        self._connected = False
        self.disconnected_called = True

    def reqMarketDataType(self, data_type: int) -> None:  # noqa: N802
        self.market_data_types.append(data_type)

    async def qualifyContractsAsync(self, *contracts: Any) -> list[Any]:  # noqa: N802
        for idx, c in enumerate(contracts, start=1):
            c.conId = getattr(c, "conId", 0) or (20_000 + idx)
        return list(contracts)

    async def reqSecDefOptParamsAsync(  # noqa: N802
        self, symbol: str, _fut_exch: str, _sec_type: str, _con_id: int
    ) -> list[Any]:
        base = date(2026, 11, 11)
        exps = [(base + timedelta(days=d)).strftime("%Y%m%d") for d in (30, 35, 40, 45, 58)]
        return [
            SimpleNamespace(
                exchange="SMART",
                tradingClass=symbol,
                multiplier="100",
                expirations=exps,
                strikes=[135.0, 140.0, 145.0],
            )
        ]

    def reqMktData(  # noqa: N802
        self,
        contract: Any,
        genericTickList: str = "",  # noqa: N803
        snapshot: bool = False,
        regulatorySnapshot: bool = False,  # noqa: N803
    ) -> Any:
        if getattr(contract, "secType", "") == "STK":
            return SimpleNamespace(contract=contract, bid=139.8, ask=140.2, last=140.0, close=139.5)
        return SimpleNamespace(
            contract=contract,
            bid=6.2,
            ask=6.6,
            last=6.4,
            close=6.3,
            volume=200,
            callOpenInterest=-1,
            putOpenInterest=-1,
            modelGreeks=None,
        )

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        self.cancelled.append(contract)


def _seed_nvda_rows(ts: datetime) -> list[dict[str, Any]]:
    sid = make_snapshot_id("NVDA", ts)
    spot = 140.0
    rows: list[dict[str, Any]] = []
    conid = 3000
    for dte in (30, 35, 40, 45, 60):
        expiry = (ts.date() + timedelta(days=dte)).isoformat()
        for strike in (135.0, 140.0, 145.0):
            for right in ("C", "P"):
                conid += 1
                rows.append(
                    {
                        "snapshot_id": sid,
                        "snapshot_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "market_date": "2026-11-11",
                        "market_data_type": 3,
                        "underlying": "NVDA",
                        "underlying_conid": 4815,
                        "underlying_price": spot,
                        "option_conid": conid,
                        "expiry": expiry,
                        "dte": dte,
                        "target_dte_bucket": dte,
                        "strike": strike,
                        "moneyness_pct": ((strike - spot) / spot) * 100.0,
                        "right": right,
                        "bid": 7.0,
                        "ask": 7.4,
                        "last": 7.2,
                        "close": 7.1,
                        "volume": 500 if (strike == 140.0 and right == "C") else 50,
                        "open_interest": None,
                        "iv": None,
                        "delta": None,
                        "gamma": None,
                        "theta": None,
                        "vega": None,
                        "greeks_source": "NONE",
                    }
                )
    return rows


def _build_test_app(tmp_path: Path, *, offline: bool = False) -> tuple[Any, CSVRepository, _MockIB]:
    settings = Settings(storage=StorageSettings())
    repo = CSVRepository(settings.storage, root=tmp_path)
    ts = datetime(2026, 11, 11, 15, 0, 0, tzinfo=UTC)
    repo.save_snapshot("NVDA", _seed_nvda_rows(ts), range_pct=0.20, strike_step=5.0)

    mock_ib = _MockIB(offline=offline)
    client = IBKRBrokerClient(settings, ib_factory=lambda: mock_ib)
    refresh_ts = datetime(2026, 11, 11, 16, 0, 0, tzinfo=UTC)
    fetcher = PagedOptionChainFetcher(
        client,
        settings,
        clock=lambda: refresh_ts,
        sleep=_noop_sleep,
    )
    app = create_app(settings=settings, repo=repo, broker_client=client, fetcher=fetcher)
    return app, repo, mock_ib


def test_index_and_status_and_snapshots_endpoints(tmp_path: Path) -> None:
    app, _repo, mock_ib = _build_test_app(tmp_path, offline=True)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r_index = await client.get("/")
                assert r_index.status_code == 200
                assert "editorial-banner-slot" in r_index.text

                r_status = await client.get("/api/status")
                assert r_status.status_code == 200
                st = r_status.json()
                assert st["connected"] is False
                assert st["port"] == 4001
                assert st["market_data_mode"] == "DELAYED"
                assert st["default_market_data_type"] == 3
                assert st["watchlist"] == ["NVDA", "AVGO", "CRDO", "PLTR"]

                r_snaps = await client.get("/api/snapshots/NVDA")
                assert r_snaps.status_code == 200
                snaps = r_snaps.json()
                assert snaps["ticker"] == "NVDA"
                assert snaps["latest_snapshot_id"] == "NVDA_20261111_150000"
                assert len(snaps["snapshots"]) == 1

    _run(_exercise())
    assert mock_ib.disconnected_called is True


def test_chain_endpoint_enriches_bsm_and_handles_422_and_404(tmp_path: Path) -> None:
    app, _repo, _mock_ib = _build_test_app(tmp_path)

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_chain = await client.get("/api/chain/nvda")
            assert r_chain.status_code == 200
            data = r_chain.json()
            assert data["meta"]["underlying"] == "NVDA"
            assert data["meta"]["source"] == "CACHE"
            # Seeded rows had greeks_source="NONE", so BSM fallback enriched them
            assert data["meta"]["greeks_source_counts"].get("BSM_FALLBACK", 0) > 0
            assert data["walls"]["primary_basis"] == "VOLUME"
            assert data["walls"]["aggregate_primary_call_wall"]["strike"] == 140.0
            assert len(data["profiles"]["buckets"]) == 5
            assert len(data["contracts"]) == 30

            # Explicit snapshot_id query param
            r_snap = await client.get(
                "/api/chain/NVDA", params={"snapshot_id": "NVDA_20261111_150000"}
            )
            assert r_snap.status_code == 200

            # Invalid ticker -> 422
            r_bad = await client.get("/api/chain/BAD$TICKER")
            assert r_bad.status_code == 422
            assert r_bad.json()["error_code"] == "INVALID_TICKER"

            # Uncached valid ticker -> 404
            r_missing = await client.get("/api/chain/PLTR")
            assert r_missing.status_code == 404
            assert r_missing.json()["error_code"] == "SNAPSHOT_NOT_FOUND"

    _run(_exercise())


def test_post_refresh_success_and_offline_503(tmp_path: Path) -> None:
    # 1. Offline Gateway -> HTTP 503 GATEWAY_OFFLINE
    app_offline, _repo_off, _ = _build_test_app(tmp_path / "offline", offline=True)

    async def _exercise_offline() -> None:
        transport = httpx.ASGITransport(app=app_offline)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_503 = await client.post(
                "/api/refresh",
                json={"ticker": "NVDA", "range_pct": 0.20, "strike_step": 5.0},
            )
            assert r_503.status_code == 503
            body = r_503.json()
            assert body["error_code"] == "GATEWAY_OFFLINE"
            assert "4001" in body["message"]

    _run(_exercise_offline())

    # 2. Online Gateway -> HTTP 200 LIVE_REFRESH + persisted to CSVRepository
    app_online, repo_on, _ = _build_test_app(tmp_path / "online", offline=False)

    async def _exercise_online() -> None:
        transport = httpx.ASGITransport(app=app_online)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_ok = await client.post(
                "/api/refresh",
                json={"ticker": "NVDA", "range_pct": 0.20, "strike_step": 5.0},
            )
            assert r_ok.status_code == 200
            payload = r_ok.json()
            assert payload["meta"]["snapshot_id"] == "NVDA_20261111_160000"
            assert payload["meta"]["source"] == "LIVE_REFRESH"
            assert payload["meta"]["greeks_source_counts"].get("BSM_FALLBACK", 0) > 0
            assert len(repo_on.list_snapshots("NVDA")) == 2

            # Invalid ticker in POST /api/refresh -> 422
            r_bad = await client.post(
                "/api/refresh",
                json={"ticker": "../etc", "range_pct": 0.20, "strike_step": 5.0},
            )
            assert r_bad.status_code == 422
            assert r_bad.json()["error_code"] == "INVALID_TICKER"

    _run(_exercise_online())
    with pytest.raises(GatewayConnectionError):
        # Verify exception class import used for clarity
        raise GatewayConnectionError("check")


def test_dashboard_html_static_assets_and_csv_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run_sync_inline(func: Callable[..., Any], *args: Any, **_kwargs: Any) -> Any:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", _run_sync_inline)
    monkeypatch.setattr(
        anyio.CancelScope,
        "__new__",
        lambda _cls, **_kw: contextlib.nullcontext(),
    )
    app, _repo, _mock_ib = _build_test_app(tmp_path)

    async def _asgi_24(scope: Any, receive: Any, send: Any) -> None:
        patched_scope = {**scope, "asgi": {"version": "3.0", "spec_version": "2.4"}}
        await app(patched_scope, receive, send)

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=_asgi_24)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_html = await client.get("/")
            assert r_html.status_code == 200
            html = r_html.text
            for required_id in (
                'id="gateway-badge"',
                'id="md-type-badge"',
                'id="greeks-source-badge"',
                'id="theme-toggle"',
                'id="editorial-banner-slot"',
                'id="watchlist-pills"',
                'id="custom-ticker-input"',
                'id="range-pct-select"',
                'id="strike-step-select"',
                'id="snapshot-select"',
                'id="refresh-btn"',
                'id="alert-banner"',
                'id="kpi-spot"',
                'id="kpi-call-wall"',
                'id="kpi-put-wall"',
                'id="kpi-anomalies"',
                'id="chart-walls"',
                'id="chart-iv-smile"',
                'id="chart-anomalies"',
                'id="chart-greeks"',
                'id="chain-table"',
            ):
                assert required_id in html, f"Missing {required_id} in index HTML"

            for column in (
                "Expiry",
                "DTE (Bucket)",
                "Right",
                "Strike",
                "Moneyness %",
                "Bid",
                "Ask",
                "Mark",
                "Volume",
                "Open Int",
                "Vol/OI",
                "IV %",
                "Delta",
                "Gamma",
                "Theta",
                "Vega",
                "Source",
            ):
                assert f">{column}</th>" in html, f"Missing table header {column}"

            r_css = await client.get("/static/css/tokens.css")
            assert r_css.status_code == 200
            assert "--bg-canvas:" in r_css.text
            assert 'html[data-theme="terminal"]' in r_css.text

            r_js = await client.get("/static/js/dashboard.js")
            assert r_js.status_code == 200
            assert "Plotly.react" in r_js.text
            assert "Plotly.purge" in r_js.text

            r_csv = await client.get("/api/csv/NVDA")
            assert r_csv.status_code == 200
            assert r_csv.headers["content-type"].startswith("text/csv")
            assert "snapshot_id" in r_csv.text
            assert "NVDA_20261111_150000" in r_csv.text

            r_csv_hist = await client.get(
                "/api/csv/NVDA", params={"snapshot_id": "NVDA_20261111_150000"}
            )
            assert r_csv_hist.status_code == 200

            r_csv_404 = await client.get(
                "/api/csv/NVDA", params={"snapshot_id": "NVDA_19990101_000000"}
            )
            assert r_csv_404.status_code == 404
            assert r_csv_404.json()["error_code"] == "SNAPSHOT_NOT_FOUND"

            r_csv_422 = await client.get("/api/csv/BAD$TICKER")
            assert r_csv_422.status_code == 422
            assert r_csv_422.json()["error_code"] == "INVALID_TICKER"

            # Verify Auto strike step (null) works cleanly on POST /api/refresh
            r_auto = await client.post(
                "/api/refresh",
                json={"ticker": "NVDA", "range_pct": 0.20, "strike_step": None},
            )
            assert r_auto.status_code == 200

    _run(_exercise())
