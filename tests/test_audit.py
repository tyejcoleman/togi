"""Audit chain (verify + tamper detection) and locked-file hashes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from togi import audit
from togi.manifest import load_manifest

from conftest import PASSPHRASE, human, human_relock, lock_bundle, run_in_pty, run_togi, togi, togi_code


def add_entries(repo: Path, n=3, bundle="notes"):
    for i in range(n):
        audit.append(repo, bundle, action="apply" if i % 2 == 0 else "reject", proposal=f"p_{i}", tier="polish",
                     files=["skills/SKILL.md"], diff_sha256="ab" * 32, stone_before={"x": i}, stone_after={"x": i + 1},
                     decided_by="auto-polish", reason=f"r{i}")


def lines(repo):
    return (repo / ".togi" / "audit.jsonl").read_text().splitlines()


def test_entry_shape_and_chain(notes, home):
    add_entries(notes, 3)
    es = audit.read(notes)
    assert [e["seq"] for e in es] == [1, 2, 3]
    assert set(es[0]) == {"seq", "ts", "action", "proposal", "tier", "files", "diff_sha256", "stone_before",
                          "stone_after", "decided_by", "reason", "scope", "prev_hash", "hash", "kid", "mac"}
    assert es[0]["prev_hash"] == "0" * 64 and es[1]["prev_hash"] == es[0]["hash"] == audit.entry_hash(es[0])
    v = audit.verify(notes, "notes")
    assert v["ok"] and v["entries"] == 3 and v["problems"] == [] and v["head"] == "ok" and v["authenticated"]


def test_edit_detected(notes, home):
    add_entries(notes, 3)
    ls = lines(notes)
    e = json.loads(ls[1])
    e["reason"] = "tampered"
    ls[1] = json.dumps(e, sort_keys=True, separators=(",", ":"))
    (notes / ".togi" / "audit.jsonl").write_text("\n".join(ls) + "\n")
    v = audit.verify(notes, "notes")
    assert not v["ok"] and v["problems"][0]["seq"] == 2 and "edited" in v["problems"][0]["problem"]


def test_edit_with_recomputed_hash_breaks_next_link(notes, home):
    add_entries(notes, 3)
    ls = lines(notes)
    e = json.loads(ls[1])
    e["reason"] = "tampered"
    e["hash"] = audit.entry_hash(e)
    ls[1] = json.dumps(e, sort_keys=True, separators=(",", ":"))
    (notes / ".togi" / "audit.jsonl").write_text("\n".join(ls) + "\n")
    v = audit.verify(notes)
    # the edited entry no longer carries a valid mac (no key to re-sign it), and the next link is broken too
    assert not v["ok"] and {p["kind"] for p in v["problems"]} >= {"mac", "chain"}
    assert any(p["seq"] == 3 and "prev_hash" in p["problem"] for p in v["problems"])


def test_deleting_middle_entry_detected(notes, home):
    add_entries(notes, 3)
    ls = lines(notes)
    (notes / ".togi" / "audit.jsonl").write_text("\n".join([ls[0], ls[2]]) + "\n")
    v = audit.verify(notes)
    assert not v["ok"] and any("seq" in p["problem"] or "prev_hash" in p["problem"] for p in v["problems"])


def test_reordering_detected(notes, home):
    add_entries(notes, 3)
    ls = lines(notes)
    (notes / ".togi" / "audit.jsonl").write_text("\n".join([ls[1], ls[0], ls[2]]) + "\n")
    assert not audit.verify(notes)["ok"]


def test_truncating_the_tail_is_caught_by_the_head_pointer(notes, home):
    add_entries(notes, 3)
    ls = lines(notes)
    (notes / ".togi" / "audit.jsonl").write_text("\n".join(ls[:2]) + "\n")
    assert audit.verify(notes)["ok"]  # the file alone cannot tell...
    v = audit.verify(notes, "notes")  # ...but the head recorded in $TOGI_HOME can
    assert not v["ok"] and v["head"] == "truncated" and "removed from the end" in v["problems"][0]["problem"]


def test_garbage_line_detected(notes, home):
    add_entries(notes, 2)
    with open(notes / ".togi" / "audit.jsonl", "a") as f:
        f.write("not json\n")
    assert not audit.verify(notes)["ok"]


def test_togi_log_and_verify_commands(notes, home):
    add_entries(notes, 2)
    out = togi("log", "notes")
    assert [r["seq"] for r in out["data"]] == [1, 2] and out["data"][0]["bundle"] == "notes"
    assert togi("log", "notes", "--verify")["data"]["ok"] is True
    ls = lines(notes)
    ls[0] = ls[0].replace('"r0"', '"evil"')
    (notes / ".togi" / "audit.jsonl").write_text("\n".join(ls) + "\n")
    code, out = togi_code("log", "notes", "--verify")
    assert code == 1 and out["error"]["code"] == "audit_chain_broken" and "seq 1" in out["error"]["message"]
    assert out["data"]["ok"] is False
    code, out = togi_code("doctor", "notes")
    assert code == 1 and out["error"]["code"] == "doctor_failed"


def test_concurrent_appends_keep_the_chain(notes, home):
    import threading

    ts = [threading.Thread(target=add_entries, args=(notes, 5)) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    v = audit.verify(notes)
    assert v["ok"] and v["entries"] == 20


# ----------------------------------------------------------------------------- locked hashes


def test_lock_records_hashes_and_logs(notes, home):
    code, shown = human(home, ["lock", "notes"], "notes")
    assert code == 0, shown
    out = togi("log", "notes")["data"]
    assert out[0]["action"] == "lock" and out[0]["decided_by"].startswith("human:")
    assert (home / "audit.key").is_file() and ((home / "audit.key").stat().st_mode & 0o777) == 0o600
    text = (notes / ".togi" / "locked.sha256").read_text()
    assert "  src/notes/store.py\n" in text and "  togi.toml\n" in text and "stone/tasks/add-and-find.toml" in text
    assert ".togi" not in text and "src/notes/format.py" not in text  # reshape files are not locked
    man = load_manifest(notes)
    assert audit.verify_locked(notes, man)["ok"] is True
    assert audit.read(notes)[0]["action"] == "lock" and audit.verify(notes, "notes")["ok"]


def test_locked_change_missing_and_new_file_detected(notes, home):
    lock_bundle()
    man = load_manifest(notes)
    (notes / "src/notes/store.py").write_text((notes / "src/notes/store.py").read_text() + "\n# evil\n")
    v = audit.verify_locked(notes, man)
    assert not v["ok"] and v["changed"] == ["src/notes/store.py"]
    lock_bundle()
    (notes / "stone/tasks/missing-note.toml").unlink()
    assert audit.verify_locked(notes, man)["missing"] == ["stone/tasks/missing-note.toml"]
    lock_bundle()
    (notes / "stone/tasks/extra.toml").write_text("id='x'\ninstruction='y'\n")
    assert audit.verify_locked(notes, man)["unlisted"] == ["stone/tasks/extra.toml"]


def test_doctor_reports_lock_state(notes, home):
    out = togi("doctor", "notes")
    row = next(c for c in out["data"]["checks"] if c["check"] == "notes: locked hashes")
    assert row["ok"] is False and row["level"] == "warn" and row["next"] == "togi lock notes"
    lock_bundle()
    row = next(c for c in togi("doctor", "notes")["data"]["checks"] if c["check"] == "notes: locked hashes")
    assert row["ok"] is True
    (notes / "togi.toml").write_text((notes / "togi.toml").read_text() + "\n# edit\n")
    code, out = togi_code("doctor", "notes")
    assert code == 1 and any(c["check"] == "notes: locked hashes" and not c["ok"] for c in out["data"]["checks"])


def test_lock_refused_for_agents(notes, home):
    r = run_togi(home, "lock", "notes", env_extra={"TOGI_AGENT": "claude"})
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "human_only"
    assert not (notes / ".togi" / "locked.sha256").exists()


# ----------------------------------------------------------------------------- item 3: the log cannot be laundered


def launder(repo, drop_seq=2, edit="EDITED: nothing to see here"):
    """The refuter's launder.py: drop an entry, edit the last, re-chain with plain sha256 (no key needed)."""
    p = repo / ".togi" / "audit.jsonl"
    es = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    es = [e for e in es if e["seq"] != drop_seq]
    es[-1]["reason"] = edit
    prev = "0" * 64
    for i, e in enumerate(es, 1):
        e["seq"], e["prev_hash"] = i, prev
        e["hash"] = audit.entry_hash(e)
        prev = e["hash"]
    p.write_text("".join(json.dumps(e, sort_keys=True, separators=(",", ":")) + "\n" for e in es))


def test_laundered_log_with_head_deleted_no_longer_verifies(notes, home):
    add_entries(notes, 4)
    (home / "state" / "notes" / "audit.head").unlink()
    launder(notes)
    v = audit.verify(notes, "notes")
    assert not v["ok"]
    kinds = {p["kind"] for p in v["problems"]}
    assert "mac" in kinds and "head" in kinds          # the re-sealed entries have no valid mac; the head is gone
    assert v["head"] == "missing"


def test_laundered_log_is_caught_by_the_mac_even_if_the_head_is_untouched(notes, home):
    add_entries(notes, 4)
    launder(notes, drop_seq=2)
    v = audit.verify(notes)                      # file only: no head available
    assert not v["ok"] and all(p["kind"] in ("mac", "chain") for p in v["problems"]) and v["problems"]


def test_a_missing_head_is_a_failure_not_a_skip(notes, home):
    add_entries(notes, 2)
    (home / "state" / "notes" / "audit.head").unlink()
    v = audit.verify(notes, "notes")
    assert not v["ok"] and v["head"] == "missing" and "re-anchors" in v["problems"][0]["problem"]


def test_an_unreadable_head_is_a_failure(notes, home):
    add_entries(notes, 2)
    (home / "state" / "notes" / "audit.head").write_text("{ not json")
    v = audit.verify(notes, "notes")
    assert not v["ok"] and v["head"] == "unreadable"


def test_a_forged_head_is_a_failure(notes, home):
    add_entries(notes, 2)
    hp = home / "state" / "notes" / "audit.head"
    head = json.loads(hp.read_text())
    head["mac"] = "0" * 64
    hp.write_text(json.dumps(head))
    assert audit.verify(notes, "notes")["head"] == "forged"


def test_append_verifies_the_whole_chain_and_writes_nothing_when_broken(notes, home):
    add_entries(notes, 3)
    p = notes / ".togi" / "audit.jsonl"
    ls = p.read_text().splitlines()
    e = json.loads(ls[0])
    e["reason"] = "tampered"
    ls[0] = json.dumps(e, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(ls) + "\n")
    before, head_before = p.read_bytes(), (home / "state" / "notes" / "audit.head").read_bytes()
    with pytest.raises(Exception) as ex:
        add_entries(notes, 1)
    assert ex.value.code == "audit_chain_broken" and ex.value.status == "refused" and "Nothing was written" in ex.value.message
    assert p.read_bytes() == before and (home / "state" / "notes" / "audit.head").read_bytes() == head_before


@pytest.mark.parametrize("damage", ["delete_head", "truncate_tail", "garbage_line", "strip_mac", "forged_entry"])
def test_append_refuses_every_kind_of_damage(notes, home, damage):
    add_entries(notes, 3)
    p = notes / ".togi" / "audit.jsonl"
    ls = p.read_text().splitlines()
    if damage == "delete_head":
        (home / "state" / "notes" / "audit.head").unlink()
    elif damage == "truncate_tail":
        p.write_text("\n".join(ls[:2]) + "\n")
    elif damage == "garbage_line":
        p.write_text("\n".join(ls) + "\nnot json\n")
    elif damage == "strip_mac":
        e = json.loads(ls[2])
        del e["mac"]
        ls[2] = json.dumps(e, sort_keys=True, separators=(",", ":"))
        p.write_text("\n".join(ls) + "\n")
    elif damage == "forged_entry":   # a well-formed, correctly hashed 4th entry but no key to sign it
        last = json.loads(ls[2])
        e = {**{k: last[k] for k in ("action", "proposal", "tier", "files", "diff_sha256", "stone_before",
                                     "stone_after", "decided_by", "reason")},
             "seq": 4, "ts": last["ts"], "prev_hash": last["hash"], "kid": last["kid"]}
        e["hash"] = audit.entry_hash(e)
        e["mac"] = "0" * 64
        p.write_text("\n".join(ls) + "\n" + json.dumps(e, sort_keys=True, separators=(",", ":")) + "\n")
    before = p.read_bytes()
    with pytest.raises(Exception) as ex:
        add_entries(notes, 1)
    assert ex.value.code == "audit_chain_broken"
    assert p.read_bytes() == before


def test_audit_lines_are_ascii_and_unicode_separators_cannot_split_a_line(notes, home):
    audit.append(notes, "notes", action="reject", proposal="p_1", tier="locked", files=["stone/a b.toml", "x\u0085y"],
                 diff_sha256="ab" * 32, stone_before=None, stone_after=None, decided_by="policy",
                 reason="line sep \x0b vt \x0c ff \x1c fs é")
    raw = (notes / ".togi" / "audit.jsonl").read_bytes()
    assert raw.isascii() and raw.count(b"\n") == 1
    e = audit.read(notes)[0]
    assert e["files"] == ["stone/a b.toml", "x\u0085y"] and " " in e["reason"]
    assert audit.verify(notes, "notes")["ok"]


def test_reading_splits_on_newline_only(notes, home):
    """A legacy/injected line carrying a RAW U+2028 inside a string must stay one line (str.splitlines splits it)."""
    add_entries(notes, 1)
    p = notes / ".togi" / "audit.jsonl"
    e = audit.read(notes)[0]
    e["reason"] = "has a\x0craw\x85separator"
    e["hash"] = audit.entry_hash(e)
    p.write_text(json.dumps(e, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
    assert len(audit.read(notes)) == 1 and audit.read(notes)[0]["reason"] == "has a\x0craw\x85separator"
    assert "_corrupt" not in audit.read(notes)[0]


def test_key_is_created_0600_and_a_damaged_key_is_an_error_not_a_new_key(notes, home):
    assert not (home / "audit.key").exists()
    add_entries(notes, 1)
    k = home / "audit.key"
    assert k.is_file() and (k.stat().st_mode & 0o777) == 0o600
    k.write_text("zz not hex")
    with pytest.raises(Exception) as ex:
        add_entries(notes, 1)
    assert ex.value.code == "audit_key_unreadable"
    assert k.read_text() == "zz not hex"


def test_legacy_unsigned_log_and_head_still_verify_and_a_persons_lock_signs_the_next_entry(notes, home):
    """A log written before signatures existed (e.g. Crowd's real one) keeps verifying, but an ordinary writer can no
    longer append to it (L1: nothing proves an all-unsigned log was not rewritten); a person's `togi lock` can, and from
    then on new entries are signed."""
    p = notes / ".togi" / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    prev, ls = "0" * 64, []
    for i in range(1, 4):
        e = {"seq": i, "ts": "2026-10-26T20:00:00.000Z", "action": "lock", "proposal": None, "tier": "locked",
             "files": [], "diff_sha256": None, "stone_before": None, "stone_after": None, "decided_by": "human:x",
             "reason": f"legacy {i}", "prev_hash": prev}
        e["hash"] = audit.entry_hash(e)
        prev = e["hash"]
        ls.append(json.dumps(e, sort_keys=True, separators=(",", ":")))
    p.write_text("\n".join(ls) + "\n")
    hp = home / "state" / "notes" / "audit.head"
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps({"seq": 3, "hash": prev}))                 # the old head format: no mac
    v = audit.verify(notes, "notes")
    assert v["ok"] and v["authenticated"] is False and v["unauthenticated_entries"] == 3
    with pytest.raises(Exception) as ex:
        add_entries(notes, 1)                                          # an ordinary writer: refused, nothing written
    assert ex.value.code == "audit_unauthenticated" and len(audit.read(notes)) == 3
    audit.append(notes, "notes", action="lock", proposal=None, tier="locked", files=[], diff_sha256=None,
                 stone_before=None, stone_after=None, decided_by="human:x", reason="re-anchor", anchor=True)
    es = audit.read(notes)
    assert "mac" not in es[0] and "mac" in es[3]
    v = audit.verify(notes, "notes")
    assert v["ok"] and v["entries"] == 4 and json.loads(hp.read_text())["mac"]
    # once signed entries exist, unsigned ones may not follow them
    ls2 = p.read_text().splitlines()
    e = json.loads(ls2[3])
    del e["mac"], e["kid"]
    e["hash"] = audit.entry_hash(e)
    ls2[3] = json.dumps(e, sort_keys=True, separators=(",", ":"))
    p.write_text("\n".join(ls2) + "\n")
    v = audit.verify(notes, "notes")
    assert not v["ok"] and v["problems"]


def test_a_person_can_re_anchor_a_log_whose_head_or_key_was_lost(notes, home):
    add_entries(notes, 3)
    (home / "state" / "notes" / "audit.head").unlink()
    (home / "audit.key").unlink()
    assert not audit.verify(notes, "notes")["ok"]
    with pytest.raises(Exception) as ex:                       # ordinary writers still refuse
        add_entries(notes, 1)
    assert ex.value.code == "audit_chain_broken"
    code, shown = human_relock(home, ["lock", "notes"], "notes")   # `togi lock` re-anchors (r5: with the extra word "relock")
    assert code == 0, shown
    v = audit.verify(notes, "notes")
    assert v["ok"] and v["entries"] == 4 and v["head"] == "ok"
    assert v["unauthenticated_entries"] == 3                   # the old entries stay chain-bound, not key-bound
    add_entries(notes, 1)
    assert audit.verify(notes, "notes")["ok"]


def test_re_anchoring_does_not_bless_a_tampered_chain(notes, home):
    add_entries(notes, 3)
    p = notes / ".togi" / "audit.jsonl"
    ls = p.read_text().splitlines()
    ls[1] = ls[1].replace('"r1"', '"evil"')
    p.write_text("\n".join(ls) + "\n")
    (home / "state" / "notes" / "audit.head").unlink()
    code, shown = human(home, ["lock", "notes"], "notes")
    assert code == 2 and "audit_chain_broken" in shown


def test_chain_only_verify_needs_nothing_from_togi_home_and_says_what_it_does_not_check(notes, home):
    add_entries(notes, 3)
    v = audit.verify(notes, "notes", chain_only=True)
    assert v["ok"] and v["authenticated"] is False and "NOT checked" in v["note"] and v["head"] == "skipped"
    # honest limit: a re-chained forgery has a consistent hash chain, so only the full verify (macs + head) sees it
    launder(notes)
    assert audit.verify(notes, "notes", chain_only=True)["ok"] is True
    assert audit.verify(notes, "notes")["ok"] is False
    out = togi("log", "notes", "--verify", "--chain-only")
    assert out["data"]["head"] == "skipped"


def test_lock_and_revert_and_apply_refuse_an_empty_togi_agent(notes, home):
    for argv in (["lock", "notes"], ["revert", "p_1"], ["apply", "p_1"]):
        r = run_togi(home, *argv, env_extra={"TOGI_AGENT": ""})
        assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "human_only", argv


def test_lock_needs_the_passphrase(notes, home):
    code, shown = human(home, ["lock", "notes"], "notes", passphrase="not the passphrase!!")
    assert code == 2 and "wrong_passphrase" in shown
    assert not (notes / ".togi" / "locked.sha256").exists() and not (home / "audit.key").exists()
    code, shown = human(home, ["lock", "notes"], "wrong word")
    assert code == 2 and "confirmation_mismatch" in shown
    r = run_togi(home, "lock", "notes", input="notes\n")
    assert r.returncode == 2 and json.loads(r.stdout)["error"]["code"] == "needs_tty"


def test_a_hand_rewritten_lock_file_is_not_trusted(notes, home):
    """Round-3 limit 1: edit a locked file, then rewrite .togi/locked.sha256 by hand with the new hash and no
    approver. The lock file no longer matches the signed `lock` entry, so the locked tier still holds."""
    lock_bundle()
    man = load_manifest(notes)
    assert audit.verify_locked(notes, man)["anchored"] is True
    store = notes / "src/notes/store.py"
    store.write_text(store.read_text() + "\n# evil\n")
    lp = notes / ".togi" / "locked.sha256"
    lines = [(f"{audit.file_sha256(store)}  src/notes/store.py" if l.endswith("  src/notes/store.py") else l)
             for l in lp.read_text().splitlines()]
    lp.write_text("\n".join(lines) + "\n")
    v = audit.verify_locked(notes, man)
    assert v["changed"] == [] and v["anchored"] is False and v["ok"] is False   # hashes "match", anchor doesn't
    assert "differs" in v["problem"]


def test_a_lock_file_without_any_lock_entry_is_not_trusted(notes, home):
    man = load_manifest(notes)
    audit.write_lock(notes, man)                     # written directly, never logged
    v = audit.verify_locked(notes, man)
    assert v["ok"] is False and v["anchored"] is False and "not recorded" in v["problem"]
