"""One cell: one item x one subject x one seed, in a throwaway sandbox. Returns a plain dict (the record the store keeps).

For each SESSION of the item, in order:
  1. its `between` events run (harness commands that change the world; `@approve|@deny|@expire <selector>` are the test-only
     approval hook);
  2. the agent starts COLD: a new agent session id and a fresh conversation. Whatever it knows next, it got from Togi;
  3. the provider drives the session; then the harness reads what really happened from the sandbox (the journal slice, the
     answers log, the task record), never from what the agent said;
  4. the session's own checks run.
After the last session the end-of-item checks run over the whole trajectory and the last valid answer. The item passes when
every gating check passes, nothing forbidden was attempted, no session overran its turn budget and nothing went wrong in the
driver. Per-session results are kept so the analysis can draw the per-session success curve.
"""
from __future__ import annotations

import time
import traceback
from typing import Any, Callable

from .. import stone
from . import checks as checklib
from . import trajectory
from .items import Item, Session, Suite
from .providers import SessionContext, SessionRun, run_session
from .sandbox import EvalSandbox, pretty_env_toggles
from .subject import Subject

HOOKS = ("@approve", "@deny", "@expire")
HARNESS_SESSIONS_KEY = "harness"


def cell_key(item_hash: str, subject_hash: str, seed: int, env_digest: str = "") -> str:
    return f"{item_hash[:16]}:{subject_hash[:16]}:{seed}:{env_digest[:8]}"


def run_between(sb: EvalSandbox, events: list[str]) -> list[dict]:
    log = []
    for ev in events:
        head, _, rest = ev.partition(" ")
        if head in HOOKS:
            res = sb.hook(head[1:], rest.strip() or "latest")
            log.append({"event": ev, "ok": bool(res.get("ok")), "detail": res.get("error", "")})
        else:
            p = sb.sh(ev, agent=False)
            log.append({"event": ev, "ok": p.returncode == 0, "detail": "" if p.returncode == 0 else (p.stdout + p.stderr)[:120]})
    return log


def _last_valid(answers: list[dict]) -> dict | None:
    for a in reversed(answers):
        if a.get("valid"):
            return a.get("answer")
    return None


def _forbidden(item: Item, entries: list[dict], sb: EvalSandbox, run: SessionRun, provider: str) -> list[str]:
    mine = [e for e in entries if e.get("session") not in (sb.harness_session, sb.loop_session)
            and e.get("event", "call") in stone.ATTEMPT_EVENTS]
    attempts = [stone.cmdline_of(e) for e in mine]
    if provider == "script":
        attempts += list(run.attempts)
    return sorted({a for a in attempts for pre in item.forbids if stone.forbidden_match(a, pre)})


def execute_cell(suite: Suite, item: Item, subject: Subject, seed: int, *, item_timeout: float = 300.0,
                 max_item_cost: float | None = None, judge: Callable[[str, str], str] | None = None) -> dict:
    t0 = time.monotonic()
    res: dict[str, Any] = {"item": item.id, "item_hash": item.hash(suite.path), "split": item.split, "world": item.world,
                           "tier": item.tier, "capabilities": list(item.capabilities), "subject": subject.id,
                           "subject_hash": subject.hash, "seed": seed, "surface": subject.surface, "n_sessions": len(item.sessions),
                           "has_schema": item.answer_schema is not None, "instruction": item.instruction[:300],
                           "legacy": item.legacy, "meta": item.meta, "bundles": list(item.bundles),
                           "success": None, "driver_error": None, "sessions": [], "forbidden": []}
    sb = None
    try:
        bundles = {b: suite.resolve_bundle(b) for b in item.bundles}
        if not bundles:
            res.update(success=False, driver_error="the item names no bundle to run against")
            return _finish(res, t0)
        sb = EvalSandbox(bundles, label=item.id[:12], toggles=pretty_env_toggles(subject.harness), suite_dir=str(suite.path),
                         skill_files=subject.skill_files)
        sb.write_answer_schema(item.answer_schema)
        for s in item.setup:
            p = sb.sh(s, agent=False)
            if p.returncode != 0:
                res.update(success=False, driver_error=f"setup failed ({s}): exit {p.returncode} {(p.stdout + p.stderr)[:120]}")
                return _finish(res, t0)
        mark = sb.journal_mark()
        ans0 = sb.answers_mark()
        runs: list[SessionRun] = []
        all_session_checks: list[dict] = []
        gate_session_checks: list[dict] = []
        for k, session in enumerate(item.sessions):
            between_log = run_between(sb, session.between) if session.between else []
            bad_between = [b for b in between_log if not b["ok"]]
            if bad_between:
                res.update(success=False, driver_error=f"between event failed before session {k + 1}: {bad_between[0]['event']} ({bad_between[0]['detail']})")
                break
            if k > 0:
                sb.new_agent_session()
            jmark, amark = sb.journal_mark(), sb.answers_mark()
            ctx = SessionContext(item=item, session=session, index=k, subject=subject, sb=sb, seed=seed + 1000 * k,
                                 timeout=item_timeout, max_cost=max_item_cost, last=k == len(item.sessions) - 1)
            ts = time.monotonic()
            run = run_session(ctx)
            runs.append(run)
            entries = sb.journal_since(jmark)
            traj = trajectory.from_journal(entries, sessions={sb.agent_session})
            answers = sb.answers_since(amark)
            record = sb.task_record()
            rec: dict[str, Any] = {"index": k, "instruction": session.instruction[:200], "turns": run.turns, "tokens": run.tokens,
                                   "prompt_tokens": run.prompt_tokens, "cost_usd": run.cost_usd, "finish_reason": run.finish_reason,
                                   "wall_s": round(time.monotonic() - ts, 3), "between": between_log, "steps": run.steps,
                                   "events": run.events, "driver_error": run.driver_error, "timed_out": run.timed_out,
                                   "answers": [{k2: a.get(k2) for k2 in ("seq", "valid", "errors", "answer")} for a in answers],
                                   "trajectory": trajectory.compact(traj), "metrics": trajectory.metrics(traj),
                                   "task_record": record, "skipped": run.skipped}
            if run.skipped:
                res["skipped"] = run.skipped
            forb = _forbidden(item, entries, sb, run, subject.provider)
            rec["forbidden"] = forb
            budget = item.session_budget(session)
            rec["over_budget"] = run.turns is not None and run.turns > budget
            rec["budget_turns"] = budget
            transcript = run.answer_text
            if answers:
                transcript += "\n" + "\n".join(str(a.get("answer")) for a in answers if a.get("valid"))
            cctx = checklib.CheckContext(item=item, session_index=k, answer=_last_valid(answers), answers=answers, trajectory=traj,
                                         transcript=transcript, sandbox=sb, turns=run.turns, tokens=run.tokens, cost_usd=run.cost_usd,
                                         forbidden=forb, judge=judge)
            if session.checks:
                sres = checklib.evaluate_all(session.checks, cctx)
                for c, r in zip(session.checks, sres):
                    r["session"] = k
                    r["milestone"] = not c.get("gate", False)
                    if c.get("gate", False):
                        gate_session_checks.append(r)
                all_session_checks += sres
                rec["checks"] = sres
                rec["success"] = checklib.passed(sres) and not forb and not rec["over_budget"] and not run.driver_error
            else:
                rec["success"] = None
            res["sessions"].append(rec)
            res["forbidden"] = sorted(set(res["forbidden"]) | set(forb))
            if run.driver_error:
                res["driver_error"] = run.driver_error
                break
        if res["driver_error"] or res.get("driver_error"):
            res["success"] = False
            return _finish(res, t0)
        if runs and all(r.skipped for r in runs):
            res.update(success=None)
            return _finish(res, t0)
        # ---- end of item
        all_entries = sb.journal_since(mark)
        agent_traj = trajectory.from_journal(all_entries, sessions=set(sb.agent_sessions))
        all_answers = sb.answers_since(ans0)
        res["answers_recorded"] = len(all_answers)
        res["answer"] = _last_valid(all_answers)
        transcript = "\n".join(r.answer_text for r in runs)
        if all_answers:
            transcript += "\n" + "\n".join(str(a.get("answer")) for a in all_answers if a.get("valid"))
        total_turns = sum(r.turns for r in runs if r.turns is not None) if any(r.turns is not None for r in runs) else None
        total_tokens = sum(r.tokens for r in runs if r.tokens is not None) if any(r.tokens is not None for r in runs) else None
        total_cost = sum(r.cost_usd for r in runs if r.cost_usd is not None) if any(r.cost_usd is not None for r in runs) else None
        end_ctx = checklib.CheckContext(item=item, session_index=None, answer=res["answer"], answers=all_answers, trajectory=agent_traj,
                                        transcript=transcript, sandbox=sb, turns=total_turns, tokens=total_tokens, cost_usd=total_cost,
                                        forbidden=res["forbidden"], judge=judge)
        end_checks = list(item.checks)
        if item.answer_schema is not None:
            end_checks = [{"type": "answer_valid"}] + end_checks
        end_results = checklib.evaluate_all(end_checks, end_ctx)
        for r in end_results:
            r["session"] = None
        over = [r["index"] for r in res["sessions"] if r.get("over_budget")]
        res["checks"] = end_results + [r for r in gate_session_checks]
        res["milestones"] = [r for r in all_session_checks if r.get("milestone")]
        res["over_budget"] = bool(over)
        res["over_budget_sessions"] = over
        res["judge"] = [r for r in res["checks"] if r["type"] == "judge"]
        res["success"] = bool(checklib.passed(res["checks"]) and not res["forbidden"] and not over)
        res["turns"], res["tokens"], res["cost_usd"] = total_turns, total_tokens, total_cost
        res["prompt_tokens"] = sum(r.prompt_tokens or 0 for r in runs) or None
        res["timed_out"] = any(r.timed_out for r in runs)
        res["task_history"] = sb.task_history()[-60:]
        res["task_record"] = sb.task_record()
        res["metrics"] = trajectory.metrics(agent_traj)
        res["drift"] = trajectory.drift(agent_traj, res["task_history"])
        if item.reference_trajectory:
            res["reference"] = trajectory.against_reference(agent_traj, item.reference_trajectory)
        res["answer_tail"] = (runs[-1].answer_text if runs else "")[-400:]
        return _finish(res, t0)
    except Exception as e:      # a bug in the harness is a driver error: never a verdict about the subject
        res.update(success=False, driver_error=f"internal: {type(e).__name__}: {e}")
        res["traceback"] = traceback.format_exc()[-1500:]
        return _finish(res, t0)
    finally:
        if sb is not None:
            sb.cleanup()


def _finish(res: dict, t0: float) -> dict:
    res["wall_s"] = round(time.monotonic() - t0, 3)
    res["valid"] = not res.get("driver_error")
    return res
