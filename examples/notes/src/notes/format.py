"""How notes are summarised in lists (reshape tier: a behaviour change needs a person)."""
from __future__ import annotations

SNIPPET = 60


def summarize(note: dict) -> dict:
    text = note["text"]
    return {"id": note["id"], "text": text if len(text) <= SNIPPET else text[: SNIPPET - 1] + "…",
            "tags": note.get("tags", [])}
