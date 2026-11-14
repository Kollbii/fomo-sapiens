"""Unit tests for generic multi-strategy `PaperTradeStore` (`src/storage/paper_trade_store.py`)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

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


def test_ledger_fields_validation_and_legacy_json_compatibility(tmp_path: Path) -> None:
    leg_default = PaperTradeLeg(side="SELL", right="P", expiry="2026-12-18", strike=130.0)
    assert leg_default.con_id == 0
    assert leg_default.qty_ratio == 1
    assert leg_default.entry_price is None

    leg_custom = PaperTradeLeg(
        side="BUY",
        right="C",
        expiry="2026-12-18",
        strike=145.0,
        con_id=0,
        qty_ratio=2,
        entry_price=0.0,
    )
    assert leg_custom.qty_ratio == 2
    assert leg_custom.entry_price == 0.0

    bad_leg_cases: list[dict[str, Any]] = [
        {"con_id": -1},
        {"qty_ratio": 0},
        {"qty_ratio": 101},
        {"entry_price": -0.01},
        {"entry_price": float("nan")},
        {"entry_price": float("inf")},
    ]
    for bad_leg_kwargs in bad_leg_cases:
        with pytest.raises(ValidationError):
            PaperTradeLeg(
                side="BUY",
                right="C",
                expiry="2026-12-18",
                strike=145.0,
                **bad_leg_kwargs,
            )

    ledger_trade = PaperTrade(
        id="NVDA-LEDGER-001",
        ticker="NVDA",
        strategy_kind="BULL_PUT_SPREAD",
        status="CLOSED",
        legs=[leg_default, leg_custom],
        qty=3,
        underlying_conid=0,
        created_from_snapshot_id=None,
        created_utc="2026-11-12T15:00:00Z",
        market_date="2026-11-12",
        entry_net_cash=310.0,
        source="LEDGER",
        ledger_campaign_id="camp-1",
        ledger_strategy_label="Bull Put Spread",
    )
    assert ledger_trade.source == "LEDGER"
    assert ledger_trade.status == "CLOSED"
    assert ledger_trade.underlying_conid == 0
    assert ledger_trade.created_from_snapshot_id is None
    assert ledger_trade.ledger_campaign_id == "camp-1"
    assert ledger_trade.ledger_strategy_label == "Bull Put Spread"

    for bad_snap in ("", "   ", "../NVDA_20261111_155004", "not-a-snapshot"):
        with pytest.raises(ValidationError):
            PaperTrade(
                id="NVDA-LEDGER-BAD",
                ticker="NVDA",
                strategy_kind="CUSTOM",
                legs=[leg_default],
                created_from_snapshot_id=bad_snap,
                created_utc="2026-11-12T15:00:00Z",
                market_date="2026-11-12",
                entry_net_cash=100.0,
            )

    # Legacy paper_trades.json payload lacking new fields loads with safe defaults.
    store = PaperTradeStore(tmp_path / "metadata", tmp_path / "tracked")
    store.metadata_dir.mkdir(parents=True, exist_ok=True)
    legacy_payload = {
        "trades": [
            {
                "id": "NVDA-20261113-b75960cb",
                "ticker": "NVDA",
                "strategy_kind": "BULL_PUT_SPREAD",
                "status": "STOPPED",
                "legs": [
                    {
                        "side": "SELL",
                        "right": "P",
                        "expiry": "2026-12-18",
                        "strike": 200.0,
                        "con_id": 705991494,
                    }
                ],
                "qty": 1,
                "underlying_conid": 4815747,
                "created_from_snapshot_id": "NVDA_20261111_135531",
                "created_utc": "2026-11-13T11:39:16Z",
                "market_date": "2026-11-11",
                "expiry": "2026-12-18",
                "entry_net_cash": 507.5,
                "status_changed_utc": "2026-11-13T16:17:58Z",
                "last_quote_utc": None,
            }
        ]
    }
    store.registry_path.write_text(json.dumps(legacy_payload), encoding="utf-8")
    loaded = store.load_trades()
    assert len(loaded) == 1
    assert loaded[0].source == "MANUAL"
    assert loaded[0].ledger_campaign_id is None
    assert loaded[0].ledger_strategy_label is None
    assert loaded[0].legs[0].qty_ratio == 1
    assert loaded[0].legs[0].entry_price is None


def test_sync_ledger_trades_lifecycle_rolls_closing_and_manual_isolation(tmp_path: Path) -> None:
    store = PaperTradeStore(tmp_path / "metadata", tmp_path / "tracked")
    t0 = datetime(2026, 11, 14, 15, 0, 0, tzinfo=UTC)

    old_legs = [
        PaperTradeLeg(
            side="SELL", right="P", expiry="2026-12-18", strike=130.0, con_id=0, entry_price=2.10
        ),
        PaperTradeLeg(
            side="BUY", right="P", expiry="2026-12-18", strike=125.0, con_id=0, entry_price=1.05
        ),
    ]
    manual_trade = PaperTrade(
        id="NVDA-20261111-manual01",
        ticker="NVDA",
        strategy_kind="BULL_PUT_SPREAD",
        status="ACTIVE",
        legs=old_legs,
        qty=1,
        underlying_conid=4815,
        created_from_snapshot_id="NVDA_20261111_155004",
        created_utc="2026-11-11T15:50:04Z",
        market_date="2026-11-11",
        entry_net_cash=105.0,
        source="MANUAL",
    )
    store.save_trades([manual_trade])

    # 1. Initial sync creates two LEDGER trades (one campaign-keyed, one fallback-keyed).
    inc_camp = PaperTrade(
        id="NVDA-LEDGER-init1",
        ticker="NVDA",
        strategy_kind="BULL_PUT_SPREAD",
        legs=old_legs,
        qty=2,
        underlying_conid=0,
        created_from_snapshot_id=None,
        created_utc="2026-11-12T14:30:00Z",
        market_date="2026-11-12",
        entry_net_cash=210.0,
        source="LEDGER",
        ledger_campaign_id="camp-nvda-1",
        ledger_strategy_label="Bull Put Spread",
    )
    inc_fallback = PaperTrade(
        id="AVGO-LEDGER-init2",
        ticker="AVGO",
        strategy_kind="CUSTOM_COMBO",
        legs=[
            PaperTradeLeg(
                side="BUY",
                right="C",
                expiry="2026-12-25",
                strike=180.0,
                con_id=0,
                qty_ratio=2,
                entry_price=3.40,
            )
        ],
        qty=1,
        underlying_conid=0,
        created_from_snapshot_id=None,
        created_utc="2026-11-13T14:30:00Z",
        market_date="2026-11-13",
        entry_net_cash=-680.0,
        source="MANUAL",  # intentionally wrong; sync_ledger_trades must coerce to LEDGER
        ledger_campaign_id=None,
    )

    created, updated, closed, all_ledger = store.sync_ledger_trades(
        [inc_camp, inc_fallback], now_utc=t0
    )
    assert (created, updated, closed) == (2, 0, 0)
    assert len(all_ledger) == 2
    assert all(t.source == "LEDGER" for t in all_ledger)
    assert not list(store.metadata_dir.glob("*.tmp"))

    # Add a last_quote_utc timestamp on inc_camp to verify sync preserves quote metadata.
    saved = store.load_trades()
    saved_with_quote = [
        t.model_copy(update={"last_quote_utc": "2026-11-14T14:55:00Z"})
        if t.id == "NVDA-LEDGER-init1"
        else t
        for t in saved
    ]
    store.save_trades(saved_with_quote)

    # 2. Idempotent re-sync (new transient IDs & duplicate incoming entries) preserves IDs.
    t1 = t0 + timedelta(minutes=5)
    resync_camp = inc_camp.model_copy(
        update={"id": "NVDA-LEDGER-transient", "created_utc": "2026-11-14T15:05:00Z"}
    )
    resync_camp_dup = inc_camp.model_copy(
        update={"id": "NVDA-LEDGER-dup", "created_utc": "2026-11-14T15:05:00Z"}
    )
    resync_fallback = inc_fallback.model_copy(
        update={"id": "AVGO-LEDGER-transient", "created_utc": "2026-11-14T15:05:00Z"}
    )
    created, updated, closed, all_ledger = store.sync_ledger_trades(
        [resync_camp, resync_camp_dup, resync_fallback], now_utc=t1
    )
    assert (created, updated, closed) == (0, 2, 0)
    by_id = {t.id: t for t in all_ledger}
    assert set(by_id) == {"NVDA-LEDGER-init1", "AVGO-LEDGER-init2"}
    assert by_id["NVDA-LEDGER-init1"].created_utc == "2026-11-12T14:30:00Z"
    assert by_id["NVDA-LEDGER-init1"].last_quote_utc == "2026-11-14T14:55:00Z"
    assert by_id["AVGO-LEDGER-init2"].created_utc == "2026-11-13T14:30:00Z"

    # 3. Roll camp-nvda-1 to new legs/market_date, open camp-nvda-2 at old legs, drop AVGO fallback.
    t2 = t0 + timedelta(hours=1)
    rolled_legs = [
        PaperTradeLeg(
            side="SELL", right="P", expiry="2027-01-15", strike=135.0, con_id=9101, entry_price=2.80
        ),
        PaperTradeLeg(
            side="BUY", right="P", expiry="2027-01-15", strike=130.0, con_id=9102, entry_price=1.40
        ),
    ]
    rolled_camp = PaperTrade(
        id="NVDA-LEDGER-new-transient",
        ticker="NVDA",
        strategy_kind="ROLL_VERTICAL",
        legs=rolled_legs,
        qty=3,
        underlying_conid=4815,
        created_from_snapshot_id="NVDA_20261114_150000",
        created_utc="2026-11-14T16:00:00Z",
        market_date="2026-11-14",
        entry_net_cash=420.0,
        source="LEDGER",
        ledger_campaign_id="camp-nvda-1",
        ledger_strategy_label="Rolled Bull Put",
    )
    new_camp = PaperTrade(
        id="NVDA-LEDGER-camp2",
        ticker="NVDA",
        strategy_kind="BULL_PUT_SPREAD",
        legs=old_legs,
        qty=1,
        underlying_conid=4815,
        created_from_snapshot_id=None,
        created_utc="2026-11-14T16:00:00Z",
        market_date="2026-11-14",
        entry_net_cash=105.0,
        source="LEDGER",
        ledger_campaign_id="camp-nvda-2",
        ledger_strategy_label="Bull Put Spread",
    )

    created, updated, closed, all_ledger = store.sync_ledger_trades(
        [new_camp, rolled_camp], now_utc=t2
    )
    assert (created, updated, closed) == (1, 1, 1)
    by_id = {t.id: t for t in all_ledger}
    rolled = by_id["NVDA-LEDGER-init1"]
    assert rolled.created_utc == "2026-11-12T14:30:00Z"
    assert rolled.market_date == "2026-11-14"
    assert rolled.expiry == "2027-01-15"
    assert rolled.legs == rolled_legs
    assert rolled.qty == 3
    assert rolled.entry_net_cash == 420.0
    assert rolled.strategy_kind == "ROLL_VERTICAL"
    assert rolled.ledger_strategy_label == "Rolled Bull Put"
    assert rolled.underlying_conid == 4815
    assert rolled.created_from_snapshot_id == "NVDA_20261114_150000"
    assert rolled.last_quote_utc == "2026-11-14T14:55:00Z"
    assert rolled.status == "ACTIVE"

    closed_avgo = by_id["AVGO-LEDGER-init2"]
    assert closed_avgo.status == "CLOSED"
    assert closed_avgo.status_changed_utc == "2026-11-14T16:00:00Z"

    assert by_id["NVDA-LEDGER-camp2"].status == "ACTIVE"

    # MANUAL trade remains untouched across the entire sync lifecycle.
    persisted = store.load_trades()
    persisted_manual = [t for t in persisted if t.source == "MANUAL"]
    assert persisted_manual == [manual_trade]


def test_sync_ledger_trades_expires_past_cutoff_without_double_closing(tmp_path: Path) -> None:
    store = PaperTradeStore(tmp_path / "metadata", tmp_path / "tracked")
    expiring_trade = PaperTrade(
        id="NVDA-LEDGER-exp1",
        ticker="NVDA",
        strategy_kind="LONG_CALL",
        status="ACTIVE",
        legs=[PaperTradeLeg(side="BUY", right="C", expiry="2026-11-20", strike=150.0)],
        qty=1,
        underlying_conid=0,
        created_from_snapshot_id=None,
        created_utc="2026-11-10T15:00:00Z",
        market_date="2026-11-10",
        entry_net_cash=-200.0,
        source="LEDGER",
        ledger_campaign_id="camp-exp-1",
    )
    store.save_trades([expiring_trade])

    # Advance past 2026-11-20 ET and omit from incoming: expires via _expire_stale, NOT closed.
    past_cutoff = datetime(2026, 11, 21, 16, 0, 0, tzinfo=UTC)
    created, updated, closed, all_ledger = store.sync_ledger_trades([], now_utc=past_cutoff)
    assert (created, updated, closed) == (0, 0, 0)
    assert len(all_ledger) == 1
    assert all_ledger[0].status == "EXPIRED"
    assert all_ledger[0].status_changed_utc == "2026-11-21T16:00:00Z"

    # An already-expired incoming trade resolves directly to EXPIRED.
    expired_incoming = PaperTrade(
        id="NVDA-LEDGER-exp2",
        ticker="NVDA",
        strategy_kind="LONG_CALL",
        status="ACTIVE",
        legs=[PaperTradeLeg(side="BUY", right="C", expiry="2026-11-20", strike=155.0)],
        qty=1,
        underlying_conid=0,
        created_from_snapshot_id=None,
        created_utc="2026-11-10T15:00:00Z",
        market_date="2026-11-10",
        entry_net_cash=-150.0,
        source="LEDGER",
        ledger_campaign_id="camp-exp-2",
    )
    created2, updated2, closed2, all_ledger2 = store.sync_ledger_trades(
        [expired_incoming], now_utc=past_cutoff
    )
    assert (created2, updated2, closed2) == (1, 0, 0)
    by_id2 = {t.id: t for t in all_ledger2}
    assert by_id2["NVDA-LEDGER-exp2"].status == "EXPIRED"
    assert by_id2["NVDA-LEDGER-exp2"].status_changed_utc == "2026-11-21T16:00:00Z"
