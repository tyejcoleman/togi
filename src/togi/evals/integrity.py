"""Integrity (EVAL-SPEC section 8): a benchmark is only worth its hashes.

  lock          `togi eval lock <suite>` hashes every item (all splits) plus suite.toml and the data tree, and records a
                pre-registration: outcome and gates, subjects, repeats, the splits counted and the analysis plan.
                `togi eval run` refuses a locked suite whose hash changed (suite_changed_since_lock), and reports any
                deviation from the registered plan in the run manifest.
  unseen        the `unseen` split is not even opened unless a run gets --include-unseen (items.load_suite); hone, examples
                and skills tooling ask for their own purpose and never get it. Every run records which splits it touched.
  canaries      every item (or its suite) carries a canary string; `scan_contamination` looks for canaries and for word
                n-gram overlap with test/unseen items in skills, examples, hone proposals and any training-data directory.
  reference     see analysis.reference_sanity: a reference subject that fails an item flags the item suspect.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from .. import paths
from ..errors import TogiError
from .canon import canonical, file_sha256, hash_obj, sha256_text
from .items import PURPOSE_SPLITS, SPLITS, Suite, load_suite

LOCK_FILE = "LOCK.json"
ANALYSIS_PLAN = {"confidence": 0.95, "intervals": "Wilson score", "paired_test": "exact McNemar on item-level wins",
                 "bootstrap": "paired, item-clustered percentile", "bootstrap_resamples": 10000, "bootstrap_seed": 20261027,
                 "verdict": "worst slice, Wilson lower bound", "unit": "item x repeat cells"}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".jsonl", ".toml", ".py", ".patch", ".diff", ".csv", ".yaml", ".yml", ".rst", ".cfg"}
MAX_SCAN_BYTES = 2_000_000


# --------------------------------------------------------------------------- hashes + lock


def data_hashes(suite_path: Path) -> dict[str, str]:
    d = suite_path / "data"
    out: dict[str, str] = {}
    if d.is_dir():
        for f in sorted(d.rglob("*")):
            if f.is_file():
                out[f.relative_to(suite_path).as_posix()] = file_sha256(f) or ""
    return out


def suite_hashes(suite: Suite) -> dict:
    items = {i.id: i.hash(suite.path) for i in sorted(suite.items, key=lambda i: i.id)}
    files = {"suite.toml": file_sha256(suite.path / "suite.toml") or "", **data_hashes(suite.path)}
    return {"items": items, "files": files, "suite_hash": hash_obj({"items": items, "files": files})}


def load_all(path: str | Path) -> Suite:
    return load_suite(path, purpose="lock")


def read_lock(path: Path) -> dict | None:
    try:
        return json.loads((Path(path) / LOCK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def diff_hashes(old: dict, new: dict) -> dict:
    oi, ni = old.get("items", {}), new.get("items", {})
    files_old, files_new = old.get("files", {}), new.get("files", {})
    return {"changed": sorted(i for i in oi if i in ni and oi[i] != ni[i]), "added": sorted(set(ni) - set(oi)),
            "removed": sorted(set(oi) - set(ni)),
            "files": sorted(f for f in set(files_old) | set(files_new) if files_old.get(f) != files_new.get(f))}


def check_lock(path: str | Path) -> dict:
    """State of a suite's lock: {"locked": False} or {"locked": True, "ok": bool, "diff": ..., "record": ...}."""
    path = Path(path).expanduser().resolve()
    rec = read_lock(path)
    if rec is None:
        return {"locked": False, "ok": True}
    cur = suite_hashes(load_all(path))
    ok = cur["suite_hash"] == rec.get("suite_hash")
    return {"locked": True, "ok": ok, "diff": None if ok else diff_hashes(rec, cur), "record": rec, "current": cur["suite_hash"]}


def enforce_lock(path: str | Path) -> dict:
    """Raise `suite_changed_since_lock` if the suite was changed after it was locked; returns the check result."""
    st = check_lock(path)
    if st["locked"] and not st["ok"]:
        d = st["diff"]
        bits = [f"{len(d[k])} {k}" for k in ("changed", "added", "removed", "files") if d[k]]
        raise TogiError("suite_changed_since_lock",
                        f"the suite changed since it was locked ({', '.join(bits)}: "
                        f"{', '.join((d['changed'] + d['added'] + d['removed'] + d['files'])[:4])}); a locked suite is the "
                        "pre-registered benchmark and does not run changed", status="refused",
                        next=f"review the change; to accept it on purpose: togi eval lock {path} --relock")
    return st


def lock_suite(path: str | Path, *, outcome: Any = None, subjects: list[Any] | None = None, repeats: int | None = None,
               relock: bool = False, analysis: dict | None = None) -> dict:
    path = Path(path).expanduser().resolve()
    suite = load_all(path)
    from .items import validate_suite

    problems = validate_suite(suite)
    if problems:
        raise TogiError("suite_invalid", f"{len(problems)} problem(s) in the suite; first: {problems[0]}",
                        next="fix the items (togi eval items shows suspect ones after a run), then lock")
    hashes = suite_hashes(suite)
    prior = read_lock(path)
    prereg = {"outcome": ({"name": outcome.name, "hash": outcome.hash, "gates": outcome.gates.to_dict(), "splits": outcome.splits,
                           "references": outcome.references} if outcome is not None else None),
              "subjects": [{"id": s.id, "hash": s.hash} for s in (subjects or [])], "repeats": repeats,
              "splits": sorted({i.split for i in suite.items}), "analysis": analysis or ANALYSIS_PLAN}
    if prior and not relock:
        same = prior.get("suite_hash") == hashes["suite_hash"] and prior.get("preregistration") == prereg
        if same:
            return {**prior, "unchanged": True}
        raise TogiError("already_locked", "this suite is already locked" + ("" if prior.get("suite_hash") == hashes["suite_hash"]
                                                                              else " and has changed since"),
                        status="refused", next=f"togi eval lock {path} --relock   (records a new lock; the old one stays in its history)")
    rec = {"version": 1, "suite": suite.name, "locked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **hashes,
           "preregistration": prereg,
           "history": ((prior or {}).get("history", []) + ([{"locked_at": prior["locked_at"], "suite_hash": prior["suite_hash"]}] if prior else []))}
    rec["record_hash"] = hash_obj({k: v for k, v in rec.items() if k != "record_hash"})
    (path / LOCK_FILE).write_text(json.dumps(rec, indent=1), encoding="utf-8")
    return rec


def preregistration_deviations(lock: dict | None, *, subjects: list[Any], repeats: int, outcome: Any, splits: list[str]) -> list[str]:
    """How a run differs from what was pre-registered at lock time (empty = it matches, or nothing was registered)."""
    if not lock:
        return []
    pre = lock.get("preregistration") or {}
    out = []
    if pre.get("outcome") and outcome is not None and pre["outcome"].get("hash") != outcome.hash:
        out.append(f"outcome {pre['outcome'].get('name')} differs from the registered one (its gates or splits changed)")
    if pre.get("subjects"):
        reg, now = {s["hash"] for s in pre["subjects"]}, {s.hash for s in subjects}
        if now - reg:
            out.append(f"subjects not pre-registered: {', '.join(s.id for s in subjects if s.hash in now - reg)}")
        if reg - now:
            out.append(f"{len(reg - now)} pre-registered subject(s) not run")
    if pre.get("repeats") is not None and pre["repeats"] != repeats:
        out.append(f"repeats {repeats} (registered {pre['repeats']})")
    return out


# --------------------------------------------------------------------------- unseen access control


def guard_splits(splits: list[str] | set[str], purpose: str) -> None:
    """Raise if `purpose` would see a split it may not (the backstop for tooling that is handed items from elsewhere)."""
    allowed = set(PURPOSE_SPLITS.get(purpose, ()))
    bad = sorted(set(splits) - allowed)
    if bad:
        raise TogiError("split_access_denied", f"{purpose} may not read the {', '.join(bad)} split", status="refused",
                        next="the unseen split is only run with --include-unseen and is never shown to hone, examples or skills")


def visible_items(path: str | Path, purpose: str) -> Suite:
    """A suite as `purpose` may see it (unseen files are not opened for hone / examples / skills / apply)."""
    suite = load_suite(path, purpose=purpose)
    guard_splits({i.split for i in suite.items}, purpose)
    return suite


# --------------------------------------------------------------------------- canaries + contamination


def make_canary(*parts: Any) -> str:
    return "TOGI-EVAL-CANARY-" + hashlib.sha256(canonical(list(parts)).encode()).hexdigest()[:24]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def _files(source: Path) -> list[Path]:
    if source.is_file():
        return [source]
    if not source.is_dir():
        return []
    return [f for f in sorted(source.rglob("*")) if f.is_file() and f.suffix.lower() in TEXT_SUFFIXES
            and "__pycache__" not in f.parts and ".git" not in f.parts]


def default_sources(suite: Suite) -> list[tuple[str, Path]]:
    """(label, path) for what a bundle's own tooling edits and reads: its skills, examples and polish/reshape files, plus
    hone's proposals in this home."""
    from .. import registry
    from ..manifest import load_manifest, walk_files

    out: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for b in sorted({b for i in suite.items for b in i.bundles} | set(suite.default_bundles)):
        try:
            repo = suite.resolve_bundle(b)
            man = load_manifest(repo)
        except Exception:
            continue
        for label, sub in (("skills", man.skills), ("examples", "examples")):
            d = repo / sub
            if d.exists() and d not in seen:
                seen.add(d)
                out.append((f"{b}:{label}", d))
        for rel in walk_files(repo):
            if man.classify(rel) in ("polish", "reshape") and (repo / rel) not in seen and not rel.startswith(man.skills + "/"):
                seen.add(repo / rel)
                out.append((f"{b}:{man.classify(rel)}", repo / rel))
    props = paths.proposals_dir()
    if props.is_dir():
        for d in sorted(props.iterdir()):
            for name in ("diff.patch", "rationale.md"):
                if (d / name).is_file():
                    out.append(("hone-proposal", d / name))
    return out


def item_phrases(item: Any) -> list[str]:
    """The text of an item that must not appear in anything the subject can learn from: what it is asked."""
    return [s.instruction for s in item.sessions if s.instruction and s.instruction != "Continue the task."]


def scan_contamination(suite: Suite, sources: list[tuple[str, Path]], *, ngram: int = 8,
                       protected: tuple[str, ...] = ("test", "unseen")) -> dict:
    canaries: dict[str, list[str]] = {}
    for i in suite.items:
        for c in {i.canary, suite.canary}:
            if c:
                canaries.setdefault(c, []).append(i.id)
    grams: dict[tuple[str, ...], set[str]] = {}
    short: dict[str, str] = {}
    for i in suite.items:
        if i.split not in protected:
            continue
        for phrase in item_phrases(i):
            w = _words(phrase)
            if len(w) >= ngram:
                for g in _ngrams(w, ngram):
                    grams.setdefault(g, set()).add(i.id)
            elif len(w) >= 3:
                short[" ".join(w)] = i.id
    hits: list[dict] = []
    scanned = 0
    for label, src in sources:
        for f in _files(src):
            try:
                if f.stat().st_size > MAX_SCAN_BYTES:
                    continue
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            for c, ids in canaries.items():
                if c in text:
                    hits.append({"kind": "canary", "source": label, "file": str(f), "canary": c, "items": sorted(set(ids))[:5]})
            words = _words(text)
            found: dict[str, int] = {}
            for g in _ngrams(words, ngram) & set(grams):
                for iid in grams[g]:
                    found[iid] = found.get(iid, 0) + 1
            joined = " ".join(words)
            for phrase, iid in short.items():
                if f" {phrase} " in f" {joined} ":
                    found[iid] = found.get(iid, 0) + 1
            for iid, n in sorted(found.items()):
                hits.append({"kind": "ngram", "source": label, "file": str(f), "item": iid, "overlapping_ngrams": n})
    return {"clean": not hits, "hits": hits, "files_scanned": scanned, "canaries": len(canaries), "ngram": ngram,
            "protected_items": sum(1 for i in suite.items if i.split in protected)}
