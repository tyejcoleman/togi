"""The approval desk: a person approves a money request on a page served by a program THEY started, instead of with
`togi approve` in a terminal. A second door to the same signed grant, built to be no weaker than the first.

What the terminal path proves (approvals.human_gate, cli._decide): this is not an agent's session (TOGI_AGENT and TOGI_RUN
unset, no `togi run` ancestor), a person is at a real terminal, the interpreter is isolated or visibly clean, $TOGI_HOME
and the approver's directory are apart, an approver is set up. Then `verify_pending` recomputes what is shown from the
live repo and the bundle's signed snapshot, the person types the confirmation word and the approver passphrase (scrypt
record, 5 wrong = a 10-minute lock, every attempt journaled), and `grant` signs what was shown, with the expiry shown.

The desk keeps every one of those, moved to where a page needs them:

  Desk.open()   once, as the host program starts, on the program's own process: refused under TOGI_AGENT or TOGI_RUN
                (set at all, even empty), under a live `togi run` ancestor, in an interpreter that is neither isolated nor
                visibly clean, with an unsafe layout, or with no approver; and, for the person's own (default-scope)
                approver, unless stdin and stdout are a terminal (the person started it from theirs). A desk that did not
                open approves nothing, so the host offers no way to approve on its pages. `commands` names the only
                (bundle, command) pairs it takes.
  review()      each time the page shows a request: `verify_pending`, exactly as `togi approve` runs it before it prompts,
                and the expiry that WILL be signed, fixed now. The review lives in this process only, under a random id,
                for REVIEW_TTL_S (the terminal's passphrase timeout). `lines` are the terminal's own prompt lines.
  approve()     the person's yes, with the passphrase they typed into the page: the review must be this desk's, unexpired
                and for this id; the request must still be pending; `verify_pending` runs again and must give the same
                signed fields (else `approval_changed`, nothing granted); the passphrase goes to `approver.require` (same
                record, attempt counter, lock-out and journal as the terminal: a wrong guess here counts there too);
                `grant` signs exactly what was reviewed, with the reviewed expiry, and `decided_by` (signed) names the desk.

The confirmation word: the terminal makes a person type it so the request they approve is the one they read. On the desk
the yes is bound to the review the person was shown (the page sends the review id, and approve() refuses any change since),
and the page sends the word it was shown, which must be the recomputed one; it is not retyped.

The passphrase: an argument to approve(), handed to `approver.require` and nothing else: never stored, logged, journaled
or returned. The host must keep it off everything else too (its request log, its error messages, any agent's context).

Honest limits. Whoever holds a live review id AND the passphrase can approve: the review id exists in this process only,
and the host must serve it only to the person's own page (crowdweb: behind the maker key, same origin, never to the agent
it runs). A test-scope approver (TOGI_APPROVER_HOME) is anyone's to create and knows its own passphrase, so the terminal
check is not asked of it; as on the terminal path, the bundle decides what such a yes is worth (Crowd honours one only on
its pretend PayPal). The same-user limits in approvals.py apply unchanged: a hostile process with the person's full file
access can sign a grant itself, desk or no desk.
"""
from __future__ import annotations

import getpass
import secrets
import sys
import threading
import time
from typing import Iterable

from . import approvals, approver, paths, runreg
from .errors import TogiError
from .pathnorm import escape_text, printable_token

REVIEW_TTL_S = approver.PASSPHRASE_TIMEOUT_S     # a review lapses like the terminal's passphrase prompt does
MAX_REVIEWS = 16                                 # live reviews per desk; the oldest goes first
MAX_PASSPHRASE = 1024
SIGNED = ("args", "args_hash", "binding_hash", "summary", "confirm_word", "lock_line", "describe_source")
NEXT_PERSON = "the person starts the program in their own terminal (not from an agent, not inside togi run)"


def _journal(command: str, ok: bool, status: str, code: str | None, host: str) -> None:
    """Host-level journal line for every desk event: argv-free, value-free (no ids, no amounts, never the passphrase)."""
    from . import journal

    journal.append(None, journal.make_entry(surface="desk", bundle=None, command=command, ok=ok, status=status,
                                            error_code=code, effect="admin", desk_host=host,
                                            approver_scope=paths.approver_scope()))


def _person_started(host: str) -> None:
    """The terminal's person checks (approvals._person_checks), applied to the process that hosts the desk."""
    if paths.is_agent():
        raise TogiError("human_only", f"the approval desk does not open in an agent's session: TOGI_AGENT is set for "
                                      f"{escape_text(host, 40)}", next=NEXT_PERSON, status="refused")
    if paths.in_run():
        raise TogiError("human_only", "the approval desk does not open inside `togi run` (TOGI_RUN is set)",
                        next=NEXT_PERSON, status="refused")
    if paths.approver_scope() == "default" and not (approvals._isatty(sys.stdin) and approvals._isatty(sys.stdout)):
        raise TogiError("needs_tty", "the approval desk opens for your own approver only in a program started from an "
                                     "interactive terminal (stdin and stdout must be TTYs)", next=NEXT_PERSON,
                        status="refused")
    if runreg.started_by_togi_run():
        raise TogiError("human_only", "the approval desk does not open under `togi run` (a parent process is `togi run`)",
                        next=NEXT_PERSON, status="refused")


def _clean_interpreter() -> None:
    problem = approvals.interpreter_problem()
    if problem:
        raise TogiError("unsafe_interpreter", f"the approval desk was refused: {problem}, so a file planted in the current "
                                              "directory or on PYTHONPATH could run in the process that receives the "
                                              "passphrase", next="start the program in an isolated interpreter "
                                                                 "(python3 -I with its paths set by its launcher)",
                        status="refused")


class Desk:
    """One per host process. Make it with `Desk.open(...)`; a desk that could not open raises and does not exist."""

    def __init__(self, host: str, commands: frozenset, scope: str, _opened: object = None):
        if _opened is not _OPENED:
            raise TogiError("bad_arg", "use Desk.open(): a desk is only made by its checks", status="refused")
        self.host, self.commands, self.scope = host, commands, scope
        self._reviews: dict[str, dict] = {}
        self._lock = threading.Lock()          # the review table
        self._deciding = threading.Lock()      # one decision at a time on this desk

    # ---- opening ------------------------------------------------------------------------------------------------

    @classmethod
    def open(cls, host: str, commands: Iterable[tuple[str, str]]) -> "Desk":
        """Open the desk for this process, or raise TogiError (refused) saying why not. `host` names the program (shown in
        the journal and signed into `decided_by`); `commands` the (bundle, command) pairs it may approve."""
        host = host if isinstance(host, str) and printable_token(host.replace(" ", "-"), 40) else "desk"
        cmds = frozenset((str(b), str(c)) for b, c in commands)
        try:
            if not cmds:
                raise TogiError("bad_arg", "an approval desk must name the commands it approves", status="refused")
            _person_started(host)
            _clean_interpreter()
            paths.require_safe_layout()
            approver.require_configured()
        except TogiError as e:
            _journal("desk open", False, e.status, e.code, host)
            raise
        _journal("desk open", True, "ok", None, host)
        return cls(host, cmds, paths.approver_scope(), _OPENED)

    def status(self) -> dict:
        return {"open": True, "host": self.host, "scope": self.scope,
                "commands": sorted(f"{b} {c}" for b, c in self.commands), "review_seconds": REVIEW_TTL_S}

    # ---- the checks every call repeats ---------------------------------------------------------------------------

    def _still_a_person(self) -> None:
        """The environment the desk opened in must still hold: a host that later marks its own process as an agent's
        (or switches approver) has no desk."""
        if paths.is_agent() or paths.in_run():
            raise TogiError("human_only", "this process is now marked as an agent's (TOGI_AGENT or TOGI_RUN is set); the "
                                          "approval desk is closed", next=NEXT_PERSON, status="refused")
        if paths.approver_scope() != self.scope:
            raise TogiError("desk_scope_changed", "the approver this desk opened with is no longer the one in use; the "
                                                  "approval desk is closed", next=NEXT_PERSON, status="refused")

    def _for_this_desk(self, rec: dict) -> None:
        if (rec.get("bundle"), rec.get("command")) not in self.commands:
            raise TogiError("desk_not_for_this", f"this approval desk approves only {', '.join(sorted(f'{b} {c}' for b, c in self.commands))}",
                            next=f"a person runs: togi approve {escape_text(str(rec.get('id', '')), 40)}",
                            status="refused")

    @staticmethod
    def _pending(aid: str, rec: dict) -> None:
        st = approvals.effective_status(rec)
        if st == "expired":
            raise TogiError("approval_expired", f"approval {aid} has expired; nothing was granted",
                            next="the agent re-runs the command to request a fresh approval", status="refused")
        if st != "pending":
            raise TogiError("approval_not_pending", f"approval {aid} is {st}, not pending", next="togi approvals",
                            status="refused")

    # ---- reviewing ------------------------------------------------------------------------------------------------

    def review(self, aid: str) -> dict:
        """What the person is about to sign, recomputed from live state, and the review id the page sends back with the
        yes. Raises TogiError (refused) if the request cannot be approved here."""
        try:
            self._still_a_person()
            rec = approvals.load(aid)
            self._for_this_desk(rec)
            self._pending(aid, rec)
            verified = approvals.verify_pending(aid, rec)
        except TogiError as e:
            _journal("desk review", False, e.status, e.code, self.host)
            raise
        expires = approvals._iso(time.time() + approvals.ttl_seconds())
        rid = "rv_" + secrets.token_urlsafe(18)
        now = time.monotonic()
        with self._lock:
            self._reviews = {k: v for k, v in self._reviews.items() if v["until"] > now}
            while len(self._reviews) >= MAX_REVIEWS:
                self._reviews.pop(min(self._reviews, key=lambda k: self._reviews[k]["until"]))
            self._reviews[rid] = {"aid": aid, "verified": verified, "expires": expires, "until": now + REVIEW_TTL_S}
        _journal("desk review", True, "ok", None, self.host)
        b = verified["binding"]
        return {"review": rid, "approval": aid, "bundle": rec["bundle"], "command": rec["command"],
                "args": verified["args"], "summary": verified["summary"], "confirm_word": verified["confirm_word"],
                "repo": b.get("repo"), "version": b.get("version"), "lock": verified["lock_line"],
                "read_from": verified["describe_source"], "scope": self.scope, "expires_if_approved": expires,
                "review_seconds": REVIEW_TTL_S, "lines": approvals.prompt_lines_for(rec, verified, expires)}

    def _review(self, rid: object, aid: str) -> dict:
        now = time.monotonic()
        with self._lock:
            r = self._reviews.get(rid) if isinstance(rid, str) else None
            if r is not None and r["until"] <= now:
                self._reviews.pop(rid, None)
                raise TogiError("review_expired", "the page showed this request too long ago; look at it again, then "
                                                  "approve. Nothing was granted", next="review it again",
                                status="refused")
        if r is None:
            raise TogiError("review_unknown", "this approval was not reviewed on this desk (or the program restarted); "
                                              "nothing was granted", next="review it again", status="refused")
        if r["aid"] != aid:
            raise TogiError("approval_mismatch", f"the review shown was for approval {escape_text(r['aid'], 40)}, not "
                                                 f"{escape_text(aid, 40)}; nothing was granted", next="review it again",
                            status="refused")
        return r

    def _forget(self, rid: str) -> None:
        with self._lock:
            self._reviews.pop(rid, None)

    # ---- approving ------------------------------------------------------------------------------------------------

    def approve(self, aid: str, review: str, passphrase: str, word: str | None = None) -> dict:
        """Grant approval `aid`, as reviewed under `review`, if `passphrase` is the approver's. `word` is the confirmation
        word the page showed (checked against the recomputed one when given). Returns the decision (never the
        passphrase); raises TogiError (refused) and grants nothing otherwise. A wrong passphrase keeps the review, so the
        person can try again while it lasts; any change to the request discards it."""
        try:
            with self._deciding:
                self._still_a_person()
                if not isinstance(passphrase, str) or not passphrase or len(passphrase) > MAX_PASSPHRASE:
                    raise TogiError("passphrase_missing", "type the approver passphrase; nothing was granted",
                                    next="type it and approve again", status="refused")
                r = self._review(review, aid)
                rec = approvals.load(aid)
                try:
                    self._for_this_desk(rec)
                    self._pending(aid, rec)
                    if word is not None and word != r["verified"]["confirm_word"]:
                        raise TogiError("confirmation_mismatch", "the request shown is not the one being approved; "
                                                                 "nothing was granted", next="review it again",
                                        status="refused")
                    if rec.get("args_hash") != r["verified"]["args_hash"]:
                        raise TogiError("approval_changed", f"approval {aid} changed after it was shown to you; nothing "
                                                            "was granted", next="review it again", status="refused")
                    verified = approvals.verify_pending(aid, rec)
                    if any(verified.get(k) != r["verified"].get(k) for k in SIGNED):
                        raise TogiError("approval_changed", f"approval {aid} is no longer what was shown to you (the "
                                                            "request, the bundle or its lock changed); nothing was "
                                                            "granted", next="review it again", status="refused")
                except TogiError:
                    self._forget(review)
                    raise
                approvals.require_approver()
                approver.require("approve", reader=lambda _prompt: passphrase, surface="desk")
                try:
                    user = getpass.getuser()
                except Exception:  # noqa: BLE001
                    user = "unknown"
                try:
                    rec = approvals.grant(aid, "approve", f"{user} (approval desk: {self.host})",
                                          expect_hash=r["verified"]["args_hash"], verified=verified, expires=r["expires"])
                finally:
                    self._forget(review)
        except TogiError as e:
            _journal("approve", False, e.status, e.code, self.host)
            raise
        _journal("approve", True, "ok", None, self.host)
        return {"approval": rec["id"], "status": rec["status"], "expires": rec["expires"],
                "decided_by": rec["decided_by"], "scope": rec["scope"]}


_OPENED = object()


def open_or_reason(host: str, commands: Iterable[tuple[str, str]]) -> tuple[Desk | None, dict | None]:
    """For hosts: (desk, None) when it opened, else (None, {"code", "message", "next"}) to show the person why not."""
    try:
        return Desk.open(host, commands), None
    except TogiError as e:
        return None, {"code": e.code, "message": e.message, "next": e.next}
