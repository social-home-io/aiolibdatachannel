"""Configuration dataclasses passed to :class:`PeerConnection`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .enums import CertificateType, TransportPolicy

__all__ = ["DataChannelOptions", "RTCConfiguration"]


@dataclass(slots=True, kw_only=True)
class RTCConfiguration:
    """PeerConnection configuration.

    Mirrors ``rtcConfiguration`` from ``<rtc/rtc.h>`` with Python-friendly
    defaults. Any value left at its default lets libdatachannel pick the
    implementation default.
    """

    ice_servers: Sequence[str] = field(default_factory=list)
    port_range_begin: int = 0
    port_range_end: int = 0
    mtu: int = 0
    max_message_size: int = 0
    enable_ice_tcp: bool = False
    # Default True: the async API drives negotiation explicitly via
    # set_local_description / create_offer so we don't want libdatachannel
    # to fire off an offer the moment a DataChannel is added.
    disable_auto_negotiation: bool = True
    certificate_type: CertificateType = CertificateType.DEFAULT
    ice_transport_policy: TransportPolicy = TransportPolicy.ALL


@dataclass(slots=True, kw_only=True)
class DataChannelOptions:
    """Options for :meth:`PeerConnection.create_data_channel`.

    The defaults produce an *ordered, reliable* channel, matching the
    behaviour of the JavaScript WebRTC API.
    """

    ordered: bool = True
    max_packet_lifetime_ms: int | None = None
    max_retransmits: int | None = None
    protocol: str = ""
    negotiated: bool = False
    stream_id: int | None = None
