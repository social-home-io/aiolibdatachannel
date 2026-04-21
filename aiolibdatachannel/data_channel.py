"""Async wrapper over libdatachannel's DataChannel handle."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Coroutine
from typing import Any, TypeVar

from . import _core
from ._loop import FutureSlot, schedule
from .exceptions import ConnectionClosedError, RTCError

__all__ = ["DataChannel"]

_CLOSED_SENTINEL: object = object()
_T = TypeVar("_T")


class DataChannel:
    """An asyncio-friendly WebRTC DataChannel.

    Instances are not created directly — obtain them from
    :meth:`aiolibdatachannel.PeerConnection.create_data_channel` or from
    the ``incoming_data_channels`` async iterator on a PeerConnection.

    Lifecycle mirrors :class:`PeerConnection`:

    * :meth:`close` (sync) — trigger shutdown and return immediately.
    * :meth:`aclose` (async) — trigger shutdown *and* wait for it to
      complete (spawned tasks drained, native handle released).
    * :meth:`wait_closed` (async) — observe another coroutine's shutdown
      without triggering it.
    * :attr:`closed` — True once :meth:`close`/:meth:`aclose` has been
      called (vs. :attr:`is_closed` which reflects the underlying SCTP
      stream state).
    """

    def __init__(
        self,
        handle: int,
        *,
        loop: asyncio.AbstractEventLoop,
        recv_buffer: int = 1024,
    ) -> None:
        self._loop = loop
        self._native = _core.DataChannel(handle)
        self._destroyed = False
        self._close_requested = False
        self._open_slot: FutureSlot[None] = FutureSlot(loop)
        self._closed_slot: FutureSlot[None] = FutureSlot(loop)
        self._buffered_low_event = asyncio.Event()
        self._buffered_low_event.set()
        self._recv_queue: asyncio.Queue[bytes | str | object] = asyncio.Queue(
            maxsize=recv_buffer,
        )
        self._last_error: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._teardown_task: asyncio.Task[None] | None = None

        # Register native callbacks. Each trampoline runs on a libdatachannel
        # worker thread with the GIL held; we marshal onto the loop.
        self._native.set_on_open(self._cb_open)
        self._native.set_on_closed(self._cb_closed)
        self._native.set_on_error(self._cb_error)
        self._native.set_on_message(self._cb_message)
        self._native.set_on_buffered_amount_low(self._cb_buffered_low)

        # Sync initial state — the channel may already be open (e.g. a
        # pre-negotiated DC opened before we attached callbacks).
        if self._native.is_open():
            self._open_slot.set(None)
        if self._native.is_closed():
            self._closed_slot.set(None)
            self._recv_queue.put_nowait(_CLOSED_SENTINEL)

    # ---- Native callback trampolines ------------------------------------

    def _cb_open(self) -> None:
        schedule(self._loop, self._open_slot.set, None)

    def _cb_closed(self) -> None:
        schedule(self._loop, self._handle_closed)

    def _cb_error(self, message: str) -> None:
        schedule(self._loop, self._handle_error, message)

    def _cb_message(self, data: bytes | str) -> None:
        schedule(self._loop, self._handle_message, data)

    def _cb_buffered_low(self) -> None:
        schedule(self._loop, self._buffered_low_event.set)

    # ---- Loop-thread dispatchers ----------------------------------------

    def _handle_closed(self) -> None:
        self._closed_slot.set(None)
        if not self._open_slot.future.done():
            self._open_slot.fail(ConnectionClosedError("channel closed before open"))
        with contextlib.suppress(asyncio.QueueFull):
            self._recv_queue.put_nowait(_CLOSED_SENTINEL)

    def _handle_error(self, message: str) -> None:
        self._last_error = message
        if not self._open_slot.future.done():
            self._open_slot.fail(RTCError(message))

    def _handle_message(self, data: bytes | str) -> None:
        try:
            self._recv_queue.put_nowait(data)
        except asyncio.QueueFull:
            # Drop oldest to keep recent data; users who care should size the
            # queue larger. Logged at warning via stderr from the binding if
            # a logger is installed.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._recv_queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._recv_queue.put_nowait(data)

    # ---- Public API -----------------------------------------------------

    @property
    def label(self) -> str:
        return self._native.label() or ""

    @property
    def protocol(self) -> str:
        return self._native.protocol() or ""

    @property
    def stream_id(self) -> int:
        return self._native.stream()

    @property
    def max_message_size(self) -> int:
        return self._native.max_message_size()

    @property
    def buffered_amount(self) -> int:
        return self._native.buffered_amount()

    @property
    def is_open(self) -> bool:
        """Reflects libdatachannel's view of the underlying stream state."""
        return self._native.is_open()

    @property
    def is_closed(self) -> bool:
        """Reflects libdatachannel's view of the underlying stream state."""
        return self._native.is_closed()

    @property
    def closed(self) -> bool:
        """True once :meth:`close` or :meth:`aclose` has been called."""
        return self._close_requested

    def set_buffered_amount_low_threshold(self, amount: int) -> None:
        """Emit ``buffered_amount_low`` when the send buffer drops below ``amount``."""

        self._native.set_buffered_amount_low_threshold(amount)

    async def wait_open(self) -> None:
        """Resolve when the channel is open, or raise if it fails to open."""

        await self._open_slot.future

    async def wait_closed(self) -> None:
        """Block until this channel's teardown is complete.

        Observer-only: does not itself trigger close. Returns
        immediately if the channel is already closed.
        """

        await self._closed_slot.future

    async def send(self, data: bytes | str, *, wait_for_drain: bool = True) -> None:
        """Send a message over the channel.

        If ``wait_for_drain`` is true and the send buffer is above the
        configured low-water mark, this coroutine awaits the
        ``buffered_amount_low`` event before sending. That gives backpressure
        for free when interleaved with a steady producer.
        """

        if self._destroyed or self._native.is_closed():
            raise ConnectionClosedError("cannot send on a closed channel")

        if wait_for_drain and not self._buffered_low_event.is_set():
            await self._buffered_low_event.wait()

        if isinstance(data, (bytes, bytearray, memoryview)):
            self._native.send_bytes(bytes(data))
        elif isinstance(data, str):
            self._native.send_text(data)
        else:
            raise TypeError(f"send() expected bytes or str, got {type(data).__name__}")

        # Reset the low-water flag so the next oversized send blocks.
        if self._buffered_low_event.is_set() and self._native.buffered_amount() > 0:
            self._buffered_low_event.clear()

    async def recv(self) -> bytes | str:
        """Receive the next message. Raises :class:`ConnectionClosedError` on close."""

        item = await self._recv_queue.get()
        if item is _CLOSED_SENTINEL:
            # Re-inject so repeated callers also see the close.
            await self._recv_queue.put(_CLOSED_SENTINEL)
            raise ConnectionClosedError(self._last_error or "channel closed")
        assert isinstance(item, (bytes, str))
        return item

    def __aiter__(self) -> AsyncIterator[bytes | str]:
        return self._iter()

    async def _iter(self) -> AsyncIterator[bytes | str]:
        while True:
            try:
                yield await self.recv()
            except ConnectionClosedError:
                return

    # ---- Task ownership -------------------------------------------------

    def spawn_task(self, coro: Coroutine[Any, Any, _T]) -> asyncio.Task[_T]:
        """Schedule ``coro`` as a task owned by this DataChannel.

        Owned tasks are cancelled and awaited during :meth:`aclose`, so
        drain pumps don't have to be tracked by the caller. Raises
        :class:`ConnectionClosedError` if the channel is already closed.
        """

        if self._close_requested:
            coro.close()
            raise ConnectionClosedError("channel is closed")
        task = self._loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- Lifecycle ------------------------------------------------------

    def close(self) -> None:
        """Trigger shutdown without waiting for it.

        Wakes up readers via the recv-queue sentinel, cancels tasks
        registered with :meth:`spawn_task`, and schedules native
        teardown. Use :meth:`aclose` to wait for teardown to finish.

        Safe to call multiple times.
        """

        if self._close_requested:
            return
        self._close_requested = True

        # Push the close sentinel immediately so anyone blocked on
        # recv()/async-for wakes up without needing the native callback.
        with contextlib.suppress(asyncio.QueueFull):
            self._recv_queue.put_nowait(_CLOSED_SENTINEL)

        for task in list(self._tasks):
            task.cancel()

        self._teardown_task = self._loop.create_task(self._teardown())

    async def _teardown(self) -> None:
        try:
            if self._tasks:
                await asyncio.gather(*list(self._tasks), return_exceptions=True)
            # _destroy handles both the native teardown and the sentinel
            # re-push (idempotent).
            self._destroy()
        finally:
            if not self._closed_slot.future.done():
                self._closed_slot.set(None)

    async def aclose(self) -> None:
        """Close and wait for teardown to complete.

        Triggers shutdown (if not already triggered), then awaits the
        completion of spawned tasks and the native handle destroy.
        Safe to call concurrently.
        """

        if not self._close_requested:
            self.close()
        await self._closed_slot.future

    def _destroy(self) -> None:
        """Force-release the native handle. Called by the owning PeerConnection.

        Also called from :meth:`_teardown` so a caller can drive their
        own teardown.
        """

        if self._destroyed:
            return
        self._destroyed = True
        # Don't call _handle_closed() here — the native rtcDelete below
        # blocks until any in-flight on_closed callback finishes, and that
        # callback is what updates asyncio state. Touching the loop from
        # here risks racing with the callback thread.
        with contextlib.suppress(Exception):
            self._native.destroy()
        # In case no on_closed callback fired (PC closed before DTLS),
        # still resolve the futures so awaiters don't hang.
        self._handle_closed()
