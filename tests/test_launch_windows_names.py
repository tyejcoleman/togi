"""Fix pass 2 of the approval desk (found by a re-check of c787208): the launcher closes the three module names that exist
only on Windows, so a file of that name on a LATER sys.path entry can never load into the process that receives a passphrase.

The standard library probes for them as it starts (`subprocess` does `try: import msvcrt, _winapi`, `mimetypes` does
`try: import winreg`) and carries on if they are missing. On Linux and macOS they are always missing, so the probe walks
all of sys.path: past site-packages, to wherever a `.pth` file put another project's `src`. A planted `msvcrt.py` there ran
inside `togi approve` (and every other togi command). `-I` removes the current directory and PYTHONPATH, not that.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import SRC, clean_env

LAUNCH = SRC / "togi" / "_launch.py"
NAMES = ("winreg", "_winapi", "msvcrt")
# stands in for `-I` plus a .pth file: an unrelated directory AFTER the standard library and site-packages
WRAP = """
import runpy, sys
plant, launcher = sys.argv[1], sys.argv[2]
sys.argv = [launcher] + sys.argv[3:]
sys.path.append(plant)
runpy.run_path(launcher, run_name="__main__")
"""


def plant(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / "PLANTED"
    for n in NAMES:
        (directory / f"{n}.py").write_text(f"open({str(marker)!r}, 'a').write({n!r} + '\\n')\nraise ModuleNotFoundError('planted')\n")
    return marker


@pytest.mark.skipif(sys.platform == "win32", reason="these modules are real on Windows")
@pytest.mark.parametrize("argv", [["approve", "ap_0123456789"], ["approvals"], ["doctor"]])
def test_a_module_named_like_a_windows_only_one_on_a_later_path_entry_never_loads(home, tmp_path, argv):
    """`togi approve` is where the passphrase is typed; the others import the same modules."""
    marker = plant(tmp_path / "plant")
    r = subprocess.run([sys.executable, "-I", "-c", WRAP, str(tmp_path / "plant"), str(LAUNCH), *argv], env=clean_env(home),
                       cwd=str(tmp_path), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert "internal_error" not in r.stdout, r.stdout
    assert not marker.exists(), marker.read_text()


@pytest.mark.skipif(sys.platform == "win32", reason="these modules are real on Windows")
def test_the_probe_is_real_without_the_launcher_closing_the_names(home, tmp_path):
    """The control: the same plant IS found when nothing closes the names (so the test above is not vacuous)."""
    marker = plant(tmp_path / "plant")
    code = "import sys; sys.path.append(sys.argv[1]); import subprocess"
    subprocess.run([sys.executable, "-I", "-c", code, str(tmp_path / "plant")], env=clean_env(home), cwd=str(tmp_path),
                   capture_output=True, text=True, timeout=60)
    assert marker.exists()


def test_the_launcher_closes_the_names_before_any_other_import():
    tree = ast.parse(LAUNCH.read_text())
    body = tree.body[1:] if isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant) else tree.body
    first = next(n for n in body if isinstance(n, (ast.Import, ast.ImportFrom)))
    assert isinstance(first, ast.Import) and [a.name for a in first.names] == ["sys"]
    closing = body[body.index(first) + 1]
    text = ast.unparse(closing)
    assert "sys.modules.update" in text and all(n in text for n in NAMES) and "win32" in text
    assert not [n for n in body[:body.index(closing)] if n is not first and isinstance(n, (ast.Import, ast.ImportFrom))]


def test_the_readme_threat_model_says_what_a_plain_copy_of_your_approver_is():
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    model = " ".join(readme[readme.index("**Threat model: read this first.**"):readme.index("The sandbox below narrows")].replace("*", "").split())
    assert "plain copy" in model and "is not detected" in model and "test scope" in model and "Pretend PayPal" in model
