#!/usr/bin/env bash
# Added for Blake: run Lake's whole test suite in parallel and summarize.
#
# Usage: tests/run_all.sh [-j N] [-o OUTDIR] [--all] [--compare DIR] [TEST_DIR ...]
#   -j N           parallel tests (default 8)
#   -o DIR         where to put per-test logs and results.txt (default tests/.out)
#   --all          include the slow/network tests (bootstrap, toolchain, online)
#   --compare DIR  judge against DIR/results.txt (an earlier run, e.g. of main):
#                  exit non-zero only for failures that did not already fail there
#   TEST_DIR       specific tests, e.g. tests/init examples/hello (default: every test)
#
# Uses the Blake binary built at the repository root unless LAKE is set, exactly
# like run_test.sh. Exits non-zero if any test fails (or, with --compare, if any
# test newly fails). Needs bash >= 3.2; on macOS install coreutils (gtail).
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

JOBS=8; OUT=.out; ALL=0; COMPARE=""; TESTS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -j) JOBS=$2; shift 2 ;;
    -o) OUT=$2; shift 2 ;;
    --all) ALL=1; shift ;;
    --compare) COMPARE=$2; shift 2 ;;
    *) TESTS+=("$1"); shift ;;
  esac
done

if [ -z "${LAKE:-}" ]; then
  LAKE=$(lake -d .. query lake) || { echo "could not build or locate Blake's lake"; exit 1; }
fi
export LAKE
TEST_TIMEOUT=${TEST_TIMEOUT:-900}
# On NixOS, nixpkgs' elan wraps the toolchain's lake to export LEAN_CC; a
# source-built lake needs the same for the executables it links to run.
if [ -z "${LEAN_CC:-}" ] && [ -x "$(lean --print-prefix)/bin/cc" ]; then
  export LEAN_CC="$(lean --print-prefix)/bin/cc"
fi

if [ ${#TESTS[@]} -eq 0 ]; then
  while IFS= read -r d; do
    if [ $ALL -eq 1 ] || ! echo "$d" | grep -q -E 'bootstrap|toolchain|online'; then
      TESTS+=("$d")
    fi
  done < <(ls -d tests/*/test.sh examples/*/test.sh extra/*/test.sh | xargs -n1 dirname)
fi

mkdir -p "$OUT"
export OUT
: > "$OUT/results.txt"
run_one() {
  d=$1; name=$(echo "$d" | tr '/' '_'); start=$(date +%s)
  if timeout "$TEST_TIMEOUT" ./run_test.sh "$d" > "$OUT/$name.log" 2>&1; then r=PASS; else r=FAIL; fi
  echo "$r $d $(( $(date +%s) - start ))s" >> "$OUT/results.txt"
  echo "$r $d"
}
export -f run_one
export TEST_TIMEOUT
printf '%s\n' "${TESTS[@]}" | xargs -P "$JOBS" -I{} bash -c 'run_one "$1"' _ {}

pass=$(grep -c '^PASS' "$OUT/results.txt" || true)
fail=$(grep -c '^FAIL' "$OUT/results.txt" || true)
echo "----"
echo "$pass passed, $fail failed (logs in tests/$OUT/)"
rc=0
if [ "$fail" -gt 0 ]; then
  grep '^FAIL' "$OUT/results.txt"
  rc=1
fi
if [ -n "$COMPARE" ]; then
  if [ ! -f "$COMPARE/results.txt" ] || ! grep -q '^PASS' "$COMPARE/results.txt"; then
    echo "no usable baseline at $COMPARE/results.txt (missing or nothing passed); judging on failures alone"
  else
    # With a baseline, a failure counts unless the same test already failed there
    # (so new tests, and tests the baseline never ran, must pass).
    newly=$(comm -13 <(grep '^FAIL' "$COMPARE/results.txt" | awk '{print $2}' | sort) \
                     <(grep '^FAIL' "$OUT/results.txt" | awk '{print $2}' | sort) || true)
    if [ -n "$newly" ]; then
      echo "FAILING and not already failing in $COMPARE:"
      echo "$newly"
      rc=1
    else
      echo "every failure (if any) already failed in $COMPARE"
      rc=0
    fi
  fi
fi
exit $rc
