"""Regression: ``DataChannel.send(wait_for_drain=True)`` must unblock
when the underlying SCTP channel closes mid-wait.

Before the fix, ``_handle_closed`` cleared the recv-queue + failed the
open-slot but never touched ``_buffered_low_event``. A sender that
landed in ``send()`` while ``buffered_amount > low_watermark``
(``_buffered_low_event`` cleared) parked on ``event.wait()`` — the only
caller that would have set the event was the native
``buffered_amount_low`` callback, which never fires once the channel
is torn down. The sender hung indefinitely.

The fix sets the event in ``_handle_closed`` and re-checks the close
flags inside ``send()`` after the wait so the post-await branch
surfaces :class:`ConnectionClosedError` instead of attempting a send
on a torn-down handle.
"""

from __future__ import annotations

import asyncio
import os

import pytest

# This test pokes at the fake native's ``close_dc`` helper to drive
# the close callback synchronously — equivalent to libdatachannel's
# ``rtcCloseDataChannel`` returning. Real-build runs can't simulate
# the race deterministically here; skip at collection time.
if os.environ.get("AIOLIB_REQUIRE_NATIVE"):
    pytest.skip(
        "fake-only — drives the close callback via _fake_native helpers",
        allow_module_level=True,
    )

from aiolibdatachannel import ConnectionClosedError, PeerConnection
from aiolibdatachannel._native import (  # type: ignore[attr-defined]
    _dcs,
    close_dc,
)


@pytest.mark.asyncio
async def test_send_wait_for_drain_unblocks_on_close() -> None:
    async with PeerConnection() as pc:
        dc = await pc.create_data_channel("backpressure")
        handle = dc._native._handle  # type: ignore[attr-defined]
        # Mark the channel as open from the fake's side so ``send`` can
        # progress past the initial ``is_closed`` guard at the top.
        _dcs[handle]["open"] = True

        # Force the low-water event into "not set" so the next ``send``
        # parks on ``event.wait()`` — same shape as a real channel
        # whose ``buffered_amount`` blew past the HWM.
        dc._buffered_low_event.clear()

        sender = asyncio.create_task(dc.send(b"payload", wait_for_drain=True))
        # Let the task land on the await.
        await asyncio.sleep(0)
        assert not sender.done(), "expected send to park on buffered_low_event"

        # Close the channel mid-send — the fix sets the event so the
        # waiter unblocks and raises ConnectionClosedError instead of
        # hanging or attempting a write on a torn-down stream.
        close_dc(handle)

        with pytest.raises(ConnectionClosedError):
            await asyncio.wait_for(sender, timeout=1.0)


@pytest.mark.asyncio
async def test_send_returns_normally_when_event_already_set() -> None:
    """Sanity: the fix doesn't break the happy path where the event was
    set all along (channel never approached the HWM)."""

    async with PeerConnection() as pc:
        dc = await pc.create_data_channel("normal")
        handle = dc._native._handle  # type: ignore[attr-defined]
        _dcs[handle]["open"] = True
        # Event starts set in __init__; nothing to do.
        await dc.send(b"payload", wait_for_drain=True)
