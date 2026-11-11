"""Read-only IBKR Gateway connection manager with dynamic MarketDataType (1..4) cascade."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal, NoReturn, TypeVar

from ib_async import IB

from src.core.config import Settings, get_settings
from src.core.exceptions import ConfigurationError, GatewayConnectionError, SecurityViolationError
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
#: Gateway/TWS connectivity drop warnings recorded for transport diagnostics.
DISCONNECT_CODES: frozenset[int] = frozenset({502, 504, 1100, 1300, 2110})

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
        self._connect_lock = asyncio.Lock()

    # ------------------------------------------------------------ connection
    @property
    def is_connected(self) -> bool:
        return bool(self.ib.isConnected())

    @property
    def was_connected(self) -> bool:
        return self._was_connected

    async def ensure_connected(self) -> None:
        """Connect (once) to `IBKR_HOST:IBKR_PORT` and apply the primary market data type.

        Uses the low-level socket handshake (`ib.client.connectAsync`) rather than
        `IB.connectAsync`, because the latter auto-requests positions / orders / account
        updates, which this read-only analytics app must never query.
        """
        async with self._connect_lock:
            if self.is_connected:
                self._was_connected = True
                return
            self._last_disconnect_notice = None
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
            self._safe_disconnect()
            raise GatewayConnectionError(f"IBKR Gateway socket disconnected during {stage}{detail}")

    def raise_transport_error(self, exc: BaseException, *, stage: str) -> NoReturn:
        """Close a broken Gateway session cleanly and raise a structured domain error."""
        detail = f" ({self._last_disconnect_notice})" if self._last_disconnect_notice else ""
        self._safe_disconnect()
        raise GatewayConnectionError(
            f"IBKR Gateway connection interrupted during {stage}{detail}: {exc!r}"
        ) from exc

    def handle_stage_error(self, exc: BaseException, *, stage: str) -> None:
        """Translate socket/timeout errors (or lost active links) into `GatewayConnectionError`."""
        if isinstance(exc, (OSError, ConnectionError, TimeoutError)) or (
            self._was_connected and not self.is_connected
        ):
            self.raise_transport_error(exc, stage=stage)

    async def run_stage(self, stage: str, coro: Awaitable[_T]) -> _T:
        """Await a broker I/O coroutine and translate transport interruptions cleanly."""
        try:
            res = await coro
        except Exception as exc:
            self.handle_stage_error(exc, stage=stage)
            raise
        self.ensure_still_connected(stage=stage)
        return res

    def disconnect(self) -> None:
        """Gracefully close the socket; never raises."""
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

    def _on_error(self, req_id: int, code: int, message: str, contract: Any = None) -> None:
        """`ib.errorEvent` listener. Must never raise."""
        try:
            if code in INFO_CODES:
                logger.debug("IBKR info %s: %s", code, message)
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
                self._last_disconnect_notice = f"IBKR {code}: {message}"
                logger.warning("IBKR connection notice %s (reqId=%s): %s", code, req_id, message)
            else:
                logger.warning("IBKR error %s (reqId=%s): %s", code, req_id, message)
        except Exception:  # pragma: no cover - defensive
            logger.exception("error handling IBKR error event")
