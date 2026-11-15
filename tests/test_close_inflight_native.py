"""Regression on the real libdatachannel: ``close()`` while a remote
candidate is on its way to the native layer.

The executor holds the candidate call for 200 ms (what a busy pool or
GIL contention does anyway). Before the fix the teardown deleted the
peer connection first and libdatachannel logged ``PeerConnection ID
does not exist`` at ERROR, the call raising ``RTCError``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from collections.abc import Callable
from typing import Any

import pytest

from aiolibdatachannel import LogLevel, PeerConnection, install_python_logger

pytestmark = pytest.mark.native

_CAND = "candidate:1 1 UDP 2122252543 127.0.0.1 40000 typ host"


class _SlowFirstJobExecutor(concurrent.futures.ThreadPoolExecutor):
    """Holds the first job (the candidate call) for 200 ms; the teardown's
    delete, submitted after it, runs at once."""

    _held = False

    def submit(  # type: ignore[override]
        self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
    ) -> concurrent.futures.Future[Any]:
        if not self._held:
            self._held = True
            inner = fn

            def fn(*a: Any, **k: Any) -> Any:
                time.sleep(0.2)
                return inner(*a, **k)

        return super().submit(fn, *args, **kwargs)


@pytest.mark.asyncio
async def test_close_under_an_in_flight_candidate_logs_no_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("aiolibdatachannel.test_close_inflight")
    install_python_logger(logger, level=LogLevel.WARNING)
    caplog.set_level(logging.WARNING, logger=logger.name)

    offerer, answerer = PeerConnection(), PeerConnection()
    await offerer.create_data_channel("x")
    offer = await offerer.set_local_description("offer")
    await answerer.set_remote_description(offer.sdp, offer.type)

    loop = asyncio.get_running_loop()
    executor = _SlowFirstJobExecutor(4)
    loop.set_default_executor(executor)
    call = asyncio.create_task(answerer.add_remote_candidate(_CAND, "0"))
    await asyncio.sleep(0.05)  # the call is in an executor thread
    answerer.close()
    await call  # applied on a live handle: no RTCError
    await asyncio.wait_for(answerer.wait_closed(), 10)
    await offerer.aclose()

    assert not [r for r in caplog.records if "does not exist" in r.getMessage()]
