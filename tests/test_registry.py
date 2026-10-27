"""Registry: add/remove/bundles, local override, lazy import, sys.path, launcher, speed."""
from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from conftest import ROOT, clean_env, run_togi, togi, togi_code


def make_repo(tmp_path, name="demo", body=None, summary="A demo.", extra_toml=""):
    repo = tmp_path / f"{name}_repo"
    (repo / "src" / name).mkdir(parents=True)
    (repo / "togi.toml").write_text(
        f'[bundle]\nname = "{name}"\nversion = "1.2.3"\nsummary = "{summary}"\nmodule = "{name}.tb:bundle"\n{extra_toml}')
    (repo / "src" / name / "__init__.py").write_text("")
    (repo / "src" / name / "tb.py").write_text(body or (
        f"from togi import Bundle\nbundle = Bundle('{name}')\n"
        "@bundle.command('hello', effect='read')\ndef hello(who: str = 'world') -> dict:\n"
        "    '''Say hello.'''\n    return {'hello': who}\n"))
    return repo


def test_add_bundles_remove(home, tmp_path):
    repo = make_repo(tmp_path)
    out = togi("add", str(repo))
    assert out["data"]["added"] == "demo" and out["next"] == "togi skill demo"
    rows = togi("bundles")["data"]
    assert rows == [{"name": "demo", "path": str(repo.resolve()), "source": "global", "version": "1.2.3",
                     "summary": "A demo.", "ok": True}]
    assert togi("demo", "hello")["data"] == {"hello": "world"}
    assert (home / "bundles.toml").read_text().count("[bundles.") == 1
    togi("remove", "demo")
    assert togi("bundles")["data"] == []
    code, out = togi_code("remove", "demo")
    assert code == 1 and out["error"]["code"] == "unknown_bundle"


def test_add_errors(home, tmp_path):
    code, out = togi_code("add", str(tmp_path / "missing"))
    assert out["error"]["code"] == "path_not_found"
    (tmp_path / "empty").mkdir()
    code, out = togi_code("add", str(tmp_path / "empty"))
    assert out["error"]["code"] == "manifest_invalid"
    repo = make_repo(tmp_path, name="skill")
    code, out = togi_code("add", str(repo))
    assert out["error"]["code"] == "reserved_name"


def test_project_local_registry_overrides_global(home, tmp_path, monkeypatch):
    g = make_repo(tmp_path / "g", name="demo", summary="global one")
    (tmp_path / "proj").mkdir()
    l = make_repo(tmp_path / "proj", name="demo", summary="local one")
    togi("add", str(g))
    monkeypatch.chdir(tmp_path / "proj")
    togi("add", str(l), "--local")
    assert (tmp_path / "proj" / ".togi" / "bundles.toml").is_file()
    rows = togi("bundles")["data"]
    assert len(rows) == 1 and rows[0]["source"] == "local" and rows[0]["summary"] == "local one"
    monkeypatch.chdir(tmp_path)  # outside the project: global again
    assert togi("bundles")["data"][0]["summary"] == "global one"


def test_bundles_are_imported_lazily(home, tmp_path):
    repo = make_repo(tmp_path, body="raise RuntimeError('import exploded')\n")
    (repo / "skills").mkdir()
    (repo / "skills" / "SKILL.md").write_text("---\nname: demo\nsummary: x\n---\n# demo\nHello skill.\n")
    togi("add", str(repo))
    assert "- demo: A demo." in togi("skill")  # index never imports
    assert "Hello skill." in togi("skill", "demo")  # SKILL.md never imports
    assert togi("bundles")["data"][0]["ok"] is True
    code, out = togi_code("demo", "hello")
    assert code == 1 and out["error"]["code"] == "bundle_import_failed" and "import exploded" in out["error"]["message"]
    assert out["next"] == "togi doctor"


def test_import_puts_repo_src_and_repo_on_sys_path(home, tmp_path):
    repo = make_repo(tmp_path, body=(
        "import sys, helper_at_root\nfrom togi import Bundle\nbundle = Bundle('demo')\n"
        "@bundle.command('paths', effect='read')\ndef paths() -> dict:\n    '''Paths.'''\n"
        "    return {'src_first': sys.path[0].endswith('src'), 'helper': helper_at_root.X}\n"))
    (repo / "helper_at_root.py").write_text("X = 7\n")
    togi("add", str(repo))
    r = run_togi(home, "demo", "paths")
    assert r.returncode == 0, r.stderr
    assert '"helper":7' in r.stdout and '"src_first":true' in r.stdout


def test_name_mismatch_is_an_error(home, tmp_path):
    repo = make_repo(tmp_path, body="from togi import Bundle\nbundle = Bundle('other')\n")
    togi("add", str(repo))
    code, out = togi_code("demo", "hello")
    assert out["error"]["code"] == "name_mismatch"


def test_unknown_bundle_lists_next(home):
    code, out = togi_code("tools", "ghost")
    assert out["error"]["code"] == "unknown_bundle" and out["next"] == "togi bundles"


def test_launcher_runs_python_with_pythonpath_and_follows_symlinks(home, tmp_path):
    link = tmp_path / "bin" / "togi"
    link.parent.mkdir()
    link.symlink_to(ROOT / "bin" / "togi")
    env = clean_env(home)
    env.pop("PYTHONPATH")  # the launcher must supply it
    r = subprocess.run([str(link), "version"], capture_output=True, text=True, env=env)
    assert r.returncode == 0 and '"togi":"0.1.0"' in r.stdout
    r = subprocess.run([str(link), "skill"], capture_output=True, text=True, env=env)
    assert r.stdout.startswith("Togi:")


def test_skill_and_tools_are_fast(home, tmp_path):
    repo = make_repo(tmp_path)
    togi("add", str(repo))
    env = clean_env(home)

    def best(args):
        times = []
        for _ in range(5):
            t = time.perf_counter()
            subprocess.run([sys.executable, "-m", "togi", *args], capture_output=True, env=env)
            times.append(time.perf_counter() - t)
        return min(times)

    base = min(_py_startup() for _ in range(5))
    # spec: < 150 ms on a laptop. Allow python's own startup variance on busy CI machines.
    assert best(["skill"]) < max(0.15, base + 0.1)
    assert best(["tools", "demo"]) < max(0.15, base + 0.1)


def _py_startup():
    t = time.perf_counter()
    subprocess.run([sys.executable, "-c", "pass"], capture_output=True)
    return time.perf_counter() - t
