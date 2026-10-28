"""The eval sandbox: stone's throwaway $TOGI_HOME + cwd + `togi` shim, extended for what evals need.

  - several bundles registered in one home (an item may use more than one);
  - the harness toggles (next hints, compact output) and the call-recording switch set in the environment;
  - a NEW agent session id per session of a multi-session item (a cold start: nothing carries over but Togi state);
  - the item's answer schema in place for `togi answer`;
  - readers for the answers log, the task record and its history;
  - the test-only approval hook (hooks.py) run between sessions;
  - the agent is `TOGI_AGENT=eval`.
"""
from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from .. import answers, paths, stone, taskrec
from ..manifest import load_manifest


class EvalSandbox(stone.Sandbox):
    def __init__(self, bundles: dict[str, Path], *, label: str = "eval", toggles: dict[str, str] | None = None,
                 suite_dir: str | None = None, skill_files: dict[str, str] | None = None):
        env: dict[str, str] = {}
        for repo in bundles.values():           # each bundle's own test mode (its fake payment mode, ...)
            try:
                env.update(load_manifest(repo).stone_env)
            except Exception:
                pass
        env.update(toggles or {})
        env["TOGI_EVAL_RECORD"] = "1"
        first = next(iter(bundles))
        super().__init__(bundles[first], first, label=label, extra_env=env)
        self.bundles = dict(bundles)
        self.suite_dir = suite_dir
        for name, f in (skill_files or {}).items():       # a skill-text variant: a private copy of the bundle with it swapped in
            self.override_skill(name, f)
        self._write_registry()
        self.loop_session = f"loop-{secrets.token_hex(4)}"
        self.agent_sessions: list[str] = []
        self.new_agent_session()

    # ------------------------------------------------------------------ registry / skills

    def _write_registry(self) -> None:
        lines = []
        for name, repo in self.bundles.items():
            lines.append(f"[bundles.{json.dumps(name)}]\npath = {json.dumps(str(repo))}\n")
        (self.home / "bundles.toml").write_text("\n".join(lines), encoding="utf-8")

    def override_skill(self, bundle: str, skill_file: str) -> None:
        from .. import apply as apply_mod

        repo = self.bundles[bundle]
        man = load_manifest(repo)
        copy = self.root / "repos" / bundle
        apply_mod.copy_repo(repo, copy)
        target = copy / man.skills / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(skill_file).expanduser(), target)
        self.bundles[bundle] = copy

    # ------------------------------------------------------------------ environments

    def env(self, agent: bool = False) -> dict[str, str]:
        env = super().env(agent)
        if agent:
            env["TOGI_AGENT"] = "eval"
        elif self.suite_dir:
            env["EVAL_SUITE_DIR"] = self.suite_dir      # setup / between / check commands may load the suite's data files
        return env

    def loop_env(self) -> dict[str, str]:
        """The agent's environment, but a session of its own: housekeeping the LOOP does for the model (task show/update)
        is not the model's own call, so it stays out of the agent's trajectory."""
        env = self.env(agent=True)
        env["TOGI_SESSION"] = self.loop_session
        return env

    def new_agent_session(self) -> str:
        self.agent_session = f"eval-{secrets.token_hex(4)}"
        self.agent_sessions.append(self.agent_session)
        return self.agent_session

    # ------------------------------------------------------------------ running things

    def run_argv(self, argv: list[str], *, agent: bool = True, timeout: float = 120, stdin: str | None = None,
                 housekeeping: bool = False) -> subprocess.CompletedProcess:
        env = self.loop_env() if housekeeping else self.env(agent)
        return subprocess.run([str(self.bin / "togi"), *argv], capture_output=True, text=True, timeout=timeout, env=env,
                              cwd=self.cwd, input=stdin)

    def hook(self, action: str, selector: str = "latest") -> dict:
        code = "import sys; sys.path.insert(0, sys.argv[1]); from togi.evals.hooks import main; sys.exit(main(sys.argv[2:]))"
        p = subprocess.run([sys.executable, "-I", "-c", code, str(paths.src_dir()), action, selector], capture_output=True,
                           text=True, timeout=60, env=self.env(agent=False), cwd=self.cwd)
        try:
            return json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"ok": False, "error": (p.stderr or p.stdout).strip()[:200]}

    # ------------------------------------------------------------------ what the sandbox recorded

    def write_answer_schema(self, schema: dict | None) -> None:
        p = self.home / answers.SCHEMA_FILE
        if schema is None:
            p.unlink(missing_ok=True)
        else:
            p.write_text(json.dumps(schema), encoding="utf-8")

    def _host_state(self) -> Path:
        return self.home / "state" / "_host"

    def answers_mark(self) -> int:
        try:
            return (self._host_state() / "answers.jsonl").stat().st_size
        except OSError:
            return 0

    def answers_since(self, mark: int = 0) -> list[dict]:
        out = []
        try:
            with open(self._host_state() / "answers.jsonl", "rb") as f:
                f.seek(mark)
                data = f.read()
        except OSError:
            return out
        for line in data.split(b"\n"):
            try:
                e = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
        return out

    def task_record(self) -> dict | None:
        try:
            doc = json.loads((self._host_state() / "task.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def task_history(self) -> list[dict]:
        out = []
        try:
            lines = (self._host_state() / "task-history.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return out
        for line in lines:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                out.append(e)
        return out

    def mcp_config(self) -> dict:
        """The one MCP server the agent gets: this Togi in the sandbox home, serving every bundle's tools plus the built-ins."""
        env = {**self.extra_env, "TOGI_HOME": str(self.home), "TOGI_APPROVER_HOME": str(self.approver_home),
               "TOGI_STONE_SANDBOX": str(self.root), "TOGI_AGENT": "eval", "TOGI_SESSION": self.agent_session,
               "PYTHONDONTWRITEBYTECODE": "1"}
        argv = paths.isolated_argv(["mcp", "serve", self.bundle, "--builtins"])
        return {"mcpServers": {stone.MCP_SERVER_KEY: {"command": argv[0], "args": argv[1:], "env": env}}}


def pretty_env_toggles(harness: dict) -> dict[str, str]:
    """The harness switches that live in the environment of every `togi` process of the sandbox."""
    t: dict[str, str] = {}
    if harness.get("next_hints") is False:
        t["TOGI_NEXT_HINTS"] = "0"
    if harness.get("compact_output") is False:
        t["TOGI_COMPACT"] = "0"
    return t
