"""Output shaping: --fields projection, pagination, the size cap, text rendering, bool flag forms."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from togi import Bundle, output
from togi.cliargs import parse_command_args
from togi.errors import ArgError


def test_project_paths_and_lists():
    data = {"a": 1, "b": {"c": 2, "d": 3}, "rows": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]}
    assert output.project(data, ["a", "b.c"]) == {"a": 1, "b": {"c": 2}}
    assert output.project(data, ["rows.x"]) == {"rows": [{"x": 1}, {"x": 3}]}
    assert output.project([{"x": 1, "y": 2}], ["y"]) == [{"y": 2}]
    assert output.project(data, ["b", "b.c"]) == {"b": {"c": 2, "d": 3}}  # a shorter path keeps the subtree
    assert output.project(data, ["nope"]) == {}


def test_paginate_list_and_dict_lists():
    assert output.paginate(list(range(10)), 3, 0) == ([0, 1, 2], {"next_offset": 3, "total": 10})
    assert output.paginate(list(range(10)), 3, 9) == ([9], {"total": 10})
    d, info = output.paginate({"items": list(range(10)), "small": [1]}, 4, 4)
    assert d == {"items": [4, 5, 6, 7], "small": [1]} and info["next_offset"] == 8 and info["totals"] == {"items": 10}


def test_fit_list_dict_and_long_strings():
    env = output.ok_envelope([{"id": i, "pad": "x" * 100} for i in range(500)])
    out, cut = output.fit(env, 2000, lambda off: f"next {off}")
    assert cut and len(output.dumps(out)) <= 2000 and out["more"] == f"next {len(out['data'])}"
    env = output.ok_envelope({"rows": [{"id": i, "pad": "y" * 80} for i in range(300)], "meta": {"k": 1}})
    out, cut = output.fit(env, 3000, None)
    assert cut and len(output.dumps(out)) <= 3000 and out["data"]["meta"] == {"k": 1} and out["truncated"] is True
    env = output.ok_envelope({"blob": "z" * 50_000})
    out, cut = output.fit(env, 2000, None)
    assert cut and len(output.dumps(out)) <= 2000 and "chars]" in out["data"]["blob"]
    small = output.ok_envelope({"a": 1})
    assert output.fit(small, 2000, None) == (small, False)


def test_json_normalisation():
    import datetime as dt

    v = output.to_jsonable({"d": Decimal("1.50"), "t": dt.datetime(2026, 1, 2, 3, 4, 5), "s": {3, 1}, "f": float("nan")})
    assert v == {"d": "1.50", "t": "2026-01-02T03:04:05", "s": [1, 3], "f": "nan"}
    with pytest.raises(TypeError):
        output.to_jsonable({"x": object()})
    assert output.dumps({"a": [1, 2], "ü": "é"}) == '{"a":[1,2],"ü":"é"}'


def test_text_rendering():
    assert output.render_text(output.ok_envelope({"a": 1, "b": [1, 2]})) == "a: 1\nb:\n  - 1\n  - 2"
    assert output.render_text(output.ok_envelope([{"id": "n_1", "t": None}])) == "- id=n_1  t=-"
    err = output.error_envelope("error", "bad", "It broke.", next="togi skill")
    assert output.render_text(err) == "error: It broke.  [bad]\nnext: togi skill"


def make_cmd():
    b = Bundle("t")

    @b.command("go")
    def go(verbose: bool = False, color: bool = True, name: str = "x", n: int = 1, limit: int = 5, text: str = "") -> dict:
        """Go."""
        return {}

    return b.commands["go"]


def test_bool_flags_negation_and_shadowing():
    cmd = make_cmd()
    assert parse_command_args(cmd, ["--verbose"]).args == {"verbose": True}
    assert parse_command_args(cmd, ["--verbose=false"]).args == {"verbose": False}
    assert parse_command_args(cmd, ["--no-color"]).args == {"color": False}
    assert parse_command_args(cmd, ["--n", "-5"]).args == {"n": -5}          # negative numbers are values
    # the command's own `limit` and `text` shadow the host flags of the same name
    p = parse_command_args(cmd, ["--limit", "9", "--text", "hello"])
    assert p.args == {"limit": 9, "text": "hello"} and p.limit == 50 and p.text is False
    p = parse_command_args(cmd, ["--json", "--full", "--offset", "3", "--fields", "a,b"])
    assert p.json and p.full and p.offset == 3 and p.fields == ["a", "b"]
    with pytest.raises(ArgError) as e:
        parse_command_args(cmd, ["--no-verbose"])  # verbose defaults False: no negation flag
    assert e.value.code == "unknown_flag"
    with pytest.raises(ArgError):
        parse_command_args(cmd, ["--n", "--json"])
