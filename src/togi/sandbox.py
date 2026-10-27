"""OS sandbox for the `togi run` child (macOS `sandbox-exec`), with a clean fallback.

The child never needs bundle code: every bundle call is proxied to the host. So the profile hides everything
that is not Python itself or the child's own temp directory:

  - network: denied (the child talks to the host over an inherited socketpair, which is not a path);
  - file reads, level "strict" (tried first): everything is denied except the Python install, the child's temp
    directory (where Togi puts its own `_runchild` stub) and the operating system's own directories (/usr, /bin,
    /sbin, /System, /dev, the developer tools) that Python and the shell need. File *metadata* (stat: does a path
    exist, how big) stays visible; contents and directory listings of your home, other volumes, other programs'
    temp files, every registered bundle repo (`.env` files included) and `$TOGI_HOME` do not;
  - file reads, level "standard" (used if "strict" cannot start Python on this machine): the same, but by denying the
    user-data zones (/Users, /Volumes, the temp areas, your home, every repo, `$TOGI_HOME`) instead of allowing only
    the system ones;
  - file writes: only the child's temp directory (and /dev/null, /dev/tty);
  - other processes: `process-info` and signals to processes outside the sandbox are denied.

Without that, a script could `import crowd.engine` and call `settle()` directly, never meeting an approval.

Honest limits (also in the README): this is a same-user process under a profile, not a VM.
  - `ps` fails under ANY `sandbox-exec` profile (it is setuid), so `ps -E` cannot read the host's environment;
    but a direct `sysctl kern.procargs2` read of another same-user process's arguments is NOT blockable with
    these filters. That is why the `togi run` host re-execs itself with a scrubbed environment first and hands
    `[policy].run_env` values over out of band: there is nothing secret left in its visible environment.
  - `sandbox-exec` is deprecated by Apple but present on every current macOS; if it is missing, or already
    inside another sandbox (nested sandboxes are refused), or cannot run Python with this profile, the run
    proceeds unsandboxed and says `sandboxed: false` with the reason.
"""
from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from dataclasses import dataclass, field
from pathlib import Path

from .pathnorm import is_unsafe_char

SANDBOX_EXEC = "/usr/bin/sandbox-exec"
# Read-denied zones (other people's and other programs' files). Python's own install and the child's temp dir
# are allowed back in below, and the repos / $TOGI_HOME are denied again last.
DENY_READ_ZONES = ("/Users", "/Volumes", "/private/tmp", "/private/var/tmp", "/private/var/folders")
# Level "strict": the ONLY places outside the Python install and the child's temp dir whose contents may be read.
# Operating-system directories only: no home, no /private/etc (hosts, ssh config...), no /opt/homebrew/etc.
STRICT_READ = ("/usr", "/bin", "/sbin", "/System", "/dev", "/Library/Frameworks", "/Library/Developer/CommandLineTools",
               "/Applications/Xcode.app", "/private/etc/localtime", "/private/var/select", "/private/var/db/dyld",
               "/opt/homebrew/lib", "/opt/homebrew/opt", "/opt/homebrew/Cellar", "/opt/homebrew/Frameworks")


@dataclass
class Sandbox:
    sandboxed: bool
    reason: str | None = None            # why not, when sandboxed is False
    prefix: list[str] = field(default_factory=list)   # argv prefix to run the child under
    level: str | None = None             # "strict" | "standard" when sandboxed
    note: str | None = None              # e.g. why "strict" was not possible


def available() -> tuple[bool, str | None]:
    if sys.platform != "darwin":
        return False, f"no OS sandbox on {sys.platform} (macOS sandbox-exec only)"
    if not os.access(SANDBOX_EXEC, os.X_OK):
        return False, f"{SANDBOX_EXEC} is not available"
    return True, None


def _q(path: str) -> str:
    """A path as an SBPL string literal. Control characters are refused (the profile could not be trusted)."""
    if any(is_unsafe_char(c) or c == "\x7f" for c in path):
        raise ValueError(f"path {path!r} has control characters")
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def python_roots() -> list[str]:
    roots = {sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix,
             os.path.dirname(os.path.realpath(sys.executable)), os.path.realpath(sys.executable)}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        p = sysconfig.get_path(key)
        if p:
            roots.add(p)
    return sorted({os.path.realpath(r) for r in roots if r})


def build_profile(*, tmp: str, repos: list[str], togi_home: str, home: str, strict: bool = False,
                  also_hidden: tuple[str, ...] = ()) -> str:
    """The SBPL profile text. All paths are resolved (the sandbox matches real paths, not symlinks). `also_hidden`
    are further paths denied outright, e.g. a $TOGI_APPROVER_HOME that is not under the user's home."""
    real = os.path.realpath
    tmp, togi_home, home = real(tmp), real(togi_home), real(home)
    hidden = sorted({real(r) for r in repos} | {togi_home} | {real(h) for h in also_hidden})
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny process-info* (target others))",
        "(deny signal (target others))",
        "(deny file-write*)",
        f'(allow file-write* (subpath {_q(tmp)}) (literal "/dev/null") (literal "/dev/dtracehelper") '
        '(literal "/dev/tty"))',
    ]
    if strict:
        lines += ["(deny file-read*)", "(allow file-read-metadata)", '(allow file-read* (literal "/"))',
                  "(allow file-read* " + " ".join(f"(subpath {_q(z)})" for z in STRICT_READ) + ")"]
    else:
        lines.append("(deny file-read* " + " ".join(f"(subpath {_q(z)})" for z in (*DENY_READ_ZONES, home)) + ")")
    lines += [f"(allow file-read* (subpath {_q(r)}))" for r in python_roots()]
    lines.append(f"(allow file-read* file-write* (subpath {_q(tmp)}))")
    lines.append("(deny file* " + " ".join(f"(subpath {_q(h)})" for h in hidden) + ")")
    return "\n".join(lines) + "\n"


def _trial(profile: str, tmp: str, home: str) -> tuple[bool, str]:
    """Run Python once under `profile`; (works, why not)."""
    try:
        p = subprocess.run([SANDBOX_EXEC, "-p", profile, sys.executable, "-I", "-B", "-c",
                            "import socket, json, itertools, types"],
                           capture_output=True, text=True, timeout=20, cwd=tmp, stdin=subprocess.DEVNULL,
                           env={"PATH": "/usr/bin:/bin", "TMPDIR": tmp, "HOME": home})
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"sandbox-exec could not be run ({type(e).__name__})"
    if p.returncode == 0:
        return True, ""
    detail = (p.stderr or "").strip().splitlines()
    return False, (detail[-1][:140] if detail else f"exit {p.returncode}")


def prepare(*, tmp: str, repos: list[str], togi_home: str, home: str,
            also_hidden: tuple[str, ...] = ()) -> Sandbox:
    """Build the profile and prove (with a trial run of Python under it) that it works here: the strict profile
    first, then the standard one, else no sandbox (the caller reports `sandboxed: false` and why)."""
    ok, why = available()
    if not ok:
        return Sandbox(False, why)
    note = None
    for strict in (True, False):
        try:
            profile = build_profile(tmp=tmp, repos=repos, togi_home=togi_home, home=home, strict=strict,
                                    also_hidden=also_hidden)
        except ValueError as e:
            return Sandbox(False, f"cannot build a sandbox profile: {e}")
        works, detail = _trial(profile, tmp, home)
        if works:
            return Sandbox(True, None, [SANDBOX_EXEC, "-p", profile], "strict" if strict else "standard", note)
        if strict:
            note = f"strict profile unusable here ({detail}); using the standard one"
        else:
            return Sandbox(False, f"sandbox-exec cannot start Python here ({detail}); it may already be inside "
                                  "another sandbox, or Python lives inside a registered bundle repo")
    return Sandbox(False, "no sandbox profile worked")  # pragma: no cover
