"""cffi bindings for libdatachannel's C API.

Low-level surface exposed to the rest of the package via :data:`lib` and
:data:`ffi`. Everything else in :mod:`aiolibdatachannel` treats these as the
only way to reach the native side; do not call raw ``ctypes``/``os`` on the
shared library elsewhere.

Only the PeerConnection + DataChannel portion of the API is declared
(WebSocket and media/RTP are disabled in our CMake build with
``NO_WEBSOCKET`` / ``NO_MEDIA``, so those symbols aren't in the .so).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from cffi import FFI

__all__ = ["CData", "ffi", "lib"]


# ---- Declarations --------------------------------------------------------
# Mirrors include/rtc/rtc.h, trimmed to the subset we actually use. When we
# bump the vendored libdatachannel, keep this file's enum values and struct
# layout in sync with include/rtc/rtc.h at the pinned tag.

_CDEF = """
// Opaque callback signatures. cffi represents function pointers generically;
// we give each one a typedef so registration + dispatch code reads cleanly.

typedef enum {
    RTC_NEW = 0,
    RTC_CONNECTING = 1,
    RTC_CONNECTED = 2,
    RTC_DISCONNECTED = 3,
    RTC_FAILED = 4,
    RTC_CLOSED = 5
} rtcState;

typedef enum {
    RTC_ICE_NEW = 0,
    RTC_ICE_CHECKING = 1,
    RTC_ICE_CONNECTED = 2,
    RTC_ICE_COMPLETED = 3,
    RTC_ICE_FAILED = 4,
    RTC_ICE_DISCONNECTED = 5,
    RTC_ICE_CLOSED = 6
} rtcIceState;

typedef enum {
    RTC_GATHERING_NEW = 0,
    RTC_GATHERING_INPROGRESS = 1,
    RTC_GATHERING_COMPLETE = 2
} rtcGatheringState;

typedef enum {
    RTC_SIGNALING_STABLE = 0,
    RTC_SIGNALING_HAVE_LOCAL_OFFER = 1,
    RTC_SIGNALING_HAVE_REMOTE_OFFER = 2,
    RTC_SIGNALING_HAVE_LOCAL_PRANSWER = 3,
    RTC_SIGNALING_HAVE_REMOTE_PRANSWER = 4
} rtcSignalingState;

typedef enum {
    RTC_LOG_NONE = 0,
    RTC_LOG_FATAL = 1,
    RTC_LOG_ERROR = 2,
    RTC_LOG_WARNING = 3,
    RTC_LOG_INFO = 4,
    RTC_LOG_DEBUG = 5,
    RTC_LOG_VERBOSE = 6
} rtcLogLevel;

typedef enum {
    RTC_CERTIFICATE_DEFAULT = 0,
    RTC_CERTIFICATE_ECDSA = 1,
    RTC_CERTIFICATE_RSA = 2
} rtcCertificateType;

typedef enum {
    RTC_TRANSPORT_POLICY_ALL = 0,
    RTC_TRANSPORT_POLICY_RELAY = 1
} rtcTransportPolicy;

typedef struct {
    const char **iceServers;
    int iceServersCount;
    const char *proxyServer;
    const char *bindAddress;
    rtcCertificateType certificateType;
    rtcTransportPolicy iceTransportPolicy;
    bool enableIceTcp;
    bool enableIceUdpMux;
    bool disableAutoNegotiation;
    bool forceMediaTransport;
    uint16_t portRangeBegin;
    uint16_t portRangeEnd;
    int mtu;
    int maxMessageSize;
} rtcConfiguration;

typedef struct {
    bool unordered;
    bool unreliable;
    unsigned int maxPacketLifeTime;
    unsigned int maxRetransmits;
} rtcReliability;

typedef struct {
    rtcReliability reliability;
    const char *protocol;
    bool negotiated;
    bool manualStream;
    uint16_t stream;
} rtcDataChannelInit;

// Callback function-pointer typedefs (CFFI syntax — omits calling convention
// attributes; we stay ABI-compatible on POSIX + default cdecl on Windows).
typedef void (*rtcLogCallbackFunc)(rtcLogLevel level, const char *message);
typedef void (*rtcDescriptionCallbackFunc)(int pc, const char *sdp, const char *type, void *ptr);
typedef void (*rtcCandidateCallbackFunc)(int pc, const char *cand, const char *mid, void *ptr);
typedef void (*rtcStateChangeCallbackFunc)(int pc, rtcState state, void *ptr);
typedef void (*rtcIceStateChangeCallbackFunc)(int pc, rtcIceState state, void *ptr);
typedef void (*rtcGatheringStateCallbackFunc)(int pc, rtcGatheringState state, void *ptr);
typedef void (*rtcSignalingStateCallbackFunc)(int pc, rtcSignalingState state, void *ptr);
typedef void (*rtcDataChannelCallbackFunc)(int pc, int dc, void *ptr);
typedef void (*rtcOpenCallbackFunc)(int id, void *ptr);
typedef void (*rtcClosedCallbackFunc)(int id, void *ptr);
typedef void (*rtcErrorCallbackFunc)(int id, const char *error, void *ptr);
typedef void (*rtcMessageCallbackFunc)(int id, const char *message, int size, void *ptr);
typedef void (*rtcBufferedAmountLowCallbackFunc)(int id, void *ptr);

// Logger / preload / cleanup.
void rtcInitLogger(rtcLogLevel level, rtcLogCallbackFunc cb);
void rtcPreload(void);
void rtcCleanup(void);
int rtcSetThreadPoolSize(unsigned int count);

// User pointer (we don't use it ourselves; the callbacks identify objects
// by their integer handle and a Python-side registry).
void rtcSetUserPointer(int id, void *ptr);
void *rtcGetUserPointer(int id);

// PeerConnection.
int rtcCreatePeerConnection(const rtcConfiguration *config);
int rtcClosePeerConnection(int pc);
int rtcDeletePeerConnection(int pc);

int rtcSetLocalDescriptionCallback(int pc, rtcDescriptionCallbackFunc cb);
int rtcSetLocalCandidateCallback(int pc, rtcCandidateCallbackFunc cb);
int rtcSetStateChangeCallback(int pc, rtcStateChangeCallbackFunc cb);
int rtcSetIceStateChangeCallback(int pc, rtcIceStateChangeCallbackFunc cb);
int rtcSetGatheringStateChangeCallback(int pc, rtcGatheringStateCallbackFunc cb);
int rtcSetSignalingStateChangeCallback(int pc, rtcSignalingStateCallbackFunc cb);
int rtcSetDataChannelCallback(int pc, rtcDataChannelCallbackFunc cb);

int rtcSetLocalDescription(int pc, const char *type);
int rtcSetRemoteDescription(int pc, const char *sdp, const char *type);
int rtcAddRemoteCandidate(int pc, const char *cand, const char *mid);

int rtcGetLocalDescription(int pc, char *buffer, int size);
int rtcGetRemoteDescription(int pc, char *buffer, int size);
int rtcGetLocalDescriptionType(int pc, char *buffer, int size);
int rtcGetRemoteDescriptionType(int pc, char *buffer, int size);

// DataChannel.
int rtcCreateDataChannel(int pc, const char *label);
int rtcCreateDataChannelEx(int pc, const char *label, const rtcDataChannelInit *init);
int rtcDeleteDataChannel(int dc);
int rtcGetDataChannelStream(int dc);
int rtcGetDataChannelLabel(int dc, char *buffer, int size);
int rtcGetDataChannelProtocol(int dc, char *buffer, int size);

// Channel common API (DataChannel).
int rtcSetOpenCallback(int id, rtcOpenCallbackFunc cb);
int rtcSetClosedCallback(int id, rtcClosedCallbackFunc cb);
int rtcSetErrorCallback(int id, rtcErrorCallbackFunc cb);
int rtcSetMessageCallback(int id, rtcMessageCallbackFunc cb);
int rtcSendMessage(int id, const char *data, int size);
int rtcClose(int id);
int rtcDelete(int id);
bool rtcIsOpen(int id);
bool rtcIsClosed(int id);
int rtcMaxMessageSize(int id);
int rtcGetBufferedAmount(int id);
int rtcSetBufferedAmountLowThreshold(int id, int amount);
int rtcSetBufferedAmountLowCallback(int id, rtcBufferedAmountLowCallbackFunc cb);
"""


# ---- Library loading ------------------------------------------------------


def _locate_library() -> Path:
    """Find the bundled libdatachannel shared library.

    Searches, in order:
    1. ``AIOLIBDATACHANNEL_LIB`` environment variable (absolute path).
    2. ``<package>/_lib/`` — the scikit-build install location.
    3. The build tree (for editable installs that haven't been installed
       into site-packages yet — scikit-build-core's editable mode triggers
       a rebuild before import so the file should already exist).
    """

    override = os.environ.get("AIOLIBDATACHANNEL_LIB")
    if override:
        p = Path(override)
        if p.exists():
            return p
        raise FileNotFoundError(f"AIOLIBDATACHANNEL_LIB points to a missing file: {p}")

    if sys.platform == "darwin":
        names = ("libdatachannel.dylib", "libdatachannel.0.dylib")
        pattern = "libdatachannel*.dylib"
    elif sys.platform == "win32":
        names = ("datachannel.dll", "libdatachannel.dll")
        pattern = "*datachannel*.dll"
    else:
        names = ("libdatachannel.so", "libdatachannel.so.0")
        pattern = "libdatachannel.so*"

    searched: list[str] = []
    pkg_dir = Path(__file__).parent

    # 1. Installed layout: <package>/_lib/<libname> (what scikit-build-core
    #    places in the built wheel via our CMakeLists.txt install target).
    for name in names:
        for p in (pkg_dir / "_lib" / name, pkg_dir / name):
            searched.append(str(p))
            if p.is_file():
                return p

    # 2. Editable / source-tree layout: search the CMake build tree.
    #    CMake's output layout varies by generator — Ninja puts the shared
    #    lib at the libdatachannel subdir root, multi-config generators
    #    (Visual Studio) add a Release/ / Debug/ subdir — so rglob the
    #    whole tree rather than guess every possibility.
    repo_root = pkg_dir.parent
    build_root = repo_root / "build"
    if build_root.is_dir():
        searched.append(f"{build_root} (glob: **/{pattern})")
        for hit in build_root.rglob(pattern):
            if hit.is_file():
                return hit

    raise FileNotFoundError(
        "libdatachannel shared library not found. Searched:\n  "
        + "\n  ".join(searched)
        + "\nSet AIOLIBDATACHANNEL_LIB to the absolute path of the .so/.dylib/.dll."
    )


ffi = FFI()
ffi.cdef(_CDEF)
lib = ffi.dlopen(str(_locate_library()))

# Type alias for cffi CData handles, exported for typing callers elsewhere.
CData = ffi.CData
