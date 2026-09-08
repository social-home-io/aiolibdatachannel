#!/usr/bin/env bash
# Update the bundled libdatachannel submodule to a specific release tag.
#
# Usage: scripts/update-libdatachannel.sh <tag>
# Example: scripts/update-libdatachannel.sh v0.24.5
#
# After this runs:
#   1. the submodule is checked out at <tag> with its own submodules synced;
#   2. the change is staged for commit;
#   3. you still need to actually rebuild and run the tests.
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <tag>" >&2
    exit 1
fi

TAG="$1"
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT/vendor/libdatachannel"

echo "==> Fetching tags..."
git fetch --tags origin

echo "==> Checking out $TAG..."
git checkout "$TAG"

echo "==> Syncing nested submodules..."
git submodule update --init --recursive

cd "$ROOT"
git add vendor/libdatachannel

echo
echo "libdatachannel bumped to $TAG."
echo "Next steps:"
echo "  1. Rebuild:        pip install -e . --no-build-isolation --force-reinstall"
echo "  2. Run tests:      pytest -q"
echo "  3. Commit:         git commit -m 'Bump libdatachannel to $TAG'"
