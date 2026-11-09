# Togi fix round 2 (2026-10-27)

## A. An agent can choose where approval state lives (high)

**The hole.** `paths.approver_path()`, `approvals_dir()` and the registry all follow `$TOGI_HOME`, an
ordinary env var an agent can set. So an agent can:
1. set `TOGI_HOME=/tmp/mine`;
2. run `togi add <repo>` there;
3. create its own approver with a fake terminal (`script`);
4. approve its own money command and run it.

The passphrase only protects the approver that sits in the agent's chosen home.

**Fix:**
1. **The approver lives at a fixed, env-independent location.**
   - `approver_home()` = `<pw_dir>/.togi`, where `pw_dir = pwd.getpwuid(os.getuid()).pw_dir`. Not
     `$HOME`, not `$TOGI_HOME`.
   - The passphrase record, the attempt state and a new **approver key** (`approver.key`, 32 random
     bytes, 0600, created by `togi approver setup` when missing) live there.
2. **Setup.** `togi approver setup` refuses to overwrite an existing approver unless the current
   passphrase is entered first (rotation).
3. **Grants are signed.**
   - When a person grants (or denies) an approval, write
     `grant_mac = HMAC-SHA256(approver.key, canonical{id, bundle, command, args_hash, binding_hash, status, expires, decided_by, scope})`.
   - `check_for_use` verifies `grant_mac` with the key from the approver home and refuses
     (`approval_unsigned`) otherwise.
   - A grant record copied or forged under another `$TOGI_HOME` therefore fails.
4. **Tests and stone need a throwaway approver:** allow `TOGI_APPROVER_HOME` to override the location.
   - Every grant made under an override carries `scope: "test"`; grants under the fixed home carry
     `scope: "default"`.
   - Togi passes the scope to the command as `ctx.approval_scope` ("default" | "test"). The Context gains
     that attribute; it is None when there was no approval.
   - Document in SPEC §2 and the README: **a bundle that moves real value must refuse a `test`-scope
     approval outside its own test mode.**
   - `togi doctor` warns when `TOGI_APPROVER_HOME` is set.
5. Update the test fixtures (conftest and any helper that builds an approver) to set
   `TOGI_APPROVER_HOME` to the throwaway home.
6. **Regression tests:**
   - The attack above (own `TOGI_HOME` + own approver via a pty) cannot run a gated command in a way that
     yields `approval_scope == "default"`.
   - A forged grant record without a valid MAC is refused.
   - A grant copied between homes is refused.
   - Setup refuses to overwrite.
   - Scope is passed through the Context on the CLI, run and MCP surfaces.

## B. `togi apply` runs proposed code before a person decides (medium)

**The hole.** For reshape proposals, apply's evaluation copies the repo, applies the diff and runs the
stone, which executes the candidate code before any human has looked at it.

**Fix:**
1. A proposal whose tier is reshape cannot be evaluated until a person passes the human gate
   (terminal + typed word + passphrase) for **evaluation**. That approval is recorded in the proposal meta
   and the audit log.
2. After evaluation, applying still needs the human decision, as today.
3. Polish proposals (non-code files only) keep automatic evaluation.
4. Belt and braces: before evaluating any proposal, re-check that every touched file is outside
   code-bearing extensions (`.py`, `.pyc`, `.so`, `.sh`, `.js`, `.ts`) unless the tier is reshape and
   evaluation was approved.
5. Regression tests: reshape evaluation refuses without the gate; polish still auto-evaluates; a "polish"
   proposal touching a `.py` file is refused.

## Constraints
- Keep `python3 -m pytest -q` green; it was 365 tests at this point. This round changes Togi only: the
  Crowd-side change (refusing test-scope approvals outside fake mode) is made separately, in the Crowd
  Price repository.
- Never create an approver in the real `<pw_dir>/.togi`. Use `TOGI_APPROVER_HOME` everywhere in tests,
  and assert no test writes to the real one.
- No `claude -p`. No broad `pkill`. Do not commit.
