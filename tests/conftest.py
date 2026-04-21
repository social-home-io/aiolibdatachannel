"""Shared pytest fixtures."""

from __future__ import annotations

import sys

import pytest


@pytest.fixture(autouse=True)
def _raise_unraisable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn ``PyErr_WriteUnraisable`` warnings into test failures.

    Our nanobind trampolines swallow Python exceptions from user callbacks
    and forward them to :func:`sys.unraisablehook`. Without this hook the
    errors disappear silently and hide real bugs.
    """

    def hook(arg: sys.UnraisableHookArgs) -> None:
        raise AssertionError(f"unraisable exception: {arg.exc_value!r}") from arg.exc_value

    monkeypatch.setattr(sys, "unraisablehook", hook)
