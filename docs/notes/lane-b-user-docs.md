# Lane B: README, GETTING-STARTED and UPGRADING paragraphs

For the orchestrator to place. Written for someone who has SideCrab running and has never read the
contract.

---

## README — the panel updates itself now

The panel used to ask the companion for a new picture every three seconds. It now keeps one
connection open and the companion pushes each new picture as it has one, so a question that needs
you appears on the glass when it is asked rather than up to three seconds later. If that connection
drops — you restart the companion, or update it — the panel goes back to asking every three
seconds and keeps trying the faster route in the background, so there is nothing to restart and
nothing to configure. The worried crab and the "data as of" banner still mean exactly what they
meant: the panel cannot see the companion, however it was talking to it.

This applies to the standalone panel. Inside iCUE the widget works exactly as it always has.

## README — settings on the glass

The standalone panel has its own settings. Tap the gear beside the clock and you get a sheet with
the things you would otherwise edit by hand:

- the 24-hour clock, the flash on a new alert, the crab's accessories, touch diagnostics
- the chime, and how loud it is
- text, accent and background colour, from a small palette
- background transparency

Tap **Save** and the panel applies it immediately — no restart, and no reload that loses your
place. It is written to `~/.sidecrab/panel-settings.json`, which is the same file you can still
edit by hand; the sheet leaves everything else in it alone, including which port the companion is
on and which monitor the panel is pinned to.

Two things are deliberately not on the sheet: the companion's port and the monitor, because a
mistake in either leaves you with a window you cannot see; and the approval pairing code, which is
never handed to the page at all.

Inside iCUE there is no gear: iCUE's own property panel is where those settings live, and a widget
cannot write them back.

## README — the chime

When a session stops and asks you something, the panel plays a short two-note chime. Once, for
that question — not again while it waits, and not for a question that was already waiting when the
panel started. It is quiet during quiet hours, and it will not play more than once in five seconds
however many sessions land at the same moment.

It is on by default. Turn it off, or change how loud it is, in the settings sheet; **Test chime**
plays it at whatever the slider says so you can set it against the room rather than by guessing.

The chime needs the standalone panel host — a browser tab at the panel's address will play it too,
once you have tapped something on the page, which is the browser's own rule about sound.

## GETTING-STARTED — after "Install the standalone panel"

Once the panel is on the Edge, tap the gear beside the clock. That is where the panel's colours,
the clock format, the chime and the rest live, and it writes them for you. Try **Test chime**
first: if you hear nothing, the panel host is not running the panel (a browser tab of your own will
not have the audio permission the host gives it), and `~/.sidecrab/logs/panel.log` will say what it
loaded.

## UPGRADING — from the previous release

Nothing to do, and nothing to configure.

- **The companion serves one new address, `/v1/events`.** It is on the same loopback port behind
  the same protections as everything else, and nothing outside your machine can reach it. Update
  the companion first if you can: an older companion simply has no such address, and the panel
  falls back to asking every three seconds exactly as it does today.
- **The panel window now accepts settings from the page it shows, and only from that page.** It
  checks where every message came from and accepts a fixed list of settings with a fixed range for
  each; anything else is dropped and logged. Host objects stay off.
- **Audio is allowed without a tap.** The panel window never takes keyboard focus, so without this
  the chime would be silent with nothing to tell you why.
- **The chime is on after the upgrade.** If you would rather it were not, the gear beside the clock
  is a tap away — or set `"chime": false` in the `props` block of
  `~/.sidecrab/panel-settings.json`.
- **Your `panel-settings.json` is safe.** A save from the sheet rewrites the file with everything
  else in it intact, atomically, so an interrupted save cannot leave you with a panel that has
  forgotten its port and its monitor.
