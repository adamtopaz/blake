# header-stub: header snapshots for edited modules, saved from a header-only stub alongside the compile

- **Date:** 2026-10-31
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** success
- **Branch / PR:** research/header-stub — https://github.com/adamtopaz/blake/pull/13
- **Result files:** research/results/20261031T120135Z-2e698309-5207622.json

## Hypothesis

`incr-header` (log `2026-10-31-incr-header.md`, code on `research/incr-header-wip`)
showed that resuming an edited module's compile from Lean's saved header snapshot
(`--incr-header-save` / `--incr-load`) makes later edits of that module 1.2-1.3x
faster: `leaf` 1.21-1.29x on all corpora, `medium-wide/swarm` 1.24-1.27x, artifacts
identical. It was recorded as a failure only because its first sweep flagged `noop`,
which never reaches the new code (its 20-pair rerun was `success`, `noop`
0.97-0.99x). Identical code cannot be resubmitted, and the attempt had a real flaw
worth fixing: the save ran inside the module's compile and nearly doubled it
(574 ms became 1.1 s on `medium-deep`'s leaf), on the critical path of the edit that
paid for it.

Lean's reuse check (`Language.Lean.process`, v4.35.0) compares only the header's
syntax tree, source positions included (`eqWithInfo`, modulo trailing whitespace).
A snapshot saved from a file holding nothing but the module's header (its source up
to the first command) is therefore as good as one saved from the module itself. That
file elaborates nothing past the imports, so its `lean` run can save the snapshot in
parallel with the real compile, and the module's job need not wait for it.

Expected: the same steady-state gains as `incr-header` (`leaf` on every corpus,
`swarm` on `wide`). The edit that saves now costs about max(compile, import + save)
instead of compile + save. `cold` and `noop` are unchanged.

## What was done

This is `incr-header`'s code (commit `1702b24`, cherry-picked as `0690824`), plus
the following changes (commit `5207622`):

- `Lake/Build/Actions.lean`:
  - `HeaderSave` and `startHeaderSave`: write the header to
    `ir/<mod>.blake-header.lean`, then start a dedicated task. The task runs
    `lean <module args> <stub> --setup <module setup> --incr-header-save=<snapshot>
    --json` through the zygote (or as a process if there is none), with no output
    files. On success it marks the key `saved`; on failure it deletes the partial
    snapshot.
  - `compileLeanModule` starts the save right after writing the setup file, then
    runs the normal compile.
  - The tasks are kept in `headerSavesRef`. `stopZygotes`, which runs at the end
    of every build, waits for them before stopping the zygote, so no `lean` outlives
    Blake.
  - The `--incr-load` fallback now also deletes the key file.
- `Lake/Build/Module.lean`, `Module.headerSnapshot`: the key now ends with the
  header's text, read with Lake's own import parser (`ParseImports.main`). A header
  that changes only in comments or layout shifts source positions, so Lean would not
  reuse a snapshot saved from it. It now changes the key instead of leaving a
  snapshot that is loaded for nothing.

Unchanged from `incr-header`:
- A module is *edited* if its source is newer than its trace file.
- The first edit records the key, the second (same key) saves, and later rebuilds
  with the same key compile with `--incr-load`. Any other rebuild deletes the
  snapshot.
- No snapshots for modules with plugins or dynlibs, or for a `lean` other than the
  one Blake was built with.

Found on the way: a snapshot saved in a zygote child holds closures that point into
`libleanshared.so` (the zygote links it; the `lean` executable does not). Plain
`lean` refuses to load it ("library required for closure relocation is not loaded
in this process"). Snapshots are saved and loaded through the same zygote binary, so
this does not arise in normal builds. If it ever does (for example the zygote dies
mid-build), the fallback recompiles without the snapshot.

Checked by hand on a scratch copy of `medium-deep` (CPUs 16-31):
- Edits 1-5 of the leaf took 660, 866 (save), 507, 516, 518 ms. With
  `incr-header` they took 650, 1194, 514, 518, 518.
- The `.olean`, `.ilean` and `.c` from a resumed compile are byte-identical to a
  plain `lean` run.
- A truncated snapshot makes the compile fail; Blake recompiles without it and
  deletes all three files.
- A comment added above the imports changes the key: the snapshot is discarded and
  the key re-recorded.
- `noop`, 40 alternating pairs against `main`: wall 56.8 vs 57.0 ms (0.995x), CPU
  151.0 vs 151.2 ms.

Checks:
- `nix develop -c tests/run_all.sh --compare .out-main`: 93 passed, 0 failed (base
  on `5f44355`: 93 passed).
- Quick harness: `correctness of candidate: identical artifacts to lake, all builds
  ok, break check ok`. Its verdict was `failure`, on `small-wide/noop` 0.927x
  (pair ratios 0.84-1.17) and `small-wide/hub` 0.962x (0.95 already on the first
  repetition, where the candidate does nothing different).

## Measurements

Standard sweep on `5207622`: 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run.

```
speedup of main vs lake: cold=1.099x, noop=2.060x, leaf=1.372x, swarm=1.106x, hub=1.092x, primary_geomean=1.359x
speedup of candidate vs lake: cold=1.110x, noop=1.998x, leaf=1.754x, swarm=1.191x, hub=1.087x, primary_geomean=1.460x
speedup of candidate vs main: cold=1.010x, noop=0.970x, leaf=1.279x, swarm=1.077x, hub=0.995x, primary_geomean=1.074x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.030x over 1 runs, threshold 7%; medians main 35.73s, candidate 34.69s)
verdict medium-deep/noop: same (paired speedup 1.065x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-deep/leaf: improved (paired speedup 1.259x over 10 runs, threshold 7%; medians main 0.43s, candidate 0.34s)
verdict medium-deep/swarm: same (paired speedup 0.996x over 3 runs, threshold 3%; medians main 14.32s, candidate 14.33s)
verdict medium-deep/hub: same (paired speedup 1.000x over 3 runs, threshold 3%; medians main 33.94s, candidate 34.01s)
verdict medium-layered/cold: same (paired speedup 1.021x over 1 runs, threshold 7%; medians main 37.72s, candidate 36.96s)
verdict medium-layered/noop: same (paired speedup 0.950x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.09s)
verdict medium-layered/leaf: improved (paired speedup 1.294x over 10 runs, threshold 7%; medians main 0.45s, candidate 0.35s)
verdict medium-layered/swarm: same (paired speedup 1.009x over 3 runs, threshold 3%; medians main 13.53s, candidate 13.75s)
verdict medium-layered/hub: same (paired speedup 0.998x over 3 runs, threshold 3%; medians main 33.33s, candidate 33.52s)
verdict medium-wide/cold: same (paired speedup 0.981x over 1 runs, threshold 7%; medians main 33.37s, candidate 34.03s)
verdict medium-wide/noop: same (paired speedup 0.984x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.09s)
verdict medium-wide/leaf: improved (paired speedup 1.290x over 10 runs, threshold 7%; medians main 0.24s, candidate 0.19s)
verdict medium-wide/swarm: improved (paired speedup 1.269x over 10 runs, threshold 7%; medians main 1.08s, candidate 0.85s)
verdict medium-wide/hub: same (paired speedup 0.981x over 3 runs, threshold 3%; medians main 32.17s, candidate 32.39s)
verdict: success
```

Per-repetition ratios main/candidate (repetition 1 records the key, 2 saves, 3 on
resume):

```
medium-deep    leaf   1.01 0.69 1.28 1.28 1.22 1.28 1.29 1.21 1.34 1.24
medium-layered leaf   1.07 0.71 1.18 1.35 1.32 1.29 1.37 1.30 1.13 1.33
medium-wide    leaf   1.10 0.63 1.32 1.53 1.28 1.45 1.15 1.30 1.08 1.38
medium-wide    swarm  1.02 0.46 1.18 1.27 1.26 1.32 1.27 1.32 1.29 1.28
medium-layered swarm  1.01 0.92 1.03
```

With `incr-header`, which saved inside the compile, the save repetition of `leaf`
was 0.55, 0.55, 0.56. Now it is 0.69, 0.71, 0.63. On `medium-wide/swarm` it barely
moved (0.42 then, 0.46 now). There, 24 modules save at once on 16 CPUs, so CPU is
the limit, and the stub's own import is extra work.

## Conclusion

Resuming an edited module from its header snapshot pays off on every corpus.
Every later rebuild of the module skips its imports, which on these corpora is a
fifth to a quarter of the build (`leaf` 1.26-1.29x, `medium-wide/swarm` 1.27x). The
`deep`/`layered` `swarm` and `hub` scenarios are long chains of modules whose
imports change on every run, so they are unchanged. Saving from a header-only stub
takes the save out of the compile, so the one edit that pays for it costs less (the
`leaf` save repetition went from 0.55x to about 0.68x of `main`). On a module whose
compile takes longer than its import plus the save, it would cost nothing in wall
time.

What remains:
- The snapshot is ~38 MB per edited module. Most of it is the environment of the
  core library, which every module imports.
- The second edit of a module still pays for the save.
- Saving on the first edit instead would make the second edit fast. It would also
  cost every one-off edit (`git pull`) the save's CPU and disk; with the save off
  the critical path that trade may now be worth measuring.
- A snapshot saved in a zygote child can be loaded only by a process that has
  `libleanshared.so` loaded.
