"""US regular-session calendar and the scheduled-collection slots (pure, clock-free).

Holidays and 13:00 ET early closes are copied from the official NYSE calendar:
    https://www.nyse.com/markets/hours-calendars   (retrieved 2026-11-12)
The same page states "1:00 p.m. (1:15 p.m. for eligible options)" for early closes; the
closing snapshot is taken 15 minutes after the close, which covers that exception.

The table covers 2026-2027 only. Outside it, weekdays are treated as trading days and the
collector status shows a warning (`calendar_covers()` is False).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, Literal
from zoneinfo import ZoneInfo

US_EASTERN: Final = ZoneInfo("America/New_York")  # same zoneinfo source as src/broker/contracts.py
POLAND: Final = ZoneInfo("Europe/Warsaw")

CALENDAR_SOURCE_URL: Final = "https://www.nyse.com/markets/hours-calendars"
CALENDAR_FIRST_DAY: Final = date(2026, 1, 1)
CALENDAR_LAST_DAY: Final = date(2027, 12, 31)

# NYSE market holidays (market closed all day).
NYSE_HOLIDAYS: Final[frozenset[date]] = frozenset(
    {
        # 2026
        date(2026, 1, 1),  # New Year's Day
        date(2026, 1, 19),  # Martin Luther King, Jr. Day
        date(2026, 2, 16),  # Washington's Birthday
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth National Independence Day
        date(2026, 7, 3),  # Independence Day (observed)
        date(2026, 9, 7),  # Labor Day
        date(2026, 11, 26),  # Thanksgiving Day
        date(2026, 12, 25),  # Christmas Day
        # 2027
        date(2027, 1, 1),  # New Year's Day
        date(2027, 1, 18),  # Martin Luther King, Jr. Day
        date(2027, 2, 15),  # Washington's Birthday
        date(2027, 3, 26),  # Good Friday
        date(2027, 5, 31),  # Memorial Day
        date(2027, 6, 18),  # Juneteenth National Independence Day (observed)
        date(2027, 7, 5),  # Independence Day (observed)
        date(2027, 9, 6),  # Labor Day
        date(2027, 11, 25),  # Thanksgiving Day
        date(2027, 12, 24),  # Christmas Day (observed)
    }
)

# NYSE early closes at 1:00 p.m. ET.
NYSE_EARLY_CLOSES: Final[frozenset[date]] = frozenset(
    {
        date(2026, 11, 27),  # day after Thanksgiving
        date(2026, 12, 24),  # Christmas Eve
        date(2027, 11, 26),  # day after Thanksgiving
    }
)

SESSION_OPEN_ET: Final = time(9, 30)
SESSION_CLOSE_ET: Final = time(16, 0)
EARLY_CLOSE_ET: Final = time(13, 0)
# Type 3 data is delayed 15 minutes: the first useful snapshot is 15 minutes after the open,
# the last interval snapshot 15 minutes before the close, the closing snapshot 15 after it.
DATA_DELAY: Final = timedelta(minutes=15)

# 0 = closing snapshot only.
ALLOWED_INTERVALS_MIN: Final[tuple[int, ...]] = (0, 15, 30, 60, 120)
DEFAULT_INTERVAL_MIN: Final = 60

SlotKind = Literal["INTERVAL", "CLOSING"]


@dataclass(frozen=True)
class Session:
    day: date
    open_utc: datetime
    close_utc: datetime
    early_close: bool


@dataclass(frozen=True)
class Slot:
    kind: SlotKind
    at_utc: datetime


def calendar_covers(day: date) -> bool:
    return CALENDAR_FIRST_DAY <= day <= CALENDAR_LAST_DAY


def is_trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in NYSE_HOLIDAYS


def session_for(day: date) -> Session | None:
    """Regular session of `day` (ET calendar date), or None on weekends / holidays."""
    if not is_trading_day(day):
        return None
    early = day in NYSE_EARLY_CLOSES
    open_et = datetime.combine(day, SESSION_OPEN_ET, tzinfo=US_EASTERN)
    close_et = datetime.combine(
        day, EARLY_CLOSE_ET if early else SESSION_CLOSE_ET, tzinfo=US_EASTERN
    )
    return Session(day, open_et.astimezone(UTC), close_et.astimezone(UTC), early)


def slots_for_day(day: date, interval_min: int) -> list[Slot]:
    """Interval slots from open+15 min to close-15 min, then the closing slot at close+15 min."""
    if interval_min not in ALLOWED_INTERVALS_MIN:
        raise ValueError(f"interval_min must be one of {ALLOWED_INTERVALS_MIN}, got {interval_min}")
    session = session_for(day)
    if session is None:
        return []
    slots: list[Slot] = []
    if interval_min > 0:
        at = session.open_utc + DATA_DELAY
        last = session.close_utc - DATA_DELAY
        while at <= last:
            slots.append(Slot("INTERVAL", at))
            at += timedelta(minutes=interval_min)
    slots.append(Slot("CLOSING", session.close_utc + DATA_DELAY))
    return slots


def et_date(ts: datetime) -> date:
    return ts.astimezone(US_EASTERN).date()


def next_slot(now: datetime, interval_min: int, *, max_days: int = 14) -> Slot | None:
    """First slot strictly after `now` (UTC-aware), searching up to `max_days` ahead."""
    day = et_date(now)
    for offset in range(max_days + 1):
        for slot in slots_for_day(day + timedelta(days=offset), interval_min):
            if slot.at_utc > now:
                return slot
    return None


def next_closing_slot(now: datetime, *, max_days: int = 14) -> Slot | None:
    return next_slot(now, 0, max_days=max_days)


def slots_between(start: datetime, end: datetime, interval_min: int) -> list[Slot]:
    """All slots with `start < at <= end` (both UTC-aware)."""
    out: list[Slot] = []
    day = et_date(start)
    last_day = et_date(end)
    while day <= last_day:
        out.extend(s for s in slots_for_day(day, interval_min) if start < s.at_utc <= end)
        day += timedelta(days=1)
    return out


def fmt_et(ts: datetime) -> str:
    return ts.astimezone(US_EASTERN).strftime("%Y-%m-%d %H:%M ET")


def fmt_poland(ts: datetime) -> str:
    return ts.astimezone(POLAND).strftime("%Y-%m-%d %H:%M %Z")
