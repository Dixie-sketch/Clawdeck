# Lane D: the state-contract section

Drop-in section for `docs/STATE-CONTRACT.md`, written in that file's style. **The version
number is provisional** and the orchestrator's to assign; the text below assumes crabd
`0.33.0` and widget `0.31.0`, and every `v0.33.0` tag in the code carries the same
assumption.

---

## v0.33.0 (provisional) (2026-09-21, ADDITIVE: a continue vocabulary per project; schema stays 5)

One additive per-session field and two new config keys. Nothing existing moves or changes
meaning, `schema` stays **5**, and an older widget ignores all of it.

### 1. `sessions[].continuePrompts`: this session's own extra continue buttons

```jsonc
"sessions": [
  { "id": "…",
    "repo": "acme-api",
    "continuePrompts": ["Rebuild the report", "Run the migrations"] }
]
```

**Present only when this session has project prompts, and absent otherwise.** Presence is
the feature detection, as it is for `queuedContinue` and `host`. The key is deliberately
**not** served as `[]`, which is what the top-level `continuePrompts` does: at the top
level always-present is the contract and empty is the ordinary case, while an empty list
on a row would be a claim that crabd looked at this project and found it configured with
nothing. Absent stays absent.

The strings are the operator's own, already filtered: crabd drops anything that is
builtin or already in the top-level list, so a consumer appends them without checking.
The rendering order is builtins, then the top-level `continuePrompts`, then these.

The top-level `continuePrompts` is **unchanged** in every respect, including being
always present and `[]` when unconfigured.

### 2. `POST /v1/action queue-continue` is now checked per SESSION

The whitelist a tap is checked against was the builtins plus the top-level extras. It is
now the builtins, plus the top-level extras, plus **that session's own**
`sessions[].continuePrompts`. Consequences for anything driving crabd out of band:

- A prompt configured for repo X and POSTed for a session in repo Y is refused **400**
  `{"error":"prompt must be one of the configured continue prompts"}`, the same answer an
  invented string has always had. There is no new status code and no new error body.
- The gate order is unchanged: shape before existence. An unknown `sessionId` carrying a
  builtin prompt is still **404** `{"error":"unknown session"}`, and an unknown session
  carrying another project's prompt is **400**, because the prompt is checked first. An
  unknown session is therefore checked against the global set, never against a project's.
- The set is still server-side, still a whitelist, and still unwidenable over HTTP: both
  new config keys are file-only and deliberately absent from the `/v1/config` whitelist.
  A project map widens what a **given session** may say, never who may say it.
- The session's `repo` and `cwd` are read off the **served document**, not re-derived. The
  allowlist is therefore built from the same `repo` the consumer drew its buttons from; a
  second derivation could answer differently inside one poll (the git cache is 30 s) and
  refuse a button that was on the glass.

### 3. Two new `config.json` keys, file-config only

```jsonc
{
  "continuePromptsByRepo": {
    "acme-api": ["Rebuild the report", "Run the migrations"],
    "orbit-desktop": ["Sign the installer"]
  },
  "continuePromptsByPath": {
    "C:\\Work\\acme-api-lane-b": ["Fold the lane in"]
  }
}
```

- **`continuePromptsByRepo`** is keyed on `sessions[].repo`, the name crabd derives from
  the origin remote, matched **case-insensitively**. Two keys differing only in case do
  not merge: the first wins and the second is dropped with a `config.json` line.
- **`continuePromptsByPath`** is keyed on an absolute path **prefix** of
  `sessions[].cwd`, matched at a path boundary, separator- and case-normalised, and the
  **longest matching key wins** (one key only, never several). It exists because
  `sessions[].repo` cannot name every project: measured 2026-09-21, a release tree and a
  linked worktree of the same repo both read `sidecrab`, and a session whose cwd is not a
  repo reads `null`. A relative key is dropped with a line, because it could never match.
- A session's extras are the repo list then the path list, deduped against each other,
  against the top-level extras and against the builtins.
- **Validated exactly as the top-level list is**, and parsed defensively because this is
  hand-edited JSON: a map that is not an object, a value that is not a list, non-strings,
  blanks, over-long entries and duplicates are dropped, each with one `config.json` line
  on stderr rather than silence or a crash. Per list: 20 prompts of at most 200
  characters. Per session: 20 combined, the repo list filling first. Per map: 50 keys,
  which bounds the parse rather than a request. **The builtins can never be lost to a
  typo**, which is the same guarantee the top-level list has.
