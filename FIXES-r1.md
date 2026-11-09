# Togi fix round 1 (from the Opus refuter, 2026-10-27)

Reproductions: the review's probe scripts, which are not kept in this repository.

Each fix needs a regression test that fails before the fix and passes after.

## High
1. **Approval can be granted by an agent through a pseudo-terminal.**
   - Repro: `(sleep 1; printf 'n_1\n') | script -q /dev/null togi approve ap_…`.
   - Fix: approvals require a secret the agent never sees:
     - `togi approver setup` asks a person for a passphrase (≥ 12 chars) twice, read from /dev/tty without
       echo, and stores an scrypt hash plus salt in `$TOGI_HOME/approver` (0600).
     - `approve`, `deny`, `lock`, human `apply` (reshape) and `revert` require that passphrase in addition to
       the typed confirmation word.
     - Rate-limit wrong attempts: after 5 wrong, lock for 10 minutes; every attempt goes in the journal.
     - With no approver configured, approve refuses with a `next` telling the person to run setup.
   - Keep the TTY check as a second layer.
   - README: say plainly what this does and doesn't stop. A passphrase the human types into the agent's own
     chat is compromised. A same-user process can read `$TOGI_HOME`, but scrypt with a strong passphrase
     makes the hash useless to it.
2. **Locked files can be overwritten via letter case.**
   - `Manifest.classify` must normalise paths before matching: resolve `.`, `..` and duplicate slashes,
     reject anything outside the repo, reject control or Unicode line/paragraph separators, and casefold for
     matching (casefold both pattern and path).
   - `apply` must re-verify the locked hashes after writing. If any changed: roll back the write, record an
     audit reject, and exit refused.
3. **The audit log can be laundered.**
   - `audit.append` must verify the whole chain and the head pointer before appending. If broken, refuse and
     write nothing.
   - Add an HMAC per entry with a key in `$TOGI_HOME/audit.key` (0600, created at first `togi lock`), so
     someone with only the repo cannot reseal it.
   - A missing or unreadable head file is a verify failure, not a skip.
   - Write audit lines with `json.dumps(..., ensure_ascii=True)` and read by splitting on `"\n"` only.
4. **`togi run` children can read the parent's environment** (`ps -wwE -p $PPID`, Linux `/proc/<pid>/environ`).
   - `togi run` must re-exec the host with a scrubbed environment (allowlist PATH, HOME, TMPDIR, LANG,
     LC_*, TOGI_*, and anything listed in a new manifest `[policy] run_env` allowlist) before starting the
     child. The visible parent environment then holds no secrets.
   - On macOS, when `/usr/bin/sandbox-exec` exists, run the child under a profile that denies network,
     process-info for other processes, and file reads outside the Python install, the bundle repos and its
     temp dir. Fall back cleanly otherwise and report `sandboxed: false` in the output.
   - The child never needs bundle code, because every call is proxied to the host. So the sandbox profile
     must also deny reads of every registered bundle repo (including `.env` files), `$TOGI_HOME`, and the
     user's home. Allow only the Python install, Togi's own `_runchild` stub, and the child's temp dir.
     Otherwise a script can import a bundle's engine and move money directly without approval. That was
     shown by the Crowd refuter: `togi run -` importing `crowd.engine` and calling `settle()`.
   - Fix the unit test to check the parent-process route too.
   - README: being a same-user process, `togi run` is only as isolated as that profile; say so.

## Medium
5. An empty `TOGI_AGENT` counts as an agent: check `"TOGI_AGENT" in os.environ`.
6. The `togi run` ancestry check: match `togi` with any flags before `run`.
   - Better: `togi run` writes `$TOGI_HOME/run/<pid>`, and `approve` refuses if any ancestor pid is listed.
7. The approval prompt must escape control characters, ANSI escapes and bidi overrides in the summary and
   args (show them as `\u…`). Cap the summary at 300 chars.
8. Diff paths in `hone`/`apply` with control or Unicode separators are rejected (same normaliser as #2).
9. Forbidden-attempt detection:
   - Any invocation of `approve`/`deny`/`lock`/`revert`/`apply`, including ones that fail flag parsing, is
     written to the `_host` journal before parsing, as argv shape only.
   - The stone's forbidden check reads every journal in the sandbox home, all sessions.
10. The stone sandbox leaks: `togi run` children must get `TOGI_HOME`, `TOGI_SESSION` and `TOGI_AGENT` from
    the host, so nested `togi` calls stay in the sandbox.
11. Apply evaluation:
    - A proposal touching any skill file can't be auto-applied on a script-driver evaluation. It needs an
      agent driver (`claude`); otherwise it waits for a human.
    - Manifest `[policy] apply_driver` (default `claude`) and `apply_runs` (default 3) set the minimum; a
      caller may raise them, not lower them.
    - Skipped tasks count as failed for the decision.
    - A turns/tokens/cost improvement only counts at ≥ 15% median improvement and the candidate's worst run
      no worse than the baseline's median; otherwise `tasks_gained ≥ 1` is required.
12. `revert` re-checks tiers and locked hashes just as `apply` does.
13. The journal must not record values in unknown-command or unknown-flag records: keep flag names, and
    replace other tokens with their type (`<num>`, `<str>`).
14. Output caps:
    - Apply the ~8 KB cap (with a `more`/`--full` hint) to error envelopes, `--dry-run`, every host command,
      and MCP results (MCP: truncate with a note).
    - Honour `--fields` on host commands where meaningful.
    - Cap pending approvals per bundle at 20, refusing further ones with a clear `next`.
15. Approvals bind to the bundle's repo path, manifest version and locked-hash digest as well as
    command+args. The prompt shows the repo path.

Out of scope this round (document as known limits): a hostile same-user process with full file access, and OS-level isolation beyond sandbox-exec.

## From the live benchmark (bn_20261026-212402_8a9c)

16. **The MCP surface is broken under the claude driver.**
    - In stone run st_20261026-204203_9503, 0 of 14 runs passed. The agent saw no usable tools, called
      tools that don't exist (`togi_skill`), and asked for the server to be connected. One run took 385 s
      and cost $0.17.
    - Fix the bridge and the stone's MCP wiring:
      - the server must start reliably and quickly;
      - expose a `<bundle>_skill` tool (args: optional topic) so progressive disclosure works over MCP;
      - give the mcp surface a system prompt that names the real tool prefix (`mcp__togi__…`) instead of
        "run `togi skill`".
    - Verify with ONE real `claude -p` run on model haiku (one task) before declaring it fixed.
17. **The benchmark must use the same driver for every surface.**
    - It defaults to `claude` whenever more than one surface is benchmarked. `--driver script` is only
      allowed for a single surface.
    - The cli row in the run above used the script driver while run and mcp used claude, so it wasn't
      comparable.
    - Add a per-task wall-clock timeout (default 180 s) and a per-task cost cap passed to the driver.
