"""Enum mirrors of libdatachannel's C state enums."""

from __future__ import annotations

from enum import IntEnum

__all__ = [
    "CertificateType",
    "GatheringState",
    "ICEState",
    "LogLevel",
    "RTCState",
    "SignalingState",
    "TransportPolicy",
]


class RTCState(IntEnum):
    NEW = 0
    CONNECTING = 1
    CONNECTED = 2
    DISCONNECTED = 3
    FAILED = 4
    CLOSED = 5


class ICEState(IntEnum):
    NEW = 0
    CHECKING = 1
    CONNECTED = 2
    COMPLETED = 3
    FAILED = 4
    DISCONNECTED = 5
    CLOSED = 6


class GatheringState(IntEnum):
    NEW = 0
    IN_PROGRESS = 1
    COMPLETE = 2


class SignalingState(IntEnum):
    STABLE = 0
    HAVE_LOCAL_OFFER = 1
    HAVE_REMOTE_OFFER = 2
    HAVE_LOCAL_PRANSWER = 3
    HAVE_REMOTE_PRANSWER = 4


class LogLevel(IntEnum):
    NONE = 0
    FATAL = 1
    ERROR = 2
    WARNING = 3
    INFO = 4
    DEBUG = 5
    VERBOSE = 6


class CertificateType(IntEnum):
    DEFAULT = 0
    ECDSA = 1
    RSA = 2


class TransportPolicy(IntEnum):
    ALL = 0
    RELAY = 1
