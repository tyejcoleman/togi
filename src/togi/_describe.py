"""Child process that reads what a bundle's commands are: the only place `togi lock` and `togi approve` ever import a bundle.

Run as `python -I -B _describe.py` by describe.run_child, under the `togi run` strict sandbox profile, in a session of its
own, with a scrubbed environment and a temporary working directory. A JSON request arrives on stdin:

    {"op": "snapshot", "repo": ..., "bundle": ...}             -> {"body": {commands, gated_roots, loaded_files}}
    {"op": "command", "repo": ..., "bundle": ..., "command": ...}
                                                                -> {"command": {name, effect, summary, ...}, "gated_roots", "loaded_files"}

and ONE line goes back on the real stdout: `TOGI-DESCRIBE <json>` (or {"error": {...}}). Anything the bundle prints while it
loads goes to stderr instead, so it can never be taken for the answer. The bundle's import-time code runs here, inside the
sandbox, and nowhere near the terminal where a person types a passphrase.
"""
import json
import os
import sys


def main() -> None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))   # the directory containing `togi`
    req = json.loads(sys.stdin.read())
    out_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        from pathlib import Path

        from togi import describe, registry
        from togi.errors import TogiError

        try:
            loaded = registry.load_repo(Path(req["repo"]), expect_name=req["bundle"])
            if req.get("op") == "snapshot":
                result = {"body": describe.build_body(loaded)}
            else:
                cmd = loaded.bundle.get(req["command"])
                if cmd is None:
                    result = {"error": {"code": "unknown_command", "message": f"{req['bundle']} has no command "
                                                                             f"{req['command']!r}"}}
                else:
                    body = describe.build_body(loaded)
                    result = {"command": describe.command_record(cmd), "gated_roots": body["gated_roots"],
                              "loaded_files": body["loaded_files"]}
        except TogiError as e:
            result = {"error": {"code": e.code, "message": e.message}}
    except BaseException as e:   # an import-time failure in the bundle: report it, never a traceback to the person
        result = {"error": {"code": "describe_failed", "message": f"{type(e).__name__}: {e}"[:300]}}
    os.write(out_fd, b"TOGI-DESCRIBE " + json.dumps(result).encode("utf-8") + b"\n")


main()
