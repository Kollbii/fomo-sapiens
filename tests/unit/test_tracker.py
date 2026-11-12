"""Position tracker: registry, USER validation, AUTO picks, slot quoting, lifecycle, dry run.

No test needs a live Gateway: the collector's fake clock / fake web service drive the slots and
a fake exact-quote source (or a mocked `ib_async.IB`) stands in for the broker.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest
from pydantic import ValidationError

from src.analytics.strategies import StrategyLegInput
from src.broker.client import IBKRBrokerClient
from src.broker.fetcher import ExactContractSpec, ExactQuoteResult, PagedOptionChainFetcher
from src.collector.calendar import US_EASTERN, Slot, slots_between, slots_for_day
from src.collector.service import CollectorService
from src.collector.store import CollectorSettings, CollectorStore, TickerPin
from src.core.config import Settings, StorageSettings, TrackerSettings
from src.core.exceptions import (
    GatewayConnectionError,
    InvalidTickerError,
    PacingViolationError,
    SnapshotNotFoundError,
    StorageSchemaError,
)
from src.core.security import ReadOnlyIBGuard, TickerValidator
from src.storage.csv_repository import CSVRepository
from src.storage.schema import COLUMNS, OptionContractRow
from src.tracker.service import (
    DuplicateSpreadError,
    TrackerCapError,
    TrackerService,
    TrackerStateError,
)
from src.tracker.store import TrackedLeg, TrackedSpread, TrackerStore
from tests.unit.test_collector import FakeClock, FakeWeb
from tests.unit.test_contracts_and_pagination import FakeIB, _noop_sleep
from tests.unit.test_strategies import _build_synthetic_chain

DAY = date(2026, 11, 16)  # Monday
EXPIRY = "2026-12-18"  # Friday, 32 DTE on DAY
LATE_EXPIRY = "2027-01-15"
NVDA_LEGS = [
    StrategyLegInput(side="SELL", right="P", expiry=EXPIRY, strike=95.0),
    StrategyLegInput(side="BUY", right="P", expiry=EXPIRY, strike=90.0),
]


def _et(day: date, hh: int, mm: int, ss: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=US_EASTERN).astimezone(UTC)


def _save_snapshot(repo: CSVRepository, ticker: str, ts: datetime, *, spot: float = 100.0) -> str:
    """A synthetic chain (puts and calls, 80-120 strikes, two expiries) saved via the repo."""
    market_day = ts.astimezone(US_EASTERN).date()
    df = _build_synthetic_chain(
        spot=spot,
        expiries=[
            (EXPIRY, (date.fromisoformat(EXPIRY) - market_day).days),
            (LATE_EXPIRY, (date.fromisoformat(LATE_EXPIRY) - market_day).days),
        ],
    )
    sid = f"{ticker}_{ts:%Y%m%d_%H%M%S}"
    df = df.with_columns(
        pl.lit(sid).alias("snapshot_id"),
        pl.lit(ts.strftime("%Y-%m-%dT%H:%M:%SZ")).alias("snapshot_ts_utc"),
        pl.lit(market_day.isoformat()).alias("market_date"),
        pl.lit(ticker).alias("underlying"),
    )
    repo.save_snapshot(ticker, df.to_dicts(), range_pct=0.35, strike_step=5.0, spot_source="LAST")
    return sid


class FakeQuotes:
    """Stands in for `PagedOptionChainFetcher.fetch_exact_contracts`."""

    def __init__(self, clock: FakeClock, web: Any = None) -> None:
        self.clock = clock
        self.web = web
        self.calls: list[tuple[str, list[tuple[int, str, float, str]], int]] = []
        self.errors: list[Exception | None] = []
        self.not_returned: set[tuple[str, float, str]] = set()
        self.unpriced = False
        self.md_type = 3
        self.events: list[str] | None = None

    async def fetch_exact_contracts(
        self, ticker: str, specs: Any, *, underlying_conid: int = 0
    ) -> ExactQuoteResult:
        if self.web is not None:
            assert self.web.refresh_owner == "collector", "tracker ran without the refresh lock"
        self.calls.append(
            (
                ticker,
                [(s.option_conid, s.expiry, s.strike, s.right) for s in specs],
                underlying_conid,
            )
        )
        if self.events is not None:
            self.events.append(f"track:{ticker}")
        if self.errors:
            err = self.errors.pop(0)
            if err is not None:
                raise err
        self.clock.now += timedelta(seconds=1)
        ts = self.clock()
        market_day = ts.astimezone(US_EASTERN).date()
        sid = f"{ticker}_{ts:%Y%m%d_%H%M%S}"
        rows = []
        for s in specs:
            if (s.expiry, s.strike, s.right) in self.not_returned:
                continue
            priced = not self.unpriced
            rows.append(
                OptionContractRow(
                    snapshot_id=sid,
                    snapshot_ts_utc=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    market_date=market_day.isoformat(),
                    market_data_type=self.md_type,
                    underlying=ticker,
                    underlying_conid=underlying_conid or 4815,
                    underlying_price=101.5,
                    option_conid=s.option_conid or 1,
                    expiry=s.expiry,
                    dte=(date.fromisoformat(s.expiry) - market_day).days,
                    strike=s.strike,
                    moneyness_pct=0.0,
                    right=s.right,
                    bid=1.0 if priced else None,
                    ask=1.2 if priced else None,
                    greeks_source="NONE",
                )
            )
        missing = tuple(s for s in specs if (s.expiry, s.strike, s.right) in self.not_returned)
        return ExactQuoteResult(
            snapshot_id=sid,
            snapshot_ts_utc=ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            market_date=market_day.isoformat(),
            market_data_type=self.md_type,
            underlying_price=101.5,
            spot_source="LAST",
            rows=tuple(rows),
            missing=missing,
        )


def _tracker(
    tmp_path: Path, clock: FakeClock, fetcher: Any, **cfg: Any
) -> tuple[TrackerService, CSVRepository]:
    settings = Settings(tracker=TrackerSettings(**cfg))
    repo = CSVRepository(settings.storage, root=tmp_path)
    store = TrackerStore(tmp_path / "data" / "metadata", tmp_path / "data" / "tracked")
    svc = TrackerService(
        settings=settings,
        repo=repo,
        fetcher=fetcher,
        store=store,
        clock=clock,
        validate_ticker=TickerValidator.validate,
    )
    return svc, repo


def _collector(
    tmp_path: Path, web: Any, clock: FakeClock, tracker: TrackerService, **settings: Any
) -> CollectorService:
    store = CollectorStore(tmp_path / "data" / "metadata")
    svc = CollectorService(web, store, clock=clock, wait=clock.wait, tracker=tracker)
    svc.settings = CollectorSettings(enabled=True, **settings)
    return svc


def _ok_meta(ticker: str, sid: str | None = None, *, md_type: int = 3) -> Any:
    return SimpleNamespace(
        meta=SimpleNamespace(
            snapshot_id=sid or f"{ticker}_20261116_150000",
            snapshot_ts_utc="2026-11-16T15:00:00Z",
            market_date="2026-11-16",
            market_data_type=md_type,
            underlying_price=100.0,
            contract_count=36,
            expirations=[EXPIRY],
            spot_source="LAST",
            quote_coverage=1.0,
            strike_step=5.0,
            range_pct=0.35,
        )
    )


NVDA_PIN = TickerPin(symbol="NVDA", range_pct=0.35, strike_step=5.0, dte_min=30, dte_max=120)
AVGO_PIN = TickerPin(symbol="AVGO", range_pct=0.35, strike_step=10.0, dte_min=30, dte_max=120)


def _user_spread(tracker: TrackerService, repo: CSVRepository, ticker: str = "NVDA") -> Any:
    sid = _save_snapshot(repo, ticker, _et(DAY, 9, 0))
    return tracker.register_user(ticker, sid, NVDA_LEGS)


# ------------------------------------------------------------------ registry
def test_user_spread_is_persisted_and_reloaded(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 5))
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock))
    spread = _user_spread(tracker, repo)
    assert spread.source == "USER" and spread.status == "ACTIVE" and spread.qty == 1
    assert spread.created_from_snapshot_id == "NVDA_20261116_140000"
    assert [(leg.side, leg.strike) for leg in spread.legs] == [("SELL", 95.0), ("BUY", 90.0)]
    assert all(leg.option_conid > 0 for leg in spread.legs)  # conId taken from the snapshot
    assert spread.entry_net_credit is not None and spread.entry_net_credit > 0
    reg = tmp_path / "data" / "metadata" / "tracked_spreads.json"
    assert json.loads(reg.read_text(encoding="utf-8"))["spreads"][0]["id"] == spread.id
    assert not list(reg.parent.glob(".*.tmp"))

    again, _ = _tracker(tmp_path, clock, FakeQuotes(clock))
    assert again.spreads() == [spread]


def test_corrupt_registry_is_never_overwritten(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 5))
    reg = tmp_path / "data" / "metadata" / "tracked_spreads.json"
    reg.parent.mkdir(parents=True)
    reg.write_text("{not json", encoding="utf-8")
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock))
    assert tracker.registry_error is not None
    assert tracker.status().registry_error is not None
    with pytest.raises(StorageSchemaError):
        _user_spread(tracker, repo)
    assert reg.read_text(encoding="utf-8") == "{not json"


def test_user_duplicate_is_rejected(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 5))
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock))
    first = _user_spread(tracker, repo)
    with pytest.raises(DuplicateSpreadError):
        tracker.register_user("NVDA", first.created_from_snapshot_id, list(reversed(NVDA_LEGS)))
    tracker.stop(first.id)
    # Once stopped, the same legs may be tracked again.
    assert tracker.register_user("NVDA", first.created_from_snapshot_id, NVDA_LEGS).id != first.id


def test_user_cap_is_enforced(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 5))
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock), max_active_spreads=1)
    first = _user_spread(tracker, repo)
    other = [
        StrategyLegInput(side="SELL", right="P", expiry=EXPIRY, strike=100.0),
        StrategyLegInput(side="BUY", right="P", expiry=EXPIRY, strike=90.0),
    ]
    with pytest.raises(TrackerCapError):
        tracker.register_user("NVDA", first.created_from_snapshot_id, other)


def _legs(*spec: tuple[str, str, str, float, int]) -> list[StrategyLegInput]:
    return [
        StrategyLegInput(side=side, right=right, expiry=exp, strike=k, qty=q)  # type: ignore[arg-type]
        for side, right, exp, k, q in spec
    ]


@pytest.mark.parametrize(
    ("legs", "match"),
    [
        (_legs(("SELL", "P", EXPIRY, 95.0, 1)), "exactly 2 legs"),
        (
            _legs(
                ("SELL", "P", EXPIRY, 95.0, 1),
                ("BUY", "P", EXPIRY, 90.0, 1),
                ("BUY", "P", EXPIRY, 85.0, 1),
            ),
            "exactly 2 legs",
        ),
        (_legs(("SELL", "C", EXPIRY, 95.0, 1), ("BUY", "P", EXPIRY, 90.0, 1)), "must be puts"),
        (
            _legs(("SELL", "P", EXPIRY, 95.0, 1), ("BUY", "P", LATE_EXPIRY, 90.0, 1)),
            "share one expiry",
        ),
        (_legs(("SELL", "P", EXPIRY, 95.0, 1), ("SELL", "P", EXPIRY, 90.0, 1)), "one SELL"),
        (_legs(("SELL", "P", EXPIRY, 95.0, 1), ("BUY", "P", EXPIRY, 95.0, 1)), "different"),
        (_legs(("SELL", "P", EXPIRY, 90.0, 1), ("BUY", "P", EXPIRY, 95.0, 1)), "must be above"),
        (_legs(("SELL", "P", EXPIRY, 95.0, 2), ("BUY", "P", EXPIRY, 90.0, 1)), "must match"),
        (_legs(("SELL", "P", EXPIRY, 96.0, 1), ("BUY", "P", EXPIRY, 90.0, 1)), "not in the snap"),
    ],
)
def test_user_registration_rejects_invalid_spreads(
    tmp_path: Path, legs: list[StrategyLegInput], match: str
) -> None:
    clock = FakeClock(_et(DAY, 9, 5))
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock))
    sid = _save_snapshot(repo, "NVDA", _et(DAY, 9, 0))
    with pytest.raises(ValueError, match=match):
        tracker.register_user("NVDA", sid, legs)
    assert tracker.spreads() == []


def test_user_registration_checks_ticker_snapshot_and_expiry(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 5))
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock))
    sid = _save_snapshot(repo, "NVDA", _et(DAY, 9, 0))
    with pytest.raises(InvalidTickerError):
        tracker.register_user("NVDA1", sid, NVDA_LEGS)
    with pytest.raises(SnapshotNotFoundError):
        tracker.register_user("NVDA", "NVDA_20261116_150001", NVDA_LEGS)
    with pytest.raises(SnapshotNotFoundError):
        tracker.register_user("AVGO", sid.replace("NVDA", "AVGO"), NVDA_LEGS)
    clock.now = _et(date(2026, 12, 21), 9, 0)
    with pytest.raises(ValueError, match="in the past"):
        tracker.register_user("NVDA", sid, NVDA_LEGS)


def test_stop_keeps_data_and_cannot_repeat(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 5))
    quotes = FakeQuotes(clock)
    tracker, repo = _tracker(tmp_path, clock, quotes)
    spread = _user_spread(tracker, repo)
    asyncio.run(tracker.track_ticker(Slot("INTERVAL", _et(DAY, 9, 45)), "NVDA", set()))
    stopped = tracker.stop(spread.id)
    assert stopped.status == "STOPPED" and stopped.status_changed_utc
    assert list((tmp_path / "data" / "tracked" / "NVDA").glob("*.csv"))  # data kept
    with pytest.raises(TrackerStateError):
        tracker.stop(spread.id)
    with pytest.raises(KeyError):
        tracker.stop("NVDA-20261116-00000000")
    assert tracker.store.read_runs()[-1].status == "STOPPED"
    # A stopped spread is not quoted any more.
    assert (
        asyncio.run(tracker.track_ticker(Slot("INTERVAL", _et(DAY, 10, 45)), "NVDA", set())) == []
    )


# ------------------------------------------------------------------ AUTO picks
def test_auto_pick_waits_for_not_before_time_and_picks_once_per_day(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock))
    early = _save_snapshot(repo, "NVDA", _et(DAY, 9, 45, 20))
    slot_early = Slot("INTERVAL", _et(DAY, 9, 45))
    assert tracker.auto_pick(slot_early, "NVDA", snapshot_id=early, usable=True) is None
    on_time = _save_snapshot(repo, "NVDA", _et(DAY, 10, 45, 20))
    slot = Slot("INTERVAL", _et(DAY, 10, 45))
    assert tracker.auto_pick(slot, "NVDA", snapshot_id=on_time, usable=False) is None
    rec = tracker.auto_pick(slot, "NVDA", snapshot_id=on_time, usable=True)
    assert rec is not None and rec.status == "REGISTERED"
    assert rec.source_snapshot_id == on_time and rec.market_date == DAY.isoformat()
    assert [(leg.side, leg.right) for leg in rec.legs] == [("SELL", "P"), ("BUY", "P")]
    spread = tracker.get(rec.spread_id or "")
    assert spread.source == "AUTO" and spread.created_from_snapshot_id == on_time
    later = _save_snapshot(repo, "NVDA", _et(DAY, 11, 45, 20))
    assert (
        tracker.auto_pick(
            Slot("INTERVAL", _et(DAY, 11, 45)), "NVDA", snapshot_id=later, usable=True
        )
        is None
    )
    # The decision survives a restart.
    again, _ = _tracker(tmp_path, clock, FakeQuotes(clock))
    assert (
        again.auto_pick(Slot("INTERVAL", _et(DAY, 11, 45)), "NVDA", snapshot_id=later, usable=True)
        is None
    )


def test_auto_pick_matches_the_engine_mode_b_legs(tmp_path: Path) -> None:
    from src.analytics.backtest import backtest_bull_put_from_repo

    clock = FakeClock(_et(DAY, 10, 45))
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock))
    sid = _save_snapshot(repo, "NVDA", _et(DAY, 10, 45, 20))
    rec = tracker.auto_pick(
        Slot("INTERVAL", _et(DAY, 10, 45)), "NVDA", snapshot_id=sid, usable=True
    )
    assert rec is not None
    engine = backtest_bull_put_from_repo(repo, "NVDA", sid, mode="AUTO_BUILD")
    assert [(leg.side, leg.right, leg.expiry, leg.strike) for leg in rec.legs] == [
        (leg.side, leg.right, leg.expiry, leg.strike) for leg in engine.entry.legs
    ]


def test_auto_duplicate_and_cap_are_logged(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 10, 45))
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock), max_active_spreads=1)
    sid = _save_snapshot(repo, "NVDA", _et(DAY, 10, 45, 20))
    first = tracker.auto_pick(
        Slot("INTERVAL", _et(DAY, 10, 45)), "NVDA", snapshot_id=sid, usable=True
    )
    assert first is not None and first.status == "REGISTERED"
    # Next day: same legs again -> duplicate (checked before the cap).
    day2 = DAY + timedelta(days=1)
    sid2 = _save_snapshot(repo, "NVDA", _et(day2, 10, 45, 20))
    dup = tracker.auto_pick(
        Slot("INTERVAL", _et(day2, 10, 45)), "NVDA", snapshot_id=sid2, usable=True
    )
    assert dup is not None and dup.status == "AUTO_DUPLICATE" and dup.spread_id == first.spread_id
    assert dup.legs and dup.source_snapshot_id == sid2
    # AVGO at the cap -> skipped and warned.
    sid3 = _save_snapshot(repo, "AVGO", _et(day2, 10, 45, 30))
    cap = tracker.auto_pick(
        Slot("INTERVAL", _et(day2, 10, 45)), "AVGO", snapshot_id=sid3, usable=True
    )
    assert cap is not None and cap.status == "AUTO_SKIPPED_CAP" and cap.legs
    assert len(tracker.active()) == 1
    warnings = tracker.status().warnings
    assert any("skipped at the cap" in w for w in warnings)
    assert any("1 of 1 tracked spreads are active" in w for w in warnings)


def test_no_usable_snapshot_is_logged_at_the_closing_slot(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 16, 15))
    tracker, _repo = _tracker(tmp_path, clock, FakeQuotes(clock))
    recs = tracker.finish_slot(Slot("CLOSING", _et(DAY, 16, 15)), ["NVDA", "AVGO"])
    assert [(r.ticker, r.status) for r in recs] == [
        ("NVDA", "AUTO_SKIPPED_NO_USABLE"),
        ("AVGO", "AUTO_SKIPPED_NO_USABLE"),
    ]
    assert tracker.finish_slot(Slot("INTERVAL", _et(DAY, 15, 45)), ["NVDA"]) == []


def test_cap_warning_at_80_percent(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 5))
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock), max_active_spreads=1)
    assert tracker.status().warnings == []
    _user_spread(tracker, repo)
    assert tracker.status().warnings == [
        "1 of 1 tracked spreads are active (over 80% of tracker.max_active_spreads)."
    ]


# ------------------------------------------------------------------ slot quoting via the collector
def _two_ticker_setup(
    tmp_path: Path, outcomes: dict[str, list[Any]], *, slot_time: tuple[int, int] = (9, 45)
) -> tuple[CollectorService, FakeWeb, FakeQuotes, TrackerService, list[TrackedSpread], Slot]:
    clock = FakeClock(_et(DAY, 9, 5))
    web = FakeWeb(outcomes, clock)
    quotes = FakeQuotes(clock, web)
    tracker, repo = _tracker(tmp_path, clock, quotes, auto_pick=False)
    spreads = [_user_spread(tracker, repo, "NVDA"), _user_spread(tracker, repo, "AVGO")]
    slot = Slot("INTERVAL", _et(DAY, *slot_time))
    clock.now = slot.at_utc
    collector = _collector(tmp_path, web, clock, tracker, tickers=[NVDA_PIN, AVGO_PIN])
    return collector, web, quotes, tracker, spreads, slot


def test_tracker_quotes_exact_legs_after_each_snapshot_under_the_lock(tmp_path: Path) -> None:
    collector, web, quotes, tracker, spreads, slot = _two_ticker_setup(
        tmp_path, {"NVDA": [_ok_meta("NVDA")], "AVGO": [_ok_meta("AVGO")]}
    )
    events: list[str] = []
    quotes.events = events
    original = web.refresh_chain_locked

    async def _logged(req: Any, *, collection_mode: str) -> Any:
        events.append(f"snapshot:{req.ticker}")
        return await original(req, collection_mode=collection_mode)

    web.refresh_chain_locked = _logged  # type: ignore[method-assign]
    records = asyncio.run(collector.run_slot(slot))
    assert [r.status for r in records] == ["OK", "OK"]  # collector records unchanged
    assert events == ["snapshot:NVDA", "track:NVDA", "snapshot:AVGO", "track:AVGO"]
    nvda = spreads[0]
    assert quotes.calls[0] == (
        "NVDA",
        [(leg.option_conid, leg.expiry, leg.strike, leg.right) for leg in nvda.legs],
        nvda.underlying_conid,
    )
    runs = [r for r in tracker.store.read_runs() if r.kind == "QUOTE"]
    assert [(r.ticker, r.status) for r in runs] == [("NVDA", "OK"), ("AVGO", "OK")]
    first = runs[0]
    assert first.underlying_price == 101.5 and first.spot_source == "LAST"
    assert first.market_data_type == 3 and first.priced_legs == 2
    path = tmp_path / "data" / "tracked" / "NVDA" / f"{first.tracked_snapshot_id}.csv"
    header = path.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert tuple(header) == COLUMNS  # same 27-column schema
    assert not (tmp_path / "data" / "snapshots" / "NVDA" / path.name).exists()
    quoted_at = datetime.strptime(first.tracked_snapshot_id or "", "NVDA_%Y%m%d_%H%M%S")
    assert tracker.get(nvda.id).last_quote_utc == quoted_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert web.refresh_owner is None


def test_shared_legs_are_requested_once(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 5))
    quotes = FakeQuotes(clock)
    tracker, repo = _tracker(tmp_path, clock, quotes, auto_pick=False)
    sid = _save_snapshot(repo, "NVDA", _et(DAY, 9, 0))
    tracker.register_user("NVDA", sid, NVDA_LEGS)
    tracker.register_user(
        "NVDA",
        sid,
        [
            StrategyLegInput(side="SELL", right="P", expiry=EXPIRY, strike=100.0),
            StrategyLegInput(side="BUY", right="P", expiry=EXPIRY, strike=95.0),
        ],
    )
    recs = asyncio.run(tracker.track_ticker(Slot("INTERVAL", _et(DAY, 9, 45)), "NVDA", set()))
    assert [r.status for r in recs] == ["OK", "OK"]
    assert len(quotes.calls) == 1
    assert sorted(s[2] for s in quotes.calls[0][1]) == [90.0, 95.0, 100.0]  # 95P only once
    assert len({r.tracked_snapshot_id for r in recs}) == 1  # one file per ticker per slot


def test_missing_contract_and_no_quote_still_record_spot(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    quotes = FakeQuotes(clock)
    tracker, repo = _tracker(tmp_path, clock, quotes, auto_pick=False)
    _user_spread(tracker, repo)
    quotes.not_returned = {(EXPIRY, 90.0, "P")}
    rec = asyncio.run(tracker.track_ticker(Slot("INTERVAL", _et(DAY, 9, 45)), "NVDA", set()))[0]
    assert rec.status == "PARTIAL" and rec.priced_legs == 1
    assert rec.missing_legs == [f"BUY P 90 {EXPIRY}: NOT_RETURNED"]
    quotes.not_returned = set()
    quotes.unpriced = True
    quotes.md_type = 4
    rec = asyncio.run(tracker.track_ticker(Slot("CLOSING", _et(DAY, 16, 15)), "NVDA", set()))[0]
    assert rec.status == "NO_QUOTE" and rec.priced_legs == 0
    assert (rec.underlying_price, rec.spot_source, rec.market_data_type) == (101.5, "LAST", 4)
    assert "FROZEN_DATA" in rec.quality_flags
    assert rec.tracked_snapshot_id is not None  # the file still records the spot


def test_gateway_down_on_attempt_one_tracks_after_the_retry(tmp_path: Path) -> None:
    collector, _web, quotes, tracker, _spreads, slot = _two_ticker_setup(
        tmp_path,
        {
            "NVDA": [GatewayConnectionError("down"), _ok_meta("NVDA")],
            "AVGO": [GatewayConnectionError("down"), _ok_meta("AVGO")],
        },
    )
    records = asyncio.run(collector.run_slot(slot))
    assert [(r.ticker, r.attempt, r.status) for r in records] == [
        ("NVDA", 1, "FAILED"),
        ("AVGO", 1, "FAILED"),
        ("NVDA", 2, "OK"),
        ("AVGO", 2, "OK"),
    ]
    assert [c[0] for c in quotes.calls] == ["NVDA", "AVGO"]  # once each, after the retry
    runs = [r for r in tracker.store.read_runs() if r.kind == "QUOTE"]
    assert [(r.ticker, r.status) for r in runs] == [("NVDA", "OK"), ("AVGO", "OK")]


def test_gateway_down_all_slot_records_failed_tracker_steps(tmp_path: Path) -> None:
    down = GatewayConnectionError("down")
    collector, _web, quotes, tracker, _spreads, slot = _two_ticker_setup(
        tmp_path, {"NVDA": [down, down], "AVGO": [down, down]}
    )
    asyncio.run(collector.run_slot(slot))
    assert quotes.calls == []
    runs = [r for r in tracker.store.read_runs() if r.kind == "QUOTE"]
    assert [(r.ticker, r.status) for r in runs] == [("NVDA", "FAILED"), ("AVGO", "FAILED")]
    assert all((r.error or "").startswith("GATEWAY_OFFLINE") for r in runs)


def test_tracker_failure_never_triggers_retries_or_stops_other_tickers(tmp_path: Path) -> None:
    collector, web, quotes, tracker, _spreads, slot = _two_ticker_setup(
        tmp_path, {"NVDA": [_ok_meta("NVDA")], "AVGO": [_ok_meta("AVGO")]}
    )
    quotes.errors = [GatewayConnectionError("dropped mid-step"), None]
    records = asyncio.run(collector.run_slot(slot))
    assert [(r.ticker, r.attempt, r.status) for r in records] == [
        ("NVDA", 1, "OK"),
        ("AVGO", 1, "OK"),
    ]
    assert len(web.calls) == 2  # no collector retry
    assert len(quotes.calls) == 2  # no tracker retry
    runs = [r for r in tracker.store.read_runs() if r.kind == "QUOTE"]
    assert [(r.ticker, r.status) for r in runs] == [("NVDA", "FAILED"), ("AVGO", "OK")]
    assert (runs[0].error or "").startswith("GATEWAY_OFFLINE")
    assert not (tmp_path / "data" / "tracked" / "NVDA").exists()  # no file for the failure


def test_busy_lock_records_skipped_tracker_steps(tmp_path: Path) -> None:
    collector, web, quotes, tracker, _spreads, slot = _two_ticker_setup(tmp_path, {})
    web.lock_free = False  # a manual refresh holds the lock
    asyncio.run(collector.run_slot(slot))
    assert quotes.calls == []
    runs = [r for r in tracker.store.read_runs() if r.kind == "QUOTE"]
    assert [(r.ticker, r.status) for r in runs] == [("NVDA", "SKIPPED"), ("AVGO", "SKIPPED")]


def test_user_spread_on_an_uncollected_ticker_is_quoted_in_the_slot(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 5))
    web = FakeWeb({"NVDA": [_ok_meta("NVDA")]}, clock)
    quotes = FakeQuotes(clock, web)
    tracker, repo = _tracker(tmp_path, clock, quotes, auto_pick=False)
    _user_spread(tracker, repo, "PLTR")
    collector = _collector(tmp_path, web, clock, tracker, tickers=[NVDA_PIN])
    clock.now = _et(DAY, 9, 45)
    asyncio.run(collector.run_slot(Slot("INTERVAL", _et(DAY, 9, 45))))
    assert [c[0] for c in quotes.calls] == ["PLTR"]
    assert [r.status for r in tracker.store.read_runs() if r.kind == "QUOTE"] == ["OK"]


def test_failed_write_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    quotes = FakeQuotes(clock)
    tracker, repo = _tracker(tmp_path, clock, quotes, auto_pick=False)
    _user_spread(tracker, repo)

    def _boom(self: pl.DataFrame, path: Any, *a: Any, **k: Any) -> None:
        Path(path).write_text("partial", encoding="utf-8")
        raise OSError("disk full")

    monkeypatch.setattr(pl.DataFrame, "write_csv", _boom)
    rec = asyncio.run(tracker.track_ticker(Slot("INTERVAL", _et(DAY, 9, 45)), "NVDA", set()))[0]
    assert rec.status == "FAILED" and "disk full" in (rec.error or "")
    folder = tmp_path / "data" / "tracked" / "NVDA"
    assert not folder.exists() or list(folder.iterdir()) == []


def test_tracked_files_are_never_overwritten(tmp_path: Path) -> None:
    store = TrackerStore(tmp_path / "meta", tmp_path / "data" / "tracked")
    row = OptionContractRow(
        snapshot_id="NVDA_20261116_150000",
        snapshot_ts_utc="2026-11-16T15:00:00Z",
        market_date="2026-11-16",
        market_data_type=3,
        underlying="NVDA",
        underlying_conid=1,
        underlying_price=100.0,
        option_conid=2,
        expiry=EXPIRY,
        dte=32,
        strike=95.0,
        moneyness_pct=-5.0,
        right="P",
        greeks_source="NONE",
    )
    path = store.save_quotes("NVDA", [row])
    before = path.read_bytes()
    with pytest.raises(StorageSchemaError, match="already exists"):
        store.save_quotes("NVDA", [row])
    assert path.read_bytes() == before
    with pytest.raises(StorageSchemaError):
        store.quote_path("NVDA", "../x")


# ------------------------------------------------------------------ lifecycle
def test_expiry_day_closing_slot_is_tracked_then_expired(tmp_path: Path) -> None:
    exp_day = date.fromisoformat(EXPIRY)
    clock = FakeClock(_et(DAY, 9, 5))
    web = FakeWeb({"NVDA": [_ok_meta("NVDA"), _ok_meta("NVDA")]}, clock)
    quotes = FakeQuotes(clock, web)
    tracker, repo = _tracker(tmp_path, clock, quotes, auto_pick=False)
    spread = _user_spread(tracker, repo)
    collector = _collector(tmp_path, web, clock, tracker, tickers=[NVDA_PIN])
    for slot in (Slot("INTERVAL", _et(exp_day, 15, 45)), Slot("CLOSING", _et(exp_day, 16, 15))):
        clock.now = slot.at_utc
        asyncio.run(collector.run_slot(slot))
    assert len(quotes.calls) == 2
    assert tracker.get(spread.id).status == "EXPIRED"
    assert [r.status for r in tracker.store.read_runs()][-3:] == ["OK", "OK", "EXPIRED"]


def test_early_close_expiry_uses_the_13_15_closing_slot(tmp_path: Path) -> None:
    early = date(2026, 11, 27)  # day after Thanksgiving, 13:00 close
    assert slots_for_day(early, 120)[-1] == Slot("CLOSING", _et(early, 13, 15))
    clock = FakeClock(_et(DAY, 9, 5))
    web = FakeWeb({"NVDA": [_ok_meta("NVDA")]}, clock)
    quotes = FakeQuotes(clock, web)
    tracker, repo = _tracker(tmp_path, clock, quotes, auto_pick=False)
    sid = _save_snapshot(repo, "NVDA", _et(DAY, 9, 0))
    # A Friday expiry inside the synthetic chain: register against a chain holding 2026-11-27.
    df = repo.load_snapshot("NVDA", sid)
    early_df = df.with_columns(
        pl.when(pl.col("expiry") == EXPIRY)
        .then(pl.lit("2026-11-27"))
        .otherwise(pl.col("expiry"))
        .alias("expiry"),
        pl.lit("NVDA_20261116_140100").alias("snapshot_id"),
    )
    repo.save_snapshot("NVDA", early_df.to_dicts())
    spread = tracker.register_user(
        "NVDA",
        "NVDA_20261116_140100",
        [
            StrategyLegInput(side="SELL", right="P", expiry="2026-11-27", strike=95.0),
            StrategyLegInput(side="BUY", right="P", expiry="2026-11-27", strike=90.0),
        ],
    )
    collector = _collector(tmp_path, web, clock, tracker, tickers=[NVDA_PIN])
    slot = Slot("CLOSING", _et(early, 13, 15))
    clock.now = slot.at_utc
    asyncio.run(collector.run_slot(slot))
    assert tracker.get(spread.id).status == "EXPIRED"
    assert [r.status for r in tracker.store.read_runs() if r.spread_id == spread.id] == [
        "OK",
        "EXPIRED",
    ]


def test_missed_slots_and_overdue_expiry_after_a_restart(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 5))
    web = FakeWeb({}, clock)
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock), auto_pick=False)
    spread = _user_spread(tracker, repo)
    collector = _collector(tmp_path, web, clock, tracker, tickers=[NVDA_PIN], interval_min=60)
    collector.settings = collector.settings.model_copy(
        update={"enabled_since_utc": "2026-11-16T14:05:00Z"}
    )
    clock.now = _et(DAY, 12, 0)  # app restarted at 12:00 ET
    collector.record_missed_slots()
    missed = [r for r in tracker.store.read_runs() if r.status == "MISSED"]
    assert [r.planned_slot_utc for r in missed] == [
        "2026-11-16T14:45:00Z",
        "2026-11-16T15:45:00Z",
        "2026-11-16T16:45:00Z",
    ]
    assert all(r.spread_id == spread.id for r in missed)
    # Restart after the expiry date: the spread is expired, not left ACTIVE.
    clock.now = _et(date(2026, 12, 21), 9, 0)
    collector.record_missed_slots()
    assert tracker.get(spread.id).status == "EXPIRED"


def test_record_missed_ignores_spreads_created_later_or_stopped(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 11, 0))
    tracker, repo = _tracker(tmp_path, clock, FakeQuotes(clock), auto_pick=False)
    spread = _user_spread(tracker, repo)
    slots = slots_between(_et(DAY, 9, 0), _et(DAY, 13, 0), 60)
    recs = tracker.record_missed(slots)
    assert [r.planned_slot_utc for r in recs] == ["2026-11-16T16:45:00Z", "2026-11-16T17:45:00Z"]
    clock.now = _et(DAY, 13, 0)
    tracker.stop(spread.id)
    assert tracker.record_missed(slots_between(_et(DAY, 13, 0), _et(DAY, 16, 0), 60)) == []


# ------------------------------------------------------------------ dry run across 30 DTE
def test_dry_run_auto_and_user_spreads_to_expiry_without_gaps(tmp_path: Path) -> None:
    """One AUTO (NVDA) and one USER (AVGO) spread from 32 DTE to expiry: every slot quoted."""
    clock = FakeClock(_et(DAY, 9, 0))
    repo_holder: dict[str, CSVRepository] = {}

    class DryWeb(FakeWeb):
        async def refresh_chain_locked(self, req: Any, *, collection_mode: str) -> Any:
            assert self.refresh_owner == "collector"
            self.calls.append((req.ticker, 0, 0, 0, 0, collection_mode))
            now = self.clock()
            local = now.astimezone(US_EASTERN)
            if (
                req.ticker == "NVDA"
                and local.date() == DAY
                and (local.hour, local.minute) == (11, 45)
            ):
                sid = _save_snapshot(repo_holder["repo"], "NVDA", now + timedelta(seconds=20))
                return _ok_meta("NVDA", sid)
            return _ok_meta(req.ticker, md_type=4)  # frozen: kept, not usable, no retry

    web = DryWeb({}, clock)
    quotes = FakeQuotes(clock, web)
    tracker, repo = _tracker(tmp_path, clock, quotes, auto_pick_not_before_et="10:45")
    repo_holder["repo"] = repo
    user = _user_spread(tracker, repo, "AVGO")
    collector = _collector(
        tmp_path, web, clock, tracker, tickers=[NVDA_PIN, AVGO_PIN], interval_min=120
    )

    exp_day = date.fromisoformat(EXPIRY)
    slots = slots_between(_et(DAY, 9, 0), _et(exp_day + timedelta(days=3), 9, 0), 120)
    picked = False
    for slot in slots:
        clock.now = slot.at_utc
        asyncio.run(collector.run_slot(slot))
        if not picked and tracker.active("NVDA"):
            picked = True
            # One AUTO pick is enough for the dry run.
            tracker.cfg = tracker.cfg.model_copy(update={"auto_pick": False})

    auto = next(s for s in tracker.spreads() if s.source == "AUTO")
    assert auto.created_from_snapshot_id.startswith("NVDA_20261116_1645")  # 11:45 ET slot
    runs = tracker.store.read_runs()
    for spread, first_slot in ((auto, _et(DAY, 11, 45)), (user, _et(DAY, 9, 45))):
        quoted = [r for r in runs if r.kind == "QUOTE" and r.spread_id == spread.id]
        expected = [
            s.at_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
            for s in slots
            if first_slot <= s.at_utc <= _et(exp_day, 16, 15)
        ]
        assert [r.planned_slot_utc for r in quoted] == expected  # no gaps
        assert {r.status for r in quoted} == {"OK"}
        dtes = {
            (date.fromisoformat(EXPIRY) - date.fromisoformat(r.market_date or "")).days
            for r in quoted
        }
        assert max(dtes) == 32 and min(dtes) == 0  # crosses the 30 DTE boundary to expiry
        assert tracker.get(spread.id).status == "EXPIRED"
        files = list((tmp_path / "data" / "tracked" / spread.ticker).glob("*.csv"))
        assert len(files) == len(expected)
    autos = [(r.ticker, r.market_date, r.status) for r in runs if r.kind == "AUTO"]
    # NVDA picked at 11:45 ET on day 1; AUTO picks were then switched off for the dry run.
    assert autos == [("NVDA", DAY.isoformat(), "REGISTERED")]


# ------------------------------------------------------------------ broker: exact contracts
def _exact_fetcher(fake_ib: FakeIB, clock_ts: datetime) -> PagedOptionChainFetcher:
    client = IBKRBrokerClient(Settings(), ib_factory=lambda: fake_ib)
    return PagedOptionChainFetcher(client, clock=lambda: clock_ts, sleep=_noop_sleep)


def test_fetch_exact_contracts_requests_only_those_contracts() -> None:
    fake_ib = FakeIB()
    qualify_calls: list[Any] = []

    def _qualify(*contracts: Any) -> list[Any]:
        qualify_calls.append(contracts)
        return list(contracts)

    fake_ib.qualify_handler = _qualify
    fetcher = _exact_fetcher(fake_ib, _et(DAY, 9, 45, 30))
    specs = [
        ExactContractSpec(9001, EXPIRY, 95.0, "P"),
        ExactContractSpec(9002, EXPIRY, 90.0, "P"),
        ExactContractSpec(9003, "2026-11-13", 90.0, "P"),  # expired: never requested
    ]
    res = asyncio.run(fetcher.fetch_exact_contracts("NVDA", specs, underlying_conid=4815))
    assert ReadOnlyIBGuard.verify_locked(fetcher.client.ib)
    assert qualify_calls == []  # conIds known: no qualification at all
    subscribed = [c for c, *_ in fake_ib.subscribed]
    assert [getattr(c, "conId", 0) for c in subscribed] == [4815, 9001, 9002]
    assert all(c in fake_ib.cancelled for c in subscribed)  # 100% teardown
    assert [(r.option_conid, r.strike, r.dte) for r in res.rows] == [
        (9001, 95.0, 32),
        (9002, 90.0, 32),
    ]
    assert res.missing == (specs[2],)
    assert res.snapshot_id == "NVDA_20261116_144530"
    assert res.market_data_type == 3 and res.spot_source in ("LAST", "MID", "CLOSE")


def test_fetch_exact_contracts_enforces_the_cap_and_ticker_rule() -> None:
    fetcher = _exact_fetcher(FakeIB(), _et(DAY, 9, 45))
    too_many = [ExactContractSpec(i + 1, EXPIRY, 50.0 + i, "P") for i in range(401)]
    with pytest.raises(PacingViolationError):
        asyncio.run(fetcher.fetch_exact_contracts("NVDA", too_many))
    with pytest.raises(InvalidTickerError):
        asyncio.run(fetcher.fetch_exact_contracts("../NVDA", too_many[:1]))


# ------------------------------------------------------------------ config
@pytest.mark.parametrize(
    "tracked_dir",
    ["data", "data/snapshots", "data/snapshots/tracked", "tracked", "../data/tracked", "/tmp/x"],
)
def test_tracked_dir_must_be_inside_data_and_outside_snapshots(tracked_dir: str) -> None:
    with pytest.raises(ValidationError, match="tracked_dir"):
        Settings(storage=StorageSettings(), tracker=TrackerSettings(tracked_dir=tracked_dir))


@pytest.mark.parametrize(
    "bad",
    [
        {"auto_pick_not_before_et": "9:45"},
        {"auto_pick_not_before_et": "24:00"},
        {"auto_pick_not_before_et": "10:60"},
        {"auto_pick_not_before_et": "ten"},
        {"max_active_spreads": 0},
        {"max_active_spreads": 201},
    ],
)
def test_tracker_settings_validation(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        TrackerSettings(**bad)


def test_tracker_settings_defaults() -> None:
    cfg = TrackerSettings()
    assert (cfg.max_active_spreads, cfg.auto_pick, cfg.auto_pick_not_before_et) == (
        120,
        True,
        "10:45",
    )
    assert cfg.tracked_dir == "data/tracked" and cfg.not_before_minutes() == 645
    assert Settings().tracker == cfg


def test_tracked_leg_and_spread_models_validate() -> None:
    with pytest.raises(ValidationError):
        TrackedLeg(side="SELL", right="P", expiry=EXPIRY, strike=0.0, option_conid=1)
    leg = TrackedLeg(side="SELL", right="P", expiry=EXPIRY, strike=95.0, option_conid=1)
    with pytest.raises(ValidationError):
        TrackedSpread(
            id="x",
            ticker="nvda1",
            source="USER",
            legs=[leg, leg],
            qty=1,
            underlying_conid=1,
            created_from_snapshot_id="NVDA_20261116_150000",
            created_at_utc="2026-11-16T15:00:00Z",
            created_market_date="2026-11-16",
        )
