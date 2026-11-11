"""Underlying qualification, spot lookup, weekly DTE windowing, and grid-aligned striding."""

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
_CANONICAL_STEPS: tuple[float, ...] = (0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0)


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
) -> list[ExpirationMatch]:
    """Select one weekly expiration for every ISO week strictly inside `(dte_min, dte_max)`.

    When multiple expirations share an ISO calendar week (for instance Mon/Wed/Fri ETF
    listings), standard Friday expirations (`weekday == 4`) take precedence; otherwise the
    latest expiration in that week is selected (covering market-holiday Thursdays).
    """
    by_week: dict[tuple[int, int], list[tuple[str, date, int]]] = {}
    seen_dates: set[str] = set()
    for raw in expirations:
        parsed = parse_ib_expiry(raw)
        if parsed is None:
            continue
        ib_exp = parsed.strftime("%Y%m%d")
        if ib_exp in seen_dates:
            continue
        dte = (parsed - market_date).days
        if dte_min < dte < dte_max:
            seen_dates.add(ib_exp)
            iso = parsed.isocalendar()
            by_week.setdefault((iso.year, iso.week), []).append((ib_exp, parsed, dte))

    matches: list[ExpirationMatch] = []
    for week_items in by_week.values():
        ib_exp, expiry, dte = max(
            week_items,
            key=lambda item: (item[1].weekday() == 4, item[1]),
        )
        matches.append(ExpirationMatch(ib_expiry=ib_exp, expiry=expiry, dte=dte))
    return sorted(matches, key=lambda m: m.dte)


def _is_grid_multiple(strike: float, step: float) -> bool:
    ratio = strike / step
    return abs(ratio - round(ratio)) < 1e-6


def _pool_for_step(window: Sequence[float], spot: float, step: float) -> list[float]:
    """Prefer strikes aligned to `step` multiples so weekly/monthly OPRA grids qualify cleanly."""
    aligned = [s for s in window if _is_grid_multiple(s, step)]
    if len(aligned) >= 2:
        diffs = [
            round(b - a, 4) for a, b in zip(aligned, aligned[1:], strict=False) if b - a > 1e-6
        ]
        min_aligned_gap = min(diffs) if diffs else step
        expected = max(1, int((window[-1] - window[0]) / step))
        min_coverage = max(2, min(len(window), int(expected * 0.65)))
        closest_aligned = min(aligned, key=lambda s: (abs(s - spot), s))
        if (
            min_aligned_gap <= step + _EPS
            and len(aligned) >= min_coverage
            and abs(closest_aligned - spot) <= max(step, 0.05 * spot)
        ):
            return aligned
    return list(window)


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


def _widening_sequence(base_step: float) -> list[float]:
    steps = [s for s in _CANONICAL_STEPS if s > base_step + _EPS]
    multiplier = 2
    tail_base = max(base_step, _CANONICAL_STEPS[-1])
    while len(steps) < 16:
        candidate = base_step * multiplier
        if candidate > tail_base + _EPS and all(abs(candidate - s) > _EPS for s in steps):
            steps.append(candidate)
        multiplier += 1
    return steps


def choose_auto_strike_step(
    available: Iterable[float],
    spot: float,
    range_pct: float,
    max_strikes: int | None = None,
) -> float:
    """Choose the finest canonical exchange step that fits inside `min(28, max_strikes)`."""
    if spot <= 0 or not 0 < range_pct <= 1:
        raise ValueError("spot and range_pct must be positive")
    lo, hi = spot * (1 - range_pct) - _EPS, spot * (1 + range_pct) + _EPS
    window = sorted({float(s) for s in available if s > 0 and lo <= s <= hi})
    if len(window) < 2:
        return 5.0
    diffs = [round(b - a, 4) for a, b in zip(window, window[1:], strict=False) if b - a > 1e-6]
    min_avail = min(diffs) if diffs else 1.0
    target_cap = min(28, max_strikes) if max_strikes is not None else 28
    candidates = [s for s in _CANONICAL_STEPS if s >= min_avail - _EPS] or [_CANONICAL_STEPS[-1]]
    for cand in candidates:
        is_fractional_step = cand < 1.0 or not _is_grid_multiple(cand, 1.0)
        if is_fractional_step and not any(
            _is_grid_multiple(s, cand) and not _is_grid_multiple(s, cand * 2.0) for s in window
        ):
            continue
        sel = select_strikes(window, spot=spot, range_pct=range_pct, strike_step=cand)
        if len(sel.strikes) <= target_cap:
            return cand
    return candidates[-1]


def select_strikes(
    available: Iterable[float],
    spot: float,
    range_pct: float,
    strike_step: float,
    max_strikes: int | None = None,
) -> StrikeSelection:
    """Moneyness window + grid-aligned ATM stride, widening along canonical steps if capped."""
    if spot <= 0 or not 0 < range_pct <= 1 or strike_step <= 0:
        raise ValueError("spot, range_pct (0, 1] and strike_step must be positive")
    if max_strikes is not None and max_strikes < 1:
        raise ValueError("max_strikes must be >= 1")
    lo, hi = spot * (1 - range_pct) - _EPS, spot * (1 + range_pct) + _EPS
    window = sorted({float(s) for s in available if s > 0 and lo <= s <= hi})
    if not window:
        raise OptionChainFetchError(f"No strikes within ±{range_pct:.0%} of spot {spot:.2f}")

    def _run_step(step_val: float) -> tuple[list[float], float]:
        pool = _pool_for_step(window, spot, step_val)
        anchor = min(pool, key=lambda s: (abs(s - spot), s))
        return _stride(pool, anchor, step_val), anchor

    effective_step = float(strike_step)
    selected, atm = _run_step(effective_step)
    if max_strikes is not None and len(selected) > max_strikes:
        for cand_step in _widening_sequence(strike_step):
            selected, atm = _run_step(cand_step)
            effective_step = cand_step
            if len(selected) <= max_strikes:
                break
    return StrikeSelection(tuple(selected), atm, float(strike_step), effective_step)


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

        contract = await self.qualify_underlying(ticker)
        spot = await self.resolve_spot(contract)
        chain = await self.fetch_chain(contract)

        market_date = market_date_for(self.clock())
        expirations = match_expirations(
            chain.expirations,
            market_date,
            oc.dte_min,
            oc.dte_max,
        )
        if not expirations:
            raise OptionChainFetchError(
                f"No weekly expirations in ({oc.dte_min}, {oc.dte_max}) DTE for {contract.symbol}"
            )
        max_strikes = self.max_total_contracts // (2 * len(expirations))
        notices: list[str] = []
        if strike_step is None:
            step = choose_auto_strike_step(chain.strikes, spot, rp, max_strikes=max_strikes)
            notices.append(
                f"Auto strike step selected ${step:g} for {contract.symbol} spot ${spot:.2f}"
            )
        else:
            step = float(strike_step)
        strikes = select_strikes(chain.strikes, spot, rp, step, max_strikes)

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
