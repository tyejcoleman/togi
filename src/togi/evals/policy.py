"""Per-step model backends: given a prompt, return the model's reply. One call = one step of an agent loop (loops.py).

    openai-compatible   POST {base_url}/chat/completions   (vLLM, llama.cpp server, LM Studio, OpenAI-style hosts)
    ollama              POST {base_url}/api/chat           (`format` = a JSON schema constrains the output)
    claude-step         `claude -p --tools "" --output-format json --model M`, one call per step, on the subscription

Two ways to get a valid action out of a model, chosen by the subject's `constrained_decoding`:
    schema mode  the backend's structured output (Ollama `format`, OpenAI `response_format: json_schema`): the reply IS
                 the action JSON and is valid by construction
    tools mode   native tool calling (`tools=[...]`): the reply is a tool call, converted to the same action

Errors the agent cannot cause (server down, bad HTTP status, a reply that is not a reply) raise ProviderError, which the
cell records as a driver error, not as the subject failing. Standard library only (urllib); the tests run these against
a mock server on 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .. import stone

DEFAULT_TIMEOUT = 120.0


class ProviderError(Exception):
    """kind: unreachable | http | timeout | bad_response | missing | cost_cap"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass
class Prompt:
    system: str
    messages: list[dict]                    # [{"role": "user" | "assistant", "content": str}]
    schema: dict | None = None              # JSON Schema of the action object (schema mode)
    tools: list[dict] | None = None         # OpenAI-style function tools (tools mode)

    def chars(self) -> int:
        n = len(self.system) + sum(len(m.get("content", "")) for m in self.messages)
        if self.tools:
            n += len(json.dumps(self.tools))
        return n

    def est_tokens(self) -> int:
        return (self.chars() + 3) // 4


@dataclass
class Reply:
    text: str = ""
    tool_call: dict | None = None           # {"name": str, "arguments": dict}
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    wall_s: float = 0.0


class Backend:
    name = "backend"

    def complete(self, prompt: Prompt, *, seed: int = 0, mode: str = "schema") -> Reply:        # pragma: no cover - interface
        raise NotImplementedError


def _post(url: str, body: dict, headers: dict[str, str], timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise ProviderError("http", f"{url} answered HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), TimeoutError):
            raise ProviderError("timeout", f"{url} did not answer within {timeout:g}s")
        raise ProviderError("unreachable", f"cannot reach {url} ({getattr(e, 'reason', e)})")
    except (TimeoutError, OSError) as e:
        raise ProviderError("timeout" if isinstance(e, TimeoutError) else "unreachable", f"{url}: {e}")
    try:
        doc = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise ProviderError("bad_response", f"{url} returned something that is not JSON: {raw[:120]!r}")
    if not isinstance(doc, dict):
        raise ProviderError("bad_response", f"{url} returned JSON that is not an object")
    return doc


def _chat_messages(prompt: Prompt) -> list[dict]:
    return [{"role": "system", "content": prompt.system}, *prompt.messages]


def _args(v: Any) -> dict:
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            d = json.loads(v)
            return d if isinstance(d, dict) else {}
        except ValueError:
            return {}
    return {}


class OpenAIBackend(Backend):
    name = "openai-compatible"

    def __init__(self, model: str, base_url: str = "http://localhost:8000/v1", api_key_env: str | None = None,
                 options: dict | None = None, timeout: float | None = None):
        self.model, self.base_url = model, base_url.rstrip("/")
        self.api_key_env, self.options = api_key_env, dict(options or {})
        self.timeout = float(timeout or self.options.get("timeout_s") or DEFAULT_TIMEOUT)

    def complete(self, prompt: Prompt, *, seed: int = 0, mode: str = "schema") -> Reply:
        body: dict[str, Any] = {"model": self.model, "messages": _chat_messages(prompt), "seed": seed, "stream": False}
        for k in ("temperature", "top_p", "max_tokens"):
            if k in self.options:
                body[k] = self.options[k]
        if mode == "tools" and prompt.tools:
            body["tools"] = prompt.tools
            body["tool_choice"] = "required"
        elif prompt.schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "action", "schema": prompt.schema, "strict": False}}
        headers = {}
        key = os.environ.get(self.api_key_env, "") if self.api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        t0 = time.monotonic()
        doc = _post(f"{self.base_url}/chat/completions", body, headers, self.timeout)
        try:
            msg = doc["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("bad_response", "the reply has no choices[0].message")
        usage = doc.get("usage") or {}
        reply = Reply(text=msg.get("content") or "", prompt_tokens=usage.get("prompt_tokens"),
                      completion_tokens=usage.get("completion_tokens"), wall_s=time.monotonic() - t0)
        calls = msg.get("tool_calls") or []
        if calls and isinstance(calls[0], dict):
            fn = calls[0].get("function") or {}
            reply.tool_call = {"name": fn.get("name"), "arguments": _args(fn.get("arguments"))}
        return reply


class OllamaBackend(Backend):
    name = "ollama"

    def __init__(self, model: str, base_url: str = "http://localhost:11434", options: dict | None = None,
                 timeout: float | None = None):
        self.model, self.base_url = model, base_url.rstrip("/")
        self.options = dict(options or {})
        self.timeout = float(timeout or self.options.get("timeout_s") or DEFAULT_TIMEOUT)

    def complete(self, prompt: Prompt, *, seed: int = 0, mode: str = "schema") -> Reply:
        opts: dict[str, Any] = {"seed": seed}
        for src, dst in (("temperature", "temperature"), ("top_p", "top_p"), ("top_k", "top_k"), ("num_ctx", "num_ctx"),
                         ("max_tokens", "num_predict")):
            if src in self.options:
                opts[dst] = self.options[src]
        body: dict[str, Any] = {"model": self.model, "messages": _chat_messages(prompt), "stream": False, "options": opts}
        if "keep_alive" in self.options:
            body["keep_alive"] = self.options["keep_alive"]
        if mode == "tools" and prompt.tools:
            body["tools"] = prompt.tools
        elif prompt.schema is not None:
            body["format"] = prompt.schema
        t0 = time.monotonic()
        doc = _post(f"{self.base_url}/api/chat", body, {}, self.timeout)
        msg = doc.get("message")
        if not isinstance(msg, dict):
            raise ProviderError("bad_response", "the reply has no `message`")
        reply = Reply(text=msg.get("content") or "", prompt_tokens=doc.get("prompt_eval_count"),
                      completion_tokens=doc.get("eval_count"), wall_s=time.monotonic() - t0)
        calls = msg.get("tool_calls") or []
        if calls and isinstance(calls[0], dict):
            fn = calls[0].get("function") or {}
            reply.tool_call = {"name": fn.get("name"), "arguments": _args(fn.get("arguments"))}
        return reply


def flatten(messages: list[dict]) -> str:
    """A transcript as one prompt text (claude -p takes one message on stdin)."""
    parts = []
    for m in messages:
        who = {"assistant": "YOU (earlier reply)", "user": "INPUT"}.get(m["role"], m["role"].upper())
        parts.append(f"[{who}]\n{m['content']}")
    return "\n\n".join(parts)


class ClaudeStepBackend(Backend):
    """Our loop with a Claude model as the per-step policy: `claude -p --tools "" --output-format json --model M`, one call per
    step. The loop builds the whole prompt; the reply text is the JSON action. Runs on the Claude subscription (the API key
    variables are removed from the environment, like every other claude driver here); no Claude Code customisation is
    loaded (--safe-mode), no tools are offered (--tools=)."""
    name = "claude-step"

    def __init__(self, model: str, options: dict | None = None, timeout: float | None = None, max_cost: float | None = None):
        self.model, self.options = model, dict(options or {})
        self.timeout = float(timeout or self.options.get("timeout_s") or 180.0)
        self.max_cost = max_cost

    def complete(self, prompt: Prompt, *, seed: int = 0, mode: str = "schema") -> Reply:
        system = prompt.system
        if prompt.schema is not None:
            system += ("\n\nReply with ONE JSON object and nothing else (no prose, no code fence) that is valid against this "
                       "JSON Schema:\n" + json.dumps(prompt.schema, separators=(",", ":")))
        cmd = stone.claude_base() + ["--system-prompt", system, "--tools="]
        if self.model:
            cmd += ["--model", self.model]
        if self.max_cost:
            cmd += ["--max-budget-usd", f"{self.max_cost:g}"]
        clean = {k: v for k, v in os.environ.items() if not k.startswith("TOGI_") and k not in stone.SECRET_ENV}
        clean["TOGI_AGENT"] = "eval-step"
        t0 = time.monotonic()
        try:
            with tempfile.TemporaryDirectory(prefix="togi-step-") as empty:
                p = stone.run_group(cmd, input=flatten(prompt.messages), env=clean, cwd=empty, timeout=self.timeout)
        except FileNotFoundError:
            raise ProviderError("missing", "the `claude` CLI is not on PATH; install Claude Code or use another provider")
        except subprocess.TimeoutExpired:
            raise ProviderError("timeout", f"claude -p exceeded {self.timeout:g}s")
        try:
            res = stone.parse_claude_output(p.stdout)
        except (ValueError, StopIteration):
            raise ProviderError("bad_response", f"claude -p exit {p.returncode}: {(p.stderr or p.stdout).strip()[:200]}")
        if res.get("is_error"):
            raise ProviderError("http", f"claude reported an error ({res.get('subtype')}): {res['answer'][:160]}")
        usage = res.get("usage") or {}
        sent = sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
        return Reply(text=res["answer"], prompt_tokens=sent or None, completion_tokens=int(usage.get("output_tokens") or 0) or None,
                     cost_usd=res.get("cost_usd"), wall_s=time.monotonic() - t0)


def make_backend(subject: Any, max_cost: float | None = None) -> Backend:
    o = subject.options
    if subject.provider == "ollama":
        return OllamaBackend(subject.model, o.get("base_url", "http://localhost:11434"), o, o.get("timeout_s"))
    if subject.provider == "openai-compatible":
        return OpenAIBackend(subject.model, o.get("base_url", "http://localhost:8000/v1"), o.get("api_key_env"), o, o.get("timeout_s"))
    if subject.provider == "claude-step":
        return ClaudeStepBackend(subject.model, o, o.get("timeout_s"), max_cost)
    raise ValueError(f"{subject.provider} is not a step-policy provider")
