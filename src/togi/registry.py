"""Registry (bundles.toml) and lazy bundle loading.

`togi add <path>` records a bundle repo in $TOGI_HOME/bundles.toml. A project-local `.togi/bundles.toml`
(found by walking up from the cwd) is merged on top: its entries win on a name clash.
Bundles are imported lazily, only when a command needs them.
"""
from __future__ import annotations

import importlib
import json
import os
import re
import sys
import sysconfig
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .bundle import Bundle
from .errors import TogiError
from .manifest import Manifest, ManifestError, load_manifest, locked_digest
from .paths import togi_home

RESERVED_NAMES = {
    "skill", "tools", "run", "approve", "deny", "approvals", "journal", "stone", "hone", "review", "apply",
    "revert", "log", "lock", "bench", "mcp", "add", "remove", "bundles", "doctor", "help", "version", "approver",
}


@dataclass
class Entry:
    name: str
    path: str
    source: str  # "global" | "local"


def global_registry_path() -> Path:
    return togi_home() / "bundles.toml"


def local_registry_path(start: Path | None = None) -> Path | None:
    cur = (start or Path.cwd()).resolve()
    # ~/.togi is the default global home, never a "project-local" registry (even when $TOGI_HOME points elsewhere)
    skip = {global_registry_path().resolve(), (Path.home() / ".togi" / "bundles.toml").resolve()}
    for d in [cur, *cur.parents]:
        cand = d / ".togi" / "bundles.toml"
        if cand.is_file() and cand.resolve() not in skip:
            return cand
    return None


def _read(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise TogiError("registry_invalid", f"{path} is not valid TOML ({e})",
                        next=f"fix or delete {path}, then run: togi bundles")
    out = {}
    for name, ent in (data.get("bundles") or {}).items():
        if isinstance(ent, dict) and isinstance(ent.get("path"), str):
            out[name] = ent["path"]
    return out


def _write(path: Path, entries: dict[str, str]) -> None:
    lines = ["# Togi bundle registry. Managed by `togi add` / `togi remove`.\n"]
    for name in sorted(entries):
        lines.append(f"\n[bundles.{json.dumps(name)}]\npath = {json.dumps(entries[name])}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".bundles-", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("".join(lines))
    os.replace(tmp, path)


def entries() -> dict[str, Entry]:
    out: dict[str, Entry] = {}
    for name, p in _read(global_registry_path()).items():
        out[name] = Entry(name, p, "global")
    loc = local_registry_path()
    if loc:
        for name, p in _read(loc).items():
            out[name] = Entry(name, p, "local")
    return out


def add(path: str, local: bool = False) -> tuple[Manifest, Path]:
    repo = Path(path).expanduser()
    if not repo.is_dir():
        raise TogiError("path_not_found", f"{path} is not a directory", next="togi add <path-to-bundle-repo>")
    try:
        man = load_manifest(repo)
    except ManifestError as e:
        raise TogiError("manifest_invalid", str(e), next="create togi.toml (see: togi skill) then run togi add again")
    if man.name in RESERVED_NAMES:
        raise TogiError("reserved_name", f"'{man.name}' is a built-in togi command and cannot be a bundle name",
                        next="rename [bundle].name in togi.toml")
    target = (Path.cwd() / ".togi" / "bundles.toml") if local else global_registry_path()
    current = _read(target)
    current[man.name] = str(man.repo)
    _write(target, current)
    return man, target


def remove(name: str) -> list[Path]:
    removed = []
    paths = [global_registry_path()]
    loc = local_registry_path()
    if loc:
        paths.insert(0, loc)
    for p in paths:
        cur = _read(p)
        if name in cur:
            del cur[name]
            _write(p, cur)
            removed.append(p)
    if not removed:
        raise TogiError("unknown_bundle", f"no bundle named '{name}' is registered", next="togi bundles")
    return removed


# --------------------------------------------------------------------------- loading


@dataclass
class Loaded:
    name: str
    repo: Path
    manifest: Manifest
    bundle: Bundle
    _mtime: int = 0

    def refresh(self) -> None:
        """Re-read togi.toml if it changed, so policy edits apply to long-lived processes (run, mcp)."""
        try:
            m = self.manifest.path.stat().st_mtime_ns
        except OSError:
            return
        if m != self._mtime:
            if self._mtime:
                try:
                    self.manifest = load_manifest(self.repo)
                except ManifestError:
                    pass  # keep the last good policy
            self._mtime = m

    def binding(self) -> dict:
        """What an approval for this bundle is bound to besides command + args."""
        self.refresh()
        return {"repo": str(self.repo), "version": self.manifest.version,
                "locked_digest": locked_digest(self.repo, self.manifest)}

    def requires_approval(self, effect: str) -> bool:
        self.refresh()
        return effect in self.manifest.gated_effects()


_CACHE: dict[tuple[str, str], Loaded] = {}


def manifest_for(name: str) -> Manifest:
    ent = entries().get(name)
    if not ent:
        raise TogiError("unknown_bundle", f"no bundle named '{name}' is registered", next="togi bundles")
    try:
        return load_manifest(ent.path)
    except ManifestError as e:
        raise TogiError("manifest_invalid", str(e), next="togi doctor")


def load(name: str) -> Loaded:
    ent = entries().get(name)
    if not ent:
        raise TogiError("unknown_bundle", f"no bundle named '{name}' is registered", next="togi bundles")
    return load_repo(Path(ent.path), expect_name=name)


# --------------------------------------------------------------------------- what a bundle's import may reach (r5)


def harden_imports() -> None:
    """Make importing a bundle read SOURCE only. `sys.dont_write_bytecode = True` (nothing is cached into the repo, so the
    repo's own `__pycache__` stays whatever a person left there) and `sys.pycache_prefix` pointing at a Togi-owned directory
    inside `$TOGI_HOME` (so Python looks for cached bytecode THERE and never beside the source): a `__pycache__/store.*.pyc`
    planted in the repo, with the right header, is never read, and the code that runs is the code in the `.py` file that
    the lock hashed. Cost: modules imported after this are compiled from source in each process (bundle code and any
    standard-library module it pulls in lazily); Togi's own modules are already imported by then."""
    sys.dont_write_bytecode = True
    sys.pycache_prefix = os.path.join(os.path.abspath(str(togi_home())), "pycache")


def _stdlib_entries() -> set[str]:
    out = set()
    for key in ("stdlib", "platstdlib"):
        p = sysconfig.get_path(key)
        if p:
            out.add(os.path.realpath(p))
            out.add(os.path.realpath(os.path.join(p, "lib-dynload")))
    return out


def stdlib_boundary() -> int:
    """The index in `sys.path` just after the last standard-library entry (the zip, the stdlib directory, lib-dynload).
    Bundle repos go there: after the standard library, so a `json.py` or `decimal.py` lying in a repo (or its `src/`)
    cannot replace a stdlib module that Togi or the bundle imports later, and before `site-packages`, so a registered repo
    still wins over an old installed copy of the same bundle. Without a recognisable standard library, the end."""
    std = _stdlib_entries()
    last = -1
    for i, entry in enumerate(sys.path):
        if not entry:
            continue
        if os.path.realpath(entry) in std or re.search(r"python\d+\.zip$", entry):
            last = i
    return last + 1 if last >= 0 else len(sys.path)


def add_repo_paths(repo: Path) -> None:
    """Put `<repo>/src` and then `<repo>` on `sys.path`, after the standard library (see `stdlib_boundary`)."""
    wanted = [str(repo / "src"), str(repo)]
    for p in wanted:
        while p in sys.path:
            sys.path.remove(p)
    at = stdlib_boundary()
    sys.path[at:at] = wanted


def load_repo(repo: Path, expect_name: str | None = None) -> Loaded:
    try:
        man = load_manifest(repo)
    except ManifestError as e:
        raise TogiError("manifest_invalid", str(e), next="togi doctor")
    if expect_name and man.name != expect_name:
        raise TogiError("name_mismatch", f"registry says '{expect_name}' but {man.path} says '{man.name}'",
                        next=f"togi remove {expect_name} && togi add {man.repo}")
    harden_imports()
    key = (man.name, str(man.repo))
    if key in _CACHE:
        return _CACHE[key]
    add_repo_paths(man.repo)
    _purge_foreign_modules(man)
    try:
        mod = importlib.import_module(man.module_name)
        obj = getattr(mod, man.module_attr)
    except TogiError:
        raise
    except Exception as e:  # import-time failure in a bundle: say where, don't crash the host
        raise TogiError("bundle_import_failed",
                        f"importing {man.module} failed: {type(e).__name__}: {e}", next="togi doctor")
    if not isinstance(obj, Bundle):
        raise TogiError("bundle_invalid", f"{man.module} is not a togi.Bundle", next="togi doctor")
    if obj.name != man.name:
        raise TogiError("name_mismatch", f"Bundle('{obj.name}') in code but [bundle].name = '{man.name}' in togi.toml",
                        next="make the two names match, then run: togi doctor")
    loaded = Loaded(man.name, man.repo, man, obj)
    loaded.refresh()
    _CACHE[key] = loaded
    return loaded


def _purge_foreign_modules(man: Manifest) -> None:
    """If a package of the same name was imported from another repo (tests, stone candidates), drop it."""
    top = man.module_name.split(".")[0]
    mod = sys.modules.get(top)
    if mod is None:
        return
    files = [getattr(mod, "__file__", None)] + list(getattr(mod, "__path__", []) or [])
    repo = str(man.repo) + os.sep
    if any(f and (str(Path(f).resolve()) + os.sep).startswith(repo) for f in files):
        return
    for k in [k for k in sys.modules if k == top or k.startswith(top + ".")]:
        del sys.modules[k]
    importlib.invalidate_caches()


def clear_cache() -> None:
    _CACHE.clear()
