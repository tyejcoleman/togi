"""Providers: how ONE SESSION of one cell is driven. A session is a cold start: a new agent session id, a fresh conversation,
and nothing from earlier sessions except what Togi itself holds (journal, task record, bundle state).

    script           deterministic replay of the item's scripts (CI, oracle references)
    claude-cli       Claude Code's own loop: `claude -p` with Bash(togi:*) (restricted, see stone.BASH_TOGI_ONLY) or the MCP server
    claude-step      OUR loop (loops.py), a Claude model as the per-step policy
    ollama / openai-compatible   OUR loop, a local model as the per-step policy

Every provider returns a SessionRun. Whatever it says about itself is not trusted for checks: the harness reads the journal,
the answers log and the task record of the sandbox.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

from .. import stone
from . import loops, policy
from .items import Item, Session
from .loops import Observation
from .sandbox import EvalSandbox
from .subject import Subject

MAX_OUT = 4000


@dataclass
class SessionRun:
    turns: int | None = None
    answer_text: str = ""
    cost_usd: float | None = None
    tokens: int | None = None
    prompt_tokens: int | None = None
    steps: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    finish_reason: str = ""
    driver_error: str | None = None
    skipped: str | None = None
    timed_out: bool = False
    attempts: list[str] = field(default_factory=list)       # commands the DRIVER ran for the agent (script steps)


@dataclass
class SessionContext:
    item: Item
    session: Session
    index: int
    subject: Subject
    sb: EvalSandbox
    seed: int = 0
    timeout: float = 300.0
    max_cost: float | None = None
    last: bool = True

    @property
    def n_sessions(self) -> int:
        return len(self.item.sessions)


# --------------------------------------------------------------------------- shared prompt pieces


def answer_instruction(item: Item, surface: str) -> str:
    if item.answer_schema is None:
        return ""
    sch = json.dumps(item.answer_schema, separators=(",", ":"))
    how = {"cli": "run `togi answer --json '<object>'`", "run": "call `togi_answer({...})` in your script (or run `togi answer --json '<object>'`)",
           "mcp": "call the `answer` tool (its arguments are the answer object)"}[surface]
    return f" When you are done, record your final answer: {how}, with an object valid for this JSON Schema: {sch}. If it is refused, fix the fields it names and try again."


def examples_text(commands: list[dict]) -> str:
    lines = []
    for c in commands:
        lines += [f"  {e}" for e in c.get("examples", [])]
    return "\n".join(lines)


def claude_system_prompt(ctx: SessionContext, commands: list[dict]) -> str:
    h, item, surface = ctx.subject.harness, ctx.item, ctx.subject.surface
    bundle = ctx.sb.bundle
    if surface == "mcp":
        pre = f"mcp__{stone.MCP_SERVER_KEY}__"
        text = (f"You have MCP tools whose names start with `{pre}`. "
                + (f"Call `{pre}{bundle}_skill` first (no arguments) to learn the {bundle} tools, then use the other tools. " if h["skills"] != "none" else "")
                + f"Use only those tools. A tool that needs a person's approval returns an approval id: report it, never try to approve it yourself.")
    else:
        text = {"none": "Use only `togi` commands.", "index": stone.SYSTEM_PROMPT,
                "full": stone.SYSTEM_PROMPT + " Then run `togi skill <bundle>` for each bundle you use."}[h["skills"]]
        text += stone.SURFACE_HINTS.get(surface, "")
    text += answer_instruction(item, surface)
    if h.get("examples"):
        text += "\nExamples:\n" + examples_text(commands)
    return text


# --------------------------------------------------------------------------- script


def latest_approval_id(sb: EvalSandbox) -> str:
    """The most recently filed approval request in the sandbox (for `{approval}` in a script step)."""
    best, best_ts = "", ""
    for f in sorted((sb.home / "approvals").glob("ap_*.json")) if (sb.home / "approvals").is_dir() else []:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if rec.get("created", "") >= best_ts:
            best, best_ts = rec["id"], rec.get("created", "")
    return best


def run_script(ctx: SessionContext) -> SessionRun:
    sb, s, item, surface = ctx.sb, ctx.session, ctx.item, ctx.subject.surface
    steps: list[str] = []
    outputs: list[str] = []
    script = list(s.script)
    if not script and not item.multi_session and item.reference_trajectory and surface == "cli":
        script = list(item.reference_trajectory)       # an item's own reference trajectory is an oracle script
    if surface == "run":
        if not s.script_run:
            return SessionRun(skipped=f"item has no script_run for surface {surface}")
        p = sb.sh("togi run -", agent=True, stdin=s.script_run)
        steps.append("togi run -")
        outputs.append(p.stdout + p.stderr)
    elif surface == "mcp":
        if not s.script_mcp:
            return SessionRun(skipped=f"item has no script_mcp for surface {surface}")
        from .. import mcp_bridge

        for c in mcp_bridge.script_calls(sb, s.script_mcp):
            steps.append(c["cmdline"])
            outputs.append(c["output"])
    else:
        if not script and s.script_answer_json is None and not (ctx.last and item.reference_answer is not None):
            return SessionRun(skipped="item has no script")
        for cmdline in script:
            if "{approval}" in cmdline:
                cmdline = cmdline.replace("{approval}", latest_approval_id(sb))
            steps.append(cmdline)
            p = sb.sh(cmdline, agent=True)
            outputs.append(p.stdout + p.stderr)
    answer_obj = s.script_answer_json if s.script_answer_json is not None else (item.reference_answer if ctx.last else None)
    if answer_obj is not None:
        p = sb.run_argv(["answer", "--json", json.dumps(answer_obj)])
        outputs.append(p.stdout)
    text = s.script_answer if s.script_answer is not None else "\n".join(outputs)
    return SessionRun(turns=len(steps), answer_text=text, steps=[{"step": i, "action": {"kind": "script", "brief": c}} for i, c in enumerate(steps)],
                      finish_reason="script", attempts=steps)


# --------------------------------------------------------------------------- claude-cli


def tools_json(sb: EvalSandbox) -> list[dict]:
    """The commands of every bundle in the sandbox as `togi tools --json` describes them (for examples and per-command tools)."""
    p = sb.run_argv(["tools", "--json"], agent=True, housekeeping=True)
    try:
        data = json.loads(p.stdout)["data"]
        return data if isinstance(data, list) else []
    except (ValueError, KeyError):
        return []


def claude_cli_command(system: str, surface: str, sb: EvalSandbox, model: str | None, max_cost: float | None) -> list[str]:
    """The exact `claude -p` call of the claude-cli provider. Nothing is started here. The Bash surfaces use the same
    confinement flags as the stone driver (`stone.BASH_TOGI_ONLY`)."""
    cmd = stone.claude_base(safe_mode=surface != "mcp") + ["--system-prompt", system]
    if surface == "mcp":
        cmd += ["--tools=", f"--allowedTools=mcp__{stone.MCP_SERVER_KEY},mcp__{stone.MCP_SERVER_KEY}__*", *stone.MCP_ISOLATION,
                "--mcp-config", json.dumps(sb.mcp_config())]
    else:
        cmd += stone.BASH_TOGI_ONLY
    if model:
        cmd += ["--model", model]
    if max_cost:
        cmd += ["--max-budget-usd", f"{max_cost:g}"]
    return cmd


def run_claude_cli(ctx: SessionContext) -> SessionRun:
    sb, subject, item = ctx.sb, ctx.subject, ctx.item
    surface = subject.surface
    commands = tools_json(sb) if subject.harness.get("examples") else []
    system = claude_system_prompt(ctx, commands)
    cmd = claude_cli_command(system, surface, sb, subject.model, ctx.max_cost)
    env = stone.claude_env(sb, surface)
    try:
        p = stone.run_group(cmd, input=ctx.session.instruction, env=env, cwd=sb.cwd, timeout=ctx.timeout)
    except FileNotFoundError:
        return SessionRun(driver_error="the `claude` CLI is not on PATH; install Claude Code or use another provider")
    except subprocess.TimeoutExpired:
        return SessionRun(finish_reason="timeout", timed_out=True, answer_text="", events=[{"step": 0, "event": "early_stop", "detail": f"claude -p exceeded {ctx.timeout:g}s"}])
    try:
        res = stone.parse_claude_output(p.stdout)
    except (ValueError, StopIteration):
        why = stone.rejected_confinement(p) if surface != "mcp" else None
        return SessionRun(driver_error=why or f"claude -p exit {p.returncode}: {(p.stderr or p.stdout).strip()[:300]}")
    if res["is_error"]:
        return SessionRun(driver_error=f"claude reported an error ({res.get('subtype')}): {res['answer'][:200]}", turns=res.get("turns"))
    return SessionRun(turns=res.get("turns"), answer_text=res["answer"], cost_usd=res.get("cost_usd"), tokens=res.get("tokens"),
                      finish_reason="claude_done")


# --------------------------------------------------------------------------- our loop (claude-step, ollama, openai-compatible)


def _cut(text: str, cap: int = MAX_OUT) -> str:
    return text if len(text) <= cap else text[:cap] + f"...[cut {len(text) - cap} chars]"


class SandboxIO:
    """What a loop does in the sandbox. The agent's own actions run as the agent session; the loop's housekeeping (task
    show/update on the model's behalf, reading the guide) runs as the loop's own session, so it is not in the agent's trajectory."""

    def __init__(self, sb: EvalSandbox, env: loops.LoopEnv, timeout: float):
        self.sb, self.env, self.timeout = sb, env, timeout
        self._ans_mark = sb.answers_mark()
        self.names: dict[str, tuple[str, list[str]]] = {}
        for c in env.commands:
            words = str(c.get("command", "")).split()
            self.names["_".join([c.get("bundle", ""), *[w.replace("-", "_") for w in words]])] = (c.get("bundle", ""), words)

    def _obs(self, p: subprocess.CompletedProcess) -> Observation:
        out = (p.stdout or "") + (("\n" + p.stderr) if p.stderr.strip() else "")
        obs = Observation(text=_cut(out.strip()), exit_code=p.returncode)
        try:
            doc = json.loads(p.stdout)
        except ValueError:
            obs.ok = p.returncode == 0
            obs.status = {3: "needs_approval", 2: "refused"}.get(p.returncode)
            return obs
        if isinstance(doc, dict):
            obs.ok = bool(doc.get("ok", p.returncode == 0))
            obs.status = doc.get("status") or ("ok" if obs.ok else "error")
            obs.code = (doc.get("error") or {}).get("code")
        else:
            obs.ok = p.returncode == 0
        return obs

    def _run(self, argv: list[str], stdin: str | None = None, housekeeping: bool = False) -> Observation:
        try:
            return self._obs(self.sb.run_argv(argv, agent=True, timeout=self.timeout, stdin=stdin, housekeeping=housekeeping))
        except subprocess.TimeoutExpired:
            return Observation(text=f"(timed out after {self.timeout:g}s)", ok=False, status="error", code="timeout")

    def run_togi(self, argv: list[str]) -> Observation:
        return self._run(argv)

    def run_script(self, src: str) -> Observation:
        return self._run(["run", "-"], stdin=src)

    def run_command(self, name: str, args: dict) -> Observation:
        if self.env.surface == "mcp":
            return self._run(["mcp", "call", self.sb.bundle if name not in self.names else self.names[name][0], name, "--args", json.dumps(args)])
        if name not in self.names:
            return Observation(text=f"no tool named {name!r}; tools: {', '.join(self.names)}", ok=False, status="error", code="unknown_command")
        bundle, words = self.names[name]
        argv = [bundle, *words]
        for k, v in args.items():
            flag = "--" + str(k).replace("_", "-")
            if isinstance(v, bool):
                if v:
                    argv.append(flag)
            elif isinstance(v, list):
                for i in v:
                    argv += [flag, str(i)]
            elif v is not None:
                argv += [flag, str(v)]
        return self._run(argv)

    def record_answer(self, obj: Any) -> tuple[bool, Observation]:
        obs = self._run(["answer", "--json", json.dumps(obj)])
        self.answer_recorded()
        return bool(obs.ok), obs

    def answer_recorded(self) -> bool:
        """True if a VALID answer reached the answers log since the last look: a script's togi_answer(), or a plain
        `togi answer` the model ran as a command, ends the job as much as the answer action does."""
        new = self.sb.answers_since(self._ans_mark)
        self._ans_mark = self.sb.answers_mark()
        return any(a.get("valid") for a in new)

    def housekeeping(self, argv: list[str]) -> Observation:
        return self._run(argv, housekeeping=True)

    def task_record(self) -> dict | None:
        return self.sb.task_record()


def build_loop_env(ctx: SessionContext, io_sb: EvalSandbox) -> loops.LoopEnv:
    sb, subject, item, h = io_sb, ctx.subject, ctx.item, ctx.subject.harness
    skill = ""
    if h["skills"] != "none":
        parts = [sb.run_argv(["skill"], agent=True, housekeeping=True).stdout.strip()]
        if h["skills"] == "full":
            for b in sb.bundles:
                parts.append(sb.run_argv(["skill", b], agent=True, housekeeping=True).stdout.strip())
        skill = "\n\n".join(p for p in parts if p)
    commands = tools_json(sb)
    stubs = ""
    if h["loop"] == "code-first":
        stubs = sb.run_argv(["tools"], agent=True, housekeeping=True).stdout
    return loops.LoopEnv(
        instruction=ctx.session.instruction, session_index=ctx.index, n_sessions=ctx.n_sessions, answer_schema=item.answer_schema,
        skill_text=skill, examples=examples_text(commands) if h["examples"] else "", stubs=stubs, commands=commands,
        surface=subject.surface, loop=h["loop"], context_policy=h["context_policy"], step_budget=h["step_budget"],
        reasoning_field=h["reasoning_field"], tools_mode="per-command" if subject.surface == "mcp" else h["tools"],
        constrained=not (subject.is_local and h["constrained_decoding"] is False),
        offer_run=subject.surface == "run" or h["loop"] == "code-first")


def run_loop(ctx: SessionContext, backend: policy.Backend | None = None) -> SessionRun:
    subject = ctx.subject
    if subject.surface == "mcp" and len(ctx.sb.bundles) > 1:
        return SessionRun(driver_error="the mcp surface serves one bundle; this item uses several")
    env = build_loop_env(ctx, ctx.sb)
    backend = backend or policy.make_backend(subject, ctx.max_cost)
    strategy = loops.strategy_for(subject.harness["loop"])
    io = SandboxIO(ctx.sb, env, min(ctx.timeout, 120.0))
    out = loops.run_loop_session(strategy, backend, env, io, seed=ctx.seed, deadline=time.monotonic() + ctx.timeout, max_cost=ctx.max_cost)
    tokens = (out.prompt_tokens + out.completion_tokens) or None
    return SessionRun(turns=out.turns, answer_text=out.transcript, cost_usd=out.cost_usd, tokens=tokens, prompt_tokens=out.prompt_tokens,
                      steps=out.steps, events=out.events, finish_reason=out.finish_reason, driver_error=out.driver_error,
                      timed_out=out.timed_out)


# --------------------------------------------------------------------------- dispatch


# tests (and extensions) can replace how a provider's backend is built without touching the loops
BACKEND_FACTORY: dict[str, Any] = {}


def run_session(ctx: SessionContext) -> SessionRun:
    p = ctx.subject.provider
    if p == "script":
        return run_script(ctx)
    if p == "claude-cli":
        return run_claude_cli(ctx)
    backend = BACKEND_FACTORY[p](ctx.subject, ctx) if p in BACKEND_FACTORY else None
    return run_loop(ctx, backend)
