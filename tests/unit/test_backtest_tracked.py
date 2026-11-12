"""Backtest engine version 2: tracked quotes, slot pairing, gaps, calendar, registry helper.

Synthetic normal snapshots (`data/snapshots`), collector and tracker run logs, tracked files
(`data/tracked`) and the tracked-spread registry are written into a temporary repository; the
engine only reads them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest

from src.analytics.backtest import (
    ENGINE_VERSION,
    INFO_FLAGS,
    TRACKER_FLAGS,
    backtest_bull_put_from_repo,
    backtest_tracked_spread,
)
from src.analytics.strategies import StrategyLegInput
from src.core.calendar import US_EASTERN, session_for
from src.core.config import StorageSettings
from src.core.exceptions import SnapshotNotFoundError
from src.storage.collector_store import CollectorStore, RunRecord
from src.storage.csv_repository import CSVRepository
from src.storage.schema import OptionContractRow
from src.storage.tracker_store import TrackedLeg, TrackedSpread, TrackerRunRecord, TrackerStore
from tests.unit.backtest_v1_scenarios import GOLDEN_PATH, scenarios
from tests.unit.test_backtest import ENTRY_QUOTES, EXPIRY, LEGS, _chain

ENTRY_SLOT = datetime(2026, 11, 16, 16, 45, tzinfo=UTC)  # 11:45 ET
SPREAD_ID = "NVDA-20261116-0000abcd"
TRACKED_Q = {(100.0, "P"): (1.0, 1.2), (95.0, "P"): (0.4, 0.5)}


def _et(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=US_EASTERN).astimezone(UTC)


def _iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class World:
    """A temporary data folder with snapshots, run logs, tracked files and a registry."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = CSVRepository(StorageSettings(), root=root)
        meta = self.repo.index_path.parent
        self.collector = CollectorStore(meta)
        self.tracker = TrackerStore(meta, root / "data" / "tracked")
        self.entry_id = self.snapshot(ENTRY_SLOT, 102.0, ENTRY_QUOTES)

    def snapshot(
        self,
        slot: datetime,
        spot: float,
        quotes: dict[tuple[float, str], Any] | None = None,
        *,
        expiries: tuple[str, ...] = (EXPIRY,),
        strikes: tuple[float, ...] = (90.0, 95.0, 100.0, 105.0, 110.0),
        attempt: int = 1,
        spot_source: str = "LAST",
        md_type: int = 3,
        scheduled: bool = True,
        delay_s: int = 20,
    ) -> str:
        ts = slot + timedelta(seconds=delay_s + 60 * (attempt - 1))
        df = _chain(ts, spot, quotes, expiries=expiries, strikes=strikes, md_type=md_type)
        meta = self.repo.save_snapshot(
            "NVDA",
            df.to_dicts(),
            range_pct=0.10,
            strike_step=5.0,
            spot_source=spot_source,  # type: ignore[arg-type]
            collection_mode="SCHEDULED" if scheduled else "MANUAL",
        )
        if scheduled:
            self.collector.append_runs(
                [
                    RunRecord(
                        planned_slot_utc=_iso(slot),
                        slot_kind="CLOSING" if _et(slot.date(), 16, 15) == slot else "INTERVAL",
                        actual_run_utc=_iso(ts),
                        ticker="NVDA",
                        attempt=attempt,
                        status="OK",
                        snapshot_id=meta.snapshot_id,
                    )
                ]
            )
        return meta.snapshot_id

    def register(self, *, status: str = "ACTIVE", changed: datetime | None = None) -> None:
        legs = [
            TrackedLeg(side="SELL", right="P", expiry=EXPIRY, strike=100.0, option_conid=1003),
            TrackedLeg(side="BUY", right="P", expiry=EXPIRY, strike=95.0, option_conid=1005),
        ]
        spread = TrackedSpread(
            id=SPREAD_ID,
            ticker="NVDA",
            source="USER",
            legs=legs,
            qty=1,
            underlying_conid=4815,
            created_from_snapshot_id=self.entry_id,
            created_at_utc=_iso(ENTRY_SLOT),
            created_market_date="2026-11-16",
            status=status,  # type: ignore[arg-type]
            status_changed_utc=_iso(changed) if changed else None,
        )
        existing = [s for s in self.tracker.load_spreads() if s.id != SPREAD_ID]
        self.tracker.save_spreads([*existing, spread])

    def track(
        self,
        slot: datetime,
        *,
        status: str = "OK",
        quotes: dict[tuple[float, str], tuple[float | None, float | None]] | None = None,
        spot: float = 101.0,
        row_spot: float = 101.0,
        spot_source: str = "LAST",
        md_type: int = 3,
        write_file: bool = True,
        spread_id: str = SPREAD_ID,
        delay_s: int = 40,
    ) -> str | None:
        ts = slot + timedelta(seconds=delay_s)
        sid = f"NVDA_{ts:%Y%m%d_%H%M%S}"
        market_day = ts.astimezone(US_EASTERN).date()
        file_id: str | None = None
        if write_file:
            rows = [
                OptionContractRow(
                    snapshot_id=sid,
                    snapshot_ts_utc=_iso(ts),
                    market_date=market_day.isoformat(),
                    market_data_type=md_type,
                    underlying="NVDA",
                    underlying_conid=4815,
                    underlying_price=row_spot,
                    option_conid=1000 + int(strike),
                    expiry=EXPIRY,
                    dte=(date.fromisoformat(EXPIRY) - market_day).days,
                    strike=strike,
                    moneyness_pct=0.0,
                    right="P" if right == "P" else "C",
                    bid=bid,
                    ask=ask,
                    greeks_source="NONE",
                )
                for (strike, right), (bid, ask) in (quotes or TRACKED_Q).items()
            ]
            self.tracker.save_quotes("NVDA", rows)
            file_id = sid
        self.tracker.append_runs(
            [
                TrackerRunRecord(
                    kind="QUOTE",
                    planned_slot_utc=_iso(slot),
                    slot_kind="INTERVAL",
                    actual_run_utc=_iso(ts),
                    ticker="NVDA",
                    market_date=market_day.isoformat(),
                    spread_id=spread_id,
                    status=status,  # type: ignore[arg-type]
                    tracked_snapshot_id=file_id if status != "FAILED" else None,
                    underlying_price=spot if status not in ("MISSED", "FAILED") else None,
                    spot_source=spot_source if status not in ("MISSED", "FAILED") else None,  # type: ignore[arg-type]
                    market_data_type=md_type if status not in ("MISSED", "FAILED") else None,
                )
            ]
        )
        return file_id

    def run(self, **kw: Any) -> Any:
        return backtest_bull_put_from_repo(
            self.repo, "NVDA", self.entry_id, mode="LEGS", legs=LEGS, **kw
        )


DAY_OUT = date(2026, 11, 25)  # legs at 23 DTE: outside a 30-120 DTE snapshot
SLOT_OUT = _et(DAY_OUT, 11, 45)


def _step(result: Any, snapshot_prefix: str) -> Any:
    return next(s for s in result.steps if s.snapshot_id.startswith(snapshot_prefix))


# ------------------------------------------------------------------ fallback to tracked rows
def test_leg_outside_the_window_switches_to_the_tracked_row(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    sid = w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",))
    tid = w.track(SLOT_OUT)
    r = w.run()
    step = _step(r, sid)
    assert step.status == "OK" and step.kind == "SNAPSHOT"
    assert [(m.status, m.quote_source) for m in step.legs] == [("PRICED", "TRACKED")] * 2
    assert step.spread_value_per_share == pytest.approx(1.1 - 0.45)
    assert step.tracked_snapshot_id == tid
    assert step.slot_utc == _iso(SLOT_OUT)
    assert step.spot == 101.0 and step.spot_source == "LAST"  # from the normal snapshot
    assert "MIXED_QUOTE_SOURCES" not in step.flags
    assert step.tracker_run_entries == ["tracker_runs.jsonl:L1"]
    repro = r.reproducibility
    assert repro.engine_version == ENGINE_VERSION == "2"
    assert repro.tracked_data_used is True
    assert repro.tracked_files == [tid] and repro.tracker_run_entries == ["tracker_runs.jsonl:L1"]


def test_mixed_sources_are_flagged_informational(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    # 95 not listed on a $10 grid: MISSING_NOT_LISTED -> tracked row; 100 from the snapshot.
    sid = w.snapshot(
        SLOT_OUT, 101.0, {(100.0, "P"): (1.9, 2.1, 2.0, 2.0)}, strikes=(90.0, 100.0, 110.0)
    )
    w.track(SLOT_OUT)
    step = _step(w.run(), sid)
    assert {m.strike: m.quote_source for m in step.legs} == {100.0: "SNAPSHOT", 95.0: "TRACKED"}
    assert "MIXED_QUOTE_SOURCES" in step.flags and "MIXED_QUOTE_SOURCES" in INFO_FLAGS
    assert step.pnl_dollars == pytest.approx(110.0 - (2.0 - 0.45) * 100)


def test_a_leg_present_in_the_snapshot_is_never_replaced(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    sid = w.snapshot(SLOT_OUT, 101.0, {(100.0, "P"): (None, None, None, None)})
    w.track(SLOT_OUT)
    step = _step(w.run(), sid)
    leg100 = next(m for m in step.legs if m.strike == 100.0)
    assert (leg100.status, leg100.quote_source) == ("NO_PRICE", "SNAPSHOT")  # not MISSING_*
    assert step.status == "INCOMPLETE"


# ------------------------------------------------------------------ pairing by slot id
def test_pairing_uses_the_slot_not_timestamp_proximity(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    tid = w.track(SLOT_OUT)
    # A manual snapshot one second after the tracked file: no slot, so no pairing.
    manual = w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",), scheduled=False, delay_s=41)
    r = w.run()
    manual_step = _step(r, manual)
    assert manual_step.status == "INCOMPLETE"
    assert {m.quote_source for m in manual_step.legs} == {None}
    tracked_step = next(s for s in r.steps if s.snapshot_id == tid)
    assert tracked_step.kind == "TRACKED_ONLY" and tracked_step.status == "OK"


def test_retry_snapshot_of_a_slot_gets_no_tracked_fallback(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    first = w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",), attempt=1)
    retry = w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",), attempt=2)
    w.track(SLOT_OUT)
    r = w.run()
    assert _step(r, first).status == "OK"
    retry_step = _step(r, retry)
    assert retry_step.status == "INCOMPLETE"
    assert {m.status for m in retry_step.legs} == {"MISSING_OUTSIDE_DTE_WINDOW"}
    assert not set(retry_step.flags) & TRACKER_FLAGS


# ------------------------------------------------------------------ tracked-only steps
def test_tracked_only_step_takes_spot_from_the_run_log(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    tid = w.track(SLOT_OUT, spot=101.25, row_spot=99.0, md_type=3)
    step = next(s for s in w.run().steps if s.snapshot_id == tid)
    assert step.kind == "TRACKED_ONLY" and step.status == "OK"
    assert (step.spot, step.spot_source, step.market_data_type) == (101.25, "LAST", 3)
    assert "SPOT_SOURCE_UNKNOWN" not in step.flags
    assert "TRACKED_ONLY_STEP" in step.flags and "FILTER_CHANGED" not in step.flags
    assert step.quote_coverage == 1.0
    assert step.tracker_run_entries == ["tracker_runs.jsonl:L1"]


def test_tracked_only_coverage_and_unreliable_rules(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    slot = _et(DAY_OUT, 17, 30)  # after the close
    tid = w.track(
        slot,
        status="PARTIAL",
        quotes={(100.0, "P"): (1.0, 1.2), (95.0, "P"): (None, None)},
        md_type=4,
        spot_source="CLOSE",
    )
    step = next(s for s in w.run().steps if s.snapshot_id == tid)
    assert step.quote_coverage == 0.5  # 1 of the 2 tracked legs has a quote
    assert {"LOW_QUOTE_COVERAGE", "FROZEN_DATA", "OFF_SESSION", "SPOT_SOURCE_CLOSE"} <= set(
        step.flags
    )
    assert "TRACKER_PARTIAL" in step.flags and "LEG_NO_PRICE:95P" in step.flags
    assert step.status == "INCOMPLETE" and step.pnl_dollars is None


def test_tracked_files_of_other_contracts_do_not_add_steps(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.track(SLOT_OUT, quotes={(90.0, "P"): (0.2, 0.3)}, spread_id="NVDA-20261116-ffffffff")
    r = w.run()
    assert [s.kind for s in r.steps] == ["SNAPSHOT"]
    assert r.reproducibility.tracked_data_used is False


# ------------------------------------------------------------------ gaps and failures
def test_missing_tracked_file_is_incomplete_and_reported(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    sid = w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",))
    tid = w.track(SLOT_OUT)
    (tmp_path / "data" / "tracked" / "NVDA" / f"{tid}.csv").unlink()
    r = w.run()
    step = _step(r, sid)
    assert step.status == "INCOMPLETE" and "TRACKER_FILE_MISSING" in step.flags
    assert any("missing" in n for n in r.notes)


@pytest.mark.parametrize("status", ["MISSED", "FAILED", "SKIPPED"])
def test_tracker_gap_without_any_data_is_a_gap_step(tmp_path: Path, status: str) -> None:
    w = World(tmp_path)
    w.register()
    w.track(SLOT_OUT, status=status, write_file=False)
    r = w.run()
    gap = next(s for s in r.steps if s.kind == "GAP")
    assert gap.status == "INCOMPLETE" and f"TRACKER_{status}" in gap.flags
    assert (gap.spot, gap.market_data_type, gap.quote_coverage) == (None, None, None)
    assert {m.status for m in gap.legs} == {"MISSING_NO_DATA"}
    assert r.summary.steps_gap == 1 and r.summary.steps_total == 1  # gaps not counted
    dumped = json.loads(r.model_dump_json())  # serializes with empty values
    assert next(s for s in dumped["steps"] if s["kind"] == "GAP")["spot"] is None


def test_tracker_gap_with_a_normal_snapshot_flags_that_step(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    sid = w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",))
    w.track(SLOT_OUT, status="FAILED", write_file=False)
    step = _step(w.run(), sid)
    assert step.status == "INCOMPLETE" and "TRACKER_FAILED" in step.flags


def test_gaps_of_other_spreads_are_ignored(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    w.track(SLOT_OUT, status="MISSED", write_file=False, spread_id="NVDA-20261116-ffffffff")
    assert [s.kind for s in w.run().steps] == ["SNAPSHOT"]


def test_stopped_spread_steps_are_flagged(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register(status="STOPPED", changed=_et(date(2026, 11, 20), 12, 0))
    sid = w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",))
    step = _step(w.run(), sid)
    assert step.status == "INCOMPLETE" and "TRACKER_STOPPED" in step.flags


def test_half_written_run_log_lines_are_skipped_and_reported(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",))
    w.track(SLOT_OUT)
    meta = tmp_path / "data" / "metadata"
    with (meta / "tracker_runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "QUOTE", "planned_slot_utc": "2026-11-2')
    with (meta / "collector_runs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write('{"planned_slot_utc": ')
    r = w.run()
    assert r.reproducibility.skipped_run_log_lines == [
        "collector_runs.jsonl:L3",
        "tracker_runs.jsonl:L2",
    ]
    assert sum("half-written" in n for n in r.notes) == 2
    assert r.summary.steps_ok == 2


# ------------------------------------------------------------------ settlement & calendar
def test_settlement_spot_from_the_run_log_when_the_snapshot_spot_is_stale(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    closing = _et(date.fromisoformat(EXPIRY), 16, 15)
    w.snapshot(closing, 97.0, expiries=("2027-01-15",), spot_source="CLOSE")
    w.track(closing, spot=97.5, md_type=4)
    s = w.run().settlement
    assert s.status == "SETTLED" and s.spot_from == "TRACKER_RUN_LOG"
    assert (s.spot, s.spot_source, s.market_data_type) == (97.5, "LAST", 4)
    assert s.intrinsic_spread_per_share == pytest.approx(2.5)
    assert s.tracker_run_entry == "tracker_runs.jsonl:L1"


def test_settlement_from_a_usable_snapshot_records_its_source(tmp_path: Path) -> None:
    w = World(tmp_path)
    closing = _et(date.fromisoformat(EXPIRY), 16, 15)
    sid = w.snapshot(closing, 104.0, expiries=("2027-01-15",), md_type=4)
    s = w.run().settlement
    assert (s.status, s.snapshot_id, s.spot_from) == ("SETTLED", sid, "SNAPSHOT")
    assert (s.spot_source, s.market_data_type) == ("LAST", 4)


EARLY = date(2026, 11, 27)  # early close 13:00 ET
EC_LEGS = [
    StrategyLegInput(side="SELL", right="P", expiry="2026-11-27", strike=100.0),
    StrategyLegInput(side="BUY", right="P", expiry="2026-11-27", strike=95.0),
]


def _ec_chain(ts: datetime, spot: float) -> pl.DataFrame:
    return _chain(ts, spot, ENTRY_QUOTES, expiries=("2026-11-27",))


def test_early_close_session_and_settlement(tmp_path: Path) -> None:
    """The one deliberate difference from engine version 1 (which used 16:00 / 16:15 always)."""
    repo = CSVRepository(StorageSettings(), root=tmp_path)
    times = {
        "entry": _et(date(2026, 11, 20), 11, 45),
        "holiday": _et(date(2026, 11, 26), 11, 0),  # Thanksgiving: no session
        "noon": _et(EARLY, 12, 30),  # inside the short session
        "close_settle": _et(EARLY, 13, 15),  # close + 15 min
    }
    ids = {}
    for name, ts in times.items():
        meta = repo.save_snapshot(
            "NVDA",
            _ec_chain(ts, 104.0).to_dicts(),
            range_pct=0.1,
            strike_step=5.0,
            spot_source="LAST",
        )
        ids[name] = meta.snapshot_id
    r = backtest_bull_put_from_repo(repo, "NVDA", ids["entry"], mode="LEGS", legs=EC_LEGS)
    by_id = {s.snapshot_id: s for s in r.steps}
    assert "OFF_SESSION" in by_id[ids["holiday"]].flags
    assert "OFF_SESSION" not in by_id[ids["noon"]].flags
    assert ids["close_settle"] not in by_id  # after the 13:00 close: not an open step
    assert r.settlement.status == "SETTLED" and r.settlement.snapshot_id == ids["close_settle"]
    assert r.settlement.ts_et == "2026-11-27 13:15 ET"
    assert session_for(EARLY) is not None and session_for(EARLY).early_close  # type: ignore[union-attr]


def test_step_after_the_early_close_is_off_session(tmp_path: Path) -> None:
    repo = CSVRepository(StorageSettings(), root=tmp_path)
    entry = repo.save_snapshot(
        "NVDA",
        _chain(_et(date(2026, 11, 20), 11, 45), 102.0, ENTRY_QUOTES).to_dicts(),
        spot_source="LAST",
    ).snapshot_id
    late = repo.save_snapshot(
        "NVDA", _chain(_et(EARLY, 13, 30), 102.0).to_dicts(), spot_source="LAST"
    ).snapshot_id
    r = backtest_bull_put_from_repo(repo, "NVDA", entry, mode="LEGS", legs=LEGS)
    assert "OFF_SESSION" in next(s for s in r.steps if s.snapshot_id == late).flags


def test_steps_beyond_the_calendar_table_are_flagged(tmp_path: Path) -> None:
    repo = CSVRepository(StorageSettings(), root=tmp_path)
    legs = [
        StrategyLegInput(side="SELL", right="P", expiry="2028-02-18", strike=100.0),
        StrategyLegInput(side="BUY", right="P", expiry="2028-02-18", strike=95.0),
    ]
    ids = [
        repo.save_snapshot(
            "NVDA",
            _chain(ts, 102.0, ENTRY_QUOTES, expiries=("2028-02-18",)).to_dicts(),
            spot_source="LAST",
        ).snapshot_id
        for ts in (_et(date(2028, 1, 4), 11, 45), _et(date(2028, 1, 5), 11, 45))
    ]
    r = backtest_bull_put_from_repo(repo, "NVDA", ids[0], mode="LEGS", legs=legs)
    assert all("CALENDAR_UNKNOWN" in s.flags for s in r.steps)
    assert r.steps[1].status == "OK"  # informational only


# ------------------------------------------------------------------ registry helper
def test_backtest_by_registry_id_equals_mode_a(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",))
    w.track(SLOT_OUT)
    by_id = backtest_tracked_spread(w.repo, SPREAD_ID)
    direct = w.run()
    assert by_id.model_dump() == direct.model_dump()
    with pytest.raises(SnapshotNotFoundError):
        backtest_tracked_spread(w.repo, "NVDA-20261116-00000000")


def test_registry_qty_is_used(tmp_path: Path) -> None:
    w = World(tmp_path)
    spreads = w.tracker.load_spreads()
    w.register()
    spread = w.tracker.load_spreads()[0].model_copy(update={"qty": 3})
    w.tracker.save_spreads([*spreads, spread])
    r = backtest_tracked_spread(w.repo, SPREAD_ID)
    assert r.entry.entry_cash_mid_dollars == pytest.approx(330.0)


# ------------------------------------------------------------------ determinism & regression
def test_same_inputs_same_output(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",))
    w.track(SLOT_OUT)
    w.track(_et(DAY_OUT, 13, 45), status="MISSED", write_file=False)
    assert w.run().model_dump() == w.run().model_dump()


# Fields added in version 2 (empty without tracker data) and the calendar texts.
_V2_ONLY = {
    "kind",
    "slot_utc",
    "tracked_snapshot_id",
    "tracker_run_entries",
    "quote_source",
    "steps_gap",
    "spot_from",
    "tracker_run_entry",
    "tracked_data_used",
    "tracked_files",
    "skipped_run_log_lines",
    "calendar_source_url",
}


def _strip(value: Any, parent: str = "") -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            # `strategies_sha256` hashes the Strategy Lab source, which the services refactor
            # split from `strategies.py` into the `strategies/` package (logic unchanged; the
            # golden numbers below still match).
            if k in _V2_ONLY or k in ("engine_version", "strategies_sha256"):
                continue
            if parent == "settlement" and k in ("spot_source", "market_data_type"):
                continue
            if parent == "quality_thresholds" and k in ("session_et", "settlement_min_time_et"):
                continue
            out[k] = _strip(v, k)
        return out
    if isinstance(value, list):
        return [_strip(v, parent) for v in value]
    return value


def test_identical_to_engine_v1_without_tracked_data() -> None:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    current = json.loads(json.dumps(scenarios()))
    assert set(current) == set(golden)
    for name in golden:
        assert golden[name]["reproducibility"]["engine_version"] == "1"
        assert current[name]["reproducibility"]["engine_version"] == "2"
        assert _strip(current[name]) == _strip(golden[name]), name
        assert current[name]["reproducibility"]["tracked_data_used"] is False
        assert all(s["kind"] == "SNAPSHOT" for s in current[name]["steps"])


# ------------------------------------------------------------------ read-only
def _tree_hash(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_engine_never_writes_snapshots_tracked_or_metadata(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.register()
    w.snapshot(SLOT_OUT, 101.0, expiries=("2027-01-15",))
    w.track(SLOT_OUT)
    w.track(_et(DAY_OUT, 13, 45), status="MISSED", write_file=False)
    data = tmp_path / "data"
    before = _tree_hash(data)
    dirs_before = sorted(p for p in data.rglob("*") if p.is_dir())
    assert any(k.startswith("tracked/") for k in before)
    assert any(k.startswith("metadata/") for k in before)
    w.run()
    backtest_tracked_spread(w.repo, SPREAD_ID)
    assert _tree_hash(data) == before
    assert sorted(p for p in data.rglob("*") if p.is_dir()) == dirs_before


def test_no_tracked_folder_creates_nothing(tmp_path: Path) -> None:
    w = World(tmp_path)
    w.run()
    assert not (tmp_path / "data" / "tracked").exists()


# ------------------------------------------------------------------ dry-run replay
def test_dry_run_replay_from_32_dte_to_expiry_has_no_incomplete_steps(tmp_path: Path) -> None:
    """One slot per trading day (11:45 ET) plus the expiry-day closing slot: the normal
    snapshots stop listing the legs below 30 DTE; the tracked rows take over without a gap."""
    w = World(tmp_path)
    w.register()
    expiry = date.fromisoformat(EXPIRY)
    day = date(2026, 11, 17)
    tracked_slots = 0
    while day <= expiry:
        session = session_for(day)
        if session is not None:
            slot = min(_et(day, 11, 45), session.close_utc - timedelta(minutes=15))
            dte = (expiry - day).days
            expiries = (EXPIRY, "2027-01-15") if dte >= 30 else ("2027-01-15",)
            w.snapshot(slot, 101.0, expiries=expiries)
            w.track(slot)
            tracked_slots += 1
        day += timedelta(days=1)
    closing = _et(expiry, 16, 15)
    w.snapshot(closing, 103.0, expiries=("2027-01-15",))
    w.track(closing, spot=103.0)

    r = backtest_tracked_spread(w.repo, SPREAD_ID)
    assert r.summary.steps_incomplete == 0
    assert all(s.status != "INCOMPLETE" for s in r.steps)
    dtes = [s.short_dte for s in r.steps]
    assert max(dtes) == 32 and min(dtes) == 0
    sources = [{m.quote_source for m in s.legs} for s in r.steps[1:]]
    assert {"SNAPSHOT"} in sources and {"TRACKED"} in sources  # crosses the 30 DTE boundary
    assert r.settlement.status == "SETTLED" and r.settlement.spot == 103.0
    assert r.settlement.spot_from == "SNAPSHOT"
    assert len(r.reproducibility.tracked_files) == sum(1 for s in r.steps if s.tracked_snapshot_id)
    assert tracked_slots == len(r.steps) - 1
