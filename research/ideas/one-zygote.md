# one-zygote: one `blake-zygote` per build, forking a child for every compile concurrently

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/2026-10-30-one-zygote.md
- **Scenarios improved:** swarm, hub (all corpora); cold slightly

## Problem

After [fork-lean](fork-lean.md), Blake ran a pool of zygotes, one per concurrent
compile, each serving one child at a time. A build compiling many modules at once
started up to 16 zygotes, each paying start-up and initialization, and every
compile slot had its own copy of Lean's initialized state. The early-started
zygote served only the first compile.

## Mechanism

`blake-zygote` (`c/blake_zygote.c`) is now a single-threaded multiplexing server:
`poll` on stdin (requests: an id, the stdout and stderr files, `LEAN_PATH`, and
`argv`, NUL-separated) and on a self-pipe written by its `SIGCHLD` handler. Each
request is forked into its own child at once (the child runs the steps of
`lean_main` after initialization, ending in `lean_shell_main`, as before). When a
child exits, the server replies `<id> <exit code>`. A stop request or end of input
makes it wait for its children and exit.

Lake (`Lake/Build/Actions.lean`) starts one zygote per build (`getZygote`, under a
lock; `prestartZygote` from `LeanLib.recBuildLean` when some source is newer than
its trace). Each compile registers an `IO.Promise` under a fresh id, writes its
request under a mutex, and waits for its reply on a `Std.Condvar` that the reader
task signals after resolving the promise. The reader fails all pending compiles if
the zygote's output ends, and they then run `lean` directly. `stopZygotes` sends the
stop request and waits for the reader and the process.

The wait must be on the condition variable, never `IO.wait`. A job blocked in a task
wait makes Lean's task manager start another worker, so Lake's
`LEAN_NUM_THREADS` limit stops bounding the number of compiles. The first version
did exactly that and peaked at 19.8 GB in a cold build.

## Where it lives

`c/blake_zygote.c` (`main`, `spawn`, `reap`, `parse_request`, `child_main`);
`Lake/Build/Actions.lean` (`Zygote`, `getZygote`, `readZygoteReplies`,
`runLeanZygote`, `prestartZygote`, `stopZygotes`).

## Measured gains

Standard sweep on machine `2e698309` (i9-14900K, 16 CPUs, 32 GB cap), medium
corpora, result `research/results/20261031T043239Z-2e698309-216c449.json`:

```
speedup of candidate vs main: cold=1.053x, noop=1.035x, leaf=0.989x, swarm=1.057x, hub=1.069x, primary_geomean=1.037x
speedup of candidate vs lake: cold=1.075x, noop=2.016x, leaf=1.299x, swarm=1.097x, hub=1.094x, primary_geomean=1.331x
```

Improved: `swarm` on all corpora (1.031x deep, 1.079x layered, 1.080x wide) and
`hub` on all corpora (1.054x, 1.067x, 1.068x). Cold builds used 4-7% less CPU and
less memory.

## Caveats and follow-ups

- Lean's initialized pages are shared copy-on-write by all children; a child that
  writes to them pays page faults. Marking the initialization's objects persistent
  in the zygote before forking might reduce that, but `lean_main` runs
  `lean_init_search_path` and option processing before
  `lean_io_mark_end_initialization`.
- The early start scans every module's source and trace mtimes on a dedicated
  thread. Cheaper triggers are possible.
- Windows: see [fork-lean](fork-lean.md); the build leaves a placeholder and Blake
  runs `lean` directly.
