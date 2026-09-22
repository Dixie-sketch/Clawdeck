# Lane E spike: finding a session's window, and the multiple-choice blocker

Measured on the development PC on 2026-09-21. Windows 11, one display attached (the Edge
was out of the display set over Remote Desktop), Claude Code CLI 2.1.278, crabd 0.32.0.
Every number below was read off this machine in this session.

## Verdict

**A session cannot be identified from outside itself. A session's HOST can.** That is
enough to build "bring that session to the front", and it is not enough to claim the
control found the session's own window. The shipped design says which of the two happened
on every attempt.

Two live sessions were running throughout the spike:

| id | title | cwd | repo | state |
|---|---|---|---|---|
| `a1b2c3d4` | Panel host build | `C:\IT` | ops-notes | working |
| `e5f6a7b8` | Dashboard redesign | `C:\IT` | ops-notes | idle |

### 1. Window titles do not identify a session

Enumerating every visible top-level window found 16 of them, and the Claude desktop app
owned exactly **one**:

```
hwnd 0x20718  pid 33480  claude  Chrome_WidgetWin_1  primary  "Claude"
```

One window, two sessions, and the title is the literal string `Claude`. No window title
anywhere on the desktop carried either session's title. A session running in the desktop
app therefore has no window of its own to find.

### 2. Terminal titles carry the cwd leaf, and that is not enough

Four visible windows were titled `pwsh in IT` - three `pwsh` consoles
(`ConsoleWindowClass`) and one Windows Terminal (`CASCADIA_HOSTING_WINDOW_CLASS`). `IT` is
the leaf of `C:\IT`, which both sessions share.

**None of those four was a Claude Code session.** Their command lines put two of them as
PowerShell MCP consoles and one as the operator's own shell; the Claude Code shells
(`CLAUDE_CODE_SHELL_LAUNCHER_SCRIPT`, and the pool under the desktop app's pid 33480) have
no windows at all. So on this PC the cwd leaf matched four windows and zero sessions, and a
control that acted on it would have been wrong every time.

This is what put the `FocusMinWordMatch` floor of 4 characters on the repo and folder
scores, and it is why a tie at the top of the ranking is refused rather than broken.

### 3. Session titles ARE in the desktop app's UI Automation tree, and reading them costs

Walking the app's UIA tree (`System.Windows.Automation`, pid 33480) found the sessions as
sidebar buttons:

```
[Button] Running Panel host build
[Button] Idle Dashboard redesign
[Button] Panel host build, rename session
```

Two traps came with it, and both argue against shipping the walk:

- **The first walk returns almost nothing, on a healthy app.** Walk one returned 15 nodes
  in 31 ms: the window frame, a pane and the three caption buttons. Walk two, seconds
  later, returned the whole web tree. Chromium enables its accessibility tree lazily on the
  first client request, so a one-shot probe reports "no sessions found" against an app that
  has them. A control built on a single walk would be wrong on first use every time.
- **Asking raises Chromium's accessibility mode in the operator's app for the rest of its
  life**, which is a cost imposed on the thing being measured.

And even with the tree in hand, acting on it means invoking a sidebar button - driving the
operator's application, not focusing a window. Out of scope by design (see the blocker
below). The tree is recorded here as evidence, not used.

### 4. crabd knows no window and no pid

`_blank()` in `companion/crabd.py` is the whole session row, and it carries `state`,
`since`, `cwd`, `question`, `events` and no window, handle or process id. The hooks it is
fed (`hooks/settings-hooks-fragment.json`: SessionStart, UserPromptSubmit, Notification,
Stop, SubagentStop, PermissionRequest, SessionEnd) deliver `session_id`, `cwd` and
`transcript_path`. Nothing in the chain ever sees a window. The host must therefore do its
own enumeration and its own ranking, from the four facts the page can tell it.

### 5. The foreground lock, measured both ways

The panel window carries `WS_EX_NOACTIVATE` and answers `MA_NOACTIVATE`, so it is normally
not the foreground process when a tap arrives. That is the case that matters, and it
behaves differently from the easy one:

| attempt | host was foreground? | call that carried it | result |
|---|---|---|---|
| 1 | yes (dev window) | `SetForegroundWindow` | ok, 32 ms |
| 2 | no (a console was) | **`SwitchToThisWindow`** | ok, 16 ms |
| 3 | no (the app was) | **`SwitchToThisWindow`** | ok, 25 ms |
| 8 | no (a browser was) | **`SwitchToThisWindow`** | ok, 29 ms |

`SetForegroundWindow` is refused whenever the host is not already the foreground process,
exactly as documented, and it returns as though it worked - which is why every step is
verified by re-reading `GetForegroundWindow` rather than by a return value.
`SwitchToThisWindow` carried every one of the hard cases.

`AttachThreadInput` was considered and rejected. It joins the host's input queue to the
foreground application's, and a hung application on the other end hangs the panel with it.
It was never needed.

**The panel never took the keyboard.** Every attempt logs `panel took focus=False`, read
back from `GetForegroundWindow` after the handover, not assumed.

### 6. What the ranking does with all of this

Evidence first, the desktop app as a labelled fallback, and a refusal where neither applies.
`ScoreWindow` scores a window title match (exact 100, contains 60) on any process, and the
repo name (20) and cwd leaf (10) on terminals only. The Claude desktop app scores nothing:
it is chosen only when nothing was matched, and comes back under its own reason so the page
can say `brought the Claude app to the front` rather than claiming the session's window was
found.

**A live run found the defect that reading the code did not.** The first cut gave the
desktop app a flat 30 points for being the Claude process. Attempt 4 asked for a session
titled `zzz no such window zzz` in `C:\zzz\nowhere`, and the panel brought the Claude app
forward and reported success. `no window found for this session` was unreachable while the
app was running - a control that reports success forever. The fallback split fixed it, and
`The_desktop_app_is_a_labelled_fallback_and_never_a_silent_success` is the test that fails
if it comes back.

## The multiple-choice blocker, re-verified

**There is still no supported path to answer a running session's multiple-choice question
from outside that session, and lane E does not add one.**

Re-checked against the installed CLI (`claude --version` reports `2.1.278 (Claude Code)`)
by reading its own `--help` output in full. Nothing in it carries an answer into a session
that is already running and attached to someone else's terminal or window:
`--input-format stream-json` is documented as working only with `--print`, which means a
non-interactive run whose stdin the caller already owns; `-r/--resume` and `--fork-session`
start a new process continuing a stored conversation rather than answering a live one;
`claude attach <id>`, `logs`, `stop` and `respawn` operate on background sessions
(`--bg`), and attach moves a background session into the current terminal instead of
delivering a keystroke to an existing one; `--remote-control` is a person steering a session
from another device, not a local API. The hook surface is the same story from the other
side: of the seven events SideCrab registers, **PermissionRequest is the only one with a
response contract** - it holds for up to 60 seconds and takes an allow or deny back, which
is exactly what the panel's Approve and Deny buttons already use. A question the model
asks in the prompt box is not a hook event at all. It is rendered by the session's own UI
and answered there.

Everything that would work is the same thing wearing different clothes: synthesising
keystrokes, driving the UI through automation, or writing the clipboard and pasting. All of
it means putting text into a session on the operator's behalf, from a surface that takes
its instructions from a web page. That is prompt injection by another route, and it is
refused by design, not by omission. Nothing in this lane types, pastes or clicks inside any
window; the host restores a window and asks Windows to make it foreground, and that is the
whole of its reach.

**Bring-to-front is the mitigation.** The panel gets the right window in front of the
operator in under 30 ms, and the operator answers at the keyboard.

## What was not verified, and why

- **On the Edge itself.** The Edge was out of the display set over Remote Desktop, so the
  host ran `--windowed` and the focus attempts were driven through the devtools port rather
  than by a fingertip. The windowed dev window does not carry `WS_EX_NOACTIVATE`; attempts
  2, 3 and 8 were run while the host was not the foreground process, which is the same
  foreground-lock position a kiosk tap starts from, and they are the measurements the
  design rests on. A tap on the real glass has not been made.
- **A terminal-hosted Claude Code session.** There was not one on this PC at any point:
  both live sessions ran in the desktop app, and every visible console belonged to
  something else. The terminal branch of the ranking was exercised against a console this
  lane started and titled itself (`cmd /c title ...`), which scored 60 on the contains
  branch and was brought forward correctly - but a real `claude -n` session in a real
  terminal has not been ranked.
- **A second Claude desktop window.** Never observed, so the `ambiguous` result for two app
  windows is covered by a unit test only.
- **The `no-match` reply on a live desktop.** Unreachable while the Claude app is running,
  which is the point of the fallback. Covered by a unit test.

## Method notes, for whoever repeats this

- Read the monitor list with `GetMonitorInfoW`, not `GetMonitorInfo`. The ANSI entry point
  binds silently and then fails on the Unicode struct's `cbSize`, so every window comes
  back with a blank monitor name and `primary` false - which reads exactly like a machine
  with no primary display.
- Call `SetProcessDpiAwarenessContext` before reading window rectangles from a script host,
  or a 2560x1600 display at 150% reports every rectangle at 1707x1067.
- A minimised window sits at -32000,-32000, so `MonitorFromWindow` answers with whichever
  monitor is nearest to nowhere. `GetWindowPlacement().rcNormalPosition` is where the window
  will actually be, and that is what the primary-display test uses.
- Do not trust a free-port check from the connection table. Port 9333 read as free and was
  held by the operator's own browser; a `TcpListener` bind test found it immediately.
- A class named `Focus` inside a `Form` subclass collides with `Control.Focus()` and fails
  to compile with CS0119. It is `WindowFocus`.
