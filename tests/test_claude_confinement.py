"""The agent that has a shell is confined by Togi's own flags, not by the person's Claude Code settings.

`--allowedTools "Bash(togi:*)"` only ADDS an allow rule to the permissions a person already has, so a `Bash(*)` rule or an `auto`
default mode in ~/.claude/settings.json let the stone agent run any command. The Bash surfaces (`cli`, `run`) of the stone driver and
of the claude-cli provider of `togi eval` therefore pass `--restricted --permission-mode dontAsk` (`stone.BASH_TOGI_ONLY`), and a
Claude Code that does not accept them is an error, never a fallback. Nothing here calls a model.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from togi import stone
from togi.evals import providers, runner, store, subject
from togi.evals.sandbox import EvalSandbox

from evalhelp import claude_calls, install_fake_claude, toml_item, write_suite

FLAGS = list(stone.BASH_TOGI_ONLY)


def subs(*specs):
    return [subject.make_subject(s) for s in specs]


def spec(root, subjects):
    return runner.RunSpec(suite_path=str(root), subjects=subjects)


def all_cells(manifest):
    return store.read_cells(manifest["run_id"])


def contiguous(cmd: list[str], flags: list[str]) -> bool:
    return any(cmd[i:i + len(flags)] == flags for i in range(len(cmd)))


def assert_confined(cmd: list[str]) -> None:
    assert "--restricted" in cmd and cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert "--tools=Bash" in cmd and "--allowedTools=Bash(togi:*)" in cmd
    assert contiguous(cmd, FLAGS)


def test_the_shared_list_is_what_the_threat_model_says():
    assert FLAGS == ["--restricted", "--permission-mode", "dontAsk", "--tools=Bash", "--allowedTools=Bash(togi:*)"]


@pytest.mark.parametrize("surface", ["cli", "run"])
def test_the_stone_driver_confines_the_bash_surfaces(notes, home, surface):
    sb = stone.Sandbox(notes, "notes")
    try:
        assert_confined(stone.claude_command("haiku", surface, sb, 0.5))
    finally:
        sb.cleanup()


def test_the_stone_driver_leaves_the_mcp_surface_alone(notes, home):
    sb = stone.Sandbox(notes, "notes")
    try:
        cmd = stone.claude_command("haiku", "mcp", sb, None)
    finally:
        sb.cleanup()
    assert "--restricted" not in cmd and "--permission-mode" not in cmd and "dontAsk" not in cmd
    assert "--tools=" in cmd and "--strict-mcp-config" in cmd and "--safe-mode" not in cmd


def test_extra_arguments_from_the_environment_come_before_the_confinement_flags(notes, home, monkeypatch):
    monkeypatch.setenv("TOGI_CLAUDE_ARGS", "--effort low")
    sb = stone.Sandbox(notes, "notes")
    try:
        cmd = stone.claude_command("haiku", "cli", sb, None)
    finally:
        sb.cleanup()
    assert cmd.index("--effort") < cmd.index("--restricted") and cmd.index("--effort") < cmd.index("--permission-mode")
    assert_confined(cmd)


@pytest.mark.parametrize("surface", ["cli", "run"])
def test_the_claude_cli_provider_confines_the_bash_surfaces(notes, home, surface):
    sb = EvalSandbox({"notes": notes})
    try:
        cmd = providers.claude_cli_command("SYSTEM", surface, sb, "haiku", 0.4)
    finally:
        sb.cleanup()
    assert_confined(cmd)
    assert cmd[cmd.index("--system-prompt") + 1] == "SYSTEM" and cmd[cmd.index("--model") + 1] == "haiku"
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.4" and "--safe-mode" in cmd


def test_the_claude_cli_provider_leaves_the_mcp_surface_alone(notes, home):
    sb = EvalSandbox({"notes": notes})
    try:
        cmd = providers.claude_cli_command("SYSTEM", "mcp", sb, None, None)
    finally:
        sb.cleanup()
    assert "--restricted" not in cmd and "--permission-mode" not in cmd
    assert "--tools=" in cmd and "--strict-mcp-config" in cmd and "--safe-mode" not in cmd


def test_the_provider_really_starts_claude_with_the_flags(home, tmp_path, monkeypatch):
    """End to end against a fake `claude`: the argv that reaches the process, on every surface that has a shell."""
    log = install_fake_claude(tmp_path, monkeypatch, script=[[]], mode="agent")
    root = write_suite(tmp_path / "s", [{k: v for k, v in toml_item().items() if k not in ("script", "script_answer_json")}])
    for surface in ("cli", "run", "mcp"):
        runner.run_matrix(spec(root, subjects=subs({"provider": "claude-cli", "model": "m", "role": "reference", "surface": surface})))
    cli, run_, mcp = [c["args"] for c in claude_calls(log)]
    assert_confined(cli)
    assert_confined(run_)
    assert "--restricted" not in mcp and "--permission-mode" not in mcp


def test_the_two_drivers_cannot_drift_apart():
    """The allow rule is written once. A second literal copy in either driver would be a place for the two to differ."""
    src = Path(stone.__file__).parent
    for rel, expected in (("stone.py", 1), ("evals/providers.py", 0)):
        text = (src / rel).read_text()
        assert text.count("--allowedTools=Bash(togi:*)") == expected and text.count('"--tools=Bash"') == expected, rel


# ----------------------------------------------------------------------------- a Claude Code that does not accept the flags

# What the real CLI does with an option it does not know: say so on stderr, exit 1, print nothing on stdout, call no model.
OLD_CLAUDE = r'''#!{python}
import json, os, sys
args = sys.argv[1:]
sys.stdin.read()
with open(os.environ["OLD_CLAUDE_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
for bad in os.environ["OLD_CLAUDE_REJECTS"].split(","):
    if bad in args:
        sys.stderr.write("error: unknown option '" + bad + "'\n")
        sys.exit(1)
sys.stdout.write(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": "ran anyway"}}))
'''


@pytest.fixture
def old_claude(tmp_path, monkeypatch):
    d = tmp_path / "oldbin"
    d.mkdir()
    exe = d / "claude"
    exe.write_text(OLD_CLAUDE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "old.log"
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("OLD_CLAUDE_LOG", str(log))
    monkeypatch.setenv("OLD_CLAUDE_REJECTS", "--restricted")
    return log


def calls(log: Path) -> list[list[str]]:
    return [json.loads(l) for l in log.read_text().splitlines()]


@pytest.mark.parametrize("engine", ["legacy", "eval"])
def test_the_stone_driver_fails_closed_when_claude_rejects_restricted(notes, home, old_claude, engine):
    r = stone.run_stone(notes, driver="claude", model="haiku", with_tests=False, save=False, engine=engine, task_ids=["missing-note"])
    t = r["tasks"][0]
    assert t["success"] is False and "does not accept `--restricted`" in t["driver_error"] and "Update Claude Code" in t["driver_error"]
    assert r["summary"]["driver_errors"] == 1
    seen = calls(old_claude)
    assert seen and all("--restricted" in a for a in seen)           # never tried again without the flag


def test_a_claude_that_rejects_only_the_dontask_value_is_refused_too(notes, home, old_claude, monkeypatch):
    monkeypatch.setenv("OLD_CLAUDE_REJECTS", "dontAsk")              # a CLI that knows --restricted but not that permission mode
    t = stone.run_stone(notes, driver="claude", with_tests=False, save=False, task_ids=["missing-note"])["tasks"][0]
    assert t["success"] is False and "does not accept" in t["driver_error"] and "ran anyway" not in json.dumps(t)
    assert all("dontAsk" in a for a in calls(old_claude))


def test_the_claude_cli_provider_fails_closed_when_claude_rejects_restricted(home, tmp_path, old_claude):
    root = write_suite(tmp_path / "s", [{k: v for k, v in toml_item().items() if k not in ("script", "script_answer_json")}])
    c = all_cells(runner.run_matrix(spec(root, subjects=subs({"provider": "claude-cli", "model": "m", "role": "reference"}))))[0]
    assert c["valid"] is False and c["success"] is False and "does not accept `--restricted`" in c["driver_error"]
    assert all("--restricted" in a for a in calls(old_claude))


def test_the_mcp_surface_does_not_need_the_flags_and_so_is_not_refused(notes, home, old_claude):
    t = stone.run_stone(notes, driver="claude", surface="mcp", with_tests=False, save=False, task_ids=["missing-note"])["tasks"][0]
    assert "does not accept" not in (t.get("driver_error") or "")
    assert all("--restricted" not in a for a in calls(old_claude))


def test_an_unrelated_claude_failure_is_not_mistaken_for_a_rejected_flag(notes, home, monkeypatch, tmp_path):
    d = tmp_path / "badbin"
    d.mkdir()
    exe = d / "claude"
    exe.write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.read()\nsys.stderr.write('error: not logged in')\nsys.exit(1)\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    t = stone.run_stone(notes, driver="claude", with_tests=False, save=False, task_ids=["missing-note"])["tasks"][0]
    assert "not logged in" in t["driver_error"] and "does not accept" not in t["driver_error"]


def test_rejected_confinement_only_speaks_when_the_flags_are_the_complaint():
    def done(code, err="", out=""):
        return subprocess.CompletedProcess([], code, out, err)

    assert stone.rejected_confinement(done(0, "unknown option '--restricted'")) is None
    assert stone.rejected_confinement(done(1, "error: unknown option '--restricted'")) == stone.CONFINEMENT_REJECTED
    assert stone.rejected_confinement(done(1, "error: option '--permission-mode <mode>' argument 'dontAsk' is invalid.")) == stone.CONFINEMENT_REJECTED
    assert stone.rejected_confinement(done(1, "error: unknown option '--bogus'")) is None
    assert stone.rejected_confinement(done(1, "network down")) is None
