"""Reports (EVAL-SPEC section 10): `togi eval report <run>` -> report.json + a self-contained report.html (inline CSS and inline
SVG, no scripts, no fonts, no CDN, nothing fetched), and `togi eval compare <a> <b>` -> a diff of two runs."""
from __future__ import annotations

import html
import json
import math
from pathlib import Path
from typing import Any

from . import analysis, store

E = html.escape
COLORS = ["#2b6cb0", "#c05621", "#2f855a", "#805ad5", "#b83280", "#718096", "#d69e2e", "#319795"]


def pct(x: float | None, d: int = 0) -> str:
    return "-" if x is None else f"{100 * x:.{d}f}%"


def num(x: Any, d: int = 2) -> str:
    return "-" if x is None else (f"{x:.{d}f}" if isinstance(x, float) else str(x))


# --------------------------------------------------------------------------- svg helpers (no namespaces, no external refs)


def svg_forest(rows: list[tuple[str, float | None, float, float, int]], width: int = 640) -> str:
    """label, rate, lo, hi, n  -> horizontal CI bars on a 0..100% axis."""
    left, h = 190, 26
    height = 30 + h * len(rows)
    out = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">']
    for i, (lab, r, lo, hi, n) in enumerate(rows):
        y = 22 + i * h
        x = lambda v: left + v * (width - left - 20)
        out.append(f'<text x="{left - 8}" y="{y + 4}" text-anchor="end" font-size="12">{E(lab)}</text>')
        out.append(f'<line x1="{x(0)}" y1="{y}" x2="{x(1)}" y2="{y}" stroke="#e2e8f0"/>')
        if r is not None:
            out.append(f'<line x1="{x(lo):.1f}" y1="{y}" x2="{x(hi):.1f}" y2="{y}" stroke="{COLORS[i % len(COLORS)]}" stroke-width="3"/>')
            out.append(f'<circle cx="{x(r):.1f}" cy="{y}" r="5" fill="{COLORS[i % len(COLORS)]}"/>')
            out.append(f'<text x="{x(1) + 4:.1f}" y="{y + 4}" font-size="11" fill="#555">{pct(r)} n={n}</text>')
    for t in (0, .25, .5, .75, 1):
        xx = left + t * (width - left - 20)
        out.append(f'<text x="{xx:.1f}" y="{height - 4}" text-anchor="middle" font-size="10" fill="#777">{int(t * 100)}%</text>')
    out.append("</svg>")
    return "".join(out)


def svg_lines(series: list[dict], *, xlog: bool = False, xlabel: str = "", ylabel: str = "success", width: int = 560, height: int = 280) -> str:
    """series: [{"name", "points": [(x, y)]}]; y in 0..1."""
    xs = [p[0] for s in series for p in s["points"]]
    if not xs:
        return "<p class=muted>nothing to plot</p>"
    fx = (lambda v: math.log(v)) if xlog else (lambda v: v)
    lo, hi = min(map(fx, xs)), max(map(fx, xs))
    span = (hi - lo) or 1.0
    l, r, t, b = 46, 120, 14, 36
    X = lambda v: l + (fx(v) - lo) / span * (width - l - r)
    Y = lambda v: t + (1 - v) * (height - t - b)
    out = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">']
    for g in (0, .25, .5, .75, 1):
        out.append(f'<line x1="{l}" y1="{Y(g):.1f}" x2="{width - r}" y2="{Y(g):.1f}" stroke="#edf2f7"/>'
                   f'<text x="{l - 6}" y="{Y(g) + 4:.1f}" text-anchor="end" font-size="10" fill="#777">{int(g * 100)}%</text>')
    for i, s in enumerate(series):
        c = COLORS[i % len(COLORS)]
        pts = sorted(s["points"])
        out.append(f'<polyline fill="none" stroke="{c}" stroke-width="2" points="{" ".join(f"{X(a):.1f},{Y(b_):.1f}" for a, b_ in pts)}"/>')
        for a, b_ in pts:
            out.append(f'<circle cx="{X(a):.1f}" cy="{Y(b_):.1f}" r="4" fill="{c}"/>')
        out.append(f'<text x="{width - r + 6}" y="{t + 14 + i * 14}" font-size="11" fill="{c}">{E(s["name"][:22])}</text>')
    for a in sorted(set(xs)):
        out.append(f'<text x="{X(a):.1f}" y="{height - b + 14}" text-anchor="middle" font-size="10" fill="#777">{a:g}</text>')
    out.append(f'<text x="{(l + width - r) / 2}" y="{height - 4}" text-anchor="middle" font-size="11" fill="#555">{E(xlabel)}</text>')
    out.append("</svg>")
    return "".join(out)


def svg_scatter(points: list[dict], frontier: list[str], xlabel: str, width: int = 460, height: int = 260) -> str:
    if not points:
        return "<p class=muted>no data (cost is only known for providers that report it, or for subjects with a price)</p>"
    xs = [p["x"] for p in points]
    hi = max(xs) or 1.0
    l, r, t, b = 46, 20, 14, 36
    X = lambda v: l + v / hi * (width - l - r)
    Y = lambda v: t + (1 - v) * (height - t - b)
    out = [f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img">']
    for g in (0, .5, 1):
        out.append(f'<line x1="{l}" y1="{Y(g):.1f}" x2="{width - r}" y2="{Y(g):.1f}" stroke="#edf2f7"/>'
                   f'<text x="{l - 6}" y="{Y(g) + 4:.1f}" text-anchor="end" font-size="10" fill="#777">{int(g * 100)}%</text>')
    front = sorted((p for p in points if p["subject"] in frontier), key=lambda p: p["x"])
    if len(front) > 1:
        out.append(f'<polyline fill="none" stroke="#a0aec0" stroke-dasharray="4 3" points="{" ".join(f"{X(p["x"]):.1f},{Y(p["y"]):.1f}" for p in front)}"/>')
    for i, p in enumerate(points):
        on = p["subject"] in frontier
        out.append(f'<circle cx="{X(p["x"]):.1f}" cy="{Y(p["y"]):.1f}" r="{6 if on else 4}" fill="{COLORS[i % len(COLORS)]}" '
                   f'opacity="{1 if on else .5}"/><text x="{X(p["x"]) + 8:.1f}" y="{Y(p["y"]) + 4:.1f}" font-size="10">{E(p["subject"][:18])}</text>')
    out.append(f'<text x="{(l + width - r) / 2}" y="{height - 4}" text-anchor="middle" font-size="11" fill="#555">{E(xlabel)}</text></svg>')
    return "".join(out)


def svg_bars(rows: list[tuple[str, int]], width: int = 460) -> str:
    if not rows:
        return "<p class=muted>no failures</p>"
    mx = max(n for _, n in rows) or 1
    h = 22
    out = [f'<svg viewBox="0 0 {width} {h * len(rows) + 6}" width="{width}" height="{h * len(rows) + 6}" role="img">']
    for i, (lab, n) in enumerate(rows):
        y = 4 + i * h
        out.append(f'<text x="108" y="{y + 14}" text-anchor="end" font-size="12">{E(lab)}</text>'
                   f'<rect x="114" y="{y + 2}" width="{(width - 170) * n / mx:.1f}" height="14" fill="#c05621"/>'
                   f'<text x="{118 + (width - 170) * n / mx:.1f}" y="{y + 14}" font-size="11">{n}</text>')
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------- html


CSS = """body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:24px auto;max-width:1040px;color:#1a202c;padding:0 16px}
h1{font-size:22px}h2{font-size:17px;margin-top:30px;border-bottom:1px solid #e2e8f0;padding-bottom:4px}
table{border-collapse:collapse;margin:8px 0;font-size:13px}td,th{border:1px solid #e2e8f0;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f7fafc}.pass{background:#c6f6d5;color:#22543d;padding:1px 8px;border-radius:9px;font-weight:600}
.fail{background:#fed7d7;color:#742a2a;padding:1px 8px;border-radius:9px;font-weight:600}.muted{color:#718096}
.flag{background:#feebc8;color:#7b341e;padding:0 6px;border-radius:8px;margin-right:3px;font-size:12px}code{background:#edf2f7;padding:0 4px}"""


def table(headers: list[str], rows: list[list[str]], raw: bool = False) -> str:
    th = "".join(f"<th>{E(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c if raw else E(str(c))}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{th}</tr>{body}</table>"


def badge(v: str) -> str:
    return f'<span class="{"pass" if v == "PASS" else "fail"}">{E(v)}</span>'


def render_html(doc: dict) -> str:
    m = doc["manifest"]
    parts = [f"<!doctype html><html lang=en><head><meta charset=utf-8><title>Togi eval {E(str(doc['run']))}</title><style>{CSS}</style></head><body>",
             f"<h1>Togi eval: {E(str(doc['run']))}</h1>"]
    o = doc.get("outcome")
    meta = [["suite", (m.get("suite") or {}).get("name")], ["status", m.get("status")], ["splits touched", ", ".join(m.get("splits_touched") or [])],
            ["repeats / seed", f"{m.get('repeats')} / {m.get('seed')}"], ["cells", f"{m.get('cells_done')} of {m.get('cells_planned')} "
                                                                           f"({m.get('cells_cached')} cached, {m.get('cells_new')} run)"],
            ["cost (USD)", num(m.get("cost_usd"), 4)], ["locked", "yes" if m.get("locked") else "no (not pre-registered)"]]
    if o:
        meta.insert(1, ["outcome", f"{o['name']}: {o.get('decision', '')}"])
    parts.append(table(["", ""], meta))
    # ---- verdicts
    v = doc.get("verdicts") or {}
    if v:
        parts.append("<h2>Outcome verdicts</h2><p class=muted>Gates applied to the worst slice, with Wilson 95% lower bounds. "
                     f"Counted splits: {E(', '.join(v['splits']))}. Gates: {E(json.dumps({k: x for k, x in v['gates'].items() if x}))}</p>")
        rows = []
        for sid, x in v["subjects"].items():
            ws = x["worst_slice"]
            clean = v["excluding_suspect"]["subjects"][sid]["verdict"]
            rows.append([E(sid), badge(x["verdict"]), E(f"{ws['slice']}: {ws['k']}/{ws['n']}, lower bound {pct(ws['lo'])}") if ws else "-",
                         E("; ".join(x["reasons"][:3]) or "-"), badge(clean) if v["suspect_items"] else "-"])
        parts.append(table(["subject", "verdict", "worst slice", "why not", "without suspect items"], rows, raw=True))
        sp = v.get("smallest_passing")
        parts.append(f"<p><b>Smallest subject that passes:</b> {E(sp['subject']) + (' (' + str(sp['size']) + 'B)' if sp['size'] else '') if sp else 'none'}</p>")
        if v["suspect_items"]:
            parts.append(f"<p class=muted>{len(v['suspect_items'])} item(s) are flagged suspect (a reference subject failed them): "
                         f"{E(', '.join(v['suspect_items'][:12]))}. Check those items before blaming a subject.</p>")
    # ---- success
    summ = doc["subjects"]
    parts.append("<h2>Success with 95% confidence intervals</h2>")
    parts.append(svg_forest([(s["subject"] + (" *" if s.get("role") in ("reference", "target") else ""), s["rate"], s["lo"], s["hi"], s["n"]) for s in summ]))
    parts.append("<p class=muted>* reference subject. Counted splits only. Wilson score intervals.</p>")
    parts.append(table(["subject", "role", "cells", "success", "95% CI", "median turns", "mean cost", "mean wall s", "mean prompt tokens", "invalid"],
                       [[s["subject"], s["role"], s["n"], pct(s["rate"], 1), f"{pct(s['lo'])} to {pct(s['hi'])}", num(s["median_turns"], 1),
                         num(s["mean_cost_usd"], 4), num(s["mean_wall_s"], 1), num(s["mean_prompt_tokens"], 0), s["invalid_cells"]] for s in summ]))
    slice_names = sorted({k for t in doc["success"].values() for k in t})
    parts.append(table(["slice"] + list(doc["success"]), [[sl] + [
        (lambda c: f"{c['k']}/{c['n']} ({pct(c['lo'])} to {pct(c['hi'])})" if c else "-")(doc["success"][sid].get(sl)) for sid in doc["success"]]
        for sl in slice_names]))
    bys = doc.get("success_by_split") or {}
    if bys:
        splits = sorted({sp for t in bys.values() for sp in t})
        parts.append("<h3>By split</h3>" + table(["subject"] + splits, [[sid] + [
            (lambda c: f"{c['k']}/{c['n']}" if c else "-")(bys[sid].get(sp)) for sp in splits] for sid in bys]))
    # ---- paired
    if doc["paired"]:
        parts.append("<h2>Paired comparisons (same items)</h2><p class=muted>Exact McNemar on item-level wins; "
                     "diff = mean success difference with a seeded item-clustered bootstrap 95% interval.</p>")
        parts.append(table(["A", "B", "items", "A wins", "B wins", "ties", "diff (A-B)", "95% interval", "McNemar p", "bootstrap p"],
                           [[p["a"], p["b"], p["items"], p["wins_a"], p["wins_b"], p["ties"], num(p["diff"], 3),
                             f"{num(p['diff_lo'], 3)} to {num(p['diff_hi'], 3)}", num(p["p_mcnemar"], 4), num(p["p_bootstrap"], 4)] for p in doc["paired"]]))
    # ---- items
    parts.append("<h2>Items</h2><p class=muted>Difficulty = pass rate across subjects. Discrimination = correlation of the item's pass "
                 "with the subjects' skill on the other items. All-pass and all-fail items tell us nothing; suspect items "
                 "were failed by a reference subject.</p>")
    items = sorted(doc["items"], key=lambda i: (not i["flags"], i["item"]))
    parts.append(table(["item", "split", "tier", "difficulty", "discrimination", "flags"], [[
        E(i["item"]), i["split"], i["tier"], num(i["difficulty"]), num(i["discrimination"]),
        "".join(f'<span class=flag>{E(f)}</span>' for f in i["flags"])] for i in items[:200]], raw=True))
    # ---- curves
    sc = doc["scaling"]
    parts.append("<h2>Scaling and cost</h2>")
    if sc["curves"]:
        series = [{"name": (c["harness"] or {}).get("loop", "") + " " + k[:4] if c.get("harness") else k[:6],
                   "points": [(p["size"], p["rate"] or 0.0) for p in c["points"]]} for k, c in sc["curves"].items()]
        parts.append(svg_lines(series, xlog=True, xlabel="model size (billions of parameters, log scale)"))
    else:
        parts.append("<p class=muted>No subject has a size, so there is no scaling curve (set `size` on a subject).</p>")
    if doc["harness_lift"]:
        parts.append("<h3>Harness lift</h3>" + table(["config", "subject", "size", "success", "baseline needs", "gain", "note"], [[
            h["config"][:6], h["subject"], h["size"], pct(h["rate"]), num(h["equivalent_baseline_size"], 1), num(h["gain_factor"], 2), h["note"]]
            for h in doc["harness_lift"]]))
    pa = doc["pareto"]
    parts.append("<h3>Pareto: success vs cost</h3>" + svg_scatter(pa["cost"]["points"], pa["cost"]["frontier"], "mean cost per run (USD)"))
    parts.append("<h3>Pareto: success vs latency</h3>" + svg_scatter(pa["latency"]["points"], pa["latency"]["frontier"], "mean wall seconds per run"))
    # ---- taxonomy
    tx = doc["taxonomy"]
    parts.append(f"<h2>Failure classes</h2><p class=muted>{tx['failed']} failed cells, classified by rule from their trajectories "
                 "(`togi eval audit &lt;run&gt; --class C` samples them for a human check).</p>")
    parts.append(svg_bars(list(tx["by_class"].items())))
    rt = doc.get("reference_trajectory") or {}
    if rt:
        parts.append("<h2>Against the reference trajectories</h2><p class=muted>On passed cells: reference calls / the subject's calls "
                     "(1.0 = as lean as the reference) and the share of the reference's commands it also used.</p>")
        parts.append(table(["subject", "cells", "efficiency", "coverage", "extra calls"], [[sid, x["cells"], num(x["mean_efficiency"]),
                                                                                            pct(x["mean_coverage"]), num(x["mean_extra_calls"], 1)] for sid, x in rt.items()]))
    # ---- long horizon
    lh = doc.get("long_horizon") or {}
    if lh.get("cells"):
        parts.append("<h2>Long-running operation</h2>")
        rows = []
        for sid, x in lh["subjects"].items():
            es = x["end_state_success"]
            rows.append([sid, f"{es['k']}/{es['n']} ({pct(es['lo'])} to {pct(es['hi'])})", num(x["steps_per_session"], 1),
                         pct(x["wasted_rate"], 1), pct(x["drift"]["rate"], 1), x["approval"]["sessions_ended_at_approval"],
                         num(x["decay_per_session"], 3)])
        parts.append(table(["subject", "end-state success", "steps / session", "wasted actions", "drift", "sessions ended at approval", "success slope / session"], rows))
        series = [{"name": sid, "points": [(p["session"], p["rate"]) for p in x["session_curve"] if p["rate"] is not None]}
                  for sid, x in lh["subjects"].items()]
        parts.append("<h3>Per-session success</h3>" + svg_lines(series, xlabel="session number"))
    # ---- integrity
    ig = doc["integrity"]
    parts.append("<h2>Integrity</h2><ul>" + "".join(f"<li>{E(x)}</li>" for x in [
        f"splits touched: {', '.join(ig['splits_touched'] or [])}" + (" (unseen included on request)" if ig["include_unseen"] else " (unseen not run)"),
        f"hidden from this run: {json.dumps(ig['hidden_splits'] or {})}",
        f"suite locked and pre-registered: {'yes' if ig['locked'] else 'no'}"
        + (f"; deviations: {'; '.join(ig['preregistration']['deviations']) or 'none'}" if ig.get("preregistration") else ""),
        f"reference subjects: {', '.join(ig['reference_subjects']) or 'NONE'}",
        f"suspect items: {len(ig['suspect_items'])}", f"cells with a driver error (counted as failures): {ig['driver_error_cells']}"]) + "</ul>")
    parts.append("</body></html>")
    return "".join(parts)


def text_summary(doc: dict) -> str:
    lines = [f"eval {doc['run']}  suite={(doc['manifest'].get('suite') or {}).get('name')}  status={doc['manifest'].get('status')}  "
             f"splits={','.join(doc['manifest'].get('splits_touched') or [])}"]
    for s in doc["subjects"]:
        lines.append(f"  {s['subject']:<32} {s['k']:>3}/{s['n']:<3} {pct(s['rate'], 1):>6}  ({pct(s['lo'])}-{pct(s['hi'])})"
                     + (f"  [{s['role']}]" if s.get("role") != "candidate" else ""))
    v = doc.get("verdicts") or {}
    for sid, x in (v.get("subjects") or {}).items():
        lines.append(f"  verdict {sid}: {x['verdict']}" + (f"  ({'; '.join(x['reasons'][:2])})" if x["reasons"] else ""))
    if v:
        sp = v.get("smallest_passing")
        lines.append("  smallest passing: " + (sp["subject"] if sp else "none"))
    if doc["integrity"]["suspect_items"]:
        lines.append(f"  suspect items: {', '.join(doc['integrity']['suspect_items'][:8])}")
    return "\n".join(lines)


def build_report(run_spec: str, *, resamples: int = 10000, seed: int = 20261027, outcome_path: str | None = None) -> dict:
    data = analysis.load_run(run_spec)
    outcome = None
    if outcome_path:
        from .outcome import load_outcome

        outcome = load_outcome(outcome_path)
    doc = analysis.analyze(data, resamples=resamples, seed=seed, outcome=outcome)
    d = store.run_dir(doc["run"])
    (d / "report.json").write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
    (d / "report.html").write_text(render_html(doc), encoding="utf-8")
    doc["paths"] = {"json": str(d / "report.json"), "html": str(d / "report.html")}
    doc["text"] = text_summary(doc)
    return doc


# --------------------------------------------------------------------------- compare


def compare_runs(a_spec: str, b_spec: str) -> dict:
    A, B = analysis.load_run(a_spec), analysis.load_run(b_spec)
    outcome = B.outcome or A.outcome
    splits = outcome.splits if outcome else None
    sa = {x["hash"]: x for x in analysis.subject_summary(A, splits)}
    sb = {x["hash"]: x for x in analysis.subject_summary(B, splits)}
    by_id_a = {x["subject"]: x for x in sa.values()}
    subjects = []
    for h, y in sb.items():
        x = sa.get(h) or by_id_a.get(y["subject"])
        if x is None:
            subjects.append({"subject": y["subject"], "status": "new in B", "b": y["rate"]})
            continue
        diff = (y["rate"] or 0) - (x["rate"] or 0)
        clear = (y["lo"] > x["hi"]) or (y["hi"] < x["lo"])
        subjects.append({"subject": y["subject"], "same_config": h in sa, "a": x["rate"], "b": y["rate"], "diff": diff,
                         "a_ci": [x["lo"], x["hi"]], "b_ci": [y["lo"], y["hi"]], "clearly_different": clear})
    removed = [x["subject"] for h, x in sa.items() if h not in sb and x["subject"] not in {y["subject"] for y in sb.values()}]
    flips = []
    ia = {(c["subject"], c["item"]): [] for c in in_splits(A, splits)}
    for c in in_splits(A, splits):
        ia[(c["subject"], c["item"])].append(analysis.score(c))
    ib: dict[tuple, list[int]] = {}
    for c in in_splits(B, splits):
        ib.setdefault((c["subject"], c["item"]), []).append(analysis.score(c))
    for k in sorted(set(ia) & set(ib)):
        ra, rb = sum(ia[k]) / len(ia[k]), sum(ib[k]) / len(ib[k])
        if ra != rb:
            flips.append({"subject": k[0], "item": k[1], "a": ra, "b": rb})
    va = analysis.verdicts(A, outcome) if outcome else {}
    vb = analysis.verdicts(B, outcome) if outcome else {}
    changes = [{"subject": s, "a": va["subjects"][s]["verdict"], "b": vb["subjects"][s]["verdict"]} for s in (vb.get("subjects") or {})
               if s in (va.get("subjects") or {}) and va["subjects"][s]["verdict"] != vb["subjects"][s]["verdict"]]
    items_a, items_b = {c["item"] for c in A.cells}, {c["item"] for c in B.cells}
    doc = {"a": A.manifest["run_id"], "b": B.manifest["run_id"], "subjects": subjects, "removed_subjects": removed,
           "item_flips": flips, "verdict_changes": changes, "items_only_in_a": sorted(items_a - items_b),
           "items_only_in_b": sorted(items_b - items_a), "same_suite": (A.manifest.get("suite") or {}).get("hash") == (B.manifest.get("suite") or {}).get("hash")}
    lines = [f"compare {doc['a']} -> {doc['b']}" + ("" if doc["same_suite"] else "  (the suites differ)")]
    for s in subjects:
        if "diff" in s:
            lines.append(f"  {s['subject']:<32} {pct(s['a'], 1)} -> {pct(s['b'], 1)}  ({s['diff'] * 100:+.1f} pts)"
                         + ("  CLEAR" if s["clearly_different"] else "") + ("" if s["same_config"] else "  (config changed)"))
        else:
            lines.append(f"  {s['subject']:<32} {s['status']}")
    lines += [f"  verdict {c['subject']}: {c['a']} -> {c['b']}" for c in changes]
    lines.append(f"  {len(flips)} item(s) changed their pass rate")
    doc["text"] = "\n".join(lines)
    return doc


def in_splits(data: analysis.RunData, splits: list[str] | None) -> list[dict]:
    return analysis.in_splits(data.cells, splits)
