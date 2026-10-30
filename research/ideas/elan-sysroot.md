# elan-sysroot: find the toolchain elan would pick without running `lean --print-prefix`

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/2026-10-30-elan-sysroot.md
- **Scenarios improved:** noop

## Problem

Blake runs from `.lake/build/bin/lake`, not from the toolchain's `bin/`, so Lake's
install detection does not treat it as co-located with `lean` and asks
`lean --print-prefix` where the toolchain is, on every invocation. With elan, `lean`
on `PATH` is elan's proxy: ~10 ms for elan to resolve the toolchain, then ~25 ms
for the real `lean` to initialize all of Lean before it reads its arguments. That
sits on the critical path before the lakefile is opened. Together with
`lean --githash` (removed by [olean-githash](olean-githash.md)), it was the whole
`noop` gap between Blake and stock Lake.

## Mechanism

Elan's choice follows fixed rules (read from elan v4.2.1's source:
`Cfg::find_override_toolchain_or_default`, `find_override_from_dir_walk`,
`lookup_unresolved_toolchain_desc`, `Toolchain::from`):

1. `ELAN_TOOLCHAIN`, if set and non-empty.
2. Walking up from the canonical working directory: a directory override in
   `$ELAN_HOME/settings.toml`, else a `lean-toolchain` file (first line, trimmed),
   else a `leanpkg.toml`.
3. The default toolchain.

A `lean-toolchain` name is tried first as a path to a toolchain directory
(relative to the file), then as a linked toolchain named after its release, then
as `origin:release`, installed under
`$ELAN_HOME/toolchains/<origin, / as -->---<release>`. `lean --print-prefix`
prints the directory above the real path of the selected `bin/lean`.

`findElanLeanSysroot?` follows those rules only where they are unambiguous and
returns `none` (so Lake runs `lean --print-prefix` exactly as before) on any doubt.
It requires all of the following:

- not Windows;
- `LEAN` and `LEAN_SYSROOT` unset, `ELAN_TOOLCHAIN` unset or empty;
- the first `lean` on `PATH` is the same file as the `elan` next to it: the same
  real path (symlinks, as package managers install it), or a hard link with the
  same size and mtime (elan's installer);
- `settings.toml` exists and has no directory overrides (any line mentioning
  `overrides`, other than an empty `[overrides]` table, means "unsure");
- walking up, no `leanpkg.toml` comes before the nearest `lean-toolchain`, and
  that file pins a release `owner/repo:v<digit>...` (channels like `stable`, bare
  names, and paths are left to elan);
- nothing exists at `<dir>/<name>` (it could be a path toolchain) and no
  `toolchains/<release>` entry exists (it could be a linked toolchain);
- the toolchain's `bin/lean` is installed (otherwise elan would install it);
- the toolchain's `Init.olean` records Blake's own `Lean.githash`, so the fast
  path only ever selects the very Lean that Blake was built with.

The sysroot is then the parent of the real path of the toolchain's `bin/`. The
result is the same `LeanInstall`, so traces, artifacts, and `lake env` are
unchanged. Checked by hand: `lake env` printed the same 24 variables as `main` in
the corpus, under `ELAN_TOOLCHAIN`, under an older `lean-toolchain`, with no
`lean-toolchain` at all, from a nested directory, with `LEAN` set, with a relative
`ELAN_HOME`, and with a directory override present. The cases that need elan
still run `lean --print-prefix`.

## Where it lives

`Lake/Config/InstallPath.lean`: `findElanLeanSysroot?` and its helpers
(`parsePinnedElanToolchain?`, `elanSettingsHaveNoOverrides`, `findInPath?`,
`isElanProxy`), called from `findLeanInstall?` when `LEAN` is unset.
`initOleanHasLeanGithash` is now a top-level function shared with `LeanInstall.get`.

## Measured gains

Standard sweep on machine `2e698309` (i9-14900K, 16 CPUs, 32 GB cap), medium
corpora, result `research/results/20261030T134734Z-2e698309-f249de8.json`:

```
speedup of candidate vs main: cold=1.008x, noop=1.143x, leaf=1.050x, swarm=1.009x, hub=1.004x, primary_geomean=1.050x
speedup of candidate vs lake: cold=1.004x, noop=1.040x, leaf=0.988x, swarm=1.000x, hub=1.007x, primary_geomean=1.009x
```

`noop` improved on all three corpora (paired 1.155x deep, 1.121x layered, 1.213x
wide); `leaf` 1.04-1.06x, under the 7% threshold. A Blake no-op now starts no
process and is slightly faster than stock Lake's.

## Caveats and follow-ups

- It encodes elan's resolution rules as of elan v4.2.1. A future elan that adds a
  new override source would be missed only if that source took precedence over a
  `lean-toolchain` file pinning Blake's own Lean release; if that happens, add the
  new source to the list of reasons to bail out.
- A toolchain whose `bin/lean` is a binary wrapper that runs a Lean installed
  elsewhere would get the wrapper's directory as its sysroot, where `lean
  --print-prefix` reports the other one. Both have the same githash (checked), so
  the build is the same; only the reported paths would differ. Elan's own
  toolchains are never like this.
- With both startup subprocesses gone, a Blake `noop` costs about what stock
  Lake's does. The remaining time is Lake's own: elaborating nothing, parsing 1000
  module headers, and checking 1000 traces (JSON parsing, hashing, `DTreeMap`
  inserts in `recBuildWithIndex`, spread over 16 threads per `perf`). That is
  where the next `noop`/`leaf` gains are, and it grows with the module count.
