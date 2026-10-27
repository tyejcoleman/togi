"""togi.toml: the bundle manifest (SPEC section 2) and tier globbing."""
from __future__ import annotations

import hashlib
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


def locked_files(repo: Path, man: Manifest) -> list[str]:
    """Repo files covered by a locked pattern. A file whose NAME cannot be judged (control characters, line
    separators) is treated as locked, so planting one under a locked directory is detected."""
    pats = [p for p in man.locked_patterns() if not p.startswith(_VOLATILE)]
    out = []
    for rel in walk_files(repo):
        try:
            norm = normalize_relpath(rel)
        except PathRejected:
            out.append(rel)
            continue
        if any(glob_match(p, norm) for p in pats):
            out.append(rel)
    return out


def locked_digest(repo: Path, man: Manifest) -> str:
    """One digest over the CURRENT contents of every locked file (not the recorded .togi/locked.sha256), so
    anything bound to it (an approval) dies when locked code changes."""
    lines = []
    for rel in sorted(locked_files(repo, man)):
        try:
            lines.append(f"{file_sha256(repo / rel)}  {rel!r}")
        except OSError:
            lines.append(f"unreadable  {rel!r}")
    return hashlib.sha256("\n".join(lines).encode("utf-8", "surrogatepass")).hexdigest()
