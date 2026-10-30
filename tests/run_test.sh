#!/usr/bin/env bash
# Modified for Blake (2026-10-29): `lake` is built from the `lakefile.toml` at
# the repository root instead of from a `lakefile.toml` in this directory.
set -euo pipefail

# Ensure this runs from the lake test directory
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

# Build `LAKE` from the repository root or use the provided one
LAKE=${LAKE:-$(lake -d .. query lake)}

# Run the test
TEST_DIR="$(cd -- "$1" && pwd)"; shift
CMD1="cd \"$TEST_DIR\" && ./test.sh"
CMD2='rc=$?; echo "EXIT: $rc"; exit $rc'
$LAKE -d .. env bash -c "$CMD1; $CMD2"
