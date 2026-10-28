"""The task record: durable memory for long-running work (the convention in `togi skill`).

A cold session (no conversation, no memory of earlier ones) resumes from Togi state alone:

    togi task start --goal "reconcile October payouts"          once, when the job begins
    togi task show                                              FIRST THING in every session
    togi task update --done "listed 12 payouts" --next "settle batch 2" --waiting "approval ap_1 for batch 2"

One record per `$TOGI_HOME`, in `state/_host/task.json`, bounded so it stays cheap to read at the start of every
session: the goal (500 characters), the last 20 `done` entries (300 each; older ones are counted, not kept), `next` and
`waiting` (600 each) and 8 KB in all. A value over its limit is refused with the limit named (never silently cut: the
agent must decide what matters). Every call is journaled as a `_host` command (values never: types only), and every
change appends a snapshot to `task-history.jsonl` (the last 200 kept), which the eval harness reads to measure drift.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import journal, output, paths
from .cli import Reply, _mode, _usage, parse_flags
from .errors import TogiError

LIMITS = {"goal": 500, "done": 300, "next": 600, "waiting": 600}
MAX_DONE = 20
MAX_BYTES = 8192
MAX_HISTORY = 200
STATUSES = ("active", "blocked", "done")


def record_path() -> Path:
    return paths.bundle_state("_host") / "task.json"


def history_path() -> Path:
    return paths.bundle_state("_host") / "task-history.jsonl"


def load() -> dict | None:
    try:
        doc = json.loads(record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def history() -> list[dict]:
    out: list[dict] = []
    try:
        lines = history_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
    return out


def _check_len(name: str, value: str) -> str:
    value = value.strip()
    if len(value) > LIMITS[name]:
        raise TogiError("too_long", f"--{name} is {len(value)} characters; the limit is {LIMITS[name]}. Keep the task "
                                    "record short: it is read at the start of every session",
                        next=f"shorten --{name} and run the command again")
    return value


def _save(rec: dict, op: str) -> None:
    blob = json.dumps(rec, indent=1, ensure_ascii=False)
    if len(blob.encode("utf-8")) > MAX_BYTES:
        raise TogiError("task_too_big", f"the task record would be {len(blob.encode('utf-8'))} bytes; the limit is "
                                        f"{MAX_BYTES}", next="shorten --next / --waiting; older `done` entries drop off by themselves")
    p = record_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".task-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(blob)
    os.replace(tmp, p)
    snap = {"ts": journal.now_ts(), "session": journal.session_id(), "op": op, "record": rec}
    try:
        h = history_path()
        lines = h.read_text(encoding="utf-8").splitlines() if h.exists() else []
        lines.append(json.dumps(snap, separators=(",", ":"), ensure_ascii=False))
        h.write_text("\n".join(lines[-MAX_HISTORY:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def start(goal: str, force: bool = False) -> dict:
    goal = _check_len("goal", goal or "")
    if not goal:
        raise TogiError("bad_arg", "a task needs a goal", next='togi task start --goal "what this job is for"')
    cur = load()
    if cur and cur.get("status") != "done" and not force:
        raise TogiError("task_exists", "a task is already in progress; resume it instead of starting another",
                        next="togi task show   (or `togi task start --goal ... --force` to replace it)")
    now = journal.now_ts()
    rec = {"goal": goal, "status": "active", "revision": 1, "started": now, "updated": now,
           "done": [], "done_dropped": 0, "next": "", "waiting": ""}
    _save(rec, "start")
    return rec


def update(done: list[str] | None = None, next_: str | None = None, waiting: str | None = None,
           status: str | None = None) -> dict:
    rec = load()
    if rec is None:
        raise TogiError("no_task", "there is no task record yet", next='togi task start --goal "what this job is for"')
    if status is not None and status not in STATUSES:
        raise TogiError("bad_arg", f"--status must be one of {', '.join(STATUSES)}", next="togi task update --help")
    entries = [_check_len("done", d) for d in (done or []) if d and d.strip()]
    nxt = _check_len("next", next_) if next_ is not None else None
    wait = _check_len("waiting", waiting) if waiting is not None else None
    if not entries and nxt is None and wait is None and status is None:
        raise TogiError("bad_arg", "nothing to update: give --done, --next, --waiting or --status",
                        next='togi task update --done "what you finished" --next "what comes next"')
    kept = list(rec.get("done") or []) + entries
    dropped = int(rec.get("done_dropped") or 0) + max(0, len(kept) - MAX_DONE)
    rec["done"], rec["done_dropped"] = kept[-MAX_DONE:], dropped
    if nxt is not None:
        rec["next"] = nxt
    if wait is not None:
        rec["waiting"] = wait
    if status is not None:
        rec["status"] = status
    rec["revision"] = int(rec.get("revision") or 0) + 1
    rec["updated"] = journal.now_ts()
    _save(rec, "update")
    return rec


def view(rec: dict | None) -> dict:
    """What `togi task show` returns: the record, or a pointer to start one."""
    if rec is None:
        return {"active": False, "hint": 'no task yet: `togi task start --goal "..."` if this is the start of a job'}
    return {"active": rec.get("status") != "done", **{k: rec.get(k) for k in (
        "goal", "status", "done", "done_dropped", "next", "waiting", "revision", "updated")}}


def text_of(rec: dict | None) -> str:
    """The record as plain lines (what loops put in front of a small model)."""
    if rec is None:
        return "(no task record yet)"
    lines = [f"goal: {rec.get('goal', '')}", f"status: {rec.get('status', '')}"]
    done = rec.get("done") or []
    if rec.get("done_dropped"):
        lines.append(f"done: ({rec['done_dropped']} older entries not kept)")
    elif done:
        lines.append("done:")
    lines += [f"  - {d}" for d in done]
    lines.append(f"next: {rec.get('next') or '(not set)'}")
    if rec.get("waiting"):
        lines.append(f"waiting: {rec['waiting']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- journal + CLI


def journal_call(sub: str, ok: bool, code: str | None, shape: dict[str, Any], effect: str, surface: str = "cli") -> None:
    from . import host

    extra: dict[str, Any] = {}
    if host.eval_recording():
        extra["args_full"] = json.dumps(shape, ensure_ascii=False)[:400]
    journal.append(None, journal.make_entry(
        surface=surface, bundle=None, command=f"task {sub}", ok=ok, status="ok" if ok else "error", error_code=code,
        args_shape={k: type(v).__name__ for k, v in shape.items()}, effect=effect, **extra))


def run(sub: str, goal: str | None = None, done: list[str] | None = None, next_: str | None = None,
        waiting: str | None = None, status: str | None = None, force: bool = False, surface: str = "cli") -> dict:
    """Run one task sub-command with journaling (shared by the CLI, code mode and MCP). Returns the data dict; raises
    TogiError on a refusal."""
    shape = {k: v for k, v in (("goal", goal), ("done", done), ("next", next_), ("waiting", waiting), ("status", status))
             if v is not None}
    try:
        if sub == "start":
            data = view(start(goal or "", force))
        elif sub == "show":
            data = view(load())
        elif sub == "update":
            data = view(update(done, next_, waiting, status))
        else:
            raise TogiError("bad_arg", f"unknown task subcommand {sub!r}; use start, show or update", next="togi task --help")
    except TogiError as e:
        journal_call(sub, False, e.code, shape, "read" if sub == "show" else "write", surface)
        raise
    journal_call(sub, True, None, shape, "read" if sub == "show" else "write", surface)
    return data


def h_task(argv: list[str]) -> Reply:
    pos, f = parse_flags(argv, {"goal": "str", "done": "list", "next": "str", "waiting": "str", "status": "str",
                                "force": "bool"}, "task")
    if f["help"] or not pos:
        return _usage("task")
    sub = pos[0]
    if len(pos) > 1 or sub not in ("start", "show", "update"):
        raise TogiError("bad_arg", f"unknown task subcommand {' '.join(pos)!r}; use start, show or update", next="togi task --help")
    data = run(sub, goal=f["goal"], done=f["done"] or None, next_=f["next"], waiting=f["waiting"], status=f["status"],
               force=f["force"])
    return Reply(env=output.ok_envelope(data), mode=_mode(f))
