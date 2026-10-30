# olean-githash: take the toolchain's githash from `Init.olean`, not from `lean --githash`

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/2026-10-30-olean-githash.md
- **Scenarios improved:** noop

## Problem

Stock `lake` is installed next to `lean` and takes the Lean githash (which goes
into every module's trace) from its own compiled-in `Lean.githash`. Blake runs from
`.lake/build/bin/lake`, so Lake's install detection treats it as "not co-located"
and, on every invocation, runs `lean --print-prefix` (through elan) to find the
toolchain and then `<sysroot>/bin/lean --githash`. `strace -f -tt` of a no-op
build of `medium-wide` showed the two processes back to back before the lakefile
is even opened: ~35 ms and ~30 ms unstraced, almost all of it `lean` initializing
all of Lean before it looks at its arguments. That accounted for nearly all of
Blake's `noop` and `leaf` gap to stock Lake in the baseline.

## Mechanism

An `.olean` header is `olean`, a format version byte (2 in Lean 4.35), a flags byte,
the Lean version string padded to 33 bytes, and the 40-character githash of the
Lean that wrote the file (bytes 40-79). `lean` refuses to import an `.olean`
whose header githash or version differs from its own (`failed to read file ...,
incompatible header`; checked by patching one byte), so in any working installation
`<sysroot>/lib/lean/Init.olean` records exactly the githash that `lean --githash`
prints. Blake reads those 80 bytes instead of starting a process.

The header is trusted only when it is well formed (magic and version byte) and its
githash equals Blake's own `Lean.githash`, i.e. when Blake is used with the very
toolchain it was built with, whose header layout is known. Otherwise (a different
toolchain, a dev build with an empty githash, a missing or unreadable file) Blake
runs `lean --githash` exactly as Lake does. The detected githash, and therefore
every trace and artifact, is unchanged.

## Where it lives

`Lake/Config/InstallPath.lean`: `LeanInstall.get` and its helper
`initOleanHasLeanGithash`.

## Measured gains

Standard sweep on machine `2e698309` (i9-14900K, 16 CPUs, 32 GB cap), medium
corpora, result `research/results/20261030T121031Z-2e698309-4923feb.json`:

```
speedup of candidate vs main: cold=1.010x, noop=1.111x, leaf=1.051x, swarm=1.008x, hub=1.003x, primary_geomean=1.042x
speedup of candidate vs lake: cold=1.012x, noop=0.891x, leaf=1.020x, swarm=0.998x, hub=1.001x, primary_geomean=0.976x
```

`noop` improved on all three corpora (paired 1.117x deep, 1.122x layered, 1.081x
wide); `leaf` 1.04-1.07x, under the 7% threshold. About 0.02-0.03 s per
invocation, so the gain is fixed per `lake` command, not proportional to the
project's size.

## Caveats and follow-ups

- The remaining startup subprocess is `lean --print-prefix` (~35 ms through elan:
  ~10 ms of elan, ~25 ms of `lean` initialization). Removing it means knowing which
  toolchain elan would pick for the current directory without asking, which
  duplicates elan's resolution rules (`ELAN_TOOLCHAIN`, directory overrides in
  `settings.toml`, `lean-toolchain` lookup, channel names). Doing that for only the
  unambiguous case, with the subprocess as a fallback, is the obvious next step.
  Running it concurrently with work that does not need the toolchain is the
  other option.
- If a future Lean changes the `.olean` header layout, the check simply stops
  matching and Blake falls back to the subprocess; it cannot misread a githash,
  because it accepts only Blake's own.
