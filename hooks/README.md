# SideCrab hooks

`settings-hooks-fragment.json` is the `hooks` object merged into `~/.claude/settings.json`
by `setup/Install-SideCrab.ps1`. It carries two kinds of entry.

## The five fire-and-forget `command` hooks

`SessionStart`, `UserPromptSubmit`, `Notification`, `SubagentStop`, `SessionEnd` each pipe
the hook JSON that Claude Code puts on stdin straight to crabd:

```
curl.exe -s -m 2 -X POST --data-binary @- http://127.0.0.1:2722/v1/hook || exit 0
```

- `curl.exe` is the Windows-native one in `C:\Windows\System32` — not Git Bash's.
- `-m 2` caps the whole call at 2 s; a refused connection fails in microseconds.
- `|| exit 0` swallows curl's exit code so a stopped crabd can never surface an error
  in Claude Code. `exit 0` behaves the same under `cmd.exe` and any POSIX shell.
- `--data-binary @-` streams stdin. curl buffers it and sends `Content-Length`, but
  crabd accepts chunked framing too.

**No `PreToolUse`/`PostToolUse`, deliberately (v0.19.0).** They were the obvious way to tell the
panel a session is alive again after the operator answers a permission dialog in the app — and they
were rejected: they would put an HTTP round trip in front of every tool call in every session, on a
machine whose loopback drops SYN-ACKs. crabd reads that same evidence out of the transcript it
already parses. See `docs/STATE-CONTRACT.md` v0.19.0 §2 and §4.

## The two `type: "http"` hooks (v0.12.0 — the control-surface wave)

`Stop` and `PermissionRequest` are `type: "http"` entries — Claude Code POSTs the hook's
stdin JSON to the `url` itself (Content-Type `application/json`) and READS THE RESPONSE as
the hook's decision. Unlike the curl hooks these are two-way: crabd answers.

```jsonc
"Stop":              { "type": "http", "url": ".../v1/hook/stop",       "timeout": 5  }
"PermissionRequest": { "type": "http", "url": ".../v1/hook/permission", "timeout": 60 }
```

- **Stop → `/v1/hook/stop`.** crabd answers within ~2 s: `{}` to let Claude stop, or
  `{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"<prompt>"}}` to feed a
  tap-to-continue prompt back in as NON-ERROR feedback (the conversation continues so the model
  can act on it — the binary's own schema text). `decision:"block"` also continues but paints
  "Stop hook error occurred" and labels the nudge an error to the model; it is retained in crabd
  as an executable fallback only. This
  replaces the former `Stop` curl entry — crabd records the done-transition on this endpoint
  now (docs\STATE-CONTRACT.md, v0.12.0 item 3). `timeout` 5 s bounds crabd's 2 s answer.
- **PermissionRequest → `/v1/hook/permission`.** crabd long-polls (up to 55 s) for an
  Approve/Deny tap from the widget, then returns
  `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"|"deny"}}}`.
  On no-tap / timeout / `panelApprovals` disabled it returns **no `hookSpecificOutput`** (`{}`), so
  the terminal dialog appears exactly as today — it NEVER auto-allows (docs\STATE-CONTRACT.md,
  v0.12.0 item 4). `timeout` 60 s sits just past crabd's 55 s poll.

**Fail-open by design.** An HTTP hook whose endpoint is refused, errors, or times out
returns `ok:false` inside Claude Code and does NOT block: a stopped crabd means Claude
stops normally and permission falls back to the terminal dialog. Nothing about crabd being
down can wedge a session.

## Verified against the shipped Claude Code (claude.exe v2.1.246, 2026-08-26)

Confirmed by inspecting the shipped binary (a Bun-compiled `claude.exe`) and
docs.claude.com/en/docs/claude-code/hooks:

- Hook handler `type` accepts `command`, **`http`**, `mcp_tool`, `prompt`, `agent`. The
  `http` handler config carries `url` (not `command`) and a `timeout` in **seconds**
  (default 600). It POSTs the stdin document as the request body.
- **HTTP hooks are skipped only for `SessionStart` and `Setup`** (the binary logs
  "HTTP hooks are not supported for" those two) — `Stop` and `PermissionRequest` both allow
  them, which is why the five ingest hooks above stay on curl and only these two are `http`.
- **Stop** continuation: BOTH `additionalContext` (non-error, shipped) and top-level
  `decision:"block"` (error-labelled, fallback) push into the same continuation array — the
  forced turn is guaranteed either way; only the labelling differs. `continuationPrompt` does
  not exist (measured 0 occurrences).
- **PermissionRequest** decides via `hookSpecificOutput.decision.{behavior: "allow"|"deny"}`
  (the shipped zod schema; NOT the PreToolUse-style `permissionDecision` string, and there is no
  `"ask"` value — the pass-through is to omit `hookSpecificOutput` entirely). An earlier
  docs-based note here read `permissionDecision:allow|deny|ask`; corrected against the binary.
- Optional `allowedHttpHookUrls` setting: if the operator has configured it, it must include
  `http://127.0.0.1:2722/*` or these two hooks are blocked ("HTTP hook blocked: … does not
  match any pattern in allowedHttpHookUrls"). Unset (the default) allows all URLs.

## The status-line command (v0.12.0)

`hooks/sidecrab_statusline.py` is installed as the `statusLine` command, not as a hook. It
POSTs the official status-line stdin document to `/v1/statusline` (fire-and-forget) and then
**chains** to any status-line command the operator already had — the installer saves it to
`~/.sidecrab/statusline-chain.json` and the uninstaller restores it. See the module
docstring and `setup/Install-SideCrab.ps1`.

## The merge marker

Events and what crabd does with them are in `docs/STATE-CONTRACT.md`. Install/uninstall
match SideCrab's own entries on the `127.0.0.1:2722/v1/hook` substring — which the `http`
URLs (`…/v1/hook/stop`, `…/v1/hook/permission`) contain as a prefix, so the same marker
finds both the `command` (`.command`) and `http` (`.url`) entries and leaves every other
hook alone.
