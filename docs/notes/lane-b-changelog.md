# Lane B: CHANGELOG entries

For the orchestrator to fold into `CHANGELOG.md` under the version it assigns. Three components
move: crabd, the widget and the panel host.

---

### Added

- **`GET /v1/events` (crabd)** — the state document pushed as `text/event-stream` instead of
  waited for. Every new snapshot arrives as a frame, with a `ping` every 15 s and a
  `retry: 3000` on connect. It sits behind the same Host allowlist and same-origin gate as every
  other route, with the same `421` and `403` bodies, and caps concurrent subscribers at eight
  (the ninth gets `503 {"error":"too many subscribers"}`). Nothing is added to or removed from
  `/v1/state` and `schema` stays 5.
- **The panel takes its transport from the stream (widget)** — in the standalone host the page
  subscribes to `/v1/events` and feeds every frame through the same `acceptDoc` the poll uses.
  The 3 s poll skips while the stream is open and takes over on error, retrying the stream on a
  3 / 6 / 12 / 24 / 30 s ladder. Inside iCUE the widget polls exactly as before. The panel
  exposes `window.__sidecrabTransport` for a desk-side check.
- **A settings sheet on the glass (widget)** — a gear chip beside the clock opens a sheet that
  edits the panel's own look and behaviour: the 24-hour clock, the alert flash, crab
  accessories, touch diagnostics, the chime, three colours from a small palette, background
  transparency and chime volume. Save writes `~/.sidecrab/panel-settings.json` through the
  standalone host and the panel applies the result live, with no reload. The sheet shows in the
  standalone host only; inside iCUE the property sheet is still where settings live.
- **A chime when a session starts waiting (widget)** — a short two-note chime, synthesized in
  the browser with no audio file, played once when a session moves into `needs_input`. Off
  during quiet hours, off when the chime setting is off, never twice inside five seconds, never
  for sessions that were already waiting when the panel started, and never for the smoke test's
  own session. On by default at volume 60, with a Test chime button in the settings sheet.
- **The panel host accepts settings from the page** — a JSON web-message channel, checked on its
  source and validated against a whitelist with types and ranges. Host objects stay off. It also
  answers `host-info` with the host version, the settings path and whether a pairing code is
  present (never the code).

### Changed

- **The panel host allows audio without a user gesture** —
  `--autoplay-policy=no-user-gesture-required`. The window never takes focus, so without it the
  chime would be silent with nothing in the log to say why.
- **`panel-settings.json` is written atomically and merged.** A save from the panel keeps
  `crabdPort`, `display`, `devtoolsPort` and every prop the sheet does not edit.

### Fixed

- Nothing. This release adds surface; no reported defect is closed by it.
