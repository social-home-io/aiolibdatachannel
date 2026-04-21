"""Configuration dataclasses passed to :class:`PeerConnection`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from urllib.parse import quote

from .enums import CertificateType, TransportPolicy

__all__ = ["DataChannelOptions", "IceServer", "IceServerLike", "RTCConfiguration"]


@dataclass(slots=True, frozen=True, kw_only=True)
class IceServer:
    """Structured STUN / TURN server entry.

    Either pass a bare URL string to :class:`RTCConfiguration.ice_servers`,
    or build an :class:`IceServer` with ``username`` / ``credential`` for
    TURN authentication. libdatachannel expects credentials inlined in
    the URL (``turn:user:pass@host:port``); :meth:`to_url` does that for
    you, URL-encoding any reserved characters.
    """

    url: str
    username: str | None = None
    credential: str | None = None

    def to_url(self) -> str:
        """Flatten to the ``scheme:[user:pass@]host`` form libdatachannel parses."""

        if self.username is None and self.credential is None:
            return self.url

        # Split off the scheme we'll put the creds after.
        scheme, _, remainder = self.url.partition(":")
        if not remainder:
            raise ValueError(f"IceServer.url is not a scheme-prefixed URL: {self.url!r}")

        # Don't let a pre-existing `user:pass@` in the URL get clobbered.
        if "@" in remainder.split("/", 1)[0]:
            raise ValueError(
                f"IceServer.url already carries credentials; pass them via "
                f"username/credential instead of embedding them in the URL: {self.url!r}"
            )

        user = quote(self.username or "", safe="")
        cred = quote(self.credential or "", safe="")
        return f"{scheme}:{user}:{cred}@{remainder}"


type IceServerLike = str | IceServer


@dataclass(slots=True, kw_only=True)
class RTCConfiguration:
    """PeerConnection configuration.

    Mirrors ``rtcConfiguration`` from ``<rtc/rtc.h>`` with Python-friendly
    defaults. Any value left at its default lets libdatachannel pick the
    implementation default.
    """

    ice_servers: Sequence[IceServerLike] = field(default_factory=list)
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
