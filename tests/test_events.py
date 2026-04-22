"""Tests for the unified :meth:`PeerConnection.events` stream."""

from __future__ import annotations

import asyncio

import pytest

from aiolibdatachannel import (
    GatheringStateChangeEvent,
    LocalCandidateEvent,
    LocalDescriptionEvent,
    PCEvent,
    PeerConnection,
    SignalingStateChangeEvent,
    StateChangeEvent,
)

# Real libdatachannel behaviour required (DTLS / ICE / state machine).
pytestmark = pytest.mark.native

_TIMEOUT = 10.0


@pytest.mark.asyncio
async def test_events_emits_expected_types_during_gathering() -> None:
    """During a simple offer generation, we should see the core event types."""

    collected: list[PCEvent] = []
    pc = PeerConnection()

    async def drain() -> None:
        async for ev in pc.events():
            collected.append(ev)

    # Plain create_task (NOT spawn_task): we want drain() to exit via the
    # close-time sentinel, not be cancelled alongside PC-owned tasks.
    drain_task = asyncio.create_task(drain())

    try:
        await pc.create_data_channel("probe")
        await pc.set_local_description("offer")
        await asyncio.sleep(0.1)
    finally:
        await pc.aclose()

    await asyncio.wait_for(drain_task, timeout=_TIMEOUT)

    kinds = {type(ev) for ev in collected}
    assert LocalDescriptionEvent in kinds
    assert LocalCandidateEvent in kinds
    assert SignalingStateChangeEvent in kinds
    # One of gathering or state transitions must have been emitted.
    assert GatheringStateChangeEvent in kinds or StateChangeEvent in kinds


@pytest.mark.asyncio
async def test_events_terminates_on_close() -> None:
    """The stream ends cleanly when the PC is closed while the iterator is idle."""

    pc = PeerConnection()

    async def drain() -> None:
        async for _ev in pc.events():
            pass

    drain_task = asyncio.create_task(drain())
    await asyncio.sleep(0)

    await pc.aclose()

    # close() → None sentinel → drain() returned without cancellation.
    await asyncio.wait_for(drain_task, timeout=_TIMEOUT)
    assert drain_task.done()
    assert not drain_task.cancelled()