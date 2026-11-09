"""The launcher says plainly that Togi needs Python 3.11 or newer, instead of ending in a traceback about `tomllib`.

The stock python3 on macOS is 3.9. Togi imports `tomllib` (3.11+) on its first import, so every command used to fail with
`ModuleNotFoundError: No module named 'tomllib'`. The check sits at the top of `_launch.py`, before anything else runs.
"""
from __future__ import annotations

import ast
import subprocess
import sys

from conftest import SRC, clean_env

LAUNCH = SRC / "togi" / "_launch.py"
# Runs the launcher as __main__ while `sys.version_info` claims to be an old Python.
FAKE_OLD = """
import runpy, sys
sys.version_info = (3, 9, 6, "final", 0)
sys.argv = [sys.argv[1], "skill"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def test_an_old_python_gets_a_plain_message_and_no_traceback(tmp_path):
    p = subprocess.run([sys.executable, "-I", "-c", FAKE_OLD, str(LAUNCH)], capture_output=True, text=True,
                       env=clean_env(tmp_path / "home"), cwd=tmp_path, timeout=30)
    assert p.returncode == 1 and p.stdout == ""
    assert "Togi needs Python 3.11 or newer" in p.stderr and "3.9.6" in p.stderr
    assert "Traceback" not in p.stderr and "tomllib" not in p.stderr


def test_the_version_check_comes_before_any_import_but_sys():
    """Nothing before the check may need a newer Python (or import Togi), or the check never gets to speak."""
    tree = ast.parse(LAUNCH.read_text(encoding="utf-8"))
    body = [n for n in tree.body if not (isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant))]
    check = next(i for i, n in enumerate(body) if isinstance(n, ast.If) and "version_info" in ast.unparse(n.test))
    imports = [ast.unparse(n) for n in body[:check] if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert imports == ["import sys"]


def test_a_current_python_still_starts(tmp_path):
    p = subprocess.run([sys.executable, "-I", str(LAUNCH), "--version"], capture_output=True, text=True,
                       env=clean_env(tmp_path / "home"), cwd=tmp_path, timeout=30)
    assert p.returncode == 0 and "Traceback" not in p.stderr
