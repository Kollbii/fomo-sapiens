"""Contract-filtered BSM enrichment, price basis resolution, tracked fallback, and settlement."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

import polars as pl

from src.analytics.backtest.context import (
    BarContext,
    clean_positive_price,
    extract_bar_context,
    is_leg_settled,
)
from src.analytics.backtest.models import (
    FLAG_FROZEN_MARKET_DATA,
    FLAG_LATE_EXPIRY_SETTLEMENT,
    FLAG_LEG_FALLBACK_PRICE,
    FLAG_MISSING_CONTRACT,
    FLAG_MISSING_PRICE,
    FLAG_NO_LAST_PRINT,
    FLAG_NO_USABLE_DAILY_SNAPSHOT,
    FLAG_TRACKED_LEG_USED,
    FLAG_UNRESOLVED_EXPIRY_LEG,
    FLAG_UNRESOLVED_EXPIRY_SPOT,
    BacktestConfig,
    BacktestLegMark,
    ClosePriceMode,
    LegMarkStatus,
    LegPriceSource,
    LegQuoteSource,
    PriceBasis,
    StepStatus,
    deduplicate_flags,
)
from src.analytics.bsm_fallback import BSMFallbackEngine
from src.analytics.strategies.chain import opt_float
from src.analytics.strategies.models import EvaluatedLeg, LegSide, StrategyLegInput
from src.core.calendar import et_date
from src.storage.backtest_store import DailyChainBar
from src.storage.quality import USABLE_SPOT_SOURCES
from src.storage.schema import COLUMNS, OPTION_CHAIN_POLARS_SCHEMA, OptionRight

STRIKE_TOL: Final = 1e-4


@dataclass(frozen=True)
class ResolvedLegQuote:
    """Resolved price components for one option leg quote row."""

    price_used: float | None
    natural_close_price: float | None
    natural_open_price: float | None
    price_source: LegPriceSource
    used_fallback: bool
    natural_close_fallback: bool
    no_last_print: bool


@dataclass(frozen=True)
class EvaluatedStepData:
    """Computed leg marks, dollar valuations, settlement flags, and status on one bar."""

    legs: list[BacktestLegMark]
    dte_min_leg: int
    mark_value_dollars: float | None
    natural_close_value_dollars: float | None
    active_close_value_dollars: float | None
    all_legs_settled: bool
    any_leg_settled: bool
    settlement_blocked: bool
    unexpired_close_contracts: int
    flags: list[str]
    tracked_snapshot_ids: list[str]
    status: StepStatus


def filter_df_for_dte_window(df: pl.DataFrame, dte_min: int, dte_max: int) -> pl.DataFrame:
    """Filter chain DataFrame to `[dte_min, dte_max]` inclusive before any BSM or wall work."""
    return df if df.height == 0 else df.filter(pl.col("dte").is_between(dte_min, dte_max))


def filter_df_for_legs(
    df: pl.DataFrame, legs: Sequence[StrategyLegInput | EvaluatedLeg]
) -> pl.DataFrame:
    """Filter chain/tracked DataFrame strictly down to target `(expiry, strike, right)` legs."""
    if df.height == 0 or not legs:
        return df.head(0)
    expr: pl.Expr | None = None
    for leg in legs:
        cond = (
            (pl.col("expiry") == leg.expiry)
            & (pl.col("right") == leg.right)
            & ((pl.col("strike") - float(leg.strike)).abs() < STRIKE_TOL)
        )
        expr = cond if expr is None else (expr | cond)
    assert expr is not None
    return df.filter(expr)


def enrich_filtered_greeks(df: pl.DataFrame, *, risk_free_rate: float) -> pl.DataFrame:
    """Run BSM fallback ONLY on filtered rows whose IV or Greeks are missing."""
    if df.height == 0:
        return df
    valid_mask = (pl.col("greeks_source") != "NONE") & pl.all_horizontal(
        pl.col(["iv", "delta", "gamma", "theta", "vega"]).is_not_null()
    )
    need_df = df.filter(~valid_mask)
    if need_df.height == 0:
        return df
    mid_expr = (pl.col("bid") + pl.col("ask")) / 2.0
    derived_mark = (
        pl.when(
            pl.col("bid").is_not_null()
            & pl.col("ask").is_not_null()
            & (pl.col("bid") > 0.0)
            & (pl.col("ask") >= pl.col("bid"))
        )
        .then(mid_expr)
        .when(pl.col("last").is_not_null() & (pl.col("last") > 0.0))
        .then(pl.col("last"))
        .when(pl.col("close").is_not_null() & (pl.col("close") > 0.0))
        .then(pl.col("close"))
        .otherwise(pl.lit(None, dtype=pl.Float64))
    )
    prepared = need_df.with_columns(
        pl.coalesce([pl.col("mark_price"), derived_mark]).alias("mark_price")
    )
    enriched = BSMFallbackEngine.enrich_missing_greeks(prepared, risk_free_rate=risk_free_rate)
    ok_df = df.filter(valid_mask)
    if ok_df.height == 0:
        return enriched
    return pl.concat(
        [ok_df.select(COLUMNS).cast(OPTION_CHAIN_POLARS_SCHEMA), enriched], how="vertical"
    )


def resolve_row_prices(
    raw: Mapping[str, object],
    *,
    side: LegSide,
    price_basis: PriceBasis,
    close_price_mode: ClosePriceMode,
) -> ResolvedLegQuote:
    """Resolve leg mark price, natural close/open fills, and fallback status under `price_basis`."""
    bid, ask = clean_positive_price(raw.get("bid")), clean_positive_price(raw.get("ask"))
    last, close = clean_positive_price(raw.get("last")), clean_positive_price(raw.get("close"))

    price_used: float | None = None
    price_source: LegPriceSource = "NO_PRICE"
    used_fallback, no_last_print = False, False

    if price_basis == "AUTO_MID_LAST_CLOSE":
        if bid is not None and ask is not None and ask >= bid:
            price_used, price_source = (bid + ask) / 2.0, "MID"
        elif last is not None:
            price_used, price_source, used_fallback = last, "LAST", True
        elif close is not None:
            price_used, price_source, used_fallback = close, "CLOSE", True
    elif last is not None:
        price_used, price_source = last, "LAST"
    elif close is not None:
        price_used, price_source, used_fallback = close, "CLOSE", True
    else:
        no_last_print = True

    nat_close_px = ask if side == "SELL" else bid
    nat_open_px = bid if side == "SELL" else ask
    natural_close = nat_close_px if nat_close_px is not None else price_used
    nat_fb = (
        nat_close_px is None and close_price_mode == "NATURAL_SPREAD" and price_used is not None
    )
    natural_open = nat_open_px if nat_open_px is not None else price_used
    return ResolvedLegQuote(
        price_used, natural_close, natural_open, price_source, used_fallback, nat_fb, no_last_print
    )


def _match_row(
    df: pl.DataFrame, expiry: str, strike: float, right: OptionRight
) -> dict[str, object] | None:
    if df.height == 0:
        return None
    sub = df.filter(
        (pl.col("expiry") == expiry)
        & (pl.col("right") == right)
        & ((pl.col("strike") - strike).abs() < STRIKE_TOL)
    )
    if sub.height == 0:
        return None
    if sub.height > 1 and "snapshot_ts_utc" in sub.columns:
        return dict(sub.sort(["snapshot_ts_utc", "snapshot_id"]).row(-1, named=True))
    return dict(sub.row(0, named=True))


def _row_usable(res: ResolvedLegQuote, close_price_mode: ClosePriceMode) -> bool:
    active = res.price_used if close_price_mode == "MID" else res.natural_close_price
    return active is not None and not res.no_last_print


def lookup_legs_on_bar(
    bar: DailyChainBar,
    legs: Sequence[StrategyLegInput | EvaluatedLeg],
    *,
    price_basis: PriceBasis,
    close_price_mode: ClosePriceMode,
    risk_free_rate: float,
) -> tuple[dict[int, tuple[dict[str, object], LegQuoteSource]], list[str]]:
    """Lookup target legs in `bar.df` then `bar.tracked_df` with contract-filtered BSM."""
    chain_sub = enrich_filtered_greeks(
        filter_df_for_legs(bar.df, legs), risk_free_rate=risk_free_rate
    )
    found: dict[int, tuple[dict[str, object], LegQuoteSource]] = {}
    missing: list[int] = []
    for idx, leg in enumerate(legs):
        row = _match_row(chain_sub, leg.expiry, float(leg.strike), leg.right)
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
    if missing and bar.tracked_df is not None and bar.tracked_df.height > 0:
        tracked_sub = enrich_filtered_greeks(
            filter_df_for_legs(bar.tracked_df, [legs[i] for i in missing]),
            risk_free_rate=risk_free_rate,
        )
        for idx in missing:
            leg = legs[idx]
            t_row = _match_row(tracked_sub, leg.expiry, float(leg.strike), leg.right)
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
    return found, deduplicate_flags(tracked_sids)


def _mk_leg(leg: StrategyLegInput | EvaluatedLeg, **kwargs: Any) -> BacktestLegMark:
    return BacktestLegMark(
        side=leg.side, right=leg.right, expiry=leg.expiry, strike=leg.strike, qty=leg.qty, **kwargs
    )


def _settled_leg_mark(
    leg: StrategyLegInput | EvaluatedLeg, bar_ctx: BarContext, bar_et: date, flags: list[str]
) -> tuple[BacktestLegMark, float | None, bool]:
    if bar_et > date.fromisoformat(leg.expiry):
        flags.append(FLAG_LATE_EXPIRY_SETTLEMENT)
    spot = bar_ctx.spot
    if spot is None or spot <= 0.0 or bar_ctx.spot_source not in USABLE_SPOT_SOURCES:
        flags.append(FLAG_UNRESOLVED_EXPIRY_SPOT)
        return (
            _mk_leg(leg, price_source="NO_PRICE", quote_source="SETTLEMENT", status="UNRESOLVED"),
            None,
            True,
        )
    intr = max(0.0, spot - leg.strike) if leg.right == "C" else max(0.0, leg.strike - spot)
    px = round(intr, 6)
    mark = _mk_leg(
        leg,
        price_used=px,
        natural_price=px,
        price_source="INTRINSIC",
        quote_source="SETTLEMENT",
        status="SETTLED",
    )
    return mark, px, False


def _market_leg_mark(
    leg: StrategyLegInput | EvaluatedLeg,
    entry: tuple[dict[str, object], LegQuoteSource] | None,
    config: BacktestConfig,
    any_settled: bool,
    flags: list[str],
) -> tuple[BacktestLegMark, float | None, float | None, float | None, bool, bool]:
    if entry is None:
        flags.append(FLAG_MISSING_CONTRACT)
        if any_settled:
            flags.append(FLAG_UNRESOLVED_EXPIRY_LEG)
        return (
            _mk_leg(leg, price_source="NO_PRICE", quote_source="NONE", status="MISSING_CONTRACT"),
            None,
            None,
            None,
            any_settled,
            False,
        )

    raw, q_src = entry
    used_tracked = q_src == "TRACKED"
    if used_tracked:
        flags.append(FLAG_TRACKED_LEG_USED)
    res = resolve_row_prices(
        raw, side=leg.side, price_basis=config.price_basis, close_price_mode=config.close_price_mode
    )
    if res.no_last_print:
        flags.append(FLAG_NO_LAST_PRINT)
    if res.used_fallback or res.natural_close_fallback:
        flags.append(FLAG_LEG_FALLBACK_PRICE)

    active_px = res.price_used if config.close_price_mode == "MID" else res.natural_close_price
    blocked = False
    if active_px is None:
        flags.append(FLAG_MISSING_PRICE)
        if any_settled:
            blocked = True
            flags.append(FLAG_UNRESOLVED_EXPIRY_LEG)
        leg_status: LegMarkStatus = "MISSING_PRICE"
    else:
        leg_status = "FALLBACK_PRICE" if (res.used_fallback or res.natural_close_fallback) else "OK"

    nat = round(res.natural_close_price, 4) if res.natural_close_price is not None else None
    mark = _mk_leg(
        leg,
        price_used=round(res.price_used, 4) if res.price_used is not None else None,
        natural_price=nat,
        price_source=res.price_source,
        quote_source=q_src,
        status=leg_status,
        iv=opt_float(raw.get("iv")),
        delta=opt_float(raw.get("delta")),
        gamma=opt_float(raw.get("gamma")),
        theta=opt_float(raw.get("theta")),
        vega=opt_float(raw.get("vega")),
    )
    return mark, res.price_used, res.natural_close_price, active_px, blocked, used_tracked


def _acc(total: float, ok: bool, mult: float, px: float | None) -> tuple[float, bool]:
    return (total + mult * px, ok) if px is not None else (total, False)


def evaluate_trade_step_on_bar(
    bar: DailyChainBar,
    legs: Sequence[StrategyLegInput | EvaluatedLeg],
    config: BacktestConfig,
    *,
    ctx: BarContext | None = None,
) -> EvaluatedStepData:
    """Mark every leg of an open trade on `bar` (settling expired legs when cutoff reached)."""
    bar_ctx = ctx if ctx is not None else extract_bar_context(bar)
    bar_et = et_date(bar.ts_utc)
    settled_flags = [is_leg_settled(leg.expiry, bar.ts_utc) for leg in legs]
    any_settled, all_settled = any(settled_flags), all(settled_flags)

    unsettled = [leg for leg, is_set in zip(legs, settled_flags, strict=True) if not is_set]
    matched_map, matched_tracked_sids = (
        lookup_legs_on_bar(
            bar,
            unsettled,
            price_basis=config.price_basis,
            close_price_mode=config.close_price_mode,
            risk_free_rate=config.risk_free_rate,
        )
        if unsettled
        else ({}, [])
    )
    unsettled_pos = {
        idx: pos for pos, idx in enumerate(i for i, s in enumerate(settled_flags) if not s)
    }

    flags: list[str] = list(bar_ctx.flags)
    leg_marks: list[BacktestLegMark] = []
    mark_val = nat_val = active_val = 0.0
    mark_ok = nat_ok = active_ok = True
    settlement_blocked = used_tracked = False
    unexpired_close_contracts = 0

    for idx, leg in enumerate(legs):
        mult = (1.0 if leg.side == "BUY" else -1.0) * float(leg.qty) * 100.0
        if settled_flags[idx]:
            l_mark, intr, blocked = _settled_leg_mark(leg, bar_ctx, bar_et, flags)
            leg_marks.append(l_mark)
            if blocked or intr is None:
                mark_ok = nat_ok = active_ok = False
                settlement_blocked = True
            else:
                mark_val, nat_val, active_val = (
                    mark_val + mult * intr,
                    nat_val + mult * intr,
                    active_val + mult * intr,
                )
            continue
        unexpired_close_contracts += leg.qty
        l_mark, px_u, px_n, px_a, blk, trk = _market_leg_mark(
            leg, matched_map.get(unsettled_pos[idx]), config, any_settled, flags
        )
        leg_marks.append(l_mark)
        settlement_blocked, used_tracked = settlement_blocked or blk, used_tracked or trk
        mark_val, mark_ok = _acc(mark_val, mark_ok, mult, px_u)
        nat_val, nat_ok = _acc(nat_val, nat_ok, mult, px_n)
        active_val, active_ok = _acc(active_val, active_ok, mult, px_a)

    deduped = deduplicate_flags(flags)
    dte_min_leg = min(max(0, (date.fromisoformat(leg.expiry) - bar_et).days) for leg in legs)
    tracked_ids = (matched_tracked_sids or list(bar.tracked_snapshot_ids)) if used_tracked else []

    if not active_ok or settlement_blocked:
        step_status: StepStatus = "INCOMPLETE"
    elif all_settled and bar_ctx.spot_source in USABLE_SPOT_SOURCES:
        bad = {FLAG_LATE_EXPIRY_SETTLEMENT, FLAG_FROZEN_MARKET_DATA, FLAG_NO_USABLE_DAILY_SNAPSHOT}
        step_status = "UNRELIABLE" if any(f in bad for f in deduped) else "OK"
    elif (
        not bar_ctx.bar_reliable
        or FLAG_LEG_FALLBACK_PRICE in deduped
        or FLAG_NO_LAST_PRINT in deduped
    ):
        step_status = "UNRELIABLE"
    else:
        step_status = "OK"

    return EvaluatedStepData(
        legs=leg_marks,
        dte_min_leg=dte_min_leg,
        mark_value_dollars=round(mark_val, 2) if mark_ok else None,
        natural_close_value_dollars=round(nat_val, 2) if nat_ok else None,
        active_close_value_dollars=round(active_val, 2) if active_ok else None,
        all_legs_settled=all_settled,
        any_leg_settled=any_settled,
        settlement_blocked=settlement_blocked,
        unexpired_close_contracts=unexpired_close_contracts,
        flags=deduped,
        tracked_snapshot_ids=tracked_ids,
        status=step_status,
    )
