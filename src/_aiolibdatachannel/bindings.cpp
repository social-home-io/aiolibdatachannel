// SPDX-License-Identifier: MPL-2.0
//
// nanobind thin binding over libdatachannel's C API (include/rtc/rtc.h).
// Only PeerConnection + DataChannel are exposed. Callbacks acquire the GIL
// and invoke Python callables directly; the asyncio bridge lives in the
// pure-Python layer.

#include <nanobind/nanobind.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/optional.h>

#include <atomic>
#include <cstring>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include <rtc/rtc.h>

namespace nb = nanobind;
using namespace nb::literals;

namespace {

// ---- Error translation ----------------------------------------------------

[[noreturn]] void raise_rtc_error(int code, const char *ctx) {
    const char *reason;
    switch (code) {
        case RTC_ERR_INVALID: reason = "invalid argument"; break;
        case RTC_ERR_FAILURE: reason = "runtime failure"; break;
        case RTC_ERR_NOT_AVAIL: reason = "not available"; break;
        case RTC_ERR_TOO_SMALL: reason = "buffer too small"; break;
        default: reason = "unknown error"; break;
    }
    std::string msg = std::string(ctx) + ": " + reason + " (" + std::to_string(code) + ")";
    nb::object exc_cls = nb::module_::import_("aiolibdatachannel.exceptions").attr("RTCError");
    PyErr_SetObject(exc_cls.ptr(), nb::make_tuple(msg, code).ptr());
    throw nb::python_error();
}

int check(int rc, const char *ctx) {
    if (rc < 0) raise_rtc_error(rc, ctx);
    return rc;
}

// Helper: read a string out of a getter that follows the
// `int getter(int id, char *buf, int size)` convention.
std::optional<std::string> read_string(int id, int (*getter)(int, char *, int), const char *ctx) {
    int required = getter(id, nullptr, 0);
    if (required == RTC_ERR_NOT_AVAIL) return std::nullopt;
    if (required < 0) raise_rtc_error(required, ctx);
    std::string buf(required ? required - 1 : 0, '\0');
    if (required > 0) {
        int rc = getter(id, buf.data(), required);
        if (rc < 0) raise_rtc_error(rc, ctx);
    }
    return buf;
}

// ---- Per-object state -----------------------------------------------------
//
// Each PeerConnection/DataChannel gets a heap-allocated `State` struct whose
// address is stored via rtcSetUserPointer. The static trampolines fetch the
// struct via rtcGetUserPointer, acquire the GIL, and invoke the Python
// callable stored in the matching slot.
//
// Callables are stored as nb::object so they can be cleared / replaced. All
// access to the slots must happen while holding the GIL.

struct PeerState {
    nb::object on_local_description;
    nb::object on_local_candidate;
    nb::object on_state_change;
    nb::object on_ice_state_change;
    nb::object on_gathering_state_change;
    nb::object on_signaling_state_change;
    nb::object on_data_channel;
};

struct ChannelState {
    nb::object on_open;
    nb::object on_closed;
    nb::object on_error;
    nb::object on_message;
    nb::object on_buffered_amount_low;
};

template <typename State>
State *state_for(int id) {
    return static_cast<State *>(rtcGetUserPointer(id));
}

// Call a Python callable, swallowing any exception so libdatachannel's worker
// thread is not derailed. Python errors are printed and cleared; the
// asyncio bridge is free to log them via sys.unraisablehook if desired.
template <typename... Args>
void safe_call(const nb::object &fn, Args &&...args) {
    if (!fn) return;
    nb::gil_scoped_acquire gil;
    try {
        fn(std::forward<Args>(args)...);
    } catch (nb::python_error &err) {
        err.restore();
        PyErr_WriteUnraisable(fn.ptr());
    } catch (const std::exception &err) {
        PyErr_SetString(PyExc_RuntimeError, err.what());
        PyErr_WriteUnraisable(fn.ptr());
    }
}

// ---- PeerConnection trampolines ------------------------------------------

void RTC_API on_local_description_tr(int pc, const char *sdp, const char *type, void *) {
    if (auto *s = state_for<PeerState>(pc))
        safe_call(s->on_local_description, std::string(sdp ? sdp : ""),
                  std::string(type ? type : ""));
}

void RTC_API on_local_candidate_tr(int pc, const char *cand, const char *mid, void *) {
    if (auto *s = state_for<PeerState>(pc))
        safe_call(s->on_local_candidate, std::string(cand ? cand : ""),
                  std::string(mid ? mid : ""));
}

void RTC_API on_state_change_tr(int pc, rtcState state, void *) {
    if (auto *s = state_for<PeerState>(pc))
        safe_call(s->on_state_change, static_cast<int>(state));
}

void RTC_API on_ice_state_change_tr(int pc, rtcIceState state, void *) {
    if (auto *s = state_for<PeerState>(pc))
        safe_call(s->on_ice_state_change, static_cast<int>(state));
}

void RTC_API on_gathering_state_change_tr(int pc, rtcGatheringState state, void *) {
    if (auto *s = state_for<PeerState>(pc))
        safe_call(s->on_gathering_state_change, static_cast<int>(state));
}

void RTC_API on_signaling_state_change_tr(int pc, rtcSignalingState state, void *) {
    if (auto *s = state_for<PeerState>(pc))
        safe_call(s->on_signaling_state_change, static_cast<int>(state));
}

void RTC_API on_data_channel_tr(int pc, int dc, void *) {
    if (auto *s = state_for<PeerState>(pc)) safe_call(s->on_data_channel, dc);
}

// ---- Channel trampolines --------------------------------------------------

void RTC_API on_open_tr(int id, void *) {
    if (auto *s = state_for<ChannelState>(id)) safe_call(s->on_open);
}

void RTC_API on_closed_tr(int id, void *) {
    if (auto *s = state_for<ChannelState>(id)) safe_call(s->on_closed);
}

void RTC_API on_error_tr(int id, const char *err, void *) {
    if (auto *s = state_for<ChannelState>(id))
        safe_call(s->on_error, std::string(err ? err : ""));
}

void RTC_API on_message_tr(int id, const char *data, int size, void *) {
    auto *s = state_for<ChannelState>(id);
    if (!s || !s->on_message) return;
    nb::gil_scoped_acquire gil;
    try {
        if (size < 0) {
            // Null-terminated text payload.
            s->on_message(nb::str(data ? data : ""));
        } else {
            s->on_message(nb::bytes(data, size));
        }
    } catch (nb::python_error &err) {
        err.restore();
        PyErr_WriteUnraisable(s->on_message.ptr());
    }
}

void RTC_API on_buffered_amount_low_tr(int id, void *) {
    if (auto *s = state_for<ChannelState>(id)) safe_call(s->on_buffered_amount_low);
}

// ---- Logging --------------------------------------------------------------

nb::object g_log_callback;
std::mutex g_log_mutex;

void RTC_API log_tr(rtcLogLevel level, const char *message) {
    nb::object cb;
    {
        std::lock_guard<std::mutex> lk(g_log_mutex);
        cb = g_log_callback;
    }
    if (!cb) return;
    nb::gil_scoped_acquire gil;
    try {
        cb(static_cast<int>(level), std::string(message ? message : ""));
    } catch (nb::python_error &err) {
        err.restore();
        PyErr_WriteUnraisable(cb.ptr());
    }
}

void py_init_logger(int level, nb::object callback) {
    {
        std::lock_guard<std::mutex> lk(g_log_mutex);
        g_log_callback = std::move(callback);
    }
    rtcInitLogger(static_cast<rtcLogLevel>(level), g_log_callback ? log_tr : nullptr);
}

// ---- PeerConnection class -------------------------------------------------

class PyPeerConnection {
public:
    PyPeerConnection(const std::vector<std::string> &ice_servers, int port_range_begin,
                     int port_range_end, int mtu, int max_message_size,
                     bool enable_ice_tcp, bool disable_auto_negotiation,
                     int certificate_type, int ice_transport_policy) {
        // Build C configuration; we must keep the char* buffers alive for the
        // duration of rtcCreatePeerConnection.
        std::vector<const char *> ice_ptrs;
        ice_ptrs.reserve(ice_servers.size());
        for (const auto &s : ice_servers) ice_ptrs.push_back(s.c_str());

        rtcConfiguration cfg{};
        cfg.iceServers = ice_ptrs.empty() ? nullptr : ice_ptrs.data();
        cfg.iceServersCount = static_cast<int>(ice_ptrs.size());
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

        int id = rtcCreatePeerConnection(&cfg);
        if (id < 0) raise_rtc_error(id, "rtcCreatePeerConnection");

        id_ = id;
        state_ = new PeerState();
        rtcSetUserPointer(id_, state_);
    }

    // Adopt an already-created peer connection handle (used by WebSocket /
    // server flows; currently only from create_data_channel siblings).
    PyPeerConnection(const PyPeerConnection &) = delete;
    PyPeerConnection &operator=(const PyPeerConnection &) = delete;

    ~PyPeerConnection() { destroy(); }

    int handle() const { return id_; }

    void close() {
        if (id_ < 0) return;
        nb::gil_scoped_release release;
        rtcClosePeerConnection(id_);
    }

    void destroy() {
        if (id_ < 0) return;
        int id = id_;
        id_ = -1;
        PeerState *state = state_;
        state_ = nullptr;
        {
            // rtcDeletePeerConnection blocks until in-flight callbacks
            // return; they may try to take the GIL, so we must release it.
            nb::gil_scoped_release release;
            rtcDeletePeerConnection(id);
        }
        delete state;
    }

    int create_data_channel(const std::string &label, bool unordered, bool unreliable,
                            unsigned int max_packet_lifetime, unsigned int max_retransmits,
                            const std::string &protocol, bool negotiated, bool manual_stream,
                            uint16_t stream) {
        rtcDataChannelInit init{};
        init.reliability.unordered = unordered;
        init.reliability.unreliable = unreliable;
        init.reliability.maxPacketLifeTime = max_packet_lifetime;
        init.reliability.maxRetransmits = max_retransmits;
        init.protocol = protocol.empty() ? nullptr : protocol.c_str();
        init.negotiated = negotiated;
        init.manualStream = manual_stream;
        init.stream = stream;
        return check(rtcCreateDataChannelEx(id_, label.c_str(), &init), "rtcCreateDataChannelEx");
    }

    void set_local_description(std::optional<std::string> type) {
        check(rtcSetLocalDescription(id_, type ? type->c_str() : nullptr), "rtcSetLocalDescription");
    }

    void set_remote_description(const std::string &sdp, const std::string &type) {
        check(rtcSetRemoteDescription(id_, sdp.c_str(), type.c_str()), "rtcSetRemoteDescription");
    }

    void add_remote_candidate(const std::string &cand, const std::string &mid) {
        check(rtcAddRemoteCandidate(id_, cand.c_str(), mid.empty() ? nullptr : mid.c_str()),
              "rtcAddRemoteCandidate");
    }

    std::optional<std::string> get_local_description() {
        return read_string(id_, rtcGetLocalDescription, "rtcGetLocalDescription");
    }

    std::optional<std::string> get_remote_description() {
        return read_string(id_, rtcGetRemoteDescription, "rtcGetRemoteDescription");
    }

    std::optional<std::string> get_local_description_type() {
        return read_string(id_, rtcGetLocalDescriptionType, "rtcGetLocalDescriptionType");
    }

    // --- callback setters ---

    void set_on_local_description(nb::object cb) {
        state_->on_local_description = std::move(cb);
        check(rtcSetLocalDescriptionCallback(id_, on_local_description_tr),
              "rtcSetLocalDescriptionCallback");
    }
    void set_on_local_candidate(nb::object cb) {
        state_->on_local_candidate = std::move(cb);
        check(rtcSetLocalCandidateCallback(id_, on_local_candidate_tr),
              "rtcSetLocalCandidateCallback");
    }
    void set_on_state_change(nb::object cb) {
        state_->on_state_change = std::move(cb);
        check(rtcSetStateChangeCallback(id_, on_state_change_tr), "rtcSetStateChangeCallback");
    }
    void set_on_ice_state_change(nb::object cb) {
        state_->on_ice_state_change = std::move(cb);
        check(rtcSetIceStateChangeCallback(id_, on_ice_state_change_tr),
              "rtcSetIceStateChangeCallback");
    }
    void set_on_gathering_state_change(nb::object cb) {
        state_->on_gathering_state_change = std::move(cb);
        check(rtcSetGatheringStateChangeCallback(id_, on_gathering_state_change_tr),
              "rtcSetGatheringStateChangeCallback");
    }
    void set_on_signaling_state_change(nb::object cb) {
        state_->on_signaling_state_change = std::move(cb);
        check(rtcSetSignalingStateChangeCallback(id_, on_signaling_state_change_tr),
              "rtcSetSignalingStateChangeCallback");
    }
    void set_on_data_channel(nb::object cb) {
        state_->on_data_channel = std::move(cb);
        check(rtcSetDataChannelCallback(id_, on_data_channel_tr), "rtcSetDataChannelCallback");
    }

private:
    int id_{-1};
    PeerState *state_{nullptr};
};

// ---- DataChannel class ----------------------------------------------------

class PyDataChannel {
public:
    explicit PyDataChannel(int id) : id_(id) {
        state_ = new ChannelState();
        rtcSetUserPointer(id_, state_);
    }

    PyDataChannel(const PyDataChannel &) = delete;
    PyDataChannel &operator=(const PyDataChannel &) = delete;

    ~PyDataChannel() { destroy(); }

    int handle() const { return id_; }

    void send_bytes(nb::bytes data) {
        const char *ptr = data.c_str();
        int size = static_cast<int>(data.size());
        int rc;
        {
            nb::gil_scoped_release release;
            rc = rtcSendMessage(id_, ptr, size);
        }
        if (rc < 0) raise_rtc_error(rc, "rtcSendMessage");
    }

    void send_text(const std::string &data) {
        int rc;
        {
            nb::gil_scoped_release release;
            // size < 0 → C API treats buffer as null-terminated string.
            rc = rtcSendMessage(id_, data.c_str(), -1);
        }
        if (rc < 0) raise_rtc_error(rc, "rtcSendMessage");
    }

    bool is_open() const { return rtcIsOpen(id_); }
    bool is_closed() const { return rtcIsClosed(id_); }

    int buffered_amount() const {
        int v = rtcGetBufferedAmount(id_);
        if (v < 0) raise_rtc_error(v, "rtcGetBufferedAmount");
        return v;
    }

    int max_message_size() const {
        int v = rtcMaxMessageSize(id_);
        if (v < 0) raise_rtc_error(v, "rtcMaxMessageSize");
        return v;
    }

    void set_buffered_amount_low_threshold(int amount) {
        check(rtcSetBufferedAmountLowThreshold(id_, amount), "rtcSetBufferedAmountLowThreshold");
    }

    std::optional<std::string> label() {
        return read_string(id_, rtcGetDataChannelLabel, "rtcGetDataChannelLabel");
    }

    std::optional<std::string> protocol() {
        return read_string(id_, rtcGetDataChannelProtocol, "rtcGetDataChannelProtocol");
    }

    int stream() const {
        int v = rtcGetDataChannelStream(id_);
        if (v < 0) raise_rtc_error(v, "rtcGetDataChannelStream");
        return v;
    }

    void close() {
        if (id_ < 0) return;
        nb::gil_scoped_release release;
        rtcClose(id_);
    }

    void destroy() {
        if (id_ < 0) return;
        int id = id_;
        id_ = -1;
        ChannelState *state = state_;
        state_ = nullptr;
        {
            nb::gil_scoped_release release;
            rtcDeleteDataChannel(id);
        }
        delete state;
    }

    void set_on_open(nb::object cb) {
        state_->on_open = std::move(cb);
        check(rtcSetOpenCallback(id_, on_open_tr), "rtcSetOpenCallback");
    }
    void set_on_closed(nb::object cb) {
        state_->on_closed = std::move(cb);
        check(rtcSetClosedCallback(id_, on_closed_tr), "rtcSetClosedCallback");
    }
    void set_on_error(nb::object cb) {
        state_->on_error = std::move(cb);
        check(rtcSetErrorCallback(id_, on_error_tr), "rtcSetErrorCallback");
    }
    void set_on_message(nb::object cb) {
        state_->on_message = std::move(cb);
        check(rtcSetMessageCallback(id_, on_message_tr), "rtcSetMessageCallback");
    }
    void set_on_buffered_amount_low(nb::object cb) {
        state_->on_buffered_amount_low = std::move(cb);
        check(rtcSetBufferedAmountLowCallback(id_, on_buffered_amount_low_tr),
              "rtcSetBufferedAmountLowCallback");
    }

private:
    int id_{-1};
    ChannelState *state_{nullptr};
};

}  // namespace

NB_MODULE(_aiolibdatachannel, m) {
    m.doc() = "Low-level nanobind bindings for libdatachannel's C API.";

    m.def("preload", []() { rtcPreload(); });
    m.def("cleanup", []() {
        nb::gil_scoped_release release;
        rtcCleanup();
    });
    m.def("init_logger", &py_init_logger, "level"_a, "callback"_a.none());
    m.def("set_thread_pool_size",
          [](unsigned int count) { return check(rtcSetThreadPoolSize(count), "rtcSetThreadPoolSize"); },
          "count"_a);

    // Error codes (for Python-side mapping)
    m.attr("ERR_SUCCESS") = int(RTC_ERR_SUCCESS);
    m.attr("ERR_INVALID") = int(RTC_ERR_INVALID);
    m.attr("ERR_FAILURE") = int(RTC_ERR_FAILURE);
    m.attr("ERR_NOT_AVAIL") = int(RTC_ERR_NOT_AVAIL);
    m.attr("ERR_TOO_SMALL") = int(RTC_ERR_TOO_SMALL);

    nb::class_<PyPeerConnection>(m, "PeerConnection")
        .def(nb::init<const std::vector<std::string> &, int, int, int, int, bool, bool, int, int>(),
             "ice_servers"_a, "port_range_begin"_a = 0, "port_range_end"_a = 0, "mtu"_a = 0,
             "max_message_size"_a = 0, "enable_ice_tcp"_a = false,
             "disable_auto_negotiation"_a = false, "certificate_type"_a = 0,
             "ice_transport_policy"_a = 0)
        .def_prop_ro("handle", &PyPeerConnection::handle)
        .def("close", &PyPeerConnection::close)
        .def("destroy", &PyPeerConnection::destroy)
        .def("create_data_channel", &PyPeerConnection::create_data_channel, "label"_a,
             "unordered"_a = false, "unreliable"_a = false, "max_packet_lifetime"_a = 0,
             "max_retransmits"_a = 0, "protocol"_a = std::string(), "negotiated"_a = false,
             "manual_stream"_a = false, "stream"_a = 0)
        .def("set_local_description", &PyPeerConnection::set_local_description,
             "type"_a.none() = nb::none())
        .def("set_remote_description", &PyPeerConnection::set_remote_description, "sdp"_a, "type"_a)
        .def("add_remote_candidate", &PyPeerConnection::add_remote_candidate, "candidate"_a,
             "mid"_a = std::string())
        .def("get_local_description", &PyPeerConnection::get_local_description)
        .def("get_remote_description", &PyPeerConnection::get_remote_description)
        .def("get_local_description_type", &PyPeerConnection::get_local_description_type)
        .def("set_on_local_description", &PyPeerConnection::set_on_local_description, "callback"_a)
        .def("set_on_local_candidate", &PyPeerConnection::set_on_local_candidate, "callback"_a)
        .def("set_on_state_change", &PyPeerConnection::set_on_state_change, "callback"_a)
        .def("set_on_ice_state_change", &PyPeerConnection::set_on_ice_state_change, "callback"_a)
        .def("set_on_gathering_state_change", &PyPeerConnection::set_on_gathering_state_change,
             "callback"_a)
        .def("set_on_signaling_state_change", &PyPeerConnection::set_on_signaling_state_change,
             "callback"_a)
        .def("set_on_data_channel", &PyPeerConnection::set_on_data_channel, "callback"_a);

    nb::class_<PyDataChannel>(m, "DataChannel")
        .def(nb::init<int>(), "handle"_a)
        .def_prop_ro("handle", &PyDataChannel::handle)
        .def("send_bytes", &PyDataChannel::send_bytes, "data"_a)
        .def("send_text", &PyDataChannel::send_text, "data"_a)
        .def("is_open", &PyDataChannel::is_open)
        .def("is_closed", &PyDataChannel::is_closed)
        .def("buffered_amount", &PyDataChannel::buffered_amount)
        .def("max_message_size", &PyDataChannel::max_message_size)
        .def("set_buffered_amount_low_threshold",
             &PyDataChannel::set_buffered_amount_low_threshold, "amount"_a)
        .def("label", &PyDataChannel::label)
        .def("protocol", &PyDataChannel::protocol)
        .def("stream", &PyDataChannel::stream)
        .def("close", &PyDataChannel::close)
        .def("destroy", &PyDataChannel::destroy)
        .def("set_on_open", &PyDataChannel::set_on_open, "callback"_a)
        .def("set_on_closed", &PyDataChannel::set_on_closed, "callback"_a)
        .def("set_on_error", &PyDataChannel::set_on_error, "callback"_a)
        .def("set_on_message", &PyDataChannel::set_on_message, "callback"_a)
        .def("set_on_buffered_amount_low", &PyDataChannel::set_on_buffered_amount_low,
             "callback"_a);
}
