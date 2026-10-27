"""The spent-approval record: what makes an approval single-use even if the approval file is edited.

An approval is a JSON file in `$TOGI_HOME/approvals/`, and `$TOGI_HOME` is somewhere the agent can write (and can even
choose, with an environment variable). Its `grant_mac` covers "approved", not "approved and not yet used": flipping
`status` from `consumed` back to `approved` and deleting the `.claimed` marker would leave a validly signed grant that
works again. So the authoritative record of use lives OUTSIDE `$TOGI_HOME`, in the approver home (the fixed
`<pw_dir>/.togi/approver.d`, or the `TOGI_APPROVER_HOME` override of a test run), next to the key that signs grants:

    spent.jsonl   one line per spent approval, appended under an exclusive flock
    spent.head    {n, mac}: how many lines there should be and the last mac, so a truncated log is noticed

Every line carries `n` (1, 2, 3...), the approval `id`, a timestamp, the `$TOGI_HOME` it was spent in, `prev` (the previous
line's mac) and `mac` = HMAC-SHA256(approver.key, canonical{n, id, ts, home, prev}). The macs chain, so removing, editing
or reordering a line breaks the next one. Any such damage is FAIL-CLOSED: every approval is refused (`spent_log_damaged`)
until a person looks, because a log that may have lost a line cannot say an approval is unused. A line that is not
parseable as a whole record (a torn write) is ignored: it cannot un-spend anything.

`claim()` is the single atomic step: under the lock it verifies the log, refuses an id already in it, and appends. The host
calls it BEFORE the command runs, and refuses to run the command if it cannot be written.

Honest limit: a process running as the same user that can write the approver home can delete `spent.jsonl` and
`spent.head` together and replay a grant that is still inside its (15 minute) lifetime; nothing stored on the machine's
own disk can stop that. What this stops is the cheap attacks: editing the approval file, deleting its marker, or using a
copy of the grant from another `$TOGI_HOME` (the grant is also bound to the `$TOGI_HOME` it was made in).
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import tempfile
from pathlib import Path

from . import approver, paths
from .errors import TogiError
from .journal import now_ts

GENESIS = "0" * 64
DOMAIN = "togi-spent-v1\n"


def log_path() -> Path:
    return paths.approver_home() / "spent.jsonl"


def head_path() -> Path:
    return paths.approver_home() / "spent.head"


def _mac(key: bytes, n: int, aid: str, ts: str, home: str, prev: str) -> str:
    body = json.dumps({"n": n, "id": aid, "ts": ts, "home": home, "prev": prev}, sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True)
    return hmac.new(key, (DOMAIN + body).encode("ascii"), hashlib.sha256).hexdigest()


def _head_mac(key: bytes, n: int, last: str) -> str:
    return hmac.new(key, f"{DOMAIN}head\n{n}\n{last}".encode("ascii"), hashlib.sha256).hexdigest()


def _damaged(why: str) -> TogiError:
    return TogiError("spent_log_damaged", f"the spent-approval record {log_path()} cannot be trusted ({why}), so no "
                                          "approval can be shown to be unused and every approval is refused",
                     next="a person inspects the approver home; to start a fresh record run: togi approver setup",
                     status="refused")


def _scan(key: bytes, text: str) -> tuple[set[str], int, str]:
    """(spent ids, n, last mac) of a log's text, after verifying the chain. Raises spent_log_damaged."""
    ids: set[str] = set()
    n, last = 0, GENESIS
    for line in text.split("\n"):
        if not line:
            continue
        try:
            rec = json.loads(line)
            ok = (isinstance(rec, dict) and isinstance(rec.get("n"), int) and isinstance(rec.get("id"), str)
                  and isinstance(rec.get("ts"), str) and isinstance(rec.get("home"), str)
                  and isinstance(rec.get("prev"), str) and isinstance(rec.get("mac"), str))
        except ValueError:
            ok = False
        if not ok:
            continue            # a torn or foreign line: it cannot un-spend anything, and adds nothing either
        if rec["n"] != n + 1 or rec["prev"] != last:
            raise _damaged(f"line {rec['n']} does not follow line {n}: an entry was removed, edited or reordered")
        if not hmac.compare_digest(rec["mac"], _mac(key, rec["n"], rec["id"], rec["ts"], rec["home"], rec["prev"])):
            raise _damaged(f"line {rec['n']} does not carry a valid signature")
        n, last = rec["n"], rec["mac"]
        ids.add(rec["id"])
    return ids, n, last


def _check_head(key: bytes, n: int, last: str) -> None:
    hp = head_path()
    try:
        head = json.loads(hp.read_text(encoding="utf-8"))
        hn, hlast, hmac_ = int(head["n"]), str(head["last"]), str(head["mac"])
    except FileNotFoundError:
        if n:
            raise _damaged("the head pointer is missing although the log has entries")
        return
    except (OSError, ValueError, KeyError, TypeError):
        raise _damaged("the head pointer is unreadable")
    if not hmac.compare_digest(hmac_, _head_mac(key, hn, hlast)):
        raise _damaged("the head pointer does not carry a valid signature")
    if (hn, hlast) != (n, last):
        raise _damaged(f"the head says {hn} approval(s) were spent but the log shows {n} (entries were removed)")


def _write_head(key: bytes, n: int, last: str) -> None:
    hp = head_path()
    fd, tmp = tempfile.mkstemp(dir=hp.parent, prefix=".spent-head-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(json.dumps({"n": n, "last": last, "mac": _head_mac(key, n, last)}, sort_keys=True))
    os.replace(tmp, hp)


def _open_locked():
    d = paths.approver_home()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(log_path(), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_all(fd: int) -> str:
    chunks, off = [], 0
    while True:
        chunk = os.pread(fd, 1 << 20, off)
        if not chunk:
            break
        chunks.append(chunk)
        off += len(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def is_spent(aid: str) -> bool:
    """Has this approval been used? Verifies the whole record first (fail-closed on damage). A missing record is an
    empty one. Needs the approver key (a grant exists, so the key does)."""
    if not log_path().exists() and not head_path().exists():
        return False
    key = approver.require_key()
    fd = _open_locked()
    try:
        ids, n, last = _scan(key, _read_all(fd))
        _check_head(key, n, last)
        return aid in ids
    finally:
        os.close(fd)


def claim(aid: str) -> None:
    """Spend `aid`: under the lock, verify the record, refuse if `aid` is already in it (`approval_used`), else append
    a signed line and move the head. Raises (and the command must not run) if it cannot be written."""
    key = approver.require_key()
    fd = _open_locked()
    try:
        ids, n, last = _scan(key, _read_all(fd))
        _check_head(key, n, last)
        if aid in ids:
            raise TogiError("approval_used", f"approval {aid} was already used; approvals are single-use",
                            next="re-run this command without --approval to request a new approval",
                            status="refused")
        home, ts = os.path.realpath(paths.togi_home()), now_ts()
        mac = _mac(key, n + 1, aid, ts, home, last)
        line = json.dumps({"n": n + 1, "id": aid, "ts": ts, "home": home, "prev": last, "mac": mac}, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=True) + "\n"
        data = line.encode("ascii")
        # a torn last line from a crashed writer has no newline: start on a fresh line so this record stays whole
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            data = b"\n" + data
        if os.write(fd, data) != len(data):
            raise TogiError("spent_log_unwritable", "could not write the spent-approval record; the approval was not "
                            "used and the command did not run", next="free disk space and retry", status="refused")
        os.fsync(fd)
        _write_head(key, n + 1, mac)
    except OSError as e:
        raise TogiError("spent_log_unwritable", f"could not write the spent-approval record ({e}); the approval was not "
                        "used and the command did not run", next="check the approver home is writable", status="refused")
    finally:
        os.close(fd)


def retire_for_new_key() -> None:
    """The approver key was replaced after the person proved the passphrase: grants signed with the old key are dead and
    the old record can no longer be verified, so set it aside and start a fresh one."""
    for p in (log_path(), head_path()):
        try:
            if p.exists():
                os.replace(p, p.with_name(p.name + f".{now_ts().replace(':', '')}.old"))
        except OSError:
            pass
