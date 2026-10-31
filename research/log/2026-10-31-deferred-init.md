# deferred-init: initialize only the parts of Lean a TOML `lake build` needs; the rest on demand

- **Date:** 2026-10-31
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** success
- **Branch / PR:** research/deferred-init — https://github.com/adamtopaz/blake/pull/15
- **Result files:** research/results/20261031T145513Z-2e698309-15f4807.json (run label `lazy-init`, the working name)

## Hypothesis

`lazy-init` (log `2026-10-31-lazy-init.md`) found that ~30 ms of every Blake
invocation, a third of `noop`, is Lean's module initialization, and that most of it
initializes Lean machinery a TOML `lake build` never uses. Two things cause it:

- Every module of Lake that imports a `Lean` module calls `lean_initialize()` from
  its runtime initializer (emitted by the compiler). That function initializes all of
  Lean: `initialize_Lean`, plus the C++ kernel and library.
- `Lake.CLI.Main` imports `LeanExport.Basic` (for `lake check`/`comparator`), which
  does `public import Lean`: 1130 modules beyond what even elaborating a
  `lakefile.lean` needs.

That attempt concluded that a real fix needed a restructured "light root". A closer
look showed a smaller cut is enough. Wrapping `lean_initialize` and the runtime
initializers of the two modules that bring in `LeanExport` (`LeanExport.Basic`,
`Lake.CLI.Check`) leaves Lake's remaining imports to initialize exactly what Lake's
code uses. The rest can be finished on demand. A relinked prototype took `noop` on a
scratch copy of `medium-deep` from 85.6 to 73.6 ms.

Expected: `noop` 10-15% faster on every corpus; `leaf` and every other scenario a
few percent (the ~12 ms is paid once per build); artifacts untouched (`lean` runs
exactly as before).

## What was done

- **`c/blake_init.c`** (new). On Linux the `lake` executable is linked with
  `--wrap` for four symbols.
  - `lean_initialize`: at start-up it now runs only what precedes
    `initialize_Init` in `lean_initialize()`: `save_stack_info`, the runtime, and
    the C++ utility modules (ascii, name, name generator, options).
  - `runtime_initialize_LeanExport_Basic` and `runtime_initialize_Lake_CLI_Check`:
    do nothing at start-up.
  - `lean_io_initializing`: reports true while the deferred initialization runs,
    because attributes and environment extensions may only be registered then
    (`Lean.initializing`).
  - `blake_full_initialize` does the rest, in `lean_initialize()`'s order:
    `initialize_Init`/`Std`/`Lean`, the kernel, the default print function, the
    library and constructions modules, then the two deferred initializers. The C++
    pieces are called by their mangled names for Lean v4.35.0.
  - On other platforms nothing is wrapped and `blake_full_initialize` does nothing.
- **`Lake/Util/FullInit.lean`** (new): `fullInitialize` runs the action in
  `fullInitializeRef`. `LakeMain` sets it to the C function (`@[extern
  "blake_full_initialize"]`), so only the executable references the symbol and
  Lake's shared library links as before.
- **When full initialization runs:** before every command other than `build`
  (`lakeCli` in `Lake/CLI/Main.lean`), and before loading a configuration written
  in Lean (`importConfigFile` in `Lake/Load/Lean/Elab.lean`). So `lake build` in a
  workspace with a `lakefile.lean`, root or dependency, still initializes
  everything, just later.
- **`lakefile.lean`:** target `blakeInit` compiles `c/blake_init.c` with Lean's own
  C compiler (it needs only `lean.h`). `lean_exe lake` gets
  `moreLinkObjs := #[blakeInit]` and, except on Windows and macOS, the `--wrap`
  arguments.

Why it is safe:
- Every module initialized at start-up still has its imports initialized (the
  generated initializers recurse), except `Lake.CLI.Main`, whose imports
  `LeanExport.Basic` and `Lake.CLI.Check` are deferred. It calls into them only in
  the `check`/`comparator`/export commands, which initialize fully first.
- A TOML `lake build` creates a Lean environment (the TOML parser runs on
  `Lean.Parser`) from the extensions registered by the modules it imports, and that
  works. Nothing in it imports `.olean`s in-process.
- `lean` and the zygote are separate programs, untouched, so artifacts cannot change.

The first build failed every full initialization with "extensions can only be
registered during initialization". In Lean 4.35, `BaseIO` externs take no world
token and return unboxed values, so `lean_io_initializing` returns a `uint8_t`
(the wrapper and `blake_full_initialize` now follow that ABI).

Checked by hand:
- A TOML `noop` and an edit work, and `lake env` works. A `lakefile.lean` package
  builds, as does `check-build`.
- `noop` on a scratch copy of `medium-deep`, 30 runs each (CPUs 16-31): 73.2 ms vs
  85.1 ms for `main`.
- `nix develop -c tests/run_all.sh --compare .out-main`: 93 passed, 0 failed (base
  on `09b5bcf`: 93 passed). The suite runs many `lakefile.lean` workspaces and every
  command, through the full-initialization path.
- Quick harness: `correctness of candidate: identical artifacts to lake, all builds
  ok, break check ok`; verdict `success` (`noop` improved 1.100x, 1.145x, 1.131x on
  the small corpora).

## Measurements

Standard sweep on `15f4807`: 16 CPUs (affinity), 32 GB (systemd MemoryMax),
`LEAN_NUM_THREADS=16`, 10 pairs for fast scenarios and 3 for the rest; one run.

```
speedup of main vs lake: cold=1.081x, noop=2.068x, leaf=1.698x, swarm=1.168x, hub=1.087x, primary_geomean=1.453x
speedup of candidate vs lake: cold=1.096x, noop=2.168x, leaf=1.759x, swarm=1.191x, hub=1.088x, primary_geomean=1.491x
speedup of candidate vs main: cold=1.014x, noop=1.048x, leaf=1.036x, swarm=1.020x, hub=1.001x, primary_geomean=1.026x
correctness of main: identical artifacts to lake, all builds ok, break check ok
correctness of candidate: identical artifacts to lake, all builds ok, break check ok
verdict medium-deep/cold: same (paired speedup 1.002x over 1 runs, threshold 7%; medians main 35.82s, candidate 35.76s)
verdict medium-deep/noop: same (paired speedup 1.001x over 10 runs, threshold 7%; medians main 0.08s, candidate 0.08s)
verdict medium-deep/leaf: same (paired speedup 1.021x over 10 runs, threshold 7%; medians main 0.35s, candidate 0.34s)
verdict medium-deep/swarm: same (paired speedup 1.018x over 3 runs, threshold 3%; medians main 14.59s, candidate 14.40s)
verdict medium-deep/hub: same (paired speedup 1.007x over 3 runs, threshold 3%; medians main 34.28s, candidate 33.95s)
verdict medium-layered/cold: same (paired speedup 1.033x over 1 runs, threshold 7%; medians main 36.53s, candidate 35.38s)
verdict medium-layered/noop: improved (paired speedup 1.111x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.09s)
verdict medium-layered/leaf: same (paired speedup 1.040x over 10 runs, threshold 7%; medians main 0.37s, candidate 0.34s)
verdict medium-layered/swarm: same (paired speedup 1.010x over 3 runs, threshold 3%; medians main 13.47s, candidate 12.94s)
verdict medium-layered/hub: same (paired speedup 1.001x over 3 runs, threshold 3%; medians main 32.05s, candidate 32.09s)
verdict medium-wide/cold: same (paired speedup 1.007x over 1 runs, threshold 7%; medians main 31.88s, candidate 31.67s)
verdict medium-wide/noop: improved (paired speedup 1.111x over 10 runs, threshold 7%; medians main 0.09s, candidate 0.08s)
verdict medium-wide/leaf: same (paired speedup 0.993x over 10 runs, threshold 7%; medians main 0.19s, candidate 0.19s)
verdict medium-wide/swarm: same (paired speedup 1.015x over 10 runs, threshold 7%; medians main 0.90s, candidate 0.89s)
verdict medium-wide/hub: same (paired speedup 0.994x over 3 runs, threshold 3%; medians main 31.96s, candidate 32.15s)
verdict: success
```

The `noop` runs themselves (ms) show how noisy the harness's `noop` is next to a
~12 ms effect. The two tools are interleaved and the speedup is the median of the
paired ratios:

```
medium-deep    main 91 72 81 101 73 85 91 79 80 96   cand 84 84 107 83 92 71 85 99 85 76
medium-layered main 83 81 105 91 90 86 96 97 98 77   cand 91 74 90 84 88 70 75 83 88 87
medium-wide    main 83 83 87 80 97 90 84 88 94 87    cand 77 72 92 66 79 76 81 95 70 94
```

Held-out check: `correctness of candidate: identical artifacts to lake, all builds
ok, break check ok`.

## Conclusion

Initializing only what a TOML `lake build` uses saves ~12 ms per invocation (85 ms
becomes 73 ms on a direct `noop`). That is a tenth of `noop`, improved on `layered`
and `wide` (1.111x each), lost in the noise on `deep`, and 2-4% elsewhere. Nothing
else changes: `lean` runs as before, and commands other than `build`, and
`lakefile.lean` workspaces, initialize everything as stock Lake does.

What remains of start-up, measured in `lazy-init`:
- A TOML build still initializes ~950 modules. The biggest share is
  `Lean.Parser.Module`'s builtin parsers (~14 ms on its own), which come in through
  `Lean.Elab.ParseImportsFast` (header reading) and `Lake.Toml` (the TOML grammar
  is built on `Lean.Parser`). Reading headers without `Lean.Parser.Module`, and
  checking what `Lake.Toml` really needs, is the next step.
- Deferring more of Lake's own heavy modules (`Lake.Load.Lean` and the elaborator
  frontend, ~190 modules; `Lake.CLI.Translate`, `BuiltinLint`, `Shake`, `Serve`,
  `Init`) works the same way: wrap their runtime initializers and call them from
  `blake_full_initialize`. The prototype numbers in `lazy-init` suggest ~5 ms more.

Caveats:
- The wrapped C++ functions are named by their Itanium-mangled names, which are
  specific to the Lean toolchain. A toolchain bump must re-check `lean_initialize()`
  in `src/initialize/init.cpp`; a missing symbol fails the link, not the run.
- The wrapping is GNU `ld`'s `--wrap`, so Linux only. macOS and Windows keep stock
  behavior.
- A future change that makes the `build` path use a deferred module (one reachable
  only through `LeanExport`, or Lean's C++ kernel) must call `fullInitialize` first.
  A deferred module's closed terms are null until then, so such a bug crashes
  rather than misbehaves.
