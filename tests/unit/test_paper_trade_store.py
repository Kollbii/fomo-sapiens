"""Unit tests for generic multi-strategy `PaperTradeStore` (`src/storage/paper_trade_store.py`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.core.exceptions import InvalidTickerError, SnapshotNotFoundError, StorageSchemaError
from src.storage.csv_repository import make_snapshot_id
from src.storage.paper_trade_store import (
    MAX_PAPER_TRADE_LEGS,
    PaperQuoteRecord,
    PaperTrade,
    PaperTradeLeg,
    PaperTradeStore,
)


def _sample_rows(
    ticker: str = "NVDA",
    ts: datetime | None = None,
    *,
    conid: int = 7001,
) -> list[dict[str, Any]]:
    when = ts or datetime(2026, 11, 11, 15, 50, 4, tzinfo=UTC)
    sid = make_snapshot_id(ticker, when)
    expiry = (when.date() + timedelta(days=37)).isoformat()
    return [
        {
            "snapshot_id": sid,
            "snapshot_ts_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "market_date": "2026-11-11",
            "market_data_type": 3,
            "underlying": ticker,
            "underlying_conid": 4815,
            "underlying_price": 142.65,
            "option_conid": conid,
            "expiry": expiry,
            "dte": 37,
            "strike": 130.0,
            "moneyness_pct": (130.0 - 142.65) / 142.65 * 100.0,
            "right": "P",
            "bid": 2.40,
            "ask": 2.60,
            "mid": 2.50,
            "last": 2.50,
            "close": 2.30,
            "volume": 50,
            "open_interest": 200,
            "iv": 0.45,
            "delta": -0.25,
            "gamma": 0.02,
            "theta": -0.05,
            "vega": 0.14,
            "greeks_source": "IBKR_MODEL",
        }
    ]


def _make_trade(
    trade_id: str,
    strategy_kind: str,
    legs: list[PaperTradeLeg],
    *,
    ticker: str = "NVDA",
    entry_net_cash: float = 120.0,
) -> PaperTrade:
    return PaperTrade(
        id=trade_id,
        ticker=ticker,
        strategy_kind=strategy_kind,
        legs=legs,
        qty=2,
        underlying_conid=4815,
        created_from_snapshot_id="NVDA_20261111_155004",
        created_utc="2026-11-11T15:50:04Z",
        market_date="2026-11-11",
        entry_net_cash=entry_net_cash,
    )


def test_missing_files_initialize_cleanly_and_ensure_layout_is_safe(tmp_path: Path) -> None:
    store = PaperTradeStore(tmp_path / "metadata", tmp_path / "tracked")
    assert store.load_trades() == []
    assert store.read_quote_records() == []
    assert store.list_quote_files("NVDA") == []
    assert not store.metadata_dir.exists()
    assert not store.tracked_dir.exists()

    store.ensure_layout()
    assert store.metadata_dir.is_dir()
    assert store.tracked_dir.is_dir()


def test_multi_strategy_legs_registry_roundtrip_and_corruption_error(tmp_path: Path) -> None:
    store = PaperTradeStore(tmp_path / "metadata", tmp_path / "tracked")

    long_call = _make_trade(
        "pt-1leg",
        "LONG_CALL",
        [PaperTradeLeg(side="BUY", right="C", expiry="2026-12-18", strike=145.0, con_id=101)],
        entry_net_cash=-350.0,
    )
    iron_condor = _make_trade(
        "pt-4leg",
        "IRON_CONDOR",
        [
            PaperTradeLeg(side="BUY", right="P", expiry="2026-12-18", strike=120.0, con_id=201),
            PaperTradeLeg(side="SELL", right="P", expiry="2026-12-18", strike=130.0, con_id=202),
            PaperTradeLeg(side="SELL", right="C", expiry="2026-12-18", strike=160.0, con_id=203),
            PaperTradeLeg(side="BUY", right="C", expiry="2026-12-25", strike=170.0, con_id=204),
        ],
        entry_net_cash=215.0,
    )
    eight_leg = _make_trade(
        "pt-8leg",
        "DOUBLE_CALENDAR_CUSTOM",
        [
            PaperTradeLeg(
                side="BUY" if i % 2 == 0 else "SELL",
                right="C" if i < 4 else "P",
                expiry="2026-12-18" if i < 4 else "2027-01-15",
                strike=120.0 + i * 5.0,
                con_id=300 + i,
            )
            for i in range(MAX_PAPER_TRADE_LEGS)
        ],
    )
    assert iron_condor.expiry == "2026-12-18"  # synced to earliest leg expiry
    assert eight_leg.expiry == "2026-12-18"

    store.save_trades([long_call, iron_condor, eight_leg])
    loaded = store.load_trades()
    assert loaded == [long_call, iron_condor, eight_leg]
    assert loaded[1].legs_key() == iron_condor.legs_key()
    assert not list(store.metadata_dir.glob("*.tmp"))

    with pytest.raises(ValueError):
        _make_trade(
            "pt-9leg",
            "TOO_MANY",
            [
                PaperTradeLeg(side="BUY", right="C", expiry="2026-12-18", strike=100 + i, con_id=i)
                for i in range(MAX_PAPER_TRADE_LEGS + 1)
            ],
        )

    store.registry_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(StorageSchemaError):
        store.load_trades()


def test_quote_jsonl_append_filter_and_corrupt_line_skip(tmp_path: Path) -> None:
    store = PaperTradeStore(tmp_path / "metadata", tmp_path / "tracked")
    rec1 = PaperQuoteRecord(
        quote_id="q-1",
        quoted_at_utc="2026-11-11T15:50:04Z",
        market_date="2026-11-11",
        ticker="NVDA",
        trade_ids=["pt-1"],
        status="QUOTED",
        tracked_snapshot_id="NVDA_20261111_155004",
        spot=142.65,
        spot_source="LAST",
        market_data_type=3,
    )
    rec2 = PaperQuoteRecord(
        quote_id="q-2",
        quoted_at_utc="2026-11-11T15:51:04Z",
        market_date="2026-11-11",
        ticker="AVGO",
        trade_ids=["pt-2"],
        status="ERROR",
        error="timeout",
    )
    store.append_quote_records([rec1])
    store.append_quote_record(rec2)

    # Inject a corrupt JSONL line; reader skips it gracefully
    with store.quotes_log_path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json}\n")

    assert store.read_quote_records() == [rec1, rec2]
    assert store.read_quote_records(ticker="nvda") == [rec1]
    assert store.read_quote_records(ticker="AVGO") == [rec2]


def test_immutable_tracked_quote_csv_validation_and_reload(tmp_path: Path) -> None:
    store = PaperTradeStore(tmp_path / "metadata", tmp_path / "tracked")
    ts1 = datetime(2026, 11, 11, 15, 50, 4, tzinfo=UTC)
    rows = _sample_rows("NVDA", ts1)
    saved_path = store.save_quotes("nvda", rows)
    assert saved_path == store.tracked_dir / "NVDA" / "NVDA_20261111_155004.csv"
    assert saved_path.is_file()
    assert store.list_quote_files("NVDA") == [saved_path]

    df = store.load_quotes("NVDA", "NVDA_20261111_155004")
    assert df.height == 1
    assert df["option_conid"].to_list() == [7001]

    # Immutable: second write with identical snapshot_id fails
    with pytest.raises(StorageSchemaError, match="already exists"):
        store.save_quotes("NVDA", rows)

    with pytest.raises(InvalidTickerError):
        store.quote_path("../BAD", "NVDA_20261111_155004")
    with pytest.raises(StorageSchemaError):
        store.save_quotes("AVGO", rows)
    with pytest.raises(StorageSchemaError):
        store.save_quotes("NVDA", [])
    with pytest.raises(SnapshotNotFoundError):
        store.load_quotes("NVDA", "NVDA_20261111_155099")
    with pytest.raises(StorageSchemaError):
        store.load_quotes("NVDA", "bad-id")
