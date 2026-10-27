"""Fix round 4 (FIXES-r4.md, the final design review of a47ced5): one section per item, each with tests that FAIL on
a47ced5 and pass now.

    H1  the human gates ran code from the current directory / PYTHONPATH      -> isolated launcher
    H2  test-scope approvals moved money in bundles that never checked scope  -> [policy] test_approvals
    M1  what the person saw at approve was not what got signed                -> recomputed from live state
    M2  single use was a field in a file the agent can edit                   -> signed spent record outside $TOGI_HOME
    M3  the money path never checked the lock                                 -> require_lock, lock state in the prompt
    M4  the evaluation cache was unsigned                                     -> MAC with the audit key
    M5  output caps failed on the CLI (wide dicts, long strings, prints, run) -> fit_any + hard cap + stdout capture
    L1..L5, run-mode site-packages

Nothing here touches the real approver: the throwaway approver comes from the `home` fixture, the "fixed" one from the
faked password database (see conftest).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from togi import apply as apply_mod
from togi import approvals, approver, audit, host, journal, output, paths, runmode, sandbox, stone
from togi.errors import TogiError

from conftest import (PASSPHRASE, ROOT, SRC, answers, clean_env, hone_script, human, lock_bundle, make_diff, run_in_pty,
                      run_togi, togi, togi_code)

BIN_TOGI = ROOT / "bin" / "togi"
MSG = "togi_messages.toml"


# ----------------------------------------------------------------------------- shared helpers


def planted(directory: Path, names=("hmac", "json", "decimal")) -> Path:
    """Stdlib-named files that record that they were imported. Returns the marker file they write."""
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "PLANTED"
    for n in names:
        (directory / f"{n}.py").write_text(f"open({str(marker)!r}, 'a').write({n!r} + '\\n')\n")
    return marker


@pytest.fixture
def python3_on_path(tmp_path):
    """A PATH whose `python3` is the interpreter running the tests, so bin/togi (which says `python3`) is the real
    launcher under test."""
    d = tmp_path / "pybin"
    d.mkdir()
    (d / "python3").symlink_to(sys.executable)
    return f"{d}{os.pathsep}{os.environ['PATH']}"


def tip_request(amount: str = "5.00", note: str = "n_1") -> str:
    code, out = togi_code("notes", "tip", "send", "--id", note, "--amount", amount)
    assert code == 3, out
    return out["approval"]["id"]


@pytest.fixture
def pending(notes, home):
    """A locked notes bundle, one note, and a pending tip approval request. Returns the approval id."""
    lock_bundle("notes")
    togi("notes", "add", "--text", "tip me")
    return tip_request()


def approved_by_library(amount: str = "5.00") -> str:
    """Request through the CLI, grant through the library under the throwaway approver (fast: no terminal)."""
    aid = tip_request(amount)
    approvals.grant(aid, "approve", "someone")
    return aid


def edit_approval(home: Path, aid: str, fn) -> dict:
    p = home / "approvals" / f"{aid}.json"
    rec = json.loads(p.read_text())
    fn(rec)
    p.write_text(json.dumps(rec, indent=1, sort_keys=True))
    return rec


def tips_sent() -> int:
    return togi("notes", "tip", "total")["data"]["count"]


# ============================================================================= H1: an isolated interpreter


@pytest.mark.parametrize("where", ["cwd", "pythonpath"])
@pytest.mark.parametrize("module", ["hmac", "json", "decimal"])
def test_h1_a_planted_stdlib_file_never_runs_inside_a_persons_togi_approve(pending, home, tmp_path, python3_on_path,
                                                                           where, module):
    """The review's probe: `hmac.py` / `json.py` in the directory where the person runs `togi approve` (or on their
    PYTHONPATH) ran inside the approval session. Through the real launcher (bin/togi) nothing planted is imported."""
    plant = tmp_path / "plant"
    marker = planted(plant, (module,))
    extra = {"PATH": python3_on_path}
    if where == "pythonpath":
        extra["PYTHONPATH"] = str(plant)
        cwd, drop = tmp_path, ()
    else:
        cwd, drop = plant, ("PYTHONPATH",)
    code, shown = run_in_pty(home, ["approve", pending], typed=answers("n_1"), env_extra=extra, drop_env=drop,
                             cwd=cwd, cmd=[str(BIN_TOGI), "approve", pending])
    assert not marker.exists(), f"{module}.py from the {where} ran inside togi approve:\n{marker.read_text()}"
    assert code == 0, shown
    assert approvals.load(pending)["status"] == "approved"


def test_h1_bin_togi_ignores_pythonpath_and_the_current_directory(home, tmp_path, python3_on_path):
    plant = tmp_path / "plant"
    marker = planted(plant)
    env = clean_env(home, PATH=python3_on_path, PYTHONPATH=str(plant))
    r = subprocess.run([str(BIN_TOGI), "version"], cwd=plant, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout)["data"]["togi"], r.stderr
    assert not marker.exists()


def test_h1_python_dash_m_togi_hands_over_to_the_isolated_launcher_before_importing_anything(home, tmp_path):
    """`python3 -m togi` puts the current directory first on sys.path; the package's first act is to restart through the
    launcher, so even `decimal.py` (the first stdlib import of togi) planted in the cwd does not run."""
    plant = tmp_path / "plant"
    marker = planted(plant)
    r = subprocess.run([sys.executable, "-m", "togi", "version"], cwd=plant, env=clean_env(home), capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout)["data"]["togi"], r.stderr
    assert not marker.exists()


def test_h1_togi_run_and_its_reexec_host_are_isolated_too(notes, home, tmp_path, python3_on_path):
    plant = tmp_path / "plant"
    marker = planted(plant)
    r = subprocess.run([str(BIN_TOGI), "run", "-"], input="print('hello from run')", cwd=plant, capture_output=True,
                       text=True, timeout=60, env=clean_env(home, PATH=python3_on_path, PYTHONPATH=str(plant)))
    assert r.returncode == 0 and r.stdout.strip() == "hello from run", r.stderr
    assert not marker.exists()


def test_h1_the_run_host_reexec_uses_the_isolated_launcher_and_no_python_variables(monkeypatch):
    from togi import cli

    seen = {}
    monkeypatch.setattr(cli, "PROCESS_ENTRY", True)
    monkeypatch.setattr(cli, "ENTRY_ARGV", ["run", "-"])
    monkeypatch.delenv("TOGI_RUN_SCRUBBED", raising=False)
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setattr(runmode.os, "execve", lambda exe, argv, env: seen.update(exe=exe, argv=argv, env=env))
    runmode.reexec_scrubbed()
    assert seen["argv"] == paths.isolated_argv(["run", "-"]) and seen["argv"][1] == "-I"
    assert not [k for k in seen["env"] if k.startswith("PYTHON")]


def test_h1_main_restarts_through_the_launcher_when_it_is_the_entry_point_of_a_normal_interpreter(monkeypatch):
    from togi import cli

    seen = []
    monkeypatch.setattr(cli, "PROCESS_ENTRY", False)                      # main() sets these; monkeypatch restores them
    monkeypatch.setattr(cli, "ENTRY_ARGV", [])
    monkeypatch.setattr(cli.os, "execv", lambda exe, argv: seen.append((exe, argv)) or (_ for _ in ()).throw(SystemExit))
    monkeypatch.setattr(sys, "argv", ["togi", "version"])
    with pytest.raises(SystemExit):
        cli.main()
    assert seen == [(sys.executable, paths.isolated_argv(["version"]))]


def test_h1_the_launcher_script_started_without_dash_I_restarts_itself_isolated(tmp_path):
    """Run directly WITHOUT -I, the launcher restarts itself with it; either way sys.flags.isolated is what runs togi."""
    plant = tmp_path / "plant"
    marker = planted(plant)
    r = subprocess.run([sys.executable, str(paths.launcher_path()), "version"], cwd=plant,
                       env={**os.environ, "PYTHONPATH": str(plant)}, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout)["data"]["togi"], r.stderr
    assert not marker.exists()


def test_h1_human_gates_refuse_a_non_isolated_interpreter_with_python_variables_or_the_cwd_on_the_path(home, monkeypatch):
    """Defence in depth: a gate that finds itself in an interpreter that is neither isolated nor visibly clean refuses
    before it asks for anything."""
    monkeypatch.setattr(approvals, "_isatty", lambda f: True)
    monkeypatch.setattr(approvals.runreg, "started_by_togi_run", lambda: False)
    clean_path = [p for p in sys.path if p not in ("", ".") and os.path.realpath(p) != os.path.realpath(os.getcwd())]
    monkeypatch.setattr(sys, "path", clean_path)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    for k in [k for k in os.environ if k.startswith("PYTHON")]:
        monkeypatch.delenv(k)
    approvals.human_gate("approve")                                       # visibly clean: fine
    monkeypatch.setenv("PYTHONPATH", "/tmp/anything")
    with pytest.raises(TogiError) as e:
        approvals.human_gate("approve")
    assert e.value.code == "unsafe_interpreter" and e.value.status == "refused" and "PYTHONPATH" in e.value.message
    monkeypatch.delenv("PYTHONPATH")
    monkeypatch.setenv("PYTHONSTARTUP", "/tmp/x.py")                      # any PYTHON* variable counts
    with pytest.raises(TogiError) as e:
        approvals.human_gate("deny")
    assert e.value.code == "unsafe_interpreter"
    monkeypatch.delenv("PYTHONSTARTUP")
    for entry in ("", ".", os.getcwd()):                                  # the current directory, however spelled
        monkeypatch.setattr(sys, "path", [entry, *clean_path])
        with pytest.raises(TogiError) as e:
            approvals.human_gate("approve")
        assert e.value.code == "unsafe_interpreter" and "current directory" in e.value.message
    # an isolated interpreter ignores PYTHON* and has no cwd on its path: it passes whatever the variables say
    monkeypatch.setenv("PYTHONPATH", "/tmp/anything")
    monkeypatch.setattr(sys, "path", ["", *clean_path])
    monkeypatch.setattr(sys, "flags", type("F", (), {"isolated": 1})())
    approvals.human_gate("approve")


def test_h1_every_repo_gate_checks_the_interpreter_too(home, monkeypatch):
    monkeypatch.setattr(approvals, "_isatty", lambda f: True)
    monkeypatch.setattr(approvals.runreg, "started_by_togi_run", lambda: False)
    monkeypatch.setenv("PYTHONPATH", "/tmp/anything")
    monkeypatch.delenv("TOGI_APPROVER_HOME")
    with pytest.raises(TogiError) as e:                       # default scope here (no override), so the scope check passes
        approvals.human_repo_gate("lock")
    assert e.value.code == "unsafe_interpreter"


def test_h1_the_stone_shim_and_mcp_server_are_started_isolated(notes, home):
    sb = stone.Sandbox(notes, "notes")
    try:
        shim = (sb.bin / "togi").read_text()
        assert " -I " in shim and "PYTHONPATH" not in shim and str(paths.launcher_path()) in shim
        cfg = stone.mcp_config(sb)["mcpServers"]["togi"]
        assert cfg["args"][0] == "-I" and "PYTHONPATH" not in cfg["env"]
        assert sb.sh("togi version").stdout.startswith('{"ok":true')
    finally:
        sb.cleanup()


# ============================================================================= H2: test_approvals opt-in


LEDGER_TOML = """[bundle]
name = "ledger"
version = "0.1.0"
summary = "One money command, for the approval-scope tests."
module = "ledger.togi_bundle:bundle"

[tiers]
polish = ["skills/**"]
locked = ["togi.toml"]

[stone]
tests = "tests"

[policy]
approve_effects = ["money"]
require_lock = false
{policy}
"""

LEDGER_PY = '''from togi import Bundle, Context

bundle = Bundle("ledger")


@bundle.command("pay", effect="money", confirm="to", examples=["togi ledger pay --to bob"])
def pay(to: str, ctx: Context = None) -> dict:
    """Pay someone (reports the scope of the approval it ran under)."""
    return {"to": to, "scope": ctx.approval_scope}


@bundle.command("log", effect="write", examples=["togi ledger log"])
def log(ctx: Context = None) -> dict:
    """Write a note with an amount in it into the journal."""
    ctx.journal_note({"amount": "5.00", "who": "bob", "n": 3, "tags": ["a"], "flag": True})
    return {"done": True}


@bundle.command("peek", effect="read", examples=["togi ledger peek"])
def peek(ctx: Context = None) -> dict:
    """Read-only; its note is stored as written."""
    ctx.journal_note({"seen": "yes"})
    return {"seen": True}
'''


@pytest.fixture
def make_ledger(tmp_path):
    def make(policy: str = "", name: str = "ledger_repo") -> Path:
        repo = tmp_path / name
        (repo / "src" / "ledger").mkdir(parents=True)
        (repo / "togi.toml").write_text(LEDGER_TOML.format(policy=policy))
        (repo / "src" / "ledger" / "__init__.py").write_text("")
        (repo / "src" / "ledger" / "togi_bundle.py").write_text(LEDGER_PY)
        return repo

    return make


def test_h2_a_test_scope_grant_is_refused_for_a_bundle_that_has_not_opted_in(home, make_ledger):
    togi("add", str(make_ledger()))
    code, out = togi_code("ledger", "pay", "--to", "bob")
    assert code == 3
    aid = out["approval"]["id"]
    # 1. the person is told up front, before any prompt, and nothing is granted
    code, shown = human(home, ["approve", aid], "bob")                    # the throwaway (test-scope) approver
    assert code == 2 and "test_scope_refused" in shown, shown
    assert approvals.load(aid)["status"] == "pending" and "grant_mac" not in approvals.load(aid)
    # 2. a grant minted anyway (same throwaway approver, no terminal) is refused when it is USED, and nothing runs
    approvals.grant(aid, "approve", "someone")
    code, out = togi_code("ledger", "pay", "--to", "bob", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "test_scope_refused" and out["status"] == "refused"
    assert approvals.load(aid)["status"] == "approved"                  # not consumed: it was never accepted


def test_h2_a_bundle_that_opts_in_accepts_test_scope_and_sees_it(home, make_ledger):
    togi("add", str(make_ledger("test_approvals = true")))
    code, out = togi_code("ledger", "pay", "--to", "bob")
    aid = out["approval"]["id"]
    code, shown = human(home, ["approve", aid], "bob")
    assert code == 0, shown
    assert togi("ledger", "pay", "--to", "bob", "--approval", aid)["data"] == {"to": "bob", "scope": "test"}


def test_h2_the_default_scope_approver_needs_no_opt_in(home, make_ledger):
    togi("add", str(make_ledger()))
    code, out = togi_code("ledger", "pay", "--to", "bob")
    aid = out["approval"]["id"]
    code, shown = human(home, ["approve", aid], "bob", scope="default")   # the fixed approver (faked pw dir), no override
    assert code == 0, shown
    r = run_togi(home, "ledger", "pay", "--to", "bob", "--approval", aid, scope="default")
    assert r.returncode == 0, r.stdout
    assert json.loads(r.stdout)["data"] == {"to": "bob", "scope": "default"}


def test_h2_test_approvals_must_be_a_real_boolean(make_ledger):
    from togi.manifest import ManifestError, load_manifest

    assert load_manifest(make_ledger("")).test_approvals is False
    assert load_manifest(make_ledger("test_approvals = true", "r2")).test_approvals is True
    for i, bad in enumerate(['"true"', "1", '"yes"', "[true]"]):
        with pytest.raises(ManifestError, match="test_approvals"):
            load_manifest(make_ledger(f"test_approvals = {bad}", f"bad{i}"))


def test_h2_only_a_stone_sandbox_gets_the_opt_in(notes, home):
    sb = stone.Sandbox(notes, "notes")
    try:
        inside = sb.env(agent=True)
        with mock.patch.dict(os.environ, inside):
            assert approvals.sandbox_optin() is True
        with mock.patch.dict(os.environ, {"TOGI_STONE_SANDBOX": inside["TOGI_STONE_SANDBOX"]}):
            assert approvals.sandbox_optin() is False                   # the variable alone, in another home: nothing
        bare = {**inside, "TOGI_HOME": str(home)}                       # right root, wrong home
        with mock.patch.dict(os.environ, bare):
            assert approvals.sandbox_optin() is False
        (sb.root / approvals.SANDBOX_MARKER).unlink()
        with mock.patch.dict(os.environ, inside):
            assert approvals.sandbox_optin() is False                   # no marker: not a sandbox the harness made
    finally:
        sb.cleanup()
    assert approvals.sandbox_optin() is False


def test_h2_a_bundles_own_tests_run_in_the_stone_sandbox(make_ledger):
    from togi.manifest import load_manifest

    repo = make_ledger()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_sandbox.py").write_text(
        "from togi import approvals\n\ndef test_i_am_in_the_stone_sandbox():\n    assert approvals.sandbox_optin()\n")
    res = stone.run_tests(repo, load_manifest(repo))
    assert res["ok"] and res["passed"] == 1, res


def test_h2_notes_tip_send_checks_the_scope_the_way_the_spec_says(notes, home, monkeypatch):
    """examples/notes: tip_send refuses a test-scope approval outside its own test mode (NOTES_TEST_MODE)."""
    lock_bundle("notes")
    togi("notes", "add", "--text", "tip me")
    monkeypatch.delenv("NOTES_TEST_MODE")                                 # leave the bundle's test mode
    aid = approved_by_library()
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "5.00", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "test_scope_refused" and "throwaway" in out["error"]["message"]
    monkeypatch.setenv("NOTES_TEST_MODE", "1")
    assert tips_sent() == 0
    aid = approved_by_library("6.00")
    assert togi("notes", "tip", "send", "--id", "n_1", "--amount", "6.00", "--approval", aid)["ok"] is True
    assert tips_sent() == 1


# ============================================================================= M1: what is shown is what is signed


def approve_in_pty(home, aid, word="n_1", **kw):
    return human(home, ["approve", aid], word, **kw)


def test_m1_args_edited_after_the_request_are_refused_not_shown_and_signed(pending, home):
    """The pending file is the requester's. Edit its args and the person would have been shown 0.01 while the grant
    signed the hash of the original; now the args are re-hashed first and the mismatch refuses."""
    edit_approval(home, pending, lambda r: r["args"].update(amount="0.01"))
    code, shown = approve_in_pty(home, pending)
    assert code == 2 and "approval_tampered" in shown and "0.01" not in shown.split("approval_tampered")[0], shown
    rec = approvals.load(pending)
    assert rec["status"] == "pending" and "grant_mac" not in rec


def test_m1_a_rewritten_summary_or_confirmation_word_is_refused(pending, home):
    edit_approval(home, pending, lambda r: r.update(summary="notes tip send: a harmless test tip (amount=0.01)"))
    code, shown = approve_in_pty(home, pending)
    assert code == 2 and "approval_tampered" in shown and "harmless" not in shown
    assert approvals.load(pending)["status"] == "pending"
    real = json.loads((home / "approvals" / f"{pending}.json").read_text())
    edit_approval(home, pending, lambda r: r.update(summary=approvals.load(pending)["summary"], confirm_word="yes"))
    assert real["confirm_word"] == "n_1"
    code, shown = approve_in_pty(home, pending, word="yes")                # the easy word the requester picked
    assert code == 2 and "approval_tampered" in shown
    assert approvals.load(pending)["status"] == "pending"


def test_m1_the_prompt_and_the_signature_come_from_the_bundles_own_command(pending, home):
    code, shown = approve_in_pty(home, pending)
    assert code == 0, shown
    assert "Send a tip to a note's author" in shown and "amount = \"5.00\"" in shown and 'Type "n_1"' in shown
    rec = approvals.load(pending)
    assert rec["status"] == "approved" and rec["summary"].startswith("notes tip send: Send a tip")
    assert rec["binding"]["repo"] == str(approvals.load(pending)["binding"]["repo"])


def test_m1_the_binding_is_recomputed_from_the_live_repo(pending, home, notes_repo):
    """The bundle changed after the request (a locked file): approving would sign a grant that can never be used."""
    toml = notes_repo / "togi.toml"
    toml.write_text(toml.read_text().replace('version = "0.1.0"', 'version = "0.1.1"'))
    lock_bundle("notes")
    code, shown = approve_in_pty(home, pending)
    assert code == 2 and "approval_stale" in shown, shown
    assert approvals.load(pending)["status"] == "pending"


def test_m1_a_record_that_lies_about_its_binding_is_refused(pending, home):
    edit_approval(home, pending, lambda r: r.update(binding_hash="0" * 64))
    code, shown = approve_in_pty(home, pending)
    assert code == 2 and "approval_stale" in shown
    assert approvals.load(pending)["status"] == "pending"


def test_m1_a_pending_file_for_an_unregistered_bundle_or_unknown_command_is_refused(pending, home):
    edit_approval(home, pending, lambda r: r.update(command="tip steal"))
    code, shown = approve_in_pty(home, pending)
    assert code == 2 and ("approval_tampered" in shown or "describe_failed" in shown), shown
    edit_approval(home, pending, lambda r: r.update(command="tip send", bundle="nonesuch"))
    code, shown = approve_in_pty(home, pending)
    assert code == 2 and "unknown_bundle" in shown
    assert approvals.load(pending)["status"] == "pending"


def test_m1_a_requester_cannot_make_an_approval_impossible_to_deny(pending, home):
    edit_approval(home, pending, lambda r: r.update(confirm_word="x" * 10000))
    code, shown = human(home, ["deny", pending], pending[-4:])
    assert code == 0, shown
    assert approvals.load(pending)["status"] == "denied"


def test_m2_the_real_home_guard_watches_the_spent_record_too(tmp_path):
    import conftest

    (tmp_path / "spent.jsonl").write_text("x")
    (tmp_path / "spent.head").write_text("x")
    (tmp_path / "bundles.toml").write_text("x")
    assert set(conftest.approver_files(tmp_path)) == {"spent.jsonl", "spent.head"}


def test_m1_what_changed_between_checking_and_signing_is_not_signed(pending, home):
    rec = approvals.load(pending)
    verified = approvals.verify_pending(pending, rec)
    assert verified["summary"] == rec["summary"] and verified["confirm_word"] == "n_1"
    edit_approval(home, pending, lambda r: r["args"].update(amount="500.00"))     # after the person was shown 5.00
    with pytest.raises(TogiError) as e:
        approvals.grant(pending, "approve", "someone", verified=verified)
    assert e.value.code == "approval_changed"
    assert approvals.load(pending)["status"] == "pending"


def test_m1_the_bundle_is_never_loaded_in_the_approval_session(notes, home, notes_repo):
    """r4: reading the command's summary needed the bundle's import-time code, so it ran in a scrubbed child. r5 (FIXES-r5
    H2) goes further: the summary comes from the describe snapshot `togi lock` signed, so approving imports nothing at all
    (and a bundle with no snapshot is read by a sandboxed child, tests/test_fixes_r5.py)."""
    log = home / "probe.log"
    probe = notes_repo / "src" / "notes" / "probe.py"
    probe.write_text("import json, os, sys\n"
                     "try:\n"
                     f"    with open({str(log)!r}, 'a') as f:\n"
                     "        f.write(json.dumps({'secret': os.environ.get('LEAK_CHECK'), 'isolated': sys.flags.isolated,\n"
                     "                            'pid': os.getpid()}) + '\\n')\n"
                     "except OSError:\n"
                     "    pass\n")
    init = notes_repo / "src" / "notes" / "__init__.py"
    init.write_text(init.read_text() + "\nfrom . import probe  # noqa: F401\n")
    toml = notes_repo / "togi.toml"                                         # (r5: new code in the gated package is tiered)
    toml.write_text(toml.read_text().replace('reshape = ["src/notes/format.py"]', 'reshape = ["src/notes/format.py", "src/notes/probe.py"]'))
    lock_bundle("notes")
    togi("notes", "add", "--text", "tip me")
    aid = tip_request()                                                     # (the host imports the bundle: its own job)
    assert log.exists()
    log.write_text("")
    code, shown = human(home, ["approve", aid], "n_1", env_extra={"LEAK_CHECK": "the-persons-secret"})
    assert code == 0, shown
    assert log.read_text() == "", "the bundle's import-time code ran while a person was approving"


# ============================================================================= M2: single use is signed and kept outside $TOGI_HOME


def test_m2_flipping_a_consumed_approval_back_to_approved_does_not_make_it_reusable(pending, home):
    aid = approved_by_library()
    assert togi("notes", "tip", "send", "--id", "n_1", "--amount", "5.00", "--approval", aid)["ok"] is True
    assert tips_sent() == 1
    edit_approval(home, aid, lambda r: r.update(status="approved"))           # grant_mac covers "approved", not "unused"
    (home / "approvals" / f"{aid}.claimed").unlink()
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "5.00", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_used", out
    assert tips_sent() == 1


def test_m2_a_claimed_marker_alone_blocks_use(pending, home):
    aid = approved_by_library()
    (home / "approvals" / f"{aid}.claimed").write_text("")
    h = approvals.args_hash("notes", "tip send", approvals.load(aid)["args"])
    with pytest.raises(TogiError) as e:
        approvals.check_for_use(aid, "notes", "tip send", h, allow_test=True)
    assert e.value.code == "approval_used"


def test_m2_the_spent_record_lives_in_the_approver_home_signed_not_in_togi_home(pending, home):
    from togi import spent

    aid = approved_by_library()
    togi("notes", "tip", "send", "--id", "n_1", "--amount", "5.00", "--approval", aid)
    log = paths.approver_home() / "spent.jsonl"
    assert log == spent.log_path() and log.is_file() and home not in log.parents
    assert not list(home.rglob("spent*"))
    rec = json.loads(log.read_text().splitlines()[0])
    assert rec["id"] == aid and rec["n"] == 1 and rec["prev"] == "0" * 64 and len(rec["mac"]) == 64
    head = paths.approver_home() / "spent.head"
    assert head.is_file() and spent.is_spent(aid) is True
    assert oct(log.stat().st_mode & 0o777) == "0o600" and oct(head.stat().st_mode & 0o777) == "0o600"


def test_m2_the_spent_record_is_written_before_the_command_runs(pending, home, monkeypatch):
    from togi import spent

    aid = approved_by_library()
    seen = {}
    real_invoke = host.invoke

    def watching(loaded, cmd, full, surface, session, agent, approval_scope=None, extra=None):
        seen["spent_while_running"] = spent.is_spent(aid)
        return real_invoke(loaded, cmd, full, surface, session, agent, approval_scope=approval_scope, extra=extra)

    monkeypatch.setattr(host, "invoke", watching)
    assert togi("notes", "tip", "send", "--id", "n_1", "--amount", "5.00", "--approval", aid)["ok"] is True
    assert seen == {"spent_while_running": True}


def test_m2_if_the_record_cannot_be_written_the_command_does_not_run(pending, home, monkeypatch):
    from togi import spent

    aid = approved_by_library()
    monkeypatch.setattr(spent, "_write_head", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "5.00", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "spent_log_unwritable"
    assert tips_sent() == 0


def test_m2_two_claims_race_and_exactly_one_wins(pending, home):
    from togi import spent

    aid = approved_by_library()
    results = []

    def claim():
        try:
            spent.claim(aid)
            results.append("won")
        except TogiError as e:
            results.append(e.code)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == ["approval_used"] * 7 + ["won"]


def spend_three():
    ids = [approved_by_library(f"{i}.00") for i in (1, 2, 3)]
    for i, aid in zip((1, 2, 3), ids):
        assert togi("notes", "tip", "send", "--id", "n_1", "--amount", f"{i}.00", "--approval", aid)["ok"] is True
    return ids


@pytest.mark.parametrize("damage", ["drop_middle_line", "edit_an_id", "truncate_the_tail", "reorder", "forged_line"])
def test_m2_a_damaged_spent_record_fails_closed_for_every_approval(pending, home, damage):
    from togi import spent

    ids = spend_three()
    log = spent.log_path()
    lines = log.read_text().splitlines()
    if damage == "drop_middle_line":
        del lines[1]
    elif damage == "edit_an_id":
        lines[0] = lines[0].replace(ids[0], "ap_0000000000")
    elif damage == "truncate_the_tail":
        del lines[2]
    elif damage == "reorder":
        lines[0], lines[1] = lines[1], lines[0]
    elif damage == "forged_line":
        forged = json.loads(lines[2])
        forged.update(n=4, id="ap_ffffffffff", prev=forged["mac"], mac="0" * 64)
        lines.append(json.dumps(forged))
    log.write_text("\n".join(lines) + "\n")
    fresh = approved_by_library("9.00")                                   # an unrelated, never-used approval
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "9.00", "--approval", fresh)
    assert code == 2 and out["error"]["code"] == "spent_log_damaged", out
    assert tips_sent() == 3


def test_m2_a_torn_or_garbage_last_line_cannot_unspend_anything_and_does_not_break_the_record(pending, home):
    from togi import spent

    ids = spend_three()
    with open(spent.log_path(), "a") as f:
        f.write('{"n": 4, "id": "ap_half')                                 # a crashed writer: no newline, not JSON
    assert spent.is_spent(ids[0]) and spent.is_spent(ids[2])
    fresh = approved_by_library("9.00")
    assert togi("notes", "tip", "send", "--id", "n_1", "--amount", "9.00", "--approval", fresh)["ok"] is True
    assert spent.is_spent(fresh) and all(spent.is_spent(i) for i in ids)
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "1.00", "--approval", ids[0])
    assert code == 2 and out["error"]["code"] == "approval_used"


def test_m2_replacing_the_approver_key_retires_the_record_that_only_it_could_verify(pending, home):
    from togi import spent

    ids = spend_three()
    assert spent.log_path().is_file()
    approver_key = paths.approver_key_path()
    approver_key.unlink()
    approver.ensure_key(repair=True)
    assert not spent.log_path().exists() and list(paths.approver_home().glob("spent.jsonl.*.old"))
    # the old grants were signed with the old key: dead, not replayable
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "1.00", "--approval", ids[0])
    assert code == 2 and out["error"]["code"] == "approval_unsigned"


# ============================================================================= M3: the money path checks the lock


def test_m3_a_money_request_is_refused_without_a_lock_for_a_bundle_with_locked_tiers(notes, home):
    togi("notes", "add", "--text", "tip me")
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "5.00")
    assert code == 2 and out["error"]["code"] == "not_locked" and "togi lock notes" in out["next"]
    assert togi("approvals")["data"] == []                                  # no request was even filed


def test_m3_require_lock_false_is_the_explicit_way_to_run_money_unlocked(home, make_ledger):
    togi("add", str(make_ledger("test_approvals = true")))                  # LEDGER_TOML says require_lock = false
    code, out = togi_code("ledger", "pay", "--to", "bob")
    assert code == 3 and out["approval"]["id"]


def test_m3_a_bundle_with_no_locked_tier_needs_no_lock_by_default(home, tmp_path):
    repo = tmp_path / "plain"
    (repo / "src" / "plain").mkdir(parents=True)
    (repo / "togi.toml").write_text('[bundle]\nname = "plain"\nmodule = "plain.togi_bundle:bundle"\n'
                                    '[tiers]\npolish = ["skills/**"]\n[policy]\napprove_effects = ["money"]\n')
    (repo / "src" / "plain" / "__init__.py").write_text("")
    (repo / "src" / "plain" / "togi_bundle.py").write_text(
        'from togi import Bundle\nbundle = Bundle("plain")\n\n@bundle.command("pay", effect="money", examples=["x"])\n'
        'def pay(to: str) -> dict:\n    """Pay."""\n    return {"to": to}\n')
    from togi.manifest import load_manifest

    assert load_manifest(repo).lock_required() is False
    togi("add", str(repo))
    assert togi_code("plain", "pay", "--to", "x")[0] == 3
    (repo / "togi.toml").write_text((repo / "togi.toml").read_text().replace("polish = [", 'locked = ["src/plain/**"]\npolish = ['))
    assert load_manifest(repo).lock_required() is True and togi_code("plain", "pay", "--to", "x")[1]["error"]["code"] == "not_locked"


def test_m3_editing_a_locked_file_blocks_money_at_request_time(pending, home, notes_repo):
    store = notes_repo / "src" / "notes" / "store.py"
    store.write_text(store.read_text() + "\n# edited after the lock\n")
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "7.00")
    assert code == 2 and out["error"]["code"] == "lock_check_failed" and "store.py" in out["error"]["message"]


def test_m3_and_at_use_time_before_the_approval_is_spent(pending, home, notes_repo):
    aid = approved_by_library()
    lock_path = notes_repo / ".togi" / "locked.sha256"
    saved = lock_path.read_text()
    lock_path.write_text("")                          # the lock file emptied by hand (the approval's binding is unchanged)
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "5.00", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "lock_check_failed" and tips_sent() == 0
    assert approvals.load(aid)["status"] == "approved"                    # refused BEFORE the approval was spent
    lock_path.write_text(saved)
    assert togi("notes", "tip", "send", "--id", "n_1", "--amount", "5.00", "--approval", aid)["ok"] is True


def test_m3_a_hand_edited_lock_file_with_a_forged_lock_entry_is_not_anchored(pending, home, notes_repo):
    """Edit a locked file, rewrite .togi/locked.sha256 to match, append a `lock` line saying so: without the audit key
    the entry has no valid mac, so the lock is not anchored and money stays refused (L2)."""
    store = notes_repo / "src" / "notes" / "store.py"
    store.write_text(store.read_text() + "\n# malicious\n")
    lp = notes_repo / ".togi" / "locked.sha256"
    lp.write_text("".join(f"{audit.file_sha256(notes_repo / rel)}  {rel}\n" for rel in sorted(
        l.split("  ", 1)[1] for l in lp.read_text().splitlines())))
    entries = audit.read(notes_repo)
    last = dict(entries[-1])
    last.update(seq=last["seq"] + 1, prev_hash=last["hash"], diff_sha256=audit.sha256_text(lp.read_text()))
    last.pop("mac"); last.pop("kid"); last.pop("hash")
    last["hash"] = audit.entry_hash(last)
    with open(audit.audit_path(notes_repo), "a") as f:
        f.write(audit.dump_line(last))
    ok, why = audit.lock_anchored(notes_repo, lp)
    assert ok is False and "not signed by this machine's audit key" in why
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "8.00")
    assert code == 2 and out["error"]["code"] == "lock_check_failed"


def test_l2_lock_anchored_checks_the_kid_and_mac_of_the_entry_it_relies_on(notes, home):
    lock_bundle("notes")
    lp = notes / ".togi" / "locked.sha256"
    assert audit.lock_anchored(notes, lp) == (True, None)
    p = audit.audit_path(notes)
    line = json.loads(p.read_text().splitlines()[-1])
    for mutate in (lambda e: e.update(mac="0" * 64), lambda e: e.update(kid="0" * 16),
                   lambda e: (e.pop("mac"), e.pop("kid")), lambda e: e.update(hash="1" * 64)):
        e = dict(line)
        mutate(e)
        p.write_text(audit.dump_line(e))
        assert audit.lock_anchored(notes, lp)[0] is False
    p.write_text(audit.dump_line(line))
    assert audit.lock_anchored(notes, lp) == (True, None)


def test_m3_the_prompt_shows_the_lock_state(pending, home):
    code, shown = approve_in_pty(home, pending)
    assert code == 0 and "lock: VERIFIED" in shown and "anchored in the signed audit log" in shown


def test_m3_approve_refuses_a_lock_that_fails_and_names_why(pending, home, notes_repo):
    (notes_repo / ".togi" / "locked.sha256").unlink()
    code, shown = approve_in_pty(home, pending)
    assert code == 2 and "not_locked" in shown
    assert approvals.load(pending)["status"] == "pending"


def test_m3_a_symlink_under_a_locked_pattern_is_reported_and_blocks_everything(notes, home, notes_repo, tmp_path):
    lock_bundle("notes")
    from togi.manifest import load_manifest

    man = load_manifest(notes_repo)
    outside = tmp_path / "evil.py"
    outside.write_text("print('x')\n")
    link = notes_repo / "tests" / "evil.py"
    link.symlink_to(outside)                                               # tests/** is locked; walk_files skipped links
    assert audit.locked_symlinks(notes_repo, man) == ["tests/evil.py"]
    from togi.manifest import locked_files

    assert "tests/evil.py" in locked_files(notes_repo, man)             # locked_files no longer skips links
    v = audit.verify_locked(notes_repo, man)
    assert v["ok"] is False and v["symlinks"] == ["tests/evil.py"]
    assert audit.lock_state(notes_repo, man)["state"] == "failed"
    # money is refused
    togi("notes", "add", "--text", "x")
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "1.00")
    assert code == 2 and out["error"]["code"] == "lock_check_failed" and "tests/evil.py" in out["error"]["message"]
    # a person cannot lock it in either, and apply refuses
    with pytest.raises(TogiError) as e:
        audit.write_lock(notes_repo, man)
    assert e.value.code == "locked_symlink"
    pid = hone_script(notes_repo, make_diff(notes_repo, {MSG: (notes_repo / MSG).read_text() + "# c\n"}), tmp_path)[1]["data"]["proposal"]
    code, out = togi_code("apply", pid, "--driver", "script")
    assert code == 2 and out["error"]["code"] == "locked_hash_mismatch" and "tests/evil.py" in out["error"]["message"]


def test_m3_a_symlinked_directory_that_hides_locked_files_is_reported(notes, home, notes_repo, tmp_path):
    from togi.manifest import load_manifest, locked_digest

    man = load_manifest(notes_repo)
    before = locked_digest(notes_repo, man)
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(notes_repo / "stone", elsewhere)
    shutil.rmtree(notes_repo / "stone")
    (notes_repo / "stone").symlink_to(elsewhere)                           # stone/** is locked
    assert "stone" in audit.locked_symlinks(notes_repo, man)
    assert locked_digest(notes_repo, man) != before                       # an approval bound to the old digest dies
    # a link that is nowhere near a locked pattern is none of the lock's business
    (notes_repo / "skills" / "see-also.md").symlink_to(notes_repo / "skills" / "money.md")
    assert audit.locked_symlinks(notes_repo, man) == ["stone"]


# ============================================================================= M4: the evaluation cache is signed


def test_m4_an_unsigned_or_edited_evaluation_cache_is_ignored_and_the_stone_runs_again(notes, home, tmp_path):
    (notes / "stone" / "tasks" / "hint.toml").write_text(
        'id = "hint"\ninstruction = "x"\nscript = ["togi notes show --id n_9"]\n[[check]]\ntranscript_contains = "Try: togi notes list"\n')
    lock_bundle("notes")
    new = (notes / MSG).read_text().replace('message = "No note with id {id}."',
                                            'message = "No note with id {id}. Try: togi notes list"')
    pid = hone_script(notes, make_diff(notes, {MSG: new}), tmp_path)[1]["data"]["proposal"]
    code, first = togi_code("apply", pid, "--driver", "script", "--dry-run")
    assert code == 0, first
    cache = home / "proposals" / pid / "evaluation.json"
    ev = json.loads(cache.read_text())
    assert isinstance(ev.get("mac"), str) and len(ev["mac"]) == 64
    honest = first["data"]["stone_after"]["nonholdout"]["successes"]
    assert honest >= 1
    # a flattering edit with the key kept, then a stripped signature: both are re-evaluated, neither is believed
    for tamper in (lambda e: e["after"]["nonholdout"].update(successes=999),
                   lambda e: e.pop("mac")):
        e = json.loads(cache.read_text())
        tamper(e)
        cache.write_text(json.dumps(e))
        assert apply_mod.read_evaluation(pid) is None
        code, again = togi_code("apply", pid, "--driver", "script", "--dry-run")
        assert code == 0 and again["data"]["stone_after"]["nonholdout"]["successes"] == honest
        assert apply_mod.read_evaluation(pid) is not None                  # rewritten, signed
    # the review does not present an unsigned cache as a result either
    e = json.loads(cache.read_text())
    e["after"]["nonholdout"]["successes"] = 999
    cache.write_text(json.dumps(e))
    assert togi("review", pid)["data"]["stone"] is None


def test_m4_a_cache_signed_for_another_proposal_is_not_accepted(notes, home, tmp_path):
    lock_bundle("notes")
    pid1 = hone_script(notes, make_diff(notes, {MSG: (notes / MSG).read_text() + "# a\n"}), tmp_path)[1]["data"]["proposal"]
    ev = {"key": "k", "before": {}, "after": {}}
    ev["mac"] = apply_mod.sign_evaluation(pid1, ev)
    assert apply_mod.evaluation_signed(pid1, ev) is True
    assert apply_mod.evaluation_signed("p_other", ev) is False


# ============================================================================= M5: output caps hold on the CLI

BIG_TOML = """[bundle]
name = "big"
version = "0.1.0"
summary = "output-size probe"
module = "big.togi_bundle:bundle"
"""

BIG_PY = '''from togi import Bundle

bundle = Bundle("big")


@bundle.command("wide", effect="read", examples=["togi big wide"])
def wide() -> dict:
    """A dict with many small keys."""
    return {f"k{i}": i for i in range(5000)}


@bundle.command("one", effect="read", examples=["togi big one"])
def one() -> list:
    """A one-item list holding one long string."""
    return ["x" * 100000]


@bundle.command("text", effect="read", examples=["togi big text"])
def text() -> str:
    """One long string."""
    return "y" * 100000


@bundle.command("nested", effect="read", examples=["togi big nested"])
def nested() -> dict:
    """A wide dict inside a dict, next to a small one."""
    return {"rows": {f"k{i}": {"a": i, "b": "zzz" * 10} for i in range(5000)}, "meta": {"k": 1}}


@bundle.command("noisy", effect="read", examples=["togi big noisy"])
def noisy() -> dict:
    """Prints a lot to stdout while running."""
    print("y" * 100000)
    return {"ok": 1}


@bundle.command("fail", effect="read", examples=["togi big fail"])
def fail() -> dict:
    """Raises a TogiError with a huge message."""
    from togi import TogiError
    raise TogiError("big_failure", "m" * 100000, next="n" * 100000)
'''


@pytest.fixture
def big(home, tmp_path):
    repo = tmp_path / "bigbundle"
    (repo / "src" / "big").mkdir(parents=True)
    (repo / "togi.toml").write_text(BIG_TOML)
    (repo / "src" / "big" / "__init__.py").write_text("")
    (repo / "src" / "big" / "togi_bundle.py").write_text(BIG_PY)
    togi("add", str(repo))
    return repo


CAP = 8192


@pytest.mark.parametrize("cmd", ["wide", "one", "text", "nested", "fail"])
def test_m5_cli_output_never_exceeds_the_cap_whatever_the_shape(big, home, cmd):
    r = run_togi(home, "big", cmd)
    assert len(r.stdout.encode()) <= CAP + 1, (cmd, len(r.stdout))
    env = json.loads(r.stdout)                                            # still one parseable envelope
    if cmd != "fail":
        assert env["ok"] is True and env["truncated"] is True and env.get("more")
    else:
        assert env["error"]["code"] == "big_failure" and env["truncated"] is True


def test_m5_a_wide_dict_is_cut_with_a_more_hint_and_keeps_its_first_keys(big, home):
    env = json.loads(run_togi(home, "big", "wide").stdout)
    assert list(env["data"])[:3] == ["k0", "k1", "k2"] and 100 < len(env["data"]) < 5000
    assert "--full" in env["more"] or "--fields" in env["more"]
    full = json.loads(run_togi(home, "big", "wide", "--full").stdout)
    assert len(full["data"]) == 5000 and "truncated" not in full           # --full still lifts the cap


def test_m5_the_cut_prefers_the_widest_container_not_the_small_neighbours(big, home):
    env = json.loads(run_togi(home, "big", "nested").stdout)
    assert env["data"]["meta"] == {"k": 1} and len(env["data"]["rows"]) < 5000


def test_m5_what_a_command_prints_does_not_reach_stdout_and_is_capped(big, home):
    r = run_togi(home, "big", "noisy")
    assert json.loads(r.stdout) == {"ok": True, "data": {"ok": 1}}         # the envelope, alone
    assert "printed 100001 characters" in r.stderr and len(r.stderr) < host.PRINT_CAP + 400
    entry = [json.loads(l) for l in (home / "state" / "big" / "journal.jsonl").read_text().splitlines()][-1]
    assert entry["printed_bytes"] == 100001


def test_m5_a_noisy_command_inside_togi_run_does_not_pollute_the_scripts_output(big, home):
    r = run_togi(home, "run", "-", input="print(big.noisy())")
    assert r.returncode == 0 and r.stdout.strip() == "{'ok': 1}", r.stdout[:200]
    assert len(r.stderr) < host.PRINT_CAP + 600


def test_m5_a_togi_run_exception_with_a_huge_message_is_capped_on_both_streams(notes, home):
    script = 'raise Exception("z" * 200000)'
    r = run_togi(home, "run", "-", input=script)
    assert r.returncode == 1 and len(r.stderr.encode()) <= 9000, len(r.stderr)
    assert len(r.stdout.encode()) <= 9000
    r = run_togi(home, "run", "-", "--json", input=script)
    assert len(r.stdout.encode()) <= CAP + 1 and json.loads(r.stdout)["ok"] is False


def test_m5_a_child_error_message_is_capped_before_the_host_keeps_it(notes, home):
    out = runmode.execute_script('raise Exception("z" * 200000)', use_sandbox=False)
    assert out.exit_code == 1 and len(out.fatal["message"]) <= runmode.MAX_MESSAGE + 40
    out = runmode.execute_script('import togi\nraise togi.TogiError("c" * 5000, "m" * 200000, next="n" * 200000)',
                                 use_sandbox=False)
    assert len(out.fatal["message"]) <= runmode.MAX_MESSAGE + 40 and len(out.fatal["next"]) <= runmode.MAX_MESSAGE + 40
    env = runmode._final_envelope(out, 60, 8192)
    assert len(output.dumps(env)) < 3500


def test_m5_a_request_line_longer_than_the_limit_stops_the_script(notes, home):
    out = runmode.execute_script('notes.add(text="x" * 2_000_000)\nprint("not reached")', use_sandbox=False)
    assert out.fatal and out.fatal["code"] == "protocol_error" and out.exit_code != 0
    assert "not reached" not in out.stdout
    assert togi("notes", "list")["data"] == []                              # the call was never made
    ok = runmode.execute_script('print(len(notes.add(text="x" * 100_000)["text"]))', use_sandbox=False)
    assert ok.exit_code == 0 and ok.stdout.strip() == "100000"             # an ordinary large call is fine


def test_m5_fit_any_always_ends_under_the_cap():
    shapes = [output.ok_envelope({f"k{i}": i for i in range(5000)}), output.ok_envelope(["x" * 100000]),
              output.ok_envelope("y" * 100000), output.ok_envelope([{f"k{i}": i for i in range(5000)}]),
              output.ok_envelope({"a": [{"b": "q" * 50000} for _ in range(3)]}),
              output.error_envelope("error", "c" * 50000, "m" * 100000, next="n" * 50000, approval={"x": "y" * 100000}),
              output.ok_envelope({"deep": [[[["z" * 40000] * 5] * 5] * 5]})]
    for env in shapes:
        out, cut = output.fit_any(env, output.MAX_BYTES, lambda off: "more")
        assert cut and output._size(out) <= output.MAX_BYTES and out["truncated"] is True and out["ok"] is env["ok"]
    assert output._size(output.hard_cap(shapes[5], 1000)) <= 1000


def test_m5_mcp_results_obey_the_cap_for_wide_dicts_too(big, home):
    from togi import mcp_bridge, registry

    env, err = mcp_bridge.call_tool(registry.load("big"), "big_wide", {})
    assert err is False and output._size(env) <= output.MAX_BYTES and env["truncated"] is True


# ============================================================================= L1: apply/revert need an authenticated log


def legacy_log(repo, home, n=3):
    """A log written before signatures existed: n unsigned entries and an old-format head."""
    prev, lines = "0" * 64, []
    for i in range(1, n + 1):
        e = {"seq": i, "ts": "2026-10-26T20:00:00.000Z", "action": "lock", "proposal": None, "tier": "locked",
             "files": [], "diff_sha256": None, "stone_before": None, "stone_after": None, "decided_by": "human:x",
             "reason": f"legacy {i}", "prev_hash": prev}
        e["hash"] = audit.entry_hash(e)
        prev = e["hash"]
        lines.append(json.dumps(e, sort_keys=True, separators=(",", ":")))
    p = audit.audit_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n")
    hp = home / "state" / "notes" / "audit.head"
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps({"seq": n, "hash": prev}))


def test_l1_an_all_unsigned_legacy_log_is_not_authenticated_and_cannot_be_appended_to_without_a_person(notes, home):
    legacy_log(notes, home)
    v = audit.verify(notes, "notes")
    assert v["ok"] is True and v["authenticated"] is False and v["unauthenticated_entries"] == 3
    with pytest.raises(TogiError) as e:
        audit.append(notes, "notes", action="reject", proposal="p_1", tier="polish", files=[], diff_sha256=None,
                     stone_before=None, stone_after=None, decided_by="policy", reason="x")
    assert e.value.code == "audit_unauthenticated" and e.value.status == "refused"
    assert len(audit.read(notes)) == 3
    with pytest.raises(TogiError) as e:                                    # what apply and revert call
        audit.require_trusted_chain(notes, "notes")
    assert e.value.code == "audit_unauthenticated"
    # a person's lock (anchor=True) may sign onto it; from then on the log is authenticated, legacy prefix and all
    audit.append(notes, "notes", action="lock", proposal=None, tier="locked", files=[], diff_sha256=None,
                 stone_before=None, stone_after=None, decided_by="human:x", reason="re-anchor", anchor=True)
    v = audit.verify(notes, "notes")
    assert v["ok"] and v["authenticated"] is True and v["unauthenticated_entries"] == 3
    audit.require_trusted_chain(notes, "notes")


def test_l1_apply_refuses_an_unauthenticated_log_before_anything_else(notes, home, tmp_path):
    pid = hone_script(notes, make_diff(notes, {MSG: (notes / MSG).read_text() + "# c\n"}), tmp_path)[1]["data"]["proposal"]
    legacy_log(notes, home)
    code, out = togi_code("apply", pid, "--driver", "script", "--dry-run")
    assert code != 0 and out["error"]["code"] == "audit_unauthenticated", out
    assert "togi lock notes" in out["next"]


def test_l1_a_log_with_a_stripped_signature_after_a_signed_entry_is_broken(notes, home):
    lock_bundle("notes")
    lock_bundle("notes")
    p = audit.audit_path(notes)
    lines = p.read_text().splitlines()
    e = json.loads(lines[-1])
    e.pop("mac"), e.pop("kid")
    e["hash"] = audit.entry_hash(e)
    lines[-1] = json.dumps(e, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(lines) + "\n")
    with pytest.raises(TogiError) as e:
        audit.require_trusted_chain(notes, "notes")
    assert e.value.code == "audit_chain_broken"


# ============================================================================= L3: the docs say how the lock is anchored


def test_l3_readme_and_spec_describe_lock_anchoring():
    readme, spec = (ROOT / "README.md").read_text(), (ROOT / "SPEC.md").read_text()
    for text in (readme, spec):
        assert "anchored" in text and "latest" in text and "`lock` entry" in text
        assert "require_lock" in text and "test_approvals" in text
    # the stale limit ("nothing yet compares the file with the `lock` entry") is gone: lock_anchored does exactly that
    assert "nothing yet compares" not in readme and "next hardening is to check the file" not in readme
    assert "## 6." in spec and "anchored" in spec[spec.index("## 6."):]


# ============================================================================= L4: polish may only touch plain text


@pytest.mark.parametrize("rel,code", [
    ("skills/run.cfg", True), ("skills/Makefile", True), ("skills/page.html", True), ("skills/tool.rb", True),
    ("skills/notes.pth", True), ("skills/helper.pl", True), ("skills/noext", True), ("skills/x.PY", True),
    ("skills/run.ps1", True), ("skills/pyproject.toml", True), ("skills/togi.toml", True), ("skills/nested/PyProject.TOML", True),
    ("skills/a.md", False), ("skills/a.TXT", False), ("skills/a.json", False), ("skills/a.yaml", False),
    ("skills/a.YML", False), ("togi_messages.toml", False), ("skills/other.toml", False)])
def test_l4_only_allowlisted_text_types_are_not_code(rel, code):
    assert apply_mod.is_code_path(rel) is code


def test_l4_a_polish_diff_adding_a_non_text_file_is_rejected_at_hone_and_audited(notes, home, tmp_path):
    lock_bundle("notes")
    for name in ("skills/run.cfg", "skills/Makefile", "skills/conftest.pth"):
        diff = make_diff(notes, {name: "x = 1\n"})
        code, out = hone_script(notes, diff, tmp_path)
        assert code == 2 and out["error"]["code"] == "code_in_polish", (name, out)
    assert [e["action"] for e in audit.read(notes)].count("reject") == 3
    code, out = hone_script(notes, make_diff(notes, {"skills/extra.md": "# more\n", "skills/data.json": "{}\n"}), tmp_path)
    assert code == 0 and out["data"]["tier"] == "polish"


def test_l4_a_shebang_still_makes_a_text_file_code(notes, home, tmp_path):
    lock_bundle("notes")
    code, out = hone_script(notes, make_diff(notes, {"skills/readme.md": "#!/bin/sh\necho hi\n"}), tmp_path)
    assert code == 2 and out["error"]["code"] == "code_in_polish"


# ============================================================================= L5: journal hygiene


def journal_lines(home, bundle):
    return [json.loads(l) for l in (home / "state" / bundle / "journal.jsonl").read_text().splitlines()]


def test_l5_a_journal_note_from_a_write_command_records_keys_and_types_only(home, make_ledger):
    togi("add", str(make_ledger()))
    togi("ledger", "log")
    togi("ledger", "peek")
    notes = [e for e in journal_lines(home, "ledger") if e["event"] == "note"]
    by_cmd = {e["command"]: e["note"] for e in notes}
    assert by_cmd["log"] == {"amount": "str", "who": "str", "n": "int", "tags": "list", "flag": "bool"}
    assert "5.00" not in json.dumps(by_cmd["log"]) and "bob" not in json.dumps(by_cmd["log"])
    assert by_cmd["peek"] == {"seen": "yes"}                               # a read command's note is stored as written


def test_l5_the_notes_example_note_is_shaped_too(notes, home):
    togi("notes", "add", "--text", "buy milk")
    note = [e for e in journal_lines(home, "notes") if e["event"] == "note"][-1]["note"]
    assert note == {"added": "bool"}


def test_l5_unknown_flag_names_are_hashed_unless_they_are_known_to_the_host_or_the_bundle(notes, home):
    togi_code("notes", "add", "--text", "x", "--sk-live-4f9a8b7c6d5e3f2a", "v")
    togi_code("notes", "add", "--text", "x", "--tag", "ok", "--wat")
    togi_code("--json", "approve", "--ap-secret-token-1234")
    raw = (home / "state" / "notes" / "journal.jsonl").read_text() + (home / "state" / "_host" / "journal.jsonl").read_text()
    for secret in ("sk-live", "4f9a8b7c", "wat", "ap-secret", "token-1234"):
        assert secret not in raw, secret
    ents = [e for e in journal_lines(home, "notes") if e["event"] == "unknown_flag"]
    assert [e["attempted"] for e in ents] == ["--" + journal.flag_name_shape(n) for n in ("sk-live-4f9a8b7c6d5e3f2a", "wat")]
    host_attempt = [e for e in journal_lines(home, "_host") if e.get("event") == "host_attempt"][-1]
    assert host_attempt["argv"] == ["--" + journal.flag_name_shape("ap-secret-token-1234")]


def test_l5_every_flag_a_host_command_parses_is_known_to_the_journal():
    """HOST_FLAGS must not drift from the flags the host commands actually accept."""
    import re

    declared = set()
    for f in (SRC / "togi").glob("*.py"):
        for m in re.finditer(r'parse_flags\(\s*argv,\s*\{([^}]*)\}', f.read_text()):
            declared |= set(re.findall(r'"([a-z-]+)":\s*"(?:bool|str|int|float|list)"', m.group(1)))
    declared |= set(__import__("togi.cli", fromlist=["COMMON"]).COMMON)
    assert declared and declared <= journal.HOST_FLAGS, declared - journal.HOST_FLAGS


# ============================================================================= run mode: site-packages


@pytest.fixture
def fake_site(tmp_path):
    """A pretend site-packages: an installed bundle module (package, dist-info, editable finder) and a bystander."""
    site = tmp_path / "fake-site-packages"
    (site / "fakebundle").mkdir(parents=True)
    (site / "fakebundle" / "__init__.py").write_text("SECRET_ENGINE = 'settle()'\n")
    (site / "fakebundle-0.1.dist-info").mkdir()
    (site / "fakebundle-0.1.dist-info" / "METADATA").write_text("Name: fakebundle\n")
    (site / "__editable___fakebundle_0_1_finder.py").write_text("# editable finder\n")
    (site / "fakebundle.pth").write_text("/somewhere\n")
    (site / "bystander").mkdir()
    (site / "bystander" / "__init__.py").write_text("VALUE = 42\n")
    (site / "fakebundle_extras").mkdir()                                  # a different package that merely shares a prefix
    (site / "fakebundle_extras" / "__init__.py").write_text("")
    return site


def test_site_installed_bundle_modules_are_found_by_listing_the_site_directory(fake_site):
    found = [Path(p).name for p in sandbox.installed_module_paths(["fakebundle"], [str(fake_site)])]
    assert found == sorted(["__editable___fakebundle_0_1_finder.py", "fakebundle", "fakebundle-0.1.dist-info",
                            "fakebundle.pth"])


def test_the_profile_denies_installed_bundle_modules_last_and_leaves_bystanders_alone(fake_site, tmp_path):
    prof = sandbox.build_profile(tmp=str(tmp_path / "t"), repos=["/repo1"], togi_home=str(tmp_path / "th"),
                                 home=str(tmp_path / "h"), strict=True, modules=["fakebundle"], site=[str(fake_site)])
    site = os.path.realpath(fake_site)
    last_deny = prof.strip().splitlines()[-1]
    # `file-read*`, not `file*`: SBPL lets the specific allow of the Python install outrank a generic `file*` deny
    assert last_deny.startswith("(deny file-read* file-write*") and f'(subpath "{site}/fakebundle")' in last_deny
    assert f'(subpath "{site}/fakebundle-0.1.dist-info")' in last_deny and f'"{site}/__editable___fakebundle_0_1_finder.py"' in last_deny
    assert "bystander" not in last_deny and "fakebundle_extras" not in last_deny
    plain = sandbox.build_profile(tmp=str(tmp_path / "t"), repos=["/repo1"], togi_home=str(tmp_path / "th"),
                                  home=str(tmp_path / "h"), strict=True)
    assert plain.strip().splitlines()[-1].startswith("(deny file* ")      # no bundle modules: the profile is as before


@pytest.mark.skipif(not sandbox.available()[0], reason="sandbox-exec is not available here")
@pytest.mark.parametrize("strict", [True, False])
def test_a_script_under_the_real_sandbox_cannot_import_a_bundle_installed_in_site_packages(fake_site, tmp_path, strict):
    t = tmp_path / "child_tmp"
    t.mkdir()
    prof = sandbox.build_profile(tmp=str(t), repos=[], togi_home=str(tmp_path / "th"), home=str(tmp_path / "h"),
                                 strict=strict, modules=["fakebundle"], site=[str(fake_site)])
    code = (f"import sys; sys.path.insert(0, {str(fake_site)!r})\n"
            "import bystander; print('bystander', bystander.VALUE)\n"
            "try:\n    import fakebundle; print('IMPORTED', fakebundle.SECRET_ENGINE)\n"
            "except Exception as e:\n    print('blocked', type(e).__name__)\n")
    p = subprocess.run([sandbox.SANDBOX_EXEC, "-p", prof, sys.executable, "-I", "-B", "-c", code], capture_output=True,
                       text=True, cwd=t, env={"PATH": "/usr/bin:/bin", "TMPDIR": str(t), "HOME": str(t)}, timeout=60)
    if "bystander 42" not in p.stdout and strict:
        pytest.skip("the strict profile cannot start Python from here: " + p.stderr[-200:])
    assert "bystander 42" in p.stdout and "blocked" in p.stdout and "IMPORTED" not in p.stdout, (p.stdout, p.stderr[-300:])


def test_togi_run_hands_the_registered_bundles_module_names_to_the_sandbox(notes, home, monkeypatch):
    assert runmode.bundle_modules() == ["notes"]
    seen = {}
    real = sandbox.prepare

    def spy(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setattr(sandbox, "prepare", spy)
    out = runmode.execute_script("print('hi')")
    assert out.stdout.strip() == "hi" and seen["modules"] == ["notes"]
