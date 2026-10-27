"""Fix round 2, part B: `togi apply` must not run proposed code before a person has decided.

The hole: for a reshape proposal, apply's evaluation copied the repo, applied the diff and ran the stone, which
executes the candidate code, before any human had looked at it. Now:
  - a reshape proposal is not evaluated until a person passed the human gate (terminal + typed word + passphrase) FOR
    EVALUATION; that approval is signed, bound to the diff and its base files, kept in the proposal's meta.json and
    written to the audit log. Applying it afterwards is a second human decision, as before;
  - a polish proposal keeps automatic evaluation, but may not touch code-bearing files at all;
  - before evaluating anything, apply re-checks both rules.

"Executes the candidate code" is proved behaviourally: the candidate `format.py` writes a marker file when imported.
"""
from __future__ import annotations

import json

import pytest

from togi import apply as apply_mod
from togi import audit, stone

from conftest import answers, hone_script, human, human_twice, lock_bundle, make_diff, run_in_pty, run_togi, togi_code

MSG = "togi_messages.toml"
HINT_TASK = '''
id = "hint"
instruction = "Show note n_9 and tell me what to try."
script = ["togi notes show --id n_9"]
[[check]]
transcript_contains = "Try: togi notes list"
'''


def meta_of(home, pid):
    return json.loads((home / "proposals" / pid / "meta.json").read_text())


def propose(repo, tmp_path, changes, expect=0):
    code, out = hone_script(repo, make_diff(repo, changes), tmp_path)
    assert code == expect, out
    return out


def apply_(pid, *flags):
    return togi_code("apply", pid, "--driver", "script", *flags)


@pytest.fixture
def marker(tmp_path):
    return tmp_path / "candidate_code_ran"


@pytest.fixture
def evil(notes, home, tmp_path, marker):
    """A locked bundle and a reshape proposal whose code announces itself (writes `marker`) the moment it is imported."""
    lock_bundle("notes")
    fmt = (notes / "src/notes/format.py").read_text()
    fmt = fmt.replace("SNIPPET = 60", f"SNIPPET = 80\nopen({str(marker)!r}, 'w').write('ran')")
    pid = propose(notes, tmp_path, {"src/notes/format.py": fmt})["data"]["proposal"]
    assert not marker.exists()
    return pid


@pytest.fixture
def no_stone(monkeypatch):
    """Fail loudly if anything evaluates (runs the stone)."""
    def boom(*a, **k):
        raise AssertionError("the stone ran: candidate code could have executed")

    monkeypatch.setattr(stone, "run_stone", boom)


# ----------------------------------------------------------------------------- B.1: reshape needs the gate first


def test_reshape_evaluation_refuses_without_the_human_gate(notes, home, evil, marker, no_stone):
    code, out = apply_(evil)
    assert code == 3 and out["error"]["code"] == "needs_human" and out["data"]["reason"] == "evaluation_needs_human"
    assert out["data"]["evaluated"] is False and "approve the evaluation" in out["error"]["message"]
    assert out["next"] == f"a person runs: togi apply {evil}"
    assert not marker.exists()
    assert not (home / "proposals" / evil / "evaluation.json").exists() and not (home / "stone").exists()
    assert [e["action"] for e in audit.read(notes)] == ["lock"]
    m = meta_of(home, evil)
    assert m["status"] == "needs_human" and m["awaiting"] == "evaluation_approval" and "evaluation_approval" not in m


def test_a_dry_run_does_not_evaluate_a_reshape_proposal_either(notes, home, evil, marker, no_stone):
    code, out = apply_(evil, "--dry-run")
    assert code == 3 and out["data"]["reason"] == "evaluation_needs_human" and out["data"]["dry_run"] is True
    assert not marker.exists() and meta_of(home, evil)["status"] == "proposed"      # a dry run records nothing


def test_piped_stdin_is_not_a_person(notes, home, evil, marker):
    r = run_togi(home, "apply", evil, "--driver", "script", input="x\nx\n")
    assert r.returncode == 3 and json.loads(r.stdout)["data"]["reason"] == "evaluation_needs_human"
    assert not marker.exists()


def test_an_agent_cannot_start_the_evaluation_even_on_a_pty(notes, home, evil, marker):
    code, shown = run_in_pty(home, ["apply", evil, "--driver", "script"], typed=answers("x"),
                             env_extra={"TOGI_AGENT": "claude"})
    assert code == 2 and "human_only" in shown and not marker.exists()


def test_the_word_alone_does_not_unlock_evaluation_and_nothing_runs(notes, home, evil, marker):
    word = evil.split("_")[-1]
    code, shown = human(home, ["apply", evil, "--driver", "script"], "wrong")
    assert code == 2 and "confirmation_mismatch" in shown and not marker.exists()
    code, shown = human(home, ["apply", evil, "--driver", "script"], word, passphrase="not the passphrase")
    assert code == 2 and "wrong_passphrase" in shown and not marker.exists()
    assert "evaluation_approval" not in meta_of(home, evil) and [e["action"] for e in audit.read(notes)] == ["lock"]
    assert not (home / "proposals" / evil / "evaluation.json").exists()


def test_the_prompt_shows_the_diff_before_asking(notes, home, evil):
    code, shown = human(home, ["apply", evil, "--driver", "script"], "wrong")
    assert "its code will RUN" in shown and "+SNIPPET = 80" in shown and "-SNIPPET = 60" in shown
    assert "open(" in shown and str(evil) in shown and "write('ran')" in shown


def test_after_the_gate_the_code_runs_and_the_approval_is_recorded_in_meta_and_audit(notes, home, evil, marker):
    word = evil.split("_")[-1]
    code, shown = human_twice(home, ["apply", evil, "--driver", "script"], word)
    assert code == 0, shown
    assert marker.exists()                                                     # evaluated, and only after the gate
    es = audit.read(notes)
    assert [e["action"] for e in es] == ["lock", "evaluate", "apply"]          # applying is its own, later decision
    ev = es[1]
    assert ev["proposal"] == evil and ev["tier"] == "reshape" and ev["decided_by"].startswith("human:")
    assert ev["diff_sha256"] == apply_mod.diff_sha((home / "proposals" / evil / "diff.patch").read_text())
    assert es[2]["decided_by"].startswith("human:") and audit.verify(notes, "notes")["ok"]
    m = meta_of(home, evil)
    ea = m["evaluation_approval"]
    assert ea["by"].startswith("human:") and ea["audit_seq"] == ev["seq"] and len(ea["mac"]) == 64
    assert ea["scope"] == "default" and ea["diff_sha256"] == ev["diff_sha256"]    # only the default approver can evaluate
    assert "SNIPPET = 80" in (notes / "src/notes/format.py").read_text()


def test_applying_is_still_a_separate_human_decision_after_evaluation(notes, home, evil, marker):
    word = evil.split("_")[-1]
    # the person approves the evaluation, then declines to apply (wrong word at the second prompt)
    code, shown = run_in_pty(home, ["apply", evil, "--driver", "script"], typed=answers(word) + [('Type "', "no")])
    assert code == 2 and "confirmation_mismatch" in shown
    assert marker.exists() and "SNIPPET = 60" in (notes / "src/notes/format.py").read_text()
    assert [e["action"] for e in audit.read(notes)] == ["lock", "evaluate"]
    # an agent's non-interactive apply now reuses the recorded approval (no second evaluation gate) but cannot apply
    marker.unlink()
    r = run_togi(home, "apply", evil, "--driver", "script", scope="default")   # the agent runs in the person's environment
    code, out = r.returncode, json.loads(r.stdout)
    assert code == 3 and out["data"]["reason"] == "reshape_needs_human"
    assert "SNIPPET = 60" in (notes / "src/notes/format.py").read_text()
    assert meta_of(home, evil)["awaiting"] == "apply"
    # the person comes back and only has to make the apply decision
    code, shown = human(home, ["apply", evil, "--driver", "script"], word)
    assert code == 0, shown
    assert "SNIPPET = 80" in (notes / "src/notes/format.py").read_text()
    assert [e["action"] for e in audit.read(notes)] == ["lock", "evaluate", "apply"]


def test_writing_an_approval_into_meta_json_by_hand_unlocks_nothing(notes, home, evil, marker, no_stone):
    m = meta_of(home, evil)
    m["evaluation_approval"] = {"by": "human:me", "at": "2099-01-01T00:00:00.000Z", "scope": "test",
                                "diff_sha256": m["diff_sha256"], "base": {}, "audit_seq": 1, "mac": "0" * 64}
    (home / "proposals" / evil / "meta.json").write_text(json.dumps(m))
    code, out = apply_(evil)
    assert code == 3 and out["data"]["reason"] == "evaluation_needs_human" and not marker.exists()
    del m["evaluation_approval"]["mac"]
    (home / "proposals" / evil / "meta.json").write_text(json.dumps(m))
    assert apply_(evil)[1]["data"]["reason"] == "evaluation_needs_human"


def test_an_approval_does_not_carry_over_to_another_diff_or_another_proposal(notes, home, evil, marker, tmp_path):
    word = evil.split("_")[-1]
    assert run_in_pty(home, ["apply", evil, "--driver", "script"], typed=answers(word) + [('Type "', "no")])[0] == 2
    approved = meta_of(home, evil)["evaluation_approval"]
    marker.unlink()
    # (a) the same proposal, but whoever can write the proposal store swapped in another diff (and its hash)
    fmt = (notes / "src/notes/format.py").read_text()
    other = make_diff(notes, {"src/notes/format.py": fmt.replace(
        "SNIPPET = 60", f"SNIPPET = 61\nopen({str(marker)!r}, 'w').write('other')")})
    d = home / "proposals" / evil
    original_diff, original_meta = (d / "diff.patch").read_text(), (d / "meta.json").read_text()
    swapped = json.loads(original_meta)
    swapped["diff_sha256"] = apply_mod.diff_sha(other)
    (d / "diff.patch").write_text(other)
    (d / "meta.json").write_text(json.dumps(swapped))
    code, out = apply_(evil)
    assert code == 3 and out["data"]["reason"] == "evaluation_needs_human" and not marker.exists()
    (d / "diff.patch").write_text(original_diff)
    (d / "meta.json").write_text(original_meta)
    # (b) a second proposal carrying the first one's approval record
    fmt2 = fmt.replace("SNIPPET = 60", f"SNIPPET = 90\nopen({str(marker)!r}, 'w').write('two')")
    pid2 = propose(notes, tmp_path, {"src/notes/format.py": fmt2})["data"]["proposal"]
    m2 = meta_of(home, pid2)
    m2["evaluation_approval"] = approved
    (home / "proposals" / pid2 / "meta.json").write_text(json.dumps(m2))
    code, out = apply_(pid2)
    assert code == 3 and out["data"]["reason"] == "evaluation_needs_human" and not marker.exists()
    # (c) the base file the approved diff applies to changed after the person looked at it
    (notes / "src/notes/format.py").write_text(fmt + "\n# edited after the person approved the evaluation\n")
    assert apply_(evil)[1]["data"]["reason"] == "evaluation_needs_human" and not marker.exists()


def test_a_diff_too_long_to_show_cannot_be_approved_at_the_prompt(notes, home, tmp_path, marker):
    lock_bundle("notes")
    fmt = (notes / "src/notes/format.py").read_text()
    long_ = fmt.replace("SNIPPET = 60", f"SNIPPET = 80\nopen({str(marker)!r}, 'w').write('x')") + "# padding\n" * 1100
    pid = propose(notes, tmp_path, {"src/notes/format.py": long_})["data"]["proposal"]
    code, shown = human(home, ["apply", pid, "--driver", "script"], pid.split("_")[-1])
    assert code == 2 and "diff_too_large_to_review" in shown and not marker.exists()
    assert "evaluation_approval" not in meta_of(home, pid)


# ----------------------------------------------------------------------------- B.3: polish keeps automatic evaluation


def test_polish_still_auto_evaluates_and_needs_no_gate(notes, home, tmp_path):
    (notes / "stone" / "tasks" / "hint.toml").write_text(HINT_TASK)
    lock_bundle("notes")
    msg = (notes / MSG).read_text().replace('message = "No note with id {id}."',
                                            'message = "No note with id {id}. Try: togi notes list"')
    pid = propose(notes, tmp_path, {MSG: msg})["data"]["proposal"]
    code, out = apply_(pid)
    assert code == 0 and out["data"]["action"] == "apply" and out["data"]["decided_by"] == "auto-polish"
    assert out["data"]["stone_before"] and out["data"]["stone_after"]
    assert "evaluation_approval" not in meta_of(home, pid)
    assert [e["action"] for e in audit.read(notes)] == ["lock", "apply"]


# ----------------------------------------------------------------------------- B.2/B.4: polish may not touch code


CODE_FILES = [
    ("skills/helper.py", "print('hi')\n"),
    ("skills/Helper.PY", "print('hi')\n"),
    ("skills/run.sh", "echo hi\n"),
    ("skills/x.js", "console.log(1)\n"),
    ("skills/x.ts", "console.log(1)\n"),
    ("skills/x.so", "not really a library\n"),
    ("skills/x.pyc", "not really bytecode\n"),
    ("skills/launcher", "#!/bin/sh\necho hi\n"),          # no extension, but a script
]


@pytest.mark.parametrize("rel,text", CODE_FILES)
def test_a_polish_proposal_touching_a_code_file_is_refused_at_hone(notes, home, tmp_path, rel, text):
    lock_bundle("notes")
    code, out = hone_script(notes, make_diff(notes, {rel: text}), tmp_path)
    assert code == 2 and out["status"] == "refused" and out["error"]["code"] == "code_in_polish"
    assert not (notes / rel).exists()
    e = audit.read(notes)[-1]
    assert e["action"] == "reject" and e["reason"].startswith("code_in_polish") and e["tier"] == "polish"


@pytest.mark.parametrize("rel,text", CODE_FILES[:3] + CODE_FILES[-1:])
def test_apply_refuses_a_polish_proposal_touching_a_code_file_even_if_hone_let_it_through(
        notes, home, tmp_path, rel, text, no_stone):
    """Belt and braces: apply re-checks, and nothing is evaluated (no_stone fails the test if the stone runs)."""
    lock_bundle("notes")
    diff = make_diff(notes, {rel: text})
    pid = "p_20261027000000_dead"
    meta = {"id": pid, "bundle": "notes", "repo": str(notes), "created": "2026-10-27T00:00:00.000Z",
            "status": "proposed", "source": "hone", "driver": "script", "model": None, "tier": "polish",
            "files": [rel], "diff_sha256": apply_mod.diff_sha(diff)}
    apply_mod.save_proposal(meta, diff, "a polish proposal that smuggles code")
    code, out = apply_(pid)
    assert code == 2 and out["error"]["code"] == "code_in_polish", out
    assert not (notes / rel).exists()
    e = audit.read(notes)[-1]
    assert e["action"] == "reject" and e["reason"].startswith("code_in_polish") and e["decided_by"] == "policy"
    assert meta_of(home, pid)["status"] == "rejected"
    assert apply_(pid)[1]["error"]["code"] == "proposal_closed"


def test_a_polish_proposal_cannot_use_an_approval_to_get_a_code_file_evaluated(notes, home, tmp_path, no_stone):
    """Evaluation approval is for reshape proposals. A polish proposal with code is refused whatever meta.json says."""
    lock_bundle("notes")
    diff = make_diff(notes, {"skills/helper.py": "print('hi')\n"})
    pid = "p_20261027000001_beef"
    meta = {"id": pid, "bundle": "notes", "repo": str(notes), "created": "2026-10-27T00:00:00.000Z",
            "status": "proposed", "source": "hone", "driver": "script", "model": None, "tier": "polish",
            "files": ["skills/helper.py"], "diff_sha256": apply_mod.diff_sha(diff),
            "evaluation_approval": {"by": "human:x", "at": "t", "scope": "test", "mac": "0" * 64}}
    apply_mod.save_proposal(meta, diff, "x")
    assert apply_(pid)[1]["error"]["code"] == "code_in_polish"


def test_a_reshape_proposal_may_carry_code_once_a_person_approved_its_evaluation(notes, home, evil, marker):
    code, shown = human_twice(home, ["apply", evil, "--driver", "script"], evil.split("_")[-1])
    assert code == 0, shown
    assert marker.exists()


def test_may_evaluate_is_the_one_rule():
    me = apply_mod.may_evaluate
    assert me("polish", [], False) is None and me("polish", [], True) is None
    assert me("polish", ["a.py"], False) == "code_in_polish" and me("polish", ["a.py"], True) == "code_in_polish"
    assert me("reshape", [], False) == "evaluation_needs_human" and me("reshape", ["a.py"], False) == "evaluation_needs_human"
    assert me("reshape", ["a.py"], True) is None and me("reshape", [], True) is None
    assert me("locked", [], True) == "locked_file" and me(None, [], True) == "locked_file"


def test_code_bearing_means_extension_or_shebang():
    cb = apply_mod.code_bearing_files
    assert cb({"a.md": "x", "b.toml": "x"}) == []
    assert cb({"a.PY": "x", "b.Sh": "x", "c.JS": "x", "d.ts": "x", "e.so": "x", "f.pyc": "x"}) == \
        ["a.PY", "b.Sh", "c.JS", "d.ts", "e.so", "f.pyc"]
    assert cb({"tool": "#!/usr/bin/env python3\n"}) == ["tool"] and cb({"notes.md": "see #!/bin/sh\n"}) == []
