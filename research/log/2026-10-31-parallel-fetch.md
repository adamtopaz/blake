# parallel-fetch: what is left of a `leaf` build, and fetching a library's modules in parallel

- **Date:** 2026-10-31
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** inconclusive (prototype only: it creates duplicate jobs, so it was not measured with the harness)
- **Branch / PR:** research/parallel-fetch-log (prototype: research/parallel-fetch-wip, https://github.com/adamtopaz/blake/tree/research/parallel-fetch-wip) — PR TBD
- **Result files:** none

## Hypothesis

After `deferred-init`, `bucket-store` and `defer-parsers`, a TOML `noop` on
`medium-deep` takes ~35-45 ms. Of that, ~25 ms is the fetch thread building the job
graph, and its profile is now flat. A `leaf` build pays the same before its one
compile. An earlier attempt (`adaptive-lean-threads`) found that creating jobs from
several threads did not help, because the copy-on-write build store serialized
them. `bucket-store` removed that, so fetching a library's modules from several jobs
in parallel might now shorten both `noop` and `leaf`.

## What was done

**1. Where a `leaf` build's time goes now.** Scratch copy of `medium-deep`, CPUs
0-15, leaf `Corpus.M00999` resumed from its header snapshot. Times are from the
outputs' modification times, relative to the start of `lake build`, over 6 builds:

```
compile requested (setup.json written)  41-57 ms
.olean written                         296-454 ms   (the compile: ~250-290 ms)
.c written                              +5 ms
trace written                           +9 ms
build finished                          +9-14 ms
```

- In `perf` of such builds, 82% of samples are the zygote child and 18% Lake. The
  child's time is Lean's elaborator and kernel on the module's own declarations
  (`replace_rec_fn`, `instantiate`, `whnf`, allocation and freeing), which Blake
  cannot change.
- The compile is requested once the module's imports have been checked: that is
  the `noop` part, plus ~2 ms for the setup JSON (106 KB for this module: 589
  imported artifact paths; `Json.pretty` takes 1.4 ms) and the header-snapshot key.
- At the end, an idle zygote takes 4.3 ms to exit after `stop`. That is the kernel
  tearing down its address space: exiting with `_exit` instead of `return` made no
  difference.
- A child's exit after its last output is fast (well under a millisecond in
  `strace`).

So the parts of a `leaf` build Blake still controls are the ~40-50 ms before the
compile and ~20 ms after it. The compile can start no earlier than the module's
imports are checked, unless it starts speculatively; see the conclusion.

**2. Prototype: parallel fetch** (`research/parallel-fetch-wip`, commit on top of
`a8959f4`).
- `LeanLib.recBuildLean` first fetches its modules' `leanArts` from 8 `Job.async`
  jobs (modules interleaved over the chunks), waits for them, then folds the
  module jobs in the original order as before, so the library's trace is unchanged.
- Results on the scratch `medium-deep` (CPUs 0-15, 30 alternating pairs against
  `main` at `4836f6b`):
  - `noop` wall 44.7 vs 40.2 ms (paired 1.091x), but CPU 140 vs 246 ms;
  - `lake build` reports 1219 jobs on a `noop` and 1248 on a `leaf`, against
    `main`'s 1002.
- The extra jobs are duplicates. `fetchOrCreate` is `fetch?` then `store`, so two
  threads that both miss a key both create its job. On a single fetch thread that
  window was harmless; with several it is hit hundreds of times. In a real build that
  would mean the same module compiled twice at once.

Not run: the test suite and the harness. The prototype is incorrect as it stands.

## Measurements

None with the harness (see above).

## Conclusion

Parallel job creation now gains something (~10% on `noop`), even with the wasted
duplicate work. But it is far from proportional to the threads, since modules'
facets wait on their imports' facets. It needs atomic memoization first:
`fetchOrCreate` must insert a placeholder (for example a job backed by an
`IO.Promise`) under the bucket's reference before creating the real job, and resolve
it afterwards, including on failure. Cycle detection stays per thread (the call
stack), which is enough for acyclic graphs. That is a change to Lake's core
memoization semantics. It is worth doing only if a corrected prototype keeps or
improves the 1.09x.

For `leaf`, the remaining large step would be a speculative compile. When an edited
module's imports have no edited module below them, start its compile at module
discovery, with the previous `setup.json`, writing to temporary outputs. Adopt the
result once the normal path confirms the same command, setup, source and unchanged
import `.olean`s; otherwise discard it. That could save ~30-35 ms of a ~330 ms `leaf`
build (~10%). It is intricate (the header-snapshot decision, message replay, outputs
written aside and moved in) and was not attempted here.
