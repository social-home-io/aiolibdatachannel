"""Cancellation-safety tests — awaiters must not leak native handles."""

from __future__ import annotations

import asyncio

import pytest

from aiolibdatachannel import PeerConnection



# Real libdatachannel behaviour required (DTLS / ICE / state machine).
pytestmark = pytest.mark.native

@pytest.mark.asyncio
async def test_cancel_mid_negotiation() -> None:
    pc = PeerConnection()
    await pc.create_data_channel("cancelme")  # offer needs at least one m-line
    task = asyncio.create_task(pc.create_offer())
    await asyncio.sleep(0)  # let the task start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await pc.aclose()


@pytest.mark.asyncio
async def test_cancel_data_channel_recv() -> None:
    async with PeerConnection() as pc:
        dc = await pc.create_data_channel("cancelme")
        recv = asyncio.create_task(dc.recv())
        await asyncio.sleep(0.05)
        recv.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recv