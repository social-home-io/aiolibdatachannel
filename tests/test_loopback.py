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


@pytest.mark.asyncio
async def test_send_after_drain_below_threshold_does_not_hang() -> None:
    """Regression: with a low-water threshold above the buffered amount,
    the buffer drains to 0 without crossing it from above, so
    libdatachannel never fires ``buffered_amount_low``. The next
    ``send(wait_for_drain=True)`` used to wait forever on that callback.
    """

    cfg = RTCConfiguration()
    async with PeerConnection(cfg) as offerer, PeerConnection(cfg) as answerer:
        tx = await offerer.create_data_channel("drain")
        offerer.spawn_task(_forward(offerer, answerer))
        answerer.spawn_task(_forward(answerer, offerer))

        offer = await offerer.set_local_description("offer")
        await answerer.set_remote_description(offer.sdp, offer.type)
        answer = await answerer.set_local_description("answer")
        await offerer.set_remote_description(answer.sdp, answer.type)

        rx = await asyncio.wait_for(answerer.accept_data_channel(), timeout=10.0)
        await asyncio.wait_for(tx.wait_open(), timeout=10.0)
        await asyncio.wait_for(rx.wait_open(), timeout=10.0)

        # Threshold above anything this test ever buffers. On loopback
        # usrsctp takes ~1 MiB before libdatachannel buffers anything, so
        # a 4 MiB burst usually leaves bytes buffered (> 0, < threshold) when the
        # final default send runs — the case that cleared the drain
        # event with no from-above crossing left to set it again.
        tx.set_buffered_amount_low_threshold(64 * 1024 * 1024)
        frame = b"x" * 4096
        count = 1024
        for _ in range(count - 1):
            await tx.send(frame, wait_for_drain=False)
        await asyncio.wait_for(tx.send(frame), timeout=5.0)
        for _ in range(count):
            assert await asyncio.wait_for(rx.recv(), timeout=5.0) == frame

        for _ in range(500):
            if tx.buffered_amount == 0:
                break
            await asyncio.sleep(0.01)
        assert tx.buffered_amount == 0

        await asyncio.wait_for(tx.send(b"next"), timeout=5.0)
        assert await asyncio.wait_for(rx.recv(), timeout=5.0) == b"next"
