#!/usr/bin/env bash
# Added for Blake: run Lake's whole test suite in parallel and summarize.
#
# Usage: tests/run_all.sh [-j N] [-o OUTDIR] [--all] [--compare DIR] [TEST_DIR ...]
#   -j N           parallel tests (default 8)
#   -o DIR         where to put per-test logs and results.txt (default tests/.out)
#   --all          include the slow/network tests (bootstrap, toolchain, online)
#   --compare DIR  also report tests that pass in DIR/results.txt (an earlier run,
#                  e.g. of main) but fail now; exit non-zero if there are any
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

export LAKE=${LAKE:-$(lake -d .. query lake)}
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
rc=0
if [ "$fail" -gt 0 ]; then
  grep '^FAIL' "$OUT/results.txt"
  rc=1
fi
if [ -n "$COMPARE" ] && [ -f "$COMPARE/results.txt" ]; then
  newly=$(comm -12 <(grep '^PASS' "$COMPARE/results.txt" | awk '{print $2}' | sort) \
                   <(grep '^FAIL' "$OUT/results.txt" | awk '{print $2}' | sort) || true)
  if [ -n "$newly" ]; then
    echo "NEWLY FAILING compared to $COMPARE:"
    echo "$newly"
    rc=1
  else
    echo "no test that passed in $COMPARE fails now"
  fi
fi
exit $rc
