# early-edited: find edited modules at the start of a build; start the zygote and their jobs first

- **Date:** 2026-10-31
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** failure
- **Branch / PR:** research/early-edited-log (code: research/early-edited-wip, https://github.com/adamtopaz/blake/tree/research/early-edited-wip) — (pending)
- **Result files:** research/results/20261031T061610Z-2e698309-58f8adf.json

## Hypothesis

With `one-zygote`, a leaf build compiles its one module in a child of the build's
zygote. `strace -f -ttt` of a leaf build on the scratch `medium-wide` copy
(times inflated by strace) showed the order of events: the zygote was started
127 ms into the build, and it forked the compile 34 ms later, i.e. after its own
initialization. The early start was triggered from `LeanLib.recBuildLean`, after
module discovery, at about the same moment the compile request arrived, so the
zygote's initialization sat on the critical path. In addition, the edited module's
job is created in collection order, behind the trace checks of the modules before
it (the `dirty-first` attempt).

Change: as soon as `recCollectLocalModules` knows the library's root modules
(before any header is read), it starts a background scan for roots whose source is
newer than their `.trace`. If there are any, the scan starts the build's zygote
immediately, and its result is kept per library. `recBuildLean` then fetches the
edited modules' `leanArts` first (memoized; the fold still mixes all jobs in the
original order, so the library's trace does not change).

Why retry `dirty-first`: on its own it was neutral, because the early `lean` start
then paid `lean`'s own ~25-30 ms start-up anyway. With a zygote that is already
initialized when the edited module's job runs, starting that job first should
translate directly into an earlier compile.

Expected: `leaf` a few tens of ms faster on every corpus; `noop` unchanged (the
scan finds nothing and starts nothing); other scenarios unchanged or slightly
better.

## What was done

`Lake/Build/Library.lean`: `sourceNewerThanTrace` is a top-level private def;
`editedModulesRef : IO.Ref (NameMap (Task (Array Module)))`;
`recCollectLocalModules` starts the scan (a dedicated `BaseIO.asTask`) right after
`getModuleArray`, calling `prestartZygote` if it finds edited modules;
`recBuildLean` waits for the library's scan and fetches its modules first (it
replaces `one-zygote`'s scan in `recBuildLean`).

On the scratch corpus under strace, the zygote now starts at 90 ms (was 127) and
forks the compile at 120 ms (was 162). Paired timing against `main` (CPUs 16-31,
`LEAN_NUM_THREADS=16`): leaf of a heavier module 486 vs 451 ms, 1.075x (16 pairs);
noop 84 vs 84 ms, 1.010x (20 pairs).

Checks: `nix develop -c tests/run_all.sh --compare .out-main` 93 passed, 0 failed;
quick harness `correctness of candidate: identical artifacts to lake, all builds
ok, break check ok`. Commit `58f8adf` on `research/early-edited-wip`.

## Measurements

Standard sweep, 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run.

```
speedup of main vs lake: cold=1.077x, noop=2.012x, leaf=1.385x, swarm=1.116x, hub=1.079x, primary_geomean=1.353x
speedup of candidate vs lake: cold=1.090x, noop=2.017x, leaf=1.408x, swarm=1.102x, hub=1.100x, primary_geomean=1.362x
speedup of candidate vs main: cold=1.011x, noop=1.002x, leaf=1.017x, swarm=0.987x, hub=1.019x, primary_geomean=1.006x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 0.997x over 1 runs, threshold 7%; medians main 35.16s, candidate 35.28s)
verdict medium-deep/noop: same (paired speedup 0.967x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-deep/leaf: same (paired speedup 0.992x over 10 runs, threshold 7%; medians main 0.43s, candidate 0.44s)
verdict medium-deep/swarm: same (paired speedup 1.000x over 3 runs, threshold 3%; medians main 14.32s, candidate 14.32s)
verdict medium-deep/hub: same (paired speedup 1.000x over 3 runs, threshold 3%; medians main 34.29s, candidate 34.16s)
verdict medium-layered/cold: same (paired speedup 1.035x over 1 runs, threshold 7%; medians main 38.52s, candidate 37.23s)
verdict medium-layered/noop: same (paired speedup 1.002x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-layered/leaf: same (paired speedup 1.045x over 10 runs, threshold 7%; medians main 0.44s, candidate 0.43s)
verdict medium-layered/swarm: same (paired speedup 0.988x over 3 runs, threshold 3%; medians main 13.26s, candidate 13.41s)
verdict medium-layered/hub: same (paired speedup 1.030x over 3 runs, threshold 3%; medians main 34.22s, candidate 33.25s)
verdict medium-wide/cold: same (paired speedup 1.003x over 1 runs, threshold 7%; medians main 33.60s, candidate 33.49s)
verdict medium-wide/noop: same (paired speedup 1.008x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-wide/leaf: same (paired speedup 1.004x over 10 runs, threshold 7%; medians main 0.24s, candidate 0.23s)
verdict medium-wide/swarm: same (paired speedup 0.994x over 10 runs, threshold 7%; medians main 1.00s, candidate 1.03s)
verdict medium-wide/hub: same (paired speedup 1.030x over 3 runs, threshold 3%; medians main 34.38s, candidate 33.53s)
verdict: neutral
```

## Conclusion

No effect in the harness. `leaf` moved by -0.8% to +4.5% (under the 7% threshold),
and nothing else moved beyond noise. The 1.075x I measured by hand was on a
scratch module that I had made much heavier than the corpus's leaves. In the
harness's leaf builds, the zygote's initialization apparently already overlaps
enough of the trace checks, so starting it ~35 ms earlier, and creating the
edited module's job first, does not shorten the critical path. Together with
`dirty-first`, this suggests the order in which Lake creates jobs is not what
limits `leaf`.

By the same token, the remaining cost of a `leaf` build is: ~35 ms of process start
(module initializers), ~40-50 ms of module discovery plus trace checking (the fetch
thread creating ~10 jobs per module), and the compile itself (import and
elaboration inside `lean`'s driver). A faster `leaf` needs either a cheaper
job graph or a cheaper compile.

Also visible in this sweep: `main` itself (with `one-zygote`) now stands at a primary
geomean of 1.353x against stock Lake (`noop` 2.012x, `leaf` 1.385x, `swarm` 1.116x,
`hub` 1.079x, `cold` 1.077x).
