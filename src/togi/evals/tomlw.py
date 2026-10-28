"""A small TOML writer (the stdlib reads TOML but cannot write it). Enough for items, suites and outcomes:
strings, ints, floats, bools, lists, nested tables and arrays of tables. `None` values are dropped."""
from __future__ import annotations

import json
import math
import re
from typing import Any

_BARE = re.compile(r"[A-Za-z0-9_-]+\Z")


def _key(k: str) -> str:
    return k if _BARE.match(k) else json.dumps(k, ensure_ascii=False)


def _is_table(v: Any) -> bool:
    return isinstance(v, dict) and len(v) > 0


def _is_aot(v: Any) -> bool:
    return isinstance(v, list) and len(v) > 0 and all(isinstance(i, dict) and len(i) > 0 for i in v)


def inline(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("TOML cannot hold inf or nan")
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(inline(i) for i in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{_key(k)} = {inline(x)}" for k, x in v.items() if x is not None) + "}"
    raise TypeError(f"cannot write {type(v).__name__} as TOML")


def _emit(path: list[str], d: dict, out: list[str]) -> None:
    for k, v in d.items():
        if v is None or _is_table(v) or _is_aot(v):
            continue
        out.append(f"{_key(k)} = {inline(v)}")
    for k, v in d.items():
        if v is None:
            continue
        name = ".".join(_key(p) for p in path + [k])
        if _is_table(v):
            out.append("")
            out.append(f"[{name}]")
            _emit(path + [k], v, out)
        elif _is_aot(v):
            for item in v:
                out.append("")
                out.append(f"[[{name}]]")
                _emit(path + [k], item, out)


def dumps(d: dict) -> str:
    out: list[str] = []
    _emit([], d, out)
    return "\n".join(out).lstrip("\n") + "\n"
