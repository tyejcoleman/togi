"""The check library (EVAL-SPEC section 4). A check is a TOML table with a `type`; it returns pass / fail and a detail.

On the recorded answer (the structured `togi answer`, never free text):
    exact       path, expected [, ignore_case, strip]
    number      path, expected [, abs_tol, rel_tol]      "47,248", "$47,248.00" and 47248 are all 47248
    regex       path, pattern
    one_of      path, values
    set_equal   path, expected [, ordered]
State, after the run:
    state_jsonpath   cmd [, path, equals | not_equals | contains | matches | exists | gt | gte | lt | lte | len_equals] [, exit_code]
Trajectory (what the agent did, from the journal):
    trajectory  rule = used | never_used | max_calls | min_calls | called_before | ended_at_needs_approval |
                       approval_requested | no_forbidden_prefix | used_code_mode     (+ command / n / first / then / prefixes)
Budget:
    budget      max_turns, max_tokens, max_cost_usd
Judge (non-deterministic; reported separately, never gates):
    judge       rubric, calibration = [{answer, label}] (both labels), threshold
Legacy free text (discouraged; kept so stone tasks import unchanged):
    transcript_contains / transcript_not_contains   text

New check types register with `@register("name")`; `validate_check` is what `togi eval` runs on every item before it will
use it, because a faulty check lies.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from ..stone import jsonpath_get

ANSWER_TYPES = ("exact", "number", "regex", "one_of", "set_equal")
TRAJECTORY_RULES = ("used", "never_used", "max_calls", "min_calls", "called_before", "ended_at_needs_approval",
                    "approval_requested", "no_forbidden_prefix", "used_code_mode")
COMPARATORS = ("equals", "not_equals", "contains", "matches", "exists", "gt", "gte", "lt", "lte", "len_equals")
HOST_BUNDLE_NONE = None


@dataclass
class CheckContext:
    """Everything a check may look at. `answer` is the last VALID recorded answer in scope (None if there is none)."""
    item: Any = None
    session_index: int | None = None          # None = end of item
    answer: dict | None = None
    answers: list[dict] = field(default_factory=list)
    trajectory: list[dict] = field(default_factory=list)
    transcript: str = ""
    sandbox: Any = None
    turns: int | None = None
    tokens: int | None = None
    cost_usd: float | None = None
    forbidden: list[str] = field(default_factory=list)
    judge: Callable[[str, str], str] | None = None      # (rubric, answer_text) -> "pass" | "fail"


CHECKS: dict[str, Callable[[dict, CheckContext], tuple[bool | None, str]]] = {}
VALIDATORS: dict[str, Callable[[dict], list[str]]] = {}


def register(name: str, validator: Callable[[dict], list[str]] | None = None):
    def deco(fn):
        CHECKS[name] = fn
        if validator:
            VALIDATORS[name] = validator
        return fn
    return deco


# --------------------------------------------------------------------------- normalisation


_NUM_JUNK = re.compile(r"[,\s$€£¥_]")


def to_number(v: Any) -> float | None:
    """A number from an answer value: ints and floats as they are; strings with thousands separators, a currency sign or a
    trailing % are read; anything else is None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    if isinstance(v, str):
        t = _NUM_JUNK.sub("", v.strip())
        t = t[:-1] if t.endswith("%") else t
        neg = t.startswith("(") and t.endswith(")")
        t = t.strip("()")
        try:
            x = float(t)
        except ValueError:
            return None
        return -x if neg else x
    return None


def norm_text(v: Any, ignore_case: bool = True, strip: bool = True) -> Any:
    if isinstance(v, str):
        t = " ".join(v.split()) if strip else v
        return t.lower() if ignore_case else t
    return v


def same_value(a: Any, b: Any, ignore_case: bool = True) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    if isinstance(a, str) and isinstance(b, str):
        return norm_text(a, ignore_case) == norm_text(b, ignore_case)
    return a == b


def _get(answer: dict | None, path: str) -> tuple[bool, Any]:
    if answer is None:
        return False, None
    try:
        return True, jsonpath_get(answer, path)
    except (KeyError, IndexError, TypeError, ValueError):
        return False, None


def _short(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= 80 else s[:77] + "..."


# --------------------------------------------------------------------------- answer checks


def _v_path(c: dict) -> list[str]:
    return [] if isinstance(c.get("path", "$"), str) and str(c.get("path", "$")).startswith("$") else ["path must start with $"]


def _v_exact(c: dict) -> list[str]:
    return _v_path(c) + ([] if "expected" in c else ["needs `expected`"])


def _v_number(c: dict) -> list[str]:
    out = _v_path(c)
    if to_number(c.get("expected")) is None:
        out.append("`expected` must be a number")
    for k in ("abs_tol", "rel_tol"):
        if k in c and (to_number(c[k]) is None or to_number(c[k]) < 0):
            out.append(f"`{k}` must be a non-negative number")
    return out


def _v_regex(c: dict) -> list[str]:
    out = _v_path(c)
    try:
        re.compile(str(c.get("pattern", "")))
        if "pattern" not in c:
            out.append("needs `pattern`")
    except re.error:
        out.append("`pattern` is not a valid regular expression")
    return out


def _v_one_of(c: dict) -> list[str]:
    return _v_path(c) + ([] if isinstance(c.get("values"), list) and c["values"] else ["needs a non-empty `values` list"])


def _v_set_equal(c: dict) -> list[str]:
    return _v_path(c) + ([] if isinstance(c.get("expected"), list) else ["`expected` must be a list"])


@register("exact", _v_exact)
def check_exact(c: dict, ctx: CheckContext) -> tuple[bool, str]:
    found, got = _get(ctx.answer, c.get("path", "$"))
    if not found:
        return False, f"no value at {c.get('path', '$')} (no valid answer, or the field is missing)"
    ok = same_value(got, c["expected"], bool(c.get("ignore_case", True)))
    return ok, "" if ok else f"got {_short(got)}, expected {_short(c['expected'])}"


@register("number", _v_number)
def check_number(c: dict, ctx: CheckContext) -> tuple[bool, str]:
    found, got = _get(ctx.answer, c.get("path", "$"))
    if not found:
        return False, f"no value at {c.get('path', '$')} (no valid answer, or the field is missing)"
    x, want = to_number(got), to_number(c["expected"])
    if x is None:
        return False, f"{_short(got)} is not a number"
    ok = math.isclose(x, want, rel_tol=float(c.get("rel_tol", 0.0)), abs_tol=max(float(c.get("abs_tol", 0.0)), 1e-9))
    return ok, "" if ok else f"got {x:g}, expected {want:g}"


@register("regex", _v_regex)
def check_regex(c: dict, ctx: CheckContext) -> tuple[bool, str]:
    found, got = _get(ctx.answer, c.get("path", "$"))
    if not found:
        return False, f"no value at {c.get('path', '$')}"
    ok = re.search(str(c["pattern"]), got if isinstance(got, str) else json.dumps(got), re.I if c.get("ignore_case", True) else 0) is not None
    return ok, "" if ok else f"{_short(got)} does not match {c['pattern']!r}"


@register("one_of", _v_one_of)
def check_one_of(c: dict, ctx: CheckContext) -> tuple[bool, str]:
    found, got = _get(ctx.answer, c.get("path", "$"))
    if not found:
        return False, f"no value at {c.get('path', '$')}"
    ok = any(same_value(got, v, bool(c.get("ignore_case", True))) for v in c["values"])
    return ok, "" if ok else f"{_short(got)} is not one of {_short(c['values'])}"


@register("set_equal", _v_set_equal)
def check_set_equal(c: dict, ctx: CheckContext) -> tuple[bool, str]:
    found, got = _get(ctx.answer, c.get("path", "$"))
    if not found:
        return False, f"no value at {c.get('path', '$')}"
    if not isinstance(got, list):
        return False, f"expected a list, got {_short(got)}"
    ic = bool(c.get("ignore_case", True))

    def key(v: Any) -> Any:
        n = to_number(v) if not isinstance(v, str) or re.fullmatch(r"[\s$,\d.\-+]+", v) else None
        return ("n", n) if n is not None else ("s", json.dumps(norm_text(v, ic), sort_keys=True, default=str))

    want, have = [key(v) for v in c["expected"]], [key(v) for v in got]
    ok = want == have if c.get("ordered") else Counter(map(str, want)) == Counter(map(str, have))
    if ok:
        return True, ""
    missing = [v for v in c["expected"] if key(v) not in have]
    extra = [v for v in got if key(v) not in want]
    return False, f"missing {_short(missing)}, unexpected {_short(extra)}" if (missing or extra) else "order or duplicates differ"


# --------------------------------------------------------------------------- state


def _v_state(c: dict) -> list[str]:
    out = []
    if not isinstance(c.get("cmd"), str) or not c["cmd"].strip():
        out.append("needs `cmd`")
    if "path" in c:
        out += _v_path(c)
        if not any(k in c for k in COMPARATORS):
            pass       # a bare path means "exists"
    return out


@register("state_jsonpath", _v_state)
def check_state(c: dict, ctx: CheckContext) -> tuple[bool, str]:
    if ctx.sandbox is None:
        return False, "no sandbox to run the command in"
    try:
        p = ctx.sandbox.sh(str(c["cmd"]), agent=False)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    if "path" not in c:
        ok = p.returncode == int(c.get("exit_code", 0))
        return ok, "" if ok else f"exit {p.returncode}"
    try:
        actual = jsonpath_get(json.loads(p.stdout), str(c["path"]))
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return False, f"{type(e).__name__}: {str(e)[:80]}"
    ok, want = True, "exists"
    if "equals" in c:
        ok, want = same_value(actual, c["equals"], False) or str(actual) == str(c["equals"]), f"== {_short(c['equals'])}"
    elif "not_equals" in c:
        ok, want = not (same_value(actual, c["not_equals"], False) or str(actual) == str(c["not_equals"])), f"!= {_short(c['not_equals'])}"
    elif "contains" in c:
        ok, want = (c["contains"] in actual) if isinstance(actual, (str, list, dict)) else False, f"contains {_short(c['contains'])}"
    elif "matches" in c:
        ok, want = bool(re.search(str(c["matches"]), str(actual))), f"matches {c['matches']!r}"
    elif "len_equals" in c:
        ok, want = hasattr(actual, "__len__") and len(actual) == int(c["len_equals"]), f"length {c['len_equals']}"
    else:
        for op, fn in (("gt", lambda a, b: a > b), ("gte", lambda a, b: a >= b), ("lt", lambda a, b: a < b), ("lte", lambda a, b: a <= b)):
            if op in c:
                x, y = to_number(actual), to_number(c[op])
                ok, want = (x is not None and y is not None and fn(x, y)), f"{op} {c[op]}"
                break
    return ok, "" if ok else f"{c['path']} is {_short(actual)}, wanted {want}"


# --------------------------------------------------------------------------- trajectory


def cmd_matches(entry: dict, command: str) -> bool:
    """`notes add` matches `togi notes add` and `togi notes add --text x`; a leading `togi ` in the check is optional."""
    want = command.strip()
    want = want[5:].strip() if want.startswith("togi ") else want
    have = entry.get("cmdline", "")
    have = have[5:] if have.startswith("togi ") else have
    return have == want or have.startswith(want + " ")


def is_bundle_call(entry: dict) -> bool:
    return bool(entry.get("bundle")) and entry.get("event", "call") == "call"


def _v_trajectory(c: dict) -> list[str]:
    rule = c.get("rule")
    if rule not in TRAJECTORY_RULES:
        return [f"rule must be one of {', '.join(TRAJECTORY_RULES)}"]
    need = {"used": ("command",), "never_used": ("command",), "max_calls": ("n",), "min_calls": ("n",),
            "called_before": ("first", "then"), "no_forbidden_prefix": ("prefixes",)}.get(rule, ())
    return [f"rule {rule} needs `{k}`" for k in need if k not in c]


@register("trajectory", _v_trajectory)
def check_trajectory(c: dict, ctx: CheckContext) -> tuple[bool, str]:
    traj = ctx.trajectory
    rule = c["rule"]
    attempts = [e for e in traj if e.get("event", "call") in ("call", "unknown_command", "unknown_flag")]
    if rule == "used":
        ok = any(cmd_matches(e, c["command"]) for e in attempts)
        return ok, "" if ok else f"never ran `{c['command']}`"
    if rule == "never_used":
        hit = [e["cmdline"] for e in attempts if cmd_matches(e, c["command"])]
        return not hit, "" if not hit else f"ran `{hit[0]}`"
    if rule in ("max_calls", "min_calls"):
        n = len([e for e in attempts if (is_bundle_call(e) or c.get("include_host"))])
        ok = n <= int(c["n"]) if rule == "max_calls" else n >= int(c["n"])
        return ok, "" if ok else f"{n} calls, limit {rule.split('_')[0]} {c['n']}"
    if rule == "called_before":
        idx = {k: next((i for i, e in enumerate(attempts) if cmd_matches(e, c[k])), None) for k in ("first", "then")}
        if idx["first"] is None or idx["then"] is None:
            return False, "one of the commands was never run"
        ok = idx["first"] < idx["then"]
        return ok, "" if ok else f"`{c['then']}` came before `{c['first']}`"
    if rule == "ended_at_needs_approval":
        calls = [e for e in attempts if is_bundle_call(e)]
        ok = bool(calls) and calls[-1].get("status") == "needs_approval"
        return ok, "" if ok else (f"last call ended {calls[-1].get('status')}" if calls else "no bundle call at all")
    if rule == "approval_requested":
        hit = [e for e in attempts if e.get("status") == "needs_approval" and (not c.get("command") or cmd_matches(e, c["command"]))]
        return bool(hit), "" if hit else "no approval was requested"
    if rule == "no_forbidden_prefix":
        bad = sorted({e["cmdline"] for e in attempts for p in c["prefixes"] if cmd_matches(e, p)})
        return not bad, "" if not bad else f"attempted {', '.join(bad[:3])}"
    if rule == "used_code_mode":
        ok = any(e.get("surface") == "run" for e in traj)
        return ok, "" if ok else "never used code mode (togi run)"
    return False, f"unknown rule {rule}"


# --------------------------------------------------------------------------- budget


def _v_budget(c: dict) -> list[str]:
    return [] if any(k in c for k in ("max_turns", "max_tokens", "max_cost_usd")) else ["needs max_turns, max_tokens or max_cost_usd"]


@register("budget", _v_budget)
def check_budget(c: dict, ctx: CheckContext) -> tuple[bool, str]:
    bad = []
    for key, have in (("max_turns", ctx.turns), ("max_tokens", ctx.tokens), ("max_cost_usd", ctx.cost_usd)):
        if key in c and have is not None and have > c[key]:
            bad.append(f"{key[4:]} {have:g} over {c[key]:g}")
    return not bad, "; ".join(bad)


# --------------------------------------------------------------------------- judge


def _v_judge(c: dict) -> list[str]:
    out = []
    if not isinstance(c.get("rubric"), str) or not c["rubric"].strip():
        out.append("a judge check must declare its `rubric`")
    cal = c.get("calibration")
    if not isinstance(cal, list) or len(cal) < 2 or not all(isinstance(x, dict) and x.get("label") in ("pass", "fail") and "answer" in x for x in cal):
        out.append("a judge check must declare a `calibration` set of {answer, label} (>= 2, labels pass/fail)")
    elif {x["label"] for x in cal} != {"pass", "fail"}:
        out.append("the calibration set needs at least one pass and one fail example")
    return out


@register("judge", _v_judge)
def check_judge(c: dict, ctx: CheckContext) -> tuple[bool | None, str]:
    """Rubric LLM judge. Calibrated first on the declared set: if it disagrees with any labelled example (below `threshold`,
    default 1.0 agreement) the verdict is None (uncalibrated) and says so. Never gates (see `gating`)."""
    if ctx.judge is None:
        return None, "no judge configured (not scored)"
    cal = c["calibration"]
    agree = sum(1 for x in cal if ctx.judge(c["rubric"], json.dumps(x["answer"], ensure_ascii=False)) == x["label"]) / len(cal)
    if agree < float(c.get("threshold", 1.0)):
        return None, f"judge failed its calibration set ({agree:.0%} agreement)"
    verdict = ctx.judge(c["rubric"], json.dumps(ctx.answer, ensure_ascii=False) if ctx.answer is not None else ctx.transcript)
    if verdict not in ("pass", "fail"):
        return None, f"judge error ({verdict!r}): not scored"
    return verdict == "pass", f"judge: {verdict} (calibration {agree:.0%})"


@register("answer_valid")
def check_answer_valid(c: dict, ctx: CheckContext) -> tuple[bool, str]:
    """A valid structured answer was recorded (implicit for every item with an answer_schema)."""
    ok = ctx.answer is not None
    if ok:
        return True, ""
    bad = [a for a in ctx.answers if not a.get("valid")]
    return False, ("the answer was refused: " + "; ".join((bad[-1].get("errors") or ["?"])[:2])) if bad else "no answer was recorded (`togi answer`)"


# --------------------------------------------------------------------------- legacy free text


def _v_text(c: dict) -> list[str]:
    return [] if isinstance(c.get("text"), str) else ["needs `text`"]


@register("transcript_contains", _v_text)
def check_transcript_contains(c: dict, ctx: CheckContext) -> tuple[bool, str]:
    ok = c["text"] in ctx.transcript
    return ok, "" if ok else "not found"


@register("transcript_not_contains", _v_text)
def check_transcript_not_contains(c: dict, ctx: CheckContext) -> tuple[bool, str]:
    ok = c["text"] not in ctx.transcript
    return ok, "" if ok else "found"


# --------------------------------------------------------------------------- public API


def validate_check(c: dict, item: Any = None) -> list[str]:
    t = c.get("type")
    if t not in CHECKS:
        return [f"unknown check type {t!r}; known: {', '.join(sorted(CHECKS))}"]
    problems = VALIDATORS.get(t, lambda _c: [])(c)
    if t in ANSWER_TYPES and item is not None:
        sch = getattr(item, "answer_schema", None)
        if sch is None and not getattr(item, "legacy", False):
            problems.append("an answer check needs the item to declare an answer_schema")
        elif isinstance(sch, dict) and sch.get("properties"):
            m = re.match(r"\$\.([A-Za-z0-9_\-]+)", str(c.get("path", "$")))
            if m and m.group(1) not in sch["properties"]:
                problems.append(f"path {c.get('path')} is not a field of the answer_schema (fields: {', '.join(sch['properties'])})")
    return problems


def check_label(c: dict) -> str:
    t = c.get("type", "?")
    if t in ANSWER_TYPES:
        return f"{t} {c.get('path', '$')} {_short(c.get('expected', c.get('values', c.get('pattern', ''))))}"
    if t == "trajectory":
        return "trajectory " + c.get("rule", "?") + (f" {c['command']}" if c.get("command") else "") + (
            f" {c['n']}" if "n" in c else "")
    if t == "state_jsonpath":
        return f"state {c.get('cmd', '')} {c.get('path', '')}".strip()
    if t == "judge":
        return "judge: " + str(c.get("rubric", ""))[:50]
    if t.startswith("transcript"):
        return f"{t} {c.get('text', '')!r}"
    return t


def evaluate(c: dict, ctx: CheckContext) -> dict:
    """Run one check -> {"type", "check", "ok", "detail", "gating"}. A check that crashes fails with the reason (it is not
    skipped: a check that cannot be evaluated must not look like a pass)."""
    t = c.get("type")
    fn = CHECKS.get(t)
    if fn is None:
        ok, detail = False, f"unknown check type {t!r}"
    else:
        try:
            ok, detail = fn(c, ctx)
        except Exception as e:      # a faulty check is a finding, not a crash
            ok, detail = False, f"check error: {type(e).__name__}: {str(e)[:80]}"
    gating = t != "judge" and c.get("gate", True) is not False
    return {"type": t, "check": check_label(c), "ok": ok, "detail": detail, "gating": gating}


def evaluate_all(checks: list[dict], ctx: CheckContext) -> list[dict]:
    return [evaluate(c, ctx) for c in checks]


def passed(results: list[dict]) -> bool:
    """All gating checks pass. A judge result (or a non-gating one) is reported but never decides."""
    return all(r["ok"] is True for r in results if r.get("gating", True))
