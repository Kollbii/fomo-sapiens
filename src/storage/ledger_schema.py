"""Storage contracts for the Hybrid Options Trade Ledger (Pydantic + Polars)."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.exceptions import InvalidTickerError
from src.core.security import TickerValidator
from src.storage.schema import INT_SENTINEL, PRICE_SENTINEL_MAX

LedgerAssetClass = Literal["OPT", "STK"]
LedgerSide = Literal["BOT", "SLD"]
LedgerOpenClose = Literal["O", "C"]
LedgerEventCode = Literal["TRADE", "EXPIRED", "ASSIGNED", "EXERCISED"]
LedgerSource = Literal[
    "FLEX_ACTIVITY_365",
    "FLEX_MONTHLY",
    "FLEX_TRADE_CONF",
    "FLEX_IMPORT",
    "GATEWAY_LIVE",
]

FLEX_SOURCES: frozenset[str] = frozenset(
    {"FLEX_ACTIVITY_365", "FLEX_MONTHLY", "FLEX_TRADE_CONF", "FLEX_IMPORT"}
)

_US_EASTERN = ZoneInfo("America/New_York")
_ALNUM_SEP_RE = re.compile(r"[A-Z0-9]+(?:[ .-][A-Z0-9]+)*")
_ALNUM_CLEAN_RE = re.compile(r"[A-Z0-9]{1,20}")

_TZ_ALIASES: dict[str, timezone | ZoneInfo] = {
    "UTC": UTC,
    "GMT": UTC,
    "Z": UTC,
    "US/EASTERN": _US_EASTERN,
    "AMERICA/NEW_YORK": _US_EASTERN,
    "EST": timezone(timedelta(hours=-5)),
    "EDT": timezone(timedelta(hours=-4)),
}


def _is_nan_or_inf(number: float) -> bool:
    return math.isnan(number) or math.isinf(number)


def sanitize_signed_float(value: Any) -> float | None:
    """Convert missing / NaN / inf / IBKR sentinel (`|x| >= 1e9`) to None."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid float")
    if isinstance(value, str):
        stripped = value.strip().replace(",", "")
        if not stripped or stripped in {"--", "N/A", "NA", "null", "None"}:
            return None
        number = float(stripped)
    else:
        number = float(value)
    if _is_nan_or_inf(number) or abs(number) >= PRICE_SENTINEL_MAX:
        return None
    return number


def sanitize_optional_conid(value: Any) -> int | None:
    """Convert missing / `<= 0` / IBKR sentinel (`>= 2147483647`) to None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        num = int(float(stripped))
    else:
        num_f = float(value)
        if _is_nan_or_inf(num_f):
            return None
        num = int(num_f)
    if num <= 0 or num >= INT_SENTINEL:
        return None
    return num


def normalize_underlying(raw: str) -> str:
    """Normalize underlying symbol via `TickerValidator`, else clean uppercase alphanumeric."""
    if not isinstance(raw, str):
        raise ValueError(f"underlying must be a string, got {type(raw).__name__}")
    stripped = raw.strip()
    if not stripped or not stripped.isascii():
        raise ValueError(f"invalid underlying symbol: {raw!r}")
    try:
        return TickerValidator.validate(stripped)
    except InvalidTickerError:
        upper = stripped.upper()
        if ".." in upper or "/" in upper or "\\" in upper:
            raise ValueError(f"invalid underlying symbol: {raw!r}") from None
        if _ALNUM_SEP_RE.fullmatch(upper) is None:
            raise ValueError(f"invalid underlying symbol: {raw!r}") from None
        cleaned = re.sub(r"[ .-]+", "", upper)
        if _ALNUM_CLEAN_RE.fullmatch(cleaned) is None:
            raise ValueError(f"invalid underlying symbol: {raw!r}") from None
        return cleaned


def normalize_expiry_yyyymmdd(raw: Any) -> str | None:
    """Normalize `YYYYMMDD`, `YYYY-MM-DD`, or `date` into `YYYYMMDD` (or `None`)."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date().strftime("%Y%m%d")
    if isinstance(raw, date):
        return raw.strftime("%Y%m%d")
    if not isinstance(raw, str):
        raise ValueError(f"expiry must be string or date, got {type(raw).__name__}")
    text = raw.strip()
    if not text:
        return None
    head = text.split(";")[0].split(" ")[0].strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", head):
        parsed = date.fromisoformat(head)
        return parsed.strftime("%Y%m%d")
    if re.fullmatch(r"\d{8}", head):
        parsed = datetime.strptime(head, "%Y%m%d").date()
        return parsed.strftime("%Y%m%d")
    raise ValueError(f"expected YYYYMMDD or YYYY-MM-DD expiry, got {raw!r}")


def normalize_right_optional(raw: Any) -> Literal["C", "P"] | None:
    """Normalize option right (`C`/`CALL`, `P`/`PUT`) or return `None` when blank."""
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text:
        return None
    if text in ("C", "CALL"):
        return "C"
    if text in ("P", "PUT"):
        return "P"
    raise ValueError(f"option right must be 'C' or 'P', got {raw!r}")


def normalize_utc_timestamp(raw: Any) -> str:
    """Normalize ISO-8601 or IBKR date/time strings into `YYYY-MM-DDTHH:MM:SSZ`."""
    if isinstance(raw, datetime):
        dt = raw.replace(tzinfo=UTC) if raw.tzinfo is None else raw.astimezone(UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(raw, date):
        return f"{raw.isoformat()}T00:00:00Z"
    if not isinstance(raw, str):
        raise ValueError(f"timestamp must be string or datetime, got {type(raw).__name__}")
    text = raw.strip()
    if not text:
        raise ValueError("timestamp must not be empty")

    # ISO-8601 fast path (including `Z` or explicit offset).
    iso_candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    if "T" in iso_candidate or (
        len(iso_candidate) >= 19 and iso_candidate[4] == "-" and iso_candidate[7] == "-"
    ):
        try:
            dt = datetime.fromisoformat(iso_candidate)
            dt_utc = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
            return dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            pass

    # Extract optional trailing timezone token (e.g. "20261112 10:30:00 US/Eastern").
    tz_info: timezone | ZoneInfo = UTC
    cleaned = text.replace(",", " ").replace(";", " ")
    parts = cleaned.split()
    if len(parts) >= 2:
        last_upper = parts[-1].upper()
        if last_upper in _TZ_ALIASES:
            tz_info = _TZ_ALIASES[last_upper]
            cleaned = " ".join(parts[:-1])
        else:
            cleaned = " ".join(parts)
    else:
        cleaned = " ".join(parts)

    for fmt in (
        "%Y%m%d %H%M%S",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d-%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H%M%S",
        "%Y%m%d",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.replace(tzinfo=tz_info).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue
    raise ValueError(f"unrecognized timestamp format: {raw!r}")


def compute_signed_proceeds(
    side: LedgerSide, quantity: float, price: float, multiplier: int
) -> float:
    """Compute signed ex-fee cashflow: negative debit for BOT, positive credit for SLD."""
    gross = float(quantity) * float(price) * int(multiplier)
    return -gross if side == "BOT" else gross


def synthesize_exec_id(
    *,
    event_code: str,
    executed_at: str,
    underlying: str,
    asset_class: str,
    symbol: str,
    expiry: str | None,
    strike: float | None,
    right: str | None,
    side: str,
    quantity: float,
    price: float,
    proceeds: float,
    ib_order_id: str | None = None,
    hint: str | None = None,
) -> str:
    """Synthesize a deterministic sha256-based canonical `exec_id` when `IBExecID` is absent."""
    strike_txt = f"{strike:.6f}" if strike is not None else ""
    payload = "|".join(
        [
            event_code,
            executed_at,
            underlying,
            asset_class,
            symbol,
            expiry or "",
            strike_txt,
            right or "",
            side,
            f"{quantity:.6f}",
            f"{price:.6f}",
            f"{proceeds:.6f}",
            ib_order_id or "",
            hint or "",
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:24]}"


class LedgerExecutionRow(BaseModel):
    """Canonical normalized execution / option lifecycle row stored in `executions.csv`."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    exec_id: str = Field(min_length=1)
    ib_order_id: str | None = None
    conid: int | None = None
    underlying_conid: int | None = None
    symbol: str = Field(min_length=1)
    underlying: str
    asset_class: LedgerAssetClass
    expiry: str | None = None
    strike: float | None = None
    right: Literal["C", "P"] | None = None
    multiplier: int = Field(default=100, gt=0)
    side: LedgerSide
    quantity: float = Field(gt=0.0)
    price: float = Field(ge=0.0)
    proceeds: float
    commission: float = Field(default=0.0, ge=0.0)
    realized_pnl_ib: float | None = None
    open_close: LedgerOpenClose | None = None
    event_code: LedgerEventCode = "TRADE"
    currency: str = Field(default="USD", min_length=1)
    fx_rate_to_base: float = Field(default=1.0, gt=0.0)
    executed_at: str
    source: LedgerSource

    @model_validator(mode="before")
    @classmethod
    def _apply_dynamic_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = dict(data)
        if raw.get("multiplier") in (None, ""):
            ac = str(raw.get("asset_class", "")).strip().upper()
            raw["multiplier"] = 1 if ac == "STK" else 100
        if raw.get("currency") in (None, ""):
            raw["currency"] = "USD"
        if raw.get("fx_rate_to_base") in (None, ""):
            raw["fx_rate_to_base"] = 1.0
        if raw.get("event_code") in (None, ""):
            raw["event_code"] = "TRADE"
        if raw.get("commission") in (None, ""):
            raw["commission"] = 0.0
        return raw

    @field_validator("ib_order_id", mode="before")
    @classmethod
    def _opt_str(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("conid", "underlying_conid", mode="before")
    @classmethod
    def _conids(cls, value: Any) -> int | None:
        return sanitize_optional_conid(value)

    @field_validator("underlying")
    @classmethod
    def _underlying(cls, value: str) -> str:
        return normalize_underlying(value)

    @field_validator("asset_class", mode="before")
    @classmethod
    def _asset_class(cls, value: Any) -> str:
        return str(value).strip().upper()

    @field_validator("expiry", mode="before")
    @classmethod
    def _expiry(cls, value: Any) -> str | None:
        return normalize_expiry_yyyymmdd(value)

    @field_validator("strike", mode="before")
    @classmethod
    def _strike(cls, value: Any) -> float | None:
        return sanitize_signed_float(value)

    @field_validator("right", mode="before")
    @classmethod
    def _right(cls, value: Any) -> Literal["C", "P"] | None:
        return normalize_right_optional(value)

    @field_validator("side", mode="before")
    @classmethod
    def _side(cls, value: Any) -> str:
        text = str(value).strip().upper()
        if text in ("BOT", "BUY"):
            return "BOT"
        if text in ("SLD", "SELL"):
            return "SLD"
        return text

    @field_validator("quantity", "price", "fx_rate_to_base", mode="before")
    @classmethod
    def _required_pos_float(cls, value: Any) -> float:
        num = sanitize_signed_float(value)
        if num is None:
            raise ValueError("value must be a finite number")
        return num

    @field_validator("proceeds", mode="before")
    @classmethod
    def _proceeds(cls, value: Any) -> float:
        num = sanitize_signed_float(value)
        if num is None:
            raise ValueError("proceeds must be a finite number")
        return num

    @field_validator("commission", mode="before")
    @classmethod
    def _commission(cls, value: Any) -> float:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 0.0
        num = sanitize_signed_float(value)
        if num is None:
            return 0.0
        return abs(num)

    @field_validator("realized_pnl_ib", mode="before")
    @classmethod
    def _realized_pnl(cls, value: Any) -> float | None:
        return sanitize_signed_float(value)

    @field_validator("open_close", mode="before")
    @classmethod
    def _open_close(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip().upper()
        if not text:
            return None
        if text in ("O", "OPEN"):
            return "O"
        if text in ("C", "CLOSE"):
            return "C"
        raise ValueError(f"open_close must be 'O', 'C', or None, got {value!r}")

    @field_validator("event_code", mode="before")
    @classmethod
    def _event_code(cls, value: Any) -> str:
        return str(value).strip().upper()

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, value: Any) -> str:
        text = str(value or "USD").strip().upper()
        return text or "USD"

    @field_validator("executed_at", mode="before")
    @classmethod
    def _executed_at(cls, value: Any) -> str:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_asset_fields(self) -> Self:
        if self.asset_class == "OPT":
            if self.expiry is None:
                raise ValueError("OPT execution row requires expiry")
            if self.strike is None or self.strike <= 0.0:
                raise ValueError("OPT execution row requires positive strike")
            if self.right not in ("C", "P"):
                raise ValueError("OPT execution row requires right ('C' or 'P')")
        else:
            if self.strike == 0.0:
                self.strike = None
            if self.expiry is not None or self.strike is not None or self.right is not None:
                raise ValueError("STK execution row must not specify expiry, strike, or right")
        return self


class LedgerPositionRow(BaseModel):
    """Canonical normalized open position snapshot row stored in `open_positions.csv`."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    conid: int | None = None
    symbol: str = Field(min_length=1)
    underlying: str
    asset_class: LedgerAssetClass
    expiry: str | None = None
    strike: float | None = None
    right: Literal["C", "P"] | None = None
    multiplier: int = Field(default=100, gt=0)
    position: float
    avg_cost: float = 0.0
    mark_price: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    source: str = Field(min_length=1)
    updated_at: str

    @model_validator(mode="before")
    @classmethod
    def _apply_dynamic_defaults(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = dict(data)
        if raw.get("multiplier") in (None, ""):
            ac = str(raw.get("asset_class", "")).strip().upper()
            raw["multiplier"] = 1 if ac == "STK" else 100
        if raw.get("avg_cost") in (None, ""):
            raw["avg_cost"] = 0.0
        return raw

    @field_validator("conid", mode="before")
    @classmethod
    def _conid(cls, value: Any) -> int | None:
        return sanitize_optional_conid(value)

    @field_validator("underlying")
    @classmethod
    def _underlying(cls, value: str) -> str:
        return normalize_underlying(value)

    @field_validator("asset_class", mode="before")
    @classmethod
    def _asset_class(cls, value: Any) -> str:
        return str(value).strip().upper()

    @field_validator("expiry", mode="before")
    @classmethod
    def _expiry(cls, value: Any) -> str | None:
        return normalize_expiry_yyyymmdd(value)

    @field_validator("strike", "market_value", "unrealized_pnl", mode="before")
    @classmethod
    def _opt_signed_float(cls, value: Any) -> float | None:
        return sanitize_signed_float(value)

    @field_validator("mark_price", mode="before")
    @classmethod
    def _mark_price(cls, value: Any) -> float | None:
        num = sanitize_signed_float(value)
        return None if num is None or num < 0.0 else num

    @field_validator("right", mode="before")
    @classmethod
    def _right(cls, value: Any) -> Literal["C", "P"] | None:
        return normalize_right_optional(value)

    @field_validator("position", mode="before")
    @classmethod
    def _position(cls, value: Any) -> float:
        num = sanitize_signed_float(value)
        if num is None:
            raise ValueError("position must be a finite number")
        return num

    @field_validator("avg_cost", mode="before")
    @classmethod
    def _avg_cost(cls, value: Any) -> float:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 0.0
        num = sanitize_signed_float(value)
        return 0.0 if num is None else num

    @field_validator("updated_at", mode="before")
    @classmethod
    def _updated_at(cls, value: Any) -> str:
        return normalize_utc_timestamp(value)

    @model_validator(mode="after")
    def _validate_asset_fields(self) -> Self:
        if self.asset_class == "OPT":
            if self.expiry is None:
                raise ValueError("OPT position row requires expiry")
            if self.strike is None or self.strike <= 0.0:
                raise ValueError("OPT position row requires positive strike")
            if self.right not in ("C", "P"):
                raise ValueError("OPT position row requires right ('C' or 'P')")
        else:
            if self.strike == 0.0:
                self.strike = None
            if self.expiry is not None or self.strike is not None or self.right is not None:
                raise ValueError("STK position row must not specify expiry, strike, or right")
        return self


LEDGER_EXECUTION_COLUMNS: tuple[str, ...] = tuple(LedgerExecutionRow.model_fields)
LEDGER_EXECUTION_NON_NULLABLE: tuple[str, ...] = tuple(
    name for name, field in LedgerExecutionRow.model_fields.items() if field.is_required()
)

LEDGER_EXECUTION_POLARS_SCHEMA = pl.Schema(
    {
        "exec_id": pl.Utf8,
        "ib_order_id": pl.Utf8,
        "conid": pl.Int64,
        "underlying_conid": pl.Int64,
        "symbol": pl.Utf8,
        "underlying": pl.Utf8,
        "asset_class": pl.Utf8,
        "expiry": pl.Utf8,
        "strike": pl.Float64,
        "right": pl.Utf8,
        "multiplier": pl.Int64,
        "side": pl.Utf8,
        "quantity": pl.Float64,
        "price": pl.Float64,
        "proceeds": pl.Float64,
        "commission": pl.Float64,
        "realized_pnl_ib": pl.Float64,
        "open_close": pl.Utf8,
        "event_code": pl.Utf8,
        "currency": pl.Utf8,
        "fx_rate_to_base": pl.Float64,
        "executed_at": pl.Utf8,
        "source": pl.Utf8,
    }
)

LEDGER_POSITION_COLUMNS: tuple[str, ...] = tuple(LedgerPositionRow.model_fields)
LEDGER_POSITION_NON_NULLABLE: tuple[str, ...] = tuple(
    name for name, field in LedgerPositionRow.model_fields.items() if field.is_required()
)

LEDGER_POSITION_POLARS_SCHEMA = pl.Schema(
    {
        "conid": pl.Int64,
        "symbol": pl.Utf8,
        "underlying": pl.Utf8,
        "asset_class": pl.Utf8,
        "expiry": pl.Utf8,
        "strike": pl.Float64,
        "right": pl.Utf8,
        "multiplier": pl.Int64,
        "position": pl.Float64,
        "avg_cost": pl.Float64,
        "mark_price": pl.Float64,
        "market_value": pl.Float64,
        "unrealized_pnl": pl.Float64,
        "source": pl.Utf8,
        "updated_at": pl.Utf8,
    }
)
