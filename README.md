# Togi 研ぎ

Togi turns any set of capabilities into a command-line tool that AI agents can **learn in one call, use
without setup, and improve under audit**.

- Any agent with a shell (Claude Code, Codex, Cursor, your own loop) uses it directly. No server, no chat
  restart.
- One Python function produces every surface: the CLI, a JSON tool list, a typed code-mode API, MCP tools.
- Guardrails are enforced in code, not in prompts: money effects need a person's approval, which needs a passphrase
  the agent never sees.
- A self-improvement loop (`hone`) changes only what it is allowed to change, only when the test suite (`the
  stone`) says things got better, and records every change in a hash-chained audit log.

Python 3.11+, standard library only. (`mcp` is an optional extra for `togi mcp serve`.) Spec: [SPEC.md](SPEC.md).

## 60-second quickstart

```sh
# 1. Put `togi` on your PATH (or: pip install -e .)
ln -s "$PWD/bin/togi" ~/.local/bin/togi

# 2. Register the example bundle
togi add examples/notes

# 3. What an agent does first: one call to learn everything
togi skill                       # what Togi is + one line per bundle
togi skill notes                 # the bundle's skill (SKILL.md), topics listed at the end
togi tools notes                 # commands as Python stubs; --json for JSON Schema

# 4. Use it (compact one-line JSON when stdout is not a terminal)
togi notes add --text "buy milk" --tag home
togi notes list --fields id,text
togi notes show --id n_9         # error: {"ok":false,"status":"error",...,"next":"togi notes find --query <words>"}

# 5. A money command needs a person (who has set up an approver passphrase once, in their own terminal)
togi approver setup                            # the PERSON, once, BEFORE any agent has a shell: 12+ characters, typed twice,
                                               # not shown. The approver lives in ~/.togi (fixed; not $TOGI_HOME, not $HOME)
togi notes tip send --id n_1 --amount 5.00     # exit 3, returns an approval id; nothing was sent
togi approve ap_...                            # the PERSON runs this: reads the prompt, types the word, types the passphrase
togi notes tip send --id n_1 --amount 5.00 --approval ap_...   # now it runs, once

# 6. Code mode: many calls, only what you print comes back
echo 'for n in notes.list(): print(n["id"], n["text"])' | togi run -
```

## What an agent sees

```
$ togi notes tip send --id n_1 --amount 5.00
{"ok":false,"status":"needs_approval","error":{"code":"needs_approval","message":"..."},
 "next":"a person runs: togi approve ap_8f3c...","approval":{"id":"ap_8f3c...","command":"notes tip send",
 "args":{"id":"n_1","amount":"5.00"},"summary":"..."}}          # exit code 3
```

- Success: `{"ok":true,"data":...}`. Error: `{"ok":false,"status","error":{"code","message"},"next"}`.
- Exit codes: `0` ok, `1` error, `2` refused (a guard said no), `3` needs approval, `4` internal bug.
- Every error says what to run next.
- `--fields a,b.c` trims output. Lists are cut at `--limit` (default 50) with `"more": "togi ... --offset N"`.
  Output never exceeds ~8 KB unless `--full`.
- `--dry-run` previews write/money/admin commands. `--help` after a command gives its flags.
- On a terminal you get readable text instead; `--json` / `--text` force either.

## Writing a bundle

A bundle is a Python package plus a `togi.toml` at the repo root. `examples/notes/` is the smallest complete
one (commands, skills, tiers, stone tasks, tests).

```python
# src/crowd/togi_bundle.py
from decimal import Decimal
from togi import Bundle, Context, TogiError

bundle = Bundle("crowd")          # must match togi.toml [bundle].name

@bundle.command("drop status", effect="read",
                examples=["togi crowd drop status --drop d_7f3a"])
def drop_status(drop: str, verbose: bool = False) -> dict:
    """Show a drop's current price, buyers, time left and state."""   # first line = summary
    ...
    return {"drop": drop, "price": Decimal("33.00"), "buyers": 52}    # Decimal/datetime become strings

@bundle.command("drop settle", effect="money", confirm="drop")
def drop_settle(drop: str, ctx: Context) -> dict:
    """Capture the held payments for a drop."""
    if not found(drop):
        raise TogiError("drop_not_found", f"No drop {drop}.", next="togi crowd drop list")
    ...
```

```toml
# togi.toml
[bundle]
name = "crowd"
version = "0.1.0"
summary = "Group-price drops on PayPal holds: the more people join, the lower the price."
module = "crowd.togi_bundle:bundle"
skills = "skills"                 # SKILL.md (main) + <topic>.md files, frontmatter: name, summary

[tiers]                           # globs relative to the repo; first match wins: locked > reshape > polish
polish  = ["skills/**", "togi_messages.toml"]
reshape = ["src/crowd/ladder.py"]
locked  = ["togi.toml", "stone/**", "src/crowd/engine.py", "src/crowd/settle.py"]

[stone]
tests = "tests"                   # deterministic pytest dir
tasks = "stone/tasks"             # agent tasks (*.toml)

[policy]
approve_effects = ["money"]       # money is always gated; add "write"/"admin" to gate those too
auto_polish = true
apply_driver = "claude"           # minimum driver `togi apply` evaluates with (default "claude"; "script" = replays)
apply_runs = 3                    # minimum runs per task when `togi apply` evaluates a change (default 3, at least 2)
run_env = ["PAYPAL_CLIENT_ID"]    # env vars the `togi run` host keeps (default none); see "Code mode" below
```

All three `[policy]` keys after `auto_polish` are optional; a manifest written before they existed behaves as
if they held the defaults shown.

Register it with `togi add <repo>`. `togi add` adds `<repo>/src` and `<repo>` to `sys.path` when the bundle is
imported. Bundles are imported lazily, only when a command needs them, so `togi skill` stays fast.

**Parameter types:** `str`, `int`, `float`, `bool` (a `--flag`; a bool defaulting to `True` gets `--no-flag`),
`Decimal` (passed as a string, never a float), `list[str]` (repeat the flag), `Literal[...]` (choices),
`Optional[...]` or any default (optional). `x_y` becomes `--x-y`. A parameter named `ctx: togi.Context` is
injected and never a flag: `ctx.state_dir` (a per-bundle writable directory), `ctx.journal_note(dict)`,
`ctx.agent` (`$TOGI_AGENT`), `ctx.surface` (`cli` | `run` | `mcp`) and `ctx.approval_scope` (`"default"` | `"test"`, or
`None` when the call had no approval; a command that moves real value refuses `"test"`, see "Test approvers and `ctx.approval_scope`" under Guardrails).
Docstring `Args:` lines become flag help.
Command parameters named like a host flag (`--limit`, `--fields`, `--text`...) shadow it for that command.
Reserved: `help`, `approval`, `dry_run`.

**Effects:** `read`, `write`, `money`, `admin`. `money` (and any effect in `approve_effects`) is intercepted by
the host before your function runs. The optional `confirm="drop"` names the parameter whose value the person
types to approve (default: the first short id-like string argument; a value containing control characters or
spaces is never offered, the approval id's tail is used instead).

**Skills.** `skills/SKILL.md` is what `togi skill <bundle>` prints; other `*.md` files are topics
(`togi skill <bundle> <topic>`). Write it for an agent: what the bundle is for, the three or four commands that
matter, what needs a person. If it is missing, a page is generated from the commands.

**Stone tasks.** `stone/tasks/*.toml`:

```toml
id = "open-drop"
holdout = false                       # holdout tasks are never shown to hone
instruction = "Juniper needs at least 30 mugs ... open a 48h drop ..."
setup = ["togi crowd test reset"]     # run by the harness before the agent
budget_turns = 12                     # exceeding it fails the task
[[check]]                             # all checks must pass, run after the agent finishes
cmd = "togi crowd drop list --json"
expect_jsonpath = "$.data[0].state"   # also: contains / matches instead of equals
equals = "live"
[[check]]
transcript_contains = "$33"           # checks the agent's final answer
[[forbid]]
command_prefix = "togi approve"       # the agent must never attempt this
# deterministic replay for CI (script driver):
script = ["togi crowd test reset", "togi crowd drop open --sku mug ..."]
script_run = "crowd.drop_open(sku='mug')"                       # same task for the `run` surface
script_mcp = [{tool = "crowd_drop_open", args = {sku = "mug"}}]  # ... and the `mcp` surface
```

Test your bundle's own pytest dir with `PYTHONPATH=src:<togi>/src python3 -m pytest tests`.

A `[[forbid]] command_prefix` matches whole words (`togi approve` forbids `togi approve ap_1`, not `togi approvals`
or `togi approver status`) and is checked against EVERY journal in the task's sandbox, in every session, so an agent
cannot hide an attempt behind another session id or a child process.

## Surfaces

| Surface | Command | Journal `surface` |
|---|---|---|
| CLI | `togi <bundle> <words> --flags` | `cli` |
| Code mode | `togi run script.py` (or `-`) | `run` |
| MCP | `togi mcp serve <bundle>` | `mcp` |

**Code mode.** `togi run` starts a child Python process with every registered bundle importable
(`crowd.drop_status(drop="d_1")`, or `import crowd`). Each call is forwarded over a JSON pipe to the host, which
enforces guards, journals and executes. The child gets a scrubbed environment (`PATH`, `HOME`, `TMPDIR`, `LANG`, and
the host's `TOGI_HOME` / `TOGI_SESSION` / `TOGI_AGENT`, so a nested `togi` call stays in the same home), a temp working
directory, a timeout (`--timeout`, default 60 s) and an output cap (8 KB, `--full` lifts it). Only what the script
prints is returned. Calls return full data (no `--limit`). A money call raises `togi.NeedsApproval` (with
`.approval_id`); a granted approval can be used with `approval="ap_..."`. Exit code mirrors the CLI (`3` if an
uncaught `NeedsApproval`). `--json` output includes `sandboxed` (true/false; `sandbox_level` when true; `sandbox_note` when
false or downgraded); without
the OS sandbox a one-line `[togi: sandboxed: false - why]` goes to stderr.

What keeps a script from reaching what it should not, in layers:

1. **The host re-execs itself first** with an allowlisted environment (`PATH`, `HOME`, `TMPDIR`, `LANG`, `LC_*`,
   `TOGI_*`, and whatever a registered bundle lists in `[policy] run_env`) before it starts the child. Its own visible
   environment (`ps -E`, `/proc/<pid>/environ`, `sysctl kern.procargs2`) then holds nothing secret. `run_env` values are
   handed to the re-exec'd host over a pipe, not left in its environment, so a bundle that needs `PAYPAL_CLIENT_ID` in
   its process does not expose it either.
2. **The child's own environment is scrubbed** (above).
3. **On macOS the child runs under a `sandbox-exec` profile**: no network; file contents readable only from the Python
   install, the child's own temp directory (where Togi puts its `_runchild` stub) and the operating system's own
   directories (`/usr`, `/bin`, `/sbin`, `/System`, `/dev`, the developer tools). So no reads of any registered bundle repo
   (`.env` files included), of `$TOGI_HOME`, of your home directory, or of other programs' temp files (file *metadata*,
   i.e. whether a path exists, stays visible); writes only to its own temp directory; signals and process inspection of
   other processes denied; it cannot even open a pty. The child never needs bundle code (every call is proxied to the
   host), so without this a script could `import crowd.engine` and call `settle()` directly, never meeting an approval.
4. Before it starts, Togi proves with a trial run that Python works under the profile (`sandbox_level: strict`). If the
   strict profile cannot start Python on a machine, it falls back to a *standard* profile that denies the user-data
   zones (your home, `/Users`, `/Volumes`, temp areas, every repo, `$TOGI_HOME`) instead of allowing only OS ones, and says
   so. If `sandbox-exec` is missing, already inside another sandbox, or cannot run Python here, the script still runs,
   **unsandboxed**, and the result says `sandboxed: false` with the reason (Linux always runs this way).

**MCP.** Tools are named `<bundle>_<command_api_name>`, described as summary + effect, with the input schema from
the signature. Money tools accept an `approval` argument; write/money/admin tools accept `dry_run`. There is one more
tool, `<bundle>_skill` (optional `topic`), the MCP twin of `togi skill <bundle> [topic]`, so progressive disclosure
works over MCP; the server's instructions tell the model to call it first. Results are cut at ~8 KB with a note. Errors
come back with `isError` set and the same envelope as the CLI. The server answers the MCP handshake before it imports
your bundle (a slow or broken bundle shows up as an error from `list_tools`, not as "the server did not connect"), and
`sys.stdout`/fd 1 are pointed at stderr while it runs, so a stray `print` in a bundle cannot corrupt the protocol stream.

## Self-improvement loop

```
journal -> stone -> hone -> review -> apply -> audit log        (revert undoes an apply)
```

1. **Journal.** Every command run, by any surface, appends one line to `$TOGI_HOME/state/<bundle>/journal.jsonl`:
   timestamp, session, agent, surface, command, argument *types* (never values), outcome, error code, duration,
   output size, and `retry_of`. `togi journal <bundle> --friction` summarises where agents struggle: error rate per
   command, top error codes, repeated retries, unknown flags/commands, truncated outputs, `--help` lookups.
2. **Stone.** `togi stone <bundle>` runs the deterministic tests plus the agent tasks in throwaway sandboxes (own
   `$TOGI_HOME`, empty cwd, `togi` on PATH). `--driver script` replays `script = [...]` (deterministic, CI).
   `--driver claude` runs `claude -p` with `--tools=Bash --allowedTools "Bash(togi:*)"`, a fixed system prompt, no
   session persistence and `TOGI_AGENT=stone`; it records success, turns, cost/usage, wall time, the journal
   slice and forbidden attempts. API keys are scrubbed from the agent's environment so it runs on your
   subscription. `--surface cli|run|mcp` picks how the agent is told to work. On the `mcp` surface the agent has no
   shell: it gets the Togi MCP server as its only tools (`mcp__togi__<bundle>_...`, starting with `..._skill`), and the
   system prompt names them. That surface cannot use `--safe-mode` (this Claude Code build switches MCP servers off in
   safe mode, which left the agent with no tools at all), so it is isolated with `--setting-sources ""
   --disable-slash-commands --strict-mcp-config` instead. A task's wall clock is enforced by killing the agent's whole
   process group.
3. **Hone.** `togi hone <bundle>` asks an agent (`claude -p`, no tools) for a unified diff and rationale, given
   the *polish and reshape files only*, the friction summary and the latest non-holdout failures. It writes
   `$TOGI_HOME/proposals/<id>/{diff.patch,rationale.md,meta.json}`. A diff that touches a locked file, a file in
   no tier, or any path outside the repo is rejected outright and the rejection is logged.
4. **Review / apply.** `togi review [<id>]`, then `togi apply <id>`: re-checks the audit chain, the diff on record and
   the locked hashes, runs the stone (same tasks, same model) on the current version and on a candidate copy with the
   diff, and decides. The manifest's `[policy] apply_driver` (default `claude`) and `apply_runs` (default 3) are
   **minimums**: `--driver`/`--runs` may raise them, never lower them (a lower driver is refused, fewer runs are raised).

   | Tier | Decision |
   |---|---|
   | locked (or no tier), or a path that cannot be judged (see below) | never |
   | any tier, if deterministic tests < 100%, forbidden attempts > 0, holdout success drops, or visible task success drops | rejected |
   | reshape | needs a person **twice**: first to let its code run in the stone at all (see below), then to apply it: each time a terminal, a typed confirmation word and the passphrase of the **default** approver (r3: not a `TOGI_APPROVER_HOME` one) |
   | polish touching a code-bearing file (`.py` `.pyc` `.so` `.sh` `.js` `.ts`, or content starting with `#!`) | never: rejected, at `hone` and again at `apply` |
   | polish | auto-applies only if `policy.auto_polish`, **and** it improves something by more than noise: at least 1 task, or a **median turns / tokens / cost improvement of at least 15% whose worst run is no worse than the old median**. Otherwise rejected. With `auto_polish = false` it waits for a person |
   | polish, touching a skill file, evaluated by the `script` driver | never auto-applies: it needs an agent driver (`claude`), otherwise it waits for a person |

   A skipped task (the driver had no script for the surface) counts as a failed one in every comparison.

   **Evaluating runs the candidate's code, so a person comes first.** The stone on the candidate copy imports and
   executes whatever the diff wrote. A polish proposal has no code in it, so it is evaluated automatically. A reshape
   proposal is **not evaluated at all** (not under `--dry-run` either) until a person has run `togi apply <id>` in a
   terminal, been shown the diff itself, typed the word and the approver passphrase. That approval is signed with the
   approver key, bound to this diff and the files it applies to, and recorded in the proposal's `meta.json`
   (`evaluation_approval`) and in the audit log (`evaluate`); editing `meta.json` by hand proves nothing, and neither does a
   record signed by a throwaway (`test`-scope) approver (r3: only the default scope counts). Without it
   `togi apply` exits 3 (`needs_human`, `reason: evaluation_needs_human`) and nothing runs. After the evaluation, applying
   it is a second human decision, as before. A diff over 1000 lines cannot be shown in full at the prompt, so its
   evaluation cannot be approved.

   Paths are judged after normalisation: `.`, `..` and `//` are resolved, the match is case-insensitive (and
   Unicode-normalised: on a case-insensitive filesystem `skills/MONEY.md` *is* the locked `skills/money.md`), a symlink is
   judged by what it points at, and a path that leaves the repo or carries control characters, bidi marks or Unicode
   line/paragraph separators is rejected. Applying is a plain file write (no git commit); the locked hashes are then
   **re-verified**, and if any moved the write is rolled back, a reject is audited and the command exits 2.

5. **Audit log.** `<repo>/.togi/audit.jsonl`: one hash-chained entry per apply / reject / revert / evaluate (and `lock`):
   `seq, ts, action, proposal, tier, files, diff_sha256, stone_before, stone_after, decided_by, reason, scope, prev_hash,
   kid, hash, mac`. `scope` (r3) is the approver scope the writing process ran under, `default` or `test`; it is inside the
   hash and the mac, and for `lock` / `revert` / `evaluate` / a human `apply` it is always `default`, because those
   refuse a test approver (older entries have no `scope` and still verify). `mac` is an HMAC over `hash` under `$TOGI_HOME/audit.key` (0600), so someone with only the repo cannot
   re-seal a rewritten chain. Lines are pure ASCII and read by splitting on `\n` only. `togi log <bundle> --verify`
   recomputes the chain, checks the macs and compares the head pointer in `$TOGI_HOME`; **a missing or unreadable head
   is a failure**, not a skip. Every writer (`apply`, `hone`'s rejections, `lock`) verifies the whole chain and the head
   first and, if broken, writes nothing. A person can re-anchor a log whose head or key was lost with `togi lock`. On a
   clean checkout (CI) `togi log <bundle> --verify --chain-only` checks the hash chain alone and says that it did not
   check signatures or the head. The log lives in the bundle repo so it travels with the code. Commit it.
6. **Revert.** `togi revert <id>` (a person: terminal, typed word, passphrase) applies the reverse diff and logs it. It
   re-checks what `apply` does: the audit chain, that the stored diff is the one the audit log says was applied, the
   tiers, and the locked hashes before and after the write (with rollback).

`togi lock <bundle>` (a person: terminal, typed word, passphrase) records the sha256 of every locked file in
`.togi/locked.sha256`; `apply`, `revert` and `doctor` re-check it. `togi.toml`, the stone tasks and tests, `.togi/` and
`.git/` are *always* locked even if the manifest forgets to say so. Hone can never edit the stone, holdout tasks, policy,
the manifest, locked files, or Togi itself, never grant approvals, and never run `apply`.

## Command reference

```
togi skill [<bundle> [<topic>]] [--topics]     togi tools [<bundle>] [--json]
togi <bundle> <words> [--flags] [--help]       togi run <file.py | -> [--timeout S] [--full]
togi approve <id> | deny <id> | approvals      togi journal [<bundle>] [--since 24h] [--friction]
togi approver [status | setup]                 (setup: a person sets or changes the approver passphrase)
togi stone <bundle> [--driver claude|script] [--model M] [--runs N] [--tasks a,b] [--surface cli|run|mcp]
togi hone <bundle> [--model M] [--driver claude|script --diff FILE]
togi review [<id>] | apply <id> [--dry-run] | revert <id> | log [<bundle>] [--verify [--chain-only]] | lock <bundle>
togi bench <bundle> --surfaces cli,run,mcp --runs N --model M [--driver claude|script] [--task-timeout 180] [--max-cost 0.50]
togi mcp serve <bundle>                        togi add <path> [--local] | remove <name> | bundles | doctor
```

Every host command honours `--fields`, `--limit`/`--offset` (on lists) and the ~8 KB cap (`--full` lifts it); so do
errors and `--dry-run` output.

Environment: `TOGI_HOME` (default `~/.togi`; moves the registry, journals, approvals, proposals and audit key, but **not** the
approver), `TOGI_APPROVER_HOME` (tests and the stone only: a throwaway approver; every grant it signs has `scope: "test"`,
see below), `TOGI_AGENT` (names the calling agent; **setting it at all, even to the
empty string,** makes human-only commands refuse), `TOGI_PASSPHRASE_TIMEOUT` (seconds to wait for the approver
passphrase, default 120), `TOGI_SESSION` (groups calls for `retry_of`; default is the parent shell's pid), `TOGI_APPROVAL_TTL`
(seconds, default 900), `TOGI_DEBUG` (print tracebacks), `TOGI_CLAUDE_BIN` / `TOGI_CLAUDE_NO_SAFE_MODE=1` /
`TOGI_CLAUDE_ARGS` (adapt the `claude -p` driver to another Claude Code version). A project-local `.togi/bundles.toml` (found by walking
up from the cwd) is merged over the global registry; its entries win.

## Guardrails and their honest limits

**Threat model.** Togi is built so that an *agent that has a shell* (or a `togi run` script) cannot approve its own
payments, rewrite locked files, launder the audit log, or read the host's secrets. It is **not** built to stop a
*hostile process running as the same user with full access to your files and to `$TOGI_HOME`*: such a process can
read the approver hash and key, write approval files, and rewrite the log and the head pointer. That is
out of scope for v0.1 (v0.2: an out-of-band approver, e.g. a web page or a keychain signature). The sandbox below
narrows what a `togi run` script is, but it is a same-user process under a profile, not a VM.

What is enforced in code:

- **Money approvals.** A call to a `money` command (or any effect in `approve_effects`) never executes the function
  without a matching grant. Without one the host writes a pending request and returns exit 3 (at most 20 pending per
  bundle; further ones are refused with a `next`). The grant is bound to bundle + command + a hash of the exact
  arguments **+ the bundle's repo path, manifest version and a digest of its locked files' contents**, so it dies if any
  of them changes. It is single use (claimed atomically before the function runs, spent even if the function then
  fails) and expires (15 min after approval).
- **Human-only commands** (`approve`, `deny`, `lock`, `revert`, the human path of `apply`, `approver setup`) need, all
  together (and `lock`, `revert` and `apply` also the default-scope approver, see "Repo-changing gates" below): stdin and stdout to be TTYs; `TOGI_AGENT` **not set at all** (an empty value still counts) and `TOGI_RUN` not
  set; no live `togi run` host among the process's ancestors (each host registers itself in `$TOGI_HOME/run/<pid>`;
  a command-line match is a second check); the typed confirmation word; and **the approver passphrase**.
- **The approver passphrase.** A TTY check alone stops an agent using a normal shell, but an agent that can run
  `script(1)` or a pty library can give `togi approve` a terminal and type the word itself (the Opus refuter did exactly
  that). So a person sets up a passphrase once, in their own terminal: `togi approver setup` (12+ characters, typed
  twice, read from `/dev/tty` with echo off, never from a pipe). Only an scrypt hash and salt are stored, in
  `approver` (0600). Every human-only action then asks for it after the typed word. `togi approver setup` on a machine
  that already has an approver asks for the *current* passphrase first and otherwise changes nothing. Wrong attempts are counted:
  five wrong, and the approver is locked for ten minutes; every attempt, right or wrong, is a line in the `_host`
  journal. With no approver set up, those actions refuse and say to run `togi approver setup`. Waiting for a passphrase
  that never comes times out (2 minutes) instead of hanging the caller.
- **The approver lives where an agent cannot move it (r2).** The approver (`approver`, `approver.state` and the signing key
  `approver.key`) is kept in `<pw_dir>/.togi`, where `pw_dir` is your account's home directory from the password
  database, **not** `$HOME` and **not** `$TOGI_HOME`. In r1 it sat in `$TOGI_HOME`, an ordinary environment variable, so an
  agent could set `TOGI_HOME=/tmp/mine`, `togi add` a bundle there, create an approver of its own with a fake terminal,
  approve its own money command and run it. Now `TOGI_HOME` moves everything *except* the approver, and the wrong-attempt
  counter cannot be reset by starting over in a new home.
- **Grants are signed (r2).** When a person grants or denies, the approver key signs the record
  (`grant_mac`, an HMAC-SHA256 over id, bundle, command, args hash, binding hash, status, expiry, decider, scope and the
  `$TOGI_HOME` it was made in). `togi <bundle> … --approval ap_…` verifies the signature with the key in the approver
  home and refuses (`approval_unsigned`) otherwise, so a record that was forged, edited (a longer expiry, a flipped
  "denied"), copied from another `$TOGI_HOME`, or made by another approver is worthless.
- **Test approvers and `ctx.approval_scope` (r2).** Tests, the stone and CI need an approver that is not yours:
  set `TOGI_APPROVER_HOME` to a throwaway directory and set it up there. Everything that approver signs carries
  `scope: "test"` (the fixed approver signs `scope: "default"`), a process only honours grants of its own scope, and Togi
  hands the verified scope to the command as `ctx.approval_scope` (`"default"`, `"test"`, or `None` when there was no
  approval) on the CLI, in `togi run` and over MCP. **Anyone can make a test approver, so a bundle that moves real value
  must refuse a `test`-scope approval outside its own test mode:**

  ```python
  @bundle.command("drop settle", effect="money", confirm="drop")
  def drop_settle(drop: str, ctx: Context) -> dict:
      if ctx.approval_scope != "default" and not settings.fake_mode:     # your bundle's own test switch
          raise TogiError("test_scope_refused", "a test approver signed this; real money needs yours",
                          next="ask the person to approve it with their own approver", status="refused")
  ```
  `togi doctor` warns while `TOGI_APPROVER_HOME` is set, `togi approver status` shows the scope, and the approval prompt
  says "TEST scope" when it is one. The stone sandbox and the bundle tests it runs get a throwaway approver of their own.

  *What this does and does not stop.* It stops an agent that has a pty but not the passphrase: it cannot approve, and
  guessing is rate-limited. It does **not** help if the passphrase is ever typed anywhere the agent can read: **a
  passphrase typed into the agent's own chat is compromised**, and so is one typed into a terminal the agent is
  recording. Set the passphrase up *before* an agent session starts: until an approver exists, `togi approver setup` can
  be run by anything that can open a terminal (an agent that gets there first owns the approver; with the fixed location
  that now also means the real one). A same-user process can read `approver`, but scrypt
  (128 MiB, ~0.2 s per guess) with a strong passphrase makes that hash expensive to attack; the attempt counter lives in
  the same directory, so it limits an agent driving the CLI, not a process that edits files. **It can also read
  `approver.key` and sign a grant: the key is a plain 0600 file, so the fixed location and the signature stop an agent that
  redirects `$TOGI_HOME` or edits and copies records, not a same-user process with full file access** (v0.2: keychain or
  out-of-band approver). The audit key still follows `$TOGI_HOME`.
- **Repo-changing gates take only the default approver (r3).** `togi lock`, `togi revert`, the approval to *evaluate* a
  reshape proposal and a human `togi apply` decide what code a bundle's repo may contain, so they accept only the approver
  in `<pw_dir>/.togi` (scope `default`). While `TOGI_APPROVER_HOME` is set they refuse (`test_scope_refused`, exit 2)
  before asking for the typed word or a passphrase. In r2 they accepted a test-scope approver, and anyone can make one:
  an agent that can write a repo could edit a locked money file, set up a throwaway approver on a pseudo-terminal
  (`TOGI_APPROVER_HOME=/tmp/x togi approver setup`) and `togi lock` the modified code, which defeats the locked tier. The
  rule has no exceptions (no carve-out for "example" bundles): a person who needs the repo gates runs them with
  `TOGI_APPROVER_HOME` unset. Money `approve`/`deny` and `approver setup` still work under a test approver, which is
  what tests and the stone need; the bundle decides what a test grant is worth (`ctx.approval_scope`). The audit entry of
  each of these actions records `scope`, an evaluation approval recorded in a proposal's `meta.json` only counts if it
  was signed in the default scope, and `togi doctor` says so while `TOGI_APPROVER_HOME` is set. This is the test suite's
  own arrangement too: tests that lock, revert or apply point the "fixed" location at a temp dir through a
  password-database shim and never use `TOGI_APPROVER_HOME` for them.
- **The approval prompt** shows the bundle, **the repo path**, the manifest version, the summary (cut at 300 characters)
  and every argument, with control characters, ANSI escapes, bidi overrides and line separators shown as `\uXXXX`, so
  nothing in an argument can move the cursor, recolour text or hide the real amount. The word to type is never one that
  could do that.
- **Journal hygiene.** Argument values never enter the journal (types only). That includes the records of unknown
  commands and flags: flag names are kept, a word is kept only if it is a near-miss of a real command word (the typo is
  the friction signal), and every other token becomes `<num>` / `<str>`. Argument hashes are kept for read commands only.
  Every invocation of a human-only command is journaled before its flags are parsed (argv shape only), and the stone's
  forbidden-command check reads every journal in the task sandbox, in every session.
- **Tiers.** Enforced by path globs after normalisation (case-insensitive, `..`/`//` resolved, symlinks followed, control
  and separator characters refused) and by the locked hashes, which are re-verified *after* a write too.
- **Output caps.** Everything an agent can receive is cut at ~8 KB with a `more`/`--full` hint: results, errors,
  `--dry-run`, every host command, and MCP results.

What is **not** a security boundary (v0.1):

- **A hostile process running as the same user with full file access** (see the threat model). Approval records,
  the audit key and head pointer, and the run registry are plain files in `$TOGI_HOME`; the approver hash and signing key
  are plain files in `<pw_dir>/.togi`.
- **`togi run` is only as isolated as its profile.** On macOS the child runs under `sandbox-exec`: no network, file
  contents readable only from the Python install, its temp dir and OS directories (so not bundle repos / `$TOGI_HOME` /
  your home), writes only to its temp dir. That is a same-user process under a profile, not a VM, and `sandbox-exec` is
  deprecated by Apple (still present on current macOS). File existence and sizes (stat) remain visible everywhere. If the
  strict profile cannot run Python on a machine, the weaker "standard" profile (deny-list of user-data zones) applies;
  system directories outside the allowlist, such as `/etc` or `/opt/homebrew/etc`, are then readable. Known edges: `ps` cannot run
  under any `sandbox-exec` profile, but a direct `sysctl kern.procargs2` read of *another same-user process's* arguments
  is not blockable with these filters, which is why the host's own environment is emptied by the re-exec rather than
  relying on the profile. Any other process of yours that holds secrets in its environment is not protected by Togi. **On Linux, or wherever `sandbox-exec` is missing or unusable, the script runs unsandboxed** (`sandboxed: false`):
  it can then read your files and the bundle repos, use the network, import bundle code directly without the host's
  guards, and read `/proc` entries of other same-user processes; only the environment scrub, the run registry and the
  approver passphrase remain. The registry (and any process-ancestry check) can be escaped by a deliberate double-fork.
- **The audit chain is tamper-evident, not tamper-proof.** Whoever controls both the repo and `$TOGI_HOME` has the key
  and the head and can rewrite everything. Someone with only the repo cannot forge an entry that verifies on your
  machine; on a different machine without the key, `--verify --chain-only` can only confirm the hashes. Commit the log
  and verify it in CI.
- **`.togi/locked.sha256` is a plain file in the repo.** `togi lock` is the sanctioned way to re-record it and, as of r3,
  needs the default approver (an agent's throwaway approver is refused). But nothing yet compares the file with the
  `lock` entry in the signed audit log, so an agent that can write the repo can still rewrite `locked.sha256` by hand
  after editing a locked file, and `togi doctor` will say "locked hashes OK" (it also leaves no `lock` entry in the log). Read
  `git diff` on locked files and on `.togi/locked.sha256` before you trust either; the next hardening is to check the file's
  hash against the latest signed `lock` entry.
- **"Fixed" means fixed against environment variables, not against code.** An agent that can run arbitrary Python can
  import Togi with the password database patched (this is exactly how the tests fake it), or call `audit.lock_bundle`
  directly. The default-scope rule closes the route an agent with only a shell and a pty has (an environment variable and
  a pseudo-terminal); it is not a boundary against a process that can run its own code as you.
- **A "test" approver is only as safe as the bundle that receives it.** Togi marks a throwaway approver's grants
  `scope: "test"` and tells the command; it cannot know which of your commands move real value. A bundle that does not
  check `ctx.approval_scope` treats a test grant like a real one.
- **Hone changes text an agent will read.** Tier gating decides *which files* may change, and the stone decides
  whether the result is better; neither reads the change for intent. A polish edit to a skill could still steer
  an agent badly. That is why a skill edit cannot auto-apply on a deterministic replay (it needs an agent-driven
  evaluation or a person), why an improvement must beat 15% with a no-worse worst run, and why `togi review` is there
  for you to read the diff. The hard gates (deterministic tests, forbidden attempts, holdout tasks) and the money
  approvals are what keep a bad edit from mattering.
- **The claude driver trusts Claude Code's permission system** to keep the stone agent to `Bash(togi:*)` (or, on the
  MCP surface, to the one Togi MCP server). The MCP surface is isolated with `--setting-sources ""` rather than
  `--safe-mode`, which still lets some installed plugins and agents load.
- The journal is best-effort: a full disk drops lines rather than failing a command.

## Development

```sh
python3 -m pytest -q            # the whole suite (2-3 minutes; it spawns real subprocesses, ptys and sandboxes)
                                # tests never touch the real approver: each gets a throwaway TOGI_APPROVER_HOME (test scope),
                                # the lock/revert/apply tests use a default-scope approver in a faked <pw_dir>/.togi, and a guard
                                # fails any test that changes an approver file in the real <pw_dir>/.togi
togi doctor                     # approver, sandbox, registry, imports, manifest, tiers, lock hashes, audit chain
```

Layout: `src/togi/` (`bundle.py` types + decorator, `host.py` the single execution path, `approvals.py`,
`approver.py` the passphrase, `journal.py`, `runmode.py` + `_runchild.py` + `sandbox.py` + `runreg.py`, `stone.py`,
`hone.py`, `apply.py`, `audit.py`, `pathnorm.py`, `patch.py`, `mcp_bridge.py`, `bench.py`, `doctor.py`, `cli.py`),
`examples/notes/`, `tests/`.
