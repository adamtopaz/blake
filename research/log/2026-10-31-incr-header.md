# incr-header: resume a module that is being edited from Lean's saved header snapshot

- **Date:** 2026-10-31
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** failure (the first standard sweep flagged `noop`; the rerun, `success`, cannot override it)
- **Branch / PR:** research/incr-header-log (code: research/incr-header-wip, https://github.com/adamtopaz/blake/tree/research/incr-header-wip) — PR TBD
- **Result files:** research/results/20261031T080508Z-2e698309-1702b24.json (standard sweep), research/results/20261031T085453Z-2e698309-1702b24.json (rerun, `--repeat 5 --repeat-fast 20`)

## Hypothesis

After `one-zygote`, a `leaf` build is mostly the compile of the one edited module,
and in a deep module much of that compile is `import`: Lean maps every
transitively imported `.olean` and builds the environment (constant map, extension
states) from them, although none of them changed since the last build.

Lean 4.35 has an experimental pair of flags for this, in `runFrontend`
(`Lean/Elab/Frontend.lean`): `--incr-header-save=<file>` compacts the environment
right after the header into a file (plus `<file>.deps`, the list of imported
files it points into), and `--incr-load=<file>` resumes from it. If the new
file's header differs, Lean imports normally. I timed this by hand on
`medium-deep`'s `leaf` target (`Corpus.M00999`, layer 84) with plain `lean` on
an idle machine (best of 5):

```
plain compile           574 ms
compile + header save  1105 ms   (snapshot 37.3 MB, .deps 0.5 MB)
compile from snapshot   430 ms   (.olean, .ilean, .c byte-identical to plain)
```

So a module that is edited again and again could compile ~140 ms (25%) faster
from the third edit on, at the cost of one ~0.5 s slower compile and ~38 MB of
disk per module. Expected: `leaf` clearly faster on `deep` and `layered` (their
leaf targets import deep closures), less on `wide`; `swarm` maybe a little
(the same 24 modules are edited each time, but those that import another edited
module never qualify); `hub` about neutral (only the hub itself qualifies);
`cold` and `noop` unchanged (no module is edited).

## What was done

`Module.headerSnapshot` (new, `Lake/Build/Module.lean`) decides, for a module
being rebuilt, whether to save or load a snapshot. The key is every input of the
compile except the source: a new `ModulePreSetup.headerHash` (the module's trace
just before the source is mixed in: imports, libraries, platform, toolchain),
`lean`, the search path, the arguments, and the module setup JSON (options and
the paths of all imported artifacts). It is kept in
`ir/<mod>.blake-header.key`, next to the snapshot `ir/<mod>.blake-header`.

- A module counts as *edited* if its source is newer than its trace file.
- First edit: record the key (`edited`). Nothing else.
- Next edit with the same key: compile with `--incr-header-save`, then mark
  the key `saved`.
- Any later rebuild with the same key: compile with `--incr-load`.
- Any other rebuild (imports changed, options changed, ...): delete the
  snapshot.
- Modules whose setup loads plugins or dynamic libraries never use snapshots
  (a resumed header does not load them again), nor does a `lean` other than the
  one Blake was built with (`initOleanHasLeanGithash`, as for the zygote).

Saving only from the second edit on keeps the 0.5 s cost away from modules that
are rebuilt once (`git pull`, a chained module in `swarm` whose import changes
every time).

`compileLeanModule` (`Lake/Build/Actions.lean`) takes the two optional paths and
passes the flags to `lean` (through the zygote, whose option table already has
them). If a run with `--incr-load` fails other than by reporting errors in the
source (exit code 1 with nothing on stderr), the snapshot is deleted and the
module is compiled again without it, so a damaged snapshot cannot fail a build.
A failed save deletes the partial snapshot.

Checked by hand on a scratch copy of `medium-deep` (CPUs 16-31): edits 1-5 of
the leaf took 650, 1194 (save), 514, 518, 518 ms with `-v`; the `.olean`,
`.ilean` and `.c` after a resumed compile are byte-identical to a plain `lean`
run of the same command; an edit with a type error reports the same error and
keeps the snapshot; a truncated snapshot makes `lean` fail, Blake recompiles
without it and the build succeeds; editing the leaf's import discards the
leaf's snapshot.

## Measurements

Commit `1702b24`. 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`. Checks first: `nix develop -c tests/run_all.sh --compare
.out-main` 93 passed, 0 failed; quick harness: candidate artifacts identical to
`lake`, all builds and the break check ok.

Standard sweep (10 pairs for fast scenarios, 3 for the rest):

```
speedup of main vs lake: cold=1.078x, noop=2.027x, leaf=1.389x, swarm=1.109x, hub=1.091x, primary_geomean=1.359x
speedup of candidate vs lake: cold=1.073x, noop=1.907x, leaf=1.727x, swarm=1.173x, hub=1.088x, primary_geomean=1.432x
speedup of candidate vs main: cold=0.996x, noop=0.941x, leaf=1.243x, swarm=1.058x, hub=0.997x, primary_geomean=1.054x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 0.998x over 1 runs, threshold 7%; medians main 36.25s, candidate 36.31s)
verdict medium-deep/noop: regressed (paired speedup 0.894x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.09s)
verdict medium-deep/leaf: improved (paired speedup 1.216x over 10 runs, threshold 7%; medians main 0.43s, candidate 0.35s)
verdict medium-deep/swarm: same (paired speedup 1.002x over 3 runs, threshold 3%; medians main 14.40s, candidate 14.42s)
verdict medium-deep/hub: same (paired speedup 0.997x over 3 runs, threshold 3%; medians main 35.05s, candidate 34.43s)
verdict medium-layered/cold: same (paired speedup 0.996x over 1 runs, threshold 7%; medians main 38.48s, candidate 38.65s)
verdict medium-layered/noop: same (paired speedup 1.005x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-layered/leaf: improved (paired speedup 1.273x over 10 runs, threshold 7%; medians main 0.44s, candidate 0.35s)
verdict medium-layered/swarm: same (paired speedup 1.001x over 3 runs, threshold 3%; medians main 13.43s, candidate 13.64s)
verdict medium-layered/hub: same (paired speedup 0.990x over 3 runs, threshold 3%; medians main 32.60s, candidate 32.82s)
verdict medium-wide/cold: same (paired speedup 0.994x over 1 runs, threshold 7%; medians main 32.76s, candidate 32.97s)
verdict medium-wide/noop: regressed (paired speedup 0.915x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-wide/leaf: improved (paired speedup 1.237x over 10 runs, threshold 7%; medians main 0.24s, candidate 0.19s)
verdict medium-wide/swarm: improved (paired speedup 1.243x over 10 runs, threshold 7%; medians main 1.04s, candidate 0.86s)
verdict medium-wide/hub: same (paired speedup 0.987x over 3 runs, threshold 3%; medians main 32.97s, candidate 33.64s)
verdict: failure
```

The `noop` "regressions" looked like noise: `noop` rebuilds nothing, so it never
reaches the new code (all of it runs inside `buildAction`, i.e. only for a module
being compiled; the one other change is reading the job's trace hash once per
module in `recFetchPreSetup`). The per-run times were bimodal (0.07 s or 0.09-0.10 s
for both tools), and 30 alternating pairs by hand (CPUs 0-15, scratch copy of
`medium-deep`) gave main 56.3 ms vs candidate 57.2 ms, paired ratio 0.981. So I
reran as §6 says, with `--repeat 5 --repeat-fast 20`:

```
speedup of main vs lake: cold=1.067x, noop=2.081x, leaf=1.347x, swarm=1.097x, hub=1.002x, primary_geomean=1.325x
speedup of candidate vs lake: cold=1.073x, noop=2.053x, leaf=1.710x, swarm=1.184x, hub=1.091x, primary_geomean=1.459x
speedup of candidate vs main: cold=1.006x, noop=0.986x, leaf=1.269x, swarm=1.079x, hub=1.089x, primary_geomean=1.101x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 0.994x over 1 runs, threshold 7%; medians main 33.81s, candidate 34.02s)
verdict medium-deep/noop: same (paired speedup 0.992x over 20 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-deep/leaf: improved (paired speedup 1.214x over 20 runs, threshold 7%; medians main 0.44s, candidate 0.35s)
verdict medium-deep/swarm: same (paired speedup 0.995x over 5 runs, threshold 3%; medians main 14.32s, candidate 14.37s)
verdict medium-deep/hub: same (paired speedup 1.002x over 5 runs, threshold 3%; medians main 41.44s, candidate 32.89s)
verdict medium-layered/cold: same (paired speedup 1.011x over 1 runs, threshold 7%; medians main 44.37s, candidate 43.90s)
verdict medium-layered/noop: same (paired speedup 0.974x over 20 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-layered/leaf: improved (paired speedup 1.276x over 20 runs, threshold 7%; medians main 0.46s, candidate 0.36s)
verdict medium-layered/swarm: same (paired speedup 0.990x over 5 runs, threshold 3%; medians main 15.05s, candidate 15.39s)
verdict medium-layered/hub: same (paired speedup 1.014x over 5 runs, threshold 3%; medians main 38.14s, candidate 37.23s)
verdict medium-wide/cold: same (paired speedup 1.013x over 1 runs, threshold 7%; medians main 38.49s, candidate 38.00s)
verdict medium-wide/noop: same (paired speedup 0.976x over 20 runs, threshold 7%; medians main 0.08s, candidate 0.09s)
verdict medium-wide/leaf: improved (paired speedup 1.286x over 20 runs, threshold 7%; medians main 0.25s, candidate 0.19s)
verdict medium-wide/swarm: improved (paired speedup 1.274x over 20 runs, threshold 7%; medians main 1.22s, candidate 0.95s)
verdict medium-wide/hub: same (paired speedup 1.007x over 5 runs, threshold 3%; medians main 37.13s, candidate 37.10s)
verdict: success
```

(The `hub` medians in the rerun are skewed by a slow first repetition of `main`
on `deep`; the paired ratio, 1.002x, is what counts. `hub` is not improved.)

Per-repetition ratios main/candidate in the first sweep show the cost profile:

```
medium-deep    leaf   1.03 0.55 1.29 1.21 1.12 1.24 1.23 1.25 1.21 1.28
medium-layered leaf   0.98 0.55 1.35 1.33 1.31 1.24 1.30 1.25 1.23 1.30
medium-wide    leaf   0.99 0.56 1.16 1.21 1.26 1.09 1.28 1.27 1.31 1.29
medium-wide    swarm  1.05 0.42 1.24 1.26 1.23 1.27 1.25 1.24 1.23 1.34
medium-deep    swarm  1.00 0.97 1.00
medium-layered swarm  1.00 0.96 1.01
```

Repetition 1 records the key, repetition 2 saves (about twice as slow), and from
repetition 3 on every build resumes from the snapshot, 1.2-1.3x faster.

## Conclusion

The mechanism works, and as expected: from the third edit of a module whose imports
did not change, its compile is ~20-25% faster (`leaf` 1.21-1.29x on all three
corpora, `medium-wide/swarm` 1.24-1.27x). Artifacts stayed identical to Lake's
everywhere, including the break check. `deep`/`layered` `swarm` and `hub`
are unchanged: their builds are long chains, and the save on repetition 2 (+0.5 s
on one module) and the gain afterwards are both small next to 14-35 s.

The outcome is nevertheless `failure`: the standard sweep flagged `noop` on two
corpora (0.894x, 0.915x), and `pr_check.py` refuses a `research:` PR whose code also
has a `failure` result, however the rerun came out. I am confident the `noop` flag
was noise (the code `noop` runs is essentially unchanged; 30 manual pairs gave 0.98x;
20 pairs per corpus in the rerun gave 0.97-0.99x), but the rule exists so that runs
are not repeated until they pass, and I followed it.

For the next iteration: this idea is worth resubmitting. Identical code cannot be
merged (the committed `failure` result matches its build-tool tree), so it needs a
genuine change, measured anew. Candidates:
- Write the key file only after the artifacts are written. Today it is written
  before `computeArtifacts`.
- Delete the key file in `compileLeanModule`'s fallback too. Today a stale `saved`
  key heals itself only on the next rebuild.
- Shrink the key: it holds the module's whole setup JSON, tens of KB for a deep
  module.
- Bigger win: make the save cheaper or hide it. On `medium-deep`'s leaf, saving
  nearly doubles the compile (574 ms becomes 1.1 s) and writes 37 MB, most of it the
  core library's environment.

Caveats a resubmission should state:
- ~38 MB of disk per module being edited, kept until its imports change or the build
  directory is cleaned.
- A resumed header runs the imported modules' `[init]` declarations through
  `runInitAttrsForModules` ("the central incr HACK" in Lean's source).
- The flags are experimental in Lean 4.35. The githash guard confines them to the
  `lean` Blake was built with.
