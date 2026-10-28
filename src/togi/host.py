"""The single execution path for every surface (cli, run, mcp): guards, approvals, journal.

    parsed args -> call() -> Result(envelope, exit_code, ...)

Nothing else executes a bundle command, so a guard added here holds everywhere.
"""
from __future__ import annotations

import contextlib
import io
import os
import shlex
import sys
import threading
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
PRINT_CAP = 4096          # what a command may print to stdout while it runs; the rest is dropped
EVAL_RECORD_ENV = "TOGI_EVAL_RECORD"   # the eval harness sets it in its sandbox: journal entries then also hold the call
ARGS_PREVIEW = 400                     # arguments and a preview of the output (for trajectory analysis). Honoured ONLY inside a
OUT_PREVIEW = 600                      # stone/eval sandbox (approvals.sandbox_optin), so a real journal never holds values.


def eval_recording() -> bool:
    """True inside an eval/stone sandbox whose harness asked for full call records (see EVAL_RECORD_ENV)."""
    return os.environ.get(EVAL_RECORD_ENV) == "1" and approvals.sandbox_optin()


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


class CappedStdout(io.TextIOBase):
    """Stands in for `sys.stdout` while a bundle command runs: keeps the first `cap` characters and counts the rest. A
    command that prints must not be able to flood (or corrupt) the result on stdout, which belongs to the envelope."""

    def __init__(self, cap: int = PRINT_CAP):
        self.cap, self.kept, self.total, self.parts = cap, 0, 0, []

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        n = len(text)
        self.total += n
        room = self.cap - self.kept
        if room > 0:
            self.parts.append(text[:room])
            self.kept += min(n, room)
        return n

    def flush(self) -> None:
        pass

    def text(self) -> str:
        return "".join(self.parts)


class _StreamRouter(io.TextIOBase):
    """`sys.stdout` / `sys.stderr` while any command runs: writes from a thread that is inside `capture_stdout` /
    `capture_stderr` go to that thread's sink, everything else (the host's own output, other threads) goes to the real
    stream. Needed because the MCP bridge runs tool calls on worker threads at the same time, and
    `contextlib.redirect_stdout` swaps one global."""

    def __init__(self, saved):
        self.saved = saved
        self.local = threading.local()

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        sink = getattr(self.local, "sink", None)
        return sink.write(text) if sink is not None else self.saved.write(text)

    def flush(self) -> None:
        if getattr(self.local, "sink", None) is None:
            self.saved.flush()

    def __getattr__(self, name: str):
        return getattr(self.saved, name)


_StdoutRouter = _StreamRouter    # (r4 name)
_ROUTE_LOCK = threading.Lock()
_ROUTES: dict[str, dict[str, Any]] = {"stdout": {"depth": 0, "router": None, "saved": None},
                                      "stderr": {"depth": 0, "router": None, "saved": None}}


@contextlib.contextmanager
def _capture(stream: str, sink):
    route = _ROUTES[stream]
    with _ROUTE_LOCK:
        if route["depth"] == 0:
            route["saved"] = getattr(sys, stream)
            route["router"] = _StreamRouter(getattr(sys, stream))
            setattr(sys, stream, route["router"])
        route["depth"] += 1
        router = route["router"]
    previous = getattr(router.local, "sink", None)
    router.local.sink = sink
    try:
        yield
    finally:
        router.local.sink = previous
        with _ROUTE_LOCK:
            route["depth"] -= 1
            if route["depth"] == 0:
                if getattr(sys, stream) is router:
                    setattr(sys, stream, route["saved"])
                route["router"] = route["saved"] = None


def capture_stdout(sink):
    """Send everything the current thread writes to `sys.stdout` into `sink` while the block runs."""
    return _capture("stdout", sink)


def capture_stderr(sink):
    """Send everything the current thread writes to `sys.stderr` into `sink` while the block runs (r5: a bundle's stderr
    is capped like its stdout; it used to go straight to the caller's stderr, unbounded)."""
    return _capture("stderr", sink)


def note_shape(note: dict) -> dict:
    """A journal note for a write/money/admin command: its keys and the TYPE of each value, never the value."""
    def kind(v: Any) -> str:
        for t, name in ((bool, "bool"), (int, "int"), (float, "float"), (str, "str"), (list, "list"), (tuple, "list"),
                        (dict, "dict")):
            if isinstance(v, t):
                return name
        return "null" if v is None else type(v).__name__

    return {str(k): kind(v) for k, v in note.items()}


def invoke(loaded: Loaded, cmd: Command, full: dict[str, Any], surface: str, session: str, agent: str | None,
           approval_scope: str | None = None, extra: dict[str, Any] | None = None) -> Any:
    kwargs = dict(full)
    if cmd.ctx_param:
        def sink(note: dict) -> None:
            # a note from a read command is stored as written; one from a write/money/admin command records keys and
            # value types only (a note is the easiest place for a command to leak an amount or an id by accident)
            shaped = output.to_jsonable(note) if cmd.effect == "read" else note_shape(note)
            journal.append(loaded.name, {"ts": journal.now_ts(), "session": session, "agent": agent,
                                         "surface": surface, "bundle": loaded.name, "command": cmd.name,
                                         "event": "note", "note": shaped})

        state = paths.bundle_state(loaded.name) / "data"
        kwargs[cmd.ctx_param] = Context(bundle=loaded.name, surface=surface, agent=agent, state_dir=state,
                                        session=session, repo=loaded.repo, command=cmd.name, note_sink=sink,
                                        approval_scope=approval_scope)
    printed, printed_err = CappedStdout(), CappedStdout()
    try:
        with capture_stdout(printed), capture_stderr(printed_err):
            result = cmd.func(**kwargs)
    finally:
        # stdout is the envelope's, and stderr is the caller's channel for Togi's own notes: what a command printed to either
        # while it ran is bounded (first PRINT_CAP characters of each) and re-emitted on stderr with a note
        for label, sink, key in (("stdout", printed, "printed_bytes"), ("stderr", printed_err, "printed_err_bytes")):
            if not sink.total:
                continue
            if extra is not None:
                extra[key] = sink.total
            cut = f" (first {sink.kept} shown)" if sink.total > sink.kept else ""
            note = (f"[togi: {loaded.name} {cmd.name} printed {sink.total} characters to {label}{cut}; "
                    + ("stdout is reserved for the result]" if label == "stdout" else "bounded like stdout]"))
            try:
                sys.stderr.write(f"{note}\n{sink.text()}\n")
            except (OSError, ValueError):
                pass
    return output.to_jsonable(result)


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
            env = output.ok_envelope(invoke(loaded, cmd, full, opts.surface, session, agent, extra=extra))
    except ArgError as e:
        event = e.code if e.code in ("unknown_flag", "unknown_command") else "call"
        env = output.error_envelope("error", e.code, e.message,
                                    next=e.next or (f"togi {bundle_name} {cmd.name} --help" if cmd else f"togi skill {bundle_name}"))
        if event != "call":   # never a value: flag names and near-miss command words only
            vocab = {w for c in loaded.bundle.commands.values() for w in c.words}
            known = {p.name.replace("_", "-") for c in loaded.bundle.commands.values() for p in c.params}
            known |= {"no-" + k for k in known}     # `--no-verbose` negates a default-true bool
            extra["attempted"] = journal.sanitize_attempt(e.flag or attempted, vocab, known)
            if event == "unknown_flag":
                extra["near"] = journal.nearest_flag(e.flag, known)   # a KNOWN flag name, never the typed text
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
        if opts.cap and not opts.full and output._size(env) > output.MAX_BYTES:
            env, truncated = output.hard_cap(env, output.MAX_BYTES), True   # the final guarantee, whatever got here
    out_bytes = len(output.dumps(env).encode("utf-8"))
    if eval_recording():       # the harness's own sandbox only: values stay out of every real journal
        try:
            extra["args_full"] = output.dumps(output.to_jsonable(args))[:ARGS_PREVIEW]
        except (TypeError, ValueError):
            pass
        extra["out_preview"] = output.dumps(env)[:OUT_PREVIEW]
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


def require_lock(loaded: Loaded) -> None:
    """The money path checks the lock (SPEC section 2), at request time and again at use time.

    A lock file that exists must verify: every locked file matches it, no symlink hides locked code, it is the lock the
    latest signed `lock` audit entry recorded, and the audit log verifies. With NO lock file the bundle must say so
    (`[policy] require_lock = false`); by default a bundle that has locked tiers needs one. Without this a bundle's
    locked money code could be edited and still run, because nothing on this path ever looked at the lock.

    Not applied inside the stone's own sandbox run (approvals.sandbox_optin): a throwaway home has no audit key to
    anchor a lock with, and nothing there is real."""
    if approvals.sandbox_optin():
        return
    from . import audit

    loaded.refresh()
    st = audit.lock_state(loaded.repo, loaded.manifest)
    if st["state"] == "verified" or (st["state"] == "absent" and not loaded.manifest.lock_required()):
        return
    if st["state"] == "absent":
        raise TogiError("not_locked", f"{loaded.name} has locked files but has never been locked (no "
                                      ".togi/locked.sha256), so its gated commands are refused",
                        next=f"a person runs, after reading the locked files: togi lock {loaded.name}   "
                             "(a bundle with no locked code says [policy] require_lock = false)", status="refused")
    raise TogiError("lock_check_failed", f"{loaded.name}'s lock does not verify ({st['detail']}), so its gated "
                                         "commands are refused",
                    next=f"review the change; a person re-locks with: togi lock {loaded.name}", status="refused")


def require_gated_code(loaded: Loaded) -> None:
    """Every importable file behind a gated command is locked or reshape (SPEC section 2, r5).

    The lock hashes the files a manifest names; what protects a money command is that the CODE it runs is among them. So,
    wherever the lock is required: each importable file (`.py`, `.so`/`.pyd`, `.pyc`, `.pth`, package directories) under the
    top-level package of every gated command, every symlink there, and every repo module this process has loaded, must be
    in the locked or reshape tier. Reshape code is allowed (changing it already needs the real approver); anything untiered
    is code nobody signed for, and the money path refuses (`gated_code_unlocked`) at request time and again at use time.
    Not applied where no lock is required (`[policy] require_lock` / no locked tiers) or inside the stone's own sandbox,
    for the reasons `require_lock` is not."""
    if approvals.sandbox_optin():
        return
    loaded.refresh()
    man = loaded.manifest
    if not man.lock_required():
        return
    from . import codeset

    problems = codeset.coverage_problems(loaded.repo, man, codeset.gated_roots_live(loaded.repo, loaded.bundle, man),
                                         codeset.loaded_repo_files(loaded.repo))
    if problems:
        raise approvals.gated_code_error(loaded.name, problems)


def _gated(loaded: Loaded, cmd: Command, full: dict, canon: dict, opts: CallOptions, session: str,
           agent: str | None, extra: dict[str, Any]) -> tuple[dict, str | None]:
    """Money (and policy-gated) effects: no valid approval -> pending request, function NOT executed."""
    h = approvals.args_hash(loaded.name, cmd.name, canon)
    binding = loaded.binding()   # repo path + manifest version + locked-files digest, bound with command + args
    bh = approvals.binding_hash(binding)
    if not opts.approval:
        require_lock(loaded)     # no request is even filed for a bundle whose locked code does not verify
        require_gated_code(loaded)   # ... or whose gated code is not all locked or reshape
        rec = approvals.request(loaded.name, cmd, canon, session, agent, binding)
        view = approvals.public_view(rec)
        return (output.error_envelope(
            "needs_approval", "needs_approval",
            f"{loaded.name} {cmd.name} has effect '{cmd.effect}' and needs a person's approval; nothing was executed",
            next=f"a person runs: togi approve {rec['id']}", approval=view), None)
    try:
        grant = approvals.check_for_use(opts.approval, loaded.name, cmd.name, h, bh,
                                        allow_test=approvals.test_approvals_allowed(loaded.manifest))
    except TogiError as e:
        if e.code == "approval_pending":  # asked but the person has not decided yet
            view = approvals.public_view(approvals.load(opts.approval))
            return output.error_envelope("needs_approval", e.code, e.message, next=e.next, approval=view), None
        raise
    require_lock(loaded)         # ... and again at use time, before the approval is spent
    require_gated_code(loaded)
    extra["approval_scope"] = grant["scope"]   # journaled: which money calls ran on a throwaway approver
    approvals.claim(opts.approval)  # single use: recorded (outside $TOGI_HOME) before the function runs
    outcome = "error"
    try:
        data = invoke(loaded, cmd, full, opts.surface, session, agent, approval_scope=grant["scope"], extra=extra)
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
