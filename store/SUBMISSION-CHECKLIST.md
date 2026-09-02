# SideCrab — Elgato Marketplace submission checklist

What the store actually requires, what this kit already satisfies, and what only the owner can
decide. Store rules researched 2026-08-26 against Elgato's maker documentation and the CORSAIR
widget article; every rule below is sourced at the bottom.

**Nothing in this file has been submitted.** No account was created, no agreement signed, no
product uploaded. This is the preparation, not the act.

**Refreshed 2026-08-26 against the v0.15.0 panel** (`widget/manifest.json` read at 23:15, after
the 0.15.0 wave landed). The gallery was re-shot from scratch — four shots → **nine** — and the
decisions list re-examined. The store rules in §1 are unchanged; §2, §3 and §4 are new. The one
thing that did not move is D1, which is still the blocker.

⚠ **This gallery is pinned to a build.** The first pass of it was shot against v0.14.0 and was
obsolete within twenty minutes: v0.15.0 added a session filter chip, a card-density chip, an
approval decision countdown and a "queued: …" line on the card, all of which are visible in the
top-right and in the grid. Before uploading anything, **check `widget/manifest.json`'s version
against the 0.15.0 this gallery records** and re-shoot if it has moved.

---

## 1. What the store requires

### 1.1 Account and agreement — before anything else

| Step | Detail |
|---|---|
| Sign in to Maker Console | `maker.elgato.com` |
| Create an organization | This is the public maker identity shown on the listing |
| Sign the Maker Agreement | **Mandatory before a product can be submitted for review** |
| Payout setup | Stripe Connect; 70/30 split in the maker's favour. **Not needed for a free listing**, which is also the only option available worldwide — paid products cannot be listed in Cuba, Iran, North Korea or Syria |

### 1.2 Listing fields and their hard limits

| Field | Limit / rule |
|---|---|
| Name | ≤30 characters, unique, English, first letter and proper nouns capitalised. **No** maker name, price, category word, promotional language, special characters, emoji, `+`, or multiple `!` |
| Description | **Minimum 250** characters, **maximum 1,500**. The first 250 characters carry the most search weight and must be plain unformatted text. Must state features and product requirements. No random keyword stuffing, no filler, no inaccurate AI-written text, no external paywall |
| Type / category | Set at creation |
| Supported languages | Listing field |
| Support links | Listing field, editable later |
| Price | Free or paid |

⚠ **Name and monetization cannot be changed later in Maker Console.** Both are one-shot choices
at creation. Everything else — description, category, media, support links — is editable.

### 1.3 Media specs

| Asset | Spec | Count |
|---|---|---|
| Thumbnail | **1920 × 960 px PNG** | 1 |
| Gallery image | **1920 × 960 px PNG** | **minimum 3, maximum 10** |
| Gallery video | 1920 × 1080 MP4, under 250 MB | counts toward the 10 |
| App icon | 288 × 288 px PNG | documented as **plugins only** — see decision D8 |

Media must accurately depict the product and its functionality, with legible text, English text
only, accurate depiction of Elgato/CORSAIR devices, and no unlicensed imagery or low-quality
screenshots.

⚠ **Gallery items cannot be re-ordered after upload.** Upload them in the order you want them
shown; the numeric order in `shots/marketplace/` is the intended one.

### 1.4 Review

- Submitting sets the status to **Pending review**.
- **4–10 business days** during busy periods.
- Rejection comes with feedback; you then resubmit either as a **new version** or as a
  **revision of the rejected version**.
- A new version needs the **product file** and **release notes**.
- Only the **most recent approved version** is available to users.
- Auto-publish on approval is a checkbox. Uncheck it if you want to time the launch, then press
  **Release** yourself.

### 1.5 The guidelines that a widget like this one can actually trip on

- **You must own the rights to everything you post** — name, description, imagery, files.
  Products may not infringe trademarks or impersonate existing offerings. *(This is the one that
  makes D1 a blocker, not a nicety.)*
- **Media must accurately depict functionality.** This is the rule §2.3 exists to answer. Every
  shot here is the real widget rendering real code paths from its own fixtures — but "the code
  path renders" and "the feature works on a user's desk" are different claims, and four of the
  things visible in this gallery have never been observed on hardware.
- **Products too similar to existing offerings without unique functionality may not be accepted.**
  Browse the iCUE widget category first; a clock-plus-crab has neighbours, the session board does
  not.
- **Safety and device integrity** — no overheating, no unauthorized shutdowns, no harmful
  software. Not a risk for an HTML panel, but the companion is a background service that the
  listing points users at, and a reviewer may look at it. The panel-approvals feature (D12) is
  the part of it a security-minded reviewer will read closely.
- **Security and privacy** — data collection needs explicit consent, and anything collecting
  personal data needs a privacy policy link and a deletion mechanism. SideCrab collects nothing
  and transmits nothing; say so plainly rather than staying silent (D6).
- **External paywalls may not gate core functionality.** The companion must stay free and
  publicly downloadable. It is, but the download has to exist at a public URL (D4).
- **Test thoroughly for errors, bugs and crashes**, and keep the description, media and contact
  details accurate and complete.
- Elgato **reserves the right to re-review at any time** and to pull or require changes to a
  product already published.

---

## 2. What is in this kit

| File | What it is |
|---|---|
| `LISTING.md` | Name, tagline, short + long description (both counted against the limits), feature bullets, requirements, disclosure, category switches |
| `shots/*.png` | **Nine** gallery shots at native **2560 × 720**, straight out of headless Edge against the widget's own generic mock fixtures |
| `shots/marketplace/*.png` | The same nine at the required **1920 × 960 PNG**, panel scaled to 1920 × 540 and centred on the panel's own `rgb(15,14,13)` background |
| `SUBMISSION-CHECKLIST.md` | This file |

### 2.1 The shot list

Nine items, inside the 3–10 range. Shot **01 is also the thumbnail candidate** (see D9). The order
below is the intended upload order and **cannot be changed afterwards**.

| # | File | Source | What it shows |
|---|---|---|---|
| 01 | `01-panel.png` | `?mock=rework` | **The hero.** The whole panel working at once: crab, clock, date, fleet dots, three usage gauges (44 / 79 / 63 %) with reset countdowns and the `~full by 23:45` depletion forecast, the `official` provenance tag, today's totals, `budget 32%`, `$12.47 today`, the 24 h sparkline with its pace marker, the `All` / `Comfortable` chips, and six session cards across four states — including a permission-request card with its `43s to decide` countdown, a `queued: Run the tests` line, ctx chips, model chips, subagent counts and a `quiet 3m` hung hint. Panel-wide red alert glow on. ⚠ **Gated by D15** |
| 02 | `02-waiting.png` | `?mock=attention&sheet=<id>&age=20` | **The point of the product.** A session waiting on you, its question open in the sheet whole, with Acknowledge, the three canned continue prompts and Pin session. The grid behind shows the 20-minute escalation chips |
| 03 | `03-approval.png` | `?mock=rework&approval=1&hold=45` | **The decision moment.** A permission request open: the tool named, the whole summary, `34s to decide`, Deny left and `Approve Bash` right, the session's recent events beneath, and the panel-wide red glow behind. ⚠ **Gated by D12** |
| 04 | `04-burn.png` | `?mock=recap&burn=1` | Today's spend opened up: per-session breakdown with model tags, the by-model split as bars, the honest "includes subagent and ended-session spend not listed above" line, and today's commit counts |
| 05 | `05-budget.png` | `?mock=rework&budget=110&spark=7d` | The daily budget **over** its target (`budget 110% — over`, amber) with the 7-day burn strip and its budget line, beside the gauges still carrying their forecast. The over-budget state and the week view in one frame. ⚠ **Gated by D15** |
| 06 | `06-history.png` | `?mock=rework&day=2026-08-24` | The day drill: an earlier day's history newest-first, with previous / next / back / close, and the honest `12 events` count line |
| 07 | `07-compact.png` | `?mock=hot&density=compact` | The v0.15.0 density switch, `Compact` chip lit — and the only shot with the **red** limit step: 97 % / 81 % / 62 %, all three ramp colours in one frame, with the amber panel glow of a waiting session |
| 08 | `08-quiet.png` | `?mock=quiet` | Quiet hours: the whole panel dimmed, `quiet until 07:00` under the clock, an already-`ACKED` waiting session, no alert glow, nothing pulsing |
| 09 | `09-standalone.png` | no query string, isolated copy on a dead port | Day one with no companion installed: crab, clock, date, and one calm line explaining where the rest comes from. ⚠ **Under-depicts — see D13** |

**Overflow and clipping: all nine inspected at native 2560 × 720 and again after the downscale.**
No page or zone overflow, no clipped control, no truncated label — **with one exception, `01` and
`05`, which carry the v0.15.0 approval-card defect recorded as D15.** Three further frames were
rejected during selection for clipping or for depicting nothing legible, and the reasons are in
§2.2 so the same frames are not re-chosen.

### 2.2 How the shots were taken (repeatable), and the five traps

Serve `widget/` **read-only** on a fresh port and shoot with headless Edge at native size.
`--force-prefers-no-reduced-motion` is not optional: **headless Chromium reports
`prefers-reduced-motion: reduce` by default**, so without it every shot is a reduced-motion shot
of a widget that holds still.

```powershell
# terminal 1 — read-only static server on a fresh port
cd C:\Dev\sidecrab\widget
python -m http.server 8797 --bind 127.0.0.1
```

```powershell
# terminal 2 — one shot per fixture. ONE warm profile, reused: see trap 1.
$edge  = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
$shots = 'C:\Dev\sidecrab\store\shots'
$b     = 'http://127.0.0.1:8797/index.html'

function Get-Shot($out, $url) {
    Start-Process -FilePath $edge -Wait -NoNewWindow -ArgumentList @(
        '--headless', '--disable-gpu', '--hide-scrollbars',
        '--force-prefers-no-reduced-motion',
        '--window-size=2560,720', '--virtual-time-budget=12000',
        "--user-data-dir=$env:TEMP\sc-shots",
        "--screenshot=$out", $url)
}

$wait = 'b21c9a10-1f77-4c33-9b02-77d1e5a30002'   # attention's question-carrying session
Get-Shot "$env:TEMP\warm.png"     "$b`?mock=rework"          # discard: warms the profile
Get-Shot "$shots\01-panel.png"    "$b`?mock=rework"
Get-Shot "$shots\02-waiting.png"  "$b`?mock=attention&sheet=$wait&age=20"
Get-Shot "$shots\03-approval.png" "$b`?mock=rework&approval=1&hold=45"
Get-Shot "$shots\04-burn.png"     "$b`?mock=recap&burn=1"
Get-Shot "$shots\05-budget.png"   "$b`?mock=rework&budget=110&spark=7d"
Get-Shot "$shots\06-history.png"  "$b`?mock=rework&day=2026-08-24"
Get-Shot "$shots\07-compact.png"  "$b`?mock=hot&density=compact"
Get-Shot "$shots\08-quiet.png"    "$b`?mock=quiet"
```

**Trap 1 — the boot frame.** A shot taken on a *cold* `--user-data-dir` can snapshot before the
first fetch resolves, producing a perfectly valid-looking panel of em-dashes and empty zones. It
happened twice in this pass, silently, with no error and a plausible PNG. The remedies are a
12,000 ms virtual-time budget, re-using a warm profile, and **looking at every shot** — a
suspiciously small PNG is the tell (28 KB versus 170 KB for the same URL).

**Trap 2 — `&sheet=` and `&pin=` match by id PREFIX, not suffix.** Passing the last six characters
of a session id opens nothing, silently, and you get an unremarkable grid shot instead of the
sheet you asked for. Pass the whole id.

**Trap 3 — the card question preview can clip MID-WORD with no ellipsis.** `.card-question` is
`-webkit-line-clamp: 4` with a `max-height` fallback, but it is also `flex: 0 1 auto` — so when
the cell is short of vertical space the flex shrink cuts the text *before* the clamp applies, and
the ellipsis never renders. Seen on `?mock=attention` at 2560 × 720 with a 154-character question.
It is a real rendering, so it is not a fake screenshot — but a gallery image showing a sentence
severed mid-word will read as a broken upload. Do not ship one.

**Trap 4 — a long question in the sheet SCROLLS.** `.sheet-question` is `overflow-y: auto`, and
`--hide-scrollbars` removes the only cue that it did. `?mock=question` (352 characters) produced a
shot whose question stops mid-sentence with nothing to say why. Pick a fixture whose question
fits: `attention`'s `…a30002` at 154 characters does.

**Trap 3b — v0.15.0 reproduced trap 3 in a new place, and it is in the shipped gallery.** See
**D15**. The approval card's summary now loses the bottom of its last line to the new
`43s to decide` countdown, on the **comfortable** density only. It is the same mechanism as trap 3
— a `flex: 0 1 auto` block shrinking below the height its clamp assumes — arriving in a block that
did not have it before. **A new line added to a card is a clipping regression until a shot proves
otherwise.**

**Trap 5 — gestures cannot be photographed honestly.** `&swipe=`, `&pinflash=` and `&ackflash=1`
all drive the shipping code path rather than drawing a picture of one, which is the right design —
but a still frame of a half-slid card reads as a rendering artifact, and the pin-flash frame is a
single glyph indistinguishable from the hero shot. `&swipe=first` on `rework` lost its target
card to a grid rebuild entirely. **Deliberate decision: the touch gestures are claimed in words in
the description and are not depicted in the gallery.** A gallery video (1920 × 1080 MP4, counts
toward the 10) is the format that could show them; none was made.

**The standalone shot needs an isolated copy.** With no `?mock=`, the widget polls
`http://127.0.0.1:2722` — and on a development machine a live `crabd` answers it (confirmed again
this pass: `2722` returned 200), so the panel renders the connected state instead of the
standalone one. Copy the widget somewhere temporary, point its default port at a dead one, shoot
there, and delete the copy:

```powershell
$tmp = "$env:TEMP\sc-standalone"
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item C:\Dev\sidecrab\widget $tmp -Recurse
$js = [IO.File]::ReadAllText("$tmp\scripts\sidecrab.js")
[IO.File]::WriteAllText("$tmp\scripts\sidecrab.js", $js.Replace('2722', '2799'))   # 2 occurrences
# serve $tmp on another fresh port (8798), then shoot TWICE and keep the second (trap 1):
Get-Shot "$shots\09-standalone.png" 'http://127.0.0.1:8798/index.html'
Get-Shot "$shots\09-standalone.png" 'http://127.0.0.1:8798/index.html'
Remove-Item $tmp -Recurse -Force
```

Verify the dead port really is dead first — a request to `http://127.0.0.1:2799/v1/state` must
fail, and `http://127.0.0.1:2722/v1/state` returning 200 is exactly why the copy is needed.

Downscale to the store's 1920 × 960 with `System.Drawing`, letterboxed on the panel's own
background so the bars are not a visible frame:

```powershell
Add-Type -AssemblyName System.Drawing
$src = 'C:\Dev\sidecrab\store\shots'; $dst = "$src\marketplace"
Get-ChildItem $src -Filter *.png | ForEach-Object {
    $img    = [System.Drawing.Image]::FromFile($_.FullName)
    $canvas = New-Object System.Drawing.Bitmap 1920, 960
    $g      = [System.Drawing.Graphics]::FromImage($canvas)
    $g.Clear([System.Drawing.Color]::FromArgb(15, 14, 13))   # the panel's own --bg-rgb
    $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $g.PixelOffsetMode   = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $g.DrawImage($img, 0, 210, 1920, 540)                    # 2560x720 -> 1920x540, centred
    $canvas.Save((Join-Path $dst $_.Name), [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $canvas.Dispose(); $img.Dispose()
}
```

Stop both servers and delete the isolated copy when you are done. Text stays legible after the
downscale; the smallest labels (`+1 more`, the per-subagent ages) sit at the edge of it, which is
one more argument for D9.

### 2.3 What is honest to claim, and what is not

Elgato's rule is that **media must accurately depict functionality**. Every shot in this gallery
is the shipping widget rendering shipping code against fixtures that are shape-faithful to what
`crabd` serves — nothing is mocked up, drawn, or composited. The session titles, repo names and
numbers in them are invented demo data, which is normal and expected for a screenshot.

**The gap is elsewhere, and it is worth stating plainly: not one pixel of this gallery was taken
on a XENEON EDGE.** All nine are headless Edge in a browser at 2560 × 720. That is fine for
proving a layout and wrong for proving a product. The table below is what a submitter needs
before pasting anything into Maker Console.

| Claim / what a shot depicts | Where it appears | Evidence today | Verdict |
|---|---|---|---|
| Clock, date, crab, crab moods | every shot | Rendered entirely by the widget with no external input | ✅ **Safe** |
| Session cards — state, model, elapsed, activity, context size | 01, 02, 05, 07 | Presence-gated on fields the shipped `crabd` serves | ✅ **Safe** |
| Usage gauges, reset countdowns, all three ramp colours | 01, 02, 05, 06, 07 | The OAuth limits path is the shipping default | ✅ **Safe** |
| Depletion forecast (`~full by …`) | 01, 05, 06 | `crabd` computes and serves `exhaustAt`; all three branches proven in one fixture | ✅ **Safe** |
| Daily budget, including the over-budget step | 01, 05 | `budget` is in `crabd`'s `CONFIG_WRITABLE`; served from `~/.sidecrab/config.json` | ✅ **Safe** |
| Burn: sparkline, by-model, per-session, commits | 01, 04, 05 | Read from local files by the shipped companion | ✅ **Safe** |
| Day drill, week strip, cross-session timeline | 06 | Served by `crabd`'s history endpoint | ✅ **Safe** |
| Session filter chip, card density chip (v0.15.0) | 01, 05, 07, 08 | Pure client-side view state on data already served; the flags that set them set the same variables a tap sets | ✅ **Safe** |
| Quiet hours | 08 | Config-driven, writable from the panel | ✅ **Safe** |
| Honest-failure banners, stale feed, no-companion line | 09 | The degrade path is the most-tested thing in the widget | ✅ **Safe** |
| **`$12.47 today` cost line** | 01, 03, 05, 06 | Only populated when Claude Code's OTLP telemetry is flowing to the companion. Never derived from token counts | ⚠ **Depicted, deliberately NOT claimed.** The description says nothing about cost. Leave it that way, or add "when telemetry is enabled" |
| **`official` provenance tag** | 01, 03, 05, 06 | `limits.source: "statusline"`. The repo's own caveat: the status line appears to render only in an interactive terminal, so on an app-hosted session `source` stays `"oauth"` and this tag never appears | ⚠ **Depicted, not claimed.** Most users will not see it. Harmless, but do not build copy on it |
| **Fleet dot showing `glow` running** | 01, 05, 07 | The glow component is **parked** — the CORSAIR SDK crashes in every non-interactive console context tested, so `SideCrab-glow` ships **disabled on purpose**. A stock install shows that dot stopped, not running | ⚠ **Depicts a state a stock install cannot reach.** The description does not mention RGB lighting and must not start. Low risk, but it is a fixture artefact, not a product state |
| **Approve / deny a permission request**, and the `43s to decide` countdown | **03**, and the request card in 01 and 05 | Written, unit-tested, and reasoned from the shipped binary's schema — but **never exercised end-to-end against a live CLI approval**. Ships off by default. The countdown is new in v0.15.0 and inherits the same gap: nothing has confirmed the hold window it counts down actually matches what the CLI enforces | ⛔ **BLOCKED — D12.** Either run the verification or cut the bullet and shot 03 |
| **"Queue a next step" (tap-to-continue), and the `queued: …` card line** | 02 (the Yes / Proceed / Stop row), 01 and 05 (the `queued:` lines) | An unsanctioned run recorded in `docs/BACKLOG.md` **claims** the CLI renders every Continue tap as *"Stop hook error occurred"*, because `decision:"block"` means "hook refused". Recorded as a claim to re-test, not a closed item | ⛔ **BLOCKED — D12.** If the claim holds, the panel sends a nudge the model receives labelled as an error. Re-test before the description promises it |
| **The iCUE temperature row** | **no shot** | A browser has no iCUE sensor provider, so the row never renders off-glass. It has never been photographed | ⚠ **Claimed in the description, depicted nowhere — D13.** The claim is honest; the media does not support it, and shot 09 actively undersells it |
| **Touch gestures — swipe, long-press, two-finger, pull** | **no shot** | Verified with a pointer stream in a browser, and every touch target measured off the live DOM at both slots. Never touched with a finger on a capacitive panel | ⚠ **Claimed in the description, deliberately not depicted (trap 5).** A hardware pass is what turns this from tested to proven |
| The panel as it looks in an actual XENEON EDGE slot | every shot | The widget is authored at 2560 × 720; CORSAIR documents the largest slot as 2536 × 696 | ⚠ **D10.** Confirm the slot on hardware; a gallery showing a wider panel than the slot renders is precisely the accuracy problem |

| **The approval card's clipped summary** | 01, 05 | Measured on this build. The card renders a real state and then loses the bottom of a text line to the new countdown | ⛔ **BLOCKED — D15.** A widget fix, not a copy fix |

**One hardware session closes most of this.** Import the packaged `.icuewidget`, place it on the
Edge, and re-shoot **01** and **09** on glass: that alone proves the slot (D10), puts the
temperature row in the media (D13), and shows the glow dot in its real stopped state. It does not
close D12 — that needs `setup\Verify-PanelApproval.ps1` with a real prompt — and it does not close
D15, which needs a CSS fix first or the re-shoot photographs the same defect.

---

## 3. Required decisions — the owner's, not the kit's

### D1 — Trademark. The blocker. ⛔ *(unchanged, and one input added)*

**Unresolved, and deliberately not resolved here.** The listing as drafted rests on marks owned
by Anthropic:

- **The name.** *Claude* and *Claude Code* are Anthropic's. The listing describes a panel whose
  entire purpose is reading Claude Code state, so the product name appears in the copy. Naming
  another company's product to say truthfully what yours works with is ordinary nominative use,
  and the store is full of listings that name the software they integrate with — but it is not a
  licence, and Elgato's rules make the maker responsible for the claim ("you must own the rights
  to any content you post", "cannot infringe trademarks or impersonate similar products").
- **The mascot.** The crab is Anthropic's Claude Code mascot. This is the harder half. The panel
  does not merely name a product — it renders a recognisable version of that product's mascot as
  its central visual identity, in the icon, in the badge and **in all nine gallery shots**, where
  it now occupies the leftmost sixth of every frame. A reviewer weighing "you must own the rights
  to any content you post" against a third party's mascot is not obviously going to land where
  the maker wants.
- **The badge.** `Claw'deck` is already the second iteration: the widget's own source records
  that a *Claude Max lockup* was removed and replaced at v0.5.0. That mitigation was taken for a
  reason; the mascot mitigation has not been taken.
- **NEW (measured 2026-08-26).** The brand-neutral description variant does **not** make the
  listing brand-neutral on its own. Shot `09-standalone.png` carries the widget's own on-glass
  string *"Claude Code stats need the SideCrab companion"*. Taking the neutral path therefore
  costs a widget string change and a re-shoot, not just a copy swap.

**What the owner has to decide, and it is a legal decision, not a design one:**

1. **Ship as-is** on the nominative-use argument, accepting the risk that Elgato rejects it, that
   Anthropic objects later, or that the listing is pulled post-publication under Elgato's
   standing right to re-review.
2. **Seek permission** — ask Anthropic for written consent to use the mascot and the product name
   in a free third-party store listing. Slowest, and the only path that actually resolves the
   question.
3. **Rename and redraw** before submitting. Fallback sketch below.

**Rename fallback sketch** (a starting point, not a recommendation):

- **The mascot.** Keep a pixel crustacean; make it demonstrably not the Claude Code crab.
  Different silhouette, different palette away from the current orange, different eye treatment.
  The panel's affordance — a creature whose posture is the status — survives a redraw intact;
  only the specific likeness has to go. This is the change with real work in it: the crab appears
  in `resources/icon.svg`, in the badge SVG in `index.html`, in the main crab art, and in **all
  nine** gallery shots, which would all need re-shooting.
- **The name.** *SideCrab* is already neutral and infringes nothing — it is the on-glass badge
  *Claw'deck* that leans on the wordplay. Under this option the badge becomes SideCrab, which
  also settles D2 for free.
- **The copy.** Swap the long description for the brand-neutral variant at the bottom of
  `LISTING.md`, and change the standalone on-glass string with it (see NEW, above).
- **What cannot be renamed away:** the companion reads Claude Code's local files, and the setup
  instructions must say so. Renaming the widget does not make the integration nameless.

### D2 — One name, two places *(unchanged — and now in nine shots)*

The manifest and the store copy say **SideCrab**; the on-glass badge says **Claw'deck**, and it is
the most prominent element in the top-left of every gallery image. A user who installs "SideCrab"
should not find a panel labelled something else. Pick one and make the manifest `name`, the badge,
the store name, the repo and the companion agree. Folded into D1 if the rename path is taken.
**Any change here re-shoots the whole gallery**, so decide it before the hardware session.

### D3 — Free or paid *(unchanged)*

One-shot: **monetization cannot be changed after the product is created.** Free needs no Stripe
Connect setup and is available in every country. Paid needs Stripe Connect, a 70/30 split, and is
unavailable in four countries. A widget whose full value depends on a separately downloaded free
companion is an awkward paid product; that is an argument, not a decision.

### D4 — Where does the companion live *(DECIDED 2026-09-01: `github.com/Dixie-sketch/Clawdeck`)*

**Decided by the owner 2026-09-01.** The public home is a fresh repo `Dixie-sketch/Clawdeck`
under Joe's personal account, seeded from the current tree with no history (the private
working repository's history carries internal strings and stays private as the upstream). The README clone line now carries the real URL. Still owed here: the first release
with the `.icuewidget` attached, and the URL in the listing copy and support links. The original
row follows for the record.

### D4 — Where does the companion live *(original row)*

The listing must point at a public download for the companion, and the README's clone line is
still a `<repo-url>` placeholder. Decide the public home (a public repo with releases is the
obvious answer), publish it, and put the real URL in the description and in the support links.
Elgato's rule that external paywalls may not gate core functionality means this download has to
stay free and unauthenticated. **The listing cannot ship with a placeholder here.**

### D5 — Support contact *(unchanged)*

Maker Console takes support links, and the guidelines require accurate contact details. Decide
what address or issue tracker receives support mail, and whether it is a personal identity or an
organization one.

### D6 — Privacy statement *(refreshed — the answer got easier)*

SideCrab collects nothing and transmits nothing, so a privacy policy is not strictly required —
the rule is conditional on collecting personal data. Two things now argue for publishing one
anyway: the companion reads a user's local session files, and the panel can **approve tool calls**
(D12). Both are questions a reviewer asks. The README's *"Before you turn on panel approvals"*
section is already most of the text; publishing it at a linkable URL answers both before they are
asked. Cheap. Do it.

### D7 — Widget version to submit *(refreshed — the numbers moved)*

`widget/manifest.json` reads **0.16.0** (measured 2026-08-27; packaged as
`SideCrab-0.16.0.icuewidget`). The packaged `.icuewidget` is the product file, and the version in
it is the version the store shows. NOTE: the gallery below was shot against **0.15.0** — the
version has since moved, so per the pre-upload checklist the gallery re-shoot condition is now
TRIGGERED (v0.16.0 added an approval-threshold slider to the settings sheet and fixed the F1/F2
card/prefs defects; visually similar, but re-verify before upload). Decide the submission version, bump the manifest, repackage, and write the
release notes the version submission requires.

Note the widget's own dev rule: `crabd` redeploys over the network, the widget does **not** — an
`.icuewidget` import is a double-click at the iCUE console, so a **schema** bump strands users
until someone stands at the desk and re-imports. The version that ships to a store is a version
you have to live with. Ship a whole, settled version; do not submit mid-wave.

### D8 — Does a widget need the 288 × 288 icon *(unchanged)*

The media documentation lists app icons as **plugins only**, and the widget declares its own
`preview_icon` (`resources/icon.svg`) in the manifest. Confirm in Maker Console whether the
widget product type asks for a 288 × 288 PNG; if it does, one has to be drawn — and it inherits
D1 whole, since the icon is the crab.

### D9 — Thumbnail treatment, and the letterbox *(refreshed — one shot made this worse)*

The gallery shots are the native panel letterboxed onto 1920 × 960, which is honest but leaves
large dark bands: a 2560 × 720 panel is 3.56:1 and the store canvas is 2:1. On the busy shots the
letterbox reads acceptably — the panel's alert glow gives it a visible edge. **On
`09-standalone.png` it does not:** a mostly-empty panel letterboxed onto a black canvas produces a
1920 × 960 image that is ~85 % black with no visible panel boundary, and a reviewer may read it as
a failed upload rather than a design.

Decide, in this order:

1. Whether the thumbnail gets a designed treatment — the panel shown on a device, or a crop of the
   strongest region — rather than a straight letterbox. `01-panel.png` is the default candidate.
2. Whether the letterboxed shots get a hairline panel border, or a device frame, so the panel's
   bounds are visible on the sparse ones.
3. Whether `09-standalone.png` survives at all in its current form, or waits for the on-glass
   re-shoot that D13 wants anyway.

### D10 — Slot size, and what a user actually sees *(unchanged, still unverified)*

The widget is authored at **2560 × 720**. CORSAIR documents the XENEON EDGE widget slots as
ranging from Small (840 × 344) to **Extra Large (2536 × 696)** — which is not the same number.
Confirm on hardware which slot the panel is intended for and that the gallery shots match what a
user sees there. A gallery showing a wider panel than the slot actually renders is exactly the
"accurately depicts functionality" problem. Every shot in this kit inherits this.

### D11 — Platform honesty *(refreshed — now answered in the copy)*

The manifest declares Windows. The widget itself will run wherever iCUE runs, but the companion
is Windows-only, so a non-Windows user gets the standalone clock and nothing more.
**Done in the refreshed copy:** the requirements sentence is now the *second paragraph* of the
description — inside the first 500 characters, above the fold, before any feature bullet. Nothing
further is owed unless the Requirements field in Maker Console turns out to want it separately.

### D12 — The approvals claim, and tap-to-continue. The second blocker. ⛔ **NEW**

Two write paths are described in the listing, depicted in the gallery, and **unproven against a
live CLI**:

- **Panel approvals.** The response shape was settled by reading the shipped binary's schema and
  the whole path is covered by tests, but the end-to-end run with a real permission prompt is
  outstanding. The feature ships **off**; the installer asks. `setup\Verify-PanelApproval.ps1`
  carries the procedure. The repo's own words: *treat the feature as written-and-tested, not
  proven, until you have run it.*
- **Tap-to-continue.** `docs/BACKLOG.md` records an unsanctioned run **claiming** the CLI renders
  every Continue tap as *"Stop hook error occurred"*, because `decision:"block"` means "hook
  refused" — the model receives the nudge labelled as an error and hedges about it. A fix is
  already sketched in `crabd.py` (`hookSpecificOutput` with `additionalContext`). That claim is
  explicitly recorded as **to be re-tested, not trusted** — but it is exactly the kind of thing
  the "test thoroughly for errors" guideline is about.

**Decide one of two, and it decides the description and the gallery together:**

- **(a) Verify first.** Run `setup\Verify-PanelApproval.ps1` with a real prompt and with the
  operator present, and re-test the Continue path on a mutating action (the backlog notes `echo`
  is auto-approved by the CLI's own classifier, so any permission test built on it reports a false
  pass). Then the description and shot 03 stand as drafted.
- **(b) Ship without them.** Delete the *"Approve or deny a permission request from the panel"*
  bullet from the description (−73 characters, body goes to 1,362), soften *"acknowledge, or queue
  a next step"* to *"acknowledge it"*, and drop `03-approval.png` from the upload — renumbering the
  remaining eight, which is still above the 3-item minimum. Shots 01 and 05 still show a
  permission-request *card* with its countdown, which is a real rendering of a real state — but if
  D12 lands here, prefer re-shooting the hero from a fixture without a `pendingPermission` rather
  than leaving an Approve affordance in the gallery that the copy no longer explains. That
  interacts with D15, which already wants those two re-shot.

**Do not submit with the claims in and the verification not run.** That is the one combination
that is both a guideline risk and a real user-facing risk.

### D13 — The temperature row is claimed and never photographed ⚠ **NEW**

A browser has no iCUE sensor provider, so the CPU/GPU temperature row does not render in any of
the nine shots. The description claims it (*"a clock and temperature display"*) and the manifest
declares the Sensors plugin as required, so the claim is honest — but no media supports it, and
`09-standalone.png` shows the standalone panel emptier than it actually is on a real device. That
is the shot most likely to undersell the product and the one a reviewer will weigh hardest against
"accurately depicts functionality".

**Decide:** re-shoot `01` and `08` on glass before upload (strongly preferred — it also closes
D10 and fixes the glow-dot artefact), or accept the under-depiction and say nothing. Do not add a
mocked-up temperature row to a browser shot; that would be exactly the fake screenshot the rule
prohibits.

### D14 — Is a gallery video worth making ⚠ **NEW**

The touch layer — swipe to dismiss, long-press to pin, two-finger tap to acknowledge everything,
pull to refresh — is a substantial part of what v0.14.0 added and **cannot honestly be shown in a
still** (trap 5). A gallery video is 1920 × 1080 MP4 under 250 MB and counts toward the 10 items.
Thirty seconds of a finger on the real panel would depict the gestures, prove the hardware, close
D10 and D13 in passing, and differentiate the listing from every static widget in the category.
Decide whether it is worth the shoot. If yes, it wants the same hardware session as D13.

### D15 — A v0.15.0 rendering defect is in two shipped shots. The third blocker. ⛔ **NEW**

**Measured 2026-08-26 on the v0.15.0 build, at 2560 × 720, comfortable density.** On a card
carrying a `pendingPermission`, the new `43s to decide` countdown line was added beneath the
approval summary in a block that had no spare vertical room. The result:

- the summary's last line is **vertically clipped** — the bottom of the glyphs is sliced, so
  descenders are cut in half;
- the clipped line sits **flush against** the countdown with no gap, so the two read as one
  overlapping smear.

The ellipsis is present, so the `-webkit-line-clamp: 4` did fire; what fails is that
`.card-approval` is `flex: 0 1 auto` and shrinks below the height its clamp assumed once the extra
line was inserted. **This is trap 3's mechanism arriving in a new block** (see §2.2, trap 3b).

**It is density-specific.** `?mock=rework&density=compact` renders the same card's summary whole,
with a clean gap before the countdown — the compact card drops the subagent list that eats the
room. So the defect is in the *comfortable* layout, which is the **default** a new user sees.

**Where it lands:** shots `01-panel.png` (the hero, and the thumbnail candidate) and
`05-budget.png`. Shot `03-approval.png` is the *sheet*, which has room and renders clean.

**This is a widget fix, not a store fix — it is out of this kit's scope and belongs to whoever
owns `widget/`.** What this checklist owes is the gate: **do not upload `01` or `05` until the
card renders its summary whole.** Then re-shoot both with the recipe in §2.2. Alternatives if the
fix is not wanted now: shoot the hero at `&density=compact` (clean, but it is not the default
density and the grid reads sparse), or shoot it from a fixture with no `pendingPermission` — but
`rework` is the only fixture carrying the budget and the depletion forecast, so that loses the two
things the hero is there to show.

---

## 4. Pre-submission run-through

**Blockers first — the rest is wasted effort until these are decided.**

- [ ] **D1** decided and, if it lands on rename, executed everywhere — icon, badge, crab art, the
      standalone on-glass string, copy, and all nine shots re-shot
- [ ] **D12** decided: either `Verify-PanelApproval.ps1` has passed and the Continue path is
      re-tested on a mutating action, or the approvals bullet, the queue half of the bullet above
      it, and shot 03 are cut
- [ ] **D15** cleared: the approval card renders its summary whole at comfortable density, and
      shots `01` and `05` have been re-shot against the fixed build

**Then:**

- [ ] D2–D11, D13 and D14 decided
- [ ] `widget/manifest.json` re-read: if the version is no longer the **0.15.0** this gallery was
      shot against, the whole gallery is re-shot before anything is uploaded
- [ ] Widget imported from the packaged `.icuewidget` and tested **on real hardware**, not only in
      a browser — this is the session that closes D10 and D13
- [ ] Shots `01` and `09` re-shot on glass if D13 lands that way; gallery re-cut and re-numbered
- [ ] Manifest version bumped; `icuewidget validate` and `icuewidget package` both clean
- [ ] `python -c "import xml.etree.ElementTree as ET; ET.fromstring(open('index.html',encoding='utf-8').read())"` passes — iCUE parses `index.html` as strict XML and the CLI validator does not catch that
- [ ] Companion published at a public URL; the clone line in the README is a real URL
- [ ] Organization created and Maker Agreement signed
- [ ] Name ≤30 chars, no emoji, no special characters — final, because it cannot be changed
- [ ] Monetization chosen — final, because it cannot be changed
- [ ] Description pasted and **re-counted in the console**: ≥250 and ≤1,500. The drafted body is
      1,435 with LF and 1,454 with CRLF — the console's own count is the one that decides
- [ ] Thumbnail 1920 × 960 PNG uploaded
- [ ] At least 3 gallery items, all 1920 × 960 PNG, uploaded **in the intended numeric order**
- [ ] Every uploaded shot re-checked against the §2.3 table — nothing depicted that the
      verification did not cover
- [ ] Support links and contact filled in, no placeholders anywhere in the listing
- [ ] Auto-publish checkbox set deliberately
- [ ] Release notes written for the submitted version

---

## Sources

- Submission Guidelines — <https://docs.elgato.com/makers/general/submission-guidelines>
- Product Guidelines (names, descriptions, media specs, pricing, IP) — <https://docs.elgato.com/guidelines/products/>
- Become a Maker (organization, agreement, Stripe Connect, revenue split) — <https://docs.elgato.com/marketplace/become-a-maker/>
- Maker Console: Getting Started — <https://docs.elgato.com/maker-console/getting-started/>
- Maker Console: Managing Products (media tab, versions, editability) — <https://docs.elgato.com/maker-console/managing-products/>
- Maker Console: Review Process (statuses, 4–10 business days, rejection path) — <https://docs.elgato.com/maker-console/review-process/>
- iCUE widget specification and getting started — <https://docs.elgato.com/icue/widgets/> · <https://docs.elgato.com/icue/widgets/specification/>
- iCUE widgets on the store (category filters: type, device, orientation, interactive, free) — <https://marketplace.elgato.com/icue/widgets>
- CORSAIR: How to Create a Custom Widget for the XENEON EDGE (slot sizes, creators portal) — <https://www.corsair.com/us/en/explorer/diy-builder/accessories/how-to-create-a-custom-widget-for-the-xeneon-edge/>

Repo-internal evidence for §2.3 and D12/D13: `README.md` (*Before you turn on panel approvals*,
*Known caveats*), `docs/BACKLOG.md` (the unsanctioned live-verification claims),
`widget/DEV.md` (fixture inventory, the nineteen screenshot flags, the reduced-motion trap),
`companion/crabd.py` (`exhaustAt`, `costUSD`, `CONFIG_WRITABLE`, the limits source constants).

**One caveat on the store sources.** The CORSAIR article still describes the creators portal as
"on the way", while Maker Console and the store's iCUE widget category are both live and
documented. Treat the field limits and media specs above as the general Marketplace rules — which
they are — and expect the widget product type to have its own wrinkles that only appear once you
are inside Maker Console with an agreement signed. Verify the widget-specific fields there before
assuming this checklist is complete.
