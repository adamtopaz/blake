#!/usr/bin/env python3
"""Blake benchmark harness.

Measures stock Lake, Blake `main`, and the candidate (the current checkout) on
the same machine, under the same CPU and memory caps, on generated corpora
(see `bench/gen/generate.py`), and writes one JSON result file that the
research loop commits under `research/results/`.

Scenarios (all after a cold build of the corpus):
  cold   full build from nothing (context; secondary target)
  noop   `build` with nothing changed: pure build-tool overhead
  leaf   edit a module nobody imports, rebuild
  swarm  edit many modules spread over the DAG at once, rebuild
  hub    edit the most widely imported module, rebuild (large downstream rebuild)

Edits accumulate (each is a new appended theorem), so no restore step is needed
and every tool sees exactly the same sequence of edits.

Correctness: after each tool's cold build, every `.olean`, `.ilean`, and `.c`
artifact is hashed and compared with stock Lake's. A candidate whose artifacts
differ is reported as INCORRECT regardless of speed.

Typical use:
  python3 bench/harness/run.py                 # default sweep: bench/configs/medium-*.json
  python3 bench/harness/run.py --quick         # small-*.json smoke run
  python3 bench/harness/run.py --tools lake,candidate --configs small-layered
  python3 bench/harness/run.py --cpus 0-15 --mem-gb 32 --repeat 5

Caps: `--cpus` pins the harness and everything it spawns to those CPUs and sets
LEAN_NUM_THREADS to their count (Lake 5 has no jobs flag; Lean's task threads
bound its concurrency). `--mem-gb` wraps each build in a transient systemd
user scope with MemoryMax when `systemd-run` is available (Linux); otherwise it
is recorded as unenforced. Defaults come from `bench/local.json` (gitignored)
or, failing that, half of the machine.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import resource
import shutil
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BENCH = ROOT / "bench"
CONFIGS = BENCH / "configs"
WORK = BENCH / "work"
RESULTS = ROOT / "research" / "results"
GEN = BENCH / "gen" / "generate.py"
SCHEMA_VERSION = 1

ALL_SCENARIOS = ("cold", "noop", "leaf", "swarm", "hub")
PRIMARY_SCENARIOS = ("noop", "leaf", "swarm", "hub")
ARTIFACT_EXTS = (".olean", ".ilean", ".c")


def log(msg: str) -> None:
    print(f"[bench {dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None, check: bool = True,
        capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, env=env, check=check,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.STDOUT if capture else None, text=True)


def git(*args: str, cwd: Path = ROOT) -> str:
    return run(["git", *args], cwd=cwd).stdout.strip()


# ---------------------------------------------------------------------------
# Caps and machine fingerprint
# ---------------------------------------------------------------------------

def parse_cpus(spec: str) -> list[int]:
    cpus: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            cpus.update(range(int(a), int(b) + 1))
        else:
            cpus.add(int(part))
    return sorted(cpus)


def default_caps() -> dict:
    local = BENCH / "local.json"
    caps = {"cpus": None, "mem_gb": None}
    if local.exists():
        caps.update(json.loads(local.read_text()))
    total = os.cpu_count() or 2
    if caps["cpus"] is None:
        caps["cpus"] = f"0-{max(0, total // 2 - 1)}"
    if caps["mem_gb"] is None:
        caps["mem_gb"] = max(1, total_mem_gb() // 2)
    return caps


def total_mem_gb() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // (1024 * 1024)
    except OSError:
        pass
    try:
        return int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") // (1024 ** 3))
    except (ValueError, OSError, AttributeError):
        return 0


def cpu_model() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def apply_caps(cpus: list[int]) -> bool:
    if hasattr(os, "sched_setaffinity"):
        try:
            os.sched_setaffinity(0, set(cpus))
            return True
        except OSError as e:
            log(f"warning: could not set CPU affinity: {e}")
    return False


def memory_wrapper(mem_gb: int | None) -> tuple[list[str], str]:
    """Command prefix enforcing the memory cap, and how it is enforced."""
    if not mem_gb:
        return [], "none"
    if shutil.which("systemd-run") and platform.system() == "Linux":
        probe = subprocess.run(["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={mem_gb}G", "--", "true"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode == 0:
            return ["systemd-run", "--user", "--scope", "-q", "-p", f"MemoryMax={mem_gb}G", "--"], "systemd-run MemoryMax"
    return [], "unenforced"


def machine_fingerprint(caps: dict, affinity_ok: bool, mem_mode: str) -> dict:
    host = socket.gethostname()
    return {
        "id": hashlib.sha256(host.encode()).hexdigest()[:8],
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpu": cpu_model(),
        "cpus_total": os.cpu_count(),
        "mem_total_gb": total_mem_gb(),
        "caps": {
            "cpus": caps["cpus"],
            "cpu_count": len(parse_cpus(caps["cpus"])),
            "cpu_affinity_enforced": affinity_ok,
            "mem_gb": caps["mem_gb"],
            "mem_enforcement": mem_mode,
        },
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def toolchain_prefix() -> Path:
    return Path(run(["lean", "--print-prefix"], cwd=ROOT).stdout.strip())


def blake_env(base: dict) -> dict:
    """Environment for running a source-built Blake binary.

    nixpkgs' elan wraps the toolchain's own `lake` to export LEAN_CC pointing at
    a NixOS-aware compiler driver (`<toolchain>/bin/cc`); a source-built lake is
    not wrapped, so do the same when that driver exists.
    """
    env = dict(base)
    cc = toolchain_prefix() / "bin" / "cc"
    if cc.exists() and "LEAN_CC" not in env:
        env["LEAN_CC"] = str(cc)
    return env


def build_blake(repo: Path, env: dict) -> Path:
    log(f"building Blake in {repo}")
    run(["lake", "build"], cwd=repo, env=env)
    exe = repo / ".lake" / "build" / "bin" / "lake"
    if not exe.exists():
        sys.exit(f"Blake build in {repo} produced no {exe}")
    return exe


def main_worktree(ref: str, env: dict) -> tuple[Path, str]:
    wt = WORK / "worktrees" / "main"
    commit = git("rev-parse", ref)
    if wt.exists():
        if git("rev-parse", "HEAD", cwd=wt) != commit:
            run(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT)
    if not wt.exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "worktree", "add", "--detach", str(wt), commit], cwd=ROOT)
    return wt, commit


def resolve_tools(names: list[str], env: dict, main_ref: str) -> dict:
    tools: dict[str, dict] = {}
    head = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    for name in names:
        if name == "lake":
            exe = shutil.which("lake")
            if not exe:
                sys.exit("stock `lake` not found on PATH (install elan)")
            version = run([exe, "--version"], cwd=ROOT).stdout.strip()
            tools[name] = {"exe": exe, "env": env, "version": version}
        elif name == "candidate":
            benv = blake_env(env)
            exe = build_blake(ROOT, benv)
            tools[name] = {"exe": str(exe), "env": benv, "commit": head, "dirty": dirty,
                           "version": run([str(exe), "--version"], cwd=ROOT, env=benv).stdout.strip()}
        elif name == "main":
            try:
                main_commit = git("rev-parse", main_ref)
            except subprocess.CalledProcessError:
                sys.exit(f"cannot resolve main ref {main_ref!r}; pass --main-ref")
            if main_commit == head and not dirty and "candidate" in names:
                log("main == candidate (clean HEAD on main); measuring it once as `candidate`")
                tools[name] = {"alias": "candidate", "commit": main_commit}
                continue
            benv = blake_env(env)
            wt, commit = main_worktree(main_ref, benv)
            exe = build_blake(wt, benv)
            tools[name] = {"exe": str(exe), "env": benv, "commit": commit,
                           "version": run([str(exe), "--version"], cwd=wt, env=benv).stdout.strip()}
        else:
            sys.exit(f"unknown tool {name!r} (expected lake, main, candidate)")
    return tools


# ---------------------------------------------------------------------------
# Corpora
# ---------------------------------------------------------------------------

def load_config(name: str) -> tuple[str, Path]:
    p = Path(name)
    if not p.exists():
        p = CONFIGS / f"{name}.json"
    if not p.exists():
        sys.exit(f"no such config: {name} (looked for {p})")
    return p.stem, p


def generate_corpus(cfg_name: str, cfg_path: Path) -> Path:
    src = WORK / "corpora" / cfg_name / "src"
    meta = src / "corpus.json"
    if meta.exists():
        existing = json.loads(meta.read_text())["config"]
        wanted = json.loads(cfg_path.read_text())
        gen_version = int(re.search(r"GENERATOR_VERSION = (\d+)", GEN.read_text()).group(1))
        if all(existing.get(k) == v for k, v in wanted.items()) and existing.get("generator_version") == gen_version:
            return src
        shutil.rmtree(src)
    log(f"generating corpus {cfg_name}")
    run([sys.executable, str(GEN), "--config", str(cfg_path), "--out", str(src)])
    return src


def fresh_copy(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".lake"))


def choose_targets(corpus: dict) -> dict:
    mods = corpus["modules"]
    n = len(mods)
    leaves = [m for m in mods if m["transitive_dependents"] == 0]
    leaf = max(leaves, key=lambda m: (m["layer"], m["name"]))
    hub = max(mods, key=lambda m: (m["transitive_dependents"], -mods.index(m)))
    # swarm: many modules edited at once, as agents working in parallel would do.
    # Agents mostly touch recent, little-imported modules, so draw from the half
    # of the corpus with the fewest transitive dependents, spread over the DAG.
    k = max(1, min(24, n // 10))
    by_fanout = sorted(mods, key=lambda m: (m["transitive_dependents"], m["name"]))
    pool = sorted(by_fanout[: max(k, n // 2)], key=lambda m: m["name"])
    step = max(1, len(pool) // k)
    swarm = [pool[i]["name"] for i in range(0, len(pool), step)][:k]
    return {"leaf": leaf["name"], "hub": hub["name"], "swarm": swarm,
            "hub_transitive_dependents": hub["transitive_dependents"]}


def module_file(corpus_dir: Path, corpus: dict, name: str) -> Path:
    for m in corpus["modules"]:
        if m["name"] == name:
            return corpus_dir / m["file"]
    raise KeyError(name)


def apply_edit(corpus_dir: Path, corpus: dict, names: list[str], stamp: int) -> None:
    for name in names:
        f = module_file(corpus_dir, corpus, name)
        with f.open("a") as fh:
            fh.write(f"\ntheorem {name}.bench_edit_{stamp} (n : Nat) : {name}.val n ≤ {name}.val n + {stamp} := by\n  omega\n")


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def timed_build(tool: dict, cwd: Path, mem_prefix: list[str], extra_env: dict) -> dict:
    env = dict(tool["env"])
    env.update(extra_env)
    cmd = [*mem_prefix, tool["exe"], "build"]
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    wall = time.perf_counter() - t0
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    jobs = len(re.findall(r"^✔", proc.stdout, flags=re.M))
    return {
        "wall_s": round(wall, 4),
        "cpu_user_s": round(after.ru_utime - before.ru_utime, 3),
        "cpu_sys_s": round(after.ru_stime - before.ru_stime, 3),
        "max_rss_mb": round(after.ru_maxrss / 1024, 1),  # largest single child (Linux: KiB)
        "exit_code": proc.returncode,
        "jobs": jobs,
        "log_tail": proc.stdout[-2000:] if proc.returncode != 0 else "",
    }


def artifact_manifest(build_dir: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for sub in ("lib", "ir"):
        base = build_dir / sub
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix in ARTIFACT_EXTS:
                manifest[str(p.relative_to(build_dir))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return manifest


def compare_manifests(ref: dict[str, str], other: dict[str, str]) -> dict:
    missing = sorted(set(ref) - set(other))
    extra = sorted(set(other) - set(ref))
    differ = sorted(k for k in ref.keys() & other.keys() if ref[k] != other[k])
    return {"identical": not (missing or extra or differ), "files_compared": len(ref),
            "missing": missing[:20], "extra": extra[:20], "differ": differ[:20],
            "n_missing": len(missing), "n_extra": len(extra), "n_differ": len(differ)}


def summarize(runs: list[dict]) -> dict:
    walls = [r["wall_s"] for r in runs if r["exit_code"] == 0]
    out = {"runs": runs, "ok": all(r["exit_code"] == 0 for r in runs)}
    if walls:
        out["median_wall_s"] = round(statistics.median(walls), 4)
        out["min_wall_s"] = round(min(walls), 4)
        out["max_wall_s"] = round(max(walls), 4)
    return out


def bench_tool(tool_name: str, tool: dict, cfg_name: str, src: Path, corpus: dict, targets: dict,
               scenarios: list[str], repeat: int, mem_prefix: list[str], extra_env: dict) -> tuple[dict, dict]:
    d = WORK / "corpora" / cfg_name / tool_name
    fresh_copy(src, d)
    results: dict[str, dict] = {}
    stamp = 0
    log(f"[{cfg_name}/{tool_name}] cold build")
    cold = timed_build(tool, d, mem_prefix, extra_env)
    results["cold"] = summarize([cold])
    if cold["exit_code"] != 0:
        log(f"[{cfg_name}/{tool_name}] cold build FAILED (exit {cold['exit_code']})")
        return results, {}
    log(f"[{cfg_name}/{tool_name}] cold: {cold['wall_s']:.1f}s wall, {cold['cpu_user_s'] + cold['cpu_sys_s']:.1f}s cpu, {cold['jobs']} jobs")
    manifest = artifact_manifest(d / ".lake" / "build")
    for scen in scenarios:
        if scen == "cold":
            continue
        runs = []
        for i in range(repeat):
            if scen == "leaf":
                stamp += 1
                apply_edit(d, corpus, [targets["leaf"]], stamp)
            elif scen == "hub":
                stamp += 1
                apply_edit(d, corpus, [targets["hub"]], stamp)
            elif scen == "swarm":
                stamp += 1
                apply_edit(d, corpus, targets["swarm"], stamp)
            r = timed_build(tool, d, mem_prefix, extra_env)
            runs.append(r)
            if r["exit_code"] != 0:
                log(f"[{cfg_name}/{tool_name}] {scen} run {i + 1} FAILED (exit {r['exit_code']})")
                break
        results[scen] = summarize(runs)
        if results[scen].get("median_wall_s") is not None:
            log(f"[{cfg_name}/{tool_name}] {scen}: median {results[scen]['median_wall_s']:.2f}s "
                f"(min {results[scen]['min_wall_s']:.2f}, max {results[scen]['max_wall_s']:.2f}, "
                f"jobs {runs[0]['jobs']})")
    return results, manifest


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def ratios(measurements: dict, tools: list[str]) -> dict:
    """speedup[tool][scenario] = lake_median / tool_median, geometric mean over corpora."""
    out: dict = {}
    for tool in tools:
        if tool == "lake":
            continue
        out[tool] = {}
        for base in ("lake", "main"):
            if base not in tools or base == tool:
                continue
            per_scen: dict[str, list[float]] = {}
            for cfg_name, per_tool in measurements.items():
                for scen in ALL_SCENARIOS:
                    a = per_tool.get(base, {}).get(scen, {}).get("median_wall_s")
                    b = per_tool.get(tool, {}).get(scen, {}).get("median_wall_s")
                    if a and b:
                        per_scen.setdefault(scen, []).append(a / b)
            out[tool][f"vs_{base}"] = {
                scen: round(statistics.geometric_mean(v), 4) for scen, v in per_scen.items()}
            prim = [out[tool][f"vs_{base}"][s] for s in PRIMARY_SCENARIOS if s in out[tool][f"vs_{base}"]]
            if prim:
                out[tool][f"vs_{base}"]["primary_geomean"] = round(statistics.geometric_mean(prim), 4)
    return out


def print_table(result: dict) -> None:
    tools = result["tool_order"]
    print()
    print(f"{'corpus':<16}{'scenario':<8}" + "".join(f"{t:>14}" for t in tools) + "   (median wall s)")
    for cfg_name, per_tool in result["measurements"].items():
        for scen in ALL_SCENARIOS:
            row = []
            for t in tools:
                s = per_tool.get(t, {}).get(scen, {})
                row.append(f"{s['median_wall_s']:>14.2f}" if s.get("median_wall_s") is not None else f"{'-':>14}")
            if any(c.strip() != "-" for c in row):
                print(f"{cfg_name:<16}{scen:<8}" + "".join(row))
    print()
    for tool, r in result["speedup"].items():
        for base, per in r.items():
            print(f"speedup of {tool} {base.replace('_', ' ')}: " +
                  ", ".join(f"{k}={v:.3f}x" for k, v in per.items()))
    for tool, c in result["correctness"].items():
        state = "identical artifacts to lake" if all(v["identical"] for v in c.values()) else "ARTIFACTS DIFFER FROM LAKE"
        print(f"correctness of {tool}: {state}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--configs", default=None,
                    help="comma-separated config names or paths (default: bench/configs/medium-*.json)")
    ap.add_argument("--quick", action="store_true", help="use bench/configs/small-*.json")
    ap.add_argument("--tools", default="lake,main,candidate", help="comma-separated: lake, main, candidate")
    ap.add_argument("--main-ref", default=None, help="git ref for `main` (default: origin/main, else main)")
    ap.add_argument("--scenarios", default=",".join(ALL_SCENARIOS))
    ap.add_argument("--repeat", type=int, default=3, help="runs per incremental scenario (median reported)")
    ap.add_argument("--cpus", default=None, help="CPU list to pin to, e.g. 0-15 (default: bench/local.json or half)")
    ap.add_argument("--mem-gb", type=int, default=None, help="memory cap in GiB (default: bench/local.json or half)")
    ap.add_argument("--out", default=None, help="result JSON path (default: research/results/<stamp>.json)")
    ap.add_argument("--label", default="", help="free-form label stored in the result (e.g. idea name)")
    ap.add_argument("--keep", action="store_true", help="keep per-tool corpus copies after the run")
    args = ap.parse_args()

    caps = default_caps()
    if args.cpus:
        caps["cpus"] = args.cpus
    if args.mem_gb is not None:
        caps["mem_gb"] = args.mem_gb
    cpus = parse_cpus(caps["cpus"])
    affinity_ok = apply_caps(cpus)
    mem_prefix, mem_mode = memory_wrapper(caps["mem_gb"])
    extra_env = {"LEAN_NUM_THREADS": str(len(cpus))}
    machine = machine_fingerprint(caps, affinity_ok, mem_mode)
    log(f"caps: cpus={caps['cpus']} ({len(cpus)}), affinity={'on' if affinity_ok else 'OFF'}, "
        f"mem={caps['mem_gb']}G via {mem_mode}, LEAN_NUM_THREADS={len(cpus)}")

    if args.configs:
        cfg_names = [c for c in args.configs.split(",") if c]
    else:
        pattern = "small-*.json" if args.quick else "medium-*.json"
        cfg_names = sorted(p.stem for p in CONFIGS.glob(pattern))
    if not cfg_names:
        sys.exit("no configs selected")
    scenarios = [s for s in args.scenarios.split(",") if s]
    for s in scenarios:
        if s not in ALL_SCENARIOS:
            sys.exit(f"unknown scenario {s}")

    env = dict(os.environ)
    env.setdefault("LANG", "C.UTF-8")
    main_ref = args.main_ref or ("origin/main" if git("branch", "-r", "--list", "origin/main") else "main")
    tool_names = [t for t in args.tools.split(",") if t]
    tools = resolve_tools(tool_names, env, main_ref)
    tool_order = [t for t in tool_names if "alias" not in tools[t]]

    measurements: dict = {}
    correctness: dict = {}
    corpora_meta: dict = {}
    t_start = time.time()
    for name in cfg_names:
        cfg_name, cfg_path = load_config(name)
        src = generate_corpus(cfg_name, cfg_path)
        corpus = json.loads((src / "corpus.json").read_text())
        targets = choose_targets(corpus)
        corpora_meta[cfg_name] = {"config": corpus["config"], "stats": corpus["stats"], "targets": targets}
        log(f"corpus {cfg_name}: {corpus['stats']['modules']} modules, {corpus['stats']['lines']} lines, "
            f"hub {targets['hub']} ({targets['hub_transitive_dependents']} transitive dependents), "
            f"swarm edits {len(targets['swarm'])}")
        measurements[cfg_name] = {}
        manifests: dict[str, dict] = {}
        for tool_name in tool_order:
            res, manifest = bench_tool(tool_name, tools[tool_name], cfg_name, src, corpus, targets,
                                       scenarios, args.repeat, mem_prefix, extra_env)
            measurements[cfg_name][tool_name] = res
            manifests[tool_name] = manifest
            if not args.keep:
                shutil.rmtree(WORK / "corpora" / cfg_name / tool_name, ignore_errors=True)
        if "lake" in manifests:
            for tool_name, m in manifests.items():
                if tool_name != "lake":
                    correctness.setdefault(tool_name, {})[cfg_name] = compare_manifests(manifests["lake"], m)
    for alias, t in tools.items():
        if "alias" in t:
            for cfg_name in measurements:
                measurements[cfg_name][alias] = measurements[cfg_name][t["alias"]]
            if t["alias"] in correctness:
                correctness[alias] = correctness[t["alias"]]
    order_with_aliases = tool_names

    result = {
        "schema": SCHEMA_VERSION,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "label": args.label,
        "duration_s": round(time.time() - t_start, 1),
        "machine": machine,
        "toolchain": (ROOT / "lean-toolchain").read_text().strip(),
        "git": {"commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
                "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))},
        "tools": {k: {kk: vv for kk, vv in v.items() if kk != "env"} for k, v in tools.items()},
        "tool_order": order_with_aliases,
        "repeat": args.repeat,
        "scenarios": scenarios,
        "corpora": corpora_meta,
        "measurements": measurements,
        "speedup": ratios(measurements, order_with_aliases),
        "correctness": correctness,
    }
    out = Path(args.out) if args.out else RESULTS / (
        f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{machine['id']}-{result['git']['commit'][:7]}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n")
    print_table(result)
    log(f"wrote {out}")


if __name__ == "__main__":
    main()
