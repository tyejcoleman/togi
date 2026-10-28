"""Trajectories: what an agent DID, read from the journal slice of its sandbox (not from what it said it did).

An entry: {ts, session, event, surface, bundle, command, cmdline, ok, status, error_code, effect, args, args_hash,
out, approval}. `args` and `out` (a preview of the call and of the reply) are only there because the eval sandbox asks
the host to record them (host.eval_recording); the journal of a real home never holds them.

Metrics defined here are the long-horizon ones (EVAL-SPEC + the long-running addendum): repeated/wasted actions,
drift against the task record, stops at an approval.
"""
from __future__ import annotations

import re
from typing import Any

from .. import stone

STATE_CHANGING = ("write", "money", "admin")
ATTEMPT_EVENTS = ("call", "unknown_command", "unknown_flag", "answer", "host_attempt")


def cmdline(e: dict) -> str:
    if e.get("event") == "answer":
        return "togi answer"
    return stone.cmdline_of(e)


def entry_from_journal(e: dict) -> dict:
    return {"ts": e.get("ts"), "session": e.get("session"), "event": e.get("event", "call"), "surface": e.get("surface"),
            "bundle": e.get("bundle"), "command": e.get("command"), "cmdline": cmdline(e), "ok": e.get("ok"),
            "status": e.get("status"), "error_code": e.get("error_code"), "effect": e.get("effect"),
            "args": e.get("args_full"), "args_hash": e.get("args_hash"), "args_shape": e.get("args_shape"),
            "out": e.get("out_preview"), "approval": e.get("approval"), "dry_run": bool(e.get("dry_run")),
            "duration_ms": e.get("duration_ms")}


def from_journal(entries: list[dict], sessions: set[str] | None = None) -> list[dict]:
    """The agent's attempts, oldest first. `sessions` limits to those session ids (the agent's, not the harness's)."""
    out = [entry_from_journal(e) for e in entries
           if e.get("event", "call") in ATTEMPT_EVENTS and (sessions is None or e.get("session") in sessions)]
    out.sort(key=lambda e: e.get("ts") or "")
    return out


def compact(traj: list[dict], limit: int = 120) -> list[dict]:
    """What a stored cell keeps: enough to audit and re-analyse (previews are cut)."""
    rows = []
    for e in traj[:limit]:
        r = {k: e.get(k) for k in ("ts", "session", "event", "surface", "cmdline", "ok", "status", "error_code", "effect",
                                   "args", "args_hash", "approval") if e.get(k) is not None}
        if e.get("out"):
            r["out"] = e["out"][:300]
        rows.append(r)
    return rows


def is_state_change(e: dict) -> bool:
    return e.get("event") == "call" and e.get("effect") in STATE_CHANGING and e.get("ok") is True and not e.get("dry_run")


def _key(e: dict) -> tuple:
    return (e.get("cmdline"), e.get("args") or e.get("args_hash") or repr(e.get("args_shape")))


def repeated_actions(traj: list[dict]) -> list[int]:
    """Indexes of calls that repeat an earlier identical call (same command and arguments) with NO state change in between:
    the agent learned nothing and changed nothing. Needs the arguments (recorded in the eval sandbox); a call whose
    arguments are unknown is compared on its argument shapes, which can only under-count."""
    last_seen: dict[tuple, int] = {}
    changes = [i for i, e in enumerate(traj) if is_state_change(e)]
    out = []
    for i, e in enumerate(traj):
        if e.get("event") not in ("call", "unknown_command", "unknown_flag"):
            continue
        k = _key(e)
        j = last_seen.get(k)
        if j is not None and not any(j < c < i for c in changes):
            out.append(i)
        last_seen[k] = i
    return out


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) >= 3}


def drift(traj: list[dict], task_history: list[dict]) -> dict:
    """Actions inconsistent with the task record. For each state-changing bundle call made while a task record exists (the
    latest snapshot at or before the call), the call is *anchored* if any word of its command (`delete`, `tip`, `send`) appears in the
    record's goal / done / next / waiting text, and *drifting* if nothing about it does. A deliberately simple, repeatable
    rule: it flags an agent that keeps a record and then acts off it (or never keeps one while changing state)."""
    snaps = sorted(task_history, key=lambda s: s.get("ts") or "")
    considered = drifting = 0
    samples = []
    for e in traj:
        if not (is_state_change(e) and e.get("bundle")):
            continue
        snap = None
        for s in snaps:
            if (s.get("ts") or "") <= (e.get("ts") or ""):
                snap = s
            else:
                break
        if snap is None:
            continue
        rec = snap.get("record") or {}
        text = " ".join([str(rec.get("goal", "")), " ".join(rec.get("done") or []), str(rec.get("next", "")), str(rec.get("waiting", ""))])
        toks = _tokens(text)
        mine = _tokens(e.get("command") or "") or _tokens(e.get("bundle") or "")      # the verb, not the bundle: every note task mentions "notes"
        considered += 1
        if not (mine & toks):
            drifting += 1
            if len(samples) < 5:
                samples.append(e["cmdline"])
    return {"considered": considered, "drifting": drifting, "rate": (drifting / considered) if considered else None,
            "samples": samples}


def metrics(traj: list[dict]) -> dict:
    attempts = [e for e in traj if e.get("event") in ("call", "unknown_command", "unknown_flag")]
    bundle_calls = [e for e in attempts if e.get("bundle") and e.get("event") == "call"]
    rep = repeated_actions(traj)
    return {"calls": len(attempts), "bundle_calls": len(bundle_calls),
            "errors": sum(1 for e in attempts if e.get("ok") is False),
            "state_changes": sum(1 for e in traj if is_state_change(e)),
            "needs_approval": sum(1 for e in attempts if e.get("status") == "needs_approval"),
            "repeated": len(rep), "code_mode_calls": sum(1 for e in attempts if e.get("surface") == "run"),
            "unknown": sum(1 for e in attempts if e.get("event") in ("unknown_command", "unknown_flag")),
            "answers": sum(1 for e in traj if e.get("event") == "answer"),
            "ended_at_approval": bool(bundle_calls) and bundle_calls[-1].get("status") == "needs_approval"}


def command_key(cmd: str) -> str:
    """`togi notes add --text 'x'` -> `notes add`: the command words before the first flag (at most three)."""
    words = []
    for w in cmd.strip().removeprefix("togi ").split():
        if w.startswith("-") or w[:1] in "'\"":
            break
        words.append(w)
        if len(words) == 3:
            break
    return " ".join(words)


def against_reference(traj: list[dict], reference: list[str]) -> dict:
    """How a trajectory compares with an item's reference trajectory: `coverage` = the share of the reference's distinct commands
    the agent also used, `efficiency` = reference calls / the agent's bundle calls (1.0 = as lean as the reference; below 1 =
    detours; above 1 = it found a shortcut), `extra_calls` = calls beyond the reference's count."""
    ref_keys = [command_key(c) for c in reference if command_key(c)]
    calls = [e for e in traj if e.get("event") == "call" and e.get("bundle")]
    used = {e["cmdline"].removeprefix("togi ") for e in calls}
    distinct = sorted(set(ref_keys))
    covered = [k for k in distinct if any(u == k or u.startswith(k + " ") for u in used)]
    n_ref, n = len(ref_keys), len(calls)
    return {"reference_calls": n_ref, "agent_calls": n, "coverage": (len(covered) / len(distinct)) if distinct else None,
            "efficiency": (n_ref / n) if n else None, "extra_calls": max(0, n - n_ref)}
