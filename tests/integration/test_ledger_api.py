"""Integration tests for Phase 3 Trade Ledger API routes, error mapping, and HTML surface."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anyio
import anyio.to_thread
import httpx
import pytest
from ib_async import CommissionReport, Contract, Execution, Fill, Position

from src.broker.client import IBKRBrokerClient
from src.broker.flex import FlexWebServiceClient
from src.core.config import IBKRSettings, LedgerSettings, Settings, StorageSettings
from src.core.exceptions import FlexServiceError, InvalidRequestError
from src.main import create_app
from src.services.ledger import LedgerService
from src.storage.csv_repository import CSVRepository
from src.storage.ledger_repository import LedgerRepository
from src.web import api as web_api
from tests.support.aio import noop_sleep, run_coro
from tests.support.ib_fakes import FakeEvent

_FLEX_TOKEN = "secret-flex-tok-999"


class _FakeLedgerIB:
    """Minimal async IB stub covering Gateway ledger fills, positions, and live MTM marks."""

    def __init__(self, *, offline: bool = False) -> None:
        self._connected = False
        self.offline = offline
        self.errorEvent = FakeEvent()
        self.wrapper = SimpleNamespace(clientId=0)
        self.fills_opt: list[Any] = []
        self.fills_stk: list[Any] = []
        self.positions_result: list[Any] = []
        self.quote_by_conid: dict[int, tuple[float, float, float, float]] = {}

        async def _connect_async(host: str, port: int, client_id: int, timeout_sec: float) -> None:
            if self.offline:
                raise ConnectionRefusedError("Gateway port 4001 offline")
            self._connected = True

        self.client = SimpleNamespace(connectAsync=_connect_async)

    def isConnected(self) -> bool:  # noqa: N802
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def reqMarketDataType(self, data_type: int) -> None:  # noqa: N802
        return None

    async def reqExecutionsAsync(self, execFilter: Any = None) -> list[Any]:  # noqa: N802, N803
        sec_type = getattr(execFilter, "secType", "") if execFilter is not None else ""
        if sec_type == "OPT":
            return list(self.fills_opt)
        if sec_type == "STK":
            return list(self.fills_stk)
        return [*self.fills_opt, *self.fills_stk]

    async def reqPositionsAsync(self) -> list[Any]:  # noqa: N802
        return list(self.positions_result)

    def reqMktData(  # noqa: N802
        self,
        contract: Any,
        genericTickList: str = "",  # noqa: N803
        snapshot: bool = False,
        regulatorySnapshot: bool = False,  # noqa: N803
    ) -> Any:
        conid = int(getattr(contract, "conId", 0) or 0)
        bid, ask, last, close = self.quote_by_conid.get(conid, (4.0, 4.2, 4.1, 4.05))
        return SimpleNamespace(
            contract=contract,
            bid=bid,
            ask=ask,
            last=last,
            close=close,
            marketDataType=3,
        )

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802
        return None


def _patch_inline_anyio(monkeypatch: pytest.MonkeyPatch, app: Any) -> Any:
    """Run Starlette anyio threadpool calls inline without OS socketpairs or worker threads."""

    async def _run_sync_inline(func: Callable[..., Any], *args: Any, **_kwargs: Any) -> Any:
        return func(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", _run_sync_inline)
    monkeypatch.setattr(
        anyio.CancelScope,
        "__new__",
        lambda _cls, **_kw: contextlib.nullcontext(),
    )

    async def _asgi_24(scope: Any, receive: Any, send: Any) -> None:
        patched_scope = {**scope, "asgi": {"version": "3.0", "spec_version": "2.4"}}
        await app(patched_scope, receive, send)

    return _asgi_24


def _make_flex_client(
    payload_by_query: dict[str, bytes] | None = None,
    *,
    fail_with: Exception | None = None,
) -> tuple[FlexWebServiceClient, list[str]]:
    requested: list[str] = []
    refs: dict[str, str] = {}
    payloads = payload_by_query or {}

    async def fake_fetch(url: str) -> bytes:
        if fail_with is not None:
            raise fail_with
        if "SendRequest" in url:
            qid = ""
            for part in url.split("?", 1)[-1].split("&"):
                if part.startswith("q="):
                    qid = part.split("=", 1)[1]
            requested.append(qid)
            ref = f"REF-{qid}"
            refs[ref] = qid
            return (
                b"<FlexStatementResponse timestamp='20261112'>"
                b"<Status>Success</Status>"
                + f"<ReferenceCode>{ref}</ReferenceCode>".encode("ascii")
                + b"<Url>https://ndcdyn.interactivebrokers.com/GetStatement</Url>"
                b"</FlexStatementResponse>"
            )
        for ref, qid in refs.items():
            if f"q={ref}" in url:
                return payloads[qid]
        return b""

    client = FlexWebServiceClient(token=_FLEX_TOKEN, http_fetch=fake_fetch, sleep=noop_sleep)
    return client, requested


def _build_app(
    tmp_path: Path,
    *,
    flex_token: str | None = None,
    activity_id: str | None = None,
    monthly_id: str | None = None,
    trade_conf_id: str | None = None,
    gateway_sync_enabled: bool = False,
    fake_ib: _FakeLedgerIB | None = None,
    payload_by_query: dict[str, bytes] | None = None,
    flex_fail_with: Exception | None = None,
) -> tuple[Any, LedgerRepository, list[str]]:
    cfg = Settings(
        ibkr=IBKRSettings(
            market_data_mode="DELAYED",
            default_market_data_type=3,
            fallback_market_data_type=4,
        ),
        storage=StorageSettings(),
        ledger=LedgerSettings(
            flex_token=flex_token,
            flex_activity_query_id=activity_id,
            flex_monthly_query_id=monthly_id,
            flex_trade_conf_query_id=trade_conf_id,
            gateway_sync_enabled=gateway_sync_enabled,
        ),
    )
    csv_repo = CSVRepository(cfg.storage, root=tmp_path)
    ledger_repo = LedgerRepository(cfg.ledger.ledger_dir, root=tmp_path)
    ib_instance = fake_ib or _FakeLedgerIB(offline=True)
    broker = IBKRBrokerClient(cfg, ib_factory=lambda: ib_instance)
    flex_client, requested_queries = _make_flex_client(
        payload_by_query,
        fail_with=flex_fail_with,
    )
    fixed_now = datetime(2026, 11, 12, 16, 0, tzinfo=UTC)
    ledger_svc = LedgerService(
        settings=cfg,
        repo=ledger_repo,
        broker_client=broker,
        flex_client=flex_client,
        now_fn=lambda: fixed_now,
        sleep=noop_sleep,
    )
    app = create_app(
        settings=cfg,
        repo=csv_repo,
        broker_client=broker,
        ledger_service=ledger_svc,
    )
    return app, ledger_repo, requested_queries


def _sample_roll_and_mtm_xml() -> bytes:
    """XML covering NVDA covered call roll + stock gain, AVGO older put, and open OPT."""
    return b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Ledger" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" toDate="20261112">
      <Trades>
        <!-- AVGO older closed short put (3M / YTD / ALL, outside 1M / 1W) -->
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="AVGO  260918P00170000" underlyingSymbol="AVGO" conid="710001"
               multiplier="100" strike="170" expiry="20260918" putCall="P"
               dateTime="20260901;143000" quantity="-1" tradePrice="4.50"
               proceeds="450.0" ibCommission="-1.10" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="SELL" ibOrderID="8801"
               ibExecID="avgo-open-1" levelOfDetail="EXECUTION" />
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="AVGO  260918P00170000" underlyingSymbol="AVGO" conid="710001"
               multiplier="100" strike="170" expiry="20260918" putCall="P"
               dateTime="20260914;153000" quantity="1" tradePrice="1.50"
               proceeds="-150.0" ibCommission="-1.10" fifoPnlRealized="297.8"
               openCloseIndicator="C" buySell="BUY" ibOrderID="8802"
               ibExecID="avgo-close-1" levelOfDetail="EXECUTION" />

        <!-- NVDA Covered Call + Roll Up-and-Out + Stock Close -->
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="STK"
               symbol="NVDA" underlyingSymbol="NVDA" conid="4815747"
               multiplier="1" dateTime="20261015;140000" quantity="100" tradePrice="120.00"
               proceeds="-12000.0" ibCommission="-1.00" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="BUY" ibOrderID="9001"
               ibExecID="nvda-stk-open" levelOfDetail="EXECUTION" />
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261030C00130000" underlyingSymbol="NVDA" conid="700010"
               underlyingConid="4815747" multiplier="100" strike="130" expiry="20261030"
               putCall="C" dateTime="20261015;140500" quantity="-1" tradePrice="3.00"
               proceeds="300.0" ibCommission="-1.00" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="SELL" ibOrderID="9002"
               ibExecID="nvda-cc1-open" levelOfDetail="EXECUTION" />
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261030C00130000" underlyingSymbol="NVDA" conid="700010"
               underlyingConid="4815747" multiplier="100" strike="130" expiry="20261030"
               putCall="C" dateTime="20261028;150000" quantity="1" tradePrice="1.20"
               proceeds="-120.0" ibCommission="-1.00" fifoPnlRealized="178.0"
               openCloseIndicator="C" buySell="BUY" ibOrderID="9003"
               ibExecID="nvda-cc1-close" levelOfDetail="EXECUTION" />
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261120C00135000" underlyingSymbol="NVDA" conid="700011"
               underlyingConid="4815747" multiplier="100" strike="135" expiry="20261120"
               putCall="C" dateTime="20261028;150005" quantity="-1" tradePrice="4.10"
               proceeds="410.0" ibCommission="-1.00" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="SELL" ibOrderID="9004"
               ibExecID="nvda-cc2-open" levelOfDetail="EXECUTION" />
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261120C00135000" underlyingSymbol="NVDA" conid="700011"
               underlyingConid="4815747" multiplier="100" strike="135" expiry="20261120"
               putCall="C" dateTime="20261110;153000" quantity="1" tradePrice="0.90"
               proceeds="-90.0" ibCommission="-1.00" fifoPnlRealized="318.0"
               openCloseIndicator="C" buySell="BUY" ibOrderID="9005"
               ibExecID="nvda-cc2-close" levelOfDetail="EXECUTION" />
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="STK"
               symbol="NVDA" underlyingSymbol="NVDA" conid="4815747"
               multiplier="1" dateTime="20261110;153500" quantity="-100" tradePrice="132.00"
               proceeds="13200.0" ibCommission="-1.00" fifoPnlRealized="1198.0"
               openCloseIndicator="C" buySell="SELL" ibOrderID="9006"
               ibExecID="nvda-stk-close" levelOfDetail="EXECUTION" />

        <!-- Open short call for Open & MTM view -->
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261218C00140000" underlyingSymbol="NVDA" conid="700001"
               underlyingConid="4815747" multiplier="100" strike="140" expiry="20261218"
               putCall="C" dateTime="20261112;153000" quantity="-2" tradePrice="5.40"
               proceeds="1080.0" ibCommission="-1.35" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="SELL" ibOrderID="9901"
               ibExecID="nvda-open-mtm-1" levelOfDetail="EXECUTION" />
      </Trades>
      <OpenPositions>
        <OpenPosition accountId="U1234567" assetCategory="OPT" levelOfDetail="SUMMARY"
                      symbol="NVDA  261218C00140000" underlyingSymbol="NVDA" conid="700001"
                      multiplier="100" strike="140" expiry="20261218" putCall="C"
                      position="-2" costBasisPrice="5.40" markPrice="4.85"
                      positionValue="-970.0" fifoPnlUnrealized="110.0" reportDate="20261112" />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


def _sample_activity_csv() -> bytes:
    return (
        b"Statement,Header,Field Name,Field Value\n"
        b'Statement,Data,Period,"November 1, 2026 - November 12, 2026"\n'
        b"Account Information,Header,Field Name,Field Value\n"
        b"Account Information,Data,Account,U1234567\n"
        b"Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,"
        b"T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code\n"
        b'Trades,Data,Order,Equity and Index Options,USD,PLTR 18DEC26 45 P,"2026-11-08, 15:00:00",'
        b"-1,2.20,2.00,220.00,-1.05,-218.95,0,20,O\n"
        b'Trades,Data,Order,Equity and Index Options,USD,PLTR 18DEC26 45 P,"2026-11-11, 15:30:00",'
        b"1,0.70,0.70,-70.00,-1.05,218.95,147.90,0,C\n"
    )


def test_ledger_html_surface_and_offline_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, ledger_repo, _ = _build_app(tmp_path)
    asgi_app = _patch_inline_anyio(monkeypatch, app)

    async def _exercise() -> None:
        async with app.router.lifespan_context(app):
            assert ledger_repo.ledger_dir.exists()
            assert (ledger_repo.ledger_dir / "imports").exists()

            transport = httpx.ASGITransport(app=asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                r_html = await client.get("/")
                assert r_html.status_code == 200
                html = r_html.text

                assert 'href="/static/css/ledger.css?v=20261114c"' in html
                assert 'data-panel-target="trade_ledger"' in html
                assert 'id="trade-ledger-card"' in html
                assert 'data-panel-id="trade_ledger"' in html
                for element_id in (
                    "ledger-status-badge",
                    "ledger-window-group",
                    "ledger-view-group",
                    "ledger-group-group",
                    "ledger-stock-gain-toggle",
                    "ledger-ticker-filter",
                    "ledger-sync-mode",
                    "btn-ledger-sync",
                    "btn-ledger-import",
                    "ledger-import-file",
                    "btn-ledger-mtm",
                    "ledger-kpi-strip",
                    "chart-ledger",
                    "ledger-table-container",
                    "ledger-table-body",
                ):
                    assert f'id="{element_id}"' in html

                r_js = await client.get("/static/js/dashboard/ledger.js")
                assert r_js.status_code == 200
                assert "&bull;" not in r_js.text
                assert "&mdash;" not in r_js.text
                for js_hook in (
                    "computeLedgerDateAxisSpec",
                    "syncLedgerTimelineForCurrentWidth",
                    "attachLedgerTimelineHandlers",
                    'lineLayout.xaxis.type = "date"',
                    "ResizeObserver",
                    "plotly_relayout",
                ):
                    assert js_hook in r_js.text

                r_status = await client.get("/api/status")
                assert r_status.status_code == 200
                status_json = r_status.json()
                assert status_json["flex_configured"] is False
                assert status_json["gateway_ledger_sync_enabled"] is False

                r_empty = await client.get("/api/ledger")
                assert r_empty.status_code == 200
                empty_data = r_empty.json()
                assert empty_data["report"]["kpis"]["realized_pnl_ex_fee"] == pytest.approx(0.0)
                assert empty_data["report"]["kpis"]["closed_count"] == 0
                assert empty_data["report"]["campaigns"] == []
                assert empty_data["report"]["open_positions"] == []
                assert empty_data["status"]["execution_count"] == 0
                assert empty_data["status"]["recommended_sync_mode"] == "full"

    run_coro(_exercise())


def test_ledger_import_and_dashboard_queries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, _ = _build_app(tmp_path)
    asgi_app = _patch_inline_anyio(monkeypatch, app)

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Invalid uploads fail cleanly with 422 without tracebacks
            r_empty = await client.post(
                "/api/ledger/import",
                files={"file": ("empty.csv", b"   ", "text/csv")},
            )
            assert r_empty.status_code == 422
            assert r_empty.json()["error_code"] == "INVALID_REQUEST"

            r_bad_xml = await client.post(
                "/api/ledger/import",
                files={"file": ("broken.xml", b"<FlexQueryResponse><broken", "application/xml")},
            )
            assert r_bad_xml.status_code == 422
            assert r_bad_xml.json()["error_code"] == "STORAGE_SCHEMA_ERROR"

            r_bad_bytes = await client.post(
                "/api/ledger/import",
                files={"file": ("bad.csv", b"\xff\xfe\x00", "text/csv")},
            )
            assert r_bad_bytes.status_code == 422
            assert r_bad_bytes.json()["error_code"] == "INVALID_REQUEST"

            # 2. Import Flex XML + Activity CSV
            r_xml = await client.post(
                "/api/ledger/import",
                files={"file": ("flex_history.xml", _sample_roll_and_mtm_xml(), "application/xml")},
            )
            assert r_xml.status_code == 200
            xml_json = r_xml.json()
            assert xml_json["file_format"] == "XML"
            assert xml_json["executions_inserted"] == 9
            assert xml_json["open_positions_imported"] == 1
            assert "U1234567" not in r_xml.text

            r_csv = await client.post(
                "/api/ledger/import",
                files={"file": ("activity_nov.csv", _sample_activity_csv(), "text/csv")},
            )
            assert r_csv.status_code == 200
            csv_json = r_csv.json()
            assert csv_json["file_format"] == "CSV"
            assert csv_json["executions_inserted"] == 2
            assert "U1234567" not in r_csv.text

            # 3. Campaigns view across timeframes
            r_1w = await client.get("/api/ledger?window=1W&group_by=campaigns&view=realized")
            r_1m = await client.get("/api/ledger?window=1M&group_by=campaigns&view=realized")
            r_all = await client.get("/api/ledger?window=ALL&group_by=campaigns&view=realized")
            assert r_1w.status_code == 200
            assert r_1m.status_code == 200
            assert r_all.status_code == 200
            assert "U1234567" not in r_all.text

            for win in ("1W", "1M", "3M", "YTD", "1Y", "ALL"):
                r_win = await client.get(f"/api/ledger?window={win}")
                assert r_win.status_code == 200
                assert r_win.json()["report"]["window"] == win

            d_1w = r_1w.json()["report"]
            d_1m = r_1m.json()["report"]
            d_all = r_all.json()["report"]
            # ALL includes AVGO (September) while 1M / 1W exclude September
            assert d_all["kpis"]["realized_pnl_ex_fee"] > d_1m["kpis"]["realized_pnl_ex_fee"]
            assert d_1m["kpis"]["realized_pnl_ex_fee"] >= d_1w["kpis"]["realized_pnl_ex_fee"]
            assert len(d_all["campaigns"]) >= 3
            assert len(d_all["chart_points"]) >= 1

            # NVDA closed covered-call roll campaign preserves roll steps & fees
            nvda_campaigns = [
                c
                for c in d_all["campaigns"]
                if c["underlying"] == "NVDA" and c["status"] == "CLOSED"
            ]
            assert len(nvda_campaigns) == 1
            assert nvda_campaigns[0]["roll_count"] >= 1
            assert len(nvda_campaigns[0]["steps"]) >= 3
            assert nvda_campaigns[0]["fees"] > 0

            # 4. Include stock gain toggle increases total_pnl_ex_fee via NVDA stock gain
            r_with_stk = await client.get(
                "/api/ledger?window=ALL&ticker=NVDA&include_stock_gain=true"
            )
            r_no_stk = await client.get(
                "/api/ledger?window=ALL&ticker=NVDA&include_stock_gain=false"
            )
            assert r_with_stk.status_code == 200
            k_with = r_with_stk.json()["report"]["kpis"]
            k_no = r_no_stk.json()["report"]["kpis"]
            assert k_with["stock_pnl_ex_fee"] == pytest.approx(1200.0)
            assert k_no["stock_pnl_ex_fee"] == pytest.approx(0.0)
            assert k_with["total_pnl_ex_fee"] > k_no["total_pnl_ex_fee"]

            # 5. Breakdowns (strategy, ticker), Executions, and Open MTM view
            r_strat = await client.get("/api/ledger?window=ALL&group_by=strategy")
            assert r_strat.status_code == 200
            assert len(r_strat.json()["report"]["breakdowns"]) >= 2

            r_tkr = await client.get("/api/ledger?window=ALL&group_by=ticker")
            assert r_tkr.status_code == 200
            labels = {row["label"] for row in r_tkr.json()["report"]["breakdowns"]}
            assert {"NVDA", "AVGO", "PLTR"} <= labels

            r_exec = await client.get("/api/ledger?window=ALL&group_by=executions&ticker=NVDA")
            assert r_exec.status_code == 200
            execs = r_exec.json()["report"]["executions"]
            assert len(execs) == 5
            assert all(row["underlying"] == "NVDA" for row in execs)

            r_exec_stk = await client.get(
                "/api/ledger?window=ALL&group_by=executions&ticker=NVDA&include_stock_gain=true"
            )
            assert r_exec_stk.status_code == 200
            assert len(r_exec_stk.json()["report"]["executions"]) == 7

            r_open = await client.get("/api/ledger?window=ALL&view=open_mtm")
            assert r_open.status_code == 200
            open_pos = r_open.json()["report"]["open_positions"]
            assert len(open_pos) == 1
            assert open_pos[0]["underlying"] == "NVDA"
            assert open_pos[0]["mark_source"] == "FLEX_MARK"

            # 6. Validation errors on query parameters
            assert (await client.get("/api/ledger?ticker=BAD$SYM")).status_code == 422
            assert (await client.get("/api/ledger?window=2D")).status_code == 422
            assert (await client.get("/api/ledger?group_by=unknown")).status_code == 422
            assert (await client.get("/api/ledger?view=unknown")).status_code == 422

    run_coro(_exercise())


def test_ledger_sync_routes_and_error_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_unconfigured, _, _ = _build_app(tmp_path / "unconfigured")
    asgi_unconfigured = _patch_inline_anyio(monkeypatch, app_unconfigured)

    app_flex_err, _, _ = _build_app(
        tmp_path / "flex_err",
        flex_token=_FLEX_TOKEN,
        activity_id="ACT365",
        flex_fail_with=FlexServiceError("IBKR Flex returned error code 1018"),
    )
    asgi_flex_err = _patch_inline_anyio(monkeypatch, app_flex_err)

    fake_ib = _FakeLedgerIB(offline=False)
    opt_contract = Contract(
        conId=700001,
        symbol="NVDA",
        secType="OPT",
        lastTradeDateOrContractMonth="20261218",
        strike=140.0,
        right="C",
        multiplier="100",
        currency="USD",
    )
    pltr_contract = Contract(
        conId=700009,
        symbol="PLTR",
        secType="OPT",
        lastTradeDateOrContractMonth="20261218",
        strike=45.0,
        right="P",
        multiplier="100",
        currency="USD",
    )
    ts = datetime(2026, 11, 12, 15, 30, tzinfo=UTC)
    fake_ib.fills_opt = [
        Fill(
            contract=opt_contract,
            execution=Execution(
                execId="nvda-open-mtm-1",
                acctNumber="U1234567",
                time=ts,
                side="SLD",
                shares=2.0,
                price=5.40,
                orderId=9901,
            ),
            commissionReport=CommissionReport(execId="nvda-open-mtm-1", commission=1.35),
            time=ts,
        ),
        Fill(
            contract=pltr_contract,
            execution=Execution(
                execId="gw-pltr-live-1",
                acctNumber="U1234567",
                time=datetime(2026, 11, 12, 15, 45, tzinfo=UTC),
                side="BOT",
                shares=1.0,
                price=2.10,
                orderId=9902,
            ),
            commissionReport=CommissionReport(execId="gw-pltr-live-1", commission=0.85),
            time=ts,
        ),
    ]
    fake_ib.positions_result = [
        Position(account="U1234567", contract=opt_contract, position=-2.0, avgCost=540.0),
    ]

    xml_payload = _sample_roll_and_mtm_xml()
    app_ok, _, queries = _build_app(
        tmp_path / "ok_sync",
        flex_token=_FLEX_TOKEN,
        activity_id="ACT365",
        monthly_id="MON30",
        gateway_sync_enabled=True,
        fake_ib=fake_ib,
        payload_by_query={"ACT365": xml_payload, "MON30": xml_payload},
    )
    asgi_ok = _patch_inline_anyio(monkeypatch, app_ok)

    async def _exercise() -> None:
        # 1. Unconfigured sync raises ConfigurationError -> 422 JSON with detail
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_unconfigured),
            base_url="http://testserver",
        ) as client:
            r_cfg = await client.post("/api/ledger/sync")
            assert r_cfg.status_code == 422
            body_cfg = r_cfg.json()
            assert body_cfg["error_code"] == "CONFIGURATION_ERROR"
            assert body_cfg["detail"]

        # 2. Flex Web Service failure maps to 502 FLEX_SERVICE_ERROR without leaking token
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_flex_err),
            base_url="http://testserver",
        ) as client:
            r_502 = await client.post("/api/ledger/sync", json={"mode": "full"})
            assert r_502.status_code == 502
            body_502 = r_502.json()
            assert body_502["error_code"] == "FLEX_SERVICE_ERROR"
            assert _FLEX_TOKEN not in r_502.text

        # 3. Mocked Flex + Gateway sync (auto mode -> full+gateway, then monthly+gateway)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=asgi_ok),
            base_url="http://testserver",
        ) as client:
            r_sync1 = await client.post("/api/ledger/sync")
            assert r_sync1.status_code == 200
            s1 = r_sync1.json()
            assert s1["mode_used"] == "full+gateway"
            assert s1["gateway_synced"] is True
            assert s1["executions_inserted"] == 10
            assert "ACT365" in queries
            assert _FLEX_TOKEN not in r_sync1.text
            assert "U1234567" not in r_sync1.text

            r_sync2 = await client.post("/api/ledger/sync", json={"mode": "auto"})
            assert r_sync2.status_code == 200
            s2 = r_sync2.json()
            assert s2["mode_used"] == "monthly+gateway"
            assert "MON30" in queries

    run_coro(_exercise())


def test_ledger_mtm_route_offline_and_live_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_ib = _FakeLedgerIB(offline=True)
    app, _, _ = _build_app(tmp_path, fake_ib=fake_ib)
    asgi_app = _patch_inline_anyio(monkeypatch, app)

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r_imp = await client.post(
                "/api/ledger/import",
                files={"file": ("seed.xml", _sample_roll_and_mtm_xml(), "application/xml")},
            )
            assert r_imp.status_code == 200

            before_realized = (await client.get("/api/ledger?window=ALL&view=realized")).json()[
                "report"
            ]["kpis"]["realized_pnl_ex_fee"]

            # 1. Gateway offline maps cleanly to 503 GATEWAY_OFFLINE
            r_off = await client.post("/api/ledger/mtm")
            assert r_off.status_code == 503
            assert r_off.json()["error_code"] == "GATEWAY_OFFLINE"

            # 2. Bring Gateway online with tighter quote (mid = 4.00 vs 4.85 Flex mark)
            fake_ib.offline = False
            fake_ib.quote_by_conid[700001] = (3.90, 4.10, 4.00, 4.05)

            r_mtm = await client.post("/api/ledger/mtm")
            assert r_mtm.status_code == 200
            mtm_json = r_mtm.json()
            assert mtm_json["refreshed_count"] == 1
            assert mtm_json["skipped_count"] == 0

            r_open = await client.get("/api/ledger?window=ALL&view=open_mtm")
            assert r_open.status_code == 200
            open_report = r_open.json()["report"]
            # Short -2 calls sold @ 5.40, live mid = 4.00 => +$280.00 unrealized ex-fee
            assert open_report["kpis"]["unrealized_mtm_ex_fee"] == pytest.approx(280.0)
            open_row = open_report["open_positions"][0]
            assert open_row["mark_price"] == pytest.approx(4.0)
            assert open_row["mark_source"] == "LIVE_GATEWAY"

            # 3. Closed realized P&L remains completely unchanged by live MTM refresh
            after_realized = (await client.get("/api/ledger?window=ALL&view=realized")).json()[
                "report"
            ]["kpis"]["realized_pnl_ex_fee"]
            assert after_realized == pytest.approx(before_realized)

    run_coro(_exercise())


def test_ledger_import_security_and_bounded_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _, _ = _build_app(tmp_path)
    asgi_app = _patch_inline_anyio(monkeypatch, app)

    async def _exercise() -> None:
        transport = httpx.ASGITransport(app=asgi_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Unsupported extensions -> 422 INVALID_REQUEST
            for bad_name, content_type in (
                ("notes.txt", "text/plain"),
                ("ledger.json", "application/json"),
            ):
                r_ext = await client.post(
                    "/api/ledger/import",
                    files={"file": (bad_name, b"placeholder payload", content_type)},
                )
                assert r_ext.status_code == 422
                assert r_ext.json()["error_code"] == "INVALID_REQUEST"

            # 2. Malformed XML/CSV rows -> 422 STORAGE_SCHEMA_ERROR (never 500, no account leak)
            bad_xml_row = (
                b'<?xml version="1.0" encoding="UTF-8"?>\n'
                b"<FlexQueryResponse><FlexStatements>"
                b'<FlexStatement accountId="U1234567"><Trades>'
                b'<Trade accountId="U1234567" assetCategory="OPT" symbol="BAD  261218C00100000" '
                b'underlyingSymbol="../etc" strike="100" expiry="20261218" putCall="C" '
                b'dateTime="20261112;153000" quantity="-1" tradePrice="2.0" '
                b'buySell="SELL" ibExecID="bad-row-xml" />'
                b"</Trades></FlexStatement></FlexStatements></FlexQueryResponse>"
            )
            r_xml_bad = await client.post(
                "/api/ledger/import",
                files={"file": ("bad_row.xml", bad_xml_row, "application/xml")},
            )
            assert r_xml_bad.status_code == 422
            assert r_xml_bad.json()["error_code"] == "STORAGE_SCHEMA_ERROR"
            assert "U1234567" not in r_xml_bad.text
            assert "Traceback" not in r_xml_bad.text

            bad_csv_nul = (
                b"ClientAccountID,Symbol,UnderlyingSymbol,AssetClass,Quantity,TradePrice,"
                b"Buy/Sell,DateTime\n"
                b"U1234567,NVDA,NVDA,STK,10,120.00,BUY,2026-11-12\x0015:00:00\n"
            )
            r_csv_bad = await client.post(
                "/api/ledger/import",
                files={"file": ("nul_row.csv", bad_csv_nul, "text/csv")},
            )
            assert r_csv_bad.status_code == 422
            assert r_csv_bad.json()["error_code"] == "STORAGE_SCHEMA_ERROR"
            assert "U1234567" not in r_csv_bad.text
            assert "Traceback" not in r_csv_bad.text

            # 3. Bounded upload enforcement (> MAX_LEDGER_IMPORT_BYTES) -> 422 INVALID_REQUEST
            monkeypatch.setattr(web_api, "MAX_LEDGER_IMPORT_BYTES", 128)
            r_big = await client.post(
                "/api/ledger/import",
                files={"file": ("oversized.csv", b"x" * 256, "text/csv")},
            )
            assert r_big.status_code == 422
            assert r_big.json()["error_code"] == "INVALID_REQUEST"

            # Direct spy confirms api_ledger_import runs bounded read(MAX+1) and closes file
            class _SpyUploadFile:
                filename = "oversized.csv"

                def __init__(self, data: bytes) -> None:
                    self._data = data
                    self.read_sizes: list[int] = []
                    self.closed = False

                async def read(self, size: int = -1) -> bytes:
                    self.read_sizes.append(size)
                    return self._data[:size] if size >= 0 else self._data

                async def close(self) -> None:
                    self.closed = True

            spy_file = _SpyUploadFile(b"x" * 256)
            with pytest.raises(InvalidRequestError, match="20 MB"):
                await web_api.api_ledger_import(
                    file=spy_file,  # type: ignore[arg-type]
                    svc=app.state.service,
                )
            assert spy_file.read_sizes == [129]
            assert spy_file.closed is True

    run_coro(_exercise())
