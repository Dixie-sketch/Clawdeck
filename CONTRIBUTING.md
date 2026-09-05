# Contributing to SideCrab

Thanks for looking. SideCrab is a small, opinionated project, and the easiest way to get a change
merged is to follow the rules it was built with. They are short.

## Before you start

- **Open an issue first for anything bigger than a typo.** A short description of what and why
  saves both of us a rewrite. Bug reports and feature requests have templates.
- **macOS first, and the Windows build is retained.** The documented path is macOS plus a
  browser: crabd serves the panel on `127.0.0.1:9999` and you open it. The iCUE widget and the
  PowerShell installer are still in the tree, still packageable and still covered by CI's
  Windows job - the panel is the same files either way, so a change to `widget/` is a change to
  both. Nothing Windows was deleted in the port and nothing Windows should be deleted casually.
  There is no Linux route: `pick_adapter` hands any other platform an adapter that says so
  rather than pretending, and PRs that add one are welcome in principle, but talk about it
  first.
- **No new runtime dependencies without a reason.** The companion, notifier and hooks are
  standard-library Python on purpose: users install one thing (Python 3.13) and nothing else.
  The one pinned dependency is `cuesdk` for the parked glow component.

## The four rules

1. **Contract first.** The widget and the companion ship separately and are never guaranteed to
   be the same version. Any change to what `/v1/state`, `/v1/action` or `/v1/config` carries
   lands in [`docs/STATE-CONTRACT.md`](docs/STATE-CONTRACT.md) *first*, then in both sides.
   Additive fields are detected by presence; `schema` is bumped only for a breaking shape, and a
   breaking shape strands every installed widget until someone re-imports it at the desk. Avoid
   it.
2. **Honest failure.** Unknown is `null`, `available: false`, or an em-dash. Never `0`, never a
   stale value re-served as if fresh. If your change can be wrong, make it say so.
3. **Every alert must survive a healthy night.** A new threshold, gate or toast is answered by a
   replay against real data, not by reasoning. A control that fires when nothing is wrong trains
   the user to ignore the one that matters.
4. **A fixed vocabulary, never free text.** The panel can acknowledge, dismiss, pin, send one of
   a configured set of prompts, and approve or deny. Do not add a path that injects arbitrary
   text into a live session.

## Running the tests

Everything is headless: the Corsair SDK sits behind an adapter, notification emission sits behind
an adapter, launchctl and `security` reach the installer through an injected environment, and no
test posts to a live crabd or depends on the wall clock.

This is the macOS CI job, in order, and the list to run before you open a PR:

```sh
python3 -m unittest discover -s companion/tests -t companion/tests
python3 -m unittest discover -s notifier/tests -t notifier/tests
python3 -m unittest discover -s hooks/tests -t hooks/tests
python3 -m unittest discover -s setup/tests -t setup/tests
python3 -m unittest discover lighting/tests
node widget/tests/test_ordering.js
node widget/tests/test_panel.js
node --check widget/scripts/sidecrab.js
python3 -c "import xml.etree.ElementTree as ET; ET.fromstring(open('widget/index.html',encoding='utf-8').read()); print('strict-XML OK')"
python3 -c "import json; m=json.load(open('widget/manifest.json',encoding='utf-8')); print(m['id'], m['version'])"
```

The Windows job runs the same Python suites with `python`, plus the Pester suite
(`pwsh -File ./setup/tests/RunTests.ps1`), which is Windows-only: it asserts `C:\` paths and
calls DPAPI. The macOS installer's suite is the mirror image - it runs in the macOS job only,
because its wrapper tests need `/bin/sh`.

CI runs both on every push and pull request. A PR needs green CI.

**Mutation-prove anything that protects the user.** If you add a gate, break it on purpose and
watch a test fail; then fix it and watch the test pass. A gate whose test cannot fail is a gate
that reports success forever.

## Working on the widget

- **crabd serves this tree.** `http://localhost:9999` is `widget/index.html` served out of one
  directory, so a `git pull` updates the panel and a reload picks it up. There is no build step
  and no bundler: `scripts/sidecrab.js` is one flat browser script, which is what makes it
  loadable whole into a `vm` context by `widget/tests/test_ordering.js`.
- `widget/DEV.md` is the developer guide: fixtures, the `?mock=` URL switches, the density and
  slot variants, the measured slot tables, and the traps that have bitten before. A layout or
  threshold change lands there with the number it was measured at, at a named size.
- **`manifest.json` and the strict-XML constraint are kept deliberately** (`widget/DEV.md`,
  v0.30.0). The iCUE build is still packageable from these same files, so the uppercase
  `<!DOCTYPE html>`, no bare `&`, no `--` inside a comment and the CDATA block around the inlined
  sensor wrapper all stay - and `manifest.json` is still the only machine-read widget version.
  The CI parse is the only check that has ever caught a blank-panel ship, and it costs nothing:

  ```sh
  python3 -c "import xml.etree.ElementTree as ET; ET.fromstring(open('widget/index.html',encoding='utf-8').read())"
  node --check widget/scripts/sidecrab.js
  ```

- Package with Corsair's WidgetBuilder CLI on Windows: `icuewidget validate widget` then
  `icuewidget package widget`. Do not commit the `.icuewidget`; releases attach it.

## Pull requests

- One change per PR. Keep the diff to what the title says.
- Update the docs that describe what you changed in the same PR: the README if it is
  user-facing, `docs/STATE-CONTRACT.md` if it is on the wire, `CHANGELOG.md` for anything a user
  would notice.
- Bump the version of the component you changed (`widget/manifest.json`, `VERSION` in
  `companion/crabd.py`, `__version__` in `notifier/sidecrab_toast.py`). `setup/` carries no
  version of its own; say "setup" in the `CHANGELOG.md` line and date it.
- Comments earn their place by stopping a future reader from making a mistake: a trap with its
  mechanism and symptom, a measured number with its provenance, a deliberate non-action. Cut the
  narration.

## Reporting a security issue

Please do not open a public issue. See [`SECURITY.md`](SECURITY.md).
