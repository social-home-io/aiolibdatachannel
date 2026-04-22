"""Shared pytest fixtures.

sys.modules injection (borrowed from the social-home/core pattern):
when ``AIOLIB_REQUIRE_NATIVE`` is unset, tests run against the
pure-Python fake in :mod:`tests._fake_native`. Tests that need the
real compiled extension mark themselves ``@pytest.mark.native`` and
are deselected when the fake is injected.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ---- sys.modules injection -------------------------------------------
# Must run before any ``aiolibdatachannel.*`` import, so keep this
# block at the top of the file and avoid nested package imports above
# it.

_REQUIRE_NATIVE = bool(os.environ.get("AIOLIB_REQUIRE_NATIVE"))

if not _REQUIRE_NATIVE:
    # Make ``from tests import _fake_native`` work before pytest's own
    # conftest import machinery has added ``tests`` to sys.path.
    _TESTS_DIR = Path(__file__).parent
    if str(_TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(_TESTS_DIR))
    import _fake_native  # noqa: E402  — injected before aiolibdatachannel

    sys.modules["aiolibdatachannel._native"] = _fake_native

import pytest  # noqa: E402


_RUN_STRESS = bool(os.environ.get("AIOLIB_STRESS"))


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item],
) -> None:
    """Filter ``@pytest.mark.native`` tests when the fake is active, and
    ``@pytest.mark.stress`` tests unless explicitly opted in.

    * The fake can't make a real DTLS handshake → native tests get
      skipped unless the caller set ``AIOLIB_REQUIRE_NATIVE=1``.
    * Stress tests take 10s+ each and we don't want to pay that on
      every fast-feedback run → skipped unless
      ``AIOLIB_STRESS=1`` is set or pytest was invoked with
      ``-m stress`` (in which case pytest's own marker filter takes
      over and we don't add our skip on top).
    """
    skip_native = pytest.mark.skip(
        reason="requires real _native extension "
        "(run with AIOLIB_REQUIRE_NATIVE=1)",
    )
    skip_stress = pytest.mark.skip(
        reason="stress test — run with AIOLIB_STRESS=1 "
        "(or 'pytest -m stress')",
    )
    # If the user already passed ``-m stress`` pytest deselects everything
    # else for us; we only need to auto-skip when stress isn't the
    # explicit selection.
    marker_expr = config.getoption("-m", default="")
    stress_explicitly_requested = "stress" in marker_expr
    for item in items:
        if not _REQUIRE_NATIVE and item.get_closest_marker("native") is not None:
            item.add_marker(skip_native)
        if (
            not _RUN_STRESS
            and not stress_explicitly_requested
            and item.get_closest_marker("stress") is not None
        ):
            item.add_marker(skip_stress)


@pytest.fixture(autouse=True)
def _raise_unraisable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn ``PyErr_WriteUnraisable`` warnings into test failures.

    Our native trampolines swallow Python exceptions from user callbacks
    and forward them to :func:`sys.unraisablehook`. Without this hook the
    errors disappear silently and hide real bugs.
    """

    def hook(arg: sys.UnraisableHookArgs) -> None:
        raise AssertionError(
            f"unraisable exception: {arg.exc_value!r}",
        ) from arg.exc_value

    monkeypatch.setattr(sys, "unraisablehook", hook)


@pytest.fixture(autouse=True)
def _reset_fake_native() -> None:
    """When running against the fake, reset its in-memory state between
    tests so handles don't leak across test boundaries."""
    if _REQUIRE_NATIVE:
        yield
        return
    # Re-register the real dispatcher after reset — _core imported it
    # at module-load time, so after reset() the fake has no dispatcher
    # and callbacks would no-op.
    import aiolibdatachannel._core as core
    import aiolibdatachannel._native as native

    native.reset()
    native.register_dispatcher(core._dispatch)
    yield
    native.reset()
    native.register_dispatcher(core._dispatch)
