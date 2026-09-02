---
title: "QA Audit — Widget correctness lane (lane 2)"
audit: QA-Audit-2026-08-27
lane: 2 (widget correctness)
scope: widget/scripts/sidecrab.js, widget/index.html, widget/styles/sidecrab.css
target: STATE-CONTRACT.md (schema 5-compat, feature-detected)
method: full read of the 4931-line runtime + markup + stylesheet; sink tracing; state-machine + gesture + approval + resource review
verdict_injection: NO INJECTION SINK — sound
last_verified: 2026-08-27
---

# Widget correctness audit — findings

Read in full: `widget/scripts/sidecrab.js` (4931 lines), `widget/index.html`,
`widget/styles/sidecrab.css`. Every claim below cites the line it was read off.
House rules honoured: evidence not prescriptions; ranked most-severe first; the
wave is capped (no readiness review manufactured).

---

## 1. THE HEADLINE: untrusted-string rendering — **NO injection sink. VERIFIED.**

**Verdict: a session titled `<img src=x onerror=alert(1)>`, a question containing
`<script>…`, a tool name of `"><svg onload=…>`, a cwd/repo/branch/event/summary
carrying any markup — ALL render as inert literal text in QtWebEngine. None
execute.** This is the single highest-value result of the lane and it is clean.

**Evidence — every sink classified:**

- **Zero HTML-parsing sinks in the entire file.** `grep` for
  `innerHTML | insertAdjacentHTML | outerHTML | document.write |
  createContextualFragment | .html(` over the whole 4931-line runtime returns
  **no matches**. There is no string-to-DOM path anywhere.
- **The universal text sink is `setText()` → `textContent`** (sidecrab.js:563-567:
  `el.textContent = s`). Every dynamic string that is not built on a fresh
  element goes through it.
- **Freshly-built nodes take strings via `element.textContent =`**, never markup.
  Traced for every untrusted source the plan names:
  - session **title** → `title.textContent = tp.text` (2254); sheet
    `setText(ui.sheetTitle, tp.text)` (2630); burn row `title.textContent =
    titleParts(s).text` (2887); timeline tag via `shortTitle` →
    `tagEl.textContent` (3048, 3410).
  - **question** → `event.textContent = question` (2303); sheet
    `setText(ui.sheetQuestion, s.question || …)` (2657).
  - **lastEvent** → `event.textContent = s.lastEvent || ''` (2306).
  - **pendingPermission.tool / .summary** → `apTool.textContent = String(pend.tool…)`
    (2282), `apSum.textContent = String(pend.summary)` (2288); sheet
    `setText(ui.sheetApprovalTool, tool)` / `…Summary` (2644-2646);
    `setText(ui.sheetApprove, 'Approve ' + tool)` (2646).
  - **cwd / repo / branch** → `repo.textContent = s.repo ? (s.repo + '@' + …) :
    (s.cwd || '')` (2262); sheet 2632.
  - **event text / kind** (per-session + history + timeline) →
    `text.textContent = … String(e.text)` (2823), `String(list[i].text)` (2823),
    day drill `text.textContent = rows[r].text` (3413), timeline 3051.
  - **continuePrompts** (config-fed extras) → button face
    `btn.textContent = list[i].label` (2698); the full prompt rides
    `setAttribute('data-continue-prompt', …)` (2696) — an attribute value, read
    back with `getAttribute` and posted as a JSON body field, never parsed as
    markup or as a URL.
  - **burn.byModel model names** → `name.textContent = rows[k].name` (2960).
  - **queued-continue label** → `queued.textContent = 'queued: ' + qLabel` (2345).
- **The only `setAttribute` calls that carry untrusted data write `title` /
  `aria-label` / `data-*`** (e.g. 2258, 2241-2242, 2346, 3146-3149). Attribute
  values are not HTML-parsed; `title`/`aria-label` are display/AX text; the
  `data-day` value is additionally regex-validated (`DAY_RE`, 3089) before it is
  set, and `encodeURIComponent`-escaped before it reaches the history URL (3197).
- **CSS carries no feed-derived injection surface.** `grep` of the stylesheet
  finds no `url(`, no `attr(`, no `expression(`; every `content:` is a static
  literal (e.g. 540, 601, 1334). Feed values only ever reach *numeric* custom
  properties (`--w`, `--h`, `--t`) or fixed token strings (`rampColor` returns
  `var(--red|amber|gauge-blue)`, 1411-1415). No feed string is interpolated into
  a stylesheet.
- **The lone `Function(...)`** (518) evaluates a hardcoded property *name* from a
  fixed set (`'textColor'`, `'crabStyle'`, `'uniqueId'`, …), never feed data.

There is nothing to exploit; no remediation is owed. This section is the
adversarial target and it holds.

---

## 2. Findings (ranked)

The runtime has clearly survived many prior waves; it is defensive throughout
(presence-gating, `typeof`/`Array.isArray` guards, `hasOwnProperty` lookups,
map pruning). The genuine defects found are all **LOW / informational**. No
High or Critical.

*Status 2026-08-27 (widget 0.16.0): F1 + F2 FIXED (repoLine signed in the card sig; unknown
stored prefs round-trip untouched). F3 stands (unreachable on QtWebEngine); F4 addressed at the
API side by the SEC-1/SEC-4 origin gates.*

### F1 — LOW — a `cwd`-only change on a repo-less session leaves a stale repo line
- **Path:** card signature `sig` omits `cwd` (sidecrab.js:1927-1952); the repo
  line falls back to `cwd` when `repo` is null (2262: `… : (s.cwd || '')`).
- **Trigger:** a session with `repo === null` whose `cwd` changes while every
  other signed field (id/state/title/model/…) stays identical.
- **Consequence:** the card is not rebuilt (sig unchanged) and keeps showing the
  previous `cwd` until something else moves the signature.
- **Severity rationale:** LOW — `cwd` is near-static for a live session, and when
  a `repo` exists `cwd` is never displayed. Cosmetic, self-heals on the next real
  change. **VERIFIED** by reading the sig builder against the render at 2262.

### F2 — LOW — older-widget `savePrefs` clobbers a newer build's filter/density value
- **Path:** `loadPrefs` maps an unrecognised stored `sessionFilter`/`density`
  value to index 0 via `prefIndex` (1122-1127, 1145-1146); `savePrefs`
  then *unconditionally* writes `FILTERS[filterIdx].key` / `DENSITIES[densityIdx].key`
  back (1172-1173).
- **Trigger:** a NEWER widget writes a filter/density mode this build does not
  know; this build loads (clamps to `all`/`comfortable`) and later saves (any pin
  or chip tap).
- **Consequence:** the newer build's stored mode value is overwritten with this
  build's default. Note the read-modify-write DOES preserve unknown *keys*
  (1170-1171) — this gap is unknown *values of known keys* only.
- **Severity rationale:** LOW — display state only, no data loss, and only bites a
  mixed-version install. **VERIFIED** against 1145-1146 / 1172-1173.

### F3 — INFORMATIONAL — `poll()` can wedge only in an engine without `AbortController`
- **Path:** the abort timer is armed *inside* `if (typeof AbortController !==
  'undefined')` (671-675); without it a fetch that never settles leaves
  `inFlight === true` and stops the poller for the session (676: `done()` only
  runs on settle).
- **Severity rationale:** INFORMATIONAL — QtWebEngine (iCUE) and every dev browser
  ship `AbortController`, so the timeout is always armed in practice; not reachable
  on the target. Flagged for completeness. **VERIFIED** at 661-685.

### F4 — INFORMATIONAL (cross-lane → lane 5) — the widget's write actions carry no caller proof
- **Path:** `postJson` sends only a `Content-Type` header (3632-3633); `decide`
  and `queue-continue` (the two v0.12.0 write paths) post a plain JSON body to
  `127.0.0.1:2722` with no token, origin assertion, or same-machine attestation.
- **Note:** this is *by contract* (the localhost API is unauthenticated by
  design) and is the SECURITY lane's call, not a widget defect. Recorded here only
  as the widget-side observation for lane 5's trust-posture model. Not actioned in
  this lane.

---

## 3. Audited SOUND (checked, no defect)

- **Untrusted-string rendering** — §1. No HTML sink anywhere; QtWebEngine cannot
  execute a hostile title/question/tool/summary/event/repo/cwd/continue-prompt.
- **Schema ceiling / dead-feed** — `acceptDoc` rejects a doc whose `schema` is
  absent, `<1`, `>SCHEMA_MAX(5)`, non-integer, or a numeric string (`"5" !==
  Math.floor("5")`), and an unparseable `generatedAt`, all → `pollFailed` +
  worried-crab stale render (690-701). A schema-6 doc dead-feeds by design.
- **Presence-gating of every additive v-field** — `pendingPermission`,
  `queuedContinue`, `budget`, `fleet`, `subagentDetail`, `events`, `limits.extra`,
  `byModel`, `contextTokens`, `costUSD`, `titleSource`, `recap.week` are each read
  only behind a `typeof`/`Array.isArray`/`!Array.isArray` guard; a wrong-typed or
  newer-crabd field is ignored, never crashes (e.g. 1677-1680, 2183-2185,
  2128-2131, 1642, 908-909). A newer field is simply never read.
- **State-machine transitions** — the sheet follows the live row and self-closes
  when the session leaves the state the sheet opened on (2626: `s.state !==
  sheetOpenState → closeSheet`), so an Acknowledge/Approve control can never sit
  under a finger for a question already answered at the keyboard.
- **Optimistic-action desync** — `ack` and `ack-all` roll back on non-2xx OR on
  network catch (3504-3520, 4136-4142); `decide` is fire-and-forget with an
  optimistic close and crabd/terminal as the authority on failure (3549-3561,
  fail-safe: a lost `allow` times out to the terminal dialog, never auto-allows);
  `queue-continue`'s durable indicator is the feed's `queuedContinue` chip, so a
  failed POST simply never shows a queued chip (2341-2348, 3530-3542).
- **Gesture discrimination** — one pointer map arbitrates all four gestures
  (3394 region): a second finger cancels every single-pointer gesture (3753-3765);
  travel > `TAP_SLOP` cancels the long-press (3801) and suppresses the synthetic
  click (3829) so a drag is never a tap; axis is committed once and never
  re-decided (3807-3818); swipe only engages on a live DISMISSABLE row
  (3858-3866); the click-swallow is a capturing document listener so a
  gesture-consumed click reaches no control, including future ones (3728-3732,
  4793).
- **Approval UI (security-critical)** — Approve fires ONLY from a real click on a
  `[data-decide]` button routed above the generic branch (4069-4074), validated
  against `{allow,deny}` and requiring `sheetSessionId` (3549-3551); no gesture,
  timer, or synthetic click can reach it. The tool name is ALWAYS shown before
  Approve — on the card (2282), in the sheet body (2644-2646), and on the button
  face itself ("Approve Bash", 2646); a missing tool degrades to "a tool", never a
  bare "Approve". `ack-all` and the crab tap explicitly SKIP a `pendingPermission`
  card (4114-4117) so a permission gate cannot be silenced by the big target. At
  the 55 s hold expiry the buttons are deliberately left enabled (a tap then
  reaches crabd, which no longer holds the request → logged no-op); documented and
  fail-safe.
- **Persistence race (the prior lane's area)** — pins/filter/density are written
  together in one read-modify-write that preserves unknown keys (1166-1176);
  single-threaded JS means no intra-widget race; the vendor object is keyed on
  `uniqueId` so it never collides with other iCUE widgets.
- **Resource / 24-7 lifetime** — exactly two permanent intervals (poll 3 s, tick
  1 s; 4809-4810); every one-shot timer (config, resize, notice, sheet-close,
  pin-flash, long-press, blink, trick, celebrate, party) is cleared before re-set;
  `sensorTimer` is guarded against duplication (4343). Listeners are attached once
  with delegation (no per-card listeners); pointer listeners are passive. Growth
  maps (`ackOptimistic`, `dismissed`, `pinned`) are pruned/capped every render
  (966-967, 1179-1187, 1315-1325); `prevSessionState` is replaced each poll. No
  unbounded listener or map. Idle CPU is the two ticks doing no-op `setText`s over
  ≤~14 nodes.
- **Prototype-safety** — `filterSessions` matches state via `hasOwnProperty`
  (1216-1221) so a session state of `constructor`/`toString` cannot match every
  bucket; `readPrefs`/`loadPrefs` reject non-object/array stored blobs (1115,
  1149).

---

## 4. Report summary

- **Untrusted-string verdict: NO injection sink — sound.** Zero
  `innerHTML`/`insertAdjacentHTML`/`document.write` in 4931 lines; every feed
  string reaches the DOM through `textContent` or a non-HTML attribute; CSS has no
  `url()`/`attr()`/`expression()`. A title/question/tool with markup renders as
  literal text in QtWebEngine.
- **Top findings:** F1 LOW (cwd-only change on a repo-less card doesn't repaint —
  cosmetic), F2 LOW (older widget overwrites a newer build's filter/density value
  — display-state only, mixed-version), F3 INFORMATIONAL (poll wedge only without
  `AbortController`, not reachable on QtWebEngine). F4 is a cross-lane note for
  lane 5 (write actions carry no caller proof — by-design/unauthenticated).
- No High/Critical. Nothing here should block ship; F1/F2 are one-liners for the
  BACKLOG if the wave wants them.
