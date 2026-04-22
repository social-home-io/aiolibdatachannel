"""Pure-Python stand-in for the compiled ``aiolibdatachannel._native``
nanobind extension.

Purpose
-------
Lets the Python wrapper tests (latched futures, async iterators,
cancellation, error propagation) run without a C++ toolchain or a built
libdatachannel. The shape matches the real extension function-by-function
so ``aiolibdatachannel._core`` can import this module unchanged.

Non-goals
---------
* Speaks nothing on the network. No SDP parsing, no DTLS, no ICE — every
  "connection" is a dict of state. Tests that need a real handshake use
  the ``@pytest.mark.native`` marker and run against the built extension.
* Doesn't model libdatachannel's thread-pool. Callbacks fire synchronously
  from whatever thread set them (the asyncio loop in tests), which is
  what the wrapper layer is designed to handle via ``schedule()``.

Scripting hooks
---------------
Tests can programmatically drive the fake between peer-connection and
data-channel lifecycle events:

    from tests import _fake_native as native
    handle = native.create_peer_connection([])
    native.emit(native.CB_STATE_CHANGE, handle, 2)  # CONNECTED

Error injection:

    native.inject_error("rtcCreateDataChannelEx", "boom")
    # next call raises RTCError("…: boom (…)")
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from typing import Any

# ---- Public enum values (mirror bindings.cpp's CallbackKind) ---------

ERR_SUCCESS: int = 0
ERR_INVALID: int = -1
ERR_FAILURE: int = -2
ERR_NOT_AVAIL: int = -3
ERR_TOO_SMALL: int = -4

CB_LOCAL_DESCRIPTION: int = 0
CB_LOCAL_CANDIDATE: int = 1
CB_STATE_CHANGE: int = 2
CB_ICE_STATE_CHANGE: int = 3
CB_GATHERING_STATE_CHANGE: int = 4
CB_SIGNALING_STATE_CHANGE: int = 5
CB_DATA_CHANNEL: int = 6
CB_DC_OPEN: int = 7
CB_DC_CLOSED: int = 8
CB_DC_ERROR: int = 9
CB_DC_MESSAGE: int = 10
CB_DC_BUFFERED_AMOUNT_LOW: int = 11
CB_LOG: int = 12


# ---- Internal state ---------------------------------------------------

_state_lock = threading.Lock()
_dispatcher: Callable[..., None] | None = None

# Per-handle dicts keyed by integer handle.
_pcs: dict[int, dict[str, Any]] = {}
_dcs: dict[int, dict[str, Any]] = {}

_next_handle = itertools.count(start=1)

_errors: dict[str, tuple[str, int]] = {}


class RTCError(Exception):
    """Local copy — must match aiolibdatachannel.exceptions.RTCError's
    constructor signature so _core.py's ``check()`` logic can see them
    as the same type when running against the real extension."""

    def __init__(self, message: str, code: int = ERR_FAILURE):
        super().__init__(message)
        self.code = code


def _raise_if_injected(ctx: str) -> None:
    try:
        msg, code = _errors.pop(ctx)
    except KeyError:
        return
    from aiolibdatachannel.exceptions import RTCError as _Real  # lazy import

    raise _Real(f"{ctx}: {msg} ({code})", code=code)


def inject_error(ctx: str, message: str = "injected", code: int = ERR_FAILURE) -> None:
    """Queue an error for the next call to ``ctx`` (one-shot)."""
    _errors[ctx] = (message, code)


def emit(kind: int, handle: int, *payload: Any) -> None:
    """Fire the dispatcher synchronously with ``(kind, handle, *payload)``.

    Tests use this to simulate libdatachannel callbacks — e.g. pushing
    a state change, delivering a DataChannel to the answerer, or
    injecting an inbound message.
    """
    with _state_lock:
        fn = _dispatcher
    if fn is None:
        return
    fn(kind, handle, *payload)


# ---- Module-level API matching _native.so -----------------------------


def register_dispatcher(fn: Callable[..., None] | None) -> None:
    global _dispatcher
    with _state_lock:
        _dispatcher = fn


# Peer-connection lifecycle


def create_peer_connection(
    ice_servers: list[str],
    *,
    port_range_begin: int = 0,
    port_range_end: int = 0,
    mtu: int = 0,
    max_message_size: int = 0,
    enable_ice_tcp: bool = False,
    disable_auto_negotiation: bool = True,
    certificate_type: int = 0,
    ice_transport_policy: int = 0,
) -> int:
    _raise_if_injected("rtcCreatePeerConnection")
    handle = next(_next_handle)
    _pcs[handle] = {
        "ice_servers": list(ice_servers),
        "local_sdp": None,
        "local_type": None,
        "remote_sdp": None,
        "remote_type": None,
        "enabled_callbacks": set(),
    }
    return handle


def close_peer_connection(pc: int) -> None:
    info = _pcs.get(pc)
    if info is None:
        return
    info["closed"] = True


def delete_peer_connection(pc: int) -> None:
    _pcs.pop(pc, None)


# Callback enable/disable — the real _native wires the C callback pointer,
# but we only need to remember which kinds the wrapper enabled so tests
# can branch on it if desired.


def _set_cb_flag(repo: dict[int, dict[str, Any]], handle: int, key: str, enable: bool) -> None:
    info = repo.get(handle)
    if info is None:
        return
    flags: set[str] = info.setdefault("enabled_callbacks", set())
    if enable:
        flags.add(key)
    else:
        flags.discard(key)


def set_local_description_callback(pc: int, enable: bool) -> None:
    _set_cb_flag(_pcs, pc, "local_description", enable)


def set_local_candidate_callback(pc: int, enable: bool) -> None:
    _set_cb_flag(_pcs, pc, "local_candidate", enable)


def set_state_change_callback(pc: int, enable: bool) -> None:
    _set_cb_flag(_pcs, pc, "state_change", enable)


def set_ice_state_change_callback(pc: int, enable: bool) -> None:
    _set_cb_flag(_pcs, pc, "ice_state_change", enable)


def set_gathering_state_change_callback(pc: int, enable: bool) -> None:
    _set_cb_flag(_pcs, pc, "gathering_state_change", enable)


def set_signaling_state_change_callback(pc: int, enable: bool) -> None:
    _set_cb_flag(_pcs, pc, "signaling_state_change", enable)


def set_data_channel_callback(pc: int, enable: bool) -> None:
    _set_cb_flag(_pcs, pc, "data_channel", enable)


# SDP


def set_local_description(pc: int, type: str | None = None) -> None:
    _raise_if_injected("rtcSetLocalDescription")
    info = _pcs.get(pc)
    if info is None:
        raise RTCError("rtcSetLocalDescription: not available", ERR_NOT_AVAIL)
    # Default to "offer" for tests that don't specify.
    t = type or "offer"
    stub_sdp = f"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\na={t}\r\n"
    info["local_sdp"] = stub_sdp
    info["local_type"] = t
    # Synthesise the local-description callback so the Python wrapper's
    # latched future resolves.
    emit(CB_LOCAL_DESCRIPTION, pc, stub_sdp, t)


def set_remote_description(pc: int, sdp: str, type: str) -> None:
    _raise_if_injected("rtcSetRemoteDescription")
    info = _pcs.get(pc)
    if info is None:
        raise RTCError("rtcSetRemoteDescription: not available", ERR_NOT_AVAIL)
    info["remote_sdp"] = sdp
    info["remote_type"] = type


def add_remote_candidate(pc: int, candidate: str, mid: str | None = None) -> None:
    _raise_if_injected("rtcAddRemoteCandidate")
    info = _pcs.get(pc)
    if info is None:
        raise RTCError("rtcAddRemoteCandidate: not available", ERR_NOT_AVAIL)
    info.setdefault("remote_candidates", []).append((candidate, mid or ""))


def get_local_description(pc: int) -> str | None:
    info = _pcs.get(pc)
    return info["local_sdp"] if info else None


def get_remote_description(pc: int) -> str | None:
    info = _pcs.get(pc)
    return info["remote_sdp"] if info else None


def get_local_description_type(pc: int) -> str | None:
    info = _pcs.get(pc)
    return info["local_type"] if info else None


# DataChannel lifecycle


def create_data_channel(
    pc: int,
    label: str,
    *,
    unordered: bool = False,
    unreliable: bool = False,
    max_packet_lifetime: int = 0,
    max_retransmits: int = 0,
    protocol: str = "",
    negotiated: bool = False,
    manual_stream: bool = False,
    stream: int = 0,
) -> int:
    _raise_if_injected("rtcCreateDataChannelEx")
    if pc not in _pcs:
        raise RTCError("rtcCreateDataChannelEx: invalid argument", ERR_INVALID)
    handle = next(_next_handle)
    _dcs[handle] = {
        "pc": pc,
        "label": label,
        "protocol": protocol,
        "stream": stream,
        "open": False,
        "closed": False,
        "buffered_amount": 0,
        "messages_sent": [],
        "enabled_callbacks": set(),
    }
    return handle


def delete_data_channel(dc: int) -> None:
    _dcs.pop(dc, None)


def set_dc_open_callback(dc: int, enable: bool) -> None:
    _set_cb_flag(_dcs, dc, "open", enable)


def set_dc_closed_callback(dc: int, enable: bool) -> None:
    _set_cb_flag(_dcs, dc, "closed", enable)


def set_dc_error_callback(dc: int, enable: bool) -> None:
    _set_cb_flag(_dcs, dc, "error", enable)


def set_dc_message_callback(dc: int, enable: bool) -> None:
    _set_cb_flag(_dcs, dc, "message", enable)


def set_dc_buffered_amount_low_callback(dc: int, enable: bool) -> None:
    _set_cb_flag(_dcs, dc, "buffered_amount_low", enable)


# I/O + state


def send_bytes(dc: int, data: bytes) -> None:
    _raise_if_injected("rtcSendMessage")
    info = _dcs.get(dc)
    if info is None:
        raise RTCError("rtcSendMessage: not available", ERR_NOT_AVAIL)
    info["messages_sent"].append(bytes(data))


def send_text(dc: int, text: str) -> None:
    _raise_if_injected("rtcSendMessage")
    info = _dcs.get(dc)
    if info is None:
        raise RTCError("rtcSendMessage: not available", ERR_NOT_AVAIL)
    info["messages_sent"].append(text)


def is_open(dc: int) -> bool:
    info = _dcs.get(dc)
    return bool(info and info.get("open") and not info.get("closed"))


def is_closed(dc: int) -> bool:
    info = _dcs.get(dc)
    return bool(info and info.get("closed"))


def buffered_amount(dc: int) -> int:
    info = _dcs.get(dc)
    return int(info["buffered_amount"]) if info else 0


def max_message_size(dc: int) -> int:
    return 262144  # matches libdatachannel's SCTP default


def set_buffered_amount_low_threshold(dc: int, amount: int) -> None:
    info = _dcs.get(dc)
    if info is None:
        raise RTCError(
            "rtcSetBufferedAmountLowThreshold: invalid argument",
            ERR_INVALID,
        )
    info["buffered_amount_low_threshold"] = amount


def get_dc_label(dc: int) -> str | None:
    info = _dcs.get(dc)
    return info["label"] if info else None


def get_dc_protocol(dc: int) -> str | None:
    info = _dcs.get(dc)
    return info.get("protocol") or None if info else None


def get_dc_stream(dc: int) -> int:
    info = _dcs.get(dc)
    return int(info.get("stream", 0)) if info else 0


def close_dc(dc: int) -> None:
    info = _dcs.get(dc)
    if info is None:
        return
    info["open"] = False
    info["closed"] = True
    emit(CB_DC_CLOSED, dc)


# Logger + global lifecycle


def init_logger(level: int, enable: bool) -> None:
    _pcs.setdefault(-1, {})["log_level"] = level
    _pcs.setdefault(-1, {})["log_enabled"] = enable


def preload() -> None:
    return None


def cleanup() -> None:
    _pcs.clear()
    _dcs.clear()
    _errors.clear()


def set_thread_pool_size(count: int) -> int:
    _pcs.setdefault(-1, {})["thread_pool_size"] = count
    return count


# ---- Introspection helpers for tests (not on the real extension) ----


def reset() -> None:
    """Clear all state. Call from a fixture per-test if you want
    isolation."""
    global _dispatcher
    with _state_lock:
        _dispatcher = None
    _pcs.clear()
    _dcs.clear()
    _errors.clear()


def pc_info(pc: int) -> dict[str, Any] | None:
    return _pcs.get(pc)


def dc_info(dc: int) -> dict[str, Any] | None:
    return _dcs.get(dc)
