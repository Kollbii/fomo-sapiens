"""Unit tests for LedgerService, AnalyzerService ledger wiring, and FlexServiceError layering."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from ib_async import CommissionReport, Contract, Execution, Fill, Position

from src.broker.client import IBKRBrokerClient
from src.broker.flex import FlexServiceError as BrokerFlexServiceError
from src.broker.flex import FlexWebServiceClient
from src.broker.ledger_sync import MAX_MARK_CONTRACTS
from src.core.config import IBKRSettings, LedgerSettings, Settings
from src.core.exceptions import (
    ConfigurationError,
    FlexServiceError,
    GatewayConnectionError,
    InvalidRequestError,
    InvalidTickerError,
    StorageSchemaError,
)
from src.services.analyzer import AnalyzerService
from src.services.ledger import LedgerService
from src.services.schemas import MAX_LEDGER_IMPORT_BYTES
from src.storage.csv_repository import CSVRepository
from src.storage.ledger_repository import LedgerRepository
from src.storage.ledger_schema import LedgerExecutionRow, LedgerPositionRow
from tests.support.aio import noop_sleep, run_coro
from tests.support.ib_fakes import FakeEvent


class _FakeServiceIB:
    """Deterministic async IB mock for Gateway sync and MTM refresh tests."""

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
                raise ConnectionRefusedError("Gateway offline")
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


def _make_flex_client(payload_by_query: dict[str, bytes]) -> tuple[FlexWebServiceClient, list[str]]:
    requested_queries: list[str] = []
    ref_to_query: dict[str, str] = {}

    async def fake_http_fetch(url: str) -> bytes:
        if "SendRequest" in url:
            qid = ""
            for part in url.split("?", 1)[-1].split("&"):
                if part.startswith("q="):
                    qid = part.split("=", 1)[1]
            requested_queries.append(qid)
            ref = f"REF-{qid}"
            ref_to_query[ref] = qid
            return (
                b"<FlexStatementResponse timestamp='20261112'>"
                b"<Status>Success</Status>"
                + f"<ReferenceCode>{ref}</ReferenceCode>".encode("ascii")
                + b"<Url>https://ndcdyn.interactivebrokers.com/GetStatement</Url>"
                b"</FlexStatementResponse>"
            )
        for ref, qid in ref_to_query.items():
            if f"q={ref}" in url:
                return payload_by_query[qid]
        return b""

    client = FlexWebServiceClient(
        token="secret-tok-xyz",
        http_fetch=fake_http_fetch,
        sleep=noop_sleep,
    )
    return client, requested_queries


def _xml_statement(
    *,
    exec_id: str = "exec-flex-001",
    trade_price: str = "5.40",
    proceeds: str = "1080.0",
    open_positions_xml: str = "",
) -> bytes:
    return (
        f"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Ledger" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" toDate="20261112">
      <Trades>
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261218C00140000" underlyingSymbol="NVDA" conid="700001"
               underlyingConid="4815747" multiplier="100" strike="140" expiry="20261218"
               putCall="C" dateTime="20261112;153000" quantity="-2" tradePrice="{trade_price}"
               proceeds="{proceeds}" ibCommission="-1.35" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="SELL" ibOrderID="9901"
               ibExecID="{exec_id}" levelOfDetail="EXECUTION" />
      </Trades>
      {open_positions_xml}
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""
    ).encode()


def _open_pos_xml(*, mark_price: str = "4.85", unrealized: str = "110.0") -> str:
    return (
        "<OpenPositions>"
        '<OpenPosition accountId="U1234567" assetCategory="OPT" levelOfDetail="SUMMARY" '
        'symbol="NVDA  261218C00140000" underlyingSymbol="NVDA" conid="700001" '
        'multiplier="100" strike="140" expiry="20261218" putCall="C" '
        f'position="-2" costBasisPrice="5.40" markPrice="{mark_price}" '
        f'positionValue="-970.0" fifoPnlUnrealized="{unrealized}" reportDate="20261112" />'
        "</OpenPositions>"
    )


def _settings(
    *,
    flex_token: str | None = "secret-tok-xyz",
    activity_id: str | None = "ACT365",
    monthly_id: str | None = "MON30",
    trade_conf_id: str | None = None,
    gateway_sync_enabled: bool = False,
) -> Settings:
    return Settings(
        ibkr=IBKRSettings(
            market_data_mode="DELAYED",
            default_market_data_type=3,
            fallback_market_data_type=4,
        ),
        ledger=LedgerSettings(
            flex_token=flex_token,
            flex_activity_query_id=activity_id,
            flex_monthly_query_id=monthly_id,
            flex_trade_conf_query_id=trade_conf_id,
            gateway_sync_enabled=gateway_sync_enabled,
        ),
    )


def test_flex_service_error_reexported_from_core_exceptions() -> None:
    assert FlexServiceError is BrokerFlexServiceError


def test_sync_mode_auto_full_then_monthly_and_config_errors(tmp_path: Path) -> None:
    fixed_now = datetime(2026, 11, 12, 16, 0, tzinfo=UTC)
    repo = LedgerRepository("data/ledger", root=tmp_path)
    flex_client, queries = _make_flex_client(
        {
            "ACT365": _xml_statement(exec_id="exec-365", open_positions_xml=_open_pos_xml()),
            "MON30": _xml_statement(exec_id="exec-mon", open_positions_xml=_open_pos_xml()),
            "TCONF": _xml_statement(exec_id="exec-tconf"),
        }
    )
    cfg = _settings(trade_conf_id="TCONF")
    svc = LedgerService(
        settings=cfg,
        repo=repo,
        flex_client=flex_client,
        now_fn=lambda: fixed_now,
    )

    # First auto sync sees empty history -> runs 365d + trade conf
    res1 = run_coro(svc.sync_ledger(mode="auto"))
    assert res1.mode_used == "full"
    assert res1.queries_run == ["FLEX_ACTIVITY_365:ACT365", "FLEX_TRADE_CONF:TCONF"]
    assert res1.executions_inserted == 2
    assert res1.open_positions_count == 1
    assert queries == ["ACT365", "TCONF"]

    status_after = svc.get_ledger_status()
    assert status_after.recommended_sync_mode == "monthly"
    assert status_after.flex_configured is True
    assert status_after.flex_monthly_configured is True
    assert status_after.flex_trade_conf_configured is True

    # Second auto sync sees existing history -> picks lighter monthly query
    res2 = run_coro(svc.sync_ledger(mode="auto"))
    assert res2.mode_used == "monthly"
    assert res2.queries_run == ["FLEX_MONTHLY:MON30", "FLEX_TRADE_CONF:TCONF"]
    assert queries == ["ACT365", "TCONF", "MON30", "TCONF"]

    # Secrets and account IDs must never leak in service responses
    dumped = res2.model_dump_json() + status_after.model_dump_json()
    assert "secret-tok-xyz" not in dumped
    assert "U1234567" not in dumped

    # Explicit monthly sync without monthly query ID fails loudly
    no_mon_svc = LedgerService(
        settings=_settings(monthly_id=None),
        repo=LedgerRepository("data/ledger", root=tmp_path / "no_mon"),
        flex_client=flex_client,
    )
    with pytest.raises(ConfigurationError, match="IBKR_FLEX_MONTHLY_QUERY_ID"):
        run_coro(no_mon_svc.sync_ledger(mode="monthly"))

    # include_gateway=True when opt-in flag is False fails loudly
    with pytest.raises(ConfigurationError, match="ENABLE_GATEWAY_LEDGER_SYNC"):
        run_coro(svc.sync_ledger(mode="full", include_gateway=True))

    # Neither Flex nor Gateway configured fails loudly
    unconfigured = LedgerService(
        settings=_settings(flex_token=None, activity_id=None, monthly_id=None),
        repo=LedgerRepository("data/ledger", root=tmp_path / "empty"),
    )
    with pytest.raises(ConfigurationError, match="Ledger sync is not configured"):
        run_coro(unconfigured.sync_ledger(mode="auto"))

    with pytest.raises(InvalidRequestError, match="Invalid sync mode"):
        run_coro(svc.sync_ledger(mode="weekly"))


def test_sync_flex_plus_gateway_dedup_and_graceful_gateway_offline(tmp_path: Path) -> None:
    repo = LedgerRepository("data/ledger", root=tmp_path)
    flex_client, _ = _make_flex_client(
        {
            "ACT365": _xml_statement(
                exec_id="exec-dup-1",
                trade_price="5.40",
                proceeds="1080.0",
                open_positions_xml=_open_pos_xml(mark_price="4.90"),
            )
        }
    )

    # Setup Gateway mock returning duplicate exec-dup-1 + new fill exec-gw-2 + position
    fake_ib = _FakeServiceIB(offline=False)
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
    new_opt = Contract(
        conId=700002,
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
                execId="exec-dup-1",
                acctNumber="U1234567",
                time=ts,
                side="SLD",
                shares=2.0,
                price=5.40,
                orderId=9901,
            ),
            commissionReport=CommissionReport(execId="exec-dup-1", commission=1.35),
            time=ts,
        ),
        Fill(
            contract=new_opt,
            execution=Execution(
                execId="exec-gw-2",
                acctNumber="U1234567",
                time=datetime(2026, 11, 12, 15, 45, tzinfo=UTC),
                side="BOT",
                shares=1.0,
                price=2.10,
                orderId=9902,
            ),
            commissionReport=CommissionReport(execId="exec-gw-2", commission=0.85),
            time=ts,
        ),
    ]
    fake_ib.positions_result = [
        Position(account="U1234567", contract=opt_contract, position=-2.0, avgCost=540.0),
    ]
    cfg = _settings(monthly_id=None, gateway_sync_enabled=True)
    broker = IBKRBrokerClient(cfg, ib_factory=lambda: fake_ib)
    svc = LedgerService(settings=cfg, repo=repo, broker_client=broker, flex_client=flex_client)

    sync_ok = run_coro(svc.sync_ledger(mode="full"))
    assert sync_ok.mode_used == "full+gateway"
    assert sync_ok.gateway_synced is True
    assert sync_ok.executions_inserted == 2
    assert sync_ok.open_positions_count == 1
    assert "U1234567" not in sync_ok.model_dump_json()

    # Duplicate exec-dup-1 must preserve settled FLEX_ACTIVITY_365 row
    execs_df = repo.load_executions()
    assert execs_df.height == 2
    dup_row = execs_df.filter(execs_df["exec_id"] == "exec-dup-1").to_dicts()[0]
    assert dup_row["source"] == "FLEX_ACTIVITY_365"
    # Merged Gateway position keeps Flex mark_price=4.90 when Gateway position lacks one
    pos_row = repo.load_positions().to_dicts()[0]
    assert pos_row["mark_price"] == pytest.approx(4.90)

    # Gateway offline degrades gracefully when Flex succeeds
    offline_ib = _FakeServiceIB(offline=True)
    offline_broker = IBKRBrokerClient(cfg, ib_factory=lambda: offline_ib)
    svc_offline = LedgerService(
        settings=cfg, repo=repo, broker_client=offline_broker, flex_client=flex_client
    )
    degraded = run_coro(svc_offline.sync_ledger(mode="full"))
    assert degraded.mode_used == "full"
    assert degraded.gateway_synced is False
    assert degraded.gateway_note is not None and "offline" in degraded.gateway_note.lower()
    assert len(degraded.notices) == 1

    # Gateway-only sync raises GatewayConnectionError when offline
    gw_only_cfg = _settings(
        flex_token=None, activity_id=None, monthly_id=None, gateway_sync_enabled=True
    )
    svc_gw_only = LedgerService(
        settings=gw_only_cfg,
        repo=LedgerRepository("data/ledger", root=tmp_path / "gw_only"),
        broker_client=IBKRBrokerClient(
            gw_only_cfg, ib_factory=lambda: _FakeServiceIB(offline=True)
        ),
    )
    with pytest.raises(GatewayConnectionError):
        run_coro(svc_gw_only.sync_ledger(mode="auto"))


def test_import_ledger_file_xml_csv_and_validation(tmp_path: Path) -> None:
    repo = LedgerRepository("data/ledger", root=tmp_path)
    svc = LedgerService(settings=_settings(), repo=repo)

    with pytest.raises(InvalidRequestError, match="empty"):
        svc.import_ledger_file("empty.csv", b"   ")

    with pytest.raises(InvalidRequestError, match="20 MB"):
        svc.import_ledger_file("huge.xml", b"x" * (MAX_LEDGER_IMPORT_BYTES + 1))

    for bad_name in (
        "notes.txt",
        "payload.json",
        "malware.exe",
        "page.html",
        "noextension",
        ".csv",
        "C:\\fakepath\\",
    ):
        with pytest.raises(InvalidRequestError, match="Unsupported ledger file extension"):
            svc.import_ledger_file(bad_name, b"a,b\n1,2\n")

    csv_payload = (
        b"ClientAccountID,Symbol,UnderlyingSymbol,AssetClass,Put/Call,Strike,Expiry,"
        b"Multiplier,Buy/Sell,Open/CloseIndicator,Quantity,TradePrice,Proceeds,"
        b"IBCommission,FifoPnlRealized,DateTime,IBExecID,Conid\n"
        b"U1234567,NVDA  261218C00140000,NVDA,OPT,C,140,20261218,100,SELL,O,-1,5.00,500,-1.0,0,"
        b"2026-11-10 14:30:00,csv-exec-1,700001\n"
    )
    csv_res = svc.import_ledger_file("../../unsafe/path/activity.csv", csv_payload)
    assert csv_res.filename == "activity.csv"
    assert csv_res.file_format == "CSV"
    assert csv_res.executions_inserted == 1

    win_res = svc.import_ledger_file(r"C:\fakepath\report.csv", csv_payload)
    assert win_res.filename == "report.csv"
    assert win_res.file_format == "CSV"

    with pytest.raises(StorageSchemaError):
        svc.import_ledger_file("bad.csv", b"Symbol,AssetClass\x00\nNVDA,OPT\n")

    xml_res = svc.import_ledger_file(
        "flex.xml",
        _xml_statement(exec_id="xml-exec-2", open_positions_xml=_open_pos_xml()),
    )
    assert xml_res.file_format == "XML"
    assert xml_res.executions_inserted == 1
    assert xml_res.open_positions_imported == 1
    assert xml_res.total_executions == 2


def test_refresh_open_mtm_updates_unrealized_only_and_cap_notice(tmp_path: Path) -> None:
    fixed_now = datetime(2026, 11, 12, 16, 0, tzinfo=UTC)
    repo = LedgerRepository("data/ledger", root=tmp_path)
    repo.upsert_executions(
        [
            LedgerExecutionRow(
                exec_id="mtm-open-1",
                symbol="NVDA 261218C140",
                underlying="NVDA",
                asset_class="OPT",
                expiry="20261218",
                strike=140.0,
                right="C",
                multiplier=100,
                side="SLD",
                quantity=2.0,
                price=5.40,
                proceeds=1080.0,
                commission=1.35,
                realized_pnl_ib=0.0,
                open_close="O",
                executed_at="2026-11-12T14:00:00Z",
                source="FLEX_IMPORT",
                conid=700001,
            )
        ]
    )
    repo.replace_positions(
        [
            LedgerPositionRow(
                conid=700001,
                symbol="NVDA 261218C140",
                underlying="NVDA",
                asset_class="OPT",
                expiry="20261218",
                strike=140.0,
                right="C",
                multiplier=100,
                position=-2.0,
                avg_cost=5.40,
                mark_price=4.85,
                unrealized_pnl=110.0,
                source="FLEX_IMPORT",
                updated_at="2026-11-12T14:00:00Z",
            )
        ]
    )

    fake_ib = _FakeServiceIB()
    fake_ib.quote_by_conid[700001] = (3.9, 4.1, 4.0, 4.05)  # mid = 4.0
    cfg = _settings()
    broker = IBKRBrokerClient(cfg, ib_factory=lambda: fake_ib)
    svc = LedgerService(settings=cfg, repo=repo, broker_client=broker, now_fn=lambda: fixed_now)

    before_exec_csv = repo.executions_path.read_text(encoding="utf-8")
    before_dash = svc.get_ledger_dashboard(window="ALL", view="open_mtm")
    assert before_dash.report.kpis.realized_pnl_ex_fee == pytest.approx(0.0)
    assert before_dash.report.kpis.unrealized_mtm_ex_fee == pytest.approx(110.0)

    mtm = run_coro(svc.refresh_open_mtm())
    assert mtm.refreshed_count == 1
    assert mtm.marks_by_conid[700001] == pytest.approx(4.0)
    assert repo.executions_path.read_text(encoding="utf-8") == before_exec_csv

    after_dash = svc.get_ledger_dashboard(window="ALL", view="open_mtm")
    # Realized P&L stays untouched; live MTM updates in-memory to (5.40 - 4.00) * 2 * 100 = 280.0
    assert after_dash.report.kpis.realized_pnl_ex_fee == pytest.approx(0.0)
    assert after_dash.report.kpis.unrealized_mtm_ex_fee == pytest.approx(280.0)

    # > MAX_MARK_CONTRACTS emits cap notice and caps refreshed batch at 60
    many_positions = [
        LedgerPositionRow(
            conid=800000 + idx,
            symbol=f"NVDA 261218C{100 + idx}",
            underlying="NVDA",
            asset_class="OPT",
            expiry="20261218",
            strike=float(100 + idx),
            right="C",
            multiplier=100,
            position=-1.0,
            avg_cost=5.0,
            source="FLEX_IMPORT",
            updated_at="2026-11-12T14:00:00Z",
        )
        for idx in range(MAX_MARK_CONTRACTS + 5)
    ]
    cap_repo = LedgerRepository("data/ledger", root=tmp_path / "cap")
    cap_repo.replace_positions(many_positions)
    cap_svc = LedgerService(settings=cfg, repo=cap_repo, broker_client=broker, sleep=noop_sleep)
    cap_res = run_coro(cap_svc.refresh_open_mtm())
    assert cap_res.refreshed_count == MAX_MARK_CONTRACTS
    assert cap_res.skipped_count == 5
    assert any("Capped live MTM refresh" in notice for notice in cap_res.notices)


def test_dashboard_validation_and_analyzer_service_delegation(tmp_path: Path) -> None:
    cfg = _settings(trade_conf_id="TCONF", gateway_sync_enabled=True)
    flex_client, _ = _make_flex_client(
        {
            "ACT365": _xml_statement(exec_id="az-exec-1", open_positions_xml=_open_pos_xml()),
            "TCONF": _xml_statement(exec_id="az-exec-2"),
        }
    )
    fake_ib = _FakeServiceIB()
    broker = IBKRBrokerClient(cfg, ib_factory=lambda: fake_ib)
    chain_repo = CSVRepository(cfg.storage, root=tmp_path)
    ledger_repo = LedgerRepository(cfg.ledger.ledger_dir, root=tmp_path)
    ledger_svc = LedgerService(
        settings=cfg,
        repo=ledger_repo,
        broker_client=broker,
        flex_client=flex_client,
        now_fn=lambda: datetime(2026, 11, 12, 16, 0, tzinfo=UTC),
    )
    analyzer = AnalyzerService(
        settings=cfg,
        repo=chain_repo,
        broker_client=broker,
        ledger_service=ledger_svc,
    )
    analyzer.ensure_layout()
    assert (tmp_path / "data" / "ledger").is_dir()

    status = analyzer.get_status()
    assert status.flex_configured is True
    assert status.flex_monthly_configured is True
    assert status.flex_trade_conf_configured is True
    assert status.gateway_ledger_sync_enabled is True
    assert "secret-tok-xyz" not in status.model_dump_json()

    # Dashboard filter validation
    with pytest.raises(InvalidRequestError, match="window"):
        analyzer.get_ledger_dashboard(window="2D")
    with pytest.raises(InvalidRequestError, match="group_by"):
        analyzer.get_ledger_dashboard(group_by="month")
    with pytest.raises(InvalidRequestError, match="view"):
        analyzer.get_ledger_dashboard(view="unrealized")
    with pytest.raises(InvalidTickerError):
        analyzer.get_ledger_dashboard(ticker="BAD$$$")

    sync_res = run_coro(analyzer.sync_ledger(mode="full", include_gateway=False))
    assert sync_res.total_executions == 2

    imp_res = analyzer.import_ledger_file("extra.xml", _xml_statement(exec_id="az-exec-3"))
    assert imp_res.total_executions == 3

    mtm_res = run_coro(analyzer.refresh_open_ledger_mtm())
    assert mtm_res.refreshed_count == 1

    dash = analyzer.get_ledger_dashboard(window="ALL", ticker="NVDA", group_by="executions")
    assert dash.report.ticker == "NVDA"
    assert len(dash.report.executions) == 3

    # Default LedgerRepository created by AnalyzerService must also isolate under repo.root
    default_analyzer = AnalyzerService(
        settings=cfg, repo=CSVRepository(cfg.storage, root=tmp_path / "iso")
    )
    assert default_analyzer.ledger.repo.executions_path.is_relative_to(tmp_path / "iso")
    assert (tmp_path / "iso" / "data" / "ledger").is_dir()
