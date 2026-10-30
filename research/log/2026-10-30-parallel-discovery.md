# parallel-discovery: read and parse module headers in parallel while collecting a library's modules

- **Date:** 2026-10-30
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** success
- **Branch / PR:** research/parallel-discovery — (pending)
- **Result files:** research/results/20261030T163740Z-2e698309-11d39d8.json

## Hypothesis

After `monitor-wake`, a no-op build of `medium-wide` takes ~0.10-0.13 s, but it
uses only ~2x parallelism on 16 CPUs (0.28 s of CPU). `perf record --latency`
with samples bucketed by time shows three phases: ~35 ms of single-threaded
startup (module initializers), then ~35 ms at 3-7 busy threads, then ~40 ms at
~17 busy threads (trace checks, `recFetchPreSetup`). In the middle phase one
worker thread is inside `LeanLib.recCollectLocalModules` (66% of its samples) and
two or three others are in `Module.recFetchInput` (reading each `.lean` file,
`Hash.ofText` with `String.crlfToLf`, UTF-8 validation, `parseImports'`).

`recCollectLocalModules` (`Lake/Build/Library.lean`) walks the library's modules
depth-first, and for each module it fetches the `imports` facet and **waits** for
it before moving on. So the headers of the library's modules are read, hashed,
and parsed roughly one at a time, on the critical path of every build, before
any trace is checked.

Change: before the walk, fetch the `imports` facet of every root module; and
before descending into a module's same-library imports, fetch theirs. Fetches
are memoized, and the walk fetches exactly these jobs anyway, so the set of jobs
is unchanged. They are only started earlier, so the header jobs run on all
workers while the walk waits on the first ones.

Expected: the middle phase shrinks from ~35 ms to a few ms on 16 CPUs, so `noop`
drops by ~25-35 ms (~25%) on every corpus and `leaf` by the same absolute amount
(~5-10%). Longer scenarios do not move. Measured with the standard sweep.

## What was done

`Lake/Build/Library.lean`, `LeanLib.recCollectLocalModules`: fetch (without
awaiting) the `imports` facet of every root module before the walk, and in `go`,
of each not-yet-visited same-library import before descending into them. Eleven
lines.

By hand on `medium-wide` (20 alternating pairs against stock Lake, CPUs 16-31): a
no-op of the whole library 0.091 s vs Lake's 0.141 s (1.56x; `main` was 1.24x in
the same kind of measurement during `monitor-wake`). `time`: ~0.09 s wall for
~0.25 s of CPU.

Checks: `nix develop -c tests/run_all.sh --compare .out-main` 93 passed, 0 failed;
quick harness `correctness of candidate: identical artifacts to lake, all builds
ok, break check ok`.

A process slip, recorded so it is not repeated: I rebuilt Blake on this branch
while the base test run for `main` was still in its last test
(`tests/check-paranoid`), so that one test of the base run may have used the
candidate binary for part of its run. It passed, which means `--compare` still
held the candidate to passing it (and it did); the other 92 base results came
from `main`'s binary. Wait for the base run before rebuilding.

## Measurements

Standard sweep, 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run,
29 minutes.

```
speedup of main vs lake: cold=0.964x, noop=1.568x, leaf=1.261x, swarm=1.020x, hub=1.008x, primary_geomean=1.194x
speedup of candidate vs lake: cold=1.011x, noop=1.846x, leaf=1.317x, swarm=1.026x, hub=1.011x, primary_geomean=1.260x
speedup of candidate vs main: cold=1.049x, noop=1.177x, leaf=1.044x, swarm=1.006x, hub=1.003x, primary_geomean=1.055x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.000x over 1 runs, threshold 7%; medians main 35.91s, candidate 35.89s)
verdict medium-deep/noop: improved (paired speedup 1.181x over 10 runs, threshold 7%; medians main 0.11s, candidate 0.09s)
verdict medium-deep/leaf: same (paired speedup 1.030x over 10 runs, threshold 7%; medians main 0.46s, candidate 0.45s)
verdict medium-deep/swarm: same (paired speedup 0.999x over 3 runs, threshold 3%; medians main 15.20s, candidate 15.22s)
verdict medium-deep/hub: same (paired speedup 0.997x over 3 runs, threshold 3%; medians main 34.84s, candidate 34.74s)
verdict medium-layered/cold: improved (paired speedup 1.148x over 1 runs, threshold 7%; medians main 43.03s, candidate 37.48s)
verdict medium-layered/noop: improved (paired speedup 1.304x over 10 runs, threshold 7%; medians main 0.12s, candidate 0.09s)
verdict medium-layered/leaf: same (paired speedup 1.024x over 10 runs, threshold 7%; medians main 0.48s, candidate 0.47s)
verdict medium-layered/swarm: same (paired speedup 1.006x over 3 runs, threshold 3%; medians main 13.54s, candidate 13.53s)
verdict medium-layered/hub: same (paired speedup 1.014x over 3 runs, threshold 3%; medians main 33.85s, candidate 33.53s)
verdict medium-wide/cold: same (paired speedup 1.005x over 1 runs, threshold 7%; medians main 34.07s, candidate 33.91s)
verdict medium-wide/noop: improved (paired speedup 1.150x over 10 runs, threshold 7%; medians main 0.10s, candidate 0.09s)
verdict medium-wide/leaf: same (paired speedup 1.059x over 10 runs, threshold 7%; medians main 0.26s, candidate 0.24s)
verdict medium-wide/swarm: same (paired speedup 1.013x over 10 runs, threshold 7%; medians main 1.04s, candidate 1.02s)
verdict medium-wide/hub: same (paired speedup 0.999x over 3 runs, threshold 3%; medians main 34.58s, candidate 34.68s)
verdict: success
```

`medium-layered/cold` counts as "improved" (1.148x), but that is noise and not
this change: `main`'s single cold run took 43.0 s and 666 s of CPU, while the
candidate (37.5 s, 580 s) and stock Lake (37.4 s, 582 s) were measured right
after it. The machine was in a slower state for that one run. This change saves
at most tens of milliseconds of a cold build. It does not affect the verdict,
which `noop` already decides.

## Conclusion

`noop` is 1.15-1.30x faster than `main` on every corpus (0.10-0.12 s down to
~0.09 s); `leaf` moved by 2-6%, under the 7% threshold, because module discovery
is only ~20-30 ms of a 0.25-0.5 s leaf build. Against stock Lake, `noop` is now
1.85x and the primary geomean 1.26x.

What is left of a 0.09 s no-op is roughly: ~30 ms of single-threaded process
startup (module initializers, then loading the workspace), the trace checks
(parallel), and teardown. Per header job, `Hash.ofText`'s `String.crlfToLf` walk
and `readFile`'s UTF-8 validation are the largest costs. For `leaf`, the `lean`
run of the edited module (~0.15-0.35 s) now dominates.
