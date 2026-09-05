# SideCrab 🦀

[![CI](https://github.com/Dixie-sketch/Clawdeck/actions/workflows/ci.yml/badge.svg)](https://github.com/Dixie-sketch/Clawdeck/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![macOS](https://img.shields.io/badge/platform-macOS-000000.svg)](#what-you-need)

**An ambient Claude Code status panel for any browser on your Mac, served by a small local
service.**

> **New here?** Read [**Getting started**](docs/GETTING-STARTED.md): a 20-minute walkthrough from
> nothing installed to a working panel, with what you should see at each step.

SideCrab turns a browser window - a spare monitor, a second Space, a tab you leave open - into a
live view of every Claude Code session on your Mac. Session cards, rate-limit gauges with a reset
countdown, today's token burn, a clock, a "needs your attention" alert, and a pixel crab whose
mood *is* the status. When a session stops to ask you something, you find out from across the
room instead of by cycling through terminal windows. Then you can answer it from the panel.

![The panel](store/shots/01-panel.png)

---

## What you need

| | Required | Notes |
|---|---|---|
| **macOS** | Built and measured on **macOS 26.6, Apple silicon** | Older macOS versions are **untested rather than unsupported**: every measurement in these docs was taken on that one machine, and no other release has been tried. |
| **Python 3.13 or newer** | From **Homebrew** (`brew install python@3.13`) or python.org | Apple's `/usr/bin/python3` is 3.9 and is **refused by version**, not by path. The installer probes `$SIDECRAB_PYTHON`, then `python3.14`, `python3.13`, `python3` across `PATH`, `/opt/homebrew/bin` and `/usr/local/bin`. |
| **Claude Code** | Installed and used on the **same Mac** | The companion reads Claude Code's local session data. It cannot see sessions on other machines. |
| **A browser** | Any modern one | The live panel was measured in **Chromium** only (Playwright, 2560x720, no console errors); Safari was opened on the same address but nothing was recorded in it, and the Chrome application itself was not exercised. An iPad or a phone on the same network needs a tunnel you set up yourself; SideCrab does not open one, and does not listen anywhere a tunnel could reach without you. |

Everything runs on one Mac and talks only over `127.0.0.1:9999`. Nothing is sent anywhere.

---

## How it works

```
Claude Code hooks ──POST──▶  crabd (127.0.0.1:9999)  ──serves──▶ the panel at http://localhost:9999
~/.claude usage + JSONL ──▶  one /v1/state JSON feed  ◀──poll──  your browser (every 3 s)
                             + blocking hook answers  ──taps──▶  /v1/action · /v1/config
                                                      ◀──poll──  notifier (macOS notifications)
```

1. **Claude Code tells crabd what is happening.** The installer adds a few *hooks* to your
   `~/.claude/settings.json`. Each one is a tiny localhost POST that fires when a session starts,
   when you submit a prompt, when a session stops, and when it needs your attention. They time out
   in two seconds and never block Claude Code if crabd is not running. Every one carries the
   `X-SideCrab-Panel` header crabd requires on writes.
2. **crabd keeps the picture.** It turns those events into per-session state (working, waiting,
   finished, needs input), reads your rate limits from the same local credential Claude Code
   uses - on this Mac that is the login Keychain item `Claude Code-credentials`, not a file - and
   reads the session transcripts read-only for token burn and elapsed time. It serves all of that
   as one JSON document on `http://127.0.0.1:9999/v1/state`.
3. **crabd also serves the panel.** `http://localhost:9999` *is* the panel: the same
   HTML, CSS and JS that the iCUE widget is packaged from, served over http from one directory.
   Open it in a browser and leave it open.
4. **The panel draws it.** Every three seconds it polls the same origin it was served from and
   repaints. The crab's posture is the summary: calm when all is well, alert when something waits
   on you, worried when the feed is stale or gone.
5. **Taps go back the same way.** Acknowledge, dismiss, pin, "Continue", and (if you turn it on)
   approve or deny are same-origin POSTs to crabd. Nothing free-text is ever sent to a session.

**What the companion reads:** `~/.claude` (session transcripts, hook payloads) and the Claude Code
credential from your login Keychain. **What it never does:** write to `~/.claude`, log or transmit
your OAuth token, listen on a network interface, or send anything off the machine. The only
outbound call is the usage-limit check to Anthropic's API with your own token, the same call
Claude Code makes.

**The panel is honest about not knowing.** A stopped companion or a feed older than 30 seconds
produces a worried crab and a "data as of HH:MM" banner. Unknown values render as an em-dash,
never as zero. A green-looking panel always means the data is fresh.

---

## Install

Open Terminal on the Mac where you run Claude Code:

```sh
git clone https://github.com/Dixie-sketch/Clawdeck.git ~/SideCrab
cd ~/SideCrab
./setup/install.sh --with-toast
```

`--with-toast` also installs the notifier, which raises a macOS notification when a session has
been waiting on you for a while. Leave it off if you do not want notifications.

The installer prints one line per thing it does. In order:

1. **Refuses before it writes anything** if something else is already holding port 9999, naming
   the PID. An install that merged your hooks and then gave up would leave you half configured.
2. **Finds a Python 3.13 or newer** by probing each candidate for its version, and writes the
   absolute path it settles on into the LaunchAgent plists - an agent does not inherit your login
   `PATH`.
3. **Backs up `~/.claude/settings.json`** to `<path>.sidecrab-bak-YYYYMMDD-HHMMSS`, then merges
   the SideCrab hook entries into it at entry level. Your other hooks are left alone, running it
   twice produces byte-identical JSON, and a `settings.json` that does not parse aborts the whole
   install before anything is written.
4. **Takes the `statusLine` slot**, saving whatever was there to
   `~/.sidecrab/statusline-chain.json` so it still runs and `uninstall.sh` can put it back.
5. **Asks whether to enable panel approvals**, printing the three guarantees first. Answer no
   until you have read [Before you turn on panel approvals](#before-you-turn-on-panel-approvals).
   The default writes `false` only when the key is absent, so a re-run never reverts a choice you
   made earlier.
6. **Writes and loads the LaunchAgents**: `com.sidecrab.crabd` always, `com.sidecrab.toast` with
   `--with-toast`. Plists at `~/Library/LaunchAgents/<label>.plist`, logs at
   `~/.sidecrab/logs/<label>.log` in a directory with mode 0700, because the log carries session
   titles and repo paths. Both carry `RunAtLoad` and `KeepAlive`, so **crabd starts when you log
   in and launchd restarts it if it dies** - you do not start it by hand and there is nothing to
   remember after a reboot. An agent you disabled yourself has its plist refreshed and is **not**
   started; `--force-enable` is the only override.

The three wrappers work out their own directory, so `./setup/install.sh` from inside the checkout
and `~/SideCrab/setup/install.sh` from anywhere else are the same command.

Then check it:

```sh
./setup/install.sh --status   # read-only: what is installed, wired and answering
./setup/install.sh --doctor   # PASS/FAIL over the whole chain a session travels
```

`--status` writes nothing at all. `--doctor` is **not** read-only: it posts a real SessionStart /
Notification / SessionEnd cycle for the session id `smoke-test` to prove the write path end to
end, and probes the header gate by POSTing without `X-SideCrab-Panel` and expecting the 403. The
cycle clears its own row, but crabd persists every hook event, so a run leaves three rows in that
day's history.

Now open **<http://localhost:9999>**. Start a Claude Code session; within a few seconds a card
for it appears.

### The one-time Keychain prompt

Claude Code keeps its credential in your login Keychain (service `Claude Code-credentials`), and
a process that is not on that item's access list gets a macOS dialog the first time it reads it.
crabd reads it through `/usr/bin/security`, so expect **one prompt shortly after the first
install**, naming that tool and that item. Choose **Always Allow** and it does not come back.

A refused read is treated as "no credential", never as a guess: the gauges go dark and the panel
says the Keychain refused, pointing at the prompt rather than telling you to log in again. The
long-lived token you store yourself (below) is a different item and does not prompt - items
created through the `security` tool carry that tool in their own access list.

### If you have set `allowedHttpHookUrls`

Two of the hooks are `type: "http"`, and if any of your Claude Code settings files defines
`allowedHttpHookUrls`, only matching URLs run - the rest are blocked silently, which would make
panel approvals do nothing at all. The installer adds both patterns when the key already exists
and **never creates it**, because creating it would switch the allowlist on and block every other
http hook you have. List both host forms; patterns match the URL as written:

```json
"allowedHttpHookUrls": ["http://127.0.0.1:9999/*", "http://localhost:9999/*"]
```

### Keeping the limit gauges alive (recommended)

The gauges read the same OAuth token Claude Code uses. That token lives about six hours and is
only rewritten when a terminal `claude` session makes an API call, so on a machine where you
mostly use the desktop app the gauges go dark by the next morning. Fix it once with a long-lived
token:

```sh
claude setup-token                 # opens a browser sign-in, prints a token
./setup/install.sh --limits-token  # paste it; stored in your login Keychain
```

The token is read from **stdin**, never from a command-line argument, so it never appears in `ps`
- which is world-readable on a Mac. It is stored as the login-Keychain item `SideCrab limits
token`, used only when the CLI's own token has expired, read fresh on each poll, and never
logged and never served. `--status` shows whether one is stored, never its value.

### Updating, uninstalling

```sh
git -C ~/SideCrab pull
~/SideCrab/setup/update.sh      # refreshes the plists from this checkout and restarts crabd
~/SideCrab/setup/uninstall.sh   # removes the agents, the hooks and the status line
```

`update.sh` refuses to restart over a port held by something else, and names the PID rather than
starting blind. The panel updates with the same pull, because crabd serves it from this checkout
- reload the tab.

`uninstall.sh` removes what SideCrab wrote and keeps your data: `~/.sidecrab` and every backup
survive. `uninstall.sh --purge` deletes `~/.sidecrab` too, after telling you exactly what is in
it and asking.

**There is no notifier-only removal.** Re-running the installer *without* `--with-toast` does not
unload the notifier - the flag only ever adds it - and `uninstall.sh` removes everything. To drop
just the notifier and keep the panel:

```sh
launchctl bootout gui/$(id -u)/com.sidecrab.toast
rm ~/Library/LaunchAgents/com.sidecrab.toast.plist
```

---

## Using it

### At a glance

- **The crab** is the summary. Calm = nothing needs you. Alert with a glow = a session is
  waiting. Worried and grey = the data is stale or the companion is gone. It sweats when a limit
  is nearly full, and it has a few tricks it does on its own.
- **Session cards** show state, model, elapsed time, the repo it is working in, a hairline for how
  full that session's context window is, and a "queued: …" line when you have sent it a next step.
- **Limit gauges** show each rate-limit window, how full it is, when it resets, and a forecast of
  when the recent burn rate would fill it.
- **TODAY** shows token burn with a sparkline, the daily budget if you set one, and cost when
  Claude Code's telemetry is flowing to the companion.
- **The week strip** is the daily recap: sessions, commits in your configured repos, tokens.
- **The hardware row** shows this Mac's CPU and memory use while the companion runs. There are no
  temperatures on macOS - see [Known caveats](#known-caveats).

### The settings sheet

The gear beside the filter chips, or press `s`. Everything the panel can configure is there:
quiet hours, the notification switch and thresholds, the daily budget, the crab style and accent
colour, and the Approval Pairing Code. Settings are kept in this browser, on the panel's own
address, so a different browser is a different set.

### Pointer

| Do this | Result |
|---|---|
| **Click** a card | Opens its detail sheet: the question it is asking, subagents, the last event |
| **Drag** a card sideways | Acknowledge or dismiss it |
| **Press and hold** a card | Pin it to the front (again to unpin) |
| **Click the crab** | Acknowledge every waiting session at once |
| **Drag down** from the top edge | Refresh now |
| **Click a gauge** | That window's detail: how full, when it resets, when it would fill |
| **Click a day** in the week strip | Drill into that day; page with prev/next |
| **Click the moon** beside the clock | Quiet for an hour · stay awake through tonight's window · back to schedule |
| **Filter and density chips** (top right) | Show only waiting / working / finished; comfortable or compact cards |

### Touch

The same handlers, because the panel uses pointer events rather than touch events. On a
touchscreen the gestures are tap, swipe, long-press, pull-down - and one more:

| Gesture | Result |
|---|---|
| **Two-finger tap** anywhere | Acknowledge every waiting session at once |
| **Tap the crab** | The same action |

A two-finger tap needs two touch points, so it is not available from a mouse or a trackpad; the
crab and the `a` key are the same command.

### Keyboard

| Key | Result |
|---|---|
| `a` | Acknowledge every waiting session |
| `p` | Pin the focused card (again to unpin) |
| `Delete` / `Backspace` | Dismiss the focused card |
| `r` | Refresh now |
| `s` | Open the settings sheet |
| `Escape` | Close the open sheet |
| `Tab` | Move between cards and controls; trapped inside an open sheet |
| `Enter` / `Space` | Activate what is focused |

Each key calls the same function its gesture calls. All of them are inert behind a modifier (so
Cmd-R still reloads and Cmd-S still saves), behind an autorepeat, while a sheet is open, and while
an input has focus - an `s` typed into the pairing code is a character, not a command.

### Sending a session its next step

On a stopped or finished session, open the card and pick a **continue prompt**: "Continue", "Run
the tests", "Commit + push", or any you add in the config file. It is delivered the next time that
session's Stop hook fires. The vocabulary is fixed on purpose. There is no free-text input on the
panel and no supported way to inject arbitrary text into a live session.

### Approving a permission request from the panel

When a session is waiting on a tool permission, the card shows the request with a countdown, and
you can approve or deny it from the panel. **This ships off.** Read the next section before you
turn it on. Turning it on is three steps:

```sh
./setup/install.sh --with-approvals   # 1. arm it
./setup/install.sh --pairing-code     # 2. print the code crabd minted
```

3. Open the panel's settings sheet and paste the code into **Approval Pairing Code**. A tap
   without the code, or with a wrong one, is refused and the terminal dialog keeps the decision,
   exactly as if the panel were not there.

The code is kept in this browser, on the panel's own address - which is a weaker guarantee than
the iCUE property it replaces, stated plainly in [`SECURITY.md`](SECURITY.md). Each browser you
open the panel in needs the code pasted once.

---

## Configuration

`~/.sidecrab/config.json`, all keys optional. Most of these are also editable from the panel's
settings sheet.

```jsonc
{
  "quietHours": { "start": "22:00", "end": "07:00" },  // dim panel, no glow, no notifications
  "toast":  { "enabled": true, "thresholdSec": 120, "approvalThresholdSec": 20 },
  "digest": { "enabled": false, "time": "09:00" },     // one "yesterday" notification per day
  "budget": { "dailyOutputTokens": 5000000 },          // null to clear; one alert on crossing
  "continuePrompts": ["Continue", "Run the tests"],    // extra taps on a stopped session
  "panelApprovals": { "enabled": false },              // approve/deny from the panel — see below
  "recapRepos": ["/Users/you/dev/my-project"]          // extra repos to count commits in
}
```

`continuePrompts` and `recapRepos` are hand-edited only. The panel reads them but does not write
them. The `toast` key keeps its name on the wire: a contract key is not a label.

---

## Before you turn on panel approvals

Approving a tool call from a browser window is a real security decision, so the guarantees are
worth reading rather than assuming:

- **It ships off.** The installer asks.
- **crabd never decides on its own.** There is no code path that answers "allow" without a
  `decide` request arriving on localhost first. The normal source of that request is a tap on
  the panel.
- **Only a paired panel can decide (crabd 0.29.0 / widget 0.27.0).** crabd mints a
  ten-character **pairing code** into `~/.sidecrab/panel-token` on first start, and every
  Approve or Deny must carry it. Ten wrong codes in a minute lock the gate for a minute. Each tap
  also names the exact request it saw (`requestId`), so a tap can never land on a request that
  replaced it. This closed the SEC-a and WID-a findings recorded in [`SECURITY.md`](SECURITY.md).
- **Three gates stand between a web page and your sessions (crabd 0.31.0).** A `Host` allowlist,
  so a site whose name re-resolves to 127.0.0.1 cannot pretend to be your panel; an Origin
  allowlist of crabd's own address, so a page you merely visit cannot read the feed; and
  `X-SideCrab-Panel` on every POST, which a cross-origin page cannot obtain permission to send.
- **Every failure is a pass-through.** Timeout, no tap, disabled, malformed, companion down: all
  return no decision, and the normal terminal dialog does its job. The worst case is the behaviour
  of a machine where SideCrab was never installed.
- **The notification has no buttons.** When a request goes undecided, the notifier tells you and
  says "Decide on the panel." A notification action is one click from a lock screen; that is fine
  for acknowledging a dot and not for allowing a command. macOS notifications posted this way
  carry no buttons anyway, but the rule came first.
- **Verified live, operator present (2026-08-27)** on the Windows build via
  `setup/Verify-PanelApproval.ps1`: a panel Approve ran the command with no keyboard, a panel Deny
  blocked it, and a full minute of ignoring both surfaces ended in the pass-through with the
  terminal dialog in charge. Two behaviours worth knowing: the terminal dialog is **raced, not
  suppressed** (whichever surface answers first wins), and the two-button card carries a real
  mis-tap risk. Prove it on a throwaway session on your own machine before trusting it.

---

## Troubleshooting

| You see | It means | Do |
|---|---|---|
| Worried grey crab, "data as of HH:MM" | The companion is stopped, or the feed is older than 30 s | `./setup/install.sh --status`, then `./setup/update.sh` to restart it |
| Panel is fine but no session cards | Hooks are not firing | Check `~/.claude/settings.json` has the SideCrab entries; re-run the installer, which merges them idempotently. If you have set `allowedHttpHookUrls`, check both host forms are in it |
| Limit gauges show an em-dash and "token expired" | The CLI's access token has passed its ~6 h life and nothing has refreshed it | Store a long-lived token once (above), or run any `claude` command in a terminal to refresh it |
| Gauges show an em-dash and a note about the Keychain | crabd asked for `Claude Code-credentials` and macOS refused this process | Approve the prompt (Always Allow) when it appears, or run `claude` in a terminal so the CLI writes a credential crabd can read without it |
| The browser cannot connect at all | crabd is not running - the panel is a page crabd serves, so there is nothing to show you a SideCrab error screen | `./setup/install.sh --status`, then `./setup/update.sh` |
| `crabd: cannot listen on 127.0.0.1:9999 - [Errno 48] Address already in use` | Something else holds the port. crabd stops loudly rather than moving to another one, because a crabd on a port nothing addresses is a silent dead panel | `lsof -nP -iTCP:9999 -sTCP:LISTEN`, then stop it. `CRABD_PORT` is the other way out, but read the note below first |
| "python3 is 3.9" or a refusal naming the version | Only Apple's `/usr/bin/python3` was found | `brew install python@3.13`, or set `$SIDECRAB_PYTHON` to a 3.13+ interpreter |
| A finished session still reads "working" | A session was killed by an app restart, so no end hook fired | It clears itself within 15 minutes; taps on it are refused rather than queued |
| Something else | | `./setup/install.sh --doctor` prints a PASS/FAIL row for every piece |

**About `CRABD_PORT`.** It moves crabd, and nothing else. The seven hook entries the installer
wrote into `~/.claude/settings.json` carry `127.0.0.1:9999` as a literal, as does the status-line
command, so moving the port means editing all of them by hand - and the installer's next run puts
9999 back. Freeing 9999 is nearly always the better answer; `CRABD_PORT` is for running a second
crabd beside the live one, which is what it was added for.

---

## Known caveats

- **No CPU or GPU temperatures on macOS.** No web page can read a die temperature, and there is
  no iCUE sensor plugin behind a browser, so the hardware row shows only what the companion can
  measure: this Mac's CPU and memory. An absent or all-null block takes the whole row off the
  glass rather than showing zeros.
- **Notifications appear under Script Editor's identity, carry no buttons, and stack.** They are
  posted through `osascript`, so macOS attributes them to Script Editor and the per-app
  notification switch is Script Editor's; the subtitle is always `SideCrab`, which is the only
  thing on screen naming the product. `display notification` has no action affordance and no
  replacement identifier, so a second outage notice sits beneath the first instead of replacing
  it. Acknowledge on the panel. Whether the notification **sound** is audible on your machine was
  not measured; it is `sound name "default"`. **If you dismissed the one-time permission prompt
  for Script Editor, alerts are lost silently** - the notifier logs the failure and re-arms, but
  nothing appears on screen. Turn it back on in System Settings > Notifications > Script Editor,
  then prove it with `python3 notifier/sidecrab_toast.py --test-toast`.
- **App Nap and timer coalescing do not throttle the feed here, measured.** Two minutes of
  sampling `/v1/state` against the live LaunchAgent gave 55 distinct snapshots, max gap 3.0 s,
  mean 2.19 s, none over 4 s, against a 2 s rebuild - so the plists carry no `ProcessType` key
  and the default scheduling stands. The reading was taken with the machine in normal use;
  battery with the lid shut is where a throttle would show, and that has not been measured.
  Recorded in [`docs/PORT-NOTES.md`](docs/PORT-NOTES.md).
- **The glow is gone on macOS.** The Corsair lighting helper is Windows-only, so there is no
  component to observe: `fleet.glow` reads `absent`, which is the truth rather than a placeholder,
  and crabd does not spawn anything looking for it.
- **The status-line feed is a fallback, not a replacement.** It fires only in an interactive
  terminal session. The credential-based limits path works regardless.
- **Cost figures need telemetry.** `costUSD` appears only when Claude Code's OTLP telemetry is
  flowing to the companion, which also means telling the exporter to send the panel header
  (`OTEL_EXPORTER_OTLP_HEADERS=X-SideCrab-Panel=1`). It is never estimated from token counts.

## Known issues

The honest list lives in [`docs/BACKLOG.md`](docs/BACKLOG.md). Worth knowing before you install:

- **The pairing code is per browser.** It lives in the browser you pasted it into, on the panel's
  own address. A second browser, or a cleared site data, needs it pasted again.
- **GHOST-a** - after a crabd restart, a session that was killed by an app restart can read
  `working` for up to 15 minutes before transcript aging retires it.
- About two dozen small cosmetic or edge-case items under "Small, known, not yet fixed".

## Security and privacy

Localhost-only by design. crabd binds `127.0.0.1` and nothing can widen it: there is no
`CRABD_HOST` and no config key that reaches the bind. It reads `~/.claude` read-only, never writes
there, and transmits nothing. There is no telemetry, no crash reporting, no update check.
Serving the panel on a real web origin is the new exposure the port introduces, and
[`SECURITY.md`](SECURITY.md) states plainly what the three gates do and do not buy, along with the
disclosed residuals and how to report a vulnerability.

---

## Windows and iCUE

SideCrab began as an iCUE widget for the Corsair Xeneon Edge, and **that build is still in the
tree and still packageable**. The panel is the same files: `widget/index.html` stays strict-XML
clean, `widget/manifest.json` still carries the widget version, and `icuewidget validate widget`
/ `icuewidget package widget` still work. CI still runs the Windows job. The PowerShell installer
and its Pester suite moved with the port rather than standing still: nine `.ps1` files changed,
all nine of them moving the port literal 2722 to 9999, and the three that POST live -
`Repair-SideCrab.ps1`, `Test-SideCrab.ps1` and `Verify-PanelApproval.ps1` - also send
`X-SideCrab-Panel`, which the Pester suite now expects in six places. **Upgrading an existing
Windows install leaves its 2722-era hook entries in `settings.json` behind**, because the
installer matches its own entries on the port: delete them by hand for now - see `UPG-a` in
[`docs/BACKLOG.md`](docs/BACKLOG.md).

What changed is which path is documented. macOS plus a browser is the one this README walks you
through; the Windows path is no longer the primary one and its instructions live in the history.
The last Windows-first release is **crabd 0.30.0 / widget 0.28.2** - see
[`CHANGELOG.md`](CHANGELOG.md) for what shipped in it and what the macOS port changed after.

---

## For developers

Want to contribute? Read [`CONTRIBUTING.md`](CONTRIBUTING.md) first: it is short, and it explains
the four rules every change is held to.

| Path | What |
|---|---|
| `widget/` | The panel: HTML/CSS/JS, no bundler. **Served by crabd** at `http://localhost:9999`, and still packageable as an iCUE widget from the same files. Dev notes in `widget/DEV.md` |
| `companion/` | **crabd**: hook receiver, session state machine, limits + burn reader, history, `/v1/state`, and the static panel route |
| `notifier/` | macOS notifications through `osascript` (and Windows toasts): waiting session, permission request, daily digest, budget crossed, companion gone quiet |
| `hooks/` | The two Claude Code hook fragments (macOS and Windows) and the status-line command that feed crabd |
| `setup/` | macOS: `install.sh` / `update.sh` / `uninstall.sh`, thin `sh` wrappers over `sidecrab_setup.py`. Windows: the PowerShell scripts, retained |
| `lighting/` | **sidecrab-glow**: Corsair RGB while a session waits. Parked, Windows-only, no macOS counterpart |
| `docs/` | [Getting started](docs/GETTING-STARTED.md) · [PORT-NOTES](docs/PORT-NOTES.md), the seams and measurements of the macOS port · [PRD](docs/PRD.md) · [STATE-CONTRACT](docs/STATE-CONTRACT.md), the producer/consumer API and the source of truth for both sides · [BACKLOG](docs/BACKLOG.md) · audit findings |

Design rules that drive most decisions: **honest failure** (unknown is `null` or an em-dash,
never `0`, never a stale value re-served), **every alert must survive a healthy night** (each
threshold is replayed against real data, each gate mutation-proven), **contract first** (`schema`
marks the last breaking shape; additive fields are detected by presence), and **a fixed
vocabulary, never free text**.

Tests, all headless. This is exactly what CI runs on macOS:

```sh
python3 -m unittest discover -s companion/tests -t companion/tests
python3 -m unittest discover -s notifier/tests -t notifier/tests
python3 -m unittest discover -s hooks/tests -t hooks/tests
python3 -m unittest discover -s setup/tests -t setup/tests
python3 -m unittest discover lighting/tests
node widget/tests/test_ordering.js
node widget/tests/test_panel.js
node --check widget/scripts/sidecrab.js
python3 -c "import xml.etree.ElementTree as ET; ET.fromstring(open('widget/index.html',encoding='utf-8').read()); print('strict-XML OK')"
python3 -c "import json; m=json.load(open('widget/manifest.json',encoding='utf-8')); print(m['id'], m['version'])"
```

The Pester suite (`pwsh -File setup/tests/RunTests.ps1`) is Windows-only: it asserts `C:\` paths
and calls DPAPI. It runs in the Windows CI job. Nothing else has an expected failure on macOS.

Running the pieces by hand: `python3 companion/crabd.py` takes no flags, `CRABD_PORT` runs a
second instance beside the live one, `CRABD_PANEL_DIR` points it at a different panel tree, and
`CRABD_CLAUDE_HOME` points it at a fake `~/.claude`. The notifier: `python3
notifier/sidecrab_toast.py --once --dry-run`, and the `--test-*` flags each fire one sample
without marking any ledger.

---

## License

MIT. See [`LICENSE`](LICENSE). SideCrab is an independent hobby project and is not affiliated with
or endorsed by Anthropic or Corsair; *Claude* and *Claude Code* are Anthropic's marks and *iCUE*
and *Xeneon* are Corsair's, named here only to say what the panel works with.
