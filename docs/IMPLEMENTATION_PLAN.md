# Implementation plan: the macOS + browser port

Working file for the port. `docs/PORT-NOTES.md` is the durable record (seams, measurements,
decisions); this file tracks stage status and is removed when every stage is Complete.
The stages are the port brief's phases, in order; no stage starts before the previous one
is green and committed.

## Stage 0: baseline and the platform seam
**Goal**: every existing suite triaged on macOS; one platform dispatch seam in crabd with
Windows and macOS implementations behind an identical method surface, injectable in tests.
**Success Criteria**: suites green on macOS with Windows-only tests skipped for a stated
reason; no `sys.platform` checks scattered through the builder; no behaviour change.
**Tests**: platform selection is pinned (import-time choice, injectable), Windows tests skip
with reasons, the notifier drive-letter test skips off Windows.
**Status**: Complete (2026-09-04; spec and quality reviews passed; companion 1166 tests, 2 Windows skips)

## Stage 1: one port, one origin
**Goal**: crabd on 9999 serves the panel and the API; the origin gate admits only the panel's
own origin; a second header gate on every mutating endpoint; static serving off the builder.
**Success Criteria**: the origin matrix in the brief passes row by row; both gates
mutation-proven; port collision fails loudly; traversal is 404; static reads never block
`/v1/state`; TRANSPORT section in the contract and SECURITY threat model amended.
**Tests**: `companion/tests/test_crabd_panel.py` (origin matrix, header gate, static serving,
traversal, bind address, collision); `hooks/tests/test_hooks_fragment.py`;
`widget/tests/test_panel.js` (transport half).
**Status**: Complete (2026-09-04; three gates mutation-proven independently; companion 1278 tests; reviewed twice)

## Stage 2: host metrics on macOS
**Goal**: `HostSampler` reads `host_statistics` / `host_statistics64` / `hw.memsize` through
ctypes on macOS with the delta arithmetic and the three failure tiers unchanged.
**Success Criteria**: the ported sampler tests pass against fixtures; the `nice` bucket
decision is written and pinned; Windows reader intact.
**Tests**: `companion/tests/test_crabd_host_darwin.py` (28 tests; two live read-only checks on darwin).
**Status**: Complete (2026-09-04; reviewed twice)

## Stage 3: fleet state on launchd
**Goal**: `FleetReader` reads `launchctl` on macOS; `fleet.glow` decision documented.
**Success Criteria**: running/stopped/absent/unknown from recorded output; missing binary,
non-zero exit, timeout and garbage land on unknown; off the request path.
**Tests**: `companion/tests/test_crabd_fleet_darwin.py` (23 tests against recorded launchctl output).
**Status**: Complete (2026-09-04; reviewed twice)

## Stage 4: the limits token
**Goal**: the long-lived token in the login Keychain via `/usr/bin/security` (stdin, never
argv); the CLI credential read from the Keychain when the file is absent.
**Success Criteria**: precedence unchanged; token never in state, health, logs or argv;
user-facing strings name the macOS command.
**Tests**: `companion/tests/test_crabd_token_darwin.py` (41 tests; the argv-leak test; a nine-module kill-switch pin; one opt-in live round trip).
**Status**: Complete (2026-09-04; reviewed twice)

## Stage 5: install, hooks and the service
**Goal**: `setup/install.sh`, `update.sh`, `uninstall.sh`, `--status`, `doctor`; LaunchAgent
plist; hooks at 9999.
**Success Criteria**: idempotent hook merge with backup, chained status line, plist shape,
Python detection.
**Tests**: `setup/tests/test_setup_*.py` driven against a temporary HOME (172 tests).
**Status**: Complete (2026-09-04; merged; reviewed twice)

## Stage 6: the panel in a browser
**Goal**: localStorage settings adapter, pairing code in the settings sheet, sensors row
without temperatures, phone-width breakpoints, keyboard and pointer equivalents for every
gesture, dev flags still mock-gated.
**Success Criteria**: `widget/tests/test_panel.js` green; every `?mock=` fixture renders as
documented from port 9999.
**Tests**: `widget/tests/test_panel.js` (383 checks against a DOM shim of the shipping index.html).
**Status**: Complete (2026-09-04; merged; reviewed twice; layouts measured in Chromium)

## Stage 7: the notifier
**Goal**: macOS user notifications through an injection-safe emitter; same deciders, ledgers,
quiet hours and snooze.
**Success Criteria**: emit matrix ported; a quote, backslash or newline in a title cannot break
out of the emitter; no Approve/Deny action.
**Tests**: `notifier/tests/test_mac_adapter.py` (554 notifier tests in all).
**Status**: Complete (2026-09-04; merged; reviewed twice; one live test notification seen by the operator)

## Stage 8: documentation and release
**Goal**: README, GETTING-STARTED, CHANGELOG, STATE-CONTRACT, SECURITY, CONTRIBUTING, CLAUDE.md
rewritten for macOS; CI `macos-latest` job.
**Success Criteria**: every decision this port made is recorded in the contract or notes; the
live install on this Mac (operator-approved 2026-09-04, on condition that everything it touches
is backed up first: `~/.claude/settings.json`, `~/.sidecrab/`, `~/Library/LaunchAgents/`) leaves a
running LaunchAgent and a panel at http://localhost:9999 in Safari and Chrome.
**Tests**: CI green on macOS; the definition-of-done walkthrough recorded in PORT-NOTES.
**Status**: In Progress (live install done 2026-09-04 with a full backup; docs under way)
