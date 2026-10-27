"""Bundle, the command decorator, and the type mapping (SPEC section 2).

One Python definition of a command produces every surface: CLI flags, JSON Schema, code-mode stubs,
MCP tools. Everything here is derived from the function signature, so there is nothing to keep in sync.
"""
from __future__ import annotations

import decimal
import inspect
import re
import types
import typing
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Literal, Union, get_args, get_origin

from .errors import ArgError

EFFECTS = ("read", "write", "money", "admin")
# Flags the host owns. A command parameter with one of the GLOBAL names shadows the host flag for that
# command (the command's own meaning wins); RESERVED names can never be command parameters.
GLOBAL_FLAGS = ("json", "text", "fields", "limit", "offset", "full")
RESERVED_PARAMS = ("help", "approval", "dry_run")
_SCALARS = {str: "str", int: "int", float: "float", Decimal: "decimal"}


@dataclass
class Param:
    name: str
    kind: str  # str | int | float | bool | decimal | list | literal
    required: bool = True
    default: Any = None
    optional: bool = False  # Optional[...] / may be None
    item_kind: str | None = None  # for list
    choices: tuple = ()
    doc: str = ""

    @property
    def flag(self) -> str:
        return "--" + self.name.replace("_", "-")

    def type_name(self) -> str:
        """Short type label used in stubs/help/journal shapes."""
        if self.kind == "list":
            return f"list[{_KIND_LABEL[self.item_kind or 'str']}]"
        if self.kind == "literal":
            return "Literal[" + ", ".join(repr(c) for c in self.choices) + "]"
        return _KIND_LABEL[self.kind]

    def shape_name(self) -> str:
        """Type name recorded in the journal (never the value)."""
        if self.kind == "list":
            return f"list[{self.item_kind or 'str'}]"
        if self.kind == "literal":
            return "literal"
        return self.kind


_KIND_LABEL = {"str": "str", "int": "int", "float": "float", "bool": "bool", "decimal": "Decimal",
               "list": "list", "literal": "Literal"}


@dataclass
class Command:
    bundle: str
    words: tuple[str, ...]
    func: Callable
    effect: str = "read"
    examples: tuple[str, ...] = ()
    params: list[Param] = field(default_factory=list)
    ctx_param: str | None = None
    summary: str = ""
    description: str = ""
    returns: str = "Any"
    confirm: str | None = None  # name of the parameter whose value a human types to approve

    @property
    def name(self) -> str:
        return " ".join(self.words)

    @property
    def api_name(self) -> str:
        return "_".join(w.replace("-", "_") for w in self.words)

    def param(self, name: str) -> Param | None:
        for p in self.params:
            if p.name == name:
                return p
        return None

    # ------------------------------------------------------------------ views

    def stub(self, max_summary: int = 80) -> str:
        """`crowd.drop_status(drop: str, verbose: bool = False) -> dict  # read - Show a drop's...`"""
        parts = []
        for p in self.params:
            label = p.type_name()
            if p.optional and p.kind != "bool":
                label += " | None"
            piece = f"{p.name}: {label}"
            if not p.required:
                piece += f" = {_default_repr(p.default)}"
            parts.append(piece)
        summary = self.summary
        if len(summary) > max_summary:
            summary = summary[: max_summary - 1].rstrip() + "…"
        return f"{self.bundle}.{self.api_name}({', '.join(parts)}) -> {self.returns}  # {self.effect} · {summary}"

    def json_schema(self) -> dict:
        props: dict[str, Any] = {}
        required = []
        for p in self.params:
            s = _param_schema(p)
            if p.doc:
                s["description"] = p.doc
            if not p.required and p.default is not None:
                s["default"] = _jsonable_default(p.default)
            props[p.name] = s
            if p.required:
                required.append(p.name)
        schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
        if required:
            schema["required"] = required
        return schema

    def describe(self) -> dict:
        return {
            "name": f"{self.bundle}.{self.api_name}",
            "bundle": self.bundle,
            "command": self.name,
            "effect": self.effect,
            "summary": self.summary,
            "examples": list(self.examples),
            "input_schema": self.json_schema(),
        }


def _default_repr(v: Any) -> str:
    if isinstance(v, Decimal):
        return f"Decimal('{v}')"
    return repr(v)


def _jsonable_default(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, (str, int, float, bool, list, dict)) or v is None:
        return v
    return str(v)


def _param_schema(p: Param) -> dict:
    if p.kind == "str":
        s: dict = {"type": "string"}
    elif p.kind == "int":
        s = {"type": "integer"}
    elif p.kind == "float":
        s = {"type": "number"}
    elif p.kind == "bool":
        s = {"type": "boolean"}
    elif p.kind == "decimal":
        s = {"type": "string", "format": "decimal", "description": "decimal number as a string, e.g. \"33.00\""}
    elif p.kind == "literal":
        types_ = {type(c) for c in p.choices}
        s = {"enum": list(p.choices)}
        if types_ == {str}:
            s["type"] = "string"
        elif types_ == {int}:
            s["type"] = "integer"
    elif p.kind == "list":
        item = Param(p.name, p.item_kind or "str")
        s = {"type": "array", "items": _param_schema(item)}
    else:  # pragma: no cover
        s = {}
    return s


# --------------------------------------------------------------------------- type analysis


def _classify(tp: Any, where: str) -> tuple[str, tuple, str | None, bool]:
    """Return (kind, choices, item_kind, optional) for an annotation."""
    optional = False
    origin = get_origin(tp)
    if origin is Union or origin is types.UnionType:
        args = get_args(tp)
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) != len(args):
            optional = True
        if len(non_none) != 1:
            raise TypeError(f"{where}: unsupported union type {tp!r}; use one type or Optional[...]")
        tp = non_none[0]
        origin = get_origin(tp)
    if tp is bool:
        return "bool", (), None, optional
    if tp in _SCALARS:
        return _SCALARS[tp], (), None, optional
    if origin is Literal:
        choices = get_args(tp)
        if not choices or not all(isinstance(c, (str, int)) and not isinstance(c, bool) for c in choices):
            raise TypeError(f"{where}: Literal[...] must contain str or int values")
        return "literal", tuple(choices), None, optional
    if origin in (list, typing.List):
        args = get_args(tp)
        item = args[0] if args else str
        if item not in _SCALARS:
            raise TypeError(f"{where}: only list[str|int|float|Decimal] is supported, got {tp!r}")
        return "list", (), _SCALARS[item], optional
    if tp is typing.Any or tp is inspect.Parameter.empty:
        return "str", (), None, optional
    raise TypeError(
        f"{where}: unsupported parameter type {tp!r}. Use str, int, float, bool, Decimal, list[str], "
        "Literal[...] or Optional[...]."
    )


def _resolve_hints(func: Callable) -> dict:
    try:
        return typing.get_type_hints(func)
    except Exception:
        hints = {}
        sig = inspect.signature(func)
        for name, prm in sig.parameters.items():
            ann = prm.annotation
            if isinstance(ann, str):
                try:
                    ann = eval(ann, getattr(func, "__globals__", {}), {"Decimal": Decimal, "Literal": Literal})
                except Exception:
                    ann = inspect.Parameter.empty
            hints[name] = ann
        return hints


def _type_label(tp: Any) -> str:
    if tp is inspect.Signature.empty or tp is typing.Any:
        return "Any"
    if tp is type(None):
        return "None"
    if isinstance(tp, type) and not get_args(tp):
        return tp.__name__
    return str(tp).replace("typing.", "").replace("<class '", "").replace("'>", "")


def _parse_arg_docs(doc: str) -> dict[str, str]:
    """Pick `name: text` lines out of a Google-style `Args:` block."""
    out: dict[str, str] = {}
    lines = doc.splitlines()
    in_args = False
    current: str | None = None
    base_indent = None
    for line in lines:
        stripped = line.strip()
        if re.fullmatch(r"(Args|Arguments|Parameters):", stripped):
            in_args = True
            current = None
            base_indent = None
            continue
        if not in_args:
            continue
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if re.fullmatch(r"[A-Z][A-Za-z ]*:", stripped) and indent == 0:
            break  # next section header at column 0
        m = re.match(r"(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)", stripped)
        if m and (base_indent is None or indent <= base_indent):
            base_indent = indent if base_indent is None else base_indent
            current = m.group(1)
            out[current] = m.group(2).strip()
        elif current:
            out[current] = (out[current] + " " + stripped).strip()
    return out


def build_command(bundle: str, words: tuple[str, ...], func: Callable, effect: str, examples: tuple[str, ...],
                  summary: str | None = None, confirm: str | None = None) -> Command:
    if effect not in EFFECTS:
        raise ValueError(f"effect must be one of {EFFECTS}, got {effect!r}")
    sig = inspect.signature(func)
    hints = _resolve_hints(func)
    doc = inspect.getdoc(func) or ""
    arg_docs = _parse_arg_docs(doc)
    first = ""
    for line in doc.splitlines():
        if line.strip():
            first = line.strip()
            break
    params: list[Param] = []
    ctx_param = None
    for name, prm in sig.parameters.items():
        where = f"{func.__qualname__}({name})"
        if prm.kind in (prm.VAR_POSITIONAL, prm.VAR_KEYWORD):
            raise TypeError(f"{where}: *args/**kwargs are not supported in togi commands")
        if prm.kind == prm.POSITIONAL_ONLY:
            raise TypeError(f"{where}: positional-only parameters are not supported")
        ann = hints.get(name, prm.annotation)
        if name == "ctx" or _is_context(ann):
            ctx_param = name
            continue
        if name in RESERVED_PARAMS:
            raise ValueError(f"{where}: '{name}' is reserved by the host; rename the parameter")
        kind, choices, item_kind, optional = _classify(ann, where)
        if ann is inspect.Parameter.empty and prm.default is not inspect.Parameter.empty and prm.default is not None:
            # unannotated: infer from the default
            kind, choices, item_kind, optional = _classify(type(prm.default), where)
        has_default = prm.default is not inspect.Parameter.empty
        default = prm.default if has_default else None
        required = not has_default
        if optional and not has_default:
            required, default = False, None
        if kind == "bool" and not has_default:
            required, default = False, False
        if kind == "list" and not has_default and optional:
            required, default = False, None
        if default is None and has_default:
            optional = True
        params.append(Param(name=name, kind=kind, required=required, default=default, optional=optional,
                            item_kind=item_kind, choices=choices, doc=arg_docs.get(name, "")))
    rest = [ln for ln in doc.splitlines()[1:]]
    description = "\n".join(rest).strip()
    cmd = Command(
        bundle=bundle, words=words, func=func, effect=effect, examples=tuple(examples), params=params,
        ctx_param=ctx_param, summary=summary or first, description=description,
        returns=_type_label(hints.get("return", sig.return_annotation)), confirm=confirm,
    )
    if confirm and cmd.param(confirm) is None:
        raise ValueError(f"confirm={confirm!r} is not a parameter of {func.__qualname__}")
    return cmd


def _is_context(ann: Any) -> bool:
    from .context import Context

    return ann is Context or (isinstance(ann, str) and ann.split(".")[-1] == "Context")


# --------------------------------------------------------------------------- coercion


def coerce_scalar(kind: str, raw: Any, flag: str, choices: tuple = ()) -> Any:
    """Coerce one value (a CLI string or a JSON value) to the Python type for `kind`."""
    try:
        if kind == "str":
            if isinstance(raw, str):
                return raw
            if isinstance(raw, (int, float, Decimal)) and not isinstance(raw, bool):
                return str(raw)
            raise ValueError("expected text")
        if kind == "int":
            if isinstance(raw, bool):
                raise ValueError("expected a whole number")
            if isinstance(raw, int):
                return raw
            if isinstance(raw, float) and raw.is_integer():
                return int(raw)
            if isinstance(raw, str):
                return int(raw.strip())
            raise ValueError("expected a whole number")
        if kind == "float":
            if isinstance(raw, bool):
                raise ValueError("expected a number")
            if isinstance(raw, (int, float)):
                return float(raw)
            if isinstance(raw, str):
                return float(raw.strip())
            raise ValueError("expected a number")
        if kind == "decimal":
            if isinstance(raw, bool):
                raise ValueError("expected a decimal number")
            if isinstance(raw, Decimal):
                d = raw
            elif isinstance(raw, float):
                d = Decimal(repr(raw))
            elif isinstance(raw, (int, str)):
                d = Decimal(str(raw).strip())
            else:
                raise ValueError("expected a decimal number")
            if not d.is_finite():
                raise ValueError("expected a finite decimal number")
            return d
        if kind == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.strip().lower() in ("true", "1", "yes", "on"):
                return True
            if isinstance(raw, str) and raw.strip().lower() in ("false", "0", "no", "off"):
                return False
            raise ValueError("expected true or false")
        if kind == "literal":
            for c in choices:
                if raw == c or (isinstance(raw, str) and raw == str(c)):
                    return c
            raise ValueError("must be one of " + ", ".join(str(c) for c in choices))
    except (ValueError, decimal.InvalidOperation) as e:
        msg = str(e) if isinstance(e, ValueError) and not isinstance(e, decimal.InvalidOperation) else "expected a decimal number"
        shown = raw if isinstance(raw, (str, int, float)) else type(raw).__name__
        raise ArgError(f"{flag}: {msg} (got {shown!r})", code="bad_arg", flag=flag) from None
    raise ArgError(f"{flag}: unsupported type {kind}", code="bad_arg", flag=flag)  # pragma: no cover


def coerce_json(p: Param, raw: Any) -> Any:
    """Coerce a JSON-ish value (code mode, MCP) for parameter `p`."""
    if raw is None:
        if p.optional or not p.required:
            return None
        raise ArgError(f"{p.flag}: value is required", code="missing_arg", flag=p.flag)
    if p.kind == "list":
        items = raw if isinstance(raw, (list, tuple)) else [raw]
        return [coerce_scalar(p.item_kind or "str", i, p.flag) for i in items]
    return coerce_scalar(p.kind, raw, p.flag, p.choices)


# --------------------------------------------------------------------------- Bundle


class Bundle:
    """A namespace of commands. `Bundle("crowd")`; the name must match togi.toml [bundle].name."""

    def __init__(self, name: str):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name or ""):
            raise ValueError(f"bundle name {name!r} must be letters/digits/_/- and start with a letter")
        self.name = name
        self.commands: dict[str, Command] = {}
        self._by_api: dict[str, Command] = {}

    # decorator -------------------------------------------------------------
    def command(self, words: str | tuple[str, ...] | list[str], *, effect: str = "read",
                examples: list[str] | tuple[str, ...] = (), summary: str | None = None,
                confirm: str | None = None) -> Callable[[Callable], Callable]:
        word_tuple = tuple(words.split()) if isinstance(words, str) else tuple(words)
        if not word_tuple or not all(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", w) for w in word_tuple):
            raise ValueError(f"command words {words!r} must be lowercase words like 'drop status'")

        def deco(func: Callable) -> Callable:
            cmd = build_command(self.name, word_tuple, func, effect, tuple(examples), summary, confirm)
            if cmd.name in self.commands:
                raise ValueError(f"duplicate command {cmd.name!r} in bundle {self.name!r}")
            if cmd.api_name in self._by_api:
                raise ValueError(f"command {cmd.name!r} collides with {self._by_api[cmd.api_name].name!r} "
                                 f"(both map to api name {cmd.api_name!r})")
            self.commands[cmd.name] = cmd
            self._by_api[cmd.api_name] = cmd
            func.__togi_command__ = cmd  # type: ignore[attr-defined]
            return func

        return deco

    # lookup ----------------------------------------------------------------
    def find(self, tokens: list[str]) -> tuple[Command | None, list[str]]:
        """Longest-prefix match of leading non-flag tokens against command words."""
        words = []
        for t in tokens:
            if t.startswith("-"):
                break
            words.append(t)
        for n in range(len(words), 0, -1):
            cmd = self.commands.get(" ".join(words[:n]))
            if cmd:
                return cmd, tokens[n:]
        return None, tokens

    def get(self, name: str) -> Command | None:
        """Look up by 'drop status', 'drop-status' or api name 'drop_status'."""
        name = name.strip()
        return self.commands.get(" ".join(name.split())) or self._by_api.get(name.replace(" ", "_").replace("-", "_"))

    def suggest(self, tokens: list[str]) -> list[str]:
        import difflib

        words = []
        for t in tokens:
            if t.startswith("-"):
                break
            words.append(t)
        target = " ".join(words)
        return difflib.get_close_matches(target, list(self.commands), n=3, cutoff=0.5) if target else []
