# bucket-store: keep the build store's entries in hash buckets, so an insertion stops copying a shared tree

- **Date:** 2026-10-31
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** success
- **Branch / PR:** research/bucket-store — TBD
- **Result files:** research/results/20261031T170303Z-2e698309-ce6e2f0.json

## Hypothesis

After `deferred-init`, the rest of a `noop` is mostly the fetch thread, which builds
the job graph. `perf record` (from `nix shell nixpkgs#perf`) of a `noop` on a
scratch copy of `medium-deep` showed:
- One thread with 26-27% of all samples, from the end of start-up to the end of
  the build. It was busy only about half of that window.
- The main thread at ~10% (start-up); each worker at 4-6%.

That hot thread's top symbols, in percent of all samples:

```
3.64  lean_dec_ref_cold
2.69  Std.DTreeMap.Internal.Impl.insert (recBuildWithIndex)
1.69  lean_mark_mt
1.34  mi_page_thread_collect_to_local
1.13  Lake.BuildKey.quickCmp
0.97  mi_free
0.84  lean_alloc_small_object_core
0.76  Lean.Name.quickCmpImpl
```

That is the build store: `Std.DTreeMap BuildKey (Job ∘ BuildData)` in a
`StateRefT'` reference (`RecBuildT`).
- **Why it is slow:** job tasks capture that reference (imports are fetched from job
  continuations on worker threads), so the reference and every map stored in it are
  multi-threaded objects. Lean never treats those as exclusive.
- **What each insert costs:** a `noop` of a 1000-module library makes ~10,000
  inserts. Each copies its whole path (~14 nodes, with ~14 `quickCmp` comparisons of
  names), marks the copies multi-threaded, and frees the old path with atomic
  reference counts.
- **Why it waits:** all of this happens inside the reference's take/set, while other
  threads that want the store wait. Even `fetch?` used `modifyGet`, a take and a set.
  That explains the fetch thread idling half its window.

Expected: a store whose insertion allocates one cell and touches one small reference
should shorten the fetch thread and `noop`, more on bigger libraries.

## What was done

`Lake/Build/Store.lean`:
- `BuildStore` is now a structure with the old `map` (default `{}`) and `buckets`:
  an array of 4096 `IO.Ref (List BuildStore.Entry)`, made by `BuildStore.new`.
- An `Entry` holds the key's hash, the key, and the job.
- The new `MonadDStore` instance for `StateRefT' IO.RealWorld BuildStore` indexes a
  bucket by the key's hash (`BuildKey` already derived `Hashable`):
  - `store` conses an entry onto that bucket;
  - `fetch?` reads the bucket and compares hashes, then keys.
- A store without buckets (`{}`, `BuildStore.empty`) uses `map` exactly as before,
  so `RecBuildT.run` with a caller's store and the deprecated `collect*` functions
  (now over `map`) keep their behavior.

`Lake/Build/Fetch.lean`: `RecBuildT.run'`, which every Lake build goes through
(`Workspace.startBuild`) and whose store is discarded afterwards, starts from
`BuildStore.new`.

The memoization semantics are unchanged: `fetchOrCreate` still does `fetch?` then
`store`, with the same (pre-existing) window in which two threads may create the
same key.

Measured by hand, `medium-deep` scratch copy, CPUs 0-15, 40 alternating pairs
against `main` at `1c94c20`:
- `noop` wall 49.7 vs 45.1 ms (paired 1.110x); CPU 142.9 vs 141.0 ms.
- In `perf`, the fetch thread's store symbols are gone (`findEntry?` 0.23%), and it
  is now busy ~80% of its window instead of ~50%. Less waiting for the reference.

Checks:
- `nix develop -c tests/run_all.sh --compare .out-main`: 93 passed, 0 failed (base
  on `1c94c20`: 93 passed).
- Quick harness: candidate artifacts identical to `lake`, all builds and the break
  check ok.
- Its verdict was `neutral`: `noop` 0.947x, 1.013x, 1.043x on the 200-module small
  corpora, whose ~2000-entry stores cost less.

## Measurements

Standard sweep on `ce6e2f0`: 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run.

```
speedup of main vs lake: cold=1.095x, noop=2.027x, leaf=1.766x, swarm=1.191x, hub=1.084x, primary_geomean=1.466x
speedup of candidate vs lake: cold=1.093x, noop=2.224x, leaf=1.800x, swarm=1.209x, hub=1.089x, primary_geomean=1.515x
speedup of candidate vs main: cold=0.999x, noop=1.097x, leaf=1.020x, swarm=1.015x, hub=1.005x, primary_geomean=1.034x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 0.995x over 1 runs, threshold 7%; medians main 35.71s, candidate 35.89s)
verdict medium-deep/noop: same (paired speedup 1.022x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.08s)
verdict medium-deep/leaf: same (paired speedup 1.029x over 10 runs, threshold 7%; medians main 0.36s, candidate 0.34s)
verdict medium-deep/swarm: same (paired speedup 0.992x over 3 runs, threshold 3%; medians main 14.31s, candidate 14.31s)
verdict medium-deep/hub: same (paired speedup 0.993x over 3 runs, threshold 3%; medians main 33.59s, candidate 33.63s)
verdict medium-layered/cold: same (paired speedup 1.016x over 1 runs, threshold 7%; medians main 37.82s, candidate 37.24s)
verdict medium-layered/noop: improved (paired speedup 1.096x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.08s)
verdict medium-layered/leaf: same (paired speedup 1.018x over 10 runs, threshold 7%; medians main 0.35s, candidate 0.34s)
verdict medium-layered/swarm: same (paired speedup 1.017x over 3 runs, threshold 3%; medians main 13.97s, candidate 13.64s)
verdict medium-layered/hub: same (paired speedup 0.986x over 3 runs, threshold 3%; medians main 32.05s, candidate 32.12s)
verdict medium-wide/cold: same (paired speedup 0.986x over 1 runs, threshold 7%; medians main 31.58s, candidate 32.01s)
verdict medium-wide/noop: improved (paired speedup 1.134x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.08s)
verdict medium-wide/leaf: same (paired speedup 1.013x over 10 runs, threshold 7%; medians main 0.18s, candidate 0.18s)
verdict medium-wide/swarm: same (paired speedup 1.020x over 10 runs, threshold 7%; medians main 0.91s, candidate 0.90s)
verdict medium-wide/hub: same (paired speedup 1.015x over 3 runs, threshold 3%; medians main 32.08s, candidate 31.51s)
verdict: success
```

`noop` runs (ms), the two tools interleaved:

```
medium-deep    main 68 75 81 72 72 82 83 79 85 86    cand 71 77 77 86 71 83 80 69 82 67
medium-layered main 84 82 88 92 91 82 82 78 96 96    cand 78 80 74 67 82 85 79 79 79 80
medium-wide    main 91 76 92 93 101 88 81 96 77 83   cand 76 86 62 66 78 82 90 104 70 70
```

Held-out check: `correctness of candidate: identical artifacts to lake, all builds
ok, break check ok`.

## Conclusion

A cheaper store shortens the fetch thread and with it `noop`. The gain is about 10%
by hand on `medium-deep` (49.7 to 45.1 ms), improved in the harness on `layered`
(1.096x) and `wide` (1.134x), and 1.022x on `deep`, where `noop` is noisiest. Other
scenarios move by 1-3%: every build fetches the whole graph. The gain grows with the
number of jobs: the old insertion cost grew with log(entries) and the time other
threads waited for the reference grew with it. So the benefit should be larger on big
libraries, where 4096 buckets still keep each list short (~17 entries for 70,000
jobs).

What is left on the fetch thread (`perf`, after the change):
- freeing and allocating objects (`lean_dec_ref_cold` 2.9%, allocator ~3%);
- building paths and names per module on every build (`FilePath` separator search,
  `modToFilePath`, `Name.toString` for trace captions in `recFetchPreSetup`):
  together ~3%;
- job plumbing (`Job.bindM`, `lean_task_map_core`).

Trace captions (`addPureTrace` formats `toString` of each value) are built on every
`noop` but read only when a trace file is written; making them lazy would take
another slice.
