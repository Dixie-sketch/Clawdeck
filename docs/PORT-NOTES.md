# Port notes: SideCrab from Windows + iCUE to macOS + any browser

The durable record of the port: every Windows- or iCUE-specific seam found while reading,
what each became, every measurement taken, and every decision the port brief asked for. Kept
current as the port proceeds. Stage status lives in `docs/IMPLEMENTATION_PLAN.md` until the
port is complete.

## How the brief and CLAUDE.md were reconciled

- Commit messages carry no model attribution trailer: the repository's own history and the
  operator's global instructions both forbid it, and they win over the session default.
- `docs/IMPLEMENTATION_PLAN.md` holds stage status (removed when done); this file holds the
  seams and decisions and persists.
- The PowerShell installer and its Pester suite stay in the tree untouched. The macOS installer
  and its Python suite are added beside them; nothing Windows is deleted.
- Work is on the `macos-port` branch, one commit per behaviour, the full suite before each.

## Baseline: the existing suites on macOS (2026-09-04, Python 3.14.6, node 24.1.0)

| Suite | Result | Triage |
|---|---|---|
| companion (`companion/tests`) | 1131 ran, OK, 3 skipped | the 3 skips are (a) genuinely Windows-only and already marked: the DPAPI round trip (`test_crabd.py:1425`) and the two live `GetSystemTimes` reads (`test_crabd.py:9744`) |
| notifier (`notifier/tests`) | 500 ran, 1 failure | `test_decider.AdapterTests.test_icon_uri_keeps_the_drive_colon_unescaped` asserts a drive letter in the toast image URI. Bucket (a): the assertion is about Windows paths, so it is skipped off Windows with a reason, never deleted |
| hooks (`hooks/tests`) | 14 ran, OK | portable as-is |
| lighting (`lighting/tests`) | 104 ran, OK | portable as-is; the component stays parked and has no macOS counterpart |
| widget ordering (`node widget/tests/test_ordering.js`) | 140/140 | portable as-is |
| setup (Pester) | not runnable, no `pwsh` | bucket (a): asserts `C:\` paths and calls DPAPI. Stays as the Windows suite; a Python suite covers the shell installer |

Bucket (b), accidentally Windows-coupled but portable, and bucket (c), real bugs, were empty
at baseline. Anything found later is added to the seam table below with its bucket.

## Measurements

- **Where Claude Code keeps its credential on this Mac (Phase 4, problem two).**
  `~/.claude/.credentials.json` does not exist. The login Keychain holds a generic-password
  item with service `Claude Code-credentials` and account equal to the login user name,
  modified the same day Claude Code last ran (Claude Code 2.1.260). The payload was not
  read in this session (reading the secret was refused by the session's permission policy),
  so its JSON shape is unmeasured. The reader therefore parses the item as the same
  `{"claudeAiOauth": {...}}` document the file carries and, if it does not parse that way,
  serves `available: false` with a note that names the Keychain item. It never guesses a
  token.
- **Keychain access prompts.** A process that is not on the item's access-control list gets
  a macOS dialog the first time it reads the item. For a LaunchAgent that is `python3`, so
  the first read after install prompts once in the operator's session; "Always Allow" ends
  it. Documented in GETTING-STARTED; the reader treats a refused read as absent.
- **Python on this Mac.** `/usr/bin/python3` is Apple's 3.9.6, below the project floor;
  Homebrew provides 3.13 and 3.14 under `/opt/homebrew/bin`. The installer must find a real
  3.13+ and reject the Apple stub by version, not by path.
- **Port 9999** was free on this machine at the time of measurement.
- **Claude Code hook mechanics (documentation check, CLI 2.1.260).** `type: "http"` hooks accept a
  `headers` map (`{"type":"http","url":...,"timeout":60,"headers":{"X-SideCrab-Panel":"1"}}`), so
  the panel header the port requires on every POST can be carried by the Stop and
  PermissionRequest hooks. SessionStart and Setup run command hooks only. Command hooks run under
  `sh -c` on macOS with the event JSON on stdin, inheriting the parent environment, so the macOS
  fragment uses `/usr/bin/curl` by absolute path. `allowedHttpHookUrls` is unset by default (every
  URL allowed); once any settings file defines it, only matching URLs run and the rest are blocked
  silently, and arrays merge across files, so the installer documents listing both
  `http://127.0.0.1:9999/*` and `http://localhost:9999/*`. `statusLine` is a single command object
  with no chaining field, so the existing wrapper-and-chain-file approach stays.
- **Credential shape.** The documentation says macOS stores the login in the Keychain and falls back
  to `~/.claude/.credentials.json` (mode 0600) only when the Keychain write fails, and that a custom
  `CLAUDE_CONFIG_DIR` keys a different Keychain entry. The item's JSON keys, read out of the 2.1.260
  binary rather than from the docs, are `claudeAiOauth.{accessToken, refreshToken, expiresAt,
  refreshTokenExpiresAt, scopes, subscriptionType, rateLimitTier, clientId}`: the same shape the
  file carries. `claude setup-token` prints a bare `sk-ant-oat...` string and saves nothing. The
  reader treats a missing or renamed key as unknown, never as a hard failure.
- **CPU ticks on macOS (Phase 2).** `host_statistics(HOST_CPU_LOAD_INFO)` through libSystem returns
  four cumulative 32-bit counters in the order user, system, idle, nice, in 1/100 s units
  (`SC_CLK_TCK` = 100), summed across cores: one second on this 16-core machine moved them by
  213, 103, 1280, 0. Two consequences the Darwin reader carries: the counters wrap 2^32 in
  about 31 days of uptime at that rate, so the reader unwraps them into 64-bit values before the
  sampler sees a delta; and the sampler keeps its Windows convention (kernel includes idle, 100 ns
  units), so the reader hands it `(idle, system + idle, user + nice) * 100000` and its arithmetic
  and `CPU_MIN_TOTAL_TICKS` are untouched. `nice` counts as busy time.
- **Memory on macOS (Phase 2).** `host_statistics64(HOST_VM_INFO64)` returns 38 words (page counts);
  `vm.pagesize` is 16384 and `hw.memsize` 128.0 GiB here. Activity Monitor's headline "Memory Used"
  is app memory (internal minus purgeable) plus wired plus compressed, 66.0 GiB at the time of
  measurement, while `top` reports total minus free, 98.3 GiB. The contract promises the Activity
  Monitor figure, so that formula is the one served.
  A live read through the finished reader on this Mac (2026-09-04) served memTotalGB 128.0,
  memUsedGB 68.2, memPct 53.3 and, on the second sample, cpuPct 15.1, agreeing with `vm_stat`
  and `sysctl` at the time; the fleet reader read `running` for a live agent, `stopped` for a
  loaded idle one and `absent` for an unregistered label, and asked nothing about glow.
- **Suite isolation, re-proven after Phase 4 (2026-09-04).** A canary run of the companion,
  setup and notifier suites left `~/.sidecrab`, `~/.claude/settings.json` and
  `~/Library/LaunchAgents` byte-identical (mtimes and sizes compared before and after), and
  the opt-in Keychain round trip left no item behind. One leak did happen earlier in the
  day: while the Phase 1 transport tests were being written, a version of the module that
  ran `main()` before its `setUpModule` repointed `PANEL_TOKEN_FILE` and the config path
  wrote crabd's default `config.json` (`quietHours: null, allowReply: false`) into the real
  `~/.sidecrab` at 16:01. The isolation was completed in the same commit and every module
  now repoints all six path globals plus the Keychain kill switch; the file was left in
  place for the installer to back up.
- **launchctl output (Phase 3).** `launchctl print gui/<uid>/<label>` exits 0 for a loaded agent
  with tab-indented first-level lines (`state = running` plus `pid = N`, or `state = not running`);
  deeper sub-objects carry their own `state = active` lines, so only the first-level line counts.
  An absent label exits 113 with `Could not find service "<label>" in domain for user gui: <uid>`.
  Other first-level state words in use: `waiting`, `spawn scheduled`.
- **The security tool (Phase 4).** `security -i` reads commands from stdin, so a secret can be
  stored without ever appearing in a process argument list; `find-generic-password -w` prints it
  to stdout; an absent item exits 44 with "The specified item could not be found in the keychain".
  Items created through the tool carry the tool in their access list, so crabd's later reads through
  the same tool do not prompt; the CLI's own credential item was created by Claude Code and does
  prompt once (see above).
- **macOS notifications through osascript (Phase 7).** One `--test-toast` run on this Mac
  (2026-09-04) exited 0 in about 120 ms with empty stderr, and the operator confirmed a
  notification appeared on screen; no permission prompt was reported. The notification carries
  Script Editor's identity, no buttons and no replacement identifier; those are recorded as
  residuals of this route, not defects.
- **Existing hooks in the operator's `~/.claude/settings.json`**: one unrelated
  `UserPromptSubmit` command hook, no `statusLine`, no `allowedHttpHookUrls`. The installer
  must preserve that hook.
- **App Nap and timer coalescing under the LaunchAgent (Phase 8) - MEASURED 2026-09-04.**
  The open question `docs/GETTING-STARTED-MACOS-NOTES.md` carried, answered with the sampling
  command that file prescribed: two minutes of `GET /v1/state` against the live
  `com.sidecrab.crabd` agent produced **55 distinct `generatedAt` snapshots, max gap 3.0 s,
  mean 2.19 s, none over 4 s**. crabd rebuilds every 2 s, so that is the healthy figure and
  not a throttled one. **Decision: the plists carry no `ProcessType` key.** The default
  scheduling stands; nothing is added on the strength of a forum post. If a later reading on
  battery with the lid shut disagrees, it lands here first, then in the plist.

## Seams (filled from the read-through; line numbers as of the baseline commit 5366719)

Port phase numbers are the brief's: 0 platform seam, 1 port/origin/static serving, 2 host
metrics via `host_statistics`, 3 fleet via `launchctl`, 4 limits token via Keychain,
5 `install.sh` + LaunchAgent, 6 browser panel with a localStorage settings adapter,
7 macOS notifications, 8 docs. "What it becomes" is a proposal, not a decision: decisions are
recorded per phase in the section below this one.

### crabd (`companion/crabd.py`)

| Line | Symbol | What it assumes | Port phase | What it becomes |
|---|---|---|---|---|
| 6 | module docstring, opening line | Serves `/v1/state` "on 127.0.0.1:2722 for the SideCrab widget" - port and consumer both fixed | 1 | Restated for the new port and for a panel crabd serves itself, in the same commit that moves `PORT`. |
| 17 | module docstring, source 6 | `schtasks /query` on the SideCrab tasks feeds `fleet` | 3 | Names the launchd probe; the served running/stopped/absent/unknown vocabulary does not move. |
| 28 | module docstring, source 10 | `GetSystemTimes` / `GlobalMemoryStatusEx` feed `host`, "beside the iCUE temperature sensors" | 2 | Names the Darwin counters. With no iCUE sensor row, `host` becomes the panel's only ambient gauge. |
| 41 | module docstring, `/v1/panel-log` rationale | The ring exists "because iCUE renders the widget on a surface no devtools can reach" | 1 | Either keeps the endpoint with an honest rationale (a phone browser still has no console) or retires it contract-first. |
| 45 | module docstring, closing line | "stdlib only, Python 3.13, Windows host" | 8 | Names the supported host or hosts. The read-only `~/.claude` rule survives verbatim. |
| 50 | `import csv` | Imported only to parse `schtasks /query /fo csv` output | 3 | Drops with the schtasks runner if the launchd probe returns a state token. |
| 51 | `import ctypes` | Kept for the DPAPI decrypt and the two Win32 host counters | 2 | Stays: `ctypes` is cross-platform and only the two `windll` call sites move. |
| 58 | `import subprocess` | Used at 4137 / 4151 / 4238 with `creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)` | 0 | Unchanged. The `getattr(..., 0)` spelling is already portable and is the idiom the rest of the port should copy. |
| 68 | `from pathlib import Path, PureWindowsPath` | `PureWindowsPath` is imported for exactly one caller, `_cwd_title` | 0 | Unchanged. See the traps list. |
| 70 | `SCHEMA_BREAKING` rationale (70-77) | A bump dead-feeds every installed widget until someone re-imports the package at the iCUE console | 1 | Records that panel and companion now ship together, so a bump costs a reload. The no-bump-during-the-port rule stands either way. |
| 78 | `VERSION = "0.30.0"` | The string the panel keys its capability-latch clear on | 0 | Bumped per the repo's convention for anything a user notices; the latch clear doubles as the panel's cache-bust lever. |
| 80 | `HOST = "127.0.0.1"` | Loopback-only bind is half the access-control story | 1 | Unchanged. Serving a page makes `0.0.0.0` tempting; every gate in the file assumes loopback reachability. |
| 83 | `PORT = int(os.environ.get("CRABD_PORT") or 2722)` | 2722 is production "and the Scheduled Task owns it"; the same literal is in hooks, setup, notifier and widget | 1 | Defaults to 9999, with the `CRABD_PORT` override kept verbatim because the suites depend on it. A cross-component sweep, not a one-line edit. |
| 87 | pairing-code rationale comment (87-93) | The secret is unreachable because "iCUE widget PROPERTIES are not reachable from a browser" | 6 | Re-derived for a panel that is itself a web origin: the store has to be one a different origin cannot read. |
| 94 | `PANEL_TOKEN_FILE` | `~/.sidecrab/panel-token`, protected by the Windows profile ACL | 5 | Same path, with the 0600 mode at 4602 becoming the actual protection rather than a no-op. |
| 115 | `CLAUDE_HOME` | `~/.claude`, with the `CRABD_CLAUDE_HOME` test override | 0 | Unchanged; it is the cleanest fixture seam in the file. |
| 117 | `CREDENTIALS_FILE` | The CLI's OAuth token lives in `~/.claude/.credentials.json` | 4 | A credential source that can also read the login Keychain item, with the same read-use-drop discipline. |
| 132 | `LIMITS_TOKEN_FILE` rationale (132-139) | "Install-SideCrab.ps1 -LimitsToken stores it here DPAPI-protected (CurrentUser)" | 4 | Names the macOS store. The three properties in the last sentence - never logged, never served, never written anywhere else - are the invariant; the mechanism is not. |
| 139 | `LIMITS_TOKEN_FILE = SIDECRAB_DIR / "limits-token.dpapi"` | The filename itself encodes the Windows crypto mechanism | 4 | A platform-neutral name, or a branch that decrypts only when the `.dpapi` suffix is present. |
| 201 | `FLEET_TASKS` | Two Windows Scheduled Task names, `SideCrab-glow` and `SideCrab-toast` | 3 | Launchd labels behind the same served keys; the keys are contract and do not change. |
| 207 | `FLEET_STATUS_COL = 2` | A measured `schtasks /query /fo csv /nh` column layout | 3 | Dies with the CSV parse, replaced by an equally measured and dated note about `launchctl` output. |
| 208 | `FLEET_STATUS_MAP` | schtasks' running / ready / queued / disabled vocabulary | 3 | A launchd map: loaded-but-not-running is `stopped`, an unknown label is absent, anything unreadable is `unknown`. |
| 214 | `FLEET_ABSENT_MARKERS` | Two measured schtasks not-found phrasings | 3 | The launchctl not-found wording, measured rather than guessed. The absent-is-not-unknown distinction is kept. |
| 221 | `HOST_BYTES_PER_GB = 1024 ** 3` | GiB, "the unit Task Manager shows, so the two agree" | 2 | Either stays GiB with a rewritten justification or moves to 10^9 to match Activity Monitor. It is a served number, so contract-first either way. |
| 235 | `CPU_MIN_TOTAL_TICKS = 1_000_000` | 100 ns FILETIME ticks and a measured Windows scheduler quantum | 2 | Re-derived for the Darwin counter unit. The null-not-zero rule it protects carries across unchanged. |
| 742 | `TS_MIN_EPOCH` / `TS_MAX_EPOCH` (743) | Bounds written against this Windows host's `fromtimestamp` behaviour | 0 | Unchanged, and both stay explicit bounds rather than a caught platform exception. |
| 766 | `CROSS_SITE_REFUSED` | The one 403 body the Origin gate answers with, reads and writes alike | 1 | Unchanged as a body. What changes is which origins reach it. |
| 778 | panel-log constants (778-793) | The header reasons from "the Xeneon Edge, where no devtools can be attached" | 1 | The same bounds with a rewritten rationale if the endpoint is kept; the SEC-d control-byte strip stays regardless. |
| 820 | `_UA_BROWSER_MARKERS` | Includes `qtwebengine` so the iCUE webview can be recognised diagnostically | 1 | Loses `qtwebengine` only once nothing else references it; the list stays diagnostic-only. |
| 823 | `_classify_ua_source` | Docstring example is "the QtWebEngine widget lands here" | 1 | A browser example. The MUST-NEVER-feed-the-gate warning matters more after the port, not less. |
| 1077 | `_cwd_title` | Parses with `PureWindowsPath` so one implementation reads Windows, POSIX and UNC cwds alike | 0 | Unchanged; POSIX root is added to the docstring's list of degenerate cases. |
| 1295 | `_local_clock` | Comment blames Windows for the zero-padded `%I` and the `lstrip('0')` | 8 | Same code, comment corrected: it is a portability workaround, not a Windows one, and `%-I` is the non-portable spelling it warns against. |
| 1376 | `UserConfig.allow_continue` | The safety argument leans on "a visited http/https page is refused 403 before it reaches the queue" | 1 | Restated against whatever the gate becomes. The server-side prompt whitelist is the half that does not move. |
| 1486 | `UserConfig.PRESERVED_SUBKEYS` | Lives in `UserConfig` because only it holds the lock over the read-modify-write | 0 | Unchanged; a handler that read the old value first would race a hand edit. |
| 1520 | `UserConfig._write` | Temp sibling plus `os.replace`, explicitly "atomic on Windows and POSIX" | 0 | Unchanged. Named here so a porter scanning for `os.replace` knows it is deliberate. |
| 1596 | `HistoryLog` | The file holds `kind` + `sessionId` + `title` + `ts` and nothing else | 0 | Unchanged. A "helpful" detail field would break a stated privacy property. |
| 1807 | `GIT_READ_BUDGET_SEC` and the A-04 / A-06 header | The 21 s stall was measured on a UNC / SMB path | 0 | Unchanged. The comment already says the bound is on the operation, not on path syntax; a stale macOS mount stalls `stat` the same way. |
| 1913 | `GitLookup._read` (`start = Path(cwd)`) | OS-native `Path`, deliberately asymmetric with `_cwd_title` | 0 | Unchanged for a macOS target. The asymmetry earns a comment rather than a rewrite; `PureWindowsPath` cannot be stat()ed. |
| 2423 | `HookTracker.ACK_EVENT` | `"acknowledged from Edge"` names the Corsair hardware and is a persisted history kind | 8 | Renamed only as a deliberate contract change, with replay accepting both spellings; old `history.jsonl` lines keep the old text forever. |
| 2935 | `_DATA_BLOB` | Win32 `DATA_BLOB` ABI struct feeding `CryptUnprotectData` | 4 | Moves behind the Windows branch of the token store, or goes with it. A companion test constructs it directly. |
| 2939 | `_dpapi_unprotect` | `ctypes.windll.crypt32`, guarded by `hasattr(ctypes, "windll")`, so it always returns `None` off Windows | 4 | A platform-dispatched secret reader keeping the exact failure contract (`None` on every failure, never a partial secret) and the module-level bytes-to-optional-bytes shape the tests monkeypatch. |
| 2962 | `read_limits_token` | Reads the `.dpapi` blob fresh on every call | 4 | Same name, signature, `path=` override and read-fresh-every-call behaviour, over the macOS store. |
| 3095 | `LimitsReader._fetch` credential read | The CLI OAuth token is in `~/.claude/.credentials.json` | 4 | Reads whichever source exists on this host; `CRABD_CLAUDE_HOME` stays the fixture seam. |
| 3120 | served note, no usable token | Tells the operator to run `Install-SideCrab.ps1 -LimitsToken` | 4 | Names the macOS command, contract-first: `docs/STATE-CONTRACT.md` quotes these notes and a companion test pins `-LimitsToken`. |
| 3124 | served note, expired token | Same script name, and this is the branch a macOS operator sees first | 4 | Same as above. The store moves before the string, or the panel advertises a command that does not exist. |
| 3145 | served note, stored token rejected | Third `-LimitsToken` reference | 4 | Renamed with its test. The two-failure-modes-never-confused property is the feature, not the wording. |
| 3332 | `ModelCatalog.credentials_file` | Same `~/.claude/.credentials.json` assumption | 4 | The same credential source as `LimitsReader`. Without it every context gauge goes dark at once. |
| 3718 | `OtlpReceiver` docstring | Documents the ingest endpoint as `127.0.0.1:2722` | 1 | Derives the documented endpoint from the bound port instead of restating it. |
| 4128 | `RecapReader._git_count` | `creationflags=getattr(...)` with a Scheduled-Task console comment | 0 | Code unchanged; the comment names the Windows-only reason, or the flag is hoisted to one module constant. |
| 4144 | `RecapReader._git_days` | Same flag; `git log --date=format-local` behaves identically on macOS | 0 | Unchanged. |
| 4160 | `FleetReader` (class + docstring) | The whole concept is Windows Task Scheduler | 3 | A pluggable supervisor probe. The four-outcome vocabulary is contract and stays shared. |
| 4207 | `FleetReader.status` | Classifies schtasks exit codes and English error phrases | 3 | The classifier stays shared; the string matching moves into the per-platform probe. |
| 4221 | `FleetReader._status_field` | Parses the schtasks CSV status column, with `csv.reader` rather than a split | 3 | Belongs inside the Windows probe. A launchd probe returns a state token and needs no CSV. |
| 4232 | `FleetReader._run` | Literal `schtasks` argv; on macOS it raises `FileNotFoundError` and every task reads `unknown` | 3 | A platform-selected default runner chosen at the wiring site. The `runner=` kwarg and its `(code, out, err)` shape do not change. |
| 4246 | `_FILETIME` | Win32 FILETIME halves | 2 | Stays behind the Windows reader; a Darwin reader needs no struct. |
| 4252 | `_MEMORYSTATUSEX` | The `GlobalMemoryStatusEx` out-parameter, with the `dwLength` trap in its docstring | 2 | Stays behind the Windows reader; a Darwin reader returns the same `(total, avail)` tuple so `_mem()` is untouched. |
| 4271 | `_filetime` | Combines the two 32-bit halves | 2 | Moves with `_read_times` into the Windows-only section. |
| 4275 | `HostSampler` | Framed as "for the panel beside the iCUE sensors"; its top failure tier is "a platform with no `ctypes.windll`" | 2 | Darwin `times` / `memory` readers behind the existing callable injection. Every line of arithmetic and all three failure tiers stay untouched. |
| 4322 | `HostSampler.sample` | Returns `None` when neither counter reads, which is every macOS host today | 2 | Unchanged. Once the Darwin readers exist it starts returning a block and the panel's presence check does the rest. |
| 4426 | `HostSampler._read_times` | `ctypes.windll.kernel32.GetSystemTimes`; the `AttributeError` is documented as the platform gate | 2 | A Darwin reader returning the same `(idle, kernel, user)` triple, selected in `__init__` so the AttributeError path stays a genuine error path. |
| 4449 | `HostSampler._read_memory` | `GlobalMemoryStatusEx`, with `dwLength` set before the call | 2 | A Darwin reader returning `(total_bytes, avail_bytes)`. The GiB-versus-GB unit question is settled in the contract, not silently. |
| 4602 | `PanelToken.load_or_create` | `os.chmod(tmp, 0o600)` with the comment "a no-op on Windows; the profile ACL does the job" | 5 | The same call with the comment inverted, and the silent `except OSError: pass` reconsidered on a platform where the chmod is the protection. |
| 4652 | `PermissionBroker` | Never auto-allows; the only producer of `allow` is a tap on `/v1/action` | 0 | Unchanged. A browser-served panel raises the stakes on this, not lowers them. |
| 5016 | `PanelLog` | Exists because iCUE renders on "a surface no devtools can attach to" | 1 | Keeps the ring and its bounds with an honest rationale, or is retired contract-first. It must not become persistent either way. |
| 5092 | `OriginRecorder` | Built to measure "what Origin the real QtWebEngine widget sends" before an allowlist could be written | 1 | Stays as the instrument that proves the new gate. The measurement it was blocked on is answered a priori once crabd serves the panel. |
| 5129 | `OriginRecorder.record` | Runs before the gate and folds an absent header to `<absent>` | 1 | Unchanged, optionally with the gate's verdict as a third key component so refused origins show on `/v1/health`. |
| 5164 | `StateBuilder.__init__` | Twelve optional reader kwargs, `None` meaning "feature not wired" | 0 | Unchanged. Platform selection happens at the wiring site; the builder stays ignorant of the platform, which is what keeps the suite valid. |
| 5218 | `StateBuilder._host` | Not optional, because a host with no counters was assumed to be the exception | 2 | Unchanged once the Darwin readers land. The `host=` kwarg stays a test-only pin. |
| 5230 | `StateBuilder.origins` | Comment describes the SEC-a measurement as still pending | 1 | Unchanged, with the comment no longer describing a blocked measurement. |
| 5641 | `StateBuilder._sessions` -> `_cwd_title(cwd)` | Call site of the `PureWindowsPath` title derivation | 0 | Unchanged. See the traps list. |
| 5655 | `StateBuilder._sessions` -> `"model": info["model"]` | The model string is placed into the served row untouched | 0 | Unchanged. The `[1m]` / `[200k]` marker is rank two of the context-window precedence. |
| 5878 | `class Handler` | HTTP/1.1, Nagle disabled, `log_message` silenced | 1 | Unchanged, except that a static 404 now leaves no trace anywhere - worth routing through `_log_once` when the static route lands. |
| 5912 | `Handler._send` | Every response gets `Cache-Control: no-store` (5926) | 1 | Gains a cache variant for static panel assets. The no-store on `/v1/*` stays deliberate. |
| 5931 | `Handler.do_OPTIONS` / `_preflight_acao` (5948) | A web origin gets no ACAO, so its preflight dies | 1 | Reflects allowlisted local origins so a separately served dev panel can preflight. `*` stays illegal. |
| 5957 | `Handler._record_origin` | Feeds the recorder before the gate; the UA is diagnostic only | 1 | Unchanged. |
| 5973 | `Handler.do_GET` (SEC-4 read gate at 5983) | Any present http(s) Origin is 403 on reads | 1 | Routes through the allowlist predicate. Genuinely foreign origins keep the 403, and the reflect-not-wildcard rule is untouched. |
| 5991 | `do_GET` route table | Four `/v1/*` routes and a 404; crabd serves no static bytes because iCUE packaged the UI | 1 | Gains a static route strictly below the `/v1/*` branches, confined to a whitelisted panel root, keeping the 404 fallback shape. |
| 6015 | `Handler._do_state` | `/v1/state` never 500s; 503 only before the first snapshot | 0 | Unchanged. See the traps list. |
| 6048 | `Handler._health` | Diagnostics, explicitly outside the contract | 1 | The natural place to expose the allowlisted origin and the served panel URL; additions here need no schema bump. |
| 6153 | `Handler._read_body` | The chunked branch exists because `curl.exe --data-binary @-` sends no Content-Length | 0 | Unchanged. The macOS hooks use the same curl shape, so the branch stays. |
| 6221 | `Handler.MUTATING_PATHS` preamble (set at 6233) | Names "the widget's opaque iCUE origin, curl-fed ingest hooks" | 1 | The same inventory with the panel named correctly. A static route is a read path and must not join the set. |
| 6241 | `Handler._is_web_origin` | Any `http://` or `https://` Origin is hostile, and the panel's own Origin serialises to `null` | 1 | An allowlist decision: exactly the origins crabd serves the panel on, computed from the bound host and port. Every other http(s) origin still refused; absent and `null` still accepted. |
| 6288 | `Handler.do_POST` (SEC-1 write gate at 6291) | The same blanket refusal on every POST, including unknown paths | 1 | The same allowlist swap, with the drain-before-refuse ordering preserved exactly. |
| 6376 | `Handler._do_panel_log` | Validates before answering, because "the caller is the widget being DEBUGGED" | 1 | Unchanged if the endpoint survives. The validate-before-answer choice is what makes it trustworthy. |
| 6552 | `Handler._await_permission` | Parks a request thread 55 s, measured against the hook's 60 s timeout | 1 | Unchanged, and never proxied: anything in front of crabd would cut the hold at its own idle timeout and turn every approval into a silent pass-through. |
| 6668 | `_do_action` reply branch (501 at 6676) | The 2026-08-26 spike reasoned about named pipes and Windows window targeting | 8 | Keeps the 501 and re-runs the finding against macOS mechanisms before any claim changes. No stub ships. |
| 6776 | `Handler._do_decide` | The pairing gate; the code reaches the panel only through an iCUE widget property | 6 | The gate and its check order unchanged. Only the delivery of the code moves. |
| 6852 | `CONFIG_WRITABLE` (preamble at 6841) | The comment says `panelApprovals` "is set only via the config FILE, by the installer's `-WithApprovals`" | 5 | Names the macOS setup path. The rule does not move: `panelApprovals` stays out of `CONFIG_WRITABLE`. |
| 7016 | `CrabdServer.allow_reuse_address = False` | Reasoned from Windows `SO_REUSEADDR` semantics, measured during build QA | 5 | Re-measured on macOS, where the likely symptom is the opposite: a restart inside TIME_WAIT failing to bind and printing the false "another crabd is running". |
| 7054 | `_fleet_loop` | Its own thread because "two schtasks subprocesses" must not run on the builder | 3 | The same thread and cadence; only the reader's runner and the comment wording change. |
| 7087 | `main()` | No argv; the Scheduled Task supplies no flags | 1 | The single wiring point for the port: fleet runner, host readers, default port and, if added, the static panel root. Replay-before-the-builder and the OTLP holder dict stay. |
| 7124 | `CrabdServer((HOST, PORT), Handler)` | The one bind, at 127.0.0.1:2722 | 1 | The one bind at the new port, still loopback. |
| 7127 | port-in-use message | "another crabd is running (set CRABD_PORT to run a second instance)" | 5 | The same message at the new port, with the macOS restart path in mind: inside TIME_WAIT it can be a false diagnosis. |
| 7130 | listening message | `crabd {VERSION} listening on http://{HOST}:{PORT}` | 1 | The line that tells the operator where the panel is - the natural place to print the panel URL. |

### Widget (`widget/`)

| Line | Symbol | What it assumes | Port phase | What it becomes |
|---|---|---|---|---|
| `index.html:1` | `<!DOCTYPE html>` | iCUE parses the file as strict XML, not HTML | 6 | Stays XML-clean until the iCUE target is formally dropped, then the constraint and its CI check retire together. |
| `index.html:6` | `<title>tr('SideCrab')</title>` | `tr()` is substituted at iCUE import time | 6 | A literal title. Served to a browser today the tab reads the macro text. |
| `index.html:9` | `x-icue-widget-preview` meta | Store-listing preview image | 6 | Dropped with the store listing; `resources/preview.png` stays only if a README uses it. |
| `index.html:11` | `crabdPort` property, default `'2722'` | The companion's port is operator-editable in the iCUE settings sheet | 1 | Derived from `location.origin` when crabd serves the page, which removes the property and the commonest misconfiguration together. |
| `index.html:15` | pairing-code comment | "Read it with `Install-SideCrab.ps1 -PairingCode`" | 6 | Names the macOS way to read the code. |
| `index.html:18` | `panelToken` property | An iCUE property is "the one place a web page the operator visits cannot read" | 6 | A store the same-origin policy protects, chosen before any code moves. The reader stays a single function. |
| `index.html:29` | `crabStyle` property | Declared a switch because switch / slider / textfield / color are the only types the iCUE validator accepts | 6 | A real enum control; `crabPlain()` already accepts the words, so the change is markup-only. |
| `index.html:49` | `toastEnabled` ("Desktop Toast Alerts") | Names the Windows toast surface, and the label's correctness depends on the notifier's global mute | 7 | Renamed for the macOS notification path, carrying the comment's warning that this label and the group info string are where a narrowed mute goes wrong silently. |
| `index.html:50` | `toastThreshold` slider, 30..600 step 10 default 120 | iCUE slider semantics | 7 | A browser control with the same min, max, step and default. |
| `index.html:67` | `approvalThreshold` slider, 5..300 step 5 default 20 | An iCUE slider has no "untouched" state, which is why the value is written only once moved | 6 | A control that can represent "not set" honestly. The write-only-when-moved rule stays regardless: it is a data-loss guard. |
| `index.html:76` | `budgetEnabled` / `budgetTokens` (77) | The slider is in thousands because the iCUE control renders its own raw value | 6 | A control that formats its own value. The x1000 conversion in `desiredBudgetConfig` moves in the same commit or the budget shifts by 1000x. |
| `index.html:79` | `cpuTempSensor` / `gpuTempSensor` (80) | `data-type="sensors-combobox"`, defaulted by calling the iCUE Sensors plugin | 2 | Either the temperature feature is dropped or it moves server-side into crabd's `host` block. No browser page can read die temperature. |
| `index.html:103` | `<script id="x-icue-groups">` | Read only by the iCUE settings console | 6 | The free specification for the browser settings page: titles, ordering, membership and help prose, already written and reviewed. |
| `index.html:127` | inlined plugin wrappers and the CDATA rationale (127-164) | Bundled inline because iCUE serves widgets from a `file://` GUID folder and parses the file as XML | 6 | Unneeded once crabd serves over http, but it only matters if the sensors survive. |
| `index.html:165` | `class IcueWidgetApiWrapper` | Promise wrapper over the Qt plugin bridge | 2 | Deletes with the sensors feature. Its three documented fixes were each a shipped defect and must not be reimplemented casually. |
| `index.html:200` | `class SimpleSensorApiWrapper` | Every call routes into the iCUE Sensors plugin | 2 | Collapses into one served field if temperatures move server-side. |
| `index.html:365` | `<svg id="crab" viewBox="0 -4 52 44">` | The headroom is a measurement taken at named iCUE slots | 6 | Re-measured at the browser's reference window size and recorded in `widget/DEV.md`, never adjusted on visual judgement. |
| `index.html:737` | `#fleet` glow / toast dots | Two dots watching Windows Scheduled Tasks; `glow` is the parked iCUE lighting helper | 3 | Follows crabd: the row hides itself on a feed with no `fleet` block, so the widget needs no change if the key stops being served. |
| `index.html:762` | `#coreLine` | The CD-33 substitute line, with the stylesheet deciding where it shows | 6 | Unchanged. The JS-knows-nothing discipline is exactly what makes arbitrary window sizes tractable. |
| `index.html:876` | `#sensors` row | One line carrying iCUE temperatures and crabd host figures | 2 | Keeps only the crabd-sourced half unless temperatures move server-side. |
| `index.html:1006` | `#sheetApprovalLeft` | The 55 s hold countdown; at zero the buttons are deliberately not disabled | 1 | Unchanged verbatim, including the deliberate non-disabling. |
| `index.html:1019` | `#sheetApprovalThreshold` | Exists only because "a widget cannot write iCUE properties" | 6 | Becomes unnecessary once a settings surface the panel owns round-trips the value; the presence gate stays until it does. |
| `index.html:1068` | `#sheetDeny` / `#sheetApprove` (1069) | Deny first and styled as the safe default; Approve carries the tool name | 6 | Unchanged. Only the authentication behind the buttons moves. |
| `index.html:1110` | `<script src="scripts/sidecrab.js">` | One flat browser script, no modules, loaded from the iCUE `file://` folder | 1 | Served over http unchanged. The no-bundler discipline is a testability constraint, not a style. |
| `manifest.json:5` | `"description"` | A store listing naming "your iCUE display" and `http://127.0.0.1:2722` | 8 | Rewritten for the new channel or dropped. It also carries a live mojibake em-dash to fix. |
| `manifest.json:6` | `"version"` | The only machine-read widget version | 6 | Something else becomes the single machine-read version, or the release sweep loses its anchor. |
| `manifest.json:12` | `"platform": "windows"` | The most explicit Windows assertion in the widget tree | 6 | Dropped with the manifest. |
| `manifest.json:17` | `"type": "dashboard_lcd"` | Restricts the panel to five known iCUE slot sizes | 6 | Dropped, and with it the guarantee that the CSS breakpoints face a closed set of viewports. |
| `manifest.json:21` | `required_plugins` sensors entry | A hard dependency on the iCUE Sensors plugin | 2 | Dropped with the sensors decision. |
| `translation.json:1` | the whole file | The `tr()` catalogue: one identity-mapped locale, consumed only at iCUE import | 6 | Deleted with the `tr()` wrappers if the settings page keeps literals. It is the wrong shape to grow into real i18n. |
| `styles/sidecrab.css:1` | file header comment | "Xeneon Edge (dashboard_lcd), designed at 2560x720 ... QtWebEngine 6.9.3 / Chromium 130: no `:has()`, no container queries" | 6 | Names the new engine floor. The missing-feature workarounds are re-examined after the port, not during it. |
| `styles/sidecrab.css:8` | `--layout-unit: 1vmin` | Calibrated for a fixed 720 px-tall glass panel | 6 | Clamped or container-derived: a resizable window swings vmin and every size in the file with it. Changing this one declaration retunes the whole panel, which is why the no-raw-viewport-units rule was worth keeping. |
| `styles/sidecrab.css:39` | `--touch-min` | A 48 px fingertip floor for the Edge glass | 6 | Unchanged shape. The floor is platform-independent and the vmin term only ever enlarges a target. |
| `styles/sidecrab.css:71` | `--accent: #BE7E6E` | One of three places the accent default is stated; iCUE overwrites it at runtime | 6 | All three statements move together. The chroma loudness rank behind it is an accessibility constraint, not decoration. |
| `styles/sidecrab.css:96` | `--font-ui` / `--font-mono` (97) | Segoe UI and Cascadia Mono head both stacks | 6 | System fonts first. Every px width comment in the file was measured in Segoe and has to be re-measured. |
| `styles/sidecrab.css:102` | `html, body { overflow: hidden }` | The panel is exactly the viewport and nothing scrolls | 6 | Reconsidered for an arbitrarily small window, where silent clipping is far likelier than across five known slots. |
| `styles/sidecrab.css:272` | `.zones { touch-action: none }` | Measured against the QtWebEngine compositor to keep the pull-down gesture alive | 6 | Re-verified on a trackpad and on mobile Safari before being kept or removed. The sheet stays outside `.zones` either way. |
| `styles/sidecrab.css:348` | standalone-state block | "The SENSORS row stays: iCUE feeds it, not the companion" | 6 | Rewritten: a page crabd serves cannot load without crabd, so the independent-data-source premise inverts. |
| `styles/sidecrab.css:1184` | hardware sensors row block (to 1380) | About 200 lines existing for the iCUE Sensors plugin | 2 | The largest deletable region if temperatures drop. The name and staleness machinery is worth keeping if they move server-side. |
| `styles/sidecrab.css:3160` | `@media (max-width: 1800px)` | Widths chosen against the closed set of iCUE slots | 6 | The mechanism survives; the 266 / 277 px measurements behind it move with the font stack. |
| `styles/sidecrab.css:3178` | `@media (max-height: 420px)` | 840x344 is an iCUE slot | 6 | Kept: 420 px tall is a plausible browser window, and the clamp-pinning discipline it establishes recurs in four places. |
| `styles/sidecrab.css:3255` | shared `<=3:2` block (to 3398) | The three `<=3:2` queries partition the space and must not overlap, and this one must come first | 6 | Kept verbatim as structure. A browser adds size combinations, so the cascade discipline matters more, not less. |
| `styles/sidecrab.css:3401` | near-square block (840x696) | Restates `.zone-limits{display:flex}` to beat an earlier query at equal specificity | 6 | Kept, including the restatement: reordering these blocks re-hides the Limits zone. |
| `styles/sidecrab.css:3494` | portrait block (416x696) | A flex column, because at 416 px the two lower zones cannot sit side by side | 6 | The template for any narrow browser breakpoint. |
| `styles/sidecrab.css:3572` | too-short block (CD-33 core line) | Hides the two lower zones and shows the substitute line in the same rule | 6 | Kept. The same-rule substitution idiom is worth preserving as a pattern. |
| `scripts/sidecrab.js:20` | `POLL_MS` / `POLL_TIMEOUT_MS` (21) | The 2500 ms abort was measured against a Windows loopback refusal | 1 | Re-measured on macOS loopback. The timeout must stay under the interval. |
| `scripts/sidecrab.js:28` | `SCHEMA_MAX = 5` | The breaking-shape ceiling, and the file's only comparison against `doc.schema` | 1 | Untouched by the port. A bump is a coordinated deploy by definition. |
| `scripts/sidecrab.js:341` | `MODEL_CTX_RE` | Parses `[1m]` / `[200k]` out of the verbatim model id | 0 | Unchanged. There is deliberately no model-name table on either side of the wire. |
| `scripts/sidecrab.js:383` | `pairingCode()` | `strProp('panelToken', '')` reads an iCUE-injected global, live on every tap | 6 | One reader over the browser store. The live read and the naming taboo both stay. |
| `scripts/sidecrab.js:387` | `tokenRequired()` | Presence-tested off `approvals.tokenRequired` | 6 | Unchanged. Any new auth is a new field detected by presence, never gated on `doc.schema`. |
| `scripts/sidecrab.js:456` | `MOCKS` | Twelve fixtures fetched from `./mock/mock-state-<name>.json` | 1 | crabd serves `/mock/*`, or the fixtures move under the served tree, or the whole screenshot harness dies with the port. |
| `scripts/sidecrab.js:848` | `icueEvents` (bare assignment) | The iCUE bridge finds the handlers only as an implicit global | 6 | Driven from whatever replaces the property sheet. It must not become a `var`, `let` or `const`. |
| `scripts/sidecrab.js:850` | `getIcueProperty(name)` | Properties arrive as same-named `let` globals; the `Function()` probe exists to read them | 6 | The single chokepoint: its body becomes a browser settings lookup and all nine callers are untouched. A CSP forbidding `unsafe-eval` silently kills the probe, which degrades to defaults. |
| `scripts/sidecrab.js:862` | `boolProp` / `strProp` (869) | Tolerate `'true'` and `'false'` strings because iCUE switches arrive as strings | 6 | Signatures unchanged, and the string tolerance stays so one code path serves both hosts. |
| `scripts/sidecrab.js:882` | `applyProperties()` | Runs only because iCUE fires `onDataUpdated` for any property change | 6 | Called by the settings sheet on every change, so `scheduleConfigSync` and `syncDiag` keep riding the same edge. |
| `scripts/sidecrab.js:888` | `setVar(root, '--accent', ...)` | The runtime-winning statement of the accent default | 6 | Stays the winner; the default moves into the new settings schema alongside the CSS and the meta. |
| `scripts/sidecrab.js:979` | `use24Clock()` | `boolProp('clock24', false)`, with the authoritative default in the property meta | 6 | The default is carried into the settings schema and this stays the only reader. The pair has drifted once already. |
| `scripts/sidecrab.js:1017` | `baseUrl()` | Builds `http://127.0.0.1:<crabdPort>`, so the panel is cross-origin to the companion | 1 | Returns the empty string when crabd serves the panel, making every call a same-origin relative path. |
| `scripts/sidecrab.js:1025` | `poll()` | A 3 s poll with a 2500 ms abort and a relative mock branch | 1 | Unchanged in shape; only the URL source moves. |
| `scripts/sidecrab.js:1056` | `acceptDoc(doc)` | Order-critical: schema gate, then the `crabd.version` latch clear, then trick detection, then render | 0 | Unchanged. The latches cost nothing and a long-lived kiosk tab is exactly what they were written for. |
| `scripts/sidecrab.js:1173` | `crabPlain()` reading `crabStyle` | Accepts a boolean or the switch's string forms | 6 | The tolerant parse is unchanged: it is what lets a control of any shape feed the same function. |
| `scripts/sidecrab.js:1571` | `renderFleet(fleet)` | Renders `glow` and `toast` dots labelled from the served keys | 3 | No change needed. The row hides itself on a feed with no `fleet` block, which is the honest-absence design working. |
| `scripts/sidecrab.js:1619` | prefs vendor-mechanism comment (to 1639) | One JSON object per widget keyed on Corsair's `uniqueId`, because iCUE shares one `file://` origin | 6 | Rewritten for a dedicated origin. The one-object shape and the display-state-only rule stay. |
| `scripts/sidecrab.js:1640` | `prefsStorage()` / `readPrefs` (1650) / `savePrefs` (1736) | `localStorage` wrapped in try/catch, read-modify-write with unknown-value round-tripping | 6 | Unchanged. The read-modify-write is version-skew protection, not vendor compliance, and every access stays wrapped. |
| `scripts/sidecrab.js:1682` | `loadPrefs()` | Keys everything on the iCUE-injected `uniqueId`, so persistence is entirely dead in a browser | 6 | A fixed origin-scoped key. `prefsStoreKey` stays the seam and the null-means-memory-only degrade stays. |
| `scripts/sidecrab.js:2778` | `clampGrid(list, capacity)` | Guarantees a waiting card is never the row the overflow tile swallows | 0 | Unchanged, with its deliberate mutant test kept. |
| `scripts/sidecrab.js:2806` | `gridCapacity()` / `trackCount` (2820) | Reads the computed grid tracks so JS never knows the row count | 6 | Unchanged: it is what makes a resizable window work for free. Only the CSS breakpoints behind it are re-measured. |
| `scripts/sidecrab.js:4347` | `fetchHistory(day)` | `baseUrl() + '/v1/history?day='`, which the Origin gate refuses from a browser | 1 | A same-origin relative path. |
| `scripts/sidecrab.js:4874` | `onSheetDecide` pairing gate | Refuses locally when unpaired, and the message names "widget settings" | 6 | The local refusal is kept verbatim; only the storage behind `pairingCode()` and the two strings move. |
| `scripts/sidecrab.js:4924` | `postAction` decide body | Reads the pairing code live and echoes the sheet's `requestId` | 6 | Unchanged. The token is never cached in a variable. |
| `scripts/sidecrab.js:4985` | `postJson(path, payload)` | Downgrades `application/json` to `text/plain` once, to dodge the preflight the Origin gate refuses | 1 | Same-origin POSTs are never preflighted, so the fallback and its permanent latch can go - but only together with the same-origin move. |
| `scripts/sidecrab.js:6043` | `desiredQuietConfig()` | Reads the `quietEnabled` / `quietStart` / `quietEnd` properties | 6 | Re-sourced from the settings store, preserving the null-versus-`{quietHours: null}` distinction exactly. |
| `scripts/sidecrab.js:6056` | `desiredToastConfig()` and `noteApprovalThreshold` | Reads the toast properties; the approval threshold rides only once moved off its baseline | 7 | Re-sourced. The baseline-then-latch sequencing stays because crabd preserves an omitted `approvalThresholdSec`. |
| `scripts/sidecrab.js:6177` | `desiredBudgetConfig()` | Reads `budgetEnabled` and `budgetTokens` | 6 | Re-sourced; the thousands conversion moves with the control. |
| `scripts/sidecrab.js:6201` | `syncConfigKey()` | Per-key POST with the 404-latches-the-endpoint, 400-does-not policy | 6 | Logic unchanged; only the base URL and the property source move, and nothing here may touch `pollFailed`. |
| `scripts/sidecrab.js:6282` | `sensorsPlugin()` | The single `window.plugins.Sensorsdataprovider` read | 2 | The one seam: returns null on macOS (the row hides itself, already the shipping behaviour) or an adapter over a served temperature field. |
| `scripts/sidecrab.js:6291` | `pluginSensorsdataproviderEvents` (bare assignment) | An implicit global the iCUE bridge looks for | 6 | Deleted with the bridge. While it exists it must stay a bare assignment and the file must stay out of strict mode. |
| `scripts/sidecrab.js:6293` | `initSensors()` | Binds the wrapper and three Qt-style signals | 2 | Replaced by a poll of a served temperature block, or dropped - but `syncSensorRow` still owns the host figures. |
| `scripts/sidecrab.js:6377` | `sensorIdFor(key)` | Resolves the id from the two iCUE properties | 2 | Re-sourced, or hard-returns empty so the row goes off the glass honestly. |
| `scripts/sidecrab.js:6732` | `forcedSensorApi()` | The dev stand-in bridge, exposing exactly three methods | 2 | The natural injection point for any macOS temperature adapter: `readSensor` consumes only those three methods. |
| `scripts/sidecrab.js:6795` | `syncSensorRow()` | Owns both the iCUE warn/hint halves and the crabd host halves | 2 | Keeps the one-owner discipline and the host halves; only the warn and hint branches and their strings go. Removing sensor markup without editing this function throws. |
| `scripts/sidecrab.js:6879` | `renderHost(host)` | Renders crabd's `host` block with member-by-member presence detection | 2 | Ships unchanged. The two width budgets in its comments are re-measured at the new layout. |
| `scripts/sidecrab.js:7027` | `syncHostSheet()` | Title "This PC"; temperatures are text, never a third chart | 8 | Reworded. The temps block goes with the sensors row if that is dropped. |
| `scripts/sidecrab.js:7197` | touch-diagnostics banner (to 7568) | About 370 lines built to discover what the iCUE webview forwards | 6 | Deleted wholesale rather than half-ported: a browser delivers a full pointer stream and the question does not arise. |
| `scripts/sidecrab.js:7230` | `DIAG_PATH = '/v1/panel-log'` | Already a bare path, but `postJson` prepends the absolute base | 1 | Same-origin relative, like every other path. |
| `scripts/sidecrab.js:7267` | `diagWanted()` | Gated on the `touchDiag` property | 6 | Goes with the diagnostics layer, or is re-sourced from the settings store with `syncDiag`'s idempotent reconcile intact. |
| `scripts/sidecrab.js:7892` | `?mock=` parse and the dev-flag block | Every dev flag is gated on `mockName`, on the stated ground that the iCUE origin carries no query string | 6 | Re-gated on an explicit build or dev switch before the panel is served over http. The assumption dies the moment the panel has an addressable URL. |
| `scripts/sidecrab.js:8105` | `&sensorstale=` reassigning `SENSOR_STALE_MS` | A query parameter writes a module-level tunable | 6 | Folded into the same build-flag gate. Nothing may cache that value. |
| `scripts/sidecrab.js:8210` | `maybeAutoGesture()` and `&ackflash=1` (set at 7994) | A query flag performs a real live ack-all POST | 6 | Deleted or hard-gated behind a build flag before the panel is served over http. |
| `scripts/sidecrab.js:8303` | module bootstrap (`readyState === 'loading'`) | The exact line `test_ordering.js` relies on to keep `init()` from running | 6 | The shape is unchanged; changing it breaks the only widget suite. |

### Notifier (`notifier/`)

| Line | Symbol | What it assumes | Port phase | What it becomes |
|---|---|---|---|---|
| `sidecrab_toast.py:1` | module docstring | "a native Windows toast"; lines 10-49 are the measured toast-mechanism log | 7 | Keeps the Windows evidence under a historical heading and adds a macOS route block with its own measurements. Deleting it loses the only record of why pwsh 7 is refused. |
| `sidecrab_toast.py:169` | `__version__` | The comment names `setup/Test-SideCrab.ps1` as the reader of both sides | 8 | The same mechanism with the macOS status command named. |
| `sidecrab_toast.py:171` | `DEFAULT_ENDPOINT` | `http://127.0.0.1:2722/v1/state` | 1 | The new port. There is a second, unlinked copy in the ack handler, so grep rather than trusting one edit. |
| `sidecrab_toast.py:199` | `SUPPORTED_SCHEMAS` | An explicit set; a stale one is a silent standstill behind a running daemon | 0 | Unchanged, and gains any new schema in the same commit. The divergence check that guarded it needs a macOS replacement. |
| `sidecrab_toast.py:214` | `CONFIG_PATH`, `LOG_PATH` (215), `STATE_PATH` (221) | `~/.sidecrab` paths; `STATE_PATH` is the only file this process writes | 0 | Unchanged. These are the clean part of the port. |
| `sidecrab_toast.py:622` | `APPROVAL_HINT` | "Decide on the panel." points at the on-glass widget | 7 | The same wording with a new referent. If it gains a URL, the byte-budget arithmetic beside it stays intact. |
| `sidecrab_toast.py:1026` | `DIGEST_ID_PREFIX` and the other id prefixes, with `STALE_ID` (1442) | Each toast kind gets its own Action Center slot; a reused tag replaces the earlier banner | 7 | Distinct notification identifiers with the same replace-in-place behaviour; Notification Center collides identically. |
| `sidecrab_toast.py:1120` | `SNOOZE_SECTION` and `SnoozeLedger` (1166) | The single-writer argument rests on the snooze handler being a separate, shell-launched process | 7 | If the action is handled in-process, the invariant is restated as one writer per top-level key, with `write_state_section` the only write path. |
| `sidecrab_toast.py:1591` | `TOAST_KINDS` / `toast_kind` | The kind is derived from the id prefix rather than a second field | 0 | Unchanged. |
| `sidecrab_toast.py:1611` | `MUTED_SWITCH_LINE` | A pinned string: `test_mute.py` asserts it verbatim | 0 | Unchanged unless the test moves in the same commit. |
| `sidecrab_toast.py:1636` | banner: "the only Windows-touching code, behind an adapter" | Everything above is pure; 1636-1962 is the platform I/O | 7 | The port's map: a new adapter class beside `PowerShellToastAdapter`, plus one construction site. |
| `sidecrab_toast.py:1640` | `ToastAdapter` (Protocol) | One method, `show(request) -> bool` | 7 | A macOS adapter on the same one-method contract, returning False rather than raising so the re-arm path works unchanged. |
| `sidecrab_toast.py:1645` | `RecordingToastAdapter` | Records instead of showing; serves the suite and `--dry-run` | 0 | Unchanged. `--once --dry-run` already runs on macOS and is the porter's first smoke test. |
| `sidecrab_toast.py:1665` | `ACK_SCHEME` and `SESSION_ID_PATTERN` (1666) | A toast outlives the notifier, so activation must be routable by the shell | 7 | Either a registered macOS URL scheme, which needs an app bundle, or an in-process notification action. The id charset never widens. |
| `sidecrab_toast.py:1692` | `SNOOZE_SCHEME` and `SNOOZE_SEC` (1697) | Two schemes, two handlers, one regex each; 1800 s is printed on the button | 7 | Two distinct actions, never one URI with a verb parameter. The fixed duration stays because the button says it out loud. |
| `sidecrab_toast.py:1710` | `SIDECRAB_AUMID` and `AUMID_REGISTRY_SUBKEY` (1711) | An AppUserModelID identity read out of HKCU | 7 | Collapses to a constant: the posting app's bundle identifier is fixed at build time and cannot be absent. |
| `sidecrab_toast.py:1733` | `probe_registered_aumid` | `import winreg` inside the function so the module imports on any OS | 7 | Deleted with the AUMID block. The lazy-import-inside-the-function pattern is the one to copy for any future platform call. |
| `sidecrab_toast.py:1769` | `registered_aumid` | Positive answers latch, negative ones expire, and an injected probe is never cached | 7 | Deleted. The never-cache-an-injected-probe rule carries to any macOS equivalent with a module-global cache. |
| `sidecrab_toast.py:1794` | `PowerShellToastAdapter` and `POWERSHELL_EXE` (1798) | System32 PowerShell 5.1, pinned because pwsh 7 lacks the WinRT projection | 7 | Replaced wholesale, or kept untouched beside a macOS adapter if Windows support is retained. |
| `sidecrab_toast.py:1824` | `PowerShellToastAdapter.aumid` | Borrows Windows PowerShell's identity when SideCrab's is unregistered | 7 | Deleted. `Notifier.run` already reads it through `getattr`, so a macOS adapter needs nothing there. |
| `sidecrab_toast.py:1858` | `build_xml` | Windows ToastGeneric XML with two protocol actions | 7 | A macOS payload builder keeping three decisions: both buttons live or die together, a bad session id drops the buttons and never the toast, and Acknowledge stays first. |
| `sidecrab_toast.py:1876` | icon URI construction | `replace("\\", "/")` and `safe="/:"` for the Windows drive colon | 7 | A no-op on macOS and therefore easy to "simplify" away. It stays verbatim while Windows ships. |
| `sidecrab_toast.py:1912` | `build_script` | WinRT projections loaded from PowerShell, with the payload crossing as base64 | 7 | Deleted for macOS, but the base64 boundary discipline carries: `osascript` is the same quoting hazard PowerShell was. |
| `sidecrab_toast.py:1937` | `show()` | `-EncodedCommand` UTF-16LE, `CREATE_NO_WINDOW`, returns False and never raises | 7 | Replaced, keeping the two properties the daemon depends on: a bool return and a timeout. |
| `sidecrab_toast.py:1965` | `fetch_state` | A urllib GET carrying no Origin, so it passes the gate today | 1 | Endpoint only. If any of this moves into browser JS on the served panel its fetches carry an Origin, which is a crabd-side decision the notifier must not assume away. |
| `sidecrab_toast.py:1984` | `RUNTIME_SECTION` and `RuntimeStamp` (1994) | Comments name `Test-SideCrab.ps1` and `Repair-SideCrab` as the consumers | 5 | The whole mechanism is kept - the stale-code problem is worse under launchd - with the consumer names updated. |
| `sidecrab_toast.py:2077` | `Notifier._emit` | The single global-mute point; the comment quotes the panel label "Desktop Toast Alerts" | 7 | No logic change. If the panel renames the switch, this comment and the panel move together. |
| `sidecrab_toast.py:2195` | `Notifier.poll_once` | `StaleFeedDecider` runs first, ahead of every early return | 0 | Unchanged. That ordering is the one a tidy-up would break. |
| `sidecrab_toast.py:2326` | `default_icon()` | Ships `sidecrab.png` beside the module | 7 | The PNG ports to a macOS notification image; `sidecrab.ico` is dead weight unless Windows ships. |
| `sidecrab_toast.py:2331` | argument parser | `--endpoint` defaults to the 2722 URL; `--version` keeps stdout clean for the setup lane | 1 | Only the default moves. The stdout purity is a contract with whatever replaces the status script. |
| `sidecrab_toast.py:2394` | `main`: `real = PowerShellToastAdapter(...)` | The one construction site that makes the notifier Windows-only at runtime | 7 | Platform-selects the adapter here. Every `--test-*` branch below it gets the new adapter for free. |
| `sidecrab_ack_handler.pyw:5` | module docstring | `pythonw` invocation, HKCU registration, silent by construction | 7 | Restated for whatever the macOS activation is. The URI-is-data and no-traceback doctrines survive the transport change. |
| `sidecrab_ack_handler.pyw:48` | `ACK_SCHEME` and its `IGNORECASE`/`ASCII` regex (53) | Case-insensitivity exists because Windows resolves the registry key that way | 7 | Both flags kept: LaunchServices is also case-insensitive, and `re.ASCII` is a homoglyph defence unrelated to platform. |
| `sidecrab_ack_handler.pyw:61` | `ACTION_ENDPOINT` | The second, unlinked copy of the port | 1 | The new port, in the same commit as the notifier's. No pairing token is added here: `ack` is not `decide`. |
| `sidecrab_snooze_handler.pyw:5` | module docstring | The same `pythonw` and HKCU shape | 7 | Ported alongside the ack handler. The "snoozing is not answering" doctrine is entirely platform-independent. |
| `sidecrab_snooze_handler.pyw:59` | `SNOOZE_SCHEME` and `SNOOZE_SEC` (74) | Duplicated across two files on purpose, with tests as the only link | 7 | The duplication is preserved; the handlers must not start importing `sidecrab_toast`. |
| `sidecrab_snooze_handler.pyw:124` | `apply_snooze` | Rebuilds the map from what is readable and preserves every other top-level key | 0 | Unchanged. That rule is the cross-process contract with `write_state_section`. |

### Hooks and status line (`hooks/`)

| Line | Symbol | What it assumes | Port phase | What it becomes |
|---|---|---|---|---|
| `settings-hooks-fragment.json:8` | `SessionStart` command hook, and 19 / 30 / 52 / 74 | `curl.exe -s -m 2 ... http://127.0.0.1:2722/v1/hook \|\| exit 0` - the Windows System32 binary and the port, five times | 1 | `curl` and the new port, emitted from one template so the value appears once. `-s -m 2` and `\|\| exit 0` stay verbatim. |
| `settings-hooks-fragment.json:41` | `Stop` http hook url | `http://127.0.0.1:2722/v1/hook/stop`, timeout 5 | 1 | Port change only. It is a two-way hook: crabd's answer is the feature, so it must not become a curl call. |
| `settings-hooks-fragment.json:63` | `PermissionRequest` http hook url | Timeout 60, deliberately just past crabd's 55 s poll | 1 | Port change only. 60 > 55 is the invariant. |
| `sidecrab_statusline.py:5` | module docstring | Wired by `setup\Install-SideCrab.ps1`, with a `python.exe` and backslash command shape | 8 | Names the macOS installer and a POSIX command shape. |
| `sidecrab_statusline.py:49` | `STATUSLINE_ENDPOINT` | The port hard-coded in a Python constant | 1 | The new port, or an env override with the new default; `lighting/sidecrab_glow.py` already shows the pattern. |
| `sidecrab_statusline.py:55` | `POST_TIMEOUT_SEC = 0.4` | A measured budget: the status line blocks the operator's prompt | 0 | Unchanged. |
| `sidecrab_statusline.py:120` | `minimal_status` cwd basename | `cwd.rstrip("\\/")` tolerates both separators | 0 | Unchanged; stripping both separators stays correct and costs nothing. |
| `sidecrab_statusline.py:153` | `run_chained` | `shell=True` so a prior `.ps1`, node or other command still works when chained | 0 | `shell=True` and the 5 s backstop stay; only the comment's example changes. |
| `README.md:15` | curl bullet | Names `C:\Windows\System32\curl.exe`, "not Git Bash's" | 8 | Notes that `/usr/bin/curl` ships on every supported macOS. |
| `README.md:59` | verified-against-the-binary section | Facts read out of `claude.exe` v2.1.246 | 8 | The facts are kept and re-verified against the macOS `claude` before any claim is edited. |
| `README.md:79` | `allowedHttpHookUrls` note | The pattern must admit `http://127.0.0.1:2722/*` or both http hooks are blocked and approvals silently do nothing | 1 | The new port in the pattern, with the doctor row that checks it moving alongside. |
| `README.md:93` | merge-marker section | Install and uninstall match SideCrab's entries on the `127.0.0.1:2722/v1/hook` substring, which is a prefix of the http urls | 5 | The new marker, keeping the prefix property so one marker still finds both `command` and `url` entries. An installed base needs a migration that matches both. |

### Setup (`setup/`)

| Line | Symbol | What it assumes | Port phase | What it becomes |
|---|---|---|---|---|
| `Install-SideCrab.ps1:66` | `param` block | Backslash literals inside `Join-Path`; on macOS pwsh these become single files literally named `.claude\settings.json` | 5 | Rewritten in Python or shell. The path set is portable; only the separators and the DPAPI blob are not. |
| `Install-SideCrab.ps1:70` | the deliberate absence of `-TaskName` | Single-instance by construction because the port is fixed in the catalogue, the hook fragment and the status-line command | 5 | The non-feature and its reasoning are kept; a fixed 9999 keeps the same property. |
| `Install-SideCrab.ps1:99` | `$HookUrlMarker` | `127.0.0.1:2722/v1/hook`, duplicated verbatim in the uninstaller | 5 | One shared constant for install and uninstall, never two copies. |
| `Install-SideCrab.ps1:105` | `Backup-File` | `<path>.sidecrab-bak-yyyyMMdd-HHmmss` in local time | 5 | The exact shape is kept: the backup pattern and stamp readers parse it and the restore path depends on it. |
| `Install-SideCrab.ps1:114` | `Merge-HookFragment` | Entry-level merge, keeping the foreign half of every shared matcher | 5 | Ported verbatim to Python. The most reusable algorithm in `setup/`. |
| `Install-SideCrab.ps1:141` | `Get-ComponentPlan` | The thin impure wrapper around a pure decision helper | 5 | The pure/impure split is kept: it is what lets the suite test decisions without installing anything. |
| `Install-SideCrab.ps1:154` | `Show-Status` (`-Status`) | Read-only rows including tasks, aumid and proto | 5 | The same row set minus aumid and proto, keeping two rules: the pairing row reports presence only, and the approvals row always names the hook wiring so ON is never a bare word. |
| `Install-SideCrab.ps1:292` | `-LimitsToken` branch | Prompts for a token and stores it DPAPI-protected | 4 | Keychain or a 0600 file, moving in the same change as crabd's reader, keeping both validations and the zeroing discipline. |
| `Install-SideCrab.ps1:303` | `-PairingCode` branch | Prints the code and points the operator at iCUE widget settings | 6 | The same flow and the same exit-1-on-absence distinction, with the destination becoming the panel's own settings surface. |
| `Install-SideCrab.ps1:320` | `$fragment` and the settings write | Backslash path; `ConvertTo-Json -Depth 40` because a shallower depth silently stringifies the nested hooks | 5 | Forward slashes and an equivalent depth guarantee. |
| `Install-SideCrab.ps1:330` | task registration block | Windows Task Scheduler end to end, with a dependency preflight under the same interpreter | 5 | A LaunchAgent per component, preserving the semantics: re-register keeps paths current, a user-disabled agent stays disabled and is not started, and `-ForceEnable` is the only override. |
| `Install-SideCrab.ps1:379` | toast HKCU registrations | AppUserModelID and shell URL-protocol activation, shipped only with the toast component | 7 | No AUMID equivalent exists; in-process notification actions delete the "unregistered scheme silently no-ops" failure class entirely. |
| `Install-SideCrab.ps1:405` | `settings.json` write block | One read, one backup, one write, with a console-python resolution for the status line | 5 | The ordering is ported exactly. The only-save-a-prior-that-is-not-ours guard and the `padding` carry-forward are the two easy things to lose. |
| `Install-SideCrab.ps1:461` | `panelApprovals` block | OFF by default; `false` is written only when the key is absent | 5 | Ported unchanged, including the write-only-when-absent rule and the notice's three guarantees. |
| `SideCrab.Common.ps1:18` | `Get-SideCrabComponentSpec` | The catalogue: backslash paths, Scheduled Task names, descriptions naming the Xeneon Edge and iCUE | 5 | The catalogue pattern is kept as the single extension point, with forward slashes, launchd labels, the glow row dropped and descriptions reworded. |
| `SideCrab.Common.ps1:34` | `crabd.Port = 2722` | The one component that owns a TCP port, written down so a restart path can ask rather than assume | 1 | 9999, still read off the row and never inlined at the restart site. |
| `SideCrab.Common.ps1:36` | `WatchFiles` | Every file whose mtime makes the running process stale, not just the entry point | 5 | The newest-of-many rule is kept for any multi-file component. |
| `SideCrab.Common.ps1:89` | `Select-SideCrabComponent` | Pure: a switch passed for a missing script is a Problem, never a silent skip | 5 | Ported as-is. The best-tested pure helper in the tree. |
| `SideCrab.Common.ps1:152` | `Get-SideCrabAumidSpec` | Windows toast identity in HKCU | 7 | Deleted. Nothing in the panel or crabd reads it. |
| `SideCrab.Common.ps1:175` | `Get-SideCrabAumidIconDecision` | Registry-only; an absent value beats a dead pointer | 7 | Deleted with the AUMID; the lesson is the same honest-failure rule crabd follows. |
| `SideCrab.Common.ps1:212` | `Get-SideCrabProtocolSpec` | Two HKCU URL schemes, because a toast outlives the process that raised it | 7 | Deleted; macOS notification actions come back to the posting process with no registry step. |
| `SideCrab.Common.ps1:257` | `Get-SideCrabProtocolCommand` | The Windows `%1` shell placeholder and its quoting | 7 | Deleted with the protocol surface. |
| `SideCrab.Common.ps1:288` | `Get-SideCrabTaskEnableDecision` | `Register-ScheduledTask -Force` always writes an enabled task | 5 | Kept: launchd bootstrap has the same trap and a `launchctl disable` must be honoured. |
| `SideCrab.Common.ps1:322` | `Get-SideCrabHookEvent` | Matches `command` and `url` concatenated so one marker finds both kinds | 5 | The concatenation trick is ported exactly, with the marker defaulted from a shared constant. |
| `SideCrab.Common.ps1:355` | `Get-SideCrabStatusLineSpec` | Backslash paths; the `sidecrab_statusline` marker is portable | 5 | Forward slashes, marker unchanged: the ownership test, the settings split and the restore decision all key off it. |
| `SideCrab.Common.ps1:389` | `Resolve-SideCrabPythonConsole` | `python.exe` versus `pythonw.exe`, skipping the WindowsApps alias stub | 5 | Collapses to python3 resolution, but must still reject the Apple 3.9 stub by version rather than by path. |
| `SideCrab.Common.ps1:402` | `Save-SideCrabPriorStatusLine` | The file's presence marks that SideCrab took the slot; null means nothing was there | 5 | Ported verbatim including the presence-versus-null distinction. |
| `SideCrab.Common.ps1:460` | `Write-SideCrabFileAtomic` | Same-volume temp plus a Move with overwrite, with a deliberately distinct temp infix | 5 | `os.replace` on a same-directory temp, keeping the distinct infix so a leftover is never mistaken for a backup. |
| `SideCrab.Common.ps1:518` | `Get-SideCrabPanelToken` | Reads `~/.sidecrab/panel-token` and formats it 5-5 against a Crockford-style alphabet | 6 | The regex and formatting are ported exactly: the panel's pairing field and crabd's minting both assume the alphabet. |
| `SideCrab.Common.ps1:549` | `Set-SideCrabLimitsToken` | DPAPI CurrentUser with no entropy, matching crabd's reader | 4 | Keychain generic password or a 0600 file, moving with crabd's reader; the `sk-ant-` validation and the zeroing stay. |
| `SideCrab.Common.ps1:582` | `Set-SideCrabPanelApprovals` | Read-modify-write of `config.json` preserving every other key | 5 | Ported directly. The preserve-every-other-key rule must not drift from crabd's `/v1/config`. |
| `SideCrab.Common.ps1:649` | `Resolve-SideCrabPython` | Prefers `pythonw.exe` so daemons run with no console | 5 | Collapses to python3 resolution; the console-versus-windowless distinction disappears. |
| `SideCrab.Common.ps1:668` | `Register-SideCrabTask` | One task shape: at logon, hidden, restart three times, no execution time limit, single instance | 5 | A LaunchAgent with `RunAtLoad` and `KeepAlive`, reading the prior enable state before bootstrapping. |
| `SideCrab.Common.ps1:719` | `Get-SideCrabTaskState` | `Get-ScheduledTask` / `Get-ScheduledTaskInfo`; an absent task is a state, not an error | 3 | `launchctl print` parsed into the same shape, still never throwing. |
| `SideCrab.Common.ps1:742` | `Get-SideCrabPortHolder` | `Get-NetTCPConnection` plus `Get-Process`; health-by-HTTP cannot tell who answered | 5 | `lsof` equivalent, keeping the injection seam, the never-throws rule and the PID in the output. |
| `SideCrab.Common.ps1:804` | `Wait-SideCrabPortRelease` | The budget is counted in polls, not wall clock, because the sleep is injectable | 5 | Kept, budget and all. Do not flip crabd's `allow_reuse_address` casually: the loud refusal is the feature. |
| `SideCrab.Common.ps1:859` | `Restart-SideCrabTask` | The one restart path; on port-release timeout it refuses to start and names the PID | 5 | `launchctl kickstart -k` with the same two waits, the same refuse-to-start-blind rule and the same injection seam. |
| `SideCrab.Common.ps1:1048` | `Get-SideCrabProtocolState` | The registry `URL Protocol` flag is what Windows actually gates on | 7 | Deleted with the protocol surface. |
| `SideCrab.Common.ps1:1104` | `Set-SideCrabProtocol` | HKCU registration, skipping a scheme whose handler file is missing | 7 | Deleted. |
| `SideCrab.Common.ps1:1203` | `Get-SideCrabHealth` | `http://127.0.0.1:2722/v1/health` as a default parameter | 1 | The new port, keeping the verdict-object shape rather than throwing. |
| `SideCrab.Common.ps1:1234` | `Get-SideCrabHealthProbe` | One retry after a backoff, because a single GET reads a healthy crabd as dead | 5 | Kept, and `RecoveredOnRetry` stays visible: a crabd that needs two attempts is not one that answers first time. |
| `SideCrab.Common.ps1:1277` | `Get-SideCrabServiceVerdict` | Four verdicts from two readings; an answer with no running task is the loudest row | 5 | The four-case table ported with launchd states. The most valuable reasoning in `setup/`, and it is pure. |
| `SideCrab.Common.ps1:1335` | `Get-SideCrabWidgetVersion` | "The widget ships through iCUE, not through this repo's install path" | 6 | Collapses once crabd serves the panel: deleted, or repointed at the served panel's version. |
| `SideCrab.Common.ps1:1417` | `Get-SideCrabCanonicalValue` | Key-sorted JSON for change detection; arrays keep their order on purpose | 5 | `json.dumps(sort_keys=True)` gives this for free; the array-order rule is kept. |
| `SideCrab.Common.ps1:1452` | `Test-SideCrabHookMatcherIsOurs` | Public API with zero production callers and an explicit do-not-reintroduce warning | 5 | Dropped, or carried with its warning. Reintroducing it on an ownership path re-opens the defect. |
| `SideCrab.Common.ps1:1472` | `Split-SideCrabHookMatcher` | The ownership primitive: entry-level, returning the original object for an unshared matcher | 5 | Ported with the same three return shapes and the identity-preserving unshared path. |
| `SideCrab.Common.ps1:1529` | `Split-SideCrabSettings` | Splits `settings.json` into ours and foreign so a restore is safe to reason about | 5 | Ported with its comparison helper: a foreign diff gates a restore, our own diff is informational. |
| `SideCrab.Common.ps1:1629` | `Get-SideCrabPruneDecision` | The newest backup is never pruned whatever its age | 5 | The never-prune-the-newest rule is ported. |
| `SideCrab.Common.ps1:1658` | `Get-SideCrabResidueSpec` | The uninstall table: wiring removed, data kept, backups kept at every switch | 5 | The table and the rule are ported so uninstall and the residue report can never disagree. |
| `SideCrab.Common.ps1:1720` | `Get-SideCrabUninstallScope` | Ownership for a narrowed uninstall; an unknown name owns nothing beyond its own task | 5 | Ported; the unknown-name rule must survive or a targeted uninstall becomes a full one. |
| `SideCrab.Common.ps1:1762` | `Get-SideCrabStatusLineRestoreDecision` | Never write over a status line that is not ours; an absent line is not a foreign one | 5 | Ported verbatim, keeping `CurrentIsOurs` a parameter rather than re-deriving it inside. |
| `SideCrab.Common.ps1:1815` | `Get-SideCrabPullPreflight` | Classifies `git status --porcelain`; the `-like '??*'` wildcard trap is PowerShell-specific | 5 | The classification is ported; the trap itself does not exist in Python. |
| `SideCrab.Common.ps1:1859` | `Get-SideCrabGlowPreflight` | `cuesdk` is the Corsair iCUE SDK | 5 | Deleted with the glow component. The inference-versus-instruction distinction is worth keeping for any future optional component. |
| `SideCrab.Common.ps1:1913` | `Get-SideCrabRunStateDecision` | Every SideCrab task is a logon daemon, so Ready means the process is gone | 3 | The same rule under launchd (loaded but no PID is not running), keeping the caller-side exclusion of crabd. |
| `SideCrab.Common.ps1:1948` | `Get-SideCrabStaleCodeDecision` | Running-but-on-stale-code is invisible to every other check | 5 | Ported directly. This class gets more likely during a port, not less. |
| `SideCrab.Common.ps1:1987` | `Test-SideCrabHookUrlAllowed` | `allowedHttpHookUrls` is a wildcard allow-list; unset allows everything, empty blocks us | 1 | Ported exactly including the null-versus-empty distinction, with the patterns re-checked against the new port. |
| `SideCrab.Common.ps1:2002` | `Get-SideCrabCommandPath` | The bare-token regex only matches Windows drive-letter paths | 5 | A POSIX absolute or `~` pattern; the quoted-first ordering stays right. |
| `SideCrab.Common.ps1:2023` | `Get-SideCrabPathOwnership` | Normalises `/` to `\` and compares with a trailing separator | 5 | Normalises to `/` and keeps the trailing-separator guard; the foreign-checkout finding is the one worth having. |
| `Test-SideCrab.ps1:31` | `$BaseUri` | `http://127.0.0.1:2722`; every probe derives from this one parameter | 1 | The new port. The cleanest single-parameter change in the tree. |
| `Test-SideCrab.ps1:47` | `$SupportedSchemas` / `$MaxLagSec` (51) / `$MinSafeThresholdSec` (55) | 30 s deliberately equals the panel's stale threshold; 60 s exists because a real toast could fire | 5 | 30 stays pinned to whatever the panel's stale threshold becomes; the safe threshold survives only if notifications do. |
| `Test-SideCrab.ps1:190` | hook round-trip block | The one non-read-only leg, with `SessionEnd` posted from a `finally` | 5 | Ported including the finally-block cleanup: it is the only write path the smoke test has. |
| `Test-SideCrab.ps1:266` | notifier and glow schema rows | Greps each consumer's schema constant out of its source and compares it to the live feed | 5 | One row per surviving consumer, including the panel's own `SCHEMA_MAX`. This is the check that catches producer/consumer divergence. |
| `Test-SideCrab.ps1:338` | status-line chain row | Four ANDed conditions, including that the command points at this repo's copy | 5 | All four ported; the points-at-this-repo check is the subtle one. |
| `Test-SideCrab.ps1:384` | limits token row | Reads the `.dpapi` path; reported, never judged | 4 | Ported against the new store, keeping report-never-judge. |
| `Test-SideCrab.ps1:402` | panel approvals row | OFF is reported, ON fails unless the hook is wired, unblocked and a pairing code exists | 6 | Ported unchanged with the new port. It is the security-posture check for the whole approvals feature. |
| `Uninstall-SideCrab.ps1:100` | `$HookUrlMarker` | The second copy of the install marker | 5 | Shares one constant with install. |
| `Uninstall-SideCrab.ps1:113` | `Remove-HookEntries` | Removes entries, not matchers, and drops an empty `hooks` object | 5 | Ported exactly; it must stay the mirror image of the merge. |
| `Uninstall-SideCrab.ps1:282` | status-line restore decision | Ownership first, then restore; preserve-foreign prints loudly and names the prior it did not restore | 5 | The ordering and the loud path are ported, plus the stranded-chain-file catch that prints before deleting. |
| `Uninstall-SideCrab.ps1:369` | `-Purge` and the residue report | The state directory is removed only when the purge emptied it | 5 | The report and the purge-if-empty guard are ported; the two printed remedy commands are rewritten. |
| `Update-SideCrab.ps1:91` | `git pull --ff-only` step | Clean-tree preflight before the pull | 5 | Ported as-is; only the stderr-folding wrapper is PowerShell-specific. |
| `Update-SideCrab.ps1:132` | restart step | Restarts only registered tasks, building the port map off the catalogue | 5 | Ported including the catalogue-derived port map and the leave-disabled rule. |
| `Update-SideCrab.ps1:171` | verify step | Health and task state together, re-read after the wait, non-zero exit on failure | 5 | Both readings ported; the remedy text becomes `lsof` plus `kill`. |
| `Update-SideCrab.ps1:228` | post-update aumid and protocol report | Registry rows, with "missing is a state this report must name" | 7 | Deleted with the registry surface, carrying the principle: an absent thing produces a row, not silence. |
| `Update-SideCrab.ps1:253` | closing warning | "The WIDGET is not updated by this script ... importing the `.icuewidget` package into iCUE" | 8 | Deleted once crabd serves the panel: a pull then does update what the browser loads. Worth a CHANGELOG line as a genuine simplification. |
| `lighting/decision.py:24` | `ACCEPTED_SCHEMAS` | `lighting/` is a read-only consumer of crabd, reachable only through the setup catalogue | 5 | Dropped from the catalogue with its doctor row; nothing under `companion/`, `widget/`, `notifier/` or `hooks/` references it. |

### Tests

| Line | Symbol | What it assumes | Port phase | What it becomes |
|---|---|---|---|---|
| `companion/tests/test_crabd.py:34` | `setUpModule` / `tearDownModule` (71) | Repoints four module globals at a temp dir so a run cannot poison the operator's real files | 4 | Whatever replaces the DPAPI store joins this module-scope guard. If the store becomes a Keychain item rather than a file, the isolation seam changes shape: inject the store object instead of repointing a path global. |
| `companion/tests/test_crabd.py:118` | `assistant_line` and the five sibling fixture helpers (136, 141, 156, 172, 186) | Every transcript fixture defaults to `cwd="C:\IT"` | 0 | A POSIX cwd default with the matching project slug, changed in the six centralised helpers rather than at 134 call sites, keeping a few explicit Windows-cwd cases. |
| `companion/tests/test_crabd.py:195` | `TempProjects` | `session_path(..., project="C--IT")` is the Windows-path slug | 0 | A POSIX slug; the class is otherwise fully portable and is the primary fixture for about forty test classes. |
| `companion/tests/test_crabd.py:271` | `CwdTitleTests` | Eight tests pinning drive roots, UNC shares and both separators | 0 | Kept in full, with macOS cases added. If `_cwd_title` ever moves off `PureWindowsPath`, the UNC and root cases fail first. |
| `companion/tests/test_crabd.py:1324` | `LimitsTokenFallbackTests` | Stubs `_dpapi_unprotect` as a plain callable and repoints the `.dpapi` path | 4 | Ported with a rename only; the stub-a-callable shape stays, which is why any replacement must remain a module-level function of the same name. |
| `companion/tests/test_crabd.py:1425` | the DPAPI round-trip skip (class at 1426) | `@unittest.skipUnless(sys.platform == "win32")` around the only real round trip | 4 | A macOS counterpart guarded on `darwin`, asserting the same two properties: a stored token reads back exactly, and a tampered or unreadable store returns None. |
| `companion/tests/test_crabd.py:2042` | `test_state_matches_the_contract_shape` | `assertEqual` on the sorted top-level key list, including `host` and `fleet` | 2 | The contract tripwire for the whole port. Any new key lands in the contract, then crabd, then this list; it must not be loosened to `assertIn`. |
| `companion/tests/test_crabd.py:4413` | `FakeSchtasks` and `schtasks_csv` (4429) | Fixture strings are schtasks CSV output and measured Windows error text | 3 | A launchctl fake behind the same `runner=` seam, keeping the five mapping outcomes and the four never-guess cases. |
| `companion/tests/test_crabd.py:9428` | `ScriptedCounters` and `scripted_sampler` (9457) | FILETIME semantics: kernel time contains idle | 2 | A new scripted-counter fixture for the Darwin counter semantics, naming the wrong answer the Windows formula would give on it. |
| `companion/tests/test_crabd.py:9464` | `HostSamplerCpuMathTests` | 62.5 and 100.0 are named as the two wrong answers beside the correct 40.0 | 2 | The method ports, the numbers do not: on disjoint macOS counters 62.5 becomes the correct answer for the same three inputs. |
| `companion/tests/test_crabd.py:9594` | memory, honest-failure and builder-wiring host tests | `(total, available)` is the `GlobalMemoryStatusEx` shape | 2 | The arithmetic fixtures port unchanged; every honest-failure and wiring test touches only the injected callables. |
| `companion/tests/test_crabd.py:9745` | `HostSamplerLiveReadTests` (skip at 9744) | The one place the arithmetic meets a real kernel | 2 | Re-guarded on `darwin` over the Darwin readers. Every bound assertion in the body is platform-neutral and stays word for word. |
| `companion/tests/test_crabd.py:7709` | `_shipped_claude_binary` and the pin tests at 7724 | The default probe path is a Windows WinGet Node install, so the three tests skip on macOS | 0 | A macOS probe path, keeping `CRABD_CLAUDE_BINARY` as the escape hatch and the needle set unchanged. |
| `companion/tests/test_crabd.py:3055` | `ServedOverASocket` | Builds a live crabd on port 0 and sets `Handler.builder` as a class attribute | 1 | Unchanged, and it is the seam a static panel route rides for free. |
| `companion/tests/test_crabd.py:3144` | action-endpoint Origin tests (3155, 3166) | A cross-site https origin is 403, `null` is 204 with a reflected ACAO | 1 | Gains a self-origin case; the rest is unchanged. |
| `companion/tests/test_crabd.py:4029` | config-endpoint Origin tests (4037) | The same premise on `POST /v1/config` | 1 | Gains a self-origin case. |
| `companion/tests/test_crabd.py:5933` | `test_history_carries_the_same_cors_as_the_other_gets` (400 case at 5941) | Errors carry CORS too, because the panel reads the status | 1 | Kept: the rule matters more for a browser panel, not less. |
| `companion/tests/test_crabd.py:8565` | `test_a_forged_null_origin_with_no_code_cannot_approve` | The SEC-a reproduction: `Origin: null` plus a harvested session and request id is still refused | 6 | Kept verbatim. The pairing code is what keeps `decide` safe once the panel has a real, guessable origin. |
| `companion/tests/test_crabd.py:10285` | `PanelLogEndpointTests` | The ring exists because the iCUE webview has no console | 1 | Kept if the endpoint survives; the stored-unexamined safety argument must stay true. |
| `companion/tests/test_crabd.py:10709` | the UA-classification gate test | A QtWebEngine-on-Windows user agent fixture | 1 | The fixture UA moves to a macOS browser; the gate/UA separation assertion is kept. |
| `companion/tests/test_crabd_livefire.py:108` | `TimestampBoundTests` | The sub-epoch case came free from a Windows `OSError` | 0 | Verified to rest on explicit bounds, not a caught platform exception; macOS accepts negative epochs happily. |
| `companion/tests/test_crabd_livefire.py:305` | `LiveFireServed` | The fully wired fixture; `release_parked` is registered last so it runs first | 1 | Portable as-is apart from two comments and a Windows cwd literal. The LIFO cleanup discipline is load-bearing on any platform. |
| `companion/tests/test_crabd_livefire.py:410` | `statusline_doc` | Windows path values throughout, deliberately over-complete | 0 | POSIX path values, keeping the deliberate over-completeness that caught the `resets_at` crash. |
| `companion/tests/test_crabd_livefire.py:730` | `Sec1OriginGateLiveFireTests` | Seven tests pinning the write-side gate against the iCUE `null` origin | 1 | Kept as the CSRF proof, with a parallel set for the self origin and a proof that a different localhost port is still refused. |
| `companion/tests/test_crabd_livefire.py:1135` | `Sec4ReadGateLiveFireTests` | Ten tests pinning the read-side gate, including a `qrc://` iCUE scheme case | 1 | The no-wildcard sweep and the fail-closed default are kept verbatim; the `qrc://` case is supplemented by the browser cases that matter. |
| `companion/tests/test_crabd_livefire.py:1182` | `test_an_http_origin_is_refused_as_well_as_https` | Asserts `http://127.0.0.1:2722` - crabd's own production origin - is 403 | 1 | The self-origin case moves to a non-self loopback port so cross-site refusal is still proven, and a new test asserts the self origin is allowed and reflected. |
| `companion/tests/test_crabd_livefire.py:1420` | `ColdStartBuildRaceTests` (unlink swallow at 1507) | Windows refuses to unlink a file a builder has open | 0 | The except branch becomes dead code on macOS; left in place with the comment naming it Windows-only. |
| `companion/tests/test_crabd_livefire.py:1734` | `StateEndpointNeverFivesTests` (hang-up fixture at 1796) | The hang-up is injected as a WinSock `ConnectionAbortedError(10053)` | 0 | An error the macOS stack actually produces. This matters more with a browser panel: a tab closing mid-poll is the routine case. |
| `companion/tests/_httpkeepalive.py:1` | module docstring | The whole rationale is a measured Windows TCP pathology | 0 | The harness is kept and the measurement is retained as Windows-dated evidence; the counters simply read zero on macOS. |
| `companion/tests/_httpkeepalive.py:237` | `start_test_server` (assertion at 254) | `assert port != 2722` hard-codes the production port | 1 | Asserted against a single shared default-port constant so the guard cannot drift from the value it protects; four call-site assertions move with it. |
| `companion/tests/test_crabd_datalane.py:568` | `BuilderHarness` | Reader-wiring with no socket | 0 | Unchanged. The shape to copy for any new reader the port adds. |
| `notifier/tests/test_decider.py:516` | `test_icon_uri_keeps_the_drive_colon_unescaped` | Asserts a drive letter in the toast image URI - the one expected off-Windows failure | 7 | Skipped off Windows with a reason, or generalised to a platform-neutral encoding assertion. Never deleted. |
| `notifier/tests/test_decider.py:539` | `test_pinned_to_windows_powershell` | Pins the System32 PowerShell path | 7 | Replaced by a pin on whichever emitter the macOS adapter uses. |
| `notifier/tests/test_decider.py:614` | `SchemaTests` | Reads `docs/STATE-CONTRACT.md` and asserts `SUPPORTED_SCHEMAS` covers every declared schema | 0 | Kept verbatim. One of the highest-value tests in the tree, and the contract file survives the port. |
| `notifier/tests/test_decider.py:650` | `AumidTests` | AppUserModelID has no macOS analogue | 7 | Deleted. The one idea worth keeping is the injected-probe pattern: never let a test pass or fail on machine state. |
| `notifier/tests/test_icon.py:3` | the whole file | Validates the Windows `.ICO` the AUMID IconUri points at | 7 | Deleted, or reduced to the PNG round-trip and downscale tests if the PNG is reused as a web icon. |
| `notifier/tests/test_ack_handler.py:26` | `HANDLER_PATH` and `_load_handler` | `.pyw` is not in `SOURCE_SUFFIXES`, so the handler is loaded by an explicit loader | 7 | Renaming the handler to `.py` removes the boilerplate; the parse and log-hygiene tests survive unchanged. |
| `notifier/tests/test_ack_handler.py:174` | `test_posts_the_contract_body_to_the_action_endpoint` | Asserts the 2722 action URL, with two more copies in the same file | 1 | The new port. Grep the repo rather than fixing the three copies one at a time. |
| `notifier/tests/test_ack_handler.py:293` | `ContractTests` | Reads `setup/SideCrab.Common.ps1` as text to pin the scheme registration | 5 | Repointed at whatever the macOS installer becomes, or deleted with it - otherwise it fails on a missing file rather than on a contract break. |
| `notifier/tests/test_snooze.py:129` | `test_the_handler_never_posts_to_crabd` | AST-walks the handler to prove it imports no HTTP client | 7 | Kept verbatim; the docstring-by-identity exclusion is a subtle correct detail. |
| `notifier/tests/test_approval.py:155` | `test_it_carries_NO_action_buttons` | Asserted through the PowerShell adapter's XML, but the rule is not Windows | 7 | Re-expressed on the macOS payload. This is the never-auto-allow invariant at the notifier layer and must survive. |
| `notifier/tests/test_stale.py:98` | `test_the_detail_names_no_host_or_port` | Asserts `"2722"` is absent from the outage text | 1 | The literal moves with the endpoint constants, or the guard silently stops guarding. |
| `notifier/tests/test_emit_matrix.py:69` | `RaisingAdapter`, `NoneReturningAdapter` (92), `SpyOwner` (103) | The failure taxonomy is derived from the PowerShell adapter's internals | 7 | The doubles are kept verbatim; only the docstring's list of what the real adapter catches changes. |
| `notifier/tests/test_version.py:4` | module docstring | Frames stale code in Scheduled Task terms | 5 | Rewritten for launchd. The stale-code problem recurs there, so `RuntimeStamp` and its tests stay. |
| `hooks/tests/test_statusline.py:66` | `test_builds_from_model_and_cwd` | Feeds a `C:\Dev\sidecrab` cwd and asserts loosely | 0 | Gains a POSIX case and a tighter assertion; as written it is near-vacuous for a cross-platform parser. |
| `hooks/tests/test_statusline.py:109` | `test_passes_stdin_and_returns_stdout` | Builds a double-quoted shell command string | 0 | A list-form argv or a shell-quoted string once `run_chained`'s POSIX shell semantics are settled. |
| `widget/tests/test_ordering.js:31` | `loadWidget()` | Loads the shipping script into a `vm` context with a `readyState: 'loading'` document stub | 6 | Survives the port unchanged and gets better: a real browser target also allows a jsdom or headless load. |
| `widget/tests/test_ordering.js:45` | `sandbox.location` | `search: ''` keeps the mock harness out of the run | 6 | Safe as is; a second load with `search='?mock=normal'` becomes worth adding once the panel is served. |
| `widget/tests/test_ordering.js:98` | `bareSliceClamp` (the deliberate mutant) | The pre-v0.26.0 clamp, asserted at 133 to fail the invariant | 6 | Kept exactly. It is the only thing proving the invariant test can fail. |
| `widget/tests/test_ordering.js:107` | the capacity sweep | `[4, 6, 8, 9, 12]` is every capacity the iCUE slot stylesheet can produce | 6 | Widened once the CSS is no longer bounded by a closed set of slots. |
| `widget/tests/test_ordering.js:189` | the `fmtNum` boundary block | A five-character width budget measured on the iCUE panel | 6 | The em-dash-never-zero rule is kept as is; the width budget is re-measured at the new panel size and recorded in `DEV.md`. |
| `setup/tests/RunTests.ps1:1` | the whole file | PowerShell 7, Pester 5 or a hand-rolled shim | 5 | Stays as the Windows suite. The ideas worth carrying are the shim's throw-on-unsupported-operator rule and its deferred `AfterAll` queue. |
| `setup/tests/SideCrab.Setup.Tests.ps1:15` | `Describe 'SideCrab setup'` | 1 Describe, 26 Context, 298 It; nothing installs anything, scripts are parsed rather than run | 5 | Stays as the Windows suite. Its It names are the specification a Python replacement mirrors, and the import-plus-fakes pattern replaces the AST lift for free. |
| `setup/tests/SideCrab.Setup.Tests.ps1:261` | `$script:Marker` | `127.0.0.1:2722/v1/hook` as the ownership marker, repeated in two more contexts | 1 | The new marker. An installed base matched on the old one will not be recognised for merge or removal, so the migration must match both. |
| `setup/tests/SideCrab.Setup.Tests.ps1:1317` | the DPAPI limits-token test | Round-trips through `ProtectedData` in CurrentUser scope | 4 | A Keychain equivalent keeping the two assertions that matter: the plaintext is never on disk, and presence is reportable without revealing the value. |
| `lighting/tests/test_control_recovery.py:18` | the three lighting suites | Corsair SDK end to end, with `cuesdk` faked into `sys.modules` | 5 | Left parked with the component. Nothing in the panel, crabd, notifier or hooks depends on them. |

### Traps a porter must not break

- **`companion/crabd.py:68` and `:1077` (call site `:5641`) - `_cwd_title` uses `PureWindowsPath` on
  purpose.** It parses `/` and `\` alike, so one implementation reads a Windows cwd, a POSIX cwd and
  a UNC path the same way on any host. Swapping it for `Path` or `PurePosixPath` during a
  de-Windows-ification sweep is the exact regression the docstring was written to prevent: os-native
  `Path` would keep `C:\Dev\acme` whole on POSIX and title every imported Windows session with the
  raw string. `companion/tests/test_crabd.py:307` and `:311` fail first.
- **`companion/crabd.py:5655` - `model` is served verbatim, `[1m]` / `[200k]` marker included.** The
  marker is rank two of the context-window precedence, parsed by `_marker_window` and by the panel's
  `MODEL_CTX_RE` (`widget/scripts/sidecrab.js:341`); `_model_base_id` strips it as a lookup key only.
  Normalising, aliasing or prettifying the served string breaks the context gauge with no visible
  error anywhere.
- **`companion/crabd.py:6015` - `/v1/state` never 500s.** A data shape crabd cannot read is skipped
  and logged once; 503 only before the first snapshot. A builder that cannot build serves the last
  good snapshot with its original `generatedAt`. Serving `sessions: []` here would say the operator
  has nothing running, and inventing that answer is worse than staleness.
- **`companion/crabd.py:4322` - when neither counter reads, there is no `host` key at all.** Not four
  nulls: the panel feature-detects presence and renders nothing, where a row of em-dashes reads as a
  broken sensor. The first CPU sample is always null rather than 0.0, a sub-quantum window is null,
  a negative busy fraction is null and re-baselines, and there is deliberately no last-good cache.
  `companion/tests/test_crabd.py:9644` pins the shape.
- **`companion/crabd.py:4652` and `:6522` - never auto-allow.** There is no path from a timeout, a
  saturated broker, a disabled config or an error to `behavior: allow`; every early exit on the
  permission path returns `{}`, which is the terminal dialog. The pass-through shape is
  `PermissionRequest`'s `decision: {behavior}`, not `PreToolUse`'s `permissionDecision`, and there is
  no `ask` value. A browser-served panel raises the stakes here, not lowers them.
- **`widget/index.html:18` and `widget/scripts/sidecrab.js:383` - never declare a function or a
  top-level `var`/`let`/`const` with a widget property's name.** Widget 0.27.0 shipped blank because
  `panelToken` was both an injected global and a declaration, which is a whole-script SyntaxError.
  The reader is named `pairingCode()` for that reason. The same rule protects
  `icueEvents` (`:848`) and `pluginSensorsdataproviderEvents` (`:6291`), which are bare assignments
  on purpose: a declaration hides them from the bridge, and strict mode or a module wrapper turns
  them into a ReferenceError.
- **`widget/index.html:1`, `:164` and `:211` - the file is parsed as strict XML.** Uppercase
  `<!DOCTYPE html>`, no bare `&` (which is why the inlined sensor wrapper sits in a CDATA block - its
  logical AND is a raw ampersand), and no `--` inside a comment. The CI parse
  (`python -c "import xml.etree.ElementTree as ET; ET.fromstring(...)"`) is the only check that has
  ever caught a blank-panel ship; relax it only in the same change that retires the iCUE target.
- **`widget/scripts/sidecrab.js:1056` - `acceptDoc`'s order is load-bearing.** A `crabd.version`
  change clears every capability latch (`cfgEndpointUnsupported`, `cfgApprovalUnsupported`,
  `quietOverrideUnsupported`, `diagUnsupported`, `cfgSent`) before anything else, and trick detection
  runs before `render()`. `crabd.version` is never compared or ordered, only tested for having
  changed; turning it into a version gate is the mistake.
- **`widget/scripts/sidecrab.js:20` and `:21` - `POLL_TIMEOUT_MS` (2500) must stay under `POLL_MS`
  (3000).** The abort exists because a refused loopback connect can take seconds to reject, and a
  never-settling fetch leaves `inFlight` stuck forever. Re-measure the timeout on macOS loopback, but
  never above the interval.
- **`widget/scripts/sidecrab.js:456` and `widget/mock/` - the mock fixtures have documented
  purposes.** `mock-state-dense.json` deliberately carries no `contextWindowTokens`; do not add it.
  `mock-state-rework.json` is what the screenshot probe matrix is baselined on and must stay
  byte-identical. `mock-history-today.json` has no date in its filename because a dated fixture
  stopped being today at midnight, and `&hist=error` names a file that is deliberately absent so the
  404 comes from the static server. If crabd serves the panel it must serve `/mock/*` too, or the
  whole harness dies with the port.
- **`companion/crabd.py:1486` and `:1520` - `config.json` is written through one locked
  read-modify-write.** `PRESERVED_SUBKEYS` lives in `UserConfig` because only that method holds the
  lock: a handler that read the old value first would race a hand edit landing between the read and
  the write, which is the defect this fix closed. The write itself is temp-sibling plus `os.replace`
  because a truncate-then-fail once left the file empty and reverted the operator to defaults.
  `setup/SideCrab.Common.ps1:582` honours the same whole-file contract from the other side.
- **`companion/crabd.py:6241` - `Origin: null` must stay accepted.** The docstring says it outright:
  do not blindly tighten the gate to reject null, because the webview legitimately sends it. The
  inverse applies to the port - do not blindly loosen it to accept http(s) either. The only sanctioned
  move is an allowlist of the panel's exact origin, computed from the bound host and port. Absent
  Origin stays accepted too: the hooks, the status line and the notifier all send none. `ACAO: *` is
  illegal on every path, at every status, and `_acao` defaults to `None` (`:5910`) so a forgetful
  code path fails closed.
- **`companion/crabd.py:568` and `hooks/settings-hooks-fragment.json:63` - 55 s under 60 s.** crabd
  parks the permission hook for `PERMISSION_POLL_SEC` = 55, deliberately inside the hook's 60 s
  timeout, and `Handler.timeout` deliberately does not bound the long poll. Never lower 60, never
  raise 55 past it, and never put a proxy in front of `/v1/hook/permission`: it would cut the hold at
  its own idle timeout and turn every approval into a silent pass-through. At most
  `PERMISSION_MAX_PENDING` = 8 holds exist; a ninth is passed through, never queued and never
  allowed.
- **`notifier/sidecrab_toast.py:199` - `SUPPORTED_SCHEMAS` going stale is silent and total.**
  Measured 2026-08-26: crabd had moved to schema 4 while this set said `{1,2,3}`; the notifier polled
  happily, logged one startup warning and never toasted again, with a running task the whole time.
  Any schema change lands here in the same commit, and the divergence check that guarded it needs a
  macOS replacement or the failure mode returns.
- **`companion/crabd.py:1596` - the history file holds no free-form text.** `kind`, `sessionId`,
  `title` and `ts`, and nothing else: no question text, no prompts, no tool output. That is why
  continue prompts are whitelisted fixed strings and why the permission summary is served on
  `/v1/state` but never persisted. It is also why `replay` does not restore `needs_input`: there
  would be nothing to say.
- **`companion/crabd.py:4137`, `:4151`, `:4238` and `notifier/sidecrab_toast.py:1941` -
  `getattr(subprocess, "CREATE_NO_WINDOW", 0)` is already the portable spelling.** The `getattr`
  default of 0 no-ops off Windows and is what lets one source run on both hosts; it is also what lets
  the notifier module import at all on macOS. Do not "clean it up" - and if a new platform call is
  ever added, copy the same shape (probe the capability, degrade, never branch on the platform
  string). `sys.platform` appears nowhere in `crabd.py`, which is the posture worth preserving.
- **`companion/crabd.py:214` - absent is not the same claim as unknown.** A task that exists and
  cannot be read is not an absent one. The launchd probe must preserve the three-way distinction;
  collapsing unreadable into `stopped` puts two green dots on a panel that has no idea.
- **`companion/crabd.py:6852` - `panelApprovals` must never join `CONFIG_WRITABLE`.** It decides
  whether an on-glass tap can allow a real tool call. `quietOverride` is excluded for a different
  reason: `/v1/action`'s quiet branch is its only writer, which is what bounds the values that can
  reach the file. A validator for `panelApprovals` was deleted on purpose (SEC-c) and must not be
  re-added - an unused validator is a latent invitation to wire the flag in.
- **`companion/crabd.py:87` and `:6776` - the pairing code's threat model inverts, it does not
  disappear.** The code is safe today because an iCUE property is unreachable from a web page. On the
  port the panel is a web page, so the secret has to live somewhere the same-origin policy protects.
  Same-origin does not make it redundant: any local process can still POST to loopback. The check
  order in `_do_decide` - shape, then token, then `requestId`, then apply - is the security argument
  and must never fall open.
- **`widget/scripts/sidecrab.js:7892` and `:8210` - the dev flags are gated only on `?mock=`.** The
  stated ground is that the iCUE origin carries no query string. That is false the moment crabd
  serves the panel at an addressable URL: `&ackflash=1` performs a real live ack-all POST,
  `&sensorstale=` rewrites a module tunable, and `&quietov=` and `&budget=` rewrite the served
  document. Re-gate the whole block on an explicit build switch before the panel is served over http.
  This is the one genuine security regression the port introduces by itself.
- **`widget/scripts/sidecrab.js:2778` - `clampGrid` guarantees a waiting card is never the row the
  overflow tile swallows.** The panel used to hold that only by inheriting crabd's pre-sort; feed it
  an unsorted document and the waiting card lands in the tail. `widget/tests/test_ordering.js:98`
  keeps the old clamp as a deliberate mutant and asserts at line 133 that it fails the invariant -
  delete the mutant and the whole invariant suite reports success forever.
- **`widget/scripts/sidecrab.js:1998` and `:2007` - one-shot flags clear on a timer, never on
  `animationend`.** Under `prefers-reduced-motion` the animation is `none` and `animationend` never
  fires, so an animationend-only reset latches the flag true forever and silently kills every later
  alert. The same discipline covers the four crab tricks.
- **`widget/scripts/sidecrab.js:1058` - nothing may gate behaviour on a schema number except the one
  acceptance check.** Every additive field is found by field presence, and presence tests type, not
  truthiness: `typeof x === 'number'`, never `Number(x)`, because `Number(null)` is 0 and a
  contract-legal null must render as an em-dash rather than a zero. Adding a second comparison
  against `doc.schema` undoes the v0.6.1 rework.
- **`notifier/sidecrab_toast.py:602`-`620` - the approval toast carries no Approve or Deny buttons.**
  Toast actions are cheap to hit: from a lock screen, from a notification the shell replays hours
  later, by anyone standing at the machine. macOS notification actions are just as cheap, so the
  platform offering them is not a reason to add them. `notifier/tests/test_approval.py:155` pins it.
- **`notifier/sidecrab_toast.py` deciders - quiet hours suppress and mark, snooze defers.** Without
  the mark, every spell that matured overnight fires the instant quiet ends: a burst of stale alerts
  on a perfectly healthy morning. That asymmetry is the feature, repeated at all six deciders, and it
  is "every alert must survive a healthy night" made concrete. `StaleFeedDecider` runs first in
  `poll_once`, ahead of every early return, because it is the one consumer with something to say when
  the fetch failed.
- **`notifier/sidecrab_snooze_handler.pyw:1`-`16` - the snooze handler never touches crabd.**
  Acking there would clear the panel's dot and stop showing a session that is still, truthfully,
  waiting. Snoozing a notification must never look like answering a question, and
  `notifier/tests/test_snooze.py:129` proves it off the AST rather than the text.
- **`notifier/sidecrab_toast.py:1666`, `sidecrab_ack_handler.pyw:58` and
  `sidecrab_snooze_handler.pyw:69` - the session-id charset is duplicated on purpose.** Neither
  handler imports `sidecrab_toast`, because they run from a shell association where the fewer things
  that can be missing the better; tests pin the copies to each other. Never widen the charset, and
  never consolidate by adding an import. The scheme regexes carry `re.ASCII` alongside `IGNORECASE`
  because without it U+212A KELVIN SIGN lowercases to `k` and a homoglyph scheme is accepted as ours.
- **`companion/tests/_httpkeepalive.py:289` - `settle()` is a barrier, not a retry.** `/v1/hook`,
  `/v1/statusline`, `/v1/metrics` and `/v1/logs` answer before they parse, so the 204 landing does not
  mean the document is ingested; asserting the side effect on the next line is asserting a race. Call
  `settle()` or `quiesce()` after any fire-and-forget POST. `start_test_server` (`:237`) must never
  bind the production port (`:254`).
- **`companion/tests/test_crabd.py:9491` - 62.5 and 100.0 are named in the assertions on purpose.**
  Windows kernel time contains idle time, so the two naive formulas give 62.5% and 100% where the
  truth is 40%, and all three are percentages an operator would believe. On macOS the counters are
  disjoint and 62.5 becomes the correct answer for the same three inputs, so porting the numbers
  unchanged would encode the Windows subtraction into a platform that does not need it. Port the
  method, not the fixture.
- **`companion/tests/test_crabd.py:2042` and `:2101` - the contract key lists are `assertEqual` on a
  sorted list, not `assertIn`.** A reintroduced or accidentally added top-level key has to fail here
  rather than ship. Every new field the panel needs lands in `docs/STATE-CONTRACT.md`, then crabd,
  then these lists.
- **`setup/SideCrab.Common.ps1:288` - a task the operator disabled stays disabled.** Re-registering
  always writes an enabled task, so a plain re-run once resurrected the parked glow component and
  started it into its crash. `-ForceEnable` is the only override and the left-disabled line is loud on
  purpose. Launchd bootstrap has the same trap.
- **`setup/SideCrab.Common.ps1:859` and `:914` - on port-release timeout the restart does not
  start.** Starting blind is what produced a dark panel; it throws naming the PID, because a foreign
  process holding the port is a different problem from a slow shutdown. Wait budgets are counted in
  polls, not wall clock, because the sleep is injectable.
- **`setup/SideCrab.Common.ps1:402` - the prior status line is saved only when it is not already
  ours.** Re-saving our own command captures the chain script as its own prior and builds an endless
  loop. The saved file's presence marks that SideCrab took the slot; an explicit null means nothing
  was there, and the two must stay distinguishable.
- **`setup/SideCrab.Common.ps1:1472` - hook ownership is entry-level, never matcher-level.** Dropping
  a matcher whole ate a hand-merged foreign hook on every re-install. The unshared path returns the
  original object so a canonical comparison stays byte-identical, and only a genuinely shared matcher
  is rebuilt.
- **`setup/SideCrab.Common.ps1:1658` - an uninstall removes wiring and keeps data, and backups are
  kept at every switch including `-Purge`.** The moment you most need last week's `settings.json` is
  the moment after an uninstall went wrong. The state directory is removed only when the purge
  emptied it.
- **`setup/SideCrab.Common.ps1:1987` - `allowedHttpHookUrls` unset is not the same as empty.** A null
  pattern list means the key is unset and everything is allowed; an empty list means the operator set
  the key and admitted nothing, which blocks both http hooks outright and makes approvals silently do
  nothing.

### User-facing strings naming Windows, iCUE or a PowerShell script

| File:line | Current text |
|---|---|
| `companion/crabd.py:2423` | `ACK_EVENT = "acknowledged from Edge"` (served in `sessions[].events` and persisted to `history.jsonl`) |
| `companion/crabd.py:3120` | "no Claude access token - run claude in a terminal, or store a long-lived one: Install-SideCrab.ps1 -LimitsToken" |
| `companion/crabd.py:3124` | "Claude token expired - run claude in a terminal to refresh it, or store a long-lived one: Install-SideCrab.ps1 -LimitsToken" |
| `companion/crabd.py:3145` | "SideCrab limits token rejected - mint a new one with claude setup-token and re-run Install-SideCrab.ps1 -LimitsToken" |
| `companion/crabd.py:4234` | argv literal `["schtasks", "/query", "/tn", task, "/fo", "csv", "/nh"]` - reaches the process table and any error an operator sees |
| `companion/crabd.py:4439` | "crabd: GetSystemTimes unavailable ({type}); serving no host CPU" |
| `companion/crabd.py:4444` | "crabd: GetSystemTimes returned failure; serving no host CPU" |
| `companion/crabd.py:4457` | "crabd: GlobalMemoryStatusEx unavailable ({type}); serving no host memory" |
| `companion/crabd.py:4462` | "crabd: GlobalMemoryStatusEx returned failure; serving no host memory" |
| `docs/GETTING-STARTED.md:3` | "from nothing installed to a crab on your Xeneon Edge that knows what your Claude Code sessions are doing" |
| `docs/GETTING-STARTED.md:16` | "**The widget.** A file you import into Corsair iCUE." |
| `docs/GETTING-STARTED.md:18` | "**The companion (`crabd`).** A small background service on the same PC." |
| `docs/GETTING-STARTED.md:28` | "Open **PowerShell 7** (the app is called \"PowerShell 7\", not \"Windows PowerShell\") and run each line." |
| `docs/GETTING-STARTED.md:33` | table row: "Windows \| `[Environment]::OSVersion.Version` \| Major version 10 (Windows 10 or 11)" |
| `docs/GETTING-STARTED.md:34` | table row: "iCUE \| Open iCUE, Settings, About \| 5.44 or newer, and a Xeneon Edge listed as a device" |
| `docs/GETTING-STARTED.md:36` | table row: "PowerShell 7 \| `$PSVersionTable.PSVersion` \| 7.x" |
| `docs/GETTING-STARTED.md:37` | table row: "Python \| ... If a Microsoft Store window opens instead, you have the Store alias ... tick \"Add python.exe to PATH\"" |
| `docs/GETTING-STARTED.md:40` | "Not on Windows, or no iCUE? SideCrab cannot run. The widget is an iCUE widget and the companion is a Windows service; there is no other build." |
| `docs/GETTING-STARTED.md:48` | "download the newest `SideCrab-<version>.icuewidget`" |
| `docs/GETTING-STARTED.md:50` | "Double-click the file. iCUE 5.46.67 or newer imports it directly. On an older iCUE, open the Xeneon Edge's dashboard editor" |
| `docs/GETTING-STARTED.md:52` | "In iCUE, put the widget on the Xeneon Edge and make it **full-screen**. It is designed for the whole 2560 x 720 display." |
| `docs/GETTING-STARTED.md:61` | "Re-import the newest release; if it stays blank, open an issue with your iCUE version" |
| `docs/GETTING-STARTED.md:71` | "git clone https://github.com/Dixie-sketch/Clawdeck.git C:\Dev\sidecrab" |
| `docs/GETTING-STARTED.md:73` | "pwsh -File .\setup\Install-SideCrab.ps1 -WithToast" |
| `docs/GETTING-STARTED.md:76` | "`-WithToast` also installs the notifier, which raises a Windows notification when a session has been waiting on you" |
| `docs/GETTING-STARTED.md:81` | "registers a Scheduled Task that starts `crabd` at logon, and starts it now," |
| `docs/GETTING-STARTED.md:84` | "registers the notifier's identity, under your user account only. No admin prompt," |
| `docs/GETTING-STARTED.md:90` | "pwsh -File .\setup\Install-SideCrab.ps1 -Status" |
| `docs/GETTING-STARTED.md:91` | "pwsh -File .\setup\Test-SideCrab.ps1" |
| `docs/GETTING-STARTED.md:108` | "Run `Test-SideCrab.ps1` and look at the hook rows." |
| `docs/GETTING-STARTED.md:121` | "pwsh -File .\setup\Install-SideCrab.ps1 -LimitsToken" |
| `docs/GETTING-STARTED.md:125` | "It is stored encrypted for your Windows account, and used only when the short-lived one has expired." |
| `docs/GETTING-STARTED.md:140` | "\"recapRepos\": [\"C:\\\\Dev\\\\my-project\"] // repos whose commits count in the recap" |
| `docs/GETTING-STARTED.md:159` | "pwsh -File .\setup\Install-SideCrab.ps1 -PairingCode" |
| `docs/GETTING-STARTED.md:162` | "In iCUE, open the widget's settings and paste the code into **Approval Pairing Code**." |
| `docs/GETTING-STARTED.md:166` | "pwsh -File .\setup\Install-SideCrab.ps1 -WithApprovals" |
| `docs/GETTING-STARTED.md:172` | "`setup\Verify-PanelApproval.ps1` walks through this with the exact commands." |
| `docs/GETTING-STARTED.md:204` | "git -C C:\Dev\sidecrab pull" |
| `docs/GETTING-STARTED.md:205` | "pwsh -File C:\Dev\sidecrab\setup\Update-SideCrab.ps1" |
| `docs/GETTING-STARTED.md:208` | "The widget updates separately: download the new `.icuewidget` from the releases page and import it again." |
| `docs/GETTING-STARTED.md:216` | "pwsh -File C:\Dev\sidecrab\setup\Uninstall-SideCrab.ps1" |
| `docs/GETTING-STARTED.md:219` | "Remove the widget from the Edge in iCUE as you would any other widget." |
| `docs/GETTING-STARTED.md:227` | "Blank panel, no crab \| Re-import the newest `.icuewidget`; then open an issue with your iCUE version" |
| `docs/GETTING-STARTED.md:228` | "Worried grey crab ... `Install-SideCrab.ps1 -Status`, then `Update-SideCrab.ps1` to restart the task" |
| `docs/GETTING-STARTED.md:229` | "No session cards \| `Test-SideCrab.ps1`; check `~/.claude/settings.json` still has the SideCrab hooks" |
| `docs/GETTING-STARTED.md:230` | "Gauges show a dash and \"token expired\" \| ... `claude setup-token`, then `Install-SideCrab.ps1 -LimitsToken`" |
| `docs/GETTING-STARTED.md:231` | "Temperatures frozen or wrong \| Pick the right sensor in the widget's settings; the row names the one it reads" |
| `docs/GETTING-STARTED.md:232` | "\"not paired\" or \"pairing code wrong\" on Approve \| Re-paste the code from `-PairingCode` into the widget's settings" |
| `docs/GETTING-STARTED.md:233` | "Anything else \| `pwsh -File .\setup\Test-SideCrab.ps1` prints a PASS/FAIL row for every piece" |
| `hooks/README.md:15` | "`curl.exe` is the Windows-native one in `C:\Windows\System32` - not Git Bash's." |
| `hooks/README.md:18` | "`exit 0` behaves the same under `cmd.exe` and any POSIX shell." |
| `hooks/README.md:59` | "## Verified against the shipped Claude Code (claude.exe v2.1.246, 2026-08-26)" |
| `hooks/README.md:61` | "Confirmed by inspecting the shipped binary (a Bun-compiled `claude.exe`)" |
| `hooks/sidecrab_statusline.py:5` | "Wired by ``setup\\Install-SideCrab.ps1`` into ``~/.claude/settings.json``" |
| `hooks/sidecrab_statusline.py:8` | "\"command\": \"\\\"<python.exe>\\\" \\\"<repo>\\hooks\\sidecrab_statusline.py\\\"\"" |
| `hooks/sidecrab_statusline.py:22` | "VERIFIED AGAINST THE SHIPPED CLAUDE CODE (claude.exe v2.1.246, 2026-08-26):" |
| `hooks/sidecrab_statusline.py:156` | "so a ``.ps1``/node/other command that worked before still works when chained through here." |
| `notifier/README.md:3` | "A native Windows toast when the Edge is out of view and a Claude session has been waiting too long." |
| `notifier/README.md:7` | "Zero pip dependencies. Python 3.13 stdlib + Windows PowerShell 5.1." |
| `notifier/README.md:418` | "`Register-SideCrabProtocol.ps1` registers this scheme alongside `sidecrab-ack:`" |
| `notifier/README.md:436` | "`Test-SideCrab.ps1` and `Repair-SideCrab.ps1` carry one row per scheme." |
| `notifier/README.md:474` | "`Test-SideCrab.ps1` / `Repair-SideCrab` can now compare `__version__` on disk against ..." |
| `notifier/README.md:531` | "\| `notifier` \| the notifier, at startup and every 15 min \| `Test-SideCrab` / a human \|" |
| `notifier/README.md:550` | "**Chosen - Route A: subprocess to Windows PowerShell 5.1, WinRT projection.**" |
| `notifier/README.md:563` | "**pwsh 7 cannot do this.** ... `POWERSHELL_EXE` is pinned to System32; do not \"modernize\" it to pwsh." |
| `notifier/README.md:608` | "\| **Fallback** \| Windows PowerShell's, `{1AC14E77-...}\WindowsPowerShell\v1.0\powershell.exe` \| it does not \|" |
| `notifier/README.md:610` | "`setup\Register-SideCrabAumid.ps1` creates that key (`DisplayName` = SideCrab, `IconUri` = `notifier\sidecrab.ico`)" |
| `notifier/README.md:615` | "Without it they are attributed to \"Windows PowerShell\", which ..." |
| `notifier/README.md:676` | "\| Registers `sidecrab-ack:` (and `sidecrab-snooze:`) \| `setup\Register-SideCrabProtocol.ps1` (HKCU, idempotent ...) \|" |
| `notifier/README.md:843` | "## Desired Scheduled Task shape - registered by the installer" |
| `notifier/README.md:883` | "`setup\Register-SideCrabAumid.ps1` and verified in `wpndatabase.db` (see *App identity*)." |
| `notifier/README.md:885` | "muting Windows PowerShell there would still mute SideCrab. `setup\Test-SideCrab.ps1` fails the \"toast identity\" row" |
| `notifier/README.md:890` | "**Subprocess cost.** Each toast spawns a PowerShell process (~0.5-1 s)." |
| `notifier/README.md:907` | "`setup\Register-SideCrabProtocol.ps1` (or a re-run of the installer) registers it; `Test-SideCrab.ps1` and `Repair-SideCrab.ps1` now have a row per scheme" |
| `notifier/sidecrab_ack_handler.pyw:5` | "pythonw sidecrab_ack_handler.pyw \"sidecrab-ack:<sessionId>\"" |
| `notifier/sidecrab_ack_handler.pyw:7` | "registered by ``setup\\Register-SideCrabProtocol.ps1`` at ``HKCU\\SOFTWARE\\Classes\\sidecrab-ack\\shell\\open\\command``" |
| `notifier/sidecrab_snooze_handler.pyw:5` | "pythonw sidecrab_snooze_handler.pyw \"sidecrab-snooze:<sessionId>\"" |
| `notifier/sidecrab_snooze_handler.pyw:7` | "registered by ``setup\\Register-SideCrabProtocol.ps1`` at ``HKCU\\SOFTWARE\\Classes\\sidecrab-snooze\\shell\\open\\command``" |
| `notifier/sidecrab_toast.py:1` | "SideCrab notifier - a native Windows toast when a Claude session has been waiting too long." |
| `notifier/sidecrab_toast.py:1841` | "toast identity: %s (SideCrab AUMID registered)" |
| `notifier/sidecrab_toast.py:1849` | "toast identity: %s (borrowed - run setup\\Register-SideCrabAumid.ps1; HKCU\\%s %s)" |
| `notifier/sidecrab_toast.py:2034` | "cannot record the running version in %s - Test-SideCrab cannot tell running-from-disk for this process" |
| `setup/Install-SideCrab.ps1:52` | ".EXAMPLE pwsh -File .\setup\Install-SideCrab.ps1 (repeated at 54, 56, 58, 60, 62)" |
| `setup/Install-SideCrab.ps1:214` | "aumid: $($aumid.Aumid) not registered - toasts group under 'Windows PowerShell'" |
| `setup/Install-SideCrab.ps1:226` | "proto: $($proto.Scheme): not registered - the toast $($proto.Button) button will not resolve" |
| `setup/Install-SideCrab.ps1:268` | "pairing: code present ($TokenPath) - print it with -PairingCode, enter it in iCUE > widget settings > Approval Pairing Code" |
| `setup/Install-SideCrab.ps1:282` | "widget: manifest $widget (installed into iCUE by import, not by this script)" |
| `setup/Install-SideCrab.ps1:294` | "1. In a terminal run: claude setup-token (it opens a browser sign-in and prints a token)" |
| `setup/Install-SideCrab.ps1:295` | "2. Paste that token below. It is stored DPAPI-protected for your Windows account only," |
| `setup/Install-SideCrab.ps1:299` | "Stored (N bytes, encrypted). crabd reads it on its next limits poll (within 10 minutes) - or restart it now with Update-SideCrab.ps1." |
| `setup/Install-SideCrab.ps1:307` | "Enter it in iCUE > the SideCrab widget's settings > Approval Pairing Code. Approve/Deny taps are refused until it matches." |
| `setup/Install-SideCrab.ps1:466` | "SECURITY: panel approvals are ON. Approve/Deny taps on the on-glass widget can now allow or reject ..." |
| `setup/Install-SideCrab.ps1:469` | "PAIRING: taps are only honoured with the pairing code. Enter $($tok.Code) in iCUE > widget settings > Approval Pairing Code" |
| `setup/Install-SideCrab.ps1:471` | "PAIRING: no code yet at $TokenPath - crabd 0.29.0+ mints it on first start. Re-run with -PairingCode ..." |
| `setup/Install-SideCrab.ps1:486` | "Done. Check http://127.0.0.1:2722/v1/health" |
| `setup/SideCrab.Common.ps1:44` | "SideCrab companion (crabd) - serves /v1/state to the Xeneon Edge widget." |
| `setup/SideCrab.Common.ps1:67` | "SideCrab glow - drives iCUE lighting from crabd state." |
| `setup/SideCrab.Common.ps1:84` | "SideCrab toast - raises Windows notifications from crabd state." |
| `setup/SideCrab.Common.ps1:399` | "No usable console python.exe found on PATH (the WindowsApps alias stub does not count). Install Python 3.13." |
| `setup/SideCrab.Common.ps1:665` | "No usable python.exe found on PATH (the WindowsApps alias stub does not count). Install Python 3.13." |
| `setup/SideCrab.Common.ps1:914` | "$TaskName was NOT restarted: ... stop it (Stop-Process -Id <pid>) and re-run." |
| `setup/SideCrab.Common.ps1:993` | "icon not found at $($spec.IconUri) - registering DisplayName only (run: python notifier\make_icon.py)" |
| `setup/SideCrab.Common.ps1:1137` | "no toast handler found under $RepoRoot\notifier - cannot register ..." |
| `setup/Test-SideCrab.ps1:24` | ".EXAMPLE pwsh -File .\setup\Test-SideCrab.ps1 (and 26: -SkipHookCycle # strictly read-only)" |
| `setup/Test-SideCrab.ps1:314` | "$($aumid.Aumid) not registered - run setup\Register-SideCrabAumid.ps1" |
| `setup/Test-SideCrab.ps1:331` | "$($proto.Scheme): not registered - the $($proto.Button) button no-ops. Run setup\Register-SideCrabProtocol.ps1" |
| `setup/Test-SideCrab.ps1:352` | "$why - run Install-SideCrab.ps1" |
| `setup/Test-SideCrab.ps1:431` | "never verified on a live prompt - run setup\Verify-PanelApproval.ps1 -DryRun before enabling" |
| `setup/Uninstall-SideCrab.ps1:25` | "registry residue: the app identity (AppUserModelId\SideCrab.Notifier) and every toast button protocol" |
| `setup/Uninstall-SideCrab.ps1:52` | "HKCU AppUserModelId\SideCrab.Notifier   the toast identity" |
| `setup/Uninstall-SideCrab.ps1:73` | ".EXAMPLE pwsh -File .\setup\Uninstall-SideCrab.ps1 (and 75 -Purge, 77 -TaskName SideCrab-glow)" |
| `setup/Uninstall-SideCrab.ps1:422` | "remove the data/cache/log files with: pwsh -File setup\Uninstall-SideCrab.ps1 -Purge" |
| `setup/Uninstall-SideCrab.ps1:423` | "prune the backups with: pwsh -File setup\Restore-SideCrab.ps1 -PruneOlderThan 30" |
| `setup/Update-SideCrab.ps1:24` | "THE WIDGET IS NOT UPDATED BY THIS SCRIPT. The Xeneon Edge widget is installed into iCUE by importing the .icuewidget package" |
| `setup/Update-SideCrab.ps1:145` | "tasks: none registered - run Install-SideCrab.ps1 first" |
| `setup/Update-SideCrab.ps1:151` | "tasks: '$($s.TaskName)' disabled - left alone (Enable-ScheduledTask to un-park)" |
| `setup/Update-SideCrab.ps1:205` | "Get-NetTCPConnection -LocalPort $crabdPort -State Listen -> Stop-Process -Id <pid>" |
| `setup/Update-SideCrab.ps1:208` | "crabd did not come up within $TimeoutSec s. Diagnose with: pwsh -File setup\Repair-SideCrab.ps1" |
| `setup/Update-SideCrab.ps1:230` | "aumid: ... registered but stale - re-run Install-SideCrab.ps1 (or Register-SideCrabAumid.ps1)" |
| `setup/Update-SideCrab.ps1:234` | "aumid: ... NOT REGISTERED - toasts will be filed under 'Windows PowerShell'; re-run Install-SideCrab.ps1" |
| `setup/Update-SideCrab.ps1:242` | "proto: ... registered but stale - re-run Install-SideCrab.ps1 (or Register-SideCrabProtocol.ps1)" |
| `setup/Update-SideCrab.ps1:248` | "proto: ... NOT REGISTERED - the $($proto.Button) button will do nothing; re-run Install-SideCrab.ps1" |
| `setup/Update-SideCrab.ps1:253` | "The WIDGET is not updated by this script. The Xeneon Edge widget updates only by importing the .icuewidget package into iCUE" |
| `setup/Update-SideCrab.ps1:254` | "Verify with: pwsh -File setup\Test-SideCrab.ps1" |
| `widget/index.html:6` | `<title>tr('SideCrab')</title>` - unsubstituted outside iCUE, so a browser tab shows the macro text |
| `widget/index.html:11` | `data-label="tr('crabd Port')" data-default="'2722'"` |
| `widget/index.html:15` | HTML comment: "Read it with `Install-SideCrab.ps1 -PairingCode`." |
| `widget/index.html:49` | `data-label="tr('Desktop Toast Alerts')"` |
| `widget/index.html:50` | `data-label="tr('Toast After')"` |
| `widget/index.html:67` | `data-label="tr('Approval Toast After')"` |
| `widget/index.html:79` | `data-label="tr('CPU Temperature Sensor')"` (and 80, GPU) |
| `widget/index.html:108` | group info: "Claude Code session data comes from the SideCrab companion service on 127.0.0.1 ... Desktop Toast Alerts is a master switch ..." |
| `widget/index.html:111` | `"title": "tr('Hardware Sensors')"` |
| `widget/index.html:113` | group info: "The sensor row is hidden entirely when iCUE reports no sensors ... importing the widget resets both of these" |
| `widget/index.html:118` | group info: "Turn this on only while investigating touch problems ... sends the log to the SideCrab companion" |
| `widget/index.html:121` | `"title": "tr('Widget Personalization')"` - "Widget" is iCUE's noun for this artifact |
| `widget/manifest.json:5` | "An ambient Claude Code status panel for your iCUE display ... this PC's CPU and memory use ... http://127.0.0.1:2722" |
| `widget/manifest.json:12` | `"platform": "windows"` |
| `widget/manifest.json:17` | `"type": "dashboard_lcd"` |
| `widget/manifest.json:21` | `"widgetbuilder.sensorsdataprovider:Sensors:1.0"` |
| `widget/scripts/sidecrab.js:4875` | "not paired - set Approval Pairing Code in widget settings" |
| `widget/scripts/sidecrab.js:4889` | "<decision> refused - pairing code wrong; check widget settings" |
| `widget/scripts/sidecrab.js:6156` | "toast after <N s\|N min> (panel)" / "(saved)" - "toast" names the Windows notifier |
| `widget/scripts/sidecrab.js:6808` | "CPU and GPU are set to the same sensor - pick a different GPU sensor in the widget settings" |
| `widget/scripts/sidecrab.js:6821` | "pick sensors in settings" |
| `widget/scripts/sidecrab.js:6849` | aria-label: "Open this PC's CPU and memory history" |
| `widget/scripts/sidecrab.js:7029` | host sheet title: "This PC" |
| `widget/translation.json:5` | "crabd Port" |
| `widget/translation.json:12` | "Desktop Toast Alerts" |
| `widget/translation.json:13` | "Toast After" (and 14, "Approval Toast After") |
| `widget/translation.json:17` | "Hardware Sensors" |
| `widget/translation.json:18` | "CPU Temperature Sensor" (and 19, "GPU Temperature Sensor") |
| `widget/translation.json:20` | the full sensors help string, naming iCUE three times and "importing the widget" |
| `widget/translation.json:24` | "Widget Personalization" |
| `widget/translation.json:29` | the full SideCrab group help string, naming 127.0.0.1 and "Desktop Toast Alerts" |

### Test-SideCrab.ps1 doctor rows

The macOS doctor prints the same rows, or a documented replacement for each. In emission order:

- `task crabd`, `task glow`, `task toast` - one row per catalogue component, the name interpolated
  from the component key (so the row set follows the catalogue, not a hard-coded list)
- `health`
- `state reachable`
- `state schema`
- `state freshness`
- `hook cycle` - emitted only as a FAIL, when the smoke session id is already live
- `hook SessionStart`
- `hook Notification` - skipped with no row when the configured toast `thresholdSec` is under 60
- `hook SessionEnd` - always, posted from a `finally` so an aborted run strands no phantom session
- `config.json`
- `notifier schema`
- `glow schema`
- `toast identity`
- `toast action (ack)` and `toast action (snooze)` - one row per registered URL scheme, the name
  built as `"toast action (<key>)"`
- `statusline chain`
- `limits token` - reported, never judged
- `panel approvals` - OFF is reported, ON is judged

Replacements the port implies: `task <key>` becomes the launchd equivalent; `toast identity` and the
two `toast action` rows disappear with the AUMID and the URL schemes; `glow schema` disappears with
the lighting component; `notifier schema` gains a sibling row for the panel's own `SCHEMA_MAX`, which
is the check that catches producer/consumer divergence and is the most likely regression class during
the port.

### Existing test seams worth reusing

- **Base classes.** `TempProjects` (`companion/tests/test_crabd.py:195`) for a temp projects tree with
  per-test config repointing; `ServedOverASocket` (`:3055`) for a live crabd on port 0 with
  `Handler.builder` set as a class attribute - a static panel route lands on the same Handler and
  every one of these fixtures exercises it for free; `V12ServedTests` (`:7193`) for the four v0.12.0
  readers plus a `PanelToken`; `LiveFireServed` (`companion/tests/test_crabd_livefire.py:305`) for the
  fully wired fixture with its LIFO `release_parked` cleanup; `BuilderHarness`
  (`companion/tests/test_crabd_datalane.py:568`) for reader wiring with no socket at all - the
  cleanest example of the optional-kwarg seam and the shape to copy for a Darwin host sampler or a
  launchctl fleet reader; `DataLaneEndpointTests` (`:732`) for the three ingest endpoints;
  `HistoryTempFile` (`companion/tests/test_crabd.py:4580`) for the history suites.
- **Stubs and helpers.** `StubLimits` (one per module), `StubHost` (`test_crabd.py:99`, so
  contract-shape assertions give the same answer off Windows), `FakeGit` (`:3367`),
  `FakeHTTP` (`:10756`), and the six transcript fixture helpers at `:118`-`:186` whose Windows cwd
  defaults are centralised so the sweep is one edit rather than 134.
- **`scripted_sampler` / `ScriptedCounters`** (`test_crabd.py:9457` and `:9428`) - `HostSampler`'s two
  kernel reads driven off lists, with the last entry sticking and `None` meaning a failed read.
  Injection is by callable rather than by patching `ctypes`, deliberately: the arithmetic is what
  needs proving and it is unreachable if the test has to own a real kernel counter to get to it. A
  Darwin counter fixture drops in here.
- **`FakeSchtasks` and `schtasks_csv`** (`test_crabd.py:4413` and `:4429`) - `FleetReader(runner=...)`
  is the only impure call and the runner returns a plain `(code, stdout, stderr)` tuple, so a
  launchctl fake drops in without touching the classifier. Keep the five mapping outcomes and the four
  never-guess cases.
- **`KeepAliveClient`, `start_test_server`, `settle` and `quiesce`**
  (`companion/tests/_httpkeepalive.py:108`, `:237`, `:289`, `:320`) - the only way tests bind a
  server, on a port proven reachable by a real request before the fixture is handed back, and never
  the production port. `settle()` is a barrier for the fire-and-forget endpoints that answer before
  they parse; `quiesce()` waits until a receiver has seen N documents, for asserting something did not
  happen. A static panel route must be reachable through this same harness.
- **The `test_ordering.js` vm loading pattern** (`widget/tests/test_ordering.js:31`) - the shipping
  `sidecrab.js` is read whole into a `vm` context with a document stub whose `readyState` is
  `'loading'`, so `init()` parks on a listener nobody fires; line 53 throws if it ran anyway.
  Top-level names are read straight off the returned context. New top-level work in the port needs a
  stub there, never a forked copy of the logic, and the deliberate mutant clamp at line 98 must stay.
- **The Pester Describe/It names to mirror** (`setup/tests/SideCrab.Setup.Tests.ps1`) - one Describe,
  26 Contexts, 298 Its, and the It names are the specification a Python installer suite should
  reproduce. The load-bearing ones named by the readers: "the ten setup scripts all exist" (`:101`),
  "the hooks README documents the PermissionRequest shape the binary actually accepts" (`:379`),
  "keeps the five curl ingest hooks as command hooks on /v1/hook" (`:972`), "no http hook is wired on
  SessionStart or Setup (the binary skips those)" (`:997`), "Install defaults panel approvals OFF -
  never auto-enables" (`:1049`), "Install uses a CONSOLE python for the status line, not pythonw"
  (`:1061`), "NEVER enables approvals and never decides for the operator" (`:1186`), "reads the
  pairing code (crabd 0.29.0) in display form, or Present=false" (`:1296`), "stores the long-lived
  limits token DPAPI-protected and reports presence only" (`:1317`), "uses the contract's own 30s
  staleness limit" (`:873`), and the whole "the restart port race (v0.20.0)" Context (`:2238`). The
  suite's own header rule - nothing installs anything, scripts are parsed rather than executed, every
  impure path is an injected scriptblock - must survive into any replacement or the suite becomes
  destructive.

## Decisions the brief asked for

One line per decision, with the phase that made it and the document that carries the
reasoning. Where two places state the same thing, the first named is the one to change first.

### Transport (Phase 1)

- **The port is 9999 and the bind stays loopback.** `DEFAULT_PORT = 9999`, overridable by
  `CRABD_PORT`; `HOST = "127.0.0.1"` is a module literal with no environment read and no
  config key, and a source-text test refuses `0.0.0.0`. 2722 was fine for a widget configured
  once at a console; a page a person opens needs a number a person types.
  `docs/STATE-CONTRACT.md` v0.31.0 §1.
- **The Origin gate became an allowlist, and the `null` row was preserved.** The three
  spellings of crabd's own bound origin are accepted and reflected exactly; every other
  `http(s)` origin, including the same host on another port and the same authority over
  `https`, is still 403, and `ACAO: *` is still illegal everywhere. `null`, `file://` and
  `qrc://` keep their existing answers - refusing `null` would refuse a QtWebEngine build that
  has no other value to send. `docs/STATE-CONTRACT.md` v0.31.0 §3, `SECURITY.md`.
- **Every POST carries `X-SideCrab-Panel`, hooks and OTLP included.** Any non-empty value; a
  POST without it is 403 `{"error":"panel header required"}` on every path including unknown
  ones, and the origin gate answers first so a cross-site page never learns the header exists.
  Both hook fragments carry it - on the curl line for a `command` hook, in the `headers` map
  for an `http` one - and an OTLP exporter has to be told
  (`OTEL_EXPORTER_OTLP_HEADERS=X-SideCrab-Panel=1`), which nothing in this repo writes.
  `docs/STATE-CONTRACT.md` v0.31.0 §4, `hooks/README.md`.
- **A `Host` allowlist runs ahead of every other gate.** It is the DNS-rebinding gate and the
  only one that can be: a page whose name re-resolves to 127.0.0.1 is same-origin as far as the
  browser is concerned, so its `GET` carries no `Origin` at all. Accepted: absent, and
  `localhost` / `127.0.0.1` / `[::1]` with no port or with the bound port. Refused: everything
  else, which by construction also refuses a port forward or a reverse proxy, because that is
  the same shape as the attack. `docs/STATE-CONTRACT.md` v0.31.0 §2, `SECURITY.md`.
- **Static serving is scoped to four first segments and capped at 64 MB.** `GET /` and
  `/index.html` serve the panel; only a path whose first segment is `styles`, `scripts`,
  `resources` or `mock` serves a file, so `/manifest.json`, `/translation.json`, `/DEV.md` and
  `/tests/...` are 404. One percent-decode, then a refusal of `..`, backslashes, NULs, empty
  and dot segments and surviving `%`, then a containment check against the resolved
  `CRABD_PANEL_DIR`. One reply reads at most `PANEL_MAX_BYTES` = 64 MB, checked by `stat` before
  the read - a limit on a directory the operator can point anywhere, not on the shipped panel.
  `docs/STATE-CONTRACT.md` v0.31.0 §5.
- **`SO_REUSEADDR` is a per-platform answer, not a constant.** `False` on Windows, where it
  admits a second listener on a port already being listened on; `True` on macOS and Linux,
  where it only buys a restart inside TIME_WAIT. A collision stays loud on all three.
  `docs/STATE-CONTRACT.md` v0.31.0 §1.
- **A held port is a loud stop that names the platform's own command.** crabd makes one bind
  attempt on the port it was told to use, quotes what the OS said verbatim, and exits 1;
  the hint is `lsof -nP -iTCP:<port> -sTCP:LISTEN` on macOS and Linux and
  `Get-NetTCPConnection` on Windows. The refused alternative - bind the next port - is recorded
  so it is not re-tried: it produces crabd up on 10000 while every hook and the panel still
  address 9999. `docs/STATE-CONTRACT.md` v0.31.0 §1.

### Host metrics (Phase 2)

- **`nice` counts as busy time.** It is user-priority-lowered work actually running, so it is
  folded into `user`; left out, a machine doing background work at nice priority under-reports
  (the worked example is 50.0% with it and 40.0% without). Idle is folded into kernel for the
  same reason the Win32 sampler expects it - unfolded, the sampler's A-08 branch serves `null`
  on every pass on a healthy Mac. `docs/STATE-CONTRACT.md` v0.32.0 §2.
- **The 32-bit mach counters are unwrapped before the sampler sees them.** They are `natural_t`
  and wrap 2^32 after about 31 days of uptime at the measured rate, which would be one backwards
  jump per bucket per month and a null gauge each time. Last raw value plus a lap count, kept
  per bucket. A genuine backwards movement is indistinguishable from a wrap and is treated as
  one - the honest trade is a single over-large window rather than a dead gauge on every
  long-uptime machine. `docs/STATE-CONTRACT.md` v0.32.0 §3.
- **`memUsedGB` is Activity Monitor's "Memory Used", not `top`'s.** App memory (internal minus
  purgeable) plus wired plus compressed: 66.0 GiB of 128.0 on the machine measured, where
  `top`'s total-minus-free is 99.3 GiB from the same page counts. The contract's promise for
  this row has always been that it matches the OS's own monitor, and the figure an operator can
  check against an app they already have open is the useful one. `free_count` and
  `inactive_count` therefore do not enter the formula. `docs/STATE-CONTRACT.md` v0.32.0 §4.

### Fleet (Phase 3)

- **`fleet.glow` is served `absent` on macOS, and nothing is spawned for it.** There is no
  lighting component on a Mac, so glow has no launchd label at all; an empty label
  short-circuits to the sentinel the platform reads as `absent`. `absent` is the literally true
  word - the query did not fail, the component is not there - and the KEY stays so the
  document's shape is identical on both platforms. The panel's rendering of `absent` is
  unchanged, so no widget update is implied. `docs/STATE-CONTRACT.md` v0.33.0 §3.

### Secrets (Phase 4)

- **Two login-Keychain generic-password items, and the store goes in on stdin.**
  `Claude Code-credentials` (written by Claude Code) and `SideCrab limits token` (written by
  `setup/install.sh --limits-token`), both with the login user name as the account. `ps` is
  world-readable on macOS, so the write travels on `security -i`'s stdin, hex-encoded by `-X`,
  with `-U` so a second token replaces the first. The read needs no secret in either direction.
  `docs/STATE-CONTRACT.md` v0.34.0 §1 and §3.
- **The file wins over the Keychain, the module carries a kill switch, and `CRABD_CLAUDE_HOME`
  suppresses the Keychain entirely.** `~/.claude/.credentials.json` is read first because the
  documentation makes it the CLI's own fallback and because asking the Keychain for an answer
  crabd already has would raise a dialog for nothing. `KEYCHAIN_CREDENTIALS_ENABLED` gates all
  three accesses - not only the credential one its name comes from - and every companion test
  module sets it `False` in `setUpModule` exactly as it repoints the path globals. A custom
  config dir keys a different Keychain entry whose name crabd cannot compose, so with
  `CRABD_CLAUDE_HOME` set the Keychain is not consulted at all. `docs/STATE-CONTRACT.md`
  v0.34.0 §2.
- **The three "store a long-lived one" notes name the platform's own command.**
  `Install-SideCrab.ps1 -LimitsToken` on Windows (unchanged), `setup/install.sh --limits-token`
  on macOS, `(no long-lived token store on this platform)` anywhere else. A fourth note, macOS
  only, separates "the Keychain refused this process" from "there are no credentials" - the two
  have different actions attached, and it is raised only when `security` actually ran and exited
  non-zero and non-44. `docs/STATE-CONTRACT.md` v0.34.0 §4.

### The browser panel (Phase 6)

- **One namespaced `localStorage` object on the panel's own origin, and the pairing guarantee is
  weaker.** Settings, display state and the pairing code live under the single key `sidecrab`
  (`PANEL_STORE_KEY`) rather than scattered keys, keeping the read-modify-write discipline the
  iCUE `uniqueId` object already had. Only a page on the panel's origin can read it, but an XSS
  in the panel or an extension with storage access could, where an iCUE property could not. The
  stronger option - an `HttpOnly` cookie crabd mints - was named and not taken, because it is a
  different pairing flow and a different `decide` wire shape. `SECURITY.md`, `widget/DEV.md`
  v0.30.0.
- **The sensors row keeps only the half a browser can fill.** No page can read a die
  temperature, so `sensorsPlugin()` returns null, the two temperature cells never render and
  both iCUE hints go with them. What is left is crabd's `host` block; an absent or all-null
  block takes the whole row off the glass rather than showing zeros. `widget/DEV.md` v0.30.0.
- **Every dev flag stays gated on `?mock=`.** The old ground for that gate - the iCUE origin
  carries no query string - died the moment crabd served the panel at an addressable URL, and
  `&ackflash=1` performs a real ack-all POST. The gate is kept and now pinned by a test rather
  than by the comment. `widget/DEV.md` v0.30.0, and the traps list above.
- **The standalone case: the companion is required and there is no fallback server.** The page
  is served by crabd, so a page with no crabd is a page that did not load; a fresh navigation
  gets a browser connection error and not a SideCrab screen. An already-open tab survives -
  the poll fails, the crab goes worried and the stale banner names the time of the last good
  document. `widget/DEV.md` v0.30.0.
- **`manifest.json` and the strict-XML check are kept and kept passing.** The iCUE build is
  still packageable from this tree - it is the same files - so the uppercase DOCTYPE, the
  absent bare `&`, the CDATA block and the CI parse all stay. That parse is the only check that
  has ever caught a blank-panel ship. One string changed: `<title>` is the literal `SideCrab`,
  because `tr()` is substituted by nothing at all in a browser. `widget/DEV.md` v0.30.0.
- **Keyboard equivalents for the four gestures, and no more.** `a` ack-all, `p` pin,
  Delete/Backspace dismiss, `r` refresh, `s` settings, Escape closes - each calling the same
  function its gesture calls, each inert behind a modifier, an autorepeat, an open sheet or a
  focused input. Arrow-key navigation of the card grid was named and skipped: Tab already
  reaches every card, and arrows would be a second traversal with its own wrap and column rules
  on a grid whose column count is a media query. `widget/DEV.md` v0.30.0.

### Notifications (Phase 7)

- **The route is `osascript` with a constant AppleScript, and the text rides in argv.** Three
  `-e` strings that never change, then `--`, then body, title and subtitle as positional
  arguments - the same boundary Windows gets from base64, obtained by not building a script out
  of operator text at all. Control bytes are stripped from every argument because `subprocess`
  raises on a NUL and that is not one of the failures `show()` converts to `False`.
  `notifier/README.md`, "macOS".
- **No buttons, and three standing differences.** `display notification` has no action
  affordance, so acknowledgement happens on the panel; the approval notification carries no
  Approve/Deny either, which is the same deliberate rule as on Windows and not a platform
  limitation. The two recorded residuals of the route are that notifications **stack** rather
  than replace (no replacement identifier exists) and that the identity is **Script Editor's**,
  so the per-app notification switch is Script Editor's. `notifier/README.md`, "macOS".

### The installer (Phase 5)

- **Python is detected by probing, never by path.** `$SIDECRAB_PYTHON`, then `python3.14`,
  `python3.13`, `python3` across `PATH`, `/opt/homebrew/bin` and `/usr/local/bin`, each asked
  its version: Apple's `/usr/bin/python3` is 3.9.6 and is refused by version. The absolute path
  it settles on is written into the plists, because a LaunchAgent does not inherit a login
  `PATH`. `hooks/README.md`, "Installing the macOS fragment".
- **The hook merge is entry-level on one marker, after a backup.**
  `<path>.sidecrab-bak-YYYYMMDD-HHMMSS` before the first write; the marker is
  `127.0.0.1:9999/v1/hook`, which the two `http` URLs contain as a prefix so one marker finds
  both `command` and `url` entries. A hook hand-merged into one of our matcher groups stays, a
  second run is byte-identical and takes no second backup, and a `settings.json` that does not
  parse aborts the whole install before anything is written. `hooks/README.md`.
- **`allowedHttpHookUrls` is extended, never created.** Both host forms are added when the key
  already exists, because patterns match the URL as written and `127.0.0.1` and `localhost` are
  different strings. Creating the key would switch the allowlist on and block every other http
  hook the operator has. Uninstall removes ours and removes the whole key rather than leaving it
  empty, because an empty list admits nothing. `hooks/README.md`.
- **One LaunchAgent per component, and a disabled agent stays disabled.** `com.sidecrab.crabd`
  always and `com.sidecrab.toast` with `--with-toast`; plists at
  `~/Library/LaunchAgents/<label>.plist`, logs at `~/.sidecrab/logs/<label>.log` under mode
  0700 because the log carries session titles and repo paths; loading is `launchctl bootout`
  then `launchctl bootstrap gui/<uid>`. A label the operator disabled has its plist refreshed
  and is not started, and `--force-enable` is the only override - the same trap Windows'
  `Register-ScheduledTask -Force` has, which once resurrected the parked glow component.
- **A restart refuses to start blind.** A foreign holder of port 9999 is refused before install
  or update writes anything, and the refusal names the PID: a foreign process holding the port
  is a different problem from a slow shutdown, and starting anyway is what produced a dark
  panel. Wait budgets are counted in polls, not wall clock, because the sleep is injectable.
- **`--doctor` is not read-only and says so.** It posts a real SessionStart / Notification /
  SessionEnd cycle for the session id `smoke-test` to prove the write path end to end, and
  probes the header gate by POSTing without `X-SideCrab-Panel` and expecting the 403. The cycle
  clears its own row from a `finally`, but crabd persists every hook event, so a run leaves
  three rows in `~/.sidecrab/history.jsonl`. `--status` writes nothing at all.

### Scope (Phase 8)

- **Windows is retained, and its CI job with it.** The iCUE widget and the PowerShell installer
  stay in the tree and stay packageable; the Pester suite runs only on Windows, so the Windows
  job in `.github/workflows/ci.yml` is kept deliberately rather than left behind. The macOS
  installer's Python suite runs only in the macOS job, because its shell-wrapper tests need
  `/bin/sh`. Nothing Windows was deleted at any point in the port.

## Definition of done

_(filled by the coordinator after the live install)_
