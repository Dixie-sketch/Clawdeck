#!/usr/bin/env python3
"""crabd - SideCrab companion service.

Serves the /v1/state document defined in docs/STATE-CONTRACT.md (schema 5, the last
breaking shape; the v0.6.x through v0.28.0 fields ride on it additively) on
127.0.0.1:9999 for the SideCrab panel, from eleven sources:

  1. Claude Code hooks POSTed to /v1/hook  -> session state machine, per-session events
  2. ~/.claude/projects/**/*.jsonl         -> titles, model, token burn, questions,
                                              contextTokens
  3. the Claude OAuth usage endpoint       -> 5-hour / weekly limit gauges (the FALLBACK
                                              source since v0.12.0)
  4. ~/.sidecrab/config.json               -> quiet hours, the quiet OVERRIDE (v0.23.0),
                                              toast, digest, the burn budget, panel
                                              approvals, the reply gate
  5. `git log` in today's session cwds     -> recap.commits, recap.week[].commits
  6. `schtasks /query` on Windows, `launchctl print gui/<uid>/<label>` on macOS
                                           -> fleet (glow / toast)
  7. ~/.sidecrab/history.jsonl             -> replayed at startup so doneToday, the
                                              per-session events ring and recap.week
                                              survive a crabd restart
  8. the status line document on /v1/statusline (v0.12.0) -> limits and per-session
                                              context, officially, with no OAuth token
                                              in the picture; PREFERRED over source 3
  9. OTLP http/json on /v1/metrics + /v1/logs (v0.12.0) -> burn.costUSD in real dollars,
                                              api_error events onto the session rings
 10. GetSystemTimes / GlobalMemoryStatusEx on Windows, mach host_statistics /
     host_statistics64 / sysctlbyname on macOS (v0.22.0, v0.32.0)
                                           -> `host`: this machine's CPU
                                              utilization and memory, for the panel
                                              beside the iCUE temperature sensors
 11. GET /v1/models on the same OAuth token (v0.28.0) -> the context WINDOW size behind
                                              contextWindowTokens, for a model id that
                                              carries no [1m]/[200k] marker (which is
                                              every live one); ranked BELOW the status
                                              line and the marker, both session-specific

and accepts touch actions on /v1/action (ack / ack-all / queue-continue / decide /
quiet; reply is 501, see below), answers the Stop and PermissionRequest hooks on
/v1/hook/stop and /v1/hook/permission, plus quietHours / toast / digest / budget
writes on /v1/config.

/v1/panel-log (v0.24.0) is a SIDE CHANNEL, not a source: the widget POSTs short
diagnostic lines to it and a maintainer GETs them back, because iCUE renders the widget on
a surface no devtools can reach. It is in-memory only, it feeds nothing, and nothing in
here ever reads a stored line back into a decision.

stdlib only, Python 3.13, Windows host. ~/.claude is read strictly read-only.
"""

from __future__ import annotations

import csv
import ctypes
import ctypes.util
import hmac
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath

try:
    # POSIX only, and it is the login user name for the two macOS Keychain items - see
    # _login_account. Guarded rather than lazily imported inside the reader so that
    # "this host has no pwd" is answered once, at import, on the one platform where it
    # is true (Windows) rather than per call.
    import pwd
except ImportError:                     # pragma: no cover - Windows
    pwd = None

# The served `schema` marks the last BREAKING shape, NOT the feature level - see the
# VERSIONING REWORK section of docs/STATE-CONTRACT.md. Additive fields (contextTokens,
# fleet, everything after) ship under this same number and are found by FIELD PRESENCE;
# only a change that alters or removes an existing field bumps it, and that bump costs a
# coordinated deploy. The lesson that bought this: crabd redeploys over RDP, the widget
# does NOT - the .icuewidget import is a double-click at the iCUE console - so shipping
# schema N+1 dead-feeds the on-glass panel until someone stands at the desk.
SCHEMA_BREAKING = 5
VERSION = "0.34.0"

HOST = "127.0.0.1"
# The production port, and the one the service registration owns. It was 2722 (C-R-A-B on
# a phone keypad) while the only client was a widget configured once at the iCUE console;
# it is 9999 now that the panel is a page a person opens in a browser and therefore a
# number a person types. Stated ONCE: PORT below reads this, and so does every test that
# has to promise it is not binding production.
DEFAULT_PORT = 9999
# CRABD_PORT exists so a test instance can run against the real ~/.claude without racing
# the live service.
PORT = int(os.environ.get("CRABD_PORT") or DEFAULT_PORT)

SIDECRAB_DIR = Path.home() / ".sidecrab"
USER_CONFIG_FILE = SIDECRAB_DIR / "config.json"
# The panel pairing code (v0.29.0, closes SEC-a + WID-a). A 10-symbol secret crabd mints
# once and keeps in the user's profile; the widget presents it on every `decide`. It is the
# one thing a web page the operator visits cannot obtain: iCUE widget PROPERTIES are not
# reachable from a browser, and a forged `Origin: null` buys nothing without it. Same-user
# local processes can read the file - they can also drive the terminal dialog, so they were
# never in the threat model. Crockford-style alphabet (no I, L, O, U) so a code read off a
# terminal and typed into iCUE's settings cannot be mis-transcribed.
PANEL_TOKEN_FILE = SIDECRAB_DIR / "panel-token"
PANEL_TOKEN_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
PANEL_TOKEN_LEN = 10                 # 32^10 = 2^50 - hopeless to guess at loopback speed once
PANEL_TOKEN_MAX_FAILURES = 10        # ...the lockout below bounds the rate anyway
PANEL_TOKEN_WINDOW_SEC = 60.0
PANEL_TOKEN_LOCKOUT_SEC = 60.0
# v0.7.0 history persistence. Like LIMITS_CACHE_FILE this is a module GLOBAL naming a
# real file under ~, and HistoryLog resolves it per call - so the test module can patch
# it once at module scope and no test can reach the operator's file. That is not a
# theoretical courtesy: the limits cache was poisoned exactly this way on 2026-08-26.
HISTORY_FILE = SIDECRAB_DIR / "history.jsonl"
HISTORY_MAX_BYTES = 2 * 1024 * 1024   # contract: rotate at ~2 MB, ONE .old generation
HISTORY_OLD_SUFFIX = ".old"
# `kind` for a done TRANSITION. Every other kind is an events-ring text, which is why
# an unrecognised kind replays as a ring entry: a future crabd adding an event text
# must not have its lines silently dropped by this one.
HISTORY_DONE_KIND = "done"
# Ring events older than this can never surface on a served row (the session is past
# both SESSION_WINDOW_SEC and GONE_AFTER_SEC), so replaying them would only grow the
# in-memory session table with rows nothing can ever render.
HISTORY_REPLAY_SEC = 24 * 3600
CLAUDE_HOME = Path(os.environ.get("CRABD_CLAUDE_HOME") or (Path.home() / ".claude"))
# Was crabd pointed somewhere other than ~/.claude? Read ONCE, here, because the answer
# decides whether the login Keychain may be asked for the CLI credential on macOS: the
# documentation says a custom config dir keys a DIFFERENT Keychain entry, and crabd
# cannot compose that entry's name - so asking about the default one would answer about
# a login the operator is not running. See DarwinPlatform.cli_credentials.
CUSTOM_CLAUDE_HOME = bool(os.environ.get("CRABD_CLAUDE_HOME"))
PROJECTS_DIR = CLAUDE_HOME / "projects"
CREDENTIALS_FILE = CLAUDE_HOME / ".credentials.json"

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_BETA = "oauth-2025-04-20"
# Retuned 2026-08-26 after ~1.5 h of continuous 429s (Retry-After: 0) on the usage
# endpoint: fetch less often, and trust a good reading for longer. The gauges drift
# minutes-slow; em-dashes tell the operator nothing at all.
LIMITS_TTL_SEC = 600              # success cache; the endpoint quota-buckets aggressively
LIMITS_429_BACKOFF_SEC = 300      # base; doubles per consecutive 429 (Retry-After is 0 there)
LIMITS_429_BACKOFF_MAX = 1800
LIMITS_LAST_GOOD_MAX_AGE = 10800  # serve last-good through a lockout up to 3 h old
# Past this age the served reading is QUALIFIED, not withheld: contract v0.4.0 widens
# limits.note to a caveat that rides alongside available:true.
LIMITS_NOTE_STALE_SEC = 900
LIMITS_CACHE_FILE = SIDECRAB_DIR / "limits-cache.json"  # survives restarts; no secrets in it
# v0.30.0: an OPTIONAL long-lived token for the usage endpoint. The CLI's own access
# token in ~/.claude/.credentials.json lives ~6 h and is rewritten only when a terminal
# `claude` makes an API call - the desktop app keeps its refreshed token elsewhere - so a
# panel fed from that file reads "token expired" most mornings. `claude setup-token`
# mints a token that lasts about a year; Install-SideCrab.ps1 -LimitsToken stores it here
# DPAPI-protected (CurrentUser), and crabd decrypts it in memory when the CLI token is
# past its expiry. Never logged, never served, never written anywhere else.
LIMITS_TOKEN_FILE = SIDECRAB_DIR / "limits-token.dpapi"
# --- macOS: the login Keychain, which is where BOTH secrets live on a Mac.
#
# `Claude Code-credentials` is the CLI's OWN credential. MEASURED 2026-09-04 (Claude Code
# 2.1.260): ~/.claude/.credentials.json does not exist on this machine at all and the
# Keychain item does, so a crabd that only knows about the file reads "no Claude
# credentials" for ever on an account that is perfectly logged in. The documentation says
# the file is written only when the Keychain write FAILS, which is why the file still
# wins where both exist.
#
# `SideCrab limits token` is SideCrab's own store for a long-lived `claude setup-token`
# value - the macOS answer to the DPAPI blob above. The account half of both items is the
# login user name, and setup/sidecrab_setup.py probes the same pair by exit code.
#
# THE KILL SWITCH is not a feature: it is how the test suite guarantees it cannot raise a
# Keychain prompt on the operator's desktop, read a secret it has no business seeing, or
# WRITE an item into a person's login Keychain. Every companion test module sets it False
# in setUpModule, exactly as they repoint the path globals, and the tests that exercise
# these paths turn it on with an injected runner.
#
# It gates every Keychain access, not only the credential one its name comes from: all
# three of cli_credentials, read_limits_token and store_limits_token check it, because
# "no test reaches the operator's Keychain" is only a guarantee if it has no exceptions.
KEYCHAIN_CREDENTIALS_ENABLED = True
KEYCHAIN_CREDENTIALS_SERVICE = "Claude Code-credentials"
KEYCHAIN_LIMITS_SERVICE = "SideCrab limits token"
SECURITY_BIN = "/usr/bin/security"
KEYCHAIN_TIMEOUT_SEC = 5.0
# MEASURED 2026-09-04: `security find-generic-password` exits 44 for an item that is not
# there ("The specified item could not be found in the keychain"), on the argv form and
# inside `security -i` alike. ABSENCE, not failure - it is answered silently.
#
# WHERE 44 COMES FROM, because it is not an errno and the arithmetic explains its
# neighbours: the tool exits with the OSStatus truncated to its low byte.
# errSecItemNotFound is -25300, and -25300 & 0xFF == 44; errSecInteractionNotAllowed is
# -25308, and -25308 & 0xFF == 36, which is the code the refused-read note is written
# against. Two OSStatus values 256 apart would collide here; none of the ones these two
# reads can produce do.
KEYCHAIN_ITEM_NOT_FOUND = 44
#: What crabd is willing to STORE as a long-lived token: the SHAPE of a `claude
#: setup-token` value (`sk-ant-oat01-...`), never decoded, never logged. The same
#: expression setup/sidecrab_setup.py validates with before it hands one over. It is also
#: a safety rule, not only a typo catcher: the macOS store command goes through
#: `security -i`'s own tokenizer, so a value carrying a quote or a newline is refused
#: here rather than quoted around.
#:
#: MATCHED WITH `fullmatch`, and the anchors are kept for the reader rather than for the
#: engine: `$` also matches just BEFORE a final newline, so `re.match` accepted a token
#: pasted with its line ending still attached - which is exactly the value that would
#: have ended the store command early.
LIMITS_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,512}$")
# What the panel says when the item is there and crabd was not allowed to read it. It
# names the ONE action that fixes it. Deliberately not the "no Claude credentials" note:
# an operator told to log in for a Keychain crabd could not open would do it, watch
# nothing change, and have no next move.
KEYCHAIN_REFUSED_NOTE = ("Claude credential is in the Keychain and crabd could not read "
                         "it - approve the Keychain prompt (Always Allow) or run claude "
                         "in a terminal")
# A cached `at` before this is not a stale reading, it is CORRUPT. Measured in
# production 2026-08-26: the real cache held at=1000.0 (Jan 1970) because the unit
# suite wrote the live file with fixture data. An `at` from 1970 makes every age
# computation meaningless, so the entry is treated as absent.
LIMITS_CACHE_MIN_EPOCH = 1.6e9    # 2020-09-13; crabd did not exist before it
LIMITS_HTTP_TIMEOUT = 10

# --- v0.28.0 model catalog: the ctx-fill gauge's DENOMINATOR for an unmarked model id.
# `GET /v1/models` on the same OAuth bearer the usage endpoint takes, mapping each
# model's `max_input_tokens` (the context window; `max_tokens` is the OUTPUT cap and is
# not it). Measured live 2026-08-28 on the operator's token: HTTP 200, ten models,
# claude-opus-5 / claude-sonnet-5 / claude-fable-5 / claude-sonnet-4-6 at 1000000 and
# claude-opus-4-5-20251101 / claude-haiku-4-5-20251001 at 200000.
#
# THIS IS THE ONLY SANCTIONED SOURCE FOR THAT NUMBER. There is deliberately no built-in
# model->window table anywhere in SideCrab: the widget refuses one (see its
# ctxWindowTokens) because a hardcoded "opus means 200k" is a number no document said,
# and it would go silently wrong the day a window changes. Moving the lookup into crabd
# does not move that rule - it only moves WHO asks the API.
MODELS_URL = "https://api.anthropic.com/v1/models?limit=100"
MODELS_API_VERSION = "2023-06-01"
# A model's window is a fixed property of the model, not a reading that drifts, so the
# TTL is hours rather than the usage endpoint's ten minutes - and a stale-by-TTL entry is
# still kept and served while a refresh keeps failing (see ModelCatalog._ensure).
MODELS_TTL_SEC = 6 * 3600
# After ANY failed fetch, wait this long before the next attempt. Without it a builder
# running every REFRESH_INTERVAL_SEC (2 s) would hammer the endpoint with an expired
# token, which is how the usage endpoint earned its 429 lockout on 2026-08-26.
MODELS_RETRY_SEC = 900
MODELS_HTTP_TIMEOUT = 10

# --- v0.13.0 depletion forecast (limits.fiveHour/weekly[.exhaustAt], schema stays 5).
# A short in-memory rolling history of each window's (ts, utilization) as it is SERVED,
# from which crabd projects when the window hits 100% at the recent burn rate. The
# builder runs every REFRESH_INTERVAL_SEC (2 s), so a naive "one sample per build" would
# hold ~450 samples over 15 min OR, capped at 20, span only ~40 s - below the 60 s the
# slope needs. FORECAST_MIN_SAMPLE_GAP_SEC is the reconciler: record at most one sample
# per ~gap, so 20 samples DO span ~15 min and a 2-reading test 60 s apart still lands
# both. A window that drops (a reset, or a statusline<->oauth source flip that re-reads a
# lower number) has its history CLEARED - a decrease is never depletion, and a slope
# fitted across a reset would forecast nonsense.
FORECAST_WINDOW_SEC = 15 * 60       # rolling history horizon (~15 min)
FORECAST_MAX_SAMPLES = 20           # cap; with the gap below this spans the whole window
FORECAST_MIN_SAMPLE_GAP_SEC = 45.0  # min spacing between RECORDED samples (20 * 45 = 900)
FORECAST_MIN_SPAN_SEC = 60.0        # need >=2 samples spanning at least this to fit a rate
# Hard cap on the number of DISTINCT windows the forecaster tracks. The two contract-named
# windows are never counted out (see _FORECAST_PROTECTED_KEYS); the rest of the budget is for
# `extra:` labels, which are attacker-influenced through the unauthenticated /v1/statusline
# (each `seven_day_*` key mints one). Without a cap the _history dict grows without bound - a
# flood of fresh random labels is a slow OOM of a daemon meant to run for weeks. 64 is far
# above any legitimate extra count (a handful of model-scoped weeklies) yet bounds the flood;
# eviction is least-recently-updated, so a genuinely recurring extra is never the one dropped.
FORECAST_MAX_KEYS = 64
_FORECAST_PROTECTED_KEYS = ("fiveHour", "weekly")
# A utilization decrease beyond this clears the window's history. Tiny so any real drop
# resets, but non-zero so float noise on an otherwise-flat reading does not; served
# utilization is rounded to 4dp upstream, so the smallest genuine step is 1e-4.
FORECAST_DROP_EPS = 1e-9

# fleet: SideCrab watching its own Scheduled Tasks. crabd is deliberately absent from
# the list - if the widget is reading this document, crabd is running.
FLEET_TASKS = (("glow", "SideCrab-glow"), ("toast", "SideCrab-toast"))
FLEET_REFRESH_SEC = 60       # contract: cached ~60 s
FLEET_POLL_SEC = 5.0
FLEET_TIMEOUT_SEC = 10       # contract
# Measured 2026-08-26 on the Windows host: `schtasks /query /tn SideCrab-glow /fo csv /nh` exits 0
# with '"\SideCrab-glow","N/A","Running"' - the status is the THIRD csv field.
FLEET_STATUS_COL = 2
FLEET_STATUS_MAP = {"running": "running", "ready": "stopped",
                    "queued": "stopped", "disabled": "stopped"}
# Same measurement, unregistered name: exit 1, stderr 'ERROR: The system cannot find
# the file specified.' The second phrasing is schtasks' other not-found wording ('The
# specified task name ... does not exist'); anything else that fails is `unknown`,
# because a task that exists and cannot be read is NOT the same claim as an absent one.
FLEET_ABSENT_MARKERS = ("cannot find", "does not exist")
# Measured 2026-09-04 on macOS 26.6 (uid 502). `launchctl print gui/<uid>/<label>` exits 0
# for a loaded agent and prints a block whose FIRST-LEVEL lines carry ONE tab: a running
# agent has '\tstate = running' and a '\tpid = ' line, a loaded idle one '\tstate = not
# running', '\truns = 0' and no pid at all. Sub-objects are indented deeper and carry
# their own '\t\tstate = active' lines, which is why the parse reads the first-level
# line only.
# `waiting` and `spawn scheduled` are the other words launchd uses for not executing.
# An unrecognised word is `unknown`, never `stopped` - same rule as the Windows map.
LAUNCHD_STATUS_MAP = {"running": "running", "not running": "stopped",
                      "waiting": "stopped", "spawn scheduled": "stopped"}
# Same measurement, unregistered label: exit 113, stdout empty, stderr 'Bad request.\n
# Could not find service "com.sidecrab.nonexistent" in domain for user gui: 502'. Any
# OTHER non-zero exit is `unknown` - a label that exists and cannot be read is not the
# same claim as an absent one.
LAUNCHD_ABSENT_MARKERS = ("could not find service",)
# The (code, out, err) a platform returns for a component it has NO service for at all -
# macOS glow, since there is no lighting component here. A code of None is not an exit
# status any process can produce, which is what makes it unmistakable; the platform's
# service_status turns it into a word, so "this component does not exist here" stays a
# platform answer rather than becoming a rule inside FleetReader.
FLEET_NO_SERVICE = (None, "", "")

# --- v0.22.0 `host`: the machine's own CPU and memory, beside the iCUE temperatures.
# Sampled on the BUILDER's existing pass (REFRESH_INTERVAL_SEC, 2 s) rather than a
# thread of its own - an ambient gauge does not need better resolution than that, and a
# thread is one more thing that can wedge while the number it feeds keeps being served.
# The 2 s cadence is also what makes the CPU delta meaningful; see HostSampler.
HOST_BYTES_PER_GB = 1024 ** 3   # GiB - the unit Task Manager shows, so the two agree
HOST_CPU_LOG_KEY = "host-cpu"
HOST_MEM_LOG_KEY = "host-mem"
# A-07 (v0.26.0). GetSystemTimes counters do NOT advance continuously - they land in coarse
# scheduler quanta: measured on this host, ~312,500 100ns-ticks (31.25 ms) of movement
# arrive at once. A sampling window so short that only a quantum or two of kernel+user time
# elapsed cannot express a trustworthy busy fraction - idle and kernel moving by the same
# quantum reads as an exact 0.0 on a machine that is NOT asleep. Below this many ticks of
# (kernel+user) delta the split is quantisation noise, so cpuPct is served NULL, never a
# fabricated 0.0. Set well above a single quantum (so the cold-start sub-quantum window is
# caught) and far below a 2 s-cadence total - tens of millions of ticks on even one core -
# so the production poll is never nulled. Reachable only at cold start, where _do_state
# builds on the request thread while _refresh_loop takes its first snapshot (overlapping,
# sub-quantum windows). = 100 ms of aggregate core-time.
CPU_MIN_TOTAL_TICKS = 1_000_000

# --- v0.32.0 `host` on macOS. HostSampler's unit is the Win32 FILETIME's 100 ns tick;
# mach counts CLK_TCK ticks, so DarwinPlatform scales by this over CLK_TCK. Kept as a
# named number because the DIVISIBILITY of it by CLK_TCK is a guard: a CLK_TCK that does
# not divide it evenly would silently lose ticks in the integer division.
HOST_100NS_PER_SEC = 10_000_000
HOST_CPU_STATES = 4          # CPU_STATE_USER, _SYSTEM, _IDLE, _NICE - the reply's count
HOST_CPU_LOAD_INFO = 3       # host_statistics flavour
HOST_VM_INFO64 = 4           # host_statistics64 flavour
# The mach CPU tick counters are natural_t - THIRTY-TWO BITS - and cumulative since boot.
# Measured on an M-series 16-core Mac: ~1600 ticks/s summed across cores at CLK_TCK 100,
# so a bucket crosses 2^32 in about 31 days of uptime. Unwrapped in DarwinPlatform, once,
# so nothing downstream ever sees the backwards jump that would otherwise arrive once per
# bucket per month.
HOST_CPU_COUNTER_MODULUS = 1 << 32

RECAP_REFRESH_SEC = 300      # contract: the recap is cached ~5 min
RECAP_POLL_SEC = 5.0
RECAP_GIT_TIMEOUT_SEC = 10
RECAP_REPO_CAP = 4           # contract: `commits` carries at most 4 repos, count desc
# Candidate repos considered before the git calls, most-recently-active first. A day
# spent in 30 repos would otherwise be 30 subprocesses per cycle; the cap bounds the
# recap thread's worst case at RECAP_REPO_SCAN_CAP * RECAP_GIT_TIMEOUT_SEC.
RECAP_REPO_SCAN_CAP = 12
WEEK_DAYS = 7                # contract: recap.week is 7 local days, oldest first
# The done-transition ring now feeds recap.week as well as doneToday, so it has to hold
# the whole week plus a margin - at 2 days the oldest week buckets would read 0 for a
# reason that has nothing to do with the operator's week.
RECAP_DONE_KEEP_SEC = (WEEK_DAYS + 1) * 86400

REFRESH_INTERVAL_SEC = 2.0   # builder cadence; widget calls stale at 30 s
BURN_WINDOW_SEC = 24 * 3600  # span of the hourly series
BURN_DAILY_DAYS = 7          # contract: burn.daily is 7 local-day buckets, oldest first
# Transcript DISCOVERY window - widened from 24 h to 7 days so burn.daily has data to
# bucket. Measured 2026-08-26 against the real ~/.claude: 222 files / 280 MB inside 7
# days, 1.1 s cold parse, 21 k usage records, 0.1 s per warm pass. What makes that
# affordable is BIG_LINE_BYTES below - without the big-line skip this is a 280 MB
# json.loads on every crabd start.
TRANSCRIPT_WINDOW_SEC = BURN_DAILY_DAYS * 86400
SESSION_WINDOW_SEC = 2 * 3600
IDLE_AFTER_SEC = 15 * 60
GONE_AFTER_SEC = 2 * 3600
DONE_DROP_SEC = 10 * 60
SUBAGENT_ACTIVE_SEC = 90

# A transcript line this big is a tool_result echo (measured: user tool_result lines
# reach hundreds of KB). Assistant usage lines and title lines are small, so skipping
# big lines that carry no "usage" key costs nothing and keeps a cold scan of a 15 MB
# transcript from parsing megabytes of tool output.
BIG_LINE_BYTES = 16384

BURN_MODEL_CAP = 4          # contract: burn.byModel is the top 4 by outputTokens desc
# Bucket for a usage record whose assistant message carried no usable `model` string.
# Only ever emitted when it actually has tokens - an empty "unknown" row would read as
# a defect on a healthy day.
BURN_MODEL_UNKNOWN = "unknown"

TITLE_MAX = 90
# Directory names that identify nothing on their own, so a cwd-derived title ending in
# one takes its parent too ("acme/src", not "src"). Deliberately SMALL and measured -
# every name here is one a widget would otherwise render on several unrelated cards at
# once. A name not in this set is served as-is: over-generalising here costs the
# operator the one word that told two sessions apart.
CWD_TITLE_GENERIC_TAILS = frozenset({
    "main", "src", "app", "repo", "work", "dev", "tmp",
})
EVENT_MAX = 120
QUESTION_MAX = 500          # contract: `question` carries the FULL text, capped
SUBAGENT_LABEL_MAX = 40
SUBAGENT_DETAIL_CAP = 5
# A transcript question older than this relative to the needs_input transition belongs
# to an earlier turn - without the guard a resolved question re-surfaces on the panel.
QUESTION_FRESH_SEC = 120
# CD-28 (v0.21.0). Clock-skew allowance on the turn boundary, NOT a second lookback: a
# question written in the turn's own first moments must not be rejected because the
# UserPromptSubmit hook's receipt time landed a fraction later than the transcript
# record's timestamp. Small on purpose - the gap it forgives is milliseconds of hook
# latency, and every extra second widens the window a previous turn's question can
# re-enter through.
QUESTION_TURN_GRACE_SEC = 5
# CD-29 (v0.21.0). How far a subagent transcript's last write may sit from a recorded
# SubagentStop and still be read as THAT stop's file. A stopping subagent writes its
# final record and the hook reaches crabd immediately after, so the real gap is a flush;
# this is wide enough to cover a slow one and far under SUBAGENT_ACTIVE_SEC, so it can
# never claim a file that is still being written.
SUBAGENT_STOP_MATCH_SEC = 10
CONFIG_RECHECK_SEC = 60
EVENTS_CAP = 8              # contract: sessions[].events, newest first
# POST /v1/config `toast.thresholdSec` bounds. Under 30 s the notifier would toast a
# turn that is merely thinking; over an hour it is not a notification any more.
CONFIG_TOAST_MIN_SEC = 30
CONFIG_TOAST_MAX_SEC = 3600
# POST /v1/config `toast.approvalThresholdSec` bounds - the OPTIONAL third member
# (v0.16.0). Its own pair rather than a reuse of the waiting-toast bounds above: the
# notifier's own default for this key is 20 s, which is BELOW CONFIG_TOAST_MIN_SEC, so
# reusing that floor would 400 the shipped default. A pending PERMISSION is a prompt the
# operator is already blocked on, not a turn that might merely be thinking, so seconds
# are a legitimate setting here where they are not for the waiting toast.
CONFIG_APPROVAL_TOAST_MIN_SEC = 5
CONFIG_APPROVAL_TOAST_MAX_SEC = 3600
# What the notifier DOES when the `toast` block (or one of its two required members) is
# absent or unusable - notifier/sidecrab_toast.py DEFAULT_THRESHOLD_SEC and
# ToastConfig.enabled. Mirrored here rather than imported: crabd must not depend on the
# notifier, and these two are the shipped, documented (README.md) fallbacks. They are
# served in `toast` (v0.18.0) so the widget's settings sheet reads what the notifier will
# actually use, never a blank. There is deliberately NO twin for approvalThresholdSec -
# see toast_block.
CONFIG_TOAST_DEFAULT_SEC = 120
CONFIG_TOAST_DEFAULT_ENABLED = True
# POST /v1/config `budget.dailyOutputTokens` bounds (contract v0.10.0). The floor is a
# real day's output - under it every day crosses 100% and the notifier's one-per-day
# toast becomes an alarm clock. The ceiling is past any plausible Max-plan day, so a
# fat-fingered extra zero reads as a typo instead of silently disabling the feature.
CONFIG_BUDGET_MIN = 100_000
CONFIG_BUDGET_MAX = 100_000_000
# --- v0.23.0 quiet OVERRIDE: POST /v1/action {"action":"quiet"}, persisted under this
# key in config.json. A fixed vocabulary and a bounded duration, both on purpose. The
# override is the operator saying "not for the next while" (or "yes, now, in spite of the
# schedule") with one tap on the glass, and it is the SCHEDULE that owns every other
# minute - so an override that could be indefinite would be a second, invisible schedule
# nobody remembers setting. The floor is the shortest span worth a tap; the ceiling is
# eight hours, long enough for a night or a working day and short enough that a forgotten
# override always expires on its own. NEVER in CONFIG_WRITABLE (see Handler) - the action
# endpoint is this key's only writer.
QUIET_OVERRIDE_KEY = "quietOverride"
QUIET_OVERRIDE_MODES = ("on", "off")   # the PERSISTED modes; "auto" clears, never stores
QUIET_OVERRIDE_MIN_MINUTES = 15
QUIET_OVERRIDE_MAX_MINUTES = 480
# Contract: todayPct is rounded to 4dp and capped. The cap is what keeps a 3-digit
# number off a panel sized for "34%" when someone budgets 100k and spends 40M; the
# widget renders >=150% red either way, so nothing is lost by flattening the tail.
BUDGET_PCT_DP = 4
BUDGET_PCT_CAP = 9.99
# GET /v1/history?day= (v0.8.0). Contract: at most 200 events for the day, newest first,
# `truncated` beyond. A day the operator actually worked runs to a few hundred hook lines,
# so this is a guard against a pathological day, not the normal shape.
HISTORY_DAY_CAP = 200
# The contract's ^\d{4}-\d{2}-\d{2}$, written \A..\Z and ASCII-only because the plain
# form has two gaps: `$` also matches before a TRAILING NEWLINE ("2026-01-01%0A" in a
# query string), and bare \d is Unicode-aware, so Arabic-Indic digits pass it. Either
# way it is only half the validation - the pattern accepts 2026-02-30. The real-date
# half is a strptime in the handler, which is what turns that into a 400.
HISTORY_DAY_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z", re.ASCII)

# Measured 2026-08-26 in ~/.claude/projects/**: an async subagent's launch tool_result
# carries "agentId: <hex>", and <hex> is the subagent transcript's filename stem
# ("agent-<hex>.jsonl"). That is the only link from a running subagent file back to the
# Agent/Task tool_use that names it.
AGENT_ID_IN_RESULT = re.compile(r"agentId:\s*([0-9a-f]{6,})")

# ---------------------------------------------------------------- v0.12.0 constants
# Everything in this block was measured against Claude Code 2.1.246 on 2026-08-26 by
# reading the SHIPPED binary's own schemas and emitters, not only the published docs.
# Where the two disagreed the binary won and the disagreement is recorded - see
# STOP_CONTINUE_* and PERMISSION_* below, which is the whole reason this block exists.

# --- 1. status line ingest (POST /v1/statusline)
# Measured in the shipped statusline document builder (2.1.246):
#     O = { ...P.five_hour && { five_hour: { used_percentage: P.five_hour.utilization*100,
#                                            resets_at: P.five_hour.resets_at } }, ... }
#     ...(O.five_hour || O.seven_day) && { rate_limits: O }
# Three facts follow, and all three are load-bearing:
#  - `used_percentage` is a PERCENT (0..100), always utilization*100. It is NOT the
#    0..1 shape the OAuth endpoint sometimes uses, so it can never be sniffed by
#    "is it > 1" the way LimitsReader._window has to guess - 0.4 here means 0.4%, and
#    guessing would render a nearly-empty gauge as 40% full.
#  - `resets_at` is EPOCH SECONDS, a number. The CLI's own consumer does
#    `Number.isFinite(x)` then `Math.min(...)*1000`, which is only true of seconds.
#  - `rate_limits` is ABSENT ENTIRELY when neither window exists (API key, Bedrock,
#    Vertex, or before the session's first API response). Absence is normal and must
#    fall back to OAuth, never render as zeros.
# Doc: https://code.claude.com/docs/en/statusline
STATUSLINE_MAX_BODY = 256 * 1024
# Contract: "OAuth remains the fallback when no statusline document has arrived in
# 10 min." The status line goes QUIET while the session is idle (its triggers are
# event-driven), so this is deliberately far longer than any refresh interval - it
# answers "has the status line stopped feeding us", not "is a session busy".
STATUSLINE_PREFER_SEC = 600
# Per-session context rows are dropped once no session could still be serving them.
# Same horizon as GONE_AFTER_SEC for the same reason: past it the id cannot appear on
# a served row, so keeping the entry is only table growth.
STATUSLINE_SESSION_KEEP_SEC = GONE_AFTER_SEC
LIMITS_SOURCE_STATUSLINE = "statusline"
LIMITS_SOURCE_OAUTH = "oauth"
CONTEXT_SOURCE_STATUSLINE = "statusline"
CONTEXT_SOURCE_TRANSCRIPT = "transcript"
# CD-36 (v0.21.0). How far BEHIND the transcript's own reading a status-line reading may
# be and still win the precedence contest. It is a clock-skew allowance, not a staleness
# budget: `context_ts` is the CLI's record timestamp and the status line's is crabd's
# receipt clock, and a document posted for a round-trip lands within a second or so of
# the record that describes it. Generous enough that no live status line ever loses,
# small enough that a session whose status line stopped feeding loses the contest long
# before STATUSLINE_SESSION_KEEP_SEC would drop the row.
CONTEXT_STATUSLINE_LEAD_SEC = 120

# --- 2. OTLP receiver (POST /v1/metrics, POST /v1/logs)
# Doc: https://code.claude.com/docs/en/monitoring-usage
OTLP_MAX_BODY = 4 * 1024 * 1024
OTLP_COST_METRIC = "claude_code.cost.usage"          # unit USD
# The event name arrives as the `event.name` attribute ("api_error"); some collectors
# also carry it as the log record's `eventName`. Both spellings are accepted because
# either one is the producer telling us the same fact.
OTLP_ERROR_EVENT = "api_error"
OTLP_SESSION_ATTR = "session.id"
# OTLP aggregationTemporality: 1 = DELTA, 2 = CUMULATIVE. Delta is Claude Code's
# default and the two need OPPOSITE arithmetic - summing a cumulative counter
# double-counts every export, and taking the max of a delta counter under-reports.
# A receiver that assumes one reads plausible-looking wrong numbers, which is the
# worst failure mode a money display can have.
OTLP_TEMPORALITY_DELTA = 1
OTLP_TEMPORALITY_CUMULATIVE = 2
BURN_COST_SOURCE_OTLP = "otlp"
# Per-session error events are capped per export so one pathological batch cannot
# flood a session's 8-entry ring (and, through it, the history file).
OTLP_EVENTS_PER_EXPORT = 20
# Hard cap on the number of distinct CUMULATIVE series tracked (audit F4, v0.17.0). The
# series key is the data point's own attribute set, arriving over the unauthenticated
# POST /v1/metrics, so a batch of points each carrying a fresh attribute mints a fresh
# key; prune() only drops whole DAYS, so within today the dict grew without bound. Same
# class as the forecaster's key flood (F1), same remedy: least-recently-updated eviction.
# What makes eviction safe here and not merely bounded: a cumulative counter carries its
# RUNNING TOTAL, so an evicted series is restored in full by that series' very next
# export - the worst case is one interval reading low, never a permanently wrong number.
# (Delta points are the ones that could not survive this, and they are not series-keyed
# at all - they fold into _delta_by_day.) 512 is far above any real exporter's series
# count for one metric on one machine.
OTLP_MAX_CUMULATIVE_SERIES = 512
# Hard cap on the number of distinct DELTA day-buckets (CRB-b, 2026-08-28 audit). F4
# hardened only the cumulative sibling above; the delta path (folded into _delta_by_day,
# keyed by local day) had NO per-write cap, so a batch of delta points carrying forged
# timeUnixNano values spanning thousands of distinct days grew the dict without bound
# until prune() next ran. Same bounded-LRU remedy, with ONE difference that matters:
# a delta bucket is a RUNNING SUM with no series total to restore it, so eviction is
# permanent - which is why _evict_delta_locked protects TODAY'S bucket specifically
# (the only one cost_today reads), not merely the just-landed day the cumulative path
# spares. 512 is far above the two days prune() ever keeps.
OTLP_MAX_DELTA_DAYS = 512

# --- 3. tap-to-continue (POST /v1/action queue-continue, POST /v1/hook/stop)
CONTINUE_TTL_SEC = 600          # contract: a queued continue expires after 10 min
# Contract v0.12.0 §3 names these three buttons; `continuePrompts` in config.json adds
# to them. The queue accepts NOTHING ELSE, and that is a security property, not tidiness:
# the queued string is handed to the model as a prompt, and anything on this machine can
# POST to a loopback port. A whitelist is what keeps that surface to strings the operator
# chose. It also lets the history line carry the prompt text verbatim (below) without
# breaking the "history holds no free-form content" rule.
#
# SIX entries for three buttons, because the contract is ambiguous about which half of a
# button goes on the wire ("Continue / Run the tests / Commit + push buttons") and the
# widget resolved it the sensible way: the short LABEL is the button face and the FULL
# INSTRUCTION is the prompt. Measured in widget/scripts/sidecrab.js CONTINUE_DEFAULTS
# (2026-08-26) - it sends the instructions. A whitelist holding only the labels would
# 400 every tap and render "not available" on a feature that shipped working, so both
# forms are accepted. This costs nothing: all six are fixed strings, so the property
# that matters - no free-form text reaches a model prompt - is unchanged.
CONTINUE_PROMPTS_BUILTIN = (
    "Continue", "Keep going with what you were doing.",
    "Run the tests", "Run the tests and report the results.",
    "Commit + push", "Commit the changes and push.",
)
CONTINUE_PROMPT_MAX = 200       # bounds a config-supplied extra
CONTINUE_PROMPTS_CAP = 20       # bounds how many extras config may add
# Contract: crabd answers the Stop hook "within 2 s". This is a BUDGET the handler is
# measured against, not a sleep - draining the queue is a dict lookup under a lock.
STOP_HOOK_ANSWER_SEC = 2.0

# PINNED SHAPE - Stop-hook continue response.
# `continuationPrompt` / `continueConversation` (docs/spikes/reply-spike-2.md, read off
# the docs) are NOT in the shipped binary's hook-output schema at all - the first appears
# nowhere, the second only as an Agent SDK spawn option. Both would be silently ignored.
# The schema that ships accepts, verbatim:
#     { continue?: bool, suppressOutput?: bool, stopReason?: str,
#       decision?: "approve" | "block", reason?: str, systemMessage?: str,
#       terminalSequence?: str, hookSpecificOutput?: <union> }
# which leaves TWO shapes that both continue the session, and they differ only in how the
# CLI LABELS the text. v0.15.0 switched to the second. MEASURED in the shipped 2.1.246
# binary (2026-08-26), all four facts read off the same normalizer/turn loop:
#
#  1. WHY THE SWITCH. `decision:"block"` normalizes to `blockingError`, and the turn loop
#     pushes its string into the hook_errors array whose non-emptiness is the ONLY thing
#     that fires `Stop hook error occurred \xB7 ctrl+o to see`. The transcript then renders
#     it as `<hookLabel ?? "Stop"> hook error: <reason>`, and the MODEL is handed
#     `Stop hook blocking error from command: "<url>": <reason>`. Nothing failed; the
#     operator sees red and the model hedges about being nudged by an error. Measured in
#     a live session, docs/spikes/live-verify.md 2.3.
#  2. WHAT REPLACES IT. `hookSpecificOutput: {hookEventName:"Stop", additionalContext}` is
#     a real member of the union (`case "Stop": case "SubagentStop":` in the normalizer
#     lifts `additionalContext` out), described in the binary as "non-error feedback
#     delivered to the model; the conversation continues so the model can act on it" and
#     kept in a SEPARATE `hook_additional_context` field "so the sanctioned feedback
#     channel is not labeled an error". The model receives
#     `Stop hook additional context: <prompt>` instead.
#  3. IT STILL CONTINUES - the load-bearing half. Both branches of the turn loop push
#     their attachment onto the SAME messages array, and the caller's test for "force
#     another turn" is `blockingErrors.length > 0` on that array, not on the error
#     strings. So additionalContext takes the identical continuation path (and the same
#     CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=8 consecutive-block ceiling); only the labelling
#     differs. A prettier shape that let the session stop would have been a regression.
#  4. hookEventName IS CHECKED. The normalizer throws "Hook returned incorrect event
#     name: expected 'Stop' but got '<x>'" - so this constant is not cosmetic, and
#     SubagentStop would need its own value.
#
# Doc: https://code.claude.com/docs/en/hooks  (verified against CLI 2.1.246)
STOP_CONTINUE_HOOK_EVENT = "Stop"
# FALLBACK, deliberately retained and NOT wired. `decision:"block"` + `reason` is what
# shipped through v0.14.0 and is proven on a live 2.1.246 turn (live-verify.md 2.2); it is
# the shape to revert to if a future CLI drops or changes the Stop additionalContext
# member. Reverting = build the body from this instead, nothing else moves.
STOP_CONTINUE_DECISION = "block"


def stop_continue_body(prompt: str) -> dict:
    """The Stop-hook answer that carries a queued continue. See the constants above."""
    return {"hookSpecificOutput": {"hookEventName": STOP_CONTINUE_HOOK_EVENT,
                                   "additionalContext": prompt}}


def stop_continue_body_fallback(prompt: str) -> dict:
    """The pre-v0.15.0 shape. Kept executable so the fallback is a one-line swap at the
    call site rather than a comment someone has to re-derive under pressure."""
    return {"decision": STOP_CONTINUE_DECISION, "reason": prompt}


# An empty object is the documented no-op, and the CLI treats an empty BODY as one too
# ("HTTP hook returned empty body, treating as empty JSON object") - so a crabd that is
# down costs a session nothing.
HOOK_PASS_THROUGH: dict = {}

# --- 4. panel approvals (POST /v1/hook/permission)
# PINNED SHAPE - PermissionRequest hook response, read off the shipped 2.1.246 schema:
#     { hookEventName: "PermissionRequest",
#       decision: { behavior: "allow", updatedInput?, updatedPermissions? }
#             | { behavior: "deny", message?, interrupt? } }
# Two traps the published summary gets wrong and the binary settles:
#  - the field is `decision: {behavior: ...}`, NOT the PreToolUse-style
#    `permissionDecision: "allow"|"deny"|"ask"`. PreToolUse has that; PermissionRequest
#    does not, and a PermissionRequest hookSpecificOutput carrying it fails validation.
#  - there is NO "ask"/"pass_through" VALUE, and `decision` is REQUIRED once the
#    PermissionRequest hookSpecificOutput is present. The pass-through is therefore to
#    return no hookSpecificOutput at all - HOOK_PASS_THROUGH above - which is what makes
#    the terminal dialog appear exactly as it does today.
PERMISSION_HOOK_EVENT = "PermissionRequest"
PERMISSION_BEHAVIOR_ALLOW = "allow"
PERMISSION_BEHAVIOR_DENY = "deny"
PERMISSION_DENY_MESSAGE = "denied from the SideCrab panel"
# Contract: hold the response up to 55 s. Under the HTTP hook's own default timeout
# (600 s) with a wide margin, and under any sane proxy/keep-alive idle limit.
PERMISSION_POLL_SEC = 55
# The long poll is bounded TWICE. Once in time (above), and once in COUNT here: past
# this many concurrent holds a request is passed straight through to the terminal
# dialog instead of parking another thread. ThreadingHTTPServer gives every request its
# own thread, so a hold cannot block /v1/state - but unbounded holds would still let a
# pathological session turn the daemon into a thread farm, and the honest answer to
# "SideCrab is saturated" is the terminal dialog the operator already knows.
PERMISSION_MAX_PENDING = 8
# The panel-facing summary of what is being asked for, per tool. Served on /v1/state
# only, NEVER persisted: a Bash command line is content, and the history file's rule is
# event kind + session id + title + ts. The history lines carry the TOOL NAME alone.
PERMISSION_SUMMARY_KEYS = ("command", "file_path", "path", "url", "pattern", "prompt")
PERMISSION_SUMMARY_MAX = EVENT_MAX
PERMISSION_TOOL_MAX = 60
PERMISSION_EVENT_REQUESTED = "permission requested"
PERMISSION_EVENT_ALLOW = "approved from panel"
PERMISSION_EVENT_DENY = "denied from panel"
# Not a decision, but the operator has to be able to tell "I did not tap in time" from
# "the panel never saw it" - both of which otherwise look like a terminal dialog.
PERMISSION_EVENT_TIMEOUT = "permission passed through"
# A-10 (v0.26.0). The permission stand-down (clear_permission -> _stand_down) used to write
# NO ring event, so an alert being dropped left no trace anywhere - in `events` or in
# history.jsonl - which is exactly what would make an A-01/A-02-class mis-clear undiagnosable
# in the field. The sibling in-app clear (note_activity) already persists its own event; this
# is the permission path's equivalent.
PERMISSION_CLEARED_EVENT = "permission alert cleared"
# The card's `question` while a hold is open (v0.20.0). Word-for-word the message the CLI
# puts on its OWN Notification for the same dialog (contract §1, measured), and that is
# load-bearing rather than cosmetic: the two hooks fire within a second of each other, and
# record()'s new-question test compares TEXT - so an identical string is what stops one
# permission prompt escalating the card twice.
PERMISSION_QUESTION = "Claude needs your permission to use %s"
# The tracker states a PermissionRequest may raise `needs_input` FROM (v0.20.0). None is
# a session crabd has seen no state-moving hook for, "working" is a live turn - the only
# two in which a dialog can actually be open. See note_permission for why the others are
# refused. "idle" joined in v0.28.2 with SessionStart's remap: a dialog normally rides a
# UserPromptSubmit-armed turn, but an SDK/headless run can open one from a just-started
# session, and refusing THAT alert would silence the panel's loudest feature.
PERMISSION_ALERT_FROM = frozenset({None, "working", "idle"})

# Cadence of the v0.12.0 expiry sweep. Well under CONTINUE_TTL_SEC so an expiring
# continue is dropped promptly, and cheap enough (three dict scans) to be unmeasurable.
EXPIRY_POLL_SEC = 30.0


# --------------------------------------------------- v0.19.0: clearing a needs_input
# THE GAP THIS CLOSES (operator-reported, 2026-08-27). The Notification hook is what
# sets `needs_input`, and Claude Code fires it for BOTH shapes of waiting: an idle
# prompt AND a permission dialog ("Claude needs your permission to use Bash" is a real
# measured message). Only a later hook could clear it, and the two ways the operator
# most often answers IN THE APP fire no hook at all:
#   - Allow/Deny on the terminal permission dialog. The PermissionRequest hook already
#     returned its pass-through when the dialog appeared; the CLI emits nothing at
#     decision time. The next hook is `Stop`, which can be an hour of tool work away.
#   - Picking an option on an AskUserQuestion sheet. That answer is a tool_result, not
#     a prompt, so `UserPromptSubmit` never fires.
# In both cases the panel kept alerting - and ESCALATING (the widget deepens at 5 min
# and 15 min unacked) - on a question that was answered seconds in.
#
# THE CLEARING SIGNAL IS THE TRANSCRIPT'S OWN TURN CLOCK: the timestamp of the newest
# assistant usage record in the session's MAIN transcript (FileFacts.context_ts). A
# usage record is a COMPLETED model round-trip, which is the one thing that cannot
# happen while the operator is still being waited on - the model is blocked. So:
#   - it never fires early. A standing question writes no usage record, ever.
#   - it fires for every answer path, because they all end in the model being called
#     again: an approved tool's result, a denied tool's result, a picked option, a
#     typed prompt (which UserPromptSubmit clears first anyway).
#   - it costs NOTHING new on the wire. crabd already parses these records for burn and
#     contextTokens; this reads a number that was already there.
# SUBAGENT files are deliberately excluded (see _blank_session's turn_ts): a background
# subagent finishing its own work while the main session waits is not an answer, and
# aggregating its records would clear a question that genuinely still stands.
#
# REJECTED, and why - both would close the same gap and neither earns its cost:
#   - PreToolUse/PostToolUse as activity pings. Precise, but they put an HTTP round trip
#     in front of EVERY tool call in every session: the highest-frequency hook surface
#     the product could have, on a host whose loopback drops SYN-ACKs. The transcript
#     already carries the same evidence on a path crabd polls anyway.
#   - OTLP activity. MEASURED in this repo, not assumed: setup/*.ps1 sets no OTEL_*
#     variable, so a default install emits no OTLP at all - a clearing signal that is
#     absent on the maintainer's machine is not a fix. And crabd maps OTLP_SESSION_ATTR at exactly
#     ONE site (OtlpReceiver.ingest_logs, `api_error` events); cost metric points are
#     keyed by attribute-set string and never resolved to a session, so per-session
#     "token activity" does not exist here. note_external's docstring already forbids
#     telemetry moving the state machine, and an api_error is evidence of a FAILING
#     request - the opposite of the block being released.
NEEDS_INPUT_CLEARED_EVENT = "answered outside the panel"
# The transcript's record timestamps and `since` (crabd's clock at hook receipt) are two
# clocks on one machine, and the record that CAUSED the question is written just BEFORE
# the Notification reaches crabd - so the honest ordering already has it behind `since`.
# The grace absorbs the disagreement anyway (a delayed transcript flush, a whole-second
# `timestamp`), and it is cheap: the record that actually clears is a LATER round-trip,
# which is seconds of model latency past the answer, not milliseconds.
NEEDS_INPUT_ACTIVITY_GRACE_SEC = 5
# A-05 (v0.26.0). `needs_input` keeps its prune EXEMPTION - a question waits even when the
# transcript goes quiet, and a genuinely recent waiting prompt (an operator's real question
# at 2am) must NEVER be evicted. But the exemption used to be TOTAL: no count cap, no age
# ceiling, so a hook flood or a set of abandoned questions grew the tracker, `_titles` and
# the served `sessions` array without bound (every needs_input row is also served on every
# poll, forever). The bound is deliberately generous and evicts OLDEST-FIRST so the healthy-
# night rule holds: a row is eligible only once it is older than the age ceiling, and past
# the count cap the OLDEST-by-`at` rows go first (an acked/abandoned row sorts out ahead of
# a fresh waiting one because a fresh one has a newer `at`). Both are far beyond any real
# waiting window, so a real prompt is untouched and only runaway growth is trimmed.
NEEDS_INPUT_MAX_AGE_SEC = 36 * 3600   # a needs_input row past 36h of no activity is stale
NEEDS_INPUT_MAX_ROWS = 512            # LRU ceiling on live needs_input rows, oldest evicted
# _resolve's "done unless reactivated" grace, named in v0.20.0 (it was a bare `+ 2`).
# SEPARATE from the constant above and deliberately smaller: that one absorbs two clocks
# disagreeing about an event that has ALREADY happened, this one absorbs the transcript
# writes the END of a turn itself provokes. 2 -> 120 in v0.28.2, measured live 2026-09-01:
# the CLI keeps writing AFTER the Stop hook - last-prompt/custom-title records, the
# ASYNC ai-title (its own model call, landing seconds to a minute later), subagent
# stragglers - and any of them past the grace flipped `done` back to `working`. A real
# resume does not need this heuristic to be fast: UserPromptSubmit re-arms `working`
# through the front door; reactivation only covers a resume whose hooks were LOST, and
# two minutes of `done` before that rare case corrects is the cheaper error.
DONE_REACTIVATION_GRACE_SEC = 120
# Hook events after which a still-parked permission hold is certainly stale: the turn
# has ended or a new one has begun, so the dialog it belongs to was answered in the app
# (or abandoned) and the Approve/Deny buttons on the card are offering a decision that
# has already been made. SubagentStop is deliberately ABSENT - a background subagent
# finishing says nothing about the main thread's dialog.
PERMISSION_STALE_EVENTS = frozenset({"Stop", "UserPromptSubmit", "SessionEnd"})


# ------------------------------------------------------ v0.20.0: never a 500 on /v1/state
# THE CRASH THIS CLOSES (observed once in production, 2026-08-27 ~10:50): the FIRST
# GET /v1/state about 2 s after crabd started raised out of the do_GET branch, and the
# operator got a 500 on a card that had been fine a second earlier. Three cold starts did
# not reproduce it, so the fix is not "the one line that threw" - it is the three seams
# that could produce it, each closed so the honest-failure rule holds by construction:
# a data shape crabd cannot read is SKIPPED and logged, never served as a 500.
#
# MEASURED (repro harness, 2026-08-27): ten distinct record shapes crash the transcript
# parser outright - a non-dict `message` or `usage`, a usage counter that is a dict, a
# list, a string, an Infinity or a NaN. Any ONE of them anywhere under ~/.claude/projects
# aborted store.scan() and therefore the WHOLE build, so a single unreadable line in one
# session's transcript took every session's card down with it. The fixtures cover none of
# these shapes, which is exactly what "a shape the fixtures don't cover" means.
#
# The transition IS ordinary and it IS bounded: a skipped record is skipped for good (the
# read offset has already moved past it), and the log is once per crabd lifetime per seam,
# because a poisoned transcript would otherwise print on every 2 s pass forever.
TRANSCRIPT_SKIP_LOG_KEY = "transcript-record"
TRANSCRIPT_FILE_LOG_KEY = "transcript-file"
STATE_SERIALIZE_LOG_KEY = "state-serialize"
STATE_BUILD_LOG_KEY = "state-build"
GET_HANGUP_LOG_KEY = "get-hangup"
PANEL_READ_LOG_KEY = "panel-read"
PANEL_TOO_BIG_LOG_KEY = "panel-too-big"
PANEL_DIR_LOG_KEY = "panel-dir"
ORIGIN_RECORD_LOG_KEY = "origin-record"
CLI_CREDENTIALS_LOG_KEY = "cli-credentials"
LIMITS_TOKEN_LOG_KEY = "limits-token"
# The 503 body for a /v1/state that has no snapshot to serve YET. Distinct from every
# other error body in this file so a reader can tell "crabd is still coming up" from
# "crabd refused you" (403) and from "no such path" (404).
STATE_NOT_BUILT = b'{"error":"state not built yet"}'
# Bound on the once-log key set. The keys are literals in this file, so this can only be
# reached by a future caller passing a computed key - and a growing set in a process that
# runs for weeks is the leak this cap exists to refuse.
LOG_ONCE_MAX_KEYS = 64
# Depth bound for the sanitising second pass at the serializer. Deep enough for anything
# the contract can produce (the deepest real path is limits.extra[].label at 3) and
# shallow enough that a self-referential structure cannot recurse the daemon to death.
JSON_SAFE_MAX_DEPTH = 12


# ---------------------------------------------------------------- v0.14.0 constants
# Live-fire hardening of the paths wired on 2026-08-26. Everything below was MEASURED
# against a running crabd on a test port, not reasoned about - both bounds here fixed a
# reproduced crash, not a hypothetical one.

# `datetime.fromtimestamp` is not total: on this Windows host it raises OSError for any
# epoch below 0 and OverflowError past the platform time_t. MEASURED 2026-08-26 - a
# statusline document carrying `resets_at: 1e30` walked _parse_ts -> _utc_iso and put an
# OverflowError traceback on the socket AFTER the 204 had gone out. The bound belongs
# HERE, in the parser every untrusted timestamp passes through (statusline resets_at,
# the OAuth endpoint's windows, transcript timestamps, the history file's ts), because
# fixing it at any one call site leaves the other three live.
TS_MIN_EPOCH = 0.0                 # 1970-01-01; fromtimestamp raises below it
TS_MAX_EPOCH = 32503680000.0       # 3000-01-01; far past anything real, inside time_t

# Hard ceiling on a request body, applied while READING rather than after. The old code
# did `rfile.read(Content-Length)` with no cap and checked the size afterwards, so a
# header claiming 900 MB made crabd try to buffer 900 MB and BLOCK - measured
# 2026-08-26, the hook POST never got an answer at all. Set to the largest per-endpoint
# cap (OTLP's 4 MB) so the per-endpoint checks below it still decide what is oversized;
# this only bounds the buffer. One byte over is deliberate: the endpoint's own
# `len(raw) > CAP` test still has to see that the body exceeded its cap.
MAX_BODY_BYTES = OTLP_MAX_BODY
# Bytes of an over-cap body we will read and throw away to keep the connection framed.
# Past this the connection is closed instead - draining an unbounded body to preserve
# keep-alive is the same denial of service the cap exists to stop.
BODY_DRAIN_MAX = 8 * 1024 * 1024
# Per-socket timeout. BaseHTTPRequestHandler catches TimeoutError around the whole
# request, so this is what turns "a client sent Content-Length: 900000000 and then went
# quiet" from a parked thread into a discarded connection. Far above any real loopback
# body transfer, far below the HTTP hook's own 600 s timeout, and it does NOT bound the
# PERMISSION_POLL_SEC hold: that is a response delay, not a socket read.
SOCKET_TIMEOUT_SEC = 30.0
# The ONE body the Origin gate answers with, reads and writes alike (SEC-1 + SEC-4).
# Shared rather than inlined twice so a cross-site GET and a cross-site POST cannot drift
# into telling an attacker which of the two they hit.
CROSS_SITE_REFUSED = b'{"error":"cross-site request refused"}'
# v0.31.0. EVERY POST carries this header, with any non-empty value; a POST without it is
# refused before it is routed. It is not authentication - the value is never read - it is
# a NON-SIMPLE request header, and that is the whole mechanism: a CORS-simple POST needs
# no permission from crabd, while one carrying a custom header must be preflighted, and
# do_OPTIONS below hands the permission only to an origin the allowlist already trusts
# (never to `null`). So the forged-`null` page keeps its reads and loses its writes.
# A DISTINCT body from CROSS_SITE_REFUSED on purpose, the opposite way round from that
# constant's sharing rule: the two refusals are told apart by an OPERATOR wiring a hook
# up, and "cross-site request refused" for a curl on the command line sent people
# looking at CORS for an hour.
# v0.31.0. The DNS-rebinding refusal. Its own body, because the two other 403s answer a
# different question: "you are cross-site" and "you sent no panel header" are both about
# the CALLER, and this one is about the caller's belief that it is talking to
# evil.example when the socket is loopback.
HOST_NOT_ALLOWED = b'{"error":"host not allowed"}'
PANEL_HEADER = "X-SideCrab-Panel"
PANEL_HEADER_REQUIRED = b'{"error":"panel header required"}'
# The panel crabd serves on / (v0.31.0). A module GLOBAL read per request, like every
# other path here, so a test can repoint it at a temp tree; CRABD_PANEL_DIR is for
# running the daemon against a panel build that is not the one beside it.
PANEL_DIR = Path(os.environ.get("CRABD_PANEL_DIR")
                 or Path(__file__).resolve().parent.parent / "widget")
# By SUFFIX, never by sniffing the bytes - and every static reply carries
# X-Content-Type-Options: nosniff, so what is declared here is what the browser uses. An
# unrecognised suffix is a download, not a guess: crabd serves a directory whose contents
# it does not enumerate, and a wrong `text/html` on a file somebody dropped in there is
# the one mistake that turns a static server into a scripting hole.
PANEL_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}
PANEL_CONTENT_TYPE_DEFAULT = "application/octet-stream"
# A ceiling on what ONE static reply may read into memory. The whole shipped panel is
# under a megabyte, so this is not a limit anybody meets - it is a limit on a directory
# an operator can point anywhere with CRABD_PANEL_DIR, or drop a file into. Without it a
# big enough file is a MemoryError, and MemoryError is NOT an OSError: it escapes the
# narrowed catch on the read, escapes do_GET's `except OSError`, and lands in
# socketserver's handle_error as a traceback on a daemon that is now also short of
# memory. Checked by stat BEFORE the read, so the bytes are never allocated.
PANEL_MAX_BYTES = 64 * 1024 * 1024
# One 404 body for a mistyped endpoint and for a file that is not the panel's to serve.
# Shared so the two cannot drift into telling a prober which of them it hit.
NOT_FOUND = b'{"error":"not found"}'
# A session id long enough to be a memory-growth vector rather than an identifier. Real
# ones are 36-char UUIDs; this is generous enough that no legitimate id is refused.
SESSION_ID_MAX = 200


# ---------------------------------------------------------------- v0.24.0 constants
# The panel diagnostics log channel (POST/GET /v1/panel-log). The widget renders inside
# iCUE on the Xeneon Edge, where no devtools can be attached - a console.log has nowhere
# to go, so the widget ships short lines here instead and a maintainer reads them over
# HTTP. These four bounds are the entire flood posture: there is no rate limit because
# the ring itself is the bound.
PANEL_LOG_MAX_LINES = 500          # the ring; oldest evicted first, counted in droppedTotal
PANEL_LOG_MAX_PER_POST = 50        # lines past this in ONE body are dropped, not a 400
PANEL_LOG_MAX_LINE_CHARS = 300     # a longer line is TRUNCATED, not rejected
PANEL_LOG_MARKER = "[panel]"       # the short client marker inside the server-side prefix
# SEC-d (2026-08-28 audit): interior C0/C1 control bytes are stripped from every stored
# line. `.strip()` only trims edge whitespace, so an ANSI/ESC-laden line stored verbatim
# is JSON-safe (dump_state escapes it) but hands raw control bytes to a maintainer who
# echoes the line to a terminal. Same posture as the notifier's XML control-strip: keep
# printable text and the ordinary whitespace (tab/LF/CR), drop C0 (0x00-0x1F less those
# three), DEL (0x7F) and C1 (0x80-0x9F). Character-class only, so unicode above 0x9F -
# accented text, emoji - is untouched.
_PANEL_LOG_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
# ONE 400 body for every way this endpoint's input can be wrong. Shared rather than
# branched so a caller cannot use the error text to probe which sub-rule it tripped, and
# so the widget lane has exactly one non-2xx shape to render.
PANEL_LOG_BAD_BODY = b'{"error":"lines must be an array of 1..50 strings"}'

# ------------------------------------------------------------ v0.25.0 origin recorder
# The distinct (Origin, source) pairs seen on the request paths, exposed read-only at
# GET /v1/health.originsSeen (ORIGIN-REC, 2026-08-28 audit). It is the passive enabler
# for the SEC-a allowlist fix: the legitimate QtWebEngine widget and a forged-null
# attacker are indistinguishable to _is_web_origin, so the widget's TRUE origin has to be
# MEASURED before it can be allowlisted. This records what actually arrives, so that
# origin can be read remotely from the widget's own live polling instead of by standing
# at the glass. Absent Origin is recorded as the literal "<absent>". LRU-capped so a
# flood of random forged origins cannot balloon it - the same posture as the panel ring.
#
# v0.27.0: MULTIPLE local sources send NO Origin - the notifier polling /v1/state, a
# maintainer's curl health checks, AND possibly the widget - so keying on Origin alone
# collapsed them all into one uninformative "<absent>" bucket (measured live 2026-08-28:
# originsSeen was ONLY {"origin":"<absent>"}). We now also classify a coarse `source` from
# the User-Agent and key on the DISTINCT (origin, source) pair, so the QtWebEngine widget
# is separable from python-urllib and curl even when all three send no Origin. The cap is
# raised to accommodate the extra dimension.
ORIGIN_RECORDER_MAX = 48
ORIGIN_ABSENT = "<absent>"
# Raw User-Agent kept (truncated) per entry: the exact UA string is itself evidence of
# WHICH build is polling. Truncated so a hostile UA cannot bloat the health payload.
ORIGIN_UA_MAX = 80
# Substrings that mark a browser / embedded-webview UA. The Xeneon-Edge widget runs in
# QtWebEngine (Chromium), so it matches on "qtwebengine"/"chrome"/"applewebkit". Matched
# case-insensitively. See _classify_ua_source.
_UA_BROWSER_MARKERS = ("mozilla", "chrome", "qtwebengine", "applewebkit")


def _classify_ua_source(user_agent) -> str:
    """Coarse SOURCE bucket for a recorded request, derived from its User-Agent:
    "browser" (a browser / embedded-webview UA - the QtWebEngine widget lands here),
    "local"  (any other non-empty UA - python-urllib, curl, ...), or
    "none"   (no User-Agent header at all).

    ⚠ SECURITY: the User-Agent is ATTACKER-CONTROLLED. This classification is DIAGNOSTIC
    ONLY - it exists solely to help a human read the recorder, and MUST NEVER feed the
    origin gate (_is_web_origin) or any decision path. The CSRF gate stays exactly as it
    is: origin-based. A forged UA can only mislabel a row in a health report a human reads;
    it can change no security outcome. See OriginRecorder."""
    if not isinstance(user_agent, str) or not user_agent.strip():
        return "none"
    ua = user_agent.lower()
    if any(marker in ua for marker in _UA_BROWSER_MARKERS):
        return "browser"
    return "local"


# --------------------------------------------------------------------------- utils

_LOG_ONCE_SEEN: set[str] = set()
_LOG_ONCE_LOCK = threading.Lock()


def _log_once(key: str, message: str) -> None:
    """One honest stderr line the FIRST time a swallowed failure happens, then silence.

    Every catch added in v0.20.0 reports through here. A swallowed exception that logs
    NOTHING is the failure mode the honest-failure rule exists to forbid; one that logs
    on every pass is a 2 s heartbeat of noise for a transcript line that will never be
    read again, and noise is how a real signal gets ignored.
    """
    with _LOG_ONCE_LOCK:
        if key in _LOG_ONCE_SEEN or len(_LOG_ONCE_SEEN) >= LOG_ONCE_MAX_KEYS:
            return
        _LOG_ONCE_SEEN.add(key)
    print(message, file=sys.stderr, flush=True)


def _as_count(value) -> int:
    """A usage counter off an untrusted transcript record, as a non-negative int.

    MEASURED: the bare `int(usage.get(...) or 0)` this replaces raised five different
    ways on five different shapes - TypeError on a dict or a list, ValueError on "twelve"
    and on NaN, OverflowError on Infinity - and every one of them aborted the whole scan.
    A counter crabd cannot read is 0, which under-reports burn by one record; the
    alternative was serving no document at all.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return max(0, int(value))


def _finite_number(value) -> float | None:
    """A JSON number that is really a number, as a float - or None.

    THE TWO SHAPES IT REFUSES, and neither is hypothetical (CD-10, measured
    2026-08-27 against hand-edited config and a crafted /v1/statusline POST):

      - bool. `True` is an int to isinstance, so an `isinstance(x, (int, float))`
        guard passes it and `float(True)` is 1.0 - which LimitsReader._window then
        served as a window 100% full. A `true` in a numeric slot is a typo, not a
        reading, and gauging it is worse than showing an em-dash.
      - NaN / Infinity. `json.loads` produces both from perfectly valid-looking JSON
        (`1e309` -> inf), and they reach `int()` as OverflowError/ValueError. The
        clamping call sites turned them into a fabricated 0% or 100% instead, which
        is the same lie by a quieter route.

    The guard belongs at the PARSE BOUNDARY - one function every untrusted numeric
    field passes through - for the reason TS_MIN_EPOCH gives above it: fixing one
    call site leaves the others live.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _pct(value) -> float | None:
    """A percentage the way the served document wants one: finite, 0..100, 1 decimal.

    The clamp is deliberate and narrow. It exists for float noise and for a counter
    that reads a hair past its own total, NOT to make an unreadable value presentable -
    `_finite_number` refuses NaN/Infinity/bool first, so garbage arrives here as None
    and leaves as None rather than as a plausible-looking 0.0 (CD-10's lesson).
    """
    value = _finite_number(value)
    if value is None:
        return None
    return round(min(max(value, 0.0), 100.0), 1)


def _positive_int(value) -> bool:
    """A whole number above zero, and not a bool. `True` is an int in Python and would
    pass every arithmetic check downstream while meaning nothing at all."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _gb(value) -> float | None:
    """Bytes as GiB, 1 decimal. Negative is not a size, so it is None, not 0.0."""
    value = _finite_number(value)
    if value is None or value < 0:
        return None
    return round(value / HOST_BYTES_PER_GB, 1)


# The context-window marker Claude Code may append to a model id - "claude-opus-5[1m]",
# "claude-sonnet-4-6[200k]". Same expression the widget has parsed since v0.22.0
# (MODEL_CTX_RE in widget/scripts/sidecrab.js); k and m both, so a future [500k] needs no
# code change on either side.
MODEL_WINDOW_MARKER_RE = re.compile(r"\[(\d+(?:\.\d+)?)\s*([kKmM])\]")


def _marker_window(model) -> int | None:
    """The window size the FEED stated in the model string, or None.

    This is a SESSION-specific fact - the CLI writes the marker for the window that
    session is actually running - which is why it outranks the model catalog, whose
    number is a property of the model in general (see StateBuilder._context_window).
    """
    if not isinstance(model, str):
        return None
    match = MODEL_WINDOW_MARKER_RE.search(model)
    if match is None:
        return None
    # float(), not _finite_number(): the group is a STRING, which that guard refuses by
    # design (it is the JSON parse boundary). The pattern already proves the digits, so
    # the only failure left is a digit run long enough to overflow to inf - which int()
    # raises on, and this function is on the build path.
    tokens = float(match.group(1)) * (1_000_000 if match.group(2) in ("m", "M") else 1_000)
    if not math.isfinite(tokens) or tokens <= 0:
        return None
    return int(tokens)


def _model_base_id(model) -> str | None:
    """The API id inside a served model string: the marker stripped, nothing else
    changed. `model` is served VERBATIM (CON-b) and must stay that way - this is a
    lookup key, never a rewrite of the field."""
    if not isinstance(model, str):
        return None
    base = MODEL_WINDOW_MARKER_RE.sub("", model).strip()
    return base or None


def _json_safe(value, depth: int = 0):
    """Coerce a structure into something json.dumps can express. The SLOW path - only
    the sanitising second pass in `dump_state` calls it, never the ordinary poll.

    Non-finite floats become null rather than the bare `NaN` / `Infinity` tokens
    json.dumps emits by default: those are not JSON, and the widget's JSON.parse dead-
    feeds the panel on them. Anything else unknown becomes its repr, which is a string an
    operator can read in a bug report - not a key that silently vanished.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if depth >= JSON_SAFE_MAX_DEPTH:
        return repr(value)
    if isinstance(value, dict):
        return {(k if isinstance(k, str) else repr(k)): _json_safe(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v, depth + 1) for v in value]
    return repr(value)


def dump_state(state) -> bytes:
    """The ONE serializer for a served document (v0.20.0).

    `allow_nan=False` is the point of the fast path: json.dumps' default is to emit bare
    `NaN` / `Infinity`, which every JSON parser downstream refuses - so the default turns
    one poisoned float into a dead panel that reports no error anywhere. Refusing it
    turns the same float into the sanitising pass below, which serves null for it and
    every other key intact. The C encoder is still used (allow_nan is a flag on it, not a
    fallback to the Python one), so the ordinary poll costs nothing.
    """
    try:
        return json.dumps(state, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        _log_once(STATE_SERIALIZE_LOG_KEY,
                  f"crabd: the state document carried a value JSON cannot express "
                  f"({type(exc).__name__}); serving it sanitised")
        return json.dumps(_json_safe(state), allow_nan=False).encode("utf-8")


def _utc_iso(epoch: float) -> str:
    """Total by construction. _parse_ts already refuses an out-of-range epoch, so the
    clamp here can only fire on an internal number - but it is what makes the FUNCTION
    unable to raise, and every endpoint in this file formats a timestamp somewhere."""
    try:
        clamped = min(max(float(epoch), TS_MIN_EPOCH), TS_MAX_EPOCH)
    except (TypeError, ValueError):
        clamped = TS_MIN_EPOCH
    if clamped != clamped:      # NaN survives min/max; json.dumps would emit bare NaN
        clamped = TS_MIN_EPOCH
    return datetime.fromtimestamp(clamped, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value) -> float | None:
    """Transcript timestamps ('2026-08-26T17:39:25.954Z') and endpoint reset times.

    Out of TS_MIN_EPOCH..TS_MAX_EPOCH is None, NOT a clamped number: this is the parser
    for values that arrive from a status line document, the OAuth endpoint and the
    history file, and "the producer sent a timestamp we cannot represent" is the same
    fact as "the producer sent no timestamp". Clamping instead would put 1970 on a reset
    gauge, which reads as a real reading. Every caller already handles None.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Epoch seconds vs milliseconds: anything past year ~2286 in seconds is ms.
        epoch = float(value) / 1000.0 if value > 1e11 else float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            epoch = datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    else:
        return None
    if epoch != epoch or not (TS_MIN_EPOCH <= epoch <= TS_MAX_EPOCH):
        return None
    return epoch


def _session_id(payload) -> str | None:
    """The session id out of a hook / statusline body, or None (v0.14.0).

    One reader for every untrusted body that names a session, because they all have to
    agree on what an id IS - and because every one of these ids becomes a DICT KEY in a
    table that lives for hours. Real ids are 36-char UUIDs; SESSION_ID_MAX is generous
    enough to refuse nothing real and small enough that a producer sending a megabyte
    string cannot grow the session table one POST at a time.
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get("session_id") or payload.get("sessionId")
    if not isinstance(value, str) or not value or len(value) > SESSION_ID_MAX:
        return None
    return value


def _trim(text, limit: int) -> str | None:
    if not isinstance(text, str):
        return None
    flat = " ".join(text.split())
    if not flat:
        return None
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _cwd_title(cwd) -> str | None:
    """Last path component of a session's cwd, or the last TWO joined with '/' when the
    last is generic (CWD_TITLE_GENERIC_TAILS). None when the path has no component of
    its own - a drive root, a bare UNC share, or no cwd at all.

    PureWindowsPath, not Path: it parses '/' and '\\' alike, so one implementation
    reads a Windows cwd, a POSIX cwd and a UNC path the same way on any host running
    crabd. os-native Path would silently keep 'C:\\Dev\\acme' whole on POSIX.
    """
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    pure = PureWindowsPath(cwd.strip())
    tail = pure.name
    if not tail:
        return None
    if tail.lower() in CWD_TITLE_GENERIC_TAILS:
        parent = pure.parent.name
        if parent:
            return _trim(f"{parent}/{tail}", TITLE_MAX)
    return _trim(tail, TITLE_MAX)


def _trim_question(text) -> str | None:
    """Trim from the FRONT, not the back: the '?' lives at the end and a question with
    its question mark cut off reads as a statement on the panel."""
    if not isinstance(text, str):
        return None
    flat = " ".join(text.split())
    if not flat:
        return None
    return flat if len(flat) <= QUESTION_MAX else "…" + flat[-(QUESTION_MAX - 1):]


def _parse_hhmm(value) -> int | None:
    """'22:00' -> minutes since local midnight. Anything else -> None (quiet stays off)."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return hour * 60 + minute


def quiet_override(config, now: float) -> dict | None:
    """The v0.23.0 `quietOverride` off config.json, normalized - or None.

    None covers all four of "no key", "malformed", "unknown mode" and EXPIRED, and the
    callers want exactly that: an expired override is the same fact as no override, so
    there is one reading of it and no branch anywhere else can honour a stale one. It is
    read on the builder's own pass rather than retired by a timer - the override dies of
    the clock, and a timer that must fire for it to end is a timer that can fail to.

    `until <= now` is EXPIRED, not still-running: the operator asked for N minutes and
    the Nth minute is over. Half-open the same way the quiet WINDOW is (end exclusive),
    so the two never disagree about a boundary minute.

    `until` is re-formatted from the parsed epoch rather than echoed, so a hand-edited
    "+00:00" offset or a fractional second is served in the one shape the contract names.
    """
    raw = config.get(QUIET_OVERRIDE_KEY) if isinstance(config, dict) else None
    if not isinstance(raw, dict) or raw.get("mode") not in QUIET_OVERRIDE_MODES:
        return None
    until = _parse_ts(raw.get("until"))
    if until is None or until <= now:
        return None
    return {"mode": raw["mode"], "until": _utc_iso(until)}


def quiet_state(config, now: float) -> dict | None:
    """Contract's top-level `quiet`. None when unconfigured or unparseable - never a
    fabricated window. An overnight range (start > end) wraps across midnight.

    `active` is THE EFFECTIVE ANSWER (v0.23.0), schedule and override resolved together
    here and nowhere else. Every consumer - the widget's dim and glow, the notifier's
    four suppression sites, the crab's nightcap - reads this one boolean, so an operator
    tapping "quiet for 2h" on the panel reaches all of them without any of them learning
    what an override is.

    An override with NO schedule configured still produces a block, with `start`/`end`
    null: "quiet is on until 21:40, and there is no window" is a fact worth serving, and
    the alternative - null the whole block, the way an unconfigured schedule is nulled -
    would make the tap do visibly nothing on the very install most likely to use it.
    """
    override = quiet_override(config, now)
    hours = config.get("quietHours") if isinstance(config, dict) else None
    start = _parse_hhmm(hours.get("start")) if isinstance(hours, dict) else None
    end = _parse_hhmm(hours.get("end")) if isinstance(hours, dict) else None
    if start is None or end is None:
        if override is None:
            return None
        return {"active": override["mode"] == "on", "start": None, "end": None,
                "override": override}
    local = datetime.fromtimestamp(now)
    minute = local.hour * 60 + local.minute
    if start == end:
        active = False  # zero-length window; "always quiet" is not expressible here
    elif start < end:
        active = start <= minute < end
    else:
        active = minute >= start or minute < end
    block = {"active": active,
             "start": "%02d:%02d" % divmod(start, 60),
             "end": "%02d:%02d" % divmod(end, 60)}
    if override is not None:
        # The override WINS in both directions. "off" suppressing a live schedule window
        # is the half that is easy to drop and the half the operator notices: it is the
        # "I am working through the night, stop dimming the panel" tap.
        block["active"] = override["mode"] == "on"
        block["override"] = override
    return block


def _toast_seconds(raw) -> int | None:
    """A usable seconds value off hand-edited config, or None.

    Mirrors notifier/sidecrab_toast.py `_threshold` DELIBERATELY, including its tolerance
    of a float and its acceptance of values outside the /v1/config bounds: this function
    answers "what will the notifier use", not "what would the endpoint have accepted". A
    hand-edited 10 is what the notifier honours, so 10 is what the panel must display.
    bool first - True is 1, and a `true` here is a typo, not a threshold.

    CD-10: through _finite_number, so a hand-edited `1e309` is None rather than an
    OverflowError out of `int()`. This runs inside toast_block on EVERY build, so
    that one character in config.json stopped every state refresh - startup served
    an empty document and a running crabd froze on its last snapshot.
    """
    value = _finite_number(raw)
    if value is None:
        return None
    value = int(value)
    return None if value < 0 else value


def toast_block(config) -> dict:
    """Contract's top-level `toast` (v0.18.0) - the toast settings the notifier is running
    on, echoed so the widget's settings sheet can DISPLAY them.

    Why it exists at all: /v1/config is POST-only and the widget cannot read config.json,
    so before this the sheet had no way to show a hand-edited value - it kept a
    touched-latch and rendered nothing. The feed is the only channel.

    `thresholdSec` and `enabled` are ALWAYS present, falling back to the notifier's shipped
    defaults, because both are required members of the config block: absent means the
    notifier is running on 120/true, which is a fact worth serving, not an unknown.

    `approvalThresholdSec` is present ONLY when the on-disk config carries a usable one,
    and NO DEFAULT IS EVER INVENTED FOR IT. That asymmetry is the entire point: the key is
    OPTIONAL, v0.16.0's preserve-on-omit work exists so an unset key stays unset, and a
    feed that answered 20 would be claiming a setting the operator never made - which the
    widget would then latch and write back, materializing it for real. Absent here means
    "not set on disk"; what the notifier falls back to is the notifier's business to know.
    An unusable value (wrong type, negative) is omitted for the same reason - it is not the
    operator's value either.
    """
    block = config.get("toast") if isinstance(config, dict) else None
    if not isinstance(block, dict):
        block = {}
    threshold = _toast_seconds(block.get("thresholdSec"))
    enabled = block.get("enabled")
    served = {
        "thresholdSec": CONFIG_TOAST_DEFAULT_SEC if threshold is None else threshold,
        "enabled": enabled if isinstance(enabled, bool) else CONFIG_TOAST_DEFAULT_ENABLED,
    }
    approval = _toast_seconds(block.get("approvalThresholdSec"))
    if approval is not None:
        served["approvalThresholdSec"] = approval
    return served


def budget_target(value) -> int | None:
    """The ONE parser for a `budget` block - shared by the /v1/config validator and by
    the served `burn.budget` below. Sharing it is the point: config.json is hand-editable,
    so without a single parser a value the endpoint refuses could still be served (or
    worse, divided by), and the two halves would disagree about what a budget is.

    Strict, and None on anything else: an unknown extra member is a different shape than
    the contract's. The range check doubles as the divide-by-zero guard - `target`
    reaches a division downstream.

    The bool guard is belt-and-braces, NOT the thing that rejects `true` today: bool
    subclasses int, but True is 1 and False is 0, so the range below already refuses
    both. Measured by mutation 2026-08-26 - deleting the isinstance line changes no
    test. It stays because it is the line that would still hold if the floor ever moved
    to 1, and because every other validator here reads the same way.
    """
    if not isinstance(value, dict) or set(value) != {"dailyOutputTokens"}:
        return None
    target = value["dailyOutputTokens"]
    if isinstance(target, bool) or not isinstance(target, int):
        return None
    if not (CONFIG_BUDGET_MIN <= target <= CONFIG_BUDGET_MAX):
        return None
    return target


def budget_block(config, output_tokens: int) -> dict | None:
    """Contract's `burn.budget`. None when unconfigured or unparseable - the key is then
    ABSENT from burn entirely, which is how both consumers detect the feature. A zeroed
    budget block would read as "budget 0%" on a panel that has no budget at all.
    """
    target = budget_target(config.get("budget") if isinstance(config, dict) else None)
    if target is None:
        return None
    return {"dailyOutputTokens": target,
            "todayPct": min(round(output_tokens / target, BUDGET_PCT_DP), BUDGET_PCT_CAP)}


def _local_hour_start(epoch: float) -> float:
    lt = datetime.fromtimestamp(epoch).replace(minute=0, second=0, microsecond=0)
    return lt.timestamp()


def _local_clock(epoch: float) -> str:
    """'2:41 PM' - local wall clock, the way the note reads on the panel. %I is
    zero-padded on Windows and there is no portable %-I, so the pad is stripped."""
    return datetime.fromtimestamp(epoch).strftime("%I:%M %p").lstrip("0")


def _local_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _local_midnight(epoch: float) -> float:
    lt = datetime.fromtimestamp(epoch).replace(hour=0, minute=0, second=0, microsecond=0)
    return lt.timestamp()


def _local_day(epoch: float) -> str:
    """Contract's `dayStart`: the LOCAL calendar day, "YYYY-MM-DD"."""
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")


def _local_day_starts(now: float, count: int) -> list[float]:
    """`count` local midnights ending with today's, oldest first. Walks back an hour
    past each midnight rather than subtracting 86400: a DST change inside the window
    makes one of these days 23 or 25 hours long, and fixed-86400 arithmetic then emits
    the same calendar date twice and drops another."""
    days = [_local_midnight(now)]
    for _ in range(max(0, count - 1)):
        days.append(_local_midnight(days[-1] - 3600))
    days.reverse()
    return days


# --------------------------------------------------------------------- user config

class UserConfig:
    """~/.sidecrab/config.json - quiet hours and the reply gate.

    Re-read at most once a minute, and then only when mtime moved: the builder runs
    every 2 s and this file is on the same disk as everything else crabd touches.
    A file that is missing, unreadable or not a JSON object reads as the defaults.
    """

    DEFAULTS = {"quietHours": None, "allowReply": False}

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data = dict(self.DEFAULTS)
        self._checked_at = 0.0
        self._mtime: float | None = None

    @property
    def path(self) -> Path:
        return self._path or USER_CONFIG_FILE

    def get(self, now: float) -> dict:
        with self._lock:
            if self._checked_at and now - self._checked_at < CONFIG_RECHECK_SEC:
                return self._data
            self._checked_at = now
            path = self.path
            try:
                mtime = path.stat().st_mtime
            except OSError:
                self._write_defaults(path)
                self._mtime = None
                self._data = dict(self.DEFAULTS)
                return self._data
            if self._mtime == mtime:
                return self._data
            self._mtime = mtime
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = None
            self._data = loaded if isinstance(loaded, dict) else dict(self.DEFAULTS)
            return self._data

    def allow_reply(self, now: float) -> bool:
        return bool(self.get(now).get("allowReply"))

    def allow_continue(self, now: float) -> bool:
        """`allowContinue` - the queue-continue enable gate (SEC-3). DEFAULT ON, and the
        asymmetry with allow_reply / panel_approvals (both default OFF, strict `is True`)
        is deliberate: tap-to-continue shipped ALWAYS-ON in v0.12.0, so gating it default
        OFF would silently 400 the widget's Continue / Run the tests / Commit + push
        buttons on every existing install - a regression of a working feature, not a
        safety win. Only an explicit boolean `false` disables it; absent or any other
        value stays ON. It is file-config only (never in CONFIG_WRITABLE), like
        allowReply and panelApprovals, so nothing over the unauthenticated HTTP API can
        toggle it.

        Its real protection is not this flag but the pair the audit verified holding:
        the server-side whitelist (queue accepts only the six builtin prompts plus the
        operator's own continuePrompts extras - never free text, and unwidenable over
        HTTP) and the SEC-1 Origin gate (a visited http/https page is refused 403 before
        it reaches the queue). The flag exists so an operator who wants tap-to-continue
        OFF entirely - closing the local-process and forged-null residual - can set it."""
        return self.get(now).get("allowContinue") is not False

    def panel_approvals(self, now: float) -> bool:
        """`panelApprovals` - {"enabled": bool}, default OFF (contract v0.12.0 §4).

        Read STRICTLY: only a literal `true` under `enabled` turns the panel path on.
        Everything else - the key absent, a non-dict, a truthy string, a 1 - is OFF,
        because the failure directions are not symmetric. Reading a malformed config as
        ON parks a live permission prompt on a 55 s hold behind a panel the operator
        may not even be looking at; reading it as OFF costs a tap and shows the terminal
        dialog, which is what happens with SideCrab uninstalled.
        """
        value = self.get(now).get("panelApprovals")
        return isinstance(value, dict) and value.get("enabled") is True

    def continue_extras(self, now: float) -> list[str]:
        """`continuePrompts` from config.json - the operator's EXTRA buttons, served at
        the top level of /v1/state so the widget can render them.

        The widget hardcodes the contract's three defaults and appends whatever this
        list carries (widget/scripts/sidecrab.js syncContinue), so the builtins are
        deliberately NOT included here - emitting them would draw each default button
        twice.

        Hand-edited JSON, so parsed the same defensive way `recapRepos` is: a non-list,
        non-strings, blanks, over-long entries and duplicates are dropped without
        comment. A typo in one entry must not cost the operator the others.
        """
        value = self.get(now).get("continuePrompts")
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for entry in value:
            if len(out) >= CONTINUE_PROMPTS_CAP:
                break
            if not isinstance(entry, str):
                continue
            prompt = " ".join(entry.split())
            if (not prompt or len(prompt) > CONTINUE_PROMPT_MAX
                    or prompt in out or prompt in CONTINUE_PROMPTS_BUILTIN):
                continue
            out.append(prompt)
        return out

    def continue_prompts(self, now: float) -> tuple[str, ...]:
        """The queue-continue whitelist: the builtin strings plus the config extras.

        The BUILTINS can never be dropped by a config typo - they are the buttons the
        widget is showing right now, and a config that fails to parse must not silently
        turn them into 400s the operator cannot explain.
        """
        return tuple(CONTINUE_PROMPTS_BUILTIN) + tuple(self.continue_extras(now))

    def recap_repos(self, now: float) -> list[str]:
        """`recapRepos` - extra absolute paths for recap.commits (contract amendment
        2026-08-26). A session whose cwd is one directory but which DRIVES a repo
        somewhere else never appears among the session cwds, so without this the repo
        it is actually committing to is invisible to the recap.

        File-config only: it is deliberately absent from the /v1/config whitelist, so
        nothing reachable over HTTP can point the git half at an arbitrary path.

        Parsed defensively - this is hand-edited JSON. A non-list, non-string entries,
        blanks and paths that are not existing directories are dropped without comment;
        a typo in the config must not cost the operator the rest of the recap.
        """
        value = self.get(now).get("recapRepos")
        if not isinstance(value, list):
            return []
        out: list[str] = []
        for entry in value:
            if not isinstance(entry, str):
                continue
            path = entry.strip()
            if not path or path in out:
                continue
            try:
                if not Path(path).is_dir():
                    continue   # skipped silently: a repo may live on a drive not mounted
            except (OSError, ValueError):
                continue
            out.append(path)
        return out

    # Sub-keys a whitelisted BLOCK may omit without that meaning "delete it" (v0.16.0).
    # The blocks are written whole, so a writer that has never heard of a member erases
    # it - which is precisely what happened to toast.approvalThresholdSec: the widget's
    # settings sheet sends {thresholdSec, enabled}, the operator's hand-edited approval
    # threshold disappeared on the next save, and the notifier silently fell back to its
    # 20 s default. Preserved HERE rather than in the handler because only this method
    # holds the lock over the read-modify-write - a handler that read the old value first
    # would race a hand edit landing between the read and the write, and this fix exists
    # because a hand-edited value was being lost.
    PRESERVED_SUBKEYS = {"toast": ("approvalThresholdSec",)}

    def set_keys(self, values: dict) -> bool:
        """POST /v1/config's write. Read-modify-write under the same lock `get` uses,
        PRESERVING every other key: only the whitelisted keys are writable over HTTP,
        and a blind rewrite would silently clear `allowReply` - a flag the user set
        deliberately - every time the widget nudged a quiet window.

        `values` is already validated by the handler, and applied WHOLE: a body naming
        both quietHours and toast is one file write, so the config can never be left
        half-updated by a crash between two writes.
        Returns False when the file could not be written; nothing is cached then.
        """
        return self._write(lambda data: data.update(
            self._with_preserved_subkeys(values, data)))

    def set_quiet_override(self, mode: str | None, until: float) -> bool:
        """POST /v1/action {"action":"quiet"}'s write (v0.23.0). `mode` None is the
        "auto" tap - clear the override - and `until` is ignored then.

        Through the SAME locked read-modify-write /v1/config uses, which is the whole
        point of routing it here rather than writing the file from the handler: the
        v0.16.0 lesson was that a writer holding a whole-file rewrite outside this lock
        loses whatever landed between its read and its write, and this endpoint is one an
        operator taps twice in a row. Idempotent by construction - clearing an absent
        override is a write of the same file, not an error.
        """
        def apply(data: dict) -> None:
            if mode is None:
                data.pop(QUIET_OVERRIDE_KEY, None)
            else:
                data[QUIET_OVERRIDE_KEY] = {"mode": mode, "until": _utc_iso(until)}
        return self._write(apply)

    def _write(self, apply) -> bool:
        with self._lock:
            path = self.path
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = None   # missing/corrupt: start from the defaults, never from {}
            data = dict(loaded) if isinstance(loaded, dict) else dict(self.DEFAULTS)
            # v0.23.0: the expired override is swept HERE, on the next write of any kind,
            # and deliberately not by a timer whose only job would be to delete a key
            # that already reads as absent (quiet_override). Anything that reads as
            # absent is removed, malformed included - the file should not keep a value
            # nothing will ever honour.
            if (QUIET_OVERRIDE_KEY in data
                    and quiet_override(data, time.time()) is None):
                data.pop(QUIET_OVERRIDE_KEY, None)
            apply(data)
            # A-03 (v0.26.0): write a sibling temp then os.replace it onto the target,
            # rather than path.write_text - which opens with "w" and TRUNCATES before it
            # writes, so a failure after the truncate (ENOSPC, a killed process, a hiccup)
            # left config.json EMPTY and silently reverted the operator to DEFAULTS,
            # losing quietHours/budget/panelApprovals and the rest. The reached-on-every
            # -tap path (POST /v1/action {"action":"quiet"} and every /v1/config save)
            # must fail-atomic: os.replace is atomic on Windows and POSIX, so a crash
            # mid-write leaves EITHER the old file or the new one, never nothing. The
            # truncate now only ever hits the temp. (Runtime sibling of setup's SET-a2,
            # which covers the installer layer; crabd is the writer on the tap path.)
            tmp = path.with_name(path.name + ".tmp")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                try:
                    tmp.unlink()       # best-effort: leave no half-written temp behind
                except OSError:
                    pass
                return False
            # Bust the cache both ways: the contract says the NEXT /v1/state reflects
            # the write, and the once-a-minute damper would otherwise hold the old
            # quiet computation for up to 60 s.
            self._data = data
            self._mtime = None
            self._checked_at = 0.0
            return True

    @classmethod
    def _with_preserved_subkeys(cls, values: dict, on_disk: dict) -> dict:
        """`values` with every PRESERVED_SUBKEYS member the writer omitted carried over
        from `on_disk`. An EXPLICIT value in `values` always wins - preservation is for
        silence, never an override - and a member the writer cannot express has no way to
        be deleted over HTTP, which is the trade: an unremovable hand-edited key beats a
        silently erased one. Copies rather than mutating `values`, so the handler's
        validated dict is never edited under it."""
        merged = dict(values)
        for key, subkeys in cls.PRESERVED_SUBKEYS.items():
            block, previous = merged.get(key), on_disk.get(key)
            if not isinstance(block, dict) or not isinstance(previous, dict):
                continue
            carried = {s: previous[s] for s in subkeys
                       if s in previous and s not in block}
            if carried:
                merged[key] = {**block, **carried}
        return merged

    @classmethod
    def _write_defaults(cls, path: Path) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cls.DEFAULTS, indent=2) + "\n", encoding="utf-8")
        except OSError:
            pass


# -------------------------------------------------------------------------- history

class HistoryLog:
    """`~/.sidecrab/history.jsonl` - the hook facts crabd used to forget when it restarted.

    One JSON object per line, append-only:
    `{"ts": epoch, "kind": "...", "sessionId": "...", "title": "..."}`.

    WHAT IS NOT IN IT is the point: no question text, no message bodies, no prompts, no
    tool output. A `kind` is one of a fixed set of timeline phrases, and `title` is the
    same session title already served on /v1/state. The file is a record of WHAT
    happened, never of what was said.

    Replay is deliberately forgiving. This is an append-only file on a desktop that
    loses power: the last record can be half a line, or NUL padding a filesystem wrote
    while the tail was never flushed. A torn line is skipped without comment - a crabd
    that refuses to start because its own history is one byte short is worse than a
    crabd that starts having forgotten one event.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        # None = not measured yet. Tracked rather than stat'ed per write, and reset to
        # None on any write error so a failed append cannot leave the count drifting.
        self._size: int | None = None
        self._tail_checked = False
        # GET /v1/history's day index: {"YYYY-MM-DD": [event, ...]} oldest first, plus
        # the ((mtime, size), (mtime, size)) stamp of (.old, current) it was built from.
        # See `day_index` for why the request path is allowed to build this at all.
        self._index: dict[str, list[dict]] | None = None
        self._index_stamp = None

    @property
    def path(self) -> Path:
        return self._path or HISTORY_FILE

    @property
    def old_path(self) -> Path:
        return self.path.with_name(self.path.name + HISTORY_OLD_SUFFIX)

    def append(self, ts: float, kind: str, session_id: str,
               title: str | None = None) -> None:
        """One line, flushed. NEVER raises: this sits on the hook path, and a full disk
        must cost the operator a history entry, not a hook Claude Code is waiting on."""
        try:
            data = (json.dumps({"ts": round(float(ts), 3), "kind": kind,
                                "sessionId": session_id, "title": title},
                               ensure_ascii=False) + "\n").encode("utf-8")
        except (TypeError, ValueError):
            return
        with self._lock:
            path = self.path
            try:
                if self._size is None:
                    self._size = path.stat().st_size
            except OSError:
                self._size = 0
            if not self._tail_checked:
                self._tail_checked = True
                # THE torn-tail trap, found by its own test: the last record of a file
                # that lost power has no newline, so a plain append welds the new record
                # onto the stump and BOTH lines become unparseable - the crash silently
                # eats the first event after every unclean stop. One newline closes the
                # stump; replay then skips one bad line instead of two.
                if self._size and not self._ends_with_newline(path):
                    data = b"\n" + data
            try:
                if self._size + len(data) > HISTORY_MAX_BYTES:
                    self._rotate(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("ab") as handle:
                    handle.write(data)
                    handle.flush()
                self._size += len(data)
            except OSError:
                self._size = None   # re-measure next time rather than drift

    @staticmethod
    def _ends_with_newline(path: Path) -> bool:
        try:
            with path.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                return handle.read(1) == b"\n"
        except OSError:
            return True   # unreadable: do not prepend a newline to a file we cannot see

    def _rotate(self, path: Path) -> None:
        """ONE generation. os.replace overwrites an existing .old on Windows, which is
        the whole size guarantee: two files, each bounded, never a third."""
        try:
            os.replace(path, self.old_path)
        except OSError:
            pass
        self._size = 0

    def replay(self) -> list[tuple[float, str, str, str | None]]:
        """Every readable entry, oldest first. `.old` is read first because rotation
        made it the older half; the explicit sort covers a clock that stepped back."""
        entries = self._read(self.old_path) + self._read(self.path)
        entries.sort(key=lambda entry: entry[0])
        return entries

    def day(self, day: str) -> tuple[list[dict], bool]:
        """GET /v1/history?day= - that LOCAL day's events, NEWEST FIRST, capped.

        Returns (events, truncated). A day with nothing in it returns ([], False): the
        contract makes absence of history a 200, not a 404, because a day the operator
        did not work is a real answer.
        """
        events = self.day_index().get(day) or []
        return events[:HISTORY_DAY_CAP], len(events) > HISTORY_DAY_CAP

    def day_index(self) -> dict[str, list[dict]]:
        """{local day -> events, NEWEST FIRST} over BOTH generations, cached by the
        (mtime, size) pair of each file.

        MEASURED 2026-08-26 on the Windows host, both files filled to the 2 MB cap (26,190
        lines, 4.2 MB): a full warm parse is 42-50 ms. That is the *parse* alone, before
        bucketing and json.dumps, and it straddles the 50 ms line the brief drew - so the
        request path gets the cache rather than the re-parse. A day tap is then a dict
        lookup, and the rebuild only happens after a hook has actually appended.

        The stamp is taken BEFORE the read, never after: an append that lands between the
        two then leaves the cache holding an event its stamp does not cover, and the next
        request rebuilds. Stamping after the read would do the opposite - a stamp newer
        than its content, which never self-corrects and silently loses that event forever.
        """
        with self._lock:
            stamp = (self._stat(self.old_path), self._stat(self.path))
            if self._index is not None and stamp == self._index_stamp:
                return self._index
            # Slurped under the append lock so a concurrent hook cannot be observed as a
            # half-written line; the PARSE happens outside it. /v1/hook already answers
            # 204 before recording, so the few ms this costs a hook is invisible to
            # Claude Code either way.
            raws = (self._slurp(self.old_path), self._slurp(self.path))
        # (ts, event) while the epoch is still in hand, because the served `ts` is a
        # second-granularity ISO string and two events inside the same second would then
        # be unorderable. `.old` first: rotation made it the older half.
        buckets: dict[str, list[tuple[float, dict]]] = {}
        for raw in raws:
            for ts, kind, sid, title in self._parse(raw):
                buckets.setdefault(_local_day(ts), []).append(
                    (ts, {"ts": _utc_iso(ts), "kind": kind,
                          "sessionId": sid, "title": title}))
        index = {}
        for day, rows in buckets.items():
            # Reverse THEN a stable descending sort: the explicit sort is what covers a
            # clock that stepped back (replay() carries the same guard for the same
            # reason), and the reverse first makes same-second events come out in reverse
            # ARRIVAL order rather than oldest-first inside the newest-first list.
            rows.reverse()
            rows.sort(key=lambda row: row[0], reverse=True)
            index[day] = [row[1] for row in rows]
        with self._lock:
            self._index, self._index_stamp = index, stamp
        return index

    @staticmethod
    def _stat(path: Path):
        try:
            info = path.stat()
        except OSError:
            return None        # absent is a stamp value like any other, not an error
        return (info.st_mtime, info.st_size)

    @staticmethod
    def _slurp(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""          # absent is the normal first-run state, not an error

    @classmethod
    def _read(cls, path: Path) -> list[tuple[float, str, str, str | None]]:
        return cls._parse(cls._slurp(path))

    @staticmethod
    def _parse(raw: str) -> list[tuple[float, str, str, str | None]]:
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue       # torn tail, or NUL padding after a power loss
            if not isinstance(obj, dict):
                continue
            ts = _parse_ts(obj.get("ts"))
            kind, sid = obj.get("kind"), obj.get("sessionId")
            if ts is None or not isinstance(kind, str) or not kind:
                continue
            if not isinstance(sid, str) or not sid:
                continue
            title = obj.get("title")
            out.append((ts, kind, sid, title if isinstance(title, str) else None))
        return out


# ----------------------------------------------------------------------- git lookup

# A-04/A-06 (v0.26.0). A `cwd` on an unreachable network path (a VPN drop, a NAS reboot, a
# stale mount) blocks every is_dir/is_file stat in _read on the SMB timeout - ~21 s per pass,
# on the BUILDER thread, re-blocking every _ttl seconds. The probe therefore runs OFF the
# caller's critical path: get() dispatches _read to a short-lived worker and waits at most
# GIT_READ_BUDGET_SEC for it. A reachable local .git answers in microseconds (synchronous
# behaviour preserved); an unreachable one blocks the WORKER, get() returns after the budget
# with the last-known answer (or nulls), and the worker still fills the cache so the next
# pass serves the real value. This bounds the operation rather than blocklisting a path
# SYNTAX - a slow local disk has the same shape as a UNC path and must be handled the same.
GIT_READ_BUDGET_SEC = 1.0
# Ceiling on CONCURRENT in-flight probes. Past this, a miss serves cached/nulls without
# spawning another worker - so a flood of distinct unreachable cwds (the unauthenticated
# /v1/hook `cwd` A-06 rides) can strand at most this many parked threads, not one per cwd.
GIT_RESOLVE_MAX_INFLIGHT = 8
# A-06. _cache had no eviction path at all: 20,000 distinct cwds -> 20,000 entries forever.
# Bounded LRU, same idiom as the forecaster (FORECAST_MAX_KEYS) and OTLP caps. cwds are few
# in real use, so this is generous headroom, not a working limit.
GIT_CACHE_MAX = 256


class GitLookup:
    """cwd -> (repo, branch) by reading .git/HEAD directly. No subprocess, works offline."""

    def __init__(self) -> None:
        # OrderedDict for the A-06 LRU: recency is the eviction order, capped at
        # GIT_CACHE_MAX. A read hit moves its key to the end; the worker pops from the front.
        self._cache: "OrderedDict[str, tuple[str | None, str | None, float]]" = OrderedDict()
        self._ttl = 30.0
        # CRB-F2: the cache is read and written from every thread that builds a document
        # (the refresh loop plus any on-demand build at cold start). The lock is NOT held
        # across _read - that reads .git/HEAD and .git/config off disk, and holding a
        # lock over file IO would serialise every session's git lookup behind the slowest
        # repo. A concurrent miss on the same cwd therefore does the read twice and the
        # second write wins, which is correct: both readings are of the same file.
        self._lock = threading.Lock()
        # A-04. Bounds the number of concurrently-parked probe workers (see get()).
        self._resolve_sem = threading.BoundedSemaphore(GIT_RESOLVE_MAX_INFLIGHT)

    def get(self, cwd: str | None) -> tuple[str | None, str | None]:
        if not cwd:
            return None, None
        now = time.time()
        with self._lock:
            hit = self._cache.get(cwd)
            if hit is not None:
                self._cache.move_to_end(cwd)   # LRU touch (A-06)
        if hit and now - hit[2] < self._ttl:
            return hit[0], hit[1]
        # Miss or stale. A-04: resolve OFF this thread's critical path, bounded, so an
        # unreachable path can never stall build(). If it resolves inside the budget
        # (the reachable local case), return the fresh answer; otherwise serve the last
        # cached value if we have one, else nulls - build() moves on either way and the
        # worker fills the cache for the next pass.
        resolved = self._resolve_bounded(cwd)
        if resolved is not None:
            return resolved
        return (hit[0], hit[1]) if hit else (None, None)

    def _resolve_bounded(self, cwd: str) -> tuple[str | None, str | None] | None:
        """Run _read on a worker thread and wait at most GIT_READ_BUDGET_SEC. Returns the
        (repo, branch) it produced, or None if it did not finish in time (or the broker of
        parked workers is saturated). The worker ALWAYS writes its result to the cache and
        releases its slot, whether or not this caller was still waiting."""
        if not self._resolve_sem.acquire(blocking=False):
            return None            # too many parked probes already - don't pile on
        holder: dict = {}
        done = threading.Event()

        def work() -> None:
            try:
                holder["v"] = self._read(cwd)
            except Exception:      # noqa: BLE001 - honest-failure rule; _read is total
                holder["v"] = (None, None)
            finally:
                repo, branch = holder.get("v", (None, None))
                with self._lock:
                    self._cache[cwd] = (repo, branch, time.time())
                    self._cache.move_to_end(cwd)
                    while len(self._cache) > GIT_CACHE_MAX:
                        self._cache.popitem(last=False)
                self._resolve_sem.release()
                done.set()

        threading.Thread(target=work, name="git-resolve", daemon=True).start()
        if done.wait(GIT_READ_BUDGET_SEC):
            return holder.get("v", (None, None))
        return None                # over budget: the worker finishes in the background

    @staticmethod
    def _remote_name(gitdir: Path) -> str | None:
        """Repo name from `[remote "origin"] url` in .git/config, read as a flat file."""
        try:
            lines = (gitdir / "config").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        in_origin = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                in_origin = stripped.replace(" ", "").lower().startswith('[remote"origin"]')
                continue
            if in_origin and stripped.lower().startswith("url"):
                _, _, url = stripped.partition("=")
                url = url.strip().rstrip("/")
                if not url:
                    return None
                name = url.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
                if name.endswith(".git"):
                    name = name[:-4]
                return name or None
        return None

    @staticmethod
    def _read(cwd: str) -> tuple[str | None, str | None]:
        try:
            start = Path(cwd)
        except (TypeError, ValueError):
            return None, None
        for cand in (start, *start.parents):
            dot = cand / ".git"
            gitdir = None
            try:
                if dot.is_dir():
                    gitdir = dot
                elif dot.is_file():
                    # linked worktree: ".git" is a file containing "gitdir: <path>"
                    text = dot.read_text(encoding="utf-8", errors="replace").strip()
                    if text.startswith("gitdir:"):
                        gitdir = Path(text[len("gitdir:"):].strip())
            except OSError:
                return None, None
            if gitdir is None:
                continue
            repo = cand.name
            common = gitdir  # the shared .git dir; a worktree's gitdir points inside it
            parts = gitdir.parts
            if "worktrees" in parts:
                # <main-repo>/.git/worktrees/<name>
                i = parts.index("worktrees")
                common = Path(*parts[:i])
                if i >= 2:
                    repo = parts[i - 2]
            # Prefer the origin remote's name (e.g. "payments-svc") over the folder
            # name ("IT") - the widget labels cards with it and folders get renamed.
            repo = GitLookup._remote_name(common) or repo
            branch = None
            try:
                head = (gitdir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                head = ""
            if head.startswith("ref:"):
                branch = head.split("/", 2)[-1] or None
            elif head:
                branch = head[:8]  # detached HEAD
            return repo, branch
        return None, None


# -------------------------------------------------------------------- transcripts

class FileFacts:
    """Incrementally parsed facts for one transcript JSONL. Files are append-only."""

    __slots__ = (
        "path", "session_id", "is_subagent", "size", "mtime", "offset", "pending",
        "requests", "custom_title", "ai_title", "last_prompt", "first_prompt",
        "last_cwd", "last_model", "last_speed", "last_ts",
        "question", "question_ts", "question_rank", "agent_labels", "_pending_agents",
        "context_tokens", "context_ts", "skipped", "_lock",
    )

    def __init__(self, path: Path, session_id: str, is_subagent: bool) -> None:
        self.path = path
        self.session_id = session_id
        self.is_subagent = is_subagent
        self.size = 0
        self.mtime = 0.0
        self.offset = 0
        self.pending = b""
        # requestId -> (ts_epoch, output, input, cache_read, cache_creation, model)
        # Assistant usage repeats once per streamed line with the SAME requestId;
        # keying on it is what stops the burn numbers being multiplied by ~4. `model` is
        # the model string off THIS message, not self.last_model - burn.byModel must
        # attribute each record to the model that actually spent it, and a session can
        # switch models mid-day.
        self.requests: dict[str, tuple[float, int, int, int, int, str | None]] = {}
        self.custom_title: str | None = None
        self.ai_title: str | None = None
        self.last_prompt: str | None = None
        self.first_prompt: str | None = None
        self.last_cwd: str | None = None
        self.last_model: str | None = None
        self.last_speed: str | None = None
        self.last_ts: float = 0.0
        # Newest question this transcript carries, with the timestamp that dates it and
        # a rank so an AskUserQuestion beats a trailing "?" written in the same second.
        self.question: str | None = None
        self.question_ts: float = 0.0
        self.question_rank: int = 0
        # agentId -> the Agent/Task tool_use `description` that launched it.
        self.agent_labels: dict[str, str] = {}
        self._pending_agents: dict[str, str] = {}  # tool_use id -> description
        # contextTokens: the INPUT side of the NEWEST usage record, i.e. how full the
        # context window was on the last request. None until one is seen - a session
        # with no usage record has an unknown context size, not a zero-sized one.
        self.context_tokens: int | None = None
        self.context_ts: float = 0.0
        # Records this file has offered that crabd could not read (v0.20.0). Never
        # served - it is what makes "the parser skipped something" answerable at all,
        # since the log line only fires once per crabd lifetime.
        self.skipped: int = 0
        # CRB-F2 SECOND HALF (v0.20.0). The store's lock made `files` safe to iterate;
        # it never covered the mutable state INSIDE a FileFacts. `requests` and
        # `agent_labels` are written by refresh() under the store lock and READ by
        # build()'s session loop, which holds nothing - so at cold start, when the
        # refresh thread and an on-demand /v1/state build are both running, one thread
        # can be iterating the dict the other is inserting into. PROVEN 2026-08-27 at
        # the object level: "dictionary changed size during iteration" in under a
        # second. Readers go through usage_records()/labels(), which copy under this.
        self._lock = threading.Lock()

    def reset(self) -> None:
        self.offset = 0
        self.pending = b""
        self.requests.clear()
        self.custom_title = self.ai_title = self.last_prompt = self.first_prompt = None
        self.last_cwd = self.last_model = self.last_speed = None
        self.last_ts = 0.0
        self.question = None
        self.question_ts = 0.0
        self.question_rank = 0
        self.agent_labels.clear()
        self._pending_agents.clear()
        self.context_tokens = None
        self.context_ts = 0.0

    def refresh(self) -> bool:
        """Parse whatever is new. Returns True when the file changed."""
        try:
            st = self.path.stat()
        except OSError:
            return False
        if st.st_size == self.size and st.st_mtime == self.mtime and self.offset:
            return False
        if st.st_size < self.offset:
            self.reset()  # truncated or rewritten -> full re-read
        try:
            with self.path.open("rb") as fh:
                fh.seek(self.offset)
                chunk = fh.read()
                self.offset = fh.tell()
        except OSError:
            return False
        self.size, self.mtime = st.st_size, st.st_mtime
        data = self.pending + chunk
        lines = data.split(b"\n")
        self.pending = lines.pop()  # trailing partial line; completed on the next pass
        # The lock spans the whole consume run, not each write: a reader must not see a
        # half-applied file (requests inserted but context_ts not yet moved), and one
        # acquire per refresh is cheaper than one per line.
        with self._lock:
            for raw in lines:
                if raw.strip():
                    self._consume(raw)
        return True

    def usage_records(self) -> dict[str, tuple[float, int, int, int, int, str | None]]:
        """A COPY of the usage records, taken under the file's own lock - see __init__."""
        with self._lock:
            return dict(self.requests)

    def labels(self) -> dict[str, str]:
        """A COPY of the subagent labels, for the same reason usage_records() copies."""
        with self._lock:
            return dict(self.agent_labels)

    def _consume(self, raw: bytes) -> None:
        """TOTAL by construction (v0.20.0). One unreadable record must cost that record
        and nothing else: this is called from a loop inside scan(), and an exception here
        used to abort the scan, the build and therefore EVERY session's card.

        The guards inside _consume_record cover the ten shapes measured on 2026-08-27;
        this catch is what makes the eleventh - the shape nobody has met yet - a skipped
        line instead of a 500. The read offset has already moved past the record, so a
        skip is permanent and cannot become a retry loop.
        """
        try:
            self._consume_record(raw)
        except Exception as exc:            # noqa: BLE001 - the honest-failure rule
            self.skipped += 1
            _log_once(TRANSCRIPT_SKIP_LOG_KEY,
                      f"crabd: skipped an unreadable transcript record in "
                      f"{self.path.name} ({type(exc).__name__}); the rest of the file "
                      f"is still read")

    def _consume_record(self, raw: bytes) -> None:
        if len(raw) > BIG_LINE_BYTES and b'"usage"' not in raw:
            return
        try:
            obj = json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(obj, dict):
            return
        kind = obj.get("type")

        if kind == "custom-title":
            self.custom_title = _trim(obj.get("customTitle"), TITLE_MAX)
            return
        if kind == "ai-title":
            self.ai_title = _trim(obj.get("aiTitle"), TITLE_MAX)
            return
        if kind == "last-prompt":
            self.last_prompt = _trim(obj.get("lastPrompt"), TITLE_MAX)
            return

        ts = _parse_ts(obj.get("timestamp"))
        if ts and ts > self.last_ts:
            self.last_ts = ts
        cwd = obj.get("cwd")
        if isinstance(cwd, str) and cwd:
            self.last_cwd = cwd

        # `message` is not guaranteed to be a dict on EITHER branch below. The old
        # `(obj.get("message") or {}).get(...)` reads as a guard and is not one: it
        # defends against null and against nothing else, so a record whose `message` is a
        # string or a list raised AttributeError straight out of the scan (measured,
        # 2026-08-27). The isinstance test is the guard the shape actually needs.
        message = obj.get("message")
        if not isinstance(message, dict):
            message = {}

        if kind == "user":
            content = message.get("content")
            # A real typed prompt has string content; tool results arrive as a list.
            if isinstance(content, str) and self.first_prompt is None:
                self.first_prompt = _trim(content, TITLE_MAX)
            elif isinstance(content, list):
                self._link_agents(content)
            return

        if kind != "assistant":
            return
        content = message.get("content")
        if isinstance(content, list):
            self._scan_assistant_blocks(content, ts or self.last_ts or time.time())
        usage = message.get("usage")
        if not isinstance(usage, dict):
            usage = {}
        model = message.get("model")
        if not (isinstance(model, str) and model):
            model = None
        if model:
            self.last_model = model
        speed = usage.get("speed")
        if isinstance(speed, str) and speed:
            self.last_speed = speed
        request_id = obj.get("requestId")
        if not isinstance(request_id, str) or not usage:
            return
        # A-11 (v0.26.0): SKIP, don't guess. A usage-bearing assistant record with no
        # parseable `timestamp` of its own AND no earlier record to inherit one from used
        # to be dated `time.time()` - the moment crabd happened to read the file. That
        # fabricated clock becomes context_ts -> turn_ts -> note_activity, the signal that
        # CLEARS a standing needs_input: a re-parse from offset 0 (an eviction + re-admit
        # after a transient OSError) could then silence a real waiting question purely
        # because of WHEN the file was read. An untimestamped record can bucket into no
        # burn window either, so contributing nothing is the honest choice - the same
        # skip-don't-500 rule the rest of the scan follows. `last_ts` (a real inherited
        # timestamp) is still fine; only the pure `now` fallback is refused.
        record_ts = ts or self.last_ts
        if not record_ts:
            return
        # _as_count, never int(): the counters are untrusted and five shapes of them
        # used to abort the whole scan. See _as_count.
        inp = _as_count(usage.get("input_tokens"))
        cache_read = _as_count(usage.get("cache_read_input_tokens"))
        cache_create = _as_count(usage.get("cache_creation_input_tokens"))
        self.requests[request_id] = (
            record_ts,
            _as_count(usage.get("output_tokens")),
            inp, cache_read, cache_create,
            model,
        )
        # NEWEST wins by timestamp, and `>=` is what makes that true in both directions
        # the file can present. A streamed repeat carries the SAME requestId, timestamp
        # and usage, so re-applying it is a no-op; but the day's LAST request can share
        # a whole-second timestamp with the one before it, and the file is append-only,
        # so on a tie the later LINE is the later request and must win. Strict `>` would
        # freeze contextTokens on the first record of any tied pair.
        if record_ts >= self.context_ts:
            self.context_tokens = inp + cache_read + cache_create
            self.context_ts = record_ts

    def _scan_assistant_blocks(self, content: list, ts: float) -> None:
        """Questions and subagent launches out of one assistant message.

        AskUserQuestion shape pinned from 227 real blocks in ~/.claude/projects on
        2026-08-26: the only input key is `questions`, a list of
        {question, header, multiSelect, options[{label, description, preview?}]}.
        """
        asked: list[str] = []
        tail_text = None
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                tail_text = block.get("text")
                continue
            if btype != "tool_use":
                continue
            name = block.get("name")
            inp = block.get("input")
            if not isinstance(inp, dict):
                continue
            if name == "AskUserQuestion":
                for question in inp.get("questions") or []:
                    if isinstance(question, dict) and isinstance(question.get("question"), str):
                        text = question["question"].strip()
                        if text:
                            asked.append(text)
            elif name in ("Agent", "Task"):
                description = inp.get("description")
                block_id = block.get("id")
                if isinstance(description, str) and description and isinstance(block_id, str):
                    self._pending_agents[block_id] = description

        if asked:
            self._remember_question(" · ".join(asked), ts, 2)
        elif isinstance(tail_text, str):
            self._remember_question(self._trailing_question(tail_text), ts, 1)

    @staticmethod
    def _trailing_question(text: str) -> str | None:
        """The closing question of an assistant turn - the last non-empty line, and only
        when it actually ends in '?'. Taking the whole turn would serve an essay."""
        for line in reversed(text.strip().splitlines()):
            stripped = line.strip()
            if stripped:
                return stripped if stripped.endswith("?") else None
        return None

    def _remember_question(self, text, ts: float, rank: int) -> None:
        trimmed = _trim_question(text)
        if not trimmed:
            return
        if ts > self.question_ts or (ts >= self.question_ts and rank >= self.question_rank):
            self.question = trimmed
            self.question_ts = ts
            self.question_rank = rank

    def _link_agents(self, content: list) -> None:
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_use_id = block.get("tool_use_id")
            description = self._pending_agents.pop(tool_use_id, None) \
                if isinstance(tool_use_id, str) else None
            if description is None:
                continue
            body = block.get("content")
            if isinstance(body, list):
                body = " ".join(part.get("text", "") for part in body
                                if isinstance(part, dict))
            if not isinstance(body, str):
                continue
            match = AGENT_ID_IN_RESULT.search(body)
            if match:
                self.agent_labels[match.group(1)] = description

    def title(self) -> str | None:
        return self.custom_title or self.ai_title or self.first_prompt or self.last_prompt

    def title_source(self) -> str | None:
        """Which tier title() came from. Read in the SAME order as title() - if the two
        ever diverge the panel styles a title by a tier that did not produce it. Both
        prompt tiers report "prompt": the widget styles by provenance (typed by a human
        vs derived), not by which end of the transcript the prompt sat at."""
        if self.custom_title:
            return "custom"
        if self.ai_title:
            return "ai"
        if self.first_prompt or self.last_prompt:
            return "prompt"
        return None

    def label(self) -> str:
        """Short name for a subagent row. Measured: subagent transcripts carry no
        ai-title/custom-title line, so in practice this is the launch prompt's opening."""
        return (self.ai_title or self.custom_title or self.first_prompt
                or self.path.stem)

    def agent_id(self) -> str:
        stem = self.path.stem
        return stem[len("agent-"):] if stem.startswith("agent-") else stem


class TranscriptStore:
    """Discovers session + subagent transcripts and keeps FileFacts warm.

    CRB-F2 (QA-Audit 2026-08-27): `files` is mutated by scan() and iterated by build(),
    and both run on more than one thread at cold start - the refresh loop, an on-demand
    /v1/state build when no snapshot exists yet, and any test or tool calling build()
    directly. Two concurrent builds could therefore have one deleting a stale key while
    the other was mid-`.values()`, which is a RuntimeError out of the dict itself and
    reaches the operator as one 500 that self-heals on the next poll. `_lock` serialises
    the scan and snapshot() hands readers a LIST, so no caller iterates the live dict.
    `files` stays a plain public attribute - the fixtures read it directly, and a reader
    of a single key was never the race.
    """

    def __init__(self, projects_dir: Path) -> None:
        self.projects_dir = projects_dir
        self.files: dict[str, FileFacts] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> list["FileFacts"]:
        """The FileFacts to build from, as a list taken under the lock. Callers iterate
        this, never `files` - see the class docstring."""
        with self._lock:
            return list(self.files.values())

    def scan(self, now: float) -> None:
        cutoff = now - TRANSCRIPT_WINDOW_SEC
        try:
            projects = [p for p in self.projects_dir.iterdir() if p.is_dir()]
        except OSError:
            return
        # Held across the whole scan, not per-mutation: the delete sweep at the end is
        # only correct against the `seen` set THIS pass built, so a second scan
        # interleaving with it would evict files it had just admitted. The work inside is
        # filesystem-bound, but the alternative is a second scan doing the same work
        # concurrently and fighting over the result.
        with self._lock:
            seen: set[str] = set()
            for project in projects:
                for path, session_id, is_sub in self._transcripts(project):
                    key = str(path)
                    seen.add(key)
                    facts = self.files.get(key)
                    if facts is None:
                        try:
                            if path.stat().st_mtime < cutoff:
                                continue  # never been read and too old to matter
                        except OSError:
                            continue
                        facts = FileFacts(path, session_id, is_sub)
                        self.files[key] = facts
                    try:
                        facts.refresh()
                    except Exception as exc:    # noqa: BLE001 - honest-failure rule
                        # _consume is already total, so reaching here means the FILE is
                        # unreadable in a way stat/open did not report. One file must not
                        # cost every other session its card: the scan carries on and this
                        # file simply stops advancing.
                        _log_once(TRANSCRIPT_FILE_LOG_KEY,
                                  f"crabd: could not read transcript {path.name} "
                                  f"({type(exc).__name__}); other sessions unaffected")
            # CD-09 (v0.21.0): a file leaves the store when it leaves the DISK **or**
            # when it leaves the WINDOW. The cutoff used to gate admission only, so a
            # file admitted while it was fresh stayed resident, stat'ed and re-offered
            # to every build for as long as crabd ran - and its whole `requests` dict
            # was COPIED into every 2-second build, forever, for a session that last
            # wrote days ago. Nothing downstream can use it: TRANSCRIPT_WINDOW_SEC is
            # burn.daily's span, and a file older than that contributes to no bucket.
            #
            # `facts.mtime` is the mtime of the last successful refresh, so a file that
            # has never been read (0.0) is left alone rather than evicted and re-admitted
            # on the next pass - which would churn its parse offset and re-read it whole.
            for key in [k for k, f in self.files.items()
                        if k not in seen or (f.mtime and f.mtime < cutoff)]:
                del self.files[key]

    @staticmethod
    def _transcripts(project: Path):
        """<proj>/<sessionId>.jsonl plus <proj>/<sessionId>/subagents/**/*.jsonl."""
        try:
            entries = list(project.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.is_file() and entry.suffix == ".jsonl":
                yield entry, entry.stem, False
            elif entry.is_dir():
                sub_root = entry / "subagents"
                if not sub_root.is_dir():
                    continue
                try:
                    for sub in sub_root.rglob("*.jsonl"):
                        if sub.name == "journal.jsonl":
                            continue  # orchestration log, no usage records
                        yield sub, entry.name, True
                except OSError:
                    continue


# -------------------------------------------------------------- session hook state

class HookTracker:
    """Session state driven by Claude Code hooks. Thread-safe; POSTs are concurrent."""

    STATE_EVENTS = {
        # idle, not working (v0.28.2): SessionStart is the app OPENING a session - the
        # operator clicking into an old one included - and no turn is running until
        # UserPromptSubmit says so. Measured live 2026-09-01: a click into a two-day-old
        # session put an amber WORKING card on the glass for 15 minutes on the strength
        # of the open alone.
        "SessionStart": ("idle", "session started"),
        "UserPromptSubmit": ("working", "working on your prompt"),
        "Notification": ("needs_input", None),
        "Stop": ("done", "finished"),
        "SessionEnd": ("gone", "session ended"),
    }
    # The contract's `events` text. Deliberately NOT the `lastEvent` label above: that one
    # says what the session IS doing (present tense, and for Notification it is the
    # question itself), this one is a timeline entry for what HAPPENED.
    EVENT_TEXT = {
        "SessionStart": "session started",
        "UserPromptSubmit": "prompt submitted",
        "Notification": "asked a question",
        "Stop": "turn finished",
        "SubagentStop": "subagent finished",
        "SessionEnd": "session ended",
    }
    ACK_EVENT = "acknowledged from Edge"
    # Replayed ring kinds that say the turn is OVER, and the state each restores
    # (CD-07). Keyed on EVENT_TEXT's values because that is what the history file
    # holds - a kind, never a state name. See replay() for why only these two.
    REPLAY_TERMINAL = {EVENT_TEXT["Stop"]: "done", EVENT_TEXT["SessionEnd"]: "gone"}

    def __init__(self, history: "HistoryLog | None" = None) -> None:
        self._lock = threading.Lock()
        self.sessions: dict[str, dict] = {}
        self.count = 0
        # (epoch, sessionId) per observed transition INTO `done`. Kept beside the
        # session rows because a row is pruned once it goes gone, and recap.doneToday
        # still has to remember that the session finished earlier today.
        self.dones: list[tuple[float, str]] = []
        # No history object = no persistence, which is what a unit test constructing a
        # bare HookTracker gets. Nothing here ever creates a file by default.
        self._history = history
        # Last-known session title, so a history line can carry `title`-at-the-time.
        # Fed by the builder (it is the only side that reads transcripts); a session
        # whose title crabd has not learned yet logs a null title rather than a guess.
        self._titles: dict[str, str] = {}

    @staticmethod
    def _blank(now: float) -> dict:
        return {"state": None, "since": now, "last_event": None, "at": now,
                "cwd": None, "stops": [], "subagent_stops": 0,
                "question": None, "turn_started": None, "acked": False,
                # v0.20.0, INTERNAL - never served. True only while this row's
                # `needs_input` is one the PermissionRequest hook raised and nothing else
                # has re-raised: the one case where the hold ending must stand the card
                # down. See note_permission.
                "permission_alert": False,
                "events": []}

    def note_titles(self, titles: dict) -> None:
        """Builder -> tracker, once per pass. Titles only; nothing else crosses."""
        with self._lock:
            for sid, title in titles.items():
                if isinstance(sid, str) and isinstance(title, str) and title:
                    self._titles[sid] = title

    def _persist(self, kind: str, session_id: str, now: float) -> None:
        """Caller holds the lock. HistoryLog.append never raises."""
        if self._history is not None:
            self._history.append(now, kind, session_id, self._titles.get(session_id))

    def _note_event(self, row: dict, text: str, now: float,
                    session_id: str | None = None, persist: bool = True) -> None:
        """Newest first, capped. Persisted to history.jsonl so the ring survives a
        restart (v0.7.0); `persist=False` is the replay path putting the ring BACK,
        which must not write the same events a second time."""
        row["events"].insert(0, {"at": _utc_iso(now), "text": text})
        del row["events"][EVENTS_CAP:]
        if persist and session_id:
            self._persist(text, session_id, now)

    def record(self, payload: dict) -> None:
        session_id = _session_id(payload)
        event = payload.get("hook_event_name") or payload.get("hookEventName")
        if not session_id or not isinstance(event, str):
            return
        now = time.time()
        with self._lock:
            self.count += 1
            row = self.sessions.setdefault(session_id, self._blank(now))
            row["at"] = now
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd:
                row["cwd"] = cwd

            # Recorded before the SubagentStop early-return: the ring is every hook seen,
            # not only the ones that move the state machine.
            timeline = self.EVENT_TEXT.get(event)
            if timeline:
                self._note_event(row, timeline, now, session_id)

            if event == "SubagentStop":
                row["stops"].append(now)
                row["subagent_stops"] += 1
                return

            mapped = self.STATE_EVENTS.get(event)
            if not mapped:
                return
            state, label = mapped
            previous_question = row["question"]
            if state == "needs_input":
                # lastEvent is the short line; `question` keeps the hook's full text.
                label = _trim(payload.get("message"), EVENT_MAX) or "waiting on you"
                row["question"] = _trim_question(payload.get("message"))
                # A-02 (v0.26.0). A Notification landing on a needs_input row TRANSFERS the
                # alert's ownership to itself: `permission_alert` is relinquished here,
                # ALWAYS, not only when `moved` fires below. The gap this closes is hook
                # ORDER. PERMISSION_QUESTION is word-for-word the CLI's own Notification
                # for the same dialog, so when PermissionRequest arrives FIRST the identical
                # -text Notification then lands with `moved` False (same question) and the
                # old `moved`-gated reset never ran - leaving permission_alert True, so the
                # hold merely expiring stood the card down, dropping an alert the operator
                # is genuinely still waiting on. A Notification IS a question waiting on the
                # operator (STATE-CONTRACT §v0.20.0 §2), so once one lands, the hold ending
                # is no longer that alert's to clear - whichever hook came first. This only
                # relinquishes the flag; it does NOT touch `since`/`acked` (that stays the
                # `moved` block's job below), so the "don't escalate one prompt twice"
                # dedup for a re-fired identical Notification is untouched, and the CLI's
                # actual hook order (unmeasured while approvals are off) no longer decides
                # the outcome - the behaviour is order-INDEPENDENT.
                row["permission_alert"] = False
            if event == "UserPromptSubmit":
                row["turn_started"] = now
            elif event in ("Stop", "SessionEnd"):
                row["turn_started"] = None
            # v0.20.0. A re-fired Notification on a card that is ALREADY `needs_input`
            # used to move nothing: `row["state"] != state` was false, so `since` stayed
            # on the FIRST question and `acked` stayed set. A second, DIFFERENT question
            # therefore landed pre-silenced on a card that had already escalated to red -
            # the exact failure note_activity's docstring warns a view-only fix would
            # cause, reachable here through the hooks instead.
            #
            # The test is the question TEXT, and that is the healthy-night guard, not a
            # nicety: Claude Code re-fires Notification for the SAME standing prompt
            # while the operator is away, and resetting on every one of those would
            # un-ack a card the operator has already seen, every time, forever.
            # TWO questions, deliberately separate (v0.21.0). `entered` is "did the
            # state machine CHANGE state" and gates the done LEDGER; `moved` is "is this
            # row's clock now wrong" and gates `since`, the ack and the question. They
            # used to be one flag, and CD-06 is what that cost.
            #
            # CD-06, the `event == "Stop"` arm of `moved`. A CONTINUATION turn - the
            # tap-to-continue path, where crabd's own Stop answer forces another turn and
            # NO UserPromptSubmit fires - ends on a done -> done transition, which moved
            # nothing. So `since` stayed pinned to the FIRST Stop of the session, and
            # _resolve reads "transcript written after `since`" as work resuming: every
            # write of the continuation turn was after that frozen `since`, so the card
            # read `working` through the second turn, through its Stop, and every turn
            # after, until it aged out of the window without ever showing `done`.
            #
            # THE STRUCTURAL POINT: _resolve's reactivation is a VIEW-only overlay that
            # never writes back, which is precisely what note_activity's docstring
            # refuses to do for needs_input. The tracker cannot see the transcript, so
            # the honest rule is that a Stop is a turn ENDING NOW whatever the row last
            # said - and re-dating is the whole of what it needs.
            #
            # The LEDGER deliberately does NOT follow, and that is what keeps a repeated
            # Stop from writing a second `done` line into history: the tracker cannot
            # tell a duplicate Stop from a continuation one, and doneToday / done_by_day
            # both count DISTINCT session ids, so a done -> done finish is already
            # counted by the transition that got the row there. (It could go uncounted
            # only if a row survived from one local day into the next, which prune's
            # GONE_AFTER_SEC horizon - CD-09 - now makes unreachable.)
            entered = row["state"] != state
            moved = (entered
                     or (state == "needs_input"
                         and row["question"] != previous_question)
                     or event == "Stop")
            if moved:
                # Contract: an ack survives only until the session moves again.
                row["acked"] = False
                row["since"] = now
                # v0.20.0: whatever raised this alert, the PermissionRequest hold is no
                # longer the only thing holding it up - so the hold expiring must not
                # stand the card down. See note_permission / clear_permission.
                row["permission_alert"] = False
                if state != "needs_input":
                    row["question"] = None
                if state == "done" and entered:
                    self.dones.append((now, session_id))
                    # A SEPARATE line from the "turn finished" ring event above: a Stop
                    # hook always writes the ring entry, but only a Stop that actually
                    # MOVED the state counts toward doneToday. Persisting the transition
                    # rather than re-deriving it from ring lines is what keeps replayed
                    # doneToday equal to what the live process counted.
                    self._persist(HISTORY_DONE_KIND, session_id, now)
            row["state"] = state
            row["last_event"] = label

    def note_activity(self, session_id: str, at: float) -> bool:
        """v0.19.0. The operator answered IN THE APP - clear `needs_input`.

        `at` is the timestamp of the newest completed model round-trip in this session's
        MAIN transcript (see the NEEDS_INPUT_* constant block for why that is the signal
        and what was rejected in its place). A round-trip strictly after the question was
        raised can only mean the model was unblocked, so this is a REAL transition and is
        written as one: `question` cleared, `acked` cleared, `since` moved, a ring event
        persisted like any hook's.

        Writing it back rather than overlaying it in the served row is the whole point.
        An overlay would leave the tracker on `needs_input`, and the NEXT Notification
        would then find `row["state"] != state` false - so `since` would not move and
        `acked` would not clear. The second question of a turn would land pre-silenced on
        a card that had already escalated to red. A view-only fix trades one stuck alert
        for a missed one.

        Only ever moves needs_input -> working. Every other state ignores it, which is
        what makes it idempotent: the build loop calls this on every pass, and after the
        first clear the row is `working` and the call is a dict lookup.
        """
        if not isinstance(session_id, str) or not session_id:
            return False
        with self._lock:
            row = self.sessions.get(session_id)
            if row is None or row["state"] != "needs_input":
                return False
            if at <= row["since"] + NEEDS_INPUT_ACTIVITY_GRACE_SEC:
                return False
            # A-12 (v0.26.0). The round-trip is a real answer - it passed the freshness gate
            # above against its OWN timestamp - but that timestamp must not be WRITTEN into
            # `since`/`at` ahead of crabd's own clock. `_parse_ts` bounds the range; nothing
            # bounded it against NOW, so a record dated ahead of this machine (an NTP step, a
            # transcript copied in from another host) posted a FUTURE `since` - a negative
            # widget age - and a future `at` postponed `prune` by the same skew. Clamp the
            # WRITTEN value only: the gate keeps using the real timestamp, so a genuine later
            # round-trip still clears, while the persisted clock can never run ahead of now.
            written = min(at, time.time())
            self._stand_down(row, written)
            self._note_event(row, NEEDS_INPUT_CLEARED_EVENT, written, session_id)
            return True

    @staticmethod
    def _stand_down(row: dict, at: float) -> None:
        """needs_input -> working. The ONE writer of that transition (v0.20.0), shared by
        the transcript's turn clock and by a permission hold ending, so the two can never
        drift into leaving a card in different shapes."""
        row["state"] = "working"
        # None, not a label: _sessions falls back to _implied_event("working"), so the
        # card cannot be left showing the answered question as its current event.
        row["last_event"] = None
        row["question"] = None
        row["acked"] = False
        row["permission_alert"] = False
        row["since"] = at
        row["at"] = max(row["at"], at)

    def note_permission(self, session_id: str, question: str, at: float) -> bool:
        """v0.20.0. A PermissionRequest hook is a session WAITING ON THE OPERATOR.

        THE GAP THIS CLOSES: `needs_input` was set by the `Notification` hook and by
        nothing else, so a session sitting on a live permission dialog read `working`
        unless a Notification happened to fire beside it. The panel renders Approve /
        Deny off the `needs_input` sheet, so the very card carrying a `pendingPermission`
        could be the one card not showing it - crabd held the operator's decision open
        for 55 s and never told them it was waiting.

        Raises from a LIVE TURN only (PERMISSION_ALERT_FROM), and the two states it
        refuses are refused for different reasons:

          - `needs_input`: the card is already alerting. The CLI fires a Notification for
            this same dialog ("Claude needs your permission to use Bash"), and the two
            arriving within a second of each other must not escalate one prompt twice, so
            `since` and `acked` are left exactly as they are.
          - `done` / `gone`: the hooks say the turn is over, and a dialog cannot be open
            in a turn that ended. This is not hypothetical - a Stop and a PermissionRequest
            for one session race in the wild, and without the refusal the later of the two
            would resurrect a finished card as alerting. It is the same judgement
            PERMISSION_STALE_EVENTS already makes from the other side.

        The row is created when absent - the caller has already gated on
        `builder.serving`, the same rule ack() and note_external() use.
        """
        if not isinstance(session_id, str) or not session_id:
            return False
        with self._lock:
            row = self.sessions.setdefault(session_id, self._blank(at))
            row["at"] = max(row["at"], at)
            if row["state"] not in PERMISSION_ALERT_FROM:
                return False
            row["state"] = "needs_input"
            row["last_event"] = question
            row["question"] = _trim_question(question)
            row["acked"] = False
            row["permission_alert"] = True
            row["since"] = at
            return True

    def clear_permission(self, session_id: str, at: float) -> bool:
        """v0.20.0. The hold ended - stand down a card THIS hook raised.

        Every exit of the long poll lands here: a panel tap, a pass-through timeout, and
        a hold retired by `stale()`. All three end the same way on the panel, which is
        the requirement - a card must never go on advertising a decision that is no
        longer open, whichever way it closed.

        `permission_alert` is the whole gate. A `needs_input` that a Notification raised
        (or re-raised with a new question) is NOT this hook's to clear: the operator is
        genuinely still being waited on, and the hold merely expiring is not an answer.
        That alert stays up and leaves through the v0.19.0 signals.
        """
        if not isinstance(session_id, str) or not session_id:
            return False
        with self._lock:
            row = self.sessions.get(session_id)
            if row is None or row["state"] != "needs_input" or not row["permission_alert"]:
                return False
            self._stand_down(row, at)
            # A-10: leave a trace. The stand-down was previously silent, so an alert being
            # dropped left nothing in `events` or history.jsonl - undiagnosable in the field.
            self._note_event(row, PERMISSION_CLEARED_EVENT, at, session_id)
            return True

    def ack(self, session_id: str, create: bool = False) -> bool:
        """True when the ack landed. `create` covers a session crabd knows from its
        transcript but has never seen a hook for - dropping that ack would leave the
        widget's card glowing with no way to quiet it."""
        with self._lock:
            row = self.sessions.get(session_id)
            if row is None:
                if not create:
                    return False
                row = self.sessions.setdefault(session_id, self._blank(time.time()))
            row["acked"] = True
            self._note_event(row, self.ACK_EVENT, time.time(), session_id)
            return True

    def live_state(self, session_id: str) -> str | None:
        """The RAW machine state for a row - None for a row this PROCESS has seen no
        state-moving hook for (replay-restored after a restart, or conjured by an
        event-only write). Serving falls back to `working` for that None (see replay's
        docstring); a write that must not trust the fallback - the continue queue, which
        needs a live Stop hook to ever drain - asks here instead (GHOST-a, v0.28.1)."""
        with self._lock:
            row = self.sessions.get(session_id)
            return row["state"] if row else None

    def note_external(self, session_id: str, text: str, create: bool = False) -> bool:
        """A ring event from something that is NOT a hook - OTLP api_error, a panel
        permission decision (v0.12.0). Same write path as a hook event, so it persists
        to history and survives a restart exactly like one.

        What it deliberately does NOT do is touch `state`, `since` or `question`. An
        api_error arriving for a session says something happened INSIDE a turn; it is
        not a transition, and a receiver that could move the state machine would let a
        telemetry batch resurrect a finished session on the panel.

        `create` follows the ack rule (see `ack`): only a session the builder is already
        serving may be conjured, so telemetry for ids crabd knows nothing about cannot
        grow the table. False means the event was dropped.
        """
        if not isinstance(session_id, str) or not session_id or not text:
            return False
        with self._lock:
            row = self.sessions.get(session_id)
            if row is None:
                if not create:
                    return False
                row = self.sessions.setdefault(session_id, self._blank(time.time()))
            self._note_event(row, text, time.time(), session_id)
            return True

    def replay(self, entries) -> None:
        """Put a persisted history back: the events ring, and the done transitions that
        recap.doneToday and recap.week count.

        A RUNNING state is deliberately NOT restored. A "working" row from before the
        restart would claim a turn is running that this process has no hook to end.

        A TERMINAL state IS restored (CD-07, v0.21.0), and that is not a softening of
        the rule above - it is the rule being applied properly. Leaving `state` at None
        was never neutral: _resolve's fallback for a row it has no state for is
        `working`, so every replayed row came back claiming exactly the live turn this
        docstring refuses to claim. Measured 2026-08-27: a session whose last event was
        `turn finished` five minutes before the restart resurrected as `working`, and
        stayed there until it aged to `idle` fifteen minutes later.
        `turn finished` and `session ended` are the two kinds that say the turn is OVER,
        which is a fact about the past like the ring and the tallies. Restoring them
        lets _resolve retire the row on its own schedule (DONE_DROP_SEC, then gone).

        Nothing else is mapped. `asked a question` in particular is NOT restored to
        needs_input: the history file holds no question text (by design), and
        needs_input is the one state _resolve never ages away - so a restored one would
        alert forever, with nothing to say and no way to clear it.
        """
        now = time.time()
        done_cutoff = now - RECAP_DONE_KEEP_SEC
        event_cutoff = now - HISTORY_REPLAY_SEC
        # sid -> (ts, state) of the newest TERMINAL event replayed for it. Applied after
        # the loop rather than inside it: a later non-terminal event (a new prompt on a
        # session that finished and was picked up again) must UNDO the restore, and the
        # entries are only guaranteed to be in ts order, not to end on the interesting one.
        terminal: dict[str, tuple[float, str]] = {}
        with self._lock:
            for ts, kind, sid, title in entries:
                if title:
                    self._titles[sid] = title
                if kind == HISTORY_DONE_KIND:
                    if ts >= done_cutoff:
                        self.dones.append((ts, sid))
                    continue
                if ts < event_cutoff:
                    continue
                row = self.sessions.setdefault(sid, self._blank(ts))
                row["at"] = max(row["at"], ts)
                # Entries arrive oldest first, so head-inserting each one leaves the
                # ring newest-first - the same order a live run produces.
                self._note_event(row, kind, ts, persist=False)
                if self.REPLAY_TERMINAL.get(kind) and ts >= terminal.get(sid, (0.0,))[0]:
                    terminal[sid] = (ts, self.REPLAY_TERMINAL[kind])
                elif sid in terminal and ts >= terminal[sid][0]:
                    del terminal[sid]
            for sid, (ts, state) in terminal.items():
                row = self.sessions[sid]
                row["state"] = state
                row["since"] = ts
                # No label: _sessions falls back to _implied_event, so a restored card
                # reads "finished" rather than a hook label this process never saw.
                row["last_event"] = None

    def done_today(self, now: float | None = None) -> int:
        """recap.doneToday - DISTINCT sessions that reached `done` since local midnight.

        Since v0.7.0 the ring is rebuilt from ~/.sidecrab/history.jsonl at startup, so a
        restart no longer resets this to 0 (the contract retires the "floor" caveat for
        restarts). What is still unknowable stays unknowable: finishes from before the
        file existed, and finishes from a session crabd never saw a hook for. Neither is
        reconstructed from a transcript that merely stopped growing.
        """
        return len(self.done_ids(now))

    def done_ids(self, now: float | None = None) -> set[str]:
        """The distinct session ids behind done_today. Exposed as a SET (CD-11) because
        recap has to reconcile two counts that were derived independently: sessionsToday
        comes from the transcript scan and doneToday from here, and a set is what lets
        the builder take the union instead of comparing two numbers it cannot align."""
        midnight = _local_midnight(now if now is not None else time.time())
        with self._lock:
            return {sid for at, sid in self.dones if at >= midnight}

    def done_by_day(self, now: float | None = None,
                    days: int = WEEK_DAYS) -> list[tuple[str, int]]:
        """recap.week's `done` half: (local day, distinct sessions finished), oldest
        first, one entry per day including the empty ones.

        Bucketed on the LOCAL day STRING rather than on epoch ranges: a DST change makes
        one day 23 or 25 hours long, and a day whose commits bucket by wall clock while
        its finishes bucket by fixed arithmetic is a row that disagrees with itself.
        """
        now = now if now is not None else time.time()
        wanted = [_local_day(start) for start in _local_day_starts(now, days)]
        index: dict[str, set] = {day: set() for day in wanted}
        with self._lock:
            for at, sid in self.dones:
                bucket = index.get(_local_day(at))
                if bucket is not None:
                    bucket.add(sid)
        return [(day, len(index[day])) for day in wanted]

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            out = {}
            for sid, row in self.sessions.items():
                copy = dict(row)
                copy["stops"] = list(row["stops"])
                copy["events"] = list(row["events"])
                out[sid] = copy
            return out

    def prune(self, now: float) -> None:
        with self._lock:
            for row in self.sessions.values():
                row["stops"] = [t for t in row["stops"] if now - t < SUBAGENT_ACTIVE_SEC]
            # The done ring only ever answers "today", so anything past the margin is
            # dead weight in a process that runs for weeks.
            if self.dones:
                cutoff = now - RECAP_DONE_KEEP_SEC
                self.dones = [d for d in self.dones if d[0] >= cutoff]
            dead = [
                sid for sid, row in self.sessions.items()
                # CD-09 (v0.21.0): the test is "can this row still reach a served row",
                # and the answer turns on GONE_AFTER_SEC for EVERY state, not only the
                # two this used to name. A row left on `working` or `done` - which is
                # how a session ends whenever no SessionEnd hook arrives, the ordinary
                # case for a closed terminal - was never eligible here, so it and its
                # events ring and its `_titles` entry were resident for the life of a
                # daemon meant to run for weeks. _resolve already retires both (`done`
                # at DONE_DROP_SEC, `working` at GONE_AFTER_SEC), so past this horizon
                # they are table growth exactly as `gone` and `None` are.
                #
                # needs_input is the ONE exemption, and it is the contract's: a question
                # keeps waiting even when the transcript goes quiet, so its row must
                # outlive the horizon that would drop any other.
                if row["state"] != "needs_input" and now - row["at"] > GONE_AFTER_SEC
            ]
            for sid in dead:
                del self.sessions[sid]
            # A-05 (v0.26.0): needs_input keeps its exemption from GONE_AFTER_SEC above, but
            # not from EVERY bound - unbounded it let a hook flood or a pile of abandoned
            # questions grow the tracker, `_titles` and the served array forever. Two
            # generous, oldest-first ceilings, applied ONLY to needs_input rows, so a
            # genuinely recent waiting question is never touched:
            #   1. AGE: a row with no activity for longer than NEEDS_INPUT_MAX_AGE_SEC is
            #      stale - far past any real waiting window - and drops.
            ni_dead = [sid for sid, row in self.sessions.items()
                       if row["state"] == "needs_input"
                       and now - row["at"] > NEEDS_INPUT_MAX_AGE_SEC]
            for sid in ni_dead:
                del self.sessions[sid]
            #   2. COUNT: past NEEDS_INPUT_MAX_ROWS live needs_input rows, evict the OLDEST
            #      by `at` first (LRU). A fresh prompt has the newest `at`, so it survives
            #      while an abandoned/acked one - which stopped moving `at` long ago - is
            #      the one dropped. The healthy-night rule is the sort direction.
            ni_rows = [(row["at"], sid) for sid, row in self.sessions.items()
                       if row["state"] == "needs_input"]
            if len(ni_rows) > NEEDS_INPUT_MAX_ROWS:
                ni_rows.sort()      # oldest `at` first
                for _, sid in ni_rows[:len(ni_rows) - NEEDS_INPUT_MAX_ROWS]:
                    del self.sessions[sid]
            if self._titles:
                live = set(self.sessions) | {sid for _, sid in self.dones}
                for sid in [s for s in self._titles if s not in live]:
                    del self._titles[sid]


# ---------------------------------------------------------------------- platform
#
# Everything crabd knows about the OS it is running on, behind ONE interface with three
# implementations. The readers above and below (HostSampler, FleetReader, LimitsReader)
# each take a `platform=` and default to PLATFORM, so none of them contains an OS test:
# they own their arithmetic and their honest-failure rules, the platform owns the
# syscall. A reader that reaches for the Win32 DLLs or `schtasks` directly is the bug
# this section exists to prevent - it is unreachable, and therefore untested, on the
# host most of this suite runs on.
#
# The three classes are INTERCHANGEABLE by contract, pinned by tests that compare their
# public method sets, signatures and binding. Adding a method to one alone ships an
# AttributeError to every other OS, in a daemon whose one promise is to keep serving.
#
# `cpu_times` and `memory` are INSTANCE methods on all three (everything else is static)
# because the macOS CPU reader keeps state: the mach tick counters are 32-bit and wrap,
# and unwrapping them needs the last raw reading per bucket. The other two classes carry
# no state and would be happy as staticmethods - they are instance methods anyway so the
# binding stays identical across the three, which is the seam's whole promise and is
# pinned by a test. PLATFORM is a module singleton, so the accumulators live as long as
# the process; a reader that built a fresh DarwinPlatform per sample would lose them.
#
# A platform with no service manager RAISES out of service_query rather than returning
# None: the caller unpacks the result, so a bare None would be a TypeError past
# FleetReader's catch list - a crash where an `unknown` belongs.
#
# The one deliberate non-raise is FLEET_NO_SERVICE, `(None, "", "")`: a component the
# platform NAMES but has no service for at all (macOS glow, since there is no lighting
# component there). It unpacks like any other answer, and the None CODE - which no
# process can exit with - is what service_status reads as `absent`. FleetReader
# short-circuits an empty target onto that sentinel before any runner is called, so a
# platform whose targets are ALL empty never reaches its own service_query at all.


def _read_cli_credentials() -> str | None:
    """The CLI credential document's raw text, or None when there is no file. Portable,
    so all three platforms delegate here rather than carrying a copy each.

    CREDENTIALS_FILE is read off the MODULE per call, never bound at import: every test
    module repoints it, and a binding taken at import would send the suite at the
    operator's live OAuth token and then at the network.
    """
    try:
        return CREDENTIALS_FILE.read_text(encoding="utf-8")
    except OSError:
        return None


def _usable_limits_token(token) -> bool:
    """Is this something crabd is willing to store? SHAPE only - see LIMITS_TOKEN_RE -
    and the value is never named in a log line or an exception on the way out."""
    return isinstance(token, str) and bool(LIMITS_TOKEN_RE.fullmatch(token))


def _keychain_name_safe(value) -> bool:
    """A service or account name crabd is willing to put in a `security -i` command.

    That command is ONE LINE with two QUOTED fields in it, so a `"` closes its field
    early, a `\\` escapes the quote that would have closed it, and a control character can
    end the line. Neither name is attacker-controlled today - the account is the login
    user's own and the service is a constant in this file - which is why this is a check
    rather than a crisis: it keeps that from being the only thing between the two.
    """
    return (isinstance(value, str) and bool(value)
            and not any(ch in '"\\' or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value))


def _login_account() -> str | None:
    """The login user name - the ACCOUNT half of both Keychain items - or None.

    `pwd.getpwuid(os.getuid())` rather than $USER: the environment of a LaunchAgent is
    not the one a terminal has, and an account name that does not match the one the items
    were created under simply finds nothing. None off POSIX, where there is no Keychain
    to name anyway.
    """
    if pwd is None or not hasattr(os, "getuid"):
        return None
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except (KeyError, OSError):
        return None


def _run_security(argv: list[str], stdin_text: str | None, timeout: float):
    """`/usr/bin/security` -> (exit code, stdout, stderr). The ONE place crabd spawns it.

    A SECRET NEVER TRAVELS IN `argv`. `ps` is world-readable on macOS, so an argument
    list is a broadcast: the store command therefore goes in on STDIN, to `security -i`,
    and the reads (whose argv names only the item, and whose secret comes back on stdout)
    are the only ones that use an argument list at all.

    BYTES IN, BYTES OUT, decoded here with `errors="replace"`. `text=True` decodes with
    the locale's codec and RAISES UnicodeDecodeError on a byte that codec cannot read -
    and UnicodeDecodeError is a ValueError, which is in neither of the except tuples that
    guard the two callers. It would come out of cli_credentials, out of the limits fetch,
    and out of build() on the refresh thread. Nothing crabd asks for should produce one
    (JSON payload, ASCII item names), and "should" is exactly why this is not left to the
    locale: a Keychain item somebody else wrote is not crabd's to make promises about.
    """
    proc = subprocess.run(
        [SECURITY_BIN, *argv],
        input=None if stdin_text is None else stdin_text.encode("utf-8"),
        capture_output=True, timeout=timeout, check=False)
    return (proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"))


class WindowsPlatform:
    """GetSystemTimes / GlobalMemoryStatusEx, schtasks, DPAPI."""

    name = "windows"

    def cpu_times(self) -> tuple[int, int, int] | None:
        """GetSystemTimes -> (idle, kernel, user) in 100 ns ticks since boot, or None.
        Cumulative counters, raw; every rule for turning them into a percentage - the
        kernel-includes-idle trap among them - is HostSampler's.

        `ctypes.windll` does not exist off Windows, so the AttributeError below is the
        error path for a host that selected this platform anyway (a test does) as well
        as for a real syscall failure.
        """
        idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
        try:
            ok = ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        except (AttributeError, OSError, ValueError) as exc:
            _log_once(HOST_CPU_LOG_KEY,
                      f"crabd: GetSystemTimes unavailable ({type(exc).__name__}); "
                      f"serving no host CPU")
            return None
        if not ok:
            _log_once(HOST_CPU_LOG_KEY,
                      "crabd: GetSystemTimes returned failure; serving no host CPU")
            return None
        return (_filetime(idle), _filetime(kernel), _filetime(user))

    def memory(self) -> tuple[int, int] | None:
        """GlobalMemoryStatusEx -> (total physical bytes, available bytes), or None."""
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        try:
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        except (AttributeError, OSError, ValueError) as exc:
            _log_once(HOST_MEM_LOG_KEY,
                      f"crabd: GlobalMemoryStatusEx unavailable ({type(exc).__name__}); "
                      f"serving no host memory")
            return None
        if not ok:
            _log_once(HOST_MEM_LOG_KEY,
                      "crabd: GlobalMemoryStatusEx returned failure; "
                      "serving no host memory")
            return None
        return (int(status.ullTotalPhys), int(status.ullAvailPhys))

    @staticmethod
    def server_reuse_address() -> bool:
        """False. Windows SO_REUSEADDR lets a SECOND process bind a port that is
        already being listened on, and the two servers then answer half the requests
        each - the split feed measured during build QA. Refusing reuse is what turns
        that into a loud "already running"."""
        return False

    @staticmethod
    def port_holder_hint(port: int) -> str:
        """The command that names what is holding `port`. PowerShell, because `lsof`
        does not exist here - and this string's whole job is to be runnable."""
        return (f"Get-NetTCPConnection -LocalPort {port} -State Listen "
                f"| Select-Object OwningProcess")

    @staticmethod
    def fleet_targets() -> tuple[tuple[str, str], ...]:
        return FLEET_TASKS

    @staticmethod
    def service_query(target: str, timeout: float):
        proc = subprocess.run(
            ["schtasks", "/query", "/tn", target, "/fo", "csv", "/nh"],
            capture_output=True, timeout=timeout, check=False,
            # No console under the Scheduled Task, and without this a window would
            # flash on the desktop on an interactive login.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (proc.returncode,
                proc.stdout.decode("utf-8", errors="replace"),
                proc.stderr.decode("utf-8", errors="replace"))

    @staticmethod
    def service_status(code, out, err) -> str:
        if code is None:
            # The no-such-component sentinel, answered EXPLICITLY. Without this branch it
            # fell into the `code != 0` path below and scanned an empty blob for a
            # not-found marker - `unknown` by accident, and `absent` by accident on the
            # day some stderr happened to carry one of those words.
            return "absent"
        if code != 0:
            blob = f"{out or ''}\n{err or ''}".lower()
            return "absent" if any(m in blob for m in FLEET_ABSENT_MARKERS) else "unknown"
        return FLEET_STATUS_MAP.get(WindowsPlatform._status_field(out), "unknown")

    @staticmethod
    def _status_field(out) -> str:
        """Last csv row's status column. `csv` rather than a split: the task name is a
        quoted field and a task name containing a comma would break a naive split."""
        try:
            rows = [row for row in csv.reader((out or "").splitlines())
                    if len(row) > FLEET_STATUS_COL]
        except (csv.Error, ValueError):
            return ""
        return rows[-1][FLEET_STATUS_COL].strip().lower() if rows else ""

    def read_limits_token(self, path) -> str | None:
        """The long-lived usage token, or None. Read fresh on every call so a token
        stored while crabd runs is picked up on the next poll; the decrypted string is
        returned to the caller and dropped - LimitsReader keeps the same no-log,
        no-store rule for it that it keeps for the CLI token.

        `_dpapi_unprotect` is looked up on the MODULE at call time, not bound at import:
        the suite stubs `crabd._dpapi_unprotect` to reach this path off Windows.
        """
        try:
            blob = path.read_bytes()
        except OSError:
            return None
        raw = _dpapi_unprotect(blob)
        if not raw:
            return None
        token = raw.decode("utf-8", errors="replace").strip()
        return token or None

    def store_limits_token(self, token: str) -> bool:
        """Store the long-lived token DPAPI-protected in LIMITS_TOKEN_FILE. True when it
        is on disk; False - having written nothing - for anything else.

        The path is read off the MODULE per call, like every other path in this file, so
        the suite can point it somewhere harmless.

        ATOMIC, and 0600 from the moment the bytes exist: a temp file in the same
        directory (so the rename cannot cross a volume) opened with the mode already set,
        then os.replace. A store that crashed half way through the old shape left a
        truncated blob that decrypts to nothing, which reads exactly like "no token
        stored" and would have sent the operator to store it again.
        """
        if not _usable_limits_token(token):
            return False
        blob = _dpapi_protect(token.encode("utf-8"))
        if not blob:
            _log_once(LIMITS_TOKEN_LOG_KEY,
                      "crabd: DPAPI would not protect the limits token; nothing was "
                      "stored; this is logged once")
            return False
        path = LIMITS_TOKEN_FILE
        temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(temp, "wb", opener=lambda p, flags: os.open(p, flags, 0o600)) as fh:
                fh.write(blob)
            os.replace(temp, path)
        except OSError as exc:
            _log_once(LIMITS_TOKEN_LOG_KEY,
                      f"crabd: the limits token could not be written "
                      f"({type(exc).__name__}); nothing was stored; this is logged once")
            try:
                temp.unlink()
            except OSError:
                pass
            return False
        return True

    def limits_token_hint(self) -> str:
        """The command that stores a long-lived token HERE. Three of LimitsReader's notes
        end with it, and a note whose whole job is to say what to do next is worth
        nothing if it names a tool this platform does not have."""
        return "Install-SideCrab.ps1 -LimitsToken"

    def cli_credentials(self) -> str | None:
        """The CLI credential document, from the FILE and nowhere else.

        An INSTANCE method, like the other two platforms' - only Darwin needs the
        instance (its Keychain seam lives on it), and the three have to be bound the
        same way or they are not interchangeable. The surface test pins that.
        """
        return _read_cli_credentials()


#: None = not looked up yet; False = looked up and NOT THERE; anything else is the
#: loaded library. The false sentinel is what keeps a host without libSystem from
#: re-running the dyld search twice a pass for the life of the process.
_DARWIN_LIBC = None


def _darwin_libc():
    """libSystem, resolved once, with every entry point crabd calls DECLARED.

    `find_library` is a dyld search - not free, and this is on a 2 s cadence - so the
    ANSWER IS REMEMBERED EITHER WAY. Off macOS the load or the lookup fails, that failure
    is remembered too, and the callers below turn the raise into the same None a failed
    syscall gives. (Unlike the CLK_TCK read, a missing libSystem is not a transient: the
    library a process has is fixed for its life.)

    THE DECLARATIONS ARE NOT DECORATION. ctypes defaults both `argtypes` and `restype` to
    `c_int`, and `mach_port_t` is UNSIGNED 32-bit: a host port at or above 2^31 does not
    fit the default signed conversion on the way IN to host_statistics, which is where
    the failure would land - on the machines whose port happens to have the high bit set
    and not on the others. The restype is the same fact one step later. The pointer
    arguments are `c_void_p`, which is what a `byref()` is handed to the kernel as, and
    it stops ctypes guessing at an `int` for the address.
    """
    global _DARWIN_LIBC
    if _DARWIN_LIBC is False:
        raise OSError("libSystem is not available on this host")
    if _DARWIN_LIBC is None:
        try:
            # No use_errno: nothing here reads errno - every one of these calls reports
            # its own failure in its return value - and asking ctypes to save and restore
            # it around each call buys a cost and no information.
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib")
            libc.mach_host_self.restype = ctypes.c_uint32
            libc.mach_host_self.argtypes = []
            for name in ("host_statistics", "host_statistics64"):
                entry = getattr(libc, name)
                entry.restype = ctypes.c_int          # kern_return_t
                entry.argtypes = [ctypes.c_uint32,    # host_t / host_priv_t
                                  ctypes.c_int,       # the flavour
                                  ctypes.c_void_p,    # the out struct
                                  ctypes.c_void_p]    # the in/out word count
            libc.sysctlbyname.restype = ctypes.c_int
            libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_size_t]
        except Exception:
            # Every shape of "not this platform" - no such library, no such symbol - and
            # the FIRST one is re-raised so the caller's log line names it. After that
            # the sentinel answers, with no search behind it.
            _DARWIN_LIBC = False
            raise
        _DARWIN_LIBC = libc
    return _DARWIN_LIBC


_DARWIN_HOST_PORT = None


def _darwin_host_port() -> int:
    """The mach host port, resolved ONCE for the life of the process.

    `mach_host_self()` takes a send right and returns a REFERENCE to it, and nothing here
    ever gives one back (`mach_port_deallocate`), so each call adds one uref to this
    task's right. MEASURED 2026-09-04: 2 urefs after one call, 1002 after 1001 - one per
    call, exactly. The endpoint is undramatic (years of two-second passes to
    MACH_PORT_UREFS_MAX, and past it host_statistics fails and the gauges null out the
    honest way), but it is a counter climbing for the life of a daemon meant to run for
    months, over a value that cannot change: the host port is a property of the task.
    """
    global _DARWIN_HOST_PORT
    if _DARWIN_HOST_PORT is None:
        _DARWIN_HOST_PORT = _darwin_libc().mach_host_self()
    return _DARWIN_HOST_PORT


def _darwin_cpu_load_info() -> tuple[int, int, int, int] | None:
    """`host_statistics(HOST_CPU_LOAD_INFO)` -> (user, system, idle, nice) raw ticks.

    MEASURED 2026-09-04 (macOS 26.6, 16 cores): kr 0, count 4, four natural_t counters
    cumulative since boot and SUMMED ACROSS CORES, in 1/CLK_TCK s. One second of wall
    clock moved them by [213, 103, 1280, 0] - about 16 cores x 100 Hz.

    The array order is the CPU_STATE_* indices, which is NOT the order the caller wants:
    user, system, idle, nice. Handed on raw; the folding is cpu_times'.
    """
    libc = _darwin_libc()
    info = (ctypes.c_uint32 * HOST_CPU_STATES)()
    count = ctypes.c_uint32(HOST_CPU_STATES)
    kr = libc.host_statistics(_darwin_host_port(), HOST_CPU_LOAD_INFO,
                              ctypes.byref(info), ctypes.byref(count))
    if kr != 0 or count.value != HOST_CPU_STATES:
        return None
    return (int(info[0]), int(info[1]), int(info[2]), int(info[3]))


class _VM_STATISTICS64(ctypes.Structure):
    """The host_statistics64(HOST_VM_INFO64) out-parameter.

    Every field is declared, in order, even the ones nothing here reads: the kernel
    writes as many 32-bit words as the in/out `count` allows, and a short struct would
    be a write past the end. 38 words (152 bytes) as measured, which is also what the
    caller passes in as the count and checks on the way back out - a kernel whose layout
    differs answers a different count, and the fields would then not be where the
    offsets below say they are.
    """
    _fields_ = [("free_count", ctypes.c_uint32),
                ("active_count", ctypes.c_uint32),
                ("inactive_count", ctypes.c_uint32),
                ("wire_count", ctypes.c_uint32),
                ("zero_fill_count", ctypes.c_uint64),
                ("reactivations", ctypes.c_uint64),
                ("pageins", ctypes.c_uint64),
                ("pageouts", ctypes.c_uint64),
                ("faults", ctypes.c_uint64),
                ("cow_faults", ctypes.c_uint64),
                ("lookups", ctypes.c_uint64),
                ("hits", ctypes.c_uint64),
                ("purges", ctypes.c_uint64),
                ("purgeable_count", ctypes.c_uint32),
                ("speculative_count", ctypes.c_uint32),
                ("decompressions", ctypes.c_uint64),
                ("compressions", ctypes.c_uint64),
                ("swapins", ctypes.c_uint64),
                ("swapouts", ctypes.c_uint64),
                ("compressor_page_count", ctypes.c_uint32),
                ("throttled_count", ctypes.c_uint32),
                ("external_page_count", ctypes.c_uint32),
                ("internal_page_count", ctypes.c_uint32),
                ("total_uncompressed_pages_in_compressor", ctypes.c_uint64)]


#: The four page counts the Activity Monitor formula needs, plus the reply's own count.
HOST_VM_FIELDS = ("wire_count", "purgeable_count", "compressor_page_count",
                  "internal_page_count")
#: Taken FROM the struct rather than written down beside it, so the capacity the call
#: passes in and the drift check it makes on the way out can never disagree. Measured 38.
HOST_VM_STAT_WORDS = ctypes.sizeof(_VM_STATISTICS64) // 4


def _darwin_vm_statistics64() -> dict | None:
    """`host_statistics64(HOST_VM_INFO64)` -> the page counts, plus the `count` the
    kernel reported writing. MEASURED 2026-09-04: kr 0, count 38."""
    libc = _darwin_libc()
    stats = _VM_STATISTICS64()
    count = ctypes.c_uint32(HOST_VM_STAT_WORDS)
    kr = libc.host_statistics64(_darwin_host_port(), HOST_VM_INFO64,
                                ctypes.byref(stats), ctypes.byref(count))
    if kr != 0:
        return None
    out = {name: int(getattr(stats, name)) for name in HOST_VM_FIELDS}
    out["count"] = int(count.value)
    return out


def _darwin_sysctl(name: str) -> int | None:
    """One named integer sysctl, or None when the name is unknown. MEASURED: hw.memsize
    answers 8 bytes, vm.pagesize 4 - both are read into the same zeroed 64-bit buffer,
    which is why the width is taken from the size the call reports back."""
    libc = _darwin_libc()
    value = ctypes.c_uint64(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    rc = libc.sysctlbyname(name.encode("ascii"), ctypes.byref(value),
                           ctypes.byref(size), None, ctypes.c_size_t(0))
    if rc != 0 or size.value not in (4, 8):
        return None
    return int(value.value) if size.value == 8 else int(value.value & 0xFFFFFFFF)


class DarwinPlatform:
    """macOS: mach host statistics, launchd, and no long-lived token store.

    The two host readers go through injectable seams (`load_info`, `vm_stats`, `sysctl`,
    `clk_tck`) rather than patched ctypes, for the same reason HostSampler takes
    callables: the arithmetic is the part with the traps in it, and it is unreachable if
    a test has to own a real kernel to get to it. Production passes nothing and the
    module-level `_darwin_*` helpers answer.
    """

    name = "darwin"

    def __init__(self, load_info=None, vm_stats=None, sysctl=None,
                 clk_tck=None, security=None, limits_service=None,
                 custom_claude_home=None) -> None:
        self._load_info = load_info or _darwin_cpu_load_info
        self._vm_stats = vm_stats or _darwin_vm_statistics64
        self._sysctl = sysctl or _darwin_sysctl
        self._clk_tck = clk_tck
        # The Keychain, behind one seam: `security` is a SUBPROCESS, and a test that had
        # to spawn the real one could neither run off macOS nor be trusted not to touch
        # the operator's own items. `limits_service` is parametrised for the one live
        # test that really does write a Keychain, so it can write its own item.
        self._security = security or _run_security
        self._limits_service = limits_service or KEYCHAIN_LIMITS_SERVICE
        # KEPT AS GIVEN, resolved per call: None means "ask the module", and the module
        # global is read at call time like every other one in this file, so a test can
        # repoint it in setUpModule. Bound here instead, the answer would belong to
        # whenever the platform was built - and the platform that matters is built at
        # import, as PLATFORM.
        self._custom_claude_home = custom_claude_home
        # The resolved 100 ns-units-per-tick scale, once SC_CLK_TCK has answered
        # usefully. See _tick_scale: only an answer is remembered.
        self._scale: int | None = None
        # The 32-bit unwrap, per bucket: the last RAW value seen, and how many whole
        # 2^32 laps have been added to it.
        #
        # UNDER A LOCK THAT SPANS THE FETCH AS WELL AS THE UNWRAP, because cpu_times runs
        # on two threads: at cold start `_do_state` builds on the REQUEST thread while
        # `_refresh_loop` builds its own first snapshot, and HostSampler calls its reader
        # OUTSIDE the lock that guards its `_prev`. Unserialised, thread A can fetch the
        # older ticks, thread B fetch newer ones and unwrap first - moving the baseline
        # forward - and A then unwrap ITS reading against a baseline newer than itself.
        # Every bucket reads smaller, the unwrap cannot tell that from a wrap (by design,
        # see _unwrap), and four laps are added to a perfectly ordinary reading. Nothing
        # downstream catches it: the deltas stay positive and idle stays under the total,
        # so A-07 and A-08 both pass and the panel is served a percentage the machine
        # never produced.
        self._cpu_lock = threading.Lock()
        self._cpu_last: list[int] | None = None
        self._cpu_laps = [0] * HOST_CPU_STATES

    def cpu_times(self) -> tuple[int, int, int] | None:
        """(idle, kernel, user) in 100 ns units, HostSampler's convention, or None.

        TWO DECISIONS LIVE HERE, and both are silent if got wrong:

        IDLE IS FOLDED INTO KERNEL. HostSampler was written against GetSystemTimes,
        where kernel time INCLUDES idle time, and its busy fraction is
        ((kernel + user) - idle) / (kernel + user). mach reports the two separately, so
        handing `system` on as `kernel` would make idle larger than the total on any
        machine that is mostly idle - which is A-08, and the sampler answers null.
        Forever, on a healthy machine: a dead gauge, not a wrong one.

        NICE IS BUSY TIME. `nice` counts user-priority-lowered processes running, so it
        belongs with `user`; dropping it under-reports a machine doing background work
        at nice priority (a box running a nice'd build reads idle while it is not).
        """
        scale = self._tick_scale()
        if scale is None:
            return None
        ticks = self._read_ticks()
        if ticks is None:
            return None
        user, system, idle, nice = ticks
        return (idle * scale, (system + idle) * scale, (user + nice) * scale)

    def _read_ticks(self) -> list[int] | None:
        """The four counters, unwrapped, or None - with the FETCH AND THE UNWRAP UNDER
        ONE LOCK. Two callers overlap at cold start (see the __init__ comment), and a
        reading unwrapped against a baseline newer than itself invents four laps that
        nothing downstream can tell from a real wrap."""
        with self._cpu_lock:
            try:
                raw = self._load_info()
            except Exception as exc:
                _log_once(HOST_CPU_LOG_KEY,
                          f"crabd: host_statistics raised {type(exc).__name__}; "
                          f"serving no host CPU")
                return None
            if raw is None:
                _log_once(HOST_CPU_LOG_KEY,
                          "crabd: host_statistics returned failure; serving no host CPU")
                return None
            # Both shapes are the same failure - the four buckets this arithmetic names
            # are not where it thinks they are - so they share an answer and a line.
            try:
                ticks = [int(v) for v in raw]
            except (TypeError, ValueError):
                ticks = None
            if ticks is None or len(ticks) != HOST_CPU_STATES:
                _log_once(HOST_CPU_LOG_KEY,
                          "crabd: host_statistics gave a reading that is not four "
                          "numbers; serving no host CPU")
                return None
            return self._unwrap(ticks)

    def _unwrap(self, ticks: list[int]) -> list[int]:
        """The four 32-bit counters as 64-bit monotonic ones.

        A bucket that reads SMALLER than last time has wrapped 2^32 (about 31 days of
        uptime per bucket at the measured ~1600 ticks/s), so a lap is added and the
        sampler never sees the backwards jump it would otherwise re-baseline on once a
        month per bucket. Each bucket carries its own lap count, so two wrapping in the
        same reading are independent.

        A genuine backwards jump of any OTHER kind - a rigged reader, a counter reset -
        is INDISTINGUISHABLE from a wrap here and is treated as one. That is the honest
        trade: the resulting delta is at worst one over-large window served as a
        percentage, and HostSampler still refuses it if idle then exceeds the total,
        while the alternative (treating every wrap as suspicious) is a null gauge on
        every long-uptime machine.
        """
        last, self._cpu_last = self._cpu_last, list(ticks)
        out = []
        for i, value in enumerate(ticks):
            if last is not None and value < last[i]:
                self._cpu_laps[i] += 1
            out.append(value + self._cpu_laps[i] * HOST_CPU_COUNTER_MODULUS)
        return out

    def _tick_scale(self) -> int | None:
        """100 ns units per CLK_TCK tick, or None.

        CLK_TCK is 100 on every macOS measured, and the scale is then 100_000. It is
        checked rather than assumed because the division is INTEGER: a CLK_TCK that does
        not divide 10_000_000 evenly would quietly drop part of every tick, and one that
        is zero or negative would divide by zero or invert the counters.

        RESOLVED ONCE and remembered: the clock is a property of the kernel, not a
        reading that drifts, and this runs every two seconds for the life of the daemon.
        Only an ANSWER is cached - a sysconf that raised or answered something unusable
        is re-asked next time, because a remembered failure would be a gauge that stays
        dark for the whole process over one bad call.
        """
        if self._scale is not None:
            return self._scale
        clk = self._clk_tck
        if clk is None:
            try:
                clk = os.sysconf("SC_CLK_TCK")
            except (AttributeError, OSError, ValueError) as exc:
                _log_once(HOST_CPU_LOG_KEY,
                          f"crabd: SC_CLK_TCK unreadable ({type(exc).__name__}); "
                          f"serving no host CPU")
                return None
        if (isinstance(clk, bool) or not isinstance(clk, int) or clk <= 0
                or HOST_100NS_PER_SEC % clk):
            _log_once(HOST_CPU_LOG_KEY,
                      f"crabd: SC_CLK_TCK is {clk!r}, which cannot scale the CPU "
                      f"counters; serving no host CPU")
            return None
        self._scale = HOST_100NS_PER_SEC // clk
        return self._scale

    def memory(self) -> tuple[int, int] | None:
        """(total physical bytes, available bytes), or None.

        `used` is ACTIVITY MONITOR's "Memory Used" - app memory + wired + compressed,
        which is (internal_page_count - purgeable_count) + wire_count +
        compressor_page_count. The contract's promise for this row is that it matches
        what the machine's own monitor shows, and on a Mac there are two other plausible
        answers that do not: `top`'s used is total - free, which is 99.3 GiB from the
        page counts recorded in the test fixture (`top` itself rounded it to "98G") on
        the 128 GiB machine measured here, against Activity Monitor's 66.0, and counting
        free + inactive + speculative as available reads differently again. Available is
        then total - used, so the served memPct is the one the user can check.

        Five refusals, each answering None with one stderr line rather than a figure.
        The last two matter most: HostSampler CLAMPS an availability outside 0..total
        back into range, so a `used` past either end would arrive at the document as a
        plausible-looking 100% or 0% instead of the null it is.
        """
        try:
            total = self._sysctl("hw.memsize")
            page = self._sysctl("vm.pagesize")
            stats = self._vm_stats()
            if stats is None:
                return self._no_memory("host_statistics64 returned failure")
            if stats["count"] != HOST_VM_STAT_WORDS:
                # STOP before a single page count is read. A different word count is a
                # different struct layout, so the fields are not at the offsets these
                # names were resolved from and the arithmetic would be confident
                # nonsense rather than an error.
                return self._no_memory(
                    f"host_statistics64 wrote {stats['count']!r} words, not "
                    f"{HOST_VM_STAT_WORDS}")
            if not _positive_int(page) or page & (page - 1):
                return self._no_memory(
                    f"vm.pagesize is {page!r}, which is not a page size")
            if not _positive_int(total):
                return self._no_memory(f"hw.memsize is {total!r}")
            used = page * (stats["internal_page_count"] - stats["purgeable_count"]
                           + stats["wire_count"] + stats["compressor_page_count"])
        except Exception as exc:
            return self._no_memory(f"the memory readers raised {type(exc).__name__}")
        if not 0 <= used <= total:
            return self._no_memory(
                f"used memory reads {used}, which is not within 0..{total}")
        return (total, total - used)

    @staticmethod
    def _no_memory(reason: str) -> None:
        """One stderr line for the life of the process, then silence.

        `_log_once` keys on THIS READER - one key for all five refusals - so the first
        one to fire is the one that speaks and a second kind arriving later is silent.
        That is the trade this reader wants: it runs every two seconds, and a key per
        kind would let a machine alternating between two failures print for ever.
        """
        _log_once(HOST_MEM_LOG_KEY, f"crabd: {reason}; serving no host memory")
        return None

    @staticmethod
    def server_reuse_address() -> bool:
        """True, and it is NOT the Windows setting under another name. BSD
        SO_REUSEADDR does not admit a second listener on an address something is
        already listening on, so a collision still fails loudly here; all it permits is
        a fresh listener taking a port that a CLOSED connection still holds in
        TIME_WAIT. Without it crabd restarted inside that window cannot have its own
        port back, and prints the "another process is holding it" message about its own
        dead connection."""
        return True

    @staticmethod
    def port_holder_hint(port: int) -> str:
        return f"lsof -nP -iTCP:{port} -sTCP:LISTEN"

    @staticmethod
    def fleet_targets() -> tuple[tuple[str, str], ...]:
        # glow has NO launchd label, and that is not an omission: there is no lighting
        # component on macOS at all (the Corsair SDK is Windows-only), so there is
        # nothing to observe and `absent` is the literally true answer. The KEY stays,
        # so the served document's shape is identical on both platforms and a panel that
        # feature-detects `fleet` draws a hollow absent dot rather than a missing row.
        return (("glow", ""), ("toast", "com.sidecrab.toast"))

    @staticmethod
    def service_query(target: str, timeout: float):
        """`launchctl print gui/<uid>/<label>` -> (exit code, stdout, stderr).

        The per-user `gui/<uid>` domain is where the SideCrab agents are loaded; the
        older `launchctl list` answers a different shape and `system/` is a different
        domain. An EMPTY target - a component this platform has no service for - returns
        the FLEET_NO_SERVICE sentinel WITHOUT SPAWNING ANYTHING, because
        `launchctl print gui/<uid>/` is a different question with a different answer.
        service_status turns that sentinel into `absent`; the two halves are one pair.
        """
        if not target:
            return FLEET_NO_SERVICE
        try:
            uid = os.getuid()
        except AttributeError as exc:
            # POSIX-only, and read OUTSIDE the subprocess call. This platform is never
            # SELECTED on a host without it, but the seam lets anything build one (the
            # suite does), and an AttributeError from here lands past FleetReader's catch
            # list and crashes the fleet thread. OSError is the shape that reader already
            # answers `unknown` to, and the one NullPlatform uses to say the same thing.
            raise OSError("os.getuid is not available on this host") from exc
        proc = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{uid}/{target}"],
            capture_output=True, timeout=timeout, check=False)
        return (proc.returncode,
                proc.stdout.decode("utf-8", errors="replace"),
                proc.stderr.decode("utf-8", errors="replace"))

    @staticmethod
    def service_status(code, out, err) -> str:
        """`launchctl print`'s answer as one of the contract's four words.

        Anything the vocabulary does not cover is `unknown`, never `stopped`: an answer
        crabd cannot read is a gap in what it knows, and the fourth word exists so it
        can say so instead of guessing the reassuring one.
        """
        if code is None:
            # The no-such-component sentinel, not a failed query. `absent` is the honest
            # word: there is no service here to be running or stopped.
            return "absent"
        if code != 0:
            blob = f"{out or ''}\n{err or ''}".lower()
            return ("absent" if any(m in blob for m in LAUNCHD_ABSENT_MARKERS)
                    else "unknown")
        return LAUNCHD_STATUS_MAP.get(DarwinPlatform._state_field(out), "unknown")

    @staticmethod
    def _state_field(out) -> str:
        """The FIRST-LEVEL `state = ...` word, or "".

        launchd indents the service's own properties with ONE tab and nests sub-objects
        deeper, and those sub-objects carry their own `state = active` lines - measured,
        two of them under a running agent. A parser taking the first `state =` anywhere,
        or the last, would report a stopped agent as running on the strength of one.
        """
        for line in (out or "").splitlines():
            if not line.startswith("\t") or line.startswith("\t\t"):
                continue
            key, sep, value = line.partition("=")
            if sep and key.strip() == "state":
                return value.strip().lower()
        return ""

    def read_limits_token(self, path) -> str | None:
        """The long-lived token out of the login Keychain, or None.

        `path` IS IGNORED here, deliberately: the three platforms have to take the same
        arguments (the surface pin says so) and on Windows the token really is that file.
        On a Mac it is a generic-password item, service `SideCrab limits token`, account
        the login user - the pair setup/sidecrab_setup.py probes by exit code.

        Read fresh on every poll, so a token stored while crabd runs is picked up on the
        next pass with no restart, and dropped as soon as the caller has used it as a
        header. Exit 44 is ABSENCE and is silent - the ordinary state of a machine whose
        operator never ran `--limits-token`, on every poll for ever. Anything else is one
        line naming the exit code, never the output: in this direction the output IS the
        token.
        """
        if not KEYCHAIN_CREDENTIALS_ENABLED:
            return None                 # the suite's kill switch: it covers both items
        code, out, why = self._keychain_read(self._limits_service)
        if code == KEYCHAIN_ITEM_NOT_FOUND:
            return None
        if code != 0:
            _log_once(LIMITS_TOKEN_LOG_KEY,
                      f"crabd: the login Keychain would not hand over the limits token "
                      f"({why}); the gauges fall back to the CLI token; this is logged "
                      f"once")
            return None
        return out.strip() or None

    def store_limits_token(self, token: str) -> bool:
        """Store the long-lived token in the login Keychain. True when it is in.

        THE SECRET TRAVELS ON STDIN. `ps` is world-readable on macOS, so a value in an
        argument list is handed to every user on the machine - and to anything sampling
        `ps` for ever after. `security -i` reads its commands from stdin, so the argv here
        is exactly ["-i"], and `-X` takes the value HEX-ENCODED, which removes the last
        question about quoting the secret itself.

        `-U` updates an item that is already there instead of failing on it: storing a
        second token is what an operator does after minting a new one, and the failure
        mode without it is the OLD, rejected token staying in the Keychain.

        The service name is quoted because it has spaces in it. MEASURED 2026-09-04:
        `security -i` honours double quotes - `find-generic-password -s "SideCrab quoting
        probe (no such item)" -a probe -w` fed to it answered "could not be found"
        (exit 44), not a usage error. A value that would need quoting of its own never
        gets this far; _usable_limits_token refuses it.

        Items created through this tool carry the tool in their access list, so crabd's
        own later reads through it do not raise a prompt.
        """
        if not _usable_limits_token(token):
            return False                # nothing stored, and the value never named
        if not KEYCHAIN_CREDENTIALS_ENABLED:
            # The same kill switch the reads honour, and it guards the WRITE for a
            # sharper reason: a suite that could reach this would be modifying the
            # operator's login Keychain, not merely reading it.
            return False
        account = _login_account()
        if account is None:
            _log_once(LIMITS_TOKEN_LOG_KEY,
                      "crabd: there is no login account to name a Keychain item with; "
                      "nothing was stored; this is logged once")
            return False
        if not (_keychain_name_safe(account)
                and _keychain_name_safe(self._limits_service)):
            # The same rule as the token check, one field along: both names go into the
            # command QUOTED, and a name that could close its own field could carry the
            # rest of the line with it.
            _log_once(LIMITS_TOKEN_LOG_KEY,
                      "crabd: this Keychain item's name cannot be quoted safely; "
                      "nothing was stored; this is logged once")
            return False
        command = (f'add-generic-password -a "{account}" -s "{self._limits_service}" '
                   f'-X {token.encode("utf-8").hex()} -U\n')
        try:
            code, _out, _err = self._security(["-i"], command, KEYCHAIN_TIMEOUT_SEC)
        except (OSError, subprocess.SubprocessError) as exc:
            _log_once(LIMITS_TOKEN_LOG_KEY,
                      f"crabd: the login Keychain would not take the limits token "
                      f"({type(exc).__name__}); nothing was stored; this is logged once")
            return False
        if code != 0:
            _log_once(LIMITS_TOKEN_LOG_KEY,
                      f"crabd: the login Keychain would not take the limits token "
                      f"(exit {code}); nothing was stored; this is logged once")
            return False
        return True

    def _custom_config_dir(self) -> bool:
        """Was crabd pointed at a config dir other than ~/.claude? The constructor
        argument outranks the module global, and the global is read HERE rather than
        remembered, for the reason in __init__."""
        if self._custom_claude_home is None:
            return CUSTOM_CLAUDE_HOME
        return bool(self._custom_claude_home)

    def limits_token_hint(self) -> str:
        return "setup/install.sh --limits-token"

    def _keychain_read(self, service: str) -> tuple[int | None, str, str]:
        """`security find-generic-password -s <service> -a <login> -w`
        -> (exit code, stdout, a short reason fit for a log line).

        The code is None when the tool could not be RUN at all (no such binary, a refused
        spawn, a timeout), and the reason is then a type name. Neither the tool's stdout
        nor its stderr ever reaches the reason: stdout is the secret, and stderr is output
        that a future macOS could put anything into.

        No `-i` here. The read carries no secret in its arguments - the item's name is
        not a secret - and the value comes back on stdout, so an argument list is safe in
        this direction and only in this direction.
        """
        account = _login_account()
        if account is None:
            return (None, "", "there is no login account to name")
        try:
            code, out, _err = self._security(
                ["find-generic-password", "-s", service, "-a", account, "-w"],
                None, KEYCHAIN_TIMEOUT_SEC)
        except (OSError, subprocess.SubprocessError) as exc:
            return (None, "", type(exc).__name__)
        return (code, out or "", f"exit {code}")

    def cli_credentials(self) -> str | None:
        """The CLI credential document: the FILE first, then the login Keychain.

        MEASURED 2026-09-04 (Claude Code 2.1.260): `~/.claude/.credentials.json` is not
        on this machine at all and the Keychain item is. The file still wins where both
        exist - the documentation says it is written only when the Keychain write FAILS,
        which makes it the CLI's own fallback - and asking the Keychain anyway would raise
        a prompt on the operator's desktop for an answer crabd already had.

        THE PAYLOAD WAS NEVER READ while this was written: it is the operator's live OAuth
        token, and the session that wrote this refused to look at it. So it is handed back
        as text and parsed as the FILE's shape by the caller that already does that, and
        nothing here guesses - a payload that is not that shape reaches the existing
        "unreadable" and "no access token" notes, never a fabricated token.

        Two gates before the Keychain is touched, both silent absence rather than failure:
        the module kill switch (see KEYCHAIN_CREDENTIALS_ENABLED), and a custom
        CRABD_CLAUDE_HOME, which keys a different Keychain entry crabd cannot name.

        A REFUSED READ RAISES PermissionError. It is not "no credentials": the item is
        there and this process was not allowed to see it, which is what a LaunchAgent
        meets the first time (a Keychain dialog in a GUI session, exit 36 "User
        interaction is not allowed" in one with no UI). The two need different words
        because they have different actions attached - log in, versus approve the prompt.
        """
        raw = _read_cli_credentials()
        if raw is not None:
            return raw
        if not KEYCHAIN_CREDENTIALS_ENABLED or self._custom_config_dir():
            return None
        code, out, why = self._keychain_read(KEYCHAIN_CREDENTIALS_SERVICE)
        if code is None:
            # The tool never RAN - no binary, a refused spawn, a timeout, no login
            # account to name the item with - so crabd learned nothing about whether
            # there are credentials. NOT the refusal below: "approve the Keychain prompt"
            # would be advice about a dialog nobody is being shown, and the operator
            # would wait for something that is never going to appear. One line naming the
            # failure TYPE, since there is no exit code to name.
            _log_once(CLI_CREDENTIALS_LOG_KEY,
                      f"crabd: {SECURITY_BIN} could not be run ({why}); serving no "
                      f"Claude credentials; this is logged once")
            return None
        if code == KEYCHAIN_ITEM_NOT_FOUND:
            return None                 # no file and no item: nothing is logged in here
        if code != 0:
            _log_once(CLI_CREDENTIALS_LOG_KEY,
                      f"crabd: the login Keychain would not hand over the Claude "
                      f"credential ({why}); approve the Keychain prompt or run claude in "
                      f"a terminal; this is logged once")
            raise PermissionError("the login Keychain refused the Claude credential item")
        return out.strip() or None


class NullPlatform:
    """Any other OS (Linux CI). Every OS-specific reading is absent, which the readers
    turn into no `host` key and an unknown fleet - never a fabricated zero."""

    name = "none"

    def cpu_times(self) -> tuple[int, int, int] | None:
        return None

    def memory(self) -> tuple[int, int] | None:
        return None

    @staticmethod
    def server_reuse_address() -> bool:
        """True, for the same reason as Darwin: Linux SO_REUSEADDR does not admit a
        second listener either, and a CI run that restarts crabd back to back is the
        exact TIME_WAIT case."""
        return True

    @staticmethod
    def port_holder_hint(port: int) -> str:
        """The POSIX answer rather than an empty one: Linux is what lands here, and
        `lsof` is the command there too."""
        return f"lsof -nP -iTCP:{port} -sTCP:LISTEN"

    @staticmethod
    def fleet_targets() -> tuple[tuple[str, str], ...]:
        # The contract's two keys are always present; there is no service to name.
        return (("glow", ""), ("toast", ""))

    @staticmethod
    def service_query(target: str, timeout: float):
        raise OSError("no service manager on this platform")

    @staticmethod
    def service_status(code, out, err) -> str:
        """`unknown`, including for the FLEET_NO_SERVICE sentinel - and that difference
        from the other two is the point, not an oversight.

        Windows and macOS answer the sentinel `absent`: they HAVE a service manager, so
        "there is no service for this component" is a fact they can state. This platform
        has none at all, so it cannot observe anything and cannot make that claim; "I
        could not find out" is the only true word it has, and it is the one it gives to
        every question.
        """
        return "unknown"

    def read_limits_token(self, path) -> str | None:
        return None

    def store_limits_token(self, token: str) -> bool:
        """False: there is nowhere to put it here. Not an exception and not a plain
        file - an unprotected bearer token on disk that later reads would hand out as
        though it had been stored properly is worse than saying no. The installer reads
        False as "nothing is confirmed stored"."""
        return False

    def limits_token_hint(self) -> str:
        """No command, because there is nothing here to run one against - and inventing
        one would send an operator to a tool that cannot work on their machine."""
        return "(no long-lived token store on this platform)"

    def cli_credentials(self) -> str | None:
        """The CLI credential document, from the FILE and nowhere else.

        An INSTANCE method, like the other two platforms' - only Darwin needs the
        instance (its Keychain seam lives on it), and the three have to be bound the
        same way or they are not interchangeable. The surface test pins that.
        """
        return _read_cli_credentials()


def select_platform(sys_platform: str):
    if sys_platform == "win32":
        return WindowsPlatform()
    if sys_platform == "darwin":
        return DarwinPlatform()
    return NullPlatform()


#: THE ONLY READ OF THE HOST'S PLATFORM STRING IN THIS MODULE, and a source-text test
#: asserts that it stays the only one. Every OS-specific reader defaults to this object;
#: a second `sys.platform` test anywhere downstream is a second answer that can disagree
#: with this one - and it would be correct on the host it was written on, which is why
#: no behavioural test can catch it.
#:
#: ONE EXCEPTION, deliberate: `_dpapi_unprotect` guards on `hasattr(ctypes, "windll")`.
#: It is a Windows helper, not a reader - it has no cross-platform behaviour to select,
#: and its guard is the same "this syscall is not here" error path the WindowsPlatform
#: counters take. The source-text test names it alongside WindowsPlatform for that
#: reason; nothing else may reach for the Win32 DLLs.
PLATFORM = select_platform(sys.platform)


# ------------------------------------------------------------------------- limits

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(raw: bytes) -> bytes | None:
    """CryptProtectData for the current user, no entropy - the exact mirror of
    `_dpapi_unprotect` below, and of the `[ProtectedData]::Protect(bytes, $null,
    'CurrentUser')` the PowerShell installer used to write this file with. None on any
    failure (not Windows, a refused call), and the caller then stores NOTHING: an
    unprotected token written to that path would be handed out by every later read as
    though it had been encrypted.
    """
    if not raw or not hasattr(ctypes, "windll"):
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(raw, len(raw))
        inp = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = _DATA_BLOB()
        if not crypt32.CryptProtectData(ctypes.byref(inp), None, None, None, None, 0,
                                        ctypes.byref(out)):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)
    except (OSError, AttributeError, ValueError):
        return None


def _dpapi_unprotect(blob: bytes) -> bytes | None:
    """CryptUnprotectData for the current user, no entropy - the exact inverse of
    PowerShell's [ProtectedData]::Protect(bytes, $null, 'CurrentUser'), which is what
    the installer writes. None on any failure (wrong user, tampered, not Windows)."""
    if not blob or not hasattr(ctypes, "windll"):
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_string_buffer(blob, len(blob))
        inp = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        out = _DATA_BLOB()
        if not crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0,
                                          ctypes.byref(out)):
            return None
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(out.pbData)
    except (OSError, AttributeError, ValueError):
        return None


def read_limits_token(path: Path = None, platform=None) -> str | None:
    """The long-lived usage token, or None. Reading it is the platform's job - the store
    is a DPAPI blob on Windows and a login Keychain item on macOS - but this stays a
    module function because it is pre-existing public API. Its name, signature and
    `path=` override predate the platform seam; adding a seam under a name is not a
    reason to change the name.

    THE SHAPE IS CHECKED ON THE WAY OUT, not only on the way in. Whatever the store holds
    was put there by something ELSE - the installer, a `security` command typed by hand,
    an older crabd - and the very next thing that happens to it is that it becomes an
    `Authorization` header. A value with a newline in it is header injection at that
    point, and one with a space is simply not a token. Same rule as the store, and the
    value is still never named in the line that says so.
    """
    token = (platform or PLATFORM).read_limits_token(path or LIMITS_TOKEN_FILE)
    if token is None:
        return None
    if not _usable_limits_token(token):
        _log_once(LIMITS_TOKEN_LOG_KEY,
                  "crabd: the stored limits token is not a shape crabd will send (see "
                  "LIMITS_TOKEN_RE); ignoring it; this is logged once")
        return None
    return token


class LimitsReader:
    """Claude OAuth usage endpoint, cached LIMITS_TTL_SEC.

    HARD RULE: the access token is read, used as a request header, and dropped. It
    is never logged, never stored, and never reaches the /v1/state payload - error
    text served to the widget is composed here, never taken from an exception that
    could echo a request.
    """

    def __init__(self, cache_file: Path | None = None, platform=None) -> None:
        # Injectable for the same reason UserConfig takes a path: a test that builds a
        # real reader must not be one forgotten patch away from writing the operator's
        # live last-good store (it happened - see LIMITS_CACHE_MIN_EPOCH).
        self._cache_file = Path(cache_file) if cache_file else None
        # Both credential sources are the platform's: the CLI document is portable, the
        # long-lived store is a DPAPI blob and exists on Windows only.
        self._platform = platform or PLATFORM
        self._lock = threading.Lock()
        self._cached: dict | None = None
        self._fetched_at = 0.0
        self._last_good: dict | None = None
        self._last_good_at = 0.0
        self._backoff_until = 0.0
        self._consecutive_429 = 0
        self._load_disk_cache()

    @property
    def cache_file(self) -> Path:
        return self._cache_file or LIMITS_CACHE_FILE

    def _load_disk_cache(self) -> None:
        """A crabd restart must not blank the gauges for the length of a 429 lockout -
        that is exactly what happened on-glass 2026-08-26 (restart wiped last-good
        mid-lockout, panel showed em-dashes for the whole quota window).

        An `at` from before LIMITS_CACHE_MIN_EPOCH is rejected outright. A reading
        cannot be dated 1970, so such a file is corrupt, and loading it is worse than
        loading nothing: `now - at` then measures 56 years, which no freshness test can
        interpret. Absent is the honest reading of a nonsense timestamp.
        """
        try:
            saved = json.loads(self.cache_file.read_text(encoding="utf-8"))
            if not (isinstance(saved, dict) and saved.get("limits", {}).get("available")):
                return
            at = float(saved.get("at", 0.0))
            if at < LIMITS_CACHE_MIN_EPOCH:
                return
            self._last_good = saved["limits"]
            self._last_good_at = at
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_disk_cache(self, wall_now: float) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps({"limits": self._last_good, "at": wall_now}), encoding="utf-8")
        except OSError:
            pass

    def get(self, now: float, force: bool = False) -> dict:
        with self._lock:
            if self._cached and not force and now - self._fetched_at < LIMITS_TTL_SEC:
                return self._cached
            # Quota-bucket 429s: Retry-After is 0 there, so back off exponentially.
            # While locked out, a recent good reading beats em-dashes - utilization
            # drifts minutes-slow - up to LIMITS_LAST_GOOD_MAX_AGE, then admit it.
            if now < self._backoff_until and not force:
                return self._serve_during_backoff(now)
        result = self._fetch()
        with self._lock:
            if result.get("available"):
                self._last_good = result
                self._last_good_at = now
                self._backoff_until = 0.0
                self._consecutive_429 = 0
                self._save_disk_cache(now)
            elif "HTTP 429" in (result.get("note") or ""):
                self._consecutive_429 += 1
                delay = min(LIMITS_429_BACKOFF_SEC * (2 ** (self._consecutive_429 - 1)),
                            LIMITS_429_BACKOFF_MAX)
                self._backoff_until = now + max(delay, float(result.pop("_retryAfter", 0) or 0))
                result = self._serve_during_backoff(now, fallback=result)
            self._cached = result
            self._fetched_at = now
        return result

    def _serve_during_backoff(self, now: float, fallback: dict | None = None) -> dict:
        if self._last_good and now - self._last_good_at < LIMITS_LAST_GOOD_MAX_AGE:
            return self._aged(now)
        return fallback or self._cached or self._unavailable(
            "usage endpoint rate-limited - waiting it out")

    def _aged(self, now: float) -> dict:
        """Last-good, QUALIFIED once it is older than LIMITS_NOTE_STALE_SEC.

        Contract v0.4.0: `note` may be non-null while `available` stays true - a caveat
        beside lit gauges, not an error. The note carries an ABSOLUTE local clock time,
        never "12 minutes ago": this dict is then cached for up to LIMITS_TTL_SEC, and a
        relative phrase would quietly become a lie inside that window while a wall-clock
        time stays true forever. Below the threshold nothing is added - a reading minutes
        old is simply what the limits are, and a permanent caveat trains the eye past it.
        """
        if now - self._last_good_at <= LIMITS_NOTE_STALE_SEC:
            return self._last_good
        served = dict(self._last_good)
        served["note"] = "limits as of " + _local_clock(self._last_good_at)
        return served

    @staticmethod
    def _unavailable(note: str) -> dict:
        return {
            "available": False, "note": note,
            "fiveHour": None, "weekly": None, "extra": [],
            "subscriptionType": None, "rateLimitTier": None,
        }

    def _fetch(self) -> dict:
        try:
            raw = self._platform.cli_credentials()
        except PermissionError:
            # macOS only: the credential is in the login Keychain and this process was
            # refused it. A DIFFERENT claim from "there are no credentials", with a
            # different action attached - the operator has to approve the prompt, and
            # being told to log in would send them to do something that changes nothing.
            return self._unavailable(KEYCHAIN_REFUSED_NOTE)
        if raw is None:
            return self._unavailable("no Claude credentials on this machine - run /login")
        try:
            oauth = (json.loads(raw) or {}).get("claudeAiOauth") or {}
        except ValueError:
            return self._unavailable("Claude credentials file is unreadable")
        token = oauth.get("accessToken")
        subscription = oauth.get("subscriptionType")
        tier = oauth.get("rateLimitTier")
        expires_at = oauth.get("expiresAt")
        cli_usable = (isinstance(token, str) and bool(token)
                      and not (isinstance(expires_at, (int, float))
                               and expires_at / 1000.0 < time.time()))
        # v0.30.0: precedence is CLI-when-fresh, else the long-lived token. The CLI
        # token is the one whose scopes are proven against this endpoint every day; the
        # setup token is the fallback for the hours (or days) the CLI file sits expired.
        token_source = "cli"
        if not cli_usable:
            # The command that stores one is the PLATFORM's - `Install-SideCrab.ps1
            # -LimitsToken` on Windows, `setup/install.sh --limits-token` on a Mac. These
            # notes are the panel's only instruction for this failure, and naming a
            # PowerShell script to a Mac operator is naming something they cannot run.
            hint = self._platform.limits_token_hint()
            fallback = read_limits_token(platform=self._platform)
            if fallback:
                token = fallback
                token_source = "sidecrab"
            elif not isinstance(token, str) or not token:
                return self._unavailable(
                    f"no Claude access token - run claude in a terminal, or store a "
                    f"long-lived one: {hint}")
            else:
                out = self._unavailable(
                    f"Claude token expired - run claude in a terminal to refresh it, or "
                    f"store a long-lived one: {hint}")
                out["subscriptionType"] = subscription
                out["rateLimitTier"] = tier
                return out

        request = urllib.request.Request(
            USAGE_URL,
            headers={
                "Authorization": "Bearer " + token,
                "anthropic-beta": USAGE_BETA,
                "Accept": "application/json",
                "User-Agent": f"crabd/{VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=LIMITS_HTTP_TIMEOUT) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code in (401, 403) and token_source == "sidecrab":
                note = (f"SideCrab limits token rejected - mint a new one with "
                        f"claude setup-token and store it again: "
                        f"{self._platform.limits_token_hint()}")
            elif code in (401, 403):
                note = "Claude token rejected - run /login in Claude Code"
            else:
                note = f"usage endpoint returned HTTP {code}"
            out = self._unavailable(note)
            out["subscriptionType"] = subscription
            out["rateLimitTier"] = tier
            if code == 429:
                try:
                    out["_retryAfter"] = float(exc.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    pass
            return out
        except (urllib.error.URLError, OSError, TimeoutError):
            out = self._unavailable("usage endpoint unreachable")
            out["subscriptionType"] = subscription
            out["rateLimitTier"] = tier
            return out
        finally:
            del request, token

        try:
            payload = json.loads(body)
        except ValueError:
            return self._unavailable("usage endpoint returned unparseable data")
        mapped = self.map_payload(payload, subscription, tier)
        # Additive (contract v0.30.0): which token answered. Diagnostic for the operator
        # (`-Status` reads it back off /v1/state); never the token.
        mapped["tokenSource"] = token_source
        return mapped

    @staticmethod
    def _window(obj) -> dict | None:
        if not isinstance(obj, dict):
            return None
        utilization = obj.get("utilization")
        if utilization is None:
            utilization = obj.get("used_percent", obj.get("usedPercent"))
        # CD-10: _finite_number, not isinstance. The old test passed a bool, and
        # `utilization: true` gauged the window at 100% full - see _finite_number.
        value = _finite_number(utilization)
        if value is None:
            return None
        # Endpoint has reported both 0..1 and 0..100 shapes, and this sniff is BLIND at
        # exactly 1.0: a percent-scale `1` (1%) and a fraction `1.0` (100%) are the same
        # number. It fired for real on 2026-09-01 - a Monday-fresh weekly at 1% gauged
        # RED at 100%. map_payload therefore overrides this guess with limits[] `percent`
        # (an unambiguous 0..100 int the same document carries) whenever a matching row
        # exists; this branch remains only for documents without one.
        if value > 1.0:
            value = value / 100.0
        resets = (obj.get("resets_at") or obj.get("resetsAt")
                  or obj.get("reset_at") or obj.get("resetAt"))
        resets_epoch = _parse_ts(resets)
        return {
            "utilization": round(max(0.0, min(value, 1.0)), 4),
            "resetsAt": _utc_iso(resets_epoch) if resets_epoch else None,
        }

    @classmethod
    def map_payload(cls, payload, subscription, tier) -> dict:
        """Mapping measured against live 200s on 2026-08-26 and 2026-09-01: top-level
        `five_hour` / `seven_day` windows (utilization 0..100), a `limits[]` array
        carrying session + weekly_all + scoped-weekly rows (`percent` 0..100 ints -
        the authoritative reading, see the reconciliation below), and an `extra_usage`
        credits block. Top-level junk keys (nimbus_quill, tangelo, ...) exist and must NOT be
        promiscuously gauged; only seven_day_* model windows are accepted from the flat
        namespace. Unknown future shapes fall back to a window-scan so the widget
        degrades to em-dashes rather than lying."""
        if not isinstance(payload, dict):
            return cls._unavailable("usage endpoint returned an unexpected document")
        source = payload
        for nest in ("usage", "data"):
            inner = payload.get(nest)
            if isinstance(inner, dict) and any(isinstance(v, dict) for v in inner.values()):
                source = inner
                break

        five_keys = ("five_hour", "fiveHour", "5h", "five_hour_limit")
        week_keys = ("seven_day", "sevenDay", "weekly", "week", "seven_day_limit")
        five = weekly = None
        for key in five_keys:
            five = cls._window(source.get(key))
            if five is not None:
                break
        for key in week_keys:
            weekly = cls._window(source.get(key))
            if weekly is not None:
                break

        # limits[] `percent` outranks the windows' `utilization` (v0.28.1). Measured
        # live 2026-09-01: `seven_day.utilization: 1.0` MEANT 1% - the same document's
        # `limits[]` said {"kind":"weekly_all","percent":1} - but _window's scale sniff
        # cannot tell a percent-scale 1 from a fraction 1.0 and served 100%. `percent`
        # is a 0..100 int with no ambiguity, so where a session/weekly_all row exists
        # its reading replaces the sniffed one; resets stay from whichever side has one.
        for row in source.get("limits") or []:
            if not isinstance(row, dict):
                continue
            pct = _finite_number(row.get("percent"))
            if pct is None:
                continue
            kind = row.get("kind")
            target = five if kind == "session" else weekly if kind == "weekly_all" else None
            if target is not None:
                target["utilization"] = round(max(0.0, min(pct / 100.0, 1.0)), 4)
                resets_epoch = _parse_ts(row.get("resets_at"))
                if target["resetsAt"] is None and resets_epoch:
                    target["resetsAt"] = _utc_iso(resets_epoch)

        extra = []
        for row in source.get("limits") or []:
            if not isinstance(row, dict) or row.get("kind") != "weekly_scoped":
                continue
            pct = _finite_number(row.get("percent"))   # CD-10, as in _window above
            if pct is None:
                continue
            scope = row.get("scope") or {}
            model = (scope.get("model") or {}).get("display_name") if isinstance(scope, dict) else None
            resets_epoch = _parse_ts(row.get("resets_at"))
            extra.append({
                "label": ("%s weekly" % model) if model else "scoped weekly",
                "utilization": round(max(0.0, min(pct / 100.0, 1.0)), 4),
                "resetsAt": _utc_iso(resets_epoch) if resets_epoch else None,
            })
        for key, value in source.items():
            if key.startswith("seven_day_"):
                window = cls._window(value)
                if window is not None:
                    extra.append({"label": key[len("seven_day_"):] + " weekly", **window})
        credits = source.get("extra_usage")
        if isinstance(credits, dict) and credits.get("is_enabled"):
            window = cls._window(credits)
            if window is not None:
                extra.append({"label": "extra credits", **window})
        if five is None and weekly is None and not extra:
            for key, value in source.items():
                window = cls._window(value)
                if window is not None:
                    extra.append({"label": str(key), **window})
        extra.sort(key=lambda w: w["utilization"], reverse=True)

        subscription = source.get("subscription_type", subscription) or subscription
        tier = source.get("rate_limit_tier", tier) or tier
        if five is None and weekly is None and not extra:
            return cls._unavailable("usage endpoint reported no limit windows")
        return {
            "available": True, "note": None,
            "fiveHour": five, "weekly": weekly, "extra": extra,
            "subscriptionType": subscription, "rateLimitTier": tier,
        }


# -------------------------------------------------------------------- model catalog

class ModelCatalog:
    """`GET /v1/models` -> {api model id: max_input_tokens}, cached MODELS_TTL_SEC.

    Exists for ONE served number: `sessions[].contextWindowTokens` on a session whose
    model id carries no window marker - which is every live session on this host
    (measured 2026-08-28: the transcripts write "claude-fable-5" / "claude-opus-5"
    bare). Without it the widget's ctx-fill bar has no denominator and does not render.

    Same token discipline as LimitsReader, and for the same reason: the access token is
    read from the credentials file, used as a header, and dropped. It is never logged,
    never cached, and never reaches /v1/state - note that this class serves an int and
    an absence and has no `note` field at all, so there is no string here that could
    carry an exception's text out to the widget.

    EVERY failure is the same answer: no entry, so `contextWindowTokens` is null and the
    bar does not draw. No fallback table, no guess, no zero (see the MODELS_URL comment).
    """

    def __init__(self, credentials_file: Path | None = None, platform=None) -> None:
        # Injectable for the reason LimitsReader's cache_file is: a test that builds a
        # real catalog must not be one forgotten patch away from reading the operator's
        # live token and hitting the network.
        self._credentials_file = Path(credentials_file) if credentials_file else None
        # ...and with no file injected, the credential comes from the PLATFORM, which on
        # macOS means the login Keychain. Reading the file directly here is what took the
        # ctx-fill bar off every card on a Mac: the file is not there at all.
        self._platform = platform or PLATFORM
        self._lock = threading.Lock()
        self._windows: dict[str, int] | None = None
        self._fetched_at = 0.0
        self._not_before = 0.0

    @property
    def credentials_file(self) -> Path:
        return self._credentials_file or CREDENTIALS_FILE

    def _credential_document(self) -> str | None:
        """The credential text, or None. An INJECTED file is read directly - it is the
        seam this class's own tests are built on - and otherwise the platform answers,
        which on macOS means the login Keychain as well as the file.

        A refusal (macOS: the item is there and this process may not read it) is the same
        None as every other failure here. The catalog has exactly one failure answer - no
        entry, so `contextWindowTokens` is null and the bar does not draw - and it runs
        inside build(), which must never raise. The platform has already said so once on
        stderr; a second line from here would be the same fact twice.
        """
        if self._credentials_file is not None:
            try:
                return self._credentials_file.read_text(encoding="utf-8")
            except OSError:
                return None
        try:
            return self._platform.cli_credentials()
        except PermissionError:
            return None

    def window(self, model, now: float) -> int | None:
        """The context window for a served model string, or None when unknown.

        Called once per session row inside build(), so the throttle in _ensure has to
        hold WITHIN a build as well as across them - a failed fetch arms _not_before
        before it returns, which is what stops eight session rows becoming eight
        requests.
        """
        base = _model_base_id(model)
        if base is None:
            return None
        self._ensure(now)
        with self._lock:
            return (self._windows or {}).get(base)

    def _ensure(self, now: float) -> None:
        with self._lock:
            fresh = self._windows is not None and now - self._fetched_at < MODELS_TTL_SEC
            if fresh or now < self._not_before:
                return
            # Armed BEFORE the request, not after it: _fetch releases the lock, and two
            # builder threads arriving together would otherwise both go to the network.
            self._not_before = now + MODELS_RETRY_SEC
        windows = self._fetch()
        if not windows:
            # A failed refresh KEEPS whatever is already known rather than blanking it.
            # Unlike a utilization gauge this number does not drift - a model's window is
            # fixed - so a catalog fetched an hour ago is not stale, it is the same
            # answer. Dropping it would put every ctx bar on the panel out for the length
            # of a token expiry, which is the honesty rule pointed at nothing.
            return
        with self._lock:
            self._windows = windows
            self._fetched_at = now
            self._not_before = 0.0

    def _fetch(self) -> dict[str, int] | None:
        raw = self._credential_document()
        if raw is None:
            return None
        try:
            oauth = (json.loads(raw) or {}).get("claudeAiOauth") or {}
        except ValueError:
            return None
        token = oauth.get("accessToken")
        if not isinstance(token, str) or not token:
            return None
        expires_at = oauth.get("expiresAt")
        if isinstance(expires_at, (int, float)) and expires_at / 1000.0 < time.time():
            return None
        request = urllib.request.Request(
            MODELS_URL,
            headers={
                # Measured 2026-08-28 against the live endpoint: this exact pair - the
                # CLI's OAuth bearer plus the oauth beta header the usage endpoint
                # already takes - answers 200 on /v1/models. An API-key header is NOT
                # what this process holds.
                "Authorization": "Bearer " + token,
                "anthropic-beta": USAGE_BETA,
                "anthropic-version": MODELS_API_VERSION,
                "Accept": "application/json",
                "User-Agent": f"crabd/{VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=MODELS_HTTP_TIMEOUT) as response:
                body = response.read().decode("utf-8", errors="replace")
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError):
            # 401 (expired token), 429, DNS, a proxy - all one answer. The caller cannot
            # act on the difference and the operator is told by the bar's absence.
            return None
        finally:
            del request, token
        try:
            return self.map_payload(json.loads(body))
        except ValueError:
            return None

    @staticmethod
    def map_payload(payload) -> dict[str, int] | None:
        """`{"data": [{"id", "max_input_tokens", ...}]}` -> {id: window}.

        `max_input_tokens` ONLY. The sibling `max_tokens` is the output cap (measured
        2026-08-28: 128000 beside a 1000000 input window) and dividing contextTokens by
        it would gauge every card at roughly eight times its real fill - a wrong bar
        reads exactly like a right one, so this must never fall back to it.

        A row whose window is missing or unusable is DROPPED, not defaulted: an id
        absent from this map is a session with no bar, which is the honest rendering.
        One page is fetched (limit=100 against a catalog of ten, measured); a model
        beyond it is simply not in the map.
        """
        if not isinstance(payload, dict):
            return None
        rows = payload.get("data")
        if not isinstance(rows, list):
            return None
        windows: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = row.get("id")
            # _finite_number, not isinstance: `max_input_tokens: true` would otherwise
            # int() to a 1-token window and pin every card on that model at 100% (CD-10).
            size = _finite_number(row.get("max_input_tokens"))
            if not isinstance(model_id, str) or not model_id or size is None or size <= 0:
                continue
            windows[model_id] = int(size)
        return windows or None


# --------------------------------------------------------------------- status line

class StatusLineReader:
    """POST /v1/statusline - the official session document, and the v0.12.0 retirement
    of the OAuth reach-around.

    A chained statusline command posts Claude Code's own stdin document here. Three
    facts are taken from it and nothing else is kept: the rate-limit windows (which
    become `limits`, tagged source "statusline"), how full the session's context window
    is (which becomes that row's contextTokens, tagged contextSource "statusline") and,
    since v0.28.0, how BIG that window is (which becomes contextWindowTokens - the
    denominator the first number fills toward). Everything else in the document - cost,
    prompts, workspace paths, pr identity - is read past. A status line fires on every
    keystroke-ish event; this object is on that path and must stay a dict write.

    Nothing here ever renders a zero for an absent number. `rate_limits` is missing
    entirely on API-key/Bedrock/Vertex sessions and before a session's first API
    response, and that is the normal case, not an error: the reader simply keeps no
    limits and `limits()` returns None, which is the builder's cue to fall back to the
    OAuth endpoint.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._limits: dict | None = None
        self._limits_at = 0.0
        # sessionId -> (contextTokens, contextWindowTokens, seen_at). Either number may
        # be None: a session before its first API call has a context_window block whose
        # current_usage is null, and "the status line told us it is unknown" is a
        # different fact from "the status line has not spoken", which is why the row
        # still exists. The SIZE (v0.28.0) is independent of the fill - a document can
        # carry the window without a usage figure to put in it, and does, on exactly
        # those pre-first-call sessions.
        self._sessions: dict[str, tuple[int | None, int | None, float]] = {}
        self.documents = 0
        # When the last document of ANY kind arrived - /v1/health's
        # lastStatuslineAgeSec. Deliberately NOT _limits_at: health is asking "is the
        # status line command still chained", and an API-key session posts documents
        # forever that carry no windows at all.
        self.last_at: float | None = None

    # -- ingest

    def ingest(self, payload, now: float) -> bool:
        """True when the document carried something worth keeping. Never raises: this
        runs behind a 204 that has already gone out."""
        if not isinstance(payload, dict):
            return False
        with self._lock:
            self.documents += 1
            self.last_at = now
        used = False
        limits = self._map_limits(payload)
        if limits is not None:
            with self._lock:
                self._limits = limits
                self._limits_at = now
            used = True
        session_id = _session_id(payload)
        if session_id:
            context = self._context_tokens(payload.get("context_window"))
            size = self._context_window_size(payload.get("context_window"))
            if context is not None or isinstance(payload.get("context_window"), dict):
                with self._lock:
                    self._sessions[session_id] = (context, size, now)
                used = True
        return used

    @classmethod
    def _map_limits(cls, payload) -> dict | None:
        """`rate_limits` -> the contract's `limits` block, or None when the document
        carries no windows (which is normal - see the class docstring).

        The five-hour and weekly windows are mapped independently: a document may carry
        one and not the other, and half a reading is still better than an em-dash on
        both gauges.
        """
        rate_limits = payload.get("rate_limits")
        if not isinstance(rate_limits, dict):
            return None
        five = cls._window(rate_limits.get("five_hour"))
        weekly = cls._window(rate_limits.get("seven_day"))
        if five is None and weekly is None:
            return None
        extra = []
        for key, value in rate_limits.items():
            # The document has carried seven_day_opus / seven_day_sonnet /
            # seven_day_oauth_apps in the shipped shapes; the plain seven_day is already
            # the weekly gauge, so only the SUFFIXED siblings become extras.
            if not key.startswith("seven_day_"):
                continue
            window = cls._window(value)
            if window is not None:
                extra.append({"label": key[len("seven_day_"):] + " weekly", **window})
        extra.sort(key=lambda w: w["utilization"], reverse=True)
        return {
            "available": True, "note": None,
            "fiveHour": five, "weekly": weekly, "extra": extra,
            # The status line document does not carry the plan name or the tier, and
            # inventing them from the OAuth reading would be a number from one source
            # wearing another source's label. Null is the honest answer; the widget
            # already renders these as optional.
            "subscriptionType": None, "rateLimitTier": None,
        }

    @staticmethod
    def _window(obj) -> dict | None:
        """`{used_percentage, resets_at}` -> `{utilization, resetsAt}`.

        `used_percentage` is divided by 100 UNCONDITIONALLY. LimitsReader._window has to
        sniff ("is it > 1?") because the OAuth endpoint has served both 0..1 and 0..100
        over time; this field never has. Sniffing here would read a genuine 0.4% window
        as 40% full - the gauge would sit near half on a session that has barely
        started. Measured shape, 2.1.246: used_percentage = utilization * 100.
        """
        if not isinstance(obj, dict):
            return None
        # CD-10: NaN and Infinity are refused HERE rather than clamped below. The
        # clamp is total (max/min quietly turn NaN into 0.0 and inf into 1.0), and
        # that is the problem - it renders a garbage field as an empty or a full
        # gauge, both of which read as real measurements of this operator's week.
        percent = _finite_number(obj.get("used_percentage"))
        if percent is None:
            return None
        utilization = max(0.0, min(percent / 100.0, 1.0))
        # Epoch SECONDS per the shipped consumer (Number.isFinite then *1000). _parse_ts
        # also takes ms and ISO, which costs nothing and covers a future reshape.
        resets = _parse_ts(obj.get("resets_at"))
        return {"utilization": round(utilization, 4),
                "resetsAt": _utc_iso(resets) if resets else None}

    @staticmethod
    def _context_tokens(block) -> int | None:
        """`context_window.total_input_tokens` - the same number crabd already computes
        from transcripts, and that is not a coincidence. Measured builder, 2.1.246:

            total_input_tokens: e.input_tokens + e.cache_creation_input_tokens
                                + e.cache_read_input_tokens

        which is contract v6's contextTokens definition exactly. So the two sources are
        interchangeable and the widget's ctx chip does not change meaning when the
        provenance flips.

        Returns None before the first API call (`current_usage` null, totals 0), because
        a real 0-token context and "no request has happened yet" are different facts and
        only one of them should light a chip.
        """
        if not isinstance(block, dict):
            return None
        # CD-10: _finite_number, so `total_input_tokens: 1e309` is "unknown" and not
        # an OverflowError out of `int()`. ingest() promises never to raise - it runs
        # behind a 204 that has already gone out - and this was the one line in it
        # that could.
        total = _finite_number(block.get("total_input_tokens"))
        if total is None:
            return None
        value = int(total)
        if value <= 0 and block.get("current_usage") is None:
            return None
        return max(0, value)

    @staticmethod
    def _context_window_size(block) -> int | None:
        """`context_window.context_window_size` - the DENOMINATOR contextTokens fills
        toward, and the most specific one there is: the CLI states it for this session's
        current model. Measured in the 2.1.250 binary's own schema text:

            "context_window_size": number,  // Context window size for current model
                                            // (e.g., 200000)

        ⚠ On this host it has never actually arrived - /v1/health has statuslineSeen 0
        (measured 2026-08-28), because an app-hosted session renders no status line. It
        is still read FIRST when it does, and StateBuilder._context_window has two
        sources under it for the host where it does not.

        _finite_number for the CD-10 reason: `context_window_size: 1e309` must be
        "unknown" and not an OverflowError out of int(), because ingest() promises never
        to raise. <= 0 is not a window, so it is unknown too - never a zero denominator.
        """
        if not isinstance(block, dict):
            return None
        size = _finite_number(block.get("context_window_size"))
        if size is None or size <= 0:
            return None
        return int(size)

    # -- read

    def limits(self, now: float) -> dict | None:
        """The served `limits` block, or None once the status line has gone silent for
        STATUSLINE_PREFER_SEC (contract: OAuth is the fallback after 10 min).

        Silence is measured from the last document that carried WINDOWS, not from the
        last document of any kind: a session that keeps posting documents with no
        `rate_limits` (an API-key session, say) must not hold the gauges on a reading
        that stopped being refreshed.

        There is deliberately NO "limits as of HH:MM" caveat here, unlike the OAuth
        path. That note exists on the OAuth side because a reading can be served long
        past LIMITS_NOTE_STALE_SEC (900 s) while the endpoint is locked out; this
        reading is DROPPED at STATUSLINE_PREFER_SEC (600 s), which is sooner, so a
        qualification branch here could never fire. A caveat that cannot fire is worse
        than none - it reads as a guarantee that the number is being checked.
        """
        with self._lock:
            if self._limits is None or now - self._limits_at > STATUSLINE_PREFER_SEC:
                return None
            return dict(self._limits)

    def context(self, session_id: str, now: float,
                not_before: float = 0.0) -> tuple[bool, int | None]:
        """-> (the status line knows this session, contextTokens). The bool is what the
        builder needs: it distinguishes "statusline says the context is unknown" from
        "statusline has never mentioned this session", and only the second falls back to
        the transcript arithmetic.

        `not_before` is CD-36 (measured 2026-08-27: a retained 150000 overrode a newer
        transcript 30000). Rows are kept for STATUSLINE_SESSION_KEEP_SEC - two hours -
        and until now that retention alone won, with nothing comparing it against the
        other source. A status line goes quiet the moment its command stops being
        chained, or when a session's own statusline errors, while the transcript keeps
        being written; past that point "the status line spoke about this session" is a
        fact about two hours ago being served as the current window.

        The bool stays FALSE for a reading that loses, not True-with-a-number: the
        caller's whole contract is that False means fall back, and the transcript
        figure is what the caller has.
        """
        entry = self._fresh(session_id, now, not_before)
        return (True, entry[0]) if entry else (False, None)

    def context_window(self, session_id: str, now: float,
                       not_before: float = 0.0) -> int | None:
        """v0.28.0. The window SIZE for this session, or None.

        Deliberately NOT the (known, value) pair `context()` returns. That bool exists so
        "the status line says the fill is unknown" can outrank a transcript figure for
        the same session - two readings of ONE moving quantity, where the fresher source
        wins even when it is blank. A window size has no rival reading: the sources below
        it (the model marker, then the catalog) describe the same model, so a status line
        that carries no size has nothing to assert and simply falls through.

        It takes the same `not_before` freshness contest as `context()` and for a reason
        that survives the size being near-constant: a session that switched models leaves
        a retained row naming the OLD model's window, and the marker/catalog underneath
        would have had the new one right.
        """
        entry = self._fresh(session_id, now, not_before)
        return entry[1] if entry else None

    def _fresh(self, session_id: str, now: float,
               not_before: float) -> tuple[int | None, int | None, float] | None:
        with self._lock:
            entry = self._sessions.get(session_id)
        if entry is None or now - entry[2] > STATUSLINE_SESSION_KEEP_SEC:
            return None
        if not_before and entry[2] < not_before:
            return None
        return entry

    def prune(self, now: float) -> None:
        with self._lock:
            dead = [sid for sid, entry in self._sessions.items()
                    if now - entry[2] > STATUSLINE_SESSION_KEEP_SEC]
            for sid in dead:
                del self._sessions[sid]


# --------------------------------------------------------------------------- OTLP

class OtlpReceiver:
    """POST /v1/metrics + POST /v1/logs - OTLP http/json from Claude Code's built-in
    telemetry (`OTEL_EXPORTER_OTLP_PROTOCOL=http/json`, endpoint 127.0.0.1:9999).

    Two facts are taken and the rest of a very large schema is walked past:
      - `claude_code.cost.usage` (USD) -> burn.costUSD for the LOCAL day, costSource
        "otlp". crabd has never had money on the panel and does not derive it: with no
        telemetry flowing, costUSD is null, not a token count multiplied by a guess.
      - `api_error` log events -> the matching session's events ring, so a session
        retrying against a 429 stops rendering as a healthy "working".

    Every method here is total. A telemetry export is fire-and-forget from a producer
    that must never be blocked or errored by its receiver, so malformed input is dropped
    silently and the endpoint has already answered 204 before any of this runs.
    """

    def __init__(self, on_event=None) -> None:
        self._lock = threading.Lock()
        # local day string -> USD. Bucketed by day so a crabd that runs for a week does
        # not need a restart to stop reporting yesterday's spend as today's.
        # OrderedDict since v0.25.0 (CRB-b): recency is the eviction order, bounded by
        # OTLP_MAX_DELTA_DAYS with today's bucket protected - see _evict_delta_locked.
        self._delta_by_day: "OrderedDict[str, float]" = OrderedDict()
        # Cumulative counters are the OTHER temporality and need the opposite
        # arithmetic: keyed by (day, series) and holding the LAST value seen, summed
        # across series at read time. Mixing the two into one number is the trap the
        # research called out - it looks plausible and is wrong.
        # OrderedDict since v0.17.0 (F4): recency is the eviction order, so the key count
        # is bounded by OTLP_MAX_CUMULATIVE_SERIES - see that constant for why dropping a
        # cumulative series costs at most one interval.
        self._cumulative: "OrderedDict[tuple[str, str], float]" = OrderedDict()
        self._seen = False
        self.exports = 0
        self.errors_seen = 0
        # Every BODY that reached a receiver method, whether or not crabd wanted
        # anything in it - /v1/health's otlpSeen. `exports` counts only the batches that
        # carried a cost point, which answers a different question: health is asking
        # whether the exporter is pointed at this port at all.
        self.documents = 0
        # Injected by main(): a callable (session_id, text) -> bool that appends to the
        # session's ring. None (a unit test) just counts.
        self._on_event = on_event

    # -- metrics

    def ingest_metrics(self, doc, now: float) -> int:
        """-> how many cost data points were taken. Tolerant by design: any level of the
        resourceMetrics/scopeMetrics/metrics/dataPoints nesting may be missing, the wrong
        type, or carry metrics crabd has no interest in."""
        with self._lock:
            self.documents += 1
        taken = 0
        for metric in self._walk(doc, "resourceMetrics", "scopeMetrics", "metrics"):
            if metric.get("name") != OTLP_COST_METRIC:
                continue
            # `sum` is what a counter exports; `gauge` is accepted because the OTLP JSON
            # mapping allows either and a collector in the middle may reshape it.
            for kind in ("sum", "gauge"):
                block = metric.get(kind)
                if not isinstance(block, dict):
                    continue
                temporality = block.get("aggregationTemporality")
                points = block.get("dataPoints")
                if not isinstance(points, list):
                    continue
                for point in points:
                    if self._take_point(point, temporality, now):
                        taken += 1
        if taken:
            with self._lock:
                self._seen = True
                self.exports += 1
        return taken

    def _take_point(self, point, temporality, now: float) -> bool:
        if not isinstance(point, dict):
            return False
        value = point.get("asDouble")
        if value is None:
            value = point.get("asInt")
        if isinstance(value, str):
            # The protobuf-JSON mapping serialises 64-bit ints as STRINGS. Costs arrive
            # as asDouble in practice, but a collector that re-encodes them would
            # otherwise be silently dropped.
            try:
                value = float(value)
            except ValueError:
                return False
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        value = float(value)
        if value < 0 or value != value or value in (float("inf"), float("-inf")):
            return False   # a negative or non-finite cost is not a cost
        stamp = self._point_time(point, now)
        day = _local_day(stamp)
        with self._lock:
            if temporality == OTLP_TEMPORALITY_CUMULATIVE:
                key = (day, self._series_key(point))
                # max(), not assignment: exports can arrive out of order, and a
                # cumulative counter never goes down within a series.
                self._cumulative[key] = max(self._cumulative.get(key, 0.0), value)
                self._cumulative.move_to_end(key)     # most-recently-updated last
                self._evict_series_locked(day)
            else:
                self._delta_by_day[day] = self._delta_by_day.get(day, 0.0) + value
                self._delta_by_day.move_to_end(day)   # most-recently-updated last
                self._evict_delta_locked(_local_day(now))
        return True

    def _evict_delta_locked(self, today: str) -> None:
        """Bound the delta keyspace (CRB-b). Caller holds self._lock; `today` is the
        local day as of arrival - the one bucket cost_today sums, so it is NEVER evicted.

        Unlike the cumulative sibling, a delta bucket carries a running sum with no series
        total to restore it, so eviction is permanent. That is why the protected key is
        TODAY specifically rather than the just-landed point's day: a flood of points with
        forged past/future timeUnixNano must not be able to push today's real spend out.
        Order is least-recently-updated first (OrderedDict recency), so the oldest ballast
        day - already on prune()'s list, since only today is ever served - goes first."""
        if len(self._delta_by_day) <= OTLP_MAX_DELTA_DAYS:
            return
        for key in list(self._delta_by_day):
            if len(self._delta_by_day) <= OTLP_MAX_DELTA_DAYS:
                break
            if key == today:
                continue
            self._delta_by_day.pop(key, None)

    def _evict_series_locked(self, day: str) -> None:
        """Bound the cumulative keyspace (F4). Caller holds self._lock; `day` is the local
        day of the point that just landed.

        TODAY'S series are given up LAST: eviction takes every key outside `day` first,
        because only one day's bucket is ever served (cost_today reads today) and the
        other one is already on prune()'s list - so those keys are ballast that nothing
        can read. Within each group the order is least-recently-updated, so a live series
        (re-touched on every export) is the last thing a flood reaches, and a live series
        it does reach comes back whole on that series' next export."""
        if len(self._cumulative) <= OTLP_MAX_CUMULATIVE_SERIES:
            return
        stale = [k for k in self._cumulative if k[0] != day]
        for key in stale + list(self._cumulative):   # oldest -> newest, ballast first
            if len(self._cumulative) <= OTLP_MAX_CUMULATIVE_SERIES:
                break
            self._cumulative.pop(key, None)

    @staticmethod
    def _point_time(point, now: float) -> float:
        """timeUnixNano -> epoch seconds. Absent/garbage falls back to arrival time,
        which is the honest approximation for a point that just arrived."""
        for key in ("timeUnixNano", "startTimeUnixNano"):
            raw = point.get(key)
            if isinstance(raw, str):
                try:
                    raw = int(raw)
                except ValueError:
                    continue
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                continue
            seconds = float(raw) / 1e9
            # Bounded at BOTH ends (v0.14.0). The floor was always here; the ceiling is
            # what stops an absurd timeUnixNano reaching _local_day -> fromtimestamp,
            # where it raises. Out of range falls back to arrival time, same as garbage.
            if LIMITS_CACHE_MIN_EPOCH < seconds <= TS_MAX_EPOCH:
                return seconds
        return now

    @classmethod
    def _series_key(cls, point) -> str:
        """A stable identity for one cumulative series: its attribute set, sorted."""
        attrs = cls._attributes(point.get("attributes"))
        return "\x1f".join(f"{k}={v}" for k, v in sorted(attrs.items()))

    # -- logs

    def ingest_logs(self, doc, now: float) -> int:
        """-> how many api_error events were routed to a session ring."""
        with self._lock:
            self.documents += 1
        taken = 0
        for record in self._walk(doc, "resourceLogs", "scopeLogs", "logRecords"):
            if taken >= OTLP_EVENTS_PER_EXPORT:
                break
            attrs = self._attributes(record.get("attributes"))
            name = attrs.get("event.name") or self._value(record.get("eventName"))
            if name != OTLP_ERROR_EVENT:
                continue
            session_id = attrs.get(OTLP_SESSION_ATTR)
            if not isinstance(session_id, str) or not session_id:
                continue
            if self._on_event is not None and self._on_event(session_id,
                                                             self._error_text(attrs)):
                taken += 1
                with self._lock:
                    self.errors_seen += 1
        return taken

    @staticmethod
    def _error_text(attrs: dict) -> str:
        """The ring line for an api_error. Status code and attempt only - never the
        `error` message, which is free-form vendor text that would land in the history
        file and break its "no content" rule for a line nobody can act on anyway."""
        status = attrs.get("status_code")
        attempt = attrs.get("attempt")
        text = "API error"
        if isinstance(status, (int, float)) and not isinstance(status, bool):
            text += " %d" % int(status)
        elif isinstance(status, str) and status.strip():
            text += " " + status.strip()[:8]
        if isinstance(attempt, (int, float)) and not isinstance(attempt, bool) \
                and int(attempt) > 1:
            text += " (attempt %d)" % int(attempt)
        return text

    # -- shared walking

    @staticmethod
    def _walk(doc, outer: str, middle: str, inner: str):
        """resourceX -> scopeX -> the leaf list, skipping anything of the wrong shape.

        Written as a generator over three tolerant loops rather than a schema parse: the
        OTLP JSON mapping is large, versioned, and reshaped by any collector in the
        middle, and the only failure this receiver may have is dropping a fact - never
        raising into a producer's export.
        """
        if not isinstance(doc, dict):
            return
        for resource in doc.get(outer) or []:
            if not isinstance(resource, dict):
                continue
            for scope in resource.get(middle) or []:
                if not isinstance(scope, dict):
                    continue
                for leaf in scope.get(inner) or []:
                    if isinstance(leaf, dict):
                        yield leaf

    @classmethod
    def _attributes(cls, attributes) -> dict:
        """OTLP's `[{key, value: {stringValue|intValue|...}}]` -> a flat dict."""
        out: dict = {}
        if not isinstance(attributes, list):
            return out
        for entry in attributes:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if not isinstance(key, str) or not key:
                continue
            out[key] = cls._value(entry.get("value"))
        return out

    @staticmethod
    def _value(raw):
        if not isinstance(raw, dict):
            return raw if isinstance(raw, (str, int, float)) else None
        for key in ("stringValue", "boolValue", "doubleValue"):
            if key in raw:
                return raw[key]
        if "intValue" in raw:
            value = raw["intValue"]
            if isinstance(value, str):     # protobuf-JSON 64-bit ints are strings
                try:
                    return int(value)
                except ValueError:
                    return None
            return value
        return None

    # -- read

    def cost_today(self, now: float) -> float | None:
        """burn.costUSD - USD spent since local midnight, or None when no telemetry has
        ever arrived. None is not 0: an operator with telemetry off must see "unknown",
        and a $0 reading on a working day would be a number crabd made up."""
        with self._lock:
            if not self._seen:
                return None
            day = _local_day(now)
            total = self._delta_by_day.get(day, 0.0)
            total += sum(value for (bucket, _), value in self._cumulative.items()
                         if bucket == day)
        return round(total, 4)

    def prune(self, now: float) -> None:
        """Yesterday's buckets can never be served again (cost_today reads one day)."""
        keep = {_local_day(start) for start in _local_day_starts(now, 2)}
        with self._lock:
            for day in [d for d in self._delta_by_day if d not in keep]:
                del self._delta_by_day[day]
            for key in [k for k in self._cumulative if k[0] not in keep]:
                del self._cumulative[key]


# -------------------------------------------------------------------------- recap

class RecapReader:
    """`recap` - what today looked like: sessions, finishes, commits per repo.

    Split in two on purpose. The CHEAP half (sessionsToday, doneToday, the candidate
    repo list) is a scan of facts the builder already holds, so the builder hands it
    over on every pass via `submit`. The EXPENSIVE half is `git log` per repo, and it
    runs HERE on the recap thread rather than on the builder: a handful of 10 s
    subprocesses on the builder thread would freeze `generatedAt` and
    the widget would (correctly) declare the whole feed dead.

    The served document is assembled from ONE `submit` snapshot plus the git run that
    followed it, so the counts and the commits describe the same instant; `computedAt`
    dates that instant. Nothing is served until the first run completes - `recap` is
    null then, never a zeroed document, because "0 sessions today" and "not computed
    yet" are different claims.
    """

    def __init__(self, runner=None, week_runner=None) -> None:
        self._runner = runner              # tests inject; production uses _git_count
        self._week_runner = week_runner    # ditto, _git_days
        self._lock = threading.Lock()
        self._recap: dict | None = None
        self._input: tuple[int, int, list[tuple[str, str]],
                           list[tuple[str, int]]] | None = None
        self._due = 0.0

    def submit(self, sessions_today: int, done_today: int,
               repos: list[tuple[str, str]],
               week_done: list[tuple[str, int]] | None = None) -> None:
        """Latest cheap facts from the builder. (repo name, a cwd inside it), most
        recently active first; `week_done` is (local day, finishes) oldest first."""
        with self._lock:
            self._input = (sessions_today, done_today, list(repos),
                           list(week_done or []))

    def get(self) -> dict | None:
        with self._lock:
            if self._recap is None:
                return None
            out = dict(self._recap)
            out["commits"] = [dict(c) for c in self._recap["commits"]]
            out["week"] = [dict(d) for d in self._recap["week"]]
            return out

    def poll(self, now: float) -> bool:
        """Recompute at most once per RECAP_REFRESH_SEC. BLOCKING (git subprocesses)."""
        with self._lock:
            if now < self._due or self._input is None:
                return False
            sessions_today, done_today, repos, week_done = self._input
            self._due = now + RECAP_REFRESH_SEC   # a slow run must not shorten the cycle
        commits = self.commits(repos)
        week = self.week(repos, week_done, now)
        recap = {"sessionsToday": sessions_today, "doneToday": done_today,
                 "commits": commits, "week": week,
                 "computedAt": _utc_iso(time.time())}
        with self._lock:
            self._recap = recap
        return True

    def week(self, repos, week_done, now: float) -> list[dict]:
        """recap.week - 7 local days oldest first, `done` from the persisted history and
        `commits` summed across the WHOLE recap scope (not the cap-4 `commits` list).

        ONE `git log` per repo covering all seven days, bucketed here. Seven `--since`/
        `--until` calls per repo would be seven process spawns each, and this runs on a
        machine where the recap scope is a dozen repos.

        %cd, not %ad, and `--date=format-local`: `--since`/`--until` filter on the
        COMMITTER date, so formatting the author date would let a rebased commit land in
        a day the range filter never selected - and `--date=short` renders in the
        commit's own recorded offset, which buckets a commit made in another timezone
        into the wrong local day. Both halves of this row are local wall clock.
        """
        if not week_done:
            return []
        days = [day for day, _done in week_done]
        totals = {day: 0 for day in days}
        runner = self._week_runner or self._git_days
        since = days[0] + " 00:00:00"
        until = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S")
        for _repo, cwd in list(repos)[:RECAP_REPO_SCAN_CAP]:
            try:
                dates = runner(cwd, since, until)
            except (OSError, ValueError, subprocess.SubprocessError):
                continue   # same rule as `commits`: a repo that will not answer is skipped
            if not dates:
                continue
            for date in dates:
                if date in totals:
                    totals[date] += 1
        return [{"day": day, "done": done, "commits": totals[day]}
                for day, done in week_done]

    def commits(self, repos) -> list[dict]:
        """Commits since local midnight per repo, cap RECAP_REPO_CAP by count desc.

        A repo that will not answer - not a repo any more, unborn HEAD, git missing,
        a filesystem that hangs until the timeout - is SKIPPED, not guessed at and not
        served as 0. Repos with no commits today are dropped too: the widget's line is
        "what got committed", and a wall of zeroes is not that.
        """
        runner = self._runner or self._git_count
        counted = []
        for repo, cwd in list(repos)[:RECAP_REPO_SCAN_CAP]:
            try:
                count = runner(cwd)
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                counted.append({"repo": repo, "count": count})
        # Name breaks the tie so a cap-4 cut is deterministic rather than dict-ordered.
        counted.sort(key=lambda c: (-c["count"], c["repo"]))
        return counted[:RECAP_REPO_CAP]

    @staticmethod
    def _git_count(cwd: str) -> int | None:
        """`git -C <cwd> log --oneline --since=midnight` line count. 'midnight' is git's
        own approxidate for today 00:00 LOCAL, which is the boundary the contract asks
        for. Read-only; no fetch, no network."""
        proc = subprocess.run(
            ["git", "-C", cwd, "log", "--oneline", "--since=midnight"],
            capture_output=True, timeout=RECAP_GIT_TIMEOUT_SEC, check=False,
            # Under the Scheduled Task there is no console to inherit, and without this
            # a window would flash on the desktop on an interactive login.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            return None
        text = proc.stdout.decode("utf-8", errors="replace")
        return sum(1 for line in text.splitlines() if line.strip())

    @staticmethod
    def _git_days(cwd: str, since: str, until: str) -> list[str] | None:
        """One local-day string per commit in the window. `None` on a non-zero exit -
        not a repo, unborn HEAD - so the caller skips rather than serving zeroes."""
        proc = subprocess.run(
            ["git", "-C", cwd, "log", f"--since={since}", f"--until={until}",
             "--format=%cd", "--date=format-local:%Y-%m-%d"],
            capture_output=True, timeout=RECAP_GIT_TIMEOUT_SEC, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0:
            return None
        text = proc.stdout.decode("utf-8", errors="replace")
        return [line.strip() for line in text.splitlines() if line.strip()]


# -------------------------------------------------------------------------- fleet

class FleetReader:
    """`fleet` - SideCrab observing its own background services (glow, toast).

    Which services exist, how to query one and how to read its answer are the
    PLATFORM's; the four served outcomes are this class's, and they are the contract:
      running  - the platform's service query reports it executing
      stopped  - it exists and is not executing
      absent   - the query failed BECAUSE there is no such service
      unknown  - anything else: the query is missing, timed out, returned a status this
                 platform does not recognise, or failed with no not-found wording

    A service whose state cannot be read is never folded into `stopped`. "the notifier
    is not running" and "I could not find out" are different claims, and a widget dot
    that guesses the first when it means the second is exactly the silent-all-green
    failure the contract's stale rules exist to prevent.

    Cached FLEET_REFRESH_SEC and computed on its own thread for the same reason recap
    is: two subprocesses on the builder thread would freeze `generatedAt`.
    """

    def __init__(self, runner=None, platform=None) -> None:
        # The reader owns the CACHING and the four-outcome rule; the platform owns the
        # service manager - which targets exist, how to query one, and how to read its
        # answer. Splitting them is what lets the Windows mapping keep being proven on
        # a host that has no schtasks: `runner=` injects the query, `platform=` the
        # parse, and neither is a test of what OS this is.
        self._runner = runner
        self._platform = platform or PLATFORM
        self._lock = threading.Lock()
        self._result = self.unknown(self._platform)
        self._due = 0.0

    @staticmethod
    def unknown(platform=None) -> dict:
        """Every component the platform names, all `unknown`. STATIC because
        StateBuilder calls it on the class for a builder with no reader attached, and
        build() must never raise. The keys come from the platform because the SERVICE
        NAMES do - the no-reader answer must carry the same key set a reader would."""
        return {name: "unknown"
                for name, _target in (platform or PLATFORM).fleet_targets()}

    def get(self) -> dict:
        with self._lock:
            return dict(self._result)

    def poll(self, now: float) -> bool:
        """Query at most once per FLEET_REFRESH_SEC. BLOCKING - the fleet thread owns it."""
        with self._lock:
            if now < self._due:
                return False
            self._due = now + FLEET_REFRESH_SEC   # a slow run must not shorten the cycle
        result = self.read()
        with self._lock:
            self._result = result
        return True

    def read(self) -> dict:
        return {name: self.status(target)
                for name, target in self._platform.fleet_targets()}

    def status(self, target: str) -> str:
        if not target:
            # A component this platform names but has NO service for. There is nothing
            # to spawn and nothing to ask, so the platform is handed the same sentinel
            # its own service_query returns for an empty target and answers in its own
            # terms. Short-circuited HERE rather than left to the platform so that an
            # INJECTED runner is not called either: a test's fake would otherwise record
            # a query for a service that does not exist, and a real injected runner
            # would spawn one.
            return self._platform.service_status(*FLEET_NO_SERVICE)
        runner = self._runner or self._platform.service_query
        try:
            code, out, err = runner(target, FLEET_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            return "unknown"
        except (OSError, ValueError):    # the query is missing, or the spawn failed
            return "unknown"
        return self._platform.service_status(code, out, err)


# ------------------------------------------------------------------ host sampler

class _FILETIME(ctypes.Structure):
    """Win32 FILETIME: a 64-bit count of 100 ns ticks, delivered as two 32-bit halves."""
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


class _MEMORYSTATUSEX(ctypes.Structure):
    """The GlobalMemoryStatusEx out-parameter.

    `dwLength` MUST be set to sizeof(struct) BEFORE the call - the API versions the
    struct by that field and returns 0 without touching a single member when it is
    wrong. Every field must be declared, in order, even the ones crabd never reads: the
    kernel writes the whole struct, and a short one is a stack write past the end.
    """
    _fields_ = [("dwLength", ctypes.c_uint32),
                ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64)]


def _filetime(ft: _FILETIME) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


class HostSampler:
    """`host` - the machine's own CPU and memory, for the panel beside the iCUE sensors.

    CPU IS A DELTA, AND THAT IS THE ONLY HARD THING IN HERE. GetSystemTimes returns
    three CUMULATIVE FILETIMEs (idle, kernel, user) counted since boot, so one reading
    describes the whole uptime and says nothing about now - utilization exists only
    BETWEEN two readings. Two consequences, both pinned by tests:

      - the FIRST sample has no predecessor, so `cpuPct` is null until the next builder
        pass (~2 s). Null, never 0.0: "not measured yet" and "the machine is asleep"
        are different claims and only one of them is true at startup.
      - KERNEL TIME INCLUDES IDLE TIME. Microsoft documents it and it is the classic
        way to get this wrong. The busy fraction is
            ((kernel + user) - idle) / (kernel + user)
        over the deltas; drop the subtraction and a completely idle host reports ~100%
        busy, which is a gauge that is not merely imprecise but inverted.

    Memory is a single instantaneous reading (GlobalMemoryStatusEx), needs no history,
    and is therefore served on the very first pass.

    HONEST FAILURE, in three tiers, because "cannot read" has three different shapes:
      - no counters at all (a platform whose reader answers None, or both calls failing)
        -> NO `host` key in the document. The widget feature-detects presence, so it
        renders nothing rather than a row of em-dashes.
      - one of the two calls failing -> that call's fields null, the other's intact.
      - a reading that is not a finite number -> null, via `_pct` / `_gb`.
    A previous pass's number is NEVER re-served as though it were fresh: there is no
    last-good cache in here at all, which is the whole reason `cpuPct` can be null on a
    running daemon.

    The lock guards `_prev` only, and it is not decorative: at cold start `_do_state`
    builds ON THE REQUEST THREAD while `_refresh_loop` is building its first snapshot,
    so two samples really can overlap. Unlocked, both would read the same `_prev`,
    both would report a delta measured from it, and the later write would win - two
    overlapping windows served as if they were consecutive.
    """

    def __init__(self, times=None, memory=None, platform=None) -> None:
        # Tests inject; production falls through to the platform's two readers.
        # Injection is by CALLABLE rather than by patching ctypes because the FILETIME
        # arithmetic - the part with the trap in it - is what needs proving, and it is
        # unreachable if the test has to own a real kernel counter to get to it. An
        # injected reader OUTRANKS the platform for that reason.
        self._times = times
        self._memory = memory
        self._platform = platform or PLATFORM
        self._lock = threading.Lock()
        self._prev: tuple[int, int, int] | None = None

    def sample(self) -> dict | None:
        """The whole block, or None when nothing at all could be read. Never raises."""
        cpu_pct, cpu_ok = self._cpu()
        mem, mem_ok = self._mem()
        if not cpu_ok and not mem_ok:
            return None
        block = {"cpuPct": cpu_pct}
        block.update(mem)
        return block

    def _cpu(self) -> tuple[float | None, bool]:
        """(cpuPct, did-the-counter-read-succeed). The two are independent: a successful
        read with no predecessor is `(None, True)`, and that is the first-sample rule."""
        reader = self._times or self._platform.cpu_times
        try:
            reading = reader()
        except Exception as exc:            # an injected reader, or a ctypes surprise
            _log_once(HOST_CPU_LOG_KEY,
                      f"crabd: host CPU counter raised {type(exc).__name__}; "
                      f"serving no cpuPct")
            return None, False
        if reading is None:
            return None, False
        try:
            now_idle, now_kernel, now_user = (int(v) for v in reading)
        except (TypeError, ValueError):
            return None, False
        with self._lock:
            prev = self._prev
            if prev is None:
                self._prev = (now_idle, now_kernel, now_user)
                return None, True           # first sample: no delta exists yet
            idle = now_idle - prev[0]
            kernel = now_kernel - prev[1]
            user = now_user - prev[2]
            if idle < 0 or kernel < 0 or user < 0:
                # A counter went BACKWARDS. It should not; a rigged reader or a driver
                # bug can. Re-baseline and report nothing for this pass rather than
                # serving a negative-derived percentage.
                self._prev = (now_idle, now_kernel, now_user)
                return None, True
            total = kernel + user
            if total < CPU_MIN_TOTAL_TICKS:
                # A-07: the window is SUB-QUANTUM. Two shapes collapse here, both untrust-
                # worthy for the same reason - less than a meaningful amount of core-time
                # elapsed between the two reads:
                #   - total == 0: no core-time at all - two builds in the same instant,
                #     which the cold-start request path really produces.
                #   - 0 < total < CPU_MIN_TOTAL_TICKS: the counters moved by only a
                #     scheduler quantum or two, so the busy fraction is quantised to a
                #     coarse 0/50/100 that reads "asleep" on a machine that is NOT (idle
                #     and kernel advancing by the same quantum gives an exact 0.0). The
                #     contract's failure table puts this in the NULL column, not a 0.0.
                # THE BASELINE MUST SURVIVE: do NOT update _prev. Re-baselining on every
                # sub-quantum pass would make a caller polling faster than the counters
                # tick accumulate nothing and be served null forever; leaving _prev alone
                # lets movement pile up against it until a real quantum lands.
                # A-09: the old note here claimed the skip is a no-op "because a zero delta
                # means this reading and the baseline are the same tuple". That is FALSE
                # whenever idle advances while kernel+user do not (a lagging/rigged reader;
                # unreachable with real counters, where idle ticks ARE kernel ticks) - the
                # tuples then differ and the skip is a real choice. The skip is still
                # correct, but for the ACCUMULATION reason above, not the equal-tuple one.
                return None, True
            if idle > total:
                # A-08: idle is a subset of kernel time, so idle <= (kernel+user) always
                # holds for a well-behaved GetSystemTimes. A rigged reader or driver bug
                # can break it, and (total - idle) then goes negative - which _pct would
                # CLAMP to a plausible-looking 0.0. Serve null instead, exactly as the
                # backwards-counter branch above does: an unusable reading belongs in the
                # contract's null column, not clamped into a false "idle". Re-baseline so
                # the next pass measures from a clean reading.
                self._prev = (now_idle, now_kernel, now_user)
                return None, True
            self._prev = (now_idle, now_kernel, now_user)
        return _pct(100.0 * (total - idle) / total), True

    def _mem(self) -> tuple[dict, bool]:
        """({memPct, memUsedGB, memTotalGB}, did-the-read-succeed)."""
        blank = {"memPct": None, "memUsedGB": None, "memTotalGB": None}
        reader = self._memory or self._platform.memory
        try:
            reading = reader()
        except Exception as exc:
            _log_once(HOST_MEM_LOG_KEY,
                      f"crabd: host memory read raised {type(exc).__name__}; "
                      f"serving no memory figures")
            return blank, False
        if reading is None:
            return blank, False
        try:
            total, avail = reading
        except (TypeError, ValueError):
            return blank, False
        total = _finite_number(total)
        avail = _finite_number(avail)
        if total is None or total <= 0 or avail is None or avail < 0:
            return blank, False
        used = total - min(avail, total)    # more available than installed is not a size
        return ({"memPct": _pct(100.0 * used / total),
                 "memUsedGB": _gb(used),
                 "memTotalGB": _gb(total)}, True)


# ---------------------------------------------------------------- continue queue

class ContinueQueue:
    """Tap-to-continue, Tier 1 (docs/spikes/reply-spike-2.md): the widget queues a
    prompt, and the session's Stop hook drains it on the way past.

    One item per session, newest wins, expiring after CONTINUE_TTL_SEC (contract). All
    three of those are the same decision: the Stop hook fires once, at the transition,
    so a queue is a bet that the session is about to finish. A second tap means the
    operator changed their mind, and a bet placed ten minutes ago is one they have
    forgotten making - delivering it then would put words in a session's mouth long
    after the moment that prompted them.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queued: dict[str, tuple[str, float]] = {}

    def queue(self, session_id: str, prompt: str, now: float) -> None:
        with self._lock:
            self._queued[session_id] = (prompt, now)

    def peek(self, session_id: str, now: float) -> str | None:
        with self._lock:
            entry = self._queued.get(session_id)
            if entry is None or now - entry[1] > CONTINUE_TTL_SEC:
                return None
            return entry[0]

    def drain(self, session_id: str, now: float) -> str | None:
        """Take the queued prompt, or None. An EXPIRED item is deleted here as well as
        ignored: leaving it would let the next Stop, minutes later, deliver a prompt
        that this drain already decided was too old."""
        with self._lock:
            entry = self._queued.pop(session_id, None)
        if entry is None or now - entry[1] > CONTINUE_TTL_SEC:
            return None
        return entry[0]

    def drain_if(self, session_id: str, prompt: str, now: float) -> str | None:
        """Drain, but ONLY the prompt the caller is holding. CD-30 (v0.21.0).

        THE RACE: the Stop handler is peek -> send -> drain on purpose (CRB-F5), so a
        send that fails leaves the prompt intact. But between the peek and the drain the
        operator can tap a DIFFERENT button - the queue is newest-wins, so the tap is
        accepted - and the unconditional drain then deleted the new prompt while the old
        one was the one actually delivered. The replacement was neither delivered nor
        kept, and the card stopped showing it, so nothing on the panel said it was gone.

        Comparing the TEXT rather than a token is what the whitelist makes safe and
        sufficient: the queue holds one item per session and its prompt is a fixed
        string from CONTINUE_PROMPTS_BUILTIN or config, so equal text means the
        operator's replacement asks for exactly what was sent. Different text means a
        genuine change of mind, and it stays queued for the next Stop.
        """
        with self._lock:
            entry = self._queued.get(session_id)
            if entry is None or entry[0] != prompt:
                return None
            del self._queued[session_id]
        return None if now - entry[1] > CONTINUE_TTL_SEC else entry[0]

    def entry(self, session_id: str, now: float) -> dict | None:
        """The contract's `sessions[].queuedContinue` (v0.14.0), or None.

        Same freshness rule as peek/drain, deliberately re-derived from the stored `at`
        rather than trusting the expiry sweep to have run: the card must stop showing
        "queued: Run the tests" at the ten-minute mark whether or not _expiry_loop got
        there first, because the Stop hook would not deliver it either.
        """
        with self._lock:
            entry = self._queued.get(session_id)
        if entry is None or now - entry[1] > CONTINUE_TTL_SEC:
            return None
        return {"prompt": entry[0], "queuedAt": _utc_iso(entry[1])}

    def pending(self, now: float) -> int:
        with self._lock:
            return sum(1 for _, at in self._queued.values()
                       if now - at <= CONTINUE_TTL_SEC)

    def prune(self, now: float) -> None:
        with self._lock:
            for sid in [s for s, (_, at) in self._queued.items()
                        if now - at > CONTINUE_TTL_SEC]:
                del self._queued[sid]


# ------------------------------------------------------------- panel approvals

class PermissionRequestMismatch(Exception):
    """decide() was given a requestId that is not the one pending for the session
    (WID-a, v0.29.0): the tap was aimed at a request that has since been replaced."""


class PanelToken:
    """The panel pairing code (v0.29.0) - the second barrier on `decide` (SEC-a).

    Three rules, structural rather than careful:
      - **Never fails open.** No code loaded means every verify() is "rejected"; a
        handler with no PanelToken at all answers 503, not 204 (see _do_decide).
      - **Constant-time compare** (hmac.compare_digest) on the normalised code, so a
        loopback caller cannot time its way to the code one symbol at a time.
      - **Bounded guessing.** PANEL_TOKEN_MAX_FAILURES rejects inside PANEL_TOKEN_WINDOW_SEC
        lock verify() for PANEL_TOKEN_LOCKOUT_SEC - the RIGHT code included, so a lockout
        is visible on the panel rather than silently absorbed.
    The code is never served: /v1/health reports presence and lockout only.
    """

    FORMAT = re.compile(r"^[0-9A-HJ-NP-TV-Z]{%d}$" % PANEL_TOKEN_LEN)

    def __init__(self, path, code) -> None:
        self.path = path
        self._code = code if code and self.FORMAT.match(code) else None
        self._lock = threading.Lock()
        self._failures: list[float] = []
        self._locked_until = 0.0

    @classmethod
    def load_or_create(cls, path: Path) -> "PanelToken":
        """Read the code off disk, minting one when the file is missing or unusable.
        Atomic write (tmp + os.replace) so a crash mid-write cannot leave a truncated
        code that the next start would silently replace with a different one."""
        code = None
        try:
            code = cls.normalize(path.read_text(encoding="utf-8"))
        except OSError:
            code = None
        if not code or not cls.FORMAT.match(code):
            code = cls.generate()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(cls.display(code) + "\n", encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)     # a no-op on Windows; the profile ACL does the job
            except OSError:
                pass
            os.replace(tmp, path)
        return cls(path, code)

    @staticmethod
    def generate() -> str:
        return "".join(secrets.choice(PANEL_TOKEN_ALPHABET) for _ in range(PANEL_TOKEN_LEN))

    @staticmethod
    def normalize(raw) -> str:
        """Upper-case, keep only the alphabet's symbols: `k7qxm-2pdab`, `K7QXM 2PDAB`
        and `K7QXM2PDAB` are the same code."""
        return re.sub(r"[^0-9A-Z]", "", str(raw or "").upper())

    @staticmethod
    def display(code: str) -> str:
        return code[:5] + "-" + code[5:] if len(code) == PANEL_TOKEN_LEN else code

    def verify(self, presented, now: float) -> str:
        """-> "ok" | "missing" | "rejected" | "locked". Only "ok" may allow anything."""
        with self._lock:
            if now < self._locked_until:
                return "locked"
            if not isinstance(presented, str) or not presented.strip():
                return "missing"
            if self._code is not None and hmac.compare_digest(
                    self.normalize(presented), self._code):
                self._failures.clear()
                return "ok"
            self._failures = [t for t in self._failures
                              if now - t < PANEL_TOKEN_WINDOW_SEC]
            self._failures.append(now)
            if len(self._failures) >= PANEL_TOKEN_MAX_FAILURES:
                self._locked_until = now + PANEL_TOKEN_LOCKOUT_SEC
                self._failures.clear()
                return "locked"
            return "rejected"

    def status(self, now: float) -> dict:
        """Diagnostic for /v1/health. Never the code."""
        with self._lock:
            recent = [t for t in self._failures if now - t < PANEL_TOKEN_WINDOW_SEC]
            locked = self._locked_until if now < self._locked_until else None
        return {"present": self._code is not None,
                "rejectedRecently": len(recent),
                "lockedUntil": _utc_iso(locked) if locked else None}


class PermissionBroker:
    """The PermissionRequest long poll (contract v0.12.0 §4).

    The hook arrives as an HTTP request and is HELD - up to PERMISSION_POLL_SEC - while
    the widget shows Approve / Deny on the needs_input sheet. A tap answers it; silence
    does not. Three rules this class exists to make structural rather than careful:

      - **It NEVER auto-allows.** There is no path from a timeout, a saturated broker,
        a disabled config or an error to `behavior: allow`. The only thing that produces
        an allow is `decide(..., "allow")`, and the only caller of that is the /v1/action
        endpoint answering a tap. This is the one property worth reading the code for:
        a companion that could allow a tool call on its own would be a remote-execution
        hole wearing a status widget.
      - **Timeout is a PASS-THROUGH, not a deny.** The terminal dialog appears exactly
        as it does with SideCrab uninstalled, so a missed tap costs nothing and an
        operator who never looks at the panel is never worse off.
      - **The wait is bounded twice** - in time and in concurrent count - and holds no
        lock while it waits, so /v1/state and the builder stay responsive underneath it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}

    def register(self, session_id: str, tool: str, summary: str | None,
                 now: float) -> dict | None:
        """-> the pending entry, or None when the broker is saturated (the caller then
        passes through). A second request for the same session REPLACES the first and
        releases it as a pass-through: the older prompt is still sitting in a terminal
        waiting for someone, and leaving its holder parked would strand a live request
        on a panel entry nothing will ever answer."""
        # WID-a (v0.29.0): a per-request id the widget must echo on decide, so a tap
        # aimed at THIS request can never land on the one that replaced it.
        entry = {"tool": tool, "summary": summary, "requestedAt": now,
                 "requestId": secrets.token_hex(8),
                 "event": threading.Event(), "decision": None}
        with self._lock:
            previous = self._pending.get(session_id)
            if previous is None and len(self._pending) >= PERMISSION_MAX_PENDING:
                return None
            self._pending[session_id] = entry
        if previous is not None:
            previous["event"].set()      # released with decision None = pass-through
        return entry

    def wait(self, entry: dict, timeout: float) -> str | None:
        """Block up to `timeout` for a tap. Returns "allow", "deny" or None (timeout).

        An Event, never a poll loop: a spin here would burn a core for 55 s per pending
        prompt, and the whole point of the bounded wait is that a held request costs
        nothing but a parked thread.
        """
        entry["event"].wait(timeout)
        return entry["decision"]

    def decide(self, session_id: str, decision: str, now: float,
               request_id=None) -> str | None:
        """-> the tool name the decision applied to, or None when nothing was pending.

        The tool name comes back so the caller can write the contract's history line
        ("approved from panel: Bash") without a second lookup that could race the entry
        being removed underneath it.

        `request_id` (WID-a, v0.29.0): when given, it must equal the pending entry's
        `requestId` or PermissionRequestMismatch is raised - checked UNDER the same lock
        that applies the decision, so a replace landing between a check and the write
        cannot be approved with the old id. None skips the check (unit callers only;
        the HTTP handler always passes one).
        """
        if decision not in (PERMISSION_BEHAVIOR_ALLOW, PERMISSION_BEHAVIOR_DENY):
            return None
        with self._lock:
            entry = self._pending.get(session_id)
            if entry is None or entry["decision"] is not None:
                return None
            if request_id is not None and not hmac.compare_digest(
                    str(request_id), entry["requestId"]):
                raise PermissionRequestMismatch(session_id)
            entry["decision"] = decision
            del self._pending[session_id]
        entry["event"].set()
        return entry["tool"]

    def release(self, session_id: str, entry: dict) -> str | None:
        """Close out a timed-out hold and return the decision that ACTUALLY applies -
        None for the ordinary pass-through, or a decision that landed in the gap.

        AUDIT F3 (v0.17.0). This used to be a bare delete, and the delete was not the
        same instant as the caller reading `wait`'s return value. In between those two
        instants a tap could land: decide() found the entry undecided, set "allow",
        removed it and returned the tool, so /v1/action wrote "approved from panel: Bash"
        and answered the widget 204 - while the hook handler, holding the None it had
        already read, answered the pass-through and let the TERMINAL dialog own the call.
        History said approved; nothing was. Safe (no allow ever reached the hook without
        a tap) but a record that disagreed with reality.

        Reading the decision under the SAME lock that removes the entry is the whole fix,
        and it closes the window from both sides:
          - a tap that got in first is RETURNED, so the handler honours the decision it
            can still answer and the history line agrees with what the hook was told;
          - otherwise the entry leaves `_pending` in that same critical section, so every
            later decide() finds nothing and is the 404 the contract already specifies
            for a tap that arrives after the hold ("no permission request pending").

        The delete stays identity-checked: a later request for the same session has
        already replaced this one in the map, and deleting by id alone would silently
        un-register the request currently being held.
        """
        with self._lock:
            decision = entry["decision"]
            if decision is None and self._pending.get(session_id) is entry:
                del self._pending[session_id]
        return decision

    def stale(self, session_id: str) -> str | None:
        """v0.19.0. Drop a hold whose dialog was already answered IN THE APP.
        -> the tool name it applied to, or None when nothing was parked.

        The hold is up to PERMISSION_POLL_SEC long, and for most of that window the
        terminal dialog it mirrors is already gone: the operator clicked Allow, the tool
        ran, the turn finished. The card meanwhile still offers Approve / Deny for a
        decision that has been made, and a tap on it would 404 or - worse - read as a
        second answer. A `Stop`, `UserPromptSubmit` or `SessionEnd` for the session is
        proof the turn moved past it (PERMISSION_STALE_EVENTS).

        The release is EXACTLY register()'s replace path: the entry leaves `_pending`
        under the lock and its event is set with `decision` still None, so the parked
        hook thread wakes and answers the ordinary pass-through. There is no route from
        here to an allow - this method never assigns `decision`.

        A tap that landed first is left alone (`decision is not None`): that request is
        already answered and removed by decide(), and re-setting its event would be a
        write onto a decision this method has no business revisiting.
        """
        with self._lock:
            entry = self._pending.get(session_id)
            if entry is None or entry["decision"] is not None:
                return None
            del self._pending[session_id]
        entry["event"].set()
        return entry["tool"]

    def pending(self, session_id: str) -> dict | None:
        """The contract's `sessions[].pendingPermission`. `summary` is served here and
        nowhere else - it is tool content, so it never reaches the history file."""
        with self._lock:
            entry = self._pending.get(session_id)
            if entry is None or entry["decision"] is not None:
                return None
            return {"tool": entry["tool"], "summary": entry["summary"],
                    "requestedAt": _utc_iso(entry["requestedAt"]),
                    "requestId": entry["requestId"]}

    def has_pending(self, session_id: str) -> bool:
        """A-01 (v0.26.0). True while a LIVE (registered, undecided) hold is parked for this
        session. The join in _await_permission uses it to answer the one question a single
        `permission_alert` boolean cannot: 'did something RE-RAISE this alert while my hold
        was ending?'. register() is newest-wins - a second PermissionRequest for one session
        (what parallel tool calls in one assistant message produce) REPLACES the first and
        releases it as a pass-through - so after B replaces A, the broker's current entry for
        the session is B. When A's released thread reaches the stand-down, B is still parked
        and this returns True, so A's exit must NOT stand the card down: the row correctly
        stays needs_input carrying B's pendingPermission, and B's own eventual clear stands
        it down."""
        with self._lock:
            entry = self._pending.get(session_id)
            return entry is not None and entry["decision"] is None

    def count(self) -> int:
        with self._lock:
            return len(self._pending)

    @staticmethod
    def summarize(tool_input) -> str | None:
        """A short human line for the panel: the Bash command, the file being written,
        the URL being fetched. Falls back to None rather than dumping the whole input -
        an unrecognised tool's argument blob is not something an operator can approve by
        reading it on a 480px panel."""
        if not isinstance(tool_input, dict):
            return None
        for key in PERMISSION_SUMMARY_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return _trim(value, PERMISSION_SUMMARY_MAX)
        return None


# -------------------------------------------------------------- depletion forecast

def _fit_slope(samples: list[tuple[float, float]]) -> float | None:
    """Least-squares utilization-per-second slope over (ts, util) samples. For two
    points this reduces to the plain delta (u1-u0)/(t1-t0), which is the "robust delta"
    the brief allows; for more it is the simple linear fit. None when the timestamps
    carry no spread (a vertical fit has no slope)."""
    n = len(samples)
    if n < 2:
        return None
    t_bar = sum(t for t, _ in samples) / n
    u_bar = sum(u for _, u in samples) / n
    num = sum((t - t_bar) * (u - u_bar) for t, u in samples)
    den = sum((t - t_bar) ** 2 for t, _ in samples)
    if den <= 0:
        return None
    return num / den


class DepletionForecaster:
    """Contract v0.13.0: `limits.fiveHour`/`limits.weekly` gain `exhaustAt` - a linear
    projection of when the window hits 100% at the recent burn rate, or null.

    Keeps a short rolling per-window history of served utilization (FORECAST_WINDOW_SEC,
    capped FORECAST_MAX_SAMPLES, sampled no denser than FORECAST_MIN_SAMPLE_GAP_SEC) and
    fits a positive slope across it. The history is in-memory only and keyed by window
    ("fiveHour", "weekly", and each extra by label) - the SAME key across the OAuth and
    statusline sources, so a source flip that re-reads a lower number trips the same
    util-DOWN reset a genuine window reset does. exhaustAt is null whenever the slope is
    flat/declining, the samples are too few or too close, the projection lands at/after
    the window's own resetsAt (it resets before it depletes), or the window carries no
    parseable resetsAt at all (v0.17.0 - the contract's cap cannot be enforced, so the
    honest answer is null, not an uncapped projection).

    Thread-safe: annotate() runs on the build thread, but the history is guarded so the
    reader could be shared without surprise.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # OrderedDict so recency is cheap: the most-recently-observed key sits at the end and
        # eviction pops from the front (least-recently-updated). Bounded by FORECAST_MAX_KEYS.
        self._history: "OrderedDict[str, list[tuple[float, float]]]" = OrderedDict()

    def annotate(self, block: dict, now: float) -> None:
        """Feed the current utilization of each window in `block` into its history and
        attach `exhaustAt` (ISO or null). Each window dict is REPLACED with a copy so a
        shared cached reading (LimitsReader/StatusLineReader both hand back dicts whose
        window sub-dicts alias their own cache) is never mutated."""
        if not isinstance(block, dict):
            return
        for key in ("fiveHour", "weekly"):
            window = block.get(key)
            if isinstance(window, dict):
                block[key] = self._annotated(key, window, now)
        extras = block.get("extra")
        if isinstance(extras, list):
            block["extra"] = [
                self._annotated("extra:" + str(w.get("label")), w, now)
                if isinstance(w, dict) else w
                for w in extras
            ]

    def _annotated(self, key: str, window: dict, now: float) -> dict:
        out = dict(window)
        out["exhaustAt"] = self._forecast(key, window, now)
        return out

    def _forecast(self, key: str, window: dict, now: float) -> str | None:
        util = window.get("utilization")
        if isinstance(util, bool) or not isinstance(util, (int, float)):
            return None
        util = float(util)
        samples = self._observe(key, now, util)
        if len(samples) < 2 or samples[-1][0] - samples[0][0] < FORECAST_MIN_SPAN_SEC:
            return None
        rate = _fit_slope(samples)
        if rate is None or rate <= 0:
            return None
        remaining = 1.0 - util          # already at/over the cap -> nothing to forecast
        if remaining <= 0:
            return None
        projected = now + remaining / rate
        resets = _parse_ts(window.get("resetsAt"))
        if resets is None:
            # AUDIT F6 (v0.17.0). No parseable resetsAt = the cap below cannot be applied,
            # and the contract's promise is that exhaustAt is NEVER extrapolated past the
            # window's own reset. Unenforceable, so the answer is null rather than a number
            # that skipped the check. MEASURED against the pre-fix code: the smallest
            # genuine step the served 4dp rounding can produce (1e-4 over ~900 s) served a
            # date 93 DAYS out for a five-hour window, and a slope an order smaller runs
            # into _utc_iso's year-3000 ceiling and serves that. Both are numbers crabd
            # made up, which is exactly what "unknown is null" exists to forbid.
            return None
        if projected >= resets:
            return None                 # the window resets before it would deplete
        return _utc_iso(projected)

    def _observe(self, key: str, now: float, util: float) -> list[tuple[float, float]]:
        """Record this reading (subject to the min-gap) and return the pruned history.
        A utilization drop clears the window first - a decrease is never depletion."""
        with self._lock:
            hist = self._history.get(key)
            if hist is None:
                hist = self._history[key] = []
            # Mark this key most-recently-updated, then bound the KEY count. Ordering the touch
            # before eviction means the window being observed right now is never the one evicted.
            self._history.move_to_end(key)
            if hist and util < hist[-1][1] - FORECAST_DROP_EPS:
                hist.clear()            # reset or source flip: the old slope is void
            if not hist or now - hist[-1][0] >= FORECAST_MIN_SAMPLE_GAP_SEC:
                hist.append((now, util))
            cutoff = now - FORECAST_WINDOW_SEC
            while hist and hist[0][0] < cutoff:
                hist.pop(0)
            if len(hist) > FORECAST_MAX_SAMPLES:
                del hist[: len(hist) - FORECAST_MAX_SAMPLES]
            self._evict_keys_locked()
            return list(hist)

    def _evict_keys_locked(self) -> None:
        """Drop least-recently-updated windows once the tracked-key count exceeds the cap. The
        caller holds self._lock. The two contract-named windows are never evicted - a flood of
        `extra:` labels in one build pushes them toward the front, so without this guard the
        real fiveHour/weekly history (the only forecasts that matter) would be the first thing
        thrown away. An evicted extra simply re-accumulates from scratch if it ever returns,
        which serves a harmless null exhaustAt until it has samples again."""
        if len(self._history) <= FORECAST_MAX_KEYS:
            return
        for key in list(self._history):          # oldest -> newest
            if len(self._history) <= FORECAST_MAX_KEYS:
                break
            if key in _FORECAST_PROTECTED_KEYS:
                continue
            del self._history[key]


# ------------------------------------------------------- v0.24.0 panel log channel

def _panel_log_lines(value) -> list[str] | None:
    """Validate + normalize a POST /v1/panel-log `lines` array.

    -> the normalized list, or None when the body is not this endpoint's shape (the
    caller then 400s). The three bounds are DELIBERATELY DIFFERENT KINDS OF ANSWER, and
    the widget lane builds against exactly this split:

      - not an array, an empty array, or ANY member that is not a string -> None -> 400.
        A type error is the caller's bug, and a silent partial store would leave the
        widget lane debugging the debugger.
      - more than PANEL_LOG_MAX_PER_POST lines -> the first 50 are kept, NO error. A
        widget mid-burst must not lose the whole batch for over-filling it; losing the
        tail of one burst is recoverable, losing the burst is not.
      - a line longer than PANEL_LOG_MAX_LINE_CHARS -> truncated, NO error. The first
        300 characters of a diagnostic line are the diagnostic.

    Strip-control THEN trim THEN truncate, in that order (SEC-d): control bytes are
    removed regardless of position (an edge `.strip()` only touches whitespace, so a
    leading ESC would otherwise survive), edge whitespace goes next, and the 300 is a
    budget on content so leading whitespace must not push the useful half off the end.

    `bool` is not special-cased here the way the numeric validators special-case it,
    because `isinstance(True, str)` is already False - the bool/int overlap has no
    analogue for strings.
    """
    if not isinstance(value, list) or not value:
        return None
    kept = value[:PANEL_LOG_MAX_PER_POST]
    if any(not isinstance(line, str) for line in kept):
        return None
    # Members past the cap are dropped WITHOUT being type-checked: they are not stored,
    # so their type cannot matter, and 400ing on line 51 would make the cap a rejection
    # after all. A body of 5000 lines therefore costs one slice, not 5000 isinstance
    # calls - which is also what keeps this endpoint cheap under a flood.
    return [_PANEL_LOG_CTRL.sub("", line).strip()[:PANEL_LOG_MAX_LINE_CHARS]
            for line in kept]


class PanelLog:
    """The panel diagnostics ring (v0.24.0) - POST /v1/panel-log in, GET the same out.

    WHY IT EXISTS. The widget is rendered by iCUE on the Xeneon Edge, a surface no
    devtools can attach to, so `console.log` has nowhere to go. The question this week
    is which input events iCUE actually delivers to the glass: a TAP is proven (panel
    approvals were verified live on 2026-08-27), while swipe, long-press and multi-touch
    are unknown. The only way to find out is for the widget to say what it saw, over the
    same loopback port everything else already rides, and for a maintainer to read it.

    IN MEMORY ONLY, AND THAT IS A DECISION, not an omission. Nothing here touches disk
    and nothing survives a crabd restart. This is a scratch channel for a live debugging
    session, not history: persisting free text the widget composes would create a file
    that grows, that backups pick up, and that somebody later reads as a record of what
    happened. `droppedTotal` exists precisely so a reader can tell they are looking at a
    tail rather than assuming the ring is the whole story.

    THE LINES ARE DATA, NEVER INSTRUCTIONS. crabd stores them verbatim, serves them
    verbatim, and NOTHING in this daemon reads them back into any decision path - not the
    state build, not the permission broker, not the continue queue, not a config write.
    That is the prompt-injection posture, and it is a property of the WIRING rather than
    of the content: this ring has exactly one reader (the GET below) and it hands the
    bytes to a human. Any future caller that parses a line in here is the change that
    breaks the property, so it is the change to refuse.

    Bounded by the ring alone - no rate limit, by design. The worst legal body (50 lines
    at 300 chars) costs one list extend and one slice under a lock held for neither IO
    nor a build, and the memory ceiling stays fixed at 500 prefixed lines whatever the
    caller does.
    """

    def __init__(self, limit: int = PANEL_LOG_MAX_LINES) -> None:
        self._lock = threading.Lock()
        self._limit = max(1, int(limit))
        self._lines: list[str] = []
        self._dropped = 0

    def append(self, lines: list[str], now: float) -> int:
        """Store already-normalized strings, each carrying the server-side prefix.
        -> how many were stored.

        ONE timestamp for the whole batch: they arrived in one request, so one receive
        time is the honest reading, and it makes the order inside a batch the order the
        widget wrote them in rather than an artefact of how fast this loop runs.

        The prefix is why the widget never has to timestamp. Its clock is this same
        machine, so the value would agree - but a uniform, server-applied prefix is what
        guarantees the ORDERING is crabd's and makes a second source safe to add later
        without renegotiating the format with whoever wrote the first one.
        """
        prefix = f"{_utc_iso(now)} {PANEL_LOG_MARKER} "
        stamped = [prefix + line for line in lines]
        if not stamped:
            return 0
        with self._lock:
            self._lines.extend(stamped)
            overflow = len(self._lines) - self._limit
            if overflow > 0:
                # Slice-delete, not a pop-per-line: a single oversized batch evicts in
                # one operation instead of 500 under the lock.
                del self._lines[:overflow]
                self._dropped += overflow
        return len(stamped)

    def snapshot(self) -> tuple[list[str], int]:
        """-> (a COPY of the ring, oldest first; lines EVICTED since this crabd started).

        A copy, so a reader iterating the result cannot be tripped by a concurrent POST
        mutating the list underneath it. `droppedTotal` counts ring evictions ONLY - not
        the lines dropped past the 50-per-post cap and not truncated characters, both of
        which the caller knew about when it sent them.
        """
        with self._lock:
            return list(self._lines), self._dropped


class OriginRecorder:
    """Distinct (`Origin`, `source`) pairs seen on the request paths (v0.25.0, ORIGIN-REC).

    DIAGNOSTIC ONLY. This feeds GET /v1/health.originsSeen and NOTHING else - it is never
    a `build()` input, never in /v1/state, and never read back into a decision path. Its
    single purpose is to let a maintainer read what Origin the real QtWebEngine widget sends
    from its live polling, which is the measurement the SEC-a allowlist fix is blocked on.

    v0.27.0 - keyed on the DISTINCT (origin, source) PAIR, not origin alone. Several local
    sources send no Origin (the notifier, curl health checks, possibly the widget), so
    origin-only keying collapsed them into one uninformative "<absent>" bucket. `source` is
    a coarse bucket derived from the User-Agent (_classify_ua_source): "browser" | "local"
    | "none". Now `null`-from-a-browser and `<absent>`-from-a-local-process are SEPARATE
    rows - which is the entire point, because it is what isolates the widget.

    ⚠ `source` is DIAGNOSTIC ONLY and derived from an ATTACKER-CONTROLLED User-Agent. It
    NEVER feeds _is_web_origin or any gate - the CSRF gate stays origin-based, unchanged.
    This recorder only MEASURES; it does not enforce. A future SEC-a fix that keys the GATE
    on "absent vs null" (the clean discriminator IF the widget proves to send absent, not
    null) is a SEPARATE, deliberate change to _is_web_origin - not something this recorder
    does or licenses.

    LRU-BOUNDED at ORIGIN_RECORDER_MAX distinct pairs: the recorder sits on the
    unauthenticated request path, so requests with random forged Origins (or UAs) could
    otherwise grow it without bound. A repeat pair bumps its count and refreshes its slot;
    a NEW pair past the cap evicts the least-recently-seen one. Absent Origin is folded to
    the literal ORIGIN_ABSENT so "no header" is itself a countable value. The raw UA is kept
    (truncated to ORIGIN_UA_MAX) per entry as evidence of which build is polling.
    Same lock idiom as PanelLog / HostSampler."""

    def __init__(self, limit: int = ORIGIN_RECORDER_MAX) -> None:
        self._lock = threading.Lock()
        self._limit = max(1, int(limit))
        # (origin string, source) -> [count, last_seen_epoch, raw_ua|None]. OrderedDict so
        # recency is the eviction order, exactly like the cumulative-series/delta-day caps.
        self._seen: "OrderedDict[tuple, list]" = OrderedDict()

    def record(self, origin, user_agent, now: float) -> None:
        """origin is the raw header value (str) or None for an absent header; user_agent
        likewise. Total by construction: it is called on every GET and POST before the
        HOST and ORIGIN gates - a rebound page's origin is the reading this exists for -
        so it must never raise into the request path. The (origin, source)
        pair is the key - source classifies the caller (browser/local/none) so the widget
        is separable from other no-Origin local processes."""
        origin_key = origin if isinstance(origin, str) else ORIGIN_ABSENT
        source = _classify_ua_source(user_agent)
        ua = (user_agent[:ORIGIN_UA_MAX]
              if isinstance(user_agent, str) and user_agent.strip() else None)
        key = (origin_key, source)
        with self._lock:
            entry = self._seen.get(key)
            if entry is None:
                self._seen[key] = [1, now, ua]
            else:
                entry[0] += 1
                entry[1] = now
                entry[2] = ua                       # keep the most-recent raw UA
            self._seen.move_to_end(key)             # most-recently-seen last
            while len(self._seen) > self._limit:
                self._seen.popitem(last=False)      # evict least-recently-seen

    def snapshot(self) -> list:
        """-> [{origin, source, userAgent, count, lastSeenAt}], least-recently-seen first.
        A fresh list of fresh dicts, so a reader cannot be tripped by a concurrent
        record()."""
        with self._lock:
            return [{"origin": origin, "source": source, "userAgent": ua,
                     "count": count, "lastSeenAt": _utc_iso(last)}
                    for (origin, source), (count, last, ua) in self._seen.items()]


# ------------------------------------------------------------------ state builder

class StateBuilder:
    def __init__(self, store: TranscriptStore, hooks: HookTracker,
                 limits: LimitsReader, started_at: float,
                 config: UserConfig | None = None,
                 recap: "RecapReader | None" = None,
                 fleet: "FleetReader | None" = None,
                 history: "HistoryLog | None" = None,
                 statusline: "StatusLineReader | None" = None,
                 otlp: "OtlpReceiver | None" = None,
                 continues: "ContinueQueue | None" = None,
                 permissions: "PermissionBroker | None" = None,
                 host: "HostSampler | None" = None,
                 models: "ModelCatalog | None" = None) -> None:
        self.store = store
        self.hooks = hooks
        self.limits = limits
        self.recap = recap
        self.fleet = fleet
        # All four v0.12.0 readers are OPTIONAL and default to None, which is the
        # "feature not wired" state a unit test gets: limits fall back to OAuth,
        # costUSD is null, and the continue/permission endpoints answer as if the
        # operator had never enabled them. Nothing here fabricates a value when its
        # source is absent.
        self.statusline = statusline
        self.otlp = otlp
        self.continues = continues
        self.permissions = permissions
        # v0.28.0 model catalog. OPTIONAL, unlike HostSampler and for the opposite
        # reason: this one is the only object in the builder that reaches the NETWORK on
        # its own, off the operator's own OAuth token. A default-constructed catalog
        # would put every unit test one forgotten patch away from a live request signed
        # with the real credentials. Absent, `contextWindowTokens` falls through to the
        # status line and the model marker, and is null when neither knows - which is a
        # served path in production too (a marker-less model on a crabd whose fetch is
        # failing), so no test is silently exercising a shape nobody ships.
        self.models = models
        # GET /v1/history reads this directly - it is a view over the file, not over any
        # part of the built snapshot. None (a unit-test builder) serves empty days, which
        # is the same answer a crabd whose history file does not exist yet gives.
        self.history = history
        self.config = config or UserConfig()
        self.git = GitLookup()
        # v0.13.0 depletion forecast. One per builder: its rolling per-window history is
        # the state, and it must persist across builds (the whole point is a trend), so
        # it lives here rather than being reconstructed each build().
        self._forecaster = DepletionForecaster()
        # v0.22.0 host CPU/memory. Constructed here rather than passed in like the
        # v0.12.0 readers, and NOT optional: the sampler is total by construction and
        # answers "I cannot read this machine" by serving no `host` key, so a builder
        # with none attached would be indistinguishable from one on a host with no
        # counters - and every unit-test builder would then be silently testing the
        # absent path. `host=` exists only so a test can pin the arithmetic.
        # Its `_prev` FILETIMEs are per-builder state that must survive across builds,
        # for the same reason the forecaster's history does: the value IS the delta.
        self._host = host if host is not None else HostSampler()
        # v0.24.0 panel diagnostics ring. Constructed here and NOT optional, for the same
        # reason HostSampler is: an absent one would make /v1/panel-log answer differently
        # under test than in production, and the endpoint's whole job is to be reachable
        # when something on the glass is being debugged. It holds no resource and starts
        # empty, so there is nothing a test would want to opt out of. NOT part of the
        # served state document - it is a side channel, never a `build()` input.
        self.panel_log = PanelLog()
        # v0.25.0 origin recorder (ORIGIN-REC). Constructed here and NOT optional, same
        # reasoning as PanelLog: diagnostic side channel, holds no resource, starts empty,
        # never a build() input. Its whole job is to be populated from the live request
        # path so the widget's true Origin can be measured for the SEC-a allowlist fix.
        self.origins = OriginRecorder()
        self.started_at = started_at
        self._lock = threading.Lock()
        self._state: dict | None = None

    def ack(self, session_id: str) -> bool:
        """POST /v1/action {"action":"ack"}. False = 404: crabd is not serving that id."""
        state = self.state
        served = any(row["id"] == session_id for row in (state or {}).get("sessions", []))
        return self.hooks.ack(session_id, create=served)

    def ack_all(self) -> int:
        """POST /v1/action {"action":"ack-all"} - every unacked needs_input session.

        Scoped to the SERVED rows, which is the same rule single ack uses: acking a
        session the widget cannot see would write an "acknowledged from Edge" event
        for something nobody looked at. Returns how many landed; the endpoint answers
        204 either way, including zero (contract).
        """
        acked = 0
        for row in (self.state or {}).get("sessions", []):
            if row.get("state") == "needs_input" and not row.get("acked"):
                if self.hooks.ack(row["id"], create=True):
                    acked += 1
        return acked

    def serving(self, session_id: str) -> bool:
        """Is this id on a row the widget can actually see? The gate for every write
        that arrives naming a session - ack, an OTLP error event, a queued continue."""
        return any(row["id"] == session_id
                   for row in (self.state or {}).get("sessions", []))

    def transcript_age(self, session_id: str, now: float) -> float | None:
        """Seconds since this session's MAIN transcript last moved, or None when no
        transcript is known (a hook-only row: age is unknowable, not zero). Subagent
        files are excluded for _blank_session's reason - a subagent writing is not the
        main session being alive. GHOST-a (v0.28.1): the continue queue's liveness
        check."""
        newest = 0.0
        for facts in self.store.snapshot():
            if facts.session_id == session_id and not facts.is_subagent:
                # mtime ONLY, not last_ts: liveness is "the FILE moved", and a late
                # write bumps mtime whatever timestamp rides inside the record. last_ts
                # is the record's own clock and can sit minutes behind a live turn.
                newest = max(newest, facts.mtime)
        return None if newest <= 0.0 else max(0.0, now - newest)

    def record_hook(self, payload) -> None:
        """v0.19.0. THE hook ingest - /v1/hook and /v1/hook/stop both land here.

        Two jobs, and the second is why this exists rather than a bare hooks.record():
        a hook that ends or restarts a turn (PERMISSION_STALE_EVENTS) also retires any
        permission hold still parked for that session, because the dialog it mirrors was
        answered in the app before the turn could move. HookTracker deliberately does not
        know the broker - it is the pure state machine - so the join lives at the builder,
        which owns both.

        Total by construction: `hooks.record` already ignores a malformed payload, the
        broker is optional (None on a unit-test builder), and `stale` on a session with
        nothing parked is a dict lookup that returns None.
        """
        self.hooks.record(payload)
        if self.permissions is None or not isinstance(payload, dict):
            return
        event = payload.get("hook_event_name") or payload.get("hookEventName")
        if event not in PERMISSION_STALE_EVENTS:
            return
        session_id = _session_id(payload)
        if session_id:
            self.permissions.stale(session_id)

    def note_session_event(self, session_id: str, text: str) -> bool:
        """OTLP's route onto a session's events ring (v0.12.0).

        Scoped to SERVED rows, the same rule ack uses. Telemetry arrives for every
        session on the machine including ones crabd has aged out, and a receiver that
        created a row per api_error would let a stream of 429s from a session finished
        an hour ago grow the table with entries nothing renders.
        """
        return self.hooks.note_external(session_id, text,
                                        create=self.serving(session_id))

    @property
    def state(self) -> dict | None:
        with self._lock:
            return self._state

    def build(self, now: float | None = None, limits: dict | None = None) -> dict:
        now = now or time.time()
        self.store.scan(now)
        self.hooks.prune(now)
        # Without these the statusline per-session dict and the OTLP day/series dicts grow
        # unbounded over a long-running crabd (data-lane finding, 2026-08-26). Reads stay
        # correct via freshness checks; this bounds memory.
        # PRESENCE-GUARDED: all four v0.12.0 readers are optional and default to None
        # (see __init__), which is what a unit-test builder and a crabd running without
        # the feature both get. Calling through unguarded turned every such builder into
        # an AttributeError - 226 of them in one suite run on 2026-08-26.
        if self.statusline is not None:
            self.statusline.prune(now)
        if self.otlp is not None:
            self.otlp.prune(now)
        limits = self._limits_block(now, limits)

        per_session: dict[str, dict] = {}
        requests: dict[str, tuple[float, int, int, int, int, str | None]] = {}
        request_owner: dict[str, str] = {}

        # snapshot(), never `.files.values()`: two builds can run at once at cold start
        # and scan()'s delete sweep would otherwise mutate the dict this loop is walking
        # (CRB-F2). See TranscriptStore's docstring.
        for facts in self.store.snapshot():
            row = per_session.setdefault(facts.session_id, self._blank_session())
            row["mtime"] = max(row["mtime"], facts.mtime)
            if facts.is_subagent:
                row["sub_total"] += 1
                if now - facts.mtime <= SUBAGENT_ACTIVE_SEC:
                    row["sub_active"] += 1
                    row["sub_files"].append(facts)
            else:
                row["title"] = facts.title()
                row["title_source"] = facts.title_source()
                row["cwd"] = facts.last_cwd
                row["model"] = facts.last_model
                row["speed"] = facts.last_speed
                row["question"] = facts.question
                row["question_ts"] = facts.question_ts
                # .labels()/.usage_records() hand back COPIES taken under the file's own
                # lock. Iterating the live dicts raced refresh() on another thread - the
                # half of CRB-F2 the store lock never covered (FileFacts.__init__).
                row["agent_labels"].update(facts.labels())
                # Newest main transcript wins. A session id can own a main file under
                # two project dirs (its cwd moved), and the loop order over those is
                # arbitrary - dating the pick is what stops the served context size
                # flipping between the two on alternate passes.
                if (facts.context_tokens is not None
                        and facts.context_ts > row["context_ts"]):
                    row["context_tokens"] = facts.context_tokens
                    row["context_ts"] = facts.context_ts
                # v0.19.0 turn clock. Same number as context_ts, kept SEPARATELY on
                # purpose: context_ts is provenance for a served figure and carries that
                # branch's `context_tokens is not None` tie-break, while this one is the
                # state machine's evidence-of-life and must be a plain max over the main
                # files. Coupling them would let a change to either rule move the other.
                row["turn_ts"] = max(row["turn_ts"], facts.context_ts)
            for request_id, record in facts.usage_records().items():
                requests[request_id] = record
                request_owner[request_id] = facts.session_id

        # v0.19.0, and it has to run BEFORE the snapshot below - the snapshot is a copy,
        # so a clear applied after it would not reach the rows this build serves and the
        # panel would keep alerting for one more poll. Keyed by the transcript's OWN
        # session id, which is what makes "a UserPromptSubmit from a DIFFERENT session
        # must not clear it" structural rather than a check: there is no cross-session
        # path into note_activity at all.
        for sid, row in per_session.items():
            if row["turn_ts"]:
                self.hooks.note_activity(sid, row["turn_ts"])
        hook_rows = self.hooks.snapshot()

        for sid, row in hook_rows.items():
            entry = per_session.setdefault(sid, self._blank_session())
            if entry["cwd"] is None:
                entry["cwd"] = row.get("cwd")

        burn, session_output = self._burn(requests, request_owner, now)
        # Attached HERE rather than inside _burn: the budget is a config fact, and _burn
        # is a pure function of the usage records that the burn suites call directly.
        # Absent config leaves the key off entirely (contract) - presence IS the feature
        # detection on both the widget and the notifier.
        config = self.config.get(now)
        budget = budget_block(config, burn["today"]["outputTokens"])
        if budget is not None:
            burn["budget"] = budget
        # Both keys ALWAYS present, both null without telemetry (contract v0.12.0 §2).
        # Unlike `budget`, whose absence is the feature detection, cost is a number the
        # widget always has a place for - and null there is the honest "SideCrab cannot
        # see your spend", which a missing key would render as nothing at all.
        cost = self.otlp.cost_today(now) if self.otlp else None
        burn["costUSD"] = cost
        burn["costSource"] = BURN_COST_SOURCE_OTLP if cost is not None else None
        sessions = self._sessions(per_session, hook_rows, session_output, now)
        # The tracker owns the history file but only the builder reads transcripts, so
        # the title a history line carries comes from here, one pass behind at worst.
        self.hooks.note_titles({row["id"]: row["title"] for row in sessions})
        if self.recap:
            self.recap.submit(*self._recap_inputs(hook_rows, now))

        document = {
            "schema": SCHEMA_BREAKING,
            "generatedAt": _utc_iso(now),
            "crabd": {"version": VERSION, "startedAt": _utc_iso(self.started_at),
                      "hooksSeen": self.hooks.count},
            "limits": limits,
            "burn": burn,
            "sessions": sessions,
            # `active` in here is the EFFECTIVE answer since v0.23.0 - schedule with the
            # operator's panel override applied. Every consumer reads it and none of them
            # needed a change; see quiet_state.
            "quiet": quiet_state(config, now),
            # v0.12.0: the operator's EXTRA continue buttons. /v1/state is the widget's
            # only channel - it has no way to read config.json - so the config-only key
            # has to ride the feed to reach the sheet it configures. Always a list
            # (empty is the common case), never absent: the widget appends it to its
            # hardcoded defaults, and a missing key and an empty one mean the same
            # thing to it.
            "continuePrompts": self.config.continue_extras(now),
            # v0.29.0 (additive): whether taps may decide, and that `decide` now needs
            # the pairing code + requestId. Presence-detected by the widget.
            "approvals": {"enabled": self.config.panel_approvals(now),
                          "tokenRequired": True},
            # v0.18.0: the toast settings, for the same reason continuePrompts rides here
            # - /v1/config is POST-only, so the feed is the widget's ONLY read path to
            # config.json. Always present; `approvalThresholdSec` inside it is not, and
            # that absence is load-bearing (see toast_block).
            "toast": toast_block(config),
            "recap": self.recap.get() if self.recap else None,
            # No reader attached (unit tests) is exactly the "cannot read it" case, and
            # it is served as unknown - never as a pair of green dots.
            "fleet": self.fleet.get() if self.fleet else FleetReader.unknown(),
        }
        # v0.22.0 `host`, and it is sampled HERE - on the builder's own 2 s pass - which
        # is what gives the CPU delta its window. PRESENCE is the feature detection
        # (STATE-CONTRACT.md v0.22.0): the key is omitted entirely when the machine's
        # counters cannot be read at all, so `fleet`'s always-present-but-unknown idiom
        # is deliberately NOT copied - `fleet` names two things crabd owns and must
        # report on, while `host` is a capability the panel simply does or does not have.
        host = self._host.sample()
        if host is not None:
            document["host"] = host
        return document

    def _limits_block(self, now: float, override: dict | None) -> dict:
        """`limits`, with the v0.12.0 `source` provenance stamped on it.

        Precedence is statusline > OAuth, and the ONLY thing that demotes the status
        line is silence (StatusLineReader.limits returns None past
        STATUSLINE_PREFER_SEC). Deliberately not "prefer whichever is fresher": the
        status line is a documented stdin contract and the OAuth call is a reach-around
        that this feature exists to retire, so a stale-but-live status line still wins
        over an endpoint SideCrab should not be poking.

        `override` is the tests' injected block. It is stamped too rather than passed
        through untouched - `source` is not optional in the served document, and a code
        path that can emit a limits block without provenance is a code path that will.
        """
        if override is None and self.statusline is not None:
            served = self.statusline.limits(now)
            if served is not None:
                served["source"] = LIMITS_SOURCE_STATUSLINE
                # v0.13.0: feed the served windows into the forecast and attach exhaustAt.
                # Keyed per window, so the statusline and OAuth sources share a history
                # and a flip that re-reads a lower number resets it as a drop (below).
                self._forecaster.annotate(served, now)
                return served
        block = override if override is not None else self.limits.get(now)
        # Copied before stamping: `limits.get` hands back its own cached dict, and
        # writing into it would mutate the reader's last-good reading.
        stamped = dict(block) if isinstance(block, dict) else block
        if isinstance(stamped, dict):
            stamped["source"] = LIMITS_SOURCE_OAUTH
            self._forecaster.annotate(stamped, now)
        return stamped

    def _recap_inputs(self, hook_rows, now: float):
        """The cheap half of `recap`, handed to the recap thread.

        sessionsToday is counted from the TRANSCRIPT SCAN, not from the served sessions
        list: the served list drops gone rows and done rows after 10 minutes, so by
        evening it holds a fraction of the sessions the day actually had. A subagent
        file counts toward its parent session id, which is why this is a set of ids and
        not a file count.

        Repo candidates are config's `recapRepos` FIRST, then today's session cwds
        newest-first. Order matters twice: dedupe keeps the first sighting of a repo,
        and RECAP_REPO_SCAN_CAP cuts the tail - so a repo the operator named explicitly
        can never be crowded out by a busy day's incidental cwds.

        CD-11 (v0.21.0): the HOOK side is unioned in, so `doneToday <= sessionsToday`
        holds BY CONSTRUCTION rather than by the two sources happening to agree. They do
        not always: a session whose transcript crabd never admitted - one older than
        TRANSCRIPT_WINDOW_SEC, one under a projects dir crabd cannot read, or a Stop
        hook from a machine whose transcripts are elsewhere - counts toward doneToday
        and counted toward nothing here. Reproduced 2026-08-27: sessionsToday=0 beside
        doneToday=1, which the panel renders as "1 of 0 finished".
        """
        midnight = _local_midnight(now)
        # Every session that FINISHED today is a session that HAPPENED today, whether or
        # not a transcript for it survives; same for any hook row that moved today.
        sessions: set[str] = set(self.hooks.done_ids(now))
        sessions.update(sid for sid, row in hook_rows.items()
                        if row.get("at", 0.0) >= midnight)
        candidates: list[tuple[float, str]] = []
        for facts in self.store.snapshot():          # CRB-F2, as in build()
            activity = max(facts.mtime, facts.last_ts)
            if activity < midnight:
                continue
            sessions.add(facts.session_id)
            if not facts.is_subagent and facts.last_cwd:
                candidates.append((activity, facts.last_cwd))
        for row in hook_rows.values():
            if row.get("at", 0.0) >= midnight and row.get("cwd"):
                candidates.append((row["at"], row["cwd"]))

        ordered = self.config.recap_repos(now) + [
            cwd for _, cwd in sorted(candidates, key=lambda c: -c[0])]

        repos: list[tuple[str, str]] = []
        seen: set[str] = set()
        for cwd in ordered:
            # GitLookup is cached and reads .git/HEAD directly - no subprocess here.
            # A cwd that is not a repo has no name to key commits by, so it is dropped
            # rather than shelled out to.
            repo, _branch = self.git.get(cwd)
            if not repo or repo in seen:
                continue
            seen.add(repo)
            repos.append((repo, cwd))
        return (len(sessions), self.hooks.done_today(now), repos,
                self.hooks.done_by_day(now))

    @staticmethod
    def _blank_session() -> dict:
        return {"title": None, "title_source": None, "cwd": None,
                "model": None, "speed": None,
                "mtime": 0.0, "sub_total": 0, "sub_active": 0, "sub_files": [],
                "agent_labels": {}, "question": None, "question_ts": 0.0,
                "context_tokens": None, "context_ts": 0.0,
                # v0.19.0: newest completed model round-trip in the MAIN transcript.
                # Subagent files never reach it - a background subagent finishing is not
                # the operator answering, and folding its records in here would clear a
                # question that is genuinely still standing. 0.0 = no usage record yet,
                # which fails safe (note_activity is only ever called on a truthy value).
                "turn_ts": 0.0}

    @staticmethod
    def _burn(requests, request_owner, now):
        midnight = _local_midnight(now)
        current_hour = _local_hour_start(now)
        buckets = {current_hour - i * 3600: 0 for i in range(24)}
        oldest = current_hour - 23 * 3600
        days = _local_day_starts(now, BURN_DAILY_DAYS)
        day_buckets = {d: 0 for d in days}
        today = {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0,
                 "cacheCreationTokens": 0, "messages": 0}
        per_session_out: dict[str, int] = {}
        model_out: dict[str, int] = {}

        for request_id, record in requests.items():
            ts, out, inp, cache_read, cache_create, model = record
            if ts >= midnight:
                # byModel is built INSIDE the same today-window branch, off the same
                # deduped records, so sum(byModel) == today.outputTokens by construction
                # before the cap is applied - not by a second pass that could drift.
                key = model or BURN_MODEL_UNKNOWN
                model_out[key] = model_out.get(key, 0) + out
                today["outputTokens"] += out
                today["inputTokens"] += inp
                today["cacheReadTokens"] += cache_read
                today["cacheCreationTokens"] += cache_create
                today["messages"] += 1
                sid = request_owner.get(request_id)
                if sid:
                    per_session_out[sid] = per_session_out.get(sid, 0) + out
            hour = _local_hour_start(ts)
            if oldest <= hour <= current_hour:
                buckets[hour] += out
            day = _local_midnight(ts)
            if day in day_buckets:
                day_buckets[day] += out

        hourly = [{"hourStart": _local_iso(h), "outputTokens": buckets[h]}
                  for h in sorted(buckets)]
        daily = [{"dayStart": _local_day(d), "outputTokens": day_buckets[d]} for d in days]
        # An "unknown" row worth 0 tokens is noise, not honesty - it says a model went
        # unidentified when nothing was spent under it. A NAMED model at 0 is kept: it
        # is a real reading, and desc ordering parks it at the tail anyway.
        if not model_out.get(BURN_MODEL_UNKNOWN):
            model_out.pop(BURN_MODEL_UNKNOWN, None)
        by_model = [{"model": m, "outputTokens": t} for m, t in
                    sorted(model_out.items(), key=lambda kv: (-kv[1], kv[0]))
                    ][:BURN_MODEL_CAP]
        return ({"today": today, "hourly": hourly, "daily": daily, "byModel": by_model},
                per_session_out)

    def _sessions(self, per_session, hook_rows, session_output, now):
        order = {"needs_input": 0, "working": 1, "done": 2, "idle": 3}
        rows = []
        for sid, info in per_session.items():
            hook = hook_rows.get(sid)
            last_activity = max(info["mtime"], hook["at"] if hook else 0.0)
            if not last_activity:
                continue
            state, since = self._resolve(hook, info["mtime"], last_activity, now)
            if state == "gone":
                continue
            if state != "needs_input" and now - last_activity > SESSION_WINDOW_SEC:
                continue
            cwd = info["cwd"] or (hook.get("cwd") if hook else None)
            repo, branch = self.git.get(cwd)
            running = info["sub_active"]
            if hook:
                running = max(0, running - len(hook["stops"]))
            turn_started = (hook or {}).get("turn_started")
            # The cwd tier runs on the RESOLVED cwd (transcript, else the hook payload),
            # not on facts.last_cwd: a session whose transcript has not been parsed yet
            # is exactly the row that has no title, and its only cwd is the hook's.
            title, title_source = info["title"], info["title_source"]
            if not title:
                # Placeholders, NOT tiers - titleSource stays null for both, so the
                # widget never styles "session" or an id stub as a derived title.
                derived = _cwd_title(cwd) if cwd else None
                title = derived or (sid[:8] if cwd else "session")
                title_source = "cwd" if derived else None
            rows.append({
                "id": sid,
                "title": title,
                "titleSource": title_source,
                "cwd": cwd,
                "repo": repo,
                "branch": branch,
                "state": state,
                "stateSince": _utc_iso(since),
                "lastActivityAt": _utc_iso(last_activity),
                "lastEvent": (hook or {}).get("last_event") or self._implied_event(state),
                "model": info["model"],
                "speed": info["speed"],
                "subagents": {"running": running, "total": info["sub_total"]},
                "todayOutputTokens": session_output.get(sid, 0),
                "question": self._question(state, hook, info, since),
                # A turn that aged out without a Stop hook is not still running; showing
                # "working 3h" on an idle card would be a lie the widget can't detect.
                "turnStartedAt": (_utc_iso(turn_started)
                                  if turn_started and state in ("working", "needs_input")
                                  else None),
                "acked": bool((hook or {}).get("acked")),
                "subagentDetail": self._subagent_detail(
                    info, running, now, (hook or {}).get("stops") or ()),
                "events": list((hook or {}).get("events") or []),
                # Subagent files are excluded upstream: a subagent's usage record
                # describes ITS window, not the one the operator is watching fill.
                **self._context(sid, info, now),
                # v0.28.0, additive: the DENOMINATOR for the line above. Always present,
                # null when unknown - the key is the widget's feature detection, exactly
                # as queuedContinue's is.
                "contextWindowTokens": self._context_window(sid, info, now),
                # Null when nothing is waiting. A pending entry means a live PermissionRequest
                # hook is parked on the long poll RIGHT NOW - it is not a record of one that
                # happened, and it disappears the instant the hook is answered or times out.
                "pendingPermission": (self.permissions.pending(sid)
                                      if self.permissions else None),
                # v0.14.0, additive. The queue was already observable in aggregate and
                # nowhere per-card, so a queued prompt was invisible until the operator
                # opened the sheet that queued it. Null once drained by the Stop hook or
                # aged out at CONTINUE_TTL_SEC - the card must never advertise a prompt
                # that will not be delivered.
                "queuedContinue": (self.continues.entry(sid, now)
                                   if self.continues else None),
            })
        rows.sort(key=lambda r: (order.get(r["state"], 9), -_parse_ts(r["lastActivityAt"])))
        return rows

    def _context(self, sid: str, info: dict, now: float) -> dict:
        """`contextTokens` + the v0.12.0 `contextSource` provenance.

        The status line wins when it has spoken about THIS session, including when what
        it said is "unknown" - it reads the live context window off the session itself,
        while the transcript figure is arithmetic over the newest usage record crabd
        happened to parse, and after a compaction those two disagree by the whole
        window. `known` is what separates "the status line says unknown" from "the
        status line has never mentioned this session"; only the second falls back.

        contextSource is `null` exactly when contextTokens is - a source label on an
        absent number would be provenance for nothing.

        CD-36: the status line wins on PRECEDENCE, not on retention. It is offered the
        transcript's own reading time (less CONTEXT_STATUSLINE_LEAD_SEC, which absorbs
        the two clocks - `context_ts` is the CLI's record timestamp, the statusline's is
        crabd's receipt clock) and declines when its reading is the older of the two. A
        live status line always passes this: it posts a document right after the very
        round-trip whose record the transcript carries.
        """
        if self.statusline is not None:
            known, tokens = self.statusline.context(
                sid, now, info["context_ts"] - CONTEXT_STATUSLINE_LEAD_SEC)
            if known:
                return {"contextTokens": tokens,
                        "contextSource": CONTEXT_SOURCE_STATUSLINE if tokens is not None
                                         else None}
        tokens = info["context_tokens"]
        return {"contextTokens": tokens,
                "contextSource": CONTEXT_SOURCE_TRANSCRIPT if tokens is not None else None}

    def _context_window(self, sid: str, info: dict, now: float) -> int | None:
        """v0.28.0 `contextWindowTokens` - the window contextTokens fills toward, in
        tokens, or None. THE REASON IT EXISTS: the widget derived this only from a
        [1m]/[200k] marker in the model id, and the live ids on this host carry no marker
        (measured 2026-08-28: "claude-fable-5", "claude-opus-5"), so no ctx-fill bar ever
        rendered on a real session.

        THREE sources, MOST SPECIFIC FIRST, and that ordering is the whole design:

          1. the status line's `context_window_size` - the CLI stating the window for
             THIS session, on the same freshness contest contextTokens takes;
          2. the model string's own marker - also session-specific (the CLI writes the
             marker for the window that session is running) and also stated by the feed;
          3. the model catalog's max_input_tokens - true of the MODEL, not of the
             session, so it is the last word and not the first.

        Rank 2 above rank 3 is not cosmetic. A served "claude-sonnet-4-6[200k]" against a
        catalog that reports 1000000 for claude-sonnet-4-6 is precisely the case: the
        marker is that session's window and the catalog is the model's ceiling, and
        preferring the ceiling would gauge the card at a fifth of its real fill - a bar
        that is wrong while looking exactly like one that is right. It also keeps the
        widget's own precedence honest: the served member wins there, so the server has
        to be the one that already honoured the marker.

        None all the way down when nothing knows, and null is what the widget needs to
        draw no bar at all. No table, no default window, no zero.
        """
        model = info.get("model")
        if self.statusline is not None:
            size = self.statusline.context_window(
                sid, now, info["context_ts"] - CONTEXT_STATUSLINE_LEAD_SEC)
            if size is not None:
                return size
        marker = _marker_window(model)
        if marker is not None:
            return marker
        if self.models is not None:
            return self.models.window(model, now)
        return None

    @staticmethod
    def _question(state, hook, info, since) -> str | None:
        """The hook message is the floor; the transcript wins only when it is both
        RICHER and belongs to this turn. Without the freshness guard an old question
        from three turns ago outranks the notification that is actually waiting.

        TWO guards, because the time window alone is not the test (CD-28, reproduced
        2026-08-27). QUESTION_FRESH_SEC is a 120 s LOOKBACK, and a turn can begin and
        raise a fresh question well inside it - so a richer question from the PREVIOUS
        turn, written 10 s before the operator's prompt started this one, still won and
        replaced the notification actually on screen. `turn_started` is the exact
        boundary the window was approximating: a question belonging to this turn cannot
        predate the UserPromptSubmit that opened it.

        The window stays as the fallback for the case turn_started cannot cover - a
        session crabd saw no UserPromptSubmit for, which is every session that was
        already running when crabd started.
        """
        if state != "needs_input":
            return None
        question = (hook or {}).get("question")
        enriched = info.get("question")
        if not enriched:
            return question
        asked_at = info.get("question_ts", 0.0)
        turn_started = (hook or {}).get("turn_started")
        # The grace absorbs two clocks: `question_ts` is the transcript record's own
        # timestamp and `turn_started` is crabd's clock at hook receipt.
        floor = (max(since - QUESTION_FRESH_SEC, turn_started - QUESTION_TURN_GRACE_SEC)
                 if turn_started else since - QUESTION_FRESH_SEC)
        if asked_at >= floor and (question is None or len(enriched) > len(question)):
            question = enriched
        return question

    @staticmethod
    def _subagent_detail(info, running: int, now: float, stops=()) -> list:
        """Running subagents only, newest first, capped. Trimmed to `running` so the
        badge count and the list can never disagree on the panel.

        CD-29 (v0.21.0): the STOPPED files are removed before the trim, not merely
        counted out of it. `running` was already `sub_active - len(stops)`, so the count
        was right - but the list was the newest `running` files by mtime, and a subagent
        that just stopped has the NEWEST mtime of all of them (its final record is the
        last thing written). So the one agent crabd knew had finished was the one the
        panel named as running, and a genuinely running older sibling was the one
        dropped. Reproduced 2026-08-27 with two subagents and one SubagentStop.

        A SubagentStop payload does not identify WHICH subagent stopped - `stops` is a
        list of times, which is all the tracker keeps - so each stop claims the file
        whose last write is nearest to it and not meaningfully after it
        (SUBAGENT_STOP_MATCH_SEC). That is a match on the only evidence there is, and
        it degrades safely: a stop that matches nothing leaves the trim to `running` as
        the backstop, exactly as before.
        """
        if running <= 0:
            return []
        candidates = list(info["sub_files"])
        for stop in sorted(stops):
            claimed = None
            for facts in candidates:
                if facts.mtime > stop + SUBAGENT_STOP_MATCH_SEC:
                    continue    # still being written after that stop - not its file
                if claimed is None or abs(facts.mtime - stop) < abs(claimed.mtime - stop):
                    claimed = facts
            if claimed is not None:
                candidates.remove(claimed)
        newest = sorted(candidates, key=lambda f: f.mtime, reverse=True)
        detail = []
        for facts in newest[:min(SUBAGENT_DETAIL_CAP, running)]:
            agent_id = facts.agent_id()
            label = info["agent_labels"].get(agent_id) or facts.label()
            detail.append({"label": _trim(label, SUBAGENT_LABEL_MAX) or agent_id[:8],
                           "ageSec": max(0, int(now - facts.mtime))})
        return detail

    @staticmethod
    def _implied_event(state: str) -> str:
        return {"working": "working", "idle": "quiet", "done": "finished"}.get(state, state)

    @staticmethod
    def _resolve(hook, transcript_mtime, last_activity, now) -> tuple[str, float]:
        """Hooks decide; transcript mtime ages. needs_input is never aged away -
        a question keeps waiting even when the transcript goes quiet (contract)."""
        state = hook.get("state") if hook else None
        since = hook.get("since") if hook else last_activity

        if state == "gone":
            return "gone", since
        if state == "needs_input":
            return "needs_input", since
        if state == "done":
            # "unless reactivated": a transcript write past the grace means work resumed
            # without the hooks saying so. v0.28.2: the reactivated row FALLS THROUGH to
            # the aging block instead of returning unaged `working` - the early return
            # made one late write (an async ai-title, a subagent straggler) a PERMANENT
            # working zombie, re-derived on every build until the 2h prune. Measured
            # live 2026-09-01: a finished session read `working · quiet 33m`.
            if transcript_mtime > since + DONE_REACTIVATION_GRACE_SEC:
                state = None   # the aging block below keys on last_activity, which
                               # already carries the transcript's own clock
            elif now - since > DONE_DROP_SEC:
                return "gone", since
            else:
                return "done", since

        age = now - last_activity
        if age > GONE_AFTER_SEC:
            return "gone", last_activity + GONE_AFTER_SEC
        if age > IDLE_AFTER_SEC:
            return "idle", last_activity + IDLE_AFTER_SEC
        return (state or "working"), (since if state else last_activity)


# --------------------------------------------------------------------- http server

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    builder: StateBuilder = None  # set on the class before serving
    # StreamRequestHandler.setup() turns this into a socket timeout, and
    # handle_one_request catches the resulting TimeoutError around the whole request.
    # Without it a client that announces a body and then goes quiet parks a thread for
    # as long as it likes. See SOCKET_TIMEOUT_SEC for why it does not bound the
    # permission long poll.
    timeout = SOCKET_TIMEOUT_SEC
    # TCP_NODELAY. BaseHTTPRequestHandler buffers the status line and headers and
    # flushes them in end_headers(), then writes the body as a SECOND send - the exact
    # two-write shape that meets Nagle plus the peer's delayed ACK and stalls for
    # hundreds of ms to seconds. Every consumer here is a request/response client on
    # loopback waiting for a small answer (a hook holding a session open, the widget's
    # poll, the status line command in front of the operator's prompt), so there is
    # nothing for Nagle to coalesce and everything for it to delay.
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):  # keep the console (and any log) quiet
        pass

    # Per-request Access-Control-Allow-Origin, set by do_GET / do_POST before any _send.
    # A reflected Origin string (the widget's opaque iCUE origin, "null") lets the widget
    # read the response; None emits no ACAO header at all - a non-browser client that
    # needs none, or a refused cross-site page that must not be handed one.
    #
    # "*" IS NO LONGER A LEGAL VALUE ANYWHERE (SEC-4, v0.16.0). It was the read
    # endpoints' setting until the audit pointed out what /v1/state actually contains:
    # cwds, session titles, the FULL question text and pendingPermission. With ACAO:*
    # any page the operator merely visited could read all of it cross-origin. The class
    # DEFAULT is now None, so a code path that forgets to set it fails closed (an
    # unreadable reply) instead of open.
    _acao: str | None = None

    def _send(self, code: int, body: bytes | None,
              ctype: str = "application/json") -> None:
        self.send_response(code)
        if body is None:
            self.send_header("Content-Length", "0")
        else:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        # UNCONDITIONAL, like the Cache-Control below it. "believe the Content-Type I
        # declared" has no reason to be a property of one branch, and a per-branch flag
        # is one more thing a new route can forget.
        self.send_header("X-Content-Type-Options", "nosniff")
        acao = self._acao
        if acao is not None:
            self.send_header("Access-Control-Allow-Origin", acao)
            # Every ACAO crabd emits is now a REFLECTION of the request Origin, so an
            # intermediary must not serve one origin's reply to another. (crabd is
            # loopback + no-store, so this is hygiene, not a live cache bug.)
            self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        # No preflight, on ANY path, is answered with ACAO:* any more (SEC-1 for the
        # mutating paths, SEC-4 for the reads): that header is what invites the
        # cross-origin read. A cross-site page's preflight gets no ACAO at all, so its
        # application/json request dies at the preflight; the panel's own origin and the
        # widget's opaque one are reflected so their preflights still pass.
        # The Host gate first, here as on the other two methods: a preflight answer is
        # the MAP of both other gates (which origins, which headers), and a rebound page
        # has no business reading it.
        if self._is_foreign_host(self.headers.get("Host"), self.server.server_port):
            self._acao = None
            self._send(403, HOST_NOT_ALLOWED)
            return
        origin = self.headers.get("Origin")
        acao = self._preflight_acao(origin)
        self.send_response(204)
        if acao is not None:
            self.send_header("Access-Control-Allow-Origin", acao)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers",
                             self._preflight_headers(origin))
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _preflight_acao(self, origin) -> str | None:
        """The Access-Control-Allow-Origin for a preflight, PATH-INDEPENDENT since
        v0.16.0: reads and writes are gated alike, so the answer depends only on the
        Origin. A cross-site page is refused with no ACAO; an allowed origin is
        reflected so its application/json preflight still succeeds - never "*"."""
        if self._is_cross_site(origin, self.server.server_port):
            return None
        return origin if origin else None

    def _preflight_headers(self, origin) -> str:
        """Which request headers this preflight may unlock. THE FORGED-NULL WRITE IS
        CLOSED HERE and nowhere else.

        `null` gets exactly Content-Type - the same list it got before 0.31.0 - and keeps
        its ACAO, so a `null` caller can still READ. A page that forged `Origin: null` (a
        sandboxed allow-scripts iframe on anything the operator visits) comes back from
        its preflight without permission to send PANEL_HEADER, and its POST therefore
        never leaves the browser.

        WHY A NON-WEB SCHEME GETS THE HEADER, and the measurement it rests on. The iCUE
        build's origin was MEASURED as `file://`, not `null` - originsSeen on 2026-09-02
        after the 0.27.0 import, `origin: file://` with an AppleWebKit/537.36 UA; the
        reading is ORIGIN-b in docs/BACKLOG.md. A web page cannot forge `Origin: file://`
        (a browser serialises an opaque origin as `null` and nothing else), so unlocking
        the header for a non-web scheme hands it to something a visited page cannot
        claim to be.

        THE ACCEPTED TRADE, stated rather than hidden: that measurement is one reading on
        one iCUE build. A build that reports `null` instead keeps its reads and loses its
        taps - the same shape as the 0.29.0 `decide` change, and safe for the same
        reason, since every write it makes has a terminal-side fallback. Widening `null`
        to close that would re-open the forged-null write for every browser on the
        machine, which is the trade going the wrong way.
        """
        if isinstance(origin, str) and origin.strip().lower() == "null":
            return "Content-Type"
        return f"Content-Type, {PANEL_HEADER}"

    def _record_origin(self, origin) -> None:
        """Feed the diagnostic origin recorder (ORIGIN-REC), ABOVE EVERY GATE so refused
        callers are counted too - the Host gate included, since a rebound page's origin
        is precisely the reading this recorder exists for. It decides nothing, which is
        what makes running it first safe. Defensive: never let a diagnostic write break a
        request - a builder without a recorder (an old test double) is a no-op."""
        builder = getattr(self, "builder", None)
        recorder = getattr(builder, "origins", None) if builder else None
        if recorder is not None:
            try:
                # User-Agent classifies the SOURCE (browser/local/none) so no-Origin
                # callers are separable. ATTACKER-CONTROLLED and DIAGNOSTIC ONLY - it
                # never reaches the origin gate below. Absent header -> None -> "none".
                user_agent = self.headers.get("User-Agent")
                recorder.record(origin, user_agent, time.time())
            except Exception as exc:    # noqa: BLE001 - never fail a request for this
                # Swallowed because a DIAGNOSTIC must not take a request down with it -
                # but said once, because a recorder that quietly stopped recording would
                # make /v1/health's originsSeen an empty answer rather than a broken one,
                # and that is the shape of failure this daemon forbids.
                _log_once(ORIGIN_RECORD_LOG_KEY,
                          f"crabd: the origin recorder raised {type(exc).__name__}; "
                          f"originsSeen may be incomplete; this is logged once")

    def do_GET(self):
        # SEC-4 (v0.16.0). The reads are gated exactly like the writes. /v1/state serves
        # cwds, session titles, the full text of the question a session is waiting on and
        # pendingPermission; under the old ACAO:* any page the operator visited could
        # read the lot cross-origin. Same predicate as the mutating gate, same 403 body:
        # a present http(s) Origin that is NOT this crabd's own is a visited page and is
        # refused; the panel's own origin (v0.31.0), absent, "null" and non-web origins
        # (the QtWebEngine widget, curl, local tools) are allowed and get their own
        # origin reflected back.
        # The Host gate runs first of the GATES, and on the reads especially: a
        # DNS-rebound page is SAME-ORIGIN with crabd as far as the browser is concerned,
        # so its GET carries no Origin at all and the allowlist below has nothing to
        # refuse it with. The recorder above it is not a gate - see _record_origin.
        origin = self.headers.get("Origin")
        self._record_origin(origin)
        if self._is_foreign_host(self.headers.get("Host"), self.server.server_port):
            self._acao = None
            self._send(403, HOST_NOT_ALLOWED)
            return
        if self._is_cross_site(origin, self.server.server_port):
            self._acao = None
            self._send(403, CROSS_SITE_REFUSED)
            return
        self._acao = origin if origin else None
        # THE SPLIT ONLY, and the narrowness is the point. `GET http://[::1/ HTTP/1.1` is
        # a legal request LINE - absolute-form is what a proxy sends, and
        # BaseHTTPRequestHandler accepts it - carrying an authority urlsplit refuses with
        # ValueError("Invalid IPv6 URL"); unguarded, that walked out of the handler into
        # socketserver's handle_error and printed a traceback for a request a scanner
        # sends by accident. A request target crabd cannot parse names nothing crabd
        # serves, so 404 is the honest answer and it is the one every other unroutable
        # path gets.
        #
        # The routing below is deliberately OUTSIDE it: wrapped, a ValueError from any
        # reader would answer this same 404, so a real bug would read exactly like a
        # mistyped path - silence, in the daemon that forbids it.
        try:
            split = urllib.parse.urlsplit(self.path)
        except ValueError:
            self._send(404, NOT_FOUND)
            return
        try:
            path = split.path.rstrip("/") or "/"
            if path == "/v1/health":
                self._send(200, dump_state(self._health()))
            elif path == "/v1/state":
                self._do_state()
            elif path == "/v1/history":
                self._do_history(split.query)
            elif path == "/v1/panel-log":
                self._do_panel_log_read()
            elif path == "/v1" or path.startswith("/v1/"):
                # STRICTLY ABOVE the panel routes. A mistyped endpoint keeps the JSON 404
                # it has always had; nothing about serving files may turn /v1/stat into a
                # page, or let a file under the panel root shadow an API path.
                self._send(404, NOT_FOUND)
            else:
                self._do_panel_file(path)
        except OSError as exc:      # noqa: BLE001 - narrowed on purpose, see below
            # The reader hung up before crabd finished answering. ORDINARY on this host
            # (loopback drops SYN-ACKs) and doubly so on a feed the widget polls every
            # 2-5 s beside the doctor's and the updater's own probes - and until v0.20.0
            # it walked out of the handler into socketserver's handle_error, which prints
            # a full traceback for a client that simply stopped listening. Same narrowing
            # and same reasoning as _send_stop_answer: BrokenPipe / ConnectionReset /
            # ConnectionAborted / Timeout are all OSError, and anything else raised from
            # in here is still a genuine surprise that still deserves its traceback.
            self.close_connection = True
            _log_once(GET_HANGUP_LOG_KEY,
                      f"crabd: a reader hung up before its GET was answered "
                      f"({type(exc).__name__}); this is logged once")

    def _do_state(self) -> None:
        """GET /v1/state - which NEVER answers 500 for a data-shape reason (v0.20.0).

        The guarantee is kept upstream of here: a transcript record crabd cannot read is
        skipped by the parser, so build() has no data-shape route to an exception at all.
        This is the backstop for the route nobody has found yet, and it has exactly two
        honest answers - never a traceback and never a fabricated document:

          - a snapshot exists (the ordinary case, including a build that has just failed
            while a good one from 2 s ago is still held): serve it. It is stale, and
            `generatedAt` says so, which is the same honest signal a wedged refresh
            thread already produces.
          - no snapshot has ever been built: 503. Serving `sessions: []` here would say
            "you have no sessions running", and inventing that answer is worse than
            admitting crabd has nothing yet - the widget retries in 2 s either way.
        """
        state = self.builder.state
        if state is None:
            # Cold start: the refresh thread's first build has not landed yet, so this
            # request builds one itself. That is the exact moment the observed crash
            # happened, and the only moment build() runs on the request path.
            try:
                state = self.builder.build()
            except Exception as exc:        # noqa: BLE001 - honest-failure rule
                _log_once(STATE_BUILD_LOG_KEY,
                          f"crabd: the first state build failed ({type(exc).__name__}); "
                          f"serving the last good snapshot if there is one")
                state = self.builder.state
        if state is None:
            self._send(503, STATE_NOT_BUILT)
            return
        self._send(200, dump_state(state))

    def _health(self) -> dict:
        """GET /v1/health - is crabd up, and ARE THE FEEDS ARRIVING (v0.14.0).

        `ok` and `version` are unchanged; everything else is additive, so nothing that
        reads health today notices. Health is not the state contract, so this needs no
        schema bump.

        The counters exist because "crabd answers 200" and "crabd is being fed" are
        different questions, and only the first one was askable. The wiring landed on
        2026-08-26 and the failure mode it invites is silent: a statusline command that
        stops being chained, a hooks block dropped out of settings.json, an OTLP
        exporter pointed elsewhere. All three leave a perfectly healthy crabd serving a
        document that quietly stops changing. `lastStatuslineAgeSec` is the sharp one -
        null means the status line has NEVER posted (not "posted a while ago"), which is
        the difference between misconfigured and idle.

        Every reader is presence-gated: a crabd (or a unit-test builder) running without
        one reports 0/null for it rather than failing the health check.
        """
        builder = self.builder
        now = time.time()
        statusline = getattr(builder, "statusline", None) if builder else None
        otlp = getattr(builder, "otlp", None) if builder else None
        hooks = getattr(builder, "hooks", None) if builder else None
        origins = getattr(builder, "origins", None) if builder else None
        token = getattr(builder, "panel_token", None) if builder else None
        last_at = statusline.last_at if statusline is not None else None
        return {
            "ok": True,
            "version": VERSION,
            "uptimeSec": int(max(0.0, now - builder.started_at)) if builder else 0,
            "hooksSeen": hooks.count if hooks is not None else 0,
            "statuslineSeen": statusline.documents if statusline is not None else 0,
            "lastStatuslineAgeSec": (int(max(0.0, now - last_at))
                                     if last_at else None),
            "otlpSeen": otlp.documents if otlp is not None else 0,
            # ORIGIN-REC (v0.25.0; v0.27.0 adds source/userAgent): the distinct
            # (origin, source) pairs seen on the request paths, the SEC-a measurement
            # enabler. Diagnostic - NOT the state contract, so no schema bump - and never
            # in /v1/state. See OriginRecorder.
            "originsSeen": origins.snapshot() if origins is not None else [],
            # v0.29.0: the pairing code's presence and lockout - never the code.
            "panelToken": (token.status(now) if token is not None
                           else {"present": False, "rejectedRecently": 0,
                                 "lockedUntil": None}),
            # v0.31.0: what this crabd will accept from a browser, and where the panel it
            # serves comes from. Diagnostic - "which origins does your crabd trust" and
            # "which build is it serving" were both answerable only by reading the source.
            # NOT the state contract, so no schema bump, and never in /v1/state.
            "panel": {"origins": sorted(self._panel_origins(self.server.server_port)),
                      "headerRequired": True,
                      "dir": str(PANEL_DIR)},
        }

    def _do_history(self, query: str) -> None:
        """GET /v1/history?day=YYYY-MM-DD - a read-only view over the persisted history.

        The two halves of "unknown day" are deliberately DIFFERENT answers, and the
        contract says so in two sentences: a day whose FORM is wrong is a 400 (the caller
        has a bug), a well-formed day with nothing in it is a 200 with no events (the
        operator did not work that day). Answering 200-empty for `day=yesterday` would
        turn a widget bug into a day that looks quiet.
        """
        days = urllib.parse.parse_qs(query).get("day") or []
        if len(days) != 1 or not self._valid_day(days[0]):
            self._send(400, b'{"error":"day must be a real date as YYYY-MM-DD"}')
            return
        day = days[0]
        log = self.builder.history
        events, truncated = log.day(day) if log is not None else ([], False)
        # `count` is the length of what was RETURNED, not the day's total - the pair
        # (count, truncated) is then self-consistent: 200 and true means "200 shown, more
        # exist", and the widget never has to reconcile a count with a shorter list.
        body = {"day": day, "events": events, "count": len(events),
                "truncated": truncated}
        # dump_state, not a bare json.dumps (CD-10 leftover): these events are parsed
        # out of a file on disk that anything on this machine can append to, and the
        # default encoder emits bare NaN/Infinity - which is not JSON, so one poisoned
        # line would dead-feed the widget's JSON.parse with no error anywhere. Every
        # other served document already goes through the one serializer; this was the
        # last that did not.
        self._send(200, dump_state(body))

    #: The only directories the panel is served out of. `index.html` is routed by NAME;
    #: everything else has to sit under one of these. An allowlist rather than a
    #: denylist because the alternative is "everything in the folder", and the folder
    #: also holds DEV.md (a quarter of a megabyte of internal measurement notes), the
    #: iCUE packaging manifest and the test harness. None of those is the panel.
    PANEL_ROOTS = frozenset(("styles", "scripts", "resources", "mock"))

    def _do_panel_file(self, path: str) -> None:
        """GET a file from PANEL_DIR - or 404. Never a traceback, never a read outside.

        Deliberately touches NOTHING else in this daemon: no builder, no lock, no
        reader. A wedged state build must not stop the panel loading (the operator would
        see a dead browser tab and no way to find out why), and a large file must not
        stall a hook (a hook with no answer is a session waiting for one).

        The files are small - the whole shipped panel is under a megabyte - so they are
        read whole and handed to _send, bounded by PANEL_MAX_BYTES (64 MB) checked by
        stat BEFORE the read, because "small" is a fact about the shipped tree and not
        about a directory CRABD_PANEL_DIR can point anywhere. There is no cache and no
        conditional-GET handling: Cache-Control: no-store is the daemon's one caching
        rule, and it is what stops a script surviving an update that the crabd it talks
        to did not.
        """
        target = self._panel_target(path)
        if target is None:
            self._panel_not_found()
            return
        # RESOLVE, then check containment. The text rules above cannot see a symlink:
        # `styles/escape.css` passes every one of them and can still point at ~/.ssh.
        root = PANEL_DIR.resolve()
        candidate = (root / target).resolve()
        if root not in candidate.parents or not candidate.is_file():
            self._panel_not_found()
            return
        # A file that raced away between is_file() and here reads as size 0, and the read
        # below then answers 404 on its own.
        try:
            size = candidate.stat().st_size
        except OSError:
            size = 0
        if size > PANEL_MAX_BYTES:
            _log_once(PANEL_TOO_BIG_LOG_KEY,
                      f"crabd: {candidate} is too big to serve "
                      f"({size} bytes, the bound is {PANEL_MAX_BYTES}); serving 404; "
                      f"this is logged once")
            self._send(404, NOT_FOUND)
            return
        try:
            body = candidate.read_bytes()
        except OSError as exc:
            # A file that resolved, is a file, and still cannot be read (permissions, a
            # racing delete). 404 rather than 500: a page crabd cannot serve is missing
            # as far as the browser is concerned, and the reason belongs in the log.
            _log_once(PANEL_READ_LOG_KEY,
                      f"crabd: a panel file could not be read ({type(exc).__name__}); "
                      f"serving 404; this is logged once")
            self._send(404, NOT_FOUND)
            return
        self._send(200, body,
                   PANEL_CONTENT_TYPES.get(candidate.suffix.lower(),
                                           PANEL_CONTENT_TYPE_DEFAULT))

    def _panel_not_found(self) -> None:
        """404 for a panel request - and, ONCE, the reason when the reason is that there
        is no panel.

        A PANEL_DIR pointing at a typo answers 404 for every asset while the API answers
        perfectly: the page is blank, and the first place anybody looks is the routing in
        this file. The stat is paid only on the 404 path, so an asset that is served
        never pays for it. Deliberately NOT logged for an ordinary missing file inside a
        panel that IS there - that is the normal answer, and it would be a line per
        favicon probe.
        """
        if not PANEL_DIR.is_dir():
            _log_once(PANEL_DIR_LOG_KEY,
                      f"crabd: the panel directory {PANEL_DIR} is not there, so every "
                      f"panel request answers 404 (the API is unaffected; set "
                      f"CRABD_PANEL_DIR); this is logged once")
        self._send(404, NOT_FOUND)

    @classmethod
    def _panel_target(cls, path: str) -> str | None:
        """The panel-relative path this URL may read, or None to refuse it.

        ONE percent-decode, then the refusals. One decode and not a loop: decoding twice
        is how `%252e%252e` becomes `..` in a server that thought it was being thorough,
        so a `%` that SURVIVES the single decode is itself a refusal rather than an
        invitation to decode again.

        Every rule here is a shape, not a guess about the filesystem:
          - a backslash: a separator on Windows and a legal filename character on POSIX,
            so `styles\\..\\x` is harmless on the host it was tested on and a traversal
            on the other
          - a NUL: truncates the path in any C library underneath
          - an empty segment (`//`): on some path implementations a leading `//` is an
            absolute path all of its own
          - a segment starting with `.`: covers `..`, `.` and every dotfile in one rule -
            `.git/config` is not a shape this needs a second test for
        """
        decoded = urllib.parse.unquote(path)
        if "%" in decoded or "\\" in decoded or "\x00" in decoded:
            return None
        if decoded in ("/", "/index.html"):
            return "index.html"
        if not decoded.startswith("/"):
            return None
        segments = decoded[1:].split("/")
        if any(not segment or segment.startswith(".") for segment in segments):
            return None
        if segments[0] not in cls.PANEL_ROOTS:
            return None
        return "/".join(segments)

    def _do_panel_log_read(self) -> None:
        """GET /v1/panel-log - the diagnostics ring, oldest first (contract v0.24.0).

        Gated by the SEC-4 read gate in do_GET like every other read, and it needs to be:
        these lines are composed by the widget while the operator touches the glass, so
        they describe what is on the panel. A visited web page has no business reading
        them cross-origin any more than it has reading /v1/state.

        `count` is the length of what was RETURNED - the same rule /v1/history's count
        follows - so it can never exceed PANEL_LOG_MAX_LINES. `droppedTotal` is what the
        ring has evicted since this crabd started; together the pair says whether the
        reader is looking at the whole session or at its tail, which a bare list cannot.
        """
        lines, dropped = self.builder.panel_log.snapshot()
        self._send(200, dump_state({"lines": lines, "count": len(lines),
                                    "droppedTotal": dropped}))

    @staticmethod
    def _valid_day(day) -> bool:
        """Regex AND strptime. The regex alone accepts 2026-02-30 and 2026-13-01; a bare
        strptime alone accepts "2026-2-3", which is not the contract's shape."""
        if not isinstance(day, str) or not HISTORY_DAY_RE.match(day):
            return False
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            return False
        return True

    def _read_body(self, limit: int = MAX_BODY_BYTES) -> bytes:
        """`curl --data-binary @-` streams stdin, so it sends Transfer-Encoding:
        chunked with no Content-Length - reading Content-Length alone yields b"".
        BaseHTTPRequestHandler does not de-chunk, so both framings are handled here.

        At most `limit` + 1 bytes are ever RETAINED, and the cap is applied while
        reading rather than after (v0.14.0). The old shape trusted Content-Length: a
        header claiming 900 MB made this method try to buffer 900 MB and block, and the
        hook that sent it never got an answer - measured against a live crabd on
        2026-08-26. The one extra byte is what lets each endpoint's own
        `len(raw) > ITS_CAP` test still see that the body was oversized.

        Never raises. A truncated, timed-out or reset read returns what arrived, so the
        caller still answers - a client that lies about its length gets a pass-through,
        not a dropped connection with no response on it.
        """
        chunked = (self.headers.get("Transfer-Encoding") or "").lower().strip() == "chunked"
        keep = limit + 1
        body = bytearray()
        try:
            if chunked:
                while len(body) < keep:
                    line = self.rfile.readline(64).strip()
                    if not line:
                        break
                    try:
                        size = int(line.split(b";", 1)[0], 16)
                    except ValueError:
                        break
                    if size == 0:
                        self.rfile.readline(4)  # trailing CRLF after the last chunk
                        break
                    if size < 0 or size > keep:
                        size = keep            # a chunk header is not a licence to allocate
                    body += self.rfile.read(size)
                    self.rfile.read(2)  # CRLF between chunks
                if len(body) >= keep:
                    self.close_connection = True   # mid-stream; the framing is gone
                return bytes(body)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                return b""
            body += self.rfile.read(min(length, keep))
            self._drain(length - len(body))
        except (OSError, ValueError):
            # TimeoutError and ConnectionResetError are both OSError. Whatever arrived
            # before the socket gave up is what the caller gets to parse.
            self.close_connection = True
        return bytes(body)

    def _drain(self, remaining: int) -> None:
        """Discard the tail of an over-cap body so keep-alive framing survives - but
        only up to BODY_DRAIN_MAX. Past that the connection is closed instead: draining
        an unbounded body to be polite is the same cost the cap exists to refuse."""
        if remaining <= 0:
            return
        if remaining > BODY_DRAIN_MAX:
            self.close_connection = True
            return
        while remaining > 0:
            block = self.rfile.read(min(remaining, 65536))
            if not block:
                break
            remaining -= len(block)

    # State-changing POST endpoints (SEC-1). Loopback binding was crabd's whole
    # access-control story; a web page the operator merely VISITS crosses it with a
    # CORS-simple POST whose side effect fires even though the browser cannot read the
    # reply. So a real web page (http/https Origin) is refused here. Absent / null /
    # non-web Origins are allowed - the widget's opaque iCUE origin, curl-fed ingest
    # hooks, the CLI's own Stop/PermissionRequest HTTP hooks and every local tool land
    # there.
    #
    # SINCE v0.16.0 THIS SET NO LONGER DECIDES WHETHER THE GATE APPLIES - do_GET runs the
    # same gate on every read (SEC-4) and do_POST runs it on every path including the
    # unknown ones. The set is kept because it is the readable inventory of what actually
    # CHANGES STATE, which is the fact the security docs and the audit reason about.
    MUTATING_PATHS = frozenset((
        "/v1/hook", "/v1/hook/stop", "/v1/hook/permission",
        "/v1/statusline", "/v1/metrics", "/v1/logs",
        "/v1/action", "/v1/config",
        "/v1/panel-log",
    ))

    @staticmethod
    def _is_web_origin(origin) -> bool:
        """True when Origin marks a real http(s) browser page. False for absent, "null",
        and any non-web scheme.

        The PURE half of the gate - it says what KIND of caller this is, not whether it
        is allowed. `_is_cross_site` below is the gate; this is what it asks first.

        WHAT WAS ACTUALLY MEASURED, because this docstring used to assert the opposite.
        The iCUE build reports `file://`, not `null` - originsSeen on 2026-09-02 after
        the 0.27.0 import, `origin: file://` with an AppleWebKit/537.36 UA (ORIGIN-b in
        docs/BACKLOG.md). QtWebEngine did NOT collapse its file page to an opaque origin.
        Both land here as "not a web origin", so this predicate never had to tell them
        apart - but the two are not interchangeable one layer up, where
        _preflight_headers hands the panel header to `file://` and refuses it to `null`.

        `null` is still not a web origin, and DO NOT "fix" that by rejecting it. A
        QtWebEngine build that does collapse to an opaque origin has no other value it
        could send, and reads from it cost nothing to allow. A sandboxed-iframe attacker
        can FORGE Origin:null, so this predicate cannot separate the two and no amount of
        tightening here will; the forged-null WRITE is closed a layer up, by PANEL_HEADER
        and the preflight rule in _preflight_headers.
        """
        if not isinstance(origin, str):
            return False
        o = origin.strip().lower()
        if not o or o == "null":
            return False
        return o.startswith("http://") or o.startswith("https://")

    #: The host names this crabd answers to. NOT a convenience list - it is the whole
    #: DNS-rebinding gate. `[::1]` carries its brackets because that is how a Host
    #: header spells an IPv6 literal.
    PANEL_HOSTS = frozenset(("localhost", "127.0.0.1", "[::1]"))

    @staticmethod
    def _host_parts(host_header: str) -> tuple[str, str | None] | None:
        """`Host` split into (host, port-or-None), lowercased. None = unparseable.

        Unparseable is REFUSED rather than ignored: a Host crabd cannot read is a Host
        it cannot check, and the whole point of this gate is that the header is the only
        thing distinguishing a rebound page from a local one.
        """
        value = host_header.strip().lower()
        if not value:
            return None
        if value.startswith("["):                   # an IPv6 literal, [::1] or [::1]:9999
            end = value.find("]")
            if end < 0:
                return None
            host, rest = value[:end + 1], value[end + 1:]
            if not rest:
                return (host, None)
            return (host, rest[1:]) if rest.startswith(":") else None
        if value.count(":") > 1:
            # A bare IPv6 literal with no brackets. Not a legal Host, and guessing which
            # colon is the port separator is exactly how a parser is walked past.
            return None
        if ":" in value:
            host, _, port = value.partition(":")
            return (host, port)
        return (value, None)

    @classmethod
    def _is_foreign_host(cls, host_header, port: int) -> bool:
        """True when the caller believes it is talking to somebody else. Refused 403.

        DNS REBINDING, which no other gate here can see. The operator visits
        http://evil.example:9999; the name has a short TTL and re-resolves to 127.0.0.1,
        so the browser now believes crabd IS evil.example and the page is SAME-ORIGIN
        with it. A same-origin GET carries no Origin header at all - which is the
        ordinary shape of a hook, a curl and a plain navigation - so the origin
        allowlist waves it through, and the GET is the request that reads /v1/state.

        `Host` is the one header taken from the URL the page thinks it is addressing
        rather than from the socket, so a rebound page still says evil.example on every
        request. An ABSENT Host is allowed: HTTP/1.0 has none, several diagnostics in
        this repo hand-roll requests without one, and absent is not a claim about
        anything.
        """
        if host_header is None:
            return False
        parts = cls._host_parts(host_header)
        if parts is None:
            return True
        host, host_port = parts
        if host not in cls.PANEL_HOSTS:
            return True
        # The port is part of the claim: a page served on another local port and rebound
        # would otherwise pass on the name alone.
        return host_port is not None and host_port != str(port)

    @staticmethod
    def _panel_origins(port: int) -> frozenset:
        """The three spellings of "this crabd", lowercase - the allowlist.

        This is the case the old _is_web_origin docstring anticipated in its last
        sentence: a panel build confirmed to send a stable non-"null" origin, allowlisted
        to that exact value. crabd serves the panel itself as of v0.31.0, so the panel's
        origin IS an http one, and "refuse every http origin" would refuse the product.

        Three, because a browser sends back whatever the operator typed and `localhost`
        resolves to ::1 first on a dual-stack machine. The BOUND port, never the
        configured one: a second instance on CRABD_PORT must allowlist itself, not the
        production daemon it is running beside.
        """
        return frozenset((f"http://localhost:{port}",
                          f"http://127.0.0.1:{port}",
                          f"http://[::1]:{port}"))

    @classmethod
    def _is_cross_site(cls, origin, port: int) -> bool:
        """The gate: a web page that is NOT this crabd's own panel. Refused 403.

        EXACT match against the allowlist, on the whole serialised origin. Not a prefix
        (`http://localhost:9999.evil.example` starts with the right string), not a host
        test that ignores the port (any dev server, notebook or other local tool the
        operator has open on 127.0.0.1 is a different origin), and not a scheme-blind one
        (nothing serves this panel over TLS, so an `https://localhost:9999` claiming to
        be it is a page that is not).
        """
        if not cls._is_web_origin(origin):
            return False
        return origin.strip().lower() not in cls._panel_origins(port)

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        origin = self.headers.get("Origin")
        self._record_origin(origin)
        if self._is_foreign_host(self.headers.get("Host"), self.server.server_port):
            # Same order and the same drain as the two gates below it.
            self._acao = None
            self._read_body()
            self._send(403, HOST_NOT_ALLOWED)
            return
        if self._is_cross_site(origin, self.server.server_port):
            # Drain the body first so keep-alive framing survives, then refuse. Drained
            # on EVERY path, not just the mutating ones: a refused POST to an unknown
            # path still arrived with a body, and leaving it in the stream desynchronises
            # the next request on the connection.
            self._acao = None
            self._read_body()
            self._send(403, CROSS_SITE_REFUSED)
            return
        # Reflect a PRESENT allowed origin (the panel's own, the widget's "null") so its
        # cors-mode fetch can read the status - the panel rolls back its optimistic tap
        # on an unreadable reply. Never the wildcard (SEC-1/SEC-4). An absent Origin is a
        # non-browser client that needs no ACAO at all.
        self._acao = origin if origin else None
        # The header gate (v0.31.0), AFTER the origin gate and before any routing, so it
        # covers every path including the unknown ones. Order is deliberate: a cross-site
        # page is told it is cross-site - which it already knew - and never learns there
        # is a header to look for. Same drain-then-refuse as above, and _acao is left as
        # the origin gate computed it, because a same-origin panel has to be able to READ
        # this 403 (an unreadable reply is a CORS error, not a status, and the panel
        # cannot tell the operator what happened).
        if not (self.headers.get(PANEL_HEADER) or "").strip():
            self._read_body()
            self._send(403, PANEL_HEADER_REQUIRED)
            return
        if path == "/v1/hook":
            # Answer first, parse after: a hook must never hold Claude Code open.
            raw = self._read_body()
            self._send(204, None)
            payload = self._json_body(raw)
            if isinstance(payload, dict):
                # The 204 is already on the wire, so an exception here can only reach
                # the socket layer as a traceback on a connection the client has
                # finished with. Swallowing it is what keeps a malformed hook body from
                # being visible to the operator at all (v0.14.0).
                try:
                    self.builder.record_hook(payload)
                except Exception:   # noqa: BLE001
                    pass
        elif path == "/v1/hook/stop":
            self._do_hook_stop(self._read_body())
        elif path == "/v1/hook/permission":
            self._do_hook_permission(self._read_body())
        elif path == "/v1/statusline":
            self._do_statusline(self._read_body())
        elif path in ("/v1/metrics", "/v1/logs"):
            self._do_otlp(path, self._read_body())
        elif path == "/v1/action":
            self._do_action(self._read_body())
        elif path == "/v1/config":
            self._do_config(self._read_body())
        elif path == "/v1/panel-log":
            self._do_panel_log(self._read_body())
        else:
            # Drain before answering, exactly like the 403 above (v0.17.0). An unknown
            # path is still a POST that ARRIVED WITH A BODY, and a body left unread sits
            # in the socket buffer where the keep-alive connection's next request-line
            # parse will find it - the client's following request is then answered as
            # garbage, or the connection dies. The test client's connect-retry hid this;
            # framing is not something a retry may be relied on to paper over.
            self._read_body()
            self._send(404, NOT_FOUND)

    @staticmethod
    def _json_body(raw: bytes):
        try:
            return json.loads(raw.decode("utf-8", errors="replace")) if raw else None
        except ValueError:
            return None

    # ------------------------------------------------------------ v0.12.0 endpoints

    def _do_statusline(self, raw: bytes) -> None:
        """POST /v1/statusline - the official session document, 204 fire-and-forget.

        Answered BEFORE the parse, like /v1/hook and for a sharper version of the same
        reason: the status line command runs on a 300 ms debounce and an in-flight one
        is CANCELLED when the next update arrives, so anything crabd makes it wait for
        is latency in front of the operator's own status bar.
        """
        self._send(204, None)
        reader = getattr(self.builder, "statusline", None)
        if reader is None or len(raw) > STATUSLINE_MAX_BODY:
            return
        payload = self._json_body(raw)
        if isinstance(payload, dict):
            try:
                reader.ingest(payload, time.time())
            except Exception:   # noqa: BLE001 - see _do_otlp; same reason, same floor
                # MEASURED 2026-08-26 on the live wiring: `resets_at: 1e30` in a real
                # document put an OverflowError traceback on the socket after the 204.
                # _parse_ts now refuses that value, and this is the floor under every
                # OTHER field of a document schema crabd does not own.
                pass

    def _do_panel_log(self, raw: bytes) -> None:
        """POST /v1/panel-log -> 204 (contract v0.24.0).

        Validated BEFORE the answer, unlike /v1/hook, /v1/statusline and the OTLP pair.
        Those three are fire-and-forget because a producer that must not be made to wait
        is on the other end; here the caller is the widget being DEBUGGED, and a 204 for
        a body crabd silently discarded is exactly the failure the channel exists to
        remove. Cheap enough to do inline: a slice, up to 50 isinstance calls, and a list
        extend under a lock that touches no IO.

        What lands in the ring is never looked at again by this daemon - see PanelLog.
        """
        body = self._json_body(raw)
        lines = _panel_log_lines(body.get("lines")) if isinstance(body, dict) else None
        if lines is None:
            self._send(400, PANEL_LOG_BAD_BODY)
            return
        self.builder.panel_log.append(lines, time.time())
        self._send(204, None)

    def _do_otlp(self, path: str, raw: bytes) -> None:
        """POST /v1/metrics + /v1/logs - OTLP http/json, ALWAYS 204, never an error.

        A telemetry receiver that 4xxs teaches the producer's exporter to retry, back
        off, and log - and Claude Code's exporter is inside the session the operator is
        working in. So the contract is 2xx-and-drop for every input: malformed JSON, a
        protobuf body posted at the JSON endpoint, an oversized batch, a signal crabd
        does not consume. 204 is a 2xx; every OTLP HTTP exporter treats the 200-299
        range as success.
        """
        self._send(204, None)
        receiver = getattr(self.builder, "otlp", None)
        if receiver is None or len(raw) > OTLP_MAX_BODY:
            return
        payload = self._json_body(raw)
        if not isinstance(payload, dict):
            return
        now = time.time()
        try:
            if path == "/v1/metrics":
                receiver.ingest_metrics(payload, now)
            else:
                receiver.ingest_logs(payload, now)
        except Exception:   # noqa: BLE001 - a telemetry batch must never reach a log
            pass

    def _do_hook_stop(self, raw: bytes) -> None:
        """POST /v1/hook/stop - the Stop hook as a type-http handler.

        Two jobs in one request, and the ORDER matters. First it is a Stop hook like any
        other, so it feeds the state machine (this endpoint replaces /v1/hook for Stop -
        skip the record and every session would sit on `working` forever). Then it
        drains the continue queue and answers.

        Answered synchronously and immediately: the contract's 2 s budget is a ceiling
        this is nowhere near, because everything it does is a dict lookup under a lock.
        Nothing on this path waits on the builder, the filesystem, or a subprocess.

        THE ORDER IS PEEK -> SEND -> CONSUME (CRB-F5, v0.16.0). It used to drain the
        queue and then send, so a send that failed - the hook's connection reset, the CLI
        gone, the socket timed out - destroyed the operator's queued prompt on its way to
        nobody. A queued continue is a tap they made and can see on the card; losing it
        silently is worse than delivering it to the next Stop. Consuming AFTER the answer
        makes a failed send a no-op: _send raises, the two lines below never run, and the
        prompt is still there for the next Stop hook.
        """
        try:
            prompt, session_id = self._peek_stop(raw)
        except Exception:   # noqa: BLE001
            # A Stop hook that gets no answer is a session that hangs waiting for one.
            # Whatever went wrong in here, the pass-through is always a correct answer -
            # it is exactly what a crabd that is DOWN produces (v0.14.0).
            prompt, session_id = None, None
        if prompt is None:
            self._send_stop_answer(json.dumps(HOOK_PASS_THROUGH).encode())
            return
        # Shape pinned by STOP_CONTINUE_HOOK_EVENT - see the constant block for the four
        # facts measured in the shipped CLI, including the one that matters most: this
        # shape forces the next turn by the SAME code path decision:"block" does.
        if not self._send_stop_answer(json.dumps(stop_continue_body(prompt)).encode()):
            return
        # Past the send without failing = the answer is on the socket. Only now is the
        # prompt spent, and only now is it true to say it was sent. Both lines are
        # deliberately after the send and deliberately in this order.
        queue = getattr(self.builder, "continues", None)
        if queue is not None:
            # drain_if, not drain (CD-30): spend the prompt that was actually sent. A
            # replacement queued between the peek and here is a different tap and is
            # kept for the next Stop, rather than deleted undelivered.
            queue.drain_if(session_id, prompt, time.time())
        self.builder.hooks.note_external(session_id, "continue sent: " + prompt)

    def _send_stop_answer(self, body: bytes) -> bool:
        """Send the Stop hook's answer. -> True when it reached the socket.

        A DELIBERATE catch, added v0.17.0. CRB-F5 moved the queue consume to after the
        send precisely so a failed send would leave the prompt intact - which it does,
        but the exception then walked out of the handler into socketserver's
        handle_error, printing a full traceback for the most ordinary transport event
        there is: the CLI's hook client, whose own budget is ~2 s, hangs up before crabd
        answers. A traceback is how crabd reports something it did not expect, and this
        is expected. One honest line instead, and the caller stops - the two lines it
        would have run next are the consume and the "continue sent" claim, and neither is
        true of an answer nobody received.

        OSError only (BrokenPipeError / ConnectionResetError / TimeoutError are all
        OSError): a socket write failing is the case being made ordinary. Anything else
        raised from in here is still a genuine surprise and still deserves its traceback.
        """
        try:
            self._send(200, body)
        except OSError as exc:      # noqa: BLE001 - narrowed on purpose, see the docstring
            self.close_connection = True
            print(f"crabd: stop-hook answer undelivered ({type(exc).__name__}); "
                  f"any queued continue is kept for the next Stop", file=sys.stderr)
            return False
        return True

    def _peek_stop(self, raw: bytes) -> tuple[str | None, str | None]:
        """The two jobs of the Stop hook, in order: feed the state machine, then LOOK AT
        (never take - see _do_hook_stop) anything queued for this session.

        A session crabd has NEVER SEEN is the ordinary case here, not an error - it is
        every session that started before this crabd did, and every session on a machine
        where the hooks block was only just wired. record() creates the row, the queue
        has nothing for it, and the answer is the pass-through.
        """
        payload = self._json_body(raw)
        if not isinstance(payload, dict):
            return None, None
        # record_hook, not hooks.record: a Stop is one of PERMISSION_STALE_EVENTS, so it
        # also retires a permission hold still parked for this session (v0.19.0).
        self.builder.record_hook(payload)
        session_id = _session_id(payload)
        queue = getattr(self.builder, "continues", None)
        if queue is None or not session_id:
            return None, session_id
        now = time.time()
        prompt = queue.peek(session_id, now)
        if prompt is None:
            # peek IGNORES an expired entry; drain DELETES it. Purging it here keeps the
            # behaviour the old drain-first shape had: an item this Stop already ruled
            # too old must not sit there for a later Stop to rule on again.
            queue.drain(session_id, now)
        return prompt, session_id

    def _do_hook_permission(self, raw: bytes) -> None:
        """POST /v1/hook/permission - the panel-approval long poll.

        Every early exit in here lands on the SAME answer: HOOK_PASS_THROUGH, an empty
        object, which is the documented no-op that lets the terminal dialog appear
        exactly as it does today. Disabled in config, a malformed body, no session id,
        a saturated broker, nobody tapping - all of them are "SideCrab has nothing to
        say about this", and none of them is an allow. There is deliberately no branch
        in this method that can produce `behavior: allow` without a tap having landed on
        /v1/action first.
        """
        try:
            decision, session_id, tool = self._await_permission(raw)
        except Exception:   # noqa: BLE001
            # Structurally safe to swallow: the ONLY value that can produce an allow is
            # a decision returned by the broker, so an error path can never widen to one
            # (v0.14.0). Everything else lands on the pass-through below.
            decision = None
        if decision is None:
            self._send(200, json.dumps(HOOK_PASS_THROUGH).encode())
            return
        if decision == PERMISSION_BEHAVIOR_ALLOW:
            inner = {"behavior": PERMISSION_BEHAVIOR_ALLOW}
        else:
            inner = {"behavior": PERMISSION_BEHAVIOR_DENY,
                     "message": PERMISSION_DENY_MESSAGE}
        self._send(200, json.dumps({
            "hookSpecificOutput": {"hookEventName": PERMISSION_HOOK_EVENT,
                                   "decision": inner}}).encode())

    def _await_permission(self, raw: bytes) -> tuple[str | None, str | None, str | None]:
        """-> (decision, session id, tool). A None decision is the pass-through, and
        every early exit in here produces one.

        The v0.14.0 addition is the `serving` gate, and it is the same scoping rule ack,
        queue-continue and the OTLP event route already use. A PermissionRequest naming
        a session crabd is NOT serving cannot be answered from the panel - the widget
        renders `pendingPermission` off the served rows and there is no row - so holding
        it would park a thread and consume one of PERMISSION_MAX_PENDING for 55 s while
        the operator watches a terminal dialog that has not appeared yet. Passing it
        through hands the dialog over immediately, which is the behaviour of a SideCrab
        that was never installed.
        """
        payload = self._json_body(raw)
        broker = getattr(self.builder, "permissions", None)
        session_id = _session_id(payload)
        enabled = self.builder.config.panel_approvals(time.time())
        if broker is None or not enabled or not session_id:
            return None, session_id, None
        if not self.builder.serving(session_id):
            return None, session_id, None
        tool = _trim(payload.get("tool_name") or payload.get("toolName"),
                     PERMISSION_TOOL_MAX) or "a tool"
        summary = broker.summarize(payload.get("tool_input") or payload.get("toolInput"))
        now = time.time()
        entry = broker.register(session_id, tool, summary, now)
        if entry is None:
            return None, session_id, tool
        # v0.20.0. The hold is a session waiting on the operator, so it moves the STATE
        # MACHINE too - the panel renders Approve / Deny off the needs_input sheet, and
        # before this the card carrying the pendingPermission could be the one card not
        # offering it. The join lives here for the same reason record_hook's does: the
        # broker and HookTracker deliberately do not know each other.
        self.builder.hooks.note_permission(session_id, PERMISSION_QUESTION % tool, now)
        try:
            # Tool name only - the summary is panel content and never reaches history.
            self.builder.note_session_event(session_id,
                                            f"{PERMISSION_EVENT_REQUESTED}: {tool}")
            decision = broker.wait(entry, PERMISSION_POLL_SEC)
        except BaseException:
            # v0.19.0. register() has ALREADY put a panel-visible pendingPermission on
            # the card, and the caller swallows whatever comes out of here into the
            # pass-through - so without this the Approve / Deny buttons would sit on that
            # card FOREVER (nothing else removes an entry but decide, release and stale),
            # long past the hold that was supposed to bound them. The release is the same
            # one the timeout path takes and can no more produce an allow. A tap that had
            # just landed is dropped with it, which is the correct trade on a path that is
            # already crashing: an un-clearable panel row outlives the request, a lost tap
            # does not.
            broker.release(session_id, entry)
            # A-01: only stand the card down if THIS was still the live hold. If a newer
            # request replaced it and is still parked, that one owns the card now.
            if not broker.has_pending(session_id):
                self.builder.hooks.clear_permission(session_id, time.time())
            raise
        if decision is None:
            # AUDIT F3 (v0.17.0): release() is the authority on what happened, not wait().
            # A tap can land between the wait expiring and the entry being dropped, and
            # only release() reads the decision under the lock that drops it - so it, not
            # the value read a moment earlier, decides whether this was a pass-through.
            decision = broker.release(session_id, entry)
        # The hold is over on EVERY path that reaches here - tap, timeout, or a hold
        # retired by stale() - and the card must not go on advertising a decision that is
        # no longer open. A needs_input some OTHER signal owns is left standing; that is
        # clear_permission's gate, not a check here.
        #
        # A-01 (v0.26.0): but a REPLACED hold must not stand down a card whose replacing
        # hold is still parked. register() is newest-wins, so two PermissionRequests for
        # one session (parallel tool calls) leave B holding the card while A's thread wakes
        # here as a pass-through; A's release() is a no-op (B is the current entry), and
        # calling clear_permission unconditionally would _stand_down a card with B's live
        # pendingPermission still on it - serving Approve/Deny on a row reading `working`,
        # the exact defect the needs_input sheet was written to close. has_pending() is the
        # join that knows both sides: skip the stand-down while a live hold remains, and let
        # B's own eventual clear retire the card.
        if not broker.has_pending(session_id):
            self.builder.hooks.clear_permission(session_id, time.time())
        if decision is None:
            # AUDIT F7 (v0.17.0): create=True, matching _do_decide. Without it a session
            # that aged out of the served set during the 55 s hold LOSES this line, and
            # the operator can no longer tell "I did not tap in time" from "the panel
            # never saw it" - the exact distinction PERMISSION_EVENT_TIMEOUT exists for.
            self.builder.hooks.note_external(
                session_id, f"{PERMISSION_EVENT_TIMEOUT}: {tool}", create=True)
        return decision, session_id, tool

    def _do_action(self, raw: bytes) -> None:
        body = self._json_body(raw)
        if not isinstance(body, dict):
            self._send(400, b'{"error":"malformed request"}')
            return
        action = body.get("action")
        if action == "ack-all":
            # Deliberately BEFORE the sessionId check: ack-all is a whole-panel gesture
            # (the widget's crab tap) and carries no session. 204 even when nothing was
            # waiting - the contract makes it idempotent so the tap is never an error.
            self.builder.ack_all()
            self._send(204, None)
            return
        if action == "quiet":
            # Beside ack-all and BEFORE the sessionId check for the same reason: the
            # quiet override is a whole-panel gesture and carries no session.
            self._do_quiet(body)
            return
        session_id = body.get("sessionId") or body.get("session_id")
        if (not isinstance(session_id, str) or not session_id
                or action not in ("ack", "reply", "queue-continue", "decide")):
            self._send(400, b'{"error":"malformed request"}')
            return
        if action == "queue-continue":
            self._do_queue_continue(session_id, body.get("prompt"))
            return
        if action == "decide":
            self._do_decide(session_id, body.get("decision"),
                            body.get("token"), body.get("requestId"))
            return
        if action == "reply":
            # 501 is the honest answer, not a stub. The 2026-08-26 spike found no way to
            # deliver text into a LIVE session: the cross-session bus is an undocumented
            # named pipe whose messages are queued for a session's next TOOL ROUND (a
            # session blocked on a permission prompt never reaches one), no `claude` CLI
            # flag reaches a running process (--resume/--continue fork), and window
            # targeting cannot tell two sessions apart. config allowReply gates the
            # feature; it does not conjure the mechanism. See docs/STATE-CONTRACT.md.
            self._send(501, b'{"error":"reply not supported"}')
            return
        if not self.builder.ack(session_id):
            self._send(404, b'{"error":"unknown session"}')
            return
        self._send(204, None)

    def _do_quiet(self, body: dict) -> None:
        """POST /v1/action {"action":"quiet"} -> 204 (contract v0.23.0).

        A FIXED VOCABULARY, and that is the security posture as much as the UX one: this
        endpoint writes config.json over the same unauthenticated loopback port every
        other action rides, so the only things it can write are `on`, `off` and a minute
        count inside a bounded range. There is no free text and no arbitrary timestamp -
        an attacker who reaches it can dim a panel for at most eight hours, and the SEC-1
        Origin gate in do_POST has already refused any visited http(s) page.

        `auto` IGNORES a `minutes` it was sent rather than 400ing on it. Clearing is the
        gesture that must never fail: it is what the operator taps when the panel is
        behaving in a way they did not intend, and "your cancel was malformed" is the
        worst possible answer to that. It is also what makes auto unconditionally
        idempotent - two taps, two 204s, the same file.
        """
        mode = body.get("mode")
        if mode not in ("on", "off", "auto"):
            self._send(400, b'{"error":"mode must be on, off or auto"}')
            return
        if mode == "auto":
            if not self.builder.config.set_quiet_override(None, 0.0):
                self._send(500, b'{"error":"could not write config"}')
                return
            self._send(204, None)
            return
        minutes = body.get("minutes")
        # bool first - True is 1, and `"minutes": true` is a typo, not fifteen minutes
        # (it would fail the range check anyway; this is the same belt-and-braces every
        # other validator in this file wears). A float is refused rather than truncated:
        # the contract says an integer, and 15.9 has no meaning the operator intended.
        if (isinstance(minutes, bool) or not isinstance(minutes, int)
                or not (QUIET_OVERRIDE_MIN_MINUTES <= minutes
                        <= QUIET_OVERRIDE_MAX_MINUTES)):
            self._send(400, b'{"error":"minutes must be an integer 15..480"}')
            return
        if not self.builder.config.set_quiet_override(mode,
                                                      time.time() + minutes * 60):
            self._send(500, b'{"error":"could not write config"}')
            return
        self._send(204, None)

    def _do_queue_continue(self, session_id: str, prompt) -> None:
        """POST /v1/action {"action":"queue-continue"} -> 204 (contract v0.12.0 §3).

        `prompt` is checked against the whitelist, not merely length-capped. The queued
        string is delivered to the model as an instruction by the Stop hook, and every
        process on this machine can reach a loopback port with no auth - so the set of
        things that can be said through SideCrab is exactly the set the operator put on
        the widget's sheet, and nothing that arrives here can widen it.

        Unknown session is 404 like `ack`, and for the same reason: queueing against an
        id crabd is not serving would sit in the queue until it expired, having told the
        widget it was accepted.
        """
        queue = getattr(self.builder, "continues", None)
        if queue is None:
            self._send(501, b'{"error":"continue not supported"}')
            return
        # SEC-3: the enable gate decide/reply already have. Default ON (see
        # UserConfig.allow_continue); an operator can turn tap-to-continue off in the
        # config FILE. 403, not 501: the feature is implemented, refused by config - the
        # widget renders any non-2xx here as "not available" and does not latch.
        now = time.time()
        if not self.builder.config.allow_continue(now):
            self._send(403, b'{"error":"tap-to-continue is disabled"}')
            return
        allowed = self.builder.config.continue_prompts(now)
        if not isinstance(prompt, str) or prompt not in allowed:
            self._send(400, b'{"error":"prompt must be one of the configured continue '
                            b'prompts"}')
            return
        if not self.builder.serving(session_id):
            self._send(404, b'{"error":"unknown session"}')
            return
        # GHOST-a (v0.28.1): a dead session's card reads `working` for up to
        # IDLE_AFTER_SEC (the state-None fallback after a restart), and a tap used to
        # queue a prompt no Stop hook would ever drain - measured live 2026-09-01,
        # three taps into a session the app had killed six minutes earlier. Queue only
        # when THIS process holds hook-grounded state for the row, or the transcript
        # moved recently; both absent means nobody is listening. The widget already
        # renders any non-2xx here as "not available" and does not latch.
        if self.builder.hooks.live_state(session_id) is None:
            age = self.builder.transcript_age(session_id, now)
            if age is None or age > IDLE_AFTER_SEC:
                self._send(409, b'{"error":"session looks gone - no live hook state '
                                b'and its transcript has been quiet"}')
                return
        queue.queue(session_id, prompt, time.time())
        self.builder.hooks.note_external(session_id, "continue queued: " + prompt,
                                         create=True)
        self._send(204, None)

    def _do_decide(self, session_id: str, decision, token=None, request_id=None) -> None:
        """POST /v1/action {"action":"decide"} -> 204 (contract v0.12.0 §4; v0.29.0 gate).

        404 when nothing is pending, and that is the important answer rather than a
        courtesy 204: a tap that lands after the 55 s hold expired must NOT read as an
        approval the widget can show, because by then the terminal dialog owns the
        decision and the operator is about to answer it a second time.

        v0.29.0 - the order of the gates is the security argument:
          1. decision shape (400) - malformed is malformed, no secret consulted;
          2. the pairing code (403 missing/rejected, 429 locked, 503 when crabd has no
             PanelToken at all - NEVER fall open to the pre-0.29.0 behaviour);
          3. requestId (400 absent, 409 stale) - checked inside the broker's lock;
          4. only then the decision is applied.
        A caller that can forge `Origin: null` (SEC-a) stops at gate 2.
        """
        broker = getattr(self.builder, "permissions", None)
        if broker is None:
            self._send(501, b'{"error":"panel approvals not supported"}')
            return
        if decision not in (PERMISSION_BEHAVIOR_ALLOW, PERMISSION_BEHAVIOR_DENY):
            self._send(400, b'{"error":"decision must be allow or deny"}')
            return
        gate = getattr(self.builder, "panel_token", None)
        if gate is None:
            self._send(503, b'{"error":"panel pairing unavailable"}')
            return
        verdict = gate.verify(token, time.time())
        if verdict == "locked":
            self._send(429, b'{"error":"pairing code locked after repeated rejects - wait a minute"}')
            return
        if verdict == "missing":
            self._send(403, b'{"error":"pairing code required"}')
            return
        if verdict != "ok":
            self._send(403, b'{"error":"pairing code rejected"}')
            return
        if not isinstance(request_id, str) or not request_id:
            # A tap that arrives after the hold expired is the contract's 404, not a
            # 400 about a field the widget had nothing to fill in.
            if broker.pending(session_id) is None:
                self._send(404, b'{"error":"no permission request pending"}')
            else:
                self._send(400, b'{"error":"requestId required"}')
            return
        try:
            tool = broker.decide(session_id, decision, time.time(), request_id)
        except PermissionRequestMismatch:
            self._send(409, b'{"error":"stale permission request"}')
            return
        if tool is None:
            self._send(404, b'{"error":"no permission request pending"}')
            return
        label = (PERMISSION_EVENT_ALLOW if decision == PERMISSION_BEHAVIOR_ALLOW
                 else PERMISSION_EVENT_DENY)
        # Contract: every decision is a history line. note_external persists it, so the
        # record of what was approved from the panel outlives this crabd.
        self.builder.hooks.note_external(session_id, f"{label}: {tool}", create=True)
        self._send(204, None)

    # panelApprovals is DELIBERATELY NOT here (QA-Audit 2026-08-27, SEC-2). It is a SECURITY
    # flag - it decides whether an on-glass tap can allow a real tool call. A flag that gates
    # security must never be settable over the unauthenticated loopback API: any local process,
    # or any web page the operator visits (the same POST is a CORS-simple request), could arm it
    # and then poll-and-pounce a pending permission. It is set only via the config FILE, by the
    # installer's -WithApprovals. `allowReply` was already excluded for the same reason.
    #
    # `quietOverride` is DELIBERATELY NOT here either (v0.23.0), and for a different reason
    # than panelApprovals: it IS panel-writable, just not through THIS endpoint. /v1/action's
    # quiet branch is its only writer, so the bounded vocabulary there (on/off/auto, 15..480
    # minutes, a `until` crabd computes from its own clock) is the whole set of values that
    # can ever reach the file. A /v1/config body naming it is an unknown key - 400, nothing
    # written - which is what stops a client minting its own `until` a decade out.
    # panelApprovals is deliberately NOT writable via /v1/config (SEC-2, 2026-08-27) and
    # must never be added - flipping the approvals security flag over loopback is exactly
    # the CSRF the origin gate exists to bound (SEC-c, 2026-08-28).
    CONFIG_WRITABLE = ("quietHours", "toast", "digest", "budget")

    def _do_config(self, raw: bytes) -> None:
        """POST /v1/config - quietHours, toast, digest and budget, and NOTHING else.

        The whitelist is exact rather than "ignore what you don't know": `allowReply`
        gates a feature, and a widget (or anything else that can reach localhost) must
        not be able to flip it by naming it in this body. Any key alone is valid, so is
        any combination of them; an unknown key anywhere is 400. An invalid body is
        rejected WHOLE - the file is never half-written from a request that failed
        validation, which is why every key is validated before the single write below.
        """
        body = self._json_body(raw)
        if (not isinstance(body, dict) or not body
                or not set(body) <= set(self.CONFIG_WRITABLE)):
            self._send(400, b'{"error":"quietHours, toast, digest and budget are the '
                            b'only writable keys"}')
            return
        values = {}
        if "quietHours" in body:
            ok, normalized = self._validate_quiet_hours(body["quietHours"])
            if not ok:
                self._send(400, b'{"error":"quietHours must be {start,end} as HH:MM, or null"}')
                return
            values["quietHours"] = normalized
        if "toast" in body:
            ok, normalized = self._validate_toast(body["toast"])
            if not ok:
                self._send(400, b'{"error":"toast must be {thresholdSec 30..3600, enabled '
                                b'bool} with an optional approvalThresholdSec 5..3600"}')
                return
            values["toast"] = normalized
        if "digest" in body:
            ok, normalized = self._validate_digest(body["digest"])
            if not ok:
                self._send(400, b'{"error":"digest must be {enabled bool, time HH:MM}"}')
                return
            values["digest"] = normalized
        if "budget" in body:
            ok, normalized = self._validate_budget(body["budget"])
            if not ok:
                self._send(400, b'{"error":"budget must be {dailyOutputTokens '
                                b'100000..100000000}, or null"}')
                return
            values["budget"] = normalized
        # panelApprovals intentionally has no branch here - it is not in CONFIG_WRITABLE
        # (SEC-2). A body naming it is already rejected 400 by the whitelist check above.
        if not self.builder.config.set_keys(values):
            self._send(500, b'{"error":"could not write config"}')
            return
        self._send(204, None)

    @staticmethod
    def _validate_quiet_hours(value):
        """-> (ok, normalized). None clears the window."""
        if value is None:
            return True, None
        if not isinstance(value, dict) or set(value) != {"start", "end"}:
            return False, None
        start, end = _parse_hhmm(value.get("start")), _parse_hhmm(value.get("end"))
        if start is None or end is None:
            return False, None
        # Stored canonically ("7:5" -> "07:05") so the file, the served `quiet` block
        # and the widget's own fields cannot disagree on formatting.
        return True, {"start": "%02d:%02d" % divmod(start, 60),
                      "end": "%02d:%02d" % divmod(end, 60)}

    @staticmethod
    def _validate_toast(value):
        """-> (ok, normalized). thresholdSec and enabled are BOTH REQUIRED (contract): a
        partial block would leave the notifier reading one setting from the body and one
        from a default, and the operator could not tell which. No null-clear either -
        "no toast" is {"enabled": false}, which still says what the threshold would be.

        `approvalThresholdSec` (v0.16.0) is the one OPTIONAL member. It is the notifier's
        pending-PERMISSION threshold, it has never been settable over HTTP, and the
        widget's settings sheet does not know it exists - which is exactly the defect
        this fixes: the block used to be required to be exactly {thresholdSec, enabled},
        so every panel save wrote a block without the key and the operator's hand-edited
        value vanished into the notifier's 20 s default with no message. Accepting it
        here is only half the fix; UserConfig.PRESERVED_SUBKEYS carries the other half
        (a write that OMITS it keeps whatever is on disk).

        Its bounds are CONFIG_APPROVAL_TOAST_*, not the waiting-toast pair - see the
        constant block for why the two cannot share a floor.

        `isinstance(x, bool)` before the int check, both ways round: bool subclasses
        int, so {"thresholdSec": true} passes a naive int test as 1 and {"enabled": 1}
        would sail through as truthy.
        """
        if not isinstance(value, dict):
            return False, None
        keys = set(value)
        if not ({"thresholdSec", "enabled"} <= keys
                <= {"thresholdSec", "enabled", "approvalThresholdSec"}):
            return False, None
        threshold, enabled = value["thresholdSec"], value["enabled"]
        if not isinstance(enabled, bool):
            return False, None
        if isinstance(threshold, bool) or not isinstance(threshold, int):
            return False, None
        if not (CONFIG_TOAST_MIN_SEC <= threshold <= CONFIG_TOAST_MAX_SEC):
            return False, None
        normalized = {"thresholdSec": threshold, "enabled": enabled}
        if "approvalThresholdSec" in value:
            approval = value["approvalThresholdSec"]
            if isinstance(approval, bool) or not isinstance(approval, int):
                return False, None
            if not (CONFIG_APPROVAL_TOAST_MIN_SEC <= approval
                    <= CONFIG_APPROVAL_TOAST_MAX_SEC):
                return False, None
            normalized["approvalThresholdSec"] = approval
        return True, normalized

    @staticmethod
    def _validate_digest(value):
        """-> (ok, normalized). BOTH members required, same rule as toast: the notifier
        fires ONE toast at `time`, and a block carrying only `enabled` would leave it
        reading the hour from a default nobody chose.

        `time` is normalized through the quiet-hours parser, so the daily digest and the
        quiet window cannot disagree about what "07:05" means - the digest is suppressed
        by quiet hours, and two parsers would eventually disagree at exactly the boundary
        that matters. The bool check leads for the same reason it does in _validate_toast:
        bool subclasses int, so `{"enabled": 1}` must not sail through as truthy.
        """
        if not isinstance(value, dict) or set(value) != {"enabled", "time"}:
            return False, None
        if not isinstance(value["enabled"], bool):
            return False, None
        minute = _parse_hhmm(value["time"])
        if minute is None:
            return False, None
        return True, {"enabled": value["enabled"],
                      "time": "%02d:%02d" % divmod(minute, 60)}

    @staticmethod
    def _validate_budget(value):
        """-> (ok, normalized). null CLEARS the budget, unlike toast and digest: those
        carry an `enabled` bool that can say "off" while remembering the setting, and a
        budget is a single number with no such member - so removal has to be expressible.
        A cleared budget drops burn.budget entirely on the next /v1/state.

        The shape check is budget_target's, deliberately: the endpoint and the served
        block must never disagree about which values are a budget.
        """
        if value is None:
            return True, None
        target = budget_target(value)
        if target is None:
            return False, None
        return True, {"dailyOutputTokens": target}

    # SEC-c (2026-08-28 audit): a `_validate_panel_approvals` validator was DELETED here.
    # It was never called - panelApprovals is deliberately not in CONFIG_WRITABLE and
    # _do_config has no branch for it - so it stood only as a latent invitation to wire
    # the security flag into the config endpoint. panelApprovals MUST stay non-writable
    # via /v1/config (SEC-2, 2026-08-27); do not re-add a validator for it.


class CrabdServer(ThreadingHTTPServer):
    # SO_REUSEADDR is a per-platform ANSWER, not a constant, because the option means
    # two different things: on Windows it admits a second listener on a port already
    # being listened on (two crabds answering half the requests each - measured during
    # build QA), and on BSD/Linux it does not, so all it buys there is a restart inside
    # the TIME_WAIT window of the last connection. Each platform class says which it is;
    # a collision is loud on all three either way.
    # Read ONCE, at import, onto the class attribute - socketserver reads it per bind
    # but this expression is not re-evaluated. A test that swaps crabd.PLATFORM must set
    # this attribute too, or it is measuring the platform it replaced.
    allow_reuse_address = PLATFORM.server_reuse_address()
    daemon_threads = True
    # socketserver's default accept backlog is FIVE. That was survivable while crabd
    # only saw hook POSTs; it is not now that the control surface is wired. The status
    # line command posts on a ~300 ms debounce, the hooks fire around it, and the OTLP
    # exporter flushes batches on its own clock - so short bursts of concurrent
    # connections are the NORMAL shape of this traffic, not a pathology. Past the
    # backlog the kernel stops completing handshakes and the client sees a CONNECT that
    # hangs, which is the worst possible failure for a status line: the operator's own
    # prompt stalls and nothing anywhere logs why. Reproduced in this suite on
    # 2026-08-26 as scattered "urlopen error timed out" at sock.connect().
    request_queue_size = 128


def _refresh_loop(builder: StateBuilder, stop: threading.Event) -> None:
    """The snapshot is built here, not in the request path, so /v1/state is a dict
    dump. A stalled builder shows up honestly as a stale generatedAt."""
    while not stop.is_set():
        try:
            state = builder.build()
            with builder._lock:
                builder._state = state
        except Exception as exc:  # a bad transcript must not kill the feed
            print(f"crabd: refresh error: {type(exc).__name__}", file=sys.stderr)
        stop.wait(REFRESH_INTERVAL_SEC)


def _recap_loop(recap: RecapReader, stop: threading.Event) -> None:
    """Own thread: `git log` per repo is a subprocess, and /v1/state must never wait
    on one."""
    while not stop.is_set():
        try:
            recap.poll(time.time())
        except Exception as exc:  # a wedged repo must not kill the feed
            print(f"crabd: recap error: {type(exc).__name__}", file=sys.stderr)
        stop.wait(RECAP_POLL_SEC)


def _fleet_loop(fleet: FleetReader, stop: threading.Event) -> None:
    """Own thread: two schtasks subprocesses, and /v1/state must never wait on one."""
    while not stop.is_set():
        try:
            fleet.poll(time.time())
        except Exception as exc:  # a wedged schtasks must not kill the feed
            print(f"crabd: fleet error: {type(exc).__name__}", file=sys.stderr)
        stop.wait(FLEET_POLL_SEC)


def _expiry_loop(builder: StateBuilder, stop: threading.Event) -> None:
    """The v0.12.0 stores age out on their own clock, not the builder's.

    Deliberately a SEPARATE thread from _refresh_loop rather than one more line inside
    build(): a queued continue must expire, and a stale day's cost must be dropped, even
    when the builder is wedged on a pathological transcript - otherwise the one failure
    mode where crabd is serving stale data is also the one where it starts delivering
    ten-minute-old prompts to sessions.
    """
    while not stop.is_set():
        now = time.time()
        try:
            if builder.continues:
                builder.continues.prune(now)
            if builder.otlp:
                builder.otlp.prune(now)
            if builder.statusline:
                builder.statusline.prune(now)
        except Exception as exc:
            print(f"crabd: expiry error: {type(exc).__name__}", file=sys.stderr)
        stop.wait(EXPIRY_POLL_SEC)


def _bind_server(host: str, port: int) -> tuple[CrabdServer | None, str | None]:
    """Bind, or say why not. -> (server, None) or (None, message). Never both.

    ONE ATTEMPT, on exactly the port it was given. The tempting shape - "busy? try the
    next one" - produces the worst failure this daemon has: crabd reports itself up on
    10000 while every hook, the status line command and the panel are still addressing
    9999, so the feed is empty and nothing anywhere says why. A daemon that cannot have
    the port it was told to have has failed, and says so.

    The message names the port, and says how to FIND the holder rather than guessing at
    it. 2722 was ours alone and "another crabd is running" was a fair guess; 9999 is a
    popular number and the holder is usually something else entirely. The command it
    suggests comes from the PLATFORM (`lsof` is not on a Windows box), and it quotes
    what the operating system actually said - "[Errno 48] Address already in use" is a
    sentence the operator can search for and it separates a busy port from a permission
    refusal on a privileged one, where the class name "OSError" separates nothing.
    """
    try:
        return CrabdServer((host, port), Handler), None
    except OSError as exc:
        return None, (
            f"crabd: cannot listen on {host}:{port} - {exc}. If another process is "
            f"holding the port, this names it: {PLATFORM.port_holder_hint(port)} - "
            f"then stop it, or set CRABD_PORT to run crabd on a different port (the "
            f"panel and the hooks have to be pointed at the same number).")


def main() -> int:
    started = time.time()
    recap = RecapReader()
    fleet = FleetReader()
    history = HistoryLog()
    hooks = HookTracker(history=history)
    # Replay BEFORE the builder runs: the first /v1/state must already carry the
    # doneToday and the rings this crabd inherited, not a zero that fills in later.
    hooks.replay(history.replay())
    statusline = StatusLineReader()
    continues = ContinueQueue()
    permissions = PermissionBroker()
    # The receiver reaches the session rings through the builder, so it is constructed
    # with a late-bound callable rather than the builder itself - the builder needs the
    # receiver in its own constructor, and a mutual reference is how a scoping rule
    # becomes a lie later ("telemetry may only APPEND to a served row" is enforced in
    # StateBuilder.note_session_event, and this is the only door to it).
    holder: dict = {}
    otlp = OtlpReceiver(
        on_event=lambda sid, text: holder["builder"].note_session_event(sid, text))
    builder = StateBuilder(TranscriptStore(PROJECTS_DIR), hooks,
                           LimitsReader(), started, UserConfig(), recap, fleet,
                           history, statusline, otlp, continues, permissions,
                           models=ModelCatalog())
    holder["builder"] = builder
    # v0.29.0: the pairing code is minted on first start and lives beside config.json.
    # Attached to the builder (like the broker) so a test double can carry its own.
    builder.panel_token = PanelToken.load_or_create(PANEL_TOKEN_FILE)
    Handler.builder = builder
    stop = threading.Event()
    thread = threading.Thread(target=_refresh_loop, args=(builder, stop), daemon=True)
    thread.start()
    threading.Thread(target=_recap_loop, args=(recap, stop), daemon=True).start()
    threading.Thread(target=_fleet_loop, args=(fleet, stop), daemon=True).start()
    threading.Thread(target=_expiry_loop, args=(builder, stop), daemon=True).start()

    # Said BEFORE the bind, so it is the first thing on stderr rather than something to
    # scroll back for. Not fatal: crabd without a panel is still the feed the notifier,
    # the glow and an iCUE widget all live on.
    if not PANEL_DIR.is_dir():
        print(f"crabd: the panel directory {PANEL_DIR} is not there - the API will "
              f"serve normally but http://{HOST}:{PORT}/ will answer 404 "
              f"(set CRABD_PANEL_DIR)", file=sys.stderr, flush=True)
    server, failure = _bind_server(HOST, PORT)
    if server is None:
        stop.set()
        print(failure, file=sys.stderr, flush=True)
        return 1
    print(f"crabd {VERSION} listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
