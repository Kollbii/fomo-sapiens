"""`LiveLegQuoteService`: live quotes for the Best Option screener's legs (multi-ticker, exact).

The live screener streams each allocated row's legs +-1 neighbour strike plus ONE spot line per
ticker. It reuses the Wall Corridor line primitives (`src/broker/corridor/lines.py`): paced
subscribe (bursts of 20, 0.55 s apart), the initial-poll market-data cascade (`DELAYED 3->4`,
`LIVE 1->2`, `10089` / `354` -> delayed), quote building with the injected `GreeksFallback`, and
the `finally:` line cleanup.

- Exact contracts only: snapshot conIds are reused, `run_stage` qualifies only a missing conId;
  never a chain request. Two budgets hold for the initial spec and every re-allocation, else
  `PacingViolationError` before any line of that spec is touched: option lines <=
  `option_lines_budget` (`min(corridor.default_max_contracts, 88)`, clamped even when the
  config allows 90) AND options + spots <= `lines_budget` (`min(corridor.hard_max_contracts,
  CORRIDOR_MAX_CONTRACTS)`, 90 by default) - e.g. 88 options + 1 spot = 89, 87 + 3 = 90.
- Lazy: subscribes on the first read. `is_busy` is checked at start and again after the session
  is registered and the `on_register` hook ran, with no await before the first `reqMktData`
  (`ScreenerLiveBusyError`, zero lines). A lock holder acquiring later sees the registered
  session and stops it (`stop_all_now`). One leg stream at a time.
- Re-allocation: `spec_source()` is read on every tick; a new `version` is applied as a diff -
  cancel the removed lines first, then subscribe the added ones (one burst pacer) - so the
  subscribed line count never exceeds the budget at any instant.
- One frame per `interval_sec` (1 s; must be >= the 0.55 s burst pause, so bursts of consecutive
  operations stay >= 0.55 s apart). Ends on stop, client disconnect, `max_frames` or `aclose()`;
  raises `GatewayConnectionError` on a socket drop or an upstream-loss notice
  (`DISCONNECT_CODES`). `finally:` cancels 100% of the session's lines on every path.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from ib_async import Stock

from src.broker.client import IBKRBrokerClient, SleepFn
from src.broker.contracts import extract_spot
from src.broker.corridor.lines import (
    BurstPacer,
    LinkGuard,
    MarketDataSession,
    build_quote_rows,
    contract_from_spec,
    is_client_disconnected,
    pace_and_guard,
    poll_initial_quotes,
    subscribe_lines,
)
from src.broker.corridor.models import (
    CORRIDOR_MAX_CONTRACTS,
    DEFAULT_CORRIDOR_MAX_CONTRACTS,
    CorridorContractSpec,
    CorridorQuoteRow,
    GreeksFallback,
)
from src.broker.fetcher import (
    DELAYED_GENERIC_TICKS,
    LIVE_OPRA_GENERIC_TICKS,
    PAGE_PACING_PAUSE_SEC,
)
from src.core.config import Settings
from src.core.exceptions import GatewayConnectionError, PacingViolationError, ScreenerLiveBusyError
from src.core.security import TickerValidator

logger = logging.getLogger(__name__)

#: Frame cadence of the live screener stream (seconds).
LIVE_LEGS_INTERVAL_SEC: Final = 1.0

SCREENER_LIVE_BUSY_MESSAGE: Final = (
    "Live Best Option screener stream not started: another IBKR operation (chain refresh, "
    "batch load, ledger sync or Corridor snapshot) holds the market-data line budget. Retry "
    "once it finishes."
)

_STAGE: Final = "live screener stream"
_SUBSCRIBE_LOST_MESSAGE: Final = (
    "IBKR Gateway connection lost while subscribing live screener quotes"
)
_STREAM_LOST_MESSAGE: Final = "IBKR Gateway connection lost during live screener stream"
_LOG_LABEL: Final = "live screener"

#: `(symbol, ISO expiry, strike rounded to 6 dp, right)` of one option line.
LegLineKey = tuple[str, str, float, str]


def leg_line_key(symbol: str, expiry: str, strike: float, right: str) -> LegLineKey:
    """Canonical key of one option line (shared strikes across rows map to ONE line)."""
    return (symbol.strip().upper(), expiry.strip(), round(float(strike), 6), right.strip().upper())


def spec_line_key(spec: CorridorContractSpec) -> LegLineKey:
    return leg_line_key(spec.symbol, spec.expiry, spec.strike, spec.right)


@dataclass(frozen=True, slots=True)
class LiveSpotLine:
    """Underlying spot line of one streamed ticker (`reference_price`: snapshot fallback)."""

    symbol: str
    underlying_conid: int
    reference_price: float


@dataclass(frozen=True)
class LiveLegsSpec:
    """Exact line set of one allocation; `version` identifies it inside a stream."""

    version: int
    options: tuple[CorridorContractSpec, ...] = ()
    spots: tuple[LiveSpotLine, ...] = ()

    @property
    def line_count(self) -> int:
        return len(self.options) + len(self.spots)

    def validate(self, budget: int, *, option_budget: int) -> None:
        """Enforce both line budgets and a consistent line set (one spot per streamed ticker).

        `budget` caps options + spots (`0..CORRIDOR_MAX_CONTRACTS`), `option_budget` the option
        lines alone (`0..budget`, the Corridor option cap). Raises `PacingViolationError` when
        either is exceeded or out of range, `InvalidTickerError` for an invalid symbol and
        `ValueError` for duplicate lines or an option line without its ticker's spot line.
        """
        if not 0 <= budget <= CORRIDOR_MAX_CONTRACTS:
            raise PacingViolationError(
                f"Live screener line budget {budget} must be between 0 and {CORRIDOR_MAX_CONTRACTS}"
            )
        if not 0 <= option_budget <= budget:
            raise PacingViolationError(
                f"Live screener option line budget {option_budget} must be between 0 and the "
                f"line budget {budget}"
            )
        if self.line_count > budget:
            raise PacingViolationError(
                f"Live screener lines {self.line_count} (options + spots) exceed the budget "
                f"{budget} (hard cap {CORRIDOR_MAX_CONTRACTS})"
            )
        if len(self.options) > option_budget:
            raise PacingViolationError(
                f"Live screener option lines {len(self.options)} exceed the option budget "
                f"{option_budget} (Corridor option cap)"
            )
        spot_symbols: set[str] = set()
        for spot in self.spots:
            symbol = TickerValidator.validate(spot.symbol)
            if symbol != spot.symbol or symbol in spot_symbols:
                raise ValueError(f"Spot line {spot.symbol!r} is duplicated or not normalized")
            spot_symbols.add(symbol)
        keys: set[LegLineKey] = set()
        for option in self.options:
            key = spec_line_key(option)
            if option.symbol not in spot_symbols:
                raise ValueError(f"Option line {key} has no spot line for its ticker")
            if key in keys:
                raise ValueError(f"Duplicate option line {key}")
            keys.add(key)


@dataclass(frozen=True)
class LiveLegsFrame:
    """One tick of the leg stream: every subscribed option line's quote plus the live spots.

    `quotes` holds every subscribed option line (cached snapshot values where IBKR sent no
    price); `live_keys` the lines IBKR priced (bid / ask / last / close). `spots` holds only the
    tickers with a usable live spot. `version` is the applied spec version. `lines_used` counts
    every open line; `lines_budget` caps options + spots, `option_lines_budget` the options.
    """

    session_id: str
    sequence: int
    version: int
    updated_ts_utc: str
    market_data_type: int
    lines_used: int
    lines_budget: int
    option_lines_budget: int
    spots: Mapping[str, float]
    quotes: Mapping[LegLineKey, CorridorQuoteRow]
    live_keys: frozenset[LegLineKey]
    notices: tuple[str, ...] = ()


@dataclass(eq=False)
class _Line:
    contract: Any
    ticker: Any


@dataclass(eq=False)
class _LegSession:
    market: MarketDataSession
    options: dict[LegLineKey, tuple[CorridorContractSpec, _Line]] = field(default_factory=dict)
    spots: dict[str, tuple[LiveSpotLine, _Line]] = field(default_factory=dict)

    @property
    def stopped(self) -> bool:
        return self.market.stop_event.is_set()

    def option_tickers(self) -> list[Any]:
        return [line.ticker for _, line in self.options.values()]


class LiveLegQuoteService:
    """Bounded multi-ticker exact-contract quote stream with diff re-allocation (one at a time)."""

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
        self._sessions: dict[str, _LegSession] = {}

    @property
    def lines_budget(self) -> int:
        """Total line budget (options + spots): `min(corridor.hard_max_contracts, 90)`."""
        return min(int(self.settings.corridor.hard_max_contracts), CORRIDOR_MAX_CONTRACTS)

    @property
    def option_lines_budget(self) -> int:
        """Option-line budget: `min(corridor.default_max_contracts, 88, lines_budget)`.

        Clamped to `DEFAULT_CORRIDOR_MAX_CONTRACTS` (88) even when the config allows
        `default_max_contracts` up to 90, so options never exceed 88 (+ spots <= 90 total).
        """
        return min(
            int(self.settings.corridor.default_max_contracts),
            DEFAULT_CORRIDOR_MAX_CONTRACTS,
            self.lines_budget,
        )

    @property
    def active_stream_count(self) -> int:
        return len(self._sessions)

    @property
    def active_line_count(self) -> int:
        return sum(len(s.market.subscribed_contracts) for s in self._sessions.values())

    def stop_session(self, session_id: str) -> int:
        """Stop one stream now (cancel 100% of its lines); returns the lines it held."""
        sess = self._sessions.get(session_id)
        if sess is None:
            return 0
        held = len(sess.market.subscribed_contracts)
        self._release(sess)
        return held

    def stop_all_now(self) -> int:
        """Synchronously stop every stream and cancel 100% of its lines; returns the count."""
        held = 0
        for sess in list(self._sessions.values()):
            held += len(sess.market.subscribed_contracts)
            self._release(sess)
        return held

    async def stop_all(self) -> int:
        """Async alias of `stop_all_now` (same contract as `CorridorQuoteService`)."""
        return self.stop_all_now()

    def _release(self, sess: _LegSession) -> None:
        sess.market.release(label=_LOG_LABEL)
        session_id = sess.market.session_id
        if self._sessions.get(session_id) is sess:
            del self._sessions[session_id]

    def _generic_ticks(self) -> str:
        if self.client.mode == "LIVE" and not self.client.live_subscription_missing:
            return LIVE_OPRA_GENERIC_TICKS
        return DELAYED_GENERIC_TICKS

    def _link_guard(self, since: int) -> LinkGuard:
        def _guard() -> None:
            self.client.ensure_link_alive(stage=_STAGE, since=since)

        return _guard

    def _ensure_alive(self, since: int) -> None:
        if not self.client.is_connected:
            raise GatewayConnectionError(_STREAM_LOST_MESSAGE)
        self.client.ensure_link_alive(stage=_STAGE, since=since)

    async def stream_live_legs(
        self,
        spec_source: Callable[[], LiveLegsSpec],
        *,
        session_id: str,
        interval_sec: float = LIVE_LEGS_INTERVAL_SEC,
        is_disconnected: Callable[[], bool | Awaitable[bool]] | None = None,
        is_busy: Callable[[], bool] | None = None,
        on_register: Callable[[], Awaitable[Sequence[str]]] | None = None,
        max_frames: int | None = None,
    ) -> AsyncGenerator[LiveLegsFrame, None]:
        """Stream the lines of `spec_source()` until stop / disconnect / error (see module doc).

        `on_register` (async) runs right after the session is registered, before the busy
        re-check: the service layer stops the other live feed there and returns notices for the
        first frame; it may also stop this session (superseded) to end the stream with no line.
        """
        if interval_sec < PAGE_PACING_PAUSE_SEC:
            raise ValueError(
                f"interval_sec must be >= {PAGE_PACING_PAUSE_SEC} s (got {interval_sec!r})"
            )

        def _raise_if_busy() -> None:
            if is_busy is not None and is_busy():
                raise ScreenerLiveBusyError(SCREENER_LIVE_BUSY_MESSAGE)

        _raise_if_busy()
        spec = spec_source()
        budget, option_budget = self.lines_budget, self.option_lines_budget
        spec.validate(budget, option_budget=option_budget)
        await self.client.ensure_connected()
        link_since = self.client.link_loss_seq
        opt_contracts, spot_contracts = await self._contracts_for(spec.options, spec.spots)

        stale = self._sessions.get(session_id)
        if stale is not None:
            self._release(stale)
        sess = _LegSession(MarketDataSession(session_id=session_id, ib=self.client.ib))
        self._sessions[session_id] = sess
        try:
            notices: list[str] = list(await on_register()) if on_register is not None else []
            if sess.stopped:
                return
            # Nothing awaits between this check and the first `reqMktData`.
            _raise_if_busy()
            # One leg stream at a time: older sessions lose 100% of their lines before ours open.
            for other in [s for s in self._sessions.values() if s is not sess]:
                self._release(other)
            self.client.reset_market_data_type()
            await self._subscribe_into(
                sess,
                spec.options,
                opt_contracts,
                spec.spots,
                spot_contracts,
                pacer=BurstPacer(self.sleep),
                link_since=link_since,
            )
            if sess.stopped:
                return
            notices.extend(
                await poll_initial_quotes(
                    self.client,
                    self.settings,
                    sleep=self.sleep,
                    option_tickers=sess.option_tickers,
                    stopped=lambda: sess.stopped,
                    label="Live screener",
                )
            )

            applied = spec
            sequence = 0
            while not sess.stopped:
                if await is_client_disconnected(is_disconnected):
                    break
                self._ensure_alive(link_since)
                latest = spec_source()
                if latest.version != applied.version:
                    latest.validate(budget, option_budget=option_budget)
                    await self._reallocate(sess, latest, link_since=link_since)
                    if sess.stopped:
                        break
                    applied = latest
                self.client.observe_tickers_market_data_type(sess.option_tickers())
                sequence += 1
                yield self._frame(
                    sess,
                    applied,
                    sequence=sequence,
                    notices=notices if sequence == 1 else (),
                )
                if max_frames is not None and sequence >= max_frames:
                    break
                if sess.stopped or await is_client_disconnected(is_disconnected):
                    break
                await self.sleep(interval_sec)
        finally:
            self._release(sess)

    async def _contracts_for(
        self,
        options: Sequence[CorridorContractSpec],
        spots: Sequence[LiveSpotLine],
    ) -> tuple[list[Any], list[Any]]:
        """Option and spot contracts reusing snapshot conIds; qualifies only missing conIds."""
        spot_contracts: list[Any] = [
            Stock(conId=s.underlying_conid, symbol=s.symbol, exchange="SMART", currency="USD")
            if s.underlying_conid > 0
            else Stock(s.symbol, "SMART", "USD")
            for s in spots
        ]
        contracts: list[Any] = [*spot_contracts, *(contract_from_spec(o) for o in options)]
        missing = [i for i, c in enumerate(contracts) if int(getattr(c, "conId", 0) or 0) <= 0]
        if missing:
            qualified = await self.client.qualify_contracts(
                [contracts[i] for i in missing],
                stage_prefix="live screener contract qualification",
                sleep=self.sleep,
            )
            if len(qualified) == len(missing):
                for idx, contract in zip(missing, qualified, strict=True):
                    contracts[idx] = contract
        n_spots = len(spot_contracts)
        return contracts[n_spots:], contracts[:n_spots]

    async def _subscribe_into(
        self,
        sess: _LegSession,
        options: Sequence[CorridorContractSpec],
        opt_contracts: Sequence[Any],
        spots: Sequence[LiveSpotLine],
        spot_contracts: Sequence[Any],
        *,
        pacer: BurstPacer,
        link_since: int,
    ) -> int:
        """Paced `reqMktData` (spots first) and line bookkeeping; returns the lines opened."""
        ticks = self._generic_ticks()
        requests: list[tuple[Any, str]] = [(c, "") for c in spot_contracts]
        requests.extend((c, ticks) for c in opt_contracts)
        tickers = await subscribe_lines(
            sess.market,
            requests,
            pacer=pacer,
            lost_message=_SUBSCRIBE_LOST_MESSAGE,
            link_guard=self._link_guard(link_since),
        )
        n_spots = len(spot_contracts)
        for spot, s_contract, s_ticker in zip(
            spots, spot_contracts, tickers[:n_spots], strict=False
        ):
            sess.spots[spot.symbol] = (spot, _Line(s_contract, s_ticker))
        for opt, o_contract, o_ticker in zip(
            options, opt_contracts, tickers[n_spots:], strict=False
        ):
            sess.options[spec_line_key(opt)] = (opt, _Line(o_contract, o_ticker))
        return len(tickers)

    async def _reallocate(self, sess: _LegSession, spec: LiveLegsSpec, *, link_since: int) -> None:
        """Apply `spec` as a diff: cancel removed lines first, then subscribe added ones."""
        wanted_opts = {spec_line_key(o): o for o in spec.options}
        wanted_spots = {s.symbol: s for s in spec.spots}
        # Kept lines take the new spec's cached snapshot attributes.
        for key, (_, line) in list(sess.options.items()):
            if key in wanted_opts:
                sess.options[key] = (wanted_opts[key], line)
        for symbol, (_, line) in list(sess.spots.items()):
            if symbol in wanted_spots:
                sess.spots[symbol] = (wanted_spots[symbol], line)

        pacer = BurstPacer(self.sleep)
        guard = self._link_guard(link_since)
        removed_opts = [key for key in sess.options if key not in wanted_opts]
        removed_spots = [symbol for symbol in sess.spots if symbol not in wanted_spots]
        cancelled = 0
        for key in removed_opts:
            if not await pace_and_guard(
                sess.market, pacer, lost_message=_SUBSCRIBE_LOST_MESSAGE, link_guard=guard
            ):
                return
            _, line = sess.options.pop(key)
            cancelled += int(sess.market.cancel_line(line.contract, label=_LOG_LABEL))
            pacer.sent += 1
        for symbol in removed_spots:
            if not await pace_and_guard(
                sess.market, pacer, lost_message=_SUBSCRIBE_LOST_MESSAGE, link_guard=guard
            ):
                return
            _, line = sess.spots.pop(symbol)
            cancelled += int(sess.market.cancel_line(line.contract, label=_LOG_LABEL))
            pacer.sent += 1

        add_opts = [o for key, o in wanted_opts.items() if key not in sess.options]
        add_spots = [s for symbol, s in wanted_spots.items() if symbol not in sess.spots]
        opened = 0
        if add_opts or add_spots:
            opt_contracts, spot_contracts = await self._contracts_for(add_opts, add_spots)
            if sess.stopped:
                return
            opened = await self._subscribe_into(
                sess,
                add_opts,
                opt_contracts,
                add_spots,
                spot_contracts,
                pacer=pacer,
                link_since=link_since,
            )
        logger.info(
            "live screener %s re-allocated to v%d: cancelled %d, subscribed %d (%d lines)",
            sess.market.session_id,
            spec.version,
            cancelled,
            opened,
            len(sess.market.subscribed_contracts),
        )

    def _frame(
        self,
        sess: _LegSession,
        applied: LiveLegsSpec,
        *,
        sequence: int,
        notices: Sequence[str],
    ) -> LiveLegsFrame:
        ts_utc = self.clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        live_spots: dict[str, float] = {}
        for symbol, (_, spot_line) in sess.spots.items():
            price = extract_spot(spot_line.ticker)
            if price is not None:
                live_spots[symbol] = float(price)
        reference = {s.symbol: s.reference_price for s in applied.spots}
        items = list(sess.options.items())
        specs = [opt for _, (opt, _) in items]
        line_spots = [live_spots.get(o.symbol, reference.get(o.symbol, 0.0)) for o in specs]
        rows, priced = build_quote_rows(
            specs,
            [line.ticker for _, (_, line) in items],
            spots=line_spots,
            updated_ts_utc=ts_utc,
            greeks_fallback=self.greeks_fallback,
        )
        keys = [key for key, _ in items]
        return LiveLegsFrame(
            session_id=sess.market.session_id,
            sequence=sequence,
            version=applied.version,
            updated_ts_utc=ts_utc,
            market_data_type=self.client.get_active_market_data_type(),
            lines_used=len(sess.market.subscribed_contracts),
            lines_budget=self.lines_budget,
            option_lines_budget=self.option_lines_budget,
            spots=live_spots,
            quotes=dict(zip(keys, rows, strict=True)),
            live_keys=frozenset(k for k, ok in zip(keys, priced, strict=True) if ok),
            notices=tuple(notices),
        )
