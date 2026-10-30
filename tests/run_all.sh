#!/usr/bin/env bash
# Added for Blake: run Lake's whole test suite in parallel and summarize.
#
# Usage: tests/run_all.sh [-j N] [-o OUTDIR] [--all] [TEST_DIR ...]
#   -j N      parallel tests (default 8)
#   -o DIR    where to put per-test logs and results.txt (default tests/.out)
#   --all     include the slow/network tests (bootstrap, toolchain, online)
#   TEST_DIR  specific tests, e.g. tests/init examples/hello (default: every test)
#
# Uses the Blake binary built at the repository root unless LAKE is set, exactly
# like run_test.sh. Exits non-zero if any test fails.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

JOBS=8; OUT=.out; ALL=0; TESTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -j) JOBS=$2; shift 2 ;;
    -o) OUT=$2; shift 2 ;;
    --all) ALL=1; shift ;;
    *) TESTS+=("$1"); shift ;;
  esac
done

export LAKE=${LAKE:-$(lake -d .. query lake)}
# On NixOS, nixpkgs' elan wraps the toolchain's lake to export LEAN_CC; a
# source-built lake needs the same for the executables it links to run.
if [ -z "${LEAN_CC:-}" ] && [ -x "$(lean --print-prefix)/bin/cc" ]; then
  export LEAN_CC="$(lean --print-prefix)/bin/cc"
fi

if [ ${#TESTS[@]} -eq 0 ]; then
  mapfile -t TESTS < <(ls -d tests/*/test.sh examples/*/test.sh extra/*/test.sh | xargs -n1 dirname)
  if [ $ALL -eq 0 ]; then
    mapfile -t TESTS < <(printf '%s\n' "${TESTS[@]}" | grep -v -E 'bootstrap|toolchain|online')
  fi
fi

mkdir -p "$OUT"
export OUT
: > "$OUT/results.txt"
run_one() {
  d=$1; name=$(echo "$d" | tr '/' '_'); start=$(date +%s)
  if ./run_test.sh "$d" > "$OUT/$name.log" 2>&1; then r=PASS; else r=FAIL; fi
  echo "$r $d $(( $(date +%s) - start ))s" >> "$OUT/results.txt"
  echo "$r $d"
}
export -f run_one
printf '%s\n' "${TESTS[@]}" | xargs -P "$JOBS" -I{} bash -c 'run_one "$1"' _ {}

pass=$(grep -c '^PASS' "$OUT/results.txt" || true)
fail=$(grep -c '^FAIL' "$OUT/results.txt" || true)
echo "----"
echo "$pass passed, $fail failed (logs in tests/$OUT/)"
if [ "$fail" -gt 0 ]; then
  grep '^FAIL' "$OUT/results.txt"
  exit 1
fi
