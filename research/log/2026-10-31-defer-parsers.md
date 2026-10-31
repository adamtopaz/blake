# defer-parsers: also defer Lean's builtin parsers and the Lake modules that need them

- **Date:** 2026-10-31
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** success
- **Branch / PR:** research/defer-parsers — TBD
- **Result files:** research/results/20261031T184727Z-2e698309-3df6fda.json

## Hypothesis

`deferred-init` made a TOML `lake build` skip `lean_initialize()`'s full
initialization of Lean and the modules behind `LeanExport`. Start-up was still ~15 ms
of a ~45 ms `noop` (medium-deep, P-cores).

`perf record --call-graph dwarf` of `lake --version` shows where those 15 ms go.
`Lean.Parser.Command`'s runtime initializer, with `Lean.Parser.Do` and
`Lean.Parser.Term` inside it, is ~60% of `runtime_initialize_LakeMain`. That is the
registration of every builtin parser (`addBuiltinParser`, token and kind tables). It
is reached first through `Lake.DSL` (the lakefile DSL: `Lake.DSL.Config` imports
`Lean.Elab.Term`). It is also reached through:
- `Lake.Load.Lean` (the elaborator frontend);
- `Lake.Build.Module` (`Lean.Elab.ParseImportsFast` publicly imports
  `Lean.Parser.Module`);
- `Lake.Toml.*` (`import Lean.Parser`);
- `Lake.Config.Meta` and `Lake.Util.Binder` (`Lean.Parser.Command`/`Term`).

A TOML build needs none of these registrations:
- the TOML grammar runs Lake's own combinators with an empty token table
  (`Lake.Toml.loadToml`);
- `ParseImportsFast` is a hand-written parser;
- the DSL, the Lean config loader and the CLI-only commands are not used.

So the same deferral mechanism should apply to the builtin parser modules and to the
Lake modules that use them, as long as nothing on the build path runs their code
early.

## What was done

Only `c/blake_init.c` and `lakefile.lean` changed. The deferred modules are now one
list (`DEFERRED` in the C file; the `--wrap` list in `lean_exe lake`'s
`moreLinkArgs` must match):

- Lean: `Lean.Parser.Term`, `Do`, `Command`, `Tactic`, `Module`, and the umbrella
  `Lean.Parser`;
- `LeanExport.Basic` and `Lake.CLI.Check` (as before);
- Lake: `Lake.Util.Binder`, `Lake.Config.Meta`, `Lake.DSL`, `Lake.Load.Lean`, and
  `Lake.CLI.Translate`, `BuiltinLint`, `Shake`, `Serve`, `Init`.

Each wrapped runtime initializer does nothing at start-up.
`blake_full_initialize` first runs `initialize_Init`/`Std`/`Lean`, which fully
initializes every Lean module, the parsers included: those call their own runtime
initializers directly, not through the wrapped symbols. It then calls the real
initializers in `DEFERRED` order; their imports' wrappers forward while
`blake_full_initialize` runs. When `fullInitialize` runs is unchanged: before every
command other than `build`, and before loading a Lean configuration.

The first prototype crashed at start-up. `Lake.Util.Binder`, imported by the config
modules through `Lake.Config.Meta`, builds a parser from
`Lean.Parser.Term.bracketedBinder` in its initializer. Both modules are used only by
macros (the `configuration` command, DSL binders), so they joined the list.

Checked by hand on a scratch copy of `medium-deep`:
- A TOML `noop` and an edit work, as do `lake env`, a `lakefile.lean` package and
  `translate-config`.
- On a scratch TOML package, these outputs are byte-identical to `main`'s:
  - a TOML syntax error;
  - an unknown field;
  - an unknown target;
  - a Lean syntax error in a module.
- `noop`, 30 runs each (CPUs 0-15): 34.9 ms vs 44.5 ms for `main` (prototype
  relink).

Checks:
- `nix develop -c tests/run_all.sh --compare .out-main`: 93 passed, 0 failed (base
  on `a4f8ad1`: 93 passed).
- Quick harness: candidate artifacts identical to `lake`, builds and break check
  ok; verdict `success` (`noop` 1.127x on `small-layered`, 1.172x on `small-wide`,
  0.971x on `small-deep`).

## Measurements

Standard sweep on `3df6fda`: 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run.

```
speedup of main vs lake: cold=1.098x, noop=2.237x, leaf=1.766x, swarm=1.216x, hub=1.101x, primary_geomean=1.517x
speedup of candidate vs lake: cold=1.099x, noop=2.598x, leaf=1.836x, swarm=1.220x, hub=1.105x, primary_geomean=1.593x
speedup of candidate vs main: cold=1.001x, noop=1.161x, leaf=1.040x, swarm=1.003x, hub=1.004x, primary_geomean=1.050x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.023x over 1 runs, threshold 7%; medians main 35.57s, candidate 34.77s)
verdict medium-deep/noop: improved (paired speedup 1.202x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.06s)
verdict medium-deep/leaf: same (paired speedup 1.002x over 10 runs, threshold 7%; medians main 0.34s, candidate 0.32s)
verdict medium-deep/swarm: same (paired speedup 1.002x over 3 runs, threshold 3%; medians main 14.36s, candidate 14.33s)
verdict medium-deep/hub: same (paired speedup 1.004x over 3 runs, threshold 3%; medians main 34.63s, candidate 34.43s)
verdict medium-layered/cold: same (paired speedup 1.014x over 1 runs, threshold 7%; medians main 39.16s, candidate 38.62s)
verdict medium-layered/noop: improved (paired speedup 1.077x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.07s)
verdict medium-layered/leaf: improved (paired speedup 1.072x over 10 runs, threshold 7%; medians main 0.36s, candidate 0.34s)
verdict medium-layered/swarm: same (paired speedup 1.022x over 3 runs, threshold 3%; medians main 14.44s, candidate 13.91s)
verdict medium-layered/hub: same (paired speedup 0.992x over 3 runs, threshold 3%; medians main 32.05s, candidate 32.30s)
verdict medium-wide/cold: same (paired speedup 0.967x over 1 runs, threshold 7%; medians main 31.25s, candidate 32.32s)
verdict medium-wide/noop: improved (paired speedup 1.118x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.07s)
verdict medium-wide/leaf: same (paired speedup 1.046x over 10 runs, threshold 7%; medians main 0.18s, candidate 0.18s)
verdict medium-wide/swarm: same (paired speedup 0.992x over 10 runs, threshold 7%; medians main 0.83s, candidate 0.85s)
verdict medium-wide/hub: same (paired speedup 0.995x over 3 runs, threshold 3%; medians main 32.23s, candidate 31.80s)
verdict: success
```

Held-out check: `correctness of candidate: identical artifacts to lake, all builds
ok, break check ok`.

## Conclusion

Deferring the builtin parsers, and the few Lake modules that need them at
initialization, takes ~10 ms more off every TOML `lake build`. That is a fifth of a
direct `noop` (44.5 to 34.9 ms). In the harness `noop` improved on every corpus
(1.08-1.20x), and `leaf` gained 0-7%. Part of the gain is beyond start-up itself:
with fewer modules initialized, fewer environment extensions are registered, so the
environment that `loadToml` creates for the TOML parser is cheaper too.

Together with `deferred-init`, a TOML build now initializes neither the elaborator
nor the parsers. Stock Lake's `noop` is 2.6x slower than Blake's on these corpora.

Caveats:
- **The `DEFERRED` rule is now broader:** a deferred module's code (Lean's builtin
  parser modules, the DSL, the Lean config loader, the macro-only config helpers)
  must not run on the `build` path before `fullInitialize`.
- **How a violation shows:**
  - Code that reads a deferred module's initialized globals crashes. The first
    prototype crashed at start-up this way, so it is not silent.
  - Code that relies on the registered builtin parser tables would see them empty.
    For example, pretty-printing Lean syntax in a message. Nothing on the TOML build
    path does that today; the suite and the error-output comparisons above agree.
- **Keep the two lists in sync:** `DEFERRED` in `c/blake_init.c` and the `--wrap`
  list in `lakefile.lean`.

What is left of a TOML `noop` (~35 ms on P-cores): ~5-8 ms of start-up (Lake's own
modules and the light Lean ones), and the fetch thread's job graph (~25 ms), which
`bucket-store` flattened. Its profile is now spread over allocation, freeing, path
and name strings, and job plumbing.
