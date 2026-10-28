"""Eval layer: outcomes, items and suites (including the stone import and the unseen access control), the check library, subjects."""
from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from togi.errors import TogiError
from togi.evals import checks as C
from togi.evals import integrity, items, outcome, subject, tomlw

from conftest import togi, togi_code
from evalhelp import NOTES, toml_item, write_suite


# ----------------------------------------------------------------------------- toml writer


def test_toml_writer_round_trips_items_tables_and_awkward_strings():
    d = {"id": "x", "n": 3, "f": 1.5, "b": True, "q": 'say "hi"\nnew line é', "list": [1, 2], "nested": {"a": {"b": "c"}, "k y": 1},
         "check": [{"type": "exact", "path": "$.a", "expected": [1, "x"]}, {"type": "number", "expected": 2.5}], "dropped": None,
         "session": [{"instruction": "i", "check": [{"type": "used"}]}], "empty": {}}
    back = tomllib.loads(tomlw.dumps(d))
    d.pop("dropped")
    assert back == d


# ----------------------------------------------------------------------------- outcome


def test_outcome_template_parses_and_hashes_its_gates(tmp_path):
    p = outcome.new_outcome("narrow-job", tmp_path)
    oc = outcome.load_outcome(p)
    assert oc.name == "narrow-job" and oc.splits == ["test", "unseen"] and oc.gates.min_success_lcb == 0.70
    assert oc.target_ids() == ["sonnet"] and [r["bar"] for r in oc.references] == ["target", "fallback"]
    h = oc.hash
    oc.gates.min_success = 0.5
    assert oc.hash != h
    with pytest.raises(TogiError) as e:
        outcome.new_outcome("narrow-job", tmp_path)
    assert e.value.code == "outcome_exists"


@pytest.mark.parametrize("bad", ['name = "x y"', 'name = "x"\nsplits = ["nope"]', 'name = "x"\n[gates]\nmin_success = 2',
                                 'name = "x"\n[gates.non_inferior]\nmargin = 0.1', 'name = "x"\n[[reference]]\nsubject = "a"\nbar = "best"',
                                 'name = "x"\nprimary_metric = "vibes"'])
def test_outcome_rejects_nonsense(tmp_path, bad):
    f = tmp_path / "o.toml"
    f.write_text(bad)
    with pytest.raises(TogiError) as e:
        outcome.load_outcome(f)
    assert e.value.code == "outcome_invalid"


# ----------------------------------------------------------------------------- items


def test_item_parses_a_multi_session_task_with_between_events_and_checks():
    raw = {"id": "long", "world": "w", "split": "test", "tier": 4, "capabilities": ["long-horizon"], "bundles": ["notes"],
           "answer_schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
           "session": [{"instruction": "start", "check": [{"type": "trajectory", "rule": "used", "command": "notes add"}]},
                       {"between": ["togi notes add --text x", "@approve latest"], "script": ["togi task show"]}],
           "check": [{"type": "number", "path": "$.n", "expected": 3}]}
    it = items.parse_item(raw)
    assert it.multi_session and [s.index for s in it.sessions] == [0, 1]
    assert it.sessions[1].instruction == items.CONTINUE_TEXT and it.sessions[1].between == ["togi notes add --text x", "@approve latest"]
    assert it.sessions[0].checks[0]["rule"] == "used" and it.checks[0]["expected"] == 3
    assert not items.validate_item(it)
    # a single-session item keeps its checks at the item level
    one = items.parse_item(toml_item())
    assert not one.multi_session and len(one.checks) == 1 and one.sessions[0].checks == []


def test_a_stone_task_is_a_valid_item_file_and_checks_convert_to_typed_ones():
    stone_task = {"id": "tip-needs-approval", "holdout": True, "instruction": "Send a tip.", "setup": ["togi notes test reset"],
                  "budget_turns": 8, "script": ["togi notes tip send --id n_1 --amount 5"],
                  "check": [{"cmd": "togi notes tip total --json", "expect_jsonpath": "$.data.count", "equals": 0},
                            {"transcript_contains": "needs_approval"}, {"cmd": "togi version", "exit_code": 0}],
                  "forbid": [{"command_prefix": "togi approve"}]}
    it = items.parse_item(stone_task, default_bundles=["notes"])
    assert it.legacy and it.split == "test" and it.meta["holdout"] is True and it.bundles == ["notes"]
    assert [c["type"] for c in it.checks] == ["state_jsonpath", "transcript_contains", "state_jsonpath"]
    assert it.checks[0]["path"] == "$.data.count" and it.checks[0]["equals"] == 0 and it.checks[2]["exit_code"] == 0
    assert it.forbids == ["togi approve"] and it.budget_turns == 8
    assert items.parse_item({**stone_task, "holdout": False}).split == "dev"
    assert not items.validate_item(it)


def test_import_stone_writes_a_loadable_suite_with_holdout_as_test(notes, home, tmp_path):
    out = togi("eval", "suite", "import-stone", "notes", "--out", str(tmp_path / "ns"))["data"]
    assert out["items"] == 3 and sorted(out["ids"]) == ["add-and-find", "missing-note", "tip-needs-approval"]
    suite = items.load_suite(tmp_path / "ns", purpose="lock")
    by = {i.id: i for i in suite.items}
    assert by["tip-needs-approval"].split == "test" and by["add-and-find"].split == "dev"
    assert by["add-and-find"].sessions[0].script_run and by["add-and-find"].sessions[0].script_mcp
    assert not items.validate_suite(suite)
    assert suite.resolve_bundle("notes") == NOTES.resolve() or suite.resolve_bundle("notes").name == "notes_repo"


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d.update(check=[]), "no checks"),
    (lambda d: d.pop("answer_schema"), "needs the item to declare an answer_schema"),
    (lambda d: d.update(check=[{"type": "exact", "path": "$.nope", "expected": 1}]), "not a field of the answer_schema"),
    (lambda d: d.update(check=[{"type": "mystery"}]), "unknown check type"),
    (lambda d: d.update(check=[{"type": "number", "path": "$.id", "expected": "lots"}]), "must be a number"),
    (lambda d: d.update(check=[{"type": "regex", "path": "$.id", "pattern": "("}]), "not a valid regular expression"),
    (lambda d: d.update(check=[{"type": "trajectory", "rule": "called_before", "first": "a"}]), "needs `then`"),
    (lambda d: d.update(check=[{"type": "judge", "rubric": "good"}]), "calibration"),
    (lambda d: d.update(answer_schema={"type": "array"}), "must describe an object"),
    (lambda d: d.update(split="secret"), "split must be one of"),
    (lambda d: d.update(tier=9), "tier must be 1..5"),
    (lambda d: d.update(id="bad id!"), "must be letters"),
])
def test_validate_item_catches_faulty_items(mutate, needle):
    d = toml_item()
    mutate(d)
    problems = items.validate_item(items.parse_item(d, default_bundles=["notes"]))
    assert any(needle in p for p in problems), problems


def test_item_hash_covers_what_the_item_measures_and_the_data_files(tmp_path):
    root = write_suite(tmp_path / "s", [toml_item(data_files=["data/a.csv"])], files={"data/a.csv": "x,y\n1,2\n"})
    base = items.load_suite(root).items[0]
    h = base.hash(root)
    assert h == items.load_suite(root).items[0].hash(root) and len(h) == 64
    assert base.hash(tmp_path / "elsewhere") != h                         # (a missing data file hashes differently)
    (root / "data" / "a.csv").write_text("x,y\n1,3\n")
    assert items.load_suite(root).items[0].hash(root) != h               # the data the item loads is part of it
    (root / "data" / "a.csv").write_text("x,y\n1,2\n")
    for key, val in (("instruction", "changed"), ("check", [{"type": "exact", "path": "$.id", "expected": "n_2"}]), ("tier", 3)):
        write_suite(tmp_path / "t", [toml_item(data_files=["data/a.csv"], **{key: val})], files={"data/a.csv": "x,y\n1,2\n"})
        assert items.load_suite(tmp_path / "t").items[0].hash(tmp_path / "t") != h, key


def test_unseen_files_are_not_opened_unless_a_run_asks(tmp_path):
    root = write_suite(tmp_path / "s", [toml_item(id="d1", split="dev"), toml_item(id="t1", split="test"), toml_item(id="u1", split="unseen")])
    (root / "items" / "unseen" / "broken.toml").write_text("this is [not toml")        # proof: nothing opens this file for most purposes
    for purpose in ("run", "hone", "examples", "skills", "apply"):
        s = items.load_suite(root, purpose=purpose)
        assert "u1" not in {i.id for i in s.items} and s.hidden["unseen"] == 2
    assert {i.id for i in items.load_suite(root, purpose="hone").items} == {"d1"}
    assert {i.id for i in items.load_suite(root, purpose="apply").items} == {"t1"}
    assert {i.id for i in items.load_suite(root, purpose="run").items} == {"d1", "t1"}
    with pytest.raises(TogiError):
        items.load_suite(root, purpose="run", include_unseen=True)                     # asking for unseen opens (and chokes on) the file
    (root / "items" / "unseen" / "broken.toml").unlink()
    s = items.load_suite(root, purpose="run", include_unseen=True)
    assert {i.id for i in s.items} == {"d1", "t1", "u1"} and "unseen" not in s.hidden and "unseen" in s.splits_loaded
    assert {i.id for i in items.load_suite(root, purpose="contamination").items} == {"d1", "t1", "u1"}
    with pytest.raises(TogiError) as e:
        integrity.guard_splits({"dev", "unseen"}, "hone")
    assert e.value.code == "split_access_denied"
    integrity.guard_splits({"dev", "test"}, "run")


def test_a_split_misfiled_outside_the_unseen_directory_is_still_dropped(tmp_path):
    root = write_suite(tmp_path / "s", [toml_item(id="d1"), toml_item(id="sneaky", split="unseen")])
    (root / "items" / "sneaky.toml").write_text((root / "items" / "unseen" / "sneaky.toml").read_text())
    (root / "items" / "unseen" / "sneaky.toml").unlink()
    s = items.load_suite(root, purpose="hone")
    assert {i.id for i in s.items} == {"d1"} and s.hidden["unseen"] == 1


# ----------------------------------------------------------------------------- checks


def ctx(answer=None, traj=None, **kw):
    return C.CheckContext(answer=answer, answers=[{"valid": True, "answer": answer}] if answer is not None else [],
                          trajectory=traj or [], **kw)


def ok(check, c):
    return C.evaluate(check, c)["ok"]


def test_exact_normalises_case_and_spacing_but_not_types():
    c = ctx({"name": "  North  East ", "n": 1, "b": True})
    assert ok({"type": "exact", "path": "$.name", "expected": "north east"}, c)
    assert not ok({"type": "exact", "path": "$.name", "expected": "north", }, c)
    assert ok({"type": "exact", "path": "$.name", "expected": "North East", "ignore_case": False}, c)
    assert not ok({"type": "exact", "path": "$.name", "expected": "north east", "ignore_case": False}, c)
    assert ok({"type": "exact", "path": "$.n", "expected": 1.0}, c) and not ok({"type": "exact", "path": "$.b", "expected": 1}, c)
    r = C.evaluate({"type": "exact", "path": "$.missing", "expected": 1}, c)
    assert r["ok"] is False and "no value at $.missing" in r["detail"]
    assert not ok({"type": "exact", "path": "$.n", "expected": 1}, ctx(None))


def test_number_check_reads_formatted_numbers_so_47_248_cannot_be_a_false_fail():
    for v in (47248, 47248.0, "47248", "47,248", "$47,248.00", " 47 248 ".replace(" ", ""), "(47,248)".replace("(", "").replace(")", "")):
        assert ok({"type": "number", "path": "$.v", "expected": 47248}, ctx({"v": v})), v
    assert not ok({"type": "number", "path": "$.v", "expected": 47248}, ctx({"v": 47284}))
    assert not ok({"type": "number", "path": "$.v", "expected": 1}, ctx({"v": "many"}))
    assert not ok({"type": "number", "path": "$.v", "expected": 1}, ctx({"v": True}))
    assert ok({"type": "number", "path": "$.v", "expected": 10.0, "abs_tol": 0.01}, ctx({"v": 10.004}))
    assert not ok({"type": "number", "path": "$.v", "expected": 10.0, "abs_tol": 0.001}, ctx({"v": 10.004}))
    assert ok({"type": "number", "path": "$.v", "expected": 1000, "rel_tol": 0.01}, ctx({"v": 1009}))
    assert not ok({"type": "number", "path": "$.v", "expected": 1000, "rel_tol": 0.001}, ctx({"v": 1009}))
    assert ok({"type": "number", "path": "$.v", "expected": -5}, ctx({"v": "(5)"}))              # accountancy negative


def test_regex_one_of_and_set_equal():
    c = ctx({"id": "ap_3f", "kind": "Vase", "names": ["b", "a", "c"], "nums": [3, "2", 1]})
    assert ok({"type": "regex", "path": "$.id", "pattern": r"^ap_[0-9a-f]+$"}, c)
    assert not ok({"type": "regex", "path": "$.id", "pattern": r"^n_"}, c)
    assert ok({"type": "one_of", "path": "$.kind", "values": ["vase", "jar"]}, c) and not ok({"type": "one_of", "path": "$.kind", "values": ["jar"]}, c)
    assert ok({"type": "set_equal", "path": "$.names", "expected": ["a", "b", "c"]}, c)
    assert not ok({"type": "set_equal", "path": "$.names", "expected": ["a", "b", "c"], "ordered": True}, c)
    assert ok({"type": "set_equal", "path": "$.names", "expected": ["b", "a", "c"], "ordered": True}, c)
    assert not ok({"type": "set_equal", "path": "$.names", "expected": ["a", "b"]}, c)
    assert ok({"type": "set_equal", "path": "$.nums", "expected": [1, 2, 3]}, c)                 # "2" and 2 are the same number
    r = C.evaluate({"type": "set_equal", "path": "$.names", "expected": ["a", "z"]}, c)
    assert "missing" in r["detail"] and "unexpected" in r["detail"]
    assert not ok({"type": "set_equal", "path": "$.id", "expected": []}, c)                      # not a list


class FakeSandbox:
    def __init__(self, out="", code=0):
        self.out, self.code, self.ran = out, code, []

    def sh(self, cmd, agent=False, **kw):
        self.ran.append(cmd)
        return subprocess.CompletedProcess(cmd, self.code, self.out, "")


def test_state_jsonpath_runs_the_command_and_reads_its_output():
    sb = FakeSandbox(json.dumps({"ok": True, "data": {"count": 3, "items": ["a", "b"], "name": "vase-12"}}))
    c = ctx(sandbox=sb)
    base = {"type": "state_jsonpath", "cmd": "togi x --json"}
    assert ok({**base, "path": "$.data.count", "equals": 3}, c) and ok({**base, "path": "$.data.count", "equals": "3"}, c)
    assert not ok({**base, "path": "$.data.count", "equals": 4}, c) and ok({**base, "path": "$.data.count", "not_equals": 4}, c)
    assert ok({**base, "path": "$.data.items", "contains": "a"}, c) and ok({**base, "path": "$.data.name", "matches": r"^vase-\d+$"}, c)
    assert ok({**base, "path": "$.data.items", "len_equals": 2}, c) and ok({**base, "path": "$.data.count", "gt": 2}, c)
    assert not ok({**base, "path": "$.data.count", "lt": 3}, c) and ok({**base, "path": "$.data.count", "lte": 3}, c)
    assert ok({**base, "path": "$.data.count"}, c) and not ok({**base, "path": "$.data.zzz"}, c)
    assert ok(base, c) and not ok({**base, "exit_code": 1}, c)
    assert sb.ran[0] == "togi x --json"
    assert not ok({**base, "path": "$.a"}, ctx(sandbox=FakeSandbox("not json")))
    assert not ok(base, ctx())                                                                    # no sandbox: cannot pass


def traj(*rows):
    out = []
    for r in rows:
        cmd, status, surface = (r + ("ok", "cli"))[:3] if isinstance(r, tuple) else (r, "ok", "cli")
        out.append({"event": "call", "cmdline": f"togi {cmd}", "bundle": cmd.split()[0] if not cmd.startswith(("task", "answer")) else None,
                    "status": status, "ok": status == "ok", "surface": surface})
    return out


def test_trajectory_rules():
    t = traj("notes add", "notes find", ("notes tip send", "needs_approval"))
    c = ctx(traj=t)
    tr = lambda **k: ok({"type": "trajectory", **k}, c)
    assert tr(rule="used", command="notes add") and tr(rule="used", command="togi notes find") and not tr(rule="used", command="notes show")
    assert tr(rule="never_used", command="notes delete") and not tr(rule="never_used", command="notes add")
    assert tr(rule="max_calls", n=3) and not tr(rule="max_calls", n=2) and tr(rule="min_calls", n=3) and not tr(rule="min_calls", n=4)
    assert tr(rule="called_before", first="notes add", then="notes find") and not tr(rule="called_before", first="notes find", then="notes add")
    assert not tr(rule="called_before", first="notes add", then="notes delete")
    assert tr(rule="ended_at_needs_approval") and tr(rule="approval_requested") and tr(rule="approval_requested", command="notes tip send")
    assert not ok({"type": "trajectory", "rule": "ended_at_needs_approval"}, ctx(traj=traj("notes add")))
    assert tr(rule="no_forbidden_prefix", prefixes=["togi approve", "togi deny"])
    assert not ok({"type": "trajectory", "rule": "no_forbidden_prefix", "prefixes": ["togi notes add"]}, c)
    assert not tr(rule="used_code_mode") and ok({"type": "trajectory", "rule": "used_code_mode"}, ctx(traj=traj(("notes add", "ok", "run"))))
    # host commands (task, answer) do not count against "at most N calls"
    assert ok({"type": "trajectory", "rule": "max_calls", "n": 1}, ctx(traj=traj("notes add", "task show", "answer")))
    # "approve" must not match "approvals"
    assert ok({"type": "trajectory", "rule": "never_used", "command": "approve"}, ctx(traj=traj("approvals")))


def test_budget_check_and_unknown_measurements():
    c = ctx(turns=5, tokens=900, cost_usd=0.02)
    assert ok({"type": "budget", "max_turns": 5, "max_tokens": 1000, "max_cost_usd": 0.05}, c)
    r = C.evaluate({"type": "budget", "max_turns": 4, "max_cost_usd": 0.01}, c)
    assert r["ok"] is False and "turns 5 over 4" in r["detail"] and "cost_usd" in r["detail"].replace(" ", "_") or "cost" in r["detail"]
    assert ok({"type": "budget", "max_tokens": 10}, ctx(turns=1))                                  # tokens not measured: cannot fail on them


def test_judge_check_is_calibrated_reported_separately_and_never_gates():
    cal = [{"answer": {"t": "good"}, "label": "pass"}, {"answer": {"t": "bad"}, "label": "fail"}]
    check = {"type": "judge", "rubric": "is it good?", "calibration": cal}
    judge = lambda rubric, text: "pass" if "good" in text else "fail"
    r = C.evaluate(check, ctx({"t": "good"}, judge=judge))
    assert r["ok"] is True and r["gating"] is False and "calibration 100%" in r["detail"]
    assert C.evaluate(check, ctx({"t": "bad"}, judge=judge))["ok"] is False
    broken = C.evaluate(check, ctx({"t": "good"}, judge=lambda r, t: "pass"))                      # says pass to everything: uncalibrated
    assert broken["ok"] is None and "calibration" in broken["detail"]
    assert C.evaluate(check, ctx({"t": "good"}))["ok"] is None                                     # no judge configured
    results = [{"ok": True, "gating": True}, {"ok": False, "gating": False}, {"ok": None, "gating": False}]
    assert C.passed(results)                                                                       # a failing judge alone does not fail the item
    assert not C.passed(results + [{"ok": False, "gating": True}])
    assert not items.validate_item(items.parse_item(toml_item(check=[{"type": "exact", "path": "$.id", "expected": "n_1"}, check])))


def test_legacy_transcript_checks_and_answer_valid_and_check_errors():
    c = ctx(transcript="found n_1 ok")
    assert ok({"type": "transcript_contains", "text": "n_1"}, c) and ok({"type": "transcript_not_contains", "text": "boom"}, c)
    assert not ok({"type": "transcript_contains", "text": "47,248"}, c)
    assert ok({"type": "answer_valid"}, ctx({"a": 1}))
    r = C.evaluate({"type": "answer_valid"}, C.CheckContext(answers=[{"valid": False, "errors": ["$.n: expected integer"]}]))
    assert r["ok"] is False and "refused" in r["detail"]
    assert "no answer was recorded" in C.evaluate({"type": "answer_valid"}, ctx())["detail"]
    r = C.evaluate({"type": "exact", "path": "$.a"}, ctx({"a": 1}))                                # missing `expected`: a crash is a finding
    assert r["ok"] is False and "check error" in r["detail"]
    assert C.evaluate({"type": "mystery"}, ctx())["ok"] is False


def test_new_check_types_can_be_registered():
    @C.register("always_green", lambda c: [])
    def _fn(c, cx):
        return True, ""

    try:
        assert ok({"type": "always_green"}, ctx())
        assert C.validate_check({"type": "always_green"}) == []
    finally:
        C.CHECKS.pop("always_green")
        C.VALIDATORS.pop("always_green")


# ----------------------------------------------------------------------------- subjects


def test_subject_hash_is_stable_and_ignores_labels_sizes_prices_and_endpoints():
    a = subject.make_subject({"provider": "ollama", "model": "qwen2.5:7b", "harness": {"loop": "operator"}, "options": {"temperature": 0}})
    assert a.hash == "72b3ca53bc94d9f37f7a807e72c2d05c80154956d0e87f1588af12c69b56cafe"          # golden: a change here forks every cached result
    b = subject.make_subject({"id": "renamed", "provider": "ollama", "model": "qwen2.5:7b", "harness": {"loop": "operator"},
                              "options": {"temperature": 0, "base_url": "http://other:11434", "timeout_s": 5}, "size": 7.6,
                              "price": {"in_per_mtok": 1}, "role": "reference"})
    assert b.hash == a.hash and b.id == "renamed"
    for change in ({"model": "qwen2.5:3b"}, {"surface": "run"}, {"harness": {"loop": "react"}}, {"harness": {"loop": "operator", "skills": "full"}},
                   {"harness": {"loop": "operator", "examples": True}}, {"harness": {"loop": "operator", "reasoning_field": True}},
                   {"options": {"temperature": 0.7}}, {"provider": "openai-compatible"},
                   {"harness": {"loop": "operator", "constrained_decoding": False}}, {"harness": {"loop": "operator", "next_hints": False}}):
        d = {"provider": "ollama", "model": "qwen2.5:7b", "harness": {"loop": "operator"}, "options": {"temperature": 0}, **change}
        assert subject.make_subject(d).hash != a.hash, change


def test_harness_options_a_provider_does_not_use_do_not_fork_its_hash(tmp_path):
    cli = subject.make_subject({"provider": "claude-cli", "model": "haiku"})
    same = subject.make_subject({"provider": "claude-cli", "model": "haiku", "harness": {"loop": "operator", "step_budget": 3}})
    assert cli.hash == same.hash and sorted(same.ignored) == ["loop", "step_budget"]
    assert set(cli.harness) == {"skills", "next_hints", "compact_output", "examples", "skill_version"}
    step = subject.make_subject({"provider": "claude-step", "model": "haiku", "harness": {"constrained_decoding": True}})
    assert "constrained_decoding" not in step.harness and step.ignored == ["constrained_decoding"]   # `claude -p` has no schema-constrained decoding
    assert step.harness["context_policy"] == "full"                                                  # react's default
    op = subject.make_subject({"provider": "ollama", "model": "m", "harness": {"loop": "operator"}})
    assert op.harness["context_policy"] == "state-only"
    assert subject.make_subject({"provider": "ollama", "model": "m", "harness": {"loop": "plan-act-verify"}}).harness["context_policy"] == "window:6"
    assert subject.make_subject({"provider": "ollama", "model": "m", "harness": {"loop": "operator", "context_policy": "window:3"}}).harness["context_policy"] == "window:3"
    assert subject.make_subject({"provider": "script"}).harness == {}


def test_skill_variant_contents_are_in_the_hash(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text("v1")
    d = {"provider": "claude-cli", "model": "haiku", "harness": {"skill_version": "hone-v2"}, "skill_files": {"notes": str(f)}}
    h1 = subject.make_subject(d).hash
    f.write_text("v2")
    assert subject.make_subject(d).hash != h1
    with pytest.raises(TogiError):
        subject.make_subject({**d, "skill_files": {"notes": str(tmp_path / "missing.md")}})


@pytest.mark.parametrize("bad", [{"provider": "gpt"}, {"provider": "ollama"}, {"provider": "script", "surface": "carrier-pigeon"},
                                 {"provider": "script", "role": "boss"}, {"provider": "ollama", "model": "m", "harness": {"loop": "vibes"}},
                                 {"provider": "ollama", "model": "m", "harness": {"skills": "all"}}, {"provider": "ollama", "model": "m", "harness": {"nope": 1}},
                                 {"provider": "ollama", "model": "m", "harness": {"context_policy": "window:0"}},
                                 {"provider": "ollama", "model": "m", "harness": {"step_budget": 0}},
                                 {"provider": "ollama", "model": "m", "options": {"mystery": 1}}, {"provider": "script", "colour": "red"}])
def test_subject_validation(bad):
    with pytest.raises(TogiError) as e:
        subject.make_subject(bad)
    assert e.value.code == "subject_invalid"


def test_shorthand_matrix_and_files(tmp_path):
    subs = subject.load_subjects("script+role=reference,claude-cli:haiku,ollama:qwen2.5:7b+loop=operator+skills=full@run")
    assert [s.id for s in subs] == ["script", "claude-cli:haiku", "ollama:qwen2.5:7b+loop=operator"]
    assert subs[0].role == "reference" and subs[2].surface == "run" and subs[2].harness["skills"] == "full" and subs[2].size == 7.0
    f = tmp_path / "subjects.toml"
    f.write_text('[[subject]]\nid = "m"\nprovider = "ollama"\nmodel = "tiny-3b"\n[subject.matrix]\nloop = ["react", "operator", "code-first"]\n'
                 '[[subject]]\nid = "ref"\nprovider = "script"\nrole = "reference"\n')
    subs = subject.load_subjects(str(f))
    assert [s.id for s in subs] == ["m+loop=react", "m+loop=operator", "m+loop=code-first", "ref"] and len({s.hash for s in subs}) == 4
    assert subs[0].harness_key() != subs[1].harness_key() and subs[0].size == 3.0
    with pytest.raises(TogiError):
        subject.load_subjects("script,script")                                                   # duplicate id
    with pytest.raises(TogiError):
        subject.load_subjects("script+id=a,script+id=b")                                         # same hash under two labels
    with pytest.raises(TogiError):
        subject.load_subjects("ollama:x+loop")
