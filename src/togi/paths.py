"""Filesystem locations. Everything honours $TOGI_HOME (default ~/.togi), EXCEPT the approver.

The approver (passphrase record, wrong-attempt state, signing key) must not be somewhere an agent can choose: an
agent that can set an environment variable could otherwise point `TOGI_HOME` at a directory it controls, create
its own approver there and approve its own money movement. So it lives at a fixed place, `<pw_dir>/.togi/approver.d`, where
`pw_dir` comes from the password database for the current uid -- not from `$HOME`, not from `$TOGI_HOME`. (r5: its own
0700 directory. In r4 the files sat loose in `<pw_dir>/.togi`, which is also the default `$TOGI_HOME`, so the journals,
approvals, registry and the approver's key and passphrase record all shared one directory. They are moved into
`approver.d` once, under the human gate: approver.migrate_legacy.)

The one override is `TOGI_APPROVER_HOME`, for tests and the stone. An approver under that override signs every
grant with `scope: "test"` (the fixed one signs `scope: "default"`), and Togi hands that scope to the command as
`ctx.approval_scope`, so a bundle that moves real value can refuse test-scope approvals outside its own test mode.
Togi itself never lets a test-scope approver change a repo: `lock`, `revert`, the reshape evaluation approval and a
human `apply` refuse while the override is set (approvals.require_default_scope).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .errors import TogiError


def togi_home() -> Path:
    env = os.environ.get("TOGI_HOME")
    return Path(env).expanduser() if env else Path.home() / ".togi"


def state_root() -> Path:
    return togi_home() / "state"


def bundle_state(bundle: str) -> Path:
    return state_root() / bundle


def journal_path(bundle: str | None) -> Path:
    """Journal for a bundle; host-level events (approve/deny attempts) go to `_host`."""
    return bundle_state(bundle or "_host") / "journal.jsonl"


def approvals_dir() -> Path:
    return togi_home() / "approvals"


def pw_dir() -> Path:
    """The account's home directory according to the password database. Deliberately NOT `$HOME` and NOT
    `$TOGI_HOME`: an agent that controls its environment must not be able to move this."""
    import pwd

    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except KeyError:
        raise TogiError("approver_home_unknown", f"uid {os.getuid()} has no entry in the password database, so the "
                                                 "fixed approver location cannot be found",
                        next="a person runs this under a normal user account", status="refused")


APPROVER_DIRNAME = "approver.d"


def account_togi_dir() -> Path:
    """`<pw_dir>/.togi`: the default `$TOGI_HOME`, and the parent of the approver's own directory. In r4 the approver's files
    sat directly in it (`legacy_approver_home`)."""
    return pw_dir() / ".togi"


def fixed_approver_home() -> Path:
    """Where the approver always lives: `<pw_dir>/.togi/approver.d`, a directory of its own (0700)."""
    return account_togi_dir() / APPROVER_DIRNAME


def legacy_approver_home() -> Path:
    """Where r4 kept the approver's files: loose in `<pw_dir>/.togi`. Read only to migrate them (approver.migrate_legacy)."""
    return account_togi_dir()


def approver_override() -> Path | None:
    """`TOGI_APPROVER_HOME`, when set to something non-empty. For tests and the stone only: see approver_scope()."""
    env = os.environ.get("TOGI_APPROVER_HOME")
    return Path(os.path.abspath(os.path.expanduser(env))) if env else None


def approver_home() -> Path:
    """The directory holding approver, approver.state and approver.key: the fixed one, unless overridden."""
    return approver_override() or fixed_approver_home()


def layout_problem() -> str | None:
    """Why `$TOGI_HOME` and the approver's directory may not be used together, or None. `$TOGI_HOME` is where an agent's
    session writes (journals, pending approvals, the registry, proposals); the approver's directory is not, so the one
    must not be, contain or sit inside the other. The single exception is the default layout itself: `$TOGI_HOME` =
    `<pw_dir>/.togi` holds the approver's own `approver.d` as a sub-directory."""
    try:
        home = os.path.realpath(togi_home())
        ad = os.path.realpath(approver_home())
        default = os.path.realpath(account_togi_dir()) if approver_override() is None else None
    except TogiError:
        return None
    if home == ad:
        return f"$TOGI_HOME ({home}) is the approver's directory"
    if ad.startswith(home + os.sep) and home != default:
        return f"$TOGI_HOME ({home}) contains the approver's directory ({ad})"
    if home.startswith(ad + os.sep):
        return f"$TOGI_HOME ({home}) is inside the approver's directory ({ad})"
    return None


def require_safe_layout() -> None:
    """Refuse (`togi_home_unsafe`) to run with a `$TOGI_HOME` that equals, contains or sits inside the approver's directory
    (see `layout_problem`). Called for every command, so no journal, approval or registry file lands next to the key."""
    why = layout_problem()
    if why:
        raise TogiError("togi_home_unsafe", f"{why}; the approver keeps its passphrase record, signing key and "
                                            "spent-approval log in a directory of their own, apart from $TOGI_HOME",
                        next="unset TOGI_HOME (the default is ~/.togi) or point it at a directory of its own",
                        status="refused")


def override_is_own_approver() -> bool:
    """Is the `TOGI_APPROVER_HOME` override, in fact, the person's own approver? It is when it names the fixed directory (or
    the r4 place the files moved from) by another path (a symlink, `..`, a trailing slash, another mount of it), or when its
    approver files are the fixed approver's own files (linked in). Such an approver is test scope by name only: whatever the
    person's own approver needs (for the desk, a terminal) applies to it. When the fixed place cannot be found, the answer
    is yes (fail closed)."""
    o = approver_override()
    if o is None:
        return False
    try:
        own = [fixed_approver_home(), legacy_approver_home()]
    except TogiError:
        return True

    def same(a: Path, b: Path) -> bool:
        try:
            return os.path.samefile(a, b)
        except OSError:
            return os.path.realpath(a) == os.path.realpath(b)

    for d in own:
        if same(o, d):
            return True
        for name in ("approver", "approver.key", "approver.state"):
            if (o / name).exists() and (d / name).exists() and same(o / name, d / name):
                return True
    return False


def approver_scope() -> str:
    """`"test"` when the approver is the `TOGI_APPROVER_HOME` override, else `"default"`. Every grant carries the
    scope of the approver that signed it, and a grant is only honoured by a process whose scope matches."""
    return "test" if approver_override() is not None else "default"


def approver_path() -> Path:
    """The approver passphrase record (scrypt hash + salt), 0600."""
    return approver_home() / "approver"


def approver_state_path() -> Path:
    """Wrong-attempt counter and lock-out time for the approver passphrase."""
    return approver_home() / "approver.state"


def approver_key_path() -> Path:
    """The approver key (32 random bytes, 0600) that signs grants (`grant_mac`)."""
    return approver_home() / "approver.key"


def audit_key_path() -> Path:
    """HMAC key that signs audit entries, 0600."""
    return togi_home() / "audit.key"


def run_registry_dir() -> Path:
    """One file per live `togi run` host, named by pid: `approve` refuses if any ancestor is listed."""
    return togi_home() / "run"


def is_agent() -> bool:
    """An agent session is any environment where TOGI_AGENT is SET, even to the empty string."""
    return "TOGI_AGENT" in os.environ


def agent_name() -> str | None:
    """The calling agent's name, or None if TOGI_AGENT is not set (an empty value is reported as 'unnamed')."""
    if "TOGI_AGENT" not in os.environ:
        return None
    return os.environ["TOGI_AGENT"] or "unnamed"


def in_run() -> bool:
    return "TOGI_RUN" in os.environ


def proposals_dir() -> Path:
    return togi_home() / "proposals"


def stone_dir(bundle: str) -> Path:
    return togi_home() / "stone" / bundle


def bench_dir(bundle: str) -> Path:
    return togi_home() / "bench" / bundle


def src_dir() -> Path:
    """Directory that contains the `togi` package."""
    return Path(__file__).resolve().parent.parent


def launcher_path() -> Path:
    """The standalone launcher script (`_launch.py`): the one way Togi starts itself."""
    return Path(__file__).resolve().parent / "_launch.py"


# Where bundle code may look for cached bytecode: a path that cannot exist (a "directory" under the /dev/null device), so
# no compiled file is ever read for a bundle and none can be planted for one, by anyone, at any moment. Every bundle
# module is compiled from its locked source on each start. (A Togi-owned cache dir was not enough: whoever can write
# that dir could plant a .pyc that runs instead of the locked source while the lock still verifies.)
NO_BYTECODE_PREFIX = "/dev/null/togi-no-bytecode"


def pycache_args(home: Path | str | None = None) -> list[str]:
    """`-X pycache_prefix=<impossible path>` for every Python child Togi starts that can load bundle code (stone tests,
    the describe child, the `togi run` child). Works under `-I`, unlike PYTHONPYCACHEPREFIX. `home` is ignored (kept for
    call-site compatibility)."""
    return ["-X", f"pycache_prefix={NO_BYTECODE_PREFIX}"]


def isolated_argv(args: list[str] | tuple[str, ...] = ()) -> list[str]:
    """argv that starts Togi in an ISOLATED interpreter: `python -I _launch.py ARGS`.

    `-I` means no current directory and no script directory on `sys.path` and every PYTHON* variable ignored, so
    nothing planted in the directory a person happens to be standing in (a `hmac.py`, a `json.py`) and nothing in
    `PYTHONPATH` can run inside Togi. The launcher puts Togi's own `src` on `sys.path` explicitly. Every place Togi
    starts itself (the `togi` launcher, the `togi run` host re-exec, the stone's `togi` shim and MCP server) uses this."""
    return [sys.executable, "-I", str(launcher_path()), *args]
