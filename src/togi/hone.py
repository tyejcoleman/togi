"""`togi hone <bundle>`: propose an improvement from journal friction and stone failures (SPEC section 6.1).

The agent (claude -p, no tools) sees the current text of the polish and reshape files only, plus the friction
summary and the latest non-holdout stone failures. It must answer with a rationale and a unified diff. The
diff is classified by tier before anything is written; a diff touching a locked file, a file in no tier, or
a path outside the bundle repo is rejected outright and that rejection is logged in the audit log.

Hone only writes a proposal. It cannot apply, approve, edit the stone, the manifest or Togi itself.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from . import apply as apply_mod
from . import audit, journal, output, patch, stone
from .cli import Reply, parse_flags, _mode, _usage
from .errors import TogiError
from .journal import now_ts
from .manifest import Manifest, walk_files
from .pathnorm import escape_text

MAX_FILE_BYTES = 40_000
MAX_TOTAL_BYTES = 120_000

HONE_SYSTEM = (
    "You improve a command-line tool that AI agents use. You may only change the files shown to you. "
    "Prefer small wording fixes (skills, error messages) that remove the friction you are shown. "
    "Answer in exactly this format and nothing else:\n"
    "RATIONALE:\n<two to five sentences: what friction you saw and why this change helps>\n"
    "DIFF:\n```diff\n<a unified diff with --- a/<path> and +++ b/<path> headers; paths relative to the repo root>\n```\n"
    "If no change is warranted, answer RATIONALE: followed by the reason and an empty DIFF section.")


def editable_files(man: Manifest) -> list[tuple[str, str, str]]:
    """(path, tier, text) for every polish/reshape file hone may see and change."""
    out, total = [], 0
    for rel in walk_files(man.repo):
        tier = man.classify(rel)
        if tier not in ("polish", "reshape"):
            continue
        f = man.repo / rel
        try:
            if f.stat().st_size > MAX_FILE_BYTES:
                continue
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if total + len(text) > MAX_TOTAL_BYTES:
            break
        total += len(text)
        out.append((rel, tier, text))
    return out


def stone_failures(bundle: str, limit: int = 5) -> list[dict]:
    res = stone.latest_result(bundle)
    rows = []
    for r in (res or {}).get("tasks", []):
        if r.get("success") is False and not r.get("holdout"):  # holdout tasks are never shown to hone
            rows.append({"task": r["task"], "checks": [c for c in r.get("checks", []) if not c["ok"]],
                         "driver_error": r.get("driver_error"), "answer_tail": r.get("answer_tail", "")[-300:]})
    return rows[:limit]


def stone_friction(bundle: str) -> dict:
    """Aggregate the agents' stumbles across the latest stone run's non-holdout tasks (holdout tasks are
    never shown to hone), so a run that passed only after errors and help lookups still teaches something."""
    res = stone.latest_result(bundle)
    agg: dict = {"task_runs": 0, "errors": 0, "help_lookups": {}, "error_codes": {}, "unknown_flags": [],
                 "retried_commands": {}}
    for r in (res or {}).get("tasks", []):
        if r.get("holdout") or not r.get("friction"):
            continue
        f = r["friction"]
        agg["task_runs"] += 1
        agg["errors"] += f.get("errors", 0)
        for h in f.get("help_lookups", []):
            agg["help_lookups"][h["command"]] = agg["help_lookups"].get(h["command"], 0) + h.get("count", 1)
        for c in f.get("top_error_codes", []):
            agg["error_codes"][c["code"]] = agg["error_codes"].get(c["code"], 0) + c.get("count", 1)
        for u in f.get("unknown_flags", []):
            if u not in agg["unknown_flags"]:
                agg["unknown_flags"].append(u)
        for rr in f.get("repeated_retries", []):
            agg["retried_commands"][rr["command"]] = agg["retried_commands"].get(rr["command"], 0) + rr.get("count", 1)
    agg["unknown_flags"] = agg["unknown_flags"][:10]
    return agg


def eval_dev_failures(bundle: str, limit: int = 5) -> list[dict]:
    """Failures on the eval layer's DEV split (the latest eval run). Hone reads dev only: the eval store keeps each split in
    its own file and only the dev file is opened, so test and unseen results never reach a prompt."""
    try:
        from .evals import integration

        return integration.dev_failures(bundle, limit)
    except Exception:       # no eval runs yet, or an unreadable store: hone works without
        return []


def build_prompt(man: Manifest) -> str:
    entries = journal.read(man.name)
    fr = journal.friction(entries)
    fr_small = {k: v for k, v in fr.items() if v and k != "commands"}
    fr_small["commands_with_errors"] = [c for c in fr["commands"] if c["errors"]][:8]
    parts = [f"Tool: {man.name} - {man.summary}", "", "FRICTION (from real usage):", json.dumps(fr_small, indent=1),
             "", "STONE FAILURES (latest run, non-holdout):", json.dumps(stone_failures(man.name), indent=1), "",
             "STONE FRICTION (latest run, non-holdout: where agents stumbled even when they passed):",
             json.dumps(stone_friction(man.name), indent=1), ""]
    dev = eval_dev_failures(man.name)
    if dev:
        parts += ["EVAL FAILURES (latest eval run, dev split only):", json.dumps(dev, indent=1), ""]
        try:
            from .evals import integration

            parts += ["EVAL FRICTION (dev split only):", json.dumps(integration.dev_friction(man.name), indent=1), ""]
        except Exception:
            pass
    parts += [
             "FILES YOU MAY CHANGE (tier in brackets; polish = wording, reshape = behaviour):"]
    for rel, tier, text in editable_files(man):
        parts.append(f"\n=== {rel} [{tier}] ===\n{text}")
    return "\n".join(parts)


def run_claude(prompt: str, model: str | None, timeout: float = 600) -> str:
    cmd = stone.claude_base() + ["--system-prompt", HONE_SYSTEM, "--tools="]
    if model:
        cmd += ["--model", model]
    import os
    import tempfile

    clean = {k: v for k, v in os.environ.items() if not k.startswith("TOGI_") and k not in stone.SECRET_ENV}
    clean["TOGI_AGENT"] = "hone"
    try:
        with tempfile.TemporaryDirectory(prefix="togi-hone-") as empty:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, env=clean,
                               cwd=empty)
    except FileNotFoundError:
        raise TogiError("claude_missing", "the `claude` CLI is not on PATH",
                        next="install Claude Code, or use: togi hone <bundle> --driver script --diff FILE")
    except subprocess.TimeoutExpired:
        raise TogiError("claude_timeout", f"claude -p exceeded {timeout:g}s", next="retry, or use a faster --model")
    try:
        res = stone.parse_claude_output(p.stdout)
    except (ValueError, StopIteration):
        raise TogiError("claude_failed", f"claude -p exit {p.returncode}: {(p.stderr or p.stdout).strip()[:300]}",
                        next="run `claude -p hi` to check the CLI works")
    if res.get("is_error"):
        raise TogiError("claude_failed", f"claude reported an error: {res['answer'][:200]}", next="retry")
    return res["answer"]


def parse_answer(text: str) -> tuple[str, str]:
    """(rationale, diff) from the model's answer. Tolerant of fences and missing headings."""
    m = re.search(r"```(?:diff|patch)?\s*\n(.*?)```", text, re.S)
    diff = m.group(1).strip("\n") if m else ""
    if not diff:
        i = re.search(r"^--- ", text, re.M)
        if i:
            diff = text[i.start():].strip("\n")
    rationale = text
    r = re.search(r"RATIONALE:\s*(.*?)(?:\nDIFF:|\Z)", text, re.S)
    if r:
        rationale = r.group(1)
    elif m:
        rationale = text[: m.start()]
    return rationale.strip(), (diff + "\n") if diff else ""


def make_proposal(man: Manifest, diff: str, rationale: str, *, model: str | None, driver: str) -> dict:
    """Classify, reject or store a proposal. Returns the result dict (also for rejections)."""
    pid = apply_mod.new_id()
    meta = {"id": pid, "bundle": man.name, "repo": str(man.repo), "created": now_ts(), "status": "proposed",
            "source": "hone", "driver": driver, "model": model, "diff_sha256": apply_mod.diff_sha(diff)}
    try:
        cl = apply_mod.classify_diff(diff, man)
    except patch.PatchError as e:
        raise TogiError("bad_diff", f"the proposed diff cannot be read: {e}",
                        next=f"togi hone {man.name}")
    files = sorted(cl.tiers)
    meta.update(tier=cl.tier, files=files)
    if cl.violation:
        entry = audit.append(man.repo, man.name, action="reject", proposal=pid, tier="locked", files=files,
                             diff_sha256=meta["diff_sha256"], stone_before=None, stone_after=None,
                             decided_by="policy", reason=f"{cl.violation}: {apply_mod.VIOLATION_TEXT[cl.violation]}")
        meta.update(status="rejected", tier="locked", audit_seq=entry["seq"])
        apply_mod.save_proposal(meta, diff, rationale)
        raise TogiError(cl.violation, f"proposal {pid} rejected: {apply_mod.VIOLATION_TEXT[cl.violation]} "
                                      f"({', '.join(files)}); logged as audit seq {entry['seq']}",
                        next=f"togi log {man.name}", status="refused")
    try:
        plan = apply_mod.plan_changes(man.repo, cl)  # it must apply to the current files
    except patch.PatchError as e:
        raise TogiError("patch_does_not_apply", f"the proposed diff does not apply: {e}", next=f"togi hone {man.name}")
    code_files = apply_mod.code_bearing_files(plan)
    if cl.tier == "polish" and code_files:   # polish is evaluated automatically, so it may carry no code at all
        why = f"{apply_mod.CODE_IN_POLISH_TEXT}: " + ", ".join(escape_text(f, 120) for f in code_files)
        entry = audit.append(man.repo, man.name, action="reject", proposal=pid, tier="polish", files=files,
                             diff_sha256=meta["diff_sha256"], stone_before=None, stone_after=None,
                             decided_by="policy", reason=f"code_in_polish: {why}")
        meta.update(status="rejected", audit_seq=entry["seq"])
        apply_mod.save_proposal(meta, diff, rationale)
        raise TogiError("code_in_polish", f"proposal {pid} rejected: {why}; logged as audit seq {entry['seq']}",
                        next=f"togi log {man.name}", status="refused")
    apply_mod.save_proposal(meta, diff, rationale)
    return {"proposal": pid, "tier": cl.tier, "files": files, "status": "proposed",
            "next": f"togi review {pid}  # then: togi apply {pid}"}


def h_hone(argv: list[str]) -> Reply:
    from . import registry

    pos, f = parse_flags(argv, {"model": "str", "driver": "str", "diff": "str", "rationale": "str"}, "hone")
    if f["help"]:
        return _usage("hone")
    if len(pos) != 1:
        raise TogiError("bad_arg", "togi hone needs a bundle name", next="togi bundles")
    man = registry.manifest_for(pos[0])
    driver = f["driver"] or ("script" if f["diff"] else "claude")
    if driver == "script":
        if not f["diff"]:
            raise TogiError("bad_arg", "the script driver needs --diff FILE (or - for stdin)",
                            next=f"togi hone {man.name} --driver script --diff change.patch")
        diff = sys.stdin.read() if f["diff"] == "-" else Path(f["diff"]).expanduser().read_text(encoding="utf-8")
        rationale = f["rationale"] or "Supplied by --diff (script driver)."
    elif driver == "claude":
        answer = run_claude(build_prompt(man), f["model"])
        rationale, diff = parse_answer(answer)
    else:
        raise TogiError("bad_arg", "--driver must be claude or script", next="togi hone --help")
    if not diff.strip():
        return Reply(env=output.ok_envelope({"proposal": None, "reason": rationale or "no change proposed"}),
                     mode=_mode(f))
    return Reply(env=output.ok_envelope(make_proposal(man, diff, rationale, model=f["model"], driver=driver)),
                 mode=_mode(f))
