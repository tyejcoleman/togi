"""The matrix runner: subjects x items x repeats, parallel with per-provider concurrency limits, resumable, cost-capped.

    plan      the cells (item, subject, seed=base+repeat) in a fixed order that interleaves subjects, so a run stopped halfway
              still has paired data
    cache     a cell whose item hash, subject hash, seed and bundle digest all match a finished cell is reused, not re-run
    resume    `resume=<run id>` (or just running the same command again, which hits the cache) finishes what was not done
    caps      `max_cost` stops dispatching new cells once the run's spend reaches it (status `stopped: cost_cap`);
              `max_item_cost` is passed to the drivers per cell; `item_timeout` is the wall clock per session
    guard     a locked suite that changed does not run; at least one reference subject must be in the matrix; unseen items
              run only with include_unseen; the manifest records the splits touched and any deviation from the pre-registration
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..errors import TogiError
from ..manifest import load_manifest, walk_files
from . import integrity, store
from .canon import hash_obj
from .cell import cell_key, execute_cell
from .items import Item, Suite, load_suite, validate_suite
from .outcome import Outcome
from .subject import SANITY_ROLES, Subject

DEFAULT_CONCURRENCY = {"script": 4, "claude-cli": 1, "claude-step": 2, "ollama": 1, "openai-compatible": 4}
VOLATILE_TOP = {"stone", "tests", ".togi", ".git"}


@dataclass
class RunSpec:
    suite_path: str
    subjects: list[Subject]
    outcome: Outcome | None = None
    repeats: int = 1
    seed: int = 0
    include_unseen: bool = False
    splits: list[str] | None = None
    max_cost: float | None = None
    max_item_cost: float | None = 0.5
    item_timeout: float = 300.0
    concurrency: dict[str, int] = field(default_factory=dict)
    run_id: str | None = None
    resume: bool = False
    require_reference: bool = True
    judge: Callable[[str, str], str] | None = None
    item_ids: list[str] | None = None
    bundle_overrides: dict[str, str] = field(default_factory=dict)    # bundle name -> repo to use instead (apply: the candidate copy)
    stop_after: int | None = None            # (tests) stop after this many NEW cells, as if interrupted
    progress: Callable[[int, int, dict], None] | None = None
    note: str = ""


_DIGESTS: dict[str, str] = {}


def bundle_digest(repo: Path) -> str:
    """What the bundle IS for evaluation purposes: its code and skills. Its own stone tasks and tests are not part of it
    (an eval cell must not go stale because someone edited a test)."""
    key = str(repo)
    h = hashlib.sha256()
    for rel in walk_files(repo):
        if rel.split("/")[0] in VOLATILE_TOP:
            continue
        try:
            h.update(rel.encode() + b"\0" + (repo / rel).read_bytes() + b"\0")
        except OSError:
            continue
    d = h.hexdigest()
    _DIGESTS[key] = d
    return d


def env_digest(suite: Suite, item: Item) -> str:
    return hash_obj([[b, bundle_digest(suite.resolve_bundle(b))] for b in sorted(item.bundles)])


def plan_cells(items: list[Item], subjects: list[Subject], repeats: int, seed: int) -> list[tuple[Item, Subject, int]]:
    return [(it, s, seed + r) for it in items for r in range(repeats) for s in subjects]


def validate_run(spec: RunSpec, suite: Suite) -> None:
    if spec.repeats < 1:
        raise TogiError("bad_arg", "--repeats must be at least 1", next="togi eval run --repeats 3")
    problems = validate_suite(suite)
    if problems:
        raise TogiError("suite_invalid", f"{len(problems)} problem(s) in the suite; first: {problems[0]}",
                        next="fix the items; a faulty check lies, so nothing runs until they are fixed")
    if spec.require_reference and not any(s.role in SANITY_ROLES for s in spec.subjects):
        raise TogiError("no_reference_subject", "every run needs at least one reference subject (role = \"reference\" or "
                                                "\"target\"): without it a failing item cannot be told from a failing model",
                        next="mark one subject role = \"reference\" (e.g. `script` replaying the reference trajectory, or "
                             "a frontier model), or pass --no-reference-check")
    ids = [s.id for s in spec.subjects]
    if len(set(ids)) != len(ids):
        raise TogiError("subject_invalid", "subject ids must be unique", next="give each subject its own id")


def select_items(spec: RunSpec, suite: Suite) -> list[Item]:
    items = [i for i in suite.items if (spec.splits is None or i.split in spec.splits)]
    if spec.item_ids:
        missing = set(spec.item_ids) - {i.id for i in items}
        if missing:
            raise TogiError("unknown_task", f"no item(s) named {', '.join(sorted(missing))} among the loaded splits", next="togi eval items")
        items = [i for i in items if i.id in spec.item_ids]
    if not items:
        raise TogiError("empty_suite", "no items to run (check --splits and --include-unseen)", next="togi eval run --help")
    return items


def run_matrix(spec: RunSpec) -> dict:
    suite = load_suite(spec.suite_path, purpose="run", include_unseen=spec.include_unseen)
    suite.bundle_paths.update({k: str(v) for k, v in spec.bundle_overrides.items()})
    lock_state = integrity.enforce_lock(spec.suite_path)
    validate_run(spec, suite)
    items = select_items(spec, suite)
    plan = plan_cells(items, spec.subjects, spec.repeats, spec.seed)
    digests = {i.id: env_digest(suite, i) for i in items}
    splits_touched = sorted({i.split for i in items})
    deviations = integrity.preregistration_deviations(lock_state.get("record"), subjects=spec.subjects, repeats=spec.repeats,
                                                      outcome=spec.outcome, splits=splits_touched) if lock_state["locked"] else []
    plan_hash = hash_obj({"suite": lock_state.get("current") or "", "items": sorted(i.id for i in items),
                          "subjects": sorted(s.hash for s in spec.subjects), "repeats": spec.repeats, "seed": spec.seed})
    if spec.resume and not spec.run_id:
        raise TogiError("bad_arg", "resume needs the run id (--resume RUN)", next="togi eval runs")
    run_id = spec.run_id or store.new_run_id(plan_hash[:4])
    old = store.read_manifest(run_id)
    if spec.resume and old is None:
        raise TogiError("run_not_found", f"no run {run_id} to resume", next="togi eval runs")
    if old is not None and old.get("plan_hash") != plan_hash:
        raise TogiError("resume_mismatch", f"run {run_id} was a different plan (suite, items, subjects, repeats or seed "
                                           "changed), so it cannot be continued", next="start a new run; cached cells are reused anyway")
    ihash = {i.id: i.hash(suite.path) for i in items}
    keys = {cell_key(ihash[it.id], sub.hash, seed, digests[it.id]) for it, sub, seed in plan}
    prior_keys = {c["key"] for c in store.read_cells(run_id) if c.get("key") in keys} if old is not None else set()
    manifest: dict[str, Any] = {
        "run_id": run_id, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "status": "running", "plan_hash": plan_hash,
        "outcome": spec.outcome.to_dict() if spec.outcome else None, "outcome_hash": spec.outcome.hash if spec.outcome else None,
        "suite": {"name": suite.name, "path": str(suite.path), "hash": lock_state.get("current") or integrity.suite_hashes(
            integrity.load_all(suite.path))["suite_hash"], "items": len(items), "splits_loaded": suite.splits_loaded, "hidden": suite.hidden},
        "subjects": [s.to_dict() for s in spec.subjects], "repeats": spec.repeats, "seed": spec.seed,
        "include_unseen": spec.include_unseen, "splits_touched": splits_touched,
        "warnings": ([f"outcome references {', '.join(r['subject'] for r in spec.outcome.references if r['subject'] not in {s.id for s in spec.subjects})}"
                      " but no such subject is in this run"] if spec.outcome and any(
                          r["subject"] not in {s.id for s in spec.subjects} for r in spec.outcome.references) else []),
        "locked": lock_state["locked"], "preregistration": {"deviations": deviations, "matches": not deviations} if lock_state["locked"] else None,
        "settings": {"max_cost": spec.max_cost, "max_item_cost": spec.max_item_cost, "item_timeout": spec.item_timeout,
                     "concurrency": {**DEFAULT_CONCURRENCY, **spec.concurrency}, "reference_check": spec.require_reference},
        "cells_planned": len(plan), "cells_done": len(prior_keys), "cells_resumed": len(prior_keys), "cells_cached": 0, "cells_new": 0, "cost_usd": 0.0, "note": spec.note,
    }
    store.write_manifest(run_id, manifest)
    cache = store.CellCache()
    limits = {**DEFAULT_CONCURRENCY, **spec.concurrency}
    sems = {p: threading.Semaphore(max(1, n)) for p, n in limits.items()}
    lock = threading.Lock()
    state = {"cost": 0.0, "new": 0, "stop": False, "stopped_by": None}

    def finish_cell(cell: dict, cached: bool) -> None:
        cell = dict(cell)
        cell["cached"] = cached
        with lock:
            store.append_cell(run_id, cell)
            manifest["cells_done"] += 1
            manifest["cells_cached" if cached else "cells_new"] += 1
            if not cached:
                state["cost"] += cell.get("cost_usd") or 0.0
                manifest["cost_usd"] = round(state["cost"], 6)
            done = manifest["cells_done"]
        if spec.progress:
            spec.progress(done, len(plan), cell)

    def work(it: Item, sub: Subject, seed: int) -> None:
        key = cell_key(ihash[it.id], sub.hash, seed, digests[it.id])
        if key in prior_keys:
            return
        hit = cache.get(key, sub.hash, spec.item_timeout)
        if hit is not None:
            finish_cell(hit, True)
            return
        with sems.get(sub.provider, threading.Semaphore(1)):
            with lock:
                if state["stop"]:
                    return
                if spec.max_cost is not None and state["cost"] >= spec.max_cost:
                    state["stop"], state["stopped_by"] = True, "cost_cap"
                    return
                if spec.stop_after is not None and state["new"] >= spec.stop_after:
                    state["stop"], state["stopped_by"] = True, "interrupted"
                    return
                state["new"] += 1
            cell = execute_cell(suite, it, sub, seed, item_timeout=spec.item_timeout, max_item_cost=spec.max_item_cost, judge=spec.judge)
        cell["key"] = key
        cell["env_digest"] = digests[it.id][:8]
        cell["item_timeout"] = spec.item_timeout
        cache.put(cell)
        finish_cell(cell, False)

    workers = max(1, sum(limits.get(p, 1) for p in {s.provider for s in spec.subjects}))
    try:
        with cf.ThreadPoolExecutor(max_workers=min(workers, 16)) as ex:
            futs = [ex.submit(work, it, sub, seed) for it, sub, seed in plan]
            for f in futs:
                f.result()
    except KeyboardInterrupt:
        state["stopped_by"] = "interrupted"
    done = manifest["cells_done"]
    manifest["status"] = "complete" if done >= len(plan) else f"stopped: {state['stopped_by'] or 'incomplete'}"
    manifest["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    store.write_manifest(run_id, manifest)
    return manifest
