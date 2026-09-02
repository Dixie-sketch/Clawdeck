# SideCrab — Elgato Marketplace listing copy

Submission copy for the iCUE widget category on Elgato Marketplace. Field limits and asset
specs are the ones documented by Elgato; every limit quoted here is cited in
`SUBMISSION-CHECKLIST.md`, which also carries the open decisions.

**Refreshed 2026-08-26 against the v0.15.0 panel** (gallery re-shot, description rewritten). The
previous copy described the v0.9.0 product and understated it by roughly half.

> **Three blockers before this copy can be pasted into Maker Console.** The product name, the
> mascot and the phrase *Claude Code* are the subject of an unresolved trademark question (D1);
> the panel-approvals claim in the description has never been exercised against a live CLI
> approval (D12); and shots `01` and `05` carry a v0.15.0 rendering defect in the approval card
> (D15). All three are in *Required decisions* in `SUBMISSION-CHECKLIST.md`. A brand-neutral
> variant of the long description is at the bottom of this file so D1 can be taken late without
> rewriting the listing.

---

## Product name

```
SideCrab
```

8 characters, inside the ≤30 limit. No maker name, no price, no category word, no promotional
language, no emoji, no special characters — all of which the naming rules prohibit.

**Note:** the panel's own on-glass badge still reads **Claw'deck**, not SideCrab, in every one of
the nine gallery shots. The store name and the thing a user sees on the display should be the
same word. Decision D2.

## Tagline

```
An ambient status panel for the coding sessions running on your PC.
```

Marketplace has no separate tagline field today — this is the one-line the listing should open
with and the line to reuse in the repo, the release notes and any social post.

## Short description (the first 250 characters)

The first 250 characters carry the most search weight and must be plain unformatted text. This is
the opening paragraph of the long description, reproduced on its own — **225 characters**, so the
whole SEO-weighted window falls inside one unformatted paragraph with nothing bulleted inside it:

```
SideCrab turns a CORSAIR LCD panel into an ambient status board for the coding sessions running on your PC. A pixel crab reacts to what is happening, so you can read your work from across the room instead of terminal windows.
```

## Long description (the full listing body)

Plain-text opening, then the requirements paragraph the guidelines ask for, then bullets.
**1,435 characters** counted with LF line endings — above the 250 minimum, inside the 1,500
maximum, with 65 characters of headroom. **Pasted with CRLF it counts 1,454**, so the real
headroom is 46; that is the number to plan against, and any edit has to be re-counted in the
console.

```
SideCrab turns a CORSAIR LCD panel into an ambient status board for the coding sessions running on your PC. A pixel crab reacts to what is happening, so you can read your work from across the room instead of terminal windows.

Requires Windows and iCUE 5.44 or newer on a dashboard_lcd device. Claude Code data needs the free SideCrab companion on the same PC; without it the panel is a clock and temperature display needing no setup.

With the companion running:

- Session cards with state, model, elapsed time, activity and context
- Usage-limit gauges with reset countdowns and depletion forecast
- A clear alert, and a colour change, when a session is waiting on you
- Today's burn: sparkline, by-model and per-session splits, optional budget
- A cross-session timeline and a seven-day strip you can tap into
- Tap a waiting session for its question; acknowledge, or queue a next step
- Approve or deny a permission request, with a countdown; off by default
- Filter the grid by state; compact the cards to fit more
- Pin, swipe, two-finger tap and pull to refresh, with a fingertip
- Quiet hours that dim the panel and hold alerts overnight

The panel never invents data: no companion, a stopped one, or a feed over 30 seconds old gives a worried crab and a "data as of" line, never a healthy panel built on stale numbers.

It makes no internet requests. It reads only http://127.0.0.1 on your machine and sends nothing anywhere.
```

⚠ **One line in that body is gated.** *"Approve or deny a permission request, with a countdown;
off by default"* is D12 — the path is written and unit-tested but has never been run against a
live CLI approval. **Delete that bullet, and gallery shot `03-approval.png`, if the listing is
submitted before `setup\Verify-PanelApproval.ps1` has passed.** Removing it takes the body to
**1,362 characters** and breaks nothing else. Note that the *queue a next step* half of the
bullet above it is gated by D12 too — soften it to *"acknowledge it"* under the same condition.

## Feature bullets (if a separate field exists)

- Reads the state of every coding session on the machine at a glance
- A pixel crab whose mood is the status: asleep, working, waiting on you, worried
- Usage-limit gauges with live reset countdowns and a depletion forecast
- Token burn for today: sparkline, by-model split, per-session breakdown, optional daily budget
- A cross-session timeline and a seven-day history strip you can drill into and page through
- Tap a waiting session for its question; acknowledge it, or queue it a canned next step and see
  what is queued on the card until it runs *(the queue half is gated: D12)*
- Approve or deny a permission request without turning around, with a countdown showing how long
  the decision can still be made there — ships off, opt-in *(gated: D12)*
- Filter the grid to the sessions waiting, working or finished; switch between comfortable and
  compact cards to fit more on the glass
- Pin a session to the front, swipe a finished one away, two-finger tap to acknowledge everything,
  pull down to refresh
- Quiet hours, editable from the panel itself
- Runs standalone as a clock and temperature panel with no companion and no setup
- Fails honestly: stale or missing data is shown as such, never faked

## Requirements (state these in the description, and in any Requirements field)

- **iCUE 5.44 or newer**
- A CORSAIR device with a **`dashboard_lcd`** display (the widget is authored for the full-width
  horizontal slot; XENEON EDGE is the reference device — see D10)
- The **Sensors** iCUE plugin, for the temperature row — declared in the widget manifest as a
  required plugin, so iCUE handles it
- **Windows**, for the optional companion service (the widget itself is a widget; the companion
  is a separate free download and is Windows-only)
- The companion additionally needs PowerShell 7 and Python 3.13

Everything past the first two bullets is optional. The widget installs and runs with none of it.

## Disclosure

The long description above already ends with the short form of this — that shortened form is the
one that fits. The full text below is **391 characters and will not also fit** inside the 1,500
limit (1,435 + 391 = 1,826); it belongs on the support/download page the listing links to, and in
the companion's own README. Do not paste both.

```
SideCrab communicates only with http://127.0.0.1 on the computer it is running on — a companion
service that you download and install yourself, and that runs entirely on your machine. The
widget makes no requests to the internet, transmits nothing off the PC, and collects no personal
data. The companion reads local files only, never writes to them, and never logs or transmits
credentials.
```

## Category, tags and listing switches

- **Type:** iCUE widget
- **Device:** the `dashboard_lcd` device family the widget declares
- **Recommended orientation:** horizontal
- **Interactive:** yes — the widget declares `"interactive": true` and the panel is tappable
- **Price:** to be decided (D3). A free listing needs no payout setup and is available worldwide.
- **Suggested search terms for the description body:** status panel, sessions, ambient display,
  developer, dashboard, clock, sensors, token usage, XENEON EDGE

## Support links

- Project homepage / source: **TBD — a public URL is required (D4)**
- Companion download + setup instructions: **TBD — same URL (D4)**
- Support contact: **TBD (D5)**

---

## Appendix — brand-neutral variant

Use this body instead if the naming decision (D1) lands on removing the third-party product name
from the listing. Same structure, same length class; only the naming changes. **1,434 characters**
(1,453 with CRLF). The same D12 gate applies to the approvals bullet.

```
SideCrab turns a CORSAIR LCD panel into an ambient status board for the AI coding sessions running on your PC. A pixel crab reacts to what is happening, so you can read your work from across the room instead of terminal windows.

Requires Windows and iCUE 5.44 or newer on a dashboard_lcd device. Session data needs the free SideCrab companion on the same PC; without it the panel is a clock and temperature display needing no setup.

With the companion running:

- Session cards with state, model, elapsed time, activity and context
- Usage-limit gauges with reset countdowns and depletion forecast
- A clear alert, and a colour change, when a session is waiting on you
- Today's burn: sparkline, by-model and per-session splits, optional budget
- A cross-session timeline and a seven-day strip you can tap into
- Tap a waiting session for its question; acknowledge, or queue a next step
- Approve or deny a permission request, with a countdown; off by default
- Filter the grid by state; compact the cards to fit more
- Pin, swipe, two-finger tap and pull to refresh, with a fingertip
- Quiet hours that dim the panel and hold alerts overnight

The panel never invents data: no companion, a stopped one, or a feed over 30 seconds old gives a worried crab and a "data as of" line, never a healthy panel built on stale numbers.

It makes no internet requests. It reads only http://127.0.0.1 on your machine and sends nothing anywhere.
```

⚠ **This variant does not make the listing brand-neutral on its own.** Gallery shot
`09-standalone.png` carries the on-glass line *"Claude Code stats need the SideCrab companion"*,
rendered by the widget itself — so the brand-neutral path needs a widget string change and a
re-shoot, not just a copy swap. Measured 2026-08-26; it is a new input to D1.

The companion's own documentation still has to name the tool it reads, so this variant moves the
naming question off the store listing rather than answering it.
