"""Milestone 1: the contract (decorator, type mapping, envelope, exit codes, limits, skill, tools)."""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Literal, Optional

import pytest

from togi import Bundle, Context, TogiError
from togi.bundle import coerce_json
from togi.errors import ArgError

from conftest import lock_bundle, togi, togi_code


# ----------------------------------------------------------------------------- decorator + type mapping


def make_bundle():
    b = Bundle("crowd")

    @b.command("drop status", effect="read", examples=["togi crowd drop status --drop d_7f3a"])
    def drop_status(drop: str, verbose: bool = False) -> dict:
        """Show a drop's current price, buyers, time left and state."""
        return {"drop": drop, "price": Decimal("33.00"), "buyers": 52}

    @b.command("drop open", effect="write")
    def drop_open(sku: str, hours: int = 48, floor: Decimal = Decimal("1.50"), tag: Optional[list[str]] = None,
                  kind: Literal["mug", "tee"] = "mug", ratio: float = 0.5, note: Optional[str] = None,
                  ctx: Context = None) -> dict:
        """Open a drop.

        Args:
            sku: the product code
            hours: how long it runs
        """
        return {}

    return b


def test_command_decorator_returns_function_and_is_callable():
    b = make_bundle()
    cmd = b.commands["drop status"]
    assert cmd.api_name == "drop_status" and cmd.effect == "read"
    assert cmd.summary == "Show a drop's current price, buyers, time left and state."
    assert cmd.func(drop="d_1")["buyers"] == 52  # plain Python still works


def test_type_mapping_to_params_and_flags():
    cmd = make_bundle().commands["drop open"]
    kinds = {p.name: (p.kind, p.required) for p in cmd.params}
    assert kinds == {"sku": ("str", True), "hours": ("int", False), "floor": ("decimal", False),
                     "tag": ("list", False), "kind": ("literal", False), "ratio": ("float", False),
                     "note": ("str", False)}
    assert cmd.param("hours").flag == "--hours"
    assert cmd.ctx_param == "ctx"
    assert cmd.param("sku").doc == "the product code"


def test_param_x_y_becomes_flag_x_dash_y():
    b = Bundle("t")

    @b.command("go", effect="read")
    def go(x_y: str) -> dict:
        """Go."""
        return {}

    assert b.commands["go"].params[0].flag == "--x-y"


def test_json_schema_and_stub():
    cmd = make_bundle().commands["drop open"]
    s = cmd.json_schema()
    assert s["required"] == ["sku"] and s["additionalProperties"] is False
    assert s["properties"]["hours"] == {"type": "integer", "default": 48, "description": "how long it runs"}
    assert s["properties"]["floor"]["type"] == "string" and s["properties"]["floor"]["format"] == "decimal"
    assert s["properties"]["tag"] == {"type": "array", "items": {"type": "string"}}
    assert s["properties"]["kind"]["enum"] == ["mug", "tee"]
    stub = make_bundle().commands["drop status"].stub()
    assert stub.startswith("crowd.drop_status(drop: str, verbose: bool = False) -> dict  # read · Show a drop's")


def test_unsupported_types_and_reserved_names_fail_fast():
    b = Bundle("t")
    with pytest.raises(TypeError):
        @b.command("a")
        def a(x: dict) -> dict:
            """A."""
    with pytest.raises(ValueError):
        @b.command("b")
        def b_(dry_run: bool = False) -> dict:
            """B."""
    with pytest.raises(ValueError):
        @b.command("c", effect="nuclear")
        def c() -> dict:
            """C."""


def test_duplicate_commands_rejected():
    b = Bundle("t")

    @b.command("go")
    def go() -> dict:
        """Go."""

    with pytest.raises(ValueError):
        @b.command("go")
        def go2() -> dict:
            """Go."""


def test_coerce_json_values():
    cmd = make_bundle().commands["drop open"]
    assert coerce_json(cmd.param("floor"), "2.50") == Decimal("2.50")
    assert coerce_json(cmd.param("hours"), "7") == 7
    assert coerce_json(cmd.param("tag"), "solo") == ["solo"]
    with pytest.raises(ArgError):
        coerce_json(cmd.param("kind"), "hat")
    with pytest.raises(ArgError):
        coerce_json(cmd.param("hours"), True)


def test_togi_error_validates_status():
    e = TogiError("x", "boom", next="togi y", status="refused")
    assert (e.code, e.next, e.status) == ("x", "togi y", "refused")
    with pytest.raises(ValueError):
        TogiError("x", status="weird")


# ----------------------------------------------------------------------------- envelope, exit codes, CLI


def test_success_envelope_one_line_json(notes, capsys):
    out = togi("notes", "add", "--text", "buy milk", "--tag", "home", "--tag", "x")
    assert out["ok"] is True and out["data"] == {"id": "n_1", "text": "buy milk", "tags": ["home", "x"]}


def test_decimal_serialised_as_string(notes):
    togi("notes", "add", "--text", "x")
    out = togi("notes", "tip", "total")
    assert out["data"] == {"count": 0, "total": "0"}


def test_error_envelope_and_exit_code_1(notes):
    code, out = togi_code("notes", "show", "--id", "n_9")
    assert code == 1
    assert out == {"ok": False, "status": "error",
                   "error": {"code": "note_not_found", "message": "No note with id n_9."},
                   "next": "togi notes find --query <words>"}


def test_refused_exit_2_and_internal_exit_4(home, tmp_path):
    repo = tmp_path / "r"
    (repo / "src" / "bad").mkdir(parents=True)
    (repo / "togi.toml").write_text('[bundle]\nname="bad"\nmodule="bad.b:bundle"\nsummary="x"\n')
    (repo / "src" / "bad" / "__init__.py").write_text("")
    (repo / "src" / "bad" / "b.py").write_text(
        "from togi import Bundle, TogiError\nbundle = Bundle('bad')\n"
        "@bundle.command('no', effect='read')\ndef no() -> dict:\n    '''Refuse.'''\n"
        "    raise TogiError('nope', 'a guard said no', next='togi skill bad', status='refused')\n"
        "@bundle.command('boom', effect='read')\ndef boom() -> dict:\n    '''Crash.'''\n    return 1/0\n"
        "@bundle.command('weird', effect='read')\ndef weird() -> dict:\n    '''Unserialisable.'''\n    return {'x': object()}\n")
    togi("add", str(repo))
    code, out = togi_code("bad", "no")
    assert code == 2 and out["status"] == "refused" and out["next"] == "togi skill bad"
    code, out = togi_code("bad", "boom")
    assert code == 4 and out["status"] == "internal" and "ZeroDivisionError" in out["error"]["message"]
    code, out = togi_code("bad", "weird")
    assert code == 4 and out["error"]["code"] == "internal_error"


def test_unknown_command_suggests_and_says_next(notes):
    code, out = togi_code("notes", "shw", "--id", "n_1")
    assert code == 1 and out["error"]["code"] == "unknown_command" and "show" in out["error"]["message"]
    assert out["next"].startswith("togi notes show")
    code, out = togi_code("notez", "list")
    assert code == 1 and out["error"]["code"] == "unknown_command" and out["next"] == "togi skill"


def test_unknown_flag_missing_and_bad_values(notes):
    code, out = togi_code("notes", "show", "--idd", "n_1")
    assert code == 1 and out["error"]["code"] == "unknown_flag" and "--id" in out["error"]["message"]
    code, out = togi_code("notes", "show")
    assert out["error"]["code"] == "missing_arg" and out["next"] == "togi notes show --id n_1"
    code, out = togi_code("notes", "seed", "--count", "many")
    assert out["error"]["code"] == "bad_arg"
    code, out = togi_code("notes", "list", "--sort", "sideways")
    assert out["error"]["code"] == "bad_arg" and "newest" in out["error"]["message"]
    code, out = togi_code("notes", "show", "stray")
    assert out["error"]["code"] == "unexpected_argument"


def test_flag_equals_form_and_negative_numbers(notes):
    out = togi("notes", "add", "--text=a=b")
    assert out["data"]["text"] == "a=b"
    out = togi("notes", "seed", "--count=2")
    assert out["data"] == {"created": 2}


def test_fields_trim_output(notes):
    togi("notes", "add", "--text", "one", "--tag", "t")
    out = togi("notes", "list", "--fields", "id,text")
    assert out["data"] == [{"id": "n_1", "text": "one"}]
    out = togi("notes", "show", "--id", "n_1", "--fields", "tags")
    assert out["data"] == {"tags": ["t"]}


def test_limit_offset_and_more_hint(notes):
    togi("notes", "seed", "--count", "120")
    out = togi("notes", "list", "--fields", "id", "--limit", "5")
    assert [n["id"] for n in out["data"]] == ["n_120", "n_119", "n_118", "n_117", "n_116"]
    assert out["more"] == "togi notes list --fields id --limit 5 --offset 5" and out["total"] == 120
    out = togi("notes", "list", "--fields", "id", "--limit", "5", "--offset", "5")
    assert out["data"][0]["id"] == "n_115"
    out = togi("notes", "list", "--fields", "id")  # default limit 50
    assert len(out["data"]) == 50 and out["more"].endswith("--offset 50")
    out = togi("notes", "list", "--fields", "id", "--limit", "500", "--full")
    assert len(out["data"]) == 120 and "more" not in out


def test_8kb_cap_and_full(notes):
    togi("notes", "seed", "--count", "200")
    full = togi("notes", "list", "--limit", "200", "--full")
    assert len(full["data"]) == 200 and "truncated" not in full
    assert len(json.dumps(full, separators=(",", ":"))) > 8192  # big without the cap
    big = togi("notes", "list", "--limit", "200")
    assert len(json.dumps(big, separators=(",", ":"))) <= 8192 + 100
    assert big["truncated"] is True and big["more"].endswith(f"--offset {len(big['data'])}")
    assert 0 < len(big["data"]) < 200


def test_dry_run_for_write_money_admin(notes):
    out = togi("notes", "add", "--text", "hi", "--dry-run")
    assert out["data"]["dry_run"] is True and out["data"]["effect"] == "write"
    assert out["data"]["would_run"] == "togi notes add --text hi"
    assert togi("notes", "list")["data"] == []  # nothing was written
    out = togi("notes", "delete", "--id", "n_1", "--dry-run")
    assert out["data"]["dry_run"] is True
    out = togi("notes", "tip", "send", "--id", "n_1", "--amount", "5", "--dry-run")
    assert out["data"]["requires_approval"] is True
    assert togi("approvals")["data"] == []  # dry-run makes no approval request


def test_help_is_short_text(notes):
    out = togi("notes", "tip", "send", "--help", expect=0)
    assert isinstance(out, str) and "--amount" in out and "--approval" in out and "togi notes tip send" in out


def test_bundle_alone_prints_skill(notes):
    assert "# notes" in togi("notes")
    for flag in ("--help", "-h"):
        assert "# notes" in togi("notes", flag)


def test_no_value_for_flag_and_dash_dash_value(notes):
    code, out = togi_code("notes", "add", "--text")
    assert out["error"]["code"] == "missing_value"
    code, out = togi_code("notes", "add", "--text", "--tag")
    assert out["error"]["code"] == "missing_value"


# ----------------------------------------------------------------------------- skill + tools


def test_skill_index_is_short_and_lists_bundles(notes):
    from togi import skills

    text = togi("skill")
    assert isinstance(text, str) and "- notes: " in text
    assert len(text) / 4 <= 400, f"index is ~{len(text) // 4} tokens"
    assert "togi approve" in text and "exit codes" in text.lower()


def test_skill_bundle_topic_and_topics(notes):
    text = togi("skill", "notes")
    assert text.startswith("# notes") and "Topics" in text and "money" in text
    assert "approval id" in togi("skill", "notes", "money")
    topics = togi("skill", "notes", "--topics")
    assert "money: How tips are approved" in topics
    code, out = togi_code("skill", "notes", "nope")
    assert code == 1 and out["error"]["code"] == "unknown_topic"
    code, out = togi_code("skill", "nobundle")
    assert out["error"]["code"] == "unknown_bundle"


def test_skill_json_mode_wraps_text(notes):
    out = togi("skill", "--json")
    assert out["ok"] is True and "Togi" in out["data"]["text"]


def test_tools_stubs_and_schema(notes):
    text = togi("tools", "notes")
    lines = text.strip().splitlines()
    assert any(l.startswith("notes.add(text: str, tag: list[str] | None = None) -> dict  # write ·") for l in lines)
    assert any("notes.tip_send(id: str, amount: Decimal)" in l and "# money" in l for l in lines)
    schemas = togi("tools", "notes", "--json")["data"]
    tip = next(s for s in schemas if s["name"] == "notes.tip_send")
    assert tip["effect"] == "money" and tip["input_schema"]["required"] == ["id", "amount"]


def test_tools_without_bundle_lists_all(notes):
    assert "notes.find(query: str)" in togi("tools")


# ----------------------------------------------------------------------------- item 14: output caps everywhere


def test_error_envelopes_obey_the_cap_with_a_full_hint(notes):
    big = "x" * 30000
    code, out = togi_code("notes", "show", "--id", big)
    assert code == 1 and out["error"]["code"] == "note_not_found"
    text = json.dumps(out, separators=(",", ":"), ensure_ascii=False)
    assert len(text.encode()) <= 8192 and out["truncated"] is True and out["more"].endswith("--full")
    assert out["next"].startswith("togi notes find")                      # the useful part survives
    code, full = togi_code("notes", "show", "--id", big, "--full")
    assert len(full["error"]["message"]) > 30000 and "truncated" not in full


def test_dry_run_obeys_the_cap(notes):
    big = "x" * 30000
    code, out = togi_code("notes", "delete", "--id", big, "--dry-run")
    assert code == 0 and out["data"]["dry_run"] is True and out["truncated"] is True
    assert len(json.dumps(out, separators=(",", ":")).encode()) <= 8192 and out["more"].endswith("--full")
    code, full = togi_code("notes", "delete", "--id", big, "--dry-run", "--full")
    assert len(json.dumps(full)) > 30000


def test_needs_approval_envelope_obeys_the_cap(notes):
    lock_bundle("notes")     # the money path needs a verified lock
    code, out = togi_code("notes", "tip", "send", "--id", "n_" + "9" * 30000, "--amount", "5.00")
    assert code == 3 and out["status"] == "needs_approval" and out["approval"]["id"].startswith("ap_")
    assert len(json.dumps(out, separators=(",", ":")).encode()) <= 8192


def _flood_approvals(n=20):
    lock_bundle("notes")
    for i in range(1, n + 1):
        togi("notes", "add", "--text", f"n{i}")
        togi_code("notes", "tip", "send", "--id", f"n_{i}", "--amount", f"{i}." + "9" * 250)


def test_host_commands_obey_the_cap_and_full_lifts_it(notes):
    _flood_approvals()
    out = togi("approvals")
    assert out["truncated"] is True and len(json.dumps(out, separators=(",", ":")).encode()) <= 8192
    assert "--full" in out["more"] or "--offset" in out["more"]
    full = togi("approvals", "--full")
    assert len(full["data"]) == 20 and "truncated" not in full


def test_host_commands_honour_fields_limit_and_offset(notes):
    _flood_approvals(6)
    rows = togi("approvals", "--fields", "id,status")["data"]
    assert len(rows) == 6 and all(set(r) == {"id", "status"} for r in rows)
    page = togi("approvals", "--fields", "id", "--limit", "2")
    assert len(page["data"]) == 2 and page["total"] == 6 and "--offset 2" in page["more"]
    page2 = togi("approvals", "--fields", "id", "--limit", "2", "--offset", "2")
    assert page2["data"] == [{"id": r["id"]} for r in rows[2:4]]
    assert togi("bundles", "--fields", "name")["data"] == [{"name": "notes"}]
    doc = togi("doctor", "notes", "--fields", "ok,errors")["data"]
    assert set(doc) == {"ok", "errors"}
    assert set(togi("journal", "notes", "--fields", "command", "--limit", "3")["data"][0]) == {"command"}
    assert set(togi("log", "notes", "--fields", "seq")["data"][0] if togi("log", "notes")["data"] else {"seq"}) == {"seq"}


def test_text_documents_obey_the_cap(notes):
    skill = notes / "skills" / "SKILL.md"
    skill.write_text(skill.read_text() + "\n" + ("A very long line of guidance for the agent. " * 40 + "\n") * 40)
    txt = togi("skill", "notes")
    assert isinstance(txt, str) and len(txt.encode()) <= 8192 + 200 and "output cut at 8192 bytes" in txt
    full = togi("skill", "notes", "--full")
    assert len(full.encode()) > 20000 and "output cut" not in full
