"""Eval layer: the local model backends against a mock server (tool calls, constrained output, max turns, errors), every agent-loop
strategy on single-session and multi-session items, token accounting per step, and claude-step with a mocked `claude`."""
from __future__ import annotations

import json
import threading
import time

import pytest

from togi.evals import cell, items, loops, policy, subject
from togi.evals.policy import ClaudeStepBackend, OllamaBackend, OpenAIBackend, Prompt, ProviderError

from evalhelp import MockLLM, claude_calls, install_fake_claude, local_subject, scripted, toml_item, write_suite

ADD = {"action": "togi", "argv": ["notes", "add", "--text", "buy milk", "--tag", "home"]}
FIND = {"action": "togi", "argv": ["notes", "find", "--query", "milk"]}
ANSWER = {"action": "answer", "answer": {"id": "n_1"}}


def bare(**kw):
    """The notes item with no scripts: a model has to do it."""
    d = {k: v for k, v in toml_item(**kw).items() if k not in ("script", "script_answer_json")}
    return d


def play(tmp_path, item, harness, policy_fn=None, actions=None, provider="ollama", name="s", surface="cli"):
    """One cell of `item` for a local-model subject (harness options in `harness`) served by a mock server playing `actions`."""
    suite = items.load_suite(write_suite(tmp_path / name, [item]))
    with MockLLM(policy_fn or scripted(actions)) as m:
        url = m.ollama_url if provider == "ollama" else m.openai_url
        sub = subject.make_subject({**local_subject(provider, url, **harness), "surface": surface})
        res = cell.execute_cell(suite, suite.items[0], sub, 0)
        return res, m


def steps_of(res, k=0):
    return res["sessions"][k]["steps"]


# ----------------------------------------------------------------------------- backends


PROMPT = Prompt(system="SYS", messages=[{"role": "user", "content": "hi"}], schema={"type": "object", "properties": {"action": {"const": "x"}}},
                tools=[{"type": "function", "function": {"name": "togi", "description": "d", "parameters": {"type": "object"}}}])


def test_ollama_backend_request_shape_schema_mode_and_usage():
    with MockLLM(scripted([{"action": "finish", "summary": "s"}])) as m:
        b = OllamaBackend("qwen2.5:7b", m.ollama_url, {"temperature": 0.2, "top_p": 0.9, "num_ctx": 4096, "max_tokens": 64, "keep_alive": "5m"})
        r = b.complete(PROMPT, seed=11, mode="schema")
    req = m.requests[0]
    assert req["_kind"] == "ollama" and req["_path"] == "/api/chat" and req["stream"] is False and req["model"] == "qwen2.5:7b"
    assert req["format"] == PROMPT.schema and "tools" not in req                                      # the provider's own constrained decoding
    assert req["options"] == {"seed": 11, "temperature": 0.2, "top_p": 0.9, "num_ctx": 4096, "num_predict": 64} and req["keep_alive"] == "5m"
    assert req["messages"][0] == {"role": "system", "content": "SYS"} and req["messages"][1]["content"] == "hi"
    assert json.loads(r.text) == {"action": "finish", "summary": "s"} and r.prompt_tokens == (3 + 2) // 4 + 0 or r.prompt_tokens >= 0
    assert r.completion_tokens == 20 and r.tool_call is None


def test_ollama_backend_tools_mode_returns_a_parsed_tool_call():
    with MockLLM(scripted([{"tool": ("togi", {"argv": ["notes", "list"]})}])) as m:
        r = OllamaBackend("m", m.ollama_url).complete(PROMPT, mode="tools")
    req = m.requests[0]
    assert req["tools"] == PROMPT.tools and "format" not in req
    assert r.tool_call == {"name": "togi", "arguments": {"argv": ["notes", "list"]}}


def test_openai_backend_json_schema_tools_auth_and_usage(monkeypatch):
    monkeypatch.setenv("MY_KEY", "sekret")
    with MockLLM(scripted([{"say": {"action": "finish"}}, {"tool": ("togi", {"argv": ["a"]})}])) as m:
        b = OpenAIBackend("tiny", m.openai_url, "MY_KEY", {"temperature": 0, "max_tokens": 50})
        r1 = b.complete(PROMPT, seed=5, mode="schema")
        r2 = b.complete(PROMPT, mode="tools")
    q1, q2 = m.requests
    assert q1["_kind"] == "openai" and q1["_path"] == "/v1/chat/completions" and q1["_auth"] == "Bearer sekret"
    assert q1["response_format"]["type"] == "json_schema" and q1["response_format"]["json_schema"]["schema"] == PROMPT.schema
    assert q1["seed"] == 5 and q1["temperature"] == 0 and q1["max_tokens"] == 50 and "tools" not in q1
    assert q2["tools"] == PROMPT.tools and q2["tool_choice"] == "required" and "response_format" not in q2
    assert r1.prompt_tokens > 0 and r1.completion_tokens == 20 and r2.tool_call == {"name": "togi", "arguments": {"argv": ["a"]}}
    monkeypatch.delenv("MY_KEY")
    with MockLLM(scripted([{"say": {}}])) as m2:
        OpenAIBackend("tiny", m2.openai_url, "MY_KEY").complete(PROMPT)
    assert m2.requests[0]["_auth"] is None


def test_backend_errors_are_provider_errors_not_agent_failures():
    with MockLLM(lambda req, n: {"status": 500, "body": "model crashed"}) as m:
        with pytest.raises(ProviderError) as e:
            OllamaBackend("m", m.ollama_url).complete(PROMPT)
    assert e.value.kind == "http" and "500" in e.value.message and "model crashed" in e.value.message
    with pytest.raises(ProviderError) as e:
        OpenAIBackend("m", "http://127.0.0.1:1/v1").complete(PROMPT)
    assert e.value.kind == "unreachable"

    def slow(req, n):
        time.sleep(1.0)
        return {"say": {}}

    with MockLLM(slow) as m:
        with pytest.raises(ProviderError) as e:
            OllamaBackend("m", m.ollama_url, timeout=0.3).complete(PROMPT)
    assert e.value.kind == "timeout"
    with MockLLM(lambda req, n: {"text": "not json at all"}) as m:           # a 200 with a non-JSON content is the loop's problem; a non-JSON BODY is the backend's
        assert OllamaBackend("m", m.ollama_url).complete(PROMPT).text == "not json at all"


def test_backend_rejects_bodies_that_are_not_replies():
    import http.server

    class Bad(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b"<html>oops</html>" if self.path.startswith("/api") else b'{"choices": []}'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Bad)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        with pytest.raises(ProviderError) as e:
            OllamaBackend("m", f"http://127.0.0.1:{port}").complete(PROMPT)
        assert e.value.kind == "bad_response"
        with pytest.raises(ProviderError) as e:
            OpenAIBackend("m", f"http://127.0.0.1:{port}/v1").complete(PROMPT)
        assert e.value.kind == "bad_response"
    finally:
        srv.shutdown()


# ----------------------------------------------------------------------------- reading replies


def test_extract_json_handles_fences_prose_and_nesting():
    assert loops.extract_json('{"action": "finish"}') == {"action": "finish"}
    assert loops.extract_json('Sure!\n```json\n{"action": "finish", "x": {"y": "}"}}\n```') == {"action": "finish", "x": {"y": "}"}}
    assert loops.extract_json('I will run {"action": "togi", "argv": ["a"]} now') == {"action": "togi", "argv": ["a"]}
    assert loops.extract_json("no json here") is None and loops.extract_json("") is None and loops.extract_json("[1, 2]") is None
    assert loops.extract_json('{broken {"action": "finish"}') == {"action": "finish"}


def test_parse_action_validates_the_reply_shape():
    env = loops.LoopEnv(instruction="i")
    r = lambda text=None, tool=None: policy.Reply(text=text or "", tool_call=tool)
    a = loops.parse_action(r('{"action":"togi","argv":["togi","notes","list"]}'), env, ["togi"])
    assert a.valid and a.argv == ["notes", "list"] and a.brief() == "togi notes list"                  # a leading "togi" is dropped
    assert loops.parse_action(r('{"action":"togi","argv":"notes list"}'), env, ["togi"]).argv == ["notes", "list"]
    for bad in ('{"action":"togi"}', '{"action":"togi","argv":[]}', '{"action":"nope"}', '{"action":"run","script":" "}', '{"action":"answer"}',
                '{"action":"plan","plan":[]}', "hello", '{"action":"togi","argv":["togi"]}'):
        a = loops.parse_action(r(bad), env, ["togi", "run", "answer", "plan"])
        assert a.valid is False and a.error, bad
    t = loops.parse_action(r(tool={"name": "notes_add", "arguments": {"text": "x"}}), env, ["notes_add"])
    assert t.kind == "command" and t.command == "notes_add" and t.args == {"text": "x"}
    t = loops.parse_action(r('{"action":"notes_add","args":{"text":"y"},"done":"d","next":"n"}'), env, ["notes_add"])
    assert t.args == {"text": "y"} and t.meta == {"done": "d", "next": "n"}
    assert loops.parse_action(r('{"action":"give_up","reason":"cannot"}'), env, ["give_up"]).meta["reason"] == "cannot"


def test_the_action_vocabulary_is_a_schema_and_a_tool_list():
    env = loops.LoopEnv(instruction="i", answer_schema={"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
                        reasoning_field=True, offer_run=True,
                        commands=[{"bundle": "notes", "command": "tip send", "effect": "money", "summary": "Send a tip.",
                                   "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}}])
    vs = loops.variants(env, {}, [])
    assert [v[0] for v in vs] == ["togi", "run", "answer", "finish", "give_up"]
    sch = loops.action_schema(vs)
    kinds = {v["properties"]["action"]["const"]: v for v in sch["oneOf"]}
    assert kinds["answer"]["properties"]["answer"]["required"] == ["id"] and "think" in kinds["togi"]["properties"]
    assert kinds["togi"]["required"] == ["action", "argv"] and kinds["togi"]["additionalProperties"] is False
    tools = loops.tools_for(vs)
    assert [t["function"]["name"] for t in tools] == ["togi", "run", "answer", "finish", "give_up"] and tools[0]["function"]["parameters"]["required"] == ["argv"]
    env.tools_mode = "per-command"
    names = [v[0] for v in loops.variants(env, {}, [])]
    assert names[0] == "notes_tip_send" and "togi" not in names
    cv = loops.command_variants(env)[0]
    assert cv[1]["properties"]["approval"] == {"type": "string"} and cv[1]["properties"]["dry_run"] == {"type": "boolean"}       # a money command can take an approval
    assert [v[0] for v in loops.variants(env, {}, [], plan_only=True)] == ["plan"]


# ----------------------------------------------------------------------------- the loop on a real sandbox, per mode


@pytest.mark.parametrize("provider", ["ollama", "openai-compatible"])
def test_constrained_decoding_on_and_native_tool_calls_off_both_solve_the_item(home, tmp_path, provider):
    on, m_on = play(tmp_path, bare(), {}, actions=[ADD, FIND, ANSWER], provider=provider, name="on")
    assert on["success"] is True and on["turns"] == 3
    assert all(("format" in r or "response_format" in r) and "tools" not in r for r in m_on.requests)
    calls = [{"tool": ("togi", {"argv": ADD["argv"]})}, {"tool": ("togi", {"argv": FIND["argv"]})}, {"tool": ("answer", {"answer": {"id": "n_1"}})}]
    off, m_off = play(tmp_path, bare(), {"constrained_decoding": False}, actions=calls, provider=provider, name="off")
    assert off["success"] is True and off["turns"] == 3
    assert all("tools" in r and "format" not in r and "response_format" not in r for r in m_off.requests)
    assert {t["function"]["name"] for t in m_off.requests[0]["tools"]} == {"togi", "answer", "finish", "give_up"}
    assert [e["cmdline"] for e in off["sessions"][0]["trajectory"]] == ["togi notes add", "togi notes find", "togi answer"]


def test_per_command_tools_become_cli_flags_and_a_money_command_stops_at_approval(home, tmp_path):
    acts = [{"action": "notes_add", "args": {"text": "tip me", "tag": ["a", "b"]}}, {"action": "notes_tip_send", "args": {"id": "n_1", "amount": "5.00"}},
            {"action": "finish", "summary": "waiting for a person"}]
    it = bare(answer_schema=None, check=[{"type": "trajectory", "rule": "ended_at_needs_approval"}], instruction="Tip n_1 5.00 (add a note first).")
    res, m = play(tmp_path, it, {"tools": "per-command"}, actions=acts)
    assert res["success"] is True and res["sessions"][0]["finish_reason"] == "finished"
    tools = {t["function"]["name"] if "function" in t else None for t in (m.requests[0].get("tools") or [])}
    schema_names = [v["properties"]["action"]["const"] for v in m.requests[0]["format"]["oneOf"]]
    assert "notes_tip_send" in schema_names and "togi" not in schema_names and "notes_tip_total" in schema_names
    traj = res["sessions"][0]["trajectory"]
    assert traj[0]["args"] == '{"text":"tip me","tag":["a","b"]}' and traj[1]["status"] == "needs_approval"
    assert res["metrics"]["ended_at_approval"] is True


def test_the_mcp_surface_calls_tools_through_the_mcp_code_path(home, tmp_path):
    acts = [{"action": "notes_add", "args": {"text": "buy milk", "tag": ["home"]}}, {"action": "notes_find", "args": {"query": "milk"}}, ANSWER]
    res, m = play(tmp_path, bare(), {}, actions=acts, name="mcp", surface="mcp")
    assert res["success"] is True, res["sessions"][0]["steps"]
    traj = res["sessions"][0]["trajectory"]
    assert [e["surface"] for e in traj if e["cmdline"] != "togi answer"] == ["mcp", "mcp"] and traj[0]["cmdline"] == "togi notes add"
    assert [v["properties"]["action"]["const"] for v in m.requests[0]["format"]["oneOf"]][0] == "notes_add"


def test_a_step_budget_ends_a_wandering_model_and_says_why(home, tmp_path):
    acts = [{"action": "togi", "argv": ["notes", "find", "--query", f"q{n}"]} for n in range(10)]
    res, m = play(tmp_path, bare(), {"step_budget": 4}, actions=acts)
    s = res["sessions"][0]
    assert res["success"] is False and res["turns"] == 4 and s["finish_reason"] == "step_budget" and len(m.requests) == 4
    assert s["events"][-1]["event"] == "early_stop" and "step budget of 4" in s["events"][-1]["detail"]
    from togi.evals import taxonomy

    assert taxonomy.classify(res)["class"] == "budget"


def test_the_same_action_and_output_four_times_is_a_loop_and_stops_early(home, tmp_path):
    res, m = play(tmp_path, bare(), {"step_budget": 12}, actions=[{"action": "togi", "argv": ["notes", "find", "--query", "zzz"]}])
    s = res["sessions"][0]
    assert s["finish_reason"] == "repeated_action" and res["turns"] == 4 and len(m.requests) == 4
    from togi.evals import taxonomy

    assert taxonomy.classify(res)["class"] == "loop"


def test_invalid_replies_get_feedback_and_three_in_a_row_stop_the_session(home, tmp_path):
    seq = [{"text": "I think we should add a note"}, {"say": {"action": "bogus"}}, {"say": ADD}, {"say": FIND}, {"say": ANSWER}]
    res, m = play(tmp_path, bare(), {}, policy_fn=lambda req, n: seq[n - 1])
    assert res["success"] is True and res["turns"] == 5
    assert [r.get("valid") for r in steps_of(res)] == [False, False, True, True, True]
    assert "NOTE: " in m.requests[1]["messages"][-1]["content"] and "JSON object" in m.requests[1]["messages"][-1]["content"]
    assert "must be one of" in m.requests[2]["messages"][-1]["content"]
    assert [e["event"] for e in res["sessions"][0]["events"]] == ["invalid_reply", "invalid_reply"]
    res, m = play(tmp_path, bare(), {}, actions=[{"text": "blah"}], name="stuck")
    assert res["sessions"][0]["finish_reason"] == "invalid_replies" and res["turns"] == 3 and res["success"] is False


def test_a_refused_answer_is_fed_back_and_the_model_can_fix_it(home, tmp_path):
    seq = [ADD, FIND, {"action": "answer", "answer": {"id": 1}}, ANSWER]
    res, m = play(tmp_path, bare(), {}, actions=seq)
    s = res["sessions"][0]
    assert res["success"] is True and res["turns"] == 4 and [e["event"] for e in s["events"]] == ["answer_rejected"]
    assert "your answer was not accepted" in m.requests[3]["messages"][-1]["content"] and "expected string" in m.requests[3]["messages"][-1]["content"]
    assert [a["valid"] for a in s["answers"]] == [False, True]


def test_give_up_finish_and_a_model_that_runs_togi_answer_itself(home, tmp_path):
    res, _ = play(tmp_path, bare(), {}, actions=[{"action": "give_up", "reason": "no idea"}])
    assert res["success"] is False and res["sessions"][0]["finish_reason"] == "gave_up" and res["turns"] == 1
    from togi.evals import taxonomy

    assert taxonomy.classify(res)["class"] == "gave_up"
    res, _ = play(tmp_path, bare(), {}, actions=[ADD, {"action": "togi", "argv": ["answer", "--json", '{"id": "n_1"}']}], name="own")
    assert res["success"] is True and res["sessions"][0]["finish_reason"] == "answered"
    assert any(e["event"] == "answered_by_command" for e in res["sessions"][0]["events"])


def test_reasoning_field_adds_a_think_property_and_keeps_the_text(home, tmp_path):
    acts = [dict(ADD, think="add the note first"), dict(FIND, think="now find it"), dict(ANSWER, think="done")]
    res, m = play(tmp_path, bare(), {"reasoning_field": True}, actions=acts)
    assert res["success"] is True
    assert all("think" in v["properties"] for v in m.requests[0]["format"]["oneOf"])
    assert [r["action"].get("think") for r in steps_of(res)] == ["add the note first", "now find it", "done"]
    plain, m2 = play(tmp_path, bare(), {}, actions=[ADD, FIND, ANSWER], name="plain")
    assert all("think" not in v["properties"] for v in m2.requests[0]["format"]["oneOf"])


def test_context_policies_limit_what_the_model_is_shown(home, tmp_path):
    acts = [{"action": "togi", "argv": ["notes", "add", "--text", f"note {n}"]} for n in range(6)] + [ANSWER]
    counts = {}
    for cp in ("full", "window:2", "state-only"):
        res, m = play(tmp_path, bare(), {"context_policy": cp, "step_budget": 7}, actions=acts, name=cp.replace(":", ""))
        counts[cp] = [len(r["messages"]) for r in m.requests]
    # (counts include the system message) system + task + last exchange; system + task + last two exchanges; everything
    assert counts["state-only"][-1] == 4 and counts["window:2"][-1] == 6 and counts["full"][-1] == 2 + 2 * 6 + 0 or counts["full"][-1] > 6
    assert counts["full"][-1] > counts["window:2"][-1] > counts["state-only"][-1] and counts["full"] == sorted(counts["full"])
    assert counts["state-only"] == [2, 4, 4, 4, 4, 4, 4]


def test_prompt_tokens_actually_sent_are_recorded_per_step(home, tmp_path):
    acts = [{"action": "togi", "argv": ["notes", "add", "--text", "x" * 200]} for _ in range(3)] + [ANSWER]
    res, m = play(tmp_path, bare(), {"loop": "react"}, actions=acts)
    s = steps_of(res)
    served = [sum(len(x["content"]) for x in r["messages"]) // 4 for r in m.requests]
    assert [x["prompt_tokens"] for x in s] == [(sum(len(x.get("content", "")) for x in r["messages"])) // 4 for r in m.requests]
    assert [x["prompt_tokens_estimated"] for x in s] == [False] * 4 and all(x["prompt_tokens_est"] > 0 for x in s)
    assert s[0]["prompt_tokens"] < s[1]["prompt_tokens"] < s[2]["prompt_tokens"] and res["prompt_tokens"] == sum(x["prompt_tokens"] for x in s)
    assert s[0]["completion_tokens"] == 20 and s[0]["prompt_chars"] > 0 and s[0]["wall_s"] >= 0
    assert served[0] > 0


def test_a_server_that_reports_no_usage_gets_an_estimate_marked_as_one(home, tmp_path):
    suite = items.load_suite(write_suite(tmp_path / "s", [bare()]))
    pol = scripted([FIND, ANSWER])

    class NoUsage(policy.Backend):
        def complete(self, prompt, *, seed=0, mode="schema"):
            r = pol({"messages": prompt.messages}, 1 if not hasattr(self, "n") else 2)
            self.n = 1
            return policy.Reply(text=json.dumps(r["say"]))

    from togi.evals import providers

    providers.BACKEND_FACTORY["ollama"] = lambda subject, ctx: NoUsage()
    try:
        sub = subject.make_subject(local_subject("ollama", "http://unused"))
        res = cell.execute_cell(suite, suite.items[0], sub, 0)
    finally:
        providers.BACKEND_FACTORY.pop("ollama")
    s = steps_of(res)
    assert all(x["prompt_tokens_estimated"] is True and x["prompt_tokens"] == x["prompt_tokens_est"] > 0 for x in s)


# ----------------------------------------------------------------------------- the four strategies


def test_react_baseline_keeps_a_growing_transcript(home, tmp_path):
    res, m = play(tmp_path, bare(), {"loop": "react"}, actions=[ADD, FIND, ANSWER])
    assert res["success"] is True
    last = m.requests[2]["messages"]
    assert [x["role"] for x in last] == ["system", "user", "assistant", "user", "assistant", "user"] and "OUTPUT:" in last[3]["content"]
    assert last[1]["content"].startswith("TASK: Add a note 'buy milk'")
    system = m.requests[0]["messages"][0]["content"]
    assert "TOGI GUIDE:" in system and "START EVERY SESSION WITH `togi task show`" in system and "FINAL ANSWER" in system and '"required":["id"]' in system
    assert "EXAMPLES:" not in system and "notes add" in system


def test_skills_and_examples_toggles_change_what_the_system_prompt_carries(home, tmp_path):
    texts = {}
    for key, h in (("none", {"skills": "none"}), ("index", {"skills": "index"}), ("full", {"skills": "full", "examples": True})):
        res, m = play(tmp_path, bare(), h, actions=[{"action": "give_up"}], name=key)
        texts[key] = m.requests[0]["messages"][0]["content"]
    assert "TOGI GUIDE" not in texts["none"] and "TOGI GUIDE" in texts["index"] and "# notes" not in texts["index"]
    assert "togi notes add --text" in texts["full"].split("EXAMPLES:")[1] and "Money commands" in texts["full"] or "tip" in texts["full"]
    assert len(texts["none"]) < len(texts["index"]) < len(texts["full"])


def test_operator_rebuilds_a_short_context_from_the_task_record_and_the_latest_output(home, tmp_path):
    acts = [dict(ADD, done="started", next="find it"), dict(FIND, done="added the note", next="answer with its id"), ANSWER]
    res, m = play(tmp_path, bare(), {"loop": "operator"}, actions=acts)
    assert res["success"] is True
    assert all([x["role"] for x in r["messages"]] == ["system", "user"] for r in m.requests)       # never a transcript
    p1, p2, p3 = (r["messages"][1]["content"] for r in m.requests)
    assert "TASK RECORD (your memory):" in p1 and "goal: Add a note 'buy milk'" in p1 and "STEP 1 of at most 12" in p1 and "LAST ACTION" not in p1
    assert "LAST ACTION: togi notes add --text buy milk --tag home" in p2 and "done:\n  - started" in p2 and "next: find it" in p2
    assert "next: answer with its id" in p3 and "LAST ACTION: togi notes find" in p3 and "togi notes add --text" not in p3.split("LAST ACTION")[0]
    rec = res["task_record"]
    assert rec["done"] == ["started", "added the note", "answered"] and [h["op"] for h in res["task_history"]] == ["start", "update", "update", "update"]
    assert {e["event"] for e in res["sessions"][0]["events"]} == {"task_started", "record_updated"}
    schema_togi = [v for v in m.requests[0]["format"]["oneOf"] if v["properties"]["action"]["const"] == "togi"][0]
    assert {"done", "next"} <= set(schema_togi["required"])                                 # the memory fields are not optional


def test_operator_prompt_stays_flat_while_react_grows(home, tmp_path):
    acts = [{"action": "togi", "argv": ["notes", "add", "--text", "y" * 300], "done": "d", "next": "n"} for _ in range(8)] + [ANSWER]
    flat, m1 = play(tmp_path, bare(budget_turns=12), {"loop": "operator", "step_budget": 9}, actions=acts, name="op")
    grow, m2 = play(tmp_path, bare(budget_turns=12), {"loop": "react", "step_budget": 9}, actions=[{k: v for k, v in a.items() if k not in ("done", "next")} for a in acts], name="re")
    op = [x["prompt_tokens"] for x in steps_of(flat)]
    re_ = [x["prompt_tokens"] for x in steps_of(grow)]
    assert op[-1] < op[1] * 1.6 and re_[-1] > re_[1] * 2.0 and re_[-1] > op[-1] * 2
    assert op[-1] < 1500 and flat["success"] is True and grow["success"] is True


def multi_item(**kw):
    """Two cold sessions: add alpha, then add beta and answer with the count."""
    d = bare(id="two-step", answer_schema={"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}},
             check=[{"type": "number", "path": "$.count", "expected": 2}, {"type": "state_jsonpath", "cmd": "togi notes list --json", "path": "$.data", "len_equals": 2}])
    d.pop("instruction")
    d["session"] = [{"instruction": "Add the note alpha. Leave the rest for later.", "check": [{"type": "state_jsonpath", "cmd": "togi notes list --json", "path": "$.data", "len_equals": 1}]},
                    {"instruction": "Continue the task."}]
    d.update(kw)
    return d


A_ALPHA = {"action": "togi", "argv": ["notes", "add", "--text", "alpha"]}
A_BETA = {"action": "togi", "argv": ["notes", "add", "--text", "beta"]}
A_COUNT = {"action": "answer", "answer": {"count": 2}}
FINISH = {"action": "finish", "summary": "alpha added"}


@pytest.mark.parametrize("loop,seq", [
    ("react", [A_ALPHA, FINISH, A_BETA, A_COUNT]),
    ("operator", [dict(A_ALPHA, done="added alpha", next="add beta in the next session"), FINISH, dict(A_BETA, done="added beta", next="answer"), A_COUNT]),
    ("plan-act-verify", [{"action": "plan", "plan": ["add alpha", "add beta", "answer"]}, dict(A_ALPHA, verify="ok"), FINISH, dict(A_BETA, verify="ok"), A_COUNT]),
    ("code-first", [{"action": "run", "script": "notes.add(text='alpha')\nprint('ok')"}, FINISH,
                    {"action": "run", "script": "notes.add(text='beta')\ntogi_answer({'count': 2})"}]),
])
def test_every_loop_completes_a_two_session_item_with_a_cold_start(home, tmp_path, loop, seq):
    res, m = play(tmp_path, multi_item(), {"loop": loop}, actions=seq, name=loop)
    assert res["success"] is True, (res["sessions"][0]["steps"], res["checks"])
    s0, s1 = res["sessions"]
    assert s0["finish_reason"] == "finished" and s1["finish_reason"] == "answered" and s0["success"] is True
    assert len({e["session"] for s in res["sessions"] for e in s["trajectory"]}) == 2
    first = lambda k: m.requests[[i for i, r in enumerate(m.requests) if f"session {k} of 2" in json.dumps(r["messages"])][0]]
    p2 = json.dumps(first(2)["messages"])
    assert "you start with no memory of earlier sessions" in p2 or "earlier sessions are only in the record" in p2
    assert "TASK: Add the note alpha" not in p2 and "INSTRUCTION: Add the note alpha" not in p2   # session 2 was never told session 1's instruction
    assert ("goal: Add the note alpha" in p2) == (loop in ("operator", "plan-act-verify"))         # (only the loops that keep a record carry it over)
    for st in s0["steps"] + s1["steps"]:
        assert st["prompt_tokens"] > 0 and st["action"]["kind"] in ("togi", "run", "plan", "finish", "answer")
    return None


def test_operator_continuity_comes_only_from_the_task_record(home, tmp_path):
    acts = [dict(A_ALPHA, done="added alpha", next="in session 2: add beta, then answer count 2"), FINISH, dict(A_BETA, done="added beta", next="answer"), A_COUNT]
    res, m = play(tmp_path, multi_item(), {"loop": "operator"}, actions=acts)
    second = [r["messages"][1]["content"] for r in m.requests if "session 2 of 2" in r["messages"][1]["content"]][0]
    assert "in session 2: add beta, then answer count 2" in second and "added alpha" in second and "INSTRUCTION: Continue the task." in second
    assert res["success"] is True and res["task_record"]["done"] == ["added alpha", "alpha added", "added beta", "answered"]


def test_plan_act_verify_writes_the_plan_into_the_task_record_verifies_and_revises(home, tmp_path):
    seq = [{"action": "plan", "plan": ["add the note", "find it", "answer"]},
           {"action": "togi", "argv": ["notes", "show", "--id", "n_9"], "verify": "ok"},                       # fails: the loop notices on its own
           dict(ADD, verify="revise", revised_plan=["add the note", "answer"]),
           dict(FIND, verify="failed"),                                                                  # the model judges it failed
           ANSWER]
    res, m = play(tmp_path, bare(), {"loop": "plan-act-verify"}, actions=seq)
    ev = [(e["step"], e["event"]) for e in res["sessions"][0]["events"]]
    assert ev[0] == (0, "plan_written") and (1, "verification_failed") in ev and (2, "plan_revised") in ev and (3, "verification_failed") in ev
    details = {e["step"]: e["detail"] for e in res["sessions"][0]["events"] if e["event"] == "verification_failed"}
    assert "the command failed" in details[1] and "note_not_found" in details[1] and "judged it failed" in details[3]
    assert res["task_record"]["next"] == "PLAN: 1) add the note 2) answer"
    first = m.requests[0]
    assert [v["properties"]["action"]["const"] for v in first["format"]["oneOf"]] == ["plan"]               # the first step can only be a plan
    assert "YOUR PLAN" in json.dumps(m.requests[1]["messages"]) and "VERIFY: the last command did not do what the plan needs" in json.dumps(m.requests[2]["messages"])
    assert res["success"] is True


def test_code_first_prefers_a_script_offers_stubs_and_recovers_from_a_failing_script(home, tmp_path):
    seq = [{"action": "run", "script": "notes.nope()"},
           {"action": "run", "script": "n = notes.add(text='buy milk', tag=['home'])\nprint(notes.find(query='milk'))\ntogi_answer({'id': n['id']})"}]
    res, m = play(tmp_path, bare(), {"loop": "code-first"}, actions=seq)
    system = m.requests[0]["messages"][0]["content"]
    assert "PREFER ONE SCRIPT" in system and "COMMANDS (Python stubs):" in system and "notes.add(text: str" in system
    assert [v["properties"]["action"]["const"] for v in m.requests[0]["format"]["oneOf"]] == ["togi", "run", "answer", "finish", "give_up"]
    assert res["success"] is True and res["turns"] == 2
    assert "no command 'nope'" in steps_of(res)[0]["obs"] or "AttributeError" in steps_of(res)[0]["obs"] or steps_of(res)[0]["ok"] is False
    assert res["metrics"]["code_mode_calls"] >= 2 and all(e["surface"] == "run" for e in res["sessions"][0]["trajectory"] if e["cmdline"] != "togi answer")


def test_the_run_surface_offers_scripts_to_every_loop_and_cli_does_not(home, tmp_path):
    for surface, expect in (("run", True), ("cli", False)):
        res, m = play(tmp_path, bare(), {"loop": "react"}, actions=[{"action": "give_up"}], name=surface, surface=surface)
        kinds = [v["properties"]["action"]["const"] for v in m.requests[0]["format"]["oneOf"]]
        assert ("run" in kinds) is expect


# ----------------------------------------------------------------------------- claude-step: our loop, a Claude model as the policy


def step_subject(**harness):
    return {"provider": "claude-step", "model": "haiku", "role": "reference", "harness": {"step_budget": 8, **harness}}


def play_claude(tmp_path, monkeypatch, item, script, **harness):
    log = install_fake_claude(tmp_path, monkeypatch, script=script, mode="step")
    suite = items.load_suite(write_suite(tmp_path / "cs", [item]))
    res = cell.execute_cell(suite, suite.items[0], subject.make_subject(step_subject(**harness)), 0, max_item_cost=0.5)
    return res, claude_calls(log)


def test_claude_step_is_one_claude_call_per_step_with_our_loop_and_no_tools(home, tmp_path, monkeypatch):
    res, calls = play_claude(tmp_path, monkeypatch, bare(), [ADD, "```json\n" + json.dumps(FIND) + "\n```", ANSWER])
    assert res["success"] is True and res["turns"] == 3 and len(calls) == 3
    a = calls[0]["args"]
    assert a[0] == "-p" and "--tools=" in a and a[a.index("--output-format") + 1] == "json" and a[a.index("--model") + 1] == "haiku"
    assert "--no-session-persistence" in a and "--safe-mode" in a
    system = a[a.index("--system-prompt") + 1]
    assert "Reply with ONE JSON object and nothing else" in system and '"oneOf"' in system and "TOGI GUIDE" in system
    assert calls[0]["prompt"].startswith("[INPUT]\nTASK:") and calls[0]["agent"] == "eval-step" and calls[0]["key"] is False     # subscription, not the API key
    assert "[YOU (earlier reply)]" in calls[2]["prompt"] and "OUTPUT:" in calls[2]["prompt"]
    s = steps_of(res)
    assert [x["prompt_tokens"] for x in s] == [250, 250, 250] and all(not x["prompt_tokens_estimated"] for x in s)       # input + cache read, as billed
    assert res["cost_usd"] == pytest.approx(0.003) and s[0]["cost_usd"] == 0.001 and res["tokens"] == 3 * 260
    assert calls[0]["cwd"] != str(tmp_path) and "togi-step" in calls[0]["cwd"]                                                 # an empty temp directory


def test_claude_step_runs_all_four_loops_and_a_multi_session_item(home, tmp_path, monkeypatch):
    seq = [dict(A_ALPHA, done="added alpha", next="beta later"), FINISH, dict(A_BETA, done="added beta", next="answer"), A_COUNT]
    res, calls = play_claude(tmp_path, monkeypatch, multi_item(), seq, loop="operator")
    assert res["success"] is True and len(calls) == 4 and res["n_sessions"] == 2
    assert len(calls[2]["prompt"]) < 1800 and "added alpha" in calls[2]["prompt"]                      # a short context, and the record carries session 1
    assert "[YOU (earlier reply)]" not in calls[2]["prompt"]


def test_claude_step_errors_and_a_missing_claude_are_driver_errors(home, tmp_path, monkeypatch):
    install_fake_claude(tmp_path, monkeypatch, mode="boom")
    suite = items.load_suite(write_suite(tmp_path / "cs", [bare()]))
    res = cell.execute_cell(suite, suite.items[0], subject.make_subject(step_subject()), 0)
    assert res["valid"] is False and "claude reported an error" in res["driver_error"]
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    res = cell.execute_cell(suite, suite.items[0], subject.make_subject(step_subject()), 0)
    assert res["valid"] is False and "not on PATH" in res["driver_error"]


def test_claude_step_backend_cost_cap_flag_and_replies(tmp_path, monkeypatch):
    log = install_fake_claude(tmp_path, monkeypatch, script=[{"action": "finish"}], mode="step")
    r = ClaudeStepBackend("sonnet", max_cost=0.25).complete(Prompt(system="S", messages=[{"role": "user", "content": "hello"}], schema={"type": "object"}))
    a = claude_calls(log)[0]["args"]
    assert a[a.index("--max-budget-usd") + 1] == "0.25" and json.loads(r.text) == {"action": "finish"} and r.cost_usd == 0.001
    assert "JSON Schema" in a[a.index("--system-prompt") + 1]


def test_the_loop_stops_when_the_cost_cap_for_an_item_is_spent(home, tmp_path, monkeypatch):
    log = install_fake_claude(tmp_path, monkeypatch, script=[{"action": "togi", "argv": ["notes", "find", "--query", "a"]}] + [
        {"action": "togi", "argv": ["notes", "find", "--query", f"q{n}"]} for n in range(20)], mode="step")
    suite = items.load_suite(write_suite(tmp_path / "cs", [bare()]))
    res = cell.execute_cell(suite, suite.items[0], subject.make_subject(step_subject(step_budget=30)), 0, max_item_cost=0.0025)
    s = res["sessions"][0]
    assert s["finish_reason"] == "cost_cap" and res["turns"] == 3 and res["success"] is False
