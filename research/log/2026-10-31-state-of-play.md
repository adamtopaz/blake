# state-of-play: where Blake's remaining time goes, and what is left to try

- **Date:** 2026-10-31
- **Author:** Claude (Opus 5.5) with Adam Topaz, machine `2e698309` (i9-14900K, 16 of 32 CPUs, 32 GB cap)
- **Outcome:** inconclusive (analysis only; no change implemented)
- **Branch / PR:** research/state-of-play-log — PR TBD
- **Result files:** none

## Hypothesis

The three previous iterations (`critical-nice`, `parallel-fetch`, `spec-compile`)
produced no measurable gain. This iteration set out to make `parallel-fetch` correct
(atomic memoization in the bucketed build store, so modules can be fetched from
several jobs). Before implementing it, I estimated what it could still win against
the harness's noise. That estimate, and an account of where Blake's time goes now,
are the content of this entry.

## What was done

Measurements on scratch copies of `medium-deep` (CPUs 0-15), using `perf` (`nix
shell nixpkgs#perf`) and file modification times, on `main` at `c5938c1`.

**Where the time goes now:**

- **Start-up.** `blake --version` takes 6.7 ms. At the start of the session it
  took ~32 ms: `deferred-init` and `defer-parsers` removed most of Lean's module
  initialization, which was not needed for a TOML build.
- **`noop`** (~35 ms bare on P-cores; ~70-80 ms in the harness, which adds the
  systemd scope for every tool) is now mostly the fetch thread building the job graph:
  - about 10 memoized jobs per module, ~10,000 for 1,000 modules;
  - its profile is flat (allocation and freeing, job plumbing, path and name
    strings) since `bucket-store` removed the store copying.
- **`leaf`** (~330 ms on `deep`):
  - ~40-50 ms of start-up, module discovery and import checks, overlapped with the
    zygote's own ~25 ms initialization;
  - ~250-290 ms of the compile, of which ~85% is Lean elaborating the module itself
    (`perf`: `replace_rec_fn`, `instantiate`, `whnf`, allocation);
  - ~20 ms to finish.
- **`swarm`, `hub`, `cold`.** These are bound by `lean`'s CPU time.
  - The `medium-deep` cold build uses 456 CPU-seconds in 33.8 s on 16 logical CPUs,
    which are 8 hyperthreaded P-cores.
  - Each step of the 85-module critical chain takes ~0.39 s under load against
    ~0.28 s alone. Process priorities do not recover that (`critical-nice`): the
    loss is shared cores and memory bandwidth, not scheduling order.
  - Kernel time is small on the P-cores (~43 of ~400 ms per compile), and
    `MIMALLOC_ALLOW_LARGE_OS_PAGES` changes nothing.

**What parallel fetching could still win.** The `parallel-fetch` prototype gained
1.09x on a direct `noop` (44.7 to 40.2 ms) while doing duplicate work. A correct
version needs three things:
- a pending placeholder (an `IO.Promise`) under each bucket's reference;
- cleanup when job creation fails;
- a guard against import cycles. With waits across threads, a cycle that Lake today
  reports from one thread's call stack would hang instead.

That is a change to Lake's core memoization for ~4-5 ms. In the harness that is ~6%
of a `noop`, below the 7% threshold, with `noop` runs varying by ±15% from run to run.
I judged it not worth the risk and did not implement it.

## Measurements

None with the harness.

## Conclusion

On this machine and harness, the measurable Blake-side gains look exhausted:

| scenario | where the time goes | what could still help |
|---|---|---|
| `noop` | the job graph (~10 jobs per module) | fewer jobs per module, or atomic memoization with parallel fetch; small, and hard to see in the harness |
| `leaf` | Lean elaborating the module (~85%) | inside `lean` (not allowed) |
| `swarm`, `hub`, `cold` | `lean` CPU time, on shared hyperthreaded cores | inside `lean` |

Directions that remain, in rough order of value:

1. **Fewer jobs per module.** Fold the facet chain of a module's build (input,
   imports, import info, pre-setup, setup, artifacts) into fewer jobs. That would
   shorten the fetch thread in proportion. But facets are Lake's public build API
   (users fetch `mod.olean`, `mod.setup` and so on), so this has to keep every facet
   fetchable and is a multi-iteration piece of work.
2. **Atomic memoization plus parallel fetch**, as above, if (1) is not done. Measure
   with `--repeat-fast 20` or more, since a ~5% `noop` change is within the noise of
   10 pairs.
3. **Cheaper import inside `lean`** (~35% of a deep module's compile) would help
   `hub`, `cold` and `swarm`. Blake cannot touch Lean; header snapshots
   (`header-stub`) already cover modules that are being edited.
