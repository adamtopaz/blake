#!/usr/bin/env python3
"""Generate a synthetic, self-contained Lean 4 package for benchmarking build tools.

The generated package imports only Lean core (`Init`/`Std`), so it builds with
nothing but the toolchain in `lean-toolchain`. Its module import graph is a DAG
whose shape is controlled by `--shape`, and every module contains a mix of
definitions and theorems (`omega`, `decide`, `simp`, `deriving` handlers) whose
elaboration cost is controlled by `--work`. Generation is deterministic in the
config, so everyone who runs the same config gets a byte-identical corpus.

Usage:
  python3 bench/gen/generate.py --config bench/configs/medium-layered.json --out WORKDIR
  python3 bench/gen/generate.py --shape wide --modules 300 --seed 7 --out WORKDIR

Output layout (WORKDIR):
  lakefile.toml, lean-toolchain, Corpus/M00001.lean ..., corpus.json

`corpus.json` records the config, every module's imports, and graph statistics
that the benchmark harness uses to choose scenario targets (leaf modules,
widely imported modules, ...).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import sys
from collections import deque
from pathlib import Path

GENERATOR_VERSION = 4

SHAPES = ("layered", "deep", "wide")

DEFAULTS = {
    "shape": "layered",
    "modules": 300,
    "seed": 1,
    # mean number of imports per module (shape-dependent interpretation)
    "imports_mean": 3.0,
    # number of generated declarations per module (beyond the fixed `val` chain)
    "decls_per_module": 6,
    # 1 (light) .. 3 (heavy): scales the size of `decide` computations
    "work": 2,
    # shape-specific knobs
    "chains": 8,        # deep: number of parallel chains
    "cross_prob": 0.3,  # deep: probability of an extra cross-chain import
    "hubs": 16,         # wide: number of hub modules everyone imports from
}


def module_name(i: int) -> str:
    return f"Corpus.M{i:05d}"


def module_path(i: int) -> str:
    return f"Corpus/M{i:05d}.lean"


# ---------------------------------------------------------------------------
# DAG shapes
# ---------------------------------------------------------------------------

def poisson_like(rng: random.Random, mean: float, lo: int, hi: int) -> int:
    """Cheap integer draw around `mean`, clamped to [lo, hi]."""
    if mean <= 0:
        return lo
    k = 0
    p = math.exp(-mean)
    s = p
    u = rng.random()
    while u > s and k < hi:
        k += 1
        p *= mean / k
        s += p
    return max(lo, min(hi, k))


def shape_layered(cfg: dict, rng: random.Random) -> tuple[list[list[int]], list[int]]:
    """Random layered DAG: module i sits in a layer and imports modules from earlier
    layers, preferring recent ones. Bushy, moderate depth (~sqrt(N) layers)."""
    n = cfg["modules"]
    layers = max(2, int(math.sqrt(n)))
    layer_of = [min(layers - 1, i * layers // n) for i in range(n)]
    by_layer: list[list[int]] = [[] for _ in range(layers)]
    for i, l in enumerate(layer_of):
        by_layer[l].append(i)
    imports: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        l = layer_of[i]
        if l == 0:
            continue
        k = poisson_like(rng, cfg["imports_mean"], 1, 12)
        chosen: set[int] = set()
        for _ in range(k):
            # distance-biased layer choice: geometric with p = 0.5
            d = 1
            while d < l and rng.random() < 0.5:
                d += 1
            src_layer = max(0, l - d)
            chosen.add(rng.choice(by_layer[src_layer]))
        imports[i] = sorted(chosen)
    return imports, layer_of


def shape_deep(cfg: dict, rng: random.Random) -> tuple[list[list[int]], list[int]]:
    """A few long parallel chains with occasional cross links: very long critical
    path (N / chains), little width. Stresses scheduling of the critical path."""
    n = cfg["modules"]
    chains = max(1, min(cfg["chains"], n))
    imports: list[list[int]] = [[] for _ in range(n)]
    depth = [0] * n
    for i in range(n):
        if i >= chains:
            prev = i - chains
            imports[i].append(prev)
            depth[i] = depth[prev] + 1
            if rng.random() < cfg["cross_prob"] and i > chains:
                other = rng.randrange(0, i)
                if other != prev:
                    imports[i].append(other)
                    depth[i] = max(depth[i], depth[other] + 1)
        imports[i] = sorted(set(imports[i]))
    return imports, depth


def shape_wide(cfg: dict, rng: random.Random) -> tuple[list[list[int]], list[int]]:
    """A small layered core of hub modules that almost everything imports, then a
    wide, shallow fan of modules each importing a couple of hubs and rarely one
    other non-hub module. Editing a hub forces a near-total rebuild."""
    n = cfg["modules"]
    hubs = max(1, min(cfg["hubs"], n // 2))
    imports: list[list[int]] = [[] for _ in range(n)]
    layer = [0] * n
    for i in range(hubs):
        if i > 0:
            k = poisson_like(rng, 1.5, 1, 4)
            imports[i] = sorted({rng.randrange(0, i) for _ in range(k)})
            layer[i] = 1 + max(layer[j] for j in imports[i])
    for i in range(hubs, n):
        k = poisson_like(rng, cfg["imports_mean"], 1, 6)
        chosen = {rng.randrange(0, hubs) for _ in range(k)}
        if i > hubs and rng.random() < 0.15:
            chosen.add(rng.randrange(hubs, i))
        imports[i] = sorted(chosen)
        layer[i] = 1 + max(layer[j] for j in imports[i])
    return imports, layer


SHAPE_FNS = {"layered": shape_layered, "deep": shape_deep, "wide": shape_wide}


# ---------------------------------------------------------------------------
# Module contents
# ---------------------------------------------------------------------------

def decide_decl(idx: int, rng: random.Random, work: int) -> str:
    """A closed numeric fact proved by `decide`; cost grows with `work`."""
    base = {1: 12, 2: 24, 3: 40}[work]
    m = base + rng.randrange(0, base // 2)
    kind = rng.randrange(0, 4)
    if kind == 3:
        # Primality sieve by `decide`: the kernel evaluates the nested `all`, so
        # this is the main knob for making a module expensive to elaborate.
        pm = {1: 30, 2: 60, 3: 120}[work] + rng.randrange(0, 20)
        primes = [k for k in range(pm) if k > 1 and all(k % d for d in range(2, k))]
        return (f"theorem primes_below_{idx} :\n"
                f"    (List.range {pm}).filter (fun n => decide (n > 1) && (List.range n).all (fun d => d < 2 || n % d != 0))\n"
                f"      = {primes} := by\n"
                f"  decide\n")
    if kind == 0:
        total = m * (m - 1) // 2
        return (f"theorem sum_range_{idx} : (List.range {m}).foldl (· + ·) 0 = {total} := by\n"
                f"  decide\n")
    if kind == 1:
        sq = sum(k * k for k in range(m))
        return (f"theorem sum_squares_{idx} : ((List.range {m}).map (fun k => k * k)).foldl (· + ·) 0 = {sq} := by\n"
                f"  decide\n")
    evens = [k for k in range(m) if k % 2 == 0]
    return (f"theorem evens_{idx} : (List.range {m}).filter (fun k => k % 2 == 0) = {evens} := by\n"
            f"  decide\n")


def omega_decl(idx: int, rng: random.Random) -> str:
    a, b = rng.randrange(1, 6), rng.randrange(1, 6)
    return (f"theorem lin_{idx} (x y z : Nat) (h₁ : x + {a} * y ≤ z) (h₂ : z ≤ x + {a} * y + {b}) :\n"
            f"    z - x - {a} * y ≤ {b} ∧ x ≤ z := by\n"
            f"  omega\n")


def simp_decl(idx: int, rng: random.Random) -> str:
    kind = rng.randrange(0, 3)
    if kind == 0:
        return (f"theorem len_app_{idx} (xs ys : List Nat) : (xs ++ ys ++ []).length = ys.length + xs.length := by\n"
                f"  simp [Nat.add_comm]\n")
    if kind == 1:
        return (f"theorem rev_rev_{idx} (xs : List Nat) : xs.reverse.reverse.length = xs.length := by\n"
                f"  simp\n")
    return (f"theorem map_id_{idx} (xs : List Nat) : (xs.map fun x => x + 0) = xs := by\n"
            f"  simp\n")


def inductive_decl(idx: int, rng: random.Random) -> str:
    k = rng.randrange(2, 5)
    ctors = "\n".join(f"  | c{j}" + (f" (n : Nat)" if j == k - 1 else "") for j in range(k))
    arms = "\n".join(
        f"  | .c{j}" + (f" n => n + {j}" if j == k - 1 else f" => {j}") for j in range(k))
    return (f"inductive Shape{idx} where\n{ctors}\n  deriving DecidableEq, Repr, Inhabited, Hashable\n\n"
            f"def Shape{idx}.size : Shape{idx} → Nat\n{arms}\n\n"
            f"theorem Shape{idx}.size_last (n : Nat) : (Shape{idx}.c{k - 1} n).size = n + {k - 1} := rfl\n")


def structure_decl(idx: int, rng: random.Random) -> str:
    return (f"structure Point{idx} where\n  x : Nat\n  y : Nat\n  deriving DecidableEq, Repr\n\n"
            f"def Point{idx}.norm1 (p : Point{idx}) : Nat := p.x + p.y\n\n"
            f"theorem Point{idx}.norm1_ge_x (p : Point{idx}) : p.x ≤ p.norm1 := by\n"
            f"  unfold Point{idx}.norm1\n  omega\n")


def val_body(i: int, imports: list[int], consts: list[int]) -> str:
    """The right-hand side of module i's `val`, as Lean source over `n`."""
    if not imports:
        return f"n + {consts[i]}"
    terms = " + ".join(f"{module_name(j)}.val (n + {k})" for k, j in enumerate(imports))
    return f"{terms} + {consts[i]}"


def render_module(i: int, imports: list[int], all_imports: list[list[int]], consts: list[int], meta: str,
                  cfg: dict, rng: random.Random) -> str:
    name = module_name(i)
    lines = []
    for j in imports:
        lines.append(f"import {module_name(j)}")
    if imports:
        lines.append("")
    lines.append(f"/-! Module `{name}` ({meta}).\n"
                 f"Generated by `bench/gen/generate.py`; do not edit by hand. -/")
    lines.append("")
    lines.append(f"namespace {name}")
    lines.append("")
    # The `val` chain gives every module a real dependency on the declarations of
    # its imports, so a change upstream is semantically visible downstream.
    if imports:
        haves = "\n".join(f"  have h{k} := {module_name(j)}.val_ge (n + {k})" for k, j in enumerate(imports))
        lines.append("/-- Value chained through this module's imports. -/")
        lines.append(f"def val (n : Nat) : Nat :=\n  {val_body(i, imports, consts)}")
        lines.append("")
        lines.append(f"theorem val_ge (n : Nat) : n ≤ val n := by\n  unfold val\n{haves}\n  omega")
        lines.append("")
        # Restate each import's definition body by `rfl`, so this module's build
        # depends on the *bodies* of its imports, not only on their statements:
        # a build tool that skipped rebuilding dependents after a body change
        # would leave these proofs stale.
        for k, j in enumerate(imports):
            lines.append(f"theorem imp_unfold_{k} (n : Nat) : {module_name(j)}.val n = {val_body(j, all_imports[j], consts)} := rfl")
    else:
        lines.append("/-- Root value of the chain. -/")
        lines.append(f"def val (n : Nat) : Nat := {val_body(i, imports, consts)}")
        lines.append("")
        lines.append("theorem val_ge (n : Nat) : n ≤ val n := by\n  unfold val\n  omega")
    lines.append("")
    gens = [decide_decl, omega_decl, simp_decl, inductive_decl, structure_decl]
    weights = [3, 2, 2, 1, 1]
    for d in range(cfg["decls_per_module"]):
        gen = rng.choices(gens, weights=weights, k=1)[0]
        if gen is decide_decl:
            lines.append(gen(d, rng, cfg["work"]))
        else:
            lines.append(gen(d, rng))
    lines.append(f"end {name}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def load_config(args: argparse.Namespace) -> dict:
    cfg = dict(DEFAULTS)
    if args.config:
        with open(args.config, encoding="utf-8") as f:
            cfg.update(json.load(f))
        cfg.setdefault("name", Path(args.config).stem)
    for key in DEFAULTS:
        val = getattr(args, key, None)
        if val is not None:
            cfg[key] = val
    if args.name:
        cfg["name"] = args.name
    cfg.setdefault("name", f"{cfg['shape']}-{cfg['modules']}-s{cfg['seed']}")
    if cfg["shape"] not in SHAPES:
        sys.exit(f"unknown shape {cfg['shape']!r}; choose from {SHAPES}")
    if cfg["work"] not in (1, 2, 3):
        sys.exit("--work must be 1, 2, or 3")
    cfg["generator_version"] = GENERATOR_VERSION
    return cfg


def graph_stats(imports: list[list[int]]) -> dict:
    n = len(imports)
    dependents: list[list[int]] = [[] for _ in range(n)]
    for i, deps in enumerate(imports):
        for j in deps:
            dependents[j].append(i)
    # transitive dependents count (what an edit to i forces to rebuild)
    trans = [0] * n
    for i in range(n):
        seen = set()
        dq = deque(dependents[i])
        while dq:
            k = dq.popleft()
            if k in seen:
                continue
            seen.add(k)
            dq.extend(dependents[k])
        trans[i] = len(seen)
    # longest path (critical path in modules), modules are topologically ordered by index
    depth = [0] * n
    for i in range(n):
        if imports[i]:
            depth[i] = 1 + max(depth[j] for j in imports[i])
    return {
        "modules": n,
        "edges": sum(len(d) for d in imports),
        "critical_path": (max(depth) + 1) if n else 0,
        "leaves": sum(1 for d in dependents if not d),
        "roots": sum(1 for d in imports if not d),
        "max_transitive_dependents": max(trans) if n else 0,
        "transitive_dependents": trans,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="JSON config file (keys as in DEFAULTS)")
    ap.add_argument("--out", required=True, help="output directory (created; must be empty or absent)")
    ap.add_argument("--name", help="corpus name recorded in corpus.json")
    ap.add_argument("--toolchain", help="lean-toolchain file to copy (default: repo's)")
    ap.add_argument("--force", action="store_true", help="overwrite a non-empty output directory")
    for key, default in DEFAULTS.items():
        kind = type(default)
        ap.add_argument(f"--{key.replace('_', '-')}", dest=key, type=kind, default=None,
                        help=f"(default {default})")
    args = ap.parse_args()
    cfg = load_config(args)

    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        if not args.force:
            sys.exit(f"{out} is not empty (use --force to overwrite)")
        shutil.rmtree(out)
    (out / "Corpus").mkdir(parents=True, exist_ok=True)

    rng = random.Random(f"blake-corpus-v{GENERATOR_VERSION}-{cfg['seed']}-{cfg['shape']}-{cfg['modules']}")
    imports, layer = SHAPE_FNS[cfg["shape"]](cfg, rng)
    n = cfg["modules"]

    total_lines = 0
    sha = hashlib.sha256()
    modules = []
    consts = [random.Random(f"{cfg['seed']}-{i}-const").randrange(1, 9) for i in range(n)]
    for i in range(n):
        mrng = random.Random(f"{cfg['seed']}-{i}")
        meta = f"layer {layer[i]}, {len(imports[i])} imports"
        text = render_module(i, imports[i], imports, consts, meta, cfg, mrng)
        path = out / module_path(i)
        data = text.encode("utf-8")
        path.write_bytes(data)
        total_lines += text.count("\n")
        sha.update(data)
        modules.append({"name": module_name(i), "file": module_path(i),
                        "imports": [module_name(j) for j in imports[i]], "layer": layer[i]})

    (out / "lakefile.toml").write_text(encoding="utf-8", data=
        "# Generated benchmark corpus (bench/gen/generate.py); do not edit.\n"
        f"name = \"corpus\"\n"
        "defaultTargets = [\"Corpus\"]\n\n"
        "[[lean_lib]]\n"
        "name = \"Corpus\"\n"
        "globs = [\"Corpus.+\"]\n")

    toolchain_src = Path(args.toolchain) if args.toolchain else Path(__file__).resolve().parents[2] / "lean-toolchain"
    (out / "lean-toolchain").write_text(toolchain_src.read_text(encoding="utf-8"), encoding="utf-8")

    stats = graph_stats(imports)
    stats["lines"] = total_lines
    stats["content_sha256"] = sha.hexdigest()
    (out / "corpus.json").write_text(encoding="utf-8", data=json.dumps({
        "config": cfg,
        "stats": {k: v for k, v in stats.items() if k != "transitive_dependents"},
        "modules": [dict(m, transitive_dependents=stats["transitive_dependents"][i])
                    for i, m in enumerate(modules)],
    }, indent=1) + "\n")

    print(f"generated {cfg['name']}: {n} modules, {total_lines} lines, {stats['edges']} edges, "
          f"critical path {stats['critical_path']}, leaves {stats['leaves']}, "
          f"max transitive dependents {stats['max_transitive_dependents']} -> {out}")


if __name__ == "__main__":
    main()
