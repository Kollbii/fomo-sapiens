"""Pure quote helpers shared by corridor selection and quote-frame building."""

from __future__ import annotations

import math

from src.storage.schema import (
    GreeksSource,
)


def opt_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        num = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def opt_int(val: object) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return None
        return int(val)
    if isinstance(val, str):
        try:
            return int(val.strip())
        except ValueError:
            return None
    return None


def compute_mid(
    bid: float | None,
    ask: float | None,
    last: float | None,
    close: float | None,
    fallback_mid: float | None = None,
) -> float | None:
    if bid is not None and ask is not None and bid > 0.0 and ask >= bid:
        return (bid + ask) / 2.0
    for candidate in (last, close, bid, ask, fallback_mid):
        if candidate is not None and candidate > 0.0:
            return candidate
    return None


def compute_spread_pct(bid: float | None, ask: float | None, mid: float | None) -> float | None:
    if bid is None or ask is None or mid is None or bid <= 0.0 or ask < bid or mid <= 0.0:
        return None
    return round(((ask - bid) / mid) * 100.0, 2)


def ib_expiry(iso_expiry: str) -> str:
    clean = iso_expiry.strip()
    if len(clean) == 10 and clean[4] == "-" and clean[7] == "-":
        return clean.replace("-", "")
    return clean[:8]


def normalize_greeks_source(raw: object) -> GreeksSource:
    text = str(raw or "NONE").upper()
    if text == "IBKR_MODEL":
        return "IBKR_MODEL"
    if text == "BSM_FALLBACK":
        return "BSM_FALLBACK"
    return "NONE"
