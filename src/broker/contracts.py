"""Underlying qualification, spot lookup, weekly DTE windowing, and grid-aligned striding."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Protocol

from ib_async import Option, Stock

from src.broker.client import IBKRBrokerClient, SleepFn, poll_until
from src.core.calendar import et_date
from src.core.config import Settings
from src.core.exceptions import (
    GatewayConnectionError,
    GatewayTimeoutError,
    InvalidTickerError,
    OptionChainFetchError,
    UnderlyingPriceUnavailableError,
)
from src.core.security import TickerValidator
from src.storage.schema import SpotSource, sanitize_price

logger = logging.getLogger(__name__)
_EPS = 1e-9
_CANONICAL_STEPS: tuple[float, ...] = (0.5, 1.0, 2.0, 2.5, 5.0, 10.0, 20.0, 25.0, 50.0, 100.0)
EXACT_PREFETCH_MAX_CONCURRENCY = 3


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
    spot_source: SpotSource | None = None


@dataclass(frozen=True)
class ExpirationSpec:
    match: ExpirationMatch
    trading_class: str
    multiplier: str
    strikes: tuple[float, ...]
    effective_step: float
    exchange_window_count: int
    qualified_contracts: dict[tuple[float, str], Any] = field(default_factory=dict)
    exact_strikes_verified: bool = False


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
    smart_chains: tuple[OptionChainLike, ...] = field(default=(), compare=False, repr=False)

    @property
    def total_contracts(self) -> int:
        if self.expiration_specs:
            total = 0
            for spec in self.expiration_specs:
                if spec.exact_strikes_verified:
                    total += sum(
                        1
                        for s in spec.strikes
                        for r in ("C", "P")
                        if (round(s, 6), r) in spec.qualified_contracts
                    )
                else:
                    total += len(spec.strikes) * 2
            return total
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
    return et_date(now)


def match_expirations(
    expirations: Iterable[str],
    market_date: date,
    dte_min: int,
    dte_max: int,
) -> list[ExpirationMatch]:
    """Select one weekly expiration per ISO week inside `[dte_min, dte_max]` (inclusive).

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
        if dte_min <= dte <= dte_max:
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


def _window_bounds(
    spot: float,
    range_pct: float,
    spot_envelope: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """Compute `[lo, hi]` strike bounds spanning current `spot` and optional historical envelope."""
    low_ref = float(spot)
    high_ref = float(spot)
    if spot_envelope is not None:
        env_lo, env_hi = spot_envelope
        if env_lo > 0.0 and math.isfinite(env_lo):
            low_ref = min(low_ref, float(env_lo))
        if env_hi > 0.0 and math.isfinite(env_hi):
            high_ref = max(high_ref, float(env_hi))
    lo = max(0.01, low_ref * (1.0 - range_pct)) - _EPS
    hi = high_ref * (1.0 + range_pct) + _EPS
    return lo, hi


def choose_auto_strike_step(
    available: Iterable[float],
    spot: float,
    range_pct: float,
    max_strikes: int | None = None,
    *,
    spot_envelope: tuple[float, float] | None = None,
) -> float:
    """Choose the finest canonical exchange step that fits inside the available strike cap."""
    if spot <= 0 or not 0 < range_pct <= 1:
        raise ValueError("spot and range_pct must be positive")
    lo, hi = _window_bounds(spot, range_pct, spot_envelope=spot_envelope)
    window = sorted({float(s) for s in available if s > 0 and lo <= s <= hi})
    if len(window) < 2:
        return 5.0
    diffs = [round(b - a, 4) for a, b in zip(window, window[1:], strict=False) if b - a > 1e-6]
    min_avail = min(diffs) if diffs else 1.0
    span_ratio = (hi - lo) / max(spot * 0.50, 1.0)
    scaled_base = max(28, int(math.ceil(28.0 * span_ratio)))
    target_cap = min(scaled_base, max_strikes) if max_strikes is not None else 28
    candidates = [s for s in _CANONICAL_STEPS if s >= min_avail - _EPS] or [_CANONICAL_STEPS[-1]]
    for cand in candidates:
        is_fractional_step = cand < 1.0 or not _is_grid_multiple(cand, 1.0)
        if is_fractional_step and not any(
            _is_grid_multiple(s, cand) and not _is_grid_multiple(s, cand * 2.0) for s in window
        ):
            continue
        sel = select_strikes(
            window,
            spot=spot,
            range_pct=range_pct,
            strike_step=cand,
            spot_envelope=spot_envelope,
        )
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
    *,
    spot_envelope: tuple[float, float] | None = None,
) -> list[float]:
    """Identify candidate strike steps that match exchange strikes in `+-range_pct`."""
    if spot <= 0 or not 0 < range_pct <= 1:
        return list(candidate_steps or ())
    lo, hi = _window_bounds(spot, range_pct, spot_envelope=spot_envelope)
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
    *,
    spot_envelope: tuple[float, float] | None = None,
) -> StrikeSelection:
    """Intersect candidate strike grid with exchange-valid strikes and adapt to native steps."""
    if spot <= 0 or not 0 < range_pct <= 1 or strike_step <= 0:
        raise ValueError("spot, range_pct (0, 1] and strike_step must be positive")
    if max_strikes is not None and max_strikes < 1:
        raise ValueError("max_strikes must be >= 1")
    lo, hi = _window_bounds(spot, range_pct, spot_envelope=spot_envelope)
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


# When both `last` and the bid/ask midpoint exist and differ by more than this fraction, the
# midpoint is used (a stale or bad `last` print must not become the snapshot spot).
SPOT_LAST_MID_MAX_DEVIATION = 0.02


def select_spot(ticker: Any) -> tuple[float, SpotSource] | None:
    """Pick the snapshot spot and report its source.

    Order: `last` (cross-checked against the midpoint), then the bid/ask midpoint, then the
    previous session `close` (flagged `CLOSE`, never usable for history).
    """
    last = sanitize_price(getattr(ticker, "last", None))
    bid = sanitize_price(getattr(ticker, "bid", None))
    ask = sanitize_price(getattr(ticker, "ask", None))
    mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
    if last is not None and mid is not None:
        if abs(last - mid) / mid > SPOT_LAST_MID_MAX_DEVIATION:
            logger.warning(
                "spot last=%.4f deviates more than %.0f%% from mid=%.4f (bid=%.4f ask=%.4f); "
                "using mid",
                last,
                SPOT_LAST_MID_MAX_DEVIATION * 100,
                mid,
                bid,
                ask,
            )
            return mid, "MID"
        return last, "LAST"
    if last is not None:
        return last, "LAST"
    if mid is not None:
        return mid, "MID"
    close = sanitize_price(getattr(ticker, "close", None))
    if close is not None:
        return close, "CLOSE"
    return None


def _spot_fields_complete(ticker: Any) -> bool:
    """True once both `last` and a two-sided bid/ask are present (enables the cross-check)."""
    return all(
        sanitize_price(getattr(ticker, attr, None)) is not None for attr in ("last", "bid", "ask")
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


def _has_running_event_loop() -> bool:
    """Return True when executing inside a live asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _clock_utc(clock: Callable[[], datetime]) -> datetime:
    dt = clock()
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _parse_exact_contracts(
    details: Iterable[Any],
    exp: ExpirationMatch,
    exp_chain: OptionChainLike,
) -> dict[tuple[float, str], Any]:
    """Filter `reqContractDetailsAsync` payloads to valid `OPT` contracts for one expiration."""
    expected_tc = str(getattr(exp_chain, "tradingClass", "") or "")
    expected_mult = str(getattr(exp_chain, "multiplier", "") or "")
    by_key: dict[tuple[float, str], Any] = {}
    for item in details:
        c = item.contract if hasattr(item, "contract") else item
        if c is None or int(getattr(c, "conId", 0) or 0) <= 0:
            continue
        if str(getattr(c, "secType", "OPT")) != "OPT":
            continue
        if str(getattr(c, "lastTradeDateOrContractMonth", ""))[:8] != exp.ib_expiry:
            continue
        if expected_tc and str(getattr(c, "tradingClass", "")) != expected_tc:
            continue
        if expected_mult and str(getattr(c, "multiplier", "")) != expected_mult:
            continue
        right = str(getattr(c, "right", "")).upper()
        strike = float(getattr(c, "strike", 0.0) or 0.0)
        if right in ("C", "P") and strike > 0:
            by_key.setdefault((round(strike, 6), right), c)
    return by_key


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
        self._exact_contracts_cache: dict[
            tuple[str, str, str, str], tuple[datetime, dict[tuple[float, str], Any]]
        ] = {}

    @property
    def max_total_contracts(self) -> int:
        oc = self.settings.options_chain
        return oc.max_contracts_per_page * oc.max_pages_per_refresh

    async def qualify_underlying(self, ticker: str) -> Any:
        symbol = TickerValidator.validate(ticker)
        results = await self.client.run_stage(
            f"underlying qualification ({symbol})",
            self.client.ib.qualifyContractsAsync(Stock(symbol, "SMART", "USD")),
        )
        valid = [
            c
            for c in results
            if c is not None and not isinstance(c, list) and int(getattr(c, "conId", 0) or 0) > 0
        ]
        if len(valid) != 1 or getattr(valid[0], "secType", "STK") != "STK":
            raise InvalidTickerError(f"{symbol} is not a uniquely qualified US stock")
        return valid[0]

    async def _snapshot_spot(self, contract: Any) -> tuple[float, SpotSource] | None:
        oc = self.settings.options_chain
        ib = self.client.ib
        stage = f"spot lookup ({getattr(contract, 'symbol', '?')})"
        since = self.client.link_checkpoint()
        ticker: Any = None
        subscribed = False
        try:
            self.client.ensure_link_alive(stage=stage, since=since)
            ticker = ib.reqMktData(contract, "", False, False)
            subscribed = True
            # Wait for last + bid/ask so the cross-check can run; on timeout take what arrived.
            # An upstream-loss notice ends the wait at once (bounded poll, fast-fail below).
            await poll_until(
                lambda: self.client.link_lost_since(since) or _spot_fields_complete(ticker),
                oc.page_collect_timeout_sec,
                oc.page_poll_interval_sec,
                self.sleep,
            )
            self.client.ensure_still_connected(stage=stage)
            self.client.ensure_link_alive(stage=stage, since=since)
            picked = select_spot(ticker)
            logger.info(
                "spot fields %s: last=%s bid=%s ask=%s close=%s -> %s",
                getattr(contract, "symbol", "?"),
                getattr(ticker, "last", None),
                getattr(ticker, "bid", None),
                getattr(ticker, "ask", None),
                getattr(ticker, "close", None),
                picked,
            )
            return picked
        except Exception as exc:
            self.client.handle_stage_error(exc, stage=stage)
            raise
        finally:
            if subscribed:
                try:
                    ib.cancelMktData(contract)
                except Exception:  # pragma: no cover - defensive
                    logger.exception("cancelMktData failed during %s", stage)

    async def resolve_spot_with_source(self, contract: Any) -> tuple[float, SpotSource]:
        picked = await self._snapshot_spot(contract)
        if picked is None and self.client.can_fallback():
            self.client.switch_to_fallback_data_type()
            picked = await self._snapshot_spot(contract)
        if picked is None or picked[0] <= 0:
            raise UnderlyingPriceUnavailableError(
                f"No usable spot price for {getattr(contract, 'symbol', '?')}"
            )
        return picked

    async def resolve_spot(self, contract: Any) -> float:
        spot, _source = await self.resolve_spot_with_source(contract)
        return spot

    async def fetch_chains(self, contract: Any) -> list[OptionChainLike]:
        chains = await self.client.run_stage(
            f"secdef lookup ({contract.symbol})",
            self.client.ib.reqSecDefOptParamsAsync(
                contract.symbol, "", contract.secType, contract.conId
            ),
        )
        return select_smart_chains(list(chains or []), contract.symbol)

    async def fetch_chain(self, contract: Any) -> OptionChainLike:
        return select_chain(await self.fetch_chains(contract), contract.symbol)

    def _cache_lookup(self, key: tuple[str, str, str, str]) -> dict[tuple[float, str], Any] | None:
        ttl = self.settings.options_chain.contract_details_cache_ttl_sec
        if ttl <= 0:
            return None
        entry = self._exact_contracts_cache.get(key)
        if entry is None:
            return None
        cached_at, payload = entry
        if (_clock_utc(self.clock) - cached_at).total_seconds() > ttl:
            self._exact_contracts_cache.pop(key, None)
            return None
        return payload

    def _cache_store(
        self, key: tuple[str, str, str, str], payload: dict[tuple[float, str], Any]
    ) -> None:
        if payload and self.settings.options_chain.contract_details_cache_ttl_sec > 0:
            self._exact_contracts_cache[key] = (_clock_utc(self.clock), payload)

    async def _query_exact_for_expiration(
        self,
        symbol: str,
        exp: ExpirationMatch,
        exp_chain: OptionChainLike,
        *,
        on_timeout: Callable[[], None] | None = None,
    ) -> dict[tuple[float, str], Any]:
        oc = self.settings.options_chain
        req_fn = getattr(self.client.ib, "reqContractDetailsAsync", None)
        if req_fn is None or not oc.prefetch_exact_strikes:
            return {}
        probe = Option(
            symbol=symbol,
            lastTradeDateOrContractMonth=exp.ib_expiry,
            strike=0.0,
            right="",
            exchange="SMART",
            multiplier=exp_chain.multiplier,
            currency="USD",
            tradingClass=exp_chain.tradingClass,
        )
        stage = f"exact contract prefetch ({symbol} {exp.ib_expiry})"
        try:
            details = await self.client.await_guarded(
                req_fn(probe),
                stage=stage,
                timeout_sec=oc.contract_details_timeout_sec,
            )
            self.client.ensure_still_connected(stage=stage)
        except (GatewayTimeoutError, TimeoutError) as exc:
            if self.client.was_connected and not self.client.is_connected:
                self.client.raise_transport_error(exc, stage=stage)
            if on_timeout is not None:
                on_timeout()
            else:
                logger.warning(
                    "Exact strike prefetch timeout fallback for %s %s: %s",
                    symbol,
                    exp.ib_expiry,
                    exc,
                )
            return {}
        except GatewayConnectionError:
            # Upstream loss / dropped socket: never degrade to the secdef fallback.
            raise
        except (OSError, ConnectionError) as exc:
            self.client.raise_transport_error(exc, stage=stage)
        except Exception as exc:
            if self.client.was_connected and not self.client.is_connected:
                self.client.raise_transport_error(exc, stage=stage)
            logger.warning(
                "Exact strike prefetch fallback for %s %s: %s",
                symbol,
                exp.ib_expiry,
                exc,
            )
            return {}
        return _parse_exact_contracts(details or (), exp, exp_chain)

    async def _prefetch_exact_contracts_by_exp(
        self,
        symbol: str,
        expirations: Sequence[ExpirationMatch],
        smart_chains: Sequence[OptionChainLike],
    ) -> dict[str, tuple[OptionChainLike, dict[tuple[float, str], Any]]]:
        """Resolve exact `ContractDetails` per target expiration with bounded concurrency."""
        oc = self.settings.options_chain
        resolved: dict[str, tuple[OptionChainLike, dict[tuple[float, str], Any]]] = {}
        pending: list[tuple[ExpirationMatch, OptionChainLike, tuple[str, str, str, str]]] = []
        for exp in expirations:
            exp_chain = select_chain_for_expiration(smart_chains, exp.ib_expiry, symbol)
            if not oc.prefetch_exact_strikes:
                resolved[exp.ib_expiry] = (exp_chain, {})
                continue
            cache_key = (symbol, exp_chain.tradingClass, exp_chain.multiplier, exp.ib_expiry)
            cached = self._cache_lookup(cache_key)
            if cached is not None:
                resolved[exp.ib_expiry] = (exp_chain, cached)
                continue
            pending.append((exp, exp_chain, cache_key))

        if pending:
            timed_out = False

            def _trip_timeout() -> None:
                nonlocal timed_out
                if not timed_out:
                    timed_out = True
                    logger.warning(
                        "Exact strike prefetch timed out for %s; skipping remaining expirations "
                        "and falling back to secdef lookup.",
                        symbol,
                    )

            if _has_running_event_loop():
                sem = asyncio.Semaphore(EXACT_PREFETCH_MAX_CONCURRENCY)

                async def _fetch_one(
                    slot_idx: int,
                    exp: ExpirationMatch,
                    exp_chain: OptionChainLike,
                    cache_key: tuple[str, str, str, str],
                ) -> None:
                    if timed_out:
                        resolved[exp.ib_expiry] = (exp_chain, {})
                        return
                    async with sem:
                        if timed_out:
                            resolved[exp.ib_expiry] = (exp_chain, {})
                            return
                        if slot_idx > 0 and oc.contract_details_cooldown_sec > 0:
                            await self.sleep(oc.contract_details_cooldown_sec)
                        if timed_out:
                            resolved[exp.ib_expiry] = (exp_chain, {})
                            return
                        exact_map = await self._query_exact_for_expiration(
                            symbol, exp, exp_chain, on_timeout=_trip_timeout
                        )
                        self._cache_store(cache_key, exact_map)
                        resolved[exp.ib_expiry] = (exp_chain, exact_map)

                tasks = [
                    asyncio.ensure_future(_fetch_one(idx, exp, ec, ck))
                    for idx, (exp, ec, ck) in enumerate(pending)
                ]
                try:
                    await asyncio.gather(*tasks)
                except BaseException:
                    # One failure (e.g. upstream loss) or a cancel stops every sibling request.
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    raise
            else:
                for idx, (exp, exp_chain, cache_key) in enumerate(pending):
                    if timed_out:
                        resolved[exp.ib_expiry] = (exp_chain, {})
                        continue
                    if idx > 0 and oc.contract_details_cooldown_sec > 0:
                        await self.sleep(oc.contract_details_cooldown_sec)
                    if timed_out:
                        resolved[exp.ib_expiry] = (exp_chain, {})
                        continue
                    exact_map = await self._query_exact_for_expiration(
                        symbol, exp, exp_chain, on_timeout=_trip_timeout
                    )
                    self._cache_store(cache_key, exact_map)
                    resolved[exp.ib_expiry] = (exp_chain, exact_map)

        return {exp.ib_expiry: resolved[exp.ib_expiry] for exp in expirations}

    async def build_plan(
        self,
        ticker: str,
        range_pct: float | None = None,
        strike_step: float | None = None,
        dte_min: int | None = None,
        dte_max: int | None = None,
        *,
        underlying_info: UnderlyingInfo | None = None,
        smart_chains: Sequence[OptionChainLike] | None = None,
        market_date: date | None = None,
        spot_envelope: tuple[float, float] | None = None,
    ) -> ChainPlan:
        oc = self.settings.options_chain
        rp = oc.default_range_pct if range_pct is None else range_pct
        eff_dte_min = oc.dte_min if dte_min is None else int(dte_min)
        eff_dte_max = oc.dte_max if dte_max is None else int(dte_max)
        if eff_dte_min > eff_dte_max:
            raise OptionChainFetchError(
                f"Invalid DTE bounds: {eff_dte_min} > {eff_dte_max} for {ticker}"
            )

        if underlying_info is None:
            contract = await self.qualify_underlying(ticker)
            spot, spot_source = await self.resolve_spot_with_source(contract)
            resolved_underlying = UnderlyingInfo(
                contract.symbol, int(contract.conId), contract, spot, spot_source
            )
        else:
            TickerValidator.validate(ticker)
            resolved_underlying = underlying_info
            contract = resolved_underlying.contract
            spot = resolved_underlying.spot

        resolved_chains = (
            list(smart_chains) if smart_chains is not None else await self.fetch_chains(contract)
        )
        primary_chain = select_chain(resolved_chains, resolved_underlying.symbol)

        resolved_market_date = (
            market_date if market_date is not None else market_date_for(self.clock())
        )
        raw_expirations = [exp for ch in resolved_chains for exp in ch.expirations]
        expirations = match_expirations(
            raw_expirations,
            resolved_market_date,
            eff_dte_min,
            eff_dte_max,
        )
        if not expirations:
            raise OptionChainFetchError(
                f"No weekly expirations in [{eff_dte_min}, {eff_dte_max}] DTE for "
                f"{resolved_underlying.symbol}"
            )

        exact_by_exp = await self._prefetch_exact_contracts_by_exp(
            resolved_underlying.symbol, expirations, resolved_chains
        )
        per_exp_strikes: dict[str, list[float]] = {}
        for exp in expirations:
            exp_chain, exact_map = exact_by_exp[exp.ib_expiry]
            per_exp_strikes[exp.ib_expiry] = (
                sorted({strike_key for strike_key, _ in exact_map})
                if exact_map
                else sorted({float(s) for s in exp_chain.strikes if s > 0})
            )

        max_strikes = max(1, self.max_total_contracts // (2 * len(expirations)))
        notices: list[str] = []
        if spot_envelope is not None:
            env_lo, env_hi = spot_envelope
            widened_lo = env_lo > 0.0 and math.isfinite(env_lo) and env_lo < spot - 1e-6
            widened_hi = env_hi > 0.0 and math.isfinite(env_hi) and env_hi > spot + 1e-6
            if widened_lo or widened_hi:
                lo_win, hi_win = _window_bounds(spot, rp, spot_envelope=spot_envelope)
                ref_lo = min(spot, env_lo) if widened_lo else spot
                ref_hi = max(spot, env_hi) if widened_hi else spot
                notices.append(
                    f"Historical spot envelope [${ref_lo:.2f}..${ref_hi:.2f}] expanded strike "
                    f"window to [${max(0.01, lo_win):.2f}..${hi_win:.2f}] for backtest continuity."
                )
        all_exchange_strikes = sorted(
            {s for strikes_list in per_exp_strikes.values() for s in strikes_list}
        )
        if strike_step is None:
            step = choose_auto_strike_step(
                all_exchange_strikes,
                spot,
                rp,
                max_strikes=max_strikes,
                spot_envelope=spot_envelope,
            )
            notices.append(
                f"Auto strike step selected ${step:g} for {resolved_underlying.symbol} "
                f"spot ${spot:.2f}"
            )
        else:
            step = float(strike_step)

        lo, hi = _window_bounds(spot, rp, spot_envelope=spot_envelope)
        specs: list[ExpirationSpec] = []
        for exp in expirations:
            exp_chain, exact_map = exact_by_exp[exp.ib_expiry]
            avail_for_exp = per_exp_strikes[exp.ib_expiry]
            exp_sel = select_strikes(
                avail_for_exp,
                spot,
                rp,
                step,
                max_strikes,
                spot_envelope=spot_envelope,
            )
            exp_window_count = sum(1 for s in avail_for_exp if lo <= s <= hi)
            specs.append(
                ExpirationSpec(
                    match=exp,
                    trading_class=exp_chain.tradingClass,
                    multiplier=exp_chain.multiplier,
                    strikes=exp_sel.strikes,
                    effective_step=exp_sel.effective_step,
                    exchange_window_count=exp_window_count,
                    qualified_contracts=exact_map,
                    exact_strikes_verified=bool(exact_map),
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
                f"exchange-native {adapted_str} for {resolved_underlying.symbol} "
                f"(cap {self.max_total_contracts} contracts)"
            )
            logger.info(notices[-1])

        avail_steps = tuple(
            detect_available_strike_steps(
                all_exchange_strikes,
                spot,
                rp,
                candidate_steps=oc.allowed_strike_steps,
                spot_envelope=spot_envelope,
            )
        )
        avail_ranges = tuple(
            detect_available_range_pcts(
                all_exchange_strikes,
                spot,
                oc.allowed_range_pcts,
            )
        )
        exact_count = sum(1 for spec in specs if spec.exact_strikes_verified)
        if exact_count == len(specs):
            notices.append(
                f"Exchange exact-verified {len(union_strikes)} strikes across "
                f"{len(specs)} expiration(s) via reqContractDetails."
            )
        elif exact_count > 0:
            notices.append(
                f"Exchange verified {len(union_strikes)} strikes across {len(specs)} "
                f"expiration(s) ({exact_count}/{len(specs)} exact via reqContractDetails, "
                "remainder via reqSecDefOptParams)."
            )
        else:
            notices.append(
                f"Exchange-native verified {len(union_strikes)} strikes across "
                f"{len(specs)} expiration(s) via reqSecDefOptParams."
            )

        return ChainPlan(
            underlying=resolved_underlying,
            market_date=resolved_market_date,
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
            smart_chains=tuple(resolved_chains),
        )
