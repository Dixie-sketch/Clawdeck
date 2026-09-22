# Install notes: the standalone panel

**Short version.** SideCrab is a standalone application. The companion serves the panel page at
`http://127.0.0.1:2722/panel/`, and `SideCrab.Panel`, a .NET 10 window using WebView2, shows it
full-screen on the Xeneon Edge. Nothing else is needed and nothing is imported anywhere.

If you are new, follow "Fresh install" below. If you ran the old widget, follow "Upgrading from
the widget". Either way it is about fifteen minutes. What the widget was and why it was retired
is recorded in the maintainers' history.

---

## What you need for the standalone panel

| | |
|---|---|
| Windows 10 or 11 | |
| Claude Code, used on this PC | The companion reads its local session data |
| PowerShell 7 and Python 3.13 | For the companion, unchanged |
| **.NET 10 SDK** | Builds the panel host once; the installer runs the build. The Desktop Runtime it installs runs the host. https://dotnet.microsoft.com/download. Without it, install with `-SkipPanel` |
| **WebView2 Runtime** | Ships with Windows 11 and most Windows 10 installs |
| The Xeneon Edge | Found by its device id. Any 2560x720 display works, and `panel-settings.json` can name another one |

If anything else is drawing its own dashboard on the Edge, turn that dashboard off for that
screen, or the two full-screen windows fight for the top.

---

## Fresh install (standalone)

```powershell
git clone https://github.com/Dixie-sketch/Clawdeck.git C:\Dev\sidecrab
cd C:\Dev\sidecrab
pwsh -File .\setup\Install-SideCrab.ps1
```

That one command installs all three components - the companion (`SideCrab-crabd`), the notifier
(`SideCrab-toast`) and the panel host (`SideCrab-panel`, built first) - as logon tasks, and starts
them. `-SkipPanel` and `-SkipToast` leave one out; `-WhatIf` describes every step and performs
none of them, including the build.

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

## Upgrading from the widget

1. Update the checkout and every component:

   ```powershell
   pwsh -File C:\Dev\sidecrab\setup\Update-SideCrab.ps1
   ```

2. Install the panel host, which a widget-era install did not have:

   ```powershell
   pwsh -File C:\Dev\sidecrab\setup\Install-SideCrab.ps1
   ```

   The same run unregisters the old `SideCrab-glow` task if you had one and records it in
   `~/.sidecrab/state/retired.json`. Its log is kept.

3. Turn off any other dashboard still drawn on the Edge, and remove the old widget from the Edge
   wherever you placed it. It can no longer reach the companion.

4. Move your settings, if you had changed any:

   | Was in the widget's settings | Now lives in |
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

- **Updating.** `Update-SideCrab.ps1`. It pulls, rebuilds the host from the pulled source,
  restarts the tasks and verifies the result, exiting non-zero if the host did not rebuild or did
  not come back. There is nothing to import at the desk any more.
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

## Troubleshooting the standalone panel

| You see | Do |
|---|---|
| "SideCrab companion not reachable" on the Edge | `Update-SideCrab.ps1`. A `404` in that message means the `widget\` folder is missing beside `companion\` |
| Nothing on the Edge, no window | `~/.sidecrab/logs/panel.log` lists every display it saw and which it picked. Set `display.deviceId` (or `width`/`height`) in `panel-settings.json` |
| The panel looks cropped or scaled | `Test-SideCrab.ps1` has a `panel viewport` row that judges only the line the running host wrote; the host corrects its zoom on the next check. Set the Edge to 100% scale in Windows display settings if you can |
| `panel viewport` says `hidden` | The host is running and deliberately showing nothing because the target display is absent. Check `display.deviceId` and that the Edge is connected |
| `panel viewport` says `stale` | The newest line in `panel.log` belongs to an earlier run, so the host running now has not drawn anything. Restart `SideCrab-panel` and look at the log |
| Two dashboards flicker on the Edge | Something else still draws its own dashboard there. Turn it off for that screen |
| The build fails with "dotnet not found" or an SDK version error | Install the .NET 10 SDK and re-run, or install with `-SkipPanel` |
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
under `%LOCALAPPDATA%\SideCrab\Panel` and goes with `-Purge` too, as do the two credential files:
the approval pairing code (`~/.sidecrab/panel-token`) and the stored limits token
(`~/.sidecrab/limits-token.dpapi`). An uninstall without `-Purge` lists both as retained rather
than removing them quietly. Backups of `settings.json` are never removed at any switch.

---

## What 0.32.0 adds on the glass and in the host

- **Panel settings** has two halves: this panel's look and chime (saved by the host) and the
  companion's quiet hours, toasts, digest and budget (sent to the companion). Only what you change is
  sent.
- **Cancel** beside a queued prompt, with an honest answer when the session already took it.
- **Sources** in the hardware sheet, and an **approval readiness** line with what to do about it.
- **A tray icon** on your main display: status, logs, reload, re-pin, pause, a display picker with a
  ten-second revert, quit until next logon. The host never parks on your primary display by accident
  and recovers from a hidden start on its own.
- **Pull down** always fetches or restarts a quiet connection; a stalled stream no longer blocks the
  refresh.
- **One log per host instance** (`panel.log`, `panel-windowed.log`, `panel-<profile>.log`).

---

## What 0.33.0 adds, and one step to take

- **Re-run the installer once** so the new `PreCompact` hook lands in `~/.claude/settings.json`:

  ```powershell
  pwsh -File .\setup\Install-SideCrab.ps1
  ```

  A second run changes nothing, and a hook you added by hand inside one of SideCrab's entries is
  kept. Skipping this costs you the "compacting now" state and nothing else.
- **What a session is doing.** A working card shows the tool it is running and what for; a chip
  shows PLAN, AUTO-EDIT or BYPASS; a task list shows as a thin line with 3/7; the Detail page adds
  files touched, prompts typed ahead, and compaction. All of it appears only when there is something
  to show.
- **The companion keeps a log** at `~/.sidecrab/logs/crabd.log`.
- **A config file with a typo is no longer replaced.** The companion used to start from the
  defaults when it could not read `~/.sidecrab/config.json`, so one tap on the quiet button could
  overwrite your quiet hours, budget, digest and continue prompts. It now refuses the save and says
  so; fix the JSON and the next save goes through. If it happened to you before this release, the
  file is the defaults and there is no copy of what was there.
- **`SideCrab.Panel.exe --check`** prints what the host would do without showing a window.
