# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## What this is

SideCrab: a browser panel that polls a localhost Python service, crabd, fed by Claude Code hooks.
crabd listens on `127.0.0.1:9999` and **serves `widget/` itself**, so the panel is
`http://localhost:9999` in any browser. The README's "How it works" and "For developers" sections
are the orientation; this file covers what they do not.

Naming: the product is SideCrab, this repo and its remote are `MaClawdeck`, and the README links the
upstream `Dixie-sketch/Clawdeck`. Leave those links alone unless asked.

The documented platform is macOS (built and measured on 26.6, Apple silicon). The **iCUE widget for
the Corsair Xeneon Edge is retained**: the same `widget/` files package with the WidgetBuilder CLI,
the PowerShell installer stays in `setup/*.ps1`, and CI keeps its Windows job. Nothing Windows was
deleted in the port. `docs/PORT-NOTES.md` is the durable record of the port - every seam, every
measurement, every decision with the phase that made it.

Read `CONTRIBUTING.md` first. Its four rules are enforced by tests and review: **contract first**,
**honest failure** (unknown is `null` / `available: false` / an em-dash, never `0`, never a stale
value re-served as fresh), **every alert must survive a healthy night**, and **a fixed vocabulary,
never free text** into a live session.

## Commands

All suites are headless: stdlib `unittest`, two Node scripts, Pester. Run from the repo root. This
is the macOS CI job in order:

```
python3 -m unittest discover -s companion/tests -t companion/tests   # ~1400 tests, ~1 min
python3 -m unittest discover -s notifier/tests -t notifier/tests
python3 -m unittest discover -s hooks/tests -t hooks/tests
python3 -m unittest discover -s setup/tests -t setup/tests           # the macOS installer
python3 -m unittest discover lighting/tests
node widget/tests/test_ordering.js
node widget/tests/test_panel.js                                      # transport: origin, header
```

One test or one file:

```
python3 -m unittest discover -s companion/tests -t companion/tests -k TitlePrecedenceTests
python3 -m unittest discover -s notifier/tests -t notifier/tests -p test_mute.py
pwsh -File setup/tests/RunTests.ps1 -Path setup/tests/SideCrab.Setup.Tests.ps1   # Windows only; for one test use Invoke-Pester -FullNameFilter
```

Widget checks the Corsair validator misses. CI runs both; run `node --check` yourself too, since a
parse-time SyntaxError is exactly how widget 0.27.0 shipped blank:

```
python3 -c "import xml.etree.ElementTree as ET; ET.fromstring(open('widget/index.html',encoding='utf-8').read())"
node --check widget/scripts/sidecrab.js
```

Panel preview: crabd serves it, so `http://localhost:9999/?mock=<name>` with one of `normal
attention empty stale question quiet recap caveat hot rework dense extras future`. Without crabd,
`cd widget && python3 -m http.server 8765` still works for the mock fixtures. No query string is the
live panel. The `&flag=` screenshot switches are tabulated in `widget/DEV.md` and **only work
alongside `?mock=`** - that gate is now pinned by a test, because an addressable panel makes
`&ackflash=1` a real ack-all POST. `mock/mock-state-dense.json` deliberately lacks
`contextWindowTokens`; do not add it.

Package the iCUE build (Windows, Corsair WidgetBuilder CLI): `icuewidget validate widget` then
`icuewidget package widget`. The `.icuewidget` is gitignored; releases attach it. The validator's
`icueEvents` warning is a known false positive.

Run the services locally: `python3 companion/crabd.py` (no flags). Env overrides: `CRABD_PORT` runs
a second instance, `CRABD_PANEL_DIR` points the static route at another tree, `CRABD_CLAUDE_HOME`
points it at a fake `~/.claude` (and, on macOS, suppresses the Keychain entirely - a custom config
dir keys a different item). `HOME=<tmp> python3 companion/crabd.py` isolates the whole `~/.sidecrab`
side. The notifier: `python3 notifier/sidecrab_toast.py --once --dry-run`; the `--test-toast`,
`--test-digest`, `--test-budget`, `--test-approval`, `--test-stale`, `--test-longrun` flags fire one
sample and never mark the ledgers. `--test-toast` is the one to run first on a Mac: the first
notification a process posts can raise a one-time permission prompt for Script Editor.

The installer, all from the repo root:

```
./setup/install.sh [--with-toast] [--with-approvals|--no-approvals] [--force-enable] [--yes]
./setup/install.sh --status | --doctor | --pairing-code | --limits-token
./setup/update.sh
./setup/uninstall.sh [--purge] [--yes]
```

`--status` writes nothing. `--doctor` does: a real SessionStart / Notification / SessionEnd cycle
for the session id `smoke-test`, leaving three rows in `history.jsonl`.

On macOS: `python` is not on PATH (use `python3`; the project targets 3.13+, and Apple's
`/usr/bin/python3` 3.9 is refused by version), and `pwsh` and `icuewidget` are not installed, so the
Pester suite (`C:\` paths, DPAPI) is the one suite that does not run. Everything else passes with no
expected failures; a handful of tests skip themselves on the platform they do not apply to.

## Architecture

### The wire contract is the spine

The widget and crabd ship separately and are never guaranteed to be the same version.
`docs/STATE-CONTRACT.md` is the source of truth for `/v1/state`, `/v1/action` and `/v1/config`; any
wire change lands there first, then in both sides. It is append-only with the newest dated section
at the top; the pre-publication "schema 6" headers in its lower half are history, not current.
`schema` (5, `SCHEMA_BREAKING` in crabd, `SCHEMA_MAX` in the widget) marks the last **breaking**
shape and is not a feature level. Every feature is detected by **field presence**, and presence
checks test type, not truthiness (`quiet.override: null`, `contextWindowTokens: null`, `cpuPct:
null` are all real shapes). The widget's only comparison against `doc.schema` is the acceptance
check; adding a second undoes the v0.6.1 rework. A bump costs a reload for a panel crabd serves and
dead-feeds every *installed iCUE widget* until someone re-imports it at the console, so additive
work never touches it. The top five dated sections (v0.30.0 to v0.34.0) are the port's wire record;
the file's own index paragraph points at them.

### crabd (`companion/crabd.py`, one module, no argv)

Layout, top to bottom: constants and measured-behaviour notes (`VERSION` at line 91), pure helpers,
then one class per concern (config, history, git, transcripts, `HookTracker` as the session state
machine, the platform classes, `LimitsReader`, the OTLP / status-line / recap / fleet / host
readers, `ContinueQueue`, `PanelToken` / `PermissionBroker`, `StateBuilder`, `Handler`,
`CrabdServer`), the loops, `main`. `grep -n '^class '` is the map.

**The platform seam.** `WindowsPlatform` / `DarwinPlatform` / `NullPlatform`, one object per OS,
chosen by `select_platform(sys.platform)` into the module global `PLATFORM`. That assignment is
**the only read of the host's platform string in the module**, and a source-text test asserts it
stays the only one; a second read is a second answer that can disagree, and it would be right on the
host it was written on. `ctypes.windll` appears in exactly two places, `WindowsPlatform` and
`_dpapi_unprotect` (a Windows helper, not a reader - the same test names that one exception).
`companion/tests/test_crabd_platform.py` is the surface pin: all three classes expose the same
twelve methods (`cpu_times`, `memory`, `fleet_targets`, `service_query`, `service_status`,
`read_limits_token`, `store_limits_token`, `limits_token_hint`, `cli_credentials`,
`server_reuse_address`, `port_holder_hint`), with the same signatures and **bound the same way** -
interchangeable, not merely answering alike today. Injection still beats the platform: every reader
takes its callables as kwargs, so it is testable on any host, and platform selection happens at the
wiring site in `main`.

`KEYCHAIN_CREDENTIALS_ENABLED` (module global, default `True`) is the kill switch: it gates **all
three** Keychain accesses, not only the credential one it is named for. Every companion test module
sets it `False` in `setUpModule`, exactly as it repoints the path globals; the tests that exercise
those paths turn it on with an injected runner. "No test reaches the operator's Keychain" is only a
guarantee if it has no exceptions.

`StateBuilder` is the hub. Every reader is an optional constructor kwarg defaulting to `None`
("feature not wired"), which is the primary test seam. Four daemon threads: `_refresh_loop` builds
the snapshot every 2 s off the request path; `_recap_loop` and `_fleet_loop` do `git` and
`schtasks` / `launchctl` subprocess work on their own cadence so it never runs on the builder;
`_expiry_loop` prunes queued prompts separately so a wedged builder still expires them.
`OtlpReceiver` reaches the builder through a late-bound holder dict;
`StateBuilder.note_session_event` is the only door onto served rings.

HTTP surface. GET `/` and `/index.html` (the panel), `/styles|scripts|resources|mock/...` (static),
`/v1/health` (diagnostics, not the contract), `/v1/state`, `/v1/history?day=`, `/v1/panel-log`. POST
`/v1/hook`, `/v1/hook/stop`, `/v1/hook/permission`, `/v1/statusline`, `/v1/metrics`, `/v1/logs`,
`/v1/action`, `/v1/config`, `/v1/panel-log`. Four gates, in this order:

- **`Host` allowlist**, ahead of everything, on GET, POST and OPTIONS alike. Absent is allowed;
  `localhost` / `127.0.0.1` / `[::1]` with no port or the bound port is allowed; anything else is
  403 `{"error":"host not allowed"}` with no CORS header. This is the DNS-rebinding gate and the
  only one that can be - a rebound page is same-origin, so its GET carries no `Origin` at all. It
  also refuses a port forward or a reverse proxy, deliberately: that is the same shape.
- **Origin allowlist** (`_is_web_origin`): the three spellings of *this crabd's bound* origin are
  handled and reflected exactly; every other `http(s)` origin, including the same host on another
  port, is 403. `null`, `file://` and `qrc://` keep their old answers. `ACAO: *` is illegal
  anywhere. Do not "fix" this by rejecting `null` (the iCUE webview may send it) and do not loosen
  it to accept http(s) generally.
- **Panel header**: every POST must carry `X-SideCrab-Panel` with any non-empty value, on every
  path including unknown ones, or 403 `{"error":"panel header required"}` - a distinct body, so a
  hook being wired up is distinguishable from a cross-site refusal. The value is never read; what
  it does is make the POST non-simple, and `do_OPTIONS` never lists it for `Origin: null`. GET and
  OPTIONS never require it.
- **Pairing token** (`~/.sidecrab/panel-token`, minted on first start): only the `decide` action
  needs it. The check order in `_do_decide` is the security argument (shape, then token presence /
  lockout / match, then `requestId`, then apply) and never falls open. `panelApprovals` is never in
  `CONFIG_WRITABLE`.

**Static serving.** `PANEL_DIR` defaults to `widget/` beside `crabd.py`, overridable by
`CRABD_PANEL_DIR`. Only a first segment of `styles`, `scripts`, `resources` or `mock` serves a
file, so `manifest.json`, `translation.json`, `DEV.md` and `tests/` are 404. One percent-decode,
then a refusal of `..` segments, backslashes, NULs, empty and dot segments and any surviving `%`,
then the resolved candidate must sit under the resolved panel dir (which also catches a symlink
pointing out). One reply reads at most `PANEL_MAX_BYTES` = 64 MB, `stat`-checked before the read.
`X-Content-Type-Options: nosniff` and `Cache-Control: no-store` are on **every** response, static or
API - one rule for the whole daemon rather than a per-branch flag a new route can forget; `no-store`
matters here because the panel now ships with crabd. A static read never touches the builder's lock.

**The bind.** `HOST = "127.0.0.1"` is a module literal - no `CRABD_HOST`, no config key, and a
source-text test refuses `0.0.0.0`. `DEFAULT_PORT = 9999`, `CRABD_PORT` overrides. One bind attempt
and one only: a collision prints what the OS said verbatim plus the platform's own port-holder
command (`lsof -nP -iTCP:<port> -sTCP:LISTEN` on macOS, `Get-NetTCPConnection` on Windows) and exits
1. Never move to another port - crabd on 10000 while every hook addresses 9999 is a silent dead
panel. `allow_reuse_address` is `PLATFORM.server_reuse_address()`: `False` on Windows (where it
admits a second listener), `True` on macOS and Linux (where it only buys a restart inside
TIME_WAIT).

**Host metrics on Darwin.** `host_statistics(HOST_CPU_LOAD_INFO)` and
`host_statistics64(HOST_VM_INFO64)` through libSystem plus `sysctlbyname`. The Darwin reader adapts
to `HostSampler`'s existing Win32 tuple convention rather than the reverse - it hands back
`(idle, system + idle, user + nice) * scale` in 100 ns units, so every A-07/A-08/A-09 branch and
`CPU_MIN_TOTAL_TICKS` keep their measured provenance. **`nice` is busy time**; **idle is folded into
kernel** (unfolded, the sampler serves null on every pass on a healthy Mac); the 32-bit tick
counters are **unwrapped** per bucket before the sampler sees them, because they wrap after ~31 days
of uptime. `memUsedGB` is **Activity Monitor's "Memory Used"** - internal minus purgeable, plus
wired, plus compressed - not `top`'s total-minus-free, which differs by ~33 GiB on the machine this
was measured on. The three failure tiers are unchanged: both readings failing means **no `host` key
at all**.

**Fleet on launchd.** `launchctl print gui/<uid>/<label>`, on the fleet thread. Only the
**first-level** `state =` line counts - sub-objects carry their own `state = active`. `running` /
`stopped` (`not running`, `waiting`, `spawn scheduled`) / `absent` (only the measured "Could not
find service" wording) / `unknown` (everything else). `fleet.glow` is `absent` on macOS: there is no
lighting component, so the label is empty, nothing is spawned, and `absent` is the literally true
word. The key stays so the document's shape is identical on both platforms.

**The two Keychain items** (macOS, crabd 0.34.0), both generic-password in the **login** Keychain
with the login user name as the account: `Claude Code-credentials` (written by Claude Code) and
`SideCrab limits token` (written by `setup/install.sh --limits-token`). The file
`~/.claude/.credentials.json` is read **first** and the Keychain second, and the Keychain is not
consulted at all when `CRABD_CLAUDE_HOME` is set - a custom config dir keys a different item whose
name crabd cannot compose. **Storing goes in on stdin**: `security -i` with argv exactly `["-i"]`,
the value hex-encoded by `-X`, because `ps` is world-readable on macOS. Exit 44 is *absence*, not
failure. A refused read (ran, exited non-zero and non-44) gets its own note pointing at the
prompt - distinct from "no credentials", because the two have different fixes.

State machine (`HookTracker.STATE_EVENTS`): SessionStart to `idle`, UserPromptSubmit to `working`,
Notification to `needs_input`, Stop to `done`, SessionEnd to `gone`; SubagentStop only decrements.
Aging by transcript mtime retires `working` after 15 min and to `gone` after 2 h; `needs_input` is
exempt from those two clocks and drops only after 36 h without activity or past 512 live rows,
oldest first. `queue-continue` on a session with no live hook state and an old transcript is refused
409 (GHOST-a). The Stop hook is peek, send, consume, so a failed send keeps the prompt. The
permission hook parks a real thread for up to 55 s, holds at most 8 pending requests
(`PERMISSION_MAX_PENDING`, so a ninth is refused rather than held), and every early exit returns
`{}` (pass-through); no path reaches `allow` without a tap.

Disk: writes only `~/.sidecrab/{config.json, history.jsonl, limits-cache.json, panel-token}`; reads
`~/.claude/projects/**/*.jsonl`, `~/.claude/.credentials.json`, `~/.sidecrab/limits-token.dpapi`,
and on macOS the two Keychain items instead of the last two. Paths are module globals resolved per
call; each test module's `setUpModule` repoints them at a temp dir **and sets the Keychain kill
switch** (the real cache was poisoned once, and one config file was written into the operator's real
`~/.sidecrab` during the port before the isolation was completed).

Invariants: `/v1/state` never 500s (503 only before the first snapshot). OTLP always answers 204.
Hooks are answered before parsing. Every swallowed exception reports through `_log_once` (capped by
`LOG_ONCE_MAX_KEYS`); silence is the forbidden failure mode. `model` is served verbatim including
the `[1m]` marker. History holds no free-form text, which is why continue prompts are whitelisted.

Tests: base classes `TempProjects`, `ServedOverASocket` (test_crabd), `LiveFireServed`
(test_crabd_livefire), `BuilderHarness` (test_crabd_datalane); the port added
`test_crabd_platform.py` (the surface pin), `test_crabd_transport.py`, `test_crabd_panel.py` (Host
allowlist, header gate, static route), `test_crabd_host_darwin.py`, `test_crabd_fleet_darwin.py` and
`test_crabd_token_darwin.py`. Clocks are explicit `now=` arguments, never patched. Use
`_httpkeepalive.start_test_server` with `KeepAliveClient` - it asserts the port is not
`crabd.DEFAULT_PORT`, off the constant rather than a literal, so the guard cannot drift from the
value it protects - and call `settle()` / `quiesce()` after any fire-and-forget POST: the 204
precedes the parse, so asserting on the next line is a race.

### Widget (`widget/`, flat browser script, no bundler, no exports)

crabd serves this tree; iCUE still loads the same `index.html` as **strict XML**, so that constraint
stays: uppercase `<!DOCTYPE html>`, no bare `&` (the inlined sensor wrapper sits in a CDATA block for
this reason), and no `--` inside an HTML comment. `manifest.json` and the strict-XML CI parse are
**kept deliberately** (`DEV.md` v0.30.0) - the iCUE build is these files, and that parse is the only
check that has ever caught a blank-panel ship.

Settings are declared once, in `index.html`: `<meta name="x-icue-property">` tags plus the
`<script id="x-icue-groups">` block. iCUE reads them to build its console; **the browser panel
parses the same declarations at runtime to generate its own settings sheet** (gear beside the filter
chips, or `s`), so a setting is one meta and one group membership and there is no second copy to
disagree. Every declared property must be claimed by exactly one group - the suite asserts it.
`cpuTempSensor`, `gpuTempSensor` and `touchDiag` are *absent* rather than disabled outside iCUE:
there is no bridge behind them.

`insideIcue()` is the host test, memoised, and it asks whether `uniqueId` is **declared**, not what
it holds - an injected empty `uniqueId` is still iCUE. Off iCUE, everything the panel persists lives
in **one namespaced `localStorage` object** under `PANEL_STORE_KEY = 'sidecrab'` on the panel's own
origin: settings, display state, pins and the pairing code, read-modify-write with unknown values
round-tripped. Never a bare key of its own.

iCUE injects each property as a same-named `let` global. **Never declare a function or top-level
`var` with a property's name**: 0.27.0 shipped blank because `panelToken` was both (the reader is
now `pairingCode()`), and that trap is a test now rather than three comments. Properties are read
live through `getIcueProperty` / `boolProp` / `strProp`, which probe with `Function()` so the same
file runs in a plain browser. `icueEvents` is a bare assignment on purpose. `tr()` wraps only the
property labels, and `translation.json` holds only those; the panel's own strings are literals in
the JS, and `<title>` is now the literal `SideCrab` (`tr()` is substituted by nothing in a browser).
The iCUE webview is Chromium 130 (QtWebEngine 6.9): no `:has()`, no container queries, no
`backdrop-filter`, no subgrid - the floor the CSS is still written to.

`scripts/sidecrab.js` runs top to bottom, each section under a banner comment: tunables (`POLL_MS`
3000, `POLL_TIMEOUT_MS` 2500 which must stay under it, `STALE_MS` 30000, `SCHEMA_MAX` 5, gesture
slop constants ordered so no two gestures claim one pointer), iCUE glue, polling (`poll`,
`acceptDoc`), wardrobe and tricks, `render()`, limits, sessions (`renderSessions` / `clampGrid` /
`buildCard`), sheets with the `postAction` / `postConfig` / `postJson` trio, touch gestures,
sensors, the mock harness, then `tick()` (1 Hz) and `init()`. The 1 Hz tick is load-bearing:
escalation tiers, countdowns, the approval hold and the stale edge fire on the second, not on the
next poll. `acceptDoc` order matters: a `crabd.version` change clears every capability latch, and
trick detection runs before `render()`.

Talking to crabd: `baseUrl()` returns `''` when `location.protocol` is `http:`/`https:` - every path
relative and same-origin, never preflighted - and `http://127.0.0.1:<crabdPort>` off the filesystem,
which is the iCUE case (a location with no protocol reads as iCUE too). An absolute URL from a
served page would be *cross-origin* to `http://localhost:9999` and answered 403. Every POST carries
`X-SideCrab-Panel`, **on both attempts**: `postJson` tries `application/json` and falls back once to
`text/plain` when the first attempt rejects, whether a network error or the 4 s abort. `decide`
carries the pairing code read live on every tap plus the `requestId` the sheet is showing; 403 means
wrong code, 409 the request changed, 429 locked. A 404 from `/v1/config` latches the whole endpoint
(`cfgEndpointUnsupported`); a 400 is handled per key. Neither may touch `pollFailed`. The filter
chips are a view, not a state: the glow, the crab and the notification threshold still see every
session.

Keyboard (v0.30.0), because a panel with an address is driven from a keyboard by definition: `a`
ack-all, `p` pin, Delete/Backspace dismiss, `r` refresh, `s` settings, Escape closes, Tab trapped
inside an open sheet, Enter/Space activate. Each calls **the same function its gesture calls**. All
inert behind a modifier (Cmd-R, Cmd-S and friends stay the browser's), behind `ev.repeat`, while a
sheet is open, and while an input has focus. Arrow-key grid navigation is deliberately skipped and
named as such.

The sensors row keeps only crabd's `host` half: no page reads a die temperature, `sensorsPlugin()`
returns null, and both iCUE hints go with the cells. An absent or all-null `host` takes the whole
row off the glass, never a row of zeros.

Layout is CSS-owned: density is `body.density-compact`, the slot layouts are `max-width`,
`max-height: 420px` and `max-aspect-ratio: 3/2` media queries in `styles/sidecrab.css` (the slot
names in `DEV.md` such as 840x696 and 416x696 are labels for those combinations, not literals in the
CSS), and `gridCapacity()` reads the computed track counts, so JS never knows the row count. One
baseline, now **bounded**: `--layout-unit: clamp(4.5px, 1vmin, 7.2px)`, because a browser window is
dragged and unbounded vmin inflated the whole panel by 25% at 1440x900. The ceiling *is* the Edge
slot's own vmin, so 2560x720 is byte-identical. No component selector uses a raw viewport unit. The
resize handler **empties the grid before re-rendering**: a real engine reports implicit tracks, so a
grid holding more cards than the new slot has cells reports its own overflow back to
`gridCapacity()` - a loop that cannot escape. The macOS system fonts join both stacks **behind** the
faces every px width comment was measured in. The accent default is stated in three places
(`:root`, the `accentColor` meta, the `strProp` fallback in `applyProperties`) and the JS one wins.

`tests/` is three files plus a shim: `_dom.js` (a ~few-hundred-line DOM shim that *parses the
shipping `index.html`* rather than listing ids - deliberately not jsdom, so `node file.js` needs
nothing installed; no layout, no cascade, no combinators), `_harness.js` (the shared vm loader),
`test_ordering.js` and `test_panel.js` (transport: origin, relative-vs-absolute, the header on both
attempts). The harness loads the shipping file whole into a `vm` context with a `document` stub
whose `readyState` is `loading`, then reads top-level names off the context. If a suite stops
loading, new top-level work in `sidecrab.js` needs a stub **in `_harness.js`**; never fork the
logic. `test_ordering.js` carries a deliberate mutant clamp and asserts that it fails.

`DEV.md` is the measured-evidence log, not just a flag table: a layout or threshold change is
expected to land there with the number it was measured at, at a named slot.

Version: `manifest.json` is the only machine-read version; the `vX.Y.Z` tags in the JS, CSS and HTML
are provenance comments that get one sweep per release.

### Notifier (`notifier/sidecrab_toast.py`)

Polls `/v1/state` every 10 s. Everything above the adapters is pure and shared; the `ToastAdapter`
protocol is the seam and `RecordingToastAdapter` serves both tests and `--dry-run`. Six deciders
(waiting, approval, longrun, digest, budget, outage) are injected into `Notifier`;
`StaleFeedDecider` runs first, ahead of every early return. Quiet hours suppress and mark; snooze
defers. That asymmetry is the feature. `Notifier._emit` is the single global-mute point. The
approval alert has no Approve/Deny buttons on purpose. `SUPPORTED_SCHEMAS` is an explicit set, and a
stale set means a silent standstill. The only file written is `~/.sidecrab/toast-state.json`, via
`DayLedger` subclasses keyed by `SECTION` with PID-tagged temp files because the snooze handler is a
second process.

`pick_adapter(sys.platform, icon)` chooses at **one construction site in `main()`**, so every
`--test-*` flag exercises the real path. `PowerShellToastAdapter` pins PowerShell 5.1 from System32
(pwsh 7 lacks the WinRT projection). `MacNotificationAdapter` runs `/usr/bin/osascript` with **three
constant `-e` strings**, then `--`, then body, title and subtitle as positional arguments: the text
is never interpolated into the script, which is the same boundary base64 buys on Windows, obtained
by not building a script out of operator text at all. `strip_control` on every argument is
load-bearing, not cosmetic - `subprocess` raises `ValueError` on a NUL and that is not one of the
failures `show()` converts to `False`. Anything neither Darwin nor Windows gets
`UnsupportedPlatformAdapter`, which says so in the operator's terms rather than naming a System32
path. Three standing macOS properties, none of them defects: **no buttons** (`display notification`
has no action affordance, so acknowledgement is on the panel), **no replacement identifier** (a
second notice stacks under the first), and **the identity is Script Editor's** (so that is whose
per-app switch mutes it; the subtitle always says SideCrab). `sidecrab_ack_handler.pyw` and
`sidecrab_snooze_handler.pyw`, and the two URL schemes, are the Windows route's and stay
Windows-only; neither imports `sidecrab_toast` and their constants are pinned by tests.

### Hooks (`hooks/`)

**Two fragments**, `settings-hooks-fragment.json` (Windows, merged by `Install-SideCrab.ps1`) and
`settings-hooks-fragment-macos.json` (merged by `install.sh`), identical apart from the curl
invocation - `hooks/tests/test_hooks_fragment.py` compares them with that one difference normalised
away, so a fix to one is a fix to both or a failing test. macOS uses `/usr/bin/curl` by absolute
path (hooks run under an `sh -c` that inherits no login `PATH`) and single quotes; Windows uses
System32 `curl.exe` and double quotes (`cmd.exe` has no single-quote literal).

Five fire-and-forget `command` hooks (`curl -s -m 2 -X POST -H 'X-SideCrab-Panel: 1' ... || exit 0`)
and two `type: "http"` hooks: Stop to `/v1/hook/stop` (crabd feeds a queued continue prompt back as
`additionalContext`) and PermissionRequest to `/v1/hook/permission` (60 s timeout, past crabd's 55 s
poll). Both carry the header in their `headers` map. The split exists because Claude Code skips HTTP
hooks for SessionStart and Setup. If the operator has set `allowedHttpHookUrls` anywhere it must
include **both** `http://127.0.0.1:9999/*` and `http://localhost:9999/*` - patterns match the URL as
written - or both HTTP hooks are blocked outright and approvals silently do nothing; the installer
**extends** that key and never creates it, because creating it would switch the allowlist on for
every other http hook. No PreToolUse/PostToolUse, deliberately. `sidecrab_statusline.py` is the
`statusLine` command, not a hook: it POSTs stdin to `/v1/statusline` then chains to the operator's
prior command saved in `~/.sidecrab/statusline-chain.json`. Both installers match SideCrab's entries
on the substring `127.0.0.1:9999/v1/hook`, which the two http URLs contain as a prefix, so one
marker finds both `command` and `url` entries.

### Setup (`setup/`, two installers side by side)

**macOS** is `setup/sidecrab_setup.py`, one importable stdlib module, plus three thin `sh` wrappers
(`install.sh`, `update.sh`, `uninstall.sh`) that do nothing but resolve a Python 3.13+ and hand
over. It is structured the way `SideCrab.Common.ps1` is: pure decision helpers first
(`approvals_decision`, `agent_state`, `merge_hook_fragment`, ...), then thin impure wrappers, then
the commands. **Every impure dependency reaches the module through one `Environment` dataclass** -
launchctl, `security`, `lsof`, HTTP, the clock, the interpreter probe, `emit`, `ask`, `read_secret`,
`store_token`, `store_capable` - so the whole suite (`setup/tests/*.py`) runs headless against a
temporary `HOME` with nothing installed and never touches the developer's Keychain. Adding a
component is one row in `AGENTS`.

Interpreter resolution **probes for a version, never a path**: `$SIDECRAB_PYTHON`, then
`python3.14`, `python3.13`, `python3` across `PATH`, `/opt/homebrew/bin`, `/usr/local/bin`; Apple's
3.9 stub is refused by version, and the absolute path found is written into the plists because a
LaunchAgent inherits no login `PATH`. Agents are `com.sidecrab.crabd` and `com.sidecrab.toast`
(there is no glow on macOS); plists in `~/Library/LaunchAgents`, logs in `~/.sidecrab/logs` at mode
0700 because they carry session titles and repo paths; load is `bootout` then `bootstrap
gui/<uid>`, restart is `kickstart -k`. **An agent the operator disabled is refreshed and not
started**; `--force-enable` is the only override - launchd has the same trap
`Register-ScheduledTask -Force` does, which once resurrected the parked glow. Install and update
**refuse before writing anything** when something foreign holds port 9999, and name the PID: waits
are counted in polls because the sleep is injectable. `--status` is read-only; `--doctor` is not (a
real `smoke-test` hook cycle plus a header-gate probe that expects the 403), and its rows are agents,
python, health, state reachable/schema/freshness, panel, panel schema, notifier schema, hook header,
the three hook events, hook cycle, `config.json`, statusline chain, limits token, panel approvals.
`--pairing-code` prints the code and names the panel URL to enter it at; `--limits-token` reads the
token from **stdin** and never from argv. Uninstall takes back what SideCrab wrote and nothing else;
`--purge` deletes `~/.sidecrab` after saying what is in it.

**Windows** is unchanged: every `.ps1` dot-sources `SideCrab.Common.ps1`, which defines about sixty
functions and runs nothing at load; the Pester suite AST-parses the scripts, lifts the pure
functions with `Import-AstFunction`, injects scriptblocks for the impure paths, and asserts some
invariants against source text. Tasks are `SideCrab-crabd`, `SideCrab-glow`, `SideCrab-toast`;
registry writes are HKCU only and nothing needs elevation.

Both: every write to `settings.json` or `config.json` is backed up to
`<path>.sidecrab-bak-YYYYMMDD-HHMMSS` first and replaced atomically (same-directory temp plus
rename), hook merging is **entry-level** so a hand-merged foreign hook in one of our matcher groups
survives, and `panelApprovals: false` is written only when the key is absent so a re-run never
reverts an operator who armed it.

### Lighting (`lighting/`)

Parked, and Windows-only: there is no macOS counterpart, the macOS installer has no glow row, and
crabd serves `fleet.glow: absent` there without spawning anything. The real cause was a use-after-free
in the lifetime of Corsair's `CueSdk.connect()` callback thunk, nothing to do with consoles or
pythonw despite the README and CHANGELOG framing; `IcueAdapter.connect` holds the fix, and
`CueSdk.disconnect()` must never be called (it hard-crashes the interpreter). The task ships
disabled and the Windows installer will not re-enable it.
`decision.decide(doc, now)` is pure; `icue.IcueAdapter` / `NullAdapter` is the only SDK seam; tests
swap a fake `cuesdk` into `sys.modules`. `cuesdk==4.0.84` is the one pinned dependency.

## Conventions when changing things

- Contract first: `docs/STATE-CONTRACT.md`, then crabd, then the widget. Update `README.md` for
  user-facing changes and `CHANGELOG.md` for anything a user would notice.
- Bump the version of the component you touched: `widget/manifest.json`, `VERSION` in
  `companion/crabd.py`, `__version__` in `notifier/sidecrab_toast.py`. `setup/` carries **no
  version**: it is dated, not numbered, so its `CHANGELOG.md` line reads `- **setup (YYYY-MM-DD)**`.
  Update the `## Current` table in `CHANGELOG.md` and add a
  `- **<version> <component> (YYYY-MM-DD)** - ...` line under the highlights.
- Findings: letter-suffixed IDs are current-era (`SEC-a`, `WID-a`, `GHOST-a`, `CD-33`, `AUD-F1`),
  numeric ones are historical. Open items live in `docs/BACKLOG.md` (security ones also under
  "Disclosed residuals" in `SECURITY.md`). Closing never deletes the row: strike the title, append
  `CLOSED in <component> <version> (date)` with the mechanism and the pinning tests.
- Mutation-prove any gate that protects the user: break it, watch a test fail, fix it.
- Alerts and thresholds are answered by a replay against real data, not by reasoning.
- Comments earn their place by stopping a mistake (a trap with mechanism and symptom, a measured
  number with provenance, a deliberate non-action). Cut narration.
- Line endings: LF everywhere except PowerShell and batch files, which stay CRLF (`.gitattributes`;
  `.editorconfig` repeats it for `.ps1` / `.psm1`). Indent 4 for Python and PowerShell, 2 for JS,
  JSON, CSS, HTML, YAML and Markdown.
- Standard-library Python only; no new runtime dependencies without a reason.
