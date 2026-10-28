"""How the eval layer plugs into what already exists (EVAL-SPEC "Integration"):

  stone   `run_stone_via_eval`: the bundle's stone tasks, imported as a suite, run through the eval runner with one default
          subject, and the cells returned in the stone's own result shape (so hone and apply read them unchanged)
  bench   `bench_matrix`: one eval run whose subjects are the surfaces
  hone    `dev_failures`: failures of the DEV split only, from the eval store (test and unseen cells are never read)
  apply   `outcome_gate`: before vs after on the outcome's gates over the TEST split (never unseen)
"""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import paths, stone
from ..errors import TogiError
from ..manifest import load_manifest
from . import analysis, integrity, items, runner, store
from . import subject as subjectlib
from .outcome import Outcome, load_outcome
from .subject import Subject, make_subject

DRIVER_PROVIDER = {"script": "script", "claude": "claude-cli"}


def import_stone_repo(repo: Path, dest: Path, task_ids: list[str] | None = None) -> Path:
    """Write `repo`'s stone tasks as a suite in `dest` (the repo is the bundle, wherever it is: a candidate copy too)."""
    man = load_manifest(repo)
    tasks = stone.load_tasks(man, task_ids)
    items.write_suite_toml(dest, f"{man.name}-stone", description=f"stone tasks of {man.name}", bundles={man.name: str(man.repo)},
                           default_bundles=[man.name])
    for t in tasks:
        items.write_item(dest, items.item_from_stone_task(t, bundle=man.name))
    return dest


def default_subject(driver: str, model: str | None, surface: str) -> Subject:
    d: dict[str, Any] = {"provider": DRIVER_PROVIDER[driver], "surface": surface, "role": "reference", "id": f"{driver}" + (f":{model}" if model else "")}
    if model and driver == "claude":
        d["model"] = model
    return make_subject(d)


def stone_result(man_name: str, repo: Path, driver: str, model: str | None, surface: str, runs: int, label: str | None,
                 cells: list[dict], tests: dict | None) -> dict:
    """Eval cells in the stone's result shape."""
    results = []
    for c in sorted(cells, key=lambda c: (c["item"], c["seed"])):
        r: dict[str, Any] = {"task": c["item"], "holdout": bool((c.get("meta") or {}).get("holdout", c.get("split") == "test")),
                             "run": c["seed"], "surface": surface}
        if c.get("success") is None:
            r.update(skipped=c.get("skipped") or "skipped", success=None)
        elif c.get("driver_error") and c.get("success") is False and not c.get("checks"):
            r.update(success=False, driver_error=c["driver_error"], turns=c.get("turns"))
        else:
            traj = [e for s in c.get("sessions", []) for e in (s.get("trajectory") or [])]
            attempts = [e for e in traj if e.get("event") in (None, "call", "unknown_command", "unknown_flag")]
            codes: dict[str, int] = {}
            for e in attempts:
                if e.get("error_code"):
                    codes[e["error_code"]] = codes.get(e["error_code"], 0) + 1
            r.update(success=bool(c["success"]), turns=c.get("turns"), cost_usd=c.get("cost_usd"), tokens=c.get("tokens"),
                     checks=[{"check": k["check"], "ok": k["ok"], "detail": k["detail"]} for k in c.get("checks", [])
                             if k.get("type") != "answer_valid" or not k.get("ok")],
                     forbidden=c.get("forbidden", []), over_budget=bool(c.get("over_budget")),
                     budget_turns=(c.get("sessions") or [{}])[0].get("budget_turns"),
                     journal=[{"command": e.get("cmdline"), "ok": e.get("ok"), "status": e.get("status"), "surface": e.get("surface")}
                              for e in attempts],
                     friction={"calls": len(attempts), "errors": sum(1 for e in attempts if e.get("ok") is False),
                               "top_error_codes": [{"code": k, "count": v} for k, v in sorted(codes.items(), key=lambda kv: -kv[1])[:5]]},
                     answer_tail=c.get("answer_tail", ""))
            if c.get("driver_error"):
                r["driver_error"] = c["driver_error"]
        r["duration_s"] = c.get("wall_s", 0)
        results.append(r)
    rid = f"st_{time.strftime('%Y%m%d-%H%M%S')}_ev"
    return {"id": rid, "bundle": man_name, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "repo": str(repo), "driver": driver, "model": model,
            "surface": surface, "runs": runs, "label": label, "tests": tests, "tasks": results, "engine": "eval",
            "summary": stone.summarize(tests, results)}


def run_stone_via_eval(repo: Path, *, driver: str, model: str | None, runs: int, task_ids: list[str] | None, surface: str,
                       with_tests: bool, task_timeout: float, max_cost: float | None, label: str | None, save: bool) -> dict:
    man = load_manifest(repo)
    tests = stone.run_tests(repo, man) if with_tests else None
    with tempfile.TemporaryDirectory(prefix="togi-stone-suite-") as tmp:
        suite_dir = import_stone_repo(repo, Path(tmp) / "suite", task_ids)
        sub = default_subject(driver, model, surface)
        spec = runner.RunSpec(suite_path=str(suite_dir), subjects=[sub], repeats=runs, item_timeout=task_timeout,
                              max_item_cost=max_cost, require_reference=False, note=f"stone {man.name} via eval")
        manifest = runner.run_matrix(spec)
        cells = store.read_cells(manifest["run_id"])
    res = stone_result(man.name, repo, driver, model, surface, runs, label, cells, tests)
    res["eval_run"] = manifest["run_id"]
    if save:
        d = paths.stone_dir(man.name)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{res['id']}.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        res["path"] = str(d / f"{res['id']}.json")
    return res


def bench_matrix(repo: Path, *, surfaces: list[str], runs: int, model: str | None, driver: str, task_ids: list[str] | None,
                 task_timeout: float, max_cost: float | None) -> dict[str, dict]:
    """One eval run, one subject per surface; returns {surface: stone-shaped result} (what bench's rows are made from)."""
    man = load_manifest(repo)
    with tempfile.TemporaryDirectory(prefix="togi-bench-suite-") as tmp:
        suite_dir = import_stone_repo(repo, Path(tmp) / "suite", task_ids)
        subs = [make_subject({**{"provider": DRIVER_PROVIDER[driver], "surface": s, "role": "reference", "id": s},
                              **({"model": model} if (model and driver == "claude") else {})}) for s in surfaces]
        spec = runner.RunSpec(suite_path=str(suite_dir), subjects=subs, repeats=runs, item_timeout=task_timeout, max_item_cost=max_cost,
                              require_reference=False, concurrency={"claude-cli": 1}, note=f"bench {man.name} via eval")
        manifest = runner.run_matrix(spec)
        cells = store.read_cells(manifest["run_id"])
    out = {}
    for s in surfaces:
        res = stone_result(man.name, repo, driver, model, s, runs, f"bench-{s}", [c for c in cells if c["subject"] == s], None)
        res["eval_run"] = manifest["run_id"]
        out[s] = res
    return out


# --------------------------------------------------------------------------- hone: dev only


def dev_failures(bundle: str | None = None, limit: int = 5) -> list[dict]:
    """Failures on the DEV split in the latest eval run (for this bundle if given). Only dev cells are read: the store keeps
    each split in its own file and this asks for dev alone; the test and unseen files are never opened."""
    integrity.guard_splits({"dev"}, "hone")
    for r in reversed(store.list_runs()):
        m = store.read_manifest(r["run"]) or {}
        cells = store.read_cells(r["run"], splits=("dev",))
        rows = []
        for c in cells:
            if c.get("success") is False and (bundle is None or bundle in (c.get("bundles") or [bundle])):
                from . import taxonomy

                cls = taxonomy.classify(c) or {}
                rows.append({"item": c["item"], "subject": c["subject"], "class": cls.get("class"), "evidence": cls.get("evidence"),
                             "checks": [{"check": k["check"], "detail": k["detail"]} for k in c.get("checks", []) if k.get("ok") is False][:4],
                             "driver_error": c.get("driver_error"), "answer_tail": (c.get("answer_tail") or "")[-300:]})
        if rows or cells:
            return rows[:limit]
    return []


def dev_friction(bundle: str | None = None) -> dict:
    """Where agents stumbled on the DEV split in the latest eval run that has dev cells (errors, error codes, repeated and
    wasted calls, unknown commands), so hone can learn from cells that passed the hard way. Dev cells only, like dev_failures."""
    integrity.guard_splits({"dev"}, "hone")
    for r in reversed(store.list_runs()):
        cells = [c for c in store.read_cells(r["run"], splits=("dev",)) if bundle is None or bundle in (c.get("bundles") or [bundle])]
        if not cells:
            continue
        codes: dict[str, int] = {}
        calls = errors = repeated = unknown = 0
        for c in cells:
            for s in c.get("sessions", []):
                m = s.get("metrics") or {}
                calls += m.get("calls", 0)
                errors += m.get("errors", 0)
                repeated += m.get("repeated", 0)
                unknown += m.get("unknown", 0)
                for e in s.get("trajectory") or []:
                    if e.get("error_code"):
                        codes[e["error_code"]] = codes.get(e["error_code"], 0) + 1
        return {"cells": len(cells), "calls": calls, "errors": errors, "repeated_calls": repeated, "unknown_commands": unknown,
                "top_error_codes": [{"code": k, "count": v} for k, v in sorted(codes.items(), key=lambda kv: -kv[1])[:5]]}
    return {}


# --------------------------------------------------------------------------- apply: the outcome gate over TEST


def outcome_gate(repo: Path, cand: Path, spec: dict, bundle: str) -> dict:
    """Run the outcome's suite for `repo` (before) and `cand` (after) over the TEST split and compare. `spec` =
    {"outcome": path, "suite": dir, "subjects": "...", "repeats": n}. The gate holds when the candidate is not worse: it
    passes the outcome's gates, or the baseline already failed them and the candidate's worst slice is no worse; and it
    makes no forbidden attempt beyond the gate's allowance. The unseen split is never run here."""
    oc = load_outcome(spec["outcome"])
    oc.splits = ["test"]                                  # apply gates on test, whatever the outcome counts elsewhere
    subs = subjectlib.load_subjects(spec["subjects"])
    out: dict[str, dict] = {}
    for label, root in (("before", repo), ("after", cand)):
        r = runner.run_matrix(runner.RunSpec(
            suite_path=spec["suite"], subjects=subs, outcome=oc, repeats=int(spec.get("repeats") or 1), include_unseen=False,
            splits=["test"], require_reference=False, bundle_overrides={bundle: str(root)}, note=f"apply gate ({label})"))
        data = analysis.load_run(r["run_id"])
        out[label] = {"run": r["run_id"], "verdicts": analysis.verdicts(data, oc)["subjects"]}
    problems = []
    for s in subs:
        b, a = out["before"]["verdicts"][s.id], out["after"]["verdicts"][s.id]
        if b["verdict"] == "PASS" and a["verdict"] == "FAIL":
            problems.append(f"{s.id}: passed the outcome before, fails it after ({'; '.join(a['reasons'][:2])})")
        elif a["verdict"] == "FAIL":
            bw, aw = b.get("worst_slice"), a.get("worst_slice")
            if bw and aw and aw["lo"] < bw["lo"] - 1e-9:
                problems.append(f"{s.id}: worst slice {aw['slice']} got worse ({bw['lo']:.2f} -> {aw['lo']:.2f})")
        if a["forbidden"] > oc.gates.max_forbidden:
            problems.append(f"{s.id}: {a['forbidden']} forbidden attempt(s)")
    return {"ok": not problems, "reason": "; ".join(problems) or "the outcome gates hold on test", "split": "test",
            "before": {"run": out["before"]["run"], "verdicts": {sid: v["verdict"] for sid, v in out["before"]["verdicts"].items()}},
            "after": {"run": out["after"]["run"], "verdicts": {sid: v["verdict"] for sid, v in out["after"]["verdicts"].items()}}}
