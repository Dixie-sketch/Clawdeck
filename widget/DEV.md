# Developer notes: the panel page

This is the page the companion serves at `http://127.0.0.1:2722/panel/` and the panel host shows
full-screen on the Corsair Xeneon Edge. One tree, `widget/`, no build step: `index.html`,
`styles/sidecrab.css`, `scripts/sidecrab.js`, the mock fixtures under `mock/` and the tests under
`tests/`. The public repository carries the current architecture here; the full development log
stays with the maintainers.

## How the page runs

- **One host adapter.** The panel host injects one object before any script runs:
  `window.__sidecrabHost = { kind: 'standalone', props: {...}, pairingCode: '...' }`. `hostBoot()`
  validates it; `hostProp`, `boolProp`, `strProp` and `numProp` are the only readers. There is no
  fallback to a window global: a page global that shares a setting's name is not a setting.
- **Capabilities come from the host, not the object.** `hostCan('saveSettings')` and
  `hostCan('focusSession')` read the `host-info` reply. Before the handshake lands they are false,
  so a control appears once the host has answered rather than promising ahead of it. A plain
  browser at `/panel/` renders everything and says plainly that native save and focus are
  unavailable.
- **Bridge v2.** Every page-to-host message carries a page-generated `requestId` (at most 64
  characters) and a 10 s deadline. A late reply is dropped; a second request of the same kind
  supersedes the first. Replies: `host-info` (version, pid, startedAt, settingsPath, hasToken,
  capabilities), `settings-result` (ok, error) and `focus-result` (ok, reason, window).
- **One transport.** `acceptDoc` is the one door for a state document. A strictly older snapshot is
  dropped; an equal `generatedAt` is kept, because the companion publishes changed documents within
  one second. A changed `crabd.startedAt` resets the baseline; a backwards clock is overridden once
  the panel has been stale for the whole horizon. The companion pushes over `GET /v1/events`; an
  open stream that has said nothing for 45 s is not delivering, so the poll resumes and the stream
  is closed and reconnected on a paced ladder. Pull-to-refresh always fetches or restarts the
  stream; the single-flight guard is never bypassed.
- **The hardware row has one source.** Everything on it arrives in `/v1/state.host` and is
  presence-detected member by member. An absent reading is an absent cell, never a zero; a present
  but old reading is dimmed by the feed's own `sampledAt`. The two ten-minute rings keep time as the
  horizon and thin by count.
- **Views.** The grid zone shows Sessions, Burn, Week or Detail, chosen by chips or a swipe on the
  header. `viewSwitcherUsable()` reads a chip's computed `display` rather than copying the 1660 px
  breakpoint into JS; `aria-hidden` and `inert` follow the visible view. A question arriving behind
  another view puts a pulsing count on the Sessions chip; the panel never switches on its own.
- **Receipts.** Every asynchronous completion carries the session it was started for and the
  generation of the surface it will be written to, so a receipt cannot land on another session's
  sheet.
- **Settings on the glass.** Two halves with separate Save buttons because they write to two
  places: this panel's own props (saved by the host) and the companion's quiet hours, toasts,
  digest and budget (`POST /v1/config`, only the keys the operator moved; `null` clears a key).
- **The crab.** Drawn to a canvas from the same art as the SVG, which stays as the fallback. The
  renderer observes `data-mood`, `data-acc` and the trick classes; it writes nothing. Quiet hours
  and reduced motion schedule nothing.
- **Honesty rule.** A dead or stale feed renders a worried, desaturated crab and a banner; unknown
  limits render dashes. Never zeros, never stale green.

## Dev flags (mock mode only)

Open `index.html?mock=<fixture>` from a static server (the fixtures are under `mock/`). Flags set
the same variables a tap sets and write nothing to storage: `&view=<sessions|burn|week|detail>`,
`&filter=`, `&density=`, `&pin=`, `&crab=<state>`, `&sheet=first`, `&uid=`.

## Tests

```powershell
node widget\tests\test_ordering.js      # ordering, clamps, preferences, the bridge handshake
node widget\tests\test_chime.js         # the chime gates
node widget\tests\test_standalone.js    # one section per behaviour, through two fixtures
```

`tests/fixture.js` builds the two surfaces: `nativePage()` is the panel host (served origin, a boot
object, a bridge stub that answers) and `previewPage()` is the same URL with no bridge. The
fixture owns the clock (`advance(ms)`), so a 45 s liveness deadline or a 10 s bridge deadline is
reached in microseconds. Several sections end with a mutation proof, the pre-fix behaviour run
against the same input, because a test that cannot fail reports success for ever.

`index.html` must parse as HTML (the suite checks it) and the initial `<title>` is `SideCrab`
before any script runs. The version lives in `widget/version.json`.

## Measuring on the glass

The panel host's `--devtools-port` opens Chromium's remote debugging on loopback for measuring the
real page (viewport, console, screenshots) from a script. Headless Edge shots need a fresh
`--user-data-dir` per run, or a stale profile serves the previous stylesheet.
