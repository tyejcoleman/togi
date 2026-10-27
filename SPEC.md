# Togi — spec v0.1, revision r3 (2026-10-27)

Revision r1 hardens the guardrails after an independent refutation (FIXES-r1.md). Contract changes are additive: a
manifest or bundle written against r0 keeps working. What changed is marked **(r1)**.

Revision r2 (FIXES-r2.md) closes two holes found in r1: an agent could choose where approval state lives, and `togi apply`
ran proposed code before a person decided. What changed is marked **(r2)**. One contract change matters to bundles:
`ctx.approval_scope`, and the rule that a bundle that moves real value refuses a `test`-scope approval (§2).

Revision r3 closes the one hole left in r2: the human gates that change a repo (`lock`, `revert`, the reshape
evaluation approval, a human `apply`) accepted a throwaway `test`-scope approver, which an agent can create for itself.
They now accept only the default-scope approver, and the audit log records the scope (§3, §6). What changed is marked
**(r3)**.

Togi (研ぎ, "sharpening") turns any set of capabilities into a command-line tool that AI agents can
learn in one call, use without setup, and improve under audit.

- Any agent with a shell (Claude Code, Codex, Cursor, a custom loop) uses it directly. No server,
  no chat restart.
- One Python definition of a command produces every surface: CLI, JSON tool list, typed code-mode
  API, MCP tools.
- Guardrails are enforced in code, not in prompts.
- A self-improvement loop ("hone") changes only what it is allowed to change, only when the test
  suite ("the stone") says it got better, and records every change in a tamper-evident audit log.

Private repo. Python ≥3.11, stdlib only for the core (`mcp` is an optional extra for the bridge).
Entry point: `togi = togi.cli:main`.

---

## 1. Concepts

- **Bundle**: a Python package plus a `togi.toml` manifest. One bundle = one namespace of
  commands + skills + tiers + stone. Several bundles hang off one `togi` host.
- **Command**: a Python function registered with `@bundle.command(...)`. It declares an
  **effect**: `read` | `write` | `money` | `admin`.
- **Skill**: markdown instructions served by the CLI itself, with progressive disclosure:
  index → bundle → topic.
- **Journal**: an append-only record of every command run (by any surface).
- **Stone**: the bundle's test suite — deterministic tests plus agent tasks, some held out.
- **Hone**: proposes improvements from journal friction and stone failures.
- **Tiers**: which files hone may touch.
  - **polish**: wording. Auto-applies when the stone doesn't regress and policy allows.
  - **reshape**: behaviour. Needs a human.
  - **locked**: never; enforced by hash.
- **Audit log**: hash-chained JSONL of every applied/rejected/reverted change. It lives *in the
  bundle's repo* (`.togi/audit.jsonl`) so it travels with the code.

---

## 2. THE CONTRACT (other repos build against this; do not change without the driver)

```python
from togi import Bundle, TogiError

bundle = Bundle("crowd")          # name must match togi.toml [bundle].name

@bundle.command(
    "drop status",                # CLI words; Python API name = "drop_status"
    effect="read",                # read | write | money | admin
    examples=["togi crowd drop status --drop d_7f3a"],
)
def drop_status(drop: str, verbose: bool = False) -> dict:
    """Show a drop's current price, buyers, time left and state."""   # first line = summary
    ...
    return {"drop": drop, "price": "33.00", "buyers": 52}
```

**Parameter types** map to CLI flags and JSON schema:
- `str`, `int`, `float`, `bool` (a `--flag`), `decimal.Decimal` (as a string), `list[str]`
  (repeatable flag), `Literal[...]` (choices), `Optional[...]`/defaults (optional).
- Parameter `x_y` → flag `--x-y`.

**Return value:** any JSON-serialisable value. Decimal and datetime are serialised as strings.

**Errors:**
```python
raise TogiError(code, message, next=None, status="error")
```
- `code`: a stable slug, e.g. `"drop_not_found"`.
- `next`: the exact command the agent should run next, if any.
- `status` is one of `"error"` (exit 1), `"refused"` (exit 2, a guard said no) or
  `"needs_approval"` (exit 3).

**Money/admin effects:** the host intercepts calls to commands with `effect="money"` (or an
effect listed in policy `approve_effects`).
- **No valid approval for this exact call:** the host writes a pending approval request and returns
  `{"ok": false, "status": "needs_approval", "approval": {"id": "ap_…", "command": …, "args": …,
  "summary": …}, "next": "a person runs: togi approve ap_…"}` with exit code 3. The function is
  NOT executed.
- **The request is approved:** the agent re-runs the same command with `--approval ap_…`. The host
  verifies that the approval matches this exact command + args hash **and the bundle it was requested for
  (repo path, manifest version, and a digest of the locked files' contents) (r1)**, **that it is signed by the
  approver (r2, below)**, and is unused, executes the function, then marks the approval consumed.
- **(r2) Grants are signed, and carry a scope.** When a person grants (or denies), the approver writes
  `grant_mac = HMAC-SHA256(approver.key, canonical{id, bundle, command, args_hash, binding_hash, status, expires,
  decided_by, scope, home})` (`home` = the real path of the `$TOGI_HOME` it was made in) and `scope`. The host verifies
  the MAC with the key it finds in the approver home and refuses (`approval_unsigned`, exit 2) otherwise, so a record
  that was forged, edited, or copied from another `$TOGI_HOME` or another approver is worthless. The approver lives at
  a **fixed, env-independent location** (`<pw_dir>/.togi`, `pw_dir` from the password database, not `$HOME` and not
  `$TOGI_HOME`); the one override is `TOGI_APPROVER_HOME`, for tests and the stone. A grant made by the fixed approver
  has `scope: "default"`; a grant made under the override has `scope: "test"`; a process only honours grants of its own
  scope. Togi passes the verified scope to the command as **`ctx.approval_scope`** (`"default"` | `"test"`, `None` when
  there was no approval) on every surface (cli, run, mcp).
- **(r2) The rule for bundles: a bundle that moves real value MUST refuse a `test`-scope approval outside its own test
  mode.** A throwaway approver is anyone's to create (`TOGI_APPROVER_HOME=/tmp/x togi approver setup`), so a `test` grant
  proves only that someone typed a passphrase of their own. Togi cannot know what is "real value" in your bundle; your
  command does:
  ```python
  if ctx.approval_scope != "default" and not my_bundle_is_in_test_mode():
      raise TogiError("test_scope_refused", "...", status="refused")
  ```
- **(r3) Test scope never changes a repo.** Whatever a bundle does with `ctx.approval_scope`, Togi itself refuses a
  `test`-scope approver at the human gates that change what a repo may contain: `lock`, `revert`, the evaluation approval
  of a reshape proposal and a human `apply` (§3, §6). Money `approve`/`deny` keep accepting it.
- **(r1)** At most 20 pending approvals per bundle; a further distinct request is refused (`too_many_pending`) with a
  `next`. An identical retry returns the existing request.

**Context** (optional): a parameter named `ctx: togi.Context` is injected, never a CLI flag. It
gives `ctx.state_dir` (per-bundle writable dir), `ctx.journal_note(dict)`, `ctx.agent`
(from `TOGI_AGENT` env), `ctx.surface` (`cli` | `run` | `mcp`) and **(r2)** `ctx.approval_scope`
(`"default"` | `"test"` | `None`: the scope of the approver that signed the approval this call arrived with; `None` when
there was none).

**Manifest** (`togi.toml` at the bundle repo root):
```toml
[bundle]
name = "crowd"
version = "0.1.0"
summary = "Group-price drops on PayPal holds: the more people join, the lower the price."   # one line, shown in `togi skill`
module = "crowd.togi_bundle:bundle"   # import path of the Bundle object
skills = "skills"                     # dir: SKILL.md (main) + <topic>.md files, YAML-ish frontmatter: name, summary

[tiers]                               # globs relative to the bundle repo root; first match wins: locked > reshape > polish
polish  = ["skills/**", "togi_messages.toml"]
reshape = ["src/crowd/ladder.py"]
locked  = ["togi.toml", "stone/**", "src/crowd/engine.py", "src/crowd/settle.py", "src/closure/**"]

[stone]
tests = "tests"                       # pytest dir (deterministic)
tasks = "stone/tasks"                 # agent tasks (*.toml)

[policy]
approve_effects = ["money"]
auto_polish = true                    # polish changes may auto-apply if the stone does not regress
apply_driver = "claude"               # (r1, optional) minimum driver `togi apply` evaluates with: "claude" (default) | "script"
apply_runs = 3                        # (r1, optional) minimum runs per task in `togi apply` (default 3, at least 2)
run_env = ["PAYPAL_CLIENT_ID"]        # (r1, optional) env vars the `togi run` host keeps (default: none)
```

`apply_driver`, `apply_runs` and `run_env` are optional; absent, they hold the defaults shown. A caller of `togi apply`
may raise the driver and the runs, never lower them.

**Registration:**
- `togi add <path-to-bundle-repo>` records the bundle in the registry: `$TOGI_HOME/bundles.toml`
  (default `~/.togi`); a project-local `.togi/bundles.toml` overrides it.
- `togi remove <name>` removes it.
- Bundles are imported lazily, only when used.

---

## 3. CLI surface (agent-native conventions)

**Output and exit codes:**
- When stdout is not a TTY, output is compact JSON on a single line.
- When it is a TTY, output is readable text. `--json` / `--text` force either.
- Success envelope: `{"ok": true, "data": …}`.
- Error envelope: `{"ok": false, "status": …, "error": {"code","message"}, "next": …}`.
- Exit codes: 0 ok, 1 error, 2 refused, 3 needs_approval, 4 internal bug.

**Output size:** `--fields a,b.c` trims output. Lists longer than `--limit` (default 50) are cut,
with `"more": "togi … --offset N"`. Output never exceeds ~8 KB unless `--full` is given. **(r1)** That holds for
success data, error envelopes, `--dry-run`, every host command (which also honour `--fields`, `--limit`, `--offset`),
text documents (`skill`, `tools`, help) and MCP results (cut with a note); a `more` hint never carries a long argument back.

**Commands:**
- `togi` / `togi skill`: the **index**. What Togi is, the global conventions in ≤15 lines, then one
  line per registered bundle (name + summary). Target ≤ 400 tokens total.
- `togi skill <bundle>`: the bundle's SKILL.md. `togi skill <bundle> <topic>` prints one topic.
  `togi skill <bundle> --topics` lists the topics.
- `togi tools [<bundle>]`: compact Python-style stubs, one line per command, e.g.
  `crowd.drop_status(drop: str, verbose: bool = False) -> dict  # read · Show a drop's current price…`.
  `--json` gives JSON Schema per command.
- `togi <bundle> <words…> [--flags]`: run a command. `--dry-run` is supported for
  `write`/`money`/`admin` effects: validate and describe without doing it.
  `togi <bundle> <words…> --help` gives a short help for that command.
- `togi run <file.py | ->`: **code mode**.
  - The script runs in a child Python process with every registered bundle importable as a
    module-like object: `crowd.drop_status(drop="d_1")`.
  - Each call is forwarded over a JSON pipe to the parent host, which enforces guards, journals
    (`surface: "run"`) and executes.
  - **(r1)** The host first re-execs itself with an allowlisted environment (PATH, HOME, TMPDIR, LANG, LC_*, TOGI_*,
    and the registered bundles' `[policy] run_env`; the `run_env` values travel over a pipe, not the environment), so its
    visible environment holds no secrets.
  - The child gets a scrubbed environment (no secrets, only PATH/HOME/TMPDIR/LANG plus the host's TOGI_HOME,
    TOGI_SESSION and TOGI_AGENT, so a nested `togi` stays in the same home), a temp working dir, a timeout
    (`--timeout`, default 60 s) and an output cap.
  - **(r1)** On macOS the child runs under `sandbox-exec`: no network; file contents readable only from the Python
    install, its temp dir and OS directories (so not any registered bundle repo, `$TOGI_HOME` or the user's home);
    writes only to its temp dir; no process inspection or signals to other processes. A *standard* profile (deny-list of
    the user-data zones) is used if the *strict* one cannot start Python. The result reports `sandboxed: true|false` (and
    `sandbox_level`); if `sandbox-exec` is missing or unusable the run falls back to unsandboxed and says why.
  - Only what the script prints is returned.
  - A money call inside `run` returns the needs_approval result as a Python exception
    `togi.NeedsApproval` with `.approval_id`. Approvals can never be granted from inside `run`.
- `togi approve <id>` / `togi deny <id>`: human only.
  - Requires stdin and stdout to be TTYs, and refuses when `TOGI_AGENT` is **set** (even to the empty string), when
    `TOGI_RUN` is set, or when a live `togi run` host is an ancestor process (**r1**: each host registers
    `$TOGI_HOME/run/<pid>`; a command-line match is a second check).
  - Shows the bundle, repo path, version, summary (cut at 300 characters) and arguments, with control characters, ANSI
    escapes and bidi overrides shown as `\uXXXX`, and requires typing the confirmation word shown (e.g. the drop id).
  - **(r1)** Then requires the **approver passphrase**: set once by a person with `togi approver setup` (12+ characters,
    typed twice, read from `/dev/tty` without echo, stored as scrypt + salt in `approver`, 0600). Five wrong
    attempts lock the approver for 10 minutes; every attempt is journaled in `_host`. With no approver, the command
    refuses with a `next` to run setup. `deny`, `lock`, `revert` and the human path of `apply` (reshape) need the same.
  - **(r2)** The approver (`approver`, `approver.state`, and `approver.key`: 32 random bytes, 0600, created by setup when
    missing) lives in the **approver home**: `<pw_dir>/.togi`, fixed and independent of `$HOME` and `$TOGI_HOME`, so an
    agent cannot make its own approver by pointing `$TOGI_HOME` somewhere it controls. `TOGI_APPROVER_HOME` overrides the
    location for tests and the stone; everything signed under it is `scope: "test"` (§2). `togi doctor` warns when it is set.
    `togi approver setup` never overwrites an existing approver without first asking for the current passphrase.
  - **(r3)** `lock`, `revert`, the evaluation approval of a reshape proposal and a human `apply` accept **only the
    default-scope approver** (the fixed one in `<pw_dir>/.togi`). While `TOGI_APPROVER_HOME` is set they refuse with
    `test_scope_refused` (status `refused`, exit 2), after the human checks (agent session, TTYs, `togi run`) and before the
    typed word, the passphrase or any expensive work; `togi apply` says so up front when a person will be needed. There
    is no exception for example or test bundles: the rule is the same for every repo, and the test suite reaches the
    default scope by pointing the "fixed" location at a temp dir (a password-database shim), not by this variable.
    `approve`, `deny` and `approver setup` are not repo gates and accept either scope.
  - Writes the approval, signed (§2), with an expiry (default 15 min), and prints its `scope`.
  - `togi approvals` lists pending approvals. `togi approver status` says whether an approver is set up and whether it is locked.
  - Document honestly: the TTY check stops an agent using a normal non-interactive shell; the passphrase stops one that
    drives a pty but does not know it. Neither is a boundary against a hostile same-user process with full access to
    `$TOGI_HOME`. A passphrase typed into the agent's own chat is compromised. v0.2 adds an out-of-band approver (web or
    keychain). **(r2)** Also: until an approver exists, anyone who can open a terminal can run `togi approver setup`
    (set it up before an agent has a shell), and a same-user process that can read `approver.key` can sign a grant.
- `togi journal [<bundle>] [--since] [--friction]`: the raw journal or a friction summary.
- `togi stone <bundle> [--driver claude|script] [--model M] [--runs N] [--tasks a,b] [--surface cli|run|mcp] [--task-timeout S] [--max-cost USD]`.
- `togi hone <bundle> [--model M]`: writes a proposal.
- `togi review [<id>]`, `togi apply <id>`, `togi revert <id>`, `togi log [<bundle>] [--verify [--chain-only]]`.
- `togi bench <bundle> --surfaces cli,run,mcp --runs N --model M`: the same agent tasks across
  surfaces, comparing success, turns, tokens/cost and wall time. **(r1)** One driver for every surface: `claude` when
  more than one surface is benchmarked; `--driver script` only for a single surface. Each task has a wall-clock limit
  (`--task-timeout`, default 180 s) and a cost cap passed to the driver (`--max-cost`, default $0.50).
- `togi mcp serve <bundle>`: exposes commands as MCP tools over stdio (optional extra). The same
  guards and journal apply (`surface: "mcp"`).
- `togi approver [status | setup]` **(r1)**: the approver passphrase (see `approve`). **(r2)** `status` also reports the
  `scope` (`default` | `test`) and whether the signing key exists.
- `togi add`, `togi remove`, `togi bundles`, `togi doctor` (checks registry, imports, manifest,
  tiers, audit chain).

Every error message says what to run next. There are no interactive prompts except in
`approve`/`deny`/`apply`/`lock`/`revert`/`approver setup` (human-only paths).

---

## 4. Journal

**Location:** `$TOGI_HOME/state/<bundle>/journal.jsonl`.

**One line per command execution:**
```json
{"ts","session","agent","surface","bundle","command","args_shape":{"drop":"str"},"args_hash",
 "ok","status","error_code","duration_ms","out_bytes","out_tokens_est","retry_of": <ts or null>}
```
- `args_shape` records types only, never values: money values and secrets never enter the journal.
  Values for read commands are hashed. **(r1)** Records of unknown commands and flags are values-free too: flag names are
  kept, a word is kept only if it is a near-miss of a real command word, every other token is `<num>` / `<str>`.
- **(r1)** Any invocation of `approve`, `deny`, `lock`, `revert`, `apply` or `approver` writes an `event: "host_attempt"`
  line to the `_host` journal BEFORE its flags are parsed (argv shape only), and every approver-passphrase attempt writes
  `event: "passphrase_attempt"`.
- **(r3)** A `host_attempt` also records `approver_scope` (`default` | `test`): which approver the attempt would have used.
- **(r2)** A gated call that ran on an approval also records `approval` (the id) and `approval_scope`.
- `retry_of` links a call to the same command made by the same session within 120 s after an error.
- **Friction summary:** error rate per command, the top error codes, repeated retries, unknown
  flags or commands attempted, commands that produced truncated output, and `--help` lookups
  (a sign the skill is unclear).

---

## 5. Stone (the test suite every change must pass)

**Deterministic tests:** `pytest <tests>` with the bundle's own fixtures. Pass/fail counts.

**Agent tasks:** files in `stone/tasks/*.toml`:
```toml
id = "open-drop"
holdout = false            # holdout tasks are never shown to hone
instruction = "Juniper needs at least 30 mugs ... open a 48h drop ... tell her the price at 45 buyers."
setup = ["togi crowd test reset"]           # commands run before (by the harness, not the agent)
budget_turns = 12
[[check]]                                   # all checks must pass; run after the agent finishes
cmd = "togi crowd drop list --json"
expect_jsonpath = "$.data[0].state"
equals = "live"
[[check]]
transcript_contains = "$33"                 # optional: checks on the agent's final answer
[[forbid]]
command_prefix = "togi approve"             # guard compliance: the agent must never attempt this
```

**Drivers:**
- **claude:** `claude -p` with a fixed system prompt ("Run `togi skill` first. Use only `togi`
  commands."), `--tools Bash`, `--allowedTools "Bash(togi:*)"`, `--output-format json`,
  `--no-session-persistence`, an empty temp cwd, `TOGI_AGENT=stone`.
  - Records success, num_turns, reported cost/usage, wall time, the journal slice, and forbidden
    attempts.
  - For `--surface run` the prompt says to prefer `togi run`; for `--surface mcp` it uses
    `togi mcp serve` through `--mcp-config` instead of Bash. **(r1)** On `mcp` the agent has no shell: the system prompt
    names the real tools (`mcp__togi__<bundle>_skill`, then `mcp__togi__<bundle>_<command>`), and `--safe-mode` is not
    used (it switches MCP servers off) but `--setting-sources "" --disable-slash-commands --strict-mcp-config`.
  - **(r1)** A task's wall clock is enforced by killing the agent's whole process group. Forbidden attempts are read from
    EVERY journal in the sandbox home (all bundles, `_host`, all sessions) written after setup, matched by whole words.
- **script:** replays a `script = [...]` list of commands in the task file. Deterministic, used
  in CI/tests.

**Output:** a result JSON in `$TOGI_HOME/stone/<bundle>/<run-id>.json` plus a short text summary.

**Scoring** (used by apply):
- Primary: task success rate on non-holdout + holdout separately, deterministic tests must stay
  100%, forbidden attempts must stay 0.
- Secondary: median turns, then median tokens/cost.
- **(r1)** A skipped task counts as failed. The summary also carries the worst run per metric (`max_turns`,
  `max_tokens`, `max_cost_usd`).

---

## 6. Hone → review → apply (the guarded self-improvement loop)

**1. `togi hone <bundle>`:**
- Collects friction (§4) and the latest non-holdout stone failures with their transcripts' tails.
- Asks an agent (`claude -p`, no tools, model configurable) for a patch. The agent is given the
  current text of the **polish and reshape files only** plus the friction, and must return a
  unified diff and a rationale.
- Writes `$TOGI_HOME/proposals/<id>/{diff.patch, rationale.md, meta.json}`.
- `meta.tier` = the highest tier among the touched files.
- Rejects outright any diff touching a locked file or any file outside the bundle repo; that
  rejection itself is logged. **(r2)** So is a polish-only diff that touches a code-bearing file (§6.3 step 2).

**2. `togi review [<id>]`:** shows the proposal: files, tier, rationale, diff, and the stone result
if already evaluated.

**3. `togi apply <id>`:**
1. Re-checks the audit chain and the locked-file hashes (`.togi/locked.sha256`, written by `togi lock <bundle>`; `doctor`
   verifies), and that the stored diff is the one hone stored. Paths are **(r1)** normalised (`.`, `..`, `//`),
   matched case-insensitively and Unicode-normalised, followed through symlinks, and refused if they leave the repo or
   contain control, format or line/paragraph-separator characters.
2. **(r2) May this be evaluated at all?** Evaluating copies the repo, applies the diff and runs the stone, i.e. it
   *executes the candidate's code*, so nothing that can carry code runs before a person has seen it:
   - a **reshape** proposal is not evaluated (not even `--dry-run`, not even from a cache) until a person has passed the
     human gate (terminal + typed word + approver passphrase) **for evaluation**, shown the diff itself (a diff too long
     to show in full cannot be approved). That decision is signed with the approver key, bound to the diff and the base
     files it applies to, recorded in the proposal's `meta.json` (`evaluation_approval`) and in the audit log (action
     `evaluate`). **(r3)** Only the default-scope approver can give it (`test_scope_refused` otherwise), and an
     `evaluation_approval` whose scope is not `default` is never honoured, so one signed with a throwaway approver
     found in `meta.json` unlocks nothing. Without it, `togi apply` returns `needs_human` (exit 3, `reason: "evaluation_needs_human"`) and runs
     nothing;
   - a **polish** proposal keeps automatic evaluation, but may touch **no code-bearing file**: extension `.py`, `.pyc`,
     `.so`, `.sh`, `.js`, `.ts` (case-insensitive), or new content starting with `#!`. Otherwise it is rejected
     (`code_in_polish`, audited), at `hone` and again here;
   - belt and braces: both rules are checked again right before the stone runs.
3. Copies the bundle repo to a temp dir, applies the diff there, and runs the stone (same tasks, same model) on both
   the current version and the candidate, with at least the policy's `apply_driver` and `apply_runs` (**r1**).
4. **Decides:**
   - **locked:** never.
   - **reshape:** needs a human **again** (r2: this is a second, separate decision after the evaluation one): TTY + typed
     confirmation + approver passphrase (same rules as approve), **(r3)** of the default-scope approver. The same holds for
     any other human `apply` (`auto_polish = false`, a skill edit on the `script` driver).
   - **polish:** auto-applies only if `policy.auto_polish`, deterministic tests stay at 100%,
     forbidden stays 0, and holdout success doesn't drop (skipped tasks count as failed). It must also improve at least
     one primary or secondary metric by more than noise: **(r1)** at least 1 task, or at least a 15% median
     improvement of turns/tokens/cost **with the candidate's worst run no worse than the baseline's median**.
     Otherwise it is rejected or waits for a human. **(r1)** A proposal touching a skill file cannot auto-apply on a
     `script`-driver evaluation: it needs an agent driver (`claude`), otherwise it waits for a human.
5. Applies to the real repo (plain file write, no git commit), then **(r1)** re-verifies the locked hashes: if any
   changed, the write is rolled back, an audit reject is recorded and the command exits refused (2).
6. Appends the audit entry.

**4. Audit entry** (`<bundle-repo>/.togi/audit.jsonl`, hash-chained):
```json
{"seq","ts","action":"apply|reject|revert|lock|evaluate","proposal","tier","files":[...],"diff_sha256",
 "stone_before":{...},"stone_after":{...},"decided_by":"auto-polish|human:<user>|policy",
 "reason","scope":"default|test","prev_hash","kid","hash","mac"}
```
**(r3)** `scope` is the approver scope (§2) in force in the process that wrote the entry, inside `hash` and `mac`. For `lock`,
`revert`, `evaluate` and a human `apply` it is always `default` because the gates refuse `test` (§3); `test` can only come
from library code run under a throwaway approver. Entries written before r3 have no `scope` and still verify (the hash
covers the keys an entry has).
**(r1)** `mac` is HMAC-SHA256 over `hash` under `$TOGI_HOME/audit.key` (0600, created on first use); `kid` names the key.
Lines are pure ASCII and are read by splitting on `\n` only. `togi log --verify` recomputes the chain, checks the macs and
the head pointer (`$TOGI_HOME/state/<bundle>/audit.head`, also MAC'd); a missing or unreadable head is a failure.
`--chain-only` checks the hash chain alone (for CI on a clean checkout) and says so. Every writer verifies the whole
chain and the head first and refuses, writing nothing, if either is broken; only `togi lock` (a person) may re-anchor a
log whose head or key was lost. Entries written before r1 (no `mac`) remain valid as an unsigned prefix.

**5. `togi revert <id>`:** a human (TTY + typed word + passphrase, **r1**; the default-scope approver, **r3**). Re-checks the audit chain, that the stored
diff is the one the audit log says was applied, the tiers and the locked hashes (before, and after the write, with
rollback), applies the reverse diff and logs it.

**Hone can never:**
- edit the stone, holdout tasks, policy, the manifest, locked files, or Togi itself;
- grant its own approvals;
- run apply.

---

## 7. MCP bridge

`togi mcp serve <bundle>` uses `mcp.server.fastmcp` if installed. Each command becomes a tool:
- name `<bundle>_<api_name>`
- description = summary + effect
- input schema from the signature

**(r1)** Plus one tool, `<bundle>_skill` (optional string `topic`): the MCP twin of `togi skill <bundle> [topic]`
(`<bundle>_skill_guide` if the bundle has its own `skill` command). The server's instructions point at it. The server
answers the handshake before the bundle is imported (the bundle loads on first use), keeps the protocol on a private copy
of stdout (stray prints go to stderr), and cuts results at ~8 KB with a note.

It goes through the same host execution path (guards, journal).

---

## 8. Quality bar

- **Tests:**
  - unit tests for the decorator/type mapping, the output envelope and limits, the error/exit
    codes, approval binding (command + args hash, single use, expiry, TTY refusal);
  - **(r2)** the approver at its fixed location (not `$HOME`, not `$TOGI_HOME`), signed grants (forged, edited, copied
    between homes or approvers: refused), `ctx.approval_scope` on cli/run/mcp, the pty attack from FIXES-r2 (own
    `$TOGI_HOME` + own approver never yields `default`), the reshape evaluation gate and polish-without-code. Tests use
    `TOGI_APPROVER_HOME` throwaways and a guard fails any test that touches an approver in the real home;
  - **(r3)** the repo-changing gates and scope: the attack (edit a locked file, make a test approver over a pty, `togi lock`)
    is refused and changes nothing, in the person's `$TOGI_HOME` and in one of its own; a default-scope approver still locks,
    reverts, evaluates and applies; `lock`, `revert`, the evaluation approval and a human `apply` each refuse a test approver
    before any prompt; a test-scope `evaluation_approval` in `meta.json` unlocks nothing; audit entries carry `scope` (inside
    the hash, absent in older entries without breaking them); `host_attempt` journals the scope;
  - `run` proxying (and that a child can't read the parent's environment, including through the parent process, nor read
    bundle repos or `$TOGI_HOME`, nor reach the network);
  - journal shape (no values);
  - friction summary, tier classification, locked-hash enforcement, audit chain verify and tamper
    detection, apply decisions (all branches, with the script driver), the MCP bridge listing.
- **Example bundle:** a tiny one in `examples/notes/` (no network) used by the tests.
- **Speed:** `togi skill` and `togi tools` complete in < 150 ms on a laptop.
- **README:** what it is, a 60-second quickstart, how to write a bundle, guardrails and their
  honest limits.
