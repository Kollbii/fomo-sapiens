from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import anyio.to_thread
import httpx
import pytest

from src.broker.client import IBKRBrokerClient
from src.broker.fetcher import PagedOptionChainFetcher
from src.core.config import IBKRSettings, Settings, StorageSettings
from src.core.exceptions import GatewayConnectionError
from src.main import create_app
from src.storage.csv_repository import CSVRepository, make_snapshot_id
from tests.support.aio import noop_sleep, run_coro
from tests.support.ib_fakes import FakeEvent


class _MockIB:
    def __init__(self, *, offline: bool = False) -> None:
        self.offline = offline
        self._connected = False
        self.errorEvent = FakeEvent()
        self.wrapper = SimpleNamespace(clientId=0)
        self.market_data_types: list[int] = []
        self.cancelled: list[Any] = []
        self.disconnected_called = False

        async def _connect_async(host: str, port: int, client_id: int, timeout_sec: float) -> None:
            if self.offline:
                raise ConnectionRefusedError(f"port {port} closed")
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
        # 30 is included, 60 is excluded ([30, 60)); 35 yields to Friday 37 -> 5 weekly expiries
        exps = [(base + timedelta(days=d)).strftime("%Y%m%d") for d in (30, 35, 37, 43, 50, 58, 60)]
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
    for dte in (37, 43, 50, 58):
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
    repo.save_snapshot(
        "NVDA",
        _seed_nvda_rows(ts),
        range_pct=0.20,
        strike_step=5.0,
        requested_strike_step=5.0,
    )

    mock_ib = _MockIB(offline=offline)
    client = IBKRBrokerClient(settings, ib_factory=lambda: mock_ib)
    refresh_ts = datetime(2026, 11, 11, 16, 0, 0, tzinfo=UTC)
    fetcher = PagedOptionChainFetcher(
        client,
        settings,
        clock=lambda: refresh_ts,
        sleep=noop_sleep,
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
                assert st["active_market_data_type"] == 3
                assert st["watchlist"] == ["NVDA", "AVGO", "CRDO", "PLTR"]
                assert st["dte_min"] == 30
                assert st["dte_max"] == 120
                assert st["default_strike_step"] is None
                assert st["available_strike_steps"] == st["allowed_strike_steps"]
                assert st["available_range_pcts"] == st["allowed_range_pcts"]
                assert st["active_capabilities_ticker"] is None

                mock_ib.client.marketDataType = 1
                r_status_live = await client.get("/api/status")
                assert r_status_live.status_code == 200
                assert r_status_live.json()["active_market_data_type"] == 1
                delattr(mock_ib.client, "marketDataType")

                r_snaps = await client.get("/api/snapshots/NVDA")
                assert r_snaps.status_code == 200
                snaps = r_snaps.json()
                assert snaps["ticker"] == "NVDA"
                assert snaps["latest_snapshot_id"] == "NVDA_20261111_150000"
                assert len(snaps["snapshots"]) == 1

    run_coro(_exercise())
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
            assert data["meta"]["range_pct"] == pytest.approx(0.20)
            assert data["meta"]["strike_step"] == pytest.approx(5.0)
            assert data["meta"]["requested_strike_step"] == pytest.approx(5.0)
            assert isinstance(data["meta"]["available_strike_steps"], list)
            assert isinstance(data["meta"]["available_range_pcts"], list)
            assert data["meta"]["dtes"] == [37, 43, 50, 58]
            # Seeded rows had greeks_source="NONE", so BSM fallback enriched them
            assert data["meta"]["greeks_source_counts"].get("BSM_FALLBACK", 0) > 0
            assert data["walls"]["primary_basis"] == "VOLUME"
            assert data["walls"]["aggregate_primary_call_wall"]["strike"] == 140.0
            assert len(data["profiles"]["expiries"]) == 4
            assert len(data["contracts"]) == 24

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

    run_coro(_exercise())


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

    run_coro(_exercise_offline())

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
            assert payload["meta"]["range_pct"] == pytest.approx(0.20)
            assert payload["meta"]["strike_step"] == pytest.approx(5.0)
            assert payload["meta"]["exchange_native_verified"] is True
            assert payload["meta"]["exchange_validated_strike_count"] >= 3
            assert 5.0 in payload["meta"]["available_strike_steps"]
            assert len(payload["meta"]["available_range_pcts"]) >= 1
            assert payload["meta"]["dtes"] == [30, 37, 43, 50, 58]
            assert payload["meta"]["greeks_source_counts"].get("BSM_FALLBACK", 0) > 0
            assert len(repo_on.list_snapshots("NVDA")) == 2

            r_status_nvda = await client.get("/api/status", params={"ticker": "NVDA"})
            assert r_status_nvda.status_code == 200
            st_nvda = r_status_nvda.json()
            assert st_nvda["active_capabilities_ticker"] == "NVDA"
            assert st_nvda["available_strike_steps"] == payload["meta"]["available_strike_steps"]
            assert st_nvda["available_range_pcts"] == payload["meta"]["available_range_pcts"]

            r_status_avgo = await client.get("/api/status", params={"ticker": "AVGO"})
            assert r_status_avgo.status_code == 200
            assert r_status_avgo.json()["active_capabilities_ticker"] is None

            r_chain_nvda = await client.get("/api/chain/NVDA")
            assert r_chain_nvda.status_code == 200
            assert r_chain_nvda.json()["meta"]["exchange_native_verified"] is True
            assert (
                r_chain_nvda.json()["meta"]["exchange_validated_strike_count"]
                == payload["meta"]["exchange_validated_strike_count"]
            )

            # Invalid ticker in POST /api/refresh -> 422
            r_bad = await client.post(
                "/api/refresh",
                json={"ticker": "../etc", "range_pct": 0.20, "strike_step": 5.0},
            )
            assert r_bad.status_code == 422
            assert r_bad.json()["error_code"] == "INVALID_TICKER"

    run_coro(_exercise_online())
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
                'id="walls-basis-label"',
                'id="walls-view-select"',
                'id="walls-metric-select"',
                'id="walls-dte-select"',
                'id="walls-split-dte"',
                'id="walls-show-anomalies"',
                'id="walls-copy-ai-btn"',
                'id="panel-visibility-controls"',
                'id="panel-visibility-reset"',
                'data-panel-target="walls"',
                'data-panel-target="iv_smile"',
                'data-panel-target="anomalies"',
                'data-panel-target="greeks"',
                'class="panel-card panel-card--walls" data-panel-id="walls"',
                'class="panel-card is-hidden" data-panel-id="iv_smile"',
                'class="panel-card" data-panel-id="anomalies"',
                'class="panel-card is-hidden" data-panel-id="greeks"',
                'id="chart-walls"',
                'id="chart-iv-smile"',
                'id="chart-anomalies"',
                'id="chart-greeks"',
                'id="chain-table"',
                'id="strategy-auto-populate"',
                'class="chain-leg-col">Leg</th>',
            ):
                assert required_id in html, f"Missing {required_id} in index HTML"
            assert 'colspan="18"' in html
            assert 'id="alert-banner"' in html and 'aria-live="polite"' in html

            for col_key, col_label in (
                ("expiry", "Expiry"),
                ("dte", "DTE"),
                ("right", "Right"),
                ("strike", "Strike"),
                ("moneyness_pct", "Moneyness %"),
                ("bid", "Bid"),
                ("ask", "Ask"),
                ("mark_price", "Mark"),
                ("volume", "Volume"),
                ("open_interest", "Open Int"),
                ("vol_oi", "Vol/OI"),
                ("iv", "IV %"),
                ("delta", "Delta"),
                ("gamma", "Gamma"),
                ("theta", "Theta"),
                ("vega", "Vega"),
                ("greeks_source", "Source"),
            ):
                assert f'data-sort-col="{col_key}">{col_label}<span class="th-sort-ind"' in html, (
                    f"Missing sortable table header {col_label}"
                )
                assert f'data-filter-col="{col_key}"' in html, f"Missing header filter {col_key}"
            assert 'id="table-reset-filters-btn"' in html
            assert 'class="table-filter-row"' in html
            assert 'id="th-filter-expiry"' in html
            assert 'id="th-filter-right"' in html
            assert 'id="th-filter-source"' in html

            r_css = await client.get("/static/css/tokens.css")
            assert r_css.status_code == 200
            assert "--bg-canvas:" in r_css.text
            assert 'html[data-theme="terminal"]' in r_css.text
            assert "panel-card--span-6" in r_css.text
            assert "panel-resize-handle" in r_css.text
            assert "nwse-resize" in r_css.text
            assert ".panel-card.is-hidden" in r_css.text
            assert "panel-toggle-pill" in r_css.text
            assert ".badge--live" in r_css.text
            assert ".badge--amber" in r_css.text
            assert ".badge--delayed" in r_css.text
            assert ".th-sort-btn" in r_css.text
            assert ".table-filter-row" in r_css.text
            assert ".th-filter-input.is-filtered" in r_css.text
            assert ".alert-banner.is-fresh" in r_css.text
            assert ".alert-banner-close" in r_css.text
            assert ".chain-add-leg-btn" in r_css.text
            assert 'href="/static/css/tokens.css?v=20261111i"' in html

            r_js = await client.get("/static/js/dashboard.js")
            assert r_js.status_code == 200
            assert "Plotly.react" in r_js.text
            assert "Plotly.purge" in r_js.text
            assert "DEFAULT_PANEL_VISIBILITY" in r_js.text
            assert "ibkr_panel_visibility_v1" in r_js.text
            assert "applyPanelVisibility" in r_js.text
            assert "TYPE 1: LIVE OPRA" in r_js.text
            assert "TYPE 2: FROZEN" in r_js.text
            assert "TYPE 3: 15M DELAYED" in r_js.text
            assert "TYPE 4: DELAYED-FROZEN" in r_js.text
            assert "applyMdTypeBadge" in r_js.text
            assert "30&ndash;120 DTE WEEKLY EXPIRATIONS" in html
            assert '<option value="auto" selected>Auto</option>' in html
            assert 'id="dte-max-select"' in html
            assert 'id="dte-bucket-tabs"' in html
            assert 'data-dte-bucket="BUCKET:61-90"' in html
            assert 'data-dte-bucket="BUCKET:91-120"' in html
            assert "30&ndash;59 DTE" not in html
            assert "&gt;30D" not in html
            assert "CHECKING FEED..." in html
            assert "&bull;" not in r_js.text
            assert "&mdash;" not in r_js.text
            assert "var minStep = 5;" not in r_js.text
            assert "delBtn.disabled = state.strategyLegs.length <= 1" not in r_js.text
            assert "if (state.strategyLegs.length <= 1) return" not in r_js.text
            assert 'src="/static/js/dashboard.js?v=20261111q"' in html
            assert "Delayed Type 3/4 Feed" not in r_js.text
            assert "Port 4001" not in r_js.text
            for js_hook in (
                "populateWallsDteFilter",
                "makeWallReferenceMarker",
                "buildSymmetricPositiveTicks",
                "computeStrikeAxisGeometry",
                "localStepByKey",
                "stepBadgeText",
                "resolveWallCandidate",
                "buildWallsAiPrompt",
                "copyWallsAiPrompt",
                "Top 3 strategie opcyjne",
                "BEZ ŚCIANY TEKSTU",
                "snapCardToGrid",
                "panel-resize-handle",
                "initChartResizeHandling",
                "ibkr_walls_view",
                "ibkr_walls_show_anomalies",
                "walls-dte-select",
                "dte-max-select",
                "dte-bucket-tabs",
                "matchesDteFilterValue",
                "syncDteBucketTabs",
                "BUCKET:91-120",
                "syncControlCapabilities",
                "resetControlCapabilities",
                "exchange_validated_strike_count",
                "exchange_native_verified",
                "renderStrategyPayoffChart",
                "autoBuildStrategy",
                "evaluateStrategyRequest",
                "syncStrategySelectors",
                "extractApiErrorMessage",
                "resolveLocalContractMid",
                "findExactEvaluatedLeg",
                "snapLegToValidContract",
                "clearAlertBanner",
                "textSnippet",
                "timeoutMs",
                "elapsedMs",
                "addSpecificStrategyLeg",
                "maybeAutoPopulateStrategy",
                "snapDteValueToExpiries",
                "applyTickerPullPrefsToControls",
                "ibkr_strategy_auto_populate_v1",
                "ibkr_pull_prefs_v1",
                "UI_RENDER",
                "REFRESH_HTTP",
                "chain-add-leg-btn",
                "alert-banner-close",
                "markStrategyEvaluationDirty",
                "syncStrategyActionButtons",
                "strategyPendingEvaluate",
                "configuredDefaultMdType",
                "gatewayAddressLabel",
                "configuredFeedBannerSummary",
                "strategy: true",
                "corridor: true",
                "startCorridorStream",
                "stopCorridorStream",
                "refreshCorridorQuotesOneShot",
                "refreshStrategyLegsQuotesOnly",
                "renderCorridorChart",
                "TABLE_COLUMNS",
                "matchesNumericFilterExpr",
                "applyTableSort",
                "populateTableHeaderFilters",
                "resetAllTableFilters",
                "ibkr_chain_table_sort_v1",
            ):
                assert js_hook in r_js.text, f"Missing {js_hook} in dashboard.js"

            assert 'data-panel-target="strategy"' in html
            assert 'data-panel-target="corridor"' in html
            assert "panel-toggle-btn" not in html
            assert html.count('class="panel-toggle-pill') == 6
            assert 'data-panel-id="corridor"' in html
            assert 'id="wall-corridor-card"' in html
            assert 'id="corridor-refresh-btn"' in html
            assert 'id="corridor-stream-toggle"' in html
            assert 'id="corridor-reprice-btn"' not in html
            assert 'id="corridor-auto-reprice"' in html
            assert 'data-panel-id="strategy"' in html
            assert 'id="strategy-autobuild-btn"' in html
            assert 'id="strategy-evaluate-btn"' in html
            assert "Evaluate / Re-Price" in html
            assert 'id="strategy-legs-tbody"' in html
            assert ".panel-card--corridor" in r_css.text
            assert ".corridor-stream-switch" in r_css.text
            assert ".chart-canvas--corridor" in r_css.text
            assert ".panel-card--strategy" in r_css.text
            assert ".chart-canvas--strategy" in r_css.text
            assert ".chart-canvas--strategy.is-idle" in r_css.text

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

    run_coro(_exercise())


def test_expanded_120_dte_refresh_and_query_filtering(tmp_path: Path) -> None:
    app, _repo, mock_ib = _build_test_app(tmp_path, offline=False)

    async def _expirations_up_to_125(
        symbol: str, _fut_exch: str, _sec_type: str, _con_id: int
    ) -> list[Any]:
        base = date(2026, 11, 11)
        exps = [(base + timedelta(days=d)).strftime("%Y%m%d") for d in (30, 58, 93, 120, 125)]
        return [
            SimpleNamespace(
                exchange="SMART",
                tradingClass=symbol,
                multiplier="100",
                expirations=exps,
                strikes=[135.0, 140.0, 145.0],
            )
        ]

    mock_ib.reqSecDefOptParamsAsync = _expirations_up_to_125  # type: ignore[method-assign]

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_ref = await client.post(
                "/api/refresh",
                json={
                    "ticker": "NVDA",
                    "range_pct": 0.20,
                    "strike_step": 5.0,
                    "dte_min": 30,
                    "dte_max": 120,
                },
            )
            assert r_ref.status_code == 200
            assert r_ref.json()["meta"]["dtes"] == [30, 58, 93, 120]

            r_bucket = await client.get("/api/chain/NVDA", params={"dte_min": 61, "dte_max": 120})
            assert r_bucket.status_code == 200
            assert r_bucket.json()["meta"]["dtes"] == [93, 120]

            r_bad_dte = await client.get("/api/chain/NVDA", params={"dte_max": 121})
            assert r_bad_dte.status_code == 422

    run_coro(_exercise())


def test_strategy_suggest_and_evaluate_endpoints(tmp_path: Path) -> None:
    app, repo, _mock_ib = _build_test_app(tmp_path, offline=True)
    # Seed a multi-strike ticker (AAPL) so Iron Condor + 4 presets all construct cleanly
    ts = datetime(2026, 11, 11, 15, 30, 0, tzinfo=UTC)
    rich_rows: list[dict[str, Any]] = []
    conid = 9000
    for dte in (37, 65):
        expiry = (ts.date() + timedelta(days=dte)).isoformat()
        for strike in (125.0, 130.0, 135.0, 140.0, 145.0, 150.0, 155.0):
            for right in ("C", "P"):
                conid += 1
                rich_rows.append(
                    {
                        "snapshot_id": make_snapshot_id("AAPL", ts),
                        "snapshot_ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "market_date": "2026-11-11",
                        "market_data_type": 3,
                        "underlying": "AAPL",
                        "underlying_conid": 265598,
                        "underlying_price": 140.0,
                        "option_conid": conid,
                        "expiry": expiry,
                        "dte": dte,
                        "strike": strike,
                        "moneyness_pct": ((strike - 140.0) / 140.0) * 100.0,
                        "right": right,
                        "bid": 4.0,
                        "ask": 4.4,
                        "last": 4.2,
                        "close": 4.2,
                        "volume": 2000 if strike in (130.0, 150.0) else 250,
                        "open_interest": None,
                        "iv": None,
                        "delta": None,
                        "gamma": None,
                        "theta": None,
                        "vega": None,
                        "greeks_source": "NONE",
                    }
                )
    repo.save_snapshot("AAPL", rich_rows)

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_all = await client.post("/api/strategy/suggest", json={"ticker": "AAPL"})
            assert r_all.status_code == 200
            payload = r_all.json()
            assert payload["ticker"] == "AAPL"
            kinds = [ev["strategy_kind"] for ev in payload["evaluations"]]
            assert kinds == [
                "BULL_PUT_SPREAD",
                "BULL_CALL_SPREAD",
                "DIAGONAL_SPREAD",
                "IRON_CONDOR",
            ]
            condor = payload["evaluations"][3]
            assert len(condor["legs"]) == 4
            assert condor["risk"]["expected_move_1sigma_low"] is not None

            exp_front = (ts.date() + timedelta(days=37)).isoformat()
            r_eval = await client.post(
                "/api/strategy/evaluate",
                json={
                    "ticker": "NVDA",
                    "strategy_kind": "CUSTOM",
                    "legs": [
                        {
                            "side": "SELL",
                            "right": "P",
                            "expiry": exp_front,
                            "strike": 140.0,
                            "qty": 1,
                        },
                        {
                            "side": "BUY",
                            "right": "P",
                            "expiry": exp_front,
                            "strike": 135.0,
                            "qty": 1,
                        },
                    ],
                    "days_forward": 10,
                    "iv_shift": -0.05,
                    "qty_multiplier": 2,
                },
            )
            assert r_eval.status_code == 200
            ev = r_eval.json()
            assert ev["strategy_kind"] == "CUSTOM"
            assert ev["qty_multiplier"] == 2
            assert any(c["label"].startswith("Simulated (T+10d") for c in ev["curves"])

            r_bad_leg = await client.post(
                "/api/strategy/evaluate",
                json={
                    "ticker": "NVDA",
                    "strategy_kind": "CUSTOM",
                    "legs": [
                        {
                            "side": "BUY",
                            "right": "C",
                            "expiry": exp_front,
                            "strike": 999.0,
                            "qty": 1,
                        }
                    ],
                },
            )
            assert r_bad_leg.status_code == 422

            r_404 = await client.post(
                "/api/strategy/suggest",
                json={"ticker": "PLTR", "strategy_kind": "BULL_PUT_SPREAD"},
            )
            assert r_404.status_code == 404

    run_coro(_exercise())


def test_dynamic_live_config_and_custom_gateway_host_port_across_ui_and_api(
    tmp_path: Path,
) -> None:
    custom_settings = Settings(
        ibkr=IBKRSettings(
            host="10.0.0.5",
            port=7496,
            market_data_mode="LIVE",
            default_market_data_type=1,
            fallback_market_data_type=2,
        ),
        storage=StorageSettings(),
    )
    repo = CSVRepository(custom_settings.storage, root=tmp_path)
    mock_ib = _MockIB(offline=True)
    client = IBKRBrokerClient(custom_settings, ib_factory=lambda: mock_ib)
    fetcher = PagedOptionChainFetcher(client, custom_settings, sleep=noop_sleep)
    app = create_app(settings=custom_settings, repo=repo, broker_client=client, fetcher=fetcher)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as http_client:
                r_index = await http_client.get("/")
                assert r_index.status_code == 200
                html = r_index.text
                assert "10.0.0.5:7496" in html
                assert "Live Type 1/2 Feed" in html
                assert "Delayed Type 3/4 Feed" not in html
                assert '"defaultMarketDataType": 1' in html
                assert '"marketDataMode": "LIVE"' in html
                assert '"ibkrPort": 7496' in html

                r_status = await http_client.get("/api/status")
                assert r_status.status_code == 200
                st = r_status.json()
                assert st["host"] == "10.0.0.5"
                assert st["port"] == 7496
                assert st["market_data_mode"] == "LIVE"
                assert st["default_market_data_type"] == 1
                assert st["fallback_market_data_type"] == 2
                assert st["active_market_data_type"] == 1

                r_503 = await http_client.post(
                    "/api/refresh",
                    json={"ticker": "NVDA", "range_pct": 0.20, "strike_step": 5.0},
                )
                assert r_503.status_code == 503
                err_msg = r_503.json()["message"]
                assert "10.0.0.5:7496" in err_msg
                assert "4001" not in err_msg

    run_coro(_exercise())


def test_lifespan_creates_storage_layout_on_empty_root(tmp_path: Path) -> None:
    settings = Settings(storage=StorageSettings())
    repo = CSVRepository(settings.storage, root=tmp_path)
    client = IBKRBrokerClient(settings, ib_factory=lambda: _MockIB(offline=True))
    app = create_app(settings=settings, repo=repo, broker_client=client)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            assert repo.snapshots_dir.is_dir()
            assert repo.latest_dir.is_dir()
            assert repo.index_path.parent.is_dir()

    run_coro(_exercise())


def test_service_level_validation_errors_map_to_structured_422(tmp_path: Path) -> None:
    app, _repo, _mock_ib = _build_test_app(tmp_path, offline=True)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                resp = await http.get("/api/chain/NVDA?dte_min=60&dte_max=40")
                assert resp.status_code == 422
                body = resp.json()
                assert body["error_code"] == "INVALID_REQUEST"
                assert body["message"] == body["detail"] == "dte_min must be <= dte_max"

    run_coro(_exercise())
