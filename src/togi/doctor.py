"""`togi doctor`: registry, imports, manifest, tiers, stone, lock hashes and the audit chain."""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

from . import approver, audit, output, paths, registry, sandbox, stone
from .cli import Reply, parse_flags, _mode, _usage
from .errors import TogiError
from .manifest import ManifestError, glob_match, load_manifest, walk_files


def check(name: str, ok: bool, detail: str = "", level: str = "error", next: str | None = None) -> dict:
    row = {"check": name, "ok": ok, "level": level if not ok else "ok", "detail": detail}
    if next and not ok:
        row["next"] = next
    return row


def check_bundle(name: str, path: str) -> list[dict]:
    rows: list[dict] = []
    p = Path(path)
    rows.append(check(f"{name}: repo exists", p.is_dir(), str(p), next=f"togi remove {name}; togi add <path>"))
    if not p.is_dir():
        return rows
    try:
        man = load_manifest(p)
    except ManifestError as e:
        rows.append(check(f"{name}: manifest", False, str(e), next="fix togi.toml"))
        return rows
    rows.append(check(f"{name}: manifest", True, f"{man.name} {man.version}"))
    rows.append(check(f"{name}: summary", bool(man.summary), "shown in `togi skill`", level="warn",
                      next="add [bundle].summary to togi.toml"))
    try:
        loaded = registry.load_repo(p, expect_name=name)
        n = len(loaded.bundle.commands)
        rows.append(check(f"{name}: imports", True, f"{man.module} ({n} command{'s' if n != 1 else ''})"))
        money = [c.name for c in loaded.bundle.commands.values() if c.effect == "money"]
        rows.append(check(f"{name}: money gate", True if "money" in man.gated_effects() else False,
                          f"gated effects: {', '.join(sorted(man.gated_effects()))}"
                          + (f"; money commands: {', '.join(money)}" if money else "")))
        missing_ex = [c.name for c in loaded.bundle.commands.values() if not c.examples]
        rows.append(check(f"{name}: examples", not missing_ex,
                          "commands without examples: " + ", ".join(missing_ex[:5]) if missing_ex else "all commands have examples",
                          level="warn", next="add examples=[...] to @bundle.command"))
    except TogiError as e:
        rows.append(check(f"{name}: imports", False, e.message, next=e.next))
    skills_dir = p / man.skills
    rows.append(check(f"{name}: SKILL.md", (skills_dir / "SKILL.md").is_file(),
                      f"{man.skills}/SKILL.md", level="warn", next="write skills/SKILL.md (or it is generated)"))
    files = list(walk_files(p))
    for tier in ("polish", "reshape", "locked"):
        for pat in getattr(man, tier):
            hit = any(glob_match(pat, f) for f in files)
            rows.append(check(f"{name}: tier {tier} glob '{pat}'", hit, "matches files" if hit else "matches no files",
                              level="warn", next="fix or remove the glob in [tiers]"))
    overlap = [f for f in files if man.classify(f) == "locked" and any(glob_match(g, f) for g in man.polish + man.reshape)]
    rows.append(check(f"{name}: tiers overlap", not overlap,
                      "locked wins over polish/reshape for: " + ", ".join(overlap[:3]) if overlap else "no overlap",
                      level="warn"))
    tests_ok = (p / man.stone_tests).exists()
    rows.append(check(f"{name}: stone tests dir", tests_ok, man.stone_tests, level="warn",
                      next="create the tests directory"))
    try:
        tasks = stone.load_tasks(man)
        hold = sum(1 for t in tasks if t.holdout)
        rows.append(check(f"{name}: stone tasks", True, f"{len(tasks)} task(s), {hold} holdout"))
        rows.append(check(f"{name}: holdout tasks", hold > 0 or not tasks, "at least one holdout task is recommended",
                          level="warn"))
    except TogiError as e:
        rows.append(check(f"{name}: stone tasks", False, e.message, next=e.next))
    lock = audit.verify_locked(p, man)
    if not lock["locked_file_present"]:
        needed = man.lock_required()
        rows.append(check(f"{name}: locked hashes", not needed,
                          "no .togi/locked.sha256: apply and this bundle's money commands are refused until it is locked"
                          if needed else "no .togi/locked.sha256, and [policy] require_lock says this bundle needs none",
                          level="warn", next=f"togi lock {name}"))
    else:
        bad = audit.lock_problems(lock) or [lock.get("problem", "the lock is not anchored in the signed audit log")]
        rows.append(check(f"{name}: locked hashes", lock["ok"],
                          f"{lock['files']} file(s) verified" if lock["ok"] else "differs: " + ", ".join(bad[:5]),
                          next=f"review the change, then: togi lock {name}"))
    if man.test_approvals:
        rows.append(check(f"{name}: test_approvals", False,
                          "[policy] test_approvals = true: this bundle accepts grants from a throwaway (test-scope) "
                          "approver, so its gated commands must check ctx.approval_scope", level="warn",
                          next="remove test_approvals unless every gated command refuses test scope outside test mode"))
    chain = audit.verify(p, name)
    if chain["entries"] or chain["head"] != "none":
        note = f"; {chain['unauthenticated_entries']} older entr{'y' if chain['unauthenticated_entries'] == 1 else 'ies'} " \
               "not signed by this machine's key" if chain["unauthenticated_entries"] else ""
        rows.append(check(f"{name}: audit chain", chain["ok"],
                          f"{chain['entries']} entr{'y' if chain['entries'] == 1 else 'ies'} verified{note}" if chain["ok"]
                          else f"broken at seq {chain['problems'][0]['seq']}: {chain['problems'][0]['problem']}",
                          next=f"togi log {name} --verify"))
    else:
        rows.append(check(f"{name}: audit chain", True, "empty"))
    return rows


def _perm(p) -> int | None:
    try:
        return p.stat().st_mode & 0o777
    except OSError:
        return None


def check_host() -> list[dict]:
    """Machine-level guards: the approver (passphrase, key, location), the audit key, the code-mode sandbox."""
    rows: list[dict] = []
    override = paths.approver_override()
    rows.append(check("TOGI_APPROVER_HOME not set", override is None,
                      "the approver is the fixed one in " + str(paths.fixed_approver_home()) if override is None
                      else f"set to {override}: approvals made here carry scope 'test', a bundle that moves real "
                           "value must refuse them outside its own test mode, and lock, revert and apply refuse this "
                           "approver outright",
                      level="warn", next="unset TOGI_APPROVER_HOME (it is for tests and the stone)"))
    try:
        configured = approver.configured()
        detail = (f"set up (scrypt) in {paths.approver_home()}" if configured else
                  f"not set up in {paths.approver_home()}: approve, deny, lock, revert and reshape-apply refuse")
    except Exception as e:   # a damaged record is reported, not hidden
        configured, detail = False, getattr(e, "message", str(e))
    rows.append(check("approver passphrase", configured, detail, level="warn",
                      next="a person runs, once, in their own terminal: togi approver setup"))
    if configured:
        mode = _perm(paths.approver_path())
        rows.append(check("approver file permissions", mode == 0o600, f"mode {oct(mode) if mode is not None else '?'}",
                          level="warn", next=f"chmod 600 {paths.approver_path()}"))
        try:
            has_key = approver.load_key() is not None
            key_detail = "present" if has_key else "missing: grants cannot be signed"
        except Exception as e:
            has_key, key_detail = False, getattr(e, "message", str(e))
        rows.append(check("approver key", has_key, key_detail, level="warn",
                          next="a person runs: togi approver setup   (it asks for the current passphrase)"))
        if has_key:
            kmode = _perm(paths.approver_key_path())
            rows.append(check("approver key permissions", kmode == 0o600,
                              f"mode {oct(kmode) if kmode is not None else '?'}", level="warn",
                              next=f"chmod 600 {paths.approver_key_path()}"))
    legacy = paths.togi_home() / "approver"
    if legacy != paths.approver_path() and legacy.exists():
        rows.append(check("legacy approver", False,
                          f"{legacy} is an r1 approver and is no longer read: the approver lives in "
                          f"{paths.approver_home()}", level="warn",
                          next=f"a person runs: togi approver setup   (then delete {legacy})"))
    kp = paths.audit_key_path()
    if kp.exists():
        mode = _perm(kp)
        rows.append(check("audit key permissions", mode == 0o600, f"mode {oct(mode) if mode is not None else '?'}",
                          level="warn", next=f"chmod 600 {kp}"))
    ok, why = sandbox.available()
    rows.append(check("code-mode sandbox (togi run)", ok, "sandbox-exec available" if ok else why, level="warn",
                      next="without it `togi run` scripts run unsandboxed and say `sandboxed: false`"))
    return rows


def h_doctor(argv: list[str]) -> Reply:
    pos, f = parse_flags(argv, {}, "doctor")
    if f["help"]:
        return _usage("doctor")
    rows: list[dict] = [check("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0])]
    rows.append(check("togi on PATH", shutil.which("togi") is not None, shutil.which("togi") or "not found",
                      level="warn", next="ln -s <togi>/bin/togi ~/.local/bin/togi"))
    rows.append(check("claude CLI (stone/hone claude driver)", shutil.which("claude") is not None,
                      shutil.which("claude") or "not found", level="warn", next="install Claude Code"))
    rows.append(check("mcp package (togi mcp serve)", importlib.util.find_spec("mcp") is not None,
                      "importable" if importlib.util.find_spec("mcp") else "not installed", level="warn",
                      next="pip install mcp"))
    rows += check_host()
    ents = registry.entries()
    rows.append(check("registry", True, f"{len(ents)} bundle(s) registered" + (
        "" if ents else "; add one with: togi add <path>")))
    names = pos[:1] or sorted(ents)
    for n in names:
        if n not in ents:
            raise TogiError("unknown_bundle", f"no bundle named '{n}' is registered", next="togi bundles")
        rows += check_bundle(n, ents[n].path)
    errors = [r for r in rows if not r["ok"] and r["level"] == "error"]
    warns = [r for r in rows if not r["ok"] and r["level"] == "warn"]
    data = {"ok": not errors, "errors": len(errors), "warnings": len(warns), "checks": rows}
    if errors:
        return Reply(env=output.error_envelope("error", "doctor_failed", f"{len(errors)} check(s) failed: "
                                               + "; ".join(r["check"] for r in errors[:3]),
                                               next=errors[0].get("next"), data=data), mode=_mode(f))
    return Reply(env=output.ok_envelope(data), mode=_mode(f))
