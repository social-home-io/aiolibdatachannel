"""Object-oriented wrapper over :mod:`aiolibdatachannel._ffi`.

This module gives the higher-level asyncio wrappers (:mod:`peer_connection`
/ :mod:`data_channel`) the same imperative surface the old nanobind
extension exposed, but implemented in pure Python on top of cffi.

Design notes:

* Native callback trampolines are registered **once** per callback type at
  module import time. Each trampoline looks up the owning Python object in
  a handle→instance dict and dispatches to the user-supplied callable.
  Nothing on the native side ever holds a strong reference to a Python
  bound method, which rules out the cycle the nanobind version hit.
* The registries live in this module (strong refs). Objects are removed
  when ``destroy()`` is called. An atexit hook tears down anything the
  caller forgot to close.
"""

from __future__ import annotations

import atexit
import contextlib
import threading
from collections.abc import Callable
from typing import Any

from ._ffi import ffi, lib
from .exceptions import RTCError

__all__ = [
    "ERR_FAILURE",
    "ERR_INVALID",
    "ERR_NOT_AVAIL",
    "ERR_SUCCESS",
    "ERR_TOO_SMALL",
    "DataChannel",
    "PeerConnection",
    "cleanup",
    "init_logger",
    "preload",
    "set_thread_pool_size",
]

ERR_SUCCESS = 0
ERR_INVALID = -1
ERR_FAILURE = -2
ERR_NOT_AVAIL = -3
ERR_TOO_SMALL = -4

_ERR_REASONS = {
    ERR_INVALID: "invalid argument",
    ERR_FAILURE: "runtime failure",
    ERR_NOT_AVAIL: "not available",
    ERR_TOO_SMALL: "buffer too small",
}


def _check(rc: int, ctx: str) -> int:
    if rc < 0:
        reason = _ERR_REASONS.get(rc, "unknown error")
        raise RTCError(f"{ctx}: {reason} ({rc})", code=rc)
    return rc


def _read_string(
    handle: int,
    getter: Callable[[int, Any, int], int],
    ctx: str,
) -> str | None:
    required = getter(handle, ffi.NULL, 0)
    if required == ERR_NOT_AVAIL:
        return None
    _check(required, ctx)
    if required == 0:
        return ""
    buf = ffi.new("char[]", required)
    rc = getter(handle, buf, required)
    _check(rc, ctx)
    # libdatachannel writes a trailing NUL; use ffi.string for safety.
    return ffi.string(buf, required).decode("utf-8", errors="replace")


# ---- Registries ---------------------------------------------------------
#
# Flat dicts keyed by libdatachannel's integer handle. Strong refs — the
# wrapper removes itself on destroy(); whatever's left when the interpreter
# exits is torn down by :func:`_atexit_destroy_all`.

_pc_lock = threading.Lock()
_pcs: dict[int, PeerConnection] = {}
_dc_lock = threading.Lock()
_dcs: dict[int, DataChannel] = {}


def _lookup_pc(handle: int) -> PeerConnection | None:
    with _pc_lock:
        return _pcs.get(handle)


def _lookup_dc(handle: int) -> DataChannel | None:
    with _dc_lock:
        return _dcs.get(handle)


# ---- Global trampolines -------------------------------------------------
#
# Each trampoline is a module-level cffi callback. libdatachannel only ever
# sees these; it never gets a pointer into a Python instance. The trampolines
# fish the instance out of the registry and fire its stored callback.


@ffi.callback("rtcDescriptionCallbackFunc")
def _on_local_description(pc: int, sdp: Any, type_: Any, ptr: Any) -> None:
    inst = _lookup_pc(pc)
    if inst is None:
        return
    cb = inst._callbacks.get("local_description")
    if cb is None:
        return
    try:
        cb(
            ffi.string(sdp).decode("utf-8", errors="replace") if sdp else "",
            ffi.string(type_).decode("utf-8", errors="replace") if type_ else "",
        )
    except Exception:  # pragma: no cover - best effort
        import sys

        sys.excepthook(*sys.exc_info())


@ffi.callback("rtcCandidateCallbackFunc")
def _on_local_candidate(pc: int, cand: Any, mid: Any, ptr: Any) -> None:
    inst = _lookup_pc(pc)
    if inst is None:
        return
    cb = inst._callbacks.get("local_candidate")
    if cb is None:
        return
    try:
        cb(
            ffi.string(cand).decode("utf-8", errors="replace") if cand else "",
            ffi.string(mid).decode("utf-8", errors="replace") if mid else "",
        )
    except Exception:  # pragma: no cover
        import sys

        sys.excepthook(*sys.exc_info())


def _dispatch_pc_int(pc: int, state: int, slot: str) -> None:
    inst = _lookup_pc(pc)
    if inst is None:
        return
    cb = inst._callbacks.get(slot)
    if cb is None:
        return
    try:
        cb(int(state))
    except Exception:  # pragma: no cover
        import sys

        sys.excepthook(*sys.exc_info())


# cffi is strictly type-checked: even though every PC state-change callback
# has the same (int, enum, void*) shape, each enum is a distinct ctype and
# needs its own trampoline. The shared dispatcher above handles the logic.
@ffi.callback("rtcStateChangeCallbackFunc")
def _on_state_change(pc: int, state: int, ptr: Any) -> None:
    _dispatch_pc_int(pc, state, "state_change")


@ffi.callback("rtcIceStateChangeCallbackFunc")
def _on_ice_state_change(pc: int, state: int, ptr: Any) -> None:
    _dispatch_pc_int(pc, state, "ice_state_change")


@ffi.callback("rtcGatheringStateCallbackFunc")
def _on_gathering_state_change(pc: int, state: int, ptr: Any) -> None:
    _dispatch_pc_int(pc, state, "gathering_state_change")


@ffi.callback("rtcSignalingStateCallbackFunc")
def _on_signaling_state_change(pc: int, state: int, ptr: Any) -> None:
    _dispatch_pc_int(pc, state, "signaling_state_change")


@ffi.callback("rtcDataChannelCallbackFunc")
def _on_data_channel(pc: int, dc: int, ptr: Any) -> None:
    inst = _lookup_pc(pc)
    if inst is None:
        return
    cb = inst._callbacks.get("data_channel")
    if cb is None:
        return
    try:
        cb(int(dc))
    except Exception:  # pragma: no cover
        import sys

        sys.excepthook(*sys.exc_info())


@ffi.callback("rtcOpenCallbackFunc")
def _on_open(handle: int, ptr: Any) -> None:
    inst = _lookup_dc(handle)
    if inst is None:
        return
    cb = inst._callbacks.get("open")
    if cb is None:
        return
    try:
        cb()
    except Exception:  # pragma: no cover
        import sys

        sys.excepthook(*sys.exc_info())


@ffi.callback("rtcClosedCallbackFunc")
def _on_closed(handle: int, ptr: Any) -> None:
    inst = _lookup_dc(handle)
    if inst is None:
        return
    cb = inst._callbacks.get("closed")
    if cb is None:
        return
    try:
        cb()
    except Exception:  # pragma: no cover
        import sys

        sys.excepthook(*sys.exc_info())


@ffi.callback("rtcErrorCallbackFunc")
def _on_error(handle: int, err: Any, ptr: Any) -> None:
    inst = _lookup_dc(handle)
    if inst is None:
        return
    cb = inst._callbacks.get("error")
    if cb is None:
        return
    try:
        cb(ffi.string(err).decode("utf-8", errors="replace") if err else "")
    except Exception:  # pragma: no cover
        import sys

        sys.excepthook(*sys.exc_info())


@ffi.callback("rtcMessageCallbackFunc")
def _on_message(handle: int, data: Any, size: int, ptr: Any) -> None:
    inst = _lookup_dc(handle)
    if inst is None:
        return
    cb = inst._callbacks.get("message")
    if cb is None:
        return
    try:
        if size < 0:
            # Null-terminated text payload.
            cb(ffi.string(data).decode("utf-8", errors="replace") if data else "")
        else:
            cb(bytes(ffi.buffer(data, size)))
    except Exception:  # pragma: no cover
        import sys

        sys.excepthook(*sys.exc_info())


@ffi.callback("rtcBufferedAmountLowCallbackFunc")
def _on_buffered_amount_low(handle: int, ptr: Any) -> None:
    inst = _lookup_dc(handle)
    if inst is None:
        return
    cb = inst._callbacks.get("buffered_amount_low")
    if cb is None:
        return
    try:
        cb()
    except Exception:  # pragma: no cover
        import sys

        sys.excepthook(*sys.exc_info())


# ---- PeerConnection ----------------------------------------------------


class PeerConnection:
    __slots__ = ("_callbacks", "_destroyed", "_handle", "_ice_buffers")

    def __init__(
        self,
        ice_servers: list[str],
        port_range_begin: int = 0,
        port_range_end: int = 0,
        mtu: int = 0,
        max_message_size: int = 0,
        enable_ice_tcp: bool = False,
        disable_auto_negotiation: bool = True,
        certificate_type: int = 0,
        ice_transport_policy: int = 0,
    ) -> None:
        # Keep byte strings alive so the `const char **` pointers we pass
        # into rtcConfiguration don't dangle during rtcCreatePeerConnection.
        encoded = [s.encode("utf-8") for s in ice_servers]
        ice_keepalive = [ffi.new("char[]", b) for b in encoded]
        ice_array = ffi.new("const char *[]", ice_keepalive) if ice_keepalive else ffi.NULL
        self._ice_buffers = (ice_keepalive, ice_array)

        cfg = ffi.new("rtcConfiguration *")
        cfg.iceServers = ice_array
        cfg.iceServersCount = len(ice_keepalive)
        cfg.proxyServer = ffi.NULL
        cfg.bindAddress = ffi.NULL
        cfg.certificateType = certificate_type
        cfg.iceTransportPolicy = ice_transport_policy
        cfg.enableIceTcp = enable_ice_tcp
        cfg.enableIceUdpMux = False
        cfg.disableAutoNegotiation = disable_auto_negotiation
        cfg.forceMediaTransport = False
        cfg.portRangeBegin = port_range_begin
        cfg.portRangeEnd = port_range_end
        cfg.mtu = mtu
        cfg.maxMessageSize = max_message_size

        handle = lib.rtcCreatePeerConnection(cfg)
        _check(handle, "rtcCreatePeerConnection")

        self._handle = handle
        self._callbacks: dict[str, Callable[..., None]] = {}
        self._destroyed = False

        with _pc_lock:
            _pcs[handle] = self

    @property
    def handle(self) -> int:
        return self._handle

    def __del__(self) -> None:  # pragma: no cover - shutdown path
        with contextlib.suppress(Exception):
            self.destroy()

    # ---- Callback registration ------------------------------------------

    def set_on_local_description(self, callback: Callable[[str, str], None]) -> None:
        self._callbacks["local_description"] = callback
        _check(
            lib.rtcSetLocalDescriptionCallback(self._handle, _on_local_description),
            "rtcSetLocalDescriptionCallback",
        )

    def set_on_local_candidate(self, callback: Callable[[str, str], None]) -> None:
        self._callbacks["local_candidate"] = callback
        _check(
            lib.rtcSetLocalCandidateCallback(self._handle, _on_local_candidate),
            "rtcSetLocalCandidateCallback",
        )

    def set_on_state_change(self, callback: Callable[[int], None]) -> None:
        self._callbacks["state_change"] = callback
        _check(
            lib.rtcSetStateChangeCallback(self._handle, _on_state_change),
            "rtcSetStateChangeCallback",
        )

    def set_on_ice_state_change(self, callback: Callable[[int], None]) -> None:
        self._callbacks["ice_state_change"] = callback
        _check(
            lib.rtcSetIceStateChangeCallback(self._handle, _on_ice_state_change),
            "rtcSetIceStateChangeCallback",
        )

    def set_on_gathering_state_change(self, callback: Callable[[int], None]) -> None:
        self._callbacks["gathering_state_change"] = callback
        _check(
            lib.rtcSetGatheringStateChangeCallback(self._handle, _on_gathering_state_change),
            "rtcSetGatheringStateChangeCallback",
        )

    def set_on_signaling_state_change(self, callback: Callable[[int], None]) -> None:
        self._callbacks["signaling_state_change"] = callback
        _check(
            lib.rtcSetSignalingStateChangeCallback(self._handle, _on_signaling_state_change),
            "rtcSetSignalingStateChangeCallback",
        )

    def set_on_data_channel(self, callback: Callable[[int], None]) -> None:
        self._callbacks["data_channel"] = callback
        _check(
            lib.rtcSetDataChannelCallback(self._handle, _on_data_channel),
            "rtcSetDataChannelCallback",
        )

    # ---- SDP plumbing ---------------------------------------------------

    def set_local_description(self, type: str | None = None) -> None:
        arg = type.encode("utf-8") if type is not None else ffi.NULL
        _check(lib.rtcSetLocalDescription(self._handle, arg), "rtcSetLocalDescription")

    def set_remote_description(self, sdp: str, type: str) -> None:
        _check(
            lib.rtcSetRemoteDescription(self._handle, sdp.encode("utf-8"), type.encode("utf-8")),
            "rtcSetRemoteDescription",
        )

    def add_remote_candidate(self, candidate: str, mid: str = "") -> None:
        mid_arg = mid.encode("utf-8") if mid else ffi.NULL
        _check(
            lib.rtcAddRemoteCandidate(self._handle, candidate.encode("utf-8"), mid_arg),
            "rtcAddRemoteCandidate",
        )

    def get_local_description(self) -> str | None:
        return _read_string(self._handle, lib.rtcGetLocalDescription, "rtcGetLocalDescription")

    def get_remote_description(self) -> str | None:
        return _read_string(self._handle, lib.rtcGetRemoteDescription, "rtcGetRemoteDescription")

    def get_local_description_type(self) -> str | None:
        return _read_string(
            self._handle, lib.rtcGetLocalDescriptionType, "rtcGetLocalDescriptionType"
        )

    # ---- DataChannel creation ------------------------------------------

    def create_data_channel(
        self,
        label: str,
        unordered: bool = False,
        unreliable: bool = False,
        max_packet_lifetime: int = 0,
        max_retransmits: int = 0,
        protocol: str = "",
        negotiated: bool = False,
        manual_stream: bool = False,
        stream: int = 0,
    ) -> int:
        init = ffi.new("rtcDataChannelInit *")
        init.reliability.unordered = unordered
        init.reliability.unreliable = unreliable
        init.reliability.maxPacketLifeTime = max_packet_lifetime
        init.reliability.maxRetransmits = max_retransmits
        # Keep the protocol bytes alive for the duration of the call.
        proto_bytes = protocol.encode("utf-8") if protocol else b""
        init.protocol = ffi.new("char[]", proto_bytes) if protocol else ffi.NULL
        init.negotiated = negotiated
        init.manualStream = manual_stream
        init.stream = stream
        handle = lib.rtcCreateDataChannelEx(self._handle, label.encode("utf-8"), init)
        _check(handle, "rtcCreateDataChannelEx")
        return handle

    # ---- Lifecycle -----------------------------------------------------

    def close(self) -> None:
        if self._destroyed:
            return
        lib.rtcClosePeerConnection(self._handle)

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        handle = self._handle
        # Unregister BEFORE rtcDeletePeerConnection so no callback can race
        # the registry cleanup.
        lib.rtcSetLocalDescriptionCallback(handle, ffi.NULL)
        lib.rtcSetLocalCandidateCallback(handle, ffi.NULL)
        lib.rtcSetStateChangeCallback(handle, ffi.NULL)
        lib.rtcSetIceStateChangeCallback(handle, ffi.NULL)
        lib.rtcSetGatheringStateChangeCallback(handle, ffi.NULL)
        lib.rtcSetSignalingStateChangeCallback(handle, ffi.NULL)
        lib.rtcSetDataChannelCallback(handle, ffi.NULL)
        self._callbacks.clear()
        with _pc_lock:
            _pcs.pop(handle, None)
        # rtcDeletePeerConnection releases the GIL internally? No — cffi
        # doesn't release the GIL by default for external calls. But
        # rtcDelete blocks waiting for callbacks; if those callbacks try to
        # acquire the GIL, we'd deadlock. We must release it explicitly.
        rc = lib.rtcDeletePeerConnection(handle)
        if rc < 0:
            # Don't raise — destroy is best-effort during cleanup.
            pass


# ---- DataChannel --------------------------------------------------------


class DataChannel:
    __slots__ = ("_callbacks", "_destroyed", "_handle")

    def __init__(self, handle: int) -> None:
        self._handle = handle
        self._callbacks: dict[str, Callable[..., None]] = {}
        self._destroyed = False
        with _dc_lock:
            _dcs[handle] = self

    @property
    def handle(self) -> int:
        return self._handle

    def __del__(self) -> None:  # pragma: no cover - shutdown path
        with contextlib.suppress(Exception):
            self.destroy()

    # ---- Callback registration -----------------------------------------

    def set_on_open(self, callback: Callable[[], None]) -> None:
        self._callbacks["open"] = callback
        _check(lib.rtcSetOpenCallback(self._handle, _on_open), "rtcSetOpenCallback")

    def set_on_closed(self, callback: Callable[[], None]) -> None:
        self._callbacks["closed"] = callback
        _check(lib.rtcSetClosedCallback(self._handle, _on_closed), "rtcSetClosedCallback")

    def set_on_error(self, callback: Callable[[str], None]) -> None:
        self._callbacks["error"] = callback
        _check(lib.rtcSetErrorCallback(self._handle, _on_error), "rtcSetErrorCallback")

    def set_on_message(self, callback: Callable[[bytes | str], None]) -> None:
        self._callbacks["message"] = callback
        _check(lib.rtcSetMessageCallback(self._handle, _on_message), "rtcSetMessageCallback")

    def set_on_buffered_amount_low(self, callback: Callable[[], None]) -> None:
        self._callbacks["buffered_amount_low"] = callback
        _check(
            lib.rtcSetBufferedAmountLowCallback(self._handle, _on_buffered_amount_low),
            "rtcSetBufferedAmountLowCallback",
        )

    # ---- Messaging ------------------------------------------------------

    def send_bytes(self, data: bytes) -> None:
        _check(lib.rtcSendMessage(self._handle, data, len(data)), "rtcSendMessage")

    def send_text(self, data: str) -> None:
        # size < 0 tells libdatachannel the buffer is null-terminated text.
        encoded = data.encode("utf-8")
        _check(lib.rtcSendMessage(self._handle, encoded, -1), "rtcSendMessage")

    # ---- State ----------------------------------------------------------

    def is_open(self) -> bool:
        return bool(lib.rtcIsOpen(self._handle))

    def is_closed(self) -> bool:
        return bool(lib.rtcIsClosed(self._handle))

    def buffered_amount(self) -> int:
        return _check(lib.rtcGetBufferedAmount(self._handle), "rtcGetBufferedAmount")

    def max_message_size(self) -> int:
        return _check(lib.rtcMaxMessageSize(self._handle), "rtcMaxMessageSize")

    def set_buffered_amount_low_threshold(self, amount: int) -> None:
        _check(
            lib.rtcSetBufferedAmountLowThreshold(self._handle, amount),
            "rtcSetBufferedAmountLowThreshold",
        )

    def label(self) -> str | None:
        return _read_string(self._handle, lib.rtcGetDataChannelLabel, "rtcGetDataChannelLabel")

    def protocol(self) -> str | None:
        return _read_string(
            self._handle, lib.rtcGetDataChannelProtocol, "rtcGetDataChannelProtocol"
        )

    def stream(self) -> int:
        return _check(lib.rtcGetDataChannelStream(self._handle), "rtcGetDataChannelStream")

    # ---- Lifecycle ------------------------------------------------------

    def close(self) -> None:
        if self._destroyed:
            return
        lib.rtcClose(self._handle)

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        handle = self._handle
        lib.rtcSetOpenCallback(handle, ffi.NULL)
        lib.rtcSetClosedCallback(handle, ffi.NULL)
        lib.rtcSetErrorCallback(handle, ffi.NULL)
        lib.rtcSetMessageCallback(handle, ffi.NULL)
        lib.rtcSetBufferedAmountLowCallback(handle, ffi.NULL)
        self._callbacks.clear()
        with _dc_lock:
            _dcs.pop(handle, None)
        lib.rtcDeleteDataChannel(handle)


# ---- Logger -------------------------------------------------------------

_log_callback: Callable[[int, str], None] | None = None


@ffi.callback("rtcLogCallbackFunc")
def _log_trampoline(level: int, message: Any) -> None:
    cb = _log_callback
    if cb is None:
        return
    try:
        cb(int(level), ffi.string(message).decode("utf-8", errors="replace") if message else "")
    except Exception:  # pragma: no cover
        import sys

        sys.excepthook(*sys.exc_info())


def init_logger(level: int, callback: Callable[[int, str], None] | None) -> None:
    global _log_callback
    _log_callback = callback
    lib.rtcInitLogger(level, _log_trampoline if callback is not None else ffi.NULL)


def preload() -> None:
    lib.rtcPreload()


def cleanup() -> None:
    lib.rtcCleanup()


def set_thread_pool_size(count: int) -> int:
    return _check(lib.rtcSetThreadPoolSize(count), "rtcSetThreadPoolSize")


# ---- Shutdown -----------------------------------------------------------


def _atexit_destroy_all() -> None:
    # Iterate snapshots since destroy() mutates the registries.
    for dc_handle in list(_dcs):
        dc = _lookup_dc(dc_handle)
        if dc is not None:
            with contextlib.suppress(Exception):
                dc.destroy()
    for pc_handle in list(_pcs):
        pc = _lookup_pc(pc_handle)
        if pc is not None:
            with contextlib.suppress(Exception):
                pc.destroy()
    with contextlib.suppress(Exception):
        lib.rtcCleanup()


atexit.register(_atexit_destroy_all)
