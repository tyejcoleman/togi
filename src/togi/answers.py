"""`togi answer`: the agent's structured final answer (EVAL-SPEC section 3).

    togi answer --json '{"region":"north","revenue":47248}'          (CLI)
    togi_answer({"region": "north", "revenue": 47248})               (code mode, inside `togi run`)
    answer {"region": "north", "revenue": 47248}                     (MCP tool `answer`)

The answer is checked against the schema in `$TOGI_HOME/answer-schema.json` when there is one (the eval harness writes
the item's schema there before the agent starts), and appended to `$TOGI_HOME/state/_host/answers.jsonl`. Checks read
that record, never free text, so "47,248" versus 47248 can never be a false fail.

The journal never holds values (SPEC section 4), so it gets only an `answer` event: ok/not ok, the field names and
their types, and a hash. The values live in the answers log, which is the agent's *output*, not a record of what it was
asked to handle. An answer that fails the schema is still recorded (as `valid: false`) so the eval can count format
failures, and the agent is told what to fix.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import journal, output, paths, schema
from .cli import Reply, _mode, _usage
from .errors import TogiError

SCHEMA_FILE = "answer-schema.json"
MAX_ANSWER_BYTES = 16384
KINDS = {bool: "bool", int: "int", float: "float", str: "str", list: "list", dict: "dict"}


def schema_path() -> Path:
    return paths.togi_home() / SCHEMA_FILE


def answers_path() -> Path:
    return paths.bundle_state("_host") / "answers.jsonl"


def load_schema() -> dict | None:
    try:
        doc = json.loads(schema_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def shape(obj: Any) -> Any:
    """Field names and value types of an answer, never the values: what the journal may hold."""
    if isinstance(obj, dict):
        return {str(k): KINDS.get(type(v), type(v).__name__) for k, v in list(obj.items())[:40]}
    return KINDS.get(type(obj), type(obj).__name__)


def read_all(offset: int = 0) -> list[dict]:
    """Every recorded answer from byte `offset` on (offset 0 = all), oldest first."""
    out: list[dict] = []
    try:
        with open(answers_path(), "rb") as f:
            f.seek(offset)
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


def size() -> int:
    try:
        return answers_path().stat().st_size
    except OSError:
        return 0


def record(obj: Any, *, surface: str = "cli", session: str | None = None) -> dict:
    """Validate and record an answer. Returns {"valid", "seq", "errors"}; never raises for an invalid answer."""
    session = session or journal.session_id()
    errors: list[str] = []
    try:
        blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        blob, obj = "null", None
        errors.append(f"the answer is not JSON-serialisable ({e})")
    if len(blob.encode("utf-8")) > MAX_ANSWER_BYTES:
        errors.append(f"the answer is {len(blob.encode('utf-8'))} bytes; the limit is {MAX_ANSWER_BYTES}")
        obj = None
    if not errors:
        sch = load_schema()
        if sch is not None:
            errors = schema.validate(obj, sch)
        elif not isinstance(obj, dict):
            errors = ["$: the answer must be a JSON object"]
    prior = size()
    seq = len(read_all()) + 1 if prior else 1
    entry = {"seq": seq, "ts": journal.now_ts(), "session": session, "agent": paths.agent_name(), "surface": surface,
             "valid": not errors, "errors": errors[:5], "answer": obj,
             "sha": hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]}
    try:
        d = answers_path()
        d.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(d, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, (json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as e:
        raise TogiError("answer_not_recorded", f"could not write the answers log ({e.strerror or e})", next="togi doctor")
    extra: dict[str, Any] = {"answer_sha": entry["sha"], "answer_seq": seq}
    if _recording():
        extra["args_full"] = blob[:400]
    journal.append(None, journal.make_entry(
        surface=surface, bundle=None, command="answer", event="answer", args_shape={"answer": shape(obj)} if obj is not None else {},
        ok=not errors, status="ok" if not errors else "error", error_code="answer_invalid" if errors else None,
        session=session, effect="write", **extra))
    return {"valid": not errors, "seq": seq, "errors": errors}


def _recording() -> bool:
    from . import host

    return host.eval_recording()


def envelope(res: dict) -> dict:
    if res["valid"]:
        return output.ok_envelope({"recorded": True, "seq": res["seq"]})
    return output.error_envelope(
        "error", "answer_invalid", "the answer was not accepted: " + "; ".join(res["errors"][:4]),
        next="fix the fields and run `togi answer --json '<object>'` again")


# --------------------------------------------------------------------------- the CLI command


def parse_args(argv: list[str]) -> tuple[dict, list[str]]:
    """`togi answer` takes `--json VALUE` (the usual form), `--file PATH`, `--stdin` or one positional JSON value. A trailing
    bare `--json` (no value after it) or `--text` is the host's output-format flag, as everywhere else."""
    opts: dict[str, Any] = {"payload": None, "file": None, "stdin": False, "mode": None, "help": False}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-h", "--help"):
            opts["help"] = True
        elif tok == "--json":
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt is not None and not nxt.startswith("--") and opts["payload"] is None:
                opts["payload"] = nxt
                i += 1
            else:
                opts["mode"] = "json"
        elif tok.startswith("--json="):
            opts["payload"] = tok[len("--json="):]
        elif tok == "--text":
            opts["mode"] = "text"
        elif tok == "--file" and i + 1 < len(argv):
            opts["file"] = argv[i + 1]
            i += 1
        elif tok == "--stdin" or tok == "-":
            opts["stdin"] = True
        elif tok.startswith("--") and tok[2:] in ("full", "limit", "offset", "fields"):
            if tok in ("--limit", "--offset", "--fields"):
                i += 1
        elif tok.startswith("--"):
            raise TogiError("unknown_flag", f"unknown flag {tok.split('=')[0]} for `togi answer`; it takes --json '<object>'",
                            next="togi answer --help")
        else:
            rest.append(tok)
        i += 1
    return opts, rest


def h_answer(argv: list[str]) -> Reply:
    opts, rest = parse_args(argv)
    mode = opts["mode"]
    if opts["help"]:
        return _usage("answer")
    if opts["payload"] is not None:
        text = opts["payload"]
    elif opts["file"]:
        try:
            text = Path(opts["file"]).expanduser().read_text(encoding="utf-8")
        except OSError as e:
            raise TogiError("path_not_found", f"cannot read {opts['file']} ({e.strerror or e})", next="togi answer --help")
    elif opts["stdin"]:
        text = sys.stdin.read()
    elif rest:
        text = " ".join(rest)
    else:
        raise TogiError("bad_arg", "togi answer needs the answer: --json '<object>'", next="togi answer --help")
    try:
        obj = json.loads(text)
    except ValueError as e:
        raise TogiError("answer_not_json", f"the answer is not valid JSON ({str(e)[:80]})",
                        next="quote it as one JSON object: togi answer --json '{\"field\": \"value\"}'")
    res = record(obj, surface="cli")
    env = envelope(res)
    return Reply(env=env, mode=mode, code=None if res["valid"] else 1)
