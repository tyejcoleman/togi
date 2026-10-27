"""The describe snapshot: what a bundle's commands ARE, recorded and signed when a person locks it (SPEC section 3, r5).

`togi approve` has to show a person what they are about to sign: the command, its summary, the arguments, the word to type.
The facts behind that (a command's name, effect, parameters, `confirm` parameter, one-line summary) live in the bundle's
Python. In r4 the approve process obtained them by importing the bundle in a child; the child had a scrubbed environment but
was otherwise an ordinary process, so a bundle whose import-time code wrote a file or scanned the disk did it, unsandboxed,
at the moment a person was about to decide. Now:

  1. The facts are read ONCE, at `togi lock` time, by `snapshot_in_child`: a child that imports the bundle under the `togi
     run` strict sandbox profile (no network, writes only to its own temp dir, nothing of `$TOGI_HOME`, the approver or any
     other repo readable), in its own session (no controlling terminal), with a scrubbed environment (no `run_env` values),
     a timeout and a capped output, and its whole process group killed afterwards.
  2. They are stored in `<repo>/.togi/describe.json`, with the sha256 of the lock file they belong to and an HMAC under the
     audit key (the key that signs the audit log), so they are only valid for exactly that lock.
  3. `approve` never imports the bundle. It checks the snapshot (`read_snapshot`), then recomputes the summary and the
     confirmation word from it as pure data (`approvals.summarize` / `confirm_word` over a `CmdView`) and compares them with
     what the request filed, which the host built at request time from the live command. Any difference is refused.
  4. A bundle with no snapshot (it was locked before r5, or never locked and `require_lock = false`) is read by
     `command_in_child`, the same hardened child; where no OS sandbox exists it is refused, not run unsandboxed.

The snapshot also records which repo code stands behind the gated commands (`gated_roots`) and which repo modules the bundle
loaded (`loaded_files`), so `approve` can re-check the gated-code coverage rule without importing anything (codeset.py).

Honest limit: the key that signs the snapshot is a plain file in `$TOGI_HOME`, like the audit key. This is tamper-evidence
against an agent that can only write the repo, not against a process that can read `$TOGI_HOME`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import codeset, paths, sandbox
from .errors import TogiError
from .pathnorm import escape_text

SNAPSHOT_REL = ".togi/describe.json"
SNAPSHOT_VERSION = 1
SNAPSHOT_DOMAIN = "togi-describe-v1\n"
RESULT_PREFIX = "TOGI-DESCRIBE "
CHILD_TIMEOUT_S = 30
MAX_STDOUT = 1_000_000
MAX_STDERR = 65_536
MAX_TOTAL = 32_000_000          # a child that writes this much has gone wrong; stop it


# --------------------------------------------------------------------------- command views (pure data)


@dataclass
class ParamView:
    name: str
    kind: str = "str"
    required: bool = True


@dataclass
class CmdView:
    """The part of a bundle `Command` that the approval summary and the confirmation word are computed from. It has the
    same attribute names, so `approvals.summarize` / `confirm_word` work on a live command and on a snapshot alike."""
    name: str
    effect: str
    summary: str
    confirm: str | None
    params: list[ParamView] = field(default_factory=list)
    api_name: str = ""
    schema: dict = field(default_factory=dict)


def command_record(cmd: Any) -> dict:
    """One command, as JSON: name, effect, summary, `confirm` parameter, ordered parameters and the JSON schema."""
    return {"name": cmd.name, "api": cmd.api_name, "effect": cmd.effect, "summary": cmd.summary, "confirm": cmd.confirm,
            "params": [{"name": p.name, "kind": p.kind, "required": p.required} for p in cmd.params],
            "schema": cmd.json_schema()}


def _good_command(rec: object) -> bool:
    """Is this the shape of a `command_record`? (An answer from the sandboxed child is untrusted text until it has been
    through here; a malformed one is a refusal, never a crash.)"""
    return (isinstance(rec, dict) and isinstance(rec.get("name"), str) and isinstance(rec.get("effect"), str)
            and isinstance(rec.get("summary"), str) and isinstance(rec.get("params"), list)
            and all(isinstance(p, dict) and isinstance(p.get("name"), str) for p in rec["params"])
            and (rec.get("confirm") is None or isinstance(rec.get("confirm"), str)))


def view_of(rec: dict) -> CmdView:
    return CmdView(name=rec["name"], effect=rec["effect"], summary=rec["summary"], confirm=rec.get("confirm"),
                   params=[ParamView(p["name"], p.get("kind", "str"), bool(p.get("required", True)))
                           for p in rec.get("params", [])], api_name=rec.get("api", ""), schema=rec.get("schema") or {})


def views(doc: dict) -> dict[str, CmdView]:
    return {rec["name"]: view_of(rec) for rec in doc.get("commands", [])}


def build_body(loaded: Any) -> dict:
    """What a loaded bundle says about itself: its commands, the roots of the repo code behind the gated ones, and every
    repo file its process has loaded. Runs in the describe child, where the bundle was just imported."""
    man = loaded.manifest
    return {"commands": [command_record(c) for c in loaded.bundle.commands.values()],
            "gated_roots": codeset.gated_roots_live(man.repo, loaded.bundle, man),
            "loaded_files": codeset.loaded_repo_files(man.repo)}


# --------------------------------------------------------------------------- the signed file


def snapshot_path(repo: Path) -> Path:
    return repo / SNAPSHOT_REL


def _canonical(doc: dict) -> bytes:
    body = {k: v for k, v in doc.items() if k != "mac"}
    return (SNAPSHOT_DOMAIN + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)).encode("ascii")


def _mac(key: bytes, doc: dict) -> str:
    return hmac.new(key, _canonical(doc), hashlib.sha256).hexdigest()


def write_snapshot(man: Any, body: dict, lock_sha256: str, *, sandboxed: bool = True) -> dict:
    """Sign `body` for the lock whose file hashes to `lock_sha256` and store it beside the lock. Called by `togi lock`."""
    from . import audit
    from .journal import now_ts

    key = audit.ensure_key()
    doc = {"v": SNAPSHOT_VERSION, "bundle": man.name, "repo": str(man.repo), "version": man.version, "module": man.module,
           "lock_sha256": lock_sha256, "recorded": now_ts(), "sandboxed": bool(sandboxed), **body}
    doc["mac"] = _mac(key, doc)
    p = snapshot_path(man.repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".describe-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="ascii") as f:
        json.dump(doc, f, sort_keys=True, ensure_ascii=True)
    os.replace(tmp, p)
    return doc


def remove_snapshot(repo: Path) -> None:
    """A lock made without a snapshot (library call, no sandbox) must not leave the previous lock's one behind."""
    try:
        snapshot_path(repo).unlink()
    except OSError:
        pass


@dataclass
class Snapshot:
    state: str                      # ok | missing | stale | invalid
    doc: dict | None = None
    why: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "ok"


def read_snapshot(man: Any, lock_sha256: str | None) -> Snapshot:
    """The snapshot for the lock whose file hashes to `lock_sha256`, if there is a genuine one. `missing`: no file (locked
    before r5, or never locked). `stale`: genuine but made for another lock or version. `invalid`: unreadable, not ours,
    or its signature does not verify (it was edited, or made on another machine)."""
    from . import audit

    p = snapshot_path(man.repo)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Snapshot("missing", why="no .togi/describe.json")
    except OSError as e:
        return Snapshot("invalid", why=f"cannot read it ({type(e).__name__})")
    try:
        doc = json.loads(raw)
        ok_shape = (isinstance(doc, dict) and doc.get("v") == SNAPSHOT_VERSION and isinstance(doc.get("commands"), list)
                    and isinstance(doc.get("mac"), str) and isinstance(doc.get("gated_roots"), list)
                    and isinstance(doc.get("loaded_files"), list)
                    and all(_good_command(c) for c in doc["commands"]))
    except (ValueError, KeyError, TypeError):
        return Snapshot("invalid", why="it is not valid JSON")
    if not ok_shape:
        return Snapshot("invalid", why="it is not a describe snapshot")
    try:
        key = audit.load_key()
    except TogiError:
        key = None
    if key is None:
        return Snapshot("invalid", doc, f"there is no audit key ({paths.audit_key_path()}) to check its signature with")
    if not hmac.compare_digest(doc["mac"].encode("ascii", "replace"), _mac(key, doc).encode()):
        return Snapshot("invalid", doc, "its signature does not verify (it was edited, or made under another audit key)")
    if doc.get("bundle") != man.name or doc.get("repo") != str(man.repo):
        return Snapshot("invalid", doc, "it was made for another bundle or another repo path")
    if lock_sha256 is None or doc.get("lock_sha256") != lock_sha256 or doc.get("version") != man.version:
        return Snapshot("stale", doc, "it was recorded for an earlier lock; `togi lock` records a fresh one")
    return Snapshot("ok", doc)


# --------------------------------------------------------------------------- the hardened child


def _prepare(man: Any, tmp: str) -> sandbox.Sandbox:
    from . import registry

    others = []
    try:
        others = [e.path for n, e in registry.entries().items() if os.path.realpath(e.path) != str(man.repo)]
    except TogiError:
        pass
    hidden = ()
    try:
        hidden = (str(paths.approver_home()),)
    except TogiError:
        pass
    return sandbox.prepare_describe(tmp=tmp, repo=str(man.repo), other_repos=others, togi_home=str(paths.togi_home()),
                                    home=os.path.expanduser("~"), also_hidden=hidden)


def _communicate(proc: subprocess.Popen, data: bytes, timeout: float) -> tuple[bytes, bytes, bool]:
    """Feed `data` to the child's stdin, read at most MAX_STDOUT / MAX_STDERR back, give up after `timeout`. Returns
    (stdout, stderr, timed_out). Unlike `subprocess.communicate` nothing is buffered without a bound."""
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ, "out")
    sel.register(proc.stderr, selectors.EVENT_READ, "err")
    try:
        proc.stdin.write(data)
        proc.stdin.close()
    except (BrokenPipeError, OSError):
        pass
    out, err = bytearray(), bytearray()
    total, timed_out = 0, False
    deadline = time.monotonic() + timeout

    def pump(events) -> None:
        nonlocal total
        for key, _ in events:
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                sel.unregister(key.fileobj)
                continue
            total += len(chunk)
            buf, cap = (out, MAX_STDOUT) if key.data == "out" else (err, MAX_STDERR)
            if len(buf) < cap:
                buf.extend(chunk[:cap - len(buf)])

    try:
        while sel.get_map():
            left = deadline - time.monotonic()
            if left <= 0 or total > MAX_TOTAL:
                timed_out = left <= 0
                break
            pump(sel.select(timeout=min(left, 0.2)))
            if proc.poll() is not None:      # gone: take what is already buffered, ignore stray holders of the pipes
                for _ in range(50):
                    ev = sel.select(timeout=0.02)
                    if not ev or not sel.get_map():
                        break
                    pump(ev)
                break
    finally:
        sel.close()
    return bytes(out), bytes(err), timed_out


def run_child(man: Any, req: dict, *, script: Path | None = None, allow_unsandboxed: bool = False,
              timeout: float = CHILD_TIMEOUT_S) -> tuple[dict, dict]:
    """Run `_describe.py` (or `script`) as the hardened child and return (its one result object, info).

    The child: under the strict sandbox profile (`sandbox.prepare_describe`); in a session of its own (`start_new_session`,
    so no controlling terminal and a process group that is killed whatever happens); with stdin a pipe that holds only the
    request; with an environment of PATH / HOME / TMPDIR / LANG and nothing else (HOME and TMPDIR are its temp dir; no
    `run_env` value, no TOGI_* variable, nothing of the person's); with the temp dir as its working directory; for at most
    `timeout` seconds and MAX_STDOUT bytes of answer. `info` has `sandboxed`, `level`, `stderr` (the tail, for messages).
    Raises TogiError `describe_unsandboxed` when no OS sandbox is available and `allow_unsandboxed` is false, and
    `describe_failed` for any failure of the child."""
    child = script or Path(__file__).resolve().with_name("_describe.py")
    tmp = os.path.realpath(tempfile.mkdtemp(prefix="togi-describe-"))
    proc = None
    try:
        sb = _prepare(man, tmp)
        if not sb.sandboxed and not allow_unsandboxed:
            raise TogiError("describe_unsandboxed",
                            f"reading {man.name}'s commands means importing it, which needs an OS sandbox, and there is none "
                            f"here ({sb.reason}); the bundle's code is not run outside one",
                            next=f"a person runs: togi lock {man.name}   (it records the commands once, so approve needs no "
                                 "import)", status="refused")
        env = {"PATH": "/usr/bin:/bin", "HOME": tmp, "TMPDIR": tmp, "LANG": "C.UTF-8"}
        try:
            proc = subprocess.Popen([*sb.prefix, sys.executable, "-I", "-B", *paths.pycache_args(), str(child)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=tmp, env=env,
                                    start_new_session=True)
        except OSError as e:
            raise TogiError("describe_failed", f"could not start the child that reads {man.name}'s commands ({e})",
                            next="togi doctor", status="refused")
        out, err, timed_out = _communicate(proc, json.dumps(req).encode("utf-8"), timeout)
    finally:
        if proc is not None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)       # nothing the bundle started may outlive the look
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
    tail = escape_text((err.decode("utf-8", "replace").strip().splitlines() or ["no output"])[-1], 200)
    info = {"sandboxed": sb.sandboxed, "level": sb.level, "stderr": tail}
    if timed_out:
        raise TogiError("describe_failed", f"loading {man.name} to read its commands took longer than {timeout:g}s",
                        next="togi doctor", status="refused")
    line = next((ln for ln in reversed(out.decode("utf-8", "replace").splitlines()) if ln.startswith(RESULT_PREFIX)), None)
    try:
        res = json.loads(line[len(RESULT_PREFIX):]) if line else None
    except ValueError:
        res = None
    if not isinstance(res, dict):
        raise TogiError("describe_failed", f"could not load {man.name} to read its commands: {tail}", next="togi doctor",
                        status="refused")
    if "error" in res:
        e = res["error"] if isinstance(res["error"], dict) else {}
        raise TogiError("describe_failed", f"{man.name}: {escape_text(str(e.get('message', 'failed')), 300)}",
                        next="togi doctor", status="refused")
    return res, info


def snapshot_in_child(man: Any, *, allow_unsandboxed: bool = False) -> tuple[dict, dict]:
    """(snapshot body, info): import the bundle in the hardened child and read its commands, gated roots and loaded files."""
    res, info = run_child(man, {"op": "snapshot", "repo": str(man.repo), "bundle": man.name},
                          allow_unsandboxed=allow_unsandboxed)
    body = res.get("body")
    if not isinstance(body, dict) or not isinstance(body.get("commands"), list):
        raise TogiError("describe_failed", f"{man.name} gave no usable description of its commands", next="togi doctor",
                        status="refused")
    return body, info


def command_in_child(man: Any, command: str) -> dict:
    """The fallback for a bundle with no snapshot: one command's record (and the gated roots / loaded files) from the
    hardened child, which is always sandboxed. The answer is untrusted text: the caller recomputes the summary and the
    confirmation word from it as data, compares them with the request's, and escapes everything it shows."""
    res, _ = run_child(man, {"op": "command", "repo": str(man.repo), "bundle": man.name, "command": command})
    rec = res.get("command")
    if not _good_command(rec):
        raise TogiError("describe_failed", f"{man.name} gave no usable description of its command", next="togi doctor",
                        status="refused")
    return res
