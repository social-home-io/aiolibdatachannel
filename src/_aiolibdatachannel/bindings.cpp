// SPDX-License-Identifier: MPL-2.0
//
// nanobind thin binding over libdatachannel's C API (include/rtc/rtc.h).
//
// Design — readers, please keep this in mind before reaching for classes:
//
//   * We expose FUNCTIONS only. Each function mirrors one libdatachannel
//     ``rtc*`` entry point. The asyncio shim in aiolibdatachannel._core
//     wraps these into object-oriented wrappers; the binding stays flat.
//
//   * The binding NEVER holds a Python reference to a user-supplied
//     callback. Instead, a SINGLE module-level Python dispatcher is
//     registered once via :func:`register_dispatcher`. Libdatachannel
//     sees our static C trampolines; they acquire the GIL and call the
//     dispatcher with ``(kind, handle, *payload)``. This is the same
//     handle-lookup pattern the earlier cffi version used to avoid the
//     per-handle reference cycles that bit the first nanobind port.
//
//   * Every blocking rtc* call releases the GIL around the libdatachannel
//     entry point. The dispatcher always re-acquires inside the trampoline.
//     This precludes the ``test_cancel_mid_negotiation`` deadlock the
//     earlier nanobind port hit: if a worker thread is mid-callback when
//     rtcDeletePeerConnection is called from the main thread, the worker
//     can acquire the GIL (main thread released it), finish the callback,
//     then libdatachannel's internal join unblocks and we return.

#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>

#include <cstdint>
#include <cstring>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include <rtc/rtc.h>

namespace nb = nanobind;

namespace {

// ---- Error translation ---------------------------------------------------

[[noreturn]] void raise_rtc_error(int code, const char *ctx) {
    const char *reason;
    switch (code) {
        case RTC_ERR_INVALID: reason = "invalid argument"; break;
        case RTC_ERR_FAILURE: reason = "runtime failure"; break;
        case RTC_ERR_NOT_AVAIL: reason = "not available"; break;
        case RTC_ERR_TOO_SMALL: reason = "buffer too small"; break;
        default: reason = "unknown error"; break;
    }
    std::string msg = std::string(ctx) + ": " + reason + " (" +
                      std::to_string(code) + ")";
    nb::object exc_cls =
        nb::module_::import_("aiolibdatachannel.exceptions").attr("RTCError");
    PyErr_SetObject(exc_cls.ptr(),
                    nb::make_tuple(msg, code).ptr());
    throw nb::python_error();
}

int check(int rc, const char *ctx) {
    if (rc < 0) raise_rtc_error(rc, ctx);
    return rc;
}

std::optional<std::string> read_string(int id,
                                       int (*getter)(int, char *, int),
                                       const char *ctx) {
    int required = getter(id, nullptr, 0);
    if (required == RTC_ERR_NOT_AVAIL) return std::nullopt;
    if (required < 0) raise_rtc_error(required, ctx);
    if (required == 0) return std::string();
    // libdatachannel returns the length including the trailing NUL.
    std::string buf(required - 1, '\0');
    // Give it a real buffer that's required bytes long so the NUL fits.
    std::vector<char> scratch(required);
    int rc = getter(id, scratch.data(), required);
    if (rc < 0) raise_rtc_error(rc, ctx);
    return std::string(scratch.data());  // null-terminated
}

// ---- Single Python dispatcher -------------------------------------------
//
// Libdatachannel calls these static C trampolines; they route to the one
// Python callable registered by ``register_dispatcher``. NOTHING else in
// this translation unit holds a PyObject* — no per-handle state, no
// per-callback state. The Python side owns the routing table.

// Callback "kind" tags shared with the Python dispatcher. Keep these
// numerically stable — aiolibdatachannel._core hard-codes them.
enum CallbackKind : int {
    CB_LOCAL_DESCRIPTION      = 0,
    CB_LOCAL_CANDIDATE        = 1,
    CB_STATE_CHANGE           = 2,
    CB_ICE_STATE_CHANGE       = 3,
    CB_GATHERING_STATE_CHANGE = 4,
    CB_SIGNALING_STATE_CHANGE = 5,
    CB_DATA_CHANNEL           = 6,
    CB_DC_OPEN                = 7,
    CB_DC_CLOSED              = 8,
    CB_DC_ERROR               = 9,
    CB_DC_MESSAGE             = 10,
    CB_DC_BUFFERED_AMOUNT_LOW = 11,
    CB_LOG                    = 12,
};

// GIL is held by Python when register_dispatcher is called, so direct
// assignment is safe; it's cleared either explicitly or at interpreter
// teardown.
static std::mutex g_dispatcher_mutex;
static nb::object g_dispatcher;

void safe_call_dispatcher(nb::object args) {
    // Must run with GIL held. Called from the trampolines after acquiring.
    // ``args`` is a tuple or equivalent — dispatched via CPython's
    // PyObject_CallObject so we don't have to fight nanobind's cast layer
    // for a simple argument forward.
    PyObject *fn_ptr = nullptr;
    {
        std::lock_guard<std::mutex> lock(g_dispatcher_mutex);
        fn_ptr = g_dispatcher.ptr();
    }
    if (fn_ptr == nullptr || fn_ptr == Py_None) {
        return;
    }
    // Hold a reference across the call in case the registry is swapped.
    Py_INCREF(fn_ptr);
    PyObject *result = PyObject_CallObject(fn_ptr, args.ptr());
    Py_DECREF(fn_ptr);
    if (result == nullptr) {
        PyErr_WriteUnraisable(fn_ptr);
    } else {
        Py_DECREF(result);
    }
}

// ---- Peer-connection trampolines ----------------------------------------

void RTC_API tr_local_description(int pc, const char *sdp,
                                  const char *type, void *) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(
        int(CB_LOCAL_DESCRIPTION), pc,
        std::string(sdp ? sdp : ""),
        std::string(type ? type : "")));
}

void RTC_API tr_local_candidate(int pc, const char *cand,
                                const char *mid, void *) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(
        int(CB_LOCAL_CANDIDATE), pc,
        std::string(cand ? cand : ""),
        std::string(mid ? mid : "")));
}

void RTC_API tr_state_change(int pc, rtcState state, void *) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(
        int(CB_STATE_CHANGE), pc, static_cast<int>(state)));
}

void RTC_API tr_ice_state_change(int pc, rtcIceState state, void *) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(
        int(CB_ICE_STATE_CHANGE), pc, static_cast<int>(state)));
}

void RTC_API tr_gathering_state_change(int pc, rtcGatheringState state,
                                       void *) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(
        int(CB_GATHERING_STATE_CHANGE), pc, static_cast<int>(state)));
}

void RTC_API tr_signaling_state_change(int pc, rtcSignalingState state,
                                       void *) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(
        int(CB_SIGNALING_STATE_CHANGE), pc, static_cast<int>(state)));
}

void RTC_API tr_data_channel(int pc, int dc, void *) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(int(CB_DATA_CHANNEL), pc, dc));
}

// ---- DataChannel trampolines --------------------------------------------

void RTC_API tr_dc_open(int id, void *) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(int(CB_DC_OPEN), id));
}

void RTC_API tr_dc_closed(int id, void *) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(int(CB_DC_CLOSED), id));
}

void RTC_API tr_dc_error(int id, const char *err, void *) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(
        int(CB_DC_ERROR), id, std::string(err ? err : "")));
}

void RTC_API tr_dc_message(int id, const char *data, int size, void *) {
    nb::gil_scoped_acquire gil;
    if (size < 0) {
        // Null-terminated text payload.
        std::string text = data ? std::string(data) : std::string();
        safe_call_dispatcher(nb::make_tuple(int(CB_DC_MESSAGE), id, text, true));
    } else {
        // Binary payload of known length.
        nb::bytes payload(data, static_cast<size_t>(size));
        safe_call_dispatcher(nb::make_tuple(int(CB_DC_MESSAGE), id, payload, false));
    }
}

void RTC_API tr_dc_buffered_amount_low(int id, void *) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(int(CB_DC_BUFFERED_AMOUNT_LOW), id));
}

void tr_log(rtcLogLevel level, const char *message) {
    nb::gil_scoped_acquire gil;
    safe_call_dispatcher(nb::make_tuple(
        int(CB_LOG), static_cast<int>(level),
        std::string(message ? message : "")));
}

// ---- Blocking-call helper ------------------------------------------------
//
// Every libdatachannel call that can block a libdatachannel worker thread
// (delete, set_*_description, add_remote_candidate, send_message, close)
// must release the GIL so the worker can acquire it inside the trampoline.
// This also covers callback registration: rtcSet*Callback takes the channel's
// internal callback mutex and, when the channel is already open, invokes the
// callback synchronously. A worker thread holding that mutex mid-callback would
// otherwise block on the GIL while we block on the mutex — a deadlock.
// This is the ONE place in the file that releases the GIL — every blocking
// rtc* call goes through it, so there is a single pattern to audit. Note the
// call shape: `check(no_gil([&] { return rtc*(...); }), "ctx")`. The GIL is
// re-acquired before check() runs, because check() raises a Python exception
// and must not touch the interpreter without it. Never write
// `no_gil([&] { check(...); })`.

template <typename F>
auto no_gil(F &&f) -> decltype(f()) {
    nb::gil_scoped_release release;
    return f();
}

// ---- Binding entry point -------------------------------------------------

}  // anonymous namespace

NB_MODULE(_native, m) {
    m.doc() = "nanobind binding over libdatachannel (internal, unstable)";

    // ---- Callback wiring --------------------------------------------------

    m.def("register_dispatcher",
          [](nb::object fn) {
              std::lock_guard<std::mutex> lock(g_dispatcher_mutex);
              g_dispatcher = fn.is_none() ? nb::object() : fn;
          },
          nb::arg("fn").none(),
          "Register the module-level Python dispatcher. Passing None "
          "clears it — callers should do that before interpreter exit "
          "so late callbacks become no-ops.");

    // ---- Peer-connection lifecycle ---------------------------------------

    m.def("create_peer_connection",
          [](const std::vector<std::string> &ice_servers,
             int port_range_begin,
             int port_range_end,
             int mtu,
             int max_message_size,
             bool enable_ice_tcp,
             bool disable_auto_negotiation,
             int certificate_type,
             int ice_transport_policy) {
              std::vector<const char *> raw;
              raw.reserve(ice_servers.size());
              for (const auto &s : ice_servers) raw.push_back(s.c_str());

              rtcConfiguration cfg{};
              cfg.iceServers = raw.empty() ? nullptr : raw.data();
              cfg.iceServersCount = static_cast<int>(raw.size());
              cfg.proxyServer = nullptr;
              cfg.bindAddress = nullptr;
              cfg.certificateType = static_cast<rtcCertificateType>(certificate_type);
              cfg.iceTransportPolicy = static_cast<rtcTransportPolicy>(ice_transport_policy);
              cfg.enableIceTcp = enable_ice_tcp;
              cfg.enableIceUdpMux = false;
              cfg.disableAutoNegotiation = disable_auto_negotiation;
              cfg.forceMediaTransport = false;
              cfg.portRangeBegin = static_cast<uint16_t>(port_range_begin);
              cfg.portRangeEnd = static_cast<uint16_t>(port_range_end);
              cfg.mtu = mtu;
              cfg.maxMessageSize = max_message_size;

              int handle = no_gil([&] { return rtcCreatePeerConnection(&cfg); });
              return check(handle, "rtcCreatePeerConnection");
          },
          nb::arg("ice_servers"),
          nb::arg("port_range_begin") = 0,
          nb::arg("port_range_end") = 0,
          nb::arg("mtu") = 0,
          nb::arg("max_message_size") = 0,
          nb::arg("enable_ice_tcp") = false,
          nb::arg("disable_auto_negotiation") = true,
          nb::arg("certificate_type") = 0,
          nb::arg("ice_transport_policy") = 0);

    m.def("close_peer_connection", [](int pc) {
        no_gil([=] { rtcClosePeerConnection(pc); });
    });

    m.def("delete_peer_connection", [](int pc) {
        // Clear callbacks BEFORE delete — mirrors _core.py's teardown.
        // GIL is released for the entire teardown: the callback-clear
        // functions take libdatachannel's internal lock, and a worker
        // thread mid-callback into our dispatcher would otherwise
        // block on the GIL and deadlock us when rtcDeletePeerConnection
        // tries to join that worker.
        no_gil([=] {
            rtcSetLocalDescriptionCallback(pc, nullptr);
            rtcSetLocalCandidateCallback(pc, nullptr);
            rtcSetStateChangeCallback(pc, nullptr);
            rtcSetIceStateChangeCallback(pc, nullptr);
            rtcSetGatheringStateChangeCallback(pc, nullptr);
            rtcSetSignalingStateChangeCallback(pc, nullptr);
            rtcSetDataChannelCallback(pc, nullptr);
            rtcDeletePeerConnection(pc);
        });
    });

    // ---- Callback registration (per-handle) ------------------------------
    //
    // Libdatachannel requires that we register each callback type with
    // the trampoline when we want notifications. Passing nullptr clears
    // it.  All trampolines route through the one Python dispatcher.

    m.def("set_local_description_callback", [](int pc, bool enable) {
        check(no_gil([=] {
                  return rtcSetLocalDescriptionCallback(
                      pc, enable ? tr_local_description : nullptr);
              }),
              "rtcSetLocalDescriptionCallback");
    });
    m.def("set_local_candidate_callback", [](int pc, bool enable) {
        check(no_gil([=] {
                  return rtcSetLocalCandidateCallback(
                      pc, enable ? tr_local_candidate : nullptr);
              }),
              "rtcSetLocalCandidateCallback");
    });
    m.def("set_state_change_callback", [](int pc, bool enable) {
        check(no_gil([=] {
                  return rtcSetStateChangeCallback(
                      pc, enable ? tr_state_change : nullptr);
              }),
              "rtcSetStateChangeCallback");
    });
    m.def("set_ice_state_change_callback", [](int pc, bool enable) {
        check(no_gil([=] {
                  return rtcSetIceStateChangeCallback(
                      pc, enable ? tr_ice_state_change : nullptr);
              }),
              "rtcSetIceStateChangeCallback");
    });
    m.def("set_gathering_state_change_callback", [](int pc, bool enable) {
        check(no_gil([=] {
                  return rtcSetGatheringStateChangeCallback(
                      pc, enable ? tr_gathering_state_change : nullptr);
              }),
              "rtcSetGatheringStateChangeCallback");
    });
    m.def("set_signaling_state_change_callback", [](int pc, bool enable) {
        check(no_gil([=] {
                  return rtcSetSignalingStateChangeCallback(
                      pc, enable ? tr_signaling_state_change : nullptr);
              }),
              "rtcSetSignalingStateChangeCallback");
    });
    m.def("set_data_channel_callback", [](int pc, bool enable) {
        check(no_gil([=] {
                  return rtcSetDataChannelCallback(
                      pc, enable ? tr_data_channel : nullptr);
              }),
              "rtcSetDataChannelCallback");
    });

    // ---- SDP plumbing ----------------------------------------------------

    m.def("set_local_description",
          [](int pc, nb::object type) {
              std::string owned;
              const char *arg = nullptr;
              if (!type.is_none()) {
                  owned = nb::cast<std::string>(type);
                  arg = owned.c_str();
              }
              check(no_gil([&] { return rtcSetLocalDescription(pc, arg); }),
                    "rtcSetLocalDescription");
          },
          nb::arg("pc"), nb::arg("type") = nb::none());

    m.def("set_remote_description",
          [](int pc, const std::string &sdp, const std::string &type) {
              check(no_gil([&] {
                        return rtcSetRemoteDescription(pc, sdp.c_str(), type.c_str());
                    }),
                    "rtcSetRemoteDescription");
          });

    m.def("add_remote_candidate",
          [](int pc, const std::string &candidate, nb::object mid) {
              std::string owned;
              const char *mid_arg = nullptr;
              if (!mid.is_none()) {
                  owned = nb::cast<std::string>(mid);
                  mid_arg = owned.c_str();
              }
              check(no_gil([&] {
                        return rtcAddRemoteCandidate(pc, candidate.c_str(), mid_arg);
                    }),
                    "rtcAddRemoteCandidate");
          },
          nb::arg("pc"), nb::arg("candidate"), nb::arg("mid") = nb::none());

    m.def("get_local_description", [](int pc) {
        return read_string(pc, rtcGetLocalDescription,
                           "rtcGetLocalDescription");
    });
    m.def("get_remote_description", [](int pc) {
        return read_string(pc, rtcGetRemoteDescription,
                           "rtcGetRemoteDescription");
    });
    m.def("get_local_description_type", [](int pc) {
        return read_string(pc, rtcGetLocalDescriptionType,
                           "rtcGetLocalDescriptionType");
    });

    // ---- DataChannel creation -------------------------------------------

    m.def("create_data_channel",
          [](int pc, const std::string &label,
             bool unordered, bool unreliable,
             int max_packet_lifetime, int max_retransmits,
             const std::string &protocol,
             bool negotiated, bool manual_stream, int stream) {
              rtcDataChannelInit init{};
              init.reliability.unordered = unordered;
              init.reliability.unreliable = unreliable;
              init.reliability.maxPacketLifeTime = max_packet_lifetime;
              init.reliability.maxRetransmits = max_retransmits;
              init.protocol = protocol.empty() ? nullptr : protocol.c_str();
              init.negotiated = negotiated;
              init.manualStream = manual_stream;
              init.stream = static_cast<uint16_t>(stream);

              int handle = no_gil([&] {
                  return rtcCreateDataChannelEx(pc, label.c_str(), &init);
              });
              return check(handle, "rtcCreateDataChannelEx");
          },
          nb::arg("pc"), nb::arg("label"),
          nb::arg("unordered") = false,
          nb::arg("unreliable") = false,
          nb::arg("max_packet_lifetime") = 0,
          nb::arg("max_retransmits") = 0,
          nb::arg("protocol") = std::string(),
          nb::arg("negotiated") = false,
          nb::arg("manual_stream") = false,
          nb::arg("stream") = 0);

    m.def("delete_data_channel", [](int dc) {
        no_gil([=] {
            rtcSetOpenCallback(dc, nullptr);
            rtcSetClosedCallback(dc, nullptr);
            rtcSetErrorCallback(dc, nullptr);
            rtcSetMessageCallback(dc, nullptr);
            rtcSetBufferedAmountLowCallback(dc, nullptr);
            rtcDeleteDataChannel(dc);
        });
    });

    // ---- DataChannel callback registration ------------------------------

    m.def("set_dc_open_callback", [](int dc, bool enable) {
        check(no_gil([=] {
                  return rtcSetOpenCallback(dc, enable ? tr_dc_open : nullptr);
              }),
              "rtcSetOpenCallback");
    });
    m.def("set_dc_closed_callback", [](int dc, bool enable) {
        check(no_gil([=] {
                  return rtcSetClosedCallback(dc, enable ? tr_dc_closed : nullptr);
              }),
              "rtcSetClosedCallback");
    });
    m.def("set_dc_error_callback", [](int dc, bool enable) {
        check(no_gil([=] {
                  return rtcSetErrorCallback(dc, enable ? tr_dc_error : nullptr);
              }),
              "rtcSetErrorCallback");
    });
    m.def("set_dc_message_callback", [](int dc, bool enable) {
        check(no_gil([=] {
                  return rtcSetMessageCallback(dc, enable ? tr_dc_message : nullptr);
              }),
              "rtcSetMessageCallback");
    });
    m.def("set_dc_buffered_amount_low_callback", [](int dc, bool enable) {
        check(no_gil([=] {
                  return rtcSetBufferedAmountLowCallback(
                      dc, enable ? tr_dc_buffered_amount_low : nullptr);
              }),
              "rtcSetBufferedAmountLowCallback");
    });

    // ---- DataChannel I/O -------------------------------------------------

    m.def("send_bytes", [](int dc, nb::bytes data) {
        const char *buf = data.c_str();
        int size = static_cast<int>(data.size());
        check(no_gil([&] { return rtcSendMessage(dc, buf, size); }),
              "rtcSendMessage");
    });

    m.def("send_text", [](int dc, const std::string &text) {
        // size < 0 → null-terminated string.
        check(no_gil([&] { return rtcSendMessage(dc, text.c_str(), -1); }),
              "rtcSendMessage");
    });

    m.def("is_open", [](int dc) { return bool(rtcIsOpen(dc)); });
    m.def("is_closed", [](int dc) { return bool(rtcIsClosed(dc)); });
    m.def("buffered_amount", [](int dc) {
        return check(rtcGetBufferedAmount(dc), "rtcGetBufferedAmount");
    });
    m.def("max_message_size", [](int dc) {
        return check(rtcMaxMessageSize(dc), "rtcMaxMessageSize");
    });
    m.def("set_buffered_amount_low_threshold", [](int dc, int amount) {
        check(rtcSetBufferedAmountLowThreshold(dc, amount),
              "rtcSetBufferedAmountLowThreshold");
    });
    m.def("get_dc_label", [](int dc) {
        return read_string(dc, rtcGetDataChannelLabel,
                           "rtcGetDataChannelLabel");
    });
    m.def("get_dc_protocol", [](int dc) {
        return read_string(dc, rtcGetDataChannelProtocol,
                           "rtcGetDataChannelProtocol");
    });
    m.def("get_dc_stream", [](int dc) {
        return check(rtcGetDataChannelStream(dc),
                     "rtcGetDataChannelStream");
    });
    m.def("close_dc", [](int dc) {
        no_gil([=] { rtcClose(dc); });
    });

    // ---- Logger ----------------------------------------------------------

    m.def("init_logger", [](int level, bool enable) {
        no_gil([=] {
            rtcInitLogger(static_cast<rtcLogLevel>(level), enable ? tr_log : nullptr);
        });
    });

    // ---- libdatachannel lifecycle ---------------------------------------

    m.def("preload", []() {
        // Spins up the worker thread pool — can block for tens of ms.
        no_gil([] { rtcPreload(); });
    });
    m.def("cleanup", []() {
        no_gil([] { rtcCleanup(); });
    });
    m.def("set_thread_pool_size", [](int count) {
        return check(rtcSetThreadPoolSize(count), "rtcSetThreadPoolSize");
    });

    // ---- Error constants for parity with _ffi --------------------------

    m.attr("ERR_SUCCESS") = RTC_ERR_SUCCESS;
    m.attr("ERR_INVALID") = RTC_ERR_INVALID;
    m.attr("ERR_FAILURE") = RTC_ERR_FAILURE;
    m.attr("ERR_NOT_AVAIL") = RTC_ERR_NOT_AVAIL;
    m.attr("ERR_TOO_SMALL") = RTC_ERR_TOO_SMALL;

    // Export the callback-kind enum values so Python can switch on them.
    m.attr("CB_LOCAL_DESCRIPTION")      = int(CB_LOCAL_DESCRIPTION);
    m.attr("CB_LOCAL_CANDIDATE")        = int(CB_LOCAL_CANDIDATE);
    m.attr("CB_STATE_CHANGE")           = int(CB_STATE_CHANGE);
    m.attr("CB_ICE_STATE_CHANGE")       = int(CB_ICE_STATE_CHANGE);
    m.attr("CB_GATHERING_STATE_CHANGE") = int(CB_GATHERING_STATE_CHANGE);
    m.attr("CB_SIGNALING_STATE_CHANGE") = int(CB_SIGNALING_STATE_CHANGE);
    m.attr("CB_DATA_CHANNEL")           = int(CB_DATA_CHANNEL);
    m.attr("CB_DC_OPEN")                = int(CB_DC_OPEN);
    m.attr("CB_DC_CLOSED")              = int(CB_DC_CLOSED);
    m.attr("CB_DC_ERROR")               = int(CB_DC_ERROR);
    m.attr("CB_DC_MESSAGE")             = int(CB_DC_MESSAGE);
    m.attr("CB_DC_BUFFERED_AMOUNT_LOW") = int(CB_DC_BUFFERED_AMOUNT_LOW);
    m.attr("CB_LOG")                    = int(CB_LOG);
}
