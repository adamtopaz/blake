# adaptive-lean-threads: give each `lean` process cap / (running `lean` processes) threads

- **Date:** 2026-10-30
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** failure
- **Branch / PR:** research/adaptive-lean-threads-log (code: research/adaptive-lean-threads-wip, https://github.com/adamtopaz/blake/tree/research/adaptive-lean-threads-wip) — (pending)
- **Result files:** research/results/20261030T223613Z-2e698309-a10ba3d.json

## Hypothesis

`cold` and `hub` are CPU-bound: ~540-660 s of CPU in ~35-45 s on 16 CPUs. Lake runs
up to `cap` `lean` processes at once (`cap` = `LEAN_NUM_THREADS`, 16 under the
harness), and each inherits `LEAN_NUM_THREADS`, so a full machine has up to 16
processes × 16 threads on 16 CPUs. In a test of 128 corpus modules compiled 16 at a time
by `lean` directly, `-j1` used 5-6% less CPU than the default (80.3 vs 85.2 s and
72.8 vs 76.8 s) and was 2-5% faster. But a lone `lean` run is 12-30% slower with
`-j1` than with 16 threads (three `medium-wide` modules: 495 vs 408 ms, 662 vs 465
ms, 678 vs 599 ms), so a fixed `-j1` would hurt `leaf` and chain-bound builds
(`swarm` on `deep`/`layered`). `-j1` and `-j16` produced byte-identical
`.olean`/`.ilean`/`.c` for all three.

Change (`Lake/Build/Actions.lean`, `compileLeanModule`): count the `lean`
processes Lake is running (a process-wide `IO.Ref Nat`), and start each new one
with `LEAN_NUM_THREADS = max 1 (cap / running)`, where `running` includes it and
`cap` is `LEAN_NUM_THREADS` if set, else the hardware concurrency (Lean's own
default; if that is unknown, the child's environment is left alone). A lone
`lean` keeps all threads; 16 concurrent ones get 1 each. Always at most `cap`.

Expected: `cold` and `hub` a few percent faster (the `hub` threshold is 3%),
`leaf` unchanged, chain-bound `swarm` unchanged. Risk: `swarm` on `wide` (24
modules at once, each slower single-threaded). Measured with the standard sweep.

Also examined in this iteration, before choosing this (no code kept):

- **Parallel job creation** (the fetch thread is ~79% busy in a no-op while
  workers idle): an unsafe probe that fetched the library's modules from 8
  parallel tasks (racing on the build store, so it creates duplicate jobs:
  1036 and 1277 instead of 1002) was *slower* than `main` on a no-op (paired 0.978x
  on `medium-wide`, 0.966x on `medium-deep`). Contention on the shared,
  copy-on-write build store eats the parallelism, so a thread-safe version would
  not pay either.
- **Chain rebuilds are `lean`-bound:** rebuilding the 67-module chain below
  `Corpus.M00490` of `medium-deep` took 30.5 s (straced). The gaps between one
  `lean` exiting and the next starting summed to 260 ms (median 5.3 ms), and
  `lean` runs took a median of 701 ms. A deep module's `lean` run in isolation is
  ~0.39 s, of which `import` is ~154 ms and initialization ~33 ms
  (`--profile`). All 1154 `.olean`s it imports are mmapped at their fixed
  addresses (no relocation fallback).
- **A fork server for `lean`** is possible in principle: `libleanshared.so`
  exports `lean_main` and `lean_shell_main`, so pre-initialized children could
  run `lean`'s own entry point. It would save only process start, initialization,
  and teardown (~25 ms of a 0.27-0.4 s run), not `import`, and it needs a
  single-threaded zygote started by Blake. Not attempted.

## What was done

`Lake/Build/Actions.lean`: a private `builtin_initialize runningLeanProcs :
IO.Ref Nat`, a `leanThreadCap` helper, and in `compileLeanModule` an increment
before `rawProc` and a decrement in a `finally`, with `LEAN_NUM_THREADS` added to
the child's environment. Commit `a10ba3d` on `research/adaptive-lean-threads-wip`.

A non-standard A/B first (`--configs medium-wide --scenarios cold,leaf,swarm,hub
--tools main,candidate`) was already `neutral`: cold 1.018x (657 s vs 642 s of
CPU), hub 0.996x, swarm 1.013x, leaf 0.989x.

Checks: `nix develop -c tests/run_all.sh --compare .out-main` 93 passed, 0 failed;
quick harness `correctness of candidate: identical artifacts to lake, all builds
ok, break check ok`.

## Measurements

Standard sweep, 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run,
33 minutes.

```
speedup of main vs lake: cold=1.010x, noop=1.982x, leaf=1.283x, swarm=1.024x, hub=1.013x, primary_geomean=1.274x
speedup of candidate vs lake: cold=1.011x, noop=1.919x, leaf=1.316x, swarm=1.034x, hub=1.009x, primary_geomean=1.274x
speedup of candidate vs main: cold=1.000x, noop=0.968x, leaf=1.026x, swarm=1.010x, hub=0.996x, primary_geomean=1.000x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.015x over 1 runs, threshold 7%; medians main 42.64s, candidate 42.01s)
verdict medium-deep/noop: same (paired speedup 0.998x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-deep/leaf: same (paired speedup 1.042x over 10 runs, threshold 7%; medians main 0.50s, candidate 0.47s)
verdict medium-deep/swarm: same (paired speedup 1.004x over 3 runs, threshold 3%; medians main 18.02s, candidate 18.00s)
verdict medium-deep/hub: same (paired speedup 1.013x over 3 runs, threshold 3%; medians main 42.68s, candidate 42.75s)
verdict medium-layered/cold: same (paired speedup 1.011x over 1 runs, threshold 7%; medians main 45.87s, candidate 45.37s)
verdict medium-layered/noop: same (paired speedup 1.003x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-layered/leaf: same (paired speedup 1.008x over 10 runs, threshold 7%; medians main 0.46s, candidate 0.45s)
verdict medium-layered/swarm: same (paired speedup 1.006x over 3 runs, threshold 3%; medians main 16.14s, candidate 16.04s)
verdict medium-layered/hub: same (paired speedup 0.996x over 3 runs, threshold 3%; medians main 41.02s, candidate 40.96s)
verdict medium-wide/cold: same (paired speedup 0.975x over 1 runs, threshold 7%; medians main 41.20s, candidate 42.25s)
verdict medium-wide/noop: same (paired speedup 0.937x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.09s)
verdict medium-wide/leaf: same (paired speedup 1.023x over 10 runs, threshold 7%; medians main 0.24s, candidate 0.24s)
verdict medium-wide/swarm: same (paired speedup 1.020x over 10 runs, threshold 7%; medians main 1.21s, candidate 1.19s)
verdict medium-wide/hub: same (paired speedup 0.977x over 3 runs, threshold 3%; medians main 42.30s, candidate 42.76s)
verdict: neutral
```

CPU seconds of the cold builds (candidate vs `main`): deep 566.7 vs 573.5,
layered 699.6 vs 709.3, wide 641.6 vs 630.1. That is within ±2%, i.e. noise.

## Conclusion

No effect. Under the harness each `lean` defaults to 16 threads on 16 CPUs, and a
corpus module gives Lean's task pool little to do in parallel, so the extra
threads mostly sleep and cost almost nothing. The 5-6% CPU saving seen with `-j1`
in the direct 128-module test does not carry over to Lake builds. (That test left
`LEAN_NUM_THREADS` unset, i.e. 32 threads per process, and ran 16 processes
continuously.) Oversubscription inside `lean` is not where cold and hub builds
lose time.

What this iteration established, for whoever comes next (see the bullets under
"Hypothesis"):

- `noop`: the fetch thread is the bottleneck, but parallel job creation does not
  help, because the shared copy-on-write build store serializes it. Only less
  work per store operation, or fewer operations, can help there.
- `swarm` on `deep`/`layered` and `hub`: the time is inside `lean` (`import` is
  ~40% of a deep module's run, initialization ~8%), not in Blake's gaps between
  `lean` runs (~5 ms each). The levers are therefore inside `lean`'s process
  model. A fork server built on `lean_shell_main` could save ~25 ms per run but
  not `import`. Anything that saves `import` (sharing an imported environment
  across modules) needs Lean internals.
