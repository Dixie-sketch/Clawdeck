# Lane C — the CHANGELOG.md entry

*Fold these bullets into the release the lanes merge into. The version heading is the
orchestrator's to assign; `Added` / `Changed` / `Fixed` match CHANGELOG.md's own sections.*

---

### Added

- **Four views on one display.** The Sessions zone gains a view switcher in its own header —
  **Sessions**, **Burn**, **Week** and **Detail** — and the identity and Limits zones are
  unchanged. Sessions is the card grid exactly as before, and the filter and density chips still
  narrow that view and no other. The active view persists with the filter, the density and the pin
  map in the same vendor-storage object, and a horizontal swipe on the header row steps through
  the four.
- **Burn view.** Today's spend at full width: by live session and by model, each shown only when
  the feed carries it and each saying so when it does not; the 24 h series at chart size with hour
  labels; the day's totals, the dollar figure when Claude Code's telemetry is flowing, and the
  budget percentage when one is set. The budget marker on hourly bars is a pace line, not a
  ceiling, the way the Limits sparkline already draws it.
- **Week view.** The last seven days as a full-width strip, with a day's history opening **inline
  underneath it** rather than in a sheet. A day the companion cannot answer for leaves the strip
  exactly as it was and says why, and the next tap tries again.
- **Detail view.** One session as a full-height page: title, repo and branch, state and elapsed,
  model, the context hairline with its numbers, the whole question with nothing trimmed,
  the permission request with the same Approve and Deny controls the sheet carries, every subagent
  row, the events list whole, the queued prompt and the continue buttons, and a Back chip. Opened
  from the new **Full view** button in a session's sheet, or from the Detail chip, which falls back
  to the session that wants a human.
- **An alert cannot hide behind a view.** A session that starts waiting while another view is up
  puts a pulsing count on the Sessions chip. The view does not change by itself: a panel that
  swapped what was under a fingertip would be worse than the thing it was warning about.
- **`&view=` dev flag** (mock mode only, in memory only), so each view can be photographed.

### Changed

- **The crab is drawn to a canvas.** Same art on the same half-cell grid, same colours, same
  moods, accessories and tricks — now with real motion: a breathing idle, eased arm sweeps, sweat
  that falls, a dance that slides between its four beats and a juggle on three arcs instead of
  three snapped positions. `requestAnimationFrame` runs only while a trick is running and stops
  when it ends; the sweating mood gets a 12 Hz timer instead, and an idle panel wakes about twice
  every four seconds. Quiet hours and reduced motion schedule nothing at all, and both still
  repaint on a mood change. Measured cost on the main thread: **+0.02 points idle, +0.32 at the
  worst standing case**, paint 0.10 ms at the 95th percentile.
- The SVG crab stays in the page as the fallback and is used whenever a 2D canvas context is not
  available.

### Fixed

- Nothing user-facing. Two defects were found and fixed inside this work: the body state class
  collided with the canvas element's own class name and translated the whole panel down 10 px, and
  the new alert badge counted waiting sessions on the first render instead of on the first
  document, so a panel that booted into another view announced alerts it had not seen arrive.
