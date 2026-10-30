# Benchmark results

One JSON file per *standard* harness run (default sweep, enforced caps, clean
tree), written by `bench/harness/run.py` and named
`<UTC timestamp>-<machine id>-<candidate commit>.json`. Non-standard runs land in
`bench/work/results/` instead. `LEADERBOARD.md` is generated from these files by
`python3 bench/harness/leaderboard.py`, which the maintainer runs after merging.

Every file records the machine fingerprint and caps, the toolchain, the git state,
the corpora (config and graph statistics), per-tool per-scenario timings (all runs
plus medians, CPU time and peak memory of the whole process tree), speedup ratios
(geometric mean over corpora; `primary_geomean` covers noop, leaf, swarm, hub), the
correctness checks against stock Lake after every build, the breaking-edit check,
the per-scenario verdicts and overall verdict, and provenance (harness and
generator hashes, scrubbed environment variables, tool binaries' hashes).

Because every run measures stock Lake and Blake `main` alongside the candidate on
the same machine in the same session, results from different machines are compared
through their ratios, never their absolute times.
