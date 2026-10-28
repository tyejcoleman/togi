"""Item generators (EVAL-SPEC section 9): pluggable, seeded, correct by construction.

A generator writes a whole suite directory (suite.toml, items/, data/) from a seed. The answer to every item is computed by
the real tool's own engine over the seeded data (SQL for `sheet`), never by a model, and the same seed always writes the
same bytes. Register new ones in GENERATORS: `generate(out_dir: Path, seed: int, **options) -> dict` (a report).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from ...errors import TogiError


def _sheet(out: Path, seed: int, **opts) -> dict:
    from . import sheet

    return sheet.generate(out, seed, **opts)


GENERATORS: dict[str, Callable[..., dict]] = {"sheet": _sheet}


def generate(name: str, out: Path, seed: int, **options) -> dict:
    if name not in GENERATORS:
        raise TogiError("unknown_generator", f"no generator named {name!r}; available: {', '.join(sorted(GENERATORS))}",
                        next="togi eval generate sheet --seed 7 --out suites/sheet")
    return GENERATORS[name](out, seed, **options)
