# Getting started with SideCrab

A first-time walkthrough, from nothing installed to a crab on your Xeneon Edge that knows what
your Claude Code sessions are doing. Budget about 20 minutes. Every step says what you should
see, so you know when it worked.

If you already know the pieces, the [README](../README.md) is the reference; this page is the
tour.

---

## 0. What you are installing

Three things, and one command installs all of them:

1. **The companion (`crabd`).** A small background service on the PC where you use Claude Code.
   It reads what Claude Code is doing and serves it, and the panel page itself, over `127.0.0.1`
   only. Nothing leaves your machine.
2. **The panel host.** A window of SideCrab's own that shows the panel full-screen on the
   Xeneon Edge: the crab, the clock, your sessions and limits.
3. **The notifier.** Windows toasts when a session has been waiting on you.

SideCrab needs no other desktop software. If you are coming from the old widget, read the
[install notes](UPGRADING-TO-STANDALONE.md).

---

## 1. Check you have what it needs

Open **PowerShell 7** (the app is called "PowerShell 7", not "Windows PowerShell") and run each
line. The expected answer is beside it.

| Check | Run | You want |
|---|---|---|
| Windows | `[Environment]::OSVersion.Version` | Major version 10 (Windows 10 or 11) |
| Claude Code | `claude --version` | A version number. If it says "not recognized", install Claude Code first and sign in once |
| PowerShell 7 | `$PSVersionTable.PSVersion` | 7.x |
| Python | `python --version` | `Python 3.13.x`. If a Microsoft Store window opens instead, you have the Store alias, not Python: install from python.org and tick "Add python.exe to PATH" |
| Git | `git --version` | Any version |
| .NET 10 SDK | `dotnet --version` | `10.0.x`. Builds the panel host once; install from dotnet.microsoft.com. Without it, install with `-SkipPanel` and you get the companion and the notifier |
| WebView2 Runtime | `(Get-ItemProperty 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}' -Name pv).pv` | A version. It ships with Windows 11 and most Windows 10 installs; otherwise install the Evergreen WebView2 Runtime from Microsoft. The panel host is a WebView2 window |

Not on Windows? SideCrab cannot run: the companion is a Windows service and the host is a Windows
window; there is no other build.

---

## 2. Install (15 minutes)

In PowerShell 7:

```powershell
git clone https://github.com/Dixie-sketch/Clawdeck.git C:\Dev\sidecrab
cd C:\Dev\sidecrab
pwsh -File .\setup\Install-SideCrab.ps1
```

All three components are installed by default. Add `-SkipPanel` or `-SkipToast` to leave one out,
and `-WhatIf` to see every step without performing any of it.

The installer prints one line per thing it does. It:

- builds the panel host, `panel-host\dist\SideCrab.Panel.exe` (the .NET 10 SDK does the work,
  once). If the build fails it says so and carries on with the other two,
- registers a Scheduled Task per component that starts it at logon, and starts it now,
- backs up `~/.claude/settings.json`, then adds the SideCrab hooks to it. Your other hooks are
  untouched, and running the installer twice never duplicates anything,
- registers the notifier's identity, under your user account only. No admin prompt,
- leaves **panel approvals** off. Section 6 covers turning them on,
- unregisters the `SideCrab-glow` task if an old install left one, and records that in
  `~/.sidecrab/state/retired.json`. Its log is kept.

Then check the result:

```powershell
pwsh -File .\setup\Install-SideCrab.ps1 -Status
pwsh -File .\setup\Test-SideCrab.ps1
```

**What you should see:** `-Status` shows each task Running and health `ok`, then one line with
the three versions - companion, panel assets and panel host - and the approvals readiness the
companion itself reports. The smoke test prints a table with every row PASS. A FAIL row names
what is wrong and what to do.

---

## 3. The panel on the Edge

Section 2 already built and started the panel host, so there is nothing more to run.

**What you should see:** within a few seconds the panel fills the Xeneon Edge: the crab, the clock,
and your sessions once you start one. The window has no border, sits above everything on that
screen, never takes focus from what you are typing, and comes back by itself after the display
sleeps or the resolution changes. If something else is still drawing its own dashboard on the
Edge, turn that dashboard off for the Edge wherever you set it up. If the Edge instead shows
"SideCrab companion not reachable", the companion is down: run `Update-SideCrab.ps1`; the host
retries every 5 seconds.

If the panel is nowhere, read `~/.sidecrab/logs/panel.log`. It lists every display the host saw,
which one it picked, the viewport it measured, and every period it spent hidden because the
target display was absent.

Once the panel is on the Edge, tap the **gear beside the clock**. That is where the panel's
colours, the clock format, the chime and the rest live, and it writes them to
`~/.sidecrab/panel-settings.json` for you (the README lists the keys if you prefer the file). Try
**Test chime** first: if you hear nothing, the page is not running in the panel host, and
`~/.sidecrab/logs/panel.log` says what it loaded. Temperatures need one more thing, HWiNFO;
section 3b.

---

## 3b. Temperatures: HWiNFO (optional)

The companion reads hardware sensors from **HWiNFO**, a separate, free download from `hwinfo.com`
(free for non-commercial use; the Pro licence covers commercial use). Install it, then:

1. Open HWiNFO. In **Settings**, on the **General / User Interface** tab, tick **Shared Memory
   Support**, **Sensors-only**, **Minimize Main Window on Startup** and **Minimize Sensors on
   Startup**, then OK.
2. Click **Sensors** so the Sensors window opens. **The shared memory exists only while that window
   is open.** Minimised counts; closed does not.

HWiNFO needs administrator rights for its kernel driver, so it runs elevated. The companion only
reads what it publishes; it never writes to it.

**What you should see:** once HWiNFO finishes its first sensor scan (a minute or two on a
well-populated PC) the hardware row shows a CPU temperature with its sensor's name, and tapping the row lists the VRM, the drives, the fans and the rest. The
graphics card needs nothing: wherever an NVIDIA driver is installed, `nvidia-smi` is there and the
card's temperature, utilisation, VRAM and power appear on their own.

**The free build stops publishing about twelve hours after it starts.** It keeps running and keeps
showing you readings; it simply stops sharing them. The panel notices: the readings dim and the host
sheet says *"HWiNFO stopped publishing (free build 12-hour limit): relaunch HWiNFO"*. To stop
thinking about it, register the relaunch task from an **elevated** PowerShell 7:

```powershell
pwsh -File .\setup\Register-HwinfoRelaunch.ps1
```

It creates one scheduled task, `SideCrab-hwinfo`, that starts HWiNFO at logon and relaunches it
daily at 04:00, so the twelve hours begin afresh while you are asleep. `-WhatIf` shows what it
would do, `-Remove` unregisters it. The Pro licence removes the need for it entirely.

---

## 4. Your first session

Open a terminal, `cd` into any project, and run `claude`. Ask it anything.

**What you should see:** within a few seconds a card appears on the panel with the session's
title, the repo name, and a WORKING state. The two LIMITS gauges fill in with your current usage
and reset times. When the session finishes its turn, the card turns DONE; when it asks you a
question, the card turns to NEEDS INPUT and the crab perks up.

The Sessions zone has four views, and the chips at the right of its header switch between them:
the **session cards**, a **Burn** page with today's tokens by session, by model and by hour, a
**Week** strip you can tap a day of to read its history, and a **Detail** page that shows one
session in full, with the whole question, the whole permission request and its Approve and Deny
buttons, every subagent and every event. A swipe across the header row steps through them, and the
panel remembers which one you were on. You will not miss a question while you are reading another
view: if a session starts waiting, the Sessions chip grows a pulsing count. The panel never
switches views for you.

If no card appears, the hooks are not reaching the companion. Run `Test-SideCrab.ps1` and look
at the hook rows.

---

## 4b. Keep the limit gauges alive (two commands, once)

The LIMITS gauges read Claude Code's own sign-in token. It lives about six hours and is rewritten
only when a terminal `claude` makes a request, so if you mostly use the desktop app the gauges show
"token expired" by the next morning. Fix it once:

```powershell
claude setup-token
pwsh -File .\setup\Install-SideCrab.ps1 -LimitsToken
```

The first command opens a browser sign-in and prints a long-lived token; paste it into the
second. It is stored encrypted for your Windows account, and used only when the short-lived one
has expired.

## 5. Make it yours (optional)

All of these live in `~/.sidecrab/config.json`, and most are also on the panel's settings sheet.
The file is created for you; every key is optional.

```jsonc
{
  "quietHours": { "start": "22:00", "end": "07:00" },  // dim the panel, no toasts, no chime
  "toast":  { "enabled": true, "thresholdSec": 120 },  // toast after a session waits this long
  "digest": { "enabled": true, "time": "09:00" },      // one "yesterday" summary toast a day
  "budget": { "dailyOutputTokens": 5000000 },          // a daily token budget marker and toast
  "continuePrompts": ["Continue", "Run the tests"],    // extra next-step buttons on every card
  "continuePromptsByRepo": {                           // and per repo, for the work that repo needs
    "acme-api": [
      "Rebuild the report",
      "Run the migrations",
      "Check the seed data",
      "Roll the staging deploy back",
      "Write the release note"
    ]
  },
  "continuePromptsByPath": {                           // or per folder, for anything git cannot name
    "C:\\Work\\acme-api-lane-b": ["Fold the lane in"]
  },
  "recapRepos": ["C:\\Dev\\my-project"]                // repos whose commits count in the recap
}
```

Tap a working or finished session and the sheet offers next steps: Continue, Run the tests and
Commit + push to start with. `continuePrompts` adds buttons to every session. `continuePromptsByRepo`
adds them to one repo only, so the five buttons above appear on `acme-api` sessions and nowhere else,
and you can give each project the words you use on it. The key is the repo name printed under the
session title. `continuePromptsByPath` does the same for a folder, which is what to reach for when two
checkouts of one repo need different buttons, or when the folder is not a repo.

Each string is both the face of the button and the instruction that is sent to the session, so write
it as an instruction: "Run the migrations", not "migrations". Nothing else can be sent from the panel,
and a button only works on the project you configured it for.

The moon button beside the clock is quiet hours on the glass: tap for an hour of quiet, tap again
to stay awake through tonight's window, tap again to go back to the schedule.

---

## 6. Approving permission requests from the panel (optional, read first)

When a Claude Code session stops to ask permission for a tool call, the card can show Approve
and Deny buttons, and a tap decides it. This is off by default because it is a real security
control, and it needs a one-time pairing so that only your panel, not a web page you happen to
visit, can decide.

1. There is nothing to pair by hand. The companion mints a pairing code into
   `~/.sidecrab/panel-token` on first start and the panel host reads it itself. To look at it:

   ```powershell
   pwsh -File .\setup\Install-SideCrab.ps1 -PairingCode
   ```

2. Turn approvals on:

   ```powershell
   pwsh -File .\setup\Install-SideCrab.ps1 -WithApprovals
   ```

3. Check that the gate is really armed. `Install-SideCrab.ps1 -Status` prints the readiness the
   companion itself reports: `off`, `no-token`, `unverified` or `ready`. Only `ready` means a tap
   would be honoured.

4. Prove it on a throwaway session before you trust it. Open `claude` in an empty folder, ask
   it to run a command your settings do not pre-allow, and when the card shows the request,
   tap Approve. The command should run with no dialog in the terminal. Then do one Deny and
   confirm the command does not run. `setup\Verify-PanelApproval.ps1` walks through this with
   the exact commands.

What to expect while it is on: a request waits on the panel for up to 55 seconds. If you do not
tap, or the companion is down, or the code is wrong, the normal terminal dialog appears and
decides, exactly as if SideCrab were not installed. The README's "Before you turn on panel
approvals" section has the full guarantees.

---

## 7. Everyday use

| Do this | To |
|---|---|
| Tap a card | Read its question or its last event |
| Swipe a card | Acknowledge or dismiss it |
| Press and hold a card | Pin it to the front |
| Tap the crab, or two-finger tap anywhere | Acknowledge everything at once |
| Tap a card that has stopped | Send it a next step: Continue, Run the tests, or your own |
| Tap a limit gauge | See when the window resets and when your current pace would fill it |
| Tap a day in the week strip | Drill into that day |
| Pull down from the top | Refresh now |
| Tap the gear beside the clock | The panel's settings: clock format, colours, the chime, Test chime |
| Tap the hardware row | Ten minutes of CPU, memory, GPU and disk, plus every other sensor |
| Read the last line of a working card | The tool it is running and what for, with the call count this turn; a PLAN or BYPASS chip shows the session's permission mode; a thin line with 3/7 is its task list |
| Tap **Sessions**, **Burn**, **Week** or **Detail** at the top right | Switch the view; a swipe across that header steps through them |
| Tap **History** beside those chips | Today's events as a timeline, with the week strip to drill another day |
| Tap **Cancel** beside a queued step | Withdraw it; the panel says if the session already took it |
| Tap **Bring to front** in a card's sheet | Put that session's window in front on your main display |
| Tap the SideCrab icon in the notification area | Status, logs, reload, re-pin, pause, the display picker, quit until next logon |

The crab is the summary. Calm means nothing needs you. Alert with a glow means a session is
waiting. Worried and grey means the data is stale: the companion stopped, or the feed is older
than 30 seconds. The panel never shows old numbers as if they were fresh.

---

### The tray icon

The panel host puts an icon in the notification area of the display you work on. Its first line
says what the panel is doing. The menu opens a status window (display, page loaded, last failure,
restart policy, log path), the log folder, a reload, a re-pin, a pause, the display picker and a
quit until your next sign-in. Pick a display from **Show the panel on...**: it applies at once and
asks on your main display whether to keep it, and reverts by itself after ten seconds if you do not
answer.

### Settings on the glass, and cancelling a queued step

The gear opens two halves: this panel's own look and chime, saved by the panel host, and the
companion's quiet hours, toasts, digest and budget, sent to the companion. Only the values you change
are sent. A queued next step has a Cancel beside it; it tells you if the session already took it.

## 8. Updating and removing

```powershell
pwsh -File C:\Dev\sidecrab\setup\Update-SideCrab.ps1
```

That pulls the new code, rebuilds the panel host from it, restarts every registered task and then
verifies that the companion answers and that the tasks came back Running. **It exits non-zero if
any of that fails**, and names the host version still on disk, so a partly-failed update cannot
read as a success. There is nothing to import and nothing to update by hand.

To remove everything the installer added, including the hooks in `~/.claude/settings.json`:

```powershell
pwsh -File C:\Dev\sidecrab\setup\Uninstall-SideCrab.ps1
```

That removes wiring and keeps your data, then prints what it left behind. Add `-Purge` to delete
`~/.sidecrab` as well: your settings, your history, **and the two credential files** - the
approval pairing code and the stored limits token. Backups of `settings.json` survive either way.

---

## 9. When something is wrong

| You see | Try |
|---|---|
| Blank panel, no crab | `~/.sidecrab/logs/panel.log` says what the host loaded; then open an issue with that log |
| Worried grey crab, "data as of HH:MM" | `Install-SideCrab.ps1 -Status`, then `Update-SideCrab.ps1` to restart the task |
| No session cards | `Test-SideCrab.ps1`; check `~/.claude/settings.json` still has the SideCrab hooks |
| Gauges show a dash and "token expired" | The CLI token lives ~6 h. Store a long-lived one: `claude setup-token`, then `Install-SideCrab.ps1 -LimitsToken` (README, "Keeping the limit gauges alive") |
| Temperatures frozen or wrong | HWiNFO is publishing a stale reading; the row names the sensor it reads and dims a stale one. Relaunch HWiNFO |
| The Edge says "SideCrab companion not reachable" | `Update-SideCrab.ps1`; the panel host retries on its own every 5 s |
| The panel host is on the wrong screen, or not showing | `~/.sidecrab/logs/panel.log` lists every display it saw; set `display.deviceId` in `panel-settings.json` |
| "not paired" or "pairing code wrong" on Approve | The host did not read the code. `Install-SideCrab.ps1 -Status` shows the readiness; `~/.sidecrab/logs/panel.log` shows what the host loaded |
| Gauges dark, "limits token rejected" | The stored long-lived token is not accepted. Mint a fresh one: `claude setup-token`, then `Install-SideCrab.ps1 -LimitsToken` |
| Bring to front says "no window found for this session" | The host looks at your main display only. A session in the Claude desktop app answers "brought the Claude app to the front": pick the session in its sidebar |
| The panel is on the wrong screen | Tap the SideCrab icon in the notification area and use **Show the panel on...**; it reverts by itself after ten seconds if you do not keep the choice |
| The companion is running but something is off | `~/.sidecrab/logs/crabd.log` has a line for everything it worked around and the full detail of anything that failed; the smoke test's `crabd log` row reads its age |
| The panel is not where you expect | `.\panel-host\dist\SideCrab.Panel.exe --check \| Out-String` prints every display, the one the host would pick and why, and each problem it sees |
| Anything else | `pwsh -File .\setup\Test-SideCrab.ps1` prints a PASS/FAIL row for every piece |

Still stuck? [Open an issue](https://github.com/Dixie-sketch/Clawdeck/issues) with the smoke-test
table. Please do not paste anything from `~/.claude`; it holds your session transcripts.
