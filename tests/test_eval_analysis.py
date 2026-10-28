"""Eval layer: the statistics against known values, then the analysis (success tables, paired tests, item statistics, verdicts,
curves, long-horizon metrics) and the failure taxonomy over synthetic runs."""
from __future__ import annotations

import math

import pytest

from togi.evals import analysis, stats, taxonomy, trajectory
from togi.evals.outcome import Gates, Outcome

# ----------------------------------------------------------------------------- statistics: known values


def test_wilson_matches_textbook_values():
    lo, hi = stats.wilson(8, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.4902, 0.9433)
    lo, hi = stats.wilson(0, 10)
    assert lo == 0.0 and round(hi, 4) == 0.2775
    lo, hi = stats.wilson(10, 10)
    assert round(lo, 4) == 0.7225 and hi == 1.0
    assert stats.wilson(0, 0) == (0.0, 1.0)
    lo, hi = stats.wilson(50, 100)
    assert round(lo, 4) == 0.4038 and round(hi, 4) == 0.5962
    assert stats.wilson(8, 10, z=1.0)[0] > stats.wilson(8, 10)[0]            # a narrower z gives a narrower interval


def test_mcnemar_exact_known_values():
    assert stats.mcnemar_exact(10, 2) == pytest.approx(158 / 4096)
    assert stats.mcnemar_exact(2, 10) == pytest.approx(158 / 4096)
    assert stats.mcnemar_exact(5, 5) == 1.0 and stats.mcnemar_exact(0, 0) == 1.0
    assert stats.mcnemar_exact(0, 8) == pytest.approx(2 / 256)
    assert stats.mcnemar_exact(1, 1) == 1.0
    assert stats.mcnemar_exact(3, 9) == pytest.approx(2 * (1 + 12 + 66 + 220) / 4096)
    assert 0 < stats.mcnemar_exact(400, 300) < 0.001                          # large counts do not overflow


def test_paired_bootstrap_is_deterministic_per_seed_and_brackets_the_mean():
    diffs = [1, 0, 0.5, -0.5, 1, 0, 0.25, 0.75, 0, 0.5, 1, -0.25]
    a = stats.paired_bootstrap(diffs, resamples=2000, seed=7)
    assert a == stats.paired_bootstrap(diffs, resamples=2000, seed=7)         # same seed, same interval, to the bit
    b = stats.paired_bootstrap(diffs, resamples=2000, seed=8)
    assert (a["lo"], a["hi"]) != (b["lo"], b["hi"])
    assert a["lo"] < a["mean"] < a["hi"] and a["mean"] == pytest.approx(sum(diffs) / len(diffs))
    assert a["n"] == 12 and a["resamples"] == 2000 and a["seed"] == 7
    flat = stats.paired_bootstrap([0.0] * 10, resamples=200, seed=1)
    assert (flat["lo"], flat["hi"], flat["p_two_sided"]) == (0.0, 0.0, 1.0)
    sure = stats.paired_bootstrap([1.0] * 20, resamples=200, seed=1)
    assert sure["lo"] == sure["hi"] == 1.0 and sure["p_two_sided"] == 0.0
    assert stats.paired_bootstrap([], resamples=10)["mean"] is None


def test_correlation_isotonic_inverse_pareto_and_slope():
    assert stats.pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert stats.pearson([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)
    assert stats.pearson([1, 1, 1], [1, 2, 3]) is None and stats.pearson([1, 2], [1, 2]) is None
    assert stats.isotonic([1, 3, 2, 4]) == [1, 2.5, 2.5, 4]
    assert stats.isotonic([1, 2, 3]) == [1, 2, 3] and stats.isotonic([3, 2, 1]) == [2, 2, 2]
    xs, ys = [1, 10, 100], [0.2, 0.5, 0.8]
    assert stats.interp_inverse(xs, ys, 0.5) == (pytest.approx(10), "in_range")
    x, note = stats.interp_inverse(xs, ys, 0.65)
    assert note == "in_range" and x == pytest.approx(10 ** 1.5)
    assert stats.interp_inverse(xs, ys, 0.1) == (1, "below_range") and stats.interp_inverse(xs, ys, 0.9) == (None, "above_range")
    pts = [(1.0, 0.5, "A"), (2.0, 0.9, "B"), (3.0, 0.8, "C"), (1.0, 0.4, "D")]
    assert sorted(stats.pareto_front(pts)) == ["A", "B"]
    assert stats.slope([1, 2, 3], [0.9, 0.7, 0.5]) == pytest.approx(-0.2) and stats.slope([1], [1]) is None
    assert stats.median([3, 1, 2]) == 2 and stats.median([1, 2, 3, 4]) == 2.5 and stats.median([]) is None


# ----------------------------------------------------------------------------- synthetic runs


def mk(item, subject, seed, success, *, split="test", world="w1", tier=1, caps=(), turns=3, cost=None, wall=1.0, **extra):
    c = {"item": item, "subject": subject, "subject_hash": "h-" + subject, "seed": seed, "success": success, "split": split,
         "world": world, "tier": tier, "capabilities": list(caps), "turns": turns, "cost_usd": cost, "wall_s": wall, "driver_error": None,
         "valid": True, "forbidden": [], "sessions": [], "checks": [], "n_sessions": 1}
    c.update(extra)
    return c


def sub(sid, role="candidate", size=None, **extra):
    return {"id": sid, "hash": "h-" + sid, "role": role, "size": size, "provider": "ollama", "surface": "cli",
            "harness": {"loop": "react"}, **extra}


def run(subjects, cells, outcome=None, **man):
    return analysis.RunData({"run_id": "r1", "subjects": subjects, "outcome": outcome.to_dict() if outcome else None, **man}, cells)


def grid(rates: dict[str, list[int]], **kw):
    """rates: subject -> [1/0 per item i0..]; one repeat each."""
    return [mk(f"i{j}", s, 0, bool(v), **kw) for s, vs in rates.items() for j, v in enumerate(vs)]


def test_success_table_has_wilson_intervals_per_subject_and_slice():
    cells = [mk(f"i{j}", "a", 0, j < 8, caps=["numeric"] if j % 2 else ["tool-choice"], tier=1 + j % 2, world="w" + str(j % 2)) for j in range(10)]
    t = analysis.success_table(run([sub("a")], cells))["a"]
    assert t["overall"]["k"] == 8 and t["overall"]["n"] == 10 and (round(t["overall"]["lo"], 4), round(t["overall"]["hi"], 4)) == (0.4902, 0.9433)
    assert t["capability:numeric"]["n"] == 5 and t["capability:tool-choice"]["n"] == 5 and t["tier:1"]["n"] == 5 and t["world:w1"]["n"] == 5
    assert analysis.success_table(run([sub("a")], cells), splits=["dev"]) == {}


def test_paired_comparison_counts_item_level_wins_and_runs_both_tests():
    a = [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0]
    b = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 1]
    d = run([sub("a"), sub("b")], grid({"a": a, "b": b}))
    p = analysis.paired(d, "a", "b", resamples=2000, seed=3)
    assert (p["items"], p["wins_a"], p["wins_b"], p["ties"]) == (12, 8, 1, 3)
    assert p["p_mcnemar"] == pytest.approx(stats.mcnemar_exact(8, 1)) and p["p_mcnemar"] < 0.05
    assert p["diff"] == pytest.approx(7 / 12) and p["diff_lo"] < p["diff"] < p["diff_hi"] and p["diff_lo"] > 0
    assert p == analysis.paired(d, "a", "b", resamples=2000, seed=3)
    # with repeats, an item is a win for the subject with the higher pass RATE on it
    cells = [mk("x", "a", s, bool(v)) for s, v in enumerate([1, 1, 0])] + [mk("x", "b", s, bool(v)) for s, v in enumerate([1, 0, 0])]
    q = analysis.paired(run([sub("a"), sub("b")], cells), "a", "b", resamples=100)
    assert (q["wins_a"], q["wins_b"]) == (1, 0)
    refs = analysis.paired_comparisons(run([sub("a"), sub("b", role="reference")], grid({"a": a, "b": b})), resamples=100)
    assert [(x["a"], x["b"]) for x in refs] == [("a", "b")]                    # every subject against the reference


def test_item_statistics_difficulty_discrimination_and_saturation():
    skills = {"s1": 0.2, "s2": 0.5, "s3": 0.8, "s4": 1.0}
    rates = {s: [] for s in skills}
    for j in range(10):                                  # items i0..i9: i0 everyone passes, i1 nobody, i2 only the strong, i3 only the weak
        for s, k in skills.items():
            if j == 0:
                v = 1
            elif j == 1:
                v = 0
            elif j == 2:
                v = int(k >= 0.8)
            elif j == 3:
                v = int(k <= 0.2)
            else:
                v = int(j / 10 + k > 1.0)
            rates[s].append(v)
    stat = {i["item"]: i for i in analysis.item_statistics(run([sub(s) for s in skills], grid(rates)))}
    assert stat["i0"]["saturation"] == "all_pass" and "all_pass" in stat["i0"]["flags"] and stat["i0"]["difficulty"] == 1.0
    assert stat["i1"]["saturation"] == "all_fail" and "suspect_all_fail" in stat["i1"]["flags"] and stat["i1"]["suspect"] is True
    assert stat["i2"]["discrimination"] > 0.8 and not stat["i2"]["flags"]
    assert stat["i3"]["discrimination"] < -0.3 and "negative_discrimination" in stat["i3"]["flags"]
    assert stat["i2"]["difficulty"] == pytest.approx(0.5) and stat["i0"]["discrimination"] is None
    two = {i["item"]: i for i in analysis.item_statistics(run([sub("s1"), sub("s2")], grid({"s1": [1, 0], "s2": [1, 0]})))}
    assert two["i0"]["discrimination"] is None                       # two subjects cannot give a correlation


def test_a_reference_that_fails_an_item_flags_it_suspect_before_the_subject_is_blamed():
    cells = grid({"ref": [1, 0, 1, 1], "cand": [1, 0, 0, 1], "weak": [0, 0, 0, 1]})
    d = run([sub("ref", role="reference"), sub("cand"), sub("weak")], cells)
    s = analysis.reference_sanity(d)
    assert s["i1"] == {"suspect": True, "failed_by": ["ref"], "passed_by_others": False}
    assert s["i2"]["suspect"] is False                                # the candidates failing i2 is on the candidates
    items = {i["item"]: i for i in analysis.item_statistics(d)}
    assert items["i1"]["suspect"] and "suspect_reference_fail" in items["i1"]["flags"] and not items["i2"]["suspect"]
    flaky = grid({"ref": [1], "cand": [1]})
    assert analysis.reference_sanity(run([sub("ref", role="target"), sub("cand")], flaky))["i0"]["suspect"] is False
    cells2 = [mk("i0", "ref", s, bool(v)) for s, v in enumerate([1, 0, 0])] + [mk("i0", "cand", 0, True)]
    assert analysis.reference_sanity(run([sub("ref", role="reference"), sub("cand")], cells2))["i0"]["suspect"] is True     # 1 of 3 < 0.5


# ----------------------------------------------------------------------------- verdicts


def outcome(**gates):
    return Outcome(name="o", decision="can small replace big?", splits=["test"], gates=Gates(**gates))


def test_verdict_is_the_worst_slice_with_wilson_lower_bounds():
    # 40 test cells: strong overall (36/40 = 0.90) but the `recovery` slice (5 cells) fails
    cells = [mk(f"i{j}", "a", 0, j >= 4, caps=["recovery"] if j < 5 else ["numeric"]) for j in range(40)]
    oc = outcome(min_success=0.8, min_success_lcb=0.6)
    v = analysis.verdict_for(run([sub("a")], cells), oc, "a")
    assert v["verdict"] == "FAIL" and v["worst_slice"]["slice"] == "capability:recovery"
    assert any(f["slice"] == "capability:recovery" and f["gate"] == "min_success" for f in v["failing"])
    assert any("capability:recovery" in r for r in v["reasons"])
    overall = next(s for s in v["slices"] if s["slice"] == "overall")
    assert overall["k"] == 36 and overall["lo"] > 0.76
    # drop the weak slice (min_cells above its size) and the same subject passes
    oc2 = outcome(min_success=0.8, min_success_lcb=0.6)
    oc2.min_cells = 6
    assert analysis.verdict_for(run([sub("a")], cells), oc2, "a")["verdict"] == "PASS"


def test_a_tiny_sample_cannot_pass_a_lower_bound_gate():
    cells = [mk(f"i{j}", "a", 0, True) for j in range(5)]
    v = analysis.verdict_for(run([sub("a")], cells), outcome(min_success=0.9, min_success_lcb=0.7), "a")
    assert v["verdict"] == "FAIL" and v["failing"][0]["gate"] == "min_success_lcb" and round(v["worst_slice"]["lo"], 2) == 0.57
    cells = [mk(f"i{j}", "a", 0, True) for j in range(30)]
    assert analysis.verdict_for(run([sub("a")], cells), outcome(min_success=0.9, min_success_lcb=0.7), "a")["verdict"] == "PASS"


def test_other_gates_forbidden_turns_cost_and_splits():
    cells = [mk(f"i{j}", "a", 0, True, turns=9, cost=0.05) for j in range(30)]
    cells[0]["forbidden"] = ["togi approve"]
    d = run([sub("a")], cells)
    assert analysis.verdict_for(d, outcome(max_forbidden=0), "a")["failing"][0]["gate"] == "max_forbidden"
    assert analysis.verdict_for(d, outcome(max_forbidden=1), "a")["verdict"] == "PASS"
    assert analysis.verdict_for(d, outcome(max_forbidden=1, max_median_turns=8), "a")["failing"][0]["gate"] == "max_median_turns"
    assert analysis.verdict_for(d, outcome(max_forbidden=1, max_median_cost_usd=0.01), "a")["failing"][0]["gate"] == "max_median_cost_usd"
    dev_only = run([sub("a")], [mk("i0", "a", 0, True, split="dev")])
    v = analysis.verdict_for(dev_only, outcome(), "a")                         # dev never counts toward the verdict
    assert v["verdict"] == "FAIL" and v["failing"][0]["gate"] == "scored_cells"


def test_non_inferiority_to_a_reference_uses_a_paired_bootstrap():
    ref = [1] * 25 + [0] * 5
    close = [1] * 24 + [0] * 6
    far = [1] * 10 + [0] * 20
    d = run([sub("sonnet", role="target"), sub("close"), sub("far")], grid({"sonnet": ref, "close": close, "far": far}))
    oc = outcome(min_success=0.0, min_success_lcb=0.0, non_inferior_to="sonnet", margin=0.10)
    assert analysis.verdict_for(d, oc, "close")["verdict"] == "PASS"
    v = analysis.verdict_for(d, oc, "far")
    assert v["verdict"] == "FAIL" and any(f["gate"] == "non_inferior" for f in v["failing"])


def test_smallest_passing_prefers_size_then_cost_and_excluding_suspect_items_can_change_a_verdict():
    good = [1] * 30
    d = run([sub("big", size=70, role="target"), sub("mid", size=7), sub("small", size=1.5), sub("bad", size=0.5)],
            grid({"big": good, "mid": good, "small": good, "bad": [1] * 15 + [0] * 15}))
    v = analysis.verdicts(d, outcome(min_success=0.8, min_success_lcb=0.7))
    assert v["subjects"]["small"]["verdict"] == "PASS" and v["subjects"]["bad"]["verdict"] == "FAIL"
    assert v["smallest_passing"] == {"subject": "small", "size": 1.5, "basis": "size", "passing": ["big", "mid", "small"]}
    nobody = analysis.verdicts(run([sub("a", size=1)], grid({"a": [0] * 10})), outcome(min_success=0.5))
    assert nobody["smallest_passing"] is None
    # an item the reference fails is suspect; judging without it can flip a candidate's verdict
    cells = grid({"ref": [1] * 28 + [0, 0], "cand": [1] * 28 + [0, 0]})
    d2 = run([sub("ref", role="reference"), sub("cand")], cells)
    oc = outcome(min_success=0.97, min_success_lcb=0.85)
    both = analysis.verdicts(d2, oc)
    assert both["suspect_items"] == ["i28", "i29"] and both["subjects"]["cand"]["verdict"] == "FAIL"
    assert both["excluding_suspect"]["subjects"]["cand"]["verdict"] == "PASS"


def test_scaling_curves_harness_lift_and_pareto():
    def s(i, size, rate, loop):
        n = 100
        return [mk(f"i{j}", i, 0, j < rate * n, cost=size / 1000, wall=size) for j in range(n)], {
            **sub(i, size=size), "harness": {"loop": loop}}
    cells, subs = [], []
    for i, size, rate, loop in [("b1", 1, 0.2, "react"), ("b3", 3, 0.5, "react"), ("b7", 7, 0.8, "react"),
                                ("o1", 1, 0.5, "operator"), ("o2", 2, 0.9, "operator"), ("u", 5, 0.4, "react")]:
        c, su = s(i, size, rate, loop)
        cells += c
        subs.append(su)
    subs[-1].pop("size")
    subs[-1]["size"] = None
    d = run(subs, cells)
    sc = analysis.scaling_curves(d)
    assert sc["unsized"] == ["u"] and len(sc["curves"]) == 2
    base = next(c for c in sc["curves"].values() if c["harness"]["loop"] == "react")
    assert [p["size"] for p in base["points"]] == [1, 3, 7] and [round(p["rate_monotone"], 2) for p in base["points"]] == [0.2, 0.5, 0.8]
    lift = {x["subject"]: x for x in analysis.harness_lift(sc)}
    assert lift["o1"]["equivalent_baseline_size"] == pytest.approx(3.0) and lift["o1"]["gain_factor"] == pytest.approx(3.0)
    assert lift["o2"]["note"] == "above_range" and lift["o2"]["gain_factor"] is None            # no baseline model got that far
    p = analysis.pareto(d)
    assert "b1" not in p["cost"]["frontier"] and "o1" in p["cost"]["frontier"]                   # o1 costs the same as b1 and does better
    assert p["cost"]["points"] and set(p["latency"]["frontier"]) <= {x["subject"] for x in p["latency"]["points"]}
    costs = analysis.pareto(run([sub("cheap"), sub("rich"), sub("worse")], grid({"cheap": [1, 1, 0, 0], "rich": [1, 1, 1, 1], "worse": [1, 0, 0, 0]})))
    assert costs["cost"]["points"] == []                                                         # no costs recorded: no frontier
    pr = run([sub("cheap"), sub("rich"), sub("worse")], [mk(f"i{j}", n, 0, bool(v), cost=c) for n, c, vs in (
        ("cheap", 0.01, [1, 1, 0, 0]), ("rich", 0.10, [1, 1, 1, 1]), ("worse", 0.20, [1, 0, 0, 0])) for j, v in enumerate(vs)])
    assert sorted(analysis.pareto(pr)["cost"]["frontier"]) == ["cheap", "rich"]


def test_cost_falls_back_to_a_price_per_token():
    c = mk("i", "a", 0, True, tokens=2_000_000, prompt_tokens=1_000_000)
    assert analysis.cost_of(c, {"price": {"in_per_mtok": 1.0, "out_per_mtok": 3.0}}) == pytest.approx(4.0)
    assert analysis.cost_of(c, {}) is None and analysis.cost_of({**c, "cost_usd": 0.5}, {}) == 0.5


# ----------------------------------------------------------------------------- failure taxonomy


def call(cmd, *, ok=True, status=None, code=None, out="", event="call", effect="read", args=None, surface="cli"):
    return {"event": event, "cmdline": "togi " + cmd, "ok": ok, "status": status or ("ok" if ok else "error"), "error_code": code,
            "out": out, "effect": effect, "args": args, "surface": surface}


def failed(traj=(), answers=None, answer=None, **kw):
    sess = {"index": 0, "trajectory": list(traj), "answers": answers or [], "finish_reason": kw.pop("finish_reason", "claude_done"),
            "instruction": kw.pop("instruction", "")}
    return mk(kw.pop("item", "i"), "a", kw.pop("seed", 0), False, sessions=[sess], answer=answer, has_schema=kw.pop("has_schema", True), **kw)


@pytest.mark.parametrize("cell,cls", [
    (failed(driver_error="the claude CLI is not on PATH"), "driver_error"),
    (failed(forbidden=["togi approve"], answer={"v": 99}), "forbidden"),
    (failed(over_budget=True), "budget"),
    (failed(finish_reason="step_budget"), "budget"),
    (failed(timed_out=True), "budget"),
    (failed(answers=[{"valid": False, "errors": ["$.v: expected number"], "answer": {"v": "x"}}]), "format"),
    (failed(answer_tail="The total is 47,248.", answers=[]), "format"),
    (failed(traj=[call("notes find", args="a")] * 3), "loop"),
    (failed(finish_reason="repeated_action"), "loop"),
    (failed(finish_reason="gave_up"), "gave_up"),
    (failed(answer_tail="I'm sorry, I cannot do that.", has_schema=False), "gave_up"),
    (failed(traj=[call("notes tip send", ok=False, status="needs_approval"), call("notes tip send", ok=False, status="needs_approval")],
            has_schema=False, answer_tail="x"), "no_stop"),
    (failed(traj=[call("answer", event="answer", effect="write"), call("notes add", effect="write")], has_schema=False, answer_tail="x"), "no_stop"),
    (failed(traj=[call("sheet query", ok=False, code="bad_sql"), call("sheet query", ok=False, code="bad_sql", args="b"),
                  call("sheet query", ok=False, code="unknown_flag", args="c")], has_schema=False, answer_tail="x"), "bad_args"),
    (failed(traj=[call("notes nope", ok=False, code="unknown_command", event="unknown_command")], has_schema=False, answer_tail="x"), "wrong_tool"),
    (failed(traj=[call("notes list", out='{"ok":true,"data":[{"total":47248}]}')], answer={"total": 47284}, has_schema=False, answer_tail="x"), "misread_output"),
    (failed(traj=[call("notes list", out='{"data":[{"a":10},{"a":32}]}')], answer={"total": 52}, has_schema=False, answer_tail="x"), "did_math"),
    (failed(has_schema=False, answer_tail="x"), "other"),
])
def test_taxonomy_rules(cell, cls):
    got = taxonomy.classify(cell)
    assert got is not None and got["class"] == cls, got


def test_taxonomy_precedence_tags_and_successes():
    c = failed(forbidden=["togi approve"], traj=[call("notes list", out='{"data":[{"a":10}]}')], answer={"total": 52}, has_schema=False, answer_tail="x")
    got = taxonomy.classify(c)
    assert got["class"] == "forbidden" and got["tags"] == ["forbidden", "did_math"]                   # the first rule that fires is the class, the rest are tags
    assert taxonomy.classify(mk("i", "a", 0, True)) is None
    wt = failed(traj=[call("notes list", out='{"data":[{"a":10}]}')], checks=[
        {"type": "trajectory", "check": "trajectory used notes add", "ok": False, "detail": "never ran `notes add`"}], has_schema=False, answer_tail="x")
    assert taxonomy.classify(wt)["class"] == "wrong_tool"
    misread_ok = failed(traj=[call("x y", out='{"n": 1000}')], answer={"n": 1000}, has_schema=False, answer_tail="x")
    assert taxonomy.classify(misread_ok)["class"] == "other"                                         # a grounded number is not blamed
    near = lambda a, b: taxonomy._near(a, b)
    assert near(47284, 47248) and near(1234, 1243) and near(1000.4, 1000) and not near(5, 5) and not near(100, 150)


def test_taxonomy_counts_and_audit_sampling_is_seeded_and_class_specific():
    cells = [failed(finish_reason="gave_up", key=f"k{i}", item=f"it{i}", seed=i) for i in range(12)] + [failed(over_budget=True, key="kb", item="b")]
    cells.append(mk("ok", "a", 0, True))
    t = taxonomy.taxonomy(cells)
    assert t["failed"] == 13 and t["by_class"] == {"budget": 1, "gave_up": 12} and t["by_subject"]["a"]["gave_up"] == 12
    s1 = taxonomy.audit_sample(cells, "gave_up", n=4, seed=1)
    assert s1 == taxonomy.audit_sample(cells, "gave_up", n=4, seed=1) and len(s1) == 4 and {x["class"] for x in s1} == {"gave_up"}
    assert [x["item"] for x in s1] != [x["item"] for x in taxonomy.audit_sample(cells, "gave_up", n=4, seed=2)]
    assert len(taxonomy.audit_sample(cells, "budget", n=4)) == 1 and taxonomy.audit_sample(cells, "loop") == []
    with pytest.raises(ValueError):
        taxonomy.audit_sample(cells, "vibes")


# ----------------------------------------------------------------------------- trajectories + long-horizon


def test_repeated_actions_need_the_same_call_with_no_state_change_between():
    t = [call("notes find", args="milk", effect="read"), call("notes find", args="milk", effect="read"),
         call("notes add", args="x", effect="write"), call("notes find", args="milk", effect="read"),
         call("notes find", args="eggs", effect="read"), call("notes find", args="eggs", effect="read")]
    assert trajectory.repeated_actions(t) == [1, 5]          # index 3 repeats index 0/1, but a write came between
    twice = [call("notes add", args="x", effect="write"), call("notes add", args="x", effect="write")]
    assert trajectory.repeated_actions(twice) == [1]          # the same write twice is waste (the first already changed the world)
    assert trajectory.metrics(t)["repeated"] == 2


def test_drift_flags_state_changes_the_task_record_does_not_account_for():
    t = [dict(call("notes add", effect="write", args="x"), ts="2026-01-01T00:00:02Z", bundle="notes", command="add"),
         dict(call("notes delete", effect="admin", args="x"), ts="2026-01-01T00:00:04Z", bundle="notes", command="delete")]
    hist = [{"ts": "2026-01-01T00:00:01Z", "record": {"goal": "add notes for the party", "done": [], "next": "add the first note", "waiting": ""}}]
    d = trajectory.drift(t, hist)
    assert d["considered"] == 2 and d["drifting"] == 1 and d["samples"] == ["togi notes delete"] and d["rate"] == 0.5
    assert trajectory.drift(t, [])["considered"] == 0                                 # no record yet: nothing to drift from
    hist.append({"ts": "2026-01-01T00:00:03Z", "record": {"goal": "g", "done": [], "next": "delete the draft note", "waiting": ""}})
    assert trajectory.drift(t, hist)["drifting"] == 0                                 # the record caught up before the delete


def lh_cell(subject, successes, *, steps=4, repeated=0, drifting=0, considered=0, ended_at_approval=(), end=True, **kw):
    sessions = [{"index": k, "success": s, "steps": [{}] * steps, "turns": steps,
                 "metrics": {"calls": steps, "repeated": repeated, "ended_at_approval": k in ended_at_approval}}
                for k, s in enumerate(successes)]
    return mk("long", subject, kw.pop("seed", 0), end, n_sessions=len(successes), sessions=sessions,
              drift={"considered": considered, "drifting": drifting}, metrics={"needs_approval": 1 if ended_at_approval else 0, "state_changes": 1},
              task_history=[{"op": "update"}], **kw)


def test_long_horizon_metrics_curve_decay_waste_drift_and_approvals():
    strong = [lh_cell("strong", [True, True, True, True], seed=s) for s in range(4)]
    decaying = ([lh_cell("decay", [True, True, False, False], seed=s, end=False, repeated=1, drifting=1, considered=2, ended_at_approval=(1,)) for s in range(3)]
                + [lh_cell("decay", [True, False, False, False], seed=3 + s, end=False, repeated=2, drifting=2, considered=2) for s in range(3)])
    d = run([sub("strong"), sub("decay")], strong + decaying + [mk("single", "strong", 0, True)])
    lh = analysis.long_horizon(d)
    assert lh["cells"] == 10                                           # single-session items are not long-horizon
    s, x = lh["subjects"]["strong"], lh["subjects"]["decay"]
    assert s["end_state_success"]["rate"] == 1.0 and x["end_state_success"]["rate"] == 0.0
    assert [p["rate"] for p in s["session_curve"]] == [1.0] * 4 and s["decay_per_session"] == pytest.approx(0.0)
    assert [p["rate"] for p in x["session_curve"]] == [1.0, 0.5, 0.0, 0.0] and x["decay_per_session"] < -0.3
    assert x["session_curve"][1]["n"] == 6 and x["session_curve"][1]["lo"] < 0.5 < x["session_curve"][1]["hi"]
    assert s["steps_per_session"] == 4 and x["steps_by_session"] == [4, 4, 4, 4]
    assert s["wasted_actions"] == 0 and x["wasted_actions"] == 3 * 4 * 1 + 3 * 4 * 2 and x["wasted_rate"] == pytest.approx(36 / 96)
    assert x["drift"] == {"considered": 12, "drifting": 9, "rate": 0.75} and s["drift"]["rate"] is None
    assert x["approval"]["sessions_ended_at_approval"] == 3 and x["approval"]["cells_with_approval_request"] == 3
    assert x["task_record"]["mean_updates"] == 1 and x["task_record"]["cells_changing_state_without_updating"] == 0
    assert analysis.analyze(d, resamples=50)["long_horizon"]["subjects"]["decay"]["decay_per_session"] == x["decay_per_session"]


def test_a_non_inferiority_bar_against_a_subject_that_was_not_run_cannot_be_cleared():
    d = run([sub("a")], grid({"a": [1] * 30}))
    v = analysis.verdict_for(d, outcome(non_inferior_to="sonnet", margin=0.1), "a")
    assert v["verdict"] == "FAIL" and v["failing"][0]["gate"] == "non_inferior_reference" and "was not part of this run" in v["reasons"][0]


def test_a_trajectory_is_compared_with_the_items_reference_trajectory():
    ref = ["togi sheet query --sql 'SELECT 1'", "togi sheet query --sql \"SELECT 2\"", "togi notes add --text x"]
    assert [trajectory.command_key(c) for c in ref] == ["sheet query", "sheet query", "notes add"]
    assert trajectory.command_key("togi notes tip send --id n_1 --amount 5") == "notes tip send"
    lean = [call("sheet query"), call("sheet query"), call("notes add")]
    for e in lean:
        e["bundle"] = e["cmdline"].split()[1]
    r = trajectory.against_reference(lean, ref)
    assert r == {"reference_calls": 3, "agent_calls": 3, "coverage": 1.0, "efficiency": 1.0, "extra_calls": 0}
    wander = [dict(call("sheet describe"), bundle="sheet")] * 3 + lean
    r = trajectory.against_reference(wander, ref)
    assert r["agent_calls"] == 6 and r["efficiency"] == 0.5 and r["extra_calls"] == 3 and r["coverage"] == 1.0
    half = trajectory.against_reference([dict(call("sheet query"), bundle="sheet")], ref)
    assert half["coverage"] == 0.5 and half["efficiency"] == 3.0
    assert trajectory.against_reference([], ref)["efficiency"] is None


def test_reference_trajectory_metrics_summarise_successful_cells_only():
    cells = [mk("a", "s", 0, True, reference={"efficiency": 1.0, "coverage": 1.0, "extra_calls": 0}),
             mk("b", "s", 0, True, reference={"efficiency": 0.5, "coverage": 0.5, "extra_calls": 2}),
             mk("c", "s", 0, False, reference={"efficiency": 0.1, "coverage": 0.0, "extra_calls": 9}), mk("d", "s", 0, True)]
    out = analysis.reference_trajectory_metrics(run([sub("s")], cells))
    assert out == {"s": {"cells": 2, "mean_efficiency": 0.75, "mean_coverage": 0.75, "mean_extra_calls": 1.0}}
