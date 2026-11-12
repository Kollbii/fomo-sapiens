"""TickerParser: OI by right, IBKR model Greeks validation and sentinel handling."""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest
from ib_async import Option, OptionComputation

from src.broker.parser import SnapshotContext, TickerParser


def _sample_context() -> SnapshotContext:
    return SnapshotContext(
        snapshot_id="NVDA_20261111_153000",
        snapshot_ts_utc="2026-11-11T15:30:00Z",
        market_date="2026-11-11",
        market_data_type=3,
        underlying="NVDA",
        underlying_conid=4815,
        underlying_price=140.0,
    )


def test_parser_call_and_put_oi_and_ibkr_model_greeks() -> None:
    ctx = _sample_context()
    call_opt = Option("NVDA", "20261211", 145.0, "C", "SMART", "100", "USD", conId=9001)
    put_opt = Option("NVDA", "20261211", 135.0, "P", "SMART", "100", "USD", conId=9002)

    call_ticker = SimpleNamespace(
        contract=call_opt,
        bid=4.0,
        ask=4.4,
        last=4.1,
        close=3.9,
        volume=250,
        callOpenInterest=1200,
        putOpenInterest=9999,
        modelGreeks=OptionComputation(
            tickAttrib=0,
            impliedVol=0.42,
            delta=0.48,
            optPrice=4.2,
            pvDividend=0.0,
            gamma=0.03,
            vega=0.18,
            theta=-0.09,
            undPrice=140.0,
        ),
    )
    row_c = TickerParser.parse_ticker(call_ticker, context=ctx)
    assert row_c.right == "C"
    assert row_c.open_interest == 1200
    assert row_c.mid == pytest.approx(4.2)
    assert row_c.mark_price == pytest.approx(4.2)
    assert row_c.greeks_source == "IBKR_MODEL"
    assert row_c.iv == pytest.approx(0.42)
    assert row_c.delta == pytest.approx(0.48)
    assert row_c.dte == 30

    put_ticker = SimpleNamespace(
        contract=put_opt,
        bid=-1.0,
        ask=float("nan"),
        last=-1.0,
        close=3.5,
        volume=2_147_483_647,
        callOpenInterest=8888,
        putOpenInterest=750,
        modelGreeks=None,
    )
    row_p = TickerParser.parse_ticker(put_ticker, context=ctx)
    assert row_p.right == "P"
    assert row_p.open_interest == 750
    assert row_p.bid is None and row_p.ask is None and row_p.mid is None and row_p.last is None
    assert row_p.close == 3.5
    assert row_p.mark_price == 3.5
    assert row_p.volume is None
    assert row_p.greeks_source == "NONE"
    assert (row_p.iv, row_p.delta, row_p.gamma, row_p.theta, row_p.vega) == (None,) * 5


@pytest.mark.parametrize(
    "bad_greeks",
    [
        SimpleNamespace(impliedVol=-0.1, delta=0.5, gamma=0.02, theta=-0.1, vega=0.1),
        SimpleNamespace(impliedVol=12.5, delta=0.5, gamma=0.02, theta=-0.1, vega=0.1),
        SimpleNamespace(impliedVol=0.4, delta=1.5, gamma=0.02, theta=-0.1, vega=0.1),
        SimpleNamespace(impliedVol=0.4, delta=0.5, gamma=-0.01, theta=-0.1, vega=0.1),
        SimpleNamespace(impliedVol=math.nan, delta=0.5, gamma=0.02, theta=-0.1, vega=0.1),
    ],
)
def test_parser_rejects_out_of_bounds_model_greeks(bad_greeks: Any) -> None:
    ctx = _sample_context()
    opt = Option("NVDA", "20261211", 140.0, "C", "SMART", "100", "USD", conId=9003)
    tk = SimpleNamespace(
        contract=opt,
        bid=5.0,
        ask=5.2,
        last=5.1,
        close=5.0,
        volume=10,
        callOpenInterest=-1,
        modelGreeks=bad_greeks,
    )
    row = TickerParser.parse_ticker(tk, context=ctx)
    assert row.greeks_source == "NONE"
    assert (row.iv, row.delta, row.gamma, row.theta, row.vega) == (None,) * 5
    assert row.open_interest is None
