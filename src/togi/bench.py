"""`togi bench <bundle> --surfaces cli,run,mcp --runs N --model M`: the same agent tasks across surfaces.

Compares success, turns, tokens/cost and wall time so a bundle author can see whether the CLI, code mode
or MCP serves their agents best. Uses the stone's sandboxes, so nothing touches real state.

A comparison is only meaningful if every surface is driven the same way, so ONE driver is used for all of them:
`claude` whenever more than one surface is benchmarked, and `--driver script` is allowed for a single surface
only. Every task has a wall-clock limit (default 180 s; the process group is killed) and a cost cap passed to
the driver (default $0.50), so one stuck agent cannot eat the run.
"""
from __future__ import annotations

import json
import secrets
import time

from . import output, paths, stone
from .cli import Reply, parse_flags, _mode, _usage
from .errors import TogiError

SURFACES = ("cli", "run", "mcp")
DEFAULT_TASK_TIMEOUT = 180.0     # seconds of wall clock per task run
DEFAULT_TASK_COST = 0.50         # USD cap per task run (claude --max-budget-usd)


def choose_driver(tasks, surfaces: list[str], requested: str | None) -> str:
    """The one driver for every surface. Several surfaces => claude; script only for a single surface."""
    if requested == "script" and len(surfaces) > 1:
        raise TogiError("bad_arg", f"--driver script cannot compare {len(surfaces)} surfaces: a script replays "
                                   "each surface differently, so the rows would not be comparable",
                        next="togi bench <bundle> --surfaces cli --driver script   (one surface), or drop --driver "
                             "to use claude on all of them")
    if requested:
        return requested
    return "claude" if len(surfaces) > 1 else stone.default_driver(tasks, surfaces[0])


def surface_row(res: dict) -> dict:
    s = res["summary"]
    runs = s["nonholdout"]["runs"] + s["holdout"]["runs"]
    wins = s["nonholdout"]["successes"] + s["holdout"]["successes"]
    return {"runs": runs, "success": wins, "success_rate": round(wins / runs, 3) if runs else None,
            "median_turns": s["median_turns"], "median_tokens": s["median_tokens"],
            "total_cost_usd": s["total_cost_usd"], "median_cost_usd": s["median_cost_usd"],
            "wall_s": s["wall_s"], "mean_wall_s": round(s["wall_s"] / runs, 2) if runs else None,
            "forbidden": s["forbidden"], "skipped": s["skipped"], "driver_errors": s["driver_errors"],
            "stone_run": res["id"]}


def best(rows: dict[str, dict]) -> dict:
    """Which surface wins each metric (ties and missing values are left out)."""
    out = {}
    for key, better in (("success_rate", max), ("median_turns", min), ("median_tokens", min),
                        ("total_cost_usd", min), ("mean_wall_s", min)):
        vals = {s: r[key] for s, r in rows.items() if r.get(key) is not None}
        if len(vals) > 1 and len(set(vals.values())) > 1:
            target = better(vals.values())
            out[key] = sorted(s for s, v in vals.items() if v == target)
    return out


def text_table(rows: dict[str, dict]) -> str:
    cols = ("success_rate", "median_turns", "median_tokens", "total_cost_usd", "mean_wall_s")
    head = f"{'surface':<8}{'driver':>9}" + "".join(f"{c:>16}" for c in cols)
    lines = [head]
    for s, r in rows.items():
        lines.append(f"{s:<8}{r.get('driver', '-'):>9}" + "".join(
            f"{('-' if r.get(c) is None else r[c]):>16}" for c in cols))
    return "\n".join(lines)


def run_bench(repo, *, surfaces: list[str], runs: int, model: str | None, driver: str | None,
              task_ids: list[str] | None, task_timeout: float | None = None, max_cost: float | None = None,
              engine: str = "legacy") -> dict:
    from .manifest import load_manifest

    man = load_manifest(repo)
    tasks = stone.load_tasks(man, task_ids)
    d = choose_driver(tasks, surfaces, driver)          # ONE driver for every row
    task_timeout = task_timeout or DEFAULT_TASK_TIMEOUT
    max_cost = max_cost or DEFAULT_TASK_COST
    rows, results = {}, {}
    if engine == "eval":     # one eval run whose subjects are the surfaces
        from .evals import integration

        results = integration.bench_matrix(repo, surfaces=surfaces, runs=runs, model=model, driver=d, task_ids=task_ids,
                                           task_timeout=task_timeout, max_cost=max_cost)
        for surface in surfaces:
            rows[surface] = {**surface_row(results[surface]), "driver": d}
    else:
        for surface in surfaces:
            res = stone.run_stone(repo, driver=d, model=model, runs=runs, task_ids=task_ids, surface=surface,
                                  with_tests=False, task_timeout=task_timeout, max_cost=max_cost,
                                  label=f"bench-{surface}")
            results[surface] = res
            rows[surface] = {**surface_row(res), "driver": d}
    bid = f"bn_{time.strftime('%Y%m%d-%H%M%S')}_{secrets.token_hex(2)}"
    out = {"id": bid, "bundle": man.name, "model": model, "runs": runs, "driver": d, "task_timeout_s": task_timeout,
           "max_cost_per_task_usd": max_cost, "surfaces": rows, "best": best(rows), "text": text_table(rows)}
    d = paths.bench_dir(man.name)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{bid}.json").write_text(json.dumps({**out, "results": results}, indent=1), encoding="utf-8")
    out["path"] = str(d / f"{bid}.json")
    return out


def h_bench(argv: list[str]) -> Reply:
    from . import registry

    pos, f = parse_flags(argv, {"surfaces": "str", "runs": "int", "model": "str", "driver": "str", "tasks": "str",
                                "task-timeout": "float", "max-cost": "float", "engine": "str"}, "bench")
    if f["help"]:
        return _usage("bench")
    if len(pos) != 1:
        raise TogiError("bad_arg", "togi bench needs a bundle name", next="togi bundles")
    surfaces = [s for s in (f["surfaces"] or "cli,run,mcp").split(",") if s]
    bad = [s for s in surfaces if s not in SURFACES]
    if bad:
        raise TogiError("bad_arg", f"unknown surface {bad[0]!r}; use cli, run or mcp", next="togi bench --help")
    if f["driver"] and f["driver"] not in ("claude", "script"):
        raise TogiError("bad_arg", "--driver must be claude or script", next="togi bench --help")
    if f["driver"] == "script" and len(surfaces) > 1:
        choose_driver([], surfaces, "script")        # raises the explanation
    man = registry.manifest_for(pos[0])
    only = [t for t in (f["tasks"] or "").split(",") if t] or None
    if f["engine"] not in (None, "legacy", "eval"):
        raise TogiError("bad_arg", "--engine must be legacy or eval", next="togi bench --help")
    out = run_bench(man.repo, surfaces=surfaces, runs=max(1, f["runs"] or 1), model=f["model"], driver=f["driver"],
                    task_ids=only, task_timeout=f["task_timeout"], max_cost=f["max_cost"], engine=f["engine"] or "legacy")
    return Reply(env=output.ok_envelope(out), mode=_mode(f))
