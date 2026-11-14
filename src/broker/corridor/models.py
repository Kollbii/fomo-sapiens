"""Wall Corridor contracts: limits, request/response models, selection specs, injected protocols."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

import numpy as np
import numpy.typing as npt
from pydantic import BaseModel, ConfigDict, Field

from src.storage.schema import (
    GreeksSource,
    OptionRight,
    WallBasis,
)

#: Hard ceiling on concurrent IBKR subscriptions (88 options + 1 spot = 89 <= 90, keeping a
#: 10-line buffer below IBKR's 100-line market-data limit).
CORRIDOR_MAX_CONTRACTS: int = 90

#: Default maximum option contracts selected for the Wall Strike Corridor.
DEFAULT_CORRIDOR_MAX_CONTRACTS: int = 88

#: Default ±strike window around Primary Put & Call Walls.
DEFAULT_WINDOW_STRIKES: int = 10

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
    window_strikes: int = Field(default=DEFAULT_WINDOW_STRIKES, ge=1, le=15)
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


def corridor_quote_key(expiry: str, strike: float, right: str) -> str:
    """Canonical string key identifying one option contract inside UI & stream diffs."""
    return f"{expiry}|{strike:g}|{right.upper()}"
