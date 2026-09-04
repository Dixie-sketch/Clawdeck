# SideCrab widget — dev notes

**2026-09-04, widget 0.29.0 — crabd moved from 2722 to 9999, and the panel gained a second
origin.** Every `2722` still written below this line is inside a dated measured-evidence
section and is left as measured; it is not a live address. What the panel does today is the
two-origin rule under **Preview**.

**Preview:** `python -m http.server 8765` from this folder, then open
`http://localhost:8765/` in a Chromium browser sized to **2560x720** (DevTools device
toolbar → Responsive → 2560 x 720). File:// also works but blocks the mock fetches.

**Mock harness (no crabd needed):** append `?mock=normal`, `?mock=attention`,
`?mock=empty`, `?mock=stale`, `?mock=question`, `?mock=quiet`, `?mock=caveat`,
`?mock=recap`, `?mock=hot`, `?mock=rework`, `?mock=dense`, `?mock=extras` or
`?mock=future`.
Leave the query string OFF (with no companion running) for the **standalone**
state, which is the first thing a store user ever sees.
Fixtures live in `mock/`; the loader rebases every timestamp so everything but
`stale` renders fresh (`stale` renders ~3 min old).

**Where the widget polls, since 0.29.0, is decided by the ORIGIN and not by a setting.**
`baseUrl()` reads `location.protocol`:

- **Served over `http:` / `https:`** — which is how crabd itself serves the panel, at
  `http://localhost:9999/` — every path is **same-origin and relative**: `/v1/state`,
  `/v1/history?day=…`, `/v1/action`. An absolute address here would be cross-origin and
  crabd's Origin gate would answer 403.
- **Opened from `file:`** — the iCUE webview, and a location that reports no protocol at
  all — the panel names crabd outright, at the port in the `crabdPort` widget property,
  **default 9999**.

**So the `python -m http.server 8765` preview is MOCK-ONLY for anything that talks to
crabd.** Served from 8765 the panel is same-origin with the static server, so `/v1/state`,
the history fetches and every POST go to 8765, which does not implement them — the panel
renders the standalone/disconnected state, whatever crabd is doing on 9999. Use `?mock=` for
panel content, and drive the real wire either from crabd's own `http://localhost:9999/` or
by opening `index.html` as a `file:` URL.

| fixture | schema | what it is for |
|---|---|---|
| `normal`, `empty`, `stale` | 1 | **v1 regression** — proves an absent v2/v3 field still renders exactly as 0.1.3. `normal` is also the **no-`burn.daily`** case: the sparkline toggle must be inert and its chip muted |
| `attention` | 2 | one needs_input WITH a question and one WITHOUT (the v1 fallback), turn chips, subagent rows |
| `question` | 3 | long multi-line question, 5 subagent rows on one card and a `+N more` clamp on another; carries `events` and `burn.daily`. It is also the **small-slot** fixture — see the 840x344 note below |
| `quiet` | 2 | `quiet.active: true` with one **acked** needs_input — ambient, nothing pulsing. Since v0.22.0 it is also the **override-ON** fixture: `quiet.override {mode: "on"}`, which is why the panel is dim, so the moon chip reads `quiet 58m` and the quiet note names the override instead of the window's 07:00 end |
| `caveat` | 4 | Since v0.22.0 also the **override-OFF** fixture: `quiet.override {mode: "off"}` on a panel that is not in a quiet window anyway, so the ONLY thing it changes is the chip reading `awake 58m` — the off branch proved from a document without disturbing anything else this fixture is for. **`limits.note` non-null with `available: true`** — the v0.4.0 widened semantics. The gauges stay lit on 61% / 44% and the note renders muted, NOT amber: a caveat is not a failure |
| `rework` | **5** | **the post-rework production shape** — schema 5 carrying EVERY current field at once: `quiet`, `recap` (4 repos **and `week`**), `limits` with a non-null `note` AND an `extra` window, `burn.today`/`byModel`/`hourly`/`daily`, `fleet` (one running, one **stopped**, so both dot shapes are in one shot), and per-session `question`/`turnStartedAt`/`acked`/`subagentDetail`/`events`/`contextTokens` (including a `null` on the idle row and a `1954200` on the `M` branch). This is the fixture that proves additive fields light up under a pinned schema number. Since v0.7.0 it is also the **countdown** fixture (its three windows sit at +33 min, +2h10m and +4d, one per format branch), the **week strip** fixture (7 days, with a `null` done on the oldest so the em-dash branch is in the shot), and the **per-key 400** fixture (see `_mock` below). Since v0.11.0 it is also the **titles and hung** fixture: one working row is deliberately 3m45s quiet, the `done` row carries `titleSource: "cwd"` and the `idle` row carries no title, repo or branch at all. Since v0.12.0 it also carries `limits.source: "statusline"` (the `official` tag), `burn.costUSD` (the `$N.NN today` line), a top-level `continuePrompts` extra, and a `pendingPermission` on `…50001` (the approval card + panel-wide red glow). Since v0.13.0 it is the **depletion-forecast** fixture, proving all three branches in one shot: `fiveHour` carries an `exhaustAt` **before** its reset (the `~full by …` line renders), `weekly` carries `exhaustAt: null` (no line), and the `extra` "opus weekly" window carries an `exhaustAt` **after** its reset (the guard suppresses the line) . Since v0.15.0 every session carries `queuedContinue` (the key is the widget's feature detection, so it is present and `null` on four of the six rows), and the `pendingPermission`'s `requestedAt` was moved to **10 s before `generatedAt`** so the approval countdown lands mid-hold rather than already expired. Since v0.17.0 it is also the **seeded approval-threshold** fixture: a top-level `toast` block carrying `approvalThresholdSec: 45`, deliberately neither the property default (20) nor a slider step, so a panel showing the property instead of the seed is visibly wrong in the shot. Since v0.22.0 it carries `quiet.override: null` — the member PRESENT and empty, which is the shape a presence test gets wrong if it checks truthiness instead of type. crabd never emits that (0.23.0 omits the key entirely when there is no override), so this is a DEFENSIVE shape the way `_mock.config400` is: the fixture exists to exercise the widget's reader, not to claim a daemon behaviour. It renders identically to an absent member — the chip reads `auto` — which is the whole point. Since v0.21.0 it is also the **host** fixture: a top-level `host` with all four members readable (`34.2` / `58.4` / `18.7` / `32.0`), so the sensors row carries a temperature, a name, a CPU figure and a memory figure at once — which is also the widest the row ever gets, and therefore the fixture the row's width budget is measured on |
| `future` | **6** | **the break regression.** Otherwise a perfectly valid document — the ONLY thing wrong with it is a schema above `SCHEMA_MAX`, so it must render the **dead feed** (worried crab, dimmed panel, no session cards). A break is still real: if this fixture ever renders its one session card, the ceiling has stopped meaning anything |
| `recap` | 5 | `burn.daily`, `events` on every session — including one with `events: []` — `recap` (3 repos), `burn.byModel` (4 models), and since v0.6.0 `fleet` (both running) and `contextTokens` on every session **including one `null`** (the idle row, which must render NO ctx chip). It exercises the recap header, the Today timeline, the burn sheet's by-model split and commits list, and the `/v1/config` quiet-hours POST path. Since v0.16.0 it also carries `_mock.config400: ["toast.approvalThresholdSec"]`, which makes it the **0.7.0–0.15.0 crabd** fixture: the `toast` key is accepted, its optional third member is not, so the drop-and-retry fallback runs in a real session. **Renamed at v0.9.0** with the removed feature it used to be named for; everything else about it is unchanged |
| `hot` | 5 | the **loud** panel: `fiveHour` at **97%** (the red step), `weekly` at 81% (amber) and an `extra` window at 62% (blue) — all three ramp steps in one shot; **30 events across 6 sessions**, so the Today timeline hits its 20-row cap and renders the `+10 earlier` tail; a session at `contextTokens: 1954200` (the `M` branch of `fmtNum`) on a **question card**, where the ctx chip must be ABSENT; and `fleet: {glow: running, toast: stopped}` for the two-shape dot row. Since v0.22.0 its one `needs_input` row is **`acked: true`**, which makes `hot` the **sweating** fixture: with nothing unacked the mood ladder falls through `waving` to `sweating` on the 97% five-hour window, so the red-limit trigger is reachable end to end from the fixture alone with no flag at all. (It was briefly done with `&ackflash=1` instead and that is NOT reliable — the optimistic ack is pruned by the rebase within one poll, the v0.4.0 trap below, so the crab reverted to `waving` before a screenshot could land. A fixture-served `acked` is not optimistic and does not prune.) The card keeps its question and its ctx-chip-absent rendering; it gains an ACKED badge and stops pulsing. Since v0.21.0 it is the **all-null host** fixture: the block is present with every member `null`, which is a crabd that could not measure this machine — both host segments must be ABSENT, never `0%`. Since crabd 0.28.0 it is the **served-denominator** fixture, and the only one carrying `contextWindowTokens`: five rows get a window from the feed with NO marker in the model id (the production case — `claude-opus-5` at 1000000 renders a bar where v0.22.0 rendered none), one row is deliberately `contextWindowTokens: null` beside a real `contextTokens` (**known fill, unknown window, NO bar** — the branch a truthiness test gets wrong), and the `claude-haiku-4-5` row is the mirror of it (**known window 200000, `contextTokens: null`, NO bar**). The `1954200`-on-a-`1000000`-window row also pins the over-100% clamp |
| `dense` | 5 | **fourteen sessions** (2 needs_input, 6 working, 3 done, 3 idle) — the density fixture. Every other fixture stops at ten, and a compact grid that holds twelve cannot be photographed FULL from ten: the capacity would be a claim rather than a picture. It is also the **filter** fixture, because it is the only one with enough of every state for `showing N of M` to say something in all four modes. Carries `queuedContinue` on three rows (one exact-label match, one trimmed) and a `pendingPermission` for the countdown. Since v0.17.0 it is the **unseeded** counterpart to `rework`: a `toast` block PRESENT with the optional `approvalThresholdSec` ABSENT - the current-crabd, operator-has-never-set-it case, and the one a truthiness test gets wrong. Since v0.22.0 it is the **context-hairline** fixture, and the only one whose models carry a window marker: four of its rows were retargeted so one grid holds every branch at once — `opus-5[1m]` at 972k (**97%, red**), `opus-5[1m]` at 784k (**78%, amber**), `sonnet-4-6[200k]` at 61k (**31%, neutral**, and the `k` branch of the marker parse), `opus-5[1m]` with `contextTokens: null` (**marked but unmeasured, NO bar**) and the ten untouched `opus-5` / `sonnet-4-6` rows (**unmarked, NO bar** — the common case, and the one a guessed denominator would have got wrong). The 97% row is a `needs_input` card, so it is also the proof that the BAR appears where the ctx BADGE cannot: the badge is dropped on a question card to save the badges row a wrap, and the bar is out of flow and costs nothing. Since v0.21.0 it is the **partial host** fixture — `cpuPct: null` beside a readable `memPct` — because "any field may be null" is a per-member fact and a block-level presence test passes this one while getting it wrong. It deliberately carries **NO `contextWindowTokens` at all**, which makes it the **pre-0.28.0 crabd** fixture: the member absent, so `ctxWindowTokens` falls back to parsing the marker itself and this grid must render byte-identically to the way it did at widget 0.22.0. Do not add the member here — that fallback has no other coverage on the glass |
| `extras` | 5 | **the two-window fixture (v0.26.0)** — `rework` byte-for-byte plus a SECOND `limits.extra`, the shape `extras.slice(0, 2)` has always allowed and that no fixture carried, which is why the overflow it causes went unmeasured from v0.17.0 to v0.26.0. A separate file rather than a second window bolted onto `rework`: `rework` is what the whole probe matrix is baselined on, and every capture of it has to stay comparable. The second window is the WORSE of the two contract-legal shapes — `exhaustAt` **before** its reset, so the forecast line renders and the row is 100.77 px against the first window's 79.89 — and its utilization (0.41) sits below the first's (0.63) because crabd sorts `extra` by utilization desc. It is the only fixture that reaches `body.limits-two-extras`, so it is the only one that shows the collapsed TODAY line and a Limits zone with no sparkline |

Since v0.10.0 `recap` and `rework` also carry `burn.budget`, and each one's
`todayPct` is computed from that fixture's own `burn.today.outputTokens` — 34% on
`recap` (1.75M/day) and 32% on `rework` (2M/day). A fixture whose percentage
disagreed with its own numbers would be proving the wrong thing.

**Schema is a BREAKING marker, not a feature level (v0.6.1 rework).** `SCHEMA_MAX = 5`
in `scripts/sidecrab.js` is the ceiling, and **every** feature is gated on FIELD
PRESENCE — never on a number. The one and only comparison against `doc.schema` left in
the widget is the acceptance check itself; if you find yourself adding a second, the
rework has been undone. `recap` and `hot` were rebased 6 → 5 at v0.6.1 because crabd
0.6.1 serves those exact fields under 5; a fixture left at 6 would have silently become
a dead-feed shot. Why it matters: crabd redeploys over RDP, the widget does **not** — an
`.icuewidget` import is a double-click at the iCUE console, so a number bump bricks the
glass until someone stands at the desk.

**Gauge ramp fixtures.** `attention` is the mid-ramp fixture: 86% and 93% both land **amber** under the v0.5.0 thresholds (75 / 95) where the old 70 / 90 ramp made 93% red, and its `extra` window at 71% stays **blue**. `hot` is the fixture that reaches the **red** step (97%), so since v0.6.0 the red bar has a screenshot, not only a computed-`background-color` read. Both are still worth reading off the DOM: `#fill5h` must compute to `rgb(212, 85, 63)`, `#fillWk` to `rgb(232, 163, 61)` and the extra row to `rgb(46, 127, 242)`.

**POSTs in mock mode never leave the page**: the stub logs the body to the console
and returns 204 for `ack` and `ack-all`, 501 for `reply`, 204 for `queue-continue`
and `decide` (v0.12.0 — unless the older-crabd 400 is being demoed via `&action400=1`
or `_mock.action400`), and 204 for `/v1/config` — so the sheet, the crab tap, the
continue/approval controls and the quiet-hours properties are all demoable without
crabd.

**The fixture's own `/v1/config` stub (v0.7.0).** A fixture may carry a top-level
`"_mock": { "config400": ["toast"] }`, and `mockConfigStatus()` answers **400** to a
POST whose top-level key is listed and 204 to everything else. That is what makes the
per-key path demoable without an older crabd to POST at: on `?mock=rework` the
quiet-hours write is accepted and the toast write is refused **in the same session**.
The underscore says harness, not contract — nothing in the render path reads `_mock`,
and the contract's "unknown top-level keys are ignored" rule is what makes carrying it
in a fixture safe. It is honoured only inside `if (mockName)`.

**Sub-member entries (v0.16.0).** An entry may also name a member inside a key —
`"toast.approvalThresholdSec"` — and then the 400 is answered only when the body for
that key actually carries that member. That is the crabd between 0.7.0 and 0.15.0: it
knows `toast` and has never heard of the optional third member. `recap` carries this
stub, so the member's drop-and-retry fallback is demoable off-glass.
Since v0.10.0 the list is `["toast", "budget"]`, so **both** older-crabd keys are
refused in the session where quiet hours is accepted. The pair is not a claim
about any real crabd — `rework` serves a `burn.budget` it also refuses to have
written, which no shipped crabd does — because `_mock` exists to exercise the
widget's handling of a REPLY, and that handling is per-key regardless of what the
document happens to carry.

**Mock history (v0.8.0).** The day drill fetches `GET /v1/history?day=…`, which in
mock mode is routed to `mock/mock-history-<day>.json` — one canned document per day,
shape-faithful to crabd's `_do_history` (UTC `ts`, grouped by LOCAL day, `count` =
the length of what was returned, `truncated` = more exist beyond it). The
`rework` fixture's week covers **2026-08-20 … 26** and the files deliberately do
not:

| day | file | what it exercises |
|---|---|---|
| `2026-08-21` | yes | an ordinary day, 7 events, comfortably under both caps |
| `2026-08-22` | yes | a well-formed day with **nothing in it** — 200 with `events: []`, so the "absence of history is not an error" branch renders its stated absence rather than a blank region |
| `2026-08-24` | yes | 12 events, still inside the row cap |
| `2026-08-25` | yes | **the truncated day**: `count: 200, truncated: true` over 24 rows, so BOTH caps are in one shot — "200 events (truncated)" on the count line under a "+6 earlier" tail |
| `2026-08-26` | yes | the **last column of the `rework` week strip**. This row said "today" from v0.8.0 until v0.19.0, and stopped being true at midnight on the 26th — see the today rows below, which is the fix |
| `2026-08-20`, `2026-08-23` | **no** | the static server 404s them, which is the **older-crabd path produced rather than simulated**: the tap is inert, one console line, and the very next tap on a live day still works |

**TODAY has no date in its filename (v0.19.0)**, because today is read off the wall
clock and a date-named fixture for it is wrong by the next morning — which is exactly
what happened to the `2026-08-26` row above. `mockHistoryUrl()` routes the requested
day to `mock-history-today*.json` when it equals `todayKey()`, and `&hist=` picks which:

| `&hist=` | file | what it exercises |
|---|---|---|
| *(default)* / `rich` | `mock-history-today.json` | **26 events across 5 sessions and every kind crabd persists** — session started / prompt submitted / asked a question / turn finished / done / session ended / subagent finished, plus `permission requested`, `approved from panel: Bash`, `denied from panel: WebFetch`, `permission passed through: Write`, `continue sent: …`, `acknowledged from Edge` and `answered outside the panel`. 26 over the 18-row cap, so the `+8 earlier` tail is in the shot |
| `empty` | `mock-history-today-empty.json` | 200 with `events: []` — a genuinely quiet day, which renders its stated absence. The chip stays normal, because crabd answered |
| `error` | *(nothing — the name 404s)* | the **older-crabd path, produced not simulated**. The sheet is NEVER opened; the chip reads `No history` instead |

`rebaseMockHistory()` moves a canned day's events onto the day that was asked for,
keeping each local clock time. For a date-named fixture the two days are the same
string and it is a no-op, so 08-21/24/25 render exactly as before; it exists so the
today file has no date baked into its contents either.

**Dev-only screenshot flags.** All of them are honoured **only** alongside `?mock=`,
so none can be reached from the iCUE origin. (There used to be a COUNT in this
sentence. It said "thirteen" from v0.10.0 to v0.13.0 while the table held fifteen
rows, was corrected to "nineteen" at v0.14.0, and was wrong again the moment v0.15.0
added three more. A number in prose beside the table it counts goes stale silently and
buys nothing — so it is gone. Count the table.)

| flag | effect |
|---|---|
| `&sheet=<id\|prefix\|first>` | auto-open the **action** sheet on the first matching needs_input session |
| `&sheet2=<id\|prefix\|first>` | auto-open the **detail** sheet on the first matching non-needs_input session |
| `&age=<minutes>` | back-date every needs_input `stateSince` so the 5 / 15 min escalation tiers can be shot without waiting them out |
| `&spark=7d` | start the sparkline on the 7-day series instead of 24 h |
| `&celebrate=1` | hold the **celebrating** mood (both arms up) so it can be photographed without waiting for a >30 min turn to land |
| `&blink=<seconds>` | fix the idle-blink interval (normally a random 8–10 s since v0.28.1; was 60–180 s) so the blink is observable |
| `&burn=1` | auto-open the **burn-by-session** sheet on the first document that lands |
| `&timeline=1` | auto-open the **Today timeline** sheet on the first document that lands |
| `&day=YYYY-MM-DD` | open the timeline and then **drill that day**, so Back has somewhere to go and the flag photographs the real navigation rather than a view that can only be closed |
| `&pin=<id\|prefix\|first>` | pre-pin one session so the sorted card and its glyph can be shot without a tap. Pins **in memory only** — a screenshot flag that wrote to the vendor store would leave the operator's own map holding a fixture session |
| `&uid=<id>` | stand in for the host-injected `uniqueId` (see the persistence note below) so the **real** storage path is exercisable off-glass |
| `&crab=<state>` | force one wardrobe state (v0.11.0). `sunglasses`, `party`, `nightcap`, `hardhat` are **held**: they outrank both the fleet's own answer and the `crabStyle` setting, so a costume can be shot against any fixture; `none` holds the bare crab. `juggle`, `bounce`, `snap` are tricks, and they are **re-fired on a loop** — a 560 ms claw snap is not a window a screenshot can be aimed at. The loop bypasses the juggle's ten minute cooldown and its five-session threshold, but **not** reduced motion and **not** quiet hours: a flag that made the panel move in a dark room would be photographing a widget that does not exist |
| `&approval=1` | auto-open the **approval** sheet on the first needs_input session carrying a `pendingPermission` (v0.12.0), so the Approve/Deny variant can be shot |
| `&action400=1` | force the older-crabd **400** on `queue-continue` and `decide` (v0.12.0), so the "not available"/no-latch inline handling is demoable without a fixture edit |
| `&swipe=<id\|prefix\|first>` | freeze one **dismissable** card mid-swipe (v0.14.0), at `&swipeX=<px>` (default **90**, past the 60 px threshold so the armed state — red left edge, thickened — is in the shot; pass a smaller number for the under-threshold rendering). The flag drives the shipping `paintSwipe()`, so the transform and the compounded fade in the shot are the ones the fingertip gets. Re-applied on every document, because the card grid rebuilds whenever its signature moves and a frozen transform lives on a node that rebuild throws away. Only `done`/`idle` cards match, which is also the fastest way to confirm a working card has no swipe rendering at all |
| `&pinflash=<id\|prefix\|first>` | pin that session and **hold** the long-press confirm (v0.14.0). Two things, not one: the flash never clears (so the glyph stays drawn), and `body.pinflash-frozen` **pauses the real `pinIn` animation at −140 ms of its 260** — the frame in the shot is one the animation genuinely produces, measured at `scale(1.235)`, not its end state. Pins in memory only, the same discipline `&pin=` keeps |
| `&ackflash=1` | run the **real** two-finger ack-all on the first document and hold its confirmation line (v0.14.0). It makes the actual POST and counts the actual acks, so the banner in the shot says what the gesture would say. On a fixture with nothing ackable it shows **nothing** — which is the no-op path, also worth a shot. `rework` is that fixture (its one `needs_input` carries a `pendingPermission`, which ack-all skips); use `attention` for the two-ack case |
| `&refreshflash=1` | hold the pull-to-refresh line (v0.14.0) without needing a 120 px drag |
| `&budget=<percent>` | put the day at that percentage of its budget, for the 100% amber and 150% red steps. It moves the **budget**, not the spend: `dailyOutputTokens` is recomputed from the fixture's own output total (and clamped to the contract's range), so the document stays self-consistent and the pace marker moves with the figure — the `&age=` discipline, in a fourth place |
| `&filter=<all\|waiting\|working\|quiet>` | set the session filter chip for the shot (v0.15.0). It sets the SAME variable a tap sets, so the mode in the picture is the real one. **In memory only** — the discipline `&pin=` keeps: a screenshot flag that wrote to the vendor store would leave the operator's own panel filtered. Applied AFTER `loadPrefs()`, so it still wins on a run that also carries `&uid=` |
| `&density=<comfortable\|compact>` | set the density chip, same discipline. Compact is a third grid row plus smaller type, so `gridCapacity()` reads 12 instead of 8 at 2560x720 — pair it with `?mock=dense` or the extra cells are empty |
| `&approvalsec=<seconds>` | stand in for the iCUE `approvalThreshold` property (v0.16.0), the way `&uid=` stands in for `uniqueId`. A dev browser has no property sheet, so without it the only observable state of the approval threshold off-glass is its default — and the whole setting is about what happens when it **moves**. It feeds `approvalPropertySec()` and nothing else, so what runs is the real baseline / touch / POST path. Boot once **without** it on a `&uid=` (the body logs no `approvalThresholdSec`), then reload **with** it on the same `&uid=` (the value has moved off the recorded baseline, so the key is in the body from then on). Clamped to 5..3600 on the way in |
| `&hold=<seconds>` | start every `pendingPermission`'s hold with that many seconds left (v0.15.0), so the approval countdown can be aimed at. The instant is **pinned on first use**, the `&age=` discipline: it then counts down in real time and reaches `expired` by itself, which is the second shot. Clamped to `APPROVAL_HOLD_SEC` on the way in — a hold longer than crabd's own would be a fixture the daemon could not have produced |
| `&sensors=<cpu>[,<gpu>][,C|F]` | stand in for the iCUE **Sensors bridge** (v0.17.0), the way `&uid=` stands in for `uniqueId`. `window.plugins` does not exist in any browser, so before this the hardware row was the one part of the Limits zone that could not be seen off-glass at all, and it is the part that decides whether that zone fits. It replaces the PLUGIN and nothing else: `refreshSensors` / `readSensor` / the 80/90 threshold colouring / `showSensor` / `markSensorZone` are the shipping ones, so the row in the shot is the row iCUE paints. Two numbers reach the amber and red steps (`&sensors=95,84`); the unit letter reaches the Fahrenheit branch, which is deliberately left uncoloured. **Mock-gated like every other flag, so the STANDALONE state, the one place the row is the only thing in the zone, cannot be combined with it** - force the classes from the console to measure that one |
| `&sensors=none` | the bridge is **here and nothing is selected** (v0.21.0) — every fresh import, because an import resets both sensor properties. `sensorIdFor` returns the same empty string an unset iCUE property gives, so what renders is the shipping path: both temperature cells gone and the `pick sensors in settings` hint in their place. It is the only way to reach that state off-glass, since every other form of `&sensors=` manufactures ids |
| `&sensornames=<cpu>\|<gpu>` | what `getSensorName` answers (v0.21.0). The forced bridge defaults to `CPU Package` / `GPU Core` — the shape a correctly-configured machine gives, so the off-glass row defaults to the row the operator ought to be looking at. Either side may be **empty** (`&sensornames=\|`) for a bridge that answers with nothing, which is the no-label rendering and a different picture. The pipe is the separator because a sensor name may well contain a comma. **Since v0.24.0 this is also the duplicate-name test** — set BOTH sides to the same string (`&sensornames=Temp+%231\|Temp+%231`, the operator's exact case) and the collision-suppression drops the label from both cells; leave them distinct (or use the defaults) and both paint. Pair with `&sensors=63,50` so the two cells hold present readings and the collision is between two live numbers |
| `&sensorsame=1` | **both** temperature properties resolve to ONE sensor id (v0.21.0) — the operator's measured defect, reproduced rather than simulated. It moves `sensorIdFor` and nothing else, so the same-sensor test, the skipped GPU read and the warning cell are all the shipping path answering about real state |
| `&hist=<rich\|empty\|error>` | which canned document TODAY's history drill reads (v0.19.0) — see the mock-history table above. `error` names a file the static server does not have, so the 404 is real |
| `&mood=<mood>` | hold one crab mood (v0.17.0): `content`, `waving`, `asleep`, `worried`, `celebrating`, and `sweating` since v0.22.0. `&celebrate=1` already did this for one mood and for this exact reason; the other four were reachable only by picking a fixture that paints them, which moves every other thing on the panel too, so a same-pose A/B of the crab ART was not possible off-glass - which is exactly what the v0.17.0 grid change had to prove. Applied AFTER the mood ladder has run, so it overrides the ANSWER and never the derivation, and validated against `MOODS` so a typo cannot paint a crab the stylesheet has no rules for |

| `&quietov=on\|off\|auto\|none` | stand in for crabd's quiet **override** (v0.22.0). It seeds the harness's DAEMON, not the widget: `applyMockQuietOverride` writes the member into the served document and honours it in `active` exactly as crabd's `quiet_state` does, so what renders is the shipping read path on a document a real companion could have sent. `none` writes an explicit `null`. A TAP then moves the same variable — which is what makes the three-state cycle demoable off-glass rather than only its first frame, and it is why the mock `/v1/action` stub applies an accepted quiet write instead of only answering 204. Pair with `&action400=1` to watch the capability latch hide the chip |
| `&touchdiag=1` | stand in for the iCUE **Touch Diagnostics** switch (v0.23.0), the way `&approvalsec=` stands in for the approval slider. It feeds `diagWanted()` and nothing else, so install / capture / coalesce / flush / remove are all the shipping path. In mock mode the flush **logs to the console instead of POSTing** (`mock POST /v1/panel-log N lines`, then the lines), the idiom `postAction` and `postConfig` already keep. The indicator is live: `diagCount` climbs as the page is driven, and `window.__sidecrabDiagLog` holds the last 400 lines for a reader in-page |
| `&host=1` | auto-open the **host history** sheet (v0.22.0). It opens through the shipping `openHostSheet`, so a fixture whose `host` block is absent or all-null (`hot`) opens **nothing at all** — the inert path, which is the one a flag must not paper over. The ring is fed one sample per poll, so the sheet lands in its `collecting - N of 10 samples` state and the charts appear ~30 s later; both are worth a shot |

**Trap (2026-08-26, v0.3.0):** `&age=` pins its instant **once**. An earlier
rolling version recomputed `Date.now() - age` on every poll, and because
`pruneAcks` drops an optimistic ack whose `stateSince` has moved, the card
silently un-acked itself one poll after the tap — the flag was misreporting the
ack path it was being used to photograph.

**Trap (2026-08-26, v0.4.0) — the rebase eats local state in mock mode.** Both
the optimistic ack and the done-card Dismiss are keyed to the session's
`stateSince`, and `rebaseMock` shifts every timestamp on EVERY poll — so in mock
mode both are pruned within ~3 s and the card silently un-acks or reappears.
Nothing is wrong with the widget: against crabd a `stateSince` only moves on a
real transition, which is exactly when both SHOULD drop. `&age=` pins
`stateSince` for `needs_input` rows, so the ack path is demoable; it does not
touch `done` rows, so to exercise Dismiss over time, freeze the feed first
(`acceptDoc = function(){}` in the console) and then drive `render()`.
**Since v0.14.0 the swipe dismiss is keyed the same way and inherits the same
trap** — a swiped card comes back within ~3 s in mock mode and does not against
crabd. The gesture test harness freezes the feed for exactly this reason.

## v0.28.2 — the cards get readable: +17% type, two-line titles, and what paid for it

Operator's ask: "make the font slightly larger on the agent cards or easier to read".
**Measured before, 2560x720 comfortable:** title 20.9 px, repo/state/event 15.5 px, five of
six fixture titles ellipsised at one line, and ~90 px of empty card height. **After:** title
24.5 px (`--fs-card-title` 2.9 → 3.4 units), meta 18.4 px (2.15 → 2.55), question 21.2 px
(2.65 → 2.95), sub-row 2.2 (was 1.95); the title wraps to TWO lines (clamp 2, pinned by
max-height, flex 0 0 auto so nothing squeezes it). Compact density keeps its own explicit
sizes and one-line titles — a third grid row has no height to give.

**What the room cost, found by scanning every card child against its cell on five fixtures
× two densities (zero overflow at the end):**

- `?mock=dense`: the approval card's badge row ran **7 px** past the cell → an approval card's
  title is ONE line (the request is what the card is for).
- `?mock=attention`: the question box was squeezed to **2.8 line boxes** of a 4-line clamp — the
  D15 mid-glyph cut, no ellipsis → `.card-question` is PINNED at three whole lines, and a
  `needs_input` card hides its subagent rows the way an approval card does (rule 6; the `N sub`
  badge still says). Selector is `.card[data-state="needs_input"]` — cards carry the state as an
  attribute, not a class (the first cut used a class and silently matched nothing).
- `?mock=rework`: a working card with three subagent rows, a queued line and four badges ran
  **19 px** past the cell — and its EVENT LINE had been squeezed to 0 px, the card's newest fact
  gone while the subagent rows stayed. → `.card-event` has a one-line `min-height` and a 2-line
  clamp with ellipsis (was a 2.5-line cut); at most TWO subagent rows show on a comfortable card;
  badges keep the pre-bump chip size (2.15 units) — four of them had wrapped to a second row.

Verified in headless Edge at 2560x720 with a FRESH profile per run: the `sc-shots` profile served
the old stylesheet for two whole passes and the "after" shots were the "before" — cache-bust the
profile (`--user-data-dir` per tag), not just the URL.

## v0.28.1 — the idle blink, every 8–10 s instead of every 1–3 min

Operator's ask. `BLINK_MIN_MS`/`BLINK_MAX_MS` 60 000/180 000 → 8 000/10 000; nothing else
moved. The gates that made the rare blink safe still hold at the new rate: calm moods only
(`content`, `sweating`), never under quiet, never under reduced motion, and the 150 ms frame is
unchanged. The original "an idle tic on a 24/7 panel is noise" reasoning was right about a tic
and wrong about a blink — at one to three minutes the crab read as a still image, and a blink is
the cheapest sign of life a panel has.

## v0.28.0 — the finish dance: shades on, four beats, when an agent lands

Asked for by the operator: "a little dance with his sunglasses on when an agent finishes
working". It rides the SAME `working -> done` edge `detectCelebration` already walks, so it is
one-shot per transition by construction, and it is bounded three ways so a busy fleet is not a
crab that never stops:

- **a real turn** — `DANCE_MIN_TURN_MS` (20 s). A three-second reply is not a job landing;
  the half-hour celebration keeps its own, much higher, bar and the two compose (a long turn
  gets the arms-up mood AND the dance);
- **one per cooldown** — `DANCE_COOLDOWN_MS` (30 s), the juggle's idiom;
- **never beside an alert** — `anyWaiting()` on the same document; a landing that arrives
  next to an open question is a quiet landing. Alerts stay the only thing moving.

The shades are the wardrobe's: `fireDance` sets `danceUntil` and `applyWardrobe` wears
`sunglasses` while it holds, WITHOUT touching `accCurrent` or the hysteresis timer, so the
fleet's own answer is back the instant the music stops (the trick's timeout calls `render`).
`plain` wardrobe dances bare-shelled: the switch is about costumes, not motion. Skipped
outright under reduced motion and quiet, like every trick, and the CSS carries the belt for
both. Motion is `crabdance`, 390 ms × 4, whole-cell `steps(1, end)` — slide left, hop, slide
right, hop.

Verified off-glass with the trick guards exercised directly (reduced motion overridden in the
page, since this dev browser has it ON): the edge dances and wears the shades; the shades are
off again after `DANCE_MS`; a waiting session, a 5 s turn, the cooldown and quiet each refuse.
`&crab=dance` holds it for photographs.

## v0.27.1 — 0.27.0 shipped BLANK on the Edge: a property and a function shared a name

**Trap, with mechanism and symptom.** iCUE injects every `x-icue-property` into the page as
a same-named GLOBAL, declared with `let`/`const` semantics. 0.27.0 added the property
`panelToken` AND a reader `function panelToken()`. In a plain browser (no injection) the
widget rendered perfectly — every check passed — but on the Edge the script hit
`SyntaxError: Identifier 'panelToken' has already been declared` at parse time, so NOTHING
ran: a blank panel with the static markup only, and crabd's originsSeen showed the widget's
polling simply stop after the import. Reproduced off-glass by prepending
`<script>let panelToken = "";</script>` to the page: the 0.27.0 script defines nothing, the
renamed one renders six cards. **Rule: a property name is reserved in the script's global
scope — never declare a function or top-level `var` with a property's name.** The reader is
now `pairingCode()`; the property keeps `panelToken`. The mock/browser checks cannot catch
this class of bug; only injection can, so the `?mock=` pages should grow a `&icue=1` switch
that pre-declares every property with `let` (next wave).

## v0.27.0 — the pairing code: a decide only the widget can send (SEC-a / WID-a closed)

crabd 0.29.0 refuses `decide` without the pairing code and the request's id. The widget's
half is small on purpose:

- **`panelToken` iCUE property** ("Approval Pairing Code", textfield, default empty). Read
  LIVE on every decide via `strProp` — the operator types it into iCUE while a request may
  already be on the card, and a boot-time cache would refuse the very tap they are making.
- **`tokenRequired()`** reads `approvals.tokenRequired` off the last good document. An older
  crabd has no `approvals` block and is never asked for a code.
- **`onSheetDecide`**: unpaired against a crabd that requires it = the sheet STAYS OPEN and the
  notice says why (`not paired — set Approval Pairing Code in widget settings`); nothing goes on
  the wire. Otherwise the sheet closes optimistically as before and the body carries `token` +
  the displayed request's `requestId` (WID-a). `403` / `409` / `429` each get their own notice
  line — a wrong code and a replaced request are different mistakes with different fixes.
- Mock mode is unchanged: `postAction` never leaves the page there, so every fixture still
  demos the sheet without a code.

Not done, deliberately: an on-glass keypad for the code. iCUE's property panel is a desktop
keyboard and a textfield is a property type the import validator has accepted; a keypad would
be new surface with a real mis-tap risk for a value typed once.

## v0.26.0 — warm-on-warm: the ring got its lightness back (AUD-F1 closed)

> **Version label is provisional.** `manifest.json` is deliberately NOT bumped in this
> branch — the final number is assigned when the lanes merge. Every `v0.26.0` tag in
> `sidecrab.css`, `index.html` and `sidecrab.js` is that provisional label and needs one
> sweep if the number changes.

The 2026-08-28 design audit's AUD-F1 said four warm hues carry four jobs — brand
`--accent`, alert `--amber`, escalation `--red`, and a permanently orange mascot — and
asked whether the attention ring survives a grayscale check. Measured, it does not: at
two of the panel's four attention states the ring is **invisible against the crab in
monochrome**, and the reason is not hue crowding. It is that the escalation ramp walks
the ring toward red, and red is the luma-poorest corner of sRGB.

### What the measurement said

Headless Chrome 151 over CDP, device metrics pinned to 2560x720, fresh tab per capture,
sample regions located by their own DOM rects in the same load as the shot. Gray is the
8-bit value a Pillow `convert('L')` produces (ITU-R 601 on gamma sRGB) — i.e. what a
monochrome photo of the glass shows.

| state | fixture | ring line, gray | crab body, gray | **separation** |
|---|---|---|---|---|
| waiting (base) | `?mock=attention` | 172 | 127 | 45 |
| escalated (tier 2) | `?mock=attention&age=20` | 140 | 127 | **13** |
| approval | `?mock=dense` | 120 | 127 | **7** |

**The ramp was inverted.** The calmest tier had the ring that survived; the two loudest
had one that did not. And it is a gamut fact, not a taste one: at the chroma these lines
carry, an in-gamut sRGB colour tops out near gray 167 at hue 44 and 164 at hue 32,
against 172 for the amber base — so escalating by hue alone can only walk the ring DOWN
in lightness, straight through the crab's band at 127.

### The fix: the line's job is lightness, the halo's is hue

Both escalated glow lines keep the oklch hue and chroma they had and gain L only:

| token | old | new | oklch move | gray |
|---|---|---|---|---|
| esc2 glow line | `#E0703C` | `#FF8D59` | L 0.667 → 0.758, C 0.1545 → 0.1535, H 43.86 → 44.29 | 139 → 169 |
| approval glow line | `var(--red)` `#D4553F` | `#FF7D64` | L 0.612 → 0.736, C 0.1649 → 0.1636, H 31.95 → 32.37 | 120 → 161 |

Nothing about which tier means what moved — same hues, same chromas, same halos, same
`--glow-line` / `--glow-halo` mechanism and the same literal-rgba halo idiom (QtWebEngine
still cannot alpha a variable colour inside a `box-shadow` list). The approval line is a
literal rather than `var(--red)` on purpose: `--red` is a LABEL colour that sits beside
text (the stale banner, the approval spine, Stop), this is a 5 px line read across a
room. Same hue, different job, different lightness. `.badge-esc`, the `cardPulseEsc2`
keyframe and the reduced-motion `.card.esc2` border took the same lift, so tier 2 is one
colour on the card and on the panel edge.

### `--accent`: `#CC785C` → `#BE7E6E`

The old default sat **on top of the mascot** — oklch H 39.15 against Claw'd's 39.51,
0.36 degrees apart, with only 0.068 of oklab dE between them. New value: H 34.72,
C 0.1131 → **0.0838**, L 0.6580 → 0.6553.

| accent vs | oklab dE before | after | change |
|---|---|---|---|
| crab `#E45C28` | 0.069 | 0.098 | **+42%** |
| esc2 line | 0.044 | 0.077 | **+75%** |
| `--red` | 0.072 | 0.092 | +28% |
| `--amber` | 0.132 | 0.142 | +8% |

Chroma is now the panel's loudness rank — crab 0.181 > red 0.165 > esc2 0.154 >
amber 0.140 > **accent 0.084** — so nothing branded can be mistaken for a signal.

**Lightness was deliberately NOT moved** (gray 141 → 143), and the sweep is why. The
panel's gray axis has no free slot: crab 127, esc2 139, green 150, muted 157, amber 172.
A search over H 8–70, C 0.06–0.14, L 0.56–0.76 under a 4.5:1 contrast floor on
`--surface` could not find an accent whose worst separation beat ~0.5 normalised units —
every candidate that bought crab separation in lightness spent the alert-vs-brand
separation the same audit demands, or landed on `--muted` (killing the documented
accent-vs-muted `#notice` ack/pull pair) or on `--green` (killing working-vs-done spines).
**Hue and chroma are free on this panel; lightness is not.** Contrast on `--surface` is
unchanged at 5.5:1.

The default is stated in THREE places and all three moved together — `:root` in
`sidecrab.css`, the `accentColor` property meta in `index.html`, and the `strProp`
fallback in `applyProperties()`. The JS one wins at runtime, so a partial edit renders
one colour in a browser and another on the glass. The iCUE override path is untouched.

### Three reclassifications: the band is for state

The audit asked for the amber→red band to be reserved for state. What the inventory
found was the mirror problem — brand chrome wearing the ALERT idiom:

1. **`.card-turn`** (the "working 14m" pill) was filled with `rgba(204, 120, 92, 0.14)` —
   a warm wash on a card, inches from the amber spine of a waiting one, and a warm wash
   is what the alert flash is. Now `rgba(237, 231, 223, 0.07)`, the panel's own neutral
   plate. Ink stays `var(--accent)`.
2. **`.sheet-btn-deny`** was filled with `rgba(204, 120, 92, 0.16)` — a warm plate under
   the safe option on the panel's one irreversible decision, beside an amber Approve.
   Now `rgba(237, 231, 223, 0.16)`: still filled, still primary, no longer warm.
3. **`.sheet-panel`'s base spine** was `var(--amber)` and only rendered correctly because
   every mode except `action` overrides it. The alert colour was the inherited default,
   so a mode added without an override would have opened with an alert spine over a burn
   chart. Base is now `var(--accent)`; `.sheet[data-mode="action"]` states the amber,
   which is the one sheet JS opens for a `needs_input` session.

(1) and (2) also fixed a second defect each: both literals were hardcoded copies of the
pre-AUD-F1 accent, so they went stale the moment the token moved **and never tracked the
iCUE `accentColor` override at all** — a user who picked a green accent got green ink on
an orange plate.

### Verified

`node --check scripts/sidecrab.js`, strict-XML parse of `index.html`, `icuewidget
validate widget` (CLI 0.4.45) clean — the `icueEvents` warning is the known false
positive. No fixture was touched. (Trap banked while writing the property comment: an
XML comment may not contain `--`, so a CSS variable name spelled out inside one fails
the strict parse.)

Grayscale proof, same rig as the baseline, before vs after:

| separation | before | after |
|---|---|---|
| ring vs crab, base | 45 | 45 |
| ring vs crab, esc2 | 13 | **42** |
| ring vs crab, approval | 7 | **34** |
| alert spine vs brand spine (`?mock=hot`, steady) | 30 | 29 |
| ACKED badge vs FAST badge | 30 | 30 |

The two failing states are fixed and the two passing ones did not pay for it. Sheet
spines re-read off the ENGINE's computed style per `data-mode` after the base-rule move:
`action` `rgb(232,163,61)`, `detail` follows `--sheet-accent`, `burn`/`forecast`/`host`/
`timeline`/`overflow` and an unknown mode all `rgb(190,126,110)` — no existing mode moved.

Overflow spot-check (pure colour work, but the sheet base rule changed): five fixtures
(`hot`, `attention`, `rework`, `dense`, `quiet`) x three slots (2560x720, 840x696,
416x696) = 15 captures, **page overflow 0x0 on every one**. Quiet-hours dimming
re-checked on `?mock=quiet`: `--dim` still lands on the new colours and the glow is still
withheld.

**Measured, not fixed, and left as one row for the next wave:** the base-tier alert spine
is a PULSE, so between keyframes it passes through `--surface-line` — sampled mid-phase
on `?mock=attention` it read gray 134 against a working spine's 143, i.e. the waiting
card's spine is momentarily darker than the working card's twice per 1.8 s cycle. That
is the pulse doing its job over time, not a colour defect, and the ESCALATED/ACKED text
badges carry the state either way.

## v0.26.0 — the design audit's Limits half: a second window, a hierarchy, a 31 px control, an inherited invariant and a four-digit k

> **Version label is provisional.** `manifest.json` is deliberately NOT bumped in this
> branch — the final number is assigned when the lanes merge. Every `v0.26.0` tag in
> `sidecrab.css`, `index.html` and `sidecrab.js` is that provisional label, the same
> discipline v0.25.0 records one section down, and needs one sweep if the number changes.

Five rows from the 2026-08-28 design audit: **F2** (two `limits.extra` windows overflow),
**F3** (the sensors row is a 31 px tap target), **F4** (the TODAY hierarchy), **F5** (pin
the clamp invariant) and **F6** (`fmtNum(999999)` → `1000k`). F1 is the other lane's.
Every figure below is off the DOM at the slot named, headless Edge, device metrics pinned,
`--force-prefers-no-reduced-motion`, fresh tab per capture.

### F2 — the second window, and the fixture that finally carried one

The contract has allowed two extra windows from the start (`extras.slice(0, 2)`), no
fixture carried two, and so the overflow stood as a v0.17.0 estimate (78.6 px, from a
window injected by hand) that nothing re-measured. `?mock=extras` carries two now, and the
estimate was **low**:

| | 2560x720 | 2536x696 |
|---|---|---|
| zone content box | 675.36 | 652.85 |
| content wanted, two windows | 768.39 | 744.84 |
| **spare** | **−93.03** | **−91.99** |
| sensors row past the content box | **99.45** | **98.20** |

`body { overflow: hidden }` eats all of it silently, which is the v0.17.0 failure exactly:
no scroll, no error, the hardware row simply not on the glass. The second window costs
**98.61 px** on its own (79.89 + an 18.72 gap), and 100.77 when it carries a rendered
forecast line — which this fixture's does deliberately, an `exhaustAt` before the reset
being as contract-legal as one after it and the worse of the two.

**Option A, and what it actually had to drop.** The audit's recommendation was to collapse
the TODAY stat row to one line. Measured, that row is worth **20.00 px** of the 93.03 — the
four-cell grid is 46.0 and one line is 26.0 — so the collapse also drops the **sparkline**,
78.30 px plus its 6.48 gap. Both halves are load-bearing: the sparkline alone misses by
8.25 px, the stat row alone by 73.03, together they free **104.78**. After:

| | 2560x720 | 2536x696 |
|---|---|---|
| `.today` height | 206.17 → **101.41** | 200.75 → **98.75** |
| **spare** | **+11.73** | **+10.01** |
| sensors row past the content box | 0.01 (subpixel) | 0.01 |

What survives: the TODAY label, one stat line (**OUT** and **MSGS**, the two figures F4
keeps at stat size) and the budget and cost lines — 24.48 px between them, and the only
place the panel says the day is over budget and what it cost. What goes: IN, CACHE RD and
the 24 h series. **That is a real loss, not a re-route** — the burn sheet is per-session
output, by-model and commits, and carries none of the three. It is the same trade the
840x696 layout has made since v0.25.0, and it is confined to the panel actually being
served two windows.

**Presence-driven, never slot-driven.** `renderLimits` toggles `body.limits-two-extras` off
`extras.length > 1` in the served document; the stylesheet owns what goes. A media query
would collapse a one-extra panel that fits perfectly well. At 840x696 and 416x696
`.gauge-extra` and `.today` are both `display:none`, and 840x344 hides the zone outright —
so a second window costs those three slots nothing and the collapse is invisible there.

### F4 — the TODAY row, permanently

Four figures at stat size made **CACHE RD** — 19.6M on `rework`, the largest number in the
zone and the cheapest one — the loudest thing in it. IN and CACHE RD now carry `.stat.meta`
(23.04 px → 15.12 at 2560x720; 22.272 → 14.616 at 2536x696). Permanent, not conditional:
the hierarchy was wrong at every size.

The demotion is bottom-aligned (`align-items: end` on `.today-grid`) and that is the half
that matters. A demoted cell is **8.0 px** shorter (38.0 against 46.0), and a stretched cell
starts its content at the TOP — which would have put two of the four KEYS 8.0 px above the
other two, a ragged label row under four numbers. Measured after: **kTop 492.38 and kBase
508.38 on all four**, the grid still **46.0**, `.today` still 206.17. So the demotion frees
the zone **nothing at all** — worth saying plainly, because the audit's F4 row reads as if
it buys F2 room. It does not buy F2 *pixels*; it buys F2 the *pair of cells* the collapse
can hide, and leaves F2 the whole of its own 104.78. What stays staggered is the value
baselines, by the two fonts' descent difference: 487.38 against 488.38, **1.0 px** at
2560x720 and 2.0 at 2536x696.

### F3 — a 31 px control, and why the row could not grow

`.sensors` becomes `.tappable` when the feed serves a host figure to have a history of, and
then paints **31.47 px** at 2560x720 (31.25 at 2536x696 and 840x696) against a
`--touch-min` of **60.48** (58.46 at both 696-tall slots). It was the one control on the
panel under the fingertip floor.

**Growing the row is not available, and that is a number.** The Limits zone's whole slack
budget is `.today`'s auto margin — 20.04 px on `?mock=rework` at 2560x720, 16.71 at
2536x696 — and `min-height: var(--touch-min)` costs 29.01 and 27.21. It overflows the zone
by **8.97** and **10.50 px**: the v0.17.0 failure re-created, in the zone that wave spent
77 px rescuing. (At 840x696 there IS room — 125.34 px clear — so growth would have fitted
there and nowhere else, and a row whose height depends on which glass it is on is worse
than one mechanism everywhere.)

So the **target** grows and the row does not: `.sensors.tappable::before`, out of flow,
`height: var(--touch-min)`, centred on the row, costing the flex column zero. Measured by
**hit test** rather than by box arithmetic — `elementFromPoint` stepped down the row's own
column, asking the document what a fingertip there would reach:

| slot | row painted | reachable band | clear above | inside the zone below |
|---|---|---|---|---|
| 2560x720 | 31.47 | **60.47** (floor 60.48) | 5.39 to `.today` | 7.53 |
| 2536x696 | 31.25 | **58.45** (floor 58.46) | 5.47 | 7.56 |
| 840x696 | 31.25 | **58.45** | 124.56 | 7.56 |
| 416x696 | not rendered | — | — | — |
| 840x344 | zone hidden | — | — | — |

The two narrow slots need nothing: v0.25.0's portrait strip drops the sensors row entirely
(`display:none`, so the class is still toggled and there is no box to grow) and 840x344
hides the whole zone. The band ends inside the zone's own bottom padding at every slot, and
the only thing above it is `.today`, whose sparkline is itself a tap target — 5.39 px of
clear air between them. **Non-tappable rows are untouched**, measured: 32.00 px of hit band
on `?mock=normal&sensors=72,54`, the row itself and nothing more. Pseudo-elements are not
event targets, so the row is still the click's target and `onSensorsClick` did not change.

### F5 — the clamp invariant was INHERITED, not held

The verdict first: **not guaranteed**. Every part of the widget was correct and the
guarantee lived in another process. `sortPinned` reads its state bands out of the order
crabd delivered (first appearance of each state) and is deliberately the identity with
nothing pinned; `filterSessions` and the dismissal pass both preserve order; the clamp was
`shown.slice(0, capacity - 1)`. Feed that code a document whose sessions are not pre-sorted
— 12 done rows, then one `needs_input`, capacity 8 — and the waiting card lands in the
"+N more" tail. Proven by test, not argued: `bareSliceClamp` in
`widget/tests/test_ordering.js` is that exact code, and the suite asserts it VIOLATES the
invariant on that feed.

The clamp is now `clampGrid(list, capacity)`: waiting rows survive first, everything else
fills what is left, and the order of both lists is the order it arrived in. Not a second
copy of crabd's band list — it names ONE state, the one the panel is for — and it is the
discipline `recapLine` already keeps ("crabd sorts commits count desc, but the max is taken
here rather than trusting position"). On any contract-conforming feed it is byte-for-byte
the old slice, which both the fingerprint sweep and a per-capacity equality check assert.
With more waiting rows than cells, waiting rows ARE cut — there is no cell to put them in —
and CD-14's tile is the route to them; what cannot happen is a waiting row cut while a done
or idle row keeps a cell.

**`widget/tests/test_ordering.js` is the widget's first node test**, and its shape is
deliberate: the SHIPPING `scripts/sidecrab.js` is loaded whole into a `vm` context with a
document stub whose `readyState` is `'loading'` — the branch a real browser takes before
DOMContentLoaded, so `init()` parks on a listener nobody fires, nothing renders, and the
functions under test are the ones on the glass. A second copy of the ordering rules in a
test file would be a copy that can disagree with the panel. If it ever stops loading, the
cause is new TOP-LEVEL work in `sidecrab.js` (everything else lives inside a function): add
the stub it needs, do not fork the logic. 140 checks, mutation-proven in both directions —
the pre-0.26.0 clamp fails the invariant by construction inside the suite, and flipping the
shipping `needs_input` test to a state that never matches kills 4 checks.

### F6 — the four-digit k

`fmtNum(999999)` painted **`1000k`**: the k branch rounds, and `Math.round(999999 / 1e3)` is
1000. The M branch now starts at **999500**, where the k branch's own rounding reaches four
digits — the same rule carried one unit further rather than a second rule. **Not** floored
to `999k`: that would be a different rounding rule for one bucket, and 998,700 already
paints `999k`, so a larger number would have painted the same string. `999499` → `999k`,
`999500` → `1.0M`, `999999` → `1.0M`, `-999999` → `-1.0M`; `1954200` and `19640000` are
unchanged. The diag chip's five-character width budget is unharmed — the widest string
`fmtNum` can hand it is now four characters, and the `999k+` clamp above
`DIAG_COUNT_SHOWN_MAX` (999999) is untouched and pinned by test. Left alone, deliberately:
`fmtNum(9999)` is `10.0k` where `fmtNum(10000)` is `10k` — two renderings of one value, but
neither is a wrong number and it is not this row.

### Verified

`node --check scripts/sidecrab.js`, strict-XML parse of `index.html` (ElementTree AND
minidom), JSON parse of all 20 fixtures + manifest + translation, `icuewidget validate
widget` (CLI 0.4.45) clean with the known `icueEvents` false positive, and
`node widget/tests/test_ordering.js` — **140/140**.

> **Trap the XML gate caught:** `--` is illegal inside an XML comment, so a comment naming a
> custom property (`--fs-stat`) fails the strict parse `index.html` has to pass. The CSS and
> the JS may name tokens; an HTML comment may not.

Headless Edge, `--force-prefers-no-reduced-motion`, fresh tab per capture, device metrics
pinned, served from this folder over `http://127.0.0.1`:

- **Overflow probe, 78 captures: page overflow 0x0, worst zone overflow 0.00, no
  offenders.** Thirteen fixtures x five slots (2560x720, 2536x696, 840x696, 840x344,
  416x696), plus 13 combinations on the new fixture: `&sensors=72,54` and
  `&sensors=95,84&sensornames=CPU Package|GPU Hot Spot` (the widest the row can paint),
  `&density=compact` at three slots, `&touchdiag=1`, `&sheet=first`, `&burn=1`, `&age=20`,
  and 416x400 / 600x400 for the short-slot fallback. Every zone measured against its own
  border box, descent stopped at scroll containers. (The border box, not the content box:
  the two clock chips anchor outside the identity zone's padding box on purpose since
  v0.24.1, and a content-box probe reports them as 28.80 px offenders at HEAD.)
- **Geometry fingerprint, 60 captures before and after** (every element in `body`, display +
  x/y/w/h to 0.01 px, HEAD served beside the working tree from a second static server). The
  ONLY geometry differences anywhere are **the two demoted stat cells and their values, at
  the two slots where TODAY renders** — 44 element rows at 2560x720 and 44 at 2536x696, with
  one capture identical at each because `?mock=future` is a dead feed with no zone. **The
  three narrow slots are geometry-identical**: `.today` is hidden there, so the demoted cells
  measure 0x0 in both and only the class STRING differs. Everything else that moved is the
  1 Hz animation phase v0.25.0 already documents — the writing tick's `w2` class and its dot,
  and the juggling crab's ball positions.
- **The collapse is inert without a second window**, which that same sweep is the proof of:
  no fixture but `extras` sets `body.limits-two-extras`, and the sparkline and the four-cell
  grid are byte-identical on the other twelve.
- **Tap targets**: the sensors row's reachable band measured by hit test at every slot (the
  table above); non-tappable rows unchanged at 32.00.

**Not measured, and deliberately: the live standalone state.** Reaching it means letting the
panel poll `127.0.0.1:2722`, which this lane is barred from contacting. In that state the
feed serves no host block, so the row is not `.tappable` and F3's rule does not reach it.

## v0.25.0 — the narrow slots get real layouts (CD-33 closed)

> **Version label is provisional.** `manifest.json` is deliberately NOT bumped in this
> branch — the final number is assigned when the lanes merge. Every `v0.25.0` tag in
> `sidecrab.css` and `index.html` is that provisional label and needs one sweep if the
> number changes.

CD-33 (v0.20.0) shipped the honest minimum for the sub-3:2 slots: both real zones stay
hidden and a two-line **core line** under the clock admits they are gone. Its own note
said what was left — *"Recorded as future, not done: full responsive layouts for the
sub-3:2 slots. That is a project."* This is that project. The two sub-3:2 slots in the
verified matrix now have real layouts, the core line is scoped down to the one narrow
slot that still cannot hold them, and the 36/64 clock overhang CD-33 found is fixed as
a class rather than as one rule.

### What the measurement said before a line was written

Headless Edge at HEAD (`b461748`), `--force-prefers-no-reduced-motion`, device metrics
pinned, DOM read over CDP, twelve fixtures:

| slot | ratio | identity zone | crab PAINTED | limited by | slack going spare |
|---|---|---|---|---|---|
| 840x696 | 1.21:1 | 840 x 696 (the whole panel) | 381.1 x 322.5 | HEIGHT, 0 px | **403.2 px of width** |
| 416x696 | 0.60:1 | 416 x 696 (the whole panel) | 382.8 x 323.9 | WIDTH | **149.7 px of height** |

Limits and Sessions were `display:none` at both. That is the finding the whole wave
turns on: **each slot already had the room, in the axis the crab was not using.**

### The three queries, and why they do not overlap

CD-33 was a single `@media (max-aspect-ratio: 3/2)` block whose one bug was a rule
beaten by source order at identical specificity. The replacement is three queries whose
conditions cannot both match, which removes that failure mode from the section:

| condition | layout |
|---|---|
| ≤3:2, height ≥ 421, width ≥ 641 | **NEAR-SQUARE** — identity band across the top, Limits + Sessions side by side under it |
| ≤3:2, height ≥ 421, width ≤ 640 | **PORTRAIT** — all three stacked |
| ≤3:2, height ≤ 420 | **TOO SHORT for three zones** — the CD-33 core line, unchanged |

421 px is not invented: it is the 420 px `max-height` breakpoint this stylesheet already
uses for the short slot, read from the other side. A shared block carries what both real
layouts do and **comes first on purpose**, because the two family queries are strict
subsets of its condition and source order is the only thing that lets a family override
win at equal specificity.

### 840x696 (near-square) — a band, then two zones

`.zones` wraps: the identity zone takes the whole first line at `height: 29%`, and
Limits (37%) and Sessions (63%) share the second at 70%. No wrapper element, because a
wrapper would have to exist at the authored 2560x720 slot too.

**What it shows.** The band is a row — crab (171.6 x 145.2 painted, height-limited) and
the clock. Limits keeps **the two windows, their depletion forecasts, the caveat note
and the hardware row**. Sessions keeps its header, the filter chip, the count, and a
**2 x 2 grid of four cards** (227.7 x 171.9), each with state, age, title, repo and the
question or the approval body.

**What is hidden, and the number that decided it.** On `?mock=rework` — the fixture
carrying every field at once — the untrimmed Limits zone wants **649.9 px** of content
in **451.0** available (head 48 + 5-hour 96.8 + weekly 77 + the extra window 77 + the
note 19.7 + the Today block 220.8 + sensors 31.3, plus 79.3 of gaps). Dropped: the
**extra window**, the **Today block** (stats, budget, cost) and the **sparkline** —
324.2 px, every one of them reachable in the burn sheet on a tap of the header. Measured
after: **325.7 px against 451.0, 125.3 px clear** on the widest fixture. The provenance
tag goes too, for a width reason: the 254.1 px header held `LIMITS` (64) + tag (89) +
hint (113), and the hint was cut to 80 px reading `by sess…` — which takes the chevron,
the header's only affordance, inside the ellipsis with it.

### 416x696 (portrait) — three stacked

A flex **column**, not the wrap trick, so `.zone-limits` can size to its own content and
Sessions takes everything left over — including the whole strip when the standalone
state drops it, with no height percentage to restate.

**What it shows.** A 19% band (crab 99.8 x 84.4, width-limited, beside the clock), a
**gauge strip** — the two windows side by side at 186.0 px each, top-aligned so their
tracks share a baseline — and a **1 x 3 column of cards** (382.8 x 127) under a header
that keeps the label, the count and the filter chip.

**What is hidden.** Everything in the strip except the two gauges: the zone header, the
extra window, the note, the Today block, the sparkline and the sensors row. The strip is
a ROW, so every survivor takes width off the two windows rather than height off the
panel — measured with the sensors row left in, the gauges dropped to **85.8 px each
against 186.0**. The burn-by-session sheet loses its route at this slot; each gauge is
still its own tap target for its own forecast. The band is 19% and not more because the
crab here **cannot spend height** — it is width-limited at 84.4 px in the 99.8 px column
the clock leaves it, so every extra percent would be band the animal cannot use and the
Sessions zone can.

### The core line is now scoped to the slot that still needs it

`display:none` at both real layouts — the zones are on the glass there and the line
would be a second, shorter copy of what they say. It stays **shown, unchanged**, at
`≤3:2 and height ≤ 420`: measured at 416x400 the near-square band alone wants 202 px of
a 400 px panel and leaves Sessions 108 px against a 60 px header, which is the CD-33
state with extra steps. Verified rendering there at 416x400 and 600x400 on two fixtures.
Nothing in JS knows any of it — the line is rendered on every slot and the stylesheet
decides, exactly as CD-33 built it.

### The 36/64 clock overhang, fixed as a class

CD-33's defect: `body.connecting:not(.has-sensors) .zone-identity { flex: 0 0 36% }` is
(0,3,1) and out-specifies any bare `.zone-identity` in a media query, so at 416x696 the
standalone zone came out 150 px wide holding a 177 px clock — 42.4 px of overhang (45.0
at 840x696). It was fixed by restating that one selector inside the query. The same
problem returns for every new narrow layout, so **all three standalone rules are
restated in both family queries** (`.zone-identity`, `.zone-grid`, and the Limits zone's
own display), and the short-slot query keeps CD-33's original restatement verbatim.
Verified with the two body-class combinations forced over CDP at both slots, and on the
`future` fixture, which reaches the same rendering from a real document.

### Six things the layout broke, found by measuring it

Every one of these is a number off the host, not a review comment.

| | what happened | fix |
|---|---|---|
| the badge won the cascade | `.brand-badge-wrap { display: flex }` lived at the very BOTTOM of the stylesheet, so it beat the band's `display: none` on source order at identical specificity; the badge painted 334.1 px wide and took the crab's column to **zero** | the block MOVED above the responsive section, the same remedy the core-line block already documents. Nothing in it changed |
| the crab collapsed | `.crab { width: 100% }` inside an auto-basis flex item is circular and resolved to 0; the clock overflowed the zone by **214 px** to the left | `flex: 1 1 0` on the wrap |
| the crab hung out of the band | the v0.17.0 `translateY(1.4vmin)` nudge is free where the wrap has slack; in the band the wrap IS the content height, so it pushed the animal **8.8 px** out at 840x696 and 4.8 at 416x696 | `transform: none` at these slots |
| the chips landed back on the clock | both chips anchor ±`--space-pad` OUTSIDE the clock row's padding box (v0.24.1) — correct while `.clock` was `width:100%` and that edge WAS the zone edge. In the band the row is only as wide as `12:12 23`, re-creating the exact defect 0.24.1 fixed | padding on the ROW, sized per chip: right `1.2 x --touch-min` (70.2 / 57.6) against a **76.5 px** moon chip on `?mock=quiet`, left `6.5 vmin` against a **36.6 px** diag chip. The diag chip is also re-anchored to `left: 0` — its old reach was 12.5 px into the crab's column — and the moon chip pulled to `-0.55 x --space-pad`, because in this band the zone spans the full width and `-(--space-pad)` put its right border exactly on the panel edge (840.0 of 840, 416.0 of 416) |
| the waiting count was ellipsised away | at 840x696 the header is 473.5 px and label + hint + three chips is 493.5 of it, so `.session-count` — the only flexible item — was squeezed to nothing and CD-34's amber `1 waiting` fragment hung **141.6 px** past the zone, clipped | hint + density + history chips dropped (the filter chip stays: it is the one control that changes what a short grid shows), and the count **wraps instead of ellipsising**. The base rule's reason for ellipsising — "a two-line header would shorten every card" — is not true here: `.grid-head` already carries `min-height: --touch-min` (58.5 / 48.0) and two lines of `--fs-meta` are 34.0 / 20.0. Measured after: header height unchanged, no card lost a pixel |
| the approval card's countdown was clipped | the tallest card the panel builds: **182.7 px of content in a 147.7 px cell** at 840x696 on `?mock=rework`, 22 px sliced off the bottom, and the bottom is the line that says whether a tap still reaches the hook | the repo line goes on THAT CARD ONLY at both slots (the `.card.approval .card-subs` idiom one section up), and the summary drops to one line **at 840x696 only** — the portrait cell fits the untrimmed card with 15.0 px to spare, so the full summary survives there. Under `&density=compact` at 840x696 the same card wanted 123.6 in 100.5, so the `PERMISSION REQUEST` label goes in that combination too |

Two smaller ones from the same sweep: the two portrait gauges are the only tap targets
left in that zone and measured **186.0 x 45.9**, 2.1 px under the 48 px fingertip floor
— they now carry `min-height: --touch-min`; and `.sensors.shown` / `.limits-note.shown`
are (0,2,0) and beat a bare `.sensors` / `.limits-note` hide no matter where it sits, so
both are restated at their own specificity (measured before the restatement: both
painted in the portrait strip).

### Rule 6, and what it cost

The band is a row, so width is the crab's and the wordmark's to argue over. **The
Claw'deck plate goes at both narrow layouts**: at 416x696 the 382.7 px of content
carries a 273.8 px clock row and leaves the crab a 99.8 px column, and the badge at even
a 24 vmin cap is another 99.8 — the crab's whole column and 12.6 px more than the row
has. The plate is a wordmark; the crab is the product on this glass. It is untouched on
every 2560x720 store shot. The **date** goes with it: at 416x696 `.clock` is sized by
its widest child and the date is that child (278.0 against the clock row's 250.1), and
at 840x696 it is what makes the band overflow on `?mock=quiet` — clock row 104.4 + date
35.4 + the quiet note 22.6 is 162.3 px of clock in 146.1 px of band, and the note, which
says *why* the panel is dim, is the one of the three that cannot be inferred from
anything else on the glass. The **fleet dots** go for the 25 px they cost, as they
already do at 840x344.

### Verified

`node --check scripts/sidecrab.js`, strict-XML parse of `index.html`
(`xml.dom.minidom`), JSON parse of every fixture + manifest + translation: all pass.
`icuewidget validate widget` (CLI 0.4.45) clean — the `icueEvents` warning is the known
false positive. **No JS was changed**: `gridCapacity()` already reads both grid axes off
the computed style, so the 2x2 and 1x3 grids and their capacities follow the stylesheet
and nothing in the widget learns which slot it is on.

Headless Edge, `--force-prefers-no-reduced-motion`, fresh tab per capture and per
gesture run, device metrics pinned to the slot:

- **The full overflow probe: 78 captures, page overflow 0x0, worst zone overflow 0.02,
  no offenders.** Twelve fixtures x five slots (2560x720, 2536x696, 840x696, 840x344,
  416x696), plus at the two new slots: three fixtures under `&density=compact`, two
  under `&touchdiag=1`, and both standalone body-class combinations forced over CDP;
  plus 416x400 and 600x400 on two fixtures for the short-slot fallback. Every zone
  measured against its own box, descent stopped at scroll containers. The 0.02 is the
  badge-wrapper/crab-wrapper subpixel rounding that is also there at HEAD.
- **An OVERLAP probe, because the overflow probe cannot see one:** the two chips are out
  of flow and can only sit ON something rather than escape a zone. **Zero intersection
  in all 78**, against the clock hours, the seconds, the date, the core line and the
  crab; minimum clear-to-clock **5.2 px**. The crab is measured on the **ART**, not on
  the SVG box — the box is 368.3 px wide at 840x696 for a 171.6 px animal, and measuring
  it reported a 421 px² "overlap" with a chip nowhere near the drawing.
- **No card is clipped by its own cell in any of the 78**, comfortable or compact.
- **The three unchanged slots are byte-identical.** A full geometry fingerprint (every
  element in `body`, display + x/y/w/h to 0.01 px) taken at 2560x720, 2536x696 and
  840x344 x twelve fixtures, before and after, from the same server: **36 captures, zero
  layout differences.** The 13 that differ at all differ only in the writing-tick's `w2`
  class and the juggling crab's ball positions — 1 Hz animation phase, not geometry.
- **Sheets at both new slots:** burn, timeline, host and approval opened by their dev
  flags — all four open, page overflow 0x0 at both.
- **Gestures, fresh tab each** (the reused-tab trap): a card tap opens the action sheet
  and moves focus to `BUTTON#sheetClose`; a gauge tap opens the forecast sheet titled
  *5-hour window*; a grid-header tap opens the timeline; a filter-chip tap advances
  `all → waiting` and the header reads `showing 2 of 14` with no overflow; a crab tap
  acks (mood `waving → content`).
- **Tap targets:** nothing under the 48 px floor at either new slot after the gauge fix.

**Not measured, and deliberately: the live standalone state.** Reaching it means letting
the panel poll `127.0.0.1:2722`, which this lane is barred from contacting. The layout
half was verified with the `connecting` / `connecting has-sensors` classes forced over
CDP, and the `future` fixture reaches the same zone arrangement from a real document.

**Carried to the next wave, not fixed here** (pre-existing, and present at the authored
slot): the sensors row becomes `.tappable` when a host drill is available and is then a
**31 px** tap target against the panel's own 48 px floor — at 2560x720, 2536x696 and
840x696 alike. It is not in this diff. → **CLOSED in v0.26.0 (AUD-F3)**, by growing the
hit area and not the row; the section above has the numbers and why the row could not grow.

## v0.24.0 — the duplicate name that named nothing

One cosmetic fix, measured live on the operator's machine. iCUE's `getSensorName()`
returns the SAME string — `Temp #1` — for both the CPU-cell sensor and the GPU-cell
sensor: two genuinely different ids reading 63°C and 50°C, one name. The row painted
`CPU 63°C Temp #1 34% · GPU 50°C Temp #1 MEM 58%`, and the doubled `Temp #1 … Temp #1`
was clutter that added nothing — the cell position already says which is CPU and which
is GPU.

### What changed

- **A non-distinctive sensor-name label is suppressed.** `paintSensorName` now paints
  through `paintOneSensorName`, which drops the label when `sensorNameCollides(key)` is
  true: this cell's name, after `shortSensorName`, equals the SIBLING cell's current
  name, case-insensitively. When both cells hold the same name **neither** shows it — a
  duplicate identifies neither cell. A distinctive name still paints, and a sibling with
  no name at all is NOT a collision (`sensorHealth[sib].name` is non-null only while that
  cell holds a present reading — `resetSensorHealth`/`hideSensor` null it — so an unset,
  absent, reset or same-id-suppressed neighbour reads empty and the named cell shows
  normally). Re-read every paint, no caching.
- **Both cells stay consistent under async ordering.** The two cells paint from
  independent reads that finish in either order, and a collision is only visible once
  BOTH names are in hand. `paintSensorName` therefore repaints the sibling too (guarded
  by `paintingSensorSibling` against recursion), so the cell that painted first — while
  the neighbour's name was still null — is corrected the instant the second, equal name
  lands. Without the repaint only the later cell would hide.
- **Display-only.** The readings, the units, the same-sensor WARNING (different ids →
  no warning, untouched), the host CPU%/MEM% segments and the value colouring are all
  exactly as before. Sensor selection, ids and the warning logic are not touched — only
  whether the NAME label paints. The suppressed name also drops off `title`/`aria-label`,
  since a hidden duplicate is not worth announcing.

### Verified

`node --check`, strict-XML parse of `index.html` (`xml.dom.minidom`), JSON parse of every
fixture + manifest + translation: all pass. `icuewidget validate widget` clean (the
`icueEvents` warning is the known false positive).

Headless Edge, `--force-prefers-no-reduced-motion`, fresh tab per run, DOM read over CDP,
device metrics pinned to the slot:

- **The suppression proof, five cases on `?mock=rework&sensors=63,50` (the host fixture,
  operator's numbers).** `&sensornames=Temp #1|Temp #1` → **both** name labels empty,
  `.shown` off, computed `display:none`, `title` cleared — while the values still read
  `63°C` / `50°C` and the same-sensor warning stays OFF (different ids). `&sensornames=CPU
  Package|GPU Core` → both labels paint (`display:block`, text and title present).
  `temp #1|Temp #1` → both suppressed, proving the compare is case-insensitive.
  `CPU Package|` (sibling unset) → CPU label paints, GPU absent — no over-suppression.
  Bridge defaults → both distinct, both paint. Screenshots `proof_equal_names.png` and
  `proof_distinct_names.png` at exactly 2560×720 show the row with and without the labels.
- **The full overflow probe: 42 captures, page overflow 0×0, worst zone overflow 0.0.**
  Twelve fixtures × three slots (2560×720 authored, 2536×696 XL, 840×344 S-H) plus the
  equal- and distinct-name sensor row on the host fixture at all three slots. Every zone
  measured against its own box, descent stopped at scroll containers. Zero offenders.

**Not measured, and deliberately: the standalone state.** Reaching it means letting the
panel poll `127.0.0.1:2722`, which this lane is barred from contacting. The sensor row and
its labels were verified on the mock-fed host fixture, which is the row-with-readings
rendering reachable without the companion.

## v0.23.0 — the touch instrument

One feature, and it is not a feature: it is a **measuring device**, and everything
about it follows from that.

### Running a diagnostic session (the operator's procedure)

1. iCUE → the SideCrab widget's settings → the **Diagnostics** group → turn
   **Touch Diagnostics** ON.
2. Look at the glass. A small dim `diag` with a number under it appears at the
   **left edge of the clock row**, mirroring the moon chip on the right. That
   number climbing is the only confirmation needed that the panel is recording;
   nothing external has to be open.
3. **Poke the glass, deliberately, one gesture at a time**, and leave a beat
   between them so the log reads as separate runs:
   - a plain **tap** on a session card,
   - a **swipe** left-to-right across a finished card,
   - a **press and hold** on any card (about a second),
   - a **two-finger tap** anywhere on the panel,
   - a **pull down** from the very top edge of the panel.
   Watch the counter after each one. A gesture that moves the counter by two or
   three and a gesture that moves it by forty are different findings, and both are
   findings.
4. Turn **Touch Diagnostics** OFF. The counter disappears and every listener is
   removed.
5. The maintainer reads the session back with **`GET /v1/panel-log`** on crabd
   (0.24.0 or later). Nothing in the panel reads it — the widget only writes.

If the counter never moves at all, that is the headline result and step 5 is
optional: the glass is sending this widget nothing.

### What it records

A document-level, **capture-phase, passive** listener for each of fifteen event
types — `pointerdown/move/up/cancel`, `mousedown/move/up`, `click`, `dblclick`,
`touchstart/move/end/cancel`, `contextmenu`, `wheel` — writing one compact line per
event into a 400-line ring:

```
+3.990 pdown mouse p1 (1204,388) prim b0 bs1
+3.990 mdown (1204,388) b0 bs1
+4.300 pmove mouse p1 (300,300) prim b-1 bs1
+4.422 pmove mouse p1 (316,300) prim b-1 bs1 coalesced 4
+5.567 pmove mouse p1 (536,300) prim b-1 bs1 coalesced 4 last
+5.884 tstart x1 (600,400)
+7.235 tstart x2 (900,300)
+4.065 click (1204,388) b0 d1
```

The stamp is **relative seconds since capture started**, because the question this
instrument answers is about intervals — a 600 ms hold, a move stream that stops
40 ms before its `up` — and a wall clock makes every reader subtract.

**Move floods are coalesced, not sampled**: first of a stream always, then one
every ~100 ms, then the LAST one when the stream ends, each carrying the count it
absorbed. A 60 Hz swipe is ~40 raw moves; measured, it comes out as 13 lines whose
`coalesced` counts sum to the other 59. The first is emitted uncoalesced because
*whether a move stream exists at all* is the headline finding; the last is flushed
on the stream's end because that is where a gesture's release lives.

Streams are keyed **per pointerId** for `pointermove` — two fingers are two
streams, and merging them would report one flood at double the rate.

**A stream that has not ended is holding its tail**, and a reader should know it: the
moves since the last 100 ms boundary sit in `pending` until the `up`/`cancel` that
ends the stream, or until diagnostics are switched off (`removeDiag` flushes every
open stream *before* removing the listeners, for exactly this reason). So a log read
mid-gesture is a few moves short of the finger; a log read after it is complete.
Measured: 200 moves at a 5 ms cadence come out as 11 lines whose markers account for
all 200 once the `up` lands, and 190 of 200 before it.

**One ordering wart, left alone deliberately.** A `pointerup` flushes the pointer
stream and a `mouseup` flushes the mouse stream, and Chromium dispatches
`pointerup` first — so a mouse stream's `last` line can appear in the ring *after*
the `pup` line even though its stamp is earlier. The stamp is the instant the event
was OBSERVED and is never rewritten to make the ring sort; a reader sorts by stamp.

### Shipping it

`POST /v1/panel-log {"lines":[…]}` → 204, at most 50 lines per post and 300 chars
per line (crabd 0.24.0's contract), flushed **once per poll cycle** — above the
poll's own in-flight guard, because whether the state fetch is stuck says nothing
about whether the operator's taps should reach the companion.

- **Presence detection is a 404 latch**, and 404 and nothing else. A 404 is the
  endpoint saying it does not exist, which is a fact about this crabd; a **400** is
  the widget sending something crabd disliked, which is a fact about one batch —
  latching on that would hide a real capability because one line was malformed. The
  latch clears on a **crabd version change**, with the `/v1/config` and quiet-
  override latches, because a redeploy to 0.24.0 is exactly what adds the endpoint.
- **A network failure latches nothing and loses nothing**: the lines stay at the
  head of the queue and go on the next poll.
- **Turning the switch off stops CAPTURE, not delivery.** The flush is deliberately
  not gated on the layer being installed: at the moment the operator flips the
  switch the queue holds the last poll cycle of lines plus the final flush of every
  open move stream — which is the operator's *last gesture*, the one they walked back
  to the keyboard right after making. That queue drains over the polls after the
  switch and is then a length check per poll forever. (Written the other way first,
  and the last gesture of every session went in the bin.)
- **The queue is capped at 400 like the ring**, and the drop count is STATED inside
  the batch it belongs to (`diag dropped 60 unsent lines (queue full)`) — a silent
  drop would make a gap in the log indistinguishable from a gap in the events.
- A **403** (crabd's Origin gate) is handled like any other refusal: the batch is
  dropped and nothing latches. It cannot arise on the glass — iCUE serves the widget
  from `file://`, whose Origin is not `http(s)` — but it *is* what a dev browser on
  `http://127.0.0.1:8765` would get, which is one more reason the mock path logs
  instead of posting.

**The relative stamp is NOT a duplicate of crabd's prefix, and neither is going
away.** The contract says the widget never timestamps and crabd prefixes each stored
line with its ISO receive time — but that is **one receive time per batch**, and a
batch is a whole poll cycle (~3 s) of a person's hand moving. The server prefix
orders the batches; the `+3.990` inside the line is what makes a 600 ms hold or a
40 ms gap before an `up` readable at all. They answer different questions and the
line is verbatim after the prefix either way.

### Where the indicator went, and the measurement that put it there

The identity zone is a flex column whose `.crab-wrap` is `flex: 1 1 auto`, so free
space in that zone IS the crab — re-measured on HEAD before a line was written, and
the v0.22.0 record reproduced exactly: 462.1 x 391.8 painted at 2560x720, height-
limited outright at 2536x696, 840x344 and 840x696. So the indicator takes **no part
in layout**, exactly as the moon chip does not, and it spends the mirror of the same
slack: the clock row is `justify-content: center`, so the clear column to the LEFT
of the hours matches the room to the right the moon chip lives in.

| slot | clear left of `clockHm` | `diag: N events` at --fs-meta | the stacked form |
|---|---|---|---|
| 2560x720 | **56.9** | 160.9 ✗ | 35.2 ✓ |
| 2536x696 | 61.3 | 155.5 ✗ | 34.0 ✓ |
| 840x344 | **54.3** | 76.8 ✗ | 16.8 ✓ |
| 840x696 | 223.8 | 155.5 ✓ | 34.0 ✓ |
| 416x696 | 90.8 | 92.9 ✗ | 20.3 ✓ |

**The last two rows are a v0.23.0 record and no longer describe the panel.** v0.25.0
made the identity zone a ROW at both of those slots, so the clock is no longer the full
width of it and both chips are re-anchored and re-cleared against explicit padding on
the clock row — current figures are in the v0.25.0 section. The first three rows and
everything below are unchanged.

The full sentence does not fit at four of the five slots, so the word and the count
are **stacked** the way the moon chip stacks its mode and its remaining time, and
the full form rides on `title`/`aria-label` — the idiom the sensor names one zone
over already keep.

**The width budget is five characters, and the probe is what said so.** `fmtNum`
runs to six characters past a million (`999.9M`) and seven past a billion, and at
~8.8 px a glyph six would have left 0.1 px at the authored slot — not a margin. So
the painted figure is clamped: above 999,999 the chip reads `999k+`, measured at
**47.3 px against 56.9 available, 9.6 px clear of the clock**. Nothing about what is
counted or logged is clamped; only what is painted.

`pointer-events: none`, which is load-bearing rather than tidy: an instrument that
could swallow one of the taps it exists to record would be changing the measurement
it is reporting.

Shown whenever capture is installed and **not** gated on feed status, unlike the
moon chip — this reports the instrument's own state, not the companion's. Verified
present on `stale` and on the `future` dead-feed fixture.

### Verified

`node --check`, strict-XML parse of `index.html`, JSON parse of every fixture +
manifest + translation: all pass. `icuewidget validate widget` clean (the
`icueEvents` warning is the known false positive).

Headless Edge, `--force-prefers-no-reduced-motion`, DOM read over CDP:

- **THE PASSIVITY PROOF — 36 scenarios, 56 outcomes per side, byte-identical with
  diagnostics ON.** Twelve fixtures and five slots rendered, plus fifteen gesture
  scenarios each driven by real synthesized input (card tap by touch and by mouse,
  crab tap, two-finger ack-all, swipe past and under the threshold, long-press pin,
  pull past and under the threshold, gauge drill, the full three-step moon cycle
  through its real POSTs, both header chips, an approval decision, and the timeline,
  burn and host sheets). Each scenario runs twice — layer absent, layer installed —
  and every outcome is compared: what opened, what was posted, which class landed
  where, and a normalized digest of the entire DOM. **Zero differences, zero
  uncaught errors.**
- **INSTRUMENT CHECK FIRST, and it caught two false failures before they became
  claims.** The same 36 scenarios run OFF against OFF: 36/36 identical. Getting
  there needed two fixes, both in the harness and both worth recording:
  1. **A tab reused across navigations carries input state.** The SECOND synthesized
     two-finger tap in a tab is swallowed and the second long press never fires its
     timer — *regardless of diagnostics*. Because the suite ran OFF first and ON
     second, that produced a stable, repeatable, correctly-signed "ON breaks the
     two-finger ack" that was entirely an artefact of ordering. A subset bisect over
     `DIAG_EVENTS` (pointer only / mouse only / touch only / each touch type / all
     fifteen) showed no listener made any difference, and an order-swapped run
     (OFF,ON,ON,OFF,OFF,ON) showed the failure following POSITION and not the flag.
     **A fresh tab per run makes both gestures deterministic in six of six runs on
     each side.**
  2. The digest was reading the wall clock in three places: the rebased epoch data
     attributes, the approval hold's own `Ns to decide`, and — the one that survived
     two passes and then failed a third — the stale banner's `data as of 12:01 AM`,
     which only differs when the two runs straddle a minute boundary. Epochs are now
     replaced by their RANK among the document's epochs (stable across runs, still
     sensitive to any change in the relative ordering of cards, resets or holds) and
     every clock-shaped string is normalized.
     **The lesson underneath both: a proof that passes is not yet a proof that is
     stable.** Each of these passed a full run before it failed one.
- **The crab is byte-identical with diagnostics on: 6 moods, sha256 over a clip read
  off the DOM**, same box (28,123 463x392) and same hash in every one.
- **24 capture assertions** against real input, stable over four consecutive runs:
  15 capture listeners installed, every one of them passive (the only non-passive
  capture listener on the document is the pre-existing click swallow); a mouse tap
  producing its `pdown`/`mdown`/`pup`/`mup`/`click` run; first-uncoalesced and
  last-not-lost both proved; `touches.length` on one and two fingers;
  `pointerType: touch` recorded; the ring and the queue capped at 400 with drops
  counted; no line over 300 chars; the mock flush logging instead of posting at ≤50
  lines a batch.
- **The coalescer is proved in two halves, because one of them cannot be proved
  through CDP.** A synthesized "flood" is only a flood if the harness can deliver it
  faster than the 100 ms window, and on a loaded machine the CDP round-trip exceeds
  that — at which point the coalescer correctly does nothing and a naive assertion
  fails for the right reason. So the CDP flood proves the **rate-independent** half
  (no move is lost at any arrival rate: 14 emitted + 59 absorbed of 60 raw), and a
  second flood driven **inside the page** at a guaranteed 5 ms cadence proves the
  rate-dependent half: **200 events over 1000 ms collapse to 11 lines — one per
  100 ms window plus the flushed tail — with all 200 accounted for.**
- **OFF REMOVES, IT DOES NOT MUTE — proved with the listener list itself**
  (`DOMDebugger.getEventListeners` on `document`, the DevTools API): **22 listeners
  with diagnostics on, 7 with them off**, exactly the 15 installed, and a subsequent
  mouse tap moves the counter by zero. Re-arming reinstalls exactly 15.
- **9 shipping assertions** with the transport stubbed so every status is reachable:
  batching at 50, one POST in flight, the 404 latch (drops the queue, stops posting),
  the crabd-version-change clear, a 400 dropping one batch without latching, a
  rejected fetch losing nothing and latching nothing, the queue cap stating its own
  drops, the 300-char line cap, and the residual queue draining after the switch
  goes off (13 lines shipped, then inert).
- **The full overflow probe: 85 captures, page overflow 0x0, every zone 0.** Twelve
  fixtures x five slots, plus five new diagnostics-on states (rework, a quiet
  override, compact density, hot, and the sensors row with capped names) at all five
  slots, each one forced to the widest count the chip can paint.
- **An OVERLAP probe, because the overflow probe cannot see one:** the indicator is
  out of flow, so it can never overflow a zone and could only sit on top of its
  neighbour instead. Clear-to-clock measured at every slot with the widest count:
  **9.6 / 15.5 / 31.6 / 178.1 / 63.4 px**, inside the clock row's own box at all
  five, no negative gap anywhere, and **no intersection with the crab's box in any
  of the 25**.

**Not measured, and deliberately: the standalone state.** Reaching it means letting
the panel poll `127.0.0.1:2722`, which this lane is barred from contacting. The
indicator is gated on `diagOn` alone and on nothing about the feed, and it was
verified on the `stale` and `future` (dead-feed) fixtures, which are the feed-failure
renderings reachable without touching the companion.

## v0.22.0 — the override, the hairline, the ten minutes, and a crab that sweats

Four features. Three of them are the same rule wearing different clothes — **say only
what the feed actually said** — and the fourth is the first new mood since v0.4.0.

### 1. The quiet override: a moon beside the clock

Quiet hours is a SCHEDULE, owned by the iCUE property sheet and written to
`/v1/config`. The override is the thing on top of it — *quiet an hour early*, or
*stay awake through tonight's window* — so it goes on a different wire:
`POST /v1/action {"action":"quiet","mode":…,"minutes":…}`, beside ack, decide and
queue-continue.

**The vocabulary is fixed and it is three words.** Tap cycles
`auto → quiet 1h → awake 1h → auto`; a long press goes straight back to `auto` from
any state (the card's press-and-hold idiom, same timer, same cancellation). One tap
target on an ambient panel cannot carry a duration picker, and a control that could
set any duration would need a second surface to set it in.

**WHERE IT COULD GO WAS THE ACTUAL WORK.** The identity zone is a flex column whose
`.crab-wrap` is `flex: 1 1 auto`, so it absorbs every spare pixel — *free space in
that zone IS the crab*. Measured at five slots before a line was written:

| slot | painted crab | limited by | height slack |
|---|---|---|---|
| 2560x720 | 462.1 x 391.0 | WIDTH | **0.8 px** |
| 2536x696 | 447.7 x 378.8 | HEIGHT | **0** |
| 840x344 | 256.2 x 216.8 | HEIGHT | **0** |
| 840x696 | 381.1 x 322.5 | HEIGHT | **0** |
| 416x696 | 382.8 x 323.9 | WIDTH | 149.7 |

A new row costs what the fleet row costs — 25.2 px — which at 840x344 is 11% of the
animal. So the chip takes **no part in layout at all**: absolutely positioned inside
the clock row's own 108 px line box, pinned right. It spends the HORIZONTAL slack
that zone does have, measured to the right of the seconds: 87.5 px at 2560x720, and
90.8 / 68.8 / 253.4 / 108.4 at the others. The type is `--fs-sub-row` rather than
`--fs-meta` because at meta size the five-letter mode word took the chip to 85.8 px
against 87.5 available — a 1.7 px margin, which is not a margin. Measured after, with
the widest state (`quiet 58m`) on the glass: **8.4 px clear of the clock at
2560x720**, 14.3 / 20.8 / 176.9 / 60.4 elsewhere, and inside the clock row's box at
every slot.

**The chip renders the FEED, never a local assumption.** The POST is optimistic — the
operator is standing there and the panel has to answer a fingertip — and the
optimistic answer is bounded by the DOCUMENT rather than by a timer: it is dropped the
moment a document generated *after* the tap lands, at which point crabd's answer is
the true one whether it agrees or not. That is the ack pattern with a better clock.
The one piece of local arithmetic is an `until` that has passed, and it is CD-42's
asymmetry: it can only ever CLEAR an override, never assert one.

**Presence detection is impossible here, and the honest answer is a latch.** The
`quiet` block exists on every supported crabd and `override` is *absent* until one is
set, so "no member" means "no override", not "no support" — and a probe POST to find
out would BE the write it was probing for. So the widget offers the control, attempts
the write and reads the reply: the "attempt-and-handle IS the capability test"
argument `/v1/config` already makes. A **400 or 404 hides the chip**, cleared on a
crabd version change; a network failure says nothing about capability and does **not**
latch.

`minutes` rides every body, `auto` included. The contract lists it as part of the
action and does not mark it optional, and 0.23.0 states that `auto` IGNORES a
`minutes` it was sent rather than 400ing on it — so one body shape is what keeps a
single 400 from meaning two different things.

**One thing the feature made wrong and then fixed.** `quiet until 07:00` is the
window's end and was correct while the schedule was the only thing that could dim the
panel. An override "on" ends in an hour, so the note would have been claiming quiet
until 07:00 on a state that ran out at 14:20. Two causes, two sentences: the note now
reads `quiet override 58m` when that is why.

### 2. `quietWindowOver` had a second holder, and could not see it

CD-42 re-evaluates the quiet window locally when the feed goes stale. crabd 0.23.0
serves an override with **no schedule** as a block whose `start` and `end` are both
`null` — and the v0.20.0 body bailed to "still quiet" on any missing end. **An
override that expired while crabd was dead left the panel dimmed indefinitely**, which
is CD-42's own failure one cause along.

Each holder is now asked separately (`quietScheduleState`, `quietOverrideState`) and
the answers combined conservatively: quiet is over only when every reason *on record*
has definitively ended, **unknown is not over on either half**, and nothing on record
at all means there is nothing to re-evaluate and the dim stays. The asymmetry is
untouched — this can only clear, never assert.

**One reading corrected against production rather than against a comment.** The old
body treated `start == end` as a 24 h window, on the stated grounds that this was
"crabd's own reading". It is not: `quiet_state` in crabd 0.23.0 has
`if start == end: active = False` — *"zero-length window; always quiet is not
expressible here"*. The widget agreeing with its own comment instead of with the
daemon left a real hole (a zero-length schedule plus an expired override kept the dim
forever), so that branch is now `none`. Verified in all ten directions.

### 3. The context hairline, and the denominator (CLOSED at crabd 0.28.0)

A 2-3 px rule along each card's **bottom** edge (the left edge is the state spine and
has been since v0.2.0). Absolutely positioned, so it takes no part in the card's
carefully argued shrink order and cannot cost a card a line.

**THE DENOMINATOR IS THE WHOLE DESIGN, and the measurement that settled it is worth
keeping.** The brief for this work said the fixtures carry ctx badges "like 1M/200k".
They do not — that is `fmtNum(contextTokens)` rendering the NUMERATOR (`ctx 2.0M`,
`ctx 184k`). At v0.22.0 **nothing in the feed, the contract or crabd carried a
context-window SIZE**, and a model-name table ("opus means 200k") would be a number no
document said — the kind of invention that goes wrong silently: the day a window
changes, every card reports a fill against last year's denominator and nothing says so.

What WAS derivable is a marker in the model id, because crabd serves the transcript's
string **verbatim** and is tested on it doing so (`test_model_string_is_served_as_is`,
on the literal `claude-opus-5[1m]`). So `[1m]` / `[200k]` are window sizes *the feed
stated*, and everything else got **no bar**.

**AND THAT WAS EVERY LIVE SESSION.** Measured 2026-08-28 off this host's transcripts:
the live ids are `claude-fable-5` and `claude-opus-5`, bare. The marked ids existed only
in `?mock=dense`. So the hairline shipped, was correct, and never once drew on a real
card — which is the failure mode this repo keeps naming: a feature that cannot fire
reports success forever.

**crabd 0.28.0 closes it** by serving `sessions[].contextWindowTokens` (int | null),
resolved from the status line's `context_window_size`, then the marker, then the Models
API's `max_input_tokens` — see `docs/STATE-CONTRACT.md` §v0.28.0. `ctxWindowTokens` now
takes the SESSION, not the model string: it prefers the served member and keeps the
marker parse as the fallback for a crabd below 0.28.0 (this widget is not redeployable
on demand). The order cannot be flipped — crabd already ranks the marker above its
catalog, so a served number is never *less* specific than the marker.

Still **no model-name table on either side**, and still no default window: unknown stays
`null` and draws no bar — the honest rendering of "this panel cannot tell you how full
that is". A `null` `contextTokens` gets none either (`typeof`, not `Number()`:
`Number(null)` is 0, and a bar pinned at empty on an unmeasured session reads as one with
all its room left). `contextWindowTokens` is in the **card signature** for the audit-F1
reason: the bar appears when crabd learns the window and disappears when it stops serving
it, and neither event moves any other field on the row.

The two ramp STEPS are the gauges' own constants so the card and the gauges cannot
disagree about where hot starts; the resting colour is `--faint` and deliberately not
the gauges' blue, which would read as a fourth gauge.

### 4. Host history: ten minutes, from a ring, with the gaps left in

Tap the sensors row for two ~10-minute sparklines built from a client-side ring
sampled once per **poll** (not per render — render runs on the tick and on every tap).
No endpoint is added: crabd serves the current reading, and a history of it is
something a panel that has been watching can assemble honestly and one that has just
booted cannot, which is what `collecting — N of 10 samples` is for.

**Nothing is ever interpolated.** Two different absences break the line and both are
real: a sample whose value is `null` (the poll landed, crabd could not measure) and a
time step past 9 s (polls that never landed). The x axis is TIME, not sample index, so
a run of missed polls leaves a hole of the right width. The y axis is a fixed 0-100%
stated in the head, never the series' own peak — auto-scaling would render 2% of idle
noise as a mountain range. Proved on the glass with a synthetic full ring carrying
both kinds of gap: **CPU 3 runs, MEM 2** (the null run nulled `cpu` alone), 5
polylines, every break visible.

The row is a control only while the feed serves a host figure — temperatures alone do
not earn the tap, because they are not in the ring. The chevron is **absolutely
positioned for a measured reason**: this row's overflow guarantee is a number (543.1
of 561.9 px with both names capped, 18.8 px spare) and a flex child would have cost
its glyph *plus* a 15.8 px inter-cell gap — 23.8 px against 18.8. Out of flow it costs
nothing; measured worst case with capped names, **12.0 px clear**.

### 5. `sweating` — the first new mood since v0.4.0

Any usage window at or past the gauges' RED step. **Where it sits in the ladder is the
decision, and it is documented beside the ladder in `sidecrab.js`:** below
connecting/stale (a panel that cannot see must not have a live opinion), below quiet
(quiet clears everything, everywhere), below waving (a question is about the person
standing there; a limit will still be true in five minutes), below **celebrating** —
because that is a 10 s self-clearing latch and sweating resumes after it, whereas the
other order would silently delete every celebration on a busy estate — and **above**
the empty-grid `asleep`, because a window at 97% is a fact about the account, not
about the grid.

**The art is three drops and the crab does not move.** Two proofs, and the pair is the
claim:

- **sweating-OFF against the v0.21.0 baseline: byte-identical**, across all five moods
  and all four wardrobe states (11 clipped captures, sha256).
- **sweating-ON against `content`: 2306 pixels changed, ZERO of them outside the drop
  rectangles** — the rects read off the DOM, so the proof cannot disagree with the
  rendering about where the art is. 1.19% of the crab's box.

Each drop is a 1-unit tip over a 3x3 body with a 1-unit glint, on the quarter-cell
grid v0.17.0 bought — 26.7 x 35.5 px at the authored slot. Placement is CLEARANCE:
they sit in the headroom either side of the shell, above the claws and outside the
party hat's column, which is the only accessory that can share a frame with this mood
(the sunglasses need `limitsCalm()`, false by definition at red; the nightcap needs
quiet, which outranks this mood; the sleep Z is asleep-only). The juggle hides them,
as it hides the wardrobe, because the balls' arc runs straight through both.

Blink and tap survive: the drops touch no `.eyes-*` group, and `sweating` joins
`content` on the idle-blink allowlist rather than being left out — otherwise the
crab's one idle tic would freeze for the hours a weekly window sits red, which reads
as a panel that has stopped.

### Verified

`node --check`, strict-XML parse of `index.html`, JSON parse of every fixture +
manifest + translation: all pass. `icuewidget validate widget` clean (the `icueEvents`
warning is the known false positive).

Headless Edge, `--force-prefers-no-reduced-motion`, DOM read over CDP:

- **The full overflow probe: 118 captures, page overflow 0x0, every zone 0.0.** Twelve
  fixtures x five slots, the standalone state at every slot, and the twelve new
  v0.22.0 states (three chip states, the latch, three `&quietov=` seeds, the ctx grid
  in both densities, sweating, and the host sheet including its capped-name worst
  case) at both landscape slots, with six of them repeated at all three narrow slots.
- **Instrument check before a line was written:** the probe reproduced the v0.21.0
  record exactly on HEAD — 79 captures, 0 offenders.
- **An OVERLAP probe, because the overflow probe cannot see one:** both new controls
  are out of flow, so they can never overflow a zone and could silently sit on top of
  their neighbour instead. Chip-to-clock and chevron-to-cell gaps measured at five
  slots x three states: no negative gap anywhere.
- **59 behaviour assertions**, including the full tap cycle end to end through the real
  POST, the optimistic answer settling on the feed, the capability latch, all ten
  `quietWindowOver` directions, the ctx bar at three fill levels plus both no-bar
  cases, the mood ladder driven on the live feed (including the exact `GAUGE_RED_PCT`
  boundary: `content` at 94%, `sweating` at 95%), and both gap kinds in the ring.
- **Zero uncaught errors or unhandled rejections** across twelve fixtures, the
  standalone state and twelve flag combinations, each with the chip tapped, the sensor
  row tapped and Escape pressed.

## v0.21.0 — the last layer of the frozen-temperature saga: the wrong sensor

**The measurement this release starts from**, taken out of iCUE's own property
storage (`dashlcd/storage`) on the operator's machine: `cpuTempSensor` and
`gpuTempSensor` both held **`1ce3d9bb-eb61-344e-b445-edc68d635364`**. One sensor
id, in both properties — almost certainly a hub or ambient probe, which is exactly
the kind of sensor that sits at one temperature all day.

The v0.18.0 wrapper race was real and is fixed. This is a **second, independent
cause with an identical symptom**: two numbers that never move and never disagree.
The panel could not tell them apart, and neither could anyone reading it, because
the row said `CPU 41°C  GPU 41°C` and nothing else — no statement of *which*
sensor either number came from. Every fix below follows from that one sentence.

### 1. Every temperature now says which sensor it came from

`getSensorName(sensorId)` beside each reading, shortened for the cell:
`CPU 72°C CPU Package`. A hub temperature masquerading as a CPU package is
legible from a doorway the moment it is labelled.

- **Cached exactly the way the units are** — a 5 minute TTL plus a change signal —
  and the reason is the same one `SENSOR_UNITS_TTL_MS` carries: a name request
  beside every 10 s value read is doubled traffic through the bridge for a string
  that only changes when the operator changes the selection. **Proved with a
  mutation:** wrapped `getSensorName` and watched **0 name calls against 4 value
  calls over 25 s**, then set `SENSOR_NAME_TTL_MS = 1` and watched them come back
  at **4 against 4**. A cache proved only by silence is indistinguishable from a
  call path that is broken.
- **There is no nameChanged signal in the contract** (sensors-data-provider.md
  lists five signals and that is not one of them), so `sensorDataChanged` is wired
  to invalidate the cache. The TTL still covers a provider that never emits.
- **The name leg cannot fail the read**, written directly beside the units leg so
  the pair cannot drift: CD-12's whole argument is that a label which could not be
  fetched must never take down the number it labels. Same 30 s backoff, so a
  bridge with no `getSensorName` costs one failed call a minute rather than one
  beside every read.
- **The selection change resets it with the rest of the health record.** A label
  from the sensor the operator just stopped watching, sitting beside a reading
  from the one they started, is worse than no label — it is a confident wrong
  answer to the exact question the label exists to answer.
- `shortSensorName` takes the **last** path segment (a path names the device on
  the left and the sensor on the right, and the device is what the cell already
  says), drops a trailing `Temperature`/`Temp` (the degree sign said it), and
  clamps to 13 characters. The full string rides on `title`/`aria-label`.

### 2. One sensor in both properties is now a stated fact, not a duplicate number

When `cpuTempSensor` and `gpuTempSensor` resolve to the same id, the GPU cell
renders **`same sensor`** in the staleness treatment (opacity 0.38) instead of a
second copy of the CPU's reading. A duplicated reading is not a reading.

Three things about it are deliberate:

- **The GPU sensor is not read at all** in that state — not to save a call, but
  because there is no second reading to take. Measured over ~2 reconciles:
  **4 value reads with distinct ids, 2 with a shared one.**
- **The warning is derived from the SELECTION, never from a read.** It is knowable
  with the bridge unable to answer a single request, which is precisely the run
  where the operator most needs to know which of the two faults they are looking
  at.
- **The settings panel says it too.** The Hardware Sensors group `info` now tells
  the operator to pick a different sensor for each and why — the panel makes the
  mistake visible, and the property sheet is where it gets made.

### 3. Host CPU and memory, from the feed

crabd 0.22.0 serves a top-level `host: {cpuPct, memPct, memUsedGB, memTotalGB}`.
The row reads `CPU 72°C CPU Package 34% · GPU 54°C GPU Core · MEM 58%`.

- **Presence-detected member by member**, never on the block being truthy: an
  older crabd sends no block, a current one may send any member as `null`, and
  both land on *the segment is simply absent*. `typeof === 'number' && isFinite`,
  so a contract-legal `null` cannot arrive as `Number(null) === 0` and paint an
  idle machine that is really one crabd could not measure.
- **The GB pair is on `title`/`aria-label` and not on the glass**, and that is a
  width decision with a number behind it (below), not a preference.
- Fixtures: `rework` carries all four members, `hot` carries the block with every
  member null, `dense` carries `cpuPct: null` beside a readable `memPct` — which
  is crabd's own **first-sample** case, since `GetSystemTimes` is cumulative and
  no utilization exists until the second builder pass.
- **One deliberate divergence from the contract's wording, recorded so nobody
  "fixes" it.** `STATE-CONTRACT.md` says a reader must render an **em-dash** for a
  null `cpuPct`. This panel **omits the segment** instead. Both agree on the thing
  that matters — *never 0%* — and where they differ the panel keeps its own rule,
  which it has had since v0.20.0 (CD-33): *an unreadable utilization is omitted
  rather than dashed*. A dash on a hardware row reads as a broken sensor, which is
  the exact misreading this whole release exists to prevent.

### 4. A fresh import now says so

Importing the widget resets both sensor properties, and the row's honest response
to that was to render nothing — which reads as *this machine has no sensors*
rather than *nobody has picked one yet*. With the plugin present and **neither**
property set, the row now carries `pick sensors in settings` in the same dim
treatment. Gated on a bound bridge, so it can never appear in a plain browser:
verified showing on `?mock=normal&sensors=none` and **absent** on `?mock=normal`,
`?mock=rework` and the standalone state.

One property set and the other empty is deliberately **not** this state: a fresh
import clears both, and a single empty property is an operator who wanted one
temperature. Nagging there would be the panel crying wolf at a choice.

### The width budget, which is where the work actually was

The row is **one line**, and that is a measured constraint: the Limits zone had
**20.0 px** of vertical slack left on `?mock=rework` after the v0.17.0 trim
(re-measured this session at exactly 20.0 — the instrument agrees with the v0.17.0
record), and one more line of meta type costs more than that. So everything added
here rides inside the existing line box, and the whole question became horizontal.

**561.9 px of zone content width at 2560x720. 360.8 px of it was free.** Three
attempts, two of them refuted by the probe rather than by argument:

| approach | measured result |
|---|---|
| cells shrink, `min-width: 0` (as shipped since v0.3.0) | shrink spreads across ALL cells including MEM, whose key and value cannot give: **MEM 74.2 px wide holding 83.7 px**, its value 9.4 px past the zone |
| cells shrink, `min-width: auto` | Chromium's min-content contribution for the name is its text width, not the 0 its own `min-width` allows, so nothing shrinks: **65.4 px past the zone**, worse |
| **cells do not shrink, the NAME is capped** | the widest row that can ever paint is fixed parts + 2 x cap. **543.1 px of 561.9, 18.8 px spare** with both names at the cap in the font's widest glyphs |

The cap started at 12 vmin and **the screenshots caught it**: `CPU Package` — the
single most likely name on this panel — measures **87.0 px** and came out
`CPU Packa...`. A label ellipsed at its most ordinary value is a label nobody
trusts. 13.5 vmin (97.2 px) holds it, along with `GPU Hot Spot` (93.1) and
`CPU Core Max` (96.1), and the 11.5 px that pays for it comes off the row's own
inter-cell gap (3 → 2.2 vmin, still 15.8 px between cells against 5.8 px inside
one, so the v0.17.0 "the gap between pairs beats the gap inside one" rule holds).
Ordinary names leave **61.3 px** spare.

### One defect this release introduced and closed before shipping

**Hiding the cell used to be enough to take the number off the glass.** Until now
a hidden sensor meant a hidden cell, so whatever text was left in the value span
could not be seen. The CPU cell now survives a dead bridge whenever the feed is
serving a host figure — so the last temperature would have gone on sitting beside
`34%`, undimmed, with no read behind it: the exact *looks live, is not* failure
v0.18.0 exists to prevent, one level down. `hideSensor` now clears the value and
the label with the state that produced them. Driven end to end: a live row, then
`sensorForced.none` flipped from the console, and the CPU cell keeps `34%` while
the temperature and its name **go**, and both come back on the next reconcile
after it is flipped off.

### Verified

`node --check`, strict-XML parse of `index.html`, JSON parse of every fixture +
manifest + translation: all pass. `icuewidget validate widget` clean (the
`icueEvents` warning is the known false positive).

Headless Edge, `--force-prefers-no-reduced-motion`, DOM read over CDP:

- **The full overflow probe: 79 captures, page overflow 0x0, every zone 0.0.**
  Twelve fixtures x five slots (2560x720 authored, 2536x696 XL, 840x344 S-H,
  840x696 M-H, 416x696 portrait), the standalone state at every slot, and at both
  landscape slots the eight new row states — labels, same-sensor, host null, host
  partial, capped names, no bridge, compact density and standalone.
- **Instrument check before a line was written:** the probe reproduced the
  v0.17.0 record exactly on HEAD — auto-margin slack 20.0 on `rework`, 123.9 on
  `hot`, 183.4 on `caveat`, 193.0 on `recap`, 217.5 on `normal`.
- **The staleness cue re-proved over the new markup** (`?mock=normal&sensors=63,71
  &sensorfail=1&sensorstale=4000`): value `63°C` at T+0 undimmed, still `63°C`
  carrying `.stale` at opacity 0.38 at T+18 s, row still shown, **and the name
  still shown** — a label is a fact about the selection, not about the read.
- **Zero uncaught errors or unhandled rejections** across every case.

## v0.20.0 — the finding wave: what the panel was not saying out loud

Fifteen findings, every one of them reproduced on the DOM before it was touched and
re-run after. The theme that runs through most of them is one thing: **the panel was
quietly showing less than it knew.** A units lookup failing threw away a temperature
it had in hand; a filter chip silenced an alert; two writes that did not happen were
reported as if they had; a tile named seven sessions and offered no way to reach any
of them; and at the narrow slots the widget's entire purpose was dropped without a
word. Fixing those is one rule, applied fifteen times: **absence is a fact, and it is
never rendered as success.**

### 1. Failure has to reach the glass — CD-13, CD-40, CD-12

Three separate defects, one shape. In each the widget knew something had gone wrong
and rendered as if it had not.

**The approval decision (CD-13).** Approve/Deny closed the sheet optimistically and
then, on a non-2xx, called `logLine` — a console message, on a display that has no
console. Reproduced with `?mock=rework&approval=1&action400=1`: the sheet shut, the
card kept its `pendingPermission`, the panel said nothing at all, and the only trace
anywhere was `[sidecrab] decide failed (HTTP 400)`. The close stays optimistic; what
is new is that the failure now reaches the **notice line** — which is the surface
this exact case needs, because the sheet is gone by the time the answer lands. The
wording sends the operator where the decision can still be made (`allow not sent —
decide in terminal`): crabd holds the hook ~55 s and then hands the request back to
the terminal dialog.

**The ack-all receipt (CD-40).** A two-finger tap writes `acknowledged N` on the
gesture, which is right. When the POST then failed, `rollbackAcks` put the cards back
and left the banner saying the acknowledgement had happened, for its full second and
a half, over rows that were visibly still waiting. Corrected in `rollbackAcks`, so
the crab tap — which has no receipt line of its own — gets the same honest answer
instead of a silent rollback on the biggest target on the glass.

**The sensor units (CD-12).** `readSensor` put the value and the units into one
`Promise.all`, which rejects if either does. Measured both branches:

| path | before | after |
|---|---|---|
| first read, units call rejects | row **hidden**, value 71 discarded | `71°`, row shown |
| TTL refresh, units call rejects | fresh 88 dropped, old value left to dim to stale | `88°C`, `failsSinceOk` 0 |

The units leg now resolves to an outcome rather than rejecting, so the `Promise.all`
fails only when the **value** does — which is the one failure that really is one. A
units call that was asked and did not answer holds off for `SENSOR_UNITS_RETRY_MS`
(30 s) so a permanently broken units bridge cannot put a second request beside every
10 s value read; `sensorUnitsChanged` clears that backoff, because a signal saying
the units have changed outranks it.

### 2. The filter was an alert filter — CD-34

`applyEscalation` read its tiers off `ui.cards.children`. With the chip on **Working**
a waiting card is not in the DOM at all, so the panel-wide tier stayed 0 while the
question stood. Measured on `?mock=dense&filter=working&age=20`: **two unacked
`needs_input` rows in the feed, one of them carrying a live `pendingPermission`, and
`body.esc1` / `esc2` / `approval` all false.** The filter had been narrowing the one
thing the comment above it promised it never would.

Split in two. Per-card classes stay DOM-driven (they can only apply to rendered
cards); the panel-wide tier and `body.approval` are now computed in `panelEscalation`
from the **feed** — same list and same ack rule as `alertNow`, so the two cannot
disagree. Quiet still clears everything. The header is the other half: a filtered
count line now reads `showing 6 of 14 · 2 waiting hidden`, so a glowing panel whose
grid holds no waiting card says where the card went.

### 3. The "+N more" tile now goes somewhere — CD-14

It was a bare `<div class="card chip">` with a number in it: no ids, no handler, not
`.tappable`. It hid **7 sessions at 2560×720 and 11 at 840×344** — the panel telling
you what it was not going to show you. It is now a real control opening a sixth sheet
mode, `data-mode="overflow"`, rendering into the timeline's list region with the
timeline's row metrics.

Two things worth keeping:

- **The cut list is kept, not recomputed.** `renderSessions` writes `overflowList` at
  the slice, because dismissals, the filter, the pin order and `gridCapacity()` all
  decided which rows those are — a second pass through the same four rules is a
  second pass that can disagree, and the disagreement would be invisible.
- **The rows are controls, so they carry the 48 px floor**, which the timeline's text
  rows deliberately do not. At 840×344 that is 11 × 48 = 542 px of list in a 238 px
  box: it **scrolls**, clipped inside the panel (measured: `listInPanel` true,
  `clientHeight` 238, `overflow-y: auto`, last row reachable). It is deliberately
  **not** trimmed the way `fitDayRows` trims the day view — the day list trims because
  its "+N earlier" tail scrolled out with the rows it was admitting, and here the
  count sits in the sheet **head**, which never scrolls. Trimming this list would have
  reproduced CD-14 one level down.

The sheet follows the feed like every other: filter down to a set that no longer
overflows and it closes itself, and it is inert when nothing is cut.

### 4. The narrow slots said nothing at all — CD-33

At `max-aspect-ratio: 3/2` the stylesheet hides **both** the Limits and the Sessions
zones, and the widget becomes a clock with a crab on it. An unacked question and a
five-hour window at 97% were both simply absent, with nothing admitting either
existed. Rule 6 is *hide, never shrink* — this is the half that was missing, which is
that what gets hidden still has to be **admitted**.

The honest minimum, done properly: a two-line **core line** under the clock —
`2 waiting · 6 working` and `5h 86% · wk 93%` — rendered on every slot and shown by
CSS only where those zones are gone, so nothing in JS knows which slot it is on. With
no companion it reads `companion not running`, because the full sentence lives in the
zone this slot has hidden. Reading rules are the panel's own: an unreadable
utilization is omitted rather than dashed, never a 0%.

**One cascade trap, caught by measuring.** The base `.core-line { display: none }`
was first written *after* the media query. Identical specificity means source order is
the whole cascade, so the base rule won at every slot: the text rendered perfectly and
computed `display: none` everywhere. The block now sits **above** the Rule 6 section
and anything added to it must stay there.

**And a pre-existing defect found while measuring that slot** (identical at HEAD, so
it is not from this wave): with no companion *and* no sensor reading,
`body.connecting:not(.has-sensors) .zone-identity { flex: 0 0 36% }` out-specifies the
narrow-slot rule. That 36/64 split is right when it has the Sessions zone to split
with; here that zone is `display:none`, so at 416×696 the identity zone came out
**150 px wide holding a 177 px clock, hanging 42.4 px out of it** (45.0 at 840×696).
The selector is restated inside the media query rather than the media query being made
stronger — the rule that knows about this slot has to be the one that wins.

~~Recorded as **future, not done**: full responsive layouts for the sub-3:2 slots. That
is a project. This is the truthful minimum until it happens.~~

**DONE at v0.25.0** — see the section at the top of this file. 840x696 and 416x696 have
real layouts and the core line is `display:none` at both; it survives, unchanged, only
where a narrow slot is also too SHORT for three zones (≤ 420 px). The 36/64 overhang
above is fixed as a class rather than as one rule: every standalone split is restated in
each of the new family queries.

### 5. A late reply could repaint a different sheet — CD-35

`dayReqId` only caught a *second day fetch* superseding the first. It said nothing
about the sheet being closed and reopened on something else, which is the case that
bit: tap a week column, close the timeline, open a session — and the history reply
lands into that session's sheet. Reproduced with a 900 ms delay injected into the
history fetch: `sheetMode` came back **`day`**, titled *Friday, August 21, 2026*, on
the sheet that had just been opened on a session. The `fromPanel` path was worse
still — it skips the open/closed test entirely, so it would reopen a sheet the
operator had already dismissed.

`sheetGen` is now bumped by `closeSheet` and by every `open*`, captured by
`openDaySheet`, and compared when the fetch returns. It is the counter that actually
means *the sheet I was fetching for*.

### 6. Keyboard and accessibility, the deliberate subset — CD-15, CD-31

This panel ships on a wall-mounted touchscreen with no keyboard, and inventing a full
keyboard UX for a surface that cannot exercise one would be shipping a second
interaction model nobody can test. But every QA pass this widget has ever had was
driven from a machine that **does** have a keyboard, where the panel was a set of divs
with click handlers: nothing reachable by Tab, nothing activatable by Enter, and a
sheet that could be opened and then only closed with a pointer.

Done, and each one measured:

| | before | after |
|---|---|---|
| Escape closes the sheet | no keydown handler anywhere | closes |
| focus moves into the sheet | stayed on `BODY` | `BUTTON#sheetClose` |
| Tab / Shift-Tab leave the panel | yes | trapped, wraps both ways |
| background hidden from AT | `aria-hidden` unset | `true` while open, removed on close |
| `aria-modal` on the dialog | absent | `true` |
| crab / gauges reachable | `role="button"`, **no** tabindex | `tabindex="0"` |
| cards reachable | 0 tab stops | 8 |
| Enter on a `role="button"` div | nothing | activates |

Two decisions inside that:

- **A card is a tab stop and deliberately NOT `role="button"`.** The role flattens an
  element to its label, and a card is a title, a state, a model, a question and a badge
  row — the one place here where the content is the point. It is matched on its class
  in `onKeyDown` instead.
- **`:focus { outline: none }` and `:focus-visible { outline }` ship as a pair.** The
  first alone would have been the accessibility regression this whole section exists to
  avoid: an invisible focus point is a keyboard UI nobody can follow. `inert` would be
  the right primitive for the background and is not reliably present in QtWebEngine, so
  the guarantee is `aria-hidden` **plus** the Tab trap — two mechanisms that are both
  known to exist here.

**CD-31** is the same family: `#notice` shipped `aria-hidden="true"` and nothing ever
set it back, so the one line that exists to confirm a gesture — `acknowledged 2`,
`refreshing` — was live `role="status"` text no accessibility API could read. It now
tracks the class in both directions.

**Deliberately skipped, and named so nobody has to re-derive why:** arrow-key
navigation of the card grid, keyboard equivalents for the four gestures (swipe-dismiss,
long-press pin, two-finger ack-all, pull-to-refresh), and `aria-live` narration of
state changes. Each is a real feature, none is a defect this wave found, and all three
would ship untested against the surface they are for.

**v0.30.0 — the skipped list shrinks to one.** The ground for skipping the gesture
equivalents was that the surface has no keyboard. A panel with an address in a browser
is driven from one by definition, so the four are here, and each key calls **the same
function the gesture calls** rather than a second copy of it:

| key | what it does | the gesture it equals | the function both reach |
|---|---|---|---|
| `a` | acknowledge every waiting session | two-finger tap anywhere, **and the crab tap**, which has always been the pointer equivalent of the two-finger one | `fireTwoFingerAck` → `ackAllWaiting` |
| `p` | pin / unpin the focused card | long press on a card | `pinCard` → `togglePin` |
| `Delete` / `Backspace` | dismiss the focused card | swipe a done/idle card away | `dismissSwiped` |
| `r` | refresh now | pull down from the top edge | `forceRefresh` |
| `s` | open the panel's settings | the gear beside the filter chips | `openSettingsSheet` |
| `Escape` | close the sheet | the backdrop or the X | `closeSheet` |

Three things had to be true for a bare letter to be safe, and all three are tests:

- **Never while a sheet is open.** The keys would fire behind it, on a grid the
  operator cannot see.
- **Never while an input has focus.** The settings sheet is the first surface on this
  panel with text fields in it, and an `a` typed into a pairing code is a character.
  `typingInAnInput()` is the guard.
- **A native `<button>` is still not double-activated.** CD-15's own rule, unchanged:
  Enter and Space are the engine's to deliver, and synthesising a second click is how
  one press denies a permission twice.

Two things that only showed up once the keys existed:

- **A pin throws away the node under focus.** Pinning re-sorts the grid and the confirm
  flash is render state, so `renderSessions` rebuilds every card — and focus goes with
  the node, to the document. Measured: without `refocusCard(id)` a second `p` had
  nothing to act on and neither did `Delete`, which is the whole keyboard path dying one
  keystroke in.
- **`suppressClick()` belongs to the long press and not to the pin.** The hold consumes
  an interaction whose finger has not lifted yet; a key has no click coming, and
  swallowing the operator's next one would be a bug with no cause on the glass. The pin
  itself is `pinCard()`, shared; the suppression stays in `firePin()`. Both directions
  are pinned by tests.

**`aria-live` narration** is the last of the three, and it is now partial rather than
skipped: each keyboard action reports on the same `#notice` line the two gestures with
no other visible result already use — `acknowledged 3`, `refreshing`, `pinned`,
`unpinned`, `dismissed`. The asymmetry with the gestures is deliberate: a long press
puts a pin glyph animating in under the fingertip and a swipe sends the card off the
glass, and a key gives neither. Arrow-key navigation of the grid is still skipped.

### 7. Quiet hours could outlive the companion — CD-42

`quiet.active` is crabd's answer, and a dead companion answers nothing — so a panel
that dimmed at 22:00 and lost its companion at 23:00 went on rendering `active: true`
at noon the next day. When the feed is **stale**, `quietWindowOver` now re-evaluates
the window's end locally from the `start`/`end` the document already carries.

The asymmetry is the honest half: **it can only ever clear quiet, never assert it.**
Dropping a dim the companion can no longer vouch for is surviving without a document;
dimming the panel on a window nobody served would be inventing one. Both ends are
required — a 22:00–07:00 window is outside its end at 23:00 and inside it at 06:00,
and an end with no start cannot tell those apart, so a window missing either one stays
quiet. *Unknown is not over.* Verified in all four directions: cleared past the end on
stale; **kept** while still inside the window on stale; **kept** with no `start`
served; **kept** on a live feed regardless.

### 8. The small ones — CD-32, CD-38, CD-39, CD-41, CD-26

**CD-32 — the standalone clock disagreed with every shipped panel.** `index.html`
declares `clock24` with `data-default="false"`, so an iCUE panel boots on the 12-hour
clock; the JS fallback said `true`, so a dev browser and the standalone QA pass — the
only two places with no property sheet to inject a value — booted on 24-hour. Five
call sites each carried their own copy of that default. All five now go through
`use24Clock()`, and the default lives in one place: the manifest's.

**CD-38 — the weekday labels never came back.** `renderSparkLabels` cleared the label
spans on the 24 h branch and left `sparkLabelSig` holding the letters it had just
deleted, so the next 7-day pass computed the same signature and skipped the rebuild.
Measured: 7d → **7 labels**, 24h → 0, 7d again → **0, permanently**. Now 7 → 0 → 7. A
cleared cache must be cleared on both sides or it is not a cache.

**CD-39 — the timeline said `(untitled)` where the card said the repo.** The merged
Today list read `s.title` directly while the card and the sheet both fall back to the
repo through `titleParts()`. On a title-less session that put a column of identical
`(untitled)` tags in the one view whose whole job is telling sessions apart — measured
at **5 rows**, now 0. `shortTitle` still does the clamping, so the fallback goes
through the same trim every other tag does.

**CD-41 — `data as of 6:12`.** `fmtClock`'s 12-hour form carries no meridiem, which is
correct for the header clock (always *now*, with AM/PM on the date line beside it) and
wrong for the two places that render a moment in the **past**: the stale banner and
`resetLabel`'s already-past fallback. Both moved to `fmtTimeOfDay`, which the timeline
rows have used for exactly this reason since v0.8.0.

**CD-26 — the toast switch label (widget half).** `Desktop Toast Alerts` reads as a
global mute, and as of the notifier's v0.20.0 change that is exactly what it is. The
label is therefore **correct as written and deliberately unchanged**; what is new is
that its correctness now depends on the notifier honouring `toast.enabled` globally,
which is named in a comment beside the property so it cannot narrow again silently.
The group `info` gained the sentence that says the scope at the point of use. No
per-property description attribute was invented: switch / slider / textfield / color /
sensors-combobox with `data-label` are the only shapes this widget has had accepted by
the import validator, and the group `info` is where per-group prose is allowed.

### Verified

`node --check`, strict-XML parse of `index.html`, JSON parse of every fixture +
manifest + translation: all pass. `icuewidget validate widget` clean (the `icueEvents`
warning is the known false positive).

Headless Edge, `--force-prefers-no-reduced-motion`, DOM read over CDP:

- **All 15 findings reproduced on the DOM before the fix and refuted after** — 14 by
  dynamic reproduction, CD-26 by inspection of the shipped copy.
- **The full overflow probe: 85 captures, page overflow 0×0, every zone 0.0, sheet
  panel 0.0 in all of them.** Twelve fixtures × five slots (2560×720 authored,
  2536×696 XL, 840×344 S-H, 840×696 M-H, 416×696 portrait), the standalone state at
  every slot, all eight sheet modes at both the authored and the small slot, and
  compact density on both density fixtures.
- **A before/after diff against HEAD** on moods, card counts, body classes, banner and
  count lines across `future` / `stale` / `quiet` / `hot` / `dense` / standalone: byte
  identical except the one intended change (`data as of 12:21` → `12:21 PM`).
- **Zero uncaught errors or unhandled rejections** across every fixture, thirteen dev
  flags, the standalone state, and a churn pass driving 50 Tab presses, Escape, Enter,
  Space and four sheet transitions.

**One instrument correction, recorded because it changed a reading.** The overflow
probe descended into scroll containers, so it counted the overflow sheet's clipped,
scrollable rows as a layout failure (292.2 px at 840×344). It now skips elements under
an `overflow: auto|scroll` ancestor and measures that container against the zone
instead — `overflow: hidden` still descends, because content clipped with no way to
scroll to it *is* lost, and that is the sliced-card failure this probe was written
for. The relaxed rule only ever removes measurements: re-run against HEAD it still
reports HEAD's own 45.0 / 42.4 identity-zone defect, so it has not been blunted.

## v0.19.0 — the panel drill-downs, and the nightcap gets its nights back

Three changes: a second way into the day's history, a detail sheet behind each usage
gauge, and one line of the wardrobe ladder moved. Every figure below was measured on
the DOM at the real slot, not read off the CSS.

### 1. Today's PERSISTED history, one tap from the panel

**What was missing.** The Sessions header already opened a "Today" view — but that one
is `sessions[].events`, a per-session ring capped at 8 entries and rebuilt from scratch
when crabd restarts. By mid-afternoon the morning's approvals, denials and continues
have been pushed out of it. `GET /v1/history?day=` is the persisted file and keeps them,
and the only route to it was the week strip's day columns, two taps in, inside a sheet.

**The control.** A third `.head-chip` beside the filter and density chips, routed in
`onGridHeadClick` above the header's own tap exactly as the other two are. It opens
`openDaySheet(todayKey())` — **the same day view the week strip opens**, deliberately:
one is a column tap and the other a chip, and a person reading the second should not
have to learn a second layout. The only difference is the subtitle, which now says
`today — history — newest first` when the day being read is today. `todayKey()` is in
the day sheet's signature for the same reason `updateDayNav` runs ahead of it: a panel
left open across midnight has to stop calling yesterday today.

**The honest-failure design, which is the point of the control rather than a detail.**
An older crabd 404s this endpoint, and *"crabd cannot answer"* and *"crabd says nothing
happened"* are indistinguishable from a tap. Opening the day view anyway would put
**"No events recorded for this day."** on the glass over what may have been the busiest
day of the week. So:

| state | what the chip does |
|---|---|
| feed not `live` (standalone, connecting, stale, dead-feed) | **hidden outright.** A widget with no companion has no history to offer, and this panel is a working clock without one |
| tap succeeds | opens the day view; chip stays `History` |
| tap fails — 404, dead socket, malformed body, timeout | **the sheet is never opened.** The chip reads `No history`, dashed border, and the reason is on `title`/`aria-label`; one console line |

**Not a latch, and that is deliberate.** `openDaySheet` gained an optional failure hook
rather than a flag: the week strip's column tap keeps its silent-inert behaviour (the
timeline behind it is the whole answer), and only a control that exists *solely* to open
this view reports a reason. The next tap fetches again either way, because crabd
redeploys under a live widget and a widget that remembered "unsupported" would need a
console import to forget it — the v0.6.1 rework's rule, in a third place. The 30 s
`HISTORY_FAIL_MS` only clears a stale reason off the header; it gates nothing.

**Verified** (headless Edge, `--force-prefers-no-reduced-motion`, DOM read over CDP):
`&hist=rich` → 26 events, 18 rows + `+8 earlier`, "26 events" foot, list overflow 0;
`&hist=empty` → "No events recorded for this day." with a `0 events` foot and the chip
unchanged; `&hist=error` → **`sheet.open === false`**, chip `No history/off`. Live
browser: a second tap after a failure fires a second fetch (no latch), and when the
document came back the chip returned to `History` and the sheet opened with 18 rows.
`?mock=stale` and `?mock=future` (the dead-feed fixture) both hide the chip.

### 2. A forecast sheet behind each gauge

`data-mode="forecast"` — a fifth mode, and unlike the day view it takes one of its own
because it shares no region with any other view. It renders into `.sheet-burn`; borrowing
`data-mode="burn"` would have meant a second attribute gating one region two ways.

Four rows, then notes, then the by-model split:

| row | source | unknown renders as |
|---|---|---|
| utilization | `<window>.utilization` | the gauge is em-dashed, so **the tap is inert** and there is no sheet |
| resets in | `resetLabel()` — the same countdown the gauge foot carries | em-dash |
| resets at | `momentText()` — split out of `resetTooltip` so the two cannot disagree about what "today" means; appends the short date when the reset is not today | em-dash |
| forecast | `forecastLabel()` first, so the sheet and the gauge line can never disagree | see below |

**The forecast row is where 0.17.0's null-honesty gets said in words.**
`forecastLabel()` answers four different facts with the same empty string, and on the
gauge that is right — a hint line reading "no forecast" under every calm window would be
noise on a panel read from a doorway. In the sheet silence is not an answer, because the
tap *was* the question. One branch carries a distinct fact and is separated out:

- `exhaustAt` at or after the window's own `resetsAt` → **"resets before it depletes"**.
  A reassurance, not an absence.
- absent, unparseable, or a projection whose moment has already passed → **"no forecast"**.
  **A date is never manufactured to fill the row** — crabd serves `null` for a window with
  no parseable reset (contract v0.17.0 §1) and the widget says so rather than inventing one.

`burn.byModel` rides underneath when the feed carries it, labelled **"today's output, all
windows"**: the feed has no per-window split and one is not invented here.

**`rework` proves three branches in one fixture** (it was built for exactly this at
v0.13.0): `fiveHour` → `~full by 11:44`, `weekly` → `no forecast` (`exhaustAt: null`),
`extra0 opus weekly` → `resets before it depletes`. The fourth — **a projection already in
the past** — is new in `hot`, whose `weekly` gained `"exhaustAt": "2026-08-26T17:40:00Z"`,
25 minutes *before* that fixture's `generatedAt`; `pinMockResets` freezes it there, so it
stays in the past. The gauge line is hidden and the sheet says `no forecast`.

**The affordance is a chevron on the gauge NAME**, the same `›` the two zone headers
carry — not a hint line of its own. The Limits zone had **20.0 px** of headroom left after
the v0.17.0 trim (measured on `rework`) and three extra lines would have cost far more than
that; the chevron rides in the name's existing line box and costs zero. Extra windows get
`data-win="extra<N>"` keyed on the **index**, never the label: an extra window's label is
vendor text, and a key built from it would open the wrong window the moment it changed.

**The affordance follows the READING, not the markup.** `setGauge` toggles `tappable`
(and `aria-disabled`) from whether the window has a finite utilization, so a gauge showing
em-dashes has no chevron, no pointer and no promise — which is the state `?mock=empty`
paints, and where `openForecastSheet` turns the tap away anyway. Measured on `empty`:
chevron computes to `none` on both fixed gauges and neither tap opens anything; on
`rework` and `hot` all three gauges carry the chevron and all three open.

**One measurement worth keeping.** `.fc-key` shipped at 16 units and at **840x344** the
widest label, "utilization", needs **56.2 px** against a 55.03 px basis — so min-content
won on that one row and its value column started 1.2 px right of the other three, which
is the misalignment a fixed column exists to prevent. **18 units** (61.9 px small, 129.6 px
at 2560x720) makes the basis the deciding number again; all four values now start at the
same x at both slots. Re-measure against "utilization" if a longer key is ever added.

### 3. The nightcap now outranks the sunglasses during quiet hours

**The defect, and it is the hard hat's defect one rung down.** Quiet hours is the operator
saying *night mode*, and a busy night is the ordinary kind — a long run left going
overnight is *every session working with the limits calm*, which is precisely the
sunglasses' condition. With sunglasses above it in the ladder, the nightcap could only
appear on a night the fleet was **also** idle or mixed: the costume for "it is night" was
unreachable on exactly the nights there was something to watch.

Quiet is a fact about the **clock**; the sunglasses are a fact about the **work**. So the
clock wins inside quiet hours and the sunglasses keep every hour outside it.
`ACCESSORIES` is reordered to `['party', 'nightcap', 'sunglasses']` to match — it is the
readable copy of the ladder, and a list that disagreed with the ifs is worse than no list.

**All eight trigger branches re-verified against the shipping `desiredAccessory`**, one
behaviour changed and seven identical:

| # | condition | 0.18.0 | 0.19.0 |
|---|---|---|---|
| 1 | `status` not `live` (stale / connecting), quiet or not | `''` | `''` |
| 2 | `crabStyle` = plain, quiet or not | `''` | `''` |
| 3 | any session `needs_input` — **including during quiet** | `''` | `''` |
| 4 | party latch open — **still outranks quiet** | `party` | `party` |
| **5** | **quiet + every session working + limits calm** | **`sunglasses`** | **`nightcap`** |
| 5b | quiet + mixed grid / no sessions / hot limits | `nightcap` | `nightcap` |
| 6 | every session working + limits calm, **not** quiet | `sunglasses` | `sunglasses` |
| 7 | every session working + a window **into the amber** | `''` | `''` |
| 8 | mixed grid, empty grid, all idle | `''` | `''` |

Driven end to end as well, not only through the ladder: with a 4-working quiet estate
`applyWardrobe` holds the change for the 10 s `ACC_STABLE_MS` anti-flap (`data-acc` still
`''` on the first tick), lands `nightcap` on the second, and drops it **instantly** to
`''` the moment a `needs_input` row appears — the "alerts stay serious" bypass, intact.
The nightcap group computes `display: block` and the shades `none`; daytime is the exact
mirror. **No accessory art changed** — the SVG groups are untouched — and `&crab=nightcap`
/ `&crab=sunglasses` still force both with the list reordered.

### Verified

`node --check`, strict-XML parse of `index.html`, JSON parse of every fixture + manifest
+ translation: all pass. `icuewidget validate widget` clean.

Headless Edge at **2560x720** and **840x344**, twelve captures with a DOM probe on each:
**page overflow 0x0, every zone 0.0, sheet panel overflow 0** in all of them — the base
panel with the chip, all four forecast branches, the three history cases, both wardrobe
costumes, and both sheets at the small slot. Live browser at exactly 2560x720: the gauge
taps, the chip tap, the no-latch retry and the recovery.

The full overflow probe walks **every element in every zone against that zone's own
content box**, plus the page against the viewport — the method the v0.17.0 wave used, and
the reason a chevron added to three gauge names and a third chip on the sessions header
are known not to have cost the Limits zone its remaining headroom.

## v0.18.0 — the frozen sensors, and the wardrobe redrawn

### The CPU/GPU temperatures never changed, and the wrapper was why

**The report:** the temperatures on the Edge are stuck at whatever they read when
the panel booted, forever. The clock ticks, so timers are running.

**The mechanism, found in our own bundled wrapper.** `SimpleSensorApiWrapper` is
not injected by iCUE and is not a missing file — it is inlined in the `<head>` of
`index.html`, verbatim from Corsair's `common-tools` reference, which is exactly
where the bug was. The reference's `request()` calls the plugin method **before**
registering the request in `pendingRequests`:

```js
const requestId = this._nextRequestId();
method.call(this.plugin, requestId, ...args);   // <- answer can arrive HERE
const timeoutId = setTimeout(...);
this.pendingRequests.set(requestId, { ... });   // <- too late
```

Qt delivers a signal **synchronously** when the connection is direct and the
provider can answer without going to hardware — which is precisely what a sensor
provider does once its cache is warm. `asyncResponse` then fires *inside*
`method.call`, `_handleAsyncResponse` looks in an empty map, drops the answer, and
the promise sits until the 5 s timeout and rejects. So: the **first, cold** read
per sensor is genuinely async, resolves, and paints a number. Every read after it
is answered from cache, synchronously, and is dropped. One good reading at boot,
then nothing — the exact report.

**Three fixes in the wrapper**, all departures from the reference text:
1. **Register before calling.** A synchronous answer now lands in a map that
   already has somewhere to put it.
2. **`Number()` the requestId on both sides.** The map is keyed by a JS number and
   a `Map` lookup is strict; a transport that echoes the id back as a string
   misses silently and every read times out.
3. **A throwing method rejects** with the real reason instead of leaving a pending
   entry to time out 5 s later with the error lost.

Rejections now carry a `code` (`"timeout"` / `"call"`) so a caller can classify one
without matching on message text.

**Two more fixes above the wrapper**, in `sidecrab.js`:
- **`sensorValueChanged(sensorId, value)` carries the new value and we were
  throwing it away**, calling `refreshSensors()` to go ask for a number iCUE had
  just handed us. It is now rendered directly. That matters because it is a live
  reading that owes the request path nothing — a broken request path can no longer
  freeze the row on its own.
- **Units are cached** (5 min TTL, plus the `sensorUnitsChanged` signal) instead of
  being re-requested beside every value. Pairing them doubled the traffic through
  the bridge, and `Promise.all` meant a units failure blanked a value that had
  arrived perfectly well.

**Proved in Chromium against a fake Qt-style plugin** — 16 pass, 0 fail, **6
mutation proofs**. The suite extracts the wrapper from the *shipping* `index.html`
at run time (a copy of a wrapper is a copy that drifts) and runs the pristine
v0.17.0 text from a HEAD checkout through the same cases:

| case | v0.18.0 | v0.17.0 |
|---|---|---|
| async provider, 4 sequential reads | `41 42 43 44` | `41 42 43 44` |
| sync-answering provider | `41 42 43 44` | 4x `[timeout]` |
| **cold read then warm cache (the operator's bug)** | `41 42 43 44` | **`41` then 3x `[timeout]`** |
| requestId echoed back as a string | `41 42 43` | 3x `[timeout]` |
| 30 reads on a warm-cache provider | 30/30, 30 distinct, 41→70 | read #2 **never settles** |
| throwing method | rejects `code:"call"` in 0 ms | rejects, no code |
| out-of-order value/units | no cross-talk | no cross-talk |
| pendingRequests after settle | 0 | 0 |

### The staleness cue, and why it is not keyed on the value

The defect above was invisible from the far side of a desk: a number that has
stopped being re-confirmed looks exactly like a machine sitting at one
temperature. So the panel now says which it is.

- Every read outcome — resolve, reject, timeout, empty — is recorded with an ISO
  timestamp and elapsed ms in a ring buffer at **`window.__sidecrabSensorLog`**
  (last 80). The console gets **failures and health transitions only**; a healthy
  panel reads two sensors every 10 s and logging every one is ~17,000 lines a day
  of "still fine". `&sensorlog=1` logs every outcome.
- **If every read of a sensor has FAILED for more than 60 s while a number is
  still on the glass, that number is dimmed** (`.sensor-v.stale`, opacity 0.38).
  Opacity and not a colour, because the value's colour already carries the 80/90
  threshold — a stale reading that was red must go on being red while it fades.
- **Keyed on read failures, never on the value not changing.** A machine idling at
  47 °C for an hour is healthy; dimming that would be the panel crying wolf at the
  truth.
- **A single failed read does not blank the row.** A blip erasing a correct reading
  is its own lie. The number is kept, and dimmed at 60 s. A read that *resolves*
  with nothing readable still hides the row immediately — that is absence, which is
  a different statement from a comms failure.
- **The cue rides the 1 Hz tick, not the 10 s sensor reconcile.** A reconcile that
  has itself stopped firing is one of the ways this row freezes, and a cue that
  could only be raised by the thing that broke would never be raised.

**Verified** with `?mock=normal&sensors=63,71&sensorfail=1&sensorstale=4000`:
`ok 63 °C` at T+0, `timeout` for both sensors at T+10 s, value still reading
`63°C` and carrying `.stale` at T+15 s with the row still shown.

### What to look for on the glass after importing 0.18.0

The real defect only manifests under iCUE, so this is the operator's checklist:

1. **Watch the CPU/GPU numbers for two minutes.** They should move. A machine at
   idle moves 1–3 °C on its own; if you want a bigger swing, run something heavy
   for thirty seconds and watch them climb and settle.
2. **If a number is dimmed**, the reads are failing — that is the new cue doing its
   job, and the value beside it is the last one that actually arrived. That is a
   report worth sending back, not a rendering glitch.
3. **If a number is bright but still not moving**, the reads are succeeding and
   iCUE is handing back the same figure. That is a different fault from this one
   (it would be in the provider, not in us) and it needs the ring buffer:
   attach a debugger to the widget and read `window.__sidecrabSensorLog` — every
   entry will say `ok` with a timestamp and the same value, which is the evidence
   that distinguishes the two cases.
4. **If the sensors row is missing entirely**, iCUE reported no sensor for the
   selected ID — check the two `sensors-combobox` settings in the widget's
   properties panel. The row is hidden by design rather than showing `NaN °C`.

### The wardrobe, redrawn on the doubled grid — and cut to three

The v0.11.0 accessories were rejected by the operator on sight, and the reason is
legible in the old coordinates: **every rect was a whole or half cell**, because
that was the entire grid v0.11.0 had. The party hat was a two-step ziggurat, the
sunglasses were one 36-unit black slab across the shell, and the nightcap was
three stacked bars. Those are *silhouettes* — the shape of a hat with none of the
shape of a hat.

v0.17.0 doubled the grid for exactly this. The addressable step is a **quarter
cell** — 1 unit, **8.9 px** at the 2560x720 slot — which is enough for a frame rim,
a stripe, a taper step and a pompom.

| accessory | worn when | what changed |
|---|---|---|
| sunglasses | **every** session working, none waiting, **and no usage window into the amber** *(and, since v0.19.0, **not** during quiet hours — see that section)* | two separate lenses with a 1-unit frame rim, a bridge, and temple arms that run out past the shell edge — a temple that stops at the silhouette reads as a painted stripe rather than as something worn |
| party hat | for 60 s after `recap.doneToday` **increments** | a tall narrow cone — **10 units wide by 12 high**, three tiers stepping 1 unit per side, a proud brim, 1-unit stripes and a flush cream pompom |
| nightcap | quiet hours are active *(and since v0.19.0 that is enough — it no longer has to wait for the fleet to go idle as well)* | a fur band, a cone that leans left and flops over, and a pompom hanging off the left shoulder — still left, because the sleep Z lives off the **right** shoulder |

**"limits are calm" is new and it is load-bearing.** The sunglasses are the costume
for *nothing needs attention*, so an estate three percent off its weekly cap has
not earned them. `limitsCalm()` reads the same `limits` block the gauges render
from, against the gauges' own amber step, so the crab and the gauge can never
disagree. Absent limits count as calm — a panel that cannot see the limits has no
business claiming they are a problem.

**The hard hat is retired.** It fired at 3+ working, which sits *inside* the state
the sunglasses exist for, so on any busy estate the sunglasses were unreachable:
the costume for "everything is going well" could only be seen when not much was
going on. Three costumes, chosen, beats four with one of them in the way.

**Two things the shots caught that the coordinates did not.**

- **The party hat was `#E06A2B` against a crab of `#E45C28`** — four units apart in
  red, fourteen in green. On the glass it did not read as a hat at all; it read as
  a lumpy extension of the crab's own head. It is `#F4BC45` gold now, two clear
  steps up in luminance and yellowness while staying inside the widget's warm
  range (the amber token is `#E8A33D`). The stripes are **red on the gold**, not
  cream: cream on gold is a ~15% luminance step, which is a stripe nobody sees
  from across a room, and this hat is only ever up for sixty seconds.
- **And then the hat was a cake.** The first gold cut was **22 units wide by 10
  high**, stepping in 2 units per side — wider than it was tall, on a 45-degree
  slope. Reviewed off a render it read as *a stack of golden pancakes*, not a
  cone. **A party hat reads as a party hat because it is taller than it is wide
  and its sides are steep**, and almost nothing else about it matters.
  The vertical budget is fixed at 12 units by the viewBox — growing it would move
  every crab pixel and cost 6% of the animal at the small slot — so height had to
  be bought two ways. **Downward:** the brim now sits at y 7..8, two units *into*
  the shell instead of resting on its top edge, which is how a hat actually sits
  on a head and clears the eyes at y 10 by two units. **And by narrowing**, which
  is the part that needed measuring rather than eyeballing: at a 12-unit brim the
  bounding box came out **exactly 12 x 12**, a square that still read as a cone but
  was not one. At 10 it is **10 x 12, 1.20:1** — 89 x 107 px at the big slot.
  Re-measure the bbox if these coordinates are ever touched.
  One more trap on the way: tapering to a 2-unit tip put a 4-unit pompom
  overhanging a half-cell neck, and it stopped reading as a cone and started
  reading as a **trophy**. Three tiers (4 / 6 / 8), pompom flush with the tier it
  caps.
- **The nightcap had a hole in it.** The first cut left the pompom hanging in space
  below the tip and a 4x2 dark notch between the tip and the cone. What it read as
  was not a cap with a bobble but a grey square that had fallen off the crab. Every
  joint is continuous now — band to cone at y 3, cone to cone at y 0 and y -2, tip
  to pompom at y 1 — and each shares a **full edge**, not a corner. Corner-to-corner
  reads as a diagonal at badge scale and as a broken shape at this one.

**Proved non-destructive, the same way the grid doubling was.** The crab SVG is
rasterized at **520 x 440** (10 px per viewBox unit, **228,800** pixels) and diffed
byte for byte against a pristine HEAD build, every mood, accessory layer off:

| case | differing pixels |
|---|---|
| content / waving / asleep / worried / celebrating, accessory OFF | **0 / 228,800** each |
| sunglasses ON vs bare crab *(negative control)* | 22,800 |
| party ON vs bare crab *(negative control)* | 6,800 |
| nightcap ON vs bare crab *(negative control)* | 29,800 |
| content vs waving, same build *(method check)* | 6,400 |

The last two blocks are not decoration. The **negative controls** prove the diff
can see paint at all — a diff that reports zero because it is blind proves
nothing. The **method check** proves the rasterizer is capturing the CSS
transforms that park the arms: the moods differ *only* by those transforms, so if
they were being dropped every mood would rasterize identically and all five rows
above would pass for the wrong reason.

### Dev flags added in 0.18.0

| flag | what it does |
|---|---|
| `&sensorlog=1` | every sensor read outcome to the console, not just failures and transitions |
| `&sensorfail=1` | first read of each sensor resolves, every read after it rejects — **the operator's bug shape**, not a generic outage, so the staleness cue can be watched arriving |
| `&sensorstale=<ms>` | shorten the 60 s staleness window for a screenshot |

`&crab=sunglasses\|party\|nightcap\|none` and `&mood=` are unchanged from v0.17.0
and are how the costumes are shot. The forced sensor bridge now runs the 10 s
reconcile too — without it the flag read each sensor exactly once, which was enough
for a v0.17.0 screenshot and useless for watching a sensor go stale.

## v0.17.0 — the legibility wave (driven off the physical screen)

Six changes, five of them reported by the operator looking at the real 2560x720
Edge running 0.16.0. Every figure below was measured on the DOM at exactly that
viewport, not read off the CSS.

### The Limits zone could not fit its own sensors row

**The report:** "text below the daily usage graph is cut off."

**The mechanism, measured.** `.zone-limits` is a fixed-height flex column: 720 px
tall, 28.8 px of padding, content box ending at **691.2**. `.today` inside it
carries `margin-top: auto`, so *every* spare pixel in the zone collects in that
one auto margin — it is the zone's entire slack budget. The sensors row is the
**last** flex child, after `.today`, and costs **62.4 px** (18.72 zone gap +
10.08 own margin + 33.6 border-box height).

On `?mock=rework` — the fullest production shape, carrying a forecast line, a
`limits.note`, the budget line and the cost line — that auto margin was already
down to **4.67 px** with the row hidden. Reveal it and the row landed at
**715.3..749.0**: 24 px below the panel's bottom edge, 57.8 px past the zone's
content box. `body { overflow: hidden }` ate it silently. Nothing scrolled, no
error, no clue — the hardware readout simply did not exist on glass. On `normal`
and `hot` (no note, no budget/cost lines) it fitted with *exactly zero* slack,
which is why this looked fine right up until it didn't.

**The fix: 77 px found inside the Limits zone, and nowhere else.** The identity
and grid zones measured fine and must not move, so nothing was taken off the
shared `--space-gap` / `--space-pad` tokens:

| lever | from | to | frees |
|---|---|---|---|
| `.zone-limits` gap | 2.6 vmin | 1.9 vmin | 30.2 px (6 gaps) |
| `.zone-limits` vertical padding | 4 vmin | 3.1 vmin | 11.5 px |
| `.limits-head` min-height | `max(48px, 8.4vmin)` = 60.5 | `max(48px, 6.7vmin)` = 48.2 | 12.3 px |
| `.today` gap | 1.2 vmin | 0.9 vmin | 8.6 px (4 gaps) |
| `.sensors` margin-top + padding-top | 1.4 + 1.2 vmin | 0.9 + 0.9 vmin | 5.8 px |
| `--spark-h` | 8.5 vmin | 7.4 vmin | 7.9 px |

The header trim is the one to re-measure before touching again: **48 px is the
floor**, and 60.5 was headroom above it, not the floor itself. The `max(48px, …)`
keeps it at 48 on any slot where vmin is smaller.

**Result, measured with `&sensors=72,54` on every fixture:** the sensors row is
the last child and ends at the content-box bottom (697.7) on all of them, with
the auto margin holding real headroom — **20.0 px on `rework`**, 123.9 on `hot`,
183.4 on `caveat`, 193.0 on `recap`, 217.5 on `normal`. A full overflow probe
(every element in every zone, against both the panel and its own zone content
box) reports **zero offenders on all twelve fixtures**, at 2560x720 and at
840x344.

**Known, NOT fixed — two `limits.extra` windows still overflow.** The contract
allows `extras.slice(0, 2)` and no fixture carries two. Injecting a second window
into `rework` puts the sensors row 78.6 px past the content box (56.3 px below
the panel edge). This is pre-existing and this wave *halved* it — before the 77 px
it was ~156 px — but it is not closed. Gap tuning cannot close it: a second
window costs 98.6 px on its own. It needs a decision about what the zone drops
(Rule 6) rather than more trimming, so it is a row for the next wave.

### The fleet dots were two letters nobody could read

**The report:** "below the date there is a g and a t with an orange dot and a
green dot, I don't understand what that is for."

**The mechanism: no truncation anywhere.** The letters were **literal text nodes
in `index.html`** — `<span class="fleet-k">g</span>` — from v0.6.0. `renderFleet()`
writes only `data-state`, `title` and `aria-label`; the source comment beside
`FLEET_PARTS` says so outright ("the g / t LETTERS are static markup in
index.html, not here"). The words *did* exist, in the title and the aria-label —
a tooltip and a screen reader, neither of which exists on a panel behind glass.

Rendered: **15.12 px** mono, glyph box **8.86 x 15.1 px**, colour `--faint`. And
the whole row occupied **62 px of a 462 px column** — 13% of the width it had.
The room was never the constraint.

**The fix.** Full words, `2.5 vmin` (18 px — deliberately between `--fs-meta` at
15.12 and the date above at 20.88, so it is readable without competing with the
date), letter-spacing 0.08em, and a dot grown 1.3 → 1.6 vmin so it is not a speck
beside an 18 px word. The gap *between* the pairs (3.4 vmin) now beats the gap
*inside* one (0.9 vmin), or "glow o toast o" reads as one run of four things. The
row now uses ~160 px of the 462. **The state semantics are untouched**: green
filled disc = running, amber hollow ring = stopped-or-parked, bare dash =
absent/unknown, and the shape is still the redundant cue for a monochrome photo.
The parked glow reads as calm-informational, which is correct — it is parked on
purpose.

### The crab grid doubled, and the crab did not change

`viewBox 0 -2 26 22` → **`0 -4 52 44`**, every coordinate multiplied by exactly 2.

**Read the grammar carefully, because the obvious reading is wrong.** A CELL is
the same physical size it always was (35.5 px at this slot) — it is now **4 units
where it was 2**. What doubled is the *addressable step*: the finest thing this
art can express went from a half-cell to a **quarter-cell**. That is why the crab
looks identical, and why it must.

**Proved, not asserted.** A build with the nudge, the badge growth and the
identity-gap change all reverted — leaving the grid as the only difference
against `HEAD` — was screenshotted against the baseline at 2560x720 and diffed:

| mood | differing pixels |
|---|---|
| content / waving / asleep / worried | **0 / 228,800 each** |
| sunglasses / party / hardhat / nightcap | **0 / 228,800 each** |

Painted crab **462.1 x 391.0 px**, body rect **319.9 x 177.7 px** — unchanged
either side. The viewBox unit halved, 17.772 → 8.886 px.

**Why bother, if it paints the same:** the wardrobe is the next piece of work and
it had run out of grid. A hat brim, a lens bridge or a pompom could only ever be
a half-cell block (~18 px here); it can now be ~9 px, so an accessory can have a
shape instead of a silhouette. **No accessory art changed** — the wardrobe groups
were scaled with everything else and are otherwise untouched, deliberately.

**Two things ride on the factor and both moved with it.** All **ten** CSS
transforms that park an arm, snap a claw or hop the rig are written in viewBox
units (`transform-box: view-box`), so every one doubled — `-2px` → `-4px`, the
esc2 lift `-4px` → `-8px`, `clawsnap` `-1px` → `-2px`, `juggle` `±11,3` →
`±22,6`. And the **badge** crab is no longer on this grid: it kept the v0.11.0
numbers, so "the badge's own glasses, verbatim" now means the same shape at twice
the coordinates, not the same integers.

**Trap for the next person.** A bare `.replace()` on
`.crab[data-mood="waving"] .arm-right { transform: translateY(-2px); }` rewrites
the **`body.quiet`** rule three lines below instead — the bare selector is a
substring of it. Anchor on the leading newline. An assertion caught this; an
unanchored replace would have shipped a quiet-hours crab waving at the wrong
height.

### The crab moved down, and the badge grew

`.crab { transform: translateY(calc(var(--layout-unit) * 1.4)) }` — **10.1 px**
here. A transform and not padding: the SVG is width-limited in this zone and
centres itself in its box, so anything that changed the BOX would resize the crab
as a side effect. `.crab` carries no other transform (the mood parks, the snap
and the bounce all ride `.rig` / `.arm` / `.claw` *inside* the SVG), so nothing
overwrites it.

**Clearance is measured on the ART, not the box** — the viewBox carries empty
headroom for the hats and empty floor below the legs, so box-to-box gaps overstate
the risk badly. Union of the visible rects: **213.1..461.9**, badge bottom 97.9,
clock row top 521.4 → **115.2 px clear above, 59.5 px clear below**. No collision.

The badge went 40 → **48 units** (+20%): 288 x 57.6 → **345.6 x 69.1 px**, 5:1
ratio untouched, 58 px of clear column each side. It gained shading in the same
pixel grammar — two literal tones either side of the badge orange, applied as
grid-aligned rects **inside the existing silhouette**, so the outline is
unchanged: shadow under the shell, on the lower row of each claw and at the foot
of each leg; a half-cell highlight along the shell's top edge, the one strip the
lens bar does not cover.

**The badge growth was not free, and it was caught by measuring.** The badge
(+11.5 px tall) and the fleet row (+2.9 px) both came out of `.crab-wrap`
(`flex: 1 1 auto`), which silently dropped the crab out of WIDTH-limited into
HEIGHT-limited: **462.1 x 391.0 → 456.2 x 386.0**. The identity zone's gap was
trimmed 2.6 → 2.2 vmin to hand back 5.8 px — more than the 4.9 the crab needed —
and the painted size is back to 462.1 x 391.0.

### The approval threshold now seeds from the feed

`/v1/state` carries a top-level `toast` block: `{ thresholdSec, enabled }`
whenever crabd is serving config at all, plus **`approvalThresholdSec` only when
the operator has set it on disk**. The widget presence-detects it (`noteApprovalSeed`)
and uses it as the DISPLAY value when the panel slider has not been touched
(`effectiveApprovalSec`). iCUE properties are read-only to a widget, so the host
slider cannot be moved to match and the panel does not pretend otherwise — it
reports the effective figure in the approval sheet instead, as
`toast after 45 s (saved)` or `toast after 90 s (panel)`.

**The seed is NOT a touch, and that separation is the whole point.** `noteApprovalSeed`
does not write `approvalSeenSec`, does not set `approvalTouched` and does not save
prefs. `approvalTouched` means *the operator moved the iCUE slider*; a value from
crabd is the operator having edited `config.json` instead. Letting the seed set
the latch would put `approvalThresholdSec` into every subsequent toast write —
exactly the materialise-an-unset-key failure v0.16.0 exists to prevent, and it
would overwrite the on-disk figure the seed was read from with the property's
default.

Presence is detected on the **member**, with `hasOwnProperty` and not truthiness:
an older crabd sends no `toast` at all, a current one sends the block *without*
the member until the operator sets it, and a contract-legal `null` must not become
`Number(null) === 0`. All three land on `null`.

Fixtures: **`rework` = seeded** (`approvalThresholdSec: 45` — deliberately not 20,
the property default, and not a slider step, so a panel showing the property
instead is visibly wrong in the shot); **`dense` = unseeded** (block present,
member absent); every other fixture keeps no `toast` block at all (older crabd).

Verified at 2560x720, four states and both invariants:

| state | effective | sheet line | `approvalThresholdSec` in the toast write |
|---|---|---|---|
| seeded, untouched (`rework`) | 45 (feed) | `toast after 45 s (saved)` | **absent** |
| touched (same `&uid=`, `&approvalsec=90`) | 90 (property) | `toast after 90 s (panel)` | present |
| first observation is a BASELINE, not a change (fresh `&uid=`, `&approvalsec=300`) | 45 (feed) | `toast after 45 s (saved)` | absent |
| unseeded (`dense`) / older crabd (any other) | 20 (property default) | line hidden | absent |

**Two bugs found by testing this, both fixed.** (1) The sheet line went stale for
up to a poll after the operator moved the slider — the latch flips in the config
sync, which is not on the render path, so `noteApprovalThreshold` now repaints the
line itself. (2) `fmtApprovalSec` rounded minutes, and printed **"2 min" for a
90 s threshold**. The slider steps by 5 up to 300, so 90 and 135 are ordinary
settings; minutes are now used only for a whole number of them.

## v0.15.0 — the reading wave

Five features and one fix, no contract change: every field below is already in
schema 5 and everything is presence-gated as usual.

**The queued chip closes the tap-to-continue loop.** `sessions[].queuedContinue`
arrived in the v0.14.0 contract and crabd has been serving it since; the widget
did not render it, so a queued tap was invisible the moment the sheet closed. It
is now a muted `queued: <label>` line on the card, between the body and the
badges. The label is mapped back from the wire prompt through `CONTINUE_DEFAULTS`
where it matches a known button, and trimmed to `QUEUED_LABEL_MAX` where it does
not — so the card reads back what the finger tapped, not the sentence that went
on the wire.

**Presence is the whole freshness test, deliberately.** The contract says crabd
re-derives freshness from `queuedAt` before it serves the field, so "a card never
advertises a prompt the Stop hook would no longer deliver" is already true one
side over. A second expiry clock here would be a copy of a rule that can disagree
with it: tighter and it hides a live queue, looser and it shows a dead one.
`queuedAt` is read as a shape check and never as a deadline.

**The chip is card STRUCTURE**, so it is in the card signature and it clears in
both directions. The clearing half is the half that matters — proved live: with
the feed frozen (`acceptDoc = function(){}`), nulling `queuedContinue` on the live
document and calling `render()` takes the chip off, and putting the field back
brings it straight back.

**The approval countdown says whether the tap still matters.** crabd holds the
PermissionRequest hook ~55 s (`APPROVAL_HOLD_SEC`, measured off the contract, not
guessed) and then returns the pass-through that lets the terminal dialog appear.
Both the approval card and the approval sheet now count that hold down from
`pendingPermission.requestedAt` on the existing 1 Hz tick — never on the 3 s poll,
because a countdown that jumped three seconds at a time would be a worse answer
than none. At zero: `expired — decide in terminal`.

Two rules the countdown keeps. **A missing or unparseable `requestedAt` renders
NOTHING, not `expired`** — unknown is not expired, and sending someone to a
terminal that is still waiting on the panel is the worse error of the two. And
**the buttons stay enabled past zero**: the panel's clock is not crabd's, so a
countdown this widget cannot verify must not take a control away from the person
standing in front of it. They dim; the line says why.

**The two header chips.** A session filter cycling All → Waiting → Working →
Done/Idle, and a density toggle, both `≥ --touch-min` on both axes and both
persisted through the SAME vendor local-storage object the pins use (one JSON
object per widget keyed on `uniqueId`; `sessionFilter` and `density` are
properties inside it, never keys of their own). `loadPins`/`savePins` became
`loadPrefs`/`savePrefs` and write every persisted property together, so a pin tap
and a chip tap cannot race each other's copy of the object.

**The filter is a VIEW, not a state the panel is in.** The glow, the crab's mood,
the toast threshold and the ack-all gesture all still see every session — a panel
that stopped glowing because someone left it on "Working" would be a filter
hiding the one thing this widget exists for. Only the card list and the count
narrow, the count to `showing N of M`, and only when rows were actually removed:
a "Working" chip on a panel where everything is working hides nothing, so the
header stays the header it was. A filter that empties the grid says so in its own
words (`No sessions waiting on you`) rather than borrowing "No active Claude
sessions", which would report the filter's answer as the fleet's.

**Density is the one place this widget breaks Rule 6 on purpose** (hide, never
shrink) — because the operator asked, by tapping a chip, which is what separates
a layout decision from a slot the panel found itself in. Compact is a third grid
row; the smaller type, padding and gap are what pay for that row, and the
subagent rows go the way Rule 6 sends them. **Measured at 2560x720 on `dense`:
comfortable 4×2 = 8 cells, compact 4×3 = 12 cells, both with 0 card, 0 zone and
0 page overflow.** At 840x344: 2×2 = 4 and 2×3 = 6. Nothing in JS knows the
number is three — `gridCapacity()` reads BOTH axes off the computed style, which
is the same reason the column count has been read that way since v0.6.0.

**The day view's row cap is now MEASURED, not declared** (the pre-existing fix).
`DAY_ROWS_MAX = 18` was measured at 2560x720 and was wrong everywhere else: at
840x344 the same eighteen rows plus the tail were **234 px of list in a 216 px
box**, so the rows scrolled and the `+N earlier` line — the one line that admits
the rows exist — went out of the panel with them. The slot is not the reason on
its own. `--touch-min` has a hard **48 px floor**, so at the small slot the sheet
head's controls take 48 px where proportionally they would take 29, and the list
pays the difference; that makes the cap a function of the slot's pixel height AND
of the floor, which is not something a constant can be.

`fitDayRows()` fills the list, appends the tail, and then trims from the end while
`scrollHeight > clientHeight` — the browser's own answer to "does this overflow" —
so the cap is a function of the real box at the real slot with the real font
metrics. The tail is appended BEFORE the loop and measured with the rows, because
fitting the rows and then pushing the tail out is the same bug one line smaller.
The slot is now part of `daySig`, or a resize with the day open would keep the
other slot's fit. `DAY_ROWS_MAX` survives as the ceiling on the first pass.
**Measured after: 2560x720 → 18 rows + `+6 earlier`, 0 overflow (unchanged);
840x344 → 16 rows + `+8 earlier`, 0 overflow, tail inside the list.**

**D15 — the approval card's summary, and the same trap one element over.** The
countdown was added inside `.card-approval`, which was `flex: 0 1 auto` around a
`flex: 0 1 auto` summary. That is the `.card-question` bug again: a
`-webkit-line-clamp` only ellipsises the line it is ALLOWED TO FINISH, so a box
squeezed below a whole number of line boxes ends its last line mid-glyph and
`overflow: hidden` eats the descenders.

Measured before the fix, headless Edge:

| slot / density / fixture | `.card-approval` squeeze | summary line boxes |
|---|---|---|
| 2560x720 comfortable, `rework` | 0 | **1.755** of a 2-line clamp (33.95 px on a 19.35 px line height), countdown 2.16 px under it |
| 2560x720 comfortable, `dense` | 0 | 1.999 — clean, which is why this is easy to miss |
| 2560x720 compact | 0 | 2.000 — clean |
| 840x344 compact | **4 px** | 1.999, but the countdown was painted **3.7 px below its own clipping box** |

Two instances of one class, and the fix is the one the earlier lane used: the
summary is pinned to exactly its clamped line count and refuses to shrink
(`flex: 0 0 auto`, `height` = clamp × line height, `max-height: none`), and
`.card-approval` is `flex: 0 0 auto` so it cannot be squeezed around children
that now have definite heights. What gives instead is the **subagent list**, per
Rule 6 — `.card.approval .card-subs` is hidden: the `N sub` badge still says they
exist and the sheet still lists them whole, whereas a permission request that
loses the bottom of its summary or its countdown has lost what the card is for.
At 840x344 in compact the summary drops to a one-line clamp, which sheds 7.7 px
against a 4.3 px deficit. **After: all eight slot × density × fixture
combinations at 0 squeeze, whole line boxes, countdown inside the box.**

The lesson is the project's own: the comment on `.card-approval` said the summary
"loses lines from the bottom instead of pushing the badges out", and it did — it
just was not allowed to lose WHOLE ones. A layout claim is a measurement or it is
nothing.

## v0.14.0 — the touch wave

Four gestures, one pointer stream, no contract change: everything below uses the
existing `/v1/action` endpoint and the fields already in schema 5.

**They are not four features, they are one arbitration problem.** Every gesture
competes for the same finger, so there is ONE pointer map (`pointers`) and one set
of ordered thresholds in `sidecrab.js`, and every discrimination decision is made
in the same place. `TAP_SLOP_PX` (10) is the smallest: under it nothing has moved
and the interaction is a tap or a hold. `SWIPE_ARM_PX` (12) and `PULL_ARM_PX` (20)
sit above it, so a pointer only commits to an axis once the finger has clearly
chosen one, and **nothing re-decides after that** — a swipe that drifts upward
stays a swipe, because a gesture that changed its mind halfway would abandon a
card mid-flight for reasons nobody can see.

| gesture | where | what commits it | what it does |
|---|---|---|---|
| swipe | a `done`/`idle` card | horizontal, `>= 60 px` on release | the existing dismiss |
| long press | any card | still for 600 ms | pin / unpin, with a confirm |
| two-finger tap | anywhere | two pointers down and up inside 700 ms, each under 14 px | ack-all, with a counted banner |
| pull down | the top 56 px, outside the sheet | downward, `>= 80 px` on release | an immediate `poll()` |

**THE CLICK IS STILL THE TAP.** None of this replaces the v0.13.0 click handlers —
a tap on a card opens its sheet through `onCardsClick` exactly as before. What the
layer adds is the ability to SWALLOW that click when a gesture consumed the
interaction, done once in `onClickCapture` on the **document, in the capture
phase**. A per-handler guard would have to be added to every control the panel
ever grows, and the one somebody forgot would be the one that opened a sheet under
a swiped card. The rule it enforces is one line in `onPointerUp`: **a drag is
never a tap**, whatever it did or did not do — which is exactly what makes a
horizontal drag on a *working* card a true no-op rather than a sheet open.

**`touch-action: none` on `.zones` is load-bearing, and it is where the wave
nearly shipped broken.** The gestures are driven from PASSIVE listeners — nothing
calls `preventDefault` — so the axis has to be claimed declaratively or the
compositor claims it first and answers with `pointercancel` instead of moves. An
earlier version put `touch-action: pan-y` on `.card.swipeable` only. The swipe
worked (horizontal was left to the page) and **the pull-down silently did nothing**
(vertical was not), which is precisely the asymmetry that rule produces and is
invisible to anything short of a live-browser test. `.zones` is the right level:
html/body are `overflow:hidden` and nothing inside it scrolls in either direction.
**The sheet is untouched** — it lives outside `#zones`, and all four of its
scrollable regions keep the browser's own panning.

**The card grid defers its rebuild while a finger is on a card** (`renderSessions`,
gated on `gestureHoldsCards()`). The grid rebuilds by throwing every card away, and
a 3 s poll landing mid-swipe is well inside one gesture. `cardSig` is deliberately
NOT advanced, so the difference is still there for the render `endSwipe` fires when
the finger lifts; the age-relabelling loop below it is skipped with the rebuild,
because `visible` and the surviving DOM can disagree about which row is at which
index and writing one's timestamps onto the other is how a card ends up aging from
another session's clock.

**The card signature signs the rendered repo LINE, not `repo` and `branch`
(v0.16.0, audit F1).** That line falls back to `cwd` when there is no repo, which
makes `cwd` a visible value — and the signature carried only the two fields, so a
repo-less session whose `cwd` moved kept the old path until something else rebuilt the
card. `repoLine()` is now the one expression the card, the sheet and the signature all
use, which is what stops them drifting apart again.
Verified in Chromium on `?mock=normal` with session 1 edited to
`repo: null, cwd: "C:\Dev\alpha"`: the card reads `C:\Dev\alpha`; changing only `cwd`
to `C:\Dev\beta` on disk repaints the card within one poll. **Mutation-proved** — with
the signature reverted to `s.repo, s.branch` the same edit leaves the document at
`beta` and the card still reading `alpha`.

**The pin confirm is render state, not a class poked onto a node** — because an
UNPIN has no glyph left to animate. `buildCard` draws a pin marker on a session the
pin map says is not pinned, for `PIN_FLASH_MS` (900) only, and `pinFlashFor()` is
in the card signature in BOTH directions so the flash clears itself.

**The ack-all is one function now.** `ackAllWaiting()` was split out of
`onCrabTap()` so the two-finger tap makes the same write rather than a second copy
of it that can drift; it returns the count, which is what the banner says and what
the crab tap tests to decide whether to blink instead. The blink moved OUT to
`onCrabTap` — an eye-flicker is the crab's answer to being tapped, and a gesture
that may be nowhere near the crab has no business firing it.

**Trap (2026-08-26, v0.14.0) — a body state class collides with a component class.**
The confirmation line is `#notice`, and the body state that shows it is
`notice-on`, deliberately a different word. A bare `.notice { display: none }` rule
plus a `body.notice` state class collide: the class selector matches ANY element
carrying the class, and `<body class="notice">` is one of them, so `display: none`
landed on the body and **the entire panel rendered blank**. It was caught by a
headless screenshot that came out white while the DOM read perfectly — the only
wrong value anywhere was body's own computed `display`. Component rules here are
addressed by ID for that reason, and every body state class reads as a state
(`stale`, `quiet`, `alert`, `empty`, `notice-on`), never as a component name.

**The notice line overlays, it does not reflow.** It is `position: absolute` over
the banner strip rather than a flex sibling of it: a line that comes and goes in
1.4 s and pushed the zones down would move every card under a finger already
travelling toward one, and at the 344 px slot it would cost the grid a row for as
long as it showed. Overlaying also settles what happens when the feed is stale —
for its moment it covers the failure banner, and the failure is still there
underneath when it goes.

**Touch-target audit (measured 2026-08-26, both slots, all seven views).** Every
interactive element was measured off the live DOM, from a generic sweep rather
than a hand-list. The `--touch-min: max(48px, ...)` token was already holding
everywhere it was applied — **the day-nav arrows, the sheet close X and the sheet
buttons all passed at exactly 48x48 at 840x344** and 60.5 px at 2560x720, so the
suspects were not the offenders. **One control failed, and it failed everywhere it
appeared:** the week strip's day columns. v0.8.0 put `data-day` on all three cells
of a column and called them one target; measured, they were three separate 115.8 x
**19** px targets with a 3.6 px gap between them — a third of the floor, with dead
air in the joins.

The fix is one hit element per column (`appendWeekHits`), **absolutely positioned
inside the grid** so it takes its containing block from the grid area its
`grid-row`/`grid-column` name and takes no part in auto-placement — the geometry is
read off the same grid the numbers use, with no second copy of the column
arithmetic to drift. Two things about that had to be measured rather than reasoned:

- **Both ends of every span must be named.** For an absolutely positioned grid
  child an `auto` end line means the grid container's **padding edge**, not "one
  track" — `grid-column: 3` gave that column a target running to the right edge of
  the strip, overlapping every column after it.
- **`grid-row: 1 / -1` needs the rows to be EXPLICIT.** A negative line number
  resolves against the explicit grid only, and `.week-grid` declared columns but
  not rows, so `-1` resolved back to line 1 and every hit came out 19 px tall —
  exactly the bug it was added to fix. `grid-template-rows: repeat(3, auto)` is
  load-bearing, not tidiness.

Result: 115.8 x **70.2** px per column (three rows plus the two gaps they span, so
the target is contiguous as well as tall enough), and **zero failures** in the
sweep at either slot.

## v0.12.0 — the control-surface wave

Four additive features, all presence-detected, schema still 5. crabd may ship
every one of these fields without an iCUE re-import; the widget lights each up on
its next update and shows nothing at all when the field is absent.

**Provenance labels.** `limits.source === "statusline"` puts a muted `official`
tag beside the LIMITS heading — the numbers came from Claude Code's own
status-line document, not the OAuth reach-around. `oauth` and an absent source
both show nothing: a tag reading "oauth" would label the ordinary state and add
noise to every panel. And `burn.costUSD`, a finite number only when OTLP
telemetry is flowing, renders `$12.47 today` under the budget line near the TODAY
stats. `typeof`, not `Number()` — `Number(null)` is 0, and a **$0.00 is never
derived from an absent cost**; only a real zero the feed reported ever shows.
Both lines are `display:none` until their field is present, so a feed carrying
neither renders byte-for-byte its v0.11.0 self.

**Tap-to-continue.** A working or done session's DETAIL sheet gains a *Continue
this session* row: three hardcoded defaults — **Continue** (`Keep going with what
you were doing.`), **Run the tests** (`Run the tests and report the results.`),
**Commit + push** (`Commit the changes and push.`) — plus one button per string
in a top-level `continuePrompts` array (presence-gated; an older crabd echoes
none and just the three show). Each button's **label is short; its
`data-continue-prompt` is the FULL instruction** that goes on the wire. Tap →
`POST /v1/action {"sessionId","action":"queue-continue","prompt":"<full text>"}`;
optimistic `queued: <label>` inline (green). A 404/400/older-crabd answer renders
`not available` inline (muted) and **does not latch** — the next tap tries again,
because crabd redeploys under a live widget. The row has its **own** status line
(`#sheetContinueStatus`) because the shared `sheet-status` is `display:none` in
detail mode.

**FULL panel approval.** A needs_input session carrying
`pendingPermission {tool, summary, requestedAt}` renders the **approval variant**:
the card shows a red `permission request` label, the **tool** prominent, and the
summary clamped, in place of the question; its spine and dot go red and it is the
**loudest** card on the panel (a wider, faster pulse and a full-red panel glow via
`body.approval`, above every escalation tier). It **cannot be acked away** — the
crab tap skips it and the approval sheet offers no ack, so a permission gate keeps
asking until a decision is made. Its action sheet gains **Deny / Approve**: Deny
is first and styled as the safe, filled default (nearest the thumb); Approve is
the amber outline and **carries the tool name** (`Approve Bash`), because
approving a shell command from a touchscreen must show WHAT. Tap →
`POST /v1/action {"sessionId","action":"decide","decision":"allow"|"deny"}`, then
the sheet **closes optimistically** — a failure is logged, not surfaced, because
the terminal dialog remains the source of truth on timeout. Absent
`pendingPermission` → the ordinary needs_input sheet (ack / reply), unchanged.

**Fixture + stub changes (`rework`).** rework now carries `limits.source:
"statusline"`, `burn.costUSD: 12.47` (+`costSource: "otlp"`), a top-level
`continuePrompts` with one extra prompt, and its needs_input session (`…50001`)
gains a `pendingPermission` (tool `Bash`). So one `?mock=rework` shot proves the
provenance tag, the cost line, the extra continue button, and the approval card +
its panel-wide red glow at once. The mock action stub now **accepts**
`queue-continue` and `decide` (204); `reply` stays 501; ack/ack-all stay 204.

**Dev flags (mock only).** `&approval=1` auto-opens the approval sheet on the
first pendingPermission session. `&action400=1` forces the older-crabd **400** on
`queue-continue` and `decide`, so the no-latch inline handling is demoable without
editing a fixture (a fixture may also carry `_mock.action400: [...]`).

## v0.13.0 — depletion forecast

**A muted "~full by 3:40 PM" under a gauge when crabd projects the window will
deplete before it resets.** `limits.fiveHour` / `limits.weekly` (and each `extra`
window) gain an optional, nullable `exhaustAt` — a linear projection, hedged with
a mandatory `~`, never presented as certainty. Presence-gated like every additive
field: an absent/`null` `exhaustAt` leaves the gauge byte-for-byte its 0.12.0 self
and takes no vertical space (`.gauge-forecast` is `display:none` until it has a
value). `forecastLabel` returns `''` (render nothing) on four honesty rules —
absent/unparseable, a moment already **in the past**, at-or-**after** the window's
own `resetsAt` (the widget guards this even though crabd never extrapolates past a
reset), and it degrades from a clock time (`fmtTimeOfDay`, which carries AM/PM in a
12h locale) to a short date once it lands more than a day out. Recomputed per poll,
not on the 1 Hz tick — the text is a fixed clock time, not a countdown. The mock
loader pins `exhaustAt` the same way it pins `resetsAt`, so a near-future forecast
does not silently drift across the reset guard between polls.

**Verified** — `node --check`, strict-XML parse, JSON parse of `rework` + manifest
all pass. Headless Edge `--force-prefers-no-reduced-motion` on `?mock=rework`, DOM
read over CDP: three gauges, **page overflow 0×0 at both 2560×720 and 840×344**;
`#forecast5h` shown with text `~full by 21:49` (near-future, before reset),
`#forecastWk` hidden and empty (`exhaustAt: null`), and the `extra` opus-weekly
forecast hidden and empty (`exhaustAt` after its reset — the guard fires). One
gauge shows the line and two do not, in a single fixture.

**Verified (v0.12.0)** — `node --check`, strict-XML parse, JSON parse of every fixture +
manifest + translation all pass. Headless Edge at 2560×720
(`--force-prefers-no-reduced-motion`; **Chrome headless screenshots are blocked in
this environment**, Edge is the working headless): the approval sheet
(`Bash` shown, Deny left / `Approve Bash` right, red panel glow), the continue row
on a done sheet (three defaults + the config-fed extra), the provenance tag and
`$12.47 today` behind both, and the main grid's red approval card — all with **0
page and 0 zone overflow**. Live-browser (`?mock=rework`, real 2560 viewport): the
`queue-continue` POST carries the full prompt and shows `queued: Continue`; the
`decide` POST fires `allow` and `deny` and closes the sheet; and with
`&action400=1` **both** continue and decide 400s are handled inline with no latch
— a second tap fires a second POST. `?mock=attention` (no new fields): no tag, no
cost line, no approval, and the plain ack sheet unchanged. Small slot **840×344**:
both the continue and approval sheets stay within the viewport (the events list
absorbs the pressure via its own scroll), page overflow 0.

## v0.11.0 — hung vs thinking, derived titles, the crab wardrobe

**A working card that has gone quiet says so in WORDS.** `lastActivityAt` older
than `HUNG_MS` (90 s) puts a muted `quiet 3m` beside the turn chip; anything
fresher gets a two-frame tick on the state dot instead. The pair is the answer to
the one question this panel could not answer before — *is that session thinking
or is it hung* — and the reason it is worded rather than coloured is the same
reason everything else here is: half of this widget's defects have arrived as a
photograph of the glass, and a photograph of a dot tells you nothing.

**The tick is the 1 Hz tick, not a CSS animation.** `tickAges` toggles a `w2`
class from `Math.floor(now / 1000) % 2`, so every card is on the same frame, a
card rebuilt mid-second joins the phase, and a grid with nothing fresh in it
toggles nothing at all — zero idle cost on a panel that runs 24/7. The frame is a
`transform: scale()` on the dot and nothing else, so the state word beside it
never moves. Quiet hours and `prefers-reduced-motion` both hold the dot steady,
in JS (the class is not applied) **and** in CSS (the transform is cleared), the
second because the OS setting can change under a running panel.

**Proved by pixels, not by reading the code** (2026-08-26): two headless shots one
virtual second apart, `--force-prefers-no-reduced-motion` → the dot's 45x27 px
box changes in 47 pixels, bbox exactly the dot; the same pair **without** the flag
→ `getbbox()` is `None`, zero pixels changed. Note the trap that found: **headless
Chrome reports `prefers-reduced-motion: reduce` by default**, so every headless
shot of this widget is a reduced-motion shot unless that flag is passed. Half a
verification pass can silently be the wrong run.

**The hint hides the age, and the chip drops its verb — both measurements.** On a
hung card the right-hand age figure and the hint are the *same number* (both are
now minus `lastActivityAt`), so the age is hidden and the hint keeps the word.
That still did not fit: at the four column slot `.card-top` is 292 px, and dot +
`WORKING` + `working 8m` + `quiet 3m` is 284 px on this machine's mono and **over**
it on the font headless Chrome falls back to — where the chip lost its number to
an ellipsis and the card said nothing about the turn at all. So the chip drops the
word on a hung card only: `WORKING 8m · quiet 3m`, both figures, ~60 px spare.
The word is the part with a copy of itself two chips to its left.

**A derived title is muted italic, and there are two ways to derive one.** crabd's
new optional `titleSource: "cwd"` says *it* fell back to the folder name; a row
with no `title` at all (an older crabd) is the widget's own fallback, and it shows
the **repo** — which names the work — dropping to the literal `untitled session`
only when there is no repo either. Never a bare `session`. Both render through one
`titleParts()` and one `title-derived` class, on the card, in the sheet and in the
burn breakdown, so a session cannot be `acme-api` in the grid and
`(untitled session)` in the sheet a tap later. `titleSource` is read for the one
value that means fallback and ignored for every other, so a crabd that adds a
third source renders as a real title rather than as a mystery.

### The wardrobe

> **Superseded by v0.18.0** — the art below was rejected by the operator and has
> been redrawn on the doubled grid, and the hard hat is retired. The
> precedence/hysteresis/suppression rules in this section still hold; the
> coordinates, the colours and the four-accessory count do not. See the v0.18.0
> section at the top.

**Four accessories, one at a time, chosen by the fleet.** All of them are
grid-aligned rect groups inside the existing crab SVG, drawn after the eyes,
switched by `data-acc` alone — nothing is built or removed at runtime, exactly
like the moods:

| accessory | worn when | notes |
|---|---|---|
| sunglasses | **every** session is working and none waits | the badge's own glasses, verbatim: the badge crab is drawn on this same grid, so the lens bar, temples and checkered glare transfer with no coordinate changing |
| party hat | for 60 s after `recap.doneToday` **increments** | on the increment, never the value — a widget that boots at "4 done" has not just watched four sessions land, and a decrease is the local day rolling over |
| hard hat | 3+ sessions working at once | brim wider than the shell, or it reads as a lid |
| nightcap | quiet hours are active | droops down the **left**, because the sleep Z lives off the right shoulder and this is the accessory that pairs with it |

**Precedence party > hardhat > sunglasses > nightcap, with 10 s of hysteresis**,
and two deliberate bypasses. Nothing (`''`) applies **instantly in both
directions** — waiting ten seconds to take a hat off because a question arrived is
the exact failure the "alerts stay serious" rule exists to prevent. And **party
bypasses the timer too**, because it is not a condition: it is a 60 s latch opened
by an edge that has already happened, so it cannot flap, and holding a
one-minute celebration back for a sixth of its life proves something the latch
already guarantees. Everything the fleet can strobe — hardhat, sunglasses,
nightcap — goes through the timer.

**Suppressed outright** on any `needs_input` row (acked or not: the sentence is
"none on needs_input", and an acked question is still a question), on a stale or
standalone feed (a hat on a panel that cannot see anything is the panel lying),
and whenever the `crabStyle` property is off.

**Measured, live (2026-08-26, `?mock=rework` with the feed frozen):** 3 working →
candidate `hardhat`, nothing worn at +4 s; flap to 2 working at +4 s → candidate
becomes `sunglasses`, still nothing worn; back to 3 at +5 s → **the timer
restarts**, nothing at +9 s; worn at +16 s. One row to `needs_input` → `''` in the
same frame. `?mock=normal` (4 working, none waiting) wears the hard hat naturally
at ~12 s — the first render past 10 s, which lands on the 3 s poll grain.

**Three tricks, latched like the flash.** Claw click on ack/ack-all (the raised
claw snaps half a cell in and back, twice), bounce when the last working session
lands and nothing waits, and the juggle easter egg at 5+ working — three pixel
balls stepping around an arc for 6 s, at most once per 10 minutes, because five
sessions working is a state that can hold for an hour and a crab that juggles
every three seconds for that hour is a crab nobody looks at again. All three are
skipped under quiet hours and reduced motion, and each clears on a **timer**, not
on `animationend` — under reduced motion the animation is `none`, `animationend`
never fires, and an animationend-only reset latches the flag true forever.

**The claw snap rides a nested `.claw` group, not the arm.** The mood rules park
the arm with a static transform, and an animation on the same element replaces
that transform outright — the first cut dropped a waving arm to its resting row
for the length of the snap. Nested, the two compose.

**The juggle hides the accessory for its 6 s.** One thing on the head at a time,
and the balls' arc passes through exactly where a hat's tip is. It is done in CSS
(`.crab.juggling .acc`) rather than in the state machine, so the wardrobe never
sees the trick happen and no hysteresis timer is disturbed. That rule has the
same specificity as the four `data-acc` rules above it and wins by **order** —
keep it below them.

**The balls' arc is wider than the shell, and that came off a shot.** The first
cut put the low balls at x 4 and x 20, which is exactly the body's own top
corners: they fused with the silhouette and read as two bumps on the crab rather
than as anything in the air. Out at x 1 and x 23 they clear it by a cell.

**The viewBox grew upward, `0 0 26 20` → `0 -2 26 22`, and 22 is a measurement.**
At 2560x720 the crab box is 462x400 px, so the SVG is width-limited at any viewBox
height up to 22.5 and the painted crab is **byte-identical** either side of this
change (scale 17.77 before and after, re-read off the live DOM). 23 would cost 2%
of the crab and 24 would cost 6%. The 840x344 slot is where every unit is paid
for — the box there is 275x220 and the crab is height-limited, so 22 costs 5.6%
(scale 10.57 → 9.98) and 24 would have cost 13%. Re-measure **both** slots before
growing it again. Verified at both: no element on the page overflows, the crab's
content bbox with the tallest hat on is `y -2, height 19` inside a 22-unit box,
and badge / crab / clock / fleet dots all still fit.

**Costume colours are literal, like the badge's** — party `#E8A33D` with a
`#F7F3EC` topper, hard hat `#F2C230`, nightcap `#7C86A8` with a cream pompom,
lenses `#0B0907`. An accessory is a costume, not a reading, so the personalization
accent must not repaint it, and a hard hat that is not safety yellow has stopped
being a hard hat.

**`crabStyle` ships as a SWITCH.** `auto` (dress for the fleet) / `plain` (never
any accessory) is the semantic, but switch / slider / textfield / color are the
only property types this widget has ever had confirmed on the Edge, and guessing
an enum type the import validator does not know fails at the console, not here.
`crabPlain()` accepts the boolean **and** the words `plain`/`auto`, so the day an
enum control exists nothing in the code changes. Anything unrecognised is `auto`:
the default is the feature being on.

**Fixture changes (`rework`).** One working row's `lastActivityAt` is now 3m45s
before `generatedAt` (its `turnStartedAt` moved back with it, so the card is
self-consistent: working 8m, quiet 3m — the `&age=` discipline in a fifth place);
the `done` row carries `"title": "acme-api", "titleSource": "cwd"` for crabd's own
fallback; and the `idle` row now has **no title, no repo and no branch** — a
session in a folder that is not a git repo — so the widget's own fallback and the
`untitled session` floor are both in one shot, along with the repo line's
long-standing drop to `cwd`.

## v0.10.0 — the burn budget, and paging through history

**A daily target on hourly bars is a PACE line, not a ceiling.** `burn.budget`
gives one figure for the whole day, and the 24 h sparkline's buckets are hours —
so the marker is drawn at `dailyOutputTokens / 24`, and an hour above it is not
an overspend, it is an hour a quieter one has to pay for. Reading it as a limit
would have the panel condemn every normal working hour. On the **7 day** bars the
same block means the opposite: one bar is one day, so the marker is drawn at
`dailyOutputTokens` and is a genuine ceiling. The tooltip says which of the two
is on screen (`budget pace 73k per hour` / `daily budget 1.8M`); the marker
itself is one dashed muted rule, because it is a target, not a reading.

**The chart scales to the TARGET when the target is above every bar.** A marker
off the top of the plot says nothing, so `scaleMax = max(peak, target)`. The axis
still reports the data peak, which is what it has always reported — visible on
`?mock=recap&spark=7d`, where the bars top out at 1.2M under a 1.75M line. With
no `burn.budget` in the feed `scaleMax` **is** peak, and the whole chart is
byte-for-byte its v0.9.0 self (proved below, not asserted).

**The line beside the TODAY stats carries the state in WORDS.** `budget 34%`
muted, `budget 134% — over` amber from 100%, `budget 167% — far over` red from
150%. Both the colour and the wording move at both steps: half this panel's
defects have been reported from a photograph of the glass, so a state that exists
only as a hue is a state nobody reads. `typeof`, not `Number()`, on `todayPct` —
a percentage crabd could not produce renders nothing at all rather than a 0% that
reads as a quiet day, so a budget block carrying only `dailyOutputTokens` draws
the marker and no line.

**The setting is the THIRD `/v1/config` key**, and it is sent exactly like the
other two: its own POST, never a combined body, `keep400` semantics (a pre-0.10.0
crabd 400s it — that is "this crabd does not know this key", handled per key and
**not** latched; 404 still latches endpoint-wide). The slider is in **thousands**
of output tokens — 100 to 20000, step 100, default 5000 — because the control
renders its own value beside a unit label and "5000000 tokens" is not a figure
anyone reads on a settings panel. 100k is the contract's own floor; 20M is the
top of the plausible range rather than the contract's 100M ceiling, since a
slider that must travel a thousand steps to a usable value is a slider nobody
sets. The value is clamped before it is sent, so an out-of-range property is
corrected rather than 400ed — and a 400 there would be indistinguishable from
the "older crabd" 400 this version has to read.

Verified in the browser at 2560x720, `?mock=recap` and `?mock=rework`:

| run | result |
|---|---|
| `?mock=recap` | `budget 34%` faint (`rgb(110,103,95)`), marker at 54% of the plot (73k pace against a 135k peak), limits-zone overflow 0, page overflow 0 |
| `?mock=recap&budget=134` | `budget 134% — over`, amber; the marker drops to the 19k pace the smaller budget implies |
| `?mock=recap&budget=167` | `budget 167% — far over`, red |
| `?mock=recap&spark=7d` | marker at the top of a chart rescaled to the 1.75M target, axis still `peak 1.2M` |
| properties, on `recap` | off → `{"budget":null}` 204 · on at 1750 → `{"budget":{"dailyOutputTokens":1750000}}` 204 · 99999 → clamped to `20000000`, not 400ed · off again → `{"budget":null}`. One key per POST throughout |
| properties, on `rework` | `quietHours` 204 while **both** `toast` and `budget` 400 in the same session, each logged as this-key-only and neither latched |

**Absent budget is byte-identical, and that was measured rather than reasoned
about.** The pre-change tree was served beside the new one and the same fixtures
shot at 2560x720 on both: `hot`, `normal`, `quiet` and `hot&timeline=1` differ
only in the columns the CLOCK occupies — and a pre-versus-pre pair, shot twenty
seconds apart, differs in the same columns and slightly more of them. Nothing
else in the panel moved.

**Day navigation: chevrons, not a second arrow.** The drilled day gains prev/next
in the head (`‹` / `›`, 48 px, beside Back and Close). Back is already a `←`, and
two glyphs pointing the same way with different meanings is a head asking to be
misread. Next goes **inert at today** — tomorrow has no history, and an arrow
whose every press is a 404 teaches that the panel is broken. It stays in the row
rather than vanishing, because a control that disappears moves the two beside it
under a finger already travelling toward them.

**Prev is never disabled, deliberately.** History thins out backwards with no
boundary the widget can know — crabd keeps one rotated generation and nothing
says where it ends — so a day it has nothing for is handled the way every other
history miss is: inert tap, one console line, the view exactly as it was. There
is still no "history unsupported" flag anywhere in the file (v0.8.0's rule, and
the fetch path is unchanged). Guessing a floor here would grey out days that are
in fact readable.

**Trap (2026-08-26, v0.10.0) — the disabled look lost on specificity.** The first
cut styled the inert arrow with a bare `.sheet-daynav[disabled]`, which is two
classes against the view gate's four: the button went inert while still painting
as live, and the computed colour read `--muted` beside a live sibling. The rule
now repeats the whole `.sheet[data-mode="timeline"][data-tl-view="day"]` gate.
This is the same trap that left a v0.9.0 `max-width` rule dead for its entire
life — anything added to these selectors carries the gate.

Verified in the browser at 2560x720 on `?mock=rework`:

| run | result |
|---|---|
| `&day=2026-08-26` | next `disabled`, `--faint`, `cursor: default`, border at 0.6 alpha; prev live and muted. Its `.click()` changes nothing |
| prev, prev | 08-26 → 08-25 (`200 events (truncated)`) → 08-24 (`12 events`), the count line following each day |
| prev onto **2026-08-23** (no file, a real 404) | title, footer and rows all unchanged, one `history 2026-08-23 unavailable (HTTP 404)` line, and the very next arrow still works |
| next, next | back up to today, where next re-disables |
| week-strip tap | still enters at the tapped day (08-24), Back still returns to Today with its week strip and 15 rows, and the arrows are `display: none` in every other view |

The arrows are re-evaluated **ahead of** the day view's signature gate, so a panel
left open across midnight re-disables next without anything in the document
having moved. `shiftDay` builds its Date from the three parts, like `dayTitle`
and `weekdayLetter`: a bare date string is UTC by the spec, and day arithmetic
through UTC lands a day early west of Greenwich.

No new `mock/mock-history-*.json` was needed. The existing set already walks
26 → 25 → 24 → (23 missing) with the truncated day in the middle of it, which is
every branch the arrows have.

**Small-slot check at 840x344:** the head is 48 px with the two new buttons, which
is exactly what it was without them (Close already set that height), so the list
region is untouched — `+6 earlier` sits 10 px into the day list's overflow at that
slot on this build **and on the pre-change build alike**, i.e. it is v0.8.0's
`DAY_ROWS_MAX` measurement not holding at 840x344 and is not this wave's. Budget
line at that slot: no overflow, zone overflow 0, page overflow 0, and the
sparkline (with its marker) is already hidden by Rule 6.

## v0.9.0 — the standalone panel, and one feature removed

**A widget is installed before its companion, always.** That ordering is the whole of
this release: on the store the first thing this panel ever renders is the state where
nothing has ever answered on 127.0.0.1, and before v0.9.0 that state was built for the
three seconds it used to last — a `connecting to crabd` banner, the zones at `--dim:
0.62`, two empty gauge tracks, four `--` stats and `waiting for crabd` in the grid. As
a permanent state that is not a panel waiting, it is a panel broken.

So the `connecting` state is now a **product**, not a wait:

- **No banner.** A banner is the panel reporting a failure, and a widget whose companion
  is not installed yet has not failed at anything. The **stale** banner is untouched — a
  companion that answered and then stopped IS a failure, and it still says so in red.
- **No dim.** `body.connecting .zones { --dim: 0.62 }` is gone. Everything the panel can
  show without a feed — clock, date, Claw'deck badge, the crab (asleep), the iCUE sensor
  row — renders at full brightness.
- **The placeholder-only blocks are hidden outright, not greyed.** `.limits-head`,
  `.gauge`, `.gauge-extra`, `.limits-note`, `.today` and `.grid-head` all read the feed
  and have nothing to read, and a row of em-dashes is the panel claiming a figure it does
  not have. The **sensors row stays**: iCUE feeds it, not the companion, so it is real
  data on a panel that has none of the other kind — and it loses its top border, being
  now the only thing in its zone.
- **One calm line** where the cards would be: *Claude Code stats need the SideCrab
  companion — see the widget's description for setup.* No URL in it. The store listing
  carries the link, and a hardcoded one goes stale on glass nobody re-imports.
- The Sessions count is **blank** rather than an em-dash in this state, for the same
  reason the gauges are hidden.

Everything else is unchanged: the first good document flips `everHadData` and the whole
panel appears, exactly as before.

**The status strip under the card grid is REMOVED** (contract, v0.9.0 REMOVAL). Gone
with it: its element, its render function, its staleness constant, the iCUE property that
switched it and that property's four `tr()` keys, its CSS — including the `@media
(max-width: 1200px)` rule that had been dead since the day it was written, the base
selector never beating the `.shown` one — and its block in every fixture. Both sides of
the contract presence-gate the key, so an older companion still emitting it is a key the
widget ignores, and there is no deploy ordering to get wrong.

**Every shipped string is generic.** The fixtures are a plausible open-source project set
(`acme-api`, `acme-web`, `payments-svc`, `docs-site`, `orbit-desktop`, `sidecrab`) doing
plausible work; this file, the code comments and the manifest name no host, system,
project or person outside SideCrab itself. The package ships the whole of `widget/`, this
file and `mock/` included, so a fixture string is a shipped string like any other — check
new ones the same way.

## v0.8.0 — pinned sessions, the day drill

**Persistence is the VENDOR's mechanism, and it is worth naming exactly.** Corsair's
local-storage reference documents one mechanism and only one: every widget has a `QUuid` exposed as the
global **`uniqueId`**, and **one JSON object holding all of that widget's persisted
properties** is stored in `localStorage` under that id. So the pin map is a
**property inside that object** (`pinnedSessions`), never a `localStorage` key of
its own — iCUE serves every widget from the same `file://` GUID origin family, and
a widget that scatters bare keys is sharing a namespace it does not own. Saves are
**read-modify-write** of the whole object for the same reason: it may carry
properties this version knows nothing about, and rewriting it fresh would drop them.
The doc also says display state only, which a list of session ids the operator chose
to keep at the front of their own panel is.

**Both halves are feature-detected, because both can be absent.** `uniqueId` does
not exist in a dev browser at all, and referencing an undeclared identifier is a
`ReferenceError` rather than `undefined` — which is why it is read through
`getIcueProperty`, the same helper the iCUE properties use. `localStorage` itself
**throws** on access in some locked-down profiles, so every call is wrapped rather
than tested once. Either missing leaves `pinStoreKey` null and the map in memory for
the session, silently: a pin that does not survive a restart is a nuisance, and a
banner about it would be worse than the nuisance.

Verified in Chromium at 2560x720 — the real path and the fallback, separately:

| run | result |
|---|---|
| `?mock=rework&uid=devwidget1` | pin → `localStorage.devwidget1` = `{"pinnedSessions":{"…50004":<ts>}}`; **reload → still pinned, still first in its band**; unpin → `{"pinnedSessions":{}}` and the feed's order back |
| `?mock=rework` (no flag) | `uniqueId` undefined → `pinStoreKey` null → the pin applies and the glyph renders, **nothing is written** (the key count does not grow, the existing key is untouched), and a reload drops it |

**An unrecognised stored VALUE is round-tripped, not replaced (v0.16.0, audit F2).**
The read-modify-write above already preserved unknown **keys**. The gap was unknown
**values of a known key**: a NEWER widget writes `sessionFilter: "waiting_or_working"`,
this build has never heard of it, `prefIndex` clamps it to index 0 — and then the next
save wrote `"all"` over it. Any save at all: a pin tap is not a statement about the
filter. So `loadPrefs` now remembers the value it did not recognise
(`filterStoredUnknown` / `densityStoredUnknown`) and `savePrefs` puts it back verbatim,
while the panel still renders index 0 locally, which is the only thing it could draw.
Cycling the chip clears the memo in that same tap — from then on this build's value IS
the operator's latest word.
Verified in Chromium: seed `localStorage.f2test` with
`{"sessionFilter":"waiting_or_working","density":"ultracompact"}` → the panel renders
`All` / comfortable and holds both strings; a **pin** tap rewrites the object with both
unknown values **intact**; a **filter chip** tap then writes `"waiting"` while `density`
stays `"ultracompact"` — the override and the round-trip in one object.

**Pinned sorts first WITHIN its band, never across one.** `sortPinned` reads the
bands out of the order **crabd already delivered** (first appearance of each state)
rather than declaring `needs_input, working, done, idle` a second time here — the
contract says crabd pre-sorts, and a second copy of that list is a copy that can
disagree with the feed. With nothing pinned the function is the identity. It is
decorate-sort-undecorate with the original index as the last key, so the result does
not depend on `Array.prototype.sort` being stable. Measured on `rework`: pinning
`…50004` (the last of three working rows) gives `0001 needs_input, 0004*, 0002,
0003, 0005 done, 0006 idle` — the waiting row still outranks the pin.

It sorts **before the capacity slice**, which is the point: a pinned session is one
that survives the `+N more` cut.

**The glyph is a shape, not a colour** — a drawn pushpin (round head, straight
needle) in the card header. A pushpin emoji is a font the Edge may not have, and a
coloured dot would fail the same colour-is-never-the-only-cue rule the fleet dots
follow; half this panel's defects have been reported from a photograph of the glass.
`isPinned` is in the card **signature** for the ctx chip's reason: reordering usually
moves the signature by itself, but pinning the only card in its band changes no order
at all, and without it the glyph would not appear until something else rebuilt the card.

**Pin/Unpin is offered by every sheet that carries a session, action mode included.**
A question you are being asked is exactly the kind of session worth keeping at the
front, and hiding the control there would make the feature look like it only worked
on quiet cards. It is on its **own row** below the ack buttons rather than joining
them: a control that joined that row would move the buttons under a finger already
reaching for them. Purely local like Dismiss — nothing is sent to crabd, so there is
no pending state and no rollback. **It is routed in `onSheetClick` ABOVE the generic
`.sheet-btn` branch**, exactly as Dismiss is and for the identical reason: it wears
`.sheet-btn` for its looks and carries no `data-sheet-action`, so the generic branch
would POST an action of `null`. Any future button that borrows those looks goes above
that line too.

Pins are keyed by **`sessionId` alone** — deliberately not the way `ackOptimistic` and
`dismissed` are keyed. Those two answer one card and must die on the next transition;
a pin says "keep this session where I can see it", which is a statement about the
session and survives every state change it makes. A pinned session that leaves the
feed simply stops being drawn and its entry is kept, so the same session coming back
comes back pinned. The map is capped at 50, evicting oldest-pin-first so the cap can
never take the pin somebody just made.

**The day drill is a fifth VIEW, not a fifth `data-mode`.** A drilled day is the
timeline's own mode carrying `data-tl-view="day"`: same list region, same scroll,
same six-selector hide list, same small-slot media query. A separate mode would have
meant a second copy of that list, and two copies drift. The differences are the
footer (count line instead of the week strip), the **Back** control in the head, and
the title.

**The fetch is NOT latched, and the distinction is the whole design.** `/v1/config`'s
404 latch exists because a POST to an endpoint an older crabd does not have is a
*write* the widget must stop attempting. A **GET that fails is just a GET that
failed**: an older crabd 404s this and the tap is inert, but the very next tap tries
again, because crabd redeploys under a live widget and the endpoint may exist by
then. There is deliberately **no "history unsupported" flag anywhere in the file** —
adding one would strand the feature until someone re-imported the widget at the iCUE
console, which is the exact failure the v0.6.1 schema rework was written to stop
repeating. Verified: a tap on a 404 day leaves the sheet on Today with one console
line, and the **next** tap on a live day opens normally.

Everything else about the tap is attempt-and-handle: the sheet only swaps once a
document has landed, so a failure leaves the timeline exactly as it was — no error
banner, no half-opened view. One request in flight at a time, and a reply that lands
after the sheet has moved on is dropped rather than swapping the panel under a finger.

**Two caps stacked, each saying its own thing.** Measured off `companion/crabd.py`
(`_do_history`, 2026-08-26): crabd's `count` is the length of what it **returned**,
not the day's total, and `truncated` says more exist beyond it. So crabd's 200 becomes
**"(truncated)"** on the count line, and this view's own `DAY_ROWS_MAX` becomes the
**"+N earlier"** row at the foot of the list. `typeof`, not `Number()`, on the count —
the by-model rule.

**`DAY_ROWS_MAX = 18` is a MEASUREMENT.** At 2560x720 a row costs 24.75 px and the
sheet panel stops growing at 662 px (`max-height: 92%`), so the ladder runs: **18
rows + the tail = 470 px of list in a 636 px panel; 19 = 661 px, which fits to the
pixel; 20 scrolls** — and the "+N earlier" line then leaves the screen, which is the
one thing this list must never do, because it is the line that admits rows exist. 18
is the last cap with a whole row of margin, and a margin measured on one browser's
font metrics is what the next browser's metrics eat (the v0.7.0 lesson, in a third
place). Re-measure against **cap + 1** if anything is ever added to this footer.

**Timeline unchanged when `recap.week` is absent** — re-verified, not assumed:
`?mock=hot&timeline=1` still gives 20 rows + `+10 earlier`, list overflow 0, week
height 0, and all three new elements (`sheetBack`, `sheetDayFoot`, the pin row)
computed `display: none`.

**Day columns are one tap target per column, not three.** All three cells carry
`data-day`, because a fingertip on a wall panel does not aim at a row of digits.
The affordance is a faint plate plus `cursor: pointer`, gated on the same attribute
the handler matches — so a column with no usable day is inert **and** unmarked by
construction, rather than by a second rule that could disagree with the handler.
The day string is in the timeline's signature alongside the letter: a strip that
rolls over midnight can carry the same seven LETTERS as the day before, and the tap
targets would then keep pointing at last week.

**Rule 6 note:** at `max-height: 420px` the week strip is already hidden (v0.7.0), so
the day drill is unreachable at that slot. Left as is — the drill is a way into a
summary that the slot has decided it has no room for, and its own footer would be the
next thing to go anyway.

Small-slot check at **840x344**: the pin row renders at the 48 px touch floor, panel
overflow 0, page overflow 0, and the QUESTION is still the only region that gives up
space (it scrolls 110 px while the ack button stays put).

## v0.7.0 — reset countdowns, toast settings, the week strip

**Countdowns, on the 1 Hz tick.** The gauge foot is now `resets in 33 min` rather than
`resets 4:30`; the clock time moved to the gauge's **tooltip** (`title`), with the date
appended when the reset is not today — a weekly window resets four days out and a bare
"7:00 AM" reads as tomorrow. The instant is parked on the span as `data-resets-at` and
relabelled by `tickResets()`, the same idiom the card ages use: the poll is 3 s, and a
countdown that only moves on a poll crosses its minute boundary up to three seconds
late. Branches, all measured off `resetLabel()`:

| remaining | renders |
|---|---|
| absent / unparseable | `—` (unchanged: em-dash, never a zero) |
| **past or exactly now** | the absolute clock time, as before v0.7.0 — a `limits` block served from a last-good reading through an endpoint lockout really does carry a stale `resetsAt`, and it must never render as a negative or as "in 0 min" |
| under 60 s | `in <1 min` |
| 1–89 min | `in 33 min` |
| 90 min – 24 h | `in 2h 10m` |
| over 24 h | `in 4d 13h` |

**The mock rebase had to be pinned for any of this to be observable.** `rebaseMock`
recomputes its delta from the fixture's fixed `generatedAt` on **every** poll, so a
`resetsAt` shifted that way is re-pinned to "now + the fixture's offset" three times a
second and the countdown reads the same figure forever. `pinMockResets()` fixes the
rebased instants on the first document and re-serves them after that. This is the
v0.3.0 `&age=` trap in a second place: a rolling fixture value makes the panel
misreport the very behaviour the fixture exists to show. Verified in Chromium at
2560x720 — `in 33 min` → `in 32 min` observed on a wall clock, not reasoned about.

**Toast settings write the SECOND `/v1/config` key.** `toastEnabled` (switch, default
on) and `toastThreshold` (slider, 30–600 s, default 120) POST
`{"toast": {"thresholdSec": N, "enabled": bool}}`, debounced 2 s, from the same
`scheduleConfigSync()` as quiet hours. Two rules, both load-bearing:

- **One key per POST, never a combined body.** A pre-0.7.0 crabd 400s a toast write,
  and a body carrying both keys would take quiet hours down with it — one unsupported
  key silently disabling a supported one.
- **400 is per-KEY and is NOT latched.** The contract says an older crabd answers 400
  to a toast write, so 400 means "this crabd does not know this key" — but 400 is also
  what a bad body gets, and latching would strand the setting until someone re-imported
  the widget at the console. The brake is the payload marker instead: the same body is
  never re-sent, a **changed** value gets a fresh try, and a crabd redeploy (version
  string moves) clears the marker outright. **404 latching is untouched** — a 404 is the
  ENDPOINT's answer, not a key's, so it still latches endpoint-wide for both keys.

Verified in the browser against `?mock=rework` (whose `_mock.config400` refuses only
`toast`): quiet hours 204 and toast 400 in one session; an unrelated property nudge
after the 400 sends **nothing**; a changed threshold sends a fresh attempt; a forced 404
latches both keys and a version change clears it.

**The approval-toast threshold (v0.16.0) is the same key's OPTIONAL third member.**
`approvalThreshold` (slider, **5–300 s**, default **20**) adds
`approvalThresholdSec` to that same `toast` body. Three things about it are
load-bearing and none of them are obvious:

- **It is sent only once the control has been MOVED.** crabd preserves the on-disk
  value when the key is omitted — that preservation exists precisely because every
  panel save used to delete a hand-edited value with nothing said. A widget that sent
  its default on every save would defeat it on the first colour change. So the first
  observation of the property is a **baseline** (recorded, nothing sent); only a later
  value that differs from it is the operator speaking, and from then on the key rides
  every toast write. The latch is deliberate: setting it back to 20 is still a
  statement and has to be able to reach crabd.
- **The baseline is PERSISTED**, in the same vendor object as the pins and the two
  chips, as `approvalToast: {seen, touched}`. An in-memory flag would go silent again
  on every panel restart and the setting would never reach crabd twice. No `uniqueId`,
  no store, drifted shape → back to silent, which **preserves** what is on disk: the
  failure direction is the safe one.
- **A 400 drops the member and retries.** A crabd between 0.7.0 and 0.15.0 knows
  `toast` perfectly well and 400s the whole block for the one member it has never heard
  of — taking `thresholdSec` and `enabled` down with it. That pairing is the LIKELY
  one, not the exotic one: crabd redeploys, the widget updates only by a console
  import. So a 400 on a body carrying the member sets `cfgApprovalUnsupported`, clears
  the payload marker and re-syncs; the two required members then land. Cleared on a
  crabd version change, like every other capability marker here.

Clamp note: the value is clamped to the CONTRACT bounds (5..3600), wider than the
slider can travel. Deliberate — the clamp exists so a value arriving from anywhere else
is corrected instead of 400ed, and a 400 here is indistinguishable from the
older-crabd one. The slider stops at 300 because a permission request is something the
operator is already blocked on.

The fixture stub grew a **sub-member form** for this: `_mock.config400` entries may now
read `"toast.approvalThresholdSec"`, meaning 400 only when the body for that key carries
that member and 204 otherwise — which IS the 0.7.0–0.15.0 crabd. `recap` carries it.
Verified in the browser: `?mock=normal&uid=X` logs
`{"toast":{"thresholdSec":120,"enabled":true}}` with no third member; reloading as
`?mock=normal&uid=X&approvalsec=45` logs it **with** `"approvalThresholdSec":45` and
persists `{"seen":45,"touched":true}`; reloading again without the flag still sends the
member (the latch survives); `&approvalsec=1` clamps to 5 and `&approvalsec=99999` to
3600; and on `?mock=recap&uid=Y&approvalsec=45` the member body gets **400**, logs
`retrying without it`, and the immediate re-sync lands
`{"toast":{"thresholdSec":120,"enabled":true}}` at **204** — the toast key is not lost.

**Not served back.** crabd 0.16.0 exposes no read surface for this value: `/v1/config`
is POST-only and `/v1/state` carries no `toast` block (it carries `quiet`, but that is
the only config echo there is). So the widget cannot seed the slider from disk, and
there is no iCUE API to write a property value from JS either. That is exactly why the
touch record has to be persisted rather than derived — and why an untouched save must
stay silent instead of asserting the property's default over a value it cannot see.

**Week strip.** `recap.week` renders as the TIMELINE sheet's footer: weekday letters,
then a `done` row, then a muted `commits` row, each number row labelled in a left-hand
column — two unlabelled rows of digits under a row of letters is a puzzle, not a
summary. Today's column is marked once, on the letter row. A day whose figure is not a
number renders `—`, never `0` (`typeof`, not `Number()` — the by-model rule). Absent
`recap.week` and the footer is `:empty` and `display:none`, so the sheet is byte-for-byte
its v0.6.0 self, cap included.

**The cap shrinks when the strip is present, and that is a measurement.** At 2560x720
the timeline region is 421 px with the footer under it and a row costs 24.75 px, so the
20-row cap overran by 74 px — three rows scrolled out of sight with **no `+N earlier`
line to admit they existed**, which is the one thing this list must not do.
`TIMELINE_MAX_WEEK = 15` plus the tail is 16 lines / 396 px, leaving 25 px of margin.
Verified: `rework` → 15 rows + `+5 earlier`, overflow 0; `hot` (no week) → 20 rows +
`+10 earlier`, overflow 0, footer height 0. Re-measure against **cap + 1** lines if
anything is ever added to this footer.

The cap follows the FEED, not the media query, so at `max-height: 420px` — where Rule 6
hides the strip — the list still caps at 15 with the room for 20. Left alone
deliberately: the cap would otherwise have to be read back out of the stylesheet the way
`gridCapacity()` reads the column count, and the error is in the safe direction with the
`+N earlier` line still telling the truth about it.

## v0.6.0 — badge scale, ctx chip, fleet dots, idle Dismiss

**Badge, +25%.** `.brand-badge` went from `--layout-unit * 32` to `* 40`, which at the
2560x720 slot is **230.4 x 46.1 px -> 288 x 57.6 px**, the 5:1 viewBox ratio and both
`shape-rendering` modes untouched. **The crab does not move**: it is WIDTH-limited in the
identity zone (462.1 x 355.4 px painted, inside a wrapper that is still ~400 px tall after
the badge grew and the fleet row appeared), so its painted size is byte-identical before
and after — measured, not assumed. If anything else is ever added to that zone, re-measure
`min(svgWidth/26, svgHeight/20)`, not the wrapper: the wrapper shrinking is harmless right
up until it crosses 355 px, and then the crab starts shrinking silently.

**ctx chip.** `contextTokens` (presence-gated) renders as a muted `ctx 549k` badge beside the
model, formatted by the same `fmtNum` as every other token figure. It is absent — never a
zero or an em-dash — when the field is `null`, missing or not a number. It is the FIRST
badge dropped when a card is tight, in three places: on a **question card** (JS, because
those already carry four lines of question), at **max-width 1800px** (CSS: measured, the
3-column card is 266 px of content and model + ctx + FAST + "3 sub" is 277 px, so the
badges row would wrap and grow the card a line), and at **max-height 420px** (CSS, Rule 6).
The BAR, unlike the chip, survives all three — it is out of flow and costs no line, which
is why a question card can show a fill it cannot show a figure for.

**Fleet dots.** Two letters with a dot each under the clock: `g` glow, `t` toast, from the
`fleet` block. Green filled disc = running, amber hollow ring = stopped, grey dash =
absent/unknown — **colour and shape both**, so the row survives a monochrome photo of the
glass. crabd is deliberately not in the row: a dot that can only be green says nothing.
The whole row is `display:none` unless the document carries a `fleet` object, so an older
crabd shows nothing rather than two grey dots that read as two dead services. Any value
the contract does not define renders as `unknown`, never as running.

**Idle Dismiss.** `DISMISSABLE = {done, idle}` is the single list; `isDismissed`,
`pruneDismissed`, `openSheet` and `onSheetDismiss` all read it, and the CSS matches
`[data-detail-state="done"]` and `="idle"`. Same keying as v0.4.0 (`sessionId` +
`stateSince`), so any transition resurrects the card. Verified: 11 sessions -> 7 cards +
`+4 idle`; dismissing 3 idle rows -> 8 cards and no chip; moving a dismissed row's
`stateSince` brings it straight back and drops the map entry.

**Grid capacity is now READ off the grid, not hard-coded.** `GRID_CAPACITY = 8` was "4
columns x 2 rows at the 2560x720 slot", but `.cards` drops to 3 then 2 columns on the
narrower dashboard_lcd sizes while the constant stayed at 8 — so at **840x344** seven cards
went into a two-row grid, the extra rows became implicit tracks, and the bottom cards were
sliced by the zone edge with their badges painted outside the card. `gridCapacity()` now
counts the tracks in the computed `grid-template-columns` and multiplies by
`GRID_ROWS`, so the breakpoints live in the stylesheet only. A **debounced `resize`
listener** was added with it: capacity is a media query now, and without a re-render a slot
change left the panel mislaid for up to a full poll.

**Small-slot question clip (the v0.5.0 defect).** The clamp at `max-height: 420px` was
already 2 lines; the clip was not the clamp. `.card-question` is `flex: 0 1 auto`, so a
short grid cell squeezed the box BELOW two line boxes and `overflow: hidden` sliced the
second line through its glyphs — a clamp can only ellipsise a line it is allowed to finish.
The media query now pins it to `flex: 0 0 auto` with `height` at exactly two line boxes and
clears `max-height`, so the clamp is the only thing that ever ends the block. Verified at
840x344: `clientHeight` 23 px = two line boxes exactly, zero card overflow, zero page
overflow.

**Timeline gap 0.6 -> 0.4.** No fixture before `hot` reached `TIMELINE_MAX`, and at 0.6 the
twenty rows plus the `+N earlier` tail overran the region by 7 px and sliced the tail.
0.45 fitted to the exact pixel, which is not a margin on another browser's font metrics;
0.4 leaves ~29 px. Re-measure against **TIMELINE_MAX + 1** lines, never against a fixture.

## v0.5.0 — the Claw'deck rebrand, in exact hexes

The Claude Max lockup is gone; the identity zone now carries the **Claw'deck badge**,
drawn as inline SVG in `index.html` (`.brand-badge`, viewBox `0 0 130 26`). Rendered at
the 2560x720 slot it measures **230 x 46 px, exactly 5.00:1**, against the old lockup's
~200 x 40 — the same slot, one step taller, because a badge has a plate and a border to
pay for and a text line does not.

| part | hex | note |
|---|---|---|
| plate fill | `#1A150F` | warm near-black |
| plate border | `#E8511F` | 3 user units, radius 7.5 matching the plate |
| mini crab | `#E8511F` | same rect grid as Claw'd (2 units = 1 pixel cell), eyes removed |
| shades + temples | `#0B0907` | 2-cell lens bar over the eye row, 1-cell temples past both body edges |
| glare | `#FFFFFF` | 3 whole cells per lens, checker, top-left |
| wordmark | `#F7F3EC` | `--font-mono`, weight 700, 15 user units |
| main crab fill | `#E45C28` | was `#D97757`. **Deliberately one step softer than the badge's `#E8511F`** so the badge pops off it |
| main crab, worried | `#BE6D4E` | was `#BC816E`. **Computed, not picked**: same hue (16.6°) and lightness (0.526) as the fill, saturation x 0.60 |

**Badge colours are literal, not tokens.** A logo must not move when the personalization
accent does. Two rendering modes on one SVG: `crispEdges` on the pixel group,
`geometricPrecision` on the plate and the wordmark — a single mode makes either the
blocks mushy or the border radius jagged.

`resources/icon.svg` is unchanged: it is white-on-transparent per the iCUE spec and never
referenced the old hex. `resources/preview.png` was **recoloured, not redrawn** — the two
flat fills `D97757` -> `E45C28` and `CC785C` -> `2E7FF2` remapped pixel-for-pixel, so the
composition, the clock glyphs and their antialiasing are preserved byte-for-byte.

**Gauge ramp (matching the Claude app's own usage panel).** The limit gauges
are **blue `#2E7FF2` below 75%, amber from 75%, red from 95%** — was accent / 70 / 90. The
base colour is fixed by design and no longer follows the personalization accent: a gauge
reads an external system, so it has to mean the same thing on every panel. Everything else
still follows `--accent`. Colour is never the only cue — the percent figure and the reset
time sit beside every bar. The token is `--gauge-blue` and `rampColor()` is its only
consumer.

**Today timeline.** Tapping the **Sessions header** (the whole line, heading and recap
alike — the recap's waiting fragment is a label, not a competing target) opens the fourth
sheet mode: every `sessions[].events` merged, tagged with a shortened session title, newest
first, capped at 20 with a `+N earlier` tail. Sessions with no events contribute nothing;
an empty day says *No events recorded since crabd started* rather than rendering blank.
`shortTitle()` cuts at the first dash-style separator before clamping, because these titles
are sentences and a length-only cut lands mid-word on most of them.

**Burn sheet, by model.** `burn.byModel` (presence-gated) adds a compact split between the day
total and the commits line. Bars are proportional to the **largest model, not the day
total**: the contract caps the list at 4, so it need not sum to anything and scaling against
an uncovered total would make every bar a stub. A model whose `outputTokens` is not a
number is **dropped**, never drawn as a zero bar — `Number(null)` is 0 and that would be the
panel inventing a figure.

**Package:** `icuewidget validate` then `icuewidget package` in this folder (install the
Corsair `icuewidget` CLI first — it is what cuts the `.icuewidget`).

**Hardware sensors.** `manifest.json` declares
`"required_plugins": ["widgetbuilder.sensorsdataprovider:Sensors:1.0"]`, and
`IcueWidgetApiWrapper` + `SimpleSensorApiWrapper` are pasted **inline** in
`<head>` per the Corsair common-tools reference (iCUE serves widgets from a
`file://` GUID folder where QtWebEngine silently refuses a plugin wrapper loaded
by `script src`). Two `sensors-combobox` properties pick the sensors, defaulting
to `getDefaultSensorIdBlock('temperature', 'cpu-temp' | 'gpu-temp')`. **The row
is `display:none` until a real reading arrives** — in a dev browser
`window.plugins` does not exist at all, and none of the screenshots below show
the row. That path is therefore the only one verifiable off-glass; the readings,
the colour thresholds and the combobox population must be confirmed on the Edge.

**Trap (2026-08-26, v0.3.0):** the inlined wrapper classes contain a logical-AND,
and a raw `&` is an **illegal token** to the strict-XML parse. The block is
wrapped in `//<![CDATA[` / `//]]>` — line comments to JavaScript, markup to XML —
so the code stays byte-for-byte the documented one. Do not unwrap it.

**Trap (2026-08-26, v0.2.0):** the same strict-XML parse also rejects a **double
hyphen inside an HTML comment** — so a comment mentioning a CSS custom property by
name (`--dim`, `--touch-min`) fails at that byte. Spell the names out in prose in
`index.html` comments; CSS and JS comments are unaffected.

**Quiet hours are a WRITE (v0.4.0; capability-detected since v0.6.1).** The
`quietEnabled` / `quietStart` / `quietEnd` properties POST `/v1/config`
(`{"quietHours": {...}}` or `{"quietHours": null}`), debounced 2 s, once any good
document has landed. There is **no schema gate** on it any more, and the reason is
worth keeping: `/v1/config` is an ENDPOINT, and no field in the document honestly
describes it. The obvious candidate — the `quiet` KEY — has been served
null-or-object since crabd **0.2.0**, but the endpoint only landed in **0.4.0**, so
presence of `quiet` would have the widget POST at a 0.2.0/0.3.0 crabd and call the
404 a surprise. Measured against `companion/crabd.py`. So the widget **attempts the
POST and reads the reply**, which is the only honest capability test:

- **404** → this crabd has no endpoint. Latched in `quietCfgUnsupported` so it is
  asked exactly once, and cleared only when `crabd.version` changes (a redeploy may
  have added it) — never on a number comparison.
- **any other non-2xx** → a rejection of *this body*, not of the endpoint: no latch,
  so a corrected payload still gets a try.
- **dead socket** → transient by assumption (crabd restarts on every deploy), so
  **not** latched; latching a blip would strand quiet hours until a console re-import.

- **400** (v0.7.0) → on a whitelist key, "this crabd does not know this key". Handled
  per key, silently, and **not** latched — see the v0.7.0 section above.

All four are silent on glass, and none may touch `pollFailed` — an absent config
endpoint is not a dead feed. An invalid time is never sent. `quietHours` and `toast`
are the only keys the widget may ever write, and they go in **separate** POSTs;
`allowReply` must never become settable from here.

**Trap (2026-08-26, on-glass):** iCUE parses index.html as STRICT XML. `<!doctype html>` lowercase fails at byte 0 and iCUE reports "Missing title element" — it must be `<!DOCTYPE html>`. `icuewidget validate` 0.4.45 does NOT catch this; `python -c "import xml.etree.ElementTree as ET; ET.fromstring(open('index.html',encoding='utf-8').read())"` does. Run it before every package.
