"""Object-oriented wrapper over :mod:`aiolibdatachannel._native`.

The asyncio shims in :mod:`.peer_connection` and :mod:`.data_channel`
import ``PeerConnection`` and ``DataChannel`` from here. Those higher
layers assume an imperative surface modelled on the libdatachannel
C API — this module provides exactly that.

Design — carried over from the previous cffi edition, because it's the
pattern that rules out the reference-cycle leaks that bit the first
nanobind port:

* ``_native`` holds **one** Python reference globally: the dispatcher
  registered in :func:`_install_dispatcher`. No per-handle Python state
  lives on the C++ side.
* Trampolines in the nanobind extension fan into that single dispatcher
  with a ``CallbackKind`` tag + handle + payload.
* This module keeps a ``handle → wrapper`` dict (strong refs). When a
  wrapper is destroyed it removes itself; an ``atexit`` hook sweeps up
  anything a caller forgot.
"""

from __future__ import annotations

import atexit
import contextlib
import threading
from collections.abc import Callable
from typing import Any

from . import _native  # type: ignore[attr-defined]  # compiled extension

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

ERR_SUCCESS: int = _native.ERR_SUCCESS
ERR_INVALID: int = _native.ERR_INVALID
ERR_FAILURE: int = _native.ERR_FAILURE
ERR_NOT_AVAIL: int = _native.ERR_NOT_AVAIL
ERR_TOO_SMALL: int = _native.ERR_TOO_SMALL


# ---- Registries ---------------------------------------------------------
#
# Flat dicts keyed by libdatachannel's integer handle. Strong refs — the
# wrapper removes itself on ``destroy()``; whatever's left when the
# interpreter exits is torn down by :func:`_atexit_destroy_all`.

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


# ---- Single-dispatcher trampoline ---------------------------------------
#
# Libdatachannel calls into _native's C trampolines, which call this
# function with ``(kind, handle, *payload)``. The dispatch is a flat
# ``if`` ladder keyed by the CallbackKind enum exported by _native.
#
# Anything raised here is converted to an unraisable exception so a
# misbehaving user callback doesn't derail libdatachannel's worker
# thread.

_CB_LOCAL_DESCRIPTION: int = _native.CB_LOCAL_DESCRIPTION
_CB_LOCAL_CANDIDATE: int = _native.CB_LOCAL_CANDIDATE
_CB_STATE_CHANGE: int = _native.CB_STATE_CHANGE
_CB_ICE_STATE_CHANGE: int = _native.CB_ICE_STATE_CHANGE
_CB_GATHERING_STATE_CHANGE: int = _native.CB_GATHERING_STATE_CHANGE
_CB_SIGNALING_STATE_CHANGE: int = _native.CB_SIGNALING_STATE_CHANGE
_CB_DATA_CHANNEL: int = _native.CB_DATA_CHANNEL
_CB_DC_OPEN: int = _native.CB_DC_OPEN
_CB_DC_CLOSED: int = _native.CB_DC_CLOSED
_CB_DC_ERROR: int = _native.CB_DC_ERROR
_CB_DC_MESSAGE: int = _native.CB_DC_MESSAGE
_CB_DC_BUFFERED_AMOUNT_LOW: int = _native.CB_DC_BUFFERED_AMOUNT_LOW
_CB_LOG: int = _native.CB_LOG


def _fire(cb: Callable[..., None] | None, *args: Any) -> None:
    if cb is None:
        return
    try:
        cb(*args)
    except Exception:  # pragma: no cover — best-effort dispatch
        import sys

        sys.excepthook(*sys.exc_info())


def _dispatch(kind: int, handle: int, *payload: Any) -> None:
    """Single entry point the C trampolines call into.

    Runs with the GIL held (acquired inside the trampoline). Looks up
    the owning wrapper, then fires the relevant stored callback.
    """
    if kind == _CB_LOCAL_DESCRIPTION:
        pc = _lookup_pc(handle)
        if pc is not None:
            _fire(pc._callbacks.get("local_description"), *payload)
    elif kind == _CB_LOCAL_CANDIDATE:
        pc = _lookup_pc(handle)
        if pc is not None:
            _fire(pc._callbacks.get("local_candidate"), *payload)
    elif kind == _CB_STATE_CHANGE:
        pc = _lookup_pc(handle)
        if pc is not None:
            _fire(pc._callbacks.get("state_change"), *payload)
    elif kind == _CB_ICE_STATE_CHANGE:
        pc = _lookup_pc(handle)
        if pc is not None:
            _fire(pc._callbacks.get("ice_state_change"), *payload)
    elif kind == _CB_GATHERING_STATE_CHANGE:
        pc = _lookup_pc(handle)
        if pc is not None:
            _fire(pc._callbacks.get("gathering_state_change"), *payload)
    elif kind == _CB_SIGNALING_STATE_CHANGE:
        pc = _lookup_pc(handle)
        if pc is not None:
            _fire(pc._callbacks.get("signaling_state_change"), *payload)
    elif kind == _CB_DATA_CHANNEL:
        pc = _lookup_pc(handle)
        if pc is not None:
            _fire(pc._callbacks.get("data_channel"), *payload)
    elif kind == _CB_DC_OPEN:
        dc = _lookup_dc(handle)
        if dc is not None:
            _fire(dc._callbacks.get("open"))
    elif kind == _CB_DC_CLOSED:
        dc = _lookup_dc(handle)
        if dc is not None:
            _fire(dc._callbacks.get("closed"))
    elif kind == _CB_DC_ERROR:
        dc = _lookup_dc(handle)
        if dc is not None:
            _fire(dc._callbacks.get("error"), *payload)
    elif kind == _CB_DC_MESSAGE:
        dc = _lookup_dc(handle)
        if dc is not None:
            # payload = (data: bytes|str, is_text: bool)
            data, _is_text = payload
            _fire(dc._callbacks.get("message"), data)
    elif kind == _CB_DC_BUFFERED_AMOUNT_LOW:
        dc = _lookup_dc(handle)
        if dc is not None:
            _fire(dc._callbacks.get("buffered_amount_low"))
    elif kind == _CB_LOG:
        # For logs the first positional is the level, not the handle.
        cb = _log_callback
        if cb is not None:
            level, message = handle, payload[0]
            try:
                cb(int(level), str(message))
            except Exception:  # pragma: no cover
                import sys

                sys.excepthook(*sys.exc_info())


# Install the dispatcher once at import time. The binding holds exactly
# one Python reference (this function), which the atexit hook clears.
_native.register_dispatcher(_dispatch)


# ---- PeerConnection -----------------------------------------------------


class PeerConnection:
    __slots__ = ("_callbacks", "_destroyed", "_handle")

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
        self._handle: int = _native.create_peer_connection(
            ice_servers=list(ice_servers),
            port_range_begin=port_range_begin,
            port_range_end=port_range_end,
            mtu=mtu,
            max_message_size=max_message_size,
            enable_ice_tcp=enable_ice_tcp,
            disable_auto_negotiation=disable_auto_negotiation,
            certificate_type=certificate_type,
            ice_transport_policy=ice_transport_policy,
        )
        self._callbacks: dict[str, Callable[..., None]] = {}
        self._destroyed = False
        with _pc_lock:
            _pcs[self._handle] = self

    @property
    def handle(self) -> int:
        return self._handle

    def __del__(self) -> None:  # pragma: no cover - shutdown path
        with contextlib.suppress(Exception):
            self.destroy()

    # ---- Callback registration ------------------------------------------

    def set_on_local_description(
        self,
        callback: Callable[[str, str], None],
    ) -> None:
        self._callbacks["local_description"] = callback
        _native.set_local_description_callback(self._handle, True)

    def set_on_local_candidate(
        self,
        callback: Callable[[str, str], None],
    ) -> None:
        self._callbacks["local_candidate"] = callback
        _native.set_local_candidate_callback(self._handle, True)

    def set_on_state_change(self, callback: Callable[[int], None]) -> None:
        self._callbacks["state_change"] = callback
        _native.set_state_change_callback(self._handle, True)

    def set_on_ice_state_change(self, callback: Callable[[int], None]) -> None:
        self._callbacks["ice_state_change"] = callback
        _native.set_ice_state_change_callback(self._handle, True)

    def set_on_gathering_state_change(
        self,
        callback: Callable[[int], None],
    ) -> None:
        self._callbacks["gathering_state_change"] = callback
        _native.set_gathering_state_change_callback(self._handle, True)

    def set_on_signaling_state_change(
        self,
        callback: Callable[[int], None],
    ) -> None:
        self._callbacks["signaling_state_change"] = callback
        _native.set_signaling_state_change_callback(self._handle, True)

    def set_on_data_channel(self, callback: Callable[[int], None]) -> None:
        self._callbacks["data_channel"] = callback
        _native.set_data_channel_callback(self._handle, True)

    # ---- SDP plumbing ---------------------------------------------------

    def set_local_description(self, type: str | None = None) -> None:
        _native.set_local_description(self._handle, type)

    def set_remote_description(self, sdp: str, type: str) -> None:
        _native.set_remote_description(self._handle, sdp, type)

    def add_remote_candidate(self, candidate: str, mid: str = "") -> None:
        _native.add_remote_candidate(
            self._handle,
            candidate,
            mid if mid else None,
        )

    def get_local_description(self) -> str | None:
        return _native.get_local_description(self._handle)

    def get_remote_description(self) -> str | None:
        return _native.get_remote_description(self._handle)

    def get_local_description_type(self) -> str | None:
        return _native.get_local_description_type(self._handle)

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
        return _native.create_data_channel(
            self._handle,
            label,
            unordered=unordered,
            unreliable=unreliable,
            max_packet_lifetime=max_packet_lifetime,
            max_retransmits=max_retransmits,
            protocol=protocol,
            negotiated=negotiated,
            manual_stream=manual_stream,
            stream=stream,
        )

    # ---- Lifecycle -----------------------------------------------------

    def close(self) -> None:
        if self._destroyed:
            return
        _native.close_peer_connection(self._handle)

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        handle = self._handle
        self._callbacks.clear()
        with _pc_lock:
            _pcs.pop(handle, None)
        # _native releases the GIL around rtcDeletePeerConnection, so
        # in-flight callbacks can acquire it and finish cleanly.
        _native.delete_peer_connection(handle)


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
        _native.set_dc_open_callback(self._handle, True)

    def set_on_closed(self, callback: Callable[[], None]) -> None:
        self._callbacks["closed"] = callback
        _native.set_dc_closed_callback(self._handle, True)

    def set_on_error(self, callback: Callable[[str], None]) -> None:
        self._callbacks["error"] = callback
        _native.set_dc_error_callback(self._handle, True)

    def set_on_message(self, callback: Callable[[bytes | str], None]) -> None:
        self._callbacks["message"] = callback
        _native.set_dc_message_callback(self._handle, True)

    def set_on_buffered_amount_low(self, callback: Callable[[], None]) -> None:
        self._callbacks["buffered_amount_low"] = callback
        _native.set_dc_buffered_amount_low_callback(self._handle, True)

    # ---- Messaging ------------------------------------------------------

    def send_bytes(self, data: bytes) -> None:
        _native.send_bytes(self._handle, data)

    def send_text(self, data: str) -> None:
        _native.send_text(self._handle, data)

    # ---- State ----------------------------------------------------------

    def is_open(self) -> bool:
        return bool(_native.is_open(self._handle))

    def is_closed(self) -> bool:
        return bool(_native.is_closed(self._handle))

    def buffered_amount(self) -> int:
        return int(_native.buffered_amount(self._handle))

    def max_message_size(self) -> int:
        return int(_native.max_message_size(self._handle))

    def set_buffered_amount_low_threshold(self, amount: int) -> None:
        _native.set_buffered_amount_low_threshold(self._handle, amount)

    def label(self) -> str | None:
        return _native.get_dc_label(self._handle)

    def protocol(self) -> str | None:
        return _native.get_dc_protocol(self._handle)

    def stream(self) -> int:
        return int(_native.get_dc_stream(self._handle))

    # ---- Lifecycle ------------------------------------------------------

    def close(self) -> None:
        if self._destroyed:
            return
        _native.close_dc(self._handle)

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        handle = self._handle
        self._callbacks.clear()
        with _dc_lock:
            _dcs.pop(handle, None)
        _native.delete_data_channel(handle)


# ---- Logger -------------------------------------------------------------

_log_callback: Callable[[int, str], None] | None = None


def init_logger(level: int, callback: Callable[[int, str], None] | None) -> None:
    global _log_callback
    _log_callback = callback
    _native.init_logger(level, callback is not None)


def preload() -> None:
    _native.preload()


def cleanup() -> None:
    _native.cleanup()


def set_thread_pool_size(count: int) -> int:
    return int(_native.set_thread_pool_size(count))


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
        _native.cleanup()
    # Drop the Python reference _native holds to our dispatcher so late
    # callbacks from C threads see a no-op instead of reaching into a
    # half-torn-down interpreter.
    with contextlib.suppress(Exception):
        _native.register_dispatcher(None)


atexit.register(_atexit_destroy_all)
