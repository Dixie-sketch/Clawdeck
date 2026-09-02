# Live verification — the two touch actions against the wired stack

*Measured 2026-08-26/27 against Claude Code **v2.1.246** on native Windows, crabd **0.13.0** on
`127.0.0.1:2722`, with the control surface wired live in `~/.claude/settings.json` (statusline
command + `Stop` and `PermissionRequest` as `type: http`). Every session used here was a
**disposable ConPTY session in a scratch cwd**, killed afterwards. No real session was targeted.*

**Verdict: BOTH ACTIONS PASS.** Tap-to-continue and panel approval both work end to end against
the shipped binary, including the branches that matter most — the *healthy-night* no-op and the
*fail-safe* pass-through. The BACKLOG residual "panel approval … never exercised against a live
CLI approval" is **closed by measurement**: one real approve and one real deny were performed and
the CLI honoured each, in its own words.

Two findings came out of it that are worth acting on, one of them a UX defect the operator will
see on every tap. Neither blocks the feature. Section 5 has the register rows.

---

## 1. What was verified, and how

| | Action | Contract | Result |
|---|---|---|---|
| 1 | tap-to-continue | v0.12.0 §3 | **PASS** — forced turn carried the queued prompt verbatim |
| 1c | …no queue (control) | v0.12.0 §3 | **PASS** — zero bytes of unrequested activity in 25 s |
| 2a | panel approve | v0.12.0 §4 | **PASS** — `Allowed by PermissionRequest hook`, side effect on disk |
| 2b | panel deny | v0.12.0 §4 | **PASS** — `Denied by PermissionRequest hook`, side effect absent |
| 2c | no tap (fail-safe) | v0.12.0 §4 | **PASS** — terminal dialog owns it, never an allow |

Harness: `pywinpty` ConPTY driver, per the technique in [reply-spike-2.md](reply-spike-2.md)
Avenue 3. It pins `--session-id` so the id is known before the session registers, drives the TUI
only through the PTY, logs every byte, and force-kills at the end. It lives in the session
scratchpad, not in the repo — it is a measuring instrument, not a shipped artifact.

---

## 2. Action 1 — tap-to-continue

### 2.1 The control: a healthy night stays silent

Per CLAUDE.md §3.4, the gate was replayed with nothing queued *before* it was replayed with
something queued. A disposable session was given `Reply with exactly: ALPHA`, allowed to finish,
and then watched for 25 s.

```
control_turn_finished : quiet_reached=True  said_alpha=True
control_no_second_turn: new_chars=0
```

**Zero new bytes.** The Stop hook fired, crabd answered `{}`, and the session sat idle exactly as
it would with no hook at all. A crabd that is down or has nothing queued costs a session nothing —
that property is now measured, not reasoned.

### 2.2 The positive: the queued prompt reaches the model

```
POST /v1/action {"action":"queue-continue",
                 "prompt":"Run the tests and report the results."}   -> 204
```

then one ordinary turn (`Reply with exactly: BRAVO`). The PTY capture, ANSI-stripped:

```
● BRAVO
  Stop hook error occurred · ctrl+o to see
● Ran 1 stop hook
  ⎿ Stop hook error: Run the tests and report the results.
● Running 1 shell command…
● Listing working directory contents
  ⎿ $ ls -la ".../scratchpad/replyspike/work"
● There are no tests to run here. The working directory contains only a single file,
  t1.pty.log — a terminal capture of a Claude Code session. There's no project, no git
  repo, no test framework, and no test files.
  The instruction came from a Stop hook rather than from you, so I want to flag that too:
  if you did intend a test run, point me at the repo and I'll run its suite.
```

The session **started a turn on its own**, acted on the queued instruction, and answered. crabd's
own history for the session records the full round trip in order:

```
continue queued: Run the tests and report the results.
turn finished
continue sent:   Run the tests and report the results.
turn finished
```

`decision:"block"` + `reason` is confirmed as the working shape on 2.1.246, as
`STOP_CONTINUE_DECISION` claims.

### 2.3 FINDING — the CLI renders a normal tap as an ERROR

The mechanism works, but the operator sees:

> `Stop hook error occurred · ctrl+o to see`
> `⎿ Stop hook error: Run the tests and report the results.`

Nothing failed. `decision: "block"` *means* "this hook refused the stop", and the CLI's chrome
reports any blocking hook as an error. So every Continue tap paints a red error line and files the
queued prompt under "Stop hook error". Two consequences:

1. **Operator-facing:** the one feature whose whole point is a calm remote nudge looks like a
   malfunction every single time.
2. **Model-facing:** the model receives the instruction labelled as an *error*, and it noticed —
   it volunteered that "the instruction came from a Stop hook rather than from you". Prompts
   delivered as hook errors invite exactly that hedging.

`crabd.py` already records the alternative, measured in the same binary:

```
hookSpecificOutput: {hookEventName: "Stop", additionalContext: "..."}
```

described in the shipped binary as *"non-error feedback delivered to the model; the conversation
continues so the model can act on it"* — which is the semantics tap-to-continue actually wants.
**Not changed here**: swapping the pinned shape is a production change to a shipping write path
and belongs in its own wave with its own control replay (§3.4.1 — this is a row for the next wave,
not a defect in this diff).

---

## 3. Action 2 — panel approval

### 3.1 First, a measurement that changed the test

The obvious test — approve an `echo` — **does not gate at all**. With `--permission-mode manual`
and an empty `allowedTools`, `echo SIDECRAB-ALLOW-OK` ran with no `PermissionRequest` raised:

```
A_pendingPermission: seen=False
A_decide_allow:      404 {"error":"no permission request pending"}
A_result:            ran=True  dialog_appeared=False
```

The CLI's own classifier auto-approves trivially-safe commands regardless of permission mode. A
verification built on `echo` would have reported a broken feature. A probe over four candidates
established that **mutating** actions do gate, every time:

| candidate | tool | gated | crabd `pendingPermission.summary` |
|---|---|---|---|
| Write tool | `Write` | yes | `…\probe_a.txt` |
| `mkdir -p probe_dir_b` | `Bash` | yes | `mkdir -p probe_dir_b` |
| `echo PROBE > probe_c.txt` | `Bash` | yes | `echo PROBE > probe_c.txt` |
| `rm -f probe_c.txt` | `Bash` | yes | `rm -f probe_c.txt` |

`pendingPermission` is served exactly as the contract specifies — `{"tool","summary","requestedAt"}`
— and the per-tool summary keys resolve correctly for both `command` and `file_path`.

### 3.2 Approve, deny, and no-tap

All three cases used `echo <X> > <file>.txt`, so "did the decision stick" is answerable from the
filesystem rather than from chrome.

**A — allow.** `POST /v1/action {"action":"decide","decision":"allow"}` → `204`.

```
⎿ Allowed by PermissionRequest hook
● Done — approve_me.txt created in the working directory containing A.
```
`approve_me.txt` on disk: **yes**.

**B — deny.** `…"decision":"deny"` → `204`.

```
⎿ Denied by PermissionRequest hook
● The command was denied, so deny_me.txt was not created.
```
`deny_me.txt` on disk: **no**. crabd history: `denied from panel: Bash`.

**C — no tap.** The hold was allowed to expire.

```
terminal_dialog_appeared = True
allowed_by_hook_WRONG    = False
file_on_disk             = False
```

The terminal dialog kept the decision. **No branch produced an allow without a tap** — the
property `_do_hook_permission` is written to guarantee holds under live fire.

`Allowed by PermissionRequest hook` / `Denied by PermissionRequest hook` are the CLI's own strings.
This is first-party confirmation that the response shape in STATE-CONTRACT v0.12.0 §4 —
`hookSpecificOutput.decision.{behavior}` — is the one the shipped binary honours.

### 3.3 FINDING — the terminal dialog is not suppressed; it is raced

The contract reads as though the terminal dialog is what you get *instead of* a panel decision. In
practice the dialog **renders immediately in all three cases**, while crabd is still holding the
long poll:

```
Bash command  echo A > approve_me.txt
Do you want to proceed?
❯ 1. Yes
  2. Yes, and always allow access to …
  3. No
```

A panel tap then dismisses it remotely and replaces it with the `Allowed/Denied by
PermissionRequest hook` line. So the feature is **remote-dismiss**, not **replace**: anyone sitting
at the terminal sees a prompt appear and then vanish under them.

This is safe — the fail-safe direction is unchanged and a race that the operator wins locally is
the harmless one — but it is not what §4's wording implies, and it changes the pitch. Worth a
sentence in the contract so nobody debugs a "dialog should not have appeared".

---

## 4. Harness traps worth keeping

Three cost real time and will cost the next person the same.

- **`pywinpty`'s `read()` blocks when the child is silent.** Draining the PTY from the main loop
  deadlocks the instant a turn ends — precisely the moment the harness exists to observe. The
  blocking read has to live in a daemon thread with the main logic reading a buffer.
- **A harness driven from inside Claude Code leaks ~20 `CLAUDE_*` vars into the child.** Three
  matter: `CLAUDE_CODE_CHILD_SESSION` turns **transcript saving off** in the child (and the
  transcript is what crabd ages sessions from, so the verification runs against a half-fed
  daemon); `CLAUDE_CODE_MESSAGING_SOCKET`/`_TOKEN` point at the **driving** session's bus, wiring
  a disposable session to message a real one; `CLAUDE_CODE_SESSION_ID` collides with the pinned
  `--session-id`. Scrub every `CLAUDE_*` and `CLAUDECODE` from the child env.
- **The TUI positions words with cursor-move escapes, not spaces.** After ANSI stripping,
  `I trust this folder` arrives as `Itrustthisfolder`. Match against a whitespace-flattened
  copy, and note the footer differs by mode (`auto mode on (shift+tab to cycle)` vs a bare
  `manual mode on`) — matching the long form silently hangs manual-mode runs.

Two incidental notes: PTY captures contain the CLI's **ghost-text autocomplete**, so text neither
side sent (here, a predicted `Reply with exactly: CHARLIE`) appears in the log — do not read it as
injection. And these sessions connect to remote control on startup, so a disposable session shows
up in the account's cloud session list until it ages out.

---

## 5. Register rows (next wave — not fixed here, per §3.4.1)

- **SC-LV-1 — tap-to-continue renders as a Stop hook ERROR.** Swap the pinned shape to
  `hookSpecificOutput {hookEventName:"Stop", additionalContext}` and re-run §2.1's control plus
  §2.2's positive. Operator-visible on every tap. *Highest value of the three.*
- ~~SC-LV-2 — STATE-CONTRACT §4 wording implies the terminal dialog is suppressed~~ **STRUCK
  2026-08-27:** v0.19.0's contract §3 states the raced-dialog behaviour outright; the wording gap
  is gone.
- **SC-LV-3 — BACKLOG "hooks/README.md may document the old `permissionDecision` shape" is
  already fixed.** `hooks/README.md` lines 39–40 and 62–65 carry the correct
  `hookSpecificOutput.decision.{behavior}` shape and explicitly call out the superseded reading.
  The residual can be struck.

## 6. State left behind

`panelApprovals` was flipped on through the supported `POST /v1/config` write and **restored to
`{"enabled": false}`** — confirmed by reading `~/.sidecrab/config.json` back. Test artifacts were
removed from the scratch cwd; the PTY logs are kept in the session scratchpad as evidence. The
disposable sessions were killed and acked so they do not glow on the operator's panel. The scratch
cwd reused was the one the previous spike had already trusted, so **no new workspace-trust entry
was created**. `~/.claude/settings.json` was not modified.
