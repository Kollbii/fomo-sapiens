"""Socket-free coroutine drivers.

Tests never open OS sockets (Windows loopback / sandboxed CI safety), so coroutines are driven
either by plain `send(None)` stepping (`run_coro`) or by a deterministic virtual-clock loop
(`run_virtual_loop`) when code under test needs `asyncio.get_running_loop()` / timers.
"""

from __future__ import annotations

import asyncio
import collections
import heapq
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def run_coro(coro: Coroutine[Any, Any, T]) -> T:
    """Drive a pure/mocked coroutine to completion without an event loop or sockets."""
    try:
        while True:
            coro.send(None)
    except StopIteration as stop:
        val: T = stop.value
        return val


async def noop_sleep(_seconds: float) -> None:
    """Injectable replacement for `asyncio.sleep` that never waits."""
    return None


class VirtualEventLoop:
    """Minimal socketless virtual-clock event loop safe inside restricted sandboxes."""

    def __init__(self) -> None:
        self._now = 0.0
        self._ready: collections.deque[asyncio.Handle] = collections.deque()
        self._scheduled: list[tuple[float, int, asyncio.TimerHandle]] = []
        self._seq = 0
        self._closed = False

    def get_debug(self) -> bool:
        return False

    def is_running(self) -> bool:
        running: Any = asyncio.events._get_running_loop()
        return running is self

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True

    def time(self) -> float:
        return self._now

    def create_future(self) -> asyncio.Future[Any]:
        return asyncio.Future(loop=self)  # type: ignore[arg-type]

    def create_task(
        self,
        coro: Coroutine[Any, Any, T],
        *,
        name: str | None = None,
        context: Any = None,
    ) -> asyncio.Task[T]:
        return asyncio.Task(coro, loop=self, name=name, context=context)  # type: ignore[arg-type]

    def _timer_handle_cancelled(self, handle: asyncio.TimerHandle) -> None:
        return None

    def call_exception_handler(self, context: dict[str, Any]) -> None:
        return None

    def call_soon(self, callback: Any, *args: Any, context: Any = None) -> asyncio.Handle:
        handle = asyncio.Handle(callback, args, self, context)  # type: ignore[arg-type]
        self._ready.append(handle)
        return handle

    def call_soon_threadsafe(
        self, callback: Any, *args: Any, context: Any = None
    ) -> asyncio.Handle:
        return self.call_soon(callback, *args, context=context)

    def call_later(
        self, delay: float, callback: Any, *args: Any, context: Any = None
    ) -> asyncio.TimerHandle:
        return self.call_at(self._now + max(0.0, delay), callback, *args, context=context)

    def call_at(
        self, when: float, callback: Any, *args: Any, context: Any = None
    ) -> asyncio.TimerHandle:
        self._seq += 1
        timer = asyncio.TimerHandle(when, callback, args, self, context)  # type: ignore[arg-type]
        heapq.heappush(self._scheduled, (when, self._seq, timer))
        return timer

    def run_until_complete(self, future: Any) -> Any:
        task = future if isinstance(future, asyncio.Task) else self.create_task(future)
        prev = asyncio.events._get_running_loop()
        asyncio.events._set_running_loop(self)  # type: ignore[arg-type]
        try:
            while not task.done():
                while self._ready:
                    handle = self._ready.popleft()
                    if not handle.cancelled():
                        handle._run()
                if task.done():
                    break
                while self._scheduled and self._scheduled[0][2].cancelled():
                    heapq.heappop(self._scheduled)
                if not self._scheduled:
                    if not self._ready:
                        raise RuntimeError("Virtual event loop stalled")
                    continue
                when, _, timer = heapq.heappop(self._scheduled)
                self._now = max(self._now, when)
                if not timer.cancelled():
                    timer._run()
            return task.result()
        finally:
            asyncio.events._set_running_loop(prev)


def run_virtual_loop(coro: Coroutine[Any, Any, T]) -> T:
    """Execute a coroutine with a live `asyncio.get_running_loop()` but no sockets."""
    loop = VirtualEventLoop()
    try:
        res: T = loop.run_until_complete(coro)
        return res
    finally:
        loop.close()
