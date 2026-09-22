# Lane D: CHANGELOG bullets

For the wave's entry in `CHANGELOG.md`. The version numbers are **provisional** and the
orchestrator's to assign; these bullets assume crabd `0.33.0` and widget `0.31.0`.

---

### Added

- **A continue vocabulary per project.** The tap-to-continue buttons were one global list
  for every session. `config.json` now takes `continuePromptsByRepo`, keyed on the repo
  name the card already shows, and `continuePromptsByPath`, keyed on an absolute path
  prefix of a session's cwd. A session's sheet and its Detail page draw the three
  builtins, then the global `continuePrompts`, then its own project's prompts. Both new
  keys are hand-edited only and are not writable over HTTP.
- **`sessions[].continuePrompts`** (contract v0.33.0, additive): that session's project
  prompts, or the key is absent. Presence is the feature detection, so an older panel
  renders exactly what it renders today.

### Changed

- **`POST /v1/action queue-continue` is checked per session.** The allowlist is the
  builtins plus the global extras plus that session's project prompts, so a prompt
  configured for one repo is refused for a session in another with the same 400 an unknown
  prompt has always had. No new status code and no new error body. The set stays
  server-side and stays a whitelist: a project map widens what a given session may say,
  never who may say it.
- **The sheet's continue row gives way before the panel does.** A longer list used to push
  the Pin row 411.58 px below the sheet and 382.77 px off the glass at the configured cap.
  The row now shrinks and scrolls once the panel is genuinely full, the events list gives
  way first so a modest list keeps every button on the glass, and the 48 px fingertip
  floor is unchanged. Measured zero overflow at 2560x720 and 2536x696 in both densities.

### Fixed

- The Detail page's continue buttons follow a config edit. The page's repaint signature did
  not include the button set, so a new prompt reached the sheet and left the page showing
  the old row until something else in the session moved.
