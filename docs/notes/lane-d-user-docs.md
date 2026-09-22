# Lane D: the user-facing docs

Two drop-ins: a block for the `README.md` Configuration section, and replacement
paragraphs for `docs/GETTING-STARTED.md` section 5. The version number is **provisional**.

---

## README.md, the Configuration section

Replace the existing `continuePrompts` line in the example block with the three lines
below, and add the paragraphs after it.

```jsonc
{
  "quietHours": { "start": "22:00", "end": "07:00" },  // dim panel, no glow, no toasts
  "toast":  { "enabled": true, "thresholdSec": 120, "approvalThresholdSec": 20 },
  "digest": { "enabled": false, "time": "09:00" },     // one "yesterday" toast per day
  "budget": { "dailyOutputTokens": 5000000 },          // null to clear; one toast on crossing
  "continuePrompts": ["Continue", "Run the tests"],    // extra taps on every session
  "continuePromptsByRepo": {                           // extra taps per repo
    "acme-api": ["Rebuild the report", "Run the migrations"]
  },
  "continuePromptsByPath": {                           // extra taps per directory
    "C:\\Work\\acme-api-lane-b": ["Fold the lane in"]
  },
  "panelApprovals": { "enabled": false },              // approve/deny from the panel, see below
  "recapRepos": ["C:\\Dev\\sidecrab"]                  // extra repos to count commits in
}
```

`continuePrompts` is the list every session gets. `continuePromptsByRepo` adds buttons to
the sessions in one repo, keyed on the repo name the card shows under the title, and case
does not matter. `continuePromptsByPath` adds buttons to the sessions under one directory,
keyed on an absolute path; the longest key that matches a session's folder wins. Use it
for a project git cannot name on its own: a worktree, which reports the name of the repo
it was cut from, and a folder that is not a repo at all, which reports no name.

A session's sheet then shows the three built-in buttons, then your global list, then that
project's list. Each string is both the button face and the instruction that is sent, so
keep them short and say what you mean. Twenty prompts per list, twenty per session, two
hundred characters each. A prompt that repeats a built-in or one of your global ones is
dropped rather than drawn twice.

**A prompt only works where you configured it.** The companion checks a tap against that
session's own list, so a button configured for one repo cannot be sent to a session in
another, and nothing but the strings in this file can ever be sent. If a tap reads "not
available", the prompt is not on that session's list.

`continuePrompts`, `continuePromptsByRepo`, `continuePromptsByPath` and `recapRepos` are
hand-edited only. The panel reads them but does not write them. A key it cannot parse is
skipped and named on the companion's log, and the three built-in buttons always work.

---

## docs/GETTING-STARTED.md, section 5

Replace the example block and add the two paragraphs after it.

```jsonc
{
  "quietHours": { "start": "22:00", "end": "07:00" },  // dim the panel, no toasts, no glow
  "toast":  { "enabled": true, "thresholdSec": 120 },  // toast after a session waits this long
  "digest": { "enabled": true, "time": "09:00" },      // one "yesterday" summary toast a day
  "budget": { "dailyOutputTokens": 5000000 },          // a daily token budget marker and toast
  "continuePrompts": ["Continue", "Run the tests"],    // extra next-step buttons on every card
  "continuePromptsByRepo": {                           // and per repo, for the work that repo needs
    "acme-api": [
      "Rebuild the report",
      "Run the migrations",
      "Check the seed data",
      "Roll the staging deploy back",
      "Write the release note"
    ]
  },
  "continuePromptsByPath": {                           // or per folder, for anything git cannot name
    "C:\\Work\\acme-api-lane-b": ["Fold the lane in"]
  },
  "recapRepos": ["C:\\Dev\\my-project"]                // repos whose commits count in the recap
}
```

Tap a working or finished session and the sheet offers next steps: Continue, Run the tests
and Commit + push to start with. `continuePrompts` adds buttons to every session.
`continuePromptsByRepo` adds them to one repo only, so the five buttons above appear on
`acme-api` sessions and nowhere else, and you can give each project the words you actually
use on it. The key is the repo name printed under the session title. `continuePromptsByPath`
does the same for a folder, which is what to reach for when two checkouts of one repo need
different buttons, or when the folder is not a repo.

Each string is both the face of the button and the instruction that is sent to the session,
so write it as an instruction: "Run the migrations", not "migrations". Nothing else can be
sent from the panel, and a button only works on the project you configured it for.

The moon button beside the clock is quiet hours on the glass: tap for an hour of quiet, tap
again to stay awake through tonight's window, tap again to go back to the schedule.
