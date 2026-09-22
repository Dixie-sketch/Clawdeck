# Changelog

The widget and the companion version independently and are never guaranteed to be the same
version. The wire contract between them is `docs/STATE-CONTRACT.md`, which carries the per-version
detail of every additive field and is the source of truth; this file is the short view.

## Current

| Component | Version | Notes |
|---|---|---|
| widget (`widget/version.json`) | 0.33.0 | **what a session is doing**: the six contract v0.35.0 session members rendered (a working card's event line becomes its live tool call with the call count this turn; PLAN, AUTO-EDIT and BYPASS chips beside the model; a thin todo hairline with a 3/7 chip and the current item on the Detail page; COMPACTING as the state word in the working colour; files touched and Claude Code's own typed-ahead queue on the Detail page, worded apart from the panel's own queued prompt), each only when present, so a session without them renders the 0.32.0 card; QA: a second permission request no longer counts down from the first one's clock, a stream that only pings no longer gates the fallback poll off, a settings save in flight no longer lands on a reopened sheet or leaves its Save inert, the hardware row sheds the sensor name then its cells on a narrow panel instead of running past its zone, the settings chip meets the 48 px floor, and the test fixture's className and classList share one store. Plus 0.32.0: | **standalone only**: the vendor host is retired (metadata, wrappers, the sensor bridge, the property probes, `manifest.json`, `translation.json`); one host adapter with capabilities from the `host-info` handshake; one transport that owns ordering (a strictly older snapshot never wins), liveness (a silent open stream is not delivering after 45 s) and refresh; receipts bound to the surface that started them; `aria-hidden` and `inert` follow the visible view; toggles named; a ten-minute history that holds ten minutes; an honest absent-feed state in every view; config on the glass (quiet hours, toasts, digest, budget, only the keys you change); Cancel on a queued line; a Sources section; approval readiness with repair text; a 10 s deadline on every bridge request. Plus 0.31.0: | **the desk wave**: a **Bring to front** control in the session sheet and on the Detail page asks the host to put that session's window in front on the main display, and says which of four things happened; a continue vocabulary per project: the sheet and the Detail page draw the builtins, the global list, then that session's own project prompts (`sessions[].continuePrompts`), and the continue row gives way before the panel does. Standalone only. Plus 0.30.1: | **the pink is gone** (0.30.1): the accent is a quiet blue (`#6F94CC`) and every chart bar and line takes the gauge blue outright; the approval glow moves from coral to orange. Plus 0.30.0: | **the standalone panel grows up**: temperatures from the companion's `host.sensors` and `host.gpu` (bridge-owned cells untouched), the host sheet with load, GPU, fans and two more charts; push transport over `/v1/events` with the poll as fallback; a settings sheet behind a gear chip; a synthesized chime on a new question; four views in the grid zone (Sessions, Burn, Week, Detail) behind chips and a header swipe, with a pulsing count on the Sessions chip when a question lands behind another view; the crab drawn to a canvas with real motion (the SVG stays as the fallback). Plus 0.29.0: | **two hosts, one codebase**: a page served by crabd at `/panel/` runs standalone, reading its settings from the host's injected `window.__sidecrabHost`, fetching same-origin, with the property-to-config sync off; inside the previous host nothing changes. Plus 0.28.2: | card type +17% (title 24.5 px, meta 18.4 px at 2560x720), titles wrap to two lines; question pinned at three whole lines; at most two subagent rows; badges keep their chip size. Plus 0.28.1: | idle blink every 8–10 s (was 60–180 s). Plus 0.28.0: | **the finish dance**: shades on and a four-beat shimmy when a session lands `working -> done` after a real turn (20 s+), once per 30 s, never beside a waiting session. Plus 0.27.1: | **0.27.0 rendered blank inside the previous host** (property/function name collision, a parse-time SyntaxError); fixed by renaming the reader. Otherwise 0.27.0: | **Approval Pairing Code** property; `decide` carries the code + `requestId`; unpaired taps are refused locally with a notice; 403/409/429 answers named on the panel |
| crabd (`companion/crabd.py`) | 0.35.0 | **six transcript-derived session members** (`activity`, `mode`, `filesTouched`, `promptQueue`, `compaction`, `todos`, all presence-gated) and the `PreCompact` hook at `POST /v1/hook/precompact`; `~/.sidecrab/logs/crabd.log` (about 1 MB, three generations); QA: `sources.limitsToken` absent while the status line serves the gauges, a lockout named as a lockout, a config file that does not parse is never replaced by the defaults, an empty transcript is not re-read every pass, an unparseable record is counted and logged, the lane F test module isolated. Plus 0.34.0: | **standalone only**: one main transcript owns a session's identity (the P1: a moved session no longer inherits the old project's allowlist); an unreadable pairing file is quarantined instead of stopping the companion; HWiNFO age is derived where it is served; PDH new-data readings are accepted; empty project lists keep their precedence; the once-only diagnostics keep a namespace of their own; a lost chunk boundary closes the connection; `sources` (one freshness verdict per feed), `approvals.readiness` with `POST /v1/approvals/verify`, `cancel-continue` (204, 409 with the delivery time, 404; the race settled under one lock), `POST /v1/config` takes the three continue keys and answers 200 with `{applied, warnings}`; `fleet.glow` removed; the origin gate allows only no-Origin clients and the server's own origin. Plus 0.33.0: | **a continue vocabulary per project**: `continuePromptsByRepo` (keyed on `sessions[].repo`, case-insensitive) and `continuePromptsByPath` (an absolute cwd prefix, longest key wins) in `config.json`, file-only; `sessions[].continuePrompts` served additively, absent when a session has none; `queue-continue` checked against builtins + globals + that session's list, same 400 as an unknown prompt; 20 per list, 20 per session, 200 chars, junk dropped with one log line. Plus 0.32.0: | **sensors, GPU, load and push**: `host.sensors` (HWiNFO shared memory, curated, capped at 24) with `host.sensorsSource` freshness, `host.gpu` (nvidia-smi), `host.load` (disk, net, commit, top process); `GET /v1/events` (SSE, same gates, eight subscribers). Plus 0.31.0: | **the panel route and two gates**: `GET /panel/` serves the widget tree from a fixed allowlist with `frame-ancestors 'none'`; a Host allowlist (`421`) closes DNS rebinding; the origin gate allows exactly crabd's own origin. `decide` still needs the pairing code. Plus 0.30.0: | **the gauges stop dying every morning**: an optional long-lived token (`claude setup-token`, stored DPAPI-protected by `Install-SideCrab.ps1 -LimitsToken`) is used whenever the CLI token has expired; `limits.tokenSource` says which answered. Plus 0.29.0: | **SEC-a + WID-a closed**: `decide` requires the pairing code (`~/.sidecrab/panel-token`, minted on first start) and the pending request's `requestId`; `approvals` block in `/v1/state`; `panelToken` diagnostics in `/v1/health` |
| panel host (`panel-host/`) | 0.5.0 | **a hunt through the host** (LO-001..LO-013): two displays matching the configured id are refused rather than settled by enumeration order; the settings watcher is re-armed when its directory vanishes; a display larger than the primary is reported and the zoom corrected against the window actually given; `--profile --windowed` and the reserved names `kiosk` and `windowed` are refused; an uncreatable `--sidecrab-dir` is refused at startup; a mistyped argument never writes to a live log; a hidden host writes no `viewport:` line (the smoke test could certify a panel nobody could see); a wrongly typed `deviceId` keeps the default and says so; the tray ticks the display the panel is on; the status window repaints on pause and pick; a superseded reply stays superseded after a legacy request; the foreground can be asked for at most four times a second. New: `--check` (`--doctor`) prints what the host would do and exits 0 or 2; a display seam and a window harness run the hardware cases on every build. Plus 0.4.0: | **recovery, a tray and a picker**: a hidden start keeps its re-pin poll, settings watcher and WebView; size fallback never picks the primary and refuses a tie; bridge v2 (one typed reply per request, `requestId`, `settings-result`); the pairing code injected top-frame only, frames constrained, permissions denied, downloads cancelled; only a terminal answers to a session title; `--profile` for a second host with its own WebView2 folder, log and mutex; `about:blank` is not loaded; per-field settings validation; escaped log fields; one log per instance; a tray icon (status, logs, reload, re-pin, pause, quit until logon) and a display picker with a ten-second revert. Plus 0.3.0: | **focus-session over the bridge**: the host enumerates and ranks windows itself (a title match, then repo and folder on terminals; the Claude app only as a labelled fallback; a tie refused), restores and fronts the winner with `SwitchToThisWindow`, verifies by re-reading the foreground, never takes focus itself; every attempt logged; the mutex name depends on the mode so a `--windowed` dev host runs beside the kiosk. Plus 0.2.1: | the fallback page follows the new accent. Plus 0.2.0: | **settings from the page**: a source-checked, whitelisted web-message bridge writes `panel-settings.json` atomically; audio allowed without a gesture; a 2 px viewport tolerance. Plus 0.1.0: | **new**: a .NET 10 WebView2 window pinned full-screen to the Xeneon Edge (by device id, never by index), topmost, tool window, never activates; re-pins on display, power and session events; its own fallback page while crabd is down. Replaces the previous host's widget as the host on the previous host's later builds |
| notifier (`notifier/sidecrab_toast.py`) | 0.20.0 | shared DayLedger with the digest; budget-crossed toast; companion-gone-quiet toast |
| lighting | retired (2026-09-21) | the Corsair SDK integration and its task are gone from the product; `Update-SideCrab.ps1` retires an existing `SideCrab-glow` task once and records it. Was: | ships disabled: the Corsair SDK crashes in every non-interactive console context tested |
| schema (`/v1/state`) | 5 | marks the last breaking shape; additive fields are feature-detected by presence |

## Highlights by wave (newest first)

- **0.35.0 crabd / 0.33.0 widget / panel host 0.5.0 (2026-09-22, the QA and feature wave)** - a bug hunt
  across every component with a reproduction behind each fix, and the panel learns what a session is doing:
  the tool it is running and what for, the permission mode, files touched, Claude Code's own prompt queue,
  context compaction as it happens (a new `PreCompact` hook) and the session's task list. The companion keeps a
  log at last. The page renders all six and closes six of its own (a permission countdown anchored to the replaced request, a ping-only stream that starved the fallback poll, a settings save landing on a reopened sheet, a hardware row running past its zone on a narrow panel). The host gains `--check`, refuses two matching displays, re-arms a dead settings watcher, and no
  longer writes a viewport line while hidden. The public repository leads with what it is, a fresh set of pictures and the three install commands;
  CI runs every widget suite; the notifier keeps its mute and snooze marks across a transient file error.

- **0.34.0 crabd / 0.32.0 widget / panel host 0.4.0 (2026-09-21, standalone only)** - the vendor host is
  retired and the external audit's 33 findings are closed. The companion serves one identity per session (the P1 the audit found), survives a broken pairing file,
  ages its sensors where they are served, reports per-source health and approval readiness, cancels a queued
  continuation, takes the continue vocabulary over HTTP, and refuses `null` and non-web origins now that nothing
  legitimate sends them. The page keeps one host
  adapter, one transport and an honest absent-feed state; settings, cancel, sources and approval readiness
  are on the glass. The host recovers from a hidden start, never parks on the primary, answers every
  request once, and gains a tray icon and a display picker. The install is the three components by
  default, the updater's verdict is honest, the smoke test binds its evidence to the running host, and
  the docs describe one product; what came before is dated history under `docs/history/`.

- **0.31.0 widget / panel host 0.3.0 / crabd 0.33.0 (2026-09-21, the desk wave)** - the panel
  reaches the desk: **Bring to front** puts the waiting session's window in front of you on the main display
  (the Claude desktop app keeps every session in one window, so the panel says "brought the Claude app to the
  front" rather than claiming more), never takes the keyboard itself, and refuses a tie rather than guessing;
  the continue vocabulary is per project (`continuePromptsByRepo` and `continuePromptsByPath` in
  `config.json`, served per session as `sessions[].continuePrompts`), and `queue-continue` checks a tap against
  that session's own list, so a prompt configured for one repo is refused for another with the same 400. Answering a session's multiple-choice question from the glass stays out by design:
  Claude Code 2.1.278 has no supported path, and every unsupported one is typing into a session on your behalf.

- **0.30.1 widget / panel host 0.2.1 (2026-09-21)** - the operator's word on the dusty-rose accent: gone. Chips,
  the working stripe and sheet borders take a quiet blue on the gauge's side of the wheel; the burn sparkline,
  the Burn view bars and the host sheet lines take the gauge blue itself, because a bar is a reading and a
  user-tinted accent must never recolour one. The approval glow loses its coral cast.

- **0.32.0 crabd / 0.30.0 widget / panel host 0.2.0 (2026-09-21, the standalone wave)** - now that
  the previous host is out of the loop the panel gets what the widget sandbox never allowed. Temperatures are
  back from HWiNFO's shared memory (curated, capped, with their own freshness so the free build's
  12-hour freeze dims rather than lies) plus the NVIDIA card from `nvidia-smi` and a machine-load
  line (disk, network, commit, the busiest process); crabd pushes state over `GET /v1/events` so
  a waiting session shows in about 130 ms instead of up to 3 s, with the poll as the fallback; a
  settings sheet behind a gear chip writes `panel-settings.json` through the host with no reload;
  a short two-note chime plays once when a session starts waiting, silent in quiet hours;
  the grid zone gains four views (Sessions, Burn, Week and Detail) chosen by chips or a
  swipe on its header, and a question that arrives behind another view puts a pulsing count on
  the Sessions chip rather than switching the glass under a finger; and the crab moves, painted
  to a canvas from the same art (breathing, arm sweeps, falling sweat, a dance that slides
  between its beats) at +0.02 points of main thread when idle. `Register-HwinfoRelaunch.ps1` registers the `SideCrab-hwinfo` task.

- **0.31.0 crabd / 0.29.0 widget / panel host 0.1.0 (2026-09-21)** - the previous host added a widget
  URL-permission layer that refuses every widget request to `127.0.0.1` and cannot keep a
  loopback grant across a restart, so the panel now has a second host: crabd serves the same
  widget tree at `/panel/` and `SideCrab.Panel` (a small WebView2 window, scheduled task
  `SideCrab-panel`, `Install-SideCrab.ps1 -Panel`) shows it on the Edge with no previous host in the
  loop. Two gates came with the route (Host allowlist, same-origin allowlist), the pairing code
  is read from `~/.sidecrab/panel-token` by the host itself, and `Update-SideCrab.ps1` rebuilds
  and restarts the host, which replaces the re-import-at-the-desk step. the previous host's widget still
  ships for older builds of the previous host. The 0.28.3 manifest with a `permissions` entry was a scratch
  build that never shipped: the entry works after an import and dies on the previous host's next start.

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
- **0.27.1 widget (2026-09-02)** - fixes 0.27.0, which imported but rendered a blank panel: the previous host
  injects properties as `let` globals and the new `panelToken` property collided with a
  same-named function. Import this one instead.
- **0.29.0 crabd / 0.27.0 widget (2026-09-01)** - panel approvals are safe to turn on: the
  pairing code and per-request id close SEC-a and WID-a. `Install-SideCrab.ps1 -PairingCode`
  prints the code; it goes into the widget's settings in the previous host. Older widgets cannot approve
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
