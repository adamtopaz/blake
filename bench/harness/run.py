#!/usr/bin/env python3
"""Blake benchmark harness.

Measures stock Lake, Blake `main`, and the candidate (the current checkout) on
the same machine, under the same CPU and memory caps, on generated corpora
(see `bench/gen/generate.py`), decides whether the candidate improved on `main`,
and writes one JSON result file.

Scenarios (each tool gets its own fresh copy of the corpus; the tools are
interleaved run by run so that drift of the machine affects them all alike):
  cold   full build from nothing (context; secondary target)
  noop   `build` with nothing changed: pure build-tool overhead
  leaf   edit a module nobody imports, rebuild
  swarm  edit many little-imported modules spread over the DAG at once, rebuild
  hub    edit the most widely imported module, rebuild (large downstream rebuild)
  break  (untimed, always run) rename a theorem in the hub so every dependent
         must fail; the build must fail exactly as stock Lake's does; then
         restore and rebuild

Edits accumulate (each is a new appended theorem), so no restore step is needed
and every tool sees exactly the same sequence of edits.

Correctness: after *every* build, each `.olean`, `.ilean`, and `.c` under
`.lake/build` is hashed and compared with stock Lake's at the same step. A
candidate whose artifacts ever differ, whose builds fail, or which leaves
processes behind is INCORRECT regardless of speed.

Verdict (printed and stored): per corpus and scenario the candidate is compared
with `main`. Sub-second build times on a shared machine are bimodal (the same
binary alternates between two modes ~20% apart), so scenarios whose first run
takes under 2 s are run --repeat-fast times (default 10) and compared on their
*minimum* with a 7% threshold; longer scenarios are run --repeat times (default
3) and compared on their *median* with threshold max(3%, spread/median of either
tool); `cold` runs once and uses 7%. Improved iff candidate < (1 - t) * main,
regressed iff candidate > (1 + t) * main. Overall: `failure` if anything is
incorrect or any scenario regressed; `success` if at least one primary scenario
improved and nothing regressed; otherwise `neutral`. Tables and speedup lines
always report medians.

Typical use:
  python3 bench/harness/run.py                 # standard sweep: medium-*, lake+main+candidate, repeat 3
  python3 bench/harness/run.py --quick         # small-*.json smoke run (not a standard run)
  python3 bench/harness/run.py --cpus 0-15 --mem-gb 32

Only a *standard* run (default configs, tools, scenarios, repeat >= 3, clean
git tree, enforced CPU cap) is written to research/results/; anything else goes
to bench/work/results/ unless --out is given.

Caps: `--cpus` pins the harness and everything it spawns to those CPUs (Linux
only) and sets LEAN_NUM_THREADS to their count (Lake 5 has no jobs flag; Lean's
task threads bound its concurrency). `--mem-gb` wraps each build in a transient
systemd user scope with MemoryMax where available; the same scope is used to
account for the whole process tree and to kill stragglers. Defaults come from
`bench/local.json` (gitignored) or, failing that, half of the machine.

The environment is scrubbed of every LAKE_*/LEAN_*/ELAN_TOOLCHAIN/XDG_CACHE_HOME
variable, and each tool gets private, empty cache and temp directories, so no
state can leak between tools or runs.
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
SCOPE_STATS = HERE / "scope_stats.sh"
SCHEMA_VERSION = 2

ALL_SCENARIOS = ("cold", "noop", "leaf", "swarm", "hub")
PRIMARY_SCENARIOS = ("noop", "leaf", "swarm", "hub")
DEFAULT_TOOLS = "lake,main,candidate"
ARTIFACT_EXTS = (".olean", ".ilean", ".c")
BUILD_TIMEOUT_S = 1800
NOISE_FLOOR = 0.03        # median-based comparison of long scenarios
NOISE_FLOOR_FAST = 0.07   # min-based comparison of scenarios under FAST_SECONDS
FAST_SECONDS = 2.0
COLD_THRESHOLD = 0.07
SCRUB_PREFIXES = ("LAKE_", "LEAN_")
SCRUB_EXACT = ("LEAN", "ELAN_TOOLCHAIN", "XDG_CACHE_HOME", "TMPDIR", "CC", "AR", "CURL")


def log(msg: str) -> None:
    print(f"[bench {dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def banner(msg: str) -> None:
    print("\n" + "!" * 78 + f"\n! {msg}\n" + "!" * 78 + "\n", flush=True)


def run(cmd: list[str], cwd: Path | None = None, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        print(proc.stdout[-4000:], file=sys.stderr)
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(cmd)}")
    return proc


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> str:
    return run(["git", *args], cwd=cwd, check=check).stdout.strip()


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


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


def available_cpus() -> list[int]:
    if hasattr(os, "sched_getaffinity"):
        return sorted(os.sched_getaffinity(0))
    return list(range(os.cpu_count() or 1))


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


def default_caps() -> dict:
    caps = {"cpus": None, "mem_gb": None}
    local = BENCH / "local.json"
    if local.exists():
        caps.update(json.loads(local.read_text(encoding="utf-8")))
    avail = available_cpus()
    if caps["cpus"] is None:
        half = avail[: max(1, len(avail) // 2)]
        caps["cpus"] = ",".join(str(c) for c in half)
    if caps["mem_gb"] is None:
        caps["mem_gb"] = max(1, total_mem_gb() // 2)
    return caps


def apply_cpu_cap(cpus: list[int]) -> tuple[list[int], bool]:
    """Pin to `cpus`; return the CPUs actually in effect and whether pinning worked."""
    if not hasattr(os, "sched_setaffinity"):
        return cpus, False
    try:
        os.sched_setaffinity(0, set(cpus))
    except OSError as e:
        log(f"warning: could not set CPU affinity: {e}")
        return cpus, False
    actual = sorted(os.sched_getaffinity(0))
    if actual != cpus:
        log(f"warning: requested cpus {cpus} but affinity is {actual} (outside this process's allowed set?)")
    return actual, True


def memory_controller_delegated() -> bool:
    uid = os.getuid()
    p = Path(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/cgroup.controllers")
    try:
        return "memory" in p.read_text().split()
    except OSError:
        return False


def scope_support(mem_gb: int | None) -> tuple[bool, str]:
    """Whether transient systemd user scopes can be used, and how memory is enforced."""
    if platform.system() != "Linux" or not shutil.which("systemd-run"):
        return False, "unenforced"
    probe = subprocess.run(["systemd-run", "--user", "--scope", "-q", "-p", "MemoryMax=1G", "--", "true"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe.returncode != 0:
        return False, "unenforced"
    if not mem_gb:
        return True, "none"
    return True, ("systemd-run MemoryMax" if memory_controller_delegated() else "unenforced (memory controller not delegated)")


def cpu_model() -> str:
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    if platform.system() == "Darwin":
        out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True)
        if out.returncode == 0:
            return out.stdout.strip()
    return platform.processor() or platform.machine()


def machine_id(cpu: str) -> str:
    seed = ""
    try:
        seed = Path("/etc/machine-id").read_text().strip()
    except OSError:
        if platform.system() == "Darwin":
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, text=True)
            m = re.search(r'"IOPlatformUUID" = "([^"]+)"', out.stdout)
            seed = m.group(1) if m else ""
    if not seed:
        seed = platform.node()
    return hashlib.sha256(f"{seed}|{cpu}".encode()).hexdigest()[:8]


# ---------------------------------------------------------------------------
# Environment hygiene and tools
# ---------------------------------------------------------------------------

def scrubbed_env() -> tuple[dict, list[str]]:
    env = {}
    scrubbed = []
    for k, v in os.environ.items():
        if k.startswith(SCRUB_PREFIXES) or k in SCRUB_EXACT:
            scrubbed.append(k)
            continue
        env[k] = v
    env.setdefault("LANG", "C.UTF-8")
    return env, sorted(scrubbed)


def toolchain_prefix(env: dict) -> Path:
    return Path(run(["lean", "--print-prefix"], cwd=ROOT, env=env).stdout.strip())


def blake_env(base: dict, prefix: Path) -> dict:
    """Environment for running a source-built Blake binary.

    nixpkgs' elan wraps the toolchain's own `lake` to export LEAN_CC pointing at
    a NixOS-aware compiler driver (`<toolchain>/bin/cc`); a source-built lake is
    not wrapped, so do the same when that driver exists.
    """
    env = dict(base)
    cc = prefix / "bin" / "cc"
    if cc.exists():
        env["LEAN_CC"] = str(cc)
    return env


def build_blake(repo: Path, env: dict) -> Path:
    log(f"building Blake in {repo}")
    run(["lake", "build"], cwd=repo, env=env)
    exe = repo / ".lake" / "build" / "bin" / "lake"
    if not exe.exists():
        sys.exit(f"Blake build in {repo} produced no {exe}")
    return exe


def main_worktree(commit: str, env: dict) -> Path:
    wt = WORK / "worktrees" / "main"
    if wt.exists() and git("rev-parse", "HEAD", cwd=wt, check=False) != commit:
        run(["git", "worktree", "remove", "--force", str(wt)], cwd=ROOT, check=False)
        shutil.rmtree(wt, ignore_errors=True)
    if not wt.exists():
        wt.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "worktree", "prune"], cwd=ROOT, check=False)
        run(["git", "worktree", "add", "--detach", str(wt), commit], cwd=ROOT)
    return wt


def resolve_main_ref(explicit: str | None) -> tuple[str, str]:
    if explicit:
        return git("rev-parse", explicit), explicit
    if git("rev-parse", "--verify", "-q", "origin/main", check=False):
        base = git("merge-base", "HEAD", "origin/main")
        return base, "merge-base(HEAD, origin/main)"
    if git("rev-parse", "--verify", "-q", "main", check=False):
        return git("merge-base", "HEAD", "main"), "merge-base(HEAD, main)"
    sys.exit("cannot find origin/main or main; pass --main-ref")


def resolve_tools(names: list[str], env: dict, main_ref: str | None) -> tuple[dict, dict]:
    prefix = toolchain_prefix(env)
    tools: dict[str, dict] = {}
    head = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
    main_commit, main_how = resolve_main_ref(main_ref) if "main" in names else (None, None)
    for name in names:
        if name == "lake":
            exe = prefix / "bin" / "lake"
            if not exe.exists():
                sys.exit(f"stock lake not found at {exe}")
            if ROOT in exe.resolve().parents:
                sys.exit(f"stock lake {exe} resolves inside the Blake checkout; refusing")
            tools[name] = {"exe": str(exe), "env": dict(env), "realpath": str(exe.resolve()),
                           "sha256": sha256_file(exe.resolve()),
                           "version": run([str(exe), "--version"], cwd=ROOT, env=env).stdout.strip()}
        elif name == "candidate":
            benv = blake_env(env, prefix)
            exe = build_blake(ROOT, benv)
            tools[name] = {"exe": str(exe), "env": benv, "commit": head, "dirty": dirty,
                           "sha256": sha256_file(exe),
                           "version": run([str(exe), "--version"], cwd=ROOT, env=benv).stdout.strip()}
        elif name == "main":
            if main_commit == head and not dirty and "candidate" in names:
                log("main == candidate (clean HEAD at the baseline); measuring it once as `candidate`")
                tools[name] = {"alias": "candidate", "commit": main_commit, "ref": main_how}
                continue
            benv = blake_env(env, prefix)
            wt = main_worktree(main_commit, benv)
            exe = build_blake(wt, benv)
            tools[name] = {"exe": str(exe), "env": benv, "commit": main_commit, "ref": main_how,
                           "sha256": sha256_file(exe),
                           "version": run([str(exe), "--version"], cwd=wt, env=benv).stdout.strip()}
        else:
            sys.exit(f"unknown tool {name!r} (expected lake, main, candidate)")
    return tools, {"prefix": str(prefix), "lean_version": run(["lean", "--version"], cwd=ROOT, env=env).stdout.strip()}


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


def corpus_content_hash(src: Path) -> str:
    h = hashlib.sha256()
    for p in sorted((src / "Corpus").glob("*.lean")):
        h.update(p.read_bytes())
    return h.hexdigest()


def generate_corpus(cfg_name: str, cfg_path: Path, env: dict) -> Path:
    src = WORK / "corpora" / cfg_name / "src"
    meta = src / "corpus.json"
    gen_version = int(re.search(r"GENERATOR_VERSION = (\d+)", GEN.read_text(encoding="utf-8")).group(1))
    if src.exists():
        ok = False
        if meta.exists():
            corpus = json.loads(meta.read_text(encoding="utf-8"))
            existing = corpus["config"]
            wanted = json.loads(cfg_path.read_text(encoding="utf-8"))
            ok = (all(existing.get(k) == v for k, v in wanted.items())
                  and existing.get("generator_version") == gen_version
                  and corpus["stats"].get("content_sha256") == corpus_content_hash(src))
        if ok:
            return src
        log(f"corpus {cfg_name} is stale or damaged; regenerating")
        shutil.rmtree(src)
    log(f"generating corpus {cfg_name}")
    run([sys.executable, str(GEN), "--config", str(cfg_path), "--out", str(src)], env=env)
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
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"\ntheorem {name}.bench_edit_{stamp} (n : Nat) : {name}.val n ≤ {name}.val n + {stamp} := by\n  omega\n")


def apply_break(corpus_dir: Path, corpus: dict, name: str) -> str:
    """Rename the hub's `val_ge`, which every dependent uses. Returns the original text."""
    f = module_file(corpus_dir, corpus, name)
    original = f.read_text(encoding="utf-8")
    broken = original.replace("theorem val_ge ", "theorem val_ge_renamed ", 1)
    if broken == original:
        raise RuntimeError(f"could not apply breaking edit to {name}")
    f.write_text(broken, encoding="utf-8")
    return original


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

class Session:
    """One tool working on one corpus copy, with private caches and scopes."""

    def __init__(self, tool_name: str, tool: dict, corpus_dir: Path, use_scope: bool, mem_gb: int | None,
                 extra_env: dict):
        self.tool_name = tool_name
        self.tool = tool
        self.dir = corpus_dir
        self.use_scope = use_scope
        self.mem_gb = mem_gb
        cache = corpus_dir.parent / f".bench-{tool_name}"
        if cache.exists():
            shutil.rmtree(cache)
        for sub in ("lake", "xdg", "tmp"):
            (cache / sub).mkdir(parents=True)
        (cache / "lake-config.toml").write_text("", encoding="utf-8")
        self.env = dict(tool["env"])
        self.env.update(extra_env)
        self.env.update({"LAKE_CACHE_DIR": str(cache / "lake"), "XDG_CACHE_HOME": str(cache / "xdg"),
                         "TMPDIR": str(cache / "tmp"), "LAKE_CONFIG": str(cache / "lake-config.toml"),
                         "LAKE_NO_CACHE": "1"})
        self.cache = cache
        self.counter = 0
        self.stragglers = 0

    def build(self) -> dict:
        self.counter += 1
        unit = f"blake-bench-{os.getpid()}-{self.tool_name}-{self.counter}"
        stats_file = self.cache / "stats.txt"
        stats_file.unlink(missing_ok=True)
        env = dict(self.env)
        env["BLAKE_BENCH_STATS"] = str(stats_file)
        if self.use_scope:
            props = ["-p", f"MemoryMax={self.mem_gb}G"] if self.mem_gb else []
            cmd = ["systemd-run", "--user", "--scope", "-q", f"--unit={unit}", *props, "--",
                   "bash", str(SCOPE_STATS), self.tool["exe"], "build"]
        else:
            cmd = ["bash", str(SCOPE_STATS), self.tool["exe"], "build"]
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        t0 = time.perf_counter()
        timed_out = False
        try:
            proc = subprocess.run(cmd, cwd=self.dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace", timeout=BUILD_TIMEOUT_S,
                                  start_new_session=True)
            out, code = proc.stdout, proc.returncode
        except subprocess.TimeoutExpired as e:
            out, code, timed_out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), -1, True
        wall = time.perf_counter() - t0
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        stragglers = self.reap(unit)
        stats = self.read_stats(stats_file)
        maxrss = after.ru_maxrss / (1024 * 1024 if platform.system() == "Darwin" else 1024)
        return {
            "wall_s": round(wall, 4),
            "cpu_user_s": round(stats.get("user_usec", (after.ru_utime - before.ru_utime) * 1e6) / 1e6, 3),
            "cpu_sys_s": round(stats.get("system_usec", (after.ru_stime - before.ru_stime) * 1e6) / 1e6, 3),
            "peak_mem_mb": round(stats["memory_peak"] / (1024 * 1024), 1) if "memory_peak" in stats else None,
            "max_rss_mb_largest_child": round(maxrss, 1),
            "exit_code": code,
            "timed_out": timed_out,
            "stragglers": stragglers,
            "jobs": len(re.findall(r"^✔", out, flags=re.M)),
            "failed_jobs": len(re.findall(r"^✖", out, flags=re.M)),
            "log_tail": out[-3000:] if code != 0 else "",
        }

    def reap(self, unit: str) -> int:
        """Kill anything the build left behind; return how many processes there were."""
        if self.use_scope:
            show = subprocess.run(["systemctl", "--user", "show", "-p", "ActiveState", "-p", "NPids", f"{unit}.scope"],
                                  capture_output=True, text=True)
            props = dict(line.split("=", 1) for line in show.stdout.splitlines() if "=" in line)
            n = 0
            if props.get("ActiveState") == "active":
                n = max(1, int(props.get("NPids", "1") or 1))
                subprocess.run(["systemctl", "--user", "stop", f"{unit}.scope"], capture_output=True)
            self.stragglers += n
            return n
        return 0

    @staticmethod
    def read_stats(path: Path) -> dict:
        stats: dict = {}
        if not path.exists():
            return stats
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("memory_peak="):
                stats["memory_peak"] = int(line.split("=", 1)[1])
            elif line.startswith(("usage_usec ", "user_usec ", "system_usec ")):
                k, v = line.split()
                stats[k] = int(v)
        return stats


def artifact_manifest(build_dir: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for sub in ("lib", "ir"):
        base = build_dir / sub
        if not base.exists():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix in ARTIFACT_EXTS:
                manifest[str(p.relative_to(build_dir))] = sha256_file(p)
    return manifest


def compare_manifests(ref: dict[str, str], other: dict[str, str], expected_files: int) -> dict:
    missing = sorted(set(ref) - set(other))
    extra = sorted(set(other) - set(ref))
    differ = sorted(k for k in ref.keys() & other.keys() if ref[k] != other[k])
    complete = len(ref) == expected_files
    return {"identical": complete and not (missing or extra or differ), "files_compared": len(ref),
            "files_expected": expected_files, "n_missing": len(missing), "n_extra": len(extra), "n_differ": len(differ),
            "missing": missing[:20], "extra": extra[:20], "differ": differ[:20]}


def summarize(runs: list[dict]) -> dict:
    walls = [r["wall_s"] for r in runs if r["exit_code"] == 0]
    out = {"runs": runs, "ok": bool(runs) and all(r["exit_code"] == 0 and not r["stragglers"] for r in runs)}
    if walls:
        out["median_wall_s"] = round(statistics.median(walls), 4)
        out["min_wall_s"] = round(min(walls), 4)
        out["max_wall_s"] = round(max(walls), 4)
    return out


def bench_corpus(cfg_name: str, src: Path, corpus: dict, targets: dict, tools: dict, order: list[str],
                 scenarios: list[str], repeat: int, repeat_fast: int, use_scope: bool, mem_gb: int | None,
                 extra_env: dict) -> tuple[dict, dict]:
    """Measure every tool on one corpus, interleaving the tools run by run.

    Each tool gets its own copy of the corpus. Cold builds happen in `order`;
    then, for every repetition of every scenario, the same edit is applied to
    every copy and the tools are built one after another in a rotating order,
    so that drift of the machine over time (thermal state, caches, background
    load) affects all tools alike instead of whichever ran last.
    Returns ({tool: per-scenario results}, {tool: {step: manifest}}).
    """
    sessions: dict[str, Session] = {}
    for name in order:
        d = WORK / "corpora" / cfg_name / name
        fresh_copy(src, d)
        sessions[name] = Session(name, tools[name], d, use_scope, mem_gb, extra_env)
    results: dict[str, dict] = {name: {} for name in order}
    manifests: dict[str, dict] = {name: {} for name in order}
    failed: set[str] = set()

    def record(name: str, step: str) -> None:
        manifests[name][step] = artifact_manifest(sessions[name].dir / ".lake" / "build")

    for name in order:
        log(f"[{cfg_name}/{name}] cold build")
        cold = sessions[name].build()
        results[name]["cold"] = summarize([cold])
        record(name, "cold")
        if cold["exit_code"] != 0 or cold["stragglers"]:
            log(f"[{cfg_name}/{name}] cold build FAILED (exit {cold['exit_code']}){' (timed out)' if cold['timed_out'] else ''}")
            failed.add(name)
            continue
        log(f"[{cfg_name}/{name}] cold: {cold['wall_s']:.1f}s wall, {cold['cpu_user_s'] + cold['cpu_sys_s']:.1f}s cpu, "
            f"peak mem {cold['peak_mem_mb'] or '?'} MB, {cold['jobs']} jobs")

    stamp = 0
    for scen in scenarios:
        if scen == "cold":
            continue
        runs: dict[str, list[dict]] = {name: [] for name in order}
        n_runs = repeat
        i = 0
        while i < n_runs:
            stamp += 1
            edit = {"leaf": [targets["leaf"]], "hub": [targets["hub"]], "swarm": targets["swarm"]}.get(scen)
            rotation = order[i % len(order):] + order[: i % len(order)]
            for name in rotation:
                if name in failed:
                    continue
                if edit:
                    apply_edit(sessions[name].dir, corpus, edit, stamp)
                r = sessions[name].build()
                runs[name].append(r)
                record(name, f"{scen}/{i}")
                if r["exit_code"] != 0 or r["stragglers"]:
                    log(f"[{cfg_name}/{name}] {scen} run {i + 1} FAILED (exit {r['exit_code']}, stragglers {r['stragglers']})")
                    failed.add(name)
            if i == 0:
                first = [runs[n][0]["wall_s"] for n in order if runs[n]]
                if first and max(first) < FAST_SECONDS:
                    n_runs = max(repeat, repeat_fast)  # cheap scenario: take many samples
            i += 1
        for name in order:
            results[name][scen] = summarize(runs[name])
            res = results[name][scen]
            if res.get("median_wall_s") is not None:
                log(f"[{cfg_name}/{name}] {scen}: median {res['median_wall_s']:.2f}s "
                    f"(min {res['min_wall_s']:.2f}, max {res['max_wall_s']:.2f}, n {len(runs[name])}, jobs {runs[name][0]['jobs']})")

    # breaking edit: every dependent of the hub must fail to build, then recover
    for name in order:
        if name in failed:
            results[name]["break"] = {"ok": False, "skipped": "earlier failure"}
            results[name]["stragglers_total"] = sessions[name].stragglers
            continue
        s = sessions[name]
        original = apply_break(s.dir, corpus, targets["hub"])
        broken = s.build()
        module_file(s.dir, corpus, targets["hub"]).write_text(original, encoding="utf-8")
        recovered = s.build()
        record(name, "recovered")
        results[name]["break"] = {"broken_exit_code": broken["exit_code"], "broken_failed_jobs": broken["failed_jobs"],
                                  "recovered_exit_code": recovered["exit_code"], "recovered_wall_s": recovered["wall_s"],
                                  "ok": broken["exit_code"] != 0 and recovered["exit_code"] == 0
                                  and not (broken["stragglers"] or recovered["stragglers"])}
        log(f"[{cfg_name}/{name}] break: broken build exit {broken['exit_code']} ({broken['failed_jobs']} failed jobs), "
            f"recovered exit {recovered['exit_code']}")
        results[name]["stragglers_total"] = s.stragglers
    return results, manifests


# ---------------------------------------------------------------------------
# Verdict and reporting
# ---------------------------------------------------------------------------

def correctness_for(tool_name: str, per_cfg_manifests: dict, per_cfg_results: dict, corpora: dict) -> dict:
    """Compare a tool's artifacts with lake's at every step, plus build health."""
    out: dict = {}
    for cfg_name, manifests in per_cfg_manifests.items():
        lake_m = per_cfg_manifests_lake(per_cfg_manifests, cfg_name)
        expected = 3 * corpora[cfg_name]["stats"]["modules"]
        steps = {}
        for step, ref in lake_m.items():
            steps[step] = compare_manifests(ref, manifests.get(tool_name, {}).get(step, {}), expected)
        res = per_cfg_results[cfg_name].get(tool_name, {})
        lake_res = per_cfg_results[cfg_name].get("lake", {})
        builds_ok = all(v.get("ok", False) for k, v in res.items() if k in ALL_SCENARIOS)
        brk = res.get("break", {})
        break_ok = bool(brk.get("ok")) and brk.get("broken_failed_jobs") == lake_res.get("break", {}).get("broken_failed_jobs")
        out[cfg_name] = {
            "identical": bool(steps) and all(v["identical"] for v in steps.values()),
            "builds_ok": builds_ok,
            "break_ok": break_ok,
            "stragglers": res.get("stragglers_total", 0),
            "steps": steps,
        }
    return out


def per_cfg_manifests_lake(per_cfg_manifests: dict, cfg_name: str) -> dict:
    return per_cfg_manifests[cfg_name].get("lake", {})


def verdicts(measurements: dict, correctness: dict, tools: list[str]) -> dict:
    out: dict = {"per_scenario": {}, "improved": [], "regressed": [], "overall": "n/a", "reasons": []}
    if "candidate" not in tools:
        return out
    incorrect = []
    for cfg_name, c in correctness.get("candidate", {}).items():
        if not c["identical"]:
            incorrect.append(f"{cfg_name}: artifacts differ from lake")
        if not c["builds_ok"]:
            incorrect.append(f"{cfg_name}: a build failed or left processes behind")
        if not c["break_ok"]:
            incorrect.append(f"{cfg_name}: breaking-edit check failed")
    out["reasons"] = incorrect
    if "main" not in tools:
        out["overall"] = "failure" if incorrect else "n/a (no main measured)"
        return out
    for cfg_name, per_tool in measurements.items():
        for scen in ALL_SCENARIOS:
            m = per_tool.get("main", {}).get(scen, {})
            c = per_tool.get("candidate", {}).get(scen, {})
            if m.get("median_wall_s") is None or c.get("median_wall_s") is None:
                continue
            if scen == "cold":
                t, stat = COLD_THRESHOLD, "median"
            elif m["median_wall_s"] < FAST_SECONDS:
                t, stat = NOISE_FLOOR_FAST, "min"
            else:
                t, stat = max(NOISE_FLOOR, (m["max_wall_s"] - m["min_wall_s"]) / m["median_wall_s"],
                              (c["max_wall_s"] - c["min_wall_s"]) / c["median_wall_s"]), "median"
            mv, cv = m[f"{stat}_wall_s"], c[f"{stat}_wall_s"]
            ratio = mv / cv
            state = "improved" if cv < (1 - t) * mv else ("regressed" if cv > (1 + t) * mv else "same")
            out["per_scenario"][f"{cfg_name}/{scen}"] = {"statistic": stat, "main_s": mv, "candidate_s": cv,
                                                          "speedup": round(ratio, 4), "threshold": round(t, 4), "state": state}
            if state == "improved":
                out["improved"].append(f"{cfg_name}/{scen}")
            elif state == "regressed":
                out["regressed"].append(f"{cfg_name}/{scen}")
    primary_improved = [k for k in out["improved"] if k.split("/")[1] in PRIMARY_SCENARIOS]
    if incorrect or out["regressed"]:
        out["overall"] = "failure"
    elif primary_improved:
        out["overall"] = "success"
    else:
        out["overall"] = "neutral"
    return out


def ratios(measurements: dict, tools: list[str]) -> dict:
    """speedup[tool][vs_base][scenario] = base_median / tool_median, geometric mean over corpora."""
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
            out[tool][f"vs_{base}"] = {scen: round(statistics.geometric_mean(v), 4) for scen, v in per_scen.items()}
            prim = [out[tool][f"vs_{base}"][s] for s in PRIMARY_SCENARIOS if s in out[tool][f"vs_{base}"]]
            if prim:
                out[tool][f"vs_{base}"]["primary_geomean"] = round(statistics.geometric_mean(prim), 4)
    return out


def print_report(result: dict) -> None:
    tools = result["tool_order"]
    print()
    print(f"{'corpus':<16}{'scenario':<9}" + "".join(f"{t:>14}" for t in tools) + "   (median wall s; FAIL = build failed)")
    for cfg_name, per_tool in result["measurements"].items():
        for scen in ALL_SCENARIOS:
            row = []
            for t in tools:
                s = per_tool.get(t, {}).get(scen, {})
                if s.get("median_wall_s") is not None and s.get("ok"):
                    row.append(f"{s['median_wall_s']:>14.2f}")
                elif s:
                    row.append(f"{'FAIL':>14}")
                else:
                    row.append(f"{'-':>14}")
            print(f"{cfg_name:<16}{scen:<9}" + "".join(row))
    print()
    for tool, r in result["speedup"].items():
        for base, per in r.items():
            print(f"speedup of {tool} {base.replace('_', ' ')}: " + ", ".join(f"{k}={v:.3f}x" for k, v in per.items()))
    for tool, c in result["correctness"].items():
        good = all(v["identical"] and v["builds_ok"] and v["break_ok"] for v in c.values())
        print(f"correctness of {tool}: " + ("identical artifacts to lake, all builds ok, break check ok" if good else
              "INCORRECT: " + "; ".join(f"{k}: identical={v['identical']} builds_ok={v['builds_ok']} break_ok={v['break_ok']}"
                                         for k, v in c.items() if not (v["identical"] and v["builds_ok"] and v["break_ok"]))))
    v = result["verdict"]
    for key, s in v.get("per_scenario", {}).items():
        print(f"verdict {key}: {s['state']} ({s['statistic']}: main {s['main_s']:.2f}s, candidate {s['candidate_s']:.2f}s, "
              f"speedup {s['speedup']:.3f}x, threshold {s['threshold'] * 100:.0f}%)")
    print(f"verdict: {v['overall']}" + (f" ({'; '.join(v['reasons'])})" if v.get("reasons") else ""))
    print(f"standard run: {'yes' if result['standard'] else 'no (' + ', '.join(result['nonstandard_reasons']) + ')'}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--configs", default=None, help="comma-separated config names or paths (default: bench/configs/medium-*.json)")
    ap.add_argument("--quick", action="store_true", help="use bench/configs/small-*.json (not a standard run)")
    ap.add_argument("--tools", default=DEFAULT_TOOLS, help="comma-separated: lake, main, candidate")
    ap.add_argument("--main-ref", default=None, help="git ref for `main` (default: merge-base of HEAD and origin/main)")
    ap.add_argument("--scenarios", default=",".join(ALL_SCENARIOS))
    ap.add_argument("--repeat", type=int, default=3, help="runs per incremental scenario taking >= 2 s (compared on medians)")
    ap.add_argument("--repeat-fast", type=int, default=10, help="runs per incremental scenario taking < 2 s (compared on minima)")
    ap.add_argument("--cpus", default=None, help="CPU list to pin to, e.g. 0-15 (default: bench/local.json or half)")
    ap.add_argument("--mem-gb", type=int, default=None, help="memory cap in GiB (default: bench/local.json or half)")
    ap.add_argument("--out", default=None, help="result JSON path (default: research/results/ for standard runs, else bench/work/results/)")
    ap.add_argument("--label", default="", help="free-form label stored in the result (e.g. the idea's slug)")
    ap.add_argument("--keep", action="store_true", help="keep per-tool corpus copies after the run")
    args = ap.parse_args()

    caps = default_caps()
    if args.cpus:
        caps["cpus"] = args.cpus
    if args.mem_gb is not None:
        caps["mem_gb"] = args.mem_gb
    requested = parse_cpus(caps["cpus"])
    cpus, affinity_ok = apply_cpu_cap(requested)
    if caps["mem_gb"] and total_mem_gb() and caps["mem_gb"] > total_mem_gb():
        sys.exit(f"--mem-gb {caps['mem_gb']} exceeds this machine's {total_mem_gb()} GB")
    use_scope, mem_mode = scope_support(caps["mem_gb"])
    if not affinity_ok:
        banner("CPU cap is NOT enforced on this platform; results will not be standard")
    if mem_mode.startswith("unenforced"):
        banner(f"memory cap is NOT enforced ({mem_mode}); the machine may be oversubscribed")
    extra_env = {"LEAN_NUM_THREADS": str(len(cpus))}
    cpu = cpu_model()
    machine = {"id": machine_id(cpu), "os": f"{platform.system()} {platform.release()}", "arch": platform.machine(),
               "cpu": cpu, "cpus_total": os.cpu_count(), "mem_total_gb": total_mem_gb(),
               "caps": {"cpus": ",".join(str(c) for c in cpus), "cpu_count": len(cpus), "cpu_affinity_enforced": affinity_ok,
                        "mem_gb": caps["mem_gb"], "mem_enforcement": mem_mode, "process_scopes": use_scope}}
    log(f"caps: cpus={machine['caps']['cpus']} ({len(cpus)}), affinity={'on' if affinity_ok else 'OFF'}, "
        f"mem={caps['mem_gb']}G via {mem_mode}, scopes={'on' if use_scope else 'off'}, LEAN_NUM_THREADS={len(cpus)}")

    default_cfgs = sorted(p.stem for p in CONFIGS.glob("medium-*.json"))
    if args.configs:
        cfg_names = [c for c in args.configs.split(",") if c]
    else:
        cfg_names = sorted(p.stem for p in CONFIGS.glob("small-*.json")) if args.quick else default_cfgs
    if not cfg_names:
        sys.exit("no configs selected")
    scenarios = [s for s in args.scenarios.split(",") if s]
    for s in scenarios:
        if s not in ALL_SCENARIOS:
            sys.exit(f"unknown scenario {s}")

    env, scrubbed = scrubbed_env()
    if scrubbed:
        log(f"scrubbed from the environment: {', '.join(scrubbed)}")
    tool_names = [t for t in args.tools.split(",") if t]
    tools, toolchain = resolve_tools(tool_names, env, args.main_ref)
    tool_order = [t for t in tool_names if "alias" not in tools[t]]
    dirty = bool(git("status", "--porcelain", "--untracked-files=no"))

    nonstandard = []
    if cfg_names != default_cfgs:
        nonstandard.append("configs")
    if tool_names != DEFAULT_TOOLS.split(","):
        nonstandard.append("tools")
    if scenarios != list(ALL_SCENARIOS):
        nonstandard.append("scenarios")
    if args.repeat < 3 or args.repeat_fast < 10:
        nonstandard.append("repeat<3 or repeat-fast<10")
    if dirty:
        nonstandard.append("dirty tree")
    if not affinity_ok:
        nonstandard.append("cpu cap unenforced")
    standard = not nonstandard

    measurements: dict = {}
    manifests_all: dict = {}
    corpora_meta: dict = {}
    orders: dict = {}
    t_start = time.time()
    for idx, name in enumerate(cfg_names):
        cfg_name, cfg_path = load_config(name)
        src = generate_corpus(cfg_name, cfg_path, env)
        corpus = json.loads((src / "corpus.json").read_text(encoding="utf-8"))
        targets = choose_targets(corpus)
        corpora_meta[cfg_name] = {"config": corpus["config"], "stats": corpus["stats"], "targets": targets,
                                  "config_sha256": sha256_file(cfg_path)}
        log(f"corpus {cfg_name}: {corpus['stats']['modules']} modules, {corpus['stats']['lines']} lines, "
            f"hub {targets['hub']} ({targets['hub_transitive_dependents']} transitive dependents), swarm edits {len(targets['swarm'])}")
        # rotate the tool order per corpus (and per repetition inside bench_corpus)
        order = tool_order[idx % len(tool_order):] + tool_order[: idx % len(tool_order)]
        orders[cfg_name] = order
        measurements[cfg_name], manifests_all[cfg_name] = bench_corpus(
            cfg_name, src, corpus, targets, tools, order, scenarios, args.repeat, args.repeat_fast,
            use_scope, caps["mem_gb"], extra_env)
        if not args.keep:
            for tool_name in order:
                shutil.rmtree(WORK / "corpora" / cfg_name / tool_name, ignore_errors=True)
                shutil.rmtree(WORK / "corpora" / cfg_name / f".bench-{tool_name}", ignore_errors=True)
    for alias, t in tools.items():
        if "alias" in t:
            for cfg_name in measurements:
                measurements[cfg_name][alias] = measurements[cfg_name][t["alias"]]
                manifests_all[cfg_name][alias] = manifests_all[cfg_name][t["alias"]]
    correctness = {}
    if "lake" in tool_names:
        for tool_name in tool_names:
            if tool_name != "lake":
                correctness[tool_name] = correctness_for(tool_name, manifests_all, measurements, corpora_meta)

    result = {
        "schema": SCHEMA_VERSION,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "label": args.label,
        "standard": standard,
        "nonstandard_reasons": nonstandard,
        "argv": sys.argv[1:],
        "duration_s": round(time.time() - t_start, 1),
        "machine": machine,
        "toolchain": {"pin": (ROOT / "lean-toolchain").read_text(encoding="utf-8").strip(), **toolchain},
        "harness": {"run_py_sha256": sha256_file(Path(__file__)), "generate_py_sha256": sha256_file(GEN),
                    "scrubbed_env": scrubbed, "build_timeout_s": BUILD_TIMEOUT_S},
        "git": {"commit": git("rev-parse", "HEAD"), "branch": git("rev-parse", "--abbrev-ref", "HEAD"), "dirty": dirty},
        "tools": {k: {kk: vv for kk, vv in v.items() if kk != "env"} for k, v in tools.items()},
        "tool_order": tool_names,
        "tool_order_per_corpus": orders,
        "repeat": args.repeat,
        "repeat_fast": args.repeat_fast,
        "noise_rule": {"floor": NOISE_FLOOR, "floor_fast": NOISE_FLOOR_FAST, "fast_seconds": FAST_SECONDS,
                       "cold_threshold": COLD_THRESHOLD},
        "scenarios": scenarios,
        "corpora": corpora_meta,
        "measurements": measurements,
        "speedup": ratios(measurements, tool_names),
        "correctness": correctness,
    }
    result["verdict"] = verdicts(measurements, correctness, tool_names)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fname = f"{stamp}-{machine['id']}-{result['git']['commit'][:7]}.json"
    out = Path(args.out) if args.out else (RESULTS / fname if standard else WORK / "results" / fname)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print_report(result)
    log(f"wrote {out}")
    if result["verdict"]["overall"] == "failure" or (
            "candidate" in correctness and not all(v["identical"] and v["builds_ok"] and v["break_ok"]
                                                   for v in correctness["candidate"].values())):
        sys.exit(2)


if __name__ == "__main__":
    main()
