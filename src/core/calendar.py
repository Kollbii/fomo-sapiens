"""US regular-session NYSE trading calendar (pure, clock-free).

Holidays and 13:00 ET early closes are copied from the official NYSE calendar:
    https://www.nyse.com/markets/hours-calendars   (retrieved 2026-11-12)
The same page states "1:00 p.m. (1:15 p.m. for eligible options)" for early closes; the
closing offset (`DATA_DELAY = 15 min`) covers both the 15-minute delayed feed and the
options early-close exception.

The table covers 2026-2027 only. Outside it, weekdays are treated as trading days and
`calendar_covers()` returns False.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

US_EASTERN: Final = ZoneInfo("America/New_York")

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
# Type 3 data is delayed 15 minutes; also covers 1:15 p.m. options close on early-close days.
DATA_DELAY: Final = timedelta(minutes=15)


@dataclass(frozen=True)
class Session:
    """UTC bounds of one NYSE regular trading session."""

    day: date
    open_utc: datetime
    close_utc: datetime
    early_close: bool


def calendar_covers(day: date) -> bool:
    """Return True if `day` falls inside the verified 2026-2027 NYSE holiday table."""
    return CALENDAR_FIRST_DAY <= day <= CALENDAR_LAST_DAY


def is_trading_day(day: date) -> bool:
    """Return True if `day` is a weekday and not a known NYSE holiday."""
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


def et_date(ts: datetime) -> date:
    """Map a timestamp (aware, or naive interpreted as UTC) to its US Eastern date."""
    aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return aware.astimezone(US_EASTERN).date()


def trading_days_between(start: date, end: date) -> list[date]:
    """Return all NYSE trading days in `[start, end]` inclusive."""
    if start > end:
        return []
    days: list[date] = []
    cur = start
    while cur <= end:
        if is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def next_trading_day(day: date) -> date:
    """Return the first NYSE trading day strictly after `day`."""
    cur = day + timedelta(days=1)
    while not is_trading_day(cur):
        cur += timedelta(days=1)
    return cur


def fmt_et(ts: datetime) -> str:
    """Format a timestamp in US Eastern time (`YYYY-MM-DD HH:MM ET`)."""
    aware = ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)
    return aware.astimezone(US_EASTERN).strftime("%Y-%m-%d %H:%M ET")
