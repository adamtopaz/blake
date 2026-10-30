#!/usr/bin/env python3
"""Check a pull request against the research-loop rules in instructions.md.

Usage (CI runs this; contributors can run it before pushing):
  python3 .github/scripts/pr_check.py --base origin/main --title "research: <one line>"

The PR title's prefix says what kind of PR it is:
  research:   a successful iteration: build-tool code + log entry + standard
              result(s) with verdict `success` + idea write-up
  log:        a failed or inconclusive iteration: log entry (+ results), no
              build-tool code
  bench:      changes to the benchmark (generator, harness, configs); no
              build-tool code
  toolchain:  a deliberate Lean toolchain bump
  anything else (fix:, docs:, chore:, ci:, ...): only the universal rules apply

Universal rules: `lean-toolchain` is unchanged (except `toolchain:`); build-tool
code and benchmark machinery are never changed in the same PR; every modified
upstream Lake file carries a "Modified for Blake" notice.

Exit status 1 with a list of problems, 0 when the PR passes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

LAKE_CODE_PREFIXES = ("Lake/", "LeanExport/")
LAKE_CODE_FILES = {"Lake.lean", "LakeMain.lean", "LeanExport.lean", "lakefile.lean", "lake-manifest.json"}
BENCH_PREFIXES = ("bench/configs/", "bench/gen/", "bench/harness/")
UPSTREAM_PREFIXES = ("Lake/", "LeanExport/", "tests/")
UPSTREAM_EXEMPT = {"tests/run_all.sh", "tests/run_test.sh", "tests/examples/bootstrap/lakefile.lean"}
NOTICE = "Modified for Blake"


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


def changed_files(base: str) -> dict[str, str]:
    """{path: status letter} for files changed between the merge-base and HEAD."""
    out = git("diff", "--name-status", "--diff-filter=ACDMR", f"{base}...HEAD")
    files: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        status = parts[0][0]
        path = parts[-1]
        files[path] = status
    return files


def is_lake_code(p: str) -> bool:
    return p.startswith(LAKE_CODE_PREFIXES) or p in LAKE_CODE_FILES


def is_bench(p: str) -> bool:
    return p.startswith(BENCH_PREFIXES)


def is_upstream(p: str) -> bool:
    return p.startswith(UPSTREAM_PREFIXES) and p not in UPSTREAM_EXEMPT


def head_contains(path: str, needle: str) -> bool:
    try:
        return needle in git("show", f"HEAD:{path}")
    except subprocess.CalledProcessError:
        return False


def is_ancestor(commit: str, of: str = "HEAD") -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", commit, of], cwd=ROOT).returncode == 0


def check_result(path: str, problems: list[str], require_success: bool) -> dict | None:
    try:
        r = json.loads(git("show", f"HEAD:{path}"))
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        problems.append(f"{path}: not readable JSON ({e})")
        return None
    if not r.get("standard"):
        problems.append(f"{path}: not a standard run ({', '.join(r.get('nonstandard_reasons', []))}); "
                        f"only standard runs may be committed")
    c = r.get("correctness", {}).get("candidate", {})
    if not c or not all(v.get("identical") and v.get("builds_ok") and v.get("break_ok") for v in c.values()):
        problems.append(f"{path}: the candidate is not correct (artifacts, builds, or break check)")
    verdict = r.get("verdict", {}).get("overall")
    if verdict is None:
        problems.append(f"{path}: has no verdict")
    elif require_success and verdict != "success":
        problems.append(f"{path}: verdict is {verdict!r}; a `research:` PR needs `success`")
    commit = r.get("git", {}).get("commit", "")
    if r.get("git", {}).get("dirty"):
        problems.append(f"{path}: was measured on a dirty tree; commit before measuring")
    elif commit and not is_ancestor(commit):
        problems.append(f"{path}: measured commit {commit[:7]} is not part of this PR")
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="origin/main", help="base ref the PR targets")
    ap.add_argument("--title", required=True, help="the PR title")
    args = ap.parse_args()

    files = changed_files(args.base)
    kind = (re.match(r"\s*([a-z]+):", args.title) or [None, ""])[1]
    problems: list[str] = []

    lake_changes = sorted(p for p in files if is_lake_code(p))
    bench_changes = sorted(p for p in files if is_bench(p))
    log_entries = sorted(p for p, s in files.items() if s == "A" and re.fullmatch(r"research/log/\d{4}-\d{2}-\d{2}-.+\.md", p))
    idea_entries = sorted(p for p, s in files.items() if s == "A" and p.startswith("research/ideas/") and p.endswith(".md")
                          and Path(p).name not in ("README.md", "TEMPLATE.md"))
    results = sorted(p for p, s in files.items() if s == "A" and re.fullmatch(r"research/results/.+\.json", p))

    # universal rules
    if "lean-toolchain" in files and kind != "toolchain":
        problems.append("lean-toolchain changed; the toolchain is pinned (use a `toolchain:` PR if this is deliberate)")
    if lake_changes and bench_changes:
        problems.append("build-tool code and benchmark machinery changed in the same PR; split them "
                        f"(code: {', '.join(lake_changes[:3])}...; bench: {', '.join(bench_changes[:3])}...)")
    for p, s in sorted(files.items()):
        if s == "M" and is_upstream(p) and not head_contains(p, NOTICE):
            problems.append(f"{p}: modified upstream file without a `{NOTICE}` notice (Apache-2.0 §4(b))")
    if "research/results/LEADERBOARD.md" in files:
        problems.append("research/results/LEADERBOARD.md is generated after merge; do not commit it in a PR")

    # per-kind rules
    if kind == "research":
        if not lake_changes:
            problems.append("`research:` PR changes no build-tool code; use `log:` for a result-only PR")
        if not log_entries:
            problems.append("`research:` PR adds no research/log/YYYY-MM-DD-<slug>.md entry")
        if not idea_entries:
            problems.append("`research:` PR adds no research/ideas/<slug>.md write-up")
        if not results:
            problems.append("`research:` PR adds no research/results/*.json standard run")
        successes = 0
        for p in results:
            r = check_result(p, problems, require_success=False)
            if r and r.get("verdict", {}).get("overall") == "success":
                successes += 1
        if results and not successes:
            problems.append("no committed result has verdict `success`")
    elif kind == "log":
        if lake_changes:
            problems.append("`log:` PR changes build-tool code; failures ship without code "
                            f"({', '.join(lake_changes[:5])})")
        if not log_entries:
            problems.append("`log:` PR adds no research/log/YYYY-MM-DD-<slug>.md entry")
        for p in results:
            check_result(p, problems, require_success=False)
    elif kind == "bench":
        if lake_changes:
            problems.append(f"`bench:` PR changes build-tool code ({', '.join(lake_changes[:5])})")
        for p in results:
            check_result(p, problems, require_success=False)
    else:
        for p in results:
            check_result(p, problems, require_success=False)

    print(f"PR kind: {kind or '(none)'}; {len(files)} changed files; build-tool code: {len(lake_changes)}; "
          f"bench: {len(bench_changes)}; log entries: {len(log_entries)}; ideas: {len(idea_entries)}; results: {len(results)}")
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("PR hygiene: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
