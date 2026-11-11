"""Zero-trust read-only guards: ticker sanitization and IB order-method blocking."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any, NoReturn

from src.core.exceptions import InvalidTickerError, SecurityViolationError

DEFAULT_WATCHLIST: tuple[str, ...] = ("NVDA", "AVGO", "CRDO", "PLTR")
TICKER_PATTERN = re.compile(r"[A-Z]{1,5}")

BLOCKED_METHODS: tuple[str, ...] = (
    "placeOrder",
    "cancelOrder",
    "reqGlobalCancel",
    "exerciseOptions",
    "whatIfOrder",
    "whatIfOrderAsync",
)
_TRAP_MARKER = "__read_only_trap__"


class TickerValidator:
    """Sanitizes every ticker before it reaches a file path or IBKR contract."""

    def __init__(
        self,
        watchlist: Iterable[str] = DEFAULT_WATCHLIST,
        allow_custom: bool = True,
    ) -> None:
        self.watchlist: tuple[str, ...] = tuple(self.validate(s) for s in watchlist)
        self.allow_custom = allow_custom

    @classmethod
    def validate(cls, raw_input: str) -> str:
        """Strip, uppercase and enforce `^[A-Z]{1,5}$`; raise `InvalidTickerError` otherwise."""
        if not isinstance(raw_input, str):
            raise InvalidTickerError(f"Ticker must be a string, got {type(raw_input).__name__}")
        stripped = raw_input.strip()
        if not stripped or not stripped.isascii():
            raise InvalidTickerError(f"Invalid ticker symbol: {raw_input!r}")
        symbol = stripped.upper()
        if not 1 <= len(symbol) <= 5 or TICKER_PATTERN.fullmatch(symbol) is None:
            raise InvalidTickerError(f"Invalid ticker symbol: {raw_input!r}")
        return symbol

    def is_watchlist(self, raw_input: str) -> bool:
        return self.validate(raw_input) in self.watchlist

    def validate_selection(self, raw_input: str) -> str:
        """Validate and additionally enforce the watchlist when custom tickers are disabled."""
        symbol = self.validate(raw_input)
        if not self.allow_custom and symbol not in self.watchlist:
            raise InvalidTickerError(f"Custom tickers are disabled; {symbol!r} not in watchlist")
        return symbol


def _make_trap(name: str) -> Callable[..., NoReturn]:
    def trap(*_args: Any, **_kwargs: Any) -> NoReturn:
        raise SecurityViolationError(
            f"Blocked IBKR method '{name}': this application is strictly read-only"
        )

    setattr(trap, _TRAP_MARKER, True)
    trap.__name__ = name
    return trap


def _targets(ib: Any) -> list[Any]:
    client = getattr(ib, "client", None)
    return [ib] if client is None else [ib, client]


class ReadOnlyIBGuard:
    """Replaces all order/mutation methods with traps that raise before any socket I/O."""

    @staticmethod
    def lock(ib: Any) -> Any:
        for target in _targets(ib):
            for name in BLOCKED_METHODS:
                setattr(target, name, _make_trap(name))
        return ib

    @staticmethod
    def verify_locked(ib: Any) -> bool:
        return all(
            getattr(getattr(target, name, None), _TRAP_MARKER, False) is True
            for target in _targets(ib)
            for name in BLOCKED_METHODS
        )
