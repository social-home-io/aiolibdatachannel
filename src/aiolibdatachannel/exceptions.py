"""Exceptions raised by :mod:`aiolibdatachannel`."""

from __future__ import annotations

__all__ = ["ConnectionClosedError", "RTCError"]


class RTCError(RuntimeError):
    """Error surfaced from the underlying libdatachannel C API."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code

    def __str__(self) -> str:  # pragma: no cover - trivial
        base = super().__str__()
        if self.code is not None:
            return f"{base} [code={self.code}]"
        return base


class ConnectionClosedError(RTCError):
    """Raised when an operation is attempted on a closed connection."""
