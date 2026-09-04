# SideCrab notifier

A native desktop alert when the panel is out of view and a Claude session has been waiting
too long. Standalone, read-only consumer of the crabd feed — it polls `/v1/state`, decides,
and fires **at most one alert per waiting spell**.

Zero pip dependencies. Python 3.13 stdlib, plus Windows PowerShell 5.1 on Windows and
`/usr/bin/osascript` on macOS — one adapter each, chosen from `sys.platform`. Everything
outside those two adapters is shared and platform-free; the macOS differences are in
[macOS](#macos).

---

## Behavior

Every 10 s the notifier GETs `http://127.0.0.1:9999/v1/state` and toasts a session when
**all** of these hold:

| Condition | Detail |
|---|---|
| `state == "needs_input"` | `working` / `done` / `idle` / `gone` are ignored |
| unacked | `acked: true` means the operator already saw it on the widget |
| aged past the threshold | `now - stateSince >= thresholdSec` (default 120 s) |
| not already toasted | dedupe key is `(sessionId, stateSince)` |
| not quiet hours | `quiet.active == true` suppresses everything |
| enabled | `toast.enabled` in config |

The toast reads:

```
Claude is waiting — <session title, trimmed to 48 chars>
<question text, trimmed to 140 chars>
SideCrab
```

Body falls back to `lastEvent`, then to a generic line, when `question` is absent (schema 1
feeds have no `question` field). Nothing is ever fabricated.

### The six toasts

| Toast | Fires when | Deduped by |
|---|---|---|
| **Waiting session** | a `needs_input` session ages past `thresholdSec` (120 s) | `(sessionId, stateSince)` |
| **Long run** (v0.16.0) | a session finishes a turn that ran longer than `longRunSec` (900 s) | `(sessionId, turnStartedAt)` |
| **Approval** (v0.15.0) | a `pendingPermission` sits undecided past `approvalThresholdSec` (20 s) | `(sessionId, requestedAt)` |
| **Daily digest** (v0.8.0) | the configured local time passes | calendar day, on disk |
| **Budget crossed** (v0.10.0) | `burn.budget.todayPct` first reaches 1.0 | calendar day, on disk |
| **Companion outage** (v0.15.0) | crabd stops answering, or its feed freezes, while someone was working | one per outage, re-armed only by a recovery |

All six share one rule: **quiet hours suppress AND mark**, never defer. The **Snooze**
button (v0.16.0) is the single deliberate exception, and it is the operator's own instruction
rather than a condition — see [Snooze](#snooze-30m-v0160).

All six are also gated by **`toast.enabled`**, with no exemptions, since v0.19.0 — see
[Config](#config).

### Dedupe — one toast per waiting spell

The ledger key is `(sessionId, stateSince)`, and it is **permanent** for the life of the
process. Consequences worth knowing:

- A session waiting for an hour toasts **once**, not thirty times.
- A **new question** moves `stateSince`, which re-arms — that is the only thing that does.
- An **ack** resolves the spell permanently. If `acked` ever flipped back to `false` at the
  same `stateSince`, it still will not re-toast.
- A session flipping `needs_input → working → needs_input` at the **same** `stateSince` is
  not a new spell.
- Not-yet-due spells are deliberately **not** consumed by an early poll — they still fire
  once they mature.

The ledger is pruned when a session leaves the feed, but only after
`LEDGER_PRUNE_GRACE` (3) consecutive absences. That grace exists for one specific failure:
crabd restarting and briefly serving an empty `sessions` array would otherwise clear the
ledger mid-spell and toast the same question twice.

### Quiet hours mean silent, not deferred

When `quiet.active` is true, a matured spell is suppressed **and marked resolved**. It does
not queue up. This is deliberate: without it, every question that matured overnight would
fire the instant quiet hours ended — a burst of stale toasts on a perfectly healthy morning,
which is exactly the kind of alert that trains you to ignore the one that matters. A
slept-through question is still on the widget, which is where it belongs.

`toast.enabled: false` behaves differently on purpose **for the three session-keyed toasts**:
they emit nothing **and record nothing**, so turning toasts back on surfaces whatever is
genuinely still waiting. The digest, the budget and the outage toast follow the quiet-hours
rule instead — suppressed and marked. Full table: the muted column of the matrix below.

### Failure behavior

- **crabd unreachable** → silent. Logged at debug, no toast. A notifier that toasts about
  its own plumbing is worse than one that says nothing.
- **Unsupported `schema`** → stands down, warns once per distinct value. Accepts 1–5
  (`SUPPORTED_SCHEMAS`); a test reads the live numbers out of `docs\STATE-CONTRACT.md` so a
  bump cannot go silent.
- **Handler failures** (crabd down, 404, timeout, malformed URI) → silent, non-zero exit,
  one log line. The toast is unaffected either way.
- **Malformed session rows / unparseable `stateSince`** → that row is skipped; the rest of
  the poll proceeds.
- **Corrupt or half-written config** → keeps the last good config.
- Any unexpected exception in a poll is logged and the loop continues.

### A toast that fails to render — the decider x failure-shape matrix (v0.18.0)

**This table is the answer to "what happens to the spell when the toast does not appear",
and it is written down so the question stops being re-asked.** A "spell" is whatever the
decider dedupes on — a waiting question, a parked permission, a finished turn, a calendar
day, an outage. Every decider marks its spell **before** the render is attempted, because
the mark is what dedupes the next poll 10 s later. So the only question is what happens to
that mark when the render fails.

`_emit` sees exactly two failure shapes:

| Shape | What produces it |
|---|---|
| **`show()` returns False** | anything `PowerShellToastAdapter.show` catches itself: a non-zero PowerShell exit (this is where `LoadXml` rejecting illegal toast XML lands), `OSError`, `SubprocessError`, a timeout. Also any adapter returning a falsy non-bool. |
| **`show()` raises** | anything it does not catch. `build_xml` / `build_script` run **outside** that try, so a payload the builder cannot construct arrives here; so does any bug in a third-party adapter. |

**Since v0.18.0 the shape does not matter.** A raise is caught per request, logged with its
traceback (`toast emission raised for session=…`), and handled exactly as a `False` would
be. What each decider then does is unchanged and deliberate:

| Decider | Signal | `show()` returns False | `show()` raises | **muted** (`toast.enabled: false`, v0.19.0) | Why |
|---|---|---|---|---|---|
| **Waiting session** | LIVE — the operator is being waited on right now | **retried** (`ToastDecider.unresolve`) | **retried** | **not consumed** — the decider stands down before the ledger write, so the question toasts on the first poll after the switch returns | The question is still open. Losing it silently is the bug the whole feature exists to prevent. |
| **Approval** | LIVE — a tool call is parked, blocking | **retried** (`ApprovalDecider.unresolve`) | **retried** | **not consumed** — same mechanism; a still-parked permission toasts when the switch returns | Same, and security-relevant: this toast is the out-of-view alert that something is waiting on a yes/no. |
| **Long run** | informational — the turn already finished | consumed | consumed | **the edge is spent** — the `working → done` observation swap is unconditional (a reading, not a decision), so a turn that finished while muted is not toasted afterwards | Nobody is blocked. A completion notice re-firing every 10 s is the worse failure — and a duration measured against a turn start from whenever the switch was last on would be confidently wrong. |
| **Daily digest** | periodic | consumed for the day | consumed for the day | **consumed for the day** — the ledger is marked before `_emit` sees it | The day is marked before the show, on purpose: a digest that failed at 07:00 must not retry 3,000 times before midnight. Same rule as quiet hours: suppress AND mark, never defer. |
| **Budget crossed** | periodic | consumed for the day | consumed for the day | **consumed for the day** — same mechanism | Same rule as the digest. |
| **Companion outage** | one-shot per outage | consumed until recovery | consumed until recovery | **consumed until recovery** — a recovery still re-arms it, so "consumed" never means "silenced forever" | Retrying would put an outage line on screen every 10 s for as long as crabd is down. |

The muted column is the same split the other two are: **a live signal re-arms, a periodic
consumes.** It is what decides what the operator sees when the switch comes back on — the
question that is still open, and not yesterday's digest arriving at an arbitrary hour.
Pinned by `notifier/tests/test_mute.py`.

The retry column is implemented by registration, not by a branch: `poll_once` records an
**owner** for the two live-signal requests only, and `_emit` re-arms a request that has one.
The other four register nothing, so consume-on-attempt is structural — there is no code path
that could accidentally start retrying them.

**Requests are also independent of each other.** Before v0.18.0 a raise escaped the emit
loop, so one poisoned payload consumed *every* spell in that poll and skipped the rest
without attempting them (measured 2026-08-27: three matured questions, one raise, one
`show()` call, three questions lost). Each request now succeeds or fails on its own.

`KeyboardInterrupt` and `SystemExit` are **not** caught — the daemon must still be
stoppable through its own poll loop. Matrix pinned by `notifier/tests/test_emit_matrix.py`.

---

## Config

Read-only from `~/.sidecrab/config.json` (**the companion lane owns writing this file — the
notifier never creates or modifies it**, and there is a test asserting that). Re-read
automatically when the file's mtime changes.

```jsonc
{
  "toast": {
    "enabled": true,             // default true
    "thresholdSec": 120,         // default 120 — the waiting-session toast
    "approvalThresholdSec": 20,  // default 20  — the approval toast
    "longRunSec": 900            // default 900 — the long-run completion toast; 0 = off
  }
}
```

All keys are optional; the whole `toast` block is optional. Bad types fall back per-field
(a string `thresholdSec` does not disable the notifier, it just uses 120), and one bad field
does not poison the other.

**`enabled: false` is a GLOBAL mute — all six toasts, since v0.19.0.** The switch is labelled
"Desktop Toast Alerts" in the panel, so it turns off desktop toast alerts; a switch that
secretly exempts categories is a worse failure than one that is too blunt.

Before v0.19.0 it was checked inside three of the six deciders — waiting, approval and
long-run, the three that key on a session — and the digest, the budget crossing and the
companion-outage toast kept firing under a switch the operator had turned off. The gate now
lives at `_emit`, the single point every toast passes through, which is what makes "all six"
structural rather than a list somebody has to remember to extend.

The three session-keyed deciders **keep their own gate as well**, and that is not redundancy:
theirs suppresses *before* the spell is marked, which is what lets a still-open
**waiting/approval** question re-surface when the switch comes back on. Long-run keeps the same
inner gate but does *not* re-surface: its `working → done` edge is spent by the unconditional
observation swap that runs ahead of the gate, so a completion that finished while muted is
stale. See the muted column in the matrix above for what each toast does with its spell.

While muted the log carries **one line per kind per local day** — never per attempt, because
a switch left off for a day is 8,640 polls. There are two shapes:

- an **aggregate line**, once a day, saying the mute is global and which kinds consume vs
  re-arm. It exists because the three session-keyed deciders stand down before the emit seam
  and so cannot be named there — without it, a log naming only the digest and budget
  suppressions reads as a *partial* mute.
- a **per-kind line** for each kind that actually reached `_emit` (digest, budget, outage).

> The approval toast follows the same gate, with no exemption. Panel approvals ship OFF and
> the panel still shows the request either way — the toast is a courtesy, not the mechanism.

**`longRunSec: 0` means OFF, not "toast every turn".** A 0 read as "no minimum" would fire on
every completed turn on the box — the textbook control that trains an operator to ignore
notifications — so the one value nobody could sanely want is spent on the switch. A negative
or non-numeric value falls back to 900 like every other threshold here.

> `approvalThresholdSec` round-trips since crabd 0.16.0: `POST /v1/config` accepts it as an
> optional third `toast` member (bounds 5..3600), and a panel save that omits it PRESERVES the
> on-disk value instead of wiping it. (Before 0.16.0 the validator required exactly
> `{thresholdSec, enabled}` and a widget save silently dropped the key.) The widget still has no
> control for it, so the file is where you set it — it just survives saves now.

Logs: `~/.sidecrab/logs/notifier.log`, rotating, 512 KB × 3.

---

## Approval toast (v0.15.0)

When a session carries a live `pendingPermission` (STATE-CONTRACT §4) that nobody has decided
within `approvalThresholdSec`:

```
Claude needs permission
Bash — git push --force origin master Decide on the panel.
SideCrab
```

**It carries no Approve/Deny buttons, and that is the feature.** Toast actions are cheap to
hit — one click from a lock screen, or from a notification the shell replays out of Action
Center hours later, by anyone standing at the machine. That is an acceptable cost for
"Acknowledge" (it clears a dot) and not for "allow this tool call". crabd equally has no branch
that yields `behavior: allow` without a `/v1/action decide` tap from the panel; a toast button
would be a second, weaker door into the same decision. So the body ends with **"Decide on the
panel."**, and the hint is reserved out of the body budget — a long tool argument gets trimmed,
the instruction never does.

There is no Acknowledge button either, for the widget's own reason: a `pendingPermission` card
is a hard stop, not an ack-able question (it is the one card the two-finger ack-all skips).

Other things worth knowing:

- **20 s, not 120 s.** crabd parks the PermissionRequest hook for 55 s and then passes it
  through to the terminal dialog. A threshold anywhere near the waiting-session one could only
  ever toast about requests that had already fallen through.
- **Not gated on `state == "needs_input"`,** which is what the widget's approval card keys on.
  crabd registers the pending entry from the PermissionRequest hook and does not itself move the
  state machine — `needs_input` arrives separately, via the Notification hook, and the two are
  not ordered. A live `pendingPermission` is already proof a real hook is parked; requiring a
  second, racing field could only lose toasts.
- **A missing `requestedAt` drops the request rather than toasting it.** It *is* the dedupe key,
  and a request that cannot be keyed cannot be promised to toast only once.
- **The dedupe mark is cleared once the request resolves** (decided, denied, or timed out), so
  the ledger stays bounded by live sessions — but only after 3 consecutive polls without it, so
  a one-poll flicker cannot re-toast a request still on screen.
- **The waiting-session toast is independent.** A request that falls through to the terminal and
  is *still* unanswered at 120 s also gets the ordinary "Claude is waiting" toast. They say
  different things, and the second one is a real escalation.

---

## Companion outage toast (v0.15.0)

The only toast about SideCrab itself, and the only one that fires while crabd is **down**:

```
SideCrab companion not responding
The companion is not answering. The panel is showing stale data until it recovers.
```

Everything else in this component goes silent when crabd is unreachable, which is right for
questions and wrong for the panel: a dead panel looks identical to a quiet one. The widget dims
and shows "data as of HH:MM", but the operator is by definition not looking at it — green dots
that froze five minutes ago read exactly like green dots.

Two conditions, one threshold:

| Arm | Unhealthy when | Timed from |
|---|---|---|
| **Unreachable** | the fetch fails, or the body is not a document | the last healthy poll |
| **Frozen** | `generatedAt` is older than 5 min | the feed's own stamp |

Both measure the same quantity — *how long since the panel held the truth*. Timing the
unreachable arm from when the notifier first **noticed** would instead restart the clock on
every resume from sleep, and would answer a different question.

The gates, and what each one is for:

- **A 5-minute dwell.** crabd restarts are routine; a 10 s gap must never toast, or the operator
  learns to ignore the one outage that matters.
- **Someone must have been working in the last 15 minutes** — a `working` or `needs_input`
  session seen on a *healthy* poll. A stale document's claim that someone is working does not
  count; re-arming from frozen content is how a dead feed keeps itself alive forever. This gate
  is what keeps a laptop that slept overnight, and a first run against a crabd that was never
  installed, completely silent.
- **One per outage, re-armed only by a recovery.** A silent outage (nobody working) does *not*
  spend the toast — if the operator turns out to have been at the machine, it is still available.
- **Quiet hours are remembered, not read** — the `quiet` block lives in the very feed this
  decider cannot reach, so the last healthy poll's value is used. Suppressed and marked, like
  everything else here.

Its Action Center tag is fixed (`stale-feed`), so a second outage toast **replaces** the first
rather than stacking. There is only ever one outage in progress, and one line about it is the
honest count.

All of this state is in-process by design. A notifier restart re-arms the toast, which is the
safe direction: at worst one extra line about an outage that is genuinely still on.

---

## Long-run completion toast (v0.16.0)

One toast when a session finishes a turn that ran longer than `toast.longRunSec` (900 s):

```
Finished after 22m - Echo SIDECRAB-ALLOW-OK
Started 22:53, finished 23:15.
```

A 25-minute build or test run is exactly the turn the operator walked away from, and the panel
cannot tell him it finished if he is not looking at it.

### The duration does not exist in any single feed document

`turnStartedAt` is set on `UserPromptSubmit` and **cleared on Stop** (STATE-CONTRACT), so the
`done` row that says a turn finished has already forgotten when it began. crabd serves no
turn-duration field and no finished-turn record. The only place the start still exists is the
**poll before the `done`** - hence a one-poll memory in `LongRunDecider`, the same technique
the widget's celebration uses (`detectCelebration` in `widget/scripts/sidecrab.js`).

The duration is `done.stateSince - prev.turnStartedAt`. Both come out of the feed; `now` is not
used at all, so the answer cannot change with how late the notifier got round to asking.

*Measured on the live feed, 2026-08-27* (262 polls, 10 s apart, against crabd 0.13.0 schema 5):

| Observation | Consequence |
|---|---|
| `turnStartedAt` populated at `05:06:22` on a `working` row and held **stable across 4 consecutive polls** | the observation window this needs is real |
| all three sessions carried `turnStartedAt: null` between turns | normal, not a fault - a session between prompts has no turn |
| one `working` row's `stateSince` **moved on every poll** (05:06:30 -> 05:06:39 -> 05:06:50 ...) | `stateSince` is *not* a usable turn start; on crabd's reactivation path it tracks the transcript mtime |

That last row is why the reactivation path is excluded rather than approximated: crabd's
`_resolve` returns `working` when the transcript is written after a Stop, with the turn already
cleared. There is no honest duration there, so there is no toast.

### What it will not do, and why

- **A turn whose `done` the notifier never observes produces no toast.** crabd holds a done row
  for many polls, so this needs the notifier stopped across the transition - and a notifier that
  was not running should not invent history when it comes back.
- **Started mid-turn -> silent for that turn.** Timing from first sight would report how long the
  *notifier* has been up, not how long the turn ran.
- **`needs_input` does not count as running.** `turnStartedAt` survives `working -> needs_input`,
  so a turn that ran 40 s and then sat 30 min waiting for the operator would report *"Finished
  after 31m"* - his own thinking time, handed back to him as compute.
- **An unparseable stamp is refused, not approximated.** A made-up duration in the one toast
  whose entire content is a duration is worse than no toast.
- **A crabd blip does not lose the turn.** The fetch failing returns before this decider, so the
  memory survives a restart mid-run.

Own Action Center slot (`longrun-<sessionId>`), no buttons: the turn is over, there is nothing
to acknowledge and nothing to snooze.

---

## Snooze 30m (v0.16.0)

The waiting toast carries a **second button** beside Acknowledge:

```xml
<actions><action activationType="protocol" content="Acknowledge" arguments="sidecrab-ack:0f9c1e2a-..."/>
         <action activationType="protocol" content="Snooze 30m"  arguments="sidecrab-snooze:0f9c1e2a-..."/></actions>
```

Pressing it suppresses further **waiting** toasts for that session for 30 minutes. Acknowledge
stays first: the leftmost button is the one hit from a lock screen by someone barely reading,
and that one should be the answer, not the deferral.

### Snoozing is not answering

**The handler never touches crabd.** The ack handler POSTs an ack to `/v1/action`; this one
deliberately does not. A snooze is a statement about *notifications*; an ack is a statement
about the *question*. Acking here would clear the widget's dot and the panel would stop showing
a session that is still, truthfully, waiting for an answer nobody has given.

That is enforced structurally, not by comment: `sidecrab_snooze_handler.pyw` imports no
`urllib`, `http` or `socket`, and a test reads that off the AST.

### Snooze DEFERS - the one exception to suppress-and-mark

Every other suppression here marks the spell consumed. Quiet hours mean *"I was asleep, that
question is stale by morning."* Snooze means *"I know, tell me again in half an hour"* - and a
reminder that never comes back is not a reminder. So the spell stays **unresolved**, and the
first poll after the mark expires toasts it again: same `stateSince`, one more time, once.

An **ack still wins**: acknowledging from the widget during a snooze resolves the spell
permanently. The snooze was only ever about *when to ask again*.

### What it does not silence, deliberately

| Toast | Snoozed? | Why |
|---|---|---|
| Waiting session | **yes** | this is the nag the button is on |
| Approval | no | a parked `pendingPermission` falls through to the terminal dialog in 55 s, so a 30-minute snooze would equal *never* - it would silently swallow a security decision |
| Long run, digest, budget, outage | no | none of them is a repeat of the toast the operator just deferred |

### The mark

`~/.sidecrab/toast-state.json`, under `"snooze"`, `{"<sessionId>": "<ISO expiry>"}`. On disk
**so a task restart cannot turn a 30-minute snooze into an immediate toast**.

The handler is the **only writer** of that key and the notifier is the only reader, which is
what makes a two-process ledger safe without a lock. Every writer of this file replaces only
its own top-level key (`write_state_section`), and the temp file carries the writing PID so two
processes cannot interleave and rename a hybrid into place. Marks that have expired, that carry
a key failing the session-id charset, or whose instant will not parse are dropped on sight -
neither *"snoozed forever"* nor *"snoozed until now"* is a decision the operator made, and the
first of those silences a waiting question permanently.

### The registry entry (registered by setup since v0.16.0)

`Register-SideCrabProtocol.ps1` registers this scheme alongside `sidecrab-ack:`; until it did,
the button rendered and the shell had nowhere to send it - the same harmless state the
Acknowledge button shipped in. The entry, identical in shape to the ack one:

```
HKCU:\SOFTWARE\Classes\sidecrab-snooze
    (Default)      = URL:SideCrab Snooze
    URL Protocol   = ''                          <- the flag Windows actually gates on
HKCU:\SOFTWARE\Classes\sidecrab-snooze\shell\open\command
    (Default)      = "<pythonw.exe>" "<repo>\notifier\sidecrab_snooze_handler.pyw" "%1"
```

In `setup\SideCrab.Common.ps1`, `Get-SideCrabProtocolSpec` returns a ROW PER SCHEME - the snooze
row carries `Handler = notifier\sidecrab_snooze_handler.pyw`, `Description = 'URL:SideCrab Snooze'`
and the same `ComponentKey = 'toast'`, so an install without the toast component leaves no scheme
pointing at a handler that is not there. `Set-SideCrabProtocol`, `Remove-SideCrabProtocol`,
`Get-SideCrabProtocolState` and every `-Status` / `-Remove` path loop over that table, so a third
button would be one more row and no new code. All three quoted parts of the command string stay
load-bearing for the same reasons as the ack handler's. `Test-SideCrab.ps1` and
`Repair-SideCrab.ps1` carry one row per scheme.

No elevation, no COM server, HKCU only. Registering is an upgrade and removing a downgrade,
never a break - and nothing about the scheme is cached in the notifier process, so **no restart
is needed** after registering it.

*Verified 2026-08-27 by running the handler exactly as that command would:* a valid URI exited
`0` and wrote a mark 1800 s out; `sidecrab-snooze:../../windows/system32` and a `sidecrab-ack:`
URI both exited `2`, wrote nothing, and logged only the argument's **length**.

---

## Version reporting (v0.16.0)

**The notifier could not previously tell anyone it was running stale code.** A `SideCrab-toast`
task that has not been restarted since `sidecrab_toast.py` changed looks - in Task Scheduler, in
the log, in `/v1/state` - exactly like one running the current file. That class bit twice on
2026-08-26 (`SUPPORTED_SCHEMAS` stopped at 3 while crabd served 5; the AUMID probe latched a
borrowed answer), and both investigations began by assuming the running code was the code on
disk. Three answers now exist:

| Answer | Where |
|---|---|
| **Startup log line** | `sidecrab notifier v0.17.0 from C:\Dev\sidecrab\notifier\sidecrab_toast.py` - on **every** invocation, `--test-*` runs included |
| **`--version`** | two parseable lines on stdout, printed *before* logging is set up so there is no preamble to strip |
| **The ledger** | a `notifier` section in `~/.sidecrab/toast-state.json` |

```jsonc
"notifier": {
  "version": "0.17.0",
  "module": "C:\\Dev\\sidecrab\\notifier\\sidecrab_toast.py",  // a repo cloned twice differs here, not in the version
  "pid": 93136,
  "startedAt": "2026-08-27T05:17:28.900484+00:00",
  "lastPollAt": "2026-08-27T05:17:28.902014+00:00"             // refreshed every 15 min, not every poll
}
```

`Test-SideCrab.ps1` / `Repair-SideCrab` can now compare `__version__` on disk against
`.notifier.version` in that file and say *"restart the task"*, instead of showing a green
`task: Running` row over a process executing a file that changed hours ago.

Written **before the first poll** and forced, because the pre-v0.16.0 ledger only existed once a
digest or budget toast had fired - on the measured box it did not exist **at all**. `lastPollAt`
is refreshed inside the poll loop so the claim "still polling" cannot outlive a poll; a version
with no recent poll behind it describes a process that has since died, which is the same lie in
the other direction.

Bump `__version__` in the same commit as any behaviour change.

---

## Daily digest (v0.8.0)

One toast per calendar day summarising **yesterday**, at a configured local time.

```jsonc
{
  "digest": {
    "enabled": true,     // default false
    "time": "09:00"      // local, strict HH:MM
  }
}
```

- Title `SideCrab — yesterday`, body `N done · M commits`, taken from crabd's
  `recap.week` row for yesterday's local date. No Acknowledge button — there is no session
  to acknowledge.
- **A bad `time` disarms the digest; it does not fall back to a default.** A wrong threshold
  is a nuisance, a wrong *time* fires a daily notification at an hour nobody asked for. The
  mismatch is logged once.
- **Scheduling rides the existing 10 s poll** — no thread, no timer. It fires on the first
  poll at or after the configured minute, so a machine asleep at 09:00 gets its digest when
  it wakes, same calendar day. A late logon still gets that day's digest, exactly once.
- **Quiet hours skip AND mark it**, matching the waiting-session toast: silent, never
  deferred, so nothing bursts when quiet lifts.
- **Absent `recap` / `week` / yesterday's row → skipped silently and marked**, retried
  tomorrow rather than later today. *Deliberate:* not marking would also let a crabd that
  was down all morning deliver a "yesterday" digest at 4 pm, which is worse than none.
  crabd being *unreachable* never reaches this branch — the poll returns first, so the
  ordinary restart case simply retries on the next poll.

### The day ledger - and the rest of `toast-state.json`

`~/.sidecrab/toast-state.json`, `{"digest": {"lastDay": "YYYY-MM-DD"}}`, written atomically
via `os.replace`. Separate from `config.json` **on purpose**: crabd rewrites that file
(`POST /v1/config`), and two writers on one file is how a half-written config gets served.

Four things now share that one document, each under its own top-level key:

| Key | Written by | Read by |
|---|---|---|
| `digest` | the notifier | the notifier |
| `budget` | the notifier | the notifier |
| `snooze` | **`sidecrab_snooze_handler.pyw`** (a separate process) | the notifier |
| `notifier` | the notifier, at startup and every 15 min | `Test-SideCrab` / a human |

Every writer goes through `write_state_section`, which is **read-modify-write on one key** and
never a whole-file rewrite. A writer that serialised only its own section would erase the other
three on every write, and the only symptom would be a duplicate toast after a restart - the
kind of bug nobody traces back to a state file. The temp file carries the writing **PID**,
because two of these writers are different processes.

Persisting the mark is what makes a `SideCrab-toast` restart unable to double-toast. Every
ledger operation is best-effort: unreadable reads as unmarked (worst case, one duplicate),
and an unwritable ledger logs once and does not stop the daemon.

The day is marked **before** the toast is shown, so a broken toast path costs one digest
rather than retrying every 10 s for the rest of the day.

---

## Toast mechanism (measured, not assumed)

**Chosen — Route A: subprocess to Windows PowerShell 5.1, WinRT projection.**
`[Windows.UI.Notifications.ToastNotificationManager, ..., ContentType=WindowsRuntime]` loads,
`CreateToastNotifier()` returns a live notifier, `Show()` returns clean, and the payload was
confirmed persisted in Windows' own notification store (`wpndatabase.db`). Zero pip
dependencies — which matters for a Scheduled Task, where a missing site-package in whichever
interpreter the task picked up would silently kill notifications.

**Rejected — Route B: the `winrt-Windows.UI.Notifications` pip package.** It downloads fine
(3.2.1 cp313 wheel) but is not installed here and would pull three packages into the task's
interpreter. It buys nothing Route A lacks.

### Traps pinned in the code

- **pwsh 7 cannot do this.** `Unable to find type [...ToastNotificationManager]` — the WinRT
  projection exists only in Windows PowerShell 5.1. `POWERSHELL_EXE` is pinned to System32;
  do not "modernize" it to pwsh.
- **`ToastNotifier.Setting` and `ToastNotificationManager::History` project as `null`** under
  5.1 rather than throwing. They are not usable as a delivery check — do not build a "did it
  show?" assertion on them.
- **The icon URI must not escape the drive colon.** `urllib.parse.quote` without `safe="/:"`
  produces `file:///C%3A/...`, which Windows does not resolve — the toast renders with no
  logo and no error. Caught by reading the first real toast's stored payload; regression
  test in place.
- Toast XML crosses into PowerShell as **base64**, so no quote, brace or backtick in a
  question can escape into PowerShell source. The script itself is passed via
  `-EncodedCommand`, so Windows command-line quoting is not in play either.
- **Two XML escapes, chosen by CONTEXT and not by trust (v0.17.0, audit F3).** Element text
  uses `xml_escape` (`&<>` + the F1 control-char strip); anything inside an attribute uses
  `xml_attr_escape`, which adds `'` -> `&#39;` and `"` -> `&#34;`. `saxutils.escape` does not
  touch quotes, and `build_xml` single-quotes its attributes, so a `'` in an attribute value
  would close it early and everything after would parse as markup — an injected
  `<action activationType='protocol'>` is a toast button that launches an attacker's URI.
  **No reachable path carries a raw quote into an attribute today** (constants, ids that
  passed `^[A-Za-z0-9-]{1,64}$`, a percent-encoded icon path), so this closes no live hole and
  no behavioural test can fail on it — which is exactly why the guard is a *routing* test that
  swaps both escapes for markers and asserts nothing between `='...'` came from the text one.
- **`wpndatabase.db` is in WAL mode — copy the `-wal` and `-shm` files with it.** Copying only
  the `.db` shows you the last *checkpoint*, not the last few minutes: a toast fired seconds
  ago is in the write-ahead log and simply is not there. Measured 2026-08-26 while evidencing
  the v0.15.0 toasts — two of them read as "shown but never persisted" for three consecutive
  reads, and the payloads appeared the instant all three files were copied together. Every
  "did the toast really land?" check in this README uses that store, so this trap can
  manufacture a phantom bug in any of them:

  ```powershell
  $src = "$env:LOCALAPPDATA\Microsoft\Windows\Notifications"
  $dst = "$env:TEMP\wpn"; New-Item -ItemType Directory -Force $dst | Out-Null
  'wpndatabase.db','wpndatabase.db-wal','wpndatabase.db-shm' |
      ForEach-Object { Copy-Item "$src\$_" "$dst\$_" -Force -ErrorAction SilentlyContinue }
  ```

### App identity

Toasts need a registered AppUserModelID. Two are in play, chosen at toast time:

| | AUMID | When |
|---|---|---|
| **Preferred** | `SideCrab.Notifier` | `HKCU\SOFTWARE\Classes\AppUserModelId\SideCrab.Notifier` exists |
| **Fallback** | Windows PowerShell's, `{1AC14E77-…}\WindowsPowerShell\v1.0\powershell.exe` | it does not |

`setup\Register-SideCrabAumid.ps1` creates that key (`DisplayName` = SideCrab, `IconUri` =
`notifier\sidecrab.ico`); `setup\Install-SideCrab.ps1` runs it with the toast component and
the uninstaller removes it. It is a HKCU write — no elevation, no COM server, no shortcut.

With it registered, Action Center groups the toasts as **SideCrab** and Windows' per-app
notification switch is ours. Without it they are attributed to "Windows PowerShell", which
still delivers — so registering is an upgrade and removing is a downgrade, never a break.

**This notifier still writes no registry.** It only READS, to choose between the two. A
*positive* answer is cached for the process lifetime; a *negative* one is re-read every
`AUMID_REPROBE_SEC` (300 s), so registering the key on a running notifier is enough on its
own — no restart needed. The identity is also resolved and logged at startup, so the log
answers "which AUMID is this process using?" without waiting for a toast.

*Measured on Windows 11:* before registration, `wpndatabase.db` attributed SideCrab's
toasts to the PowerShell GUID AUMID and no SideCrab handler existed; after, the same toast
lands as `aumid=SideCrab.Notifier`, `group='sidecrab'`, with a `SideCrab.Notifier` handler
registered in the platform's `NotificationHandler` table.

#### The "borrowed while the key existed" trap — root cause, 2026-08-26

A fresh notifier logged `borrowed` while `HKCU\…\AppUserModelId\SideCrab.Notifier` was
plainly readable from a shell. **The probe was right and the shell was wrong.** HKCU
registry *writes* made from the agent/automation shell that ran
`setup\Register-SideCrabAumid.ps1` land in a per-session virtualized overlay: that shell
reads its own writes back and reports the key present, while every process outside it — the
`SideCrab-toast` Scheduled Task included — sees the real hive without it.

Proven with a write/read matrix rather than argued:

| Key written by | Visible to the shell | Visible to a Scheduled Task |
|---|---|---|
| the shell | yes | **no** (`winerror=2`) |
| a Scheduled Task | yes | yes |

`NtQueryKey` confirmed both processes resolve `HKCU\SOFTWARE\Classes` to the same
`\REGISTRY\USER\<SID>_Classes`, and the failing read was `winerror=2` (absent), not
access-denied — so this was never a path, WOW64, integrity-level or ACL problem. Registering
the AUMID from a Scheduled Task made it visible everywhere, and the daemon's next start
logged `toast identity: SideCrab.Notifier (SideCrab AUMID registered)`.

**Rule that falls out:** *"readable from a shell" is not evidence the AUMID is registered.*
Only a read taken from the notifier's own execution context is — which is why the borrowed
log line now carries the probe's actual failure reason (`winerror=…`), so "not registered"
and "cannot read it" are never again the same message.

### Acknowledge button (v0.7.0)

The toast carries one action button. It uses **protocol activation** — the toast's
`arguments` are `sidecrab-ack:<sessionId>`, and the shell routes that to a registered
handler:

```xml
<actions><action activationType="protocol" content="Acknowledge"
                 arguments="sidecrab-ack:0f9c1e2a-…"/></actions>
```

**Why a protocol and not a callback.** A toast sits in Action Center until it is dismissed,
long after the notifier that raised it may have exited. Foreground/background activation
needs a COM-registered application and calls back into a process that may be gone; a URL
scheme is routed by the shell, which is still there. Protocol activation is also the only
one of the three that needs no elevation and no COM server — just two HKCU keys.

| Piece | Lives in |
|---|---|
| Writes the URI into the toast | `sidecrab_toast.py` → `ack_uri()` |
| Registers `sidecrab-ack:` (and `sidecrab-snooze:`) | `setup\Register-SideCrabProtocol.ps1` (HKCU, idempotent, `-Remove`, `-Status`; one row per scheme) |
| Receives and acts on it | `sidecrab_ack_handler.pyw` |

The handler POSTs `{"sessionId": …, "action": "ack"}` to `/v1/action` (5 s timeout) and
**exits silently on any failure** — no window, no dialog, one line to
`~/.sidecrab/logs/ack-handler.log`. A traceback dialog appearing because crabd happens to be
stopped would be worse than the ack simply not landing. It writes its own log rather than
sharing `notifier.log`, which the daemon holds open through a rotating handler.

**The URI is data.** It arrives from the shell, out of a toast payload that outlives the
process that wrote it and can be replayed at any time. The session id is matched against
`^[A-Za-z0-9-]{1,64}$` **before** it reaches a URL, a log line or a JSON body — and since v0.17.0
(audit F4) the **scheme token alone** is matched case-insensitively, because RFC 3986 schemes
are case-insensitive and Windows resolves the handler key that way: the shell can hand back
`SIDECRAB-ACK:<id>` for a key registered lowercase, and the old exact match refused it with a
length-only log line, i.e. lost the operator's click silently. The fold is `re.IGNORECASE |
re.ASCII`; the `re.ASCII` is load-bearing, since full-Unicode folding maps U+212A KELVIN SIGN
onto `k` and U+017F LONG S onto `s`, and a homoglyph is not a case difference. **Nothing after
the colon is loosened** — the charset test is unchanged and applies identically whatever the
scheme's case. A refused URI is never echoed into the log, only its length. The same charset is checked again on the
*writing* side: an id that escaped the XML attribute would be a **stored** injection, not a
transient one. An id that fails it drops the button, never the toast — a question waiting on
the operator still has to reach him.

Registering is an upgrade and removing a downgrade, never a break: nothing about the scheme
is cached in the notifier process, so **no restart is needed** after registering it (unlike
the AUMID). Without the registration the button renders and the shell has nowhere to send it.

*Measured on Windows 11:* the real toast for session `toast-e2e-test` persisted in
`wpndatabase.db` (row 13024) carrying the `<actions>` element verbatim; running the
registry's own `shell\open\command` with that row's URI exited 0, logged `204`, and flipped
`acked` to `true` in `/v1/state` with an `acknowledged from Edge` event. Four malformed URIs
through the same command exited 2 and changed nothing.

### Icon

`sidecrab.png` (64×64 RGBA) is generated by `make_icon.py` from the same pixel-crab geometry
as `widget/resources/icon.svg`, re-drawn white-on-orange so it reads in both Windows themes.
The widget's own assets are read-only to this component and were not modified: `icon.svg` is white
on transparent (invisible on a light toast) and `preview.png` is a 128×56 banner (wrong aspect
for `appLogoOverride`). Regenerate with `python make_icon.py`.

The same run converts that PNG into `sidecrab.ico` (16/32/64 px, 32-bit) for the AUMID's
`IconUri`, which the shell reads and which wants a multi-resolution icon rather than a single
bitmap. The ICO is CONVERTED FROM the PNG, not re-rendered from the geometry, so an edited
PNG carries into the icon on the next run — one drawing, one source of truth. Still pure
stdlib: `zlib` + `struct`, no pip dependency on either side.

---

## macOS

Everything above this line — the six deciders, the thresholds, the dedupe ledgers, quiet
hours, snooze marks, the failure matrix — is shared. What changes on a Mac is the last step,
and only that: `MacNotificationAdapter` posts through `/usr/bin/osascript` instead of
building toast XML for PowerShell. `pick_adapter(sys.platform, icon)` chooses, at the one
construction site in `main()`, so every `--test-*` flag exercises the real macOS path.

**The mechanism.** Three CONSTANT AppleScript strings passed with `-e`, then `--`, then the
body, the title and the subtitle as three positional arguments:

```
/usr/bin/osascript -e 'on run argv' \
  -e 'display notification (item 1 of argv) with title (item 2 of argv) subtitle (item 3 of argv) sound name "default"' \
  -e 'end run' -- <body> <title> "SideCrab"
```

The notification text is never interpolated into the script. *Measured on macOS 26.6:* a
probe argument carrying `"`, `\`, a newline, `$(touch …)`, backticks, `&` and `; rm -rf /`
came back byte-identical with exit 0 and nothing substituted or executed — the same boundary
Windows gets from base64, obtained by not building a script out of operator text at all.
`notifier/tests/test_mac_adapter.py` pins it: the three `-e` strings must stay byte-identical
whatever the request says, and the subprocess must be handed a **list** with no `shell=`.

Control bytes are stripped from every argument with the same character class the Windows lane
strips (`strip_control`), which is load-bearing here rather than cosmetic: `subprocess`
refuses an argument carrying a NUL with `ValueError`, and that is not one of the failures
`show()` converts to `False`.

**No buttons.** `display notification` has no action affordance, so there is no Acknowledge
and no Snooze on macOS — the operator acknowledges **on the panel**, where the context is.
The approval notification carries no Approve/Deny either, which is the same deliberate rule
as on Windows and not a platform limitation. `sidecrab-ack:` / `sidecrab-snooze:`,
`sidecrab_ack_handler.pyw` and `sidecrab_snooze_handler.pyw` are the Windows route's protocol
handlers and stay Windows-only; nothing on macOS registers or invokes them.

**Two residuals**, both permanent properties of this route:

| | What it means |
|---|---|
| Notifications **stack**, they do not replace | `display notification` cannot set a replacement identifier. A second outage notice sits beneath the first instead of replacing it, and the same goes for the digest and budget alerts. The ids and prefixes still keep the deciders' ledgers honest — one alert per outage, per day, per spell — so what stacks is only what the notifier meant to say twice. |
| The identity is **Script Editor**'s | Notifications posted through `osascript` are attributed to Script Editor, so macOS's per-app notification switch is Script Editor's, not SideCrab's. The subtitle is always `SideCrab`, which is the only thing on screen naming the product. There is no AUMID to register and no registry to read: that whole block is Windows-only. |

The first notification may raise a one-time macOS permission prompt for Script Editor. Until
it is allowed, `osascript` can exit non-zero or sit until the adapter's 10 s timeout; both are
logged (`notification failed rc=…`) and both return `False`, which re-arms a waiting question
for the next poll rather than consuming it.

There is no Linux route. `pick_adapter` hands every other platform the Windows adapter, whose
`show()` then fails honestly with the reason in the log.

---

## Running it

```powershell
# normal daemon
python notifier\sidecrab_toast.py

# one evaluation, decide only, never show  (safe against production crabd)
python notifier\sidecrab_toast.py --once --dry-run --verbose

# fire a sample toast and exit
python notifier\sidecrab_toast.py --test-toast

# fire ONE digest toast from the live feed, ignoring the schedule and never marking the
# ledger — proves the digest's XML/AUMID path without waiting for the clock
python notifier\sidecrab_toast.py --test-digest
python notifier\sidecrab_toast.py --test-budget

# fire ONE approval toast off whatever is parked right now (a SAMPLE request when nothing is),
# and ONE outage toast. Neither goes through its decider — they prove the toast RENDERS; the
# tests prove the gates hold, and can do it without stopping the operator's own crabd.
python notifier\sidecrab_toast.py --test-approval
python notifier\sidecrab_toast.py --test-stale

# fire ONE long-run completion toast, ignoring the threshold and the ledger. The SESSION is
# real (the first the live feed carries), so the title trimming is proven on a real title.
python notifier\sidecrab_toast.py --test-longrun

# which module is this, really?  (answers on stdout, before logging is set up)
python notifier\sidecrab_toast.py --version

# exercise the digest SCHEDULER against live crabd without touching the real files
python notifier\sidecrab_toast.py --once --dry-run --config <temp.json> --state <temp-state.json>
```

Flags: `--endpoint`, `--interval`, `--config`, `--state`, `--once`, `--dry-run`,
`--test-toast`, `--test-digest`, `--test-budget`, `--test-approval`, `--test-stale`,
`--test-longrun`, `--version`, `--verbose`.

`--test-toast` now also proves the **Snooze** button: it fires the ordinary waiting toast, which
carries both actions.

### Tests

```powershell
python -m unittest discover -s notifier\tests -t notifier\tests -v
```

**543 tests** - 84 decision/adapter/ack-action/AUMID + 72 snooze + 53 long-run + 52 digest +
49 approval + 43 budget + 41 macOS adapter + 38 stale-feed + 34 ack handler + 29 global mute +
24 version/state-file + 13 emit matrix + 11 icon.
(Counted with `loadTestsFromName().countTestCases()`, so the parts sum to the whole - the
previous breakdown was hand-kept and summed to 446 against a stated 449.) Stdlib `unittest` only, fully headless: toast
emission sits behind a `ToastAdapter` and the tests use `RecordingToastAdapter` or, for the
macOS adapter, an injected runner in place of the subprocess - no test posts a real
notification, and none reads or writes anything outside a temp dir. The AUMID
tests inject the registry read, so none of them passes or fails because of what happens to be
registered on the running machine; the handler tests fake `urlopen` and redirect the log to a
temp dir, so no test posts to a live crabd. Every decider is handed its own `now` and ledger
value, so no test sleeps or depends on the wall clock. Every `Notifier` built in a test is
handed a temp `SnoozeLedger`, so no test reads the operator's own `~/.sidecrab`.

Every gate is mutation-proven — breaking it in turn fails the suite:

| Gate broken | Failures |
|---|---|
| toast threshold / dedupe / quiet / enabled | 6 / 1 / 2 / 4 |
| digest dedupe (forget the day mark) | 6 |
| digest not-due (fire regardless of the clock) | 2 |
| digest quiet (defer instead of mark-and-skip) | 2 |
| digest missing row (leave the day armed) | 2 |
| digest disabled (mark the day anyway) | 3 |
| bad digest `time` falls back to a default | 12 |
| AUMID latches the borrowed answer | 1 |
| AUMID negative cache never expires | 1 |
| digest keeps the Acknowledge button | 1 |
| approval threshold (fire regardless of the clock) | 7 |
| approval dedupe (forget the mark) | 7 |
| approval quiet / disabled | 1 / 1 |
| **approval grows Approve/Deny buttons** | 2 |
| approval clear-grace drops to one poll | 1 |
| stale dwell (fire on the first unhealthy poll) | 3 |
| stale activity gate (fire when nobody was working) | 5 |
| stale one-per-outage (fire every poll) | 5 |
| stale re-arm (never re-arm after a recovery) | 3 |
| stale quiet (toast through quiet hours) | 2 |
| stale activity latch refreshed from a FROZEN feed | 1 |
| outage toast runs after the unreachable early return | 4 |
| **snooze suppression removed entirely** | 7 |
| **snooze marks the spell instead of deferring it** | 4 |
| snooze button dropped from the toast XML | 6 |
| snooze ledger cached for the process lifetime | 1 |
| snooze ledger trusts an unparseable instant (snoozed forever) | 1 |
| **long-run threshold ignored (fire on every finished turn)** | 2 |
| long-run `0` means "toast everything" instead of off | 1 |
| long-run counts `needs_input` as running | 1 |
| long-run times the finish from `now` instead of the done row | 9 |
| long-run dedupe forgotten | 4 |
| long-run approximates an unparseable stamp | 2 |
| long-run quiet hours defer instead of mark-and-skip | 2 |
| a state-file writer rewrites the whole document | 4 |
| **an attribute value escaped with the quote-blind `xml_escape`** | 1 |
| `xml_attr_escape` stops escaping `'` / `"` | 1 / 1 |
| `xml_attr_escape` drops the F1 control-char strip | 1 |
| **ack scheme match back to case-sensitive** | 4 |
| **snooze scheme match back to case-sensitive** | 3 |
| scheme fold loses `re.ASCII` (homoglyph passes as our scheme) | 1 ack / 2 snooze |
| **the global mute removed from `_emit`** (the CD-26 bug: digest/budget/outage escape) | 12 |
| a muted live signal consumed instead of re-armed | 1 |
| the muted line logged per attempt rather than per kind per day | 1 |
| the config read moved back below the schema check (the outage path escapes) | 12 |
| the muted path's per-request guard dropped (v0.18.0 batch independence) | 1 |
| **the macOS display line built with the title interpolated** | 3 |
| **the macOS argv joined and run through a shell** | 1 |
| one macOS argument left unstripped of control bytes | 2 |
| the control strip widened over tab / newline / return | 5 |
| `MAC_TITLE_TRIM` collapsed to `TITLE_TRIM` (the label eaten) | 4 |
| the macOS argument cap removed | 2 |
| the macOS failure line carries the request title and the untruncated stderr | 2 |
| the macOS adapter catches `TimeoutExpired` only | 3 |
| a non-zero `osascript` reported as shown (the spell consumed) | 2 |
| the platform test inverted in `pick_adapter` | 4 |
| `main` names `PowerShellToastAdapter` directly again | 1 |

> The long-run **quiet** row was `0` on the first mutation run. The obvious test (quiet poll,
> then a loud poll on the same done row) passed with the mark deleted, because the
> `working -> done` **edge** had already been consumed - it proved nothing. It was rewritten
> around crabd's reactivation flap, which is the one path that revisits a decided turn.

---

## Desired Scheduled Task shape — registered by the installer

**The setup lane owns wiring. Nothing below has been run.** Registering it is a deliberate
act, and this lane created no task, no registry key and no autostart entry.

Required properties:

| Property | Value | Why |
|---|---|---|
| Trigger | **At log on**, current user | Toasts need an interactive session |
| Action | `pythonw.exe` (not `python.exe`) + full path to `sidecrab_toast.py` | `pythonw` has no console to flash |
| Run as | The logged-on user, **not** SYSTEM, **not** highest privileges | A Session-0 task cannot raise a toast at all |
| Hidden | Yes | |
| If already running | Do not start a new instance | The ledger is per-process; two instances double-toast |
| On battery / idle | Do not stop, do not require idle | Otherwise it silently dies on a laptop |
| Restart on failure | Every 5 min, 3 attempts | |

Sketch for the setup lane — **review before running**:

```powershell
$py     = (Get-Command pythonw.exe).Source
$script = '<repo>\notifier\sidecrab_toast.py'

$action   = New-ScheduledTaskAction -Execute $py -Argument "`"$script`""
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
              -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
              -RestartInterval (New-TimeSpan -Minutes 5) -RestartCount 3 `
              -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName 'SideCrab Notifier' -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Description 'Toasts when a Claude session is waiting.'
```

---

## Open risks

- ~~**Borrowed AUMID.**~~ **CLOSED 2026-08-26** — `SideCrab.Notifier` is registered by
  `setup\Register-SideCrabAumid.ps1` and verified in `wpndatabase.db` (see *App identity*).
  What remains of the risk: a box where that key is absent still falls back to the borrowed
  AUMID silently, so muting Windows PowerShell there would still mute SideCrab.
  `setup\Test-SideCrab.ps1` fails the "toast identity" row when it is missing.
- **Focus Assist / Do Not Disturb** suppresses toasts at the OS level, independently of the
  `quiet` block. `Show()` still returns success. There is no reliable read-back for this
  (the `Setting` projection is null under 5.1), so the notifier cannot warn about it. macOS
  Focus behaves the same way and is worse in one respect: the switch that silences these
  notifications is Script Editor's, so an operator who mutes them has muted every script on
  the machine (see [macOS](#macos)).
- **Subprocess cost.** Each toast spawns a PowerShell process (~0.5–1 s). Fine at one toast
  per waiting spell; it would not be fine if the dedupe rule were ever loosened.
- **Ledger is in-process** for the waiting-session, approval and outage toasts (the digest and
  budget day-marks are on disk). Restarting the notifier re-arms every currently-waiting spell
  and any live outage, so a restart can produce a small burst. Acceptable for a logon-scoped
  task; persisting the ledger would be the fix if it proves annoying.
- **The approval toast has a ~35 s useful window.** It fires at 20 s and crabd passes the
  request through to the terminal dialog at 55 s. A toast the operator reaches at 60 s points
  at a panel that no longer has an Approve button — the decision moved to the terminal. That is
  the honest behaviour (nothing was auto-decided), but the toast cannot say so, because by the
  time it is read the notifier has moved on. Lowering `approvalThresholdSec` widens the window
  at the cost of toasting about requests the operator was about to answer anyway.
- **The outage toast cannot distinguish "crabd stopped" from "the machine is busy".** A hard
  freeze that stalls the poll loop for five minutes looks the same as a stopped service. Both
  mean the panel is not to be trusted, which is what the toast says.
- **The Snooze button is inert on any machine whose install predates v0.16.0.** An unregistered
  scheme renders and the shell has nowhere to send it - harmless, and indistinguishable from a
  working button until it is pressed. `setup\Register-SideCrabProtocol.ps1` (or a re-run of the
  installer) registers it; `Test-SideCrab.ps1` and `Repair-SideCrab.ps1` now have a row per scheme,
  so an unregistered one is reported rather than assumed.
- **Two processes write `toast-state.json`.** Each replaces only its own top-level key and each
  renames a PID-suffixed temp, so an interleave cannot produce a hybrid file - but a snooze
  written in the exact window between the notifier reading and rewriting the document is lost.
  The notifier writes it at most twice a day plus a stamp every 15 min, so the window is tiny
  and the cost is one lost snooze, never a corrupt file. A lock would be the fix if it ever
  shows up.
- **The long-run toast is silent when the notifier misses the `working -> done` edge.** The
  duration exists only in the poll before the `done` (crabd clears `turnStartedAt` on Stop), so
  a notifier restarted across the transition cannot report that turn. Deliberate: the
  alternative is inventing a duration. crabd holds a done row for many polls, so this needs the
  notifier actually stopped, not merely slow.
- **`uninstall` still describes `toast-state.json` as "the digest + budget ledger".** It now
  also holds the snooze marks and the runtime stamp. The uninstaller purges the whole file, so
  behaviour is correct; only the comment in `setup\Uninstall-SideCrab.ps1` is stale - a
  `setup/` file this lane did not touch.
- **`quiet` is served as `null`, not an object,** when quiet hours are unconfigured — that is
  what production crabd 0.2.0 actually emits. Handled and tested, but worth knowing if the
  producer ever changes shape.
