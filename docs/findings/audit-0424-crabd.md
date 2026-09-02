# audit-0424 — crabd core, independent read-only lane (2026-08-28)

**Scope.** `companion/crabd.py`, weighted to everything shipped since `QA-Audit-2026-08-27.md` /
`CD-Register-2026-08-27.md` closed: the activity-clears-`needs_input` machinery, the quiet
override, `HostSampler`, `PanelLog` + `/v1/panel-log`, the never-500 guarantee, retention, and
contract conformance against `docs/STATE-CONTRACT.md`.

**Method.** Every row below is reproduced against a live crabd on an ephemeral test port
(never 2722) or against the shipped classes directly. Nothing was fixed, nothing was committed,
and nothing outside this file was touched. Repro scripts are listed at the end.

## ⚠ Tree state — read this before acting on the table

This lane was briefed against **HEAD `674ab1e`, crabd 0.24.0, tree clean**. Partway through the
run a **second, concurrent audit wave** landed in the same working tree: `crabd.py` is now
**0.25.0** (uncommitted), alongside `docs/findings/QA-Audit-2026-08-28.md` (SEC-a/SEC-c/SEC-d/
CRB-b/ORIGIN-REC and eight more). Two consequences:

1. **Every finding below was RE-MEASURED against the working tree's 0.25.0** after that wave
   landed. All twelve still reproduce. The 0.25.0 diff touches `OtlpReceiver`, `_panel_log_lines`,
   `_is_web_origin`'s docstring, `_validate_panel_approvals` (deleted) and adds `OriginRecorder`;
   it does not touch `clear_permission`, `note_permission`, `HookTracker.prune`,
   `UserConfig._write`, `GitLookup` or `HostSampler._cpu`, which is where these findings live.
2. **Two lanes now disagree, and that is the point of an independent lane.**
   `QA-Audit-2026-08-28.md` records "**No P1 defects anywhere**" and lists as *audited SOUND*:
   "broker no-auto-allow + **stand-down single-writer** + replay". A-01 and A-02 below are two
   independently-reproduced cases where the stand-down clears an alert it does not own — the
   exact invariant that line certifies. Reconciling the two registers is the orchestrator's call;
   this lane's evidence is stated so it can be checked rather than believed.

**Live-host posture (read-only measurement, `~/.sidecrab/config.json`, 2026-08-28):**
`panelApprovals.enabled` is now **`false`** — the other wave's SEC-a mitigation. That switch also
makes A-01 and A-02 **dormant**: with approvals off, `_do_hook_permission` passes through before
`register()`, so `note_permission` never fires. **They go live again the moment approvals are
re-enabled**, which is why they belong in the same decision as SEC-a rather than after it.

---

## Findings

| ID | Rank | Status | Finding |
|----|------|--------|---------|
| **A-01** | **P1** | CONFIRMED (full stack) | A **replaced** permission hold stands the card down while the **live** hold is still parked — a `pendingPermission` is served on a card reading `working` |
| **A-02** | **P1** | CONFIRMED (full stack) | When the `PermissionRequest` hook arrives **before** the CLI's own `Notification` for the same dialog, the hold expiring clears the Notification's alert — the contract says it must not |
| **A-03** | **P2** | CONFIRMED | `UserConfig._write` is not atomic: a failed write **empties** `config.json` and silently reverts the operator to `DEFAULTS` |
| **A-04** | **P2** | CONFIRMED | A `cwd` on an unreachable network path blocks the **builder thread** for 21 s per pass, re-blocking every 30 s, indefinitely |
| **A-05** | **P2** | CONFIRMED | `needs_input` rows are exempt from `prune` with **no cap and no age-out** — the tracker, `_titles` and the served `sessions` array grow without bound |
| **A-06** | P3 | CONFIRMED | `GitLookup._cache` has **no eviction path at all** |
| **A-07** | P3 | CONFIRMED | `cpuPct` is served as a fabricated `0.0` for a sub-quantum sampling window (cold start) |
| **A-08** | P3 | CONFIRMED | `idle > kernel+user` is clamped to `0.0` rather than served null — against the contract's own wording |
| **A-09** | P3 | CONFIRMED | The `total <= 0` branch's "updating `_prev` would be a no-op" comment is false |
| **A-10** | P3 | CONFIRMED | The permission stand-down writes **no** timeline event, so A-01/A-02 leave no trace anywhere |
| **A-11** | P3 | **SUSPECTED** | An untimestamped usage record is dated at **parse** time, and that value feeds the `needs_input` clearing clock |
| **A-12** | P3 | CONFIRMED | `note_activity` accepts a **future** `turn_ts` and writes it into `since`/`at` |

**Counts: 12 findings — 2 P1 · 3 P2 · 7 P3. 11 CONFIRMED, 1 SUSPECTED.**

---

### A-01 (P1, CONFIRMED full stack) — a replaced hold stands down a card whose live hold is still parked

**The shape.** `PermissionBroker.register` (`crabd.py:3952`) is *newest-wins*: a second
`PermissionRequest` for the same session **replaces** the first and releases it as a pass-through.
Two permission requests for one session is not exotic — it is what **parallel tool calls in a
single assistant message** produce.

The sequence, all four steps in the shipped code:

1. Request **A** registers. `note_permission` (`:2375`) moves `working → needs_input` and sets
   `permission_alert = True`.
2. Request **B** registers, replacing A. `note_permission(B)` is **refused** — the row is already
   `needs_input`, which is not in `PERMISSION_ALERT_FROM`. `permission_alert` stays `True`,
   still meaning "A owns this alert".
3. A's parked thread wakes with `decision None`, and `_await_permission` calls
   `clear_permission` unconditionally (`:5741`).
4. `clear_permission` (`:2416`) gates **only** on `permission_alert`, which is still `True`, so
   `_stand_down` runs: `state → working`, `question → None`.

**B is still parked.** The card now reads `working` and serves a live `pendingPermission`
carrying Approve/Deny — which is precisely the defect STATE-CONTRACT.md §v0.20.0 §2 was written
to close ("the very card carrying a `pendingPermission` could be the one card not offering it").
The widget renders Approve/Deny off the `needs_input` sheet, so the operator is offered nothing,
and B then times out at 55 s as a pass-through having never been shown.

**Measured** (`r10_parallel_perm.py`, two real long-poll HTTP holds against a live crabd, poll
shortened to 6 s; re-run on working-tree 0.25.0):

```
after A only : state='needs_input' pending={'tool': 'Bash', 'summary': 'git push', ...}
after B (A released, B still parked):
    state             = 'working'
    question          = None
    pendingPermission = {'tool': 'Write', 'summary': 'C:\x.txt', ...}
    lastEvent         = 'working'
```

**Root cause (shared with A-02).** `permission_alert` is a single boolean that can express
"a hold raised this alert" but not *which* hold, nor "something else has since re-raised it".
`_blank`'s own docstring states the intended invariant — "True only while this row's
`needs_input` is one the PermissionRequest hook raised **and nothing else has re-raised**" — and
that is exactly what the flag cannot represent.

---

### A-02 (P1, CONFIRMED full stack) — hook arrival order decides whether the operator keeps the alert

`PERMISSION_QUESTION` is, by this repo's own measurement, **word for word** the message the CLI
puts on its own `Notification` for the same dialog (contract §v0.20.0 §2). Both hooks fire within
about a second of each other.

- **Notification first** (the order the design assumes): the row is already `needs_input`, so
  `note_permission` refuses, `permission_alert` stays `False`, and the hold expiring correctly
  leaves the alert standing.
- **PermissionRequest first**: `permission_alert = True`. The identical-text Notification then
  lands on `record()` (`:2301`) where `entered` is False and the question text is unchanged, so
  `moved` is False and the `permission_alert = False` reset at `:2313` **never runs**. The hold
  expiring then stands the card down.

**Measured** (`r6_fullstack.py`, real HTTP, real long poll, both orders, same fixture):

```
order = notify-first : after hold  state='needs_input'   <- contract-correct
order = perm-first   : after hold  state='working'       <- alert lost
```

**Why P1.** The terminal dialog is still open and the operator is genuinely still being waited
on; the card silently reads `working`, the notifier's waiting/approval toasts do not fire off a
`working` row, and the glow does not light. `docs/STATE-CONTRACT.md:454-456` states the violated
rule verbatim: *"A `needs_input` a `Notification` raised (or re-raised with a new question) is
still a question genuinely waiting, and a hold merely expiring is not an answer."*

**Reachability caveat, stated because it is not pinned:** which of the two hooks the CLI emits
first is **not measured anywhere in this repo**, and I did not measure it (approvals are off on
the live host and the brief is read-only). If the order is stable and Notification-first, A-02 is
latent rather than live; if it is either-way, it is a coin flip per permission prompt. **A-01
needs no such assumption.**

---

### A-03 (P2, CONFIRMED) — a failed config write empties `config.json`

`UserConfig._write` (`:1360`) ends in a bare `path.write_text(...)` (`:1379`). `write_text` opens
with `"w"`, which **truncates before it writes**. A write that fails after the truncate — ENOSPC,
a killed process, a filesystem hiccup — leaves the file empty. The next `get()` parses nothing,
falls back to `DEFAULTS`, and the operator has silently lost `quietHours`, `budget`, `digest`,
`toast`, `continuePrompts`, `recapRepos`, `allowReply` **and `panelApprovals`**.

**Measured** (`r5_quiet_config.py` Q6 — a `write_text` that truncates then raises `OSError(28)`,
which is what ENOSPC does):

```
before:  {"allowReply": true, "budget": {...5000000}, "panelApprovals": {"enabled": true},
          "quietHours": {"end": "07:00", "start": "22:00"}}
set_quiet_override -> False        (the panel tap gets its 500)
file on disk now:  ''
reloaded config:   {"allowReply": false, "quietHours": null}
panelApprovals now: False
```

The endpoint answers `500 {"error":"could not write config"}`, which reads as "your tap did not
land" — not as "your entire configuration was destroyed". The write path is reached on **every**
`/v1/action {"action":"quiet"}` tap and every `/v1/config` save.

The security direction fails safe (`panelApprovals` reverts to OFF), so this is a settings-loss
bug, not a security downgrade. The remedy is the ordinary one: write a sibling temp file and
`os.replace` it.

**Relationship to `QA-Audit-2026-08-28.md` SET-a2.** That row fixes the *setup* layer — a
pre-write backup taken by the installer, restorable by `Restore`. It does not make crabd's own
runtime `_write` atomic, and crabd is the writer on the panel-tap path. **This half is still
open**; the two should be read together.

---

### A-04 (P2, CONFIRMED) — one network `cwd` stalls the builder for 21 s, every 30 s, forever

`_sessions` resolves `cwd` from the transcript **or from the hook payload**, then calls
`GitLookup.get(cwd)` (`:1636`). On a miss, `_read` (`:1674`) walks the path and every parent with
`.is_dir()` / `.is_file()`. Against an unreachable UNC path those stat calls block on the SMB
timeout, **on the builder thread**, inside `build()`.

**Measured** (`r11_git.py`, real call, real path `\\10.255.255.1\share\x`):

```
returned in 21.0 s -> (None, None)
```

`build()` runs on `_refresh_loop` every 2 s, so one such cwd freezes the whole document for 21 s —
past the widget's 30 s staleness threshold on a bad day — and at cold start it blocks the
**request-thread** build in `_do_state` too. The 30 s cache TTL means it re-blocks every 30 s
rather than once.

**It does not need an attacker.** A real session whose cwd is a network share that goes away (VPN
drop, NAS reboot) produces exactly this. The unauthenticated variant is worse only in that it is
permanent: a `/v1/hook` `Notification` with a UNC `cwd` creates a `needs_input` row that A-05
shows is **never pruned**, so the stall repeats for the life of the daemon.

**Also measured** (`r11_git.py` G1): every hook-supplied `cwd` reaches `GitLookup` — 500 hook
POSTs with distinct fabricated cwds produce 500 cache keys after one `build()`.

---

### A-05 (P2, CONFIRMED) — `needs_input` rows are unbounded in count and in age

`HookTracker.prune` (`:2584`) exempts `needs_input` from the `GONE_AFTER_SEC` sweep (`:2608`).
The exemption is deliberate and the contract justifies it — a question keeps waiting — but it is
**total**: there is no count cap, no age ceiling, and no second sweep. `_sessions` likewise exempts
`needs_input` from the `SESSION_WINDOW_SEC` drop, so every such row is also **served on every
poll, forever**.

**Measured** (`r1_needs_input.py` H2 / `r2_live.py` E2c, live crabd):

```
5000 Notification hooks -> /v1/state = 200, sessions=5008, 3,004,252 bytes (2.9 MB)
tracker rows after prune(+7 days) : 5005
_titles retained                  : 5006
control: the same 5000 rows as `working` -> 0 after prune(+7 days)
20000 Notification posts -> 20000 rows survive a 7-day prune
```

and (`r9_final.py` X6) a single abandoned row survives `prune(+1d / +7d / +30d / +365d)` — **and
survives an `ack`**, since `ack` sets a flag and does not move state.

Two consequences, both reachable without an attacker:

- **Healthy-night version:** a session whose terminal is closed while a question stands fires no
  `SessionEnd`, so the card alerts forever with no mechanism to retire it. `ack` silences the
  widget's escalation but the row and the card stay.
- **Unauthenticated version:** `/v1/hook` needs no auth and a `Notification` body creates a row.
  N posts = N permanent rows, N permanent cards and an N-proportional document on a 2–5 s poll.

---

### A-06 (P3, CONFIRMED) — `GitLookup._cache` never evicts

`GitLookup` (`:1622`) keys `_cache` by cwd string with a 30 s **freshness** TTL that is never used
as an eviction rule (`:1646`). Nothing removes a key, ever. Measured: 20,000 distinct cwds →
20,000 entries, ~2.8 MB, no eviction path (`r3_host_retention.py` RET1).

Alone this is memory only; it matters because it is fed by the same unauthenticated hook `cwd`
that A-04 rides. Every other store in the daemon is bounded, and was verified so in the same run
(see SOUND).

---

### A-07 (P3, CONFIRMED) — a fabricated `cpuPct: 0.0` on a sub-quantum window

`GetSystemTimes` counters advance in coarse quanta (measured on this host: **312,500 ticks =
31.25 ms** of counter movement across a 2 ms wall window). `_cpu` (`:3723`) guards `total <= 0`
but not "total is one quantum", so when idle and kernel move by the same quantum the busy
fraction is exactly 0 and `_pct` (`:3767`) serves it.

**Measured** (`r9_final.py` X1) — 300 samples ~2 ms apart on a machine that was *running the test
loop*:

```
{0.0: 197, None: 81, 50.0: 5, 25.0: 3, 16.7: 3, ...}
same sampler at the production 2 s cadence: [1.6, 2.3, 2.2, 2.1]
```

197 of 300 readings say the machine is idle; it is not. Contract (`STATE-CONTRACT.md`, v0.22.0
failure table): *"never clamped into a plausible-looking `0.0` or `100.0`"*, and *"'the machine
is asleep' is a different claim and it would be a false one"*.

**Reachability is narrow and worth stating:** at the production 2 s cadence the reading is honest.
The short window exists only where the class docstring already says two samples can overlap —
cold start, when `_do_state` builds on the request thread while `_refresh_loop` builds its first
snapshot. So the exposure is the first `cpuPct` a widget sees after a crabd restart.

---

### A-08 (P3, CONFIRMED) — a glitched counter is served as `0.0`, not null

If `idle > kernel + user`, `100 * (total - idle) / total` is negative and `_pct` clamps it to
`0.0`. Measured (`r3_host_retention.py` HS3): reader returning `(idle=1000, kernel=100, user=100)`
after a `(0,0,0)` baseline → `cpuPct = 0.0` for an arithmetic value of `-400.0`.

The sibling branch three lines up gets this right: a **backwards** counter re-baselines and
returns `None` (verified, HS4). The contract's failure table puts an unusable return in the
null column; its arithmetic block separately says "clamped to 0..100". **The two rules disagree
at exactly this input and the code takes the clamp.** Not reachable with a well-behaved
`GetSystemTimes` (idle time is a subset of kernel time), but the code already anticipates "a
rigged reader or a driver bug can" for the neighbouring case.

---

### A-09 (P3, CONFIRMED) — a load-bearing comment that is not true

`_cpu`'s `total <= 0` branch deliberately does not update `_prev`, justified in-code by
*"Updating `_prev` would be a no-op — a zero delta means this reading and the baseline are the
same tuple"*. That is false whenever `idle` moves and `kernel + user` do not: the idle baseline
goes stale and is then carried into the next window.

Measured (`r3_host_retention.py` HS5): baseline `(0,0,0)`; pass 2 `(100,0,0)` → `None`, `_prev`
left at `(0,0,0)`; pass 3 `(100,100,0)` → **`0.0`** for a window that was 100 % busy.

**Not reachable with real counters** (idle ticks *are* kernel ticks, so idle cannot advance while
kernel does not). Filed because the comment is the *reason* the code omits the assignment, and a
future reader will trust it. The deliberate action is still correct; the justification is not.

---

### A-10 (P3, CONFIRMED) — the stand-down is silent

`_stand_down` (`:2361`) writes no ring event. `note_activity` persists
`NEEDS_INPUT_CLEARED_EVENT` ("answered outside the panel") for the *same* transition; the
permission path does not.

Measured (`r9_final.py` X5): events before `clear_permission` `['prompt submitted']`; after,
identical. In the A-02 full-stack run the timeline reads
`['permission passed through: Bash', 'asked a question', 'permission requested: Bash',
'prompt submitted']` — nothing anywhere says the alert was dropped, in `events` or in
`history.jsonl`. **This is what would make A-01/A-02 undiagnosable in the field**, which is why it
is filed rather than folded away.

---

### A-11 (P3, SUSPECTED) — a record is dated at parse time, and that date clears alerts

`FileFacts._consume_record` (`:1921`): `record_ts = ts or self.last_ts or time.time()`. An
assistant record carrying `usage` + `requestId` but no parseable `timestamp`, in a file where no
earlier record set `last_ts`, is dated **now** — the moment crabd happened to read it. That value
becomes `context_ts` → `turn_ts` → `note_activity`, the clock that clears `needs_input`.

Measured (`r7_framing.py` R8/R9): a timestamp-less usage record yields `context_ts == time.time()`
to the millisecond, and a fresh `FileFacts` over the *same unchanged line* 0.2 s later dates it
0.2 s later. Feeding that into `note_activity` clears a standing question.

**SUSPECTED, not confirmed, and the honest bound:** it needs (a) a real transcript to omit
`timestamp` on the first usage-bearing record — I have not observed one — and (b) a store
eviction + re-admission to re-parse from offset 0 (reachable via a transient `OSError` in
`_transcripts`, which drops a project's files out of the `seen` set). The safer fallback is to
**skip** such a record, which is what the never-500 rule does everywhere else, rather than to
date it now.

---

### A-12 (P3, CONFIRMED) — a future `turn_ts` is written straight into `since`

`note_activity` (`:2327`) sets `row["since"] = at` and `row["at"] = max(row["at"], at)` with no
ceiling against crabd's own clock. Measured (`r1_needs_input.py` H4) with a transcript timestamp
one hour ahead: `since` and `at` both land **3600 s in the future**. `stateSince` then serves a
future ISO time (the widget computes a negative age), and the future `at` postpones `prune` by
the same hour. Same-machine clocks make this unlikely; an NTP step or a transcript copied in from
elsewhere makes it possible. `_parse_ts` already bounds the *range*; nothing bounds it against
*now*.

---

## Audited SOUND — what I attacked that held

Listed so the coverage is checkable, not only the defects. All re-run against working-tree 0.25.0.

**The never-500 guarantee — held under everything I could build** (`r4_never500.py`):
a 150 KB poison transcript carrying ~55 hostile record shapes (non-dict `message`/`usage`,
counters as dict/list/string/`NaN`/`Infinity`, `1e400`, timestamps of `1e30`/`-1e30`/`True`/`{}`/
`+99:00`, content as string/int/`[None]`, a 100 KB question, cwds of `1`/`[]`/`\x00`/3000 chars,
torn JSON, a bare BOM, a 40 KB line) → `build()` clean, **no stderr**, `200`, parseable. Then 100
rounds of randomized poison across `/v1/statusline`, `/v1/metrics`, `/v1/logs` and `/v1/hook`
(400 POSTs) with a build and a GET after each: **0 failures**. 20 poisoned `config.json` shapes
(`1e309`, `NaN`, bools in numeric slots, non-dict blocks, `quietOverride` as a list): **0** non-200
or unparseable. 24 threads × 6 concurrent **cold-start** GETs with no snapshot: 144/144 `200`,
no client errors, no server stderr. No `NaN`/`Infinity` ever reached the wire.

**Contract conformance — clean in both directions** (`r8_contract.py`). A document served from an
adversarial-but-legal state (needs_input + enriched transcript question + live
`pendingPermission` + `queuedContinue` + subagent + budget + `quietOverride` "off" against a live
window + duplicate/over-long `continuePrompts` + `approvalThresholdSec`) diffed field-by-field:
**no missing key, no extra key**, top level or per session. 27 documented invariants checked and
held — `schema 5`, 24 hourly / 7 daily buckets, `byModel ≤ 4`, `costUSD`/`costSource` always
present, `budget` present-when-configured, `quiet.override` present and winning in both
directions, `start`/`end` remaining the *schedule*, `toast.approvalThresholdSec` only-when-set,
`continuePrompts` dedup/trim, `events ≤ 8`, `subagentDetail ≤ 5`, `contextSource` domain,
`pendingPermission`/`queuedContinue` shapes, sort order, `501` for reply.

**The quiet override** (`r5_quiet_config.py`). Full vocabulary matrix: `on`/`off`/`auto` only,
`minutes` integer 15..480 with float/bool/string/absent all `400`, `auto` ignoring `minutes` and
idempotent. Expiry half-open (`until <= now` is expired) and consistent with the window's
exclusive `end`. Override wins in **both** directions, over a live window and with no schedule at
all (`start`/`end` null). Zero-length window is not "always quiet". Expired override reads as
absent. **DST**: spring-forward, post-jump and fall-back-ambiguous instants all resolve correctly
(absolute-epoch `until` vs local-minute window is the right split). `quietOverride`,
`panelApprovals`, `allowReply`, `allowContinue`, `recapRepos` all `400` on `/v1/config`. A
`/v1/config` write **preserves** a live override and every file-only key. 180 concurrent
writes across 3 threads (`set_quiet_override` ×2 + `set_keys`): 0 exceptions, both keys intact,
no lost update.

**`PanelLog` + `/v1/panel-log`** (`r2_live.py`, `r7_framing.py`). Ring bounded at 500 lines /
~22 KB under a 2,000-line flood with `droppedTotal` accounting for every eviction; over-cap
bodies (25 MB, 15 MB) rejected before allocation; the legal worst case (700×5000 chars) accepted,
truncated to 300. Every malformed shape `400`s with the single shared body. **Gate ordering is
correct**: a cross-site POST is `403` *before* the ring is touched (`EVIL` never stored); a
cross-site GET is `403`; the preflight emits no ACAO for an http(s) origin and reflects `null`.
Whitespace-only lines stored as bare prefixes — which the contract explicitly specifies
("A line that trims to empty is stored as an empty line"), so **not** a finding.
**Keep-alive framing survives everything**: pipelined POST+GET on one connection, chunked
transfer-encoding, a `Content-Length` that lies (5 MB claimed, 20 bytes sent → `400`, server
still answering), a `FFFFFFFF` chunk header, and a 6 MB body (`400`, connection deliberately
closed past `BODY_DRAIN_MAX` — documented behaviour).

**`HostSampler`** apart from A-07/A-08/A-09. `_MEMORYSTATUSEX` is 64 bytes with field offsets
0/4/8/16/24/32/40/48/56 — exact match to the Win32 struct, `dwLength` set before the call;
`_FILETIME` 8 bytes; the two-word FILETIME assembly is correct across a 2³² boundary (50.0 %
where 50.0 % is true). First sample is `None`, never `0.0`. A backwards counter re-baselines and
returns null. Six hostile CPU readers (raising, `None`, wrong arity, strings, floats, `NaN`) and
seven hostile memory readers (avail>total, total 0, negative, `inf`, `NaN`, bools, raising) all
return honest nulls and **none raises**. 4,000 samples across 8 threads: 0 exceptions — the
`_prev` lock holds. Against the real kernel: `cpuPct 1.7`, `memPct 35.8`, `22.1 / 61.7 GiB`,
which agrees with the machine.

**Retention, everywhere except A-05/A-06.** Forecaster keys capped at 64 under a 5,000-label
flood with the two contract windows protected; OTLP cumulative series capped at 512;
`ContinueQueue` and `StatusLineReader` fully drained by `prune`; `PanelLog` at 500;
`HistoryLog` rotating at 2 MB × 2 generations (40,000 appends → 1.6 MB live + 2.1 MB `.old`),
`replay()` bounded by `HISTORY_REPLAY_SEC`, and replay restoring **only** terminal states
(500 rows, all `done`, no resurrected `working`).

**The permission broker.** AUDIT-F3 regression re-run 400 rounds of `decide` racing `release`
at the exact expiry instant: **0** disagreements between what history claims and what the hook
was told. `release` remains the authority under the lock. No path from timeout, saturation,
disabled config or exception to `allow`.

**`MUTATING_PATHS` drift check** — the BACKLOG notes nothing asserts it matches the real route
table. Extracted both at HEAD and diffed: 9 declared, 9 routed, **zero drift either way**. (The
absence of a *test* stands as an open BACKLOG row; the current state is clean.)

**`/v1/history`.** Well-formed day → 200; malformed, impossible (`2026-02-30`), empty, duplicated,
trailing-`%0A` and Arabic-Indic-digit days → 400, all through the regex+`strptime` pair. A
`history.jsonl` poisoned with torn JSON, `NaN`, `Infinity`, `1e400` and wrong-typed fields still
serves 200, still parses, still returns the good lines on either side of the poison, and puts no
`NaN`/`Infinity` on the wire.

---

## Repro inventory

All under the session scratchpad
`…\7b1dbf2f-39ef-499e-a3e9-d04f14cdd313\scratchpad\a0424\`. Each is standalone
(`python <file>`), binds only ephemeral ports, and asserts `port != 2722`.

| Script | Covers |
|---|---|
| `r1_needs_input.py` | A-02 (unit), A-05, A-12; grace boundary; re-fire semantics |
| `r2_live.py` | PanelLog flood/gates, 20 poisoned configs, hostile hooks, A-05 at 5000 rows |
| `r3_host_retention.py` | A-06, A-08, A-09; HostSampler struct/ctypes/lock/failure tiers; bounded-store controls |
| `r4_never500.py` | never-500: poison transcript, 100 randomized rounds, concurrent cold starts, `/v1/history` |
| `r5_quiet_config.py` | A-03; quiet vocabulary/bounds/DST/precedence; config whitelist; concurrency; `MUTATING_PATHS` drift |
| `r6_fullstack.py` | **A-02 full stack** (both hook orders, real long poll); `/v1/history` poisoning |
| `r7_framing.py` | keep-alive framing (pipelined/chunked/lying CL/over-cap), origin gates, A-11 |
| `r8_contract.py` | contract conformance diff, 27 invariants |
| `r9_final.py` | A-07, A-10; history rotation + replay; F3 race regression ×400 |
| `r10_parallel_perm.py` | **A-01 unit + full stack** |
| `r11_git.py` | A-04 (21 s UNC block), A-06 reachability from hook `cwd` |

**Suite baseline:** `pytest companion/tests` — **1012 passed, 48 subtests** in 193 s, taken at
`674ab1e` before any of this work. No repo file was modified by this lane.
