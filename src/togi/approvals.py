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

from . import approver, paths, runreg
from .errors import TogiError
from .journal import now_ts, parse_ts
from .pathnorm import escape_text, printable_token

DEFAULT_TTL_S = 15 * 60
MAX_PENDING_PER_BUNDLE = 20
SUMMARY_CAP = 300


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


def summarize(bundle: str, command: Any, args: dict) -> str:
    shown = ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, [], False))
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


def check_for_use(aid: str, bundle: str, command: str, h: str, bh: str | None = None) -> dict:
    """Verify a grant for this exact call: it matches, is signed, is unused and unexpired. Returns the record (its
    `scope` is the verified scope of the approver). Raises TogiError (refused / needs_approval) otherwise."""
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
    if st == "expired":
        raise TogiError("approval_expired", f"approval {aid} has expired",
                        next="re-run this command without --approval to request a new approval", status="refused")
    return rec


def claim(aid: str) -> None:
    """Atomically take the single use. A second concurrent claimer fails."""
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


def human_gate(action: str) -> None:
    """Refuse unless a person is at a real terminal. `approve`/`deny` use it directly; `lock`, `revert` and `apply`
    use `human_repo_gate`, which also demands the default-scope approver.
    An agent session is `TOGI_AGENT` being set at all (even empty); `TOGI_RUN` likewise."""
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


def require_default_scope(action: str) -> None:
    """The gates that change what a bundle's repo may do (`lock`, `revert`, evaluating a reshape proposal, a human
    `apply`) accept ONLY the default-scope approver: the fixed one in `<pw_dir>/.togi`. Refused, always, while
    `$TOGI_APPROVER_HOME` is set: anyone can make a throwaway approver there with a pseudo-terminal, so its yes proves
    nothing about repo changes (an agent could edit a locked money file and `togi lock` it under its own approver).
    The refusal comes before any prompt, passphrase or expensive work, and names the way out."""
    if paths.approver_scope() == "default":
        return
    try:
        fixed = str(paths.fixed_approver_home())
    except TogiError:
        fixed = "<pw_dir>/.togi"
    raise TogiError("test_scope_refused",
                    f"`togi {action}` changes what a bundle's repo may do, so only the default approver ({fixed}) may "
                    "authorize it; TOGI_APPROVER_HOME is set, which makes this a throwaway test-scope approver that "
                    "anyone can create. Nothing was changed",
                    next="a person runs it in their own terminal with TOGI_APPROVER_HOME unset", status="refused")


def human_repo_gate(action: str) -> None:
    """`human_gate` plus the default-scope rule: for every human action that changes a repo's locked state."""
    human_gate(action)
    require_default_scope(action)


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


def prompt_lines_for(rec: dict) -> list[str]:
    """What a person sees before approving: every untrusted string is escaped, so nothing in a summary or an
    argument can move the cursor, recolour text or hide the real amount."""
    b = rec.get("binding") or {}
    lines = [f"Approval {escape_text(rec['id'], 40)}  expires {escape_text(rec['expires'], 40)}",
             f"bundle: {escape_text(rec['bundle'], 60)}   repo: {escape_text(b.get('repo') or '(unknown)', 200)}   "
             f"version: {escape_text(b.get('version') or '?', 40)}",
             escape_text(rec["summary"], SUMMARY_CAP), "args:"]
    for k, v in rec["args"].items():
        val = json.dumps(v, ensure_ascii=False, default=str)
        lines.append(f"  {escape_text(k, 60)} = {escape_text(val, 300)}")
    if paths.approver_scope() == "test":
        lines.append("approver: TEST scope ($TOGI_APPROVER_HOME is set). A bundle that moves real value must refuse "
                     "this approval outside its own test mode.")
    return lines


def grant(aid: str, decision: str, by: str, ttl: int | None = None, expect_hash: str | None = None) -> dict:
    """Record a person's decision. The caller (cli) has already passed human_gate + confirmation + passphrase.
    `expect_hash` is the args hash the person was shown: if the record changed since, nothing is granted."""
    rec = load(aid)
    st = effective_status(rec)
    if st != "pending":
        raise TogiError("approval_not_pending", f"approval {aid} is {st}, not pending",
                        next="togi approvals", status="refused")
    if expect_hash is not None and rec.get("args_hash") != expect_hash:
        raise TogiError("approval_changed", f"approval {aid} changed while you were reading it; nothing was granted",
                        next="togi approvals", status="refused")
    now = time.time()
    if decision == "approve":
        rec["status"] = "approved"
        rec["approved_at"] = now_ts()
        rec["expires"] = _iso(now + (ttl if ttl else ttl_seconds()))
    else:
        rec["status"] = "denied"
        rec["denied_at"] = now_ts()
    rec["decided_by"] = f"human:{by}"
    rec["scope"] = paths.approver_scope()
    rec["grant_mac"] = sign_grant(rec)    # no key, no grant: nothing is written unsigned
    _write(rec)
    return rec
