"""`CorridorQuoteService`: bounded IBKR subscriptions, one-shot pulls and SSE-ready streams.

Every `reqMktData` is paired with `cancelMktData` in `finally:` (stop, disconnect,
`aclose()`, Gateway drop, lifespan exit). Paced subscription, the initial-poll cascade, quote
building and line cleanup are the shared primitives of `src/broker/corridor/lines.py` (also used
by the live screener leg stream, `src/broker/live_legs.py`)."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from ib_async import Stock

from src.broker.client import IBKRBrokerClient, SleepFn
from src.broker.contracts import extract_spot
from src.broker.corridor.lines import (
    BurstPacer,
    MarketDataSession,
    build_quote_rows,
    contract_from_spec,
    is_client_disconnected,
    poll_initial_quotes,
    subscribe_lines,
)
from src.broker.corridor.models import (
    CORRIDOR_MAX_CONTRACTS,
    CorridorContractSpec,
    CorridorQuoteFrame,
    CorridorQuoteRow,
    CorridorSelection,
    GreeksFallback,
    corridor_quote_key,
)
from src.broker.fetcher import DELAYED_GENERIC_TICKS, LIVE_OPRA_GENERIC_TICKS
from src.core.config import Settings
from src.core.exceptions import (
    CorridorBusyError,
    GatewayConnectionError,
    OptionChainFetchError,
    PacingViolationError,
)

logger = logging.getLogger(__name__)

CORRIDOR_BUSY_MESSAGE = (
    "Live Wall Corridor stream not started: another IBKR operation (chain refresh, batch "
    "load or ledger sync) holds the market-data line budget. Retry once it finishes."
)
_SUBSCRIBE_LOST_MESSAGE = "IBKR Gateway connection lost while subscribing corridor quotes"

#: A Wall Corridor session is a shared `MarketDataSession` (one-shot pull or stream).
_ActiveCorridorSession = MarketDataSession


def _build_frame_quotes(
    specs: Sequence[CorridorContractSpec],
    tickers: Sequence[Any],
    *,
    spot: float,
    updated_ts_utc: str,
    greeks_fallback: GreeksFallback | None,
) -> list[CorridorQuoteRow]:
    """Convert raw IBKR `Ticker` objects to `CorridorQuoteRow` items (Greeks fallback injected)."""
    rows, _ = build_quote_rows(
        specs,
        tickers,
        spots=[spot] * len(specs),
        updated_ts_utc=updated_ts_utc,
        greeks_fallback=greeks_fallback,
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
    """Manages bounded `<= 90`-contract Wall Corridor snapshots and constant streaming."""

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
        session.release(label="corridor")
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

        candidates = [contract_from_spec(spec) for spec in selection.specs]
        missing = [i for i, c in enumerate(candidates) if int(getattr(c, "conId", 0) or 0) <= 0]
        if missing:
            res = await self.client.qualify_contracts(
                [candidates[i] for i in missing],
                stage_prefix="corridor contract qualification",
                sleep=self.sleep,
            )
            if len(res) == len(missing):
                for idx, contract in zip(missing, res, strict=True):
                    candidates[idx] = contract

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
                    await self.client.run_stage(
                        f"corridor underlying qualification ({selection.ticker})",
                        self.client.ib.qualifyContractsAsync(
                            Stock(selection.ticker, "SMART", "USD")
                        ),
                    )
                    or []
                )
                if (
                    qual_spot
                    and qual_spot[0] is not None
                    and not isinstance(qual_spot[0], (list, tuple, set, dict, str, bytes))
                ):
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
        # Spot first (no generic ticks), then the options; one burst pacer for the whole set.
        requests: list[tuple[Any, str]] = []
        if spot_contract is not None:
            requests.append((spot_contract, ""))
        requests.extend((contract, generic_ticks) for contract in option_contracts)
        tickers = await subscribe_lines(
            session,
            requests,
            pacer=BurstPacer(self.sleep),
            lost_message=_SUBSCRIBE_LOST_MESSAGE,
        )
        if spot_contract is None:
            return tickers, None
        if not tickers:
            return [], None
        return tickers[1:], tickers[0]

    async def _poll_initial_corridor(
        self,
        session: _ActiveCorridorSession,
        option_contracts: Sequence[Any],
        spot_contract: Any | None,
        *,
        resubscribe_on_fallback: bool,
    ) -> tuple[_ActiveCorridorSession, list[Any], Any | None, list[str]]:
        """Subscribe corridor, wait for initial ticks, and apply dynamic Type fallback if needed."""
        current = session
        option_tickers: list[Any] = []
        spot_ticker: Any | None = None

        async def _resubscribe(suffix: str) -> None:
            nonlocal current, option_tickers, spot_ticker
            self._cleanup_session(current)
            current = _ActiveCorridorSession(
                session_id=f"{current.session_id}-{suffix}",
                ib=self.client.ib,
            )
            self._active_sessions[current.session_id] = current
            option_tickers, spot_ticker = await self._subscribe_corridor(
                current,
                option_contracts,
                spot_contract,
                generic_ticks=self._generic_ticks(),
            )

        try:
            option_tickers, spot_ticker = await self._subscribe_corridor(
                current,
                option_contracts,
                spot_contract,
                generic_ticks=self._generic_ticks(),
            )
            if current.stop_event.is_set():
                return current, option_tickers, spot_ticker, []
            notices = await poll_initial_quotes(
                self.client,
                self.settings,
                sleep=self.sleep,
                option_tickers=lambda: option_tickers,
                stopped=lambda: current.stop_event.is_set(),
                label="Corridor",
                resubscribe=_resubscribe if resubscribe_on_fallback else None,
            )
            return current, option_tickers, spot_ticker, notices
        except BaseException:
            self._cleanup_session(current)
            raise

    async def fetch_corridor_quotes(
        self,
        selection: CorridorSelection,
        *,
        snapshot_id: str,
        on_frame: Callable[[CorridorQuoteFrame], None] | None = None,
    ) -> CorridorQuoteFrame:
        """One-shot targeted Wall Strike Corridor pull (`<= 90` contracts) with cleanup."""
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
            if session.stop_event.is_set() or len(option_tickers) != len(selection.specs):
                raise OptionChainFetchError(
                    "Corridor quote pull stopped before all contracts were subscribed"
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
        is_busy: Callable[[], bool] | None = None,
        on_register: Callable[[], Sequence[str]] | None = None,
    ) -> AsyncGenerator[CorridorQuoteFrame, None]:
        """Continuously stream throttled Wall Corridor quotes until stop/disconnect/error.

        The generator subscribes lazily (first `anext`). `is_busy` (injected by the service
        layer, e.g. `refresh_lock.locked`) is re-checked when the generator starts and again
        right after the session is registered, with no `await` before the first `reqMktData`.
        A lock holder that acquires later therefore always sees this session and stops it via
        `stop_all_streams()`; one that acquired earlier makes this check raise
        `CorridorBusyError` with zero subscriptions opened.

        `on_register` (optional, synchronous) runs in that same step, right after the session is
        registered: the service layer stops the other live feed there (one live feed at a
        time); the notices it returns are prepended to the first frame's notices.
        """

        def _raise_if_busy() -> None:
            if is_busy is not None and is_busy():
                raise CorridorBusyError(CORRIDOR_BUSY_MESSAGE)

        _raise_if_busy()
        # Ensure zero overlapping live streams so IBKR line count stays <= 90
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
            # Same synchronous step as the registration above: nothing can acquire the
            # refresh lock between this check and the first `reqMktData`.
            register_notices = list(on_register()) if on_register is not None else []
            _raise_if_busy()
            session, option_tickers, spot_ticker, notices = await self._poll_initial_corridor(
                session,
                option_contracts,
                spot_contract,
                resubscribe_on_fallback=False,
            )
            notices = [*register_notices, *notices]

            last_sigs: dict[str, tuple[Any, ...]] = {}
            last_spot: float | None = None
            seq = 0
            idle_elapsed = 0.0
            heartbeat_sec = corridor_cfg.heartbeat_interval_sec

            while not session.stop_event.is_set():
                if await is_client_disconnected(is_disconnected):
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

                if session.stop_event.is_set() or await is_client_disconnected(is_disconnected):
                    break
                await self.sleep(eff_poll)
        finally:
            self._cleanup_session(session)
