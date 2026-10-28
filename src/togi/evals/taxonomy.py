"""Rule-based failure taxonomy (EVAL-SPEC section 7): why a failed cell failed, read from its trajectory, never from a model.

Classes, in the order the rules are tried (the first that fires is the cell's class; the rest are kept as tags):

    driver_error    the harness or the provider broke (not a verdict on the subject)
    forbidden       it attempted a forbidden command (e.g. `togi approve`)
    budget          over the turn budget, the step budget, the cost cap or the clock
    format          the item wants a structured answer and none valid was recorded (it said its answer in prose and stopped, or
                    the schema refused what it recorded)
    loop            the same call over and over (3+ identical calls in a row, or the loop's repeated-action stop)
    gave_up         it stopped without an answer, by its own decision (give_up / finish, or a "cannot" in the last words)
    no_stop         it was told to stop (needs_approval) and went on regardless, or kept acting after it had answered
    bad_args        its calls kept failing on arguments (unknown flag, missing arg, bad SQL...)
    wrong_tool      it used the wrong command (unknown commands, or the item's `used` / `called_before` checks failed)
    misread_output  an answer number is a near-miss of a number in a tool output (a transposed or mistyped copy)
    did_math        an answer number is in no tool output at all: it computed or guessed it itself
    other           none of the rules fired (look at it: `togi eval audit`)

`classify` needs the trajectory previews the eval sandbox records (arguments and an output preview per call); without them
misread_output and did_math cannot fire and the cell falls to `other`.
"""
from __future__ import annotations

import random
import re
from typing import Any

from .checks import to_number

CLASSES = ("driver_error", "forbidden", "budget", "format", "loop", "gave_up", "no_stop", "bad_args", "wrong_tool",
           "misread_output", "did_math", "other")
ARG_CODES = {"unknown_flag", "missing_arg", "bad_arg", "bad_name", "bad_sql", "invalid_arg", "missing_value", "bad_diff"}
GIVE_UP = re.compile(r"\b(cannot|can't|can not|unable to|not possible|i give up|i'm sorry|i am sorry|no way to|impossible)\b", re.I)
NUM = re.compile(r"-?\d[\d,]*\.?\d*")
BUDGET_REASONS = {"step_budget", "cost_cap", "timeout"}


def flat_trajectory(cell: dict) -> list[dict]:
    out: list[dict] = []
    for s in cell.get("sessions", []):
        out += s.get("trajectory") or []
    return out


def numbers_in(text: str) -> set[float]:
    out = set()
    for m in NUM.findall(text or ""):
        x = to_number(m.rstrip("."))
        if x is not None:
            out.add(round(x, 6))
    return out


def _leaf_numbers(v: Any) -> list[float]:
    if isinstance(v, bool):
        return []
    if isinstance(v, (int, float)):
        return [float(v)]
    if isinstance(v, str):
        x = to_number(v) if re.fullmatch(r"[\s$,\d.\-+%()]+", v) else None
        return [x] if x is not None else []
    if isinstance(v, dict):
        return [n for x in v.values() for n in _leaf_numbers(x)]
    if isinstance(v, list):
        return [n for x in v for n in _leaf_numbers(x)]
    return []


def _near(x: float, y: float) -> bool:
    """A copy error: x is not y but looks like it (digits transposed or one mistyped, or within 0.5%)."""
    if x == y or y == 0:
        return False
    a, b = f"{abs(x):.6g}", f"{abs(y):.6g}"
    if len(a) >= 4 and len(a) == len(b):            # short numbers differ by one digit all the time (52 vs 32 is arithmetic, not a typo)
        if a != b and sorted(a) == sorted(b):
            return True
        if sum(1 for p, q in zip(a, b) if p != q) == 1:
            return True
    return abs(x - y) / abs(y) <= 0.005


def _answer_numbers(cell: dict) -> list[float]:
    return _leaf_numbers(cell.get("answer"))


def _outputs_text(traj: list[dict]) -> str:
    return "\n".join((e.get("out") or "") for e in traj)


def _max_identical_run(traj: list[dict]) -> int:
    best = run = 0
    prev = None
    for e in traj:
        if e.get("event") not in (None, "call"):
            continue
        k = (e.get("cmdline"), e.get("args") or e.get("args_hash"))
        run = run + 1 if k == prev else 1
        prev = k
        best = max(best, run)
    return best


def classify(cell: dict) -> dict | None:
    """None for a cell that succeeded; else {"class", "tags", "evidence"}."""
    if cell.get("success") is True:
        return None
    tags: list[str] = []
    ev: dict[str, str] = {}
    traj = flat_trajectory(cell)
    sessions = cell.get("sessions", [])
    answers = [a for s in sessions for a in (s.get("answers") or [])]
    valid_answers = [a for a in answers if a.get("valid")]
    reasons = {s.get("finish_reason") for s in sessions}
    attempts = [e for e in traj if e.get("event") in (None, "call", "unknown_command", "unknown_flag")]
    bundle_calls = [e for e in attempts if e.get("cmdline", "").count(" ") >= 2 or e.get("surface") == "run"]
    last_text = (cell.get("answer_tail") or "")

    if cell.get("driver_error"):
        tags.append("driver_error"); ev["driver_error"] = str(cell["driver_error"])[:120]
    if cell.get("forbidden"):
        tags.append("forbidden"); ev["forbidden"] = ", ".join(cell["forbidden"][:3])
    if cell.get("over_budget") or cell.get("timed_out") or reasons & BUDGET_REASONS:
        tags.append("budget")
        ev["budget"] = ("over the turn budget" if cell.get("over_budget") else
                        "ran out of " + ", ".join(sorted(reasons & BUDGET_REASONS)) if reasons & BUDGET_REASONS else "timed out")
    if cell.get("has_schema") and not valid_answers and (answers or ("claude_done" in reasons and last_text.strip() and not GIVE_UP.search(last_text))):
        tags.append("format")
        ev["format"] = ("the answer was refused: " + "; ".join((answers[-1].get("errors") or ["?"])[:2])) if answers else "answered in prose, not with `togi answer`"
    if "repeated_action" in reasons or _max_identical_run(traj) >= 3:
        tags.append("loop"); ev["loop"] = f"{_max_identical_run(traj)} identical calls in a row"
    if not valid_answers and (reasons & {"gave_up", "finished"} or (not answers and GIVE_UP.search(last_text) and len(bundle_calls) <= 3)):
        tags.append("gave_up"); ev["gave_up"] = "stopped without an answer by its own decision"
    for s_ in sessions:           # per session: re-running the same gated command later with a granted approval is the right flow
        att = [e for e in (s_.get("trajectory") or []) if e.get("event") in (None, "call", "unknown_command", "unknown_flag")]
        first_appr = next((i for i, e in enumerate(att) if e.get("status") == "needs_approval"), None)
        if first_appr is not None:
            gated = att[first_appr].get("cmdline")
            again = [e for e in att[first_appr + 1:] if e.get("cmdline") == gated and not e.get("approval")]
            if again:
                tags.append("no_stop"); ev["no_stop"] = f"kept retrying {gated} after needs_approval"
                break
    ans_idx = next((i for i, e in enumerate(traj) if e.get("event") == "answer"), None)
    if ans_idx is not None and any(e.get("event") in (None, "call") and e.get("effect") in ("write", "money", "admin")
                                   for e in traj[ans_idx + 1:]):
        if "no_stop" not in tags:
            tags.append("no_stop")
        ev.setdefault("no_stop", "kept changing state after it answered")
    codes = [e.get("error_code") for e in attempts if e.get("ok") is False]
    arg_fail = [c for c in codes if c in ARG_CODES]
    if len(arg_fail) >= 2 and len(arg_fail) * 3 >= max(1, len(attempts)):
        tags.append("bad_args"); ev["bad_args"] = f"{len(arg_fail)} calls failed on arguments ({', '.join(sorted(set(arg_fail))[:3])})"
    unknown = [e for e in attempts if e.get("event") == "unknown_command"]
    failed_tool_checks = [c for c in cell.get("checks", []) if c.get("type") == "trajectory" and c.get("ok") is False
                          and any(w in c.get("check", "") for w in ("used", "called_before"))]
    if unknown or failed_tool_checks:
        tags.append("wrong_tool"); ev["wrong_tool"] = (f"{len(unknown)} unknown command(s)" if unknown else failed_tool_checks[0]["detail"])
    nums = _answer_numbers(cell)
    outputs = traj and _outputs_text(traj)
    if nums and outputs:
        seen = numbers_in(outputs) | numbers_in(" ".join(s.get("instruction", "") for s in sessions))
        wrong = [x for x in nums if round(x, 6) not in seen]
        near = [(x, y) for x in wrong for y in seen if _near(x, y)]
        if near:
            tags.append("misread_output"); ev["misread_output"] = f"answer {near[0][0]:g} vs output {near[0][1]:g}"
        elif wrong:
            tags.append("did_math"); ev["did_math"] = f"{wrong[0]:g} is in no tool output"
    if not tags:
        tags.append("other"); ev["other"] = "no rule fired"
    primary = next(c for c in CLASSES if c in tags)
    return {"class": primary, "tags": tags, "evidence": ev.get(primary, "")}


def taxonomy(cells: list[dict]) -> dict:
    """Counts per class (and per subject) over the failed cells."""
    by_class: dict[str, int] = {}
    by_subject: dict[str, dict[str, int]] = {}
    for c in cells:
        r = classify(c)
        if r is None:
            continue
        by_class[r["class"]] = by_class.get(r["class"], 0) + 1
        d = by_subject.setdefault(c["subject"], {})
        d[r["class"]] = d.get(r["class"], 0) + 1
    return {"failed": sum(by_class.values()), "by_class": dict(sorted(by_class.items(), key=lambda kv: CLASSES.index(kv[0]))),
            "by_subject": by_subject}


def audit_sample(cells: list[dict], cls: str, n: int = 5, seed: int = 20261027) -> list[dict]:
    """A seeded random sample of the failed cells in one class, with what a person needs to judge the rule."""
    if cls not in CLASSES:
        raise ValueError(f"unknown class {cls!r}; classes: {', '.join(CLASSES)}")
    pool = sorted((c for c in cells if (classify(c) or {}).get("class") == cls), key=lambda c: c.get("key") or c["item"] + c["subject"])
    picked = random.Random(seed).sample(pool, min(n, len(pool)))
    out = []
    for c in picked:
        r = classify(c) or {}
        out.append({"item": c["item"], "subject": c["subject"], "seed": c["seed"], "split": c.get("split"),
                    "instruction": c.get("instruction"), "class": cls, "tags": r.get("tags"), "evidence": r.get("evidence"),
                    "failed_checks": [{"check": k["check"], "detail": k["detail"]} for k in c.get("checks", []) if k.get("ok") is False][:4],
                    "answer": c.get("answer"), "calls": [(e.get("cmdline"), e.get("status")) for e in flat_trajectory(c)][:12],
                    "answer_tail": (c.get("answer_tail") or "")[-200:]})
    return out
