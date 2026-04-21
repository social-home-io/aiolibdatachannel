"""Interactive WebRTC DataChannel answerer. See offerer.py for usage."""

from __future__ import annotations

import asyncio
import base64
import json
import sys

from aiolibdatachannel import PeerConnection, RTCConfiguration


def encode(data: dict[str, object]) -> str:
    return base64.b64encode(json.dumps(data).encode()).decode()


def decode(line: str) -> dict[str, object]:
    return json.loads(base64.b64decode(line.strip()))


async def main() -> None:
    cfg = RTCConfiguration(ice_servers=["stun:stun.l.google.com:19302"])
    async with PeerConnection(cfg) as pc:
        offer = decode(sys.stdin.readline())
        await pc.set_remote_description(str(offer["sdp"]), str(offer["type"]))

        answer = await pc.create_answer()
        print(encode({"sdp": answer.sdp, "type": answer.type}), flush=True)

        dc = await pc.accept_data_channel()
        await dc.wait_open()
        print("[answerer] channel open", file=sys.stderr)

        async for msg in dc:
            print(f"[peer] {msg!r}", file=sys.stderr)
            await dc.send(f"echo: {msg!r}")


if __name__ == "__main__":
    asyncio.run(main())
