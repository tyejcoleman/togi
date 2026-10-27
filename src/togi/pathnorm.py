"""Path and text normalisation shared by the tier classifier, the diff parser and the approval prompt.

One normaliser, used everywhere a repo-relative path is judged, so the classifier, the patch applier and the
audit log can never disagree about which file a name means:

- `normalize_relpath`  resolves `.`, `..` and duplicate slashes, and rejects anything that leaves the repo, or
  that carries control characters, format characters (bidi overrides, zero-width), or Unicode line and
  paragraph separators. Those make a name that looks like one file and is another.
- `fold`               the form used for matching: NFKC + casefold (+ NFKC again). Used on both the pattern
  and the path, because macOS (APFS) and Windows are case-insensitive and APFS is also normalisation-
  insensitive: `skills/MONEY.md` and `skills/money.md` are the same file there.
- `escape_text`        makes untrusted text safe to show on a person's terminal (control characters, ANSI
  escapes and bidi overrides become `\\uXXXX`).
"""
from __future__ import annotations

import unicodedata

# General categories that never belong in a repo path: control, format (bidi marks/overrides, zero-width),
# line separator, paragraph separator, surrogate.
BAD_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Cs"})
MAX_PATH_CHARS = 1024


class PathRejected(ValueError):
    """A path that cannot be a safe repo-relative file name. `.code` is a stable slug."""

    def __init__(self, message: str, code: str = "bad_path"):
        super().__init__(message)
        self.code = code


def is_unsafe_char(ch: str) -> bool:
    return unicodedata.category(ch) in BAD_CATEGORIES


def escape_text(text: object, cap: int | None = None, keep: str = "") -> str:
    """Display-safe text: every control/format/separator character is shown as \\uXXXX (\\UXXXXXXXX beyond the
    BMP). Printable text, including non-ASCII letters, is left alone. `cap` truncates (with a visible marker);
    `keep` lists control characters to let through (e.g. "\\n\\t" for multi-line documents)."""
    s = text if isinstance(text, str) else str(text)
    extra = 0
    if cap is not None and len(s) > cap:  # cut the source, not the escaped form: never half an escape sequence
        extra, s = len(s) - cap, s[:cap]
    out = []
    for ch in s:
        if (is_unsafe_char(ch) or ch == "\x7f") and ch not in keep:
            cp = ord(ch)
            out.append(f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\U{cp:08x}")
        else:
            out.append(ch)
    return "".join(out) + (f"...[+{extra} chars]" if extra else "")


def printable_token(s: str, max_len: int = 40) -> bool:
    """True when `s` is short and has nothing in it that could alter what a terminal shows."""
    return 0 < len(s) <= max_len and not any(is_unsafe_char(c) or c == "\x7f" or c.isspace() for c in s)


def fold(s: str) -> str:
    """Comparison form: NFKC, casefold, NFKC (the 'canonical caseless' recipe with compatibility folding)."""
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", s).casefold())


def normalize_relpath(path: str) -> str:
    """Repo-relative POSIX path with `.`/`..`/`//` resolved. Raises PathRejected (code `outside_repo` or
    `bad_path`). Does not touch the filesystem (symlinks are the caller's concern; see apply.classify_diff)."""
    if not isinstance(path, str) or not path:
        raise PathRejected("empty path", "bad_path")
    if len(path) > MAX_PATH_CHARS:
        raise PathRejected(f"path is longer than {MAX_PATH_CHARS} characters", "bad_path")
    for ch in path:
        if is_unsafe_char(ch) or ch == "\x7f":
            raise PathRejected(f"path {escape_text(path, 120)!r} contains a control, format or line-separator "
                               f"character (U+{ord(ch):04X})", "bad_path")
    if "\\" in path:
        raise PathRejected(f"path {path!r} contains a backslash", "bad_path")
    if path.startswith(("/", "~")):
        raise PathRejected(f"path {path!r} is absolute or unusual", "outside_repo")
    parts: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise PathRejected(f"path {path!r} escapes the repository", "outside_repo")
            parts.pop()
            continue
        parts.append(part)
    if not parts:
        raise PathRejected(f"path {path!r} names the repository itself", "bad_path")
    return "/".join(parts)
