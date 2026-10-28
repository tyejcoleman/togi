"""The eval store under `$TOGI_HOME/eval/`:

    runs/<run_id>/manifest.json     what was run (outcome, suite hash, subjects, repeats, splits touched, pre-registration), status
    runs/<run_id>/cells.<split>.jsonl  one line per finished cell, one file per split, appended the moment it finishes (so an interrupted run keeps its work)
    cells/<subject_hash>.jsonl      the result cache: every valid cell ever produced for that subject, keyed by
                                    item hash : subject hash : seed : bundle digest
    runs/<run_id>/report.json|html  written by `togi eval report`

A cell is reused when ALL of item hash, subject hash, seed and the bundle digest match, so editing an item, a subject's
harness config or the bundle's code makes it run again, and nothing else does. A cell with a driver error (the harness or
the provider failed, not the subject) is never cached or reused.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from .. import paths
from ..errors import TogiError

_LOCK = threading.RLock()


def eval_dir() -> Path:
    return paths.togi_home() / "eval"


def runs_dir() -> Path:
    return eval_dir() / "runs"


def run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
        raise TogiError("bad_arg", f"{run_id!r} is not a run id", next="togi eval runs")
    return runs_dir() / run_id


def new_run_id(tag: str = "") -> str:
    """A fresh id every call (time + random): two runs of the same plan in one second are still two runs."""
    return f"ev_{time.strftime('%Y%m%d-%H%M%S')}_{secrets.token_hex(3)}"


def _append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def _read_lines(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict):
                    out.append(e)
    except OSError:
        pass
    return out


def write_manifest(run_id: str, manifest: dict) -> None:
    d = run_dir(run_id)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".manifest.tmp"
    tmp.write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, d / "manifest.json")


def read_manifest(run_id: str) -> dict | None:
    try:
        return json.loads((run_dir(run_id) / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


SPLIT_FILES = ("dev", "test", "unseen")


def append_cell(run_id: str, cell: dict) -> None:
    """Each split has its own file (`cells.<split>.jsonl`), so a reader that may only see dev never opens the others."""
    split = cell.get("split") if cell.get("split") in SPLIT_FILES else "dev"
    _append(run_dir(run_id) / f"cells.{split}.jsonl", cell)


def read_cells(run_id: str, splits: tuple[str, ...] | None = None) -> list[dict]:
    """The run's finished cells, from the files of the requested splits only (all of them by default)."""
    out: list[dict] = []
    for sp in (splits or SPLIT_FILES):
        out += _read_lines(run_dir(run_id) / f"cells.{sp}.jsonl")
    return out


class CellCache:
    """The cross-run cache of finished cells (one file per subject hash)."""

    def __init__(self) -> None:
        self._shards: dict[str, dict[str, dict]] = {}

    def _shard(self, subject_hash: str) -> dict[str, dict]:
        with _LOCK:
            if subject_hash not in self._shards:
                self._shards[subject_hash] = {c["key"]: c for c in _read_lines(eval_dir() / "cells" / f"{subject_hash[:32]}.jsonl")
                                              if "key" in c}
            return self._shards[subject_hash]

    def get(self, key: str, subject_hash: str, item_timeout: float | None = None) -> dict | None:
        c = self._shard(subject_hash).get(key)
        if c is None:
            return None
        if c.get("timed_out") and item_timeout is not None and c.get("item_timeout") != item_timeout:
            return None        # it ran out of time under a different limit: that proves nothing under this one
        return c

    def put(self, cell: dict) -> None:
        if not cell.get("valid", True) or cell.get("key") is None:
            return
        with _LOCK:
            self._shard(cell["subject_hash"])[cell["key"]] = cell
        _append(eval_dir() / "cells" / f"{cell['subject_hash'][:32]}.jsonl", cell)


def list_runs() -> list[dict]:
    out = []
    d = runs_dir()
    if d.is_dir():
        for p in sorted(d.iterdir()):
            m = read_manifest(p.name) if p.is_dir() else None
            if m:
                out.append({"run": m.get("run_id", p.name), "created": m.get("created"), "status": m.get("status"),
                            "suite": (m.get("suite") or {}).get("name"), "outcome": (m.get("outcome") or {}).get("name"),
                            "subjects": [s["id"] for s in m.get("subjects", [])], "cells": m.get("cells_done"),
                            "splits": m.get("splits_touched")})
    return out


def resolve_run(spec: str) -> str:
    """A run id, a unique prefix of one, or `latest`."""
    runs = [r["run"] for r in list_runs()]
    if spec == "latest":
        if not runs:
            raise TogiError("run_not_found", "there are no eval runs yet", next="togi eval run --help")
        return runs[-1]
    if spec in runs:
        return spec
    hits = [r for r in runs if r.startswith(spec)]
    if len(hits) == 1:
        return hits[0]
    raise TogiError("run_not_found" if not hits else "run_ambiguous",
                    f"no eval run matches {spec!r}" if not hits else f"{spec!r} matches {len(hits)} runs", next="togi eval runs")
