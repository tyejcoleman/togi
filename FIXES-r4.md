# Togi fix round 4 (final design review, 2026-10-27, against a47ced5)

Full review: the session scratchpad under `review-togi-3/` (probe bundle, outputs). Every item needs a
regression test that fails before the fix and passes after. Keep `python3 -m pytest -q` green (454 now) and
the real-home guard passing.

## High
- **H1. Human-only commands load code from the current directory or `PYTHONPATH`.**
  - The cause: `bin/togi` and the `togi run` re-exec use `python3 -m togi`, which puts the cwd first on
    `sys.path` and honours `PYTHONPATH`. A stdlib-named file (`hmac.py`, `json.py`, …) in the directory
    where the person runs `togi approve`/`lock`/`revert`/`apply` runs inside the person's session.
  - The fix:
    - launch with `python3 -I` (isolated: no cwd, no `PYTHON*` env) and insert Togi's `src` explicitly; do
      the same in the run-host re-exec and the stone shim;
    - human gates also refuse if any `PYTHON*` variable is set or the cwd/`''` is on `sys.path`;
    - bundles are still imported from their registered repo paths, which are added explicitly.
  - Regression test: a dummy `hmac.py` / `json.py` in the cwd and on `PYTHONPATH` is never imported by
    `togi approve` (marker file not written).
- **H2. Test-scope approvals run money commands by default.**
  - Togi refuses a test-scope grant for a gated effect unless the bundle manifest opts in with
    `[policy] test_approvals = true`. Crowd will opt in; it already guards with fake-mode.
  - The stone sandbox may set the opt-in only for its own sandbox run.
  - Update `examples/notes` so `tip_send` checks `ctx.approval_scope` the way the SPEC says bundles must.

## Medium
- **M1. What the person sees at approve is not what gets signed.** At approve time:
  - recompute `args_hash` from the shown args, and the binding from the live repo;
  - regenerate the summary and confirmation word from the loaded command, not the pending file;
  - refuse on any mismatch with the pending record;
  - sign what was recomputed.
- **M2. Single use is not signature-protected.**
  - `check_for_use` refuses if a `.claimed` marker exists.
  - Keep a spent-approval record outside `$TOGI_HOME`, in the approver home (default scope), appended
    under a lock with a MAC. `check_for_use` refuses ids found there.
  - Consumption must write the spent record before the command runs.
- **M3. The money path never checks the lock.**
  - At request time and at use time, a money command requires a passing, anchored `verify_locked`
    whenever the bundle has a lock file. With no lock file, require the manifest to say
    `[policy] require_lock = false` explicitly; default true for bundles with locked tiers.
  - Show the lock state in the approval prompt.
  - `locked_files` must not skip symlinks: a symlink under a locked pattern is reported (and refused for
    apply).
- **M4. The evaluation cache (`proposals/<id>/evaluation.json`) is unsigned.** MAC it with the audit key and
  refuse an unsigned or mismatched cache (re-run the stone).
- **M5. Output caps fail on the CLI.** Measured: a 5,000-key dict gave 62 KB; one long string 100 KB; bundle
  prints 100 KB; a `togi run` exception 204 KB on stderr.
  - Use `fit_any` plus a hard final cap on the serialised envelope.
  - Truncate wide dicts with a `more` hint.
  - Redirect bundle stdout during command execution into a capped buffer (as MCP already does).
  - Cap child error messages and the socket line length in run mode.
  - Tests for each measured case.

## Low
- **L1.** The audit verify used by apply/revert must require `authenticated` (every entry after the first
  locally signed entry must be signed, and an all-unsigned legacy log can't be appended to without a person).
- **L2.** `lock_anchored` verifies the kid and mac of the lock entry it relies on.
- **L3.** The README and SPEC §6 describe lock-file anchoring; fix the stale README line about it.
- **L4.** Polish may only touch files from an allowlist of text types: `.md`, `.txt`, `.toml` that isn't
  `pyproject`/`togi.toml`, `.json`, `.yaml`/`.yml`. Everything else counts as code for this rule.
- **L5.** Unknown flag *names* are hashed in the journal unless they match a known flag of any command.
  `ctx.journal_note` payloads for write/money/admin commands record keys and value types only.
- **Run-mode site-packages:** the strict sandbox profile denies the purelib/platlib paths containing any
  registered bundle module (or only allows the stdlib dir). Test with a fake installed module.

## Constraints
- This round changes Togi only. A separate small change in the Crowd Price repository opts Crowd into
  `test_approvals` and `require_lock`.
- Never write the real `<pw_dir>/.togi`.
- No `claude -p`. No broad `pkill`. Do not commit.
