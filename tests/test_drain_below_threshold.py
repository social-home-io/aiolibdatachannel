"""Regression: ``send(wait_for_drain=True)`` must not hang when the send
buffer drains without ever rising above the low-water threshold.

libdatachannel fires ``buffered_amount_low`` only when the buffered
amount crosses the threshold *from above* (``previous > threshold &&
amount <= threshold`` in ``Channel::triggerBufferedAmount``). ``send()``
used to clear its drain event whenever *any* bytes were still buffered
after a send. With a threshold above the buffered amount (e.g. 512 KiB
threshold, a few KiB per frame) the buffer drained back to 0 without
crossing the threshold, no callback fired, the event stayed cleared and
the next ``send()`` waited forever.

The fake native keeps ``buffered_amount`` static and only fires
``buffered_amount_low`` when a test emits it, which mirrors exactly the
"no crossing → no callback" behaviour.
"""

from __future__ import annotations

import asyncio
import os

import pytest

if os.environ.get("AIOLIB_REQUIRE_NATIVE"):
    pytest.skip(
        "fake-only — drives buffered_amount via _fake_native state",
        allow_module_level=True,
    )

from aiolibdatachannel._native import (  # type: ignore[attr-defined]
    CB_DC_BUFFERED_AMOUNT_LOW,
    _dcs,
    emit,
)

from aiolibdatachannel import PeerConnection

_TIMEOUT = 1.0


@pytest.mark.asyncio
async def test_send_does_not_hang_when_buffer_stays_below_threshold() -> None:
    async with PeerConnection() as pc:
        dc = await pc.create_data_channel("below-threshold")
        handle = dc._native._handle  # type: ignore[attr-defined]
        _dcs[handle]["open"] = True
        dc.set_buffered_amount_low_threshold(512 * 1024)

        # A few KiB still in flight after the send — below the threshold.
        _dcs[handle]["buffered_amount"] = 4096
        await asyncio.wait_for(dc.send(b"x" * 4096), timeout=_TIMEOUT)

        # The buffer drains to 0 without crossing the threshold from
        # above, so libdatachannel never fires buffered_amount_low.
        _dcs[handle]["buffered_amount"] = 0
        await asyncio.wait_for(dc.send(b"next"), timeout=_TIMEOUT)


@pytest.mark.asyncio
async def test_raising_threshold_above_buffered_releases_waiter() -> None:
    async with PeerConnection() as pc:
        dc = await pc.create_data_channel("raise-threshold")
        handle = dc._native._handle  # type: ignore[attr-defined]
        _dcs[handle]["open"] = True

        # Default threshold 0: bytes in flight → real backpressure.
        _dcs[handle]["buffered_amount"] = 4096
        await dc.send(b"first")
        sender = asyncio.create_task(dc.send(b"second"))
        await asyncio.sleep(0)
        assert not sender.done()

        # Raising the threshold above what is buffered can never produce
        # a from-above crossing, so it must release the waiter itself.
        dc.set_buffered_amount_low_threshold(64 * 1024)
        await asyncio.wait_for(sender, timeout=_TIMEOUT)


@pytest.mark.asyncio
async def test_send_still_waits_when_buffer_is_above_threshold() -> None:
    """Backpressure is kept: above the threshold, the next send parks
    until libdatachannel reports the crossing."""

    async with PeerConnection() as pc:
        dc = await pc.create_data_channel("above-threshold")
        handle = dc._native._handle  # type: ignore[attr-defined]
        _dcs[handle]["open"] = True
        dc.set_buffered_amount_low_threshold(1024)

        _dcs[handle]["buffered_amount"] = 8192
        await dc.send(b"x" * 8192)

        sender = asyncio.create_task(dc.send(b"next"))
        await asyncio.sleep(0.05)
        assert not sender.done(), "expected send to wait for buffered_amount_low"

        _dcs[handle]["buffered_amount"] = 0
        emit(CB_DC_BUFFERED_AMOUNT_LOW, handle)
        await asyncio.wait_for(sender, timeout=_TIMEOUT)
