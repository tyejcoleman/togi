# Togi v0.2: the service split (plan)

## Why
v0.1's guarantees hold only while the agent reaches tools through Togi's own surfaces: a shell limited to `togi …`,
`togi run`, or MCP. An agent with an unrestricted shell running as the same OS user can:
- edit Togi's own source;
- read `$TOGI_HOME` (audit key, approval records);
- rewrite anything that user can.

No in-process check can stop the same user from changing the checker. Six rounds of review kept finding new forms of
this one fact (planted bytecode, redirected homes, forged records). The fix is a process boundary, not more checks.

## Shape
```
 agent (any shell, any user)          person (their own terminal / phone)
        │  togi … (thin client)                  │  togi approve … (client)
        ▼                                        ▼
   ┌───────────────── unix socket (0600, owned by the service user) ─────────────────┐
   │ togid: runs as the PERSON's user (or a dedicated `togi` user)                   │
   │  - holds the approver key, the audit key and the spent log (never readable by   │
   │    the agent's user)                                                            │
   │  - imports bundle code only from read-only, locked copies it made at lock time  │
   │  - executes every command; returns envelopes                                    │
   │  - approvals: a person authenticates to togid (passphrase, or Touch ID via      │
   │    LocalAuthentication, or a phone page), never through the agent's process     │
   └─────────────────────────────────────────────────────────────────────────────────┘
```

**What changes:**
- **The agent's `togi` is a client.** It forwards argv over the socket. Skills, tools, code mode and MCP all keep
  their current shape for the agent.
- **The agent runs as its own OS user,** or in a container that can reach only the socket. Bundle repos are
  read-only to it.
- **Lock means a snapshot.** `togi lock` copies the locked code into a togid-owned store (content-addressed by
  hash). togid runs that copy, so editing the repo afterwards changes nothing until a person locks again. This
  replaces "hash and compare" with "run what was hashed".
- **Approvals are out of band.** Default is the person's terminal client authenticating to togid. Options: macOS
  Touch ID, or a one-time link to a local web page. The agent's process is never part of the approval path.
- **The self-improvement loop runs inside togid.** It runs against throwaway copies. Applying a change produces a
  new locked snapshot only through the human gate.

**What stays the same:**
- the bundle contract (SPEC §2);
- the tiers and the stone;
- the audit format;
- the agent-facing commands.

## Cost and order
- **togid with a unix socket, plus the thin client:** about 2 days. Most of the host is reused; the socket is a
  JSON-lines protocol.
- **Snapshot lock store:** about 1 day.
- **Out-of-band approval (terminal client first):** about 1 day. Touch ID later.
- **Install script for a separate agent user on macOS:** about half a day. It uses `dscl`, so a person runs it.

This is post-hackathon work. For the hackathon demo, the agent runs with `--allowedTools "Bash(togi:*)"` together with
`--restricted --permission-mode dontAsk` (an allow-list alone only adds to the person's own Claude Code settings; see the
threat model in the README). The stone and `togi eval` pass the same flags. That is inside v0.1's guarantees.
