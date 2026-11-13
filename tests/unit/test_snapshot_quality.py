"""Unit tests for snapshot quality measurement, usable gate, and real-quote fallback."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest
from ib_async import Option

from src.broker.client import IBKRBrokerClient
from src.broker.fetcher import (
    PagedOptionChainFetcher,
    empty_real_quote_ratio,
    has_real_quote,
)
from src.core.config import Settings
from src.storage.quality import (
    MIN_QUOTE_COVERAGE,
    USABLE_MARKET_DATA_TYPES,
    USABLE_SPOT_SOURCES,
    SnapshotQualityVerdict,
    evaluate_snapshot,
    is_usable_snapshot,
    measure_quote_quality,
)
from src.storage.schema import SnapshotMeta
from tests.support.aio import noop_sleep, run_coro
from tests.support.ib_fakes import FakeChainIB


def _meta(
    *,
    market_data_type: int = 3,
    spot_source: str | None = "LAST",
    quote_coverage: float | None = 0.95,
) -> SnapshotMeta:
    return SnapshotMeta(
        snapshot_id="NVDA_20261111_155004",
        file_path="data/snapshots/NVDA/NVDA_20261111_155004.csv",
        snapshot_ts_utc=datetime(2026, 11, 11, 15, 50, 4, tzinfo=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        market_date="2026-11-11",
        market_data_type=market_data_type,
        underlying_price=142.65,
        contract_count=10,
        expirations=["2026-12-18"],
        spot_source=spot_source,  # type: ignore[arg-type]
        quote_coverage=quote_coverage,
    )


def test_measure_quote_quality_counts_quoted_and_prior_close_only() -> None:
    empty = pl.DataFrame(
        schema={
            "bid": pl.Float64,
            "ask": pl.Float64,
            "last": pl.Float64,
            "close": pl.Float64,
        }
    )
    assert measure_quote_quality(empty).quote_coverage == 0.0

    df = pl.DataFrame(
        {
            "bid": [2.0, None, None, None, 1.5],
            "ask": [2.2, 1.8, None, None, None],
            "last": [2.1, None, 3.0, None, None],
            "close": [2.0, 1.7, 2.9, 4.0, 1.4],
        }
    )
    quality = measure_quote_quality(df)
    assert quality.contract_count == 5
    assert quality.quoted_contract_count == 3  # rows 0, 1, 4 have bid or ask
    assert quality.prior_close_only_count == 1  # row 3 has close only (no bid/ask/last)
    assert quality.quote_coverage == pytest.approx(0.6)


def test_evaluate_snapshot_quality_gate() -> None:
    assert MIN_QUOTE_COVERAGE == 0.90
    assert frozenset({"LAST", "MID"}) == USABLE_SPOT_SOURCES
    assert frozenset({1, 3}) == USABLE_MARKET_DATA_TYPES

    for md_type in (1, 3):
        for src in ("LAST", "MID"):
            verdict = evaluate_snapshot(
                _meta(market_data_type=md_type, spot_source=src, quote_coverage=0.90)
            )
            assert isinstance(verdict, SnapshotQualityVerdict)
            assert verdict.usable is True
            assert verdict.reasons == ()
            assert is_usable_snapshot(
                _meta(market_data_type=md_type, spot_source=src, quote_coverage=0.90)
            )

    low_cov = evaluate_snapshot(_meta(quote_coverage=0.8999))
    assert not low_cov.usable
    assert any("quote_coverage" in r for r in low_cov.reasons)

    close_spot = evaluate_snapshot(_meta(spot_source="CLOSE"))
    assert not close_spot.usable
    assert any("spot_source" in r for r in close_spot.reasons)

    frozen = evaluate_snapshot(_meta(market_data_type=4))
    assert not frozen.usable
    assert any("market_data_type" in r for r in frozen.reasons)

    legacy = evaluate_snapshot(_meta(spot_source=None, quote_coverage=None))
    assert not legacy.usable
    assert len(legacy.reasons) == 2


def test_real_quote_triggers_type3_to_type4_fallback_when_close_only() -> None:
    assert has_real_quote(SimpleNamespace(bid=1.0, ask=None, close=1.0))
    assert has_real_quote(SimpleNamespace(delayedBid=None, delayedAsk=1.2, close=1.0))
    assert not has_real_quote(SimpleNamespace(bid=-1.0, ask=float("nan"), last=1.0, close=1.0))
    assert empty_real_quote_ratio([]) == 0.0

    fake_ib = FakeChainIB()
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    fetcher = PagedOptionChainFetcher(client, sleep=noop_sleep)
    contracts = [
        Option("NVDA", "20261211", float(130 + i), "C", "SMART", conId=i + 1) for i in range(10)
    ]

    def _factory(contract: Any, _ticks: str) -> Any:
        if client.current_type == 3:
            # Close-only tickers are not live/delayed quotes -> 90% missing real quotes -> Type 4
            if contract.conId == 1:
                return SimpleNamespace(contract=contract, bid=5.0, ask=5.2, last=5.1, close=5.0)
            return SimpleNamespace(contract=contract, bid=None, ask=None, last=None, close=4.8)
        return SimpleNamespace(contract=contract, bid=4.7, ask=4.9, last=4.8, close=4.8)

    fake_ib.ticker_factory = _factory
    tickers = run_coro(fetcher._fetch_page_delayed(contracts))
    assert len(tickers) == 10
    assert client.current_type == 4
    assert empty_real_quote_ratio(tickers) == 0.0
