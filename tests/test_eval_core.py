"""Eval layer, core pieces that live in Togi itself: the schema validator, `togi answer` on every surface, the task record
(`togi task`), the harness ablation switches, and the call recording that only an eval sandbox may turn on."""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap

import pytest

from togi import answers, journal, mcp_bridge, registry, schema, skills, taskrec

from conftest import clean_env, run_togi, togi, togi_code


# ----------------------------------------------------------------------------- schema validator


def test_schema_types_required_enum_and_nesting():
    sch = {"type": "object", "required": ["name", "n"], "additionalProperties": False,
           "properties": {"name": {"type": "string", "minLength": 2}, "n": {"type": "integer", "minimum": 0},
                          "tags": {"type": "array", "items": {"enum": ["a", "b"]}, "maxItems": 2},
                          "kind": {"enum": [1, "x"]}, "inner": {"type": "object", "properties": {"v": {"type": "number"}}}}}
    assert schema.validate({"name": "ab", "n": 3, "tags": ["a"], "inner": {"v": 1.5}}, sch) == []
    errs = schema.validate({"name": "a", "n": -1, "tags": ["a", "z", "b"], "extra": 1, "inner": {"v": "x"}}, sch)
    text = " | ".join(errs)
    assert "$.name: shorter than 2" in text and "$.n: below the minimum 0" in text and "more than 2 items" in text
    assert "$.tags[1]: must be one of" in text and "unexpected field 'extra'" in text and "$.inner.v: expected number, got string" in text
    assert "missing required field 'name'" in " ".join(schema.validate({"n": 1}, sch))


def test_schema_booleans_are_not_integers_and_integral_floats_are():
    assert schema.validate(True, {"type": "integer"}) and schema.validate(True, {"type": "number"})
    assert schema.validate(3.0, {"type": "integer"}) == [] and schema.validate(3.5, {"type": "integer"})
    assert schema.validate(1, {"const": True}) and schema.validate(True, {"const": True}) == []
    assert schema.validate(float("nan"), {"type": "number"})


def test_schema_oneof_anyof_allof_not_ref_pattern():
    assert schema.validate("a", {"oneOf": [{"type": "string"}, {"type": "number"}]}) == []
    assert schema.validate(1, {"oneOf": [{"type": "integer"}, {"type": "number"}]})            # matches both
    assert schema.validate(None, {"anyOf": [{"type": "string"}, {"type": "number"}]})
    assert schema.validate("ab", {"allOf": [{"type": "string"}, {"maxLength": 1}]})
    assert schema.validate("x", {"not": {"type": "string"}})
    ref = {"$defs": {"id": {"type": "string", "pattern": "^n_[0-9]+$"}}, "type": "object", "properties": {"id": {"$ref": "#/$defs/id"}}}
    assert schema.validate({"id": "n_12"}, ref) == [] and schema.validate({"id": "x"}, ref)
    assert schema.validate({"id": 1}, {"properties": {"id": {"$ref": "#/nope"}}})[0].startswith("$.id: cannot resolve")


def test_check_schema_flags_unusable_answer_schemas():
    assert schema.check_schema({"type": "object", "properties": {"a": {"type": "string"}}}) == []
    assert any("type" in p for p in schema.check_schema({"properties": {}}))
    assert any("unknown type" in p for p in schema.check_schema({"type": "object", "properties": {"a": {"type": "strng"}}}))
    assert any("pattern" in p for p in schema.check_schema({"type": "object", "properties": {"a": {"type": "string", "pattern": "("}}}))
    assert schema.check_schema([1]) == ["the schema must be a JSON object"]


# ----------------------------------------------------------------------------- togi answer


def answers_log(home):
    return [json.loads(l) for l in (home / "state" / "_host" / "answers.jsonl").read_text().splitlines()]


def test_answer_without_a_schema_accepts_an_object_and_rejects_the_rest(home):
    out = togi("answer", "--json", '{"region": "north", "revenue": 47248}')
    assert out["ok"] and out["data"] == {"recorded": True, "seq": 1}
    code, bad = togi_code("answer", "--json", "[1, 2]")
    assert code == 1 and bad["error"]["code"] == "answer_invalid" and "must be a JSON object" in bad["error"]["message"]
    code, nj = togi_code("answer", "--json", "{nope")
    assert code == 1 and nj["error"]["code"] == "answer_not_json" and "quote it" in nj["next"]
    log = answers_log(home)
    assert [(e["valid"], e["answer"]) for e in log] == [(True, {"region": "north", "revenue": 47248}), (False, [1, 2])]


def test_answer_is_validated_against_the_tasks_schema_and_a_refusal_is_recorded(home):
    (home / "answer-schema.json").write_text(json.dumps({"type": "object", "required": ["n"], "additionalProperties": False,
                                                          "properties": {"n": {"type": "integer"}}}))
    code, out = togi_code("answer", "--json", '{"n": "47,248"}')
    assert code == 1 and out["error"]["code"] == "answer_invalid" and "$.n: expected integer" in out["error"]["message"]
    assert out["next"].startswith("fix the fields")
    assert togi("answer", "--json", '{"n": 47248}')["data"]["seq"] == 2
    log = answers_log(home)
    assert [e["valid"] for e in log] == [False, True] and log[0]["errors"]


def test_answer_forms_positional_stdin_file_and_trailing_json_flag(home, tmp_path):
    assert togi("answer", '{"a": 1}')["ok"]
    assert togi("answer", "--json", '{"a": 2}', "--json")["ok"]                    # a trailing bare --json is the output flag
    f = tmp_path / "a.json"
    f.write_text('{"a": 3}')
    assert togi("answer", "--file", str(f))["ok"]
    p = run_togi(home, "answer", "--stdin", input='{"a": 4}')
    assert json.loads(p.stdout)["data"]["recorded"] is True
    assert [e["answer"] for e in answers_log(home)] == [{"a": 1}, {"a": 2}, {"a": 3}, {"a": 4}]
    code, out = togi_code("answer", "--bogus", "x")
    assert code == 1 and out["error"]["code"] == "unknown_flag"
    assert togi_code("answer")[1]["error"]["code"] == "bad_arg"


def test_answer_event_in_the_journal_holds_types_never_values(home):
    togi("answer", "--json", '{"secret": "sk-live-abc123", "amount": 12.5, "ok": true}')
    entries = journal.read("_host")
    ev = [e for e in entries if e.get("event") == "answer"][0]
    assert ev["command"] == "answer" and ev["ok"] is True and ev["surface"] == "cli"
    assert ev["args_shape"] == {"answer": {"secret": "str", "amount": "float", "ok": "bool"}}
    assert "sk-live" not in json.dumps(entries) and "12.5" not in json.dumps(entries)
    assert len(ev["answer_sha"]) == 16


def test_answer_is_bounded(home):
    big = json.dumps({"x": "y" * (answers.MAX_ANSWER_BYTES + 10)})
    code, out = togi_code("answer", "--json", big)
    assert code == 1 and "limit is" in out["error"]["message"]


def run_script(home, src, **kw):
    return run_togi(home, "run", "-", input=textwrap.dedent(src), **kw)


def test_answer_in_code_mode_is_togi_answer(notes, home):
    (home / "answer-schema.json").write_text(json.dumps({"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}))
    p = run_script(home, """
        n = notes.add(text="hello")
        r = togi_answer({"id": n["id"]})
        print("recorded", r)
        try:
            togi_answer({"id": 7})
        except togi.TogiError as e:
            print("refused", e.code)
    """)
    assert p.returncode == 0, p.stderr
    assert "recorded {'recorded': True, 'seq': 1}" in p.stdout and "refused answer_invalid" in p.stdout
    log = answers_log(home)
    assert [(e["valid"], e["surface"]) for e in log] == [(True, "run"), (False, "run")]
    ev = [e for e in journal.read("_host") if e.get("event") == "answer"]
    assert [e["surface"] for e in ev] == ["run", "run"]


def test_answer_over_mcp_tool_and_mcp_call(notes, home):
    pytest.importorskip("mcp")
    loaded = registry.load("notes")
    names = [s["name"] for s in mcp_bridge.tool_specs(loaded)]
    assert "answer" not in names and "task_show" not in names              # opt-in: a plain server lists bundle tools only
    specs = {s["name"]: s for s in mcp_bridge.tool_specs(loaded, builtins=True)}
    assert {"answer", "task_start", "task_show", "task_update"} <= set(specs)
    assert specs["answer"]["inputSchema"]["type"] == "object"
    (home / "answer-schema.json").write_text(json.dumps({"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}))
    assert mcp_bridge.builtin_specs()[0]["inputSchema"]["required"] == ["id"]       # the item's schema IS the tool's input schema
    env, err = mcp_bridge.call_tool(loaded, "answer", {"id": "n_1"})
    assert not err and env["data"] == {"recorded": True, "seq": 1}
    env, err = mcp_bridge.call_tool(loaded, "answer", {"id": 5})
    assert err and env["error"]["code"] == "answer_invalid"
    assert [e["surface"] for e in journal.read("_host") if e.get("event") == "answer"] == ["mcp", "mcp"]
    out = togi("mcp", "call", "notes", "answer", "--args", '{"id": "n_2"}')
    assert out["data"]["recorded"] is True
    code, out = togi_code("mcp", "call", "notes", "notes_add", "--args", '{"text": "via call"}')
    assert code == 0 and out["data"]["text"] == "via call"
    assert [e["surface"] for e in journal.read("notes") if e.get("event") == "call"] == ["mcp"]


def test_the_mcp_server_lists_the_builtin_tools_over_stdio_when_asked(notes, home):
    mcp = pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go(extra):
        params = StdioServerParameters(command=sys.executable, args=["-m", "togi", "mcp", "serve", "notes", *extra],
                                       env=clean_env(home, TOGI_AGENT="mcp-client"))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                names = [t.name for t in (await session.list_tools()).tools]
                res = await session.call_tool("answer", {"id": "n_1"}) if extra else None
                return names, res

    plain, _ = asyncio.run(go([]))
    with_builtins, res = asyncio.run(go(["--builtins"]))
    assert "answer" not in plain and len(plain) == 10
    assert {"answer", "task_show", "task_start", "task_update"} <= set(with_builtins)
    assert json.loads(res.content[0].text)["data"]["recorded"] is True


# ----------------------------------------------------------------------------- the task record


def test_task_start_show_update_and_the_resume_convention(home):
    assert togi("task", "show")["data"]["active"] is False
    out = togi("task", "start", "--goal", "reconcile October payouts")["data"]
    assert out["goal"] == "reconcile October payouts" and out["status"] == "active" and out["revision"] == 1
    code, again = togi_code("task", "start", "--goal", "something else")
    assert code == 1 and again["error"]["code"] == "task_exists" and "togi task show" in again["next"]
    up = togi("task", "update", "--done", "listed 12 payouts", "--done", "checked fees", "--next", "settle batch 2",
              "--waiting", "approval ap_1")["data"]
    assert up["done"] == ["listed 12 payouts", "checked fees"] and up["next"] == "settle batch 2" and up["waiting"] == "approval ap_1"
    assert up["revision"] == 2
    shown = togi("task", "show")["data"]
    assert shown["done"] == up["done"] and shown["goal"] == "reconcile October payouts"
    togi("task", "update", "--waiting", "", "--status", "blocked")
    assert togi("task", "show")["data"]["waiting"] == "" and togi("task", "show")["data"]["status"] == "blocked"
    assert togi("task", "start", "--goal", "replace it", "--force")["data"]["goal"] == "replace it"
    code, out = togi_code("task", "update")
    assert code == 1 and out["error"]["code"] == "bad_arg"
    togi("task", "update", "--status", "done")
    assert togi("task", "show")["data"]["active"] is False
    assert togi("task", "start", "--goal", "a new job after the old one is done")["ok"]            # a done task is replaceable


def test_task_update_without_a_task_and_bad_subcommands(home):
    code, out = togi_code("task", "update", "--next", "x")
    assert code == 1 and out["error"]["code"] == "no_task" and "task start" in out["next"]
    assert togi_code("task", "frobnicate")[1]["error"]["code"] == "bad_arg"
    assert togi_code("task", "update", "--status", "weird")[1]["error"]["code"] in ("bad_arg", "no_task")


def test_task_record_is_bounded_and_keeps_only_the_recent_done_entries(home):
    togi("task", "start", "--goal", "g")
    for name, n in (("goal", 501), ("next", 601), ("waiting", 601)):
        flag = "--" + name
        args = ["task", "start", "--force", "--goal", "x" * n] if name == "goal" else ["task", "update", flag, "x" * n]
        code, out = togi_code(*args)
        assert code == 1 and out["error"]["code"] == "too_long" and str(taskrec.LIMITS[name]) in out["error"]["message"]
    code, out = togi_code("task", "update", "--done", "d" * 301)
    assert code == 1 and out["error"]["code"] == "too_long"
    for i in range(27):
        togi("task", "update", "--done", f"step {i}")
    rec = togi("task", "show")["data"]
    assert len(rec["done"]) == taskrec.MAX_DONE and rec["done"][-1] == "step 26" and rec["done_dropped"] == 7
    assert len(json.dumps(taskrec.load())) < taskrec.MAX_BYTES


def test_task_commands_are_journaled_as_types_and_snapshots_are_kept(home):
    togi("task", "start", "--goal", "sk-live-private-goal")
    togi("task", "update", "--done", "paid 1234.56", "--next", "n")
    togi("task", "show")
    ents = [e for e in journal.read("_host") if str(e.get("command", "")).startswith("task")]
    assert [(e["command"], e["effect"]) for e in ents] == [("task start", "write"), ("task update", "write"), ("task show", "read")]
    assert ents[1]["args_shape"] == {"done": "list", "next": "str"}
    assert "sk-live" not in json.dumps(ents) and "1234.56" not in json.dumps(ents)
    hist = taskrec.history()
    assert [h["op"] for h in hist] == ["start", "update"] and hist[1]["record"]["done"] == ["paid 1234.56"]
    assert "goal: sk-live-private-goal" in taskrec.text_of(taskrec.load()) and "(no task record yet)" == taskrec.text_of(None)


def test_task_over_code_mode_and_mcp(notes, home):
    pytest.importorskip("mcp")
    p = run_script(home, """
        print(togi_task_show()["active"])
        togi_task_start(goal="job")
        togi_task_update(done="a", next="b")
        print(togi_task_show()["next"])
    """)
    assert p.returncode == 0, p.stderr
    assert p.stdout.split() == ["False", "b"]
    loaded = registry.load("notes")
    env, err = mcp_bridge.call_tool(loaded, "task_update", {"done": ["c", "d"], "waiting": "approval"})
    assert not err and env["data"]["done"] == ["a", "c", "d"] and env["data"]["waiting"] == "approval"
    env, err = mcp_bridge.call_tool(loaded, "task_show", {})
    assert env["data"]["goal"] == "job"
    env, err = mcp_bridge.call_tool(loaded, "task_start", {"goal": "again"})
    assert err and env["error"]["code"] == "task_exists"
    env, err = mcp_bridge.call_tool(loaded, "task_show", {"bogus": 1})
    assert err and env["error"]["code"] == "unknown_flag"
    assert {e["surface"] for e in journal.read("_host") if str(e.get("command", "")).startswith("task")} == {"run", "mcp"}


def test_the_index_skill_teaches_the_long_running_convention_and_the_answer_command(home):
    text = skills.index_text()
    assert "START EVERY SESSION WITH `togi task show`" in text and "togi task update --done" in text
    assert "togi answer --json" in text and "togi_answer" in text
    assert "answer" in registry.RESERVED_NAMES and "task" in registry.RESERVED_NAMES and "eval" in registry.RESERVED_NAMES


# ----------------------------------------------------------------------------- ablation switches + recording


def test_next_hints_and_compact_output_switches(notes, home, monkeypatch):
    code, on = togi_code("notes", "show", "--id", "n_9")
    assert on["next"].startswith("togi notes find")
    monkeypatch.setenv("TOGI_NEXT_HINTS", "0")
    code, off = togi_code("notes", "show", "--id", "n_9")
    assert code == 1 and "next" not in off and off["error"]["code"] == "note_not_found"
    monkeypatch.delenv("TOGI_NEXT_HINTS")
    monkeypatch.setenv("TOGI_COMPACT", "0")
    p = run_togi(home, "notes", "list", env_extra={"TOGI_COMPACT": "0"})
    assert p.stdout.count("\n") > 1 and json.loads(p.stdout)["ok"] is True       # pretty-printed, still the same envelope
    p = run_togi(home, "notes", "list")
    assert p.stdout.count("\n") == 1


def test_calls_record_their_arguments_only_inside_an_eval_sandbox(notes, home, monkeypatch):
    monkeypatch.setenv("TOGI_EVAL_RECORD", "1")                          # the switch alone, in a normal home, does nothing
    togi("notes", "add", "--text", "private text")
    e = [x for x in journal.read("notes") if x.get("command") == "add"][0]
    assert "args_full" not in e and "out_preview" not in e
    assert "private text" not in json.dumps(journal.read("notes"))
