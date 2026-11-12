"""Minimal `ib_async` building blocks shared by broker/web tests."""

from __future__ import annotations

from typing import Any


class FakeEvent:
    """Minimal `eventkit.Event` stand-in supporting `+=` subscription and `emit`."""

    def __init__(self) -> None:
        self._handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> FakeEvent:
        self._handlers.append(handler)
        return self

    def emit(self, *args: Any) -> None:
        for handler in list(self._handlers):
            handler(*args)
