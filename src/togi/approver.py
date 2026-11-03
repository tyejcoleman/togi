"""The approver: a passphrase and a signing key that an agent never sees, kept where an agent cannot move them.

A TTY check stops an agent using a normal shell, but an agent that can run `script(1)` or a pty library can
give `togi approve` a terminal and type the confirmation word itself. So every human-only action also needs a
passphrase that a person set up once with `togi approver setup` and types at the terminal each time:

- stored as scrypt(passphrase, salt) in `<approver home>/approver` (0600); the passphrase itself is never stored;
- read from `/dev/tty` with echo off (so it is not on stdout/stderr, and not read from a pipe);
- wrong attempts are counted in `<approver home>/approver.state`: after 5, the approver is locked for 10 minutes;
  every attempt (right or wrong, locked or not) is written to the `_host` journal;
- with no approver set up, human-only actions refuse and say how to set one up;
- `togi approver setup` never overwrites an existing approver unless the current passphrase is entered first;
- grants are signed: `<approver home>/approver.key` (32 random bytes, 0600, created by setup when missing) keys an
  HMAC over every grant (see approvals.py), so a grant record that was not made by this approver is worthless.

<approver home> is `<pw_dir>/.togi/approver.d` -- a 0700 directory of its own (r5), fixed, independent of $HOME and
$TOGI_HOME (see paths.py), and holding the passphrase record, the key, the attempt state and the spent-approval log. r4 kept
those files loose in `<pw_dir>/.togi`, beside the journals and approvals of the default $TOGI_HOME; `migrate_legacy` moves
them in once, under the human gate. The only override is $TOGI_APPROVER_HOME, for tests and the stone (used as given, never
re-permissioned); grants made under it carry `scope: "test"`, and the gates that change a repo (lock, revert, reshape
evaluation, human apply) refuse it outright: anyone can make such an approver.

What this does and does not stop (also in the README): it stops an agent that has a shell and a pty but not the
passphrase, and an agent that redirects $TOGI_HOME to a directory of its own. A passphrase typed into the agent's
own chat is compromised. Until an approver exists, whoever can open a terminal can run `togi approver setup`: set it
up before an agent has a shell. A same-user process can read `approver` and `approver.key`; scrypt with a strong
passphrase makes the first expensive to attack, nothing protects the second from a process with full file access
(that is outside this threat model; v0.2: a keychain or out-of-band approver).
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import select
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import paths
from .errors import TogiError
from .journal import now_ts

KEY_BYTES = 32
MIN_LENGTH = 12
FAIL_LIMIT = 5
LOCK_SECONDS = 10 * 60
DEFAULT_N = 2 ** 17     # 128 MiB, ~0.2 s: a person types a passphrase a few times a day
SCRYPT_R, SCRYPT_P, DKLEN = 8, 1, 32
PROMPT = "Approver passphrase (not shown): "
PASSPHRASE_TIMEOUT_S = 120    # nobody types for longer than this; a pty-driving script that has no passphrase just waits


# --------------------------------------------------------------------------- the record


def _kdf(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=DKLEN,
                          maxmem=256 * n * r * p + 2 ** 22)


def load() -> dict | None:
    """The approver record, or None if none is set up. A damaged record is an error, not 'no approver'."""
    p = paths.approver_path()
    try:
        rec = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _refuse_if_migration_pending()
        return None
    except (OSError, ValueError) as e:
        raise TogiError("approver_unreadable", f"{p} cannot be read ({e})",
                        next="restore it from a backup, or remove it and run: togi approver setup", status="refused")
    try:
        n, r, pp = int(rec["n"]), int(rec["r"]), int(rec["p"])
        salt, want = base64.b64decode(rec["salt"]), base64.b64decode(rec["hash"])
        ok = rec.get("kdf") == "scrypt" and 2 ** 4 <= n <= 2 ** 22 and n & (n - 1) == 0 and 1 <= r <= 32 \
            and 1 <= pp <= 8 and len(salt) >= 8 and len(want) == DKLEN
    except (KeyError, ValueError, TypeError):
        ok = False
    if not ok:
        raise TogiError("approver_unreadable", f"{p} is not a valid approver record",
                        next="restore it from a backup, or remove it and run: togi approver setup", status="refused")
    return rec


def configured() -> bool:
    return load() is not None


def _check_strength(passphrase: str) -> None:
    if len(passphrase) < MIN_LENGTH:
        raise TogiError("passphrase_too_short", f"the passphrase must be at least {MIN_LENGTH} characters",
                        next="run: togi approver setup", status="error")
    if passphrase != passphrase.strip() or len(set(passphrase)) < 4:
        raise TogiError("passphrase_weak", "the passphrase must not start or end with spaces and must use at "
                                           "least 4 different characters", next="run: togi approver setup")


def _private_dir(d: Path) -> None:
    """Create the approver home 0700. The fixed one (`<pw_dir>/.togi/approver.d`, ours alone) is also brought back to 0700 if
    it is looser; a `$TOGI_APPROVER_HOME` override is a directory somebody else chose (a test's, never `/tmp`), so an
    existing one is left exactly as it is."""
    existed = d.exists()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    if existed and paths.approver_override() is None and d == paths.fixed_approver_home():
        try:
            if d.stat().st_mode & 0o077:
                os.chmod(d, 0o700)
        except OSError:
            pass


def create(passphrase: str, *, n: int = DEFAULT_N, replace: bool = False) -> dict:
    """Write the approver record (and the signing key, when missing). An existing approver is NOT replaced unless
    `replace=True`, and only `setup()` passes that, after the person proved the current passphrase. `n` is the
    scrypt cost (tests lower it; it is stored in the file)."""
    _check_strength(passphrase)
    salt = secrets.token_bytes(16)
    rec = {"v": 1, "kdf": "scrypt", "n": n, "r": SCRYPT_R, "p": SCRYPT_P, "salt": base64.b64encode(salt).decode(),
           "hash": base64.b64encode(_kdf(passphrase, salt, n, SCRYPT_R, SCRYPT_P)).decode(), "created": now_ts()}
    target = paths.approver_path()
    _private_dir(target.parent)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".approver-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            json.dump(rec, f, sort_keys=True)
        if replace:
            os.replace(tmp, target)
        else:
            try:
                os.link(tmp, target)          # atomic and exclusive: two first-time setups cannot both win
            except FileExistsError:
                raise TogiError("approver_exists", f"an approver already exists at {target}; it is not replaced "
                                                   "without its current passphrase",
                                next="a person runs: togi approver setup   (it asks for the current passphrase first)",
                                status="refused")
            finally:
                os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    ensure_key(repair=replace)
    reset_state()
    return rec


def check(rec: dict, passphrase: str) -> bool:
    got = _kdf(passphrase, base64.b64decode(rec["salt"]), int(rec["n"]), int(rec["r"]), int(rec["p"]))
    return hmac.compare_digest(got, base64.b64decode(rec["hash"]))


# --------------------------------------------------------------------------- the r4 -> r5 layout

# r4 kept these loose in `<pw_dir>/.togi`; r5 keeps them in `<pw_dir>/.togi/approver.d`. `approver` is moved LAST: until it
# is there the migration is not finished, so a crash half way is picked up again by the next human command.
LEGACY_FILES = ("approver.key", "approver.state", "spent.jsonl", "spent.head")
_LEGACY_OLD = re.compile(r"spent\.(jsonl|head)\..+\.old")


def _legacy_names() -> list[str]:
    """r4 approver files present in the old place (empty under a `$TOGI_APPROVER_HOME` override, which never had one)."""
    if paths.approver_override() is not None:
        return []
    old = paths.legacy_approver_home()
    names = [n for n in ("approver", *LEGACY_FILES) if (old / n).is_file()]
    try:
        names += sorted(f.name for f in old.iterdir() if _LEGACY_OLD.fullmatch(f.name) and f.is_file())
    except OSError:
        pass
    return names


def layout_state() -> dict:
    """`state`: override (a test approver, no layout to migrate) | current | legacy (r4 files still loose in
    `<pw_dir>/.togi`, to be moved) | conflict (the same file exists in both places: two approvers, and nothing here may
    guess which is the person's). `legacy` lists the r4 files."""
    if paths.approver_override() is not None:
        return {"state": "override", "legacy": [], "dir": str(paths.approver_home())}
    legacy = _legacy_names()
    new = paths.fixed_approver_home()
    # `approver.state` (a failure counter) is not an identity: if a stray copy exists in both places the old one wins
    clash = [n for n in legacy if n != "approver.state" and (new / n).exists()]
    state = "conflict" if clash else ("legacy" if legacy else "current")
    return {"state": state, "legacy": legacy, "clash": clash, "dir": str(new)}


def _conflict_error(st: dict) -> TogiError:
    return TogiError("approver_conflict", f"the approver exists in two places: {', '.join(st['clash'])} in both "
                                          f"{paths.legacy_approver_home()} (r4 layout) and {paths.fixed_approver_home()}. "
                                          "Togi will not guess which one is yours, so every human action is refused",
                     next="a person looks at both, keeps the right one and removes the other", status="refused")


def _refuse_if_migration_pending() -> None:
    """Called when a file is missing from the new place: if it is still sitting in the old one, say so (instead of the
    misleading 'no approver set up')."""
    try:
        st = layout_state()
    except TogiError:
        return
    if st["state"] == "legacy" and st["legacy"]:
        raise TogiError("approver_migration_pending", f"the approver's files are still in the r4 location "
                        f"{paths.legacy_approver_home()} ({', '.join(st['legacy'])}) and have not been moved to "
                        f"{paths.fixed_approver_home()} yet",
                        next="a person runs, in their own terminal, any human command (e.g. togi approver migrate); "
                             "it moves them once", status="refused")
    if st["state"] == "conflict":
        raise _conflict_error(st)


def migrate_legacy() -> list[str]:
    """Move the r4 approver files into `approver.d`, in place and once. Called from the human gate (approvals.human_gate),
    so only after a person's terminal checks passed. Each file is renamed (same file system, nothing copied or rewritten);
    the passphrase record goes last. A name that exists in BOTH places is a conflict and nothing is moved. Returns the names
    moved ([] when there was nothing to do)."""
    st = layout_state()
    if st["state"] == "conflict":
        raise _conflict_error(st)
    if st["state"] != "legacy":
        return []
    old, new = paths.legacy_approver_home(), paths.fixed_approver_home()
    _private_dir(new)
    order = [n for n in st["legacy"] if n != "approver"] + (["approver"] if "approver" in st["legacy"] else [])
    moved = []
    for n in order:
        os.replace(old / n, new / n)
        moved.append(n)
    try:
        os.chmod(new, 0o700)
    except OSError:
        pass
    from . import journal

    journal.append(None, journal.make_entry(surface="cli", bundle=None, command="approver migrate", event="approver_migrated",
                                            ok=True, status="ok", effect="admin", files=moved))
    return moved


# --------------------------------------------------------------------------- the signing key


def load_key() -> bytes | None:
    """The approver key, or None if there is none. A key file that exists but is not 32 bytes is an error."""
    p = paths.approver_key_path()
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        _refuse_if_migration_pending()
        return None
    except OSError as e:
        raise TogiError("approver_key_unreadable", f"{p} cannot be read ({e})",
                        next="a person runs: togi approver setup", status="refused")
    if len(raw) != KEY_BYTES:
        raise TogiError("approver_key_unreadable", f"{p} is not a valid approver key",
                        next="a person runs: togi approver setup", status="refused")
    return raw


def ensure_key(repair: bool = False) -> bytes:
    """Create `approver.key` (32 random bytes, 0600) if it is missing and return it. `repair=True` (only after the
    person proved the passphrase) also replaces a damaged key; grants signed with the old one then stop working."""
    try:
        existing = load_key()
    except TogiError:
        if not repair:
            raise
        existing = None
    if existing is not None:
        return existing
    target = paths.approver_key_path()
    _private_dir(target.parent)
    key = secrets.token_bytes(KEY_BYTES)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".approver-key-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
        if repair and target.exists():
            os.replace(tmp, target)
        else:
            try:
                os.link(tmp, target)          # exclusive: a concurrent creator's key wins, and we use theirs
            except FileExistsError:
                os.unlink(tmp)
                return load_key() or key
            os.unlink(tmp)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    from . import spent

    spent.retire_for_new_key()   # grants signed with a lost key are dead; so is a spent record only that key could verify
    return key


def require_key() -> bytes:
    key = load_key()
    if key is None:
        raise TogiError("approver_key_missing", f"the approver key {paths.approver_key_path()} is missing, so "
                                                "grants cannot be signed",
                        next="a person runs: togi approver setup", status="refused")
    return key


def mac_hex(key: bytes, payload: bytes) -> str:
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- attempt counter (rate limit)


class _State:
    """`approver.state` under an exclusive flock: read-modify-write is atomic across processes."""

    def __enter__(self):
        p = paths.approver_state_path()
        _private_dir(p.parent)
        self.fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        raw = os.pread(self.fd, 4096, 0)
        try:
            d = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            d = {}
        self.failures = int(d.get("failures", 0) or 0)
        self.locked_until = float(d.get("locked_until", 0) or 0)
        return self

    def save(self) -> None:
        blob = json.dumps({"failures": self.failures, "locked_until": self.locked_until}).encode()
        os.ftruncate(self.fd, 0)
        os.pwrite(self.fd, blob, 0)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def reset_state() -> None:
    with _State() as st:
        st.failures, st.locked_until = 0, 0.0
        st.save()


def lock_remaining(now: float | None = None) -> float:
    now = time.time() if now is None else now
    with _State() as st:
        return max(0.0, st.locked_until - now)


def status() -> dict:
    layout = layout_state()
    try:
        rec = load()
    except TogiError as e:
        if e.code not in ("approver_migration_pending", "approver_conflict"):
            raise
        return {"configured": False, "created": None, "locked": False, "locked_for_seconds": 0, "wrong_attempts": 0,
                "path": str(paths.approver_path()), "scope": paths.approver_scope(), "key": False, "layout": layout["state"],
                "legacy_files": layout["legacy"], "next": e.next}
    with _State() as st:
        left = max(0.0, st.locked_until - time.time())
        failures = st.failures
    return {"configured": rec is not None, "created": (rec or {}).get("created"),
            "locked": left > 0, "locked_for_seconds": int(math.ceil(left)), "wrong_attempts": failures,
            "path": str(paths.approver_path()), "scope": paths.approver_scope(),
            "key": paths.approver_key_path().is_file(),
            "next": None if rec else "a person runs: togi approver setup"}


def _journal(action: str, ok: bool, code: str | None, surface: str = "cli") -> None:
    """Every passphrase attempt is a journal line (host-level `_host` journal). `surface` says where the passphrase was
    typed: "cli" (the terminal, read from /dev/tty) or "desk" (a page served by a program the person started; desk.py)."""
    from . import journal

    journal.append(None, journal.make_entry(
        surface=surface, bundle=None, command="approver verify", event="passphrase_attempt", ok=ok,
        status="ok" if ok else "refused", error_code=code, effect="admin", for_action=action))


# --------------------------------------------------------------------------- reading the passphrase


def read_secret(prompt: str = PROMPT) -> str:
    """One line from the controlling terminal with echo off. Never from stdin/a pipe, never echoed."""
    try:
        import termios

        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except (OSError, ImportError):
        raise TogiError("needs_tty", "the approver passphrase is read from the controlling terminal (/dev/tty) "
                                     "and there is none", next="ask the person to run it in their own terminal",
                        status="refused")
    try:
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] = new[3] & ~termios.ECHO
        # Echo off and typed-ahead input dropped (it was echoed) BEFORE the prompt appears: whatever arrives
        # after the prompt is read, and is never shown.
        termios.tcsetattr(fd, termios.TCSAFLUSH, new)
        try:
            os.write(fd, prompt.encode("utf-8"))
            buf = b""
            deadline = time.monotonic() + _timeout()
            while b"\n" not in buf and b"\r" not in buf:
                left = deadline - time.monotonic()
                if left <= 0 or not select.select([fd], [], [], left)[0]:
                    raise TimeoutError
                chunk = os.read(fd, 4096)
                if not chunk:
                    raise EOFError
                buf += chunk
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            os.write(fd, b"\n")
        line = buf.replace(b"\r", b"\n").split(b"\n", 1)[0]
        return line.decode("utf-8", "replace")
    except EOFError:
        raise TogiError("passphrase_aborted", "no passphrase was entered", next="run the command again",
                        status="refused")
    except TimeoutError:
        raise TogiError("passphrase_timeout", f"no passphrase was entered within {_timeout():g} seconds; nothing "
                                              "was changed", next="run the command again", status="refused")
    finally:
        os.close(fd)


def _timeout() -> float:
    try:
        return max(0.5, float(os.environ.get("TOGI_PASSPHRASE_TIMEOUT", PASSPHRASE_TIMEOUT_S)))
    except ValueError:
        return float(PASSPHRASE_TIMEOUT_S)


# --------------------------------------------------------------------------- the gate every human-only action uses


def require_configured() -> dict:
    rec = load()
    if rec is None:
        raise TogiError("no_approver", "no approver passphrase is set up on this machine, so human-only actions "
                                       "(approve, deny, lock, revert, reshape apply) are refused",
                        next="a person runs, once, in their own terminal: togi approver setup", status="refused")
    if load_key() is None:   # an approver without its key cannot sign grants: finish the setup first
        raise TogiError("approver_key_missing", f"the approver at {paths.approver_path()} has no signing key "
                                                f"({paths.approver_key_path().name}), so grants cannot be signed",
                        next="a person runs: togi approver setup   (it asks for the current passphrase first)",
                        status="refused")
    return rec


def _locked_error(remaining: float) -> TogiError:
    mins = max(1, int(math.ceil(remaining / 60)))
    return TogiError("approver_locked", f"too many wrong passphrases; the approver is locked for {mins} more "
                                        f"minute(s)", next=f"try again in {mins} minute(s)", status="refused")


def require(action: str, reader: Callable[[str], str] | None = None, *, need_key: bool = True, surface: str = "cli") -> None:
    """Ask for the passphrase and check it. Raises TogiError (refused) unless it is right. `need_key=False` is for
    `setup`, which is how a missing key gets created. `reader` replaces the terminal read (/dev/tty): the approval desk
    passes the passphrase a person typed into its page (`surface="desk"`); the record, the attempt counter, the lock-out
    and the journal are the same ones, so a wrong guess on either surface counts against both."""
    rec = require_configured() if need_key else load()
    if rec is None:
        rec = require_configured()
    remaining = lock_remaining()
    if remaining > 0:
        _journal(action, False, "approver_locked", surface)
        raise _locked_error(remaining)
    try:
        pw = (reader or read_secret)(PROMPT)
    except TogiError as e:
        _journal(action, False, e.code, surface)
        raise
    with _State() as st:           # verification happens under the lock: parallel guesses cannot race the counter
        now = time.time()
        if st.locked_until > now:
            _journal(action, False, "approver_locked", surface)
            raise _locked_error(st.locked_until - now)
        good = check(rec, pw)
        if good:
            st.failures = 0
            st.save()
        else:
            st.failures += 1
            left = FAIL_LIMIT - st.failures
            if st.failures >= FAIL_LIMIT:
                st.locked_until, st.failures, left = now + LOCK_SECONDS, 0, 0
            st.save()
    _journal(action, good, None if good else "wrong_passphrase", surface)
    if good:
        return
    if left <= 0:
        raise TogiError("approver_locked", f"wrong passphrase; the approver is now locked for "
                                           f"{LOCK_SECONDS // 60} minutes", next="try again later", status="refused")
    raise TogiError("wrong_passphrase", f"wrong passphrase ({left} attempt(s) left before a "
                                        f"{LOCK_SECONDS // 60}-minute lock); nothing was changed",
                    next="run the command again", status="refused")


def setup(reader: Callable[[str], str] | None = None, n: int = DEFAULT_N) -> dict:
    """`togi approver setup`: create the approver, or change its passphrase. An existing approver is never
    overwritten without the current passphrase (rate-limited and journaled like every other attempt)."""
    read = reader or read_secret
    existing = load()
    if existing is not None:
        require("approver setup", reader, need_key=False)
    first = read(f"New approver passphrase (at least {MIN_LENGTH} characters, not shown): ")
    _check_strength(first)
    second = read("Type it again: ")
    if first != second:
        raise TogiError("passphrase_mismatch", "the two passphrases differ; nothing was changed",
                        next="run: togi approver setup", status="refused")
    rec = create(first, n=n, replace=existing is not None)
    return {"configured": True, "created": rec["created"], "replaced": existing is not None,
            "path": str(paths.approver_path()), "scope": paths.approver_scope()}
