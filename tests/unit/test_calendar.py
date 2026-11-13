"""Unit tests for the pure NYSE regular-session trading calendar (`src/core/calendar.py`)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from src.core.calendar import (
    CALENDAR_FIRST_DAY,
    CALENDAR_LAST_DAY,
    DATA_DELAY,
    EARLY_CLOSE_ET,
    NYSE_EARLY_CLOSES,
    NYSE_HOLIDAYS,
    SESSION_CLOSE_ET,
    SESSION_OPEN_ET,
    US_EASTERN,
    calendar_covers,
    et_date,
    fmt_et,
    is_trading_day,
    next_trading_day,
    session_for,
    trading_days_between,
)


def test_holidays_and_early_closes_match_nyse_calendar() -> None:
    assert date(2026, 1, 1) == CALENDAR_FIRST_DAY
    assert date(2027, 12, 31) == CALENDAR_LAST_DAY
    assert timedelta(minutes=15) == DATA_DELAY

    for holiday in (
        date(2026, 1, 1),
        date(2026, 4, 3),
        date(2026, 7, 3),
        date(2026, 11, 26),
        date(2026, 12, 25),
        date(2027, 1, 1),
        date(2027, 12, 24),
    ):
        assert holiday in NYSE_HOLIDAYS
        assert not is_trading_day(holiday)
        assert session_for(holiday) is None

    # Weekend is never a trading day
    assert not is_trading_day(date(2026, 11, 14))
    assert session_for(date(2026, 11, 14)) is None
    assert not is_trading_day(date(2026, 11, 15))
    assert session_for(date(2026, 11, 15)) is None

    # Regular session (EST = UTC-5 in mid-November)
    reg = session_for(date(2026, 11, 11))
    assert reg is not None
    assert not reg.early_close
    assert reg.open_utc == datetime(2026, 11, 11, 14, 30, tzinfo=UTC)
    assert reg.close_utc == datetime(2026, 11, 11, 21, 0, tzinfo=UTC)
    assert reg.open_utc.astimezone(US_EASTERN).time() == SESSION_OPEN_ET
    assert reg.close_utc.astimezone(US_EASTERN).time() == SESSION_CLOSE_ET

    # Early close (1:00 PM ET)
    for early_day in (date(2026, 11, 27), date(2026, 12, 24), date(2027, 11, 26)):
        assert early_day in NYSE_EARLY_CLOSES
        early = session_for(early_day)
        assert early is not None
        assert early.early_close is True
        assert early.close_utc.astimezone(US_EASTERN).time() == EARLY_CLOSE_ET


def test_et_date_dst_boundaries_calendar_covers_and_navigation() -> None:
    # 02:00 UTC on 2026-11-12 is 21:00 EST on 2026-11-11
    assert et_date(datetime(2026, 11, 12, 2, 0, tzinfo=UTC)) == date(2026, 11, 11)
    # Naive timestamp interpreted as UTC
    assert et_date(datetime(2026, 7, 15, 2, 0)) == date(2026, 7, 14)
    # EDT (UTC-4): 13:30 UTC == 09:30 ET
    assert fmt_et(datetime(2026, 7, 15, 13, 30, tzinfo=UTC)) == "2026-07-15 09:30 ET"

    assert calendar_covers(date(2026, 1, 1))
    assert calendar_covers(date(2027, 12, 31))
    assert not calendar_covers(date(2025, 12, 31))
    assert not calendar_covers(date(2028, 1, 1))

    # Thanksgiving week 2026: Mon Nov 23, Tue Nov 24, Wed Nov 25, [Thu Nov 26 holiday], Fri Nov 27
    days = trading_days_between(date(2026, 11, 23), date(2026, 11, 29))
    assert days == [
        date(2026, 11, 23),
        date(2026, 11, 24),
        date(2026, 11, 25),
        date(2026, 11, 27),
    ]
    assert trading_days_between(date(2026, 11, 25), date(2026, 11, 24)) == []
    # Next trading day from Wed Nov 25 skips Thanksgiving holiday
    assert next_trading_day(date(2026, 11, 25)) == date(2026, 11, 27)
    # Next trading day from Fri Nov 27 skips weekend
    assert next_trading_day(date(2026, 11, 27)) == date(2026, 11, 30)
