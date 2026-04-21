"""asyncio-friendly Python wrapper for libdatachannel.

See :class:`PeerConnection` for the primary entry point.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError, version

from . import _core
from .config import DataChannelOptions, IceServer, IceServerLike, RTCConfiguration
from .data_channel import DataChannel
from .enums import (
    CertificateType,
    GatheringState,
    ICEState,
    LogLevel,
    RTCState,
    SignalingState,
    TransportPolicy,
)
from .exceptions import ConnectionClosedError, RTCError
from .peer_connection import (
    DataChannelEvent,
    GatheringStateChangeEvent,
    IceCandidate,
    IceStateChangeEvent,
    LocalCandidateEvent,
    LocalDescription,
    LocalDescriptionEvent,
    PCEvent,
    PeerConnection,
    SdpType,
    SignalingStateChangeEvent,
    StateChangeEvent,
)

try:
    __version__ = version("aiolibdatachannel")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = [
    "CertificateType",
    "ConnectionClosedError",
    "DataChannel",
    "DataChannelEvent",
    "DataChannelOptions",
    "GatheringState",
    "GatheringStateChangeEvent",
    "ICEState",
    "IceCandidate",
    "IceServer",
    "IceServerLike",
    "IceStateChangeEvent",
    "LocalCandidateEvent",
    "LocalDescription",
    "LocalDescriptionEvent",
    "LogLevel",
    "PCEvent",
    "PeerConnection",
    "RTCConfiguration",
    "RTCError",
    "RTCState",
    "SdpType",
    "SignalingState",
    "SignalingStateChangeEvent",
    "StateChangeEvent",
    "TransportPolicy",
    "__version__",
    "init_logger",
    "install_python_logger",
    "preload",
    "set_thread_pool_size",
]


# rtcLogLevel → stdlib logging level. RTC_LOG_VERBOSE has no natural Python
# equivalent; we map it to DEBUG alongside RTC_LOG_DEBUG.
_RTC_TO_PY_LEVEL: dict[int, int] = {
    int(LogLevel.FATAL): logging.CRITICAL,
    int(LogLevel.ERROR): logging.ERROR,
    int(LogLevel.WARNING): logging.WARNING,
    int(LogLevel.INFO): logging.INFO,
    int(LogLevel.DEBUG): logging.DEBUG,
    int(LogLevel.VERBOSE): logging.DEBUG,
}


def _py_level_to_rtc(py_level: int) -> LogLevel:
    """Map a stdlib logging level to the coarsest rtcLogLevel that covers it."""
    if py_level <= logging.DEBUG:
        return LogLevel.DEBUG
    if py_level <= logging.INFO:
        return LogLevel.INFO
    if py_level <= logging.WARNING:
        return LogLevel.WARNING
    if py_level <= logging.ERROR:
        return LogLevel.ERROR
    return LogLevel.FATAL


def init_logger(
    level: LogLevel | int = LogLevel.WARNING,
    callback: Callable[[int, str], None] | None = None,
) -> None:
    """Initialise libdatachannel's logger.

    If ``callback`` is provided it is invoked with ``(rtc_level, message)``
    for every log line; otherwise libdatachannel logs to stdout at the
    chosen level. For most applications, prefer :func:`install_python_logger`
    which wires libdatachannel into the standard :mod:`logging` module.
    """

    _core.init_logger(int(level), callback)


def install_python_logger(
    logger: logging.Logger | str | None = None,
    *,
    level: LogLevel | int | None = None,
) -> logging.Logger:
    """Route libdatachannel's logs into Python's :mod:`logging` module.

    Each libdatachannel log line is forwarded as a record on the given
    logger (default: ``"aiolibdatachannel"``), with severity translated
    from rtcLogLevel to the corresponding stdlib level (FATAL→CRITICAL,
    ERROR→ERROR, WARNING→WARNING, INFO→INFO, DEBUG/VERBOSE→DEBUG).

    By default the filter level libdatachannel applies on its side is
    derived from the logger's effective level so we don't pay to format
    lines we'd immediately drop. Pass ``level`` to override.

    Returns the logger instance for convenience. Calling this more than
    once simply reinstalls the adapter with the new settings.
    """

    if isinstance(logger, str):
        log = logging.getLogger(logger)
    elif logger is None:
        log = logging.getLogger("aiolibdatachannel")
    else:
        log = logger

    if level is None:
        rtc_level: LogLevel = _py_level_to_rtc(log.getEffectiveLevel())
    elif isinstance(level, LogLevel):
        rtc_level = level
    else:
        rtc_level = LogLevel(int(level))

    def _adapter(rtc_lvl: int, message: str) -> None:
        py_lvl = _RTC_TO_PY_LEVEL.get(rtc_lvl, logging.INFO)
        # libdatachannel terminates lines with its own formatting; strip
        # the trailing newline so the logging formatter owns linebreaks.
        log.log(py_lvl, "%s", message.rstrip())

    _core.init_logger(int(rtc_level), _adapter)
    return log


def preload() -> None:
    """Eagerly initialise libdatachannel's global state (optional)."""

    _core.preload()


def set_thread_pool_size(count: int) -> None:
    """Set libdatachannel's internal thread-pool size (applied to new threads)."""

    _core.set_thread_pool_size(count)
