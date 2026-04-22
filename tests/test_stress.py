"""Soak tests — long-running scenarios that would hide a slow leak or a
timing regression from the smoke suite.

Default-skipped. Enable with::

    AIOLIB_STRESS=1 pytest -m stress
    # or
    pytest -m stress

Both the ``native`` marker (real libdatachannel build required) and the
``stress`` marker (long runtime) are applied module-wide. CI runs the
release workflow with ``AIOLIB_STRESS=1`` so built wheels exercise
these paths before publishing.

What's covered:

* **High-volume DataChannel I/O** — two PCs negotiate loopback,
  exchange 10 000 messages in both directions, and verify every
  payload arrives in order. Stresses the message trampoline, the
  send/recv queues, buffered-amount bookkeeping, and the backpressure
  path.
* **Lifecycle churn** — 100 PC create/destroy cycles (bare + with
  DataChannel). The earlier nanobind port leaked a handle per PC; at
  100 iterations the leak compounded into ``nanobind: leaked 100+``
  at exit. This test would have caught it; asserting clean shutdown
  locks in the fix.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from aiolibdatachannel import (
    ConnectionClosedError,
    PeerConnection,
    RTCConfiguration,
)

pytestmark = [pytest.mark.native, pytest.mark.stress]


# ─── Shared helpers ──────────────────────────────────────────────────────


async def _forward(src: PeerConnection, dst: PeerConnection) -> None:
    """Drain ICE candidates from ``src`` into ``dst``."""
    async for cand in src.ice_candidates():
        await dst.add_remote_candidate(cand.candidate, cand.mid)


async def _negotiate(
    offerer: PeerConnection, answerer: PeerConnection,
) -> None:
    """Full trickle-ICE negotiation between two local PCs."""
    offer = await offerer.set_local_description("offer")
    await answerer.set_remote_description(offer.sdp, offer.type)
    answer = await answerer.set_local_description("answer")
    await offerer.set_remote_description(answer.sdp, answer.type)


# ─── High-volume I/O ──────────────────────────────────────────────────────

# Total round-trips per direction. Ten batches of 1000 ping-pongs each.
# The batching keeps the receive queue from overflowing: the wrapper's
# default ``recv_buffer=1024`` drops the oldest message when the queue
# fills, so an uninterleaved 10k-in-a-row send would silently lose 9000
# messages. Batched, the consumer drains each 1000 before the next
# batch ships, exercising exactly the same callback + send/recv paths
# end-to-end with every message accounted for.
BATCHES = 10
BATCH_SIZE = 1_000
TOTAL = BATCHES * BATCH_SIZE
LOOP_TIMEOUT_S = 60.0


@pytest.mark.asyncio
async def test_loopback_ten_thousand_messages() -> None:
    """Exchange 10 000 messages in each direction over a live loopback.

    Assertions:
    * Every sent payload arrives at the other side.
    * Payloads arrive in order (we number them).
    * No payloads are dropped on either side.
    * Clean teardown — no hung tasks, no unretrieved futures.

    The producer uses ``wait_for_drain=False`` because the default
    (block on the ``buffered_amount_low`` event) throttles to
    ~one-send-per-loop-iteration, which would turn the test into a
    timeout. High-volume producers are expected to skip per-send
    drain waits and rely on SCTP's own flow-control instead.

    We run ``BATCHES`` rounds of ``BATCH_SIZE`` messages; each round
    drains fully before the next starts, which keeps pressure on the
    default recv queue bounded and lets us verify every single
    payload.
    """
    cfg = RTCConfiguration()
    async with (
        PeerConnection(cfg) as offerer,
        PeerConnection(cfg) as answerer,
    ):
        offerer_dc = await offerer.create_data_channel("soak")
        offerer.spawn_task(_forward(offerer, answerer))
        answerer.spawn_task(_forward(answerer, offerer))

        await _negotiate(offerer, answerer)
        incoming = await asyncio.wait_for(
            answerer.accept_data_channel(), timeout=10.0,
        )
        await asyncio.wait_for(offerer_dc.wait_open(), timeout=10.0)
        await asyncio.wait_for(incoming.wait_open(), timeout=10.0)

        async def _round_trip(
            tx, rx, *, tag: str, start: int,
        ) -> list[int]:
            async def produce() -> None:
                for i in range(start, start + BATCH_SIZE):
                    await tx.send(f"{tag}:{i}".encode(), wait_for_drain=False)

            async def consume() -> list[int]:
                seen: list[int] = []
                while len(seen) < BATCH_SIZE:
                    msg = await rx.recv()
                    got_tag, _, n = msg.decode().partition(":")
                    assert got_tag == tag
                    seen.append(int(n))
                return seen

            _, seen = await asyncio.gather(produce(), consume())
            return seen

        started = time.monotonic()
        all_o2a: list[int] = []
        all_a2o: list[int] = []
        for batch in range(BATCHES):
            o2a, a2o = await asyncio.wait_for(
                asyncio.gather(
                    _round_trip(
                        offerer_dc, incoming,
                        tag="o2a",
                        start=batch * BATCH_SIZE,
                    ),
                    _round_trip(
                        incoming, offerer_dc,
                        tag="a2o",
                        start=batch * BATCH_SIZE,
                    ),
                ),
                timeout=LOOP_TIMEOUT_S / BATCHES + 5.0,
            )
            all_o2a.extend(o2a)
            all_a2o.extend(a2o)

        elapsed = time.monotonic() - started

        assert all_o2a == list(range(TOTAL)), (
            f"lost/reordered o→a; first gap at index "
            f"{next((i for i, v in enumerate(all_o2a) if v != i), -1)}"
        )
        assert all_a2o == list(range(TOTAL)), (
            f"lost/reordered a→o; first gap at index "
            f"{next((i for i, v in enumerate(all_a2o) if v != i), -1)}"
        )
        # Sanity: a sub-100ms run means we didn't actually round-trip
        # through libdatachannel — probably the wiring is short-circuited.
        assert elapsed > 0.1, (
            f"suspiciously fast ({elapsed:.3f}s); are sends real?"
        )


# ─── Lifecycle churn ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hundred_pc_lifecycles_no_handle_exhaustion() -> None:
    """Bare PC cycling: 100× create + aclose. Each iteration mints a
    fresh handle; if libdatachannel's handle allocator or our registry
    leaked, this would slow down or OOM.
    """
    for i in range(100):
        async with PeerConnection():
            pass
        if i % 25 == 24:
            # Give the thread pool a chance to drain.
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_hundred_pc_with_datachannel_cycles() -> None:
    """Each iteration creates a PC, attaches a DC, generates an offer,
    then tears down. Exercises all 5 DataChannel callbacks + all 7 PC
    callbacks per iteration — 1200 callback registrations total.
    """
    for _ in range(100):
        async with PeerConnection() as pc:
            await pc.create_data_channel("churn")
            await pc.set_local_description("offer")


# ─── Leak-free stress under ``-X dev`` ───────────────────────────────────


def test_stress_shutdown_has_no_leak_warnings() -> None:
    """Run a compressed version of the above stress scenarios in a
    subprocess under ``-X dev`` and grep for ``nanobind: leaked``.

    Size reduced to 20 PC cycles + 500 messages so the subprocess
    completes inside 30 s even on the slowest CI runner; the structural
    behaviour under scrutiny (ref-cycle cleanup, un-retrieved futures)
    shows up at any size. The pure ``stress`` test above runs the full
    10k-message workload.
    """
    repo_root = Path(__file__).parent.parent
    env = dict(os.environ)
    env["AIOLIB_REQUIRE_NATIVE"] = "1"
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")

    script = textwrap.dedent(
        """
        import asyncio
        from aiolibdatachannel import PeerConnection, RTCConfiguration


        async def forward(src, dst):
            async for cand in src.ice_candidates():
                await dst.add_remote_candidate(cand.candidate, cand.mid)


        async def loopback_500():
            cfg = RTCConfiguration()
            async with (
                PeerConnection(cfg) as o,
                PeerConnection(cfg) as a,
            ):
                dc = await o.create_data_channel("chan")
                o.spawn_task(forward(o, a))
                a.spawn_task(forward(a, o))
                offer = await o.set_local_description("offer")
                await a.set_remote_description(offer.sdp, offer.type)
                answer = await a.set_local_description("answer")
                await o.set_remote_description(answer.sdp, answer.type)
                incoming = await asyncio.wait_for(
                    a.accept_data_channel(), timeout=10.0,
                )
                await asyncio.wait_for(dc.wait_open(), timeout=10.0)
                await asyncio.wait_for(incoming.wait_open(), timeout=10.0)
                for i in range(500):
                    await dc.send(f"{i}".encode())
                got = []
                while len(got) < 500:
                    got.append(await incoming.recv())
                assert len(got) == 500


        async def churn_20():
            for _ in range(20):
                async with PeerConnection() as pc:
                    await pc.create_data_channel("c")
                    await pc.set_local_description("offer")


        async def main():
            await loopback_500()
            await churn_20()


        asyncio.run(main())
        """
    )
    proc = subprocess.run(
        [sys.executable, "-X", "dev", "-c", script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=120.0,
    )
    out = proc.stdout
    assert proc.returncode == 0, (
        f"subprocess exited with {proc.returncode}; output:\n{out}"
    )
    assert "nanobind:" not in out.lower() or "leaked" not in out.lower(), (
        f"nanobind leak message:\n{out}"
    )
    assert "Future exception was never retrieved" not in out, (
        f"un-retrieved future:\n{out}"
    )


# ─── Cancellation under load ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_during_high_volume_send_is_clean() -> None:
    """Cancel a producer mid-stream; the teardown path should not leak
    a future nor deadlock on rtcDeleteDataChannel.
    """
    cfg = RTCConfiguration()
    async with (
        PeerConnection(cfg) as offerer,
        PeerConnection(cfg) as answerer,
    ):
        dc = await offerer.create_data_channel("cancel")
        offerer.spawn_task(_forward(offerer, answerer))
        answerer.spawn_task(_forward(answerer, offerer))
        await _negotiate(offerer, answerer)
        incoming = await asyncio.wait_for(
            answerer.accept_data_channel(), timeout=10.0,
        )
        await asyncio.wait_for(dc.wait_open(), timeout=10.0)
        await asyncio.wait_for(incoming.wait_open(), timeout=10.0)

        async def flood() -> None:
            i = 0
            while True:
                try:
                    await dc.send(f"{i}".encode())
                except ConnectionClosedError:
                    return
                i += 1

        flooder = asyncio.create_task(flood())
        await asyncio.sleep(0.05)
        flooder.cancel()
        try:
            await flooder
        except asyncio.CancelledError:
            pass
