"""Native loopback coverage for ``PeerConnection.restart_ice``.

Lives in a separate file from the fake-based suite because every test
here needs the compiled extension (real ICE flow) — the fake stubs
``set_local_description`` without re-running gathering.

Marked ``host_only`` so ``CIBUILDWHEEL=1`` (manylinux docker test
step) auto-skips it; the SCTP teardown hang documented in #12 affects
loopback PC pairs exactly the same way as ``test_shutdown_hygiene``.
The bare-metal ``tests-native`` CI job (ubuntu-latest + macos-latest
x Python 3.12 / 3.13 / 3.14) is where this test runs in CI.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from aiolibdatachannel import (
    PeerConnection,
    RTCConfiguration,
    RTCState,
)

pytestmark = [pytest.mark.native, pytest.mark.host_only]


async def _forward(src: PeerConnection, dst: PeerConnection) -> None:
    """Drain ICE candidates from ``src`` into ``dst`` until gathering ends."""

    async for cand in src.ice_candidates():
        await dst.add_remote_candidate(cand.candidate, cand.mid)


_UFRAG_RE = re.compile(r"^a=ice-ufrag:(\S+)", re.MULTILINE)
_PWD_RE = re.compile(r"^a=ice-pwd:(\S+)", re.MULTILINE)


def _ice_credentials(sdp: str) -> tuple[str, str]:
    ufrag = _UFRAG_RE.search(sdp)
    pwd = _PWD_RE.search(sdp)
    assert ufrag is not None, f"no ice-ufrag in sdp:\n{sdp}"
    assert pwd is not None, f"no ice-pwd in sdp:\n{sdp}"
    return ufrag.group(1), pwd.group(1)


@pytest.mark.asyncio
async def test_restart_ice_changes_credentials_and_keeps_datachannel_open() -> None:
    """``restart_ice`` re-runs ICE on a live PC pair: the new SDP
    carries fresh ufrag/pwd (proving libdatachannel actually did a
    restart, not a no-op), and the DataChannel established before
    the restart continues to carry traffic afterwards (proving the
    SCTP layer survives)."""

    cfg = RTCConfiguration()  # host candidates only — loopback
    async with PeerConnection(cfg) as offerer, PeerConnection(cfg) as answerer:
        offerer_dc = await offerer.create_data_channel("survives-restart")
        offerer.spawn_task(_forward(offerer, answerer))
        answerer.spawn_task(_forward(answerer, offerer))

        offer = await offerer.create_offer()
        await answerer.set_remote_description(offer.sdp, offer.type)
        answer = await answerer.create_answer()
        await offerer.set_remote_description(answer.sdp, answer.type)

        incoming = await asyncio.wait_for(answerer.accept_data_channel(), timeout=10.0)
        await asyncio.wait_for(offerer_dc.wait_open(), timeout=10.0)
        await asyncio.wait_for(incoming.wait_open(), timeout=10.0)
        await asyncio.wait_for(offerer.wait_for_state(RTCState.CONNECTED), timeout=10.0)

        ufrag1, pwd1 = _ice_credentials(offer.sdp)

        # ---- ICE restart ------------------------------------------------
        restart_offer = await offerer.restart_ice()
        await answerer.set_remote_description(restart_offer.sdp, restart_offer.type)
        restart_answer = await answerer.create_answer()
        await offerer.set_remote_description(restart_answer.sdp, restart_answer.type)

        ufrag2, pwd2 = _ice_credentials(restart_offer.sdp)
        assert (ufrag1, pwd1) != (ufrag2, pwd2), (
            "restart_ice produced the same ICE credentials as the original "
            "offer — libdatachannel didn't actually restart ICE"
        )

        # DataChannel must still carry traffic after the restart.
        await offerer_dc.send(b"after-restart")
        msg = await asyncio.wait_for(incoming.recv(), timeout=5.0)
        assert msg == b"after-restart"
