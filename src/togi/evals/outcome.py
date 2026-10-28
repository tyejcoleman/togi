"""An outcome (`outcomes/<name>.toml`): the decision being made, and the bar a subject has to clear.

    name, description
    decision          one sentence: what the result decides
    primary_metric    success rate on the outcome's splits (default test + unseen; dev never counts)
    [gates]           per SLICE: min_success (point estimate) and min_success_lcb (Wilson lower bound);
                      max_forbidden (default 0); optional max_median_turns / max_median_cost_usd;
                      optional [gates.non_inferior] to = "<subject id>", margin = 0.05
    [slices]          capability tags, tiers, worlds to gate (default: every one that appears), min_cells
    [[reference]]     subject = "<id>", bar = "target" | "fallback"  (the models the bar is set against)

A verdict applies the gates on the WORST slice (analysis.verdict), so a subject that is strong on average but weak on one
capability does not pass.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import TogiError
from .canon import hash_obj

SPLITS = ("dev", "test", "unseen")
PRIMARY_METRICS = ("success_rate",)
BARS = ("target", "fallback")

TEMPLATE = '''# Outcome: the decision being made, and the bar.
name = "{name}"
description = "One or two sentences on the job this outcome covers."
decision = "What does the result decide? (e.g. can a small model replace Sonnet 5.5 for this job?)"
primary_metric = "success_rate"
splits = ["test", "unseen"]          # the splits the verdict counts; dev is for developing and is never gated

[gates]                                # applied to EVERY slice; the verdict is the worst slice
min_success = 0.85                     # point estimate of the success rate
min_success_lcb = 0.70                 # Wilson 95% lower bound of the success rate
max_forbidden = 0                      # forbidden attempts allowed across the run
# max_median_turns = 12
# max_median_cost_usd = 0.05
# [gates.non_inferior]                 # optional: also not worse than a reference by more than `margin` (paired bootstrap)
# to = "sonnet"
# margin = 0.05

[slices]                               # optional: restrict what is gated (default: every tag, tier and world that appears)
# capabilities = ["numeric", "multi-step"]
# tiers = [1, 2, 3]
# worlds = ["retail-a"]
min_cells = 1                          # a slice with fewer scored cells than this is reported but not gated

[[reference]]                          # the models the bar is set against
subject = "sonnet"
bar = "target"
[[reference]]
subject = "haiku"
bar = "fallback"
'''


@dataclass
class Gates:
    min_success: float = 0.0
    min_success_lcb: float = 0.0
    max_forbidden: int = 0
    max_median_turns: float | None = None
    max_median_cost_usd: float | None = None
    non_inferior_to: str | None = None
    margin: float = 0.05

    def to_dict(self) -> dict:
        return {"min_success": self.min_success, "min_success_lcb": self.min_success_lcb,
                "max_forbidden": self.max_forbidden, "max_median_turns": self.max_median_turns,
                "max_median_cost_usd": self.max_median_cost_usd, "non_inferior_to": self.non_inferior_to,
                "margin": self.margin}


@dataclass
class Outcome:
    name: str
    description: str = ""
    decision: str = ""
    primary_metric: str = "success_rate"
    splits: list[str] = field(default_factory=lambda: ["test", "unseen"])
    gates: Gates = field(default_factory=Gates)
    slice_capabilities: list[str] = field(default_factory=list)
    slice_tiers: list[int] = field(default_factory=list)
    slice_worlds: list[str] = field(default_factory=list)
    min_cells: int = 1
    references: list[dict] = field(default_factory=list)      # [{"subject", "bar"}]
    path: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description, "decision": self.decision,
                "primary_metric": self.primary_metric, "splits": list(self.splits), "gates": self.gates.to_dict(),
                "slices": {"capabilities": self.slice_capabilities, "tiers": self.slice_tiers,
                           "worlds": self.slice_worlds, "min_cells": self.min_cells},
                "references": [dict(r) for r in self.references]}

    @property
    def hash(self) -> str:
        return hash_obj(self.to_dict())

    def target_ids(self) -> list[str]:
        return [r["subject"] for r in self.references if r.get("bar") == "target"]


def _float(v: Any, where: str, lo: float = 0.0, hi: float | None = 1.0) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TogiError("outcome_invalid", f"{where} must be a number", next="fix the outcome file")
    if v < lo or (hi is not None and v > hi):
        raise TogiError("outcome_invalid", f"{where} must be between {lo:g} and {hi:g}" if hi is not None
                        else f"{where} must be at least {lo:g}", next="fix the outcome file")
    return float(v)


def parse_outcome(raw: dict, path: str = "") -> Outcome:
    name = raw.get("name")
    if not isinstance(name, str) or not name.replace("-", "").replace("_", "").isalnum():
        raise TogiError("outcome_invalid", "an outcome needs a simple `name` (letters, digits, - and _)",
                        next="fix the outcome file")
    metric = raw.get("primary_metric", "success_rate")
    if metric not in PRIMARY_METRICS:
        raise TogiError("outcome_invalid", f"primary_metric must be one of {', '.join(PRIMARY_METRICS)}", next="fix the outcome file")
    splits = raw.get("splits", ["test", "unseen"])
    if not isinstance(splits, list) or not splits or any(s not in SPLITS for s in splits):
        raise TogiError("outcome_invalid", f"splits must be a non-empty list drawn from {', '.join(SPLITS)}", next="fix the outcome file")
    g = raw.get("gates", {})
    if not isinstance(g, dict):
        raise TogiError("outcome_invalid", "[gates] must be a table", next="fix the outcome file")
    gates = Gates(
        min_success=_float(g.get("min_success", 0.0), "gates.min_success"),
        min_success_lcb=_float(g.get("min_success_lcb", 0.0), "gates.min_success_lcb"),
        max_forbidden=int(_float(g.get("max_forbidden", 0), "gates.max_forbidden", 0, None)),
        max_median_turns=_float(g["max_median_turns"], "gates.max_median_turns", 0, None) if "max_median_turns" in g else None,
        max_median_cost_usd=_float(g["max_median_cost_usd"], "gates.max_median_cost_usd", 0, None)
        if "max_median_cost_usd" in g else None)
    ni = g.get("non_inferior")
    if ni is not None:
        if not isinstance(ni, dict) or not isinstance(ni.get("to"), str):
            raise TogiError("outcome_invalid", "[gates.non_inferior] needs `to = \"<subject id>\"`", next="fix the outcome file")
        gates.non_inferior_to = ni["to"]
        gates.margin = _float(ni.get("margin", 0.05), "gates.non_inferior.margin")
    sl = raw.get("slices", {})
    refs = []
    for r in raw.get("reference", []):
        if not isinstance(r, dict) or not isinstance(r.get("subject"), str):
            raise TogiError("outcome_invalid", "each [[reference]] needs `subject = \"<id>\"`", next="fix the outcome file")
        bar = r.get("bar", "target")
        if bar not in BARS:
            raise TogiError("outcome_invalid", f"reference bar must be one of {', '.join(BARS)}", next="fix the outcome file")
        refs.append({"subject": r["subject"], "bar": bar})
    return Outcome(name=name, description=str(raw.get("description", "")), decision=str(raw.get("decision", "")),
                   primary_metric=metric, splits=list(splits), gates=gates,
                   slice_capabilities=[str(x) for x in sl.get("capabilities", [])],
                   slice_tiers=[int(x) for x in sl.get("tiers", [])], slice_worlds=[str(x) for x in sl.get("worlds", [])],
                   min_cells=int(sl.get("min_cells", 1)), references=refs, path=path)


def load_outcome(spec: str | Path) -> Outcome:
    """Load by path, or by name from `./outcomes/<name>.toml`."""
    p = Path(str(spec)).expanduser()
    if not p.is_file():
        alt = Path.cwd() / "outcomes" / (str(spec) if str(spec).endswith(".toml") else f"{spec}.toml")
        if alt.is_file():
            p = alt
        else:
            raise TogiError("outcome_not_found", f"no outcome file at {spec} (or outcomes/{spec}.toml)",
                            next="togi eval outcome new <name>")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise TogiError("outcome_invalid", f"{p.name}: invalid TOML ({e})", next="fix the outcome file")
    return parse_outcome(raw, str(p))


def new_outcome(name: str, directory: Path | None = None) -> Path:
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise TogiError("bad_arg", "an outcome name is letters, digits, - and _", next="togi eval outcome new <name>")
    d = (directory or Path.cwd()) / "outcomes"
    target = d / f"{name}.toml"
    if target.exists():
        raise TogiError("outcome_exists", f"{target} already exists", next=f"edit {target}")
    d.mkdir(parents=True, exist_ok=True)
    target.write_text(TEMPLATE.format(name=name), encoding="utf-8")
    return target
