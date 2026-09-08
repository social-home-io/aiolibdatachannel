"""Regression: registering a callback on an ALREADY-OPEN handle must not
deadlock the event loop.

``rtcSet*Callback`` takes the channel's internal ``synchronized_callback``
recursive mutex, and — when the channel is already open — invokes the
callback synchronously before returning. Before the fix, the binding held
the GIL across that call, which closes a two-thread cycle:

* a libdatachannel worker thread is inside ``tr_dc_message``, holding the
  channel mutex, blocked acquiring the GIL;
* the event-loop thread holds the GIL and blocks on that same mutex inside
  ``rtcSetMessageCallback``.

Neither side can advance. The whole loop wedges at 0% CPU, unrecoverable —
the faulthandler dump shows the main thread parked in
``rtcSetOpenCallback -> rtc::Channel::onOpen -> onAvailable ->
std::recursive_mutex::lock``. Trickle ICE makes this easy to hit in
production: the data channel is already open by the time the wrapper
registers its callbacks.

The fix routes every ``set_*_callback`` setter through the binding's
``no_gil`` helper, so the worker can take the GIL, finish its callback and
drop the mutex while the loop thread waits.

Why this test looks the way it does:

* **It pokes at ``_core``.** Re-registering on an open, busy channel is the
  narrowest way to hit the exact C entry points; going through the public
  API only reaches them once, at construction, and only wins the race by
  luck.
* **It runs in a subprocess.** A regression here is a genuine deadlock with
  the GIL held, so ``pytest-timeout``'s SIGALRM handler — which needs the
  GIL to run — never fires. In-process, a regression would hang the entire
  test session instead of failing it. A child interpreter under
  ``subprocess.run(timeout=...)`` turns the wedge into a clean failure.
* **It is probabilistic.** The race needs a worker mid-callback at the
  moment the loop thread registers. Flooding both directions while
  re-registering in a tight loop hits it reliably in practice; a single
  iteration would not.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

pytestmark = [pytest.mark.native, pytest.mark.host_only]

# How long to hold the flood + re-registration overlap open. The race
# needs a worker mid-callback at the instant the loop thread registers, so
# what matters is sustained overlap, not a raw iteration count.
SOAK_S = 5.0

# Floor on work actually performed, asserted from the child's counters.
# Without this the test could "pass" by doing nothing at all — an empty
# run exercises no race and would mask the very regression it guards.
MIN_REREGISTRATIONS = 500
MIN_MESSAGES = 500

# Generous: the child needs SOAK_S plus handshake and teardown. Exceeding
# this means we deadlocked, which is exactly what we are testing for.
CHILD_TIMEOUT_S = 90.0

_CHILD = """
    import asyncio
    import time

    from aiolibdatachannel import PeerConnection, RTCConfiguration

    SOAK_S = {soak}


    async def _forward(src, dst):
        async for cand in src.ice_candidates():
            await dst.add_remote_candidate(cand.candidate, cand.mid)


    async def main():
        cfg = RTCConfiguration()
        async with PeerConnection(cfg) as offerer, PeerConnection(cfg) as answerer:
            dc = await offerer.create_data_channel("deadlock")
            offerer.spawn_task(_forward(offerer, answerer))
            answerer.spawn_task(_forward(answerer, offerer))

            offer = await offerer.set_local_description("offer")
            await answerer.set_remote_description(offer.sdp, offer.type)
            answer = await answerer.set_local_description("answer")
            await offerer.set_remote_description(answer.sdp, answer.type)

            incoming = await asyncio.wait_for(
                answerer.accept_data_channel(), timeout=15.0
            )
            await asyncio.wait_for(dc.wait_open(), timeout=15.0)
            await asyncio.wait_for(incoming.wait_open(), timeout=15.0)

            stop = asyncio.Event()
            counts = {{"rereg": 0, "recv": 0}}

            async def flood(tx):
                # Keep libdatachannel's workers inside tr_dc_message (and
                # therefore holding the channel mutex) as much as possible.
                payload = b"x" * 256
                while not stop.is_set():
                    try:
                        await tx.send(payload, wait_for_drain=False)
                    except Exception:
                        # Buffer pressure / teardown. Irrelevant here: the
                        # test asserts liveness, not delivery.
                        await asyncio.sleep(0.01)
                    await asyncio.sleep(0)

            async def drain(rx):
                while not stop.is_set():
                    try:
                        await asyncio.wait_for(rx.recv(), timeout=0.2)
                        counts["recv"] += 1
                    except Exception:
                        await asyncio.sleep(0)

            async def rereg():
                # The call under test. Every iteration re-enters
                # rtcSetMessageCallback / rtcSetBufferedAmountLowCallback on
                # an OPEN channel from the loop thread. The sleep(0) each
                # pass is load-bearing: it hands the GIL to the workers so
                # they are genuinely mid-callback when we come back around.
                deadline = time.monotonic() + SOAK_S
                while time.monotonic() < deadline:
                    dc._native.set_on_message(dc._cb_message)
                    incoming._native.set_on_message(incoming._cb_message)
                    dc._native.set_on_buffered_amount_low(dc._cb_buffered_low)
                    counts["rereg"] += 1
                    await asyncio.sleep(0)
                stop.set()

            tasks = [
                asyncio.create_task(flood(dc)),
                asyncio.create_task(flood(incoming)),
                asyncio.create_task(drain(dc)),
                asyncio.create_task(drain(incoming)),
                asyncio.create_task(rereg()),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                stop.set()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        print("REREG", counts["rereg"])
        print("RECV", counts["recv"])
        print("NO_DEADLOCK")


    asyncio.run(main())
"""


def test_reregistering_callbacks_on_open_channel_does_not_deadlock() -> None:
    """Hammer ``set_*_callback`` on two open, message-flooded channels.

    Passes when the child prints ``NO_DEADLOCK``. A regression wedges the
    child's event loop with the GIL held, so it never prints and never
    exits — surfaced here as ``subprocess.TimeoutExpired``.
    """
    env = dict(os.environ)
    env["AIOLIB_REQUIRE_NATIVE"] = "1"
    script = textwrap.dedent(_CHILD).format(soak=SOAK_S)

    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=CHILD_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            "callback re-registration deadlocked: the child event loop made "
            f"no progress in {CHILD_TIMEOUT_S}s. This is the GIL/callback-mutex "
            "inversion — rtcSet*Callback must run inside the binding's no_gil "
            f"helper.\npartial output:\n{exc.output}"
        )

    assert proc.returncode == 0, f"child exit {proc.returncode}; output:\n{proc.stdout}"
    assert "NO_DEADLOCK" in proc.stdout, f"child never completed:\n{proc.stdout}"

    counts = {
        line.split()[0]: int(line.split()[1])
        for line in proc.stdout.splitlines()
        if line.startswith(("REREG ", "RECV "))
    }
    # Guard against a vacuous pass: if the child sailed through without
    # actually overlapping registration with in-flight callbacks, it proved
    # nothing about the race.
    assert counts.get("REREG", 0) >= MIN_REREGISTRATIONS, (
        f"only {counts.get('REREG', 0)} re-registrations in {SOAK_S}s — too few "
        f"to have exercised the race:\n{proc.stdout}"
    )
    assert counts.get("RECV", 0) >= MIN_MESSAGES, (
        f"only {counts.get('RECV', 0)} messages delivered — the workers were "
        f"never busy, so no callback mutex was ever held:\n{proc.stdout}"
    )
