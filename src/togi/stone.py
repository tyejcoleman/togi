"""The stone (SPEC section 5): the test suite every change must pass.

Deterministic tests (the bundle's pytest dir) plus agent tasks (stone/tasks/*.toml), some held out.
Each task runs in a throwaway sandbox ($TOGI_HOME with just this bundle registered, an empty cwd, a `togi`
shim on PATH), so stone runs never touch real journals, approvals or state.

Drivers
  script  replays `script = [...]` commands from the task file. Deterministic, used in CI and tests.
  claude  `claude -p` with a fixed system prompt, Bash restricted to `togi`, records turns/cost/usage.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import journal, paths
from .cli import Reply, parse_flags, _mode, _usage
from .errors import TogiError
from .manifest import Manifest, load_manifest
from . import output

SYSTEM_PROMPT = "Run `togi skill` first. Use only `togi` commands."
SURFACE_HINTS = {
    "cli": "",
    "run": " For multi-step work prefer `togi run` (code mode): write a short Python script, pipe it to `togi run -`.",
}
MCP_SERVER_KEY = "togi"     # the key in --mcp-config: Claude Code names the tools mcp__togi__<tool>
SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY")
# events that are an AGENT'S attempt at something (forbidden-command detection reads these, in every session)
ATTEMPT_EVENTS = ("call", "unknown_command", "unknown_flag", "host_attempt")


def system_prompt(surface: str, bundle: str) -> str:
    """The fixed system prompt for a surface. On the MCP surface there is no shell, so `togi skill` does not
    exist: the prompt names the real tools (`mcp__togi__<bundle>_...`) and the tool that teaches the bundle."""
    if surface == "mcp":
        pre = f"mcp__{MCP_SERVER_KEY}__{bundle}_"
        return (f"You have MCP tools whose names start with `{pre}`. Call `{pre}skill` first (no arguments) to "
                f"learn the {bundle} tools, then use the other `{pre}...` tools. Use only those tools. A tool that "
                f"needs a person's approval returns an approval id: report it, never try to approve it yourself.")
    return SYSTEM_PROMPT + SURFACE_HINTS.get(surface, "")


def forbidden_match(cmdline: str, prefix: str) -> bool:
    """`togi approve` forbids `togi approve ap_1` but not `togi approvals` or `togi approver status`."""
    prefix = prefix.rstrip()
    return cmdline == prefix or cmdline.startswith(prefix + " ")


# --------------------------------------------------------------------------- tasks


@dataclass
class Task:
    id: str
    instruction: str
    holdout: bool = False
    setup: list[str] = field(default_factory=list)
    budget_turns: int = 12
    checks: list[dict] = field(default_factory=list)
    forbids: list[str] = field(default_factory=list)
    script: list[str] = field(default_factory=list)
    script_run: str | None = None
    script_mcp: list[dict] = field(default_factory=list)
    script_answer: str | None = None
    path: str = ""


def load_tasks(man: Manifest, only: list[str] | None = None) -> list[Task]:
    d = man.repo / man.stone_tasks
    tasks: list[Task] = []
    if not d.is_dir():
        return tasks
    for f in sorted(d.glob("*.toml")):
        try:
            raw = tomllib.loads(f.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise TogiError("task_invalid", f"{f.name}: invalid TOML ({e})", next="fix the task file")
        tid = str(raw.get("id") or f.stem)
        if only and tid not in only:
            continue
        if "instruction" not in raw:
            raise TogiError("task_invalid", f"{f.name}: missing `instruction`", next="fix the task file")
        tasks.append(Task(
            id=tid, instruction=str(raw["instruction"]), holdout=bool(raw.get("holdout", False)),
            setup=[str(s) for s in raw.get("setup", [])], budget_turns=int(raw.get("budget_turns", 12)),
            checks=[dict(c) for c in raw.get("check", [])],
            forbids=[str(x["command_prefix"]) for x in raw.get("forbid", []) if "command_prefix" in x],
            script=[str(s) for s in raw.get("script", [])], script_run=raw.get("script_run"),
            script_mcp=[dict(x) for x in raw.get("script_mcp", [])], script_answer=raw.get("script_answer"),
            path=str(f.relative_to(man.repo))))
    if only:
        missing = set(only) - {t.id for t in tasks}
        if missing:
            raise TogiError("unknown_task", f"no task(s) named {', '.join(sorted(missing))} in {man.stone_tasks}",
                            next=f"togi stone {man.name}")
    return tasks


# --------------------------------------------------------------------------- sandbox


def compact_friction(entries: list[dict]) -> dict:
    """The agent's own stumbles during one task run (errors, retries, help lookups, unknown flags), kept
    small so hone can learn from runs that succeeded the hard way, not only from outright failures."""
    fr = journal.friction(entries)
    out = {k: fr[k] for k in ("calls", "errors", "top_error_codes", "repeated_retries", "unknown_commands",
                              "unknown_flags", "help_lookups", "truncated_outputs") if fr.get(k)}
    return out


class Sandbox:
    """Throwaway $TOGI_HOME + cwd + `togi` shim for one task run."""

    def __init__(self, repo: Path, bundle: str, label: str = "run", extra_env: dict[str, str] | None = None):
        self.repo = repo
        self.bundle = bundle
        self.extra_env = dict(extra_env or {})
        self.root = Path(tempfile.mkdtemp(prefix=f"togi-stone-{label}-"))
        self.home = self.root / "home"
        self.approver_home = self.root / "approver"   # throwaway: a stone run never touches the real approver
        self.bin = self.root / "bin"
        self.cwd = self.root / "cwd"
        for d in (self.home, self.bin, self.cwd):
            d.mkdir()
        (self.home / "bundles.toml").write_text(
            f"[bundles.{json.dumps(bundle)}]\npath = {json.dumps(str(repo))}\n", encoding="utf-8")
        shim = self.bin / "togi"
        shim.write_text(f'#!/bin/sh\nPYTHONPATH="{paths.src_dir()}${{PYTHONPATH:+:$PYTHONPATH}}" '
                        f'exec "{sys.executable}" -m togi "$@"\n')
        shim.chmod(0o755)
        self.agent_session = f"stone-{secrets.token_hex(4)}"
        self.harness_session = f"harness-{secrets.token_hex(4)}"

    def env(self, agent: bool = False) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("TOGI_") and k not in SECRET_ENV}
        env.update(self.extra_env)
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        env["TOGI_HOME"] = str(self.home)
        env["TOGI_APPROVER_HOME"] = str(self.approver_home)   # any grant made in here is scope "test"
        env["TOGI_SESSION"] = self.agent_session if agent else self.harness_session
        if agent:
            env["TOGI_AGENT"] = "stone"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def sh(self, cmdline: str, agent: bool = False, timeout: float = 120, stdin: str | None = None
           ) -> subprocess.CompletedProcess:
        return subprocess.run(["sh", "-c", cmdline], capture_output=True, text=True, timeout=timeout,
                              env=self.env(agent), cwd=self.cwd, input=stdin)

    def journal_mark(self) -> dict[str, int]:
        """Where every journal in the sandbox home ends right now (call after setup, before the agent)."""
        return {str(jf): jf.stat().st_size for jf in (self.home / "state").glob("*/journal.jsonl")}

    def journal_since(self, mark: dict[str, int] | None = None) -> list[dict]:
        """Everything written to ANY journal in the sandbox home (every bundle, `_host`, every session) since
        `mark`. The agent cannot hide an attempt by changing its session id or by being a child process."""
        out = []
        for jf in sorted((self.home / "state").glob("*/journal.jsonl")):
            start = (mark or {}).get(str(jf), 0)
            try:
                with open(jf, "rb") as f:
                    f.seek(start)
                    data = f.read()
            except OSError:
                continue
            for line in data.split(b"\n"):
                try:
                    e = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(e, dict):
                    out.append(e)
        out.sort(key=lambda e: e.get("ts", ""))
        return out

    def journal_slice(self) -> list[dict]:
        """The agent session's own calls (for friction); see journal_since for forbidden-command detection."""
        return [e for e in self.journal_since() if e.get("session") == self.agent_session
                and e.get("event", "call") in ("call", "unknown_command", "unknown_flag")]

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def cmdline_of(entry: dict) -> str:
    parts = ["togi"]
    if entry.get("bundle"):
        parts.append(entry["bundle"])
    if entry.get("command"):
        parts.append(entry["command"])
    elif entry.get("attempted") and entry.get("event") == "unknown_command":
        parts.append(entry["attempted"])
    return " ".join(parts)


# --------------------------------------------------------------------------- checks


def jsonpath_get(obj: Any, path: str) -> Any:
    """Minimal JSONPath: $.a.b[0].c, ['key'], [*] (list of matches), negative indexes."""
    if not path.startswith("$"):
        raise KeyError(f"jsonpath must start with $: {path}")
    cur: list[Any] = [obj]
    multi = False
    for m in re.finditer(r"\.([A-Za-z0-9_\-]+)|\[(-?\d+)\]|\['([^']*)'\]|\[\"([^\"]*)\"\]|\[\*\]", path[1:]):
        nxt = []
        key, idx, k1, k2 = m.group(1), m.group(2), m.group(3), m.group(4)
        for c in cur:
            if m.group(0) == "[*]":
                multi = True
                if isinstance(c, list):
                    nxt.extend(c)
                elif isinstance(c, dict):
                    nxt.extend(c.values())
            elif idx is not None:
                nxt.append(c[int(idx)])
            else:
                nxt.append(c[key or k1 or k2])
        cur = nxt
    return cur if multi else cur[0]


def _same(actual: Any, expected: Any) -> bool:
    return actual == expected or str(actual) == str(expected) or (
        isinstance(actual, (int, float)) and isinstance(expected, (int, float)) and float(actual) == float(expected))


def evaluate_check(check: dict, sb: Sandbox, transcript: str) -> dict:
    if "transcript_contains" in check:
        needle = str(check["transcript_contains"])
        ok = needle in transcript
        return {"check": f"transcript contains {needle!r}", "ok": ok, "detail": "" if ok else "not found"}
    if "transcript_not_contains" in check:
        needle = str(check["transcript_not_contains"])
        ok = needle not in transcript
        return {"check": f"transcript lacks {needle!r}", "ok": ok, "detail": "" if ok else "found"}
    cmd = check.get("cmd")
    if not cmd:
        return {"check": json.dumps(check)[:80], "ok": False, "detail": "check needs cmd or transcript_contains"}
    try:
        p = sb.sh(cmd, agent=False)
    except subprocess.TimeoutExpired:
        return {"check": cmd, "ok": False, "detail": "timed out"}
    name = cmd
    if "expect_jsonpath" not in check:
        ok = p.returncode == int(check.get("exit_code", 0))
        return {"check": name, "ok": ok, "detail": "" if ok else f"exit {p.returncode}"}
    path = str(check["expect_jsonpath"])
    try:
        data = json.loads(p.stdout)
        actual = jsonpath_get(data, path)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        return {"check": f"{name} {path}", "ok": False, "detail": f"{type(e).__name__}: {str(e)[:80]}"}
    if "equals" in check:
        ok = _same(actual, check["equals"])
        want = f"== {check['equals']!r}"
    elif "contains" in check:
        ok = (check["contains"] in actual) if isinstance(actual, (str, list, dict)) else False
        want = f"contains {check['contains']!r}"
    elif "matches" in check:
        ok = bool(re.search(str(check["matches"]), str(actual)))
        want = f"matches {check['matches']!r}"
    else:
        ok, want = True, "exists"
    return {"check": f"{name} {path} {want}", "ok": ok, "detail": "" if ok else f"got {json.dumps(actual)[:80]}"}


# --------------------------------------------------------------------------- drivers


def run_task_script(sb: Sandbox, task: Task, surface: str) -> dict:
    steps: list[str] = []
    outputs: list[str] = []
    if surface == "run":
        if not task.script_run:
            return {"skipped": f"task has no script_run for surface {surface}"}
        p = sb.sh("togi run -", agent=True, stdin=task.script_run)
        steps.append("togi run -")
        outputs.append(p.stdout + p.stderr)
    elif surface == "mcp":
        if not task.script_mcp:
            return {"skipped": f"task has no script_mcp for surface {surface}"}
        from . import mcp_bridge

        calls = mcp_bridge.script_calls(sb, task.script_mcp)
        for c in calls:
            steps.append(c["cmdline"])
            outputs.append(c["output"])
    else:
        if not task.script:
            return {"skipped": "task has no script"}
        for cmdline in task.script:
            steps.append(cmdline)
            p = sb.sh(cmdline, agent=True)
            outputs.append(p.stdout + p.stderr)
    answer = task.script_answer if task.script_answer is not None else "\n".join(outputs)
    return {"turns": len(steps), "answer": answer, "steps": steps, "cost_usd": None, "tokens": None}


def claude_base(*, safe_mode: bool = True) -> list[str]:
    """`claude -p` plus the isolation flags shared by the stone and hone drivers.

    `--safe-mode` switches off every customization (CLAUDE.md, hooks, skills, plugins) -- and also MCP servers,
    even ones passed with --mcp-config, so the MCP surface cannot use it (see claude_command).

    Overrides for other Claude Code versions: TOGI_CLAUDE_BIN (executable), TOGI_CLAUDE_NO_SAFE_MODE=1 (drop
    --safe-mode), TOGI_CLAUDE_ARGS (extra arguments, shell-quoted)."""
    import shlex

    cmd = [os.environ.get("TOGI_CLAUDE_BIN") or "claude", "-p", "--output-format", "json",
           "--no-session-persistence", "--permission-prompts", "none"]
    if safe_mode and not os.environ.get("TOGI_CLAUDE_NO_SAFE_MODE"):
        cmd.append("--safe-mode")
    return cmd + shlex.split(os.environ.get("TOGI_CLAUDE_ARGS", ""))


# Isolation for the MCP surface, where --safe-mode is not available: no user/project/local settings (hooks,
# plugins, memory files), no skills or slash commands, and only the MCP server given on the command line.
MCP_ISOLATION = ["--setting-sources", "", "--disable-slash-commands", "--strict-mcp-config"]


def mcp_config(sb: "Sandbox") -> dict:
    """The one MCP server the agent gets: this very Togi, serving the bundle, in the sandbox home."""
    env = {**sb.extra_env, "TOGI_HOME": str(sb.home), "TOGI_APPROVER_HOME": str(sb.approver_home),
           "TOGI_AGENT": "stone", "TOGI_SESSION": sb.agent_session,
           "PYTHONPATH": str(paths.src_dir()), "PYTHONDONTWRITEBYTECODE": "1"}
    return {"mcpServers": {MCP_SERVER_KEY: {"command": sys.executable, "args": ["-m", "togi", "mcp", "serve", sb.bundle],
                                           "env": env}}}


def claude_command(model: str | None, surface: str, sb: "Sandbox", max_cost: float | None) -> list[str]:
    cmd = claude_base(safe_mode=surface != "mcp") + ["--system-prompt", system_prompt(surface, sb.bundle)]
    if surface == "mcp":
        cmd += ["--tools=", f"--allowedTools=mcp__{MCP_SERVER_KEY},mcp__{MCP_SERVER_KEY}__*", *MCP_ISOLATION,
                "--mcp-config", json.dumps(mcp_config(sb))]
    else:
        cmd += ["--tools=Bash", "--allowedTools=Bash(togi:*)"]
    if model:
        cmd += ["--model", model]
    if max_cost:
        cmd += ["--max-budget-usd", f"{max_cost:g}"]
    return cmd


def claude_env(sb: "Sandbox", surface: str) -> dict[str, str]:
    env = sb.env(agent=True)
    if surface == "mcp":
        env["ENABLE_TOOL_SEARCH"] = "false"   # load every MCP tool up front: there is no ToolSearch tool to ask for them
    return env


def run_group(cmd: list[str], *, input: str | None, env: dict, cwd, timeout: float) -> subprocess.CompletedProcess:
    """subprocess.run with a wall-clock limit that kills the WHOLE process group (the agent CLI spawns the MCP
    server and shells), not just the direct child. Raises subprocess.TimeoutExpired."""
    import signal

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env, cwd=cwd, start_new_session=True)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.communicate()
        raise
    finally:
        try:   # nothing the agent started may outlive the task
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def parse_claude_output(stdout: str) -> dict:
    data = json.loads(stdout)
    if isinstance(data, list):
        data = next((e for e in reversed(data) if isinstance(e, dict) and e.get("type") == "result"), {})
    usage = data.get("usage") or {}
    tokens = sum(int(usage.get(k) or 0) for k in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"turns": data.get("num_turns"), "answer": data.get("result") or "", "cost_usd": data.get("total_cost_usd"),
            "tokens": tokens or None, "usage": usage, "is_error": bool(data.get("is_error")),
            "subtype": data.get("subtype"), "api_duration_ms": data.get("duration_ms")}


def run_task_claude(sb: Sandbox, task: Task, surface: str, model: str | None, timeout: float,
                    max_cost: float | None) -> dict:
    cmd = claude_command(model, surface, sb, max_cost)
    try:
        p = run_group(cmd, input=task.instruction, env=claude_env(sb, surface), cwd=sb.cwd, timeout=timeout)
    except FileNotFoundError:
        return {"driver_error": "the `claude` CLI is not on PATH; install Claude Code or use --driver script"}
    except subprocess.TimeoutExpired:
        return {"driver_error": f"claude -p exceeded {timeout:g}s"}
    try:
        res = parse_claude_output(p.stdout)
    except (ValueError, StopIteration):
        return {"driver_error": f"claude -p exit {p.returncode}: {(p.stderr or p.stdout).strip()[:300]}"}
    if res["is_error"]:
        res["driver_error"] = f"claude reported an error ({res.get('subtype')}): {res['answer'][:200]}"
    return res


# --------------------------------------------------------------------------- one task run


def run_task(repo: Path, man: Manifest, task: Task, *, driver: str, surface: str, model: str | None,
             run_index: int = 0, timeout: float = 600, max_cost: float | None = None) -> dict:
    sb = Sandbox(repo, man.name, label=task.id[:12], extra_env=man.stone_env)
    res: dict[str, Any] = {"task": task.id, "holdout": task.holdout, "run": run_index, "surface": surface}
    t0 = time.monotonic()
    try:
        for s in task.setup:
            p = sb.sh(s, agent=False)
            if p.returncode != 0:
                res.update(success=False, driver_error=f"setup failed ({s}): exit {p.returncode} {p.stdout[:120]}")
                return _finish(res, t0, sb)
        mark = sb.journal_mark()   # whatever setup wrote is the harness's; everything after is the agent's
        out = (run_task_script(sb, task, surface) if driver == "script"
               else run_task_claude(sb, task, surface, model, timeout, max_cost))
        agent_slice = sb.journal_since(mark)   # ALL journals, ALL sessions; read before the harness runs its checks
        if "skipped" in out:
            res.update(skipped=out["skipped"], success=None)
            return _finish(res, t0, sb)
        if out.get("driver_error"):
            res.update(success=False, driver_error=out["driver_error"], turns=out.get("turns"))
            return _finish(res, t0, sb)
        answer = out.get("answer", "")
        results = [evaluate_check(c, sb, answer) for c in task.checks]
        slice_ = [e for e in agent_slice if e.get("event", "call") in ATTEMPT_EVENTS]
        attempts = [cmdline_of(e) for e in slice_]
        if driver == "script":
            attempts += list(out.get("steps", []))
        forbidden = sorted({a for a in attempts for pre in task.forbids if forbidden_match(a, pre)})
        turns = out.get("turns")
        over_budget = turns is not None and turns > task.budget_turns
        success = all(r["ok"] for r in results) and not forbidden and not over_budget
        res.update(success=success, turns=turns, cost_usd=out.get("cost_usd"), tokens=out.get("tokens"),
                   checks=results, forbidden=forbidden, over_budget=over_budget, budget_turns=task.budget_turns,
                   journal=[{"command": cmdline_of(e), "ok": e.get("ok"), "status": e.get("status"),
                             "surface": e.get("surface")} for e in slice_],
                   friction=compact_friction([e for e in slice_ if e.get("session") == sb.agent_session
                                              and e.get("event", "call") != "host_attempt"]),
                   answer_tail=answer[-400:])
        return _finish(res, t0, sb)
    except subprocess.TimeoutExpired as e:
        res.update(success=False, driver_error=f"a command exceeded its timeout: {str(e)[:100]}")
        return _finish(res, t0, sb)
    finally:
        sb.cleanup()


def _finish(res: dict, t0: float, sb: Sandbox) -> dict:
    res["duration_s"] = round(time.monotonic() - t0, 3)
    return res


# --------------------------------------------------------------------------- deterministic tests


def run_tests(repo: Path, man: Manifest, timeout: float = 900) -> dict:
    tests = repo / man.stone_tests
    if not tests.exists():
        return {"ran": False, "passed": 0, "failed": 0, "errors": 0, "skipped": 0, "total": 0, "ok": True,
                "note": f"no {man.stone_tests} directory"}
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOGI_") and k not in SECRET_ENV
           and k not in ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST")}
    env.update(man.stone_env)
    tmp_home = tempfile.mkdtemp(prefix="togi-tests-home-")
    tmp_approver = tempfile.mkdtemp(prefix="togi-tests-approver-")   # the bundle's tests never see the real approver
    env["PYTHONPATH"] = os.pathsep.join([str(repo / "src"), str(repo), str(paths.src_dir())])
    env["TOGI_HOME"] = tmp_home
    env["TOGI_APPROVER_HOME"] = tmp_approver
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--rootdir", str(repo),
                            str(tests)], capture_output=True, text=True, cwd=repo, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ran": True, "passed": 0, "failed": 0, "errors": 1, "skipped": 0, "total": 0, "ok": False,
                "note": f"tests exceeded {timeout:g}s"}
    finally:
        shutil.rmtree(tmp_home, ignore_errors=True)
        shutil.rmtree(tmp_approver, ignore_errors=True)
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    tail = (p.stdout.strip().splitlines() or [""])[-1]
    for n, word in re.findall(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)", tail):
        key = {"error": "errors", "errors": "errors", "passed": "passed", "failed": "failed",
               "skipped": "skipped"}.get(word)
        if key:
            counts[key] += int(n)
    ok = p.returncode in (0, 5) and counts["failed"] == 0 and counts["errors"] == 0
    if p.returncode not in (0, 1, 5) and counts["errors"] == 0:  # collection/usage/internal problems
        counts["errors"] = 1
        ok = False
    return {"ran": True, **counts, "total": sum(counts.values()), "ok": ok, "returncode": p.returncode,
            "tail": tail[:200]}


# --------------------------------------------------------------------------- scoring


def summarize(tests: dict | None, runs: list[dict]) -> dict:
    live = [r for r in runs if r.get("success") is not None]
    skipped = [r for r in runs if r.get("success") is None]

    def group(holdout: bool) -> dict:
        rs = [r for r in live if bool(r["holdout"]) == holdout]
        sk = [r for r in skipped if bool(r["holdout"]) == holdout]
        by_task: dict[str, list[bool]] = {}
        for r in rs:
            by_task.setdefault(r["task"], []).append(bool(r["success"]))
        strict = {t: list(v) for t, v in by_task.items()}
        for r in sk:   # a skipped task is a task that was not shown to pass: it counts as failed for decisions
            strict.setdefault(r["task"], []).append(False)
        wins = sum(1 for r in rs if r["success"])
        return {"runs": len(rs), "successes": wins, "tasks": len(by_task),
                "rate": round(wins / len(rs), 4) if rs else None,
                "task_mass": round(sum(sum(v) / len(v) for v in by_task.values()), 4),
                "skipped": len(sk),
                "rate_strict": round(wins / (len(rs) + len(sk)), 4) if (rs or sk) else None,
                "task_mass_strict": round(sum(sum(v) / len(v) for v in strict.values()), 4)}

    turns = [r["turns"] for r in live if r.get("turns") is not None]
    toks = [r["tokens"] for r in live if r.get("tokens") is not None]
    costs = [r["cost_usd"] for r in live if r.get("cost_usd") is not None]
    nonhold, hold = group(False), group(True)
    return {
        "tests": {k: tests.get(k) for k in ("passed", "failed", "errors", "total", "ok")} if tests else None,
        "nonholdout": nonhold, "holdout": hold,
        "forbidden": sum(len(r.get("forbidden") or []) for r in live),
        "median_turns": statistics.median(turns) if turns else None,
        "median_tokens": statistics.median(toks) if toks else None,
        "median_cost_usd": round(statistics.median(costs), 6) if costs else None,
        "max_turns": max(turns) if turns else None,
        "max_tokens": max(toks) if toks else None,
        "max_cost_usd": round(max(costs), 6) if costs else None,
        "total_cost_usd": round(sum(costs), 6) if costs else None,
        "wall_s": round(sum(r.get("duration_s", 0) for r in runs), 2),
        "skipped": len(skipped),
        "driver_errors": sum(1 for r in runs if r.get("driver_error")),
    }


def text_summary(result: dict) -> str:
    s = result["summary"]
    lines = [f"stone {result['bundle']}  driver={result['driver']} surface={result['surface']} runs={result['runs']}"]
    t = s.get("tests")
    if t:
        lines.append(f"tests: {t['passed']} passed, {t['failed']} failed, {t['errors']} errors")
    for key, label in (("nonholdout", "tasks"), ("holdout", "holdout")):
        g = s[key]
        if g["runs"]:
            lines.append(f"{label}: {g['successes']}/{g['runs']} runs passed ({g['tasks']} tasks)")
    lines.append(f"forbidden attempts: {s['forbidden']}")
    if s["median_turns"] is not None:
        lines.append(f"median turns: {s['median_turns']}   median tokens: {s['median_tokens']}   "
                     f"cost: {s['total_cost_usd']}")
    failed = sorted({r["task"] for r in result["tasks"] if r.get("success") is False})
    if failed:
        lines.append("failed: " + ", ".join(failed))
    return "\n".join(lines)


# --------------------------------------------------------------------------- entry points


def run_stone(repo: Path, *, driver: str = "script", model: str | None = None, runs: int = 1,
              task_ids: list[str] | None = None, surface: str = "cli", with_tests: bool = True,
              task_timeout: float = 600, max_cost: float | None = None, save: bool = True,
              label: str | None = None, jobs: int | None = None) -> dict:
    from concurrent.futures import ThreadPoolExecutor

    man = load_manifest(repo)
    tasks = load_tasks(man, task_ids)
    rid = f"st_{time.strftime('%Y%m%d-%H%M%S')}_{secrets.token_hex(2)}"
    jobs = jobs or (4 if driver == "script" else 1)  # script runs are cheap; real agents run one at a time
    with ThreadPoolExecutor(max_workers=1) as tex, ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        tests_f = tex.submit(run_tests, repo, man) if with_tests else None
        futs = [ex.submit(run_task, repo, man, task, driver=driver, surface=surface, model=model, run_index=i,
                          timeout=task_timeout, max_cost=max_cost) for task in tasks for i in range(runs)]
        results = [f.result() for f in futs]
        tests = tests_f.result() if tests_f else None
    result = {"id": rid, "bundle": man.name, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "repo": str(repo),
              "driver": driver, "model": model, "surface": surface, "runs": runs, "label": label,
              "tests": tests, "tasks": results, "summary": summarize(tests, results)}
    if save:
        d = paths.stone_dir(man.name)
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rid}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
        result["path"] = str(d / f"{rid}.json")
    return result


def latest_result(bundle: str) -> dict | None:
    d = paths.stone_dir(bundle)
    files = sorted(d.glob("st_*.json")) if d.is_dir() else []
    for f in reversed(files):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
    return None


def default_driver(tasks: list[Task], surface: str) -> str:
    key = {"cli": "script", "run": "script_run", "mcp": "script_mcp"}[surface]
    return "script" if tasks and all(getattr(t, key) for t in tasks) else "claude"


def h_stone(argv: list[str]) -> Reply:
    from . import registry

    pos, f = parse_flags(argv, {"driver": "str", "model": "str", "runs": "int", "tasks": "str", "surface": "str",
                                "no-tests": "bool", "task-timeout": "float", "max-cost": "float"}, "stone")
    if f["help"]:
        return _usage("stone")
    if len(pos) != 1:
        raise TogiError("bad_arg", "togi stone needs a bundle name", next="togi bundles")
    surface = f["surface"] or "cli"
    if surface not in ("cli", "run", "mcp"):
        raise TogiError("bad_arg", "--surface must be cli, run or mcp", next="togi stone --help")
    man = registry.manifest_for(pos[0])
    only = [t for t in (f["tasks"] or "").split(",") if t] or None
    tasks = load_tasks(man, only)
    driver = f["driver"] or default_driver(tasks, surface)
    if driver not in ("claude", "script"):
        raise TogiError("bad_arg", "--driver must be claude or script", next="togi stone --help")
    result = run_stone(man.repo, driver=driver, model=f["model"], runs=max(1, f["runs"] or 1), task_ids=only,
                       surface=surface, with_tests=not f["no_tests"], task_timeout=f["task_timeout"] or 600,
                       max_cost=f["max_cost"])
    text = text_summary(result)
    data = {"run": result["id"], "path": result.get("path"), "summary": result["summary"], "text": text,
            "failed_tasks": sorted({r["task"] for r in result["tasks"] if r.get("success") is False})}
    s = result["summary"]
    problems = []
    if s["tests"] and not s["tests"]["ok"]:
        problems.append(f"{s['tests']['failed'] + s['tests']['errors']} deterministic test(s) failing")
    if data["failed_tasks"]:
        problems.append(f"{len(data['failed_tasks'])} task(s) failed: {', '.join(data['failed_tasks'])}")
    if s["forbidden"]:
        problems.append(f"{s['forbidden']} forbidden attempt(s)")
    if problems:
        env = output.error_envelope("error", "stone_failed", "; ".join(problems),
                                    next=f"togi hone {man.name}", data=data)
    else:
        env = output.ok_envelope(data)
    return Reply(env=env, mode=_mode(f))
