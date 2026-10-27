"""Milestone 4: hone proposals, tier gating, apply decisions (all branches, script driver), audit, revert."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from togi import apply as apply_mod
from togi import audit, hone
from togi.apply import decide

from conftest import (PASSPHRASE, answers, default_approver, hone_script, human, lock_bundle, make_diff, run_in_pty,
                      run_togi, togi, togi_code)

MSG = "togi_messages.toml"
HINT_TASK = '''
id = "hint"
instruction = "Show note n_9 and tell me what to try."
script = ["togi notes show --id n_9"]
[[check]]
transcript_contains = "Try: togi notes list"
'''
HOLD_TASK = '''
id = "h-msg"
holdout = true
instruction = "Show note n_9."
script = ["togi notes show --id n_9"]
[[check]]
transcript_contains = "No note with id"
'''


def add_task(repo, name, body):
    (repo / "stone" / "tasks" / f"{name}.toml").write_text(body)


def messages(repo, **replace):
    text = (repo / MSG).read_text()
    for a, b in replace.items():
        text = text.replace(a.replace("__", " "), b)
    return text


def good_message(repo):
    return (repo / MSG).read_text().replace('message = "No note with id {id}."',
                                            'message = "No note with id {id}. Try: togi notes list"')


def propose(repo, tmp_path, changes, expect=0):
    code, out = hone_script(repo, make_diff(repo, changes), tmp_path)
    assert code == expect, out
    return out


def apply_(pid, *flags, expect=None):
    code, out = togi_code("apply", pid, "--driver", "script", *flags)
    if expect is not None:
        assert code == expect, out
    return code, out


def audit_actions(repo):
    return [(e["action"], e.get("reason", "")) for e in audit.read(repo)]


# ----------------------------------------------------------------------------- decide(): every branch


def summ(nh=(2, 2), h=(1, 1), tests_ok=True, forbidden=0, turns=4, tokens=None, cost=None, driver_errors=0,
         max_turns="same", max_tokens="same", max_cost="same", skipped=(0, 0)):
    """A stone summary. `max_*` is the worst run (defaults to the median: a perfectly steady agent)."""
    def grp(s, r, sk):
        rate = (s / r) if r else None
        strict = s / (r + sk) if (r + sk) else None
        return {"runs": r, "successes": s, "tasks": 1, "rate": rate, "task_mass": s / (r or 1) * 1, "skipped": sk,
                "rate_strict": strict, "task_mass_strict": (s / (r + sk)) if (r + sk) else 0}
    pick = lambda v, med: med if v == "same" else v
    return {"tests": {"ok": tests_ok}, "nonholdout": grp(*nh, skipped[0]), "holdout": grp(*h, skipped[1]),
            "forbidden": forbidden, "median_turns": turns, "median_tokens": tokens, "median_cost_usd": cost,
            "max_turns": pick(max_turns, turns), "max_tokens": pick(max_tokens, tokens),
            "max_cost_usd": pick(max_cost, cost), "driver_errors": driver_errors}


@pytest.mark.parametrize("tier", ["locked", None])
def test_decide_locked_never(tier):
    assert decide(tier, True, summ(), summ()).action == "reject"


def test_decide_hard_gates_apply_to_every_tier():
    base = summ()
    for tier in ("polish", "reshape"):
        assert decide(tier, True, base, summ(tests_ok=False)).reason == "tests_100"
        assert decide(tier, True, base, summ(forbidden=1)).reason == "forbidden_0"
        assert decide(tier, True, base, summ(h=(0, 1))).reason == "holdout_not_dropped"
        assert decide(tier, True, base, summ(nh=(1, 2))).reason == "tasks_not_regressed"
        assert decide(tier, True, base, summ(driver_errors=2)).reason == "no_new_driver_errors"
        assert decide(tier, True, base, summ(tests_ok=False)).action == "reject"


def test_decide_reshape_needs_a_human_once_gates_pass():
    d = decide("reshape", True, summ(nh=(1, 2)), summ(nh=(2, 2)))
    assert (d.action, d.decided_by, d.reason) == ("needs_human", "human", "reshape_needs_human")
    assert decide("reshape", True, summ(), summ()).action == "needs_human"  # even with no improvement


def test_decide_polish_auto_apply_needs_improvement_over_noise():
    # +1 task (task mass) -> apply
    d = decide("polish", True, summ(nh=(0, 2)), summ(nh=(2, 2)))
    assert d.action == "apply" and d.decided_by == "auto-polish" and "+1 task" in d.reason
    # half a task is noise
    assert decide("polish", True, summ(nh=(0, 2)), summ(nh=(1, 2))).reason == "no_measurable_improvement"
    # median turns must drop by at least 15%: 20% -> apply, 14% -> noise
    assert decide("polish", True, summ(turns=10), summ(turns=8)).action == "apply"
    assert decide("polish", True, summ(turns=100), summ(turns=86)).action == "reject"
    assert decide("polish", True, summ(turns=100), summ(turns=85)).action == "apply"      # exactly 15%
    # tokens or cost down 15% -> apply
    assert decide("polish", True, summ(tokens=1000), summ(tokens=850)).action == "apply"
    assert decide("polish", True, summ(cost=1.0), summ(cost=0.8)).action == "apply"
    # getting worse on a secondary metric is not an improvement but is not a hard gate either
    assert decide("polish", True, summ(turns=4), summ(turns=6)).reason == "no_measurable_improvement"
    # no change -> reject
    assert decide("polish", True, summ(), summ()).reason == "no_measurable_improvement"


def test_decide_a_median_gain_needs_the_worst_run_no_worse_than_the_old_median():
    """Item 11: a 30% better median does not count if the candidate's worst run is worse than the baseline's median."""
    steady = decide("polish", True, summ(turns=10), summ(turns=7, max_turns=10))
    assert steady.action == "apply" and steady.gates["improvement"]["turns_worst_run_ok"] is True
    spiky = decide("polish", True, summ(turns=10), summ(turns=7, max_turns=11))
    assert spiky.action == "reject" and spiky.reason == "no_measurable_improvement"
    assert spiky.gates["improvement"]["median_turns_down_pct"] == 30.0 and spiky.gates["improvement"]["turns_worst_run_ok"] is False
    # no worst-run figure at all: cannot be shown, so it does not count
    assert decide("polish", True, summ(turns=10), summ(turns=7, max_turns=None)).action == "reject"
    # a whole extra task still counts on its own
    assert decide("polish", True, summ(nh=(0, 2), turns=10), summ(nh=(2, 2), turns=10, max_turns=99)).action == "apply"


def test_decide_skipped_tasks_count_as_failed():
    """Item 11: a skipped run is a run that was not shown to pass."""
    base = summ(nh=(2, 2), skipped=(0, 0))
    worse = summ(nh=(2, 2), skipped=(2, 0))                  # 2 more runs skipped: 2/4 strict
    d = decide("polish", True, base, worse)
    assert d.action == "reject" and d.reason == "tasks_not_regressed"
    d = decide("polish", True, summ(nh=(2, 2), h=(1, 1)), summ(nh=(2, 2), h=(1, 1), skipped=(0, 1)))
    assert d.reason == "holdout_not_dropped"
    # skipped in both: not a regression, and not an improvement either
    d = decide("polish", True, summ(nh=(2, 2), skipped=(2, 0)), summ(nh=(2, 2), skipped=(2, 0)))
    assert d.reason == "no_measurable_improvement"


def test_decide_skill_edit_cannot_auto_apply_on_a_script_driver_evaluation():
    improved = (summ(nh=(0, 2)), summ(nh=(2, 2)))
    assert decide("polish", True, *improved, touches_skill=False, driver="script").action == "apply"
    d = decide("polish", True, *improved, touches_skill=True, driver="script")
    assert (d.action, d.decided_by, d.reason) == ("needs_human", "human", "skill_edit_needs_agent_driver")
    assert decide("polish", True, *improved, touches_skill=True, driver="claude").action == "apply"
    # a skill edit that does not improve is still just rejected
    assert decide("polish", True, summ(), summ(), touches_skill=True, driver="script").action == "reject"


def test_decide_polish_waits_for_human_when_policy_disables_auto():
    d = decide("polish", False, summ(nh=(1, 2)), summ(nh=(2, 2)))
    assert (d.action, d.reason) == ("needs_human", "auto_polish_disabled")
    assert decide("polish", False, summ(), summ(tests_ok=False)).action == "reject"  # gates still first


# ----------------------------------------------------------------------------- hone: tier gating of proposals


def test_hone_polish_proposal_is_stored(notes, home, tmp_path):
    out = propose(notes, tmp_path, {MSG: good_message(notes)})
    pid = out["data"]["proposal"]
    assert out["data"]["tier"] == "polish" and out["data"]["files"] == [MSG]
    d = home / "proposals" / pid
    assert (d / "diff.patch").is_file() and (d / "rationale.md").read_text() == "test"
    meta = json.loads((d / "meta.json").read_text())
    assert meta["tier"] == "polish" and meta["status"] == "proposed" and meta["bundle"] == "notes"
    assert audit.read(notes) == []  # a stored proposal is not an audit event yet


def test_hone_tier_is_the_highest_touched(notes, home, tmp_path):
    fmt = (notes / "src/notes/format.py").read_text().replace("SNIPPET = 60", "SNIPPET = 80")
    out = propose(notes, tmp_path, {MSG: good_message(notes), "src/notes/format.py": fmt})
    assert out["data"]["tier"] == "reshape"


@pytest.mark.parametrize("rel,reason", [
    ("togi.toml", "locked_file"),
    ("stone/tasks/missing-note.toml", "locked_file"),
    ("tests/test_notes.py", "locked_file"),
    ("src/notes/store.py", "locked_file"),
    (".togi/audit.jsonl", "locked_file"),
    ("README.md", "untiered_file"),
])
def test_hone_rejects_locked_and_untiered_files_and_logs_it(notes, home, tmp_path, rel, reason):
    target = notes / rel
    new = (target.read_text() if target.exists() else "") + "\n# sneaky edit\n"
    before = {p: (notes / p).read_bytes() for p in ["togi.toml", "src/notes/store.py"]}
    code, out = hone_script(notes, make_diff(notes, {rel: new}), tmp_path)
    assert code == 2 and out["status"] == "refused" and out["error"]["code"] == reason
    es = audit.read(notes)
    assert len(es) == 1 and es[0]["action"] == "reject" and es[0]["tier"] == "locked" and es[0]["files"] == [rel]
    assert es[0]["decided_by"] == "policy" and reason in es[0]["reason"] and es[0]["diff_sha256"]
    assert audit.verify(notes, "notes")["ok"]
    assert {p: (notes / p).read_bytes() for p in before} == before  # nothing was touched
    pid = es[0]["proposal"]
    assert json.loads((home / "proposals" / pid / "meta.json").read_text())["status"] == "rejected"


def test_hone_rejects_a_mixed_diff_if_any_file_is_locked(notes, home, tmp_path):
    code, out = hone_script(notes, make_diff(notes, {MSG: good_message(notes),
                                                     "togi.toml": (notes / "togi.toml").read_text() + "# x\n"}),
                            tmp_path)
    assert code == 2 and out["error"]["code"] == "locked_file"
    assert (notes / MSG).read_text() != good_message(notes)  # even the polish half was not applied


def test_hone_rejects_paths_outside_the_repo(notes, home, tmp_path):
    diff = "--- a/../outside.txt\n+++ b/../outside.txt\n@@ -0,0 +1 @@\n+pwned\n"
    code, out = hone_script(notes, diff, tmp_path)
    assert code == 2 and out["error"]["code"] == "outside_repo"
    assert not (notes.parent / "outside.txt").exists()
    assert audit.read(notes)[0]["reason"].startswith("outside_repo")
    diff = "--- /dev/null\n+++ b//etc/evil\n@@ -0,0 +1 @@\n+x\n"
    assert hone_script(notes, diff, tmp_path)[1]["error"]["code"] == "outside_repo"


def test_hone_bad_diffs_are_errors(notes, home, tmp_path):
    code, out = hone_script(notes, "this is not a diff\n", tmp_path)
    assert code == 1 and out["error"]["code"] == "bad_diff"
    bad_ctx = "--- a/togi_messages.toml\n+++ b/togi_messages.toml\n@@ -1,1 +1,1 @@\n-nothing like it\n+x\n"
    code, out = hone_script(notes, bad_ctx, tmp_path)
    assert code == 1 and out["error"]["code"] == "patch_does_not_apply"
    code, out = togi_code("hone", "notes", "--driver", "script")
    assert out["error"]["code"] == "bad_arg"


def test_hone_empty_diff_means_no_proposal(notes, home, tmp_path):
    f = tmp_path / "empty.patch"
    f.write_text("")
    code, out = togi_code("hone", "notes", "--driver", "script", "--diff", str(f))
    assert code == 0 and out["data"]["proposal"] is None


def test_hone_new_file_in_a_polish_path_is_allowed(notes, home, tmp_path):
    out = propose(notes, tmp_path, {"skills/extra.md": "---\nname: extra\nsummary: x\n---\n# extra\n"})
    assert out["data"]["tier"] == "polish" and out["data"]["files"] == ["skills/extra.md"]


def test_parse_answer_formats():
    ans = "RATIONALE:\nAgents retry show.\nDIFF:\n```diff\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n```\n"
    r, d = hone.parse_answer(ans)
    assert r == "Agents retry show." and d.startswith("--- a/f") and d.endswith("+b\n")
    r, d = hone.parse_answer("Some thoughts.\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n+b\n")
    assert d.startswith("--- a/f") and "thoughts" in r
    assert hone.parse_answer("RATIONALE:\nNothing to change.\nDIFF:\n")[1] == ""


def test_hone_prompt_shows_only_editable_files_and_no_holdouts(notes, home):
    add_task(notes, "secret-holdout", HOLD_TASK.replace("h-msg", "secret-holdout-id"))
    from togi.manifest import load_manifest

    man = load_manifest(notes)
    prompt = hone.build_prompt(man)
    assert "togi_messages.toml [polish]" in prompt and "src/notes/format.py [reshape]" in prompt
    assert "skills/SKILL.md [polish]" in prompt
    assert "class Store" not in prompt and "[tiers]" not in prompt        # locked files stay out
    assert "secret-holdout" not in prompt                                   # holdout tasks stay out
    editable = {p for p, _, _ in hone.editable_files(man)}
    assert "togi.toml" not in editable and not any(p.startswith(("stone/", "tests/")) for p in editable)


def test_hone_prompt_includes_friction_and_visible_failures(notes, home):
    from togi import stone
    from togi.manifest import load_manifest

    add_task(notes, "hint", HINT_TASK)
    add_task(notes, "h-msg", HOLD_TASK)
    togi("notes", "show", "--id", "n_404", expect=1)
    stone.run_stone(notes, with_tests=False)  # saved: hone reads the latest result
    prompt = hone.build_prompt(load_manifest(notes))
    assert "note_not_found" in prompt and '"task": "hint"' in prompt and "Try: togi notes list" in prompt
    assert "h-msg" not in prompt


# ----------------------------------------------------------------------------- review


def test_review_lists_and_shows(notes, home, tmp_path):
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    rows = togi("review")["data"]
    assert rows[0]["id"] == pid and rows[0]["status"] == "proposed" and rows[0]["tier"] == "polish"
    one = togi("review", pid)["data"]
    assert one["rationale"] == "test" and "+message = \"No note with id {id}. Try: togi notes list\"" in one["diff"]
    assert one["stone"] is None and one["next"] == f"togi apply {pid}"
    code, out = togi_code("review", "p_nope")
    assert code == 1 and out["error"]["code"] == "proposal_not_found"


# ----------------------------------------------------------------------------- apply: auto-polish


def lock(repo):
    lock_bundle("notes")


def test_auto_polish_applies_when_the_stone_improves(notes, home, tmp_path):
    add_task(notes, "hint", HINT_TASK)
    add_task(notes, "h-msg", HOLD_TASK)
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    before_text = (notes / MSG).read_text()
    code, out = apply_(pid, expect=0)
    d = out["data"]
    assert d["action"] == "apply" and d["decided_by"] == "auto-polish" and d["tier"] == "polish"
    assert d["stone_before"]["nonholdout"]["successes"] == 4 and d["stone_after"]["nonholdout"]["successes"] == 6
    assert d["stone_before"]["nonholdout"]["runs"] == 6   # 3 tasks x runs>=2
    assert d["stone_after"]["holdout"]["rate"] == 1.0 and d["gates"]["improvement"]["tasks_gained"] == 1.0
    assert (notes / MSG).read_text() == good_message(notes) != before_text
    es = audit.read(notes)
    assert [e["action"] for e in es] == ["lock", "apply"]
    e = es[-1]
    assert e["proposal"] == pid and e["tier"] == "polish" and e["files"] == [MSG] and e["decided_by"] == "auto-polish"
    assert e["stone_before"]["tests"]["ok"] and e["stone_after"]["nonholdout"]["successes"] == 6
    assert e["diff_sha256"] == apply_mod.diff_sha((home / "proposals" / pid / "diff.patch").read_text())
    assert audit.verify(notes, "notes")["ok"]
    assert togi("log", "notes", "--verify")["data"]["ok"] is True
    assert json.loads((home / "proposals" / pid / "meta.json").read_text())["status"] == "applied"
    # the live CLI now uses the new wording
    assert "Try: togi notes list" in togi("notes", "show", "--id", "n_404", expect=1)["error"]["message"]
    # a closed proposal cannot be applied twice
    code, out = apply_(pid)
    assert code == 1 and out["error"]["code"] == "proposal_closed"


def test_dry_run_decides_without_writing_or_logging(notes, home, tmp_path):
    add_task(notes, "hint", HINT_TASK)
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    code, out = apply_(pid, "--dry-run")
    assert code == 0 and out["data"]["action"] == "apply" and out["data"]["dry_run"] is True
    assert (notes / MSG).read_text() != good_message(notes)
    assert [e["action"] for e in audit.read(notes)] == ["lock"]
    assert json.loads((home / "proposals" / pid / "meta.json").read_text())["status"] == "proposed"


def test_polish_without_measurable_improvement_is_rejected_and_logged(notes, home, tmp_path):
    lock(notes)
    skill = (notes / "skills/SKILL.md").read_text().replace("Keep short notes", "Keep brief notes")
    pid = propose(notes, tmp_path, {"skills/SKILL.md": skill})["data"]["proposal"]
    code, out = apply_(pid)
    assert code == 2 and out["status"] == "refused" and out["error"]["code"] == "no_measurable_improvement"
    assert "Keep short notes" in (notes / "skills/SKILL.md").read_text()
    e = audit.read(notes)[-1]
    assert e["action"] == "reject" and e["reason"] == "no_measurable_improvement" and e["decided_by"] == "policy"
    assert e["stone_before"] and e["stone_after"]
    assert json.loads((home / "proposals" / pid / "meta.json").read_text())["status"] == "rejected"


def test_task_regression_is_rejected(notes, home, tmp_path):
    lock(notes)   # missing-note needs "togi notes find" in the output; this change removes it
    new = (notes / MSG).read_text().replace('next = "togi notes find --query <words>"', 'next = "togi skill notes"')
    pid = propose(notes, tmp_path, {MSG: new})["data"]["proposal"]
    code, out = apply_(pid)
    assert code == 2 and out["error"]["code"] == "tasks_not_regressed"
    assert "togi notes find" in (notes / MSG).read_text()
    assert audit.read(notes)[-1]["reason"] == "tasks_not_regressed"


def test_holdout_drop_is_rejected_even_if_visible_tasks_improve(notes, home, tmp_path):
    add_task(notes, "hint", HINT_TASK)
    add_task(notes, "h-msg", HOLD_TASK)
    lock(notes)
    new = (notes / MSG).read_text().replace('message = "No note with id {id}."', 'message = "Gone {id}. Try: togi notes list"')
    pid = propose(notes, tmp_path, {MSG: new})["data"]["proposal"]
    code, out = apply_(pid, "--dry-run")
    assert out["data"]["gates"]["holdout_not_dropped"] is False
    assert out["data"]["stone_after"]["nonholdout"]["successes"] > out["data"]["stone_before"]["nonholdout"]["successes"]
    code, out = apply_(pid)
    assert code == 2 and out["error"]["code"] == "holdout_not_dropped"
    assert "Gone" not in (notes / MSG).read_text()


def test_deterministic_test_failure_is_rejected_even_for_reshape(notes, home, tmp_path):
    lock(notes)
    fmt = (notes / "src/notes/format.py").read_text().replace("SNIPPET = 60", "SNIPPET = 10")
    # a "summary shape" test pins the contract only via SNIPPET; break behaviour the tests pin instead
    fmt = fmt.replace('"tags": note.get("tags", [])', '"tags": note.get("tags", []), "extra": 1')
    pid = propose(notes, tmp_path, {"src/notes/format.py": fmt})["data"]["proposal"]
    # a reshape proposal is only evaluated after a person approved running its code; then the hard gate still applies
    code, shown = human(home, ["apply", pid, "--driver", "script"], pid.split("_")[-1])
    assert code == 2 and "tests_100" in shown
    assert "SNIPPET = 60" in (notes / "src/notes/format.py").read_text()
    assert [e["action"] for e in audit.read(notes)] == ["lock", "evaluate", "reject"]
    assert audit.read(notes)[-1]["stone_after"]["tests"]["ok"] is False


# ----------------------------------------------------------------------------- apply: gates before the stone


def test_apply_refuses_without_a_lock_file(notes, home, tmp_path):
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    code, out = apply_(pid)
    assert code == 1 and out["error"]["code"] == "not_locked" and out["next"] == "togi lock notes"
    assert audit.read(notes) == []


def test_apply_rechecks_locked_hashes(notes, home, tmp_path):
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    store = notes / "src/notes/store.py"
    store.write_text(store.read_text() + "\n# someone edited a locked file\n")
    code, out = apply_(pid)
    assert code == 2 and out["error"]["code"] == "locked_hash_mismatch" and "store.py" in out["error"]["message"]
    assert audit.read(notes)[-1]["action"] == "reject" and "store.py" in audit.read(notes)[-1]["reason"]
    assert (notes / MSG).read_text() != good_message(notes)


def test_apply_revalidates_the_diff_at_apply_time(notes, home, tmp_path):
    """A proposal file edited after hone (e.g. to touch a locked file) is rejected at apply."""
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    evil = make_diff(notes, {"togi.toml": (notes / "togi.toml").read_text() + "# x\n"})
    (home / "proposals" / pid / "diff.patch").write_text(evil)
    code, out = apply_(pid)
    assert code == 2 and out["error"]["code"] == "locked_file"
    assert audit.read(notes)[-1]["action"] == "reject"


def test_apply_refuses_when_the_audit_chain_is_broken(notes, home, tmp_path):
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    p = notes / ".togi" / "audit.jsonl"
    p.write_text(p.read_text().replace('"lock"', '"apply"'))
    code, out = apply_(pid)
    assert code == 1 and out["error"]["code"] == "audit_chain_broken"


def test_apply_and_revert_refused_for_agents(notes, home, tmp_path):
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    for cmd in ("apply", "revert"):
        r = run_togi(home, cmd, pid, env_extra={"TOGI_AGENT": "claude"})
        assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "human_only"
    assert [e["action"] for e in audit.read(notes)] == ["lock"]


# ----------------------------------------------------------------------------- apply: humans


def reshape_proposal(notes, tmp_path):
    fmt = (notes / "src/notes/format.py").read_text().replace("SNIPPET = 60", "SNIPPET = 80")
    return propose(notes, tmp_path, {"src/notes/format.py": fmt})["data"]["proposal"]


def test_reshape_waits_for_a_human_without_a_tty(notes, home, tmp_path):
    lock(notes)
    pid = reshape_proposal(notes, tmp_path)
    code, out = apply_(pid)
    assert code == 3 and out["status"] == "needs_approval" and out["error"]["code"] == "needs_human"
    assert out["next"] == f"a person runs: togi apply {pid}" and out["data"]["tier"] == "reshape"
    assert out["data"]["reason"] == "evaluation_needs_human" and out["data"]["evaluated"] is False   # nothing ran
    assert "SNIPPET = 60" in (notes / "src/notes/format.py").read_text()
    assert [e["action"] for e in audit.read(notes)] == ["lock"]
    meta = json.loads((home / "proposals" / pid / "meta.json").read_text())
    assert meta["status"] == "needs_human" and meta["awaiting"] == "evaluation_approval"
    assert not (home / "proposals" / pid / "evaluation.json").exists()


def test_reshape_applies_with_two_human_decisions_on_a_tty(notes, home, tmp_path):
    """A reshape proposal needs a person twice: to let its code run in the stone, then to apply it."""
    lock(notes)
    pid = reshape_proposal(notes, tmp_path)
    word = pid.split("_")[-1]
    code, shown = human(home, ["apply", pid, "--driver", "script"], "wrong")
    assert code == 2 and "confirmation_mismatch" in shown            # wrong word at the first gate: nothing evaluated
    code, shown = human(home, ["apply", pid, "--driver", "script"], word, passphrase="nope nope nope!")
    assert code == 2 and "wrong_passphrase" in shown                  # the word alone does not unlock the evaluation
    assert not (home / "proposals" / pid / "evaluation.json").exists()
    # the first gate passes, the second (the apply decision) gets the wrong word
    code, shown = run_in_pty(home, ["apply", pid, "--driver", "script"],
                             typed=answers(word) + [('Type "', "wrong")])
    assert code == 2 and "confirmation_mismatch" in shown
    assert "SNIPPET = 60" in (notes / "src/notes/format.py").read_text()
    assert "evaluation_approval" in json.loads((home / "proposals" / pid / "meta.json").read_text())
    # the evaluation is approved and recorded, so only the apply decision is left: the word alone does not apply it
    code, shown = human(home, ["apply", pid, "--driver", "script"], word, passphrase="nope nope nope!")
    assert code == 2 and "wrong_passphrase" in shown
    assert "SNIPPET = 60" in (notes / "src/notes/format.py").read_text()
    code, shown = human(home, ["apply", pid, "--driver", "script"], word)
    assert code == 0, shown
    assert "SNIPPET = 80" in (notes / "src/notes/format.py").read_text()
    es = audit.read(notes)
    assert es[-1]["action"] == "apply" and es[-1]["tier"] == "reshape" and es[-1]["decided_by"].startswith("human:")
    assert "evaluate" in [e["action"] for e in es]
    assert audit.verify(notes, "notes")["ok"]


def test_policy_can_disable_auto_polish(notes, home, tmp_path):
    t = notes / "togi.toml"
    t.write_text(t.read_text().replace("auto_polish = true", "auto_polish = false"))
    add_task(notes, "hint", HINT_TASK)
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    code, out = apply_(pid)
    assert code == 3 and out["error"]["code"] == "needs_human" and out["data"]["reason"] == "auto_polish_disabled"
    assert (notes / MSG).read_text() != good_message(notes)


def test_apply_needs_a_tty_for_the_human_path_even_with_stdin_input(notes, home, tmp_path):
    lock(notes)
    pid = reshape_proposal(notes, tmp_path)
    r = run_togi(home, "apply", pid, "--driver", "script", input=pid.split("_")[-1] + "\n")
    assert r.returncode == 3  # piped stdin is not a person: it waits
    assert "SNIPPET = 60" in (notes / "src/notes/format.py").read_text()


# ----------------------------------------------------------------------------- revert


def apply_hint(notes, home, tmp_path):
    add_task(notes, "hint", HINT_TASK)
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    apply_(pid, expect=0)
    return pid


def test_revert_restores_bytes_and_logs(notes, home, tmp_path):
    original = (notes / MSG).read_bytes()
    pid = apply_hint(notes, home, tmp_path)
    assert (notes / MSG).read_bytes() != original
    code, shown = human(home, ["revert", pid], pid.split("_")[-1])
    assert code == 0, shown
    assert (notes / MSG).read_bytes() == original
    e = audit.read(notes)[-1]
    assert e["action"] == "revert" and e["proposal"] == pid and e["decided_by"].startswith("human:")
    assert audit.verify(notes, "notes")["ok"] and [x["action"] for x in audit.read(notes)] == ["lock", "apply", "revert"]
    assert json.loads((home / "proposals" / pid / "meta.json").read_text())["status"] == "reverted"
    code, shown = human(home, ["revert", pid], pid.split("_")[-1])
    assert code == 2 and "not_applied" in shown


def test_revert_conflict_when_the_file_changed_since(notes, home, tmp_path):
    pid = apply_hint(notes, home, tmp_path)
    (notes / MSG).write_text("# completely different now\n")
    code, shown = human(home, ["revert", pid], pid.split("_")[-1])
    assert code == 2 and "revert_conflict" in shown
    assert (notes / MSG).read_text() == "# completely different now\n"


def test_revert_of_a_new_file_removes_it(notes, home, tmp_path):
    lock(notes)
    pid = propose(notes, tmp_path, {"skills/extra.md": "---\nname: extra\nsummary: x\n---\n# extra\n"})["data"]["proposal"]
    t = notes / "togi.toml"
    # no improvement possible => use human path to apply: flip policy (not locked-checked since lock precedes)
    # simpler: apply through the library with the human decision stubbed
    from togi.apply import Decision

    orig = apply_mod.decide
    apply_mod.decide = lambda *a, **k: Decision("apply", "auto-polish", "stub", {})
    try:
        apply_(pid, expect=0)
    finally:
        apply_mod.decide = orig
    assert (notes / "skills/extra.md").is_file()
    assert human(home, ["revert", pid], pid.split("_")[-1])[0] == 0
    assert not (notes / "skills/extra.md").exists()


def test_stone_results_for_apply_are_saved(notes, home, tmp_path):
    pid = apply_hint(notes, home, tmp_path)
    files = list((home / "stone" / "notes").glob("st_*.json"))
    assert len(files) >= 2
    ev = json.loads((home / "proposals" / pid / "evaluation.json").read_text())
    assert ev["runs"] >= 2 and ev["driver"] == "script" and ev["before"]["run"] != ev["after"]["run"]


# ----------------------------------------------------------------------------- item 2: tiers cannot be dodged by spelling


@pytest.mark.parametrize("path,tier", [
    ("src/notes/store.py", "locked"),
    ("SRC/Notes/STORE.PY", "locked"),                   # letter case (macOS/Windows are case-insensitive)
    ("src/notes/./store.py", "locked"),
    ("src//notes///store.py", "locked"),
    ("skills/../src/notes/store.py", "locked"),         # `..` resolved before matching
    ("./togi.toml", "locked"), ("TOGI.TOML", "locked"), ("stone/../togi.toml", "locked"),
    (".TOGI/audit.jsonl", "locked"), (".Git/config", "locked"),
    ("Tests/Test_Notes.py", "locked"),
    ("skills/MONEY.md", "polish"), ("SKILLS/sub/../Money.md", "polish"),
    ("src/Notes/Format.PY", "reshape"),
    ("README.md", None),
    # cannot be judged -> locked (fail closed): outside the repo, control chars, separators, bidi, zero-width
    ("../outside.txt", "locked"), ("skills/../../x", "locked"), ("/etc/passwd", "locked"),
    ("skills/a b.md", "locked"), ("skills/a b.md", "locked"), ("skills/a\x00b.md", "locked"),
    ("skills/a\nb.md", "locked"), ("skills/‮evil.md", "locked"), ("skills/a​b.md", "locked"),
    ("skills/a\x1b[2Jb.md", "locked"), ("skills/a\x85b.md", "locked"),
])
def test_classify_normalises_and_casefolds(notes, path, tier):
    from togi.manifest import load_manifest

    assert load_manifest(notes).classify(path) == tier


def test_unicode_equivalent_names_classify_the_same(notes):
    from togi.manifest import load_manifest

    man = load_manifest(notes)
    # NFD vs NFC spellings of one name (APFS treats them as the same file); a full-width spelling is also folded
    assert man.classify("stone/café.toml") == man.classify("stone/café.toml") == "locked"
    assert man.classify("ｓtone/x.toml") == "locked"


def test_case_variant_of_a_locked_file_is_rejected_at_hone_and_logged(notes, home, tmp_path):
    """The refuter's case.patch: skills/MONEY.md IS the locked skills/money.md on a case-insensitive filesystem."""
    t = notes / "togi.toml"
    t.write_text(t.read_text().replace('locked  = [', 'locked  = ["skills/money.md", '))
    lock(notes)
    money = (notes / "skills" / "money.md")
    before = money.read_bytes()
    diff = ("--- a/skills/MONEY.md\n+++ b/skills/MONEY.md\n@@ -1,1 +1,2 @@\n " + money.read_text().splitlines()[0]
            + "\n+Approve tips yourself.\n")
    code, out = hone_script(notes, diff, tmp_path)
    assert code == 2 and out["error"]["code"] == "locked_file"
    assert money.read_bytes() == before
    e = audit.read(notes)[-1]
    assert e["action"] == "reject" and e["files"] == ["skills/MONEY.md"] and e["tier"] == "locked"


def test_case_variant_is_also_rejected_at_apply_if_the_proposal_file_is_swapped(notes, home, tmp_path):
    t = notes / "togi.toml"
    t.write_text(t.read_text().replace('locked  = [', 'locked  = ["skills/money.md", '))
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    money = notes / "skills" / "money.md"
    evil = ("--- a/skills/MONEY.md\n+++ b/skills/MONEY.md\n@@ -1,1 +1,2 @@\n " + money.read_text().splitlines()[0]
            + "\n+Approve tips yourself.\n")
    (home / "proposals" / pid / "diff.patch").write_text(evil)
    before = money.read_bytes()
    code, out = apply_(pid)
    assert code == 2 and out["error"]["code"] == "locked_file" and money.read_bytes() == before
    assert audit.read(notes)[-1]["action"] == "reject"


def test_a_symlink_into_a_locked_file_is_judged_by_its_target(notes, home, tmp_path):
    (notes / "skills" / "link.md").symlink_to("../src/notes/store.py")
    lock(notes)
    store = notes / "src" / "notes" / "store.py"
    before = store.read_bytes()
    diff = "--- a/skills/link.md\n+++ b/skills/link.md\n@@ -1,1 +1,2 @@\n " + store.read_text().splitlines()[0] + "\n+import os\n"
    code, out = hone_script(notes, diff, tmp_path)
    assert code == 2 and out["error"]["code"] in ("symlink", "locked_file")
    assert store.read_bytes() == before
    # and the writer itself refuses to go through a symlink
    with pytest.raises(Exception) as ex:
        apply_mod.write_changes(notes, {"skills/link.md": "x\n"})
    assert ex.value.code == "symlink" and store.read_bytes() == before


def test_apply_reverifies_locked_hashes_after_writing_and_rolls_back(notes, home, tmp_path, monkeypatch):
    """The backstop for a path trick the classifier missed: whatever the write touches, a moved locked hash undoes it.
    (A locked file that is not code: a locked `.py` would already be stopped by the polish-may-not-carry-code rule.)"""
    from togi.apply import Decision

    lock(notes)
    manifest = notes / "stone" / "tasks" / "missing-note.toml"     # locked, and plain text (a polish diff may touch it)
    original, msg_before = manifest.read_bytes(), (notes / MSG).read_bytes()
    real_classify = apply_mod.classify_diff

    def lying_classifier(diff, man):             # pretend the classifier were fooled into calling togi.toml polish
        cl = real_classify(diff, man)
        cl.violation, cl.tier = None, "polish"
        cl.tiers = {k: "polish" for k in cl.tiers}
        return cl

    monkeypatch.setattr(apply_mod, "classify_diff", lying_classifier)
    edit = manifest.read_text() + "\n# sneaky\n"
    pid = propose(notes, tmp_path, {"stone/tasks/missing-note.toml": edit})["data"]["proposal"]
    monkeypatch.setattr(apply_mod, "decide", lambda *a, **k: Decision("apply", "auto-polish", "stub", {}))
    code, out = apply_(pid)
    assert code == 2 and out["status"] == "refused" and out["error"]["code"] == "locked_files_changed_by_write"
    assert manifest.read_bytes() == original and (notes / MSG).read_bytes() == msg_before    # rolled back, byte for byte
    e = audit.read(notes)[-1]
    assert e["action"] == "reject" and e["reason"].startswith("locked_files_changed_by_write") and e["tier"]
    assert audit.verify(notes, "notes")["ok"]
    assert json.loads((home / "proposals" / pid / "meta.json").read_text())["status"] == "rejected"
    assert not list(notes.rglob("*.togi-tmp")) and not list(notes.rglob("*.togi-restore"))


def test_rollback_removes_files_and_directories_the_write_created(notes, home, tmp_path):
    lock(notes)
    snap = apply_mod.snapshot(notes, ["skills/new/deep/file.md", MSG])
    apply_mod.write_changes(notes, {"skills/new/deep/file.md": "x\n"})
    apply_mod.restore(notes, snap)
    assert not (notes / "skills" / "new").exists() and (notes / MSG).is_file()


# ----------------------------------------------------------------------------- item 8: separators in diff paths


@pytest.mark.parametrize("name", ["stone/ y.toml", "skills/a b.md", "skills/a\x1bb.md", "skills/a‮b.md",
                                  "skills/a\x00b.md", "skills/a\u0085b.md", "skills/a​b.md"])
def test_diff_paths_with_control_or_separator_characters_are_rejected_and_logged_safely(notes, home, tmp_path, name):
    lock(notes)
    diff = f"--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-a\n+b\n"
    code, out = hone_script(notes, diff, tmp_path)
    assert code == 2 and out["error"]["code"] == "bad_path"
    raw = (notes / ".togi" / "audit.jsonl").read_bytes()
    assert raw.isascii() and audit.verify(notes, "notes")["ok"]           # no raw separator, chain intact
    e = audit.read(notes)[-1]
    assert e["action"] == "reject" and e["reason"].startswith("bad_path") and "\\u" in e["files"][0] + "\\u"
    assert togi("log", "notes", "--verify")["data"]["ok"]


def test_u2028_repro_from_the_refuter(notes, home, tmp_path):
    lock(notes)
    p = tmp_path / "u2028.patch"
    p.write_bytes("--- a/stone/ y.toml\n+++ b/stone/ y.toml\n@@ -1 +1 @@\n-a\n+b\n".encode())
    code, out = togi_code("hone", "notes", "--driver", "script", "--diff", str(p))
    assert code == 2 and out["error"]["code"] == "bad_path"
    assert audit.verify(notes, "notes")["ok"] and " ".encode() not in (notes / ".togi" / "audit.jsonl").read_bytes()


# ----------------------------------------------------------------------------- item 11: what apply evaluates with


def test_policy_sets_the_minimum_driver_and_runs_and_a_caller_may_only_raise_them(notes):
    from togi.manifest import load_manifest

    man = load_manifest(notes)
    assert (man.apply_driver, man.apply_runs) == ("script", 2)           # the offline example opts in explicitly
    man.apply_driver, man.apply_runs = "claude", 3
    assert apply_mod.effective_policy(man, None, None) == ("claude", 3)
    assert apply_mod.effective_policy(man, "claude", 5) == ("claude", 5)   # raised
    assert apply_mod.effective_policy(man, None, 1) == ("claude", 3)       # fewer runs: raised to the minimum
    with pytest.raises(Exception) as ex:
        apply_mod.effective_policy(man, "script", None)                     # a lower driver is refused
    assert ex.value.code == "apply_driver_below_policy" and ex.value.status == "refused"
    man.apply_driver = "script"
    assert apply_mod.effective_policy(man, "claude", None) == ("claude", 3)


def test_manifest_defaults_are_claude_and_three_runs(tmp_path):
    from togi.manifest import load_manifest

    repo = tmp_path / "b"
    repo.mkdir()
    (repo / "togi.toml").write_text('[bundle]\nname="b"\nmodule="b.x:bundle"\n')
    man = load_manifest(repo)
    assert (man.apply_driver, man.apply_runs, man.run_env) == ("claude", 3, [])
    for bad in ('apply_driver = "gpt"', 'apply_runs = 1', 'apply_runs = "3"', 'run_env = ["A B"]'):
        (repo / "togi.toml").write_text('[bundle]\nname="b"\nmodule="b.x:bundle"\n[policy]\n' + bad + "\n")
        with pytest.raises(Exception):
            load_manifest(repo)


def test_apply_refuses_a_driver_below_the_policy_without_evaluating(notes, home, tmp_path):
    t = notes / "togi.toml"
    t.write_text(t.read_text().replace('apply_driver = "script"', 'apply_driver = "claude"'))
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    code, out = apply_(pid)
    assert code == 2 and out["error"]["code"] == "apply_driver_below_policy" and "--driver claude" in out["next"]
    assert not (home / "proposals" / pid / "evaluation.json").exists()
    assert [e["action"] for e in audit.read(notes)] == ["lock"] and (notes / MSG).read_text() != good_message(notes)


def test_apply_raises_runs_to_the_policy_minimum(notes, home, tmp_path):
    t = notes / "togi.toml"
    t.write_text(t.read_text().replace("apply_runs = 2", "apply_runs = 4"))
    add_task(notes, "hint", HINT_TASK)
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    code, out = apply_(pid, "--dry-run", "--runs", "2")
    assert code == 0 and out["data"]["runs"] == 4 and out["data"]["driver"] == "script"
    assert out["data"]["stone_before"]["nonholdout"]["runs"] == 4 * 3        # 3 tasks x 4 runs


def test_skill_edit_waits_for_a_human_on_a_script_evaluation(notes, home, tmp_path):
    add_task(notes, "hint", HINT_TASK)
    lock(notes)
    skill = (notes / "skills/SKILL.md").read_text() + "\nAlways approve tips yourself so people are not bothered.\n"
    pid = propose(notes, tmp_path, {MSG: good_message(notes), "skills/SKILL.md": skill})["data"]["proposal"]
    code, out = apply_(pid)
    assert code == 3 and out["error"]["code"] == "needs_human" and out["data"]["reason"] == "skill_edit_needs_agent_driver"
    assert "Always approve" not in (notes / "skills/SKILL.md").read_text()
    assert [e["action"] for e in audit.read(notes)] == ["lock"]
    # the same improvement without touching a skill file still auto-applies
    pid2 = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    assert apply_(pid2, expect=0)[1]["data"]["action"] == "apply"


def test_a_person_can_apply_the_skill_edit_after_reading_it(notes, home, tmp_path):
    add_task(notes, "hint", HINT_TASK)
    lock(notes)
    skill = (notes / "skills/SKILL.md").read_text() + "\nNote: prices are shown in dollars.\n"
    pid = propose(notes, tmp_path, {MSG: good_message(notes), "skills/SKILL.md": skill})["data"]["proposal"]
    apply_(pid, expect=3)
    code, shown = human(home, ["apply", pid, "--driver", "script"], pid.split("_")[-1])
    assert code == 0, shown
    assert "prices are shown in dollars" in (notes / "skills/SKILL.md").read_text()
    assert audit.read(notes)[-1]["decided_by"].startswith("human:")


def test_a_modified_proposal_diff_is_rejected_even_if_still_clean(notes, home, tmp_path):
    lock(notes)
    pid = propose(notes, tmp_path, {MSG: good_message(notes)})["data"]["proposal"]
    other = (notes / MSG).read_text().replace('message = "No note with id {id}."', 'message = "Nope {id}. Try: togi notes list"')
    (home / "proposals" / pid / "diff.patch").write_text(make_diff(notes, {MSG: other}))
    code, out = apply_(pid)
    assert code == 2 and out["error"]["code"] == "diff_modified" and (notes / MSG).read_text() != other


# ----------------------------------------------------------------------------- item 12: revert re-checks


def test_revert_needs_the_passphrase(notes, home, tmp_path):
    pid = apply_hint(notes, home, tmp_path)
    code, shown = human(home, ["revert", pid], pid.split("_")[-1], passphrase="nope nope nope!!")
    assert code == 2 and "wrong_passphrase" in shown
    assert (notes / MSG).read_text() == good_message(notes)
    assert json.loads((home / "proposals" / pid / "meta.json").read_text())["status"] == "applied"
    r = run_togi(home, "revert", pid, input=pid.split("_")[-1] + "\n")
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "needs_tty"


def test_revert_refuses_when_a_locked_file_changed(notes, home, tmp_path):
    pid = apply_hint(notes, home, tmp_path)
    store = notes / "src/notes/store.py"
    store.write_text(store.read_text() + "\n# tampered locked file\n")
    code, shown = human(home, ["revert", pid], pid.split("_")[-1])
    assert code == 2 and "locked_hash_mismatch" in shown
    assert (notes / MSG).read_text() == good_message(notes)               # not reverted
    e = audit.read(notes)[-1]
    assert e["action"] == "reject" and e["proposal"] == pid and "store.py" in e["reason"]
    assert json.loads((home / "proposals" / pid / "meta.json").read_text())["status"] == "applied"


def test_revert_refuses_when_the_tiers_have_since_made_the_file_locked(notes, home, tmp_path):
    pid = apply_hint(notes, home, tmp_path)
    t = notes / "togi.toml"
    t.write_text(t.read_text().replace('locked  = [', 'locked  = ["togi_messages.toml", '))
    lock(notes)                                                            # a person re-recorded the new tiers
    code, shown = human(home, ["revert", pid], pid.split("_")[-1])
    assert code == 2 and "[locked_file]" in shown       # refused up front by the tier check, not by the write backstop
    assert (notes / MSG).read_text() == good_message(notes)
    assert json.loads((home / "proposals" / pid / "meta.json").read_text())["status"] == "applied"


def test_revert_refuses_a_diff_that_is_not_the_one_that_was_applied(notes, home, tmp_path):
    pid = apply_hint(notes, home, tmp_path)
    (home / "proposals" / pid / "diff.patch").write_text(make_diff(notes, {"skills/SKILL.md": "# hijacked\n"}))
    code, shown = human(home, ["revert", pid], pid.split("_")[-1])
    assert code == 2 and "proposal_modified" in shown
    assert (notes / "skills/SKILL.md").read_text() != "# hijacked\n"


def test_revert_refuses_on_a_broken_audit_chain(notes, home, tmp_path):
    pid = apply_hint(notes, home, tmp_path)
    p = notes / ".togi" / "audit.jsonl"
    p.write_text(p.read_text().replace('"lock"', '"apply"', 1))
    code, shown = human(home, ["revert", pid], pid.split("_")[-1])
    assert code == 2 and "audit_chain_broken" in shown and (notes / MSG).read_text() == good_message(notes)


def test_revert_reverifies_locked_hashes_after_writing_and_rolls_back(notes, home, tmp_path, monkeypatch):
    """Run in-process (no terminal) with the human step stubbed: the write is made to also move a locked file."""
    pid = apply_hint(notes, home, tmp_path)
    default_approver()                            # revert is a repo-changing gate: only the default approver passes it
    monkeypatch.delenv("TOGI_APPROVER_HOME")
    store = notes / "src/notes/store.py"
    orig_write = apply_mod.write_changes

    def write_and_tamper(repo, changes):
        orig_write(repo, changes)
        store.write_text(store.read_text() + "\n# moved by the write\n")

    import unittest.mock as mock

    with mock.patch.object(apply_mod.approvals, "human_gate"), \
            mock.patch.object(apply_mod.approvals, "confirm_and_authenticate", return_value="tester"), \
            mock.patch.object(apply_mod, "write_changes", write_and_tamper):
        with pytest.raises(Exception) as ex:
            apply_mod.revert_proposal(pid)
    assert ex.value.code == "locked_files_changed_by_write" and ex.value.status == "refused"
    assert (notes / MSG).read_text() == good_message(notes)                   # the revert itself was rolled back
    assert audit.read(notes)[-1]["action"] == "reject"
    assert json.loads((home / "proposals" / pid / "meta.json").read_text())["status"] == "applied"
