from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import anyio.to_thread
import httpx
import pytest

from src.analytics.strategies import StrategyLegInput
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
                'id="snapshot-picker-toggle"',
                'id="snapshot-picker-panel"',
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

            # Stylesheets: every <link> from base.html, in cascade order (tokens first).
            sheets = re.findall(r'href="(/static/css/[a-z_]+\.css)\?v=20261112c"', html)
            assert sheets[0] == "/static/css/tokens.css"
            assert sheets[-1] == "/static/css/responsive.css"
            css_texts: list[str] = []
            for sheet in sheets:
                r_sheet = await client.get(sheet)
                assert r_sheet.status_code == 200, sheet
                css_texts.append(r_sheet.text)
            r_css = SimpleNamespace(text="\n".join(css_texts))
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
            assert len(sheets) == 11

            # Dashboard ES modules: crawl the import graph from the served entry point.
            r_main = await client.get("/static/js/dashboard/main.js")
            assert r_main.status_code == 200
            assert r_main.headers["cache-control"] == "no-cache"
            module_texts: dict[str, str] = {"main.js": r_main.text}
            pending = ["main.js"]
            while pending:
                for spec in re.findall(r'from "\./([a-z_]+\.js)"', module_texts[pending.pop()]):
                    if spec not in module_texts:
                        r_mod = await client.get(f"/static/js/dashboard/{spec}")
                        assert r_mod.status_code == 200, spec
                        module_texts[spec] = r_mod.text
                        pending.append(spec)
            assert len(module_texts) >= 10
            r_js = SimpleNamespace(text="\n".join(module_texts.values()))
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
            assert '<script type="module" src="/static/js/dashboard/main.js?v=20261112c">' in html
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
                assert js_hook in r_js.text, f"Missing {js_hook} in dashboard modules"

            assert 'data-panel-target="strategy"' in html
            assert 'data-panel-target="corridor"' in html
            assert "panel-toggle-btn" not in html
            assert html.count('class="panel-toggle-pill') == 7
            assert 'data-panel-target="backtest"' in html
            assert 'id="backtest-card"' in html and 'data-panel-id="backtest"' in html
            assert "Steps are evenly spaced, not to time scale." in html
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
    run_coro(_exercise())


def test_refresh_records_quality_metadata_and_legacy_entries_load(tmp_path: Path) -> None:
    app, repo, _ = _build_test_app(tmp_path)

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_ok = await client.post(
                "/api/refresh",
                json={"ticker": "NVDA", "range_pct": 0.20, "strike_step": 5.0},
            )
            assert r_ok.status_code == 200
            meta = r_ok.json()["meta"]
            assert meta["spot_source"] == "LAST"
            assert meta["collection_mode"] == "MANUAL"
            assert meta["quote_coverage"] == pytest.approx(1.0)
            assert meta["quoted_contract_count"] == meta["contract_count"]
            assert meta["prior_close_only_count"] == 0
            assert not any("not usable" in n for n in meta["notices"])

            r_snaps = await client.get("/api/snapshots/NVDA")
            assert r_snaps.status_code == 200
            snaps = {s["snapshot_id"]: s for s in r_snaps.json()["snapshots"]}
            fresh = snaps["NVDA_20261111_160000"]
            assert fresh["spot_source"] == "LAST"
            assert fresh["collection_mode"] == "MANUAL"
            assert fresh["quote_coverage"] == pytest.approx(1.0)
            # The seeded snapshot was saved without spot_source / collection_mode
            seeded = snaps["NVDA_20261111_150000"]
            assert seeded["spot_source"] is None
            assert seeded["collection_mode"] is None

            r_chain = await client.get(
                "/api/chain/NVDA", params={"snapshot_id": "NVDA_20261111_160000"}
            )
            assert r_chain.status_code == 200
            assert r_chain.json()["meta"]["spot_source"] == "LAST"
            assert r_chain.json()["meta"]["collection_mode"] == "MANUAL"

    run_coro(_exercise())
    assert len(repo.list_snapshots("NVDA")) == 2


def test_refresh_flags_unusable_snapshot_in_notices(tmp_path: Path) -> None:
    app, repo, mock_ib = _build_test_app(tmp_path)
    original = mock_ib.reqMktData

    def _close_only_spot(contract: Any, *args: Any, **kwargs: Any) -> Any:
        if getattr(contract, "secType", "") == "STK":
            return SimpleNamespace(contract=contract, bid=-1.0, ask=-1.0, last=-1.0, close=139.5)
        return original(contract, *args, **kwargs)

    mock_ib.reqMktData = _close_only_spot  # type: ignore[method-assign]

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_ok = await client.post(
                "/api/refresh",
                json={"ticker": "NVDA", "range_pct": 0.20, "strike_step": 5.0},
            )
            assert r_ok.status_code == 200
            meta = r_ok.json()["meta"]
            assert meta["spot_source"] == "CLOSE"
            assert any(
                "not usable for history" in n and "spot_source CLOSE" in n for n in meta["notices"]
            )

    run_coro(_exercise())
    # Saved with a flag, not refused
    assert repo.list_snapshots("NVDA")[0].spot_source == "CLOSE"


def test_refresh_stops_running_corridor_streams_first(tmp_path: Path) -> None:
    import asyncio

    app, _repo, mock_ib = _build_test_app(tmp_path)
    corridor = app.state.service.corridor
    stream_contracts = [SimpleNamespace(conId=900 + i) for i in range(60)]
    session = SimpleNamespace(
        session_id="tab-2",
        stop_event=asyncio.Event(),
        cleaned_up=False,
        subscribed_contracts=stream_contracts,
        ib=mock_ib,
    )
    corridor._active_sessions["tab-2"] = session

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_ok = await client.post(
                "/api/refresh",
                json={"ticker": "NVDA", "range_pct": 0.20, "strike_step": 5.0},
            )
            assert r_ok.status_code == 200
            assert any(
                "Stopped the corridor stream (60" in n for n in r_ok.json()["meta"]["notices"]
            )

    run_coro(_exercise())
    assert corridor.active_stream_count == 0
    # The 60 stream lines were cancelled before the first option page was subscribed
    assert mock_ib.cancelled[:60] == stream_contracts


# ------------------------------------------------------------------ scheduled collection
def test_collector_status_and_settings_persist_across_restart(tmp_path: Path) -> None:
    app, _repo, _ = _build_test_app(tmp_path)

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r0 = await client.get("/api/collector/status")
            assert r0.status_code == 200
            st0 = r0.json()
            assert st0["enabled"] is False and st0["restored_on_startup"] is False
            assert [(t["symbol"], t["range_pct"], t["strike_step"]) for t in st0["tickers"]] == [
                ("NVDA", 0.35, 5.0),
                ("AVGO", 0.35, 10.0),
            ]
            assert st0["calendar_source_url"] == "https://www.nyse.com/markets/hours-calendars"

            body = {
                "enabled": True,
                "interval_min": 30,
                "tickers": [
                    {
                        "symbol": "NVDA",
                        "range_pct": 0.35,
                        "strike_step": 5.0,
                        "dte_min": 30,
                        "dte_max": 120,
                    },
                    {
                        "symbol": "aapl",
                        "range_pct": 0.20,
                        "strike_step": 5.0,
                        "dte_min": 30,
                        "dte_max": 120,
                    },
                ],
            }
            r1 = await client.put("/api/collector/settings", json=body)
            assert r1.status_code == 200, r1.text
            st1 = r1.json()
            assert st1["enabled"] is True and st1["interval_min"] == 30
            assert [t["symbol"] for t in st1["tickers"]] == ["NVDA", "AAPL"]
            assert all(t["pinned_since_utc"] for t in st1["tickers"])

    run_coro(_exercise())
    assert (tmp_path / "data" / "metadata" / "collector_settings.json").is_file()
    assert not any((tmp_path / "data" / "snapshots").rglob("collector*"))

    app2, _repo2, _ = _build_test_app(tmp_path / "restart-copy")
    # Simulate a restart on the same data directory.
    restarted = create_app(
        settings=app2.state.service.settings,
        repo=CSVRepository(StorageSettings(), root=tmp_path),
        broker_client=app2.state.service.broker_client,
        fetcher=app2.state.service.fetcher,
    )

    async def _after_restart() -> None:
        transport = httpx.ASGITransport(app=restarted)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            st = (await client.get("/api/collector/status")).json()
            assert st["enabled"] is True and st["restored_on_startup"] is True
            assert [t["symbol"] for t in st["tickers"]] == ["NVDA", "AAPL"]

    run_coro(_after_restart())


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"interval_min": 45}, 422),
        (
            {
                "tickers": [
                    {
                        "symbol": "../etc",
                        "range_pct": 0.2,
                        "strike_step": 5.0,
                        "dte_min": 30,
                        "dte_max": 120,
                    }
                ]
            },
            422,
        ),
        (
            {
                "tickers": [
                    {
                        "symbol": "NVDA",
                        "range_pct": 0.33,
                        "strike_step": 5.0,
                        "dte_min": 30,
                        "dte_max": 120,
                    }
                ]
            },
            422,
        ),
        (
            {
                "tickers": [
                    {
                        "symbol": "NVDA",
                        "range_pct": 0.2,
                        "strike_step": 7.0,
                        "dte_min": 30,
                        "dte_max": 120,
                    }
                ]
            },
            422,
        ),
        (
            {
                "tickers": [
                    {
                        "symbol": "NVDA",
                        "range_pct": 0.2,
                        "strike_step": 5.0,
                        "dte_min": 90,
                        "dte_max": 30,
                    }
                ]
            },
            422,
        ),
        (
            {
                "tickers": [
                    {
                        "symbol": "NVDA",
                        "range_pct": 0.2,
                        "strike_step": 5.0,
                        "dte_min": 30,
                        "dte_max": 121,
                    }
                ]
            },
            422,
        ),
        (
            {
                "tickers": [
                    {
                        "symbol": "NVDA",
                        "range_pct": 0.2,
                        "strike_step": 5.0,
                        "dte_min": 30,
                        "dte_max": 120,
                    },
                    {
                        "symbol": "nvda",
                        "range_pct": 0.2,
                        "strike_step": 5.0,
                        "dte_min": 30,
                        "dte_max": 120,
                    },
                ]
            },
            422,
        ),
        (
            {
                "tickers": [
                    {
                        "symbol": s,
                        "range_pct": 0.2,
                        "strike_step": 5.0,
                        "dte_min": 30,
                        "dte_max": 120,
                    }
                    for s in ("A", "B", "C", "D", "E", "F", "G")
                ]
            },
            422,
        ),
    ],
)
def test_collector_settings_validation(tmp_path: Path, change: dict[str, Any], status: int) -> None:
    app, _repo, _ = _build_test_app(tmp_path)
    body: dict[str, Any] = {"enabled": True, "interval_min": 60, "tickers": []}
    body.update(change)

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.put("/api/collector/settings", json=body)
            assert r.status_code == status, r.text

    run_coro(_exercise())
    assert not (tmp_path / "data" / "metadata" / "collector_settings.json").exists()


def test_manual_refresh_reports_a_running_collection_cycle(tmp_path: Path) -> None:
    app, _repo, _ = _build_test_app(tmp_path)
    svc = app.state.service
    assert run_coro(svc.acquire_refresh("collector", 1.0)) is True

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.post("/api/refresh", json={"ticker": "NVDA", "range_pct": 0.2})
            assert r.status_code == 409
            body = r.json()
            assert body["error_code"] == "COLLECTOR_BUSY"
            assert body["retry_after_sec"] > 0
            assert "Waiting for scheduled collection" in body["message"]
            r_bad = await client.post("/api/refresh", json={"ticker": "../etc", "range_pct": 0.2})
            assert r_bad.status_code == 422  # validation still comes first

    run_coro(_exercise())
    svc.release_refresh()
    assert svc.refresh_lock.locked() is False


def test_scheduled_cycle_uses_the_real_refresh_path(tmp_path: Path) -> None:
    import asyncio

    from src.core.calendar import Slot
    from src.storage.collector_store import CollectorSettings, TickerPin

    app, repo, _ = _build_test_app(tmp_path)
    collector = app.state.service.get_collector()
    collector.settings = CollectorSettings(
        enabled=True,
        tickers=[
            TickerPin(symbol="NVDA", range_pct=0.20, strike_step=5.0, dte_min=30, dte_max=120)
        ],
    )
    slot = Slot("INTERVAL", datetime(2026, 11, 11, 16, 0, 0, tzinfo=UTC))
    records = asyncio.run(collector.run_slot(slot))

    assert [(r.status, r.pin_drift, r.snapshot_id) for r in records] == [
        ("OK", False, "NVDA_20261111_160000")
    ]
    meta = next(m for m in repo.list_snapshots("NVDA") if m.snapshot_id == "NVDA_20261111_160000")
    assert meta.collection_mode == "SCHEDULED"
    assert meta.range_pct == pytest.approx(0.20)
    assert app.state.service.refresh_lock.locked() is False
    snap_files = list((tmp_path / "data" / "snapshots").rglob("*"))
    assert not [p for p in snap_files if p.name.startswith(".") or p.suffix == ".tmp"]


# ------------------------------------------------------------------ position tracker
_TRACK_EXPIRY = "2026-12-18"  # seeded snapshot: 2026-11-11 + 37 days
_TRACK_BODY: dict[str, Any] = {
    "ticker": "NVDA",
    "snapshot_id": "NVDA_20261111_150000",
    "legs": [
        {"side": "SELL", "right": "P", "expiry": _TRACK_EXPIRY, "strike": 140.0},
        {"side": "BUY", "right": "P", "expiry": _TRACK_EXPIRY, "strike": 135.0},
    ],
}


def test_tracker_endpoints_register_list_and_stop(tmp_path: Path) -> None:
    app, _repo, _ = _build_test_app(tmp_path)

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            empty = (await client.get("/api/tracker/spreads")).json()
            assert empty == {"active_count": 0, "max_active_spreads": 120, "spreads": []}

            r = await client.post("/api/tracker/spreads", json=_TRACK_BODY)
            assert r.status_code == 201, r.text
            spread = r.json()
            assert spread["source"] == "USER" and spread["status"] == "ACTIVE"
            assert [leg["strike"] for leg in spread["legs"]] == [140.0, 135.0]
            assert all(leg["option_conid"] > 0 for leg in spread["legs"])

            dup = await client.post("/api/tracker/spreads", json=_TRACK_BODY)
            assert dup.status_code == 409

            listed = (await client.get("/api/tracker/spreads?status=ACTIVE")).json()
            assert listed["active_count"] == 1 and listed["spreads"][0]["id"] == spread["id"]
            assert listed["spreads"][0]["last_quote_utc"] is None

            st = (await client.get("/api/collector/status")).json()
            assert st["tracker"]["active_count"] == 1
            assert st["tracker"]["max_active_spreads"] == 120
            assert st["tracker"]["last_result"] is None

            stop = await client.post(f"/api/tracker/spreads/{spread['id']}/stop")
            assert stop.status_code == 200 and stop.json()["status"] == "STOPPED"
            again = await client.post(f"/api/tracker/spreads/{spread['id']}/stop")
            assert again.status_code == 409
            unknown = await client.post("/api/tracker/spreads/NVDA-20261111-0000abcd/stop")
            assert unknown.status_code == 404
            bad_id = await client.post("/api/tracker/spreads/..%2Fx/stop")
            assert bad_id.status_code in (404, 422)
            stopped = (await client.get("/api/tracker/spreads?status=STOPPED")).json()
            assert [s["id"] for s in stopped["spreads"]] == [spread["id"]]

    run_coro(_exercise())
    assert (tmp_path / "data" / "metadata" / "tracked_spreads.json").is_file()


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"ticker": "nvda1"}, 422),
        ({"ticker": "AVGO", "snapshot_id": "AVGO_20261111_150000"}, 404),
        ({"snapshot_id": "NVDA_20261111_150001"}, 404),
        ({"snapshot_id": "../etc"}, 422),
        ({"legs": []}, 422),
        ({"extra": 1}, 422),
        (
            {
                "legs": [
                    {"side": "SELL", "right": "P", "expiry": _TRACK_EXPIRY, "strike": 135.0},
                    {"side": "BUY", "right": "P", "expiry": _TRACK_EXPIRY, "strike": 140.0},
                ]
            },
            422,
        ),
        (
            {
                "legs": [
                    {"side": "SELL", "right": "P", "expiry": _TRACK_EXPIRY, "strike": 141.0},
                    {"side": "BUY", "right": "P", "expiry": _TRACK_EXPIRY, "strike": 135.0},
                ]
            },
            422,
        ),
    ],
)
def test_tracker_register_validation(tmp_path: Path, change: dict[str, Any], status: int) -> None:
    app, _repo, _ = _build_test_app(tmp_path)

    async def _exercise() -> int:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r = await client.post("/api/tracker/spreads", json={**_TRACK_BODY, **change})
            return r.status_code

    assert run_coro(_exercise()) == status
    assert not (tmp_path / "data" / "metadata" / "tracked_spreads.json").exists()


def test_scheduled_cycle_quotes_tracked_legs_through_the_broker(tmp_path: Path) -> None:
    import asyncio

    from src.core.calendar import Slot
    from src.storage.collector_store import CollectorSettings, TickerPin

    app, _repo, mock_ib = _build_test_app(tmp_path)
    subscribed: list[Any] = []
    real_req = mock_ib.reqMktData

    def _recording_req(contract: Any, *args: Any, **kwargs: Any) -> Any:
        subscribed.append(contract)
        return real_req(contract, *args, **kwargs)

    mock_ib.reqMktData = _recording_req  # type: ignore[method-assign]
    svc = app.state.service
    tracker = svc.get_tracker()
    spread = tracker.register_user(
        "NVDA",
        "NVDA_20261111_150000",
        [StrategyLegInput.model_validate(leg) for leg in _TRACK_BODY["legs"]],
    )
    leg_conids = {leg.option_conid for leg in spread.legs}
    collector = svc.get_collector()
    collector.settings = CollectorSettings(
        enabled=True,
        tickers=[
            TickerPin(symbol="NVDA", range_pct=0.20, strike_step=5.0, dte_min=30, dte_max=120)
        ],
    )
    slot = Slot("INTERVAL", datetime(2026, 11, 11, 16, 0, 0, tzinfo=UTC))
    asyncio.run(collector.run_slot(slot))

    runs = [r for r in tracker.store.read_runs() if r.kind == "QUOTE"]
    assert [(r.spread_id, r.status, r.spot_source) for r in runs] == [
        (spread.id, "OK", runs[0].spot_source)
    ]
    assert runs[0].underlying_price and runs[0].market_data_type in (1, 2, 3, 4)
    files = list((tmp_path / "data" / "tracked" / "NVDA").glob("*.csv"))
    assert len(files) == 1
    # The tracker's option subscriptions are exactly the two leg conIds.
    option_subs = [c for c in subscribed if getattr(c, "secType", "") == "OPT"]
    assert {getattr(c, "conId", 0) for c in option_subs[-2:]} == leg_conids
    assert getattr(subscribed[-3], "secType", "") == "STK"  # tracker spot, then the two legs
    assert all(c in mock_ib.cancelled for c in subscribed)
    assert svc.refresh_lock.locked() is False


# ------------------------------------------------------------------ backtest panel API
@pytest.fixture(autouse=True)
def _backtest_inline_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the backtest API's worker-thread calls inline (these tests use no real event loop)."""
    from src.web import backtest_api

    async def _inline(func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)

    monkeypatch.setattr(backtest_api, "_in_thread", _inline)


def _backtest_app(world: Any, **tracker_cfg: Any) -> Any:
    from src.core.config import TrackerSettings

    settings = Settings(storage=StorageSettings(), tracker=TrackerSettings(**tracker_cfg))
    client = IBKRBrokerClient(settings, ib_factory=lambda: _MockIB(offline=True))
    return create_app(settings=settings, repo=world.repo, broker_client=client)


def _bt_post(app: Any, body: dict[str, Any]) -> httpx.Response:
    async def _go() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/api/backtest/run", json=body)

    return run_coro(_go())


def _bt_get(app: Any, url: str) -> httpx.Response:
    async def _go() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.get(url)

    return run_coro(_go())


def _legs_body(world: Any, **change: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source": "LEGS",
        "ticker": "NVDA",
        "entry_snapshot_id": world.entry_id,
        "expiry": "2026-12-18",
        "short_strike": 100.0,
        "long_strike": 95.0,
        "qty": 1,
    }
    body.update(change)
    return body


def test_backtest_entries_are_newest_first_with_quality(tmp_path: Path) -> None:
    from tests.unit.test_backtest_tracked import SLOT_OUT, World

    w = World(tmp_path)
    later = w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",), md_type=4)
    body = _bt_get(_backtest_app(w), "/api/backtest/entries/nvda").json()
    assert body["ticker"] == "NVDA"
    assert [e["snapshot_id"] for e in body["entries"]] == [later, w.entry_id]
    newest = body["entries"][0]
    assert newest["usable"] is False
    assert any("market_data_type" in r for r in newest["quality_reasons"])
    assert body["entries"][1]["usable"] is True
    assert newest["ts_et"].endswith(" ET") and newest["ts_poland"].endswith(" PL")
    assert _bt_get(_backtest_app(w), "/api/backtest/entries/NVDA1").status_code == 422


def test_backtest_my_spread_open_trade(tmp_path: Path) -> None:
    from tests.unit.test_backtest import ENTRY_QUOTES
    from tests.unit.test_backtest_tracked import World, _et

    w = World(tmp_path)
    w.snapshot(_et(date(2026, 11, 20), 11, 45), 103.0, ENTRY_QUOTES)
    r = _bt_post(_backtest_app(w), _legs_body(w, qty=2))
    assert r.status_code == 200, r.text
    data = r.json()
    res = data["result"]
    assert data["source"] == "LEGS" and data["outcome"] == "OPEN"
    assert [(leg["side"], leg["strike"], leg["qty"]) for leg in res["entry"]["legs"]] == [
        ("SELL", 100.0, 2),
        ("BUY", 95.0, 2),
    ]
    assert res["entry"]["entry_cash_mid_dollars"] == pytest.approx(220.0)
    assert len(data["ts_poland"]) == len(res["steps"]) == 2
    assert [x["code"] for x in data["warnings"]] == ["TRADE_OPEN"]
    assert data["tracked"] is None
    assert data["collector"] == {"enabled": False, "ever_enabled": True}
    assert data["days_held"] == pytest.approx(4.0, abs=0.01)
    assert res["summary"]["steps_gap"] == 0
    assert len(data["assumptions"]) == 6


@pytest.mark.parametrize(
    ("change", "status", "fragment"),
    [
        ({"long_strike": 105.0}, 422, "below the short"),
        ({"short_strike": 101.0}, 422, "not found"),
        ({"ticker": "NVDA1"}, 422, ""),
        ({"entry_snapshot_id": "NVDA_20261116_000000"}, 404, ""),
        ({"qty": 101}, 422, ""),
        ({"expiry": None}, 422, "needs expiry"),
    ],
)
def test_backtest_my_spread_validation(
    tmp_path: Path, change: dict[str, Any], status: int, fragment: str
) -> None:
    from tests.unit.test_backtest_tracked import World

    w = World(tmp_path)
    r = _bt_post(_backtest_app(w), _legs_body(w, **change))
    assert r.status_code == status, r.text
    assert fragment.lower() in r.text.lower()


def test_backtest_auto_build_and_no_valid_spread(tmp_path: Path) -> None:
    import polars as pl

    from tests.unit.test_backtest import _auto_chain
    from tests.unit.test_backtest_tracked import World

    w = World(tmp_path)
    first = _auto_chain("2026-11-11T15:30:00", 100.0)
    w.repo.save_snapshot("NVDA", first.to_dicts(), spot_source="LAST")
    w.repo.save_snapshot(
        "NVDA", _auto_chain("2026-11-11T18:30:00", 103.0).to_dicts(), spot_source="LAST"
    )
    sid = str(first["snapshot_id"][0])
    app = _backtest_app(w)
    r = _bt_post(app, {"source": "AUTO_BUILD", "ticker": "NVDA", "entry_snapshot_id": sid})
    assert r.status_code == 200, r.text
    entry = r.json()["result"]["entry"]
    assert [leg["side"] for leg in entry["legs"]] == ["SELL", "BUY"] and entry["rationale"]

    calls_only = _auto_chain("2026-11-11T20:30:00", 100.0).filter(pl.col("right") == "C")
    w.repo.save_snapshot("NVDA", calls_only.to_dicts(), spot_source="LAST")
    bad = _bt_post(
        app,
        {"source": "AUTO_BUILD", "ticker": "NVDA", "entry_snapshot_id": "NVDA_20261111_203000"},
    )
    assert bad.status_code == 422 and "no valid bull put spread" in bad.text


def test_backtest_single_snapshot_warns_few_snapshots(tmp_path: Path) -> None:
    from tests.unit.test_backtest_tracked import World

    w = World(tmp_path)
    data = _bt_post(_backtest_app(w), _legs_body(w)).json()
    codes = [x["code"] for x in data["warnings"]]
    assert "FEW_SNAPSHOTS" in codes and "TRADE_OPEN" in codes
    assert data["history_snapshot_count"] == 1


def test_backtest_settled_and_unresolved(tmp_path: Path) -> None:
    from tests.unit.test_backtest_tracked import World, _et

    expiry = date(2026, 12, 18)
    w = World(tmp_path / "settled")
    w.snapshot(_et(expiry, 16, 15), 104.0, expiries=("2027-01-15",))
    data = _bt_post(_backtest_app(w), _legs_body(w)).json()
    s = data["result"]["settlement"]
    assert data["outcome"] == "SETTLED" and s["spot_from"] == "SNAPSHOT"
    assert (s["spot"], s["spot_source"], s["market_data_type"]) == (104.0, "LAST", 3)
    assert data["settlement_ts_poland"] == "2026-12-18 22:15 PL"

    w2 = World(tmp_path / "unresolved")
    w2.snapshot(_et(expiry, 16, 5), 104.0, expiries=("2027-01-15",), scheduled=False)
    w2.snapshot(_et(date(2026, 12, 21), 10, 0), 104.0, expiries=("2027-01-15",), scheduled=False)
    data2 = _bt_post(_backtest_app(w2), _legs_body(w2)).json()
    assert data2["outcome"] == "UNRESOLVED"
    assert "SETTLEMENT_UNRESOLVED" in [x["code"] for x in data2["warnings"]]


def test_backtest_tracked_spread_by_id_and_already_tracked(tmp_path: Path) -> None:
    from tests.unit.test_backtest_tracked import SLOT_OUT, SPREAD_ID, World

    w = World(tmp_path)
    w.register()
    w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",))
    w.track(SLOT_OUT)
    app = _backtest_app(w)
    by_id = _bt_post(app, {"source": "TRACKED", "spread_id": SPREAD_ID})
    assert by_id.status_code == 200, by_id.text
    legs = _bt_post(app, _legs_body(w)).json()
    assert by_id.json()["result"] == legs["result"]  # same as mode A with the registry legs
    assert legs["tracked"] == {"spread_id": SPREAD_ID, "source": "USER", "status": "ACTIVE"}
    assert legs["result"]["reproducibility"]["tracked_data_used"] is True
    unknown = _bt_post(app, {"source": "TRACKED", "spread_id": "NVDA-20261116-00000000"})
    assert unknown.status_code == 404
    assert _bt_post(app, {"source": "TRACKED"}).status_code == 422


def test_backtest_passes_tracked_dir_and_serializes_gap_steps(tmp_path: Path) -> None:
    from src.storage.tracker_store import TrackerStore
    from tests.unit.test_backtest_tracked import SLOT_OUT, World, _et

    w = World(tmp_path)
    w.tracker = TrackerStore(w.repo.index_path.parent, tmp_path / "data" / "tracked_alt")
    w.register()
    w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",))
    tid = w.track(SLOT_OUT)
    w.track(_et(date(2026, 11, 25), 13, 45), status="MISSED", write_file=False)
    data = _bt_post(_backtest_app(w, tracked_dir="data/tracked_alt"), _legs_body(w)).json()
    res = data["result"]
    assert res["reproducibility"]["tracked_files"] == [tid]  # read from the configured folder
    gap = next(s for s in res["steps"] if s["kind"] == "GAP")
    assert (gap["spot"], gap["market_data_type"], gap["quote_coverage"]) == (None, None, None)
    assert gap["pnl_dollars"] is None and gap["status"] == "INCOMPLETE"
    assert res["summary"]["steps_gap"] == 1
    # With the default folder the tracked file is not found: the leg stays missing.
    default = _bt_post(_backtest_app(w), _legs_body(w)).json()
    assert default["result"]["reproducibility"]["tracked_files"] == []


def test_backtest_warns_when_the_collector_log_is_missing(tmp_path: Path) -> None:
    from tests.unit.test_backtest_tracked import SLOT_OUT, World

    w = World(tmp_path)
    w.register()
    w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",))
    w.track(SLOT_OUT)
    (tmp_path / "data" / "metadata" / "collector_runs.jsonl").unlink()
    data = _bt_post(_backtest_app(w), _legs_body(w)).json()
    assert "COLLECTOR_LOG_MISSING" in [x["code"] for x in data["warnings"]]
    kinds = [s["kind"] for s in data["result"]["steps"]]
    assert kinds.count("TRACKED_ONLY") == 1  # the doubled step the warning explains


def test_tracking_the_same_legs_twice_is_refused(tmp_path: Path) -> None:
    from tests.unit.test_backtest_tracked import World

    w = World(tmp_path)
    app = _backtest_app(w)
    body = {
        "ticker": "NVDA",
        "snapshot_id": w.entry_id,
        "legs": [
            {"side": "SELL", "right": "P", "expiry": "2026-12-18", "strike": 100.0},
            {"side": "BUY", "right": "P", "expiry": "2026-12-18", "strike": 95.0},
        ],
    }

    async def _go() -> tuple[int, int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            a = await client.post("/api/tracker/spreads", json=body)
            b = await client.post("/api/tracker/spreads", json=body)
            return a.status_code, b.status_code

    assert run_coro(_go()) == (201, 409)
    data = _bt_post(app, _legs_body(w)).json()
    assert data["tracked"] is not None and data["tracked"]["status"] == "ACTIVE"
