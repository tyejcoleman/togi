"""A small, tolerant unified-diff parser and applier (stdlib only).

Hone proposals come from a language model, so line numbers and hunk counts are often slightly off. This
applier ignores the counts, finds each hunk by its context (nearest to the stated line), and refuses anything
ambiguous or unsupported (renames, deletions, binary patches, paths outside the repo).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .pathnorm import PathRejected, escape_text, normalize_relpath


class PatchError(Exception):
    pass


class BadPathError(PatchError):
    """A diff names a path that is unsafe to judge or write. `.code` is `outside_repo` or `bad_path`."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


@dataclass
class Hunk:
    old_start: int
    new_start: int
    lines: list[tuple[str, str]] = field(default_factory=list)  # (' ' | '+' | '-', text)


@dataclass
class FilePatch:
    path: str
    old_path: str | None
    new_path: str | None
    hunks: list[Hunk] = field(default_factory=list)
    is_new: bool = False
    is_delete: bool = False

    def added(self) -> int:
        return sum(1 for h in self.hunks for k, _ in h.lines if k == "+")

    def removed(self) -> int:
        return sum(1 for h in self.hunks for k, _ in h.lines if k == "-")


_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _clean_path(raw: str) -> str:
    raw = raw.split("\t")[0].strip()
    if len(raw) > 1 and raw[0] == '"' and raw[-1] == '"':
        raw = raw[1:-1]
    return raw


def parse(diff: str) -> list[FilePatch]:
    text = diff.replace("\r\n", "\n")
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    out: list[FilePatch] = []
    i, n = 0, len(lines)
    pending_new = pending_delete = False
    while i < n:
        ln = lines[i]
        if ln.startswith("rename from") or ln.startswith("rename to") or ln.startswith("copy from"):
            raise PatchError("renames and copies are not supported")
        if ln.startswith("GIT binary patch") or ln.startswith("Binary files"):
            raise PatchError("binary patches are not supported")
        if ln.startswith("new file mode"):
            pending_new = True
        elif ln.startswith("deleted file mode"):
            pending_delete = True
        elif ln.startswith("diff "):
            pending_new = pending_delete = False
        if ln.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ "):
            old_raw, new_raw = _clean_path(ln[4:]), _clean_path(lines[i + 1][4:])
            is_new = old_raw == "/dev/null" or pending_new
            is_delete = new_raw == "/dev/null" or pending_delete
            old_p = None if old_raw == "/dev/null" else old_raw
            new_p = None if new_raw == "/dev/null" else new_raw
            if old_p and new_p and old_p.startswith("a/") and new_p.startswith("b/"):
                old_p, new_p = old_p[2:], new_p[2:]
            elif old_p is None and new_p and new_p.startswith("b/"):  # git style new file
                new_p = new_p[2:]
            elif new_p is None and old_p and old_p.startswith("a/"):  # git style deletion
                old_p = old_p[2:]
            path = new_p or old_p
            if not path:
                raise PatchError("diff header without a file path")
            fp = FilePatch(path=path, old_path=old_p, new_path=new_p, is_new=bool(is_new), is_delete=bool(is_delete))
            i += 2
            while i < n:
                m = _HUNK.match(lines[i])
                if not m:
                    break
                hunk = Hunk(int(m.group(1)), int(m.group(2)))
                i += 1
                while i < n:
                    body = lines[i]
                    if body.startswith("@@") or body.startswith("diff ") or (
                            body.startswith("--- ") and i + 1 < n and lines[i + 1].startswith("+++ ")):
                        break
                    if body.startswith("\\"):  # "\ No newline at end of file"
                        i += 1
                        continue
                    if body == "":
                        hunk.lines.append((" ", ""))  # a model dropped the leading space of a blank context line
                    elif body[0] in " +-":
                        hunk.lines.append((body[0], body[1:]))
                    else:
                        break
                    i += 1
                if not hunk.lines:
                    raise PatchError("empty hunk")
                fp.hunks.append(hunk)
            if not fp.hunks:
                raise PatchError(f"{path}: file header without any hunks")
            out.append(fp)
            pending_new = pending_delete = False
            continue
        i += 1
    if not out:
        raise PatchError("no file changes found in the diff (expected unified diff with ---/+++ headers and @@ hunks)")
    return out


def safe_relpath(path: str, repo: Path) -> str:
    """Repo-relative POSIX path (`.`, `..` and `//` resolved), or BadPathError if it escapes the repo, carries
    control/format/line-separator characters, or resolves outside the repo through a symlink."""
    try:
        norm = normalize_relpath(path)
    except PathRejected as e:
        raise BadPathError(str(e), e.code) from None
    resolved = (repo / norm).resolve()
    root = repo.resolve()
    if resolved != root and root not in resolved.parents:
        raise BadPathError(f"path {escape_text(path, 120)!r} resolves outside the repository", "outside_repo")
    return norm


def _split(text: str) -> tuple[list[str], bool]:
    if text == "":
        return [], True
    nl = text.endswith("\n")
    body = text[:-1] if nl else text
    return body.split("\n"), nl


def apply_to_text(text: str | None, fp: FilePatch, reverse: bool = False) -> str:
    lines, ends_nl = _split(text or "")
    delta = 0
    floor = 0
    for hi, h in enumerate(fp.hunks, 1):
        swap = {" ": " ", "+": "-", "-": "+"} if reverse else {" ": " ", "+": "+", "-": "-"}
        eff = [(swap[k], t) for k, t in h.lines]
        old = [t for k, t in eff if k in (" ", "-")]
        stated = max((h.new_start if reverse else h.old_start) - 1 + delta, 0)
        pos = _find(lines, old, stated, floor)
        if pos is None:
            raise PatchError(f"{fp.path}: hunk {hi} does not apply (the context was not found; the file may have "
                             "changed since the proposal was written)")
        seg = lines[pos: pos + len(old)]
        new, oi = [], 0
        for k, t in eff:  # keep the file's own text for context lines (matters after whitespace-fuzzy matches)
            if k == " ":
                new.append(seg[oi])
                oi += 1
            elif k == "-":
                oi += 1
            else:
                new.append(t)
        lines[pos: pos + len(old)] = new
        delta += len(new) - len(old)
        floor = pos + len(new)
    return "\n".join(lines) + ("\n" if ends_nl or not lines else "") if lines else ""


def _find(lines: list[str], block: list[str], stated: int, floor: int) -> int | None:
    if not block:
        return min(max(stated, floor), len(lines))
    for strip in (False, True):
        cands = []
        for p in range(floor, len(lines) - len(block) + 1):
            seg = lines[p: p + len(block)]
            if (seg == block) if not strip else ([s.rstrip() for s in seg] == [b.rstrip() for b in block]):
                cands.append(p)
        if cands:
            return min(cands, key=lambda p: abs(p - stated))
    return None


def reverse_diff(diff: str) -> str:
    """Textual reverse of a diff (for display); apply_to_text(reverse=True) is what revert uses."""
    out = []
    for fp in parse(diff):
        out.append(f"--- {fp.new_path or '/dev/null'}\n+++ {fp.old_path or '/dev/null'}")
        for h in fp.hunks:
            out.append(f"@@ -{h.new_start} +{h.old_start} @@")
            for k, t in h.lines:
                out.append({" ": " ", "+": "-", "-": "+"}[k] + t)
    return "\n".join(out) + "\n"
