"""JSON-file storage for notes and tips (locked: hone may not edit this)."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")


class Store:
    def __init__(self, state_dir: Path):
        self.notes_path = Path(state_dir) / "notes.json"
        self.tips_path = Path(state_dir) / "tips.json"

    def notes(self) -> list[dict]:
        return _load(self.notes_path, [])

    def add(self, text: str, tags: list[str]) -> dict:
        notes = self.notes()
        note = {"id": f"n_{len(notes) + 1}", "text": text, "tags": tags}
        notes.append(note)
        _save(self.notes_path, notes)
        return note

    def get(self, note_id: str) -> dict | None:
        return next((n for n in self.notes() if n["id"] == note_id), None)

    def delete(self, note_id: str) -> bool:
        notes = self.notes()
        kept = [n for n in notes if n["id"] != note_id]
        _save(self.notes_path, kept)
        return len(kept) != len(notes)

    def tip(self, note_id: str, amount: Decimal) -> dict:
        tips = _load(self.tips_path, [])
        rec = {"note": note_id, "amount": str(amount)}
        tips.append(rec)
        _save(self.tips_path, tips)
        return rec

    def tips(self) -> list[dict]:
        return _load(self.tips_path, [])

    def reset(self) -> None:
        for p in (self.notes_path, self.tips_path):
            if p.exists():
                p.unlink()
