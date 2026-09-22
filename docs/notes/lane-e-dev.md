# Lane E developer notes: bring a session to the front

Provisional labels: widget v0.31.0, panel host 0.3.0. Measurements are from the development
PC on 2026-09-21; `docs/notes/lane-e-spike.md` carries the evidence.

## The contract

The page posts one message through the bridge lane B built:

```json
{ "type": "focus-session", "sessionId": "...", "title": "...", "cwd": "...", "repo": "..." }
```

Four facts about a session, and **never a window handle**. The host ranks windows it
enumerated itself; a handle from the page would be a window picker a visited page could aim
anywhere on the desktop.

The host replies:

```json
{ "type": "focus-result", "sessionId": "...", "ok": true, "reason": "focused", "window": "..." }
```

`reason` is `focused` (a window matched), `desktop-app` (the fallback), `refused`,
`no-window`, `no-match`, `ambiguous` or `enumerate-failed`. The page must treat `ok` as the
literal `true`: a truthy string from a later host is a host this build does not understand.

## Where the code is

| What | Where |
|---|---|
| Message validation, ranking, cwd leaf, mutex name | `panel-host/SideCrab.Panel/PanelLogic.cs` |
| Window enumeration and the foreground handover | `panel-host/SideCrab.Panel/WindowFocus.cs` |
| The bridge case, logging, the reply | `PanelForm.FocusSessionFromPage` |
| Page side, one block | `widget/scripts/sidecrab.js`, `---- lane E ----` |
| Styles, one block | `widget/styles/sidecrab.css`, `---- lane E ----` |
| Host tests | `panel-host/SideCrab.Panel.Tests/PanelLogicTests.cs` |
| Page tests | `widget/tests/test_ordering.js` |

Edits to existing functions are one line each and marked `lane E:`: the `focus-session`
case in `OnWebMessageReceived`, the `focus-result` branch in `onHostMessage`, a routing
branch in `onSheetClick` and in `onGridViewClick`, the chip in `detailHead`, the status line
in `renderDetailView`, and `laneEInit()` after `laneBInit()` in `init`.

## The ranking, and the numbers behind it

`ScoreWindow` scores **evidence only**:

| Signal | Score | Why that number |
|---|---|---|
| Window title equals the session title | 100 | The strongest evidence there is. |
| Window title contains it (title >= 6 chars) | 60 | A real console title came back `Administrator:  lane E focus probe session ` - an equality test misses it. |
| Repo name in a TERMINAL's title (>= 4 chars) | 20 | Claude Code names a terminal window (`claude -n`). |
| cwd leaf in a TERMINAL's title (>= 4 chars) | 10 | Weakest signal, terminals only. |

`SelectWindow` then: takes the unique top scorer; refuses a tie as `ambiguous`; falls back
to the Claude desktop app when nothing scored; returns `no-match` when there is no app
either.

## Traps

- **The desktop app must never score alongside evidence.** The first cut gave it a flat 30
  for being the Claude process, and a live run asked for a session titled
  `zzz no such window zzz` in `C:\zzz\nowhere`: the panel brought the app forward and
  reported success. `no window found for this session` was unreachable while the app was
  running. A control that cannot fail reports success forever. The test that fails if this
  comes back is `The_desktop_app_is_a_labelled_fallback_and_never_a_silent_success`.
- **The cwd leaf on this PC is `IT`, two characters, and four windows carry it.** That is
  the `FocusMinWordMatch` floor, and it is why a tie is refused rather than broken. A coin
  toss between four identically titled consoles is wrong three times in four, and being
  wrong means taking the keyboard away from whatever was being typed into.
- **`SetForegroundWindow` is refused when the host is not already the foreground process,
  and returns as if it worked.** It flashes a taskbar button instead. Every step in
  `WindowFocus.Bring` is verified by re-reading `GetForegroundWindow`, never by a return
  value. `SwitchToThisWindow` carried every measured hard case.
- **`AttachThreadInput` is deliberately absent.** It joins the host's input queue to the
  foreground application's, and a hung application on the other end hangs the panel with
  it. This is the one window in the estate that must never stop repainting.
- **A control character in a page-supplied string would forge a line in `panel.log`.** The
  log is the only account of what the host did, so `ValidateFocusRequest` strips control
  characters and caps every value. Proven live: a title containing
  `\n2026-01-01 00:00:00 focus(forged) granted by the page` landed on one line.
- **A minimised window sits at -32000,-32000**, so the primary-display test reads
  `GetWindowPlacement().rcNormalPosition`, not the live rectangle. `MonitorFromRect` is
  called with `MONITOR_DEFAULTTONULL` so a window that is off every monitor is dropped
  rather than snapped onto the primary.
- **A class named `Focus` inside a `Form` subclass does not compile** (CS0119, against
  `Control.Focus()`). It is `WindowFocus`.
- **The Detail head has no room for a status line.** Measured at 2560 px: with the title,
  three state chips and the button in that row, the status ellipsed to `Brought t...`. It
  is its own line below the head, hidden by `:empty` when there is nothing to say.
- **The sheet's shared status line is `display:none` in detail mode**, which is why lane E
  carries its own - the same reason the continue row carries one.
- **A CDP `Emulation.setDeviceMetricsOverride` fights the host's own zoom corrector.** The
  two settle below the stylesheet's measured 1660 px breakpoint and the view switcher
  disappears, so a screenshot of the Detail page silently comes back as the Sessions view.
  Size the dev window to the Edge's own geometry instead.

## Judgement calls, for the orchestrator to take or leave

1. **The sheet control is its own row below the pin row, not in it.** The pin row's own
   comment argues for this: it carries Pin and Full view at the 48 px fingertip floor, and
   a third control joining them moves both under a finger already travelling toward one.
2. **`brought to front` and `no window found for this session` are capitalised**, which the
   panel's other status lines are not (`saved`, `acknowledged`, `sent: ...`). The strings
   came from the brief verbatim. Lower-casing them is a one-line change in
   `LANE_E_REASONS` and `laneEOnFocusResult` if house consistency should win.
3. **The mutex change is lane E's, not the feature's.** It was needed to run a host built
   from the worktree beside the operator's own. It is small and tested, and it can be
   dropped without touching the feature.
4. **The desktop-app fallback still brings the app forward for a session that is provably
   not in it**, because nothing can tell the two apart. It is labelled rather than refused,
   on the grounds that every session on this PC today lives in that app and refusing would
   make the control useless. Refusing instead is a two-line change in `SelectWindow`.

## Running it

```powershell
pwsh -File .\setup\Build-SideCrabPanel.ps1
dotnet test panel-host\SideCrab.Panel.Tests
node widget\tests\test_ordering.js
```

Tests: host 28 -> 38, `test_ordering.js` 173 -> 192 checks, `test_chime.js` 29 unchanged.
