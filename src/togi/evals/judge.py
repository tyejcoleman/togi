"""The rubric judge for `judge` checks: a Claude model, through `claude -p` on the subscription (no API key), asked to reply
`pass` or `fail`. A judge is non-deterministic, so a `judge` check declares its rubric and a calibration set (checks.py), is
calibrated against it before it is believed, is reported separately and never gates. Anything the judge says that is not
`pass` or `fail` (or a missing `claude`) makes the verdict `error`, which the check reports as not scored."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import Callable

from .. import stone

SYSTEM = ("You are a strict grader. You are given a RUBRIC and an ANSWER. Decide whether the answer satisfies the rubric. "
          "Reply with exactly one word: pass or fail.")


def claude_judge(model: str | None = None, timeout: float = 120.0) -> Callable[[str, str], str]:
    def judge(rubric: str, text: str) -> str:
        cmd = stone.claude_base() + ["--system-prompt", SYSTEM, "--tools="] + (["--model", model] if model else [])
        clean = {k: v for k, v in os.environ.items() if not k.startswith("TOGI_") and k not in stone.SECRET_ENV}
        clean["TOGI_AGENT"] = "eval-judge"
        prompt = f"RUBRIC:\n{rubric}\n\nANSWER:\n{text}\n\nDoes the answer satisfy the rubric? Reply with exactly one word: pass or fail."
        try:
            with tempfile.TemporaryDirectory(prefix="togi-judge-") as empty:
                p = stone.run_group(cmd, input=prompt, env=clean, cwd=empty, timeout=timeout)
            res = stone.parse_claude_output(p.stdout)
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, StopIteration):
            return "error"
        if res.get("is_error"):
            return "error"
        m = re.match(r"\W*(pass|fail)\b", res["answer"].strip(), re.I)
        return m.group(1).lower() if m else "error"
    return judge
