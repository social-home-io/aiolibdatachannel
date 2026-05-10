"""Tests for the remote-candidate buffering window.

libdatachannel returns ``RTC_ERR_INVALID`` (-2) from
``rtcAddRemoteCandidate`` when no remote description has been set
yet — and the C++ side also logs an ``ERROR`` line for the same
condition. The signaling layer routinely surfaces ICE candidates
a beat ahead of the offer/answer they belong to (small wire-order
window when the candidate-batch frame and SDP frame overlap), so
the wrapper buffers candidates until ``set_remote_description``
applies the SDP, then drains them in arrival order.
"""

from __future__ import annotations

import os

import pytest

# These tests cover Python-side wrapper logic (the
# ``add_remote_candidate`` buffer + ``set_remote_description``
# drain). They reach into ``aiolibdatachannel._native._pcs`` /
# ``_errors`` to assert ordering + injected-failure semantics —
# both attributes only exist on the fake. The compiled extension
# exposes neither.
#
# Skip *at module-import time* when running with the real native
# build — otherwise collection itself fails because the import of
# ``aiolibdatachannel`` triggers the C++ extension load. ``pytest``
# evaluates ``pytestmark`` AFTER imports, so module-level imports
# below are guarded by an explicit pre-check.
if os.environ.get("AIOLIB_REQUIRE_NATIVE"):
    pytest.skip(
        "fake-only — pokes at _fake_native internals",
        allow_module_level=True,
    )

from aiolibdatachannel import PeerConnection  # noqa: E402


@pytest.mark.asyncio
async def test_candidate_before_remote_description_is_buffered() -> None:
    """A candidate handed in before the SDP must not raise — the
    wrapper holds it in a per-PC buffer and forwards on
    ``set_remote_description``."""

    async with PeerConnection() as pc:
        # No SDP yet — must NOT raise.
        await pc.add_remote_candidate(
            "candidate:1 1 UDP 2122252543 192.0.2.1 1234 typ host",
            "0",
        )
        # Apply an SDP. The buffered candidate flushes through.
        await pc.set_remote_description(
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\na=offer\r\n",
            "offer",
        )
        # The fake native records every accepted candidate; the buffer
        # drain landed before this point so a fresh candidate now is
        # accepted directly (proves the path through the non-buffered
        # branch still works).
        await pc.add_remote_candidate(
            "candidate:2 1 UDP 2122252542 192.0.2.2 1234 typ host",
            "0",
        )


@pytest.mark.asyncio
async def test_candidate_after_remote_description_is_passthrough() -> None:
    """Once the SDP is in place, candidates skip the buffer and go
    straight to libdatachannel."""

    async with PeerConnection() as pc:
        await pc.set_remote_description(
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\na=offer\r\n",
            "offer",
        )
        # No buffering involved — directly to native.
        await pc.add_remote_candidate(
            "candidate:1 1 UDP 2122252543 192.0.2.1 1234 typ host",
            "0",
        )


@pytest.mark.asyncio
async def test_buffer_drains_in_order() -> None:
    """Candidates flush through in arrival order, not natural-key or
    reverse — important for ICE candidate-pair selection ordering."""

    async with PeerConnection() as pc:
        await pc.add_remote_candidate("cand:A", "0")
        await pc.add_remote_candidate("cand:B", "0")
        await pc.add_remote_candidate("cand:C", "0")
        await pc.set_remote_description(
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\na=offer\r\n",
            "offer",
        )
        # Reach into the fake's pc-state to inspect arrival order.
        from aiolibdatachannel._native import _pcs  # type: ignore[attr-defined]

        info = next(iter(_pcs.values()))
        ordered = [c for c, _ in info.get("remote_candidates", [])]
        assert ordered == ["cand:A", "cand:B", "cand:C"]


@pytest.mark.asyncio
async def test_drain_failure_logs_and_continues(caplog) -> None:
    """One bad buffered candidate must not prevent the rest from
    flushing — each native call is independent.

    Mirrors the old (pre-buffer) behaviour where the caller would have
    retried after the SDP landed: a malformed line raises ``RTCError``
    on its own, but adjacent candidates still go through.
    """
    import logging

    from aiolibdatachannel._native import (  # type: ignore[attr-defined]
        _errors,  # type: ignore[attr-defined]
        _pcs,
    )

    async with PeerConnection() as pc:
        await pc.add_remote_candidate("cand:1", "0")
        await pc.add_remote_candidate("cand:2", "0")
        # Pre-arm a one-shot failure on the next
        # ``rtcAddRemoteCandidate`` call — that hits when the buffer
        # drains, between candidate 1 and 2.
        _errors["rtcAddRemoteCandidate"] = ("test injected", -2)
        with caplog.at_level(logging.DEBUG, logger="aiolibdatachannel.peer_connection"):
            await pc.set_remote_description(
                "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\na=offer\r\n",
                "offer",
            )
        info = next(iter(_pcs.values()))
        landed = [c for c, _ in info.get("remote_candidates", [])]
        # The first call trips the injected failure, the second still
        # lands. Either order would be a regression in the drain loop.
        assert "cand:2" in landed
        assert any("buffered remote candidate" in m for m in caplog.messages)
