"""Regression: ``close()`` must not delete the native peer connection
under a call that is still on its way to libdatachannel.

``set_remote_description`` / ``add_remote_candidate`` run the native
call in an executor thread; ``close()`` schedules ``_teardown``, which
runs the native delete in another executor thread. Nothing ordered the
two, and ``_core.PeerConnection`` only checked ``_destroyed`` in
``close`` / ``destroy``: a ``close()`` landing while a candidate call
waited for its thread deleted the handle first, and the call then hit a
deleted id — libdatachannel's ``PeerConnection ID does not exist`` at
ERROR plus an ``RTCError`` to the caller.

Now ``_teardown`` waits for every native call already in an executor
thread (even when its awaiter was cancelled — a running executor job
cannot be), and a call made after the teardown raises
:class:`ConnectionClosedError` without touching the native layer.
"""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

if os.environ.get("AIOLIB_REQUIRE_NATIVE"):
    pytest.skip(
        "fake-only — gates and inspects _fake_native calls",
        allow_module_level=True,
    )

from aiolibdatachannel import (
    ConnectionClosedError,
    PeerConnection,
    _native,  # type: ignore[attr-defined]
)
from aiolibdatachannel._core import PeerConnection as CorePC

_CAND = "candidate:1 1 UDP 2122252543 192.0.2.1 1234 typ host"
_SDP = "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\na=offer\r\n"


class _Gate:
    """Wraps one fake native function: the call waits (in its executor
    thread) for :attr:`release`, then records whether its handle was
    already deleted — what libdatachannel logs as ``PeerConnection ID
    does not exist``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self.on_deleted_handle: list[int] = []
        self.calls = 0
        real = getattr(_native, name)

        def gated(pc: int, *args: object) -> object:
            self.calls += 1
            self.reached.set()
            assert self.release.wait(5)
            if pc not in _native._pcs:
                self.on_deleted_handle.append(pc)
            return real(pc, *args)

        monkeypatch.setattr(_native, name, gated)


async def _wait(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, 5)


@pytest.mark.asyncio
async def test_close_waits_for_an_in_flight_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pc = PeerConnection()
    await pc.set_remote_description(_SDP, "offer")
    gate = _Gate(monkeypatch, "add_remote_candidate")
    call = asyncio.create_task(pc.add_remote_candidate(_CAND, "0"))
    await _wait(gate.reached)
    pc.close()
    await asyncio.sleep(0.2)  # the teardown would have deleted it here
    assert not pc._closed_event.is_set()
    gate.release.set()
    await call
    await asyncio.wait_for(pc.wait_closed(), 5)
    assert gate.on_deleted_handle == []


@pytest.mark.asyncio
async def test_close_waits_for_an_in_flight_remote_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pc = PeerConnection()
    gate = _Gate(monkeypatch, "set_remote_description")
    call = asyncio.create_task(pc.set_remote_description(_SDP, "offer"))
    await _wait(gate.reached)
    pc.close()
    await asyncio.sleep(0.2)
    gate.release.set()
    await call
    await asyncio.wait_for(pc.wait_closed(), 5)
    assert gate.on_deleted_handle == []


@pytest.mark.asyncio
async def test_a_cancelled_awaiter_does_not_let_the_teardown_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loop shutdown cancels the awaiting task; the executor thread runs
    on regardless, so the teardown must still wait for it."""
    pc = PeerConnection()
    await pc.set_remote_description(_SDP, "offer")
    gate = _Gate(monkeypatch, "add_remote_candidate")
    call = asyncio.create_task(pc.add_remote_candidate(_CAND, "0"))
    await _wait(gate.reached)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    pc.close()
    await asyncio.sleep(0.2)
    assert not pc._closed_event.is_set()
    gate.release.set()
    await asyncio.wait_for(pc.wait_closed(), 5)
    assert gate.on_deleted_handle == []


@pytest.mark.asyncio
async def test_a_call_after_close_never_reaches_the_native_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pc = PeerConnection()
    await pc.set_remote_description(_SDP, "offer")
    gate = _Gate(monkeypatch, "add_remote_candidate")
    gate.release.set()
    await pc.aclose()
    with pytest.raises(ConnectionClosedError):
        await pc.add_remote_candidate(_CAND, "0")
    with pytest.raises(ConnectionClosedError):
        await pc.set_remote_description(_SDP, "offer")
    assert gate.calls == 0


def test_the_core_wrapper_refuses_a_destroyed_handle() -> None:
    """Sync calls straight on ``_core`` (``local_description`` and
    friends) after ``destroy`` raise instead of passing a deleted id to
    the native layer."""
    core = CorePC([])
    core.destroy()
    for call in (
        lambda: core.add_remote_candidate(_CAND, "0"),
        lambda: core.set_remote_description(_SDP, "offer"),
        core.get_local_description,
        core.get_remote_description,
        lambda: core.create_data_channel("x"),
    ):
        with pytest.raises(ConnectionClosedError):
            call()
