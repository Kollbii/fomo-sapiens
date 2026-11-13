"""Snapshot quality metrics and the single "usable snapshot" rule.

A snapshot is *usable for history* (daily bar selection and backtests) only if all hold:

1. `quote_coverage >= MIN_QUOTE_COVERAGE`: share of contracts with a current quote.
2. `spot_source` is `LAST` or `MID`: the spot was not the previous session close.
3. `market_data_type` is `1` (live) or `3` (delayed): frozen data (`2` / `4`) is not usable.

Unknown values (older index entries without the fields) make a snapshot *not* usable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import polars as pl

from src.storage.schema import SnapshotMeta, SpotSource

MIN_QUOTE_COVERAGE: Final = 0.90
USABLE_SPOT_SOURCES: Final[frozenset[SpotSource]] = frozenset({"LAST", "MID"})
USABLE_MARKET_DATA_TYPES: Final[frozenset[int]] = frozenset({1, 3})


@dataclass(frozen=True)
class QuoteQuality:
    """Per-snapshot contract quote coverage counts."""

    contract_count: int
    quoted_contract_count: int
    prior_close_only_count: int

    @property
    def quote_coverage(self) -> float:
        if self.contract_count == 0:
            return 0.0
        return round(self.quoted_contract_count / self.contract_count, 4)


def measure_quote_quality(df: pl.DataFrame) -> QuoteQuality:
    """Count quoted rows and rows priced only by the previous session close."""
    if df.height == 0:
        return QuoteQuality(0, 0, 0)
    has_quote = pl.col("bid").is_not_null() | pl.col("ask").is_not_null()
    close_only = (
        pl.col("bid").is_null()
        & pl.col("ask").is_null()
        & pl.col("last").is_null()
        & pl.col("close").is_not_null()
    )
    counts = df.select(
        has_quote.sum().alias("quoted"),
        close_only.sum().alias("close_only"),
    ).row(0)
    return QuoteQuality(df.height, int(counts[0]), int(counts[1]))


@dataclass(frozen=True)
class SnapshotQualityVerdict:
    """Result of applying the usable-snapshot quality gate to `SnapshotMeta`."""

    usable: bool
    reasons: tuple[str, ...]


def evaluate_snapshot(meta: SnapshotMeta) -> SnapshotQualityVerdict:
    """Apply the usable-snapshot rule; `reasons` lists every failed condition."""
    reasons: list[str] = []
    if meta.quote_coverage is None:
        reasons.append("quote_coverage unknown")
    elif meta.quote_coverage < MIN_QUOTE_COVERAGE:
        reasons.append(f"quote_coverage {meta.quote_coverage:.2f} < {MIN_QUOTE_COVERAGE:.2f}")
    if meta.spot_source is None:
        reasons.append("spot_source unknown")
    elif meta.spot_source not in USABLE_SPOT_SOURCES:
        reasons.append(f"spot_source {meta.spot_source} (previous session close)")
    if meta.market_data_type not in USABLE_MARKET_DATA_TYPES:
        reasons.append(f"market_data_type {meta.market_data_type} (frozen or unknown)")
    return SnapshotQualityVerdict(usable=not reasons, reasons=tuple(reasons))


def is_usable_snapshot(meta: SnapshotMeta) -> bool:
    """Return True if `meta` satisfies every snapshot quality condition."""
    return evaluate_snapshot(meta).usable
