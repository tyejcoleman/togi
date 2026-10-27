"""Output: envelopes, JSON normalisation, --fields, --limit/--offset, the 8 KB cap, readable text.

Agents get compact one-line JSON when stdout is not a TTY; people get readable text.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import json
import math
import uuid
from decimal import Decimal
from pathlib import PurePath
from typing import Any, Callable

from .errors import EXIT_CODES
from .pathnorm import escape_text

DEFAULT_LIMIT = 50
MAX_BYTES = 8192


def json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, (_dt.datetime, _dt.date, _dt.time)):
        return o.isoformat()
    if isinstance(o, (PurePath, uuid.UUID)):
        return str(o)
    if isinstance(o, enum.Enum):
        return o.value
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return dataclasses.asdict(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    if isinstance(o, (bytes, bytearray)):
        raise TypeError("bytes are not JSON-serialisable; return text or a list instead")
    raise TypeError(f"{type(o).__name__} is not JSON-serialisable")


def dumps(obj: Any) -> str:
    """Compact single-line JSON."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=json_default)


def to_jsonable(value: Any) -> Any:
    """Normalise a command's return value to plain JSON values (Decimal/datetime become strings)."""
    return _sanitize(json.loads(json.dumps(value, default=json_default)))


def _sanitize(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    if isinstance(v, list):
        return [_sanitize(i) for i in v]
    if isinstance(v, dict):
        return {k: _sanitize(i) for k, i in v.items()}
    return v


def est_tokens(nbytes: int) -> int:
    return (nbytes + 3) // 4


# --------------------------------------------------------------------------- envelopes


def ok_envelope(data: Any) -> dict:
    return {"ok": True, "data": data}


def error_envelope(status: str, code: str, message: str, next: str | None = None, **extra: Any) -> dict:
    env: dict[str, Any] = {"ok": False, "status": status, "error": {"code": code, "message": message}}
    if next:
        env["next"] = next
    env.update({k: v for k, v in extra.items() if v is not None})
    return env


def exit_code_for(envelope: dict) -> int:
    if envelope.get("ok"):
        return EXIT_CODES["ok"]
    return EXIT_CODES.get(envelope.get("status", "error"), 1)


# --------------------------------------------------------------------------- --fields


def parse_fields(spec: str | None) -> list[str] | None:
    if not spec:
        return None
    fields = [f.strip() for f in spec.split(",") if f.strip()]
    return fields or None


def project(data: Any, fields: list[str]) -> Any:
    """Keep only the dotted paths in `fields`. Paths map over lists transparently."""
    tree: dict = {}
    for f in fields:
        node = tree
        parts = f.split(".")
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                node[part] = None  # leaf: keep whole value
            else:
                nxt = node.get(part, {})
                if nxt is None:  # a shorter path already keeps the whole subtree
                    break
                node[part] = nxt
                node = nxt
    return _project(data, tree)


def _project(data: Any, tree: dict) -> Any:
    if isinstance(data, list):
        return [_project(i, tree) for i in data]
    if isinstance(data, dict):
        out = {}
        for key, sub in tree.items():
            if key in data:
                out[key] = data[key] if sub is None else _project(data[key], sub)
        return out
    return data


# --------------------------------------------------------------------------- limit / offset / cap


def paginate(data: Any, limit: int, offset: int) -> tuple[Any, dict]:
    """Cut long lists. Returns (data, info) where info may hold total/next_offset for a `more` hint."""
    info: dict[str, Any] = {}
    if isinstance(data, list):
        total = len(data)
        view = data[offset: offset + limit]
        if offset + limit < total:
            info["next_offset"] = offset + limit
        info["total"] = total
        return view, info
    if isinstance(data, dict):
        out = dict(data)
        for k, v in data.items():
            if isinstance(v, list) and len(v) > limit:
                out[k] = v[offset: offset + limit]
                if offset + limit < len(v):
                    info["next_offset"] = offset + limit
                info.setdefault("totals", {})[k] = len(v)
        return out, info
    return data, info


def _size(env: dict) -> int:
    return len(dumps(env).encode("utf-8"))


def fit(env: dict, max_bytes: int, more_hint: Callable[[int | None], str] | None = None,
        start_offset: int = 0) -> tuple[dict, bool]:
    """Shrink `env["data"]` until the serialised envelope is <= max_bytes. Returns (env, truncated).

    A list loses its tail (the `more` hint says where to continue). Anything else, and a list whose first items are
    themselves too big, is shrunk by `_shrink`: the largest list OR dict is halved (a dict keeps its first keys) until the
    value fits, then long strings are cut, and as a last resort the value becomes a text preview of its JSON. The
    envelope is always `truncated: true` with a `more` hint when anything was cut."""
    if _size(env) <= max_bytes:
        return env, False
    env = dict(env)
    data = env.get("data")
    budget = max_bytes - 160
    truncated_offset = None
    if isinstance(data, list):
        lo, hi = 0, len(data)
        while lo < hi:  # largest prefix that fits
            mid = (lo + hi + 1) // 2
            if _size({**env, "data": data[:mid]}) <= budget:
                lo = mid
            else:
                hi = mid - 1
        keep = max(lo, 1) if data else 0
        env["data"] = data[:keep]
        truncated_offset = start_offset + keep
        if _size(env) > max_bytes:   # even the first item alone is too big (one long string, one wide dict)
            env["data"] = _shrink(env["data"], env, budget)
    else:
        env["data"] = _shrink(data, env, budget)
    env["truncated"] = True
    env["more"] = more_hint(truncated_offset) if more_hint else "re-run with --full for everything"
    return env, True


def _shrink(work: Any, env: dict, budget: int) -> Any:
    """Make `work` small enough to sit in `env["data"]` within `budget` bytes, whatever its shape."""
    def fits(v: Any) -> bool:
        return _size({**env, "data": v}) <= budget

    for _ in range(80):                 # halve the biggest list/dict until it fits
        if fits(work):
            return work
        nxt = _halve_largest_container(work)
        if nxt is None:
            break
        work = nxt
    for limit in (240, 80, 24):         # then cut long strings
        work = _trim_strings(work, limit)
        if fits(work):
            return work
        for _ in range(80):
            nxt = _halve_largest_container(work)
            if nxt is None or fits(work):
                break
            work = nxt
        if fits(work):
            return work
    text = dumps(work)                   # last resort: a text preview of the JSON (still flagged `truncated`)
    room = max(budget - _size({**env, "data": ""}) - 40, 40)
    return text[:room] + f"...[+{max(len(text) - room, 0)} chars]"


def hard_cap(env: dict, max_bytes: int) -> dict:
    """The final guarantee: whatever `env` holds, the result serialises to at most `max_bytes` (unless the bare
    ok/status/error code alone cannot, which a slug-sized code never does). Keeps ok, status, the error code and as much
    of the message and `next` as fits, a short preview of any data, `truncated: true` and a `more` hint."""
    if _size(env) <= max_bytes:
        return env
    err = env.get("error") if isinstance(env.get("error"), dict) else {}
    for limit in (600, 240, 80, 24):
        out: dict[str, Any] = {"ok": bool(env.get("ok"))}
        if "status" in env:
            out["status"] = str(env["status"])[:40]
        if err:
            out["error"] = {"code": str(err.get("code", ""))[:80], "message": _trim_strings(str(err.get("message", "")), limit)}
        if env.get("next"):
            out["next"] = _trim_strings(str(env["next"]), limit)
        if "data" in env and env["data"] is not None:
            text = dumps(env["data"])
            out["data"] = text[: limit * 2] + (f"...[+{len(text) - limit * 2} chars]" if len(text) > limit * 2 else "")
        out["truncated"] = True
        out["more"] = "output was cut to fit the size limit; re-run with --full for everything"
        if _size(out) <= max_bytes:
            return out
    return {"ok": bool(env.get("ok")), "truncated": True, "more": "output cut; re-run with --full"}


def fit_any(env: dict, max_bytes: int, more_hint: Callable[[int | None], str] | None = None) -> tuple[dict, bool]:
    """`fit` for every kind of envelope: success data is shrunk by `fit`; error envelopes, dry-run descriptions
    and anything else have their long strings cut (message, summary, args...) until the whole thing fits.
    Always keeps ok/status/error.code, adds `truncated: true` and a `more` hint, and ends in `hard_cap`: the
    serialised result is never larger than `max_bytes`."""
    if _size(env) <= max_bytes:
        return env, False
    if env.get("ok") or isinstance(env.get("data"), (list, dict)):
        shrunk, _ = fit(env, max_bytes, more_hint)
        if _size(shrunk) <= max_bytes:
            return shrunk, True
        env = shrunk
    out = dict(env)
    for limit in (1000, 400, 160, 60):
        out = {k: (v if k in ("ok", "status") else _trim_strings(v, limit)) for k, v in env.items()}
        if _size(out) <= max_bytes - 200:
            break
    if _size(out) > max_bytes - 200:   # still too big (huge nested structure): keep only the essentials
        out = {k: out[k] for k in ("ok", "status", "error", "next") if k in out}
    out["truncated"] = True
    out.setdefault("more", more_hint(None) if more_hint else "re-run with --full for everything")
    return hard_cap(out, max_bytes), True


def cap_text(text: str, max_bytes: int) -> str:
    """Cut a plain-text document at a line boundary under `max_bytes`, with a visible footer."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    footer = f"\n[togi: output cut at {max_bytes} bytes; re-run with --full for everything]\n"
    room = max_bytes - len(footer.encode("utf-8"))
    head = raw[:room].decode("utf-8", "ignore")
    nl = head.rfind("\n")
    if nl > room // 2:
        head = head[:nl]
    return head.rstrip("\n") + footer


def _halve_largest_container(obj: Any) -> Any:
    """Halve the biggest list or dict inside `obj` (a dict keeps its first keys). None if nothing can be halved."""
    path = _largest_container_path(obj, ())
    if path is None:
        return None
    return _replace_at(obj, path[0], _halve)


def _halve(c: Any) -> Any:
    if isinstance(c, dict):
        return dict(list(c.items())[: max(1, len(c) // 2)])
    return c[: max(1, len(c) // 2)]


def _mass(obj: Any) -> int:
    """How much halving this container would save: a list counts its whole size, a dict only its OWN keys and values (the
    containers inside it are weighed on their own), so a wide dict is cut before the small lists and dicts beside it."""
    total = len(dumps(obj))
    if isinstance(obj, dict):
        total -= sum(len(dumps(v)) for v in obj.values() if isinstance(v, (list, dict)))
    return total


def _largest_container_path(obj: Any, path: tuple) -> tuple[tuple, int] | None:
    best = None
    if isinstance(obj, (list, dict)) and len(obj) > 1:
        best = (path, _mass(obj))
    if isinstance(obj, list):
        for i, item in enumerate(obj[:3]):
            sub = _largest_container_path(item, path + (i,))
            if sub and (best is None or sub[1] > best[1]):
                best = sub
    elif isinstance(obj, dict):
        for k, v in list(obj.items())[:200]:
            if isinstance(v, (list, dict)):
                sub = _largest_container_path(v, path + (k,))
                if sub and (best is None or sub[1] > best[1]):
                    best = sub
    return best


def _replace_at(obj: Any, path: tuple, fn: Callable) -> Any:
    if not path:
        return fn(obj)
    head, rest = path[0], path[1:]
    if isinstance(obj, list):
        out = list(obj)
        out[head] = _replace_at(obj[head], rest, fn)
        return out
    out = dict(obj)
    out[head] = _replace_at(obj[head], rest, fn)
    return out


def _trim_strings(obj: Any, limit: int) -> Any:
    if isinstance(obj, str):
        return obj if len(obj) <= limit else obj[:limit] + f"…[+{len(obj) - limit} chars]"
    if isinstance(obj, list):
        return [_trim_strings(i, limit) for i in obj]
    if isinstance(obj, dict):
        return {k: _trim_strings(v, limit) for k, v in obj.items()}
    return obj


# --------------------------------------------------------------------------- readable text


def render_text(env: dict) -> str:
    if env.get("ok"):
        body = render_value(env.get("data"))
        tail = []
        if env.get("more"):
            tail.append(f"more: {env['more']}")
        return escape_text(body + ("\n" + "\n".join(tail) if tail else ""), keep="\n\t")
    lines = []
    err = env.get("error") or {}
    status = env.get("status", "error")
    lines.append(f"{status}: {err.get('message', err.get('code', 'failed'))}  [{err.get('code', status)}]")
    appr = env.get("approval")
    if appr:
        lines.append(f"approval: {appr.get('id')}  {appr.get('summary', '')}".rstrip())
    if env.get("next"):
        lines.append(f"next: {env['next']}")
    return escape_text("\n".join(lines), keep="\n\t")


def render_value(v: Any, indent: int = 0) -> str:
    pad = " " * indent
    if isinstance(v, dict):
        if not v:
            return pad + "{}"
        lines = []
        for k, val in v.items():
            if isinstance(val, (dict, list)) and val:
                lines.append(f"{pad}{k}:")
                lines.append(render_value(val, indent + 2))
            else:
                lines.append(f"{pad}{k}: {_scalar(val)}")
        return "\n".join(lines)
    if isinstance(v, list):
        if not v:
            return pad + "(none)"
        lines = []
        for item in v:
            if isinstance(item, dict) and all(not isinstance(x, (dict, list)) for x in item.values()):
                lines.append(pad + "- " + "  ".join(f"{k}={_scalar(x)}" for k, x in item.items()))
            elif isinstance(item, (dict, list)):
                lines.append(pad + "-")
                lines.append(render_value(item, indent + 2))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return "\n".join(lines)
    return pad + _scalar(v)


def _scalar(v: Any) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, (dict, list)):
        return dumps(v)
    return str(v)
