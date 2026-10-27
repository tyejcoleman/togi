"""Shared fixtures: an isolated $TOGI_HOME, a private copy of the notes example, and CLI helpers."""
from __future__ import annotations

import io
import json
import os
import pty
import re
import select
import stat
import shutil
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
EXAMPLE = ROOT / "examples" / "notes"
PASSPHRASE = "correct horse battery staple"   # the approver passphrase of every throwaway test home


# ----------------------------------------------------------------------------- the real approver is never touched
#
# The approver lives in `<pw_dir>/.togi` -- the REAL home of whoever runs the tests. No test may create, change or
# even touch an approver there. Three layers:
#   1. every test gets a throwaway approver through TOGI_APPROVER_HOME (the `home` fixture, `clean_env`);
#   2. an autouse fixture swaps `pwd.getpwuid` so that, in this process, even a test that forgets layer 1 resolves
#      the "fixed" location to a temp dir (and subprocess tests that need the fixed location use `fake_pw_dir`);
#   3. an autouse fixture compares the real home's approver files before and after EVERY test and fails the test
#      that changed them (`test_the_real_approver_guard_detects_a_write` proves the guard can fail).
#
# Two scopes of approver (see approvals.require_default_scope):
#   - TEST scope (TOGI_APPROVER_HOME set): what `home` gives every test. Money approvals (`approve`, `deny`) accept it.
#   - DEFAULT scope (the fixed `<pw_dir>/.togi`, faked into a temp dir): the ONLY approver the repo-changing gates
#     (`lock`, `revert`, `apply`) accept. `run_in_pty` / `run_togi` pick it for those three commands by themselves
#     (`scope="auto"`), run the child with the pwd shim and no TOGI_APPROVER_HOME, and create that approver on demand
#     (same passphrase as the test one). Pass `scope="test"` to run them as an agent with its own approver would.

def _real_pw_dir() -> Path:
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir)


REAL_PW_DIR = _real_pw_dir()          # captured at import, before any test patches pwd


def approver_files(root: Path) -> dict:
    """Every approver-related file directly inside `root` (approver, approver.key, approver.state, the spent-approval
    record, temp files), and (r5) everything inside an `approver.d` directory there: the fixed approver lives in
    `<pw_dir>/.togi/approver.d`, so a write in the real home shows up as a change of the directory or of a file in it."""
    out = {}
    try:
        for f in root.iterdir():
            if "approver" in f.name or f.name.startswith("spent"):     # spent.jsonl / spent.head live beside the key
                st = f.stat()
                out[f.name] = (st.st_size, st.st_mtime_ns, st.st_ino)
                if f.is_dir():
                    for g in sorted(f.rglob("*")):
                        gs = g.stat()
                        out[f"{f.name}/{g.relative_to(f).as_posix()}"] = (gs.st_size, gs.st_mtime_ns, gs.st_ino)
    except FileNotFoundError:
        pass
    return out


REAL_APPROVER_AT_START = approver_files(REAL_PW_DIR / ".togi")

REPO_GATES = ("lock", "revert", "apply")   # human actions that change a repo's locked state: default-scope approver only
_RUNNING: dict = {}                        # the running test's fake pw dir (set by the autouse fixture below)


def throwaway_approver_home(home: Path) -> Path:
    """The throwaway approver home that goes with a test's $TOGI_HOME (a sibling, so it is NOT inside it)."""
    home = Path(home)
    return home.parent / (home.name + "_approver")


def clean_env(home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("TOGI_")}
    env["TOGI_HOME"] = str(home)
    env["TOGI_APPROVER_HOME"] = str(throwaway_approver_home(home))
    env["PYTHONPATH"] = str(SRC)          # inert for togi itself (it runs `python -I`); some tests start other interpreters
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["NOTES_TEST_MODE"] = "1"          # the notes example's own test mode (its tip_send refuses test-scope approvals outside it)
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


# Run togi in a subprocess whose password database says the account's home is `fake_pw_dir`: the way to test the
# FIXED approver location (no TOGI_APPROVER_HOME) without ever touching the real one.
PW_SHIM = """
import pwd, sys
_fake, _src, _real = sys.argv[1], sys.argv[2], pwd.getpwuid
def _getpwuid(uid):
    r = _real(uid)
    return pwd.struct_passwd((r.pw_name, r.pw_passwd, r.pw_uid, r.pw_gid, r.pw_gecos, _fake, r.pw_shell))
pwd.getpwuid = _getpwuid
sys.argv = ["togi"] + sys.argv[3:]
sys.path.insert(0, _src)
from togi.cli import main
sys.exit(main())
"""


def togi_argv(argv, fake_pw_dir: Path | None = None) -> list[str]:
    """How a test starts togi: the launcher's own isolated form (`python -I _launch.py`, paths.isolated_argv), or, with
    a faked password database, `python -I -c SHIM` (isolated too: the human gates refuse any other kind)."""
    from togi import paths

    if fake_pw_dir is None:
        return paths.isolated_argv(argv)
    return [sys.executable, "-I", "-c", PW_SHIM, str(fake_pw_dir), str(SRC), *argv]


@pytest.fixture(autouse=True)
def fake_pw_dir(tmp_path, monkeypatch):
    """In-process: the password database says the account home is a temp dir (never the real one), and fail the
    test if the real home's approver files changed while it ran."""
    import pwd

    fake = tmp_path / "fake_pw_dir"
    fake.mkdir()
    real_getpwuid = pwd.getpwuid

    def getpwuid(uid):
        r = real_getpwuid(uid)
        return pwd.struct_passwd((r.pw_name, r.pw_passwd, r.pw_uid, r.pw_gid, r.pw_gecos, str(fake), r.pw_shell))

    monkeypatch.setattr(pwd, "getpwuid", getpwuid)
    _RUNNING["fake_pw_dir"] = fake
    yield fake
    _RUNNING.pop("fake_pw_dir", None)
    now = approver_files(REAL_PW_DIR / ".togi")
    assert now == REAL_APPROVER_AT_START, (
        f"a test touched the REAL approver in {REAL_PW_DIR / '.togi'}: {sorted(set(now) ^ set(REAL_APPROVER_AT_START)) or 'modified'}. "
        "Tests must use TOGI_APPROVER_HOME (the `home` fixture) or the `fake_pw_dir` fixture.")


@pytest.fixture(autouse=True)
def restore_import_hardening():
    """`registry.harden_imports` (called whenever a bundle is loaded in-process) turns bytecode writing off and points
    `sys.pycache_prefix` into the test's $TOGI_HOME for the rest of the process; give every test its own."""
    saved = (sys.dont_write_bytecode, sys.pycache_prefix, list(sys.path))
    yield
    sys.dont_write_bytecode, sys.pycache_prefix = saved[0], saved[1]
    sys.path[:] = saved[2]                  # the repo paths a test's bundle load added (each test has its own tmp repos)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "togi_home"
    h.mkdir()
    for k in list(os.environ):
        if k.startswith("TOGI_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("TOGI_HOME", str(h))
    monkeypatch.setenv("TOGI_APPROVER_HOME", str(throwaway_approver_home(h)))
    monkeypatch.setenv("TOGI_SESSION", "test-session")
    monkeypatch.setenv("NOTES_TEST_MODE", "1")      # see clean_env
    monkeypatch.chdir(tmp_path)
    from togi import approver

    approver.create(PASSPHRASE, n=2 ** 10)   # cheap scrypt: the cost is stored in the record
    return h


@pytest.fixture
def notes_repo(tmp_path):
    dest = tmp_path / "notes_repo"
    shutil.copytree(EXAMPLE, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"))
    return dest


@pytest.fixture
def notes(home, notes_repo):
    """notes bundle registered in an isolated home."""
    togi("add", str(notes_repo))
    return notes_repo


def togi(*argv, expect: int | None = 0) -> dict | str:
    """Run the CLI in-process; returns the parsed JSON envelope (or raw text if not JSON)."""
    from togi import cli, registry

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(list(argv))
    out = buf.getvalue()
    if expect is not None:
        assert code == expect, f"exit {code} != {expect}: {out}"
    try:
        res = json.loads(out)
    except ValueError:
        return out
    if isinstance(res, dict):
        res["_exit"] = code
    return res


def togi_code(*argv) -> tuple[int, dict | str]:
    from togi import cli

    buf = io.StringIO()
    with redirect_stdout(buf):
        code = cli.main(list(argv))
    out = buf.getvalue()
    try:
        return code, json.loads(out)
    except ValueError:
        return code, out


def default_approver(passphrase: str = PASSPHRASE) -> Path:
    """Make sure a DEFAULT-scope approver exists in the running test's fake pw dir (`<fake>/.togi`, never the real
    one) and return that dir. Idempotent: an approver that is already there (a test that set up its own) is kept."""
    from unittest import mock

    from togi import approver

    fake = _RUNNING["fake_pw_dir"]
    if not any((fake / ".togi" / n).exists() for n in ("approver.d/approver", "approver")):     # (r5 layout, or r4's)
        with mock.patch.dict(os.environ):
            os.environ.pop("TOGI_APPROVER_HOME", None)
            approver.create(passphrase, n=2 ** 10)
    return fake


def _resolve_scope(argv, scope: str, fake_pw: Path | None, drop_env: tuple) -> tuple[Path | None, tuple]:
    """`scope="auto"`: DEFAULT scope for lock/revert/apply (the pwd shim + no TOGI_APPROVER_HOME + a default approver),
    TEST scope for everything else. An explicit `fake_pw` is the caller's own arrangement and is left alone."""
    assert scope in ("auto", "default", "test"), scope
    if scope == "auto":
        scope = "default" if argv and argv[0] in REPO_GATES else "test"
    if scope == "default" and fake_pw is None:
        return default_approver(), tuple(drop_env) + ("TOGI_APPROVER_HOME",)
    return fake_pw, drop_env


def run_togi(home: Path, *argv, input: str | None = None, env_extra: dict | None = None, timeout: int = 60,
             cwd: Path | None = None, drop_env: tuple = (), fake_pw: Path | None = None, scope: str = "auto"):
    """Run togi (isolated, as the launcher starts it) as a subprocess with piped stdio (what an agent's shell looks like). `fake_pw` makes
    the account home a temp dir (see PW_SHIM); `drop_env` removes variables, e.g. ("TOGI_APPROVER_HOME",); `scope`
    chooses the approver (see the note at the top: "auto" = default scope for lock/revert/apply)."""
    fake_pw, drop_env = _resolve_scope(argv, scope, fake_pw, drop_env)
    env = clean_env(home, **(env_extra or {}))
    for k in drop_env:
        env.pop(k, None)
    return subprocess.run(togi_argv(argv, fake_pw), input=input, capture_output=True, text=True,
                          env=env, timeout=timeout, cwd=cwd)


def run_in_pty(home: Path, argv, typed=None, env_extra: dict | None = None, timeout: int = 60,
               drop_env: tuple = (), fake_pw: Path | None = None, scope: str = "auto",
               cwd: Path | None = None, cmd: list[str] | None = None) -> tuple[int, str]:
    """Run togi on a real pseudo-terminal that is the child's CONTROLLING terminal (so /dev/tty works): what a
    person at a terminal looks like. `typed` is answered in order, one line per prompt: each item is either a
    string (typed once the child has printed something new and gone quiet) or (expect, string) (typed once
    `expect` has appeared in the output). `scope`: see the note at the top ("auto" = the default-scope approver for
    lock/revert/apply, a test-scope one for the rest). `cwd` is the directory the child starts in; `cmd` replaces the
    command line (default: togi started the way the launcher starts it). Returns (exit code, everything the terminal
    showed)."""
    fake_pw, drop_env = _resolve_scope(argv, scope, fake_pw, drop_env)
    env = clean_env(home, **(env_extra or {}))
    for k in drop_env:
        env.pop(k, None)
    pid, fd = pty.fork()
    if pid == 0:  # child: pty.fork made the slave our stdio and controlling terminal
        try:
            if cwd is not None:
                os.chdir(cwd)
            argv_ = cmd if cmd is not None else togi_argv(argv, fake_pw)    # `cmd`: any launcher, e.g. bin/togi itself
            os.execve(argv_[0], argv_, env)
        finally:
            os._exit(127)
    out = b""
    pending = list(typed or [])
    consumed, last_new, seen_at_send = 0, time.time(), -1
    deadline = time.time() + timeout
    status = None
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            last_new = time.time()
        if pending:
            item = pending[0]
            expect, line = item if isinstance(item, tuple) else (None, item)
            text = out.decode("utf-8", "replace")
            ready = (expect in text[consumed:]) if expect else (len(out) > seen_at_send >= 0 or
                                                               (seen_at_send < 0 and len(out) > 0)) and \
                                                              time.time() - last_new > 0.25
            if ready:
                if expect:
                    consumed = text.index(expect, consumed) + len(expect)
                os.write(fd, (line + "\n").encode())
                pending.pop(0)
                seen_at_send = len(out)
                last_new = time.time()
        if not r:
            done, st = os.waitpid(pid, os.WNOHANG)
            if done:
                status = st
                break
    # drain what is left
    try:
        while True:
            r, _, _ = select.select([fd], [], [], 0.2)
            if not r:
                break
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            out += chunk
    except OSError:
        pass
    if status is None:   # timed out (e.g. the child is still waiting for an answer nobody typed): do not hang
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
            if not done:
                os.kill(pid, 9)
                _, status = os.waitpid(pid, 0)
                out += b"\n[test harness: child killed after timeout]\n"
        except (ChildProcessError, ProcessLookupError):
            status = 0
    os.close(fd)
    return os.waitstatus_to_exitcode(status), out.decode("utf-8", "replace")


def answers(word: str, passphrase: str = PASSPHRASE):
    """The two answers a person gives: the typed confirmation word, then the approver passphrase."""
    return [('Type "', word), ("passphrase", passphrase)]


def human(home: Path, argv, word: str, passphrase: str = PASSPHRASE, **kw) -> tuple[int, str]:
    """A person at a terminal runs `togi <argv>`, types `word`, then the passphrase."""
    return run_in_pty(home, argv, typed=answers(word, passphrase), **kw)


def human_relock(home: Path, argv, word: str, passphrase: str = PASSPHRASE, **kw) -> tuple[int, str]:
    """`togi lock` after something changed (or with the audit head pointer lost): the bundle name, then the extra word
    "relock" (r5), then the passphrase."""
    return run_in_pty(home, argv, typed=[('Type "', word), ('Type "', "relock"), ("passphrase", passphrase)], **kw)


def human_twice(home: Path, argv, word: str, passphrase: str = PASSPHRASE, **kw) -> tuple[int, str]:
    """Two human decisions in one run (`togi apply` of a reshape proposal): approve its evaluation, then apply it."""
    return run_in_pty(home, argv, typed=answers(word, passphrase) + answers(word, passphrase), **kw)


def lock_bundle(name: str = "notes") -> dict:
    """Record the locked hashes the way a person's `togi lock` does, without a terminal (library call). A person's
    lock is made with the default-scope approver, so the audit entry says `scope: "default"`."""
    from unittest import mock

    from togi import audit, registry

    with mock.patch.dict(os.environ):
        os.environ.pop("TOGI_APPROVER_HOME", None)
        # (the helper stands in for a person, who `togi lock`s with a sandbox, or says "unsandboxed" where there is none)
        return audit.lock_bundle(registry.manifest_for(name), "human:test", allow_unsandboxed=True)


# ----------------------------------------------------------------------------- a fake `claude` executable (no API calls)

FAKE = r'''#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
instr = sys.stdin.read()
mode = os.environ.get("FAKE_CLAUDE_MODE", "agent")
with open(os.environ["FAKE_CLAUDE_LOG"], "w") as f:
    json.dump({{"args": args, "instruction": instr, "cwd": os.getcwd(), "agent": os.environ.get("TOGI_AGENT"),
               "key": "ANTHROPIC_API_KEY" in os.environ, "home": os.environ.get("TOGI_HOME")}}, f)
if mode == "hone":
    answer = open(os.environ["FAKE_CLAUDE_ANSWER"]).read()
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                      "result": answer, "total_cost_usd": 0.002, "usage": {{"input_tokens": 5, "output_tokens": 5}}}}))
    sys.exit(0)
if mode == "hang":
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3); open(" + repr(os.environ["FAKE_CLAUDE_MARKER"]) + ", 'w').write('x')"])
    import time; time.sleep(60)
subprocess.run(["togi", "notes", "show", "--id", "n_9"], capture_output=True)
if mode == "sneaky":
    subprocess.run(["togi", "approve", "ap_whatever"], capture_output=True)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": mode == "error", "num_turns": 3,
                  "result": "Not found. Try: togi notes find --query <words>", "total_cost_usd": 0.01,
                  "usage": {{"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 1000}}}}))
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    d = tmp_path / "fakebin"
    d.mkdir()
    exe = d / "claude"
    exe.write_text(FAKE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-for-the-agent")
    return log


def logged(log: Path) -> dict:
    return json.loads(log.read_text())



# ----------------------------------------------------------------------------- helpers for hone/apply tests


def make_diff(repo: Path, changes: dict[str, str | None]) -> str:
    """Unified diff (git style) turning the repo's files into `changes` ({relpath: new text})."""
    import difflib

    out = []
    for rel, new in changes.items():
        f = repo / rel
        old = f.read_text() if f.exists() else ""
        a = old.splitlines(keepends=True)
        b = new.splitlines(keepends=True)
        a_name = f"a/{rel}" if f.exists() else "/dev/null"
        out.extend(difflib.unified_diff(a, b, a_name, f"b/{rel}"))
    text = "".join(out)
    return text if text.endswith("\n") else text + "\n"


@pytest.fixture
def locked(notes, home):
    """The notes repo registered AND locked (togi lock), ready for apply."""
    lock_bundle("notes")
    return notes


def hone_script(repo: Path, diff: str, tmp_path: Path, expect: int | None = 0, rationale: str = "test"):
    f = tmp_path / "proposal.patch"
    f.write_text(diff)
    return togi_code("hone", "notes", "--driver", "script", "--diff", str(f), "--rationale", rationale)
