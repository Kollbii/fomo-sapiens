"""Daily bar metadata and quality-context extraction (pure, clock-free)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

from src.analytics.backtest.models import (
    FLAG_CLOSE_SPOT_ONLY,
    FLAG_EMPTY_CHAIN,
    FLAG_FROZEN_MARKET_DATA,
    FLAG_LOW_QUOTE_COVERAGE,
    FLAG_NO_USABLE_DAILY_SNAPSHOT,
    FLAG_SNAPSHOT_META_MISSING,
    deduplicate_flags,
)
from src.analytics.strategies.chain import opt_float
from src.core.calendar import DATA_DELAY, SESSION_CLOSE_ET, US_EASTERN, session_for
from src.storage.backtest_store import DailyChainBar
from src.storage.quality import (
    MIN_QUOTE_COVERAGE,
    USABLE_MARKET_DATA_TYPES,
    USABLE_SPOT_SOURCES,
    measure_quote_quality,
)
from src.storage.schema import SpotSource

PRICE_SENTINEL: Final = 1e9


@dataclass(frozen=True)
class BarContext:
    """Extracted metadata and usability flags for a single daily bar."""

    spot: float | None
    spot_source: SpotSource | None
    market_data_type: int | None
    quote_coverage: float | None
    flags: tuple[str, ...]
    bar_reliable: bool


def clean_positive_price(val: object) -> float | None:
    """Return finite positive non-sentinel float price, else `None`."""
    out = opt_float(val)
    return None if (out is None or out <= 0.0 or out >= PRICE_SENTINEL) else out


def extract_bar_context(bar: DailyChainBar) -> BarContext:
    """Extract spot, quote quality, market data type, and reliability flags for `bar`."""
    meta, df = bar.meta, bar.df
    flags: list[str] = list(bar.selection_flags)

    spot: float | None = None
    if meta is not None and 0.0 < meta.underlying_price < PRICE_SENTINEL:
        spot = float(meta.underlying_price)
    elif df.height > 0:
        spot = clean_positive_price(df["underlying_price"][0])

    spot_source: SpotSource | None = meta.spot_source if meta is not None else None
    md_type: int | None = (
        meta.market_data_type
        if meta is not None
        else (int(df["market_data_type"][0]) if df.height > 0 else None)
    )
    quote_coverage: float | None = (
        meta.quote_coverage
        if (meta is not None and meta.quote_coverage is not None)
        else (measure_quote_quality(df).quote_coverage if df.height > 0 else None)
    )

    if df.height == 0:
        flags.append(FLAG_EMPTY_CHAIN)
    if meta is None:
        flags.append(FLAG_SNAPSHOT_META_MISSING)
    if quote_coverage is None or quote_coverage < MIN_QUOTE_COVERAGE:
        flags.append(FLAG_LOW_QUOTE_COVERAGE)
    if spot_source is None or spot_source not in USABLE_SPOT_SOURCES:
        flags.append(FLAG_CLOSE_SPOT_ONLY)
    if md_type is None or md_type not in USABLE_MARKET_DATA_TYPES:
        flags.append(FLAG_FROZEN_MARKET_DATA)

    deduped = tuple(deduplicate_flags(flags))
    reliable = (
        FLAG_NO_USABLE_DAILY_SNAPSHOT not in deduped
        and df.height > 0
        and meta is not None
        and quote_coverage is not None
        and quote_coverage >= MIN_QUOTE_COVERAGE
        and spot_source in USABLE_SPOT_SOURCES
        and md_type in USABLE_MARKET_DATA_TYPES
    )
    return BarContext(spot, spot_source, md_type, quote_coverage, deduped, reliable)


def expiry_settlement_cutoff_utc(expiry: str) -> datetime:
    """Return UTC timestamp when `expiry` settles (`session close + 15m DATA_DELAY`)."""
    exp_day = date.fromisoformat(expiry)
    sess = session_for(exp_day)
    if sess is not None:
        return sess.close_utc + DATA_DELAY
    close_utc = datetime.combine(exp_day, SESSION_CLOSE_ET, tzinfo=US_EASTERN).astimezone(UTC)
    return close_utc + DATA_DELAY


def is_leg_settled(expiry: str, ts_utc: datetime) -> bool:
    """Return True once `ts_utc` is on/after the expiry session settlement cutoff."""
    aware = ts_utc if ts_utc.tzinfo is not None else ts_utc.replace(tzinfo=UTC)
    return aware >= expiry_settlement_cutoff_utc(expiry)
