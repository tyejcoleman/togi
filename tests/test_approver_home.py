"""Fix round 2, part A: an agent cannot choose where approval state lives.

The hole: the approver, the approvals and the registry all followed $TOGI_HOME, an ordinary environment variable. An
agent could set TOGI_HOME=/tmp/mine, `togi add` a bundle there, create its own approver with a fake terminal, approve
its own money command and run it. Now the approver (passphrase record, attempt state, signing key) lives at a fixed
place -- `<pw_dir>/.togi` -- every grant is signed with the approver key, and a throwaway approver exists only through
TOGI_APPROVER_HOME and marks everything it signs `scope: "test"`, which the command sees as `ctx.approval_scope`.

Nothing here touches the real approver: the fixed location is faked (conftest: `fake_pw_dir`), and the autouse guard
fails any test that changes an approver file in the real home.
"""
from __future__ import annotations

import asyncio
import json
import sys
import textwrap

import pytest

from togi import approvals, approver, mcp_bridge, paths, registry

import conftest
from conftest import (PASSPHRASE, REAL_PW_DIR, answers, approver_files, clean_env, human, run_in_pty, run_togi,
                      throwaway_approver_home, togi, togi_code)

VAULT_TOML = '''[bundle]
name = "vault"
version = "0.1.0"
summary = "Scope demo: money commands that report the scope of the approval they ran under."
module = "vault.togi_bundle:bundle"

[tiers]
polish = ["skills/**"]
locked = ["togi.toml", "src/vault/engine.py"]

[policy]
approve_effects = ["money"]
'''

VAULT_PY = '''import os

from togi import Bundle, Context, TogiError

bundle = Bundle("vault")


@bundle.command("pay", effect="money", confirm="to", examples=["togi vault pay --to bob"])
def pay(to: str, ctx: Context = None) -> dict:
    """Pay someone. Reports the scope of the approval it ran under."""
    return {"to": to, "scope": ctx.approval_scope, "surface": ctx.surface}


@bundle.command("pay real", effect="money", confirm="to", examples=["togi vault pay real --to bob"])
def pay_real(to: str, ctx: Context = None) -> dict:
    """What a bundle that moves REAL value does: it refuses a test-scope approval outside its own test mode."""
    if ctx.approval_scope != "default" and not os.environ.get("VAULT_TEST_MODE"):
        raise TogiError("test_scope_refused", "this approval was signed by a throwaway (test-scope) approver; real "
                        "money moves only on a default-scope approval", next="ask the person to approve it with the "
                        "real approver", status="refused")
    return {"to": to, "paid": True, "scope": ctx.approval_scope}


@bundle.command("whoami", effect="read", examples=["togi vault whoami"])
def whoami(ctx: Context = None) -> dict:
    """Read-only: the approval scope a command sees when there was no approval."""
    return {"scope": ctx.approval_scope}
'''


@pytest.fixture
def vault_repo(tmp_path):
    repo = tmp_path / "vault_repo"
    (repo / "src" / "vault").mkdir(parents=True)
    (repo / "togi.toml").write_text(VAULT_TOML)
    (repo / "src" / "vault" / "__init__.py").write_text("")
    (repo / "src" / "vault" / "engine.py").write_text("# the engine\n")
    (repo / "src" / "vault" / "togi_bundle.py").write_text(VAULT_PY)
    return repo


@pytest.fixture
def vault(home, vault_repo):
    togi("add", str(vault_repo))
    return vault_repo


def request(*words):
    code, out = togi_code("vault", *words)
    assert code == 3, out
    return out["approval"]["id"]


def approve(home, aid, word, **kw):
    code, shown = human(home, ["approve", aid], word, **kw)
    assert code == 0, shown
    return shown


def contents(root):
    return {f.name: f.read_bytes() for f in root.iterdir() if "approver" in f.name} if root.is_dir() else {}


# ----------------------------------------------------------------------------- A.1: a fixed, env-independent location


def test_approver_home_is_the_password_database_home_not_home_and_not_togi_home(fake_pw_dir, monkeypatch, tmp_path):
    for k in ("TOGI_APPROVER_HOME",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "somewhere_else"))
    monkeypatch.setenv("TOGI_HOME", str(tmp_path / "agents_choice"))
    assert paths.approver_home() == fake_pw_dir / ".togi" == paths.fixed_approver_home()
    assert paths.approver_path() == fake_pw_dir / ".togi" / "approver"
    assert paths.approver_state_path() == fake_pw_dir / ".togi" / "approver.state"
    assert paths.approver_key_path() == fake_pw_dir / ".togi" / "approver.key"
    assert paths.approver_scope() == "default"
    assert paths.togi_home() == tmp_path / "agents_choice"             # TOGI_HOME still moves everything else
    assert paths.approver_home() != paths.togi_home()


def test_the_override_moves_the_approver_and_marks_it_test(fake_pw_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("TOGI_APPROVER_HOME", str(tmp_path / "throwaway"))
    assert paths.approver_home() == tmp_path / "throwaway" and paths.approver_scope() == "test"
    assert paths.approver_key_path() == tmp_path / "throwaway" / "approver.key"
    monkeypatch.setenv("TOGI_APPROVER_HOME", "")                         # empty is not an override
    assert paths.approver_home() == fake_pw_dir / ".togi" and paths.approver_scope() == "default"


def test_setup_in_the_fixed_home_makes_the_approver_and_a_0600_key_and_scope_is_default(home, fake_pw_dir):
    fixed = dict(drop_env=("TOGI_APPROVER_HOME",), fake_pw=fake_pw_dir)
    ask = [("New approver passphrase", "a passphrase for the fixed home"), ("again", "a passphrase for the fixed home")]
    code, shown = run_in_pty(home, ["approver", "setup"], typed=ask, **fixed)
    assert code == 0, shown
    fh = fake_pw_dir / ".togi"
    assert (fh / "approver").is_file() and (fh / "approver.key").is_file()
    assert (fh / "approver").stat().st_mode & 0o777 == 0o600 and (fh / "approver.key").stat().st_mode & 0o777 == 0o600
    assert len((fh / "approver.key").read_bytes()) == 32
    assert not (home / "approver").exists() and not (home / "approver.key").exists()      # nothing in $TOGI_HOME
    status = json.loads(run_togi(home, "approver", "status", **fixed).stdout)["data"]
    assert status["configured"] is True and status["key"] is True and status["scope"] == "default"
    assert status["path"] == str(fh / "approver")


def test_state_and_key_follow_the_approver_home_so_a_new_togi_home_does_not_reset_the_lockout(vault, home,
                                                                                               tmp_path, monkeypatch):
    for _ in range(5):
        with pytest.raises(Exception):
            approver.require("approve", reader=lambda _p: "not the passphrase at all")
    assert approver.lock_remaining() > 0
    monkeypatch.setenv("TOGI_HOME", str(tmp_path / "a_fresh_home"))     # an agent starts over in a new $TOGI_HOME
    assert approver.lock_remaining() > 0 and approver.status()["locked"] is True


# ----------------------------------------------------------------------------- A.2: setup never overwrites


def test_setup_refuses_to_overwrite_without_the_current_passphrase(home):
    ah = throwaway_approver_home(home)
    before = contents(ah)
    assert before.keys() >= {"approver", "approver.key"}
    code, shown = run_in_pty(home, ["approver", "setup"], typed=[("passphrase", "an attacker's own passphrase")])
    assert code == 2 and "wrong_passphrase" in shown
    assert "New approver passphrase" not in shown                        # it never got as far as asking for a new one
    assert {k: v for k, v in contents(ah).items() if k != "approver.state"} == \
        {k: v for k, v in before.items() if k != "approver.state"}


def test_the_library_will_not_replace_an_approver_either(home):
    ah = throwaway_approver_home(home)
    before = contents(ah)
    with pytest.raises(Exception) as e:
        approver.create("an attacker's own passphrase", n=2 ** 10)
    assert e.value.code == "approver_exists" and e.value.status == "refused"
    with pytest.raises(Exception) as e:
        approver.setup(reader=lambda _p: "an attacker's own passphrase", n=2 ** 10)
    assert e.value.code == "wrong_passphrase"
    assert {k: v for k, v in contents(ah).items() if k != "approver.state"} == \
        {k: v for k, v in before.items() if k != "approver.state"}


def test_rotating_the_passphrase_keeps_the_key_so_earlier_grants_stay_valid(vault, home):
    aid = request("pay", "--to", "bob")
    approve(home, aid, "bob")
    ah = throwaway_approver_home(home)
    key = (ah / "approver.key").read_bytes()
    answers_ = iter([PASSPHRASE, "a new passphrase 12345", "a new passphrase 12345"])
    approver.setup(reader=lambda _p: next(answers_), n=2 ** 10)
    assert (ah / "approver.key").read_bytes() == key
    assert togi("vault", "pay", "--to", "bob", "--approval", aid)["data"]["scope"] == "test"


def test_an_approver_without_its_key_cannot_grant_and_setup_restores_the_key(vault, home):
    ah = throwaway_approver_home(home)
    aid = request("pay", "--to", "bob")
    (ah / "approver.key").unlink()
    code, shown = human(home, ["approve", aid], "bob")
    assert code == 2 and "approver_key_missing" in shown and approvals.load(aid)["status"] == "pending"
    assert togi("approver", "status")["data"]["key"] is False
    answers_ = iter([PASSPHRASE, PASSPHRASE, PASSPHRASE])            # the person proves the current one, picks a new one
    approver.setup(reader=lambda _p: next(answers_), n=2 ** 10)
    assert len((ah / "approver.key").read_bytes()) == 32
    approve(home, aid, "bob")


# ----------------------------------------------------------------------------- A.3: grants are signed


def test_a_grant_carries_a_mac_and_a_scope(vault, home):
    aid = request("pay", "--to", "bob")
    approve(home, aid, "bob")
    rec = approvals.load(aid)
    assert rec["scope"] == "test" and len(rec["grant_mac"]) == 64
    int(rec["grant_mac"], 16)
    out = togi("vault", "pay", "--to", "bob", "--approval", aid)
    assert out["data"] == {"to": "bob", "scope": "test", "surface": "cli"}


def test_a_denial_is_signed_too_and_cannot_be_flipped_into_an_approval(vault, home):
    aid = request("pay", "--to", "bob")
    assert human(home, ["deny", aid], "bob")[0] == 0
    rec = approvals.load(aid)
    assert rec["status"] == "denied" and rec["grant_mac"]
    assert togi_code("vault", "pay", "--to", "bob", "--approval", aid)[1]["error"]["code"] == "approval_denied"
    rec["status"] = "approved"                                          # flip the person's "no" into a "yes"
    approvals._write(rec)
    code, out = togi_code("vault", "pay", "--to", "bob", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_unsigned"


@pytest.mark.parametrize("how", ["no_mac", "garbage_mac", "own_key", "edited_expiry", "edited_decider",
                                 "edited_scope"])
def test_a_forged_or_edited_grant_is_refused_and_does_not_run(vault, home, how):
    aid = request("pay", "--to", "bob")
    rec = approvals.load(aid)
    if how in ("edited_expiry", "edited_decider", "edited_scope"):       # start from a REAL grant, then edit it
        approve(home, aid, "bob")
        rec = approvals.load(aid)
        if how == "edited_expiry":
            rec["expires"] = "2099-01-01T00:00:00.000Z"
        elif how == "edited_decider":
            rec["decided_by"] = "human:somebody_else"
        else:
            rec["scope"] = "default"
    else:                                                                # a record nobody ever approved
        rec.update(status="approved", decided_by="human:me", expires="2099-01-01T00:00:00.000Z", scope="test")
        if how == "garbage_mac":
            rec["grant_mac"] = "0" * 64
        elif how == "own_key":                                           # signed, but with a key that is not the approver's
            import hashlib
            import hmac

            rec["grant_mac"] = hmac.new(b"k" * 32, approvals.grant_payload(rec), hashlib.sha256).hexdigest()
    approvals._write(rec)
    code, out = togi_code("vault", "pay", "--to", "bob", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_unsigned" and out["status"] == "refused"
    assert "re-run this command without --approval" in out["next"]
    # the journal never saw it run
    lines = [json.loads(l) for l in (home / "state" / "vault" / "journal.jsonl").read_text().splitlines()]
    assert not [l for l in lines if l.get("approval") == aid]


def test_a_genuine_grant_is_the_control_for_the_forgeries_above(vault, home):
    aid = request("pay", "--to", "bob")
    approve(home, aid, "bob")
    assert togi("vault", "pay", "--to", "bob", "--approval", aid)["ok"] is True


def test_a_grant_copied_to_another_togi_home_is_refused(vault, home, vault_repo, tmp_path, monkeypatch):
    """Same approver, same bundle, same command and arguments: still worthless in a home it was not made in."""
    aid = request("pay", "--to", "bob")
    approve(home, aid, "bob")
    genuine = (home / "approvals" / f"{aid}.json").read_text()
    other = tmp_path / "other_home"
    (other / "approvals").mkdir(parents=True)
    monkeypatch.setenv("TOGI_HOME", str(other))
    togi("add", str(vault_repo))
    (other / "approvals" / f"{aid}.json").write_text(genuine)             # the copy
    code, out = togi_code("vault", "pay", "--to", "bob", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_unsigned"
    monkeypatch.setenv("TOGI_HOME", str(home))                            # ...and the original is untouched and works
    assert togi("vault", "pay", "--to", "bob", "--approval", aid)["data"]["scope"] == "test"


def test_a_grant_made_by_another_approver_is_refused(vault, home, monkeypatch, tmp_path):
    aid = request("pay", "--to", "bob")
    approve(home, aid, "bob")
    other_approver = tmp_path / "some_other_approver"
    monkeypatch.setenv("TOGI_APPROVER_HOME", str(other_approver))
    approver.create(PASSPHRASE, n=2 ** 10)                                # a different approver: different key
    assert (other_approver / "approver.key").read_bytes() != (throwaway_approver_home(home) / "approver.key").read_bytes()
    code, out = togi_code("vault", "pay", "--to", "bob", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_unsigned"


def test_a_grant_signed_in_test_scope_is_not_honoured_by_the_default_approver(vault, home, fake_pw_dir, monkeypatch):
    aid = request("pay", "--to", "bob")
    approve(home, aid, "bob")
    monkeypatch.delenv("TOGI_APPROVER_HOME")                              # now this process uses the fixed approver
    approver.create(PASSPHRASE, n=2 ** 10)                                # (in the fake pw dir, never the real one)
    assert paths.approver_home() == fake_pw_dir / ".togi"
    code, out = togi_code("vault", "pay", "--to", "bob", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_unsigned"


# ----------------------------------------------------------------------------- the attack from the refuter, end to end


def test_own_togi_home_plus_own_approver_over_a_pty_cannot_run_a_gated_command_as_default_scope(
        vault_repo, home, tmp_path, fake_pw_dir, monkeypatch):
    """The attack: TOGI_HOME=/tmp/mine, `togi add`, create an approver with a fake terminal, approve, run.
    Whatever the agent does, a gated command never sees `approval_scope == "default"`."""
    # The person's real approver, in the fixed home (a fake pw dir here, never the real one).
    monkeypatch.delenv("TOGI_APPROVER_HOME")
    approver.create(PASSPHRASE, n=2 ** 10)
    fixed_home = fake_pw_dir / ".togi"
    persons = contents(fixed_home)

    fixed = dict(drop_env=("TOGI_APPROVER_HOME",), fake_pw=fake_pw_dir)  # the agent's shell: no override variable
    mine = tmp_path / "mine"
    mine.mkdir()
    ATTACKER = "the attacker's own passphrase"
    seen = []                                                            # every scope a command ever saw

    def run_gated(aid_or_none, *words, extra=None, **kw):
        argv = ["vault", *words] + (["--approval", aid_or_none] if aid_or_none else [])
        r = run_togi(mine, *argv, **kw)
        body = json.loads(r.stdout)
        if body.get("ok"):
            seen.append(body["data"]["scope"])
        return r.returncode, body

    assert run_togi(mine, "add", str(vault_repo), **fixed).returncode == 0
    code, body = run_gated(None, "pay", "--to", "bob", **fixed)
    assert code == 3
    aid = body["approval"]["id"]

    # 1. try to replace the person's approver with its own: setup asks for the CURRENT passphrase first
    code, shown = run_in_pty(mine, ["approver", "setup"], typed=[("passphrase", ATTACKER)], **fixed)
    assert code == 2 and "wrong_passphrase" in shown and "New approver passphrase" not in shown
    # 2. approve against the person's approver with its own passphrase
    code, shown = run_in_pty(mine, ["approve", aid], typed=answers("bob", ATTACKER), **fixed)
    assert code == 2 and "wrong_passphrase" in shown
    assert run_gated(aid, "pay", "--to", "bob", **fixed)[1]["error"]["code"] == "approval_pending"
    # 3. use an approver of its own, through the override, and approve its own request
    code, shown = run_in_pty(mine, ["approver", "setup"], typed=[("New approver passphrase", ATTACKER),
                                                                 ("again", ATTACKER)])
    assert code == 0, shown
    assert throwaway_approver_home(mine).is_dir() and (throwaway_approver_home(mine) / "approver.key").is_file()
    code, shown = run_in_pty(mine, ["approve", aid], typed=answers("bob", ATTACKER))
    assert code == 0, shown
    # 3a. ...and run it with the override: it runs, but as scope "test"
    code, body = run_gated(aid, "pay", "--to", "bob")
    assert code == 0 and body["data"]["scope"] == "test"
    # 3b. a bundle that moves real value refuses it
    aid2 = run_gated(None, "pay", "real", "--to", "bob")[1]["approval"]["id"]
    assert run_in_pty(mine, ["approve", aid2], typed=answers("bob", ATTACKER))[0] == 0
    code, body = run_gated(aid2, "pay", "real", "--to", "bob")
    assert code == 2 and body["error"]["code"] == "test_scope_refused"
    # 4. the grant its own approver signed, presented where the real approver is in force: refused
    aid3 = run_gated(None, "pay", "--to", "carol")[1]["approval"]["id"]
    assert run_in_pty(mine, ["approve", aid3], typed=answers("carol", ATTACKER))[0] == 0
    code, body = run_gated(aid3, "pay", "--to", "carol", **fixed)
    assert code == 2 and body["error"]["code"] == "approval_unsigned"
    # 5. forging a record that CLAIMS scope "default", signed with its own key: refused under the override...
    aid4 = run_gated(None, "pay", "--to", "dave")[1]["approval"]["id"]
    monkeypatch.setenv("TOGI_HOME", str(mine))
    monkeypatch.setenv("TOGI_APPROVER_HOME", str(throwaway_approver_home(mine)))
    rec = approvals.load(aid4)
    rec.update(status="approved", decided_by="human:forged", expires="2099-01-01T00:00:00.000Z", scope="default")
    rec["grant_mac"] = approvals.sign_grant(rec)                       # it holds its own key, so it can sign anything
    approvals._write(rec)
    code, body = run_gated(aid4, "pay", "--to", "dave")
    assert code == 2 and body["error"]["code"] == "approval_unsigned"
    # ...and under the fixed approver (the key is not the person's)
    code, body = run_gated(aid4, "pay", "--to", "dave", **fixed)
    assert code == 2 and body["error"]["code"] == "approval_unsigned"

    assert seen and set(seen) == {"test"}                                # never "default"
    after = contents(fixed_home)                                         # the person's approver is exactly as it was
    assert after["approver"] == persons["approver"] and after["approver.key"] == persons["approver.key"]


def test_the_person_with_the_real_approver_gets_default_scope(vault_repo, home, tmp_path, fake_pw_dir, monkeypatch):
    """The other half: the fixed approver, no override, yields `default` -- the only way to a real-money run."""
    monkeypatch.delenv("TOGI_APPROVER_HOME")
    approver.create(PASSPHRASE, n=2 ** 10)
    fixed = dict(drop_env=("TOGI_APPROVER_HOME",), fake_pw=fake_pw_dir)
    assert run_togi(home, "add", str(vault_repo), **fixed).returncode == 0
    aid = json.loads(run_togi(home, "vault", "pay", "real", "--to", "bob", **fixed).stdout)["approval"]["id"]
    code, shown = human(home, ["approve", aid], "bob", **fixed)
    assert code == 0, shown
    assert approvals.load(aid)["scope"] == "default"
    r = run_togi(home, "vault", "pay", "real", "--to", "bob", "--approval", aid, **fixed)
    assert r.returncode == 0 and json.loads(r.stdout)["data"] == {"to": "bob", "paid": True, "scope": "default"}


# ----------------------------------------------------------------------------- A.4: scope reaches the command on every surface


def test_scope_is_none_without_an_approval(vault, home):
    assert togi("vault", "whoami")["data"] == {"scope": None}


def test_scope_reaches_the_command_on_the_cli(vault, home):
    aid = request("pay", "--to", "bob")
    approve(home, aid, "bob")
    out = togi("vault", "pay", "--to", "bob", "--approval", aid)
    assert out["data"] == {"to": "bob", "scope": "test", "surface": "cli"}
    entry = [json.loads(l) for l in (home / "state" / "vault" / "journal.jsonl").read_text().splitlines()][-1]
    assert entry["approval"] == aid and entry["approval_scope"] == "test"        # and it is journaled


def test_scope_reaches_the_command_through_togi_run(vault, home):
    aid = request("pay", "--to", "bob")
    approve(home, aid, "bob")
    import subprocess

    script = textwrap.dedent(f"""
        r = vault.pay(to="bob", approval={aid!r})
        print(r["scope"], r["surface"])
        print(vault.whoami())
        """)
    p = subprocess.run([sys.executable, "-m", "togi", "run", "-"], input=script, capture_output=True, text=True,
                       env=clean_env(home), timeout=90)
    assert p.returncode == 0, p.stderr
    assert p.stdout.splitlines() == ["test run", "{'scope': None}"]


def test_scope_reaches_the_command_through_mcp_in_process(vault, home):
    aid = request("pay", "--to", "bob")
    approve(home, aid, "bob")
    loaded = registry.load("vault")
    env, is_err = mcp_bridge.call_tool(loaded, "vault_pay", {"to": "bob", "approval": aid})
    assert is_err is False and env["data"] == {"to": "bob", "scope": "test", "surface": "mcp"}
    assert mcp_bridge.call_tool(loaded, "vault_whoami", {})[0]["data"] == {"scope": None}


def test_scope_reaches_the_command_through_a_real_mcp_server(vault, home):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    aid = request("pay", "--to", "bob")
    approve(home, aid, "bob")

    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "togi", "mcp", "serve", "vault"],
                                       env=clean_env(home, TOGI_SESSION="mcp-test", TOGI_AGENT="mcp-client"))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await session.call_tool("vault_pay", {"to": "bob", "approval": aid})

    res = asyncio.run(go())
    assert res.isError is False and json.loads(res.content[0].text)["data"] == {"to": "bob", "scope": "test",
                                                                                 "surface": "mcp"}


def test_the_prompt_tells_the_person_when_the_approver_is_a_test_one(vault, home):
    aid = request("pay", "--to", "bob")
    shown = approve(home, aid, "bob")
    assert "TEST scope" in shown and approvals.load(aid)["scope"] == "test"


def test_the_context_has_the_attribute_and_defaults_to_none():
    from togi import Context

    assert Context().approval_scope is None and Context(approval_scope="test").approval_scope == "test"


# ----------------------------------------------------------------------------- doctor, sandbox, stone


def test_doctor_warns_when_togi_approver_home_is_set(home, monkeypatch):
    out = togi("doctor")
    row = [r for r in out["data"]["checks"] if r["check"] == "TOGI_APPROVER_HOME not set"][0]
    assert row["ok"] is False and row["level"] == "warn" and "scope 'test'" in row["detail"]
    monkeypatch.delenv("TOGI_APPROVER_HOME")
    row = [r for r in togi("doctor")["data"]["checks"] if r["check"] == "TOGI_APPROVER_HOME not set"][0]
    assert row["ok"] is True


def test_doctor_reports_the_key_and_a_legacy_approver(home):
    rows = {r["check"]: r for r in togi("doctor")["data"]["checks"]}
    assert rows["approver key"]["ok"] is True and rows["approver key permissions"]["ok"] is True
    (home / "approver").write_text("{}")                                  # an r1 approver inside $TOGI_HOME
    rows = {r["check"]: r for r in togi("doctor")["data"]["checks"]}
    assert rows["legacy approver"]["ok"] is False and "no longer read" in rows["legacy approver"]["detail"]


def test_the_run_sandbox_hides_the_approver_home(tmp_path):
    from togi import sandbox

    profile = sandbox.build_profile(tmp=str(tmp_path / "t"), repos=[], togi_home=str(tmp_path / "h"),
                                    home=str(tmp_path / "u"), also_hidden=(str(tmp_path / "appr"),))
    assert str((tmp_path / "appr").resolve()) in profile.split("(deny file* ")[-1]


def test_a_nested_togi_in_a_run_child_keeps_the_hosts_approver_home(home, tmp_path):
    from togi import runmode

    env = runmode.scrubbed_env(str(tmp_path))
    assert env["TOGI_APPROVER_HOME"] == str(throwaway_approver_home(home))


def test_a_stone_sandbox_never_sees_the_real_approver(notes, home):
    from togi import stone

    sb = stone.Sandbox(notes, "notes")
    try:
        env = sb.env(agent=True)
        assert env["TOGI_APPROVER_HOME"] == str(sb.approver_home) and sb.approver_home.parent == sb.root
        assert sb.approver_home != paths.fixed_approver_home()
        cfg = stone.mcp_config(sb)["mcpServers"][stone.MCP_SERVER_KEY]["env"]
        assert cfg["TOGI_APPROVER_HOME"] == str(sb.approver_home)
    finally:
        sb.cleanup()


# ----------------------------------------------------------------------------- the guard itself


def test_no_test_resolves_the_fixed_approver_to_the_real_home(home, monkeypatch, fake_pw_dir):
    monkeypatch.delenv("TOGI_APPROVER_HOME")
    assert paths.approver_home() == fake_pw_dir / ".togi" != REAL_PW_DIR / ".togi"


def test_the_real_approver_guard_detects_a_write(tmp_path):
    """The autouse fixture compares `approver_files(<real home>/.togi)` before and after each test; this proves
    that comparison fails when an approver file appears, changes or goes."""
    root = tmp_path / "fake_real_home"
    root.mkdir()
    before = approver_files(root)
    assert before == {}
    (root / "approver.key").write_bytes(b"x" * 32)
    assert approver_files(root) != before
    snap = approver_files(root)
    (root / "approver.key").write_bytes(b"y" * 33)
    assert approver_files(root) != snap
    (root / ".approver-abc.tmp").write_text("x")                           # a half-written temp file counts too
    assert ".approver-abc.tmp" in approver_files(root)
    assert approver_files(tmp_path / "does_not_exist") == {}


def test_the_real_home_has_the_same_approver_files_as_when_the_suite_started():
    assert approver_files(REAL_PW_DIR / ".togi") == conftest.REAL_APPROVER_AT_START
