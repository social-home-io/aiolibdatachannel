"""Logging integration tests."""

from __future__ import annotations

import logging

import pytest

from aiolibdatachannel import (
    LogLevel,
    PeerConnection,
    init_logger,
    install_python_logger,
)


@pytest.fixture(autouse=True)
def _reset_logger() -> None:
    """Undo any logger installation between tests."""

    yield
    # Re-arm a no-op native callback so later tests don't pick up our hook.
    init_logger(LogLevel.NONE, None)


@pytest.mark.native
@pytest.mark.asyncio
async def test_python_logger_receives_records(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("aiolibdatachannel.test")
    install_python_logger(logger, level=LogLevel.DEBUG)

    with caplog.at_level(logging.DEBUG, logger="aiolibdatachannel.test"):
        async with PeerConnection() as pc:
            await pc.create_data_channel("probe")

    # libdatachannel emits at least one line during peer-connection setup
    # and teardown; we don't pin wording, just the plumbing.
    assert any(
        record.name == "aiolibdatachannel.test" and record.message for record in caplog.records
    ), "no records routed through the installed Python logger"


def test_severity_mapping() -> None:
    """Python logger mapping picks the coarsest rtcLogLevel that still sees the target severity."""

    logger = logging.getLogger("aiolibdatachannel.test.severity")
    logger.setLevel(logging.WARNING)
    install_python_logger(logger)  # level derived from effective level
    # Nothing to assert beyond "didn't raise" — the native filter is opaque
    # from Python's side, but the function must accept this combination.

    logger.setLevel(logging.DEBUG)
    install_python_logger(logger)
