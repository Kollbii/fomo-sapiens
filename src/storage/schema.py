"""Storage contracts: `OptionContractRow` (Pydantic) and `OPTION_CHAIN_POLARS_SCHEMA` (Polars)."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, datetime
from typing import Any, Literal, Self

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.exceptions import InvalidTickerError
from src.core.security import TickerValidator

PRICE_SENTINEL_MAX = 1e9
INT_SENTINEL = 2_147_483_647
SNAPSHOT_ID_RE = re.compile(r"[A-Z]{1,5}_\d{8}_\d{6}")
_STANDARD_RANGE_PCTS = (0.10, 0.15, 0.20, 0.25, 0.35, 0.50)

# Shared domain literals (single definition; imported by broker, analytics and services).
GreeksSource = Literal["IBKR_MODEL", "BSM_FALLBACK", "NONE"]
OptionRight = Literal["C", "P"]
WallBasis = Literal["OPEN_INTEREST", "VOLUME"]
SpotSource = Literal["LAST", "MID", "CLOSE"]


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def sanitize_price(value: Any) -> float | None:
    """`<= 0`, NaN, or `>= 1e9` (IBKR sentinels) -> None."""
    if _is_missing(value):
        return None
    number = float(value)
    return None if number <= 0.0 or number >= PRICE_SENTINEL_MAX or math.isinf(number) else number


def sanitize_count(value: Any) -> int | None:
    """`< 0`, NaN, or `>= 2147483647` -> None."""
    if _is_missing(value):
        return None
    number = float(value)
    if number < 0 or number >= INT_SENTINEL or math.isinf(number):
        return None
    return int(number)


def _sanitize_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    number = float(value)
    return None if math.isinf(number) or abs(number) >= PRICE_SENTINEL_MAX else number


class OptionContractRow(BaseModel):
    """One option contract quote; the exact 27-column CSV contract (field order = column order)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    snapshot_id: str
    snapshot_ts_utc: str
    market_date: str
    market_data_type: int = Field(ge=1, le=4)
    underlying: str
    underlying_conid: int = Field(gt=0)
    underlying_price: float = Field(gt=0.0, lt=PRICE_SENTINEL_MAX)
    option_conid: int = Field(gt=0)
    expiry: str
    dte: int = Field(ge=0, le=400)
    strike: float = Field(gt=0.0, lt=PRICE_SENTINEL_MAX)
    moneyness_pct: float
    right: Literal["C", "P"]
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    last: float | None = None
    close: float | None = None
    mark_price: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    greeks_source: GreeksSource

    # --- boundary sanitization -------------------------------------------------
    @field_validator("bid", "ask", "mid", "last", "close", "mark_price", mode="before")
    @classmethod
    def _prices(cls, value: Any) -> float | None:
        return sanitize_price(value)

    @field_validator("volume", "open_interest", mode="before")
    @classmethod
    def _counts(cls, value: Any) -> int | None:
        return sanitize_count(value)

    @field_validator("iv", mode="before")
    @classmethod
    def _iv(cls, value: Any) -> float | None:
        number = _sanitize_float(value)
        return None if number is None or number <= 0.0 or number > 10.0 else number

    @field_validator("delta", mode="before")
    @classmethod
    def _delta(cls, value: Any) -> float | None:
        number = _sanitize_float(value)
        return None if number is None or abs(number) > 1.001 else max(-1.0, min(1.0, number))

    @field_validator("gamma", mode="before")
    @classmethod
    def _gamma(cls, value: Any) -> float | None:
        number = _sanitize_float(value)
        return None if number is None or number < 0.0 else number

    @field_validator("theta", "vega", mode="before")
    @classmethod
    def _other_greeks(cls, value: Any) -> float | None:
        return _sanitize_float(value)

    # --- format validation -----------------------------------------------------
    @field_validator("underlying")
    @classmethod
    def _underlying(cls, value: str) -> str:
        try:
            return TickerValidator.validate(value)
        except InvalidTickerError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("snapshot_id")
    @classmethod
    def _snapshot_id(cls, value: str) -> str:
        if SNAPSHOT_ID_RE.fullmatch(value) is None:
            raise ValueError(f"snapshot_id must match {{TICKER}}_{{YYYYMMDD_HHMMSS}}: {value!r}")
        return value

    @field_validator("market_date", "expiry")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
            raise ValueError(f"expected YYYY-MM-DD, got {value!r}")
        date.fromisoformat(value)
        return value

    @field_validator("snapshot_ts_utc")
    @classmethod
    def _ts(cls, value: str) -> str:
        if not value.endswith("Z"):
            raise ValueError(f"snapshot_ts_utc must be ISO-8601 UTC ending in 'Z': {value!r}")
        datetime.fromisoformat(value[:-1] + "+00:00")
        return value

    @model_validator(mode="after")
    def _derive(self) -> Self:
        if not self.snapshot_id.startswith(f"{self.underlying}_"):
            raise ValueError("snapshot_id prefix must equal underlying")
        if self.mid is None and self.bid is not None and self.ask is not None:
            self.mid = (self.bid + self.ask) / 2.0
        if self.mark_price is None:
            for candidate in (self.mid, self.last, self.close):
                if candidate is not None:
                    self.mark_price = candidate
                    break
        return self


COLUMNS: tuple[str, ...] = tuple(OptionContractRow.model_fields)
NON_NULLABLE_COLUMNS: tuple[str, ...] = tuple(
    name for name, field in OptionContractRow.model_fields.items() if field.is_required()
)

OPTION_CHAIN_POLARS_SCHEMA = pl.Schema(
    {
        "snapshot_id": pl.Utf8,
        "snapshot_ts_utc": pl.Utf8,
        "market_date": pl.Utf8,
        "market_data_type": pl.Int64,
        "underlying": pl.Utf8,
        "underlying_conid": pl.Int64,
        "underlying_price": pl.Float64,
        "option_conid": pl.Int64,
        "expiry": pl.Utf8,
        "dte": pl.Int64,
        "strike": pl.Float64,
        "moneyness_pct": pl.Float64,
        "right": pl.Utf8,
        "bid": pl.Float64,
        "ask": pl.Float64,
        "mid": pl.Float64,
        "last": pl.Float64,
        "close": pl.Float64,
        "mark_price": pl.Float64,
        "volume": pl.Int64,
        "open_interest": pl.Int64,
        "iv": pl.Float64,
        "delta": pl.Float64,
        "gamma": pl.Float64,
        "theta": pl.Float64,
        "vega": pl.Float64,
        "greeks_source": pl.Utf8,
    }
)


class SnapshotMeta(BaseModel):
    """One entry of `data/metadata/snapshot_index.json`."""

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    file_path: str
    snapshot_ts_utc: str
    market_date: str
    market_data_type: int
    underlying_price: float
    contract_count: int
    expirations: list[str]
    range_pct: float | None = None
    strike_step: float | None = None
    requested_strike_step: float | None = None
    exchange_validated_strike_count: int | None = None
    available_strike_steps: list[float] | None = None
    available_range_pcts: list[float] | None = None
    exchange_native_verified: bool | None = None
    spot_source: SpotSource | None = None
    quoted_contract_count: int | None = None
    quote_coverage: float | None = None
    prior_close_only_count: int | None = None


def infer_chain_step_and_range(df: pl.DataFrame) -> tuple[float | None, float | None]:
    """Infer `(strike_step, range_pct)` directly from an option chain DataFrame."""
    if df.height == 0:
        return (None, None)

    strikes = sorted({float(s) for s in df["strike"].to_list() if s is not None})
    inferred_step: float | None = None
    if len(strikes) >= 2:
        diffs = [
            round(b - a, 4) for a, b in zip(strikes, strikes[1:], strict=False) if b - a > 1e-6
        ]
        if diffs:
            counts = Counter(diffs)
            inferred_step = min(counts, key=lambda d: (-counts[d], d))

    spot = float(df["underlying_price"][0]) if df["underlying_price"][0] is not None else 0.0
    inferred_range: float | None = None
    if spot > 0.0 and strikes:
        raw_range = max(abs(strikes[0] - spot), abs(strikes[-1] - spot)) / spot
        if raw_range > 0.0:
            nearest = min(_STANDARD_RANGE_PCTS, key=lambda p: abs(p - raw_range))
            inferred_range = (
                nearest if abs(nearest - raw_range) <= 0.03 else round(min(1.0, raw_range), 2)
            )
    return (inferred_step, inferred_range)
