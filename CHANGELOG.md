# Changelog

The widget and the companion version independently and are never guaranteed to be the same
version. The wire contract between them is `docs/STATE-CONTRACT.md`, which carries the per-version
detail of every additive field and is the source of truth; this file is the short view.

## Current

| Component | Version | Notes |
|---|---|---|
| widget (`widget/manifest.json`) | 0.30.0 | **the panel in a browser**: its own settings sheet (gear beside the filter chips, or `s`), generated at runtime from the same property declarations the iCUE console reads; settings and the pairing code kept in one `localStorage` object on the panel's own origin; keyboard equivalents for the four gestures (`a` ack-all, `p` pin, Delete/Backspace dismiss, `r` refresh); the layout baseline clamped so a resizable window stops inflating the panel (2560x720 unchanged); the sensors row keeps only the companion's half; "Desktop Toast Alerts" is now "Desktop Notifications". Plus 0.29.0: | **crabd moved to port 9999, and the panel can be served by crabd itself**: same-origin relative paths over http(s), the `crabd Port` property (default 9999) only when loaded from `file:` as iCUE does; every POST carries `X-SideCrab-Panel`. Plus 0.28.2: | card type +17% (title 24.5 px, meta 18.4 px at 2560x720), titles wrap to two lines; question pinned at three whole lines; at most two subagent rows; badges keep their chip size. Plus 0.28.1: | idle blink every 8–10 s (was 60–180 s). Plus 0.28.0: | **the finish dance**: shades on and a four-beat shimmy when a session lands `working -> done` after a real turn (20 s+), once per 30 s, never beside a waiting session. Plus 0.27.1: | **0.27.0 rendered blank inside iCUE** (property/function name collision, a parse-time SyntaxError); fixed by renaming the reader. Otherwise 0.27.0: | **Approval Pairing Code** property; `decide` carries the code + `requestId`; unpaired taps are refused locally with a notice; 403/409/429 answers named on the panel |
| crabd (`companion/crabd.py`) | 0.34.0 | **the gauges work on a Mac that keeps its login in the Keychain**: Claude Code stores its credential in the login Keychain (and, on the machine measured, writes no `~/.claude/.credentials.json` at all), so crabd reads the file first and that item second - and reads the long-lived `claude setup-token` value from its own Keychain item instead of a DPAPI blob. Neither secret ever enters an argument list: the store command goes in on `security -i`'s stdin, hex-encoded. The three "store a long-lived one" notes name the command for the platform they are shown on, and a Keychain crabd was refused gets its own note pointing at the prompt. Plus 0.33.0: | **the fleet dots work on a Mac**: `fleet` reads `launchctl print gui/<uid>/<label>` on its own thread, mapping launchd's first-level `state` onto the same four words; `glow` is served `absent` because there is no lighting component on macOS at all, and nothing is spawned for it. Plus 0.32.0: | **the host gauges work on a Mac**: `host` is now served on macOS from mach `host_statistics` / `host_statistics64` and `sysctlbyname`, with no shape change - `memUsedGB` is Activity Monitor's "Memory Used" (not `top`'s), the 32-bit tick counters are unwrapped so a month of uptime cannot blank the gauge, and the three failure tiers are unchanged. Plus 0.31.0: | **crabd moved to port 9999 and now serves the panel itself**: `GET /` is the panel, behind three gates - a `Host` allowlist (DNS rebinding), an Origin allowlist of this crabd's own origin (`null` and the non-web schemes preserved), and `X-SideCrab-Panel` on every POST. A held port is a loud stop naming the command that finds the holder. Plus 0.30.0: | **the gauges stop dying every morning**: an optional long-lived token (`claude setup-token`, stored DPAPI-protected by `Install-SideCrab.ps1 -LimitsToken`) is used whenever the CLI token has expired; `limits.tokenSource` says which answered. Plus 0.29.0: | **SEC-a + WID-a closed**: `decide` requires the pairing code (`~/.sidecrab/panel-token`, minted on first start) and the pending request's `requestId`; `approvals` block in `/v1/state`; `panelToken` diagnostics in `/v1/health` |
| notifier (`notifier/sidecrab_toast.py`) | 0.22.0 | **runs on macOS**: a second adapter posts through `/usr/bin/osascript`, chosen from `sys.platform` at one construction site; the notification text rides in `argv` past a constant AppleScript, never interpolated into it. No buttons and no replace-in-place on that route. Plus 0.21.0: | polls crabd on port 9999; the toast's Acknowledge button POSTs with `X-SideCrab-Panel`. Plus 0.20.0: | shared DayLedger with the digest; budget-crossed toast; companion-gone-quiet toast |
| setup (`setup/`) | no version | **macOS installer**: `install.sh` / `update.sh` / `uninstall.sh` over `sidecrab_setup.py`, with `--status`, `--doctor`, `--pairing-code` and `--limits-token`. Dated, not numbered. The PowerShell installer (`setup/*.ps1`) is retained unchanged for the Windows build |
| lighting (`lighting/sidecrab_glow.py`) | parked | ships disabled: the Corsair SDK crashes in every non-interactive console context tested. **Windows-only - there is no macOS component**, so `fleet.glow` reads `absent` there and nothing is spawned for it |
| schema (`/v1/state`) | 5 | marks the last breaking shape; additive fields are feature-detected by presence |

## Highlights by wave (newest first)

- **the macOS port (2026-09-04)** - SideCrab runs on macOS, and the panel is a page in a
  browser rather than only a widget inside iCUE. crabd moved to port 9999 and now **serves
  the panel itself** at `http://localhost:9999`, behind three new gates - a `Host` allowlist,
  an Origin allowlist of its own address, and `X-SideCrab-Panel` on every POST. Everything
  the panel used to get from iCUE it now has for itself: its own settings sheet, its own
  storage for settings and the pairing code, and keyboard equivalents for the four gestures.
  The host gauges, the fleet dots, the limits token and Claude Code's own credential all have
  macOS readers - mach `host_statistics`, `launchctl`, and the login Keychain - and the
  notifier posts through `osascript`. A shell-and-Python installer (`setup/install.sh`) merges
  the hooks, writes the config and loads two LaunchAgents, with a `--doctor` that walks the
  whole chain a session travels. **Windows is untouched**: the iCUE widget is the same files
  and still packageable, the PowerShell installer is unchanged, and CI still runs its job. The
  per-component lines below carry the detail, newest wave first; `docs/PORT-NOTES.md` carries
  the seams, the measurements and every decision.
- **0.34.0 crabd (2026-09-04)** - the limit gauges and the context bars work on a Mac whose
  Claude Code keeps its login in the Keychain. Measured on the machine this was written on:
  `~/.claude/.credentials.json` does not exist there at all, so a crabd that only knew about
  that file said "no Claude credentials - run /login" for ever on an account that was
  perfectly logged in, and `/login` was not even the fix. crabd now reads the file first and
  the login Keychain second, and stores its own long-lived token (`setup/install.sh
  --limits-token`) in a Keychain item rather than the Windows DPAPI file. Neither secret is
  ever put in an argument list - `ps` is world-readable on a Mac - so the store command
  travels on stdin, hex-encoded. If the Keychain refuses the read, the panel says so in its
  own words and points at the prompt to approve, rather than sending you to log in again.
  Windows is untouched.
- **0.33.0 crabd (2026-09-04)** - the two fleet dots work on a Mac. crabd asks launchd about
  the notifier agent instead of asking Windows' task scheduler and getting nothing, so the dot
  turns green when the agent is running, hollow when it is loaded and idle, and stays grey when
  crabd genuinely could not find out - the distinction the whole feature exists for. The glow
  dot reads "absent", which is the truth rather than a placeholder: there is no lighting
  component on macOS, so there is nothing to observe and crabd does not go looking. The panel
  needs no update - it already knows how to draw all four states, and the document's shape did
  not change.
- **0.32.0 crabd (2026-09-04)** - the CPU and memory gauges work on a Mac. Until now the panel
  showed nothing where they go, because the only readers crabd had were Win32 ones; it now reads
  the same two figures from mach and answers in exactly the same shape, so an existing widget or
  panel lights them up with no update. `memUsedGB` is the number Activity Monitor calls "Memory
  Used", deliberately - `top`'s total-minus-free is 99.3 GiB from the same page counts (`top`
  itself printed the rounded "98G") where Activity Monitor says 66.0 on the machine
  this was measured on, and the figure you can check against an app you already have open is the
  useful one. Two things that would have gone quietly wrong are handled: the mach tick counters
  are 32 bits and wrap after about a month of uptime, so they are unwrapped before anything sees
  them, and idle time is folded into kernel time the way the Win32 counters report it, without
  which the CPU gauge would read null forever on a healthy machine. Nothing about Windows
  changed, and neither did the document's shape.
- **0.30.0 widget (2026-09-04)** - the panel is a page you can actually live in. It has its
  own settings sheet now (the gear beside the filter chips, or press `s`), because a browser
  has no iCUE property console: every setting, the colours and the pairing code are there, and
  they are kept in this browser on the panel's own address. The four touch gestures gained
  keyboard equivalents - `a` acknowledges everything waiting, `p` pins the focused card,
  Delete dismisses it, `r` refreshes now. Resizing the window no longer inflates the whole
  panel or strands the card grid mid-drag, and a phone-sized window is a real layout rather
  than a squeezed one. The temperature row keeps only the figures the companion can measure,
  and the host view stops calling a Mac a PC. **On the Edge nothing moves**: the reference
  size renders identically (measured), and the macOS system fonts were added *behind* the
  ones the panel was measured in rather than in front of them, so the face an Edge
  resolves is unchanged by construction. An iCUE re-import changes only what is listed
  here.
- **0.22.0 notifier (2026-09-04)** - the notifier works on macOS. The same six alerts, the same
  thresholds and the same quiet hours; only the last step changed, to a `display notification`
  posted through `osascript`. Two things are different on a Mac and will stay different:
  notifications carry no buttons, so an alert is acknowledged on the panel, and they stack
  rather than replace, so a second outage notice sits under the first. They are also attributed
  to Script Editor, which is whose notification switch mutes them. Windows is untouched.
- **setup (2026-09-04)** - macOS installer: `setup/install.sh`, `update.sh` and
  `uninstall.sh` merge the hook fragment into `~/.claude/settings.json`, take (and give back)
  the `statusLine` slot, write `~/.sidecrab/config.json` and load `com.sidecrab.crabd` and
  `com.sidecrab.toast` as LaunchAgents. Every file is backed up before it is written and
  replaced atomically; `allowedHttpHookUrls` is never created, only extended when you already
  have one; an agent you disabled is refreshed but not started; a restart refuses to start
  over a port held by something else and names the PID. `--status` is read-only; `--doctor`
  proves the write path end to end, so it posts a real `smoke-test` hook cycle and leaves
  three rows in that day's history. `--pairing-code` prints the code, `--limits-token` reads
  the token from stdin so it never reaches `ps`.
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
