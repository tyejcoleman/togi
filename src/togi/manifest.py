"""togi.toml: the bundle manifest (SPEC section 2) and tier globbing."""
from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .pathnorm import PathRejected, fold, normalize_relpath

TIER_ORDER = {"polish": 1, "reshape": 2, "locked": 3}
APPLY_DRIVERS = ("script", "claude")   # ordered: a caller may raise the driver, never lower it
MIN_APPLY_RUNS = 2


class ManifestError(Exception):
    pass


@dataclass
class Manifest:
    repo: Path
    name: str
    version: str = "0.0.0"
    summary: str = ""
    module: str = ""
    skills: str = "skills"
    polish: list[str] = field(default_factory=list)
    reshape: list[str] = field(default_factory=list)
    locked: list[str] = field(default_factory=list)
    stone_tests: str = "tests"
    stone_tasks: str = "stone/tasks"
    stone_env: dict[str, str] = field(default_factory=dict)   # [stone].env: set for tests and task runs
    approve_effects: list[str] = field(default_factory=lambda: ["money"])
    auto_polish: bool = False
    run_env: list[str] = field(default_factory=list)      # [policy].run_env: env vars the `togi run` host keeps
    apply_driver: str = "claude"                           # [policy].apply_driver: minimum driver for `togi apply`
    apply_runs: int = 3                                    # [policy].apply_runs: minimum runs for `togi apply`
    test_approvals: bool = False                           # [policy].test_approvals: accept test-scope (throwaway-approver) grants
    require_lock: bool | None = None                       # [policy].require_lock: None = default (see lock_required)
    raw: dict = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.repo / "togi.toml"

    @property
    def module_name(self) -> str:
        return self.module.split(":")[0]

    @property
    def module_attr(self) -> str:
        return self.module.split(":")[1] if ":" in self.module else "bundle"

    def lock_required(self) -> bool:
        """Must a money command see a verified lock (`.togi/locked.sha256`, anchored in the audit log) before it runs?
        `[policy] require_lock` says so explicitly; absent, it is true for a bundle that has locked tiers of its own
        (`[tiers].locked` is not empty) and false for one that has none. A lock file that EXISTS must always verify,
        whatever this says: it only decides whether a bundle may run money commands with no lock file at all."""
        return bool(self.locked) if self.require_lock is None else self.require_lock

    def gated_effects(self) -> set[str]:
        """Effects that need a human approval. `money` is always gated."""
        return set(self.approve_effects) | {"money"}

    def implicit_locked(self) -> list[str]:
        """Always locked, whatever the manifest says: hone must never edit these."""
        out = ["togi.toml", ".togi/**", ".git/**"]
        for d in (self.stone_tasks, self.stone_tests):
            d = d.strip("/")
            if d and d != ".":
                out += [d, d + "/**"]
        out.append("stone/**")
        return out

    def locked_patterns(self) -> list[str]:
        return list(dict.fromkeys(self.locked + self.implicit_locked()))

    def classify(self, relpath: str) -> str | None:
        """Tier of a repo-relative path: locked > reshape > polish; None if it matches no tier.

        The path is normalised first (`.`, `..`, `//` resolved) and matched case-insensitively against
        case-folded patterns, so `skills/MONEY.md` is the locked `skills/money.md`. A path that cannot be
        normalised (control characters, line separators, outside the repo) is `locked`: fail closed."""
        try:
            rel = normalize_relpath(relpath)
        except PathRejected:
            return "locked"
        if any(glob_match(g, rel) for g in self.locked_patterns()):
            return "locked"
        if any(glob_match(g, rel) for g in self.reshape):
            return "reshape"
        if any(glob_match(g, rel) for g in self.polish):
            return "polish"
        return None


def _str_list(table: dict, key: str, where: str) -> list[str]:
    val = table.get(key, [])
    if isinstance(val, str):
        val = [val]
    if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
        raise ManifestError(f"{where}.{key} must be a list of strings")
    return list(val)


def load_manifest(repo: str | Path) -> Manifest:
    repo = Path(repo).expanduser().resolve()
    path = repo / "togi.toml"
    if not path.is_file():
        raise ManifestError(f"no togi.toml in {repo}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ManifestError(f"{path}: invalid TOML ({e})") from None
    b = raw.get("bundle")
    if not isinstance(b, dict):
        raise ManifestError(f"{path}: missing [bundle] table")
    name = b.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name):
        raise ManifestError(f"{path}: [bundle].name must be a simple name like \"crowd\"")
    module = b.get("module")
    if not isinstance(module, str) or not module:
        raise ManifestError(f"{path}: [bundle].module is required, e.g. \"{name}.togi_bundle:bundle\"")
    tiers = raw.get("tiers", {})
    stone = raw.get("stone", {})
    policy = raw.get("policy", {})
    return Manifest(
        repo=repo,
        name=name,
        version=str(b.get("version", "0.0.0")),
        summary=str(b.get("summary", "")).strip(),
        module=module,
        skills=str(b.get("skills", "skills")),
        polish=_str_list(tiers, "polish", "tiers"),
        reshape=_str_list(tiers, "reshape", "tiers"),
        locked=_str_list(tiers, "locked", "tiers"),
        stone_tests=str(stone.get("tests", "tests")),
        stone_tasks=str(stone.get("tasks", "stone/tasks")),
        stone_env=_stone_env(stone, path),
        approve_effects=_str_list(policy, "approve_effects", "policy") if "approve_effects" in policy else ["money"],
        auto_polish=bool(policy.get("auto_polish", False)),
        run_env=_run_env(policy, path),
        apply_driver=_apply_driver(policy, path),
        apply_runs=_apply_runs(policy, path),
        test_approvals=_policy_bool(policy, "test_approvals", False, path),
        require_lock=_policy_bool(policy, "require_lock", None, path),
        raw=raw,
    )


def _run_env(policy: dict, path) -> list[str]:
    """[policy].run_env: names of environment variables the `togi run` host keeps (everything else is scrubbed
    before the script's child starts). Values are handed over out of band, never left in the visible
    environment of the host process."""
    names = _str_list(policy, "run_env", "policy") if "run_env" in policy else []
    bad = [n for n in names if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)]
    if bad:
        raise ManifestError(f"{path}: [policy].run_env has invalid variable name(s): {', '.join(bad)}")
    return list(dict.fromkeys(names))


def _policy_bool(policy: dict, key: str, default, path):
    """A [policy] switch must be a real TOML boolean: "true" (a string) or 1 is a typo, not consent."""
    if key not in policy:
        return default
    v = policy[key]
    if not isinstance(v, bool):
        raise ManifestError(f"{path}: [policy].{key} must be true or false")
    return v


def _apply_driver(policy: dict, path) -> str:
    d = policy.get("apply_driver", "claude")
    if d not in APPLY_DRIVERS:
        raise ManifestError(f"{path}: [policy].apply_driver must be \"claude\" or \"script\"")
    return d


def _apply_runs(policy: dict, path) -> int:
    n = policy.get("apply_runs", 3)
    if isinstance(n, bool) or not isinstance(n, int) or n < MIN_APPLY_RUNS:
        raise ManifestError(f"{path}: [policy].apply_runs must be an integer >= {MIN_APPLY_RUNS}")
    return n


def _stone_env(stone: dict, path) -> dict[str, str]:
    """[stone].env: plain string variables the bundle needs under test (e.g. a fake payment mode).
    TOGI_* names are refused: the host owns those."""
    env = stone.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise ManifestError(f"{path}: [stone].env must be a table of string values")
    bad = [k for k in env if k.startswith("TOGI_") or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)]
    if bad:
        raise ManifestError(f"{path}: [stone].env may not set {', '.join(bad)}")
    return dict(env)


# --------------------------------------------------------------------------- globbing


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern:
    pat = pattern.strip()
    if pat.startswith("./"):
        pat = pat[2:]
    i, out = 0, []
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i: i + 2] == "**":
                if pat[i: i + 3] == "**/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                cls = pat[i + 1: j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append("[" + cls + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(pattern: str, relpath: str) -> bool:
    """Case-insensitive (NFKC + casefold on both sides): tier globs must not be dodged by letter case."""
    return bool(_glob_regex(fold(pattern)).match(fold(relpath)))


# What `walk_files` leaves out (hone, apply and doctor read repo text with it). NOT used for the lock set (r5): see `_walk_for_lock`.
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", ".mypy_cache", ".ruff_cache",
             "dist", "build", ".tox"}


def walk_files(repo: Path):
    """Yield repo-relative posix paths of regular files, skipping VCS/cache dirs."""
    import os

    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for fn in sorted(filenames):
            if fn.endswith((".pyc", ".pyo")) or fn == ".DS_Store":
                continue
            full = Path(dirpath) / fn
            if full.is_symlink() or not full.is_file():
                continue
            yield full.relative_to(repo).as_posix()


# --------------------------------------------------------------------------- locked files (shared helpers)

_VOLATILE = (".togi", ".git")   # audit log / lock file / VCS: never part of the locked hash set


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _literal_prefix(pattern: str) -> str:
    pat = pattern.strip()
    if pat.startswith("./"):
        pat = pat[2:]
    cut = min([i for i in (pat.find(c) for c in "*?[") if i >= 0] or [len(pat)])
    return fold(pat[:cut])


def _walk_for_lock(repo: Path, patterns: list[str]):
    """Yield (repo-relative posix path, kind) for every entry that could fall under a locked pattern, kind being "file",
    "link_file" or "link_dir". NO skip list: `__pycache__`, `build`, `dist`, `venv`, `node_modules` are walked like any other
    directory, because a locked pattern that covers them covers what Python can import from them (r5). The only things
    left out are the repo's own `.git` / `.togi` (volatile by design, `_VOLATILE`), bytecode (`.pyc`/`.pyo`) inside
    `__pycache__` (never read by any process Togi starts: see registry.harden_imports and paths.pycache_args), and
    directories that provably cannot hold a match (a pattern's literal prefix says so)."""
    prefixes = [_literal_prefix(p) for p in patterns]

    def may_match(rel_dir: str) -> bool:
        d = fold(rel_dir) + "/"
        return any(lp.startswith(d) or d.startswith(lp) for lp in prefixes)

    for dirpath, dirnames, filenames in os.walk(repo):
        here = Path(dirpath)
        rel_here = here.relative_to(repo).as_posix()
        rel_here = "" if rel_here == "." else rel_here
        keep = []
        for d in sorted(dirnames):
            rel = f"{rel_here}/{d}" if rel_here else d
            if os.path.islink(here / d):
                yield rel, "link_dir"
            elif not (rel in _VOLATILE) and may_match(rel):
                keep.append(d)
        dirnames[:] = keep
        in_pycache = "__pycache__" in rel_here.split("/")
        for fn in sorted(filenames):
            if fn == ".DS_Store":
                continue
            if in_pycache and fn.endswith((".pyc", ".pyo")):
                # Bytecode caches are never read by any Python process Togi starts (sys.pycache_prefix in-process,
                # `-X pycache_prefix` for every child), so they can't change what runs; hashing them would only make
                # every ordinary test run "break" the lock. Anything else in a __pycache__ dir is still hashed.
                continue
            rel = f"{rel_here}/{fn}" if rel_here else fn
            if os.path.islink(here / fn):
                yield rel, "link_file"
            elif os.path.isfile(here / fn):     # (a FIFO or socket cannot be imported, and hashing one would block)
                yield rel, "file"


def scan_locked(repo: Path, man: Manifest) -> tuple[list[str], list[str]]:
    """(regular files, symlinks) that make up the lock set. One walk; `locked_files` / `locked_symlinks` are views of it.

    The lock set is what Python could import in place of, or as, the locked code (r5), not only the files a glob names:
      - every file that matches a locked pattern, anywhere (no `__pycache__` / `build` / `dist` / `venv` exemption);
      - the shadows of every locked importable file (`codeset.shadows_of`): a package directory, an extension module or
        bytecode that would be imported instead, whatever tier the globs give it;
      - symlinks that stand where locked content is (see `locked_symlinks`), which are never hashed as content;
      - a file whose NAME cannot be judged (control characters, line separators), treated as locked.
    """
    from .codeset import importable, shadows_of

    pats = [p for p in man.locked_patterns() if not p.startswith(_VOLATILE)]
    files: set[str] = set()
    links: set[str] = set()
    for rel, kind in _walk_for_lock(repo, pats):
        try:
            norm = normalize_relpath(rel)
        except PathRejected:
            (files if kind == "file" else links).add(rel)
            continue
        if kind == "file":
            if any(glob_match(p, norm) for p in pats):
                files.add(rel)
        elif kind == "link_file":
            if any(glob_match(p, norm) for p in pats):
                links.add(rel)
        else:   # a link to a directory: under a locked directory, or standing in place of an ancestor of locked files
            hit = any(glob_match(p, x) for p in pats for x in (norm, norm + "/"))
            if not hit:
                hit = any(_literal_prefix(p).startswith(fold(norm) + "/") for p in pats)
            if hit:
                links.add(rel)
    sh_files, sh_links = shadows_of(repo, sorted(f for f in files if importable(f.rsplit("/", 1)[-1])))
    files.update(sh_files)
    links.update(sh_links)
    return sorted(files - links), sorted(links)


def locked_files(repo: Path, man: Manifest) -> list[str]:
    """Repo files in the lock set (`scan_locked`), PLUS any symlink standing where locked content is: a link is never
    skipped, because locked code reached through a link nobody hashed is the hole this list exists to close. Callers that
    hash files must treat the links separately (`write_lock` refuses them, `verify_locked` fails on them)."""
    files, links = scan_locked(repo, man)
    return sorted({*files, *links})


def locked_symlinks(repo: Path, man: Manifest) -> list[str]:
    """Symlinks that stand where locked content is, or could hide it: a link whose own path matches a locked
    pattern (`src/closure/evil.py -> /tmp/x.py` is importable as locked code but is not hashed), a link to a
    directory under a locked directory, a link to a directory that replaces an ancestor of a locked file (a
    symlinked `src` hides everything below it from the walk), and (r5) a link standing in the place of a module that
    would shadow a locked one. Such a link makes the lock fail (`verify_locked`) and is refused for `togi lock`, so locked
    code can never be reached through a path nobody hashed."""
    return scan_locked(repo, man)[1]


def locked_digest(repo: Path, man: Manifest) -> str:
    """One digest over the CURRENT contents of every file in the lock set (not the recorded .togi/locked.sha256), so
    anything bound to it (an approval) dies when locked code changes OR when something appears that Python would import
    instead of it. Locked symlinks are part of it too (a link appearing under a locked pattern changes the digest)."""
    files, links = scan_locked(repo, man)
    lines = []
    for rel in files:
        try:
            lines.append(f"{file_sha256(repo / rel)}  {rel!r}")
        except OSError:
            lines.append(f"unreadable  {rel!r}")
    for rel in links:
        try:
            lines.append(f"symlink  {rel!r} -> {os.readlink(repo / rel)!r}")
        except OSError:
            lines.append(f"symlink  {rel!r}")
    return hashlib.sha256("\n".join(lines).encode("utf-8", "surrogatepass")).hexdigest()
