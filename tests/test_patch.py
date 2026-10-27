"""The tolerant unified-diff applier and tier classification."""
from __future__ import annotations

from pathlib import Path

import pytest

from togi import patch
from togi.manifest import glob_match, load_manifest

from conftest import EXAMPLE

BASE = "line1\nline2\nline3\nline4\nline5\nline6\n"


def one(diff):
    (fp,) = patch.parse(diff)
    return fp


def test_apply_simple_git_style_diff():
    d = "diff --git a/f.txt b/f.txt\nindex 1..2 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -2,3 +2,3 @@\n line2\n-line3\n+LINE3\n line4\n"
    fp = one(d)
    assert fp.path == "f.txt" and (fp.added(), fp.removed()) == (1, 1)
    assert patch.apply_to_text(BASE, fp) == "line1\nline2\nLINE3\nline4\nline5\nline6\n"


def test_wrong_line_numbers_and_counts_are_tolerated():
    d = "--- a/f.txt\n+++ b/f.txt\n@@ -40,9 +40,9 @@\n line5\n-line6\n+SIX\n"
    assert patch.apply_to_text(BASE, one(d)).endswith("line5\nSIX\n")


def test_blank_context_line_without_leading_space():
    text = "a\n\nb\n"
    d = "--- a/f\n+++ b/f\n@@ -1,3 +1,3 @@\n a\n\n-b\n+B\n"
    assert patch.apply_to_text(text, one(d)) == "a\n\nB\n"


def test_trailing_whitespace_fuzz():
    d = "--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n line1\n-line2\n+X\n"
    assert patch.apply_to_text("line1   \nline2\nz\n", one(d)) == "line1   \nX\nz\n"


def test_new_file_and_reverse():
    d = "--- /dev/null\n+++ b/new.md\n@@ -0,0 +1,2 @@\n+hello\n+world\n"
    fp = one(d)
    assert fp.is_new and patch.apply_to_text(None, fp) == "hello\nworld\n"
    assert patch.apply_to_text("hello\nworld\n", fp, reverse=True) == ""


def test_reverse_restores_original_exactly():
    d = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,4 @@\n line1\n+inserted\n line2\n-line3\n+three\n"
    fp = one(d)
    new = patch.apply_to_text(BASE, fp)
    assert new != BASE
    assert patch.apply_to_text(new, fp, reverse=True) == BASE


def test_missing_context_is_an_error():
    d = "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-nothere\n+x\n"
    with pytest.raises(patch.PatchError, match="does not apply"):
        patch.apply_to_text(BASE, one(d))


def test_multiple_hunks_apply_in_order():
    d = "--- a/f\n+++ b/f\n@@ -1,1 +1,1 @@\n-line1\n+ONE\n@@ -6,1 +6,1 @@\n-line6\n+SIX\n"
    assert patch.apply_to_text(BASE, one(d)) == "ONE\nline2\nline3\nline4\nline5\nSIX\n"


def test_unsupported_and_garbage_diffs():
    for bad in ["rename from a\nrename to b\n", "GIT binary patch\n", "just words\n", "--- a/f\n+++ b/f\n"]:
        with pytest.raises(patch.PatchError):
            patch.parse(bad)


def test_safe_relpath(tmp_path):
    assert patch.safe_relpath("skills/SKILL.md", tmp_path) == "skills/SKILL.md"
    for bad in ["/etc/passwd", "../x", "a/../../x", "~/x", "a\\b"]:
        with pytest.raises(patch.PatchError):
            patch.safe_relpath(bad, tmp_path)
    (tmp_path / "link").symlink_to("/tmp")
    with pytest.raises(patch.PatchError):
        patch.safe_relpath("link/escape.txt", tmp_path)


def test_glob_semantics():
    assert glob_match("skills/**", "skills/a.md") and glob_match("skills/**", "skills/x/y.md")
    assert not glob_match("skills/**", "skillsx/a.md")
    assert glob_match("src/*.py", "src/a.py") and not glob_match("src/*.py", "src/sub/a.py")
    assert glob_match("**/x.toml", "x.toml") and glob_match("**/x.toml", "a/b/x.toml")
    assert glob_match("togi.toml", "togi.toml") and not glob_match("togi.toml", "sub/togi.toml")
    assert glob_match("a/?.md", "a/b.md") and not glob_match("a/?.md", "a/bc.md")


def test_tier_classification_and_implicit_locks():
    man = load_manifest(EXAMPLE)
    c = man.classify
    assert c("skills/SKILL.md") == "polish" and c("togi_messages.toml") == "polish"
    assert c("src/notes/format.py") == "reshape"
    assert c("src/notes/store.py") == "locked" and c("togi.toml") == "locked"
    assert c("stone/tasks/add-and-find.toml") == "locked" and c("tests/test_notes.py") == "locked"
    assert c(".togi/audit.jsonl") == "locked" and c(".togi/locked.sha256") == "locked"
    assert c("README.md") is None and c("src/notes/togi_bundle.py") is None


def test_locked_wins_over_polish_and_reshape(tmp_path):
    (tmp_path / "togi.toml").write_text(
        '[bundle]\nname="x"\nmodule="x:b"\n[tiers]\npolish=["**"]\nreshape=["src/**"]\nlocked=["src/core.py"]\n')
    man = load_manifest(tmp_path)
    assert man.classify("src/core.py") == "locked" and man.classify("src/other.py") == "reshape"
    assert man.classify("notes.md") == "polish"
    # even a greedy polish glob cannot expose the manifest, the stone, or the audit log
    assert man.classify("togi.toml") == "locked" and man.classify("stone/tasks/a.toml") == "locked"
    assert man.classify(".togi/audit.jsonl") == "locked"
