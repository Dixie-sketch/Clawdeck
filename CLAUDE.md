# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this
repository.

## What this is

SideCrab: an iCUE widget for the Corsair Xeneon Edge that polls a localhost Python service, crabd,
fed by Claude Code hooks. The README's "How it works" and "For developers" sections are the
orientation; this file covers what they do not.

Naming: the product is SideCrab, this repo and its remote are `MaClawdeck`, and the README links the
upstream `Dixie-sketch/Clawdeck`. Leave those links alone unless asked.

The product is Windows-only. This checkout is often worked on from macOS, where everything except
the Pester suite and widget packaging still runs (details under Commands).

Read `CONTRIBUTING.md` first. Its four rules are enforced by tests and review: **contract first**,
**honest failure** (unknown is `null` / `available: false` / an em-dash, never `0`, never a stale
value re-served as fresh), **every alert must survive a healthy night**, and **a fixed vocabulary,
never free text** into a live session.

## Commands

All suites are headless: stdlib `unittest`, one Node script, Pester. Run from the repo root.

```
python -m unittest discover -s companion/tests -t companion/tests   # ~1100 tests, ~3 min
python -m unittest discover -s notifier/tests  -t notifier/tests
python -m unittest discover lighting/tests
python -m unittest discover -s hooks/tests -t hooks/tests
node widget/tests/test_ordering.js
pwsh -File setup/tests/RunTests.ps1          # Pester 5 if installed, else its built-in shim (-NoPester forces the shim)
```

One test or one file:

```
python -m unittest discover -s companion/tests -t companion/tests -k TitlePrecedenceTests
python -m unittest discover -s notifier/tests -t notifier/tests -p test_mute.py
pwsh -File setup/tests/RunTests.ps1 -Path setup/tests/SideCrab.Setup.Tests.ps1   # one file; for one test use Invoke-Pester -FullNameFilter
```

Widget checks the Corsair validator misses. CI runs the strict-XML one; run `node --check` yourself,
since a parse-time SyntaxError is exactly how widget 0.27.0 shipped blank:

```
python -c "import xml.etree.ElementTree as ET; ET.fromstring(open('widget/index.html',encoding='utf-8').read())"
node --check widget/scripts/sidecrab.js
```

Widget preview without iCUE or crabd: `cd widget && python -m http.server 8765`, open it in Chromium
sized 2560x720, and append `?mock=<name>` with one of `normal attention empty stale question quiet
recap caveat hot rework dense extras future`. No query string is the standalone state (what a store
user sees first). The `&flag=` screenshot switches are tabulated in `widget/DEV.md` and only work
alongside `?mock=`. `mock/mock-state-dense.json` deliberately lacks `contextWindowTokens`; do not
add it.

Package (Windows, Corsair WidgetBuilder CLI): `icuewidget validate widget` then `icuewidget package
widget`. The `.icuewidget` is gitignored; releases attach it. The validator's `icueEvents` warning
is a known false positive.

Run the services locally: `python companion/crabd.py` (no flags; `CRABD_PORT` runs a second
instance, `CRABD_CLAUDE_HOME` points it at a fake `~/.claude`). The notifier: `python
notifier/sidecrab_toast.py --once --dry-run`; the `--test-toast`, `--test-digest`, `--test-budget`,
`--test-approval`, `--test-stale`, `--test-longrun` flags fire one sample and never mark the
ledgers.

On macOS: `python` is not on PATH (use `python3`; the project targets 3.13), `pwsh` and `icuewidget`
are not installed, the Pester suite is Windows-only (it asserts `C:\` paths and calls DPAPI), and
the notifier suite has one expected off-Windows failure (`test_decider.py` asserts a drive letter in
the toast image path). Two companion tests skip off-Windows. Everything else passes.

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
check; adding a second undoes the v0.6.1 rework. A bump dead-feeds every installed widget until
someone re-imports it at the iCUE console, so additive work never touches it.

### crabd (`companion/crabd.py`, one module, no argv)

Layout, top to bottom: constants and measured-behaviour notes (`VERSION` at line 78), pure helpers,
then one class per concern (config, history, git, transcripts, `HookTracker` as the session state
machine, `LimitsReader`, the OTLP / status-line / recap / fleet / host readers, `ContinueQueue`,
`PanelToken` / `PermissionBroker`, `StateBuilder`, `Handler`, `CrabdServer`), the loops, `main`.
`grep -n '^class '` is the map.

`StateBuilder` is the hub. Every reader is an optional constructor kwarg defaulting to `None`
("feature not wired"), which is the primary test seam. Four daemon threads: `_refresh_loop` builds
the snapshot every 2 s off the request path; `_recap_loop` and `_fleet_loop` do git and `schtasks`
subprocess work on their own cadence so it never runs on the builder; `_expiry_loop` prunes queued
prompts separately so a wedged builder still expires them. `OtlpReceiver` reaches the builder
through a late-bound holder dict; `StateBuilder.note_session_event` is the only door onto served
rings.

HTTP surface. GET `/v1/health` (diagnostics, not the contract), `/v1/state`, `/v1/history?day=`,
`/v1/panel-log`. POST `/v1/hook`, `/v1/hook/stop`, `/v1/hook/permission`, `/v1/statusline`,
`/v1/metrics`, `/v1/logs`, `/v1/action`, `/v1/config`, `/v1/panel-log`. Two gates:

- **Origin** (`_is_web_origin`): a present `http(s)` Origin is refused 403 on reads and writes
  alike; absent, `null` or non-web is allowed and reflected. `ACAO: *` is illegal anywhere. Do not
  "fix" this by rejecting `null`: the iCUE webview legitimately sends it.
- **Pairing token** (`~/.sidecrab/panel-token`, minted on first start): only the `decide` action
  needs it. The check order in `_do_decide` is the security argument (shape, then token presence /
  lockout / match, then `requestId`, then apply) and never falls open. `panelApprovals` is never in
  `CONFIG_WRITABLE`.

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
`~/.claude/projects/**/*.jsonl`, `~/.claude/.credentials.json`, `~/.sidecrab/limits-token.dpapi`.
Paths are module globals resolved per call; each test module's `setUpModule` repoints them at a temp
dir (the real cache was poisoned once).

Invariants: `/v1/state` never 500s (503 only before the first snapshot). OTLP always answers 204.
Hooks are answered before parsing. Every swallowed exception reports through `_log_once` (capped by
`LOG_ONCE_MAX_KEYS`); silence is the forbidden failure mode. `model` is served verbatim including
the `[1m]` marker. History holds no free-form text, which is why continue prompts are whitelisted.

Tests: base classes `TempProjects`, `ServedOverASocket` (test_crabd), `LiveFireServed`
(test_crabd_livefire), `BuilderHarness` (test_crabd_datalane). Clocks are explicit `now=` arguments,
never patched. Use `_httpkeepalive.start_test_server` (never port 2722) with `KeepAliveClient`, and
call `settle()` / `quiesce()` after any fire-and-forget POST: the 204 precedes the parse, so
asserting on the next line is a race.

### Widget (`widget/`, flat browser script, no bundler, no exports)

iCUE loads `index.html` as **strict XML**: uppercase `<!DOCTYPE html>`, no bare `&` (the inlined
sensor wrapper sits in a CDATA block for this reason), and no `--` inside an HTML comment. Widget
properties are `<meta name="x-icue-property">` tags in `index.html`, not the manifest, and iCUE
injects each one as a same-named `let` global. **Never declare a function or top-level `var` with a
property's name**: 0.27.0 shipped blank because `panelToken` was both (the reader is now
`pairingCode()`). Properties are read live through `getIcueProperty` / `boolProp` / `strProp`, which
probe with `Function()` so the same file runs in a plain browser. `icueEvents` is a bare assignment
on purpose. `tr()` wraps only the iCUE property labels in `index.html`, and `translation.json` holds
only those; the panel's own strings are literals in the JS. The webview is Chromium 130 (QtWebEngine
6.9): no `:has()`, no container queries, no `backdrop-filter`, no subgrid.

`scripts/sidecrab.js` runs top to bottom, each section under a banner comment: tunables (`POLL_MS`
3000, `POLL_TIMEOUT_MS` 2500 which must stay under it, `STALE_MS` 30000, `SCHEMA_MAX` 5, gesture
slop constants ordered so no two gestures claim one pointer), iCUE glue, polling (`poll`,
`acceptDoc`), wardrobe and tricks, `render()`, limits, sessions (`renderSessions` / `clampGrid` /
`buildCard`), sheets with the `postAction` / `postConfig` / `postJson` trio, touch gestures,
sensors, the mock harness, then `tick()` (1 Hz) and `init()`. The 1 Hz tick is load-bearing:
escalation tiers, countdowns, the approval hold and the stale edge fire on the second, not on the
next poll. `acceptDoc` order matters: a `crabd.version` change clears every capability latch, and
trick detection runs before `render()`.

Talking to crabd: `postJson` tries `application/json` and falls back once to `text/plain` when the
first attempt rejects, whether a network error or the 4 s abort (the JSON content type makes the
POST preflighted). `decide` carries the pairing code read live from the property on every tap plus
the `requestId` the sheet is showing; 403 means wrong code, 409 the request changed, 429 locked. A
404 from `/v1/config` latches the whole endpoint (`cfgEndpointUnsupported`); a 400 is handled per
key. Neither may touch `pollFailed`. The filter chips are a view, not a state: the glow, the crab
and the toast threshold still see every session.

Layout is CSS-owned: density is `body.density-compact`, the slot layouts are `max-width`,
`max-height: 420px` and `max-aspect-ratio: 3/2` media queries in `styles/sidecrab.css` (the slot
names in `DEV.md` such as 840x696 and 416x696 are labels for those combinations, not literals in the
CSS), and `gridCapacity()` reads the computed track counts, so JS never knows the row count. One
`--layout-unit: 1vmin` baseline; no component selector uses a raw viewport unit. The accent default
is stated in three places (`:root`, the `accentColor` meta, the `strProp` fallback in
`applyProperties`) and the JS one wins.

`tests/test_ordering.js` loads the shipping file whole into a `vm` context with a `document` stub
whose `readyState` is `loading`, then reads top-level names off the context. If it stops loading,
new top-level work in `sidecrab.js` needs a stub; never fork the logic. It carries a deliberate
mutant clamp and asserts that it fails.

`DEV.md` is the measured-evidence log, not just a flag table: a layout or threshold change is
expected to land there with the number it was measured at, at a named slot.

Version: `manifest.json` is the only machine-read version; the `vX.Y.Z` tags in the JS, CSS and HTML
are provenance comments that get one sweep per release.

### Notifier (`notifier/sidecrab_toast.py`)

Polls `/v1/state` every 10 s. Everything above `PowerShellToastAdapter` is pure; the `ToastAdapter`
protocol is the seam and `RecordingToastAdapter` serves both tests and `--dry-run`. Six deciders
(waiting, approval, longrun, digest, budget, outage) are injected into `Notifier`;
`StaleFeedDecider` runs first, ahead of every early return. Quiet hours suppress and mark; snooze
defers. That asymmetry is the feature. `Notifier._emit` is the single global-mute point. The
approval toast has no Approve/Deny buttons on purpose. `SUPPORTED_SCHEMAS` is an explicit set, and a
stale set means a silent standstill. The only file written is `~/.sidecrab/toast-state.json`, via
`DayLedger` subclasses keyed by `SECTION` with PID-tagged temp files because the snooze handler is a
second process. `sidecrab_ack_handler.pyw` POSTs an ack to `/v1/action`;
`sidecrab_snooze_handler.pyw` never touches crabd. Neither imports `sidecrab_toast`; their constants
are pinned by tests. PowerShell 5.1 from System32 is pinned for emission because pwsh 7 lacks the
WinRT projection.

### Hooks (`hooks/`)

Five fire-and-forget `command` hooks (`curl.exe -s -m 2 ... || exit 0`) and two `type: "http"`
hooks: Stop to `/v1/hook/stop` (crabd feeds a queued continue prompt back as `additionalContext`)
and PermissionRequest to `/v1/hook/permission` (60 s timeout, past crabd's 55 s poll). The split
exists because Claude Code skips HTTP hooks for SessionStart. If the operator has set
`allowedHttpHookUrls` in their Claude Code settings it must include `http://127.0.0.1:2722/*`, or
both HTTP hooks are blocked outright and approvals silently do nothing. No PreToolUse/PostToolUse,
deliberately. `sidecrab_statusline.py` is the `statusLine` command, not a hook: it POSTs stdin to
`/v1/statusline` then chains to the operator's prior command saved in
`~/.sidecrab/statusline-chain.json`. The installer matches SideCrab's entries on the substring
`127.0.0.1:2722/v1/hook` across both `command` and `url`.

### Setup (`setup/`, PowerShell 7)

Every script dot-sources `SideCrab.Common.ps1`, which defines about sixty functions and runs nothing
at load. Pure decision helpers (`Get-SideCrabComponentSpec`, `Select-SideCrabComponent`,
`Get-SideCrabTaskEnableDecision`, ...) are separated from thin impure wrappers; the Pester suite
AST-parses the scripts, lifts the pure functions with `Import-AstFunction`, injects scriptblocks for
the impure paths, and asserts some invariants against source text. Adding a component is one row in
`Get-SideCrabComponentSpec`. Tasks are `SideCrab-crabd`, `SideCrab-glow`, `SideCrab-toast`; registry
writes are HKCU only and nothing needs elevation. A task the operator disabled is re-registered
disabled and not started; `-ForceEnable` is the only override. `Install-SideCrab.ps1 -Status` is
read-only, `-PairingCode` prints the token, `-LimitsToken` stores a DPAPI-protected long-lived
token. Every write to `settings.json` or `config.json` is backed up first and the config write is
atomic (same-volume temp plus rename).

### Lighting (`lighting/`)

Parked. The real cause was a use-after-free in the lifetime of Corsair's `CueSdk.connect()` callback
thunk, nothing to do with consoles or pythonw despite the README and CHANGELOG framing;
`IcueAdapter.connect` holds the fix, and `CueSdk.disconnect()` must never be called (it hard-crashes
the interpreter). The task ships disabled and the installer will not re-enable it.
`decision.decide(doc, now)` is pure; `icue.IcueAdapter` / `NullAdapter` is the only SDK seam; tests
swap a fake `cuesdk` into `sys.modules`. `cuesdk==4.0.84` is the one pinned dependency.

## Conventions when changing things

- Contract first: `docs/STATE-CONTRACT.md`, then crabd, then the widget. Update `README.md` for
  user-facing changes and `CHANGELOG.md` for anything a user would notice.
- Bump the version of the component you touched: `widget/manifest.json`, `VERSION` in
  `companion/crabd.py`, `__version__` in `notifier/sidecrab_toast.py`. Update the `## Current` table
  in `CHANGELOG.md` and add a `- **<version> <component> (YYYY-MM-DD)** - ...` line under the
  highlights.
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
