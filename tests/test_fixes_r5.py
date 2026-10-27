"""Fix round 5 (FIXES-r5.md, the fifth design review of 12b4416): one section per item, each with tests that FAIL on 12b4416
and pass now.

    H1  the lock covered file paths, not the code Python imports      -> lock set = importable set; gated-code coverage;
                                                                         source-only imports; repo paths after the stdlib
    H2  `togi approve` imported the bundle before the person decided  -> signed describe snapshot; hardened sandboxed child
    M1  approver files shared a directory with $TOGI_HOME             -> <pw_dir>/.togi/approver.d (0700), r4 migration
    M2  the lock prompt did not say what changed                      -> changed/missing/new + head + audit entries, "relock"
    M3  a bundle's stderr was unbounded                               -> captured with the stdout cap, CLI and MCP
    L1  the approve prompt showed the requester's expiry              -> the expiry that is signed
    L2  a truncated argument could be approved                        -> prompt_too_long
    L3  `togi run --max-output` went past the --full limit            -> clamped

Nothing here touches the real approver: the throwaway one comes from the `home` fixture, the "fixed" one from the faked
password database (see conftest).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import py_compile
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from togi import approvals, approver, audit, host, paths, registry, sandbox, spent
from togi.errors import TogiError
from togi.manifest import load_manifest, locked_digest

try:          # (so the file can be run against 12b4416, where these do not exist, to see each test fail there)
    from togi import codeset, describe
    from togi.manifest import scan_locked
except ImportError:                                                    # pragma: no cover
    codeset = describe = scan_locked = None

import conftest
from conftest import (PASSPHRASE, ROOT, SRC, answers, clean_env, human, lock_bundle, run_in_pty, run_togi, togi,
                      togi_code)

needs_sandbox = pytest.mark.skipif(not sandbox.available()[0], reason="no sandbox-exec here")
PYC_TAG = sys.implementation.cache_tag


# ----------------------------------------------------------------------------- shared helpers


def set_tiers(repo: Path, *, locked=None, reshape=None, polish=None) -> None:
    """Rewrite the [tiers] table of a bundle's togi.toml (a person re-locks afterwards)."""
    p = repo / "togi.toml"
    text = p.read_text()
    old = load_manifest(repo)
    pick = lambda new, cur: cur if new is None else new   # noqa: E731
    table = "[tiers]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in (
        ("polish", pick(polish, old.polish)), ("reshape", pick(reshape, old.reshape)), ("locked", pick(locked, old.locked))))
    new_text, n = re.subn(r"\[tiers\]\n.*?(?=\n\[stone\])", table, text, flags=re.S)
    assert n == 1
    p.write_text(new_text)


def tip_request(amount: str = "5.00", note: str = "n_1"):
    return togi_code("notes", "tip", "send", "--id", note, "--amount", amount)


def edit_approval(home: Path, aid: str, fn) -> dict:
    p = home / "approvals" / f"{aid}.json"
    rec = json.loads(p.read_text())
    fn(rec)
    p.write_text(json.dumps(rec, indent=1, sort_keys=True))
    return rec


@pytest.fixture
def pending(notes, home):
    """A locked notes bundle, one note, and a pending tip approval request. Returns the approval id."""
    lock_bundle("notes")
    togi("notes", "add", "--text", "tip me")
    code, out = tip_request()
    assert code == 3, out
    return out["approval"]["id"]


def approve_in_pty(home, aid, word="n_1", **kw):
    return human(home, ["approve", aid], word, **kw)


def marker_in(tmp_path: Path, name: str = "MARK") -> Path:
    return tmp_path / name


# ============================================================================= H1: what is hashed is what is importable


def test_h1_the_example_locks_its_gated_module_and_package(notes_repo):
    man = load_manifest(notes_repo)
    for rel in ("src/notes/togi_bundle.py", "src/notes/__init__.py", "src/notes/store.py", "src/notes/messages.py"):
        assert man.classify(rel) == "locked", rel
    assert man.classify("src/notes/format.py") == "reshape"               # reshape code in the package stays allowed
    files, _ = codeset.importable_under(notes_repo, "src/notes")
    assert files and all(man.classify(f) in ("locked", "reshape") for f in files)


def test_h1_money_is_refused_while_the_module_with_the_money_command_is_unlocked(notes, home, notes_repo):
    """The review's probe: tip_send's own module and the package __init__ were in no tier, so they could change after the
    lock and the lock still said VERIFIED."""
    set_tiers(notes_repo, locked=["togi.toml", "stone/**", "tests/**", "src/notes/store.py", "src/notes/messages.py"])
    lock_bundle("notes")
    togi("notes", "add", "--text", "tip me")
    code, out = tip_request()
    assert code == 2 and out["error"]["code"] == "gated_code_unlocked", out
    assert "togi_bundle.py" in out["error"]["message"] and "__init__.py" in out["error"]["message"]
    assert "[tiers].locked" in out["next"] and "togi lock notes" in out["next"]
    assert togi("approvals")["data"] == []                                 # no request was even filed


def test_h1_editing_an_untiered_module_after_the_lock_no_longer_goes_unnoticed(notes, home, notes_repo):
    set_tiers(notes_repo, locked=["togi.toml", "stone/**", "tests/**", "src/notes/store.py", "src/notes/messages.py"])
    lock_bundle("notes")
    man = load_manifest(notes_repo)
    d0 = locked_digest(notes_repo, man)
    with open(notes_repo / "src/notes/togi_bundle.py", "a") as f:
        f.write("\n# an edit nobody signed for\n")
    assert locked_digest(notes_repo, man) == d0                            # (untiered: not in the digest) ...
    togi("notes", "add", "--text", "x")
    code, out = tip_request()
    assert code == 2 and out["error"]["code"] == "gated_code_unlocked"     # ... so the money path refuses instead


@pytest.mark.parametrize("what", ["gate-the-extra-module", "reshape-is-fine"])
def test_h1_a_new_importable_file_in_the_gated_package_must_be_tiered(notes, home, notes_repo, what):
    lock_bundle("notes")
    togi("notes", "add", "--text", "tip me")
    assert tip_request()[0] == 3                                          # fine as shipped
    (notes_repo / "src/notes/helper.py").write_text("# new, and nobody tiered it\n")
    code, out = tip_request("6.00")
    assert code == 2 and out["error"]["code"] == "gated_code_unlocked" and "helper.py" in out["error"]["message"]
    if what == "reshape-is-fine":                                         # reshape code needs the real approver to change
        set_tiers(notes_repo, reshape=["src/notes/format.py", "src/notes/helper.py"])
        lock_bundle("notes")
        assert tip_request("6.00")[0] == 3
    else:
        set_tiers(notes_repo, locked=load_manifest(notes_repo).locked + ["src/notes/helper.py"])
        lock_bundle("notes")
        assert tip_request("6.00")[0] == 3


@pytest.mark.parametrize("name", ["ext.so", "ext.pyd", "legacy.pyc", "site.pth", "pkg/__init__.py", "pkg/deep/mod.py"])
def test_h1_every_kind_of_importable_thing_counts(notes, home, notes_repo, name):
    lock_bundle("notes")
    togi("notes", "add", "--text", "tip me")
    f = notes_repo / "src/notes" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\0")
    code, out = tip_request()
    assert code == 2 and out["error"]["code"] == "gated_code_unlocked" and f.name in out["error"]["message"], out


def test_h1_a_symlink_inside_the_gated_package_is_refused(notes, home, notes_repo, tmp_path):
    lock_bundle("notes")
    togi("notes", "add", "--text", "tip me")
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "__init__.py").write_text("")
    (notes_repo / "src/notes/linked").symlink_to(other)                    # a directory link IS a package
    code, out = tip_request()
    assert code == 2 and out["error"]["code"] == "gated_code_unlocked" and "linked" in out["error"]["message"], out


def test_h1_a_repo_module_the_bundle_loads_from_outside_the_gated_package_must_be_tiered_too(notes, home, notes_repo):
    (notes_repo / "src/helperpkg").mkdir()
    (notes_repo / "src/helperpkg/__init__.py").write_text("VALUE = 1\n")
    tb = notes_repo / "src/notes/togi_bundle.py"
    tb.write_text(tb.read_text().replace("from .format import summarize", "from helperpkg import VALUE  # noqa: F401\nfrom .format import summarize"))
    lock_bundle("notes")
    togi("notes", "add", "--text", "tip me")
    code, out = tip_request()
    assert code == 2 and out["error"]["code"] == "gated_code_unlocked" and "helperpkg" in out["error"]["message"], out
    set_tiers(notes_repo, reshape=["src/notes/format.py", "src/helperpkg/**"])
    lock_bundle("notes")
    assert tip_request()[0] == 3


def test_h1_doctor_warns_about_untiered_gated_code(notes, home, notes_repo):
    lock_bundle("notes")
    ok = [r for r in togi("doctor", "notes")["data"]["checks"] if r["check"] == "notes: gated code locked"]
    assert ok and ok[0]["ok"] is True
    (notes_repo / "src/notes/helper.py").write_text("x = 1\n")
    rows = [r for r in togi("doctor", "notes")["data"]["checks"] if r["check"] == "notes: gated code locked"]
    assert rows and rows[0]["ok"] is False and rows[0]["level"] == "warn" and "helper.py" in rows[0]["detail"]
    assert "togi lock notes" in rows[0]["next"]


def test_h1_a_bundle_that_needs_no_lock_is_not_asked_for_coverage(home, tmp_path):
    repo = tmp_path / "plain"
    (repo / "src" / "plain").mkdir(parents=True)
    (repo / "togi.toml").write_text('[bundle]\nname = "plain"\nmodule = "plain.togi_bundle:bundle"\n'
                                    '[tiers]\npolish = ["skills/**"]\n[policy]\napprove_effects = ["money"]\n')
    (repo / "src" / "plain" / "__init__.py").write_text("")
    (repo / "src" / "plain" / "togi_bundle.py").write_text(
        'from togi import Bundle\nbundle = Bundle("plain")\n\n@bundle.command("pay", effect="money", examples=["x"])\n'
        'def pay(to: str) -> dict:\n    """Pay."""\n    return {"to": to}\n')
    togi("add", str(repo))
    assert load_manifest(repo).lock_required() is False
    assert togi_code("plain", "pay", "--to", "x")[0] == 3


# ---- the lock set covers what Python would import in place of a locked module

def _shadow_paths(repo: Path):
    ext = importlib.machinery.EXTENSION_SUFFIXES[0]
    return {
        "package-directory": ("src/notes/store/__init__.py", b"# a package beats store.py\n"),
        "extension-module": (f"src/notes/store{ext}", b"\0"),
        "plain-so": ("src/notes/store.so", b"\0"),
        "sourceless-pyc": ("src/notes/store.pyc", b"\0"),
        "pycache": (f"src/notes/__pycache__/store.{PYC_TAG}.pyc", b"inert"),
        "pycache-of-the-init": (f"src/notes/__pycache__/__init__.{PYC_TAG}.pyc", b"inert"),
    }


@pytest.mark.parametrize("kind", ["package-directory", "extension-module", "plain-so", "sourceless-pyc"])
def test_h1_something_that_shadows_a_locked_module_counts_as_a_change(notes, home, notes_repo, kind):
    """The review's probe, in three steps: an inert package directory beside the locked store.py, an extension module,
    and a file in __pycache__ left the digest, the lock and the approval binding exactly as they were."""
    lock_bundle("notes")
    togi("notes", "add", "--text", "tip me")
    man = load_manifest(notes_repo)
    d0 = locked_digest(notes_repo, man)
    assert audit.verify_locked(notes_repo, man)["ok"] is True
    rel, data = _shadow_paths(notes_repo)[kind]
    f = notes_repo / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    assert locked_digest(notes_repo, man) != d0, "the digest ignores what shadows the locked module"
    v = audit.verify_locked(notes_repo, man)
    assert v["ok"] is False and rel in v["unlisted"], v
    code, out = tip_request()
    assert code == 2 and out["error"]["code"] == "lock_check_failed" and f.name in out["error"]["message"], out


def test_h1_the_example_review_probe_steps_each_change_the_digest(notes_repo):
    """test_g2 of the reviewer's file, with the assertion turned around: d0 != d1 != d2 != d3."""
    man = load_manifest(notes_repo)
    d0 = locked_digest(notes_repo, man)
    with open(notes_repo / "src/notes/togi_bundle.py", "a") as f:
        f.write("\n# inert edit\n")
    d1 = locked_digest(notes_repo, man)
    (notes_repo / "src/notes/store").mkdir()
    (notes_repo / "src/notes/store/__init__.py").write_text("# inert\n")
    d2 = locked_digest(notes_repo, man)
    spec = importlib.machinery.PathFinder.find_spec("store", [str(notes_repo / "src/notes")])
    assert spec.origin.endswith("store/__init__.py")                       # what import really loads
    (notes_repo / "src/notes/__pycache__").mkdir()
    (notes_repo / "src/notes/__pycache__/store.cpython-312.pyc").write_bytes(b"inert")
    d3 = locked_digest(notes_repo, man)
    assert len({d0, d1, d2}) == 3
    assert d3 == d2      # bytecode in __pycache__ is never read by anything Togi runs, so it is not part of the lock


def test_h1_a_reshape_glob_cannot_launder_a_shadow(notes, home, notes_repo):
    """Tiers are `locked > reshape > polish` by path: with `reshape = src/notes/**` a planted store/__init__.py is
    'reshape' by glob. It still shadows a locked module, so it is in the lock set whatever the globs say."""
    set_tiers(notes_repo, reshape=["src/notes/**"],
              locked=["togi.toml", "stone/**", "tests/**", "src/notes/store.py", "src/notes/messages.py",
                      "src/notes/__init__.py", "src/notes/togi_bundle.py"])
    lock_bundle("notes")
    man = load_manifest(notes_repo)
    (notes_repo / "src/notes/store").mkdir()
    (notes_repo / "src/notes/store/__init__.py").write_text("# glob says reshape\n")
    assert man.classify("src/notes/store/__init__.py") == "reshape"
    assert "src/notes/store/__init__.py" in audit.verify_locked(notes_repo, man)["unlisted"]


def test_h1_no_skip_list_under_a_locked_pattern(notes, home, notes_repo):
    """`build`, `dist`, `venv` and `__pycache__` were never walked, so a locked glob did not cover what sat in them."""
    set_tiers(notes_repo, locked=["togi.toml", "stone/**", "tests/**", "src/notes/__init__.py", "src/notes/togi_bundle.py",
                                  "src/notes/store.py", "src/notes/messages.py", "src/vend/**"])
    (notes_repo / "src/vend").mkdir()
    (notes_repo / "src/vend/engine.py").write_text("x = 1\n")
    lock_bundle("notes")
    man = load_manifest(notes_repo)
    for d in ("build", "dist", "venv", ".venv", "__pycache__", "node_modules"):
        p = notes_repo / "src/vend" / d / "evil.py"
        p.parent.mkdir()
        p.write_text("# importable as vend." + d + ".evil\n")
        assert f"src/vend/{d}/evil.py" in audit.verify_locked(notes_repo, man)["unlisted"], d
    files, _ = scan_locked(notes_repo, man)
    assert all(f"src/vend/{d}/evil.py" in files for d in ("build", "dist", "venv", ".venv", "__pycache__", "node_modules"))


def test_h1_a_shadow_of_a_module_at_the_repo_root_can_sit_in_src(notes_repo):
    (notes_repo / "engine.py").write_text("x = 1\n")
    set_tiers(notes_repo, locked=load_manifest(notes_repo).locked + ["engine.py"])
    man = load_manifest(notes_repo)
    d0 = locked_digest(notes_repo, man)
    (notes_repo / "src/engine").mkdir()
    (notes_repo / "src/engine/__init__.py").write_text("# src is first on the import path\n")
    assert locked_digest(notes_repo, man) != d0


def test_h1_unrelated_files_and_directories_do_not_disturb_the_lock(notes, home, notes_repo):
    lock_bundle("notes")
    (notes_repo / "README.md").write_text("docs\n")
    (notes_repo / "src/notes/data").mkdir()                                # a directory with no __init__ is not importable
    (notes_repo / "src/notes/data/rows.json").write_text("[]")
    (notes_repo / "src/notes/__pycache__").mkdir(exist_ok=True)
    (notes_repo / f"src/notes/__pycache__/format.{PYC_TAG}.pyc").write_bytes(b"inert")   # format.py is reshape, not locked
    assert audit.verify_locked(notes_repo, load_manifest(notes_repo))["ok"] is True


# ---- source-only imports, and the repo after the standard library


def _compile_unchecked(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    py_compile.compile(str(src), cfile=str(dest), doraise=True,
                       invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)


def test_h1_a_planted_repo_pycache_is_never_read(notes, home, notes_repo, tmp_path):
    """An unchecked-hash .pyc is trusted by Python whatever the source says. One planted next to format.py (a reshape
    file the lock does not hash) ran its payload the next time the bundle loaded."""
    mark = marker_in(tmp_path)
    evil = tmp_path / "format.py"
    evil.write_text(f"open({str(mark)!r}, 'w').write('ran from a planted pyc')\n"
                    "def summarize(note):\n    return {'id': note['id'], 'text': 'pwned', 'tags': []}\n")
    cached = Path(importlib.util.cache_from_source(str(notes_repo / "src/notes/format.py")))
    _compile_unchecked(evil, cached)
    togi("notes", "add", "--text", "hello")
    r = run_togi(home, "notes", "list")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not mark.exists(), "the planted bytecode ran"
    assert json.loads(r.stdout)["data"][0]["text"] == "hello"              # the SOURCE was used


def test_h1_loading_a_bundle_writes_no_bytecode_into_the_repo(notes, home, notes_repo):
    for p in notes_repo.rglob("__pycache__"):
        shutil.rmtree(p)
    r = run_togi(home, "notes", "list", env_extra={"PYTHONDONTWRITEBYTECODE": ""})
    assert r.returncode == 0, r.stderr
    assert not list(notes_repo.rglob("*.pyc"))


def test_h1_the_import_settings_a_bundle_runs_under(notes, home):
    registry.clear_cache()
    registry.load("notes")
    assert sys.dont_write_bytecode is True
    assert sys.pycache_prefix == str(home / "pycache")


def test_h1_the_repo_goes_after_the_standard_library_on_sys_path(notes, home, notes_repo):
    registry.clear_cache()
    registry.load("notes")
    std = registry._stdlib_entries()
    idx = {p: i for i, p in enumerate(sys.path)}
    last_std = max(i for p, i in idx.items() if os.path.realpath(p) in std)
    assert idx[str(notes_repo / "src")] > last_std and idx[str(notes_repo)] == idx[str(notes_repo / "src")] + 1


@pytest.mark.parametrize("where", ["root", "src", "both"])
def test_h1_a_stdlib_named_file_in_the_repo_cannot_replace_the_stdlib(notes, home, notes_repo, tmp_path, where):
    """The review's probe: json.py / decimal.py at the repo root or in src/ shadowed the standard library for the host.
    Checked in a fresh isolated interpreter the way Togi starts: after the bundle loads, every stdlib name still
    resolves to the standard library. (zipapp is one nothing has imported yet, so it also shows the import itself.)"""
    names = ("json", "decimal", "hmac", "zipapp")
    mark = marker_in(tmp_path)
    for d in {"root": [notes_repo], "src": [notes_repo / "src"], "both": [notes_repo, notes_repo / "src"]}[where]:
        for n in names:
            (d / f"{n}.py").write_text(f"open({str(mark)!r}, 'a').write({n!r})\n")
    code = (f"import sys, importlib.machinery as M\nsys.path.insert(0, {str(SRC)!r})\n"
            f"from togi import registry\nregistry.load_repo(__import__('pathlib').Path({str(notes_repo)!r}))\n"
            "import zipapp\n"
            f"print([(n, M.PathFinder.find_spec(n, sys.path).origin) for n in {names!r}])\n")
    env = {"PATH": os.environ["PATH"], "TOGI_HOME": str(home), "TOGI_APPROVER_HOME": str(conftest.throwaway_approver_home(home)),
           "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert not mark.exists(), mark.read_text() if mark.exists() else ""
    origins = eval(r.stdout.strip().splitlines()[-1])
    assert all(str(notes_repo) not in o for _, o in origins), origins


def test_h1_the_repo_still_wins_over_an_installed_copy_of_the_same_bundle(notes, home, notes_repo, tmp_path):
    """After the standard library but BEFORE site-packages: a registered repo is what runs, not an old installed copy."""
    registry.clear_cache()
    registry.load("notes")
    sp = [i for i, p in enumerate(sys.path) if p.endswith("site-packages")]
    if sp:
        assert sys.path.index(str(notes_repo / "src")) < min(sp)


def test_h1_spec_states_the_rule_for_a_money_commands_code():
    spec = (ROOT / "SPEC.md").read_text()
    assert "gated_code_unlocked" in spec and "r5" in spec.lower()
    assert re.search(r"code behind a (money|gated) command must be locked or reshape", spec, re.I)


# ============================================================================= H2: approve does not run the bundle's code

PROBE_CHILD = '''import json, os, socket, subprocess, sys
req = json.loads(sys.stdin.read())
rep = {}


def attempt(name, fn):
    try:
        fn()
        rep[name] = "ok"
    except BaseException as e:
        rep[name] = type(e).__name__


attempt("write_outside", lambda: open(req["outside"], "w").write("x"))
attempt("read_secret", lambda: open(req["secret"]).read())
attempt("write_own_tmp", lambda: open(os.path.join(os.environ["TMPDIR"], "ok.txt"), "w").write("x"))
attempt("open_tty", lambda: os.open("/dev/tty", os.O_RDWR))
attempt("network", lambda: socket.create_connection(("1.1.1.1", 53), timeout=2))
rep["isatty"] = [os.isatty(i) for i in (0, 1, 2)]
rep["session_leader"] = os.getsid(0) == os.getpid()
rep["env"] = sorted(os.environ)
rep["cwd"] = os.getcwd()
rep["tmpdir"] = os.environ.get("TMPDIR")
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
rep["grandchild"] = grandchild.pid
print("TOGI-DESCRIBE " + json.dumps({"report": rep}))
'''

SLEEPER_CHILD = "import sys, time\nsys.stdin.read()\ntime.sleep(120)\n"


def lock_without_snapshot(name: str = "notes") -> dict:
    """A lock the way r4 made it: hashes and an audit entry, no describe snapshot."""
    with mock.patch.dict(os.environ):
        os.environ.pop("TOGI_APPROVER_HOME", None)
        return audit.lock_bundle(registry.manifest_for(name), "human:test", snapshot=False)


def snapshot_of(notes_repo: Path):
    man = load_manifest(notes_repo)
    return describe.read_snapshot(man, audit.sha256_text((notes_repo / ".togi/locked.sha256").read_text()))


@needs_sandbox
def test_h2_lock_records_a_signed_snapshot_of_the_commands(notes, home, notes_repo):
    out = lock_bundle("notes")
    assert out["describe_snapshot"] is True and out["commands"] == 9
    snap = snapshot_of(notes_repo)
    assert snap.ok, snap.why
    cmds = describe.views(snap.doc)
    tip = cmds["tip send"]
    assert tip.effect == "money" and tip.confirm == "id" and [p.name for p in tip.params] == ["id", "amount"]
    assert tip.summary.startswith("Send a tip") and tip.schema["required"] == ["id", "amount"]
    assert snap.doc["gated_roots"] == ["src/notes"]
    assert "src/notes/togi_bundle.py" in snap.doc["loaded_files"] and "src/notes/format.py" in snap.doc["loaded_files"]
    assert snap.doc["lock_sha256"] == audit.sha256_text((notes_repo / ".togi/locked.sha256").read_text())
    assert len(snap.doc["mac"]) == 64 and snap.doc["sandboxed"] is True


def test_h2_a_snapshot_edited_by_hand_is_not_trusted(pending, home, notes_repo):
    p = notes_repo / ".togi/describe.json"
    doc = json.loads(p.read_text())
    for c in doc["commands"]:
        if c["name"] == "tip send":
            c["summary"] = "Say hello (harmless)"
    p.write_text(json.dumps(doc))
    assert snapshot_of(notes_repo).state == "invalid"
    code, shown = approve_in_pty(home, pending)
    assert code == 2 and "describe_snapshot_invalid" in shown and "harmless" not in shown, shown
    assert approvals.load(pending)["status"] == "pending"


def test_h2_a_snapshot_signed_by_another_machine_or_repo_is_not_trusted(pending, home, notes_repo, tmp_path):
    p = notes_repo / ".togi/describe.json"
    doc = json.loads(p.read_text())
    doc["repo"] = str(tmp_path / "somewhere_else")                      # a copy of someone else's snapshot
    p.write_text(json.dumps(doc))
    assert snapshot_of(notes_repo).state == "invalid"
    (home / "audit.key").write_text("00" * 32 + "\n")                    # (and any other key fails the signature)
    assert snapshot_of(notes_repo).state == "invalid"


def test_h2_approve_does_not_import_the_bundle(pending, home, notes_repo):
    """r4 loaded the bundle in a child to read the summary, so anything the bundle did at import ran while a person was
    about to decide. Now the summary comes from the signed snapshot: even a bundle whose reshape code raises at import
    (it is not in the lock digest) can be approved, because nothing imports it."""
    fmt = notes_repo / "src/notes/format.py"
    fmt.write_text(fmt.read_text() + "\nraise RuntimeError('import boom')\n")
    code, shown = approve_in_pty(home, pending)
    assert code == 0, shown
    assert "the signed snapshot" in shown and "Send a tip to a note's author" in shown
    assert approvals.load(pending)["status"] == "approved"


@needs_sandbox
def test_h2_import_time_code_never_writes_outside_the_sandbox_at_lock_or_approve(notes, home, notes_repo, tmp_path):
    mark = tmp_path / "IMPORT_MARKER"
    fmt = notes_repo / "src/notes/format.py"
    fmt.write_text(fmt.read_text() + f"\ntry:\n    open({str(mark)!r}, 'w').write('import-time code ran')\nexcept OSError:\n    pass\n")
    lock_bundle("notes")                                                   # the snapshot child imports it, sandboxed
    assert not mark.exists(), "the describe child wrote outside its sandbox"
    togi("notes", "add", "--text", "tip me")
    code, out = tip_request()                                              # (the host imports the bundle: that is its job)
    assert code == 3
    mark.unlink(missing_ok=True)
    code, shown = approve_in_pty(home, out["approval"]["id"])
    assert code == 0, shown
    assert not mark.exists(), "import-time code ran in the approval session"


@needs_sandbox
def test_h2_without_a_snapshot_approve_reads_the_bundle_in_the_sandbox(notes, home, notes_repo, tmp_path):
    mark = tmp_path / "IMPORT_MARKER"
    fmt = notes_repo / "src/notes/format.py"
    fmt.write_text(fmt.read_text() + f"\ntry:\n    open({str(mark)!r}, 'w').write('import-time code ran')\nexcept OSError:\n    pass\n")
    lock_without_snapshot("notes")                                         # a lock like r4's: no .togi/describe.json
    assert not (notes_repo / ".togi/describe.json").exists()
    togi("notes", "add", "--text", "tip me")
    code, out = tip_request()
    assert code == 3
    mark.unlink(missing_ok=True)
    code, shown = approve_in_pty(home, out["approval"]["id"])
    assert code == 0, shown
    assert "sandboxed read of the bundle" in shown and "togi lock" in shown
    assert not mark.exists(), "the fallback child wrote outside its sandbox"


@needs_sandbox
def test_h2_a_snapshot_for_an_earlier_lock_is_stale_and_falls_back_to_the_sandbox(pending, home, notes_repo):
    saved = (notes_repo / ".togi/describe.json").read_text()
    toml = notes_repo / "togi.toml"
    toml.write_text(toml.read_text().replace('version = "0.1.0"', 'version = "0.1.1"'))
    lock_without_snapshot("notes")
    (notes_repo / ".togi/describe.json").write_text(saved)                 # a genuine snapshot, but for the old lock
    assert snapshot_of(notes_repo).state == "stale"
    code, out = tip_request("7.00")
    assert code == 3
    code, shown = approve_in_pty(home, out["approval"]["id"])
    assert code == 0, shown
    assert "sandboxed read of the bundle" in shown


def test_h2_the_pending_file_must_agree_with_the_snapshot(pending, home):
    edit_approval(home, pending, lambda r: r.update(summary="notes tip send: a harmless test tip (amount=0.01)"))
    code, shown = approve_in_pty(home, pending)
    assert code == 2 and "approval_tampered" in shown and "harmless" not in shown
    edit_approval(home, pending, lambda r: r.update(summary=approvals.load(pending)["summary"], confirm_word="yes"))
    code, shown = approve_in_pty(home, pending, word="yes")
    assert code == 2 and "approval_tampered" in shown
    assert approvals.load(pending)["status"] == "pending"


@pytest.mark.parametrize("edit", ["unknown-command", "read-command", "extra-argument"])
def test_h2_a_request_for_something_the_snapshot_does_not_describe_is_refused(pending, home, edit):
    def fn(r):
        if edit == "unknown-command":
            r["command"] = "tip steal"
        elif edit == "read-command":                                      # a command that exists but is not gated
            r["command"] = "list"
        else:
            r["args"]["extra"] = "x"
            r["args_hash"] = approvals.args_hash(r["bundle"], r["command"], r["args"])
    edit_approval(home, pending, fn)
    code, shown = approve_in_pty(home, pending)
    assert code == 2 and ("approval_tampered" in shown), shown
    assert approvals.load(pending)["status"] == "pending"


def test_h2_the_pure_data_summary_matches_the_hosts_for_every_command_of_the_example(notes, home, notes_repo):
    lock_bundle("notes")
    snap = snapshot_of(notes_repo)
    loaded = registry.load("notes")
    for name, view in describe.views(snap.doc).items():
        live = loaded.bundle.get(name)
        args = {p.name: ("n_1" if p.kind == "str" else 1) for p in live.params}
        assert approvals.summarize("notes", view, args) == approvals.summarize("notes", live, args)
        assert approvals.confirm_word(args, view, "ap_0123456789") == approvals.confirm_word(args, live, "ap_0123456789")


@needs_sandbox
def test_h2_the_describe_child_is_sandboxed_in_its_own_session_with_a_scrubbed_environment(
        notes, home, notes_repo, tmp_path, monkeypatch):
    (notes_repo / "_probe_child.py").write_text(PROBE_CHILD)
    outside, secret = tmp_path / "outside.txt", home / "secret.txt"
    secret.write_text("the person's secret")
    monkeypatch.setenv("LEAK_ME", "the person's environment")
    monkeypatch.setenv("NOTES_TEST_MODE", "1")                              # a [policy] run_env variable of notes
    man = load_manifest(notes_repo)
    assert man.run_env == ["NOTES_TEST_MODE"]
    res, info = describe.run_child(man, {"outside": str(outside), "secret": str(secret)},
                                   script=notes_repo / "_probe_child.py")
    r = res["report"]
    assert info["sandboxed"] is True and info["level"] in ("strict", "standard")
    assert r["write_outside"] == "PermissionError" and not outside.exists()
    assert r["read_secret"] == "PermissionError"                            # $TOGI_HOME is hidden
    assert r["write_own_tmp"] == "ok" and r["network"] != "ok" and r["open_tty"] != "ok"
    assert r["isatty"] == [False, False, False] and r["session_leader"] is True
    assert set(r["env"]) <= {"PATH", "HOME", "TMPDIR", "LANG", "__CF_USER_TEXT_ENCODING"}, r["env"]
    assert r["cwd"] == r["tmpdir"]                                          # its working directory is its temp directory
    pid = r["grandchild"]
    for _ in range(40):                                                     # the whole process group is killed afterwards
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail("a process the describe child started outlived it")
    assert not os.path.exists(r["tmpdir"])                                  # and its temp directory is removed


@needs_sandbox
def test_h2_a_describe_child_that_never_answers_is_stopped(notes, home, notes_repo):
    (notes_repo / "_sleeper.py").write_text(SLEEPER_CHILD)
    man = load_manifest(notes_repo)
    t0 = time.monotonic()
    with pytest.raises(TogiError) as e:
        describe.run_child(man, {}, script=notes_repo / "_sleeper.py", timeout=1.5)
    assert e.value.code == "describe_failed" and "took longer" in e.value.message
    assert time.monotonic() - t0 < 15


def test_h2_no_sandbox_means_the_bundle_is_not_run_outside_one(pending, home, notes_repo):
    man = load_manifest(notes_repo)
    with mock.patch.object(sandbox, "available", return_value=(False, "no sandbox-exec on this test machine")):
        with pytest.raises(TogiError) as e:
            describe.run_child(man, {"op": "snapshot", "repo": str(man.repo), "bundle": "notes"})
        assert e.value.code == "describe_unsandboxed" and e.value.status == "refused"
        # a library lock records no snapshot rather than running the bundle outside a sandbox ...
        with mock.patch.dict(os.environ):
            os.environ.pop("TOGI_APPROVER_HOME", None)
            out = audit.lock_bundle(registry.manifest_for("notes"), "human:test")
        assert out["describe_snapshot"] is False and not (notes_repo / ".togi/describe.json").exists()
        # ... and approve, which then has no snapshot, refuses instead of importing the bundle in this process
        code, o = tip_request("8.00")
        rec = approvals.load(o["approval"]["id"])
        with pytest.raises(TogiError) as e2:
            approvals.verify_pending(rec["id"], rec)
        assert e2.value.code == "describe_unsandboxed"


def test_h2_the_old_unsandboxed_child_is_gone():
    assert not hasattr(approvals, "describe_in_child")
    src = (ROOT / "src/togi/describe.py").read_text()
    assert "start_new_session=True" in src and "killpg" in src and "prepare_describe" in src


# ============================================================================= M1: the approver's own directory

R4_FILES = ("approver", "approver.key", "approver.state", "spent.jsonl", "spent.head")
FIXED = dict(drop_env=("TOGI_APPROVER_HOME",))          # the person's shell: no override variable (plus fake_pw=...)


def make_r4_layout(fake_pw_dir: Path, tmp_path: Path) -> Path:
    """The approver as r4 left it: its files loose in `<pw_dir>/.togi`. Built with a throwaway approver, then copied."""
    flat = fake_pw_dir / ".togi"
    flat.mkdir(parents=True, exist_ok=True)
    build = tmp_path / "r4_build"
    with mock.patch.dict(os.environ, {"TOGI_APPROVER_HOME": str(build)}):
        approver.create(PASSPHRASE, n=2 ** 10)
        spent.claim("ap_0123456789")
    for f in build.iterdir():
        shutil.copy2(f, flat / f.name)
    return flat


def test_m1_the_approver_has_a_directory_of_its_own_in_the_default_layout(fake_pw_dir, monkeypatch):
    monkeypatch.delenv("TOGI_APPROVER_HOME", raising=False)
    monkeypatch.delenv("TOGI_HOME", raising=False)
    where = fake_pw_dir / ".togi" / "approver.d"
    assert paths.fixed_approver_home() == paths.approver_home() == where
    assert paths.approver_path().parent == paths.approver_key_path().parent == paths.approver_state_path().parent == where
    assert spent.log_path().parent == spent.head_path().parent == where
    # the reviewer's check: the spent log no longer shares the directory that $TOGI_HOME defaults to
    monkeypatch.setenv("HOME", str(fake_pw_dir))
    assert paths.togi_home() == fake_pw_dir / ".togi" and spent.log_path().parent != paths.togi_home()
    approver.create(PASSPHRASE, n=2 ** 10)
    assert where.is_dir() and where.stat().st_mode & 0o777 == 0o700
    assert (where / "approver").is_file() and (where / "approver.key").is_file() and not (fake_pw_dir / ".togi/approver").exists()


def test_m1_an_existing_loose_approver_dir_is_brought_back_to_0700(fake_pw_dir, monkeypatch):
    monkeypatch.delenv("TOGI_APPROVER_HOME", raising=False)
    where = fake_pw_dir / ".togi" / "approver.d"
    where.mkdir(parents=True, mode=0o755)
    os.chmod(where, 0o755)
    approver.create(PASSPHRASE, n=2 ** 10)
    assert where.stat().st_mode & 0o777 == 0o700


def test_m1_an_override_directory_is_used_as_given(tmp_path, monkeypatch):
    d = tmp_path / "somebodys_dir"
    d.mkdir()
    os.chmod(d, 0o755)
    monkeypatch.setenv("TOGI_APPROVER_HOME", str(d))
    approver.create(PASSPHRASE, n=2 ** 10)
    assert d.stat().st_mode & 0o777 == 0o755                                 # never chmod a directory we did not make


@pytest.mark.parametrize("home_is", ["the-approver-dir", "inside-it", "above-it"])
def test_m1_a_togi_home_that_equals_contains_or_sits_in_the_approver_dir_is_refused(
        fake_pw_dir, tmp_path, monkeypatch, home_is):
    monkeypatch.delenv("TOGI_APPROVER_HOME", raising=False)
    ad = fake_pw_dir / ".togi" / "approver.d"
    ad.mkdir(parents=True)
    chosen = {"the-approver-dir": ad, "inside-it": ad / "mine", "above-it": fake_pw_dir}[home_is]
    monkeypatch.setenv("TOGI_HOME", str(chosen))
    assert paths.layout_problem()
    with pytest.raises(TogiError) as e:
        paths.require_safe_layout()
    assert e.value.code == "togi_home_unsafe" and e.value.status == "refused"
    code, out = togi_code("bundles")
    assert code == 2 and out["error"]["code"] == "togi_home_unsafe"
    assert not (ad / "journal.jsonl").exists() and not (ad / "bundles.toml").exists()


def test_m1_the_default_home_which_holds_approver_d_and_a_home_beside_it_are_fine(fake_pw_dir, tmp_path, monkeypatch):
    monkeypatch.delenv("TOGI_APPROVER_HOME", raising=False)
    monkeypatch.setenv("TOGI_HOME", str(fake_pw_dir / ".togi"))             # the default layout: .togi/approver.d inside it
    assert paths.layout_problem() is None
    monkeypatch.setenv("TOGI_HOME", str(tmp_path / "beside"))
    assert paths.layout_problem() is None
    monkeypatch.setenv("TOGI_APPROVER_HOME", str(tmp_path / "beside"))      # a test approver home equal to $TOGI_HOME
    assert paths.layout_problem()
    monkeypatch.setenv("TOGI_APPROVER_HOME", str(tmp_path / "beside" / "approver"))
    assert paths.layout_problem()                                           # ... or inside it
    monkeypatch.setenv("TOGI_APPROVER_HOME", str(tmp_path / "sibling"))
    assert paths.layout_problem() is None


def test_m1_r4_files_are_moved_in_place_once_at_the_first_human_command(pending, home, fake_pw_dir, tmp_path):
    flat = make_r4_layout(fake_pw_dir, tmp_path)
    before = {n: (flat / n).read_bytes() for n in R4_FILES}
    fixed = dict(FIXED, fake_pw=fake_pw_dir)
    # a non-human command moves nothing and says what is pending
    r = run_togi(home, "approver", "status", **fixed)
    st = json.loads(r.stdout)["data"]
    assert st["configured"] is False and st["layout"] == "legacy" and "migrate" in st["next"]
    assert all((flat / n).exists() for n in R4_FILES) and not (flat / "approver.d").exists()
    r = run_togi(home, "approver", "migrate", **fixed)                       # piped stdio is not a person
    assert json.loads(r.stdout)["error"]["code"] == "needs_tty" and not (flat / "approver.d").exists()
    # any human command does it: here, the person approves a request (their r4 passphrase still works)
    code, shown = human(home, ["approve", pending], "n_1", **fixed)
    assert code == 0, shown
    ad = flat / "approver.d"
    assert ad.stat().st_mode & 0o777 == 0o700
    assert {n: (ad / n).read_bytes() for n in R4_FILES if n != "approver.state"} == \
        {n: v for n, v in before.items() if n != "approver.state"}
    assert not any((flat / n).exists() for n in R4_FILES), "the r4 copies must be gone"
    assert approvals.load(pending)["scope"] == "default"
    # the spent record moved with the key that signs it: the old entry is still spent, and the log verifies
    with mock.patch.dict(os.environ):
        os.environ.pop("TOGI_APPROVER_HOME")
        assert spent.is_spent("ap_0123456789") is True
    events = [json.loads(l) for l in (home / "state/_host/journal.jsonl").read_text().splitlines()]
    assert any(e.get("event") == "approver_migrated" for e in events)
    # and it is once: the second look finds nothing to do
    code, shown = run_in_pty(home, ["approver", "migrate", "--json"], **fixed)
    assert code == 0 and '"moved":[]' in shown.replace(" ", ""), shown


def test_m1_a_grant_signed_before_the_migration_still_works_after_it(pending, home, fake_pw_dir, tmp_path):
    make_r4_layout(fake_pw_dir, tmp_path)
    fixed = dict(FIXED, fake_pw=fake_pw_dir)
    code, shown = human(home, ["approve", pending], "n_1", **fixed)         # migrates, then signs with the same key
    assert code == 0, shown
    r = run_togi(home, "notes", "tip", "send", "--id", "n_1", "--amount", "5.00", "--approval", pending, **fixed)
    assert r.returncode == 0, r.stdout
    assert json.loads(r.stdout)["data"]["amount"] == "5.00"


def test_m1_a_pending_migration_is_named_not_mistaken_for_no_approver(pending, home, fake_pw_dir, tmp_path):
    make_r4_layout(fake_pw_dir, tmp_path)
    fixed = dict(FIXED, fake_pw=fake_pw_dir)
    code, shown = human(home, ["approve", pending], "n_1", **dict(fixed, scope="test"))
    assert code == 0, shown
    # (a request made before, a grant made now) -> now simulate: r4 files back in the flat place, no approver.d yet
    shutil.rmtree(fake_pw_dir / ".togi" / "approver.d")
    make_r4_layout(fake_pw_dir, tmp_path / "again")
    r = run_togi(home, "notes", "tip", "send", "--id", "n_1", "--amount", "5.00", "--approval", pending, **fixed)
    out = json.loads(r.stdout)
    assert out["error"]["code"] == "approver_migration_pending" and "migrate" in out["next"], out


def test_m1_an_approver_in_both_places_is_a_conflict_nothing_is_moved(pending, home, fake_pw_dir, tmp_path):
    flat = make_r4_layout(fake_pw_dir, tmp_path)
    ad = flat / "approver.d"
    ad.mkdir(mode=0o700)
    (ad / "approver").write_text("planted before the migration by something that is not the person")
    fixed = dict(FIXED, fake_pw=fake_pw_dir)
    before = {n: (flat / n).read_bytes() for n in R4_FILES}
    code, shown = human(home, ["approve", pending], "n_1", **fixed)
    assert code == 2 and "approver_conflict" in shown and 'Type "' not in shown, shown
    assert {n: (flat / n).read_bytes() for n in R4_FILES} == before and (ad / "approver").read_text().startswith("planted")
    assert approvals.load(pending)["status"] == "pending"


def test_m1_doctor_reports_the_layout(home, fake_pw_dir, tmp_path):
    make_r4_layout(fake_pw_dir, tmp_path)
    r = run_togi(home, "doctor", fake_pw=fake_pw_dir, **FIXED)
    rows = {c["check"]: c for c in json.loads(r.stdout)["data"]["checks"]}
    assert rows["approver files layout"]["ok"] is False and "approver.d" in rows["approver files layout"]["detail"]
    assert "legacy approver" not in rows                                     # (r1's check must not mistake r4's files for it)
    assert rows["TOGI_HOME apart from the approver's directory"]["ok"] is True


def test_m1_readme_and_spec_say_where_the_approver_lives():
    readme, spec = (ROOT / "README.md").read_text(), (ROOT / "SPEC.md").read_text()
    assert "approver.d" in readme and "approver.d" in spec
    assert "~/.togi (fixed" not in readme            # r4's wording: the approver "lives in ~/.togi"
    assert re.search(r"approver\.d.{0,200}0700|0700.{0,200}approver\.d", spec, re.S)


# ============================================================================= M2: what the lock prompt shows

def lock_typed(home, typed, **kw):
    return run_in_pty(home, ["lock", "notes"], typed=typed, **kw)            # (lock picks the default-scope approver)


def audit_len(repo: Path) -> int:
    return len(audit.read(repo))


def test_m2_a_first_lock_needs_no_extra_word_and_records_the_snapshot(notes, home, notes_repo):
    code, shown = lock_typed(home, answers("notes"))
    assert code == 0, shown
    assert "First lock" in shown and "relock" not in shown.lower().replace("relock after", "") or "Type \"relock\"" not in shown
    assert 'Type "relock"' not in shown
    assert "commands read in a" in shown and "gated: tip send" in shown
    assert (notes_repo / ".togi/describe.json").is_file() and (notes_repo / ".togi/locked.sha256").is_file()


def changed_state(notes_repo: Path):
    """Edit one locked file, remove one, add one: the three kinds of change the prompt has to show."""
    store = notes_repo / "src/notes/store.py"
    store.write_text(store.read_text() + "\n# changed after the last lock\n")
    (notes_repo / "stone/tasks/missing-note.toml").unlink()
    (notes_repo / "tests/test_extra.py").write_text("def test_x():\n    pass\n" * 3)


def test_m2_the_prompt_shows_changed_missing_and_new_files_with_sizes(notes, home, notes_repo):
    lock_bundle("notes")
    changed_state(notes_repo)
    seq0 = audit_len(notes_repo)
    code, shown = lock_typed(home, [('Type "', "notes"), ('Type "', "not relock")])
    assert code == 2 and "confirmation_mismatch" in shown, shown
    assert "changed: 1" in shown and "src/notes/store.py" in shown and re.search(r"store\.py\s+\(\d+ bytes\)", shown)
    assert "missing: 1" in shown and "stone/tasks/missing-note.toml" in shown
    assert "new: 1" in shown and re.search(r"tests/test_extra\.py\s+\(\d+ bytes\)", shown)
    assert 'Type "relock"' in shown
    assert audit_len(notes_repo) == seq0, "refusing the extra word must change nothing"


def test_m2_the_head_pointer_and_the_audit_entries_since_the_last_lock_are_shown(notes, home, notes_repo):
    lock_bundle("notes")
    for i in range(3):                                                       # entries written after the lock
        audit.append(notes_repo, "notes", action="reject", proposal=f"p_00{i}", tier="locked", files=[], diff_sha256=None,
                     stone_before=None, stone_after=None, decided_by="policy", reason="test entry")
    code, shown = lock_typed(home, [('Type "', "notes")], timeout=20)
    assert "audit head pointer: ok" in shown
    assert "audit entries since the last lock: 3" in shown and "p_002" in shown and "reject" in shown


def test_m2_a_missing_head_pointer_needs_the_extra_word(notes, home, notes_repo):
    lock_bundle("notes")
    (home / "state/notes/audit.head").unlink()
    code, shown = lock_typed(home, [('Type "', "notes"), ('Type "', "no")])
    assert "audit head pointer: MISSING" in shown and 'Type "relock"' in shown and code == 2
    code, shown = lock_typed(home, [('Type "', "notes"), ('Type "', "relock"), ("passphrase", PASSPHRASE)])
    assert code == 0, shown
    assert audit.verify(notes_repo, "notes")["head"] == "ok"                 # re-anchored by the person


def test_m2_with_the_extra_word_the_changes_are_locked(notes, home, notes_repo):
    lock_bundle("notes")
    changed_state(notes_repo)
    man = load_manifest(notes_repo)
    assert audit.verify_locked(notes_repo, man)["ok"] is False
    code, shown = lock_typed(home, [('Type "', "notes"), ('Type "', "relock"), ("passphrase", PASSPHRASE)])
    assert code == 0, shown
    assert audit.verify_locked(notes_repo, man)["ok"] is True


def test_m2_nothing_changed_means_no_extra_word(notes, home, notes_repo):
    lock_bundle("notes")
    code, shown = lock_typed(home, answers("notes"))
    assert code == 0, shown
    assert "no locked file changed, went missing or is new" in shown and 'Type "relock"' not in shown


def test_m2_a_deleted_lock_file_needs_the_extra_word(notes, home, notes_repo):
    lock_bundle("notes")
    (notes_repo / ".togi/locked.sha256").unlink()
    code, shown = lock_typed(home, [('Type "', "notes"), ('Type "', "no")])
    assert "MISSING" in shown and 'Type "relock"' in shown and code == 2


def test_m2_the_locked_files_changing_while_the_person_decides_are_not_locked(notes, home, notes_repo):
    lock_bundle("notes")
    man = load_manifest(notes_repo)
    review = audit.lock_review(man)
    (notes_repo / "src/notes/store.py").write_text("# changed after the prompt was built\n")
    seq0 = audit_len(notes_repo)
    with mock.patch.dict(os.environ):
        os.environ.pop("TOGI_APPROVER_HOME", None)
        with pytest.raises(TogiError) as e:
            audit.lock_bundle(man, "human:test", expect_digest=review["digest"])
    assert e.value.code == "repo_changed" and audit_len(notes_repo) == seq0


def test_m2_an_unreadable_bundle_leaves_the_old_lock_alone(notes, home, notes_repo):
    lock_bundle("notes")
    text0 = (notes_repo / ".togi/locked.sha256").read_text()
    seq0 = audit_len(notes_repo)
    tb = notes_repo / "src/notes/togi_bundle.py"
    tb.write_text(tb.read_text() + "\nraise RuntimeError('cannot import')\n")
    with mock.patch.dict(os.environ):
        os.environ.pop("TOGI_APPROVER_HOME", None)
        with pytest.raises(TogiError) as e:
            audit.lock_bundle(load_manifest(notes_repo), "human:test")
    assert e.value.code == "describe_failed" and "cannot import" in e.value.message
    assert (notes_repo / ".togi/locked.sha256").read_text() == text0 and audit_len(notes_repo) == seq0


# ============================================================================= M3: a bundle's stderr is bounded too

LOUD_PY = '''import sys
from togi import Bundle

bundle = Bundle("loud")


@bundle.command("shout", effect="read", examples=["togi loud shout"])
def shout() -> dict:
    """Write a lot to stderr and a lot to stdout."""
    sys.stderr.write("E" * 300000)
    print("O" * 300000)
    return {"ok": 1}
'''


@pytest.fixture
def loud(home, tmp_path):
    repo = tmp_path / "loud"
    (repo / "src" / "loud").mkdir(parents=True)
    (repo / "togi.toml").write_text('[bundle]\nname = "loud"\nmodule = "loud.b:bundle"\nsummary = "Loud."\n')
    (repo / "src" / "loud" / "__init__.py").write_text("")
    (repo / "src" / "loud" / "b.py").write_text(LOUD_PY)
    togi("add", str(repo))
    return repo


def test_m3_stderr_written_by_a_bundle_is_capped_like_stdout_on_the_cli(loud, home):
    r = run_togi(home, "loud", "shout", "--json")
    assert r.returncode == 0 and len(r.stdout) < 8192
    assert len(r.stderr) < 9000, f"{len(r.stderr)} bytes of stderr reached the caller"
    assert "printed 300000 characters to stderr (first 4096 shown)" in r.stderr
    assert "printed 300001 characters to stdout (first 4096 shown)" in r.stderr
    entry = [json.loads(l) for l in (home / "state/loud/journal.jsonl").read_text().splitlines() if '"call"' in l][-1]
    assert entry["printed_err_bytes"] == 300000 and entry["printed_bytes"] == 300001


def test_m3_and_on_the_mcp_path(loud, home, capsys):
    from togi import mcp_bridge

    registry.clear_cache()
    loaded = registry.load("loud")
    env, is_error = mcp_bridge.call_tool(loaded, "loud_shout", {})
    err = capsys.readouterr().err
    assert is_error is False and env["data"] == {"ok": 1}
    assert len(err) < 9000 and "printed 300000 characters to stderr" in err


def test_m3_two_calls_at_once_do_not_see_each_others_stderr(loud, home, capsys):
    import threading

    from togi import mcp_bridge

    registry.clear_cache()
    loaded = registry.load("loud")
    done = []
    ts = [threading.Thread(target=lambda: done.append(mcp_bridge.call_tool(loaded, "loud_shout", {}))) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    err = capsys.readouterr().err
    assert len(done) == 4 and all(e["data"] == {"ok": 1} for e, _ in done)
    assert len(err) < 4 * 9000 and err.count("printed 300000 characters to stderr") == 4


def test_m3_the_streams_are_restored_afterwards(loud, home, capsys):
    saved = (sys.stdout, sys.stderr)
    from togi import mcp_bridge

    registry.clear_cache()
    mcp_bridge.call_tool(registry.load("loud"), "loud_shout", {})
    capsys.readouterr()
    assert (sys.stdout, sys.stderr) == saved


# ============================================================================= L1 / L2 / L3


def test_l1_the_prompt_shows_the_expiry_that_will_be_signed_not_the_requesters(pending, home):
    edit_approval(home, pending, lambda r: r.update(expires="2099-01-01T00:00:00.000Z"))
    code, shown = approve_in_pty(home, pending)
    assert code == 0, shown
    first = [l for l in shown.splitlines() if l.startswith("Approval ")][0]
    assert "2099" not in shown and "if you approve" in first
    signed = approvals.load(pending)["expires"]
    assert signed in first                                                   # the line the person read is the one signed
    import datetime as dt

    delta = (dt.datetime.strptime(signed, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)
             - dt.datetime.now(dt.timezone.utc)).total_seconds()
    assert 0 < delta <= approvals.DEFAULT_TTL_S


def test_l1_the_reviewers_unit_check():
    rec = {"id": "ap_0123456789", "expires": "2099-01-01T00:00:00.000Z", "bundle": "notes",
           "args": {"id": "n_1", "amount": "5.00"}, "summary": "s"}
    verified = {"args": rec["args"], "binding": {"repo": "/r", "version": "0.1.0"}, "summary": "s", "lock_line": "lock: VERIFIED"}
    first = approvals.prompt_lines_for(rec, verified)[0]
    assert "2099" not in first and "if you approve" in first
    assert "2099" in approvals.prompt_lines_for(rec, None)[0] and "as the requester wrote it" in approvals.prompt_lines_for(rec, None)[0]


def test_l1_ttl_is_what_is_shown_and_signed(pending, home):
    code, shown = human(home, ["approve", pending, "--ttl", "2m"], "n_1")
    assert code == 0, shown
    import datetime as dt

    signed = approvals.load(pending)["expires"]
    delta = (dt.datetime.strptime(signed, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=dt.timezone.utc)
             - dt.datetime.now(dt.timezone.utc)).total_seconds()
    first = [l for l in shown.splitlines() if l.startswith("Approval ")][0]
    assert 0 < delta <= 120 and signed in first                              # (the result printed after it also has `signed`)


def test_l2_an_argument_too_long_to_show_cannot_be_approved(notes, home):
    lock_bundle("notes")
    long_id = "n_" + "7" * 400
    code, out = tip_request("5.00", note=long_id)
    assert code == 3
    aid = out["approval"]["id"]
    code, shown = approve_in_pty(home, aid, word="n_7")
    assert code == 2 and "prompt_too_long" in shown and 'Type "' not in shown, shown
    assert approvals.load(aid)["status"] == "pending" and "grant_mac" not in approvals.load(aid)


def test_l2_at_the_limit_it_still_works(notes, home):
    lock_bundle("notes")
    ok_id = "n_" + "7" * (approvals.ARG_VALUE_CAP - 2 - 2)                   # +2 for the JSON quotes
    code, out = tip_request("5.00", note=ok_id)
    code, shown = approve_in_pty(home, out["approval"]["id"], word=ok_id[:40])
    assert "prompt_too_long" not in shown


def test_l2_a_denial_is_not_blocked_by_a_long_argument(notes, home):
    lock_bundle("notes")
    code, out = tip_request("5.00", note="n_" + "7" * 400)
    aid = out["approval"]["id"]
    code, shown = human(home, ["deny", aid], approvals.load(aid)["confirm_word"])
    assert code == 0, shown
    assert approvals.load(aid)["status"] == "denied"


def test_l3_run_max_output_cannot_go_past_the_full_limit(notes, home):
    assert runmode_clamp(3_000_000, False) == 1_000_000
    assert runmode_clamp(None, True) == 1_000_000 and runmode_clamp(None, False) == 8192
    assert runmode_clamp(500, False) == 500 and runmode_clamp(-5, False) >= 1
    r = run_togi(home, "run", "--max-output", "3000000", "-", input="print('x' * 2500000)\n", timeout=90)
    assert len(r.stdout.encode()) <= 1_000_000, len(r.stdout)          # script bytes and the note together
    assert "output cut at 1000000 bytes" in r.stdout


def runmode_clamp(requested, full):
    from togi import runmode

    return runmode.clamp_output(requested, full)


def test_l3_the_clamp_is_documented():
    from togi import cli

    assert "1000000" in cli.USAGE["run"] and "--max-output" in cli.USAGE["run"]
    assert "--max-output" in (ROOT / "README.md").read_text() and "--max-output" in (ROOT / "SPEC.md").read_text()


def test_h1_a_reshape_proposal_that_adds_a_shadow_is_rolled_back(notes, home, notes_repo):
    """Apply re-verifies the lock set after the write: a diff the tier globs allow (reshape) that plants a package shadowing
    a locked module changes the lock set, so the write is undone."""
    from togi import apply as apply_mod

    set_tiers(notes_repo, reshape=["src/notes/**"],
              locked=["togi.toml", "stone/**", "tests/**", "src/notes/store.py", "src/notes/messages.py",
                      "src/notes/__init__.py", "src/notes/togi_bundle.py"])
    lock_bundle("notes")
    man = load_manifest(notes_repo)
    assert man.classify("src/notes/store/__init__.py") == "reshape"
    moved = apply_mod.write_verified(notes_repo, man, {"src/notes/store/__init__.py": "# shadow\n"})
    assert moved is not None and "src/notes/store/__init__.py" in moved["unlisted"]
    assert not (notes_repo / "src/notes/store").exists(), "the write must have been rolled back"
    assert audit.verify_locked(notes_repo, man)["ok"] is True


def test_h2_doctor_reports_the_snapshot(notes, home, notes_repo):
    lock_bundle("notes")
    rows = {r["check"]: r for r in togi("doctor", "notes")["data"]["checks"]}
    assert rows["notes: describe snapshot"]["ok"] is True
    p = notes_repo / ".togi/describe.json"
    doc = json.loads(p.read_text())
    doc["commands"][0]["summary"] = "tampered"
    p.write_text(json.dumps(doc))
    code, out = togi_code("doctor", "notes")
    rows = {r["check"]: r for r in out["data"]["checks"]}
    assert rows["notes: describe snapshot"]["ok"] is False and rows["notes: describe snapshot"]["level"] == "error"
    p.unlink()
    rows = {r["check"]: r for r in togi("doctor", "notes")["data"]["checks"]}
    assert rows["notes: describe snapshot"]["ok"] is False and rows["notes: describe snapshot"]["level"] == "warn"


def test_m2_without_an_os_sandbox_the_person_types_a_further_word_before_the_bundle_is_imported(
        notes, home, notes_repo, monkeypatch):
    """No sandbox-exec on the machine: `togi lock` still works, but only after `unsandboxed` is typed, and it says so."""
    typed, seen_lines = [], []
    monkeypatch.setattr(approvals, "human_repo_gate", lambda action: None)          # (the gates have their own tests)
    monkeypatch.setattr(approvals, "require_approver", lambda: None)
    monkeypatch.setattr(approver, "require", lambda action, reader=None, need_key=True: typed.append("<passphrase>"))

    def fake_type(lines, word, action):
        typed.append(word)
        seen_lines.extend(lines)
        return "tester"

    monkeypatch.setattr(approvals, "type_confirmation", fake_type)
    monkeypatch.setattr(sandbox, "available", lambda: (False, "no sandbox-exec on this test machine"))
    out = togi("lock", "notes")
    assert typed == ["notes", "unsandboxed", "<passphrase>"], typed
    assert any("NO OS SANDBOX" in l for l in seen_lines)
    assert out["data"]["describe_snapshot"] is True
    snap = snapshot_of(notes_repo)
    assert snap.ok and snap.doc["sandboxed"] is False


WRITER_PY = '''import os
from togi import Bundle

MARK = os.environ.get("WRITER_MARK") or {mark!r}
open(MARK, "w").write("import-time code ran with the person's authority")      # unconditional: no try/except
bundle = Bundle("writer")


@bundle.command("pay", effect="money", confirm="to", examples=["togi writer pay --to bob"])
def pay(to: str) -> dict:
    """Pay someone."""
    return {{"to": to}}
'''


@needs_sandbox
def test_h2_a_bundle_whose_import_writes_a_file_cannot_do_it_from_inside_an_approval(home, tmp_path):
    """The review's probe. A bundle that writes a marker file when it is imported (here with no bundle lock, so there is no
    snapshot and approve must read the bundle itself): the host writes it when it runs the command, which is its job; but
    approving it never does, because the only import happens in the sandboxed child, where the write is denied."""
    mark = tmp_path / "WRITER_MARK"
    repo = tmp_path / "writer_repo"
    (repo / "src" / "writer").mkdir(parents=True)
    (repo / "togi.toml").write_text('[bundle]\nname = "writer"\nmodule = "writer.b:bundle"\nsummary = "Writes at import."\n'
                                    '[policy]\nrequire_lock = false\ntest_approvals = true\n')
    (repo / "src" / "writer" / "__init__.py").write_text("")
    (repo / "src" / "writer" / "b.py").write_text(WRITER_PY.format(mark=str(mark)))
    togi("add", str(repo))
    code, out = togi_code("writer", "pay", "--to", "bob")
    assert code == 3 and mark.exists()                                     # (the host imported it to take the request)
    mark.unlink()
    aid = out["approval"]["id"]
    code, shown = human(home, ["approve", aid], "bob")
    assert not mark.exists(), "the bundle's import-time code ran in the approval"
    assert code == 2 and "describe_failed" in shown and "Operation not permitted" in shown, shown
    assert approvals.load(aid)["status"] == "pending"


def test_h1_approve_rechecks_the_gated_code_rule_from_the_snapshot_without_importing(pending, home, notes_repo):
    """The request was filed while the package was covered; code is added to it afterwards. The untiered file is not in the
    lock digest, so the binding is unchanged: only the coverage rule (recomputed from the snapshot's roots) stops it."""
    (notes_repo / "src/notes/late.py").write_text("# added after the request\n")
    code, shown = approve_in_pty(home, pending)
    assert code == 2 and "gated_code_unlocked" in shown and "late.py" in shown and 'Type "' not in shown, shown
    assert approvals.load(pending)["status"] == "pending"


def test_h1_the_rule_holds_on_the_mcp_and_code_mode_paths_too(notes, home, notes_repo):
    from togi import mcp_bridge

    lock_bundle("notes")
    (notes_repo / "src/notes/helper.py").write_text("x = 1\n")
    registry.clear_cache()
    env, is_error = mcp_bridge.call_tool(registry.load("notes"), "notes_tip_send", {"id": "n_1", "amount": "5.00"})
    assert is_error is True and env["error"]["code"] == "gated_code_unlocked"
    r = run_togi(home, "run", "-", input="import notes\ntry:\n    notes.tip_send(id='n_1', amount='5.00')\nexcept Exception as e:\n"
                                         "    print(type(e).__name__, getattr(e, 'code', ''))\n")
    assert "gated_code_unlocked" in r.stdout, r.stdout + r.stderr


def test_h2_what_the_sandboxed_child_says_about_a_repo_is_checked_before_it_is_used(notes_repo):
    assert codeset.clean_relpaths(["src/notes", "../outside", "/etc", "a/../../b", "ok\x00", 5, None]) == ["src/notes"]
    good = {"name": "pay", "effect": "money", "summary": "s", "params": [{"name": "to"}], "confirm": "to"}
    assert describe._good_command(good)
    for bad in ({**good, "params": [{"nope": 1}]}, {**good, "params": "to"}, {**good, "name": 3}, {**good, "confirm": 7}, [], None):
        assert not describe._good_command(bad)
    # a hostile root cannot make the parent walk somewhere else
    man = load_manifest(notes_repo)
    assert codeset.coverage_problems(notes_repo, man, ["../../../../etc"], ["../../x.py"]) == []


def test_m1_a_half_finished_migration_is_picked_up_again(home, fake_pw_dir, tmp_path, monkeypatch):
    flat = make_r4_layout(fake_pw_dir, tmp_path)
    ad = flat / "approver.d"
    ad.mkdir(mode=0o700)
    os.replace(flat / "approver.key", ad / "approver.key")           # what a crash after the first rename leaves behind
    monkeypatch.delenv("TOGI_APPROVER_HOME")
    st = approver.layout_state()
    assert st["state"] == "legacy" and "approver" in st["legacy"] and "approver.key" not in st["legacy"]
    moved = approver.migrate_legacy()
    assert moved[-1] == "approver" and not (flat / "approver").exists() and (ad / "approver").is_file()
    assert (ad / "approver.key").is_file() and approver.migrate_legacy() == []
    assert approver.layout_state()["state"] == "current"



# ---- bytecode caches: never read by Togi, so never part of the lock (post-r5 refinement)

@pytest.mark.parametrize("rel", ["src/notes/__pycache__/store.{tag}.pyc", "src/notes/__pycache__/__init__.{tag}.pyc",
                                 "tests/__pycache__/test_x.{tag}.pyc"])
def test_bytecode_in_pycache_does_not_flip_the_lock(notes, home, notes_repo, rel):
    """Every ordinary test run regenerates __pycache__; if that changed the lock, a person could never keep it."""
    lock_bundle("notes")
    man = load_manifest(notes_repo)
    f = notes_repo / rel.format(tag=PYC_TAG)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"inert")
    assert audit.verify_locked(notes_repo, man)["ok"] is True


def test_a_source_file_inside_a_pycache_dir_is_still_locked(notes, home, notes_repo):
    set_tiers(notes_repo, locked=["togi.toml", "stone/**", "tests/**", "src/notes/__init__.py", "src/notes/togi_bundle.py",
                                  "src/notes/store.py", "src/notes/messages.py", "src/vend/**"])
    (notes_repo / "src/vend/__pycache__").mkdir(parents=True)
    (notes_repo / "src/vend/engine.py").write_text("x = 1\n")
    lock_bundle("notes")
    (notes_repo / "src/vend/__pycache__/evil.py").write_text("# importable as vend.__pycache__.evil\n")
    assert "src/vend/__pycache__/evil.py" in audit.verify_locked(notes_repo, load_manifest(notes_repo))["unlisted"]


def test_every_python_child_togi_starts_uses_a_togi_owned_bytecode_cache(home):
    from togi import paths as P
    args = P.pycache_args()
    assert args[0] == "-X" and args[1] == f"pycache_prefix={home / 'pycache'}"
    assert P.isolated_argv(["x"])[1:4] == ["-I", "-X", f"pycache_prefix={home / 'pycache'}"]
    import inspect
    from togi import describe, runmode, stone
    for mod in (describe, runmode, stone):
        assert "pycache_args(" in inspect.getsource(mod), mod.__name__
