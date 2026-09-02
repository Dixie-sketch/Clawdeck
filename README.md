# SideCrab 🦀

**An ambient Claude Code status panel for the Corsair Xeneon Edge.**

A full-screen 2560×720 iCUE widget — plus an optional local companion service — that turns the
desk display into a live view of every Claude Code session on your machine: rate-limit gauges,
session cards, needs-your-attention alerts, token burn, a clock, and a crab whose mood *is* the
status.

If a session asks you a question, you find out from across the room instead of by cycling through
terminal windows. Then you can answer it from the panel.

```
Claude Code hooks ──POST──▶  crabd (127.0.0.1:2722)  ◀──poll── SideCrab widget (iCUE / Xeneon Edge)
statusline + OTLP  ──POST──▶ one /v1/state JSON feed  ──write─▶ /v1/action · /v1/config
~/.claude usage + JSONL ──▶  + blocking hook answers  ◀──poll── glow (RGB) · notifier (toasts)
```

---

## Two ways to run it

**Widget only.** Install the widget, run nothing else. You get the crab, the clock, and whatever
your iCUE sensor providers expose. It is a good-looking panel that needs no setup and no
background process.

**Widget + companion.** Run `crabd` on the machine where you use Claude Code and the panel comes
alive: live session cards, limit gauges with a depletion forecast, burn history and an optional
daily token budget, a daily recap with a drillable week strip, and alerts. Everything is
localhost-only — the companion reads `~/.claude` strictly read-only, never writes to it, and never
logs or transmits your OAuth token.

The widget always degrades honestly. No companion, a stopped companion, or a feed older than 30
seconds all produce a worried crab and a "data as of HH:MM" banner — never a green-looking panel
built out of stale numbers.

---

## What you can do from the glass

The panel started glance-only and is now a control surface. It never accepts free text — the
vocabulary is fixed, and everything goes to the companion on localhost.

- **Tap** a card for its detail sheet · **swipe** to acknowledge or dismiss · **long-press** to pin
  a session to the top · **pull down** to refresh · **two-finger tap** the crab to acknowledge
  everything at once.
- **Send a canned continue prompt** ("Continue", "Run the tests", "Commit + push", plus your own)
  to a session that has stopped. It is delivered the next time that session's Stop hook fires.
- **Approve or deny a permission request** without turning around — off by default, and see the
  caveat below before you turn it on.
- **Change the settings**: quiet hours, toast thresholds, digest time, token budget, and the
  approvals switch. (`continuePrompts` and `recapRepos` are hand-edited in the file — the panel
  reads them but does not write them.)
- **Drill into a day** by tapping it in the week strip.

## Components

| Path | What |
|---|---|
| `widget/` | The iCUE widget — HTML/CSS/JS, packaged to `.icuewidget` with Corsair's WidgetBuilder CLI |
| `companion/` | **crabd** — the local service: hook receiver, session state machine, limits + burn reader, history, `/v1/state` |
| `lighting/` | **sidecrab-glow** — optional: pulses Corsair RGB while a session is waiting on you (currently parked — see below) |
| `notifier/` | Optional: native Windows toasts — waiting session, permission request, daily digest, budget crossed, companion gone quiet |
| `hooks/` | The Claude Code hook fragment and the chained statusline command that feed crabd |
| `setup/` | Install / update / uninstall / smoke-test / verification scripts |
| `docs/` | [PRD](docs/PRD.md) — what and why · [STATE-CONTRACT](docs/STATE-CONTRACT.md) — the producer/consumer API · [BACKLOG](docs/BACKLOG.md) |

`lighting/` and `notifier/` are independent read-only consumers of the same feed. Either can be
absent; neither is required by anything else.

---

## Install

### 1. The widget

SideCrab is not in the iCUE widget store yet. Download the packaged `.icuewidget` from the
[releases page](https://github.com/Dixie-sketch/Clawdeck/releases), or build it yourself:

```powershell
icuewidget validate widget
icuewidget package  widget
```

Then import the `.icuewidget` into iCUE and place it full-screen on the Xeneon Edge. Requires
iCUE 5.44 or newer and a `dashboard_lcd` device; double-clicking the package to import needs iCUE
5.46.67 or newer (older iCUE imports the file from within the app).

### 2. The companion (optional, but it's the point)

Requirements:

- Windows 10 or 11, with iCUE 5.44 or newer and a `dashboard_lcd` device (the Xeneon Edge)
- PowerShell 7 (`pwsh`)
- Python 3.13 on `PATH` as a real install. The Microsoft Store alias stub is rejected by the
  installer, because it cannot host a background service.
- `curl.exe` (ships with Windows since 10 1803) - the hook fragment uses it

```powershell
git clone https://github.com/Dixie-sketch/Clawdeck.git C:\Dev\sidecrab
cd C:\Dev\sidecrab
pwsh -File .\setup\Install-SideCrab.ps1 -WithGlow -WithToast
```

That script:

- registers logon Scheduled Tasks for crabd and any optional components you asked for,
- merges the SideCrab hook entries into `~/.claude/settings.json` (backed up first, matched on the
  crabd URL, so re-running never duplicates and other hooks are left alone),
- registers the toast identity and the `sidecrab-ack:` protocol handler when the notifier is
  installed — both `HKCU`, no elevation.

`-WithGlow` / `-WithToast` are optional; with no switches, an optional component is installed when
its script is present. Then check it:

```powershell
pwsh -File .\setup\Install-SideCrab.ps1 -Status   # read-only status of every piece
pwsh -File .\setup\Test-SideCrab.ps1              # end-to-end smoke test, PASS/FAIL table
```

`setup\Update-SideCrab.ps1` and `setup\Uninstall-SideCrab.ps1` do what they say; the uninstaller
removes the tasks, the hook entries and both registry keys.

### Configuration

`~/.sidecrab/config.json` — all keys optional:

```jsonc
{
  "quietHours": { "start": "22:00", "end": "07:00" },  // dim panel, no glow, no toasts
  "toast":  { "enabled": true, "thresholdSec": 120, "approvalThresholdSec": 20 },
  "digest": { "enabled": false, "time": "09:00" },     // one "yesterday" toast per day
  "budget": { "dailyOutputTokens": 5000000 },          // null to clear; one toast on crossing
  "continuePrompts": ["Continue", "Run the tests"],    // extra taps on a stopped session
  "panelApprovals": { "enabled": false },              // approve/deny from the panel — see below
  "recapRepos": ["C:\\Dev\\sidecrab"]                  // extra repos to count commits in
}
```

Most of these are editable from the widget itself; the file is there for the ones that are not,
and for scripting.

---

## Before you turn on panel approvals

Approving a tool call from a wall-mounted touchscreen is a real security decision, so the
guarantees are worth reading rather than assuming:

- **It ships off.** The installer asks.
- **crabd never decides on its own.** There is no code path that answers "allow" without a
  `decide` request arriving on localhost first. The normal source of that request is a tap on
  the panel.
- **Known residual - SEC-a, open.** crabd cannot tell the widget's opaque `null` Origin from a
  forged one, so while approvals are ON, a sandboxed iframe on a web page you visit, or any
  process running on this PC, could send that `decide` for a pending request you never tapped.
  The queue-continue path is bounded by a fixed prompt whitelist; the approval path is not.
  Until this is closed, leave approvals off on a machine where you browse the web while a
  session sits on a pending permission. Tracked in [`docs/BACKLOG.md`](docs/BACKLOG.md) (SEC-a,
  WID-a) and disclosed in [`SECURITY.md`](SECURITY.md).
- **Every failure is a pass-through.** Timeout, no tap, disabled, malformed, companion down — all
  return no decision, and the normal terminal dialog does its job. The worst case is the behaviour
  of a machine where SideCrab was never installed.
- **The toast has no buttons.** When a request goes undecided, the notifier tells you — and says
  "Decide on the panel." A notification action is one click from a lock screen, or from a toast the
  shell replays hours later; that is fine for acknowledging a dot and not for allowing a command.
- **Verified live, operator present (2026-08-27)** via `setup\Verify-PanelApproval.ps1`: a panel
  Approve ran the command with no keyboard, a panel Deny blocked it, and a full minute of
  ignoring both surfaces ended in the 55-second pass-through with the terminal dialog fully in
  charge — an ignored or dead panel can never block a session. Two behaviours worth knowing
  before you rely on it: the terminal dialog is RACED, not suppressed (it renders immediately;
  whichever surface answers first wins), and the two-button card carries a real mis-tap risk —
  during the verification the operator tapped Approve intending Deny, which triggered a bench
  investigation proving the buttons themselves are wired correctly. Test your own setup with the
  same script before trusting it on a new machine.

---

## Known caveats

- **The glow is parked.** The Corsair SDK crashes in every non-interactive console context tested,
  so the `SideCrab-glow` task ships disabled on purpose and the panel's fleet dot honestly shows it
  stopped. The installer no longer re-enables it on a re-run. It needs a newer SDK, a visible
  tray-mode process, or a Corsair fix.
- **The statusline feed is a fallback, not a replacement.** Wired and working when invoked, but the
  status line appears to render only in an interactive terminal session — on an app-hosted one it
  never fires, and `limits.source` stays `"oauth"`. The OAuth path stays regardless.
- **Cost figures need telemetry.** `burn.costUSD` is populated only when Claude Code's OTLP
  telemetry is flowing to the companion. It is never derived from token counts — no number, no line.

---

## Known issues

The honest list lives in [`docs/BACKLOG.md`](docs/BACKLOG.md). The ones worth knowing before you
install:

- **SEC-a / WID-a** - the panel-approval residuals above. Approvals ship off.
- **GHOST-a** - after a crabd restart, a session that was killed by an app restart (no `SessionEnd`
  hook fires) can read `working` for up to 15 minutes before transcript aging retires it. Taps
  on such a row are refused with 409 rather than queued into the void.
- **Glow is parked** (see caveats) and the **status-line feed only fires in an interactive
  terminal**.
- About two dozen small, known, cosmetic or edge-case items under "Small, known, not yet fixed".

## Security

Localhost-only by design; the companion reads `~/.claude` read-only and transmits nothing.
[`SECURITY.md`](SECURITY.md) has the threat model, the disclosed residuals, and how to report a
vulnerability.

---

## Design rules

Two things drive most of the decisions in this repo, and both are worth knowing before you send a
patch:

**Honest failure.** Unknown is `null`, or `available: false`, or an em-dash — never `0`, never a
stale value silently re-served. A panel that looks healthy must mean the data is fresh; when the
companion goes quiet while you were working, the notifier says so, because a dead panel and a calm
one look identical from across the room. Quiet hours suppress alerts rather than queueing them, so
nothing bursts at 07:00 on a perfectly ordinary morning.

**Every alert has to survive a healthy night.** A control that pages when nothing is wrong trains
you to ignore the one that matters — so each threshold here is answered by a replay against real
data, and every gate is mutation-proven by breaking it and watching the suite fail.

**Contract first.** The widget and the companion ship separately and are never guaranteed to be the
same version, so [`docs/STATE-CONTRACT.md`](docs/STATE-CONTRACT.md) is the source of truth for both.
`schema` marks the last *breaking* shape; additive fields are detected by presence, never by
version number. A change lands in the contract first, then in both sides.

**A fixed vocabulary, never free text.** The panel can acknowledge, dismiss, pin, send one of a
configured set of prompts, and approve or deny. Injecting arbitrary text into a live session has no
supported mechanism — the spike found none that is safe — so `POST /v1/action` answers `501` for
`reply` and will keep doing so until one exists. See the [PRD](docs/PRD.md) roadmap.

---

## Tests

```powershell
python -m unittest discover -s companion\tests -t companion\tests
python -m unittest discover -s notifier\tests  -t notifier\tests
python -m unittest discover lighting\tests
pwsh -File .\setup\tests\RunTests.ps1
```

All headless: the Corsair SDK sits behind an adapter, toast emission sits behind an adapter, and no
test posts to a live crabd or depends on the wall clock.

---

## License

MIT. See [`LICENSE`](LICENSE). SideCrab is an independent hobby project and is not affiliated with
or endorsed by Anthropic or Corsair; *Claude* and *Claude Code* are Anthropic's marks and *iCUE* and
*Xeneon* are Corsair's, named here only to say what the panel works with.
