#!/usr/bin/env python3
"""Check a pull request against the research-loop rules in instructions.md.

Usage (CI runs this; contributors run it before pushing):
  python3 .github/scripts/pr_check.py --base origin/main --title "research: <one line>"

The PR title must start with one of these prefixes (case-insensitive):
  research:   a successful iteration: build-tool code + log entry + idea
              write-up + standard result(s); the newest result measured on
              this PR's build-tool code must say `success`
  log:        a failed or inconclusive iteration: log entry (+ results), no
              build-tool code
  bench:      changes to the benchmark (generator, harness, configs, CI,
              test runner); no build-tool code
  fix:        a build-tool or infrastructure fix outside the research loop
  toolchain:  a deliberate Lean toolchain bump
  ci:, docs:, chore:  infrastructure, documentation, housekeeping; no
              build-tool code

Universal rules: `lean-toolchain` is unchanged (except `toolchain:`); build-tool
code and benchmark machinery never change in the same PR; committed results are
standard runs measured on a clean commit; existing results are never modified
(except by a `bench:` rescore); the generated leaderboard is never committed;
every modified upstream Lake file carries a "Modified for Blake" notice.

Exit status 1 with a list of problems, 0 when the PR passes.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

KINDS = ("research", "log", "bench", "fix", "toolchain", "ci", "docs", "chore")
CODE_KINDS = ("research", "fix", "toolchain")          # may change build-tool code
INFRA_KINDS = ("bench", "ci", "fix", "chore")           # may change CI, test runner, bench machinery
LAKE_CODE_PREFIXES = ("Lake/", "LeanExport/")
LAKE_CODE_FILES = ("Lake.lean", "LakeMain.lean", "LeanExport.lean", "lakefile.lean", "lake-manifest.json", "lean-toolchain")
BENCH_PREFIXES = ("bench/configs/", "bench/gen/", "bench/harness/")
INFRA_FILES = ("tests/run_all.sh", "tests/run_test.sh")
UPSTREAM_PREFIXES = ("Lake/", "LeanExport/", "tests/")
UPSTREAM_ROOT_FILES = ("Lake.lean", "LakeMain.lean", "LeanExport.lean")
UPSTREAM_EXEMPT = ("tests/run_all.sh", "tests/run_test.sh", "tests/examples/bootstrap/lakefile.lean")
COMMENTABLE = (".lean", ".sh", ".toml", ".py", ".nix", ".yml", ".yaml", ".c", ".h", ".md")
NOTICE = "Modified for Blake"


def git(*args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def show(ref: str, path: str) -> str | None:
    p = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=ROOT, capture_output=True, text=True)
    return p.stdout if p.returncode == 0 else None


def changed_files(base: str) -> dict[str, str]:
    """{path: status letter} for files changed between the merge-base and HEAD (R = renamed-to)."""
    files: dict[str, str] = {}
    for line in git("diff", "--name-status", "--diff-filter=ACDMR", f"{base}...HEAD").splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        if status == "R":
            files[parts[1]] = "D"
            files[parts[2]] = "R"
        else:
            files[parts[1]] = status
    return files


def is_lake_code(p: str) -> bool:
    return p.startswith(LAKE_CODE_PREFIXES) or p in LAKE_CODE_FILES


def is_bench(p: str) -> bool:
    return p.startswith(BENCH_PREFIXES)


def is_upstream(p: str) -> bool:
    return (p.startswith(UPSTREAM_PREFIXES) or p in UPSTREAM_ROOT_FILES) and p not in UPSTREAM_EXEMPT


def commit_exists(commit: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=ROOT, capture_output=True).returncode == 0


def same_build_tool_tree(commit: str) -> bool:
    paths = [*LAKE_CODE_PREFIXES, *LAKE_CODE_FILES]
    p = subprocess.run(["git", "diff", "--quiet", commit, "HEAD", "--", *paths], cwd=ROOT, capture_output=True)
    return p.returncode == 0


def base_verdict(base: str, result: dict) -> str | None:
    """Recompute the result's verdict with the base branch's harness, if it has one."""
    src = show(base, "bench/harness/run.py")
    if src is None:
        return None
    with tempfile.TemporaryDirectory() as d:
        mod_path = Path(d) / "base_run.py"
        mod_path.write_text(src, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("base_run", mod_path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod.verdicts(result["measurements"], result["correctness"], result["tool_order"])["overall"]
        except Exception as e:  # noqa: BLE001
            return f"(could not recompute: {e})"


def check_result(path: str, base: str, problems: list[str], notes: list[str]) -> dict | None:
    """Hygiene every committed result must satisfy; returns the parsed result."""
    text = show("HEAD", path)
    try:
        r = json.loads(text or "")
    except json.JSONDecodeError as e:
        problems.append(f"{path}: not readable JSON ({e})")
        return None
    if not r.get("standard"):
        problems.append(f"{path}: not a standard run ({', '.join(r.get('nonstandard_reasons', []))}); "
                        f"only standard runs may be committed")
    if r.get("git", {}).get("dirty"):
        problems.append(f"{path}: was measured on a dirty tree; commit before measuring")
    if r.get("verdict", {}).get("overall") is None:
        problems.append(f"{path}: has no verdict")
    # provenance: the harness, generator, and configs the run used must be the base's
    h = r.get("harness", {})
    for key, rel in (("run_py_sha256", "bench/harness/run.py"), ("generate_py_sha256", "bench/gen/generate.py")):
        base_src = show(base, rel)
        if base_src is not None and h.get(key) and h[key] != hashlib.sha256(base_src.encode("utf-8")).hexdigest():
            problems.append(f"{path}: measured with a {rel} that differs from {base}'s")
    for cfg_name, meta in r.get("corpora", {}).items():
        base_cfg = show(base, f"bench/configs/{cfg_name}.json")
        if base_cfg is not None and meta.get("config_sha256") and meta["config_sha256"] != hashlib.sha256(base_cfg.encode("utf-8")).hexdigest():
            problems.append(f"{path}: corpus config {cfg_name} differs from {base}'s")
    recomputed = base_verdict(base, r)
    if recomputed is not None and recomputed != r["verdict"]["overall"]:
        problems.append(f"{path}: stored verdict {r['verdict']['overall']!r} but {base}'s harness computes {recomputed!r}")
    c = r.get("correctness", {}).get("candidate", {})
    correct = bool(c) and all(v.get("identical") and v.get("builds_ok") and v.get("break_ok") for v in c.values())
    notes.append(f"{path}: verdict {r['verdict'].get('overall')}, candidate {'correct' if correct else 'INCORRECT'}, "
                 f"commit {r.get('git', {}).get('commit', '')[:7]}")
    r["_correct"] = correct
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main", help="base ref the PR targets")
    ap.add_argument("--title", required=True, help="the PR title")
    args = ap.parse_args()

    files = changed_files(args.base)
    m = re.match(r"\s*([A-Za-z]+):", args.title)
    kind = m.group(1).lower() if m else ""
    problems: list[str] = []
    notes: list[str] = []

    lake_changes = sorted(p for p in files if is_lake_code(p))
    bench_changes = sorted(p for p in files if is_bench(p))
    infra_changes = sorted(p for p in files if p.startswith(".github/") or p in INFRA_FILES)
    test_removals = sorted(p for p, s in files.items() if s == "D" and p.startswith("tests/"))
    log_entries = sorted(p for p, s in files.items() if s in "AR" and re.fullmatch(r"research/log/\d{4}-\d{2}-\d{2}-.+\.md", p))
    idea_entries = sorted(p for p, s in files.items() if s in "AR" and re.fullmatch(r"research/ideas/(?!README|TEMPLATE).+\.md", p))
    results_added = sorted(p for p, s in files.items() if s in "AR" and re.fullmatch(r"research/results/.+\.json", p))
    results_touched = sorted(p for p, s in files.items() if s in "MD" and re.fullmatch(r"research/results/.+\.json", p))

    if kind not in KINDS:
        problems.append(f"PR title must start with one of {', '.join(k + ':' for k in KINDS)} (got {args.title!r})")

    # universal rules
    if "lean-toolchain" in files and kind != "toolchain":
        problems.append("lean-toolchain changed; the toolchain is pinned (use a `toolchain:` PR if this is deliberate)")
    if lake_changes and kind not in CODE_KINDS:
        problems.append(f"`{kind or '(none)'}:` PR changes build-tool code ({', '.join(lake_changes[:5])}); only "
                        f"{', '.join(k + ':' for k in CODE_KINDS)} may")
    if lake_changes and bench_changes:
        problems.append("build-tool code and benchmark machinery changed in the same PR; split them "
                        f"(code: {', '.join(lake_changes[:3])}; bench: {', '.join(bench_changes[:3])})")
    if bench_changes and kind not in INFRA_KINDS:
        problems.append(f"`{kind or '(none)'}:` PR changes benchmark machinery ({', '.join(bench_changes[:5])}); use `bench:`")
    if infra_changes and kind not in INFRA_KINDS:
        problems.append(f"`{kind or '(none)'}:` PR changes CI or the test runner ({', '.join(infra_changes[:5])}); use `ci:` or `bench:`")
    if test_removals and kind not in INFRA_KINDS:
        problems.append(f"`{kind or '(none)'}:` PR removes or renames tests ({', '.join(test_removals[:5])})")
    if results_touched and kind != "bench":
        problems.append(f"existing results modified or deleted ({', '.join(results_touched[:5])}); results are immutable "
                        f"(a `bench:` PR may rescore them)")
    for p, s in sorted(files.items()):
        if s == "M" and is_upstream(p) and p.endswith(COMMENTABLE):
            content = show("HEAD", p) or ""
            if NOTICE not in content:
                problems.append(f"{p}: modified upstream file without a `{NOTICE}` notice (Apache-2.0 §4(b))")
    if "research/results/LEADERBOARD.md" in files:
        problems.append("research/results/LEADERBOARD.md is generated after merge; do not commit it in a PR")

    # every committed result
    results = {}
    for p in results_added:
        r = check_result(p, args.base, problems, notes)
        if r:
            results[p] = r

    # per-kind rules
    if kind == "research":
        if not lake_changes:
            problems.append("`research:` PR changes no build-tool code; use `log:` for a result-only PR")
        if not log_entries:
            problems.append("`research:` PR adds no research/log/YYYY-MM-DD-<slug>.md entry")
        if not idea_entries:
            problems.append("`research:` PR adds no research/ideas/<slug>.md write-up")
        if not results_added:
            problems.append("`research:` PR adds no research/results/*.json standard run")
        on_head = []
        for p, r in results.items():
            commit = r.get("git", {}).get("commit", "")
            if not commit_exists(commit):
                problems.append(f"{p}: measured commit {commit[:7]} is not in this repository; push it "
                                f"(merge origin/main into your branch rather than rebasing)")
            elif same_build_tool_tree(commit):
                on_head.append((r["timestamp"], p, r))
            else:
                notes.append(f"{p}: measured on build-tool code that differs from this PR's; not decisive")
            main_commit = r.get("tools", {}).get("main", {}).get("commit", "")
            try:
                merge_base = git("merge-base", "HEAD", args.base).strip()
            except RuntimeError:
                merge_base = ""
            if main_commit and merge_base and main_commit != merge_base:
                notes.append(f"{p}: compared against main {main_commit[:7]}, but the PR's merge-base is now "
                             f"{merge_base[:7]}; the maintainer may ask for a re-measure")
        if results and not on_head:
            problems.append("no committed result was measured on this PR's build-tool code")
        if on_head:
            on_head.sort()
            newest = on_head[-1][2]
            if newest["verdict"]["overall"] != "success":
                problems.append(f"the newest result on this PR's code ({on_head[-1][1]}) has verdict "
                                f"{newest['verdict']['overall']!r}; a `research:` PR needs `success`")
            if not newest.get("_correct"):
                problems.append(f"the newest result on this PR's code ({on_head[-1][1]}) is not correct")
            for _, p, r in on_head:
                if r["verdict"]["overall"] == "failure":
                    problems.append(f"{p}: a result on this PR's code has verdict `failure`")
    elif kind == "log":
        if not log_entries:
            problems.append("`log:` PR adds no research/log/YYYY-MM-DD-<slug>.md entry")
    # bench/fix/toolchain/ci/docs/chore: universal rules only

    print(f"PR kind: {kind or '(none)'}; {len(files)} changed files; build-tool code: {len(lake_changes)}; "
          f"bench: {len(bench_changes)}; infra: {len(infra_changes)}; log entries: {len(log_entries)}; "
          f"ideas: {len(idea_entries)}; results: {len(results_added)}")
    for n in notes:
        print(f"  note: {n}")
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("PR hygiene: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
