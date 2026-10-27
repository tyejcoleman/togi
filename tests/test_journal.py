"""Journal shape (types only, no values), retry linking, friction summary."""
from __future__ import annotations

import json

import pytest

from togi import journal

from conftest import togi, togi_code

KEYS = {"ts", "session", "agent", "surface", "bundle", "command", "args_shape", "args_hash", "ok", "status",
        "error_code", "duration_ms", "out_bytes", "out_tokens_est", "retry_of"}


def lines(home, bundle="notes"):
    p = home / "state" / bundle / "journal.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_journal_line_has_the_spec_shape(notes, home):
    togi("notes", "add", "--text", "SECRET-VALUE", "--tag", "T")
    togi("notes", "find", "--query", "SECRET-VALUE")
    add, find = [l for l in lines(home) if l["event"] == "call"][:2]
    assert KEYS <= set(add)
    assert add["bundle"] == "notes" and add["command"] == "add" and add["surface"] == "cli"
    assert add["session"] == "test-session" and add["ok"] is True and add["status"] == "ok"
    assert add["args_shape"] == {"text": "str", "tag": "list[str]"}
    assert add["args_hash"] is None  # write commands carry no hash
    assert find["args_shape"] == {"query": "str"} and len(find["args_hash"]) == 16  # read: hashed
    assert find["out_bytes"] > 0 and find["out_tokens_est"] == (find["out_bytes"] + 3) // 4
    raw = (home / "state" / "notes" / "journal.jsonl").read_text()
    assert "SECRET-VALUE" not in raw and '"T"' not in raw  # values never enter the journal


def test_same_read_args_hash_equal_different_args_differ(notes, home):
    togi("notes", "find", "--query", "a")
    togi("notes", "find", "--query", "a")
    togi("notes", "find", "--query", "b")
    h = [l["args_hash"] for l in lines(home)]
    assert h[0] == h[1] != h[2]


def test_agent_env_recorded(notes, home, monkeypatch):
    monkeypatch.setenv("TOGI_AGENT", "claude-test")
    togi("notes", "list")
    assert lines(home)[-1]["agent"] == "claude-test"


def test_error_codes_status_and_retry_of(notes, home):
    togi("notes", "show", "--id", "n_1", expect=1)
    togi("notes", "add", "--text", "x")
    togi("notes", "show", "--id", "n_1")  # succeeds now: retry of the earlier failure
    ls = [l for l in lines(home) if l["command"] == "show"]
    assert ls[0]["ok"] is False and ls[0]["error_code"] == "note_not_found" and ls[0]["retry_of"] is None
    assert ls[1]["ok"] is True and ls[1]["retry_of"] == ls[0]["ts"]
    togi("notes", "show", "--id", "n_1")  # previous attempt succeeded -> not a retry
    assert [l for l in lines(home) if l["command"] == "show"][2]["retry_of"] is None


def test_retry_is_per_session_and_within_120s(notes, home, monkeypatch):
    togi("notes", "show", "--id", "n_9", expect=1)
    monkeypatch.setenv("TOGI_SESSION", "other")
    togi("notes", "show", "--id", "n_9", expect=1)
    ls = [l for l in lines(home) if l["command"] == "show"]
    assert ls[1]["retry_of"] is None  # different session
    # age the first entry beyond the window
    p = home / "state" / "notes" / "journal.jsonl"
    old = json.loads(p.read_text().splitlines()[0])
    old["ts"] = "2020-01-01T00:00:00.000Z"
    rest = p.read_text().splitlines()[1:]
    p.write_text("\n".join([json.dumps(old)] + rest) + "\n")
    monkeypatch.setenv("TOGI_SESSION", "test-session")
    togi("notes", "show", "--id", "n_9", expect=1)
    assert [l for l in lines(home) if l["command"] == "show"][-1]["retry_of"] is None


def test_note_events_do_not_count_as_calls(notes, home):
    togi("notes", "add", "--text", "x")  # the add command calls ctx.journal_note
    ev = [l["event"] for l in lines(home)]
    assert "note" in ev and ev.count("call") == 1
    assert journal.friction(lines(home))["calls"] == 1


def test_friction_summary(notes, home):
    togi("notes", "show", "--id", "n_1", expect=1)
    togi("notes", "show", "--id", "n_1", expect=1)
    togi("notes", "show", "--idd", "n_1", expect=1)           # unknown flag
    togi("notes", "shw", "--id", "n_1", expect=1)              # unknown command
    togi("notes", "tip", "send", "--help")                     # help lookup
    togi("notes", "seed", "--count", "200")
    togi("notes", "list", "--limit", "200")                    # truncated by the 8 KB cap
    f = togi("journal", "notes", "--friction")["data"]
    by_cmd = {c["command"]: c for c in f["commands"]}
    assert by_cmd["show"]["calls"] == 2 and by_cmd["show"]["errors"] == 2 and by_cmd["show"]["error_rate"] == 1.0
    assert {"code": "note_not_found", "count": 2} in f["top_error_codes"]
    assert f["repeated_retries"] == [{"command": "show", "count": 2}] or f["repeated_retries"][0]["command"] == "show"
    assert {"command": "show", "flag": "--idd", "count": 1} in f["unknown_flags"]
    assert f["unknown_commands"] == [{"attempted": "shw", "count": 1}]
    assert f["help_lookups"] == [{"command": "tip send", "count": 1}]
    assert f["truncated_outputs"] == [{"command": "list", "count": 1}]
    assert f["calls"] == 4 and f["error_rate"] == 0.5


def test_raw_journal_and_since(notes, home):
    togi("notes", "add", "--text", "x")
    out = togi("journal", "notes")
    assert out["data"][-1]["command"] == "add"
    assert togi("journal", "notes", "--since", "1h")["data"]
    assert togi("journal", "notes", "--since", "2999-01-01")["data"] == []
    code, bad = togi_code("journal", "--since", "yesterday-ish")
    assert code == 1 and bad["error"]["code"] == "bad_arg"
    assert len(togi("journal", "notes", "--limit", "1")["data"]) == 1


def test_args_shape_for_json_surface_types(notes, home):
    togi("notes", "tip", "send", "--id", "n_1", "--amount", "1.5", expect=3)
    last = lines(home)[-1]
    assert last["args_shape"] == {"id": "str", "amount": "decimal"} and last["effect"] == "money"


# ----------------------------------------------------------------------------- item 13: unknown-command/flag records hold no values


def raw_journal(home, bundle="notes"):
    return (home / "state" / bundle / "journal.jsonl").read_text()


def test_unknown_command_records_hold_types_not_values(notes, home):
    togi_code("notes", "sk_live_TOPSECRET99", "--amount", "5.123456")
    togi_code("notes", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6", "7", "hunter2")
    togi_code("notes", "8271.9364", "4242424242")
    ents = [l for l in lines(home) if l["event"] == "unknown_command"]
    assert [e["attempted"] for e in ents] == ["<str>", "<str> <num> <str>", "<num> <num>"]
    raw = raw_journal(home)
    for secret in ("TOPSECRET99", "sk_live", "a1b2c3d4", "hunter2", "5.123456", "8271.9364", "4242424242"):
        assert secret not in raw, secret


def test_a_misspelt_command_word_is_kept_because_it_is_the_friction_signal(notes, home):
    code, out = togi_code("notes", "tip", "sned", "--id", "n_1")
    ents = [l for l in lines(home) if l["event"] == "unknown_command"]
    assert ents and ents[0]["attempted"] == "tip sned"                      # near-miss of real command words: kept
    assert "sned" in out["error"]["message"]                                 # and the agent is still told what it typed
    fr = journal.friction(lines(home))
    assert fr["unknown_commands"] == [{"attempted": "tip sned", "count": 1}]


def test_unknown_flag_records_keep_the_flag_name_but_not_its_value(notes, home):
    togi_code("notes", "add", "--text", "x", "--sk-live-ZZZ99=hello-secret")
    togi_code("notes", "add", "--text", "x", "--verbos", "also-secret")
    togi_code("notes", "add", "--text", "x", "--" + "z" * 80 + "=v")
    ents = [l for l in lines(home) if l["event"] == "unknown_flag"]
    assert [e["attempted"] for e in ents] == ["--sk-live-ZZZ99", "--verbos", "--<flag>"]    # names kept; no '=value'
    raw = raw_journal(home)
    assert "hello-secret" not in raw and "also-secret" not in raw and "zzzz" not in raw


def test_friction_still_reports_unknown_flags(notes, home):
    togi_code("notes", "add", "--text", "x", "--tgs", "a")
    fr = journal.friction(lines(home))
    assert fr["unknown_flags"] == [{"command": "add", "flag": "--tgs", "count": 1}]


def test_shapes_unit():
    assert journal.argv_shape(["ap_1", "--ttl", "15m", "--ttl=15m", "--n=5", "-h", "-5", "3.5"]) == [
        "<str>", "--ttl", "<str>", "--ttl=<str>", "--n=<num>", "-<short>", "<num>", "<num>"]
    assert journal.sanitize_attempt("--tag=SECRET") == "--tag"
    assert journal.sanitize_attempt("drop setle d_7f3a 5.00", {"drop", "settle"}) == "drop setle <str> <num>"
    assert journal.sanitize_attempt(None) is None


# ----------------------------------------------------------------------------- item 9: human-only attempts are always on record


@pytest.mark.parametrize("cmd", ["approve", "deny", "lock", "revert", "apply", "approver"])
def test_human_only_commands_are_journaled_before_parsing_even_when_parsing_fails(notes, home, cmd):
    code, out = togi_code(cmd, "ap_secretvalue123", "--no-such-flag", "--ttl", "99m", "--also=bad")
    assert code != 0
    host = lines(home, "_host")
    attempts = [l for l in host if l["event"] == "host_attempt"]
    assert len(attempts) == 1 and attempts[0]["command"] == cmd and attempts[0]["status"] == "attempted"
    assert attempts[0]["argv"] == ["<str>", "--no-such-flag", "--ttl", "<str>", "--also=<str>"]    # shape only
    assert "secretvalue123" not in raw_journal(home, "_host") and "99m" not in raw_journal(home, "_host")


def test_journaling_survives_leading_mode_flags_and_agent_sessions(notes, home, monkeypatch):
    monkeypatch.setenv("TOGI_AGENT", "stone")
    togi_code("--json", "approve", "--bogus")
    ents = [l for l in lines(home, "_host") if l["event"] == "host_attempt"]
    assert ents and ents[0]["agent"] == "stone" and ents[0]["argv"] == ["--bogus"]


def test_other_commands_do_not_write_attempt_records(notes, home):
    togi("approvals")
    togi("journal")
    assert not [l for l in lines(home, "_host") if l.get("event") == "host_attempt"]


# ----------------------------------------------------------------------------- item 5: an empty TOGI_AGENT is still an agent


def test_agent_presence_is_the_variable_being_set(home, monkeypatch):
    from togi import paths

    assert paths.is_agent() is False and paths.agent_name() is None
    monkeypatch.setenv("TOGI_AGENT", "")
    assert paths.is_agent() is True and paths.agent_name() == "unnamed"
    monkeypatch.setenv("TOGI_AGENT", "claude")
    assert paths.agent_name() == "claude"
    monkeypatch.delenv("TOGI_AGENT")
    monkeypatch.setenv("TOGI_RUN", "")
    assert paths.in_run() is True


def test_journal_and_context_record_an_empty_agent_as_unnamed(notes, home, monkeypatch):
    from togi.context import Context

    monkeypatch.setenv("TOGI_AGENT", "")
    togi("notes", "add", "--text", "x")
    assert lines(home)[0]["agent"] == "unnamed"
    assert Context().agent == "unnamed"
    monkeypatch.delenv("TOGI_AGENT")
    assert Context().agent is None


def test_empty_agent_cannot_use_the_host_only_commands(notes, home):
    from conftest import run_togi

    for argv in (["approve", "ap_x"], ["deny", "ap_x"], ["approver", "setup"]):
        r = run_togi(home, *argv, env_extra={"TOGI_AGENT": ""})
        assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "human_only", argv
