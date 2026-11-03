"""The approval desk (desk.py): a person approves on a page served by a program they started. It must be no weaker than
`togi approve`: only a person's process opens it, the person sees what is signed and the grant binds to it, with the same
signing, TTL and attempt limits, and nothing an agent can reach approves through it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from togi import approvals, approver, desk, journal, paths, spent

from conftest import PASSPHRASE, SRC, clean_env, default_approver, lock_bundle, togi, togi_code

TIP = ["notes", "tip", "send", "--id", "n_1", "--amount", "5.00"]
ONLY_TIPS = [("notes", "tip send")]


@pytest.fixture
def tipped(notes):
    lock_bundle("notes")
    togi("notes", "add", "--text", "tip me")
    return notes


@pytest.fixture
def clean(monkeypatch):
    """pytest's own interpreter is neither isolated nor clean (PYTHONPATH, the current directory on sys.path); the real
    check runs in the subprocess tests below. Here it is taken as passed, and every other check is real."""
    monkeypatch.setattr(approvals, "interpreter_problem", lambda: None)


@pytest.fixture
def opened(tipped, clean):
    return desk.Desk.open("notes test", ONLY_TIPS)


def request_approval() -> str:
    code, out = togi_code(*TIP)
    assert code == 3, out
    return out["approval"]["id"]


def desk_events(home: Path) -> list[dict]:
    return [e for e in journal.read("_host") if e.get("surface") == "desk"]


def files_holding(root: Path, needle: bytes) -> list[str]:
    hits = []
    for f in root.rglob("*"):
        try:
            if f.is_file() and needle in f.read_bytes():
                hits.append(str(f))
        except OSError:
            continue
    return hits


# ---- who may open a desk ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("var,value", [("TOGI_AGENT", "maker-demo"), ("TOGI_AGENT", ""), ("TOGI_RUN", "1"), ("TOGI_RUN", "")])
def test_an_agents_process_cannot_open_a_desk(tipped, clean, monkeypatch, var, value):
    monkeypatch.setenv(var, value)
    with pytest.raises(approvals.TogiError) as e:
        desk.Desk.open("studio", ONLY_TIPS)
    assert e.value.code == "human_only" and e.value.status == "refused"
    assert any(x["command"] == "desk open" and x["ok"] is False for x in desk_events(tipped))


def test_a_process_under_togi_run_cannot_open_a_desk(tipped, clean, monkeypatch):
    from togi import runreg

    monkeypatch.setattr(runreg, "started_by_togi_run", lambda max_depth=40: True)
    with pytest.raises(approvals.TogiError) as e:
        desk.Desk.open("studio", ONLY_TIPS)
    assert e.value.code == "human_only"


def test_an_unclean_interpreter_cannot_open_a_desk(tipped, monkeypatch):
    monkeypatch.setattr(approvals, "interpreter_problem", lambda: "PYTHONPATH is set, and this interpreter is not isolated")
    with pytest.raises(approvals.TogiError) as e:
        desk.Desk.open("studio", ONLY_TIPS)
    assert e.value.code == "unsafe_interpreter"


def test_no_approver_no_desk(tipped, clean, home):
    for f in paths.approver_home().iterdir():
        f.unlink()
    with pytest.raises(approvals.TogiError) as e:
        desk.Desk.open("studio", ONLY_TIPS)
    assert e.value.code == "no_approver"


def test_a_desk_must_name_its_commands_and_cannot_be_made_without_its_checks(tipped, clean):
    with pytest.raises(approvals.TogiError):
        desk.Desk.open("studio", [])
    with pytest.raises(approvals.TogiError):
        desk.Desk("studio", frozenset(ONLY_TIPS), "test")


def test_the_persons_own_approver_needs_a_terminal_to_open_a_desk(tipped, clean, monkeypatch):
    default_approver()
    monkeypatch.delenv("TOGI_APPROVER_HOME")
    assert paths.approver_scope() == "default"
    with pytest.raises(approvals.TogiError) as e:          # pytest's stdin and stdout are not terminals
        desk.Desk.open("studio", ONLY_TIPS)
    assert e.value.code == "needs_tty"
    monkeypatch.setattr(approvals, "_isatty", lambda f: True)
    d = desk.Desk.open("studio", ONLY_TIPS)
    assert d.status()["scope"] == "default"


@pytest.mark.parametrize("how", ["symlink", "dotdot", "slash", "linked_files", "legacy"])
def test_a_test_approver_that_is_the_persons_own_needs_a_terminal_too(tipped, clean, monkeypatch, tmp_path, how):
    """Review (desk refuter, P2): TOGI_APPROVER_HOME naming <pw_dir>/.togi/approver.d by another path is scope "test" by name,
    yet it is the person's own approver; it opened a desk with no terminal. Now it needs one, as the person's own does."""
    fake = default_approver()                                           # the person's own approver, in the test's fake pw dir
    own = paths.fixed_approver_home()
    assert str(own).startswith(str(fake)) and (own / "approver").is_file()
    if how == "symlink":
        alias = tmp_path / "looks-like-a-test-approver"
        alias.symlink_to(own, target_is_directory=True)
    elif how == "dotdot":
        alias = own / ".." / "approver.d"
    elif how == "slash":
        alias = Path(str(own) + "/")
    elif how == "linked_files":
        alias = tmp_path / "linked"
        alias.mkdir()
        for n in ("approver", "approver.key"):
            if (own / n).exists():
                (alias / n).symlink_to(own / n)
    else:
        alias = own.parent                                               # where r4 kept the files
    monkeypatch.setenv("TOGI_APPROVER_HOME", str(alias))
    assert paths.approver_scope() == "test" and paths.override_is_own_approver() is True
    with pytest.raises(approvals.TogiError) as e:                       # pytest's stdin and stdout are not terminals
        desk.Desk.open("studio", ONLY_TIPS)
    assert e.value.code == "needs_tty" and "names your own approver" in e.value.message
    monkeypatch.setattr(approvals, "_isatty", lambda f: True)           # from a terminal, as for the person's own
    if how == "legacy":                                                  # (r5 keeps no approver there: nothing to open with)
        with pytest.raises(approvals.TogiError) as e:
            desk.Desk.open("studio", ONLY_TIPS)
        assert e.value.code == "no_approver"
    else:
        assert desk.Desk.open("studio", ONLY_TIPS).status()["open"] is True


def test_a_real_throwaway_approver_is_not_the_persons_own(opened):
    assert paths.approver_scope() == "test" and paths.override_is_own_approver() is False


def test_a_test_scope_desk_opens_without_a_terminal(opened):
    st = opened.status()
    assert st == {"open": True, "host": "notes test", "scope": "test", "commands": ["notes tip send"],
                  "review_seconds": desk.REVIEW_TTL_S}


def isolated_script(home: Path, body: str, **env_extra) -> subprocess.CompletedProcess:
    """Run `body` as a program a person might start: `python3 -I`, Togi's src put on sys.path by the script itself."""
    script = f"import sys, json\nsys.path.insert(0, {str(SRC)!r})\n" + body
    env = clean_env(home, **env_extra)
    for k in [k for k, v in env_extra.items() if v is None]:
        env.pop(k, None)
    return subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, env=env, timeout=60)


OPEN = ("from togi import desk\n"
        "d, why = desk.open_or_reason('studio', [('notes', 'tip send')])\n"
        "print(json.dumps(d.status() if d else why))\n")


def test_a_real_isolated_program_opens_a_desk_and_the_same_program_as_an_agent_does_not(tipped, home):
    r = isolated_script(home, OPEN)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["open"] is True
    r = isolated_script(home, OPEN, TOGI_AGENT="maker-demo")
    assert json.loads(r.stdout)["code"] == "human_only"


def test_a_real_program_with_pythonpath_and_no_isolation_does_not_open_a_desk(tipped, home):
    env = clean_env(home)               # clean_env sets PYTHONPATH
    script = f"import sys, json\nsys.path.insert(0, {str(SRC)!r})\n" + OPEN
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
    assert json.loads(r.stdout)["code"] == "unsafe_interpreter", r.stdout + r.stderr


# ---- the person sees what is signed --------------------------------------------------------------------------------

def test_the_review_is_what_togi_approve_would_show(opened):
    aid = request_approval()
    rv = opened.review(aid)
    rec = approvals.load(aid)
    verified = approvals.verify_pending(aid, rec)
    assert rv["args"] == {"id": "n_1", "amount": "5.00"} == verified["args"]
    assert rv["summary"] == verified["summary"] and rv["confirm_word"] == "n_1" == verified["confirm_word"]
    assert rv["lines"] == approvals.prompt_lines_for(rec, verified, rv["expires_if_approved"])
    assert "VERIFIED" in rv["lock"] and rv["scope"] == "test" and rv["review"].startswith("rv_")
    left = approvals.parse_ts(rv["expires_if_approved"]) - time.time()
    assert approvals.ttl_seconds() - 5 < left <= approvals.ttl_seconds()
    assert approvals.load(aid)["status"] == "pending"           # looking changes nothing


def test_approving_signs_exactly_the_review_and_the_grant_is_single_use(opened, home):
    aid = request_approval()
    rv = opened.review(aid)
    out = opened.approve(aid, rv["review"], PASSPHRASE, word=rv["confirm_word"])
    assert out["status"] == "approved" and out["scope"] == "test"
    assert out["expires"] == rv["expires_if_approved"]                       # the expiry shown is the expiry signed
    rec = approvals.load(aid)
    assert rec["decided_by"].startswith("human:") and "approval desk: notes test" in rec["decided_by"]
    approvals.verify_grant(rec)                                              # signed by the approver like a terminal grant
    assert togi(*TIP, "--approval", aid)["ok"] is True
    code, again = togi_code(*TIP, "--approval", aid)
    assert code == 2 and again["error"]["code"] == "approval_used"
    assert spent.is_spent(aid)
    with pytest.raises(approvals.TogiError) as e:                            # a review is used once
        opened.approve(aid, rv["review"], PASSPHRASE)
    assert e.value.code == "review_unknown"
    ev = [(x["command"], x["ok"]) for x in desk_events(home)]
    assert ("desk open", True) in ev and ("desk review", True) in ev and ("approve", True) in ev
    attempts = [x for x in journal.read("_host") if x.get("event") == "passphrase_attempt"]
    assert attempts[-1]["surface"] == "desk" and attempts[-1]["ok"] is True


def test_the_passphrase_is_never_written_anywhere(opened, home, tmp_path):
    aid = request_approval()
    rv = opened.review(aid)
    with pytest.raises(approvals.TogiError):
        opened.approve(aid, rv["review"], "a wrong guess that is long enough")
    opened.approve(aid, rv["review"], PASSPHRASE)
    for secret in (PASSPHRASE, "a wrong guess that is long enough"):
        assert files_holding(tmp_path, secret.encode()) == []


# ---- refusals --------------------------------------------------------------------------------------------------------

def test_a_wrong_passphrase_grants_nothing_counts_and_the_review_can_be_retried(opened, home):
    aid = request_approval()
    rv = opened.review(aid)
    with pytest.raises(approvals.TogiError) as e:
        opened.approve(aid, rv["review"], "not the passphrase at all")
    assert e.value.code == "wrong_passphrase" and approvals.load(aid)["status"] == "pending"
    assert approver.status()["wrong_attempts"] == 1
    bad = [x for x in journal.read("_host") if x.get("event") == "passphrase_attempt"][-1]
    assert bad["surface"] == "desk" and bad["ok"] is False and bad["error_code"] == "wrong_passphrase"
    assert opened.approve(aid, rv["review"], PASSPHRASE)["status"] == "approved"
    assert approver.status()["wrong_attempts"] == 0


def test_five_wrong_passphrases_lock_the_approver_for_the_terminal_too(opened, home):
    aid = request_approval()
    rv = opened.review(aid)
    codes = []
    for _ in range(approver.FAIL_LIMIT):
        with pytest.raises(approvals.TogiError) as e:
            opened.approve(aid, rv["review"], "not the passphrase at all")
        codes.append(e.value.code)
    assert codes[-1] == "approver_locked" and approver.lock_remaining() > 0     # the same lock `togi approve` checks
    with pytest.raises(approvals.TogiError) as e:
        opened.approve(aid, rv["review"], PASSPHRASE)
    assert e.value.code == "approver_locked" and approvals.load(aid)["status"] == "pending"


def test_an_empty_or_huge_passphrase_is_refused_before_any_check(opened):
    aid = request_approval()
    rv = opened.review(aid)
    for bad in ("", None, 12345, "x" * (desk.MAX_PASSPHRASE + 1)):
        with pytest.raises(approvals.TogiError) as e:
            opened.approve(aid, rv["review"], bad)
        assert e.value.code == "passphrase_missing"
    assert approver.status()["wrong_attempts"] == 0


def test_an_unencodable_passphrase_is_a_wrong_one_counted_and_journaled(opened, home):
    """Review (desk refuter, P2): a lone surrogate (a page can send one in JSON) made scrypt's encode raise: a 500, no journal
    line, no attempt counted. Now it is a wrong passphrase like any other."""
    aid = request_approval()
    rv = opened.review(aid)
    before = len([x for x in journal.read("_host") if x.get("event") == "passphrase_attempt"])
    for bad in ("\ud800", "pass\udfffword", "\udc80" * 20):
        with pytest.raises(approvals.TogiError) as e:
            opened.approve(aid, rv["review"], bad)
        assert e.value.code in ("wrong_passphrase", "approver_locked")
    assert approver.status()["wrong_attempts"] == 3 and approvals.load(aid)["status"] == "pending"
    tries = [x for x in journal.read("_host") if x.get("event") == "passphrase_attempt"][before:]
    assert [(t["surface"], t["ok"], t["error_code"]) for t in tries] == [("desk", False, "wrong_passphrase")] * 3
    assert opened.approve(aid, rv["review"], PASSPHRASE)["status"] == "approved"


def test_the_terminal_reader_too_counts_an_unencodable_passphrase(home):
    from conftest import default_approver as _d  # noqa: F401 - (the test approver of `home` is the one checked)
    approver.create(PASSPHRASE, n=2 ** 10, replace=True) if approver.load() is None else None
    with pytest.raises(approvals.TogiError) as e:
        approver.require("approve", reader=lambda _p: "\udfff")
    assert e.value.code == "wrong_passphrase" and approver.status()["wrong_attempts"] == 1


def test_a_request_changed_after_review_is_refused(opened):
    aid = request_approval()
    rv = opened.review(aid)
    f = paths.approvals_dir() / f"{aid}.json"
    rec = json.loads(f.read_text())
    rec["args"]["amount"] = "500.00"                       # the requester edits what was asked for
    f.write_text(json.dumps(rec))
    with pytest.raises(approvals.TogiError) as e:
        opened.approve(aid, rv["review"], PASSPHRASE)
    assert e.value.code in ("approval_tampered", "approval_changed")
    assert json.loads(f.read_text())["status"] == "pending"
    with pytest.raises(approvals.TogiError) as e:          # and that review is gone
        opened.approve(aid, rv["review"], PASSPHRASE)
    assert e.value.code == "review_unknown"


def test_a_bundle_changed_after_review_is_refused(opened, notes_repo):
    aid = request_approval()
    rv = opened.review(aid)
    man = notes_repo / "togi.toml"
    man.write_text(man.read_text().replace('version = "', 'version = "9', 1))
    with pytest.raises(approvals.TogiError) as e:
        opened.approve(aid, rv["review"], PASSPHRASE)
    assert e.value.code in ("approval_stale", "approval_changed", "lock_check_failed")
    assert approvals.load(aid)["status"] == "pending"


def test_a_review_of_one_request_cannot_approve_another(opened):
    a = request_approval()
    rv = opened.review(a)
    togi("notes", "add", "--text", "another")
    code, out = togi_code("notes", "tip", "send", "--id", "n_2", "--amount", "7.00")
    b = out["approval"]["id"]
    with pytest.raises(approvals.TogiError) as e:
        opened.approve(b, rv["review"], PASSPHRASE)
    assert e.value.code == "approval_mismatch"
    assert approvals.load(b)["status"] == "pending" and approvals.load(a)["status"] == "pending"


def test_a_wrong_confirmation_word_is_refused(opened):
    aid = request_approval()
    rv = opened.review(aid)
    with pytest.raises(approvals.TogiError) as e:
        opened.approve(aid, rv["review"], PASSPHRASE, word="n_2")
    assert e.value.code == "confirmation_mismatch" and approvals.load(aid)["status"] == "pending"


def test_a_lapsed_review_is_refused(opened, monkeypatch):
    aid = request_approval()
    rv = opened.review(aid)
    real = time.monotonic
    monkeypatch.setattr(desk.time, "monotonic", lambda: real() + desk.REVIEW_TTL_S + 1)
    with pytest.raises(approvals.TogiError) as e:
        opened.approve(aid, rv["review"], PASSPHRASE)
    assert e.value.code == "review_expired" and approvals.load(aid)["status"] == "pending"


def test_an_expired_request_cannot_be_reviewed_or_approved(opened, monkeypatch):
    aid = request_approval()
    rv = opened.review(aid)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + approvals.ttl_seconds() + 5)    # the request's own 15 minutes pass
    with pytest.raises(approvals.TogiError) as e:
        opened.approve(aid, rv["review"], PASSPHRASE)
    assert e.value.code == "approval_expired"
    with pytest.raises(approvals.TogiError) as e:
        opened.review(aid)
    assert e.value.code == "approval_expired"
    monkeypatch.setattr(time, "time", real)
    assert approvals.load(aid)["status"] == "pending"


def test_a_denied_or_used_request_cannot_be_reviewed(opened):
    aid = request_approval()
    rv = opened.review(aid)
    opened.approve(aid, rv["review"], PASSPHRASE)
    with pytest.raises(approvals.TogiError) as e:
        opened.review(aid)
    assert e.value.code == "approval_not_pending"


def test_a_desk_approves_only_the_commands_it_names(tipped, clean):
    other = desk.Desk.open("studio", [("crowd", "drop settle")])
    aid = request_approval()
    with pytest.raises(approvals.TogiError) as e:
        other.review(aid)
    assert e.value.code == "desk_not_for_this"


def test_a_process_marked_as_an_agent_after_opening_has_no_desk(opened, monkeypatch):
    aid = request_approval()
    rv = opened.review(aid)
    monkeypatch.setenv("TOGI_AGENT", "studio")
    with pytest.raises(approvals.TogiError) as e:
        opened.approve(aid, rv["review"], PASSPHRASE)
    assert e.value.code == "human_only" and approvals.load(aid)["status"] == "pending"
    with pytest.raises(approvals.TogiError):
        opened.review(aid)


def test_switching_approver_after_opening_closes_the_desk(opened, monkeypatch, tmp_path):
    aid = request_approval()
    rv = opened.review(aid)
    monkeypatch.delenv("TOGI_APPROVER_HOME")
    with pytest.raises(approvals.TogiError) as e:
        opened.approve(aid, rv["review"], PASSPHRASE)
    assert e.value.code == "desk_scope_changed"


def test_unknown_or_malformed_ids_are_refused(opened):
    for aid in ("ap_nope", "../etc/passwd", "ap_1; rm"):
        with pytest.raises(approvals.TogiError) as e:
            opened.review(aid)
        assert e.value.code == "approval_not_found"
    aid = request_approval()
    for rid in (None, "", "rv_made_up", 7):
        with pytest.raises(approvals.TogiError) as e:
            opened.approve(aid, rid, PASSPHRASE)
        assert e.value.code == "review_unknown"


def test_the_terminal_gate_is_unchanged_by_the_desk(tipped, home):
    """`togi approve` still needs a terminal and refuses agents; the desk adds no CLI command and no flag."""
    from conftest import run_togi

    aid = request_approval()
    r = run_togi(home, "approve", aid, input="n_1\n")
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "needs_tty"
    from togi import cli

    assert "desk" not in cli.HOST_COMMANDS and not any("desk" in c for c in cli._HANDLERS)
