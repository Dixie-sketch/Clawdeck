---
title: "QA audit — Setup/PowerShell + Contract conformance (lanes 4 & 6)"
audit_of: "2026-08-27 SideCrab full QA (docs/QA-AUDIT-PLAN.md)"
lane: "setup-contract"
date: 2026-08-27
method: "read-only; live commands were NOT run (no crabd on this box). Findings are code-derived."
scope:
  - setup/*.ps1, setup/SideCrab.Common.ps1, hooks/*, docs/STATE-CONTRACT.md
  - companion/crabd.py (builder + endpoints), widget/scripts/sidecrab.js, notifier/sidecrab_toast.py, lighting/decision.py
---

# SideCrab QA — Setup/PowerShell (lane 4) + Contract conformance (lane 6)

Every finding: path (file:line) · the drift/defect · consequence · severity · VERIFIED (read off the
code) vs INFERRED (reasoned, not reproduced — no live crabd on the audit box). Auditors owe evidence,
not prescriptions (charter §3.4.1); where a fix has two shapes, both are named and the choice is left open.

---

## SECTION A — Setup / PowerShell

### A1 — Matcher-level hook ownership deletes / fails to guard a co-located foreign hook  · MEDIUM · VERIFIED
`Test-SideCrabHookMatcherIsOurs` (SideCrab.Common.ps1:832-846), `Merge-HookFragment`
(Install-SideCrab.ps1:100-118) and `Remove-HookEntries` (Uninstall-SideCrab.ps1:100-127) all classify an
**entire hook matcher object** as "ours" when **any** hook inside it carries the marker.

- **Uninstall** drops the whole matcher (Uninstall:119 `if ($isOurs) { $removed++ }` — the matcher is not
  kept), deleting any operator hook that shares that matcher block with a SideCrab hook.
- **The restore guard has the same blind spot.** `Split-SideCrabSettings` (Common:876-882) routes the
  whole "ours" matcher into `Ours`, so a foreign hook co-located there never enters the `Foreign` split and
  its loss is invisible to `Compare-SideCrabSettingsPair` — a "safe" restore reverts it with no warning.

**Consequence:** an operator hook that shares a matcher with a SideCrab hook is silently deleted on
uninstall, and silently reverted by a guarded restore.
**Reachability / why it's not higher:** the shipped installer always writes each SideCrab hook as its OWN
standalone matcher (hooks/settings-hooks-fragment.json), and `Merge-HookFragment` appends rather than
merges — so no matcher is ever "partly ours" in normal operation. This bites only when the operator
hand-merges a SideCrab hook into an existing matcher block. It is nonetheless a real gap in a guard whose
entire purpose (Restore header, "THE ONE HAZARD, AND THE GUARD") is protecting operator edits.
**This is the worst setup finding.**

### A2 — Marker is a substring match; a foreign hook that references the crabd URL is clobbered · MEDIUM–LOW · VERIFIED
The marker `127.0.0.1:2722/v1/hook` is matched as a substring anywhere in a hook's `command`+`url`
(Common:243, Common:841; Install:106; Uninstall:114). Any operator hook that legitimately posts to crabd —
or any command that merely contains that literal (an `echo`, a comment) — reads as SideCrab's:
`Merge-HookFragment` drops it on re-install (for the 7 fragment events), and `Remove-HookEntries` removes it
on uninstall for **all** events (wider blast radius than merge).
**Consequence:** operator hook silently removed. **Likelihood:** low — requires the operator to reference
the crabd endpoint from their own hook.

### A3 — settings.json round-trips through an unordered hashtable → reorders every operator key on each write · LOW · VERIFIED
`ConvertFrom-Json -AsHashtable` (Install:314, Uninstall:227) yields an unordered `[hashtable]`; the paired
`ConvertTo-Json` (Install:357, Uninstall:279) re-emits the operator's top-level and nested keys in hash
order. No semantic loss, but every install/uninstall reshuffles unrelated keys, churning the operator's
settings.json and its git diff. Compounding: **Install always backs up + rewrites** when hooks aren't
skipped, even on a no-op re-run (Merge removes then re-adds identical entries), piling timestamped backups
and reordering each time. Uninstall is smarter — it writes only `if ($changed)` (Uninstall:278) and deletes
the just-made backup otherwise (Uninstall:283); Install lacks that symmetry.

### A3b — `ConvertTo-Json -Depth 40` corrupts settings nested deeper than 40 · LOW · INFERRED
Both read and write pin Depth 40. An operator object nested past 40 levels stringifies to
`"System.Collections..."` on write. Not plausible in real Claude Code settings, but the read/write depths
should stay equal and generous. No fix urgency.

### A4 — `Start-ScheduledTask` rides the "Register scheduled task" ShouldProcess verb; config write double-gates · LOW (honesty) · VERIFIED
Install:270-277 gates Register **and** Start under one `ShouldProcess('Register scheduled task')`, so a
`-Confirm` of "Register" silently also starts the task. Under `-WhatIf` both are correctly skipped (no
safety impact) — only the confirm prompt is imprecise. Separately, the panelApprovals write double-gates:
the outer `ShouldProcess($ConfigPath,'Configure panelApprovals')` (Install:366) wraps
`Set-SideCrabPanelApprovals`, which has its own `ShouldProcess` (Common:384) → two `-Confirm` prompts for
one write.

### Audited SOUND — Setup
- **Injection via the chained statusline command — SOUND.** `run_chained` (sidecrab_statusline.py:153-168)
  runs `shell=True` on the operator's OWN prior status-line command, which Claude Code already trusted and
  shell-ran; the untrusted stdin document is passed via `input=` (a pipe), never interpolated into the
  command string. No untrusted value reaches a shell. No `Invoke-Expression` exists anywhere in setup.
  Registry/settings command strings embed only operator-controlled, quoted paths (`$RepoRoot`, resolved
  python) — `Get-SideCrabProtocolCommand`/`Get-SideCrabStatusLineCommand`; a quote inside `$RepoRoot`
  would break the command but is operator-controlled, not an injection vector.
- **Disabled-task logic — SOUND.** `Get-SideCrabTaskEnableDecision` (Common:187-219) re-registers a
  disabled task but leaves it Disabled and unstarted unless `-ForceEnable`; Update (Update:121-125) and
  Repair (Repair:171-175, 199-204) both explicitly skip Disabled tasks and never attach a start/enable
  fix to one. **No path enables or starts a disabled task automatically**, and Repair `-Fix`'s stale-code
  restart is only ever attached to a Running (non-disabled) task.
- **Restore foreign-edit guard, non-co-located case — SOUND.** Key-sorted, array-order-preserving
  canonical comparison (`Get-SideCrabCanonicalValue`, Common:797-821; the one-element-array trap is
  handled by the comma-wrap at Common:818) correctly flags added/removed/edited foreign top-level keys and
  refuses without `-Force`; the pre-overwrite safety backup (Restore:239-243) and the unparseable-backup
  refusal (Restore:201-207) are correct.
- **Backup handling — SOUND.** Timestamp read from the NAME not mtime (`Read-SideCrabBackupStamp`); newest
  backup never pruned (`Get-SideCrabPruneDecision`); backups kept at every uninstall switch; residue
  driven by one shared table (`Get-SideCrabResidueSpec`) so report and `-Purge` cannot disagree.
- **ShouldProcess on the state-mutating Common functions — SOUND.** Set/Remove-SideCrabAumid,
  Set/Remove-SideCrabProtocol, Set/Clear-SideCrabPanelApprovals, Save-SideCrabPriorStatusLine are each
  `SupportsShouldProcess` with the write inside the gate; `New-Item`/`Set-Content`/`Copy-Item` inherit
  `-WhatIf` as ShouldProcess-aware cmdlets, so `-WhatIf` runs create nothing.
- **Toast-ownership narrowing on uninstall — SOUND.** `$ownsToast` (Uninstall:179-183) correctly removes
  the two HKCU registrations only on the full sweep or a `-TaskName SideCrab-toast` run.

---

## SECTION B — Contract conformance (STATE-CONTRACT.md, both directions)

Producer = companion/crabd.py; consumers = widget/scripts/sidecrab.js, notifier/sidecrab_toast.py,
lighting/decision.py. **4 material drifts + 2 minor**, ranked.

### B1 — `panelApprovals` is HTTP-writable in crabd but the contract documents no such /v1/config key · HIGH (security-relevant) · VERIFIED
`crabd.py:3919` `CONFIG_WRITABLE = ("quietHours","toast","digest","budget","panelApprovals")`, and
`_do_config` validates + writes `panelApprovals` (crabd.py:3963-3968). The contract documents the
/v1/config writable set as **quietHours** (v0.4.0), **toast** (v0.7.0), **digest** (v0.8.0), **budget**
(v0.10.0) — and documents `panelApprovals` (v0.12.0 §4) as installer/file-managed ("the installer
asks/flips per the operator's choice"), **never** as HTTP-settable. The contract's founding stance
(STATE-CONTRACT.md:248-249) is explicit: only whitelisted keys are writable and security flags "must never
be settable remotely."

**Corroboration this is drift, not an undocumented-but-intended feature:** crabd's OWN `_do_config`
docstring says "quietHours, toast, digest and budget, and NOTHING else" (crabd.py:3922) — it omits
panelApprovals; and the widget's config code asserts "quietHours, toast and budget are the ONLY keys
writable over HTTP (contract)" (sidecrab.js:3608) and never sends panelApprovals.

**Consequence:** any local process can arm panel approvals over the unauthenticated loopback API
(`POST /v1/config {"panelApprovals":{"enabled":true}}`), removing a precondition for the
`POST /v1/action {"action":"decide","decision":"allow"}` approval path — a security-relevant expansion of
the HTTP surface the contract's whitelist exists to bound. **Overlaps the security lane (5).** Fix is
either "document panelApprovals as a /v1/config key" or "remove it from CONFIG_WRITABLE and keep it
installer/file-only" — the maintainer's call; the second matches the contract as written. **Worst contract drift.**

### B2 — top-level `continuePrompts` is served and consumed but undocumented as a served field · MEDIUM · VERIFIED
crabd emits `continuePrompts` at the /v1/state document top level (crabd.py:3126); the widget reads
`lastGoodDoc.continuePrompts` (sidecrab.js:2677, 210). The contract documents `continuePrompts` **only** as
a config.json key (v0.12.0 §3) and never lists it in the /v1/state Document or any additive section. The
widget has no channel to config.json, so this served field is load-bearing yet unspecified — a change to
it lands in code without the contract's "a change lands here first" gate (STATE-CONTRACT.md:329-330).
Both an emitted-but-undocumented field AND a consumer reading an undocumented field.

### B3 — `contextSource: "transcript"` emitted; contract names only `"statusline"` · LOW · VERIFIED
`_context` (crabd.py:3353-3361) emits `contextSource ∈ {"statusline","transcript",null}`. The contract
(v0.12.0 item 1) documents only that statusline-fed contextTokens "carries contextSource: statusline" and
neither enumerates `"transcript"` nor states the key is always present. The widget renders provenance off
it. A reader trusting the contract would not expect `"transcript"`. Doc-lies-by-omission.

### B4 — undocumented status codes on documented endpoints · LOW · VERIFIED
`/v1/config` returns **500** on write failure (crabd.py:3970) — contract documents only 204/400.
`/v1/action` returns **501** for `queue-continue`/`decide` when the reader/broker is absent (crabd.py:3878,
3903) — contract documents 501 for `reply` only. Both occur only in degraded/older builds, but are
response shapes the contract does not cover.

### B5 — presence-guarantee of `pendingPermission` / `contextSource` unstated · MINOR · VERIFIED
crabd always emits both keys (null when N/A: crabd.py:3327, 3360), but the contract doesn't state they are
always-present the way it does for `queuedContinue`. The widget presence-gates defensively, so no runtime
issue — a documentation gap only.

### B6 — stale in-code comment, widget /v1/config · MINOR · VERIFIED
sidecrab.js:3608 says quietHours/toast/budget are "the ONLY keys writable over HTTP (contract)", omitting
`digest` (which IS documented and writable). A stale widget comment, not a contract drift — noted so B1's
evidence isn't misread as "digest is also undocumented".

### Audited SOUND — Contract
- **Schema handling, both directions — SOUND.** crabd serves `SCHEMA_BREAKING = 5` (crabd.py:58, 3112);
  widget `SCHEMA_MAX = 5` accepts 1–5 and dead-feeds anything above or non-integer (sidecrab.js:28, 690);
  notifier `SUPPORTED_SCHEMAS` and glow `ACCEPTED_SCHEMAS` are both `{1,2,3,4,5}`. Matches the "widget
  accepts 1–5, additive-by-presence" rework exactly, and test_decider.py:496-536 pins the consumer sets to
  the contract's own numbers.
- **`estate` removal (v0.9.0) — SOUND both sides.** No `.estate`/`estateStrip` property read exists in the
  widget (the many "estate" tokens are the crab's mood/accessory metaphor — "dress for the fleet"), and
  crabd emits no `estate` key. Removal is honoured and deploy-order-free as the contract claims.
- **Per-session fields + null semantics — CONFORMANT** for id/title/titleSource/cwd/repo/branch/state/
  stateSince/lastActivityAt/lastEvent/model/speed/subagents/todayOutputTokens/question/turnStartedAt/
  acked/subagentDetail/events/contextTokens/queuedContinue/pendingPermission (crabd.py:3297-3336).
  `titleSource` emits exactly `{custom,ai,prompt,cwd,null}` (crabd.py:1394-1404 + 3296), matching v0.12.0.
  `queuedContinue` is always present (crabd.py:3334), matching v0.14.0's "the key itself is the feature
  detection". Sort order (needs_input→working→done→idle, then newest) matches the contract (crabd.py:3337).
- **`burn` additive fields — CONFORMANT.** byModel (cap 4, desc; sum == today.outputTokens by
  construction, crabd.py:3234-3264), daily (7 buckets, DST-safe), budget (absent when unconfigured —
  presence IS the feature detection), costUSD/costSource (both always present, null without OTLP). Widget
  presence-gates each (byModel `Array.isArray`, costUSD `typeof==='number' && isFinite`).
- **`limits.source` / `exhaustAt` — CONFORMANT.** source stamped statusline|oauth on every served block,
  statusline > oauth by silence only (crabd.py:3133-3163); exhaustAt nullable and never past resetsAt;
  widget honours past/after-reset → render nothing (sidecrab.js:1490-1527).
- **Endpoint semantics — CONFORMANT** for /v1/health (additive counters, not part of the state contract),
  /v1/history (regex + strptime day validation, 400 vs 200-empty split, cap 200 + truncated), /v1/hook*
  (204/200 fire-and-forget, empty-body pass-through), /v1/action ack (204/404) / ack-all (204 idempotent) /
  reply (501). The Stop-hook and PermissionRequest response SHAPES match the contract's measured
  `additionalContext` / `decision:{behavior}` pins.
