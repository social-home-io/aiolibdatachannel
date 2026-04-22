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
that on Windows is still open.

The Python↔C boundary uses [nanobind](https://github.com/wjakob/nanobind)'s
stable-ABI mode (`Py_LIMITED_API`): libdatachannel and its static
dependencies (usrsctp, libjuice, OpenSSL) link directly into the
extension, and a single `cp312-abi3-<platform>` wheel per architecture
covers every CPython 3.12+ interpreter — no per-Python-version build.
The binding layer itself is deliberately thin: native trampolines route
every libdatachannel callback through a single Python dispatcher, and
the asyncio semantics live in the pure-Python wrapper on top, not in C++.

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

Full native build (requires a C++17 toolchain, CMake ≥ 3.24, ninja,
and OpenSSL headers):

```bash
git clone --recursive https://github.com/social-home-io/aiolibdatachannel.git
cd aiolibdatachannel
pip install -e .[dev]
pytest
```

If you only need to touch the Python wrapper (async semantics, async
iterators, event plumbing), skip the C++ build and run against the
pure-Python stand-in from `tests/_fake_native.py`:

```bash
pip install pytest pytest-asyncio
PYTHONPATH=. pytest -m "not native"
```

`tests/conftest.py` injects the fake into `sys.modules` whenever
`AIOLIB_REQUIRE_NATIVE` is unset. Tests marked `@pytest.mark.native`
(loopback, cancellation, logging, errors, stress) are auto-skipped in
that mode.

Long-running soak tests (10 000-message loopback, 100 PC lifecycles,
shutdown-leak subprocess checks) live behind a `stress` marker and
are off by default — enable with `pytest -m stress` or
`AIOLIB_STRESS=1 pytest`. They also run nightly in CI.

See [docs/BUILDING.md](docs/BUILDING.md) for detailed build instructions,
including how to switch TLS backends and how to bump the bundled
libdatachannel version.

## License

This project is licensed under the Mozilla Public License 2.0, matching
the license of libdatachannel which it bundles and statically links.
See [LICENSE](LICENSE) and [vendor/libdatachannel/LICENSE](vendor/libdatachannel/LICENSE).
