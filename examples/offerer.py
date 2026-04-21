"""Interactive WebRTC DataChannel offerer.

Pipes an SDP offer to stdout, reads the answer + candidates from stdin in
base64-JSON form, then echoes messages typed on stdin until the channel
closes. Pair with ``examples/answerer.py``.

Usage::

    python examples/offerer.py | python examples/answerer.py

(Or run them in separate terminals and copy/paste the JSON blobs.)
"""

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
        dc = await pc.create_data_channel("chat")

        offer = await pc.create_offer()
        print(encode({"sdp": offer.sdp, "type": offer.type}), flush=True)

        answer = decode(sys.stdin.readline())
        await pc.set_remote_description(str(answer["sdp"]), str(answer["type"]))

        await dc.wait_open()
        print("[offerer] channel open", file=sys.stderr)

        async def pump_recv() -> None:
            async for msg in dc:
                print(f"[peer] {msg!r}", file=sys.stderr)

        recv_task = asyncio.create_task(pump_recv())
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        while line := await reader.readline():
            await dc.send(line.decode().rstrip("\n"))
        recv_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
