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
    """A malformed candidate must raise once the connection has a
    remote description applied. (Without a remote description the
    wrapper *buffers* the candidate — see
    ``test_remote_candidate_buffering.py`` — so the raise only
    happens after the offer/answer SDP exchange is complete.)
    """
    async with (
        PeerConnection() as offerer,
        PeerConnection() as answerer,
    ):
        await offerer.create_data_channel("dummy")
        offer = await offerer.set_local_description("offer")
        await answerer.set_remote_description(offer.sdp, offer.type)
        # answerer now has a remote description — invalid candidate
        # bypasses the buffer and goes straight to libdatachannel.
        with pytest.raises(RTCError):
            await answerer.add_remote_candidate("not-a-candidate-line", "0")
