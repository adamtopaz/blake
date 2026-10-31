# deferred-init: initialize only the parts of Lean a TOML `lake build` needs; the rest on demand

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/2026-10-31-deferred-init.md (investigation: ../log/2026-10-31-lazy-init.md)
- **Scenarios improved:** noop (`layered`, `wide`); every invocation ~12 ms faster

## Problem

About 30 ms of every Blake invocation, a third of `noop`, is Lean's module
initialization. The dynamic loader takes ~0.25 ms and `strace` shows ~20 ms of pure
computation. Two things make it initialize all of Lean:

- Every module of Lake that imports a `Lean` module calls `lean_initialize()` from
  its compiler-generated runtime initializer. That function runs `initialize_Init`,
  `initialize_Std` and `initialize_Lean`, then the C++ kernel and library modules.
- `Lake.CLI.Main` imports `LeanExport.Basic`, which does `public import Lean`.

A TOML `lake build` uses neither. Lake's own imports (`Lake.Load.Toml`,
`Lake.Build.*`, the elaborator frontend in `Lake.Load.Lean`) already cover
everything it runs.

## Mechanism

`c/blake_init.c`, linked into the `lake` executable. On Linux the executable is
linked with GNU `ld`'s `--wrap` for four symbols:

- `lean_initialize`: at start-up it does only what precedes `initialize_Init` in
  `lean_initialize()`: `save_stack_info`, the runtime, and the C++ utility modules
  (ascii, name, name generator, options).
- `runtime_initialize_LeanExport_Basic` and `runtime_initialize_Lake_CLI_Check`
  (the only modules that bring in `LeanExport`): do nothing at start-up.
- `lean_io_initializing`: reports true while the deferred initialization runs.
  Builtin initializers register attributes and environment extensions, which
  `Lean.initializing` allows only then.

`blake_full_initialize` runs the rest in `lean_initialize()`'s order:
`initialize_Init`/`Std`/`Lean`, the kernel, the default print function, the library
and constructions modules, then the two deferred initializers. Lake reaches it
through `fullInitialize` (`Lake/Util/FullInit.lean`); `LakeMain` sets
`fullInitializeRef` to the C function, so Lake's shared library has no reference to
it. It runs:

- before every command other than `build` (`lakeCli`), and
- before loading a configuration written in Lean (`importConfigFile`), for the root
  or any dependency.

So everything except a TOML `lake build` behaves exactly as before, only later. The
build itself runs `lean` and the zygote as before; they are separate programs, so
artifacts cannot change.

## Where it lives

- `c/blake_init.c`.
- `lakefile.lean`: the `blakeInit` target; `moreLinkObjs` and the Linux-only
  `moreLinkArgs` of `lean_exe lake`.
- `Lake/Util/FullInit.lean`; `LakeMain.lean`.
- `lakeCli` in `Lake/CLI/Main.lean`; `importConfigFile` in
  `Lake/Load/Lean/Elab.lean`.

## Measured gains

Standard sweep on machine `2e698309` (i9-14900K, 16 CPUs, 32 GB cap), result
`research/results/20261031T153330Z-2e698309-782fae0.json`:

```
speedup of candidate vs main: cold=0.997x, noop=1.143x, leaf=1.014x, swarm=1.017x, hub=0.996x, primary_geomean=1.041x
```

`noop` improved on `layered` (1.126x) and `wide` (1.177x); on `deep` it was 1.038x,
within the noise of the harness's ~70-105 ms `noop` runs. An earlier sweep of the
same code (`20261031T145513Z-2e698309-15f4807.json`) gave 1.111x, 1.111x and 1.001x.
Direct `noop` on `medium-deep`, 30 runs each: 73.2 ms vs 85.1 ms.

## Caveats and follow-ups

- **Platforms:** Linux only (GNU `ld`'s `--wrap`). macOS and Windows keep stock
  behavior: `blake_full_initialize` exists there but does nothing.
- **Toolchain coupling:** the C++ pieces of `lean_initialize()` are called by their
  mangled names for Lean v4.35.0. A toolchain bump must re-check
  `src/initialize/init.cpp`; a renamed symbol fails the link, not the run.
- **Lean 4.35 ABI:** `BaseIO` externs take no world token and return unboxed
  values (`lean_io_initializing` returns `uint8_t`).
- **Rule for the `build` path:** code there must not use a module reachable only
  through `LeanExport`, nor Lean's C++ kernel, without calling `fullInitialize`
  first. A deferred module's closed terms are null until it is initialized, so such
  a bug crashes rather than misbehaves.
- **Next:**
  - A TOML build still initializes ~950 modules; ~14 ms of it is
    `Lean.Parser.Module`'s builtin parsers, reached through
    `Lean.Elab.ParseImportsFast` and `Lake.Toml`. Avoiding those is the next step.
  - Deferring Lake's own heavy modules (`Lake.Load.Lean`, `Lake.CLI.Translate`,
    `BuiltinLint`, `Shake`, `Serve`, `Init`) works the same way and may save ~5 ms
    more.
