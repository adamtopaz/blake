# elan-sysroot: find the toolchain elan would pick without running `lean --print-prefix`

- **Date:** 2026-10-30
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** success
- **Branch / PR:** research/elan-sysroot — (pending)
- **Result files:** research/results/20261030T134734Z-2e698309-f249de8.json

## Hypothesis

After `olean-githash`, Blake's only remaining per-invocation subprocess is
`lean --print-prefix`, which it runs because it is not installed next to `lean`.
`lean` on `PATH` is elan's proxy, so this costs ~10 ms of elan resolving the
toolchain plus ~25 ms of `lean` initializing all of Lean before it reads its
arguments, all on the critical path before the lakefile is opened. Stock Lake
pays none of it. That subprocess is the rest of Blake's `noop` gap to Lake
(0.891x after `olean-githash`).

Elan's choice of toolchain follows fixed rules (elan v4.2.1,
`Cfg::find_override_toolchain_or_default` and `lookup_unresolved_toolchain_desc`),
which were read from its source for this entry: `ELAN_TOOLCHAIN` if set; else,
walking up from the canonical working directory, the first directory override in
`$ELAN_HOME/settings.toml`, `lean-toolchain` file (first line, trimmed), or
`leanpkg.toml`; else the default toolchain. A `lean-toolchain` name is tried first
as a path to a toolchain directory (relative to the file), then as a linked
toolchain named after its release, then as `origin:release`, which lives in
`$ELAN_HOME/toolchains/<origin with / as -->---<release>` and is installed if
missing. `lean --print-prefix` then prints the directory above the real location
of that toolchain's `bin/lean`.

So in the unambiguous case Blake can read the answer off the file system. It does
so only when **all** of these hold, and otherwise runs `lean --print-prefix` as
before: the platform is not Windows; `LEAN` and `LEAN_SYSROOT` are unset (the
existing code handles those); `ELAN_TOOLCHAIN` is unset or empty; the first
`lean` on `PATH` is the same file as the `elan` beside it (same real path, or a
hard link with the same size and mtime); `ELAN_HOME` (or `$HOME/.elan`) has a
`settings.toml` with no directory overrides; walking up from the canonical
working directory, no `leanpkg.toml` comes before the nearest `lean-toolchain`;
that file pins a release `owner/repo:v<digit>...` (no channels, bare names, or
paths); nothing exists at `<dir>/<name>` (a path toolchain) and no
`toolchains/<release>` entry exists (a linked toolchain); the toolchain's
`bin/lean` is installed; and its `Init.olean` records Blake's own `Lean.githash`
(as in `olean-githash`), so the fast path only ever selects the very Lean that
Blake was built with.

Expected: Blake's `noop` drops by ~0.03 s on every corpus (~15% vs `main`, to
about stock Lake's level), `leaf` by ~5-6% (probably under the 7% threshold),
and nothing else changes. Measured with the standard sweep.

## What was done

`Lake/Config/InstallPath.lean`: new `findElanLeanSysroot?` with helpers
`parsePinnedElanToolchain?`, `elanSettingsHaveNoOverrides`, `findInPath?`, and
`isElanProxy`; `findLeanInstall?` tries it before `lean --print-prefix` when `LEAN`
is unset. `initOleanHasLeanGithash` (from `olean-githash`) moved from a `where`
helper of `LeanInstall.get` to a top-level function so both can use it. The file
now also imports `Init.Data.String.Legacy` (for `String.splitOn`).

Checks by hand before measuring:

- `strace -f -e execve` of a no-op build of `medium-wide` shows no subprocess at all.
- `lake env` printed the same 24 variables as `main`'s binary (after replacing the
  two binaries' own paths) in eight situations: the corpus; `ELAN_TOOLCHAIN` set
  to v4.34.0; a directory whose `lean-toolchain` says v4.34.0; `/tmp` (no
  `lean-toolchain`, elan's default); a subdirectory of a v4.35.0 project; `LEAN`
  set; a relative `ELAN_HOME`; an `ELAN_HOME` whose `settings.toml` has a directory
  override. The fast path fired only in the corpus, the subdirectory, and the
  relative-`ELAN_HOME` cases; the others ran `lean --print-prefix` as before.
- Paired no-op timing on `medium-wide` (30 alternating pairs, CPUs 16-31): Blake
  0.148 s, stock Lake 0.159 s, paired speedup 1.05x over Lake.
- `nix develop -c tests/run_all.sh --compare .out-main`: 93 passed, 0 failed (as on
  `main`, where the base run in the dev shell also had 93 passed, 0 failed).
- Quick harness: `correctness of candidate: identical artifacts to lake, all
  builds ok, break check ok`.

## Measurements

Standard sweep, 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run,
29 minutes.

```
speedup of main vs lake: cold=0.996x, noop=0.910x, leaf=0.940x, swarm=0.991x, hub=1.004x, primary_geomean=0.960x
speedup of candidate vs lake: cold=1.004x, noop=1.040x, leaf=0.988x, swarm=1.000x, hub=1.007x, primary_geomean=1.009x
speedup of candidate vs main: cold=1.008x, noop=1.143x, leaf=1.050x, swarm=1.009x, hub=1.004x, primary_geomean=1.050x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.023x over 1 runs, threshold 7%; medians main 36.72s, candidate 35.88s)
verdict medium-deep/noop: improved (paired speedup 1.155x over 10 runs, threshold 7%; medians main 0.18s, candidate 0.16s)
verdict medium-deep/leaf: same (paired speedup 1.042x over 10 runs, threshold 7%; medians main 0.58s, candidate 0.56s)
verdict medium-deep/swarm: same (paired speedup 1.001x over 3 runs, threshold 3%; medians main 15.39s, candidate 15.38s)
verdict medium-deep/hub: same (paired speedup 1.004x over 3 runs, threshold 3%; medians main 34.59s, candidate 34.52s)
verdict medium-layered/cold: same (paired speedup 0.996x over 1 runs, threshold 7%; medians main 37.12s, candidate 37.29s)
verdict medium-layered/noop: improved (paired speedup 1.121x over 10 runs, threshold 7%; medians main 0.20s, candidate 0.18s)
verdict medium-layered/leaf: same (paired speedup 1.050x over 10 runs, threshold 7%; medians main 0.60s, candidate 0.57s)
verdict medium-layered/swarm: same (paired speedup 1.002x over 3 runs, threshold 3%; medians main 13.37s, candidate 13.33s)
verdict medium-layered/hub: same (paired speedup 1.009x over 3 runs, threshold 3%; medians main 33.77s, candidate 33.46s)
verdict medium-wide/cold: same (paired speedup 1.006x over 1 runs, threshold 7%; medians main 33.67s, candidate 33.47s)
verdict medium-wide/noop: improved (paired speedup 1.213x over 10 runs, threshold 7%; medians main 0.20s, candidate 0.17s)
verdict medium-wide/leaf: same (paired speedup 1.064x over 10 runs, threshold 7%; medians main 0.39s, candidate 0.37s)
verdict medium-wide/swarm: same (paired speedup 1.022x over 10 runs, threshold 7%; medians main 1.10s, candidate 1.08s)
verdict medium-wide/hub: same (paired speedup 0.995x over 3 runs, threshold 3%; medians main 34.11s, candidate 34.10s)
verdict: success
```

## Conclusion

As predicted: `noop` is 12-21% faster than `main` on every corpus, `leaf` 4-6%
(under the 7% threshold, so `same`), nothing else moved. With both startup
subprocesses gone, Blake's `noop` is now slightly faster than stock Lake's
(`noop=1.040x` vs lake), and its primary geomean against Lake is above 1.0 for
the first time (1.009x).

Startup is now done: a Blake no-op starts no process. What remains of a no-op is
Lake's own work (loading the workspace, parsing 1000 module headers, checking
1000 traces; spread across 16 threads per `perf`, with JSON trace parsing,
allocation, and the build index's `DTreeMap` inserts the largest items), and it
grows with the module count. A `leaf` edit is dominated by the one `lean` run
(~0.3-0.4 s). The large levers left are `hub` and `swarm` (finer-grained
invalidation), as the baseline entry says.
