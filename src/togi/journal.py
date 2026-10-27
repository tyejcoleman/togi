"""Journal (SPEC section 4): append-only JSONL of every command run, by any surface.

`args_shape` records types only. `args_hash` is recorded for read commands only (a hash of a small-domain
money value is brute-forceable, so write/money/admin calls carry null). No values ever enter the journal.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import hashlib
import json
import os
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from . import paths

RETRY_WINDOW_S = 120


def now_ts() -> str:
    t = _dt.datetime.now(_dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def parse_ts(ts: str) -> float:
    return _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_dt.timezone.utc).timestamp()


def session_id() -> str:
    """Explicit $TOGI_SESSION, else the parent shell's pid (stable within one terminal)."""
    return os.environ.get("TOGI_SESSION") or f"s{os.getppid()}"


def args_hash(args: dict) -> str:
    blob = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- shapes (no values, ever)

_NUM = re.compile(r"[-+]?\d[\d.,_]*\Z")
_FLAG_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,39}\Z")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,23}\Z")


def value_shape(v: str) -> str:
    return "<num>" if _NUM.match(v) else "<str>"


def flag_shape(tok: str) -> str:
    """`--amount=5.00` -> `--amount=<num>`; `--name` -> `--name`. A flag-looking token whose 'name' is not a
    plausible flag name (a value in disguise) is recorded as `--<flag>`."""
    name, eq, val = tok[2:].partition("=")
    base = "--" + (name if _FLAG_NAME.match(name) else "<flag>")
    return base + (("=" + value_shape(val)) if eq else "")


def argv_shape(tokens: list[str]) -> list[str]:
    """argv with every value replaced by its type; flag names are kept."""
    out = []
    for t in tokens:
        if t.startswith("--") and len(t) > 2:
            out.append(flag_shape(t))
        elif t.startswith("-") and len(t) > 1 and not _NUM.match(t):
            out.append("-<short>")
        else:
            out.append(value_shape(t))
    return out


def sanitize_attempt(attempted: str | None, vocab: set[str] | frozenset[str] = frozenset()) -> str | None:
    """What may be written about an unknown command/flag: a flag keeps its name, a word is kept only if it is
    (or is a near-miss of) a real command word of the bundle -- that typo is exactly the friction signal hone
    needs and cannot be a value -- and every other token becomes `<num>` / `<str>`."""
    if attempted is None:
        return None
    if attempted.startswith("--"):
        return flag_shape(attempted).split("=", 1)[0]
    words = []
    for w in attempted.split():
        if w.startswith("-"):
            words.append(flag_shape(w) if w.startswith("--") else "-<short>")
        elif _WORD.match(w) and (w in vocab or difflib.get_close_matches(w, list(vocab), n=1, cutoff=0.75)):
            words.append(w)
        else:
            words.append(value_shape(w))
    return " ".join(words)


def append(bundle: str | None, entry: dict) -> None:
    """One O_APPEND write per line so concurrent writers never interleave. Never raises."""
    try:
        path = paths.journal_path(bundle)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(entry, separators=(",", ":"), ensure_ascii=False, default=str) + "\n").encode("utf-8")
        fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def _tail_lines(path: Path, max_bytes: int = 65536) -> list[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > max_bytes and lines else lines


def find_retry_of(bundle: str | None, session: str, command: str | None, ts: str) -> str | None:
    """ts of the latest failed call of `command` by `session` within 120 s before `ts`."""
    if not command:
        return None
    now = parse_ts(ts)
    for line in reversed(_tail_lines(paths.journal_path(bundle))):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event", "call") not in ("call",) or e.get("session") != session or e.get("command") != command:
            continue
        try:
            age = now - parse_ts(e["ts"])
        except (KeyError, ValueError):
            continue
        if age > RETRY_WINDOW_S:
            break
        if e.get("ok") is False:
            return e["ts"]
        return None  # the most recent attempt succeeded: not a retry
    return None


def make_entry(*, surface: str, bundle: str | None, command: str | None, event: str = "call",
               args_shape: dict | None = None, args_hash_: str | None = None, ok: bool | None = None,
               status: str | None = None, error_code: str | None = None, duration_ms: int = 0,
               out_bytes: int = 0, out_tokens_est: int = 0, session: str | None = None,
               agent: str | None = None, **extra: Any) -> dict:
    ts = now_ts()
    sess = session or session_id()
    entry = {
        "ts": ts, "session": sess,
        "agent": agent if agent is not None else paths.agent_name(),
        "surface": surface, "bundle": bundle, "command": command,
        "args_shape": args_shape or {}, "args_hash": args_hash_,
        "ok": ok, "status": status, "error_code": error_code,
        "duration_ms": duration_ms, "out_bytes": out_bytes, "out_tokens_est": out_tokens_est,
        "retry_of": None, "event": event,
    }
    entry.update({k: v for k, v in extra.items() if v is not None})
    if event == "call":
        entry["retry_of"] = find_retry_of(bundle, sess, command, ts)
    return entry


# --------------------------------------------------------------------------- reading


def parse_since(spec: str | None) -> float | None:
    """'90s', '15m', '24h', '7d', an ISO date/time, or None."""
    if not spec:
        return None
    m = re.fullmatch(r"(\d+)\s*([smhdw])", spec.strip())
    if m:
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return time.time() - int(m.group(1)) * mult
    s = spec.strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt).replace(tzinfo=_dt.timezone.utc).timestamp()
        except ValueError:
            continue
    raise ValueError(f"cannot read --since {spec!r}; use 24h, 7d or 2026-10-26")


def read(bundle: str | None = None, since: float | None = None) -> list[dict]:
    """Entries for one bundle ('_host' for host-level events) or, with bundle=None, all of them."""
    files: list[Path] = []
    if bundle:
        files = [paths.journal_path(bundle)]
    else:
        root = paths.state_root()
        if root.is_dir():
            files = sorted(root.glob("*/journal.jsonl"))
    out: list[dict] = []
    for f in files:
        try:
            with open(f, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if since is not None:
                        try:
                            if parse_ts(e["ts"]) < since:
                                continue
                        except (KeyError, ValueError):
                            continue
                    out.append(e)
        except OSError:
            continue
    out.sort(key=lambda e: e.get("ts", ""))
    return out


# --------------------------------------------------------------------------- friction


def friction(entries: Iterable[dict], top: int = 5) -> dict:
    """Where the skill or the commands confuse agents (SPEC section 4)."""
    calls = Counter()
    errors = Counter()
    truncated = Counter()
    helps = Counter()
    retries = Counter()
    codes = Counter()
    unknown_cmds = Counter()
    unknown_flags: dict[tuple, int] = defaultdict(int)
    total = 0
    for e in entries:
        ev = e.get("event", "call")
        cmd = e.get("command")
        if ev == "help":
            helps[cmd or "(bundle)"] += 1
            continue
        if ev == "unknown_command":
            unknown_cmds[e.get("attempted") or "?"] += 1
            continue
        if ev == "unknown_flag":
            unknown_flags[(cmd or "?", e.get("attempted") or "?")] += 1
            continue
        if ev != "call":
            continue
        total += 1
        calls[cmd] += 1
        if e.get("ok") is False:
            errors[cmd] += 1
            if e.get("error_code"):
                codes[e["error_code"]] += 1
        if e.get("retry_of"):
            retries[cmd] += 1
        if e.get("truncated"):
            truncated[cmd] += 1
    commands = []
    for cmd, n in calls.most_common():
        commands.append({"command": cmd, "calls": n, "errors": errors[cmd],
                         "error_rate": round(errors[cmd] / n, 3)})
    return {
        "calls": total,
        "errors": sum(errors.values()),
        "error_rate": round(sum(errors.values()) / total, 3) if total else 0.0,
        "commands": commands,
        "top_error_codes": [{"code": c, "count": n} for c, n in codes.most_common(top)],
        "repeated_retries": [{"command": c, "count": n} for c, n in retries.most_common(top)],
        "unknown_commands": [{"attempted": a, "count": n} for a, n in unknown_cmds.most_common(top)],
        "unknown_flags": [{"command": c, "flag": f, "count": n}
                          for (c, f), n in sorted(unknown_flags.items(), key=lambda kv: -kv[1])[:top]],
        "truncated_outputs": [{"command": c, "count": n} for c, n in truncated.most_common(top)],
        "help_lookups": [{"command": c, "count": n} for c, n in helps.most_common(top)],
    }
