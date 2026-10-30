# parallel-discovery: read and parse module headers in parallel while collecting a library's modules

- **Merged:** YYYY-MM-DD, PR <URL> (filled in by the maintainer on merge)
- **Log entry:** ../log/2026-10-30-parallel-discovery.md
- **Scenarios improved:** noop

## Problem

Every `lake build` of a library first collects its modules
(`LeanLib.recCollectLocalModules`, the `modules` facet): the modules matched by its
roots and globs plus their local transitive imports. Only then does
`LeanLib.recBuildLean` fetch each module's artifacts, which is where trace
checking (and, if needed, `lean`) happens. The collection walks the import graph
depth-first and, for each module, fetches its `imports` facet and waits for it.
That facet depends on the module's `input` job, which reads the `.lean` file,
hashes it (`Hash.ofText`, which first normalizes line endings with
`String.crlfToLf`), and parses its header with `parseImports'`. So the headers were processed about
one at a time, all on the critical path.

`perf record --latency` of a no-op build of `medium-wide` (1000 modules), with
samples bucketed by time: ~35 ms of single-threaded startup, then ~35 ms with
3-7 busy threads, one of them in `recCollectLocalModules` (66% of its samples)
and two or three in `recFetchInput`, then ~40 ms with ~17 busy threads checking
traces.

## Mechanism

Fetch the `imports` facet of every root module before the walk starts, and,
inside the walk, fetch those of a module's not-yet-visited same-library imports
before descending into them. Fetches are memoized in the build store and the
walk would fetch exactly these jobs anyway, so the set of jobs, their results,
the order of the collected modules, and the logs are all unchanged. Only the
jobs' start times move: the header jobs now run on all workers while the walk
waits on the first of them, and by the time it gets to a module its header is
usually done.

## Where it lives

`Lake/Build/Library.lean`: `LeanLib.recCollectLocalModules` and its `go`.

## Measured gains

Standard sweep on machine `2e698309` (i9-14900K, 16 CPUs, 32 GB cap), medium
corpora, result `research/results/20261030T163740Z-2e698309-11d39d8.json`:

```
speedup of candidate vs main: cold=1.049x, noop=1.177x, leaf=1.044x, swarm=1.006x, hub=1.003x, primary_geomean=1.055x
speedup of candidate vs lake: cold=1.011x, noop=1.846x, leaf=1.317x, swarm=1.026x, hub=1.011x, primary_geomean=1.260x
```

`noop` improved on all corpora (paired 1.181x deep, 1.304x layered, 1.150x wide);
`leaf` 1.02-1.06x, under the threshold. (The `cold` figure, and the "improved"
verdict for `medium-layered/cold`, come from one slow `main` cold run; see the
log entry.)

By hand on `medium-wide` (20 alternating pairs against stock Lake, CPUs 16-31): a
no-op of the whole library 0.091 s vs Lake's 0.141 s (1.56x; `main` was 1.24x).

## Caveats and follow-ups

- Each header job still does more than it needs to: `Hash.ofText` runs
  `String.crlfToLf`, which decodes the whole file character by character to look
  for `\r\n` (about 20% of a header job's samples; it returns the string
  unchanged when there is none), and `IO.FS.readFile` validates UTF-8 and counts
  code points. A byte-level scan for `\r` before the character walk would keep
  hashes, and so traces, identical.
- The remaining single-threaded part of a no-op is process startup: ~25-30 ms
  between exec and the first file access, spent in the module initializers of
  everything Blake links (Lean's parser tables among them).
