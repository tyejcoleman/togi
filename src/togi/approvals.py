"""Money-effect approvals (SPEC section 2/3).

A pending request is written by the host when an agent calls a gated command. Only a person at a real
terminal can grant it (`togi approve <id>`). The grant is bound to bundle + command + args hash, expires,
and can be used once. The function is never executed without a matching, unused, unexpired grant.

A grant is also bound to the bundle it was requested for: repo path, manifest version and a digest of the
locked files' contents. Granting needs a person at a terminal, the typed confirmation word AND the approver
passphrase (see approver.py).

A grant is SIGNED: `grant_mac = HMAC-SHA256(approver.key, canonical{id, bundle, command, args_hash, binding_hash,
status, expires, decided_by, scope, home})`, where `home` is the real path of the $TOGI_HOME the grant was made in.
The key lives in the approver home (fixed, not chosen by $TOGI_HOME; see paths.py). `check_for_use` verifies the MAC
with the key it finds there and refuses (`approval_unsigned`) otherwise, so a record that was forged, edited, or
copied from another $TOGI_HOME or another approver is worthless. The grant also carries the `scope` of the approver
that signed it ("default" for the fixed one, "test" for a $TOGI_APPROVER_HOME override) and the host hands it to the
command as `ctx.approval_scope`; a bundle that moves real value must refuse "test" outside its own test mode.

Repo-changing human gates (lock, revert, reshape evaluation, human apply) additionally require the DEFAULT scope
(`human_repo_gate`): a throwaway approver is anyone's to create, so its yes may approve money (the bundle decides what a
test grant is worth) but never a change to what a repo may contain.

What a person is shown is what gets signed: `togi approve` does not trust the pending file. It recomputes the args hash
from the args it is about to display, the binding from the live repo, the lock state from the live lock, and the summary
and the confirmation word as pure data from the describe snapshot that `togi lock` signed (describe.py), refuses on any
mismatch with the pending record, and signs what it recomputed (`verify_pending`). It never imports the bundle: the only
code that ever runs is the describe child, sandboxed, for a bundle that has no snapshot yet.

A grant for a throwaway (test-scope) approver is refused unless the bundle's manifest opts in with
`[policy] test_approvals = true` (`test_approvals_allowed`): Togi cannot know which bundles move real value, so the default
is that none of them accept a grant anyone could mint with `TOGI_APPROVER_HOME=/tmp/x togi approver setup`.

Single use is kept in the spent-approval record outside `$TOGI_HOME` (spent.py), not in the approval file.

Honest limit: the TTY check stops an agent using a normal non-interactive shell; the passphrase stops one that
drives a pty; the fixed approver and the MAC stop one that redirects $TOGI_HOME. None of them is a boundary against
a hostile process running as the same user with full file access: it can read `approver.key` and sign a grant.
"""
from __future__ import annotations

import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import approver, codeset, paths, runreg, spent
from .errors import TogiError
from .journal import now_ts, parse_ts
from .pathnorm import escape_text, printable_token

DEFAULT_TTL_S = 15 * 60
MAX_PENDING_PER_BUNDLE = 20
SUMMARY_CAP = 300


SANDBOX_MARKER = ".stone-sandbox"
SANDBOX_ENV = "TOGI_STONE_SANDBOX"


def sandbox_optin() -> bool:
    """True only inside a stone sandbox run (stone.Sandbox / stone.run_tests make one): the harness sets
    `TOGI_STONE_SANDBOX=<sandbox root>`, and the root must contain the marker file, hold this `$TOGI_HOME` as
    `<root>/home` and this `$TOGI_APPROVER_HOME` as `<root>/approver`. The stone runs a bundle's own code in a throwaway
    home with a throwaway approver and no lock: money tasks there cannot touch anything real, so the sandbox accepts
    test-scope grants and skips the lock check for ITS run only. The variable alone, in any other home, does nothing.

    Honest limit: the marker and the variable are things a same-user process can create too. This is the line between
    "the harness's own sandbox" and "a bundle that never opted in", not a boundary against a deliberate forger."""
    root = os.environ.get(SANDBOX_ENV)
    if not root:
        return False
    try:
        root = os.path.realpath(root)
        override = paths.approver_override()
        return (os.path.isfile(os.path.join(root, SANDBOX_MARKER))
                and os.path.realpath(paths.togi_home()) == os.path.join(root, "home")
                and override is not None and os.path.realpath(override) == os.path.join(root, "approver"))
    except OSError:
        return False


def test_approvals_allowed(man: Any) -> bool:
    """May a grant signed by a throwaway (test-scope) approver move this bundle's gated commands? Only if its manifest
    says `[policy] test_approvals = true` (the bundle checks `ctx.approval_scope` itself, or only has fakes to move),
    or this is the stone's own sandbox run."""
    return bool(getattr(man, "test_approvals", False)) or sandbox_optin()


def test_scope_error(aid: str | None, bundle: str) -> TogiError:
    what = f"approval {aid} was signed" if aid else "this approval would be signed"
    return TogiError(
        "test_scope_refused",
        f"{what} by a throwaway test-scope approver ($TOGI_APPROVER_HOME is set), and {bundle} does not accept those: "
        "its manifest does not say [policy] test_approvals = true. Anyone can make a test approver, so by default none "
        "can authorize a gated command",
        next="a person approves it with the real approver (TOGI_APPROVER_HOME unset), or the bundle's author opts in "
             "with [policy] test_approvals = true after making the command check ctx.approval_scope",
        status="refused")


def ttl_seconds() -> int:
    try:
        return int(os.environ.get("TOGI_APPROVAL_TTL", DEFAULT_TTL_S))
    except ValueError:
        return DEFAULT_TTL_S


def args_hash(bundle: str, command: str, args: dict) -> str:
    blob = json.dumps({"bundle": bundle, "command": command, "args": args}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _path(aid: str) -> Path:
    if not aid.replace("_", "").isalnum():
        raise TogiError("approval_not_found", f"'{aid}' is not an approval id", next="togi approvals")
    return paths.approvals_dir() / f"{aid}.json"


def _write(rec: dict) -> None:
    d = paths.approvals_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".ap-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=1, sort_keys=True, default=str)
    os.replace(tmp, _path(rec["id"]))


def _iso(epoch: float) -> str:
    import datetime as dt

    t = dt.datetime.fromtimestamp(epoch, dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def load(aid: str) -> dict:
    p = _path(aid)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise TogiError("approval_not_found", f"no approval '{aid}'", next="togi approvals", status="refused")


def effective_status(rec: dict, now: float | None = None) -> str:
    """pending | approved | denied | consumed | expired"""
    now = time.time() if now is None else now
    st = rec.get("status", "pending")
    if st in ("denied", "consumed"):
        return st
    if now > parse_ts(rec["expires"]):
        return "expired"
    return st


def confirm_word(rec_or_args: dict, command: Any = None, fallback_id: str = "") -> str:
    """The word a person types to approve: the `confirm` parameter's value, else the first short id-like
    string argument, else the last 4 characters of the approval id."""
    args = rec_or_args
    if command is not None:
        conf = str(args.get(getattr(command, "confirm", None) or "", "")).strip()
        if conf and printable_token(conf):
            return conf
        order = [p.name for p in command.params] if getattr(command, "params", None) else list(args)
    else:
        order = list(args)
    for name in order:
        v = args.get(name)
        if isinstance(v, str) and printable_token(v):   # never a word that could move the cursor or hide text
            return v
    return fallback_id[-4:] if fallback_id else "yes"


def confirm_word_for_denial(rec: dict) -> str:
    """The word typed to DENY. A denial verifies nothing (it only ever makes a record harmless), so it shows the record as
    filed; but a requester must not be able to make it impossible to deny by filing a 10,000-character word. A word that
    is not short and printable is replaced by the approval id's tail."""
    w = rec.get("confirm_word")
    return w if isinstance(w, str) and printable_token(w) else str(rec.get("id", ""))[-4:] or "deny"


def summarize(bundle: str, command: Any, args: dict) -> str:
    """One line for the prompt. Arguments appear in the command's parameter order (extras sorted), NOT in the dict's
    order: the record is stored with sorted keys, and `togi approve` regenerates this text from the reloaded args and
    compares it to the stored one, so it must come out the same either way."""
    order = [p.name for p in getattr(command, "params", [])]
    keys = [k for k in order if k in args] + sorted(k for k in args if k not in order)
    shown = ", ".join(f"{k}={args[k]}" for k in keys if args[k] not in (None, [], False))
    base = f"{bundle} {command.name}: {command.summary}".strip()
    text = f"{base} ({shown})" if shown else base
    return text if len(text) <= SUMMARY_CAP else text[:SUMMARY_CAP - 1] + "\u2026"


def binding_hash(binding: dict) -> str:
    """What an approval is bound to besides command + args: where the bundle lives, which manifest version,
    and the contents of its locked files. If any of them changes, the approval is dead."""
    blob = json.dumps({k: binding.get(k) for k in ("repo", "version", "locked_digest")}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode()).hexdigest()


# --------------------------------------------------------------------------- signed grants

GRANT_FIELDS = ("id", "bundle", "command", "args_hash", "binding_hash", "expires", "decided_by", "scope")


def _home_id() -> str:
    """The $TOGI_HOME a grant lives in (real path): part of what is signed, so a grant cannot be copied between homes."""
    return os.path.realpath(paths.togi_home())


def signed_status(rec: dict) -> str:
    """The decision the signature covers: `consumed` is only a later bookkeeping state of an approved grant."""
    return "denied" if rec.get("status") == "denied" else "approved"


def grant_payload(rec: dict) -> bytes:
    body = {k: rec.get(k) for k in GRANT_FIELDS}
    body["status"] = signed_status(rec)
    body["home"] = _home_id()
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("ascii")


def sign_grant(rec: dict) -> str:
    """The `grant_mac` for a decided record, under the approver key of the current approver home."""
    return approver.mac_hex(approver.require_key(), grant_payload(rec))


def verify_grant(rec: dict) -> None:
    """Raise `approval_unsigned` unless this decided record carries a valid MAC made by THIS approver (the key in
    the approver home), for THIS $TOGI_HOME, with the scope this process's approver signs with."""
    why = None
    mac, scope = rec.get("grant_mac"), paths.approver_scope()
    if not isinstance(mac, str) or not mac:
        why = "it carries no signature from an approver"
    elif rec.get("scope") != scope:
        why = f"it was signed in scope {rec.get('scope')!r} but this process uses the {scope!r} approver"
    else:
        key = approver.load_key()
        if key is None:
            why = f"there is no approver key at {paths.approver_key_path()} to check it with"
        elif not hmac.compare_digest(mac.encode("utf-8", "replace"),
                                     approver.mac_hex(key, grant_payload(rec)).encode()):
            why = "its signature does not match (it was edited, or made by another approver or in another $TOGI_HOME)"
    if why:
        raise TogiError("approval_unsigned", f"approval {rec.get('id', '?')} is refused: {why}",
                        next="re-run this command without --approval to request a new approval", status="refused")


def sign_blob(kind: str, fields: dict) -> str:
    """HMAC (approver key) over `kind`, `fields`, the current scope and this $TOGI_HOME: a person's decision recorded
    somewhere an agent could also write (e.g. a proposal's meta.json) must be signed, or it proves nothing."""
    body = {"kind": kind, "fields": fields, "scope": paths.approver_scope(), "home": _home_id()}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("ascii")
    return approver.mac_hex(approver.require_key(), blob)


def blob_valid(kind: str, fields: dict, scope: object, mac: object) -> bool:
    """True only if `mac` is `sign_blob(kind, fields)` made by THIS approver, in THIS $TOGI_HOME, in the scope this
    process uses. Never raises: a missing or damaged key simply means "not valid"."""
    if not isinstance(mac, str) or scope != paths.approver_scope():
        return False
    try:
        return hmac.compare_digest(mac.encode("utf-8", "replace"), sign_blob(kind, fields).encode())
    except TogiError:
        return False


def pending_count(bundle: str, now: float | None = None) -> int:
    now = time.time() if now is None else now
    d = paths.approvals_dir()
    n = 0
    for f in (d.glob("ap_*.json") if d.is_dir() else []):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if rec.get("bundle") == bundle and effective_status(rec, now) == "pending":
            n += 1
    return n


def request(bundle: str, command: Any, args: dict, session: str | None, agent: str | None,
            binding: dict | None = None) -> dict:
    """Create (or reuse an identical live) pending approval request. At most MAX_PENDING_PER_BUNDLE pending
    requests per bundle: a flood would bury the one a person needs to see."""
    binding = binding or {"repo": None, "version": None, "locked_digest": None}
    h, bh = args_hash(bundle, command.name, args), binding_hash(binding)
    now = time.time()
    d = paths.approvals_dir()
    pending = 0
    if d.is_dir():
        for f in d.glob("ap_*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if effective_status(rec, now) != "pending":
                continue
            if rec.get("args_hash") == h and rec.get("binding_hash") == bh:
                return rec  # an agent retrying must not spam the human with duplicates
            if rec.get("bundle") == bundle:
                pending += 1
    if pending >= MAX_PENDING_PER_BUNDLE:
        raise TogiError("too_many_pending", f"{bundle} already has {pending} pending approval requests (the limit "
                                            f"is {MAX_PENDING_PER_BUNDLE}); no new request was created",
                        next="a person runs: togi approvals   then approves or denies them (togi deny <id>)",
                        status="refused")
    aid = "ap_" + secrets.token_hex(5)
    rec = {
        "id": aid, "bundle": bundle, "command": command.name, "effect": command.effect, "args": args,
        "args_hash": h, "binding": {k: binding.get(k) for k in ("repo", "version", "locked_digest")},
        "binding_hash": bh, "summary": summarize(bundle, command, args),
        "confirm_word": confirm_word(args, command, aid),
        "status": "pending", "created": now_ts(), "expires": _iso(now + ttl_seconds()),
        "session": session, "agent": agent,
    }
    _write(rec)
    return rec


def public_view(rec: dict) -> dict:
    return {"id": rec["id"], "command": f"{rec['bundle']} {rec['command']}", "args": rec["args"],
            "summary": rec["summary"], "expires": rec["expires"]}


def check_for_use(aid: str, bundle: str, command: str, h: str, bh: str | None = None, *,
                  allow_test: bool = False) -> dict:
    """Verify a grant for this exact call: it matches, is signed, is unused and unexpired, and (when its scope is
    "test") the bundle accepts test-scope grants (`allow_test`, from `test_approvals_allowed`). Returns the record (its
    `scope` is the verified scope of the approver). Raises TogiError (refused / needs_approval) otherwise.

    "Unused" is decided by the spent-approval record outside `$TOGI_HOME` (spent.py) and the `.claimed` marker, never by
    the `status` field alone: that field is in a file the agent can edit."""
    rec = load(aid)
    st = effective_status(rec)
    nxt_new = None
    if rec["bundle"] != bundle or rec["command"] != command or rec["args_hash"] != h:
        raise TogiError("approval_mismatch",
                        f"approval {aid} was requested for a different command or different arguments",
                        next="re-run this command without --approval to request a new approval", status="refused")
    if bh is not None and rec.get("binding_hash") != bh:
        raise TogiError("approval_mismatch",
                        f"approval {aid} was requested for a different state of {bundle} (its repo path, manifest "
                        "version or locked files changed, or the approval predates that binding)",
                        next="re-run this command without --approval to request a new approval", status="refused")
    if st == "pending":
        raise TogiError("approval_pending", f"approval {aid} has not been granted yet",
                        next=f"a person runs: togi approve {aid}", status="needs_approval")
    verify_grant(rec)   # every decided record must be a genuine, current grant: forged or copied ones stop here
    if st == "denied":
        raise TogiError("approval_denied", f"approval {aid} was denied by a person",
                        next=nxt_new, status="refused")
    if st == "consumed":
        raise TogiError("approval_used", f"approval {aid} was already used; approvals are single-use",
                        next="re-run this command without --approval to request a new approval", status="refused")
    if _path(aid).with_suffix(".claimed").exists() or spent.is_spent(aid):
        raise TogiError("approval_used", f"approval {aid} was already used; approvals are single-use",
                        next="re-run this command without --approval to request a new approval", status="refused")
    if st == "expired":
        raise TogiError("approval_expired", f"approval {aid} has expired",
                        next="re-run this command without --approval to request a new approval", status="refused")
    if rec.get("scope") == "test" and not allow_test:
        raise test_scope_error(aid, bundle)
    return rec


def claim(aid: str) -> None:
    """Atomically take the single use, BEFORE the command runs. The signed spent-approval record outside `$TOGI_HOME` is
    the authority (spent.claim: under a lock, refuses an id already in it, fails closed if it cannot write); the
    `.claimed` marker beside the approval is a second, cheap witness. A second concurrent claimer fails."""
    spent.claim(aid)
    marker = _path(aid).with_suffix(".claimed")
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise TogiError("approval_used", f"approval {aid} was already used; approvals are single-use",
                        next="re-run this command without --approval to request a new approval", status="refused")
    os.close(fd)


def mark_consumed(aid: str, outcome: str) -> None:
    rec = load(aid)
    rec["status"] = "consumed"
    rec["consumed_at"] = now_ts()
    rec["outcome"] = outcome
    _write(rec)


def list_all(include_done: bool = False) -> list[dict]:
    d = paths.approvals_dir()
    out = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("ap_*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        st = effective_status(rec)
        if not include_done and st not in ("pending", "approved"):
            continue
        out.append({"id": rec["id"], "bundle": rec["bundle"], "command": rec["command"], "status": st,
                    "summary": rec["summary"], "expires": rec["expires"], "created": rec["created"]})
    return out


# --------------------------------------------------------------------------- the human-only path


def human_gate(action: str, *, repo: bool = False, migrate: bool = True) -> None:
    """Refuse unless a person is at a real terminal, in an isolated (or visibly clean) interpreter. `approve`/`deny` use
    it directly; `lock`, `revert` and `apply` pass `repo=True` (via `human_repo_gate`), which also demands the
    default-scope approver. An agent session is `TOGI_AGENT` being set at all (even empty); `TOGI_RUN` likewise.

    Once the person's checks have passed, r4's loose approver files are moved into `approver.d` (r5, `approver.migrate_legacy`):
    the migration happens at the first human command after the upgrade, never in an agent's session."""
    _person_checks(action)
    if repo:
        require_default_scope(action)
    require_clean_interpreter(action)
    paths.require_safe_layout()
    if migrate and paths.approver_override() is None:
        approver.migrate_legacy()


def _person_checks(action: str) -> None:
    if paths.is_agent():
        raise TogiError("human_only", f"`togi {action}` is for people; TOGI_AGENT is set so this was refused",
                        next="ask the person to run it in their own terminal", status="refused")
    if paths.in_run():
        raise TogiError("human_only", f"`togi {action}` cannot run from inside `togi run`",
                        next="ask the person to run it in their own terminal", status="refused")
    if not (_isatty(sys.stdin) and _isatty(sys.stdout)):
        raise TogiError("needs_tty", f"`togi {action}` needs an interactive terminal (stdin and stdout must be TTYs)",
                        next="ask the person to run it in their own terminal", status="refused")
    if runreg.started_by_togi_run():
        raise TogiError("human_only", f"`togi {action}` cannot run from inside `togi run` (a parent process is "
                                      "`togi run`)", next="ask the person to run it in their own terminal",
                        status="refused")


def interpreter_problem() -> str | None:
    """Why this interpreter must not host a human gate, or None. An ISOLATED interpreter (`python -I`, how the `togi`
    launcher starts everything) ignores PYTHON* variables and has neither the current directory nor the script
    directory on `sys.path`, so nothing planted in either can load. Any other interpreter is acceptable only while it is
    visibly clean: no PYTHON* variable set (PYTHONPATH, PYTHONSTARTUP, PYTHONHOME, PYTHONUSERBASE...) and neither `''`,
    `.` nor the current directory on `sys.path`."""
    if sys.flags.isolated:
        return None
    pyvars = sorted(k for k in os.environ if k.startswith("PYTHON"))
    if pyvars:
        return f"{', '.join(pyvars)} {'is' if len(pyvars) == 1 else 'are'} set, and this interpreter is not isolated"
    try:
        cwd = os.path.realpath(os.getcwd())
    except OSError:
        cwd = None
    for entry in sys.path:
        if entry in ("", ".") or (cwd is not None and os.path.realpath(entry) == cwd):
            return "the current directory is on sys.path, and this interpreter is not isolated"
    return None


def require_clean_interpreter(action: str) -> None:
    """The human gates run only in an isolated or visibly clean interpreter: a file planted in the directory the person
    stands in (a `hmac.py`, a `json.py`) or in PYTHONPATH must not run inside the session where they type a passphrase."""
    problem = interpreter_problem()
    if problem:
        raise TogiError("unsafe_interpreter", f"`togi {action}` was refused: {problem}, so a file planted in the current "
                                              "directory or on PYTHONPATH could run inside this approval",
                        next="start togi with its launcher (bin/togi, which runs python3 -I), not python3 -m togi or "
                             "a bare interpreter",
                        status="refused")


def require_default_scope(action: str) -> None:
    """The gates that change what a bundle's repo may do (`lock`, `revert`, evaluating a reshape proposal, a human
    `apply`) accept ONLY the default-scope approver: the fixed one in `<pw_dir>/.togi/approver.d`. Refused, always, while
    `$TOGI_APPROVER_HOME` is set: anyone can make a throwaway approver there with a pseudo-terminal, so its yes proves
    nothing about repo changes (an agent could edit a locked money file and `togi lock` it under its own approver).
    The refusal comes before any prompt, passphrase or expensive work, and names the way out."""
    if paths.approver_scope() == "default":
        return
    try:
        fixed = str(paths.fixed_approver_home())
    except TogiError:
        fixed = "<pw_dir>/.togi/approver.d"
    raise TogiError("test_scope_refused",
                    f"`togi {action}` changes what a bundle's repo may do, so only the default approver ({fixed}) may "
                    "authorize it; TOGI_APPROVER_HOME is set, which makes this a throwaway test-scope approver that "
                    "anyone can create. Nothing was changed",
                    next="a person runs it in their own terminal with TOGI_APPROVER_HOME unset", status="refused")


def human_repo_gate(action: str) -> None:
    """`human_gate` plus the default-scope rule: for every human action that changes a repo's locked state."""
    human_gate(action, repo=True)


def require_approver() -> None:
    """Fail early (before any prompt or expensive work) when no approver passphrase is set up."""
    approver.require_configured()


def _isatty(f) -> bool:
    try:
        return f.isatty()
    except (AttributeError, ValueError):
        return False


def type_confirmation(prompt_lines: list[str], word: str, action: str) -> str:
    """Show the lines (callers pass them already escaped), require the person to type `word`. Returns the user."""
    for ln in prompt_lines:
        sys.stdout.write(ln + "\n")
    sys.stdout.write(f'Type "{escape_text(word, 60)}" to {action}: ')
    sys.stdout.flush()
    try:
        typed = sys.stdin.readline().strip()
    except (OSError, ValueError):
        typed = ""
    if typed != word:
        raise TogiError("confirmation_mismatch", f"you typed {escape_text(typed, 60)!r}, not "
                                                 f"{escape_text(word, 60)!r}; nothing was changed",
                        next="run the command again and type the word exactly", status="refused")
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def confirm_and_authenticate(prompt_lines: list[str], word: str, action: str, pass_action: str | None = None) -> str:
    """The whole human step: show the prompt, typed confirmation word, then the approver passphrase."""
    user = type_confirmation(prompt_lines, word, action)
    approver.require(pass_action or action)
    return user


# --------------------------------------------------------------------------- what the person sees is what is signed

ARG_NAME_CAP = 60          # what the approval prompt shows of an argument's name ...
ARG_VALUE_CAP = 300        # ... and of its value (as JSON); an argument longer than this cannot be approved (L2)


def _tampered(aid: str, what: str) -> TogiError:
    return TogiError("approval_tampered",
                     f"approval {aid} does not match what it claims: {what}. Nothing was shown for approval and nothing "
                     "was signed", next="togi approvals   (the agent can re-run the command to request a fresh one)",
                     status="refused")


def gated_code_error(name: str, problems: list[str]) -> TogiError:
    shown = ", ".join(escape_text(x, 120) for x in problems[:5]) + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else "")
    return TogiError("gated_code_unlocked",
                     f"{name}: code that can run behind its gated commands is neither locked nor reshape: {shown}. A money "
                     "command only runs code a person locked, or code that needs a person to change",
                     next=f"add them to [tiers].locked (or reshape) in togi.toml, then a person runs: togi lock {name}",
                     status="refused")


def check_prompt_fits(aid: str, args: dict) -> None:
    """Refuse (`prompt_too_long`) an approval that has an argument the prompt would have to cut: the person cannot see what
    they are signing. Names are cut at ARG_NAME_CAP and values (as JSON) at ARG_VALUE_CAP characters."""
    for k, val in args.items():
        name, shown = str(k), json.dumps(val, ensure_ascii=False, default=str)
        if len(name) > ARG_NAME_CAP or len(shown) > ARG_VALUE_CAP:
            raise TogiError("prompt_too_long",
                            f"approval {aid} has an argument ({escape_text(name, 40)}) that is too long to show in full "
                            f"(limit {ARG_VALUE_CAP} characters per value); nothing was shown for approval and nothing was "
                            "signed, because you could not see what you were signing",
                            next="the agent re-runs the command with a shorter value, or a person runs the command's work "
                                 "another way", status="refused")


def lock_line(lock: dict, man: Any) -> str:
    """One line for the approval prompt: the lock state the money path sees."""
    st = lock["state"]
    if st == "verified":
        return f"lock: VERIFIED - {lock['detail']}, anchored in the signed audit log"
    if st == "absent":
        return ("lock: none, and this bundle does not require one ([policy] require_lock = false)"
                if not man.lock_required() else "lock: NONE - this bundle requires `togi lock` and has not been locked")
    return f"lock: FAILED - {lock['detail']}"


def verify_pending(aid: str, rec: dict) -> dict:
    """Everything `togi approve` is about to show, recomputed from live state instead of read from the pending file
    (which the agent that made the request can write). Raises TogiError (refused) on any mismatch with the record, and
    returns what the person is shown AND what `grant` signs:

      args, args_hash        the args to display, and their hash recomputed from them
      binding, binding_hash  repo path, manifest version and locked-files digest, recomputed from the live repo
      lock                   the lock state (audit.lock_state)
      summary, confirm_word  recomputed as pure data from the describe snapshot `togi lock` signed (describe.py), or, for a
                             bundle with no snapshot, from the hardened sandboxed child. The bundle is NOT imported here.

    Checked before the person is asked anything: the record is internally consistent (args really hash to args_hash), the
    bundle is registered, a test-scope approver is acceptable to it, the live bundle is still the one the request was made
    for, its lock does not fail, no argument is too long to show, the command exists, is gated and takes these arguments,
    the code behind it is covered by the lock, and the summary and word on file are the ones recomputed."""
    from . import audit, describe, registry
    from .manifest import ManifestError, load_manifest, locked_digest

    bundle, command, args = rec.get("bundle"), rec.get("command"), rec.get("args")
    if rec.get("id") != aid or not isinstance(bundle, str) or not isinstance(command, str) or not isinstance(args, dict):
        raise _tampered(aid, "its id, bundle, command or args are missing or malformed")
    ent = registry.entries().get(bundle)
    if ent is None:
        raise TogiError("unknown_bundle", f"approval {aid} is for bundle '{escape_text(bundle, 60)}', which is not "
                                          "registered here, so it cannot be checked against the live repo",
                        next="run togi approve from the project directory, or: togi add <path-to-bundle-repo>",
                        status="refused")
    try:
        man = load_manifest(ent.path)
    except ManifestError as e:
        raise TogiError("manifest_invalid", str(e), next="togi doctor", status="refused")
    if paths.approver_scope() == "test" and not test_approvals_allowed(man):
        raise test_scope_error(None, bundle)
    if args_hash(bundle, command, args) != rec.get("args_hash"):
        raise _tampered(aid, "the arguments shown do not hash to the args_hash the request was filed under (they were "
                             "edited after the request)")
    binding = {"repo": str(man.repo), "version": man.version, "locked_digest": locked_digest(man.repo, man)}
    bh = binding_hash(binding)
    if bh != rec.get("binding_hash"):
        raise TogiError("approval_stale", f"{bundle} is not the state the request was made for (its repo path, manifest "
                                          "version or a locked file changed); approving would sign a grant that could "
                                          "never be used", next="the agent re-runs the command to request a fresh "
                                                                "approval", status="refused")
    lock = audit.lock_state(man.repo, man)
    if lock["state"] == "failed" or (lock["state"] == "absent" and man.lock_required()):
        raise TogiError("lock_check_failed" if lock["state"] == "failed" else "not_locked",
                        f"{bundle}: {lock_line(lock, man)}. Its money commands are refused until the lock verifies",
                        next=f"review the change, then a person runs: togi lock {bundle}", status="refused")
    check_prompt_fits(aid, args)
    snap = describe.Snapshot("missing")
    if lock["state"] == "verified":
        snap = describe.read_snapshot(man, audit.sha256_text(audit._read_text(audit.lock_path(man.repo))))
        if snap.state == "invalid":
            raise TogiError("describe_snapshot_invalid", f"{bundle}: the describe snapshot .togi/describe.json cannot be "
                                                         f"trusted: {snap.why}", next=f"a person runs: togi lock {bundle}",
                            status="refused")
    if snap.ok:
        cmd = describe.views(snap.doc).get(command)
        roots, loaded = snap.doc["gated_roots"], snap.doc["loaded_files"]
        source = "the signed snapshot from `togi lock`"
    else:
        d = describe.command_in_child(man, command)     # hardened + sandboxed; the bundle is never imported in this process
        cmd = describe.view_of(d["command"]) if d.get("command") else None
        roots, loaded = d.get("gated_roots") or [], d.get("loaded_files") or []
        source = "a sandboxed read of the bundle (no signed snapshot: run `togi lock` to record one)"
    if cmd is None:
        raise _tampered(aid, f"{bundle} has no command {escape_text(command, 60)!r}"
                             + (" in its signed snapshot (the bundle's commands changed since the last lock?)" if snap.ok else ""))
    if cmd.effect not in man.gated_effects():
        raise _tampered(aid, f"{escape_text(command, 60)!r} is not a gated command of {bundle}")
    extra = sorted(k for k in args if k not in {p.name for p in cmd.params})
    if extra:
        raise _tampered(aid, f"it carries arguments the command does not take ({escape_text(', '.join(extra), 80)})")
    if man.lock_required():
        problems = codeset.coverage_problems(man.repo, man, roots, loaded)
        if problems:
            raise gated_code_error(bundle, problems)
    summary, word = summarize(bundle, cmd, args), confirm_word(args, cmd, aid)
    if summary != rec.get("summary") or word != rec.get("confirm_word"):
        raise _tampered(aid, "its summary or confirmation word is not the one the bundle's own command produces"
                             + (" (or the command changed since the last `togi lock`)" if snap.ok else ""))
    return {"args": args, "args_hash": rec["args_hash"], "binding": binding, "binding_hash": bh, "lock": lock,
            "summary": summary, "confirm_word": word, "lock_line": lock_line(lock, man), "describe_source": source}


def prompt_lines_for(rec: dict, verified: dict | None = None, expires: str | None = None) -> list[str]:
    """What a person sees before approving: every untrusted string is escaped, so nothing in a summary or an
    argument can move the cursor, recolour text or hide the real amount. With `verified` (from `verify_pending`) it
    is built from the recomputed values, not the pending file's, and the first line shows the expiry that WILL be signed
    (`expires`, else now + the TTL), never the field the requester wrote into the file (r5). Without it (a denial) the
    record is shown as filed and says so."""
    v = verified or {}
    b = v.get("binding") or rec.get("binding") or {}
    summary = v.get("summary", rec.get("summary", ""))
    args = v.get("args", rec.get("args", {}))
    if verified:
        when = escape_text(expires or _iso(time.time() + ttl_seconds()), 40)
        head = f"Approval {escape_text(rec['id'], 40)}  expires {when} if you approve"
    else:
        head = (f"Approval {escape_text(rec['id'], 40)}  request expires {escape_text(rec['expires'], 40)} "
                "(as the requester wrote it)")
    lines = [head,
             f"bundle: {escape_text(rec['bundle'], 60)}   repo: {escape_text(b.get('repo') or '(unknown)', 200)}   "
             f"version: {escape_text(b.get('version') or '?', 40)}",
             escape_text(summary, SUMMARY_CAP), "args:"]
    for k, val in args.items():
        shown = json.dumps(val, ensure_ascii=False, default=str)
        lines.append(f"  {escape_text(k, ARG_NAME_CAP)} = {escape_text(shown, ARG_VALUE_CAP)}")
    if v.get("lock_line"):
        lines.append(escape_text(v["lock_line"], 300))
    if v.get("describe_source"):
        lines.append(escape_text(f"command read from: {v['describe_source']}", 300))
    if not verified:
        lines.append("(shown as filed by the requester; not re-checked against the bundle)")
    if paths.approver_scope() == "test":
        lines.append("approver: TEST scope ($TOGI_APPROVER_HOME is set). A bundle that moves real value must refuse "
                     "this approval outside its own test mode.")
    return lines


def grant(aid: str, decision: str, by: str, ttl: int | None = None, expect_hash: str | None = None,
          verified: dict | None = None, expires: str | None = None) -> dict:
    """Record a person's decision. The caller (cli) has already passed human_gate + confirmation + passphrase.
    `expect_hash` is the args hash the person was shown: if the record changed since, nothing is granted.
    `verified` (from `verify_pending`, approve only) is what the person was actually shown and what is SIGNED: the
    record on disk is re-read, must still agree with it on every field the grant covers (bundle, command, args, args
    hash, binding hash, summary, confirmation word), and is then written from the recomputed values, not its own.
    `expires` (an ISO time) is the expiry the person was shown at the prompt, signed exactly; without it, now + the TTL."""
    rec = load(aid)
    st = effective_status(rec)
    if st != "pending":
        raise TogiError("approval_not_pending", f"approval {aid} is {st}, not pending",
                        next="togi approvals", status="refused")
    if expect_hash is not None and rec.get("args_hash") != expect_hash:
        raise TogiError("approval_changed", f"approval {aid} changed while you were reading it; nothing was granted",
                        next="togi approvals", status="refused")
    if verified is not None:
        shown = {"args": verified["args"], "args_hash": verified["args_hash"], "binding_hash": verified["binding_hash"],
                 "summary": verified["summary"], "confirm_word": verified["confirm_word"]}
        if any(rec.get(k) != v for k, v in shown.items()):
            raise TogiError("approval_changed", f"approval {aid} changed after it was checked; nothing was granted",
                            next="togi approvals", status="refused")
        rec.update(shown)
        rec["binding"] = {k: verified["binding"].get(k) for k in ("repo", "version", "locked_digest")}
    now = time.time()
    if decision == "approve":
        rec["status"] = "approved"
        rec["approved_at"] = now_ts()
        rec["expires"] = expires or _iso(now + (ttl if ttl else ttl_seconds()))
    else:
        rec["status"] = "denied"
        rec["denied_at"] = now_ts()
    rec["decided_by"] = f"human:{by}"
    rec["scope"] = paths.approver_scope()
    rec["grant_mac"] = sign_grant(rec)    # no key, no grant: nothing is written unsigned
    _write(rec)
    return rec
