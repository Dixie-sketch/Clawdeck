# Contributing to SideCrab

Thanks for looking. SideCrab is a small, opinionated project, and the easiest way to get a change
merged is to follow the rules it was built with. They are short.

## Before you start

- **Open an issue first for anything bigger than a typo.** A short description of what and why
  saves both of us a rewrite. Bug reports and feature requests have templates.
- **Windows only, and that is deliberate.** iCUE is Windows-only and so is the Xeneon Edge
  integration. PRs that add a macOS or Linux path for the companion are welcome in principle,
  but talk about it first.
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

Everything is headless: the Corsair SDK sits behind an adapter, toast emission sits behind an
adapter, and no test posts to a live crabd or depends on the wall clock.

```powershell
python -m unittest discover -s companion\tests -t companion\tests
python -m unittest discover -s notifier\tests  -t notifier\tests
python -m unittest discover lighting\tests
python -m unittest discover -s hooks\tests -t hooks\tests
node widget\tests\test_ordering.js
node widget\tests\test_panel.js
pwsh -File .\setup\tests\RunTests.ps1
```

The macOS installer's suite is Python, so it runs on either platform:

```
python -m unittest discover -s setup\tests -t setup\tests
```

CI runs the same on every push and pull request. A PR needs green CI.

**Mutation-prove anything that protects the user.** If you add a gate, break it on purpose and
watch a test fail; then fix it and watch the test pass. A gate whose test cannot fail is a gate
that reports success forever.

## Working on the widget

- `widget/DEV.md` is the developer guide: fixtures, the `?mock=` URL switches, the density and
  slot variants, and the traps that have bitten before.
- iCUE parses `widget/index.html` as **strict XML**. The CLI validator does not catch a bare `&`
  or an unclosed void element, so run this before packaging:

  ```powershell
  python -c "import xml.etree.ElementTree as ET; ET.fromstring(open('widget/index.html',encoding='utf-8').read())"
  ```

- Package with Corsair's WidgetBuilder CLI: `icuewidget validate widget` then
  `icuewidget package widget`. Do not commit the `.icuewidget`; releases attach it.

## Pull requests

- One change per PR. Keep the diff to what the title says.
- Update the docs that describe what you changed in the same PR: the README if it is
  user-facing, `docs/STATE-CONTRACT.md` if it is on the wire, `CHANGELOG.md` for anything a user
  would notice.
- Bump the version of the component you changed (`widget/manifest.json`, `VERSION` in
  `companion/crabd.py`, `__version__` in `notifier/sidecrab_toast.py`).
- Comments earn their place by stopping a future reader from making a mistake: a trap with its
  mechanism and symptom, a measured number with its provenance, a deliberate non-action. Cut the
  narration.

## Reporting a security issue

Please do not open a public issue. See [`SECURITY.md`](SECURITY.md).
