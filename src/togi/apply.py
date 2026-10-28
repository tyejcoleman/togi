"""Proposals, review, apply and revert (SPEC section 6): the guarded half of the self-improvement loop.

apply: re-check locked hashes -> may this proposal be evaluated at all? -> candidate copy + diff -> stone on
current AND candidate (same tasks, same model, at least the policy's runs, at least the policy's driver) -> decide
by tier and policy -> write the real files -> re-verify the locked hashes (roll back if any moved) -> append to the
hash-chained audit log.

Evaluating a proposal means running the stone on a copy of the repo with the diff applied, i.e. EXECUTING code the
agent wrote. So before a person has seen it nothing that can carry code is evaluated:
  - a polish proposal may only touch non-code files (no .py/.pyc/.so/.sh/.js/.ts, nothing starting with `#!`);
    it is evaluated automatically, as before;
  - a reshape proposal is not evaluated until a person has passed the human gate (terminal + typed word +
    approver passphrase) FOR EVALUATION, shown the diff itself. That decision is signed with the approver key, bound
    to this diff and the base files it applies to, stored in the proposal's meta.json and written to the audit log
    (`evaluate`). Applying it afterwards is a second, separate human decision.

Both human decisions (and `revert`) need the DEFAULT-scope approver: a throwaway approver (TOGI_APPROVER_HOME) is
refused (`test_scope_refused`), and a recorded evaluation approval only counts if it was signed in the default scope.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import approvals, audit, output, paths, patch, stone
from .cli import Reply, parse_flags, _mode, _usage
from .errors import TogiError
from .journal import now_ts
from .manifest import APPLY_DRIVERS, MIN_APPLY_RUNS, TIER_ORDER, Manifest, load_manifest, walk_files
from .pathnorm import escape_text, fold

# A polish proposal is evaluated AUTOMATICALLY, so it may only touch plain text. The rule is an ALLOWLIST, not a list of
# bad extensions: anything not in it counts as code for this purpose (an unknown or missing extension, `.cfg`, `.ini`,
# `.html`, `.pth`, a `Makefile`...). `.toml` is text except the two files that configure a build or Togi itself. Content
# that starts with a shebang is code whatever its name. A reshape proposal's code is only evaluated after a person
# approved the evaluation.
TEXT_EXTENSIONS = (".md", ".txt", ".toml", ".json", ".yaml", ".yml")
NOT_TEXT_NAMES = ("pyproject.toml", "togi.toml")
MAX_REVIEW_DIFF_LINES = 1000   # a person approves what they were shown: a diff too long to show is not approvable here
MIN_RUNS = MIN_APPLY_RUNS
NOISE_PCT = 15.0  # median turns / tokens / cost must improve by at least this much ...
NOISE_TASKS = 1.0
EVAL_SCHEMA = 3   # bump when the stone summary shape changes: invalidates cached evaluations


# --------------------------------------------------------------------------- proposal store


def proposal_dir(pid: str) -> Path:
    if not pid.replace("_", "").isalnum():
        raise TogiError("proposal_not_found", f"'{pid}' is not a proposal id", next="togi review")
    return paths.proposals_dir() / pid


def new_id() -> str:
    return f"p_{time.strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(2)}"


def save_proposal(meta: dict, diff: str, rationale: str) -> Path:
    d = proposal_dir(meta["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "diff.patch").write_text(diff, encoding="utf-8")
    (d / "rationale.md").write_text(rationale, encoding="utf-8")
    save_meta(meta)
    return d


def save_meta(meta: dict) -> None:
    (proposal_dir(meta["id"]) / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True), encoding="utf-8")


def load_proposal(pid: str) -> tuple[dict, str, str]:
    d = proposal_dir(pid)
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        diff = (d / "diff.patch").read_text(encoding="utf-8")
    except (OSError, ValueError):
        raise TogiError("proposal_not_found", f"no proposal '{pid}'", next="togi review")
    rationale = (d / "rationale.md").read_text(encoding="utf-8") if (d / "rationale.md").is_file() else ""
    return meta, diff, rationale


def list_proposals() -> list[dict]:
    d = paths.proposals_dir()
    rows = []
    for m in sorted(d.glob("p_*/meta.json")) if d.is_dir() else []:
        try:
            meta = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rows.append({k: meta.get(k) for k in ("id", "bundle", "tier", "status", "files", "created")})
    return rows


def diff_sha(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- the evaluation cache is signed
#
# `proposals/<id>/evaluation.json` holds the stone's before/after comparison that `decide` reads. Whoever can write
# $TOGI_HOME could put a flattering comparison there and have a polish proposal auto-apply on it, so the cache is
# MAC'd with the audit key (the one that signs the audit log) and an unsigned, edited or foreign one is ignored: the
# stone simply runs again. The MAC covers the proposal id and everything in the record.

EVAL_DOMAIN = "togi-eval-v1\n"


def _eval_blob(pid: str, ev: dict) -> bytes:
    body = {"proposal": pid, "evaluation": {k: v for k, v in ev.items() if k != "mac"}}
    return (EVAL_DOMAIN + json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                                     default=str)).encode("ascii")


def sign_evaluation(pid: str, ev: dict) -> str:
    return hmac.new(audit.ensure_key(), _eval_blob(pid, ev), hashlib.sha256).hexdigest()


def evaluation_signed(pid: str, ev: object) -> bool:
    """True only for a cache record signed with THIS machine's audit key for THIS proposal. Never raises."""
    if not isinstance(ev, dict) or not isinstance(ev.get("mac"), str):
        return False
    try:
        key = audit.load_key()
    except TogiError:
        return False
    if key is None:
        return False
    return hmac.compare_digest(ev["mac"], hmac.new(key, _eval_blob(pid, ev), hashlib.sha256).hexdigest())


def read_evaluation(pid: str) -> dict | None:
    """The cached evaluation of a proposal if it is present AND signed; None otherwise (re-run the stone)."""
    cache = proposal_dir(pid) / "evaluation.json"
    try:
        ev = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return ev if evaluation_signed(pid, ev) else None


# --------------------------------------------------------------------------- classification


@dataclass
class Classified:
    patches: list[patch.FilePatch]
    tiers: dict[str, str | None]
    tier: str            # highest tier among the touched files ("locked" if any is locked/untiered/outside)
    violation: str | None  # reason code when the proposal must be rejected outright


def _stricter(a: str | None, b: str | None) -> str | None:
    """The more restrictive of two tiers: locked beats untiered beats reshape beats polish."""
    if "locked" in (a, b):
        return "locked"
    if a is None or b is None:
        return None
    return a if TIER_ORDER[a] >= TIER_ORDER[b] else b


def classify_diff(diff: str, man: Manifest) -> Classified:
    patches = patch.parse(diff)  # PatchError propagates to the caller
    tiers: dict[str, str | None] = {}
    violation = None
    root = man.repo.resolve()
    for fp in patches:
        try:
            rel = patch.safe_relpath(fp.path, man.repo)
        except patch.BadPathError as e:   # outside the repo, control characters, line separators...
            tiers[escape_text(fp.path, 120)] = None
            violation = violation or e.code
            continue
        fp.path = rel
        tier = man.classify(rel)
        # The name must also mean the same file after symlinks are followed: a polish-looking name that is a
        # symlink (or sits under a symlinked directory) onto a locked file is judged by what it really is.
        target = man.repo / rel
        try:
            real_rel = target.resolve().relative_to(root).as_posix()
        except ValueError:
            tiers[rel] = None
            violation = violation or "outside_repo"
            continue
        if fold(real_rel) != fold(rel) or target.is_symlink():
            tier = _stricter(tier, man.classify(real_rel))
            violation = violation or "symlink"
        tiers[rel] = tier
        if tier == "locked":
            violation = violation or "locked_file"
        elif tier is None:
            violation = violation or "untiered_file"
        elif fp.is_delete:
            violation = violation or "deletes_not_supported"
    known = [t for t in tiers.values() if t]
    top = max(known, key=lambda t: TIER_ORDER[t]) if known else "locked"
    if violation:
        top = "locked"
    return Classified(patches, tiers, top, violation)


CODE_IN_POLISH_TEXT = ("a polish proposal may only touch plain-text files (.md, .txt, .toml other than pyproject.toml / "
                       "togi.toml, .json, .yaml, .yml); anything else counts as code, as does a file starting with #!")

VIOLATION_TEXT = {
    "locked_file": "the diff touches a locked file",
    "outside_repo": "the diff touches a path outside the bundle repo",
    "untiered_file": "the diff touches a file that is in no tier (only polish/reshape files may change)",
    "deletes_not_supported": "deleting files is not supported",
    "bad_path": "the diff names a path with control, format or line-separator characters",
    "symlink": "the diff touches a path through a symlink",
}


def plan_changes(repo: Path, cl: Classified) -> dict[str, str]:
    """New file contents (in memory) after applying the diff to `repo`. Raises PatchError."""
    out = {}
    for fp in cl.patches:
        f = repo / fp.path
        if fp.is_new:
            if f.exists():
                raise patch.PatchError(f"{fp.path}: the diff creates this file but it already exists")
            base = None
        else:
            if not f.is_file():
                raise patch.PatchError(f"{fp.path}: file does not exist")
            base = f.read_text(encoding="utf-8")
        out[fp.path] = patch.apply_to_text(base, fp)
    return out


# --------------------------------------------------------------------------- decision rules


@dataclass
class Decision:
    action: str          # apply | reject | needs_human
    decided_by: str
    reason: str
    gates: dict


def _pct_drop(before: float | None, after: float | None) -> float:
    if before in (None, 0) or after is None:
        return 0.0
    return (before - after) / before * 100.0


def _rate(g: dict) -> float | None:
    """Task success rate where a skipped run counts as a failed one (falls back to `rate` for hand-made input)."""
    return g.get("rate_strict", g.get("rate"))


def _mass(g: dict) -> float:
    return g.get("task_mass_strict", g.get("task_mass")) or 0


METRICS = (("turns", "median_turns", "max_turns"), ("tokens", "median_tokens", "max_tokens"),
           ("cost", "median_cost_usd", "max_cost_usd"))


def decide(tier: str | None, auto_polish: bool, before: dict | None, after: dict | None, *,
           touches_skill: bool = False, driver: str = "claude") -> Decision:
    """The apply rules (SPEC 6.3). Pure: summaries in, decision out.

    A turns/tokens/cost improvement only counts at >= NOISE_PCT median improvement AND when the candidate's
    worst run is no worse than the baseline's median; otherwise at least one more task must pass. Skipped tasks
    count as failures. A proposal touching a skill file cannot auto-apply on a script-driver evaluation."""
    if tier in (None, "locked"):
        return Decision("reject", "policy", "locked_file", {})
    b, a = before or {}, after or {}
    gates: dict[str, Any] = {}
    a_tests = a.get("tests")
    gates["tests_100"] = True if a_tests is None else bool(a_tests.get("ok"))
    gates["forbidden_0"] = (a.get("forbidden") or 0) == 0
    bh, ah = _rate(b.get("holdout") or {}), _rate(a.get("holdout") or {})
    gates["holdout_not_dropped"] = True if (bh is None or ah is None) else ah >= bh
    bn, an = _rate(b.get("nonholdout") or {}), _rate(a.get("nonholdout") or {})
    gates["tasks_not_regressed"] = True if (bn is None or an is None) else an >= bn
    gates["no_new_driver_errors"] = (a.get("driver_errors") or 0) <= (b.get("driver_errors") or 0)
    failed = [k for k, v in gates.items() if not v]
    if failed:
        return Decision("reject", "policy", failed[0], gates)
    mass_gain = sum(_mass(a.get(k) or {}) - _mass(b.get(k) or {}) for k in ("nonholdout", "holdout"))
    imp: dict[str, Any] = {"tasks_gained": round(mass_gain, 4)}
    counted = False
    for label, med, worst in METRICS:
        pct = _pct_drop(b.get(med), a.get(med))
        worst_ok = a.get(worst) is not None and b.get(med) is not None and a[worst] <= b[med]
        imp[f"median_{label}_down_pct"] = round(pct, 1)
        imp[f"{label}_worst_run_ok"] = bool(worst_ok)
        if pct >= NOISE_PCT and worst_ok:
            counted = True
    gates["improvement"] = imp
    improved = mass_gain >= NOISE_TASKS - 1e-9 or counted
    if tier == "reshape":
        return Decision("needs_human", "human", "reshape_needs_human", gates)
    if not auto_polish:
        return Decision("needs_human", "human", "auto_polish_disabled", gates)
    if improved and touches_skill and driver != "claude":
        # skills are text an agent will obey; a deterministic replay cannot judge how an agent reads them
        return Decision("needs_human", "human", "skill_edit_needs_agent_driver", gates)
    if improved:
        return Decision("apply", "auto-polish", "improved: " + _improvement_text(imp), gates)
    return Decision("reject", "policy", "no_measurable_improvement", gates)


def _improvement_text(imp: dict) -> str:
    bits = []
    if imp["tasks_gained"] >= NOISE_TASKS - 1e-9:
        bits.append(f"+{imp['tasks_gained']:g} task(s)")
    for label in ("turns", "tokens", "cost"):
        if imp[f"median_{label}_down_pct"] >= NOISE_PCT and imp[f"{label}_worst_run_ok"]:
            bits.append(f"median {label} -{imp[f'median_{label}_down_pct']:g}% (worst run no worse than the old median)")
    return ", ".join(bits) or "no change"


def compact(summary: dict | None, run_id: str | None = None) -> dict | None:
    if summary is None:
        return None
    s = {k: summary.get(k) for k in ("tests", "nonholdout", "holdout", "forbidden", "median_turns",
                                     "median_tokens", "median_cost_usd", "max_turns", "max_tokens", "max_cost_usd",
                                     "driver_errors", "skipped")}
    if run_id:
        s["run"] = run_id
    return s


# --------------------------------------------------------------------------- candidate copy


def copy_repo(repo: Path, dest: Path) -> None:
    ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache", "node_modules", ".venv", "venv",
                                    "*.egg-info", ".mypy_cache", ".ruff_cache", ".DS_Store")
    shutil.copytree(repo, dest, ignore=ignore, symlinks=True)


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def write_changes(repo: Path, changes: dict[str, str]) -> None:
    root = repo.resolve()
    for rel, content in changes.items():
        f = repo / rel
        if f.is_symlink() or not _within(f.parent.resolve(), root):
            raise TogiError("symlink", f"{escape_text(rel, 120)} is, or sits under, a symlink; nothing was written",
                            next="review the proposal with: togi review", status="refused")
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f".{f.name}.togi-tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(f)


def snapshot(repo: Path, rels) -> dict[str, tuple[bytes | None, int | None]]:
    """Bytes + mode of each file before a write (None = did not exist), to roll back with."""
    snap: dict[str, tuple[bytes | None, int | None]] = {}
    for rel in rels:
        f = repo / rel
        try:
            snap[rel] = (f.read_bytes(), f.stat().st_mode & 0o7777)
        except OSError:
            snap[rel] = (None, None)
    return snap


def restore(repo: Path, snap: dict[str, tuple[bytes | None, int | None]]) -> None:
    root = repo.resolve()
    for rel, (data, mode) in snap.items():
        f = repo / rel
        try:
            if data is None:
                f.unlink(missing_ok=True)
                d = f.parent
                while d.resolve() != root and not any(d.iterdir()):   # directories the write created
                    d.rmdir()
                    d = d.parent
            else:
                tmp = f.with_name(f".{f.name}.togi-restore")
                tmp.write_bytes(data)
                if mode is not None:
                    tmp.chmod(mode)
                tmp.replace(f)
        except OSError:
            pass


def write_verified(repo: Path, man: Manifest, changes: dict[str, str]) -> dict | None:
    """Write `changes`, then re-verify the locked hashes. If any locked file moved (a path trick the classifier
    missed), roll the write back and return the verification result; None means all good."""
    snap = snapshot(repo, changes)
    try:
        write_changes(repo, changes)
    except BaseException:
        restore(repo, snap)
        raise
    after = audit.verify_locked(repo, man)
    if after["ok"]:
        return None
    restore(repo, snap)
    return after


# --------------------------------------------------------------------------- may this proposal be evaluated?


def is_code_path(rel: str) -> bool:
    """True unless `rel` is a plain-text type on the allowlist (TEXT_EXTENSIONS, minus pyproject.toml / togi.toml)."""
    f = fold(rel)
    base = f.rsplit("/", 1)[-1]
    return not f.endswith(TEXT_EXTENSIONS) or base in NOT_TEXT_NAMES


def code_bearing_files(plan: dict[str, str]) -> list[str]:
    """Touched files that can carry code: any that is not an allowlisted text type, or whose new content starts with
    a shebang."""
    return sorted(rel for rel, text in plan.items() if is_code_path(rel) or text.startswith("#!"))


def may_evaluate(tier: str | None, code_files: list[str], evaluation_approved: bool) -> str | None:
    """None if the proposal may be evaluated, else why not. The rule, in one place (and checked again right before
    the stone runs): code-bearing files only in a reshape proposal whose evaluation a person approved, and a reshape
    proposal is not evaluated at all until a person approved it."""
    if tier == "reshape":
        return None if evaluation_approved else "evaluation_needs_human"
    if tier == "polish":
        return "code_in_polish" if code_files else None
    return "locked_file"


def _eval_fields(pid: str, bundle: str, repo: Path, sha: str, base_hashes: dict, by: object, at: object) -> dict:
    return {"proposal": pid, "bundle": bundle, "repo": str(repo), "diff_sha256": sha, "base": base_hashes,
            "by": by, "at": at}


def evaluation_approved(meta: dict, pid: str, bundle: str, repo: Path, sha: str, base_hashes: dict) -> bool:
    """A person approved running THIS diff on THESE base files: the record in meta.json is signed by the approver
    key, so writing `evaluation_approval` into meta.json by hand proves nothing."""
    ea = meta.get("evaluation_approval")
    if not isinstance(ea, dict) or ea.get("scope") != "default":
        return False       # a test-scope "approval" is anyone's to forge (the gate refuses test scope), so it counts for nothing
    return approvals.blob_valid("evaluate", _eval_fields(pid, bundle, repo, sha, base_hashes, ea.get("by"), ea.get("at")),
                                ea.get("scope"), ea.get("mac"))


def _approve_evaluation(pid: str, meta: dict, man: Manifest, cl: Classified, files: list[str], diff: str,
                        rationale: str, sha: str, base_hashes: dict) -> str:
    """The human gate for EVALUATION of a reshape proposal: the person is shown the diff (its code is about to run),
    types the word and the approver passphrase. Recorded (signed) in meta.json and in the audit log."""
    approvals.human_repo_gate("apply")
    approvals.require_approver()
    diff_lines = diff.splitlines()
    if len(diff_lines) > MAX_REVIEW_DIFF_LINES:
        raise TogiError("diff_too_large_to_review", f"proposal {pid} has {len(diff_lines)} diff lines; one this long "
                                                    f"cannot be shown in full at the prompt (limit "
                                                    f"{MAX_REVIEW_DIFF_LINES}), so its evaluation cannot be approved",
                        next="ask hone for a smaller change", status="refused")
    lines = [f"Evaluate proposal {escape_text(pid, 60)}  tier={cl.tier}: its code will RUN in the stone",
             f"bundle: {escape_text(man.name, 60)}   repo: {escape_text(man.repo, 200)}",
             "files: " + ", ".join(f"{escape_text(f, 120)} [{cl.tiers[f]}]" for f in files),
             "rationale: " + escape_text(rationale.strip(), 600), "diff:"]
    lines += ["  " + escape_text(ln, 400) for ln in diff_lines]
    lines.append("Approving copies the repo, applies this diff to the copy and runs its tests and tasks on this "
                 "machine. Applying it for real is a separate decision afterwards.")
    user = approvals.confirm_and_authenticate(lines, pid.split("_")[-1], "run this change's code in the stone",
                                              "apply evaluate")
    by, at = f"human:{user}", now_ts()
    mac = approvals.sign_blob("evaluate", _eval_fields(pid, man.name, man.repo, sha, base_hashes, by, at))
    entry = audit.append(man.repo, man.name, action="evaluate", proposal=pid, tier=cl.tier, files=files,
                         diff_sha256=sha, stone_before=None, stone_after=None, decided_by=by,
                         reason="evaluation approved: the candidate's code may run in the stone")
    meta["evaluation_approval"] = {"by": by, "at": at, "scope": paths.approver_scope(),
                                   "diff_sha256": sha, "base": base_hashes, "audit_seq": entry["seq"], "mac": mac}
    save_meta(meta)
    return user


# --------------------------------------------------------------------------- apply


def is_skill_file(rel: str, man: Manifest) -> bool:
    """Files under the bundle's skills directory: text an agent will read and obey."""
    d = fold(man.skills.strip("/"))
    return bool(d) and fold(rel).startswith(d + "/")


def effective_policy(man: Manifest, driver: str | None, runs: int | None) -> tuple[str, int]:
    """Driver and runs `apply` will use: the manifest's `apply_driver` / `apply_runs` are minimums. A caller may
    raise them (claude over script, more runs), never lower them. A lower driver is refused; fewer runs are
    raised to the minimum."""
    rank = {d: i for i, d in enumerate(APPLY_DRIVERS)}
    if driver is not None and rank[driver] < rank[man.apply_driver]:
        raise TogiError("apply_driver_below_policy",
                        f"{man.name}'s policy requires the {man.apply_driver!r} driver for apply; "
                        f"{driver!r} is below that minimum", next=f"togi apply <id> --driver {man.apply_driver}",
                        status="refused")
    return (driver or man.apply_driver), max(man.apply_runs, MIN_RUNS, runs or 0)


def apply_proposal(pid: str, *, runs: int | None = None, driver: str | None = None, model: str | None = None,
                   surface: str = "cli", dry_run: bool = False, task_timeout: float = 600,
                   interactive: bool = True, outcome: dict | None = None) -> dict:
    """`outcome` = {"outcome": file, "suite": dir, "subjects": spec, "repeats": n} adds one gate: the candidate must hold the
    outcome's gates on the suite's TEST split (togi.evals.integration.outcome_gate; the unseen split is never run here)."""
    audit.refuse_agents("apply")
    meta, diff, rationale = load_proposal(pid)
    if meta.get("status") in ("applied", "rejected", "reverted"):
        raise TogiError("proposal_closed", f"proposal {pid} is already {meta['status']}", next="togi review")
    repo = Path(meta["repo"])
    man = load_manifest(repo)
    bundle = man.name
    sha = diff_sha(diff)
    try:
        cl = classify_diff(diff, man)
    except patch.PatchError as e:
        return _reject(meta, man, None, sha, "bad_diff", str(e), dry_run=dry_run, tier=None)
    files = sorted(cl.tiers)
    if cl.violation:
        return _reject(meta, man, cl, sha, cl.violation, VIOLATION_TEXT[cl.violation], dry_run=dry_run)
    if meta.get("diff_sha256") and meta["diff_sha256"] != sha:
        return _reject(meta, man, cl, sha, "diff_modified", "the proposal's diff changed after hone stored it",
                       dry_run=dry_run)
    audit.require_trusted_chain(repo, bundle)   # intact AND authenticated by this machine's key (L1)
    lock = audit.verify_locked(repo, man)
    if not lock["locked_file_present"]:
        raise TogiError("not_locked", "this bundle has no .togi/locked.sha256, so locked files cannot be enforced",
                        next=f"togi lock {bundle}")
    if not lock["ok"]:
        bad = audit.lock_problems(lock)[:5] or [lock.get("problem", "the lock is not anchored")]
        return _reject(meta, man, cl, sha, "locked_hash_mismatch",
                       "locked files differ from .togi/locked.sha256: " + ", ".join(bad), dry_run=dry_run,
                       raise_instead=True, next=f"review the change, then: togi lock {bundle}")
    try:
        plan = plan_changes(repo, cl)
    except patch.PatchError as e:
        return _reject(meta, man, cl, sha, "patch_does_not_apply", str(e), dry_run=dry_run)
    base_hashes = {rel: (audit.file_sha256(repo / rel) if (repo / rel).is_file() else None) for rel in plan}
    touches_skill = any(is_skill_file(rel, man) for rel in plan)

    # ---- policy minimums
    driver, runs = effective_policy(man, driver, runs)
    if interactive and not dry_run and (cl.tier == "reshape" or not man.auto_polish):
        approvals.require_default_scope("apply")   # a person will be needed, and only the default approver counts:
        approvals.require_approver()               # say so before anything slow
    # ---- may this be evaluated at all? Evaluating EXECUTES the candidate, so code needs a person first
    code_files = code_bearing_files(plan)
    approved = cl.tier == "reshape" and evaluation_approved(meta, pid, bundle, repo, sha, base_hashes)
    block = may_evaluate(cl.tier, code_files, approved)
    if block == "code_in_polish":
        return _reject(meta, man, cl, sha, block, CODE_IN_POLISH_TEXT + ": " + ", ".join(
            escape_text(f, 120) for f in code_files), dry_run=dry_run)
    if block == "evaluation_needs_human":
        if dry_run or not interactive:     # nothing runs: not even a dry run, not even a cached comparison
            return _needs_evaluation_approval(meta, pid, bundle, cl, files, dry_run)
        _approve_evaluation(pid, meta, man, cl, files, diff, rationale, sha, base_hashes)
        approved = True
        block = may_evaluate(cl.tier, code_files, approved)
    if block:   # belt and braces: whatever the branches above did, the stone does not run unless this says so
        raise TogiError("evaluation_refused", f"proposal {pid} may not be evaluated ({block})",
                        next="togi review", status="refused")
    # ---- evaluate current vs candidate (cached per diff+repo state+settings)
    key = _eval_key(repo, sha, driver, model, runs, surface, json.dumps(outcome, sort_keys=True) if outcome else "")
    cache = proposal_dir(pid) / "evaluation.json"
    ev = None
    cached = read_evaluation(pid)      # only a record signed by this machine's key; anything else is ignored
    if cached is not None and cached.get("key") == key:
        ev = cached
    if ev is None:
        before_res = stone.run_stone(repo, driver=driver, model=model, runs=runs, surface=surface,
                                     task_timeout=task_timeout, label=f"{pid}-before")
        with tempfile.TemporaryDirectory(prefix="togi-candidate-") as tmp:
            cand = Path(tmp) / repo.name
            copy_repo(repo, cand)
            write_changes(cand, plan)
            after_res = stone.run_stone(cand, driver=driver, model=model, runs=runs, surface=surface,
                                        task_timeout=task_timeout, save=False, label=f"{pid}-after")
            after_res["repo"] = f"candidate of {repo}"
            d = paths.stone_dir(bundle)
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{after_res['id']}.json").write_text(json.dumps(after_res, indent=1), encoding="utf-8")
            after_res["path"] = str(d / f"{after_res['id']}.json")
            gate = None
            if outcome:
                from .evals import integration

                gate = integration.outcome_gate(repo, cand, outcome, bundle)
        ev = {"key": key, "before": compact(before_res["summary"], before_res["id"]),
              "after": compact(after_res["summary"], after_res["id"]),
              "driver": driver, "model": model, "runs": runs, "surface": surface, "at": now_ts()}
        if outcome:
            ev["outcome_gate"] = gate
        ev["mac"] = sign_evaluation(pid, ev)
        cache.write_text(json.dumps(ev, indent=1), encoding="utf-8")
    dec = decide(cl.tier, man.auto_polish, ev["before"], ev["after"], touches_skill=touches_skill, driver=driver)
    og = ev.get("outcome_gate") if outcome else None
    if og is not None and not og.get("ok") and dec.action != "reject":
        dec = Decision("reject", "policy", "outcome_gate_failed: " + og.get("reason", ""), {**dec.gates, "outcome_gate": og})
    result: dict[str, Any] = {"proposal": pid, "bundle": bundle, "tier": cl.tier, "files": files,
                              "action": dec.action, "decided_by": dec.decided_by, "reason": dec.reason,
                              "stone_before": ev["before"], "stone_after": ev["after"], "gates": dec.gates,
                              "driver": driver, "runs": runs, "dry_run": dry_run}
    meta.update(tier=cl.tier, files=files, evaluated=ev["at"], driver=driver, decision=dec.action)
    if dry_run:
        return result
    if dec.action == "reject":
        entry = audit.append(repo, bundle, action="reject", proposal=pid, tier=cl.tier, files=files,
                             diff_sha256=sha, stone_before=ev["before"], stone_after=ev["after"],
                             decided_by="policy", reason=dec.reason)
        meta.update(status="rejected", audit_seq=entry["seq"])
        save_meta(meta)
        result.update(audit_seq=entry["seq"])
        return result
    decided_by = dec.decided_by
    if dec.action == "needs_human":
        if not interactive:
            meta.update(status="needs_human", awaiting="apply")
            save_meta(meta)
            result["needs_human"] = True
            return result
        user = _human_confirm(pid, man, cl, files, ev, dec)
        decided_by = f"human:{user}"
    # ---- re-check the world did not move under us, then write and re-verify
    for rel, h in base_hashes.items():
        now = audit.file_sha256(repo / rel) if (repo / rel).is_file() else None
        if now != h:
            raise TogiError("repo_changed", f"{escape_text(rel, 120)} changed while the stone was running; nothing "
                                            "was applied", next=f"togi apply {pid}")
    again = audit.verify_locked(repo, man)
    if not again["ok"]:
        raise TogiError("locked_hash_mismatch", "locked files changed while the stone was running; nothing applied",
                        next=f"togi lock {bundle}")
    moved = write_verified(repo, man, plan)
    if moved is not None:
        bad = audit.lock_problems(moved)[:5]
        return _reject(meta, man, cl, sha, "locked_files_changed_by_write",
                       "the write changed locked files and was rolled back: " + ", ".join(bad), dry_run=False,
                       raise_instead=True, next="review the proposal's paths; the classifier and the filesystem "
                                                "disagree about which file a path is")
    entry = audit.append(repo, bundle, action="apply", proposal=pid, tier=cl.tier, files=files, diff_sha256=sha,
                         stone_before=ev["before"], stone_after=ev["after"], decided_by=decided_by,
                         reason=dec.reason)
    meta.update(status="applied", audit_seq=entry["seq"], applied_at=entry["ts"], decided_by=decided_by)
    save_meta(meta)
    result.update(action="apply", decided_by=decided_by, audit_seq=entry["seq"])
    return result


def _needs_evaluation_approval(meta: dict, pid: str, bundle: str, cl: Classified, files: list[str],
                               dry_run: bool) -> dict:
    """A reshape proposal nobody has approved for evaluation: nothing is run; a person has to start it."""
    result = {"proposal": pid, "bundle": bundle, "tier": cl.tier, "files": files, "action": "needs_human",
              "decided_by": "human", "reason": "evaluation_needs_human", "needs_human": True, "evaluated": False,
              "dry_run": dry_run, "message": f"proposal {pid} is tier reshape: evaluating it runs its code, so a person "
                                             "must approve the evaluation first (nothing was run)"}
    if not dry_run:
        meta.update(status="needs_human", awaiting="evaluation_approval", tier=cl.tier, files=files)
        save_meta(meta)
    return result


def _eval_key(repo: Path, sha: str, driver: str, model: str | None, runs: int, surface: str, extra: str = "") -> str:
    h = hashlib.sha256()
    h.update(f"v{EVAL_SCHEMA}|{sha}|{driver}|{model}|{runs}|{surface}".encode())
    if extra:
        h.update(b"|outcome|" + extra.encode())
    for rel in sorted(walk_files(repo)):
        if rel.startswith(".togi/"):
            continue
        h.update(rel.encode("utf-8", "surrogatepass"))
        h.update(audit.file_sha256(repo / rel).encode())
    return h.hexdigest()


def _reject(meta: dict, man: Manifest, cl: Classified | None, sha: str, code: str, message: str, *, dry_run: bool,
            tier: str | None = "locked", raise_instead: bool = False, next: str | None = None,
            close_proposal: bool = True) -> dict:
    tier = cl.tier if cl else tier
    files = sorted(cl.tiers) if cl else []
    result = {"proposal": meta["id"], "bundle": man.name, "tier": tier, "files": files, "action": "reject",
              "decided_by": "policy", "reason": code, "message": message, "dry_run": dry_run}
    if dry_run:
        return result
    entry = audit.append(man.repo, man.name, action="reject", proposal=meta["id"], tier=tier, files=files,
                         diff_sha256=sha, stone_before=None, stone_after=None, decided_by="policy",
                         reason=f"{code}: {message}")
    if close_proposal:   # a refused revert leaves the (applied) proposal as it was
        meta.update(status="rejected", tier=tier, audit_seq=entry["seq"])
        save_meta(meta)
    result["audit_seq"] = entry["seq"]
    if raise_instead:
        raise TogiError(code, message + f" (rejection logged as audit seq {entry['seq']})", next=next, status="refused")
    return result


def _human_confirm(pid: str, man: Manifest, cl: Classified, files: list[str], ev: dict, dec: Decision) -> str:
    approvals.human_repo_gate("apply")
    approvals.require_approver()
    word = pid.split("_")[-1]
    b, a = ev["before"] or {}, ev["after"] or {}
    lines = [f"Proposal {escape_text(pid, 60)}  tier={cl.tier}  ({escape_text(dec.reason, 200)})",
             f"bundle: {escape_text(man.name, 60)}   repo: {escape_text(man.repo, 200)}",
             "files: " + ", ".join(f"{escape_text(f, 120)} [{cl.tiers[f]}]" for f in files),
             f"stone before: tasks {_rate_text(b)}  tests {_tests(b)}",
             f"stone after:  tasks {_rate_text(a)}  tests {_tests(a)}"]
    return approvals.confirm_and_authenticate(lines, word, "apply this change", "apply")


def _rate_text(s: dict) -> str:
    n, h = s.get("nonholdout") or {}, s.get("holdout") or {}
    return f"{n.get('successes', 0)}/{n.get('runs', 0)} + holdout {h.get('successes', 0)}/{h.get('runs', 0)}"


def _tests(s: dict) -> str:
    t = s.get("tests")
    return "n/a" if not t else f"{t['passed']} passed, {t['failed']} failed"


# --------------------------------------------------------------------------- revert


def revert_proposal(pid: str) -> dict:
    """Undo an applied proposal. A person only (terminal + typed word + approver passphrase), and with the same
    checks `apply` makes: a healthy audit chain, the diff on record, tiers, and the locked hashes before AND
    after the write (a mismatch after rolls the write back)."""
    approvals.human_repo_gate("revert")
    meta, diff, _ = load_proposal(pid)
    if meta.get("status") != "applied":
        raise TogiError("not_applied", f"proposal {pid} is {meta.get('status')}, so there is nothing to revert",
                        next="togi review", status="refused")
    repo = Path(meta["repo"])
    man = load_manifest(repo)
    sha = diff_sha(diff)
    audit.require_trusted_chain(repo, man.name, status="refused")
    applied = [e for e in audit.read(repo) if e.get("action") == "apply" and e.get("proposal") == pid]
    if not applied or applied[-1].get("diff_sha256") != sha:
        raise TogiError("proposal_modified", f"the diff stored for {pid} is not the one the audit log says was "
                                             "applied; refusing to write it", next=f"togi log {man.name}",
                        status="refused")
    try:
        cl = classify_diff(diff, man)
    except patch.PatchError as e:
        return _reject(meta, man, None, sha, "bad_diff", str(e), dry_run=False, tier=None, raise_instead=True,
                       close_proposal=False)
    if cl.violation:
        return _reject(meta, man, cl, sha, cl.violation, VIOLATION_TEXT[cl.violation], dry_run=False,
                       raise_instead=True, close_proposal=False,
                       next="the tiers changed since this was applied; revert by hand and "
                            f"record it with: togi lock {man.name}")
    lock = audit.verify_locked(repo, man)
    if not lock["locked_file_present"]:
        raise TogiError("not_locked", "this bundle has no .togi/locked.sha256, so locked files cannot be enforced",
                        next=f"togi lock {man.name}", status="refused")
    if not lock["ok"]:
        bad = audit.lock_problems(lock)[:5] or [lock.get("problem", "the lock is not anchored")]
        return _reject(meta, man, cl, sha, "locked_hash_mismatch",
                       "locked files differ from .togi/locked.sha256: " + ", ".join(bad), dry_run=False,
                       raise_instead=True, close_proposal=False,
                       next=f"review the change, then: togi lock {man.name}")
    changes: dict[str, str] = {}
    try:
        for fp in cl.patches:
            f = repo / fp.path
            if not f.is_file():
                raise patch.PatchError(f"{fp.path} no longer exists")
            changes[fp.path] = patch.apply_to_text(f.read_text(encoding="utf-8"), fp, reverse=True)
    except patch.PatchError as e:
        raise TogiError("revert_conflict", f"cannot revert cleanly: {e}",
                        next="the files changed after the proposal was applied; revert by hand and record it",
                        status="refused")
    approvals.require_approver()
    files = sorted(cl.tiers)
    lines = [f"Revert proposal {escape_text(pid, 60)}  tier={cl.tier}",
             f"bundle: {escape_text(man.name, 60)}   repo: {escape_text(man.repo, 200)}",
             "files: " + ", ".join(f"{escape_text(f, 120)} [{cl.tiers[f]}]" for f in files)]
    user = approvals.confirm_and_authenticate(lines, pid.split("_")[-1], "revert this change", "revert")
    removals = [fp.path for fp in cl.patches if fp.is_new and changes.get(fp.path, "x") == ""]
    writes = {k: v for k, v in changes.items() if k not in removals}
    snap = snapshot(repo, list(writes) + removals)
    try:
        for rel in removals:
            (repo / rel).unlink()
        write_changes(repo, writes)
    except BaseException:
        restore(repo, snap)
        raise
    after = audit.verify_locked(repo, man)
    if not after["ok"]:
        restore(repo, snap)
        bad = audit.lock_problems(after)[:5]
        return _reject(meta, man, cl, sha, "locked_files_changed_by_write",
                       "the revert changed locked files and was rolled back: " + ", ".join(bad), dry_run=False,
                       raise_instead=True, close_proposal=False,
                       next="the classifier and the filesystem disagree about a path")
    entry = audit.append(repo, man.name, action="revert", proposal=pid, tier=meta.get("tier"), files=files,
                         diff_sha256=sha, stone_before=None, stone_after=None, decided_by=f"human:{user}",
                         reason="reverted by request")
    meta.update(status="reverted", reverted_at=entry["ts"], revert_audit_seq=entry["seq"])
    save_meta(meta)
    return {"proposal": pid, "bundle": man.name, "reverted": files, "audit_seq": entry["seq"]}


# --------------------------------------------------------------------------- CLI


def h_review(argv: list[str]) -> Reply:
    pos, f = parse_flags(argv, {}, "review")
    if f["help"]:
        return _usage("review")
    if not pos:
        return Reply(env=output.ok_envelope(list_proposals()), mode=_mode(f))
    meta, diff, rationale = load_proposal(pos[0])
    ev = None
    e = read_evaluation(pos[0])        # a cache that is not signed by this machine's key is not shown as a result
    if e is not None:
        ev = {"before": e.get("before"), "after": e.get("after"), "driver": e.get("driver"), "runs": e.get("runs")}
    data = {"id": meta["id"], "bundle": meta["bundle"], "status": meta.get("status"), "tier": meta.get("tier"),
            "files": meta.get("files"), "rationale": rationale.strip(), "diff": diff, "stone": ev,
            "next": f"togi apply {meta['id']}" if meta.get("status") in ("proposed", "needs_human") else None}
    env = output.ok_envelope(data)
    if not f["full"]:
        env, _ = output.fit(env, output.MAX_BYTES, lambda off: f"togi review {pos[0]} --full")
    return Reply(env=env, mode=_mode(f))


def h_apply(argv: list[str]) -> Reply:
    pos, f = parse_flags(argv, {"runs": "int", "driver": "str", "model": "str", "surface": "str",
                                "dry-run": "bool", "task-timeout": "float", "outcome": "str", "suite": "str",
                                "subjects": "str", "repeats": "int"}, "apply")
    if f["help"]:
        return _usage("apply")
    if len(pos) != 1:
        raise TogiError("bad_arg", "togi apply needs a proposal id", next="togi review")
    if f["driver"] and f["driver"] not in ("claude", "script"):
        raise TogiError("bad_arg", "--driver must be claude or script", next="togi apply --help")
    gate = None
    if f["outcome"] or f["suite"] or f["subjects"]:
        if not (f["outcome"] and f["suite"] and f["subjects"]):
            raise TogiError("bad_arg", "--outcome, --suite and --subjects go together", next="togi apply --help")
        gate = {"outcome": f["outcome"], "suite": f["suite"], "subjects": f["subjects"], "repeats": f["repeats"] or 1}
    res = apply_proposal(pos[0], runs=f["runs"], driver=f["driver"], model=f["model"],
                         surface=f["surface"] or "cli", dry_run=f["dry_run"],
                         task_timeout=f["task_timeout"] or 600, interactive=sys.stdin.isatty() and sys.stdout.isatty(),
                         outcome=gate)
    if res.get("needs_human"):
        return Reply(env=output.error_envelope(
            "needs_approval", "needs_human",
            res.get("message") or f"proposal {pos[0]} is tier {res['tier']} ({res['reason']}) and needs a person "
                                  "to confirm it",
            next=f"a person runs: togi apply {pos[0]}", data=res), mode=_mode(f))
    if sys.stdout.isatty() and _mode(f) is None and res["action"] != "reject":
        return Reply(text=_apply_text(res), mode="text")
    if res["action"] == "reject" and not f["dry_run"]:
        return Reply(env=output.error_envelope(
            "refused", res["reason"].split(":")[0], res.get("message") or f"rejected: {res['reason']}",
            next="togi review", data=res), mode=_mode(f))
    return Reply(env=output.ok_envelope(res), mode=_mode(f))


def h_revert(argv: list[str]) -> Reply:
    pos, f = parse_flags(argv, {}, "revert")
    if f["help"]:
        return _usage("revert")
    if len(pos) != 1:
        raise TogiError("bad_arg", "togi revert needs a proposal id", next="togi review")
    return Reply(env=output.ok_envelope(revert_proposal(pos[0])), mode=_mode(f))


def _apply_text(res: dict) -> str:
    b, a = res.get("stone_before") or {}, res.get("stone_after") or {}
    verb = "dry run: would apply" if res.get("dry_run") else "applied"
    lines = [f"{verb} {res['proposal']}  tier={res['tier']}  decided_by={res['decided_by']}  ({res['reason']})",
             "files: " + ", ".join(res["files"]),
             f"tasks   {_rate_text(b)}  ->  {_rate_text(a)}", f"tests   {_tests(b)}  ->  {_tests(a)}"]
    if res.get("audit_seq"):
        lines.append(f"audit seq {res['audit_seq']}   undo with: togi revert {res['proposal']}")
    return "\n".join(lines)
