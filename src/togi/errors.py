"""Errors that cross the command boundary (SPEC section 2)."""
from __future__ import annotations

STATUSES = ("error", "refused", "needs_approval")
EXIT_CODES = {"ok": 0, "error": 1, "refused": 2, "needs_approval": 3, "internal": 4}


class TogiError(Exception):
    """Raise from a command to return a structured, agent-readable failure.

    code    stable slug, e.g. "drop_not_found"
    message what happened, in one sentence
    next    the exact command the agent should run next, if any
    status  "error" (exit 1), "refused" (exit 2, a guard said no) or "needs_approval" (exit 3)
    """

    def __init__(self, code: str, message: str = "", next: str | None = None, status: str = "error"):
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.next = next
        self.status = status

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


class NeedsApproval(TogiError):
    """Raised inside `togi run` when a money call needs a human approval."""

    def __init__(self, approval_id: str, message: str | None = None, *, approval: dict | None = None,
                 next: str | None = None):
        super().__init__(
            "needs_approval",
            message or f"approval {approval_id} is required before this can run",
            next=next or f"a person runs: togi approve {approval_id}",
            status="needs_approval",
        )
        self.approval_id = approval_id
        self.approval = approval or {"id": approval_id}


class ArgError(Exception):
    """Internal: a CLI/JSON argument could not be understood."""

    def __init__(self, message: str, code: str = "bad_arg", flag: str | None = None, next: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.flag = flag
        self.next = next
