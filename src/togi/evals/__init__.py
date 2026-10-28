"""Togi Eval: the outcome-driven benchmark layer (docs/EVAL-SPEC.md).

    outcome  (outcome.py)    the decision being made, and the bar (gates per slice, reference subjects)
    item     (items.py)      one task in a suite: setup, sessions, answer schema, checks; stone tasks import as items
    check    (checks.py)     the pluggable check library (answer, state, trajectory, budget, judge)
    subject  (subject.py)    model x provider x harness config x surface, with a stable hash
    loops    (loops.py)      the agent-loop strategies (react, operator, plan-act-verify, code-first)
    policy   (policy.py)     per-step model backends (ollama, openai-compatible, claude-step)
    providers (providers.py) how one session of one cell is driven (script, claude-cli, claude-step, ollama, ...)
    cell     (cell.py)       one item x subject x seed run in a throwaway sandbox
    runner / store           the resumable matrix runner and the eval store under $TOGI_HOME/eval
    stats / analysis / taxonomy   pure functions over a run
    integrity (integrity.py) suite lock, unseen-split access, canaries, contamination scan
    generators/              seeded, correct-by-construction item generators (sheet)
    report                   JSON + self-contained HTML, compare

Nothing in this package imports a specific bundle: outcomes, items, checks, subjects and analysis are usable for any job.
"""
