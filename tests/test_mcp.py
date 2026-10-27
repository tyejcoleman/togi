"""Milestone 5: the MCP bridge (listing, schemas, guarded calls over real stdio) and `togi bench`."""
from __future__ import annotations

import asyncio
import json
import sys

import pytest

from togi import mcp_bridge, registry
from togi.errors import TogiError

from conftest import clean_env, lock_bundle, logged, togi, togi_code

mcp = pytest.importorskip("mcp")


@pytest.fixture
def loaded(notes, home):
    return registry.load("notes")


def specs(loaded):
    return {s["name"]: s for s in mcp_bridge.tool_specs(loaded)}


def test_tool_listing_names_descriptions_and_schemas(loaded):
    s = specs(loaded)
    assert set(s) == {"notes_skill", "notes_add", "notes_list", "notes_show", "notes_find", "notes_delete",
                      "notes_tip_send", "notes_tip_total", "notes_seed", "notes_test_reset"}
    show = s["notes_show"]
    assert show["description"] == "Show one note in full. [effect: read]"
    assert show["inputSchema"]["required"] == ["id"] and "approval" not in show["inputSchema"]["properties"]
    assert "dry_run" not in show["inputSchema"]["properties"]           # read: nothing to preview
    tip = s["notes_tip_send"]
    assert "[effect: money]" in tip["description"] and "approval" in tip["description"]
    assert tip["inputSchema"]["properties"]["approval"]["type"] == "string"
    assert tip["inputSchema"]["properties"]["amount"]["format"] == "decimal"
    assert tip["inputSchema"]["required"] == ["id", "amount"]
    assert s["notes_add"]["inputSchema"]["properties"]["tag"] == {"type": "array", "items": {"type": "string"}}
    assert s["notes_list"]["inputSchema"]["properties"]["sort"]["enum"] == ["newest", "oldest"]
    assert "dry_run" in s["notes_add"]["inputSchema"]["properties"]


def test_cli_lists_the_mcp_tools(notes, home):
    out = togi("mcp", "tools", "notes")["data"]
    assert [t["name"] for t in out][:4] == ["notes_skill", "notes_add", "notes_list", "notes_show"]


def test_call_tool_goes_through_the_host_guards(loaded, home):
    lock_bundle("notes")      # the money path needs a verified lock
    env, err = mcp_bridge.call_tool(loaded, "notes_add", {"text": "hello", "tag": ["a"]})
    assert err is False and env["data"]["id"] == "n_1"
    env, err = mcp_bridge.call_tool(loaded, "notes_tip_send", {"id": "n_1", "amount": "5.00"})
    assert err is True and env["status"] == "needs_approval" and env["approval"]["id"].startswith("ap_")
    assert mcp_bridge.call_tool(loaded, "notes_tip_total", {})[0]["data"]["count"] == 0   # not executed
    env, err = mcp_bridge.call_tool(loaded, "notes_tip_send", {"id": "n_1", "amount": "5.00",
                                                              "approval": env["approval"]["id"]})
    assert err is True and env["error"]["code"] == "approval_pending"
    env, err = mcp_bridge.call_tool(loaded, "notes_show", {"id": "n_404"})
    assert err and env["error"]["code"] == "note_not_found"
    env, err = mcp_bridge.call_tool(loaded, "notes_show", {"idd": "x"})
    assert err and env["error"]["code"] == "unknown_flag"
    env, err = mcp_bridge.call_tool(loaded, "notes_nope", {})
    assert err and env["error"]["code"] == "unknown_command"
    env, err = mcp_bridge.call_tool(loaded, "notes_delete", {"id": "n_1", "dry_run": True})
    assert not err and env["data"]["dry_run"] is True


def test_mcp_server_over_real_stdio(notes, home):
    lock_bundle("notes")      # the money path needs a verified lock
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "togi", "mcp", "serve", "notes"],
                                       env=clean_env(home, TOGI_SESSION="mcp-test", TOGI_AGENT="mcp-client"))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = [t.name for t in tools.tools]
                added = await session.call_tool("notes_add", {"text": "via mcp"})
                shown = await session.call_tool("notes_show", {"id": "n_404"})
                money = await session.call_tool("notes_tip_send", {"id": "n_1", "amount": "5.00"})
                return names, added, shown, money, tools.tools

    names, added, shown, money, tool_objs = asyncio.run(go())
    assert "notes_tip_send" in names and "notes_skill" in names and len(names) == 10
    assert tool_objs[0].inputSchema["type"] == "object"
    assert added.isError is False and json.loads(added.content[0].text) == {
        "ok": True, "data": {"id": "n_1", "text": "via mcp", "tags": []}}
    assert shown.isError is True and json.loads(shown.content[0].text)["error"]["code"] == "note_not_found"
    assert money.isError is True and json.loads(money.content[0].text)["status"] == "needs_approval"
    lines = [json.loads(l) for l in (home / "state" / "notes" / "journal.jsonl").read_text().splitlines()]
    calls = [l for l in lines if l["event"] == "call"]
    assert [(l["surface"], l["command"], l["status"]) for l in calls] == [
        ("mcp", "add", "ok"), ("mcp", "show", "error"), ("mcp", "tip send", "needs_approval")]
    assert {l["session"] for l in calls} == {"mcp-test"} and {l["agent"] for l in calls} == {"mcp-client"}
    assert togi("notes", "tip", "total")["data"]["count"] == 0


def test_missing_mcp_package_is_a_clear_error(notes, home, monkeypatch):
    monkeypatch.setitem(sys.modules, "mcp.server.fastmcp", None)
    code, out = togi_code("mcp", "serve", "notes")
    assert code == 1 and out["error"]["code"] == "mcp_missing" and out["next"] == "pip install mcp"


# ----------------------------------------------------------------------------- bench


def test_bench_uses_one_driver_for_every_surface_claude_by_default(notes, home, fake_claude):
    """Item 17: the benchmark that compared a script-driven cli row with claude-driven run/mcp rows was meaningless."""
    out = togi("bench", "notes", "--surfaces", "cli,run,mcp", "--tasks", "missing-note")["data"]
    assert out["driver"] == "claude" and {r["driver"] for r in out["surfaces"].values()} == {"claude"}
    assert set(out["surfaces"]) == {"cli", "run", "mcp"} and "claude" in out["text"]
    args = logged(fake_claude)["args"]                       # the last surface driven was mcp
    assert "--safe-mode" not in args and args[args.index("--max-budget-usd") + 1] == "0.5"    # default $0.50 cap


def test_bench_refuses_the_script_driver_for_several_surfaces(notes, home):
    code, out = togi_code("bench", "notes", "--surfaces", "cli,run", "--driver", "script")
    assert code == 1 and out["error"]["code"] == "bad_arg" and "one surface" in out["next"]
    code, out = togi_code("bench", "notes", "--driver", "script")                 # the default surfaces are all three
    assert code == 1 and out["error"]["code"] == "bad_arg"
    from togi import bench

    with pytest.raises(TogiError):
        bench.run_bench(notes, surfaces=["cli", "run"], runs=1, model=None, driver="script", task_ids=None)


def test_bench_script_driver_is_fine_for_a_single_surface(notes, home):
    for surface, turns in (("cli", 2), ("run", 1)):
        out = togi("bench", "notes", "--surfaces", surface, "--runs", "2")["data"]
        row = out["surfaces"][surface]
        assert out["driver"] == "script" and row["driver"] == "script" and row["runs"] == 6
        assert row["success_rate"] == 1.0 and row["forbidden"] == 0 and row["median_turns"] == turns
    assert (home / "bench" / "notes").is_dir() and not (home / "state").exists()


def test_bench_has_a_per_task_wall_clock_limit_and_cost_cap_by_default(notes, home, monkeypatch):
    from togi import bench, stone

    seen = []

    def fake_run_stone(repo, **kw):
        seen.append(kw)
        return {"id": "st_x", "summary": {"nonholdout": {"runs": 0, "successes": 0}, "holdout": {"runs": 0, "successes": 0},
                                          "median_turns": None, "median_tokens": None, "total_cost_usd": None,
                                          "median_cost_usd": None, "wall_s": 0, "forbidden": 0, "skipped": 0,
                                          "driver_errors": 0}, "tasks": []}

    monkeypatch.setattr(stone, "run_stone", fake_run_stone)
    out = bench.run_bench(notes, surfaces=["cli", "mcp"], runs=1, model="haiku", driver=None, task_ids=None)
    assert [k["driver"] for k in seen] == ["claude", "claude"]
    assert all(k["task_timeout"] == 180.0 and k["max_cost"] == 0.5 for k in seen)
    assert out["task_timeout_s"] == 180.0 and out["max_cost_per_task_usd"] == 0.5
    seen.clear()
    bench.run_bench(notes, surfaces=["run"], runs=1, model=None, driver=None, task_ids=None, task_timeout=30, max_cost=0.1)
    assert seen[0]["task_timeout"] == 30 and seen[0]["max_cost"] == 0.1 and seen[0]["driver"] == "script"


def test_bench_timeout_cuts_a_hung_agent(notes, home, fake_claude, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    monkeypatch.setenv("FAKE_CLAUDE_MARKER", str(tmp_path / "survivor"))
    import time

    t0 = time.monotonic()
    out = togi("bench", "notes", "--surfaces", "cli,run", "--tasks", "missing-note", "--task-timeout", "1")["data"]
    assert time.monotonic() - t0 < 60
    assert all(r["driver_errors"] == 1 and r["success"] == 0 for r in out["surfaces"].values())


def test_bench_reports_skipped_tasks(notes, home):
    t = notes / "stone" / "tasks" / "cli-only.toml"
    t.write_text('id = "cli-only"\ninstruction = "x"\nscript = ["togi notes list"]\n')
    out = togi("bench", "notes", "--surfaces", "run", "--driver", "script")["data"]
    assert out["surfaces"]["run"]["skipped"] == 1 and out["surfaces"]["run"]["runs"] == 3


def test_bench_rejects_unknown_surface(notes, home):
    code, out = togi_code("bench", "notes", "--surfaces", "cli,smoke")
    assert code == 1 and out["error"]["code"] == "bad_arg"


# ----------------------------------------------------------------------------- item 16: the MCP surface works for an agent


def serve(home, name="notes", **env):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    return stdio_client(StdioServerParameters(command=sys.executable, args=["-m", "togi", "mcp", "serve", name],
                                              env=clean_env(home, TOGI_AGENT="mcp-client", **env)))


def mcp_run(home, coro_fn, name="notes", **env):
    from mcp import ClientSession

    async def go():
        async with serve(home, name, **env) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await coro_fn(session)

    return asyncio.run(go())


def test_skill_tool_serves_the_guide_and_topics_over_mcp(notes, home):
    """`togi skill notes` has an MCP twin, `notes_skill`; the agent that saw no such tool asked for `togi_skill`."""
    async def fn(session):
        main = await session.call_tool("notes_skill", {})
        topic = await session.call_tool("notes_skill", {"topic": "money"})
        bad = await session.call_tool("notes_skill", {"topic": "nope"})
        extra = await session.call_tool("notes_skill", {"nonsense": 1})
        tools = {t.name: t for t in (await session.list_tools()).tools}
        return main, topic, bad, extra, tools["notes_skill"]

    main, topic, bad, extra, spec = mcp_run(home, fn)
    from togi import skills
    from togi.manifest import load_manifest

    man = load_manifest(notes)
    assert main.isError is False and main.content[0].text == skills.bundle_text(man)      # exactly `togi skill notes`
    assert "Topics (`togi skill notes <topic>`)" in main.content[0].text
    assert topic.isError is False and topic.content[0].text == skills.topic_text(man, "money")
    assert bad.isError is True and json.loads(bad.content[0].text)["error"]["code"] == "unknown_topic"
    assert "money" in json.loads(bad.content[0].text)["error"]["message"]
    assert extra.isError is True and json.loads(extra.content[0].text)["error"]["code"] == "bad_arg"
    assert spec.inputSchema["properties"]["topic"]["type"] == "string" and "required" not in spec.inputSchema
    assert "FIRST" in spec.description


def test_skill_tool_for_a_bundle_without_a_skill_file_is_generated_from_the_commands(notes, home):
    (notes / "skills" / "SKILL.md").unlink()
    loaded = registry.load("notes")
    env, err = mcp_bridge.call_tool(loaded, "notes_skill", {})
    assert err is False and "notes.add(" in env["data"]["text"] and "generated from the commands" in env["data"]["text"]


def test_a_bundle_command_named_skill_keeps_its_name(tmp_path, home):
    repo = tmp_path / "sk"
    (repo / "src" / "sk").mkdir(parents=True)
    (repo / "togi.toml").write_text('[bundle]\nname="sk"\nmodule="sk.b:bundle"\nsummary="x"\n')
    (repo / "src" / "sk" / "__init__.py").write_text("")
    (repo / "src" / "sk" / "b.py").write_text(
        "from togi import Bundle\nbundle = Bundle('sk')\n@bundle.command('skill', effect='read')\n"
        "def skill() -> dict:\n    '''Own command.'''\n    return {'own': True}\n")
    togi("add", str(repo))
    loaded = registry.load("sk")
    names = [s["name"] for s in mcp_bridge.tool_specs(loaded)]
    assert names == ["sk_skill_guide", "sk_skill"]
    assert mcp_bridge.call_tool(loaded, "sk_skill", {})[0]["data"] == {"own": True}
    assert "own" not in mcp_bridge.call_tool(loaded, "sk_skill_guide", {})[0]["data"]["text"]


def test_server_instructions_point_at_the_skill_tool(notes, home):
    from mcp import ClientSession

    async def go():
        async with serve(home) as (r, w):
            async with ClientSession(r, w) as session:
                return (await session.initialize()).instructions

    text = asyncio.run(go())
    assert "`notes_skill`" in text and "approval" in text and "Tiny notes" in text


def test_server_starts_fast_and_answers_the_handshake_before_the_bundle_is_imported(notes, home, tmp_path):
    """A bundle that takes seconds to import must not make the server look dead: initialize returns at once."""
    import time

    repo = tmp_path / "slow"
    (repo / "src" / "slow").mkdir(parents=True)
    (repo / "togi.toml").write_text('[bundle]\nname="slow"\nmodule="slow.b:bundle"\nsummary="Slow to import."\n')
    (repo / "src" / "slow" / "__init__.py").write_text("")
    (repo / "src" / "slow" / "b.py").write_text(
        "import time\ntime.sleep(4)\nfrom togi import Bundle\nbundle = Bundle('slow')\n"
        "@bundle.command('ping', effect='read')\ndef ping() -> dict:\n    '''Ping.'''\n    return {'pong': 1}\n")
    togi("add", str(repo))
    from mcp import ClientSession

    async def go():
        t0 = time.monotonic()
        async with serve(home, "slow") as (r, w):
            async with ClientSession(r, w) as session:
                init = await session.initialize()
                t_init = time.monotonic() - t0
                tools = await session.list_tools()
                res = await session.call_tool("slow_ping", {})
                return t_init, [t.name for t in tools.tools], res, init.instructions

    t_init, names, res, instr = asyncio.run(go())
    assert t_init < 3.0, f"initialize took {t_init:.1f}s: it waited for the bundle import"
    assert names == ["slow_skill", "slow_ping"] and json.loads(res.content[0].text)["data"] == {"pong": 1}
    assert "Slow to import" in instr


def test_a_broken_bundle_still_connects_and_reports_why(notes, home, tmp_path):
    repo = tmp_path / "broken"
    (repo / "src" / "broken").mkdir(parents=True)
    (repo / "togi.toml").write_text('[bundle]\nname="broken"\nmodule="broken.b:bundle"\nsummary="Breaks on import."\n')
    (repo / "src" / "broken" / "__init__.py").write_text("")
    (repo / "src" / "broken" / "b.py").write_text("raise RuntimeError('import exploded')\n")
    togi("add", str(repo))

    async def fn(session):
        try:
            tools = await session.list_tools()
            return "listed", [t.name for t in tools.tools]
        except Exception as e:
            return "error", str(e)

    kind, detail = mcp_run(home, fn, "broken")
    assert kind == "error" and "bundle_import_failed" in detail and "import exploded" in detail

    async def call(session):
        return await session.call_tool("broken_ping", {})

    res = mcp_run(home, call, "broken")
    assert res.isError is True and "bundle_import_failed" in res.content[0].text


def test_a_bundle_that_prints_cannot_corrupt_the_protocol_stream(notes, home, tmp_path):
    repo = tmp_path / "chatty"
    (repo / "src" / "chatty").mkdir(parents=True)
    (repo / "togi.toml").write_text('[bundle]\nname="chatty"\nmodule="chatty.b:bundle"\nsummary="Prints."\n')
    (repo / "src" / "chatty" / "__init__.py").write_text("")
    (repo / "src" / "chatty" / "b.py").write_text(
        "import os, sys\nprint('hello from import')\nos.write(1, b'raw fd write at import\\n')\n"
        "from togi import Bundle\nbundle = Bundle('chatty')\n"
        "@bundle.command('talk', effect='read')\ndef talk() -> dict:\n    '''Talk.'''\n"
        "    print('hello from a command')\n    sys.stdout.write('more\\n')\n    os.write(1, b'raw fd write\\n')\n"
        "    return {'said': 'ok'}\n")
    togi("add", str(repo))

    async def fn(session):
        tools = await session.list_tools()
        res = await session.call_tool("chatty_talk", {})
        again = await session.call_tool("chatty_talk", {})
        return [t.name for t in tools.tools], res, again

    names, res, again = mcp_run(home, fn, "chatty")
    assert names == ["chatty_skill", "chatty_talk"]
    assert res.isError is False and json.loads(res.content[0].text)["data"] == {"said": "ok"}
    assert json.loads(again.content[0].text)["data"] == {"said": "ok"}


def test_mcp_results_are_cut_at_8kb_with_a_note(notes, home):
    async def fn(session):
        await session.call_tool("notes_seed", {"count": 400})
        return (await session.call_tool("notes_list", {}), await session.call_tool("notes_show", {"id": "x" * 40000}))

    big, err = mcp_run(home, fn)
    text = big.content[0].text
    assert len(text.encode()) <= 8192 + 400 and big.isError is False
    env = json.loads(text)
    assert env["truncated"] is True and "8192" in env["more"] and "narrower" in env["more"]
    etext = err.content[0].text
    assert err.isError is True and len(etext.encode()) <= 8192 + 400 and json.loads(etext)["error"]["code"] == "note_not_found"
    # the in-process path used by tests and the script driver agrees
    loaded = registry.load("notes")
    env, _ = mcp_bridge.call_tool(loaded, "notes_list", {})
    from togi import output

    assert env.get("truncated") is True and len(output.dumps(env).encode()) <= 8192


def test_mcp_server_handshake_is_quick(notes, home):
    import time

    from mcp import ClientSession

    async def go():
        t0 = time.monotonic()
        async with serve(home) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                await session.list_tools()
                return time.monotonic() - t0

    assert asyncio.run(go()) < 6.0
