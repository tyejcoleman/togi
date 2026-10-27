# Togi fix round 5 (design review 5, 2026-10-27, against 12b4416)

The review's checks are in the session scratchpad under `review-togi-5/test_review5.py`. Each item needs a
regression test that fails at 12b4416 (`git stash` or a worktree) and passes after. Keep
`python3 -m pytest -q` green (564 now) and the real-home guard passing.

## High
- **H1. The lock covers file paths, not the code that runs.** Make the lock and binding cover what Python
  will actually import for a bundle's gated commands:
  - **Gated-code coverage.** Every importable file (`.py`, `.so`/`.pyd`, `.pyc`, `.pth`, package dirs) under
    the package containing each gated command's module must be in the locked or reshape tier.
    - Untiered code there makes the money path refuse (`gated_code_unlocked`) and `togi doctor` warn.
    - Reshape files inside the package are allowed: changing them already needs the real approver.
  - **No skip-list hole.** Under locked patterns, hash every importable thing with no exemption for
    `__pycache__`, `build`, `dist` or `venv`. A package directory or an extension module that shadows a locked
    module counts as a change.
  - **Bytecode.** Run bundle code with `sys.dont_write_bytecode = True` and
    `sys.pycache_prefix` set to a Togi-owned cache dir in `$TOGI_HOME`, so a planted repo `__pycache__` is never
    read.
  - **Import order.** Bundle repo paths go after the standard library on `sys.path`, so a repo-root file
    can't shadow stdlib modules. Test with a planted `json.py` / `decimal.py` at the repo root and in `src/`.
  - **Example bundle.** Update `examples/notes` so its gated module and package are locked.
  - **SPEC §2.** Document the rule: the code behind a money command must be locked or reshape.
- **H2. `togi approve` runs the bundle's code before the person decides.**
  - Best fix: build the approval summary and confirmation word at request time inside the host, then store
    them in the pending record, which is signed by nobody and so untrusted. At approve, verify them against a
    **describe snapshot** recorded and signed (audit key) at `togi lock` time, plus a pure-data recomputation
    that needs no bundle import. The snapshot holds each command's name, effect, parameter schema and
    `confirm` param.
  - If a bundle's summary needs code, the describe child must run:
    - under the `togi run` strict sandbox profile;
    - in its own session (`start_new_session`), with its process group killed afterwards;
    - with no `run_env` values and a scrubbed env.
  - Test: an approve on a bundle whose import writes a marker file never writes it outside the sandbox and
    never inherits the person's terminal.

## Medium
- **M1.** Give the approver its own 0700 directory (`<pw_dir>/.togi/approver.d/`) holding the passphrase
  record, the key, the attempt state and the spent log. Refuse a `TOGI_HOME` that equals or contains it.
  Migrate r4 files in place, once, under the human gate. Fix the README wording.
- **M2.** The `togi lock` prompt shows:
  - the locked files changed, missing and new since the last lock (with sizes);
  - the head-pointer state;
  - the audit entries since the last lock.
  A missing head or a non-empty change set needs an extra typed confirmation ("relock").
- **M3.** Capture bundle stderr during command execution with the same cap as stdout, on CLI and MCP.

## Low
- **L1.** The approve prompt shows the expiry that will be signed (now + TTL), not the requester's field.
- **L2.** If any argument value would be truncated at the prompt, refuse with `prompt_too_long` (the person can't
  see what they sign), as the 1000-line diff rule already does.
- **L3.** Clamp `togi run --max-output` to the `--full` limit, and document it.

## Constraints
- Do not touch `~/Development/closure`. A Crowd refuter is running there; its manifest will be updated
  separately so its whole package is locked or reshape.
- Never write the real `<pw_dir>/.togi`.
- No `claude -p`, no broad `pkill`, do not commit.
