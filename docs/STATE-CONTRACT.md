# SideCrab state contract — `/v1/state` (schema 5-compat, feature-detected)

> **The vendor widget host is retired (2026-09-21, crabd 0.34.0 / widget 0.32.0).** The panel's
> only consumer is the standalone panel host showing the page crabd serves at `/panel/`. Every
> version section below that describes the widget running inside the vendor's frame is the dated
> record of that time and is not edited; the current transport, gates and host bridge are the
> newest section. The retirement itself is in the maintainers' history.

> **VERSIONING REWORK (v0.6.1/crabd 0.6.1, 2026-08-26).** The strict schema whitelist coupled
> every crabd deploy to a console-bound widget import (schema N+1 bricked the on-glass widget
> until someone stood at the desk). New policy:
> - `"schema"` now marks the last **BREAKING** shape — pinned at **5** until a break actually
>   happens. Additive features (contextTokens, fleet, and everything after) are detected by
>   FIELD PRESENCE, never by schema number.
> - The widget accepts `schema` 1–5; a value above its ceiling is still a dead feed (that's
>   what a real break looks like). Unknown top-level or per-session KEYS are always ignored.
> - crabd may therefore ship new additive fields at any time without a widget import; the
>   widget lights features up whenever it is next updated. Breaks require bumping `schema`
>   AND a coordinated deploy — which is exactly why they should be rare.
> The "Schema 6" section below is retitled in place: its FIELDS are unchanged and live; only
> the schema NUMBER they ride on is now 5.

## v0.35.0 (2026-09-22 — ADDITIVE: six session members from the transcript, one new hook route; schema stays 5)

Six additive members on `sessions[]`, all derived from the transcript Claude Code already
writes, plus one new hook event the installer registers. Nothing is removed and no answer
changes shape.

**The rule that governs all six: absent, never zero.** A member is present only when
crabd has something to say. `"filesTouched": {"count": 0}`, `"promptQueue": 0`,
`"todos": {"done": 0, "total": 0}` and `"compaction": {"count": 0}` are all claims about
a session crabd may simply not have parsed yet, and a panel that renders them looks
equally confident when it knows and when it does not. Presence is the feature detection,
exactly as it is for `queuedContinue` and `host`.

### 1. `sessions[].activity`: what the current turn is doing

```jsonc
"activity": { "tool": "Bash", "detail": "run the tests",
              "at": "2026-09-22T06:11:04Z", "callsThisTurn": 7 }
```

The newest `tool_use` of the **current turn**, and how many tool calls that turn has made.

**Present only while the session is `working`, and only when this turn has used a tool.**
On a card that has finished or gone quiet the last tool of the last turn is history, and
rendering it beside `done` would read as a tool still running.

**`callsThisTurn` counts the TURN, not the session.** It resets at the user prompt that
opens a turn. A tool result arriving back (a `user` record with list content) is not a
prompt and does not reset it.

**`detail` comes off a per-tool allowlist, and the command text is never a candidate:**

| tool | `detail` |
|---|---|
| `Bash`, `PowerShell`, `Agent`, `Task` | the `description` input |
| `Edit`, `Write`, `Read` | the **leaf** of `file_path`, never the full path |
| `Grep`, `Glob` | the `pattern` input |
| anything else | `null` |

A shell command, a file's contents and a prompt can each carry a secret, and this member
is rendered on a screen on a desk. A tool whose detail is not on that list serves `null`;
it never falls back to another key of the same input. A tool crabd has never heard of is
served with its own name and a null detail, and **still counts** toward `callsThisTurn` -
the number is how busy the turn is, not how many of its tools this build recognises.

`detail` is capped at 80 characters, `tool` at 40. `at` is the tool_use record's own
timestamp.

### 2. `sessions[].mode`: the CLI's permission mode

```jsonc
"mode": "plan"
```

The newest `mode` record's value, **lower-cased and otherwise passed through**. The values
seen so far are `normal`, `plan`, `acceptedits` and `bypasspermissions`; a mode a later
CLI introduces reaches the panel unchanged, because a whitelist here would show nothing
at all while a session was in it. Absent when the transcript carries no `mode` record.

### 3. `sessions[].filesTouched`: what this session has changed

```jsonc
"filesTouched": { "count": 12, "recent": ["crabd.py", "test_crabd.py", "README.md"] }
```

Distinct `file_path` values from **`Edit` and `Write` only**. `Read` is deliberately
excluded: the member answers "what has this session changed", and folding reads in would
make every card claim a hundred files on a session that changed none.

`count` is distinct **full paths**, so two files called `config.py` in different
directories are two files. `recent` is the last five **leaves**, newest first, and a
duplicate leaf is kept as two entries rather than collapsed - they are two files, and
de-duplicating the display would under-count what is on the panel. Absent when nothing has
been touched.

### 4. `sessions[].promptQueue`: Claude Code's own typed-ahead depth

```jsonc
"promptQueue": 2
```

Counted from the CLI's `queue-operation` records: `enqueue` minus `dequeue` minus
`remove`. **Floored at zero and then omitted when zero**, because crabd can start reading
a transcript mid-session and meet a dequeue whose enqueue it never saw; a negative depth
is arithmetic, not a queue. An operation this build does not recognise moves nothing.

This is **not** `queuedContinue`, which is SideCrab's own tap-to-continue queue and keeps
its existing shape and meaning.

### 5. `sessions[].compaction`: context compactions, past and running

```jsonc
"compaction": { "count": 2, "lastAt": "2026-09-22T05:12:44Z", "inProgress": false }
```

`count` and `lastAt` come from the CLI's `system` records with
`subtype: "compact_boundary"`. **`inProgress` cannot come from the transcript at all** - a
compaction that has not finished has written nothing - so it comes from a new `PreCompact`
hook and is true between that hook arriving and the next write to the transcript.

Absent when the session has never compacted **and** nothing is in progress. A session
compacting for the first time therefore serves `count: 0` with `inProgress: true`, which
is the one place a zero appears here and it is a measured zero, not an absent one.

#### The new hook route: `POST /v1/hook/precompact`

`PreCompact` is registered in `hooks/settings-hooks-fragment.json` as a `command` entry
alongside the other five fire-and-forget hooks, posting to `/v1/hook/precompact`. Same
body, same two gates (Host then Origin), same 204-before-the-parse answer as `/v1/hook`:
the session is about to compact a large context and nothing crabd does may sit in front
of that.

It has its **own path** rather than riding `/v1/hook` because it records a fact about the
session, not a state transition. It moves no state, dates no `since`, writes no timeline
event and does not count as activity - it only stamps when it arrived. It does count as a
hook for `sources.hooks` and `/v1/health`.

### 6. `sessions[].todos`: the session's own task list

```jsonc
"todos": { "done": 3, "total": 7, "current": "wire the PreCompact route" }
```

From the newest `TodoWrite` input. `current` is the `in_progress` item's `content`, or its
`activeForm` when there is no content, capped at 80 characters; it is `null` when nothing
is in progress. The list itself is never kept or served - it is the operator's own working
notes.

An **empty** `TodoWrite` is the list being cleared, and clears the member rather than
serving `{"done": 0, "total": 0}`. Absent when the session has never written one. Installs
that do not use `TodoWrite` never see this member at all.

### Which file a member comes from, on a session whose cwd moved

A session id can own a main transcript under two project directories (SCA-001). The four
members that describe what the session is doing **now** - `activity`, `mode`,
`promptQueue`, `todos` - come from the **identity file**, the one the deterministic
latest rule picks, for the same reason `cwd`, `title` and `model` do. The two that are
session **history** - `filesTouched` and `compaction` - are aggregated across every main
file, the way `agent_labels` and the usage records already are.

---

### Correction to the v0.34.0 `sources` section (2026-09-22)

Two sentences in the shipped v0.34.0 text describe behaviour that was wrong in practice.
Both were measured on the live companion on 2026-09-22.

**`limitsToken` is absent while the status line is serving `limits`.** The OAuth reader is
only consulted when the status line has gone quiet, so while the status line is serving it
is not a feed crabd can judge - it stops being polled, and the entry froze on its last
verdict with an `ageSec` that grew forever. The v0.34.0 rule "a source crabd cannot judge
is ABSENT from the object" now covers this case too, and `statusline` is the entry that
judges what is actually filling the gauges.

**`limitsToken.note` during a rate-limit lockout names the lockout, not the reading's
age.** It previously passed through the served `limits` block's own caveat ("limits as of
11:30 PM"), which describes a healthy reading and was being served as the explanation for
a source marked not-ok. The two notes answer different questions and stay separate: the
`limits` block's is a qualification beside lit gauges, the source's is a diagnosis.

## v0.34.0 (2026-09-21, ADDITIVE: source health, approval readiness, cancel, a wider config write; REMOVED: `fleet.glow`; schema stays 5)

Two additive top-level members, two additive members inside `approvals`, one new action,
one new route, and three new writable config keys. One thing is **removed** and one
answer **changes shape**; both are called out below. `schema` stays **5**: an older panel
ignores everything additive here, but see the two breaking notes before shipping it
against an old consumer.

### 1. `sources`: one freshness verdict per feed

```jsonc
"sources": {
  "hooks":       { "ok": true,  "lastAt": "2026-09-22T04:39:57Z", "ageSec": 1.8, "note": null },
  "transcripts": { "ok": true,  "lastAt": "2026-09-22T04:39:58Z", "ageSec": 0.0, "note": null },
  "limitsToken": { "ok": true,  "lastAt": "2026-09-22T04:38:58Z", "ageSec": 60.0, "note": null },
  "hwinfo":      { "ok": false, "lastAt": "2026-09-22T04:38:25Z", "ageSec": 94.7,
                   "note": "HWiNFO stopped publishing (free build 12-hour limit): readings are old" },
  "gpu":         { "ok": true,  "lastAt": "2026-09-22T04:39:57Z", "ageSec": 1.8, "note": null }
}
```

**Present when crabd can judge at least one source, and absent otherwise.** Presence is
the feature detection, as it is for `host`. The members are `hooks`, `transcripts`,
`statusline`, `limitsToken`, `otlp`, `hwinfo` and `gpu`, each
`{ "ok": bool, "lastAt": iso|null, "ageSec": number|null, "note": string|null }`.

**A source crabd cannot judge is ABSENT from the object.** Never a false `ok`, never a
false failure. The status line and the OTLP exporter are optional wiring an operator may
simply not have done, and "never seen" does not tell that apart from "stopped", so
neither key appears until that source has spoken once. `hwinfo` and `gpu` appear only
when their readers are attached. A consumer must therefore iterate what is there, not
look up a fixed list.

**`ok` means the source produced inside its own window - and silence with nothing to
report is `ok`.** Hooks, the status line and telemetry are event-driven: on a night with
nobody working, silence is the correct reading, and a panel that goes amber every night
is a panel nobody reads. crabd judges them only while a session is actually running,
which it knows from the newest transcript mtime - evidence that does not come from the
hooks, so it can be used to judge them. `hooks` additionally waits out a 15-minute
uptime grace, because hook rows do not survive a crabd restart and a crabd restarted
mid-turn holds none until that turn's Stop.

**`note` is one short human reason, and only when `ok` is false.** It is null whenever
`ok` is true, so a consumer can render it without checking. **`lastAt` and `ageSec` are
null when the source has never produced.** Absent stays absent: there is no zero here.

`hwinfo`'s entry is the same verdict `host.sensorsSource` already carries, plus one the
reading alone cannot give: a sampler that has stopped POLLING while its last reading is
still young reads `ok: false` with "the HWiNFO sampler has stopped polling".

### 2. `approvals` gains `readiness` and `verifiedAt`

```jsonc
"approvals": { "enabled": true, "tokenRequired": true,
               "readiness": "unverified", "verifiedAt": null }
```

`enabled` and `tokenRequired` are **unchanged**. The two new members answer a question
the panel could previously only ask by sending a real `decide` and having it refused:

| `readiness` | means |
|---|---|
| `off` | approvals are disabled; no tap can decide anything |
| `no-token` | enabled, but crabd holds no pairing code at all - an unwritable `~/.sidecrab`, or a file it quarantined and could not replace |
| `unverified` | a code exists and nothing has yet proved this panel has it |
| `ready` | a code was verified in this crabd process |

`verifiedAt` is the ISO time of that verification, or null. It is **not** cleared by
`off`: it is a fact about what happened, not a second copy of the enable flag. It is held
in memory, so a crabd restart reads `unverified` again - what was verified was a panel
this process can no longer see.

The host's `hasToken` is a different claim (a token file exists on the host side) and is
untouched.

### 3. `POST /v1/approvals/verify` - is this the right code

```jsonc
POST /v1/approvals/verify
{ "code": "K7QXM-2PDAB" }
```

| answer | when |
|---|---|
| **204**, no body | the code matches; `approvals.readiness` becomes `ready` and `verifiedAt` is stamped |
| **403** `{"error":"pairing code rejected"}` | it does not match |
| **429** `{"error":"too many attempts - wait a minute"}` | five attempts have already been made inside a minute |
| **400** `{"error":"code required"}` | no `code`, or not a non-empty string. The shape gate spends no attempt |
| **400** `{"error":"malformed request"}` | the body is not a JSON object |
| **503** `{"error":"panel pairing unavailable"}` | crabd holds no PanelToken at all |

**It can never allow or deny anything.** There is no reference to the permission broker
on this path: no body and no ordering of requests turns a pairing check into an approval,
and a pending permission is still pending after a successful verify. **It never returns
the code**, on any answer.

The Host allowlist and the origin gate are **exactly** `/v1/action`'s. The attempt
budget is its own and is deliberately separate from the `decide` lockout, so neither
route can spend or clear the other's: a panel checking its pairing cannot lock the
operator out of Approve and Deny. The budget counts **attempts**, not failures.

`panelApprovals` being off does **not** gate this route. Pairing is checked before
approvals are armed, which is exactly when the answer is worth having, and verifying
arms nothing.

### 4. `POST /v1/action cancel-continue` - withdraw a queued continuation

```jsonc
POST /v1/action
{ "action": "cancel-continue", "sessionId": "…" }
```

| answer | when |
|---|---|
| **204**, no body | a queued item was removed. `sessions[].queuedContinue` clears on the next build, and the history line `continue cancelled: <prompt>` is written |
| **409** `{"error":"already delivered","deliveredAt":"…"}` | a Stop hook already has it |
| **404** `{"error":"nothing queued"}` | nothing queued, and nothing recently delivered |
| **403** `{"error":"tap-to-continue is disabled"}` | `allowContinue` is false in the config file |
| **501** `{"error":"continue not supported"}` | this crabd has no continue queue |

Replacing a queued prompt with another one was the only way to change your mind before
this, and replacing is not cancelling - the session still gets told to do something.

**The race is part of the contract.** A Stop hook can fire between the tap and this
handler, and the two answers are not interchangeable: 204 means the session will not act
on it and 409 means it will. The queue settles the order under one lock, so exactly one
of the two wins and the loser is told which it was, with the time the delivery was
taken. An item the Stop hook has CLAIMED but not yet finished sending counts as
delivered: the answer is being written and there is no instant at which crabd could take
it back. A send that never reached the socket releases the claim, because the prompt is
kept for the next Stop and is still the operator's to withdraw.

**An expired item is `nothing queued`, not a cancellation.** The card stops showing a
queued continue at the ten-minute TTL, so it is not what the operator is cancelling.

There is deliberately **no session-existence gate**, unlike `queue-continue`: a prompt
queued for a session that has since gone quiet is the one an operator most wants to
withdraw, and "unknown session" would strand it until the TTL.

### 5. `POST /v1/config` - three new keys, and the answer now has a body

**BREAKING for any consumer that asserts 204.** The route answered `204 No Content`. It
now answers **`200`** with:

```jsonc
{ "applied": { "quietHours": { "start": "07:05", "end": "23:09" } },
  "warnings": [] }
```

`applied` is the **normalised** value now on disk, so a sheet renders what it actually
got: `"7:5"` comes back as `"07:05"`. `warnings` is a list of short strings, empty in
the ordinary case.

`CONFIG_WRITABLE` gains `continuePrompts`, `continuePromptsByRepo` and
`continuePromptsByPath`, validated exactly as the config FILE parser validates them.
That parser **drops** bad entries rather than failing, and a drop the operator cannot
see is a setting that silently did not take - so each drop is a warning:

- an entry that is not text, is blank, is over 200 characters, or duplicates another;
- a prompt that is already a builtin button (it would be drawn twice);
- a repo key that differs from an earlier one only in case (the first still wins);
- a path key that is not absolute (it could never match a session cwd);
- anything past the 20-prompt or 50-project cap.

The key's **own** shape being wrong - a list where an object belongs, or the reverse -
is still a **400** that writes nothing. Only keys present in the body are written, a
`null` value clears that key, and every other key in the file survives unchanged.

**An empty list is written and returned as an empty list.** It is precedence-bearing
configuration, not an absent key: an empty `continuePromptsByPath` entry is how an
operator says "this subtree gets none of the parent's extras".

`panelApprovals`, `allowReply`, `allowContinue` and `recapRepos` stay file-only, and a
body naming any of them is still rejected whole.

### 6. REMOVED: `fleet.glow`

`fleet` is now `{"toast": "running"|"stopped"|"absent"|"unknown"}`. The glow component
was retired with the Corsair RGB path (`docs/history/RGB-retired-2026-09-21.md`), and a
key that could only ever report a task that is not there is a fault light nobody can
clear. **A consumer reading `fleet.glow` must stop.** Everything else about `fleet` -
the four outcomes, the ~60 s cache, `unknown` never being folded into `stopped` - is
unchanged.

### 7. The origin gate is narrower (transport, CLEAN-04)

Exactly two kinds of request pass the origin gate now:

- a request with **no `Origin` header**, which is every native client: the CLI's Stop and
  PermissionRequest hooks, the status line command, the notifier, the setup scripts and
  curl. Measured on the live companion - `GET /v1/health.originsSeen` holds only
  `<absent>` pairs. This is not authentication and never was; the Host allowlist and the
  pairing code are the gates that are;
- **exactly one of this server's own origins**, `http://127.0.0.1:<bound port>` or
  `http://localhost:<bound port>`, which is what the crabd-served panel page sends.

`null` and the non-web schemes (`file:`, `qrc:`) are now **refused 403** on every route,
reads included, and get no `Access-Control-Allow-Origin`. They were allowed for the
the previous host's file/qrc page, which this wave retires; `null` is also the one origin a
sandboxed allow-scripts iframe on a visited page can forge, so closing the allowance
closes that vector rather than merely bounding it with the pairing code.

The Host allowlist (421) and the pairing gate on `decide` are **unchanged**.

### What did not move

`schema` stays **5**. `sessions[]`, `burn`, `limits`, `quiet`, `recap`, `toast`,
`continuePrompts`, `host` and `generatedAt` are untouched in shape and meaning. The
per-session `queue-continue` allowlist from v0.33.0 is unchanged; a session whose cwd
moved is now served under the project it is actually in, which is the same rule applied
to better data rather than a new rule.

### The two things to read before shipping this against an old consumer

1. **`POST /v1/config` answers 200, not 204.** Anything asserting 204 fails.
2. **`fleet.glow` is gone.** Anything reading it fails. The widget's `FLEET_PARTS` still
   names it and `renderFleet` turns an absent key into `unknown`, so the panel draws a
   permanently grey glow dot until that entry and its `fleetGlow` element are removed.

Everything else is additive and presence-detected.

## v0.33.0 (2026-09-21 — ADDITIVE: a continue vocabulary per project, and a per-session `queue-continue` allowlist; schema stays 5)

One additive per-session field and two new config keys. Nothing existing moves or changes
meaning, `schema` stays **5**, and an older widget ignores all of it.

### 1. `sessions[].continuePrompts`: this session's own extra continue buttons

```jsonc
"sessions": [
  { "id": "…",
    "repo": "acme-api",
    "continuePrompts": ["Rebuild the report", "Run the migrations"] }
]
```

**Present only when this session has project prompts, and absent otherwise.** Presence is
the feature detection, as it is for `queuedContinue` and `host`. The key is deliberately
**not** served as `[]`, which is what the top-level `continuePrompts` does: at the top
level always-present is the contract and empty is the ordinary case, while an empty list
on a row would be a claim that crabd looked at this project and found it configured with
nothing. Absent stays absent.

The strings are the operator's own, already filtered: crabd drops anything that is
builtin or already in the top-level list, so a consumer appends them without checking.
The rendering order is builtins, then the top-level `continuePrompts`, then these.

The top-level `continuePrompts` is **unchanged** in every respect, including being
always present and `[]` when unconfigured.

### 2. `POST /v1/action queue-continue` is now checked per SESSION

The whitelist a tap is checked against was the builtins plus the top-level extras. It is
now the builtins, plus the top-level extras, plus **that session's own**
`sessions[].continuePrompts`. Consequences for anything driving crabd out of band:

- A prompt configured for repo X and POSTed for a session in repo Y is refused **400**
  `{"error":"prompt must be one of the configured continue prompts"}`, the same answer an
  invented string has always had. There is no new status code and no new error body.
- The gate order is unchanged: shape before existence. An unknown `sessionId` carrying a
  builtin prompt is still **404** `{"error":"unknown session"}`, and an unknown session
  carrying another project's prompt is **400**, because the prompt is checked first. An
  unknown session is therefore checked against the global set, never against a project's.
- The set is still server-side, still a whitelist, and still unwidenable over HTTP: both
  new config keys are file-only and deliberately absent from the `/v1/config` whitelist.
  A project map widens what a **given session** may say, never who may say it.
- The session's `repo` and `cwd` are read off the **served document**, not re-derived. The
  allowlist is therefore built from the same `repo` the consumer drew its buttons from; a
  second derivation could answer differently inside one poll (the git cache is 30 s) and
  refuse a button that was on the glass.

### 3. Two new `config.json` keys, file-config only

```jsonc
{
  "continuePromptsByRepo": {
    "acme-api": ["Rebuild the report", "Run the migrations"],
    "orbit-desktop": ["Sign the installer"]
  },
  "continuePromptsByPath": {
    "C:\\Work\\acme-api-lane-b": ["Fold the lane in"]
  }
}
```

- **`continuePromptsByRepo`** is keyed on `sessions[].repo`, the name crabd derives from
  the origin remote, matched **case-insensitively**. Two keys differing only in case do
  not merge: the first wins and the second is dropped with a `config.json` line.
- **`continuePromptsByPath`** is keyed on an absolute path **prefix** of
  `sessions[].cwd`, matched at a path boundary, separator- and case-normalised, and the
  **longest matching key wins** (one key only, never several). It exists because
  `sessions[].repo` cannot name every project: measured 2026-09-21, a release tree and a
  linked worktree of the same repo both read `sidecrab`, and a session whose cwd is not a
  repo reads `null`. A relative key is dropped with a line, because it could never match.
- A session's extras are the repo list then the path list, deduped against each other,
  against the top-level extras and against the builtins.
- **Validated exactly as the top-level list is**, and parsed defensively because this is
  hand-edited JSON: a map that is not an object, a value that is not a list, non-strings,
  blanks, over-long entries and duplicates are dropped, each with one `config.json` line
  on stderr rather than silence or a crash. Per list: 20 prompts of at most 200
  characters. Per session: 20 combined, the repo list filling first. Per map: 50 keys,
  which bounds the parse rather than a request. **The builtins can never be lost to a
  typo**, which is the same guarantee the top-level list has.

## v0.32.0 (2026-09-21 — ADDITIVE `host` members + TRANSPORT: `GET /v1/events`, the panel-host bridge; schema stays 5)

crabd `VERSION` → `0.32.0`, widget `0.30.0`, panel host `0.2.0`. Four additive members inside `host`
(§1 to §4, lane A), one new route and a host bridge (§5 to §8, lane B). Nothing existing moves or
changes meaning; `schema` stays **5** and an older widget ignores all of it.

Four new members inside the existing top-level `host` key, from three samplers that each
own a thread and a 5 s cadence. No existing member moves and none changes meaning, so
`schema` stays **5** and no widget import is needed.

**`host` may now exist without its v0.22.0 members.** Until this release the block was
present only when `GetSystemTimes` or `GlobalMemoryStatusEx` could be read. A machine
whose kernel counters both fail can still have a readable GPU or a readable HWiNFO
mapping, so the block is now served whenever *any* of its sources answered. Readers that
already test each member for presence (the contract has required this since v0.22.0) need
no change; a reader that assumed `host` implies `cpuPct` does.

### 1. `host.sensors` — a curated list of this machine's hardware sensors

```jsonc
"sensors": [                       // present only when the HWiNFO reader is wired
  { "name":   "CPU (Tctl/Tdie)",   // the user label, falling back to the original
    "device": "CPU [#0]: AMD Ryzen 9 9950X3D2: Enhanced",   // the sensor it belongs to
    "kind":   "temp",              // temp | fan | volt | power | clock | usage | other
    "unit":   "°C",                // as HWiNFO spells it
    "value":  62.1 }               // number, or null when the reading is not finite
]
```

**Read from HWiNFO's shared memory, stdlib only** (`OpenFileMappingW` plus `ctypes`, never
`mmap`'s `tagname`, which *creates* the section when none exists). HWiNFO publishes
`Global\HWiNFO_SENS_SM2` while its Sensors window is open; crabd opens it read-only, takes
one copy, and parses that.

**The list is CURATED and CAPPED at 24, and the cap is a policy rather than a slice.** On
the reference machine 37 of 524 readings pass the rank test, of which 10 are CPU
temperatures and 10 are GPU temperatures. Filling the list in rank order and taking the
first 24 keeps those twenty and drops every fan, every drive and the package power, which
is a cap deleting whole *kinds*. So the list is filled one reading per rank per pass: every
kind that exists gets a place, and only depth within the largest kinds is trimmed.

What is served, in rank order: CPU temperatures (`Tctl`, `Tdie`, CCD, die, package), GPU
temperatures, VRM, drive, motherboard and chipset, CPU package power, and every fan and
pump. The ranks are matched against the reading's **label**, never against the sensor name:
the per-core temperatures sit under a sensor called `CPU [#0]: …`, so a device-side match
sweeps all sixteen of them in as CPU temperatures. Specificity decides the walk order, not
rank order, because this board labels its VRM probes `CPU VDDCR_VDD VRM (SVI3 TFN)`.

**Four exclusions, each measured rather than imagined.** `Accumulated CPU Temperature`
reads 194,117,396 °C and `Accumulated CPU Power` 135,046,525 W: counters wearing a
temperature's name and unit. `Temp9` is an unpopulated board header, a number with no
subject. The sixteen per-core temperatures, the two L3 cache temperatures and the eight
`GPU Memory A0..C1` rows are detail a one-line row has no width for. Under the name gates
sit range gates, applied only when the unit says which scale it is: a temperature outside
-50..150 °C (or its Fahrenheit equivalent) and a power above 5000 W are not readings.

**A fan at 0 RPM is a measured zero, not an absence.** Both card fans read 0 at idle, which
is fan-stop working. `value: 0` and `value: null` are different claims and both are served.

**Values carry sensor precision, not float precision**: 1 dp for temperatures and power,
3 dp for volts, whole numbers for RPM and MHz. The mapping hands back seventeen digits of a
die temperature the silicon reports to about half a degree.

Strings are ANSI and NUL-terminated, decoded as latin-1: `°C` arrives as one `0xB0` byte,
which utf-8 refuses and utf-8-with-replacement turns into a question mark.

### 2. `host.sensorsSource` — where those readings came from, and how old they are

```jsonc
"sensorsSource": {
  "provider":  "hwinfo",
  "pollTime":  "2026-09-21T23:14:49Z",   // HWiNFO's own poll time, or null
  "ageSec":    1.9,                      // seconds since it, 1 dp, or null
  "stale":     false,                    // ageSec > 30
  "available": true,
  "note":      null                      // a string whenever there is something to say
}
```

**`stale` is derived from the poll time, never from whether the read succeeded, and that
is the whole point of this member.** The free build of HWiNFO stops publishing about twelve
hours after launch and leaves the mapping in place with its last poll time frozen in it. A
reader that only checked for the mapping would serve half-day-old temperatures as current,
forever. The note then reads *"HWiNFO stopped publishing (free build 12-hour limit):
relaunch HWiNFO"*, and `sensors` still carries the readings: they were real when they were
taken, and the panel dims them. Dropping them would turn *old* into *absent*.

Three states, three answers:

| What happened | What is served |
|---|---|
| The mapping is there and its poll time is recent | `available: true`, `stale: false`, `note: null` |
| The mapping is there and its poll time is older than 30 s | `available: true`, `stale: true`, the relaunch note, and the readings |
| No mapping at all | `available: false`, `stale: false`, `pollTime` and `ageSec` null, `sensors: []`, note *"HWiNFO not running, its Sensors window closed, or Shared Memory Support off"* |
| A mapping whose header does not parse | `available: false`, note `"unreadable"` |

The absent note names all three causes because the reader cannot tell them apart:
`OpenFileMappingW` answers `ERROR_FILE_NOT_FOUND` for every one. The Sensors-window cause
is the one an operator is least likely to guess, and it is real: with HWiNFO's main window
up and no Sensors window there is no mapping at all.

A poll time in the future is a clock that moved, not a reading from ahead. `ageSec` clamps
at 0 rather than going negative.

### 3. `host.gpu` — the NVIDIA card, from `nvidia-smi`

```jsonc
"gpu": {
  "name": "NVIDIA GeForce RTX 5070", "driver": "610.74",
  "tempC": 48.0, "utilPct": 4.0,
  "memUsedMB": 4000.0, "memTotalMB": 12227.0,
  "powerW": 12.26, "powerLimitW": 250.0, "clockMHz": 382.0,
  "available": true, "note": null,
  "sampledAt": "2026-09-21T23:19:27Z"
}
```

**The block is served with `available: false` rather than omitted** once the sampler exists.
"This machine has no NVIDIA card" is an answer; a missing key is not, and a reader would
have to tell it apart from an older crabd. Every member is null in that state, and `note`
says which failure it was: *"nvidia-smi not found - no NVIDIA driver on this machine"*,
*"nvidia-smi timed out"*, or *"nvidia-smi returned no readable row"*.

**`sampledAt` is this block's own freshness and it is not optional.** The sampler runs on a
5 s cadence against a 2 s document, so `generatedAt` would date the figure up to two polls
young. Every value that can be stale carries its own clock.

**Nine columns, and the count is exact.** The answer comes back with
`--format=csv,noheader`, so the column order *is* the contract. Fewer than nine means a
column vanished; MORE means one was split, which is what a comma decimal separator does,
and both shift every field after the break. A shifted row is refused rather than served as
a plausible wrong reading. `[N/A]` in one column nulls that field and leaves the other
eight standing.

### 4. `host.load` — what the whole machine is doing

```jsonc
"load": {
  "diskReadBps": 1267326, "diskWriteBps": 410492,   // whole machine, whole numbers
  "netRxBps": 36919, "netTxBps": 125770,
  "commitPct": 45.5,
  "topProcess": { "name": "pwsh.exe", "pid": 8124, "cpuPct": 3.1 },
  "sampledAt": "2026-09-21T23:08:56Z"
}
```

Disk and network come from PDH counters held in one long-lived query
(`\PhysicalDisk(_Total)\Disk Read Bytes/sec` and its write twin,
`\Network Interface(*)\Bytes Received/sec` and its sent twin). Commit comes from
`GlobalMemoryStatusEx`, the same call the v0.22.0 sampler already makes. The busiest
process comes from a Toolhelp snapshot plus `GetProcessTimes` deltas.

**The first sample of every delta is null**, exactly as `cpuPct` has been since v0.22.0.
PDH answers the first collect of a rate counter with `PDH_CSTATUS_INVALID_DATA`, and that
becomes a null rather than a zero. No baseline collect is taken at open, because two
collects back to back produce a rate over a sub-millisecond window and serving it would be
the trap `GetSystemTimes` already documents.

**Every member fails independently.** A PDH query that cannot open does not take the commit
figure with it; a snapshot that cannot be taken does not blank the throughput.

**`topProcess` is null until there are two snapshots**, and it is keyed on
(pid, creation time) because Windows reuses pids: without the creation time a short-lived
process hands its baseline to an unrelated one and produces a fabricated spike. `cpuPct` is
a percentage of the whole machine, so one fully busy core on a 32-thread host is 3.1%.
Processes that refuse `PROCESS_QUERY_LIMITED_INFORMATION` are skipped rather than counted
as zero: a protected process is one this reader cannot measure, and 0% would be a claim
about it. A process that started since the last snapshot has no baseline and is skipped for
one interval.

**`netRxBps` and `netTxBps` are a filtered sum, and the filter is a heuristic.** PDH's
`Network Interface` set lists every pseudo-interface beside the real adapters, including a
virtual switch that carries the *same* bytes as the NIC underneath it. Instances whose
names contain `loopback`, `isatap`, `teredo`, `pseudo`, `tunnel`, `vethernet`, `virtual`,
`miniport`, `filter`, `qos`, `bluetooth`, `vpn` or `tap-` are excluded. On a host with an
unusually named virtual adapter the sum can still double-count, so this figure is a
throughput indicator and not an accounting number. Null when PDH could not answer at all:
an empty sum would be a measured zero.

### Hard rules this keeps

- Unknown is `null` or `available: false`, never `0`.
- Every value that can be stale carries its own freshness (`sensorsSource.ageSec`,
  `gpu.sampledAt`, `load.sampledAt`).
- No sampler runs in the request path or in `build()`. A wedged sampler shows up as a
  growing `ageSec`, not as a feed that stopped.
- Nothing here can raise into `build()`. A malformed mapping is `available: false` with
  `note: "unreadable"`.

### Transport additions in the same release

### 5. `GET /v1/events` — the state document, pushed

A `text/event-stream` that carries the same document `/v1/state` serves, one frame per NEW
snapshot. The widget polled every 3 s, so a question the operator was standing in front of could
sit unlit for that long; this route closes that gap and leaves the poll as the fallback.

**It sits behind the SAME two gates as every other route, in the same order**, and reuses the same
predicates rather than carrying copies of them:

| Request | Answer |
|---|---|
| `Host` that is not `127.0.0.1:<bound port>` or `localhost:<bound port>` | `421 {"error":"host not allowed"}` |
| a present `http(s)` `Origin` that is not one of this socket's own | `403 {"error":"cross-site request refused"}` |
| this socket's own origin | streamed, with that origin reflected in `Access-Control-Allow-Origin` + `Vary: Origin` |
| `null`, absent, and non-web origins | streamed, unchanged |
| a ninth concurrent subscriber | `503 {"error":"too many subscribers"}` |

A long-lived readable stream of `/v1/state` is the widest read surface crabd has (cwds, titles,
the full question text, `pendingPermission`), and an `EventSource` a visited page opened would go
on delivering all of it. The gates run BEFORE the subscriber cap, so a refused request never takes
a slot.

**Headers on a served stream:**

```
Content-Type: text/event-stream
Cache-Control: no-store
X-Accel-Buffering: no
Connection: close
```

No `Content-Length` and no chunking: the body ends when the connection does. `X-Accel-Buffering`
is there because a buffering intermediary is the one failure mode that turns a push transport into
a slower poll with no error anywhere.

**The frames, in order:**

| Frame | When | Body |
|---|---|---|
| `retry: 3000` | on connect | the reconnection time a client that reconnects on its own should use |
| `event: state` | immediately | `data: ` + the full `/v1/state` JSON, ONE line |
| `event: error` | instead of the first state frame, when no snapshot has been built yet | `data: {"error":"state not built yet"}`, and the stream STAYS OPEN |
| `event: state` | every time the builder publishes a NEW snapshot | as above |
| `event: ping` | every 15 s of quiet | `data: {}` |

**"New" is object identity OR `generatedAt`.** `generatedAt` has one-second resolution, so two
builds inside the same second carry the same string and a string-only test would drop the second
one. The same snapshot is never sent twice.

**Cold start is an `error` event and not a close.** `/v1/state` answers `503` and hangs up; the
stream says the same thing and then waits, because the first build is seconds away and closing
would send the client into its backoff for nothing.

**What it does not do.** It never blocks the builder: detection is a 250 ms read of the builder's
`state` property, which takes the builder lock only long enough to copy a reference and never
across a socket write. A client hang-up is detected on the socket rather than on the next write,
so a slot comes back in 250 ms instead of up to a ping; a stop event ends every open stream when
the server shuts down.

### 6. The widget's transport, standalone only (widget)

In the standalone host — and only there — the widget subscribes to `baseUrl() + '/v1/events'` when
`EventSource` exists. Inside the previous host it polls exactly as before: the widget's origin there is `null`,
and an `EventSource` is one more thing to go wrong on a surface with no devtools for a saving of
two and a half seconds.

- Every `state` frame goes through **the same `acceptDoc`** the poll uses — the same schema check,
  the same `generatedAt` check, the same dead-feed latch. A frame that is not JSON is a dead feed,
  exactly as an unparseable poll body is.
- The 3 s poll **skips while the stream is open** and takes over the moment it is not.
- On a transport error the page closes the stream itself, polls at once, and retries the stream on
  **3 / 6 / 12 / 24 / 30 s**, reset by the next `state` frame. The browser's own reconnect is not
  used: it would reconnect on a fixed clock for ever.
- **The stale and dead-feed rendering is unchanged in every mode.** `generatedAt` older than 30 s,
  or a failed poll, is the stale state; a `ping` is liveness only and touches nothing the stale
  logic reads.
- Diagnostic: the page exposes `window.__sidecrabTransport = {mode: 'sse' | 'poll', lastEventAt}`
  and logs one line on each switch.

### 7. The panel host web-message bridge (panel host)

The standalone host is the only surface that can SAVE a setting: the previous host owns its own property sheet
and a widget cannot write it back, and a plain browser at `/panel/` has no file to write. So the
host accepts exactly two messages from the page it loaded, and `IsWebMessageEnabled` is on while
`AreHostObjectsAllowed` stays off — a JSON channel, never a live .NET surface.

**Every message is checked on `Source` first**, against the same navigation lock the window
already uses: a message from any frame that is not `http://127.0.0.1:<port>/panel/…` is logged and
dropped.

| Page sends | Host answers |
|---|---|
| `{"type":"host-info"}` | `{"type":"host-info", "version", "settingsPath", "hasToken": bool}` — the presence of the pairing code, never the code |
| `{"type":"settings", "props": {...}}` | writes the file, then `{"type":"settings-saved", "props": {...}}` carrying what was ACTUALLY stored |

**The props a page may write, and nothing else:**

| Key | Type | Rule |
|---|---|---|
| `clock24`, `alertFlash`, `crabStyle`, `touchDiag`, `chime` | bool | the JSON literals only; a string `"true"` or a `1` is dropped |
| `textColor`, `accentColor`, `backgroundColor` | string | `#RRGGBB` exactly, upper-cased; three digits, a name, an alpha channel or a CSS function is dropped |
| `transparency`, `chimeVolume` | number | rounded and clamped to 0..100 |

Unknown keys are dropped. Wrong types are dropped and never coerced — a coerced setting is a value
nobody chose being written to the operator's file. **If nothing survives, nothing is written.**

`panelToken`, `crabdPort` and `display` are deliberately absent from that list: the pairing code is
the one secret a visited page must never read back or replace, and the port and the display are how
the host finds the companion and the glass, so a page that could move either could point the window
elsewhere or hide it.

The file is merged and written atomically (same-directory temp, then a rename), so `crabdPort`,
`display`, `devtoolsPort` and any prop this sheet does not edit all survive a save. The host's own
write does not trigger its watcher's page reload: the page applied the props live the moment it got
`settings-saved`.

### 8. Audio in the standalone host

`--autoplay-policy=no-user-gesture-required` joins the WebView2 browser arguments. The window never
activates (`WS_EX_NOACTIVATE`) and the alert the chime answers arrives while nobody is touching the
glass, so Chromium's default policy leaves the `AudioContext` suspended and the chime is silent with
no error anywhere.

## v0.31.0 (2026-09-21 — TRANSPORT: the panel route, the Host allowlist, the same-origin allowlist; schema stays 5)

crabd `VERSION` → `0.31.0`, widget `0.29.0`, panel host `0.1.0`. No field is added to or removed
from `/v1/state`. Three transport changes, all so the panel can run OUTSIDE the previous host: the previous host
added a widget URL-permission layer that refuses every widget request to `127.0.0.1` and saves
its grant without the port a loopback grant needs, so the widget cannot reach crabd on that build
(README, "the previous host's later builds").

### 1. `GET /panel/` — crabd serves the widget tree

| Request | Answer |
|---|---|
| `GET /panel` | `301` to `/panel/` (the page's relative `styles/` and `scripts/` links need the directory) |
| `GET /panel/`, `GET /panel/index.html` | `200` `text/html; charset=utf-8`, the shipped `widget/index.html` |
| `GET /panel/styles/sidecrab.css`, `/panel/scripts/sidecrab.js`, `/panel/resources/icon.svg` | `200` with the file's content type |
| anything else under `/panel/` (`mock/`, `translation.json`, `manifest.json`, `..`, encoded dots, absolute paths) | `404` |
| the widget tree missing beside `companion/` | `404 {"error":"panel not available"}`, never a 500 |

Every `/panel/` answer carries `Content-Security-Policy: frame-ancestors 'none'`,
`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and `Cache-Control: no-store`. The
allowlist is a fixed map from request path to file: the request is never joined onto the
filesystem, so a traversal has nothing to climb. `mock/` is deliberately absent, which keeps the
`?mock=` dev switches unreachable from the served origin. The route sits behind the same read gate
as every other GET.

### 2. Host allowlist — every method, before anything else

A request whose `Host` is not exactly `127.0.0.1:<bound port>` or `localhost:<bound port>`
(lower-cased, port included, the port read off the socket crabd actually bound) is answered
`421 {"error":"host not allowed"}` on GET, POST and OPTIONS, with the POST body drained so
keep-alive framing survives. An absent `Host` is refused too. This closes DNS rebinding: a page at
`attacker.example` that resolves its name to `127.0.0.1` is same-origin to itself, so no Origin
check can stop it, but its `Host` names the attacker. Every real client of crabd (curl, the CLI's
http hooks, urllib, PowerShell, every browser) already sends a matching Host; nothing that worked
before this release is refused by it.

### 3. Same-origin allowlist on the origin gate

The v0.16.0 gate refused every present `http(s)` `Origin`. The panel crabd serves sends its own
origin on every POST, so exactly the two origins that name this socket are now allowed and
reflected:

| Request `Origin` | Answer |
|---|---|
| `http://127.0.0.1:<bound port>` or `http://localhost:<bound port>` | handled normally; reflected in `Access-Control-Allow-Origin` + `Vary: Origin` |
| any other `http(s)` origin: another port, `https`, no port, `127.0.0.1.evil.example`, a trailing path, `[::1]`, `0.0.0.0` | `403 {"error":"cross-site request refused"}`, no ACAO, exactly as before |
| `null`, absent, and non-web schemes (`file://`, `qrc://`) | unchanged |

**`decide` still needs the pairing code and the `requestId`.** Same origin is a transport fact,
not a credential: a decide from the panel's own origin with no code is `403 pairing code
required`, with a wrong code `403 pairing code rejected`. In the standalone host the code reaches
the page from `~/.sidecrab/panel-token` through the host's injected
`window.__sidecrabHost.props.panelToken`, which no web page can read. crabd still never serves it.

### 4. The widget in a standalone host (widget 0.29.0)

The widget detects the host from its own address: served over `http(s)` from a `/panel` path means
crabd served it. In that mode `baseUrl()` is `window.location.origin` (same-origin fetches, no
`crabdPort` guess); `getvendorProperty()` reads `window.__sidecrabHost.props`, one object the host
injects before any script runs (never bare globals, so a prop cannot collide with a function name
the way 0.27.0's did), and otherwise returns each reader's default; `uniqueId` is the constant
`standalone`; the Sensors bridge is simply absent, so the temperature row hides itself; and the
property-to-config sync is OFF, so `~/.sidecrab/config.json` is the one master for quiet hours,
toast, digest and budget. Inside the previous host nothing changes.

`/v1/health.originsSeen` (diagnostic, not part of this contract) gains `source: "panel"` for a
request that came from the served panel: an `Origin` equal to crabd's own, or a `Referer` under
`/panel/`.

## v0.30.0 (2026-09-04 — ADDITIVE: `limits.tokenSource`; the long-lived limits token; schema stays 5)

crabd `VERSION` → `0.30.0`. One additive member, one new optional file, no wire change on any
write path.

**`limits.tokenSource`** — `"cli"` | `"sidecrab"`, present only when `limits.available` is true.
Which token answered the usage endpoint: the CLI's own access token from
`~/.claude/.credentials.json` (`cli`), or the long-lived token the operator stored with
`Install-SideCrab.ps1 -LimitsToken` (`sidecrab`). Diagnostic; an older widget ignores it.

**Why.** The CLI access token lives about six hours and is rewritten only when a terminal
`claude` makes an API call — the desktop app keeps its refreshed token elsewhere — so a panel fed
from that file read *"Claude token expired - run /login"* most mornings, and `/login` was not even
the fix (the CLI was still logged in; its file was merely stale). `claude setup-token` mints a
token that lasts about a year.

**Precedence.** CLI token when unexpired, else the stored token, else the (reworded) unavailable
notes: *"Claude token expired - run claude in a terminal to refresh it, or store a long-lived one:
Install-SideCrab.ps1 -LimitsToken"*. A `401`/`403` while the stored token is in use reads
*"SideCrab limits token rejected - mint a new one with claude setup-token and re-run
Install-SideCrab.ps1 -LimitsToken"*, so the two failure modes are never confused.

**The store.** `~/.sidecrab/limits-token.dpapi`: the token, DPAPI-protected for the current
Windows user with no entropy (`[ProtectedData]::Protect(..., CurrentUser)`), decrypted in memory by
crabd with `CryptUnprotectData` on each limits poll and dropped — never logged, never served, never
copied anywhere. Read fresh every poll, so storing it needs no restart.

## v0.29.0 (2026-09-01 — ADDITIVE fields + a TRANSPORT change on `decide`; schema stays 5)

crabd `VERSION` → `0.29.0`, widget `0.27.0`. **Closes SEC-a and WID-a.** Two additive fields, one
diagnostic, and one write path that now REQUIRES two new body members.

**`approvals`** (top level, always present):
```jsonc
"approvals": { "enabled": false, "tokenRequired": true }
```
`enabled` mirrors config `panelApprovals.enabled` (strict-true). `tokenRequired` is `true` on
every crabd from 0.29.0; a document without the block is an older crabd that never asks for one.

**`sessions[].pendingPermission.requestId`** — `string` (16 hex), minted per `register()`. A
replacing request for the same session gets a NEW id.

**`POST /v1/action {"action":"decide"}`** now takes:
```jsonc
{ "sessionId": "...", "action": "decide", "decision": "allow" | "deny",
  "token": "K7QXM-2PDAB",        // the pairing code; case- and hyphen-insensitive
  "requestId": "0f3a9c...7e" }   // pendingPermission.requestId as displayed
```
Answers, in the order the gates run: `400` decision malformed · `503` crabd has no pairing gate
(never falls open) · `429` gate locked (ten rejects inside a minute lock it for a minute; the
right code is locked too) · `403 {"error":"pairing code required"}` / `{"error":"pairing code
rejected"}` · `404` nothing pending · `400 {"error":"requestId required"}` (only when something IS
pending) · `409 {"error":"stale permission request"}` (id is not the pending one; checked under the
broker lock) · `204` applied. **A widget older than 0.27.0 sends neither member and is refused with
403 — its "decide failed … decide in terminal" notice is the honest answer, and the hold passes
through to the terminal dialog exactly as a no-tap does.** That is why this is a transport note and
not a schema bump: nothing an old widget renders changes, and its one write that stops working
stops SAFELY.

**The pairing code.** `~/.sidecrab/panel-token`, 10 symbols of `0123456789ABCDEFGHJKMNPQRSTVWXYZ`
(2^50), written atomically on first start, shown as `XXXXX-XXXXX`. Printed by
`Install-SideCrab.ps1 -PairingCode`; held by the widget as the previous host's property `panelToken`
("Approval Pairing Code"). NEVER served: `/v1/health` gains
`"panelToken": {"present": bool, "rejectedRecently": int, "lockedUntil": ISO | null}`.

**Why this and not the two candidates the SEC-a row listed.** The widget's true origin IS `null`
(originsSeen measured it), so an allowlist cannot separate it from a forged one; and a nonce
served in `/v1/state` is read by the same forged-null caller that would echo it. A secret that
lives in a widget PROPERTY is the one thing a visited page cannot reach.

## v0.28.0 (2026-08-28 — ADDITIVE: the ctx-fill DENOMINATOR; schema stays 5)

crabd `VERSION` → `0.28.0`. One new per-session member. No key moves, nothing is removed,
`schema` stays **5**, and an older widget ignores it as an unknown key — so no widget
import is needed to deploy this crabd.

**`sessions[].contextWindowTokens`** — `int > 0 | null`. The context window the session's
`contextTokens` is filling toward: the DENOMINATOR of the widget's ctx-fill hairline.

```jsonc
"contextTokens": 549300,         // int | null — how FULL the window is
"contextWindowTokens": 1000000   // int | null — how BIG it is
```

**Always present, `null` when unknown** — the same idiom as `contextTokens`,
`queuedContinue` and `pendingPermission`: the KEY is the consumer's feature detection, the
VALUE is the reading. A reader must therefore test the value's TYPE, not the key's
truthiness.

**Why it exists.** The widget derived this denominator only from a `[1m]`/`[200k]` marker
in the model id, and the live ids on this host carry none (measured 2026-08-28:
`claude-fable-5`, `claude-opus-5`), so the bar never drew on a real session — the feature
was shipped and invisible.

**Source priority — MOST SPECIFIC FIRST, and the order is load-bearing:**

| # | Source | Scope |
|---|---|---|
| 1 | the status line document's `context_window.context_window_size` | this SESSION, live |
| 2 | the `[1m]`/`[200k]` marker in the session's own `model` string | this SESSION, stated by the feed |
| 3 | `GET https://api.anthropic.com/v1/models` → that model's `max_input_tokens` | this MODEL, in general |

Rank 1 takes the same freshness contest `contextTokens` takes (CD-36: a status-line row
older than the transcript's own reading loses), because a retained row can name a model the
session has since left. Rank 2 above rank 3 is the case a served `claude-sonnet-4-6[200k]`
makes: the marker is that session's window, the catalog is the model's ceiling, and
preferring the ceiling would gauge the card at a fifth of its true fill — a wrong bar looks
exactly like a right one. `max_input_tokens`, never the sibling `max_tokens`, which is the
OUTPUT cap (measured 2026-08-28: 128000 beside a 1000000 input window).

**Failure = `null`, always.** No credentials file, no token, an expired token, 401, 429, a
timeout, a malformed document, an id absent from the catalog: every one of them serves
`null`, and the widget draws no bar. There is **no model-name table** on either side of
this wire — a built-in "opus means 200k" is a number no document said and would go silently
wrong the day a window changed. A failed catalog fetch KEEPS the last good catalog (a
model's window is a fixed property, not a drifting reading) and throttles the next attempt
by 15 min; a successful one is cached 6 h, in memory only, never to disk.

**Reader rules.** `null` = unknown = **no bar, and no denominator in the tooltip** — never a
zero, never a default window, never last week's number. A crabd below 0.28.0 omits the key
entirely; the widget then parses the marker itself, exactly as it did before, so the two
behaviours are identical wherever a marker exists.

**The token.** The catalog call carries the same OAuth bearer + `anthropic-beta:
oauth-2025-04-20` the usage endpoint takes (verified live 2026-08-28: HTTP 200). The
standing rule is unchanged — the token is read, sent, and dropped; never logged, never
persisted, never in `/v1/state`. `ModelCatalog` serves an integer or an absence and has no
`note` field at all, so no error text from it can reach the widget.

## v0.26.0 (2026-08-28 — BEHAVIOUR + robustness; schema stays 5, no field added or removed)

No shape change: not one key moves, so `schema` stays **5** and no widget import is needed.
crabd `VERSION` → `0.26.0`. This wave is the audit-0424 fixes; the observable ones are:

- **A permission stand-down now writes a ring event.** When a permission hold stands its card
  down, crabd persists a `"permission alert cleared"` entry on `sessions[].events` (and to
  `history.jsonl`), exactly as the in-app clear persists `"answered outside the panel"`. The
  stand-down used to be silent, which left an A-01/A-02-class mis-clear with no trace anywhere.
  `events` stays capped at 8, newest-first, as before.
- **A replaced permission hold no longer stands its card down while the live hold is parked
  (A-01).** Two `PermissionRequest`s for one session — what parallel tool calls in one
  assistant message produce — are newest-wins: B replaces A. A's release must not clear the
  card while B is still parked, or the card reads `working` while serving B's live
  `pendingPermission`. It now stays `needs_input` until B itself resolves. (Dormant while
  panel approvals are disabled; correct the moment they are re-enabled.)
- **An identical-text `Notification` for a live permission dialog ends the alert in either hook
  order (A-02).** The CLI's own `Notification` for a permission dialog is word-for-word
  `PERMISSION_QUESTION`; once it has landed on the row, the hold merely expiring is no longer
  an answer — regardless of which of the two hooks the CLI emitted first (unmeasured). The
  identical text still does not re-escalate (`stateSince`/`acked` unchanged).
- **`cpuPct` is served null for a sub-quantum window or an `idle > kernel+user` glitch (A-07/
  A-08)** — see the CPU failure table above; both belong in the null column, never a `0.0`.
- **`needs_input` retention is bounded (A-05)** — see §9 of v0.21.0; the exemption from
  `GONE_AFTER_SEC` holds, but count and age ceilings now trim runaway growth, oldest-first.
- Internal robustness with no served effect: crabd's own config write is now atomic (a failed
  write can no longer empty `config.json`), a `cwd` on an unreachable network path can no
  longer stall the document build, the git cache is a bounded LRU, an untimestamped usage
  record is skipped rather than dated `now`, and a future round-trip timestamp is clamped to
  crabd's clock before it is written into `stateSince`.

## v0.24.0 (2026-08-28 — a SIDE CHANNEL; schema stays 5; NOT part of this contract)

The **panel diagnostics log channel**: `POST /v1/panel-log` for the widget to say what it
saw, `GET /v1/panel-log` for a maintainer to read it back. crabd `VERSION` → `0.24.0`.

> **⚠ READ THIS FIRST — this endpoint is NOT part of the widget-facing state contract.**
> It is **OPTIONAL** in both directions. `/v1/state` is unchanged, `schema` stays **5**, and
> **a widget must function fully when `/v1/panel-log` 404s** — which is exactly what every
> crabd at 0.23.0 and below does. Nothing the widget renders may depend on this channel, no
> feature may be gated on it, and a failed POST to it must never surface to the operator.
> It is a debugging aid the widget writes to and forgets; if the write fails, the panel
> carries on as though it had never tried.

**Why it exists.** The widget is rendered by the previous host on the Xeneon Edge, a surface no devtools
can attach to — `console.log` has nowhere to go. The question this week: **which input
events does the previous host actually deliver to the widget when the operator touches the glass?** A
**tap** is proven (panel approvals were verified live with the operator on 2026-08-27);
**swipe, long-press and multi-touch are unknown**. The only way to find out is for the
widget to describe what it received, over the same loopback port everything else rides, and
for a human to read it.

### 1. `POST /v1/panel-log` — the widget writes

```jsonc
{"lines": ["pointerdown t=12 id=0 x=140 y=88", "pointerup t=131 dx=2 dy=1"]}
```

| | |
|---|---|
| **`lines`** | an **array of 1..50 strings**. No other key is read |
| **204** | on success, empty body |
| **400** | `lines` absent, not an array, empty, or **any member of the first 50 is not a string**. Body: `{"error":"lines must be an array of 1..50 strings"}`. **Nothing is stored** |
| **403** | a present `http(s)` **Origin** — the same SEC-1 gate every mutating endpoint rides, and it fires **before** anything is stored |

**Two over-limits are NOT errors, and this is the part to build against:**

- **More than 50 lines → the first 50 are kept, 204.** A widget mid-burst must not lose the
  whole batch for over-filling it: losing the tail of one burst is recoverable, losing the
  burst is not. Members past the 50th are **not even type-checked** — they are not stored,
  so their type cannot matter, and 400ing on line 51 would make the cap a rejection after
  all. (It is also what keeps a 5000-line body costing one slice rather than 5000 checks.)
- **A line longer than 300 characters → truncated to 300, 204.** The first 300 characters of
  a diagnostic line are the diagnostic. Lines are **trimmed first, then truncated** — the
  300 is a budget on content, so leading whitespace must not be able to push the useful half
  off the end. A line that trims to empty is stored as an empty line; it is still evidence
  the widget posted.

### 2. `GET /v1/panel-log` — the maintainer reads

```jsonc
{
  "lines": ["2026-08-28T02:11:04Z [panel] pointerdown t=12 id=0 x=140 y=88", "..."],
  "count": 2,
  "droppedTotal": 0
}
```

| | |
|---|---|
| **`lines`** | the ring, **oldest first**, each carrying the server-side prefix below |
| **`count`** | the length of what was **RETURNED** — the same rule `/v1/history`'s `count` follows, so it can never exceed 500 and a reader never has to reconcile it against a shorter list |
| **`droppedTotal`** | lines **evicted by the ring** since this crabd started. Ring evictions only — not lines dropped past the 50-per-post cap, and not truncated characters, both of which the caller already knew about |
| **403** | a present `http(s)` **Origin** — the SEC-4 read gate, like every other GET. These lines describe what is on the operator's panel while they touch it; a visited page has no more business reading them than reading `/v1/state` |

**Reads do not consume.** It is a ring, not a queue — two people reading see the same lines.

### 3. The prefix — the widget never timestamps

Every stored line is prefixed **server-side**, and is verbatim after that:

```
2026-08-28T02:11:04Z [panel] <the line exactly as sent, trimmed and truncated>
```

ISO-8601Z receive time, one space, the short client marker `[panel]`, one space, the line.
The widget's clock is this same machine, so a widget-side timestamp would agree — but a
uniform, server-applied prefix is what makes the **ordering** crabd's, and what makes a
second source safe to add later without renegotiating the format with whoever wrote the
first one. **One timestamp per batch**: the lines arrived in one request, so one receive
time is the honest reading, and it makes intra-batch order the order the widget wrote them
in rather than an artefact of loop speed.

### 4. In memory only, and 500 lines is the whole flood posture

**The ring is 500 lines and it is NOT persisted — deliberately.** Nothing here touches disk
and nothing survives a crabd restart. This is a scratch channel for a live debugging
session, **not history**: persisting free text the widget composes would create a file that
grows, that backups pick up, and that somebody later reads as a record of what happened.
`droppedTotal` exists precisely so a reader can tell they are looking at a tail rather than
assuming the ring is the whole story.

**There is no rate limit, because the ring IS the bound.** The worst legal body — 50 lines
at 300 characters — costs one list extend and one slice under a lock that touches neither IO
nor a build, and the memory ceiling stays fixed at 500 prefixed lines however hard the caller
pushes. Eviction is a single slice-delete rather than a pop per line, so one oversized batch
does not hold the lock 500 times.

### 5. The lines are DATA, never instructions

crabd stores them verbatim, serves them verbatim, and **nothing in the daemon reads a stored
line back into any decision path** — not the state build, not the permission broker, not the
continue queue, not a config write. That is the prompt-injection posture, and it is a
property of the **wiring** rather than of the content: the ring has exactly one reader (the
GET above) and it hands the bytes to a human. Any future caller that parses a line in here
is the change that breaks the property, so it is the change to refuse.

The corollary for whoever reads the output: **these lines are untrusted text**. They arrive
over an unauthenticated loopback port that any process on the machine can reach, and the
`[panel]` marker is a label crabd applied to the transport, not a proof of authorship.

---

## v0.23.0 (2026-08-27 — ADDITIVE; schema stays 5)

The quiet-hours **override**: one operator-tappable gesture on the panel that says "quiet, for
the next while" or "not quiet, whatever the schedule says". Additive in every direction — a new
`action` value, a new config key, a new OPTIONAL member inside the existing `quiet` block — so
`schema` stays **5** and no widget import is needed. crabd `VERSION` → `0.23.0`.

### 1. `POST /v1/action {"action": "quiet"}` — the tap

```jsonc
{"action": "quiet", "mode": "on",   "minutes": 120}   // force quiet until now+120 min
{"action": "quiet", "mode": "off",  "minutes": 60}    // force AWAKE until now+60 min
{"action": "quiet", "mode": "auto"}                   // clear the override, now
```

| | |
|---|---|
| **`mode`** | exactly `"on"`, `"off"` or `"auto"` — a **fixed vocabulary**, nothing else |
| **`minutes`** | an **integer 15..480**, REQUIRED for `on`/`off`. Not a float, not a numeric string, not a bool |
| **`sessionId`** | not required and not read — this is a whole-panel gesture, like `ack-all` |
| **204** | on success, empty body |
| **400** | a mode outside the three, or (for `on`/`off`) a missing/ill-typed/out-of-range `minutes`. Nothing is written |
| **403** | a present `http(s)` **Origin** — the same SEC-1 gate every mutating action rides. A visited web page cannot dim the operator's panel |
| **500** | the config file could not be written |

**`auto` IGNORES a `minutes` it was sent** rather than 400ing on it, and is idempotent: two taps,
two 204s, the same file. Clearing is the gesture the operator reaches for when the panel is doing
something they did not intend, and "your cancel was malformed" is the worst answer to that.

**Why a fixed vocabulary and a bounded duration** — this endpoint writes `config.json` over the
same unauthenticated loopback port every other action rides. The complete set of values that can
reach the file is `on`/`off` plus a minute count in range; `until` is computed from **crabd's own
clock**, never supplied by the caller. An attacker who reaches the port can dim a panel for at
most eight hours. The floor (15 min) is the shortest span worth a tap; the ceiling (8 h) is long
enough for a night or a working day and short enough that a forgotten override always expires on
its own — the SCHEDULE owns every other minute, and an indefinite override would be a second,
invisible schedule nobody remembers setting.

### 2. `quietOverride` in `~/.sidecrab/config.json` — the persistence

```jsonc
"quietOverride": { "mode": "on", "until": "2026-08-27T23:40:00Z" }   // ABSENT = no override
```

**It survives a crabd restart** — the tap is a file write, not process state. Written through the
**same locked read-modify-write** `/v1/config` uses (the v0.16.0 preserve-under-lock lesson), so
every other key in the file is preserved; a writer holding a whole-file rewrite outside that lock
loses whatever landed between its read and its write, and this is an endpoint an operator taps
twice in a row.

**An expired override is treated as ABSENT on read** — there is exactly one reading of it, so no
branch anywhere can honour a stale one — **and is removed from the file lazily, on the next config
write of any kind.** Deliberately no timer: the override dies of the clock, and a timer that must
fire for it to end is a timer that can fail to. Anything that reads as absent is swept, malformed
values included; a **live** override is never swept by an unrelated config write.

**`until` is half-open**: `until <= now` is EXPIRED, matching the quiet window's exclusive `end`,
so the two cannot disagree about a boundary minute. A hand-edited `until` is re-formatted from the
parsed epoch, so the served value is always the one shape above.

**`quietOverride` is NOT in the `/v1/config` whitelist** (still exactly `quietHours`, `toast`,
`digest`, `budget`). It IS panel-writable — just not through that endpoint. `/v1/action`'s quiet
branch is its only writer, which is what bounds the values that can ever reach the file: a
`/v1/config` body naming it is an unknown key → **400, nothing written**, the same posture
`panelApprovals` has for a different reason (SEC-2).

### 3. `quiet.active` is now the EFFECTIVE answer, and `quiet.override` reports why

```jsonc
"quiet": {
  "active": true,                 // EFFECTIVE: schedule with the override applied
  "start": "22:00",               // the SCHEDULE, unchanged — or NULL when none is configured
  "end":   "07:00",
  "override": {                   // OPTIONAL — present only while an UNEXPIRED override exists
    "mode":  "on",
    "until": "2026-08-27T23:40:00Z"
  }
}
```

**The override wins in both directions**: `on` → `active: true` however the schedule reads, `off`
→ `active: false` even inside a live quiet window (the "I am working through the night, stop
dimming the panel" tap — the half that is easy to drop and the half the operator notices).

**Resolved in crabd, once, in `quiet_state`.** Every consumer already reads this one boolean —
the widget's dim and glow, the crab's nightcap, and all four of the notifier's suppression sites
(waiting toast, approval toast, long-run toast, digest/budget/outage) via its `is_quiet(state)`,
which reads `state["quiet"]["active"]` off the feed and computes nothing itself. So the tap
reaches every one of them without any of them learning what an override is.

**An override with NO schedule configured still produces a block**, with `start`/`end` **null**.
"Quiet is on until 21:40, and there is no window" is a fact worth serving, and nulling the whole
block — the way an unconfigured schedule is nulled — would make the tap do visibly nothing on the
install most likely to use it. `quiet` is still `null` when there is neither a schedule nor a live
override, and `override` is **absent**, not null, when there is no override.

## v0.22.0 (2026-08-27 — ADDITIVE; schema stays 5)

One new top-level key, `host`. Additive, so `schema` stays **5**, unknown keys are ignored by
every existing reader, and no widget import is needed. crabd `VERSION` → `0.22.0`.

### 1. `host` — this machine's CPU and memory, beside the previous host's temperature sensors

```jsonc
"host": {                        // OPTIONAL top-level key — PRESENCE is the feature detection
  "cpuPct":     34.2,            // float 0..100, 1 dp, or NULL (see "the first sample" below)
  "memPct":     58.1,            // float 0..100, 1 dp, or null
  "memUsedGB":  18.6,            // float GiB, 1 dp, or null
  "memTotalGB": 32.0             // float GiB, 1 dp, or null
}
```

**Read straight off the Windows kernel, stdlib only** — `GetSystemTimes` and
`GlobalMemoryStatusEx` through `ctypes`. No perfmon counter subscription, no WMI, no
third-party package; crabd remains a single-file stdlib script.

**`GB` means GiB (1024³)**, which is the unit Task Manager shows — so the panel and the OS
agree rather than differing by 7%.

**Sampled on the builder's own pass, not on a thread of its own.** The sample is taken inside
`build()`, which runs every `REFRESH_INTERVAL_SEC` (2 s), so the effective refresh matches the
rest of the document and `generatedAt` dates these numbers as honestly as it dates the others.
An ambient gauge does not need better resolution than that, and a fifth thread would be one
more thing that can wedge while the value it feeds keeps being served.

**THE FIRST SAMPLE HAS NO `cpuPct`, and this is not a bug.** `GetSystemTimes` returns
*cumulative* counters since boot, so utilization exists only *between* two readings. crabd
holds no CPU number until its second builder pass (~2 s after start, and after any counter
glitch that forces a re-baseline). **Null there means "not measured yet" — never 0%.** A
reader must **render an em-dash OR omit the reading entirely — never a 0% gauge**: "the machine
is asleep" is a different claim and it would be a false one. (The em-dash and the omission are
equally conformant. The QtWebEngine widget OMITS the CPU reading while `cpuPct` is null; a
second consumer is free to draw an em-dash instead. The one forbidden rendering is a lit 0%
gauge — CON-a, 2026-08-28 audit.)

**The arithmetic, stated because it is wrong in a believable way if you get it wrong.**
Kernel time *includes* idle time. Over the delta between two readings:

```
busy  = (kernel + user) - idle
total = (kernel + user)
cpuPct = 100 * busy / total     clamped to 0..100, rounded to 1 dp
```

**Two windows are served NULL rather than a number (v0.26.0), because the number would be a
false one:**
- **A sub-quantum window** (`total` below ~100 ms of aggregate core-time). `GetSystemTimes`
  counters advance in coarse scheduler quanta (~31 ms lands at once), so a window that caught
  only a quantum or two cannot express a real busy fraction — idle and kernel moving by the
  same quantum reads as an exact `0.0` on a machine that is not asleep. Reachable only at cold
  start, where the request-thread build and the first `_refresh_loop` build overlap. Served
  null, never `0.0`.
- **`idle > kernel + user`.** Idle time is a subset of kernel time, so this cannot happen with
  a well-behaved counter; a rigged reader or driver bug can produce it, and `busy` then goes
  negative. Served null (the same choice the backwards-counter re-baseline makes), never
  clamped up to `0.0`.

Treating the three counters as disjoint buckets, or omitting the subtraction, yields
percentages that look entirely plausible and are not the truth — on the pinned fixture, 62.5%
and 100% against a real 40%. An un-subtracted implementation reports a **completely idle host
as ~100% busy**.

**Honest failure, in three tiers**, because "cannot read" has three different shapes:

| What happened | What is served |
|---|---|
| Neither counter readable — no `ctypes.windll` (not Windows), or both calls failed | **No `host` key at all.** Presence is the detection, so the panel renders nothing rather than a row of em-dashes that reads like a broken sensor |
| One of the two calls failed | The block is present; **that call's fields are null**, the other's are intact |
| A call returned, but what it returned is unusable — a non-finite number, a negative size, installed memory of zero bytes | Treated as that call having failed: **its fields are null**, never clamped into a plausible-looking `0.0` or `100.0` |

One `stderr` line per failure kind for the lifetime of the process (the `_log_once` rule), then
silence. **No last-good cache exists in the sampler**: a good reading followed by a failed one
serves null, never the previous number re-dated as fresh. Nothing here can raise into `build()`
or produce a non-finite float for `dump_state` to sanitise.

## v0.21.0 (2026-08-27 — BEHAVIOUR; schema stays 5, no field added or removed)

No shape change: not one key moves, so `schema` stays **5** and no widget import is needed. crabd
`VERSION` → `0.21.0`. This release is the crabd lane of a finding-verification wave — nine
confirmed defects fixed, two claims refuted and pinned. Everything below is something a reader can
observe; nothing here is a new field.

### 1. A continuation turn now shows its own `Stop` (CD-06)

**The bug.** A `Stop` arriving on a card already tracked as `done` moved nothing, so `stateSince`
stayed pinned to the session's FIRST `Stop`. `state` is resolved as "done unless the transcript was
written after `stateSince`" — and every write of the continuation turn is after that frozen
timestamp. So the card read **`working` through the second turn, through its `Stop`, and every turn
after**, until it aged out of the window without ever reading `done` again. The tap-to-continue path
is exactly this shape: crabd's own `Stop` answer forces another turn and no `UserPromptSubmit` fires.

**Now:** every `Stop` re-dates `stateSince` (and clears `question` / `acked`), whatever the row last
said. The **done ledger is deliberately not re-armed** by a `done → done` Stop — `recap.doneToday`
and `recap.week` count DISTINCT session ids, so that finish is already counted, and re-arming would
write a second `done` line into history for every repeated `Stop`.

### 2. A restart no longer resurrects finished sessions as `working` (CD-07)

Replaying `~/.sidecrab/history.jsonl` restored the events ring and the tallies but left `state`
unset — and an unset state resolves to **`working`**. So every session that had FINISHED before the
restart came back claiming a live turn, and stayed there until it aged to `idle` 15 minutes later.

**Now:** a replayed row whose newest event is `turn finished` comes back **`done`**, and
`session ended` comes back **`gone`**. Both then age out on their normal schedule. A row whose ring
ends on a later non-terminal event (the session was picked up again) is **not** restored. `asked a
question` is deliberately **never** restored to `needs_input`: history holds no question text, and
`needs_input` is the one state that is never aged away — a restored one would alert forever with
nothing to say.

### 3. `contextSource` — a stale status line no longer beats a newer transcript (CD-36)

Status-line rows are retained for two hours, and until now that retention alone decided precedence:
a reading from 90 minutes ago overrode a transcript figure from 30 seconds ago (reproduced —
`150000` over `30000`). The **precedence is unchanged** (status line wins; it reads the live window
and the transcript figure is arithmetic that disagrees by a whole window after a compaction) — it is
now conditional on the status line's reading not being the OLDER of the two, with a 120 s allowance
for the two clocks. A live status line always wins; one that has stopped feeding loses to the
transcript instead of holding the chip for two hours.

### 4. `recap.doneToday` can no longer exceed `recap.sessionsToday` (CD-11)

The two counts came from sources that never met — `sessionsToday` from the transcript scan,
`doneToday` from the hook ledger — so a session crabd holds no transcript for (older than the
7-day window, or under a projects dir it cannot read) produced `sessionsToday: 0` beside
`doneToday: 1`. `sessionsToday` now unions in every session that finished today and every hook row
that moved today, so `doneToday <= sessionsToday` holds **by construction**.

### 5. `subagentDetail` no longer names an agent that has stopped (CD-29)

`subagents.running` was already correct, but the LIST was the newest `running` transcripts by
mtime — and a subagent that has just stopped has the newest mtime of all of them (its final record
is the last thing written). So the one agent crabd knew had finished was the one named, and a
genuinely running older sibling was dropped. Each recorded `SubagentStop` now retires the file whose
last write is nearest it before the list is trimmed.

### 6. `question` is scoped to the turn that is actually waiting (CD-28)

The transcript's richer question enriched the hook's message when it fell inside a 120 s lookback —
but a whole turn fits inside 120 s, so a question from the PREVIOUS turn could replace the
notification actually on screen. The lookback is now floored at the current turn's start
(`UserPromptSubmit`), with a small allowance for hook latency. Sessions crabd saw no
`UserPromptSubmit` for — every session already running when crabd started — keep the plain window.

### 7. `queuedContinue` — a replacement tapped mid-delivery is kept (CD-30)

The `Stop` handler is peek → send → consume on purpose, so a failed send leaves the prompt intact.
But a prompt queued in that gap (the queue is newest-wins, so the tap is accepted) was then deleted
by the consume while the OLD prompt was the one delivered — neither delivered nor kept, and the card
stopped showing it. The consume now spends only the prompt that was actually sent; a different one
stays queued for the next `Stop`.

### 8. `limits` — a bool or non-finite utilization is absent, not gauged (CD-10)

`utilization: true` rendered a window **100% full** (a bool passes an `int` type test and
`float(True)` is `1.0`), and `NaN` / `Infinity` were silently clamped to an empty or a full gauge.
All four numeric parse boundaries — both `_window` mappers, the scoped-weekly `percent`, and the
status line's `context_window` — now refuse them, so the gauge reads em-dash instead of a
fabricated measurement of the operator's week.

The same class of value in `config.json` was worse than cosmetic: a hand-edited
`toast.thresholdSec: 1e309` is valid JSON that parses to `inf`, and `int(inf)` raised inside every
build — **startup served an empty document and a running crabd froze on its last snapshot**. And
`GET /v1/history` now serves through the same serializer as every other document, so a poisoned
line cannot emit bare `NaN` / `Infinity` at a reader's `JSON.parse`.

### 9. Retention is bounded (CD-09) — internal, no served change

A transcript admitted while it was fresh stayed resident, re-stat'ed and fully re-copied into every
2-second build for as long as crabd ran; a session row left on `working` or `done` (the ordinary end
of a session whose terminal was closed, with no `SessionEnd` hook) was never pruned. Both now leave
at their existing horizons — the transcript window, and `GONE_AFTER_SEC`. **`needs_input` keeps its
contract exemption**: a question waits even when everything else has gone quiet.

**`needs_input` is exempt from `GONE_AFTER_SEC`, not from every bound (v0.26.0).** The old
exemption was total — no count cap, no age ceiling — so a hook flood or a pile of abandoned
questions grew the tracker and the served `sessions` array without limit (every `needs_input`
row is served on every poll). Two generous, oldest-first ceilings now trim runaway growth
while **never** evicting a genuinely recent waiting prompt: a row past a many-hour age ceiling
of no activity is dropped, and past a max live-row count the OLDEST-by-`at` rows go first (a
fresh 2am prompt has the newest `at`, so an abandoned/acked row — which stopped moving `at`
long ago — is the one dropped). Both ceilings sit far beyond any real waiting window, so a
real question is untouched. Internal — no served shape changes.

### Refuted, and pinned by tests so they stay refuted

- **A rapid second question is not cleared while unanswered.** v0.20.0's different-question re-fire
  rule moves `stateSince` past the first question's activity, which is what the clear is compared
  against. Verified against the exact replay, not the rule.
- **One malformed transcript record does not lose the tail behind it.** The read offset does move
  to EOF before parsing — but the whole chunk is already in memory and the per-record catch is
  INSIDE the line loop, so a throwing record costs its own line and nothing else. Pinned because the
  load-bearing detail is that catch's POSITION.

## v0.20.0 (2026-08-27 — BEHAVIOUR; schema stays 5, no field added or removed)

No shape change: not one key moves, so `schema` stays **5** and no widget import is needed. crabd
`VERSION` → `0.20.0`. Three things a reader can observe change.

### 1. `/v1/state` NEVER answers a 500 for a data-shape reason

**The crash this closes (observed once in production, 2026-08-27 ~10:50).** The FIRST
`GET /v1/state` about 2 s after crabd started raised out of the handler and the widget got a 500.
Three later cold starts did not reproduce it, so the fix is not the one line that threw — it is
every seam that could produce it.

MEASURED against 0.19.0 with a repro harness: **ten distinct transcript record shapes crashed the
parser outright** — a non-dict `message` or `usage`, and a usage counter that is a dict, a list, a
word, an `Infinity` or a `NaN`. Any one of them anywhere under `~/.claude/projects` aborted the
transcript scan and therefore the whole build, so **one unreadable line in one session's transcript
took every session's card down with it**. A second seam was proven at the object level: the
per-file usage records were written by the scan and read by the build with no lock between them
(CRB-F2 covered the file *table*, never the state *inside* a file), which two near-simultaneous
cold-start builds can hit.

The guarantee now:

- **A record crabd cannot read is skipped, and the rest of the file is still read.** A counter it
  cannot read is `0`, which under-reports burn by that record — the honest trade against serving
  nothing at all.
- **A file it cannot read costs that file only.** Every other session still gets its card.
- **Nothing is swallowed silently.** Every skip is counted, and the first one prints one line to
  stderr. Once, not per poll — a poisoned transcript would otherwise print every 2 s forever.
- **The served document is always valid JSON.** A value that cannot be expressed is served as its
  string form; a non-finite number is served as `null`, never as the bare `NaN` / `Infinity` tokens
  `json.dumps` emits by default (those are not JSON, and a reader's `JSON.parse` dies on them
  silently).
- **If a build fails anyway**, `/v1/state` serves the **last good snapshot** — stale, with
  `generatedAt` saying how stale, which is the same honest signal a wedged refresh thread already
  produces. With no snapshot ever built it answers **`503 {"error":"state not built yet"}`**, a NEW
  status on this endpoint. Serving `sessions: []` there would claim the operator has no sessions
  running, and that is an answer crabd made up. The widget retries on its next poll either way.

A reader hanging up mid-answer is also no longer a traceback — ordinary transport on a loopback
that drops SYN-ACKs, logged once and dropped.

### 2. A live `PermissionRequest` puts the card on `needs_input`

**The gap:** `needs_input` was set by the `Notification` hook and by nothing else, so a session
sitting on a live permission dialog read **`working`** unless a `Notification` happened to fire
beside it. The panel renders Approve / Deny off the `needs_input` sheet — so the very card carrying
a `pendingPermission` could be the one card not offering it. crabd held the operator's decision
open for 55 s and never told them it was waiting.

The hold now moves the state machine, and **every way it can resolve ends with the card standing
down**: a panel tap, an answer given in the app (v0.19.0's turn clock), and a plain timeout.

Two refusals make it safe rather than careful:

- It raises **only** from `working` or from a session crabd has seen no state-moving hook for. A
  `done` or `gone` row is left alone — a Stop and a PermissionRequest for one session race in the
  wild, and the later of the two must not resurrect a finished card as alerting.
- The stand-down applies **only** to an alert the hold itself raised. A `needs_input` a
  `Notification` raised (or re-raised with a new question) is still a question genuinely waiting,
  and a hold merely expiring is not an answer.

The card's `question` while a hold is open is `"Claude needs your permission to use <tool>"` —
word for word the message the CLI puts on its own `Notification` for the same dialog, so the two
hooks arriving a second apart escalate one prompt once, not twice.

### 3. A NEW question on an already-alerting card re-alerts at full strength

A `Notification` arriving while the row was already `needs_input` used to move nothing: `stateSince`
still dated the FIRST question and `acked` was still set, so the second question of a turn landed
pre-silenced on a card that had already escalated to red. That is the failure v0.19.0 §2 warned a
view-only fix would cause, reachable through the hooks instead.

The test is the question **text**, and that is the healthy-night guard rather than a nicety: Claude
Code re-fires `Notification` for a prompt the operator has walked away from, and resetting on every
one of those would un-ack an acknowledged card all night.

---

## v0.19.0 (2026-08-27 — BEHAVIOUR; schema stays 5, no field added or removed)

No shape change: not one key moves, so `schema` stays **5** and no widget import is needed. What
changes is **when a `needs_input` row stops being one** — and that is a guarantee the widget
depends on, so it is written down here. crabd `VERSION` → `0.19.0`.

### 1. THE GAP — `needs_input` outlived the operator's in-app answer

Reported by the operator: the maintainer answers a waiting session **in the Claude Code desktop app** and the
Xeneon panel keeps alerting — and keeps *escalating* (the widget deepens at 5 min and again at
15 min unacked) — until the turn eventually ends.

`needs_input` is set by the **`Notification`** hook, and Claude Code fires `Notification` for
**both** shapes of waiting: an idle prompt, and a permission dialog (`"Claude needs your permission
to use Bash"` is a real measured message). Before v0.19.0 the ONLY things that moved a session out
of `needs_input` were a later `SessionStart` / `UserPromptSubmit` / `Stop` / `SessionEnd` hook —
and the two commonest in-app answers fire **none** of them:

| How the maintainer answers | What fires at decision time | Cleared before v0.19.0? |
|---|---|---|
| Types a prompt | `UserPromptSubmit` | yes — this path was always correct |
| Clicks Allow/Deny on the terminal permission dialog | **nothing** — the `PermissionRequest` hook already returned its pass-through when the dialog appeared | no, not until `Stop` (an hour of tool work away) |
| Picks an option on an `AskUserQuestion` sheet | **nothing** — the answer is a `tool_result`, not a prompt | no, not until `Stop` |

`_resolve` also never ages a `needs_input` away (by design — a question keeps waiting even when the
transcript is quiet), so there was no second chance either.

### 2. THE CLEARING SIGNAL — a completed model round-trip in the session's own MAIN transcript

crabd now clears `needs_input` when the newest **assistant usage record** in that session's **main**
transcript is newer than the moment the question was raised (plus a 5 s grace). A usage record is a
*completed model round-trip*, which is the one thing that cannot happen while the operator is still
being waited on — the model is blocked. So the guarantee runs in both directions:

- **It can never fire early.** A question that genuinely still stands writes no usage record, ever.
  Silence is not read as an answer; only a *later round-trip* is.
- **It fires for every answer path**, because all of them end in the model being called again: an
  approved tool's result, a denied tool's result, a picked option, a typed prompt.
- **Subagent transcripts are excluded.** A background subagent finishing its own work while the main
  session waits is not an answer, and folding its records in would clear a standing question.
- **It is per-session by construction** — the signal is keyed by the transcript's own session id, so
  there is no path at all by which one session's activity reaches another's row.
- **It is a real transition, not a display overlay**: `question` → null, `acked` → false,
  `stateSince` → the round-trip, `lastEvent` → `"working"`, and an `"answered outside the panel"`
  entry on `events`. That matters because an overlay would leave the tracker on `needs_input`, and
  the **next** `Notification` would then find no state change — so `stateSince` would not move and
  `acked` would not clear, and the second question of a turn would land pre-silenced on a card that
  had already escalated to red. A new question after a clear **re-alerts at full strength**.

Costs nothing new on the wire: crabd already parses these records for `burn` and `contextTokens`.

### 3. A parked `pendingPermission` is retired by the next `Stop` / `UserPromptSubmit` / `SessionEnd`

Follows from the v0.12.0 spike finding (`docs/spikes/live-verify.md` §3.3, SC-LV-2): **the terminal
dialog is not suppressed, it is RACED** — it renders immediately while crabd is still holding the
55 s long poll. So the operator can answer it at t=2 s, the tool runs, the turn finishes — and the
card goes on offering Approve / Deny for another 53 s on a decision already made, where a tap is a
404 at best. Any of those three hooks for that session proves the turn moved past the dialog, so the
hold is released as the ordinary **pass-through**. There is still no route to an `allow` that is not
a tap. `SubagentStop` is deliberately **not** in that set — a background subagent finishing says
nothing about the main thread's dialog.

Also fixed: a raise between `register()` and `release()` used to strand a **panel-visible**
`pendingPermission` forever (nothing but a later request for the same session could clear it — not
the hold, not the expiry sweep). The release is now structural.

### 4. Rejected, and why (recorded so it is not re-litigated)

- **`PreToolUse` / `PostToolUse` as activity pings.** They would close §1 precisely, but they put an
  HTTP round trip in front of **every tool call in every session** — the highest-frequency hook
  surface the product could have, on a host whose loopback drops SYN-ACKs. The transcript already
  carries the same evidence on a path crabd polls anyway. Not wired; not optional-wired either.
- **OTLP activity as a clearing signal.** MEASURED in this repo, not assumed: `setup/*.ps1` sets no
  `OTEL_*` variable, so a default install emits no OTLP at all — a clearing signal that is absent on
  the operator's machine is not a fix. And crabd resolves `session.id` at exactly **one** site
  (`OtlpReceiver.ingest_logs`, `api_error` events); cost metric points are keyed by attribute-set
  string and never mapped to a session, so per-session "token activity" does not exist here. An
  `api_error` is also evidence of a *failing* request — the opposite of the block being released.
- **`SubagentStop` as a clearing signal.** An orchestrator's background subagent finishing while the
  main session waits on a question is an ordinary, frequent event. Clearing on it would silence a
  question nobody answered — the one failure mode worse than the bug being fixed.

---

## v0.18.0 (2026-08-27 — ADDITIVE; schema stays 5)

One new top-level key. Additive, so `schema` stays **5**, unknown keys are ignored by every
existing reader, and no widget import is needed. crabd `VERSION` → `0.18.0`.

### 1. `toast` — the notifier's toast settings, echoed onto the feed

```jsonc
"toast": {
  "thresholdSec": 120,          // int, ALWAYS present
  "enabled": true,              // bool, ALWAYS present
  "approvalThresholdSec": 45    // int, PRESENT ONLY when the on-disk config sets one
}
```

**Why the feed carries config at all.** `/v1/config` is POST-only and the widget cannot
read `~/.sidecrab/config.json`, so before this there was no channel by which the settings
sheet could ever *display* an existing value — it kept a touched-latch and rendered
nothing for a setting the operator had hand-edited. Same reasoning as `continuePrompts`
(v0.12.0): a config-only key rides the feed because the feed is the only read path.

This is an **echo**, not a second write path. `/v1/config` remains the only way to change
these values, with its bounds and its 400s unchanged.

**The two required members always appear.** `thresholdSec` and `enabled` are required
members of the config block, so a missing `toast` block means the notifier is running on
its shipped defaults (`120` / `true`, `notifier/sidecrab_toast.py`
`DEFAULT_THRESHOLD_SEC` and `ToastConfig.enabled`, also documented in `README.md`). That
is a fact worth serving, not an unknown. An unusable hand-edited value (wrong type,
negative) falls back to the same defaults, which is also what the notifier does with it.

**`approvalThresholdSec` is present only when it is set on disk, and NO DEFAULT IS EVER
INVENTED FOR IT.** The asymmetry is the point. The key is optional; v0.16.0's
preserve-on-omit work (§2 of that section below) exists precisely because a round trip
that materialized this key erased the operator's hand edit. A feed answering `20` for an
unset key would hand the widget a value to latch and write back — reintroducing the very
defect from the other end. **Absent here means "not set on disk"**; what the notifier
falls back to is the notifier's business to know, not the feed's to claim. An unusable
value is omitted for the same reason: it is not the operator's value either.

**The served value is what the NOTIFIER will use, not what `/v1/config` would accept.**
The notifier honours any non-negative seconds value, so a hand-edited `thresholdSec: 10`
— below the endpoint's 30 s floor — is served as `10`. The panel must not display `120`
while the box behaves like `10`; clamping for a slider's own range is the reader's job.

The write→read guarantee is the existing one: the **next** `/v1/state` after a successful
`POST /v1/config` reflects it (the once-a-minute config damper is busted by the write).

## v0.17.0 (2026-08-27 — behaviour + a TRANSPORT note; schema stays 5)

No field is added, removed or renamed, so `schema` stays **5** and no widget import is
needed. What changed is what three existing values are allowed to SAY. crabd `VERSION` →
`0.17.0`. (Audit `docs/findings/audit-crabd.md`, items F3, F4, F6, F7, plus two backlog
items.)

### 1. `exhaustAt` is null when the window carries no parseable `resetsAt` (audit F6)

The v0.13.0 rule below — *"never extrapolated past the window's own `resetsAt`"* — was
only enforced when there WAS one. With `resetsAt` absent, null or unparseable the cap was
skipped and the raw projection was served: measured against the pre-fix code, the smallest
utilization step the served 4dp rounding can produce (1e-4 over ~900 s) yielded a date 93
days out on a five-hour window, and a slope an order smaller reached `_utc_iso`'s
year-3000 ceiling and was served as that.

**Now: no parseable `resetsAt` → `exhaustAt: null`.** A cap that cannot be applied does
not become a number crabd made up — the same *unknown is null, never a fabricated value*
rule the rest of this document runs on. Consumers see strictly more nulls and never a new
shape; every window a real source emits carries a reset, so this is an edge, not the
common path.

### 2. A permission tap in the timeout gap now answers the hook it claims to (audit F3)

`POST /v1/action {"action":"decide"}` keeps its answers exactly as documented in §4 below
— 204 when a hold was pending, **404 `{"error":"no permission request pending"}`** once
the 55 s hold has ended. What changed is a sub-millisecond window at the 55 s mark where
the two could disagree: a tap landing after the hold expired but before the entry was
dropped was accepted (204) and written to history as `"approved from panel: …"`, while the
hook had already been answered with the pass-through — so the TERMINAL dialog owned the
call and the panel's record said otherwise.

**Now the two always agree.** A tap that gets in before the hold is closed is honoured:
the hook is answered with that decision and the history line is true. A tap after it is
the documented 404, and the hook's `permission passed through` line stands. **The
never-auto-allow invariant is untouched** — a `behavior: allow` still requires a `decide`
tap and nothing else can produce one.

`permission passed through: <tool>` is also now written even when the session's row aged
out during the hold (audit F7), so *"I did not tap in time"* stays distinguishable from
*"the panel never saw it"* — the distinction that line exists for. History-only; the
served document is unaffected.

### 3. TRANSPORT: an unknown POST path drains its body before answering 404

Framing note, not a contract change: `POST` to a path crabd does not serve is still
**404 `{"error":"not found"}`**, but the request body is now read and discarded first —
matching the 403 cross-origin branch. Left in the socket, those bytes were parsed as the
next request line on a keep-alive connection, so the request AFTER an unknown POST could
be answered as garbage or kill the connection. Any client may now pipeline normally
across a 404.

(Also in this release, no observable effect: the OTLP cumulative-series keyspace is
bounded per day — audit F4 — and a Stop hook whose answer cannot be delivered logs one
line instead of a traceback. Both are memory/console hygiene; the served numbers and the
CRB-F5 queued-continue guarantee are unchanged.)

## v0.16.0 (2026-08-27 — additive + a TRANSPORT change; schema stays 5)

Nothing in the DOCUMENT changed, so `schema` stays **5** and no widget import is needed.
What changed is the transport gate, one config member, and three drifts this document had
accumulated. crabd `VERSION` → `0.16.0`.

### 1. TRANSPORT: the Origin gate now covers the READS too (QA-Audit SEC-4)

Supersedes the "permissive CORS (`Access-Control-Allow-Origin: *`)" line in **Transport**
below, and the "Same CORS as the other GETs / as /v1/action" lines throughout. **crabd no
longer emits `Access-Control-Allow-Origin: *` on any route, method or status code.**

The rule, identical for `GET`, `POST` and `OPTIONS` on every path:

| Request `Origin` | Answer |
|---|---|
| present and `http://…` / `https://…` | **403** `{"error":"cross-site request refused"}`, **no** `Access-Control-Allow-Origin` header |
| `null` (the widget's opaque QtWebEngine origin) | handled normally; `Access-Control-Allow-Origin: null` + `Vary: Origin` |
| absent (curl, the CLI's own http hooks, local tools) | handled normally; **no** ACAO header (a non-browser client needs none) |
| any non-web scheme (`file://`, `qrc://…`) | handled normally; the origin is reflected |

Why the reads and not just the writes: `/v1/state` carries every live session's `cwd`, its
`title`, the FULL text of `question`, and `pendingPermission`. Under `ACAO: *` any page the
operator merely visited could read all of it cross-origin from a background tab. SEC-1
(v0.15.0) closed the write half; this closes the read half with the same predicate.

**The widget is unaffected** — an opaque origin serializes to exactly `null`, which is
allowed and reflected, so its cors-mode `fetch` can still read every reply including the
error statuses it branches on. A widget that ever reports a stable non-`null` origin would
need that value allowlisted; `null` is the only serialization an opaque origin has.

### 2. `/v1/config` — `toast` gains an OPTIONAL third member

`toast` is now `{"thresholdSec": int 30..3600, "enabled": bool,
"approvalThresholdSec": int 5..3600 (optional)}`. The two original members stay REQUIRED;
an out-of-range or non-int `approvalThresholdSec`, or any fourth member, is still 400 with
nothing written.

`approvalThresholdSec` is the notifier's *pending-permission* threshold and needs its own
bounds: its shipped default is 20 s, which is **below** `thresholdSec`'s 30 s floor, so the
two cannot share one. A pending permission is something the operator is already blocked on;
a merely-thinking turn is not.

**A write that OMITS `approvalThresholdSec` PRESERVES whatever is on disk.** This is the
half that matters: the widget's settings sheet does not know the key exists and sends
`{thresholdSec, enabled}`, and because blocks are written whole, every panel save used to
delete a hand-edited value with no message (the notifier then fell back to its 20 s
default). An explicit value in the body always wins — preservation is for silence, never an
override. The corollary is accepted deliberately: the key cannot be *deleted* over HTTP,
only hand-edited out of the file.

### 3. Behaviour: a Stop hook whose answer fails no longer eats the queued continue

`POST /v1/hook/stop` now PEEKS the continue queue, sends the answer, and only then consumes
the item and writes the `continue sent:` history line. Before, it drained first, so a send
that failed (connection reset, CLI gone) destroyed a prompt the operator had tapped and
could see on the card. Observable consequence for anything reading crabd out of band: the
card's `queuedContinue` clears *just after* the hook's response lands, not before it. Since
v0.21.0 that clear is conditional: only the prompt actually delivered is spent, so a replacement
tapped inside that window stays queued for the next `Stop` instead of being deleted undelivered.

### 4. Contract drifts documented (all pre-existing, none new)

- **`continuePrompts` config key.** `~/.sidecrab/config.json` `"continuePrompts": ["ship
  it", …]` — the operator's EXTRA tap-to-continue buttons. File-config only; deliberately
  NOT in the `/v1/config` whitelist. Served at the TOP LEVEL of `/v1/state` as
  `"continuePrompts": [...]` (always present, `[]` when unconfigured) because the widget
  cannot read `config.json`. Also CONSUMED as the queue whitelist: `POST /v1/action
  queue-continue` accepts the builtin prompts plus these, and nothing else. Parsed
  defensively — non-list, non-strings, blanks, over-long entries, duplicates and anything
  already builtin are dropped silently, and the builtins can never be lost to a typo.
- **`contextSource: "transcript"`.** The per-session `contextSource` documented in v0.12.0
  named only `"statusline"`. It has always had a second value — `"transcript"`, the tokens
  derived from the transcript's newest usage record, which is what a session serves before
  (or without) a statusline document. The key is always PRESENT and is `null` exactly when
  `contextTokens` is null: a source label on an absent number would be a claim about
  nothing.
- **The `500` / `503` / `501` / `403` status codes crabd can answer.** Undocumented until now:
  - `POST /v1/config` → **500** `{"error":"could not write config"}` — a body that
    validated but could not be persisted. Distinct from the 400 a bad body gets, and the
    only 500 crabd emits deliberately.
  - `GET /v1/state` → **503** `{"error":"state not built yet"}` (v0.20.0) — no snapshot has
    ever been built and this request's own build failed. The ONLY non-2xx `/v1/state` has
    besides the cross-site 403, and never a 500 for a data-shape reason (v0.20.0 §1).
  - `POST /v1/action` → **501** for a feature this crabd does not carry:
    `{"error":"reply not supported"}` (injection unproven — see v2 additions),
    `{"error":"continue not supported"}` (no continue queue wired),
    `{"error":"panel approvals not supported"}` (no permission broker wired).
  - `POST /v1/action queue-continue` → **403** `{"error":"tap-to-continue is disabled"}`
    when config `allowContinue` is `false`. 403 rather than 501 on purpose: the feature is
    implemented and refused by configuration, not missing.
  - Any request carrying an http(s) `Origin` → **403** `{"error":"cross-site request
    refused"}` (§1 above).
  The widget renders every non-2xx from `/v1/action` as "not available" without latching,
  so the distinction is for operators and logs, not for widget branching.

## v0.14.0 additions (2026-08-26 — additive, schema stays 5)

**`sessions[].queuedContinue`** — `{"prompt": "...", "queuedAt": "ISO"} | null`. Always present
(the key itself is the widget's feature detection), null when nothing is queued. Freshness is
re-derived from `queuedAt` rather than trusting the expiry sweep, so a card never advertises a
prompt the Stop hook would no longer deliver.

**`GET /v1/health` counters** (health is not part of the state contract, documented here for
operators): `{"ok", "version", "uptimeSec", "hooksSeen", "statuslineSeen",
"lastStatuslineAgeSec": N|null, "otlpSeen", "originsSeen"}`. `lastStatuslineAgeSec` distinguishes
*never posted* (null — misconfigured) from *posted and went quiet* (a number — idle operator);
zero for both would make the two indistinguishable, which is the failure this counter exists to
catch.

**`originsSeen` (v0.25.0, ORIGIN-REC; v0.27.0 adds `source`/`userAgent`) — DIAGNOSTIC, and
explicitly NOT part of the widget-facing contract.** An array of
`{"origin": "...", "source": "browser"|"local"|"none", "userAgent": "..."|null, "count": N,
"lastSeenAt": "ISO"}` recording the distinct **(origin, source)** pairs seen on the request paths (a
raw absent header is folded to the literal `"<absent>"`; the set is LRU-capped at 48 so a flood of
forged origins/UAs cannot balloon it). It is the passive enabler for the SEC-a fix: the legitimate
QtWebEngine widget and a forged-`null` attacker are indistinguishable to the origin gate, so the
widget's TRUE origin has to be MEASURED before it can be allowlisted — and this lets it be read
remotely from the widget's own live polling instead of at the glass.

**Why `source` (v0.27.0).** Origin-only keying collapsed every no-Origin caller into one
uninformative `"<absent>"` bucket — the notifier polling `/v1/state`, a maintainer's curl health
checks, and possibly the widget all landed there together (measured live 2026-08-28: `originsSeen`
was only `{"origin":"<absent>"}`). `source` is a coarse bucket derived from the request's
`User-Agent` (`"browser"` when it contains `Mozilla`/`Chrome`/`QtWebEngine`/`AppleWebKit` — the
widget is QtWebEngine/Chromium; `"local"` for any other non-empty UA like python-urllib or curl;
`"none"` when there is no UA at all), and `userAgent` is that raw UA truncated to ~80 chars. Keying
on the (origin, source) pair means `null`-from-a-browser and `<absent>`-from-a-local-process are
separate rows, which is what isolates the widget. **⚠ The `User-Agent` is attacker-controlled and
this classification is DIAGNOSTIC ONLY** — it never feeds `_is_web_origin` or any decision path; the
CSRF gate stays exactly as it is (origin-based). A future SEC-a fix keying the gate on *absent vs
null* (the clean discriminator if the widget proves to send absent, not null) is a SEPARATE,
deliberate change to the gate — this recorder only MEASURES.

It lives ONLY here in `/v1/health`; it is **never** in `/v1/state`, never a `build()` input, and
never read back into any decision path.

## v0.13.0 additions (2026-08-26 — additive, schema stays 5)

**Depletion forecast.** `limits.fiveHour` / `limits.weekly` each gain an optional
`"exhaustAt": "ISO" | null` — a linear projection of when the window hits 100% at the recent
burn rate, computed by crabd from the window's utilization delta over the last ~15 min of served
readings (needs ≥2 readings spaced ≥60 s; null when flat/declining/insufficient data). Never
extrapolated past the window's own `resetsAt` (a window resets before it depletes → null) —
**and since v0.17.0 (§1 above) a window with no parseable `resetsAt` is null too**, because a
cap that cannot be applied must not become a served number. The
widget renders a muted "~full by 3:40 PM" line under the gauge when present and sooner than reset;
absent/null → nothing. Pure projection, clearly hedged ("~"), never presented as certainty.

## v0.12.0 additions (2026-08-26 — additive, schema stays 5) — "the control-surface wave"

**1. statusLine ingest (retires the OAuth reach-around).** A chained statusline command
(`hooks/sidecrab_statusline.py`, wired by the installer WITH settings backup, chaining any
pre-existing statusline) POSTs the official stdin session document to `POST /v1/statusline`
(204, fire-and-forget). crabd prefers this source for limits + per-session context:
`limits.source: "statusline" | "oauth"` (new field; widget shows a muted provenance label) and
statusline-fed `contextTokens` carries `contextSource: "statusline"`. OAuth remains the fallback
when no statusline document has arrived in 10 min. Per-session context has a second condition
since v0.21.0: the status line's reading must not be the OLDER of the two (120 s clock allowance),
or the transcript figure wins — retention alone used to decide it, for up to two hours.

**2. OTLP receiver.** `POST /v1/metrics` + `POST /v1/logs` accept OTLP http/json from Claude
Code's built-in telemetry (installer sets the env vars in the hook-carrying settings? NO — env
config documented in README, user-level opt-in). crabd aggregates: `burn.costUSD` (today, from
claude_code.cost.usage when telemetry flows; null otherwise — never derived), API error events
into sessions[].events. Malformed OTLP → 204 and dropped (a telemetry write must never error the
producer). Provenance: burn.costSource: "otlp" | null.

**3. Tap-to-continue (Tier 1).** `POST /v1/action` gains `{"sessionId","action":"queue-continue",
"prompt": "<one of the configured set>"}` → 204, one queued item per session (newest wins),
expires 10 min. The Stop hook becomes type-http pointing at `POST /v1/hook/stop` — crabd answers
within 2 s. **SHAPE REVISED after binary deep-read (v0.15.0):** `decision:"block"` DOES force another turn,
but routes through the CLI's *error* channel — the operator sees "Stop hook error occurred" and
the model receives the nudge labelled a blocking error (measured; it visibly hedges). The
sanctioned non-error channel is `{"hookSpecificOutput": {"hookEventName": "Stop",
"additionalContext": <prompt>}}` — the binary's own schema text: "non-error feedback delivered
to the model; the conversation continues so the model can act on it", and BOTH branches push
into the same continuation array, so the forced turn is guaranteed on the same code path. crabd
emits additionalContext; `decision:block` is retained as an executable fallback constant.
`continuationPrompt` does not exist (measured 0). `{}` = proceed/stop normally. Widget: done/working cards'
sheets gain Continue / Run the tests / Commit + push buttons + extras from config
`continuePrompts: ["..."]`.

**4. FULL panel approval (the maintainer's explicit choice).** PermissionRequest hook → type-http
`POST /v1/hook/permission` (long-poll): crabd registers the pending request
(sessions[].pendingPermission: {"tool","summary","requestedAt"} — additive), holds the response
up to 55 s awaiting `POST /v1/action {"sessionId","action":"decide","decision":"allow"|"deny"}`
from the widget (Approve/Deny buttons on the needs_input sheet). **VERIFIED v2.1.246 by reading the shipped zod schema (supersedes an earlier docs-based
mis-reading):** the response is `{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
"decision": {"behavior": "allow"} | {"behavior": "deny", "message": ...}}}` — NOT the PreToolUse-
style `permissionDecision` string, and there is **no `"ask"` value**. The pass-through (timeout /
no-tap / disabled / malformed) is to return **no `hookSpecificOutput` at all** (`{}`), which lets
the TERMINAL DIALOG appear as today. crabd has NO branch that yields `behavior:allow` without a
`/v1/action decide` tap having landed first. PermissionRequest
http hook timeout is set to 60 s (past crabd's 55 s poll). Note `allowedHttpHookUrls`, if the
operator has set it, must include `http://127.0.0.1:2722/*` or the http hooks are blocked. NEVER auto-allow. Config `panelApprovals: {"enabled": false}`
default OFF; the installer asks/flips per the operator's choice. Every decision logged to
history ("approved from panel: Bash", "denied from panel: ...").



**Title fallback + provenance.** The per-session `title` chain gains a final tier: the session's
cwd tail (last path component; last two joined with "/" when the last is generic — main, src,
app, repo, work, dev, tmp). New optional per-session `"titleSource"`:
`"custom" | "ai" | "prompt" | "cwd" | null` — presence-detected; the widget renders
`"cwd"`-derived titles muted-italic. Sessions with no title at all render the repo name, else
"untitled session" — never a bare "session" literal.

## v0.10.0 additions (2026-08-26 — additive, schema stays 5)

**Burn budget.** `/v1/config` fourth key: `budget` — `{"dailyOutputTokens": int 100000..100000000}`
or `null` to clear (strict; else 400 nothing written). When configured, `burn` gains:
```jsonc
"budget": { "dailyOutputTokens": 5000000, "todayPct": 0.34 }   // pct capped at 9.99
```
Absent config → no `budget` key. Consumed by: the widget (a target marker on the 24h sparkline
and a muted "budget 34%" line near TODAY; ≥100% amber, ≥150% red — text carries the state) and
the notifier (ONE toast per day on first crossing 100%, "Daily token budget crossed", deduped in
its ledger like the digest; quiet-suppressed-and-marked).

## v0.9.0 REMOVAL (2026-08-26 — publication)

**The `estate` block is REMOVED.** A private-dashboard integration existed before publication and
was removed for release: crabd no longer emits the key or carries its reader, and the widget no
longer renders the strip or offers the `estateStrip` setting. Both sides presence-gated it already,
so removal is deploy-order-free. Historical `estate` sections below are collapsed to this note.

Two rules the removal leaves behind, both still live:

- Every shipped string and fixture is generic — SideCrab reads nothing but local Claude Code state.
- **The widget must degrade to a useful standalone product without crabd** (clock + crab + the previous host
  sensors), because a store user installs the widget first and may never install the companion.

## v0.8.0 additions (2026-08-26 — additive, schema stays 5)

**`GET /v1/history?day=YYYY-MM-DD`** — read-only view over the persisted history (current file +
the one .old generation): `{"day": "...", "events": [{"ts": "ISO", "kind": "...", "sessionId":
"...", "title": "..."}], "count": N, "truncated": false}` — events of that LOCAL day, newest
first, cap 200 with `truncated: true` beyond. `day` is strictly validated (^\d{4}-\d{2}-\d{2}$
and a real date; else 400). Unknown/empty day → empty events, 200 (absence of history is not an
error). Same CORS as the other GETs.

**`/v1/config` third key: `digest`** — `{"enabled": bool, "time": "HH:MM"}`, both members, strict
(else 400 nothing written). Consumed by the notifier: when enabled, ONE toast at the configured
local time daily — "Yesterday: N done · M commits" from crabd's recap.week — deduped per calendar
day, quiet-hours-suppressed-and-skipped (not deferred), silent when crabd is unreachable.

**Widget-only in v0.8.0:** session PINNING — the detail sheet gains Pin/Unpin; pinned sessions
sort first within their state band (needs_input still outranks everything), marked with a small
pin glyph, persisted via the previous host's local-storage mechanism the vendor docs describe (falling back
to in-memory when unavailable — a lost pin is a nuisance, not an error); tapping a DAY in the
timeline-footer week strip opens that day's history via GET /v1/history (absent endpoint on an
older crabd → the day tap is inert, attempt-and-handle, no latch).

## v0.7.0 additions (2026-08-26 — additive, schema stays 5, detected by presence)

`recap.week` — the last 7 local days (oldest first), from PERSISTED history (see below) + git:
```jsonc
"week": [ { "day": "2026-08-20", "done": 3, "commits": 14 } ]   // commits = sum across recap-scope repos
```

**History persistence:** hook-derived facts (events, done transitions) now append to
`~/.sidecrab/history.jsonl` (one JSON object per line; no secrets, no question text — event kind +
session id + title-at-time + ts only), replayed at startup so doneToday/events/week survive crabd
restarts. Size-capped by rotation (~2 MB, one .old generation). The doneToday "floor" caveat is
retired for restarts after this ships; pre-persistence history remains unknowable and is never
fabricated. **A replayed row also gets its TERMINAL state back (v0.21.0)** — `turn finished` →
`done`, `session ended` → `gone` — because an unset state resolves to `working`, so the old
"restore nothing" rule had every finished session claiming a live turn after a restart. A running
state is still never restored, and `asked a question` is never restored to `needs_input`.

`POST /v1/config` whitelist grows to TWO keys: `quietHours` (as before) and
`toast` — `{"thresholdSec": int 30..3600, "enabled": bool}`, both members required, strictly
validated (else 400, nothing written). An older crabd 400s a toast write — the widget's per-key
handling must treat that as this-key-unsupported (no latch; 404 latching semantics unchanged).

**Toast action (notifier + registration):** toasts gain an "Acknowledge" button using protocol
activation — `sidecrab-ack:<sessionId>` — handled by a registered HKCU protocol
(`sidecrab-ack`) whose handler POSTs `{"sessionId": ..., "action": "ack"}` to /v1/action and
exits silently. Registration script lives in setup/ (HKCU only, idempotent, -Remove), wired into
Install/Uninstall like the AUMID. The sessionId in the URI is validated by the handler against
a conservative charset (^[A-Za-z0-9-]{1,64}$) before any HTTP call — a toast payload is data.

# Previous header (fields still accurate): schema 6 additions

> **Schema 6 (v0.6.0, 2026-08-26) is ADDITIVE over schema 5.** SUPERSEDED numbering — these
> fields now ship under `"schema": 5` per the rework above.

## v6 additions

Per session — how full the context window was on the LAST request (the input side of the
newest assistant usage record: input_tokens + cache_read_input_tokens +
cache_creation_input_tokens):
```jsonc
"contextTokens": 549300        // int | null when no usage record exists yet
```
Its denominator — how BIG that window is — arrived later, as `contextWindowTokens`; see the
v0.28.0 section at the top.

New top-level `fleet` — SideCrab observing its own components (Scheduled Task states via
schtasks query, cached ~60 s; "running" | "stopped" | "absent" | "unknown"):
```jsonc
"fleet": { "glow": "running", "toast": "running" }
```
crabd itself is omitted — if you can read this document, crabd is running. A component whose
task exists but whose last-known state can't be read is "unknown", never guessed.

Widget-only in v0.6.0: Claw'deck badge scaled up ~25%; ctx chip on session cards ("ctx 549k",
muted, next to the model chip; absent when null); fleet dots — two small labeled dots (g/t)
under the clock: green running, amber stopped, gray absent/unknown, with the not-color-alone
rule carried by the letter + a dot shape change; idle-card Dismiss (same semantics as done);
fix the small-slot (max-height 420px) question clip (clamp at 2 lines, never mid-glyph);
add a ?mock= fixture exercising the >=95% red gauge step and the timeline "+N earlier" row.



> **Schema 5 (v0.5.0, 2026-08-26) is ADDITIVE over schema 4.** crabd emits `"schema": 5`;
> the widget accepts 1–5. Anything else is a dead feed.

## v5 additions

`burn` gains a today-by-model split (from the same deduped usage records; cap 4 desc):
```jsonc
"byModel": [ { "model": "claude-fable-5", "outputTokens": 429000 } ]
```

Widget-only in v0.5.0 (no contract impact): the **Claw'deck rebrand** — the Claude Max lockup
is replaced by the Claw'deck badge (dark rounded plate #1A150F, orange border, mini pixel crab
wearing pixel sunglasses, monospace "Claw'deck" wordmark ~#F7F3EC); the main crab's fill warms
from #D97757 to the logo orange family (~#E8541F, worried-desaturation retuned to match);
tapping the RECAP header opens a today-timeline sheet built from sessions[].events merged
newest-first across sessions; the burn sheet shows the byModel split when present.



> **Schema 4 (v0.4.0, 2026-08-26) is ADDITIVE over schema 3.** crabd emits `"schema": 4`;
> the widget accepts 1–4. Anything else is a dead feed.

## v4 additions

New top-level `recap` (cached ~5 min; local read-only `git log`, never a network call):
```jsonc
"recap": {
  "sessionsToday": 9,          // sessions with any activity since local midnight — transcripts
                               // UNIONED with today's hook rows and finishes (v0.21.0), so
  "doneToday": 3,              // doneToday <= sessionsToday holds by construction
  "commits": [                 // commits since local midnight per distinct repo seen among
    { "repo": "sidecrab", "count": 12 }   // today's session cwds; cap 4, count desc
  ],
  "computedAt": "ISO"
}
```
- `recap` is `null` until the first computation completes (a few seconds after crabd starts) —
  never a zeroed document. The widget renders null as the v3 header.
- `doneToday` is a FLOOR: only Stop transitions observed by the running crabd count;
  pre-restart finishes are never reconstructed or inflated.
- Repo scope (amended 2026-08-26): session-cwd repos PLUS any paths listed in
  `~/.sidecrab/config.json` `"recapRepos": ["C:\\Dev\\sidecrab", ...]` — a session whose cwd is
  elsewhere but which DRIVES a repo would otherwise hide that repo's commits entirely. recapRepos
  is file-config only, NOT settable via /v1/config (whitelist unchanged: quietHours only).

`POST /v1/action` gains `{"action": "ack-all"}` → 204: acks EVERY unacked needs_input session
(each records an "acknowledged from Edge" event). 204 even when nothing was waiting.

New endpoint `POST /v1/config` — body `{"quietHours": {"start": "HH:MM", "end": "HH:MM"}}` or
`{"quietHours": null}` → 204: validates HH:MM strictly (else 400, nothing written), rewrites
`~/.sidecrab/config.json` PRESERVING all other keys. quietHours is the ONLY key writable over
HTTP — `allowReply` and anything else must never be settable remotely. Same CORS as /v1/action.

**limits.note semantics widened (v0.4.0):** `note` may now be non-null while `available:true` —
a caveat, not an error (e.g. "limits as of 2:41 PM" when serving a last-good reading older than
15 min through an endpoint lockout). The widget renders any non-null note, muted, regardless of
`available`; gauges stay lit on the last-good values.

Widget-only in v0.4.0 (no contract impact): tapping the CRAB = ack-all (no-op when nothing
waits); celebrating mood (both arms up ~10 s) when a session completes a turn that ran >30 min;
rare idle blink (reduced-motion-safe); done-card Dismiss (local hide until state changes);
tapping the LIMITS zone header → burn-by-session sheet from sessions[].todayOutputTokens;
quiet-hours the previous host's properties that POST /v1/config on change.



> **Schema 3 (v0.3.0, 2026-08-26) is ADDITIVE over schema 2.** crabd emits `"schema": 3`;
> the widget accepts 1, 2 **or** 3. Anything else is a dead feed.

## v3 additions

*(The top-level `estate` block that shipped in v0.3.0 was the private-dashboard integration.
**Removed for publication in v0.9.0** — see the note at the top of this document. Nothing consumes
or emits it.)*

`burn` gains a 7-day series:
```jsonc
"daily": [ { "dayStart": "2026-08-20", "outputTokens": 0 } ]   // 7 entries, oldest first, local days
```

Per session:
```jsonc
"events": [ { "at": "ISO", "text": "asked a question" } ]   // cap 8, newest first: state
// transitions + hook events for THIS session since crabd started (ring buffer; empty ok)
```

Widget-only in v0.3.0 (no contract impact): escalation tiers from unacked needs_input
`stateSince` age; 24h↔7d sparkline toggle; hardware sensors row via the previous host's sensor
data-provider plugin (hidden entirely when the previous host's API is absent, e.g. dev browser).



> **Schema 2 (v0.2.0, 2026-08-26) is ADDITIVE over schema 1.** crabd emits `"schema": 2`;
> the widget accepts 1 **or** 2 (an absent v2 field renders as its v1 behavior). Anything
> other than 1 or 2 is a dead feed.

## v2 additions

Per session (all optional; `null`/absent = v1 behavior):
```jsonc
{
  "question": "string|null",       // FULL text the session is waiting on (needs_input only):
                                   // the Notification hook's message, enriched from the transcript
                                   // tail when the transcript carries a longer question
  "turnStartedAt": "ISO|null",     // set on UserPromptSubmit, cleared on Stop -> widget shows "working 14m"
  "acked": false,                  // true after POST /v1/action {action:"ack"} - widget keeps the
                                   // needs_input card visible but DROPS the panel glow/pulse for it;
                                   // cleared automatically on the session's next state transition
  "subagentDetail": [              // cap 5, running only, newest first — agents retired by a
                                   // SubagentStop are excluded, not merely trimmed away (v0.21.0)
    { "label": "string", "ageSec": 0 }
  ]
}
```

Top level:
```jsonc
"quiet": { "active": false, "start": "22:00", "end": "07:00" }   // or null when unconfigured.
// active=true -> widget dims to ambient, crab asleep, NO flash/glow/pulse; needs_input cards
// still render statically (a question keeps waiting). Config lives in ~/.sidecrab/config.json
// {"quietHours": {"start":"22:00","end":"07:00"}} - crabd computes `active`, widget only renders.
// SINCE v0.23.0 `active` is the EFFECTIVE answer (schedule + the operator's override) and the
// block carries an optional `override`; start/end may be null. See the v0.23.0 section.
```

New endpoint — touch actions:
- `POST /v1/action` body `{"sessionId": "...", "action": "ack"}` → 204. Unknown session → 404.
- `{"action": "reply", "text": "..."}` → 204 only when reply-injection is PROVEN and enabled
  (config flag `allowReply`, default false); otherwise **501** with `{"error": "reply not supported"}`.
  The widget must render the 501 path gracefully (sheet shows "not available yet").
- Same CORS as the GETs. Text is one of the widget's canned strings only — free-form input is v3.



**This document is the contract between `companion/` (crabd, the producer) and `widget/` (the consumer).
Neither side may change it unilaterally — a change lands here first, bumps `schema`, and updates both sides in one commit.**

## Transport
- crabd binds `127.0.0.1:2722` (2722 = C-R-A-B on a phone keypad), HTTP, no auth (localhost-only, read-only data).
- `GET /v1/state` → the full document below, `Content-Type: application/json`. **CORS: see the v0.16.0 §1 table above — this line's original "permissive CORS (`Access-Control-Allow-Origin: *`)" is SUPERSEDED and no longer true.** The widget still renders from the previous host QtWebEngine origin; that origin is `null`, which is allowed and reflected.
- `GET /v1/health` → **this two-field shape is SUPERSEDED (CON-c, 2026-08-28).** The daemon serves the full 8-field diagnostic set documented under "v0.14.0 additions" above (`ok`, `version`, `uptimeSec`, `hooksSeen`, `statuslineSeen`, `lastStatuslineAgeSec`, `otlpSeen`, `originsSeen`). `ok`/`version` are unchanged, so any reader of the original two keys still works; the rest are additive and diagnostic. **Health is NOT part of the state contract** — the widget does not consume it, and nothing here bumps `schema`.
- `POST /v1/hook` → body is the raw Claude Code hook JSON from stdin (fields include `session_id`, `hook_event_name`, `cwd`, ...). Responds 204. Fire-and-forget; hooks must never block Claude Code (client timeout ≤2 s).
- The widget polls `/v1/state` every 3 s. It renders the **stale/dead-feed state** (worried crab, dimmed panel, "data as of HH:MM" banner) whenever a poll fails OR `generatedAt` is older than 30 s. Silence must never render as all-green.

## Document

```jsonc
{
  "schema": 1,
  "generatedAt": "2026-08-26T18:05:00Z",        // UTC ISO-8601; widget compares against Date.now()
  "crabd": { "version": "0.1.0", "startedAt": "ISO", "hooksSeen": 0 },

  "limits": {                                    // from the Claude OAuth usage endpoint
    "available": true,                           // false => widget shows em-dash gauges + note; NEVER zeros
    "note": null,                                // human string when available=false ("token expired — open Claude Code")
    "fiveHour":  { "utilization": 0.42, "resetsAt": "ISO" },   // utilization 0..1; NULL when available=false (CON-c) — not an always-object
    "weekly":    { "utilization": 0.18, "resetsAt": "ISO" },   // NULL when available=false (CON-c); an object otherwise (may gain exhaustAt, v0.13.0)
    "extra": [],                                 // any additional windows the endpoint reports: {label, utilization, resetsAt}
    "subscriptionType": "max",
    "rateLimitTier": "string-as-reported"
  },

  "burn": {                                      // aggregated from ~/.claude transcript JSONL (assistant.message.usage)
    "today": { "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheCreationTokens": 0, "messages": 0 },
    "hourly": [ { "hourStart": "ISO-local-hour", "outputTokens": 0 } ]   // last 24 buckets, oldest first
  },

  "sessions": [
    {
      "id": "uuid",
      "title": "string",                         // customTitle > aiTitle > first-prompt excerpt
      "cwd": "C:\\Dev\\sidecrab",
      "repo": "sidecrab",                        // null when cwd is not a git repo
      "branch": "master",                        // null likewise
      "state": "working" | "needs_input" | "done" | "idle" | "gone",
      "stateSince": "ISO",
      "lastActivityAt": "ISO",                   // max(hook event, transcript mtime)
      "lastEvent": "asked a question" ,          // short human line; for needs_input include what for
      "model": "claude-fable-5",                 // from last assistant message, SERVED VERBATIM (CON-b) — see note below
      // CON-b (2026-08-28): the `model` string is a CHANNEL, not just a label. Claude Code may append a
      //   context-window marker — e.g. "claude-fable-5[1m]" or "…[200k]" — and BOTH sides parse that
      //   marker as a ctx-fill denominator (crabd since 0.28.0, ranked above its model catalog; the
      //   widget as its fallback for a crabd below 0.28.0).
      //   INVARIANT: crabd serves `model` exactly as the transcript wrote it — it does NOT normalize,
      //   trim, or strip the marker. Two consumers depend on the marker being present; stripping it to
      //   "tidy" the label would silently break the ctx-fill denominator. crabd's own catalog lookup
      //   strips the marker to build a LOOKUP KEY and never writes that back into this field.
      //   CORRECTED 2026-08-28: an absent marker does NOT fall back to "the widget's default window
      //   size" — there has never been one, and inventing one is the thing both sides refuse. It falls
      //   through to `contextWindowTokens`, and to NO BAR when that is null.
      "speed": "standard" | "fast",
      "subagents": { "running": 0, "total": 0 },
      "todayOutputTokens": 0,
      "contextTokens": 549300,          // v0.6.0 — how full (int | null)
      "contextWindowTokens": 1000000    // v0.28.0 — how big  (int | null); see the section at the top
    }
  ],
  // sessions array is pre-sorted by crabd: needs_input, then working, then done, then idle. "gone" excluded.
  // done sessions are dropped ~10 min after stateSince unless reactivated. idle = no activity > 15 min, process may still live.
  // (A top-level "estate" block shipped here through v0.8.0 and was REMOVED in v0.9.0 — see the
  //  note at the top. It is not emitted, not read, and not reserved.)
}
```

## Session state machine (crabd owns it)
| Hook event | Transition |
|---|---|
| `SessionStart` | → `working` (new row) |
| `UserPromptSubmit` | → `working` |
| `Notification` | → `needs_input` (lastEvent from notification message). A **re-fire** carrying a **different** question is a new alert: `stateSince` moves and `acked` clears. The **same** question re-fired changes nothing (v0.20.0) |
| `Stop` | → `done`. **Always re-dates `stateSince`**, including a `done` → `done` Stop — that is how a continuation turn (tap-to-continue fires no `UserPromptSubmit`) shows its own finish. The done LEDGER is armed only by a real change INTO `done`, so a repeated Stop cannot write a second `done` line (v0.21.0) |
| `SubagentStop` | decrement `subagents.running`, and retire the stopped agent from `subagentDetail` — matched by nearest last-write, since the hook does not say which agent it was (v0.21.0) |
| `SessionEnd` | → `gone` |
| `PermissionRequest` | → `needs_input` while the hold is open, from `working` or from a session with no prior hook only (v0.20.0) |
| *(not a hook)* the `PermissionRequest` hold ending — tap, timeout or retired as stale | `needs_input` → `working`, but **only** if that alert was the hold's own (v0.20.0) |
| *(not a hook)* a completed model round-trip in the session's MAIN transcript, newer than the question | `needs_input` → `working` (v0.19.0) |

Hooks are best-effort: a killed terminal fires nothing, so crabd also ages by transcript mtime
(no writes > 15 min → `idle`; > 2 h → `gone`). A `needs_input` row is **never cleared by aging** —
a question keeps waiting however long the file is quiet. It is cleared by a newer hook event for
that session, or, since v0.19.0, by evidence the model ran again for that session (§2 above), which
is what an answer given in the app looks like from outside the CLI.

## Hard rules for both sides
- crabd reads `~/.claude` strictly read-only and **never logs, serves, or persists the OAuth token** — the token exists only in the HTTPS request to the usage endpoint.
- The widget makes exactly one kind of network call: `http://127.0.0.1:2722/v1/state` (+`/v1/health`). Everything else is bundled.
- All numbers are honest: unknown = `null`/`available:false`, never 0 or a stale value silently re-served.
