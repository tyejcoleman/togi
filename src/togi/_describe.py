"""Child process for `togi approve`: what does this bundle's command say about itself?

Run as `python -I _describe.py` with a JSON request on stdin: {repo, bundle, command, args, id}. It imports the bundle
(registry.load_repo adds the repo explicitly), looks up the command and prints ONE line,
`TOGI-DESCRIBE {"summary": ..., "confirm_word": ...}` (or {"error": {...}}), on the real stdout. Anything the bundle
prints while it loads goes to stderr instead. It exists so that the bundle's import-time code never runs inside the
person's approval session; see approvals.describe_in_child.
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

        from togi import approvals, registry
        from togi.errors import TogiError

        try:
            loaded = registry.load_repo(Path(req["repo"]), expect_name=req["bundle"])
            cmd = loaded.bundle.get(req["command"])
            if cmd is None:
                result = {"error": {"code": "unknown_command", "message": f"{req['bundle']} has no command "
                                                                         f"{req['command']!r}"}}
            else:
                args = req["args"]
                result = {"summary": approvals.summarize(req["bundle"], cmd, args),
                          "confirm_word": approvals.confirm_word(args, cmd, req["id"]), "effect": cmd.effect}
        except TogiError as e:
            result = {"error": {"code": e.code, "message": e.message}}
    except BaseException as e:   # an import-time failure in the bundle: report it, never a traceback to the person
        result = {"error": {"code": "describe_failed", "message": f"{type(e).__name__}: {e}"[:300]}}
    os.write(out_fd, b"TOGI-DESCRIBE " + json.dumps(result).encode("utf-8") + b"\n")


main()
