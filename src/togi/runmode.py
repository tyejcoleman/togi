"""`togi run`: code mode. A script calls bundles as Python modules; every call still goes through the host.

    parent (this process)                         child (python -I, scrubbed env, temp cwd)
    ---------------------                         -----------------------------------------
    registry + guards + journal  <-- JSON lines -- crowd.drop_status(drop="d1")
    executes the command         --- JSON lines -> returns data / raises TogiError / NeedsApproval

Only what the script prints is returned. Three layers keep a script from reaching what it should not:

  1. the host re-execs itself with a scrubbed environment before starting the child, so its own visible
     environment (`ps -E`, `/proc/<pid>/environ`, `sysctl kern.procargs2`) holds nothing secret;
     `[policy].run_env` values are handed to the re-exec'd host over a pipe, never left in the environment;
  2. the child gets a scrubbed environment too (plus TOGI_HOME/SESSION/AGENT so a nested `togi` stays in the
     same home) and a temp working directory;
  3. on macOS the child runs under a `sandbox-exec` profile (no network, no reads of the bundle repos,
     $TOGI_HOME or the user's home, writes only to its temp dir); elsewhere it runs unsandboxed and the
     result says `sandboxed: false`.

Honest limit: the child is a same-user process; it is only as isolated as that profile (README).
"""
from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import answers, cli, host, output, paths, registry, runreg, sandbox, taskrec
from .cli import Reply, parse_flags, _mode, _usage
from .errors import ArgError, TogiError

REEXEC_FLAG = "TOGI_RUN_SCRUBBED"     # set on the re-exec'd host: do not scrub twice
ENV_FD = "TOGI_RUN_ENV_FD"            # inheritable pipe carrying [policy].run_env values
HOST_ENV_ALLOW = ("PATH", "HOME", "TMPDIR", "LANG")
MAX_OOB_BYTES = 8000                  # a pipe buffer is at least 16 KB; keep the payload well inside it

DEFAULT_TIMEOUT = 60.0
DEFAULT_MAX_OUTPUT = 8192
FULL_MAX_OUTPUT = 1_000_000
STDERR_CAP = 4096
MAX_LINE = 1_000_000          # one JSON request line from the child; more than this and the child is stopped
MAX_MESSAGE = 1000            # a child's error message, as the host reports it


@dataclass
class RunOutcome:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    truncated: bool = False
    fatal: dict | None = None
    approvals: list[dict] = field(default_factory=list)
    calls: int = 0
    sandboxed: bool = False
    sandbox_reason: str | None = None
    sandbox_level: str | None = None     # "strict" | "standard" when sandboxed
    host_env_scrubbed: bool = False


def _approver_hidden() -> tuple[str, ...]:
    """The approver home (passphrase record, state and signing key) is hidden from the child like $TOGI_HOME."""
    try:
        return (str(paths.approver_home()),)
    except TogiError:
        return ()


def scrubbed_env(tmpdir: str) -> dict[str, str]:
    """The child's whole environment: PATH/HOME/TMPDIR/LANG, `TOGI_RUN` (so `togi approve` knows it was started
    from code mode) and the host's TOGI_HOME / TOGI_SESSION / TOGI_AGENT, so a nested `togi` call lands in the
    SAME home (e.g. a stone sandbox) instead of silently falling back to the user's real ~/.togi."""
    from . import journal

    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", tmpdir),
        "TMPDIR": tmpdir,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "TOGI_RUN": "1",
        "TOGI_HOME": str(paths.togi_home()),
        "TOGI_SESSION": journal.session_id(),
    }
    if paths.approver_override() is not None:   # a nested `togi` must use the same (throwaway) approver as the host
        env["TOGI_APPROVER_HOME"] = str(paths.approver_override())
    if os.environ.get("TOGI_STONE_SANDBOX"):    # a nested `togi` in a stone sandbox run is still in that sandbox
        env["TOGI_STONE_SANDBOX"] = os.environ["TOGI_STONE_SANDBOX"]
    if paths.is_agent():
        env["TOGI_AGENT"] = os.environ["TOGI_AGENT"]
    return env


# --------------------------------------------------------------------------- the host's own environment


def manifest_run_env() -> list[str]:
    """Union of `[policy].run_env` over every registered bundle: the variables the host may keep."""
    from .manifest import ManifestError, load_manifest

    names: list[str] = []
    try:
        ents = registry.entries()
    except TogiError:
        return names
    for ent in ents.values():
        try:
            names += load_manifest(ent.path).run_env
        except (ManifestError, OSError):
            continue
    return list(dict.fromkeys(names))


def host_environment(environ: dict[str, str], run_env: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """(environment for the re-exec'd host, values to hand over out of band). The first holds only the
    allowlist -- PATH, HOME, TMPDIR, LANG, LC_*, TOGI_* -- and never a PYTHON* variable: the host is started isolated
    (`python -I`, which ignores them) and finds Togi through the launcher, not PYTHONPATH, so even a manifest that lists
    PYTHONPATH in `run_env` cannot carry it in. The second holds the other `run_env` variables, so they never sit in a
    process-visible environment."""
    env = {k: v for k, v in environ.items()
           if k in HOST_ENV_ALLOW or k.startswith("LC_") or k.startswith("TOGI_")}
    oob = {k: environ[k] for k in run_env if k in environ and k not in env and not k.startswith("PYTHON")}
    return env, oob


def _adopt_out_of_band_env() -> None:
    """In the re-exec'd host: read the run_env values from the inherited pipe into os.environ (not visible to
    `ps -E`/procfs: those show only the environment the process was started with)."""
    raw_fd = os.environ.pop(ENV_FD, None)
    if not raw_fd:
        return
    try:
        fd = int(raw_fd)
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        os.close(fd)
        os.environ.update({str(k): str(v) for k, v in json.loads(b"".join(chunks) or b"{}").items()})
    except (OSError, ValueError):
        pass


def reexec_scrubbed() -> bool:
    """Make this process the `togi run` host with a scrubbed environment. In the CLI process this REPLACES the
    process (exec) with the same command line and the allowlisted environment, and never returns; in the
    already-scrubbed copy it adopts the out-of-band values and returns True. Embedded/in-process callers (tests,
    libraries) are left alone and get False."""
    if os.environ.get(REEXEC_FLAG):
        _adopt_out_of_band_env()
        return True
    if not cli.PROCESS_ENTRY or not sys.executable:
        return False
    env, oob = host_environment(dict(os.environ), manifest_run_env())
    env[REEXEC_FLAG] = "1"
    blob = json.dumps(oob).encode() if oob else b""
    if blob and len(blob) <= MAX_OOB_BYTES:
        r, w = os.pipe()
        os.write(w, blob)
        os.close(w)
        os.set_inheritable(r, True)
        env[ENV_FD] = str(r)
    elif blob:    # too large for a pipe buffer: keep them in the environment rather than lose them
        env.update(oob)
    try:
        os.execve(sys.executable, paths.isolated_argv(cli.ENTRY_ARGV), env)   # python -I _launch.py ...: see paths.py
    except OSError:
        return False
    return False  # pragma: no cover  (execve does not return)


def bundle_modules() -> list[str]:
    """Top-level import names of every registered bundle (`crowd` for `crowd.togi_bundle:bundle`). The sandbox hides
    these from the child even where they are INSTALLED (site-packages), so a script cannot import a bundle's engine."""
    from .manifest import ManifestError, load_manifest

    names: list[str] = []
    try:
        ents = registry.entries()
    except TogiError:
        return names
    for ent in ents.values():
        try:
            names.append(load_manifest(ent.path).module_name.split(".")[0])
        except (ManifestError, OSError):
            continue
    return sorted(set(n for n in names if n))


def describe_bundle(name: str) -> dict:
    try:
        loaded = registry.load(name)
    except TogiError as e:
        return output.error_envelope(e.status, e.code, e.message, next=e.next)
    meta = []
    for c in loaded.bundle.commands.values():
        meta.append({"api": c.api_name, "command": c.name, "effect": c.effect, "summary": c.summary,
                     "stub": c.stub(), "params": [{"name": p.name, "kind": p.kind, "required": p.required}
                                                  for p in c.params]})
    return output.ok_envelope(meta)


def proxied_call(msg: dict, session: str, seen: list[dict]) -> dict:
    name = msg.get("bundle", "")
    opts = host.CallOptions(surface="run", cap=False, paginate=False, full=True,
                            dry_run=bool(msg.get("dry_run")), approval=msg.get("approval") or None,
                            session=session, argv=["run", name])
    try:
        loaded = registry.load(name)
    except TogiError as e:
        return output.error_envelope(e.status, e.code, e.message, next=e.next)
    cmd = loaded.bundle.get(str(msg.get("command", "")))
    if cmd is None:
        err = ArgError(f"{name} has no command {msg.get('command')!r}", code="unknown_command",
                       flag=str(msg.get("command")), next=f"togi tools {name}")
        return host.call(loaded, None, {}, opts, error=err).envelope
    raw = msg.get("args") or {}
    try:
        args = host.build_args(cmd, raw)
    except ArgError as e:
        return host.call(loaded, cmd, {}, opts, error=e).envelope
    env = host.call(loaded, cmd, args, opts).envelope
    if env.get("status") == "needs_approval" and env.get("approval"):
        seen.append(env["approval"])
    return env


def execute_script(source: str, *, timeout: float = DEFAULT_TIMEOUT, max_output: int = DEFAULT_MAX_OUTPUT,
                   session: str | None = None, use_sandbox: bool = True) -> RunOutcome:
    from . import journal

    session = session or journal.session_id()
    out = RunOutcome()
    entries = registry.entries()
    names = sorted(entries)
    with tempfile.TemporaryDirectory(prefix="togi-run-") as tmp:
        tmp = os.path.realpath(tmp)
        script = Path(tmp) / "script.py"
        script.write_text(source, encoding="utf-8")
        # Togi's stub lives in the child's own temp dir: the sandbox never has to expose Togi's source tree
        child_py = Path(tmp) / "_runchild.py"
        errors_py = Path(tmp) / "errors.py"
        shutil.copyfile(Path(__file__).with_name("_runchild.py"), child_py)
        shutil.copyfile(Path(__file__).with_name("errors.py"), errors_py)
        sb = (sandbox.prepare(tmp=tmp, repos=[e.path for e in entries.values()], togi_home=str(paths.togi_home()),
                              home=os.path.expanduser("~"), also_hidden=_approver_hidden(),
                              modules=bundle_modules())
              if use_sandbox
              else sandbox.Sandbox(False, "sandbox disabled by the caller"))
        out.sandboxed, out.sandbox_reason, out.sandbox_level = sb.sandboxed, sb.reason or sb.note, sb.level
        parent_sock, child_sock = socket.socketpair()
        proc = subprocess.Popen(
            [*sb.prefix, sys.executable, "-I", "-B", *paths.pycache_args(), str(child_py), str(errors_py), str(child_sock.fileno()),
             str(script), json.dumps(names)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp,
            env=scrubbed_env(tmp), pass_fds=(child_sock.fileno(),), start_new_session=True)
        child_sock.close()
        timed_out = threading.Event()

        def kill():
            timed_out.set()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        timer = threading.Timer(timeout, kill)
        timer.daemon = True
        timer.start()
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, "stdout")
        sel.register(proc.stderr, selectors.EVENT_READ, "stderr")
        sel.register(parent_sock, selectors.EVENT_READ, "sock")
        state = {"buf": b""}
        stdout_b, stderr_b = bytearray(), bytearray()

        def pump(events) -> None:
            for key, _ in events:
                if key.data == "sock":
                    try:
                        data = parent_sock.recv(65536)
                    except OSError:
                        data = b""
                    if not data:
                        sel.unregister(parent_sock)
                        continue
                    state["buf"] += data
                    while b"\n" in state["buf"]:
                        line, state["buf"] = state["buf"].split(b"\n", 1)
                        if len(line) > MAX_LINE:
                            _protocol_abort(out, proc, f"a request line of {len(line)} bytes (the limit is {MAX_LINE})")
                            state["buf"] = b""
                            break
                        _handle(line, parent_sock, session, out)
                    if len(state["buf"]) > MAX_LINE:   # no newline in sight: stop buffering it
                        _protocol_abort(out, proc, f"more than {MAX_LINE} bytes without a line end")
                        state["buf"] = b""
                else:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        sel.unregister(key.fileobj)
                        continue
                    if key.data == "stdout":
                        room = max_output - len(stdout_b)
                        if room > 0:
                            stdout_b.extend(chunk[:room])
                        if len(chunk) > room:
                            out.truncated = True
                    elif len(stderr_b) < STDERR_CAP:
                        stderr_b.extend(chunk[: STDERR_CAP - len(stderr_b)])

        try:
            while sel.get_map():
                pump(sel.select(timeout=0.2))
                if proc.poll() is not None:
                    # the child is gone: take what is already buffered, ignore any stray background holders
                    for _ in range(50):
                        ev = sel.select(timeout=0.02)
                        if not ev or not sel.get_map():
                            break
                        pump(ev)
                    break
        finally:
            timer.cancel()
            try:
                os.killpg(proc.pid, signal.SIGKILL)  # nothing the script started may outlive the run
            except (ProcessLookupError, PermissionError):
                pass
            sel.close()
            parent_sock.close()
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            rc = proc.wait()
    out.stdout = stdout_b.decode("utf-8", "replace")
    out.stderr = stderr_b.decode("utf-8", "replace")
    out.timed_out = timed_out.is_set()
    out.exit_code = 1 if out.timed_out else (rc if rc in (0, 1, 2, 3) else 1)
    return out


def _protocol_abort(out: RunOutcome, proc, why: str) -> None:
    """The child sent something the host will not buffer: stop it, and report that instead of its output."""
    out.fatal = {"op": "fatal", "kind": "exception", "code": "protocol_error",
                 "message": f"the script sent {why}; it was stopped"}
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _cut(text: object, limit: int = MAX_MESSAGE) -> str:
    s = text if isinstance(text, str) else str(text)
    return s if len(s) <= limit else s[:limit] + f"...[+{len(s) - limit} chars]"


def _handle(line: bytes, sock: socket.socket, session: str, out: RunOutcome) -> None:
    try:
        msg = json.loads(line.decode("utf-8"))
    except ValueError:
        return
    op = msg.get("op")
    if op == "fatal":
        # whatever the script says about its own failure is bounded before it is kept: a 200 KB exception message must not
        # reach the caller's stderr
        for k in ("message", "next", "code"):
            if isinstance(msg.get(k), str):
                msg[k] = _cut(msg[k])
        if isinstance(msg.get("approval"), dict):
            msg["approval"] = {k: (_cut(v) if isinstance(v, str) else v) for k, v in msg["approval"].items()
                               if k in ("id", "command", "summary", "expires")}
        out.fatal = msg
        return
    if op == "describe":
        resp = describe_bundle(str(msg.get("bundle")))
    elif op == "answer":       # togi_answer(obj) in a script: the same record `togi answer` makes
        out.calls += 1
        try:
            resp = answers.envelope(answers.record(msg.get("answer"), surface="run", session=session))
        except TogiError as e:
            resp = output.error_envelope(e.status, e.code, e.message, next=e.next)
    elif op == "task":         # togi_task_show() / togi_task_start(goal) / togi_task_update(done=, next=, waiting=)
        out.calls += 1
        try:
            a = msg.get("args") or {}
            done = a.get("done")
            resp = output.ok_envelope(taskrec.run(
                str(msg.get("sub")), goal=a.get("goal"), done=[done] if isinstance(done, str) else done,
                next_=a.get("next"), waiting=a.get("waiting"), status=a.get("status"), force=bool(a.get("force")),
                surface="run"))
        except TogiError as e:
            resp = output.error_envelope(e.status, e.code, e.message, next=e.next)
    elif op == "call":
        out.calls += 1
        try:
            resp = proxied_call(msg, session, out.approvals)
        except Exception as e:  # a host bug must not hang the child
            resp = output.error_envelope("internal", "internal_error", f"{type(e).__name__}: {e}", next="togi doctor")
    else:
        resp = output.error_envelope("error", "bad_request", f"unknown op {op!r}")
    try:
        sock.sendall((output.dumps(resp) + "\n").encode("utf-8"))
    except OSError:
        pass  # the child died (timeout or crash)


# --------------------------------------------------------------------------- CLI handler


def clamp_output(requested: int | None, full: bool) -> int:
    """The byte cap for what a script prints: `--max-output N` if given, else the default (or the `--full` limit with
    `--full`), and never more than FULL_MAX_OUTPUT (1,000,000). `--max-output` raises the default cap but cannot lift the
    ceiling that `--full` already has, so one flag cannot be used to pull an unbounded stream back into a context (r5)."""
    cap = requested if requested else (FULL_MAX_OUTPUT if full else DEFAULT_MAX_OUTPUT)
    return max(1, min(int(cap), FULL_MAX_OUTPUT))


def h_run(argv: list[str]) -> Reply:
    pos, f = parse_flags(argv, {"timeout": "float", "max-output": "int"}, "run")
    if f["help"]:
        return _usage("run")
    if len(pos) != 1:
        raise TogiError("bad_arg", "togi run needs a script path, or - to read the script from stdin",
                        next="togi run script.py")
    scrubbed = reexec_scrubbed()      # may replace this process; nothing has been read from stdin yet
    runreg.register()                 # so `approve` can see that a script's descendants are not a person
    if pos[0] == "-":
        source = sys.stdin.read()
    else:
        p = Path(pos[0]).expanduser()
        if not p.is_file():
            raise TogiError("path_not_found", f"no script at {pos[0]}", next="togi run <file.py | ->")
        source = p.read_text(encoding="utf-8")
    cap = clamp_output(f["max_output"], f["full"])
    timeout = f["timeout"] or DEFAULT_TIMEOUT
    res = execute_script(source, timeout=timeout, max_output=cap)
    res.host_env_scrubbed = scrubbed
    env = _final_envelope(res, timeout, cap)
    mode = _mode(f)
    if mode == "json":
        data = {"stdout": res.stdout, "stderr": res.stderr, "exit_code": res.exit_code,
                "truncated": res.truncated, "sandboxed": res.sandboxed}
        if res.sandboxed:
            data["sandbox_level"] = res.sandbox_level
        if res.sandbox_reason:
            data["sandbox_note"] = res.sandbox_reason
        if env.get("ok"):
            return Reply(env=output.ok_envelope(data), mode="json", code=0)
        return Reply(env={**env, "data": data}, mode="json", code=res.exit_code)
    # default: exactly what the script printed, on stdout; problems on stderr
    if res.truncated:
        # the note is part of the budget: what reaches stdout (script bytes + note) never exceeds `cap`, which never exceeds
        # FULL_MAX_OUTPUT, so one run cannot hand back more than the --full limit
        note = f"\n[togi: output cut at {cap} bytes; re-run with --full or print less]\n"
        keep = res.stdout.encode("utf-8")[:max(0, cap - len(note.encode()))].decode("utf-8", "ignore")
        sys.stdout.write(keep + note)
    else:
        sys.stdout.write(res.stdout)
        if res.stdout and not res.stdout.endswith("\n"):
            sys.stdout.write("\n")
    sys.stdout.flush()
    if not res.sandboxed:   # never silent: the person/agent should know this ran without the OS sandbox
        sys.stderr.write(f"[togi: sandboxed: false - {res.sandbox_reason}]\n")
    if res.stderr.strip() and not (res.fatal and res.fatal.get("kind") in ("togi_error", "needs_approval")):
        sys.stderr.write(res.stderr if res.stderr.endswith("\n") else res.stderr + "\n")
    if not env.get("ok"):
        sys.stderr.write(output.dumps(output.hard_cap(env, output.MAX_BYTES)) + "\n")
    sys.stderr.flush()
    return Reply(code=res.exit_code, silent=True)


def _final_envelope(res: RunOutcome, timeout: float, cap: int) -> dict:
    if res.timed_out:
        return output.error_envelope("error", "timeout", f"the script ran longer than {timeout:g}s and was stopped",
                                     next=f"togi run --timeout {int(timeout * 2)} <script>")
    fatal = res.fatal or {}
    if res.exit_code == 0:
        return output.ok_envelope(None)
    if fatal.get("kind") == "needs_approval":
        appr = fatal.get("approval") or (res.approvals[-1] if res.approvals else None)
        return output.error_envelope("needs_approval", "needs_approval", _cut(fatal.get("message") or "approval needed"),
                                     next=_cut(fatal.get("next")) if fatal.get("next") else None, approval=appr)
    if fatal.get("kind") == "togi_error":
        return output.error_envelope(_cut(fatal.get("status", "error"), 20), _cut(fatal.get("code", "error"), 80),
                                     _cut(fatal.get("message", "")),
                                     next=_cut(fatal.get("next")) if fatal.get("next") else None)
    msg = _cut(fatal.get("message") or (res.stderr.strip().splitlines() or ["the script failed"])[-1])
    return output.error_envelope("error", _cut(fatal.get("code", "script_error"), 80), msg,
                                 next="fix the script and re-run: togi run <script>")
