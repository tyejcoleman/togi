"""Milestone 2: `togi run` code mode (child process, JSON-pipe proxy, scrubbed env, timeout, NeedsApproval)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import textwrap

import pytest

from togi import approvals

from conftest import SRC, human, lock_bundle, run_in_pty, run_togi, togi


def run(home, script, *flags, env_extra=None, timeout=60):
    return run_togi(home, "run", "-", *flags, input=textwrap.dedent(script), env_extra=env_extra, timeout=timeout)


UNSANDBOXED_HOST = """
import sys
sys.path.insert(0, {src!r})
from togi import runmode, runreg
runreg.register()                      # exactly what the `togi run` host does, but its command line is NOT `togi run`
out = runmode.execute_script(sys.stdin.read(), use_sandbox=False)
sys.stdout.write(out.stdout)
sys.stderr.write(out.stderr)
sys.exit(out.exit_code)
"""


def run_unsandboxed(home, script, env_extra=None, timeout=90):
    """A registered `togi run`-style host whose child is NOT under sandbox-exec: what runs on Linux, or when
    sandbox-exec is unavailable. Used to test the layers that must hold without the sandbox."""
    import subprocess

    from conftest import clean_env

    return subprocess.run([sys.executable, "-c", UNSANDBOXED_HOST.format(src=str(SRC))], input=textwrap.dedent(script),
                          capture_output=True, text=True, env=clean_env(home, **(env_extra or {})), timeout=timeout)


def journal_lines(home):
    p = home / "state" / "notes" / "journal.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()]


def test_script_calls_bundle_functions_and_only_prints_come_back(notes, home):
    r = run(home, """
        a = notes.add(text="from code", tag=["x"])
        notes.add(text="second")
        rows = notes.list()
        print(len(rows), a["id"])
        """)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "2 n_1\n"  # nothing but what the script printed
    assert r.stderr == ""


def test_calls_are_journaled_with_surface_run_and_guarded(notes, home):
    lock_bundle("notes")     # the money path needs a verified lock (notes has locked tiers)
    r = run(home, "notes.add(text='a'); notes.find(query='a'); notes.tip_send(id='n_1', amount='1')")
    assert r.returncode == 3  # uncaught NeedsApproval
    calls = [l for l in journal_lines(home) if l["event"] == "call"]
    assert [(l["surface"], l["command"], l["status"]) for l in calls] == [
        ("run", "add", "ok"), ("run", "find", "ok"), ("run", "tip send", "needs_approval")]
    assert [l["args_shape"] for l in calls] == [{"text": "str"}, {"query": "str"}, {"id": "str", "amount": "decimal"}]
    assert '"a"' not in (home / "state" / "notes" / "journal.jsonl").read_text()  # values never recorded


def test_code_mode_returns_full_data_not_the_cli_page(notes, home):
    r = run(home, "notes.seed(count=120); print(len(notes.list()))")
    assert r.stdout.strip() == "120"  # no --limit 50 cut: the script decides what to print


def test_child_cannot_read_parent_env_secrets(notes, home):
    r = run(home, """
        import os
        print(os.environ.get("MY_API_SECRET"), os.environ.get("TOGI_AGENT"), os.environ.get("AWS_SECRET_ACCESS_KEY"))
        print(sorted(k for k in os.environ if not k.startswith("__CF") and not k.startswith("LC_")))
        """, env_extra={"MY_API_SECRET": "hunter2", "TOGI_AGENT": "claude", "AWS_SECRET_ACCESS_KEY": "zzz"})
    assert r.returncode == 0, r.stderr
    first, second = r.stdout.strip().splitlines()
    assert first == "None claude None"        # TOGI_AGENT is the host's, forwarded on purpose (item 10)
    assert "hunter2" not in r.stdout and "zzz" not in r.stdout
    assert set(eval(second)) <= {"PATH", "HOME", "TMPDIR", "LANG", "TOGI_RUN", "TOGI_HOME", "TOGI_SESSION",
                                 "TOGI_AGENT", "TOGI_APPROVER_HOME"}      # the last: only set under a throwaway approver


def test_child_runs_in_a_temp_dir(notes, home, tmp_path):
    r = run(home, "import os, tempfile; print(os.getcwd() == os.path.realpath(os.environ['TMPDIR']))")
    assert r.stdout.strip() == "True"


def test_timeout_kills_the_script(notes, home):
    r = run(home, "import time; print('started', flush=True); time.sleep(30)", "--timeout", "1")
    assert r.returncode == 1
    assert "started" in r.stdout
    err = json.loads(r.stderr.strip().splitlines()[-1])
    assert err["error"]["code"] == "timeout" and "--timeout" in err["next"]


def test_timeout_kills_child_processes_too(notes, home, tmp_path):
    marker = tmp_path / "survivor"
    r = run(home, f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3); open({str(marker)!r}, 'w').write('x')"])
        time.sleep(30)
        """, "--timeout", "1")
    assert r.returncode == 1
    import time

    time.sleep(3.5)
    assert not marker.exists()


def test_output_cap_and_full(notes, home):
    r = run(home, "print('x' * 20000)")
    assert r.returncode == 0 and len(r.stdout) < 8400 and "output cut at 8192 bytes" in r.stdout
    r = run(home, "print('x' * 20000)", "--full")
    assert len(r.stdout) > 20000 and "output cut" not in r.stdout


def test_money_call_raises_needs_approval_and_does_not_execute(notes, home):
    lock_bundle("notes")     # the money path needs a verified lock (notes has locked tiers)
    r = run(home, """
        notes.add(text="tip me")
        try:
            notes.tip_send(id="n_1", amount="5.00")
        except togi.NeedsApproval as e:
            print("approval:", e.approval_id, e.approval["command"])
        print(notes.tip_total())
        """)
    assert r.returncode == 0, r.stderr
    line1, line2 = r.stdout.strip().splitlines()
    assert line1.startswith("approval: ap_") and line1.endswith("notes tip send")
    assert line2 == "{'count': 0, 'total': '0'}"
    assert [a["status"] for a in togi("approvals")["data"]] == ["pending"]


def test_uncaught_needs_approval_exits_3_with_envelope(notes, home):
    lock_bundle("notes")     # the money path needs a verified lock (notes has locked tiers)
    r = run(home, "notes.add(text='x'); notes.tip_send(id='n_1', amount='5.00')")
    assert r.returncode == 3
    env = json.loads(r.stderr.strip().splitlines()[-1])
    assert env["status"] == "needs_approval" and env["approval"]["id"].startswith("ap_")
    assert env["next"] == f"a person runs: togi approve {env['approval']['id']}"


def _approve_from_script(home, aid):
    return (
        "import json, os, subprocess, sys\n"
        f"env = dict(os.environ, PYTHONPATH={str(SRC)!r}, TOGI_HOME={str(home)!r})\n"
        f"p = subprocess.run([sys.executable, '-m', 'togi', 'approve', {aid!r}], env=env, input='n_1' + chr(10),\n"
        "                   capture_output=True, text=True)\n"
        "print(p.returncode, (json.loads(p.stdout)['error']['code'] if p.stdout.strip() else 'no-output'))\n")


def test_approvals_cannot_be_granted_from_inside_run(notes, home):
    lock_bundle("notes")     # the money path needs a verified lock (notes has locked tiers)
    togi("notes", "add", "--text", "x")
    aid = togi("notes", "tip", "send", "--id", "n_1", "--amount", "2", expect=3)["approval"]["id"]
    r = run(home, _approve_from_script(home, aid))
    assert r.returncode == 0, r.stderr
    code, reason = r.stdout.split()[:2]
    # sandboxed: the nested togi cannot even start (Togi's source and $TOGI_HOME are hidden from the child);
    # unsandboxed: it refuses (see the next test). Either way nothing was granted.
    assert code != "0" and approvals.load(aid)["status"] == "pending"


def test_approvals_cannot_be_granted_from_inside_an_unsandboxed_run(notes, home):
    lock_bundle("notes")     # the money path needs a verified lock (notes has locked tiers)
    togi("notes", "add", "--text", "x")
    aid = togi("notes", "tip", "send", "--id", "n_1", "--amount", "2", expect=3)["approval"]["id"]
    r = run_unsandboxed(home, _approve_from_script(home, aid))
    assert r.returncode == 0, r.stderr
    code, reason = r.stdout.split()[:2]
    assert code == "2" and reason in ("human_only", "needs_tty")
    assert approvals.load(aid)["status"] == "pending"


def test_a_granted_approval_can_be_used_from_run_once(notes, home):
    lock_bundle("notes")     # the money path needs a verified lock (notes has locked tiers)
    togi("notes", "add", "--text", "x")
    aid = togi("notes", "tip", "send", "--id", "n_1", "--amount", "2", expect=3)["approval"]["id"]
    assert human(home, ["approve", aid], "n_1")[0] == 0
    r = run(home, f"""
        print(notes.tip_send(id="n_1", amount="2", approval={aid!r}))
        try:
            notes.tip_send(id="n_1", amount="2", approval={aid!r})
        except togi.TogiError as e:
            print(e.status, e.code)
        """)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().splitlines() == ["{'note': 'n_1', 'amount': '2'}", "refused approval_used"]


def test_typed_api_positional_decimal_import_and_dry_run(notes, home):
    lock_bundle("notes")     # the money path needs a verified lock (notes has locked tiers)
    from decimal import Decimal  # noqa: F401  (documented usage)

    r = run(home, """
        import notes as n
        from decimal import Decimal
        assert n is notes
        print(n.add("positional", ["t"]))
        print(n.delete(id="n_1", dry_run=True)["dry_run"])
        print(n.find.__doc__.splitlines()[0])
        try:
            n.tip_send(id="n_1", amount=Decimal("1.50"))
        except togi.NeedsApproval as e:
            print(e.approval["args"])
        print("add" in dir(n))
        """)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines[0] == "{'id': 'n_1', 'text': 'positional', 'tags': ['t']}"
    assert lines[1] == "True" and lines[2] == "Find notes whose text contains the query."
    assert lines[3] == "{'id': 'n_1', 'amount': '1.50'}" and lines[4] == "True"


def test_bad_calls_raise_togi_error_with_next(notes, home):
    r = run(home, """
        for kwargs in ({"text": "x", "nope": 1}, {}, {"text": ["a"]}):
            try:
                notes.add(**kwargs)
            except togi.TogiError as e:
                print(e.code, "|", e.next)
        try:
            notes.nope()
        except AttributeError as e:
            print("attr", str(e)[:40])
        """)
    assert r.returncode == 0, r.stderr
    out = r.stdout.strip().splitlines()
    assert out[0].startswith("unknown_flag | togi notes add --help")
    assert out[1].startswith("missing_arg |")
    assert out[2].startswith("bad_arg |")
    assert out[3].startswith("attr bundle 'notes' has no command")


def test_script_exception_exits_1_with_clean_traceback(notes, home):
    r = run(home, "x = 1\nnotes.show(id='n_404')")
    assert r.returncode == 1
    env = json.loads(r.stderr.strip().splitlines()[-1])
    assert env["error"]["code"] == "note_not_found" and env["next"].startswith("togi notes find")
    r = run(home, "print('before')\n1/0")
    assert r.returncode == 1 and r.stdout == "before\n"
    assert "ZeroDivisionError" in r.stderr and "_runchild" not in r.stderr
    assert json.loads(r.stderr.strip().splitlines()[-1])["error"]["code"] == "script_error"


def test_refused_error_in_run_exits_2(home, tmp_path):
    repo = tmp_path / "r"
    (repo / "src" / "bad").mkdir(parents=True)
    (repo / "togi.toml").write_text('[bundle]\nname="bad"\nmodule="bad.b:bundle"\nsummary="x"\n')
    (repo / "src" / "bad" / "__init__.py").write_text("")
    (repo / "src" / "bad" / "b.py").write_text(
        "from togi import Bundle, TogiError\nbundle = Bundle('bad')\n"
        "@bundle.command('no', effect='read')\ndef no() -> dict:\n    '''Refuse.'''\n"
        "    raise TogiError('nope', 'guard', next='togi skill bad', status='refused')\n")
    togi("add", str(repo))
    r = run(home, "bad.no()")
    assert r.returncode == 2
    assert json.loads(r.stderr.strip().splitlines()[-1])["status"] == "refused"


def test_json_flag_wraps_streams(notes, home):
    r = run(home, "print('hi')", "--json")
    out = json.loads(r.stdout)
    assert out["ok"] is True and out["data"]["stdout"] == "hi\n" and out["data"]["stderr"] == ""
    assert out["data"]["exit_code"] == 0 and out["data"]["truncated"] is False
    assert out["data"]["sandboxed"] in (True, False)      # always reported (item 4)


def test_run_reads_a_file(notes, home, tmp_path):
    f = tmp_path / "s.py"
    f.write_text("print(notes.add(text='file')['id'])")
    r = run_togi(home, "run", str(f))
    assert r.returncode == 0 and r.stdout.strip() == "n_1"
    r = run_togi(home, "run", str(tmp_path / "missing.py"))
    assert r.returncode == 1 and json.loads(r.stdout)["error"]["code"] == "path_not_found"


ANCESTRY_SCRIPT = """
import os, pty, sys, time
env = {{k: v for k, v in os.environ.items() if not k.startswith('TOGI_')}}      # scrubbed of TOGI_RUN / TOGI_AGENT
env.update(PYTHONPATH={src!r}, TOGI_HOME={home!r})
pid, fd = pty.fork()                                                            # ...and a REAL controlling terminal
if pid == 0:
    os.execve(sys.executable, [sys.executable, '-m', 'togi', 'approve', {aid!r}], env)
for line in (b'n_1', {pw!r}.encode()):                                          # ...even WITH the right passphrase
    try:
        os.write(fd, line + bytes([10]))
    except OSError:                                    # the child already refused and exited: that is the point
        break
out = b''
while True:
    try:
        chunk = os.read(fd, 4096)
    except OSError:
        break
    if not chunk:
        break
    out += chunk
_, status = os.waitpid(pid, 0)
print(os.waitstatus_to_exitcode(status), b'human_only' in out)
"""


def test_ancestry_check_stops_a_script_that_scrubs_togi_run_and_fakes_a_tty(notes, home):
    """Without the sandbox (Linux / no sandbox-exec) a script can strip TOGI_RUN, open a pty AND know the
    passphrase; the run registry still refuses, because a live `togi run` host is among `approve`'s ancestors.
    The host here is `python -c ...`: its command line does not look like `togi run`, so only the registry can
    be what catches it."""
    lock_bundle("notes")     # the money path needs a verified lock (notes has locked tiers)
    from conftest import PASSPHRASE

    togi("notes", "add", "--text", "x")
    aid = togi("notes", "tip", "send", "--id", "n_1", "--amount", "2", expect=3)["approval"]["id"]
    r = run_unsandboxed(home, ANCESTRY_SCRIPT.format(src=str(SRC), home=str(home), aid=aid, pw=PASSPHRASE))
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["2", "True"]
    assert approvals.load(aid)["status"] == "pending"


def test_sandboxed_script_cannot_even_open_a_pty(notes, home):
    from togi import sandbox

    if not sandbox.available()[0]:
        pytest.skip("no sandbox-exec here")
    r = run(home, "import pty\ntry:\n    pty.openpty(); print('pty ok')\nexcept OSError as e:\n    print('no pty:', e)\n")
    assert r.returncode == 0 and r.stdout.startswith("no pty:")


# ----------------------------------------------------------------------------- item 6: finding the run host above us


@pytest.mark.parametrize("cmd,hit", [
    ("/usr/bin/python3 -m togi run -", True), ("python3 -m togi run script.py --timeout 5", True),
    ("python3 -m togi --json run -", True), ("python3 -m togi --text --json run -", True),
    ("/Users/x/bin/togi run -", True), ("togi run script.py", True), ("/Users/x/bin/togi --json run -", True),
    ("/opt/homebrew/bin/python3.12 /Users/x/.local/bin/togi --json run -", True),
    ("/usr/bin/python3 -I /Users/x/togi/src/togi/_launch.py run -", True),       # r4: how the launcher starts the host
    ("/usr/bin/python3 -I /Users/x/togi/src/togi/_launch.py --json run x.py --timeout 5", True),
    ("/usr/bin/python3 -I /Users/x/togi/src/togi/_launch.py approve ap_1", False),
    ("python3 -m togi approve ap_1", False), ("togi notes add --text run", False), ("vim togi run.txt", False),
    ("python3 -m pytest tests/test_run.py", False), ("/bin/zsh -c togi approve", False),
])
def test_run_command_line_pattern_accepts_flags_before_run(cmd, hit):
    from togi import runreg

    assert bool(runreg._RUN_CMD.search(cmd)) is hit


def test_run_host_registers_and_unregisters_itself(notes, home):
    # an unsandboxed child can see $TOGI_HOME: exactly one live host is listed while it runs, none afterwards
    r = run_unsandboxed(home, f"import os\nprint(len(os.listdir({str(home / 'run')!r})))\n")
    assert r.stdout.strip() == "1"
    assert not list((home / "run").iterdir())
    r = run(home, "print('hi')")                           # the real `togi run`: also cleaned up
    assert r.returncode == 0 and (not (home / "run").exists() or not list((home / "run").iterdir()))


def test_started_by_togi_run_uses_the_registry_and_ignores_stale_entries(home, tmp_path):
    from togi import runreg

    assert runreg.started_by_togi_run() is False
    d = home / "run"
    d.mkdir()
    (d / str(os.getppid())).write_text(json.dumps({"pid": os.getppid(), "start": "Mon Jan  1 00:00:00 2001"}))
    assert runreg.started_by_togi_run() is False        # same pid but a different start time: a reused pid
    info = runreg.proc_info(os.getppid())
    (d / str(os.getppid())).write_text(json.dumps({"pid": os.getppid(), "start": info[1]}))
    assert runreg.started_by_togi_run() is True         # this process's parent is "a live togi run host"


# ----------------------------------------------------------------------------- item 4: the host's environment and the sandbox

PROBE_PARENT_ENV = """
import os, sys
def parent_env_blob():
    ppid = os.getppid()
    if sys.platform == "darwin":                  # what `ps -wwE -p $PPID` shows, read directly (ps cannot run in the sandbox)
        import ctypes, ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, ppid)     # CTL_KERN, KERN_PROCARGS2, pid
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            return b""
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return b""
        return buf.raw[:size.value]
    try:
        return open(f"/proc/{ppid}/environ", "rb").read()      # Linux
    except OSError:
        return b""
blob = parent_env_blob()
print("blob", len(blob) > 0, "secret", b"hunter2-secret" in blob, "other", b"AWS_SECRET" in blob)
"""
SECRETS = {"MY_API_SECRET": "hunter2-secret", "AWS_SECRET_ACCESS_KEY": "AWS_SECRET-zzz", "OPENAI_API_KEY": "sk-zzz"}


def test_the_probe_is_not_vacuous_it_sees_an_unscrubbed_hosts_environment(notes, home):
    """If the host were NOT re-exec'd with a scrubbed environment, a child could read it (the refuter's finding)."""
    r = run_unsandboxed(home, PROBE_PARENT_ENV, env_extra=SECRETS)
    assert r.stdout.split() == ["blob", "True", "secret", "True", "other", "True"], r.stderr


def test_togi_run_host_has_no_secrets_in_its_visible_environment(notes, home):
    """Item 4: `togi run` re-execs itself scrubbed, so `ps -E` / /proc/<pid>/environ / sysctl show nothing secret."""
    r = run(home, PROBE_PARENT_ENV, env_extra=SECRETS)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["blob", "True", "secret", "False", "other", "False"]


def test_the_refuters_ps_reproduction_finds_nothing(notes, home):
    script = """
        import os, subprocess
        ppid = os.getppid()
        try:
            out = subprocess.run(["ps", "-wwE", "-o", "command=", "-p", str(ppid)], capture_output=True, text=True).stdout
        except Exception as e:
            out = ""
        print("FAKE_SECRET_TOKEN visible:", "s3cr3t-value-xyz" in out)
        """
    r = run(home, script, env_extra={"FAKE_SECRET_TOKEN": "s3cr3t-value-xyz"})
    assert r.stdout.strip() == "FAKE_SECRET_TOKEN visible: False"


def test_host_environment_is_an_allowlist():
    from togi import paths, runmode

    environ = {"PATH": "/bin", "HOME": "/h", "TMPDIR": "/t", "LANG": "C", "LC_ALL": "C", "LC_CTYPE": "UTF-8",
               "TOGI_HOME": "/th", "TOGI_AGENT": "x", "TOGI_APPROVAL_TTL": "5", "MY_API_SECRET": "s", "PYTHONPATH": "/p",
               "AWS_ACCESS_KEY_ID": "a", "BUNDLE_TOKEN": "b", "SSH_AUTH_SOCK": "/sock", "HTTPS_PROXY": "http://p"}
    env, oob = runmode.host_environment(environ, [])
    assert set(env) == {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TOGI_HOME", "TOGI_AGENT",
                        "TOGI_APPROVAL_TTL"} and oob == {}                         # no PYTHON* at all: the host runs python -I
    env, oob = runmode.host_environment(environ, ["BUNDLE_TOKEN", "HTTPS_PROXY", "NOT_SET_ANYWHERE"])
    assert "BUNDLE_TOKEN" not in env and oob == {"BUNDLE_TOKEN": "b", "HTTPS_PROXY": "http://p"}   # handed over, not left
    env, oob = runmode.host_environment(environ, ["PYTHONPATH"])       # even a manifest cannot carry PYTHONPATH in
    assert not [k for k in env if k.startswith("PYTHON")] and not [k for k in oob if k.startswith("PYTHON")]


def test_reexec_leaves_in_process_callers_alone(home):
    from togi import runmode

    assert runmode.reexec_scrubbed() is False        # a library/test call is not the CLI process: never exec


def test_child_environment_carries_togi_home_session_and_agent(home, monkeypatch):
    from togi import runmode

    env = runmode.scrubbed_env("/tmp/x")
    assert env["TOGI_HOME"] == str(home) and env["TOGI_SESSION"] == "test-session" and "TOGI_AGENT" not in env
    monkeypatch.setenv("TOGI_AGENT", "")
    assert runmode.scrubbed_env("/tmp/x")["TOGI_AGENT"] == ""        # present-but-empty still marks an agent
    monkeypatch.setenv("TOGI_AGENT", "stone")
    assert runmode.scrubbed_env("/tmp/x")["TOGI_AGENT"] == "stone"


def make_env_bundle(tmp_path, name="envb"):
    repo = tmp_path / name
    (repo / "src" / name).mkdir(parents=True)
    (repo / "togi.toml").write_text(
        f'[bundle]\nname="{name}"\nmodule="{name}.b:bundle"\nsummary="x"\n[policy]\nrun_env = ["BUNDLE_SECRET_TOKEN"]\n')
    (repo / "src" / name / "__init__.py").write_text("")
    (repo / "src" / name / "b.py").write_text(
        "import os\nfrom togi import Bundle\nbundle = Bundle('%s')\n"
        "@bundle.command('peek', effect='read')\ndef peek() -> dict:\n    '''What credential the host has.'''\n"
        "    return {'token': os.environ.get('BUNDLE_SECRET_TOKEN'), 'other': os.environ.get('MY_API_SECRET')}\n" % name)
    return repo


def test_run_env_values_reach_the_bundle_but_not_the_visible_environment_or_the_script(home, tmp_path):
    repo = make_env_bundle(tmp_path)
    togi("add", str(repo))
    env_extra = {"BUNDLE_SECRET_TOKEN": "bundle-token-123", "MY_API_SECRET": "hunter2-secret"}
    r = run(home, PROBE_PARENT_ENV + "\nimport os\nprint(os.environ.get('BUNDLE_SECRET_TOKEN'))\n"
                                     "print(envb.peek())\n", env_extra=env_extra)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.strip().splitlines()
    assert lines[0] == "blob True secret False other False"
    assert "bundle-token-123" not in r.stdout.replace("{'token': 'bundle-token-123'", "")      # script cannot see it itself
    assert lines[1] == "None"                                                                   # not in the child's environ
    assert lines[2] == "{'token': 'bundle-token-123', 'other': None}"                           # the bundle (host) has it
    # and the visible environment of the host never held it
    r = run(home, "import os, subprocess\n" + PROBE_PARENT_ENV.replace("hunter2-secret", "bundle-token-123"),
            env_extra=env_extra)
    assert "secret False" in r.stdout


def test_oversize_run_env_falls_back_to_the_environment_instead_of_losing_values(home, tmp_path, monkeypatch):
    from togi import runmode

    monkeypatch.setattr(runmode, "MAX_OOB_BYTES", 10)
    # (the pipe path is covered above; here only the decision: too big for the pipe => kept in the environment)
    env, oob = runmode.host_environment({"PATH": "/bin", "BUNDLE_SECRET_TOKEN": "x" * 50}, ["BUNDLE_SECRET_TOKEN"])
    assert oob == {"BUNDLE_SECRET_TOKEN": "x" * 50} and len(json.dumps(oob)) > runmode.MAX_OOB_BYTES


SANDBOX_PROBE = """
import os, socket
def can(path, mode="r"):
    try:
        with open(path, "rb" if mode == "r" else "w") as f:
            f.read(16) if mode == "r" else f.write("x")
        return True
    except OSError:
        return False
print("repo_env", can({repo_env!r}))
print("repo_code", can({repo_code!r}))
print("togi_home", can({togi_marker!r}))
print("approvals_forge", can({forge!r}, "w"))
print("repo_write", can({repo_write!r}, "w"))
try:
    os.listdir(os.path.expanduser("~")); print("home_list True")
except OSError:
    print("home_list False")
print("write_elsewhere", can("/private/tmp/togi-sandbox-escape", "w"))
try:
    s = socket.create_connection(("127.0.0.1", {port}), timeout=2); s.close(); print("network True")
except OSError:
    print("network False")
print("own_tmp", can(os.path.join(os.environ["TMPDIR"], "ok.txt"), "w"))
print("system_hosts", can("/etc/hosts"))
print("system_usr", can("/usr/bin/true"))
print("bundle_call", notes.add(text="still works")["id"])
"""


@pytest.mark.skipif(sys.platform != "darwin" or not os.access("/usr/bin/sandbox-exec", os.X_OK),
                    reason="sandbox-exec is macOS only")
def test_sandboxed_child_cannot_read_repos_togi_home_or_the_network(notes, home, tmp_path):
    import socket

    (notes / ".env").write_text("BUNDLE_SECRET=top-secret-bundle-value\n")
    (home / "marker.txt").write_text("togi-home-secret\n")
    (home / "approvals").mkdir(exist_ok=True)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    script = SANDBOX_PROBE.format(repo_env=str(notes / ".env"), repo_code=str(notes / "src/notes/store.py"),
                                  togi_marker=str(home / "marker.txt"), forge=str(home / "approvals" / "ap_forged.json"),
                                  repo_write=str(notes / "src/notes/evil.py"), port=srv.getsockname()[1])
    try:
        r = run(home, script, "--json")
    finally:
        srv.close()
    out = json.loads(r.stdout)
    assert out["ok"] and out["data"]["sandboxed"] is True, out
    got = dict(l.rsplit(" ", 1) for l in out["data"]["stdout"].strip().splitlines())
    assert got["repo_env"] == "False" and got["repo_code"] == "False"          # bundle repo, including .env files
    assert got["togi_home"] == "False" and got["approvals_forge"] == "False"   # $TOGI_HOME: no reads, no forged approval
    assert got["repo_write"] == "False" and got["write_elsewhere"] == "False"  # writes only to its own temp dir
    assert got["network"] == "False" and got["own_tmp"] == "True"
    assert got["home_list"] == "False"                                          # the user's home directory
    assert got["system_usr"] == "True"                                           # the OS's own files Python needs
    level = out["data"]["sandbox_level"]
    assert level in ("strict", "standard")
    assert got["system_hosts"] == ("False" if level == "strict" else "True")     # strict allows only OS directories
    assert got["bundle_call"] == "n_1"                                         # proxied calls still work
    assert not (notes / "src/notes/evil.py").exists() and not (home / "approvals" / "ap_forged.json").exists()
    assert (notes / ".env").read_text().startswith("BUNDLE_SECRET")


def test_the_sandbox_probe_is_not_vacuous_an_unsandboxed_child_reads_all_of_it(notes, home, tmp_path):
    import socket

    (notes / ".env").write_text("BUNDLE_SECRET=top-secret-bundle-value\n")
    (home / "marker.txt").write_text("togi-home-secret\n")
    (home / "approvals").mkdir(exist_ok=True)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    script = SANDBOX_PROBE.format(repo_env=str(notes / ".env"), repo_code=str(notes / "src/notes/store.py"),
                                  togi_marker=str(home / "marker.txt"), forge=str(home / "approvals" / "ap_forged.json"),
                                  repo_write=str(tmp_path / "evil.py"), port=srv.getsockname()[1])
    try:
        r = run_unsandboxed(home, script)
    finally:
        srv.close()
    got = dict(l.rsplit(" ", 1) for l in r.stdout.strip().splitlines())
    assert got["repo_env"] == got["repo_code"] == got["togi_home"] == got["approvals_forge"] == "True", r.stderr
    assert got["network"] == "True" and got["home_list"] == "True" and got["repo_write"] == "True"


@pytest.mark.skipif(sys.platform != "darwin" or not os.access("/usr/bin/sandbox-exec", os.X_OK),
                    reason="sandbox-exec is macOS only")
def test_a_script_cannot_import_the_engine_and_move_money_without_approval(notes, home):
    """The Crowd refuter's finding: `togi run -` importing the engine and calling it directly."""
    lock_bundle("notes")     # the money path needs a verified lock (notes has locked tiers)
    script = f"""
        import importlib.util, sys
        for name in ("notes",):
            sys.modules.pop(name, None)
        sys.path.insert(0, {str(notes / 'src')!r})
        try:
            import notes.store as engine
            print("engine imported:", engine.__file__)
        except Exception as e:
            print("engine import failed:", type(e).__name__)
        spec = importlib.util.spec_from_file_location("eng", {str(notes / 'src/notes/store.py')!r})
        try:
            spec.loader.exec_module(importlib.util.module_from_spec(spec)); print("direct load ok")
        except Exception as e:
            print("direct load failed:", type(e).__name__)
        """
    r = run(home, script)
    assert r.returncode == 0, r.stderr
    assert "engine import failed" in r.stdout and "direct load failed" in r.stdout and "imported:" not in r.stdout
    # the supported route is the proxied call, and that meets the approval
    r = run(home, "notes.add(text='x')\ntry:\n    notes.tip_send(id='n_1', amount='1')\nexcept togi.NeedsApproval as e:\n    print('needs approval')")
    assert r.stdout.strip() == "needs approval"


def test_unsandboxed_run_says_so(notes, home, monkeypatch):
    """No sandbox-exec (Linux, or already inside another sandbox): the script still runs, flagged sandboxed:false."""
    from togi import runmode, sandbox

    monkeypatch.setattr(sandbox, "available", lambda: (False, "no OS sandbox on linux (macOS sandbox-exec only)"))
    out = runmode.execute_script("print('hi', notes.add(text='x')['id'])")
    assert out.stdout.strip() == "hi n_1" and out.exit_code == 0
    assert out.sandboxed is False and "no OS sandbox" in out.sandbox_reason


def test_unsandboxed_run_reports_it_in_json_and_on_stderr(notes, home):
    r = run_unsandboxed(home, "print('hi')")                  # the same library path with the sandbox switched off
    assert r.stdout == "hi\n"
    from togi import runmode

    out = runmode.execute_script("print('hi')", use_sandbox=False)
    assert out.sandboxed is False and out.sandbox_reason == "sandbox disabled by the caller"


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
def test_sandbox_preflight_failure_falls_back_cleanly(notes, home, tmp_path, monkeypatch):
    """sandbox-exec present but unusable here (e.g. a nested sandbox refuses it): run unsandboxed, say why."""
    from togi import runmode, sandbox

    fake = tmp_path / "sandbox-exec"
    fake.write_text("#!/bin/sh\necho 'sandbox_apply: Operation not permitted' >&2\nexit 71\n")
    fake.chmod(0o755)
    monkeypatch.setattr(sandbox, "SANDBOX_EXEC", str(fake))
    out = runmode.execute_script("print('still runs')")
    assert out.stdout.strip() == "still runs" and out.sandboxed is False
    assert "cannot start Python here" in out.sandbox_reason and "sandbox_apply" in out.sandbox_reason


@pytest.mark.skipif(sys.platform != "darwin" or not os.access("/usr/bin/sandbox-exec", os.X_OK),
                    reason="sandbox-exec is macOS only")
def test_strict_profile_is_used_when_it_works_and_the_standard_one_when_it_does_not(notes, home, monkeypatch):
    from togi import runmode, sandbox

    out = runmode.execute_script("print('hi')")
    assert out.sandboxed is True and out.sandbox_level == "strict" and out.stdout == "hi\n"
    real_trial = sandbox._trial
    monkeypatch.setattr(sandbox, "_trial", lambda profile, tmp, home: (False, "forced failure") if "(deny file-read*)\n" in profile
                        else real_trial(profile, tmp, home))        # as if the strict profile could not start Python here
    out = runmode.execute_script("print('hi')")
    assert out.sandboxed is True and out.sandbox_level == "standard" and out.stdout == "hi\n"
    assert "strict profile unusable here" in out.sandbox_reason       # said, even though it is sandboxed


def test_sandbox_profile_hides_repos_and_togi_home_and_quotes_paths(tmp_path):
    from togi import sandbox

    strict = sandbox.build_profile(tmp=str(tmp_path / "t"), repos=[str(tmp_path / "repo1")], togi_home=str(tmp_path / "th"),
                                   home=str(tmp_path / "home"), strict=True)
    assert "(deny file-read*)" in strict and "(allow file-read-metadata)" in strict and '(subpath "/usr")' in strict
    assert "/Users" not in strict.split("(allow file-read*")[0]        # not a deny-list: the default is deny
    assert strict.strip().splitlines()[-1].startswith("(deny file* ") and "repo1" in strict.strip().splitlines()[-1]

    prof = sandbox.build_profile(tmp=str(tmp_path / "t"), repos=[str(tmp_path / "repo1"), '/odd"path/repo2'],
                                 togi_home=str(tmp_path / "th"), home=str(tmp_path / "home"))
    assert "(deny network*)" in prof and "(deny process-info* (target others))" in prof and "(deny file-write*)" in prof
    last_deny = prof.strip().splitlines()[-1]
    assert last_deny.startswith("(deny file*") and "repo1" in last_deny and "th" in last_deny      # denied LAST: wins
    assert '/odd\\"path/repo2' in prof and '"/odd"path' not in prof                                  # no injection
    with pytest.raises(ValueError):
        sandbox.build_profile(tmp=str(tmp_path), repos=["/bad\nrepo"], togi_home=str(tmp_path), home=str(tmp_path))


UNSANDBOXED_NESTED = """
import os, subprocess, sys
env = dict(os.environ, PYTHONPATH={src!r})
p = subprocess.run([sys.executable, "-m", "togi", "notes", "tip", "send", "--id", "n_1", "--amount", "3.00"],
                   env=env, capture_output=True, text=True)
print(p.returncode, "needs_approval" in p.stdout)
"""


def test_nested_togi_call_from_a_script_stays_in_the_hosts_home(notes, home, tmp_path):
    """Item 10: the refuter's nested.py. Without TOGI_HOME the nested call fell back to the user's real ~/.togi."""
    lock_bundle("notes")     # the money path needs a verified lock (notes has locked tiers)
    fake_home = tmp_path / "realhome"
    (fake_home / ".togi").mkdir(parents=True)
    shutil.copy(home / "bundles.toml", fake_home / ".togi" / "bundles.toml")        # the user's REAL registry
    togi("notes", "add", "--text", "x")
    r = run_unsandboxed(home, UNSANDBOXED_NESTED.format(src=str(SRC)), env_extra={"HOME": str(fake_home)})
    assert r.returncode == 0 and r.stdout.split() == ["3", "True"], r.stderr
    assert not (fake_home / ".togi" / "approvals").exists()                         # nothing leaked to the real home
    assert any((home / "approvals").glob("ap_*.json"))                              # it landed in the host's home
