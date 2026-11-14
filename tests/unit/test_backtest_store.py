"""Unit tests for read-only `O(window)` Backtest storage (`src/storage/backtest_store.py`)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.core.config import StorageSettings
from src.core.exceptions import BacktestValidationError, InvalidTickerError
from src.storage.backtest_store import (
    NO_USABLE_DAILY_SNAPSHOT,
    load_daily_chain_bars,
    load_tracked_quotes_by_date,
    select_daily_bar_snapshots,
)
from src.storage.csv_repository import CSVRepository, make_snapshot_id
from src.storage.paper_trade_store import PaperTradeStore


def _make_chain_rows(
    ticker: str,
    ts_utc: datetime,
    market_date: str,
    *,
    bid: float | None = 2.40,
    ask: float | None = 2.60,
    last: float | None = 2.50,
    market_data_type: int = 3,
    conid: int = 7001,
    strike: float = 130.0,
) -> list[dict[str, Any]]:
    sid = make_snapshot_id(ticker, ts_utc)
    expiry = (date.fromisoformat(market_date) + timedelta(days=37)).isoformat()
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    return [
        {
            "snapshot_id": sid,
            "snapshot_ts_utc": ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "market_date": market_date,
            "market_data_type": market_data_type,
            "underlying": ticker,
            "underlying_conid": 4815,
            "underlying_price": 142.65,
            "option_conid": conid,
            "expiry": expiry,
            "dte": 37,
            "strike": strike,
            "moneyness_pct": (strike - 142.65) / 142.65 * 100.0,
            "right": "P",
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "last": last,
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


def test_select_daily_bar_snapshots_o_window_never_opens_out_of_window_csvs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = CSVRepository(StorageSettings(), root=tmp_path)

    # Window target: 2026-11-11 .. 2026-11-12
    # Out-of-window snapshots: Nov 10 and Nov 14
    nov10 = datetime(2026, 11, 10, 16, 0, 0, tzinfo=UTC)
    # Nov 11 usable morning + unusable later (Type 4 frozen) -> must pick usable morning!
    nov11_usable = datetime(2026, 11, 11, 15, 0, 0, tzinfo=UTC)
    nov11_frozen = datetime(2026, 11, 11, 20, 0, 0, tzinfo=UTC)
    # Nov 12: UTC 01:00 Nov 13 is 20:00 EST on Nov 12! Only close-only -> NO_USABLE_DAILY_SNAPSHOT
    nov12_late_et = datetime(2026, 11, 13, 1, 0, 0, tzinfo=UTC)
    nov14 = datetime(2026, 11, 14, 16, 0, 0, tzinfo=UTC)

    repo.save_snapshot("NVDA", _make_chain_rows("NVDA", nov10, "2026-11-10"), spot_source="LAST")
    s_nov11_usable = repo.save_snapshot(
        "NVDA", _make_chain_rows("NVDA", nov11_usable, "2026-11-11"), spot_source="LAST"
    )
    repo.save_snapshot(
        "NVDA",
        _make_chain_rows("NVDA", nov11_frozen, "2026-11-11", market_data_type=4),
        spot_source="LAST",
    )
    s_nov12_close = repo.save_snapshot(
        "NVDA",
        _make_chain_rows("NVDA", nov12_late_et, "2026-11-12", bid=None, ask=None, last=None),
        spot_source="CLOSE",
    )
    repo.save_snapshot("NVDA", _make_chain_rows("NVDA", nov14, "2026-11-14"), spot_source="LAST")

    # Corrupt out-of-window CSVs on disk; backtest loader MUST NOT open them
    (repo.snapshots_dir / "NVDA" / f"{make_snapshot_id('NVDA', nov10)}.csv").write_text(
        "CORRUPT!", encoding="utf-8"
    )
    (repo.snapshots_dir / "NVDA" / f"{make_snapshot_id('NVDA', nov14)}.csv").write_text(
        "CORRUPT!", encoding="utf-8"
    )

    # Also test invalid filename note
    (repo.snapshots_dir / "NVDA" / "not_a_snapshot.csv").write_text("x", encoding="utf-8")

    load_calls: list[str] = []
    orig_load = repo.load_snapshot

    def _spy_load(ticker: str, snapshot_id: str) -> Any:
        load_calls.append(snapshot_id)
        return orig_load(ticker, snapshot_id)

    monkeypatch.setattr(repo, "load_snapshot", _spy_load)

    selections, notes = select_daily_bar_snapshots(
        repo,
        "NVDA",
        start_date=date(2026, 11, 11),
        end_date=date(2026, 11, 12),
    )
    assert [s.snapshot_id for s in selections] == [
        s_nov11_usable.snapshot_id,
        s_nov12_close.snapshot_id,
    ]
    assert selections[0].market_date == "2026-11-11"
    assert selections[0].flags == ()
    assert selections[1].market_date == "2026-11-12"
    assert selections[1].flags == (NO_USABLE_DAILY_SNAPSHOT,)
    assert any("not_a_snapshot.csv" in n for n in notes)
    assert load_calls == []  # selection stage opens ZERO CSV files!

    bars, bar_notes = load_daily_chain_bars(
        repo,
        "NVDA",
        start_date=date(2026, 11, 11),
        end_date=date(2026, 11, 12),
        tracked_dir=tmp_path / "data" / "tracked",
    )
    assert [b.snapshot_id for b in bars] == [
        s_nov11_usable.snapshot_id,
        s_nov12_close.snapshot_id,
    ]
    assert load_calls == [s_nov11_usable.snapshot_id, s_nov12_close.snapshot_id]
    assert not (tmp_path / "data" / "tracked").exists()  # read-only: never creates tracked_dir
    assert len(bar_notes) == len(notes)


def test_load_daily_chain_bars_attaches_latest_tracked_quotes_and_handles_corrupt_index(
    tmp_path: Path,
) -> None:
    repo = CSVRepository(StorageSettings(), root=tmp_path)
    store = PaperTradeStore(tmp_path / "data" / "metadata", tmp_path / "data" / "tracked")

    bar_ts = datetime(2026, 11, 11, 15, 0, 0, tzinfo=UTC)
    repo.save_snapshot("NVDA", _make_chain_rows("NVDA", bar_ts, "2026-11-11"), spot_source="LAST")

    # Two tracked quote snapshots on 2026-11-11 for conId 7001, plus one earlier file on Nov 10
    q_nov10 = datetime(2026, 11, 10, 18, 0, 0, tzinfo=UTC)
    q_early = datetime(2026, 11, 11, 16, 0, 0, tzinfo=UTC)
    q_late = datetime(2026, 11, 11, 19, 30, 0, tzinfo=UTC)
    store.save_quotes("NVDA", _make_chain_rows("NVDA", q_nov10, "2026-11-10", bid=1.1, ask=1.2))
    store.save_quotes(
        "NVDA", _make_chain_rows("NVDA", q_early, "2026-11-11", bid=2.0, ask=2.2, conid=7001)
    )
    store.save_quotes(
        "NVDA",
        _make_chain_rows("NVDA", q_late, "2026-11-11", bid=3.0, ask=3.2, conid=7001)
        + _make_chain_rows(
            "NVDA", q_late, "2026-11-11", bid=1.5, ask=1.7, conid=7002, strike=125.0
        ),
    )

    # Corrupt index: read-only backtest never rewrites it and flags NO_USABLE_DAILY_SNAPSHOT
    index_before = "{corrupt index"
    repo.index_path.write_text(index_before, encoding="utf-8")

    bars, notes = load_daily_chain_bars(
        repo,
        "NVDA",
        start_date=date(2026, 11, 11),
        end_date=date(2026, 11, 11),
        tracked_dir=store.tracked_dir,
    )
    assert repo.index_path.read_text(encoding="utf-8") == index_before
    assert len(bars) == 1
    bar = bars[0]
    assert bar.selection_flags == (NO_USABLE_DAILY_SNAPSHOT,)
    assert any("Snapshot index unreadable" in n for n in notes)
    assert bar.tracked_df is not None
    assert bar.tracked_df.height == 2
    by_strike = {float(r["strike"]): r for r in bar.tracked_df.to_dicts()}
    assert by_strike[130.0]["bid"] == pytest.approx(3.0)
    assert by_strike[125.0]["bid"] == pytest.approx(1.5)
    assert bar.tracked_snapshot_ids == (make_snapshot_id("NVDA", q_late),)

    # Also test standalone date slice lookup and validations
    slices = load_tracked_quotes_by_date(
        "NVDA",
        target_dates=["2026-11-11"],
        tracked_dir=store.tracked_dir,
    )
    assert list(slices) == ["2026-11-11"]
    assert slices["2026-11-11"].df.height == 2

    with pytest.raises(InvalidTickerError):
        select_daily_bar_snapshots(repo, "../BAD")
    with pytest.raises(BacktestValidationError):
        select_daily_bar_snapshots(
            repo, "NVDA", start_date=date(2026, 11, 12), end_date=date(2026, 11, 11)
        )
    assert select_daily_bar_snapshots(repo, "AVGO")[0] == []


def test_load_daily_chain_bars_preserves_merged_short_dte_rows(tmp_path: Path) -> None:
    repo = CSVRepository(StorageSettings(), root=tmp_path)
    repo.ensure_layout()
    ts_utc = datetime(2026, 11, 13, 19, 30, 0, tzinfo=UTC)
    mkt_date = "2026-11-13"
    sid = make_snapshot_id("NVDA", ts_utc)
    rows: list[dict[str, Any]] = []
    for idx, dte in enumerate((0, 7, 14, 29, 30, 37, 65)):
        expiry = (date.fromisoformat(mkt_date) + timedelta(days=dte)).isoformat()
        rows.append(
            {
                "snapshot_id": sid,
                "snapshot_ts_utc": ts_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "market_date": mkt_date,
                "market_data_type": 3,
                "underlying": "NVDA",
                "underlying_conid": 4815,
                "underlying_price": 140.0,
                "option_conid": 8000 + idx,
                "expiry": expiry,
                "dte": dte,
                "strike": 140.0,
                "moneyness_pct": 0.0,
                "right": "P",
                "bid": 2.4,
                "ask": 2.6,
                "mid": 2.5,
                "last": 2.5,
                "close": 2.4,
                "mark_price": 2.5,
                "volume": 100,
                "open_interest": 500,
                "iv": 0.42,
                "delta": -0.35,
                "gamma": 0.03,
                "theta": -0.05,
                "vega": 0.11,
                "greeks_source": "IBKR_MODEL",
            }
        )
    repo.save_snapshot("NVDA", rows, spot_source="MID")

    bars, notes = load_daily_chain_bars(
        repo,
        "NVDA",
        start_date=date(2026, 11, 13),
        end_date=date(2026, 11, 13),
    )
    assert notes == []
    assert len(bars) == 1
    assert sorted(bars[0].df["dte"].to_list()) == [0, 7, 14, 29, 30, 37, 65]
