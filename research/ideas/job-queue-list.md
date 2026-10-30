# job-queue-list: register build jobs in O(1) instead of copying the whole job queue

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/2026-10-30-job-queue-list.md
- **Scenarios improved:** noop (on `medium-deep`; small elsewhere)

## Problem

Lake's `fetch` runs on a single thread by design, and in a no-op build that
thread (the fold in `LeanLib.recBuildLean` that creates every module's jobs) is
the bottleneck: ~79% busy while the workers checking traces sit at ~13% each
(`perf`, `medium-wide`). About a quarter of its samples were
`lean_copy_expand_array`, `lean_del_core_other`, and `lean_dec_ref_cold`.

Each job the build registers for the monitor is pushed onto the build's job
queue, an `IO.Ref (Array OpaqueJob)` that the monitor thread also holds. Lean
never treats a multi-threaded object as exclusive (`lean_is_exclusive` is false
unless the object is single-threaded), so `Array.push` on it always copies the
whole array, atomically incrementing every job's reference count, and the old
array is then freed, decrementing them again. The monitor drains the queue once
per 100 ms tick, so registering `n` jobs within a tick costs ~n²/2 element
copies.

## Mechanism

`JobQueue` is now an `IO.Ref (List OpaqueJob)`, newest job first. `registerJob`
conses onto it, which allocates one cell and copies nothing. `Monitor.drainQueue`
takes the list and reverses it into an array, so the monitor sees the jobs in
exactly the order they were registered, as before. Nothing else changes.

The general lesson: any persistent structure kept in an `IO.Ref` that crosses
threads is copied on every update. An `Array` there costs O(n) per push, a tree
O(log n), a list cons O(1).

## Where it lives

`Lake/Build/Context.lean` (`JobQueue`, `mkJobQueue`),
`Lake/Build/Job/Register.lean` (`registerJob`), `Lake/Build/Run.lean`
(`Monitor.drainQueue`; the deprecated `monitorJobs` now takes a `JobQueue`).

## Measured gains

Two standard sweeps on machine `2e698309` (i9-14900K, 16 CPUs, 32 GB cap), medium
corpora, results `research/results/20261030T194423Z-2e698309-a53246d.json` and
(rerun, `--repeat 5 --repeat-fast 20`) `research/results/20261030T203613Z-2e698309-a53246d.json`:

```
speedup of candidate vs main: cold=0.999x, noop=1.038x, leaf=1.000x, swarm=0.995x, hub=1.010x, primary_geomean=1.011x
speedup of candidate vs main: cold=1.000x, noop=1.051x, leaf=1.010x, swarm=1.005x, hub=1.005x, primary_geomean=1.018x
```

`noop` improved on `medium-deep` in both (paired 1.120x, then 1.090x over 20
pairs); `medium-layered` 1.026x / 1.069x and `medium-wide` 1.021x / 0.997x. A
small gain at 1000 modules that grows with the number of jobs per monitor tick.

By hand (paired, alternating, CPUs 16-31, `LEAN_NUM_THREADS=16`): no-op of
`medium-wide` (1000 modules) 89.3 ms to 84.5 ms (1.056x); no-op of `large-wide`
(8000 modules) 673 ms to 620 ms (1.087x).

## Caveats and follow-ups

- The fetch thread is still the bottleneck of a no-op. What remains in it is
  mostly the build store, a `DTreeMap` in a multi-threaded `IO.Ref`: every insert
  copies the ~15-node path (never exclusive, as above), frees the old one, and
  marks the new nodes and jobs multi-threaded; and the comparisons of
  `BuildKey`s by name. A hash map would be worse (it would copy its whole bucket
  array per insert). Fewer store operations per module, or letting more than one
  thread fetch with a thread-safe `fetchOrCreate`, are the levers left.
