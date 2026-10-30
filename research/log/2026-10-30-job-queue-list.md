# job-queue-list: register build jobs in O(1) instead of copying the whole job queue

- **Date:** 2026-10-30
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** success
- **Branch / PR:** research/job-queue-list — (pending)
- **Result files:** research/results/20261030T194423Z-2e698309-a53246d.json, research/results/20261030T203613Z-2e698309-a53246d.json (rerun with `--repeat 5 --repeat-fast 20`)

## Hypothesis

After `parallel-discovery`, the trace-check phase of a no-op build of
`medium-wide` (~50 ms of ~90 ms) runs only ~3.75 threads busy on average out of
16-30. `perf` per thread shows why: one thread, the one running
`LeanLib.recBuildLean`'s fold that creates every module's jobs (`fetch` is kept on
a single thread by design), is ~79% busy, and every other thread ~13%. The checks
wait for their jobs to be created.

In that thread, `lean_copy_expand_array` (57 self samples), `lean_del_core_other`
(56), and `lean_dec_ref_cold` (127) together took about a quarter of the samples.
The arrays being copied are the build's job queue: `registerJob`
(`Lake/Build/Job/Register.lean`) does `registeredJobs.modify (·.push job)` on an
`IO.Ref (Array OpaqueJob)` that the monitor thread also holds. A multi-threaded
object is never exclusive in Lean (`lean_is_exclusive` returns false unless the
object is single-threaded, `lean.h`), so every push copies the whole array,
incrementing every element's reference count atomically, and then frees the
old array. The monitor drains the queue only once per tick (up to 100 ms), and a
no-op of 1000 modules registers ~1000 jobs within one tick, so registration costs
~n²/2 = 500k element copies. It grows quadratically: ~32M at 8000 modules.

Change: make `JobQueue` an `IO.Ref (List OpaqueJob)`, newest first. Registering
conses (O(1), no copy), and the monitor's `drainQueue` reverses what it takes, so
it sees the jobs in the same registration order as before. The deprecated,
unused `monitorJobs` takes a `JobQueue` instead of the raw array ref.

Expected: a few ms off `noop` and `leaf` on the medium corpora (paired hand
measurement on `medium-wide`: 89.3 ms to 84.5 ms, 1.056x, possibly under the 7%
threshold), and much more on larger projects, since the cost is quadratic.
Nothing else changes. Measured with the standard sweep.

Before this, an earlier probe of this iteration (not committed, not measured with
the sweep): `Module.recFetchPreSetup` awaits each module's `precompileImports`
job on the fetch thread. Skipping that await when no library in the workspace is
precompiled (binding the job instead, so its errors and log are kept) changed
nothing (paired 1.005x on `medium-wide`), so the await is not a bottleneck.

## What was done

`Lake/Build/Context.lean`: `JobQueue := IO.Ref (List OpaqueJob)`, `mkJobQueue`
starts from `[]`. `Lake/Build/Job/Register.lean`: `registerJob` conses.
`Lake/Build/Run.lean`: `Monitor.drainQueue` takes the list and reverses it into an
array; the deprecated `monitorJobs` takes a `JobQueue`. Commit `a53246d`.

Afterwards, `lean_copy_expand_array` and `lean_del_core_other` are gone from the
top of the fetch thread's profile. What is left there is mostly
`lean_dec_ref_cold`, the build store's `DTreeMap` insert, `lean_mark_mt`, and
`BuildKey`/`Name` comparisons.

By hand (paired, alternating, CPUs 16-31, `LEAN_NUM_THREADS=16`): no-op of
`medium-wide` 89.3 ms to 84.5 ms (1.056x); no-op of `large-wide` (8000 modules,
generated from `bench/configs/large-wide.json` and built once with stock Lake)
673 ms to 620 ms (1.087x). That is less than n²/2 at 8000 jobs would suggest,
because the monitor drains the queue every 100 ms tick, which bounds how long it
grows. So the saving is roughly proportional to the registrations per tick.

Checks: `nix develop -c tests/run_all.sh --compare .out-main` 93 passed, 0 failed
(`main` also 93/93); quick harness `correctness of candidate: identical artifacts
to lake, all builds ok, break check ok`; held-out corpora identical too.

## Measurements

Standard sweep, 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; 30 minutes:

```
speedup of main vs lake: cold=1.003x, noop=1.983x, leaf=1.320x, swarm=1.034x, hub=1.000x, primary_geomean=1.283x
speedup of candidate vs lake: cold=1.002x, noop=2.059x, leaf=1.320x, swarm=1.029x, hub=1.010x, primary_geomean=1.296x
speedup of candidate vs main: cold=0.999x, noop=1.038x, leaf=1.000x, swarm=0.995x, hub=1.010x, primary_geomean=1.011x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.003x over 1 runs, threshold 7%; medians main 40.18s, candidate 40.05s)
verdict medium-deep/noop: improved (paired speedup 1.120x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.08s)
verdict medium-deep/leaf: same (paired speedup 1.010x over 10 runs, threshold 7%; medians main 0.44s, candidate 0.44s)
verdict medium-deep/swarm: same (paired speedup 1.012x over 3 runs, threshold 3%; medians main 15.61s, candidate 15.58s)
verdict medium-deep/hub: same (paired speedup 0.993x over 3 runs, threshold 3%; medians main 38.96s, candidate 38.80s)
verdict medium-layered/cold: same (paired speedup 1.022x over 1 runs, threshold 7%; medians main 43.29s, candidate 42.38s)
verdict medium-layered/noop: same (paired speedup 1.026x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-layered/leaf: same (paired speedup 1.013x over 10 runs, threshold 7%; medians main 0.46s, candidate 0.45s)
verdict medium-layered/swarm: same (paired speedup 1.006x over 3 runs, threshold 3%; medians main 15.14s, candidate 15.40s)
verdict medium-layered/hub: same (paired speedup 0.998x over 3 runs, threshold 3%; medians main 38.51s, candidate 38.72s)
verdict medium-wide/cold: same (paired speedup 0.974x over 1 runs, threshold 7%; medians main 38.42s, candidate 39.45s)
verdict medium-wide/noop: same (paired speedup 1.021x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-wide/leaf: same (paired speedup 0.977x over 10 runs, threshold 7%; medians main 0.24s, candidate 0.24s)
verdict medium-wide/swarm: same (paired speedup 0.998x over 10 runs, threshold 7%; medians main 1.10s, candidate 1.10s)
verdict medium-wide/hub: same (paired speedup 1.027x over 3 runs, threshold 3%; medians main 39.65s, candidate 38.42s)
verdict: success
```

Because one corpus carried the verdict (`medium-deep/noop` 1.120x while the
other two `noop`s were ~1.02x), I reran the standard sweep with `--repeat 5
--repeat-fast 20` (52 minutes):

```
speedup of main vs lake: cold=0.999x, noop=1.861x, leaf=1.311x, swarm=1.023x, hub=1.004x, primary_geomean=1.258x
speedup of candidate vs lake: cold=0.999x, noop=1.956x, leaf=1.324x, swarm=1.029x, hub=1.009x, primary_geomean=1.281x
speedup of candidate vs main: cold=1.000x, noop=1.051x, leaf=1.010x, swarm=1.005x, hub=1.005x, primary_geomean=1.018x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.016x over 1 runs, threshold 7%; medians main 40.91s, candidate 40.28s)
verdict medium-deep/noop: improved (paired speedup 1.090x over 20 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-deep/leaf: same (paired speedup 1.022x over 20 runs, threshold 7%; medians main 0.46s, candidate 0.45s)
verdict medium-deep/swarm: same (paired speedup 0.999x over 5 runs, threshold 3%; medians main 15.81s, candidate 15.70s)
verdict medium-deep/hub: same (paired speedup 1.008x over 5 runs, threshold 3%; medians main 39.06s, candidate 39.05s)
verdict medium-layered/cold: same (paired speedup 1.013x over 1 runs, threshold 7%; medians main 47.35s, candidate 46.76s)
verdict medium-layered/noop: same (paired speedup 1.069x over 20 runs, threshold 7%; medians main 0.09s, candidate 0.08s)
verdict medium-layered/leaf: same (paired speedup 1.034x over 20 runs, threshold 7%; medians main 0.48s, candidate 0.47s)
verdict medium-layered/swarm: same (paired speedup 1.014x over 5 runs, threshold 3%; medians main 16.46s, candidate 16.20s)
verdict medium-layered/hub: same (paired speedup 0.995x over 5 runs, threshold 3%; medians main 40.54s, candidate 40.35s)
verdict medium-wide/cold: same (paired speedup 0.972x over 1 runs, threshold 7%; medians main 40.85s, candidate 42.02s)
verdict medium-wide/noop: same (paired speedup 0.997x over 20 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-wide/leaf: same (paired speedup 0.980x over 20 runs, threshold 7%; medians main 0.25s, candidate 0.26s)
verdict medium-wide/swarm: same (paired speedup 1.003x over 20 runs, threshold 7%; medians main 1.21s, candidate 1.22s)
verdict medium-wide/hub: same (paired speedup 1.010x over 5 runs, threshold 3%; medians main 41.72s, candidate 41.31s)
verdict: success
```

## Conclusion

A real but small gain. Both standard runs say `success` because `noop` on
`medium-deep` improves by 9-12%, but `noop` on `medium-layered` is 1.03-1.07x and on
`medium-wide` 1.00-1.02x (the hand measurement on `wide` said 1.056x), and
nothing else moves. Primary geomean vs `main`: 1.011x and 1.018x. Correctness is
unaffected (the job set and order seen by the monitor are the same).

It is still worth having because it removes a superlinear cost: Lake registers
one job per module, so a build of 100k modules would register ~10k jobs per
monitor tick and copy ~50M array elements per tick. More generally, any
persistent structure that Lake keeps in an `IO.Ref` shared across threads is
copied on every update. Lean never treats a multi-threaded object as exclusive,
so an `Array` costs O(n) per push and a tree O(log n). This is worth looking for
elsewhere: the build store (a `DTreeMap`, ~15 nodes copied per insert) is the
largest remaining item on the fetch thread.

The fetch thread is still the bottleneck of a no-op (it creates every job while
the workers wait). The next lever is fewer store operations per module, or a
thread-safe `fetchOrCreate` so that more than one thread can create jobs.
