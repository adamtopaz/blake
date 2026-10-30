# olean-githash: read the toolchain's githash from `Init.olean` instead of running `lean --githash`

- **Date:** 2026-10-30
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** success
- **Branch / PR:** research/olean-githash — (pending)
- **Result files:** research/results/20261030T121031Z-2e698309-4923feb.json

## Hypothesis

Blake's `noop` is ~0.04-0.05 s slower than stock Lake's (baseline entry). An
`strace -f -tt` of a Blake no-op build of `medium-wide` shows why: before it
opens the lakefile, Blake runs two `lean` subprocesses one after the other, because
its binary is not installed next to `lean` (`Lake/Config/InstallPath.lean`):

```
+0 ms   exec .lake/build/bin/lake build
+25 ms  exec lean --print-prefix            (elan proxy, then the real lean)
+68 ms  exit
+72 ms  exec <sysroot>/bin/lean --githash
+96 ms  exit
+98 ms  open lakefile.toml
```

Stock `lake`, which sits next to `lean`, opens the lakefile ~28 ms after exec and
spawns nothing. Unstraced, `lean --print-prefix` through elan costs ~35 ms and
`lean --githash` ~30 ms here; nearly all of it is `lean` initializing all of Lean
before parsing its arguments.

The second subprocess can go. Every `.olean` header records the githash of the
Lean that wrote it (bytes 40-79 after `olean`, a version byte, a flags byte, and a
33-byte version string), and `lean` refuses to import an `.olean` whose header
githash or version differs from its own (checked: patching one byte of either
gives `failed to read file ..., incompatible header`). So in any toolchain that
can elaborate anything, `<sysroot>/lib/lean/Init.olean` carries exactly the
githash `lean --githash` prints. Reading 80 bytes replaces a 30 ms process.

To stay conservative, the header is trusted only when it is well formed and its
githash equals Blake's own compiled-in `Lean.githash` (the normal case: Blake run
with the toolchain it was built with). In every other case, such as a different
toolchain, a dev build, or an unreadable file, Blake runs `lean --githash` as before.
Expected: `noop` ~0.03 s faster on every corpus (~14%, above the 7% threshold),
`leaf` ~5% faster (likely under the threshold), everything else unchanged.
Measured with the standard sweep.

## What was done

`Lake/Config/InstallPath.lean`: `LeanInstall.get` gained a helper,
`initOleanHasLeanGithash`, which reads the first 80 bytes of
`<sysroot>/lib/lean/Init.olean` and returns true when they start with `olean` and
format version 2 and bytes 40-79 equal Blake's `Lean.githash` (which must be
40 characters long). In that case the githash is `Lean.githash` and no process is
started; otherwise `lean --githash` runs as before. The co-located path and
`LEAN_GITHASH` overrides are untouched. After the change `strace -f -e execve` of
a no-op build shows only the `lean --print-prefix` process, `lake env printenv
LEAN_GITHASH` prints the same hash, and an existing build stays up to date (the
trace input did not change).

A hand-run paired no-op comparison against `main`'s binary on `medium-wide`
(30 alternating pairs, CPUs 16-31) gave a median speedup of 1.16x before the sweep.

Checks: `tests/run_all.sh --compare .out-main` 92 passed, 1 failed
(`examples/reverse-ffi`, which also fails on `main` here: `make` is not installed);
the quick harness printed `correctness of candidate: identical artifacts to lake,
all builds ok, break check ok`.

## Measurements

Standard sweep, 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run,
29 minutes.

```
speedup of main vs lake: cold=1.002x, noop=0.802x, leaf=0.971x, swarm=0.989x, hub=0.998x, primary_geomean=0.936x
speedup of candidate vs lake: cold=1.012x, noop=0.891x, leaf=1.020x, swarm=0.998x, hub=1.001x, primary_geomean=0.976x
speedup of candidate vs main: cold=1.010x, noop=1.111x, leaf=1.051x, swarm=1.008x, hub=1.003x, primary_geomean=1.042x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.006x over 1 runs, threshold 7%; medians main 35.71s, candidate 35.51s)
verdict medium-deep/noop: improved (paired speedup 1.117x over 10 runs, threshold 7%; medians main 0.22s, candidate 0.20s)
verdict medium-deep/leaf: same (paired speedup 1.065x over 10 runs, threshold 7%; medians main 0.51s, candidate 0.48s)
verdict medium-deep/swarm: same (paired speedup 0.995x over 3 runs, threshold 3%; medians main 15.41s, candidate 15.30s)
verdict medium-deep/hub: same (paired speedup 1.003x over 3 runs, threshold 3%; medians main 34.70s, candidate 34.60s)
verdict medium-layered/cold: same (paired speedup 1.007x over 1 runs, threshold 7%; medians main 36.35s, candidate 36.08s)
verdict medium-layered/noop: improved (paired speedup 1.122x over 10 runs, threshold 7%; medians main 0.21s, candidate 0.19s)
verdict medium-layered/leaf: same (paired speedup 1.036x over 10 runs, threshold 7%; medians main 0.61s, candidate 0.59s)
verdict medium-layered/swarm: same (paired speedup 1.002x over 3 runs, threshold 3%; medians main 13.10s, candidate 13.08s)
verdict medium-layered/hub: same (paired speedup 1.007x over 3 runs, threshold 3%; medians main 33.13s, candidate 32.75s)
verdict medium-wide/cold: same (paired speedup 1.018x over 1 runs, threshold 7%; medians main 33.50s, candidate 32.89s)
verdict medium-wide/noop: improved (paired speedup 1.081x over 10 runs, threshold 7%; medians main 0.19s, candidate 0.18s)
verdict medium-wide/leaf: same (paired speedup 1.060x over 10 runs, threshold 7%; medians main 0.39s, candidate 0.37s)
verdict medium-wide/swarm: same (paired speedup 1.015x over 10 runs, threshold 7%; medians main 1.10s, candidate 1.08s)
verdict medium-wide/hub: same (paired speedup 0.995x over 3 runs, threshold 3%; medians main 33.10s, candidate 33.27s)
verdict: success
```

## Conclusion

It worked as predicted: `noop` is 8-12% faster on every corpus (about 0.02-0.03 s),
`leaf` 4-6% faster (under the 7% threshold, so `same`), and nothing else moved.
Blake's `noop` gap to stock Lake shrank from 0.80x to 0.89x.

The rest of the gap is the other startup process, `lean --print-prefix` through
elan (~35 ms unstraced: ~10 ms elan, ~25 ms `lean` initializing before it reads
its arguments). Removing it needs the toolchain elan would choose for the current
directory without asking elan or `lean`; see the caveats in
`research/ideas/olean-githash.md`. Beyond startup, a no-op build of 1000 modules
still spends ~0.13 s parsing module headers and checking traces, which is where
stock Lake's own cost is and what will dominate at 100k modules.
