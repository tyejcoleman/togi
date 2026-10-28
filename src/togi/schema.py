"""A small JSON Schema validator (stdlib only) for `togi answer` and the eval layer.

It covers what answer schemas and tool-argument schemas use: type (one or a list), enum, const, properties, required,
additionalProperties (bool or schema), items, minItems / maxItems / uniqueItems, minimum / maximum (and the exclusive
forms), minLength / maxLength, pattern, oneOf / anyOf / allOf / not, and local `$ref` ("#/$defs/x", "#/definitions/x").
Everything else (format, $schema, title, description, default, examples...) is ignored, as the spec allows.

`validate(instance, schema)` returns a list of error strings (empty = valid); each one starts with a JSON-pointer-ish
path (`$.items[2].price`). It never raises on a bad instance; a malformed schema is reported by `check_schema`.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_ERRORS = 20
MAX_DEPTH = 40
_TYPES = ("string", "number", "integer", "boolean", "array", "object", "null")


def _type_ok(inst: Any, t: str) -> bool:
    if t == "string":
        return isinstance(inst, str)
    if t == "boolean":
        return isinstance(inst, bool)
    if t == "integer":
        return (isinstance(inst, int) and not isinstance(inst, bool)) or (
            isinstance(inst, float) and math.isfinite(inst) and inst == int(inst))
    if t == "number":
        return isinstance(inst, (int, float)) and not isinstance(inst, bool) and (
            not isinstance(inst, float) or math.isfinite(inst))
    if t == "array":
        return isinstance(inst, list)
    if t == "object":
        return isinstance(inst, dict)
    if t == "null":
        return inst is None
    return False


def _json_eq(a: Any, b: Any) -> bool:
    """JSON equality: `true` is not `1`, `1` equals `1.0`."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_json_eq(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_json_eq(a[k], b[k]) for k in a)
    return a == b


def _resolve(ref: str, root: dict) -> Any:
    if not ref.startswith("#"):
        raise KeyError(f"only local $ref is supported: {ref}")
    node: Any = root
    for part in [p for p in ref[1:].split("/") if p]:
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def validate(instance: Any, schema: Any, root: dict | None = None) -> list[str]:
    """Errors for `instance` against `schema` (an empty list means valid)."""
    errors: list[str] = []
    _walk(instance, schema, root if root is not None else (schema if isinstance(schema, dict) else {}), "$", errors, 0)
    return errors[:MAX_ERRORS]


def is_valid(instance: Any, schema: Any) -> bool:
    return not validate(instance, schema)


def _walk(inst: Any, schema: Any, root: dict, path: str, errors: list[str], depth: int) -> None:
    if len(errors) >= MAX_ERRORS or depth > MAX_DEPTH:
        return
    if schema is True or schema is None or schema == {}:
        return
    if schema is False:
        errors.append(f"{path}: no value is allowed here")
        return
    if not isinstance(schema, dict):
        errors.append(f"{path}: schema is not an object")
        return
    if "$ref" in schema:
        try:
            _walk(inst, _resolve(str(schema["$ref"]), root), root, path, errors, depth + 1)
        except (KeyError, TypeError) as e:
            errors.append(f"{path}: cannot resolve $ref {schema['$ref']!r} ({e})")
        return
    t = schema.get("type")
    if t is not None:
        types = [t] if isinstance(t, str) else list(t)
        if not any(_type_ok(inst, x) for x in types):
            errors.append(f"{path}: expected {' or '.join(types)}, got {_kind(inst)}")
            return
    if "const" in schema and not _json_eq(inst, schema["const"]):
        errors.append(f"{path}: must equal {json.dumps(schema['const'])[:60]}")
    if "enum" in schema and not any(_json_eq(inst, e) for e in schema["enum"]):
        errors.append(f"{path}: must be one of {json.dumps(schema['enum'])[:100]}")
    if isinstance(inst, str):
        if "minLength" in schema and len(inst) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(inst) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema:
            try:
                if not re.search(schema["pattern"], inst):
                    errors.append(f"{path}: does not match pattern {schema['pattern']!r}")
            except re.error:
                errors.append(f"{path}: the schema's pattern is not a valid regular expression")
    if isinstance(inst, (int, float)) and not isinstance(inst, bool):
        if "minimum" in schema and inst < schema["minimum"]:
            errors.append(f"{path}: below the minimum {schema['minimum']}")
        if "maximum" in schema and inst > schema["maximum"]:
            errors.append(f"{path}: above the maximum {schema['maximum']}")
        if "exclusiveMinimum" in schema and inst <= schema["exclusiveMinimum"]:
            errors.append(f"{path}: must be above {schema['exclusiveMinimum']}")
        if "exclusiveMaximum" in schema and inst >= schema["exclusiveMaximum"]:
            errors.append(f"{path}: must be below {schema['exclusiveMaximum']}")
    if isinstance(inst, list):
        if "minItems" in schema and len(inst) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(inst) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if schema.get("uniqueItems"):
            seen = [json.dumps(i, sort_keys=True) for i in inst]
            if len(set(seen)) != len(seen):
                errors.append(f"{path}: items must be unique")
        sub = schema.get("items")
        if sub is not None:
            for i, item in enumerate(inst):
                _walk(item, sub, root, f"{path}[{i}]", errors, depth + 1)
    if isinstance(inst, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in inst:
                errors.append(f"{path}: missing required field '{name}'")
        for k, v in inst.items():
            if k in props:
                _walk(v, props[k], root, f"{path}.{k}", errors, depth + 1)
            else:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    errors.append(f"{path}: unexpected field '{k}'")
                elif isinstance(extra, dict):
                    _walk(v, extra, root, f"{path}.{k}", errors, depth + 1)
    for key in ("allOf",):
        for i, sub in enumerate(schema.get(key) or []):
            _walk(inst, sub, root, path, errors, depth + 1)
    if "anyOf" in schema:
        if not any(not validate_sub(inst, s, root, depth) for s in schema["anyOf"]):
            errors.append(f"{path}: matches none of the allowed alternatives")
    if "oneOf" in schema:
        hits = sum(1 for s in schema["oneOf"] if not validate_sub(inst, s, root, depth))
        if hits != 1:
            errors.append(f"{path}: must match exactly one alternative (matches {hits})")
    if "not" in schema and not validate_sub(inst, schema["not"], root, depth):
        errors.append(f"{path}: must not match the excluded shape")


def validate_sub(inst: Any, schema: Any, root: dict, depth: int) -> list[str]:
    errs: list[str] = []
    _walk(inst, schema, root, "$", errs, depth + 1)
    return errs


def _kind(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


def check_schema(schema: Any, *, require_object: bool = True) -> list[str]:
    """Problems with a schema itself: not an object, unknown `type`, a bad `required`, an answer schema that is not an
    object schema. Returns a list of strings (empty = usable)."""
    problems: list[str] = []
    if not isinstance(schema, dict):
        return ["the schema must be a JSON object"]
    if require_object and schema.get("type") != "object":
        problems.append('an answer schema must describe an object: set type = "object"')
    _check_node(schema, "$", problems, 0)
    return problems


def _check_node(node: Any, path: str, problems: list[str], depth: int) -> None:
    if not isinstance(node, dict) or depth > MAX_DEPTH:
        return
    t = node.get("type")
    if t is not None:
        for x in ([t] if isinstance(t, str) else (t if isinstance(t, list) else [None])):
            if x not in _TYPES:
                problems.append(f"{path}: unknown type {x!r}")
    if "required" in node and not (isinstance(node["required"], list) and all(isinstance(r, str) for r in node["required"])):
        problems.append(f"{path}: `required` must be a list of field names")
    if "pattern" in node:
        try:
            re.compile(node["pattern"])
        except (re.error, TypeError):
            problems.append(f"{path}: `pattern` is not a valid regular expression")
    for k, v in (node.get("properties") or {}).items():
        _check_node(v, f"{path}.{k}", problems, depth + 1)
    if isinstance(node.get("items"), dict):
        _check_node(node["items"], f"{path}[]", problems, depth + 1)
    for key in ("oneOf", "anyOf", "allOf"):
        for i, sub in enumerate(node.get(key) or []):
            _check_node(sub, f"{path}.{key}[{i}]", problems, depth + 1)
