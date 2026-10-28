"""Eval layer: wall-clock limits, the rubric judge, more than one bundle in a sandbox, skill variants in our loops, the CLI flags that
steer a run, and the stone import run end to end."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from togi.errors import TogiError
from togi.evals import analysis, cell, items, judge as judgelib, runner, store, subject, taxonomy
from togi.evals import checks as C
from togi.evals.sandbox import EvalSandbox

from conftest import togi, togi_code
from evalhelp import NOTES, MockLLM, claude_calls, install_fake_claude, local_subject, scripted, toml_item, write_suite
from test_eval_loops import ADD, ANSWER, FIND, bare, play


def spec(root, subjects, **kw):
    return runner.RunSpec(suite_path=str(root), subjects=[subject.make_subject(s) for s in subjects], **kw)


# ----------------------------------------------------------------------------- wall-clock limits


def test_a_slow_local_model_hits_the_item_timeout_and_the_cell_says_so(home, tmp_path):
    def slow(req, n):
        time.sleep(0.5)
        return {"say": {"action": "togi", "argv": ["notes", "find", "--query", f"q{n}"]}}

    suite = items.load_suite(write_suite(tmp_path / "s", [bare()]))
    with MockLLM(slow) as m:
        sub = subject.make_subject(local_subject("ollama", m.ollama_url, role="reference", step_budget=20))
        t0 = time.monotonic()
        res = cell.execute_cell(suite, suite.items[0], sub, 0, item_timeout=1.2)
    s = res["sessions"][0]
    assert time.monotonic() - t0 < 15 and s["finish_reason"] == "timeout" and s["timed_out"] and res["timed_out"] is True
    assert res["success"] is False and res["valid"] is True                      # a slow model is a result, not a driver error
    assert any("wall-clock" in e["detail"] for e in s["events"]) and taxonomy.classify(res)["class"] == "budget"


def test_a_hung_claude_is_killed_with_its_children_and_a_timeout_is_not_reused_under_a_longer_limit(home, tmp_path, monkeypatch):
    marker = tmp_path / "survivor"
    install_fake_claude(tmp_path, monkeypatch, mode="hang")
    monkeypatch.setenv("FAKE_CLAUDE_MARKER", str(marker))
    root = write_suite(tmp_path / "s", [{k: v for k, v in toml_item().items() if k not in ("script", "script_answer_json")}])
    sub = [{"provider": "claude-cli", "model": "m", "role": "reference"}]
    t0 = time.monotonic()
    m1 = runner.run_matrix(spec(root, sub, item_timeout=1.5))
    c = store.read_cells(m1["run_id"])[0]
    assert time.monotonic() - t0 < 20 and c["timed_out"] is True and c["success"] is False and c["valid"] is True
    time.sleep(3.6)
    assert not marker.exists()                                                  # the grandchild (like claude's MCP server) died with the group
    again = runner.run_matrix(spec(root, sub, item_timeout=1.5))
    assert again["cells_cached"] == 1                                           # same limit: the timeout is a result
    longer = runner.run_matrix(spec(root, sub, item_timeout=2.5))
    assert longer["cells_cached"] == 0 and longer["cells_new"] == 1             # a different limit proves nothing about this one


# ----------------------------------------------------------------------------- the judge


JUDGE_CHECK = {"type": "judge", "rubric": "The answer says GOOD things only.",
               "calibration": [{"answer": {"t": "GOOD stuff"}, "label": "pass"}, {"answer": {"t": "bad stuff"}, "label": "fail"}]}


def test_claude_judge_asks_for_one_word_and_anything_else_is_an_error(tmp_path, monkeypatch):
    log = install_fake_claude(tmp_path, monkeypatch, mode="judge")
    j = judgelib.claude_judge("haiku")
    assert j("rubric", "GOOD answer") == "pass" and j("rubric", "meh") == "fail"
    a = claude_calls(log)[0]["args"]
    assert a[a.index("--model") + 1] == "haiku" and "--tools=" in a and "Reply with exactly one word" in a[a.index("--system-prompt") + 1]
    assert claude_calls(log)[0]["key"] is False
    monkeypatch.setenv("FAKE_CLAUDE_JUDGE_SAYS", "Pass.")
    assert j("r", "x") == "pass"
    monkeypatch.setenv("FAKE_CLAUDE_JUDGE_SAYS", "I am not sure")
    assert j("r", "x") == "error"
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    assert j("r", "x") == "error"


def test_a_judge_check_in_a_run_is_reported_separately_and_never_decides(home, tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch, mode="judge")
    schema = {"type": "object", "required": ["id", "t"], "properties": {"id": {"type": "string"}, "t": {"type": "string"}}}
    ok_item = toml_item(id="good", answer_schema=schema, script_answer_json={"id": "n_1", "t": "GOOD and fine"},
                        check=[{"type": "exact", "path": "$.id", "expected": "n_1"}, JUDGE_CHECK])
    bad_item = toml_item(id="bad", answer_schema=schema, script_answer_json={"id": "n_1", "t": "rude"},
                         check=[{"type": "exact", "path": "$.id", "expected": "n_1"}, JUDGE_CHECK])
    root = write_suite(tmp_path / "s", [ok_item, bad_item])
    m = runner.run_matrix(spec(root, [{"provider": "script", "role": "reference"}], judge=judgelib.claude_judge("haiku")))
    by = {c["item"]: c for c in store.read_cells(m["run_id"])}
    assert by["good"]["judge"][0]["ok"] is True and by["bad"]["judge"][0]["ok"] is False and by["bad"]["judge"][0]["gating"] is False
    assert by["good"]["success"] is True and by["bad"]["success"] is True          # the failing judge changed nothing about success
    nojudge = runner.run_matrix(spec(root, [{"provider": "script", "role": "reference", "options": {"temperature": 3}}]))
    assert store.read_cells(nojudge["run_id"])[0]["judge"][0]["ok"] is None
    assert analysis.analyze(analysis.load_run(m["run_id"]), resamples=10)["integrity"]["judge_cells"] == 2


# ----------------------------------------------------------------------------- more than one bundle, skill variants


def make_bundle(root: Path, name: str, word: str) -> Path:
    (root / "src" / name).mkdir(parents=True)
    (root / "togi.toml").write_text(f'[bundle]\nname="{name}"\nmodule="{name}.togi_bundle:bundle"\nsummary="says {word}"\n[tiers]\npolish=["skills/**"]\n')
    (root / "src" / name / "__init__.py").write_text("")
    (root / "src" / name / "togi_bundle.py").write_text(
        f"from togi import Bundle\nbundle = Bundle({name!r})\n@bundle.command('say', effect='read')\ndef say() -> dict:\n    '''Say the word.'''\n    return {{'word': {word!r}}}\n")
    (root / "skills").mkdir()
    (root / "skills" / "SKILL.md").write_text(f"---\nname: {name}\nsummary: x\n---\n# {name}\nSay it with `togi {name} say`.\n")
    return root


def test_an_item_can_use_several_bundles_in_one_sandbox(home, tmp_path):
    other = make_bundle(tmp_path / "echoer", "echoer", "banana")
    it = toml_item(id="two", bundles=["notes", "echoer"], script=["togi notes add --text x", "togi echoer say"],
                   script_answer_json={"id": "banana"}, check=[{"type": "exact", "path": "$.id", "expected": "banana"},
                                                              {"type": "trajectory", "rule": "used", "command": "echoer say"}])
    root = write_suite(tmp_path / "s", [it], bundles={"notes": {"path": str(NOTES)}, "echoer": {"path": str(other)}})
    m = runner.run_matrix(spec(root, [{"provider": "script", "role": "reference"}]))
    c = store.read_cells(m["run_id"])[0]
    assert c["success"] is True and [e["cmdline"] for e in c["sessions"][0]["trajectory"]][:2] == ["togi notes add", "togi echoer say"]
    sb = EvalSandbox({"notes": NOTES, "echoer": other})
    try:
        assert sorted(json.loads(sb.run_argv(["bundles"]).stdout)["data"], key=lambda b: b["name"])[0]["name"] == "echoer"
        guide = sb.run_argv(["skill"]).stdout
        assert "- notes:" in guide and "- echoer: says banana" in guide
    finally:
        sb.cleanup()
    # a bundle the suite cannot find stops the run before any cell starts, with the way out
    bad = write_suite(tmp_path / "bad", [toml_item(bundles=["ghost"])], bundles={})
    with pytest.raises(TogiError) as e:
        runner.run_matrix(spec(bad, [{"provider": "script", "role": "reference"}]))
    assert e.value.code == "bundle_unresolved" and "ghost" in e.value.message and "togi add" in e.value.next


def test_a_skill_variant_reaches_a_loop_provider_prompt(home, tmp_path):
    variant = tmp_path / "SKILL-v2.md"
    variant.write_text("---\nname: notes\nsummary: v2\n---\n# notes v2\nHONE-V2 says: always tag your notes.\n")
    suite = items.load_suite(write_suite(tmp_path / "s", [bare()]))
    outs = {}
    for label, extra in (("orig", {}), ("v2", {"skill_files": {"notes": str(variant)}, "harness": {"skills": "full", "skill_version": "hone-v2"}})):
        with MockLLM(scripted([{"action": "give_up"}])) as mock:
            d = local_subject("ollama", mock.ollama_url, skills="full")
            d.update({k: v for k, v in extra.items() if k == "skill_files"})
            if "harness" in extra:
                d["harness"] = {**d["harness"], **extra["harness"]}
            sub = subject.make_subject(d)
            res = cell.execute_cell(suite, suite.items[0], sub, 0)
            outs[label] = (mock.requests[0]["messages"][0]["content"], sub.hash)
    assert "HONE-V2" in outs["v2"][0] and "HONE-V2" not in outs["orig"][0] and outs["v2"][1] != outs["orig"][1]


# ----------------------------------------------------------------------------- CLI flags that steer a run


def outcome_file(tmp_path):
    p = tmp_path / "o.toml"
    p.write_text('name = "o"\nsplits = ["dev", "test", "unseen"]\n[gates]\nmin_success = 0.5\nmin_success_lcb = 0.0\n')
    return p


def test_cli_run_flags_splits_items_unseen_resume_and_run_id(home, tmp_path):
    root = write_suite(tmp_path / "s", [toml_item(id="d1", split="dev"), toml_item(id="d2", split="dev"), toml_item(id="t1", split="test"),
                                        toml_item(id="u1", split="unseen")])
    oc = outcome_file(tmp_path)
    base = ["eval", "run", "--outcome", str(oc), "--suite", str(root), "--subjects", "script+role=reference", "--quiet"]
    plain = togi(*base)["data"]
    assert plain["splits_touched"] == ["dev", "test"] and plain["cells"]["planned"] == 3
    unseen = togi(*base, "--include-unseen", "--splits", "unseen")["data"]
    assert unseen["splits_touched"] == ["unseen"] and unseen["cells"]["planned"] == 1
    one = togi(*base, "--items", "d1,t1", "--run-id", "named-run")["data"]
    assert one["run"] == "named-run" and one["cells"]["planned"] == 2 and one["status"] == "complete"
    code, err = togi_code(*base, "--items", "nope")
    assert code == 1 and err["error"]["code"] == "unknown_task"
    code, err = togi_code(*base, "--splits", "unseen")                           # unseen is not loaded without the flag
    assert code == 1 and err["error"]["code"] == "empty_suite"
    part = togi(*base, "--run-id", "half", "--repeats", "2", "--items", "d1,d2,t1")["data"]
    assert part["status"] == "complete"
    code, err = togi_code(*base, "--resume", "no-such-run")
    assert code == 1 and err["error"]["code"] == "run_not_found"
    again = togi(*base, "--resume", "named-run", "--items", "d1,t1")["data"]
    assert again["run"] == "named-run" and again["cells"]["done"] == 2 and again["status"] == "complete"
    assert togi_code(*base, "--concurrency", "claude-cli")[1]["error"]["code"] == "bad_arg"


def test_cli_run_cost_cap_and_reference_waiver(home, tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch, script=[[["togi", "answer", "--json", '{"id": "n_1"}']]], mode="agent")
    its = [{k: v for k, v in toml_item(id=f"c{n}").items() if k not in ("script", "script_answer_json")} for n in range(5)]
    root = write_suite(tmp_path / "s", its)
    oc = outcome_file(tmp_path)
    out = togi("eval", "run", "--outcome", str(oc), "--suite", str(root), "--subjects", "claude-cli:m+role=reference", "--max-cost", "0.03",
               "--concurrency", "claude-cli=1", "--quiet", "--max-item-cost", "0.4")
    d = out["data"]
    assert d["status"] == "stopped: cost_cap" and d["cells"]["new"] == 2 and "--resume" in d["next"]
    code, err = togi_code("eval", "run", "--outcome", str(oc), "--suite", str(root), "--subjects", "claude-cli:m", "--quiet")
    assert err["error"]["code"] == "no_reference_subject"
    ok = togi("eval", "run", "--outcome", str(oc), "--suite", str(root), "--subjects", "claude-cli:m", "--no-reference-check", "--quiet",
              "--max-cost", "0.03", "--concurrency", "claude-cli=1")["data"]
    assert ok["status"].startswith("stopped") and ok["cells"]["cached"] == 2


def test_cli_judge_flag_runs_the_rubric_judge(home, tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch, mode="judge")
    schema = {"type": "object", "required": ["id", "t"], "properties": {"id": {"type": "string"}, "t": {"type": "string"}}}
    root = write_suite(tmp_path / "s", [toml_item(answer_schema=schema, script_answer_json={"id": "n_1", "t": "GOOD"},
                                                  check=[{"type": "exact", "path": "$.id", "expected": "n_1"}, JUDGE_CHECK])])
    d = togi("eval", "run", "--outcome", str(outcome_file(tmp_path)), "--suite", str(root), "--subjects", "script+role=reference",
             "--judge", "claude:haiku", "--quiet")["data"]
    assert store.read_cells(d["run"])[0]["judge"][0]["ok"] is True
    assert togi_code("eval", "run", "--outcome", str(outcome_file(tmp_path)), "--suite", str(root), "--subjects", "script+role=reference",
                     "--judge", "gpt:4")[1]["error"]["code"] == "bad_arg"


def test_a_stone_suite_imported_from_a_bundle_runs_through_the_cli_end_to_end(notes, home, tmp_path):
    dest = tmp_path / "ns"
    togi("eval", "suite", "import-stone", "notes", "--out", str(dest))
    assert togi("eval", "suite", "validate", str(dest))["data"]["ok"] is True
    d = togi("eval", "run", "--outcome", str(outcome_file(tmp_path)), "--suite", str(dest), "--subjects", "script+role=reference", "--quiet")["data"]
    assert d["status"] == "complete" and d["cells"]["planned"] == 3
    cells = store.read_cells(d["run"])
    assert all(c["success"] for c in cells) and {c["split"] for c in cells} == {"dev", "test"} and all(c["legacy"] for c in cells)
    assert d["verdicts"]["script"] == "PASS"
    d2 = togi("eval", "run", "--outcome", str(outcome_file(tmp_path)), "--suite", str(dest), "--subjects", "script+role=reference",
              "--quiet")["data"]
    assert d2["cells"]["cached"] == 3


def test_hooks_refuse_to_run_outside_an_eval_sandbox(home):
    import subprocess
    import sys

    from togi import paths

    code = "import sys; sys.path.insert(0, sys.argv[1]); from togi.evals.hooks import main; sys.exit(main(sys.argv[2:]))"
    env = {k: v for k, v in __import__("os").environ.items() if k.startswith("TOGI_")} | {"PATH": "/usr/bin:/bin"}
    p = subprocess.run([sys.executable, "-I", "-c", code, str(paths.src_dir()), "approve", "latest"], capture_output=True, text=True, env=env)
    assert p.returncode == 2 and "not inside an eval sandbox" in p.stdout
    p = subprocess.run([sys.executable, "-I", "-c", code, str(paths.src_dir()), "frobnicate"], capture_output=True, text=True, env=env)
    assert p.returncode == 2


# ----------------------------------------------------------------------------- the MCP surface for real, compare edge cases, driver errors via stone


def test_the_sandbox_mcp_server_serves_the_bundle_plus_answer_and_task_over_real_stdio(home, tmp_path):
    mcp = pytest.importorskip("mcp")
    import asyncio

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    sb = EvalSandbox({"notes": NOTES})
    try:
        sb.write_answer_schema({"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}})
        cfg = sb.mcp_config()["mcpServers"]["togi"]
        assert cfg["env"]["TOGI_AGENT"] == "eval" and cfg["env"]["TOGI_SESSION"] == sb.agent_session and cfg["args"][-1] == "--builtins"

        async def go():
            params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env=cfg["env"], cwd=str(sb.cwd))
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    tools = {t.name: t for t in (await session.list_tools()).tools}
                    add = await session.call_tool("notes_add", {"text": "via sandbox mcp"})
                    bad = await session.call_tool("answer", {"id": 7})
                    good = await session.call_tool("answer", {"id": "n_1"})
                    task = await session.call_tool("task_start", {"goal": "mcp job"})
                    return tools, add, bad, good, task

        tools, add, bad, good, task = asyncio.run(go())
        assert {"answer", "task_show", "notes_add", "notes_skill"} <= set(tools) and tools["answer"].inputSchema["required"] == ["id"]
        assert json.loads(add.content[0].text)["data"]["id"] == "n_1" and bad.isError is True and good.isError is False
        assert json.loads(task.content[0].text)["data"]["goal"] == "mcp job"
        assert [(a["valid"], a["surface"]) for a in sb.answers_since(0)] == [(False, "mcp"), (True, "mcp")]
        assert sb.task_record()["goal"] == "mcp job"
    finally:
        sb.cleanup()


def test_script_provider_on_the_mcp_surface_drives_a_real_server(notes, home):
    pytest.importorskip("mcp")
    from togi import stone

    r = stone.run_stone(notes, driver="script", with_tests=False, save=False, engine="eval", surface="mcp", task_ids=["add-and-find"])
    t = r["tasks"][0]
    assert t["success"] is True and {j["surface"] for j in t["journal"]} == {"mcp"}


def test_stone_via_eval_reports_a_missing_claude_as_a_driver_error(notes, home, monkeypatch):
    from togi import stone

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    r = stone.run_stone(notes, driver="claude", model="haiku", with_tests=False, save=False, engine="eval", task_ids=["add-and-find"])
    t = r["tasks"][0]
    assert t["success"] is False and "not on PATH" in t["driver_error"] and r["summary"]["driver_errors"] == 1


def test_compare_marks_a_subject_that_is_new_in_the_second_run_and_one_that_changed_config(home, tmp_path):
    from togi.evals import report

    root = write_suite(tmp_path / "s", [toml_item(id="a"), toml_item(id="b")])
    ref = {"provider": "script", "role": "reference", "id": "oracle"}
    first = runner.run_matrix(spec(root, [ref]))
    second = runner.run_matrix(spec(root, [ref, {"provider": "script", "id": "extra", "options": {"temperature": 1}}]))
    cmp = report.compare_runs(first["run_id"], second["run_id"])
    by = {s["subject"]: s for s in cmp["subjects"]}
    assert by["extra"]["status"] == "new in B" and by["oracle"]["same_config"] is True and by["oracle"]["diff"] == 0
    renamed = runner.run_matrix(spec(root, [{**ref, "options": {"temperature": 1}}]))
    cmp = report.compare_runs(first["run_id"], renamed["run_id"])
    assert cmp["subjects"][0]["same_config"] is False and "(config changed)" in cmp["text"]
    assert cmp["removed_subjects"] == [] and cmp["items_only_in_a"] == cmp["items_only_in_b"] == []
