# Lane C — the DEV.md section

*Drop this into `widget/DEV.md` above the v0.29.0 section, and add the `&view=` row to the
dev-flag table. Nothing else in DEV.md changes.*

---

## v0.30.0 — four views on one display, and a crab that moves

> **Version label is provisional.** `manifest.json` is deliberately NOT bumped in this lane —
> the final number is assigned when the lanes merge. Every `v0.30.0` tag in `sidecrab.css`,
> `index.html` and `sidecrab.js` is that provisional label and needs one sweep if the number
> changes.

The grid zone showed one thing. It now shows four, chosen by chips in its own header, and the
crab is painted to a canvas instead of held still in an SVG. The identity zone and the Limits
zone are byte-identical to v0.29.0.

### The four views

| chip | what the zone shows |
|---|---|
| **Sessions** | the card grid, unchanged. The filter and density chips narrow THIS view and no other, which is why they stay where they were rather than joining the switcher |
| **Burn** | today's spend at full width: by session, by model, and the 24 h series the Limits sparkline only has room to sketch |
| **Week** | `recap.week` as a seven-day strip with the day drill **inline** |
| **Detail** | one session as a page, with the controls its sheet already has |

The chips are the LAST items in the header on purpose: the tappable run of header that still
opens the Today timeline is the left-hand half, where the heading and its visible hint are, and
anything added here has to stay out of it. The active view persists in the same vendor-storage
object the filter, the density and the pin map already share (`gridView`), with the same
round-trip of a value this build does not know that v0.16.0's audit F2 put on the other two.

### The trap: a hidden card grid loses its capacity

`gridCapacity()` derives the capacity from the COMPUTED `grid-template` lists on `#cards`, which
is what keeps the breakpoints in the stylesheet and out of JS. A `display:none` grid computes
**both** axes to the single token `none`, so `trackCount()` falls back to its 4x2 default — and a
compact grid or a 3-column slot would come back from another view with the wrong capacity and the
"+N more" tile cutting the wrong rows.

So the card grid is never `display:none` while another view is up. It keeps `display:grid` and
takes a zero height instead, where both axes still resolve to a list of lengths. Measured off the
DOM, all four views, both densities:

| state | `grid-template-rows` | `grid-template-columns` | `gridCapacity()` |
|---|---|---|---|
| Sessions, comfortable | `282.25px 282.25px` | 4 x `326.766px` | 8 |
| Burn/Week/Detail, comfortable | `0px 0px` | 4 x `326.766px` | **8** |
| Sessions, compact | `188.656px` x 3 | 4 x `334.33px` | 12 |
| Burn/Week/Detail, compact | `0px 0px 0px` | 4 x `334.33px` | **12** |

The cards still render (the badge logic reads the same document the grid was built from) and they
lay out into nothing.

### An alert never hides behind a view, and never yanks the glass

A session that flips to `needs_input` while another view is up is counted on the Sessions chip,
the chip takes an amber border and the badge pulses. **Nothing force-switches.** Forcing the view
would move a control out from under a finger already travelling toward it, which is the failure
every sheet on this panel is built to avoid.

The badge counts the **edge** and never the value — the rule `detectTricks` states for the party
hat. Two things that are not alerts:

- a panel that BOOTS into the Burn view beside two waiting sessions has watched nothing happen;
- a row that was already waiting when the operator left the Sessions view is not news.

**The baseline is the first DOCUMENT, not the first render**, and that was a real defect for one
round: `render()` runs once before any poll has landed, so the first render seeded an empty map
and the first real document then read every waiting session as brand new. Photographed on
`?mock=rework&view=burn`: a badge of **1** on a panel that had been up for two seconds.
`everHadData` is the panel's own answer to "has a document arrived", so it is what gates the seed.

Pinned by test, with the mutant beside it: `widget/tests/test_ordering.js` counts the edge, and a
value count is asserted to badge the boot case. Removing the seed guard from `trackViewAlerts`
fails **6 of 173** checks.

### The swipe is on the header row, and only there

A horizontal swipe on `.grid-head` steps the view; left is forward and it wraps. The cards area
keeps its own horizontal gesture (ack/dismiss) and gains nothing — two meanings for one gesture on
one surface is the thing a fingertip gets wrong.

It has its own listeners on the header rather than a branch inside the document-level gesture
layer, because it needs nothing that layer tracks and the layer already does the one thing it
depends on: `onPointerUp` calls `suppressClick()` for any travel past `TAP_SLOP_PX` (10 px), so a
swipe can never also open the Today timeline. The threshold is `HEAD_SWIPE_PX` = 48, four times
that slop, so a committing swipe is always suppressed by the time it commits. A second finger
abandons it outright: two fingers is the ack-all gesture.

Driven over CDP at 2560x720, from Sessions: swipe left → Burn, swipe left → Week, swipe right →
Burn, a 20 px drag → Burn (unchanged), sheet open = **false** throughout. A plain tap on the
header still opens the timeline (`data-mode="timeline"`, open = true).

### What each view reuses rather than reimplements

- **Burn** reads `sessions[].todayOutputTokens` for the by-session half and `burn.byModel` for the
  by-model half, each presence-gated and each saying so when its source is absent — `typeof`, not
  `Number()`, so a session crabd has no figure for is left OUT rather than listed at zero. The
  chart's scale rule is `renderSparkTarget`'s own: on hourly bars the daily budget is a PACE line
  and not a ceiling, and the chart scales to the target when the target is above every bar.
- **Week** calls `fetchHistory()` — the sheet drill's own function, which owns the mock routing,
  the 4 s timeout, the abort and the today-rebase. What is NOT shared is the navigation: the
  sheet's drill has prev/next chevrons and a Back that returns to the timeline, and neither has
  anything to return to here.
- **Detail**'s Approve, Deny and continue buttons carry the SHEET's own `data-decide` and
  `data-continue-prompt` attributes and reach `onSheetDecide` / `onSheetContinue`. Those two now
  read `laneCActionSessionId()` instead of `sheetSessionId` alone, which is the whole of the change
  they needed: the pairing code, the `requestId` echo, the 403/409/429 wording and the no-latch
  continue handling are inherited, not re-typed. A sheet WINS when one is open, because it is a
  modal over that page.

Proved by wrapping `postAction` and tapping the real controls:

```
detail Deny     -> postAction("b21c8d55-…0001", "decide", null, "deny")
detail Continue -> postAction("b21cd0e5-…0003", "queue-continue", "Keep going with what you were doing.")
detail status   -> "queued: Continue"
```

The week drill, tapped through: 08-21 → 7 rows under "Friday, August 21, 2026 — 7 events";
08-25 → 18 rows and a "+6 earlier" tail (both caps, each in its own words); 08-22 → "No events
recorded for this day."; **08-20 → the file is not there, so the 404 is produced rather than
simulated** and the pane says the companion may predate 0.8.0; the very next tap on 08-21 works
again (no latch).

### The Detail page

Full height, two columns. Left: the question WHOLE (it scrolls rather than clamping — the card
gives it three lines and this is the one surface that gives it all of them), or the permission
block with Deny and Approve and the hold countdown; then the queued line and the continue
buttons. Right: every subagent row and the events list whole. Above both: Back, the title and
repo/branch, the state chip with its elapsed figure, the model, and the context hairline **with
its numbers** — `972k of 1.0M — 97%`, which is the one thing a 3 px rule on a card cannot say.

Reached two ways: the **Full view** button in the session sheet's pin row, and the Detail chip
itself, which falls back to the row that wants a human (waiting, then working, then done, then
idle) when nothing has been chosen. There is deliberately **no affordance on the card**, and the
number is why: every control on this panel wears the 48 px fingertip floor, the card header is a
single line box, and a floor-sized control in it would take that height off the card body at the
density where the cards are already tightest (v0.28.2 measured that room down to single pixels).

Ages are absent from the page's signature, exactly as they are from the card signature, and are
relabelled in place by the 1 Hz tick — the approval hold is the reason it is a tick and not the
poll.

### The smaller slots keep one view, and it is the cards

**1660 is a measured number.** The Sessions header's rigid content — the label 94.81, the hint
68.39, the three old chips 60.47 / 129.14 / 91.28, the four new ones 100.75 / 62.89 / 62.89 /
81.83, nine 10.8 px gaps and the zone's own 57.6 px of padding — is **907.2 px**, and the grid zone
is 55.5% of the panel, so the switcher needs a panel of 907.2 / 0.555 = **1635 px**. Confirmed by
sweeping the slot down: the header first overflows between 1700 (0.00) and 1600 (20.00 px). 1660
is that threshold with one chip's worth of cushion for another engine's font metrics.

Below it the switcher goes and the card grid comes back whatever the stored view says. The
stylesheet owns that breakpoint exactly as it owns `gridCapacity`'s: nothing in JS knows which slot
it is on, the stored view survives untouched, and it is on the glass again the moment the panel is
wide enough to show its chips. No alert can hide behind a view there for the same reason — the
view that is showing IS the cards.

| slot | chips | Burn view | cards height | cards on glass | `gridCapacity()` | page overflow | zone overflow |
|---|---|---|---|---|---|---|---|
| 2560x720 | `block` | `flex` | 0 | 6 | 8 | 0x0 | 0.00 |
| 2536x696 | `block` | `flex` | 0 | 6 | 8 | 0x0 | 0.00 |
| 1661x720 | `block` | `flex` | 0 | 6 | 6 | 0x0 | 0.00 |
| **1660x720** | `none` | `none` | 583 | 6 | 6 | 0x0 | 0.00 |
| 840x696 | `none` | `none` | 362 | 4 | 4 | 0x0 | 0.00 |
| 416x696 | `none` | `none` | 393 | 3 | 3 | 0x0 | 0.00 |
| 840x344 | `none` | `none` | 260 | 4 | 4 | 0x0 | 0.00 |

---

## The canvas crab

Every rect is transcribed from the SVG in `index.html` at the SAME coordinates on the SAME
half-cell grid (viewBox `0 -4 52 44`, 2 units = one half cell, 4 units = one cell), so the still
frames are the frames that shipped; what is new is that the poses in between exist.

**The SVG is still the truth about state.** The renderer reads `data-mood`, `data-acc` and the
trick classes off `#crab` and the quiet/esc2 classes off `body` through two MutationObservers, and
writes nothing anywhere. The mood ladder, the wardrobe hysteresis, the dance's three gates,
`scheduleBlink` and every `&crab=` / `&mood=` / `&celebrate=` / `&blink=` flag drive the same
attributes they always did and this follows them.

**The SVG is also the fallback**, hidden only once a 2D context has actually been obtained
(`body.crab-canvas-on`), so a host with no canvas renders exactly what it rendered before. Its
keyframes are stopped with it: a hidden element still runs its animations, and on a 24/7 panel
that is a compositor job per frame for nothing.

**Motion is eased in time and quantized in space**, which is the pixel-art idiom and not a
compromise: every pose lands on an integer viewBox unit, so the blocks stay hard-edged (the canvas
spelling of `shape-rendering="crispEdges"` is whole device pixels on every edge — two rects that
share an edge in viewBox units round to the same device pixel, so the silhouette stays one solid
shape instead of growing seams), and the EASING decides which unit it is on at a given
millisecond. A sweep across four units is five poses arriving on an ease-out curve, where the CSS
keyframes had two arriving on a `steps(1, end)`.

| motion | before | now |
|---|---|---|
| `waveonce` | two frames, 640 ms x 3 | an eased nought-to-one-cell sweep, same 1.92 s |
| `snap` | two frames, 260 ms x 2 | an eased half-cell travel in and back |
| `bounce` | two frames, 380 ms x 2 | a sine hop, so the animal hangs at the top |
| `dance` | four stills, 390 ms x 4 | eased slides between the same four beats |
| `juggling` | three snapped positions | three parabolic arcs, a third of a cycle apart |
| `sweating` | three static drops | three drops accelerating down their own clearance, then re-forming |
| idle | a still image | the shell's top edge rises one unit and settles, ~4.2 s |

The drips' fall distances are **clearance, not taste**: the claws sit at y 14..20 across x 0..8 and
x 44..52, and each of the three drops is in one of those columns. Drop 2 starts eight units lower
than the others, so three units is all the room it has (8 / 3 / 8).

The hard hat is not here because it is not in the tree: it was retired at v0.18.0 and no art for it
exists. `data-acc="hardhat"` paints no accessory, which is exactly what the SVG does.

### The frame budget, which is the whole point on a 24/7 panel

Three scheduling states and only one of them is `requestAnimationFrame`:

| state | scheduler | why |
|---|---|---|
| a trick | `requestAnimationFrame` | all bounded (520 ms to 6 s), and the loop STOPS when the last one ends |
| `sweating` | a fixed 83 ms timer | the mood can hold for hours, and an eight-step fall over 1.4 s changes pose about six times a second; 60 Hz would be ten wake-ups per pose |
| idle | a timer aimed AT the breath's next pose boundary | the pose is binary, so it wakes about twice every 4.2 s |
| quiet or reduced motion | **nothing is scheduled at all** | quiet means nothing on this panel moves |

An unchanged pose hash skips the clear and the fills entirely, so a 60 Hz loop costs about what a
12 Hz one does.

Read off the running page:

```
idle              crabRaf = 0, crabSlowTimer set, crabMotion = {}
a trick on a loop crabRaf > 0, crabMotion = ["juggling"]
reduced motion    reducedMotion() = true, crabRaf = 0, crabSlowTimer = null
```

### Idle CPU, with and without

`Performance.getMetrics`' `TaskDuration` is the renderer's cumulative main-thread task time, so two
samples 30 s apart give a duty cycle out of the engine rather than out of Task Manager. Same page,
same fixture, same window; the OFF run stops the renderer in-page before the sample starts.

| state | crab ON | crab OFF | cost |
|---|---|---|---|
| idle, `?mock=recap` | **1.98%** | 1.96% | +0.02 pp |
| a waiting session, `?mock=rework` | **2.25%** | 2.11% | +0.14 pp |
| sweating for hours, `?mock=hot&mood=sweating` | **0.53%** | 0.21% | +0.32 pp |
| quiet hours, `?mock=quiet` | **0.20%** | 0.18% | +0.02 pp |

(The 10x between the top two rows and the bottom two is not the crab: `recap` and `rework` both
carry an unacked `needs_input` card, and the card pulse is a CSS animation that runs whatever the
crab is doing. `hot`'s waiting row is acked and quiet suppresses the pulse outright.)

A trick held on a loop, which is the worst case the panel can reach and lasts seconds:

| state | main thread |
|---|---|
| breath only, `?mock=recap` | 1.90% |
| `&crab=juggle` on a loop | 2.30% |

**Frame timings** (`window.__sidecrabCrabFrames` holds the last 120 paint durations in ms):
during a trick, n = 120, min 0.00, median 0.00, p95 **0.10**, max **0.10** ms. Across every mood
and accessory capture, max **0.20** ms.

### The rules that did not move

- **`prefers-reduced-motion`**: `fireDance` / `fireJuggle` / `fireSnap` / `fireBounce` all refuse
  (`crabMotion` stays `{}`, no class is added, `crabRaf` stays 0), nothing is scheduled — **and a
  mood change still repaints** (`waving` → `worried`, pixel hash changes).
- **Quiet hours**: the same, plus the sweat is withheld and the juggle balls are not painted.
  A mood change still repaints (`content` → `worried`).
- **Tap-to-ack**: the canvas is `pointer-events: none`, `elementFromPoint` at the crab's centre
  returns `crabWrap`, and a dispatched click takes **2 acks on `?mock=attention`**, identical to the
  same page with the renderer switched off.
- **Colour tokens win at runtime**: the palette is read from the SVG's own computed custom
  properties and dropped on any mutation. Setting `--crab-fill` to `#00FF88` on `#crab` moves the
  painted crab from `#E45C28` to `#00FF88`; setting `--accent` on `documentElement` moves the
  active view chip's border to `rgb(0, 170, 255)`.
- **The dance gates** (20 s minimum turn, 30 s cooldown, never beside a waiting session) are
  untouched — they live in `fireDance` and `detectCelebration`, which this lane does not edit.

### The trap this lane banked

**A state class on `body` and a styling class on an element must never share a name.** The first
cut called both `crab-canvas`, so the element rule's `transform: translateY(1.4 vmin)` matched
`body.crab-canvas` as well and translated the WHOLE PANEL down **10.08 px**. Every zone still
measured zero overflow against itself — the zones moved with the body — and the only thing that
said so was the PAGE overflow, which was exactly 10 on all four fixtures in all four views. A
per-zone probe would have shipped it.

### Verified

`node --check scripts/sidecrab.js`, strict-XML parse of `index.html` (ElementTree AND minidom),
JSON parse of all 20 fixtures plus manifest and translation, `icuewidget validate widget`
(CLI 0.4.45) clean with the known `icueEvents` warning, `node widget/tests/test_ordering.js`
**173/173** (was 140), and `python -m unittest discover -s companion/tests -t companion/tests`
**1147 tests OK** (1 skipped) — untouched, and proved untouched.

Headless Edge 140, `--force-prefers-no-reduced-motion`, device metrics pinned, a FRESH
`--user-data-dir` per run (the v0.28.2 trap: a shared profile served the old stylesheet for two
whole passes), served from `widget/` over `http://127.0.0.1:8765`:

- **Overflow sweep, 64 captures**: four fixtures (`rework`, `dense`, `attention`, `recap`) x four
  views x two densities x two slots (2560x720, 2536x696). **Page overflow 0x0, worst zone overflow
  0.00, header overflow 0.00, no offenders, no console errors, on every one.** 17,016 elements
  measured against their own zone's border box, descent stopped at scroll containers.
- **Seven slots** for the fallback band, in the table above: page 0x0 and zone 0.00 on all seven.
- Type at 2560x720, both densities (they are identical — this zone's views are not density-scaled,
  because density is a property of the CARD GRID and the chips say so by narrowing only it):
  chip 15.12, burn row 19.44, panel head 15.12, hour label 12.24, week `done` figure 23.04, detail
  title 24.48, detail chip 16.56, detail question 21.24 px.
- **The widest row's spare width** at 2560x720, per view: Sessions header **61.74 px** spare with
  the switcher in it; the week column 10.43; the burn row and the detail context line come out at
  −0.03 and −0.02 px, which is the probe's own gap arithmetic on a flex row whose last item
  ellipsises — the zone scan reads 0.00 for both.

**Measured, not fixed, and left as one row for the next wave:** at an arbitrary 1400–1401 px panel
width the identity zone overflows by 1.38–1.47 px at `clockSs` / `clockHm`. It reproduces at HEAD
with this lane stashed, so it is not this lane's, and 1400 px is not a slot in the verified matrix.

### Dev flag added

| flag | effect |
|---|---|
| `&view=<sessions\|burn\|week\|detail>` | set the grid zone's view for the shot. It sets the SAME variable a chip tap sets and nothing else, so what is photographed is the real view; it does **not** write to the vendor store — the discipline `&pin=`, `&filter=` and `&density=` keep, because a screenshot flag that persisted would leave the operator's own panel on a view they never chose. Applied AFTER `loadPrefs()`, so it still wins on a run that also carries `&uid=` |

### The pictures

`docs/notes/lane-c-view-burn.png`, `lane-c-view-week.png` (with a day drilled inline),
`lane-c-view-detail.png`; and the crab at `lane-c-crab-mood-{content,waving,asleep,worried,celebrating,sweating}.png`,
`lane-c-crab-acc-{sunglasses,party,nightcap}.png`,
`lane-c-crab-trick-{juggle,dance,bounce,snap}.png` — each trick caught mid-motion, with the pose
it was caught at recorded beside it:

| shot | pose (`armL,armR,clawR,rigX,rigY,breath`) |
|---|---|
| `trick-juggle` | balls at `19,5` / `-10,1` / `5,0` — three points of one arc |
| `trick-dance` | `rigX 3, rigY -1`, mid-slide between two beats, shades on |
| `trick-bounce` | `rigY -4`, the top of the hop |
| `trick-snap` | `clawR -2`, the claw all the way in |
