"""The single execution path for every surface (cli, run, mcp): guards, approvals, journal.

    parsed args -> call() -> Result(envelope, exit_code, ...)

Nothing else executes a bundle command, so a guard added here holds everywhere.
"""
from __future__ import annotations

import os
import shlex
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

from . import approvals, journal, output, paths
from .bundle import Command, coerce_json
from .context import Context
from .errors import ArgError, EXIT_CODES, TogiError
from .registry import Loaded


MAX_HINT_CHARS = 300


@dataclass
class CallOptions:
    surface: str = "cli"
    fields: list[str] | None = None
    limit: int = output.DEFAULT_LIMIT
    offset: int = 0
    full: bool = False
    cap: bool = True            # apply the ~8 KB cap (CLI only; code mode and MCP return everything)
    paginate: bool = True
    dry_run: bool = False
    approval: str | None = None
    argv: list[str] = field(default_factory=list)  # original words, to build `more` hints
    session: str | None = None


@dataclass
class Result:
    envelope: dict
    exit_code: int
    status: str
    truncated: bool = False
    out_bytes: int = 0


def build_args(cmd: Command, raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce a JSON-ish dict (code mode, MCP) to typed args; unknown keys are an error."""
    names = {p.name for p in cmd.params}
    unknown = [k for k in raw if k not in names]
    if unknown:
        raise ArgError(f"unknown argument(s): {', '.join(unknown)}; {cmd.name} takes: "
                       f"{', '.join(p.name for p in cmd.params) or '(none)'}", code="unknown_flag",
                       flag=unknown[0], next=f"togi {cmd.bundle} {cmd.name} --help")
    out = {}
    for p in cmd.params:
        if p.name in raw:
            out[p.name] = coerce_json(p, raw[p.name])
    return out


def complete_args(cmd: Command, args: dict[str, Any]) -> dict[str, Any]:
    """Fill defaults; raise ArgError for missing required parameters."""
    full: dict[str, Any] = {}
    missing = []
    for p in cmd.params:
        if p.name in args:
            full[p.name] = args[p.name]
        elif p.required:
            missing.append(p)
        else:
            full[p.name] = p.default
    if missing:
        ex = cmd.examples[0] if cmd.examples else f"togi {cmd.bundle} {cmd.name} --help"
        names = ", ".join(f"{p.flag} ({p.type_name()})" for p in missing)
        raise ArgError(f"missing required {names}", code="missing_arg", flag=missing[0].flag,
                       next=ex if cmd.examples else ex)
    return full


def _shape(cmd: Command, supplied: dict[str, Any]) -> dict[str, str]:
    return {p.name: p.shape_name() for p in cmd.params if p.name in supplied}


def canonical_args(cmd: Command, full: dict[str, Any]) -> dict:
    return output.to_jsonable(full)


def invoke(loaded: Loaded, cmd: Command, full: dict[str, Any], surface: str, session: str, agent: str | None,
           approval_scope: str | None = None) -> Any:
    kwargs = dict(full)
    if cmd.ctx_param:
        def sink(note: dict) -> None:
            journal.append(loaded.name, {"ts": journal.now_ts(), "session": session, "agent": agent,
                                         "surface": surface, "bundle": loaded.name, "command": cmd.name,
                                         "event": "note", "note": output.to_jsonable(note)})

        state = paths.bundle_state(loaded.name) / "data"
        kwargs[cmd.ctx_param] = Context(bundle=loaded.name, surface=surface, agent=agent, state_dir=state,
                                        session=session, repo=loaded.repo, command=cmd.name, note_sink=sink,
                                        approval_scope=approval_scope)
    return output.to_jsonable(cmd.func(**kwargs))


def dry_run_data(loaded: Loaded, cmd: Command, canon: dict, gated: bool) -> dict:
    flags = []
    for p in cmd.params:
        v = canon.get(p.name)
        if v is None or (p.kind == "bool" and not v):
            continue
        if p.kind == "bool":
            flags.append(p.flag)
        elif isinstance(v, list):
            for i in v:
                flags += [p.flag, str(i)]
        else:
            flags += [p.flag, str(v)]
    return {"dry_run": True, "command": f"{loaded.name} {cmd.name}", "effect": cmd.effect, "args": canon,
            "requires_approval": gated, "would_run": shlex.join(["togi", loaded.name, *cmd.words, *flags]),
            "note": "nothing was executed" + ("; a real run will return an approval request first" if gated else "")}


def call(loaded: Loaded, cmd: Command | None, args: dict[str, Any], opts: CallOptions, *,
         attempted: str | None = None, error: ArgError | None = None) -> Result:
    """Execute one command (or report a pre-execution argument problem) with full guards + journaling."""
    t0 = time.monotonic()
    session = opts.session or journal.session_id()
    agent = paths.agent_name()
    bundle_name = loaded.name
    status = "ok"
    env: dict
    approval_used = None
    event = "call"
    extra: dict[str, Any] = {}
    canon: dict = {}
    try:
        if error is not None:
            raise error
        assert cmd is not None
        full = complete_args(cmd, args)
        canon = canonical_args(cmd, full)
        gated = loaded.requires_approval(cmd.effect)
        if opts.dry_run and cmd.effect != "read":
            env = output.ok_envelope(dry_run_data(loaded, cmd, canon, gated))
            extra["dry_run"] = True
        elif gated:
            env, approval_used = _gated(loaded, cmd, full, canon, opts, session, agent, extra)
        else:
            env = output.ok_envelope(invoke(loaded, cmd, full, opts.surface, session, agent))
    except ArgError as e:
        event = e.code if e.code in ("unknown_flag", "unknown_command") else "call"
        env = output.error_envelope("error", e.code, e.message,
                                    next=e.next or (f"togi {bundle_name} {cmd.name} --help" if cmd else f"togi skill {bundle_name}"))
        if event != "call":   # never a value: flag names and near-miss command words only
            vocab = {w for c in loaded.bundle.commands.values() for w in c.words}
            extra["attempted"] = journal.sanitize_attempt(e.flag or attempted, vocab)
    except TogiError as e:
        env = _from_togi_error(e)
    except Exception as e:  # a bug in the command: exit 4, never a traceback in the agent's context
        if os.environ.get("TOGI_DEBUG"):
            traceback.print_exc()
        env = output.error_envelope("internal", "internal_error",
                                    f"{type(e).__name__}: {e}", next=f"togi doctor  # then report this bug in {bundle_name}")
    status = "ok" if env.get("ok") else env.get("status", "error")
    # ---- shape the output for this surface
    truncated = False
    if opts.surface == "cli":
        if env.get("ok") and not extra.get("dry_run"):
            env, truncated = _shape_output(env, opts, loaded.name, cmd)
        elif opts.cap and not opts.full:   # errors and --dry-run obey the ~8 KB cap too
            env, truncated = output.fit_any(env, output.MAX_BYTES,
                                            lambda off: _more_hint(opts.argv, loaded.name, None, opts))
    out_bytes = len(output.dumps(env).encode("utf-8"))
    # ---- journal (types only, never values)
    entry = journal.make_entry(
        surface=opts.surface, bundle=bundle_name, command=cmd.name if cmd else None, event=event,
        args_shape=_shape(cmd, args) if cmd else {},
        args_hash_=journal.args_hash(output.to_jsonable(args)) if (cmd and cmd.effect == "read") else None,
        ok=bool(env.get("ok")), status=status,
        error_code=(env.get("error") or {}).get("code") if not env.get("ok") else None,
        duration_ms=int((time.monotonic() - t0) * 1000), out_bytes=out_bytes,
        out_tokens_est=output.est_tokens(out_bytes), session=session, agent=agent,
        truncated=True if truncated else None, approval=approval_used,
        effect=cmd.effect if cmd else None, **extra)
    journal.append(bundle_name, entry)
    return Result(env, EXIT_CODES.get(status, 1) if status != "ok" else 0, status, truncated, out_bytes)


def _from_togi_error(e: TogiError) -> dict:
    extra = {}
    if isinstance(getattr(e, "approval", None), dict):
        extra["approval"] = e.approval
    return output.error_envelope(e.status, e.code, e.message, next=e.next, **extra)


def _gated(loaded: Loaded, cmd: Command, full: dict, canon: dict, opts: CallOptions, session: str,
           agent: str | None, extra: dict[str, Any]) -> tuple[dict, str | None]:
    """Money (and policy-gated) effects: no valid approval -> pending request, function NOT executed."""
    h = approvals.args_hash(loaded.name, cmd.name, canon)
    binding = loaded.binding()   # repo path + manifest version + locked-files digest, bound with command + args
    bh = approvals.binding_hash(binding)
    if not opts.approval:
        rec = approvals.request(loaded.name, cmd, canon, session, agent, binding)
        view = approvals.public_view(rec)
        return (output.error_envelope(
            "needs_approval", "needs_approval",
            f"{loaded.name} {cmd.name} has effect '{cmd.effect}' and needs a person's approval; nothing was executed",
            next=f"a person runs: togi approve {rec['id']}", approval=view), None)
    try:
        grant = approvals.check_for_use(opts.approval, loaded.name, cmd.name, h, bh)
    except TogiError as e:
        if e.code == "approval_pending":  # asked but the person has not decided yet
            view = approvals.public_view(approvals.load(opts.approval))
            return output.error_envelope("needs_approval", e.code, e.message, next=e.next, approval=view), None
        raise
    extra["approval_scope"] = grant["scope"]   # journaled: which money calls ran on a throwaway approver
    approvals.claim(opts.approval)  # single use: taken before the function runs
    outcome = "error"
    try:
        data = invoke(loaded, cmd, full, opts.surface, session, agent, approval_scope=grant["scope"])
        outcome = "ok"
        return output.ok_envelope(data), opts.approval
    except TogiError:
        outcome = "refused_or_error"
        raise
    finally:
        approvals.mark_consumed(opts.approval, outcome)


def _shape_output(env: dict, opts: CallOptions, bundle: str, cmd: Command | None) -> tuple[dict, bool]:
    data = env.get("data")
    start_offset = opts.offset
    shadow = {p.name for p in cmd.params} if cmd else set()
    do_page = opts.paginate and "limit" not in shadow and "offset" not in shadow
    out = dict(env)
    info: dict = {}
    if do_page:
        data, info = output.paginate(data, opts.limit, opts.offset)
    if opts.fields and "fields" not in shadow:
        data = output.project(data, opts.fields)
    out["data"] = data
    if "next_offset" in info:
        out["more"] = _more_hint(opts.argv, bundle, info["next_offset"], opts)
        out["total"] = info.get("total") or info.get("totals")
    truncated = False
    if opts.cap and not opts.full:
        out, truncated = output.fit(out, output.MAX_BYTES, lambda off: _more_hint(opts.argv, bundle, off, opts),
                                    start_offset=start_offset)
    return out, truncated


def _more_hint(argv: list[str], bundle: str, offset: int | None, opts: CallOptions) -> str:
    """`togi crowd drop list --offset 50`: the same call, next page (or --full when we cannot page)."""
    toks = list(argv)
    cleaned = []
    skip = False
    for i, t in enumerate(toks):
        if skip:
            skip = False
            continue
        if t in ("--offset",):
            skip = True
            continue
        if t.startswith("--offset="):
            continue
        cleaned.append(t)
    base = ["togi", *cleaned]
    if offset is None:
        hint, short = shlex.join(base + ["--full"]), "re-run the same command with --full"
    else:
        hint, short = shlex.join(base + ["--offset", str(offset)]), f"re-run the same command with --offset {offset}"
    return hint if len(hint) <= MAX_HINT_CHARS else short   # a hint must not carry a 30 KB argument back at the caller
