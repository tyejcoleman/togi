"""Audit log (SPEC section 6) and locked-file hashes.

`<bundle-repo>/.togi/audit.jsonl` is a hash chain: every entry carries `prev_hash` and `hash` (sha256 over the
canonical JSON of the entry without `hash`/`mac`). Editing, deleting or reordering any entry breaks the chain.

Two things stop someone who can edit the repo from simply re-sealing a rewritten chain:

- **HMAC.** Every entry also carries `kid` (which key signed it) and `mac` = HMAC-SHA256 over its `hash` under
  the key in `$TOGI_HOME/audit.key` (0600). Without that key a rewritten entry cannot be given a valid mac.
- **Head pointer.** `$TOGI_HOME/state/<bundle>/audit.head` records the last seq + hash (also MAC'd). A log that
  is shorter than, or differs from, the recorded head is reported. A missing or unreadable head is a
  failure, not a skip.

`append` verifies the WHOLE chain and the head before it writes anything, and refuses (writing nothing) if
either is broken. `togi lock` is the one human action that may re-anchor a log whose head or key was lost.

Honest limit: whoever can read `$TOGI_HOME` has the key and the head and can rewrite everything. This is
tamper-evidence against the repo, not against the machine's own user.

`.togi/locked.sha256` holds the sha256 of every locked file (`togi lock`). `apply` and `doctor` re-check it.

Every entry carries `scope` ("default" | "test"): the scope of the approver in force in the process that wrote it
(paths.approver_scope()), inside the hash and the mac. The human gates behind `lock`, `revert`, `evaluate` and a human
`apply` refuse a test-scope approver (approvals.require_default_scope), so through the CLI those entries are always
"default"; "test" means a library call under a throwaway approver (the test-suite's `lock_bundle`). Entries written
before this field existed have none and still verify (the hash is over the keys an entry has).
"""
from __future__ import annotations

import fcntl
import getpass
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any

from . import approver, output, paths
from .cli import Reply, parse_flags, _mode, _usage
from .errors import TogiError
from .journal import now_ts
from .manifest import (Manifest, file_sha256, locked_digest, locked_files, locked_symlinks, scan_locked,  # noqa: F401  (re-exported)
                       walk_files)
from .pathnorm import escape_text

GENESIS = "0" * 64
ACTIONS = ("apply", "reject", "revert", "lock", "evaluate")
MAC_DOMAIN = "togi-audit-v1\n"
HEAD_DOMAIN = "togi-audit-head-v1\n"

# problem kinds: "chain" (content), "mac" (forged/edited), "head" (pointer), "key" (cannot authenticate)
HEAD_RECOVERABLE = ("missing", "unreadable")   # states a human `togi lock` may re-anchor


def audit_path(repo: Path) -> Path:
    return repo / ".togi" / "audit.jsonl"


def lock_path(repo: Path) -> Path:
    return repo / ".togi" / "locked.sha256"


def head_path(bundle: str) -> Path:
    return paths.bundle_state(bundle) / "audit.head"


# --------------------------------------------------------------------------- key, hash, mac


def load_key() -> bytes | None:
    """The audit HMAC key, or None if none exists yet. An unreadable/garbled key is an error, never a
    silent regeneration (that would let a broken home quietly start a fresh trust root)."""
    p = paths.audit_key_path()
    try:
        raw = p.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        raise TogiError("audit_key_unreadable", f"cannot read {p}: {e}", next=f"restore {p} from a backup",
                        status="refused")
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        key = b""
    if len(key) < 16:
        raise TogiError("audit_key_unreadable", f"{p} does not hold a valid key", next=f"restore {p} from a backup",
                        status="refused")
    return key


def ensure_key() -> bytes:
    key = load_key()
    if key is not None:
        return key
    p = paths.audit_key_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:  # another process won the race
        return load_key() or key
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(key.hex() + "\n")
    return key


def key_id(key: bytes) -> str:
    return hashlib.sha256(b"kid\n" + key).hexdigest()[:16]


def entry_hash(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k not in ("hash", "mac")}
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8", "surrogatepass")).hexdigest()


def entry_mac(key: bytes, entry_hash_hex: str) -> str:
    return hmac.new(key, (MAC_DOMAIN + entry_hash_hex).encode("ascii"), hashlib.sha256).hexdigest()


def head_mac(key: bytes, seq: int, hash_hex: str) -> str:
    return hmac.new(key, f"{HEAD_DOMAIN}{seq}\n{hash_hex}".encode("ascii"), hashlib.sha256).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def dump_line(entry: dict) -> str:
    """One audit line: pure ASCII (every non-ASCII character escaped), so no Unicode line or paragraph
    separator can ever sit raw inside a line and be mistaken for a line break by a reader."""
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"


def parse_log(text: str) -> list[dict]:
    """Entries of a log file's text. Splits on "\\n" ONLY (str.splitlines also breaks on U+2028, U+0085, \\x0b...,
    which would let one line be read as two). A line that is not a JSON object becomes a `_corrupt` marker."""
    out: list[dict] = []
    for line in text.split("\n"):
        if line == "":
            continue
        try:
            e = json.loads(line)
        except ValueError:
            out.append({"_corrupt": line[:80]})
            continue
        out.append(e if isinstance(e, dict) else {"_corrupt": line[:80]})
    return out


def _read_text(p: Path) -> str:
    # newline="" : no universal-newline translation, a raw \r is not turned into a line break
    with open(p, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def read(repo: Path) -> list[dict]:
    p = audit_path(repo)
    return parse_log(_read_text(p)) if p.is_file() else []


# --------------------------------------------------------------------------- verification


def _problem(seq: Any, kind: str, text: str) -> dict:
    return {"seq": seq, "kind": kind, "problem": text}


def check_entries(entries: list[dict], key: bytes | None) -> tuple[list[dict], dict]:
    """Chain + mac checks. Returns (problems, info). info: authenticated (bool), unauthenticated (int)."""
    problems: list[dict] = []
    prev = GENESIS
    kid = key_id(key) if key else None
    local_phase = False          # once an entry carries OUR kid, every later entry must too
    n_unauth = 0
    any_signed = any(isinstance(e, dict) and "mac" in e for e in entries)
    last_local = 0
    for idx, e in enumerate(entries, 1):
        if "_corrupt" in e:
            problems.append(_problem(idx, "chain", "line is not valid JSON"))
            prev = sha256_text(e["_corrupt"])
            continue
        if e.get("seq") != idx:
            problems.append(_problem(e.get("seq", idx), "chain", f"seq is {e.get('seq')}, expected {idx} "
                                                                  "(an entry was removed, added or reordered)"))
        if e.get("prev_hash") != prev:
            problems.append(_problem(e.get("seq", idx), "chain", "prev_hash does not match the previous entry"))
        if entry_hash(e) != e.get("hash"):
            problems.append(_problem(e.get("seq", idx), "chain", "hash does not match the entry's content "
                                                                  "(the entry was edited)"))
        prev = e.get("hash", "") if isinstance(e.get("hash"), str) else ""
        if key is None:
            n_unauth += 1
            continue
        mine = e.get("kid") == kid and "mac" in e
        if mine:
            local_phase = True
            last_local = idx
            good = isinstance(e.get("mac"), str) and hmac.compare_digest(
                e["mac"], entry_mac(key, e.get("hash") if isinstance(e.get("hash"), str) else ""))
            if not good:
                problems.append(_problem(e.get("seq", idx), "mac", "mac does not verify under this machine's audit "
                                                                     "key (the entry was edited or forged)"))
        elif local_phase:
            problems.append(_problem(e.get("seq", idx), "mac", "entry is not signed by this machine's audit key, "
                                                                 "but earlier entries were (a forged or stripped "
                                                                 "signature)"))
        else:
            n_unauth += 1
    if key is None and any_signed:
        problems.append(_problem(None, "key", f"entries are signed but {paths.audit_key_path()} does not exist, so "
                                              "they cannot be authenticated"))
    # "authenticated": this machine's key vouches for the log. At least one entry is signed by it and (checked above)
    # every entry after the first signed one is too; an unsigned PREFIX (a log older than signatures) is allowed and
    # counted in `unauthenticated`. A log with no signed entry at all is chain-bound only, never authenticated.
    info = {"authenticated": bool(entries) and key is not None and last_local > 0 and not problems,
            "unauthenticated": n_unauth, "signed": last_local > 0}
    return problems, info


def read_head(bundle: str, key: bytes | None) -> tuple[str, dict | None, dict | None]:
    """(state, head, problem) where state is none-expected: ok | missing | unreadable | forged."""
    hp = head_path(bundle)
    try:
        raw = hp.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None, _problem(None, "head", f"no head pointer at {hp} (new machine, or $TOGI_HOME was "
                                                       "wiped); `togi lock` re-anchors the log")
    except (OSError, UnicodeDecodeError):
        return "unreadable", None, _problem(None, "head", f"head pointer {hp} cannot be read")
    try:
        head = json.loads(raw)
        seq, hhash = int(head["seq"]), head["hash"]
        if not isinstance(hhash, str) or seq < 1:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return "unreadable", None, _problem(None, "head", f"head pointer {hp} is not valid")
    if "mac" in head:
        if key is None or not hmac.compare_digest(str(head["mac"]), head_mac(key, seq, hhash)):
            return "forged", head, _problem(seq, "head", "the head pointer's mac does not verify")
    return "ok", head, None


def verify_state(entries: list[dict], key: bytes | None, bundle: str | None) -> dict:
    """Everything `verify` and `append` need: chain, macs and head, as one result."""
    problems, info = check_entries(entries, key)
    head_state = "none"
    if bundle:
        state, head, prob = read_head(bundle, key)
        if state == "missing" and not entries:
            head_state = "none"            # nothing logged yet, nothing to anchor
        elif state != "ok":
            head_state = state
            problems.append(prob)
        else:
            hseq, hhash = int(head["seq"]), head["hash"]
            if len(entries) < hseq:
                problems.append(_problem(hseq, "head", f"log ends at seq {len(entries)} but the recorded head is "
                                                       f"seq {hseq} (entries were removed from the end)"))
                head_state = "truncated"
            elif entries[hseq - 1].get("hash") != hhash:
                problems.append(_problem(hseq, "head", "the entry at the recorded head differs from what was "
                                                       "written (the log was rewritten)"))
                head_state = "rewritten"
            else:
                head_state = "ok"
    return {"ok": not problems, "entries": len(entries), "problems": problems, "head": head_state,
            "authenticated": bool(info["authenticated"]) and head_state == "ok", "key": "present" if key else "missing",
            "unauthenticated_entries": info["unauthenticated"]}


def verify(repo: Path, bundle: str | None = None, *, chain_only: bool = False) -> dict:
    """Recompute the chain, macs and head. `chain_only` skips everything that needs $TOGI_HOME (for CI on a
    clean checkout): it checks the hash chain and nothing else, and says so."""
    entries = read(repo)
    if chain_only:
        problems, info = check_entries(entries, None)
        problems = [p for p in problems if p["kind"] == "chain"]
        return {"ok": not problems, "entries": len(entries), "problems": problems, "head": "skipped",
                "authenticated": False, "key": "skipped", "unauthenticated_entries": len(entries),
                "note": "chain_only: hashes checked, signatures and head pointer NOT checked"}
    return verify_state(entries, load_key(), bundle)


# --------------------------------------------------------------------------- append


def append(repo: Path, bundle: str, *, action: str, proposal: str | None, tier: str | None, files: list[str],
           diff_sha256: str | None, stone_before: dict | None, stone_after: dict | None, decided_by: str,
           reason: str, anchor: bool = False) -> dict:
    """Append one signed entry. Verifies the whole chain, the macs and the head pointer first; if any is
    broken it raises and writes nothing. `anchor=True` (only `togi lock`, a human with the approver
    passphrase) additionally accepts a log whose head pointer or key was lost, and re-anchors it."""
    if action not in ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    p = audit_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+", encoding="utf-8", newline="") as f:  # one handle: lock, verify, append, move the head
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            entries = parse_log(f.read())
            key = load_key()
            if key is None:
                if any("mac" in e for e in entries) and not anchor:
                    raise TogiError(
                        "audit_chain_broken",
                        f"refusing to write: the log has signed entries but {paths.audit_key_path()} is missing, so "
                        "they cannot be authenticated. Nothing was written.",
                        next=f"restore {paths.audit_key_path()}, or a person re-anchors the log with: "
                             f"togi lock {bundle}", status="refused")
                key = ensure_key()   # first use (or a human re-anchoring): start the trust root
            if entries and not anchor and not any("mac" in e for e in entries if isinstance(e, dict)):
                raise TogiError(
                    "audit_unauthenticated",
                    f"refusing to write: the audit log has {len(entries)} entr{'y' if len(entries) == 1 else 'ies'} "
                    "and none is signed (it predates signatures), so nothing here proves it was not rewritten. "
                    "Nothing was written.",
                    next=f"a person re-anchors it, after reading it, with: togi lock {bundle}", status="refused")
            kid = key_id(key)
            v = verify_state(entries, key, bundle)
            bad = v["problems"]
            if anchor:
                bad = [q for q in bad if not (q["kind"] == "head" and v["head"] in HEAD_RECOVERABLE)]
            if bad:
                q = bad[0]
                raise TogiError(
                    "audit_chain_broken",
                    f"refusing to write: the audit log is broken at seq {q['seq']}: {q['problem']}. Nothing was "
                    "written.", next=f"togi log {bundle} --verify  # then restore .togi/audit.jsonl from version "
                                     "control", status="refused")
            prev_hash, seq = GENESIS, 0
            if entries:
                last = entries[-1]
                prev_hash, seq = last["hash"], int(last["seq"])
            entry: dict[str, Any] = {
                "seq": seq + 1, "ts": now_ts(), "action": action, "proposal": proposal, "tier": tier,
                "files": list(files), "diff_sha256": diff_sha256, "stone_before": stone_before,
                "stone_after": stone_after, "decided_by": decided_by, "reason": reason,
                "scope": paths.approver_scope(), "prev_hash": prev_hash, "kid": kid}
            entry["hash"] = entry_hash(entry)
            entry["mac"] = entry_mac(key, entry["hash"])
            f.seek(0, os.SEEK_END)
            f.write(dump_line(entry))
            f.flush()
            os.fsync(f.fileno())
            _write_head(bundle, entry, key)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
    return entry


def _write_head(bundle: str, entry: dict, key: bytes) -> None:
    hp = head_path(bundle)
    hp.parent.mkdir(parents=True, exist_ok=True)
    head = {"seq": entry["seq"], "hash": entry["hash"], "kid": key_id(key),
            "mac": head_mac(key, entry["seq"], entry["hash"])}
    fd, tmp = tempfile.mkstemp(dir=hp.parent, prefix=".head-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(json.dumps(head, sort_keys=True))
    os.replace(tmp, hp)


# --------------------------------------------------------------------------- locked hashes


def lock_text(repo: Path, man: Manifest) -> tuple[list[str], str]:
    """(files, text of `.togi/locked.sha256`) for the lock set as it is now, WITHOUT writing anything. Refuses a repo that a
    lock cannot pin: a symlink where locked code is (`locked_symlink`) or a locked file name with control characters."""
    links = locked_symlinks(repo, man)
    if links:   # a link standing where locked code is would be reachable but never hashed
        raise TogiError("locked_symlink", "symlink(s) stand under the locked patterns, so the locked files cannot be "
                        "pinned: " + ", ".join(escape_text(x, 120) for x in links[:5]),
                        next="replace each link with the real file or remove it, then run: togi lock " + man.name,
                        status="refused")
    linkset = set(links)
    files = sorted(f for f in locked_files(repo, man) if f not in linkset)
    for rel in files:  # a name with a newline/separator would forge extra lines in the lock file
        if escape_text(rel) != rel or "\n" in rel:
            raise TogiError("unsafe_file_name", f"locked file name {escape_text(rel)!r} has control characters",
                            next="rename or delete that file, then run: togi lock " + man.name, status="refused")
    lines = [f"{file_sha256(repo / rel)}  {rel}" for rel in files]
    return files, "\n".join(lines) + ("\n" if lines else "")


def write_lock(repo: Path, man: Manifest) -> list[str]:
    files, text = lock_text(repo, man)
    lp = lock_path(repo)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(text, encoding="utf-8")
    return files


def read_recorded(repo: Path) -> dict[str, str] | None:
    """{path: sha256} from `.togi/locked.sha256`, or None when there is no lock file."""
    lp = lock_path(repo)
    if not lp.is_file():
        return None
    recorded: dict[str, str] = {}
    for line in _read_text(lp).split("\n"):
        if line:
            h, _, rel = line.partition("  ")
            recorded[rel] = h
    return recorded


def verify_locked(repo: Path, man: Manifest) -> dict:
    lp = lock_path(repo)
    recorded = read_recorded(repo)
    if recorded is None:
        return {"ok": False, "locked_file_present": False, "changed": [], "missing": [], "unlisted": [],
                "problem": "no .togi/locked.sha256"}
    changed, missing = [], []
    for rel, h in recorded.items():
        f = repo / rel
        if not f.is_file():
            missing.append(rel)
        elif file_sha256(f) != h:
            changed.append(rel)
    files_now, links = scan_locked(repo, man)
    unlisted = sorted(set(files_now) - set(recorded))
    anchored, anchor_problem = lock_anchored(repo, lp)
    ok = not (changed or missing or unlisted or links) and anchored
    out = {"ok": ok, "locked_file_present": True, "files": len(recorded), "changed": sorted(changed),
           "missing": sorted(missing), "unlisted": [escape_text(u, 120) for u in unlisted],
           "symlinks": [escape_text(x, 120) for x in links], "anchored": anchored}
    if anchor_problem:
        out["problem"] = anchor_problem
    return out


def lock_problems(v: dict) -> list[str]:
    """The files (or links) a failed `verify_locked` result names, for messages."""
    return list(dict.fromkeys(v.get("changed", []) + v.get("missing", []) + v.get("unlisted", [])
                              + v.get("symlinks", [])))


def lock_anchored(repo: Path, lp: Path) -> tuple[bool, str | None]:
    """The lock file is only trusted if it is exactly the one the latest `lock` audit entry recorded, AND that entry is
    genuine: its hash matches its content and it carries this machine's key id and a valid mac. Otherwise anyone who
    can write the repo could edit a locked file, rewrite the hash list by hand and append a `lock` line saying so."""
    try:
        entries = read(repo)
        key = load_key()
    except Exception as e:  # an unreadable log or key can't vouch for anything
        return False, f"audit log or key unreadable: {type(e).__name__}"
    locks = [e for e in entries if e.get("action") == "lock"]
    if not locks:
        return False, ".togi/locked.sha256 is not recorded by any `lock` entry in the audit log"
    last = locks[-1]
    if key is None:
        return False, f"there is no audit key ({paths.audit_key_path()}) to check the latest `lock` entry's signature with"
    h = last.get("hash")
    if (last.get("kid") != key_id(key) or not isinstance(h, str) or entry_hash(last) != h
            or not isinstance(last.get("mac"), str) or not hmac.compare_digest(last["mac"], entry_mac(key, h))):
        return False, "the latest `lock` entry in the audit log is not signed by this machine's audit key"
    if last.get("diff_sha256") != sha256_text(_read_text(lp)):
        return False, ".togi/locked.sha256 differs from the one the latest signed `lock` entry recorded"
    return True, None


def lock_state(repo: Path, man: Manifest) -> dict:
    """The lock as the MONEY path and the approval prompt need it. `state` is one of
      verified  the lock file exists, every locked file matches it, no link hides locked code, it is the one a signed
                `lock` entry recorded, and the audit log itself verifies (chain, macs, head)
      failed    a lock file exists and something is wrong (named in `detail`)
      absent    no .togi/locked.sha256 (whether that is acceptable is the manifest's `require_lock`)"""
    v = verify_locked(repo, man)
    if not v["locked_file_present"]:
        return {"state": "absent", "ok": False, "files": 0, "detail": "no .togi/locked.sha256"}
    if not v["ok"]:
        bad = lock_problems(v)[:5]
        detail = ("locked files differ from the lock: " + ", ".join(bad)) if bad else v.get("problem", "not anchored")
        return {"state": "failed", "ok": False, "files": v.get("files", 0), "detail": detail}
    problem = chain_problem(repo, man.name)
    if problem:
        return {"state": "failed", "ok": False, "files": v.get("files", 0), "detail": problem[1]}
    return {"state": "verified", "ok": True, "files": v.get("files", 0), "detail": f"{v.get('files', 0)} locked file(s) verified"}


def chain_problem(repo: Path, bundle: str) -> tuple[str, str] | None:
    """(code, message) if the audit log must not be relied on, else None. Requires the log to be intact AND
    authenticated by this machine's key (an empty log has nothing to rely on and is fine)."""
    v = verify(repo, bundle)
    if not v["ok"]:
        p0 = v["problems"][0]
        return "audit_chain_broken", f"the audit log is broken at seq {p0['seq']}: {p0['problem']}"
    if v["entries"] and not v["authenticated"]:
        return "audit_unauthenticated", (
            f"the audit log ({v['entries']} entr{'y' if v['entries'] == 1 else 'ies'}) is not authenticated: no entry "
            "is signed by this machine's audit key, so it could have been rewritten")
    return None


def require_trusted_chain(repo: Path, bundle: str, status: str = "error") -> None:
    """For apply and revert: raise unless the audit log verifies AND is authenticated (`authenticated`, see
    check_entries). The fix for an unsigned or foreign-signed log is a person's `togi lock`."""
    problem = chain_problem(repo, bundle)
    if problem:
        code, msg = problem
        raise TogiError(code, msg, next=f"togi log {bundle} --verify" if code == "audit_chain_broken"
                        else f"a person reads the log, then re-anchors it with: togi lock {bundle}", status=status)


def lock_bundle(man: Manifest, decided_by: str, *, snapshot: Any = None, expect_digest: str | None = None,
                allow_unsandboxed: bool = False) -> dict:
    """Record the locked hashes, the signed describe snapshot, and log it. Called only after a person authenticated
    (`togi lock`), or by tests.

    `snapshot`: None = read the bundle's commands now, in the hardened sandboxed child (describe.snapshot_in_child); if there
    is no OS sandbox on this machine none is recorded rather than running the bundle outside one, and `approve` reads the
    bundle in a sandbox when it needs to (never here: a library call does not run untrusted code unsandboxed). A
    `(body, info)` pair = already read (`togi lock` reads it before it asks). False = record none (a lock like r4's).
    `allow_unsandboxed`: read the bundle without an OS sandbox when there is none (a caller that is the person, e.g. a test
    harness standing in for one; `togi lock` asks for it with a typed word). `expect_digest`: the locked digest the person was
    shown; if the repo's lock set changed since, nothing is written
    (`repo_changed`). Nothing is written before the child has answered, so a bundle that cannot be read leaves the old lock."""
    from . import describe

    ensure_key()
    files, text = lock_text(man.repo, man)
    if expect_digest is not None and locked_digest(man.repo, man) != expect_digest:
        raise TogiError("repo_changed", f"the locked files of {man.name} changed while you were deciding; nothing was "
                                        "locked", next=f"read the changes again, then run: togi lock {man.name}",
                        status="refused")
    body, sandboxed = None, True
    if snapshot is None:
        try:
            body, info = describe.snapshot_in_child(man, allow_unsandboxed=allow_unsandboxed)
            sandboxed = bool(info.get("sandboxed"))
        except TogiError as e:
            if e.code != "describe_unsandboxed":
                raise
    elif snapshot is not False:
        body, info = snapshot
        sandboxed = bool(info.get("sandboxed"))
    lp = lock_path(man.repo)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(text, encoding="utf-8")
    entry = append(man.repo, man.name, action="lock", proposal=None, tier="locked", files=files,
                   diff_sha256=sha256_text(text), stone_before=None, stone_after=None, decided_by=decided_by,
                   reason=f"recorded {len(files)} locked file hash(es)", anchor=True)
    if body is not None:
        describe.write_snapshot(man, body, sha256_text(text), sandboxed=sandboxed)
    else:
        describe.remove_snapshot(man.repo)
    return {"locked": len(files), "file": str(lp), "audit_seq": entry["seq"], "describe_snapshot": body is not None,
            "commands": len(body["commands"]) if body else None}


# --------------------------------------------------------------------------- what the lock prompt shows (M2)

MAX_LISTED = 12


def _size(repo: Path, rel: str) -> str:
    try:
        return f"{(repo / rel).stat().st_size} bytes"
    except OSError:
        return "unreadable"


def lock_review(man: Manifest) -> dict:
    """What a person has to see before re-locking: against the LAST lock, the locked files that changed, went missing or are
    new (with sizes), the state of the audit head pointer, and the audit entries written since that lock. `needs_relock` is
    true when the typed word "relock" is also required: any of those files, a missing head pointer (this machine no longer
    remembers where the log ends) or a lock file that was deleted. `digest` is the locked digest at this moment: `lock_bundle`
    refuses to write if it differs when the person finally confirms. A first lock (nothing to compare with) needs no extra word."""
    repo = man.repo
    entries = read(repo)
    last = max((i for i, e in enumerate(entries) if e.get("action") == "lock"), default=None)
    since = entries[last + 1:] if last is not None else entries
    recorded = read_recorded(repo)
    files_now, links = scan_locked(repo, man)
    changed = missing = new = []
    lock_file_missing = recorded is None and last is not None
    if recorded is not None:
        changed = sorted(r for r, h in recorded.items() if (repo / r).is_file() and file_sha256(repo / r) != h)
        missing = sorted(r for r in recorded if not (repo / r).is_file())
        new = sorted(set(files_now) - set(recorded))
    try:
        state = verify_state(entries, load_key(), man.name)
        head, problems = state["head"], state["problems"]
    except TogiError as e:
        head, problems = "unreadable", [{"seq": None, "kind": "key", "problem": e.message}]
    return {"first": recorded is None and last is None, "lock_file_missing": lock_file_missing, "changed": changed,
            "missing": missing, "new": new, "head": head, "problems": problems, "since": since,
            "last_lock": entries[last] if last is not None else None,
            "needs_relock": bool(changed or missing or new or lock_file_missing or head in HEAD_RECOVERABLE),
            "digest": locked_digest(repo, man), "files": len(files_now)}


def lock_prompt_lines(man: Manifest, review: dict, snap: tuple[dict, dict] | None, gated_problems: list[str]) -> list[str]:
    repo = man.repo

    def listing(label: str, rels: list[str], sizes: bool) -> list[str]:
        if not rels:
            return []
        out = [f"  {label}: {len(rels)}"]
        for rel in rels[:MAX_LISTED]:
            out.append(f"    {escape_text(rel, 120)}" + (f"  ({_size(repo, rel)})" if sizes else ""))
        if len(rels) > MAX_LISTED:
            out.append(f"    ... and {len(rels) - MAX_LISTED} more")
        return out

    last = review["last_lock"]
    lines = [f"Lock bundle {escape_text(man.name, 60)}  ({escape_text(repo, 200)})",
             f"This records the sha256 of {review['files']} locked file(s) in .togi/locked.sha256, signs an audit entry and "
             "signs a snapshot of the bundle's commands (.togi/describe.json). Anything Python could import in place of "
             "a locked file counts as a locked file."]
    if review["first"]:
        lines.append("First lock: there is no earlier lock to compare with.")
    elif review["lock_file_missing"]:
        lines.append("WARNING: .togi/locked.sha256 is MISSING although this bundle was locked before "
                     f"(audit entry #{last.get('seq')}).")
    else:
        lines.append(f"Since the last lock (audit entry #{last.get('seq') if last else '?'}, "
                     f"{escape_text((last or {}).get('ts', '?'), 40)}):")
        body = (listing("changed", review["changed"], True) + listing("missing", review["missing"], False)
                + listing("new", review["new"], True))
        lines += body or ["  no locked file changed, went missing or is new"]
    head = review["head"]
    if head in HEAD_RECOVERABLE:
        lines.append(f"audit head pointer: {head.upper()} (this machine no longer remembers where the log ends; locking "
                     "re-anchors it, so read the log first: togi log " + man.name + ")")
    else:
        lines.append(f"audit head pointer: {escape_text(head, 20)}" + ("" if not review["problems"] else
                     f"; the log has {len(review['problems'])} problem(s): "
                     + escape_text(review["problems"][0].get("problem", "?"), 160)))
    since = review["since"]
    lines.append(f"audit entries since the last lock: {len(since)}")
    for e in since[-MAX_LISTED:]:
        lines.append("    #%s %s %s %s%s" % (escape_text(e.get("seq", "?"), 8), escape_text(e.get("ts", "?"), 30),
                                             escape_text(e.get("action", "?"), 12), escape_text(e.get("decided_by", "?"), 40),
                                             f" {escape_text(e['proposal'], 20)}" if e.get("proposal") else ""))
    if len(since) > MAX_LISTED:
        lines.append(f"    ... and {len(since) - MAX_LISTED} earlier")
    if snap is not None:
        body, info = snap
        gated = [c["name"] for c in body["commands"] if c["effect"] in man.gated_effects()]
        lines.append(f"commands read in a {info.get('level') or 'sandbox'} sandbox: {len(body['commands'])}"
                     + (f"; gated: {escape_text(', '.join(gated), 120)}" if gated else ""))
    if gated_problems and man.lock_required():
        lines.append("WARNING: code behind the gated commands is neither locked nor reshape, so their money path will "
                     f"refuse: {escape_text(', '.join(gated_problems[:5]), 200)}")
    lines.append("Do this only after reading the diff of every locked file that changed.")
    return lines


def current_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def refuse_agents(action: str) -> None:
    """Agent presence is `TOGI_AGENT` being SET (an empty value still counts), or `TOGI_RUN`."""
    if paths.is_agent() or paths.in_run():
        raise TogiError("human_only", f"`togi {action}` is for people; an agent session (TOGI_AGENT/togi run) "
                                      "cannot run it", next="ask the person to run it in their own terminal",
                        status="refused")


# --------------------------------------------------------------------------- CLI


def h_lock(argv: list[str]) -> Reply:
    from . import approvals, codeset, describe, registry

    pos, f = parse_flags(argv, {}, "lock")
    if f["help"]:
        return _usage("lock")
    if len(pos) != 1:
        raise TogiError("bad_arg", "togi lock needs a bundle name", next="togi bundles")
    approvals.human_repo_gate("lock")
    man = registry.manifest_for(pos[0])
    approvals.require_approver()
    links = locked_symlinks(man.repo, man)
    if links:    # say so before asking for anything: such a repo cannot be locked
        raise TogiError("locked_symlink", "symlink(s) stand under the locked patterns, so the locked files cannot be "
                        "pinned: " + ", ".join(escape_text(x, 120) for x in links[:5]),
                        next="replace each link with the real file or remove it, then run: togi lock " + man.name,
                        status="refused")
    review = lock_review(man)
    for q in review["problems"]:     # a log that was edited cannot be re-anchored by anybody: say so before asking for anything
        if q["kind"] in ("chain", "mac"):
            raise TogiError("audit_chain_broken", f"refusing to lock: the audit log is broken at seq {q['seq']}: "
                                                  f"{q['problem']}. Nothing was written.",
                            next=f"togi log {man.name} --verify  # then restore .togi/audit.jsonl from version control",
                            status="refused")
    # Read the bundle's commands NOW, in the sandboxed child, so the prompt can say what is being vouched for. Where there is
    # no OS sandbox the person is told so and asked for one more word, and the read happens only after they agreed.
    snap, unsandboxed = None, False
    try:
        snap = describe.snapshot_in_child(man)
    except TogiError as e:
        if e.code != "describe_unsandboxed":
            raise
        unsandboxed = True
    problems = codeset.coverage_problems(man.repo, man, snap[0]["gated_roots"], snap[0]["loaded_files"]) if snap else []
    lines = lock_prompt_lines(man, review, snap, problems)
    if unsandboxed:
        lines.append("NO OS SANDBOX on this machine: to record the commands Togi must import the bundle outside one (own "
                     "process, scrubbed environment, temporary directory; its code can still read what you can).")
    user = approvals.type_confirmation(lines, man.name, "lock")
    if review["needs_relock"]:
        approvals.type_confirmation(
            ["The locked files listed above differ from the last lock, or the audit head pointer is missing."],
            "relock", "relock after reading what changed")
    if unsandboxed:
        approvals.type_confirmation(["Importing the bundle without an OS sandbox."], "unsandboxed",
                                    "let the bundle be imported outside a sandbox")
    approver.require("lock")
    if unsandboxed:
        snap = describe.snapshot_in_child(man, allow_unsandboxed=True)
    data = lock_bundle(man, f"human:{user}", snapshot=snap, expect_digest=review["digest"])
    return Reply(env=output.ok_envelope(data), mode=_mode(f))


def h_log(argv: list[str]) -> Reply:
    from . import registry

    pos, f = parse_flags(argv, {"verify": "bool", "chain-only": "bool"}, "log")
    if f["help"]:
        return _usage("log")
    names = pos[:1] or sorted(registry.entries())
    if not names:
        raise TogiError("no_bundles", "no bundles are registered", next="togi add <path>")
    if f["verify"]:
        results = {}
        ok = True
        for n in names:
            man = registry.manifest_for(n)
            v = verify(man.repo, man.name, chain_only=f["chain_only"])
            results[n] = v
            ok = ok and v["ok"]
        data = results[names[0]] if pos else results
        if ok:
            return Reply(env=output.ok_envelope(data), mode=_mode(f))
        bad = [n for n, v in results.items() if not v["ok"]]
        first = results[bad[0]]["problems"][0]
        return Reply(env=output.error_envelope(
            "error", "audit_chain_broken",
            f"{bad[0]}: audit chain broken at seq {first['seq']}: {first['problem']}",
            next=f"inspect {registry.manifest_for(bad[0]).repo}/.togi/audit.jsonl and restore it from version "
                 "control (a person can re-anchor a log whose head or key was lost with: togi lock " + bad[0] + ")",
            data=data), mode=_mode(f))
    rows = []
    for n in names:
        man = registry.manifest_for(n)
        for e in read(man.repo):
            rows.append({"bundle": n, **{k: e.get(k) for k in (
                "seq", "ts", "action", "proposal", "tier", "files", "decided_by", "scope", "reason")}})
    limit = f["limit"] or output.DEFAULT_LIMIT
    total = len(rows)
    view = rows[-limit:]
    if f["fields"]:
        view = output.project(view, output.parse_fields(f["fields"]))
    env = output.ok_envelope(view)
    if total > len(view):
        env["total"] = total
        env["more"] = f"togi log {pos[0] if pos else ''} --limit {total}".replace("  ", " ")
    return Reply(env=env, mode=_mode(f), paged=True, projected=True)
