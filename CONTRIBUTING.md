# Contributing to SideCrab

Thanks for looking. SideCrab is a small, opinionated project, and the easiest way to get a change
merged is to follow the rules it was built with. They are short.

## Before you start

- **Open an issue first for anything bigger than a typo.** A short description of what and why
  saves both of us a rewrite. Bug reports and feature requests have templates.
- **Windows only, and that is deliberate.** The companion is a Windows service, the notifier
  raises Windows toasts and the panel host is a Windows window. PRs that add a macOS or Linux
  path for the companion are welcome in principle, but talk about it first.
- **No new runtime dependencies without a reason.** The companion, notifier and hooks are
  standard-library Python on purpose: users install one thing (Python 3.13) and nothing else.
  The panel host (`panel-host/`) is the deliberate exception: a small C# WinForms app on the
  .NET 10 Desktop Runtime and the WebView2 Runtime, part of the default install and declinable
  with `Install-SideCrab.ps1 -SkipPanel`.

## The four rules

1. **Contract first.** The panel page, the companion and the host are versioned separately and
   are never guaranteed to be the same version. Any change to what `/v1/state`, `/v1/action` or
   `/v1/config` carries lands in [`docs/STATE-CONTRACT.md`](docs/STATE-CONTRACT.md) *first*, then
   in both sides. Additive fields are detected by presence; `schema` is bumped only for a
   breaking shape, which strands every consumer that has not updated. Avoid it.
2. **Honest failure.** Unknown is `null`, `available: false`, or an em-dash. Never `0`, never a
   stale value re-served as if fresh. If your change can be wrong, make it say so.
3. **Every alert must survive a healthy night.** A new threshold, gate or toast is answered by a
   replay against real data, not by reasoning. A control that fires when nothing is wrong trains
   the user to ignore the one that matters.
4. **A fixed vocabulary, never free text.** The panel can acknowledge, dismiss, pin, send one of
   a configured set of prompts, and approve or deny. Do not add a path that injects arbitrary
   text into a live session.

## Running the tests

Everything is headless: toast emission sits behind an adapter, the setup suite lifts its pure
decisions out by AST and installs nothing, and no test posts to a live crabd or depends on the
wall clock.

```powershell
python -m unittest discover -s companion\tests -t companion\tests
python -m unittest discover -s notifier\tests  -t notifier\tests
python -m unittest discover -s hooks\tests -t hooks\tests
node widget\tests\test_ordering.js
pwsh -File .\setup\tests\RunTests.ps1
dotnet test panel-host\SideCrab.Panel.Tests      # the panel host's pure decisions (needs the .NET 10 SDK)
```

CI runs the same on every push and pull request. A PR needs green CI.

**Mutation-prove anything that protects the user.** If you add a gate, break it on purpose and
watch a test fail; then fix it and watch the test pass. A gate whose test cannot fail is a gate
that reports success forever.

## Working on the panel page

- `widget/DEV.md` is the developer guide: fixtures, the `?mock=` URL switches, the density and
  slot variants, and the traps that have bitten before.
- `widget/` is the live asset tree, served as-is by crabd at `http://127.0.0.1:2722/panel/` and
  shown by `panel-host/`. There is nothing to package and nothing to import.
- Its version is `widget/version.json`. CI parses that file and parses `widget/index.html` as
  HTML; both are merge gates.
- **Nothing in current product surface may name the retired vendor integration.** CI greps the
  tree for its names and fails on a hit; the exact pattern and the exemptions are in
  `.github/workflows/ci.yml`, step "No retired vendor integration in current surface". Dated
  history under `docs/history/`, `docs/findings/`, `CHANGELOG.md`, `docs/BACKLOG.md`,
  `widget/DEV.md`, `docs/notes/` and `setup/tests/` are exempt: they are the record, and
  rewriting a record is falsifying it. If you find something historically useful, move it to
  `docs/history/` with a date and a sentence saying why, and delete the instruction.

## Pull requests

- One change per PR. Keep the diff to what the title says.
- Update the docs that describe what you changed in the same PR: the README if it is
  user-facing, `docs/STATE-CONTRACT.md` if it is on the wire, `CHANGELOG.md` for anything a user
  would notice.
- Bump the version of the component you changed (`widget/version.json`, `VERSION` in
  `companion/crabd.py`, `__version__` in `notifier/sidecrab_toast.py`, `Program.Version` and the
  csproj `<Version>` in `panel-host/SideCrab.Panel/`). They move independently;
  `Install-SideCrab.ps1 -Status` prints all three side by side.
- Comments earn their place by stopping a future reader from making a mistake: a trap with its
  mechanism and symptom, a measured number with its provenance, a deliberate non-action. Cut the
  narration.

## Reporting a security issue

Please do not open a public issue. See [`SECURITY.md`](SECURITY.md).
