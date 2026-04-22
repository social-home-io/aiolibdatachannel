"""Shutdown-hygiene regression tests.

Locks in two guarantees that the nanobind migration introduced and that
regress easily if someone edits the wrapper or the binding:

1. **No un-retrieved futures.** Creating a DataChannel inside
   ``async with PeerConnection()`` and letting the block exit without
   calling ``wait_open()`` must not emit a ``Future exception was never
   retrieved`` warning. The latched slot's ``fail()`` marks the
   exception retrieved via a done-callback.
2. **No nanobind leaks.** Python interpreter shutdown after repeated
   PeerConnection lifecycles must not print ``nanobind: leaked N
   instances/functions``. The single-dispatcher pattern rules out the
   reference cycle the earlier port had.

Both checks run as subprocesses so we can observe the interpreter's
exit output (nanobind writes to stderr on teardown, and the
unretrieved-future warning also appears at GC time). ``-X dev`` is
set so those messages are amplified.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.native


def _run(script: str, *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    """Run a Python snippet in a subprocess under ``-X dev`` and return
    its CompletedProcess, merging stderr into stdout so we can grep both.

    The subprocess inherits the parent's ``sys.path`` (via the default
    behaviour of ``sys.executable``): in local dev that's the editable
    install, in cibuildwheel's test pass that's the venv with the
    installed wheel. We deliberately do NOT set ``PYTHONPATH`` to the
    source tree — on an installed wheel that would shadow the real
    package with the source checkout, which doesn't contain the
    compiled ``_native`` extension and fails with ``ImportError``.
    """
    env = dict(os.environ)
    env["AIOLIB_REQUIRE_NATIVE"] = "1"
    return subprocess.run(
        [sys.executable, "-X", "dev", "-c", textwrap.dedent(script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )


def _assert_clean_exit(proc: subprocess.CompletedProcess[str]) -> None:
    """Fail the test if the subprocess emitted any nanobind or
    unretrieved-future warnings. Regex rather than substring to catch
    variants like ``nanobind: leaked 2 instances!``.
    """
    out = proc.stdout
    assert proc.returncode == 0, f"subprocess exit {proc.returncode}; output:\n{out}"
    assert "nanobind:" not in out.lower() or "leaked" not in out.lower(), (
        f"nanobind leak message in output:\n{out}"
    )
    assert "Future exception was never retrieved" not in out, (
        f"un-retrieved future warning in output:\n{out}"
    )


# ─── Un-retrieved futures ─────────────────────────────────────────────


def test_datachannel_create_and_teardown_no_unretrieved_future() -> None:
    """Creating a DC inside ``async with PC()`` + exiting without touching
    the DC must not leak a ``ConnectionClosedError`` on the open slot.
    This is the exact shape that triggered the bug we fixed in
    :func:`FutureSlot.fail` — the DC's ``_open_slot.future`` got an
    exception but nobody awaited ``wait_open()``.
    """
    proc = _run(
        """
        import asyncio
        import aiolibdatachannel as rtc

        async def main():
            async with rtc.PeerConnection() as pc:
                await pc.create_data_channel("probe")
                await pc.set_local_description("offer")
                async for _ in pc.ice_candidates():
                    break

        asyncio.run(main())
        """
    )
    _assert_clean_exit(proc)


def test_peer_connection_close_during_set_local_description() -> None:
    """A PC closed right after set_local_description, without awaiting
    anything else, must also exit clean — exercises the PC-level
    ``_local_description`` slot teardown path."""
    proc = _run(
        """
        import asyncio
        import aiolibdatachannel as rtc

        async def main():
            pc = rtc.PeerConnection()
            await pc.create_data_channel("probe")
            await pc.set_local_description("offer")
            await pc.aclose()

        asyncio.run(main())
        """
    )
    _assert_clean_exit(proc)


def test_futureslot_fail_without_await_is_silent() -> None:
    """Unit-level repro: FutureSlot.fail() without anyone awaiting
    should not emit warnings at GC time."""
    proc = _run(
        """
        import asyncio
        from aiolibdatachannel._loop import FutureSlot

        async def main():
            loop = asyncio.get_running_loop()
            slot = FutureSlot(loop)
            slot.fail(RuntimeError("nobody is listening"))
            # Intentionally never await slot.future.
            await asyncio.sleep(0)

        asyncio.run(main())
        """
    )
    _assert_clean_exit(proc)


# ─── Nanobind leak checks ─────────────────────────────────────────────


def test_single_pc_no_nanobind_leak() -> None:
    """Smoke: one PC created and cleanly closed should not leak."""
    proc = _run(
        """
        import asyncio
        import aiolibdatachannel as rtc

        async def main():
            async with rtc.PeerConnection():
                pass

        asyncio.run(main())
        """
    )
    _assert_clean_exit(proc)


def test_twenty_pc_lifecycles_no_nanobind_leak() -> None:
    """Stress: 20 PC create/close cycles must not print any
    ``nanobind: leaked N`` messages at interpreter exit. The earlier
    nanobind port would leak one handle per PC because each
    ``State`` struct kept an ``nb::object`` callback ref that the
    Python GC couldn't see past."""
    proc = _run(
        """
        import asyncio
        import aiolibdatachannel as rtc

        async def main():
            for _ in range(20):
                async with rtc.PeerConnection() as pc:
                    pass

        asyncio.run(main())
        """,
        timeout=30.0,
    )
    _assert_clean_exit(proc)


def test_pc_with_datachannel_cycles_no_leak() -> None:
    """Each cycle creates a PC + DC, generates an offer, then tears
    down. The DC path is the one most likely to leak because it
    registers five callbacks (open/closed/error/message/buffered-low)
    which were separate ``nb::object`` fields in the old design."""
    proc = _run(
        """
        import asyncio
        import aiolibdatachannel as rtc

        async def main():
            for _ in range(5):
                async with rtc.PeerConnection() as pc:
                    await pc.create_data_channel("chan")
                    await pc.set_local_description("offer")

        asyncio.run(main())
        """,
        timeout=30.0,
    )
    _assert_clean_exit(proc)


def test_events_iterator_drained_no_leak() -> None:
    """Drain the unified :meth:`events` iterator during close —
    confirms the iterator termination path doesn't leave pending
    callbacks holding native state."""
    proc = _run(
        """
        import asyncio
        import aiolibdatachannel as rtc

        async def main():
            async with rtc.PeerConnection() as pc:
                await pc.create_data_channel("probe")
                async def drain():
                    async for _ in pc.events():
                        pass
                t = asyncio.create_task(drain())
                await pc.set_local_description("offer")
                await asyncio.sleep(0.1)
                # Block exits → __aexit__ aclose() cancels drain via
                # sentinel + teardown.
            await t

        asyncio.run(main())
        """
    )
    _assert_clean_exit(proc)


# ─── GIL-release verification ──────────────────────────────────────────


def test_cancel_mid_negotiation_does_not_deadlock() -> None:
    """Regression for the hang that motivated the original
    cffi migration. Cancelling a task mid-``create_offer`` must not
    deadlock on ``rtcDeletePeerConnection``. The subprocess has a
    hard 15 s cap; if we hang longer the test fails from timeout."""
    proc = _run(
        """
        import asyncio
        import aiolibdatachannel as rtc

        async def main():
            pc = rtc.PeerConnection()
            await pc.create_data_channel("cancelme")
            task = asyncio.create_task(pc.create_offer())
            await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await pc.aclose()

        asyncio.run(main())
        """,
        timeout=15.0,
    )
    _assert_clean_exit(proc)
