# critical-nice: compile modules off the critical path at a higher nice value

- **Date:** 2026-10-31
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** failure (no effect on the scenarios it targets; the harness's `success` comes from a `noop` result this change cannot cause, see below)
- **Branch / PR:** research/critical-nice-log (code: research/critical-nice-wip, https://github.com/adamtopaz/blake/tree/research/critical-nice-wip) — PR TBD
- **Result files:** research/results/20261031T202904Z-2e698309-3e49371.json

## Hypothesis

`hub` (1.10x vs stock Lake) and `cold` (1.10x) are the scenarios where Blake gains
least. On `medium-deep` both take ~33-35 s for a critical path of 85 modules, ~0.39 s
per step. An uncontended compile of a mid-chain module (`Corpus.M00500`, plain
`lean`, idle P-cores) takes ~0.28 s wall. So each step of the critical chain loses
~30-40% to contention with the other compiles running at the same time.

Lake starts a module's job as soon as its imports are done, and every `lean` competes
for CPU on equal terms. If modules with slack ran at a lower priority (a higher nice
value), the critical chain should get the CPU first whenever more threads are
runnable than there are CPUs. This is the usual critical-path-first list scheduling,
done by the OS: modules with slack are the short chains and leaves.

Also measured on the way, and not the problem: a compile's system time.
- On the harness's P-cores a compile spends ~43 ms in the kernel against ~350 ms
  of user time; the E-cores showed 250 ms.
- `strace -c` shows 3,500 `mmap`s of `.olean` files and cheap system calls.
- `MIMALLOC_ALLOW_LARGE_OS_PAGES=1` changes nothing. Lean's mimalloc 3.4.5 does
  read `MIMALLOC_*` options; the system's transparent huge pages are in `madvise`
  mode.

## What was done

- `c/blake_zygote.c`: a request has a new field after `LEAN_PATH`, a nice increment,
  which the child applies with `nice()` before it starts Lean (its threads inherit
  it).
- `Lake/Build/Actions.lean`:
  - `runLeanZygote` sends the increment;
  - `compileLeanModule` takes it (`nice`, default 0);
  - a registry holds each module's value (`setModuleNices`, `moduleNice`);
  - header-snapshot saves (`header-stub`) run at nice 10.
- `Lake/Build/Library.lean`: `recCollectLocalModules` records each module's local
  imports while it walks them, then computes each module's value (`moduleNices`).
- `Lake/Build/Module.lean`: `buildLean` passes its module's value.

Two versions of the value, both measured by hand. In both, 4-6 alternating hub
rebuilds of a scratch copy of `medium-deep` (CPUs 0-15), each after a harness-style
theorem added to the hub. Nice values sampled from `ps` 8 s into each build.

1. **By height** (the longest chain of the library's modules that import it): 0 for
   the greatest height, 10 for modules nothing imports, proportional in between.
   Children ran at nice 1-6. Candidate 32.65, 32.34, 32.60 s; `main` 32.53, 32.60,
   32.92 s.
2. **By slack** (how much shorter the longest import chain through the module is
   than the library's longest chain): 0 on a critical path, then 2 per module of
   slack, up to 19. Children ran at nice 0, 2 and 4 (5-6 at 0, 6-7 at 2). Candidate
   32.50, 32.46, 32.48 s; `main` 32.39, 32.67, 32.32 s.

No difference either way. The standard sweep measured the second version (commit
`3e49371`).

## Measurements

Standard sweep on `3e49371`: 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run.

```
speedup of main vs lake: cold=1.099x, noop=2.415x, leaf=1.810x, swarm=1.199x, hub=1.091x, primary_geomean=1.546x
speedup of candidate vs lake: cold=1.109x, noop=2.349x, leaf=1.742x, swarm=1.208x, hub=1.102x, primary_geomean=1.528x
speedup of candidate vs main: cold=1.009x, noop=0.972x, leaf=0.963x, swarm=1.008x, hub=1.010x, primary_geomean=0.988x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.002x over 1 runs, threshold 7%; medians main 33.83s, candidate 33.77s)
verdict medium-deep/noop: same (paired speedup 0.998x over 10 runs, threshold 7%; medians main 0.07s, candidate 0.08s)
verdict medium-deep/leaf: same (paired speedup 1.002x over 10 runs, threshold 7%; medians main 0.32s, candidate 0.34s)
verdict medium-deep/swarm: same (paired speedup 1.008x over 3 runs, threshold 3%; medians main 14.51s, candidate 14.41s)
verdict medium-deep/hub: same (paired speedup 1.008x over 3 runs, threshold 3%; medians main 33.33s, candidate 33.08s)
verdict medium-layered/cold: same (paired speedup 1.029x over 1 runs, threshold 7%; medians main 35.97s, candidate 34.97s)
verdict medium-layered/noop: same (paired speedup 0.987x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.09s)
verdict medium-layered/leaf: same (paired speedup 0.996x over 10 runs, threshold 7%; medians main 0.34s, candidate 0.34s)
verdict medium-layered/swarm: same (paired speedup 1.007x over 3 runs, threshold 3%; medians main 13.36s, candidate 13.20s)
verdict medium-layered/hub: same (paired speedup 1.008x over 3 runs, threshold 3%; medians main 31.81s, candidate 31.71s)
verdict medium-wide/cold: same (paired speedup 0.997x over 1 runs, threshold 7%; medians main 31.56s, candidate 31.64s)
verdict medium-wide/noop: improved (paired speedup 1.146x over 10 runs, threshold 7%; medians main 0.07s, candidate 0.06s)
verdict medium-wide/leaf: same (paired speedup 0.980x over 10 runs, threshold 7%; medians main 0.17s, candidate 0.18s)
verdict medium-wide/swarm: same (paired speedup 1.010x over 10 runs, threshold 7%; medians main 0.91s, candidate 0.91s)
verdict medium-wide/hub: same (paired speedup 1.010x over 3 runs, threshold 3%; medians main 32.48s, candidate 31.87s)
verdict: success
```

The one `improved` verdict, `medium-wide/noop`, cannot come from this change.
- A `noop` compiles nothing, so no nice value is ever used.
- The only new work on that path is computing the modules' values in
  `recCollectLocalModules`, which can only cost time.
- The two other corpora's `noop` were 0.998x and 0.987x.
- The runs (ms) show the usual spread of the harness's `noop`:

```
medium-wide noop main 64 91 88 64 73 63 59 80 71 85   cand 60 62 59 56 64 84 81 68 64 57
```

The scenarios the change targets did not move:
- `hub` 1.008x, 1.008x, 1.010x;
- `cold` 1.002x, 1.029x, 0.997x (one run each);
- the hand-timed hub rebuilds above.

So I record this as a failure and do not merge it as a `research:` PR: the code would
add a protocol field and a scheduling heuristic on the strength of a noise result.

## Conclusion

Lowering the priority of compiles off the critical path does not shorten `hub` or
`cold` builds on this machine.
- The `medium-deep` cold build uses 456 CPU-seconds in 33.8 s, ~13.5 of 16 logical
  CPUs on average.
- Those 16 are 8 physical P-cores with hyperthreading, so the critical chain loses
  its time to sharing a core (and memory bandwidth) with whatever runs next to it.
  CFS weights cannot help with that; they matter only when threads wait for a CPU.
- Even giving half the compiles nice 2-4 left the chain's per-step time unchanged.

For the next attempt at `hub`/`cold`:
- The 30-40% slowdown of a step under load is real, but it is hardware contention,
  not scheduling order.
- What can still help is less CPU work per compile, which mostly means inside
  `lean` (import ~35% of a deep module's run; already explored in
  `incr-header`/`header-stub` for edited modules).
- Fewer concurrent compiles, so the chain runs on a core of its own, would cost
  throughput; that trade-off was not tried.
