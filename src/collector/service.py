"""Scheduled snapshot collection (background task in the FastAPI lifespan).

One cycle per slot (see `calendar.slots_for_day`): the selected tickers are refreshed one after
another through `AnalyzerWebService.refresh_chain_locked`, i.e. the same code, filters and
paging as a manual refresh, with pinned parameters and `collection_mode="SCHEDULED"`.

Policies (documented in docs/ARCHITECTURE.md §12):
- One refresh at a time: the cycle holds `refresh_lock`; it waits up to
  `MANUAL_REFRESH_WAIT_SEC` for a running manual refresh, then records the slot as SKIPPED.
- Corridor stream active: postpone up to `CORRIDOR_POSTPONE_MAX_SEC`, then the refresh stops it.
- Gateway down / timeout / fetch error: retry that ticker once after `FAILURE_RETRY_DELAY_SEC`.
- Low quote coverage: keep and flag the snapshot, retry once after `LOW_COVERAGE_RETRY_DELAY_SEC`
  (both snapshots are kept). Frozen / off-session data is never retried. Nothing is dropped.
- Slots missed while the app was not running are recorded as MISSED on startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from pydantic import BaseModel, ConfigDict

from src.collector.calendar import (
    CALENDAR_LAST_DAY,
    CALENDAR_SOURCE_URL,
    EARLY_CLOSE_ET,
    SESSION_CLOSE_ET,
    SESSION_OPEN_ET,
    Slot,
    calendar_covers,
    et_date,
    fmt_et,
    fmt_poland,
    next_closing_slot,
    next_slot,
    session_for,
    slots_between,
)
from src.collector.store import (
    CollectorSettings,
    CollectorStore,
    RunRecord,
    TickerPin,
    utc_now_iso,
)
from src.core.exceptions import AnalyzerError, GatewayConnectionError, InvalidTickerError
from src.storage.quality import MIN_QUOTE_COVERAGE, evaluate_snapshot
from src.storage.schema import SnapshotMeta

if TYPE_CHECKING:
    from src.web.api import AnalyzerWebService

logger = logging.getLogger(__name__)

COLLECTOR_OWNER: Final = "collector"
MANUAL_REFRESH_WAIT_SEC: Final = 120.0
CORRIDOR_POSTPONE_MAX_SEC: Final = 600.0
CORRIDOR_POLL_SEC: Final = 30.0
FAILURE_RETRY_DELAY_SEC: Final = 300.0
LOW_COVERAGE_RETRY_DELAY_SEC: Final = 120.0
TICKER_REFRESH_TIMEOUT_SEC: Final = 240.0
MISSED_LOOKBACK_DAYS: Final = 7
MAX_IDLE_WAIT_SEC: Final = 3600.0
RECENT_RUNS_IN_STATUS: Final = 20

ClockFn = Callable[[], datetime]
WaitFn = Callable[[float], Awaitable[bool]]  # wait up to N seconds; True = woken early


class TimeView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    utc: str
    et: str
    poland: str
    kind: str | None = None


def _time_view(ts: datetime, kind: str | None = None) -> TimeView:
    return TimeView(utc=utc_now_iso(ts), et=fmt_et(ts), poland=fmt_poland(ts), kind=kind)


class CollectorStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    restored_on_startup: bool
    interval_min: int
    tickers: list[TickerPin]
    busy: bool
    current_ticker: str | None
    now: TimeView
    next_run: TimeView | None
    next_closing: TimeView | None
    today_trading_day: bool
    today_session_et: str | None
    today_early_close: bool
    calendar_source_url: str
    calendar_valid_until: str
    calendar_warning: str | None
    last_cycle_planned: TimeView | None
    last_cycle_actual: TimeView | None
    last_results: dict[str, RunRecord]
    last_error: RunRecord | None
    snapshots_today: int
    gaps_today: int
    missed_recent: int
    recent_runs: list[RunRecord]
    last_corridor_stop: dict[str, Any] | None


def _parse_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


class CollectorService:
    def __init__(
        self,
        web: AnalyzerWebService,
        store: CollectorStore,
        *,
        clock: ClockFn | None = None,
        wait: WaitFn | None = None,
    ) -> None:
        self.web = web
        self.store = store
        self.clock: ClockFn = clock or (lambda: datetime.now(UTC))
        self._wake = asyncio.Event()
        self._wait: WaitFn = wait or self._wait_for_wake
        self.settings, self.restored_on_startup = store.load_settings()
        self._task: asyncio.Task[None] | None = None
        self.busy = False
        self.current_ticker: str | None = None
        self.last_cycle_planned: datetime | None = None
        self.last_cycle_actual: datetime | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:  # e.g. a test driving the lifespan without an event loop
            logger.warning("no running event loop; scheduled collection loop not started")
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="snapshot-collector")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _now(self) -> datetime:
        now = self.clock()
        return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)

    async def _wait_for_wake(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=max(0.0, seconds))
        except TimeoutError:
            return False
        self._wake.clear()
        return True

    async def _sleep_until(self, target: datetime) -> bool:
        """Wait until `target`; True if woken early (settings changed)."""
        while True:
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return False
            if await self._wait(min(remaining, MAX_IDLE_WAIT_SEC)):
                return True

    async def _loop(self) -> None:
        while True:
            try:
                if not self.settings.enabled or not self.settings.tickers:
                    await self._wait(MAX_IDLE_WAIT_SEC)
                    continue
                slot = next_slot(self._now(), self.settings.interval_min)
                if slot is None:
                    await self._wait(MAX_IDLE_WAIT_SEC)
                    continue
                if await self._sleep_until(slot.at_utc):
                    continue  # settings changed: recompute
                if self.settings.enabled:
                    await self.run_slot(slot)
            except asyncio.CancelledError:
                raise
            except Exception:  # never let the background task die
                logger.exception("collector loop error; continuing")
                await self._wait(60.0)

    # ------------------------------------------------------------------ settings
    def update_settings(self, new: CollectorSettings) -> CollectorSettings:
        """Store new settings. Unchanged pins keep `pinned_since_utc`; new or changed ones
        get the current time, so every parameter change is visible and dated."""
        now_iso = utc_now_iso(self._now())
        old = {p.symbol: p for p in self.settings.tickers}
        pins: list[TickerPin] = []
        for pin in new.tickers:
            prev = old.get(pin.symbol)
            since = (
                prev.pinned_since_utc
                if prev is not None and prev.params_key() == pin.params_key()
                else now_iso
            )
            pins.append(pin.model_copy(update={"pinned_since_utc": since or now_iso}))
        enabled_since = (
            self.settings.enabled_since_utc
            if (new.enabled and self.settings.enabled)
            else (now_iso if new.enabled else None)
        )
        stored = CollectorSettings(
            enabled=new.enabled,
            interval_min=new.interval_min,
            tickers=pins,
            updated_at_utc=now_iso,
            enabled_since_utc=enabled_since,
        )
        self.store.save_settings(stored)
        self.settings = stored
        self._wake.set()
        return stored

    # ------------------------------------------------------------------ startup gaps
    def record_missed_slots(self) -> list[RunRecord]:
        """Record slots that passed while the app was not running (collector was enabled)."""
        if not self.settings.enabled or not self.settings.tickers:
            return []
        now = self._now()
        runs = self.store.read_runs()
        marks = [_parse_utc(r.planned_slot_utc) for r in runs]
        if self.settings.enabled_since_utc:
            marks.append(_parse_utc(self.settings.enabled_since_utc))
        if not marks:
            return []
        start = max(max(marks), now - timedelta(days=MISSED_LOOKBACK_DAYS))
        missed = slots_between(start, now - timedelta(seconds=1), self.settings.interval_min)
        records = [
            RunRecord(
                planned_slot_utc=utc_now_iso(slot.at_utc),
                slot_kind=slot.kind,
                actual_run_utc=None,
                ticker=pin.symbol,
                status="MISSED",
                note="App was not running at the planned time.",
                requested_range_pct=pin.range_pct,
                requested_strike_step=pin.strike_step,
                dte_min=pin.dte_min,
                dte_max=pin.dte_max,
            )
            for slot in missed
            for pin in self.settings.tickers
        ]
        self.store.append_runs(records)
        if records:
            logger.warning("collector recorded %d missed slot(s) on startup", len(missed))
        return records

    # ------------------------------------------------------------------ one slot
    async def run_slot(self, slot: Slot) -> list[RunRecord]:
        """Run one collection cycle for `slot`; returns (and logs) one record per attempt."""
        pins = list(self.settings.tickers)
        self.last_cycle_planned = slot.at_utc
        records: list[RunRecord] = []
        self.busy = True
        try:
            postponed = await self._postpone_for_corridor()
            if not await self.web.acquire_refresh(COLLECTOR_OWNER, MANUAL_REFRESH_WAIT_SEC):
                records = [
                    self._record(slot, pin, status="SKIPPED", note="A manual refresh kept running.")
                    for pin in pins
                ]
                self.store.append_runs(records)
                return records
            retry_fail: list[TickerPin] = []
            retry_cov: list[TickerPin] = []
            try:
                self.last_cycle_actual = self._now()
                for pin in pins:
                    rec = await self._collect_one(slot, pin, attempt=1, postponed=postponed)
                    records.append(rec)
                    self.store.append_runs([rec])
                    if rec.status == "FAILED" and _retryable(rec):
                        retry_fail.append(pin)
                    elif rec.status == "LOW_QUALITY" and _low_coverage_only(rec):
                        retry_cov.append(pin)
            finally:
                self.current_ticker = None
                self.web.release_refresh()
            pass_end = self._now()
            for delay, group in (
                (LOW_COVERAGE_RETRY_DELAY_SEC, retry_cov),
                (FAILURE_RETRY_DELAY_SEC, retry_fail),
            ):
                if not group:
                    continue
                target = pass_end + timedelta(seconds=delay)
                while await self._sleep_until(target):
                    pass  # a settings change does not shorten the retry delay
                if not await self.web.acquire_refresh(COLLECTOR_OWNER, MANUAL_REFRESH_WAIT_SEC):
                    skipped = [
                        self._record(
                            slot, p, status="SKIPPED", attempt=2, note="Retry skipped: busy."
                        )
                        for p in group
                    ]
                    records.extend(skipped)
                    self.store.append_runs(skipped)
                    continue
                try:
                    for pin in group:
                        rec = await self._collect_one(slot, pin, attempt=2, postponed=0.0)
                        records.append(rec)
                        self.store.append_runs([rec])
                finally:
                    self.current_ticker = None
                    self.web.release_refresh()
            return records
        finally:
            self.busy = False

    async def _postpone_for_corridor(self) -> float:
        waited = 0.0
        while self.web.corridor.active_stream_count and waited < CORRIDOR_POSTPONE_MAX_SEC:
            await self._wait(CORRIDOR_POLL_SEC)
            waited += CORRIDOR_POLL_SEC
        return waited

    def _record(
        self,
        slot: Slot,
        pin: TickerPin,
        *,
        status: str,
        attempt: int = 1,
        actual: datetime | None = None,
        **extra: Any,
    ) -> RunRecord:
        return RunRecord.model_validate(
            {
                "planned_slot_utc": utc_now_iso(slot.at_utc),
                "slot_kind": slot.kind,
                "actual_run_utc": utc_now_iso(actual) if actual else None,
                "ticker": pin.symbol,
                "attempt": attempt,
                "status": status,
                "requested_range_pct": pin.range_pct,
                "requested_strike_step": pin.strike_step,
                "dte_min": pin.dte_min,
                "dte_max": pin.dte_max,
                **extra,
            }
        )

    async def _collect_one(
        self, slot: Slot, pin: TickerPin, *, attempt: int, postponed: float
    ) -> RunRecord:
        from src.web.api import RefreshRequest  # local import: api imports this module

        self.current_ticker = pin.symbol
        started = self._now()
        note = (
            f"Postponed {postponed:.0f}s while a corridor stream was active." if postponed else None
        )
        try:
            symbol = self.web.validate_ticker(pin.symbol)
            req = RefreshRequest(
                ticker=symbol,
                range_pct=pin.range_pct,
                strike_step=pin.strike_step,
                dte_min=pin.dte_min,
                dte_max=pin.dte_max,
            )
            payload = await asyncio.wait_for(
                self.web.refresh_chain_locked(req, collection_mode="SCHEDULED"),
                timeout=TICKER_REFRESH_TIMEOUT_SEC,
            )
        except InvalidTickerError as exc:
            return self._record(
                slot,
                pin,
                status="FAILED",
                attempt=attempt,
                actual=started,
                error=f"INVALID_TICKER: {exc}",
                note=note,
            )
        except TimeoutError:
            return self._record(
                slot,
                pin,
                status="FAILED",
                attempt=attempt,
                actual=started,
                error=f"TIMEOUT: no result within {TICKER_REFRESH_TIMEOUT_SEC:.0f}s",
                note=note,
            )
        except GatewayConnectionError as exc:
            return self._record(
                slot,
                pin,
                status="FAILED",
                attempt=attempt,
                actual=started,
                error=f"GATEWAY_OFFLINE: {exc}",
                note=note,
            )
        except AnalyzerError as exc:
            return self._record(
                slot,
                pin,
                status="FAILED",
                attempt=attempt,
                actual=started,
                error=f"{type(exc).__name__}: {exc}",
                note=note,
            )
        except Exception as exc:  # never crash the cycle
            logger.exception("collector refresh failed for %s", pin.symbol)
            return self._record(
                slot,
                pin,
                status="FAILED",
                attempt=attempt,
                actual=started,
                error=f"UNEXPECTED: {type(exc).__name__}: {exc}",
                note=note,
            )

        meta = payload.meta
        verdict = evaluate_snapshot(
            SnapshotMeta(
                snapshot_id=meta.snapshot_id,
                file_path="",
                snapshot_ts_utc=meta.snapshot_ts_utc,
                market_date=meta.market_date,
                market_data_type=meta.market_data_type,
                underlying_price=meta.underlying_price,
                contract_count=meta.contract_count,
                expirations=meta.expirations,
                spot_source=meta.spot_source,
                quote_coverage=meta.quote_coverage,
            )
        )
        effective = meta.strike_step
        drift = (effective is None or abs(effective - pin.strike_step) > 1e-6) or (
            meta.range_pct is not None and abs(meta.range_pct - pin.range_pct) > 1e-6
        )
        return self._record(
            slot,
            pin,
            status="OK" if verdict.usable else "LOW_QUALITY",
            attempt=attempt,
            actual=started,
            snapshot_id=meta.snapshot_id,
            quote_coverage=meta.quote_coverage,
            usable=verdict.usable,
            quality_reasons=list(verdict.reasons),
            effective_strike_step=effective,
            pin_drift=drift,
            note=note,
        )

    # ------------------------------------------------------------------ status
    def status(self) -> CollectorStatus:
        now = self._now()
        today = et_date(now)
        session = session_for(today)
        runs = self.store.read_runs()
        last_results: dict[str, RunRecord] = {}
        for rec in runs:
            last_results[rec.ticker] = rec
        errors = [r for r in runs if r.status in ("FAILED", "SKIPPED")]

        def _on_today(rec: RunRecord) -> bool:
            return et_date(_parse_utc(rec.actual_run_utc or rec.planned_slot_utc)) == today

        today_runs = [r for r in runs if _on_today(r)]
        recent_cut = now - timedelta(days=MISSED_LOOKBACK_DAYS)
        nxt = next_slot(now, self.settings.interval_min) if self.settings.enabled else None
        closing = next_closing_slot(now)
        warning = None
        if not calendar_covers(today):
            warning = (
                f"Holiday calendar ends {CALENDAR_LAST_DAY.isoformat()}: weekends are still "
                "skipped but exchange holidays are not known. Update src/collector/calendar.py."
            )
        session_text = None
        if session is not None:
            close_t = EARLY_CLOSE_ET if session.early_close else SESSION_CLOSE_ET
            session_text = f"{SESSION_OPEN_ET:%H:%M}-{close_t:%H:%M} ET"
        return CollectorStatus(
            enabled=self.settings.enabled,
            restored_on_startup=self.restored_on_startup and self.settings.enabled,
            interval_min=self.settings.interval_min,
            tickers=list(self.settings.tickers),
            busy=self.busy,
            current_ticker=self.current_ticker,
            now=_time_view(now),
            next_run=_time_view(nxt.at_utc, nxt.kind) if nxt else None,
            next_closing=_time_view(closing.at_utc, closing.kind) if closing else None,
            today_trading_day=session is not None,
            today_session_et=session_text,
            today_early_close=bool(session and session.early_close),
            calendar_source_url=CALENDAR_SOURCE_URL,
            calendar_valid_until=CALENDAR_LAST_DAY.isoformat(),
            calendar_warning=warning,
            last_cycle_planned=_time_view(self.last_cycle_planned)
            if self.last_cycle_planned
            else None,
            last_cycle_actual=_time_view(self.last_cycle_actual)
            if self.last_cycle_actual
            else None,
            last_results=last_results,
            last_error=errors[-1] if errors else None,
            snapshots_today=sum(1 for r in today_runs if r.snapshot_id),
            gaps_today=sum(1 for r in today_runs if r.status in ("FAILED", "SKIPPED", "MISSED")),
            missed_recent=sum(
                1
                for r in runs
                if r.status == "MISSED" and _parse_utc(r.planned_slot_utc) >= recent_cut
            ),
            recent_runs=runs[-RECENT_RUNS_IN_STATUS:],
            last_corridor_stop=self.web.last_corridor_stop,
        )


def _retryable(rec: RunRecord) -> bool:
    return rec.attempt == 1 and not (rec.error or "").startswith(("INVALID_TICKER", "UNEXPECTED"))


def _low_coverage_only(rec: RunRecord) -> bool:
    """Retry only when low coverage is the sole problem (frozen / off-session would not help)."""
    if rec.attempt != 1 or rec.quote_coverage is None or rec.quote_coverage >= MIN_QUOTE_COVERAGE:
        return False
    return all(reason.startswith("quote_coverage") for reason in rec.quality_reasons)
