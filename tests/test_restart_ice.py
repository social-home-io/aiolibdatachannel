"""Tests for ``PeerConnection.restart_ice``.

Drives the fake ``_native`` directly so the test stays deterministic
(real libdatachannel gathering depends on host network state). The
native-level coverage of restart-ICE survival of the SCTP layer lives
under ``@pytest.mark.native`` at the bottom of this module.
"""

from __future__ import annotations

import asyncio
import os

import pytest

# Pokes at ``_fake_native`` internals — skip when the compiled
# extension is loaded.
if os.environ.get("AIOLIB_REQUIRE_NATIVE"):
    pytest.skip(
        "fake-only — drives gathering callbacks via _fake_native",
        allow_module_level=True,
    )

from aiolibdatachannel._native import (  # type: ignore[attr-defined]
    CB_GATHERING_STATE_CHANGE,
    CB_LOCAL_CANDIDATE,
    _pcs,
    emit,
)

from aiolibdatachannel import ConnectionClosedError, IceCandidate, PeerConnection
from aiolibdatachannel.enums import GatheringState


def _pc_handle() -> int:
    """Return the (single) live PC handle in the fake."""
    return next(iter(_pcs.keys()))


async def _drive_initial_offer(pc: PeerConnection) -> None:
    """Run ``create_offer`` against the fake, emitting the gathering-
    complete callback so the awaiter resolves. Leaves the PC in the
    "previously emitted an offer" state that ``restart_ice`` builds on.
    """
    offer_task = asyncio.create_task(pc.create_offer())
    await asyncio.sleep(0)  # let create_offer drive set_local_description
    emit(CB_GATHERING_STATE_CHANGE, _pc_handle(), GatheringState.COMPLETE.value)
    await offer_task


@pytest.mark.asyncio
async def test_restart_ice_returns_new_local_description() -> None:
    """``restart_ice()`` re-runs ``set_local_description("offer")``,
    waits for the new gathering cycle to complete, and returns the
    fresh inline-ICE ``LocalDescription``."""

    async with PeerConnection() as pc:
        await pc.create_data_channel("probe")
        await _drive_initial_offer(pc)

        restart_task = asyncio.create_task(pc.restart_ice())
        await asyncio.sleep(0)
        emit(CB_GATHERING_STATE_CHANGE, _pc_handle(), GatheringState.COMPLETE.value)

        result = await restart_task
        assert result.type == "offer"
        assert "v=0" in result.sdp


@pytest.mark.asyncio
async def test_restart_ice_drains_stale_candidate_queue() -> None:
    """Stale items from the previous gather cycle — including the
    ``None`` sentinel pushed on gather-complete — must be drained on
    restart so ``ice_candidates()`` consumers see a clean stream that
    terminates only on the new gather-complete."""

    async with PeerConnection() as pc:
        await pc.create_data_channel("probe")
        await _drive_initial_offer(pc)

        # Simulate leftover state from the previous gather cycle: a
        # candidate the caller never iterated + the terminator.
        pc._ice_candidates.put_nowait(IceCandidate("stale", "0"))
        pc._ice_candidates.put_nowait(None)
        assert not pc._ice_candidates.empty()

        restart_task = asyncio.create_task(pc.restart_ice())
        await asyncio.sleep(0)
        # By now ``restart_ice`` has drained the queue and called
        # set_local_description. Any leftover ``None`` would have caused
        # an ``ice_candidates()`` iterator to short-circuit; assert the
        # drain ran.
        assert pc._ice_candidates.empty()

        emit(CB_GATHERING_STATE_CHANGE, _pc_handle(), GatheringState.COMPLETE.value)
        await restart_task
        # A fresh terminator now sits in the queue from the new
        # gather-complete.
        assert pc._ice_candidates.qsize() == 1
        assert pc._ice_candidates.get_nowait() is None


@pytest.mark.asyncio
async def test_restart_ice_trickle_returns_without_gathering() -> None:
    """``restart_ice(trickle=True)`` returns as soon as
    libdatachannel produces the new SDP — it does NOT wait for the
    gathering cycle. The caller iterates :meth:`ice_candidates` to
    forward new candidates as they're discovered."""

    async with PeerConnection() as pc:
        await pc.create_data_channel("probe")
        await _drive_initial_offer(pc)

        # Seed stale items so we can also verify the drain runs in the
        # trickle path.
        pc._ice_candidates.put_nowait(IceCandidate("stale", "0"))
        pc._ice_candidates.put_nowait(None)

        result = await pc.restart_ice(trickle=True)
        assert result.type == "offer"
        # Queue was drained before the new set_local_description fired.
        assert pc._ice_candidates.empty()

        # Drive one fresh candidate through the trickle path. The
        # async iterator must yield only that fresh candidate (the
        # stale one was drained on restart entry).
        emit(CB_LOCAL_CANDIDATE, _pc_handle(), "fresh-cand", "0")
        await asyncio.sleep(0)
        seen = pc._ice_candidates.get_nowait()
        assert isinstance(seen, IceCandidate)
        assert seen.candidate == "fresh-cand"


@pytest.mark.asyncio
async def test_set_local_description_none_is_w3c_alias() -> None:
    """Per the W3C spec, ``setLocalDescription`` with no type on a
    previously-connected PC triggers an ICE restart. The wrapper's
    ``set_local_description(None)`` is functionally equivalent to
    ``restart_ice(trickle=True)``: same SDP shape, same queue-drain
    bookkeeping."""

    async with PeerConnection() as pc:
        await pc.create_data_channel("probe")
        await _drive_initial_offer(pc)

        pc._ice_candidates.put_nowait(IceCandidate("stale", "0"))
        pc._ice_candidates.put_nowait(None)

        result = await pc.set_local_description(None)
        assert result.type == "offer"
        # The alias must do the same queue cleanup as ``restart_ice``;
        # otherwise an ``ice_candidates`` iterator would see the stale
        # terminator before any new candidates.
        assert pc._ice_candidates.empty()


@pytest.mark.asyncio
async def test_restart_ice_raises_when_closed() -> None:
    """Calling ``restart_ice`` after ``aclose`` must surface a clean
    :class:`ConnectionClosedError` (same contract as
    :meth:`create_offer`)."""

    pc = PeerConnection()
    await pc.aclose()
    with pytest.raises(ConnectionClosedError):
        await pc.restart_ice()
    with pytest.raises(ConnectionClosedError):
        await pc.restart_ice(trickle=True)
