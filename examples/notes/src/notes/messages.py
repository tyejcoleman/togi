"""Error wording lives in togi_messages.toml (polish tier). This loader is locked."""
from __future__ import annotations

import tomllib
from pathlib import Path

_DEFAULTS = {
    "note_not_found": {"message": "No note with id {id}.", "next": "togi notes find --query <words>"},
    "empty_text": {"message": "A note needs some text.", "next": None},
    "bad_amount": {"message": "A tip must be more than zero.", "next": None},
}
_FILE = Path(__file__).resolve().parents[2] / "togi_messages.toml"


def message(key: str, **fmt) -> tuple[str, str | None]:
    table = dict(_DEFAULTS)
    try:
        table.update(tomllib.loads(_FILE.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError):
        pass
    entry = table.get(key) or _DEFAULTS.get(key) or {"message": key, "next": None}
    nxt = entry.get("next")
    return entry["message"].format(**fmt), (nxt.format(**fmt) if nxt else None)
