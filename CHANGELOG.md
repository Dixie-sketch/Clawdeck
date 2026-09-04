# Changelog

The widget and the companion version independently and are never guaranteed to be the same
version. The wire contract between them is `docs/STATE-CONTRACT.md`, which carries the per-version
detail of every additive field and is the source of truth; this file is the short view.

## Current

| Component | Version | Notes |
|---|---|---|
| widget (`widget/manifest.json`) | 0.29.0 | **crabd moved to port 9999, and the panel can be served by crabd itself**: same-origin relative paths over http(s), the `crabd Port` property (default 9999) only when loaded from `file:` as iCUE does; every POST carries `X-SideCrab-Panel`. Plus 0.28.2: | card type +17% (title 24.5 px, meta 18.4 px at 2560x720), titles wrap to two lines; question pinned at three whole lines; at most two subagent rows; badges keep their chip size. Plus 0.28.1: | idle blink every 8–10 s (was 60–180 s). Plus 0.28.0: | **the finish dance**: shades on and a four-beat shimmy when a session lands `working -> done` after a real turn (20 s+), once per 30 s, never beside a waiting session. Plus 0.27.1: | **0.27.0 rendered blank inside iCUE** (property/function name collision, a parse-time SyntaxError); fixed by renaming the reader. Otherwise 0.27.0: | **Approval Pairing Code** property; `decide` carries the code + `requestId`; unpaired taps are refused locally with a notice; 403/409/429 answers named on the panel |
| crabd (`companion/crabd.py`) | 0.31.0 | **crabd moved to port 9999 and now serves the panel itself**: `GET /` is the panel, behind three gates - a `Host` allowlist (DNS rebinding), an Origin allowlist of this crabd's own origin (`null` and the non-web schemes preserved), and `X-SideCrab-Panel` on every POST. A held port is a loud stop naming the command that finds the holder. Plus 0.30.0: | **the gauges stop dying every morning**: an optional long-lived token (`claude setup-token`, stored DPAPI-protected by `Install-SideCrab.ps1 -LimitsToken`) is used whenever the CLI token has expired; `limits.tokenSource` says which answered. Plus 0.29.0: | **SEC-a + WID-a closed**: `decide` requires the pairing code (`~/.sidecrab/panel-token`, minted on first start) and the pending request's `requestId`; `approvals` block in `/v1/state`; `panelToken` diagnostics in `/v1/health` |
| notifier (`notifier/sidecrab_toast.py`) | 0.21.0 | polls crabd on port 9999; the toast's Acknowledge button POSTs with `X-SideCrab-Panel`. Plus 0.20.0: | shared DayLedger with the digest; budget-crossed toast; companion-gone-quiet toast |
| lighting (`lighting/sidecrab_glow.py`) | parked | ships disabled: the Corsair SDK crashes in every non-interactive console context tested |
| schema (`/v1/state`) | 5 | marks the last breaking shape; additive fields are feature-detected by presence |

## Highlights by wave (newest first)

- **0.31.0 crabd (2026-09-04)** - crabd listens on 9999 and serves the panel itself, so the
  panel is a page you open in a browser rather than only a widget inside iCUE. Three gates come
  with that: a `Host` allowlist, so a site whose name re-resolves to 127.0.0.1 cannot pretend to
  be your panel; an Origin allowlist of this crabd's own address, so a page you merely visit
  still cannot read the feed; and `X-SideCrab-Panel` on every POST, which is what stops a
  forged-origin page writing to a session. A port already in use is a loud stop that names the
  command for finding what is holding it, never a quiet move to another port. **An iCUE widget
  older than 0.29.0 can still read but can no longer tap** - update both sides.
- **0.29.0 widget (2026-09-04)** - the panel follows crabd to port 9999, and can now be
  opened in a browser: when crabd serves it, every request is same-origin and relative, so
  nothing has to be configured. The `crabd Port` property stays for the iCUE case, where the
  panel is loaded from disk and has to name crabd outright; its default is 9999. Every POST
  carries the `X-SideCrab-Panel` header crabd 0.31.0 requires. **Re-import the widget at the
  iCUE console** - an installed 0.28.x keeps polling 2722 and shows the standalone state.
- **0.21.0 notifier (2026-09-04)** - polls crabd on 9999, and the Acknowledge button on a
  toast sends the panel header, so acks keep landing. Nothing else changed; if the notifier
  and crabd are updated together there is nothing to do.

- **0.30.0 crabd (2026-09-04)** - "token expired" every morning, fixed. The CLI's token lives
  ~6 h and only a terminal `claude` refreshes the file, so `claude setup-token` +
  `Install-SideCrab.ps1 -LimitsToken` stores a year-long token, DPAPI-encrypted, used only
  when the short-lived one is stale. The unavailable notes now say what actually fixes it.

- **0.28.2 widget (2026-09-02)** - the session cards are easier to read: type is about 17%
  larger and titles wrap to two lines instead of being cut. To pay for it, a question card pins
  its question at three whole lines and hides its subagent rows, an approval card keeps a
  one-line title, and a card shows at most two subagent rows. Compact density is unchanged.
- **0.28.1 widget (2026-09-02)** - the crab blinks every 8 to 10 seconds instead of every one to
  three minutes. Same gates: calm moods only, never under quiet hours or reduced motion.
- **0.28.0 widget (2026-09-02)** - the finish dance. When an agent finishes a real turn the crab
  puts its sunglasses on and does a little dance. Bounded: 20 s minimum turn, 30 s cooldown,
  never while a session is waiting on you, never under quiet hours or reduced motion.
- **0.27.1 widget (2026-09-02)** - fixes 0.27.0, which imported but rendered a blank panel: iCUE
  injects properties as `let` globals and the new `panelToken` property collided with a
  same-named function. Import this one instead.
- **0.29.0 crabd / 0.27.0 widget (2026-09-01)** - panel approvals are safe to turn on: the
  pairing code and per-request id close SEC-a and WID-a. `Install-SideCrab.ps1 -PairingCode`
  prints the code; it goes into the widget's iCUE settings. Older widgets cannot approve
  against this crabd (refused, terminal dialog decides) - update both sides.

- **0.28.x crabd (2026-09-01)** - two live incidents fixed: a session killed by an app restart
  stayed `working` and swallowed queued taps (GHOST-a, half closed, taps now refused with 409);
  finished sessions re-activated by the CLI's post-Stop bookkeeping (GHOST-b, closed).
- **0.26.0 widget / 0.28.0 crabd (2026-08-28)** - served context-window denominator, real layouts
  for the sub-3:2 slots, six design-audit findings closed.
- **0.26.0 crabd (2026-08-28)** - backend audit: Origin gate extended to reads (SEC-4), config
  atomic write, GitLookup bounded, needs_input row cap, two permission stand-down P1s fixed.
  SEC-a recorded as the one open security residual (see `SECURITY.md`).
- **0.20.0 crabd (2026-08-27)** - never-500, restart race fixed; widget drill-downs.
- **0.15.0 (2026-08-27)** - the control-surface wave: panel approvals (off by default),
  tap-to-continue, settings from the glass, session filter and density chips, verified live with
  the operator present.
- **0.9.0 (2026-08-26)** - internal-dashboard integration removed; repo genericised for publication; manifest
  id became `com.sidecrab.widget`.
- **0.6.1 (2026-08-26)** - versioning rework: `schema` pinned at 5, additive fields by presence.
- **0.1.0 (2026-08-26)** - first widget package.
