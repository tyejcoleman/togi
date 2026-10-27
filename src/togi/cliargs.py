"""Parse `--flags` for a bundle command (and the host's global flags) with agent-friendly errors."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from .bundle import GLOBAL_FLAGS, Command, Param, coerce_scalar
from .errors import ArgError
from .output import DEFAULT_LIMIT, parse_fields

_NUMBER = re.compile(r"-\d+(\.\d+)?\Z")


@dataclass
class Parsed:
    args: dict[str, Any] = field(default_factory=dict)
    json: bool = False
    text: bool = False
    fields: list[str] | None = None
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    full: bool = False
    dry_run: bool = False
    approval: str | None = None
    help: bool = False


def parse_command_args(cmd: Command, tokens: list[str]) -> Parsed:
    by_flag: dict[str, Param] = {}
    negations: dict[str, Param] = {}
    for p in cmd.params:
        by_flag[p.name.replace("_", "-")] = p
        if p.kind == "bool" and not p.required and p.default is True:
            negations["no-" + p.name.replace("_", "-")] = p
    shadowed = {p.name for p in cmd.params}
    host_flags = {g for g in GLOBAL_FLAGS if g not in shadowed} | {"dry-run", "approval", "help"}
    out = Parsed()
    lists: dict[str, list[str]] = {}
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "-h":
            out.help = True
            i += 1
            continue
        if not tok.startswith("--") or tok == "--":
            hint = f"flags look like --name value; try: togi {cmd.bundle} {cmd.name} --help"
            raise ArgError(f"unexpected value {tok!r}; {hint}", code="unexpected_argument", flag=tok,
                           next=f"togi {cmd.bundle} {cmd.name} --help")
        name, eq, inline = tok[2:].partition("=")
        key = name.replace("_", "-")
        if key in by_flag:
            p = by_flag[key]
            if p.kind == "bool":
                out.args[p.name] = coerce_scalar("bool", inline, p.flag) if eq else True
                i += 1
                continue
            value, i = _take_value(tokens, i, eq, inline, p.flag, cmd)
            if p.kind == "list":
                lists.setdefault(p.name, []).append(value)
            else:
                out.args[p.name] = coerce_scalar(p.kind, value, p.flag, p.choices)
            continue
        if key in negations:
            out.args[negations[key].name] = False
            i += 1
            continue
        if key in host_flags:
            i = _host_flag(out, key, tokens, i, eq, inline, cmd)
            continue
        allowed = sorted(set(by_flag) | set(negations) | host_flags)
        close = difflib.get_close_matches(key, allowed, n=1, cutoff=0.6)
        msg = f"unknown flag --{name}"
        if close:
            msg += f"; did you mean --{close[0]}?"
        else:
            msg += f"; {cmd.name} takes: " + (", ".join(p.flag for p in cmd.params) or "(no flags)")
        raise ArgError(msg, code="unknown_flag", flag=f"--{name}", next=f"togi {cmd.bundle} {cmd.name} --help")
    for pname, items in lists.items():
        p = cmd.param(pname)
        out.args[pname] = [coerce_scalar(p.item_kind or "str", v, p.flag) for v in items]
    return out


def _take_value(tokens: list[str], i: int, eq: str, inline: str, flag: str, cmd: Command) -> tuple[str, int]:
    if eq:
        return inline, i + 1
    if i + 1 >= len(tokens):
        raise ArgError(f"{flag} needs a value", code="missing_value", flag=flag,
                       next=f"togi {cmd.bundle} {cmd.name} --help")
    nxt = tokens[i + 1]
    if nxt.startswith("--") and len(nxt) > 2:
        raise ArgError(f"{flag} needs a value but got {nxt!r}; write {flag}=VALUE if the value starts with --",
                       code="missing_value", flag=flag, next=f"togi {cmd.bundle} {cmd.name} --help")
    return nxt, i + 2


def _host_flag(out: Parsed, key: str, tokens: list[str], i: int, eq: str, inline: str, cmd: Command) -> int:
    if key in ("json", "text", "full", "dry-run", "help"):
        val = True if not eq else coerce_scalar("bool", inline, "--" + key)
        setattr(out, key.replace("-", "_"), val)
        return i + 1
    value, j = _take_value(tokens, i, eq, inline, "--" + key, cmd)
    if key == "fields":
        out.fields = parse_fields(value)
    elif key == "limit":
        out.limit = max(1, coerce_scalar("int", value, "--limit"))
    elif key == "offset":
        out.offset = max(0, coerce_scalar("int", value, "--offset"))
    elif key == "approval":
        out.approval = value
    return j
