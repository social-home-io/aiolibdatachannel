# aiolibdatachannel

An `asyncio`-friendly Python wrapper around
[libdatachannel](https://github.com/paullouisageneau/libdatachannel) — a
lightweight C/C++ WebRTC stack. `aiolibdatachannel` exposes WebRTC
**Peer Connections** and **Data Channels** through idiomatic `async/await`
Python.

> **Scope:** PeerConnection + DataChannel only. WebSocket and Media/RTP
> transport are deliberately disabled in this build to keep the wheel
> small and the API focused.

## Install

```bash
pip install aiolibdatachannel
```

Wheels are published for Linux (`manylinux_2_28` x86_64 / aarch64) and
macOS (arm64, 14.0+). Intel Macs and Windows aren't currently covered —
Intel Mac because Apple Silicon is the modern target, Windows because
libdatachannel dynamically links OpenSSL and the wheel packaging for
that on Windows is still open. libdatachannel is bundled as a shared library inside the
wheel and loaded via [`cffi`](https://cffi.readthedocs.io/) — no native
Python extension, no per-Python-version build, one
`py3-none-<platform>` wheel covers every supported interpreter.

## Quickstart

```python
import asyncio
from aiolibdatachannel import PeerConnection, RTCConfiguration

async def main() -> None:
    config = RTCConfiguration(ice_servers=["stun:stun.l.google.com:19302"])

    async with PeerConnection(config) as pc:
        dc = await pc.create_data_channel("chat")

        offer = await pc.create_offer()
        # ... ship `offer` to the peer via your signalling channel ...
        answer_sdp = await receive_answer_from_peer()
        await pc.set_remote_description(answer_sdp, "answer")

        await dc.wait_open()
        await dc.send(b"hello")

        async for message in dc:
            print("got", message)

asyncio.run(main())
```

See `examples/offerer.py` and `examples/answerer.py` for a runnable pair of
scripts that negotiate over stdin/stdout.

## Logging

Route libdatachannel's internal logs through Python's standard
[`logging`](https://docs.python.org/3/library/logging.html) module:

```python
import logging
from aiolibdatachannel import install_python_logger

logging.basicConfig(level=logging.INFO)
install_python_logger()  # logger name defaults to "aiolibdatachannel"
```

Severities are translated (`FATAL→CRITICAL`, `ERROR→ERROR`,
`WARNING→WARNING`, `INFO→INFO`, `DEBUG`/`VERBOSE→DEBUG`) and the filter
threshold on the native side is derived from the Python logger's
effective level, so you don't pay to format lines that would be filtered
out anyway. Pass `install_python_logger(my_logger)` or
`install_python_logger(level=LogLevel.DEBUG)` to customise.

## Development

```bash
git clone --recursive https://github.com/social-home-io/aiolibdatachannel.git
cd aiolibdatachannel
pip install -e .[dev]
pytest
```

See [docs/BUILDING.md](docs/BUILDING.md) for detailed build instructions,
including how to switch TLS backends and how to bump the bundled
libdatachannel version.

## License

This project is licensed under the Mozilla Public License 2.0, matching
the license of libdatachannel which it bundles and statically links.
See [LICENSE](LICENSE) and [vendor/libdatachannel/LICENSE](vendor/libdatachannel/LICENSE).
