"""Eval layer, the whole CLI path: run a small matrix of mock models, then verdicts, item statistics, audit samples, the JSON and
self-contained HTML report, and compare."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from togi.errors import TogiError
from togi.evals import analysis, report, store, tomlw

from conftest import togi, togi_code
from evalhelp import MockLLM, toml_item, write_suite

ADD = {"action": "togi", "argv": ["notes", "add", "--text", "buy milk", "--tag", "home"]}
FIND = {"action": "togi", "argv": ["notes", "find", "--query", "milk"]}


def agent(kind):
    """A mock model whose competence depends on `kind`; it reads the case number from the instruction."""
    def policy(req, n):
        msgs = req["messages"]
        case = int(re.search(r"case (\d+)", msgs[1]["content"]).group(1))
        step = sum(1 for m in msgs if m["role"] == "assistant")
        if kind == "weak":
            return {"say": {"action": "give_up", "reason": "I cannot do this"}}
        answer = {"action": "answer", "answer": {"id": "n_9" if (kind == "medium" and case % 2) else "n_1"}}
        return {"say": [ADD, FIND, answer][step]}
    return policy


def case_items(n=8, **kw):
    """8 ordinary items plus case-8, whose answer key is wrong on purpose (nobody can pass it, the reference included)."""
    caps = [["tool-choice"], ["numeric"], ["multi-step", "numeric"], ["recovery"]]
    its = [toml_item(id=f"case-{k}", split="test", tier=1 + k % 3, capabilities=caps[k % 4], world="w" + str(k % 2),
                     instruction=f"Add a note 'buy milk' tagged home, find it, tell me its id. (case {k})", **kw) for k in range(n)]
    its.append({**toml_item(id="case-8", split="test", tier=2, capabilities=["numeric"], world="w0",
                            instruction="Add a note 'buy milk' tagged home, find it, tell me its id. (case 8)"),
                "check": [{"type": "exact", "path": "$.id", "expected": "n_77"}]})
    return its


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    """4 subjects over 9 test items x 2 repeats: the oracle, a strong, a medium and a weak mock model. Run ONCE for the module."""
    from togi import approver

    base = tmp_path_factory.mktemp("report")
    mp = pytest.MonkeyPatch()
    for k in list(os.environ):
        if k.startswith("TOGI_"):
            mp.delenv(k)
    mp.setenv("TOGI_HOME", str(base / "togi_home"))
    mp.setenv("TOGI_APPROVER_HOME", str(base / "togi_home_approver"))
    mp.setenv("TOGI_SESSION", "test-session")
    mp.setenv("NOTES_TEST_MODE", "1")
    tmp_path = base
    servers = {k: MockLLM(agent(k)) for k in ("strong", "medium", "weak")}
    for s in servers.values():
        s.__enter__()
    try:
        subs = [{"id": "oracle", "provider": "script", "role": "reference"}]
        for kind, size in (("strong", 14), ("medium", 7), ("weak", 3)):
            subs.append({"id": f"{kind}-{size}b", "provider": "ollama", "model": f"{kind}:{size}b", "size": size,
                         "options": {"base_url": servers[kind].ollama_url, "timeout_s": 20}, "harness": {"loop": "react", "step_budget": 6}})
        sf = tmp_path / "subjects.toml"
        sf.write_text(tomlw.dumps({"subject": subs}))
        root = write_suite(tmp_path / "suite", case_items())
        oc = tmp_path / "outcome.toml"
        oc.write_text('name = "narrow-job"\ndecision = "Can a 7B model do the notes job?"\nsplits = ["test"]\n'
                      '[gates]\nmin_success = 0.6\nmin_success_lcb = 0.3\n[[reference]]\nsubject = "oracle"\nbar = "target"\n')
        out = togi("eval", "run", "--outcome", str(oc), "--suite", str(root), "--subjects", str(sf), "--repeats", "2", "--quiet",
                   "--concurrency", "ollama=4,script=4")["data"]
        yield {"out": out, "root": root, "oc": oc, "sf": sf, "run": out["run"], "servers": servers, "tmp": tmp_path}
    finally:
        for s in servers.values():
            s.__exit__()
        mp.undo()


def test_run_reports_status_verdicts_and_where_the_report_is(matrix):
    out = matrix["out"]
    assert out["status"] == "complete" and out["cells"] == {"planned": 72, "done": 72, "cached": 0, "new": 72}
    assert out["splits_touched"] == ["test"]
    assert out["verdicts"] == {"oracle": "PASS", "strong-14b": "PASS", "medium-7b": "FAIL", "weak-3b": "FAIL"}
    assert out["smallest_passing"] == "strong-14b" and Path(out["report"]["html"]).is_file() and Path(out["report"]["json"]).is_file()   # (size 14 < no size)
    assert out["suspect_items"] == ["case-8"]                                                       # the reference failed it: blame the item first
    assert "oracle" in out["text"] and "weak-3b" in out["text"] and "suspect items: case-8" in out["text"]
    assert store.read_manifest(matrix["run"])["status"] == "complete"


def test_verdict_command_applies_the_gates_to_the_worst_slice(matrix):
    v = togi("eval", "verdict", matrix["run"])["data"]
    assert v["verdicts"]["strong-14b"]["verdict"] == "PASS" and v["verdicts"]["weak-3b"]["verdict"] == "FAIL"
    assert v["verdicts"]["medium-7b"]["verdict"] == "FAIL"                                        # 50% on the case-parity slices: fails its worst slice
    assert v["verdicts"]["medium-7b"]["worst_slice"]["slice"] and v["verdicts"]["weak-3b"]["reasons"]
    assert v["smallest_passing"]["subject"] == "strong-14b" and v["smallest_passing"]["basis"] == "size" and v["suspect_items"] == ["case-8"]
    assert v["verdicts_without_suspect_items"]["strong-14b"] == "PASS" and v["verdicts_without_suspect_items"]["medium-7b"] == "FAIL"
    assert "weak-3b: FAIL" in v["text"] and "smallest passing: strong-14b" in v["text"]
    # the same run under a stricter outcome file
    strict = matrix["tmp"] / "strict.toml"
    strict.write_text(matrix["oc"].read_text().replace("min_success = 0.6", "min_success = 1.0").replace("min_success_lcb = 0.3", "min_success_lcb = 0.9"))
    assert togi("eval", "verdict", matrix["run"][:12], "--outcome", str(strict))["data"]["verdicts"]["strong-14b"]["verdict"] == "FAIL"


def test_json_report_has_every_section_and_the_numbers_add_up(matrix):
    doc = json.loads(Path(matrix["out"]["report"]["json"]).read_text())
    assert {"subjects", "success", "paired", "items", "scaling", "harness_lift", "pareto", "verdicts", "taxonomy", "long_horizon", "integrity"} <= set(doc)
    by = {s["subject"]: s for s in doc["subjects"]}
    assert by["oracle"]["k"] == 16 and by["strong-14b"]["k"] == 16 and by["weak-3b"]["k"] == 0 and by["oracle"]["n"] == 18
    assert by["medium-7b"]["k"] == 8 and by["medium-7b"]["n"] == 18 and by["medium-7b"]["lo"] < 0.45 < by["medium-7b"]["hi"]
    pairs = {(p["a"], p["b"]): p for p in doc["paired"]}
    assert pairs[("weak-3b", "oracle")]["wins_b"] == 8 and pairs[("weak-3b", "oracle")]["ties"] == 1 and pairs[("weak-3b", "oracle")]["p_mcnemar"] == pytest.approx(2 / 256)
    assert pairs[("strong-14b", "oracle")]["p_mcnemar"] == 1.0 and pairs[("strong-14b", "oracle")]["ties"] == 9
    assert doc["taxonomy"]["by_class"].get("gave_up") == 18 and doc["taxonomy"]["by_subject"]["weak-3b"]["gave_up"] == 18
    assert [round(p["rate"], 2) for c in doc["scaling"]["curves"].values() for p in c["points"]] == [0.0, 0.44, 0.89]
    assert doc["integrity"]["reference_subjects"] == ["oracle"] and doc["integrity"]["suspect_items"] == ["case-8"]
    items = {i["item"]: i for i in doc["items"]}
    assert len(items) == 9 and items["case-0"]["difficulty"] == pytest.approx(3 / 4) and items["case-1"]["difficulty"] == pytest.approx(2 / 4)
    assert items["case-8"]["saturation"] == "all_fail" and items["case-8"]["suspect"] and "suspect_reference_fail" in items["case-8"]["flags"]


def test_html_report_is_one_self_contained_file(matrix):
    html = Path(matrix["out"]["report"]["html"]).read_text()
    assert html.startswith("<!doctype html>") and "<svg" in html and "polyline" in html and "Outcome verdicts" in html
    for forbidden in ("http://", "https://", "<script", "src=", "<link", "@import", "url(", "<iframe", "//cdn", "xmlns"):
        assert forbidden not in html, forbidden
    assert 'class="pass"' in html and 'class="fail"' in html and "strong-14b" in html and "Smallest subject that passes" in html
    assert len(html) < 120_000


def test_html_escapes_everything_it_prints(home, tmp_path):
    root = write_suite(tmp_path / "s", [toml_item()])
    from togi.evals import runner, subject

    ref = subject.make_subject({"provider": "script", "role": "reference", "id": "x<script>alert(1)</script>"})
    m = runner.run_matrix(runner.RunSpec(suite_path=str(root), subjects=[ref]))
    doc = report.build_report(m["run_id"], resamples=20)
    html = Path(doc["paths"]["html"]).read_text()
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_items_command_flags_saturated_and_suspect_items(matrix):
    out = togi("eval", "items", matrix["run"])["data"]
    rows = {r["item"]: r for r in out["items"]}
    assert len(rows) == 9 and out["flagged"] == 1 and out["suspect"] == ["case-8"]
    assert rows["case-0"]["difficulty"] == 0.75 and rows["case-1"]["difficulty"] == 0.5 and rows["case-8"]["saturation"] == "all_fail"
    assert rows["case-0"]["discrimination"] > 0.5 and "1 flagged" in out["text"] and "case-8" in out["text"]


def test_audit_samples_a_failure_class_for_a_human(matrix):
    a = togi("eval", "audit", matrix["run"], "--class", "gave_up", "--n", "3", "--seed", "4")["data"]
    assert a["available"] == 18 and len(a["sample"]) == 3 and {s["subject"] for s in a["sample"]} == {"weak-3b"}
    s0 = a["sample"][0]
    assert s0["class"] == "gave_up" and s0["failed_checks"] and "case" in s0["instruction"] and s0["evidence"]
    assert togi("eval", "audit", "latest", "--class", "gave_up", "--n", "3", "--seed", "4")["data"]["sample"] == a["sample"]       # seeded
    assert togi("eval", "audit", "latest", "--class", "gave_up", "--n", "3", "--seed", "5")["data"]["sample"] != a["sample"]
    assert togi("eval", "audit", "latest", "--class", "loop")["data"]["sample"] == []
    code, err = togi_code("eval", "audit", "latest", "--class", "vibes")
    assert code == 1 and err["error"]["code"] == "bad_arg" and "driver_error" in err["next"]
    assert togi_code("eval", "audit", "latest")[1]["error"]["code"] == "bad_arg"


def test_runs_listing_run_ids_and_report_regeneration(matrix):
    runs = togi("eval", "runs")["data"]
    assert runs["total"] == 1 and runs["runs"][0]["suite"] == "t" and runs["runs"][0]["outcome"] == "narrow-job"
    assert runs["runs"][0]["subjects"] == ["oracle", "strong-14b", "medium-7b", "weak-3b"] and runs["runs"][0]["splits"] == ["test"]
    r = togi("eval", "report", "latest", "--resamples", "300", "--seed", "9")["data"]
    assert r["run"] == matrix["run"] and Path(r["paths"]["html"]).is_file() and r["verdicts"]["weak-3b"] == "FAIL"
    doc9 = json.loads(Path(r["paths"]["json"]).read_text())
    assert doc9["paired"][0]["resamples"] == 300 and doc9["paired"][0]["seed"] == 9
    assert togi("eval", "report", matrix["run"][:14])["data"]["run"] == matrix["run"]
    code, err = togi_code("eval", "report", "ev_nope")
    assert code == 1 and err["error"]["code"] == "run_not_found"


def test_compare_two_runs_shows_subject_deltas_item_flips_and_verdict_changes(matrix):
    root, oc, sf = matrix["root"], matrix["oc"], matrix["sf"]
    first = matrix["run"]
    its = case_items()
    its[0] = {**its[0], "check": [{"type": "exact", "path": "$.id", "expected": "n_77"}]}                       # an item whose answer key changed
    write_suite(root, its)
    second = togi("eval", "run", "--outcome", str(oc), "--suite", str(root), "--subjects", str(sf), "--repeats", "2", "--quiet",
                  "--concurrency", "ollama=4,script=4")["data"]
    assert second["cells"]["cached"] == 64 and second["cells"]["new"] == 8                                       # eight items were served from the cache
    cmp = togi("eval", "compare", first, second["run"])["data"]
    flips = {(f["subject"], f["item"]): (f["a"], f["b"]) for f in cmp["item_flips"]}
    assert flips[("strong-14b", "case-0")] == (1.0, 0.0) and flips[("oracle", "case-0")] == (1.0, 0.0)
    assert ("weak-3b", "case-0") not in flips
    assert cmp["same_suite"] is False and {s["subject"] for s in cmp["subjects"]} == {"oracle", "strong-14b", "medium-7b", "weak-3b"}
    strong = next(s for s in cmp["subjects"] if s["subject"] == "strong-14b")
    assert strong["a"] == pytest.approx(16 / 18) and strong["b"] == pytest.approx(14 / 18) and strong["same_config"] is True
    assert "strong-14b" in cmp["text"] and "item(s) changed their pass rate" in cmp["text"]
    same = togi("eval", "compare", first, first)["data"]
    assert same["item_flips"] == [] and same["same_suite"] is True and same["verdict_changes"] == []
    assert togi_code("eval", "compare", first)[1]["error"]["code"] == "bad_arg"


def test_cli_errors_name_the_missing_piece(home, tmp_path):
    root = write_suite(tmp_path / "s", [toml_item()])
    assert togi_code("eval", "run", "--suite", str(root), "--subjects", "script")[1]["error"]["code"] == "bad_arg"            # no --outcome
    assert togi_code("eval", "outcome", "frob")[1]["error"]["code"] == "bad_arg"
    assert togi_code("eval", "frobnicate")[1]["error"]["code"] == "bad_arg"
    assert togi_code("eval", "run", "--outcome", "nope", "--suite", str(root), "--subjects", "script")[1]["error"]["code"] == "outcome_not_found"
    assert togi_code("eval", "suite", "validate", str(tmp_path / "missing"))[1]["error"]["code"] == "suite_not_found"
    out = togi("eval", "outcome", "new", "mine", "--dir", str(tmp_path))["data"]
    assert Path(out["outcome"]).is_file()
    oc = tmp_path / "outcomes" / "mine.toml"
    code, err = togi_code("eval", "run", "--outcome", str(oc), "--suite", str(root), "--subjects", "script", "--quiet")
    assert code == 1 and err["error"]["code"] == "no_reference_subject"
    ok = togi("eval", "run", "--outcome", str(oc), "--suite", str(root), "--subjects", "script+role=reference", "--quiet")["data"]
    assert ok["status"] == "complete" and ok["verdicts"]["script"] in ("PASS", "FAIL")
    assert "togi eval run" in togi("eval", "--help", "--json")["data"]["text"]


def test_lock_command_and_run_refusal_through_the_cli(home, tmp_path):
    root = write_suite(tmp_path / "s", [toml_item()])
    out = togi("eval", "lock", str(root), "--repeats", "2", "--subjects", "script+role=reference")["data"]
    assert out["items"] == 1 and out["preregistered"]["repeats"] == 2 and out["unchanged"] is False and (root / "LOCK.json").is_file()
    write_suite(root, [toml_item(instruction="changed after the lock")])
    code, err = togi_code("eval", "run", "--outcome", str(tmp_path / "o.toml"), "--suite", str(root), "--subjects", "script+role=reference")
    assert code in (1, 2) and err["error"]["code"] in ("suite_changed_since_lock", "outcome_not_found")
    oc = tmp_path / "o.toml"
    oc.write_text('name = "o"\n')
    code, err = togi_code("eval", "run", "--outcome", str(oc), "--suite", str(root), "--subjects", "script+role=reference", "--quiet")
    assert code == 2 and err["error"]["code"] == "suite_changed_since_lock" and "--relock" in err["next"]
    assert togi("eval", "lock", str(root), "--relock")["data"]["unchanged"] is False
    assert togi("eval", "run", "--outcome", str(oc), "--suite", str(root), "--subjects", "script+role=reference", "--quiet")["data"]["status"] == "complete"
