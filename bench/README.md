# Blake benchmarks

Everything here exists to answer one question reproducibly: *is this version of
Blake faster than the last one, and than stock Lake, on builds that look like a
gigantic Lean project?* See `../instructions.md` for how the research loop uses it.

## Pieces

- `gen/generate.py`: generates a self-contained Lean package (imports only Lean
  core) with a controlled import DAG and controlled per-module elaboration cost.
  Deterministic: the same config always yields byte-identical files.
- `configs/*.json`: the corpora. Three DAG shapes at three sizes:
  - `layered`: random layered DAG, bushy, depth about the square root of the module count.
  - `deep`: a handful of long chains with occasional cross links; long critical path.
  - `wide`: a small core of hub modules imported by nearly everything, then a wide
    shallow fan; editing a hub rebuilds almost the whole corpus.
  - sizes: `small` (200 modules, smoke tests), `medium` (1500 modules, the default
    sweep), `large` (8000 modules, for scaling questions; slow).
- `harness/run.py`: the measurement. Builds stock Lake, Blake `main` (from a git
  worktree of `origin/main`), and the candidate (this checkout), copies the corpus
  once per tool, then runs the scenarios below and writes one JSON result.
- `harness/leaderboard.py`: renders `../research/results/LEADERBOARD.md`.
- `local.json` (gitignored): your machine's caps, e.g. `{"cpus": "0-15", "mem_gb": 32}`.
- `work/` (gitignored): generated corpora, per-tool copies, and the `main` worktree.

## Scenarios

| scenario | what happens | what it measures |
|---|---|---|
| `cold` | build from nothing | total throughput; the scheduler's ability to keep the CPUs busy |
| `noop` | build again with nothing changed | pure build-tool overhead: loading the workspace, trace and hash checks |
| `leaf` | append a theorem to a module nobody imports, build | minimal end-to-end latency of one edit |
| `swarm` | append a theorem to up to 24 little-imported modules spread over the DAG, build | what a swarm of agents produces between builds |
| `hub` | append a theorem to the most widely imported module, build | a large downstream rebuild: scheduling and parallelism |

Edits accumulate (each one is a new theorem with a fresh name), so the tools see the
same sequence of edits and no restore step is needed. Incremental scenarios are run
`--repeat` times (default 3) and the median is reported.

## Caps and comparability

The harness pins itself and all children to `--cpus` (CPU affinity on Linux) and
sets `LEAN_NUM_THREADS` to the number of CPUs, which is what bounds Lake 5's
concurrency (it has no jobs flag). `--mem-gb` is enforced with a transient
`systemd-run --user --scope` when available, otherwise recorded as unenforced. All
of this goes into the result file along with a machine fingerprint. Only the ratios
between tools measured in the same run are comparable across machines.

## Correctness

After each tool's cold build the harness hashes every `.olean`, `.ilean`, and `.c`
under `.lake/build` and compares them with stock Lake's. Any difference marks the
candidate incorrect. (`.trace`, `.hash`, and `.setup.json` files are expected to
differ; they contain absolute paths and tool-specific data.)

## Running pieces by hand

```sh
python3 bench/harness/run.py --quick --tools lake,candidate      # 3 small corpora, ~3 min
python3 bench/harness/run.py --configs medium-deep --scenarios cold,noop --repeat 5
python3 bench/harness/run.py --keep                              # keep bench/work/corpora/*/<tool>/ for experiments
python3 bench/gen/generate.py --shape wide --modules 50 --out /tmp/c && cd /tmp/c && lake build
```

Changing the generator or the configs changes every future comparison. Such changes
go in their own PR, discussed first, and bump `GENERATOR_VERSION` so stale corpora
are regenerated.
