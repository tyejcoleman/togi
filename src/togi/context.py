"""Context: optional first-class parameter injected into commands that declare `ctx: togi.Context`."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from . import paths


class Context:
    """What a command may know about its caller. Never a CLI flag.

    state_dir      per-bundle writable directory (created on first use)
    journal_note   append a free-form note to the journal (stored verbatim, so keep values out of it)
    agent          $TOGI_AGENT, or None
    surface        "cli" | "run" | "mcp"
    approval_scope "default" | "test" | None. Set only on a gated call that arrived with a person's approval: the
                   scope of the approver that signed it ("default" = the fixed approver, "test" = a throwaway one
                   under $TOGI_APPROVER_HOME). None when there was no approval. A command that moves real value
                   must refuse "test" outside its own test mode.
    """

    def __init__(self, bundle: str = "test", surface: str = "cli", agent: str | None = None,
                 state_dir: str | Path | None = None, session: str | None = None,
                 repo: str | Path | None = None, command: str | None = None,
                 note_sink: Callable[[dict], None] | None = None, approval_scope: str | None = None):
        self.bundle = bundle
        self.approval_scope = approval_scope
        self.surface = surface
        self.agent = agent if agent is not None else paths.agent_name()
        self.session = session
        self.repo = Path(repo) if repo else None
        self.command = command
        self._state_dir = Path(state_dir) if state_dir else None
        self._note_sink = note_sink
        self.notes: list[dict] = []  # notes recorded when no journal sink is attached (tests)

    @property
    def state_dir(self) -> Path:
        if self._state_dir is None:
            self._state_dir = Path(tempfile.mkdtemp(prefix=f"togi-{self.bundle}-"))
        self._state_dir.mkdir(parents=True, exist_ok=True)
        return self._state_dir

    def journal_note(self, note: dict[str, Any]) -> None:
        if not isinstance(note, dict):
            raise TypeError("journal_note expects a dict")
        self.notes.append(note)
        if self._note_sink:
            self._note_sink(note)
