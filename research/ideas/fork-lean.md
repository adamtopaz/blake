# fork-lean: compile modules in forked children of a pre-initialized `lean` driver

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/2026-10-30-fork-lean.md
- **Scenarios improved:** swarm (`deep`), hub (`wide`); leaf, hub, and cold slightly elsewhere

## Problem

Every module Lake compiles is a fresh `lean` process. Before it reads the module's
source, that process `exec`s, dynamically links `libleanshared`, and runs Lean's
initialization (every builtin parser, attribute, and extension; `--profile`
reports `initialization` 20-33 ms). In `hub`, `cold`, and chain-bound `swarm`
builds this is paid once per module, and on the critical path of every step of a
chain.

## Mechanism

The `lean` executable is a thin C++ wrapper (`lean_main` in `src/util/shell.cpp`,
v4.35.0) around Lean code. After `lean::initializer` it calls
`lean_init_search_path`, `lean_enable_initializer_execution`, parses options with
`lean_shell_options_process`, calls `lean_io_mark_end_initialization`, starts a
task manager (`lean_init_task_manager_using`), and runs `lean_shell_main`
(`Lean/Shell.lean`) on a thread with a large stack. All of these are exported by
`libleanshared.so`.

`blake-zygote` (`c/blake_zygote.c`) is a small C program linked against the
toolchain's `libleanshared`. It calls `lean_initialize()` once and then serves
requests on stdin. For each request it `fork`s; the child redirects stdout and
stderr to files named in the request, sets `LEAN_PATH`, and performs the steps
above with the request's `argv`. The parent waits and replies with the exit code.
So each module still runs in its own process (crashes, memory, and global state
stay isolated), and it runs exactly `lean`'s code, minus the start-up.

Blake itself cannot be the forking parent: it has also initialized Lake's own
modules, whose registered environment extensions would be visible to the child
and could end up in the `.olean`s it writes. The zygote initializes only what
`lean` initializes.

In Lake (`Lake/Build/Actions.lean`): `compileLeanModule` asks `zygoteFor?`
whether `blake-zygote` sits next to Blake's binary and the toolchain's `Init.olean`
records Blake's own `Lean.githash`. If so, it takes an idle zygote from a pool (or
starts one), sends the request, reads the outputs back, and returns them as if
from `IO.Process.output`. On any failure it runs `lean` itself. It logs the same
verbose command line either way. Because the zygote's built-in search path is
relative to its own location, the toolchain's `lib/lean` (which `lean` appends
last) is appended to `LEAN_PATH`. `stopZygotes` ends every zygote when
`runBuild`/`runFetchM` finish, so none outlives Lake.
`LeanLib.recBuildLean` starts one zygote early, in the background, when some
module's source is newer than its trace, so that a leaf edit does not pay the
zygote's own initialization on its critical path.

## Where it lives

`c/blake_zygote.c`; the `blakeZygote` target and the `lake` executable's `needs`
in `lakefile.lean`; `Lake/Build/Actions.lean` (`zygoteFor?`, `runLeanZygote`,
`prestartZygote`, `stopZygotes`, and the call in `compileLeanModule`);
`Lake/Build/Run.lean` (`stopZygotes` calls); `Lake/Build/Library.lean` (the early
start); `Lake/Config/InstallPath.lean` (`initOleanHasLeanGithash` made public).

## Measured gains

Two standard sweeps on machine `2e698309` (i9-14900K, 16 CPUs, 32 GB cap), medium
corpora; results `research/results/20261031T003346Z-2e698309-9a95eef.json` and
`research/results/20261031T011544Z-2e698309-ad55938.json` (the merged code):

```
speedup of candidate vs main: cold=1.004x, noop=0.991x, leaf=1.038x, swarm=0.995x, hub=1.014x, primary_geomean=1.009x
speedup of candidate vs main: cold=1.022x, noop=0.980x, leaf=1.043x, swarm=1.010x, hub=1.032x, primary_geomean=1.016x
```

Improved: `swarm` on `medium-deep` (1.043x, first sweep) and `hub` on
`medium-wide` (1.050x, second sweep). Consistently a little faster but under the
thresholds: `leaf` 1.03-1.05x on every corpus, `hub` 1.01-1.03x elsewhere. A
little slower, within thresholds: `swarm` on `medium-wide` (0.968x in both) and
`noop` (0.94-0.98x in the second sweep); see the log entry for why.

## Caveats and follow-ups

- Only the start-up is saved. `import` (~40% of a deep module's run) is still paid
  in every child. Sharing an imported environment across modules would need Lean
  internals.
- The zygote mirrors `lean_main`'s option table and start-up sequence for Lean
  v4.35.0. A toolchain bump must re-check `src/util/shell.cpp`; the githash guard
  already keeps any other Lean on the old path.
- Not done: honoring `LEAN_STACK_SIZE`/`--tstack` for worker threads the way
  `set_thread_stack_size_from_env` does (the child's main thread gets a 64 MB
  stack).
- **Windows:** `c/blake_zygote.c` uses `fork`/`waitpid`, so building Blake on
  Windows will fail at the `blakeZygote` target. Linux (the reference platform, and
  CI) and macOS are fine. A follow-up `fix:` should compile the zygote only on
  POSIX platforms (or stub it) and make `zygoteFor?` return `none` on Windows.
