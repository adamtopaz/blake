# dirty-first: check (and rebuild) modules whose source is newer than their trace first

- **Date:** 2026-10-30
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** failure
- **Branch / PR:** research/dirty-first-log (code: research/dirty-first-wip, https://github.com/adamtopaz/blake/tree/research/dirty-first-wip) — https://github.com/adamtopaz/blake/pull/6
- **Result files:** research/results/20261030T181728Z-2e698309-a091b87.json

## Hypothesis

After `parallel-discovery`, a `leaf` build of `medium-wide` (~0.24 s in the last
sweep) is ~0.09 s of Blake work around a ~0.15 s `lean` run. `strace -f -ttt` of a
leaf build of `Corpus.M00500` (with `main`'s binary, so all times inflated by
strace) shows the order of events: startup until ~45 ms, headers read until ~65
ms, then 1000 trace checks from ~70 to ~150 ms. `M00500.trace` is read at ~125 ms,
its stale outputs are deleted, and `lean` is started at ~135-195 ms.

The edited module's `lean` run waits behind the trace checks of the modules
ahead of it. `LeanLib.recBuildLean` fetches every module's `leanArts` in
collection order, and the task pool runs jobs roughly in creation order. So the
later an edited module comes in that order, the later its rebuild starts. The
harness's leaf, like any edited module except the first, waits behind part of
the 1000 checks.

Change: before that fold, fetch the `leanArts` of modules whose source file is
strictly newer than their `.trace` file, so their jobs (and their dependencies'
check jobs) are created, and therefore run, first. The fold then fetches every
module as before. Fetches are memoized, so it gets the same jobs back and mixes
them in the original order: the library job's trace and every module's trace are
unchanged. Modules without a `.trace` (never built) are not prioritized, so a cold
build keeps its order exactly. Cost: two `stat` calls per module, ~1 ms for
1000 modules.

Expected: `leaf` and `swarm` start their `lean` runs up to one check phase
(~40-60 ms) earlier, ~10-20% on `leaf` and a few percent on `swarm` on `wide`.
`noop` pays ~1 ms. `hub` and `cold` do not move. Measured with the standard sweep.

## What was done

`Lake/Build/Library.lean`, `LeanLib.recBuildLean`: before the fold that fetches
and mixes every module's `leanArts`, a loop fetches (without awaiting) the
`leanArts` of each module whose `.lean` file's mtime is strictly greater than
its `.trace` file's (`sourceNewerThanTrace`, two `metadata` calls). Commit
`a091b87` on `research/dirty-first-wip`.

It did what it was meant to do mechanically. `strace -f -ttt` of a leaf build
of `Corpus.M00998` on `medium-wide`: the edited module's `.trace` is now the
first one read (62 ms, before almost all other checks), its stale outputs are
deleted, and `lean` is exec'd at 67 ms, where before it waited until the other
checks were done. But `lean` then needed ~60 ms just to open its source file:
its own startup competes for the 16 CPUs with Blake's 16 workers, which are
still checking the other 999 traces.

Before the standard sweep, a non-standard A/B (`--configs medium-wide,medium-deep
--scenarios noop,leaf --tools main,candidate`) already said `neutral`: leaf
1.046x on `wide`, 1.000x on `deep`.

Checks: `nix develop -c tests/run_all.sh --compare .out-main` 93 passed, 0 failed
(`main` also 93/93); quick harness `correctness of candidate: identical artifacts
to lake, all builds ok, break check ok`.

## Measurements

Standard sweep, 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run,
30 minutes.

```
speedup of main vs lake: cold=1.013x, noop=1.913x, leaf=1.314x, swarm=1.069x, hub=1.007x, primary_geomean=1.282x
speedup of candidate vs lake: cold=0.974x, noop=1.834x, leaf=1.322x, swarm=1.067x, hub=0.995x, primary_geomean=1.266x
speedup of candidate vs main: cold=0.961x, noop=0.958x, leaf=1.006x, swarm=0.998x, hub=0.988x, primary_geomean=0.988x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 0.989x over 1 runs, threshold 7%; medians main 38.49s, candidate 38.93s)
verdict medium-deep/noop: same (paired speedup 1.015x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-deep/leaf: same (paired speedup 0.997x over 10 runs, threshold 7%; medians main 0.45s, candidate 0.44s)
verdict medium-deep/swarm: same (paired speedup 1.003x over 3 runs, threshold 3%; medians main 15.32s, candidate 15.26s)
verdict medium-deep/hub: same (paired speedup 1.004x over 3 runs, threshold 3%; medians main 36.77s, candidate 36.62s)
verdict medium-layered/cold: same (paired speedup 0.983x over 1 runs, threshold 7%; medians main 40.06s, candidate 40.73s)
verdict medium-layered/noop: same (paired speedup 0.967x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.10s)
verdict medium-layered/leaf: same (paired speedup 0.994x over 10 runs, threshold 7%; medians main 0.46s, candidate 0.46s)
verdict medium-layered/swarm: same (paired speedup 1.012x over 3 runs, threshold 3%; medians main 14.38s, candidate 14.29s)
verdict medium-layered/hub: same (paired speedup 0.976x over 3 runs, threshold 3%; medians main 36.40s, candidate 37.30s)
verdict medium-wide/cold: regressed (paired speedup 0.913x over 1 runs, threshold 7%; medians main 34.42s, candidate 37.71s)
verdict medium-wide/noop: same (paired speedup 0.959x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-wide/leaf: same (paired speedup 1.013x over 10 runs, threshold 7%; medians main 0.24s, candidate 0.24s)
verdict medium-wide/swarm: same (paired speedup 0.967x over 10 runs, threshold 7%; medians main 1.00s, candidate 1.01s)
verdict medium-wide/hub: same (paired speedup 0.983x over 3 runs, threshold 3%; medians main 33.11s, candidate 33.67s)
verdict: failure
```

The one "regressed" verdict, `medium-wide/cold` (0.913x), is a single pair and
is drift, not this change: the three cold builds ran candidate, Lake, `main` in
that order and got steadily faster (37.7 s / 570 s CPU, 36.1 s / 547 s, 34.4 s /
528 s). In a cold build no module has a `.trace`, so nothing is prioritized and
the jobs are created in the same order as on `main`; the change adds 2000 failed
`stat` calls (~2 ms). Still, the verdict is the verdict, and nothing improved
anyway: `leaf` moved by -0.6% to +1.3%, `swarm` by -3% to +1%.

## Conclusion

Starting the edited module's `lean` run first does not shorten a leaf build,
because the build is not waiting on the order of the checks; it is waiting on
CPU. During the ~40 ms of trace checking, all 16 CPUs are busy, so a `lean`
process started early just shares them and finishes no sooner. Reordering work
inside a CPU-saturated phase does not help. The checks have to get cheaper, or
the rebuild has to stop competing with them.

What would help instead, from the profiles taken for this entry:

- The checks cost ~0.2 ms of CPU per module, mostly Lake's job machinery: ~10
  facet jobs per module, ~24 futex calls per module (task hand-offs), ~18 `statx`
  per module, JSON parsing of a 2.4 KB `.trace` (~15 µs, only ~15 ms of CPU for
  1000 modules, so not the main cost), and `fetchOrCreate` on the shared
  `DTreeMap` build store. Fewer jobs per module, or a fast path that checks an
  up-to-date module in one job, is the lever for `noop` and `leaf` now.
- A `lean` run is ~0.27 s for a corpus module in isolation (`--profile`):
  ~20 ms initialization, ~60 ms import (an imports-only file takes 0.10 s), and
  the rest elaboration. A cold or `hub` build is CPU-saturated at 16 CPUs
  (~520-620 s of CPU in ~35-40 s), so only less work per `lean` run helps there.
  Running `lean` with `-j1` instead of 16 threads was 2-5% faster in a 128-module
  test (noise-level).
- Blake's own startup is ~26 ms. Linking it as a non-PIE executable (to skip
  181k load-time relocations) saved ~2 ms of that in a quick test; not worth an
  iteration on its own. The rest is Lean's module initializers, which Blake runs
  because it links all of Lean for `lakefile.lean` elaboration.
