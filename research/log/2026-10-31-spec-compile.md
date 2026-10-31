# spec-compile: compile edited modules speculatively before their imports are checked

- **Date:** 2026-10-31
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** failure (no effect on `leaf`, which it targets; the harness's `success` comes from `noop` noise, see below)
- **Branch / PR:** research/spec-compile-log (code: research/spec-compile-wip, https://github.com/adamtopaz/blake/tree/research/spec-compile-wip) — PR TBD
- **Result files:** research/results/20261031T225057Z-2e698309-5d37e98.json

## Hypothesis

`parallel-fetch` (log of the same date) found that a `leaf` build of `medium-deep`
requests its one compile ~40-57 ms after start. That is the module discovery and
import checks every build does. The compile itself then takes ~250-290 ms. If an
edited module whose imports were not edited were compiled speculatively as soon as
it is known to be edited, with the setup of its previous compile, the compile should
start ~30 ms earlier. Its result would be adopted once Lake confirms it would have
run the very same compile. Expected: `leaf` ~10% faster, more on `wide` (shorter
compiles); little elsewhere.

## What was done

`Lake/Build/Actions.lean`:
- `speculateCompile` starts a compile of the module through the zygote:
  - the arguments are those `compileLeanModule` would use, except that the outputs
    go to `<file>.spec` and the setup is a copy of the previous `setup.json`;
  - `--incr-load` is added if the module's header snapshot is marked `saved`;
  - it does nothing if the setup says `isModule`, or if any of the setup's
    `importArts` modules is itself edited;
  - it records the source's modification time and those of every imported artifact
    listed in the setup.
- `compileLeanModule` claims a matching speculation (`claimSpeculation`) instead of
  running `lean`. A speculation matches only if:
  - the arguments (with the real paths) equal the ones it would run;
  - the setup JSON it just wrote is byte-identical to the one the speculation ran
    with;
  - `LEAN_PATH` is the same;
  - the source and every recorded input still have their recorded modification
    times;
  - no header-snapshot save is due.

  It then waits for the speculation, moves its outputs into place, logs the same
  verbose command line, and processes its messages as usual. Otherwise it compiles
  normally.
- `stopZygotes` waits for leftover speculations and removes their temporary files.

`Lake/Build/Library.lean`: as soon as `recCollectLocalModules` has the library's
roots, the background task that starts the zygote (`one-zygote`, until now run after
discovery in `recBuildLean`) also starts speculations for the edited roots.
`headerSnapshotLoad?` guesses the snapshot argument and skips modules about to save
a snapshot.

Checked by hand on a scratch copy of `medium-deep`:
- The speculation is adopted: `strace` shows `M00999.olean.spec`, `.ilean.spec`
  and `.c.spec` renamed into place, and the build's CPU time equals `main`'s, so
  there is no second compile.
- No `.spec` files are left behind.

The first version started speculations after discovery (in `recBuildLean`). Its
`.olean` appeared no earlier than with `main`: discovery itself is most of the
pre-compile time. The zygote starts only then, and its own initialization (~25 ms,
`lean_initialize` of all of Lean) gates any compile, speculative or not.

Moving the trigger to the roots (commit `5d37e98`), 16 alternating `leaf` pairs
(CPUs 0-15, each build preceded by a harness-style edit): `main` 432 ms, candidate
417 ms, paired 1.024x.

## Measurements

Standard sweep on `5d37e98`: 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run.

```
speedup of main vs lake: cold=1.079x, noop=2.485x, leaf=1.853x, swarm=1.207x, hub=1.097x, primary_geomean=1.572x
speedup of candidate vs lake: cold=1.088x, noop=2.513x, leaf=1.836x, swarm=1.199x, hub=1.107x, primary_geomean=1.573x
speedup of candidate vs main: cold=1.008x, noop=1.011x, leaf=0.991x, swarm=0.994x, hub=1.009x, primary_geomean=1.001x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.006x over 1 runs, threshold 7%; medians main 35.30s, candidate 35.07s)
verdict medium-deep/noop: improved (paired speedup 1.139x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.07s)
verdict medium-deep/leaf: same (paired speedup 1.009x over 10 runs, threshold 7%; medians main 0.34s, candidate 0.33s)
verdict medium-deep/swarm: same (paired speedup 1.005x over 3 runs, threshold 3%; medians main 14.69s, candidate 14.54s)
verdict medium-deep/hub: same (paired speedup 1.017x over 3 runs, threshold 3%; medians main 33.97s, candidate 33.40s)
verdict medium-layered/cold: same (paired speedup 1.014x over 1 runs, threshold 7%; medians main 37.99s, candidate 37.46s)
verdict medium-layered/noop: improved (paired speedup 1.086x over 10 runs, threshold 7%; medians main 0.07s, candidate 0.07s)
verdict medium-layered/leaf: same (paired speedup 0.999x over 10 runs, threshold 7%; medians main 0.34s, candidate 0.34s)
verdict medium-layered/swarm: same (paired speedup 0.979x over 3 runs, threshold 3%; medians main 13.72s, candidate 13.54s)
verdict medium-layered/hub: same (paired speedup 1.007x over 3 runs, threshold 3%; medians main 32.89s, candidate 32.94s)
verdict medium-wide/cold: same (paired speedup 1.004x over 1 runs, threshold 7%; medians main 32.24s, candidate 32.12s)
verdict medium-wide/noop: same (paired speedup 0.961x over 10 runs, threshold 7%; medians main 0.07s, candidate 0.07s)
verdict medium-wide/leaf: same (paired speedup 0.987x over 10 runs, threshold 7%; medians main 0.17s, candidate 0.17s)
verdict medium-wide/swarm: same (paired speedup 0.991x over 10 runs, threshold 7%; medians main 0.90s, candidate 0.94s)
verdict medium-wide/hub: same (paired speedup 1.001x over 3 runs, threshold 3%; medians main 31.89s, candidate 31.55s)
verdict: success
```

`leaf`, the scenario this targets, did not move: 1.009x, 0.999x, 0.987x. Runs in
ms (the second run of each is the header-snapshot save, see `header-stub`):

```
medium-deep    main 403 563 332 324 332 342 378 301 335 349   cand 424 550 350 326 305 303 329 336 348 314
medium-layered main 410 576 367 337 339 332 321 326 314 356   cand 408 587 319 329 331 342 347 328 325 349
medium-wide    main 206 349 160 156 162 164 161 187 177 168   cand 204 362 158 189 169 186 148 156 169 181
```

The two `improved` verdicts are `noop`, on `deep` (1.139x) and `layered` (1.086x).
`wide` came out at 0.961x, and `noop` overall at 1.011x. On a `noop` the change only
moves the background scan of 1000 source and trace modification times (which ran
anyway) from after module discovery to before it. That cannot explain +14% on one
corpus and -4% on another; it is the harness's usual `noop` spread (runs of 51-96
ms here). As with `critical-nice`, I record this as a failure and do not merge it:
the machinery is intricate (outputs written aside and moved in, a second validation
path for every compile) and buys nothing measurable.

## Conclusion

Compiling an edited module before Lake has checked its imports works (the result is
adopted, with byte-identical artifacts), but it does not make `leaf` faster.
- The compile cannot start before the build's zygote is ready, and the zygote spends
  ~25 ms initializing all of Lean.
- Even started at the library's roots, before any header is read, the zygote is ready
  only shortly before Lake's normal path would have sent the compile anyway.
- Measured by hand the gain was ~15 ms of ~430 (1.024x); in the harness it is lost
  in noise.

What this says about `leaf`:
- Its fixed cost before the compile is now mostly Lake's start-up, module discovery,
  and the zygote's initialization, which overlap one another.
- The compile itself (Lean elaborating the module) is ~85% of the build.

Remaining options:
- A zygote that is already initialized when Lake starts. That would mean one that
  outlives a build, which the rules forbid.
- A cheaper zygote initialization: only what children need, though they need the
  elaborator.
- Faster elaboration inside `lean`, which is off-limits.
