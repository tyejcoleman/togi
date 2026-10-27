"""The `togi` command line: dispatch, output modes, host commands."""
from __future__ import annotations

import difflib
import json
import os
import shlex
import sys
from dataclasses import dataclass
from typing import Any, Callable

from . import __version__, output, paths, registry
from .errors import ArgError, TogiError

HOST_COMMANDS = ("skill", "tools", "run", "approve", "deny", "approvals", "approver", "journal", "stone", "hone",
                 "review", "apply", "revert", "log", "lock", "bench", "mcp", "add", "remove", "bundles", "doctor",
                 "version", "help")
# Human-only commands. ANY invocation of these is journaled before its flags are parsed (argv shape only), so a
# forbidden attempt is on record even when it fails to parse.
GUARDED_COMMANDS = ("approve", "deny", "lock", "revert", "apply", "approver")


@dataclass
class Reply:
    env: dict | None = None      # JSON envelope (rendered as JSON or text)
    text: str | None = None      # plain document (skill, tools, help): printed as-is unless --json
    mode: str | None = None      # "json" | "text" | None (auto: TTY -> text)
    code: int | None = None
    cap: bool = True             # apply the ~8 KB cap (unless --full)
    silent: bool = False         # the handler already wrote its output (togi run)
    capped: bool = False         # already shaped by host.call (bundle commands): the finalizer leaves it alone
    paged: bool = False          # the handler applied --limit/--offset itself
    projected: bool = False      # the handler applied --fields itself


# --------------------------------------------------------------------------- flag parsing for host commands

COMMON = {"json": "bool", "text": "bool", "full": "bool", "fields": "str", "limit": "int", "offset": "int",
          "help": "bool"}


def parse_flags(argv: list[str], spec: dict[str, str], cmdname: str, aliases: dict[str, str] | None = None
                ) -> tuple[list[str], dict[str, Any]]:
    """Tiny flag parser for host commands. spec: flag -> bool|str|int|list."""
    full = {**COMMON, **spec}
    flags: dict[str, Any] = {k: ([] if v == "list" else (False if v == "bool" else None)) for k, v in full.items()}
    pos: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "-h":
            tok = "--help"
        if tok.startswith("--") and len(tok) > 2:
            name, eq, inline = tok[2:].partition("=")
            key = name.replace("_", "-")
            if aliases and key in aliases:
                key = aliases[key]
            if key not in full:
                close = difflib.get_close_matches(key, list(full), n=1, cutoff=0.6)
                hint = f"; did you mean --{close[0]}?" if close else ""
                raise TogiError("unknown_flag", f"unknown flag --{name} for `togi {cmdname}`{hint}",
                                next=f"togi {cmdname} --help")
            kind = full[key]
            if kind == "bool":
                flags[key] = True if not eq else inline.lower() in ("1", "true", "yes", "on")
                i += 1
                continue
            if eq:
                val = inline
                i += 1
            else:
                if i + 1 >= len(argv):
                    raise TogiError("missing_value", f"--{name} needs a value", next=f"togi {cmdname} --help")
                val = argv[i + 1]
                i += 2
            if kind in ("int", "float"):
                try:
                    val = int(val) if kind == "int" else float(val)
                except ValueError:
                    raise TogiError("bad_arg", f"--{name} expects a number, got {val!r}",
                                    next=f"togi {cmdname} --help")
            if kind == "list":
                flags[key].append(val)
            else:
                flags[key] = val
            continue
        pos.append(tok)
        i += 1
    flags = {k.replace("-", "_"): v for k, v in flags.items()}
    return pos, flags


def _mode(f: dict) -> str | None:
    return "json" if f.get("json") else ("text" if f.get("text") else None)


def _usage(name: str) -> Reply:
    return Reply(text=USAGE.get(name, f"togi {name}\n"))


USAGE = {
    "skill": "togi skill [<bundle> [<topic>]] [--topics]    learn togi / a bundle / one topic\n",
    "tools": "togi tools [<bundle>] [--json]    commands as Python stubs; --json for JSON Schema\n",
    "run": "togi run <file.py | -> [--timeout S] [--full] [--max-output BYTES] [--json]    code mode: bundles are modules "
           "(output is cut at 8192 bytes; --max-output raises that, never past the --full limit of 1000000)\n",
    "approve": "togi approve <id> [--ttl 15m]    a person grants an approval (terminal + typed word + passphrase)\n",
    "deny": "togi deny <id>    a person denies an approval (terminal + typed word + passphrase)\n",
    "approvals": "togi approvals [--all]    list pending approvals\n",
    "approver": "togi approver [status | setup | migrate]    status: is the approver passphrase set up; setup: a person sets or "
                "changes it (needs a terminal); migrate: a person moves r4's loose approver files into approver.d (any human "
                "command does it too)\n",
    "journal": "togi journal [<bundle>] [--since 24h] [--friction] [--limit N]\n",
    "stone": "togi stone <bundle> [--driver claude|script] [--model M] [--runs N] [--tasks a,b] [--surface cli|run|mcp]\n",
    "hone": "togi hone <bundle> [--model M] [--driver claude|script] [--diff FILE]    write an improvement proposal\n",
    "review": "togi review [<id>]    show a proposal (or list them)\n",
    "apply": "togi apply <id> [--runs N] [--driver D] [--model M] [--dry-run]    evaluate and apply a proposal (a reshape one\n"
             "    needs a person first to let its code run, then again to apply it)\n",
    "revert": "togi revert <id>    undo an applied proposal (a person: terminal + typed word + passphrase of the default "
              "approver)\n",
    "log": "togi log [<bundle>] [--verify]    the audit log of applied/rejected/reverted changes\n",
    "lock": "togi lock <bundle>    record the sha256 of every locked file (a person: terminal + typed word + passphrase "
            "of the default approver; refused while TOGI_APPROVER_HOME is set)\n",
    "bench": "togi bench <bundle> --surfaces cli,run,mcp --runs N --model M [--driver claude|script] "
             "[--task-timeout 180] [--max-cost 0.50]   (script only for one surface)\n",
    "mcp": "togi mcp serve <bundle>    expose commands as MCP tools over stdio\n",
    "add": "togi add <path-to-bundle-repo> [--local]\n",
    "remove": "togi remove <bundle>\n",
    "bundles": "togi bundles\n",
    "doctor": "togi doctor [<bundle>]    check registry, imports, manifest, tiers, locks and audit chain\n",
}


# --------------------------------------------------------------------------- main


PROCESS_ENTRY = False        # True when main() is the process's own entry point (not a library/test call)
ENTRY_ARGV: list[str] = []   # the argv it was started with, to re-exec a command (togi run) with a scrubbed env


def main(argv: list[str] | None = None) -> int:
    global PROCESS_ENTRY, ENTRY_ARGV
    if argv is None:
        PROCESS_ENTRY, ENTRY_ARGV = True, list(sys.argv[1:])
        _ensure_isolated()
    argv = list(sys.argv[1:] if argv is None else argv)
    pre_mode = _scan_mode(argv)
    common = _scan_common(argv)
    try:
        reply = dispatch(argv)
    except BrokenPipeError:  # pragma: no cover
        return 0
    except TogiError as e:
        extra = {"approval": e.approval} if isinstance(getattr(e, "approval", None), dict) else {}
        reply = Reply(env=output.error_envelope(e.status, e.code, e.message, next=e.next, **extra), mode=pre_mode)
    except ArgError as e:
        reply = Reply(env=output.error_envelope("error", e.code, e.message, next=e.next), mode=pre_mode)
    except KeyboardInterrupt:
        reply = Reply(env=output.error_envelope("error", "interrupted", "interrupted", next=None), mode=pre_mode)
    except Exception as e:  # a bug in togi itself
        if os.environ.get("TOGI_DEBUG"):
            import traceback

            traceback.print_exc()
        reply = Reply(env=output.error_envelope("internal", "internal_error", f"{type(e).__name__}: {e}",
                                                next="togi doctor"), mode=pre_mode)
    try:
        return write_reply(finalize(reply, common, argv))
    except BrokenPipeError:  # pragma: no cover
        return reply.code or 0


def _ensure_isolated() -> None:
    """Called when `main()` is the process's own entry point. Togi runs in an isolated interpreter (`python -I`: no
    current directory, no PYTHON* variables; see paths.isolated_argv). Started any other way (a pip console script,
    `python -c`), restart through the launcher before doing anything; if that is impossible carry on, and the human
    gates (approvals.human_gate) refuse to run in an interpreter that is not isolated and not clean."""
    if sys.flags.isolated or not sys.executable:
        return
    try:
        os.execv(sys.executable, paths.isolated_argv(sys.argv[1:]))
    except OSError:
        pass


def _scan_common(argv: list[str]) -> dict:
    """The host's common flags, read straight from argv so EVERY host command (and error) honours them."""
    out: dict[str, Any] = {"full": False, "fields": None, "limit": None, "offset": 0}
    i = 0
    while i < len(argv):
        tok = argv[i]
        name, eq, inline = tok.partition("=") if tok.startswith("--") else (tok, "", "")
        if name == "--full":
            out["full"] = True
        elif name in ("--fields", "--limit", "--offset"):
            val = inline if eq else (argv[i + 1] if i + 1 < len(argv) else None)
            if not eq:
                i += 1
            if name == "--fields":
                out["fields"] = output.parse_fields(val)
            elif val is not None:
                try:
                    out[name[2:]] = max(0 if name == "--offset" else 1, int(val))
                except ValueError:
                    pass
        i += 1
    return out


def _more_for_host(argv: list[str], offset: int) -> str:
    cleaned, skip = [], False
    for t in argv:
        if skip:
            skip = False
            continue
        if t == "--offset":
            skip = True
            continue
        if t.startswith("--offset="):
            continue
        cleaned.append(t)
    hint = shlex.join(["togi", *cleaned, "--offset", str(offset)])
    return hint if len(hint) <= 300 else f"re-run the same command with --offset {offset}"


def finalize(reply: Reply, common: dict, argv: list[str]) -> Reply:
    """One place that gives every host reply (and every error) the SPEC's output rules: `--fields`, `--limit` /
    `--offset` on lists, and the ~8 KB cap with a `more`/`--full` hint. Bundle commands are shaped in host.call."""
    if reply.silent or reply.capped:
        return reply
    full = common["full"]
    if reply.env is not None:
        env = reply.env
        if env.get("ok") and "data" in env:
            if common["fields"] and not reply.projected:
                env = {**env, "data": output.project(env["data"], common["fields"])}
            if isinstance(env["data"], list) and not reply.paged and (
                    len(env["data"]) > (common["limit"] or output.DEFAULT_LIMIT) or common["offset"]):
                limit = common["limit"] or output.DEFAULT_LIMIT
                data, info = output.paginate(env["data"], limit, common["offset"])
                env = {**env, "data": data}
                if "next_offset" in info:
                    env["more"] = _more_for_host(argv, info["next_offset"])
                    env["total"] = info["total"]
        if reply.cap and not full:
            env, _ = output.fit_any(env, output.MAX_BYTES, None)
        reply.env = env
    elif reply.text is not None and reply.cap and not full:
        reply.text = output.cap_text(reply.text, output.MAX_BYTES)
    return reply


def _scan_mode(argv: list[str]) -> str | None:
    if "--json" in argv:
        return "json"
    if "--text" in argv:
        return "text"
    return None


def write_reply(reply: Reply) -> int:
    if reply.silent:
        return reply.code or 0
    mode = reply.mode
    want_text = mode == "text" or (mode is None and sys.stdout.isatty())
    if reply.text is not None:
        if mode == "json":
            out = output.dumps(output.ok_envelope({"text": reply.text.rstrip("\n")}))
        else:
            out = reply.text.rstrip("\n")
        code = reply.code or 0
    else:
        env = reply.env or output.ok_envelope(None)
        out = output.render_text(env) if want_text else output.dumps(env)
        code = reply.code if reply.code is not None else output.exit_code_for(env)
    sys.stdout.write(out + "\n")
    sys.stdout.flush()
    return code


def dispatch(argv: list[str]) -> Reply:
    pre = {"json": False, "text": False}
    while argv and argv[0] in ("--json", "--text"):
        pre[argv.pop(0)[2:]] = True
    mode = "json" if pre["json"] else ("text" if pre["text"] else None)
    if not argv or argv[0] in ("skill", "help", "--help", "-h"):
        rest = argv[1:] if argv and argv[0] == "skill" else []
        return h_skill(rest, mode)
    cmd, rest = argv[0], argv[1:]
    if cmd not in ("version", "--version"):
        paths.require_safe_layout()     # $TOGI_HOME may not be, hold or sit in the approver's own directory (r5)
    handler = _HANDLERS.get(cmd)
    if handler:
        if cmd in GUARDED_COMMANDS:
            _host_attempt(cmd, rest)
        if mode:
            rest = rest + (["--json"] if mode == "json" else ["--text"])
        return handler(rest)
    if cmd == "--version":
        return Reply(env=output.ok_envelope({"togi": __version__}), mode=mode)
    ents = registry.entries()
    if cmd in ents:
        return run_bundle_command(cmd, rest, mode)
    # unknown first word
    pool = list(ents) + list(HOST_COMMANDS)
    close = difflib.get_close_matches(cmd, pool, n=2, cutoff=0.6)
    hint = f"; did you mean {' or '.join(repr(c) for c in close)}?" if close else ""
    raise TogiError("unknown_command", f"'{cmd}' is not a togi command or a registered bundle{hint}",
                    next="togi skill")


# --------------------------------------------------------------------------- skill / tools / bundles


def h_skill(argv: list[str], mode: str | None = None) -> Reply:
    from . import skills

    pos, f = parse_flags(argv, {"topics": "bool"}, "skill")
    mode = _mode(f) or mode
    if f["help"]:
        return _usage("skill")
    if not pos:
        return Reply(text=skills.index_text(), mode=mode)
    man = registry.manifest_for(pos[0])
    if f["topics"]:
        ts = skills.topics(man)
        return Reply(env=output.ok_envelope(ts), mode=mode) if mode == "json" else Reply(
            text="\n".join(f"{t['topic']}: {t['summary']}" for t in ts) or "(no topics)\n", mode=mode)
    if len(pos) >= 2:
        txt = skills.topic_text(man, pos[1])
        if txt is None:
            avail = ", ".join(t["topic"] for t in skills.topics(man)) or "(none)"
            raise TogiError("unknown_topic", f"{man.name} has no topic '{pos[1]}'; topics: {avail}",
                            next=f"togi skill {man.name} --topics")
        return Reply(text=txt, mode=mode)
    tools_text = None
    if not (man.repo / man.skills / "SKILL.md").is_file():
        loaded = registry.load(pos[0])
        tools_text = "\n".join(c.stub() for c in loaded.bundle.commands.values())
    return Reply(text=skills.bundle_text(man, tools_text), mode=mode)


def h_tools(argv: list[str]) -> Reply:
    pos, f = parse_flags(argv, {}, "tools")
    if f["help"]:
        return _usage("tools")
    names = pos[:1] or sorted(registry.entries())
    if pos and pos[0] not in registry.entries():
        raise TogiError("unknown_bundle", f"no bundle named '{pos[0]}' is registered", next="togi bundles")
    lines, schemas = [], []
    for name in names:
        try:
            loaded = registry.load(name)
        except TogiError as e:
            if pos:
                raise
            lines.append(f"# {name}: could not load ({e.code}); run: togi doctor")
            continue
        for c in loaded.bundle.commands.values():
            lines.append(c.stub())
            schemas.append(c.describe())
    if f["json"]:
        return Reply(env=output.ok_envelope(schemas), mode="json")
    return Reply(text="\n".join(lines) + "\n", mode=_mode(f))


def h_bundles(argv: list[str]) -> Reply:
    from .manifest import ManifestError, load_manifest

    pos, f = parse_flags(argv, {}, "bundles")
    out = []
    for name, ent in sorted(registry.entries().items()):
        row: dict[str, Any] = {"name": name, "path": ent.path, "source": ent.source}
        try:
            man = load_manifest(ent.path)
            row.update(version=man.version, summary=man.summary, ok=True)
        except ManifestError as e:
            row.update(ok=False, problem=str(e), next="togi doctor")
        out.append(row)
    return Reply(env=output.ok_envelope(out), mode=_mode(f))


def h_add(argv: list[str]) -> Reply:
    pos, f = parse_flags(argv, {"local": "bool"}, "add")
    if f["help"]:
        return _usage("add")
    if len(pos) != 1:
        raise TogiError("bad_arg", "togi add needs exactly one path", next="togi add <path-to-bundle-repo>")
    man, target = registry.add(pos[0], local=f["local"])
    return Reply(env=output.ok_envelope({"added": man.name, "path": str(man.repo), "registry": str(target),
                                         "version": man.version}) | {"next": f"togi skill {man.name}"},
                 mode=_mode(f))


def h_remove(argv: list[str]) -> Reply:
    pos, f = parse_flags(argv, {}, "remove")
    if f["help"]:
        return _usage("remove")
    if len(pos) != 1:
        raise TogiError("bad_arg", "togi remove needs a bundle name", next="togi bundles")
    removed = registry.remove(pos[0])
    return Reply(env=output.ok_envelope({"removed": pos[0], "from": [str(p) for p in removed]}), mode=_mode(f))


# --------------------------------------------------------------------------- running a bundle command


def run_bundle_command(name: str, rest: list[str], pre_mode: str | None = None) -> Reply:
    from . import host
    from .cliargs import parse_command_args

    loaded = registry.load(name)
    bundle = loaded.bundle
    cmd, remain = bundle.find(rest)
    shadow = {p.name for p in cmd.params} if cmd else set()
    mode_tokens = pre_mode or (
        "json" if ("--json" in rest and "json" not in shadow) else
        ("text" if ("--text" in rest and "text" not in shadow) else None))
    if cmd is None:
        words = []  # the words before the first flag: a command attempt, never flag values
        for t in rest:
            if t.startswith("-"):
                break
            words.append(t)
        words = words[:3]
        if not words:  # `togi notes`, `togi notes --help`: show the bundle's skill (and note the lookup)
            from . import journal, skills

            if any(t in ("--help", "-h") for t in rest):
                journal.append(name, journal.make_entry(surface="cli", bundle=name, command=None, event="help"))
            return Reply(text=skills.bundle_text(loaded.manifest, "\n".join(
                c.stub() for c in bundle.commands.values())), mode=mode_tokens)
        close = bundle.suggest(rest)
        attempted = " ".join(words)
        hint = f"; did you mean '{close[0]}'?" if close else ""
        err = ArgError(f"'{attempted}' is not a {name} command{hint}", code="unknown_command", flag=attempted,
                       next=f"togi tools {name}" if not close else f"togi {name} {close[0]} --help")
        res = host.call(loaded, None, {}, host.CallOptions(argv=[name, *rest]), attempted=attempted, error=err)
        return Reply(env=res.envelope, mode=mode_tokens, code=res.exit_code, capped=True)
    argv_full = [name, *rest]
    try:
        parsed = parse_command_args(cmd, remain)
    except ArgError as e:
        res = host.call(loaded, cmd, {}, host.CallOptions(argv=argv_full), error=e)
        return Reply(env=res.envelope, mode=mode_tokens, code=res.exit_code, capped=True)
    mode = "json" if parsed.json else ("text" if parsed.text else pre_mode)
    if parsed.help:
        from . import journal

        journal.append(name, journal.make_entry(surface="cli", bundle=name, command=cmd.name, event="help",
                                                effect=cmd.effect))
        return Reply(text=command_help(loaded, cmd), mode=mode)
    opts = host.CallOptions(surface="cli", fields=parsed.fields, limit=parsed.limit, offset=parsed.offset,
                            full=parsed.full, dry_run=parsed.dry_run, approval=parsed.approval, argv=argv_full)
    res = host.call(loaded, cmd, parsed.args, opts)
    return Reply(env=res.envelope, mode=mode, code=res.exit_code, capped=True)


def command_help(loaded, cmd) -> str:
    lines = [f"togi {loaded.name} {cmd.name}  ({cmd.effect})", cmd.summary]
    if cmd.description:
        lines.append(cmd.description)
    lines.append("flags:")
    for p in cmd.params:
        label = p.flag if p.kind == "bool" else f"{p.flag} {p.type_name().upper() if p.kind != 'literal' else '|'.join(map(str, p.choices))}"
        note = "required" if p.required else f"default {p.default!r}"
        extra = f"  {p.doc}" if p.doc else ""
        lines.append(f"  {label:<28} {note}{extra}")
    lines.append("  --fields a,b.c  --limit N  --offset N  --full  --json  --text")
    if cmd.effect != "read":
        lines.append("  --dry-run                    describe without doing it")
    if loaded.requires_approval(cmd.effect):
        lines.append("  --approval ap_...            this command needs a person's approval first (exit 3)")
    if cmd.examples:
        lines.append("examples:")
        lines += [f"  {e}" for e in cmd.examples]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- approvals (human-only)


def _host_journal(command: str, ok: bool, status: str, code: str | None, **extra: Any) -> None:
    from . import journal

    journal.append(None, journal.make_entry(surface="cli", bundle=None, command=command, ok=ok, status=status,
                                            error_code=code, effect="admin", **extra))


def _host_attempt(command: str, argv: list[str]) -> None:
    """Record that a human-only command was invoked, BEFORE its flags are parsed: argv shape only (flag names
    kept, every other token replaced by `<str>`/`<num>`), so no value ever reaches the journal."""
    from . import journal

    journal.append(None, journal.make_entry(surface="cli", bundle=None, command=command, event="host_attempt",
                                            status="attempted", effect="admin", argv=journal.argv_shape(argv),
                                            approver_scope=paths.approver_scope()))


def _decide(argv: list[str], decision: str) -> Reply:
    from . import approvals

    name = "approve" if decision == "approve" else "deny"
    pos, f = parse_flags(argv, {"ttl": "str"}, name)
    if f["help"]:
        return _usage(name)
    try:
        approvals.human_gate(name)
        if len(pos) != 1:
            raise TogiError("bad_arg", f"togi {name} needs an approval id", next="togi approvals")
        approvals.require_approver()
        rec = approvals.load(pos[0])
        st = approvals.effective_status(rec)
        if st != "pending":
            raise TogiError("approval_not_pending", f"approval {rec['id']} is {st}, not pending",
                            next="togi approvals", status="refused")
        # What is shown is what is signed: the pending file is the requester's, so an approval is re-derived from the
        # live repo and the bundle's own command first (refusing on any mismatch) and THAT is displayed and signed.
        verified = approvals.verify_pending(pos[0], rec) if decision == "approve" else None
        ttl = None
        if f["ttl"]:
            from .journal import parse_since
            import time

            ttl = max(1, int(time.time() - parse_since(f["ttl"])))
        word = verified["confirm_word"] if verified else approvals.confirm_word_for_denial(rec)
        # the expiry shown is the expiry signed: computed once, here, from now + the TTL (never the requester's field)
        import time as _time
        expires = approvals._iso(_time.time() + (ttl if ttl else approvals.ttl_seconds())) if verified else None
        user = approvals.confirm_and_authenticate(approvals.prompt_lines_for(rec, verified, expires), word, decision, name)
        rec = approvals.grant(rec["id"], decision, user, ttl, expect_hash=rec["args_hash"], verified=verified,
                              expires=expires)
    except TogiError as e:
        _host_journal(name, False, e.status, e.code)
        raise
    _host_journal(name, True, "ok", None)
    return Reply(env=output.ok_envelope({"approval": rec["id"], "status": rec["status"],
                                         "expires": rec["expires"], "decided_by": rec["decided_by"],
                                         "scope": rec["scope"]}),
                 mode=_mode(f))


def h_approver(argv: list[str]) -> Reply:
    from . import approvals, approver

    pos, f = parse_flags(argv, {}, "approver")
    if f["help"]:
        return _usage("approver")
    sub = pos[0] if pos else "status"
    if sub == "status":
        return Reply(env=output.ok_envelope(approver.status()), mode=_mode(f))
    if sub == "migrate" and len(pos) == 1:
        try:
            approvals.human_gate("approver migrate", migrate=False)
            moved = approver.migrate_legacy() if paths.approver_override() is None else []
        except TogiError as e:
            _host_journal("approver migrate", False, e.status, e.code)
            raise
        _host_journal("approver migrate", True, "ok", None)
        return Reply(env=output.ok_envelope({"moved": moved, "dir": str(paths.approver_home()),
                                             "layout": approver.layout_state()["state"]}), mode=_mode(f))
    if sub != "setup" or len(pos) > 1:
        raise TogiError("bad_arg", f"unknown approver subcommand {sub!r}", next="togi approver status")
    try:
        approvals.human_gate("approver setup")
        data = approver.setup()
    except TogiError as e:
        _host_journal("approver setup", False, e.status, e.code)
        raise
    _host_journal("approver setup", True, "ok", None)
    return Reply(env=output.ok_envelope(data), mode=_mode(f))


def h_approvals(argv: list[str]) -> Reply:
    from . import approvals

    pos, f = parse_flags(argv, {"all": "bool"}, "approvals")
    if f["help"]:
        return _usage("approvals")
    return Reply(env=output.ok_envelope(approvals.list_all(include_done=f["all"])), mode=_mode(f))


# --------------------------------------------------------------------------- journal


def h_journal(argv: list[str]) -> Reply:
    from . import journal

    pos, f = parse_flags(argv, {"since": "str", "friction": "bool"}, "journal")
    if f["help"]:
        return _usage("journal")
    try:
        since = journal.parse_since(f["since"])
    except ValueError as e:
        raise TogiError("bad_arg", str(e), next="togi journal --since 24h")
    entries = journal.read(pos[0] if pos else None, since)
    if f["friction"]:
        return Reply(env=output.ok_envelope(journal.friction(entries)), mode=_mode(f))
    limit = f["limit"] or output.DEFAULT_LIMIT
    total = len(entries)
    view = entries[-limit:]
    if f["fields"]:
        view = output.project(view, output.parse_fields(f["fields"]))
    env = output.ok_envelope(view)
    if total > len(view):
        env["total"] = total
        env["more"] = f"togi journal {pos[0] if pos else ''} --limit {min(total, limit * 4)}".replace("  ", " ")
    return Reply(env=env, mode=_mode(f), paged=True, projected=True)


# --------------------------------------------------------------------------- later-milestone handlers (lazy)


def _lazy(modname: str, func: str) -> Callable[[list[str]], Reply]:
    def handler(argv: list[str]) -> Reply:
        import importlib

        mod = importlib.import_module(f".{modname}", __package__)
        return getattr(mod, func)(argv)

    return handler


_HANDLERS: dict[str, Callable[[list[str]], Reply]] = {
    "tools": h_tools,
    "bundles": h_bundles,
    "add": h_add,
    "remove": h_remove,
    "approve": lambda a: _decide(a, "approve"),
    "deny": lambda a: _decide(a, "deny"),
    "approvals": h_approvals,
    "approver": h_approver,
    "journal": h_journal,
    "version": lambda a: Reply(env=output.ok_envelope({"togi": __version__}), mode=_scan_mode(a)),
    "run": _lazy("runmode", "h_run"),
    "stone": _lazy("stone", "h_stone"),
    "hone": _lazy("hone", "h_hone"),
    "review": _lazy("apply", "h_review"),
    "apply": _lazy("apply", "h_apply"),
    "revert": _lazy("apply", "h_revert"),
    "log": _lazy("audit", "h_log"),
    "lock": _lazy("audit", "h_lock"),
    "bench": _lazy("bench", "h_bench"),
    "mcp": _lazy("mcp_bridge", "h_mcp"),
    "doctor": _lazy("doctor", "h_doctor"),
}
