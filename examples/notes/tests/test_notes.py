"""Deterministic tests for the notes example bundle (the 'stone' tests).

Run with: PYTHONPATH=src:../../src python3 -m pytest tests
"""
import pytest
from decimal import Decimal

from togi import Context, TogiError
from notes.togi_bundle import add, delete, find, list_notes, show, tip_send, tip_total


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTES_TEST_MODE", "1")   # the bundle's own test mode (the stone sets it from [stone].env)
    return Context(bundle="notes", state_dir=tmp_path)


def test_add_and_show(ctx):
    n = add(text="buy milk", tag=["home"], ctx=ctx)
    assert n["id"] == "n_1"
    assert show(id="n_1", ctx=ctx)["text"] == "buy milk"


def test_list_newest_first(ctx):
    add(text="a", ctx=ctx)
    add(text="b", ctx=ctx)
    assert [n["text"] for n in list_notes(ctx=ctx)] == ["b", "a"]
    assert [n["text"] for n in list_notes(sort="oldest", ctx=ctx)] == ["a", "b"]


def test_find(ctx):
    add(text="Buy Milk", ctx=ctx)
    add(text="walk dog", ctx=ctx)
    assert [n["text"] for n in find(query="milk", ctx=ctx)] == ["Buy Milk"]


def test_missing_note_error_has_next(ctx):
    with pytest.raises(TogiError) as e:
        show(id="n_404", ctx=ctx)
    assert e.value.code == "note_not_found"
    assert e.value.next


def test_delete(ctx):
    add(text="x", ctx=ctx)
    assert delete(id="n_1", ctx=ctx) == {"deleted": "n_1"}
    with pytest.raises(TogiError):
        delete(id="n_1", ctx=ctx)


def test_tips_sum(ctx):
    add(text="x", ctx=ctx)
    tip_send(id="n_1", amount=Decimal("5.00"), ctx=ctx)
    tip_send(id="n_1", amount=Decimal("2.50"), ctx=ctx)
    assert tip_total(ctx=ctx)["total"] == Decimal("7.50")


def test_tip_rejects_nonpositive(ctx):
    add(text="x", ctx=ctx)
    with pytest.raises(TogiError) as e:
        tip_send(id="n_1", amount=Decimal("0"), ctx=ctx)
    assert e.value.code == "bad_amount"


def test_summary_shape_and_snippet():
    from notes.format import SNIPPET, summarize

    long = {"id": "n_1", "text": "x" * 200, "tags": ["a"]}
    s = summarize(long)
    assert set(s) == {"id", "text", "tags"} and len(s["text"]) == SNIPPET and s["text"].endswith("…")
    assert summarize({"id": "n_2", "text": "short"})["text"] == "short"


def test_tip_refuses_a_test_scope_approval_outside_test_mode(ctx, monkeypatch):
    """SPEC section 2: a bundle that moves value refuses an approval from a throwaway approver outside its test mode."""
    add(text="x", ctx=ctx)
    monkeypatch.delenv("NOTES_TEST_MODE")
    for scope in ("test", None):
        ctx.approval_scope = scope
        with pytest.raises(TogiError) as e:
            tip_send(id="n_1", amount=Decimal("1.00"), ctx=ctx)
        assert e.value.code == "test_scope_refused" and e.value.status == "refused"
    assert tip_total(ctx=ctx)["count"] == 0
    ctx.approval_scope = "default"                       # the real approver's grant is fine
    assert tip_send(id="n_1", amount=Decimal("1.00"), ctx=ctx)["amount"] == "1.00"
