"""Read-only IBKR Gateway connection manager with Type 3 -> 4 (or 1 -> 2) market data cascade."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from ib_async import IB

from src.core.config import Settings, get_settings
from src.core.exceptions import ConfigurationError, GatewayConnectionError, SecurityViolationError
from src.core.security import ReadOnlyIBGuard

logger = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]

#: Connection / data-farm status notices: purely informational.
INFO_CODES: frozenset[int] = frozenset({2104, 2106, 2107, 2108, 2158})
#: "Requested market data requires additional subscription" style warnings: non-fatal.
SUBSCRIPTION_CODES: frozenset[int] = frozenset({10089, 354})
#: Pacing warnings: the fetcher widens the inter-page cooldown.
PACING_CODES: frozenset[int] = frozenset({100, 162})

DELAYED_TYPES: frozenset[int] = frozenset({3, 4})
LIVE_PRIMARY_TYPE = 1
LIVE_FALLBACK_TYPE = 2
DELAYED_PRIMARY_TYPE = 3
DELAYED_FALLBACK_TYPE = 4


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
            self.primary_type, self.fallback_type = LIVE_PRIMARY_TYPE, LIVE_FALLBACK_TYPE
        else:
            if (
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
        self.subscription_warning_count = 0
        self.live_subscription_missing = False
        self._pacing_warning = False
        self._connect_lock = asyncio.Lock()

    # ------------------------------------------------------------ connection
    @property
    def is_connected(self) -> bool:
        return bool(self.ib.isConnected())

    async def ensure_connected(self) -> None:
        """Connect (once) to `IBKR_HOST:IBKR_PORT` and apply the primary market data type.

        Uses the low-level socket handshake (`ib.client.connectAsync`) rather than
        `IB.connectAsync`, because the latter auto-requests positions / orders / account
        updates, which this read-only analytics app must never query.
        """
        async with self._connect_lock:
            if self.is_connected:
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
            logger.info("connected to IBKR %s:%s clientId=%s", cfg.host, cfg.port, cfg.client_id)
            self.reset_market_data_type()

    def disconnect(self) -> None:
        """Gracefully close the socket; never raises."""
        self._safe_disconnect()

    def _safe_disconnect(self) -> None:
        try:
            self.ib.disconnect()
        except Exception:  # pragma: no cover - defensive
            logger.exception("error while disconnecting from IBKR")

    # ------------------------------------------------------- market data type
    def _set_market_data_type(self, data_type: int) -> None:
        self.ib.reqMarketDataType(data_type)
        self.current_type = data_type
        logger.info("reqMarketDataType(%d)", data_type)

    def reset_market_data_type(self) -> int:
        """Start every refresh from the configured primary type (3 = Delayed or 1 = Live)."""
        self._set_market_data_type(self.primary_type)
        return self.current_type

    def can_fallback(self) -> bool:
        return self.current_type != self.fallback_type

    def switch_to_fallback_data_type(self) -> int:
        """Cascade to the frozen variant: 3 -> 4 (Delayed-Frozen) or 1 -> 2 (Frozen)."""
        target = DELAYED_FALLBACK_TYPE if self.current_type in DELAYED_TYPES else self.fallback_type
        self._set_market_data_type(target)
        return target

    # ---------------------------------------------------------- error events
    def consume_pacing_warning(self) -> bool:
        """Return True once if a pacing warning arrived since the last call."""
        flagged, self._pacing_warning = self._pacing_warning, False
        return flagged

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
                    self.live_subscription_missing = True
                    self._set_market_data_type(DELAYED_PRIMARY_TYPE)
            elif code in PACING_CODES:
                self._pacing_warning = True
                logger.warning("IBKR pacing warning %s: %s", code, message)
            else:
                logger.warning("IBKR error %s (reqId=%s): %s", code, req_id, message)
        except Exception:  # pragma: no cover - defensive
            logger.exception("error handling IBKR error event")
