# bucket-store: keep the build store's entries in hash buckets, so an insertion stops copying a shared tree

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/2026-10-31-bucket-store.md
- **Scenarios improved:** noop (`layered`, `wide`); every build's job-graph construction

## Problem

Lake memoizes every job of a build in its build store, a
`Std.DTreeMap BuildKey (Job ∘ BuildData)` held in the `StateRefT'` reference of
`RecBuildT`. Job tasks capture that reference, because a module's imports are fetched
from job continuations on worker threads. So the reference, and every map stored into
it, is a multi-threaded object, and Lean never treats multi-threaded objects as
exclusive. Every insertion therefore:

- copied the map's whole path (~14 nodes, with ~14 name comparisons, for the ~10,000
  entries of a 1000-module library);
- marked the copies multi-threaded;
- freed the old path with atomic reference counts.

All of this happened inside the reference's take/set, while other threads waited. Even
lookups used `modifyGet`. In `perf` of a `noop` on `medium-deep`, this was the largest
part of the fetch thread: the thread that builds the job graph, on the critical path.
It was also why that thread sat idle about half the time.

## Mechanism

`BuildStore` (`Lake/Build/Store.lean`) is now a structure:

- `map`: the old tree, used when the store has no buckets (`{}`);
- `buckets`: a fixed array of 4096 `IO.Ref (List Entry)`, made by `BuildStore.new`.

Its `MonadDStore` instance (for `StateRefT' IO.RealWorld BuildStore`) picks a bucket
by the key's hash (`BuildKey` derives `Hashable`):

- `store` conses one entry (hash, key, job) onto that bucket;
- `fetch?` reads that bucket and compares hashes, then keys.

An insertion allocates one cell and holds only one small reference, so the other
threads rarely wait.

`RecBuildT.run'` (`Lake/Build/Fetch.lean`), which every Lake build goes through and
whose store is discarded afterwards, starts from `BuildStore.new`. Stores made
without buckets keep the old behavior: `RecBuildT.run` with a caller's store, and the
deprecated `collect*` functions (which read `map`). Memoization semantics are
unchanged: `fetchOrCreate` is still `fetch?` then `store`.

## Where it lives

- `Lake/Build/Store.lean`: `BuildStore`, `BuildStore.Entry`, `BuildStore.new`,
  `findEntry?`, the `MonadDStore` instance.
- `Lake/Build/Fetch.lean`: `RecBuildT.run'`.

## Measured gains

Standard sweep on machine `2e698309` (i9-14900K, 16 CPUs, 32 GB cap), result
`research/results/20261031T170303Z-2e698309-ce6e2f0.json`:

```
speedup of candidate vs main: cold=0.999x, noop=1.097x, leaf=1.020x, swarm=1.015x, hub=1.005x, primary_geomean=1.034x
```

`noop` improved on `layered` (1.096x) and `wide` (1.134x), and was 1.022x on `deep`.
By hand on `medium-deep` (40 pairs): 49.7 vs 45.1 ms. The benefit grows with the
number of jobs.

## Caveats and follow-ups

- 4096 buckets are allocated per build (~0.1 ms). Lists stay short up to ~70,000
  jobs (~17 per bucket). A much larger workspace would want more buckets, or
  buckets sized from the number of modules.
- `BuildStore` changed from a type abbreviation to a structure. Code outside Lake
  that treated it as a `DTreeMap` must use `.map` (none in Lake or its tests).
- Next on the fetch thread:
  - freeing and allocating objects;
  - per-module path and name strings;
  - trace captions, which `addPureTrace` formats on every build but which are read
    only when a trace file is written.
