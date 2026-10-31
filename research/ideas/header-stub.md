# header-stub: resume edited modules from Lean's header snapshot, saved from a header-only stub

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/2026-10-31-header-stub.md (first attempt: ../log/2026-10-31-incr-header.md)
- **Scenarios improved:** leaf (all corpora), swarm (`wide`)

## Problem

When a module is edited and rebuilt, `lean` imports its whole import closure again.
It maps every transitively imported `.olean` and builds the environment (constant
map, environment extension states) from them, although none of them changed. On
`medium-deep`'s leaf target (layer 84) a plain compile takes 574 ms; resuming after
the imports takes 430 ms. In an edit-build loop this cost is paid on every build.

## Mechanism

Lean 4.35's `runFrontend` (`Lean/Elab/Frontend.lean`) has two experimental flags.
`--incr-header-save=<file>` compacts the environment as it stands right after the
header into `<file>`. `--incr-load=<file>` maps it back and resumes from it, if the
new file's header syntax tree equals the saved one, source positions included
(`Language.Lean.process`); otherwise Lean imports normally.

Blake uses them for modules being edited (`Module.headerSnapshot`):

- **Edited** means the source is newer than the module's trace file.
- **The key** is every input of the compile except the source after its header:
  - `ModulePreSetup.headerHash`, the module's trace just before the source is
    mixed in (imports, libraries, platform, toolchain);
  - `lean`, the search path and the arguments;
  - the module setup (options, and the paths of every imported artifact);
  - the header's text.

  It is kept in `ir/<mod>.blake-header.key`.
- **First edit:** record the key.
- **Second edit with the same key:** save the snapshot. A separate `lean` run on a
  stub file holding only the header (`ir/<mod>.blake-header.lean`, same arguments
  and setup file) saves it alongside the normal compile (`HeaderSave`,
  `startHeaderSave` in `Lake/Build/Actions.lean`). The stub's header is the module's,
  positions included, so Lean accepts its snapshot. The module's job does not wait
  for it; `stopZygotes` waits for outstanding saves at the end of the build, so no
  `lean` outlives Blake.
- **Later rebuilds with the same key:** compile with `--incr-load`.
- **Any other rebuild** (imports, options or header text changed) deletes the
  snapshot.

Saving waits for the second edit in a row, so modules that are rebuilt once
(`git pull`), or whose imports change every time, never pay for a snapshot.

Safety:
- The key covers the content (through the import trace) and the paths of
  everything the snapshot points into.
- Modules whose setup loads plugins or dynamic libraries never use snapshots (a
  resumed header does not load them).
- Neither does a `lean` other than the one Blake was built with
  (`initOleanHasLeanGithash`).
- If a resumed compile fails other than by reporting errors in the source (exit
  code 1, empty stderr), Blake deletes the snapshot and its key and compiles again
  without it.
- Artifacts are byte-identical to a plain compile's: checked by hand, and on every
  harness step.

## Where it lives

- `Lake/Build/Module.lean`: `Module.headerSnapshot`; `Module.buildLean`;
  `headerHash` in `recFetchPreSetup`.
- `Lake/Build/Infos.lean`: `ModulePreSetup.headerHash`.
- `Lake/Build/Actions.lean`: `HeaderSave`, `startHeaderSave`, `headerSavesRef`,
  `runLeanQuiet`, the `incrLoad?`/`headerSave?` parameters of `compileLeanModule`
  and the fallback, and the wait in `stopZygotes`.

## Measured gains

Standard sweep on machine `2e698309` (i9-14900K, 16 CPUs, 32 GB cap), result
`research/results/20261031T120135Z-2e698309-5207622.json`:

```
speedup of candidate vs main: cold=1.010x, noop=0.970x, leaf=1.279x, swarm=1.077x, hub=0.995x, primary_geomean=1.074x
```

`leaf` improved on `deep` (1.259x), `layered` (1.294x) and `wide` (1.290x);
`swarm` on `wide` (1.269x). Nothing regressed. The first attempt, which saved
inside the compile, measured the same steady-state gains:
`research/results/20261031T080508Z-2e698309-1702b24.json` and
`research/results/20261031T085453Z-2e698309-1702b24.json`.

## Caveats and follow-ups

- The first two edits of a module gain nothing. The second runs the stub save
  alongside the compile: on `medium-deep`'s leaf that edit took 866 ms instead of
  ~650 ms (1194 ms when the save ran inside the compile).
- ~38 MB of disk per module being edited. It holds the environment of the whole
  import closure, core library included, and stays until the module's key changes
  or the build directory is cleaned.
- A snapshot saved in a zygote child holds closures into `libleanshared.so`, so only
  a process that has loaded it (a zygote child, not the `lean` executable) can load
  the snapshot. Saves and loads both go through the zygote; a mismatch falls back
  to a plain compile.
- Resuming runs the imported modules' `[init]` declarations through
  `runInitAttrsForModules` instead of importing them, which Lean's source calls
  "the central incr HACK"; exotic initializers could in theory behave differently.
- The flags are experimental in Lean. A toolchain bump must re-check them; the
  githash guard keeps any other `lean` on the old path.
- To try: save on the first edit. Now that the save is off the critical path, the
  cost to one-off edits may be worth the faster second edit.
