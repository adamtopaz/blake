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
  (`tests/`) must stay as green as it is on `main`.
- **Artifacts.** For the same inputs, Blake must produce `.olean`, `.ilean`, and
  `.c` files **byte-identical** to stock Lake's, after cold *and* incremental
  builds, and it must fail exactly where Lake fails. The harness checks all of
  this; a candidate that fails any check is not an improvement, however fast.
- **Honesty of the measurement.** Blake may not behave differently because it is
  being benchmarked: no code may depend on the corpus's names (`Corpus.*`,
  `bench_edit_*`), its paths, the harness's environment variables, or its edit
  pattern. No machine-specific build flags (such as `-march=native`). Parallelism
  must respect the harness's caps (`LEAN_NUM_THREADS` and the CPU set). Any helper
  process Blake starts must be started by the measured command and must not
  outlive it; the harness kills and reports stragglers. Merged changes are
  re-checked by the maintainer on held-out corpora.

## 1. Set up (about 15 minutes)

Prerequisites: `git`, [`elan`](https://github.com/leanprover/elan) (it installs the
pinned toolchain automatically), Python 3.10+, bash, and the `gh` CLI for the PR.
Linux is the reference platform: CPU and memory caps are enforced only on Linux
(CPU affinity, and `systemd-run --user` for memory and process accounting). On
macOS the harness still runs but its results are marked non-standard; install
`coreutils` from Homebrew for the test suite (`gtail`). A `nix develop` shell with
everything is available but optional.

```sh
gh repo fork adamtopaz/blake --clone --remote-name fork   # origin = upstream, fork = yours
cd blake
lake build                                                # Blake -> .lake/build/bin/lake (~1 min)
.lake/build/bin/lake --version
```

If you already have a clone, keep `origin` pointing at `adamtopaz/blake` and add
your fork as `fork`; the harness compares against `origin/main`.

On NixOS, nixpkgs' `elan` wraps the toolchain's `lake` to export `LEAN_CC`; the
harness and `tests/run_all.sh` do the same for Blake automatically. If you run
Blake by hand, first `export LEAN_CC="$(lean --print-prefix)/bin/cc"`.

**Resource caps.** Benchmarks must not take over the machine. Decide what this
machine may spend, derive it from `nproc` and your RAM (do not copy the example),
and write it once to `bench/local.json` (gitignored). The default is half of the
machine.

```json
{"cpus": "0-15", "mem_gb": 32}
```

Then run two baselines you will compare against later:

```sh
tests/run_all.sh -o .out-main                        # Lake's test suite on main (~5-10 min)
python3 bench/harness/run.py --quick --tools lake,candidate   # harness smoke test (~3 min)
```

The last command must end with `correctness of candidate: identical artifacts to
lake, all builds ok, break check ok`. Quick runs are not standard runs; they are
written to `bench/work/results/`, not to `research/results/`.

## 2. Orient (about 10 minutes)

1. Read `research/ideas/`: write-ups of every idea that produced a merged
   improvement. Build on them; do not undo them.
2. Read `research/log/`: one entry per attempted iteration, **including failures**.
   Do not repeat a failed idea unless you have a genuinely new angle, and say so.
3. Read `research/results/LEADERBOARD.md` for where Blake currently stands.
4. Read `bench/README.md` for what the benchmark measures and how to run parts of
   it by hand.

Then **profile before guessing**. Blake is Lake, so start from where Lake spends
time on the scenarios that matter. `python3 bench/harness/run.py --quick --keep`
leaves per-tool corpus copies under `bench/work/corpora/` for ad hoc experiments;
`lake build -v` prints every `lean` invocation; on Linux `strace -c` and `perf`
work on the Blake binary, and Lean's `--profile` works everywhere. Baseline
observations from the first run are listed at the end of this file.

## 3. Pick one hypothesis (about 10 minutes)

Choose **one** idea for this iteration. Good ideas (a) target the primary
scenarios, (b) can be implemented in about an hour, and (c) will show up on the
medium corpora. Prefer measurement-driven ideas over architectural rewrites.

Before writing code:

1. Update and branch: `git fetch origin && git switch -c research/<slug> origin/main`.
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
- **Commit before you measure.** Results record the commit they measured;
  measuring uncommitted code makes a result nobody can reproduce.

## 5. Verify correctness (about 10 minutes)

All three must pass before measuring:

```sh
lake build                                   # Blake itself builds
tests/run_all.sh --compare .out-main         # no test that passed on main fails now
python3 bench/harness/run.py --quick --tools lake,candidate   # artifacts identical, break check ok
```

If the harness prints `INCORRECT`, your change is wrong, not clever.

## 6. Measure (at most 30 minutes)

Run the standard sweep. It measures stock Lake, Blake `main` (the merge-base of
your branch and `origin/main`), and your candidate on the same corpora, in the same
session, under the same caps, and decides for you:

```sh
python3 bench/harness/run.py --label <slug>
```

It prints a table, `speedup ... vs main` and `vs lake` lines, one `verdict` line per
corpus and scenario, and a final `verdict: success|neutral|failure` line, and writes
`research/results/<timestamp>-<machine>-<commit>.json`. The rule it applies:

- Per corpus and scenario, the tools are interleaved and compared on **paired
  runs**: repetition *i* of the candidate against repetition *i* of `main`, and
  the speedup is the median of those ratios (a shared machine toggles between
  speed states over seconds; pairing cancels that). Scenarios under 2 s get 10
  repetitions and a 7% threshold, longer ones 3 repetitions and 3%, `cold` runs
  once with 7%. **improved** if the paired speedup is at least `1 + t`,
  **regressed** if at most `1 − t`. Tables and speedup lines show medians.
- **success**: at least one primary scenario improved on at least one corpus and
  nothing regressed anywhere (including `cold`), with all correctness checks
  passing. **failure**: anything regressed or any check failed. Otherwise
  **neutral**.

If a result looks like noise, rerun with `--repeat 5 --repeat-fast 20`. **Commit every result file
the standard sweep writes**, including reruns and the ones you do not like. Never
tune the benchmark, corpora, or caps to make a result look better.

## 7. Record (about 10 minutes)

Finish your log entry: the outcome (`success`, `failure`, or `inconclusive`), the
harness's speedup and verdict lines verbatim, the result file names, and what the
next person should know.

If the outcome is `success`, also write `research/ideas/<slug>.md` from
`research/ideas/TEMPLATE.md`: the problem, the mechanism, where it lives in the
code, measured gains, caveats.

Commit on your branch:

- `success`: the code, the log entry, the result JSON(s), and the idea write-up.
  Commit messages and the PR title start with `research:`.
- `failure` or `inconclusive`: **only** the log entry and result JSON(s), prefixed
  `log:`. Do not include the code in the PR; describe the approach in the log and,
  if the code is worth keeping, push it to a separate `research/<slug>-wip` branch
  on your fork and link it from the log.

Do not regenerate or commit `research/results/LEADERBOARD.md`; the maintainer does
that after merging (it would conflict between concurrent PRs). You may run
`python3 bench/harness/leaderboard.py` locally to look at it.

## 8. Submit

Open a PR against `adamtopaz/blake` `main` even for failures: negative results are
part of the product.

```sh
git push -u fork research/<slug>
gh pr create --repo adamtopaz/blake --base main --head <your-github-user>:research/<slug> \
  --title "research: <one line>" --body-file research/log/<your-entry>.md
```

(Use `log:` instead of `research:` in the title for failures and inconclusive
results. Maintainers with push access may push the branch to `origin` instead.) If
`gh` is unavailable, push the branch and print the compare URL
`https://github.com/adamtopaz/blake/compare/main...<your-github-user>:research/<slug>`
so a human can open the PR.

Before pushing, run the same hygiene check CI will run:

```sh
python3 .github/scripts/pr_check.py --base origin/main --title "research: <one line>"
```

CI (`.github/workflows/pr-check.yml`) then builds Blake, runs Lake's test suite
against the base branch's results, checks artifact identity and the breaking-edit
rule against stock Lake on the small corpora, and runs the hygiene check. It never
measures performance; your result file is the measurement. Fix red checks before
asking for review. The maintainer reviews and merges; log-only PRs are merged
quickly. If `main` moves before your PR is merged, the maintainer may ask you to
rebase and re-measure. After a merge, a workflow regenerates
`research/results/LEADERBOARD.md`.

## Hard rules, in one place

1. One hypothesis per iteration; about two hours; always end with a PR.
2. Do not change `lean-toolchain`; do not patch Lean.
3. Drop-in compatibility stays intact; no test that passes on `main` may fail.
4. Artifacts stay byte-identical to Lake's after every build; failures match Lake's.
5. No benchmark-aware behavior, no machine-specific flags, no helper processes
   that outlive the measured command, no ignoring the caps.
6. Commit before measuring; measure with the standard sweep; commit every result
   it writes; let the harness decide.
7. Never modify the benchmark, corpora, caps, or harness in the same PR as a
   build-tool change. Benchmark changes are their own PRs, discussed first in an
   issue.
8. Record every attempt, including failures, in `research/log/`.

## Map of the code

- `Lake/Build/Run.lean`: top-level build driver; job scheduling and the thread pool.
- `Lake/Build/Module.lean`: how a module is built, how `lean` is invoked, setup
  files, dynlib and plugin handling.
- `Lake/Build/Job/`: the job monad and async machinery.
- `Lake/Build/Trace.lean`, `Lake/Build/Common.lean`: traces, hashes, up-to-date
  checks, artifact handling.
- `Lake/Load/`: loading workspaces, manifests, and elaborating lakefiles.
- `Lake/Config/`: package, library, workspace, environment, install detection
  (`InstallPath.lean`: how `lake` finds `lean` and itself).
- `Lake/CLI/`: the command line.
- `LeanExport/`: parser for Lean's export format (used by `lake check`).
- `bench/`: corpus generator and harness. `tests/`: Lake's test suite.

## Baseline observations (first run, 2026-10-29, 16 cores)

- Blake and Lake produce identical artifacts and essentially identical `cold`,
  `swarm`, and `hub` times: as expected, since Blake is Lake.
- Blake's `noop` and `leaf` builds are ~50-60 ms slower than stock Lake's on every
  corpus (about 0.21 s vs 0.15 s no-op on 200 modules). Two candidate causes, both
  unverified: Blake's binary is not installed next to `lean`, so on every
  invocation it runs `lean --print-prefix` to locate the toolchain
  (`Lake/Config/InstallPath.lean`; that subprocess costs ~30 ms here); and Blake's
  binary is statically linked and built with `supportInterpreter`, while stock
  `lake` is a small dynamically linked executable. Measure before fixing.
- On a 200-module corpus a no-op build already costs ~0.15 s and a one-leaf edit
  ~0.4 s, of which ~0.1 s is `lean` startup. At 100k modules the per-module trace
  checking and the import loading inside each `lean` process dominate; measure how
  they scale with the `large-*` configs before optimizing them.
- A `hub` edit rebuilds every transitive dependent even though an appended theorem
  changes no downstream artifact. Lake invalidates by `.olean` hash; finer-grained
  invalidation (by the interface a dependent actually uses) would pass the
  byte-identical check and is the single largest lever on the `hub` and `swarm`
  scenarios. The breaking-edit check exists to make sure such a scheme still
  rebuilds dependents when it must.
