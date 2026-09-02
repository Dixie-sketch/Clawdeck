# SideCrab backlog

## Engineering notes

- **Browser verification must pin its own target.** Any automated or agent-driven visual check of
  the widget has to open its own tab against an explicit local URL (`widget/index.html`, or the
  `?mock=` fixtures) rather than reusing whatever page a shared browser session happens to have
  loaded. An inherited tab produces verification against the wrong page — and, on a shared browser,
  can surface permission prompts for a site nobody involved intended to touch.

- ~~**SEC-a — the one open security decision.**~~ **CLOSED in crabd 0.29.0 / widget 0.27.0
  (2026-09-01).** The fix is the one the original row did not list: a **pairing code** —
  crabd mints `~/.sidecrab/panel-token` on first start, the widget holds it as an iCUE
  property (unreadable from any web page) and sends it in every `decide`; crabd compares in
  constant time, locks the gate for a minute after ten rejects, and answers 503 rather than
  falling open when no gate object exists. Neither of the row's two candidates was taken: the
  widget's true origin is `null` (measured via originsSeen — indistinguishable by design), and a
  nonce delivered over `/v1/state` is readable by the same forged-null caller. Mutation-proven:
  verify()→"ok" fails 4 tests, requestId check removed fails 1, fail-open fails 1. Re-enabling
  approvals is now an operator choice, not a risk acceptance.
- ~~**WID-a — decide payload has no per-request id.**~~ **CLOSED in the same release.**
  `pendingPermission.requestId` (16 hex, per register) must be echoed; mismatch is 409, checked
  under the broker lock so a replace between read and write cannot be approved with the old id.
- **A-04 fuller fix (deferred, safe bound shipped).** GitLookup is now bounded to a 1s worker
  budget per unreachable cwd. A fully-async resolver the builder never waits on would remove even
  that per-miss cost but reworks ~8 GitLookup tests — not worth the blast radius yet.

- **Fresh browser tab per gesture-test run.** A CDP tab reused across navigations carries input
  state: the SECOND synthesized two-finger tap in a tab is swallowed and the second long-press
  never fires its timer — regardless of anything under test. This produced a stable, correctly-
  signed, completely false "diagnostics breaks the two-finger ack" (refuted by subset bisect +
  order swap, 2026-08-28). Prior waves' gesture assertions that reused a tab may have measured
  run position; fresh tab per run.

- **"Idempotent" must include the operator's own state.** A re-run that rebuilds a resource from
  scratch also erases whatever a human did to it afterwards — the glow task was disabled on
  purpose and came back enabled and running. Any `-Force`-style re-registration has to read the
  prior state first and restore the parts the installer does not own, with an explicit switch as
  the only way to overturn them.

## Unsanctioned live-verification run (2026-08-26) — claims to RE-TEST, not to trust

**STATUS 2026-08-27 — every claim below is now SETTLED by sanctioned means; section kept for the
process note.** The Stop-hook "error" labeling: fixed waves ago (crabd answers via
`hookSpecificOutput additionalContext`, binary-verified). Approve/deny honored + no-tap
fall-through: PROVEN in the operator-present verification (BACKLOG "Verify before relying on",
closed entry). Raced-not-suppressed dialog: CONFIRMED sanctioned (verify2 run) and stated in
STATE-CONTRACT + README. Echo auto-approval: confirmed and generalized — the operator's
`defaultMode: auto` waves through echo-class AND simple file writes; permission tests use
`--permission-mode default` + mutating commands (recorded in change log (h)).

An agent the orchestrator did not brief ran live approve/deny tests on the operator's machine and
toggled `panelApprovals` on and then back off via `POST /v1/config`. Its own report says its
mission text arrived TRUNCATED and it reconstructed objectives from repo docs. Machine state was
verified clean afterwards (approvals false, settings.json intact, crabd healthy). Its findings are
recorded here as **claims**, to be re-tested under the documented `Verify-PanelApproval.ps1`
procedure with the operator present — not as closed items:

- CLAIM: tap-to-continue works end-to-end, but the CLI renders every Continue tap as
  **"Stop hook error occurred"** — because `decision:"block"` *means* "hook refused". The model
  receives the nudge labelled as an error and visibly hedges about it. Proposed fix already noted
  in `crabd.py`: use `hookSpecificOutput {hookEventName:"Stop", additionalContext}` (the binary
  describes it as non-error feedback). **If re-confirmed, this is a real UX defect worth fixing.**
- CLAIM: approve/deny were honored by the CLI, and no-tap fell through safely.
- CLAIM: the terminal dialog is RACED, not suppressed — it renders immediately and a panel tap
  dismisses it remotely. If true, §4's wording overstates suppression and should be corrected.
- CLAIM (verifiable cheaply, and useful regardless): `echo` is auto-approved by the CLI's own
  classifier even under `--permission-mode manual`, so any permission test built on it reports a
  false failure. Build permission tests on mutating actions.
- Its one repo artifact: `docs/spikes/live-verify.md`.

**Process note for this repo:** live verification that changes operator state (config toggles,
approvals, settings) happens only with the operator present and only through the documented
procedure. An agent whose brief arrives incomplete must stop and ask, never reconstruct scope from
the repo and proceed.

## Small, known, not yet fixed

- **GHOST-a (2026-09-01, live incident) — HALF CLOSED in crabd 0.28.1:** a session killed by an
  app restart (no SessionEnd — hooks are best-effort) read `working`, and card taps queued
  continues into the void (3 queued on a session whose transcript had been frozen 5+ min) — each
  tap-conjured row re-arming another 15 min of `working`. **Fixed:** `/v1/action queue-continue`
  now answers 409 when this process holds no hook-grounded state for the row AND the main
  transcript's mtime is quieter than IDLE_AFTER_SEC (the widget renders non-2xx as "not
  available", no latch); hook-grounded state overrides a quiet transcript so a live long turn
  still queues (both directions test-pinned). **Still open:** a restored row reads `working` for
  up to 15 min after a crabd restart before mtime-aging retires it — the state-None serve
  fallback cannot be tightened without misreading a live long thinking turn; revisit only if the
  15-min window actually bites.
- **GHOST-b (2026-09-01, live incident) — CLOSED in crabd 0.28.2.** Finished sessions rendered
  `working · quiet 33m`: `_resolve`'s done-reactivation fired on the CLI's POST-Stop bookkeeping
  writes (async ai-title, last-prompt records, subagent stragglers — all past the 2 s grace) and
  its early return BYPASSED the aging block, making one late write a permanent zombie until the
  2h prune. Fixed: grace 2→120 s (a real resume re-arms via UserPromptSubmit and never waits on
  the heuristic) and a reactivated row falls through to aging. Also: `SessionStart` now maps to
  `idle`, not `working` — clicking into an old session put an amber WORKING card on the glass
  with no turn running; `PERMISSION_ALERT_FROM` grew `idle` so an SDK/headless permission still
  alerts. All three test-pinned (zombie regression, boundary at 120, transitions table).

*(2026-08-28 design audit — canvas artifact "SideCrab Design Audit" — added the three rows below;
its F2/F3/F6 restate rows already here with recommendations: F2 extra-window overflow → collapse
the TODAY row to one line when a second extra is served; F3 sensors row → grow to --touch-min when
tappable; F6 fmtNum. **F2, F3, F4, F5 and F6 all closed 2026-08-28 in widget 0.26.0** — F1 (hue
compression) is the other lane's. F3 has no row of its own: it was carried in DEV.md v0.25.0's
"not fixed here" note, and is closed there. See DEV.md v0.26.0 for every measurement.)*

- ~~**AUD-F1 — warm-on-warm hue compression:** brand accent `#CC785C`, alert amber `#E8A33D`,
  escalation red `#D4553F` and the crab share one warm band; the attention ring should survive a
  grayscale check without leaning on hue distance it barely has. Audit accent-tinted chrome,
  consider cooling/lightening `--accent` in oklch.~~
  **CLOSED 2026-08-28.** The grayscale check failed where the audit suspected, and worse than it
  guessed: measured at 2560x720, the ring read gray 140 at escalation tier 2 and **120 at approval
  against a crab body of 127** — separations of 13 and 7 — while the CALMEST tier's amber line
  cleared it by 45. The ramp was inverted, because escalating by hue walks the ring toward red and
  red is the luma-poorest corner of sRGB. Both escalated glow lines now keep their oklch hue and
  chroma and gain lightness only (`#E0703C` → `#FF8D59`; approval's `var(--red)` → `#FF7D64`):
  separations are 42 and 34, with the base tier and the alert-vs-brand chrome deltas unmoved.
  `--accent` `#CC785C` → `#BE7E6E` (H 39.15 → 34.72, C 0.113 → 0.084) — it had been sitting 0.36
  degrees off the mascot's own hue; crab dE +42%, esc2 dE +75%. Lightness deliberately held: a
  sweep proved the panel's gray axis has no free slot. Three reclassifications moved brand chrome
  out of the alert idiom (`.card-turn` and `.sheet-btn-deny` warm fills → neutral; the sheet's base
  spine from `--amber` to `--accent`, with `data-mode="action"` stating the amber). Evidence and
  the full before/after tables: `widget/DEV.md` § v0.26.0.
- ~~**AUD-F4 — TODAY row demotion**~~ **CLOSED 2026-08-28 (widget 0.26.0):** IN and CACHE RD carry
  `.stat.meta`, permanently and at every slot; `align-items: end` keeps all four keys on one line
  (kTop 492.38 on all four, measured) and the row height is unchanged at 46.0, so the demotion costs
  the Limits zone nothing — which is what leaves F2 the whole of its own saving. See DEV.md v0.26.0.
- ~~**AUD-F5 — pin the clamp invariant**~~ **CLOSED 2026-08-28 (widget 0.26.0):** it was NOT
  guaranteed — the widget inherited it from crabd's pre-sort (`sortPinned` reads its bands out of
  the delivered order and is the identity with nothing pinned), so an unsorted feed put a waiting
  card in the "+N more" tail. The clamp is now `clampGrid()` and keeps waiting rows first;
  `widget/tests/test_ordering.js` pins it (140 checks, mutation-proven both ways).

*(2026-08-27 v0.16.0 wave closed: the doctor's crabd-answering retry, the `sidecrab-snooze:`
registration — both schemes in `Get-SideCrabProtocolSpec`, all callers loop — and both
`approvalThresholdSec` entries: crabd now accepts + round-trips it, bounds 5..3600 with its own
constants, and a save that omits it PRESERVES the on-disk value. Also closed from the QA register:
SEC-4 — reads gated by the same origin predicate, no `ACAO:*` anywhere; SETUP matcher-level hook
ownership on uninstall + Restore guard; CRB-F5 peek→send→drain; CRB-F2 store/git-cache locks; the
3 contract drifts documented.)*

*(2026-08-27 v0.17.0 wave closed all five rows that stood here: Merge-HookFragment re-install +
Repair wiring-scan entry-level ownership (setup, both mutation-proven); the crabd 404 body-drain,
the deliberate `_send` catch in `_do_hook_stop` (CRB-F5 guarantee intact), and the datalane
negative-test barrier. Also closed: audit-crabd F3 (timeout-gap phantom approval — release() is
now the authority, under the lock), F4 (OTLP cumulative keyspace LRU-bounded at 512), F6
(unparseable resetsAt → null exhaustAt, honest-failure rule), F7 (create=True history line);
audit-widget F1 (repoLine in the card signature) + F2 (unknown prefs round-trip untouched);
audit-notifier F3 (quote-safe attribute escaping, routing-tested) + F4 (case-insensitive scheme
with `re.ASCII` pinned against Unicode-folding homoglyphs).)*

*(2026-08-27 v0.18.0 companion / widget 0.17.0 wave closed both rows that stood here: the `_emit`
sweep found and fixed a real bug — a RAISING `show()` (builder runs outside the adapter's try)
escaped `_emit` and consumed EVERY pending toast in the batch with only the first attempted; now
per-request guarded, raise == failed-show, live-signal deciders retry. Full decider×failure matrix
in `notifier/README.md` "Failure behavior". And `/v1/state` now serves
`toast: {thresholdSec, enabled[, approvalThresholdSec-only-when-set]}` — the widget settings sheet
seeds its display from it without ever touching the touched-latch.)*

*(2026-08-27 wave-19 closures: the Update-SideCrab restart port race — single shared
`Restart-SideCrabTask`, wait-for-port-release, task-aware health verdict, honest exit code;
crabd 0.20.0 never-500 on /v1/state — 10 crashable transcript-record shapes fixed (one bad line
used to kill every card), FileFacts locking, `dump_state` NaN-safe serializer, 503-not-fabricated
cold start; PermissionRequest raises needs_input with three-way stand-down verified; re-fired
different-question resets the alert clock (same text deliberately does not); widget 0.19.0
history chip + gauge forecast sheets + nightcap-wins-quiet-hours; SC-LV-2/SC-LV-3 struck.)*

*(2026-08-27 CD-register wave closed: the external 43-finding register (CD-01..43) fully worked —
see `docs/findings/CD-Register-2026-08-27.md` for the verdict tables. 36 confirmed→fixed across
all four components, 4 refuted/already-closed-at-HEAD, 2 doc resolutions, 1 register wording
corrected. Also closed from this list: the bare-Start-ScheduledTask gate, `_do_history` onto
`dump_state`, and the LimitsReader bool/non-finite boundary — all folded into the lanes.)*

- **A continuation turn finishing on a LATER local day misses that day's recap.week bucket**
  (done→done deliberately doesn't re-arm the done ledger) — unreachable today only because the
  prune retires done rows at 2h; becomes live if that horizon grows.
- ~~**`LimitsReader._window`'s ">1.0 means percent" sniff can't tell `1.0` (100%) from `1` (1%)**~~
  **CLOSED 2026-09-01 (crabd 0.28.1) — it fired live:** a Monday-fresh weekly at 1% gauged RED at
  100% (raw `seven_day.utilization: 1.0` = percent-scale 1). Measured against a live 200: the same
  document's `limits[]` carries unambiguous `percent` 0..100 ints (session 4, weekly_all 1), so
  map_payload now lets a matching limits[] row's percent OUTRANK the window sniff; the sniff
  survives only for documents without a limits[] array. Test-pinned both directions.
- **CD-29's stop-to-file match is nearest-last-write** because SubagentStop carries no agent id —
  if the CLI ever adds one, replace the heuristic, don't tune it.
- ~~Full responsive layouts for ≤3:2 slots~~ **CLOSED 2026-08-28 (widget 0.25.0):** real layouts
  at 840×696 (identity band + Limits/Sessions side by side) and 416×696 (portrait stack); the
  36/64 clock-overhang defect fixed as a class; core line survives only at `≤3:2 and height ≤ 420`.
  78-capture probe zero overflow/overlap, three unchanged slots byte-identical. See DEV.md v0.25.0.
- ~~Doctor aumid row wording~~ **CLOSED 2026-08-27:** the row now distinguishes a missing icon
  file (named, with the dead-pointer note) from genuinely differing values. Message-only; the
  underlying decision logic was already CD-25-tested.
- **Process: shared scratchpad collisions between parallel lanes** — one lane's repro file was
  overwritten mid-wave by another; brief lanes to use lane-unique filenames there.
- ~~**`fmtNum(999999)` renders `1000k`**~~ **CLOSED 2026-08-28 (widget 0.26.0):** the M branch now
  starts at **999500**, where the k branch's own `Math.round` reaches 1000k — so 999,999 reads
  `1.0M`, not floored to `999k` (which would have painted the same string as 998,700 for a larger
  number). Four characters, inside the diag chip's five-character budget; the `999k+` clamp above
  `DIAG_COUNT_SHOWN_MAX` is untouched and pinned by test.
- **`MUTATING_PATHS` in crabd is documentation-only** — nothing asserts it matches the real
  do_POST route table; a future write path could be added without an entry and no test notices.
  panel-log's membership is pinned; the general drift check is open.
- **Headless-Chromium synthesized touch drags emit a mid-stream `pcancel` at (0,0)** while the
  stream continues — recorded by the diagnostics instrument itself; check against real glass
  before treating any pcancel line from the Edge as an iCUE artifact.
- **`Get-SideCrabPortHolder` fails OPEN when the probe throws** (no NetTCPIP) — restart then
  starts blind; pinned deliberate by test, revisit if it ever bites.
- **Install-SideCrab re-registration doesn't race the bind** (`MultipleInstances IgnoreNew`) —
  adding a release-wait would turn a harmless re-install-while-running into a 10s throw; decision
  recorded, not a defect.
- **History chip `dayBusy` single-flight**: a tap during an in-flight week-strip fetch is
  silently inert ≤4s. Accepted for now.
- ~~**Two `limits.extra` windows overflow the limits zone by ~79px**~~ **CLOSED 2026-08-28
  (widget 0.26.0), Option A.** A fixture carries two now (`?mock=extras`), which put the number at
  **99.45 px** past the zone's content box at 2560x720 (98.20 at 2536x696 — worse than the 78.6 the
  0.17.0 note recorded because this second window carries a rendered forecast line). The zone gives
  up its sparkline and drops TODAY to one line (OUT + MSGS) while two windows are served:
  104.78 px freed against 93.03 needed, **+11.73 px spare** at 2560x720 and +10.01 at 2536x696.
  Presence-driven (`body.limits-two-extras`, toggled off the document), never slot-driven.
- **`?mock=future` renders the `asleep` crab where DEV.md describes `worried`** — confirmed
  identical to pre-0.17.0 baseline (not a regression); reconcile fixture or doc.
- ~~notifier/README.md "six toasts" table vs "Failure behavior" matrix overlap~~ **CLOSED
  2026-08-27 as deliberate structure, not a merge:** re-read in full — the two tables answer
  different questions (trigger + dedupe key vs failure-shape + muted behavior) and the 0.18.0/
  0.19.0 rewrites already cross-link them both ways; a merge would be one unwieldy 8-column
  table. No edit made.
- **`PowerShellToastAdapter.build_script` still runs outside `show()`'s try** — consequence is
  handled at the `_emit` boundary (which also covers third-party adapters); moving the builder
  inside would be a redundant second layer. Recorded as deliberate.

- ~~**Serve each session's context-window SIZE in /v1/state**~~ **DONE (crabd 0.28.0,
  2026-08-28).** `sessions[].contextWindowTokens` (int | null, always present), resolved
  most-specific-first: the status line's `context_window_size`, then the `[1m]`/`[200k]` marker in
  the model id, then the Models API's `max_input_tokens` (OAuth bearer, 6 h cache, 15 min failure
  throttle, last-good kept across a failed refresh). Every failure is `null` and `null` draws no
  bar; still no model-name table on either side of the wire. `ctxWindowTokens()` takes the session
  now and prefers the served member, keeping the marker parse as the fallback for a crabd below
  0.28.0. → `docs/STATE-CONTRACT.md` §v0.28.0, `widget/DEV.md` §3.
- **Host history sheet on a fresh boot** draws its line in the right ~6% of the 10-minute axis —
  honest but odd-reading; self-corrects. Revisit only if it grates.

## Host / environment (not a SideCrab defect, but it affects SideCrab)

- **This workstation drops loopback SYN-ACKs in bursts.** Diagnosed 2026-08-26 while making the
  crabd suite deterministic: healthy listening socket, backlog 128, idle — yet four half-open
  `SYN_RECEIVED` connections on 127.0.0.1 whose SYN-ACK never completes, clearing on their own
  after seconds-to-tens-of-seconds. Reproduces with urllib AND http.client, backlog 5 and 128,
  inside and outside a test framework. Likely cause: the TCP dynamic port range is set to
  **1024–65535** (Windows default is 49152–65535), so both ends draw from the whole registered
  range where filter/inspection drivers hook loopback. **Why it matters for the shipped product:**
  crabd's real hooks (Stop, PermissionRequest, the curl ingest hooks, statusline) all POST to
  127.0.0.1:2722 over this same stack. A Stop or PermissionRequest hook that can't complete its
  handshake **stalls the Claude Code session for the hook's timeout with nothing logging why** —
  and I wired those HTTP hooks live today. The `/v1/health` counters make a quiet feed diagnosable,
  but before blaming crabd for a stalled prompt in production, check `netsh int ipv4 show dynamicport tcp`
  and whatever loopback-inspecting filter driver is installed. Consider restoring the default port
  range. The test suite mitigates by retrying connect() only (never the request) and pre-proving
  each ephemeral port.

## Verify before relying on

- **statusLine ingest is wired and works, but this host never invokes it.** Measured 2026-08-26
  after wiring: `settings.json` carries the command correctly; `post_statusline()` called directly
  delivers in 1–36 ms and flips `limits.source` to `"statusline"` with the right utilization; a
  direct `POST /v1/statusline` also works (204). But no document ever arrives on its own — the
  status line appears to render only in an interactive terminal session, not in an app-hosted one,
  so `limits.source` stays `"oauth"` here. Consequence: the OAuth path (and its 429 armor) is
  still the live source on this machine, and the forecast/exhaustAt gets its samples from OAuth
  readings. **Verify on a plain `claude` terminal session before claiming the OAuth reach-around
  is retired** — and keep the OAuth fallback regardless; a feed that only some session types
  produce is a fallback, not a replacement.

- ~~Panel approval — never exercised against a live CLI approval~~ **CLOSED 2026-08-27 — VERIFIED
  LIVE, operator present, via the documented `Verify-PanelApproval.ps1` procedure** (preflight
  green first). All three legs proven on real disposable sessions (`--permission-mode default`;
  the operator's `defaultMode: auto` auto-approves ordinary commands, so default-mode sessions are
  REQUIRED to trigger prompts — echo-class AND simple file-writes both sail through auto):
  **(1) APPROVE** 19:28:37Z session `fad722b5`: "approved from panel: Bash" → command ran, no
  keyboard. **(2) DENY** 19:45:08Z session `5b8ffbda`: "denied from panel: Bash" → file never
  created. **(3) NO-TAP** session `16c01dca`: "permission passed through" at exactly the 55s
  hold → terminal dialog owned the decision, session never wedged. One operator mis-tap during
  the drill (Approve tapped when Deny intended, 19:33) prompted a full defect investigation:
  bench-driving the shipped Deny button captured wire payload `"decision":"deny"` and the markup
  carries `data-decide="deny"` — NOT a button defect; recorded as muscle-memory risk inherent to
  a two-button card. The raced-dialog claim from the unsanctioned run is now CONFIRMED sanctioned
  (verify2, 19:26: operator answered the terminal first and crabd retired the hold as
  pass-through). **panelApprovals is now ENABLED on the operator's machine by operator decision.**
  Stop-hook shape (`decision:"block"`+`reason`) confirmed — `continuationPrompt` measured 0 in
  the binary.
- ~~hooks/README.md old `permissionDecision` shape~~ **CLOSED 2026-08-27:** verified already
  correct (documents the binary-verified `hookSpecificOutput.decision.{behavior}` shape); a
  regression test now pins it.
- ~~The Python suite is not pytest-clean~~ **CLOSED 2026-08-27 — it already was.** The "226 fail"
  figure was the stale record of the presence-guard bug fixed 2026-08-26; `pytest companion/tests`
  was green at 719 before this wave and is green at 755 after (both invocation directions). One
  hardening added: `tearDownModule` now nulls `Handler.builder` so no fixture leaves a builder
  pointing at a deleted TemporaryDirectory.

## Parked (needs the world to change)

- **Glow under a Scheduled Task: SOLVED (2026-08-26).** The crash was never console-related — it
  was a use-after-free in `lighting/icue.py`: `connect()` published the `CueSdk` to `self` only on
  the success path, so every FAILED handshake freed the ctypes thunk the native SDK keeps calling
  ~2x/s. The varying NTSTATUS codes (`0xC000001D`/`0xC0000005`/`0xC0000096`) were one memory bug,
  not four environment bugs; `cuesdk` 4.0.84 is also the newest on PyPI, so there was no upgrade
  path. A/B proof: drop the local ref → crash 1.2s; hold one extra reference → 40s clean. After the
  fix the real entry points survive 90s in all four previously-fatal contexts and 130s as
  `pythonw glow_launcher.pyw` + `DETACHED_PROCESS` (the task's exact action). `glow_launcher.pyw` is
  now a plain in-process entry point (the child-console relay this project earlier added on a WRONG
  console theory is gone). New `--selftest` reports SDK/session/device/LED state with a verdict and
  exit 0/1/2, logged at startup. `SideCrab-glow` is still **Disabled** — re-enabling is deliberate
  (`Enable-ScheduledTask SideCrab-glow`); it will now stay up and say WHY it is dark rather than
  crash-loop. This machine still exposes 0 lightable LEDs and currently has no iCUE SDK server
  listening at all (iCUE running is not evidence the SDK server is up).

- **Tap-to-reply (real injection):** needs a supported external send into a live session. The spike
  found none that is safe — the bus delivers only at tool rounds, the CLI forks a new session, and
  window targeting is unsafe. `POST /v1/action` answers `501` for `reply` until this changes.
- **Cloud session cards:** no stable endpoint.
- **RGB glow on case lighting:** needs the chassis LEDs exposed to the iCUE SDK, or a Corsair
  keyboard/mouse on the machine. See `lighting/README.md`.
