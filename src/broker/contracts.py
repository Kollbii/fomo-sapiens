"""Underlying qualification, spot lookup, weekly DTE windowing, and grid-aligned striding."""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
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
class ExpirationSpec:
    match: ExpirationMatch
    trading_class: str
    multiplier: str
    strikes: tuple[float, ...]
    effective_step: float
    exchange_window_count: int


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
    expiration_specs: tuple[ExpirationSpec, ...] = ()
    exchange_validated_strike_count: int = 0
    available_strike_steps: tuple[float, ...] = ()
    available_range_pcts: tuple[float, ...] = ()
    exchange_native_verified: bool = True

    @property
    def total_contracts(self) -> int:
        if self.expiration_specs:
            return sum(len(spec.strikes) * 2 for spec in self.expiration_specs)
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
    """Select one weekly expiration per ISO week inside `[dte_min, dte_max)` (inclusive lower).

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
        if dte_min <= dte < dte_max:
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


def detect_native_strike_step(strikes: Iterable[float]) -> float:
    """Return the modal positive strike increment from `strikes` (or min diff on ties)."""
    ordered = sorted({float(s) for s in strikes if s > 0})
    diffs = [round(b - a, 4) for a, b in zip(ordered, ordered[1:], strict=False) if b - a > 1e-6]
    if not diffs:
        return 5.0
    counts = Counter(diffs)
    return float(min(counts, key=lambda d: (-counts[d], d)))


def detect_available_strike_steps(
    available: Iterable[float],
    spot: float,
    range_pct: float,
    candidate_steps: Sequence[float] | None = None,
) -> list[float]:
    """Identify candidate strike steps that match exchange strikes in `+-range_pct`."""
    if spot <= 0 or not 0 < range_pct <= 1:
        return list(candidate_steps or ())
    lo, hi = spot * (1 - range_pct) - _EPS, spot * (1 + range_pct) + _EPS
    window = sorted({float(s) for s in available if s > 0 and lo <= s <= hi})
    if len(window) < 2:
        return [detect_native_strike_step(available)]

    diffs = [round(b - a, 4) for a, b in zip(window, window[1:], strict=False) if b - a > 1e-6]
    min_gap = min(diffs) if diffs else 5.0
    native_modal = detect_native_strike_step(window)
    pool_steps = list(candidate_steps) if candidate_steps is not None else list(_CANONICAL_STEPS)
    if all(abs(native_modal - s) > 1e-4 for s in pool_steps):
        pool_steps.append(native_modal)

    supported: list[float] = []
    for cand in sorted({float(s) for s in pool_steps if s > 0}):
        if cand + _EPS < min_gap:
            continue
        is_fractional = cand < 1.0 or not _is_grid_multiple(cand, 1.0)
        if is_fractional and not any(
            _is_grid_multiple(s, cand) and not _is_grid_multiple(s, cand * 2.0) for s in window
        ):
            continue
        aligned = [s for s in window if _is_grid_multiple(s, cand)]
        if len(aligned) >= 2:
            aligned_diffs = [
                round(b - a, 4) for a, b in zip(aligned, aligned[1:], strict=False) if b - a > 1e-6
            ]
            if aligned_diffs and min(aligned_diffs) <= cand * 1.5 + _EPS:
                supported.append(cand)
                continue
        if abs(cand - native_modal) <= 1e-4:
            supported.append(cand)

    return supported or [native_modal]


def detect_available_range_pcts(
    available: Iterable[float],
    spot: float,
    candidate_ranges: Sequence[float],
) -> list[float]:
    """Return candidate moneyness ranges covering at least 2 exchange-valid strikes."""
    if spot <= 0:
        return list(candidate_ranges)
    clean = sorted({float(s) for s in available if s > 0})
    supported: list[float] = []
    for rp in sorted({float(r) for r in candidate_ranges if 0 < r <= 1}):
        lo, hi = spot * (1 - rp) - _EPS, spot * (1 + rp) + _EPS
        if sum(1 for s in clean if lo <= s <= hi) >= 2:
            supported.append(rp)
    return supported or [max(candidate_ranges)]


def select_strikes(
    available: Iterable[float],
    spot: float,
    range_pct: float,
    strike_step: float,
    max_strikes: int | None = None,
) -> StrikeSelection:
    """Intersect candidate strike grid with exchange-valid strikes and adapt to native steps."""
    if spot <= 0 or not 0 < range_pct <= 1 or strike_step <= 0:
        raise ValueError("spot, range_pct (0, 1] and strike_step must be positive")
    if max_strikes is not None and max_strikes < 1:
        raise ValueError("max_strikes must be >= 1")
    lo, hi = spot * (1 - range_pct) - _EPS, spot * (1 + range_pct) + _EPS
    window = sorted({float(s) for s in available if s > 0 and lo <= s <= hi})
    if not window:
        raise OptionChainFetchError(f"No strikes within ±{range_pct:.0%} of spot {spot:.2f}")
    exchange_lookup = {round(s, 6): s for s in window}

    def _run_step(step_val: float) -> tuple[list[float], float]:
        pool = _pool_for_step(window, spot, step_val)
        anchor = min(pool, key=lambda s: (abs(s - spot), s))
        candidates = _stride(pool, anchor, step_val)
        intersected = [
            exchange_lookup[round(s, 6)] for s in candidates if round(s, 6) in exchange_lookup
        ]
        return (intersected or candidates), anchor

    effective_step = float(strike_step)
    selected, atm = _run_step(effective_step)
    if max_strikes is not None and len(selected) > max_strikes:
        for cand_step in _widening_sequence(strike_step):
            selected, atm = _run_step(cand_step)
            effective_step = cand_step
            if len(selected) <= max_strikes:
                break

    if len(selected) >= 2:
        diffs = [
            round(b - a, 4) for a, b in zip(selected, selected[1:], strict=False) if b - a > 1e-6
        ]
        if diffs and min(diffs) > effective_step + _EPS:
            effective_step = detect_native_strike_step(selected)

    return StrikeSelection(tuple(selected), atm, float(strike_step), effective_step)


def select_smart_chains(chains: Sequence[OptionChainLike], symbol: str) -> list[OptionChainLike]:
    """Return all SMART option chains matching `symbol` (preferring multiplier 100)."""
    smart = [c for c in chains if c.exchange == "SMART"]
    if not smart:
        raise OptionChainFetchError(f"No SMART option chain for {symbol}")
    return [c for c in smart if c.multiplier == "100"] or smart


def select_chain(chains: Sequence[OptionChainLike], symbol: str) -> OptionChainLike:
    """Primary SMART chain: multiplier 100 preferred, then largest expiration x strike grid."""
    preferred = select_smart_chains(chains, symbol)
    return max(
        preferred,
        key=lambda c: (len(c.expirations) * len(c.strikes), c.tradingClass == symbol),
    )


def select_chain_for_expiration(
    chains: Sequence[OptionChainLike], ib_expiry: str, symbol: str
) -> OptionChainLike:
    """Pick the SMART chain descriptor owning `ib_expiry` (`tradingClass` + exchange strikes)."""
    preferred = select_smart_chains(chains, symbol)
    matching = [c for c in preferred if any(exp[:8] == ib_expiry for exp in c.expirations)]
    candidates = matching or preferred
    return max(
        candidates,
        key=lambda c: (c.tradingClass == symbol, len(c.strikes), len(c.expirations)),
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

    async def fetch_chains(self, contract: Any) -> list[OptionChainLike]:
        chains = await self.client.ib.reqSecDefOptParamsAsync(
            contract.symbol, "", contract.secType, contract.conId
        )
        return select_smart_chains(list(chains or []), contract.symbol)

    async def fetch_chain(self, contract: Any) -> OptionChainLike:
        return select_chain(await self.fetch_chains(contract), contract.symbol)

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
        smart_chains = await self.fetch_chains(contract)
        primary_chain = select_chain(smart_chains, contract.symbol)

        market_date = market_date_for(self.clock())
        raw_expirations = [exp for ch in smart_chains for exp in ch.expirations]
        expirations = match_expirations(
            raw_expirations,
            market_date,
            oc.dte_min,
            oc.dte_max,
        )
        if not expirations:
            raise OptionChainFetchError(
                f"No weekly expirations in [{oc.dte_min}, {oc.dte_max}) DTE for {contract.symbol}"
            )

        max_strikes = self.max_total_contracts // (2 * len(expirations))
        notices: list[str] = []
        all_exchange_strikes = sorted(
            {float(s) for ch in smart_chains for s in ch.strikes if s > 0}
        )
        if strike_step is None:
            step = choose_auto_strike_step(all_exchange_strikes, spot, rp, max_strikes=max_strikes)
            notices.append(
                f"Auto strike step selected ${step:g} for {contract.symbol} spot ${spot:.2f}"
            )
        else:
            step = float(strike_step)

        lo, hi = spot * (1 - rp) - _EPS, spot * (1 + rp) + _EPS
        specs: list[ExpirationSpec] = []
        for exp in expirations:
            exp_chain = select_chain_for_expiration(smart_chains, exp.ib_expiry, contract.symbol)
            exp_sel = select_strikes(exp_chain.strikes, spot, rp, step, max_strikes)
            exp_window_count = sum(
                1 for s in {float(v) for v in exp_chain.strikes} if lo <= s <= hi
            )
            specs.append(
                ExpirationSpec(
                    match=exp,
                    trading_class=exp_chain.tradingClass,
                    multiplier=exp_chain.multiplier,
                    strikes=exp_sel.strikes,
                    effective_step=exp_sel.effective_step,
                    exchange_window_count=exp_window_count,
                )
            )

        union_strikes = tuple(sorted({s for spec in specs for s in spec.strikes}))
        if not union_strikes:
            raise OptionChainFetchError(
                f"No exchange-valid strikes within ±{rp:.0%} of spot {spot:.2f}"
            )
        step_counts = Counter(spec.effective_step for spec in specs)
        overall_step = float(min(step_counts, key=lambda st: (-step_counts[st], st)))
        atm_strike = min(union_strikes, key=lambda s: (abs(s - spot), s))
        strikes = StrikeSelection(
            strikes=union_strikes,
            atm_strike=atm_strike,
            requested_step=step,
            effective_step=overall_step,
        )

        if strikes.widened or any(abs(spec.effective_step - step) > _EPS for spec in specs):
            adapted_vals = sorted({spec.effective_step for spec in specs})
            adapted_str = ", ".join(f"${v:g}" for v in adapted_vals)
            notices.append(
                f"Adapted strike step from requested ${strikes.requested_step:g} to "
                f"exchange-native {adapted_str} for {contract.symbol} "
                f"(cap {self.max_total_contracts} contracts)"
            )
            logger.info(notices[-1])

        avail_steps = tuple(
            detect_available_strike_steps(
                all_exchange_strikes,
                spot,
                rp,
                candidate_steps=oc.allowed_strike_steps,
            )
        )
        avail_ranges = tuple(
            detect_available_range_pcts(
                all_exchange_strikes,
                spot,
                oc.allowed_range_pcts,
            )
        )
        notices.append(
            f"Exchange-native verified {len(union_strikes)} strikes across "
            f"{len(specs)} expiration(s) via reqSecDefOptParams."
        )

        return ChainPlan(
            underlying=UnderlyingInfo(contract.symbol, int(contract.conId), contract, spot),
            market_date=market_date,
            exchange="SMART",
            trading_class=primary_chain.tradingClass,
            multiplier=primary_chain.multiplier,
            expirations=tuple(expirations),
            strikes=strikes,
            range_pct=rp,
            notices=tuple(notices),
            expiration_specs=tuple(specs),
            exchange_validated_strike_count=len(union_strikes),
            available_strike_steps=avail_steps,
            available_range_pcts=avail_ranges,
            exchange_native_verified=True,
        )
