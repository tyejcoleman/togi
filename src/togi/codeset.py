"""What Python can import from a bundle repo: the set the lock and the approval binding have to cover (SPEC section 2, r5).

A lock that hashes the files a manifest NAMES is not a lock on the code that RUNS. Python resolves `import notes.store` by
looking in a directory, in this order: a package directory `store/` (when it has an `__init__`), then an extension module
(`store.so`, `store.cpython-312-darwin.so`), then `store.py`, then a bare `store.pyc`. So an inert-looking new directory or
`.so` beside a locked `store.py` replaces it without touching a single hashed byte. This module is the one place that knows
that rule, so the lock set (`manifest.locked_files`), the gated-code coverage check (`gated_code_unlocked`) and the doctor
cannot disagree about it:

  shadows_of(...)       the files that would be imported INSTEAD of a locked module: every one belongs to the lock set,
                        whatever tier a glob gives it (a reshape glob must not be able to launder a shadow);
  coverage_problems()   every importable file under the package of a gated command, and every repo module the bundle's
                        process has loaded, must be in the locked or reshape tier. Anything else is code that runs behind a
                        money command with nobody having signed for it.

Nothing here executes or imports bundle code; it reads directory listings. `gated_roots_live` / `loaded_repo_files` read
the live process (function code objects, `sys.modules`) and are only called where the bundle is legitimately loaded
already (the host), or in the sandboxed describe child (describe.py), never in a person's session.
"""
from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path
from typing import Any, Iterable

# Everything the import system can load from a directory. `.pth` files are not imported either, but they are the thing a
# site directory turns into extra sys.path entries, so they count as code.
IMPORT_SUFFIXES = (".py", ".pyw", ".pyc", ".pyo", ".so", ".pyd", ".pth")
COVERED_TIERS = ("locked", "reshape")      # a gated command's code must sit in one of these
PYCACHE = "__pycache__"


def importable(name: str) -> bool:
    """Could the import system load a file with this name? (`store.cpython-312-darwin.so` ends in `.so`.)"""
    return name.endswith(IMPORT_SUFFIXES)


def stem_of(name: str) -> str:
    """The module name a file would be imported as: everything before the first dot."""
    return name.split(".", 1)[0]


def _entries(path: str, cache: dict) -> list[os.DirEntry]:
    got = cache.get(path)
    if got is None:
        try:
            with os.scandir(path) as it:
                got = sorted(it, key=lambda e: e.name)
        except OSError:
            got = []
        cache[path] = got
    return got


def has_init(path: str, cache: dict) -> bool:
    """Is this directory a regular package? (an `__init__` with any importable suffix)"""
    return any(e.name.startswith("__init__.") and importable(e.name) and not e.is_dir(follow_symlinks=False)
               for e in _entries(path, cache))


def _tree(repo: str, rel_dir: str, files: set[str], links: set[str]) -> None:
    """Every file below a directory, nothing skipped (a shadowing package is all of its content)."""
    for dirpath, dirnames, filenames in os.walk(os.path.join(repo, rel_dir)):
        base = os.path.relpath(dirpath, repo).replace(os.sep, "/")
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                links.add(f"{base}/{d}")
        for f in filenames:
            (links if os.path.islink(os.path.join(dirpath, f)) else files).add(f"{base}/{f}")


def _shadows_in(repo: str, rel_dir: str, stem: str, skip: str | None, cache: dict, files: set[str],
                links: set[str]) -> None:
    """What would be imported as `stem` from `repo/rel_dir` instead of (or beside) the module file called `skip`."""
    here = os.path.join(repo, rel_dir) if rel_dir else repo
    prefix = f"{rel_dir}/" if rel_dir else ""
    for e in _entries(here, cache):
        if e.name == skip or stem_of(e.name) != stem:
            continue
        if e.is_symlink():
            if e.name == stem or importable(e.name):       # a link in the place of a package, or of an extension module
                links.add(prefix + e.name)
        elif e.name == stem and e.is_dir(follow_symlinks=False):
            if has_init(e.path, cache):                    # a package directory wins over every module file of that name
                _tree(repo, prefix + e.name, files, links)
        elif e.name != stem and importable(e.name) and e.is_file(follow_symlinks=False):
            files.add(prefix + e.name)
    # Bytecode in `__pycache__` is not a shadow: no Python process Togi starts ever reads it (pycache_prefix in-process,
    # `-X pycache_prefix` for children). A bare `name.pyc` beside the source IS importable and is handled above.


def shadows_of(repo: Path, locked: Iterable[str]) -> tuple[list[str], list[str]]:
    """(files, symlinks): everything that Python would import in place of, or beside, one of the locked importable files.

    For each locked `d/name.ext` (an importable file) that is: in `d`, a package directory `name/` with an `__init__`
    (and all of its content), `name.<any extension suffix>` and a bare `name.pyc` (bytecode in `__pycache__` is never read). A locked file that is
    NOT directly under `src/` is also checked against `src/`, which comes first on the import path: `src/engine.py` or
    `src/engine/` would replace a locked `engine.py` at the repo root. Symlinks standing in these places are returned
    separately (a link is never hashed as content). The result never contains a path that is itself in `locked`."""
    root = str(repo)
    cache: dict = {}
    files: set[str] = set()
    links: set[str] = set()
    seen: set[tuple[str, str, str | None]] = set()

    def look(rel_dir: str, stem: str, skip: str | None) -> None:
        key = (rel_dir, stem, skip)
        if key not in seen:
            seen.add(key)
            _shadows_in(root, rel_dir, stem, skip, cache, files, links)

    for rel in locked:
        parts = rel.split("/")
        name = parts[-1]
        if not importable(name):
            continue
        look("/".join(parts[:-1]), stem_of(name), name)
        if parts[0] != "src" and os.path.isdir(os.path.join(root, "src")):
            top = stem_of(parts[0]) if len(parts) == 1 else parts[0]
            look("src", top, None)
    own = set(locked)
    return sorted(files - own), sorted(links - own)


# --------------------------------------------------------------------------- coverage of the code behind gated commands


def importable_under(repo: Path, root_rel: str) -> tuple[list[str], list[str]]:
    """(importable files, symlinks) at or below `root_rel` (a directory or one module file), nothing skipped except the
    bytecode caches (`__pycache__`: Togi never reads them, see registry.harden_imports). A symlink anywhere in the package is
    returned as a problem of its own: a link to a directory is a package, whatever it points at."""
    top = os.path.join(str(repo), root_rel)
    files: list[str] = []
    links: list[str] = []
    if os.path.islink(top):
        return files, [root_rel]
    if os.path.isfile(top):
        return ([root_rel] if importable(os.path.basename(top)) else []), links
    for dirpath, dirnames, filenames in os.walk(top):
        base = os.path.relpath(dirpath, str(repo)).replace(os.sep, "/")
        for d in sorted(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                links.append(f"{base}/{d}")
        dirnames[:] = sorted(d for d in dirnames if d != PYCACHE)
        for f in sorted(filenames):
            if os.path.islink(os.path.join(dirpath, f)):
                if importable(f):
                    links.append(f"{base}/{f}")
            elif importable(f):
                files.append(f"{base}/{f}")
    return files, links


def root_for_file(repo: Path, filename: str | None) -> str | None:
    """The repo-relative root that holds all of the code a module file belongs to: the top-level package directory
    (`src/notes` for `src/notes/togi_bundle.py`), or the file itself for a top-level module. None for code outside the
    repo. The path is NOT resolved: a symlinked package must still read as being inside the repo."""
    if not filename or not os.path.isabs(filename):
        return None
    rel = os.path.relpath(os.path.abspath(filename), str(repo))
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    parts = rel.split(os.sep)
    base = 1 if parts[0] == "src" else 0
    return "/".join(parts) if len(parts) - base <= 1 else "/".join(parts[:base + 1])


def gated_roots_live(repo: Path, bundle: Any, man: Any) -> list[str]:
    """Roots (see `root_for_file`) of the code behind the bundle's gated commands, from the live command objects: the file
    the function's code comes from, and the module it claims to be in. Needs the bundle imported."""
    gated = man.gated_effects()
    roots: set[str] = set()
    for cmd in bundle.commands.values():
        if cmd.effect not in gated:
            continue
        fn = inspect.unwrap(cmd.func)
        code = getattr(fn, "__code__", None)
        if code is not None:
            r = root_for_file(repo, code.co_filename)
            if r:
                roots.add(r)
        mod = sys.modules.get(getattr(fn, "__module__", "") or "")
        r = root_for_file(repo, getattr(mod, "__file__", None))
        if r:
            roots.add(r)
    return sorted(roots)


def loaded_repo_files(repo: Path) -> list[str]:
    """Repo-relative files of every module in this process that was loaded from inside the repo (bytecode caches excluded)."""
    prefix = str(repo) + os.sep
    out: set[str] = set()
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not isinstance(f, str) or not os.path.isabs(f):
            continue
        f = os.path.abspath(f)
        if f.startswith(prefix) and f"{os.sep}{PYCACHE}{os.sep}" not in f:
            out.add(f[len(prefix):].replace(os.sep, "/"))
    return sorted(out)


def clean_relpaths(items: object) -> list[str]:
    """Only the entries of an untrusted list that are plain repo-relative paths (no `..`, nothing absolute, no control
    characters): what a sandboxed child reports about a repo is data to check, never a path to go and walk."""
    from .pathnorm import PathRejected, normalize_relpath

    out = []
    for item in items if isinstance(items, list) else []:
        try:
            if isinstance(item, str):
                out.append(normalize_relpath(item))
        except PathRejected:
            continue
    return out


def coverage_problems(repo: Path, man: Any, roots: Iterable[str], loaded: Iterable[str] = ()) -> list[str]:
    """Importable code behind a gated command that is neither locked nor reshape: every importable file under each root,
    every symlink in a root, and every loaded repo module. Sorted, repo-relative. Empty means covered."""
    bad: set[str] = set()
    roots, loaded = clean_relpaths(list(roots)), clean_relpaths(list(loaded))
    for root in roots:
        files, links = importable_under(repo, root)
        bad.update(links)
        bad.update(f for f in files if man.classify(f) not in COVERED_TIERS)
    bad.update(f for f in loaded if importable(os.path.basename(f)) and man.classify(f) not in COVERED_TIERS)
    return sorted(bad)
