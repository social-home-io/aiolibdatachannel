"""Async wrapper around libdatachannel's PeerConnection."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, Self, TypeVar, cast, get_args

from . import _core
from ._loop import FutureSlot, schedule
from .config import DataChannelOptions, RTCConfiguration
from .data_channel import DataChannel
from .enums import GatheringState, ICEState, RTCState, SignalingState
from .exceptions import ConnectionClosedError, RTCError

logger = logging.getLogger(__name__)

__all__ = [
    "DataChannelEvent",
    "GatheringStateChangeEvent",
    "IceCandidate",
    "IceStateChangeEvent",
    "LocalCandidateEvent",
    "LocalDescription",
    "LocalDescriptionEvent",
    "PCEvent",
    "PeerConnection",
    "SdpType",
    "SignalingStateChangeEvent",
    "StateChangeEvent",
]

_T = TypeVar("_T")

type SdpType = Literal["offer", "answer", "pranswer", "rollback"]
_SDP_TYPES: frozenset[str] = frozenset(get_args(SdpType.__value__))


def _coerce_sdp_type(raw: str | None, default: SdpType = "offer") -> SdpType:
    """Narrow a raw native string to :data:`SdpType`.

    libdatachannel only ever emits the four literal kinds; if something
    unexpected comes through we fall back to ``default`` rather than
    crash the event dispatch.
    """
    if raw is None or raw not in _SDP_TYPES:
        return default
    return cast(SdpType, raw)


@dataclass(slots=True, frozen=True)
class LocalDescription:
    """A locally-generated session description."""

    sdp: str
    type: SdpType


@dataclass(slots=True, frozen=True)
class IceCandidate:
    """A locally-generated ICE candidate."""

    candidate: str
    mid: str


# ---- Event types -----------------------------------------------------------


@dataclass(slots=True, frozen=True)
class LocalDescriptionEvent:
    """Emitted when a local session description has been produced."""

    description: LocalDescription


@dataclass(slots=True, frozen=True)
class LocalCandidateEvent:
    """Emitted when ICE gathering yields a new local candidate."""

    candidate: IceCandidate


@dataclass(slots=True, frozen=True)
class StateChangeEvent:
    """Emitted when the PeerConnection's overall state changes."""

    state: RTCState


@dataclass(slots=True, frozen=True)
class IceStateChangeEvent:
    """Emitted when the ICE transport state changes."""

    state: ICEState


@dataclass(slots=True, frozen=True)
class GatheringStateChangeEvent:
    """Emitted when the ICE gathering state changes."""

    state: GatheringState


@dataclass(slots=True, frozen=True)
class SignalingStateChangeEvent:
    """Emitted when the signaling state changes."""

    state: SignalingState


@dataclass(slots=True, frozen=True)
class DataChannelEvent:
    """Emitted when the remote peer opens a DataChannel."""

    channel: DataChannel


type PCEvent = (
    LocalDescriptionEvent
    | LocalCandidateEvent
    | StateChangeEvent
    | IceStateChangeEvent
    | GatheringStateChangeEvent
    | SignalingStateChangeEvent
    | DataChannelEvent
)


class PeerConnection:
    """An asyncio-friendly WebRTC PeerConnection.

    Must be instantiated from within a running asyncio loop; the loop is
    captured at construction time and used to marshal all native callbacks.

    Lifecycle:

    * :meth:`close` (sync) — trigger shutdown and return immediately.
    * :meth:`aclose` (async) — trigger shutdown *and* wait for it to
      complete (spawned tasks drained, native handle released).
    * :meth:`wait_closed` (async) — observe another coroutine's shutdown
      without triggering it.
    * :attr:`closed` — boolean property.

    Tasks the caller needs the PC to manage (e.g. ICE-candidate forwarding
    pumps) should be registered with :meth:`spawn_task` — they get
    cancelled and awaited during :meth:`aclose` so consumers don't have
    to bookkeep them by hand.
    """

    def __init__(self, config: RTCConfiguration | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        cfg = config or RTCConfiguration()
        self._native = _core.PeerConnection(
            ice_servers=[
                entry if isinstance(entry, str) else entry.to_url() for entry in cfg.ice_servers
            ],
            port_range_begin=cfg.port_range_begin,
            port_range_end=cfg.port_range_end,
            mtu=cfg.mtu,
            max_message_size=cfg.max_message_size,
            enable_ice_tcp=cfg.enable_ice_tcp,
            disable_auto_negotiation=cfg.disable_auto_negotiation,
            certificate_type=int(cfg.certificate_type),
            ice_transport_policy=int(cfg.ice_transport_policy),
        )

        self._state = RTCState.NEW
        self._ice_state = ICEState.NEW
        self._gathering_state = GatheringState.NEW
        self._signaling_state = SignalingState.STABLE

        # ``add_remote_candidate`` raises ``RTCError`` if the native
        # side hasn't applied a remote description yet (libdatachannel
        # ``rtcAddRemoteCandidate`` returns ``RTC_ERR_INVALID``). The
        # signaling layer routinely surfaces ICE candidates a beat
        # ahead of the offer/answer they belong to (small window — the
        # candidate-batch frame and the SDP frame may overlap on the
        # wire). Buffer those locally and drain as soon as
        # ``set_remote_description`` finishes so the caller doesn't
        # need to coordinate ordering.
        self._remote_description_set = asyncio.Event()
        self._buffered_remote_candidates: list[tuple[str, str]] = []
        self._local_description: FutureSlot[LocalDescription] = FutureSlot(self._loop)
        self._gathering_complete: FutureSlot[None] = FutureSlot(self._loop)

        self._state_events: dict[RTCState, asyncio.Event] = {
            state: asyncio.Event() for state in RTCState
        }
        self._state_events[RTCState.NEW].set()

        self._ice_candidates: asyncio.Queue[IceCandidate | None] = asyncio.Queue()
        self._incoming_dc: asyncio.Queue[DataChannel | None] = asyncio.Queue()
        self._events_queue: asyncio.Queue[PCEvent | None] = asyncio.Queue()

        # Keep references to DataChannel wrappers so user-provided ones are
        # not garbage-collected while the native handle is still live.
        self._data_channels: list[DataChannel] = []

        # Lifecycle machinery.
        self._closed: bool = False
        self._closed_event: asyncio.Event = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._teardown_task: asyncio.Task[None] | None = None

        # Wire up native callbacks.
        self._native.set_on_local_description(self._cb_local_description)
        self._native.set_on_local_candidate(self._cb_local_candidate)
        self._native.set_on_state_change(self._cb_state_change)
        self._native.set_on_ice_state_change(self._cb_ice_state_change)
        self._native.set_on_gathering_state_change(self._cb_gathering_state_change)
        self._native.set_on_signaling_state_change(self._cb_signaling_state_change)
        self._native.set_on_data_channel(self._cb_data_channel)

    # ---- Native callback trampolines ------------------------------------

    def _cb_local_description(self, sdp: str, type_: str) -> None:
        schedule(
            self._loop,
            self._handle_local_description,
            LocalDescription(sdp, _coerce_sdp_type(type_)),
        )

    def _cb_local_candidate(self, candidate: str, mid: str) -> None:
        schedule(self._loop, self._handle_local_candidate, IceCandidate(candidate, mid))

    def _cb_state_change(self, state: int) -> None:
        schedule(self._loop, self._handle_state_change, RTCState(state))

    def _cb_ice_state_change(self, state: int) -> None:
        schedule(self._loop, self._handle_ice_state, ICEState(state))

    def _cb_gathering_state_change(self, state: int) -> None:
        schedule(self._loop, self._handle_gathering_state, GatheringState(state))

    def _cb_signaling_state_change(self, state: int) -> None:
        schedule(self._loop, self._handle_signaling_state, SignalingState(state))

    def _cb_data_channel(self, handle: int) -> None:
        schedule(self._loop, self._handle_incoming_dc, handle)

    # ---- Loop-thread dispatchers ----------------------------------------

    def _emit(self, event: PCEvent) -> None:
        """Push an event onto :meth:`events` without blocking the loop."""
        with contextlib.suppress(asyncio.QueueFull):
            self._events_queue.put_nowait(event)

    def _handle_local_description(self, desc: LocalDescription) -> None:
        self._local_description.set(desc)
        self._emit(LocalDescriptionEvent(desc))

    def _handle_local_candidate(self, cand: IceCandidate) -> None:
        self._ice_candidates.put_nowait(cand)
        self._emit(LocalCandidateEvent(cand))

    def _handle_state_change(self, state: RTCState) -> None:
        self._state = state
        self._state_events[state].set()
        self._emit(StateChangeEvent(state))
        if state in (RTCState.CLOSED, RTCState.FAILED):
            # Resolve any pending futures with a close error so awaiters
            # don't hang indefinitely.
            if not self._local_description.future.done():
                self._local_description.fail(ConnectionClosedError("peer connection closed"))
            if not self._gathering_complete.future.done():
                self._gathering_complete.set(None)
            with contextlib.suppress(asyncio.QueueFull):
                self._ice_candidates.put_nowait(None)  # iterator sentinel

    def _handle_ice_state(self, state: ICEState) -> None:
        self._ice_state = state
        self._emit(IceStateChangeEvent(state))

    def _handle_gathering_state(self, state: GatheringState) -> None:
        self._gathering_state = state
        self._emit(GatheringStateChangeEvent(state))
        if state is GatheringState.COMPLETE:
            self._gathering_complete.set(None)
            with contextlib.suppress(asyncio.QueueFull):
                self._ice_candidates.put_nowait(None)

    def _handle_signaling_state(self, state: SignalingState) -> None:
        self._signaling_state = state
        self._emit(SignalingStateChangeEvent(state))

    def _handle_incoming_dc(self, handle: int) -> None:
        dc = DataChannel(handle, loop=self._loop)
        self._data_channels.append(dc)
        self._incoming_dc.put_nowait(dc)
        self._emit(DataChannelEvent(dc))

    # ---- State introspection --------------------------------------------

    @property
    def state(self) -> RTCState:
        return self._state

    @property
    def ice_state(self) -> ICEState:
        return self._ice_state

    @property
    def gathering_state(self) -> GatheringState:
        return self._gathering_state

    @property
    def signaling_state(self) -> SignalingState:
        return self._signaling_state

    @property
    def closed(self) -> bool:
        """True once :meth:`close` or :meth:`aclose` has been called."""
        return self._closed

    async def wait_for_state(self, state: RTCState) -> None:
        """Block until the PeerConnection reaches ``state``."""

        await self._state_events[state].wait()

    # ---- SDP / candidate plumbing ---------------------------------------

    async def create_offer(self) -> LocalDescription:
        """Create + set the local offer and wait for ICE gathering to finish.

        Returns the complete SDP (with all candidates inlined) so callers
        don't have to reassemble trickle candidates. For trickle-ICE, use
        :meth:`set_local_description` + the ``ice_candidates`` iterator.
        """

        if self._closed:
            raise ConnectionClosedError("peer connection is closed")
        self._local_description.reset()
        self._gathering_complete.reset()
        self._native.set_local_description("offer")
        await self._gathering_complete.future
        sdp = self._native.get_local_description()
        kind = _coerce_sdp_type(self._native.get_local_description_type(), default="offer")
        if sdp is None:
            raise RTCError("local description not available after gathering")
        return LocalDescription(sdp=sdp, type=kind)

    async def create_answer(self) -> LocalDescription:
        """Same as :meth:`create_offer` but produces an answer SDP."""

        self._local_description.reset()
        self._gathering_complete.reset()
        self._native.set_local_description("answer")
        await self._gathering_complete.future
        sdp = self._native.get_local_description()
        kind = _coerce_sdp_type(self._native.get_local_description_type(), default="answer")
        if sdp is None:
            raise RTCError("local description not available after gathering")
        return LocalDescription(sdp=sdp, type=kind)

    async def restart_ice(self, *, trickle: bool = False) -> LocalDescription:
        """Re-run ICE without dropping the existing PeerConnection.

        ``trickle=False`` (the default) mirrors :meth:`create_offer`:
        awaits the new ICE gathering cycle and returns the complete
        inline-ICE SDP. ``trickle=True`` mirrors
        :meth:`set_local_description`: returns the bare SDP as soon as
        libdatachannel produces it; iterate :meth:`ice_candidates` to
        forward new candidates as they are discovered.

        libdatachannel decides whether this call performs a true ICE
        restart (the PC has been previously connected) or builds a
        fresh offer (never connected) based on its own state machine.
        The only Python-side pre-condition is that ``close`` /
        ``aclose`` has not run.

        :raises ConnectionClosedError: if ``close`` / ``aclose`` ran.
        :raises RTCError: ``trickle=False`` and the native layer
            fails to produce an SDP after gathering completes.
        """

        if self._closed:
            raise ConnectionClosedError("peer connection is closed")
        self._local_description.reset()
        if not trickle:
            self._gathering_complete.reset()
        while not self._ice_candidates.empty():
            self._ice_candidates.get_nowait()
        self._native.set_local_description("offer")
        if trickle:
            return await self._local_description.future
        await self._gathering_complete.future
        sdp = self._native.get_local_description()
        kind = _coerce_sdp_type(self._native.get_local_description_type(), default="offer")
        if sdp is None:
            raise RTCError("local description not available after gathering")
        return LocalDescription(sdp=sdp, type=kind)

    async def set_local_description(self, type_: SdpType | None = None) -> LocalDescription:
        """Generate and set the local description without waiting for ICE.

        Resolves as soon as the SDP is produced; use the ``ice_candidates``
        async iterator to forward candidates to the remote side as they
        are discovered.

        Calling with ``type_=None`` on a previously-connected PC is the
        W3C-spec form of ICE restart and is equivalent to
        :meth:`restart_ice` (with ``trickle=True``).
        """

        if self._closed:
            raise ConnectionClosedError("peer connection is closed")
        self._local_description.reset()
        while not self._ice_candidates.empty():
            self._ice_candidates.get_nowait()
        self._native.set_local_description(type_)
        return await self._local_description.future

    async def set_remote_description(self, sdp: str, type_: SdpType) -> None:
        """Apply an SDP offer/answer received from the remote peer."""

        await self._loop.run_in_executor(
            None,
            self._native.set_remote_description,
            sdp,
            type_,
        )
        # Drain any candidates the signaling layer handed us before the
        # SDP arrived. Both calls flow through the same executor, so by
        # the time we get here the native side is ready to accept them.
        self._remote_description_set.set()
        if self._buffered_remote_candidates:
            buffered = self._buffered_remote_candidates
            self._buffered_remote_candidates = []
            for cand, mid in buffered:
                try:
                    await self._loop.run_in_executor(
                        None,
                        self._native.add_remote_candidate,
                        cand,
                        mid,
                    )
                except Exception:
                    # Each candidate is independent — log and keep
                    # going, the same way ``add_remote_candidate``
                    # would have if the caller had retried after the
                    # SDP landed.
                    logger.debug(
                        "drained buffered remote candidate failed",
                        exc_info=True,
                    )

    async def add_remote_candidate(self, candidate: str, mid: str = "") -> None:
        """Register an ICE candidate received from the remote peer.

        If the remote description hasn't been applied yet the candidate
        is buffered locally and forwarded to libdatachannel as soon as
        :meth:`set_remote_description` completes — see the matching
        comment on ``_buffered_remote_candidates`` in ``__init__``.
        """

        if not self._remote_description_set.is_set():
            self._buffered_remote_candidates.append((candidate, mid))
            return
        await self._loop.run_in_executor(
            None,
            self._native.add_remote_candidate,
            candidate,
            mid,
        )

    @property
    def local_description(self) -> LocalDescription | None:
        sdp = self._native.get_local_description()
        if sdp is None:
            return None
        return LocalDescription(
            sdp=sdp, type=_coerce_sdp_type(self._native.get_local_description_type())
        )

    @property
    def remote_description(self) -> str | None:
        return self._native.get_remote_description()

    # ---- DataChannel management -----------------------------------------

    async def create_data_channel(
        self, label: str, options: DataChannelOptions | None = None
    ) -> DataChannel:
        """Create an outgoing DataChannel."""

        opts = options or DataChannelOptions()
        handle = self._native.create_data_channel(
            label=label,
            unordered=not opts.ordered,
            unreliable=(
                opts.max_packet_lifetime_ms is not None or opts.max_retransmits is not None
            ),
            max_packet_lifetime=opts.max_packet_lifetime_ms or 0,
            max_retransmits=opts.max_retransmits or 0,
            protocol=opts.protocol,
            negotiated=opts.negotiated,
            manual_stream=opts.stream_id is not None,
            stream=opts.stream_id or 0,
        )
        dc = DataChannel(handle, loop=self._loop)
        self._data_channels.append(dc)
        return dc

    def incoming_data_channels(self) -> AsyncIterator[DataChannel]:
        """Async iterator over remotely-initiated DataChannels.

        Terminates cleanly when the PeerConnection is closed — consumer
        code can write a plain ``async for`` without any defensive
        cancellation.
        """

        return self._iter_incoming()

    async def _iter_incoming(self) -> AsyncIterator[DataChannel]:
        while True:
            dc = await self._incoming_dc.get()
            if dc is None:
                # Re-inject so every concurrent consumer also sees close.
                with contextlib.suppress(asyncio.QueueFull):
                    self._incoming_dc.put_nowait(None)
                return
            yield dc

    async def accept_data_channel(self) -> DataChannel:
        """Await the next remotely-initiated DataChannel.

        Raises :class:`ConnectionClosedError` if the PeerConnection is
        closed before one arrives.
        """

        dc = await self._incoming_dc.get()
        if dc is None:
            with contextlib.suppress(asyncio.QueueFull):
                self._incoming_dc.put_nowait(None)
            raise ConnectionClosedError("peer connection closed before a data channel arrived")
        return dc

    def ice_candidates(self) -> AsyncIterator[IceCandidate]:
        """Async iterator over locally-gathered ICE candidates.

        Terminates cleanly when gathering reaches
        :attr:`GatheringState.COMPLETE` *or* the PeerConnection is
        closed — ``async for`` without any defensive cancellation on the
        caller's side.
        """

        return self._iter_ice_candidates()

    async def _iter_ice_candidates(self) -> AsyncIterator[IceCandidate]:
        while True:
            cand = await self._ice_candidates.get()
            if cand is None:
                with contextlib.suppress(asyncio.QueueFull):
                    self._ice_candidates.put_nowait(None)
                return
            yield cand

    def events(self) -> AsyncIterator[PCEvent]:
        """Unified stream of everything the PeerConnection emits.

        Yields tagged events — local descriptions, ICE candidates,
        state transitions, incoming DataChannels — until the PC is
        closed, at which point the iterator terminates cleanly (same
        contract as :meth:`ice_candidates`). Intended for consumers
        that want a single ``async for`` over every PC signal; the
        specialised iterators (:meth:`ice_candidates`,
        :meth:`incoming_data_channels`) remain available for narrower
        cases and are populated in parallel.

        Multi-consumer safe: each event is delivered to one consumer
        and the close sentinel is re-injected so every concurrent
        iterator terminates.

        Usage::

            async for ev in pc.events():
                match ev:
                    case LocalCandidateEvent(candidate=c):
                        await remote.add_remote_candidate(c.candidate, c.mid)
                    case StateChangeEvent(state=RTCState.FAILED):
                        break
        """
        return self._iter_events()

    async def _iter_events(self) -> AsyncIterator[PCEvent]:
        while True:
            ev = await self._events_queue.get()
            if ev is None:
                with contextlib.suppress(asyncio.QueueFull):
                    self._events_queue.put_nowait(None)
                return
            yield ev

    # ---- Task ownership --------------------------------------------------

    def spawn_task(self, coro: Coroutine[Any, Any, _T]) -> asyncio.Task[_T]:
        """Schedule ``coro`` as a task owned by this PeerConnection.

        Owned tasks are cancelled and awaited during :meth:`aclose`, so
        the caller doesn't have to track helper tasks (e.g. ICE forwarding
        pumps) by hand.

        Raises :class:`ConnectionClosedError` if the PC is already closed.
        """

        if self._closed:
            coro.close()
            raise ConnectionClosedError("peer connection is closed")
        task = self._loop.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---- Lifecycle ------------------------------------------------------

    def close(self) -> None:
        """Trigger shutdown without waiting for it.

        Wakes up every public iterator (via sentinel), cancels tasks
        registered with :meth:`spawn_task`, and schedules the native
        teardown on the event loop. For a shutdown *barrier* that waits
        until that has all finished, use :meth:`aclose`.

        Safe to call multiple times; subsequent calls are no-ops.
        """

        if self._closed:
            return
        self._closed = True

        # Wake up any iterator that's blocked on queue.get() right now,
        # synchronously, so consumer tasks can exit on their next step
        # without having to wait for the native state-change callback.
        with contextlib.suppress(asyncio.QueueFull):
            self._ice_candidates.put_nowait(None)
        with contextlib.suppress(asyncio.QueueFull):
            self._incoming_dc.put_nowait(None)
        with contextlib.suppress(asyncio.QueueFull):
            self._events_queue.put_nowait(None)

        # Cancel any helper tasks the user registered. Tasks driven by our
        # own iterators will finish naturally from the sentinels above;
        # cancel is belt-and-braces for tasks doing other things.
        for task in list(self._tasks):
            task.cancel()

        # Drive the blocking teardown steps off a background task; aclose
        # (or __aexit__) awaits the completion event.
        self._teardown_task = self._loop.create_task(self._teardown())

    async def _teardown(self) -> None:
        try:
            if self._tasks:
                # Snapshot: the set mutates via add_done_callback.
                await asyncio.gather(*list(self._tasks), return_exceptions=True)
            for dc in self._data_channels:
                dc._destroy()
            await self._loop.run_in_executor(None, self._native.destroy)
        finally:
            self._closed_event.set()

    async def aclose(self) -> None:
        """Close and wait for teardown to complete.

        Triggers shutdown (if not already triggered), then awaits the
        completion of all spawned tasks and the native handle destroy.
        Safe to call concurrently: multiple awaiters all return when the
        first call's teardown finishes.
        """

        if not self._closed:
            self.close()
        await self._closed_event.wait()

    async def wait_closed(self) -> None:
        """Block until this PeerConnection's teardown is complete.

        Unlike :meth:`aclose`, this does *not* itself trigger close — it
        just observes another coroutine's shutdown. Returns immediately
        if the PC is already fully closed.
        """

        await self._closed_event.wait()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
