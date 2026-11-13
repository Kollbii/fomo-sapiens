"""Unit tests for Flex Web Service client, XML/CSV parsers, ledger schemas, and repository."""

from __future__ import annotations

import http.client
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.broker.flex import (
    FlexServiceError,
    FlexWebServiceClient,
    parse_activity_csv,
    parse_flex_xml,
    redact_flex_secrets,
    select_flex_query_ids,
)
from src.core.config import LedgerSettings
from src.core.exceptions import ConfigurationError, StorageSchemaError
from src.storage.ledger_repository import LedgerRepository
from src.storage.ledger_schema import (
    LEDGER_EXECUTION_POLARS_SCHEMA,
    LEDGER_POSITION_POLARS_SCHEMA,
    LedgerExecutionRow,
    LedgerPositionRow,
)
from tests.support.aio import noop_sleep, run_coro


def test_select_flex_query_ids_modes() -> None:
    cfg_all = LedgerSettings(
        flex_token="tok",
        flex_activity_query_id="ACT365",
        flex_monthly_query_id="MON30",
        flex_trade_conf_query_id="TCONF",
    )
    assert select_flex_query_ids(cfg_all, mode="auto", has_existing_history=False) == [
        ("ACT365", "FLEX_ACTIVITY_365"),
        ("TCONF", "FLEX_TRADE_CONF"),
    ]
    assert select_flex_query_ids(cfg_all, mode="auto", has_existing_history=True) == [
        ("MON30", "FLEX_MONTHLY"),
        ("TCONF", "FLEX_TRADE_CONF"),
    ]
    assert select_flex_query_ids(cfg_all, mode="full", has_existing_history=True) == [
        ("ACT365", "FLEX_ACTIVITY_365"),
        ("TCONF", "FLEX_TRADE_CONF"),
    ]
    assert select_flex_query_ids(cfg_all, mode="monthly", has_existing_history=False) == [
        ("MON30", "FLEX_MONTHLY"),
        ("TCONF", "FLEX_TRADE_CONF"),
    ]

    # Auto falls back to 365d when monthly placeholder is unset
    cfg_no_monthly = LedgerSettings(
        flex_token="tok",
        flex_activity_query_id="ACT365",
        flex_monthly_query_id=None,
    )
    assert select_flex_query_ids(cfg_no_monthly, mode="auto", has_existing_history=True) == [
        ("ACT365", "FLEX_ACTIVITY_365")
    ]
    with pytest.raises(ConfigurationError, match="IBKR_FLEX_MONTHLY_QUERY_ID"):
        select_flex_query_ids(cfg_no_monthly, mode="monthly", has_existing_history=True)

    with pytest.raises(ConfigurationError, match="IBKR_FLEX_ACTIVITY_QUERY_ID"):
        select_flex_query_ids(LedgerSettings(), mode="full", has_existing_history=False)


def test_flex_web_client_polls_1019_and_redacts_secrets() -> None:
    urls_called: list[str] = []
    sleep_calls: list[float] = []

    async def fake_sleep(sec: float) -> None:
        sleep_calls.append(sec)

    responses = iter(
        [
            (
                b"<FlexStatementResponse timestamp='20261112'>"
                b"<Status>Success</Status>"
                b"<ReferenceCode>REF-999</ReferenceCode>"
                b"<Url>https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>"
                b"</FlexStatementResponse>"
            ),
            (
                b"<FlexStatementResponse>"
                b"<Status>Warn</Status><ErrorCode>1019</ErrorCode>"
                b"<ErrorMessage>Statement generation in progress.</ErrorMessage>"
                b"</FlexStatementResponse>"
            ),
            (
                b"<FlexStatementResponse>"
                b"<Status>Warn</Status><ErrorCode>1019</ErrorCode>"
                b"<ErrorMessage>Statement generation in progress.</ErrorMessage>"
                b"</FlexStatementResponse>"
            ),
            b"<FlexQueryResponse queryName='Ledger365' type='AF'>"
            b"<FlexStatements/></FlexQueryResponse>",
        ]
    )

    async def fake_fetch(url: str) -> bytes:
        urls_called.append(url)
        return next(responses)

    client = FlexWebServiceClient(
        token="secret-flex-tok-xyz",
        http_fetch=fake_fetch,
        poll_interval_sec=0.5,
        max_poll_attempts=5,
        sleep=fake_sleep,
    )
    xml_out = run_coro(client.fetch_statement("123456"))
    assert b"<FlexQueryResponse" in xml_out
    assert len(urls_called) == 4
    assert "t=secret-flex-tok-xyz" in urls_called[0]
    assert "q=123456" in urls_called[0]
    assert "q=REF-999" in urls_called[1]
    assert sleep_calls == [0.5, 0.5]

    # Permanent error must redact both token and accountId
    async def error_fetch(url: str) -> bytes:
        if "SendRequest" in url:
            return (
                b"<FlexStatementResponse><Status>Fail</Status><ErrorCode>1012</ErrorCode>"
                b"<ErrorMessage>Token secret-flex-tok-xyz invalid for acctId='U9988776' "
                b"at https://example.com?t=secret-flex-tok-xyz</ErrorMessage>"
                b"</FlexStatementResponse>"
            )
        return b""

    err_client = FlexWebServiceClient(
        token="secret-flex-tok-xyz", http_fetch=error_fetch, sleep=noop_sleep
    )
    with pytest.raises(FlexServiceError) as exc_info:
        run_coro(err_client.fetch_statement("123456"))
    msg = str(exc_info.value)
    assert "secret-flex-tok-xyz" not in msg
    assert "U9988776" not in msg
    assert "[REDACTED]" in msg

    # Missing token raises ConfigurationError
    no_tok = FlexWebServiceClient(http_fetch=fake_fetch, sleep=noop_sleep)
    with pytest.raises(ConfigurationError):
        run_coro(no_tok.fetch_statement("123456"))


def test_parse_flex_xml_trades_trade_confirms_option_eae_and_positions() -> None:
    xml_payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="Ledger" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" toDate="20261112">
      <Trades>
        <Trade accountId="U1234567" currency="USD" fxRateToBase="1" assetCategory="OPT"
               symbol="NVDA  261218C00140000" underlyingSymbol="NVDA" conid="700001"
               underlyingConid="4815747" multiplier="100" strike="140" expiry="20261218"
               putCall="C" dateTime="20261112;153000" quantity="-2" tradePrice="5.40"
               proceeds="1080.0" ibCommission="-1.35" fifoPnlRealized="0"
               openCloseIndicator="O" buySell="SELL" ibOrderID="9901"
               ibExecID="0000e0d5.672a1b3c.01.01" levelOfDetail="EXECUTION" />
        <Trade accountId="U1234567" assetCategory="OPT" symbol="NVDA  261218C00140000"
               underlyingSymbol="NVDA" strike="140" expiry="20261218" putCall="C"
               dateTime="20261112;153000" quantity="-2" tradePrice="5.40"
               buySell="SELL" levelOfDetail="ORDER" />
        <Trade accountId="U1234567" currency="USD" assetCategory="STK"
               symbol="AVGO" underlyingSymbol="AVGO" conid="4350"
               dateTime="2026-11-12 14:00:00" quantity="50" tradePrice="180.00"
               ibCommission="-1.00" buySell="BUY" ibExecID="0000e0d5.672a0001.01.01" />
        <Trade accountId="U1234567" currency="USD" assetCategory="FUT"
               symbol="ESZ6" dateTime="20261112;120000" quantity="1" tradePrice="5900"
               buySell="BUY" ibExecID="fut-skip-1" />
      </Trades>
      <TradeConfirms>
        <TradeConfirm accountId="U1234567" assetCategory="OPT"
                      symbol="PLTR  261218P00045000" underlyingSymbol="PLTR" conid="700009"
                      multiplier="100" strike="45" expiry="20261218" putCall="P"
                      dateTime="20261112;161500" quantity="3" tradePrice="2.10"
                      ibCommission="-2.10" buySell="BUY" openCloseIndicator="O"
                      ibOrderID="9902" ibExecID="tc-pltr-1" />
      </TradeConfirms>
      <OptionEAE>
        <OptionEAE accountId="U1234567" assetCategory="OPT"
                   symbol="NVDA  261106P00130000" underlyingSymbol="NVDA" conid="700002"
                   multiplier="100" strike="130" expiry="20261106" putCall="P"
                   date="20261106" transactionType="Expiration" quantity="2"
                   tradePrice="0" proceeds="0" commisions="0" realizedPnl="420.0" />
        <OptionEAE accountId="U1234567" assetCategory="OPT"
                   symbol="CRDO  261106P00040000" underlyingSymbol="CRDO"
                   multiplier="100" strike="40" expiry="20261106" putCall="P"
                   date="20261106" transactionType="Assignment" quantity="1"
                   tradePrice="0" proceeds="0" commisions="-0.25" />
        <OptionEAE accountId="U1234567" assetCategory="OPT"
                   symbol="AVGO  261106C00170000" underlyingSymbol="AVGO"
                   multiplier="100" strike="170" expiry="20261106" putCall="C"
                   date="20261106" transactionType="Exercise" quantity="-1"
                   tradePrice="0" proceeds="0" />
      </OptionEAE>
      <OpenPositions>
        <OpenPosition accountId="U1234567" assetCategory="OPT" levelOfDetail="SUMMARY"
                      symbol="NVDA  261218C00140000" underlyingSymbol="NVDA" conid="700001"
                      multiplier="100" strike="140" expiry="20261218" putCall="C"
                      position="-2" costBasisPrice="5.40" markPrice="4.85"
                      positionValue="-970.0" fifoPnlUnrealized="110.0" reportDate="20261112" />
        <OpenPosition accountId="U1234567" assetCategory="OPT" levelOfDetail="LOT"
                      symbol="NVDA  261218C00140000" underlyingSymbol="NVDA"
                      strike="140" expiry="20261218" putCall="C" position="-2" />
        <OpenPosition accountId="U1234567" assetCategory="CASH" symbol="USD" position="5000" />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""
    execs, positions = parse_flex_xml(xml_payload, default_source="FLEX_ACTIVITY_365")
    # 2 Trades (ORDER skipped when EXECUTION present, FUT skipped) + 1 TradeConfirm + 3 OptionEAE
    assert len(execs) == 6
    by_event = {e.event_code: e for e in execs}
    assert {"TRADE", "EXPIRED", "ASSIGNED", "EXERCISED"} <= set(by_event)

    opt_trade = execs[0]
    assert opt_trade.exec_id == "0000e0d5.672a1b3c.01.01"
    assert opt_trade.commission == 1.35
    assert opt_trade.side == "SLD"
    assert opt_trade.open_close == "O"
    assert opt_trade.executed_at == "2026-11-12T15:30:00Z"

    stk_trade = execs[1]
    assert stk_trade.asset_class == "STK"
    assert stk_trade.multiplier == 1
    assert stk_trade.proceeds == pytest.approx(-9000.0)
    assert stk_trade.expiry is None

    tc_trade = execs[2]
    assert tc_trade.underlying == "PLTR"
    assert tc_trade.proceeds == pytest.approx(-630.0)

    exp_row = by_event["EXPIRED"]
    assert exp_row.exec_id.startswith("sha256:")
    assert exp_row.open_close == "C"
    assert exp_row.realized_pnl_ib == 420.0
    assert by_event["ASSIGNED"].commission == 0.25
    assert by_event["EXERCISED"].side == "SLD"

    assert len(positions) == 1
    assert positions[0].underlying == "NVDA"
    assert positions[0].position == -2.0
    assert positions[0].unrealized_pnl == 110.0
    assert "U1234567" not in repr(execs)
    assert "U1234567" not in repr(positions)


def test_parse_activity_csv_flat_and_multisection() -> None:
    flat_csv = (
        "ClientAccountID,Symbol,UnderlyingSymbol,AssetClass,Expiry,Strike,Put/Call,Multiplier,"
        "Buy/Sell,Quantity,TradePrice,Proceeds,IBCommission,Open/CloseIndicator,Date/Time\n"
        "U12345,NVDA  261218C00140000,NVDA,OPT,20261218,140,C,100,"
        "SELL,-1,5.25,,,-O,20261112;153000\n"
        "U12345,NVDA  261218C00140000,NVDA,OPT,20261218,140,C,100,"
        "SELL,-1,5.25,525,-1.10,O,20261112;153000\n"
        "U12345,EUR.USD,EUR,CASH,,,,,,,1000,1.08,,,,20261112;153000\n"
        "U12345,Total,,OPT,,,,,,,,,,,,\n"
    )
    # Fix first row's O/C indicator (`-O` -> `O`)
    flat_csv = flat_csv.replace(",-O,", ",O,")
    flat_execs, flat_pos = parse_activity_csv(flat_csv, default_source="FLEX_IMPORT")
    assert len(flat_execs) == 2
    assert flat_pos == []
    assert flat_execs[0].exec_id.startswith("sha256:")
    assert flat_execs[0].proceeds == pytest.approx(525.0)
    assert flat_execs[1].commission == pytest.approx(1.10)
    # Two identical rows without IBExecID remain distinct deterministically
    assert flat_execs[0].exec_id != flat_execs[1].exec_id
    flat_execs_again, _ = parse_activity_csv(flat_csv, default_source="FLEX_IMPORT")
    assert [r.exec_id for r in flat_execs] == [r.exec_id for r in flat_execs_again]

    activity_csv = (
        "Statement,Header,Field Name,Field Value\n"
        'Statement,Data,Period,"November 1, 2026 - November 12, 2026"\n'
        "Account Information,Header,Field Name,Field Value\n"
        "Account Information,Data,Account,U9999999\n"
        "Trades,Header,DataDiscriminator,Asset Category,Currency,Symbol,Date/Time,Quantity,"
        "T. Price,C. Price,Proceeds,Comm/Fee,Basis,Realized P/L,MTM P/L,Code\n"
        'Trades,Data,Order,Equity and Index Options,USD,NVDA 18DEC26 140 C,"2026-11-12, 15:30:00",'
        "-2,5.40,5.20,1080.00,-1.35,-1078.65,0,40,O\n"
        'Trades,Data,Order,Equity and Index Options,USD,NVDA 06NOV26 130 P,"2026-11-06, 16:00:00",'
        "2,0,0,0,0,-400,400,0,C;Ep\n"
        'Trades,Data,Order,Stocks,USD,AVGO,"2026-11-12, 14:00:00",'
        "100,180.0,181.0,-18000,-1.0,18001,0,100,O\n"
        'Trades,Data,Order,Forex,USD,EUR.USD,"2026-11-12, 12:00:00",1000,1.08,,0,-2,,,,\n'
        "Trades,SubTotal,,Equity and Index Options,USD,NVDA 18DEC26 140 C,,-2,,,1080,-1.35,,,,\n"
        "Trades,Total,,Equity and Index Options,USD,,,,,1080,-1.35,,,,\n"
        "Open Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,"
        "Cost Price,Cost Basis,Close Price,Value,Unrealized P/L,Code\n"
        "Open Positions,Data,Summary,Equity and Index Options,USD,NVDA 18DEC26 140 C,"
        "-2,100,5.393,-1078.65,4.85,-970.0,108.65,\n"
        "Open Positions,Total,,Equity and Index Options,USD,,,,,-1078.65,,-970.0,108.65,\n"
    )
    act_execs, act_pos = parse_activity_csv(activity_csv, default_source="FLEX_IMPORT")
    assert len(act_execs) == 3
    assert act_execs[0].underlying == "NVDA"
    assert act_execs[0].expiry == "20261218"
    assert act_execs[0].strike == 140.0
    assert act_execs[0].right == "C"
    assert act_execs[0].side == "SLD"
    assert act_execs[0].open_close == "O"
    assert act_execs[1].event_code == "EXPIRED"
    assert act_execs[1].open_close == "C"
    assert act_execs[2].asset_class == "STK"
    assert act_execs[2].multiplier == 1

    assert len(act_pos) == 1
    assert act_pos[0].position == -2.0
    assert act_pos[0].mark_price == 4.85
    assert act_pos[0].updated_at == "2026-11-12T00:00:00Z"


def test_ledger_schema_rejects_account_id_and_invalid_rows() -> None:
    with pytest.raises(ValidationError):
        LedgerExecutionRow(
            exec_id="e1",
            symbol="NVDA  261218C00140000",
            underlying="NVDA",
            asset_class="OPT",
            expiry="20261218",
            strike=140.0,
            right="C",
            side="BOT",
            quantity=1,
            price=5.0,
            proceeds=-500.0,
            executed_at="2026-11-12T15:30:00Z",
            source="FLEX_IMPORT",
            accountId="U123",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        LedgerPositionRow(
            symbol="NVDA",
            underlying="../etc",
            asset_class="STK",
            position=10,
            source="FLEX_IMPORT",
            updated_at="2026-11-12T15:30:00Z",
        )


def test_ledger_repository_upsert_dedup_gateway_upgrade_and_sync_state(tmp_path: Path) -> None:
    repo = LedgerRepository(tmp_path / "ledger")
    repo.ensure_layout()
    assert repo.ledger_dir.is_dir()
    assert repo.imports_dir.is_dir()

    # Empty reads return properly typed Polars frames
    empty_exec = repo.load_executions()
    empty_pos = repo.load_positions()
    assert empty_exec.height == 0
    assert empty_exec.schema == LEDGER_EXECUTION_POLARS_SCHEMA
    assert empty_pos.height == 0
    assert empty_pos.schema == LEDGER_POSITION_POLARS_SCHEMA
    assert repo.load_sync_state()["recommended_incremental_mode"] == "full"

    # 1. Insert provisional GATEWAY_LIVE fill (no commission/open_close yet)
    gw_row = LedgerExecutionRow(
        exec_id="0000e0d5.672a1b3c.01.01",
        ib_order_id="101",
        conid=700001,
        symbol="NVDA  261218C00140000",
        underlying="NVDA",
        asset_class="OPT",
        expiry="20261218",
        strike=140.0,
        right="C",
        multiplier=100,
        side="SLD",
        quantity=2,
        price=5.40,
        proceeds=1080.0,
        commission=0.0,
        open_close=None,
        executed_at="2026-11-12T15:30:42Z",
        source="GATEWAY_LIVE",
    )
    ins, upd = repo.upsert_executions([gw_row])
    assert (ins, upd) == (1, 0)

    # 2. Authoritative FLEX_IMPORT row arrives (synthetic sha256 exec_id, same minute)
    #    -> upgrades the GATEWAY_LIVE fill seamlessly without double-counting!
    flex_upgrade = LedgerExecutionRow(
        exec_id="sha256:flexdeadbeef0011223344",
        ib_order_id="9901",
        symbol="NVDA 18DEC26 140 C",
        underlying="NVDA",
        asset_class="OPT",
        expiry="20261218",
        strike=140.0,
        right="C",
        multiplier=100,
        side="SLD",
        quantity=2,
        price=5.40,
        proceeds=1080.0,
        commission=1.35,
        open_close="O",
        event_code="TRADE",
        executed_at="2026-11-12T15:30:00Z",
        source="FLEX_IMPORT",
    )
    stk_earlier = LedgerExecutionRow(
        exec_id="stk-1",
        symbol="AVGO",
        underlying="AVGO",
        asset_class="STK",
        side="BOT",
        quantity=10,
        price=180.0,
        proceeds=-1800.0,
        commission=1.0,
        executed_at="2026-11-12T14:00:00Z",
        source="FLEX_ACTIVITY_365",
    )
    ins2, upd2 = repo.upsert_executions([flex_upgrade, stk_earlier])
    assert (ins2, upd2) == (1, 1)

    df = repo.load_executions()
    assert df.height == 2
    # Sorted deterministically by (executed_at, exec_id)
    assert df["exec_id"].to_list() == ["stk-1", "sha256:flexdeadbeef0011223344"]
    upgraded = df.row(1, named=True)
    assert upgraded["source"] == "FLEX_IMPORT"
    assert upgraded["commission"] == pytest.approx(1.35)
    assert upgraded["open_close"] == "O"
    assert upgraded["ib_order_id"] == "9901"
    assert upgraded["conid"] == 700001  # preserved from Gateway fill

    # 3. Secondary match by exec_id suffix also upgrades GATEWAY_LIVE even if timestamp differs > 1m
    gw_suffix = LedgerExecutionRow(
        exec_id="0000ffff.99887766.01.01",
        symbol="PLTR  261218P00045000",
        underlying="PLTR",
        asset_class="OPT",
        expiry="20261218",
        strike=45.0,
        right="P",
        side="BOT",
        quantity=1,
        price=2.10,
        proceeds=-210.0,
        executed_at="2026-11-12T16:15:00Z",
        source="GATEWAY_LIVE",
    )
    repo.upsert_executions([gw_suffix])
    fx_suffix = gw_suffix.model_copy(
        update={
            "exec_id": "99887766.01.01",
            "commission": 0.75,
            "open_close": "O",
            "executed_at": "2026-11-12T16:17:00Z",
            "source": "FLEX_MONTHLY",
        }
    )
    ins3, upd3 = repo.upsert_executions([fx_suffix])
    assert (ins3, upd3) == (0, 1)
    assert repo.load_executions().height == 3

    # 4. Re-running Gateway sync for already-settled Flex row must NOT double-count
    ins4, upd4 = repo.upsert_executions([gw_row])
    assert (ins4, upd4) == (0, 0)
    assert repo.load_executions().height == 3

    # 5. Atomic replace_positions + sync_state
    pos_count = repo.replace_positions(
        [
            LedgerPositionRow(
                conid=700001,
                symbol="NVDA  261218C00140000",
                underlying="NVDA",
                asset_class="OPT",
                expiry="20261218",
                strike=140.0,
                right="C",
                position=-2,
                avg_cost=5.40,
                mark_price=4.85,
                market_value=-970.0,
                unrealized_pnl=110.0,
                source="FLEX_IMPORT",
                updated_at="2026-11-12T16:20:00Z",
            )
        ]
    )
    assert pos_count == 1
    assert repo.load_positions().height == 1

    state = repo.save_sync_state(last_sync_at="2026-11-12T16:20:00Z", last_sync_mode="full")
    assert state["total_executions"] == 3
    assert state["open_positions_count"] == 1
    assert state["recommended_incremental_mode"] == "monthly"
    assert state["source_counts"] == {
        "FLEX_ACTIVITY_365": 1,
        "FLEX_IMPORT": 1,
        "FLEX_MONTHLY": 1,
    }
    assert list(repo.ledger_dir.glob(".*.tmp")) == []

    # Invalid dict rejected with StorageSchemaError
    bad_dict: dict[str, Any] = {"exec_id": ""}
    with pytest.raises(StorageSchemaError):
        repo.upsert_executions([bad_dict])


def test_flex_parsers_wrap_malformed_rows_and_redact_unquoted_secrets() -> None:
    # Unquoted and colon-separated account IDs must be redacted alongside quoted attrs
    raw_msg = (
        "Failed for acctId=U12345 accountId: U9988776 account=U5551234, "
        "ClientAccountID='U7770001' at https://ndcdyn.interactivebrokers.com?t=tok-abc-123"
    )
    redacted = redact_flex_secrets(raw_msg, token="tok-abc-123")
    for secret in ("U12345", "U9988776", "U5551234", "U7770001", "tok-abc-123"):
        assert secret not in redacted
    assert redacted.count("[REDACTED]") >= 5

    # http.client.HTTPException is mapped to FlexServiceError with secrets redacted
    async def http_exc_fetch(_url: str) -> bytes:
        raise http.client.RemoteDisconnected("peer closed for acctId=U12345?t=tok-abc-123")

    exc_client = FlexWebServiceClient(
        token="tok-abc-123", http_fetch=http_exc_fetch, sleep=noop_sleep
    )
    with pytest.raises(FlexServiceError) as exc_info:
        run_coro(exc_client.fetch_statement("123456"))
    assert "U12345" not in str(exc_info.value)
    assert "tok-abc-123" not in str(exc_info.value)

    # Malformed XML row: path-traversal underlyingSymbol wraps ValidationError as StorageSchemaError
    bad_xml_underlying = (
        b"<FlexQueryResponse><FlexStatements><FlexStatement accountId='U1234567'><Trades>"
        b"<Trade assetCategory='OPT' symbol='NVDA 261218C140' underlyingSymbol='../etc' "
        b"strike='140' expiry='20261218' putCall='C' dateTime='20261112;153000' "
        b"quantity='-1' tradePrice='5.0' buySell='SELL' />"
        b"</Trades></FlexStatement></FlexStatements></FlexQueryResponse>"
    )
    with pytest.raises(StorageSchemaError, match="Malformed ledger row/payload"):
        parse_flex_xml(bad_xml_underlying)

    # Malformed XML row: invalid date & bad strike
    bad_xml_date = (
        b"<FlexQueryResponse><FlexStatements><FlexStatement><Trades>"
        b"<Trade assetCategory='OPT' symbol='NVDA 261218C140' underlyingSymbol='NVDA' "
        b"strike='-10' expiry='20261218' putCall='C' dateTime='not-a-date' "
        b"quantity='-1' tradePrice='5.0' buySell='SELL' />"
        b"</Trades></FlexStatement></FlexStatements></FlexQueryResponse>"
    )
    with pytest.raises(StorageSchemaError, match="Malformed ledger row/payload"):
        parse_flex_xml(bad_xml_date)

    # Malformed CSV row: bad underlying + NUL byte csv.Error both raise StorageSchemaError
    bad_csv_underlying = (
        "ClientAccountID,Symbol,UnderlyingSymbol,AssetClass,Put/Call,Strike,Expiry,"
        "Buy/Sell,Quantity,TradePrice,DateTime\n"
        "U1234567,BAD,../etc,OPT,C,140,20261218,SELL,-1,5.0,2026-11-12 15:30:00\n"
    )
    with pytest.raises(StorageSchemaError, match="Malformed ledger row/payload"):
        parse_activity_csv(bad_csv_underlying)

    with pytest.raises(StorageSchemaError, match="Malformed ledger row/payload"):
        parse_activity_csv(b"Symbol,AssetClass\x00\nNVDA,OPT\n")

    bad_csv_price = (
        "ClientAccountID,Symbol,UnderlyingSymbol,AssetClass,Put/Call,Strike,Expiry,"
        "Buy/Sell,Quantity,TradePrice,DateTime\n"
        "U1234567,NVDA 261218C140,NVDA,OPT,C,140,20261218,SELL,-1,not-a-price,2026-11-12 15:30:00\n"
    )
    with pytest.raises(StorageSchemaError, match="Malformed ledger row/payload"):
        parse_activity_csv(bad_csv_price)

    with pytest.raises(UnicodeDecodeError):
        parse_activity_csv(b"\xff\xfeinvalid-utf8")


def test_parse_flex_xml_missing_asset_category_inference_and_fx_filtering() -> None:
    xml_payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="MissingAssetCat" type="AF">
  <FlexStatements count="1">
    <FlexStatement toDate="20261112">
      <Trades>
        <!-- Option inferred from option attrs without assetCategory -->
        <Trade symbol="AAPL" strike="220" expiry="20261218" putCall="C"
               dateTime="20261112;150000" quantity="-1" tradePrice="4.50"
               buySell="SELL" openCloseIndicator="O" tradeID="opt-attr-1" />
        <!-- Option inferred from OCC symbol without assetCategory or option attrs -->
        <Trade symbol="MSFT  261218P00420000" dateTime="20261112;150500"
               quantity="2" tradePrice="6.20" buySell="BUY" tradeID="opt-sym-1" />
        <!-- Stock inferred without assetCategory (even with strike="0") -->
        <Trade symbol="NVDA" strike="0" dateTime="20261112;151000"
               quantity="10" tradePrice="140.0" buySell="BUY" tradeID="stk-nvda-1" />
        <!-- Dotted stock tickers must NOT be treated as FX pairs -->
        <Trade symbol="BRK.B" dateTime="20261112;151500"
               quantity="5" tradePrice="450.0" buySell="BUY" tradeID="stk-brkb-1" />
        <Trade symbol="BF.B" dateTime="20261112;152000"
               quantity="8" tradePrice="45.0" buySell="BUY" tradeID="stk-bfb-1" />
        <!-- FX currency pairs without assetCategory must be skipped -->
        <Trade symbol="USD.PLN" currency="PLN" dateTime="20261112;152500"
               quantity="1000" tradePrice="4.05" buySell="BUY" tradeID="fx-skip-1" />
        <Trade symbol="eur.usd" underlyingSymbol="EUR" currency="USD"
               dateTime="20261112;152600" quantity="-500" tradePrice="1.08"
               buySell="SELL" tradeID="fx-skip-2" />
        <Trade symbol="GBP.USD" underlyingSymbol="FX" currency="USD"
               dateTime="20261112;152700" quantity="500" tradePrice="1.27"
               buySell="BUY" tradeID="fx-skip-3" />
        <!-- AFX token without underlyingSymbol and without option fields must be skipped -->
        <Trade symbol="PLN" notes="C;AFX" currency="PLN" dateTime="20261112;152800"
               quantity="250" tradePrice="1.0" buySell="BUY" tradeID="afx-skip-1" />
        <!-- Explicit non-OPT/STK assetCategory stays skipped -->
        <Trade assetCategory="BOND" symbol="UST9128" dateTime="20261112;152900"
               quantity="10" tradePrice="99.5" buySell="BUY" tradeID="bond-skip-1" />
        <Trade assetCategory="FUND" symbol="VTSAX" dateTime="20261112;152930"
               quantity="10" tradePrice="120.0" buySell="BUY" tradeID="fund-skip-1" />
        <Trade assetCategory="CASH" symbol="USD.CAD" dateTime="20261112;152945"
               quantity="100" tradePrice="1.38" buySell="BUY" tradeID="cash-skip-1" />
        <Trade symbol="TOTAL" dateTime="20261112;153000"
               quantity="1" tradePrice="1.0" buySell="BUY" tradeID="tot-skip-1" />
      </Trades>
      <OpenPositions>
        <OpenPosition symbol="AAPL  261218C00220000" position="-1" markPrice="4.10" />
        <OpenPosition symbol="BRK.B" position="5" markPrice="452.0" />
        <OpenPosition symbol="USD.PLN" underlyingSymbol="" position="1000" markPrice="4.05" />
        <OpenPosition symbol="PLN" notes="AFX" position="250" markPrice="1.0" />
        <OpenPosition assetCategory="BOND" symbol="UST9128" position="10" markPrice="99.5" />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""
    execs, positions = parse_flex_xml(xml_payload, default_source="FLEX_ACTIVITY_365")
    assert [e.exec_id for e in execs] == [
        "opt-attr-1",
        "opt-sym-1",
        "stk-nvda-1",
        "stk-brkb-1",
        "stk-bfb-1",
    ]
    assert execs[0].asset_class == "OPT"
    assert execs[0].underlying == "AAPL"
    assert execs[1].asset_class == "OPT"
    assert execs[1].underlying == "MSFT"
    assert execs[1].expiry == "20261218"
    assert execs[1].strike == 420.0
    assert execs[1].right == "P"
    assert execs[2].asset_class == "STK"
    assert execs[3].asset_class == "STK"
    assert execs[3].underlying == "BRKB"
    assert execs[4].asset_class == "STK"
    assert execs[4].underlying == "BFB"

    assert len(positions) == 2
    assert positions[0].asset_class == "OPT"
    assert positions[0].underlying == "AAPL"
    assert positions[1].asset_class == "STK"
    assert positions[1].underlying == "BRKB"


def test_parse_flex_xml_trade_notes_and_option_eae_deduplication() -> None:
    xml_payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="NotesAndEAE" type="AF">
  <FlexStatements count="1">
    <FlexStatement toDate="20261112">
      <Trades>
        <!-- Non-lifecycle Flex notes tokens (IA, FP, RP, P) must stay TRADE and open_close=None -->
        <Trade assetCategory="OPT" symbol="AAPL  261218C00220000" underlyingSymbol="AAPL"
               strike="220" expiry="20261218" putCall="C" dateTime="20261106;100000"
               quantity="1" tradePrice="3.20" buySell="BUY" notes="IA;FP;RP;P"
               transactionType="ExchTrade" tradeID="tr-ia-1" />
        <!-- Expiration recorded in <Trade notes="Ep"> (with tradeID, commission, exact time) -->
        <Trade assetCategory="OPT" symbol="NVDA  261106P00130000" underlyingSymbol="NVDA"
               strike="130" expiry="20261106" putCall="P" dateTime="20261106;162000"
               quantity="2" tradePrice="0" proceeds="0" ibCommission="-0.15"
               buySell="BUY" notes="C;Ep" transactionType="BookTrade" tradeID="tr-ep-1" />
        <!-- Assignment recorded in <Trade notes="A"> -->
        <Trade assetCategory="OPT" symbol="MSFT  261106P00410000" underlyingSymbol="MSFT"
               strike="410" expiry="20261106" putCall="P" dateTime="20261106;162000"
               quantity="1" tradePrice="0" proceeds="0" ibCommission="0"
               buySell="BUY" notes="A" transactionType="BookTrade" tradeID="tr-a-1" />
      </Trades>
      <TradeConfirms>
        <!-- Exercise recorded in <TradeConfirm notes="Ex"> -->
        <TradeConfirm assetCategory="OPT" symbol="AAPL  261106C00200000" underlyingSymbol="AAPL"
                      strike="200" expiry="20261106" putCall="C" dateTime="20261106;162000"
                      quantity="-1" tradePrice="0" proceeds="0"
                      buySell="SELL" notes="Ex" tradeID="tc-ex-1" />
      </TradeConfirms>
      <OptionEAE>
        <!-- Duplicates of Trade/TradeConfirm above: MUST be skipped -->
        <OptionEAE assetCategory="OPT" symbol="NVDA  261106P00130000" underlyingSymbol="NVDA"
                   strike="130" expiry="20261106" putCall="P" date="20261106"
                   transactionType="Expiration" quantity="2" tradePrice="0" realizedPnl="420.0" />
        <OptionEAE assetCategory="OPT" symbol="MSFT  261106P00410000" underlyingSymbol="MSFT"
                   strike="410" expiry="20261106" putCall="P" date="20261106"
                   transactionType="Assignment" quantity="1" tradePrice="0" />
        <OptionEAE assetCategory="OPT" symbol="AAPL  261106C00200000" underlyingSymbol="AAPL"
                   strike="200" expiry="20261106" putCall="C" date="20261106"
                   transactionType="Exercise" quantity="-1" tradePrice="0" />
        <!-- Standalone OptionEAE NOT in Trades: MUST still be parsed -->
        <OptionEAE assetCategory="OPT" symbol="AVGO  261106P00170000" underlyingSymbol="AVGO"
                   strike="170" expiry="20261106" putCall="P" date="20261106"
                   transactionType="Expiration" quantity="3" tradePrice="0" realizedPnl="310.0" />
      </OptionEAE>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""
    execs, _ = parse_flex_xml(xml_payload, default_source="FLEX_ACTIVITY_365")
    assert len(execs) == 5

    tr_ia = execs[0]
    assert tr_ia.exec_id == "tr-ia-1"
    assert tr_ia.event_code == "TRADE"
    assert tr_ia.open_close is None

    tr_ep = execs[1]
    assert tr_ep.exec_id == "tr-ep-1"
    assert tr_ep.event_code == "EXPIRED"
    assert tr_ep.open_close == "C"
    assert tr_ep.commission == pytest.approx(0.15)
    assert tr_ep.executed_at == "2026-11-06T16:20:00Z"

    tr_a = execs[2]
    assert tr_a.exec_id == "tr-a-1"
    assert tr_a.event_code == "ASSIGNED"
    assert tr_a.open_close == "C"

    tc_ex = execs[3]
    assert tc_ex.exec_id == "tc-ex-1"
    assert tc_ex.event_code == "EXERCISED"
    assert tc_ex.open_close == "C"

    standalone_eae = execs[4]
    assert standalone_eae.underlying == "AVGO"
    assert standalone_eae.exec_id.startswith("sha256:")
    assert standalone_eae.event_code == "EXPIRED"
    assert standalone_eae.realized_pnl_ib == pytest.approx(310.0)
    assert standalone_eae.executed_at == "2026-11-06T00:00:00Z"


def test_parse_flex_xml_open_position_missing_quantity_recovery() -> None:
    xml_payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<FlexQueryResponse queryName="PosRecovery" type="AF">
  <FlexStatements count="1">
    <FlexStatement toDate="20261112">
      <OpenPositions>
        <!-- Float noise: 28.5 / (1.14 * 1) == 24.999999999999996 -> must round to 25.0 -->
        <OpenPosition assetCategory="STK" symbol="AAPL" side="Long"
                      positionValue="28.5" markPrice="1.14" multiplier="1"
                      costBasisMoney="25.0" fifoPnlUnrealized="3.5" />
        <!-- Short option with positive positionValue + side="Short" -> negated to -2.0 -->
        <OpenPosition assetCategory="OPT" symbol="NVDA  261218C00140000"
                      side="Short" positionValue="970.0" markPrice="4.85" multiplier="100"
                      costBasisMoney="-1080.0" fifoPnlUnrealized="110.0" />
        <!-- Short option with already-negative positionValue + side="Short" -> stays -3.0 -->
        <OpenPosition assetCategory="OPT" symbol="MSFT  261218P00410000"
                      side="Short" positionValue="-1500.0" markPrice="5.00" />
        <!-- Zero/dust or missing markPrice or explicit position="0" -> skipped -->
        <OpenPosition assetCategory="STK" symbol="AVGO" position="0"
                      positionValue="1000.0" markPrice="180.0" />
        <OpenPosition assetCategory="STK" symbol="PLTR"
                      positionValue="0.0" markPrice="45.0" />
        <OpenPosition assetCategory="STK" symbol="CRDO"
                      positionValue="500.0" markPrice="0" />
      </OpenPositions>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""
    _, positions = parse_flex_xml(xml_payload, default_source="FLEX_ACTIVITY_365")
    assert len(positions) == 3

    aapl, nvda, msft = positions
    assert aapl.underlying == "AAPL"
    assert aapl.position == 25.0
    assert aapl.avg_cost == pytest.approx(1.0)
    assert aapl.unrealized_pnl == pytest.approx(3.5)

    assert nvda.underlying == "NVDA"
    assert nvda.position == -2.0
    assert nvda.avg_cost == pytest.approx(5.40)

    assert msft.underlying == "MSFT"
    assert msft.position == -3.0
    assert msft.multiplier == 100


def test_ledger_repository_self_heals_synthetic_lifecycle_duplicates(tmp_path: Path) -> None:
    repo = LedgerRepository(tmp_path / "ledger")

    # 1. Existing stale sha256: OptionEAE/CSV row at midnight with realized_pnl_ib=420.0
    stale_syn = LedgerExecutionRow(
        exec_id="sha256:eae00112233445566778899",
        conid=700002,
        symbol="NVDA 06NOV26 130 P",
        underlying="NVDA",
        asset_class="OPT",
        expiry="20261106",
        strike=130.0,
        right="P",
        multiplier=100,
        side="BOT",
        quantity=2.0,
        price=0.0,
        proceeds=0.0,
        commission=0.0,
        realized_pnl_ib=420.0,
        open_close="C",
        event_code="EXPIRED",
        executed_at="2026-11-06T00:00:00Z",
        source="FLEX_IMPORT",
    )
    assert repo.upsert_executions([stale_syn]) == (1, 0)

    # Incoming Flex <Trade notes="Ep"> row (broker tradeID, 16:20 UTC, commission=0.15)
    real_trade = LedgerExecutionRow(
        exec_id="trade-ep-99001",
        symbol="NVDA  261106P00130000",
        underlying="NVDA",
        asset_class="OPT",
        expiry="20261106",
        strike=130.0,
        right="P",
        multiplier=100,
        side="BOT",
        quantity=2.0,
        price=0.0,
        proceeds=0.0,
        commission=0.15,
        realized_pnl_ib=None,
        open_close="C",
        event_code="EXPIRED",
        executed_at="2026-11-06T16:20:00Z",
        source="FLEX_ACTIVITY_365",
    )
    ins, upd = repo.upsert_executions([real_trade])
    assert (ins, upd) == (0, 1)

    df = repo.load_executions()
    assert df.height == 1
    row = df.row(0, named=True)
    assert row["exec_id"] == "trade-ep-99001"
    assert row["executed_at"] == "2026-11-06T16:20:00Z"
    assert row["commission"] == pytest.approx(0.15)
    assert row["realized_pnl_ib"] == pytest.approx(420.0)
    assert row["conid"] == 700002

    # 2. Converse: real row without realized_pnl_ib exists; incoming sha256: row backfills & skips
    real_assign = LedgerExecutionRow(
        exec_id="trade-a-88001",
        symbol="AAPL  261106P00210000",
        underlying="AAPL",
        asset_class="OPT",
        expiry="20261106",
        strike=210.0,
        right="P",
        multiplier=100,
        side="BOT",
        quantity=1.0,
        price=0.0,
        proceeds=0.0,
        commission=0.25,
        realized_pnl_ib=None,
        open_close="C",
        event_code="ASSIGNED",
        executed_at="2026-11-06T16:20:00Z",
        source="FLEX_ACTIVITY_365",
    )
    assert repo.upsert_executions([real_assign]) == (1, 0)

    syn_assign = LedgerExecutionRow(
        exec_id="sha256:eae99887766554433221100",
        conid=700055,
        symbol="AAPL  261106P00210000",
        underlying="AAPL",
        asset_class="OPT",
        expiry="20261106",
        strike=210.0,
        right="P",
        multiplier=100,
        side="BOT",
        quantity=1.0,
        price=0.0,
        proceeds=0.0,
        realized_pnl_ib=-150.0,
        open_close="C",
        event_code="ASSIGNED",
        executed_at="2026-11-06T00:00:00Z",
        source="FLEX_IMPORT",
    )
    assert repo.upsert_executions([syn_assign]) == (0, 1)
    assert repo.upsert_executions([syn_assign]) == (0, 0)

    df_after = repo.load_executions()
    assert df_after.height == 2
    aapl_row = df_after.filter(df_after["exec_id"] == "trade-a-88001").row(0, named=True)
    assert aapl_row["realized_pnl_ib"] == pytest.approx(-150.0)
    assert aapl_row["conid"] == 700055

    # 3. Same-batch [synthetic, real] produces (1, 0) and retains only real exec_id
    syn_batch = LedgerExecutionRow(
        exec_id="sha256:batch112233445566778899",
        symbol="MSFT  261106C00420000",
        underlying="MSFT",
        asset_class="OPT",
        expiry="20261106",
        strike=420.0,
        right="C",
        side="SLD",
        quantity=1.0,
        price=0.0,
        proceeds=0.0,
        realized_pnl_ib=210.0,
        open_close="C",
        event_code="EXERCISED",
        executed_at="2026-11-06T00:00:00Z",
        source="FLEX_IMPORT",
    )
    real_batch = syn_batch.model_copy(
        update={
            "exec_id": "trade-ex-77001",
            "realized_pnl_ib": None,
            "executed_at": "2026-11-06T16:20:00Z",
            "source": "FLEX_MONTHLY",
        }
    )
    assert repo.upsert_executions([syn_batch, real_batch]) == (1, 0)
    df_final = repo.load_executions()
    assert df_final.height == 3
    msft_row = df_final.filter(df_final["underlying"] == "MSFT").row(0, named=True)
    assert msft_row["exec_id"] == "trade-ex-77001"
    assert msft_row["realized_pnl_ib"] == pytest.approx(210.0)
