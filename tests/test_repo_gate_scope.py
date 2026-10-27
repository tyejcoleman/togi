"""Fix round 3: the human gates that change a repo accept only the DEFAULT-scope approver.

The hole: `togi lock`, `togi revert`, the approval to evaluate a reshape proposal and a human `togi apply` accepted any
approver that could be set up, including a throwaway one made under TOGI_APPROVER_HOME (scope "test"). An agent that can
write a repo could therefore edit a locked money file, create its own test approver over a pseudo-terminal, and
`togi lock` the modified code, defeating the locked tier. Only money approvals were scope-checked, and only by the
bundle.

Now those four gates refuse while TOGI_APPROVER_HOME is set (`test_scope_refused`, exit 2), before any prompt, and they
write the approver scope into the audit entry. `lock`/`revert`/`apply` through `run_in_pty` / `run_togi` use the
default-scope approver of the faked password database by themselves (conftest `scope="auto"`); the tests that need the
agent's view say `scope="test"`.

Nothing here touches the real approver: the fixed location is the conftest fake pw dir, and the autouse guard fails any
test that changes an approver file in the real home.
"""
from __future__ import annotations

import json

import pytest

from togi import apply as apply_mod
from togi import approvals, audit, paths
from togi.manifest import load_manifest

import conftest
from conftest import (answers, approver_files, hone_script, human, human_twice, lock_bundle, make_diff,
                      run_in_pty, run_togi, togi, togi_code)

ATTACKER = "the attacker's own passphrase"
MSG = "togi_messages.toml"
STORE = "src/notes/store.py"          # a locked file
HINT_TASK = '''
id = "hint"
instruction = "Show note n_9 and tell me what to try."
script = ["togi notes show --id n_9"]
[[check]]
transcript_contains = "Try: togi notes list"
'''
REFUSED = "test_scope_refused"


def propose(repo, tmp_path, changes):
    code, out = hone_script(repo, make_diff(repo, changes), tmp_path)
    assert code == 0, out
    return out["data"]["proposal"]


def apply_(pid, *flags):
    return togi_code("apply", pid, "--driver", "script", *flags)


def meta_of(home, pid):
    return json.loads((home / "proposals" / pid / "meta.json").read_text())


def good_message(repo):
    return (repo / MSG).read_text().replace('message = "No note with id {id}."',
                                            'message = "No note with id {id}. Try: togi notes list"')


def default_files(fake_pw_dir):
    return approver_files(fake_pw_dir / ".togi")


def nothing_asked(shown: str) -> bool:
    """The refusal comes before the typed word and before the passphrase prompt."""
    return 'Type "' not in shown and "Approver passphrase" not in shown


@pytest.fixture
def marker(tmp_path):
    return tmp_path / "candidate_code_ran"


@pytest.fixture
def evil(notes, home, tmp_path, marker):
    """A locked bundle and a reshape proposal whose code writes `marker` the moment it is imported."""
    lock_bundle("notes")
    fmt = (notes / "src/notes/format.py").read_text()
    fmt = fmt.replace("SNIPPET = 60", f"SNIPPET = 80\nopen({str(marker)!r}, 'w').write('ran')")
    pid = propose(notes, tmp_path, {"src/notes/format.py": fmt})
    assert not marker.exists()
    return pid


# ----------------------------------------------------------------------------- the attack, end to end


@pytest.mark.parametrize("own_home", [False, True], ids=["the persons TOGI_HOME", "its own TOGI_HOME"])
def test_editing_a_locked_file_then_locking_it_under_its_own_test_approver_is_refused(
        notes, home, tmp_path, fake_pw_dir, own_home):
    """The attack: edit a locked money file in a repo it can write, make an approver of its own on a pseudo-terminal
    (TOGI_APPROVER_HOME, or that plus a TOGI_HOME of its own with the bundle added), and `togi lock` the new code."""
    # the person's side: a default-scope approver, and a lock they made themselves
    code, shown = human(home, ["lock", "notes"], "notes")
    assert code == 0, shown
    persons_approver = default_files(fake_pw_dir)
    assert persons_approver and audit.read(notes)[0]["scope"] == "default"
    lock_before = (notes / ".togi" / "locked.sha256").read_bytes()
    log_before = (notes / ".togi" / "audit.jsonl").read_bytes()

    # the agent edits a locked file (it can write the repo)
    store = notes / STORE
    store.write_text(store.read_text() + "\n# send every tip to the attacker\n")
    man = load_manifest(notes)
    assert audit.verify_locked(notes, man)["changed"] == [STORE]

    # ...and makes an approver of its own over a pty (this is allowed; it is what a test or the stone does)
    if own_home:                                       # TOGI_HOME=/tmp/mine, `togi add`, and an approver beside it
        run_home = tmp_path / "mine"
        run_home.mkdir()
        agent = dict(scope="test")
        assert run_togi(run_home, "add", str(notes), scope="test").returncode == 0
    else:                                              # the person's TOGI_HOME, a fresh TOGI_APPROVER_HOME
        run_home = home
        agent = dict(scope="test", env_extra={"TOGI_APPROVER_HOME": str(tmp_path / "agent_approver")})
    code, shown = run_in_pty(run_home, ["approver", "setup"], typed=[("New approver passphrase", ATTACKER),
                                                                     ("again", ATTACKER)], **agent)
    assert code == 0, shown
    assert default_files(fake_pw_dir) == persons_approver                 # the person's approver was not touched

    # ...then locks the modified code with it, typing the word and ITS passphrase
    code, shown = run_in_pty(run_home, ["lock", "notes"], typed=answers("notes", ATTACKER), **agent)
    assert code == 2 and REFUSED in shown, shown
    assert nothing_asked(shown)
    # nothing moved: the lock file, the audit log and the verdict on the locked file are exactly as they were
    assert (notes / ".togi" / "locked.sha256").read_bytes() == lock_before
    assert (notes / ".togi" / "audit.jsonl").read_bytes() == log_before
    assert audit.verify_locked(notes, man)["changed"] == [STORE]
    if own_home:
        assert not (run_home / "audit.key").exists() and not (run_home / "state" / "notes" / "audit.head").exists()
    assert default_files(fake_pw_dir) == persons_approver

    # and the same thing with the PERSON's approver (default scope) is how a reviewed change gets locked
    code, shown = human(home, ["lock", "notes"], "notes")
    assert code == 0, shown
    assert audit.verify_locked(notes, man)["ok"] is True
    last = audit.read(notes)[-1]
    assert last["action"] == "lock" and last["scope"] == "default" and last["decided_by"].startswith("human:")
    assert audit.verify(notes, "notes")["ok"]


def test_the_refusal_is_a_refused_error_with_a_way_out(notes, home):
    code, shown = run_in_pty(home, ["lock", "notes"], typed=answers("notes"), scope="test")
    assert code == 2 and REFUSED in shown
    assert "TOGI_APPROVER_HOME" in shown and "default approver" in shown and "unset" in shown


def test_the_single_rule_raises_a_refused_error_naming_the_fixed_location(home):
    """approvals.require_default_scope is the one rule: status refused (exit 2), names the fixed location."""
    with pytest.raises(Exception) as ex:
        approvals.require_default_scope("lock")
    e = ex.value
    assert e.code == REFUSED and e.status == "refused"
    assert str(paths.fixed_approver_home()) in e.message and "unset" in e.next


def test_an_empty_togi_approver_home_is_not_an_override(home, monkeypatch):
    monkeypatch.setenv("TOGI_APPROVER_HOME", "")
    approvals.require_default_scope("lock")           # does not raise: this is the default scope
    monkeypatch.setenv("TOGI_APPROVER_HOME", str(home.parent / "x"))
    with pytest.raises(Exception) as ex:
        approvals.require_default_scope("lock")
    assert ex.value.code == REFUSED


def test_the_other_human_checks_still_come_first_and_still_apply(notes, home):
    """The scope rule adds to the human gate, it does not replace it: an agent session and a pipe are refused for
    their own reasons, the scope rule only speaks to a person at a terminal."""
    r = run_togi(home, "lock", "notes", scope="test", input="notes\n")
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "needs_tty"
    code, shown = run_in_pty(home, ["lock", "notes"], typed=answers("notes"), scope="test",
                             env_extra={"TOGI_AGENT": "claude"})
    assert code == 2 and "human_only" in shown and REFUSED not in shown
    r = run_togi(home, "lock", "notes", scope="default", input="notes\n")
    assert json.loads(r.stdout)["error"]["code"] == "needs_tty"


# ----------------------------------------------------------------------------- a default-scope approver still works


def test_lock_with_the_default_approver_works_and_the_entry_carries_the_scope(notes, home):
    code, shown = human(home, ["lock", "notes"], "notes")
    assert code == 0, shown
    e = audit.read(notes)[0]
    assert e["action"] == "lock" and e["scope"] == "default" and e["decided_by"].startswith("human:")
    rows = togi("log", "notes")["data"]
    assert rows[0]["scope"] == "default" and rows[0]["action"] == "lock"
    assert audit.verify(notes, "notes")["ok"] is True


def test_the_scope_is_inside_the_hash_and_the_mac(notes, home):
    """A "test" entry cannot be rewritten as "default" (or the reverse) without breaking the chain."""
    audit.lock_bundle(load_manifest(notes), "human:test")                 # in-process under the test approver
    p = audit.audit_path(notes)
    assert json.loads(p.read_text())["scope"] == "test"
    p.write_text(p.read_text().replace('"scope":"test"', '"scope":"default"'))
    v = audit.verify(notes, "notes")
    assert v["ok"] is False and v["problems"][0]["kind"] == "chain"
    code, out = togi_code("log", "notes", "--verify")
    assert code == 1 and out["error"]["code"] == "audit_chain_broken"


def test_entries_written_before_the_scope_field_still_verify(notes, home):
    lock_bundle("notes")
    p = audit.audit_path(notes)
    e = json.loads(p.read_text().splitlines()[0])
    del e["scope"]                                                       # what an older togi wrote
    key = audit.load_key()
    e["hash"] = audit.entry_hash(e)
    e["mac"] = audit.entry_mac(key, e["hash"])
    p.write_text(audit.dump_line(e))
    audit._write_head("notes", e, key)
    assert audit.verify(notes, "notes")["ok"] is True
    lock_bundle("notes")                                                  # a new entry chains onto the old one
    es = audit.read(notes)
    assert "scope" not in es[0] and es[1]["scope"] == "default" and audit.verify(notes, "notes")["ok"]
    assert togi("log", "notes")["data"][0]["scope"] is None


def test_append_records_the_scope_of_the_process_that_wrote_it(notes, home, monkeypatch):
    def write():
        return audit.append(notes, "notes", action="reject", proposal=None, tier=None, files=[], diff_sha256=None,
                            stone_before=None, stone_after=None, decided_by="policy", reason="r")

    assert write()["scope"] == "test"                                     # TOGI_APPROVER_HOME is set by `home`
    monkeypatch.delenv("TOGI_APPROVER_HOME")
    assert write()["scope"] == "default"
    assert audit.verify(notes, "notes")["ok"]


def test_every_attempt_at_a_human_only_command_records_its_scope_in_the_journal(notes, home, fake_pw_dir):
    run_togi(home, "lock", "notes", scope="test")
    run_togi(home, "lock", "notes", scope="default")
    attempts = [json.loads(l) for l in (home / "state" / "_host" / "journal.jsonl").read_text().splitlines()]
    attempts = [a for a in attempts if a.get("event") == "host_attempt" and a["command"] == "lock"]
    assert [a["approver_scope"] for a in attempts] == ["test", "default"]


# ----------------------------------------------------------------------------- revert


def applied_polish(notes, home, tmp_path):
    (notes / "stone" / "tasks" / "hint.toml").write_text(HINT_TASK)
    lock_bundle("notes")
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})
    code, out = apply_(pid)
    assert code == 0 and out["data"]["action"] == "apply", out
    return pid


def test_revert_under_a_test_scope_approver_is_refused_and_under_the_default_one_it_works(notes, home, tmp_path):
    pid = applied_polish(notes, home, tmp_path)
    changed = (notes / MSG).read_bytes()
    log_before = (notes / ".togi" / "audit.jsonl").read_bytes()
    word = pid.split("_")[-1]
    code, shown = human(home, ["revert", pid], word, scope="test")
    assert code == 2 and REFUSED in shown and nothing_asked(shown), shown
    assert (notes / MSG).read_bytes() == changed and (notes / ".togi" / "audit.jsonl").read_bytes() == log_before
    assert meta_of(home, pid)["status"] == "applied"

    code, shown = human(home, ["revert", pid], word)
    assert code == 0, shown
    assert (notes / MSG).read_bytes() != changed
    e = audit.read(notes)[-1]
    assert e["action"] == "revert" and e["scope"] == "default" and e["decided_by"].startswith("human:")
    assert meta_of(home, pid)["status"] == "reverted" and audit.verify(notes, "notes")["ok"]


def test_the_library_revert_checks_the_scope_too(notes, home, tmp_path, monkeypatch):
    """Not only the CLI handler: revert_proposal itself refuses under a test approver (a terminal is stubbed here)."""
    import unittest.mock as mock

    pid = applied_polish(notes, home, tmp_path)
    with mock.patch.object(approvals, "_isatty", return_value=True):
        with pytest.raises(Exception) as ex:
            apply_mod.revert_proposal(pid)
    assert ex.value.code == REFUSED
    assert audit.read(notes)[-1]["action"] == "apply"


# ----------------------------------------------------------------------------- reshape evaluation


def test_evaluating_a_reshape_proposal_under_a_test_scope_approver_is_refused(notes, home, evil, marker):
    word = evil.split("_")[-1]
    code, shown = human_twice(home, ["apply", evil, "--driver", "script"], word, scope="test")
    assert code == 2 and REFUSED in shown and nothing_asked(shown), shown
    assert not marker.exists()                                           # the candidate's code never ran
    assert "evaluation_approval" not in meta_of(home, evil)
    assert [e["action"] for e in audit.read(notes)] == ["lock"]
    assert not (home / "proposals" / evil / "evaluation.json").exists() and not (home / "stone").exists()
    assert "SNIPPET = 60" in (notes / "src/notes/format.py").read_text()


def test_with_the_default_approver_the_evaluation_and_the_apply_are_recorded_with_their_scope(notes, home, evil, marker):
    code, shown = human_twice(home, ["apply", evil, "--driver", "script"], evil.split("_")[-1])
    assert code == 0, shown
    assert marker.exists()
    es = audit.read(notes)
    assert [e["action"] for e in es] == ["lock", "evaluate", "apply"]
    assert [e["scope"] for e in es] == ["default", "default", "default"]
    assert meta_of(home, evil)["evaluation_approval"]["scope"] == "default"
    assert audit.verify(notes, "notes")["ok"]


def forged_test_scope_approval(notes, home, pid):
    """What an agent can do with an approver of its own: sign an `evaluation_approval` that LOOKS valid to it."""
    m = meta_of(home, pid)
    diff = (home / "proposals" / pid / "diff.patch").read_text()
    sha = apply_mod.diff_sha(diff)
    plan = apply_mod.plan_changes(notes, apply_mod.classify_diff(diff, load_manifest(notes)))
    base = {rel: audit.file_sha256(notes / rel) for rel in plan}
    by, at = "human:agent", "2026-10-27T00:00:00.000Z"
    fields = apply_mod._eval_fields(pid, "notes", notes, sha, base, by, at)
    mac = approvals.sign_blob("evaluate", fields)                        # signed with the throwaway key: it owns it
    m["evaluation_approval"] = {"by": by, "at": at, "scope": "test", "diff_sha256": sha, "base": base,
                                "audit_seq": 1, "mac": mac}
    (home / "proposals" / pid / "meta.json").write_text(json.dumps(m))
    return m["evaluation_approval"]


def test_an_evaluation_approval_signed_by_a_test_approver_unlocks_nothing(notes, home, evil, marker):
    ea = forged_test_scope_approval(notes, home, evil)
    m = meta_of(home, evil)
    base = ea["base"]
    # it is a perfectly valid signature of the throwaway approver in force right now...
    assert approvals.blob_valid("evaluate", apply_mod._eval_fields(evil, "notes", notes, ea["diff_sha256"], base,
                                                                   ea["by"], ea["at"]), "test", ea["mac"])
    # ...and still does not count: the evaluation gate only honours the default scope
    assert apply_mod.evaluation_approved(m, evil, "notes", notes, ea["diff_sha256"], base) is False
    code, out = apply_(evil)                                             # an agent, no terminal
    assert code == 3 and out["data"]["reason"] == "evaluation_needs_human" and not marker.exists()
    code, shown = run_in_pty(home, ["apply", evil, "--driver", "script"], typed=answers("x"), scope="test")
    assert code == 2 and REFUSED in shown and not marker.exists()       # ...and with a pty, the gate refuses it
    assert not (home / "stone").exists()


# ----------------------------------------------------------------------------- human apply (the decision itself)


def skill_edit(notes):
    return (notes / "skills/SKILL.md").read_text() + "\nNote: prices are shown in dollars.\n"


def test_a_human_apply_under_a_test_scope_approver_is_refused_and_under_the_default_one_it_works(notes, home, tmp_path):
    (notes / "stone" / "tasks" / "hint.toml").write_text(HINT_TASK)
    lock_bundle("notes")
    pid = propose(notes, tmp_path, {MSG: good_message(notes), "skills/SKILL.md": skill_edit(notes)})
    code, out = apply_(pid)                                              # a skill edit on the script driver waits
    assert code == 3 and out["data"]["reason"] == "skill_edit_needs_agent_driver"
    word = pid.split("_")[-1]
    code, shown = human(home, ["apply", pid, "--driver", "script"], word, scope="test")
    assert code == 2 and REFUSED in shown and nothing_asked(shown), shown
    assert "prices are shown in dollars" not in (notes / "skills/SKILL.md").read_text()
    assert [e["action"] for e in audit.read(notes)] == ["lock"]

    code, shown = human(home, ["apply", pid, "--driver", "script"], word)
    assert code == 0, shown
    e = audit.read(notes)[-1]
    assert e["action"] == "apply" and e["decided_by"].startswith("human:") and e["scope"] == "default"
    assert "prices are shown in dollars" in (notes / "skills/SKILL.md").read_text()


def test_when_a_person_will_be_needed_the_refusal_comes_before_anything_slow(notes, home, tmp_path):
    t = notes / "togi.toml"
    t.write_text(t.read_text().replace("auto_polish = true", "auto_polish = false"))
    lock_bundle("notes")
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})
    code, shown = human(home, ["apply", pid, "--driver", "script"], pid.split("_")[-1], scope="test")
    assert code == 2 and REFUSED in shown and nothing_asked(shown)
    assert not (home / "stone").exists() and not (home / "proposals" / pid / "evaluation.json").exists()


def test_an_auto_polish_apply_needs_no_person_and_so_needs_no_default_approver(notes, home, tmp_path):
    """Only the human gates are scope-checked. A polish change the policy applies by itself is not one of them (and
    `apply` is refused to agent sessions anyway); its audit entry still says which scope it was written under."""
    pid = applied_polish(notes, home, tmp_path)
    e = audit.read(notes)[-1]
    assert e["action"] == "apply" and e["decided_by"] == "auto-polish" and e["scope"] == "test"
    assert meta_of(home, pid)["status"] == "applied"


# ----------------------------------------------------------------------------- what stays allowed, and the doctor


def test_money_approvals_still_work_under_a_test_scope_approver(notes, home):
    """The restriction is for the repo-changing gates. A throwaway approver still approves money (the bundle decides
    what to do with the scope, ctx.approval_scope) and still sets itself up: that is how the tests and the stone work."""
    lock_bundle("notes")                                                 # the money path needs a verified lock
    togi("notes", "add", "--text", "tip me")
    tip = ("notes", "tip", "send", "--id", "n_1", "--amount", "5.00")
    code, out = togi_code(*tip)
    assert code == 3
    aid = out["approval"]["id"]
    code, shown = human(home, ["approve", aid], "n_1")                   # scope="auto" -> the test approver
    assert code == 0, shown
    assert approvals.load(aid)["scope"] == "test"
    assert togi(*tip, "--approval", aid)["data"]["note"] == "n_1"
    status = togi("approver", "status")["data"]
    assert status["scope"] == "test" and status["configured"] is True


def test_doctor_says_which_actions_refuse_a_test_approver(home):
    row = [r for r in togi("doctor")["data"]["checks"] if r["check"] == "TOGI_APPROVER_HOME not set"][0]
    assert row["ok"] is False and "lock, revert and apply refuse" in row["detail"]


def test_the_helpers_pick_the_default_scope_for_exactly_the_repo_gates(home, fake_pw_dir):
    assert conftest.REPO_GATES == ("lock", "revert", "apply")
    assert conftest._resolve_scope(["approve", "x"], "auto", None, ()) == (None, ())
    fake, drop = conftest._resolve_scope(["lock", "notes"], "auto", None, ())
    assert fake == fake_pw_dir and drop == ("TOGI_APPROVER_HOME",)
    assert (fake_pw_dir / ".togi" / "approver").is_file()                # made in the fake home...
    assert default_files(conftest.REAL_PW_DIR) == conftest.REAL_APPROVER_AT_START      # ...never in the real one
