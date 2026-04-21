#!/usr/bin/env bash
# One-shot developer build: set up venv, install in editable mode, run tests.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
    echo "==> Creating .venv..."
    python3.12 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Upgrading build toolchain..."
python -m pip install --upgrade pip wheel

echo "==> Installing in editable mode with dev extras..."
pip install -e ".[dev]"

echo "==> Running linters and tests..."
ruff check src tests
ruff format --check src tests
mypy src
pytest -q
