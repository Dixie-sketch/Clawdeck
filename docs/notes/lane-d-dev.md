# Lane D: widget/DEV.md material

Drop-in section for `widget/DEV.md`, written in that file's style. The version number is
**provisional** and the orchestrator's to assign; the text assumes widget `0.31.0`.

---

## v0.31.0: a continue vocabulary per project, and the row that had to give way

The tap-to-continue buttons were one list for every session: the three hardcoded defaults
plus whatever the feed carried at the top level in `continuePrompts`. crabd now also
serves `sessions[].continuePrompts`, that session's own project prompts, and the panel
draws builtins, then globals, then those.

### One builder, two surfaces

`continueButtons(s)` is the whole rendering rule and both surfaces call it: `syncContinue`
for the session sheet and `detailMain` for the Detail page. They had two copies of the
same loop, and a per-session list would have been two chances to disagree about the order.
The buttons still carry `data-continue-prompt` and `data-continue-label`, so the tap path
is untouched and both surfaces still reach `onSheetContinue`.

The prototype trap is worth keeping in mind if this function is ever rewritten: the
duplicate check is `Object.create(null)`, because a plain `{}` inherits `Object.prototype`
and `seen['constructor']` reads truthy, so a prompt with that text would vanish as a
duplicate it never had. There is a test for exactly that.

The Detail page's repaint signature now includes the button set. It did not, so a config
edit repainted the sheet's row and left the page showing the old buttons until something
else in the session happened to move.

### The width was never the problem; the height was

Measured with headless Edge 153 over CDP, device metrics pinned, DPR 1, a fresh
`--user-data-dir` per run, served from `widget/` over `http://127.0.0.1:8771`, densities
set with `&density=`, the sheet opened with `&sheet2=` and the Detail page with the
shipping `laneCOpenDetail`.

**The sheet panel is already full at ten buttons.** Its `max-height` is 92% and the events
list is the only child that could shrink, so it absorbed the overflow: read off the DOM at
2560x720 with ten buttons, the events list was at 59.59 px of its 154 px of content while
the button row kept all 260.78 px. That is the behaviour this lane had to preserve for
modest lists and could not preserve at the cap.

**At the configured cap, 3 builtins + 20 global + 20 project = 43 buttons, the old row
walked the actions off the glass:**

| slot | what overflowed | below the panel | off the glass |
|---|---|---|---|
| 2560x720 | sheet `.sheet-pin-actions` | 411.58 px | 382.77 px |
| 2536x696 | sheet `.sheet-pin-actions` | 398.78 px | 370.94 px |
| 2560x720 | Detail `.dv-continue` | 810.95 px | 782.16 px |
| 2536x696 | Detail `.dv-continue` | 782.80 px | 754.97 px |

The sheet also grew the page a 383 px scroll at 2560x720, which is the reading a per-zone
probe would have missed.

**The fix is shrink and scroll, never clamp**, the rule `.dv-question` already keeps. The
continue block became shrinkable with `min-height: 0` and its button row scrolls. A clamp
would hide buttons the operator configured with nothing to say they exist, while a
shrinkable row gives way only once the panel is genuinely full.

**An equal shrink factor was not good enough.** Flex shares a shrink in proportion to
factor times base size, so with the events list at factor 1 the ten-button row lost
63.33 px, which is three buttons behind a scroll on the one surface whose point is that
the words are on the glass. The events list now has `flex-shrink: 200`, so it absorbs the
whole overflow until it is itself at zero (about thirteen buttons here) and only then does
the row scroll. The row's loss at ten buttons: 63.33 px at factor 1, 6.65 px at 24,
0.83 px at 200. The factor is inert on every other sheet, because with nothing else
shrinking it cannot matter, and the events list carries its own scroll either way.

### The numbers as shipped

Zero overflow everywhere below, page overflow 0x0, no console errors, and the two
densities are identical throughout, which is the expected reading for this zone.

| case | buttons | block | row visible / content | scrolls | smallest button |
|---|---|---|---|---|---|
| sheet, 2560x720 | 10 | 259.94 | 199.23 / 200 | no | 64.00 |
| sheet, 2536x696 | 10 | 251.34 | 192.92 / 194 | no | 62.00 |
| sheet, 2560x720 | 6 | 186.70 | 126 / 126 | no | 126.00 |
| sheet, 2536x696 | 6 | 180.42 | 122 / 122 | no | 122.00 |
| Detail, 2560x720 | 10 | 376.44 | 331.09 / 331 | no | 60.47 |
| Detail, 2536x696 | 10 | 363.52 | 320.08 / 320 | no | 58.45 |
| sheet at the cap, 2560x720 | 43 | 313.72 | 253.02 / 703 | yes | 60.47 |
| sheet at the cap, 2536x696 | 43 | 303.06 | 244.64 / 681 | yes | 58.45 |
| Detail at the cap, 2560x720 | 43 | 435.31 | 389.97 / 1211 | yes | 60.47 |
| Detail at the cap, 2536x696 | 43 | 421.59 | 378.16 / 1170 | yes | 58.45 |

Every smallest button clears the 48 px fingertip floor, at the cap included.

**The unchanged case is unchanged, and that is measured rather than asserted.**
Twenty-eight captures are **byte-for-byte identical to the pre-lane tree**, every rect and
every scroll height: twelve on fixtures that carry no project prompts (`?mock=normal` and
`?mock=attention`, the detail sheet and the Detail page, both slots, both densities), and
sixteen on the ACTION sheet (`&sheet=first` on `rework`, `attention`, `question` and
`rework&approval=1`, both slots, both densities). The action sheet matters because
`.sheet-events` is shared: it carries the continue row in the markup and renders zero
buttons in it, so the shrink factor is provably inert there rather than argued to be.

### Verified

`node --check scripts/sidecrab.js`, strict-XML parse of `index.html` (ElementTree and
minidom), JSON parse of all 21 fixtures, `node widget/tests/test_ordering.js` **185/185**
(was 173), and `python -m unittest discover -s companion/tests -t companion/tests`
**1243 tests OK** (was 1220), 1 skipped.

The per-session refusal is proved two ways and both mutations were run against the
shipping code rather than reasoned about:

- Remove the per-session gate, so the handler reads the global whitelist again: 2 failures,
  the queue refuses this project's own prompt (`400 != 204`).
- Flatten the whitelist across every configured project, which is what "per repo" looks
  like when the per-session half is dropped: 2 failures, the refusal test gets its 204
  (`204 != 400`).

The flattened mutant is also installed on a live builder inside the suite itself, so the
refusal test carries its own proof that it can fail.

### The pictures

`docs/notes/lane-d-sheet-sidecrab.png` and `lane-d-sheet-acme-api.png`: the same document,
two sessions in different repos, each sheet carrying its own project's buttons.
`lane-d-detail-sidecrab.png`: the same vocabulary on the Detail page. All three at
2560x720, comfortable, `?mock=rework`.
