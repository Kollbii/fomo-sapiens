from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from ib_async import IB

from src.core.exceptions import AnalyzerError, InvalidTickerError, SecurityViolationError
from src.core.security import BLOCKED_METHODS, ReadOnlyIBGuard, TickerValidator

WATCHLIST = ["NVDA", "AVGO", "CRDO", "PLTR"]


@pytest.mark.parametrize("symbol", WATCHLIST)
def test_watchlist_valid(symbol: str) -> None:
    v = TickerValidator(WATCHLIST)
    assert v.validate(symbol) == symbol
    assert v.is_watchlist(symbol.lower())


@pytest.mark.parametrize(
    ("raw", "expected"), [("SPY", "SPY"), ("AAPL", "AAPL"), ("  aapl\n", "AAPL"), ("a", "A")]
)
def test_custom_valid(raw: str, expected: str) -> None:
    v = TickerValidator(WATCHLIST)
    assert v.validate(raw) == expected
    assert not v.is_watchlist("SPY")


@pytest.mark.parametrize(
    "bad",
    [
        "../etc",
        "..",
        "a/b",
        "a\\b",
        "NVDA;rm",
        "NVDA|ls",
        "$HOME",
        "BRK.B",
        "BRK-B",
        "123",
        "N1",
        "",
        "   ",
        "ABCDEF",
        "NV DA",
        "NVDA\nX",
        "ß",
        "ＮＶＤＡ",
    ],
)
def test_invalid_rejected(bad: str) -> None:
    with pytest.raises(InvalidTickerError):
        TickerValidator.validate(bad)


def test_non_string_rejected() -> None:
    with pytest.raises(InvalidTickerError):
        TickerValidator.validate(None)  # type: ignore[arg-type]


def test_custom_disabled_enforces_watchlist() -> None:
    v = TickerValidator(WATCHLIST, allow_custom=False)
    assert v.validate_selection("nvda") == "NVDA"
    with pytest.raises(InvalidTickerError):
        v.validate_selection("SPY")


def test_errors_share_base() -> None:
    assert issubclass(InvalidTickerError, AnalyzerError)
    assert issubclass(SecurityViolationError, AnalyzerError)


def _call(fn: Any) -> None:
    result = fn()
    if inspect.isawaitable(result):
        asyncio.run(_await(result))


async def _await(awaitable: Any) -> None:
    await awaitable


def _assert_all_blocked(ib: Any) -> None:
    assert ReadOnlyIBGuard.verify_locked(ib)
    targets = [ib] + ([ib.client] if getattr(ib, "client", None) is not None else [])
    for target in targets:
        for name in BLOCKED_METHODS:
            with pytest.raises(SecurityViolationError):
                _call(lambda t=target, n=name: getattr(t, n)("dummy", 1, x=2))


def test_guard_on_dummy_object() -> None:
    sent: list[Any] = []
    client = SimpleNamespace(send=sent.append)
    dummy = SimpleNamespace(client=client, placeOrder=lambda *a: sent.append(a))
    assert not ReadOnlyIBGuard.verify_locked(dummy)
    assert ReadOnlyIBGuard.lock(dummy) is dummy
    _assert_all_blocked(dummy)
    assert sent == []


def test_guard_without_client_attribute() -> None:
    dummy = SimpleNamespace()
    ReadOnlyIBGuard.lock(dummy)
    _assert_all_blocked(dummy)


def test_guard_on_real_unconnected_ib() -> None:
    ib = IB()
    assert not ReadOnlyIBGuard.verify_locked(ib)
    ReadOnlyIBGuard.lock(ib)
    _assert_all_blocked(ib)
    assert not ib.isConnected()  # no socket was ever opened


def test_blocked_methods_cover_spec() -> None:
    assert set(BLOCKED_METHODS) == {
        "placeOrder",
        "cancelOrder",
        "reqGlobalCancel",
        "exerciseOptions",
        "whatIfOrder",
        "whatIfOrderAsync",
    }
