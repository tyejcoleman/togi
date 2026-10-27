"""Filesystem locations. Everything honours $TOGI_HOME (default ~/.togi), EXCEPT the approver.

The approver (passphrase record, wrong-attempt state, signing key) must not be somewhere an agent can choose: an
agent that can set an environment variable could otherwise point `TOGI_HOME` at a directory it controls, create
its own approver there and approve its own money movement. So it lives at a fixed place, `<pw_dir>/.togi`, where
`pw_dir` comes from the password database for the current uid -- not from `$HOME`, not from `$TOGI_HOME`.

The one override is `TOGI_APPROVER_HOME`, for tests and the stone. An approver under that override signs every
grant with `scope: "test"` (the fixed one signs `scope: "default"`), and Togi hands that scope to the command as
`ctx.approval_scope`, so a bundle that moves real value can refuse test-scope approvals outside its own test mode.
Togi itself never lets a test-scope approver change a repo: `lock`, `revert`, the reshape evaluation approval and a
human `apply` refuse while the override is set (approvals.require_default_scope).
"""
from __future__ import annotations

import os
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


def fixed_approver_home() -> Path:
    """Where the approver always lives: `<pw_dir>/.togi`."""
    return pw_dir() / ".togi"


def approver_override() -> Path | None:
    """`TOGI_APPROVER_HOME`, when set to something non-empty. For tests and the stone only: see approver_scope()."""
    env = os.environ.get("TOGI_APPROVER_HOME")
    return Path(os.path.abspath(os.path.expanduser(env))) if env else None


def approver_home() -> Path:
    """The directory holding approver, approver.state and approver.key: the fixed one, unless overridden."""
    return approver_override() or fixed_approver_home()


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
    """Directory that contains the `togi` package (put on PYTHONPATH for child processes)."""
    return Path(__file__).resolve().parent.parent
