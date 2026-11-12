"""Convert raw `ib_async.Ticker` + qualified `Option` metadata into `OptionContractRow`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from src.broker.contracts import parse_ib_expiry
from src.storage.schema import (
    GreeksSource,
    OptionContractRow,
    sanitize_count,
    sanitize_price,
)

MAX_IV = 10.0
MAX_ABS_DELTA = 1.001


@dataclass(frozen=True)
class SnapshotContext:
    """Shared snapshot-level metadata applied to every parsed option row."""

    snapshot_id: str
    snapshot_ts_utc: str
    market_date: str
    market_data_type: int
    underlying: str
    underlying_conid: int
    underlying_price: float


def normalize_expiry(raw: str | date) -> str:
    """Normalize IBKR `YYYYMMDD` or ISO `YYYY-MM-DD` to `YYYY-MM-DD`."""
    if isinstance(raw, date):
        return raw.isoformat()
    text = raw.strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return date.fromisoformat(text).isoformat()
    parsed = parse_ib_expiry(text)
    if parsed is None:
        raise ValueError(f"Cannot parse option expiry: {raw!r}")
    return parsed.isoformat()


def normalize_right(raw: str) -> Literal["C", "P"]:
    upper = raw.strip().upper()
    if upper in ("C", "CALL"):
        return "C"
    if upper in ("P", "PUT"):
        return "P"
    raise ValueError(f"Option right must be 'C' or 'P', got {raw!r}")


def has_real_quote(ticker: Any) -> bool:
    """Return True if the contract has a current quote: a valid positive `bid` or `ask`.

    `last` and `close` do not count (`close` is the previous session). A zero or `-1` bid is an
    IBKR "no bid" value, so a far out-of-the-money option with bid `0` and a valid ask still
    counts as quoted.
    """
    return any(sanitize_price(getattr(ticker, attr, None)) is not None for attr in ("bid", "ask"))


def has_usable_quote(ticker: Any) -> bool:
    """Return True if at least one of `bid`, `ask`, `last`, or `close` is a valid positive price."""
    return any(
        sanitize_price(getattr(ticker, attr, None)) is not None
        for attr in ("bid", "ask", "last", "close")
    )


def extract_open_interest(ticker: Any, right: Literal["C", "P"]) -> int | None:
    """Extract tick `101` Open Interest (`callOpenInterest` for C, `putOpenInterest` for P)."""
    primary_attr = "callOpenInterest" if right == "C" else "putOpenInterest"
    oi = sanitize_count(getattr(ticker, primary_attr, None))
    if oi is not None:
        return oi
    return sanitize_count(getattr(ticker, "openInterest", None))


def extract_model_greeks(
    ticker: Any,
) -> tuple[float | None, float | None, float | None, float | None, float | None, GreeksSource]:
    """Extract and validate IBKR `modelGreeks`.

    Returns `(iv, delta, gamma, theta, vega, greeks_source)`.
    If `modelGreeks` is missing or any core bound (`0 < iv <= 10.0`,
    `abs(delta) <= 1.001`, `gamma >= 0`) fails, returns all-`None` with `"NONE"`
    so Phase 4's vectorized BSM solver can populate them.
    """
    mg = getattr(ticker, "modelGreeks", None)
    if mg is None:
        return (None, None, None, None, None, "NONE")

    iv = sanitize_price(getattr(mg, "impliedVol", None))
    if iv is None or iv <= 0.0 or iv > MAX_IV:
        return (None, None, None, None, None, "NONE")

    raw_delta = getattr(mg, "delta", None)
    raw_gamma = getattr(mg, "gamma", None)
    if raw_delta is None or raw_gamma is None:
        return (None, None, None, None, None, "NONE")
    try:
        delta_f = float(raw_delta)
        gamma_f = float(raw_gamma)
    except (TypeError, ValueError):
        return (None, None, None, None, None, "NONE")

    # Reject NaN / inf / sentinel / out-of-range delta or gamma
    if (
        delta_f != delta_f
        or gamma_f != gamma_f
        or abs(delta_f) > MAX_ABS_DELTA
        or gamma_f < 0.0
        or gamma_f >= 1e9
    ):
        return (None, None, None, None, None, "NONE")

    delta = max(-1.0, min(1.0, delta_f))
    gamma = gamma_f

    theta = _optional_greek(getattr(mg, "theta", None))
    vega = _optional_greek(getattr(mg, "vega", None))
    return (iv, delta, gamma, theta, vega, "IBKR_MODEL")


def _optional_greek(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val or abs(val) >= 1e9:
        return None
    return val


def has_live_opra_data(ticker: Any, right: Literal["C", "P"] | None = None) -> bool:
    """Return True when quote, `modelGreeks`, and tick `101` Open Interest are all populated."""
    if not has_usable_quote(ticker):
        return False
    _, _, _, _, _, source = extract_model_greeks(ticker)
    if source != "IBKR_MODEL":
        return False
    contract = getattr(ticker, "contract", None)
    resolved_right: Literal["C", "P"] = (
        right if right is not None else normalize_right(getattr(contract, "right", "C") or "C")
    )
    return extract_open_interest(ticker, resolved_right) is not None


class TickerParser:
    """Stateless/contextual parser converting `ib_async.Ticker` objects into `OptionContractRow`."""

    def __init__(self, context: SnapshotContext | None = None) -> None:
        self.context = context

    @classmethod
    def parse_ticker(
        cls,
        ticker: Any,
        *,
        contract: Any | None = None,
        context: SnapshotContext | None = None,
        snapshot_id: str | None = None,
        snapshot_ts_utc: str | None = None,
        market_date: str | None = None,
        market_data_type: int | None = None,
        underlying: str | None = None,
        underlying_conid: int | None = None,
        underlying_price: float | None = None,
        dte: int | None = None,
        expiry: str | date | None = None,
    ) -> OptionContractRow:
        """Build a validated `OptionContractRow` from an `ib_async.Ticker` and option metadata."""
        opt = contract if contract is not None else getattr(ticker, "contract", None)
        if opt is None:
            raise ValueError("Option contract metadata must be provided on ticker or via contract=")

        sid = snapshot_id or (context.snapshot_id if context else None)
        ts_utc = snapshot_ts_utc or (context.snapshot_ts_utc if context else None)
        m_date = market_date or (context.market_date if context else None)
        m_type = (
            market_data_type
            if market_data_type is not None
            else (context.market_data_type if context else None)
        )
        und = underlying or (context.underlying if context else getattr(opt, "symbol", None))
        und_conid = (
            underlying_conid
            if underlying_conid is not None
            else (context.underlying_conid if context else None)
        )
        spot = (
            underlying_price
            if underlying_price is not None
            else (context.underlying_price if context else None)
        )

        if (
            sid is None
            or ts_utc is None
            or m_date is None
            or m_type is None
            or und is None
            or und_conid is None
            or spot is None
        ):
            raise ValueError("Missing required snapshot context fields for TickerParser")

        raw_expiry = (
            expiry if expiry is not None else getattr(opt, "lastTradeDateOrContractMonth", "")
        )
        iso_expiry = normalize_expiry(raw_expiry)
        resolved_dte = (
            dte
            if dte is not None
            else (date.fromisoformat(iso_expiry) - date.fromisoformat(m_date)).days
        )

        strike = float(getattr(opt, "strike", 0.0))
        right = normalize_right(str(getattr(opt, "right", "")))
        option_conid = int(getattr(opt, "conId", 0))
        moneyness_pct = ((strike - spot) / spot) * 100.0 if spot > 0 else 0.0

        bid = sanitize_price(getattr(ticker, "bid", None))
        ask = sanitize_price(getattr(ticker, "ask", None))
        last = sanitize_price(getattr(ticker, "last", None))
        close = sanitize_price(getattr(ticker, "close", None))
        volume = sanitize_count(getattr(ticker, "volume", None))
        open_interest = extract_open_interest(ticker, right)
        iv, delta, gamma, theta, vega, greeks_source = extract_model_greeks(ticker)

        return OptionContractRow(
            snapshot_id=sid,
            snapshot_ts_utc=ts_utc,
            market_date=m_date,
            market_data_type=m_type,
            underlying=und,
            underlying_conid=und_conid,
            underlying_price=spot,
            option_conid=option_conid,
            expiry=iso_expiry,
            dte=resolved_dte,
            strike=strike,
            moneyness_pct=moneyness_pct,
            right=right,
            bid=bid,
            ask=ask,
            last=last,
            close=close,
            volume=volume,
            open_interest=open_interest,
            iv=iv,
            delta=delta,
            gamma=gamma,
            theta=theta,
            vega=vega,
            greeks_source=greeks_source,
        )

    def parse(
        self,
        ticker: Any,
        *,
        contract: Any | None = None,
        dte: int | None = None,
        expiry: str | date | None = None,
    ) -> OptionContractRow:
        """Instance convenience wrapper using `self.context`."""
        return self.parse_ticker(
            ticker,
            contract=contract,
            context=self.context,
            dte=dte,
            expiry=expiry,
        )
