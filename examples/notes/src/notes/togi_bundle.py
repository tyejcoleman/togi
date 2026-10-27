"""The notes bundle: the smallest useful example of the Togi contract."""
from __future__ import annotations

from decimal import Decimal
from typing import Literal, Optional

from togi import Bundle, Context, TogiError

from .format import summarize
from .messages import message
from .store import Store

bundle = Bundle("notes")


def _err(key: str, code: str | None = None, **fmt) -> TogiError:
    msg, nxt = message(key, **fmt)
    return TogiError(code or key, msg, next=nxt)


@bundle.command("add", effect="write", examples=['togi notes add --text "buy milk" --tag home'])
def add(text: str, tag: Optional[list[str]] = None, ctx: Context = None) -> dict:
    """Add a note. Repeat --tag to attach tags."""
    if not text.strip():
        raise _err("empty_text")
    note = Store(ctx.state_dir).add(text.strip(), tag or [])
    ctx.journal_note({"added": True})
    return note


@bundle.command("list", effect="read", examples=["togi notes list --tag home --sort oldest"])
def list_notes(tag: Optional[str] = None, sort: Literal["newest", "oldest"] = "newest", ctx: Context = None) -> list:
    """List notes, newest first (or --sort oldest)."""
    notes = Store(ctx.state_dir).notes()
    if tag:
        notes = [n for n in notes if tag in n.get("tags", [])]
    if sort == "newest":
        notes = list(reversed(notes))
    return [summarize(n) for n in notes]


@bundle.command("show", effect="read", examples=["togi notes show --id n_1"])
def show(id: str, ctx: Context = None) -> dict:
    """Show one note in full."""
    note = Store(ctx.state_dir).get(id)
    if not note:
        raise _err("note_not_found", id=id)
    return note


@bundle.command("find", effect="read", examples=["togi notes find --query milk"])
def find(query: str, ctx: Context = None) -> list:
    """Find notes whose text contains the query."""
    q = query.lower()
    return [summarize(n) for n in Store(ctx.state_dir).notes() if q in n["text"].lower()]


@bundle.command("delete", effect="admin", examples=["togi notes delete --id n_1 --dry-run"])
def delete(id: str, ctx: Context = None) -> dict:
    """Delete a note for good."""
    if not Store(ctx.state_dir).delete(id):
        raise _err("note_not_found", id=id)
    return {"deleted": id}


@bundle.command("tip send", effect="money", confirm="id", examples=["togi notes tip send --id n_1 --amount 5.00"])
def tip_send(id: str, amount: Decimal, ctx: Context = None) -> dict:
    """Send a tip to a note's author. Moves money: needs a person's approval."""
    store = Store(ctx.state_dir)
    if not store.get(id):
        raise _err("note_not_found", id=id)
    if amount <= 0:
        raise _err("bad_amount")
    return store.tip(id, amount)


@bundle.command("tip total", effect="read", examples=["togi notes tip total"])
def tip_total(ctx: Context = None) -> dict:
    """Total of all tips sent so far."""
    tips = Store(ctx.state_dir).tips()
    return {"count": len(tips), "total": sum((Decimal(t["amount"]) for t in tips), Decimal("0"))}


@bundle.command("seed", effect="write", examples=["togi notes seed --count 120"])
def seed(count: int = 100, ctx: Context = None) -> dict:
    """Create many filler notes (used to try paging and output limits)."""
    store = Store(ctx.state_dir)
    for i in range(count):
        store.add(f"filler note number {i + 1} " + "lorem ipsum " * 8, ["seed"])
    return {"created": count}


@bundle.command("test reset", effect="admin", examples=["togi notes test reset"])
def test_reset(ctx: Context = None) -> dict:
    """Wipe all notes and tips (test setup)."""
    Store(ctx.state_dir).reset()
    return {"reset": True}
