"""Helpers for bridging libdatachannel's worker threads onto an asyncio loop.

Callbacks fired by the native extension run on libdatachannel's internal
worker threads (with the GIL held). They must not touch asyncio state
directly; instead they schedule work onto the loop via
``loop.call_soon_threadsafe``. These helpers centralise the edge cases:

* The loop may have been closed by the time the callback fires.
* A :class:`asyncio.Future` slot may already be resolved, in which case we
  silently ignore the second resolution instead of raising
  ``InvalidStateError``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any, TypeVar

__all__ = ["FutureSlot", "schedule"]

T = TypeVar("T")


def schedule(loop: asyncio.AbstractEventLoop, fn: Callable[..., Any], *args: Any) -> None:
    """Thread-safely schedule ``fn(*args)`` on ``loop``.

    Silently no-ops when the loop is already closed — this happens during
    interpreter shutdown when libdatachannel's threads are still draining.
    """

    # Silently no-op if the loop is closed (interpreter shutdown race).
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(fn, *args)


class FutureSlot[T]:
    """A latched :class:`asyncio.Future` that may be resolved at most once.

    Subsequent ``set_result``/``set_exception`` calls after the future is
    resolved are silently ignored, which matches how libdatachannel fires
    some callbacks repeatedly (e.g. a gathering state transition).
    """

    __slots__ = ("_future", "_loop")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._future: asyncio.Future[T] = loop.create_future()

    @property
    def future(self) -> asyncio.Future[T]:
        return self._future

    def set(self, value: T) -> None:
        if not self._future.done():
            self._future.set_result(value)

    def fail(self, exc: BaseException) -> None:
        if not self._future.done():
            self._future.set_exception(exc)

    def reset(self) -> None:
        if not self._future.done():
            self._future.cancel()
        self._future = self._loop.create_future()

    def __await__(self):  # type: ignore[no-untyped-def]
        return self._future.__await__()
