# Building aiolibdatachannel

This document covers everything needed to build the package from source —
including how to update the bundled libdatachannel, switch TLS backends,
and produce a release wheel locally.

## Prerequisites

All platforms need:

- **Python 3.12 or newer** — headers must be available (on Debian/Ubuntu
  install `python3.12-dev`).
- **CMake ≥ 3.24** and **Ninja** (the build backend falls back to Make
  if Ninja isn't present).
- **A C++17 compiler**: GCC ≥ 10, Clang ≥ 11, or MSVC 2019+.
- **OpenSSL development headers** (the default TLS backend).

Platform specifics:

| Platform | Install command |
|---|---|
| Debian / Ubuntu | `sudo apt install build-essential cmake ninja-build libssl-dev python3.12-dev` |
| Fedora | `sudo dnf install gcc-c++ cmake ninja-build openssl-devel python3.12-devel` |
| macOS | `brew install cmake ninja openssl@3` (set `OPENSSL_ROOT_DIR=$(brew --prefix openssl@3)` if CMake can't find it) |
| Windows | Install Visual Studio 2022 Build Tools; `choco install cmake ninja openssl` or use vcpkg |

## Cloning

libdatachannel is vendored as a git submodule under `vendor/libdatachannel`
— clone with submodules:

```bash
git clone --recursive https://github.com/pvizeli/aiolibdatachannel.git
cd aiolibdatachannel
```

If you already cloned without `--recursive`:

```bash
git submodule update --init --recursive
```

## Editable install (developer loop)

```bash
pip install -e ".[dev]"
```

`scikit-build-core` configures a CMake build under `build/`, compiles
libdatachannel statically along with the nanobind extension, and installs
the resulting `.so`/`.pyd` into your virtualenv. Subsequent imports
re-run CMake on-demand whenever C++ sources change (via
`editable.rebuild = true` in `pyproject.toml`).

Run the full local verify:

```bash
scripts/dev-build.sh
```

## Producing a wheel locally

```bash
pipx run build         # yields dist/aiolibdatachannel-*.whl and .tar.gz
```

To test the wheel in isolation:

```bash
python -m venv /tmp/verify && source /tmp/verify/bin/activate
pip install dist/aiolibdatachannel-*.whl
pytest tests/
```

## Switching TLS backend

OpenSSL is the default. To build against mbedtls or GnuTLS instead, pass
CMake definitions through pip/scikit-build-core:

```bash
# mbedtls
pip install -e . -C cmake.define.USE_MBEDTLS=ON

# GnuTLS
pip install -e . -C cmake.define.USE_GNUTLS=ON
```

Only one backend may be enabled at a time.

## Updating bundled libdatachannel

Use the helper script with the target tag:

```bash
scripts/update-libdatachannel.sh v0.24.3
```

The script:

1. Fetches tags in `vendor/libdatachannel`.
2. Checks out the requested tag.
3. Recursively syncs libdatachannel's own submodules (`usrsctp`, `plog`,
   `libjuice`, `nlohmann_json`, etc.).
4. Stages `vendor/libdatachannel` for commit.

After it runs, **rebuild and re-test** before committing:

```bash
pip install -e . --force-reinstall --no-build-isolation
pytest
git commit -m "Bump libdatachannel to v0.24.3"
```

The CI release workflow picks up whatever is committed — no extra action
is needed beyond pushing a new `v*` tag.

## CI wheel build matrix

Release wheels are produced by `.github/workflows/release.yml` using
[`cibuildwheel`](https://cibuildwheel.pypa.io/):

| Platform | Tags |
|---|---|
| Linux | `manylinux_2_28` x86_64 + aarch64 |
| macOS | x86_64 + arm64 |
| Windows | AMD64 |

Wheels ship the Python source as-is plus the bundled
`libdatachannel.{so,dylib,dll}`; cffi loads it at runtime. A single
`py3-none-<platform>` wheel per `(os, arch)` covers every supported
Python interpreter — no per-version build or stable-ABI tagging needed.

## Troubleshooting

- **`Could NOT find OpenSSL`** — install OpenSSL dev headers (see table
  above) or point CMake at your installation via
  `pip install -e . -C cmake.define.OPENSSL_ROOT_DIR=/path/to/openssl`.
- **`nanobind not found`** — install it explicitly: `pip install nanobind`.
  Normally `scikit-build-core` resolves it from `build-system.requires`.
- **Changes to `bindings.cpp` aren't picked up** — editable rebuilds only
  trigger on import. Touch `__init__.py` or run
  `pip install -e . --no-build-isolation --force-reinstall`.
