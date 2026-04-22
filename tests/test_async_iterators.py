"""Tests pinning the self-termination contract on async iterators.

Every public async iterator on :class:`PeerConnection` must terminate
cleanly when the PC is closed — consumer code relies on plain
``async for`` without any defensive ``CancelledError`` handling. These
tests each run under :func:`asyncio.wait_for` so a regression that
re-introduces a hang fails fast instead of stalling CI.
"""

from __future__ import annotations

import asyncio

import pytest

from aiolibdatachannel import GatheringState, IceCandidate, PeerConnection

# Real libdatachannel behaviour required (DTLS / ICE / state machine).
pytestmark = pytest.mark.native

_TIMEOUT = 10.0


@pytest.mark.asyncio
async def test_ice_candidates_terminates_on_gathering_complete() -> None:
    """The iterator returns on its own once gathering reaches COMPLETE."""

    async def collect(pc: PeerConnection) -> list[IceCandidate]:
        return [cand async for cand in pc.ice_candidates()]

    async with PeerConnection() as pc:
        await pc.create_data_channel("probe")
        await pc.set_local_description("offer")
        candidates = await asyncio.wait_for(collect(pc), timeout=_TIMEOUT)

    assert candidates, "expected at least one host candidate from loopback gathering"
    assert pc.gathering_state is GatheringState.COMPLETE


@pytest.mark.asyncio
async def test_ice_candidates_terminates_on_close() -> None:
    """An idle iterator exits when ``aclose`` is called from another coroutine."""

    async with PeerConnection() as pc:
        sink: list[IceCandidate] = []

        async def drain() -> None:
            async for cand in pc.ice_candidates():
                sink.append(cand)

        task = pc.spawn_task(drain())
        # Give the task a tick to enter the iterator.
        await asyncio.sleep(0)

    # __aexit__ → aclose() → task terminated one way or another (either
    # the sentinel arrived before the cancel and the async-for returned,
    # or the cancel won). What matters is no hang.
    assert task.done()


@pytest.mark.asyncio
async def test_incoming_data_channels_terminates_on_close() -> None:
    """The ``incoming_data_channels`` iterator exits on close even when idle."""

    async with PeerConnection() as pc:

        async def drain() -> None:
            async for _dc in pc.incoming_data_channels():
                pass  # nothing will ever arrive in this test

        task = pc.spawn_task(drain())
        await asyncio.sleep(0)

    assert task.done()


@pytest.mark.asyncio
async def test_spawn_task_cancelled_on_close() -> None:
    """Long-sleeping spawned tasks get cancelled during ``aclose``."""

    pc = PeerConnection()

    async def nap() -> None:
        await asyncio.sleep(3600)

    task = pc.spawn_task(nap())
    await asyncio.sleep(0)

    await asyncio.wait_for(pc.aclose(), timeout=_TIMEOUT)
    assert task.done()
    # asyncio.gather(..., return_exceptions=True) in _teardown swallows
    # the CancelledError, so the task itself reports as cancelled.
    assert task.cancelled() or isinstance(task.exception(), asyncio.CancelledError)


@pytest.mark.asyncio
async def test_wait_closed_from_third_party() -> None:
    """``wait_closed`` lets another coroutine barrier on teardown without triggering it."""

    pc = PeerConnection()

    async def observer() -> None:
        await pc.wait_closed()

    obs = asyncio.create_task(observer())
    await asyncio.sleep(0)
    assert not obs.done(), "wait_closed must not return before close"

    await asyncio.wait_for(pc.aclose(), timeout=_TIMEOUT)
    await asyncio.wait_for(obs, timeout=_TIMEOUT)
    assert pc.closed


@pytest.mark.asyncio
async def test_sync_close_then_await_wait_closed() -> None:
    """Sync ``close()`` is fire-and-forget; ``wait_closed`` still resolves."""

    pc = PeerConnection()
    pc.close()
    assert pc.closed  # flag flipped synchronously
    await asyncio.wait_for(pc.wait_closed(), timeout=_TIMEOUT)
