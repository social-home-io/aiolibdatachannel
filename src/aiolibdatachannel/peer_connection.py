"""Async wrapper around libdatachannel's PeerConnection."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from . import _core
from ._loop import FutureSlot, schedule
from .config import DataChannelOptions, RTCConfiguration
from .data_channel import DataChannel
from .enums import GatheringState, ICEState, RTCState, SignalingState
from .exceptions import ConnectionClosedError, RTCError

__all__ = ["IceCandidate", "LocalDescription", "PeerConnection"]


@dataclass(slots=True, frozen=True)
class LocalDescription:
    """A locally-generated session description."""

    sdp: str
    type: str


@dataclass(slots=True, frozen=True)
class IceCandidate:
    """A locally-generated ICE candidate."""

    candidate: str
    mid: str


class PeerConnection:
    """An asyncio-friendly WebRTC PeerConnection.

    Must be instantiated from within a running asyncio loop; the loop is
    captured at construction time and used to marshal all native callbacks.
    """

    def __init__(self, config: RTCConfiguration | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        cfg = config or RTCConfiguration()
        self._native = _core.PeerConnection(
            ice_servers=list(cfg.ice_servers),
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
        self._local_description: FutureSlot[LocalDescription] = FutureSlot(self._loop)
        self._gathering_complete: FutureSlot[None] = FutureSlot(self._loop)

        self._state_events: dict[RTCState, asyncio.Event] = {
            state: asyncio.Event() for state in RTCState
        }
        self._state_events[RTCState.NEW].set()

        self._ice_candidates: asyncio.Queue[IceCandidate | None] = asyncio.Queue()
        self._incoming_dc: asyncio.Queue[DataChannel] = asyncio.Queue()

        # Keep references to DataChannel wrappers so user-provided ones are
        # not garbage-collected while the native handle is still live.
        self._data_channels: list[DataChannel] = []
        self._closed = False

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
        schedule(self._loop, self._local_description.set, LocalDescription(sdp, type_))

    def _cb_local_candidate(self, candidate: str, mid: str) -> None:
        schedule(self._loop, self._ice_candidates.put_nowait, IceCandidate(candidate, mid))

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

    def _handle_state_change(self, state: RTCState) -> None:
        self._state = state
        self._state_events[state].set()
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

    def _handle_gathering_state(self, state: GatheringState) -> None:
        self._gathering_state = state
        if state is GatheringState.COMPLETE:
            self._gathering_complete.set(None)
            with contextlib.suppress(asyncio.QueueFull):
                self._ice_candidates.put_nowait(None)

    def _handle_signaling_state(self, state: SignalingState) -> None:
        self._signaling_state = state

    def _handle_incoming_dc(self, handle: int) -> None:
        dc = DataChannel(handle, loop=self._loop)
        self._data_channels.append(dc)
        self._incoming_dc.put_nowait(dc)

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
        type_ = self._native.get_local_description_type() or "offer"
        if sdp is None:
            raise RTCError("local description not available after gathering")
        return LocalDescription(sdp=sdp, type=type_)

    async def create_answer(self) -> LocalDescription:
        """Same as :meth:`create_offer` but produces an answer SDP."""

        self._local_description.reset()
        self._gathering_complete.reset()
        self._native.set_local_description("answer")
        await self._gathering_complete.future
        sdp = self._native.get_local_description()
        type_ = self._native.get_local_description_type() or "answer"
        if sdp is None:
            raise RTCError("local description not available after gathering")
        return LocalDescription(sdp=sdp, type=type_)

    async def set_local_description(self, type_: str | None = None) -> LocalDescription:
        """Generate and set the local description without waiting for ICE.

        Resolves as soon as the SDP is produced; use the ``ice_candidates``
        async iterator to forward candidates to the remote side as they
        are discovered.
        """

        self._local_description.reset()
        self._native.set_local_description(type_)
        return await self._local_description.future

    async def set_remote_description(self, sdp: str, type_: str) -> None:
        """Apply an SDP offer/answer received from the remote peer."""

        await self._loop.run_in_executor(
            None,
            self._native.set_remote_description,
            sdp,
            type_,
        )

    async def add_remote_candidate(self, candidate: str, mid: str = "") -> None:
        """Register an ICE candidate received from the remote peer."""

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
        return LocalDescription(sdp=sdp, type=self._native.get_local_description_type() or "")

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
        """Async iterator over remotely-initiated DataChannels."""

        return self._iter_incoming()

    async def _iter_incoming(self) -> AsyncIterator[DataChannel]:
        while not self._closed:
            dc = await self._incoming_dc.get()
            yield dc

    async def accept_data_channel(self) -> DataChannel:
        """Await the next remotely-initiated DataChannel."""

        return await self._incoming_dc.get()

    async def ice_candidates(self) -> AsyncIterator[IceCandidate]:
        """Async iterator over locally-gathered ICE candidates.

        Terminates when ICE gathering completes or the connection closes.
        """

        while True:
            cand = await self._ice_candidates.get()
            if cand is None:
                return
            yield cand

    # ---- Lifecycle ------------------------------------------------------

    async def close(self) -> None:
        """Close the connection and release all native resources."""

        if self._closed:
            return
        self._closed = True
        for dc in self._data_channels:
            dc._destroy()
        await self._loop.run_in_executor(None, self._native.destroy)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
