"""Cached option-chain rows prepared for strategy construction and pricing (pure)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import polars as pl

from src.analytics.strategies.models import LegSide
from src.storage.schema import OptionRight

EntryPriceBasis = Literal["MID", "NATURAL", "AUTO_MID_LAST_CLOSE", "LAST_CLOSE_ONLY"]


@dataclass(frozen=True)
class PreparedRow:
    expiry: str
    dte: int
    strike: float
    right: OptionRight
    bid: float | None
    ask: float | None
    mid: float
    spread_pct: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    open_interest: int | None
    volume: int | None
    option_conid: int | None
    last: float | None = None
    close: float | None = None


# ------------------------------------------------------------------ Row & Quote Helpers
def opt_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        out = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def opt_int(val: object) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, (float, str)):
        try:
            return int(val)
        except ValueError:
            return None
    return None


def resolve_row_mid(raw: dict[str, object]) -> float:
    bid = opt_float(raw.get("bid"))
    ask = opt_float(raw.get("ask"))
    if bid is not None and ask is not None and bid > 0.0 and ask >= bid:
        return (bid + ask) / 2.0
    for key in ("mid", "mark_price", "last", "close"):
        val = opt_float(raw.get(key))
        if val is not None and val > 0.0:
            return val
    return 0.0


def prepare_rows(df: pl.DataFrame) -> tuple[str, float, list[PreparedRow]]:
    if df.height == 0:
        raise ValueError("Cannot evaluate option strategy on an empty chain DataFrame")
    first = df.row(0, named=True)
    underlying = str(first["underlying"])
    spot = float(first["underlying_price"])
    if spot <= 0.0:
        raise ValueError(f"Invalid underlying_price {spot} for {underlying}")

    rows: list[PreparedRow] = []
    for raw in df.iter_rows(named=True):
        right_str = str(raw["right"]).upper()
        if right_str not in ("C", "P"):
            continue
        right_lit: OptionRight = "C" if right_str == "C" else "P"
        bid = opt_float(raw.get("bid"))
        ask = opt_float(raw.get("ask"))
        mid = resolve_row_mid(raw)
        spread_pct: float | None = None
        if bid is not None and ask is not None and bid > 0.0 and ask >= bid and mid > 0.0:
            spread_pct = ((ask - bid) / mid) * 100.0
        rows.append(
            PreparedRow(
                expiry=str(raw["expiry"]),
                dte=int(raw["dte"]),
                strike=float(raw["strike"]),
                right=right_lit,
                bid=bid,
                ask=ask,
                mid=mid,
                spread_pct=spread_pct,
                iv=opt_float(raw.get("iv")),
                delta=opt_float(raw.get("delta")),
                gamma=opt_float(raw.get("gamma")),
                theta=opt_float(raw.get("theta")),
                vega=opt_float(raw.get("vega")),
                open_interest=opt_int(raw.get("open_interest")),
                volume=opt_int(raw.get("volume")),
                option_conid=opt_int(raw.get("option_conid")),
                last=opt_float(raw.get("last")),
                close=opt_float(raw.get("close")),
            )
        )
    return underlying, spot, rows


def _positive_price(val: float | None) -> float | None:
    if val is None or math.isnan(val) or math.isinf(val) or val <= 0.0:
        return None
    return val


def resolve_entry_leg_price(
    row: PreparedRow,
    side: LegSide,
    basis: EntryPriceBasis = "MID",
) -> float | None:
    """Resolve leg entry price per share matching Strategy Lab or backtest entry price basis."""
    if basis == "MID":
        return row.mid if row.mid > 0.0 else None

    bid = _positive_price(row.bid)
    ask = _positive_price(row.ask)
    if basis == "NATURAL":
        side_px = bid if side == "SELL" else ask
        if side_px is not None:
            return side_px
        return row.mid if row.mid > 0.0 else None

    last = _positive_price(row.last)
    close = _positive_price(row.close)
    if basis == "LAST_CLOSE_ONLY":
        return last if last is not None else close

    # AUTO_MID_LAST_CLOSE: mirrors backtest pricing.resolve_row_prices exactly
    if bid is not None and ask is not None and ask >= bid:
        return (bid + ask) / 2.0
    return last if last is not None else close


def row_lookup_map(rows: list[PreparedRow]) -> dict[tuple[str, float, OptionRight], PreparedRow]:
    mapping: dict[tuple[str, float, OptionRight], PreparedRow] = {}
    for r in rows:
        mapping[(r.expiry, round(r.strike, 6), r.right)] = r
    return mapping


def find_row(
    lookup: dict[tuple[str, float, OptionRight], PreparedRow],
    expiry: str,
    strike: float,
    right: OptionRight,
) -> PreparedRow:
    exact = lookup.get((expiry, round(strike, 6), right))
    if exact is not None:
        return exact
    for (exp_k, k_val, r_val), row in lookup.items():
        if exp_k == expiry and r_val == right and abs(k_val - strike) < 1e-4:
            return row
    raise ValueError(f"Contract not found in loaded snapshot: {expiry} {strike:g}{right}")


def chain_reference_iv(rows: list[PreparedRow], spot: float, target_expiry: str) -> float:
    same_exp = [
        r for r in rows if r.expiry == target_expiry and r.iv is not None and 0.01 <= r.iv <= 5.0
    ]
    pool = same_exp or [r for r in rows if r.iv is not None and 0.01 <= r.iv <= 5.0]
    if not pool:
        return 0.35
    near_atm = sorted(
        pool,
        key=lambda r: (abs(r.strike - spot), r.spread_pct if r.spread_pct is not None else 999.0),
    )
    return float(near_atm[0].iv) if near_atm[0].iv is not None else 0.35


# ------------------------------------------------------------------ Wall-Anchored Auto-Builder
def liquidity_score(row: PreparedRow) -> float:
    if row.mid <= 0.01:
        return -100.0
    spread_pen = min((row.spread_pct if row.spread_pct is not None else 25.0), 100.0)
    spread_bonus = max(0.0, 35.0 - spread_pen)
    activity = math.log1p(max(row.open_interest or 0, 0) + max(row.volume or 0, 0)) * 4.5
    return spread_bonus + activity
