"""The claude drivers (stone and hone) exercised against a fake `claude` executable: no API calls are made."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from togi import audit, hone, stone

from conftest import logged, make_diff, togi, togi_code

def test_stone_claude_driver_end_to_end(notes, home, fake_claude):
    r = stone.run_stone(notes, driver="claude", model="haiku", with_tests=False, task_ids=["missing-note"],
                        save=False, max_cost=0.5)
    run = r["tasks"][0]
    seen = logged(fake_claude)
    assert seen["args"][0] == "-p" and "--no-session-persistence" in seen["args"]
    assert seen["args"][seen["args"].index("--system-prompt") + 1] == "Run `togi skill` first. Use only `togi` commands."
    assert seen["args"][seen["args"].index("--output-format") + 1] == "json"
    assert "--tools=Bash" in seen["args"] and "--allowedTools=Bash(togi:*)" in seen["args"]
    assert seen["args"][seen["args"].index("--model") + 1] == "haiku"
    assert seen["instruction"].startswith("Show the note n_9")
    assert seen["agent"] == "stone" and seen["key"] is False        # subscription, never the API key
    assert Path(seen["cwd"]).name == "cwd" and "togi-stone" in seen["cwd"]   # empty sandbox cwd
    assert run["success"] is True and run["turns"] == 3 and run["cost_usd"] == 0.01 and run["tokens"] == 1120
    assert [j["command"] for j in run["journal"]] == ["togi notes show"] and run["journal"][0]["ok"] is False
    assert not (home / "state").exists()            # the agent's calls hit the sandbox journal, not ours


def test_claude_driver_flags_forbidden_attempts_via_the_journal(notes, home, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sneaky")
    run = stone.run_stone(notes, driver="claude", with_tests=False, task_ids=["missing-note"], save=False)["tasks"][0]
    assert run["forbidden"] == ["togi approve"] and run["success"] is False
    assert any(j["command"] == "togi approve" and j["status"] == "refused" for j in run["journal"])


def test_claude_driver_error_result_is_a_driver_error(notes, home, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "error")
    run = stone.run_stone(notes, driver="claude", with_tests=False, task_ids=["missing-note"], save=False)["tasks"][0]
    assert run["success"] is False and "claude reported an error" in run["driver_error"]


def test_budget_turns_apply_to_claude_runs(notes, home, fake_claude):
    t = notes / "stone" / "tasks" / "missing-note.toml"
    t.write_text(t.read_text().replace("budget_turns = 6", "budget_turns = 2"))
    run = stone.run_stone(notes, driver="claude", with_tests=False, task_ids=["missing-note"], save=False)["tasks"][0]
    assert run["turns"] == 3 and run["over_budget"] is True and run["success"] is False


def test_mcp_surface_command_uses_mcp_config(notes, home, fake_claude):
    stone.run_stone(notes, driver="claude", with_tests=False, task_ids=["missing-note"], save=False, surface="mcp")
    seen = logged(fake_claude)
    args = seen["args"]
    cfg = json.loads(args[args.index("--mcp-config") + 1])
    assert cfg["mcpServers"]["togi"]["command"] == sys.executable
    assert cfg["mcpServers"]["togi"]["args"] == ["-m", "togi", "mcp", "serve", "notes"]
    assert "--strict-mcp-config" in args and "--tools=" in args and "--allowedTools=mcp__togi,mcp__togi__*" in args
    assert "--safe-mode" not in args and args[args.index("--setting-sources") + 1] == ""
    prompt = args[args.index("--system-prompt") + 1]
    assert "mcp__togi__notes_skill" in prompt and "togi skill" not in prompt


def test_claude_timeout_kills_the_whole_process_group(notes, home, fake_claude, monkeypatch, tmp_path):
    """A hung agent (the benchmark had a 385 s run) is cut at the wall-clock limit, children included."""
    marker = tmp_path / "survivor"
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hang")
    monkeypatch.setenv("FAKE_CLAUDE_MARKER", str(marker))
    import time

    t0 = time.monotonic()
    run = stone.run_stone(notes, driver="claude", with_tests=False, task_ids=["missing-note"], save=False,
                          task_timeout=1.5)["tasks"][0]
    assert time.monotonic() - t0 < 20
    assert run["success"] is False and "exceeded 1.5s" in run["driver_error"]
    time.sleep(3.5)
    assert not marker.exists()          # the grandchild (like claude's MCP server) was killed with the group


def test_hone_claude_driver_writes_a_proposal(notes, home, fake_claude, monkeypatch, tmp_path):
    new = (notes / "togi_messages.toml").read_text().replace('message = "No note with id {id}."',
                                                              'message = "No note with id {id}. Try: togi notes list"')
    diff = make_diff(notes, {"togi_messages.toml": new})
    ans = tmp_path / "answer.txt"
    ans.write_text("RATIONALE:\nAgents hit note_not_found and had no hint.\nDIFF:\n```diff\n" + diff + "```\n")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hone")
    monkeypatch.setenv("FAKE_CLAUDE_ANSWER", str(ans))
    togi("notes", "show", "--id", "n_404", expect=1)   # real friction for the prompt
    out = togi("hone", "notes", "--model", "haiku")
    assert out["data"]["tier"] == "polish" and out["data"]["files"] == ["togi_messages.toml"]
    seen = logged(fake_claude)
    assert "--tools=" in seen["args"] and seen["args"][seen["args"].index("--model") + 1] == "haiku"
    assert seen["agent"] == "hone" and seen["key"] is False
    assert "note_not_found" in seen["instruction"] and "togi_messages.toml [polish]" in seen["instruction"]
    assert "class Store" not in seen["instruction"]
    meta = json.loads((home / "proposals" / out["data"]["proposal"] / "meta.json").read_text())
    assert meta["driver"] == "claude" and meta["model"] == "haiku"
    assert "no hint" in (home / "proposals" / out["data"]["proposal"] / "rationale.md").read_text()


def test_hone_claude_answer_touching_a_locked_file_is_rejected_and_logged(notes, home, fake_claude, monkeypatch, tmp_path):
    diff = make_diff(notes, {"stone/tasks/add-and-find.toml": (notes / "stone/tasks/add-and-find.toml").read_text() + "\n# x\n"})
    ans = tmp_path / "answer.txt"
    ans.write_text("RATIONALE:\nLoosen the check.\nDIFF:\n```diff\n" + diff + "```\n")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "hone")
    monkeypatch.setenv("FAKE_CLAUDE_ANSWER", str(ans))
    code, out = togi_code("hone", "notes")
    assert code == 2 and out["error"]["code"] == "locked_file"
    e = audit.read(notes)[-1]
    assert e["action"] == "reject" and e["files"] == ["stone/tasks/add-and-find.toml"] and e["tier"] == "locked"


def test_hone_without_claude_on_path_says_what_to_do(notes, home, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    code, out = togi_code("hone", "notes")
    assert code == 1 and out["error"]["code"] == "claude_missing" and "--driver script" in out["next"]
