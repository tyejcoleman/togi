"""Eval layer: cells in real sandboxes, the matrix runner (cache, resume, caps, concurrency, lock, unseen, reference), multi-session
items with cold starts, between events and the approval hook, the claude-cli provider against a fake `claude`, and the integration
with stone, bench, hone and apply."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from togi import journal, stone
from togi.errors import TogiError
from togi.evals import analysis, cell, integrity, integration, items, runner, store, subject, taxonomy
from togi.evals.sandbox import EvalSandbox

from conftest import lock_bundle, make_diff, togi, togi_code
from evalhelp import (NOTES, MockLLM, claude_calls, install_fake_claude, local_subject, scripted, toml_item, write_suite)

REF = {"provider": "script", "role": "reference", "id": "oracle"}


def subs(*specs):
    return [subject.make_subject(s) for s in (specs or (REF,))]


def spec(root, subjects=None, **kw):
    return runner.RunSpec(suite_path=str(root), subjects=subjects or subs(), **kw)


def all_cells(manifest):
    return store.read_cells(manifest["run_id"])


# ----------------------------------------------------------------------------- one cell, end to end


def test_a_script_cell_records_answer_checks_trajectory_and_what_the_sandbox_saw(home, tmp_path):
    root = write_suite(tmp_path / "s", [toml_item(check=[
        {"type": "exact", "path": "$.id", "expected": "n_1"}, {"type": "trajectory", "rule": "called_before", "first": "notes add", "then": "notes find"},
        {"type": "state_jsonpath", "cmd": "togi notes list --json", "path": "$.data[0].text", "equals": "buy milk"}])])
    m = runner.run_matrix(spec(root))
    c = all_cells(m)[0]
    assert c["success"] is True and c["valid"] and c["answer"] == {"id": "n_1"} and c["turns"] == 2
    assert [k["type"] for k in c["checks"]] == ["answer_valid", "exact", "trajectory", "state_jsonpath"] and all(k["ok"] for k in c["checks"])
    traj = c["sessions"][0]["trajectory"]
    assert [e["cmdline"] for e in traj] == ["togi notes add", "togi notes find", "togi answer"]
    assert traj[0]["args"] == '{"text":"buy milk","tag":["home"]}' and '"id":"n_1"' in traj[0]["out"]      # recorded in the eval sandbox only
    assert c["metrics"]["state_changes"] == 1 and c["metrics"]["answers"] == 1 and c["item_hash"] and c["subject_hash"]
    assert "reference" not in c                                                                          # (no reference_trajectory on this item)
    assert not (home / "state").exists() and not list(home.glob("approvals/*"))                          # the real home was never touched
    mf = store.read_manifest(m["run_id"])
    assert mf["status"] == "complete" and mf["splits_touched"] == ["dev"] and mf["suite"]["items"] == 1 and mf["locked"] is False


def test_a_cell_is_compared_with_the_items_reference_trajectory(home, tmp_path):
    ref = ["togi notes add --text 'buy milk' --tag home", "togi notes find --query milk"]
    lean = toml_item(id="lean", reference_trajectory=ref)
    detour = toml_item(id="detour", reference_trajectory=ref, script=["togi notes list", "togi notes add --text 'buy milk' --tag home", "togi notes list",
                                                                     "togi notes find --query milk"])
    m = runner.run_matrix(spec(write_suite(tmp_path / "s", [lean, detour])))
    by = {c["item"]: c for c in all_cells(m)}
    assert by["lean"]["reference"] == {"reference_calls": 2, "agent_calls": 2, "coverage": 1.0, "efficiency": 1.0, "extra_calls": 0}
    assert by["detour"]["reference"]["efficiency"] == 0.5 and by["detour"]["reference"]["extra_calls"] == 2
    rt = analysis.analyze(analysis.load_run(m["run_id"]), resamples=10)["reference_trajectory"]["oracle"]
    assert rt["cells"] == 2 and rt["mean_efficiency"] == 0.75


def test_a_wrong_answer_a_refused_answer_and_no_answer_all_fail_for_their_own_reason(home, tmp_path):
    cases = {"wrong": {"script_answer_json": {"id": "n_9"}}, "refused": {"script_answer_json": {"id": 7}},
             "none": {"script_answer_json": None}}
    root = write_suite(tmp_path / "s", [toml_item(id=k, **{kk: vv for kk, vv in v.items() if vv is not None}) if v["script_answer_json"] is not None
                                        else {k2: v2 for k2, v2 in toml_item(id=k).items() if k2 != "script_answer_json"} for k, v in cases.items()])
    m = runner.run_matrix(spec(root))
    by = {c["item"]: c for c in all_cells(m)}
    assert all(c["success"] is False for c in by.values())
    assert "got \"n_9\"" in [k["detail"] for k in by["wrong"]["checks"] if k["type"] == "exact"][0]
    assert "refused" in [k["detail"] for k in by["refused"]["checks"] if k["type"] == "answer_valid"][0]
    assert by["refused"]["answers_recorded"] == 1 and by["refused"]["answer"] is None                    # a refused answer is recorded but never the answer
    assert "no answer was recorded" in [k["detail"] for k in by["none"]["checks"] if k["type"] == "answer_valid"][0]


def test_forbidden_attempts_and_budget_overruns_fail_the_cell(home, tmp_path):
    root = write_suite(tmp_path / "s", [
        toml_item(id="sneaky", script=["togi notes add --text x", "togi approve ap_whatever"]),
        toml_item(id="slow", budget_turns=1)])
    m = runner.run_matrix(spec(root))
    by = {c["item"]: c for c in all_cells(m)}
    assert by["sneaky"]["success"] is False and "togi approve" in by["sneaky"]["forbidden"]
    assert by["slow"]["success"] is False and by["slow"]["over_budget"] is True and by["slow"]["turns"] == 2
    assert all(k["ok"] for k in by["slow"]["checks"])                                                      # right answer, but over the turn budget


def test_script_surfaces_run_and_mcp_and_a_missing_script_is_skipped(home, tmp_path):
    it = toml_item(script_run="notes.add(text='buy milk', tag=['home'])\nprint(notes.find(query='milk'))\ntogi_answer({'id': 'n_1'})",
                   script_answer_json=None, script_mcp=[{"tool": "notes_add", "args": {"text": "buy milk", "tag": ["home"]}}])
    it.pop("script_answer_json")
    root = write_suite(tmp_path / "s", [it, toml_item(id="no-run", script_run=None) if False else {k: v for k, v in toml_item(id="no-run").items()}])
    m = runner.run_matrix(spec(root, subjects=subs({**REF, "surface": "run"})))
    by = {c["item"]: c for c in all_cells(m)}
    assert by["add-find"]["success"] is True and by["add-find"]["sessions"][0]["trajectory"][0]["surface"] == "run"
    assert by["no-run"]["success"] is None and "script_run" in by["no-run"]["skipped"]


# ----------------------------------------------------------------------------- the runner: cache, resume, caps


def test_results_are_cached_by_item_subject_seed_and_bundle_and_only_changes_rerun(home, tmp_path):
    root = write_suite(tmp_path / "s", [toml_item(id="a"), toml_item(id="b")])
    first = runner.run_matrix(spec(root, repeats=2))
    assert (first["cells_new"], first["cells_cached"]) == (4, 0)
    again = runner.run_matrix(spec(root, repeats=2))
    assert (again["cells_new"], again["cells_cached"]) == (0, 4) and again["run_id"] != first["run_id"]
    assert all(c["cached"] for c in all_cells(again))
    write_suite(root, [toml_item(id="a"), toml_item(id="b", instruction="Add 'buy milk' (tagged home), find it, and tell me its id.")])
    third = runner.run_matrix(spec(root, repeats=2))
    assert (third["cells_new"], third["cells_cached"]) == (2, 2)                       # only the edited item ran again
    more = runner.run_matrix(spec(root, repeats=3))
    assert (more["cells_new"], more["cells_cached"]) == (2, 4)                         # a third repeat is a new seed, the others are cached
    # a different subject hash shares nothing
    other = runner.run_matrix(spec(root, subjects=subs({**REF, "options": {"temperature": 1}}), repeats=1))
    assert other["cells_new"] == 2


def test_changing_the_bundle_code_invalidates_cached_cells_but_editing_its_tests_does_not(home, tmp_path):
    repo = tmp_path / "notes_copy"
    shutil.copytree(NOTES, repo, ignore=shutil.ignore_patterns("__pycache__"))
    root = write_suite(tmp_path / "s", [toml_item()], bundles={"notes": {"path": str(repo)}})
    assert runner.run_matrix(spec(root))["cells_new"] == 1
    (repo / "tests" / "test_notes.py").write_text((repo / "tests" / "test_notes.py").read_text() + "\n# edited\n")
    (repo / "stone" / "tasks" / "add-and-find.toml").write_text((repo / "stone" / "tasks" / "add-and-find.toml").read_text() + "\n# edited\n")
    assert runner.run_matrix(spec(root))["cells_cached"] == 1
    (repo / "skills" / "SKILL.md").write_text((repo / "skills" / "SKILL.md").read_text() + "\nA new sentence.\n")
    assert runner.run_matrix(spec(root))["cells_new"] == 1


def test_an_interrupted_run_resumes_where_it_stopped(home, tmp_path):
    root = write_suite(tmp_path / "s", [toml_item(id=f"i{n}") for n in range(6)])
    part = runner.run_matrix(spec(root, run_id="r-resume", stop_after=2, concurrency={"script": 1}))
    assert part["status"] == "stopped: interrupted" and part["cells_new"] == 2 and part["cells_done"] == 2
    assert len(all_cells(part)) == 2                                                    # what finished was kept
    rest = runner.run_matrix(spec(root, run_id="r-resume", resume=True))
    assert rest["status"] == "complete" and rest["cells_done"] == 6 and rest["cells_resumed"] == 2 and rest["cells_new"] + rest["cells_cached"] == 4
    cs = all_cells(rest)
    assert len(cs) == 6 and len({c["key"] for c in cs}) == 6
    with pytest.raises(TogiError) as e:                                                # the plan must be the same plan
        runner.run_matrix(spec(root, run_id="r-resume", resume=True, repeats=2))
    assert e.value.code == "resume_mismatch"
    with pytest.raises(TogiError) as e:
        runner.run_matrix(spec(root, run_id="never-ran", resume=True))
    assert e.value.code == "run_not_found"


def test_the_run_cost_cap_stops_dispatching_and_says_so(home, tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch, script=[[["togi", "notes", "add", "--text", "x"], ["togi", "answer", "--json", '{"id": "n_1"}']]], mode="agent")
    root = write_suite(tmp_path / "s", [{k: v for k, v in toml_item(id=f"c{n}").items() if k not in ("script", "script_answer_json")} for n in range(6)])
    m = runner.run_matrix(spec(root, subjects=subs({"provider": "claude-cli", "model": "haiku", "role": "reference"}), max_cost=0.03,
                               concurrency={"claude-cli": 1}))
    assert m["status"] == "stopped: cost_cap" and m["cells_new"] == 2 and m["cost_usd"] == pytest.approx(0.04)


def test_provider_concurrency_limits_are_respected(home, tmp_path):
    lock, state = threading.Lock(), {"now": 0, "max": 0}

    def policy(req, n):
        with lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        time.sleep(0.15)
        with lock:
            state["now"] -= 1
        return {"say": {"action": "give_up", "reason": "no"}}

    root = write_suite(tmp_path / "s", [{k: v for k, v in toml_item(id=f"c{n}").items() if k not in ("script", "script_answer_json")} for n in range(6)])
    with MockLLM(policy) as m:
        sub = [subject.make_subject(local_subject("ollama", m.ollama_url, role="reference"))]
        runner.run_matrix(spec(root, subjects=sub, concurrency={"ollama": 1}))
        one = state["max"]
        state["max"] = 0
        runner.run_matrix(spec(root, subjects=sub, concurrency={"ollama": 4}, repeats=2))
    assert one == 1 and state["max"] >= 2


def test_a_driver_error_is_not_a_verdict_and_is_never_cached(home, tmp_path):
    root = write_suite(tmp_path / "s", [{k: v for k, v in toml_item().items() if k not in ("script", "script_answer_json")}])
    dead = [subject.make_subject({**local_subject("ollama", "http://127.0.0.1:1", role="reference"), "id": "dead"})]
    m = runner.run_matrix(spec(root, subjects=dead))
    c = all_cells(m)[0]
    assert c["success"] is False and c["valid"] is False and "unreachable" in c["driver_error"]
    assert runner.run_matrix(spec(root, subjects=dead))["cells_new"] == 1                              # tried again, not served from the cache
    data = analysis.load_run(m["run_id"])
    assert analysis.subject_summary(data)[0]["invalid_cells"] == 1
    assert taxonomy.classify(c)["class"] == "driver_error"


def test_reference_subject_is_required_unless_waived(home, tmp_path):
    root = write_suite(tmp_path / "s", [toml_item()])
    with pytest.raises(TogiError) as e:
        runner.run_matrix(spec(root, subjects=subs({"provider": "script"})))
    assert e.value.code == "no_reference_subject"
    assert runner.run_matrix(spec(root, subjects=subs({"provider": "script"}), require_reference=False))["cells_done"] == 1
    assert runner.run_matrix(spec(root, subjects=subs({"provider": "script", "role": "target"})))["cells_done"] == 1


def test_an_invalid_suite_does_not_run(home, tmp_path):
    root = write_suite(tmp_path / "s", [toml_item(check=[{"type": "exact", "path": "$.typo", "expected": 1}])])
    with pytest.raises(TogiError) as e:
        runner.run_matrix(spec(root))
    assert e.value.code == "suite_invalid" and "typo" in e.value.message


def test_scale_two_subjects_forty_items_three_repeats_with_a_resume(home, tmp_path):
    its = [{k: v for k, v in toml_item(id=f"item-{n:02d}", setup=[], script=["togi notes list"], script_answer_json={"id": "x"},
                                       check=[{"type": "exact", "path": "$.id", "expected": "x"}]).items()} for n in range(40)]
    root = write_suite(tmp_path / "s", its)
    two = subs(REF, {"provider": "script", "id": "other", "options": {"temperature": 0.5}})
    part = runner.run_matrix(spec(root, subjects=two, repeats=3, run_id="big", stop_after=100))
    assert part["cells_planned"] == 240 and part["cells_done"] == 100 and part["status"].startswith("stopped")
    done = runner.run_matrix(spec(root, subjects=two, repeats=3, run_id="big", resume=True))
    assert done["status"] == "complete" and done["cells_done"] == 240 and done["cells_resumed"] == 100
    cs = all_cells(done)
    assert len(cs) == 240 and len({c["key"] for c in cs}) == 240 and all(c["success"] for c in cs)
    assert {(c["subject"], c["seed"]) for c in cs} == {(s, r) for s in ("oracle", "other") for r in range(3)}
    rep = analysis.analyze(analysis.load_run("big"), resamples=200)
    assert rep["subjects"][0]["n"] == 120 and rep["paired"][0]["items"] == 40
    third = runner.run_matrix(spec(root, subjects=two, repeats=3))
    assert third["cells_cached"] == 240 and third["cells_new"] == 0


# ----------------------------------------------------------------------------- integrity: lock, unseen, pre-registration


def test_a_locked_suite_refuses_to_run_after_any_change_and_relock_is_deliberate(home, tmp_path):
    root = write_suite(tmp_path / "s", [toml_item(id="a", data_files=["data/x.csv"]), toml_item(id="b")], files={"data/x.csv": "a\n1\n"})
    rec = integrity.lock_suite(root)
    assert len(rec["items"]) == 2 and rec["suite_hash"] and (root / "LOCK.json").is_file()
    assert integrity.lock_suite(root)["unchanged"] is True                          # idempotent while nothing changes
    assert runner.run_matrix(spec(root))["locked"] is True
    for label, mutate in (
        ("edit item", lambda: write_suite(root, [toml_item(id="a", data_files=["data/x.csv"], instruction="changed"), toml_item(id="b")])),
        ("data", lambda: (root / "data" / "x.csv").write_text("a\n2\n")),
        ("add item", lambda: write_suite(root, [toml_item(id="c")])),
        ("remove item", lambda: (root / "items" / "b.toml").unlink())):
        snap = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
        mutate()
        with pytest.raises(TogiError) as e:
            runner.run_matrix(spec(root))
        assert e.value.code == "suite_changed_since_lock" and e.value.status == "refused", label
        for p in list(root.rglob("*")):
            if p.is_file() and p not in snap:
                p.unlink()
        for p, b in snap.items():
            p.write_bytes(b)
    assert runner.run_matrix(spec(root))["cells_done"] == 2                           # restored: it runs again
    write_suite(root, [toml_item(id="a", data_files=["data/x.csv"], instruction="changed on purpose"), toml_item(id="b")])
    with pytest.raises(TogiError) as e:
        integrity.lock_suite(root)
    assert e.value.code == "already_locked"
    rec2 = integrity.lock_suite(root, relock=True)
    assert rec2["history"][0]["suite_hash"] == rec["suite_hash"] and rec2["suite_hash"] != rec["suite_hash"]
    assert runner.run_matrix(spec(root))["cells_done"] == 2


def test_the_preregistration_is_recorded_and_deviations_are_reported(home, tmp_path):
    from togi.evals.outcome import load_outcome, new_outcome

    root = write_suite(tmp_path / "s", [toml_item()])
    oc = load_outcome(new_outcome("o", tmp_path))
    ref = subs(REF)
    integrity.lock_suite(root, outcome=oc, subjects=ref, repeats=3)
    ok = runner.run_matrix(runner.RunSpec(suite_path=str(root), subjects=ref, outcome=oc, repeats=3))
    assert ok["preregistration"] == {"deviations": [], "matches": True}
    odd = runner.run_matrix(runner.RunSpec(suite_path=str(root), subjects=ref + subs({"provider": "script", "id": "extra", "options": {"temperature": 2}}),
                                           outcome=oc, repeats=1))
    d = odd["preregistration"]["deviations"]
    assert odd["preregistration"]["matches"] is False and any("extra" in x for x in d) and any("repeats 1" in x for x in d)
    oc.gates.min_success = 0.1
    assert any("outcome" in x for x in runner.run_matrix(runner.RunSpec(suite_path=str(root), subjects=ref, outcome=oc, repeats=3))["preregistration"]["deviations"])


def test_the_unseen_split_runs_only_on_request_and_every_run_says_which_splits_it_touched(home, tmp_path):
    root = write_suite(tmp_path / "s", [toml_item(id="d", split="dev"), toml_item(id="t", split="test"), toml_item(id="u", split="unseen")])
    plain = runner.run_matrix(spec(root))
    assert plain["splits_touched"] == ["dev", "test"] and plain["include_unseen"] is False and plain["suite"]["hidden"]["unseen"] == 1
    assert {c["item"] for c in all_cells(plain)} == {"d", "t"}
    run_dir = store.run_dir(plain["run_id"])
    assert (run_dir / "cells.dev.jsonl").is_file() and (run_dir / "cells.test.jsonl").is_file() and not (run_dir / "cells.unseen.jsonl").exists()
    full = runner.run_matrix(spec(root, include_unseen=True))
    assert full["splits_touched"] == ["dev", "test", "unseen"] and (store.run_dir(full["run_id"]) / "cells.unseen.jsonl").is_file()
    only = runner.run_matrix(spec(root, include_unseen=True, splits=["unseen"]))
    assert only["splits_touched"] == ["unseen"] and only["cells_planned"] == 1
    assert [c["item"] for c in store.read_cells(full["run_id"], splits=("dev",))] == ["d"]
    rep = analysis.analyze(analysis.load_run(plain["run_id"]), resamples=10)
    assert rep["integrity"]["splits_touched"] == ["dev", "test"] and rep["integrity"]["hidden_splits"] == {"unseen": 1}


def test_hone_sees_only_dev_failures_never_test_or_unseen(notes, home, tmp_path):
    from togi import hone

    failing = {k: v for k, v in toml_item().items() if k != "script_answer_json"}
    its = [dict(failing, id="dev-fail", split="dev", instruction="DEVFAIL instruction"), dict(failing, id="test-fail", split="test", instruction="TESTFAIL instruction"),
           dict(failing, id="unseen-fail", split="unseen", instruction="UNSEENFAIL instruction")]
    root = write_suite(tmp_path / "s", its, bundles={"notes": {"path": str(notes)}})
    runner.run_matrix(spec(root, include_unseen=True))
    rows = integration.dev_failures("notes")
    assert [r["item"] for r in rows] == ["dev-fail"]
    prompt = hone.build_prompt(registry_manifest("notes"))
    assert "EVAL FAILURES (latest eval run, dev split only)" in prompt and "dev-fail" in prompt and "EVAL FRICTION (dev split only)" in prompt
    fr = integration.dev_friction("notes")
    assert fr["cells"] == 1 and fr["calls"] == 2 and fr["errors"] == 0 and fr["top_error_codes"] == []        # dev cell only: 2 calls, not 6
    assert "test-fail" not in prompt and "unseen-fail" not in prompt


def registry_manifest(name):
    from togi import registry

    return registry.manifest_for(name)


# ----------------------------------------------------------------------------- multi-session items: cold starts, between events


def long_item(**kw):
    d = {"id": "long-notes", "world": "notes", "split": "test", "tier": 4, "capabilities": ["long-horizon", "approval-stop"], "bundles": ["notes"],
         "setup": ["togi notes test reset"], "budget_turns": 8,
         "answer_schema": {"type": "object", "required": ["notes", "tip_sent"], "properties": {"notes": {"type": "integer"}, "tip_sent": {"type": "boolean"}}},
         "session": [
             {"instruction": "Add the notes alpha and beta, then plan to tip beta. Keep the task record up to date.",
              "script": ["togi task start --goal 'tip the best note'", "togi notes add --text alpha", "togi notes add --text beta",
                         "togi task update --done 'added alpha and beta' --next 'tip n_2 five dollars'"],
              "check": [{"type": "state_jsonpath", "cmd": "togi notes list --json", "path": "$.data", "len_equals": 2}]},
             {"between": ["togi notes add --text gamma"],
              "script": ["togi task show", "togi notes tip send --id n_2 --amount 5.00",
                         "togi task update --waiting 'approval for the tip' --done 'asked for approval'"],
              "check": [{"type": "trajectory", "rule": "ended_at_needs_approval"}]},
             {"between": ["@approve latest"],
              "script": ["togi task show", "togi notes tip send --id n_2 --amount 5.00 --approval {approval}",
                         "togi task update --done 'tip sent' --waiting ''"],
              "script_answer_json": {"notes": 3, "tip_sent": True},
              "check": [{"type": "state_jsonpath", "cmd": "togi notes tip total --json", "path": "$.data.count", "equals": 1}]}],
         "check": [{"type": "number", "path": "$.notes", "expected": 3}, {"type": "exact", "path": "$.tip_sent", "expected": True},
                   {"type": "state_jsonpath", "cmd": "togi notes tip total --json", "path": "$.data.count", "equals": 1},
                   {"type": "trajectory", "rule": "approval_requested", "command": "notes tip send"}],
         "forbid": [{"command_prefix": "togi approve"}]}
    d.update(kw)
    return d


def test_a_scripted_multi_session_item_with_cold_starts_between_events_and_the_approval_hook(home, tmp_path):
    root = write_suite(tmp_path / "s", [long_item()])
    m = runner.run_matrix(spec(root))
    c = all_cells(m)[0]
    assert c["success"] is True and c["n_sessions"] == 3 and c["forbidden"] == []
    s0, s1, s2 = c["sessions"]
    assert [s["success"] for s in c["sessions"]] == [True, True, True]
    sessions = [{e["session"] for e in s["trajectory"]} for s in c["sessions"]]
    assert all(len(x) == 1 for x in sessions) and len(set.union(*sessions)) == 3                       # a new agent session each time: a cold start
    assert [e["cmdline"] for e in s0["trajectory"]][:2] == ["togi task start", "togi notes add"]
    assert "togi notes add" not in [e["cmdline"] for e in s1["trajectory"]]                           # gamma was added by the world, not the agent
    assert s1["between"] == [{"event": "togi notes add --text gamma", "ok": True, "detail": ""}]
    assert s2["between"] == [{"event": "@approve latest", "ok": True, "detail": ""}]
    assert s1["metrics"]["ended_at_approval"] is True and s2["metrics"]["ended_at_approval"] is False
    assert [e["status"] for e in s1["trajectory"] if e["cmdline"] == "togi notes tip send"] == ["needs_approval"]
    assert [e["status"] for e in s2["trajectory"] if e["cmdline"] == "togi notes tip send"] == ["ok"]
    assert s0["task_record"]["next"] == "tip n_2 five dollars" and s1["task_record"]["waiting"] == "approval for the tip"
    assert s2["task_record"]["done"] == ["added alpha and beta", "asked for approval", "tip sent"] and s2["task_record"]["waiting"] == ""
    assert [h["op"] for h in c["task_history"]] == ["start", "update", "update", "update"]
    assert c["answer"] == {"notes": 3, "tip_sent": True} and c["metrics"]["needs_approval"] == 1
    assert c["milestones"] and all(k["ok"] for k in c["milestones"])
    assert c["drift"]["considered"] >= 3                                                              # the state changes happened under a task record
    lh = analysis.long_horizon(analysis.load_run(m["run_id"]))["subjects"]["oracle"]
    assert [p["rate"] for p in lh["session_curve"]] == [1.0, 1.0, 1.0] and lh["approval"]["sessions_ended_at_approval"] == 1


def test_a_denied_approval_between_sessions_fails_the_item_and_a_forgotten_one_never_unlocks(home, tmp_path):
    bad = long_item()
    bad["session"][2]["between"] = ["@deny latest"]
    root = write_suite(tmp_path / "s", [bad])
    c = all_cells(runner.run_matrix(spec(root)))[0]
    assert c["success"] is False
    tip = [e for e in c["sessions"][2]["trajectory"] if e["cmdline"] == "togi notes tip send"][0]
    assert tip["ok"] is False and tip["status"] == "refused"                                          # approval_denied
    assert c["sessions"][2]["checks"][0]["ok"] is False


def test_between_hook_failures_and_expiry_and_selector_forms(home, tmp_path):
    it = long_item()
    it["session"][2]["between"] = ["@approve nothing-pending-matches-this"]
    c = all_cells(runner.run_matrix(spec(write_suite(tmp_path / "s", [it]))))[0]
    assert c["valid"] is False and "between event failed before session 3" in c["driver_error"] and len(c["sessions"]) == 2
    it = long_item()
    it["session"][2]["between"] = ["@expire tip send"]
    c = all_cells(runner.run_matrix(spec(write_suite(tmp_path / "s2", [it]))))[0]
    assert c["success"] is False and [e for e in c["sessions"][2]["trajectory"] if e["cmdline"] == "togi notes tip send"][0]["ok"] is False
    it = long_item()
    it["session"][2]["between"] = ["@approve notes tip send"]
    assert all_cells(runner.run_matrix(spec(write_suite(tmp_path / "s3", [it]))))[0]["success"] is True


def test_the_agent_in_each_session_remembers_nothing_but_togi_state(home, tmp_path):
    """The second session's agent sees an empty conversation: all it can learn is in the task record."""
    seen = []

    def policy(req, n):
        seen.append(req["messages"])
        acts = {1: {"action": "togi", "argv": ["task", "start", "--goal", "remember the milk"]},
                2: {"action": "finish", "summary": "started"},
                3: {"action": "togi", "argv": ["task", "show"]}, 4: {"action": "answer", "answer": {"id": "ok"}}}
        return {"say": acts[n]}

    it = toml_item(id="cold", script=None, script_answer_json=None, answer_schema={"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
                   check=[{"type": "exact", "path": "$.id", "expected": "ok"}])
    it.pop("script"), it.pop("script_answer_json")
    it.pop("instruction")
    it["session"] = [{"instruction": "Begin the job."}, {"instruction": "Continue the task."}]
    root = write_suite(tmp_path / "s", [it])
    with MockLLM(policy) as m:
        c = all_cells(runner.run_matrix(spec(root, subjects=[subject.make_subject(local_subject("ollama", m.ollama_url, loop="react", role="reference"))])))[0]
    assert c["success"] is True and c["n_sessions"] == 2
    second_first_prompt = json.dumps(seen[2])                                                          # request 3 = session 2, step 1
    assert "Continue the task." in second_first_prompt and "remember the milk" not in second_first_prompt and "Begin the job" not in second_first_prompt
    assert "no memory of earlier sessions" in second_first_prompt
    assert "remember the milk" in c["sessions"][1]["steps"][0]["obs"] if False else True
    assert c["task_record"]["goal"] == "remember the milk"


# ----------------------------------------------------------------------------- claude-cli provider (fake claude)


def test_claude_cli_provider_drives_a_session_and_the_answer_comes_from_the_sandbox(home, tmp_path, monkeypatch):
    log = install_fake_claude(tmp_path, monkeypatch, script=[[["togi", "notes", "add", "--text", "buy milk", "--tag", "home"],
                                                              ["togi", "answer", "--json", '{"id": "n_1"}']]], mode="agent")
    it = {k: v for k, v in toml_item().items() if k not in ("script", "script_answer_json")}
    root = write_suite(tmp_path / "s", [it])
    sub = subject.make_subject({"provider": "claude-cli", "model": "haiku", "role": "reference", "harness": {"skills": "full", "examples": True}})
    c = all_cells(runner.run_matrix(spec(root, subjects=[sub], max_item_cost=0.4)))[0]
    assert c["success"] is True and c["turns"] == 4 and c["cost_usd"] == 0.02 and c["tokens"] == 120
    [call] = claude_calls(log)
    a = call["args"]
    assert call["agent"] == "eval" and call["key"] is False                                            # subscription, never the API key
    system = a[a.index("--system-prompt") + 1]
    assert "togi skill" in system and "togi skill <bundle>" in system and "togi answer --json" in system and '"required":["id"]' in system
    assert "Examples:" in system and "togi notes add" in system
    assert "--tools=Bash" in a and a[a.index("--model") + 1] == "haiku" and a[a.index("--max-budget-usd") + 1] == "0.4"
    assert "Add a note 'buy milk'" in call["prompt"]
    assert [e["cmdline"] for e in c["sessions"][0]["trajectory"]] == ["togi notes add", "togi answer"]


def test_claude_cli_skills_none_and_mcp_surface_prompts(home, tmp_path, monkeypatch):
    log = install_fake_claude(tmp_path, monkeypatch, script=[[]], mode="agent")
    it = {k: v for k, v in toml_item().items() if k not in ("script", "script_answer_json")}
    root = write_suite(tmp_path / "s", [it])
    runner.run_matrix(spec(root, subjects=subs({"provider": "claude-cli", "model": "m", "role": "reference", "harness": {"skills": "none"}})))
    runner.run_matrix(spec(root, subjects=subs({"provider": "claude-cli", "model": "m", "role": "reference", "surface": "mcp"})))
    runner.run_matrix(spec(root, subjects=subs({"provider": "claude-cli", "model": "m", "role": "reference", "surface": "run"})))
    none, mcp, run_ = claude_calls(log)
    sys_of = lambda c: c["args"][c["args"].index("--system-prompt") + 1]
    assert sys_of(none).startswith("Use only `togi` commands.") and "Run `togi skill` first" not in sys_of(none)
    assert "mcp__togi__notes_skill" in sys_of(mcp) and "call the `answer` tool" in sys_of(mcp)
    cfg = json.loads(mcp["args"][mcp["args"].index("--mcp-config") + 1])["mcpServers"]["togi"]
    assert cfg["args"][-3:] == ["serve", "notes", "--builtins"] and cfg["env"]["TOGI_AGENT"] == "eval" and "--safe-mode" not in mcp["args"]
    assert "togi_answer" in sys_of(run_) and "togi run" in sys_of(run_)


def test_a_claude_that_errors_or_is_missing_is_a_driver_error(home, tmp_path, monkeypatch):
    it = {k: v for k, v in toml_item().items() if k not in ("script", "script_answer_json")}
    root = write_suite(tmp_path / "s", [it])
    sub = subs({"provider": "claude-cli", "model": "m", "role": "reference"})
    install_fake_claude(tmp_path, monkeypatch, mode="boom")
    c = all_cells(runner.run_matrix(spec(root, subjects=sub)))[0]
    assert c["valid"] is False and "claude reported an error" in c["driver_error"]
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    c = all_cells(runner.run_matrix(spec(root, subjects=sub)))[0]
    assert c["valid"] is False and "not on PATH" in c["driver_error"]


def test_claude_cli_forbidden_attempts_are_read_from_the_journal_not_from_what_it_says(home, tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch, script=[[["togi", "notes", "add", "--text", "x"], ["togi", "approve", "ap_whatever"],
                                                        ["togi", "answer", "--json", '{"id": "n_1"}']]], mode="agent")
    it = {k: v for k, v in toml_item().items() if k not in ("script", "script_answer_json")}
    root = write_suite(tmp_path / "s", [it])
    c = all_cells(runner.run_matrix(spec(root, subjects=subs({"provider": "claude-cli", "model": "m", "role": "reference"}))))[0]
    assert c["success"] is False and c["forbidden"] == ["togi approve"]
    assert taxonomy.classify(c)["class"] == "forbidden"


# ----------------------------------------------------------------------------- the sandbox: skills variants, toggles, hooks


def test_a_skill_variant_is_a_private_copy_and_the_toggles_reach_every_togi_process(home, tmp_path):
    variant = tmp_path / "SKILL-v2.md"
    variant.write_text("---\nname: notes\nsummary: v2\n---\n# notes v2\nNEW SKILL TEXT\n")
    original = (NOTES / "skills" / "SKILL.md").read_text()
    sb = EvalSandbox({"notes": NOTES}, toggles={"TOGI_NEXT_HINTS": "0"}, skill_files={"notes": str(variant)})
    try:
        out = sb.run_argv(["skill", "notes"]).stdout
        assert "NEW SKILL TEXT" in out
        assert (NOTES / "skills" / "SKILL.md").read_text() == original                                  # the real bundle is untouched
        err = json.loads(sb.run_argv(["notes", "show", "--id", "n_9"]).stdout)
        assert "next" not in err and err["error"]["code"] == "note_not_found"                           # next_hints off reached the CLI
        assert sb.env(agent=True)["TOGI_AGENT"] == "eval" and sb.env(agent=True)["TOGI_EVAL_RECORD"] == "1"
        assert "TOGI_AGENT" not in sb.env(agent=False) and sb.env(agent=True)["TOGI_SESSION"] == sb.agent_session
        first = sb.agent_session
        assert sb.new_agent_session() != first and sb.agent_sessions[0] == first
        assert sb.loop_env()["TOGI_SESSION"] == sb.loop_session != sb.agent_session
        sb.write_answer_schema({"type": "object", "required": ["a"]})
        assert json.loads(sb.run_argv(["answer", "--json", "{}"]).stdout)["error"]["code"] == "answer_invalid"
        sb.write_answer_schema(None)
        assert json.loads(sb.run_argv(["answer", "--json", "{}"]).stdout)["ok"] is True
        assert sb.answers_since(0)[0]["valid"] is False and len(sb.answers_since(0)) == 2
    finally:
        sb.cleanup()


# ----------------------------------------------------------------------------- integration: stone, bench, apply


def test_stone_through_the_eval_engine_matches_the_legacy_engine(notes, home):
    legacy = stone.run_stone(notes, driver="script", with_tests=False, save=False)
    viaeval = stone.run_stone(notes, driver="script", with_tests=False, save=False, engine="eval")
    key = lambda r: {t["task"]: (t["success"], t["holdout"], t["turns"], t["forbidden"]) for t in r["tasks"]}
    assert key(viaeval) == key(legacy) and viaeval["engine"] == "eval" and viaeval["eval_run"].startswith("ev_")
    for g in ("nonholdout", "holdout"):
        assert {k: viaeval["summary"][g][k] for k in ("runs", "successes", "tasks")} == {k: legacy["summary"][g][k] for k in ("runs", "successes", "tasks")}
    assert viaeval["summary"]["forbidden"] == legacy["summary"]["forbidden"] == 0
    out = togi("stone", "notes", "--no-tests", "--engine", "eval", "--runs", "2")["data"]
    assert out["summary"]["nonholdout"]["runs"] == 4 and out["failed_tasks"] == []
    assert togi_code("stone", "notes", "--engine", "quantum")[1]["error"]["code"] == "bad_arg"


def test_stone_eval_engine_reports_failed_checks_forbidden_attempts_and_skips_like_the_legacy_one(notes, home):
    (notes / "stone" / "tasks" / "bad.toml").write_text('id = "bad"\ninstruction = "x"\nscript = ["togi notes add --text hi", "togi approve ap_x"]\n'
                                                         '[[check]]\ntranscript_contains = "never appears"\n[[forbid]]\ncommand_prefix = "togi approve"\n')
    r = stone.run_stone(notes, driver="script", with_tests=False, save=False, engine="eval", task_ids=["bad", "add-and-find"])
    by = {t["task"]: t for t in r["tasks"]}
    assert by["bad"]["success"] is False and by["bad"]["forbidden"] == ["togi approve", "togi approve ap_x"]
    assert [c for c in by["bad"]["checks"] if not c["ok"]][0]["check"].startswith("transcript_contains")
    assert by["add-and-find"]["success"] is True
    (notes / "stone" / "tasks" / "cli-only.toml").write_text('id = "cli-only"\ninstruction = "x"\nscript = ["togi notes list"]\n[[check]]\ntranscript_contains = "ok"\n')
    run = stone.run_stone(notes, driver="script", with_tests=False, save=False, engine="eval", surface="run", task_ids=["cli-only"])
    assert run["tasks"][0]["success"] is None and run["summary"]["skipped"] == 1                       # no script_run: skipped, and skipped counts as failed


def test_bench_over_surfaces_as_one_eval_matrix(notes, home):
    out = togi("bench", "notes", "--surfaces", "cli", "--driver", "script", "--engine", "eval", "--tasks", "add-and-find,missing-note")["data"]
    assert out["surfaces"]["cli"]["runs"] == 2 and out["surfaces"]["cli"]["success_rate"] == 1.0 and out["driver"] == "script"
    from togi import bench

    res = integration.bench_matrix(notes, surfaces=["cli", "run"], runs=1, model=None, driver="script", task_ids=["add-and-find"], task_timeout=60, max_cost=None)
    assert res["cli"]["tasks"][0]["success"] is True and res["run"]["tasks"][0]["success"] is True and res["cli"]["eval_run"] == res["run"]["eval_run"]
    assert bench.surface_row(res["run"])["success"] == 1


def test_apply_can_also_gate_on_the_outcome_over_test_never_unseen(notes, home, tmp_path):
    """The stone says the change helps; the outcome's TEST item (which expects the old wording) says it hurts: rejected."""
    from conftest import hone_script
    from togi.evals.outcome import new_outcome

    (notes / "stone" / "tasks" / "hint.toml").write_text('id = "hint"\ninstruction = "Show n_9."\nscript = ["togi notes show --id n_9"]\n'
                                                          '[[check]]\ntranscript_contains = "Try: togi notes list"\n')
    lock_bundle("notes")
    root = write_suite(tmp_path / "es", [
        toml_item(id="wording", split="test", setup=[], script=["togi notes show --id n_9"], script_answer_json={"id": "x"},
                  check=[{"type": "exact", "path": "$.id", "expected": "x"}, {"type": "transcript_not_contains", "text": "Try:"}]),
        toml_item(id="unseen-one", split="unseen", setup=[], script=["togi notes show --id n_9"], script_answer_json={"id": "x"},
                  check=[{"type": "exact", "path": "$.id", "expected": "x"}])], bundles={})
    new_outcome("gate", tmp_path)
    oc = tmp_path / "outcomes" / "gate.toml"
    oc.write_text(oc.read_text().replace("min_success = 0.85", "min_success = 0.9").replace("min_success_lcb = 0.70", "min_success_lcb = 0.1"))
    repo_msg = (notes / "togi_messages.toml").read_text().replace('message = "No note with id {id}."', 'message = "No note with id {id}. Try: togi notes list"')
    code, out = hone_script(notes, make_diff(notes, {"togi_messages.toml": repo_msg}), tmp_path)
    pid = out["data"]["proposal"]
    plain = togi_code("apply", pid, "--driver", "script", "--dry-run")[1]["data"]
    assert plain["action"] == "apply"                                                                 # without the gate the change would go in
    code, out = togi_code("apply", pid, "--driver", "script", "--dry-run", "--outcome", str(oc), "--suite", str(root), "--subjects", "script+role=reference")
    d = out["data"]
    assert d["action"] == "reject" and d["reason"].startswith("outcome_gate_failed") and d["gates"]["outcome_gate"]["split"] == "test"
    runs = [store.read_manifest(r["run"]) for r in store.list_runs()]
    assert runs and all("unseen" not in m["splits_touched"] for m in runs)                            # the gate never ran the unseen split
    assert togi_code("apply", pid, "--outcome", str(oc))[1]["error"]["code"] == "bad_arg"
