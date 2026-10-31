# defer-parsers: also defer Lean's builtin parsers and the Lake modules that need them

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/2026-10-31-defer-parsers.md (builds on ../ideas/deferred-init.md)
- **Scenarios improved:** noop (all corpora); leaf (`layered`)

## Problem

After `deferred-init`, a TOML `lake build` still spent ~15 ms initializing modules.
About 60% of that was registering Lean's builtin parsers: the runtime initializers of
`Lean.Parser.Command`, `Do` and `Term` (`addBuiltinParser`, token and kind tables).
Several of Lake's imports reach them:
- the lakefile DSL (`Lake.DSL`, through the elaborator);
- the Lean configuration loader (`Lake.Load.Lean`);
- `ParseImportsFast` (through `Lean.Parser.Module`);
- the TOML grammar (`import Lean.Parser`);
- two macro-only helpers (`Lake.Config.Meta`, `Lake.Util.Binder`).

A TOML build uses none of these registrations. The TOML grammar runs Lake's own
combinators with an empty token table, and `ParseImportsFast` is a hand-written
parser.

## Mechanism

The same `--wrap` deferral as `deferred-init` (`c/blake_init.c`), with the deferred
runtime initializers kept in one list, `DEFERRED`:

- Lean's builtin parser modules: `Lean.Parser.Term`, `Do`, `Command`, `Tactic`,
  `Module`, and `Lean.Parser`;
- `LeanExport.Basic` and `Lake.CLI.Check` (from `deferred-init`);
- `Lake.Util.Binder` and `Lake.Config.Meta`, which build parsers at initialization
  and are used only by macros;
- `Lake.DSL` and `Lake.Load.Lean`;
- the commands `translate-config`, `lint`, `shake`, `serve` and `new`/`init`.

At start-up these initializers do nothing; a module imported only through them stays
uninitialized. `blake_full_initialize` (before any command other than `build`, and
before loading a Lean configuration) runs:
1. `initialize_Init`/`Std`/`Lean`, which initialize every Lean module fully,
   including the parsers, whose own runtime initializers are called directly rather
   than through the wrapped symbols;
2. the real initializers of the Lake modules in `DEFERRED` order.

Fewer initialized modules also means fewer registered environment extensions, so the
environment that `loadToml` creates for the TOML parser is cheaper.

## Where it lives

- `c/blake_init.c`: `DEFERRED`, `DEFINE_WRAP`, `blake_full_initialize`.
- `lakefile.lean`: the matching `--wrap` list in `lean_exe lake`'s `moreLinkArgs`.

## Measured gains

Standard sweep on machine `2e698309` (i9-14900K, 16 CPUs, 32 GB cap), result
`research/results/20261031T184727Z-2e698309-3df6fda.json`:

```
speedup of candidate vs main: cold=1.001x, noop=1.161x, leaf=1.040x, swarm=1.003x, hub=1.004x, primary_geomean=1.050x
```

- `noop` improved on `deep` (1.202x), `layered` (1.077x) and `wide` (1.118x).
- `leaf` improved on `layered` (1.072x).
- Direct `noop` on `medium-deep` (P-cores): 44.5 ms becomes 34.9 ms.

## Caveats and follow-ups

- **What may not run early:** a deferred module's code must not run on the `build`
  path before `fullInitialize`, nor anything that relies on the builtin parser
  tables (for example, pretty-printing Lean syntax).
  - Reading a deferred module's initialized globals crashes; this happened in the
    first prototype.
  - Relying on the tables finds them empty.
- **Keeping the lists in sync:** `DEFERRED` and the lakefile's `--wrap` list must
  match. A module in the C list but not the lakefile fails the link, because
  `__real_…` is undefined. A module in the lakefile but not the C list also fails
  the link, because `__wrap_…` is undefined.
- **Remaining start-up:** a few ms for Lake's own modules and the light Lean ones
  (`Lean.Environment`, JSON, `Lean.PrettyPrinter.Formatter`/`Parenthesizer` for the
  TOML grammar). The rest of `noop` is the fetch thread's job graph.
