"""asyncio-friendly Python wrapper for libdatachannel.

See :class:`PeerConnection` for the primary entry point.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from . import _core
from .config import DataChannelOptions, RTCConfiguration
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
from .peer_connection import IceCandidate, LocalDescription, PeerConnection

try:
    __version__ = version("aiolibdatachannel")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = [
    "CertificateType",
    "ConnectionClosedError",
    "DataChannel",
    "DataChannelOptions",
    "GatheringState",
    "ICEState",
    "IceCandidate",
    "LocalDescription",
    "LogLevel",
    "PeerConnection",
    "RTCConfiguration",
    "RTCError",
    "RTCState",
    "SignalingState",
    "TransportPolicy",
    "__version__",
    "init_logger",
    "preload",
    "set_thread_pool_size",
]


def init_logger(level: LogLevel | int = LogLevel.WARNING, callback=None) -> None:  # type: ignore[no-untyped-def]
    """Initialise libdatachannel's logger.

    If ``callback`` is provided it is invoked with ``(level, message)`` for
    every log line; otherwise libdatachannel logs to stdout at the chosen
    level.
    """

    _core.init_logger(int(level), callback)


def preload() -> None:
    """Eagerly initialise libdatachannel's global state (optional)."""

    _core.preload()


def set_thread_pool_size(count: int) -> None:
    """Set libdatachannel's internal thread-pool size (applied to new threads)."""

    _core.set_thread_pool_size(count)
