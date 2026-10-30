# Blake research loop — instructions for coding agents

You are about to run **one iteration** of a crowd-sourced research loop. The loop's
purpose is to make **Blake**, a fork of Lake (Lean 4's build system), build gigantic
Lean projects much faster than Lake does. Anyone can run an iteration on their own
machine by pointing a coding agent at this file. One iteration takes about **two
hours** of wall-clock time and ends with a pull request.

Read this whole file before doing anything. Follow the steps in order. The rules in
each step are not optional; a PR that breaks one will not be merged.

## 0. The goal

**Target.** Axiom formalized the Classification of Finite Simple Groups in Lean
(43.1 million lines). When their repository was at 15 million lines, a cold build
with Lake took **11 hours on a 32-core machine**. Their replacement build tool,
drake, builds **50 million lines in 2 hours** (that figure very likely uses several
machines and a shared cache). Blake should beat that. We will not get there in one
step, so every iteration must produce a measured, reproducible improvement on the
benchmark in this repository, or a recorded negative result.

**Priorities, in order.**

1. **Incremental rebuild latency**: what an agent swarm actually waits on. The
   benchmark's primary scenarios are `noop` (nothing changed), `leaf` (one module
   nobody imports was edited), `swarm` (many little-imported modules edited at
   once), and `hub` (a widely imported module was edited).
2. Single-machine **cold build** time (scenario `cold`).
3. Distributed builds across machines. Not yet part of the benchmark; do not work on
   this unless `research/ideas/` says the groundwork is in place.

**What may change.** Anything in how Blake drives `lean`, schedules work, hashes,
caches, loads, links, or communicates is fair game: persistent elaboration daemons,
linking Lean as a library, skipping work Lake does unnecessarily, content-addressed
caches, critical-path scheduling, cheaper traces, and so on.

**What may not change.**

- **Lean itself.** The toolchain is pinned in `lean-toolchain`. Do not change it,
  and do not carry patches to the Lean compiler or server.
- **Drop-in compatibility.** Existing lakefiles (`lakefile.lean` and
  `lakefile.toml`), the `lake` command line, `lake serve` for editors, the manifest
  format, and the build directory layout must keep working. Lake's test suite
  (`tests/`) must stay green.
- **Artifacts.** For the same inputs, Blake must produce `.olean`, `.ilean`, and
  `.c` files **byte-identical** to stock Lake's. The harness checks this and a
  candidate that fails is not an improvement, however fast.

## 1. Set up (about 5 minutes)

Prerequisites: `git`, [`elan`](https://github.com/leanprover/elan) (it installs the
pinned toolchain automatically), Python 3.10+, and the `gh` CLI if you want the PR
opened for you. A `nix develop` shell with everything is available but optional.

```sh
git clone https://github.com/adamtopaz/blake && cd blake
lake build                           # builds Blake into .lake/build/bin/lake (about 1 minute)
.lake/build/bin/lake --version
```

On NixOS, nixpkgs' `elan` wraps the toolchain's `lake` to export `LEAN_CC`; the
harness does the same for Blake automatically. If you run Blake by hand, first
`export LEAN_CC="$(lean --print-prefix)/bin/cc"`.

**Resource caps.** Benchmarks must not take over the machine. The harness pins
itself to a CPU set and a memory cap and records them in the result. Defaults are
half of the machine; set yours once in `bench/local.json` (gitignored):

```json
{"cpus": "0-15", "mem_gb": 32}
```

Then smoke-test the benchmark (about 3 minutes):

```sh
python3 bench/harness/run.py --quick --tools lake,candidate
```

## 2. Orient (about 10 minutes)

1. Read `research/ideas/`: write-ups of every idea that produced a merged
   improvement. Build on them; do not undo them.
2. Read `research/log/`: one entry per attempted iteration, **including failures**.
   Do not repeat a failed idea unless you have a genuinely new angle, and say so.
3. Read `research/results/LEADERBOARD.md` for where Blake currently stands.
4. Read `bench/README.md` for what the benchmark measures and how to run parts of
   it by hand.

Then **profile before guessing**. Blake is Lake, so start from where Lake spends
time on the scenarios that matter. Useful tools: `python3 bench/harness/run.py
--quick --keep` leaves per-tool corpus copies under `bench/work/corpora/` for ad hoc
experiments; `lake build -v` prints every `lean` invocation; `strace -c`, `perf`,
and Lean's `--profile` all work on the Blake binary. Baseline observations from the
first run are listed at the end of this file.

## 3. Pick one hypothesis (about 10 minutes)

Choose **one** idea for this iteration. Good ideas (a) target the primary
scenarios, (b) can be implemented in about an hour, and (c) will show up on the
medium corpora. Prefer measurement-driven ideas over architectural rewrites.

Before writing code:

1. Update `main` and branch from it: `git fetch origin && git switch -c
   research/<slug> origin/main`.
2. Create your log entry from the template:
   `cp research/log/TEMPLATE.md research/log/$(date +%F)-<slug>.md` and fill in the
   hypothesis, why you believe it, and how you will measure it. The entry exists so
   that even an abandoned attempt leaves knowledge behind.

## 4. Implement (at most 60 minutes)

- Keep changes focused on the hypothesis.
- Every upstream Lake file you modify must carry a short notice near the top of the
  change, like `-- Modified for Blake (YYYY-MM-DD): <what and why>`. Blake is
  distributed under Apache 2.0, which requires modified files to say so.
- Keep notes in your log entry as you go: what you tried, what surprised you.
- If the implementation is not working after 60 minutes, stop, record what you
  learned, and go to step 7 with outcome `inconclusive`.

## 5. Verify correctness (about 10 minutes)

All three must pass before measuring:

```sh
lake build                           # Blake itself builds
tests/run_all.sh                     # Lake's test suite (parallel, ~5-10 minutes)
python3 bench/harness/run.py --quick --tools lake,candidate   # artifacts identical
```

The last command prints `correctness of candidate: identical artifacts to lake`.
If it prints `ARTIFACTS DIFFER`, your change is wrong, not clever.

## 6. Measure (at most 30 minutes)

Run the full sweep. It measures stock Lake, Blake `main`, and your candidate on the
same corpora, in the same session, under the same caps:

```sh
python3 bench/harness/run.py --label <slug>
```

It writes `research/results/<timestamp>-<machine>-<commit>.json` and prints a table
plus speedup lines. The decision is made against **`main`** (Lake is context for the
long-term target):

- A scenario is **improved** when the candidate's median is faster than `main`'s by
  more than the larger of 3% and `main`'s own min-to-max spread for that scenario.
  It is **regressed** by the symmetric rule.
- The iteration is a **success** when the candidate improves at least one primary
  scenario on at least one corpus and regresses none (on any corpus, any scenario,
  including `cold`).
- If the numbers are within noise, rerun with `--repeat 5` before deciding. Never
  tune the benchmark, corpora, or caps to make a result look better.

## 7. Record (about 10 minutes)

Finish your log entry: outcome (`success`, `failure`, or `inconclusive`), the
speedup lines verbatim, the result file name, and what the next person should know.

If the outcome is `success`, also:

1. Write `research/ideas/<slug>.md` from `research/ideas/TEMPLATE.md`: the
   problem, the mechanism, where it lives in the code, measured gains, caveats.
2. Regenerate the leaderboard: `python3 bench/harness/leaderboard.py`.

Commit the code (if any), the log entry, the result JSON, the idea write-up, and
the leaderboard on your branch. Use clear commit messages; the first line should
start with `research:` for successful iterations and `log:` for recorded failures.

## 8. Submit

Open a PR against `main` even for failures: negative results are part of the
product. With the GitHub CLI:

```sh
git push -u origin research/<slug>
gh pr create --base main --title "research: <one line>" --body-file research/log/<your-entry>.md
```

If `gh` is unavailable, push the branch and print the compare URL
(`https://github.com/adamtopaz/blake/compare/main...research/<slug>`) so a human can
open the PR. The maintainer reviews and merges; log-only PRs are merged quickly.

## Hard rules, in one place

1. One hypothesis per iteration; about two hours; always end with a PR.
2. Do not change `lean-toolchain`; do not patch Lean.
3. Drop-in compatibility and Lake's test suite stay intact.
4. `.olean`/`.ilean`/`.c` artifacts stay byte-identical to Lake's.
5. Measure with the harness under caps; decide against `main` with the noise rule.
6. Never modify the benchmark, corpora, or caps in the same PR as a build-tool
   change. Benchmark changes are their own PRs, discussed first in an issue.
7. Record every attempt, including failures, in `research/log/`.

## Map of the code

- `Lake/Build/Run.lean`: top-level build driver; job scheduling and the thread pool.
- `Lake/Build/Module.lean`: how a module is built, how `lean` is invoked, setup
  files, dynlib and plugin handling.
- `Lake/Build/Job/`: the job monad and async machinery.
- `Lake/Build/Trace.lean`, `Lake/Build/Common.lean`: traces, hashes, up-to-date
  checks, artifact handling.
- `Lake/Load/`: loading workspaces, manifests, and elaborating lakefiles.
- `Lake/Config/`: package, library, workspace, environment, install detection.
- `Lake/CLI/`: the command line.
- `LeanExport/`: parser for Lean's export format (used by `lake check`).
- `bench/`: corpus generator and harness. `tests/`: Lake's test suite.

## Baseline observations (first run, 2026-10-29, 16 cores)

- Blake and Lake produce identical artifacts and essentially identical cold,
  `swarm`, and `hub` times: as expected, since Blake is Lake.
- Blake's `noop` and `leaf` builds are ~60 ms slower than stock Lake's on every
  corpus (0.21 s vs 0.15 s no-op on 200 modules). Blake's binary is statically
  linked and built with `supportInterpreter`; stock `lake` is a small dynamically
  linked executable. Startup cost is the first obvious lead.
- On a 200-module corpus a no-op build already costs ~0.15 s and a one-leaf edit
  ~0.4 s, of which ~0.1 s is `lean` startup. At 100k modules the per-module trace
  checking and the import loading inside each `lean` process dominate; measure how
  they scale with the `large-*` configs before optimizing them.
