# Benchmark results

One JSON file per harness run, written by `bench/harness/run.py`, named
`<UTC timestamp>-<machine id>-<candidate commit>.json`. `LEADERBOARD.md` is
generated from these files by `python3 bench/harness/leaderboard.py`; regenerate it
whenever you add a result.

Every file records the machine fingerprint and caps, the toolchain, the git state,
the corpora (config and graph statistics), per-tool per-scenario timings (all runs
plus medians), speedup ratios (geometric mean over corpora; `primary_geomean`
covers noop, leaf, swarm, hub), and the artifact-identity check against stock Lake.

Because every run measures stock Lake and Blake `main` alongside the candidate on
the same machine in the same session, results from different machines are compared
through their ratios, never their absolute times.
