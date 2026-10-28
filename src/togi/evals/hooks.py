"""Harness-only hooks, run in a subprocess INSIDE an eval sandbox (never reachable from the `togi` command line):
a test-only way to simulate a person deciding on an approval between sessions of a long-running item.

    python -I -c ... hooks approve|deny|expire <selector>

selector: `latest` (the newest pending approval), an approval id, or command words (`tip send`, `notes tip send`).
It signs with the sandbox's throwaway approver (test scope), which only a sandbox accepts (approvals.sandbox_optin).
"""
from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    from .. import approvals, approver, paths

    if len(argv) < 2 or argv[0] not in ("approve", "deny", "expire"):
        print(json.dumps({"ok": False, "error": "usage: approve|deny|expire <selector>"}))
        return 2
    action, selector = argv[0], " ".join(argv[1:]).strip()
    if not approvals.sandbox_optin():
        print(json.dumps({"ok": False, "error": "not inside an eval sandbox"}))
        return 2
    approver.ensure_key()
    pending = [r for r in approvals.list_all() if r["status"] == "pending"]
    if selector.startswith("ap_"):
        pick = [r for r in pending if r["id"] == selector]
    elif selector in ("", "latest"):
        pick = sorted(pending, key=lambda r: r["created"])[-1:]
    else:
        words = selector.removeprefix("togi ").strip()
        pick = sorted([r for r in pending if f"{r['bundle']} {r['command']}".startswith(words) or r["command"].startswith(words)],
                      key=lambda r: r["created"])[-1:]
    if not pick:
        print(json.dumps({"ok": False, "error": f"no pending approval matches {selector!r}"}))
        return 1
    aid = pick[0]["id"]
    if action == "expire":
        rec = approvals.load(aid)
        rec["expires"] = "2000-01-01T00:00:00Z"
        approvals._write(rec)
        print(json.dumps({"ok": True, "approval": aid, "status": "expired"}))
        return 0
    rec = approvals.grant(aid, "approve" if action == "approve" else "deny", "eval-harness")
    print(json.dumps({"ok": True, "approval": aid, "status": rec["status"]}))
    return 0


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main(sys.argv[1:]))
