"""Canonical JSON and hashes: what makes an item hash, a subject hash and a lock stable across runs and machines."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical(obj: Any) -> str:
    """One text per value: sorted keys, no spaces, ASCII only, floats via repr (so 1.0 and 1 differ, as they should)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_obj(obj: Any) -> str:
    return sha256_text(canonical(obj))


def file_sha256(path: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def short(h: str | None, n: int = 12) -> str:
    return (h or "")[:n]
