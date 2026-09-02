# External review register CD-01..43 — verification + fix record (2026-08-27)

Joe delivered a 43-finding external review register (5 P1 / 26 P2 / 12 P3), compiled against
commit `ba050b3` — one commit behind the wave-19 ship (`291978d`). Worked as a verify-then-fix
fan-out: four Opus lanes by component ownership, each required to reproduce or refute AT HEAD
before touching anything, because two register claims already contradicted orchestrator
measurements. That discipline mattered: **four findings were refuted or already closed at HEAD**,
and one register claim was itself mis-stated in a way that would have misled a fix.

Lanes: crabd (11 findings) · setup (12 + carry-over) · widget (15) · lighting+notifier (3) ·
orchestrator (2 doc findings + pre-verification).

## P1 verdicts

| ID | Claim | Verdict |
|----|-------|---------|
| CD-01 | Rapid second question cleared while unanswered | **REFUTED at HEAD** — closed by 291978d (different-question re-fire moves `since`, putting B's transition ahead of A's activity; the literal replay now passes). Pinned by regression tests. |
| CD-02 | Targeted uninstall tears down the rest of SideCrab | **CONFIRMED → FIXED** — `Get-SideCrabUninstallScope` owns a per-component surface table; `-TaskName SideCrab-glow` now removes the glow task only. |
| CD-03 | Uninstall blindly restores saved status line over a newer foreign one | **CONFIRMED → FIXED, worse than filed** — the null-prior arm outright DELETED a foreign status line. Restore now gates on marker ownership; foreign lines preserved with a loud message. |
| CD-04 | Update exits 0 when crabd never becomes healthy | **ALREADY CLOSED (wave 19)** — `exit ([int]$verifyFailed)` off the task-aware verdict; pinned. |
| CD-05 | Both toast action buttons broken (protocols unregistered) | **HEADLINE REFUTED by direct HKCU read** (both schemes registered, correct handlers). Residual CONFIRMED → FIXED: the protocol/AUMID status report had no arm for a completely missing scheme — a silent report read as clean. `NOT REGISTERED` rows added. |

## P2/P3 verdicts by lane

**crabd (companion 856 → 914 tests, 18/18 mutants caught, VERSION → 0.21.0):**
CD-06 ✅ fixed (Stop always re-dates `stateSince`; `entered`/`moved` split keeps the done-ledger
semantics) · CD-07 ✅ fixed (history replay restores terminal state; questions deliberately not
restored) · CD-08 **refuted at HEAD** (tail-loss half — the per-line catch inside the loop means a
throwing record costs only its own line; pinned) · CD-09 ✅ fixed (window eviction + row pruning;
`needs_input` keeps its contract exemption) · CD-10 ✅ fixed (`_finite_number` parse-boundary guard
at 5 sites — `1e309` in config crashed EVERY build; bool gauged 100%; `_do_history` onto
`dump_state`) · CD-11 ✅ fixed (`doneToday <= sessionsToday` by construction) · CD-28 ✅ fixed
(lookback floored at the turn's start) · CD-29 ✅ fixed (SubagentStop claims its file before the
trim) · CD-30 ✅ fixed (`drain_if` compare-and-drain) · CD-36 ✅ fixed (stale statusline context
loses to newer transcript data, 120s allowance).

**setup (233 → 278 tests):**
CD-02/03/05-residual per P1 table · CD-16 already closed (wave 19), pinned · CD-17 ✅ fixed (real
`--porcelain` preflight; refuted-on-live-data proof that the old promise was false) · CD-18 ✅
resolved by REMOVING `-TaskName` from install (single-instance by construction — fixed port,
fixed hooks; the param could only create unmanageable installs) · CD-19 ✅ fixed (logon daemons in
Ready = dead → FAIL rows for toast/glow; crabd deliberately excluded — its liveness is the
health + port-owner rows; live host pre-checked: toast Running, glow Disabled, no false fire) ·
CD-20 ✅ fixed (`-FixVerify` re-measures; "didn't throw" no longer means fixed) · CD-21 ✅ fixed
(`import cuesdk` under the task's interpreter before registering glow) · CD-24 ✅ fixed (freshness
watches all four glow files) · CD-25 ✅ fixed (dead icon no longer "Current"; stale IconUri
deleted) · wave-19 carry-over ✅ fixed (no start fix offered while anything holds 2722; offered
fixes go through the port-waiting restart).

**lighting + notifier (67 → 100 · 471 → 498 tests, 9/9 mutants caught, notifier → 0.19.0):**
CD-22 ✅ fixed (control/session loss drops devices+claims — deferred out of the native callback
thread; paint discriminates `CE_NoControl`; reacquisition on 2s backoff) · CD-23 ✅ fixed (bounded
30s mid-alert rescan; per-device acquisition; vanished devices pruned) · CD-26 ✅ fixed as decided
(**register wording corrected**: the switch already gated three of six kinds, not one — the
escapees were digest/budget/outage; now `toast.enabled=false` gates ALL emission at `_emit`;
muted live-signals re-arm, muted periodics consume; one log line per kind per day; matrix gains
a "muted" column).

**orchestrator (docs):**
CD-37 ✅ fixed (double-click import needs iCUE 5.46.67+; 5.44+ imports from within the app) ·
CD-43 **resolved as not-a-contradiction** — README now states the truth precisely: one live
approval run DID happen, unsanctioned, recorded as unverified claims (`docs/spikes/live-verify.md`);
the feature stays "written-and-tested, not proven" until the documented operator-present procedure
runs. CD-27 ✅ resolved by import (glass measured at widget 0.19.0, versions aligned).

**widget (0.20.0 packaged; 85 overflow captures across 5 slots all clean; zero uncaught errors):**
All 15 CONFIRMED → FIXED except CD-26's widget half (already correct — the label matches the
notifier's new global gate). CD-12 ✅ (units failure no longer discards a healthy temperature;
30s units backoff) · CD-13 ✅ (a failed Approve/Deny now says so on the panel: "allow not sent —
decide in terminal") · CD-14 ✅ (the +N-more chip opens a real overflow sheet; rows open their
sessions; hid 7-11 sessions before) · CD-15 ✅ (Escape, focus trap, aria-modal, tab stops on
crab/gauges/cards; gesture-equivalents and grid arrows deliberately skipped and named in code) ·
CD-31 ✅ (ack notice visible to a11y) · CD-32 ✅ (standalone clock default matches the manifest) ·
CD-33 ✅ (≤3:2 slots get an honest core line — waiting/working counts + limit % — instead of
losing the app's purpose; full narrow layouts recorded as future) · CD-34 ✅ (panel escalation
computed from the FEED — a filter can no longer hide a standing alert; header says "N waiting
hidden") · CD-35 ✅ (sheet generation counter; a late history response can't hijack another
sheet) · CD-38 ✅ · CD-39 ✅ · CD-40 ✅ (failed ack-all corrects its receipt) · CD-41 ✅ (12h
times carry AM/PM) · CD-42 ✅ (quiet mode re-evaluates locally when the feed is stale — can only
clear, never assert). Plus one pre-existing fix found in passing: the connecting+no-sensors
identity zone out-specified the narrow-slot rule (42-45px clock overhang at ≤3:2); and one
instrument correction (overflow probe now measures scroll containers, not their clipped rows —
re-proven against HEAD's known defect before trusting it).

## Register meta-corrections
- CD-05 headline and CD-26 scope were both wrong as filed; CD-01/CD-04/CD-08/CD-16 were stale
  (fixed in the commit after the register's pin). A register compiled against a moving tree needs
  its own revalidation pass — which is exactly what this wave was.
- Version lockstep across components is now formally ABANDONED: crabd 0.21.0, notifier 0.19.0,
  widget 0.20.0, lighting unversioned. Components version independently; the contract
  (`STATE-CONTRACT.md`, schema 5) is the compatibility authority, not version-number equality.
