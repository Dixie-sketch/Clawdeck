# SideCrab hooks

Two fragments, each the `hooks` object merged into `~/.claude/settings.json`:

- **`settings-hooks-fragment.json`** — Windows, merged by `setup/Install-SideCrab.ps1`.
- **`settings-hooks-fragment-macos.json`** — macOS, merged by `setup/install.sh`. The
  PowerShell installer does not read it and must not be pointed at it.

They carry two kinds of entry and are identical apart from the curl invocation —
`hooks/tests/test_hooks_fragment.py` compares them with that one difference normalised
away, so a fix to one is a fix to both or a failing test.

## crabd's two gates

crabd 0.31.0 and later listens on **`127.0.0.1:9999`**, loopback only, and refuses two
things:

- **Origin.** An `Origin` header that is absent, `null`, a non-web scheme, or crabd's own
  origin is allowed; any other `http(s)` origin is 403. Hooks send no `Origin` at all.
- **`X-SideCrab-Panel`.** EVERY POST must carry it, with any non-empty value; SideCrab
  sends `1`. Without it crabd answers 403 `{"error":"panel header required"}` and the hook
  is silently lost. GETs never need it. A crabd older than 0.31.0 ignores the header, so
  sending it always is safe in both directions.

Both fragments therefore carry the header on every entry — on the curl line for a `command`
hook, in the `headers` map for an `http` one.

## The five fire-and-forget `command` hooks

`SessionStart`, `UserPromptSubmit`, `Notification`, `SubagentStop`, `SessionEnd` each pipe
the hook JSON that Claude Code puts on stdin straight to crabd:

```
curl.exe -s -m 2 -X POST -H "X-SideCrab-Panel: 1" --data-binary @- http://127.0.0.1:9999/v1/hook || exit 0
/usr/bin/curl -s -m 2 -X POST -H 'X-SideCrab-Panel: 1' --data-binary @- http://127.0.0.1:9999/v1/hook || exit 0
```

- `curl.exe` is the Windows-native one in `C:\Windows\System32` — not Git Bash's. On macOS
  the path is absolute: hooks run under a shell that inherits no login `PATH`, and a bare
  `curl` there is a coin toss.
- The header quoting follows the shell. `cmd.exe` has no single-quote literal, so Windows
  uses double quotes; macOS runs the command under `sh -c` and uses single quotes.
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
"Stop":              { "type": "http", "url": ".../v1/hook/stop",       "timeout": 5,
                       "headers": { "X-SideCrab-Panel": "1" } }
"PermissionRequest": { "type": "http", "url": ".../v1/hook/permission", "timeout": 60,
                       "headers": { "X-SideCrab-Panel": "1" } }
```

`headers` is a documented `http` handler field
(code.claude.com/docs/en/hooks#http-hook-fields); Claude Code sends each pair on the POST.

- **Stop → `/v1/hook/stop`.** crabd answers within ~2 s: `{}` to let Claude stop, or
  `{"hookSpecificOutput":{"hookEventName":"Stop","additionalContext":"<prompt>"}}` to feed a
  tap-to-continue prompt back in as NON-ERROR feedback (the conversation continues so the model
  can act on it — the binary's own schema text). `decision:"block"` also continues but paints
  "Stop hook error occurred" and labels the nudge an error to the model; it is retained in crabd
  as an executable fallback only. This
  replaces the former `Stop` curl entry — crabd records the done-transition on this endpoint
  now (docs/STATE-CONTRACT.md, v0.12.0 item 3). `timeout` 5 s bounds crabd's 2 s answer.
- **PermissionRequest → `/v1/hook/permission`.** crabd long-polls (up to 55 s) for an
  Approve/Deny tap from the widget, then returns
  `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"|"deny"}}}`.
  On no-tap / timeout / `panelApprovals` disabled it returns **no `hookSpecificOutput`** (`{}`), so
  the terminal dialog appears exactly as today — it NEVER auto-allows (docs/STATE-CONTRACT.md,
  v0.12.0 item 4). `timeout` 60 s sits just past crabd's 55 s poll.

**Fail-open by design.** An HTTP hook whose endpoint is refused, errors, or times out
returns `ok:false` inside Claude Code and does NOT block: a stopped crabd means Claude
stops normally and permission falls back to the terminal dialog. Nothing about crabd being
down can wedge a session.

## Verified against the shipped Claude Code (claude.exe v2.1.246, 2026-08-26)

Confirmed by inspecting the shipped binary (a Bun-compiled `claude.exe`) and
code.claude.com/docs/en/hooks:

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
  `http://127.0.0.1:9999/*` or these two hooks are blocked ("HTTP hook blocked: … does not
  match any pattern in allowedHttpHookUrls"). Unset — the default, measured against the
  binary — allows all URLs.

  **List both host forms.** Patterns are matched against the URL as written, and
  `127.0.0.1` and `localhost` are different strings, so `http://127.0.0.1:9999/*` does not
  cover a hook wired to `http://localhost:9999/…` or the other way round. Our own fragments
  use `127.0.0.1` throughout, but crabd serves the panel at `localhost:9999`, and an
  operator who edits a URL to match what they see in the browser gets a silently blocked
  hook. `setup/tests/SideCrab.Setup.Tests.ps1` pins exactly that asymmetry against
  `Test-SideCrabHookUrlAllowed`. Listing both costs nothing:

  ```json
  "allowedHttpHookUrls": ["http://127.0.0.1:9999/*", "http://localhost:9999/*"]
  ```

  Adding them in one settings file does not remove another file's entries: the allowlist
  is the **merged** one across every settings level
  (code.claude.com/docs/en/hooks, "Hook locations": Claude Code runs an HTTP hook handler
  only if its URL matches the merged allowlist; the general rule is
  code.claude.com/docs/en/settings, "Lists merge instead of overriding").

## The status-line command (v0.12.0)

`hooks/sidecrab_statusline.py` is installed as the `statusLine` command, not as a hook. It
POSTs the official status-line stdin document to `/v1/statusline` (fire-and-forget, with the
`X-SideCrab-Panel` header like every other POST) and then
**chains** to any status-line command the operator already had — the installer saves it to
`~/.sidecrab/statusline-chain.json` and the uninstaller restores it. See the module
docstring and `setup/Install-SideCrab.ps1`.

## Installing the macOS fragment

`setup/install.sh` merges it. The script is a thin `sh` wrapper: it finds a Python 3.13+
(`$SIDECRAB_PYTHON`, then `python3.14`/`python3.13`/`python3` across `PATH`,
`/opt/homebrew/bin` and `/usr/local/bin`, each probed for its version — Apple's
`/usr/bin/python3` is a command-line-tools stub and is refused), then hands over to
`setup/sidecrab_setup.py`.

```
./setup/install.sh [--with-toast] [--with-approvals|--no-approvals] [--force-enable] [--yes]
./setup/install.sh --status | --doctor | --pairing-code | --limits-token
./setup/update.sh
./setup/uninstall.sh [--purge] [--yes]
```

What the merge does to `~/.claude/settings.json`, in order:

1. **Backs the file up** to `<path>.sidecrab-bak-YYYYMMDD-HHMMSS` before the first write.
2. **Merges the fragment at ENTRY level** on the `127.0.0.1:9999/v1/hook` marker. Our
   entries are replaced by the fragment's current ones; a hook you hand-merged into one of
   our matcher groups stays, and a matcher group of your own is untouched. Running it twice
   produces byte-identical JSON and takes no second backup.
3. **Takes the `statusLine` slot**, saving whatever was there to
   `~/.sidecrab/statusline-chain.json` so `sidecrab_statusline.py` can chain to it and
   `uninstall.sh` can put it back. Your `padding` is carried forward. It never re-saves when
   the slot is already ours, which would chain to itself.
4. **Adds both `allowedHttpHookUrls` patterns only if you already set that key.** If the key
   is absent it is left absent — creating it would switch the allowlist on and block every
   other http hook you have. `uninstall.sh` removes ours again, and removes the whole key
   rather than leave it empty, because an empty list admits nothing.

A `settings.json` that does not parse aborts the whole install, naming the file: nothing is
written and no agent is loaded.

`./setup/install.sh --doctor` proves the wiring end to end, including that the header gate is
live: it POSTs to `/v1/hook` **without** `X-SideCrab-Panel` and expects the 403.

## The merge marker

Events and what crabd does with them are in `docs/STATE-CONTRACT.md`. Install/uninstall
match SideCrab's own entries on the `127.0.0.1:9999/v1/hook` substring — which the `http`
URLs (`…/v1/hook/stop`, `…/v1/hook/permission`) contain as a prefix, so the same marker
finds both the `command` (`.command`) and `http` (`.url`) entries and leaves every other
hook alone.
