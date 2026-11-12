"""Targeted Wall Strike Corridor (±k strikes, <= 60 contracts) & Live Constant Streaming."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import numpy as np
import numpy.typing as npt
import polars as pl
from ib_async import Option, Stock
from pydantic import BaseModel, ConfigDict, Field

from src.broker.client import IBKRBrokerClient, SleepFn, market_data_type_label, poll_until
from src.broker.contracts import extract_spot
from src.broker.fetcher import (
    DELAYED_GENERIC_TICKS,
    EMPTY_QUOTE_FALLBACK_THRESHOLD,
    LIVE_OPRA_GENERIC_TICKS,
    empty_quote_ratio,
)
from src.broker.parser import extract_model_greeks, extract_open_interest, has_usable_quote
from src.core.config import Settings
from src.core.exceptions import (
    GatewayConnectionError,
    OptionChainFetchError,
    PacingViolationError,
)
from src.core.security import TickerValidator
from src.storage.schema import (
    GreeksSource,
    OptionRight,
    WallBasis,
    sanitize_count,
    sanitize_price,
)

logger = logging.getLogger(__name__)

CORRIDOR_MAX_CONTRACTS = 60
DEFAULT_CORRIDOR_MAX_CONTRACTS = 56
DEFAULT_WINDOW_STRIKES = 5

CorridorZone = Literal["PUT_WALL", "CALL_WALL", "BOTH_WALLS", "ATM", "STRATEGY_LEG"]
FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]


class CorridorLegRef(BaseModel):
    """Reference to an open Strategy Lab leg prioritized inside the Wall Corridor."""

    model_config = ConfigDict(extra="forbid")

    expiry: str
    strike: float = Field(gt=0.0)
    right: OptionRight


class CorridorQuoteRow(BaseModel):
    """Single option quote inside a Wall Strike Corridor snapshot or stream frame."""

    model_config = ConfigDict(extra="forbid")

    option_conid: int
    expiry: str
    dte: int
    strike: float
    right: OptionRight
    zone: CorridorZone
    is_put_wall: bool = False
    is_call_wall: bool = False
    is_strategy_leg: bool = False
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    last: float | None = None
    close: float | None = None
    spread_pct: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    greeks_source: GreeksSource = "NONE"
    updated_ts_utc: str


class CorridorQuoteFrame(BaseModel):
    """Full Wall Corridor quote frame returned by one-shot pull or streamed via SSE."""

    model_config = ConfigDict(extra="forbid")

    ticker: str
    snapshot_id: str
    target_expiry: str
    target_dte: int
    window_strikes: int
    max_contracts: int
    subscribed_count: int
    subscription_cap: int = CORRIDOR_MAX_CONTRACTS
    underlying_price: float
    market_data_type: int
    primary_call_wall: float | None
    primary_put_wall: float | None
    wall_basis: WallBasis
    updated_ts_utc: str
    sequence: int = 1
    mode: Literal["ONE_SHOT", "STREAM"] = "ONE_SHOT"
    elapsed_ms: float | None = None
    changed_keys: list[str] = Field(default_factory=list)
    quotes: list[CorridorQuoteRow]
    notices: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class CorridorContractSpec:
    """Selected contract inside the Wall Corridor plus cached fallback attributes."""

    option_conid: int
    symbol: str
    expiry: str
    ib_expiry: str
    dte: int
    strike: float
    right: OptionRight
    zone: CorridorZone
    is_put_wall: bool
    is_call_wall: bool
    is_strategy_leg: bool
    cached_bid: float | None
    cached_ask: float | None
    cached_mid: float | None
    cached_last: float | None
    cached_close: float | None
    cached_volume: int | None
    cached_open_interest: int | None
    cached_iv: float | None
    cached_delta: float | None
    cached_gamma: float | None
    cached_theta: float | None
    cached_vega: float | None
    cached_greeks_source: GreeksSource


@dataclass(frozen=True)
class CorridorSelection:
    """Deterministic Wall Strike Corridor contract set ready for broker subscription."""

    ticker: str
    underlying_conid: int
    underlying_price: float
    target_expiry: str
    target_dte: int
    window_strikes: int
    max_contracts: int
    primary_put_wall: float | None
    primary_call_wall: float | None
    wall_basis: WallBasis
    atm_strike: float
    specs: tuple[CorridorContractSpec, ...]


@dataclass(frozen=True)
class WallAnchors:
    """Primary Put/Call Wall strikes the corridor is centred on (computed by the caller).

    The broker layer never computes walls itself; `src/services/corridor.py` derives anchors
    from the analytics walls report. `by_expiry` maps ISO expiry -> `(put_wall, call_wall)`;
    missing sides fall back to the aggregate walls.
    """

    basis: WallBasis
    aggregate_put: float | None = None
    aggregate_call: float | None = None
    by_expiry: Mapping[str, tuple[float | None, float | None]] = field(default_factory=dict)

    def resolve(self, expiry: str) -> tuple[float | None, float | None]:
        put_wall, call_wall = self.by_expiry.get(expiry, (None, None))
        return (
            put_wall if put_wall is not None else self.aggregate_put,
            call_wall if call_wall is not None else self.aggregate_call,
        )


class GreeksFallback(Protocol):
    """Computes Greeks for quotes lacking IBKR `modelGreeks` (injected; e.g. vectorized BSM).

    Returns `(iv, delta, gamma, theta, vega, usable)`; rows with `usable == False` keep their
    cached snapshot Greeks.
    """

    def __call__(
        self,
        *,
        mark_price: FloatArray,
        spot: FloatArray,
        strike: FloatArray,
        dte: FloatArray,
        is_call: BoolArray,
        fallback_iv: FloatArray,
    ) -> tuple[FloatArray, FloatArray, FloatArray, FloatArray, FloatArray, BoolArray]: ...


@dataclass
class _ActiveCorridorSession:
    session_id: str
    ib: Any
    subscribed_contracts: list[Any] = field(default_factory=list)
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    cleaned_up: bool = False


def corridor_quote_key(expiry: str, strike: float, right: str) -> str:
    """Canonical string key identifying one option contract inside UI & stream diffs."""
    return f"{expiry}|{strike:g}|{right.upper()}"


def _opt_float(val: object) -> float | None:
    if val is None:
        return None
    try:
        num = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(num) or math.isinf(num):
        return None
    return num


def _opt_int(val: object) -> int | None:
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


async def _is_client_disconnected(
    callback: Callable[[], bool | Awaitable[bool]] | None,
) -> bool:
    if callback is None:
        return False
    res = callback()
    if inspect.isawaitable(res):
        return bool(await res)
    return bool(res)


def _compute_mid(
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


def _compute_spread_pct(bid: float | None, ask: float | None, mid: float | None) -> float | None:
    if bid is None or ask is None or mid is None or bid <= 0.0 or ask < bid or mid <= 0.0:
        return None
    return round(((ask - bid) / mid) * 100.0, 2)


def _nearest_index(sorted_strikes: Sequence[float], anchor: float) -> int:
    return min(
        range(len(sorted_strikes)),
        key=lambda idx: (abs(sorted_strikes[idx] - anchor), sorted_strikes[idx]),
    )


def _ib_expiry(iso_expiry: str) -> str:
    clean = iso_expiry.strip()
    if len(clean) == 10 and clean[4] == "-" and clean[7] == "-":
        return clean.replace("-", "")
    return clean[:8]


def _normalize_greeks_source(raw: object) -> GreeksSource:
    text = str(raw or "NONE").upper()
    if text == "IBKR_MODEL":
        return "IBKR_MODEL"
    if text == "BSM_FALLBACK":
        return "BSM_FALLBACK"
    return "NONE"


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
    cached_mid = _compute_mid(bid, ask, last, close, sanitize_price(raw.get("mid")))
    return CorridorContractSpec(
        option_conid=_opt_int(raw.get("option_conid")) or 0,
        symbol=symbol,
        expiry=exp_iso,
        ib_expiry=_ib_expiry(exp_iso),
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
        cached_volume=_opt_int(raw.get("volume")),
        cached_open_interest=_opt_int(raw.get("open_interest")),
        cached_iv=_opt_float(raw.get("iv")),
        cached_delta=_opt_float(raw.get("delta")),
        cached_gamma=_opt_float(raw.get("gamma")),
        cached_theta=_opt_float(raw.get("theta")),
        cached_vega=_opt_float(raw.get("vega")),
        cached_greeks_source=_normalize_greeks_source(raw.get("greeks_source")),
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
    """Select `±window_strikes` around Primary Put & Call Walls, strictly `<= 60` contracts.

    Priority tiers inside the contract budget:
      0. Active Strategy Lab legs (even if on a secondary DTE, e.g. Diagonal spreads)
      1. Primary Wall strikes & sides (`P` inside Put Wall `±k`, `C` inside Call Wall `±k`),
         ordered closest to their Wall strike first
      2. Opposite right at those Wall strikes (if `include_opposite_right=True`)
      3. Nearest ATM `±k` strikes around underlying spot if remaining budget allows
    """
    if df.height == 0:
        raise OptionChainFetchError("Cannot select Wall Corridor from an empty chain DataFrame")

    eff_win = min(max(1, int(window_strikes)), 15)
    eff_max = min(max(1, int(max_contracts)), CORRIDOR_MAX_CONTRACTS)

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
    atm_idx = _nearest_index(avail_strikes, atm_strike)
    idx_by_strike = {round(s, 6): i for i, s in enumerate(avail_strikes)}

    pw_window = {
        i for i in range(max(0, pw_idx - eff_win), min(len(avail_strikes), pw_idx + eff_win + 1))
    }
    cw_window = {
        i for i in range(max(0, cw_idx - eff_win), min(len(avail_strikes), cw_idx + eff_win + 1))
    }
    atm_window = {
        i for i in range(max(0, atm_idx - eff_win), min(len(avail_strikes), atm_idx + eff_win + 1))
    }

    priority_keys = {(leg.expiry, round(float(leg.strike), 6), leg.right) for leg in priority_legs}
    all_rows_by_key: dict[tuple[str, float, str], dict[str, Any]] = {}
    for row in df.iter_rows(named=True):
        right_u = str(row["right"]).upper()
        if right_u not in ("C", "P"):
            continue
        key = (str(row["expiry"]), round(float(row["strike"]), 6), right_u)
        all_rows_by_key[key] = row

    def _classify_zone(idx: int | None, *, is_leg: bool) -> CorridorZone:
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

    ranked_candidates: list[tuple[tuple[int, int, float, float, int], CorridorContractSpec]] = []
    seen_keys: set[tuple[str, float, str]] = set()

    # Tier 0: explicitly prioritized Strategy Lab legs (capped reasonably inside eff_max)
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
        zone = _classify_zone(s_idx, is_leg=True)
        is_pw = put_wall is not None and abs(float(raw_match["strike"]) - put_wall) < 1e-4
        is_cw = call_wall is not None and abs(float(raw_match["strike"]) - call_wall) < 1e-4
        spec = _row_to_spec(
            raw_match,
            symbol=symbol,
            zone=zone,
            is_put_wall=is_pw and leg.right == "P",
            is_call_wall=is_cw and leg.right == "C",
            is_strategy_leg=True,
        )
        ranked_candidates.append(((0, 0, abs(spec.strike - spot), spec.strike, 0), spec))

    # Tiers 1..3 inside target expiration
    for row in df_exp.iter_rows(named=True):
        right_u = str(row["right"]).upper()
        if right_u not in ("C", "P"):
            continue
        strike_val = float(row["strike"])
        key = (resolved_expiry, round(strike_val, 6), right_u)
        if key in seen_keys:
            continue
        s_idx = idx_by_strike.get(round(strike_val, 6))
        if s_idx is None:
            continue

        in_pw = s_idx in pw_window
        in_cw = s_idx in cw_window
        in_atm = s_idx in atm_window
        if not (in_pw or in_cw or in_atm):
            continue

        d_pw = abs(s_idx - pw_idx) if in_pw else 999
        d_cw = abs(s_idx - cw_idx) if in_cw else 999
        d_atm = abs(s_idx - atm_idx) if in_atm else 999

        if (in_pw and right_u == "P") or (in_cw and right_u == "C"):
            tier = 1
            wall_dist = d_pw if (in_pw and right_u == "P") else d_cw
        elif (in_pw or in_cw) and include_opposite_right:
            tier = 2
            wall_dist = min(d_pw, d_cw)
        elif in_atm:
            tier = 3
            wall_dist = d_atm
        else:
            continue

        seen_keys.add(key)
        is_pw = put_wall is not None and abs(strike_val - put_wall) < 1e-4 and right_u == "P"
        is_cw = call_wall is not None and abs(strike_val - call_wall) < 1e-4 and right_u == "C"
        is_leg = key in priority_keys
        spec = _row_to_spec(
            row,
            symbol=symbol,
            zone=_classify_zone(s_idx, is_leg=is_leg),
            is_put_wall=is_pw,
            is_call_wall=is_cw,
            is_strategy_leg=is_leg,
        )
        right_ord = 0 if right_u == "P" else 1
        ranked_candidates.append(
            ((tier, wall_dist, abs(strike_val - spot), strike_val, right_ord), spec)
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


def _contract_from_spec(spec: CorridorContractSpec) -> Option:
    """Build an IBKR `Option` reusing pre-qualified `conId` when available."""
    return Option(
        conId=spec.option_conid if spec.option_conid > 0 else 0,
        symbol=spec.symbol,
        lastTradeDateOrContractMonth=spec.ib_expiry,
        strike=spec.strike,
        right=spec.right,
        exchange="SMART",
        multiplier="100",
        currency="USD",
    )


def _build_frame_quotes(
    specs: Sequence[CorridorContractSpec],
    tickers: Sequence[Any],
    *,
    spot: float,
    updated_ts_utc: str,
    greeks_fallback: GreeksFallback | None,
) -> list[CorridorQuoteRow]:
    """Convert raw IBKR `Ticker` objects to `CorridorQuoteRow` items (Greeks fallback injected)."""
    interim: list[tuple[CorridorContractSpec, dict[str, Any]]] = []
    missing_indices: list[int] = []

    for idx, (spec, tk) in enumerate(zip(specs, tickers, strict=True)):
        bid = sanitize_price(getattr(tk, "bid", None))
        ask = sanitize_price(getattr(tk, "ask", None))
        last = sanitize_price(getattr(tk, "last", None))
        close = sanitize_price(getattr(tk, "close", None))
        if bid is None and ask is None and last is None and close is None:
            bid = spec.cached_bid
            ask = spec.cached_ask
            last = spec.cached_last
            close = spec.cached_close
        mid = _compute_mid(bid, ask, last, close, spec.cached_mid)
        spread_pct = _compute_spread_pct(bid, ask, mid)
        vol = sanitize_count(getattr(tk, "volume", None))
        if vol is None:
            vol = spec.cached_volume
        oi = extract_open_interest(tk, spec.right)
        if oi is None:
            oi = spec.cached_open_interest

        iv, delta, gamma, theta, vega, g_source = extract_model_greeks(tk)
        if g_source != "IBKR_MODEL":
            missing_indices.append(idx)

        interim.append(
            (
                spec,
                {
                    "bid": bid,
                    "ask": ask,
                    "mid": mid,
                    "last": last,
                    "close": close,
                    "spread_pct": spread_pct,
                    "volume": vol,
                    "open_interest": oi,
                    "iv": iv,
                    "delta": delta,
                    "gamma": gamma,
                    "theta": theta,
                    "vega": vega,
                    "greeks_source": g_source,
                },
            )
        )

    # Rows without IBKR modelGreeks: injected fallback (vectorized BSM, <= 60 rows) when
    # available, otherwise / when unusable the cached snapshot Greeks are kept.
    usable = np.zeros(len(missing_indices), dtype=np.bool_)
    computed: tuple[FloatArray, FloatArray, FloatArray, FloatArray, FloatArray] | None = None
    if missing_indices and greeks_fallback is not None and spot > 0.0:
        miss_specs = [interim[i][0] for i in missing_indices]
        iv_arr, d_arr, g_arr, th_arr, v_arr, usable = greeks_fallback(
            mark_price=np.array(
                [float(interim[i][1]["mid"] or 0.0) for i in missing_indices], dtype=np.float64
            ),
            spot=np.full(len(missing_indices), spot, dtype=np.float64),
            strike=np.array([s.strike for s in miss_specs], dtype=np.float64),
            dte=np.array([float(s.dte) for s in miss_specs], dtype=np.float64),
            is_call=np.array([s.right == "C" for s in miss_specs], dtype=np.bool_),
            fallback_iv=np.array(
                [s.cached_iv if s.cached_iv is not None else np.nan for s in miss_specs],
                dtype=np.float64,
            ),
        )
        computed = (iv_arr, d_arr, g_arr, th_arr, v_arr)
    for pos, row_idx in enumerate(missing_indices):
        spec_i, data_i = interim[row_idx]
        if computed is not None and bool(usable[pos]):
            iv_arr, d_arr, g_arr, th_arr, v_arr = computed
            data_i["iv"] = round(float(iv_arr[pos]), 4)
            data_i["delta"] = round(float(d_arr[pos]), 4)
            data_i["gamma"] = round(float(g_arr[pos]), 5)
            data_i["theta"] = round(float(th_arr[pos]), 4)
            data_i["vega"] = round(float(v_arr[pos]), 4)
            data_i["greeks_source"] = "BSM_FALLBACK"
        else:
            data_i["iv"] = spec_i.cached_iv
            data_i["delta"] = spec_i.cached_delta
            data_i["gamma"] = spec_i.cached_gamma
            data_i["theta"] = spec_i.cached_theta
            data_i["vega"] = spec_i.cached_vega
            data_i["greeks_source"] = spec_i.cached_greeks_source

    rows: list[CorridorQuoteRow] = []
    for spec, d in interim:
        rows.append(
            CorridorQuoteRow(
                option_conid=spec.option_conid,
                expiry=spec.expiry,
                dte=spec.dte,
                strike=spec.strike,
                right=spec.right,
                zone=spec.zone,
                is_put_wall=spec.is_put_wall,
                is_call_wall=spec.is_call_wall,
                is_strategy_leg=spec.is_strategy_leg,
                bid=round(float(d["bid"]), 4) if d["bid"] is not None else None,
                ask=round(float(d["ask"]), 4) if d["ask"] is not None else None,
                mid=round(float(d["mid"]), 4) if d["mid"] is not None else None,
                last=round(float(d["last"]), 4) if d["last"] is not None else None,
                close=round(float(d["close"]), 4) if d["close"] is not None else None,
                spread_pct=d["spread_pct"],
                volume=d["volume"],
                open_interest=d["open_interest"],
                iv=d["iv"],
                delta=d["delta"],
                gamma=d["gamma"],
                theta=d["theta"],
                vega=d["vega"],
                greeks_source=d["greeks_source"],
                updated_ts_utc=updated_ts_utc,
            )
        )
    return rows


def _quote_signature(row: CorridorQuoteRow) -> tuple[Any, ...]:
    return (
        row.bid,
        row.ask,
        row.mid,
        row.last,
        row.volume,
        row.open_interest,
        row.iv,
        row.delta,
    )


class CorridorQuoteService:
    """Manages bounded `<= 60`-contract Wall Corridor snapshots and constant streaming."""

    def __init__(
        self,
        client: IBKRBrokerClient,
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: SleepFn = asyncio.sleep,
        greeks_fallback: GreeksFallback | None = None,
    ) -> None:
        self.client = client
        self.settings = settings or client.settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self.greeks_fallback = greeks_fallback
        self._active_sessions: dict[str, _ActiveCorridorSession] = {}

    @property
    def active_stream_count(self) -> int:
        return len(self._active_sessions)

    def _generic_ticks(self) -> str:
        if self.client.mode == "LIVE" and not self.client.live_subscription_missing:
            return LIVE_OPRA_GENERIC_TICKS
        return DELAYED_GENERIC_TICKS

    def _cleanup_session(self, session: _ActiveCorridorSession) -> None:
        session.stop_event.set()
        if not session.cleaned_up:
            session.cleaned_up = True
            for contract in session.subscribed_contracts:
                try:
                    session.ib.cancelMktData(contract)
                except Exception:  # pragma: no cover - defensive cleanup
                    logger.exception("corridor cancelMktData failed for %s", contract)
        self._active_sessions.pop(session.session_id, None)

    async def stop_all_streams(self) -> int:
        """Signal and cancel 100% of active corridor market-data subscriptions."""
        sessions = list(self._active_sessions.values())
        count = 0
        for sess in sessions:
            count += len(sess.subscribed_contracts)
            self._cleanup_session(sess)
        return count

    async def _prepare_contracts(
        self,
        selection: CorridorSelection,
    ) -> tuple[list[Any], Any | None]:
        """Build option & optional spot contracts reusing cached `conId` when present."""
        if len(selection.specs) > CORRIDOR_MAX_CONTRACTS:
            raise PacingViolationError(
                f"Corridor selection {len(selection.specs)} exceeds {CORRIDOR_MAX_CONTRACTS} limit"
            )

        candidates = [_contract_from_spec(spec) for spec in selection.specs]
        unqualified = [c for c in candidates if int(getattr(c, "conId", 0) or 0) <= 0]
        if unqualified:
            res = list(await self.client.ib.qualifyContractsAsync(*unqualified) or [])
            if res and len(res) == len(unqualified):
                res_iter = iter(res)
                candidates = [
                    next(res_iter) if int(getattr(c, "conId", 0) or 0) <= 0 else c
                    for c in candidates
                ]

        spot_contract: Any | None = None
        if len(candidates) < CORRIDOR_MAX_CONTRACTS:
            if selection.underlying_conid > 0:
                spot_contract = Stock(
                    conId=selection.underlying_conid,
                    symbol=selection.ticker,
                    exchange="SMART",
                    currency="USD",
                )
            else:
                qual_spot = list(
                    await self.client.ib.qualifyContractsAsync(
                        Stock(selection.ticker, "SMART", "USD")
                    )
                    or []
                )
                if qual_spot:
                    spot_contract = qual_spot[0]

        total_subs = len(candidates) + (1 if spot_contract is not None else 0)
        if total_subs > CORRIDOR_MAX_CONTRACTS:
            raise PacingViolationError(
                f"Corridor total subscriptions {total_subs} exceed hard cap "
                f"{CORRIDOR_MAX_CONTRACTS}"
            )
        return candidates, spot_contract

    async def _subscribe_corridor(
        self,
        session: _ActiveCorridorSession,
        option_contracts: Sequence[Any],
        spot_contract: Any | None,
        *,
        generic_ticks: str,
    ) -> tuple[list[Any], Any | None]:
        ib = self.client.ib
        spot_ticker: Any | None = None
        if spot_contract is not None:
            spot_ticker = ib.reqMktData(
                spot_contract,
                genericTickList="",
                snapshot=False,
                regulatorySnapshot=False,
            )
            session.subscribed_contracts.append(spot_contract)

        option_tickers: list[Any] = []
        for contract in option_contracts:
            tk = ib.reqMktData(
                contract,
                genericTickList=generic_ticks,
                snapshot=False,
                regulatorySnapshot=False,
            )
            session.subscribed_contracts.append(contract)
            option_tickers.append(tk)

        return option_tickers, spot_ticker

    async def _poll_initial_corridor(
        self,
        session: _ActiveCorridorSession,
        option_contracts: Sequence[Any],
        spot_contract: Any | None,
        *,
        resubscribe_on_fallback: bool,
    ) -> tuple[_ActiveCorridorSession, list[Any], Any | None, list[str]]:
        """Subscribe corridor, wait for initial ticks, and apply dynamic Type fallback if needed."""
        oc = self.settings.options_chain
        corridor_cfg = self.settings.corridor
        notices: list[str] = []

        async def _resubscribe(suffix: str) -> tuple[list[Any], Any | None]:
            nonlocal session
            self._cleanup_session(session)
            session = _ActiveCorridorSession(
                session_id=f"{session.session_id}-{suffix}",
                ib=self.client.ib,
            )
            self._active_sessions[session.session_id] = session
            return await self._subscribe_corridor(
                session,
                option_contracts,
                spot_contract,
                generic_ticks=self._generic_ticks(),
            )

        try:
            option_tickers, spot_ticker = await self._subscribe_corridor(
                session,
                option_contracts,
                spot_contract,
                generic_ticks=self._generic_ticks(),
            )
            await poll_until(
                lambda: all(has_usable_quote(tk) for tk in option_tickers),
                corridor_cfg.oneshot_collect_timeout_sec,
                oc.page_poll_interval_sec,
                self.sleep,
            )

            if self.client.live_subscription_missing:
                sub_notice = self.client.consume_subscription_fallback_notice()
                if sub_notice:
                    notices.append(sub_notice)
                if resubscribe_on_fallback:
                    option_tickers, spot_ticker = await _resubscribe("dl")
                await poll_until(
                    lambda: all(has_usable_quote(tk) for tk in option_tickers),
                    corridor_cfg.oneshot_collect_timeout_sec,
                    oc.page_poll_interval_sec,
                    self.sleep,
                )

            if (
                self.client.can_fallback()
                and empty_quote_ratio(option_tickers) > EMPTY_QUOTE_FALLBACK_THRESHOLD
            ):
                prev_type = self.client.current_type
                new_type = self.client.switch_to_fallback_data_type()
                notices.append(
                    f"Corridor Type {prev_type} ({market_data_type_label(prev_type)}) quotes were "
                    f">80% empty; cascaded to Type {new_type} ({market_data_type_label(new_type)})."
                )
                if resubscribe_on_fallback:
                    option_tickers, spot_ticker = await _resubscribe("fb")
                    timeout_sec = corridor_cfg.oneshot_collect_timeout_sec
                else:
                    timeout_sec = min(1.5, corridor_cfg.oneshot_collect_timeout_sec)
                await poll_until(
                    lambda: all(has_usable_quote(tk) for tk in option_tickers),
                    timeout_sec,
                    oc.page_poll_interval_sec,
                    self.sleep,
                )

            self.client.observe_tickers_market_data_type(option_tickers)
            return session, option_tickers, spot_ticker, notices
        except BaseException:
            self._cleanup_session(session)
            raise

    async def fetch_corridor_quotes(
        self,
        selection: CorridorSelection,
        *,
        snapshot_id: str,
        on_frame: Callable[[CorridorQuoteFrame], None] | None = None,
    ) -> CorridorQuoteFrame:
        """One-shot targeted Wall Strike Corridor pull (`<= 60` contracts) with cleanup."""
        t0 = time.perf_counter()

        await self.client.ensure_connected()
        self.client.reset_market_data_type()
        option_contracts, spot_contract = await self._prepare_contracts(selection)

        session = _ActiveCorridorSession(
            session_id=f"oneshot-{uuid.uuid4().hex[:8]}",
            ib=self.client.ib,
        )
        self._active_sessions[session.session_id] = session

        try:
            session, option_tickers, spot_ticker, notices = await self._poll_initial_corridor(
                session,
                option_contracts,
                spot_contract,
                resubscribe_on_fallback=True,
            )

            live_spot = (
                extract_spot(spot_ticker) if spot_ticker is not None else None
            ) or selection.underlying_price
            ts_utc = self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            quotes = _build_frame_quotes(
                selection.specs,
                option_tickers,
                spot=float(live_spot),
                updated_ts_utc=ts_utc,
                greeks_fallback=self.greeks_fallback,
            )
            elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            frame = CorridorQuoteFrame(
                ticker=selection.ticker,
                snapshot_id=snapshot_id,
                target_expiry=selection.target_expiry,
                target_dte=selection.target_dte,
                window_strikes=selection.window_strikes,
                max_contracts=selection.max_contracts,
                subscribed_count=len(session.subscribed_contracts),
                subscription_cap=CORRIDOR_MAX_CONTRACTS,
                underlying_price=round(float(live_spot), 4),
                market_data_type=self.client.get_active_market_data_type(),
                primary_call_wall=selection.primary_call_wall,
                primary_put_wall=selection.primary_put_wall,
                wall_basis=selection.wall_basis,
                updated_ts_utc=ts_utc,
                sequence=1,
                mode="ONE_SHOT",
                elapsed_ms=elapsed_ms,
                changed_keys=[corridor_quote_key(q.expiry, q.strike, q.right) for q in quotes],
                quotes=quotes,
                notices=notices,
            )
            if on_frame is not None:
                on_frame(frame)
            return frame
        finally:
            self._cleanup_session(session)

    async def stream_corridor_quotes(
        self,
        selection: CorridorSelection,
        *,
        snapshot_id: str,
        poll_interval_sec: float | None = None,
        is_disconnected: Callable[[], bool | Awaitable[bool]] | None = None,
        max_frames: int | None = None,
        on_frame: Callable[[CorridorQuoteFrame], None] | None = None,
    ) -> AsyncGenerator[CorridorQuoteFrame, None]:
        """Continuously stream throttled Wall Corridor quotes until stop/disconnect/error."""
        # Ensure zero overlapping live streams so IBKR line count stays <= 60
        await self.stop_all_streams()

        await self.client.ensure_connected()
        self.client.reset_market_data_type()
        option_contracts, spot_contract = await self._prepare_contracts(selection)

        corridor_cfg = self.settings.corridor
        eff_poll = (
            float(poll_interval_sec)
            if poll_interval_sec is not None
            else corridor_cfg.stream_poll_interval_sec
        )
        session = _ActiveCorridorSession(
            session_id=f"stream-{uuid.uuid4().hex[:8]}",
            ib=self.client.ib,
        )
        self._active_sessions[session.session_id] = session

        try:
            session, option_tickers, spot_ticker, notices = await self._poll_initial_corridor(
                session,
                option_contracts,
                spot_contract,
                resubscribe_on_fallback=False,
            )

            last_sigs: dict[str, tuple[Any, ...]] = {}
            last_spot: float | None = None
            seq = 0
            idle_elapsed = 0.0
            heartbeat_sec = corridor_cfg.heartbeat_interval_sec

            while not session.stop_event.is_set():
                if await _is_client_disconnected(is_disconnected):
                    break
                if not self.client.is_connected:
                    raise GatewayConnectionError(
                        "IBKR Gateway connection lost during live corridor stream"
                    )

                self.client.observe_tickers_market_data_type(option_tickers)
                raw_spot = extract_spot(spot_ticker) if spot_ticker is not None else None
                live_spot = float(raw_spot) if raw_spot is not None else selection.underlying_price
                ts_utc = self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                quotes = _build_frame_quotes(
                    selection.specs,
                    option_tickers,
                    spot=live_spot,
                    updated_ts_utc=ts_utc,
                    greeks_fallback=self.greeks_fallback,
                )

                changed: list[str] = []
                for q in quotes:
                    qkey = corridor_quote_key(q.expiry, q.strike, q.right)
                    sig = _quote_signature(q)
                    if last_sigs.get(qkey) != sig:
                        changed.append(qkey)
                        last_sigs[qkey] = sig

                spot_changed = last_spot is None or abs(live_spot - last_spot) >= 0.005
                should_emit = (
                    seq == 0 or bool(changed) or spot_changed or idle_elapsed >= heartbeat_sec
                )

                if should_emit:
                    seq += 1
                    idle_elapsed = 0.0
                    last_spot = live_spot
                    frame = CorridorQuoteFrame(
                        ticker=selection.ticker,
                        snapshot_id=snapshot_id,
                        target_expiry=selection.target_expiry,
                        target_dte=selection.target_dte,
                        window_strikes=selection.window_strikes,
                        max_contracts=selection.max_contracts,
                        subscribed_count=len(session.subscribed_contracts),
                        subscription_cap=CORRIDOR_MAX_CONTRACTS,
                        underlying_price=round(live_spot, 4),
                        market_data_type=self.client.get_active_market_data_type(),
                        primary_call_wall=selection.primary_call_wall,
                        primary_put_wall=selection.primary_put_wall,
                        wall_basis=selection.wall_basis,
                        updated_ts_utc=ts_utc,
                        sequence=seq,
                        mode="STREAM",
                        changed_keys=changed,
                        quotes=quotes,
                        notices=notices if seq == 1 else [],
                    )
                    if on_frame is not None:
                        on_frame(frame)
                    yield frame
                    if max_frames is not None and seq >= max_frames:
                        break
                else:
                    idle_elapsed += eff_poll

                if session.stop_event.is_set() or await _is_client_disconnected(is_disconnected):
                    break
                await self.sleep(eff_poll)
        finally:
            self._cleanup_session(session)
