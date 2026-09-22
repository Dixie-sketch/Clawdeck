# Lane E documentation notes

Draft prose for the orchestrator to fold into `README.md` and
`docs/UPGRADING-TO-STANDALONE.md`. Not applied to the shipped docs.

## For README, "Using it"

### Bring a session to the front

Tap a card, and the sheet now offers **Bring to front** under Pin session. The Detail view
carries the same control as a chip beside Back. Tapping it puts that session's window in
front of you on your main display, so you can answer at the keyboard without hunting for it.

The panel itself never takes the keyboard. It has no taskbar entry, it does not appear in
Alt-Tab, and touching the glass does not steal focus from whatever you are typing into -
bringing another window forward does not change that.

What it says afterwards:

| It says | What happened |
|---|---|
| brought to front | A window matched that session and it is in front of you now. |
| brought the Claude app to the front | The Claude desktop app is in front. It keeps every session it runs in one window, so pick the session in its sidebar. |
| more than one window could be this session | Two windows looked equally likely. Nothing was moved, on purpose - bringing the wrong one forward would take the keyboard away from whatever you were using. |
| no window found for this session | Nothing on your main display looks like that session. |

It only ever looks at your **main display**, never at the Edge, and it never touches the
panel's own window.

**It does not answer for you.** Nothing in SideCrab types into a session, pastes into one,
or clicks anything inside one. A session's multiple-choice question is answered in that
session, by you. Getting the right window in front of you is the whole of what this control
does, and it is why it exists: the panel already tells you *which* session is waiting, and
this makes the next step one tap instead of a hunt across nine windows.

Approve and Deny are the exception, and they are not an exception to that rule: a tool
permission request is a question Claude Code asks *outside* the session's own prompt, over
a channel built for an answer, which is why the panel can answer it and cannot answer
anything else.

### Where it works

Standalone only - the panel host window. In iCUE the control is not there at all, because
iCUE's widget has no host to ask.

## For UPGRADING-TO-STANDALONE, the upgrade notes

The standalone panel can now reach your desktop for one thing: bringing a session's window
to the front. The widget running inside iCUE cannot, and does not show the control.

The host works it out from the session's title, working directory and repository - the
facts crabd already has. It enumerates your windows itself; the page never names one. If
your sessions run in the Claude desktop app, expect **brought the Claude app to the front**:
that app keeps every session in a single window, so there is no per-session window for the
panel to find, and you pick the session in its sidebar. A session running in a terminal that
Claude Code has named is matched by that name directly.

Every attempt is written to `~/.sidecrab/logs/panel.log` with the window it chose, why it
chose it, how it was brought forward and whether the panel kept its hands off the keyboard:

```
focus(a1b2c3d4-...) 'Panel host build' -> claude 'Claude' [Chrome_WidgetWin_1]
  score 0, 16 candidates, SwitchToThisWindow, ok=True, panel took focus=False, 29 ms
```

If you run a panel host by hand with `--windowed` while the installed one is running, both
now start: the pinned window and a windowed dev host no longer share a single-instance
lock.
