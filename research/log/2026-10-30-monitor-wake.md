# monitor-wake: end the build when its last job finishes, not at the monitor's next 100 ms tick

- **Date:** 2026-10-30
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** success
- **Branch / PR:** research/monitor-wake — (pending)
- **Result files:** research/results/20261030T151332Z-2e698309-4bc612e.json

## Hypothesis

With startup fixed (`olean-githash`, `elan-sysroot`), a Blake build of a single
module of `medium-wide` (`lake build Corpus.M00500`, a handful of dependencies)
still took 134 ms, almost as long as a no-op build of all 1000 modules (140 ms),
although starting and loading the workspace (`lake env true`) takes 37 ms. An
`strace -f -ttt` of that build shows all work done 45 ms after exec (the last
`.c.hash` read) and then the main thread blocked in a futex for 89 ms before
exiting.

The wait is Lake's build monitor (`Lake/Build/Run.lean`, `Monitor.loop`): it
scans the jobs, renders progress, and then sleeps the rest of `updateFrequency`
(100 ms) unconditionally. A build therefore ends 0-100 ms after its last job
finishes, at the next tick. Stock Lake does the same, so this is a fixed cost
of every `lake build` that neither Lake nor Blake needs.

Change: the monitor still ticks every 100 ms for progress output, but its sleep
ends as soon as every job it is waiting on has finished. It polls in 1 ms
slices with a cursor that skips finished jobs, so each job's state is read about
once per tick. Nothing about what is built or how changes; only when the
monitor notices that the build is over.

Expected: builds end up to 100 ms sooner. That should show in `noop` (but a
full no-op of 1000 modules has ~95 ms of real work, so less than in the
single-module case) and in `leaf` (a ~0.3 s `lean` run that ends at a random
point of the tick, so ~50 ms on average, ~10%), less in `swarm` on `wide`
(~1.1 s), and not at all in `hub` or `cold`. Measured with the standard sweep.

## What was done

`Lake/Build/Run.lean`: new `Monitor.waitUntil`; `Monitor.sleep` takes the
unfinished jobs of the last scan and waits with `waitUntil` until they have all
finished or the tick's deadline (`lastUpdate + updateFrequency`) passes, instead
of `IO.sleep` for the rest of the tick; `Monitor.loop` passes its unfinished jobs.
About 20 lines.

By hand on `medium-wide` (20 alternating pairs against stock Lake, CPUs 16-31):
`lake build Corpus.M00500` 0.035 s vs Lake's 0.147 s (4.2x); a no-op of the
whole library 0.131 s vs 0.157 s (1.24x). The whole-library no-op gained less
because it has ~95 ms of real work after startup. That work is mostly serial:
`time` shows ~0.28 s of CPU (user+sys) in ~0.13 s of wall time on 16 CPUs, and
~0.22 s of wall time on one CPU.

Checks: `nix develop -c tests/run_all.sh --compare .out-main` 93 passed, 0 failed
(`main` also 93/93 in the dev shell); quick harness `correctness of candidate:
identical artifacts to lake, all builds ok, break check ok`, with
`noop=2.793x, leaf=1.367x` vs stock Lake on the small corpora.

## Measurements

Standard sweep, 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run,
29 minutes.

```
speedup of main vs lake: cold=1.002x, noop=1.027x, leaf=1.025x, swarm=0.996x, hub=0.998x, primary_geomean=1.011x
speedup of candidate vs lake: cold=1.005x, noop=1.583x, leaf=1.257x, swarm=1.023x, hub=1.004x, primary_geomean=1.196x
speedup of candidate vs main: cold=1.003x, noop=1.542x, leaf=1.227x, swarm=1.027x, hub=1.006x, primary_geomean=1.182x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.010x over 1 runs, threshold 7%; medians main 35.98s, candidate 35.61s)
verdict medium-deep/noop: improved (paired speedup 1.579x over 10 runs, threshold 7%; medians main 0.17s, candidate 0.11s)
verdict medium-deep/leaf: improved (paired speedup 1.138x over 10 runs, threshold 7%; medians main 0.56s, candidate 0.47s)
verdict medium-deep/swarm: same (paired speedup 1.006x over 3 runs, threshold 3%; medians main 15.29s, candidate 15.06s)
verdict medium-deep/hub: same (paired speedup 1.007x over 3 runs, threshold 3%; medians main 34.55s, candidate 34.32s)
verdict medium-layered/cold: same (paired speedup 1.003x over 1 runs, threshold 7%; medians main 37.01s, candidate 36.90s)
verdict medium-layered/noop: improved (paired speedup 1.518x over 10 runs, threshold 7%; medians main 0.17s, candidate 0.11s)
verdict medium-layered/leaf: improved (paired speedup 1.158x over 10 runs, threshold 7%; medians main 0.57s, candidate 0.49s)
verdict medium-layered/swarm: same (paired speedup 1.003x over 3 runs, threshold 3%; medians main 13.52s, candidate 13.39s)
verdict medium-layered/hub: same (paired speedup 1.024x over 3 runs, threshold 3%; medians main 34.00s, candidate 33.43s)
verdict medium-wide/cold: same (paired speedup 0.996x over 1 runs, threshold 7%; medians main 33.70s, candidate 33.84s)
verdict medium-wide/noop: improved (paired speedup 1.594x over 10 runs, threshold 7%; medians main 0.17s, candidate 0.10s)
verdict medium-wide/leaf: improved (paired speedup 1.324x over 10 runs, threshold 7%; medians main 0.37s, candidate 0.27s)
verdict medium-wide/swarm: improved (paired speedup 1.070x over 10 runs, threshold 7%; medians main 1.08s, candidate 1.02s)
verdict medium-wide/hub: same (paired speedup 0.994x over 3 runs, threshold 3%; medians main 34.08s, candidate 34.29s)
verdict: success
```

## Conclusion

The largest gain of the loop so far, from a ~20-line change: `noop` 1.52-1.59x and
`leaf` 1.14-1.32x faster than `main` on every corpus, and `swarm` on `wide`
(1.1 s) 1.07x. Longer scenarios do not move, as expected for a cost of at most
100 ms per build. Against stock Lake, Blake's primary geomean is now 1.196x
(`noop` 1.58x, `leaf` 1.26x).

A no-op of 1000 modules now takes ~0.10-0.11 s: ~30 ms of process start and
workspace load, and ~70-80 ms of checking traces. That checking is mostly serial
(about 2x parallelism on 16 CPUs), presumably because each module's check waits
for its imports' checks as a chain of job continuations. Finding and shortening
that chain is the next lever for `noop` and `leaf`; it will matter more on deep
DAGs and on the `large-*` corpora. Separately, the ~25 ms between exec and the
first file access is the static binary initializing every Lean and Lake module.
