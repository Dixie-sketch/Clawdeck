# SideCrab.Panel: the standalone panel host

A small Windows kiosk window that shows the SideCrab panel full-screen on the Corsair Xeneon
Edge. It loads `http://127.0.0.1:2722/panel/`, which crabd serves from the `widget/` tree. It is
the only supported host since 2026-09-21; what came before it and why it went is recorded in
the maintainers' history.

## What the window does

- **Borderless, topmost, a tool window** (no taskbar or Alt+Tab entry) that **never activates**:
  `WS_EX_NOACTIVATE` plus `MA_NOACTIVATE` on every press, the shape a vendor dashboard window
  takes on this display. A tap on the Edge reaches the page without taking focus from what you are typing.
- **Pinned to the Edge by identity, never by index.** The monitor whose PnP device id contains
  `CRXED00`, or failing that the one that is exactly 2560x720 (`panel-settings.json` can change
  both). The size fallback never picks your **primary** monitor and refuses a tie between two
  monitors of that size: a 2560x720 desktop monitor is an ordinary thing to own, and the wrong
  answer here is a topmost full-screen window over the display you work on. A device id you
  name yourself is honoured whatever the monitor is, primary included - but **two** monitors
  carrying that id are refused in the same way, because two Xeneon Edges both contain
  `CRXED00` and picking the first is picking by index. Nothing matching means the window stays
  hidden and the tray menu says why, and the display picker is how you name one of the two.
  Windows caps every window at the size of your **primary** monitor plus its sizing border, so
  a display larger than that is covered only in part; the host logs it when it happens and
  corrects the page's zoom toward the window it actually got.
- **Re-pins itself** on `WM_DISPLAYCHANGE`, `WM_DPICHANGED`, `DisplaySettingsChanged`, power
  resume, session unlock and reconnect, and on a 5 second timer; hides while the Edge is absent
  and returns when it is back. The poll, the settings watcher and the WebView all start from
  the message loop, **not** from the first time a window is shown, so a host that starts with
  its monitor absent still recovers when you attach or pick one. The same poll re-arms the
  settings watcher: a FileSystemWatcher whose directory is removed stops raising events for
  good and says nothing, which used to make every later edit invisible for the life of the
  process.
- **Reachable from the tray**, on whatever display you actually work on: state (which display,
  or hidden and why, and the last failure), open the log folder, reload the panel, re-pin now,
  pause until you resume, quit until next logon, and a display picker listing every monitor
  with its full device id, size, position and scaling. A pick applies at once and **goes back
  on its own after 10 seconds** unless you keep it, on a prompt shown on the primary: a
  mistaken pick can put the panel somewhere you cannot see, and it must never leave you
  without the controls. The tray never takes the keyboard; its menus are the only activation.
- **Measures its own viewport** after every load and re-pin and logs
  `viewport: 2560x720 css px, dpr 1, zoom 1, window 2560x720 physical, pid N, started <iso>`.
  On a scaled monitor it corrects `ZoomFactor` so the CSS viewport equals the physical size.
  While the target monitor is absent it logs `hidden: target display absent, pid N, started
  <iso>` once, then at most once a minute.
- **Locked to the panel.** Any navigation that is not `http://127.0.0.1:<port>/panel/…` (or the
  host's own fallback page) is cancelled and logged; a **child frame** may only reach the panel's
  own origin; permission prompts are denied and downloads cancelled (there is nobody at the glass
  to answer either); new windows are refused; swipe navigation, pinch zoom, the context menu,
  browser accelerator keys, autofill and host objects are all off. A navigation that *succeeds*
  but lands somewhere other than the panel — `about:blank`, say — is not treated as loaded: the
  host keeps a bounded retry and then shows its own fallback page rather than a blank window.
- **Takes settings from the panel, and only from the panel.** Web messages are on (host objects
  are not), and every message is checked on its `Source` against the same navigation lock above.
  Three are answered, and **every accepted request gets exactly one typed reply** carrying the
  `requestId` it answers, success or failure, so a save that could not be written says so
  instead of leaving the panel waiting: `host-info` returns the host version, pid, start time,
  the settings path, whether a pairing code is present — never the code — and what this host can
  do; `settings` writes `panel-settings.json` and answers `settings-result`; `focus-session`
  answers `focus-result`. A `settings` message's `props` go through a whitelist with a type and a
  range for each key: unknown keys are dropped, wrong types are dropped rather than coerced,
  colours must be `#RRGGBB`, percentages are clamped to 0..100, and nothing is written when
  nothing survives. `panelToken`, `crabdPort` and `display` are not on that list. The file is
  merged and written atomically, so every other key survives, and the save does not reload the
  page (the panel has already applied it).
- **Lets the panel make a sound.** `--autoplay-policy=no-user-gesture-required` is always on the
  WebView2 command line. The window never activates, and the alert the panel's chime answers
  arrives while nobody is touching the glass, so Chromium's default policy would leave the
  `AudioContext` suspended and the chime silent with no error anywhere.
- **Honest when crabd is down.** A failed load shows a dark "companion not reachable" page naming
  the URL and the reason, and the host retries every 5 seconds; a crashed WebView2 process is
  re-created; an unhandled exception exits non-zero so the scheduled task relaunches it.
- **Supplies the page's settings.** Before any page script runs it injects one object,
  `window.__sidecrabHost = { kind: "standalone", version, props: {...} }`: the widget's settings
  from `~/.sidecrab/panel-settings.json` `props` and the
  approval pairing code read from `~/.sidecrab/panel-token`. One JSON object, never bare globals,
  so a prop cannot collide with a function name. It is assigned **in the top frame only**, so an
  embedded document never receives a copy of the pairing code. Editing either file reloads the
  page.

## Files

| Path | What |
|---|---|
| `SideCrab.Panel/PanelLogic.cs` | The pure decisions: navigation lock, display selection, host script, zoom correction, the bridge contract, the log and status wording |
| `SideCrab.Panel/PanelForm.cs` | The window: styles, pinning, WebView2 setup, fallback page, viewport check, settings watch, the bridge |
| `SideCrab.Panel/TrayUi.cs` | The tray icon, its menu, the status window and the display picker's timed confirmation |
| `SideCrab.Panel/Displays.cs` | Monitor enumeration (`EnumDisplayMonitors`, `EnumDisplayDevices`, `GetDpiForMonitor`) |
| `SideCrab.Panel/PanelSettings.cs` | `panel-settings.json` and `panel-token` |
| `SideCrab.Panel/WindowFocus.cs` | Enumerating the desktop and handing over the foreground for `focus-session` |
| `SideCrab.Panel/Program.cs` | Entry point, single-instance mutex, command line |
| `SideCrab.Panel/HostCheck.cs` | `--check`: the report, its exit code, and stdout for a windowless app |
| `SideCrab.Panel.Tests/` | MSTest: every gate above, broken on purpose while it was written |

## Build, run, test

```powershell
pwsh -File .\setup\Build-SideCrabPanel.ps1 -Test     # dotnet test + dotnet publish -> panel-host\dist\SideCrab.Panel.exe
pwsh -File .\setup\Install-SideCrab.ps1 -Panel        # builds if missing, registers + starts the SideCrab-panel task
.\panel-host\dist\SideCrab.Panel.exe --windowed       # a normal window on any monitor, for a PC without an Edge
```

Command line (all optional): `--port N`, `--display <id fragment>`, `--devtools-port N`,
`--windowed`, `--sidecrab-dir <path>`, `--profile <name>`, `--check`. The scheduled task passes
none; the settings file carries the same facts, including `"devtoolsPort"` for a desk-side
measurement over Chromium's remote-debugging endpoint. A switch that is missing its value takes
the next switch as one no longer: `--profile --windowed` is refused instead of starting a kiosk
called "windowed". `kiosk` and `windowed` are reserved profile names, because they resolve to the
files the unnamed hosts already own.

### `--check`: what this host would do, without doing it

```powershell
$report = .\panel-host\dist\SideCrab.Panel.exe --check | Out-String   # the pipe is what makes pwsh wait
```

One line per fact and no colour: every display with its id and size, which one it would pick and
why, the WebView2 runtime version, the settings file it would read and every warning reading it,
the log path and whether it can be appended to, and whether another host is already running. It
exits **0** when the panel would show and **2** when something named in the output stops it, with
a `problem:` line for each. It shows no window, takes no single-instance name and never opens the
log, so it is safe to run while the installed host is live. On a PC with no Edge attached:

```
pick: none
pick-reason: none (the target display is not attached)
problem: no display matched, so the panel would start hidden: the target display is not attached
result: problem
```

`--profile` is how you run a **second** host for diagnosis beside the installed one. It takes its
own WebView2 user-data folder, its own log file and its own single-instance name, so it can ask
for a different debugger port without meeting the installed host's environment in a shared
folder, and cannot start it is refused. Nothing installs it; it is a switch you type. It will
happily put a second window on the Edge, so use it with a settings directory of its own:

```powershell
.\panel-host\dist\SideCrab.Panel.exe --profile qa --sidecrab-dir C:\temp\sidecrab-qa --devtools-port 9333
```

Requirements: the .NET 10 SDK to build, the .NET 10 Desktop Runtime and the WebView2 Runtime to
run. The log is `~/.sidecrab/logs/panel.log` for the installed host, `panel-windowed.log` for
`--windowed` and `panel-<profile>.log` for a named profile: one writer per file, because two
hosts appending to one file lose lines silently.
