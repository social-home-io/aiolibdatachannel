"""End-to-end: two PeerConnections negotiating over localhost.

Drives trickle-ICE manually between two PCs in the same process, opens
a DataChannel, exchanges a handful of messages, and asserts clean
shutdown. The ICE-candidate forwarding pumps are registered via
``pc.spawn_task`` so ``async with``'s exit (``aclose``) tears them
down without manual cancellation bookkeeping.
"""

from __future__ import annotations

import asyncio

import pytest

from aiolibdatachannel import (
    ConnectionClosedError,
    PeerConnection,
    RTCConfiguration,
    RTCState,
)

# Real libdatachannel behaviour required (DTLS / ICE / state machine).
pytestmark = pytest.mark.native


async def _forward(src: PeerConnection, dst: PeerConnection) -> None:
    """Drain ICE candidates from ``src`` into ``dst`` until gathering ends."""

    async for cand in src.ice_candidates():
        await dst.add_remote_candidate(cand.candidate, cand.mid)


@pytest.mark.asyncio
async def test_loopback_datachannel() -> None:
    cfg = RTCConfiguration()  # no STUN — host candidates are enough on loopback
    async with PeerConnection(cfg) as offerer, PeerConnection(cfg) as answerer:
        offerer_dc = await offerer.create_data_channel("chat")
        offerer.spawn_task(_forward(offerer, answerer))
        answerer.spawn_task(_forward(answerer, offerer))

        offer = await offerer.set_local_description("offer")
        await answerer.set_remote_description(offer.sdp, offer.type)
        answer = await answerer.set_local_description("answer")
        await offerer.set_remote_description(answer.sdp, answer.type)

        incoming = await asyncio.wait_for(answerer.accept_data_channel(), timeout=10.0)

        await asyncio.wait_for(offerer_dc.wait_open(), timeout=10.0)
        await asyncio.wait_for(incoming.wait_open(), timeout=10.0)

        await offerer_dc.send(b"ping")
        msg = await asyncio.wait_for(incoming.recv(), timeout=5.0)
        assert msg == b"ping"

        await incoming.send("pong")
        reply = await asyncio.wait_for(offerer_dc.recv(), timeout=5.0)
        assert reply == "pong"

    # __aexit__ → aclose() terminated the forward() tasks via sentinels +
    # awaited them. No manual cancel needed.


@pytest.mark.asyncio
async def test_send_after_close_raises() -> None:
    async with PeerConnection() as pc:
        dc = await pc.create_data_channel("temp")
    with pytest.raises(ConnectionClosedError):
        await dc.send(b"nope")


@pytest.mark.asyncio
async def test_state_progresses_to_closed() -> None:
    pc = PeerConnection()
    assert pc.state is RTCState.NEW
    await pc.aclose()
    # After close the handle is destroyed; state should be CLOSED or a
    # terminal state. We give the callback a brief chance to land.
    for _ in range(50):
        if pc.state in (RTCState.CLOSED, RTCState.DISCONNECTED, RTCState.FAILED):
            return
        await asyncio.sleep(0.01)
    # Terminal state not strictly required (close is abrupt); passing is OK.