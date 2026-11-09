# Togi — spec v0.1, revision r5 (2026-10-27)

Revision r1 hardens the guardrails after an independent refutation (FIXES-r1.md). Contract changes are additive: a
manifest or bundle written against r0 keeps working. What changed is marked **(r1)**.

Revision r2 (FIXES-r2.md) closes two holes found in r1: an agent could choose where approval state lives, and `togi apply`
ran proposed code before a person decided. What changed is marked **(r2)**. One contract change matters to bundles:
`ctx.approval_scope`, and the rule that a bundle that moves real value refuses a `test`-scope approval (§2).

Revision r3 closes the one hole left in r2: the human gates that change a repo (`lock`, `revert`, the reshape
evaluation approval, a human `apply`) accepted a throwaway `test`-scope approver, which an agent can create for itself.
They now accept only the default-scope approver, and the audit log records the scope (§3, §6). What changed is marked
**(r3)**.

Revision r4 (FIXES-r4.md) answers the final design review. Two of the six guarantees did not hold: the human gates loaded
code from the current directory or `PYTHONPATH` (Togi now starts itself isolated, §3), and a throwaway test approver could
move money in any bundle that did not check scope (a bundle must now opt in with `[policy] test_approvals = true`, §2).
The rest had gaps: what a person is shown at `approve` is recomputed rather than read from the pending file and is what is
signed (§3); single use is a signed record outside `$TOGI_HOME` (§2); the money path checks the lock (`[policy]
require_lock`, §2, §6); the evaluation cache is signed (§6); the output caps hold on the CLI (§3); apply and revert need an
authenticated audit log, the lock entry's signature is verified, polish may touch only plain text, and unknown flag names
and write-command notes are not journaled verbatim (§4, §6); `togi run` hides installed bundle modules from the script
(§3). What changed is marked **(r4)**. Contract changes for bundles: two optional `[policy]` keys, and the example
`tip_send` that shows the scope check.

Revision r5 (FIXES-r5.md) answers the fifth design review. The lock and the approval binding covered file *paths*, not the code
Python imports for a money command, and `togi approve` ran the bundle's code before the person decided. Now what is hashed is what
is importable (§2: a package directory, an extension module or bytecode that would be imported in place of a locked module is part
of the lock; the code behind every gated command must be locked or reshape, else `gated_code_unlocked`; bundles are imported from
source only, after the standard library), and `approve` never imports a bundle: it recomputes the prompt from a describe snapshot
that `togi lock` signed, read by a sandboxed child (§3). The approver gets a directory of its own, `<pw_dir>/.togi/approver.d`
(0700), the lock prompt shows what changed, a bundle's stderr is capped like its stdout, the expiry shown at `approve` is the one
signed, an argument too long to show cannot be approved, and `togi run --max-output` stops at the `--full` limit. What changed is
marked **(r5)**. Contract changes for bundles: a manifest with a lock must put the code behind its gated commands in `locked` or
`reshape` (§2); the manifest needs no new key.

The eval layer (2026-10-27, **§9**, designed in docs/EVAL-SPEC.md) is additive: it changes nothing in the contract of §2 and every
existing guarantee stands. New host commands `togi answer`, `togi task` and `togi eval` (marked **(eval)**), an `--engine eval` option
on `stone` and `bench`, an optional outcome gate on `apply`, and a long-running convention in the index skill.

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
  (repo path, manifest version, and a digest of the locked files' contents (r1); r5: of the whole **lock set**, below)**, **that it is signed by the
  approver (r2, below)**, and is unused, executes the function, then marks the approval consumed.
- **(r2) Grants are signed, and carry a scope.** When a person grants (or denies), the approver writes
  `grant_mac = HMAC-SHA256(approver.key, canonical{id, bundle, command, args_hash, binding_hash, status, expires,
  decided_by, scope, home})` (`home` = the real path of the `$TOGI_HOME` it was made in) and `scope`. The host verifies
  the MAC with the key it finds in the approver home and refuses (`approval_unsigned`, exit 2) otherwise, so a record
  that was forged, edited, or copied from another `$TOGI_HOME` or another approver is worthless. The approver lives at
  a **fixed, env-independent location** (`<pw_dir>/.togi/approver.d` since r5, `<pw_dir>/.togi` before; `pw_dir` from the password database, not `$HOME` and not
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
- **(r5) The code behind a money command must be locked or reshape.** The lock hashes files, and a file can be replaced
  without touching a hashed byte, because Python resolves `import notes.store` by looking in a directory for a package
  directory `store/` (with an `__init__`), then an extension module (`store.so`, `store.cpython-312-darwin.so`), then
  `store.py`, then a bare `store.pyc`. So two rules, enforced by one piece of code (`codeset.py`) wherever the lock is used:
  - **The lock set is what is importable, not only what a glob names.** It is every file under a locked pattern (nothing
    skipped: `__pycache__`, `build`, `dist`, `venv`, `node_modules` are walked like any directory; only the repo's own `.git`
    and `.togi`, and **bytecode (`.pyc`/`.pyo`) inside `__pycache__`**, are left out. No Python process Togi starts ever
    reads bytecode for bundle code at all: in-process imports and every child that can load bundle code use a pycache
    prefix that cannot exist (`/dev/null/togi-no-bytecode`), so that bytecode cannot change what runs, and hashing it would make every ordinary test run
    "break" the lock) **plus the shadows of every locked importable file**: in its directory a package directory of the
    same name that has an `__init__` (and everything in it), any `name.<suffix>` extension module or bare `name.pyc`; and,
    for a locked file not under `src/`, the same names in `src/`, which is first on the import path.
    A shadow is in the lock set whatever tier a glob gives it (a `reshape = ["src/pkg/**"]` cannot launder a planted
    `store/__init__.py`), a symlink standing where a shadow would be fails the lock like any locked symlink, and a new shadow
    is a changed lock (`verify_locked` lists it as `unlisted`, the approval binding changes, the money path says
    `lock_check_failed`). The lock file, `locked_digest` and the approval binding are all computed over this set.
  - **Gated-code coverage.** Wherever the lock is required (`Manifest.lock_required()`): every importable file (`.py`, `.pyw`,
    `.pyc`, `.so`, `.pyd`, `.pth`, package directories) under the top-level package of each gated command (found from the
    function's code object and module, so `src/notes` for `src/notes/togi_bundle.py`; the file itself for a top-level
    module), every symlink in it, and every repo module the bundle's process has loaded, must be in the `locked` or `reshape`
    tier. `__pycache__` contents are not looked at (they are never read, below). Reshape code is allowed because changing it
    already needs a person with the real approver (`togi apply`). Anything else is code that runs behind a money command
    with nobody having signed for it: the money path refuses with `gated_code_unlocked` (status `refused`, exit 2) at request
    time and again at use time, no request is filed, `togi approve` refuses it from the snapshot (§3), and `togi doctor`
    warns (`<bundle>: gated code locked`). Not applied inside the stone's own sandbox (§5), nor to a bundle that needs no lock.
  - **Source only, standard library first.** A bundle is imported with `sys.dont_write_bytecode = True` and `sys.pycache_prefix`
    set to an impossible path (`/dev/null/togi-no-bytecode`; a `$TOGI_HOME` cache could be planted by whoever can write
    `$TOGI_HOME`), so a `.pyc` planted in the repo's `__pycache__` or anywhere else (an unchecked hash-based one is trusted
    whatever the source says) is never read and nothing is cached into the repo; and its repo paths (`<repo>/src`, then
    `<repo>`) go on `sys.path` **after the standard library and before `site-packages`**, so a `json.py`, `decimal.py` or
    `hmac.py` in the repo or its `src/` cannot replace the standard library, while a registered repo still beats an old
    installed copy of the same bundle. (`togi`'s own modules are imported before this; modules imported afterwards, including
    standard-library ones a bundle pulls in lazily, are compiled from source in each process.)
  - Honest limits: code a gated command imports from outside its package and that nothing has loaded yet is only covered once
    it is loaded (keep gated code in one package); code installed outside the repo is outside the lock; reshape files are not in
    the digest (a person with the real approver is the control, as before).
- **(r1)** At most 20 pending approvals per bundle; a further distinct request is refused (`too_many_pending`) with a
  `next`. An identical retry returns the existing request.
- **(r4) A bundle must opt in before a test-scope grant counts at all.** A `test`-scope grant (§ above) for a gated command
  is refused with `test_scope_refused` (status `refused`, exit 2) unless the bundle's manifest says
  `[policy] test_approvals = true`: `togi approve` under a throwaway approver refuses up front, before any prompt, and a
  grant minted anyway (the record is signed by a test approver) is refused when `--approval` presents it, before the
  command runs and without spending the grant. The stone's own sandbox run and the bundle tests it starts opt in for
  themselves (`TOGI_STONE_SANDBOX`, §5). A bundle that opts in is saying its gated commands do what the rule above says;
  `examples/notes` does (`tip_send` checks `ctx.approval_scope` against its own `NOTES_TEST_MODE`).
- **(r4) Single use is a signed record outside `$TOGI_HOME`.** The `status` and `.claimed` marker in the approval file are
  bookkeeping an agent can edit (the grant's MAC covers "approved", not "unused"). The authority is the **spent-approval
  record** `spent.jsonl` (+ `spent.head`) in the approver home (§3): one line per spent approval, appended under an exclusive
  lock, `{n, id, ts, home, prev, mac}` with `mac = HMAC-SHA256(approver.key, canonical{n, id, ts, home, prev})` chained to the
  previous line, and a MAC'd head counting the lines. `check_for_use` refuses (`approval_used`) an id found there or with a
  `.claimed` marker. The host writes the record **before** the command runs (`approvals.claim`) and does not run the command if
  it cannot be written. A line missing, edited, reordered, or a head that disagrees with the log, is `spent_log_damaged`:
  every approval is refused until a person looks. A line that is not a whole record (a torn write) is ignored.
- **(r4) The money path checks the lock.** At request time and again at use time (before the approval is spent) a gated
  command requires `audit.lock_state` to be `verified`: `.togi/locked.sha256` exists, every locked file matches it, no symlink
  stands under a locked pattern, it is the one the latest `lock` audit entry recorded (and that entry's key id and mac verify),
  and the audit log itself is intact and authenticated (§6). A failing lock is `lock_check_failed`; **no lock file** is
  `not_locked` unless `require_lock` is false (`Manifest.lock_required()`). Both are `refused`. **(r5)** The same two points also
  require the gated-code rule above (`gated_code_unlocked`). No request is filed for a bundle that fails. The approval prompt shows the lock state, and `togi approve` refuses a failing one. The stone's own
  sandbox run is exempt (§5).

**Context** (optional): a parameter named `ctx: togi.Context` is injected, never a CLI flag. It
gives `ctx.state_dir` (per-bundle writable dir), `ctx.journal_note(dict)`, `ctx.agent`
(from `TOGI_AGENT` env), `ctx.surface` (`cli` | `run` | `mcp`) and **(r2)** `ctx.approval_scope`
(`"default"` | `"test"` | `None`: the scope of the approver that signed the approval this call arrived with; `None` when
there was none). **(r4)** `ctx.journal_note(dict)` from a `write`, `money` or `admin` command is journaled as its keys and
the *types* of its values (`{"amount": "str"}`); a `read` command's note is stored as written.

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
locked  = ["togi.toml", "stone/**", "src/crowd/engine.py", "src/crowd/settle.py", "src/crowd/togi_bundle.py", "src/crowd/__init__.py"]
                                      # (r5) the package of a money command: every importable file locked or reshape

[stone]
tests = "tests"                       # pytest dir (deterministic)
tasks = "stone/tasks"                 # agent tasks (*.toml)

[policy]
approve_effects = ["money"]
auto_polish = true                    # polish changes may auto-apply if the stone does not regress
apply_driver = "claude"               # (r1, optional) minimum driver `togi apply` evaluates with: "claude" (default) | "script"
apply_runs = 3                        # (r1, optional) minimum runs per task in `togi apply` (default 3, at least 2)
run_env = ["PAYPAL_CLIENT_ID"]        # (r1, optional) env vars the `togi run` host keeps (default: none)
test_approvals = false                # (r4, optional) accept grants signed by a throwaway test-scope approver? default false
require_lock = true                   # (r4, optional) gated commands need a verified `togi lock`; default: true iff [tiers].locked is non-empty
```

`[tiers]` (r5): for a bundle with a lock, the module that defines a gated command and every other importable file of its
package belongs in `locked` (or `reshape`); `examples/notes` lists `src/notes/__init__.py` and `src/notes/togi_bundle.py` beside
`store.py` and `messages.py`. `apply_driver`, `apply_runs`, `run_env`, `test_approvals` and `require_lock` are optional; absent, they hold the defaults
shown (`require_lock`: true when the bundle has locked tiers of its own, false when it has none). A caller of `togi apply`
may raise the driver and the runs, never lower them. **(r4)** `test_approvals` and `require_lock` must be TOML booleans
(`"true"` or `1` is a manifest error). `test_approvals = true` is a statement that every gated command of the bundle checks
`ctx.approval_scope` (or only moves fakes); see the rule below. `require_lock = false` is the only way for a bundle with
locked tiers to run gated commands with no `.togi/locked.sha256`.

**Registration:**
- `togi add <path-to-bundle-repo>` records the bundle in the registry: `$TOGI_HOME/bundles.toml`
  (default `~/.togi`); a project-local `.togi/bundles.toml` overrides it.
- `togi remove <name>` removes it.
- Bundles are imported lazily, only when used.

---

## 3. CLI surface (agent-native conventions)

**(r4) Starting Togi.** `bin/togi` runs `python3 -I src/togi/_launch.py ARGS`: the interpreter is **isolated** (`-I`: no current
directory or script directory on `sys.path`, every `PYTHON*` variable ignored, no user site) and the launcher puts Togi's own
`src` on `sys.path` explicitly. Without it `python3 -m togi` puts the current directory first and honours `PYTHONPATH`, so a
stdlib-named file (`hmac.py`, `json.py`) planted where a person runs `togi approve` / `lock` / `revert` / `apply` would run
inside the person's approval session. Every place Togi starts itself uses the same form (`paths.isolated_argv`): the
`togi run` host re-exec, the stone's `togi` shim and its MCP server. `python3 -m togi` is handed to the launcher by the package's
first statements, before any other import; `cli.main`, as the entry point of any other normal interpreter, restarts through the
launcher before dispatching. The human gates (`approve`, `deny`, `lock`, `revert`, `apply`, `approver setup`) additionally
**refuse** (`unsafe_interpreter`, `refused`) in an interpreter that is not isolated and has a `PYTHON*` variable set or the
current directory (`''`, `.`) on `sys.path`; the check comes after the TTY and scope checks and before any prompt. Bundles are
imported from their registered repo paths (`<repo>/src`, `<repo>`), which are added explicitly; `PYTHONPATH` no longer reaches
them, including through a manifest's `run_env`. **(r5)** Those paths go after the standard library, and bundle code is read from
source only (§2).

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
**(r4)** The cut handles every shape and ends in a hard cap: a list loses its tail; otherwise the largest list or dict is
halved (a dict keeps its first keys; `more` says to narrow with `--fields` or lift the cap with `--full`), then long strings
are cut, then the value becomes a text preview; a bare `fit_any` + `hard_cap` guarantees the serialised envelope is never larger
than ~8 KB without `--full`. What a command prints to stdout while it runs is captured (first 4 KB kept) and written to stderr
with a note, never to stdout; the journal records `printed_bytes`. **(r5)** What it writes to **stderr** is captured the same
way (first 4 KB kept, a note, `printed_err_bytes` in the journal), on the CLI, in `togi run` and over MCP; output written below
Python (`os.write(2, ...)`, a subprocess that inherits the descriptor) is not seen by this and is not bounded. In code mode a script's error message is cut at 1000
characters in the child and again in the host, and a request line over 1 MB (or one without a line end) stops the script
(`protocol_error`).

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
  - **(r4)** The re-exec uses the isolated launcher (§3, "Starting Togi"): `python -I _launch.py ...`, no `PYTHON*` variable in the
    host's environment (not even one a manifest lists in `run_env`).
  - The child gets a scrubbed environment (no secrets, only PATH/HOME/TMPDIR/LANG plus the host's TOGI_HOME,
    TOGI_SESSION and TOGI_AGENT, so a nested `togi` stays in the same home), a temp working dir, a timeout
    (`--timeout`, default 60 s) and an output cap (8192 bytes by default; **(r5)** `--max-output N` raises it, `--full` is
    1,000,000, and `--max-output` is **clamped to that same 1,000,000**, so no flag lifts the ceiling).
  - **(r1)** On macOS the child runs under `sandbox-exec`: no network; file contents readable only from the Python
    install, its temp dir and OS directories (so not any registered bundle repo, `$TOGI_HOME` or the user's home);
    writes only to its temp dir; no process inspection or signals to other processes. **(r4)** Where a registered bundle is
    *installed* (its module sits in the Python install's `site-packages`), the paths there that are that module (package or
    module file, extension module, `.dist-info` / `.egg-info`, `.pth`, editable-install finders) are denied last with
    `file-read*` (a generic `file*` deny does not outrank the specific allow of the install), so `import crowd` fails in the
    script too. A *standard* profile (deny-list of
    the user-data zones) is used if the *strict* one cannot start Python. The result reports `sandboxed: true|false` (and
    `sandbox_level`); if `sandbox-exec` is missing or unusable the run falls back to unsandboxed and says why.
  - Only what the script prints is returned.
  - A money call inside `run` returns the needs_approval result as a Python exception
    `togi.NeedsApproval` with `.approval_id`. Approvals can never be granted from inside `run`.
- `togi approve <id>` / `togi deny <id>`: human only.
  - Requires stdin and stdout to be TTYs, and refuses when `TOGI_AGENT` is **set** (even to the empty string), when
    `TOGI_RUN` is set, or when a live `togi run` host is an ancestor process (**r1**: each host registers
    `$TOGI_HOME/run/<pid>`; a command-line match is a second check).
  - **(r4)** Also refuses, after those checks and before any prompt, in an interpreter that is not isolated and has a
    `PYTHON*` variable set or the current directory on `sys.path` (`unsafe_interpreter`; see "Starting Togi" above).
  - **(r4, r5) What is shown is what is signed, and approving runs no bundle code.** `approve` does not trust the pending file
    (the requester wrote it). Before anything is shown it **recomputes**: the args hash from the arguments it will display (must
    equal the record's `args_hash`), the binding (repo path, manifest version, lock-set digest) from the live repo (must equal
    `binding_hash`: `approval_stale`), the lock state from the live lock (a failing lock is `lock_check_failed`, an absent one
    where `require_lock` applies is `not_locked`), and the **summary and the confirmation word as pure data from the describe
    snapshot** (below), then compares them with what the request filed. The host built the request's summary and word at request
    time from the live command; any mismatch is refused (`approval_tampered`), and so is a command the snapshot does not
    describe, one that is not gated, one that takes other arguments than the record carries, a `test`-scope approver for a bundle
    that has not opted in (`test_scope_refused`), and an argument too long to be shown in full (`prompt_too_long`, below). The
    prompt shows the recomputed values, the lock state and where the command was read from, and `grant` signs exactly those (it
    re-reads the record and refuses, `approval_changed`, if it no longer agrees). `deny` shows the record as filed and verifies
    nothing: a denial is signed too and cannot be turned into an approval. **The bundle is not imported in the approval
    session**, in r4 it was, in an isolated child that was otherwise an ordinary process, so import-time code could write
    files or scan the disk while a person was about to decide.
  - **(r5) The describe snapshot.** `togi lock` imports the bundle once, in a hardened child (`describe.py`, `_describe.py`), and
    stores `<repo>/.togi/describe.json`:
    ```json
    {"v": 1, "bundle", "repo", "version", "module", "lock_sha256": "<sha256 of .togi/locked.sha256>", "recorded", "sandboxed",
     "commands": [{"name", "api", "effect", "summary", "confirm", "params": [{"name","kind","required"}], "schema": {...}}],
     "gated_roots": ["src/notes"], "loaded_files": ["src/notes/__init__.py", ...], "mac": "<hmac>"}
    ```
    `mac` = HMAC-SHA256 under the **audit key** over a domain string and the canonical JSON of everything else, so it is only
    valid for this machine, this repo path and exactly this lock (`lock_sha256` is the `diff_sha256` of the latest signed `lock`
    audit entry). A snapshot that is unreadable, not ours or whose signature fails is `describe_snapshot_invalid` (approve
    refuses); one made for an earlier lock is *stale* and treated as absent. The summary and word are recomputed from it with the
    same functions the host used (`approvals.summarize`, `approvals.confirm_word` over a `CmdView`); `gated_roots` and
    `loaded_files` let `approve` re-check the gated-code rule (§2) without importing anything.
  - **(r5) The describe child.** The only code that imports a bundle for `lock` or `approve` runs: under the `togi run` strict
    sandbox profile family (no network; writes only to its own temp dir; `$TOGI_HOME`, the approver and every other registered
    repo hidden; it may read its one repo and Togi's source; no process inspection or signals to others); in its own session
    (`start_new_session`: no controlling terminal, stdin is a pipe holding only the request), with its whole process group
    killed afterwards; with an environment of PATH / HOME / TMPDIR / LANG only (HOME and TMPDIR are its temp dir; no `run_env`
    value, no `TOGI_*`, nothing of the person's); a 30 s timeout; and its answer capped. Where no OS sandbox is available
    (`describe_unsandboxed`) the bundle is **not** run outside one: a library `lock` records no snapshot; `togi lock` says so,
    and only after a further typed word (`unsandboxed`) imports the bundle without one; `approve` for a bundle with no
    snapshot (locked before r5, never locked with `require_lock = false`, or stale) uses the same sandboxed child for that one
    command, and refuses without a sandbox.
  - Shows the bundle, repo path, version, summary (cut at 300 characters) and arguments, with control characters, ANSI
    escapes and bidi overrides shown as `\uXXXX`, and requires typing the confirmation word shown (e.g. the drop id).
    **(r5)** The first line shows the expiry that **will be signed** (`now` + the TTL, or `--ttl`), computed once and passed to
    the grant, never the `expires` field of the pending file. **(r5)** If any argument name (over 60 characters) or value (as
    JSON, over 300) would be cut at the prompt, `approve` refuses with `prompt_too_long` before asking for anything, as the
    1000-line diff rule does for `apply`: a person cannot sign what they cannot see. A denial is not blocked by it.
  - **(r1)** Then requires the **approver passphrase**: set once by a person with `togi approver setup` (12+ characters,
    typed twice, read from `/dev/tty` without echo, stored as scrypt + salt in `approver`, 0600). Five wrong
    attempts lock the approver for 10 minutes; every attempt is journaled in `_host`. With no approver, the command
    refuses with a `next` to run setup. `deny`, `lock`, `revert` and the human path of `apply` (reshape) need the same.
  - **(r2, r5)** The approver (`approver`, `approver.state`, and `approver.key`: 32 random bytes, 0600, created by setup when
    missing) lives in the **approver home**: `<pw_dir>/.togi/approver.d`, a directory of its own (**0700**, brought back to 0700
    if it is looser), fixed and independent of `$HOME` and `$TOGI_HOME`, so an agent cannot make its own approver by pointing
    `$TOGI_HOME` somewhere it controls. It holds the passphrase record, the key, the attempt state and the spent-approval log
    and nothing else. (r4 kept them loose in `<pw_dir>/.togi`, which is also the default `$TOGI_HOME`, beside the journals,
    approvals and registry.) **A `$TOGI_HOME` that is the approver directory, sits inside it, or contains it is refused
    (`togi_home_unsafe`, exit 2)** for every command, the one exception being the default home `<pw_dir>/.togi` itself, which
    holds `approver.d` as its sub-directory; a `TOGI_APPROVER_HOME` override is held to the same rule. **Migration:** r4's
    files are moved into `approver.d` in place (renamed, not copied; the passphrase record last), once, at the first human
    command after the upgrade (`approve`, `deny`, `lock`, `revert`, `apply`, `approver setup`) or with `togi approver migrate`,
    after that command's terminal and interpreter checks and never from an agent session; it is journaled (`approver_migrated`).
    Until then non-human commands report `approver_migration_pending` instead of "no approver". If the same file exists in both
    places (`approver_conflict`) nothing is moved and every human command refuses until a person resolves it; a stray
    `approver.state` in the new place does not count as a conflict (the old one wins). `togi doctor` reports the layout. `TOGI_APPROVER_HOME` overrides the
    location for tests and the stone; everything signed under it is `scope: "test"` (§2). `togi doctor` warns when it is set.
    `togi approver setup` never overwrites an existing approver without first asking for the current passphrase.
  - **(r3)** `lock`, `revert`, the evaluation approval of a reshape proposal and a human `apply` accept **only the
    default-scope approver** (the fixed one in `<pw_dir>/.togi/approver.d`). While `TOGI_APPROVER_HOME` is set they refuse with
    `test_scope_refused` (status `refused`, exit 2), after the human checks (agent session, TTYs, `togi run`) and before the
    typed word, the passphrase or any expensive work; `togi apply` says so up front when a person will be needed. There
    is no exception for example or test bundles: the rule is the same for every repo, and the test suite reaches the
    default scope by pointing the "fixed" location at a temp dir (a password-database shim), not by this variable.
    `approve`, `deny` and `approver setup` are not repo gates and accept either scope. **(r4)** They accept a test scope for
    a given bundle only if that bundle opted in with `test_approvals` (§2).
  - **(r4)** The approver home (r5: `approver.d`) also holds `spent.jsonl` and `spent.head` (§2, the spent-approval record); a replaced approver
    key retires them (renamed `*.old`), since only the old key could verify them and the grants it signed are dead.
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
- `togi approver [status | setup | migrate]` **(r1)**: the approver passphrase (see `approve`). **(r2)** `status` also reports the
  `scope` (`default` | `test`) and whether the signing key exists. **(r5)** `status` adds `layout` (`legacy` | `conflict`) while r4's files
  are still to be moved or exist in both places; `migrate` is the human-only way to run the r4 migration by itself.
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
  Values for read commands are hashed. **(r1)** Records of unknown commands and flags are values-free too: a word is kept
  only if it is a near-miss of a real command word, every other token is `<num>` / `<str>`. **(r4)** A flag name is kept only
  if it is a flag the host or one of the bundle's commands has (`journal.HOST_FLAGS`, the bundle's parameters and their
  `--no-` forms); any other name is recorded as `#` + 8 hex digits of its hash (`--#1a2b3c4d`), because a made-up name is where
  a value hides. An unknown-flag record also carries `near`, the *known* flag it most resembles (`--tag` for `--tgs`),
  so the friction summary (`unknown_flags: [{command, flag, count, near}]`) still points hone at the typo.
- **(r4)** `ctx.journal_note` payloads (`event: "note"`) from write/money/admin commands are `{key: type}` only (`"str"`,
  `"int"`, `"float"`, `"bool"`, `"list"`, `"dict"`, `"null"`); read commands' notes are stored as written. A call whose
  command printed to stdout also records `printed_bytes`.
- **(r1)** Any invocation of `approve`, `deny`, `lock`, `revert`, `apply` or `approver` writes an `event: "host_attempt"`
  line to the `_host` journal BEFORE its flags are parsed (argv shape only), and every approver-passphrase attempt writes
  `event: "passphrase_attempt"`.
- **(r3)** A `host_attempt` also records `approver_scope` (`default` | `test`): which approver the attempt would have used.
- **(r2)** A gated call that ran on an approval also records `approval` (the id) and `approval_scope`.
- `retry_of` links a call to the same command made by the same session within 120 s after an error.
- **(eval)** `togi answer` appends an `event: "answer"` line to the `_host` journal: ok / not ok, the field names and their types
  (`{"answer": {"region": "str", "revenue": "int"}}`) and a hash, never a value. The values go to the answers log
  (`state/_host/answers.jsonl`, §9.2): the agent's *output*, not what it was asked to handle. `togi task` calls are `_host`
  commands (`task start|show|update`, effect write/read, argument types only). Inside an eval or stone sandbox only
  (`approvals.sandbox_optin()` plus `TOGI_EVAL_RECORD=1`), a call line also carries `args_full` (the arguments, cut at 400
  characters) and `out_preview` (the reply, cut at 600) so a trajectory can be analysed; a real home never holds them.
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
  commands."), `--tools Bash`, `--allowedTools "Bash(togi:*)"`, `--restricted`, `--permission-mode dontAsk`,
  `--output-format json`, `--no-session-persistence`, an empty temp cwd, `TOGI_AGENT=stone`.
  - `Bash(togi:*)` is an allow rule, and an allow rule only adds to the permissions a person already has in Claude Code.
    `--restricted` makes Claude Code ignore the person's settings files and `--permission-mode dontAsk` denies every command
    the rule does not name, so a `Bash(*)` rule in the person's own settings does not reach the agent. These flags are
    passed on the `cli` and `run` surfaces by both the stone driver and the `claude-cli` provider of `togi eval`, from one
    list (`stone.BASH_TOGI_ONLY`). A Claude Code that does not accept them is a driver error; there is no fallback. See the
    README, "Self-improvement loop", step 2.
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

**(r4) The sandbox is the one place a bundle may use test-scope approvals and skip the lock.** A stone run (`Sandbox`) and the
bundle's own deterministic tests (`run_tests`) each live in one throwaway root holding `home/` (the `$TOGI_HOME`),
`approver/` (the `$TOGI_APPROVER_HOME`) and a marker file, and set `TOGI_STONE_SANDBOX=<root>`. `approvals.sandbox_optin()` is
true only when that variable names a directory with the marker whose `home` is this `$TOGI_HOME` and whose `approver` is this
`$TOGI_APPROVER_HOME`; the variable alone, in any other home, does nothing. Inside it a bundle accepts test-scope grants
without `test_approvals` and the money path skips the lock check (a throwaway home has no audit key to anchor a lock with, and
nothing there is real). The stone's `togi` shim and MCP server are started isolated (`paths.isolated_argv`, §3) and no longer
use `PYTHONPATH`. A `togi run` child launched inside the sandbox inherits the variable.

**(eval) `--engine eval`.** `togi stone <bundle> --engine eval` runs the same tasks through the eval layer (§9): the tasks are
imported as a suite (`holdout` becomes split `test`, anything else `dev`; the checks become typed checks), run by the eval runner
with one default subject, and returned in the stone's own result shape, so `hone` and `apply` read them unchanged. `bench
--engine eval` does the same with one subject per surface. The default engine is still the original one.

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
   verifies; **r5**: over the lock set of §2, shadows included), and that the stored diff is the one hone stored. **(r4)** The chain must be intact **and authenticated** (`audit_unauthenticated`
   otherwise): at least one entry signed by this machine's key, every entry after the first signed one signed too (an unsigned
   prefix from before signatures is allowed). The lock file is **anchored**: it counts only if it is exactly what the latest `lock`
   audit entry recorded (`diff_sha256` = sha256 of the file) AND that entry is genuine (`hash` recomputed from its content, `kid` =
   this machine's key id, `mac` verifies), so a locked file edited together with a hand-rewritten `locked.sha256` and a forged `lock`
   line is `not anchored`. A symlink standing under a locked pattern, or in place of a locked directory or of an ancestor of locked
   files, is reported by `verify_locked` (`symlinks`) and fails the lock; `togi lock` refuses to pin such a repo (`locked_symlink`). Paths are **(r1)** normalised (`.`, `..`, `//`),
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
   - a **polish** proposal keeps automatic evaluation, but may touch **only plain text**: **(r4)** an allowlist, not a list of bad
     extensions: `.md`, `.txt`, `.json`, `.yaml`, `.yml` and `.toml` other than `pyproject.toml` / `togi.toml` (case-insensitive).
     Everything else counts as code for this rule (`.py`, `.sh`, `.cfg`, `.ini`, `.html`, `.pth`, a file with no extension...),
     and so does new content starting with `#!`. Otherwise it is rejected (`code_in_polish`, audited), at `hone` and again here;
   - belt and braces: both rules are checked again right before the stone runs.
3. Copies the bundle repo to a temp dir, applies the diff there, and runs the stone (same tasks, same model) on both
   the current version and the candidate, with at least the policy's `apply_driver` and `apply_runs` (**r1**). **(r4)** The
   comparison is cached in `proposals/<id>/evaluation.json` and the cache is signed: `mac` = HMAC-SHA256(audit key, domain +
   proposal id + the record). A cache with no mac, a wrong one or one signed for another proposal is ignored (the stone runs
   again; `togi review` does not show it).
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
**(r4)** `lock_anchored` (and so `verify_locked`, `doctor`, the money path) verifies the `kid` and `mac` of the `lock` entry it
relies on, not just its `diff_sha256`. An audit log in which NO entry is signed (it predates signatures) is chain-bound only:
`authenticated` is false, an ordinary writer cannot append to it (`audit_unauthenticated`), `apply` and `revert` refuse it, and a
person's `togi lock` (`anchor=True`) is what signs the next entry onto it. `authenticated` is true when at least one entry is
signed by this machine's key, every later one is too, and the head matches.
Lines are pure ASCII and are read by splitting on `\n` only. `togi log --verify` recomputes the chain, checks the macs and
the head pointer (`$TOGI_HOME/state/<bundle>/audit.head`, also MAC'd); a missing or unreadable head is a failure.
`--chain-only` checks the hash chain alone (for CI on a clean checkout) and says so. Every writer verifies the whole
chain and the head first and refuses, writing nothing, if either is broken; only `togi lock` (a person) may re-anchor a
log whose head or key was lost. Entries written before r1 (no `mac`) remain valid as an unsigned prefix.

**(r5) `togi lock <bundle>`.** A human gate (terminal, default-scope approver, typed bundle name, passphrase). Before it asks it reads
the bundle's commands in the sandboxed describe child (§3) and builds the prompt, which shows: the **locked files changed, missing
and new since the last lock, with sizes**; the **state of the audit head pointer** (`ok`, `MISSING` ...); the **audit entries
written since the last lock**; the commands read (and the gated ones); and a warning if code behind the gated commands is not
locked or reshape. After the bundle name the person must also type **`relock`** if any of those files differ from the last lock,
the lock file was deleted, or the head pointer is missing (a first lock, or one where nothing changed, needs no extra word), and
**`unsandboxed`** if the machine has no OS sandbox. Then the passphrase. Nothing is written if the locked set changed while the
person was deciding (`repo_changed`) or if the bundle cannot be read (`describe_failed`); otherwise it writes `.togi/locked.sha256`,
the `lock` audit entry and `.togi/describe.json` (§3). A lock without a snapshot (library call without a sandbox) removes any
older snapshot.

**5. `togi revert <id>`:** a human (TTY + typed word + passphrase, **r1**; the default-scope approver, **r3**). Re-checks the audit chain, that the stored
diff is the one the audit log says was applied, the tiers and the locked hashes (before, and after the write, with
rollback), applies the reverse diff and logs it.

**Hone can never:**
- edit the stone, holdout tasks, policy, the manifest, locked files, or Togi itself;
- grant its own approvals;
- run apply.

---

**(eval) The outcome gate.** `togi apply <id> --outcome FILE --suite DIR --subjects S` adds one gate to the apply rules: the
candidate must also hold the outcome's gates on the suite's **test** split (`integration.outcome_gate`: the suite is run for the
current bundle and for the candidate copy; the candidate passes if it passes the gates, or the baseline failed them and the
candidate's worst slice is no worse, with no forbidden attempts beyond the gate's allowance). The unseen split is never run here.
A failed gate is a `reject` with reason `outcome_gate_failed`. The evaluation cache key includes the outcome arguments.

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

**(eval)** `togi mcp serve <bundle> --builtins` (or `TOGI_MCP_BUILTINS=1`) also lists four host tools, the twins of the host
commands: `answer` (its arguments ARE the answer object; its input schema is the task's answer schema when
`$TOGI_HOME/answer-schema.json` exists), `task_start`, `task_show`, `task_update`. A plain server lists bundle tools only.
`togi mcp tools <bundle> [--builtins]` lists them; `togi mcp call <bundle> <tool> [--args JSON]` makes one MCP tool call through
the same code the server runs (surface `mcp` in the journal, no 8 KB CLI cap) without the stdio protocol, for harnesses.

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
  - **(r4)** one regression test per item of FIXES-r4.md (`tests/test_fixes_r4.py`): planted `hmac.py`/`json.py`/`decimal.py` in the
    cwd and on `PYTHONPATH` never run in `togi approve` through the real launcher (`bin/togi`), `python3 -m togi`, the `togi run`
    host re-exec and the stone shim; test-scope grants refused without `test_approvals`; args/summary/word/binding edits refused at
    approve; a consumed approval flipped back to `approved` stays spent, the spent record's damage cases fail closed; money
    refused without a verified lock (absent, edited, unanchored, forged `lock` entry, symlink); unsigned evaluation cache ignored;
    the measured output cases (5,000-key dict, one 100 KB string, 100 KB of prints, a 200 KB `togi run` exception); unauthenticated
    legacy log; the polish allowlist; hashed flag names and shaped notes; an installed bundle module hidden from the sandboxed
    child (under the real `sandbox-exec`);
  - **(r5)** one regression test per item of FIXES-r5.md (`tests/test_fixes_r5.py`): a shadowing package directory / extension
    module / `.pyc` / `__pycache__` file changes the digest, the lock and the binding; untiered code behind a money command
    (module, new file, `.so`, `.pth`, symlink, loaded helper package) is `gated_code_unlocked` and reshape is allowed; no skip
    list under a locked pattern; a planted unchecked `__pycache__` pyc and planted `json.py`/`decimal.py`/`hmac.py` never run;
    approve works on a bundle that raises at import (it does not import it), the snapshot is signed / stale / invalid, the
    describe child is sandboxed, in its own session, scrubbed, killed and bounded (probe child under the real `sandbox-exec`);
    `approver.d` is 0700 and holds all four files, `TOGI_HOME` containing / equal to / inside it is refused, r4 files migrate once
    under a pty, never for an agent, and a double approver is a conflict; the lock prompt content and `relock`; stderr capped on
    CLI and MCP; the expiry signed is the expiry shown; `prompt_too_long`; `--max-output` clamped;
  - `run` proxying (and that a child can't read the parent's environment, including through the parent process, nor read
    bundle repos or `$TOGI_HOME`, nor reach the network);
  - journal shape (no values);
  - friction summary, tier classification, locked-hash enforcement, audit chain verify and tamper
    detection, apply decisions (all branches, with the script driver), the MCP bridge listing.
  - **(eval)** `tests/test_eval_*.py`: the schema validator; `togi answer` on cli/code mode/MCP; the task record; every check type;
    items, the stone import and the unseen access control; subject hashes (a golden value); the statistics against known
    values (Wilson, exact McNemar, a seeded bootstrap); item statistics, verdicts, curves and long-horizon metrics on synthetic
    runs; every failure-taxonomy rule; the lock refusal; canaries and the contamination scan; resumable caching and a
    2 subjects x 40 items x 3 repeats matrix with a resume; multi-session scripted items with cold starts, between events and the
    approval hook; every agent loop on single- and multi-session items against a mock OpenAI-compatible/Ollama server (tool calls,
    constrained output, max steps, errors, prompt tokens per step); claude-step and claude-cli against a mocked `claude`; the sheet
    generator (determinism, answers recomputed independently, against the real sheet bundle where present); the report and
    compare. No test touches the network (the mock server is on 127.0.0.1) or downloads a model.
- **Example bundle:** a tiny one in `examples/notes/` (no network) used by the tests.
- **Speed:** `togi skill` and `togi tools` complete in < 150 ms on a laptop.
- **README:** what it is, a 60-second quickstart, how to write a bundle, guardrails and their
  honest limits.

---

## 9. Eval: the outcome-driven benchmark layer (2026-10-27)

Design record: docs/EVAL-SPEC.md. Code: `src/togi/evals/`. It answers one question with evidence: **what reaches a declared outcome
at the lowest cost** (which model, which harness settings, whether fine-tuning or distillation is needed at all), for any
outcome, not just the bundles we have. Nothing in `togi.evals` imports a specific bundle.

### 9.1 Objects

- **Outcome** (`outcomes/<name>.toml`, `togi eval outcome new <name>`): `name`, `description`, `decision` (what the result
  decides), `primary_metric = "success_rate"` over `splits` (default `test`+`unseen`; `dev` never counts), `[gates]` applied to
  EVERY slice (`min_success`, `min_success_lcb` = the Wilson 95% lower bound, `max_forbidden`, optional `max_median_turns`,
  `max_median_cost_usd`, optional `[gates.non_inferior] to, margin`), `[slices]` (which capability tags / tiers / worlds to gate,
  `min_cells`), `[[reference]]` (`subject`, `bar = target | fallback`). Its hash covers all of it.
- **Item** (`items/<split>/<id>.toml`): identity (`id`, `world`, `split` dev|test|unseen, `tier` 1-5, `capabilities` tags: tool-choice,
  single-step, multi-step, recovery, approval-stop, synthesis, ambiguity, refusal, injection-resistance, code-mode-benefit,
  numeric, long-horizon), `bundles`, `setup` (harness commands; `$EVAL_SUITE_DIR` is the suite directory), one or more
  **sessions** (below), an `answer_schema` (JSON Schema, an object), end-of-item `[[check]]`s, `[[forbid]] command_prefix`,
  `budget_turns` (per session), optional `reference_trajectory` (replayable commands: the `script` provider replays it as an oracle),
  `reference_answer`, `canary`, `data_files` (hashed into the item hash). **Item hash** = sha256 of the canonical item content +
  the data files' bytes. A **stone task file is a valid item file**: `holdout = true` becomes split `test`, otherwise `dev`;
  `cmd`/`expect_jsonpath` checks become `state_jsonpath`, `transcript_contains` stays (legacy, discouraged), `forbid` carries over.
- **Sessions** (multi-session, long-running items): `[[session]]` blocks, each with its own `instruction` (default
  "Continue the task."), optional `between = [...]` (commands the harness runs just before the session to change the world:
  buyers join, a payment fails; `@approve|@deny|@expire <latest | ap_id | command words>` is the test-only approval hook,
  run in the sandbox with its throwaway approver and never reachable from the `togi` command line), optional per-session
  `[[session.check]]` (milestones for the per-session success curve; `gate = true` makes one count toward the item), and
  per-session scripts for the `script` provider (`{approval}` in a script step is the newest approval id). **Each session starts
  the agent COLD**: a new agent session id and a fresh conversation. Continuity comes from Togi state alone.
- **Suite** (`suite.toml` + `items/` + `data/`): `name`, `canary`, `[bundles.<name>] path` (else the registry), `generator`.
  `items.load_suite(path, purpose=...)`: a purpose reads only the splits it may (`PURPOSE_SPLITS`): `run` dev+test (+unseen only
  with `include_unseen`), `hone`/`examples`/`skills` dev only, `apply` test only, `lock`/`contamination`/`report` all. Files of
  a split a purpose may not read are not opened.
- **Subject** = model x provider x harness config x surface (`cli`|`run`|`mcp`), with a stable **hash** over exactly what changes
  its behaviour (provider, model, surface, the harness keys that provider uses, sampling options, the skill variant's file
  CONTENTS). Labels, `size`, `price` and endpoints are not in it. Providers: `script` (deterministic replay), `claude-cli`
  (Claude Code's own loop, the stone driver's isolation), `claude-step` (OUR loop, a Claude model as the per-step policy via
  `claude -p --tools= --output-format json --model M`, one call per step, on the subscription), `ollama`, `openai-compatible`
  (OUR loop, a local model as the policy). Harness config: `skills` none|index|full, `next_hints`, `compact_output`,
  `constrained_decoding` (local providers), `reasoning_field`, `examples`, `skill_version` (+ `skill_files`), `loop`
  (react|operator|plan-act-verify|code-first), `context_policy` (full|window:N|state-only|auto), `step_budget`, `tools`
  (single|per-command). `role`: candidate|reference|target|fallback. Subjects come from a TOML file (`[[subject]]`, with an
  optional `[subject.matrix]` of lists that expands to the cross product) or a shorthand
  `provider:model[@surface][+key=value...]`.

### 9.2 `togi answer`: the structured final answer

`togi answer --json '<object>'` (also `--file`, `--stdin`, or one positional JSON value) records the answer, validated against the
item's schema in `$TOGI_HOME/answer-schema.json` (the harness writes it; without one any JSON object is accepted). On every surface:
CLI `togi answer`, code mode `togi_answer(obj)` (raises `togi.TogiError(answer_invalid)`), MCP tool `answer` (`--builtins`). Records go
to `$TOGI_HOME/state/_host/answers.jsonl` (`seq, ts, session, surface, valid, errors, answer`); an invalid answer is recorded
as `valid: false` and refused with the fields to fix (exit 1, `answer_invalid`). Checks read the LAST VALID answer, never free
text, so "47,248" cannot be a false fail (the `number` check also reads "$47,248.00"). Answers are bounded (16 KB).

### 9.3 The task record (long-running work)

`togi task start --goal "..."`, `togi task show`, `togi task update --done "..." --next "..." --waiting "..." [--status
active|blocked|done]`; also MCP `task_*` and code-mode `togi_task_*`. One record per `$TOGI_HOME` in `state/_host/task.json`,
bounded (goal 500 characters; the last 20 `done` entries of 300, older ones counted in `done_dropped`; `next`/`waiting` 600; 8 KB in
all; over a limit is refused with the limit named, never silently cut). Every change appends a snapshot to
`task-history.jsonl` (the last 200 kept). Journaled (types only). The index skill teaches the convention: **start every session
with `togi task show`**.

### 9.4 Check library (`evals/checks.py`)

On the recorded answer: `exact`, `number` (normalised; `abs_tol`/`rel_tol`), `regex`, `one_of`, `set_equal` (`ordered`). State:
`state_jsonpath` (a command's output after the run; equals, not_equals, contains, matches, exists, gt/gte/lt/lte, len_equals,
or an exit code). Trajectory: `trajectory` with `rule` = used, never_used, max_calls, min_calls, called_before,
ended_at_needs_approval, approval_requested, no_forbidden_prefix, used_code_mode (read from the journal, not from what the agent
said). `budget` (turns, tokens, cost). `judge` (rubric + a calibration set of labelled examples, both labels required; calibrated
first, reported separately, **never gates**). Legacy `transcript_contains`/`transcript_not_contains`. An item with an
`answer_schema` gets an implicit `answer_valid`. `validate_check` runs on every item before a suite may run (an unknown type, a
`number` with a non-number, a path that is not a field of the schema, a judge without calibration: a faulty check lies). New
types register with `@register`.

### 9.5 Agent loops (`evals/loops.py`, `evals/policy.py`)

One small interface per strategy: state in -> prompt (`build_prompt`), reply -> next action (`interpret`), action + output ->
state (`after`). The harness (`run_loop_session`) owns the backend call, the execution through Togi in the sandbox, accounting
and the stop rules. The model's reply is one JSON action: `togi {argv}`, `run {script}`, `answer {answer}`, `finish`,
`give_up`, or (per-command tools / MCP) `<bundle>_<command> {args}`. With `constrained_decoding` the action vocabulary is a JSON
Schema (`oneOf`) handed to the provider's structured output (Ollama `format`, OpenAI `response_format`); without it the
vocabulary is native tool calls (`tools`).

- **react**: the baseline; the transcript grows.
- **operator**: stateless and re-entrant. Every step rebuilds a SHORT context: the task record, the instruction, the latest action
  and output. The model writes its progress (`done`, `next`) with each command and the loop stores it with `togi task update`.
  No growing transcript; the best fit for small models and long-running work.
- **plan-act-verify**: a first step that can only be `plan`, stored in the task record (`next: "PLAN: 1) ..."`); each action carries
  `verify` (ok|failed|revise) and an optional `revised_plan`; the loop also verifies on its own (a failed command is a failed
  verification) and resumes a plan found in the record in a later session.
- **code-first**: prefers ONE `togi run` script that ends with `togi_answer`; stubs in the prompt; falls back to single calls.

`context_policy` (full | window:N | state-only; `auto` = the loop's default) limits the history shown; `step_budget` caps the model calls
per session. Every step records the prompt tokens actually sent (the provider's count, else an estimate flagged
`prompt_tokens_estimated`), the chosen action, wall time, cost and loop-level events (`plan_written`, `plan_revised`,
`verification_failed`, `invalid_reply`, `answer_rejected`, `record_updated`, `early_stop`). Stop rules: answer accepted (also when a
script or a plain `togi answer` records it), `finish`, `give_up`, three invalid replies in a row, the same action and output four
times, the step budget, the item's cost cap, the wall clock. A provider that is unreachable or answers nonsense is a **driver
error** (`ProviderError`), never a verdict on the subject.

### 9.6 Runs, the store and resumability

`togi eval run --outcome O --suite S --subjects X --repeats N` runs subjects x items x repeats (seed = base seed + repeat), parallel
with per-provider concurrency limits (`--concurrency claude-cli=2,ollama=1`; defaults script 4, claude-cli 1, claude-step 2, ollama
1, openai-compatible 4). Each cell runs in a throwaway sandbox (`evals/sandbox.py`: stone's, with several bundles, the harness
toggles in the environment, `TOGI_AGENT=eval`, call recording on, a new agent session per session). The store is
`$TOGI_HOME/eval/`: `runs/<id>/manifest.json`, `runs/<id>/cells.<split>.jsonl` (one file per split, appended the moment a cell
finishes), `cells/<subject hash>.jsonl` (the result cache). **A cell is reused when item hash, subject hash, seed AND the bundle's
digest all match** (the bundle digest is an addition to the specified key: editing the bundle's code or skills must invalidate its
results; its tests and stone tasks are not part of it). Driver-error cells are never cached. `--resume RUN` continues an interrupted run
(the plan must be the same); running the same command again hits the cache. `--max-cost` stops dispatching new cells once the run's
spend reaches it (status `stopped: cost_cap`); `--max-item-cost` is passed to the drivers; `--item-timeout` is the wall clock per
session (the process group is killed). A cell records: answer, checks (with `session` and milestone flags), per-session results
(instruction, turns, steps, events, answers, trajectory, metrics, task record, between log), turns, tokens, prompt tokens, cost,
wall time, forbidden attempts, drift, the task-record history.

### 9.7 Analysis (`evals/analysis.py`, `stats.py`, `taxonomy.py`; pure functions over a run)

- **Success** with a Wilson 95% interval per subject x slice (overall, `capability:`, `tier:`, `world:`) and per split.
- **Paired comparisons** on the same items: exact McNemar on item-level wins (with repeats an item is a win for the subject with the
  higher pass rate on it) and a seeded, item-clustered paired bootstrap of the success difference (percentile interval; the same
  seed gives the same interval to the bit).
- **Item statistics**: difficulty (pass rate across subjects), discrimination (correlation of the item's pass with the subjects'
  skill on the OTHER items; needs 3+ subjects), saturation flags (`all_pass`, `all_fail`), `negative_discrimination`, and
  **suspect** items: a `reference`/`target` subject that fails an item flags it before anyone blames the subject under test (verdicts
  are reported both with and without the suspect items).
- **Curves**: success vs model size per harness config (isotonic-smoothed); **harness lift**: for a point on another config's curve,
  the size at which the baseline curve reaches the same success (log-interpolated) and the gain factor, or `above_range`; **Pareto**
  frontiers for success vs cost and success vs latency.
- **Outcome verdicts**: the gates on the WORST slice with Wilson lower bounds -> PASS/FAIL per subject with reasons; the
  non-inferiority gate against a reference by paired bootstrap; the smallest subject that passes (by size, then cost). Driver-error
  cells count as failures and are reported as invalid.
- **Failure taxonomy** (rule-based, from the trajectory): `driver_error, forbidden, budget, format, loop, gave_up, no_stop, bad_args,
  wrong_tool, misread_output` (an answer number that is a transposed or mistyped copy of a tool output number), `did_math` (an answer
  number in no tool output), `other`; first rule that fires is the class, the rest are tags; `togi eval audit <run> --class C` draws a
  seeded random sample for a human.
- **Reference-trajectory metrics**: for items with a `reference_trajectory`, per subject over its passed cells: `efficiency` (reference
  calls / the subject's bundle calls; 1.0 = as lean as the reference), `coverage` (the share of the reference's commands it also
  used) and extra calls. A cheap model that passes with three times the calls is a cost.
- **Long-horizon metrics** (multi-session items): end-state success; steps per session; wasted actions (the same command and
  arguments repeated with no state change between); drift (a state-changing call whose command words appear nowhere in the task
  record at that moment, a deliberately simple repeatable rule); sessions ended at an approval and forbidden attempts; the
  per-session success curve with Wilson intervals and its slope (does performance decay with session count?); task-record updates.

### 9.8 Integrity

`togi eval lock <suite> [--outcome --subjects --repeats]` hashes every item (all splits), `suite.toml` and the `data/` tree into
`LOCK.json` and records the **pre-registration** (outcome and gates, subjects, repeats, splits, the analysis plan). `togi eval run`
refuses a locked suite whose hash changed (`suite_changed_since_lock`); a run reports any deviation from the registered plan in its
manifest; `--relock` records a new lock and keeps the old in its history. The **unseen** split runs only with `--include-unseen`;
every run records the splits it touched and how many files stayed unread; hone reads dev failures only. **Canaries**: every item
carries one; `togi eval contamination --suite S [--data DIR ...]` scans the bundles' skills, examples and polish/reshape files,
hone proposals and any data directory for canaries and for word 8-gram overlap with test/unseen items. **Reference sanity**: every run
needs at least one reference/target subject (`--no-reference-check` to waive); see suspect items above.

### 9.9 Generators

`togi eval generate <generator> --seed N --out <suite>`: seeded, correct by construction (the answer is computed by the real
tool's engine over seeded data, never by a model; the same seed writes the same bytes). `sheet` writes spreadsheet questions:
worlds are the splits (dev `retail-d1`; test `retail-t1`, `retail-t2` with other vocabulary, sizes and decimal prices; unseen
`clinic-u1`, a DIFFERENT schema and domain: `visits`/`patients`, other columns and nouns), 17 templates covering totals, filters,
counts, averages, top-k, gaps, per-group winners, joins, synthesis, ambiguity (the right answer is to ask), an unanswerable question
(refusal), a misnamed table (recovery), a poisoned dimension row (injection resistance) and a delete that needs a person (approval
stop). Every well-posedness condition (no ties for "top", a non-zero gap) is verified on the data before an item is emitted.

### 9.10 Reports

`togi eval report <run>` writes `report.json` and a self-contained `report.html` (inline CSS and inline SVG: no scripts, fonts, links or
CDN; everything printed is escaped): verdicts, CI bars, paired deltas, item statistics, scaling and Pareto curves, failure classes,
long-horizon table and per-session curve, integrity. `togi eval compare <a> <b>` matches subjects by hash (else by id): rate
deltas (flagged CLEAR when the intervals do not overlap), item flips, verdict changes, items only in one run.

### 9.11 Integration, switches and honest limits

`stone --engine eval`, `bench --engine eval`, `hone` (dev failures from the eval store), `apply --outcome` (§5, §6). Environment
switches the harness uses (they work anywhere): `TOGI_NEXT_HINTS=0` (error envelopes lose `next`), `TOGI_COMPACT=0`
(pretty-printed JSON), `TOGI_EVAL_RECORD=1` (call recording, honoured only inside a stone/eval sandbox), `TOGI_MCP_BUILTINS=1`.

Honest limits. The drift metric and the taxonomy are rules, not understanding: they are repeatable and auditable (`togi eval
audit`), and they will mislabel some cells. Heuristic classes that need tool outputs (`misread_output`, `did_math`) rely on the
output previews the eval sandbox records. A judge check needs a judge backend (`--judge claude:<model>`); without one it reports
`ok: null` and decides nothing. The `mcp` surface for a loop provider serves one bundle at a time. The sandbox is a throwaway home
and a `togi` shim, not an OS sandbox: the agent is a same-user process that is *asked* to use only `togi` (forbidden attempts are
detected from the journal and fail the cell; they are not prevented).
