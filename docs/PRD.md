# SideCrab — product & design document

*A full-screen Corsair Xeneon Edge widget that turns a desk display into an ambient Claude Code
status panel: rate-limit gauges, live session state, attention alerts, a clock, and the crab.*

This is the companion document to the [README](../README.md). The README tells you what SideCrab is
and how to install it; this one explains what it is *for*, how it is put together, and why it is
built the way it is.

---

## 1. Problem & goal

If you run more than one Claude Code session at a time, the only way to know that a session
finished, stalled, or is **waiting on a question or a permission prompt** is to cycle through
terminal windows. Usage limits (the 5-hour window, the weekly cap) are invisible until you type
`/usage`. Meanwhile the second screen on the desk — in this case a 14.5″, 2560×720 Corsair Xeneon
Edge — is doing decorative duty.

**Goal:** one glance answers three questions at all times.

1. **Does any session need me right now?** — question asked, permission pending, stopped, errored.
2. **How much runway do I have?** — 5-hour and weekly limit gauges, token/cost burn.
3. **What is everything working on?** — session cards: title, repo/branch, model, last activity.

Plus a clock and the crab as the emotional centrepiece. The crab **is** the status summary: if you
learn nothing else about the panel, you learn what a waving crab means.

**Non-goals**

- **Free-text input from the touchscreen.** The panel acts through a fixed vocabulary — acknowledge,
  dismiss, pin, a canned continue prompt, approve/deny — never an arbitrary typed message. Real
  injection into a live session is still blocked (§7).
- Cloud sessions or sessions on other machines. The companion reports the machine it runs on.
- Deep integration with other Claude surfaces — no stable feed has been identified.

---

## 2. Platform facts

- A widget is an HTML/CSS/JS folder (`index.html` + `manifest.json`) rendered by iCUE's
  Chromium-based QtWebEngine, packaged to a `.icuewidget` bundle by the **WidgetBuilder CLI**
  (`icuewidget init/validate/package`) and installed by double-click into a recent iCUE. Device
  target `dashboard_lcd`, full-screen, landscape 2560×720.
- Widgets **cannot read local files.** They can make HTTP requests and consume iCUE data-provider
  plugins (sensors, media, FPS). Everything Claude-specific therefore arrives over localhost HTTP
  from the companion service (§3).
- Because QtWebEngine tracks an older Chromium, the widget is deliberately zero-framework and
  conservative in its CSS, and every font and asset is bundled — it must render fully offline.
- Corsair's widget documentation (spec, plugins, controls) is the reference for the device target
  and the manifest schema.

---

## 3. Architecture — four components, one repo

```
Claude Code                              companion "crabd"              iCUE / Xeneon Edge
───────────                              ─────────────────              ──────────────────
hooks (Notification, Stop,   ──POST──▶   localhost HTTP service  ◀─poll── SideCrab widget
  SessionStart/End, Subagent)            - session state machine         (HTML/JS, 2560×720)
statusline document          ──POST──▶   - limits/usage reader     ──────▶ GET /v1/state (3 s)
OTLP metrics + logs          ──POST──▶   - token/cost aggregator   ◀────── POST /v1/action
Stop + PermissionRequest     ◀─answer─   - history + recap                 POST /v1/config
  (blocking http hooks)                  - control surface
~/.claude usage + transcripts ──read──▶       ▲        ▲
git (per-session cwd)         ──read──▶  poll │        │ poll
                                    sidecrab-glow   notifier
                                   (RGB lighting)  (Windows toast)
```

Both glow and notifier are **standalone and read-only** consumers. Either can be absent without
breaking anything else.

The widget is no longer purely a consumer: since v0.12.0 it also **writes** — settings via
`POST /v1/config`, and per-session actions (acknowledge, dismiss, a queued continue prompt,
approve/deny) via `POST /v1/action`. Everything remains localhost-only.

### 3.1 The widget — `widget/`

Static HTML/CSS/JS. Polls `http://127.0.0.1:2722/v1/state` and renders §4 from a single JSON
document — everything in one round trip, so there is exactly one thing that can be stale and
exactly one place staleness is judged.

**The widget must be useful on its own.** Someone who installs it from the store and never runs the
companion still gets the clock, the crab, and whatever iCUE's own sensor providers expose. The
companion-fed zones degrade to an honest "no companion" state rather than an empty panel.

### 3.2 The companion — `companion/` (crabd)

A small local service on the machine running Claude Code. It:

- **Receives hooks.** Claude Code hooks (`Notification`, `Stop`, `SubagentStop`, `SessionStart`,
  `SessionEnd`, `UserPromptSubmit`) POST one-line JSON events, which drive a per-session state
  machine: `working / needs_input / done / idle / gone`.
- **Reads limits and usage** from two sources with explicit provenance. The chained **statusline**
  command posts Claude Code's own session document (`limits.source: "statusline"`); the OAuth
  usage endpoint is the fallback when no statusline document has arrived in 10 minutes. Plus
  per-session and per-day token aggregation from the local transcript JSONL, and an optional
  **OTLP receiver** for the built-in telemetry, which is the only way `burn.costUSD` is ever
  populated — it is never derived.
- **Projects depletion.** A linear `exhaustAt` per limit window from the utilization delta over
  recent readings, `null` when flat, declining, or thin on data, and never extrapolated past the
  window's own reset. Hedged in the UI ("~full by 3:40 PM"), never presented as certainty.
- **Enriches sessions.** Title, cwd → repo and branch (a local `git` read, never a network call),
  model and speed from transcript metadata, last-activity age, context-window size, and a
  `titleSource` so a weakly-derived title can be rendered as such.
- **Persists history** to `~/.sidecrab/history.jsonl` — event kind, session id, title, timestamp;
  no secrets and no question text — replayed at startup so the daily recap and the week strip
  survive restarts. Served back by `GET /v1/history?day=`.
- **Answers blocking hooks.** `Stop` (to deliver a queued continue prompt) and `PermissionRequest`
  (to let the panel approve or deny) are `type-http` hooks that crabd answers directly. See §4.7.
- **Watches its own fleet** — the Scheduled Task state of the glow and the notifier.
- **Serves `/v1/state`** with a monotonic `generatedAt`, and accepts `POST /v1/config` on a
  strict key whitelist. Binds `127.0.0.1` only.

It reads `~/.claude` strictly read-only and never logs, serves or persists the OAuth token.

### 3.3 The glow — `lighting/`

An optional consumer that pulses Corsair RGB terracotta while a session is unacknowledged and
waiting, and hands the lights straight back to your own iCUE profile the moment the alert clears.
Quiet hours, acknowledgement, and a dead feed all release the lights. The decision is a pure
function, so it is fully tested without a Corsair device in the room.

**Parked, and honestly so.** The Corsair SDK crashes in every non-interactive console context
tested — `pythonw`, a hidden child process, and a hidden new console all fault at handshake; only
an interactive console degrades gracefully. The `SideCrab-glow` Scheduled Task is therefore
**disabled on purpose**, the installer no longer overturns that on re-run, and the panel's fleet
dot shows it stopped rather than pretending otherwise. It needs a newer SDK, a visible tray-mode
process, or a Corsair fix.

### 3.4 The notifier — `notifier/`

An optional consumer that raises native Windows toasts when the panel is out of view. Five of
them, each deduped so it cannot repeat: a session **waiting** past a threshold (with an
Acknowledge button), a **permission request** left undecided, a daily **digest**, a **token
budget** crossing, and — the only one about SideCrab itself — the **companion going quiet**.

Two rules run through all five. Quiet hours **suppress and mark** rather than defer, so nothing
bursts at 07:00 on an ordinary morning. And the approval toast deliberately carries **no
Approve/Deny buttons**: a toast action is one click from a lock screen, or from a notification
the shell replays hours later, which is an acceptable cost for acknowledging a dot and not for
allowing a tool call. It says "Decide on the panel."

*Historical note:* an integration with a private internal operations dashboard existed before
publication and was removed for release. Nothing in SideCrab reads any data source other than the
local Claude Code state and the companion feed.

---

## 4. UI specification (2560×720, dark)

Three zones, left to right.

**4.1 Identity zone (~520 px, left).** The crab, large, centred — the panel's status face:
**content/idle** (everything working, or nothing active) · **waving/alert** (a session needs input)
· **asleep** (quiet hours) · **worried** (feed stale or companion down). Below it, the clock: big
HH:MM, subtle seconds, a date line, and two small labelled **fleet dots** (`g`, `t`) reporting
whether the glow and the notifier are actually running — green running, amber stopped, grey
absent or unknown. Colour is never the only carrier: the letter and the dot shape say it too. crabd
has no dot, because if the panel is rendering at all, crabd is up. The crab and clock are what make
the widget worth the screen even with zero sessions.

**4.2 Limits and burn zone (~620 px).** Two horizontal gauges — **5-hour window** and **weekly
limit** — each with percent used, resets-at time, a colour ramp (calm → amber ≥70% → red ≥90%), a
muted provenance label saying whether the number came from the statusline or OAuth, and a hedged
**depletion line** ("~full by 3:40 PM") when a projection exists and lands before the window
resets. Under them, today's token figures, an optional cost line, and a 24-hour burn sparkline
carrying an optional **budget marker** and a "budget 34%" line. Parity with what Claude Code itself
reports is the requirement.

**4.3 Session grid (~1420 px, right).** A card per live or recent session, sorted **needs-input
first**, then working, then recently-done — with **pinned** sessions first inside their band.
Each card carries a state colour edge, the session title (rendered muted-italic when it was only
derived from the working directory), repo`@`branch, model and speed, a context-size chip,
last-activity age, and a one-line last event. Subagents show as a badge on the parent; a queued
continue prompt shows on the card that queued it. Comfortable at 6–8 cards; overflow collapses
oldest-idle into a "+N idle" chip. A finished card lingers about ten minutes, then fades unless the
session comes back.

Tapping a card opens its **detail sheet** — pin/unpin, acknowledge, dismiss, a canned continue
prompt, or (when a permission request is live) Approve and Deny. Tapping a day in the week strip
drills into that day's history.

**4.4 Attention behaviour — loud, but never irreversible.** When a session enters *needs-input*:
the panel edge glows, the crab waves, the card pulses at the top of the grid, and (optionally) the
panel flashes once on the transition. A live permission request additionally colours the card and
the panel edge, because it is a hard stop rather than a question that can wait. Everything settles
into a steady indication — nothing blinks forever. Quiet hours suppress the flash, the glow and the
pulse; the card still renders, because a question keeps waiting whether or not you want to be
shouted at about it.

**4.5 Touch.** Four gestures, arbitrated through one pointer map because they compete for the same
finger: **tap** a card for its sheet, **swipe** a card to acknowledge or dismiss it, **long-press**
to pin, **pull down** to refresh, and a **two-finger tap** on the crab to acknowledge everything at
once (deliberately skipping any card holding a permission request — that is a decision, not a dot).
Destructive-looking gestures confirm inline rather than acting on the first contact.

**4.6 The control surface.** The panel is also where SideCrab is configured: quiet hours, toast
thresholds, the digest time, the daily token budget, and whether panel approvals are on at all.
Writes go to `POST /v1/config` on a strict per-key whitelist — *exact*, not
ignore-what-you-don't-know, so nothing that can reach localhost can flip a key by naming it. An
older companion that rejects one key must leave the others working, so the widget treats a
rejection as *this key is unsupported here*, never as a dead feed. The prompt vocabulary and the
recap repo list stay file-only: the panel reads them and does not write them.

**4.7 Approvals are opt-in and fail safe.** When enabled, a `PermissionRequest` is held for up to
55 seconds while the panel offers Approve and Deny. Three properties are non-negotiable, and they
are what make the feature acceptable at all:

- **Nothing is ever auto-allowed.** There is no code path that answers "allow" without a tap having
  landed first.
- **The pass-through is the default.** A timeout, a disabled setting, an unrecognised body, or no
  tap all return no decision — which is exactly the behaviour of a machine where SideCrab was never
  installed. The terminal dialog does its normal job.
- **It ships off.** The installer asks; the operator opts in.

**4.8 Failure states are honest.** This is the design rule the whole project is organised around.

- Companion unreachable, or `generatedAt` older than 30 s → the panel dims, the crab looks worried,
  a banner reads "data as of HH:MM".
- Limits unknown → gauges show an em-dash. **Never 0%.**
- A number that cannot be computed is `null` and renders as absent. Nothing is inflated, and
  nothing stale is silently re-served as if it were current.

**Silence must never render as all-green.** A panel that looks healthy has to mean the data is
actually fresh, or the panel is worse than no panel at all. The notifier extends the same rule off
the glass: it toasts when the companion goes quiet while you were working, because a dead panel and
a calm one look identical from across the room.

**4.9 Visual language.** Dark terminal aesthetic: near-black background, a single warm terracotta
accent family, warm off-white text, generous spacing, monospace for numbers. The crab art is
original.

---

## 5. Process — contract first

`widget/` and `companion/` are a producer and a consumer that ship separately: the widget is
imported into iCUE by hand (or installed from the store), the companion is updated by pulling the
repo. They therefore cannot assume they are the same version, ever.

[`STATE-CONTRACT.md`](STATE-CONTRACT.md) is the contract between them, and it is the source of
truth for both sides. **A change lands there first, then in both implementations.** The rules that
fall out of that:

- **`schema` marks the last BREAKING shape**, not the last release. Additive fields are detected by
  **field presence**, never by a schema number — so the companion can ship new fields at any time
  and the widget lights them up whenever it is next updated.
- A schema number above the consumer's ceiling is treated as a dead feed. That is what a real
  break should look like.
- Unknown top-level and per-session keys are always ignored.
- Every consumer states the schema range it accepts, and there is a test that reads the live
  numbers out of the contract document — so a bump cannot go silent. (This is not hypothetical: a
  consumer once kept running, kept polling, and quietly never notified again after a bump. "The
  process is running" is not a test that a feature works.)

---

## 6. Repo layout & delivery

| Path | What |
|---|---|
| `widget/` | The iCUE widget — HTML/CSS/JS, packaged with the WidgetBuilder CLI |
| `companion/` | crabd — the local service and the `/v1/state` feed |
| `lighting/` | sidecrab-glow — optional RGB alert |
| `notifier/` | Optional Windows toast notifier + Acknowledge handler |
| `hooks/` | The Claude Code hook fragment and the chained statusline command that feed crabd |
| `setup/` | Install/update/uninstall/smoke-test/verification scripts |
| `docs/` | This document, the state contract, the backlog, spikes |

`icuewidget validate` passing is the merge gate for widget changes; releases attach the packaged
`.icuewidget`. Install is one script: register the Scheduled Tasks, merge the hook entries, import
the widget into iCUE.

---

## 7. Roadmap

| Stage | Content |
|---|---|
| **Shipped — the panel** | Widget on glass, all zones live; crabd with hooks, limits, burn, forecast, recap, history and fleet; notifier with five toasts; honest-failure behaviour verified on device |
| **Shipped — the control surface** | Settings from the panel; pins, the day drill and four touch gestures; queued continue prompts; panel approvals (default off); statusline ingest and the OTLP receiver, both with explicit provenance |
| **Needs a live turn, not more code** | Panel approvals have never been exercised against a real CLI approval — the response shape was settled by reading the shipped binary's schema, and `setup\Verify-PanelApproval.ps1` carries the procedure. Until that runs with the operator present, approvals stay off by default and the feature is "written, not proven" |
| **Verify before relying on** | The statusline path works when invoked but **this host never invokes it** — the status line appears to render only in an interactive terminal, so OAuth stays the live source here. It is a fallback-grade feed until confirmed on a plain terminal session, and the OAuth path stays regardless |
| **Parked** | The glow, on the Corsair SDK's non-interactive crash (§3.3) · real tap-to-reply, on a supported external send into a live session — the spike found none that is safe, and `POST /v1/action` answers `501` for `reply` until that changes |
| **Next** | Standalone-widget polish for store users who never install the companion; making the Python suite pytest-clean before publication; packaging and update ergonomics |
| **Watch list** | Cloud sessions (no stable endpoint) · other machines · marketplace distribution |

---

## 8. Risks & mitigations

- **The usage/limit source can shift** between Claude Code versions. Isolated in one companion
  module; degrades to burn-only with the gauges em-dashed, never to invented numbers.
- **Hooks are best-effort** — a killed terminal fires nothing. The companion also ages sessions by
  transcript mtime, so a session cannot sit "working" forever because a hook never arrived.
- **QtWebEngine quirks.** Zero-framework, conservative CSS, tested on the real device early rather
  than late.
- **Widget sandbox limits on localhost fetch** were verified with a spike before the full widget
  was built; the documented plugin/data-provider path is the fallback.
- **Corsair SDK coverage varies by device.** Some lighting (notably case RGB behind a hub) is not
  exposed as addressable. The glow treats "connected but nothing to pulse" as its own degraded
  state and re-enumerates, rather than pretending it painted something. Separately, the SDK does
  not survive a non-interactive console at all, which is why the glow is parked (§3.3).
- **Blocking hooks sit in Claude Code's critical path.** `Stop` and `PermissionRequest` are
  answered by crabd, so a companion that hangs would stall a turn. Both are bounded well inside the
  CLI's own timeout, and every failure mode — unreachable, malformed, disabled, timed out —
  resolves to the same pass-through the CLI would take on its own. A hook that cannot decide must
  never be a hook that blocks.
- **The installer must not overturn the operator's own state.** A `-Force` re-registration writes
  an enabled task, which once resurrected the deliberately-disabled glow straight back into its
  crash. Re-registration now reads the prior state first and restores what the installer does not
  own; an explicit switch is the only way to overturn it. "Idempotent" has to include the human's
  edits, not just the file's contents.
