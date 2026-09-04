# Getting started on macOS — installer notes

Notes for the macOS install path, to be folded into `GETTING-STARTED.md` when the port lands.
Measured facts only; anything unmeasured says so and carries the command to measure it.

## What `setup/install.sh` does

```
./setup/install.sh [--with-toast] [--with-approvals|--no-approvals] [--force-enable] [--yes]
./setup/install.sh --status | --doctor | --pairing-code | --limits-token
./setup/update.sh
./setup/uninstall.sh [--purge] [--yes]
```

1. **Finds a Python 3.13 or newer.** `$SIDECRAB_PYTHON` first, then `python3.14`,
   `python3.13`, `python3` across `PATH`, `/opt/homebrew/bin` and `/usr/local/bin`. Each
   candidate is *probed* for its version rather than trusted: `/usr/bin/python3` on this Mac
   is Apple's 3.9.6, and the Xcode command-line-tools stub exits non-zero with a "No
   developer tools were found" note. The refusal names the fix
   (`brew install python@3.13`). The absolute path it settles on is written into the plists,
   because a LaunchAgent does not inherit your login `PATH`.
2. **Merges the hook fragment into `~/.claude/settings.json`**, after backing it up to
   `<path>.sidecrab-bak-YYYYMMDD-HHMMSS`. Entry level, on the `127.0.0.1:9999/v1/hook`
   marker; your own hooks are kept. See `hooks/README.md`.
3. **Takes the `statusLine` slot**, saving your prior command to
   `~/.sidecrab/statusline-chain.json` so it still runs and `uninstall.sh` can restore it.
4. **Writes `panelApprovals.enabled` into `~/.sidecrab/config.json`** (backed up first,
   replaced atomically, every other key preserved). The default writes `false` only when the
   key is absent, so a re-run never reverts a `--with-approvals` you chose earlier.
5. **Writes and loads the LaunchAgents.** `com.sidecrab.crabd` always,
   `com.sidecrab.toast` with `--with-toast`. Plists at
   `~/Library/LaunchAgents/<label>.plist`; logs at `~/.sidecrab/logs/<label>.log`
   (directory mode 0700 — the log carries session titles and repo paths). Loading is
   `launchctl bootout` then `launchctl bootstrap gui/<uid>`. A label you disabled with
   `launchctl disable` has its plist refreshed but is **not** started; `--force-enable` is
   the only override.

`./setup/install.sh --doctor` afterwards walks the whole chain a session travels and exits
non-zero if any row fails.

**`--doctor` is not quite read-only.** It posts a real SessionStart / Notification /
SessionEnd cycle for the session id `smoke-test` to prove the write path end to end. The
cycle cleans up after itself — SessionEnd is sent from a `finally`, so even a crash mid-run
clears the row — but crabd persists every hook event, so the run leaves three rows in
`~/.sidecrab/history.jsonl` and they will appear in that day's history. `--status` writes
nothing at all.

## `allowedHttpHookUrls` — list both host forms

If you have set `allowedHttpHookUrls` anywhere in your Claude Code settings, it must admit
crabd or the `Stop` and `PermissionRequest` http hooks are never called at all — silently.
The installer adds both patterns when the key exists, and **never creates it** when it does
not: creating it would switch the allowlist on and block every other http hook you have.

```json
"allowedHttpHookUrls": ["http://127.0.0.1:9999/*", "http://localhost:9999/*"]
```

Both forms, because patterns are matched against the URL as written and `127.0.0.1` and
`localhost` are different strings. Arrays **merge** across settings files, so a pattern in
your user settings and one in a project's are both in force — adding ours removes none of
yours.

## The Keychain prompt

Claude Code keeps its credential in the login Keychain (service `Claude Code-credentials`,
account = your login name), not in `~/.claude/.credentials.json`. A process that is not on
that item's access list gets a macOS dialog the first time it reads it — for a LaunchAgent
that process is `python3`, so **expect one prompt in your session shortly after the first
install**. Choose **Always Allow** and it does not come back. A refused read is treated as
"no credential", never as a guess: the gauges go dark with a note.

The long-lived token you store with `./setup/install.sh --limits-token` is a different item
and does not prompt: items created through the `security` tool carry that tool in their own
access list. The token is read from stdin, never from a command-line argument, so it never
appears in `ps`.

**`--limits-token` needs a crabd that carries the Keychain store** — `PLATFORM.store_limits_token`,
which arrives with the next crabd phase of the port. Until then the command refuses, naming
that attribute, and stores nothing; the `limits token` row in `--status` and `--doctor` says
**"not supported by this crabd"** rather than "none stored", because "none stored" would send
you to run a command that cannot yet work.

## App Nap and timer coalescing — UNMEASURED

Whether macOS throttles a `KeepAlive` LaunchAgent's timers (App Nap, timer coalescing) has
**not** been measured on this hardware, so the plists carry **no `ProcessType` key**: the
default scheduling stands until there is a number. Do not add one on the strength of a
forum post.

To measure it, leave crabd running and sample the feed's own timestamp for two minutes:

```sh
for i in $(seq 1 120); do
  curl -s localhost:9999/v1/state | python3 -c 'import json,sys; print(json.load(sys.stdin)["generatedAt"])'
  sleep 1
done > /tmp/generated.txt

python3 - <<'PY'
from datetime import datetime
stamps = [datetime.fromisoformat(line.strip().replace("Z", "+00:00"))
          for line in open("/tmp/generated.txt") if line.strip()]
gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
print("max gap %.1fs over %d samples" % (max(gaps), len(stamps)))
PY
```

crabd rebuilds its snapshot every 2 s, so a healthy run's max gap is a little over 2 s. Run
it once with the terminal focused and once with the machine idle and the lid shut on
battery — the second is where a throttle would show. Record the number in
`docs/PORT-NOTES.md` before changing anything about the plist.

## TCC (the "would like to access your Documents folder" dialogs)

crabd **reads** `~/.claude` (transcripts and settings) and **writes only** `~/.sidecrab`
(its own config, history, logs and pairing code). The installer is the thing that writes
`~/.claude/settings.json`, and it does so once, with a backup, when you run it — not from
the agent. None of those is a TCC-protected location, so no folder-access dialog is
expected. If a future
change makes crabd read `~/Documents`, `~/Desktop`, `~/Downloads` or an iCloud folder, macOS
will prompt — under a LaunchAgent that prompt can appear detached from any window, and a
denial is permanent until it is reset in System Settings. Treat "the daemon needs a new
directory" as a decision with a user-visible cost, not a detail.
