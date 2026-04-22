"""Error translation tests."""

from __future__ import annotations

import pytest

from aiolibdatachannel import PeerConnection, RTCError

# Real libdatachannel behaviour required (DTLS / ICE / state machine).
pytestmark = pytest.mark.native


@pytest.mark.asyncio
async def test_invalid_remote_description_raises() -> None:
    async with PeerConnection() as pc:
        with pytest.raises(RTCError):
            await pc.set_remote_description("not-a-valid-sdp", "offer")


@pytest.mark.asyncio
async def test_invalid_candidate_raises() -> None:
    async with PeerConnection() as pc:
        await pc.create_data_channel("dummy")
        await pc.set_local_description("offer")
        with pytest.raises(RTCError):
            await pc.add_remote_candidate("not-a-candidate-line", "0")
