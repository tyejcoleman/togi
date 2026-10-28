"""Analysis: pure functions over a run's cells (EVAL-SPEC section 7). Nothing here runs a model or touches the sandbox.

    success_table        success with a Wilson 95% CI per subject x slice (overall, capability, tier, world)
    paired_comparisons   exact McNemar + a seeded item-clustered paired bootstrap between subjects on the same items
    item_statistics      difficulty, discrimination (correlation with the other items' skill), saturation flags, suspect items
    reference_sanity     a reference subject that fails an item flags the item SUSPECT before anyone blames the subject
    scaling / lift / pareto   success vs model size per harness config, the size-equivalent gain of a harness setting,
                         success vs cost and vs latency
    verdicts             the outcome's gates applied to the WORST slice, with Wilson lower bounds -> PASS / FAIL per subject,
                         and the smallest subject that passes
    long_horizon         end-state success, steps per session, wasted actions, drift, stops at approval, per-session curve
    taxonomy             rule-based failure classes (taxonomy.py)

A scored cell is one (item, repeat) run. Cells with a driver error count as failures (conservative) and are reported as
invalid; the report says how many.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from ..errors import TogiError
from . import stats, taxonomy as tax
from .outcome import Outcome, parse_outcome
from .subject import SANITY_ROLES

Z = stats.Z95


@dataclass
class RunData:
    manifest: dict
    cells: list[dict]

    @property
    def subjects(self) -> list[dict]:
        return self.manifest.get("subjects", [])

    def subject(self, sid: str) -> dict | None:
        return next((s for s in self.subjects if s["id"] == sid), None)

    @property
    def outcome(self) -> Outcome | None:
        o = self.manifest.get("outcome")
        if not o:
            return None
        raw = {"name": o["name"], "description": o.get("description", ""), "decision": o.get("decision", ""),
               "primary_metric": o.get("primary_metric", "success_rate"), "splits": o.get("splits", ["test", "unseen"]),
               "gates": {k: v for k, v in o.get("gates", {}).items() if v is not None and k != "non_inferior_to"},
               "slices": o.get("slices", {}), "reference": o.get("references", [])}
        if o.get("gates", {}).get("non_inferior_to"):
            raw["gates"]["non_inferior"] = {"to": o["gates"]["non_inferior_to"], "margin": o["gates"].get("margin", 0.05)}
        return parse_outcome(raw)


def load_run(spec: str) -> RunData:
    from . import store

    rid = store.resolve_run(spec)
    m = store.read_manifest(rid)
    if m is None:
        raise TogiError("run_not_found", f"run {rid} has no manifest", next="togi eval runs")
    return RunData(m, store.read_cells(rid))


# --------------------------------------------------------------------------- basics


def score(cell: dict) -> int:
    return 1 if cell.get("success") is True else 0


def rate_of(cells: list[dict]) -> dict:
    n = len(cells)
    k = sum(score(c) for c in cells)
    lo, hi = stats.wilson(k, n)
    return {"n": n, "k": k, "rate": (k / n) if n else None, "lo": lo, "hi": hi}


def slices_of(cell: dict) -> list[str]:
    return (["overall"] + [f"capability:{c}" for c in cell.get("capabilities", [])] + [f"tier:{cell.get('tier')}"]
            + [f"world:{cell.get('world')}"])


def in_splits(cells: list[dict], splits: list[str] | None) -> list[dict]:
    return [c for c in cells if splits is None or c.get("split") in splits]


def by_subject(cells: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in cells:
        out.setdefault(c["subject"], []).append(c)
    return out


def success_table(data: RunData, splits: list[str] | None = None) -> dict:
    out: dict[str, dict[str, dict]] = {}
    for sid, cs in by_subject(in_splits(data.cells, splits)).items():
        groups: dict[str, list[dict]] = {}
        for c in cs:
            for s in slices_of(c):
                groups.setdefault(s, []).append(c)
        out[sid] = {name: rate_of(g) for name, g in sorted(groups.items())}
    return out


def success_by_split(data: RunData) -> dict:
    out: dict[str, dict[str, dict]] = {}
    for sid, cs in by_subject(data.cells).items():
        out[sid] = {sp: rate_of([c for c in cs if c.get("split") == sp]) for sp in sorted({c.get("split") for c in cs})}
    return out


def item_rates(cells: list[dict]) -> dict[str, float]:
    """Per item: the subject's success rate over the repeats of that item."""
    acc: dict[str, list[int]] = {}
    for c in cells:
        acc.setdefault(c["item"], []).append(score(c))
    return {i: sum(v) / len(v) for i, v in acc.items()}


# --------------------------------------------------------------------------- paired comparisons


def paired(data: RunData, a: str, b: str, splits: list[str] | None = None, *, resamples: int = 10000,
           seed: int = 20261027) -> dict:
    cs = by_subject(in_splits(data.cells, splits))
    ra, rb = item_rates(cs.get(a, [])), item_rates(cs.get(b, []))
    common = sorted(set(ra) & set(rb))
    wins_a = sum(1 for i in common if ra[i] > rb[i])
    wins_b = sum(1 for i in common if rb[i] > ra[i])
    diffs = [ra[i] - rb[i] for i in common]
    boot = stats.paired_bootstrap(diffs, resamples=resamples, seed=seed)
    return {"a": a, "b": b, "items": len(common), "wins_a": wins_a, "wins_b": wins_b, "ties": len(common) - wins_a - wins_b,
            "mean_a": (sum(ra[i] for i in common) / len(common)) if common else None,
            "mean_b": (sum(rb[i] for i in common) / len(common)) if common else None,
            "diff": boot["mean"], "diff_lo": boot["lo"], "diff_hi": boot["hi"], "p_bootstrap": boot["p_two_sided"],
            "p_mcnemar": stats.mcnemar_exact(wins_a, wins_b), "resamples": resamples, "seed": seed}


def paired_comparisons(data: RunData, splits: list[str] | None = None, *, resamples: int = 10000, seed: int = 20261027,
                       against: list[str] | None = None) -> list[dict]:
    """Every subject against each reference (or each given id); with no reference, all pairs."""
    ids = [s["id"] for s in data.subjects]
    refs = against or [s["id"] for s in data.subjects if s.get("role") in SANITY_ROLES]
    pairs = [(x, r) for r in refs for x in ids if x != r] if refs else [(x, y) for i, x in enumerate(ids) for y in ids[i + 1:]]
    return [paired(data, x, y, splits, resamples=resamples, seed=seed) for x, y in pairs]


# --------------------------------------------------------------------------- items


def reference_sanity(data: RunData, splits: list[str] | None = None) -> dict:
    """item id -> {"suspect": bool, "failed_by": [reference ids], "passed_by_others": bool}. A reference subject that fails
    an item (pass rate under 0.5 over its repeats) makes the item suspect: check the item, its checks and the skill first."""
    refs = [s["id"] for s in data.subjects if s.get("role") in SANITY_ROLES]
    cs = by_subject(in_splits(data.cells, splits))
    rates = {sid: item_rates(cs.get(sid, [])) for sid in [s["id"] for s in data.subjects]}
    items = sorted({c["item"] for c in in_splits(data.cells, splits)})
    out = {}
    for i in items:
        failed = [r for r in refs if i in rates[r] and rates[r][i] < 0.5]
        others = [sid for sid in rates if sid not in refs and rates[sid].get(i, 0) >= 0.5]
        out[i] = {"suspect": bool(failed), "failed_by": failed, "passed_by_others": bool(others)}
    return out


def item_statistics(data: RunData, splits: list[str] | None = None) -> list[dict]:
    cells = in_splits(data.cells, splits)
    cs = by_subject(cells)
    sids = list(cs)
    rates = {sid: item_rates(cs[sid]) for sid in sids}
    meta = {c["item"]: c for c in cells}
    sanity = reference_sanity(data, splits)
    out = []
    for i in sorted(meta):
        per = {sid: rates[sid][i] for sid in sids if i in rates[sid]}
        x = list(per.values())
        # skill of each subject on every OTHER item (rest-score), so an item is not correlated with itself
        skill = []
        for sid in per:
            others = [v for j, v in rates[sid].items() if j != i]
            skill.append(sum(others) / len(others) if others else 0.0)
        disc = stats.pearson(x, skill) if len(per) >= 3 else None
        diff = sum(x) / len(x) if x else None
        flags = []
        if len(per) >= 2:
            if all(v == 1.0 for v in x):
                flags.append("all_pass")
            elif all(v == 0.0 for v in x):
                flags.append("all_fail")
        if disc is not None and disc < 0:
            flags.append("negative_discrimination")
        elif disc is not None and disc < 0.1:
            flags.append("low_discrimination")
        if sanity.get(i, {}).get("suspect"):
            flags.append("suspect_reference_fail")
        if "all_fail" in flags and "suspect_reference_fail" not in flags:
            flags.append("suspect_all_fail")
        c0 = meta[i]
        out.append({"item": i, "split": c0.get("split"), "world": c0.get("world"), "tier": c0.get("tier"),
                    "capabilities": c0.get("capabilities", []), "n_subjects": len(per), "difficulty": diff,
                    "discrimination": disc, "rates": per, "saturation": "all_pass" if "all_pass" in flags else (
                        "all_fail" if "all_fail" in flags else None),
                    "suspect": bool(sanity.get(i, {}).get("suspect")) or "suspect_all_fail" in flags, "flags": flags,
                    "failed_by_reference": sanity.get(i, {}).get("failed_by", [])})
    return out


# --------------------------------------------------------------------------- cost, latency, curves


def cost_of(cell: dict, subject: dict | None) -> float | None:
    if cell.get("cost_usd") is not None:
        return float(cell["cost_usd"])
    price = (subject or {}).get("price")
    if price and cell.get("tokens"):
        pin = cell.get("prompt_tokens") or cell["tokens"]
        return (pin * price.get("in_per_mtok", 0.0) + (cell["tokens"] - pin) * price.get("out_per_mtok", 0.0)) / 1e6
    return None


def subject_summary(data: RunData, splits: list[str] | None = None) -> list[dict]:
    cs = by_subject(in_splits(data.cells, splits))
    out = []
    for s in data.subjects:
        cells = cs.get(s["id"], [])
        costs = [x for x in (cost_of(c, s) for c in cells) if x is not None]
        turns = [c["turns"] for c in cells if c.get("turns") is not None]
        r = rate_of(cells)
        out.append({"subject": s["id"], "role": s.get("role"), "size": s.get("size"), "provider": s.get("provider"),
                    "model": s.get("model"), "harness": s.get("harness"), "surface": s.get("surface"), **r,
                    "mean_cost_usd": (sum(costs) / len(costs)) if costs else None,
                    "total_cost_usd": sum(costs) if costs else None, "mean_wall_s": (sum(c.get("wall_s", 0) for c in cells) / len(cells)) if cells else None,
                    "median_turns": stats.median(turns), "mean_prompt_tokens": (lambda v: sum(v) / len(v) if v else None)(
                        [c["prompt_tokens"] for c in cells if c.get("prompt_tokens")]),
                    "invalid_cells": sum(1 for c in cells if c.get("driver_error")), "hash": s.get("hash")})
    return out


def harness_key_of(s: dict) -> str:
    from .subject import harness_key

    return harness_key(s)


def scaling_curves(data: RunData, splits: list[str] | None = None) -> dict:
    """Success vs model size, one curve per harness config. Subjects without a `size` are left out (and listed)."""
    summ = {x["subject"]: x for x in subject_summary(data, splits)}
    groups: dict[str, list[dict]] = {}
    unsized = []
    for s in data.subjects:
        x = summ[s["id"]]
        if s.get("size") is None:
            unsized.append(s["id"])
            continue
        groups.setdefault(harness_key_of(s), []).append({"subject": s["id"], "size": s["size"], "rate": x["rate"], "lo": x["lo"],
                                                         "hi": x["hi"], "n": x["n"], "harness": s.get("harness"), "surface": s.get("surface")})
    curves = {}
    for k, pts in groups.items():
        pts.sort(key=lambda p: p["size"])
        iso = stats.isotonic([p["rate"] or 0.0 for p in pts], [max(1, p["n"]) for p in pts])
        for p, y in zip(pts, iso):
            p["rate_monotone"] = y
        curves[k] = {"harness": pts[0]["harness"], "surface": pts[0]["surface"], "points": pts}
    return {"curves": curves, "unsized": unsized}


def harness_lift(scaling: dict, baseline: str | None = None) -> list[dict]:
    """The size-equivalent gain of a harness config over a baseline config: for each point B of another curve, the model size at
    which the baseline curve (monotone-smoothed, interpolated in log size) reaches B's success, and gain = that size / B's size.
    A gain above 1 means the harness setting is worth that many times more parameters. Beyond the baseline's range it says so."""
    curves = scaling["curves"]
    if len(curves) < 2:
        return []
    base_key = baseline if baseline in curves else max(curves, key=lambda k: (len(curves[k]["points"]), k))
    base = curves[base_key]["points"]
    xs, ys = [p["size"] for p in base], [p["rate_monotone"] for p in base]
    out = []
    for k, c in curves.items():
        if k == base_key:
            continue
        for p in c["points"]:
            x_eq, note = stats.interp_inverse(xs, ys, p["rate"] or 0.0)
            out.append({"config": k, "baseline": base_key, "subject": p["subject"], "size": p["size"], "rate": p["rate"],
                        "equivalent_baseline_size": x_eq, "gain_factor": (x_eq / p["size"]) if x_eq else None, "note": note})
    return out


def pareto(data: RunData, splits: list[str] | None = None) -> dict:
    summ = subject_summary(data, splits)
    cost_pts = [(x["mean_cost_usd"], x["rate"] or 0.0, x["subject"]) for x in summ if x["mean_cost_usd"] is not None]
    lat_pts = [(x["mean_wall_s"], x["rate"] or 0.0, x["subject"]) for x in summ if x["mean_wall_s"] is not None]
    return {"cost": {"points": [{"subject": i, "x": x, "y": y} for x, y, i in cost_pts], "frontier": stats.pareto_front(cost_pts)},
            "latency": {"points": [{"subject": i, "x": x, "y": y} for x, y, i in lat_pts], "frontier": stats.pareto_front(lat_pts)}}


def reference_trajectory_metrics(data: RunData, splits: list[str] | None = None) -> dict:
    """Per subject, over its SUCCESSFUL cells of items that have a reference trajectory: how lean (efficiency) and how close
    (coverage of the reference's commands) its trajectories are. A cheap model that passes with 3x the calls is a cost."""
    out: dict[str, dict] = {}
    for sid, cs in by_subject(in_splits(data.cells, splits)).items():
        rows = [c["reference"] for c in cs if c.get("success") is True and c.get("reference")]
        eff = [r["efficiency"] for r in rows if r.get("efficiency") is not None]
        cov = [r["coverage"] for r in rows if r.get("coverage") is not None]
        if rows:
            out[sid] = {"cells": len(rows), "mean_efficiency": sum(eff) / len(eff) if eff else None,
                        "mean_coverage": sum(cov) / len(cov) if cov else None, "mean_extra_calls": sum(r["extra_calls"] for r in rows) / len(rows)}
    return out


# --------------------------------------------------------------------------- outcome verdicts


def verdict_for(data: RunData, outcome: Outcome, sid: str, *, exclude_items: set[str] | None = None,
                resamples: int = 2000) -> dict:
    g = outcome.gates
    cells = [c for c in in_splits(data.cells, outcome.splits) if c["subject"] == sid and c["item"] not in (exclude_items or set())]
    groups: dict[str, list[dict]] = {}
    for c in cells:
        for s in slices_of(c):
            groups.setdefault(s, []).append(c)

    def wanted(name: str) -> bool:
        kind, _, val = name.partition(":")
        if name == "overall":
            return True
        sel = {"capability": outcome.slice_capabilities, "tier": [str(t) for t in outcome.slice_tiers], "world": outcome.slice_worlds}.get(kind, [])
        return not sel or val in sel

    slices = []
    for name, cs in sorted(groups.items()):
        if not wanted(name):
            continue
        r = rate_of(cs)
        slices.append({"slice": name, **r, "gated": r["n"] >= outcome.min_cells})
    failing = []
    for sl in slices:
        if not sl["gated"]:
            continue
        if sl["rate"] < g.min_success:
            failing.append({"slice": sl["slice"], "gate": "min_success", "got": sl["rate"], "want": g.min_success})
        if sl["lo"] < g.min_success_lcb:
            failing.append({"slice": sl["slice"], "gate": "min_success_lcb", "got": sl["lo"], "want": g.min_success_lcb})
    forbidden = sum(len(c.get("forbidden") or []) for c in cells)
    if forbidden > g.max_forbidden:
        failing.append({"slice": "overall", "gate": "max_forbidden", "got": forbidden, "want": g.max_forbidden})
    turns = [c["turns"] for c in cells if c.get("turns") is not None]
    med_turns = stats.median(turns)
    if g.max_median_turns is not None and med_turns is not None and med_turns > g.max_median_turns:
        failing.append({"slice": "overall", "gate": "max_median_turns", "got": med_turns, "want": g.max_median_turns})
    sub = data.subject(sid)
    costs = [x for x in (cost_of(c, sub) for c in cells) if x is not None]
    med_cost = stats.median(costs)
    if g.max_median_cost_usd is not None and med_cost is not None and med_cost > g.max_median_cost_usd:
        failing.append({"slice": "overall", "gate": "max_median_cost_usd", "got": med_cost, "want": g.max_median_cost_usd})
    ni = None
    if g.non_inferior_to and g.non_inferior_to != sid:
        ni = {"to": g.non_inferior_to, "margin": g.margin, "slices": []}
        if data.subject(g.non_inferior_to) is None:      # a bar set against a subject that was not run cannot be cleared
            failing.append({"slice": "overall", "gate": "non_inferior_reference", "got": 0, "want": 1})
        for sl in slices:
            if sl["slice"] != "overall" and not sl["gated"]:
                continue
            sub_ids = {c["item"] for c in groups.get(sl["slice"], [])}
            ref_cells = [c for c in in_splits(data.cells, outcome.splits) if c["subject"] == g.non_inferior_to and c["item"] in sub_ids]
            mine = [c for c in groups.get(sl["slice"], [])]
            ra, rb = item_rates(mine), item_rates(ref_cells)
            common = sorted(set(ra) & set(rb))
            if len(common) < 3:
                continue
            boot = stats.paired_bootstrap([ra[i] - rb[i] for i in common], resamples=resamples)
            ok = boot["lo"] >= -g.margin
            ni["slices"].append({"slice": sl["slice"], "items": len(common), "diff": boot["mean"], "lo": boot["lo"], "ok": ok})
            if not ok:
                failing.append({"slice": sl["slice"], "gate": "non_inferior", "got": boot["lo"], "want": -g.margin})
    gated = [s for s in slices if s["gated"]]
    worst = min(gated, key=lambda s: (s["lo"], s["slice"])) if gated else None
    if not cells:
        failing.append({"slice": "overall", "gate": "scored_cells", "got": 0, "want": 1})
    invalid = sum(1 for c in cells if c.get("driver_error"))
    return {"subject": sid, "verdict": "FAIL" if failing else "PASS", "n": len(cells), "worst_slice": worst, "slices": slices,
            "failing": failing, "forbidden": forbidden, "median_turns": med_turns, "median_cost_usd": med_cost,
            "invalid_cells": invalid, "non_inferior": ni, "reasons": [describe_failure(f) for f in failing]}


def describe_failure(f: dict) -> str:
    got, want = f["got"], f["want"]
    if f["gate"] in ("min_success", "min_success_lcb"):
        label = "success" if f["gate"] == "min_success" else "95% lower bound"
        return f"{f['slice']}: {label} {got:.2f} < {want:.2f}"
    if f["gate"] == "non_inferior":
        return f"{f['slice']}: not within the margin of the reference (lower bound {got:+.2f} < {want:+.2f})"
    if f["gate"] == "scored_cells":
        return "no scored cells in the outcome's splits"
    if f["gate"] == "non_inferior_reference":
        return "the outcome's reference subject was not part of this run, so non-inferiority cannot be shown"
    return f"{f['slice']}: {f['gate']} {got:g} > {want:g}"


def smallest_passing(verdicts: dict[str, dict], data: RunData) -> dict | None:
    passing = [sid for sid, v in verdicts.items() if v["verdict"] == "PASS"]
    if not passing:
        return None
    summ = {x["subject"]: x for x in subject_summary(data)}

    def key(sid: str):
        sub = data.subject(sid) or {}
        size = sub.get("size")
        cost = summ[sid]["mean_cost_usd"]
        return (size if size is not None else math.inf, cost if cost is not None else math.inf, sid)

    best = min(passing, key=key)
    sub = data.subject(best) or {}
    basis = "size" if sub.get("size") is not None else ("cost" if summ[best]["mean_cost_usd"] is not None else "listing order")
    return {"subject": best, "size": sub.get("size"), "basis": basis, "passing": passing}


def verdicts(data: RunData, outcome: Outcome | None = None, *, resamples: int = 2000) -> dict:
    outcome = outcome or data.outcome
    if outcome is None:
        return {}
    suspect = {i for i, s in reference_sanity(data, outcome.splits).items() if s["suspect"]}
    all_v = {s["id"]: verdict_for(data, outcome, s["id"], resamples=resamples) for s in data.subjects}
    clean_v = {s["id"]: verdict_for(data, outcome, s["id"], exclude_items=suspect, resamples=resamples) for s in data.subjects}
    return {"outcome": outcome.name, "decision": outcome.decision, "splits": outcome.splits, "gates": outcome.gates.to_dict(),
            "subjects": all_v, "smallest_passing": smallest_passing(all_v, data), "suspect_items": sorted(suspect),
            "excluding_suspect": {"subjects": clean_v, "smallest_passing": smallest_passing(clean_v, data)}}


# --------------------------------------------------------------------------- long horizon


def long_horizon(data: RunData, splits: list[str] | None = None) -> dict:
    cells = [c for c in in_splits(data.cells, splits) if c.get("n_sessions", 1) > 1]
    out: dict[str, Any] = {"cells": len(cells), "subjects": {}}
    for sid, cs in by_subject(cells).items():
        sessions = [s for c in cs for s in c.get("sessions", [])]
        steps = [len(s.get("steps") or []) or (s.get("turns") or 0) for s in sessions]
        calls = sum((s.get("metrics") or {}).get("calls", 0) for s in sessions)
        repeated = sum((s.get("metrics") or {}).get("repeated", 0) for s in sessions)
        drift_c = sum((c.get("drift") or {}).get("considered", 0) for c in cs)
        drift_d = sum((c.get("drift") or {}).get("drifting", 0) for c in cs)
        appr_sessions = sum(1 for s in sessions if (s.get("metrics") or {}).get("ended_at_approval"))
        appr_cells = sum(1 for c in cs if (c.get("metrics") or {}).get("needs_approval"))
        max_k = max((c["n_sessions"] for c in cs), default=0)
        curve = []
        for k in range(max_k):
            ks = [s for c in cs for s in c.get("sessions", []) if s.get("index") == k and s.get("success") is not None]
            n, wins = len(ks), sum(1 for s in ks if s["success"])
            lo, hi = stats.wilson(wins, n)
            curve.append({"session": k + 1, "n": n, "successes": wins, "rate": (wins / n) if n else None, "lo": lo, "hi": hi})
        pts = [(p["session"], p["rate"], p["n"]) for p in curve if p["rate"] is not None]
        decay = stats.slope([p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts]) if len(pts) >= 2 else None
        updates = [sum(1 for h in (c.get("task_history") or []) if h.get("op") == "update") for c in cs]
        no_update = sum(1 for c in cs if (c.get("metrics") or {}).get("state_changes", 0) and not any(
            h.get("op") == "update" for h in (c.get("task_history") or [])))
        out["subjects"][sid] = {
            "cells": len(cs), "end_state_success": rate_of(cs), "steps_per_session": (sum(steps) / len(steps)) if steps else None,
            "steps_by_session": [(sum(len(s.get("steps") or []) or (s.get("turns") or 0) for c in cs for s in c.get("sessions", []) if s.get("index") == k)
                                  / max(1, sum(1 for c in cs for s in c.get("sessions", []) if s.get("index") == k))) for k in range(max_k)],
            "calls": calls, "wasted_actions": repeated, "wasted_rate": (repeated / calls) if calls else None,
            "drift": {"considered": drift_c, "drifting": drift_d, "rate": (drift_d / drift_c) if drift_c else None},
            "approval": {"cells_with_approval_request": appr_cells, "sessions_ended_at_approval": appr_sessions,
                         "forbidden_attempts": sum(len(c.get("forbidden") or []) for c in cs)},
            "task_record": {"mean_updates": (sum(updates) / len(updates)) if updates else None, "cells_changing_state_without_updating": no_update},
            "session_curve": curve, "decay_per_session": decay}
    return out


# --------------------------------------------------------------------------- the whole document


def analyze(data: RunData, *, resamples: int = 10000, seed: int = 20261027, outcome: Outcome | None = None) -> dict:
    outcome = outcome or data.outcome
    splits = outcome.splits if outcome else None
    sanity = reference_sanity(data, splits)
    scaling = scaling_curves(data, splits)
    doc = {
        "run": data.manifest.get("run_id"), "manifest": {k: data.manifest.get(k) for k in (
            "created", "status", "suite", "repeats", "seed", "splits_touched", "include_unseen", "locked", "preregistration",
            "cells_planned", "cells_done", "cells_cached", "cells_new", "cost_usd", "settings", "note")},
        "outcome": outcome.to_dict() if outcome else None,
        "subjects": subject_summary(data, splits),
        "success": success_table(data, splits), "success_by_split": success_by_split(data),
        "paired": paired_comparisons(data, splits, resamples=resamples, seed=seed),
        "items": item_statistics(data, splits), "reference_sanity": {i: s for i, s in sanity.items() if s["suspect"]},
        "scaling": scaling, "harness_lift": harness_lift(scaling), "pareto": pareto(data, splits),
        "verdicts": verdicts(data, outcome, resamples=min(resamples, 2000)) if outcome else {},
        "taxonomy": tax.taxonomy(in_splits(data.cells, splits)),
        "long_horizon": long_horizon(data, splits), "reference_trajectory": reference_trajectory_metrics(data, splits),
        "integrity": {"splits_touched": data.manifest.get("splits_touched"), "include_unseen": data.manifest.get("include_unseen"),
                      "locked": data.manifest.get("locked"), "preregistration": data.manifest.get("preregistration"),
                      "hidden_splits": (data.manifest.get("suite") or {}).get("hidden"),
                      "reference_subjects": [s["id"] for s in data.subjects if s.get("role") in SANITY_ROLES],
                      "suspect_items": sorted(i for i, s in sanity.items() if s["suspect"]),
                      "driver_error_cells": sum(1 for c in data.cells if c.get("driver_error")),
                      "judge_cells": sum(1 for c in data.cells if c.get("judge"))},
    }
    return doc
