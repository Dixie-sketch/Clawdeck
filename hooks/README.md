# SideCrab hooks

`settings-hooks-fragment.json` is the `hooks` object merged into `~/.claude/settings.json`
by `setup/Install-SideCrab.ps1`. Ten events, each on its own crabd route.

| Event | Route | Type | Timeout | What crabd does with it |
|---|---|---|---|---|
| `SessionStart` | `/v1/hook/session-start` | `command` | 3 s | the session opened: `idle`, not `working` |
| `UserPromptSubmit` | `/v1/hook/prompt` | `http` | 3 s | a turn started: `working`, clears a question and a failure |
| `Notification` | `/v1/hook/notification` | `http` | 3 s | `needs_input`, unless the type says otherwise |
| `Stop` | `/v1/hook/stop` | `http` | 5 s | the turn finished, and the tap-to-continue answer |
| `StopFailure` | `/v1/hook/stop-failure` | `http` | 3 s | the turn died on an API error: `failed` |
| `SubagentStart` | `/v1/hook/subagent-start` | `http` | 3 s | an exact subagent count, and the agent's name |
| `SubagentStop` | `/v1/hook/subagent-stop` | `http` | 3 s | pairs with the start by `agent_id` |
| `PermissionRequest` | `/v1/hook/permission` | `http` | 60 s | the panel-approval long poll |
| `SessionEnd` | `/v1/hook/session-end` | `http` | 3 s | `gone` |
| `PreCompact` | `/v1/hook/precompact` | `http` | 3 s | a compaction is running |

## The eight fire-and-forget `http` hooks

Claude Code POSTs the hook's stdin JSON to the `url` itself, with
`Content-Type: application/json`, and reads the response as the hook's decision. crabd
answers **204 with no body** and records the hook afterwards, so nothing it does is ever
in front of the session.

```jsonc
"UserPromptSubmit": { "type": "http", "url": ".../v1/hook/prompt", "timeout": 3 }
```

- **No `curl.exe`, no shell, no process spawn per event, and no `|| exit 0`.** The
  dependency on a `curl.exe` being on PATH, and on whatever shell the CLI picked, is gone.
- **`timeout` is in seconds** (the CLI multiplies it by 1000). 3 s is a ceiling nothing
  here comes near: the handler reads a body, writes a 204, and takes a lock.
- **An empty body is fine.** The CLI parses an empty response as `{}` and logs that it
  did, which is why a 204 is a legitimate answer rather than a warning.

**Fail-open, measured rather than assumed.** A hook whose endpoint is refused, errors,
times out or answers a non-2xx produces an empty decision for every event SideCrab
registers, plus a line in the CLI's own log. Only `PreToolUse` turns a failed hook into a
denial, and SideCrab registers no `PreToolUse`. A stopped crabd means Claude stops
normally and permission falls back to the terminal dialog; nothing about crabd being down
can wedge a session.

## `SessionStart` is the one `command` hook, and it has to be

```
curl.exe -s -m 2 -X POST --data-binary @- http://127.0.0.1:2722/v1/hook/session-start || exit 0
```

The CLI **skips `type: "http"` handlers on `SessionStart` and `Setup`** and logs
`HTTP hooks are not supported for SessionStart`. An http entry there does not fire slowly
or fail loudly: it never runs, and crabd never learns that a session opened.

- `curl.exe` is the Windows-native one in `C:\Windows\System32` - not Git Bash's.
- `-m 2` caps the whole call at 2 s; a refused connection fails in microseconds.
- `|| exit 0` swallows curl's exit code so a stopped crabd can never surface an error
  in Claude Code. `exit 0` behaves the same under `cmd.exe` and any POSIX shell.
- `--data-binary @-` streams stdin. curl buffers it and sends `Content-Length`, but
  crabd accepts chunked framing too.

## The two-way hooks

`Stop` and `PermissionRequest` are the two where crabd's answer matters.

- **Stop to `/v1/hook/stop`.** crabd answers within ~2 s: `{}` to let Claude stop, or
  `{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"<prompt>"}}` to feed a
  tap-to-continue prompt back in as NON-ERROR feedback (the conversation continues so the model
  can act on it - the binary's own schema text). `decision:"block"` also continues but paints
  "Stop hook error occurred" and labels the nudge an error to the model; it is retained in crabd
  as an executable fallback only. `timeout` 5 s bounds crabd's 2 s answer.
- **PermissionRequest to `/v1/hook/permission`.** crabd long-polls (up to 55 s) for an
  Approve/Deny tap from the widget, then returns
  `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"|"deny"}}}`.
  On no-tap / timeout / `panelApprovals` disabled it returns **no `hookSpecificOutput`** (`{}`), so
  the terminal dialog appears exactly as today - it NEVER auto-allows (docs\STATE-CONTRACT.md,
  v0.12.0 item 4). `timeout` 60 s sits just past crabd's 55 s poll.

## No `matcher` on any entry, deliberately

`Notification` was the obvious candidate: the CLI matches it on notification type, and
crabd wants that type. It gets it without a matcher - the type rides in the payload as
`notification_type`, whether or not one is declared.

A matcher **filters**. The live type list has fifteen values, differs from the published
reference's list in both directions, and moves between CLI releases. A fragment that
enumerates types is a fragment that silently drops the next one added, and the type crabd
most wants to see is the one it has never heard of.

Tool events would need matchers if SideCrab ever consumed them. It does not.

**No `PreToolUse`/`PostToolUse`, deliberately (v0.19.0).** They were the obvious way to tell the
panel a session is alive again after the operator answers a permission dialog in the app - and they
were rejected: they would put an HTTP round trip in front of every tool call in every session, on a
machine whose loopback drops SYN-ACKs. crabd reads that same evidence out of the transcript it
already parses. See `docs/STATE-CONTRACT.md` v0.19.0 §2 and §4.

## Verified against the shipped Claude Code (claude.exe v2.1.278, 2026-09-22)

Confirmed by inspecting the shipped binary (a Bun-compiled `claude.exe`) and
code.claude.com/docs/en/hooks. Where the two disagree the binary wins: it is what POSTs.

- Hook handler `type` accepts `command`, **`http`**, `mcp_tool`, `prompt`, `agent`. The
  `http` handler config carries `url` (not `command`), optional `headers` and
  `allowedEnvVars`, and a `timeout` in **seconds** (default 600). It POSTs the stdin
  document as the request body.
- **HTTP hooks are skipped for `SessionStart` and `Setup`,** and for those two only. The
  dispatcher's own test is `event === "SessionStart" || event === "Setup"`.
- The response is `ok` for any 2xx. An empty body is parsed as `{}`. A non-2xx, a
  refused connection, a timeout and a proxy mismatch all produce an empty decision for
  every event but `PreToolUse`, plus a warning line in the CLI's log.
- **`StopFailure` carries `error`, not `error_type`.** The reference documents
  `error_type`; the binary builds the payload with `error` and its own matcher metadata
  says `fieldToMatch: "error"`. crabd reads both. It fires **instead of** `Stop`, and its
  output and exit code are ignored.
- **`SubagentStart` carries `agent_id` and `agent_type`; `SubagentStop` carries both
  too,** plus `agent_transcript_path` and `last_assistant_message`. The backlog's CD-29
  row was written when the stop carried no id.
- **`Notification` carries `notification_type`.** The live enum is `permission_prompt`,
  `idle_prompt`, `auth_success`, `elicitation_dialog`, `agent_needs_input`,
  `agent_completed`, `elicitation_url_dialog`, `worker_permission_prompt`,
  `push_notification`, `computer_use_enter`, `computer_use_exit`,
  `quota_auto_resume_fired`, `quota_auto_resume_stale`, `quota_auto_resume_disabled`,
  `model_refusal_fallback`.
- **Stop** continuation: BOTH `additionalContext` (non-error, shipped) and top-level
  `decision:"block"` (error-labelled, fallback) push into the same continuation array - the
  forced turn is guaranteed either way; only the labelling differs. `continuationPrompt` does
  not exist (measured 0 occurrences).
- **PermissionRequest** decides via `hookSpecificOutput.decision.{behavior: "allow"|"deny"}`
  (the shipped zod schema; NOT the PreToolUse-style `permissionDecision` string, and there is no
  `"ask"` value - the pass-through is to omit `hookSpecificOutput` entirely).
- Optional `allowedHttpHookUrls` setting: if the operator has configured it, it must include
  `http://127.0.0.1:2722/*` or **every http hook** is blocked ("HTTP hook blocked: … does not
  match any pattern in allowedHttpHookUrls"). Unset (the default) allows all URLs. This
  matters more than it did: eight entries depend on it now, not two.

## The status-line command (v0.12.0)

`hooks/sidecrab_statusline.py` is installed as the `statusLine` command, not as a hook. It
POSTs the official status-line stdin document to `/v1/statusline` (fire-and-forget) and then
**chains** to any status-line command the operator already had - the installer saves it to
`~/.sidecrab/statusline-chain.json` and the uninstaller restores it. See the module
docstring and `setup/Install-SideCrab.ps1`.

## The merge marker

Events and what crabd does with them are in `docs/STATE-CONTRACT.md`. Install/uninstall
match SideCrab's own entries on the `127.0.0.1:2722/v1/hook` substring - which every route
above contains as a prefix, so the same marker finds both the `command` (`.command`) and
`http` (`.url`) entries and leaves every other hook alone. Re-running the installer
therefore **replaces** a pre-v0.36.0 curl entry with its http successor rather than
leaving both.

The bare `/v1/hook` is still served for exactly that reason: a fragment installed on
another machine before this release posts every event there, and a 404 would stop that
panel's state machine without a word.
