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
ln -s "$PWD/bin/togi" ~/.local/bin/togi      # bin/togi runs `python3 -I`: see "Start Togi through its launcher" below

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
                                               # not shown. The approver lives in ~/.togi/approver.d (0700; fixed; not $TOGI_HOME, not $HOME)
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
locked  = ["togi.toml", "stone/**", "src/crowd/engine.py", "src/crowd/settle.py",
           "src/crowd/togi_bundle.py", "src/crowd/__init__.py"]   # the money command's own module and package: see "The code behind a money command"

[stone]
tests = "tests"                   # deterministic pytest dir
tasks = "stone/tasks"             # agent tasks (*.toml)

[policy]
approve_effects = ["money"]       # money is always gated; add "write"/"admin" to gate those too
auto_polish = true
apply_driver = "claude"           # minimum driver `togi apply` evaluates with (default "claude"; "script" = replays)
apply_runs = 3                    # minimum runs per task when `togi apply` evaluates a change (default 3, at least 2)
run_env = ["PAYPAL_CLIENT_ID"]    # env vars the `togi run` host keeps (default none); see "Code mode" below
test_approvals = false            # accept grants from a throwaway (test-scope) approver? default false; see "Test approvers"
require_lock = true               # money commands need a verified `togi lock`; default true when [tiers].locked is not empty
```

**The code behind a money command must be locked or reshape (r5).** Python imports more than the files you list: a new
package directory `store/` beside a locked `store.py` is imported *instead of* it, so is `store.so`, so is a `.pyc`. Togi
therefore (1) puts every such shadow of a locked file into the lock (a new one is a changed lock: the money path says
`lock_check_failed`), and (2) requires that every importable file (`.py`, `.so`/`.pyd`, `.pyc`, `.pth`, package directories) in
the top-level package of each money command, every symlink there and every repo module the bundle loads, is in the `locked` or
`reshape` tier. Anything untiered there makes the money path refuse with `gated_code_unlocked`, and `togi doctor` warns, so list
the module that defines the command (`src/crowd/togi_bundle.py`) and the package's `__init__.py` as locked, like
`examples/notes` does. Reshape files in the package are fine (changing them needs the real approver, through `togi apply`).
Bundles are also imported from **source only** (no bytecode is read from or written to the repo; a `.pyc` planted in
`__pycache__` is ignored) and with their repo paths **after the standard library**, so a `json.py` in the repo cannot replace
`json`. Bytecode inside a repo's `__pycache__` is not part of the lock: nothing Togi starts ever reads it (every Python
child that can load bundle code uses an impossible pycache prefix, `/dev/null/togi-no-bytecode`, so no bytecode is read
from anywhere), so running a locked repo's tests in place doesn't disturb the lock.
A bare `name.pyc` beside the source is importable and stays locked.

The `[policy]` keys after `auto_polish` are optional; a manifest written before they existed behaves as if they held the
defaults shown. `test_approvals` and `require_lock` must be real TOML booleans (`"true"` or `1` is an error, not consent).

Register it with `togi add <repo>`. When the bundle is imported, `<repo>/src` and `<repo>` are added to `sys.path`
(after the standard library, before site-packages). Bundles are imported lazily, only when a command needs them, so `togi skill` stays fast.

**Start Togi through its launcher.** `bin/togi` runs `python3 -I src/togi/_launch.py`: Python's *isolated* mode, with no
current directory and no `PYTHONPATH` / `PYTHON*` variable on the import path, and Togi's own `src` added explicitly. The
reason is the people who type a passphrase into it: `python3 -m togi approve` puts the directory you are standing in
first on `sys.path`, so a `hmac.py` or `json.py` lying there (or on your `PYTHONPATH`) would run inside *your*
approval session. Everything Togi starts itself uses the same form (`paths.isolated_argv`): the `togi run` host re-exec,
the stone's `togi` shim and its MCP server. `python3 -m togi` and any other normal interpreter that imports Togi hands
over to the launcher before it imports anything else it can (see the limits below), and the human gates refuse in an
interpreter that is neither isolated nor visibly clean. A bundle's code is imported from its registered repo
(`<repo>/src`, `<repo>`), which Togi adds explicitly; a bundle that needs third-party packages needs them installed in the
interpreter's own site-packages, because `PYTHONPATH` no longer reaches it (and `-I` skips the per-user site directory, so
install the optional `mcp` package for the interpreter itself, `python3 -m pip install mcp`, not with `--user`).

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
prints is returned. An error message from the script is cut at 1000 characters, a request line over 1 MB (or one that
never ends) stops the script (`protocol_error`), and what a bundle command itself prints to stdout never mixes into the
script's output (see "Output caps"). Calls return full data (no `--limit`). A money call raises `togi.NeedsApproval` (with
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
   That includes a bundle that is *installed* (`pip install crowd`): the Python install stays readable, but the paths
   inside its `site-packages` that are a registered bundle's module (the package, its `.dist-info`, `.pth` and
   editable-install finder files) are denied last, so `import crowd` fails in the script too.
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
   | polish touching anything that is not plain text. Allowed: `.md` `.txt` `.json` `.yaml` `.yml`, and `.toml` other than `pyproject.toml` / `togi.toml`. Everything else counts as code (`.py` `.sh` `.js` `.cfg` `.ini` `.html` `.pth`, a `Makefile`, a file with no extension...), and so does content starting with `#!` | never: rejected, at `hone` and again at `apply` |
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

   **The evaluation is signed.** The stone's before/after comparison is cached in `proposals/<id>/evaluation.json` so a
   re-run of `togi apply` is cheap. The record carries a `mac` (HMAC under the audit key, covering the proposal id and
   everything in it). A cache with no mac, a wrong one, or one signed for another proposal is ignored and the stone runs
   again (and `togi review` does not show it as a result): editing the cache to make a polish change look like an
   improvement does not auto-apply it.

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
   first and, if broken, writes nothing. `apply` and `revert` also require the log to be **authenticated**: at least one
   entry signed by this machine's key and every entry after the first signed one signed too (an unsigned *prefix*, from
   before signatures existed, is allowed and counted as `unauthenticated_entries`). A log with no signed entry at all
   cannot be appended to by anything but a person's `togi lock`, and `apply` / `revert` refuse it (`audit_unauthenticated`).
   A person can re-anchor a log whose head or key was lost with `togi lock`. On a
   clean checkout (CI) `togi log <bundle> --verify --chain-only` checks the hash chain alone and says that it did not
   check signatures or the head. The log lives in the bundle repo so it travels with the code. Commit it.
6. **Revert.** `togi revert <id>` (a person: terminal, typed word, passphrase) applies the reverse diff and logs it. It
   re-checks what `apply` does: the audit chain, that the stored diff is the one the audit log says was applied, the
   tiers, and the locked hashes before and after the write (with rollback).

`togi lock <bundle>` (a person: terminal, typed word, passphrase) records the sha256 of every file in the lock set (the
locked files **and whatever Python would import in place of them**, see "The code behind a money command") in
`.togi/locked.sha256`, signs a `lock` entry into the audit log, and records a signed **describe snapshot** of the bundle's
commands in `.togi/describe.json` (it imports the bundle once, in a sandboxed child, to read them). Before it asks for anything
the prompt shows the locked files **changed, missing and new since the last lock (with sizes)**, the state of the audit head
pointer, the audit entries written since the last lock, and the commands it read; if any file differs, the lock file was
deleted or the head pointer is missing, you must also type `relock`. `apply`, `revert`, `doctor` and the **money path** re-check
the lock. The lock file is only trusted when it is **anchored**: it must be exactly the one the latest `lock` entry in the audit
log recorded, and that entry must be genuine (its hash matches its content, it carries this machine's key id and a valid
mac). Editing a locked file and rewriting `locked.sha256` to match leaves it unanchored, and everything that relies on
the lock refuses. A symlink that stands under a locked pattern (or in place of a locked directory) is reported by
`verify_locked`, refused by `togi lock`, and blocks `apply` and money, because locked code could otherwise be reached through
a path nobody hashed. `togi.toml`, the stone tasks and tests, `.togi/` and `.git/` are *always* locked even if the
manifest forgets to say so. Hone can never edit the stone, holdout tasks, policy,
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
errors and `--dry-run` output. `togi run --max-output N` raises the 8192-byte cap on what a script prints, but **never past
1,000,000 bytes**, the same ceiling `--full` has (r5: a larger `N` is clamped, not honoured).

Environment: `TOGI_HOME` (default `~/.togi`; moves the registry, journals, approvals, proposals and audit key, but **not** the
approver, which lives in `~/.togi/approver.d`; a `TOGI_HOME` that is, contains or sits inside the approver's directory is refused,
except the default `~/.togi` itself), `TOGI_APPROVER_HOME` (tests and the stone only: a throwaway approver; every grant it signs has `scope: "test"`,
see below), `TOGI_STONE_SANDBOX` (set by the stone harness for its own sandbox runs; does nothing in any other home), `TOGI_AGENT` (names the calling agent; **setting it at all, even to the
empty string,** makes human-only commands refuse), `TOGI_PASSPHRASE_TIMEOUT` (seconds to wait for the approver
passphrase, default 120), `TOGI_SESSION` (groups calls for `retry_of`; default is the parent shell's pid), `TOGI_APPROVAL_TTL`
(seconds, default 900), `TOGI_DEBUG` (print tracebacks), `TOGI_CLAUDE_BIN` / `TOGI_CLAUDE_NO_SAFE_MODE=1` /
`TOGI_CLAUDE_ARGS` (adapt the `claude -p` driver to another Claude Code version). A project-local `.togi/bundles.toml` (found by walking
up from the cwd) is merged over the global registry; its entries win.

## Guardrails and their honest limits

**Threat model: read this first.** Togi's guarantees hold when the agent reaches your tools **only through Togi's own
surfaces**:
- a shell restricted to `togi …` commands (for Claude Code: `--allowedTools "Bash(togi:*)"`, as the stone and the demo
  run it);
- `togi run` scripts (sandboxed on macOS);
- the MCP bridge.

Within that, an agent cannot approve its own payments, run code other than the locked code behind a money command,
rewrite locked files, launder the audit log, or read the host's secrets.

Togi does **not** protect you from an agent with an **unrestricted shell running as your own user**. Such an agent can:
- edit Togi's own source, or plant files in its install;
- read `$TOGI_HOME` (the audit key, approval records);
- rewrite anything you can.

No in-process check can stop the same user from changing the checker. If an agent needs a general shell, run it as a
**separate OS user or in a container** that can reach only the `togi` command and its bundle repos read-only. The v0.2
plan removes this limit: a small Togi service runs as you, holds the approver and audit keys, and executes gated
commands; the agent gets only a thin client over a socket.

The sandbox below narrows what a `togi run` script is, but it is a same-user process under a profile, not a VM.

What is enforced in code:

- **Money approvals.** A call to a `money` command (or any effect in `approve_effects`) never executes the function
  without a matching grant. Without one the host writes a pending request and returns exit 3 (at most 20 pending per
  bundle; further ones are refused with a `next`). The grant is bound to bundle + command + a hash of the exact
  arguments **+ the bundle's repo path, manifest version and a digest of its locked files' contents**, so it dies if any
  of them changes. It is single use and expires (15 min after approval). "Single use" is not a field in the approval
  file (an agent can edit that, and the grant's signature covers "approved", not "unused"): a signed **spent-approval
  record** is kept *outside* `$TOGI_HOME`, in the approver home, appended under a lock before the function runs
  (spent even if the function then fails). Each line is MAC'd and chained to the one before and a head pointer counts the
  lines, so a deleted, edited or reordered line makes every approval refuse (`spent_log_damaged`) until a person looks.
  The `.claimed` marker beside the approval is a second witness.
- **The money path checks the lock (r4).** A gated command is refused, at request time and again at use time, unless the
  bundle's lock verifies (`togi lock`, anchored in the signed audit log, no hidden symlinks, log authenticated). If there is
  no lock file at all, a bundle with locked tiers (`[tiers].locked` not empty) is refused (`not_locked`) unless its
  manifest says `[policy] require_lock = false` outright; a bundle with no locked tier needs none. A lock file that exists
  must verify whatever `require_lock` says. The approval prompt shows the lock state (`lock: VERIFIED ...`) and
  `togi approve` refuses a lock that fails. The stone's own sandbox run is exempt (its throwaway home has no audit key to
  anchor a lock with, and nothing there is real).
- **Human-only commands** (`approve`, `deny`, `lock`, `revert`, the human path of `apply`, `approver setup`) need, all
  together (and `lock`, `revert` and `apply` also the default-scope approver, see "Repo-changing gates" below): stdin and stdout to be TTYs; `TOGI_AGENT` **not set at all** (an empty value still counts) and `TOGI_RUN` not
  set; no live `togi run` host among the process's ancestors (each host registers itself in `$TOGI_HOME/run/<pid>`;
  a command-line match is a second check); an interpreter that is **isolated** (`python -I`, which is how `bin/togi` starts)
  or at least visibly clean (no `PYTHON*` variable, no current directory on `sys.path`); the typed confirmation word; and
  **the approver passphrase**.
- **What you are shown is what is signed (r4, r5).** The pending approval is a file the requesting agent wrote, so
  `togi approve` does not trust it. Before it asks you anything it recomputes the arguments' hash from the arguments it is
  about to show, the binding (repo path, version, lock-set digest) from the live repo, and the lock state from the live
  lock; it recomputes the summary and the confirmation word **as plain data from the describe snapshot `togi lock` signed**
  (r5: it imports nothing; r4 loaded the bundle in a child process, which could still run the bundle's import-time code
  while you were about to decide); it refuses on any mismatch with the pending record (`approval_tampered`,
  `approval_stale`, `lock_check_failed`, `not_locked`, `gated_code_unlocked`), refuses an argument too long to be shown whole
  (`prompt_too_long`: you cannot sign what you cannot see), shows the recomputed values and **the expiry that will be signed**
  (r5; not the one the requester wrote into the file), and signs them. If the record changes between the check and the
  signature, nothing is signed (`approval_changed`).
- **The approver passphrase.** A TTY check alone stops an agent using a normal shell, but an agent that can run
  `script(1)` or a pty library can give `togi approve` a terminal and type the word itself (the Opus refuter did exactly
  that). So a person sets up a passphrase once, in their own terminal: `togi approver setup` (12+ characters, typed
  twice, read from `/dev/tty` with echo off, never from a pipe). Only an scrypt hash and salt are stored, in
  `approver` (0600, in `~/.togi/approver.d`). Every human-only action then asks for it after the typed word. `togi approver setup` on a machine
  that already has an approver asks for the *current* passphrase first and otherwise changes nothing. Wrong attempts are counted:
  five wrong, and the approver is locked for ten minutes; every attempt, right or wrong, is a line in the `_host`
  journal. With no approver set up, those actions refuse and say to run `togi approver setup`. Waiting for a passphrase
  that never comes times out (2 minutes) instead of hanging the caller.
- **The approver lives where an agent cannot move it (r2), in a directory of its own (r5).** The approver (`approver`,
  `approver.state`, the signing key `approver.key` and the spent-approval log) is kept in `<pw_dir>/.togi/approver.d` (mode
  0700), where `pw_dir` is your account's home directory from the password database, **not** `$HOME` and **not**
  `$TOGI_HOME`. (r4 kept the files loose in `<pw_dir>/.togi`, which is also the default `$TOGI_HOME`, so the journals,
  approvals and registry shared a directory with the key and the passphrase record. They move into `approver.d` once, at your
  first human command after upgrading (or `togi approver migrate`), never from an agent session; if the same file exists in both
  places, Togi refuses until you resolve it.) In r1 it sat in `$TOGI_HOME`, an ordinary environment variable, so an
  agent could set `TOGI_HOME=/tmp/mine`, `togi add` a bundle there, create an approver of its own with a fake terminal,
  approve its own money command and run it. Now `TOGI_HOME` moves everything *except* the approver, and the wrong-attempt
  counter cannot be reset by starting over in a new home. A `TOGI_HOME` that equals, contains or sits inside the approver's
  directory (other than the default `~/.togi` that holds `approver.d`) is refused (`togi_home_unsafe`).
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

  **A bundle must opt in before Togi accepts a test-scope grant at all (r4).** Relying on every bundle to remember the
  check above meant a bundle that forgot it moved real money on a grant anyone could mint. Now a `test`-scope grant for a
  gated command is refused (`test_scope_refused`) unless the manifest says `[policy] test_approvals = true`: `togi approve`
  under a test approver refuses up front, and a grant minted anyway is refused when it is used. Opt in only when the
  command checks `ctx.approval_scope` itself (as `examples/notes` does, with `NOTES_TEST_MODE`), or only has fakes to move.
  The stone's own sandbox run and the bundle tests it starts opt in for themselves automatically: the harness sets
  `TOGI_STONE_SANDBOX=<root>`, honoured only when `<root>` carries the harness's marker file and holds this
  `$TOGI_HOME` as `<root>/home` and this `$TOGI_APPROVER_HOME` as `<root>/approver`. `togi doctor` flags bundles that opt in.

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
  in `<pw_dir>/.togi/approver.d` (scope `default`). While `TOGI_APPROVER_HOME` is set they refuse (`test_scope_refused`, exit 2)
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
  commands and flags: a flag name is kept only if it is a flag the host or one of the bundle's commands really has,
  otherwise it is recorded as a hash (`--#1a2b3c4d`; a made-up name is exactly where a value hides, e.g. `--sk-live-...`),
  with a `near` field naming the real flag it most resembles so hone still sees the typo; a command word is kept only if it
  is a near-miss of a real command word, and every other token becomes `<num>` / `<str>`. A `ctx.journal_note` from a
  write, money or admin command is recorded as keys and value *types* only (read commands' notes are stored as written).
  Argument hashes are kept for read commands only.
  Every invocation of a human-only command is journaled before its flags are parsed (argv shape only), and the stone's
  forbidden-command check reads every journal in the task sandbox, in every session.
- **Tiers.** Enforced by path globs after normalisation (case-insensitive, `..`/`//` resolved, symlinks followed, control
  and separator characters refused) and by the locked hashes, which are re-verified *after* a write too.
- **Output caps.** Everything an agent can receive is cut at ~8 KB with a `more`/`--full` hint: results, errors,
  `--dry-run`, every host command, and MCP results. The cut handles any shape: a list loses its tail; the largest list or
  dict is halved (a wide dict keeps its first keys, `more` says how to narrow with `--fields`); long strings are cut; and
  as a last resort the value becomes a text preview, so the serialised envelope is never larger than the cap
  (`output.hard_cap`). What a bundle command prints to stdout while it runs is captured (4 KB kept, the rest dropped) and
  goes to stderr with a note, so it cannot flood or corrupt the JSON on stdout; the journal records `printed_bytes`.
  (C-level writes straight to file descriptor 1 are not captured.) `togi run` caps a script's error message and the request
  lines it sends.

What is **not** a security boundary (v0.1):

- **A hostile process running as the same user with full file access** (see the threat model). Approval records,
  the audit key and head pointer, and the run registry are plain files in `$TOGI_HOME`; the approver hash and signing key
  are plain files in `<pw_dir>/.togi/approver.d`.
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
- **`.togi/locked.sha256` is a plain file in the repo, but it is anchored.** `togi lock` records it (default approver
  only) and signs a `lock` entry into the audit log; the lock only counts if it is exactly what the latest such entry
  recorded and that entry carries this machine's key id and a valid mac (r2/r3 added the comparison, r4 the signature
  check on the entry it relies on). So an agent that can write the repo cannot edit a locked file, rewrite `locked.sha256`
  and have anything accept it: `togi doctor` reports it, `apply`/`revert` refuse it and the money path refuses it. What it
  cannot stop is someone who can also read `$TOGI_HOME/audit.key` (they can sign a `lock` entry) or who can truncate the
  log and the head pointer together. Commit the log and the lock file, and read `git diff` on locked files.
- **Starting Togi any other way narrows the isolation.** `bin/togi` and everything Togi starts itself run `python3 -I`.
  `python3 -m togi` is caught by the package's first import (before it imports any other module) and restarted isolated, but
  `python -m` itself imports `runpy` and a few more standard modules from the current directory before any Togi code can
  run; a pip console script or `python -c "import togi.cli"` has imported Togi's own modules (and so standard ones such as
  `decimal` and `json`) from a normal `sys.path` before `cli.main` can restart it. Use `bin/togi`. The human gates refuse a
  non-isolated interpreter that has a `PYTHON*` variable or the current directory on its path, which catches the usual
  planted-file case, but code that has already run in such a process is not undone. A project-local `.togi/bundles.toml` in
  the current directory (or any parent) still overrides the global registry; the approval prompt and `togi lock` show the repo path
  they act on, so read it.
- **`togi approve` never imports the bundle (r5).** In r4 it loaded the bundle in a scrubbed child to regenerate the summary,
  but that child was an ordinary same-user process: import-time code could write files or read your disk while you were about
  to decide. Now the summary and the word you type are recomputed as plain data from the **describe snapshot** that `togi lock`
  signed (`.togi/describe.json`, HMAC under the audit key, valid only for that lock and that repo path) and compared with what
  the request filed. Reading the bundle happens only in the sandboxed child (the `togi run` strict profile family: no network,
  writes only to its own temp dir, `$TOGI_HOME`, the approver and other repos hidden; its own session so no terminal; scrubbed
  environment with no `run_env` values; killed as a group; time- and output-bounded): at `togi lock`, and for `approve` of a
  bundle that has no snapshot yet. With no OS sandbox on the machine a bundle is not run outside one (`describe_unsandboxed`);
  `togi lock` asks for one more typed word (`unsandboxed`) before importing it that way. The snapshot is only as good as the
  audit key, a plain file in `$TOGI_HOME`: it stops an agent that can write the repo, not one that can read `$TOGI_HOME`. If a
  reshape change alters a gated command's name, summary or parameters, the request (built from the live command) no longer
  matches the snapshot and `approve` refuses until a person re-locks.
- **What the r5 lock covers, and what it does not.** The lock set is every locked file plus whatever Python would import in
  place of it; the money path additionally refuses untiered code in the package of a gated command and any untiered repo
  module the bundle has loaded. It does **not** hash `reshape` files (they are allowed behind a money command because changing
  one through `togi apply` needs the real approver; an agent that can write the repo directly can still edit a reshape file
  without the digest noticing, so keep reshape code to behaviour you are content to have a person review in `git diff`), it
  cannot see a module a gated command imports lazily from outside its package until something has loaded it (keep gated code
  in one package), and it says nothing about code installed outside the repo (site-packages, the standard library).
- **Bytecode and import order are Togi's, not the repo's (r5).** Bundle code is compiled from source on each start (the cost of
  never trusting a `.pyc` found in the repo); output a bundle writes below Python (`os.write(2, ...)`, a subprocess that inherits
  the descriptor) is not captured by the stdout/stderr caps.
- **Proposal files are plain files in `$TOGI_HOME`.** `apply` re-classifies the diff text it actually reads, re-checks the
  locked hashes and the signed audit log, and the evaluation cache and a reshape proposal's evaluation approval are signed;
  but `meta.json`'s `status` and stored `diff_sha256` are not (an agent can edit them). That cannot make a locked file
  changeable or an unapproved reshape run, only re-open a closed proposal that still has to apply cleanly and pass the same checks.
- **The spent-approval record stops edits, not deletion.** A process that can delete `spent.jsonl` and `spent.head` in the
  approver home together can replay a grant that is still inside its 15-minute life. The record detects every other kind of
  damage and then refuses everything.
- **The stone sandbox's opt-in is a convention, not a secret.** `TOGI_STONE_SANDBOX`, the marker file and the two matching
  paths are things a same-user process can create; they separate the harness's own sandbox from "a bundle that never opted
  in", they are not a boundary against someone deliberately forging them.
- **"Fixed" means fixed against environment variables, not against code.** An agent that can run arbitrary Python can
  import Togi with the password database patched (this is exactly how the tests fake it), or call `audit.lock_bundle`
  directly. The default-scope rule closes the route an agent with only a shell and a pty has (an environment variable and
  a pseudo-terminal); it is not a boundary against a process that can run its own code as you.
- **A "test" approver is only as safe as the bundle that opted in.** Togi marks a throwaway approver's grants
  `scope: "test"`, refuses them for every bundle that has not said `[policy] test_approvals = true`, and tells the command
  the scope; it cannot know which of your commands move real value. A bundle that opts in without checking
  `ctx.approval_scope` treats a test grant like a real one.
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
                                # the lock/revert/apply tests use a default-scope approver in a faked <pw_dir>/.togi/approver.d, and a guard
                                # fails any test that changes an approver file (or approver.d) in the real <pw_dir>/.togi
togi doctor                     # approver, sandbox, registry, imports, manifest, tiers, lock hashes, audit chain
```

Layout: `src/togi/` (`_launch.py` the isolated launcher, `_describe.py` + `describe.py` the sandboxed child and the signed
snapshot that replace importing a bundle in `togi approve`, `codeset.py` what Python can import from a repo (the lock set's shadows and
the gated-code coverage check), `bundle.py` types + decorator, `host.py` the single execution path, `approvals.py`, `spent.py` the
spent-approval record, `approver.py` the passphrase, `journal.py`, `runmode.py` + `_runchild.py` + `sandbox.py` + `runreg.py`, `stone.py`,
`hone.py`, `apply.py`, `audit.py`, `pathnorm.py`, `patch.py`, `mcp_bridge.py`, `bench.py`, `doctor.py`, `cli.py`),
`examples/notes/`, `tests/`.
