# SideCrab.Panel: the standalone panel host

A small Windows kiosk window that shows the SideCrab panel full-screen on the Corsair Xeneon
Edge without iCUE. It loads `http://127.0.0.1:2722/panel/`, which crabd 0.31.0 serves from the
same `widget/` tree the iCUE widget is packaged from, so there is one panel and two hosts.

It exists because iCUE 5.51.40 added a widget URL-permission layer that refuses every widget
request to `127.0.0.1` and cannot keep a loopback grant across a restart (README, "Known
caveats").

## What the window does

- **Borderless, topmost, a tool window** (no taskbar or Alt+Tab entry) that **never activates**:
  `WS_EX_NOACTIVATE` plus `MA_NOACTIVATE` on every press, the same shape iCUE's own dashboard
  window uses. A tap on the Edge reaches the page without taking focus from what you are typing.
- **Pinned to the Edge by identity, never by index.** The monitor whose PnP device id contains
  `CRXED00`, or failing that the one that is exactly 2560x720 (`panel-settings.json` can change
  both). Nothing matching means the window stays hidden and says so in the log; it is never
  parked on the primary.
- **Re-pins itself** on `WM_DISPLAYCHANGE`, `WM_DPICHANGED`, `DisplaySettingsChanged`, power
  resume, session unlock and reconnect, and on a 5 second timer; hides while the Edge is absent
  and returns when it is back.
- **Measures its own viewport** after every load and re-pin and logs
  `viewport: 2560x720 css px, dpr 1, zoom 1, window 2560x720 physical`. On a scaled monitor it
  corrects `ZoomFactor` so the CSS viewport equals the physical size.
- **Locked to the panel.** Any navigation that is not `http://127.0.0.1:<port>/panel/…` (or the
  host's own fallback page) is cancelled and logged; new windows are refused; swipe navigation,
  pinch zoom, the context menu, browser accelerator keys, autofill, host objects and web messages
  are all off.
- **Honest when crabd is down.** A failed load shows a dark "companion not reachable" page naming
  the URL and the reason, and the host retries every 5 seconds; a crashed WebView2 process is
  re-created; an unhandled exception exits non-zero so the scheduled task relaunches it.
- **Supplies what iCUE used to.** Before any page script runs it injects one object,
  `window.__sidecrabHost = { kind: "standalone", version, props: {...} }`: the widget's settings
  from `~/.sidecrab/panel-settings.json` `props` (same names as the iCUE properties) and the
  approval pairing code read from `~/.sidecrab/panel-token`. One JSON object, never bare globals,
  so a prop cannot collide with a function name. Editing either file reloads the page.

## Files

| Path | What |
|---|---|
| `SideCrab.Panel/PanelLogic.cs` | The pure decisions: navigation lock, display selection, host script, zoom correction |
| `SideCrab.Panel/PanelForm.cs` | The window: styles, pinning, WebView2 setup, fallback page, viewport check, settings watch |
| `SideCrab.Panel/Displays.cs` | Monitor enumeration (`EnumDisplayMonitors`, `EnumDisplayDevices`, `GetDpiForMonitor`) |
| `SideCrab.Panel/PanelSettings.cs` | `panel-settings.json` and `panel-token` |
| `SideCrab.Panel/Program.cs` | Entry point, single-instance mutex, command line |
| `SideCrab.Panel.Tests/` | MSTest: every gate above, broken on purpose while it was written |

## Build, run, test

```powershell
pwsh -File .\setup\Build-SideCrabPanel.ps1 -Test     # dotnet test + dotnet publish -> panel-host\dist\SideCrab.Panel.exe
pwsh -File .\setup\Install-SideCrab.ps1 -Panel        # builds if missing, registers + starts the SideCrab-panel task
.\panel-host\dist\SideCrab.Panel.exe --windowed       # a normal window on any monitor, for a PC without an Edge
```

Command line (all optional): `--port N`, `--display <id fragment>`, `--devtools-port N`,
`--windowed`, `--sidecrab-dir <path>`. The scheduled task passes none; the settings file carries
the same facts, including `"devtoolsPort"` for a desk-side measurement over Chromium's
remote-debugging endpoint.

Requirements: the .NET 10 SDK to build, the .NET 10 Desktop Runtime and the WebView2 Runtime to
run. The log is `~/.sidecrab/logs/panel.log`.
