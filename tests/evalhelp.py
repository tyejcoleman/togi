"""Helpers for the eval-layer tests: a mock LLM server on 127.0.0.1 (OpenAI-compatible and Ollama shapes), suite builders around
the notes example, and a fake `claude` for the claude-step and claude-cli providers. No network beyond loopback, no model."""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NOTES = Path(__file__).resolve().parent.parent / "examples" / "notes"


# --------------------------------------------------------------------------- mock LLM server


class MockLLM:
    """policy(req: dict, n: int) -> one of
         {"say": {...}}                  the model answers with this JSON object (an action) as its content (schema mode)
         {"tool": ("togi", {...})}       the model makes this native tool call (tools mode)
         {"text": "..."}                 raw content
         {"status": 500, "body": "..."}  an HTTP error
       Every request is kept in .requests (the parsed JSON body, plus .kind)."""

    def __init__(self, policy):
        self.policy = policy
        self.requests: list[dict] = []
        self.n = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):      # quiet
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                kind = "ollama" if self.path.startswith("/api/chat") else "openai"
                body["_kind"], body["_path"] = kind, self.path
                body["_auth"] = self.headers.get("Authorization")
                outer.requests.append(body)
                outer.n += 1
                spec = outer.policy(body, outer.n)
                if "status" in spec:
                    self.send_response(spec["status"])
                    self.end_headers()
                    self.wfile.write(spec.get("body", "error").encode())
                    return
                chars = sum(len(m.get("content", "")) for m in body.get("messages", [])) + (len(json.dumps(body["tools"])) if "tools" in body else 0)
                p_tokens, c_tokens = chars // 4, 20
                content, calls = "", []
                if "say" in spec:
                    content = json.dumps(spec["say"])
                elif "tool" in spec:
                    name, args = spec["tool"]
                    calls = [(name, args)]
                elif "text" in spec:
                    content = spec["text"]
                if kind == "ollama":
                    msg = {"role": "assistant", "content": content}
                    if calls:
                        msg["tool_calls"] = [{"function": {"name": n, "arguments": a}} for n, a in calls]
                    out = {"model": body.get("model"), "message": msg, "done": True, "prompt_eval_count": p_tokens, "eval_count": c_tokens}
                else:
                    msg = {"role": "assistant", "content": content}
                    if calls:
                        msg["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
                                             for n, a in calls]
                    out = {"choices": [{"index": 0, "message": msg}], "usage": {"prompt_tokens": p_tokens, "completion_tokens": c_tokens}}
                data = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()

    @property
    def ollama_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def openai_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"


def scripted(actions: list[dict]):
    """A policy that plays `actions` in order (the last one repeats), whatever the prompt says."""
    def policy(req, n):
        a = actions[min(n, len(actions)) - 1]
        return a if any(k in a for k in ("say", "tool", "text", "status")) else {"say": a}
    return policy


# --------------------------------------------------------------------------- suites around the notes example


NOTES_SCHEMA = {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}


def toml_item(**kw) -> dict:
    """An item dict with sensible defaults for the notes example."""
    d = {"id": "add-find", "world": "notes", "split": "dev", "tier": 1, "capabilities": ["tool-choice"], "bundles": ["notes"],
         "instruction": "Add a note 'buy milk' tagged home, then find it by searching for milk. Tell me its id.",
         "setup": ["togi notes test reset"], "budget_turns": 8, "answer_schema": NOTES_SCHEMA,
         "script": ["togi notes add --text 'buy milk' --tag home", "togi notes find --query milk"],
         "script_answer_json": {"id": "n_1"},
         "check": [{"type": "exact", "path": "$.id", "expected": "n_1"}], "forbid": [{"command_prefix": "togi approve"}]}
    d.update(kw)
    return d


def write_suite(root: Path, items: list[dict], *, name: str = "t", canary: str = "", bundles: dict | None = None,
                files: dict[str, str] | None = None) -> Path:
    from togi.evals import tomlw

    root.mkdir(parents=True, exist_ok=True)
    suite = {"name": name, "default_bundles": ["notes"], "bundles": {"notes": {"path": str(NOTES)}} if bundles is None else bundles}
    if canary:
        suite["canary"] = canary
    (root / "suite.toml").write_text(tomlw.dumps(suite))
    for it in items:
        sub = "items/unseen" if it.get("split") == "unseen" else "items"          # (dev and test sit at the root: both layouts work)
        (root / sub).mkdir(parents=True, exist_ok=True)
        (root / sub / f"{it['id']}.toml").write_text(tomlw.dumps(it))
    for rel, text in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def local_subject(provider: str, url: str, *, model: str = "tiny-7b", loop: str = "react", role: str = "candidate", **harness) -> dict:
    return {"provider": provider, "model": model, "role": role, "options": {"base_url": url, "timeout_s": 20},
            "harness": {"loop": loop, "step_budget": 12, **harness}}


# --------------------------------------------------------------------------- a fake `claude`

FAKE_CLAUDE = r'''#!{python}
import json, os, subprocess, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
mode = os.environ.get("FAKE_CLAUDE_MODE", "step")
log = os.environ["FAKE_CLAUDE_LOG"]
n = 0
if os.path.exists(log + ".n"):
    n = int(open(log + ".n").read())
open(log + ".n", "w").write(str(n + 1))
with open(log, "a") as f:
    f.write(json.dumps({{"args": args, "prompt": prompt, "n": n, "cwd": os.getcwd(), "agent": os.environ.get("TOGI_AGENT"),
                        "session": os.environ.get("TOGI_SESSION"), "key": "ANTHROPIC_API_KEY" in os.environ}}) + "\n")
if mode == "step":      # the loop's per-step policy: reply with the n-th scripted action
    script = json.load(open(os.environ["FAKE_CLAUDE_SCRIPT"]))
    act = script[min(n, len(script) - 1)]
    text = act if isinstance(act, str) else json.dumps(act)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "result": text,
                      "total_cost_usd": 0.001, "usage": {{"input_tokens": 50, "output_tokens": 10, "cache_read_input_tokens": 200}}}}))
elif mode == "agent":   # Claude Code's own loop: run the scripted togi commands as the agent, then finish
    script = json.load(open(os.environ["FAKE_CLAUDE_SCRIPT"]))      # one list of argv lists per invocation (the last repeats)
    for argv in script[min(n, len(script) - 1)]:
        subprocess.run(argv, capture_output=True)
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 4, "result": "done",
                      "total_cost_usd": 0.02, "usage": {{"input_tokens": 100, "output_tokens": 20}}}}))
elif mode == "hang":    # a stuck agent: it also starts a grandchild that must be killed with it
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3); open(" + repr(os.environ["FAKE_CLAUDE_MARKER"]) + ", 'w').write('x')"])
    import time; time.sleep(60)
elif mode == "judge":   # the rubric judge: pass if the grader's prompt mentions GOOD, fail otherwise (the reply may be garbled on request)
    word = "pass" if "GOOD" in prompt.split("ANSWER:")[-1] else "fail"
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                      "result": os.environ.get("FAKE_CLAUDE_JUDGE_SAYS", word), "total_cost_usd": 0.0005, "usage": {{"input_tokens": 10, "output_tokens": 1}}}}))
else:
    print(json.dumps({{"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom", "num_turns": 0}}))
'''


def install_fake_claude(tmp_path: Path, monkeypatch, script: list | None = None, mode: str = "step") -> Path:
    d = tmp_path / "fakeclaude"
    d.mkdir(exist_ok=True)
    exe = d / "claude"
    exe.write_text(FAKE_CLAUDE.format(python=sys.executable))
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "claude-step.log"
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-for-the-agent")
    if script is not None:
        sp = tmp_path / "claude-script.json"
        sp.write_text(json.dumps(script))
        monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(sp))
    return log


def claude_calls(log: Path) -> list[dict]:
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
