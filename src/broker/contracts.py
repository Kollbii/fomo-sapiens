"""Underlying qualification, spot resolution, DTE bucket matching and strike striding."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ib_async import Stock

from src.broker.client import IBKRBrokerClient, SleepFn, poll_until
from src.core.config import Settings
from src.core.exceptions import (
    InvalidTickerError,
    OptionChainFetchError,
    UnderlyingPriceUnavailableError,
)
from src.core.security import TickerValidator
from src.storage.schema import sanitize_price

logger = logging.getLogger(__name__)

US_EASTERN = ZoneInfo("America/New_York")
_EPS = 1e-9


class OptionChainLike(Protocol):
    exchange: str
    tradingClass: str  # noqa: N815 - mirrors ib_async attribute name
    multiplier: str
    expirations: Sequence[str]
    strikes: Sequence[float]


@dataclass(frozen=True)
class ExpirationMatch:
    ib_expiry: str  # IBKR format YYYYMMDD
    expiry: date
    dte: int
    target_dte_bucket: int


@dataclass(frozen=True)
class StrikeSelection:
    strikes: tuple[float, ...]
    atm_strike: float
    requested_step: float
    effective_step: float

    @property
    def widened(self) -> bool:
        return self.effective_step > self.requested_step + _EPS


@dataclass(frozen=True)
class UnderlyingInfo:
    symbol: str
    conid: int
    contract: Any
    spot: float


@dataclass(frozen=True)
class ChainPlan:
    underlying: UnderlyingInfo
    market_date: date
    exchange: str
    trading_class: str
    multiplier: str
    expirations: tuple[ExpirationMatch, ...]
    strikes: StrikeSelection
    range_pct: float
    notices: tuple[str, ...] = ()

    @property
    def total_contracts(self) -> int:
        return len(self.expirations) * len(self.strikes.strikes) * 2

    @property
    def bucket_by_expiry(self) -> dict[str, int]:
        return {m.ib_expiry: m.target_dte_bucket for m in self.expirations}


# --------------------------------------------------------------------- pure helpers
def parse_ib_expiry(raw: str) -> date | None:
    """Parse IBKR `YYYYMMDD` (optionally followed by a time) into a date."""
    text = raw.strip()[:8]
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def market_date_for(now: datetime) -> date:
    """US Eastern session date for an aware timestamp."""
    return now.astimezone(US_EASTERN).date()


def match_expirations(
    expirations: Iterable[str],
    market_date: date,
    dte_min: int,
    dte_max: int,
    tolerance: int,
    buckets: Sequence[int],
) -> list[ExpirationMatch]:
    """Match the closest expiration in `[dte_min - tol, dte_max + tol]` to each target bucket.

    Ties prefer expirations inside the nominal `[dte_min, dte_max)` window, then the
    nearer date. An expiration claimed by several buckets is kept once, labelled with the
    bucket closest to its DTE.
    """
    lo, hi = dte_min - tolerance, dte_max + tolerance
    candidates: dict[str, tuple[date, int]] = {}
    for raw in expirations:
        parsed = parse_ib_expiry(raw)
        if parsed is None:
            continue
        dte = (parsed - market_date).days
        if lo <= dte <= hi:
            candidates[parsed.strftime("%Y%m%d")] = (parsed, dte)
    if not candidates:
        return []

    claims: dict[str, list[int]] = {}
    for bucket in buckets:
        best = min(
            candidates.items(),
            key=lambda kv: (
                abs(kv[1][1] - bucket),
                not (dte_min <= kv[1][1] < dte_max),
                kv[1][1],
            ),
        )
        claims.setdefault(best[0], []).append(bucket)

    matches = []
    for ib_expiry, claiming in claims.items():
        expiry, dte = candidates[ib_expiry]
        bucket = min(claiming, key=lambda b: (abs(dte - b), b))
        matches.append(ExpirationMatch(ib_expiry, expiry, dte, bucket))
    return sorted(matches, key=lambda m: m.dte)


def _stride(window: Sequence[float], atm: float, step: float) -> list[float]:
    selected = [atm]
    last = atm
    for strike in (s for s in window if s > atm):
        if strike - last >= step - _EPS:
            selected.append(strike)
            last = strike
    last = atm
    for strike in (s for s in reversed(window) if s < atm):
        if last - strike >= step - _EPS:
            selected.append(strike)
            last = strike
    return sorted(selected)


def select_strikes(
    available: Iterable[float],
    spot: float,
    range_pct: float,
    strike_step: float,
    max_strikes: int | None = None,
) -> StrikeSelection:
    """Moneyness window + ATM-anchored stride, widening the step until `<= max_strikes`."""
    if spot <= 0 or not 0 < range_pct <= 1 or strike_step <= 0:
        raise ValueError("spot, range_pct (0, 1] and strike_step must be positive")
    if max_strikes is not None and max_strikes < 1:
        raise ValueError("max_strikes must be >= 1")
    lo, hi = spot * (1 - range_pct) - _EPS, spot * (1 + range_pct) + _EPS
    window = sorted({float(s) for s in available if s > 0 and lo <= s <= hi})
    if not window:
        raise OptionChainFetchError(f"No strikes within ±{range_pct:.0%} of spot {spot:.2f}")
    atm = min(window, key=lambda s: (abs(s - spot), s))

    multiplier = 1
    selected = _stride(window, atm, strike_step)
    while max_strikes is not None and len(selected) > max_strikes:
        multiplier += 1
        selected = _stride(window, atm, strike_step * multiplier)
    return StrikeSelection(tuple(selected), atm, strike_step, strike_step * multiplier)


def select_chain(chains: Sequence[OptionChainLike], symbol: str) -> OptionChainLike:
    """Primary SMART chain: multiplier 100 preferred, then largest expiration x strike grid."""
    smart = [c for c in chains if c.exchange == "SMART"]
    if not smart:
        raise OptionChainFetchError(f"No SMART option chain for {symbol}")
    preferred = [c for c in smart if c.multiplier == "100"] or smart
    return max(
        preferred,
        key=lambda c: (len(c.expirations) * len(c.strikes), c.tradingClass == symbol),
    )


def extract_spot(ticker: Any) -> float | None:
    """Reference price: last -> mid (bid/ask) -> close."""
    last = sanitize_price(getattr(ticker, "last", None))
    if last is not None:
        return last
    bid = sanitize_price(getattr(ticker, "bid", None))
    ask = sanitize_price(getattr(ticker, "ask", None))
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return sanitize_price(getattr(ticker, "close", None))


# --------------------------------------------------------------------- service
class ContractDiscoveryService:
    def __init__(
        self,
        client: IBKRBrokerClient,
        settings: Settings | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self.client = client
        self.settings = settings or client.settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep

    @property
    def max_total_contracts(self) -> int:
        oc = self.settings.options_chain
        return oc.max_contracts_per_page * oc.max_pages_per_refresh

    async def qualify_underlying(self, ticker: str) -> Any:
        symbol = TickerValidator.validate(ticker)
        results = await self.client.ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD"))
        valid = [
            c
            for c in results
            if c is not None and not isinstance(c, list) and int(getattr(c, "conId", 0) or 0) > 0
        ]
        if len(valid) != 1 or getattr(valid[0], "secType", "STK") != "STK":
            raise InvalidTickerError(f"{symbol} is not a uniquely qualified US stock")
        return valid[0]

    async def _snapshot_spot(self, contract: Any) -> float | None:
        oc = self.settings.options_chain
        ib = self.client.ib
        ticker = ib.reqMktData(contract, "", False, False)
        try:
            await poll_until(
                lambda: extract_spot(ticker) is not None,
                oc.page_collect_timeout_sec,
                oc.page_poll_interval_sec,
                self.sleep,
            )
            return extract_spot(ticker)
        finally:
            ib.cancelMktData(contract)

    async def resolve_spot(self, contract: Any) -> float:
        spot = await self._snapshot_spot(contract)
        if spot is None and self.client.can_fallback():
            self.client.switch_to_fallback_data_type()
            spot = await self._snapshot_spot(contract)
        if spot is None or spot <= 0:
            raise UnderlyingPriceUnavailableError(
                f"No usable spot price for {getattr(contract, 'symbol', '?')}"
            )
        return spot

    async def fetch_chain(self, contract: Any) -> OptionChainLike:
        chains = await self.client.ib.reqSecDefOptParamsAsync(
            contract.symbol, "", contract.secType, contract.conId
        )
        return select_chain(list(chains or []), contract.symbol)

    async def build_plan(
        self,
        ticker: str,
        range_pct: float | None = None,
        strike_step: float | None = None,
    ) -> ChainPlan:
        oc = self.settings.options_chain
        rp = oc.default_range_pct if range_pct is None else range_pct
        step = oc.default_strike_step if strike_step is None else strike_step

        contract = await self.qualify_underlying(ticker)
        spot = await self.resolve_spot(contract)
        chain = await self.fetch_chain(contract)

        market_date = market_date_for(self.clock())
        expirations = match_expirations(
            chain.expirations,
            market_date,
            oc.dte_min,
            oc.dte_max,
            oc.dte_boundary_tolerance_days,
            oc.target_dte_buckets,
        )
        if not expirations:
            raise OptionChainFetchError(
                f"No expirations within {oc.dte_min}..{oc.dte_max} DTE (±"
                f"{oc.dte_boundary_tolerance_days}) for {contract.symbol}"
            )
        max_strikes = self.max_total_contracts // (2 * len(expirations))
        strikes = select_strikes(chain.strikes, spot, rp, step, max_strikes)

        notices: list[str] = []
        if strikes.widened:
            notices.append(
                f"Strike step widened from ${strikes.requested_step:g} to "
                f"${strikes.effective_step:g} to respect the "
                f"{self.max_total_contracts}-contract cap"
            )
            logger.info(notices[-1])

        return ChainPlan(
            underlying=UnderlyingInfo(contract.symbol, int(contract.conId), contract, spot),
            market_date=market_date,
            exchange="SMART",
            trading_class=chain.tradingClass,
            multiplier=chain.multiplier,
            expirations=tuple(expirations),
            strikes=strikes,
            range_pct=rp,
            notices=tuple(notices),
        )
