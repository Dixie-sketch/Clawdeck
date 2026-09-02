# Getting started with SideCrab

A first-time walkthrough, from nothing installed to a crab on your Xeneon Edge that knows what
your Claude Code sessions are doing. Budget about 20 minutes. Every step says what you should
see, so you know when it worked.

If you already know the pieces, the [README](../README.md) is the reference; this page is the
tour.

---

## 0. What you are installing

Two things, and the second is optional:

1. **The widget.** A file you import into Corsair iCUE. It draws the panel: the crab, the clock,
   your CPU and GPU temperatures, and, once the companion runs, your sessions.
2. **The companion (`crabd`).** A small background service on the same PC. It reads what Claude
   Code is doing and serves it to the widget over `127.0.0.1` only. Nothing leaves your machine.

Stop after step 2 and you have a handsome clock and temperature panel. Do step 3 and it comes
alive.

---

## 1. Check you have what it needs

Open **PowerShell 7** (the app is called "PowerShell 7", not "Windows PowerShell") and run each
line. The expected answer is beside it.

| Check | Run | You want |
|---|---|---|
| Windows | `[Environment]::OSVersion.Version` | Major version 10 (Windows 10 or 11) |
| iCUE | Open iCUE, Settings, About | 5.44 or newer, and a Xeneon Edge listed as a device |
| Claude Code | `claude --version` | A version number. If it says "not recognized", install Claude Code first and sign in once |
| PowerShell 7 | `$PSVersionTable.PSVersion` | 7.x |
| Python | `python --version` | `Python 3.13.x`. If a Microsoft Store window opens instead, you have the Store alias, not Python: install from python.org and tick "Add python.exe to PATH" |
| Git | `git --version` | Any version |

Not on Windows, or no iCUE? SideCrab cannot run. The widget is an iCUE widget and the companion
is a Windows service; there is no other build.

---

## 2. Install the widget (5 minutes)

1. Go to the [releases page](https://github.com/Dixie-sketch/Clawdeck/releases/latest) and
   download `SideCrab-<version>.icuewidget`.
2. Double-click the file. iCUE 5.46.67 or newer imports it directly. On an older iCUE, open the
   Xeneon Edge's dashboard editor in iCUE and use its import option to pick the file.
3. In iCUE, put the widget on the Xeneon Edge and make it **full-screen**. It is designed for the
   whole 2560 × 720 display.

**What you should see:** the crab, a large clock, and a line saying Claude Code stats need the
companion. Temperatures appear once you pick sensors: open the widget's settings in iCUE and
choose a CPU and a GPU sensor. Each reading shows the sensor's name beside it, so a wrong pick is
obvious.

If the panel is completely blank, with no crab and no clock, the widget did not load. Re-import
the newest release; if it stays blank, [open an issue](https://github.com/Dixie-sketch/Clawdeck/issues)
with your iCUE version.

---

## 3. Install the companion (10 minutes)

In PowerShell 7:

```powershell
git clone https://github.com/Dixie-sketch/Clawdeck.git C:\Dev\sidecrab
cd C:\Dev\sidecrab
pwsh -File .\setup\Install-SideCrab.ps1 -WithToast
```

`-WithToast` also installs the notifier, which raises a Windows notification when a session has
been waiting on you for a while. Leave it off if you do not want toasts.

The installer prints one line per thing it does. It:

- registers a Scheduled Task that starts `crabd` at logon, and starts it now,
- backs up `~/.claude/settings.json`, then adds the SideCrab hooks to it. Your other hooks are
  untouched, and running the installer twice never duplicates anything,
- registers the notifier's identity, under your user account only. No admin prompt,
- asks about **panel approvals**. Answer **no** for now; section 6 covers it.

Then check the result:

```powershell
pwsh -File .\setup\Install-SideCrab.ps1 -Status
pwsh -File .\setup\Test-SideCrab.ps1
```

**What you should see:** `-Status` shows the crabd task Running and health `ok`. The smoke test
prints a table with every row PASS. A FAIL row names what is wrong and what to do.

---

## 4. Your first session

Open a terminal, `cd` into any project, and run `claude`. Ask it anything.

**What you should see:** within a few seconds a card appears on the panel with the session's
title, the repo name, and a WORKING state. The two LIMITS gauges fill in with your current usage
and reset times. When the session finishes its turn, the card turns DONE; when it asks you a
question, the card turns to NEEDS INPUT and the crab perks up.

If no card appears, the hooks are not reaching the companion. Run `Test-SideCrab.ps1` and look
at the hook rows.

---

## 5. Make it yours (optional)

All of these live in `~/.sidecrab/config.json`, and most are also on the panel's settings sheet.
The file is created for you; every key is optional.

```jsonc
{
  "quietHours": { "start": "22:00", "end": "07:00" },  // dim the panel, no toasts, no glow
  "toast":  { "enabled": true, "thresholdSec": 120 },  // toast after a session waits this long
  "digest": { "enabled": true, "time": "09:00" },      // one "yesterday" summary toast a day
  "budget": { "dailyOutputTokens": 5000000 },          // a daily token budget marker and toast
  "continuePrompts": ["Continue", "Run the tests"],    // extra next-step buttons on a card
  "recapRepos": ["C:\\Dev\\my-project"]                // repos whose commits count in the recap
}
```

The moon button beside the clock is quiet hours on the glass: tap for an hour of quiet, tap again
to stay awake through tonight's window, tap again to go back to the schedule.

---

## 6. Approving permission requests from the panel (optional, read first)

When a Claude Code session stops to ask permission for a tool call, the card can show Approve
and Deny buttons, and a tap decides it. This is off by default because it is a real security
control, and it needs a one-time pairing so that only your panel, not a web page you happen to
visit, can decide.

1. Print the pairing code the companion made for this PC:

   ```powershell
   pwsh -File .\setup\Install-SideCrab.ps1 -PairingCode
   ```

2. In iCUE, open the widget's settings and paste the code into **Approval Pairing Code**.
3. Turn approvals on:

   ```powershell
   pwsh -File .\setup\Install-SideCrab.ps1 -WithApprovals
   ```

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

The crab is the summary. Calm means nothing needs you. Alert with a glow means a session is
waiting. Worried and grey means the data is stale: the companion stopped, or the feed is older
than 30 seconds. The panel never shows old numbers as if they were fresh.

---

## 8. Updating and removing

```powershell
git -C C:\Dev\sidecrab pull
pwsh -File C:\Dev\sidecrab\setup\Update-SideCrab.ps1
```

That updates the companion. The widget updates separately: download the new `.icuewidget` from
the releases page and import it again. The two sides tolerate a version gap, so the order does
not matter. After an import, check the widget's settings; iCUE can reset them, including the
pairing code.

To remove everything the installer added, including the hooks in `~/.claude/settings.json`:

```powershell
pwsh -File C:\Dev\sidecrab\setup\Uninstall-SideCrab.ps1
```

Remove the widget from the Edge in iCUE as you would any other widget.

---

## 9. When something is wrong

| You see | Try |
|---|---|
| Blank panel, no crab | Re-import the newest `.icuewidget`; then open an issue with your iCUE version |
| Worried grey crab, "data as of HH:MM" | `Install-SideCrab.ps1 -Status`, then `Update-SideCrab.ps1` to restart the task |
| No session cards | `Test-SideCrab.ps1`; check `~/.claude/settings.json` still has the SideCrab hooks |
| Gauges show a dash and "/login" | Run `claude` and sign in again |
| Temperatures frozen or wrong | Pick the right sensor in the widget's settings; the row names the one it reads |
| "not paired" or "pairing code wrong" on Approve | Re-paste the code from `-PairingCode` into the widget's settings |
| Anything else | `pwsh -File .\setup\Test-SideCrab.ps1` prints a PASS/FAIL row for every piece |

Still stuck? [Open an issue](https://github.com/Dixie-sketch/Clawdeck/issues) with the smoke-test
table. Please do not paste anything from `~/.claude`; it holds your session transcripts.
