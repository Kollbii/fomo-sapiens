"""Deterministic, I/O-free Wall Strike Corridor contract selection (`<= 90` contracts)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import polars as pl

from src.broker.corridor.models import (
    CORRIDOR_MAX_CONTRACTS,
    DEFAULT_CORRIDOR_MAX_CONTRACTS,
    DEFAULT_WINDOW_STRIKES,
    CorridorContractSpec,
    CorridorLegRef,
    CorridorSelection,
    CorridorZone,
    WallAnchors,
)
from src.broker.corridor.quotes import (
    compute_mid,
    ib_expiry,
    normalize_greeks_source,
    opt_float,
    opt_int,
)
from src.core.exceptions import (
    OptionChainFetchError,
    PacingViolationError,
)
from src.core.security import TickerValidator
from src.storage.schema import (
    OptionRight,
    sanitize_price,
)


def _nearest_index(sorted_strikes: Sequence[float], anchor: float) -> int:
    return min(
        range(len(sorted_strikes)),
        key=lambda idx: (abs(sorted_strikes[idx] - anchor), sorted_strikes[idx]),
    )


def _classify_zone(
    idx: int | None,
    pw_window: set[int],
    cw_window: set[int],
    *,
    is_leg: bool,
) -> CorridorZone:
    if idx is None:
        return "STRATEGY_LEG"
    in_pw = idx in pw_window
    in_cw = idx in cw_window
    if in_pw and in_cw:
        return "BOTH_WALLS"
    if in_pw:
        return "PUT_WALL"
    if in_cw:
        return "CALL_WALL"
    if is_leg:
        return "STRATEGY_LEG"
    return "ATM"


def _select_secondary_expiry(
    exps: Sequence[tuple[str, int]],
    resolved_expiry: str,
    resolved_dte: int,
    wall_anchors: WallAnchors,
) -> str | None:
    other_exps = [(exp, dte) for exp, dte in exps if exp != resolved_expiry]
    if not other_exps:
        return None
    if wall_anchors.by_expiry:
        in_anchors = [item for item in other_exps if item[0] in wall_anchors.by_expiry]
        if in_anchors:
            other_exps = in_anchors
    sec_expiry, _ = min(
        other_exps,
        key=lambda item: (
            0 if item[1] > resolved_dte else 1,
            abs(item[1] - resolved_dte),
            item[1],
            item[0],
        ),
    )
    return sec_expiry


def _row_to_spec(
    raw: dict[str, Any],
    *,
    symbol: str,
    zone: CorridorZone,
    is_put_wall: bool,
    is_call_wall: bool,
    is_strategy_leg: bool,
) -> CorridorContractSpec:
    exp_iso = str(raw["expiry"])
    right_lit: OptionRight = "C" if str(raw["right"]).upper() == "C" else "P"
    bid = sanitize_price(raw.get("bid"))
    ask = sanitize_price(raw.get("ask"))
    last = sanitize_price(raw.get("last"))
    close = sanitize_price(raw.get("close"))
    cached_mid = compute_mid(bid, ask, last, close, sanitize_price(raw.get("mid")))
    return CorridorContractSpec(
        option_conid=opt_int(raw.get("option_conid")) or 0,
        symbol=symbol,
        expiry=exp_iso,
        ib_expiry=ib_expiry(exp_iso),
        dte=int(raw["dte"]),
        strike=float(raw["strike"]),
        right=right_lit,
        zone=zone,
        is_put_wall=is_put_wall,
        is_call_wall=is_call_wall,
        is_strategy_leg=is_strategy_leg,
        cached_bid=bid,
        cached_ask=ask,
        cached_mid=cached_mid,
        cached_last=last,
        cached_close=close,
        cached_volume=opt_int(raw.get("volume")),
        cached_open_interest=opt_int(raw.get("open_interest")),
        cached_iv=opt_float(raw.get("iv")),
        cached_delta=opt_float(raw.get("delta")),
        cached_gamma=opt_float(raw.get("gamma")),
        cached_theta=opt_float(raw.get("theta")),
        cached_vega=opt_float(raw.get("vega")),
        cached_greeks_source=normalize_greeks_source(raw.get("greeks_source")),
    )


def _collect_expiry_candidates(
    df_exp: pl.DataFrame,
    *,
    expiry: str,
    symbol: str,
    spot: float,
    put_wall: float | None,
    call_wall: float | None,
    half_win: int,
    wall_tier: int,
    include_opposite_right: bool,
    include_atm_fill: bool,
    priority_keys: set[tuple[str, float, str]],
    seen_keys: set[tuple[str, float, str]],
    ranked_candidates: list[tuple[tuple[int, int, float, float, int], CorridorContractSpec]],
) -> None:
    avail_strikes = sorted({float(s) for s in df_exp["strike"].to_list() if float(s) > 0.0})
    if not avail_strikes:
        return

    atm_strike = min(avail_strikes, key=lambda s: (abs(s - spot), s))
    pw_anchor = put_wall if put_wall is not None else spot
    cw_anchor = call_wall if call_wall is not None else spot

    pw_idx = _nearest_index(avail_strikes, pw_anchor)
    cw_idx = _nearest_index(avail_strikes, cw_anchor)
    atm_idx = _nearest_index(avail_strikes, atm_strike)
    idx_by_strike = {round(s, 6): i for i, s in enumerate(avail_strikes)}

    pw_window = set(
        range(max(0, pw_idx - half_win), min(len(avail_strikes), pw_idx + half_win + 1))
    )
    cw_window = set(
        range(max(0, cw_idx - half_win), min(len(avail_strikes), cw_idx + half_win + 1))
    )

    for row in df_exp.iter_rows(named=True):
        right_u = str(row["right"]).upper()
        if right_u not in ("C", "P"):
            continue
        strike_val = float(row["strike"])
        key = (expiry, round(strike_val, 6), right_u)
        if key in seen_keys:
            continue
        s_idx = idx_by_strike.get(round(strike_val, 6))
        if s_idx is None:
            continue

        in_pw = s_idx in pw_window
        in_cw = s_idx in cw_window
        if in_pw or in_cw:
            if not include_opposite_right and not (
                (in_pw and right_u == "P") or (in_cw and right_u == "C")
            ):
                continue
            tier = wall_tier
            d_pw = abs(s_idx - pw_idx) if in_pw else 999
            d_cw = abs(s_idx - cw_idx) if in_cw else 999
            wall_dist = min(d_pw, d_cw)
        elif include_atm_fill:
            tier = 3
            wall_dist = abs(s_idx - atm_idx)
        else:
            continue

        seen_keys.add(key)
        is_pw = put_wall is not None and abs(strike_val - put_wall) < 1e-4 and right_u == "P"
        is_cw = call_wall is not None and abs(strike_val - call_wall) < 1e-4 and right_u == "C"
        is_leg = key in priority_keys
        spec = _row_to_spec(
            row,
            symbol=symbol,
            zone=_classify_zone(s_idx, pw_window, cw_window, is_leg=is_leg),
            is_put_wall=is_pw,
            is_call_wall=is_cw,
            is_strategy_leg=is_leg,
        )
        right_ord = 0 if right_u == "P" else 1
        ranked_candidates.append(
            ((tier, wall_dist, abs(strike_val - spot), strike_val, right_ord), spec)
        )


def select_wall_corridor(
    df: pl.DataFrame,
    *,
    wall_anchors: WallAnchors,
    target_expiry: str | None = None,
    window_strikes: int = DEFAULT_WINDOW_STRIKES,
    max_contracts: int = DEFAULT_CORRIDOR_MAX_CONTRACTS,
    priority_legs: Sequence[CorridorLegRef] = (),
    include_opposite_right: bool = True,
) -> CorridorSelection:
    """Select `±window_strikes` around Primary Put & Call Walls, strictly `<= 90` contracts.

    Priority tiers inside the contract budget:
      0. Active Strategy Lab legs (even if on a secondary DTE, e.g. Diagonal spreads)
      1. Primary DTE Put Wall `±window_strikes` & Call Wall `±window_strikes` (`("P", "C")`)
      2. Secondary DTE Put/Call Wall `±min(2, window_strikes)` (`("P", "C")`)
      3. Primary DTE ATM fill outward (`("P", "C")`) until `max_contracts` is reached
    """
    if (
        isinstance(max_contracts, bool)
        or int(max_contracts) <= 0
        or int(max_contracts) > CORRIDOR_MAX_CONTRACTS
    ):
        raise PacingViolationError(
            f"Corridor max_contracts={max_contracts} must be between 1 and {CORRIDOR_MAX_CONTRACTS}"
        )
    if df.height == 0:
        raise OptionChainFetchError("Cannot select Wall Corridor from an empty chain DataFrame")

    eff_win = min(max(1, int(window_strikes)), 15)
    eff_max = int(max_contracts)

    first = df.row(0, named=True)
    symbol = TickerValidator.validate(str(first["underlying"]))
    underlying_conid = int(first["underlying_conid"])
    spot = float(first["underlying_price"])
    if spot <= 0.0:
        raise OptionChainFetchError(f"Invalid spot price {spot} for {symbol}")

    exp_rows = df.select(["expiry", "dte"]).unique().sort(["dte", "expiry"]).iter_rows(named=True)
    exps = [(str(r["expiry"]), int(r["dte"])) for r in exp_rows]
    if not exps:
        raise OptionChainFetchError(f"No expirations found for {symbol}")

    exp_dte_map = dict(exps)
    if target_expiry and target_expiry in exp_dte_map:
        resolved_expiry = target_expiry
        resolved_dte = exp_dte_map[target_expiry]
    else:
        resolved_expiry, resolved_dte = exps[0]

    put_wall, call_wall = wall_anchors.resolve(resolved_expiry)

    df_exp = df.filter(pl.col("expiry") == resolved_expiry).sort(["strike", "right"])
    if df_exp.height == 0:
        raise OptionChainFetchError(f"No contracts available for {symbol} {resolved_expiry}")

    avail_strikes = sorted({float(s) for s in df_exp["strike"].to_list() if float(s) > 0.0})
    if not avail_strikes:
        raise OptionChainFetchError(f"No positive strikes available for {symbol} {resolved_expiry}")

    atm_strike = min(avail_strikes, key=lambda s: (abs(s - spot), s))
    pw_anchor = put_wall if put_wall is not None else spot
    cw_anchor = call_wall if call_wall is not None else spot

    pw_idx = _nearest_index(avail_strikes, pw_anchor)
    cw_idx = _nearest_index(avail_strikes, cw_anchor)
    idx_by_strike = {round(s, 6): i for i, s in enumerate(avail_strikes)}

    pw_window = set(range(max(0, pw_idx - eff_win), min(len(avail_strikes), pw_idx + eff_win + 1)))
    cw_window = set(range(max(0, cw_idx - eff_win), min(len(avail_strikes), cw_idx + eff_win + 1)))

    priority_keys: set[tuple[str, float, str]] = {
        (leg.expiry, round(float(leg.strike), 6), str(leg.right)) for leg in priority_legs
    }
    all_rows_by_key: dict[tuple[str, float, str], dict[str, Any]] = {}
    for row in df.iter_rows(named=True):
        right_u = str(row["right"]).upper()
        if right_u not in ("C", "P"):
            continue
        key = (str(row["expiry"]), round(float(row["strike"]), 6), right_u)
        all_rows_by_key[key] = row

    ranked_candidates: list[tuple[tuple[int, int, float, float, int], CorridorContractSpec]] = []
    seen_keys: set[tuple[str, float, str]] = set()

    # Tier 0: explicitly prioritized Strategy Lab legs (even across secondary DTEs)
    for leg in priority_legs:
        key = (leg.expiry, round(float(leg.strike), 6), leg.right)
        if key in seen_keys:
            continue
        raw_match = all_rows_by_key.get(key)
        if raw_match is None:
            continue
        seen_keys.add(key)
        s_idx = (
            idx_by_strike.get(round(float(raw_match["strike"]), 6))
            if key[0] == resolved_expiry
            else None
        )
        zone = _classify_zone(s_idx, pw_window, cw_window, is_leg=True)
        leg_pw, leg_cw = wall_anchors.resolve(key[0])
        is_pw = leg_pw is not None and abs(float(raw_match["strike"]) - leg_pw) < 1e-4
        is_cw = leg_cw is not None and abs(float(raw_match["strike"]) - leg_cw) < 1e-4
        spec = _row_to_spec(
            raw_match,
            symbol=symbol,
            zone=zone,
            is_put_wall=is_pw and leg.right == "P",
            is_call_wall=is_cw and leg.right == "C",
            is_strategy_leg=True,
        )
        right_ord = 0 if spec.right == "P" else 1
        ranked_candidates.append(((0, 0, abs(spec.strike - spot), spec.strike, right_ord), spec))

    # Tier 1 (Primary DTE Put/Call Wall windows) + Tier 3 (Primary DTE ATM fill outward)
    _collect_expiry_candidates(
        df_exp,
        expiry=resolved_expiry,
        symbol=symbol,
        spot=spot,
        put_wall=put_wall,
        call_wall=call_wall,
        half_win=eff_win,
        wall_tier=1,
        include_opposite_right=include_opposite_right,
        include_atm_fill=True,
        priority_keys=priority_keys,
        seen_keys=seen_keys,
        ranked_candidates=ranked_candidates,
    )

    # Tier 2: Secondary DTE Put/Call Wall ±min(2, window_strikes)
    sec_expiry = _select_secondary_expiry(exps, resolved_expiry, resolved_dte, wall_anchors)
    if sec_expiry is not None:
        df_sec = df.filter(pl.col("expiry") == sec_expiry).sort(["strike", "right"])
        if df_sec.height > 0:
            sec_pw, sec_cw = wall_anchors.resolve(sec_expiry)
            _collect_expiry_candidates(
                df_sec,
                expiry=sec_expiry,
                symbol=symbol,
                spot=spot,
                put_wall=sec_pw,
                call_wall=sec_cw,
                half_win=min(2, eff_win),
                wall_tier=2,
                include_opposite_right=include_opposite_right,
                include_atm_fill=False,
                priority_keys=priority_keys,
                seen_keys=seen_keys,
                ranked_candidates=ranked_candidates,
            )

    ranked_candidates.sort(key=lambda pair: pair[0])
    chosen = [spec for _, spec in ranked_candidates[:eff_max]]
    chosen.sort(key=lambda s: (s.dte, s.strike, s.right))

    return CorridorSelection(
        ticker=symbol,
        underlying_conid=underlying_conid,
        underlying_price=spot,
        target_expiry=resolved_expiry,
        target_dte=resolved_dte,
        window_strikes=eff_win,
        max_contracts=eff_max,
        primary_put_wall=put_wall,
        primary_call_wall=call_wall,
        wall_basis=wall_anchors.basis,
        atm_strike=atm_strike,
        specs=tuple(chosen),
    )
