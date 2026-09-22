# Install notes: SideCrab moved from the iCUE widget to a standalone panel

**Short version.** From 0.29.0 (2026-09-21) the recommended way to run SideCrab is the
**standalone panel host**: a small window of our own that shows the panel full-screen on the
Xeneon Edge, with iCUE nowhere in the loop. The iCUE widget still exists and still works on iCUE
builds **before 5.51.40**, but it is no longer the primary host and it cannot be made to work on
5.51.40 or newer.

If you are new, follow "Fresh install" below. If you have the widget today, follow "Upgrading
from the iCUE widget". Either way it is about ten minutes.

---

## Why this changed

iCUE 5.51.40 added a URL-permission layer for widgets. Every request the SideCrab widget makes to
the companion on `127.0.0.1` is refused inside iCUE (`net::ERR_ACCESS_DENIED`, then
`net::ERR_BLOCKED_BY_CLIENT` after the grant is rebuilt). A `permissions` entry in the widget
manifest works right after an import and dies on the next iCUE start, because iCUE saves the grant
without the port a loopback grant needs. Nothing in a widget package can change that. The symptom
is a panel that reads "data as of HH:MM" or shows no cards while the companion is perfectly
healthy.

So the panel now has a second host. The companion serves the same panel page itself at
`http://127.0.0.1:2722/panel/`, and `SideCrab.Panel`, a .NET 10 window using WebView2, shows it
on the Edge. One panel, one codebase, two hosts.

---

## What you need for the standalone panel

| | |
|---|---|
| Windows 10 or 11 | |
| Claude Code, used on this PC | The companion reads its local session data |
| PowerShell 7 and Python 3.13 | For the companion, unchanged |
| **.NET 10 SDK** | Builds the panel host once; `Install-SideCrab.ps1 -Panel` runs the build. The Desktop Runtime it installs runs the host. https://dotnet.microsoft.com/download |
| **WebView2 Runtime** | Ships with Windows 11 and most Windows 10 installs |
| The Xeneon Edge | Found by its device id. Any 2560x720 display works, and `panel-settings.json` can name another one |

iCUE is **not** required. If you have it, it keeps running your fans and lighting. Only its own
dashboard on the Edge must be turned off, or the two full-screen windows fight for the top.

---

## Fresh install (standalone)

```powershell
git clone https://github.com/Dixie-sketch/Clawdeck.git C:\Dev\sidecrab
cd C:\Dev\sidecrab
pwsh -File .\setup\Install-SideCrab.ps1 -WithToast -Panel
```

That one command installs the companion (`SideCrab-crabd`), the notifier (`SideCrab-toast`),
builds the panel host and registers it (`SideCrab-panel`), all as logon tasks, and starts them.

**What you should see:** within a few seconds the panel fills the Edge: the crab, the clock, the
limit gauges, and a card for each Claude Code session as you start them. Then check it:

```powershell
pwsh -File .\setup\Install-SideCrab.ps1 -Status
pwsh -File .\setup\Test-SideCrab.ps1
```

Every row of the smoke test should be PASS, including `task panel`, `panel route` and
`panel viewport` (the last one confirms the page is drawn at the Edge's real size).

Keep the limit gauges alive across the CLI token's six-hour life with a long-lived token, once:

```powershell
claude setup-token
pwsh -File .\setup\Install-SideCrab.ps1 -LimitsToken
```

---

## Upgrading from the iCUE widget

1. Pull and update the companion. The panel page is served from companion 0.31.0 on, so this step
   is required:

   ```powershell
   git -C C:\Dev\sidecrab pull
   pwsh -File C:\Dev\sidecrab\setup\Update-SideCrab.ps1
   ```

2. Build and start the panel host:

   ```powershell
   pwsh -File C:\Dev\sidecrab\setup\Install-SideCrab.ps1 -Panel
   ```

3. In iCUE, select the Xeneon Edge and turn its **dashboard off** for that screen. Leave iCUE
   running; fans and lighting are unaffected. Until you do this, iCUE's dashboard and the panel host
   are both "always on top" and take turns.
4. Remove the SideCrab widget from the Edge in iCUE if it is still there. On 5.51.40 or newer it
   can no longer reach the companion anyway.
5. Move your settings, if you had changed any:

   | Was in the widget's iCUE settings | Now lives in |
   |---|---|
   | Colours, 24-hour clock, flash on alert, crab accessories, transparency, touch diagnostics | `~/.sidecrab/panel-settings.json`, under `props`, same names (`clock24`, `alertFlash`, `crabStyle`, `textColor`, `accentColor`, `backgroundColor`, `transparency`, `touchDiag`) |
   | Quiet hours, toast, digest, budget | `~/.sidecrab/config.json`, where they already were; the host does not push these from a property sheet |
   | Approval pairing code | Read automatically from `~/.sidecrab/panel-token`. Nothing to paste |
   | CPU and GPU sensors | The companion, from HWiNFO's shared memory and `nvidia-smi`; see "Temperatures" below. Without HWiNFO the CPU cell hides itself; the GPU cell needs only the NVIDIA driver |

   A minimal `panel-settings.json`, every key optional:

   ```jsonc
   {
     "crabdPort": 2722,
     "display": { "deviceId": "CRXED00", "width": 2560, "height": 720 },
     "props": { "clock24": true }
   }
   ```

   Editing the file reloads the panel by itself.

---

## What is different day to day

- **Updating.** `git pull` then `Update-SideCrab.ps1`. It rebuilds the host from the pulled
  source and restarts it. There is no `.icuewidget` to import at the desk any more.
- **The window.** Borderless, always on top on the Edge, hidden from the taskbar and Alt+Tab, and
  it never takes keyboard focus, so a tap on the Edge does not interrupt what you are typing. It
  comes back on its own after the display sleeps or the resolution changes.
- **Touch.** The same gestures: tap, swipe, press and hold, two-finger tap, pull down to refresh.
- **When the companion is down.** The Edge shows a dark "SideCrab companion not reachable" page and
  the host retries every five seconds. Start the companion and the panel returns.
- **Logging.** `~/.sidecrab/logs/panel.log` records the display the host picked, the viewport it
  measured, and every re-pin and refused navigation.
- **Live updates.** The companion pushes each new picture over `GET /v1/events`; a question shows
  in about a tenth of a second instead of up to three. If the connection drops, the panel polls
  every three seconds again and keeps retrying the stream. Nothing to configure.
- **Settings on the glass.** The gear beside the clock opens a settings sheet; Save writes
  `panel-settings.json` through the host and applies it with no reload. The sheet never touches
  the port, the display or the pairing code.
- **The chime.** A short two-note chime when a session starts waiting: once per question, silent
  in quiet hours, never twice in five seconds. On after the upgrade; turn it off or set the volume
  in the sheet, or put `"chime": false` in the `props` block of `panel-settings.json`.
- **Temperatures.** Back, from HWiNFO and `nvidia-smi`; see the next section.
- **Four views, and a crab that moves.** The Sessions header has chips for Sessions, Burn, Week
  and Detail (or swipe across the header); a question arriving behind another view puts a pulsing
  count on the Sessions chip. The crab is drawn to a canvas now and breathes, sweeps, sweats and
  dances; he holds still in quiet hours and under reduced motion.
- **Bring a session to the front.** A card's sheet and the Detail page carry a **Bring to front**
  control that puts that session's window in front of you on your main display. The host works it
  out from the session's title, working directory and repository and enumerates your windows
  itself; the page never names one. If your sessions run in the Claude desktop app, expect
  "brought the Claude app to the front", because that app keeps every session in a single window;
  pick the session in its sidebar. Every attempt is written to `~/.sidecrab/logs/panel.log`. The
  panel never takes the keyboard, and nothing in it types into a session: a multiple-choice
  question is still answered in the session, by you.

---

## Temperatures: HWiNFO, and the twelve-hour relaunch task

Install HWiNFO from `hwinfo.com` (free for non-commercial use; the Pro licence covers commercial
use). In its **Settings**, General / User Interface tab, tick **Shared Memory Support**,
**Sensors-only**, **Minimize Main Window on Startup** and **Minimize Sensors on Startup**; then open
its **Sensors** window and leave it open (minimised is fine). The shared memory exists only while
that window runs. HWiNFO is elevated for its driver; the companion only reads. Once HWiNFO finishes
its first sensor scan (a minute or two) the row shows the CPU temperature with its sensor name; the host sheet lists the VRM, drives, board,
chipset, package power and fans. The graphics card needs nothing beyond its NVIDIA driver.

**The free build stops sharing about twelve hours after launch**; the panel dims the readings and
says *"HWiNFO stopped publishing (free build 12-hour limit): relaunch HWiNFO"*. From an elevated
PowerShell 7, `pwsh -File .\setup\Register-HwinfoRelaunch.ps1` registers `SideCrab-hwinfo`, a task
that starts HWiNFO at logon and relaunches it daily at 04:00 (`-WhatIf`, `-Remove`). The Pro licence
removes the need for it.

---

## Staying on the iCUE widget (iCUE 5.44 to 5.51.39)

The widget is still built and still released: download `SideCrab-<version>.icuewidget` from the
releases page and import it as before. It stops working the day iCUE updates itself past
5.51.39, and the companion cannot help with that. When it happens, come back to "Upgrading" above.

---

## Troubleshooting the standalone panel

| You see | Do |
|---|---|
| "SideCrab companion not reachable" on the Edge | `Update-SideCrab.ps1`. A `404` in that message means the companion is older than 0.31.0 or the `widget\` folder is missing beside `companion\` |
| Nothing on the Edge, no window | `~/.sidecrab/logs/panel.log` lists every display it saw and which it picked. Set `display.deviceId` (or `width`/`height`) in `panel-settings.json` |
| The panel looks cropped or scaled | `Test-SideCrab.ps1` has a `panel viewport` row; the host corrects its zoom on the next check. Set the Edge to 100% scale in Windows display settings if you can |
| Two dashboards flicker on the Edge | iCUE's own dashboard is still on for the Edge. Turn it off in iCUE |
| `-Panel` fails with "dotnet not found" or an SDK version error | Install the .NET 10 SDK and re-run |
| Approve or Deny says "not paired" | The host reads `~/.sidecrab/panel-token`; make sure the companion has started at least once (it mints the code) and restart `SideCrab-panel` |
| No temperatures; the host sheet names HWiNFO | Install HWiNFO, turn on Shared Memory Support, open its Sensors window; `Test-SideCrab.ps1` has a `sensors` row that says which source is missing |
| Temperatures dimmed with the 12-hour note | Relaunch HWiNFO, or register the `SideCrab-hwinfo` task |
| No chime | Quiet hours, the chime setting, or the page is not in the panel host: the gear's Test chime tells you which |

---

## Removing the standalone panel

```powershell
pwsh -File C:\Dev\sidecrab\setup\Uninstall-SideCrab.ps1 -TaskName SideCrab-panel   # just the host
pwsh -File C:\Dev\sidecrab\setup\Uninstall-SideCrab.ps1                            # everything
```

`panel-settings.json` is your data and stays unless you pass `-Purge`. The WebView2 profile lives
under `%LOCALAPPDATA%\SideCrab\Panel` and goes with `-Purge` too.
