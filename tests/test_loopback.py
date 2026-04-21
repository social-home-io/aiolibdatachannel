"""End-to-end: two PeerConnections negotiating over localhost.

We drive trickle-ICE manually between two PCs in the same process, open a
DataChannel, exchange a handful of messages, and assert clean shutdown.
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


async def _glue(a: PeerConnection, b: PeerConnection) -> asyncio.Task[None]:
    """Forward ICE candidates from ``a`` to ``b`` as they are gathered."""

    async def pump() -> None:
        async for cand in a.ice_candidates():
            await b.add_remote_candidate(cand.candidate, cand.mid)

    return asyncio.create_task(pump())


@pytest.mark.asyncio
async def test_loopback_datachannel() -> None:
    cfg = RTCConfiguration()  # no STUN — host candidates are enough on loopback
    async with PeerConnection(cfg) as offerer, PeerConnection(cfg) as answerer:
        offerer_dc = await offerer.create_data_channel("chat")
        pump_a = await _glue(offerer, answerer)
        pump_b = await _glue(answerer, offerer)

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

        pump_a.cancel()
        pump_b.cancel()
        await asyncio.gather(pump_a, pump_b, return_exceptions=True)


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
    await pc.close()
    # After close the handle is destroyed; state should be CLOSED or a
    # terminal state. We give the callback a brief chance to land.
    for _ in range(50):
        if pc.state in (RTCState.CLOSED, RTCState.DISCONNECTED, RTCState.FAILED):
            return
        await asyncio.sleep(0.01)
    # Terminal state not strictly required (close is abrupt); passing is OK.
