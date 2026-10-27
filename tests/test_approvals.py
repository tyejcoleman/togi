"""Money-effect approvals: pending request, binding to command+args hash, single use, expiry, TTY-only."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time

import pytest

from togi import approvals

from conftest import (clean_env, PASSPHRASE, answers, human, lock_bundle, run_in_pty, run_togi, throwaway_approver_home,
                      togi, togi_code)

TIP = ["notes", "tip", "send", "--id", "n_1", "--amount", "5.00"]


@pytest.fixture
def tipped(notes):
    lock_bundle("notes")     # the money path needs a verified lock (notes has locked tiers)
    togi("notes", "add", "--text", "tip me")
    return notes


def request_approval():
    code, out = togi_code(*TIP)
    assert code == 3, out
    return out


def test_money_call_returns_pending_request_and_does_not_execute(tipped):
    out = request_approval()
    assert out["ok"] is False and out["status"] == "needs_approval"
    a = out["approval"]
    assert a["id"].startswith("ap_") and a["command"] == "notes tip send"
    assert a["args"] == {"id": "n_1", "amount": "5.00"} and "tip" in a["summary"].lower()
    assert out["next"] == f"a person runs: togi approve {a['id']}"
    assert togi("notes", "tip", "total")["data"]["count"] == 0  # NOT executed
    pending = togi("approvals")["data"]
    assert [p["id"] for p in pending] == [a["id"]] and pending[0]["status"] == "pending"


def test_identical_retry_reuses_the_pending_request(tipped):
    a1 = request_approval()["approval"]["id"]
    a2 = request_approval()["approval"]["id"]
    assert a1 == a2


def test_approval_pending_is_not_enough(tipped):
    aid = request_approval()["approval"]["id"]
    code, out = togi_code(*TIP, "--approval", aid)
    assert code == 3 and out["error"]["code"] == "approval_pending" and out["approval"]["id"] == aid
    assert togi("notes", "tip", "total")["data"]["count"] == 0


def test_approve_refused_without_a_tty(tipped, home):
    aid = request_approval()["approval"]["id"]
    r = run_togi(home, "approve", aid, input="n_1\n")
    assert r.returncode == 2
    out = json.loads(r.stdout)
    assert out["status"] == "refused" and out["error"]["code"] == "needs_tty"
    assert approvals.load(aid)["status"] == "pending"


@pytest.mark.parametrize("var,value", [("TOGI_AGENT", "1"), ("TOGI_AGENT", ""), ("TOGI_RUN", "1"), ("TOGI_RUN", "")])
def test_approve_refused_for_agents_even_on_a_tty(tipped, home, var, value):
    """Item 5: an agent is any session where TOGI_AGENT is SET, even to the empty string."""
    aid = request_approval()["approval"]["id"]
    code, out = run_in_pty(home, ["approve", aid], typed=answers("n_1"), env_extra={var: value})
    assert code == 2 and "human_only" in out
    assert approvals.load(aid)["status"] == "pending"


def human_approve(home, aid, word="n_1", decision="approve", passphrase=PASSPHRASE):
    return human(home, [decision, aid], word, passphrase)


def test_full_flow_approve_then_run_once(tipped, home):
    aid = request_approval()["approval"]["id"]
    code, shown = human_approve(home, aid)
    assert code == 0, shown
    assert "tip" in shown.lower() and 'Type "n_1"' in shown
    rec = approvals.load(aid)
    assert rec["status"] == "approved" and rec["decided_by"].startswith("human:")
    out = togi(*TIP, "--approval", aid)
    assert out["ok"] is True and out["data"] == {"note": "n_1", "amount": "5.00"}
    assert togi("notes", "tip", "total")["data"]["count"] == 1
    # single use
    code, again = togi_code(*TIP, "--approval", aid)
    assert code == 2 and again["error"]["code"] == "approval_used"
    assert togi("notes", "tip", "total")["data"]["count"] == 1
    assert approvals.load(aid)["status"] == "consumed"


def test_wrong_confirmation_word_changes_nothing(tipped, home):
    aid = request_approval()["approval"]["id"]
    code, shown = human_approve(home, aid, word="yes")
    assert code == 2 and "confirmation_mismatch" in shown
    assert approvals.load(aid)["status"] == "pending"


def test_approval_binds_to_exact_args(tipped, home):
    aid = request_approval()["approval"]["id"]
    assert human_approve(home, aid)[0] == 0
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "500.00", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_mismatch"
    assert togi("notes", "tip", "total")["data"]["count"] == 0
    # still usable for the exact call (a mismatch does not burn it)
    assert togi(*TIP, "--approval", aid)["ok"] is True


def test_approval_binds_to_command(tipped, home, notes_repo):
    # make admin gated too, so `delete` is a different gated command
    toml = notes_repo / "togi.toml"
    toml.write_text(toml.read_text().replace('approve_effects = ["money"]', 'approve_effects = ["money", "admin"]'))
    lock_bundle("notes")     # togi.toml is locked: a person re-locks after editing it
    aid = request_approval()["approval"]["id"]
    assert human_approve(home, aid)[0] == 0
    code, out = togi_code("notes", "delete", "--id", "n_1", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_mismatch"
    code, out = togi_code("notes", "delete", "--id", "n_1")
    assert code == 3 and out["approval"]["command"] == "notes delete"  # policy-gated effect


def test_defaults_do_not_change_the_binding(tipped, home):
    # amount spelled the same; --sort etc. irrelevant. Same canonical args => same hash
    from togi.approvals import args_hash

    assert args_hash("notes", "tip send", {"id": "n_1", "amount": "5.00"}) == \
        args_hash("notes", "tip send", {"amount": "5.00", "id": "n_1"})


def test_expiry_after_approval(tipped, home):
    aid = request_approval()["approval"]["id"]
    assert human_approve(home, aid)[0] == 0
    rec = approvals.load(aid)
    rec["expires"] = "2020-01-01T00:00:00.000Z"
    rec["grant_mac"] = approvals.sign_grant(rec)    # as if the approver had signed it with that expiry
    approvals._write(rec)
    code, out = togi_code(*TIP, "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_expired"
    assert togi("notes", "tip", "total")["data"]["count"] == 0


def test_pending_request_expires_and_cannot_be_approved(tipped, home):
    aid = request_approval()["approval"]["id"]
    rec = approvals.load(aid)
    rec["expires"] = "2020-01-01T00:00:00.000Z"
    approvals._write(rec)
    code, shown = human_approve(home, aid)
    assert code == 2 and "approval_not_pending" in shown
    # a fresh identical call makes a NEW request rather than reusing the expired one
    assert request_approval()["approval"]["id"] != aid


def test_default_ttl_is_15_minutes_after_approval(tipped, home):
    aid = request_approval()["approval"]["id"]
    assert human_approve(home, aid)[0] == 0
    from togi.journal import parse_ts

    left = parse_ts(approvals.load(aid)["expires"]) - time.time()
    assert 14 * 60 < left <= 15 * 60 + 2


def test_deny_blocks_use(tipped, home):
    aid = request_approval()["approval"]["id"]
    assert human_approve(home, aid, decision="deny")[0] == 0
    code, out = togi_code(*TIP, "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_denied"


def test_unknown_approval_id(tipped):
    code, out = togi_code(*TIP, "--approval", "ap_nope")
    assert code == 2 and out["error"]["code"] == "approval_not_found"


def test_claim_is_atomic(tipped):
    aid = request_approval()["approval"]["id"]
    approvals.claim(aid)
    with pytest.raises(Exception) as e:
        approvals.claim(aid)
    assert getattr(e.value, "code", "") == "approval_used"


def test_failed_execution_still_consumes_the_approval(tipped, home):
    # amount 0 passes arg parsing but the function refuses it; the approval is spent (conservative)
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "0")
    aid = out["approval"]["id"]
    assert human_approve(home, aid)[0] == 0
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "0", "--approval", aid)
    assert code == 1 and out["error"]["code"] == "bad_amount"
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "0", "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_used"


def test_journal_never_records_money_values(tipped, home):
    request_approval()
    text = (home / "state" / "notes" / "journal.jsonl").read_text()
    assert "5.00" not in text and "tip me" not in text
    last = json.loads(text.strip().splitlines()[-1])
    assert last["command"] == "tip send" and last["status"] == "needs_approval"
    assert last["args_shape"] == {"id": "str", "amount": "decimal"} and last["args_hash"] is None


# ----------------------------------------------------------------------------- item 1: the approver passphrase


def journal_host(home):
    p = home / "state" / "_host" / "journal.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def test_the_typed_word_alone_is_not_enough(tipped, home):
    """Before the fix a pty + the confirmation word was all it took. Now the passphrase is also required."""
    aid = request_approval()["approval"]["id"]
    code, shown = human_approve(home, aid, passphrase="not the passphrase!!")
    assert code == 2 and "wrong_passphrase" in shown and "4 attempt(s) left" in shown
    assert approvals.load(aid)["status"] == "pending"
    code, out = togi_code(*TIP, "--approval", aid)
    assert code == 3 and out["error"]["code"] == "approval_pending"


@pytest.mark.skipif(shutil.which("script") is None, reason="script(1) not available")
def test_script_pty_repro_from_the_refuter_no_longer_approves(tipped, home):
    """`(sleep 1; printf 'n_1\\n') | script -q /dev/null togi approve ap_...` : an agent with a pty and the word."""
    aid = request_approval()["approval"]["id"]
    env = clean_env(home, TOGI_PASSPHRASE_TIMEOUT="2")
    if sys.platform == "darwin":
        cmd = f"(sleep 1; printf 'n_1\\n'; sleep 1) | script -q /dev/null {sys.executable} -m togi approve {aid}"
    else:
        cmd = f"(sleep 1; printf 'n_1\\n'; sleep 1) | script -q -c '{sys.executable} -m togi approve {aid}' /dev/null"
    subprocess.run(["sh", "-c", cmd], env=env, capture_output=True, text=True, timeout=60)
    assert approvals.load(aid)["status"] == "pending"


def test_waiting_for_a_passphrase_that_never_comes_times_out_instead_of_hanging(tipped, home):
    """The script(1) repro used to hang an agent's shell forever once it could not supply the passphrase."""
    aid = request_approval()["approval"]["id"]
    t0 = time.monotonic()
    code, shown = run_in_pty(home, ["approve", aid], typed=[('Type "', "n_1")], env_extra={"TOGI_PASSPHRASE_TIMEOUT": "1.5"})
    assert code == 2 and "passphrase_timeout" in shown and time.monotonic() - t0 < 20
    assert approvals.load(aid)["status"] == "pending"
    assert [a["error_code"] for a in journal_host(home) if a["event"] == "passphrase_attempt"] == ["passphrase_timeout"]


def test_no_approver_means_approve_refuses_and_says_how_to_set_one_up(tipped, home):
    aid = request_approval()["approval"]["id"]
    ah = throwaway_approver_home(home)
    (ah / "approver").unlink()
    code, shown = human_approve(home, aid)
    assert code == 2 and "no_approver" in shown and "togi approver setup" in shown
    assert approvals.load(aid)["status"] == "pending"
    assert togi("approver", "status")["data"] == {
        "configured": False, "created": None, "locked": False, "locked_for_seconds": 0, "wrong_attempts": 0,
        "path": str(ah / "approver"), "scope": "test", "key": True, "next": "a person runs: togi approver setup"}


def test_deny_needs_the_passphrase_too(tipped, home):
    aid = request_approval()["approval"]["id"]
    code, shown = human_approve(home, aid, decision="deny", passphrase="wrong wrong wrong")
    assert code == 2 and "wrong_passphrase" in shown and approvals.load(aid)["status"] == "pending"


def test_five_wrong_passphrases_lock_the_approver_for_ten_minutes_and_each_attempt_is_journaled(tipped, home):
    from togi import approver

    aid = request_approval()["approval"]["id"]

    def wrong(_prompt):
        return "definitely not it"

    for i in range(4):
        with pytest.raises(Exception) as e:
            approver.require("approve", reader=wrong)
        assert e.value.code == "wrong_passphrase"
    with pytest.raises(Exception) as e:
        approver.require("approve", reader=wrong)
    assert e.value.code == "approver_locked" and "10 minutes" in e.value.message
    # locked: even the RIGHT passphrase is refused, from a real terminal too, and the approval is untouched
    code, shown = human_approve(home, aid)
    assert code == 2 and "approver_locked" in shown and approvals.load(aid)["status"] == "pending"
    ah = throwaway_approver_home(home)
    st = json.loads((ah / "approver.state").read_text())
    assert 590 < st["locked_until"] - time.time() <= 600
    attempts = [e for e in journal_host(home) if e["event"] == "passphrase_attempt"]
    assert [a["error_code"] for a in attempts] == ["wrong_passphrase"] * 5 + ["approver_locked"]
    assert all(a["ok"] is False and a["for_action"] == "approve" for a in attempts)
    # after the ten minutes it works again, and a good passphrase clears the counter
    st["locked_until"] = time.time() - 1
    (ah / "approver.state").write_text(json.dumps(st))
    assert human_approve(home, aid)[0] == 0
    assert json.loads((ah / "approver.state").read_text())["failures"] == 0


def test_a_good_passphrase_resets_the_wrong_count(tipped, home):
    from togi import approver

    for _ in range(3):
        with pytest.raises(Exception):
            approver.require("approve", reader=lambda _p: "nope nope nope nope")
    approver.require("approve", reader=lambda _p: PASSPHRASE)
    assert approver.status()["wrong_attempts"] == 0


def test_approver_record_is_scrypt_and_0600_and_has_no_plaintext(home):
    from togi import approver

    approver.create("a different long passphrase", n=2 ** 10, replace=True)
    p = throwaway_approver_home(home) / "approver"
    assert (p.stat().st_mode & 0o777) == 0o600
    rec = json.loads(p.read_text())
    assert rec["kdf"] == "scrypt" and rec["n"] == 1024 and len(rec["salt"]) >= 16 and rec["hash"]
    assert "different" not in p.read_text()
    assert approver.check(rec, "a different long passphrase") and not approver.check(rec, "a different long passphras")
    with pytest.raises(Exception) as e:
        approver.create("short one", n=2 ** 10, replace=True)
    assert e.value.code == "passphrase_too_short"
    default = approver._kdf  # production cost: 2**17 (128 MiB), stored in the record so it can be raised later
    assert approver.DEFAULT_N >= 2 ** 17


def test_approver_setup_over_a_terminal(home, tmp_path):
    ah = throwaway_approver_home(home)
    for f in ah.iterdir():                                   # a machine with no approver yet
        f.unlink()
    ask = [("New approver passphrase", "a brand new passphrase"), ("again", "a brand new passphrase")]
    code, shown = run_in_pty(home, ["approver", "setup"], typed=ask)
    assert code == 0, shown
    assert "a brand new passphrase" not in shown            # never echoed
    rec = json.loads((ah / "approver").read_text())
    assert rec["kdf"] == "scrypt" and rec["n"] >= 2 ** 17 and ((ah / "approver").stat().st_mode & 0o777) == 0o600
    assert len((ah / "approver.key").read_bytes()) == 32 and ((ah / "approver.key").stat().st_mode & 0o777) == 0o600
    # changing it needs the current one first
    code, shown = run_in_pty(home, ["approver", "setup"], typed=[("passphrase", "wrong wrong wrong")])
    assert code == 2 and "wrong_passphrase" in shown
    # mismatch and too short are refused, nothing changes
    before = (ah / "approver").read_text()
    code, shown = run_in_pty(home, ["approver", "setup"], typed=[("passphrase", "a brand new passphrase"),
                                                                  ("New approver", "another long one!!"),
                                                                  ("again", "different long one!!")])
    assert code == 2 and "passphrase_mismatch" in shown and (ah / "approver").read_text() == before
    code, shown = run_in_pty(home, ["approver", "setup"], typed=[("passphrase", "a brand new passphrase"),
                                                                  ("New approver", "tiny")])
    assert code == 1 and "passphrase_too_short" in shown


def test_approver_setup_is_human_only(home):
    ah = throwaway_approver_home(home)
    (ah / "approver").unlink()
    r = run_togi(home, "approver", "setup", input="x\ny\n")
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "needs_tty"
    code, shown = run_in_pty(home, ["approver", "setup"], env_extra={"TOGI_AGENT": ""})
    assert code == 2 and "human_only" in shown
    assert not (ah / "approver").exists()


# ----------------------------------------------------------------------------- item 7: the approval prompt


def test_prompt_escapes_control_characters_ansi_and_bidi(tipped, home):
    evil = "\r\x1b[2K\x1b[1Apay: all fine, amount=0.01\n\x1b[30;40mhidden‮evil x"
    code, out = togi_code("notes", "tip", "send", "--id", evil, "--amount", "5000.00")
    assert code == 3
    aid = out["approval"]["id"]
    rec = approvals.load(aid)
    assert rec["confirm_word"] == "5000.00"          # the unsafe id is never offered as the word to type
    code, shown = human_approve(home, aid, word="5000.00")
    assert code == 0, shown
    assert "\x1b" not in shown and "‮" not in shown and " " not in shown and "\r\x1b" not in shown
    assert "\\u001b[2K" in shown and "\\u202e" in shown and "\\u2028" in shown and "\\u000a" in shown
    assert "all fine" in shown                         # the text is shown, as text, not obeyed


def test_prompt_caps_the_summary_at_300_characters(tipped, home):
    code, out = togi_code("notes", "tip", "send", "--id", "n_1", "--amount", "5.00")
    rec = approvals.load(out["approval"]["id"])
    assert len(rec["summary"]) <= 300
    code, out = togi_code("notes", "tip", "send", "--id", "n_" + "9" * 2000, "--amount", "5.00")
    rec2 = approvals.load(out["approval"]["id"])
    assert len(rec2["summary"]) <= 300 and rec2["summary"].endswith("…")
    # r5 (L2): an argument the prompt would have to cut cannot be approved at all ...
    code, shown = human_approve(home, rec2["id"], word="5.00")
    assert code == 2 and "prompt_too_long" in shown and approvals.load(rec2["id"])["status"] == "pending"
    # ... but one that fits (up to 300 characters) is shown whole, in lines that stay short
    code, out = togi_code("notes", "tip", "send", "--id", "n_" + "9" * 250, "--amount", "5.00")
    code, shown = human_approve(home, out["approval"]["id"], word="5.00")
    assert code == 0 and max(len(l) for l in shown.splitlines()) < 700


def test_text_output_of_host_commands_cannot_inject_escapes(tipped, home):
    togi_code("notes", "tip", "send", "--id", "n_\x1b[2J", "--amount", "5.00")
    from togi import output

    txt = output.render_text(togi("approvals"))
    assert "\x1b" not in txt and "\\u001b" in txt


# ----------------------------------------------------------------------------- item 15: what an approval is bound to


def approved(home, notes_repo):
    aid = request_approval()["approval"]["id"]
    assert human_approve(home, aid)[0] == 0
    return aid


def test_approval_records_repo_version_and_locked_digest(tipped, home, notes_repo):
    rec = approvals.load(request_approval()["approval"]["id"])
    assert rec["binding"]["repo"] == str(notes_repo.resolve()) and rec["binding"]["version"] == "0.1.0"
    assert len(rec["binding"]["locked_digest"]) == 64 and rec["binding_hash"]


def test_prompt_shows_the_repo_path(tipped, home, notes_repo):
    aid = request_approval()["approval"]["id"]
    _, shown = human_approve(home, aid)
    assert str(notes_repo.resolve()) in shown and "version: 0.1.0" in shown


def test_approval_dies_when_the_manifest_version_changes(tipped, home, notes_repo):
    aid = approved(home, notes_repo)
    t = notes_repo / "togi.toml"
    t.write_text(t.read_text().replace('version = "0.1.0"', 'version = "9.9.9"'))
    code, out = togi_code(*TIP, "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_mismatch" and "locked files" in out["error"]["message"]
    assert togi("notes", "tip", "total")["data"]["count"] == 0
    t.write_text(t.read_text().replace('version = "9.9.9"', 'version = "0.1.0"'))
    assert togi(*TIP, "--approval", aid)["ok"] is True      # put back exactly: valid again (and then spent)


def test_approval_dies_when_a_locked_file_changes(tipped, home, notes_repo):
    aid = approved(home, notes_repo)
    store = notes_repo / "src" / "notes" / "store.py"
    store.write_text(store.read_text() + "\n# a tampered engine\n")
    code, out = togi_code(*TIP, "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_mismatch"
    assert togi("notes", "tip", "total")["data"]["count"] == 0


def test_approval_dies_when_the_bundle_moves_to_another_repo_path(tipped, home, notes_repo, tmp_path):
    aid = approved(home, notes_repo)
    other = tmp_path / "elsewhere" / "notes"
    shutil.copytree(notes_repo, other)
    togi("remove", "notes")
    togi("add", str(other))
    code, out = togi_code(*TIP, "--approval", aid)
    assert code == 2 and out["error"]["code"] == "approval_mismatch"


def test_an_approval_from_before_the_binding_existed_is_refused(tipped, home, notes_repo):
    aid = approved(home, notes_repo)
    rec = approvals.load(aid)
    rec.pop("binding_hash")
    rec.pop("binding")
    approvals._write(rec)
    assert togi_code(*TIP, "--approval", aid)[1]["error"]["code"] == "approval_mismatch"


# ----------------------------------------------------------------------------- item 14: pending approvals are capped


def test_at_most_20_pending_approvals_per_bundle(tipped, home):
    for i in range(1, 21):
        togi("notes", "add", "--text", f"n{i}")
    ids = []
    for i in range(1, 21):
        code, out = togi_code("notes", "tip", "send", "--id", f"n_{i}", "--amount", "1.00")
        assert code == 3, out
        ids.append(out["approval"]["id"])
    code, out = togi_code("notes", "tip", "send", "--id", "n_21", "--amount", "1.00")
    assert code == 2 and out["error"]["code"] == "too_many_pending" and "togi approvals" in out["next"]
    assert len(togi("approvals")["data"]) == 20
    # the same call again still returns its existing request instead of a refusal
    code, out = togi_code("notes", "tip", "send", "--id", "n_3", "--amount", "1.00")
    assert code == 3 and out["approval"]["id"] == ids[2]
    # a person clearing one makes room
    assert human_approve(home, ids[0], word="n_1", decision="deny")[0] == 0
    assert togi_code("notes", "tip", "send", "--id", "n_21", "--amount", "1.00")[0] == 3
