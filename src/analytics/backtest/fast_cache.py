"""Pure in-memory daily-bar index and memoized lookup/scope cache for fast backtesting."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final

import polars as pl

if TYPE_CHECKING:
    from src.analytics.backtest.rules import EntryBuildResult

from src.analytics.backtest.context import BarContext, extract_bar_context, is_leg_settled
from src.analytics.backtest.models import (
    ClosePriceMode,
    LegQuoteSource,
    PriceBasis,
    deduplicate_flags,
)
from src.analytics.backtest.pricing import (
    ResolvedLegQuote,
    enrich_filtered_greeks,
    filter_df_for_dte_window,
    resolve_row_prices,
)
from src.analytics.strategies.chain import PreparedRow, prepare_rows
from src.analytics.strategies.models import EvaluatedLeg, StrategyLegInput
from src.analytics.walls import ChainWallsReport, compute_walls
from src.core.calendar import et_date
from src.storage.backtest_store import DailyChainBar
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA, OptionRight

QuoteKey = tuple[str, float, OptionRight]
EntryScopeTuple = tuple[str, pl.DataFrame, ChainWallsReport, str, float, list[PreparedRow]]

TARGET_DTE_CENTER: Final = 38


def pick_front_expiry(df: pl.DataFrame, *, target_dte_min: int, target_dte_max: int) -> str:
    """Pick the front expiration closest to `TARGET_DTE_CENTER` (or window midpoint)."""
    target_center = (
        float(TARGET_DTE_CENTER)
        if target_dte_min <= TARGET_DTE_CENTER <= target_dte_max
        else 0.5 * float(target_dte_min + target_dte_max)
    )
    exp_rows = df.select(["expiry", "dte"]).unique(subset=["expiry"]).iter_rows(named=True)
    best = min(
        exp_rows,
        key=lambda r: (abs(float(r["dte"]) - target_center), int(r["dte"]), str(r["expiry"])),
    )
    return str(best["expiry"])


def _quote_key(expiry: str, strike: float, right: OptionRight) -> QuoteKey:
    return (expiry, round(float(strike), 4), right)


def _row_usable(res: ResolvedLegQuote, close_price_mode: ClosePriceMode) -> bool:
    active = res.price_used if close_price_mode == "MID" else res.natural_close_price
    return active is not None and not res.no_last_print


def _needs_greeks_enrichment(row: dict[str, object]) -> bool:
    if row.get("greeks_source") == "NONE":
        return True
    for col in ("iv", "delta", "gamma", "theta", "vega"):
        val = row.get(col)
        if val is None or (isinstance(val, float) and math.isnan(val)):
            return True
    return False


def _index_df(df: pl.DataFrame | None) -> dict[QuoteKey, dict[str, object]]:
    if df is None or df.height == 0:
        return {}
    has_ts = "snapshot_ts_utc" in df.columns
    if has_ts and df.height > 1:
        sort_cols = (
            ["snapshot_ts_utc", "snapshot_id"]
            if "snapshot_id" in df.columns
            else ["snapshot_ts_utc"]
        )
        ordered = df.sort(sort_cols)
    else:
        ordered = df

    out: dict[QuoteKey, dict[str, object]] = {}
    for row in ordered.iter_rows(named=True):
        right_str = str(row.get("right") or "").upper()
        if right_str not in ("C", "P"):
            continue
        right_lit: OptionRight = "C" if right_str == "C" else "P"
        strike_raw = row.get("strike")
        if not isinstance(strike_raw, (int, float)):
            continue
        key = _quote_key(str(row["expiry"]), float(strike_raw), right_lit)
        if has_ts or key not in out:
            out[key] = row
    return out


@dataclass(frozen=True)
class FastBarEntry:
    """Pre-indexed context, ET date, and O(1) quote maps for a single daily bar."""

    bar: DailyChainBar
    ctx: BarContext
    bar_et: date
    chain_rows: dict[QuoteKey, dict[str, object]]
    tracked_rows: dict[QuoteKey, dict[str, object]]


class FastBarCache:
    """Pure, leak-free per-bar quote index and memoized entry-scope / BSM enrichment cache."""

    def __init__(
        self,
        entries: Sequence[FastBarEntry],
        *,
        _entry_scope_cache: dict[tuple[object, ...], EntryScopeTuple | None] | None = None,
        _enriched_quote_cache: dict[tuple[object, ...], dict[str, object]] | None = None,
        _entry_candidate_cache: (
            dict[tuple[object, ...], tuple[EntryBuildResult | None, str | None]] | None
        ) = None,
    ) -> None:
        self._entries: tuple[FastBarEntry, ...] = tuple(entries)
        self._bars: tuple[DailyChainBar, ...] = tuple(entry.bar for entry in self._entries)
        self._entry_scope_cache: dict[tuple[object, ...], EntryScopeTuple | None] = (
            _entry_scope_cache if _entry_scope_cache is not None else {}
        )
        self._enriched_quote_cache: dict[tuple[object, ...], dict[str, object]] = (
            _enriched_quote_cache if _enriched_quote_cache is not None else {}
        )
        self._entry_candidate_cache: dict[
            tuple[object, ...], tuple[EntryBuildResult | None, str | None]
        ] = _entry_candidate_cache if _entry_candidate_cache is not None else {}

    @classmethod
    def from_bars(cls, bars: Sequence[DailyChainBar]) -> FastBarCache:
        """Build a `FastBarCache` sorted chronologically by `(ts_utc, snapshot_id)`."""
        ordered = sorted(bars, key=lambda b: (b.ts_utc, b.snapshot_id))
        entries = [
            FastBarEntry(
                bar=bar,
                ctx=extract_bar_context(bar),
                bar_et=et_date(bar.ts_utc),
                chain_rows=_index_df(bar.df),
                tracked_rows=_index_df(bar.tracked_df),
            )
            for bar in ordered
        ]
        return cls(entries)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple[FastBarEntry, ...]:
        """Chronologically ordered `FastBarEntry` tuple."""
        return self._entries

    @property
    def bars(self) -> tuple[DailyChainBar, ...]:
        """Chronologically ordered `DailyChainBar` tuple."""
        return self._bars

    @property
    def entry_candidate_cache(
        self,
    ) -> dict[tuple[object, ...], tuple[EntryBuildResult | None, str | None]]:
        """Per-bar (`snapshot_id`-keyed) memoized single-bar entry candidate outcomes."""
        return self._entry_candidate_cache

    def slice(self, start: int, end: int) -> FastBarCache:
        """Return a sub-window view sharing strictly per-bar (`snapshot_id`-keyed) caches."""
        return FastBarCache(
            self._entries[start:end],
            _entry_scope_cache=self._entry_scope_cache,
            _enriched_quote_cache=self._enriched_quote_cache,
            _entry_candidate_cache=self._entry_candidate_cache,
        )

    def with_fresh_memo(self) -> FastBarCache:
        """Return a view sharing immutable `_entries` with isolated per-request memo dicts."""
        return FastBarCache(self._entries)

    def lookup_legs_fast(
        self,
        bar_idx: int,
        legs: Sequence[StrategyLegInput | EvaluatedLeg],
        *,
        price_basis: PriceBasis,
        close_price_mode: ClosePriceMode,
        risk_free_rate: float,
        need_greeks: bool = True,
    ) -> tuple[dict[int, tuple[dict[str, object], LegQuoteSource]], list[str]]:
        """O(1) leg lookup in `chain_rows` then `tracked_rows`, with memoized BSM enrichment."""
        entry = self._entries[bar_idx]
        found: dict[int, tuple[dict[str, object], LegQuoteSource]] = {}
        keys_by_idx: dict[int, QuoteKey] = {}
        missing: list[int] = []

        for idx, leg in enumerate(legs):
            key = _quote_key(leg.expiry, float(leg.strike), leg.right)
            keys_by_idx[idx] = key
            row = entry.chain_rows.get(key)
            if row is None:
                missing.append(idx)
                continue
            found[idx] = (row, "CHAIN")
            res = resolve_row_prices(
                row, side=leg.side, price_basis=price_basis, close_price_mode=close_price_mode
            )
            if not _row_usable(res, close_price_mode):
                missing.append(idx)

        tracked_sids: list[str] = []
        if missing and entry.tracked_rows:
            for idx in missing:
                leg = legs[idx]
                key = keys_by_idx[idx]
                t_row = entry.tracked_rows.get(key)
                if t_row is None:
                    continue
                t_res = resolve_row_prices(
                    t_row, side=leg.side, price_basis=price_basis, close_price_mode=close_price_mode
                )
                if idx not in found or _row_usable(t_res, close_price_mode):
                    found[idx] = (t_row, "TRACKED")
                    sid = t_row.get("snapshot_id")
                    if isinstance(sid, str) and sid:
                        tracked_sids.append(sid)

        if need_greeks and found:
            snapshot_id = entry.bar.snapshot_id
            to_enrich_meta: list[tuple[int, LegQuoteSource, QuoteKey, tuple[object, ...]]] = []
            to_enrich_rows: list[dict[str, object]] = []
            for idx, (row, q_src) in list(found.items()):
                if not _needs_greeks_enrichment(row):
                    continue
                key = keys_by_idx[idx]
                cache_key: tuple[object, ...] = (snapshot_id, risk_free_rate, q_src, key)
                cached = self._enriched_quote_cache.get(cache_key)
                if cached is not None:
                    found[idx] = (cached, q_src)
                else:
                    to_enrich_meta.append((idx, q_src, key, cache_key))
                    to_enrich_rows.append(row)

            if to_enrich_rows:
                sub_df = pl.DataFrame(to_enrich_rows, schema=OPTION_CHAIN_POLARS_SCHEMA)
                enriched_df = enrich_filtered_greeks(sub_df, risk_free_rate=risk_free_rate)
                for (idx, q_src, _key, cache_key), enr_row in zip(
                    to_enrich_meta, enriched_df.iter_rows(named=True), strict=True
                ):
                    self._enriched_quote_cache[cache_key] = enr_row
                    found[idx] = (enr_row, q_src)

        return found, deduplicate_flags(tracked_sids)

    def get_entry_scope(
        self,
        bar_idx: int,
        *,
        dte_min: int,
        dte_max: int,
        risk_free_rate: float,
        walls_top_n: int,
        is_diagonal: bool,
    ) -> EntryScopeTuple | None:
        """Return memoized `(front_expiry, enriched_df, walls, underlying, spot, prepared_rows)`."""
        entry = self._entries[bar_idx]
        bar = entry.bar
        cache_key: tuple[object, ...] = (
            bar.snapshot_id,
            dte_min,
            dte_max,
            risk_free_rate,
            walls_top_n,
            is_diagonal,
        )
        if cache_key in self._entry_scope_cache:
            return self._entry_scope_cache[cache_key]

        if bar.df.height == 0:
            self._entry_scope_cache[cache_key] = None
            return None

        front_df = filter_df_for_dte_window(bar.df, dte_min, dte_max)
        if front_df.height == 0:
            self._entry_scope_cache[cache_key] = None
            return None

        unsettled_front = [
            str(exp)
            for exp in front_df["expiry"].unique().to_list()
            if not is_leg_settled(str(exp), bar.ts_utc)
        ]
        if not unsettled_front:
            self._entry_scope_cache[cache_key] = None
            return None

        front_df = front_df.filter(pl.col("expiry").is_in(unsettled_front))
        front_expiry = pick_front_expiry(
            front_df,
            target_dte_min=dte_min,
            target_dte_max=dte_max,
        )
        if is_diagonal:
            raw_scope = filter_df_for_dte_window(bar.df, dte_min, max(dte_max, 120))
            unsettled_scope = [
                str(exp)
                for exp in raw_scope["expiry"].unique().to_list()
                if not is_leg_settled(str(exp), bar.ts_utc)
            ]
            scope_df = raw_scope.filter(pl.col("expiry").is_in(unsettled_scope))
        else:
            scope_df = front_df

        enriched_df = enrich_filtered_greeks(scope_df, risk_free_rate=risk_free_rate)
        walls = compute_walls(enriched_df, top_n=walls_top_n)
        try:
            underlying, spot, prepared = prepare_rows(enriched_df)
        except ValueError:
            underlying, spot, prepared = "", 0.0, []
        scope_val: EntryScopeTuple = (front_expiry, enriched_df, walls, underlying, spot, prepared)
        self._entry_scope_cache[cache_key] = scope_val
        return scope_val
