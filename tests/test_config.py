"""Configuration dataclass tests."""

from __future__ import annotations

import pytest

from aiolibdatachannel import IceServer, PeerConnection, RTCConfiguration


def test_ice_server_without_credentials_is_passthrough() -> None:
    server = IceServer(url="stun:stun.l.google.com:19302")
    assert server.to_url() == "stun:stun.l.google.com:19302"


def test_ice_server_embeds_credentials_into_url() -> None:
    server = IceServer(
        url="turn:turn.example.com:3478",
        username="alice",
        credential="s3cret",
    )
    assert server.to_url() == "turn:alice:s3cret@turn.example.com:3478"


def test_ice_server_url_encodes_reserved_chars() -> None:
    server = IceServer(
        url="turn:turn.example.com:3478",
        username="alice@realm",
        credential="p@ss word",
    )
    assert server.to_url() == "turn:alice%40realm:p%40ss%20word@turn.example.com:3478"


def test_ice_server_rejects_url_already_with_credentials() -> None:
    server = IceServer(
        url="turn:alice:old@turn.example.com:3478",
        username="bob",
        credential="new",
    )
    with pytest.raises(ValueError, match="already carries credentials"):
        server.to_url()


def test_ice_server_rejects_unscheemed_url() -> None:
    server = IceServer(url="turn.example.com", username="alice", credential="pw")
    with pytest.raises(ValueError, match="not a scheme-prefixed URL"):
        server.to_url()


@pytest.mark.asyncio
async def test_rtc_configuration_accepts_mixed_str_and_dataclass() -> None:
    # The PeerConnection creation just needs to not raise when we mix
    # IceServer with plain strings.
    config = RTCConfiguration(
        ice_servers=[
            "stun:stun.l.google.com:19302",
            IceServer(url="turn:turn.example.com:3478", username="u", credential="p"),
        ]
    )
    async with PeerConnection(config):
        pass
