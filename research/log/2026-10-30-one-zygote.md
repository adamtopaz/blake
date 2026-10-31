# one-zygote: one `blake-zygote` per build, forking a child for every compile concurrently

- **Date:** 2026-10-30
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** success
- **Branch / PR:** research/one-zygote — (pending)
- **Result files:** research/results/20261031T034724Z-2e698309-2fc8bd6.json (commit `2fc8bd6`, before the fix: failure), research/results/20261031T043239Z-2e698309-216c449.json (commit `216c449`, the PR's code: success)

## Hypothesis

`fork-lean` keeps a pool of zygotes, one per concurrent compile, and each zygote
runs one child at a time. A build that compiles many modules at once therefore
starts up to 16 zygotes, each paying `exec`, dynamic linking, and Lean's
initialization (~25-30 ms). Its standard sweeps showed `swarm` on `medium-wide`
(24 modules at once, ~1.1 s) at 0.97-0.98x, which is plausibly those 16
initializations on the critical path of the first wave of compiles. Also, the
early-started zygote served only the first compile.

Change: one zygote per build. It reads requests tagged with an id, forks a child
for each one as soon as it arrives (single-threaded: `poll` on stdin and on a
`SIGCHLD` self-pipe), and replies `<id> <exit code>` when that child exits. Lake
starts it once (the early start in `LeanLib.recBuildLean` now always serves the
build) and keeps the waiting compiles in a map of promises, resolved by one reader
task. Requests are written under a mutex. If the zygote dies, every pending
compile falls back to running `lean`.

Expected: `swarm` on `wide` recovers its 2-3%, `leaf` is unchanged or slightly
better, and `hub`/`cold` drop ~15 zygote initializations per build (negligible CPU).

## What was done

`c/blake_zygote.c`: the request/reply loop rewritten as above (fork is
~0.2-0.5 ms: 64 forks, median 0.19 ms, under `strace -T`). `Lake/Build/Actions.lean`:
`Zygote` (child, write lock, pending promises, id counter, dead flag, reader task),
`getZygote` (start once, under a lock), `readZygoteReplies`, `runLeanZygote`,
`prestartZygote`, and `stopZygotes` (send the stop request, wait for the reader
and the process). A protocol test with three concurrent requests returned three
replies with byte-identical outputs. A swarm build of 24 modules started exactly
one zygote, and none was left afterwards.

But paired timing against `main` (the pool) on the scratch `medium-wide` copy
(CPUs 16-31, `LEAN_NUM_THREADS=16`) went the wrong way for concurrent work:

- 24-module swarm: pool 1715 ms, one zygote 1831 ms, paired **0.942x** (20 pairs);
  an earlier run gave 0.982x (10 pairs).
- leaf: 473 ms vs 449 ms, paired 1.057x (16 pairs).
- noop: 85 ms vs 85 ms, 0.993x.

Lake's own job durations for the 27 compiles of such a swarm build, three rounds
each: pool mean 765/764/740 ms, one zygote mean 998/972/987 ms (max ~1.4 vs 1.5 s).
So each compile was ~30% slower in this version. My first guess was kernel
contention: children forked from one parent share its memory copy-on-write, and
their concurrent page faults go through shared reverse-mapping (`anon_vma`) locks.
The first standard sweep (below) showed the real cause: **peak memory 19.8 GB** in
the cold build of `medium-wide`, against ~1.6 GB for every other tool. Far more
than 16 compiles were running at once.

Each compile waited for its reply with `IO.wait` on a promise. When a worker blocks
in a task wait, Lean's task manager starts another worker to make up for it, so
Lake's job limit (`LEAN_NUM_THREADS`) no longer bounded the number of compiles. The
pool design never hit this, because it blocked in OS-level pipe reads, which the
task manager does not see.

Fix (second commit): compiles wait on a `Std.Condvar` (with its `BaseMutex`), which
the reply reader signals after resolving a promise. These are OS-level waits, which
the task manager does not compensate for. After the fix, sampling
`/proc/<zygote>/task/*/children` every 50 ms during a 44 s hub rebuild of the
scratch corpus never saw more than 16 compile children (881 samples). The held-out corpora are byte-identical too (`identical artifacts to lake, all builds ok, break check ok`). Paired
timing against `main` became: 24-module swarm 1.055x (16 pairs), leaf 1.050x (12
pairs).

Lesson for Lake code in general: a Lake job must not block in `IO.wait`, `Task.get`,
or `IO.waitAny` on a task that is not itself a build job, or the build's concurrency
cap silently stops holding.

## Measurements

Standard sweeps, 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest. Both
commits passed `nix develop -c tests/run_all.sh --compare .out-main` (93/93) and
the quick harness (`identical artifacts to lake, all builds ok, break check ok`).

Commit `2fc8bd6` (waiting with `IO.wait`; the cold build of `medium-wide` peaked at
19.8 GB):

```
speedup of main vs lake: cold=1.027x, noop=2.067x, leaf=1.370x, swarm=1.070x, hub=1.014x, primary_geomean=1.324x
speedup of candidate vs lake: cold=1.032x, noop=2.006x, leaf=1.362x, swarm=1.099x, hub=0.995x, primary_geomean=1.314x
speedup of candidate vs main: cold=1.005x, noop=0.970x, leaf=0.994x, swarm=1.027x, hub=0.981x, primary_geomean=0.993x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.057x over 1 runs, threshold 7%; medians main 39.60s, candidate 37.46s)
verdict medium-deep/noop: same (paired speedup 1.040x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.08s)
verdict medium-deep/leaf: same (paired speedup 1.009x over 10 runs, threshold 7%; medians main 0.43s, candidate 0.42s)
verdict medium-deep/swarm: same (paired speedup 0.998x over 3 runs, threshold 3%; medians main 15.54s, candidate 15.10s)
verdict medium-deep/hub: improved (paired speedup 1.038x over 3 runs, threshold 3%; medians main 38.93s, candidate 37.51s)
verdict medium-layered/cold: same (paired speedup 1.010x over 1 runs, threshold 7%; medians main 43.37s, candidate 42.95s)
verdict medium-layered/noop: same (paired speedup 1.003x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-layered/leaf: same (paired speedup 0.984x over 10 runs, threshold 7%; medians main 0.45s, candidate 0.46s)
verdict medium-layered/swarm: same (paired speedup 1.029x over 3 runs, threshold 3%; medians main 15.57s, candidate 14.98s)
verdict medium-layered/hub: same (paired speedup 1.008x over 3 runs, threshold 3%; medians main 38.67s, candidate 38.36s)
verdict medium-wide/cold: same (paired speedup 0.952x over 1 runs, threshold 7%; medians main 40.79s, candidate 42.84s)
verdict medium-wide/noop: regressed (paired speedup 0.921x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.09s)
verdict medium-wide/leaf: same (paired speedup 0.991x over 10 runs, threshold 7%; medians main 0.24s, candidate 0.24s)
verdict medium-wide/swarm: same (paired speedup 1.005x over 10 runs, threshold 7%; medians main 1.18s, candidate 1.17s)
verdict medium-wide/hub: regressed (paired speedup 0.936x over 3 runs, threshold 3%; medians main 38.30s, candidate 42.49s)
verdict: failure
```

Commit `216c449` (waiting on a condition variable; the PR's code):

```
speedup of main vs lake: cold=1.021x, noop=1.947x, leaf=1.314x, swarm=1.038x, hub=1.024x, primary_geomean=1.284x
speedup of candidate vs lake: cold=1.075x, noop=2.016x, leaf=1.299x, swarm=1.097x, hub=1.094x, primary_geomean=1.331x
speedup of candidate vs main: cold=1.053x, noop=1.035x, leaf=0.989x, swarm=1.057x, hub=1.069x, primary_geomean=1.037x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.066x over 1 runs, threshold 7%; medians main 42.94s, candidate 40.28s)
verdict medium-deep/noop: same (paired speedup 0.991x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.08s)
verdict medium-deep/leaf: same (paired speedup 1.006x over 10 runs, threshold 7%; medians main 0.41s, candidate 0.42s)
verdict medium-deep/swarm: improved (paired speedup 1.031x over 3 runs, threshold 3%; medians main 15.82s, candidate 15.35s)
verdict medium-deep/hub: improved (paired speedup 1.054x over 3 runs, threshold 3%; medians main 39.21s, candidate 37.20s)
verdict medium-layered/cold: same (paired speedup 1.048x over 1 runs, threshold 7%; medians main 43.08s, candidate 41.11s)
verdict medium-layered/noop: same (paired speedup 1.041x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.08s)
verdict medium-layered/leaf: same (paired speedup 0.983x over 10 runs, threshold 7%; medians main 0.44s, candidate 0.44s)
verdict medium-layered/swarm: improved (paired speedup 1.079x over 3 runs, threshold 3%; medians main 15.56s, candidate 14.60s)
verdict medium-layered/hub: improved (paired speedup 1.067x over 3 runs, threshold 3%; medians main 39.10s, candidate 36.35s)
verdict medium-wide/cold: same (paired speedup 1.045x over 1 runs, threshold 7%; medians main 39.63s, candidate 37.93s)
verdict medium-wide/noop: same (paired speedup 0.978x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.08s)
verdict medium-wide/leaf: same (paired speedup 0.988x over 10 runs, threshold 7%; medians main 0.23s, candidate 0.24s)
verdict medium-wide/swarm: improved (paired speedup 1.080x over 10 runs, threshold 7%; medians main 1.15s, candidate 1.07s)
verdict medium-wide/hub: improved (paired speedup 1.068x over 3 runs, threshold 3%; medians main 39.46s, candidate 36.61s)
verdict: success
```

Cold-build CPU seconds and peak memory, candidate vs `main`, second sweep:
deep 532.4 vs 573.7 s (1389 vs 1558 MB), layered 625.7 vs 664.0 s (1743 vs
1930 MB), wide 583.5 vs 606.7 s (1460 vs 1641 MB).

## Conclusion

One zygote per build is clearly better than a pool, once it respects Lake's
concurrency limit. `swarm` and `hub` improved on all three corpora (`swarm`
1.031-1.080x, `hub` 1.054-1.068x), `cold` is 1.045-1.066x faster with 4-7% less
CPU and less memory, and `leaf`/`noop` are unchanged. Against stock Lake the
primary geomean is now 1.331x (`hub` 1.094x, `swarm` 1.097x, `cold` 1.075x).

Why it beats the pool by more than the ~15 saved zygote initializations: all
compiles are now children of one process, so the pages of Lean's initialized
state (parser tables, environment extension registries, ...), which they mostly
read, are the same physical pages for every compile and stay warm in the shared
caches. With 16 zygotes, every compile slot had its own copy. (Inferred from the
CPU and memory numbers, not measured directly.)

Two things for whoever touches this next:

- **A Lake job must never block in `IO.wait`/`Task.get` on a non-job task.** The task
  manager compensates for the blocked worker by starting another, and the build's
  concurrency cap silently disappears (here: 19.8 GB peak and a `failure`
  verdict). Use OS-level waits (`Std.Condvar`, pipe reads) instead.
- The zygote is single-threaded (`poll` on stdin plus a `SIGCHLD` self-pipe), so
  forking from it is safe; forks take ~0.2-0.5 ms each.

Follow-ups: the early start still costs a scan of every module's source and
trace mtimes; Windows still only gets a placeholder zygote (see
`research/ideas/fork-lean.md`); and the per-child cost of `fork` plus
copy-on-write faults remains.
