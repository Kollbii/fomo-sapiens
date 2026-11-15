"""Read-only IBKR Gateway connection manager with dynamic MarketDataType (1..4) cascade."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator, Sequence
from typing import Any, Literal, NoReturn, TypeVar

from ib_async import IB

from src.broker.flex import sanitize_client_notice
from src.core.config import Settings, get_settings
from src.core.exceptions import (
    ConfigurationError,
    GatewayConnectionError,
    GatewayTimeoutError,
    SecurityViolationError,
)
from src.core.security import ReadOnlyIBGuard

logger = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]
_T = TypeVar("_T")

#: Connection / data-farm status notices: purely informational.
INFO_CODES: frozenset[int] = frozenset({2104, 2106, 2107, 2108, 2158})
#: "Requested market data requires additional subscription" style warnings: non-fatal.
SUBSCRIPTION_CODES: frozenset[int] = frozenset({10089, 354})
#: Pacing warnings: the fetcher widens the inter-page cooldown.
PACING_CODES: frozenset[int] = frozenset({100, 162})
#: Gateway/TWS connectivity drop warnings: recorded for diagnostics and fail pending stages fast.
DISCONNECT_CODES: frozenset[int] = frozenset({502, 504, 1100, 1300, 2110})
#: Connectivity restored notices (`1101` data lost / `1102` data maintained): never fail a stage.
RESTORE_CODES: frozenset[int] = frozenset({1101, 1102})

LIVE_TYPES: frozenset[int] = frozenset({1, 2})
DELAYED_TYPES: frozenset[int] = frozenset({3, 4})
ALL_MARKET_DATA_TYPES: frozenset[int] = frozenset({1, 2, 3, 4})
LIVE_PRIMARY_TYPE = 1
LIVE_FALLBACK_TYPE = 2
DELAYED_PRIMARY_TYPE = 3
DELAYED_FALLBACK_TYPE = 4
MARKET_DATA_TYPE_LABELS: dict[int, str] = {
    1: "Live OPRA",
    2: "Frozen",
    3: "Delayed",
    4: "Delayed-Frozen",
}

#: Max contracts sent in one `ib.qualifyContractsAsync` call to avoid Gateway pacing lockups.
QUALIFY_CHUNK_SIZE = 10
#: Pacing pause (seconds) between consecutive `qualify_contracts` micro-batches.
QUALIFY_CHUNK_PAUSE_SEC = 0.25


def market_data_type_label(data_type: int) -> str:
    """Return human-readable label for IBKR market data type `1..4`."""
    return MARKET_DATA_TYPE_LABELS.get(data_type, f"Type {data_type}")


def _ticker_has_quote_data(ticker: Any) -> bool:
    """Check whether `ticker` received real quote or modelGreek values (safe for default Ticker)."""
    for attr in (
        "bid",
        "ask",
        "last",
        "close",
        "delayedBid",
        "delayedAsk",
        "delayedLast",
        "delayedClose",
    ):
        raw = getattr(ticker, attr, None)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            num = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isnan(num) and not math.isinf(num) and num > 0.0:
            return True
    return getattr(ticker, "modelGreeks", None) is not None


async def poll_until(
    predicate: Callable[[], bool], timeout_sec: float, interval: float, sleep: SleepFn
) -> bool:
    """Poll `predicate` every `interval` up to `timeout_sec` (iteration-bounded)."""
    max_polls = max(1, math.ceil(timeout_sec / interval))
    for _ in range(max_polls):
        if predicate():
            return True
        await sleep(interval)
    return predicate()


def _has_running_event_loop() -> bool:
    """Return True when executing inside a live asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _close_unawaited(awaitable: Awaitable[Any]) -> None:
    """Close a coroutine that will never be awaited (avoids `never awaited` warnings)."""
    if inspect.iscoroutine(awaitable):
        awaitable.close()


def _consume_task_outcome(task: asyncio.Future[Any]) -> None:
    """Done-callback for abandoned request tasks: retrieve the outcome so nothing is logged."""
    if not task.cancelled():
        task.exception()


def _contract_match_key(contract: Any) -> tuple[str, str, str, float, str]:
    """Build a matching key to align partial qualification responses back to input chunks."""
    raw_strike = getattr(contract, "strike", 0.0) or 0.0
    try:
        strike = round(float(raw_strike), 6)
    except (TypeError, ValueError):
        strike = 0.0
    return (
        str(getattr(contract, "secType", "") or "").upper(),
        str(getattr(contract, "symbol", "") or "").upper(),
        str(getattr(contract, "lastTradeDateOrContractMonth", "") or ""),
        strike,
        str(getattr(contract, "right", "") or "").upper(),
    )


def _is_usable_contract(item: Any) -> bool:
    """True when `qualifyContractsAsync` returned a concrete contract instead of None/list."""
    return item is not None and not isinstance(item, (list, tuple, set, dict, str, bytes))


def _merge_chunk_results(chunk: Sequence[Any], res: Sequence[Any]) -> list[Any]:
    """Preserve positional alignment on a qualification chunk even on partial/empty responses."""
    if len(res) == len(chunk):
        return [
            item if _is_usable_contract(item) else original
            for original, item in zip(chunk, res, strict=True)
        ]
    merged = list(chunk)
    qualified_by_id: dict[int, Any] = {}
    qualified_by_key: dict[tuple[str, str, str, float, str], Any] = {}
    for item in res:
        if not _is_usable_contract(item):
            continue
        qualified_by_id[id(item)] = item
        qualified_by_key.setdefault(_contract_match_key(item), item)
    for idx, original in enumerate(chunk):
        if id(original) in qualified_by_id:
            merged[idx] = qualified_by_id[id(original)]
            continue
        matched = qualified_by_key.get(_contract_match_key(original))
        if matched is not None:
            merged[idx] = matched
    return merged


class IBKRBrokerClient:
    """Owns the single guarded `ib_async.IB` instance. Feature modules never create `IB()`."""

    def __init__(
        self,
        settings: Settings | None = None,
        ib_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        cfg = self.settings.ibkr
        self.mode: Literal["DELAYED", "LIVE"] = cfg.market_data_mode
        if self.mode == "LIVE":
            if cfg.default_market_data_type not in LIVE_TYPES:
                raise ConfigurationError(
                    "MARKET_DATA_MODE=LIVE requires primary market data type 1 (Live OPRA) "
                    "or 2 (Frozen)"
                )
        elif (
            cfg.default_market_data_type not in DELAYED_TYPES
            or cfg.fallback_market_data_type not in DELAYED_TYPES
        ):
            raise ConfigurationError(
                "MARKET_DATA_MODE=DELAYED requires market data types 3 (Delayed) / "
                "4 (Delayed-Frozen)"
            )
        self.primary_type = cfg.default_market_data_type
        self.fallback_type = cfg.fallback_market_data_type

        self.ib: Any = ReadOnlyIBGuard.lock((ib_factory or IB)())
        if not ReadOnlyIBGuard.verify_locked(self.ib):
            raise SecurityViolationError("ReadOnlyIBGuard failed to lock the IB instance")
        self.ib.errorEvent += self._on_error

        self.current_type: int = self.primary_type
        self._observed_connection_type: int | None = None
        self.subscription_warning_count = 0
        self.live_subscription_missing = False
        self._subscription_fallback_notice: str | None = None
        self._pacing_warning = False
        self._was_connected = False
        self._last_disconnect_notice: str | None = None
        self._last_disconnect_reason: Literal["timeout", "connection_lost"] | None = None
        self._last_timeout_notice: str | None = None
        self._connect_lock = asyncio.Lock()
        # Upstream-loss generation counter (bumped by every DISCONNECT_CODES notice), the
        # generation at which the current link-watch scope began, and abort futures of the
        # stages currently awaiting an IBKR answer (resolved by `_on_error`).
        self._link_loss_seq = 0
        self._link_scope_since: int | None = None
        self._stage_aborts: set[asyncio.Future[None]] = set()

    # ------------------------------------------------------------ connection
    @property
    def is_connected(self) -> bool:
        return bool(self.ib.isConnected())

    @property
    def was_connected(self) -> bool:
        return self._was_connected

    @property
    def connection_state(
        self,
    ) -> Literal["connected", "timeout_reset", "connection_lost", "offline"]:
        """Return current socket lifecycle state for status and UI banners."""
        if self.is_connected:
            return "connected"
        if self._last_disconnect_reason == "timeout":
            return "timeout_reset"
        if self._last_disconnect_reason == "connection_lost":
            return "connection_lost"
        return "offline"

    @property
    def connection_notice(self) -> str | None:
        """Return a non-sensitive notice describing the latest timeout or disconnect."""
        state = self.connection_state
        if state == "timeout_reset":
            return (
                sanitize_client_notice(self._last_timeout_notice) or None
                if self._last_timeout_notice
                else None
            )
        if state == "connection_lost":
            return (
                sanitize_client_notice(self._last_disconnect_notice) or None
                if self._last_disconnect_notice
                else None
            )
        return None

    def _clear_disconnect_state(self) -> None:
        self._last_disconnect_reason = None
        self._last_timeout_notice = None
        self._last_disconnect_notice = None

    def reset_after_timeout(self, *, stage: str) -> None:
        """Recycle the local Gateway socket after a timeout so orphaned queues drop."""
        self._last_disconnect_reason = "timeout"
        self._last_timeout_notice = sanitize_client_notice(
            f"Socket recycled after Gateway time-out during {stage}; auto-reconnects on next pull"
        )
        logger.warning(
            "Recycling IBKR Gateway socket after timeout during %s; next pull will reconnect",
            stage,
        )
        self._safe_disconnect()

    async def ensure_connected(self) -> None:
        """Connect (once) to `IBKR_HOST:IBKR_PORT` and apply the primary market data type.

        Uses the low-level socket handshake (`ib.client.connectAsync`) rather than
        `IB.connectAsync`, because the latter auto-requests positions / orders / account
        updates, which this read-only analytics app must never query.
        """
        async with self._connect_lock:
            self._clear_disconnect_state()
            if self.is_connected:
                self._was_connected = True
                return
            cfg = self.settings.ibkr
            self.ib.wrapper.clientId = cfg.client_id
            try:
                await self.ib.client.connectAsync(
                    cfg.host, cfg.port, cfg.client_id, cfg.connect_timeout_sec
                )
            except (OSError, TimeoutError, ConnectionError) as exc:
                self._safe_disconnect()
                raise GatewayConnectionError(
                    f"IBKR Gateway unreachable at {cfg.host}:{cfg.port}: {exc!r}"
                ) from exc
            if not self.is_connected:
                self._safe_disconnect()
                raise GatewayConnectionError(f"IBKR API handshake failed at {cfg.host}:{cfg.port}")
            self._was_connected = True
            logger.info("connected to IBKR %s:%s clientId=%s", cfg.host, cfg.port, cfg.client_id)
            self.reset_market_data_type()

    def ensure_still_connected(self, *, stage: str) -> None:
        """Raise `GatewayConnectionError` if an active Gateway socket dropped mid-batch."""
        if self._was_connected and not self.is_connected:
            detail = f" ({self._last_disconnect_notice})" if self._last_disconnect_notice else ""
            self._last_disconnect_reason = "connection_lost"
            self._safe_disconnect()
            raise GatewayConnectionError(f"IBKR Gateway socket disconnected during {stage}{detail}")

    def raise_transport_error(self, exc: BaseException, *, stage: str) -> NoReturn:
        """Close a broken Gateway session cleanly and raise a structured domain error."""
        detail = f" ({self._last_disconnect_notice})" if self._last_disconnect_notice else ""
        self._last_disconnect_reason = "connection_lost"
        self._safe_disconnect()
        raise GatewayConnectionError(
            f"IBKR Gateway connection interrupted during {stage}{detail}: {exc!r}"
        ) from exc

    def handle_stage_error(self, exc: BaseException, *, stage: str) -> None:
        """Translate socket/timeout errors (or lost active links) into `GatewayConnectionError`.

        A `GatewayTimeoutError` recycles the socket via `reset_after_timeout`. A non-timeout
        `GatewayConnectionError` is left untouched except that a dropped socket sets
        `connection_lost` and closes cleanly (plain upstream-loss keeps the local socket open).
        """
        if isinstance(exc, GatewayTimeoutError):
            self.reset_after_timeout(stage=stage)
            return
        if isinstance(exc, GatewayConnectionError):
            if self._was_connected and not self.is_connected:
                self._last_disconnect_reason = "connection_lost"
                self._safe_disconnect()
            return
        if isinstance(exc, (OSError, ConnectionError, TimeoutError)) or (
            self._was_connected and not self.is_connected
        ):
            self.raise_transport_error(exc, stage=stage)

    # --------------------------------------------------------- link watching
    @property
    def link_loss_seq(self) -> int:
        """Generation counter bumped by every `DISCONNECT_CODES` notice."""
        return self._link_loss_seq

    @contextlib.contextmanager
    def link_watch(self) -> Iterator[int]:
        """Scope one IBKR operation: an upstream-loss notice inside it fails later stages.

        Nested scopes reuse the outer start generation. Yields that generation.
        """
        if self._link_scope_since is not None:
            yield self._link_scope_since
            return
        self._link_scope_since = self._link_loss_seq
        try:
            yield self._link_scope_since
        finally:
            self._link_scope_since = None

    def link_checkpoint(self) -> int:
        """Generation a stage compares against: the active link-watch start, else now."""
        if self._link_scope_since is not None:
            return self._link_scope_since
        return self._link_loss_seq

    def link_lost_since(self, since: int) -> bool:
        """True when a `DISCONNECT_CODES` notice arrived after generation `since`."""
        return self._link_loss_seq != since

    def ensure_link_alive(self, *, stage: str, since: int) -> None:
        """Raise `GatewayConnectionError` if the Gateway reported upstream loss after `since`.

        A dropped socket is reported (and closed) first via `ensure_still_connected`. A plain
        upstream loss keeps the socket open so the Gateway can deliver its restore notices.
        """
        if not self.link_lost_since(since):
            return
        self.ensure_still_connected(stage=stage)
        detail = f" ({self._last_disconnect_notice})" if self._last_disconnect_notice else ""
        raise GatewayConnectionError(
            f"IBKR Gateway lost its upstream connection during {stage}{detail}"
        )

    async def await_guarded(
        self, awaitable: Awaitable[_T], *, stage: str, timeout_sec: float
    ) -> _T:
        """Await one IBKR request bounded by `timeout_sec` and raced against upstream loss.

        Raises `GatewayTimeoutError` when the Gateway does not answer in time and
        `GatewayConnectionError` as soon as a `DISCONNECT_CODES` notice arrives while the
        request is pending. The request task is cancelled whenever the caller stops waiting
        (timeout, upstream loss, or the caller's own cancellation). Without a running event
        loop (socket-free `run_coro` tests) the awaitable is stepped directly, unbounded.
        """
        since = self.link_checkpoint()
        try:
            self.ensure_link_alive(stage=stage, since=since)
        except BaseException:
            _close_unawaited(awaitable)
            raise
        if not _has_running_event_loop():
            res = await awaitable
            self.ensure_link_alive(stage=stage, since=since)
            return res

        loop = asyncio.get_running_loop()
        task: asyncio.Future[_T] = asyncio.ensure_future(awaitable)
        abort: asyncio.Future[None] = loop.create_future()
        self._stage_aborts.add(abort)
        waiters: set[asyncio.Future[Any]] = {task, abort}
        try:
            done, _pending = await asyncio.wait(
                waiters, timeout=timeout_sec, return_when=asyncio.FIRST_COMPLETED
            )
            if abort in done or self.link_lost_since(since):
                self.ensure_link_alive(stage=stage, since=since)
            if task in done:
                return task.result()
            raise GatewayTimeoutError(
                f"IBKR Gateway did not answer during {stage} within {timeout_sec:g} s"
            )
        finally:
            self._stage_aborts.discard(abort)
            if not abort.done():
                abort.cancel()
            if task.done():
                # Abort and a failed request can finish in the same wait: the link-loss error
                # wins, so retrieve the request outcome here or asyncio logs it as unretrieved.
                _consume_task_outcome(task)
            else:
                task.cancel()
                task.add_done_callback(_consume_task_outcome)

    async def qualify_contracts(
        self,
        contracts: Sequence[Any],
        *,
        stage_prefix: str = "contract qualification",
        chunk_size: int = QUALIFY_CHUNK_SIZE,
        pause_sec: float = QUALIFY_CHUNK_PAUSE_SEC,
        sleep: SleepFn = asyncio.sleep,
    ) -> list[Any]:
        """Qualify IBKR contracts in bounded micro-batches (`<= chunk_size`) with pacing pauses."""
        if chunk_size < 1:
            raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")
        if not contracts:
            return []

        since = self.link_checkpoint()
        qualified: list[Any] = []
        for idx, start in enumerate(range(0, len(contracts), chunk_size)):
            if idx > 0:
                self.ensure_still_connected(stage=stage_prefix)
                self.ensure_link_alive(stage=stage_prefix, since=since)
                if pause_sec > 0:
                    await sleep(pause_sec)
                self.ensure_still_connected(stage=stage_prefix)
                self.ensure_link_alive(stage=stage_prefix, since=since)

            chunk = list(contracts[start : start + chunk_size])
            stage = f"{stage_prefix} ({len(chunk)} contracts)"
            res = list(await self.run_stage(stage, self.ib.qualifyContractsAsync(*chunk)) or [])
            qualified.extend(_merge_chunk_results(chunk, res))
        return qualified

    async def run_stage(
        self, stage: str, coro: Awaitable[_T], *, timeout_sec: float | None = None
    ) -> _T:
        """Await a broker I/O coroutine (bounded) and translate transport interruptions cleanly.

        The wait is limited to `timeout_sec` (default `ibkr.request_timeout_sec`). A timeout
        raises `GatewayTimeoutError` after calling `reset_after_timeout(stage=stage)` so IB
        Gateway tears down orphaned `client_id` request queues; the next pull reconnects.
        Non-timeout dropped sockets and transport errors set `connection_lost` and close cleanly.
        """
        limit = self.settings.ibkr.request_timeout_sec if timeout_sec is None else timeout_sec
        try:
            res = await self.await_guarded(coro, stage=stage, timeout_sec=limit)
        except GatewayTimeoutError:
            self.reset_after_timeout(stage=stage)
            raise
        except Exception as exc:
            self.handle_stage_error(exc, stage=stage)
            raise
        self.ensure_still_connected(stage=stage)
        return res

    def disconnect(self) -> None:
        """Gracefully close the socket on explicit request; clears disconnect diagnostics."""
        self._clear_disconnect_state()
        self._safe_disconnect()

    def _safe_disconnect(self) -> None:
        self._was_connected = False
        self._observed_connection_type = None
        try:
            self.ib.disconnect()
        except Exception:  # pragma: no cover - defensive
            logger.exception("error while disconnecting from IBKR")

    # ------------------------------------------------------- market data type
    def _set_market_data_type(self, data_type: int) -> None:
        self.ib.reqMarketDataType(data_type)
        self.current_type = data_type
        self._observed_connection_type = None
        logger.info("reqMarketDataType(%d)", data_type)

    def observe_tickers_market_data_type(self, tickers: Sequence[Any]) -> int:
        """Record the modal connection-granted `Ticker.marketDataType` (`1..4`) when valid."""
        observed: list[int] = []
        for tk in tickers:
            raw_md = getattr(tk, "marketDataType", None)
            if type(raw_md) is not int or raw_md not in ALL_MARKET_DATA_TYPES:
                continue
            if self.current_type in DELAYED_TYPES:
                # Delayed queries never upgrade to Live; ignore default Ticker().marketDataType == 1
                if raw_md in DELAYED_TYPES:
                    observed.append(raw_md)
            else:
                # In LIVE mode, Gateway downgrades to 2/3/4 explicitly; Type 1 counts once populated
                if raw_md != LIVE_PRIMARY_TYPE or _ticker_has_quote_data(tk):
                    observed.append(raw_md)
        if observed:
            counts = Counter(observed)
            modal = int(min(counts, key=lambda dt: (-counts[dt], dt)))
            self._observed_connection_type = modal
        return self.get_active_market_data_type()

    def get_active_market_data_type(self) -> int:
        """Query `ib.client.marketDataType` / observed ticker type, else tracked `current_type`."""
        client = getattr(self.ib, "client", None)
        client_md = getattr(client, "marketDataType", None)
        if type(client_md) is int and client_md in ALL_MARKET_DATA_TYPES:
            return client_md
        if self._observed_connection_type in ALL_MARKET_DATA_TYPES:
            return int(self._observed_connection_type)
        return int(self.current_type)

    def reset_market_data_type(self) -> int:
        """Start every refresh from the configured primary type."""
        self.live_subscription_missing = False
        self._subscription_fallback_notice = None
        self._observed_connection_type = None
        self._set_market_data_type(self.primary_type)
        return self.current_type

    def _next_fallback_target(self) -> int:
        if self.live_subscription_missing and self.current_type in DELAYED_TYPES:
            return DELAYED_FALLBACK_TYPE
        return self.fallback_type

    def can_fallback(self) -> bool:
        return self.current_type != self._next_fallback_target()

    def switch_to_fallback_data_type(self) -> int:
        """Cascade to configured fallback type (`3->4`, `1->2`, or configured fallback)."""
        target = self._next_fallback_target()
        self._set_market_data_type(target)
        return target

    # ---------------------------------------------------------- error events
    def consume_pacing_warning(self) -> bool:
        """Return True once if a pacing warning arrived since the last call."""
        flagged, self._pacing_warning = self._pacing_warning, False
        return flagged

    def consume_subscription_fallback_notice(self) -> str | None:
        """Return and clear any queued live-subscription downgrade notice."""
        notice, self._subscription_fallback_notice = self._subscription_fallback_notice, None
        return notice

    def _signal_link_loss(self) -> None:
        """Bump the upstream-loss generation and wake every pending stage. Never raises."""
        self._link_loss_seq += 1
        for abort in list(self._stage_aborts):
            try:
                if not abort.done():
                    abort.set_result(None)
            except Exception:  # pragma: no cover - e.g. future of a closed loop
                logger.debug("could not signal a pending IBKR stage", exc_info=True)

    def _on_error(self, req_id: int, code: int, message: str, contract: Any = None) -> None:
        """`ib.errorEvent` listener. Must never raise."""
        try:
            if code in INFO_CODES:
                logger.debug("IBKR info %s: %s", code, message)
            elif code in RESTORE_CODES:
                logger.info("IBKR connectivity restored %s: %s", code, message)
            elif code in SUBSCRIPTION_CODES:
                self.subscription_warning_count += 1
                logger.info("IBKR subscription notice %s (reqId=%s): %s", code, req_id, message)
                if self.current_type not in DELAYED_TYPES:
                    # No live OPRA permissions: continue on the delayed cascade.
                    prev_type = self.current_type
                    self.live_subscription_missing = True
                    self._subscription_fallback_notice = (
                        f"Live subscription unavailable on Gateway for Type {prev_type} "
                        f"({market_data_type_label(prev_type)}, code {code}); cascaded to "
                        f"MarketDataType({DELAYED_PRIMARY_TYPE}) "
                        f"{market_data_type_label(DELAYED_PRIMARY_TYPE)}."
                    )
                    if self.is_connected:
                        try:
                            self._set_market_data_type(DELAYED_PRIMARY_TYPE)
                        except ConnectionError:
                            self.current_type = DELAYED_PRIMARY_TYPE
                    else:
                        self.current_type = DELAYED_PRIMARY_TYPE
                    self._observed_connection_type = DELAYED_PRIMARY_TYPE
            elif code in PACING_CODES:
                self._pacing_warning = True
                logger.warning("IBKR pacing warning %s: %s", code, message)
            elif code in DISCONNECT_CODES:
                sanitized = sanitize_client_notice(f"IBKR {code}: {message}")
                self._last_disconnect_notice = sanitized or f"IBKR {code}"
                logger.warning("IBKR connection notice %s (reqId=%s): %s", code, req_id, sanitized)
                self._signal_link_loss()
            else:
                logger.warning("IBKR error %s (reqId=%s): %s", code, req_id, message)
        except Exception:  # pragma: no cover - defensive
            logger.exception("error handling IBKR error event")
