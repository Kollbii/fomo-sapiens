"""Shared async-to-blocking concurrency typing helpers for the services layer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar

_T = TypeVar("_T")


class BlockingRunner(Protocol):
    """Runs a blocking zero-argument callable off the event loop (e.g. `asyncio.to_thread`)."""

    def __call__(self, func: Callable[[], _T], /) -> Awaitable[_T]: ...
