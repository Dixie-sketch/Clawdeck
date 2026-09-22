# Lane E change-log notes

For the orchestrator to fold. Version labels are provisional: widget v0.31.0, panel host
0.3.0.

## Added

- **Bring a session's window to the front, from the panel.** A control in the session sheet
  and on the Detail page asks the panel host to put that session's window in front of the
  primary display. The host enumerates the desktop itself, ranks the windows against the
  session's title, working directory and repository, restores the winner if it is minimised
  and hands it the foreground. The panel never takes the keyboard itself. Standalone only:
  in iCUE the control does not exist.
- **The host answers, and the answer is specific.** `brought to front` when a window matched
  the session, `brought the Claude app to the front` when the Claude desktop app was the
  fallback, `more than one window could be this session` when the ranking would have had to
  guess, and `no window found for this session` when there was nothing to bring. Every
  attempt and its outcome is written to `~/.sidecrab/logs/panel.log`.

## Changed

- **The panel host's single-instance mutex now depends on the mode.** The pinned kiosk keeps
  `Local\SideCrab.Panel` unchanged; a `--windowed` dev host takes
  `Local\SideCrab.Panel.windowed`. A windowed host has no kiosk window and pins to nothing,
  so it must neither block nor be blocked by the pinned one. The scheduled task never passes
  `--windowed`, so the guard on the Edge is untouched.

## Not added, deliberately

- **Answering a session's multiple-choice question from the glass.** Re-verified against
  Claude Code 2.1.278: no supported path exists, and every unsupported one means
  synthesising input into a window on the operator's behalf. Bring-to-front is the
  mitigation; the operator answers at the keyboard. `docs/notes/lane-e-spike.md` has the
  finding.
