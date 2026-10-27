"""`togi mcp serve <bundle>`: the same commands as MCP tools over stdio (SPEC section 7).

Tools are named `<bundle>_<api_name>`, described as summary + effect, with the input schema taken from the
command signature, plus one extra tool, `<bundle>_skill` (optional `topic`), so progressive disclosure works
over MCP exactly as `togi skill <bundle> [topic]` does on the CLI. Calls go through the same host path as the CLI
(guards, approvals, journal with `surface: "mcp"`). Gated commands accept an `approval` argument;
write/money/admin accept `dry_run`. Results are cut at ~8 KB with a note. Needs the optional `mcp` package.

The server is built to start fast and to stay up:
  - it answers the MCP handshake BEFORE the bundle is imported (the bundle loads on first use), so a slow or
    broken bundle never looks like "the server did not connect";
  - the protocol channel is a private duplicate of stdout; fd 1 and `sys.stdout` are pointed at stderr, so a
    bundle that prints cannot corrupt the stream.
"""
from __future__ import annotations

import io
import os
import sys
from typing import Any

from . import host, output, registry, skills
from .bundle import Command
from .cli import Reply, parse_flags, _mode, _usage
from .errors import ArgError, TogiError
from .registry import Loaded

SKILL_SUFFIX = "skill"


def skill_tool_name(loaded: Loaded) -> str:
    """`<bundle>_skill`, unless the bundle has its own command of that name (then `<bundle>_skill_guide`)."""
    own = {c.api_name for c in loaded.bundle.commands.values()}
    return f"{loaded.name}_{SKILL_SUFFIX}" if SKILL_SUFFIX not in own else f"{loaded.name}_{SKILL_SUFFIX}_guide"


def tool_name(bundle: str, cmd: Command) -> str:
    return f"{bundle}_{cmd.api_name}"


def skill_spec(loaded: Loaded) -> dict:
    return {"name": skill_tool_name(loaded),
            "description": (f"Read how to use the {loaded.name} tools. Call this FIRST. With no argument it returns the "
                            f"main guide and the list of topics; pass `topic` to read one topic. [effect: read]"),
            "inputSchema": {"type": "object", "properties": {"topic": {
                "type": "string", "description": "optional: a topic name from the guide's topic list"}},
                "additionalProperties": False},
            "command": None, "effect": "read"}


def tool_specs(loaded: Loaded) -> list[dict]:
    specs = [skill_spec(loaded)]
    for cmd in loaded.bundle.commands.values():
        schema = cmd.json_schema()
        gated = loaded.requires_approval(cmd.effect)
        desc = f"{cmd.summary} [effect: {cmd.effect}]"
        if gated:
            desc += (" Needs a person's approval: the first call returns needs_approval with an approval id; "
                     "after the person approves, call again with the same arguments plus `approval`.")
            schema["properties"]["approval"] = {"type": "string", "description": "approval id granted by a person"}
        if cmd.effect != "read":
            schema["properties"]["dry_run"] = {"type": "boolean", "description": "describe without doing it"}
        specs.append({"name": tool_name(loaded.name, cmd), "description": desc, "inputSchema": schema,
                      "command": cmd.name, "effect": cmd.effect})
    return specs


MCP_MORE = "output was cut at 8192 bytes; call again with narrower arguments (a filter, a smaller limit, an id)"


def call_skill(loaded: Loaded, arguments: dict[str, Any] | None) -> tuple[dict, bool]:
    topic = (arguments or {}).get("topic")
    unknown = [k for k in (arguments or {}) if k != "topic"]
    if unknown or (topic is not None and not isinstance(topic, str)):
        return output.error_envelope("error", "bad_arg", "the skill tool takes only an optional string `topic`",
                                     next=f"call {skill_tool_name(loaded)} with no arguments"), True
    man = loaded.manifest
    if topic:
        text = skills.topic_text(man, topic)
        if text is None:
            avail = ", ".join(t["topic"] for t in skills.topics(man)) or "(none)"
            return output.error_envelope(
                "error", "unknown_topic", f"{man.name} has no topic '{topic}'; topics: {avail}",
                next=f"call {skill_tool_name(loaded)} with no arguments to list the topics"), True
    else:
        tools_text = None
        if not (man.repo / man.skills / "SKILL.md").is_file():
            tools_text = "\n".join(c.stub() for c in loaded.bundle.commands.values())
        text = skills.bundle_text(man, tools_text)
    return output.ok_envelope({"topic": topic or None, "text": text}), False


def call_tool(loaded: Loaded, name: str, arguments: dict[str, Any] | None, session: str | None = None
              ) -> tuple[dict, bool]:
    """Run a tool through the host. Returns (envelope, is_error). Results are cut at ~8 KB with a note."""
    if name == skill_tool_name(loaded):
        env, is_error = call_skill(loaded, arguments)
        return env, is_error
    args = dict(arguments or {})
    approval = args.pop("approval", None)
    dry_run = bool(args.pop("dry_run", False))
    prefix = loaded.name + "_"
    cmd = loaded.bundle.get(name[len(prefix):]) if name.startswith(prefix) else None
    opts = host.CallOptions(surface="mcp", cap=False, paginate=False, full=True, dry_run=dry_run,
                            approval=approval or None, session=session, argv=["mcp", loaded.name])
    if cmd is None:
        err = ArgError(f"no tool named {name!r}; tools start with '{prefix}' (start with `{skill_tool_name(loaded)}`)",
                       code="unknown_command", flag=name, next=f"call {skill_tool_name(loaded)}")
        env = host.call(loaded, None, {}, opts, error=err).envelope
        return _cut(env), True
    try:
        typed = host.build_args(cmd, args)
    except ArgError as e:
        return _cut(host.call(loaded, cmd, {}, opts, error=e).envelope), True
    env = host.call(loaded, cmd, typed, opts).envelope
    return _cut(env), not env.get("ok", False)


def _cut(env: dict) -> dict:
    env, _ = output.fit_any(env, output.MAX_BYTES, lambda off: MCP_MORE)
    if env.get("truncated"):
        env["more"] = MCP_MORE
    return env


def render(loaded: Loaded, name: str, env: dict) -> str:
    """Text for the MCP result: the skill tool returns the document itself, everything else the JSON envelope."""
    if name == skill_tool_name(loaded) and env.get("ok"):
        return output.cap_text(env["data"]["text"], output.MAX_BYTES)
    return output.dumps(env)


def _fastmcp():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        raise TogiError("mcp_missing", "the optional `mcp` package is not installed", next="pip install mcp")
    return FastMCP


def instructions_for(name: str) -> str:
    try:
        summary = registry.manifest_for(name).summary
        extra = ""
    except TogiError as e:
        summary, extra = "", f" (the bundle could not be read: {e.message})"
    return (f"{summary} Start by calling the `{name}_skill` tool: it explains the bundle. Every other tool is a "
            f"{name} command; money tools need a person's approval (you get an approval id back; never try to "
            f"approve it yourself).{extra}").strip()


def build_server(name_or_loaded):
    """A FastMCP server for a bundle. Given a name, the bundle is imported lazily (first list/call); given a
    Loaded (tests) it is used as is."""
    FastMCP = _fastmcp()
    from mcp.types import TextContent
    from mcp.types import Tool as MCPTool

    name = name_or_loaded if isinstance(name_or_loaded, str) else name_or_loaded.name
    state: dict[str, Loaded | None] = {"loaded": None if isinstance(name_or_loaded, str) else name_or_loaded}

    def get_loaded() -> Loaded:
        if state["loaded"] is None:
            state["loaded"] = registry.load(name)
        return state["loaded"]

    class TogiMCP(FastMCP):
        async def list_tools(self) -> list[MCPTool]:  # type: ignore[override]
            import anyio

            def build() -> list[dict]:
                try:
                    return tool_specs(get_loaded())
                except TogiError as e:
                    raise RuntimeError(f"{e.code}: {e.message}" + (f" (next: {e.next})" if e.next else ""))

            specs = await anyio.to_thread.run_sync(build)
            return [MCPTool(name=s["name"], description=s["description"], inputSchema=s["inputSchema"]) for s in specs]

        async def call_tool(self, name: str, arguments: dict[str, Any]):  # type: ignore[override]
            import anyio

            def run() -> tuple[str, bool]:
                try:
                    loaded = get_loaded()
                except TogiError as e:
                    return output.dumps(output.error_envelope(e.status, e.code, e.message, next=e.next)), True
                env, is_error = call_tool(loaded, name, arguments)
                return render(loaded, name, env), is_error

            text, is_error = await anyio.to_thread.run_sync(run)
            if is_error:
                raise RuntimeError(text)  # the SDK turns this into isError=true with exactly this text
            return [TextContent(type="text", text=text)]

    return TogiMCP(f"togi-{name}", log_level="WARNING", instructions=instructions_for(name))


def serve_stdio(server) -> None:
    """Run `server` over stdio on a PRIVATE copy of stdout. fd 1 and sys.stdout then point at stderr, so nothing
    a bundle prints (at import time or in a command, from Python or from C) can land in the protocol stream."""
    import anyio
    from mcp.server.stdio import stdio_server

    proto_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    proto_out = anyio.wrap_file(io.TextIOWrapper(os.fdopen(proto_fd, "wb"), encoding="utf-8"))

    async def run() -> None:
        async with stdio_server(stdout=proto_out) as (read_stream, write_stream):
            await server._mcp_server.run(read_stream, write_stream, server._mcp_server.create_initialization_options())

    anyio.run(run)


def h_mcp(argv: list[str]) -> Reply:
    pos, f = parse_flags(argv, {}, "mcp")
    if f["help"] or not pos:
        return _usage("mcp")
    sub = pos[0]
    if sub not in ("serve", "tools"):
        raise TogiError("bad_arg", f"unknown mcp subcommand '{sub}'", next="togi mcp serve <bundle>")
    if len(pos) != 2:
        raise TogiError("bad_arg", f"togi mcp {sub} needs a bundle name", next="togi bundles")
    if sub == "tools":
        loaded = registry.load(pos[1])
        return Reply(env=output.ok_envelope([{k: s[k] for k in ("name", "description", "inputSchema")}
                                             for s in tool_specs(loaded)]), mode=_mode(f))
    if pos[1] not in registry.entries():
        raise TogiError("unknown_bundle", f"no bundle named '{pos[1]}' is registered", next="togi bundles")
    server = build_server(pos[1])
    serve_stdio(server)
    return Reply(silent=True, code=0)


# --------------------------------------------------------------------------- client side (stone / bench)


def script_calls(sb, calls: list[dict]) -> list[dict]:
    """Replay `script_mcp = [{tool, args}]` against a real `togi mcp serve` over stdio (script driver)."""
    import asyncio

    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        raise TogiError("mcp_missing", "the optional `mcp` package is not installed", next="pip install mcp")

    async def go() -> list[dict]:
        params = StdioServerParameters(command=str(sb.bin / "togi"), args=["mcp", "serve", sb.bundle],
                                       env=sb.env(agent=True), cwd=str(sb.cwd))
        out = []
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for c in calls:
                    res = await session.call_tool(c["tool"], c.get("args") or {})
                    text = "".join(getattr(b, "text", "") for b in res.content)
                    out.append({"cmdline": f"mcp {c['tool']}", "output": text, "is_error": res.isError})
        return out

    return asyncio.run(go())
