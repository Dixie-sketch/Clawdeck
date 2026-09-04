# Port notes: SideCrab from Windows + iCUE to macOS + any browser

The durable record of the port: every Windows- or iCUE-specific seam found while reading,
what each became, every measurement taken, and every decision the port brief asked for. Kept
current as the port proceeds. Stage status lives in `docs/IMPLEMENTATION_PLAN.md` until the
port is complete.

## How the brief and CLAUDE.md were reconciled

- Commit messages carry no model attribution trailer: the repository's own history and the
  operator's global instructions both forbid it, and they win over the session default.
- `docs/IMPLEMENTATION_PLAN.md` holds stage status (removed when done); this file holds the
  seams and decisions and persists.
- The PowerShell installer and its Pester suite stay in the tree untouched. The macOS installer
  and its Python suite are added beside them; nothing Windows is deleted.
- Work is on the `macos-port` branch, one commit per behaviour, the full suite before each.

## Baseline: the existing suites on macOS (2026-09-04, Python 3.14.6, node 24.1.0)

| Suite | Result | Triage |
|---|---|---|
| companion (`companion/tests`) | 1131 ran, OK, 3 skipped | the 3 skips are (a) genuinely Windows-only and already marked: the DPAPI round trip (`test_crabd.py:1425`) and the two live `GetSystemTimes` reads (`test_crabd.py:9744`) |
| notifier (`notifier/tests`) | 500 ran, 1 failure | `test_decider.AdapterTests.test_icon_uri_keeps_the_drive_colon_unescaped` asserts a drive letter in the toast image URI. Bucket (a): the assertion is about Windows paths, so it is skipped off Windows with a reason, never deleted |
| hooks (`hooks/tests`) | 14 ran, OK | portable as-is |
| lighting (`lighting/tests`) | 104 ran, OK | portable as-is; the component stays parked and has no macOS counterpart |
| widget ordering (`node widget/tests/test_ordering.js`) | 140/140 | portable as-is |
| setup (Pester) | not runnable, no `pwsh` | bucket (a): asserts `C:\` paths and calls DPAPI. Stays as the Windows suite; a Python suite covers the shell installer |

Bucket (b), accidentally Windows-coupled but portable, and bucket (c), real bugs, were empty
at baseline. Anything found later is added to the seam table below with its bucket.

## Measurements

- **Where Claude Code keeps its credential on this Mac (Phase 4, problem two).**
  `~/.claude/.credentials.json` does not exist. The login Keychain holds a generic-password
  item with service `Claude Code-credentials` and account equal to the login user name,
  modified the same day Claude Code last ran (Claude Code 2.1.260). The payload was not
  read in this session (reading the secret was refused by the session's permission policy),
  so its JSON shape is unmeasured. The reader therefore parses the item as the same
  `{"claudeAiOauth": {...}}` document the file carries and, if it does not parse that way,
  serves `available: false` with a note that names the Keychain item. It never guesses a
  token.
- **Keychain access prompts.** A process that is not on the item's access-control list gets
  a macOS dialog the first time it reads the item. For a LaunchAgent that is `python3`, so
  the first read after install prompts once in the operator's session; "Always Allow" ends
  it. Documented in GETTING-STARTED; the reader treats a refused read as absent.
- **Python on this Mac.** `/usr/bin/python3` is Apple's 3.9.6, below the project floor;
  Homebrew provides 3.13 and 3.14 under `/opt/homebrew/bin`. The installer must find a real
  3.13+ and reject the Apple stub by version, not by path.
- **Port 9999** was free on this machine at the time of measurement.
- **Existing hooks in the operator's `~/.claude/settings.json`**: one unrelated
  `UserPromptSubmit` command hook, no `statusLine`, no `allowedHttpHookUrls`. The installer
  must preserve that hook.

## Seams (filled from the read-through; line numbers as of the baseline commit 5366719)

_(table follows once the survey lands)_

## Decisions the brief asked for

_(recorded per phase as they are made)_
