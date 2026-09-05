# Getting started with SideCrab

A first-time walkthrough on macOS, from nothing installed to a panel in your browser that knows
what your Claude Code sessions are doing. Budget about 20 minutes. Every step says what you
should see, so you know when it worked.

If you already know the pieces, the [README](../README.md) is the reference; this page is the
tour.

---

## 0. What you are installing

Two things, and only the first is required:

1. **The companion (`crabd`).** A small background service on your Mac. It reads what Claude Code
   is doing, serves it as one JSON document on `127.0.0.1:9999`, **and serves the panel itself**
   on the same port. Nothing leaves your machine.
2. **The notifier.** Optional. Raises a macOS notification when a session has been waiting on you
   for a while.

There is no separate panel to install. `http://localhost:9999` *is* the panel, so it exists only
while crabd runs - a page with no crabd is a page that did not load, and your browser will say so
in its own words rather than showing you a SideCrab screen.

---

## 1. Check you have what it needs

Open Terminal and run each line. The expected answer is beside it.

| Check | Run | You want |
|---|---|---|
| macOS | `sw_vers -productVersion` | This was built and measured on 26.6. Older is untested, not refused |
| Python | `python3.13 --version` | `Python 3.13.x` or newer. `python3.14 --version` is just as good |
| Claude Code | `claude --version` | A version number. If it says "command not found", install Claude Code first and sign in once |
| Git | `git --version` | Any version |

**If `python3.13` is not found**, check what you do have:

```sh
/usr/bin/python3 --version   # Apple's: 3.9.6 on this machine, and the installer refuses it
brew install python@3.13     # the fix
```

The refusal is **by version, not by path**. The installer probes `$SIDECRAB_PYTHON`, then
`python3.14`, `python3.13` and `python3` across `PATH`, `/opt/homebrew/bin` and
`/usr/local/bin`, asking each one what it is. The absolute path it settles on is written into
the LaunchAgent files, because an agent does not inherit your login `PATH`.

---

## 2. Clone it

```sh
git clone https://github.com/Dixie-sketch/Clawdeck.git ~/SideCrab
cd ~/SideCrab
```

Anywhere you like; the installer records the path it was run from. Keep it somewhere you will
not move, because the LaunchAgents point at these files.

---

## 3. Install (5 minutes)

```sh
./setup/install.sh --with-toast
```

`--with-toast` also loads the notifier agent. Leave it off if you do not want notifications; you
can add it later by re-running with the flag.

Partway through it stops and asks:

```
  Panel approvals let a tap in the browser panel allow or deny a tool call.
    - crabd holds each permission prompt for at most 55 s and NEVER auto-allows
    - no tap, a timeout, or approvals off all fall back to the terminal dialog
    - a tap is only honoured with the pairing code - print it with install.sh --pairing-code
  Enable panel approvals? [y/N]
```

**Answer no for now.** Section 8 covers it. (`--yes` takes every default without asking;
`--with-approvals` and `--no-approvals` answer it on the command line.)

**What you should see:** one line per thing it did, ending with the panel's address. It
refuses before writing anything if something else already holds port 9999; it backs
`~/.claude/settings.json` up to `<path>.sidecrab-bak-YYYYMMDD-HHMMSS` before merging the hooks;
it takes the `statusLine` slot and saves whatever was there; and it loads
`com.sidecrab.crabd` (and `com.sidecrab.toast`) as LaunchAgents.

Then check it:

```sh
./setup/install.sh --status
```

**What you should see:** the agents `loaded, running` with a pid, `service: ok - crabd answered
and the agent is running - it owns port 9999`, `hooks: 7 of 7 present`, and
`panel: http://localhost:9999`. `--status` writes nothing at all.

---

## 4. The Keychain prompt

Claude Code keeps its credential in your login Keychain (service `Claude Code-credentials`), not
in a file - on the machine this was written on, `~/.claude/.credentials.json` does not exist at
all. A process that is not on that item's access list gets a macOS dialog the first time it reads
it, and crabd reads it through `/usr/bin/security`.

**What you should see:** one dialog, shortly after the first install, naming the `security` tool
and the `Claude Code-credentials` item. Choose **Always Allow** and it does not come back.

If you dismiss it, nothing breaks and nothing is guessed: the LIMITS gauges go dark and the panel
says the Keychain refused, pointing you back at the prompt rather than telling you to log in
again. The two failures - "there are no credentials" and "this process was not allowed to see
them" - have different fixes, so they are different messages.

---

## 5. Open the panel

Open **<http://localhost:9999>** in Safari.

**What you should see:** the crab, a large clock, the LIMITS gauges filling in with your current
usage and reset times, and a hardware row with this Mac's CPU and memory. No session cards yet -
you have not started a session.

Now open the same address in Chrome. **What you should see:** the same panel. What does *not*
carry across is settings: each browser keeps its own copy on the panel's address, so a colour or
a pairing code set in one is not set in the other. That is the same-origin policy doing its job,
and section 8 explains why it matters for the pairing code.

Resize the window. The layout is a set of media queries, not a fixed canvas: a laptop window
gets three card columns, a tablet-shaped window two, a phone-shaped one gets a single column with
the gauges side by side.

---

## 6. Your first session

Open a terminal, `cd` into any project, and run `claude`. Ask it anything.

**What you should see:** within a few seconds a card appears on the panel with the session's
title, the repo name, and a WORKING state. When the session finishes its turn the card turns
DONE; the crab may put its sunglasses on and dance if the turn was a real one.

If no card appears, the hooks are not reaching the companion. Run `./setup/install.sh --doctor`
and look at the hook rows.

---

## 7. Watch it ask you something

Ask the session a question it has to come back to you with - anything that makes it stop and
wait rather than finish.

**What you should see:** the card turns to NEEDS INPUT, the crab perks up and glows, and (if you
installed the notifier) a macOS notification arrives once the session has been waiting past the
threshold - 120 seconds by default. The notification is attributed to **Script Editor**, because
that is who `osascript` posts as; the subtitle always says SideCrab. It has no buttons: you
acknowledge on the panel, where the context is.

The very first notification a process posts can raise a one-time macOS permission prompt for
Script Editor. To meet that at a prompt rather than wonder why an alert was silent, fire one
deliberately:

```sh
python3 notifier/sidecrab_toast.py --test-toast
```

Now try the panel's controls:

| Do this | What happens |
|---|---|
| Click the card | Its sheet opens: the question, the subagents, the last event |
| Click the crab, or press `a` | Every waiting session is acknowledged at once; the glow stops |
| Press and hold a card, or press `p` with it focused | It is pinned to the front. Again to unpin |
| Drag a card sideways, or press Delete | Acknowledge or dismiss it |
| The filter chips, top right | Show only waiting / working / finished. It is a view: the glow, the crab and the notification threshold still see every session |
| The density chip | Comfortable or compact cards |
| The moon beside the clock | Quiet for an hour, then "stay awake through tonight's window", then back to the schedule. The choice is written to `~/.sidecrab/config.json` by crabd, so it survives a reload |
| Click a stopped card | Pick a next step: Continue, Run the tests, Commit + push, or any you added to `continuePrompts`. It is delivered the next time that session's Stop hook fires |
| Press `s` | The settings sheet: quiet hours, notifications, the budget, the crab style, the accent colour, the pairing code |

The continue prompts are a **fixed vocabulary**. There is no free-text box on the panel, and no
supported way to inject arbitrary text into a live session.

---

## 8. Approving permission requests from the panel (optional, read first)

When a Claude Code session stops to ask permission for a tool call, the card can show Approve
and Deny buttons, and a tap decides it. This is off by default because it is a real security
control, and it needs a one-time pairing so that only your panel, not a web page you happen to
visit, can decide.

1. Turn approvals on:

   ```sh
   ./setup/install.sh --with-approvals
   ```

2. Print the pairing code crabd minted for this machine:

   ```sh
   ./setup/install.sh --pairing-code
   ```

3. Open the panel's settings sheet (`s`), find **Approval Pairing Code**, and paste it in.

4. Prove it on a throwaway session before you trust it. Open `claude` in an empty folder, ask it
   to run a command your settings do not pre-allow, and when the card shows the request, tap
   Approve. The command should run with no dialog in the terminal. Then do one Deny and confirm
   the command does not run.

What to expect while it is on: a request waits on the panel for up to 55 seconds. If you do not
tap, or the companion is down, or the code is wrong, the normal terminal dialog appears and
decides, exactly as if SideCrab were not installed. The terminal dialog is **raced, not
suppressed**: whichever surface answers first wins.

**The code lives in the browser you pasted it into**, in that browser's storage on the panel's
address. Only a page on that address can read it - but that is a weaker guarantee than the iCUE
property it replaces, because a script running on the panel's own origin can. The README's
"Before you turn on panel approvals" and [`SECURITY.md`](../SECURITY.md) have the full argument
and what the code is only one factor of.

---

## 9. Prove it fails honestly

The most useful thing you can do before you trust a status panel is watch it lie badly on
purpose. Stop the companion:

```sh
launchctl bootout gui/$(id -u)/com.sidecrab.crabd
```

**What you should see, in the tab you already have open:** within about 30 seconds the crab turns
grey and worried and a banner appears reading `crabd not responding — data as of HH:MM`. The
cards do not disappear and the numbers do not go to zero - the last good document stays on the
glass, dated. That is the whole design: a green-looking panel always means fresh data.

**What you should see in a new tab:** a browser connection error. There is nothing serving a
page, so there is nothing to show you a nicer message.

Start it again:

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.sidecrab.crabd.plist
```

(Re-running `./setup/install.sh` does the same thing, plus everything else it does.) The banner
clears on the next poll, within three seconds.

---

## 10. The doctor

```sh
./setup/install.sh --doctor
```

**What you should see:** a PASS/FAIL table with a row for each agent, the interpreter, health,
the state document's reachability, schema and freshness, the panel, the panel's and the
notifier's accepted schemas, the header gate, the three hook events, the hook cycle,
`config.json`, the status-line chain, the limits token and panel approvals. A FAIL row names what
is wrong; the command exits non-zero if any row failed.

**`--doctor` is not read-only.** It posts a real SessionStart / Notification / SessionEnd cycle
for the session id `smoke-test` to prove the write path end to end, and it POSTs to `/v1/hook`
*without* the `X-SideCrab-Panel` header to prove the header gate is live. The cycle cleans up
after itself - SessionEnd is sent from a `finally`, so even a crash mid-run clears the row - but
crabd persists every hook event, so the run leaves three rows in `~/.sidecrab/history.jsonl` and
they appear in that day's history. Use `--status` when you want a look and no footprint.

---

## 11. Make it yours (optional)

All of these live in `~/.sidecrab/config.json`, and most are also on the panel's settings sheet.
The file is created for you; every key is optional.

```jsonc
{
  "quietHours": { "start": "22:00", "end": "07:00" },  // dim the panel, no notifications
  "toast":  { "enabled": true, "thresholdSec": 120 },  // alert after a session waits this long
  "digest": { "enabled": true, "time": "09:00" },      // one "yesterday" summary a day
  "budget": { "dailyOutputTokens": 5000000 },          // a daily token budget marker and alert
  "continuePrompts": ["Continue", "Run the tests"],    // extra next-step buttons on a card
  "recapRepos": ["/Users/you/dev/my-project"]          // repos whose commits count in the recap
}
```

The `toast` key keeps its name on the wire even though the panel now calls the switch "Desktop
Notifications": a contract key is not a label.

---

## 12. Updating and removing

```sh
git -C ~/SideCrab pull
~/SideCrab/setup/update.sh
```

That refreshes the plists from the checkout and restarts crabd - and because crabd serves the
panel from the same checkout, the pull updates the panel too. Reload the tab. `update.sh` refuses
to restart over a port held by something else, and names the PID rather than starting blind.

To remove everything the installer added, including the hooks in `~/.claude/settings.json` and
the status-line slot:

```sh
~/SideCrab/setup/uninstall.sh
```

It takes back what SideCrab wrote and nothing else: your own hooks stay, your prior status-line
command is restored, and `~/.sidecrab` and every backup survive. `uninstall.sh --purge` deletes
`~/.sidecrab` too, after telling you what is in it and asking.

---

## 13. Two macOS things worth knowing

### App Nap and timer coalescing - measured, and no plist key

macOS can throttle a background process's timers. Whether it throttles a `KeepAlive` LaunchAgent
was measured on this hardware rather than guessed: two minutes of sampling the feed's own
timestamp gave **55 distinct snapshots, max gap 3.0 s, mean 2.19 s, none over 4 s**, against a
2 s rebuild. So the plists carry **no `ProcessType` key** and the default scheduling stands.

That reading was taken with the machine in normal use. The case where a throttle would show -
idle, on battery, lid shut - has not been measured. To take your own reading:

```sh
for i in $(seq 1 120); do
  curl -s localhost:9999/v1/state | python3 -c 'import json,sys; print(json.load(sys.stdin)["generatedAt"])'
  sleep 1
done > /tmp/generated.txt

python3 - <<'PY'
from datetime import datetime
stamps = [datetime.fromisoformat(line.strip().replace("Z", "+00:00"))
          for line in open("/tmp/generated.txt") if line.strip()]
gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
print("max gap %.1fs over %d samples" % (max(gaps), len(stamps)))
PY
```

Record any number you get in [`PORT-NOTES.md`](PORT-NOTES.md) before changing anything about the
plist. Do not add a `ProcessType` on the strength of a forum post.

### TCC (the "would like to access your Documents folder" dialogs)

crabd **reads** `~/.claude` (transcripts and settings) and **writes only** `~/.sidecrab` (its own
config, history, logs and pairing code). The installer is the thing that writes
`~/.claude/settings.json`, and it does so once, with a backup, when you run it - not from the
agent. None of those is a TCC-protected location, so no folder-access dialog is expected.

If a future change makes crabd read `~/Documents`, `~/Desktop`, `~/Downloads` or an iCloud
folder, macOS will prompt - and under a LaunchAgent that prompt can appear detached from any
window, with a denial that is permanent until it is reset in System Settings. Treat "the daemon
needs a new directory" as a decision with a user-visible cost, not a detail.

---

## 14. When something is wrong

| You see | Try |
|---|---|
| The browser cannot connect | crabd is not running. `./setup/install.sh --status`, then `./setup/update.sh` |
| Worried grey crab, "data as of HH:MM" | The same: the agent stopped, or the feed is older than 30 s |
| No session cards | `./setup/install.sh --doctor`; check `~/.claude/settings.json` still has the SideCrab hooks. If you set `allowedHttpHookUrls`, both host forms have to be in it |
| Gauges show a dash and "token expired" | The CLI token lives ~6 h. Store a long-lived one: `claude setup-token`, then `./setup/install.sh --limits-token` (README, "Keeping the limit gauges alive") |
| Gauges show a dash and a Keychain note | Approve the `Claude Code-credentials` prompt (Always Allow), or run `claude` in a terminal |
| `Address already in use` on port 9999 | `lsof -nP -iTCP:9999 -sTCP:LISTEN`, then stop it. crabd stops loudly rather than moving to another port |
| The installer refuses your Python | It is Apple's 3.9 stub. `brew install python@3.13`, or set `$SIDECRAB_PYTHON` |
| "not paired" or "pairing code wrong" on Approve | Re-paste the code from `--pairing-code` into that browser's settings sheet. Each browser needs it once |
| Anything else | `./setup/install.sh --doctor` prints a PASS/FAIL row for every piece |

Still stuck? [Open an issue](https://github.com/Dixie-sketch/Clawdeck/issues) with the doctor
table. Please do not paste anything from `~/.claude`; it holds your session transcripts.
