"""Scheduled collection: session calendar, slots, cycle policies (fake clock, no Gateway)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from src.collector import service as collector_service
from src.collector.calendar import (
    NYSE_EARLY_CLOSES,
    NYSE_HOLIDAYS,
    US_EASTERN,
    Slot,
    calendar_covers,
    fmt_poland,
    next_slot,
    session_for,
    slots_between,
    slots_for_day,
)
from src.collector.service import CollectorService
from src.collector.store import CollectorSettings, CollectorStore, RunRecord, TickerPin
from src.core.exceptions import GatewayConnectionError


def _et(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=US_EASTERN).astimezone(UTC)


def _et_times(slots: list[Slot]) -> list[str]:
    return [f"{s.kind[0]}{s.at_utc.astimezone(US_EASTERN):%H:%M}" for s in slots]


# ------------------------------------------------------------------ calendar & slots
def test_regular_day_hourly_slots_and_closing_snapshot() -> None:
    slots = slots_for_day(date(2026, 11, 16), 60)  # Monday
    assert _et_times(slots) == [
        "I09:45", "I10:45", "I11:45", "I12:45", "I13:45", "I14:45", "I15:45", "C16:15",
    ]  # fmt: skip


def test_other_intervals_and_closing_only() -> None:
    day = date(2026, 11, 16)
    assert len(slots_for_day(day, 15)) == 25 + 1  # 09:45 .. 15:45 every 15 min + closing
    assert _et_times(slots_for_day(day, 120)) == ["I09:45", "I11:45", "I13:45", "I15:45", "C16:15"]
    assert _et_times(slots_for_day(day, 0)) == ["C16:15"]
    with pytest.raises(ValueError, match="interval_min"):
        slots_for_day(day, 45)


def test_weekend_and_holidays_have_no_slots() -> None:
    assert slots_for_day(date(2026, 11, 14), 60) == []  # Saturday
    assert slots_for_day(date(2026, 11, 15), 60) == []  # Sunday
    assert slots_for_day(date(2026, 11, 26), 60) == []  # Thanksgiving
    assert slots_for_day(date(2027, 12, 24), 60) == []  # Christmas observed
    assert session_for(date(2026, 11, 26)) is None


def test_early_close_day_closing_at_13_15() -> None:
    slots = slots_for_day(date(2026, 11, 27), 60)
    assert _et_times(slots) == ["I09:45", "I10:45", "I11:45", "I12:45", "C13:15"]
    session = session_for(date(2026, 11, 27))
    assert session is not None and session.early_close


def test_calendar_table_matches_the_nyse_page_counts() -> None:
    assert len([d for d in NYSE_HOLIDAYS if d.year == 2026]) == 10
    assert len([d for d in NYSE_HOLIDAYS if d.year == 2027]) == 10
    assert {date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)} == NYSE_EARLY_CLOSES
    assert all(d.weekday() < 5 for d in NYSE_HOLIDAYS | NYSE_EARLY_CLOSES)
    assert calendar_covers(date(2027, 12, 31)) and not calendar_covers(date(2028, 1, 3))


@pytest.mark.parametrize(
    ("day", "closing_utc_hour", "closing_poland"),
    [
        (date(2026, 10, 30), 20, "21:15 CET"),  # US daylight time, Poland already standard
        (date(2026, 11, 2), 21, "22:15 CET"),  # both standard time
        (date(2027, 3, 12), 21, "22:15 CET"),  # before the US switch
        (date(2027, 3, 15), 20, "21:15 CET"),  # US switched 14.03, Poland only on 28.03
        (date(2027, 3, 29), 20, "22:15 CEST"),  # both daylight time
    ],
)
def test_daylight_saving_transitions(day: date, closing_utc_hour: int, closing_poland: str) -> None:
    closing = slots_for_day(day, 60)[-1]
    assert closing.kind == "CLOSING"
    assert closing.at_utc.astimezone(US_EASTERN).strftime("%H:%M") == "16:15"
    assert closing.at_utc.hour == closing_utc_hour
    assert fmt_poland(closing.at_utc).endswith(closing_poland)


def test_next_slot_after_restart_mid_session_and_over_weekend() -> None:
    day = date(2026, 11, 16)
    nxt = next_slot(_et(day, 12, 10), 60)
    assert nxt is not None and nxt.at_utc == _et(day, 12, 45)
    after_close = next_slot(_et(day, 16, 20), 60)
    assert after_close is not None and after_close.at_utc == _et(date(2026, 11, 17), 9, 45)
    friday_evening = next_slot(_et(date(2026, 11, 20), 17, 0), 60)
    assert friday_evening is not None and friday_evening.at_utc == _et(date(2026, 11, 23), 9, 45)
    before_holiday = next_slot(_et(date(2026, 11, 25), 17, 0), 60)  # Thanksgiving is closed
    assert before_holiday is not None and before_holiday.at_utc == _et(date(2026, 11, 27), 9, 45)


def test_slots_between_is_exclusive_start_inclusive_end() -> None:
    day = date(2026, 11, 16)
    got = slots_between(_et(day, 9, 45), _et(day, 11, 45), 60)
    assert _et_times(got) == ["I10:45", "I11:45"]


# ------------------------------------------------------------------ settings model
def test_settings_defaults_are_off_with_pinned_nvda_avgo() -> None:
    s = CollectorSettings()
    assert s.enabled is False and s.interval_min == 60
    assert [(p.symbol, p.range_pct, p.strike_step, p.dte_min, p.dte_max) for p in s.tickers] == [
        ("NVDA", 0.35, 5.0, 30, 120),
        ("AVGO", 0.35, 10.0, 30, 120),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {"interval_min": 45},
        {
            "tickers": [
                {
                    "symbol": "../x",
                    "range_pct": 0.2,
                    "strike_step": 5,
                    "dte_min": 30,
                    "dte_max": 120,
                }
            ]
        },
        {
            "tickers": [
                {"symbol": "NVDA", "range_pct": 0.2, "strike_step": 5, "dte_min": 90, "dte_max": 30}
            ]
        },
        {
            "tickers": [
                {
                    "symbol": "NVDA",
                    "range_pct": 0.2,
                    "strike_step": 5,
                    "dte_min": 30,
                    "dte_max": 121,
                }
            ]
        },
        {
            "tickers": [
                {
                    "symbol": "NVDA",
                    "range_pct": 0.2,
                    "strike_step": 5,
                    "dte_min": 30,
                    "dte_max": 120,
                },
                {
                    "symbol": "nvda",
                    "range_pct": 0.2,
                    "strike_step": 5,
                    "dte_min": 30,
                    "dte_max": 120,
                },
            ]
        },
    ],
)
def test_settings_validation(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CollectorSettings.model_validate(bad)


# ------------------------------------------------------------------ cycle with a fake web service
class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start
        self.waits: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.now += timedelta(seconds=seconds)
        return False


def _meta(ticker: str, *, coverage: float = 1.0, md_type: int = 3, step: float = 5.0) -> Any:
    return SimpleNamespace(
        meta=SimpleNamespace(
            snapshot_id=f"{ticker}_20261116_150000",
            snapshot_ts_utc="2026-11-16T15:00:00Z",
            market_date="2026-11-16",
            market_data_type=md_type,
            underlying_price=150.0,
            contract_count=100,
            expirations=["2026-12-18"],
            spot_source="LAST",
            quote_coverage=coverage,
            strike_step=step,
            range_pct=0.35,
        )
    )


class FakeWeb:
    """Stands in for AnalyzerWebService: scripted refresh outcomes per ticker."""

    def __init__(self, outcomes: dict[str, list[Any]], clock: FakeClock) -> None:
        self.outcomes = {k: list(v) for k, v in outcomes.items()}
        self.clock = clock
        self.calls: list[tuple[str, float, float | None, int | None, int | None, str]] = []
        self.refresh_lock = asyncio.Lock()
        self.refresh_owner: str | None = None
        self.corridor = SimpleNamespace(active_stream_count=0)
        self.corridor_counts: list[int] = []
        self.last_corridor_stop: dict[str, Any] | None = None
        self.lock_free = True

    def validate_ticker(self, raw: str) -> str:
        return raw.upper()

    async def acquire_refresh(self, owner: str, timeout_sec: float) -> bool:
        if not self.lock_free:
            return False
        self.refresh_owner = owner
        return True

    def release_refresh(self) -> None:
        self.refresh_owner = None

    async def refresh_chain_locked(self, req: Any, *, collection_mode: str) -> Any:
        assert self.refresh_owner == "collector"
        self.calls.append(
            (req.ticker, req.range_pct, req.strike_step, req.dte_min, req.dte_max, collection_mode)
        )
        outcome = self.outcomes[req.ticker].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _collector(tmp_path: Path, web: FakeWeb, clock: FakeClock, **settings: Any) -> CollectorService:
    store = CollectorStore(tmp_path / "metadata")
    svc = CollectorService(web, store, clock=clock, wait=clock.wait)  # type: ignore[arg-type]
    svc.settings = CollectorSettings(enabled=True, **settings)
    return svc


DAY = date(2026, 11, 16)


def test_cycle_refreshes_tickers_in_order_with_pinned_params(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    web = FakeWeb({"NVDA": [_meta("NVDA")], "AVGO": [_meta("AVGO", step=10.0)]}, clock)
    svc = _collector(tmp_path, web, clock)
    records = asyncio.run(svc.run_slot(Slot("INTERVAL", _et(DAY, 9, 45))))
    assert web.calls == [
        ("NVDA", 0.35, 5.0, 30, 120, "SCHEDULED"),
        ("AVGO", 0.35, 10.0, 30, 120, "SCHEDULED"),
    ]
    assert [(r.ticker, r.status, r.pin_drift) for r in records] == [
        ("NVDA", "OK", False),
        ("AVGO", "OK", False),
    ]
    assert svc.store.read_runs() == records
    assert web.refresh_owner is None and svc.busy is False


def test_pin_drift_is_flagged_when_the_step_was_widened(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    web = FakeWeb({"NVDA": [_meta("NVDA", step=10.0)]}, clock)
    svc = _collector(tmp_path, web, clock, tickers=[CollectorSettings().tickers[0]])
    rec = asyncio.run(svc.run_slot(Slot("INTERVAL", _et(DAY, 9, 45))))[0]
    assert rec.pin_drift is True
    assert (rec.requested_strike_step, rec.effective_strike_step) == (5.0, 10.0)


def test_gateway_down_is_retried_once_after_five_minutes(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    web = FakeWeb({"NVDA": [GatewayConnectionError("port 4001 closed"), _meta("NVDA")]}, clock)
    svc = _collector(tmp_path, web, clock, tickers=[CollectorSettings().tickers[0]])
    records = asyncio.run(svc.run_slot(Slot("INTERVAL", _et(DAY, 9, 45))))
    assert [(r.attempt, r.status) for r in records] == [(1, "FAILED"), (2, "OK")]
    assert records[0].error is not None and records[0].error.startswith("GATEWAY_OFFLINE")
    assert sum(clock.waits) == pytest.approx(300.0)


def test_gateway_down_twice_gives_up_until_the_next_slot(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    down = GatewayConnectionError("down")
    web = FakeWeb({"NVDA": [down, down]}, clock)
    svc = _collector(tmp_path, web, clock, tickers=[CollectorSettings().tickers[0]])
    records = asyncio.run(svc.run_slot(Slot("INTERVAL", _et(DAY, 9, 45))))
    assert [(r.attempt, r.status) for r in records] == [(1, "FAILED"), (2, "FAILED")]
    assert len(web.calls) == 2  # no tight retry loop


def test_partial_failure_does_not_stop_the_other_tickers(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    web = FakeWeb({"NVDA": [RuntimeError("boom")], "AVGO": [_meta("AVGO", step=10.0)]}, clock)
    svc = _collector(tmp_path, web, clock)
    records = asyncio.run(svc.run_slot(Slot("INTERVAL", _et(DAY, 9, 45))))
    assert [(r.ticker, r.status) for r in records] == [("NVDA", "FAILED"), ("AVGO", "OK")]
    assert records[0].error is not None and records[0].error.startswith("UNEXPECTED")


def test_low_coverage_is_kept_flagged_and_retried_once(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    web = FakeWeb({"NVDA": [_meta("NVDA", coverage=0.5), _meta("NVDA", coverage=0.97)]}, clock)
    svc = _collector(tmp_path, web, clock, tickers=[CollectorSettings().tickers[0]])
    records = asyncio.run(svc.run_slot(Slot("INTERVAL", _et(DAY, 9, 45))))
    assert [(r.attempt, r.status, r.usable) for r in records] == [
        (1, "LOW_QUALITY", False),
        (2, "OK", True),
    ]
    assert records[0].snapshot_id is not None  # kept, not dropped
    assert sum(clock.waits) == pytest.approx(120.0)


def test_frozen_data_is_flagged_but_not_retried(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 16, 15))
    web = FakeWeb({"NVDA": [_meta("NVDA", coverage=0.5, md_type=4)]}, clock)
    svc = _collector(tmp_path, web, clock, tickers=[CollectorSettings().tickers[0]])
    records = asyncio.run(svc.run_slot(Slot("CLOSING", _et(DAY, 16, 15))))
    assert [(r.status, r.slot_kind) for r in records] == [("LOW_QUALITY", "CLOSING")]
    assert any("market_data_type 4" in reason for reason in records[0].quality_reasons)


def test_busy_manual_refresh_skips_the_slot(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    web = FakeWeb({"NVDA": [_meta("NVDA")]}, clock)
    web.lock_free = False
    svc = _collector(tmp_path, web, clock, tickers=[CollectorSettings().tickers[0]])
    records = asyncio.run(svc.run_slot(Slot("INTERVAL", _et(DAY, 9, 45))))
    assert [r.status for r in records] == ["SKIPPED"]
    assert web.calls == []


def test_corridor_stream_postpones_the_cycle(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    web = FakeWeb({"NVDA": [_meta("NVDA")]}, clock)
    web.corridor.active_stream_count = 1

    async def _wait(seconds: float) -> bool:
        await FakeClock.wait(clock, seconds)
        if len(clock.waits) >= 2:  # the stream ends after two polls
            web.corridor.active_stream_count = 0
        return False

    store = CollectorStore(tmp_path / "metadata")
    svc = CollectorService(web, store, clock=clock, wait=_wait)  # type: ignore[arg-type]
    svc.settings = CollectorSettings(enabled=True, tickers=[CollectorSettings().tickers[0]])
    rec = asyncio.run(svc.run_slot(Slot("INTERVAL", _et(DAY, 9, 45))))[0]
    assert rec.planned_slot_utc == "2026-11-16T14:45:00Z"
    assert rec.actual_run_utc == "2026-11-16T14:46:00Z"
    assert rec.note == "Postponed 60s while a corridor stream was active."


def test_corridor_postpone_is_capped_at_ten_minutes(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 9, 45))
    web = FakeWeb({"NVDA": [_meta("NVDA")]}, clock)
    web.corridor.active_stream_count = 1  # never ends; the refresh stops it
    svc = _collector(tmp_path, web, clock, tickers=[CollectorSettings().tickers[0]])
    rec = asyncio.run(svc.run_slot(Slot("INTERVAL", _et(DAY, 9, 45))))[0]
    assert sum(clock.waits) == pytest.approx(600.0)
    assert rec.actual_run_utc == "2026-11-16T14:55:00Z"


# ------------------------------------------------------------------ missed slots & settings
def test_missed_slots_are_recorded_on_startup(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 11, 0))
    store = CollectorStore(tmp_path / "metadata")
    store.append_runs(
        [
            RunRecord(
                planned_slot_utc="2026-11-13T20:45:00Z",  # Fri 15:45 ET: last slot that ran
                slot_kind="INTERVAL",
                actual_run_utc="2026-11-13T20:45:00Z",
                ticker="NVDA",
                status="OK",
            )
        ]
    )
    svc = CollectorService(FakeWeb({}, clock), store, clock=clock, wait=clock.wait)  # type: ignore[arg-type]
    svc.settings = CollectorSettings(enabled=True, tickers=[CollectorSettings().tickers[0]])
    missed = svc.record_missed_slots()
    # Fri 16:15 closing, then (weekend skipped) Mon 09:45 and 10:45
    assert [(r.planned_slot_utc, r.slot_kind, r.status) for r in missed] == [
        ("2026-11-13T21:15:00Z", "CLOSING", "MISSED"),
        ("2026-11-16T14:45:00Z", "INTERVAL", "MISSED"),
        ("2026-11-16T15:45:00Z", "INTERVAL", "MISSED"),
    ]
    assert svc.store.read_runs()[-3:] == missed
    assert svc.record_missed_slots() == []  # idempotent


def test_no_missed_slots_when_collection_was_off(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 11, 0))
    svc = CollectorService(FakeWeb({}, clock), CollectorStore(tmp_path), clock=clock)  # type: ignore[arg-type]
    assert svc.settings.enabled is False
    assert svc.record_missed_slots() == []


def test_update_settings_persists_and_dates_pin_changes(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 8, 0))
    store = CollectorStore(tmp_path / "metadata")
    svc = CollectorService(FakeWeb({}, clock), store, clock=clock, wait=clock.wait)  # type: ignore[arg-type]
    first = svc.update_settings(CollectorSettings(enabled=True))
    assert first.enabled_since_utc == "2026-11-16T13:00:00Z"
    assert all(p.pinned_since_utc == "2026-11-16T13:00:00Z" for p in first.tickers)

    clock.now += timedelta(hours=1)
    nvda, avgo = first.tickers
    changed = CollectorSettings(
        enabled=True,
        tickers=[nvda, avgo.model_copy(update={"strike_step": 5.0, "pinned_since_utc": None})],
    )
    second = svc.update_settings(changed)
    assert second.tickers[0].pinned_since_utc == "2026-11-16T13:00:00Z"  # unchanged
    assert second.tickers[1].pinned_since_utc == "2026-11-16T14:00:00Z"  # changed -> new date
    assert second.enabled_since_utc == "2026-11-16T13:00:00Z"

    reloaded, from_file = CollectorStore(tmp_path / "metadata").load_settings()
    assert from_file and reloaded == second
    assert not list((tmp_path / "metadata").glob(".*.tmp"))


def test_status_reports_times_in_et_and_poland(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 10, 0))
    svc = _collector(tmp_path, FakeWeb({}, clock), clock)
    st = svc.status()
    assert st.next_run is not None and st.next_run.et == "2026-11-16 10:45 ET"
    assert st.next_run.poland == "2026-11-16 16:45 CET"
    assert st.next_closing is not None and st.next_closing.et == "2026-11-16 16:15 ET"
    assert st.next_closing.poland == "2026-11-16 22:15 CET"
    assert st.today_session_et == "09:30-16:00 ET"
    assert st.calendar_warning is None


def test_status_warns_after_the_calendar_ends(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2028, 1, 4, 15, 0, tzinfo=UTC))
    svc = _collector(tmp_path, FakeWeb({}, clock), clock)
    assert svc.status().calendar_warning is not None


def test_start_and_stop_leave_no_task(tmp_path: Path) -> None:
    async def _go() -> None:
        clock = FakeClock(_et(DAY, 10, 0))
        svc = CollectorService(FakeWeb({}, clock), CollectorStore(tmp_path))  # type: ignore[arg-type]
        await svc.start()
        assert svc._task is not None and not svc._task.done()
        await svc.stop()
        assert svc._task is None

    asyncio.run(_go())  # asyncio.run fails if a task were still pending at the end


def test_loop_runs_the_next_slot_then_waits(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 10, 40))
    web = FakeWeb({"NVDA": [_meta("NVDA")]}, clock)
    store = CollectorStore(tmp_path / "metadata")
    ran: list[Slot] = []

    async def _go() -> None:
        svc = CollectorService(web, store, clock=clock, wait=clock.wait)  # type: ignore[arg-type]
        svc.settings = CollectorSettings(enabled=True, tickers=[CollectorSettings().tickers[0]])
        original = svc.run_slot

        async def _run_slot(slot: Slot) -> list[RunRecord]:
            ran.append(slot)
            out = await original(slot)
            raise asyncio.CancelledError  # stop the loop after one cycle
            return out

        svc.run_slot = _run_slot  # type: ignore[method-assign]
        with pytest.raises(asyncio.CancelledError):
            await svc._loop()

    asyncio.run(_go())
    assert [s.at_utc for s in ran] == [_et(DAY, 10, 45)]
    assert clock.now == _et(DAY, 10, 45)
    assert store.read_runs()[0].status == "OK"


def test_full_session_dry_run(tmp_path: Path) -> None:
    """A whole trading day with a mocked clock: 7 hourly snapshots plus the closing one."""
    clock = FakeClock(_et(DAY, 9, 0))
    per_day = [_meta("NVDA") for _ in range(8)]
    web = FakeWeb({"NVDA": per_day, "AVGO": [_meta("AVGO", step=10.0) for _ in range(8)]}, clock)
    svc = _collector(tmp_path, web, clock)

    async def _day() -> list[RunRecord]:
        out: list[RunRecord] = []
        for slot in slots_for_day(DAY, svc.settings.interval_min):
            clock.now = slot.at_utc
            out.extend(await svc.run_slot(slot))
        return out

    records = asyncio.run(_day())
    assert len(records) == 16 and all(r.status == "OK" for r in records)
    assert [r.slot_kind for r in records].count("CLOSING") == 2
    st = svc.status()
    assert st.snapshots_today == 16 and st.gaps_today == 0


def test_status_counts_gaps(tmp_path: Path) -> None:
    clock = FakeClock(_et(DAY, 10, 0))
    svc = _collector(tmp_path, FakeWeb({}, clock), clock)
    svc.store.append_runs(
        [
            RunRecord(
                planned_slot_utc="2026-11-16T14:45:00Z",
                slot_kind="INTERVAL",
                actual_run_utc=None,
                ticker="NVDA",
                status="MISSED",
            )
        ]
    )
    st = svc.status()
    assert (st.gaps_today, st.missed_recent, st.snapshots_today) == (1, 1, 0)
    assert st.last_results["NVDA"].status == "MISSED"


def test_pin_model_rejects_auto_step() -> None:
    with pytest.raises(ValidationError):
        TickerPin.model_validate(
            {
                "symbol": "NVDA",
                "range_pct": 0.35,
                "strike_step": None,
                "dte_min": 30,
                "dte_max": 120,
            }
        )


def test_constants_document_the_policy() -> None:
    assert collector_service.FAILURE_RETRY_DELAY_SEC == 300.0
    assert collector_service.LOW_COVERAGE_RETRY_DELAY_SEC == 120.0
    assert collector_service.CORRIDOR_POSTPONE_MAX_SEC == 600.0
