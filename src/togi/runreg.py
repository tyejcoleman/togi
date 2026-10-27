"""Which `togi run` hosts are alive, so a script's descendants cannot approve anything.

`togi run` writes `$TOGI_HOME/run/<pid>` (with the process start time, so a reused pid is not mistaken for it)
and removes it on exit. `approve` and the other human-only actions walk their own ancestor processes and
refuse if any of them is listed. A command-line regex is kept as a second layer for a host started before
this registry existed.

Honest limit: a deliberate double-fork + setsid breaks the ancestor chain; the passphrase and the sandbox
(which hides `$TOGI_HOME` and other processes from the script) are the layers that cover that.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import paths

# `togi run`, `python -m togi run`, `togi --json run -`, `/path/to/togi --text run x.py`
_RUN_CMD = re.compile(r"(?:(?:^|[\s/])togi|-m\s+togi)(?:\s+--?[A-Za-z][\w-]*(?:=\S+)?)*\s+run(?:\s|$)")
_PS = re.compile(r"^\s*(\d+)\s+(\w{3}\s+\w{3}\s+\d+\s+[\d:]{8}\s+\d{4})\s+(.*)$")
_ENV = {"LC_ALL": "C", "PATH": "/bin:/usr/bin:/usr/sbin:/sbin"}
_registered: Path | None = None


def _norm_start(s: str) -> str:
    return " ".join(s.split())


def proc_info(pid: int) -> tuple[int, str, str] | None:
    """(ppid, start time, command line) of a process, or None if it cannot be seen."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=,lstart=,command=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=3, env=_ENV).stdout.strip("\n")
    except (OSError, subprocess.SubprocessError):
        return None
    m = _PS.match(out.splitlines()[0]) if out.strip() else None
    if not m:
        return None
    return int(m.group(1)), _norm_start(m.group(2)), m.group(3)


def _record(pid: int) -> dict | None:
    try:
        return json.loads((paths.run_registry_dir() / str(pid)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def register() -> None:
    """Called by the `togi run` host once it is the final process (after any re-exec)."""
    global _registered
    d = paths.run_registry_dir()
    try:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        _sweep(d)
        info = proc_info(os.getpid())
        rec = {"pid": os.getpid(), "start": info[1] if info else None, "ts": time.time()}
        f = d / str(os.getpid())
        f.write_text(json.dumps(rec), encoding="utf-8")
        _registered = f
        atexit.register(unregister)
    except OSError:
        pass   # best effort: the regex and the other layers still apply


def unregister() -> None:
    global _registered
    if _registered is not None:
        try:
            _registered.unlink()
        except OSError:
            pass
        _registered = None


def _sweep(d: Path) -> None:
    for f in d.iterdir():
        try:
            pid = int(f.name)
            os.kill(pid, 0)
        except ValueError:
            continue
        except ProcessLookupError:
            try:
                f.unlink()
            except OSError:
                pass
        except PermissionError:
            continue


def started_by_togi_run(max_depth: int = 40) -> bool:
    """True if any ancestor of this process is a live `togi run` host."""
    pid = os.getppid()
    for _ in range(max_depth):
        if pid <= 1:
            return False
        info = proc_info(pid)
        if info is None:
            return False
        ppid, start, cmd = info
        rec = _record(pid)
        if rec is not None and rec.get("start") and _norm_start(rec["start"]) == start:
            return True
        if _RUN_CMD.search(cmd.strip()):
            return True
        pid = ppid
    return False
