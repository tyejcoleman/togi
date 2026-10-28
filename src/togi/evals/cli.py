"""`togi eval ...`: the command line of the eval layer (EVAL-SPEC "CLI")."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .. import output
from ..cli import Reply, _mode, _usage, parse_flags
from ..errors import TogiError

HELP = """\
togi eval outcome new <name>                           write outcomes/<name>.toml (the decision, the gates, the references)
togi eval suite import-stone <bundle> [--out DIR]      a bundle's stone tasks as a suite (items/*.toml)
togi eval suite validate <suite>                       problems that make an item untrustworthy (a faulty check lies)
togi eval generate <generator> --seed N --out <suite>  a seeded suite with computed answers (generators: sheet)
togi eval lock <suite> [--outcome O --subjects S --repeats N] [--relock]    hash every item; pre-register the plan
togi eval run --outcome O --suite S --subjects <file|list> --repeats N [--include-unseen] [--max-cost USD]
              [--seed N] [--splits dev,test] [--items a,b] [--resume RUN] [--concurrency claude-cli=2,ollama=1]
              [--item-timeout S] [--max-item-cost USD] [--no-reference-check] [--judge claude:<model>]
togi eval report <run> [--outcome O]                   report.json + a self-contained report.html
togi eval verdict <run> [--outcome O]                  the gates on the worst slice, per subject
togi eval compare <a> <b>                              two runs side by side
togi eval items <run>                                  difficulty, discrimination, saturation, suspect items
togi eval audit <run> --class <c> [--n 5] [--seed N]   a random sample of one failure class, for a human to check
togi eval contamination --suite S [--data DIR ...]     canaries and n-gram overlap in skills, examples, proposals, training data
togi eval runs                                         the runs in this home
subjects: a TOML file of [[subject]] entries, or  provider:model[@surface][+key=value...],...
          e.g.  script+role=reference,claude-cli:haiku,ollama:qwen2.5:7b+loop=operator
"""

SPEC = {"out": "str", "seed": "int", "per-template": "int", "bundle": "list", "outcome": "str", "subjects": "str",
        "repeats": "int", "suite": "str", "include-unseen": "bool", "max-cost": "float", "splits": "str", "items": "str",
        "resume": "str", "run-id": "str", "concurrency": "str", "item-timeout": "float", "max-item-cost": "float",
        "no-reference-check": "bool", "relock": "bool", "class": "str", "n": "int", "data": "list", "ngram": "int",
        "resamples": "int", "quiet": "bool", "note": "str", "dir": "str", "judge": "str"}


def _csv(v: str | None) -> list[str] | None:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def _need(f: dict, key: str, flag: str, example: str) -> str:
    if not f[key]:
        raise TogiError("bad_arg", f"togi eval needs {flag}", next=example)
    return f[key]


def _bundle_map(items: list[str]) -> dict[str, str]:
    out = {}
    for b in items or []:
        name, eq, path = b.partition("=")
        if not eq:
            raise TogiError("bad_arg", "--bundle takes name=path", next="--bundle sheet=/path/to/sheet")
        out[name] = str(Path(path).expanduser().resolve())
    return out


def h_eval(argv: list[str]) -> Reply:
    pos, f = parse_flags(argv, SPEC, "eval")
    if f["help"] or not pos:
        return Reply(text=HELP, mode=_mode(f))
    sub, rest = pos[0], pos[1:]
    handler = {"outcome": h_outcome, "suite": h_suite, "generate": h_generate, "lock": h_lock, "run": h_run, "report": h_report,
               "verdict": h_verdict, "compare": h_compare, "items": h_items, "audit": h_audit, "contamination": h_contamination,
               "runs": h_runs}.get(sub)
    if handler is None:
        raise TogiError("bad_arg", f"unknown eval command {sub!r}", next="togi eval --help")
    return Reply(env=output.ok_envelope(handler(rest, f)), mode=_mode(f))


# --------------------------------------------------------------------------- authoring


def h_outcome(rest: list[str], f: dict) -> dict:
    from . import outcome

    if len(rest) != 2 or rest[0] != "new":
        raise TogiError("bad_arg", "usage: togi eval outcome new <name>", next="togi eval --help")
    p = outcome.new_outcome(rest[1], Path(f["dir"]) if f["dir"] else None)
    return {"outcome": str(p), "next": f"edit {p}: the decision, the gates, the reference subjects"}


def h_suite(rest: list[str], f: dict) -> dict:
    from . import items

    if rest[:1] == ["import-stone"] and len(rest) == 2:
        dest, ids = items.import_stone(rest[1], Path(f["out"]).expanduser() if f["out"] else None)
        return {"suite": str(dest), "items": len(ids), "ids": ids, "next": f"togi eval run --suite {dest} --outcome O --subjects script+role=reference"}
    if rest[:1] == ["validate"] and len(rest) == 2:
        suite = items.load_suite(rest[1], purpose="lock")
        problems = items.validate_suite(suite)
        return {"suite": suite.name, "items": len(suite.items), "problems": problems, "ok": not problems,
                "text": "ok: no problems" if not problems else "\n".join(problems[:20])}
    raise TogiError("bad_arg", "usage: togi eval suite import-stone <bundle> | validate <suite>", next="togi eval --help")


def h_generate(rest: list[str], f: dict) -> dict:
    from .generators import generate

    if len(rest) != 1:
        raise TogiError("bad_arg", "usage: togi eval generate <generator> --seed N --out <suite>", next="togi eval generate sheet --seed 7 --out suites/sheet")
    out = Path(_need(f, "out", "--out", "togi eval generate sheet --seed 7 --out suites/sheet")).expanduser()
    opts: dict = {}
    if f["per_template"]:
        opts["per_template"] = f["per_template"]
    bundles = _bundle_map(f["bundle"])
    if bundles.get("sheet"):
        opts["bundle"] = bundles["sheet"]
    rep = generate(rest[0], out, f["seed"] if f["seed"] is not None else 0, **opts)
    return {**rep, "next": f"togi eval lock {out}   then: togi eval run --suite {out} ..."}


def _subjects(f: dict):
    from . import subject

    return subject.load_subjects(_need(f, "subjects", "--subjects", "togi eval run --subjects script+role=reference,claude-cli:haiku"))


def h_lock(rest: list[str], f: dict) -> dict:
    from . import integrity, outcome

    if len(rest) != 1:
        raise TogiError("bad_arg", "usage: togi eval lock <suite>", next="togi eval --help")
    oc = outcome.load_outcome(f["outcome"]) if f["outcome"] else None
    subs = _subjects(f) if f["subjects"] else None
    rec = integrity.lock_suite(rest[0], outcome=oc, subjects=subs, repeats=f["repeats"], relock=f["relock"])
    return {"suite": rec["suite"], "suite_hash": rec["suite_hash"], "items": len(rec["items"]), "locked_at": rec["locked_at"],
            "unchanged": bool(rec.get("unchanged")), "preregistered": {"outcome": (rec["preregistration"]["outcome"] or {}).get("name"),
                                                                       "subjects": len(rec["preregistration"]["subjects"]),
                                                                       "repeats": rec["preregistration"]["repeats"]},
            "next": "togi eval run refuses this suite if any item or data file changes"}


# --------------------------------------------------------------------------- running


def _concurrency(spec: str | None) -> dict[str, int]:
    out = {}
    for part in _csv(spec) or []:
        k, eq, v = part.partition("=")
        if not eq or not v.isdigit():
            raise TogiError("bad_arg", "--concurrency takes provider=N,provider=N", next="--concurrency claude-cli=2,ollama=1")
        out[k] = int(v)
    return out


def h_run(rest: list[str], f: dict) -> dict:
    from . import outcome, report, runner

    oc = outcome.load_outcome(_need(f, "outcome", "--outcome", "togi eval outcome new <name>"))
    suite_path = _need(f, "suite", "--suite", "togi eval run --suite <dir>")
    subs = _subjects(f)

    def progress(done: int, total: int, cell: dict) -> None:
        if not f["quiet"]:
            sys.stderr.write(f"[eval {done}/{total}] {cell['item']} / {cell['subject']} / seed {cell['seed']}: "
                             f"{'pass' if cell.get('success') else ('error' if cell.get('driver_error') else 'fail')}"
                             f"{' (cached)' if cell.get('cached') else ''}\n")

    judge = None
    if f["judge"]:
        from . import judge as judgelib

        kind, _, model = f["judge"].partition(":")
        if kind != "claude":
            raise TogiError("bad_arg", "--judge takes claude[:<model>]", next="--judge claude:haiku")
        judge = judgelib.claude_judge(model or None)
    spec = runner.RunSpec(suite_path=suite_path, subjects=subs, outcome=oc, repeats=f["repeats"] or 1, seed=f["seed"] or 0, judge=judge,
                          include_unseen=f["include_unseen"], splits=_csv(f["splits"]), max_cost=f["max_cost"],
                          max_item_cost=f["max_item_cost"] if f["max_item_cost"] is not None else 0.5,
                          item_timeout=f["item_timeout"] or 300.0, concurrency=_concurrency(f["concurrency"]), run_id=f["run_id"] or f["resume"],
                          resume=bool(f["resume"]), require_reference=not f["no_reference_check"], item_ids=_csv(f["items"]),
                          progress=progress, note=f["note"] or "")
    m = runner.run_matrix(spec)
    doc = report.build_report(m["run_id"], resamples=2000)
    v = doc.get("verdicts") or {}
    out = {"run": m["run_id"], "status": m["status"], "cells": {"planned": m["cells_planned"], "done": m["cells_done"],
                                                                "cached": m["cells_cached"], "new": m["cells_new"]},
           "splits_touched": m["splits_touched"], "cost_usd": m["cost_usd"],
           "verdicts": {sid: x["verdict"] for sid, x in (v.get("subjects") or {}).items()},
           "smallest_passing": (v.get("smallest_passing") or {}).get("subject"), "suspect_items": doc["integrity"]["suspect_items"],
           "report": doc["paths"], "text": doc["text"]}
    if m["status"] != "complete":
        out["next"] = f"togi eval run ... --resume {m['run_id']}"
    return out


def h_report(rest: list[str], f: dict) -> dict:
    from . import report

    doc = report.build_report(rest[0] if rest else "latest", resamples=f["resamples"] or 10000, seed=f["seed"] or 20261027,
                              outcome_path=f["outcome"])
    return {"run": doc["run"], "paths": doc["paths"], "subjects": [{k: s[k] for k in ("subject", "n", "rate", "lo", "hi")} for s in doc["subjects"]],
            "verdicts": {sid: x["verdict"] for sid, x in ((doc.get("verdicts") or {}).get("subjects") or {}).items()},
            "text": doc["text"]}


def h_verdict(rest: list[str], f: dict) -> dict:
    from . import analysis
    from .outcome import load_outcome

    data = analysis.load_run(rest[0] if rest else "latest")
    oc = load_outcome(f["outcome"]) if f["outcome"] else data.outcome
    if oc is None:
        raise TogiError("bad_arg", "this run has no outcome; pass --outcome", next="togi eval verdict <run> --outcome <file>")
    v = analysis.verdicts(data, oc)
    lines = [f"{sid}: {x['verdict']}" + (f"  ({'; '.join(x['reasons'][:2])})" if x["reasons"] else "") for sid, x in v["subjects"].items()]
    sp = v["smallest_passing"]
    lines.append("smallest passing: " + (sp["subject"] if sp else "none"))
    return {"verdicts": {sid: {k: x[k] for k in ("verdict", "n", "worst_slice", "reasons", "invalid_cells")} for sid, x in v["subjects"].items()},
            "smallest_passing": sp, "suspect_items": v["suspect_items"],
            "verdicts_without_suspect_items": {sid: x["verdict"] for sid, x in v["excluding_suspect"]["subjects"].items()}, "text": "\n".join(lines)}


def h_compare(rest: list[str], f: dict) -> dict:
    from . import report

    if len(rest) != 2:
        raise TogiError("bad_arg", "usage: togi eval compare <runA> <runB>", next="togi eval runs")
    return report.compare_runs(rest[0], rest[1])


def h_items(rest: list[str], f: dict) -> dict:
    from . import analysis

    data = analysis.load_run(rest[0] if rest else "latest")
    oc = data.outcome
    rows = analysis.item_statistics(data, oc.splits if oc else None)
    flagged = [r for r in rows if r["flags"]]
    lines = [f"{len(rows)} items, {len(flagged)} flagged"] + [
        f"  {r['item']:<40} diff={r['difficulty']:.2f} disc={('%.2f' % r['discrimination']) if r['discrimination'] is not None else '-'}  {','.join(r['flags'])}"
        for r in flagged[:30]]
    slim = [{k: r[k] for k in ("item", "split", "tier", "difficulty", "discrimination", "saturation", "suspect", "flags")} for r in rows]
    return {"items": slim, "flagged": len(flagged), "suspect": [r["item"] for r in rows if r["suspect"]], "text": "\n".join(lines)}


def h_audit(rest: list[str], f: dict) -> dict:
    from . import analysis, taxonomy

    cls = _need(f, "class", "--class", "togi eval audit <run> --class did_math")
    data = analysis.load_run(rest[0] if rest else "latest")
    try:
        sample = taxonomy.audit_sample(data.cells, cls, f["n"] or 5, f["seed"] if f["seed"] is not None else 20261027)
    except ValueError as e:
        raise TogiError("bad_arg", str(e), next=f"classes: {', '.join(taxonomy.CLASSES)}")
    return {"class": cls, "available": sum(1 for c in data.cells if (taxonomy.classify(c) or {}).get("class") == cls), "sample": sample}


def h_contamination(rest: list[str], f: dict) -> dict:
    from . import integrity, items

    suite = items.load_suite(_need(f, "suite", "--suite", "togi eval contamination --suite <dir>"), purpose="contamination")
    sources = integrity.default_sources(suite) + [(f"data:{d}", Path(d).expanduser()) for d in (f["data"] or [])]
    rep = integrity.scan_contamination(suite, sources, ngram=f["ngram"] or 8)
    rep["text"] = ("clean: no canary and no n-gram overlap in " + f"{rep['files_scanned']} file(s)") if rep["clean"] else (
        f"{len(rep['hits'])} hit(s):\n" + "\n".join(f"  {h['kind']} {h['source']} {h['file']} {h.get('item') or h.get('canary')}" for h in rep["hits"][:15]))
    return rep


def h_runs(rest: list[str], f: dict) -> dict:
    from . import store

    runs = store.list_runs()
    return {"runs": runs[-30:], "total": len(runs)}
