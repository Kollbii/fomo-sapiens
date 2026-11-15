"""Entry strategy construction, trading-calendar cadence checks, and TP/SL/DTE/expiry exit rules."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Final

import polars as pl

from src.analytics.backtest.context import (
    BarContext,
    clean_positive_price,
    extract_bar_context,
    is_leg_settled,
)
from src.analytics.backtest.models import (
    FLAG_BROKER_FILL_ENTRY,
    FLAG_MISSING_PRICE,
    FLAG_NO_LAST_PRINT,
    BacktestConfig,
    BacktestLegMark,
    ExitReason,
    deduplicate_flags,
)
from src.analytics.backtest.pricing import (
    EvaluatedStepData,
    enrich_filtered_greeks,
    evaluate_trade_step_on_bar,
    filter_df_for_dte_window,
    lookup_legs_on_bar,
    resolve_row_prices,
)
from src.analytics.strategies.builders import StrategyFilterError
from src.analytics.strategies.engine import evaluate_strategy
from src.analytics.strategies.models import (
    EvaluatedLeg,
    StrategyEvaluationResponse,
    StrategyLegInput,
)
from src.analytics.walls import compute_walls
from src.core.exceptions import BacktestValidationError
from src.storage.backtest_store import DailyChainBar
from src.storage.schema import OPTION_CHAIN_POLARS_SCHEMA

DENOM_EPS: Final = 1e-9
TARGET_DTE_CENTER: Final = 38


@dataclass(frozen=True)
class BrokerFillAnchor:
    """Entry cash and fill-price anchor for replaying an already-opened position."""

    entry_date: str
    entry_ts_utc: str
    entry_snapshot_id: str
    entry_net_cash_dollars: float
    leg_entry_prices: Sequence[float | None] = ()


@dataclass(frozen=True)
class EntryBuildResult:
    """Validated entry legs, cash/risk baselines, and day-0 step evaluation."""

    legs: list[EvaluatedLeg]
    entry_cash_mid_dollars: float
    entry_cash_natural_dollars: float
    entry_basis_dollars: float
    max_profit_dollars: float | None
    max_loss_dollars: float | None
    max_loss_unbounded: bool
    initial_step: EvaluatedStepData
    selection_notes: list[str]
    flags: list[str]


@dataclass
class CadenceState:
    """Mutable cadence cursor updated strictly on past/current bars."""

    triggered_weeks: set[tuple[int, int]] = field(default_factory=set)
    last_attempt_bar_idx: int | None = None


@dataclass(frozen=True)
class ExitDecision:
    """Result of evaluating exit criteria for one open trade on the current bar."""

    should_exit: bool
    reason: ExitReason | None = None
    unresolved: bool = False
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _adjust_risk_bounds(
    ev: StrategyEvaluationResponse, entry_cash_mid_r: float
) -> tuple[float, float | None, bool]:
    delta_cash = entry_cash_mid_r - ev.pricing.mid_cash_total
    max_profit_dollars = round(max(0.0, ev.risk.max_profit + delta_cash), 2)
    if ev.risk.max_loss_unbounded:
        return max_profit_dollars, None, True
    return max_profit_dollars, round(max(0.0, ev.risk.max_loss - delta_cash), 2), False


def parse_anchor_ts_utc(raw: str) -> datetime:
    """Parse an ISO-8601 UTC timestamp string from `BrokerFillAnchor` into UTC `datetime`."""
    text = raw.strip()
    if not text:
        raise BacktestValidationError("broker_fill_anchor.entry_ts_utc must not be empty.")
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso_text)
    except ValueError as exc:
        raise BacktestValidationError(f"Invalid entry_ts_utc {raw!r}: {exc}") from exc
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def resolve_broker_leg_prices(
    legs: Sequence[StrategyLegInput],
    qty_multiplier: int,
    entry_net_cash_dollars: float,
    leg_entry_prices: Sequence[float | None],
) -> list[float]:
    """Resolve per-leg entry fill prices from explicit prices and total net entry cash."""
    if qty_multiplier <= 0:
        raise BacktestValidationError("qty_multiplier must be >= 1.")
    if not math.isfinite(entry_net_cash_dollars):
        raise BacktestValidationError("entry_net_cash_dollars must be finite.")
    if leg_entry_prices and len(leg_entry_prices) != len(legs):
        raise BacktestValidationError(
            f"leg_entry_prices length ({len(leg_entry_prices)}) must match legs ({len(legs)})."
        )
    resolved: list[float | None] = []
    known_cash = 0.0
    missing_indices: list[int] = []
    for idx, leg in enumerate(legs):
        raw_px = leg_entry_prices[idx] if idx < len(leg_entry_prices) else None
        if raw_px is not None and math.isfinite(raw_px) and raw_px >= 0.0:
            px = 0.0 if raw_px == 0.0 else float(raw_px)
            resolved.append(px)
            cash_sign = -1.0 if leg.side == "BUY" else 1.0
            known_cash += cash_sign * float(leg.qty * qty_multiplier) * 100.0 * px
        else:
            resolved.append(None)
            missing_indices.append(idx)

    if missing_indices:
        residual_cash = entry_net_cash_dollars - known_cash
        signed_shares = 0.0
        sell_shares = 0.0
        buy_shares = 0.0
        for idx in missing_indices:
            leg = legs[idx]
            shares = float(leg.qty * qty_multiplier) * 100.0
            if leg.side == "BUY":
                signed_shares -= shares
                buy_shares += shares
            else:
                signed_shares += shares
                sell_shares += shares

        uniform_px = residual_cash / signed_shares if abs(signed_shares) > DENOM_EPS else -1.0
        if uniform_px >= 0.0 and math.isfinite(uniform_px):
            clean_px = 0.0 if uniform_px == 0.0 else float(uniform_px)
            for idx in missing_indices:
                resolved[idx] = clean_px
        elif residual_cash >= 0.0 and sell_shares > DENOM_EPS:
            sell_px = residual_cash / sell_shares
            for idx in missing_indices:
                resolved[idx] = sell_px if legs[idx].side == "SELL" else 0.0
        elif residual_cash < 0.0 and buy_shares > DENOM_EPS:
            buy_px = (-residual_cash) / buy_shares
            for idx in missing_indices:
                resolved[idx] = buy_px if legs[idx].side == "BUY" else 0.0
        else:
            for idx in missing_indices:
                resolved[idx] = 0.0

    return [0.0 if px is None else float(px) for px in resolved]


def _resolve_anchor_underlying_and_spot(
    legs: Sequence[StrategyLegInput],
    anchor: BrokerFillAnchor,
    reference_bar: DailyChainBar | None,
) -> tuple[str, float]:
    underlying = ""
    spot: float | None = None
    if reference_bar is not None:
        spot = extract_bar_context(reference_bar).spot
        if reference_bar.df.height > 0:
            underlying = str(reference_bar.df["underlying"][0] or "").strip()
        if (
            (not underlying or spot is None or spot <= 0.0)
            and reference_bar.tracked_df is not None
            and reference_bar.tracked_df.height > 0
        ):
            if not underlying:
                underlying = str(reference_bar.tracked_df["underlying"][0] or "").strip()
            if spot is None or spot <= 0.0:
                spot = clean_positive_price(reference_bar.tracked_df["underlying_price"][0])
        if not underlying and "_" in reference_bar.snapshot_id:
            underlying = reference_bar.snapshot_id.split("_", 1)[0].strip()
    if not underlying and "_" in anchor.entry_snapshot_id:
        underlying = anchor.entry_snapshot_id.split("_", 1)[0].strip()
    if not underlying:
        underlying = "UNDERLYING"
    if spot is None or spot <= 0.0:
        spot = sum(float(leg.strike) for leg in legs) / float(len(legs))
    return underlying, float(spot)


def build_broker_fill_entry(
    legs: Sequence[StrategyLegInput],
    config: BacktestConfig,
    anchor: BrokerFillAnchor,
    reference_bar: DailyChainBar | None = None,
) -> EntryBuildResult:
    """Build an anchored Day-0 `EntryBuildResult` from recorded fill cash & leg prices."""
    if not legs:
        raise BacktestValidationError("At least one option leg is required.")
    if not anchor.entry_snapshot_id.strip():
        raise BacktestValidationError("broker_fill_anchor.entry_snapshot_id must not be empty.")
    if not math.isfinite(anchor.entry_net_cash_dollars):
        raise BacktestValidationError("broker_fill_anchor.entry_net_cash_dollars must be finite.")
    try:
        entry_day = date.fromisoformat(anchor.entry_date)
    except ValueError as exc:
        raise BacktestValidationError(f"Invalid entry_date {anchor.entry_date!r}: {exc}") from exc
    entry_dt = parse_anchor_ts_utc(anchor.entry_ts_utc)
    ts_utc_str = entry_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    resolved_prices = resolve_broker_leg_prices(
        legs, config.qty, anchor.entry_net_cash_dollars, anchor.leg_entry_prices
    )
    underlying, spot = _resolve_anchor_underlying_and_spot(legs, anchor, reference_bar)

    syn_rows: list[dict[str, object]] = []
    for idx, (leg, px) in enumerate(zip(legs, resolved_prices, strict=True), start=1):
        dte = max(0, (date.fromisoformat(leg.expiry) - entry_day).days)
        syn_rows.append(
            {
                "snapshot_id": anchor.entry_snapshot_id,
                "snapshot_ts_utc": ts_utc_str,
                "market_date": anchor.entry_date,
                "market_data_type": 3,
                "underlying": underlying,
                "underlying_conid": 1,
                "underlying_price": spot,
                "option_conid": idx,
                "expiry": leg.expiry,
                "dte": dte,
                "strike": float(leg.strike),
                "moneyness_pct": round(((float(leg.strike) - spot) / spot) * 100.0, 4),
                "right": leg.right,
                "bid": px,
                "ask": px,
                "mid": px,
                "last": px,
                "close": px,
                "mark_price": px,
                "volume": None,
                "open_interest": None,
                "iv": None,
                "delta": None,
                "gamma": None,
                "theta": None,
                "vega": None,
                "greeks_source": "NONE",
            }
        )
    syn_df = pl.DataFrame(syn_rows, schema=OPTION_CHAIN_POLARS_SCHEMA)

    entry_cash_mid_r = round(anchor.entry_net_cash_dollars, 2) or 0.0
    entry_cash_nat_r = entry_cash_mid_r
    entry_basis = round(abs(entry_cash_mid_r), 2) or 0.0
    anchor_note = f"Anchored to broker fill from {anchor.entry_snapshot_id} ({anchor.entry_date})."

    try:
        ev = evaluate_strategy(
            syn_df,
            strategy_kind=config.strategy_kind,
            legs=list(legs),
            qty_multiplier=config.qty,
            risk_free_rate=config.risk_free_rate,
        )
        max_profit_dollars, max_loss_dollars, max_loss_unbounded = _adjust_risk_bounds(
            ev, entry_cash_mid_r
        )
        eval_legs = [
            ev_leg.model_copy(
                update={
                    "bid": round(px, 4),
                    "ask": round(px, 4),
                    "mid": round(px, 4),
                    "natural_fill": round(px, 4),
                }
            )
            for ev_leg, px in zip(ev.legs, resolved_prices, strict=True)
        ]
        selection_notes = deduplicate_flags([anchor_note, *ev.rationale])
    except ValueError:
        max_profit_dollars = None
        max_loss_dollars = None
        max_loss_unbounded = False
        eval_legs = [
            EvaluatedLeg(
                side=leg.side,
                right=leg.right,
                expiry=leg.expiry,
                dte=max(0, (date.fromisoformat(leg.expiry) - entry_day).days),
                strike=float(leg.strike),
                qty=leg.qty * config.qty,
                bid=round(px, 4),
                ask=round(px, 4),
                mid=round(px, 4),
                natural_fill=round(px, 4),
                spread_pct=0.0 if px > 0.0 else None,
                iv=None,
                delta=None,
                gamma=None,
                theta=None,
                vega=None,
                open_interest=None,
                volume=None,
                option_conid=None,
                selection_reason=leg.selection_reason,
            )
            for leg, px in zip(legs, resolved_prices, strict=True)
        ]
        selection_notes = [anchor_note]

    leg_marks = [
        BacktestLegMark(
            side=leg.side,
            right=leg.right,
            expiry=leg.expiry,
            strike=leg.strike,
            qty=leg.qty,
            price_used=round(px, 4),
            natural_price=round(px, 4),
            price_source="BROKER_FILL",
            quote_source="BROKER_FILL",
            status="OK",
        )
        for leg, px in zip(eval_legs, resolved_prices, strict=True)
    ]
    dte_min_leg = min(max(0, (date.fromisoformat(leg.expiry) - entry_day).days) for leg in legs)
    close_val = round(-entry_cash_mid_r, 2) or 0.0
    initial_step = EvaluatedStepData(
        legs=leg_marks,
        dte_min_leg=dte_min_leg,
        mark_value_dollars=close_val,
        natural_close_value_dollars=close_val,
        active_close_value_dollars=close_val,
        all_legs_settled=False,
        any_leg_settled=False,
        settlement_blocked=False,
        unexpired_close_contracts=sum(leg.qty for leg in eval_legs),
        flags=[FLAG_BROKER_FILL_ENTRY],
        tracked_snapshot_ids=[],
        status="OK",
    )
    return EntryBuildResult(
        legs=eval_legs,
        entry_cash_mid_dollars=entry_cash_mid_r,
        entry_cash_natural_dollars=entry_cash_nat_r,
        entry_basis_dollars=entry_basis,
        max_profit_dollars=max_profit_dollars,
        max_loss_dollars=max_loss_dollars,
        max_loss_unbounded=max_loss_unbounded,
        initial_step=initial_step,
        selection_notes=selection_notes,
        flags=[FLAG_BROKER_FILL_ENTRY],
    )


def _pick_front_expiry(df: pl.DataFrame, *, target_dte_min: int, target_dte_max: int) -> str:
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


def _fail_or_skip(
    msg: str, *, raise_on_error: bool, skip_reason: str
) -> tuple[None, list[str], str]:
    if raise_on_error:
        raise BacktestValidationError(msg)
    return None, [msg], skip_reason


def build_entry_candidate(
    bar: DailyChainBar,
    config: BacktestConfig,
    *,
    legs: Sequence[StrategyLegInput] | None = None,
    raise_on_error: bool = False,
    ctx: BarContext | None = None,
) -> tuple[EntryBuildResult | None, list[str]]:
    """Construct and validate a new trade candidate on `bar` using contract-filtered BSM."""
    entry, notes, _ = build_entry_candidate_outcome(
        bar, config, legs=legs, raise_on_error=raise_on_error, ctx=ctx
    )
    return entry, notes


def build_entry_candidate_outcome(
    bar: DailyChainBar,
    config: BacktestConfig,
    *,
    legs: Sequence[StrategyLegInput] | None = None,
    raise_on_error: bool = False,
    ctx: BarContext | None = None,
) -> tuple[EntryBuildResult | None, list[str], str | None]:
    """Construct and validate a new trade candidate, returning `(entry, notes, skip_reason)`."""
    bar_ctx = ctx if ctx is not None else extract_bar_context(bar)
    if bar_ctx.spot is None or bar_ctx.spot <= 0.0:
        return _fail_or_skip(
            f"Skipped entry on {bar.market_date}: invalid underlying spot price.",
            raise_on_error=raise_on_error,
            skip_reason="INVALID_SPOT",
        )

    apply_bps_filters = legs is None and config.strategy_kind == "BULL_PUT_SPREAD"
    eff_min_credit = (
        config.min_entry_credit_dollars
        if (
            apply_bps_filters
            and config.min_entry_credit_dollars is not None
            and config.min_entry_credit_dollars > 0.0
        )
        else None
    )
    eff_min_ror = (
        config.min_entry_ror_pct
        if (
            apply_bps_filters
            and config.min_entry_ror_pct is not None
            and config.min_entry_ror_pct > 0.0
        )
        else None
    )

    eval_notes: list[str] = []
    if legs is not None:
        if not legs:
            raise BacktestValidationError("At least one option leg is required.")
        if any(is_leg_settled(leg.expiry, bar.ts_utc) for leg in legs):
            return _fail_or_skip(
                f"Cannot enter trade on {bar.market_date}: one or more legs are already expired.",
                raise_on_error=raise_on_error,
                skip_reason="ALREADY_SETTLED",
            )
        matched_raw, _ = lookup_legs_on_bar(
            bar,
            legs,
            price_basis=config.price_basis,
            close_price_mode=config.close_price_mode,
            risk_free_rate=config.risk_free_rate,
        )
        if len(matched_raw) < len(legs):
            missing_desc = ", ".join(
                f"{leg.expiry} {leg.strike:g}{leg.right}"
                for i, leg in enumerate(legs)
                if i not in matched_raw
            )
            return _fail_or_skip(
                f"Contract(s) not found on {bar.market_date}: {missing_desc}",
                raise_on_error=raise_on_error,
                skip_reason="MISSING_CONTRACT",
            )
        leg_rows = [dict(matched_raw[i][0]) for i in range(len(legs))]
        for row in leg_rows:
            row["underlying_price"] = bar_ctx.spot
        leg_df = pl.DataFrame(leg_rows, schema=OPTION_CHAIN_POLARS_SCHEMA)
        walls = compute_walls(bar.df, top_n=config.walls_top_n) if bar.df.height > 0 else None
        try:
            ev = evaluate_strategy(
                leg_df,
                strategy_kind=config.strategy_kind,
                legs=list(legs),
                qty_multiplier=config.qty,
                risk_free_rate=config.risk_free_rate,
                walls_report=walls,
            )
        except ValueError as exc:
            return _fail_or_skip(
                f"Strategy evaluation failed on {bar.market_date}: {exc}",
                raise_on_error=raise_on_error,
                skip_reason="STRATEGY_BUILD_ERROR",
            )
    else:
        if config.strategy_kind == "CUSTOM":
            raise BacktestValidationError("CUSTOM strategy requires explicit legs.")
        if bar.df.height == 0:
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: empty option chain.",
                raise_on_error=raise_on_error,
                skip_reason="EMPTY_CHAIN",
            )
        front_df = filter_df_for_dte_window(bar.df, config.target_dte_min, config.target_dte_max)
        if front_df.height == 0:
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: no contracts in "
                f"[{config.target_dte_min}, {config.target_dte_max}] DTE.",
                raise_on_error=raise_on_error,
                skip_reason="NO_EXPIRY_IN_DTE_WINDOW",
            )
        unsettled_front = [
            str(exp)
            for exp in front_df["expiry"].unique().to_list()
            if not is_leg_settled(str(exp), bar.ts_utc)
        ]
        if not unsettled_front:
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: "
                "selected expiry already reached settlement cutoff.",
                raise_on_error=raise_on_error,
                skip_reason="ALREADY_SETTLED",
            )
        front_df = front_df.filter(pl.col("expiry").is_in(unsettled_front))
        front_expiry = _pick_front_expiry(
            front_df,
            target_dte_min=config.target_dte_min,
            target_dte_max=config.target_dte_max,
        )
        if config.strategy_kind == "DIAGONAL_SPREAD":
            raw_scope = filter_df_for_dte_window(
                bar.df, config.target_dte_min, max(config.target_dte_max, 120)
            )
            unsettled_scope = [
                str(exp)
                for exp in raw_scope["expiry"].unique().to_list()
                if not is_leg_settled(str(exp), bar.ts_utc)
            ]
            scope_df = raw_scope.filter(pl.col("expiry").is_in(unsettled_scope))
        else:
            scope_df = front_df
        enriched_df = enrich_filtered_greeks(scope_df, risk_free_rate=config.risk_free_rate)
        walls = compute_walls(enriched_df, top_n=config.walls_top_n)
        try:
            ev = evaluate_strategy(
                enriched_df,
                strategy_kind=config.strategy_kind,
                target_expiry=front_expiry,
                qty_multiplier=config.qty,
                risk_free_rate=config.risk_free_rate,
                walls_report=walls,
                min_net_credit_dollars=eff_min_credit,
                min_ror_pct=eff_min_ror,
                entry_price_basis=config.price_basis,
            )
        except StrategyFilterError as exc:
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: {exc}",
                raise_on_error=raise_on_error,
                skip_reason=exc.skip_reason,
            )
        except ValueError as exc:
            reason = (
                "INSUFFICIENT_WINGS"
                if "Insufficient wing strikes" in str(exc)
                else "STRATEGY_BUILD_ERROR"
            )
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: {exc}",
                raise_on_error=raise_on_error,
                skip_reason=reason,
            )

    eval_notes.extend(ev.rationale)

    if any(is_leg_settled(leg.expiry, bar.ts_utc) for leg in ev.legs):
        return _fail_or_skip(
            f"Skipped entry on {bar.market_date}: "
            "selected expiry already reached settlement cutoff.",
            raise_on_error=raise_on_error,
            skip_reason="ALREADY_SETTLED",
        )

    entry_rows, _ = lookup_legs_on_bar(
        bar,
        ev.legs,
        price_basis=config.price_basis,
        close_price_mode=config.close_price_mode,
        risk_free_rate=config.risk_free_rate,
    )
    entry_cash_mid = 0.0
    entry_cash_nat = 0.0
    updated_legs: list[EvaluatedLeg] = []
    entry_flags: list[str] = []

    for idx, leg in enumerate(ev.legs):
        found_entry = entry_rows.get(idx)
        if found_entry is None:
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: missing leg "
                f"{leg.expiry} {leg.strike:g}{leg.right}.",
                raise_on_error=raise_on_error,
                skip_reason="MISSING_CONTRACT",
            )
        raw_row, _ = found_entry
        res = resolve_row_prices(
            raw_row,
            side=leg.side,
            price_basis=config.price_basis,
            close_price_mode=config.close_price_mode,
        )
        if res.price_used is None or res.no_last_print:
            reason_flag = FLAG_NO_LAST_PRINT if res.no_last_print else FLAG_MISSING_PRICE
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: unusable price ({reason_flag}) for "
                f"{leg.expiry} {leg.strike:g}{leg.right}.",
                raise_on_error=raise_on_error,
                skip_reason="UNUSABLE_ENTRY_PRICE",
            )
        cash_sign = -1.0 if leg.side == "BUY" else 1.0
        entry_cash_mid += cash_sign * float(leg.qty) * 100.0 * res.price_used
        nat_open = res.natural_open_price if res.natural_open_price is not None else res.price_used
        entry_cash_nat += cash_sign * float(leg.qty) * 100.0 * nat_open
        if res.used_fallback:
            entry_flags.append("LEG_FALLBACK_PRICE")
        updated_legs.append(
            leg.model_copy(
                update={
                    "mid": round(res.price_used, 4),
                    "natural_fill": round(nat_open, 4),
                }
            )
        )

    entry_cash_mid_r = round(entry_cash_mid, 2)
    entry_cash_nat_r = round(entry_cash_nat, 2)
    entry_basis = round(abs(entry_cash_mid_r), 2)
    max_profit_dollars, max_loss_dollars, _ = _adjust_risk_bounds(ev, entry_cash_mid_r)

    if apply_bps_filters and (eff_min_credit is not None or eff_min_ror is not None):
        per_spread_credit = round(entry_cash_mid_r / float(config.qty), 2)
        if per_spread_credit <= 0.0:
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: non-positive spread credit "
                f"${per_spread_credit:.2f}.",
                raise_on_error=raise_on_error,
                skip_reason="NON_POSITIVE_CREDIT",
            )
        per_spread_loss = (
            round(max_loss_dollars / float(config.qty), 2) if max_loss_dollars is not None else None
        )
        if per_spread_loss is None or per_spread_loss <= 0.0:
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: non-positive spread max loss.",
                raise_on_error=raise_on_error,
                skip_reason="NON_POSITIVE_MAX_LOSS",
            )
        ror_pct = (per_spread_credit / per_spread_loss) * 100.0
        bad_credit = eff_min_credit is not None and per_spread_credit < eff_min_credit - 1e-9
        bad_ror = eff_min_ror is not None and ror_pct < eff_min_ror - 1e-9
        if bad_credit or bad_ror:
            skip_code = (
                "BELOW_MIN_CREDIT_AND_ROR"
                if (bad_credit and bad_ror)
                else ("BELOW_MIN_CREDIT" if bad_credit else "BELOW_MIN_ROR")
            )
            return _fail_or_skip(
                f"Skipped entry on {bar.market_date}: credit=${per_spread_credit:.2f}, "
                f"RoR={ror_pct:.1f}% below entry filter thresholds.",
                raise_on_error=raise_on_error,
                skip_reason=skip_code,
            )

    initial_step = evaluate_trade_step_on_bar(bar, updated_legs, config, ctx=bar_ctx)
    all_flags = deduplicate_flags([*entry_flags, *initial_step.flags])
    return (
        EntryBuildResult(
            legs=updated_legs,
            entry_cash_mid_dollars=entry_cash_mid_r,
            entry_cash_natural_dollars=entry_cash_nat_r,
            entry_basis_dollars=entry_basis,
            max_profit_dollars=max_profit_dollars,
            max_loss_dollars=max_loss_dollars,
            max_loss_unbounded=ev.risk.max_loss_unbounded,
            initial_step=initial_step,
            selection_notes=deduplicate_flags(eval_notes),
            flags=all_flags,
        ),
        [],
        None,
    )


def should_attempt_entry(
    bar: DailyChainBar,
    bar_idx: int,
    config: BacktestConfig,
    open_count: int,
    state: CadenceState,
) -> bool:
    """Evaluate whether `entry_cadence` triggers an entry attempt on `bar`."""
    if open_count >= config.max_open_trades:
        return False

    if config.entry_cadence == "WHEN_FLAT":
        return open_count == 0

    if config.entry_cadence == "EVERY_N_DAYS":
        if state.last_attempt_bar_idx is None:
            return True
        return (bar_idx - state.last_attempt_bar_idx) >= config.entry_every_n_days

    bar_day = date.fromisoformat(bar.market_date)
    iso_year, iso_week, _ = bar_day.isocalendar()
    week_key = (iso_year, iso_week)
    if week_key in state.triggered_weeks:
        return False

    target = date.fromisocalendar(iso_year, iso_week, config.entry_weekday + 1)
    return bar_day >= target


def record_entry_attempt(bar: DailyChainBar, bar_idx: int, state: CadenceState) -> None:
    """Update cadence state once an entry attempt executes on `bar`."""
    bar_day = date.fromisoformat(bar.market_date)
    iso_year, iso_week, _ = bar_day.isocalendar()
    state.triggered_weeks.add((iso_year, iso_week))
    state.last_attempt_bar_idx = bar_idx


def resolve_tp_sl_thresholds(
    config: BacktestConfig,
    *,
    entry_basis_dollars: float,
    max_loss_dollars: float | None,
    max_loss_unbounded: bool,
) -> tuple[float | None, float | None, float | None, list[str], list[str]]:
    """Return `(denom, profit_target_dollars, stop_loss_dollars, skip_flags, skip_notes)`."""
    if config.tp_sl_basis == "CREDIT_DEBIT_PCT":
        if entry_basis_dollars <= DENOM_EPS:
            return (
                None,
                None,
                None,
                ["ZERO_ENTRY_BASIS"],
                ["TP/SL skipped: entry basis is zero under CREDIT_DEBIT_PCT."],
            )
        denom = entry_basis_dollars
    else:
        if max_loss_unbounded or max_loss_dollars is None or max_loss_dollars <= DENOM_EPS:
            return (
                None,
                None,
                None,
                ["MAX_RISK_UNBOUNDED_SKIP_TPSL"],
                ["TP/SL skipped: max loss is unbounded or zero under MAX_RISK_PCT."],
            )
        denom = max_loss_dollars

    tp_dollars = (
        round(denom * (config.profit_target_pct / 100.0), 2)
        if config.profit_target_pct is not None
        else None
    )
    sl_dollars = (
        -round(denom * (config.stop_loss_pct / 100.0), 2)
        if config.stop_loss_pct is not None
        else None
    )
    return denom, tp_dollars, sl_dollars, [], []


def evaluate_exit_rule(
    step: EvaluatedStepData,
    config: BacktestConfig,
    *,
    entry_cash_mid_dollars: float,
    entry_basis_dollars: float,
    max_loss_dollars: float | None,
    max_loss_unbounded: bool,
) -> ExitDecision:
    """Evaluate `EXPIRY_SETTLED`, `EXIT_DTE`, `PROFIT_TARGET`, and `STOP_LOSS` in priority order."""
    if step.any_leg_settled:
        if step.settlement_blocked or step.active_close_value_dollars is None:
            return ExitDecision(
                should_exit=True,
                reason="EXPIRY_SETTLED",
                unresolved=True,
                notes=[
                    "Trade kept UNRESOLVED at expiry due to missing non-CLOSE spot or leg price."
                ],
            )
        return ExitDecision(should_exit=True, reason="EXPIRY_SETTLED", unresolved=False)

    if step.active_close_value_dollars is None:
        flags = (
            ["EXIT_SKIPPED_MISSING_PRICE"]
            if (config.exit_dte is not None and step.dte_min_leg <= config.exit_dte)
            else []
        )
        return ExitDecision(should_exit=False, flags=flags)

    if config.exit_dte is not None and step.dte_min_leg <= config.exit_dte:
        return ExitDecision(should_exit=True, reason="EXIT_DTE")

    gross_pnl = entry_cash_mid_dollars + step.active_close_value_dollars
    denom, _tp_dollars, _sl_dollars, skip_flags, skip_notes = resolve_tp_sl_thresholds(
        config,
        entry_basis_dollars=entry_basis_dollars,
        max_loss_dollars=max_loss_dollars,
        max_loss_unbounded=max_loss_unbounded,
    )
    if denom is None:
        return ExitDecision(should_exit=False, flags=skip_flags, notes=skip_notes)

    if (
        config.profit_target_pct is not None
        and (gross_pnl / denom) * 100.0 >= config.profit_target_pct
    ):
        return ExitDecision(should_exit=True, reason="PROFIT_TARGET")
    if config.stop_loss_pct is not None and (-gross_pnl / denom) * 100.0 >= config.stop_loss_pct:
        return ExitDecision(should_exit=True, reason="STOP_LOSS")
    return ExitDecision(should_exit=False)
