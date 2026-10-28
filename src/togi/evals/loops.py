"""Agent-loop strategies for step-policy providers (ollama, openai-compatible, claude-step). One module, one small interface:

    state in -> prompt          LoopStrategy.build_prompt(env, state)
    reply     -> next action    LoopStrategy.interpret(env, state, reply)
    action + output -> state    LoopStrategy.after(env, state, action, observation, io)

The harness (run_loop_session) owns everything else: calling the backend, executing the action through Togi in the sandbox,
accounting for tokens, step records, loop-level events and the stop rules. A strategy only decides what the model sees and
how its reply is read.

    react             baseline. The transcript grows; the model picks the next togi call or the answer.
    operator          stateless and re-entrant. Every step rebuilds a SHORT context from the TASK RECORD and the latest
                      output only; the model writes its own progress back (`done`, `next`) and the loop stores it with
                      `togi task update`. No growing transcript: the best fit for small models and long-running work.
    plan-act-verify   writes a short plan into the task record, acts, and verifies each result against the plan, revising it
                      (`verify`, `revised_plan`). The loop also verifies on its own: a failed command is a failed verification.
    code-first        prefers ONE `togi run` script that does the whole job and calls togi_answer; falls back to single calls.

`context_policy` (full | window:N | state-only) limits how much history a strategy puts in front of the model, and
`step_budget` caps the steps per session. Every step records the prompt tokens actually sent (the provider's count, or an
estimate marked as such), the action chosen and any loop-level events (plan written/revised, verification failed, invalid
reply, early stop).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .. import taskrec
from .policy import Prompt, ProviderError, Reply

OBS_CAP = 3000
OPERATOR_OBS_CAP = 1500
MAX_INVALID_STREAK = 3
REPEAT_LIMIT = 4
FORBIDDEN_WORDS = ("approve", "deny")


@dataclass
class Observation:
    text: str = ""
    ok: bool | None = None
    status: str | None = None
    code: str | None = None
    exit_code: int | None = None


@dataclass
class Action:
    kind: str                      # togi | run | command | answer | give_up | plan
    argv: list[str] | None = None
    script: str | None = None
    command: str | None = None     # per-command tool name, e.g. notes_add
    args: dict | None = None
    answer: Any = None
    plan: list[str] | None = None
    meta: dict = field(default_factory=dict)       # think, done, next, waiting, verify, revised_plan, reason
    valid: bool = True
    error: str | None = None

    def brief(self, cap: int = 160) -> str:
        if self.kind == "togi":
            s = "togi " + " ".join(self.argv or [])
        elif self.kind == "command":
            s = f"{self.command} {json.dumps(self.args or {}, separators=(',', ':'))}"
        elif self.kind == "run":
            s = "togi run <<" + " ".join((self.script or "").split())
        elif self.kind == "answer":
            s = "answer " + json.dumps(self.answer, separators=(",", ":"), default=str)
        elif self.kind == "plan":
            s = "plan " + " | ".join(self.plan or [])
        else:
            s = self.kind + (f": {self.meta.get('reason') or self.meta.get('summary')}" if (self.meta.get("reason") or self.meta.get("summary")) else "")
        return s if len(s) <= cap else s[:cap - 3] + "..."

    def as_json(self) -> dict:
        d: dict[str, Any] = {"action": self.command if self.kind == "command" else self.kind}
        for k in ("argv", "script", "args", "answer", "plan"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        d.update({k: v for k, v in self.meta.items() if v not in (None, "", [])})
        return d


@dataclass
class Exchange:
    action: Action
    obs: Observation


@dataclass
class LoopState:
    step: int = 0
    history: list[Exchange] = field(default_factory=list)
    task: dict | None = None
    plan: list[str] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    invalid_streak: int = 0
    feedback: str | None = None            # shown once on the next prompt (an invalid reply, a refused answer)
    finished: bool = False
    finish_reason: str | None = None
    answered: bool = False
    verify_failures: int = 0

    def event(self, kind: str, detail: str = "") -> None:
        self.events.append({"step": self.step, "event": kind, "detail": detail[:200]})


@dataclass
class LoopEnv:
    """What stays the same for a whole session."""
    instruction: str
    session_index: int = 0
    n_sessions: int = 1
    answer_schema: dict | None = None
    skill_text: str = ""
    examples: str = ""
    stubs: str = ""
    commands: list[dict] = field(default_factory=list)       # `togi tools --json` entries
    surface: str = "cli"
    loop: str = "react"
    context_policy: str = "full"
    step_budget: int = 20
    reasoning_field: bool = False
    tools_mode: str = "single"
    constrained: bool = True
    offer_run: bool = False


# --------------------------------------------------------------------------- the action vocabulary (schema + tools)


def _variant(kind: str, props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": {"action": {"const": kind}, **props}, "required": ["action", *required],
            "additionalProperties": False}


def command_variants(env: LoopEnv) -> list[tuple[str, dict, str]]:
    """(tool name, args schema, description) per command, for the per-command tool mode and the MCP surface."""
    out = []
    for c in env.commands:
        bundle, words = c.get("bundle", ""), str(c.get("command", "")).split()
        name = "_".join([bundle, *[w.replace("-", "_") for w in words]])
        schema = json.loads(json.dumps(c.get("input_schema") or {"type": "object", "properties": {}}))
        if c.get("effect") != "read":
            schema.setdefault("properties", {})["dry_run"] = {"type": "boolean"}
        if c.get("effect") in ("money", "admin"):
            schema.setdefault("properties", {})["approval"] = {"type": "string"}
        out.append((name, schema, f"{c.get('summary', '')} [{c.get('effect', '')}]".strip()))
    return out


def variants(env: LoopEnv, extras: dict[str, dict], required_extras: list[str], plan_only: bool = False,
             allow_plan: bool = False) -> list[tuple[str, dict, str, list[str]]]:
    """(kind, properties, description, required) for every action this step may take."""
    think = {"think": {"type": "string", "maxLength": 300}} if env.reasoning_field else {}
    ex = {**think, **extras}
    if plan_only:
        return [("plan", {**think, "plan": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8}},
                 "write a short numbered plan (at most 8 steps)", ["plan"])]
    out: list[tuple[str, dict, str, list[str]]] = []
    per_command = env.tools_mode == "per-command" or env.surface == "mcp"
    if per_command:
        for name, schema, desc in command_variants(env):
            out.append((name, {**ex, "args": schema}, desc, ["args", *required_extras]))
    else:
        out.append(("togi", {**ex, "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1}},
                    "run `togi <argv...>` (argv = the words after `togi`)", ["argv", *required_extras]))
    if env.offer_run and env.surface != "mcp":
        out.append(("run", {**ex, "script": {"type": "string"}}, "run a Python script with `togi run -`; call togi_answer({...}) in it",
                    ["script", *required_extras]))
    ans_schema = env.answer_schema or {"type": "object"}
    out.append(("answer", {**think, "answer": ans_schema}, "record the final answer; the job is done when it is accepted", ["answer"]))
    out.append(("finish", {**think, "summary": {"type": "string", "maxLength": 300}},
                "end this session without a final answer (you are waiting for a person, or nothing more can be done now)", []))
    out.append(("give_up", {**think, "reason": {"type": "string"}}, "stop because the job cannot be done", []))
    if allow_plan:
        out.append(("plan", {**think, "plan": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8}},
                    "replace the plan", ["plan"]))
    return out


def action_schema(vs: list[tuple[str, dict, str, list[str]]]) -> dict:
    return {"oneOf": [_variant(k, p, r) for k, p, _d, r in vs]}


def tools_for(vs: list[tuple[str, dict, str, list[str]]]) -> list[dict]:
    tools = []
    for k, props, desc, req in vs:
        params = {"type": "object", "properties": props, "required": [r for r in req if r != "action"]}
        tools.append({"type": "function", "function": {"name": k, "description": desc, "parameters": params}})
    return tools


# --------------------------------------------------------------------------- reading a reply


def extract_json(text: str) -> Any | None:
    """The first JSON object in a model's text: bare, in a code fence, or after some prose. None if there is none."""
    t = text.strip()
    if not t:
        return None
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*\n(.*?)```", t, re.S)
    if m:
        try:
            v = json.loads(m.group(1))
            return v if isinstance(v, dict) else None
        except ValueError:
            pass
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(t)):
            ch = t[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        v = json.loads(t[start:i + 1])
                        if isinstance(v, dict):
                            return v
                    except ValueError:
                        pass
                    break
        start = t.find("{", start + 1)
    return None


def parse_action(reply: Reply, env: LoopEnv, allowed: list[str]) -> Action:
    """A reply as an Action (valid=False with `error` if it is not one the harness can run)."""
    if reply.tool_call and reply.tool_call.get("name"):
        obj: Any = {"action": reply.tool_call["name"], **(reply.tool_call.get("arguments") or {})}
    else:
        obj = extract_json(reply.text)
    if not isinstance(obj, dict):
        return Action("invalid", valid=False, error="your reply was not a JSON object. Reply with ONE JSON object like "
                                                    '{"action":"togi","argv":["..."]}')
    kind = obj.get("action")
    if kind not in allowed:
        return Action("invalid", valid=False, error=f'"action" must be one of {", ".join(allowed)} (got {kind!r})')
    meta = {k: obj[k] for k in ("think", "done", "next", "waiting", "verify", "revised_plan", "reason", "summary") if k in obj}
    meta = {k: (v if isinstance(v, (list, dict)) else str(v)) for k, v in meta.items()}
    if kind == "togi":
        argv = obj.get("argv")
        if isinstance(argv, str):
            argv = argv.split()
        if not isinstance(argv, list) or not argv:
            return Action("invalid", valid=False, error='"argv" must be a non-empty list of strings: the words after `togi`')
        argv = [str(a) for a in argv]
        if argv[0] == "togi":
            argv = argv[1:]
        if not argv:
            return Action("invalid", valid=False, error='"argv" is empty after removing the leading "togi"')
        return Action("togi", argv=argv, meta=meta)
    if kind == "run":
        script = obj.get("script")
        if not isinstance(script, str) or not script.strip():
            return Action("invalid", valid=False, error='"script" must be the Python source to run')
        return Action("run", script=script, meta=meta)
    if kind == "answer":
        if "answer" not in obj:
            return Action("invalid", valid=False, error='an "answer" action needs an "answer" object')
        return Action("answer", answer=obj["answer"], meta=meta)
    if kind in ("give_up", "finish"):
        return Action(kind, meta=meta)
    if kind == "plan":
        plan = obj.get("plan")
        if not isinstance(plan, list) or not plan:
            return Action("invalid", valid=False, error='"plan" must be a non-empty list of short steps')
        return Action("plan", plan=[str(p) for p in plan][:8], meta=meta)
    args = obj.get("args")
    if args is None:
        args = {k: v for k, v in obj.items() if k not in ("action", "think", "done", "next", "waiting", "verify", "revised_plan")}
    if not isinstance(args, dict):
        return Action("invalid", valid=False, error='"args" must be an object')
    return Action("command", command=str(kind), args=args, meta=meta)


# --------------------------------------------------------------------------- prompt pieces


def clip(text: str, cap: int) -> str:
    return text if len(text) <= cap else text[:cap] + f"...[cut {len(text) - cap} chars]"


def base_system(env: LoopEnv, role_lines: list[str]) -> str:
    parts = [
        "You operate the `togi` command-line host to do a job, in steps. Each step you reply with ONE JSON object (an action); "
        "the harness runs it and shows you the output.",
        "Actions:",
    ]
    if env.tools_mode == "per-command" or env.surface == "mcp":
        parts.append('  {"action":"<tool name>","args":{...}}   call one tool (the names are in the tool list below)')
    else:
        parts.append('  {"action":"togi","argv":["notes","add","--text","hi"]}   run `togi notes add --text hi` (argv = the words after `togi`)')
    if env.offer_run and env.surface != "mcp":
        parts.append('  {"action":"run","script":"..."}   run a Python script with `togi run -` (bundles are modules)')
    parts.append('  {"action":"answer","answer":{...}}   record your final answer; the job is done when it is accepted')
    parts.append('  {"action":"finish","summary":"..."}   end this session without a final answer (waiting for a person, or nothing more to do now)')
    parts.append('  {"action":"give_up","reason":"..."}   stop, only if the job cannot be done')
    parts += role_lines
    parts.append("Rules: use only togi. Never approve anything yourself: when a command needs a person's approval, stop and "
                 "report the approval id. Copy numbers and names exactly from outputs; do not compute in your head what a "
                 "command can compute.")
    if env.skill_text:
        parts += ["", "TOGI GUIDE:", env.skill_text.strip()]
    if env.stubs:
        parts += ["", "COMMANDS (Python stubs):", env.stubs.strip()]
    if env.examples:
        parts += ["", "EXAMPLES:", env.examples.strip()]
    if env.answer_schema is not None:
        parts += ["", "FINAL ANSWER: when you are done, use the answer action with an object valid for this JSON Schema:",
                  json.dumps(env.answer_schema, separators=(",", ":"))]
    else:
        parts += ["", 'FINAL ANSWER: when you are done, use the answer action with an object such as {"text": "<what you found>"}.']
    if env.tools_mode == "per-command" or env.surface == "mcp":
        parts += ["", "TOOLS: " + "; ".join(f"{n} ({d})" for n, _s, d in command_variants(env))]
    return "\n".join(parts)


def obs_block(obs: Observation, cap: int) -> str:
    return clip(obs.text or "(no output)", cap)


def history_window(env: LoopEnv, st: LoopState) -> tuple[list[Exchange], int]:
    """The exchanges the model may see under the context policy, and how many earlier ones were left out."""
    cp = env.context_policy
    h = st.history
    if cp == "full":
        return h, 0
    n = 1 if cp == "state-only" else int(cp.split(":")[1])
    return h[-n:], max(0, len(h) - n)


def transcript_messages(env: LoopEnv, st: LoopState, first_user: str, cap: int = OBS_CAP) -> list[dict]:
    msgs = [{"role": "user", "content": first_user}]
    shown, omitted = history_window(env, st)
    if omitted:
        msgs.append({"role": "user", "content": f"(your {omitted} earlier steps are not shown; rely on the outputs below)"})
    for ex in shown:
        msgs.append({"role": "assistant", "content": json.dumps(ex.action.as_json(), separators=(",", ":"), default=str)})
        msgs.append({"role": "user", "content": "OUTPUT:\n" + obs_block(ex.obs, cap)})
    if st.feedback:
        msgs.append({"role": "user", "content": "NOTE: " + st.feedback})
    # consecutive same-role messages are merged: several backends reject them
    merged: list[dict] = []
    for m in msgs:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1] = {"role": m["role"], "content": merged[-1]["content"] + "\n\n" + m["content"]}
        else:
            merged.append(dict(m))
    return merged


def first_user(env: LoopEnv) -> str:
    where = f" (session {env.session_index + 1} of {env.n_sessions}; you start with no memory of earlier sessions)" if env.n_sessions > 1 else ""
    return f"TASK{where}: {env.instruction}"


# --------------------------------------------------------------------------- strategies


class LoopStrategy:
    name = "base"
    default_context = "full"
    extras: dict[str, dict] = {}
    required_extras: list[str] = []

    # -- state in -> prompt
    def vocabulary(self, env: LoopEnv, st: LoopState) -> list[tuple[str, dict, str, list[str]]]:
        return variants(env, self.extras, self.required_extras)

    def role_lines(self, env: LoopEnv) -> list[str]:
        return []

    def messages(self, env: LoopEnv, st: LoopState) -> list[dict]:
        return transcript_messages(env, st, first_user(env))

    def build_prompt(self, env: LoopEnv, st: LoopState) -> Prompt:
        vs = self.vocabulary(env, st)
        return Prompt(system=base_system(env, self.role_lines(env)), messages=self.messages(env, st),
                      schema=action_schema(vs) if env.constrained else None,
                      tools=None if env.constrained else tools_for(vs))

    # -- reply -> next action
    def interpret(self, env: LoopEnv, st: LoopState, reply: Reply) -> Action:
        allowed = [v[0] for v in self.vocabulary(env, st)]
        return parse_action(reply, env, allowed)

    # -- action + output -> state
    def start(self, env: LoopEnv, st: LoopState, io: Any) -> None:
        """Called once at the start of a session."""

    def after(self, env: LoopEnv, st: LoopState, action: Action, obs: Observation, io: Any) -> None:
        """Called after an action ran."""


class React(LoopStrategy):
    name = "react"
    default_context = "full"


class Operator(LoopStrategy):
    """Every step is a fresh, short context: the task record + the instruction + the latest output. Memory is the record."""
    name = "operator"
    default_context = "state-only"
    extras = {"done": {"type": "string", "maxLength": 250}, "next": {"type": "string", "maxLength": 500}}
    required_extras = ["done", "next"]

    def role_lines(self, env: LoopEnv) -> list[str]:
        return ["You have NO memory of earlier steps or sessions. The TASK RECORD you are shown each step is your memory: with "
                'every command also give "done" (what the previous step achieved, one line) and "next" (what to do next); the '
                "harness saves them with `togi task update`. If a person's approval is needed, put what you wait for in the "
                "answer or next."]

    def start(self, env: LoopEnv, st: LoopState, io: Any) -> None:
        rec = io.task_record()
        if rec is None:
            goal = " ".join(env.instruction.split())[: taskrec.LIMITS["goal"]]
            io.housekeeping(["task", "start", "--goal", goal])
            st.event("task_started", goal[:80])
        st.task = io.task_record()

    def messages(self, env: LoopEnv, st: LoopState) -> list[dict]:
        rec = st.task
        lines = ["TASK RECORD (your memory):", taskrec.text_of(rec), "", f"INSTRUCTION: {env.instruction}"]
        if env.n_sessions > 1:
            lines.append(f"(session {env.session_index + 1} of {env.n_sessions}; earlier sessions are only in the record above)")
        lines.append(f"STEP {st.step + 1} of at most {env.step_budget}.")
        shown, omitted = history_window(env, st)
        for ex in shown:
            lines.append(f"LAST ACTION: {ex.action.brief(200)}")
            lines.append("LAST OUTPUT:\n" + obs_block(ex.obs, OPERATOR_OBS_CAP))
        if st.feedback:
            lines.append("NOTE: " + st.feedback)
        lines.append('Reply with ONE JSON action. Include "done" and "next" so the record stays true.')
        return [{"role": "user", "content": "\n".join(lines)}]

    def after(self, env: LoopEnv, st: LoopState, action: Action, obs: Observation, io: Any) -> None:
        if action.kind in ("togi", "run", "command"):
            argv = ["task", "update"]
            done = str(action.meta.get("done") or "")[: taskrec.LIMITS["done"]]
            nxt = str(action.meta.get("next") or "")[: taskrec.LIMITS["next"]]
            waiting = obs.status == "needs_approval"
            if done:
                argv += ["--done", done]
            if nxt:
                argv += ["--next", nxt]
            if waiting:
                argv += ["--waiting", "a person's approval (the last command returned needs_approval)"]
            if len(argv) > 2:
                io.housekeeping(argv)
                st.event("record_updated", (done or nxt)[:80])
        elif action.kind in ("answer", "finish") and (st.answered or action.kind == "finish"):
            note = str(action.meta.get("summary") or "answered")[: taskrec.LIMITS["done"]]
            io.housekeeping(["task", "update", "--done", note])
            st.event("record_updated", note[:80])
        st.task = io.task_record()


class PlanActVerify(LoopStrategy):
    name = "plan-act-verify"
    default_context = "window:6"
    extras = {"verify": {"enum": ["ok", "failed", "revise"]}, "revised_plan": {"type": "array", "items": {"type": "string"}, "maxItems": 8}}
    required_extras = []

    def vocabulary(self, env: LoopEnv, st: LoopState) -> list[tuple[str, dict, str, list[str]]]:
        if not st.plan:
            return variants(env, {}, [], plan_only=True)
        return variants(env, self.extras, [])

    def role_lines(self, env: LoopEnv) -> list[str]:
        return ["First write a short plan (action plan). Then act one command at a time. After each output, check it against the "
                'plan: put "verify":"ok" if it did what the plan step needed, "failed" if not, or "revise" (with '
                '"revised_plan": [...]) if the plan must change.']

    def messages(self, env: LoopEnv, st: LoopState) -> list[dict]:
        if not st.plan:
            text = first_user(env) + "\nWrite your plan now (action plan: 2 to 8 short steps)."
            if st.feedback:
                text += "\nNOTE: " + st.feedback
            return [{"role": "user", "content": text}]
        plan = "\n".join(f"  {i + 1}. {p}" for i, p in enumerate(st.plan))
        head = (f"{first_user(env)}\n\nTASK RECORD (kept by togi; earlier sessions are only here):\n{taskrec.text_of(st.task)}\n\n"
                f"YOUR PLAN:\n{plan}\n\nCommands run this session: {len(st.history)}.")
        msgs = transcript_messages(env, st, head)
        return msgs

    def start(self, env: LoopEnv, st: LoopState, io: Any) -> None:
        st.task = io.task_record()
        if st.task and st.task.get("next", "").startswith("PLAN:"):
            st.plan = [p.strip() for p in re.split(r"\s*\d+[.)]\s*", st.task["next"][5:]) if p.strip()]
            st.event("plan_resumed", f"{len(st.plan)} steps from the task record")

    def _store_plan(self, st: LoopState, io: Any, env: LoopEnv, revised: bool) -> None:
        if io.task_record() is None:
            io.housekeeping(["task", "start", "--goal", " ".join(env.instruction.split())[: taskrec.LIMITS["goal"]]])
        text = "PLAN: " + " ".join(f"{i + 1}) {p}" for i, p in enumerate(st.plan))
        io.housekeeping(["task", "update", "--next", text[: taskrec.LIMITS["next"]]])
        st.event("plan_revised" if revised else "plan_written", f"{len(st.plan)} steps")

    def after(self, env: LoopEnv, st: LoopState, action: Action, obs: Observation, io: Any) -> None:
        if action.kind == "plan":
            st.plan = list(action.plan or [])
            self._store_plan(st, io, env, revised=False)
            st.task = io.task_record()
            return
        if action.kind not in ("togi", "run", "command"):
            return
        failed_by_loop = obs.ok is False
        said = str(action.meta.get("verify") or "")
        if failed_by_loop or said == "failed":
            st.verify_failures += 1
            st.event("verification_failed", ("the command failed: " if failed_by_loop else "the model judged it failed: ") + (obs.code or obs.text[:60]))
            st.feedback = "VERIFY: the last command did not do what the plan needs. Fix it or revise the plan."
        else:
            done = action.brief(120)
            io.housekeeping(["task", "update", "--done", done[: taskrec.LIMITS["done"]]])
        revised = action.meta.get("revised_plan")
        if said == "revise" and isinstance(revised, list) and revised:
            st.plan = [str(p) for p in revised][:8]
            self._store_plan(st, io, env, revised=True)
        st.task = io.task_record()


class CodeFirst(LoopStrategy):
    name = "code-first"
    default_context = "full"

    def role_lines(self, env: LoopEnv) -> list[str]:
        return ["PREFER ONE SCRIPT: write a single Python script for `togi run` that does the whole job (bundles are modules: "
                "see the stubs; print what you need) and ends by calling togi_answer({...}) with your final answer. If the "
                "script fails, read the error and send a corrected script; if code is impractical, use single togi calls."]


STRATEGIES: dict[str, type[LoopStrategy]] = {"react": React, "operator": Operator, "plan-act-verify": PlanActVerify,
                                             "code-first": CodeFirst}


def strategy_for(name: str) -> LoopStrategy:
    return STRATEGIES[name]()


# --------------------------------------------------------------------------- one session


@dataclass
class SessionOutcome:
    steps: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    turns: int = 0
    finish_reason: str = ""
    answered: bool = False
    transcript: str = ""
    cost_usd: float | None = None
    prompt_tokens: int = 0
    prompt_tokens_est: int = 0
    completion_tokens: int = 0
    driver_error: str | None = None
    timed_out: bool = False


def run_loop_session(strategy: LoopStrategy, backend: Any, env: LoopEnv, io: Any, *, seed: int = 0,
                     deadline: float | None = None, max_cost: float | None = None) -> SessionOutcome:
    """Drive one session: build prompt -> ask the backend -> read the action -> run it -> update state, until the answer is
    accepted, the model gives up, a stop rule fires, or `step_budget` is spent."""
    st = LoopState()
    out = SessionOutcome()
    texts: list[str] = []
    strategy.start(env, st, io)
    mode = "schema" if env.constrained else "tools"
    for i in range(env.step_budget):
        st.step = i
        if deadline is not None and time.monotonic() > deadline:
            st.event("early_stop", "wall-clock limit")
            st.finish_reason, out.timed_out = "timeout", True
            break
        prompt = strategy.build_prompt(env, st)
        st.feedback = None
        try:
            reply = backend.complete(prompt, seed=seed + i, mode=mode)
        except ProviderError as e:
            out.driver_error = f"{e.kind}: {e.message}"
            st.finish_reason = "driver_error"
            break
        out.turns += 1
        est = prompt.est_tokens()
        sent = reply.prompt_tokens if reply.prompt_tokens is not None else est
        out.prompt_tokens += sent
        out.prompt_tokens_est += est
        out.completion_tokens += reply.completion_tokens or 0
        if reply.cost_usd is not None:
            out.cost_usd = (out.cost_usd or 0.0) + reply.cost_usd
        rec: dict[str, Any] = {"step": i, "prompt_tokens": sent, "prompt_tokens_est": est,
                               "prompt_tokens_estimated": reply.prompt_tokens is None, "prompt_chars": prompt.chars(),
                               "completion_tokens": reply.completion_tokens, "cost_usd": reply.cost_usd,
                               "wall_s": round(reply.wall_s, 3)}
        action = strategy.interpret(env, st, reply)
        n_events = len(st.events)
        if not action.valid:
            st.invalid_streak += 1
            st.feedback = action.error
            st.event("invalid_reply", action.error or "")
            rec.update(action={"kind": "invalid"}, valid=False, events=st.events[n_events:])
            out.steps.append(rec)
            if st.invalid_streak >= MAX_INVALID_STREAK:
                st.event("early_stop", f"{MAX_INVALID_STREAK} invalid replies in a row")
                st.finish_reason = "invalid_replies"
                rec["events"] = st.events[n_events:]
                break
            continue
        st.invalid_streak = 0
        obs = Observation()
        if action.kind == "give_up":
            st.finish_reason = "gave_up"
            texts.append("GAVE UP: " + str(action.meta.get("reason", "")))
        elif action.kind == "finish":
            st.finish_reason = "finished"
            texts.append("FINISHED: " + str(action.meta.get("summary", "")))
        elif action.kind == "answer":
            valid, obs = io.record_answer(action.answer)
            texts.append("ANSWER: " + json.dumps(action.answer, default=str))
            if valid:
                st.answered, st.finish_reason = True, "answered"
            else:
                st.feedback = "your answer was not accepted: " + obs.text[:300]
                st.event("answer_rejected", obs.text[:120])
        elif action.kind == "plan":
            pass
        elif action.kind == "run":
            obs = io.run_script(action.script or "")
        elif action.kind == "command":
            obs = io.run_command(action.command or "", action.args or {})
        else:
            obs = io.run_togi(action.argv or [])
        if action.kind in ("togi", "run", "command"):
            texts.append(obs.text)
            if hasattr(io, "answer_recorded") and io.answer_recorded():
                st.answered, st.finish_reason = True, "answered"
                st.event("answered_by_command", action.brief(80))
        if action.kind != "plan":
            st.history.append(Exchange(action, obs))
        strategy.after(env, st, action, obs, io)
        rec.update(action={"kind": action.kind, "brief": action.brief(), **({"think": action.meta["think"]} if action.meta.get("think") else {})},
                   valid=True, ok=obs.ok, obs=obs.text[:200])
        if max_cost is not None and (out.cost_usd or 0) > max_cost:
            st.event("early_stop", f"cost cap ${max_cost:g}")
            st.finish_reason = "cost_cap"
        elif len(st.history) >= REPEAT_LIMIT and action.kind in ("togi", "run", "command") and all(
                e.action.brief() == action.brief() and e.obs.text == obs.text for e in st.history[-REPEAT_LIMIT:]):
            st.event("early_stop", f"the same action and output {REPEAT_LIMIT} times")
            st.finish_reason = "repeated_action"
        rec["events"] = st.events[n_events:]
        out.steps.append(rec)
        if st.finish_reason:
            break
    else:
        st.event("early_stop", f"step budget of {env.step_budget} spent")
        st.finish_reason = "step_budget"
        if out.steps:
            out.steps[-1].setdefault("events", []).append(st.events[-1])
    out.events = st.events
    out.finish_reason = st.finish_reason or "stopped"
    out.answered = st.answered
    out.transcript = "\n".join(texts)
    return out
