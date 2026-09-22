#!/usr/bin/env python3
"""crabd - SideCrab companion service.

Serves the /v1/state document defined in docs/STATE-CONTRACT.md (schema 5, the last
breaking shape; the v0.6.x through v0.28.0 fields ride on it additively) on
127.0.0.1:2722 for the SideCrab widget, from eleven sources:

  1. Claude Code hooks POSTed to /v1/hook  -> session state machine, per-session events
  2. ~/.claude/projects/**/*.jsonl         -> titles, model, token burn, questions,
                                              contextTokens
  3. the Claude OAuth usage endpoint       -> 5-hour / weekly limit gauges (the FALLBACK
                                              source since v0.12.0)
  4. ~/.sidecrab/config.json               -> quiet hours, the quiet OVERRIDE (v0.23.0),
                                              toast, digest, the burn budget, panel
                                              approvals, the reply gate
  5. `git log` in today's session cwds     -> recap.commits, recap.week[].commits
  6. `schtasks /query` on the SideCrab tasks -> fleet (the notifier)
  7. ~/.sidecrab/history.jsonl             -> replayed at startup so doneToday, the
                                              per-session events ring and recap.week
                                              survive a crabd restart
  8. the status line document on /v1/statusline (v0.12.0) -> limits and per-session
                                              context, officially, with no OAuth token
                                              in the picture; PREFERRED over source 3
  9. OTLP http/json on /v1/metrics + /v1/logs (v0.12.0) -> burn.costUSD in real dollars,
                                              api_error events onto the session rings
 10. GetSystemTimes / GlobalMemoryStatusEx (v0.22.0) -> `host`: this machine's CPU
                                              utilization and memory, for the panel
                                              beside the HWiNFO temperature sensors
 11. GET /v1/models on the same OAuth token (v0.28.0) -> the context WINDOW size behind
                                              contextWindowTokens, for a model id that
                                              carries no [1m]/[200k] marker (which is
                                              every live one); ranked BELOW the status
                                              line and the marker, both session-specific

and accepts touch actions on /v1/action (ack / ack-all / queue-continue / decide /
quiet; reply is 501, see below), answers the Stop and PermissionRequest hooks on
/v1/hook/stop and /v1/hook/permission, plus quietHours / toast / digest / budget
writes on /v1/config.

/v1/panel-log (v0.24.0) is a SIDE CHANNEL, not a source: the panel POSTs short
diagnostic lines to it and a maintainer GETs them back, because the panel host renders it
on a surface no devtools can reach. It is in-memory only, it feeds nothing, and nothing in
here ever reads a stored line back into a decision.

stdlib only, Python 3.13, Windows host. ~/.claude is read strictly read-only.
"""

from __future__ import annotations

import csv
import ctypes
import hmac
import json
import math
import os
import re
import select        # lane B: hang-up detection on an open /v1/events stream
import secrets
import struct                          # lane A: the HWiNFO shared-memory layout
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath

# The served `schema` marks the last BREAKING shape, NOT the feature level - see the
# VERSIONING REWORK section of docs/STATE-CONTRACT.md. Additive fields (contextTokens,
# fleet, everything after) ship under this same number and are found by FIELD PRESENCE;
# only a change that alters or removes an existing field bumps it, and that bump costs a
# coordinated deploy. The lesson that bought this: crabd and the panel page do not
# redeploy together, so shipping schema N+1 dead-feeds the on-glass panel until the page
# crabd serves has been updated too.
SCHEMA_BREAKING = 5
VERSION = "0.35.0"

HOST = "127.0.0.1"
# 2722 is the production port and the Scheduled Task owns it. CRABD_PORT exists so a
# test instance can run against the real ~/.claude without racing the live service.
PORT = int(os.environ.get("CRABD_PORT") or 2722)

SIDECRAB_DIR = Path.home() / ".sidecrab"
USER_CONFIG_FILE = SIDECRAB_DIR / "config.json"
# The panel pairing code (v0.29.0, closes SEC-a + WID-a). A 10-symbol secret crabd mints
# once and keeps in the user's profile; the panel presents it on every `decide`. It is the
# one thing a web page the operator visits cannot obtain. Same-user local processes can
# read the file - they can also drive the terminal dialog, so they were never in the
# threat model. Crockford-style alphabet (no I, L, O, U) so a code read off a terminal and
# typed into the panel cannot be mis-transcribed.
PANEL_TOKEN_FILE = SIDECRAB_DIR / "panel-token"
PANEL_TOKEN_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
# SCA-005: where an unusable pairing file is kept when a new code is minted over it.
PANEL_TOKEN_UNUSABLE_SUFFIX = ".unusable"
PANEL_TOKEN_LEN = 10                 # 32^10 = 2^50 - hopeless to guess at loopback speed once
PANEL_TOKEN_MAX_FAILURES = 10        # ...the lockout below bounds the rate anyway
PANEL_TOKEN_WINDOW_SEC = 60.0
# C4 / MF-017: the readiness probe's OWN budget, counting ATTEMPTS rather than failures.
# Separate from the decide lockout on purpose - a panel checking whether it is paired
# must not be able to lock the operator out of Approve and Deny, and a wrong code here
# must not be cheaper than a wrong code there. Five a minute against 32^10 is not a
# guessing route.
PANEL_TOKEN_PROBE_MAX = 5
PANEL_TOKEN_PROBE_WINDOW_SEC = 60.0
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
# The `sources.limitsToken` note while the endpoint is locked out (M-02, v0.35.0). It is
# the SOURCE's diagnosis, never the `limits` block's caveat: the two answer different
# questions, and serving the caveat as the diagnosis is what hid the lockout.
LIMITS_BACKOFF_NOTE = ("the usage endpoint is rate-limited; the gauges are the last "
                       "good reading")
LIMITS_CACHE_FILE = SIDECRAB_DIR / "limits-cache.json"  # survives restarts; no secrets in it
# v0.30.0: an OPTIONAL long-lived token for the usage endpoint. The CLI's own access
# token in ~/.claude/.credentials.json lives ~6 h and is rewritten only when a terminal
# `claude` makes an API call - the desktop app keeps its refreshed token elsewhere - so a
# panel fed from that file reads "token expired" most mornings. `claude setup-token`
# mints a token that lasts about a year; Install-SideCrab.ps1 -LimitsToken stores it here
# DPAPI-protected (CurrentUser), and crabd decrypts it in memory when the CLI token is
# past its expiry. Never logged, never served, never written anywhere else.
LIMITS_TOKEN_FILE = SIDECRAB_DIR / "limits-token.dpapi"
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
# the list - if the panel is reading this document, crabd is running.
# `glow` left this tuple with the RGB retirement (CLEAN-05, 2026-09-21). A component that
# no longer ships would have reported `absent` or a permanently `stopped` task forever,
# and a health indicator that can only ever say one thing is a fault light nobody can
# clear. The key simply stops being served; see docs/history/RGB-retired-2026-09-21.md.
FLEET_TASKS = (("toast", "SideCrab-toast"),)
FLEET_REFRESH_SEC = 60       # contract: cached ~60 s
FLEET_POLL_SEC = 5.0
FLEET_TIMEOUT_SEC = 10       # contract
# Measured 2026-08-26 on the Windows host: `schtasks /query /tn SideCrab-toast /fo csv /nh`
# exits 0 with '"\SideCrab-toast","N/A","Running"' - the status is the THIRD csv field.
FLEET_STATUS_COL = 2
FLEET_STATUS_MAP = {"running": "running", "ready": "stopped",
                    "queued": "stopped", "disabled": "stopped"}
# Same measurement, unregistered name: exit 1, stderr 'ERROR: The system cannot find
# the file specified.' The second phrasing is schtasks' other not-found wording ('The
# specified task name ... does not exist'); anything else that fails is `unknown`,
# because a task that exists and cannot be read is NOT the same claim as an absent one.
FLEET_ABSENT_MARKERS = ("cannot find", "does not exist")

# --- v0.22.0 `host`: the machine's own CPU and memory, beside the HWiNFO temperatures.
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
# ---- v0.35.0 (provisional label): the six additive session members ----
# `activity.detail` is capped at 80 by the contract. The tool NAME is capped too, for
# the reason every other served string is: it comes out of a transcript record crabd
# does not write, and an unbounded one is a payload.
ACTIVITY_DETAIL_MAX = 80
ACTIVITY_TOOL_MAX = 40
# WHICH INPUT KEY `detail` may read, per tool. An allowlist and not a fallback chain:
# see FileFacts._note_tool_use for why the command text is never a candidate. `Task` is
# `Agent`'s other name, exactly as the subagent-label branch already treats it.
ACTIVITY_DESCRIPTION_TOOLS = frozenset({"Bash", "PowerShell", "Agent", "Task"})
ACTIVITY_PATH_TOOLS = frozenset({"Edit", "Write", "Read"})
ACTIVITY_PATTERN_TOOLS = frozenset({"Grep", "Glob"})
# The two tools that CHANGE a file. Read is deliberately absent: `filesTouched` answers
# "what has this session edited", and folding reads in would make every card claim a
# hundred files on a session that changed none.
FILE_TOUCH_TOOLS = frozenset({"Edit", "Write"})
FILES_RECENT_CAP = 5
TODO_CURRENT_MAX = 80
MODE_MAX = 40
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
# v0.33.0 (provisional) - the per-project vocabularies, `continuePromptsByRepo` and
# `continuePromptsByPath`. Same per-list bound as the global extras, and the same one
# again on a SESSION's combined extras: the repo list fills first, so a path list only
# reaches the sheet while the repo list has left room. Twenty is already past what the
# sheet shows without scrolling (measured 2026-09-21: eight buttons fill the row at
# 2560x720), so this cap bounds the payload, not the design.
CONTINUE_PROMPTS_PROJECT_CAP = 20
# How many KEYS each map may carry. This bounds the PARSE, not a request: the parse is
# cached per config load (UserConfig._projects) and one queue-continue consults one
# repo key plus one path key.
CONTINUE_PROMPTS_PROJECT_KEYS = 50
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
TRANSCRIPT_PARSE_LOG_KEY = "transcript-parse"       # M-05 (v0.35.0)
CONFIG_UNPARSEABLE_LOG_KEY = "config-unparseable"   # M-03 (v0.35.0)
# The 503 body for a /v1/state that has no snapshot to serve YET. Distinct from every
# other error body in this file so a reader can tell "crabd is still coming up" from
# "crabd refused you" (403) and from "no such path" (404).
STATE_NOT_BUILT = b'{"error":"state not built yet"}'
# Bound on the once-log key sets. TWO sets, not one (SCA-032): the keys in this file are
# literals and their cap is really a leak guard, but the per-project config validators
# build keys out of OPERATOR DATA, and a config with enough distinct bad entries used to
# consume the single global cap and take every later first-time diagnostic with it -
# measured by the audit at 64 warnings from one synthetic config, after which an injected
# unreadable-transcript exception incremented the skip counter and printed nothing. A
# fixed failure class must still get its one line however badly the config is typed.
LOG_ONCE_MAX_KEYS = 64
LOG_ONCE_MAX_CONFIG_KEYS = 64
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
# A session id long enough to be a memory-growth vector rather than an identifier. Real
# ones are 36-char UUIDs; this is generous enough that no legitimate id is refused.
SESSION_ID_MAX = 200

# ---------------------------------------------------------------- v0.31.0 the panel host
# crabd serves the panel itself at GET /panel/ (contract v0.31.0), so a standalone host
# window (panel-host/, WebView2) or any local browser can run the same widget/ tree that
# ships with the panel host. Two gates come with it, both answered before any route:
#
#   1. Host allowlist (HOST_NOT_ALLOWED, 421). A DNS-rebinding page resolves its own name
#      to 127.0.0.1 and then reads or drives crabd with a Host of `attacker.example:2722`
#      - an Origin check cannot stop it (the page IS same-origin to itself), the Host
#      header can. Only the two names that mean this socket are accepted, with the BOUND
#      port, so a test instance on another port never accepts 2722's names.
#   2. Same-origin allowlist. The panel's fetches carry `Origin: http://127.0.0.1:2722`
#      (a real http origin, which SEC-1/SEC-4 refuse). Exactly the two origins that name
#      this socket are allowed, beside a request with no Origin header at all - which is
#      every native client. Since v0.34.0 (provisional) nothing else is: `null` and the
#      non-web schemes went with the retired vendor page. See Handler._refused_origin.
#
# The file allowlist IS the traversal defence: a request path is looked up in this map
# and never joined onto the filesystem, so `..`, encoded dots and absolute paths have
# nothing to escape with - an unknown key is a 404. mock/ and translation.json are
# deliberately absent: the ?mock= dev switches are unreachable from the served origin.
HOST_NOT_ALLOWED = b'{"error":"host not allowed"}'
PANEL_NOT_AVAILABLE = b'{"error":"panel not available"}'
PANEL_DIR = Path(os.environ.get("CRABD_PANEL_DIR")
                 or (Path(__file__).resolve().parent.parent / "widget"))
PANEL_FILES = {
    "index.html": ("index.html", "text/html; charset=utf-8"),
    "styles/sidecrab.css": ("styles/sidecrab.css", "text/css; charset=utf-8"),
    "scripts/sidecrab.js": ("scripts/sidecrab.js", "text/javascript; charset=utf-8"),
    "resources/icon.svg": ("resources/icon.svg", "image/svg+xml"),
}
PANEL_ALLOWED_HOSTNAMES = ("127.0.0.1", "localhost")


# ---- lane B: server-sent events (GET /v1/events) ----
# The panel's own transport. The widget polled /v1/state every 3 s, so a question the
# operator is standing in front of could sit unlit for that long; this route pushes each
# NEW snapshot as it is published and keeps the poll as the fallback.
#
# It sits behind the SAME two gates as every other route - _host_allowed then
# _refused_origin, in that order, with the same 421/403 bodies - because a long-lived
# readable stream of /v1/state is the widest read surface crabd has: cwds, titles, the
# full question text and pendingPermission, and an EventSource a visited page opened
# would go on delivering them.
SSE_MAX_SUBSCRIBERS = 8
SSE_PING_SEC = 15.0
# The builder publishes on its own 2 s cadence and never calls out to a subscriber, so
# detection is a poll of builder.state (which takes the builder lock only to COPY the
# reference, never across a socket write). 250 ms is a quarter of the "within 1 s" the
# contract promises, and 8 subscribers x 4 reads/s is 32 lock acquisitions a second
# against a lock the builder holds for one assignment.
SSE_POLL_SEC = 0.25
# Matches the widget's own first backoff step, so a client that honours the field and one
# that reconnects on its own schedule behave alike.
SSE_RETRY_MS = 3000
SSE_TOO_MANY = b'{"error":"too many subscribers"}'


class SseSubscribers:
    """The concurrent-subscriber cap for GET /v1/events.

    Each stream parks a handler THREAD for as long as it is open, so the cap is what
    stops a page opening EventSources until crabd has no threads left for the hooks a
    session is blocked on. Deliberately a counter and not a queue: a refused subscriber
    is told so (503) rather than held, because a browser that is waiting for headers
    looks exactly like one that is connected.
    """

    def __init__(self, limit: int = SSE_MAX_SUBSCRIBERS) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._count = 0

    def acquire(self) -> bool:
        with self._lock:
            if self._count >= self.limit:
                return False
            self._count += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._count > 0:
                self._count -= 1

    @property
    def count(self) -> int:
        with self._lock:
            return self._count


# The fallbacks a Handler built without a CrabdServer gets (a bare double in a unit
# test). Never reached by a served request: CrabdServer constructs its own pair.
_SSE_FALLBACK_STOP = threading.Event()
_SSE_FALLBACK_SLOTS = SseSubscribers()


def sse_frame(event: str, data: bytes) -> bytes:
    """One SSE frame. `data` must already be ONE line - json.dumps never emits a raw
    newline, and a multi-line data field would be read as two events by the client."""
    return b"event: " + event.encode("ascii") + b"\ndata: " + data + b"\n\n"


# ---------------------------------------------------------------- v0.24.0 constants
# The panel diagnostics log channel (POST/GET /v1/panel-log). The panel renders on the
# Xeneon Edge, where no devtools can be attached - a console.log has nowhere to go, so the
# page ships short lines here instead and a maintainer reads them over HTTP. These four bounds are the entire flood posture: there is no rate limit because
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


# ------------------------------------------------------- v0.35.0 crabd's own log
# THE GAP THIS CLOSES: until now crabd had no log file. `~/.sidecrab/logs/` held the
# panel, notifier and ack-handler logs and nothing of crabd's own, and crabd runs under a
# Scheduled Task with no console - so every `print(..., file=sys.stderr)` in this file,
# every _log_once line and every traceback that escaped a worker thread went to a handle
# nobody owns. Two live inconsistencies were measured on 2026-09-22 (the frozen
# `sources.limitsToken` verdict and a fleet reading that disagreed with schtasks) and
# neither left a single line anywhere to diagnose them from.
#
# stderr keeps everything it gets today. This is additive: the same line goes to both, so
# a maintainer running crabd in a console sees no change.
CRABD_LOG_FILE = Path.home() / ".sidecrab" / "logs" / "crabd.log"
# ~1 MB and three generations, matching the notifier's own posture. Sized so a crash loop
# writing a traceback per restart still leaves the first one readable.
CRABD_LOG_MAX_BYTES = 1_000_000
CRABD_LOG_GENERATIONS = 3


class CrabdLog:
    """The rotating file behind log_line(). NEVER raises - it is called from inside the
    exception handlers whose whole job is to keep a failure from reaching the operator,
    and a logger that can throw turns a swallowed error into a crashed thread.

    A write failure disables the file for the life of the process rather than being
    retried per line: an unwritable ~/.sidecrab (a locked profile, a full disk) would
    otherwise cost an OSError on every diagnostic on the busiest path there is. stderr
    is unaffected either way, so nothing is lost that was not already lost today.

    `path` is read off the module global on every call, never captured in __init__, for
    the reason LimitsReader.cache_file is: the suites repoint the global at a temp dir,
    and a captured path is one forgotten patch away from writing the operator's live log.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._disabled = False

    @property
    def path(self) -> Path:
        return self._path or CRABD_LOG_FILE

    def write(self, message: str, exc: BaseException | None = None) -> None:
        if self._disabled:
            return
        line = f"{_utc_iso(time.time())} {message}"
        if exc is not None:
            # The traceback goes to the FILE and not to stderr: stderr's one-line shape is
            # what the existing callers print and what a console reader expects, while the
            # file is the only place a Scheduled Task's traceback can land at all.
            line += "\n" + "".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)).rstrip()
        with self._lock:
            try:
                path = self.path
                path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate(path)
                with path.open("a", encoding="utf-8", errors="replace", newline="\n") as fh:
                    fh.write(line + "\n")
            except OSError:
                self._disabled = True

    @staticmethod
    def _rotate(path: Path) -> None:
        """Caller holds the lock. Rolls only when the file is already at the cap, so an
        ordinary line costs one stat. os.replace, so a generation is never half-moved."""
        try:
            if path.stat().st_size < CRABD_LOG_MAX_BYTES:
                return
        except OSError:
            return                      # no file yet, or it cannot be stat'ed: nothing to roll
        for gen in range(CRABD_LOG_GENERATIONS - 1, 0, -1):
            src = path.with_name(f"{path.name}.{gen}")
            if src.exists():
                os.replace(src, path.with_name(f"{path.name}.{gen + 1}"))
        os.replace(path, path.with_name(f"{path.name}.1"))


_CRABD_LOG = CrabdLog()


def log_line(message: str, exc: BaseException | None = None,
             stderr: bool = True) -> None:
    """One diagnostic, to crabd.log and (by default) to stderr.

    `stderr=False` is for the lines that already reached stderr through their own
    print() - the file gets the copy, the console is not told twice.
    """
    if stderr:
        print(message, file=sys.stderr, flush=True)
    _CRABD_LOG.write(message, exc)


# --------------------------------------------------------------------------- utils

_LOG_ONCE_SEEN: set[str] = set()            # keys that are literals in this file
_LOG_ONCE_CONFIG_SEEN: set[str] = set()     # keys built from the operator's config
_LOG_ONCE_SUPPRESSED: set[str] = set()      # which sets have already said they are full
_LOG_ONCE_LOCK = threading.Lock()


def _log_once(key: str, message: str, config: bool = False) -> None:
    """One honest stderr line the FIRST time a swallowed failure happens, then silence.

    Every catch added in v0.20.0 reports through here. A swallowed exception that logs
    NOTHING is the failure mode the honest-failure rule exists to forbid; one that logs
    on every pass is a 2 s heartbeat of noise for a transcript line that will never be
    read again, and noise is how a real signal gets ignored.

    `config=True` marks a key built from operator data (a repo name, a path) and sends
    it to its own bounded set (SCA-032). The two sets never share a budget, so no
    quantity of malformed config can silence the first report of an unrelated transcript,
    sampler or build failure. A set that fills says so once, and that notice is itself
    one of the once-lines rather than a per-pass complaint.
    """
    seen = _LOG_ONCE_CONFIG_SEEN if config else _LOG_ONCE_SEEN
    cap = LOG_ONCE_MAX_CONFIG_KEYS if config else LOG_ONCE_MAX_KEYS
    label = "config" if config else "internal"
    with _LOG_ONCE_LOCK:
        if key in seen:
            return
        if len(seen) >= cap:
            if label in _LOG_ONCE_SUPPRESSED:
                return
            _LOG_ONCE_SUPPRESSED.add(label)
            message = (f"crabd: more than {cap} distinct {label} diagnostics - the rest "
                       f"are suppressed for the life of this process")
        else:
            seen.add(key)
    log_line(message)


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


def _path_leaf(path) -> str | None:
    """The file name of a tool input's `file_path`, or None (v0.35.0).

    PureWindowsPath for _cwd_title's reason: it parses '/' and '\\' alike, so the same
    code reads C:\\repo\\a.py, \\\\server\\share\\a.py and /home/u/a.py, on any host. Only
    the LEAF is ever served - a full path is a machine layout and a user name, and the
    panel is a screen on a desk.

    A bare drive root and a bare UNC share have no leaf of their own and serve None
    rather than the drive letter. A TRAILING separator is stripped by PureWindowsPath, so
    'D:\\work\\' reads as 'work' - measured, and left alone: a tool's `file_path` is a
    file and never ends in one.
    """
    if not isinstance(path, str) or not path.strip():
        return None
    return PureWindowsPath(path.strip()).name or None


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
    here and nowhere else. Every consumer - the panel's dim, the notifier's
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
        # lane D (v0.33.0): the parsed per-project maps, and the _data object they were
        # parsed FROM. get() replaces _data with a new dict on every re-read, so an
        # identity check is the whole invalidation - and it is what keeps a malformed
        # map's warning to one line per config load rather than one every 2 s build.
        self._projects: tuple[dict, list] | None = None
        self._projects_for: dict | None = None

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

    # ---- lane D (v0.33.0, provisional): a continue vocabulary per project ----

    def _project_list(self, raw, where: str) -> list[str] | None:
        """One project's prompts, validated exactly as continue_extras validates the
        global list - strings only, whitespace collapsed, CONTINUE_PROMPT_MAX, deduped,
        capped.

        None means MALFORMED and [] means "the operator wrote an empty list", and
        SCA-011 is what the difference costs: an empty list is a deliberate instruction
        - this project gets no path extras - and collapsing the two made `[]` vanish and
        the less-specific entry apply instead. A malformed value is still not an error;
        the rest of the operator's config must survive one bad key.
        """
        if not isinstance(raw, list):
            _log_once("cfgproj:" + where,
                      f"crabd: config.json {where} is not a list - ignored",
                      config=True)
            return None
        out: list[str] = []
        dropped = False
        for entry in raw:
            if len(out) >= CONTINUE_PROMPTS_PROJECT_CAP:
                dropped = True
                break
            if not isinstance(entry, str):
                dropped = True
                continue
            prompt = " ".join(entry.split())
            if not prompt or len(prompt) > CONTINUE_PROMPT_MAX or prompt in out:
                dropped = True
                continue
            out.append(prompt)
        if dropped:
            _log_once("cfgprojdrop:" + where,
                      f"crabd: config.json {where} - some entries dropped (strings "
                      f"only, 1..{CONTINUE_PROMPT_MAX} chars, deduped, "
                      f"{CONTINUE_PROMPTS_PROJECT_CAP} max)", config=True)
        return out

    def _projects_parsed(self, now: float) -> tuple[dict, list]:
        """(by-repo map, by-path list) from config.json, parsed once per config load.

        by-repo is keyed by CASEFOLDED repo name: the key an operator types is the name
        the card shows, and git's casing is not something they chose. Two keys differing
        only in case are a collision the first one wins, warned rather than merged.

        by-path answers a MEASURED gap, it is not a second way to say the same thing.
        `sessions[].repo` is the origin remote's name (GitLookup._remote_name), so
        measured 2026-09-21: a release tree and a linked worktree of the same repo BOTH
        read `sidecrab`, and a session whose cwd is not a repo at all reads null - no
        repo key can reach either case. Keyed on a path PREFIX, longest match wins,
        sorted here so the match is a scan of a pre-ordered list.
        """
        data = self.get(now)
        with self._lock:
            if self._projects_for is data and self._projects is not None:
                return self._projects

        by_repo: dict[str, list[str]] = {}
        # Tracked SEPARATELY from by_repo (SCA-011): the first-wins rule is about the
        # KEY, and a first key whose list is empty or malformed still owns the name.
        # Keying the collision test off by_repo let a later `EXAMPLE` win over an earlier
        # `Example: []`, which is the documented rule read backwards.
        seen_repo: set[str] = set()
        raw_repo = data.get("continuePromptsByRepo")
        if raw_repo is not None and not isinstance(raw_repo, dict):
            _log_once("cfgrepo:type", "crabd: config.json continuePromptsByRepo is "
                                      "not an object - ignored")
            raw_repo = None
        for key, value in list((raw_repo or {}).items())[:CONTINUE_PROMPTS_PROJECT_KEYS]:
            name = key.strip() if isinstance(key, str) else ""
            if not name:
                _log_once("cfgrepo:blank", "crabd: config.json continuePromptsByRepo "
                                           "has a blank key - ignored")
                continue
            folded = name.casefold()
            if folded in seen_repo:
                _log_once("cfgrepo:dup:" + folded,
                          f"crabd: config.json continuePromptsByRepo has two keys that "
                          f"differ only in case ({name}) - the first one is used",
                          config=True)
                continue
            seen_repo.add(folded)
            prompts = self._project_list(value, f"continuePromptsByRepo[{name}]")
            if prompts is not None:
                by_repo[folded] = prompts

        by_path: list[tuple[str, str, list[str]]] = []
        raw_path = data.get("continuePromptsByPath")
        if raw_path is not None and not isinstance(raw_path, dict):
            _log_once("cfgpath:type", "crabd: config.json continuePromptsByPath is "
                                      "not an object - ignored")
            raw_path = None
        for key, value in list((raw_path or {}).items())[:CONTINUE_PROMPTS_PROJECT_KEYS]:
            root = key.strip() if isinstance(key, str) else ""
            # A RELATIVE key can never match - a session cwd is absolute - so it is
            # refused with a line rather than kept as a key that silently never fires.
            if not root or not os.path.isabs(root):
                _log_once("cfgpath:rel:" + str(key)[:64],
                          f"crabd: config.json continuePromptsByPath key {key!r} is "
                          f"not an absolute path - ignored", config=True)
                continue
            norm = os.path.normcase(os.path.normpath(root))
            prompts = self._project_list(value, f"continuePromptsByPath[{root}]")
            # An EMPTY list is kept and a MALFORMED one is not (SCA-011). The empty list
            # is precedence-bearing: continue_session_extras stops at the longest
            # matching prefix, so an empty entry there is how an operator says "this
            # subtree gets none of the parent's extras". A malformed value carries no
            # such instruction and must not suppress the parent by accident.
            if prompts is not None:
                # Matched at a path BOUNDARY, never as a bare string prefix: without
                # the separator, C:\Dev\side would own C:\Dev\sidecrab as well.
                boundary = norm if norm.endswith(os.sep) else norm + os.sep
                by_path.append((norm, boundary, prompts))
        by_path.sort(key=lambda row: -len(row[1]))   # longest prefix wins

        parsed = (by_repo, by_path)
        with self._lock:
            self._projects_for = data
            self._projects = parsed
        return parsed

    def continue_session_extras(self, now: float, repo, cwd) -> list[str]:
        """The prompts THIS session gets beyond the global list, or [].

        Served as `sessions[].continuePrompts` and consumed as the per-session half of
        the whitelist, so the two can never disagree: one function, both readers.

        Order is general to specific - the repo list, then the path list - and the
        COMBINED result is capped at CONTINUE_PROMPTS_PROJECT_CAP, so a repo list that
        fills the cap leaves no room for a path list. Anything already builtin or
        already in the global extras is dropped here: the widget draws builtins, then
        globals, then this, and a duplicate would be one button drawn twice.
        """
        by_repo, by_path = self._projects_parsed(now)
        picked: list[str] = []
        if isinstance(repo, str) and repo.strip():
            picked.extend(by_repo.get(repo.strip().casefold(), ()))
        if isinstance(cwd, str) and cwd.strip():
            here = os.path.normcase(os.path.normpath(cwd.strip()))
            for norm, boundary, prompts in by_path:
                if here == norm or here.startswith(boundary):
                    picked.extend(prompts)
                    break
        if not picked:
            return []
        globals_ = self.continue_extras(now)
        out: list[str] = []
        for prompt in picked:
            if len(out) >= CONTINUE_PROMPTS_PROJECT_CAP:
                break
            if prompt in out or prompt in globals_ or prompt in CONTINUE_PROMPTS_BUILTIN:
                continue
            out.append(prompt)
        return out

    def continue_prompts_for(self, now: float, repo, cwd) -> tuple[str, ...]:
        """The queue-continue whitelist for ONE session: the global set plus that
        session's project prompts.

        THE GATE THAT MATTERS: a prompt configured only for repo X is not on this tuple
        for a session in repo Y, so that tap is refused with the same 400 an unknown
        prompt has always had. The set stays server-side and stays a whitelist - a
        project map widens what a given session may say, never who may say it, and
        nothing reachable over HTTP can add a key to it.
        """
        return (tuple(CONTINUE_PROMPTS_BUILTIN) + tuple(self.continue_extras(now))
                + tuple(self.continue_session_extras(now, repo, cwd)))

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
                raw = path.read_text(encoding="utf-8")
            except OSError:
                raw = ""        # missing or unreadable: start from the defaults, never {}
            try:
                loaded = json.loads(raw) if raw.strip() else None
            except ValueError:
                # M-03 (v0.35.0). A file that EXISTS and carries text crabd cannot parse
                # is the operator's own hand-edit - a trailing comma, a half-pasted block
                # - and starting from the defaults here REWROTE it: one tap on the panel's
                # quiet button replaced quietHours, budget, digest, panelApprovals and
                # every continue prompt with {"quietHours":null,"allowReply":false}, with
                # nothing on the panel or in a log to say a file had just been lost.
                # Reproduced 2026-09-22 with one trailing comma and one quiet tap.
                #
                # Refusing is what the 500 branch in _do_config already exists for. An
                # EMPTY file is deliberately NOT refused: it holds nothing to lose, and it
                # is the residue of the pre-A-03 truncating writer, so refusing there
                # would wedge an operator out of their own settings permanently.
                _log_once(CONFIG_UNPARSEABLE_LOG_KEY,
                          f"crabd: {path.name} is not valid JSON - refusing to overwrite "
                          f"it; fix the file and the next save will take")
                return False
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
        "context_tokens", "context_ts", "skipped", "seen", "_lock",
        # ---- v0.35.0 (provisional label), the six additive session members ----
        "mode", "turn_tool", "turn_tool_calls", "files_touched",
        "queue_depth", "compactions", "compaction_ts", "todos", "todos_ts",
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
        # M-04 (v0.35.0): whether refresh() has completed a read of this file. The
        # no-change short-circuit used to test `self.offset`, which is 0 for an EMPTY
        # transcript - so a session's freshly created .jsonl was re-opened and re-read on
        # every 2 s pass, and refresh() reported "changed" every time, for as long as it
        # stayed empty. `size`/`mtime` cannot stand in: they are 0 / 0.0 before the first
        # read, which is exactly what an empty file stats as.
        self.seen: bool = False
        # ---- v0.35.0 (provisional label): the six additive members ----
        # `mode` is the newest `mode` record's value, lower-cased and passed through
        # whatever the file says - normal/plan/acceptEdits/bypassPermissions today, and
        # a name this build has never heard of tomorrow. None until one is seen.
        self.mode: str | None = None
        # The newest tool_use of the CURRENT turn, and how many that turn has made. Both
        # reset at the user prompt that opens a turn, so a card that says "Bash x7" is
        # counting this turn and not the session. None / 0 outside a turn.
        self.turn_tool: dict | None = None
        self.turn_tool_calls: int = 0
        # file_path -> the timestamp it was last written, for the Edit/Write tools only.
        # DISTINCT paths, so `count` is files and not edits. Unbounded on purpose: it
        # holds strings and the whole FileFacts is evicted at TRANSCRIPT_WINDOW_SEC, so a
        # cap here would buy nothing and would make `count` a lie once it bit.
        self.files_touched: dict[str, float] = {}
        # Claude Code's OWN typed-ahead queue depth, from its `queue-operation` records.
        # Floored at zero at the serve: a file crabd started reading mid-session can open
        # on a dequeue whose enqueue it never saw, and a negative depth is not a thing.
        self.queue_depth: int = 0
        self.compactions: int = 0
        self.compaction_ts: float = 0.0
        # The newest TodoWrite input, already reduced to {done,total,current}. The raw
        # list is never kept: it carries prompt text, and nothing serves it.
        self.todos: dict | None = None
        self.todos_ts: float = 0.0
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
        self.mode = None
        self.turn_tool = None
        self.turn_tool_calls = 0
        self.files_touched.clear()
        self.queue_depth = 0
        self.compactions = 0
        self.compaction_ts = 0.0
        self.todos = None
        self.todos_ts = 0.0

    def refresh(self) -> bool:
        """Parse whatever is new. Returns True when the file changed."""
        try:
            st = self.path.stat()
        except OSError:
            return False
        if st.st_size == self.size and st.st_mtime == self.mtime and self.seen:
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
        self.seen = True
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
            # M-05 (v0.35.0): COUNTED. A record that does not parse is a record crabd
            # could not read, which is what `skipped` means - and it used to return here
            # silently, so a byte-order mark on the first line or a record the writer
            # half-flushed left no evidence anywhere at all. The `skipped` counter is the
            # only thing that makes "the parser dropped something" answerable.
            self.skipped += 1
            _log_once(TRANSCRIPT_PARSE_LOG_KEY,
                      f"crabd: a transcript record in {self.path.name} is not valid "
                      f"JSON and was skipped; the rest of the file is still read")
            return
        if not isinstance(obj, dict):
            return
        kind = obj.get("type")

        # v0.35.0 `mode`: {type, mode, sessionId} and no timestamp of its own, so it is
        # handled up here with the other untimestamped one-key records. Lower-cased and
        # otherwise passed through: the panel renders what the CLI wrote, and a whitelist
        # here would silently drop a mode a later CLI introduces.
        if kind == "mode":
            value = obj.get("mode")
            if isinstance(value, str) and value.strip():
                self.mode = _trim(value.strip().lower(), MODE_MAX)
            return
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

        # v0.35.0 `promptQueue`: Claude Code's own typed-ahead queue, counted from its
        # `queue-operation` records. Three operations were measured on this host -
        # enqueue, dequeue, remove - and an operation this build does not know is
        # deliberately ignored rather than guessed at in either direction.
        if kind == "queue-operation":
            operation = obj.get("operation")
            if operation == "enqueue":
                self.queue_depth += 1
            elif operation in ("dequeue", "remove"):
                self.queue_depth -= 1
            return
        # v0.35.0 `compaction`: the boundary record the CLI writes once the compaction has
        # HAPPENED. The in-progress half cannot come from here - a compaction that has not
        # finished has written nothing - which is why it takes a PreCompact hook.
        if kind == "system":
            if obj.get("subtype") == "compact_boundary":
                self.compactions += 1
                at = ts or self.last_ts
                if at > self.compaction_ts:
                    self.compaction_ts = at
            return

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
            if isinstance(content, str):
                if self.first_prompt is None:
                    self.first_prompt = _trim(content, TITLE_MAX)
                # v0.35.0 `activity`: a typed prompt OPENS a turn, so both the counter and
                # the last tool reset HERE and nowhere else. Counting from the session
                # start instead would make `callsThisTurn` a number that only ever grows,
                # which is a session total wearing a turn's name.
                self.turn_tool = None
                self.turn_tool_calls = 0
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
            # v0.35.0: every tool_use feeds `activity`, and Edit/Write additionally feed
            # `filesTouched`. Done before the AskUserQuestion / Agent branches below so an
            # AskUserQuestion still counts as a call of this turn - it is one.
            self._note_tool_use(name, inp, ts)
            if name == "AskUserQuestion":
                for question in inp.get("questions") or []:
                    if isinstance(question, dict) and isinstance(question.get("question"), str):
                        text = question["question"].strip()
                        if text:
                            asked.append(text)
            elif name == "TodoWrite":
                self._note_todos(inp.get("todos"), ts)
            elif name in ("Agent", "Task"):
                description = inp.get("description")
                block_id = block.get("id")
                if isinstance(description, str) and description and isinstance(block_id, str):
                    self._pending_agents[block_id] = description

        if asked:
            self._remember_question(" · ".join(asked), ts, 2)
        elif isinstance(tail_text, str):
            self._remember_question(self._trailing_question(tail_text), ts, 1)

    def _note_tool_use(self, name, inp: dict, ts: float) -> None:
        """v0.35.0 `activity` + `filesTouched`, from one tool_use block.

        ⚠ THE COMMAND TEXT IS NEVER TAKEN. `detail` is the tool's own `description` for
        Bash / PowerShell / Agent, the leaf of `file_path` for Edit / Write / Read, and
        the `pattern` for Grep / Glob - a shell command, a file's contents and a prompt
        can all carry a secret, and this member is rendered on a screen on a desk. A tool
        whose detail is not on that list serves null; it never falls back to some other
        key of the same input, because "whatever else was in there" is the rule that
        would eventually put a token on the glass.

        The call is counted whatever the tool is - `callsThisTurn` is how busy the turn
        is, not how many of its tools this build recognises.
        """
        if not isinstance(name, str) or not name:
            return
        self.turn_tool_calls += 1
        detail = None
        if name in ACTIVITY_DESCRIPTION_TOOLS:
            value = inp.get("description")
            detail = value if isinstance(value, str) else None
        elif name in ACTIVITY_PATH_TOOLS:
            detail = _path_leaf(inp.get("file_path"))
        elif name in ACTIVITY_PATTERN_TOOLS:
            value = inp.get("pattern")
            detail = value if isinstance(value, str) else None
        self.turn_tool = {"tool": _trim(name, ACTIVITY_TOOL_MAX),
                          "detail": _trim(detail, ACTIVITY_DETAIL_MAX),
                          "at": ts}
        if name in FILE_TOUCH_TOOLS:
            path = inp.get("file_path")
            if isinstance(path, str) and path.strip():
                # Keyed on the FULL path and served as the leaf: two files called
                # config.py in different directories are two files, and collapsing them
                # would under-count. Re-assigning on a repeat is what makes `recent`
                # order by last touch rather than by first sighting.
                self.files_touched[path.strip()] = ts

    def _note_todos(self, todos, ts: float) -> None:
        """v0.35.0 `todos`, reduced at parse time. Only the three numbers and the one
        line the panel shows are kept - the list itself is the operator's own working
        notes and nothing serves it.

        NEWEST WINS BY TIMESTAMP, `>=` for the reason the context figure uses it: a
        streamed repeat carries the same clock, and on a tie the later LINE is the later
        write in an append-only file.
        """
        if not isinstance(todos, list) or ts < self.todos_ts:
            return
        total = done = 0
        current = None
        for item in todos:
            if not isinstance(item, dict):
                continue
            total += 1
            status = item.get("status")
            if status == "completed":
                done += 1
            elif status == "in_progress" and current is None:
                text = item.get("content") or item.get("activeForm")
                current = text if isinstance(text, str) else None
        if not total:
            # An EMPTY TodoWrite is the list being cleared, which is "no todos" and not
            # "0 of 0" - the absent-not-zero rule, applied at the record that clears it.
            self.todos, self.todos_ts = None, ts
            return
        self.todos = {"done": done, "total": total,
                      "current": _trim(current, TODO_CURRENT_MAX)}
        self.todos_ts = ts

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
        # C3: the scan's own verdict. `last_scan_at` is set on every pass, ok or not, so
        # a builder that stopped calling scan() is a different fact from a projects
        # directory that cannot be listed.
        self.last_scan_at: float | None = None
        self.last_scan_ok = False
        self.last_scan_note: str | None = None

    def snapshot(self) -> list["FileFacts"]:
        """The FileFacts to build from, as a list taken under the lock. Callers iterate
        this, never `files` - see the class docstring."""
        with self._lock:
            return list(self.files.values())

    def scan(self, now: float) -> None:
        cutoff = now - TRANSCRIPT_WINDOW_SEC
        try:
            projects = [p for p in self.projects_dir.iterdir() if p.is_dir()]
        except OSError as exc:
            self.last_scan_at, self.last_scan_ok = now, False
            self.last_scan_note = (f"{self.projects_dir} could not be listed "
                                   f"({type(exc).__name__})")
            return
        self.last_scan_at, self.last_scan_ok, self.last_scan_note = now, True, None
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
        self.last_at = 0.0                      # C3: when a hook last arrived
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
                # v0.35.0, INTERNAL - never served as itself. When a PreCompact hook last
                # arrived for this session; `compaction.inProgress` is derived from it
                # against the transcript's own clock. See note_precompact.
                "precompact_at": None,
                "events": []}

    def note_precompact(self, session_id: str, now: float) -> bool:
        """The PreCompact hook (v0.35.0). -> True when it was recorded.

        DELIBERATELY NOT a state transition. A compaction is the CLI reorganising its own
        context, not the session changing what it is doing: it moves no state, dates no
        `since`, writes no timeline event and touches no question. All it records is WHEN,
        because `compaction.inProgress` is "a PreCompact arrived and the transcript has
        not been written since" and there is no other evidence of the gap - the boundary
        record only appears once the compaction has finished.

        `at` is left alone for the same reason. It drives pruning and the served
        lastActivityAt, and a compaction on a session whose transcript has gone quiet is
        not the session becoming active again.

        The hook counters DO move: a PreCompact is a hook arriving, which is exactly what
        `sources.hooks` measures and what /v1/health counts.
        """
        if not session_id:
            return False
        with self._lock:
            self.count += 1
            self.last_at = now
            row = self.sessions.setdefault(session_id, self._blank(now))
            row["precompact_at"] = now
        return True

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
            self.last_at = now                  # C3: the hooks source's own clock
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


# ------------------------------------------------------------------------- limits

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


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


def read_limits_token(path: Path = None) -> str | None:
    """The long-lived usage token, or None. Read fresh on every call so a token stored
    while crabd runs is picked up on the next poll; the decrypted string is returned to
    the caller and dropped - LimitsReader keeps the same no-log, no-store rule for it
    that it keeps for the CLI token."""
    path = path or LIMITS_TOKEN_FILE
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    raw = _dpapi_unprotect(blob)
    if not raw:
        return None
    token = raw.decode("utf-8", errors="replace").strip()
    return token or None


class LimitsReader:
    """Claude OAuth usage endpoint, cached LIMITS_TTL_SEC.

    HARD RULE: the access token is read, used as a request header, and dropped. It
    is never logged, never stored, and never reaches the /v1/state payload - error
    text served to the widget is composed here, never taken from an exception that
    could echo a request.
    """

    def __init__(self, cache_file: Path | None = None) -> None:
        # Injectable for the same reason UserConfig takes a path: a test that builds a
        # real reader must not be one forgotten patch away from writing the operator's
        # live last-good store (it happened - see LIMITS_CACHE_MIN_EPOCH).
        self._cache_file = Path(cache_file) if cache_file else None
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

    def health(self, now: float) -> dict | None:
        """C3's `limitsToken` entry, or None before the first fetch - which is the
        contract's "a source crabd cannot judge is absent from the object". Read off
        this reader's own state; it never triggers a fetch, so putting it in a build
        costs nothing and cannot spend the endpoint's rate budget.
        """
        with self._lock:
            if not self._fetched_at:
                return None
            cached = self._cached or {}
            ok = bool(cached.get("available"))
            last_good = self._last_good_at or None
            note = cached.get("note")
            backoff = self._backoff_until > now
        if backoff:
            # M-02 (v0.35.0). While locked out, `_cached` is _aged()'s last-good, whose
            # note is the "limits as of 11:30 PM" CAVEAT - a qualification beside lit
            # gauges, not a diagnosis. Passing it through made the source entry say NOT
            # OK and then explain itself with a sentence that describes a healthy
            # reading, so the one place that names the lockout never got to. Measured on
            # the live companion 2026-09-22: limitsToken ok false, note "limits as of
            # 11:30 PM", lastAt 54 minutes old, with limits.available true beside it.
            note = LIMITS_BACKOFF_NOTE
        elif not ok and not note:
            note = "the usage endpoint did not answer"
        return {"ok": ok, "lastAt": last_good, "note": note,
                # A reading being SERVED from last-good while the endpoint is locked out
                # is still a source that is not currently answering.
                "backoff": backoff}

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
            raw = CREDENTIALS_FILE.read_text(encoding="utf-8")
        except OSError:
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
            fallback = read_limits_token()
            if fallback:
                token = fallback
                token_source = "sidecrab"
            elif not isinstance(token, str) or not token:
                return self._unavailable(
                    "no Claude access token - run claude in a terminal, or store a "
                    "long-lived one: Install-SideCrab.ps1 -LimitsToken")
            else:
                out = self._unavailable(
                    "Claude token expired - run claude in a terminal to refresh it, or "
                    "store a long-lived one: Install-SideCrab.ps1 -LimitsToken")
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
                note = ("SideCrab limits token rejected - mint a new one with "
                        "claude setup-token and re-run Install-SideCrab.ps1 -LimitsToken")
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

    def __init__(self, credentials_file: Path | None = None) -> None:
        # Injectable for the reason LimitsReader's cache_file is: a test that builds a
        # real catalog must not be one forgotten patch away from reading the operator's
        # live token and hitting the network.
        self._credentials_file = Path(credentials_file) if credentials_file else None
        self._lock = threading.Lock()
        self._windows: dict[str, int] | None = None
        self._fetched_at = 0.0
        self._not_before = 0.0

    @property
    def credentials_file(self) -> Path:
        return self._credentials_file or CREDENTIALS_FILE

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
        try:
            raw = self.credentials_file.read_text(encoding="utf-8")
        except OSError:
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
    telemetry (`OTEL_EXPORTER_OTLP_PROTOCOL=http/json`, endpoint 127.0.0.1:2722).

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
        self.last_at: float | None = None       # C3: when a document last arrived
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
            self.last_at = now
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
            self.last_at = now
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
    """`fleet` - SideCrab observing its own Scheduled Tasks (the notifier).

    Four outcomes, and the difference between the last two is the whole point:
      running  - schtasks reports Running
      stopped  - Ready / Queued / Disabled: the task exists and is not executing
      absent   - the query failed BECAUSE there is no such task
      unknown  - anything else: schtasks missing, timed out, an unrecognised status,
                 a non-zero exit with no not-found wording

    A task whose state cannot be read is never folded into `stopped`. "the notifier is
    not running" and "I could not find out" are different claims, and a widget dot that
    guesses the first when it means the second is exactly the silent-all-green failure
    the contract's stale rules exist to prevent.

    Cached FLEET_REFRESH_SEC and computed on its own thread for the same reason recap
    is: two subprocesses on the builder thread would freeze `generatedAt`.
    """

    def __init__(self, runner=None) -> None:
        self._runner = runner        # tests inject; production uses _run
        self._lock = threading.Lock()
        self._result = self.unknown()
        self._due = 0.0

    @staticmethod
    def unknown() -> dict:
        return {name: "unknown" for name, _task in FLEET_TASKS}

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
        return {name: self.status(task) for name, task in FLEET_TASKS}

    def status(self, task: str) -> str:
        runner = self._runner or self._run
        try:
            code, out, err = runner(task, FLEET_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            return "unknown"
        except (OSError, ValueError):    # schtasks missing, or the spawn failed
            return "unknown"
        if code != 0:
            blob = f"{out or ''}\n{err or ''}".lower()
            return "absent" if any(m in blob for m in FLEET_ABSENT_MARKERS) else "unknown"
        return FLEET_STATUS_MAP.get(self._status_field(out), "unknown")

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

    @staticmethod
    def _run(task: str, timeout: float):
        proc = subprocess.run(
            ["schtasks", "/query", "/tn", task, "/fo", "csv", "/nh"],
            capture_output=True, timeout=timeout, check=False,
            # No console under the Scheduled Task, and without this a window would
            # flash on the desktop on an interactive login.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (proc.returncode,
                proc.stdout.decode("utf-8", errors="replace"),
                proc.stderr.decode("utf-8", errors="replace"))


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
    """`host` - the machine's own CPU and memory, beside the HWiNFO sensors.

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
      - no counters at all (a platform with no `ctypes.windll`, or both calls failing)
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

    def __init__(self, times=None, memory=None) -> None:
        # Tests inject; production uses the two static readers below. Injection is by
        # CALLABLE rather than by patching ctypes because the FILETIME arithmetic - the
        # part with the trap in it - is what needs proving, and it is unreachable if the
        # test has to own a real kernel counter to get to it.
        self._times = times
        self._memory = memory
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
        reader = self._times or self._read_times
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
        reader = self._memory or self._read_memory
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

    @staticmethod
    def _read_times() -> tuple[int, int, int] | None:
        """GetSystemTimes -> (idle, kernel, user) in 100 ns ticks since boot, or None.

        `ctypes.windll` does not exist off Windows, so the AttributeError below is the
        platform gate as well as the error path - which is why the sandboxed test run
        serves no `host` key at all instead of failing.
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

    @staticmethod
    def _read_memory() -> tuple[int, int] | None:
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


# ---- lane A: HWiNFO shared memory, the NVIDIA GPU, machine load ----
#
# Three samplers, three threads, one additive home: they all land inside the v0.22.0
# `host` block. None of them runs in the request path and none of them can raise into
# build() - the same two rules HostSampler and FleetReader already keep, for the same
# reason: /v1/state is a dict dump, and a wedged sampler must show up as a stale
# `ageSec`/`sampledAt` rather than as a feed that stopped.

# Both namespaces, in this order. HWiNFO publishes into the GLOBAL kernel namespace
# when it runs elevated (which is how it reads most sensors) and into the session
# namespace otherwise; a bare name resolves to the caller's session. Opening either is
# unprivileged. OpenFileMapping - never CreateFileMapping, and never mmap's tagname,
# which CREATES the section when it is missing: a crabd that invented an empty
# `HWiNFO_SENS_SM2` would be a crabd that broke HWiNFO's own publish on the next launch.
HWINFO_MAP_NAMES = ("Global\\HWiNFO_SENS_SM2", "HWiNFO_SENS_SM2")
HWINFO_SIGNATURE = b"HWiS"
HWINFO_POLL_SEC = 5.0
# Beyond this the poll time is not a reading of now. HWiNFO's own sensor poll is ~2 s
# by default, so 30 s is roughly fifteen missed polls - and it is the same number the
# widget calls the whole feed stale at, which keeps one definition of "old" on the glass.
HWINFO_STALE_SEC = 30.0
HWINFO_SENSOR_CAP = 24
# The documented element sizes (SDK "HWiNFO_SHM"): a sensor element is
# 4 + 4 + 128 + 128, a reading element 4 + 4 + 4 + 128 + 128 + 16 + 4 doubles. They are
# the FLOOR for a sane header and nothing else - the stride always comes off the
# header's own dwSizeOf* fields, so a newer revision with bigger elements still parses.
HWINFO_SENSOR_ELEM_MIN = 264
HWINFO_READING_ELEM_MIN = 316
HWINFO_SENSOR_ELEM_MAX = 4096
HWINFO_READING_ELEM_MAX = 8192
HWINFO_MAX_SENSORS = 4096
HWINFO_MAX_READINGS = 65536
HWINFO_STR_LEN = 128
HWINFO_UNIT_LEN = 16
# Where the four doubles start inside a reading element. The char arrays before them
# end at 284, and MSVC's default 8-byte alignment then pushes `double Value` to 288 -
# so the two candidates are 288 (padded, what a stock build of the SDK header emits)
# and 284 (packed). Which one is live is DECIDED PER MAPPING by _hwinfo_value_offset
# rather than assumed: reading the doubles four bytes early turns every value into a
# denormal, which is exactly the kind of wrong number that looks like a reading.
HWINFO_VALUE_OFFSETS = (288, 284)
HWINFO_LABEL_OFF = 12
HWINFO_UNIT_OFF = 268
# THREE CAUSES, ALL OF THEM ORDINARY, and the note names all three because the reader
# cannot tell them apart from the outside - OpenFileMapping answers ERROR_FILE_NOT_FOUND
# for every one. Measured 2026-09-21: the section exists only while the SENSORS window is
# open (minimized counts); HWiNFO with its main window up and no sensors window publishes
# nothing at all, which is the cause an operator is least likely to guess.
HWINFO_NOTE_ABSENT = ("HWiNFO not running, its Sensors window closed, "
                      "or Shared Memory Support off")
HWINFO_NOTE_STALE = ("HWiNFO stopped publishing (free build 12-hour limit): "
                     "relaunch HWiNFO")
HWINFO_NOTE_UNREADABLE = "unreadable"

# ---- C3 / MF-008: the windows `sources` judges each feed against ----
# Every one of these was chosen by asking the §3.4 question - would it fire on a healthy
# night? - and the answer for all of them is no, because the event-driven sources are
# only judged while a session is actually running.
# No transcript has advanced in this long, so nothing is expected from the hooks, the
# status line or the telemetry exporter and their silence is correct.
SOURCE_IDLE_SEC = 900.0
# How long crabd must have been up before it will say the hooks are not arriving. Hook
# rows do not survive a restart, so a crabd restarted mid-turn legitimately holds none
# until that turn's Stop - and a turn can run a long time.
SOURCE_HOOK_GRACE_SEC = 900.0
# The CLI's OTLP exporter batches; this is many intervals, not one.
SOURCE_OTLP_FRESH_SEC = 900.0
# The transcript scan runs at the top of every build (2 s), so a scan this old means the
# builder thread itself stopped, not that the disk is slow.
SOURCE_SCAN_FRESH_SEC = 60.0
# Six HWiNFO poll intervals with no completed read: the sampler thread, not the mapping.
SOURCE_HWINFO_SAMPLER_SEC = HWINFO_POLL_SEC * 6
HWINFO_LOG_KEY = "host-hwinfo"

# SENSOR_READING_TYPE -> the widget's fixed vocabulary. 4 (current) and 8 (other) both
# land on "other": the row has no cell for amps, and inventing a word for one would be
# a vocabulary the widget has to grow to match.
HWINFO_KINDS = {0: "other", 1: "temp", 2: "volt", 3: "fan", 4: "other",
                5: "power", 6: "clock", 7: "usage", 8: "other"}

# CURATION, and the ORDER is the cap's policy: a mapping with more than 24 interesting
# readings loses the least interesting ones, never an arbitrary 24.
# Matched against the READING LABEL only, never the sensor (device) name. That is the
# whole reason the row is not 32 rows long on this machine: the per-core temperatures
# sit under a sensor called "CPU [#0]: ...", so a device-side match would sweep every
# one of them in as a CPU temperature.
HWINFO_PER_CORE_RE = re.compile(r"\bcore\s*#?\d", re.I)
# MATCH ORDER IS SPECIFICITY; the number beside each pattern is the SERVING rank, and
# the two orders are deliberately different. Measured on this machine 2026-09-21: the
# board's VRM probes are labelled "CPU VDDCR_VDD VRM (SVI3 TFN)" and carry both words,
# so a CPU-first walk files three VRM temperatures as CPU temperatures and the row
# shows no VRM at all.
HWINFO_TEMP_RANKS = (
    (2, re.compile(r"\bvrm\b|\bvsoc\b|\bmos\b|vcore\s*soc", re.I)),
    (3, re.compile(r"\bdrive\b|\bnvme\b|\bssd\b|\bhdd\b|\bdisk\b", re.I)),
    (4, re.compile(r"motherboard|chipset|\bpch\b|\bsystem\b|ambient", re.I)),
    (1, re.compile(r"\bgpu\b|\bvideo\b", re.I)),
    (0, re.compile(r"\bcpu\b|\btctl\b|\btdie\b|\bccd\d*\b|\bdie\b|package", re.I)),
)
# A TEMPERATURE HAS TO BE ONE, AND A LABEL HAS TO NAME SOMETHING. Four exclusions, each
# measured on this board 2026-09-21 rather than imagined:
#   - "Accumulated CPU Temperature" reads 194,119,626 °C and "Accumulated CPU Power"
#     135,046,525 W. They are counters wearing a temperature's name and unit; nine
#     digits in a cell built for two is not a reading anyone can use.
#   - "Temp9" is an unpopulated board header. A number with no subject is not a fact.
#   - the sixteen per-core temperatures, the two L3 cache temperatures and the eight
#     "GPU Memory A0..C1" rows are detail this row has no width for and the package
#     figures already stand for.
# The range gates are the backstop under the name gates, and they are applied only when
# the unit says which scale it is - an unrecognised unit is left alone rather than
# judged against a guess.
HWINFO_EXCLUDE_RE = re.compile(
    r"accumulated|^temp\d+$|\bl3\s*cache\b|\bmemory\s+[a-z]\d\b", re.I)
HWINFO_TEMP_RANGE_C = (-50.0, 150.0)
HWINFO_TEMP_RANGE_F = (-58.0, 302.0)
HWINFO_POWER_MAX_W = 5000.0
HWINFO_RANK_POWER = 5
HWINFO_RANK_FAN = 6
HWINFO_CPU_POWER_RE = re.compile(r"\bcpu\b", re.I)
HWINFO_PACKAGE_POWER_RE = re.compile(r"package|\bppt\b", re.I)

# nvidia-smi. The column list is fixed and ORDER IS THE CONTRACT - the answer carries no
# header, so a column added in the middle of this string silently renames every field
# after it. Measured on this machine 2026-09-21: one row,
# "NVIDIA GeForce RTX 5070, 610.74, 54, 2 %, 7114 MiB, 12227 MiB, 27.07 W, 250.00 W, 720 MHz".
NVIDIA_QUERY = ("name,driver_version,temperature.gpu,utilization.gpu,memory.used,"
                "memory.total,power.draw,power.limit,clocks.sm")
NVIDIA_NUMERIC_FIELDS = ("tempC", "utilPct", "memUsedMB", "memTotalMB",
                         "powerW", "powerLimitW", "clockMHz")
NVIDIA_FIELDS = ("name", "driver") + NVIDIA_NUMERIC_FIELDS
NVIDIA_POLL_SEC = 5.0
NVIDIA_TIMEOUT_SEC = 4.0
NVIDIA_NOTE_ABSENT = "nvidia-smi not found - no NVIDIA driver on this machine"
NVIDIA_NOTE_TIMEOUT = "nvidia-smi timed out"
NVIDIA_NOTE_EMPTY = "nvidia-smi returned no readable row"
NVIDIA_LOG_KEY = "host-gpu"
# The leading number of a field, units and all: "27.07 W" -> 27.07, "2 %" -> 2,
# "[N/A]" -> nothing. DELIBERATELY NOT locale-tolerant about the decimal mark: in this
# output a comma is the FIELD separator, so "27,07 W" is not a number with a comma in
# it, it is two columns - which is why the column count below is exact rather than a
# minimum. Accepting a comma decimal here would turn a shifted row into a plausible
# wrong reading instead of a refusal.
NVIDIA_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

LOAD_POLL_SEC = 5.0
LOAD_LOG_KEY = "host-load"
LOAD_COUNTERS = (("diskReadBps", "\\PhysicalDisk(_Total)\\Disk Read Bytes/sec"),
                 ("diskWriteBps", "\\PhysicalDisk(_Total)\\Disk Write Bytes/sec"))
LOAD_NET_COUNTERS = (("netRxBps", "\\Network Interface(*)\\Bytes Received/sec"),
                     ("netTxBps", "\\Network Interface(*)\\Bytes Sent/sec"))
# Instances that are NOT a wire. The sum is over physical adapters, and PDH's
# `Network Interface` set lists every pseudo-interface beside them - loopback, tunnels,
# and the virtual switch that carries the SAME bytes as the NIC underneath it, which is
# the one that would double-count rather than merely inflate.
LOAD_NET_PSEUDO = ("loopback", "isatap", "teredo", "pseudo", "tunnel", "vethernet",
                   "virtual", "miniport", "filter", "qos", "bluetooth", "vpn", "tap-")
PDH_FMT_DOUBLE = 0x00000200
PDH_MORE_DATA = 0x800007D2       # PDH_MORE_DATA, returned by the sizing call
# v0.34.0 (provisional), SCA-010. The two CStatus codes that mean "this reading is
# good". PDH_CSTATUS_NEW_DATA (1) is documented as valid CHANGED data and is what a
# rate counter can answer on a sample where the value moved; treating it as a failure
# threw away a successful reading, and on a wildcard array it silently under-summed
# the interfaces that reported it. Anything else - PDH_CSTATUS_INVALID_DATA on the
# first collect included - still serves null.
# https://learn.microsoft.com/en-us/windows/win32/perfctrs/checking-pdh-interface-return-values
PDH_CSTATUS_VALID_DATA = 0x00000000
PDH_CSTATUS_NEW_DATA = 0x00000001
PDH_SUCCESS_STATUSES = frozenset((PDH_CSTATUS_VALID_DATA, PDH_CSTATUS_NEW_DATA))
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


def _hwinfo_str(blob, offset: int, length: int) -> str:
    """A fixed char[] out of the mapping: latin-1, truncated at the first NUL.

    latin-1 rather than utf-8 because HWiNFO writes single-byte code-page text and
    "°C" arrives as one 0xB0 byte - which utf-8 refuses and utf-8-with-replacement
    turns into a question mark. latin-1 cannot fail and maps 0xB0 to the degree sign.
    """
    raw = bytes(blob[offset:offset + length])
    cut = raw.find(b"\x00")
    if cut >= 0:
        raw = raw[:cut]
    return raw.decode("latin-1", errors="replace").strip()


def _hwinfo_header(blob, size: int) -> dict | None:
    """The SHM2 header, or None when nothing in this blob is one.

    TWO CANDIDATE LAYOUTS, and the choice is made by validation rather than by belief.
    `__time64_t poll_time` follows three DWORDs, so a compiler with 8-byte alignment
    puts it at 16 and a packed one at 12 - and every offset after it moves with it.
    Picking the wrong one yields a poll time of about 1970 and section offsets that
    point outside the mapping, which is precisely what the checks below refuse.
    """
    if size < 44 or bytes(blob[0:4]) != HWINFO_SIGNATURE:
        return None
    for poll_off in (16, 12):
        if size < poll_off + 32:
            continue
        try:
            version, revision = struct.unpack_from("<II", blob, 4)
            poll_time = struct.unpack_from("<q", blob, poll_off)[0]
            (sensor_off, sensor_elem, sensor_n,
             reading_off, reading_elem, reading_n) = struct.unpack_from(
                "<6I", blob, poll_off + 8)
        except struct.error:
            continue
        header_end = poll_off + 32
        if not (HWINFO_SENSOR_ELEM_MIN <= sensor_elem <= HWINFO_SENSOR_ELEM_MAX):
            continue
        if not (HWINFO_READING_ELEM_MIN <= reading_elem <= HWINFO_READING_ELEM_MAX):
            continue
        if sensor_n > HWINFO_MAX_SENSORS or reading_n > HWINFO_MAX_READINGS:
            continue
        if sensor_off < header_end or reading_off < header_end:
            continue
        if sensor_off + sensor_elem * sensor_n > size:
            continue
        if reading_off + reading_elem * reading_n > size:
            continue
        if not (0 < poll_time < TS_MAX_EPOCH):
            continue
        return {"version": int(version), "revision": int(revision),
                "pollTime": int(poll_time), "pollOffset": poll_off,
                "sensorOff": int(sensor_off), "sensorElem": int(sensor_elem),
                "sensorCount": int(sensor_n), "readingOff": int(reading_off),
                "readingElem": int(reading_elem), "readingCount": int(reading_n)}
    return None


def _hwinfo_value_offset(blob, head: dict) -> int:
    """Where `double Value` starts inside a reading element, decided by measurement.

    The two candidates differ by the four padding bytes MSVC inserts after the 16-byte
    unit string, and the wrong one reads each double from four bytes of zero padding
    plus the low half of the real number - a denormal, or a value with no relation to
    its own min/max. So each candidate is SCORED over the first readings: all four
    doubles finite, and min <= value <= max, which is a relation the real layout holds
    and a shifted one cannot hold by accident more than occasionally. Ties go to the
    padded layout, which is what a stock compile of the SDK header produces.
    """
    best, best_score = HWINFO_VALUE_OFFSETS[0], -1
    probe = min(head["readingCount"], 64)
    for offset in HWINFO_VALUE_OFFSETS:
        if offset + 32 > head["readingElem"]:
            continue
        score = 0
        for i in range(probe):
            base = head["readingOff"] + i * head["readingElem"]
            try:
                value, low, high, _avg = struct.unpack_from("<4d", blob, base + offset)
            except struct.error:
                break
            if not (math.isfinite(value) and math.isfinite(low) and math.isfinite(high)):
                continue
            if low <= value <= high:
                score += 1
        if score > best_score:
            best, best_score = offset, score
    return best


def hwinfo_parse(blob, size: int | None = None) -> dict:
    """The whole mapping -> {'ok', 'note', 'pollTime', 'sensors'}. Never raises.

    `sensors` is the CURATED list the contract serves, already capped and ordered.
    """
    if blob is None:
        return {"ok": False, "note": HWINFO_NOTE_ABSENT, "pollTime": None, "sensors": []}
    if size is None:
        size = len(blob)
    head = _hwinfo_header(blob, size)
    if head is None:
        return {"ok": False, "note": HWINFO_NOTE_UNREADABLE, "pollTime": None,
                "sensors": []}
    devices = []
    for i in range(head["sensorCount"]):
        base = head["sensorOff"] + i * head["sensorElem"]
        original = _hwinfo_str(blob, base + 8, HWINFO_STR_LEN)
        user = _hwinfo_str(blob, base + 8 + HWINFO_STR_LEN, HWINFO_STR_LEN)
        devices.append(user or original)
    value_off = _hwinfo_value_offset(blob, head)
    readings = []
    for i in range(head["readingCount"]):
        base = head["readingOff"] + i * head["readingElem"]
        try:
            kind_id, sensor_index = struct.unpack_from("<II", blob, base)
            value = struct.unpack_from("<d", blob, base + value_off)[0]
        except struct.error:
            break
        original = _hwinfo_str(blob, base + HWINFO_LABEL_OFF, HWINFO_STR_LEN)
        user = _hwinfo_str(blob, base + HWINFO_LABEL_OFF + HWINFO_STR_LEN,
                           HWINFO_STR_LEN)
        kind = HWINFO_KINDS.get(int(kind_id), "other")
        readings.append({
            "name": user or original,
            "device": devices[sensor_index] if sensor_index < len(devices) else "",
            "kind": kind,
            "unit": _hwinfo_str(blob, base + HWINFO_UNIT_OFF, HWINFO_UNIT_LEN),
            "value": _hwinfo_round(kind, _finite_number(value)),
        })
    return {"ok": True, "note": None, "pollTime": head["pollTime"],
            "sensors": hwinfo_curate(readings)}


def _hwinfo_round(kind: str, value):
    """Sensor precision, not float precision. The mapping hands back a double carrying
    seventeen digits of a die temperature the silicon reports to about half a degree -
    serving `56.692291259765625` is false precision dressed as accuracy, and it is the
    same 1-dp rule the v0.22.0 members already keep. RPM and MHz are whole numbers."""
    if value is None:
        return None
    if kind in ("fan", "clock"):
        return int(round(value))
    return round(value, 3 if kind == "volt" else 1)


def _hwinfo_unit_scale(unit: str) -> str:
    """'C', 'F', or '' for a unit this code will not second-guess. The degree sign
    arrives as a single 0xB0 byte, so it is stripped rather than matched."""
    text = (unit or "").strip().lstrip("°").strip().upper()
    if text.startswith("C"):
        return "C"
    if text.startswith("F"):
        return "F"
    return ""


def _hwinfo_rank(reading: dict) -> int | None:
    """The curation policy, as one number. None = this reading is not served."""
    label = reading["name"]
    if not label or HWINFO_EXCLUDE_RE.search(label):
        return None
    value = reading["value"]
    if reading["kind"] == "fan":
        return HWINFO_RANK_FAN          # fans and pumps are one reading type
    if reading["kind"] == "power":
        if not (HWINFO_CPU_POWER_RE.search(label)
                and HWINFO_PACKAGE_POWER_RE.search(label)):
            return None
        return None if value is not None and value > HWINFO_POWER_MAX_W else HWINFO_RANK_POWER
    if reading["kind"] != "temp":
        return None
    if HWINFO_PER_CORE_RE.search(label):
        return None                     # per-core detail; the package figure stands for it
    scale = _hwinfo_unit_scale(reading["unit"])
    if scale and value is not None:
        low, high = HWINFO_TEMP_RANGE_C if scale == "C" else HWINFO_TEMP_RANGE_F
        if not (low <= value <= high):
            return None
    for rank, pattern in HWINFO_TEMP_RANKS:
        if pattern.search(label):
            return rank
    return None


def _hwinfo_within_rank(reading: dict) -> int:
    """The tiebreak inside one rank. 1 sorts after 0.

    THE INTEGRATED GPU ALSO REPORTS "GPU Temperature" (measured: sensor 13, the
    Radeon in the CPU package, beside sensor 14's discrete card). Both are real, and
    the one the operator means is the card - so the iGPU's rows go last inside the GPU
    rank rather than being dropped, and the cap trims them first.
    """
    return 1 if (reading["device"] or "").strip().lower().startswith("igpu") else 0


def hwinfo_curate(readings: list, cap: int = HWINFO_SENSOR_CAP) -> list:
    """Rank, then fill ROUND ROBIN to the cap, then order by rank for serving.

    THE CAP IS A POLICY, not a slice, and the live mapping is what proved it: 37 of
    this machine's 524 readings pass the rank test, of which 10 are CPU temperatures
    and 10 are GPU temperatures. A flat "sort by rank and take 24" served those twenty
    plus four more and dropped every fan, every drive and the CPU package power -
    which is to say the cap silently deleted whole kinds rather than depth within a
    kind. Filling one reading per rank per pass gives every kind that exists a place,
    and trims the deepest entries of the largest kinds, which is the only thing a cap
    can take away without changing what the row is about.
    """
    buckets: dict[int, list] = {}
    for index, reading in enumerate(readings):
        rank = _hwinfo_rank(reading)
        if rank is not None:
            buckets.setdefault(rank, []).append((_hwinfo_within_rank(reading), index,
                                                 reading))
    for rows in buckets.values():
        rows.sort(key=lambda row: (row[0], row[1]))
    ranks = sorted(buckets)
    chosen: list = []
    depth = 0
    while len(chosen) < cap and any(len(buckets[r]) > depth for r in ranks):
        for rank in ranks:
            if len(chosen) >= cap:
                break
            if len(buckets[rank]) > depth:
                inner, index, reading = buckets[rank][depth]
                chosen.append((rank, inner, index, reading))
        depth += 1
    chosen.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3] for row in chosen]


class HwinfoReader:
    """`host.sensors` + `host.sensorsSource` - HWiNFO's shared memory, read-only.

    THE FREE BUILD STOPS PUBLISHING TWELVE HOURS AFTER LAUNCH and leaves the mapping in
    place with its last poll time frozen in it. That is why `stale` is derived from the
    poll time and not from "did the read succeed": a reader that only checked for the
    mapping would go on serving half-day-old temperatures as current, which is the one
    failure this whole block exists to refuse.

    `opener` is injected by tests and returns (bytes-like, size) or None for "no
    mapping". Production opens the section itself; nothing here writes to it.
    """

    def __init__(self, opener=None) -> None:
        self._opener = opener
        self._lock = threading.Lock()
        self._sensors: list = []
        self._source = self.unavailable(HWINFO_NOTE_ABSENT)
        # SCA-009: the reading's OWN clock, kept so get() can age it at consumption
        # time, and the heartbeat that says when this reader last reached a verdict.
        self._poll_epoch: float | None = None
        self._read_at: float | None = None
        self._due = 0.0

    @staticmethod
    def unavailable(note: str) -> dict:
        return {"provider": "hwinfo", "pollTime": None, "ageSec": None,
                "stale": False, "available": False, "note": note}

    def get(self, now: float | None = None) -> tuple[list, dict]:
        """The cached reading, AGED WHERE IT IS SERVED (SCA-009).

        The sampler thread and the builder thread are independent, and when the sampler
        stops advancing - the thread dies, the section stops being readable, HWiNFO's
        free build hits its twelve-hour limit mid-run - the `ageSec` and `stale` it
        computed at read time freeze with it. The audit built 90 s later and got a new
        generatedAt beside ageSec 0.0 and stale false: the exact independent-worker
        failure the contract says must produce a growing age. Age is a property of the
        READING, not of when the reading was taken, so it is derived from the stored
        poll time on every serve. No hardware is touched here - this is arithmetic.
        """
        if now is None:
            now = time.time()
        with self._lock:
            sensors, source = list(self._sensors), dict(self._source)
            poll_epoch = self._poll_epoch
        # None is every unavailable path: there is no reading to age, and inventing one
        # would be the zero-for-absent the honesty rule forbids.
        if poll_epoch is None:
            return sensors, source
        age = round(max(0.0, now - poll_epoch), 1)
        stale = age > HWINFO_STALE_SEC
        source["ageSec"] = age
        source["stale"] = stale
        source["note"] = HWINFO_NOTE_STALE if stale else None
        return sensors, source

    def heartbeat(self) -> float | None:
        """When read() last reached a verdict, or None before the first one. This is the
        SAMPLER's liveness, which is a different question from the reading's age: a
        sampler that stopped running and a sensor section that stopped being written
        both freeze `pollTime`, and only this tells them apart."""
        with self._lock:
            return self._read_at

    def _remember(self, poll_epoch, now: float) -> None:
        """Called by read() on every path, OUTSIDE the caller's lock (poll() releases
        before it reads). `_lock` is not reentrant, so this must never be reached from
        inside a critical section."""
        with self._lock:
            self._poll_epoch = poll_epoch
            self._read_at = now

    def poll(self, now: float) -> bool:
        with self._lock:
            if now < self._due:
                return False
            self._due = now + HWINFO_POLL_SEC
        sensors, source = self.read(now)
        with self._lock:
            self._sensors, self._source = sensors, source
        return True

    def read(self, now: float) -> tuple[list, dict]:
        opener = self._opener or self._open_mapping
        try:
            opened = opener()
        except Exception as exc:
            _log_once(HWINFO_LOG_KEY,
                      f"crabd: HWiNFO shared memory raised {type(exc).__name__}; "
                      f"serving no sensors")
            self._remember(None, now)
            return [], self.unavailable(HWINFO_NOTE_UNREADABLE)
        if opened is None:
            self._remember(None, now)
            return [], self.unavailable(HWINFO_NOTE_ABSENT)
        try:
            blob, size = opened
            parsed = hwinfo_parse(blob, size)
        except Exception as exc:
            _log_once(HWINFO_LOG_KEY,
                      f"crabd: HWiNFO shared memory parse raised "
                      f"{type(exc).__name__}; serving no sensors")
            self._remember(None, now)
            return [], self.unavailable(HWINFO_NOTE_UNREADABLE)
        if not parsed["ok"]:
            self._remember(None, now)
            return [], self.unavailable(parsed["note"] or HWINFO_NOTE_UNREADABLE)
        # A poll time in the future is a clock that moved, not a reading from ahead;
        # clamped to 0 so `ageSec` stays a duration rather than going negative.
        age = round(max(0.0, now - parsed["pollTime"]), 1)
        stale = age > HWINFO_STALE_SEC
        self._remember(parsed["pollTime"], now)
        return parsed["sensors"], {
            "provider": "hwinfo",
            "pollTime": _utc_iso(parsed["pollTime"]),
            "ageSec": age,
            "stale": stale,
            "available": True,
            "note": HWINFO_NOTE_STALE if stale else None,
        }

    @staticmethod
    def _open_mapping():
        """(snapshot bytes, size) for the live section, or None when there is none.

        A COPY, taken with one string_at, rather than a view held open across the
        parse: HWiNFO rewrites the section in place on its own poll, and parsing a
        moving buffer would mix two polls' readings into one served list.
        """
        try:
            kernel32 = ctypes.windll.kernel32
        except AttributeError:          # not Windows - the platform gate, as HostSampler's is
            return None
        kernel32.OpenFileMappingW.restype = ctypes.c_void_p
        kernel32.OpenFileMappingW.argtypes = [ctypes.c_uint32, ctypes.c_int,
                                              ctypes.c_wchar_p]
        kernel32.MapViewOfFile.restype = ctypes.c_void_p
        kernel32.MapViewOfFile.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                           ctypes.c_uint32, ctypes.c_uint32,
                                           ctypes.c_size_t]
        kernel32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        for name in HWINFO_MAP_NAMES:
            handle = kernel32.OpenFileMappingW(0x0004, False, name)   # FILE_MAP_READ
            if not handle:
                continue
            address = None
            try:
                address = kernel32.MapViewOfFile(handle, 0x0004, 0, 0, 0)
                if not address:
                    continue
                size = _virtual_query_size(address)
                if not size:
                    continue
                return (ctypes.string_at(address, size), size)
            finally:
                if address:
                    kernel32.UnmapViewOfFile(ctypes.c_void_p(address))
                kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None


class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
    """VirtualQuery's out-parameter. Only RegionSize is read: MapViewOfFile with a
    length of 0 maps the whole section without saying how big it was, and reading past
    it is an access violation rather than a Python exception."""
    _fields_ = [("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", ctypes.c_uint32),
                ("PartitionId", ctypes.c_uint16),
                ("_pad", ctypes.c_uint16),
                ("RegionSize", ctypes.c_size_t),
                ("State", ctypes.c_uint32),
                ("Protect", ctypes.c_uint32),
                ("Type", ctypes.c_uint32),
                ("_pad2", ctypes.c_uint32)]


def _virtual_query_size(address: int) -> int:
    try:
        kernel32 = ctypes.windll.kernel32
    except AttributeError:
        return 0
    info = _MEMORY_BASIC_INFORMATION()
    written = kernel32.VirtualQuery(ctypes.c_void_p(address), ctypes.byref(info),
                                    ctypes.sizeof(info))
    if not written:
        return 0
    return int(info.RegionSize)


def _lane_a_bps(value) -> int | None:
    """Bytes per second as a whole number. PDH answers with a double carrying fifteen
    digits of a figure that is a rate over a 5 s window; sub-byte precision on it is
    noise dressed as measurement. Negative is not a throughput, so it is None."""
    value = _finite_number(value)
    if value is None or value < 0:
        return None
    return int(round(value))


def _nvidia_number(text: str) -> float | None:
    """One nvidia-smi cell as a number. '[N/A]', '', and anything unparseable -> None."""
    if text is None:
        return None
    match = NVIDIA_NUM_RE.search(str(text))
    if not match:
        return None
    try:
        return _finite_number(float(match.group(0)))
    except ValueError:
        return None


def nvidia_parse(stdout: str) -> dict:
    """A --format=csv,noheader answer -> the served `host.gpu`, minus freshness.

    csv rather than a split, for the reason FleetReader parses schtasks with it: a
    field may be quoted, and a naive split on ', ' breaks the first time a GPU name
    contains a comma.
    """
    rows = []
    try:
        for row in csv.reader((stdout or "").splitlines()):
            if row and any(cell.strip() for cell in row):
                rows.append(row)
    except (csv.Error, ValueError):
        rows = []
    # EXACTLY nine columns. Fewer means a column vanished, MORE means one was split -
    # and both shift every field after the break, which is how a memory figure ends up
    # in the power slot reading like a plausible number.
    if not rows or len(rows[0]) != len(NVIDIA_FIELDS):
        block = {field: None for field in NVIDIA_FIELDS}
        block.update({"available": False, "note": NVIDIA_NOTE_EMPTY})
        return block
    cells = [cell.strip() for cell in rows[0]]
    block = {"name": cells[0] or None, "driver": cells[1] or None}
    for index, field in enumerate(NVIDIA_NUMERIC_FIELDS, start=2):
        block[field] = _nvidia_number(cells[index])
    block.update({"available": True, "note": None})
    return block


class GpuReader:
    """`host.gpu` - nvidia-smi, on its own thread.

    A SUBPROCESS, so it is never on the request path and never on the builder's: the
    same rule recap and fleet keep. `available: false` is served rather than the block
    being dropped, because "this machine has no NVIDIA GPU" is an answer and a missing
    key is not - the widget would have to tell it apart from an older crabd.
    """

    def __init__(self, runner=None) -> None:
        self._runner = runner
        self._lock = threading.Lock()
        self._result = self.unavailable(NVIDIA_NOTE_ABSENT)
        self._due = 0.0

    @staticmethod
    def unavailable(note: str) -> dict:
        block = {field: None for field in NVIDIA_FIELDS}
        block.update({"available": False, "note": note, "sampledAt": None})
        return block

    def get(self) -> dict:
        with self._lock:
            return dict(self._result)

    def poll(self, now: float) -> bool:
        with self._lock:
            if now < self._due:
                return False
            self._due = now + NVIDIA_POLL_SEC
        result = self.read(now)
        with self._lock:
            self._result = result
        return True

    def read(self, now: float) -> dict:
        runner = self._runner or self._run
        try:
            code, out, _err = runner(NVIDIA_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            return self.unavailable(NVIDIA_NOTE_TIMEOUT)
        except (OSError, ValueError):   # nvidia-smi absent, or the spawn failed
            return self.unavailable(NVIDIA_NOTE_ABSENT)
        except Exception as exc:
            _log_once(NVIDIA_LOG_KEY,
                      f"crabd: nvidia-smi raised {type(exc).__name__}; serving no GPU")
            return self.unavailable(NVIDIA_NOTE_EMPTY)
        if code != 0:
            return self.unavailable(NVIDIA_NOTE_EMPTY)
        block = nvidia_parse(out)
        # Its OWN freshness, not the document's: this sampler runs on a 5 s cadence
        # against a 2 s build, so `generatedAt` would date the figure two polls young.
        block["sampledAt"] = _utc_iso(now) if block["available"] else None
        return block

    @staticmethod
    def _run(timeout: float):
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader"],
            capture_output=True, timeout=timeout, check=False,
            # Same reason FleetReader passes it: no console under the Scheduled Task,
            # and a window would flash on the desktop on an interactive login.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (proc.returncode,
                proc.stdout.decode("utf-8", errors="replace"),
                proc.stderr.decode("utf-8", errors="replace"))


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    """PDH_FMT_COUNTERVALUE. The padding field is DECLARED rather than left to ctypes:
    the C type is a DWORD followed by a union whose widest member is 8 bytes, so the
    double sits at offset 8 on every build - and an implicit alignment that differed
    would read the counter out of the CStatus word."""
    _fields_ = [("CStatus", ctypes.c_uint32),
                ("_pad", ctypes.c_uint32),
                ("doubleValue", ctypes.c_double)]


class _PDH_FMT_COUNTERVALUE_ITEM_W(ctypes.Structure):
    _fields_ = [("szName", ctypes.c_wchar_p),
                ("FmtValue", _PDH_FMT_COUNTERVALUE)]


class _PROCESSENTRY32W(ctypes.Structure):
    """Toolhelp's process record. dwSize MUST be set before Process32FirstW or the call
    fails - the same versioned-struct rule _MEMORYSTATUSEX carries."""
    _fields_ = [("dwSize", ctypes.c_uint32),
                ("cntUsage", ctypes.c_uint32),
                ("th32ProcessID", ctypes.c_uint32),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.c_uint32),
                ("cntThreads", ctypes.c_uint32),
                ("th32ParentProcessID", ctypes.c_uint32),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_uint32),
                ("szExeFile", ctypes.c_wchar * 260)]


class PdhRates:
    """The two disk counters and the two network counter ARRAYS, in one long-lived query.

    THE FIRST COLLECT OF A RATE COUNTER HAS NO RATE, and PDH says so with
    PDH_CSTATUS_INVALID_DATA rather than with a zero. That is the same shape as
    HostSampler's first `cpuPct`, and it gets the same answer: null, never 0.0. The
    query is held OPEN between polls so each later collect measures against the
    previous one - re-opening per poll would make every sample a first sample.
    """

    def __init__(self) -> None:
        self._pdh = None
        self._query = None
        self._counters: list[tuple[str, ctypes.c_void_p]] = []
        self._net: list[tuple[str, ctypes.c_void_p]] = []
        self._opened = False

    def _open(self) -> bool:
        if self._opened:
            return self._query is not None
        self._opened = True
        try:
            pdh = ctypes.WinDLL("pdh")
        except (AttributeError, OSError, FileNotFoundError):
            _log_once(LOAD_LOG_KEY, "crabd: pdh.dll unavailable; serving no host load")
            return False
        pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                              ctypes.c_size_t,
                                              ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        pdh.PdhGetFormattedCounterValue.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(_PDH_FMT_COUNTERVALUE)]
        pdh.PdhGetFormattedCounterArrayW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        # PDH_STATUS IS UNSIGNED, and ctypes' default c_int restype makes the two
        # statuses this code branches on arrive NEGATIVE - PDH_MORE_DATA (0x800007D2)
        # as -2147481646, which never equals the constant and silently turned every
        # network figure into a null. Measured on this machine 2026-09-21.
        for func in (pdh.PdhOpenQueryW, pdh.PdhAddEnglishCounterW,
                     pdh.PdhCollectQueryData, pdh.PdhGetFormattedCounterValue,
                     pdh.PdhGetFormattedCounterArrayW):
            func.restype = ctypes.c_uint32
        query = ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
            _log_once(LOAD_LOG_KEY, "crabd: PdhOpenQueryW failed; serving no host load")
            return False
        self._pdh, self._query = pdh, query
        for key, path in LOAD_COUNTERS:
            handle = ctypes.c_void_p()
            if pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(handle)) == 0:
                self._counters.append((key, handle))
        for key, path in LOAD_NET_COUNTERS:
            handle = ctypes.c_void_p()
            if pdh.PdhAddEnglishCounterW(query, path, 0, ctypes.byref(handle)) == 0:
                self._net.append((key, handle))
        # NO BASELINE COLLECT HERE, deliberately. Collecting twice back to back would
        # produce a rate measured over a sub-millisecond window and serve it as a
        # reading - the trap HostSampler documents for GetSystemTimes. The first
        # sample() collect is answered PDH_CSTATUS_INVALID_DATA by PDH itself, which
        # becomes a null; the second measures across a real 5 s window.
        return True

    def sample(self) -> dict:
        blank = {key: None for key, _ in LOAD_COUNTERS + LOAD_NET_COUNTERS}
        if not self._open():
            return blank
        pdh, query = self._pdh, self._query
        if pdh.PdhCollectQueryData(query) != 0:
            return blank
        out = dict(blank)
        for key, handle in self._counters:
            value = _PDH_FMT_COUNTERVALUE()
            status = pdh.PdhGetFormattedCounterValue(handle, PDH_FMT_DOUBLE, None,
                                                     ctypes.byref(value))
            if status == 0 and value.CStatus in PDH_SUCCESS_STATUSES:
                out[key] = _lane_a_bps(value.doubleValue)
        for key, handle in self._net:
            out[key] = self._sum_instances(handle)
        return out

    def _sum_instances(self, handle) -> float | None:
        """Every non-pseudo instance of a wildcard counter, added up. None when PDH
        could not answer at all - an empty sum would be a measured zero."""
        pdh = self._pdh
        size = ctypes.c_uint32(0)
        count = ctypes.c_uint32(0)
        status = pdh.PdhGetFormattedCounterArrayW(handle, PDH_FMT_DOUBLE,
                                                  ctypes.byref(size),
                                                  ctypes.byref(count), None)
        if status != PDH_MORE_DATA or size.value == 0 or count.value == 0:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        status = pdh.PdhGetFormattedCounterArrayW(handle, PDH_FMT_DOUBLE,
                                                  ctypes.byref(size),
                                                  ctypes.byref(count), buffer)
        if status != 0:
            return None
        items = ctypes.cast(
            buffer, ctypes.POINTER(_PDH_FMT_COUNTERVALUE_ITEM_W * count.value)).contents
        total = 0.0
        seen = False
        for item in items:
            name = (item.szName or "").lower()
            if not name or name == "_total":
                continue
            if any(marker in name for marker in LOAD_NET_PSEUDO):
                continue
            if item.FmtValue.CStatus not in PDH_SUCCESS_STATUSES:
                continue
            value = _finite_number(item.FmtValue.doubleValue)
            if value is None:
                continue
            total += value
            seen = True
        return _lane_a_bps(total) if seen else None


class LoadReader:
    """`host.load` - whole-machine disk and network throughput, commit, and the busiest
    process. ctypes and stdlib only, on its own thread, 5 s.

    EVERY MEMBER IS INDEPENDENT. PDH missing does not take the commit figure with it,
    and a snapshot that cannot be taken does not blank the throughput - each failure
    serves its own null, which is the three-tier rule HostSampler already keeps one
    block up.
    """

    def __init__(self, rates=None, processes=None, commit=None, cpu_count=None) -> None:
        self._rates = rates if rates is not None else PdhRates()
        self._processes = processes
        self._commit = commit
        self._cpus = cpu_count or (os.cpu_count() or 1)
        self._lock = threading.Lock()
        self._result = self.blank()
        self._due = 0.0
        self._prev_times: dict | None = None
        self._prev_at: float | None = None

    @staticmethod
    def blank() -> dict:
        return {"diskReadBps": None, "diskWriteBps": None, "netRxBps": None,
                "netTxBps": None, "commitPct": None, "topProcess": None,
                "sampledAt": None}

    def get(self) -> dict:
        with self._lock:
            result = dict(self._result)
        if isinstance(result.get("topProcess"), dict):
            result["topProcess"] = dict(result["topProcess"])
        return result

    def poll(self, now: float) -> bool:
        with self._lock:
            if now < self._due:
                return False
            self._due = now + LOAD_POLL_SEC
        result = self.read(now)
        with self._lock:
            self._result = result
        return True

    def read(self, now: float) -> dict:
        out = self.blank()
        try:
            out.update(self._rates.sample())
        except Exception as exc:
            _log_once(LOAD_LOG_KEY, f"crabd: PDH sample raised {type(exc).__name__}; "
                                    f"serving no throughput")
        out["commitPct"] = self._commit_pct()
        out["topProcess"] = self._top_process(now)
        out["sampledAt"] = _utc_iso(now)
        return out

    def _commit_pct(self) -> float | None:
        """Committed bytes as a percentage of the commit limit, from the SAME
        GlobalMemoryStatusEx call the v0.22.0 sampler uses - one syscall, no counter
        subscription, and no first-sample hole because it is not a rate."""
        reader = self._commit or self._read_commit
        try:
            reading = reader()
        except Exception:
            return None
        if reading is None:
            return None
        try:
            total, avail = reading
        except (TypeError, ValueError):
            return None
        total = _finite_number(total)
        avail = _finite_number(avail)
        if total is None or total <= 0 or avail is None or avail < 0:
            return None
        return _pct(100.0 * (total - min(avail, total)) / total)

    @staticmethod
    def _read_commit() -> tuple[int, int] | None:
        status = _MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        try:
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        except (AttributeError, OSError, ValueError):
            return None
        if not ok:
            return None
        return (int(status.ullTotalPageFile), int(status.ullAvailPageFile))

    def _top_process(self, now: float) -> dict | None:
        """The busiest process over the interval since the last sample, or None.

        CPU TIME IS CUMULATIVE, exactly as GetSystemTimes is, so this is a delta and
        the first sample has no answer - null, never a process at 0%. Keyed on
        (pid, creation time) because Windows reuses pids: without the creation time a
        short-lived process could hand its baseline to an unrelated one and produce a
        fabricated spike.
        """
        reader = self._processes or self._read_process_times
        try:
            current = reader()
        except Exception as exc:
            _log_once(LOAD_LOG_KEY, f"crabd: process snapshot raised "
                                    f"{type(exc).__name__}; serving no top process")
            return None
        if not current:
            return None
        previous, previous_at = self._prev_times, self._prev_at
        self._prev_times, self._prev_at = current, now
        if not previous or previous_at is None:
            return None
        elapsed = now - previous_at
        if elapsed <= 0:
            return None
        window = elapsed * 1e7 * self._cpus      # 100 ns ticks of whole-machine capacity
        best = None
        for key, (name, ticks) in current.items():
            was = previous.get(key)
            if was is None:
                continue                          # started since the last sample
            delta = ticks - was[1]
            if delta < 0:
                continue
            pct = _pct(100.0 * delta / window)
            if pct is None:
                continue
            if best is None or pct > best["cpuPct"]:
                best = {"name": name, "pid": key[0], "cpuPct": pct}
        return best

    @staticmethod
    def _read_process_times() -> dict:
        """{(pid, created): (name, kernel+user ticks)} for every process we may open.

        Processes that refuse PROCESS_QUERY_LIMITED_INFORMATION are skipped rather
        than counted as zero: a protected process is one this reader cannot measure,
        and "0%" would be a claim about it.
        """
        try:
            kernel32 = ctypes.windll.kernel32
        except AttributeError:
            return {}
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == INVALID_HANDLE_VALUE:
            return {}
        out: dict = {}
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        try:
            more = kernel32.Process32FirstW(ctypes.c_void_p(snapshot),
                                            ctypes.byref(entry))
            while more:
                pid = int(entry.th32ProcessID)
                name = str(entry.szExeFile)
                # pid 0 is the Idle process: it is the machine NOT working, and the
                # contract's topProcess is the one that is.
                if pid and name.lower() not in ("idle", "system idle process"):
                    handle = kernel32.OpenProcess(
                        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                    if handle:
                        created, kernel_t, user_t = _FILETIME(), _FILETIME(), _FILETIME()
                        exited = _FILETIME()
                        ok = kernel32.GetProcessTimes(
                            ctypes.c_void_p(handle), ctypes.byref(created),
                            ctypes.byref(exited), ctypes.byref(kernel_t),
                            ctypes.byref(user_t))
                        kernel32.CloseHandle(ctypes.c_void_p(handle))
                        if ok:
                            out[(pid, _filetime(created))] = (
                                name, _filetime(kernel_t) + _filetime(user_t))
                more = kernel32.Process32NextW(ctypes.c_void_p(snapshot),
                                               ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(snapshot))
        return out

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
        # MF-002. Two records the cancel path needs, and neither is served:
        #   _claimed   - a Stop hook has taken this prompt and is answering with it. The
        #                send has not returned yet, so the item is still in _queued
        #                (CRB-F5 keeps it there until the answer is on the socket), but
        #                it can no longer be cancelled.
        #   _delivered - the last prompt actually sent, so a cancel that lost the race
        #                says "already delivered, at this time" instead of the "nothing
        #                queued" that reads as though the tap never happened.
        self._claimed: dict[str, tuple[str, float]] = {}
        self._delivered: dict[str, tuple[str, float]] = {}

    def queue(self, session_id: str, prompt: str, now: float) -> None:
        with self._lock:
            self._queued[session_id] = (prompt, now)
            # A new tap is a new bet: whatever was claimed or delivered before is not
            # what this item is, and leaving either behind would let a cancel of THIS
            # prompt be answered with the previous one's delivery.
            self._claimed.pop(session_id, None)
            self._delivered.pop(session_id, None)

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

    def claim(self, session_id: str, now: float) -> str | None:
        """Peek, and mark the item as SPOKEN FOR (MF-002). The Stop hook calls this
        instead of peek: from here the prompt is on its way into the answer, and a
        cancel arriving afterwards has lost - it must say so rather than delete an item
        that is about to be delivered anyway and report a success that never happened.

        The claim is released by release() when the send fails, and consumed by
        drain_if() when it succeeds. Both of those pair with this call by prompt text,
        the same comparison drain_if already relies on.
        """
        with self._lock:
            entry = self._queued.get(session_id)
            if entry is None or now - entry[1] > CONTINUE_TTL_SEC:
                return None
            self._claimed[session_id] = (entry[0], now)
            return entry[0]

    def release(self, session_id: str, prompt: str) -> None:
        """Undo a claim whose send never reached the socket. CRB-F5 keeps the prompt for
        the next Stop, so the cancel path must stop calling it delivered."""
        with self._lock:
            claimed = self._claimed.get(session_id)
            if claimed is not None and claimed[0] == prompt:
                del self._claimed[session_id]

    def cancel(self, session_id: str, now: float) -> tuple[str, str | None, float]:
        """-> (verdict, prompt, at). MF-002.

        "cancelled" - a live queued item was removed; `prompt` is what it said.
        "delivered" - a Stop hook already has it; `at` is when it was taken. A CLAIMED
                      item counts as delivered: the answer is being written as this
                      runs, and there is no point at which crabd could take it back.
        "nothing"   - nothing queued, nothing recently delivered. An EXPIRED item lands
                      here and is deleted: the card stopped showing it at the ten-minute
                      mark, so it is not what the operator is cancelling.
        """
        with self._lock:
            claimed = self._claimed.get(session_id)
            if claimed is not None:
                return "delivered", claimed[0], claimed[1]
            entry = self._queued.pop(session_id, None)
            if entry is not None and now - entry[1] <= CONTINUE_TTL_SEC:
                return "cancelled", entry[0], entry[1]
            sent = self._delivered.get(session_id)
            if sent is not None and now - sent[1] <= CONTINUE_TTL_SEC:
                return "delivered", sent[0], sent[1]
            return "nothing", None, 0.0

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
            claimed = self._claimed.get(session_id)
            if claimed is not None and claimed[0] == prompt:
                del self._claimed[session_id]
            # The delivery record, for a cancel that arrives after this (MF-002).
            self._delivered[session_id] = (prompt, now)
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
            # Every one of the three ages out on the same window (MF-002): past it a
            # delivery is history the operator cannot be cancelling, and a claim that
            # old belongs to a Stop hook that is long gone.
            for store in (self._queued, self._claimed, self._delivered):
                for sid in [s for s, (_, at) in store.items()
                            if now - at > CONTINUE_TTL_SEC]:
                    del store[sid]


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
        # C4 / MF-017: the readiness probe's attempt ring and the last time a code
        # matched. IN MEMORY ONLY - a restart returns to unverified, because what was
        # verified was a panel this process can no longer see.
        self._probes: list[float] = []
        self._verified_at = 0.0

    @classmethod
    def load_or_create(cls, path: Path) -> "PanelToken":
        """Read the code off disk, minting one when the file is missing or unusable.
        Atomic write (tmp + os.replace) so a crash mid-write cannot leave a truncated
        code that the next start would silently replace with a different one.

        IT NEVER RAISES (SCA-005). main() calls this before the listener exists and
        outside every try/except crabd has, so an exception here does not cost the
        operator their approvals - it costs them the companion, the panel and the feed
        with it, on a machine where approvals are very likely disabled anyway. The two
        ways it used to raise are both closed: the file is read as BYTES, because a text
        read of an encoding-corrupted or UTF-16 pairing file raises UnicodeDecodeError
        and the OSError catch never covered it; and a mint that cannot be written returns
        a token with NO code rather than propagating. No code is fail-closed - verify()
        can then answer only missing or rejected, never ok - which is the posture the
        whole class exists to keep.
        """
        code = None
        try:
            # errors="replace" rather than a second decode attempt: the code is ten
            # symbols of [0-9A-HJ-NP-TV-Z], so any byte sequence that is not that file
            # fails FORMAT below whatever it decodes to, and guessing an encoding would
            # only invent a code the operator never saw.
            code = cls.normalize(path.read_bytes().decode("utf-8", "replace"))
        except OSError:
            code = None
        if code and cls.FORMAT.match(code):
            return cls(path, code)
        cls._quarantine(path)
        code = cls.generate()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(cls.display(code) + "\n", encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)     # a no-op on Windows; the profile ACL does the job
            except OSError:
                pass
            os.replace(tmp, path)
        except OSError as exc:
            print(f"crabd: the panel pairing file {path} could not be written "
                  f"({type(exc).__name__}); panel approvals are unavailable until it "
                  f"can be - everything else is serving normally",
                  file=sys.stderr, flush=True)
            return cls(path, None)
        return cls(path, code)

    @staticmethod
    def _quarantine(path: Path) -> None:
        """Keep an unusable pairing file beside itself before minting over it.

        A MISSING file is the ordinary first start and moves nothing. A file that exists
        but cannot be used is the operator's - they may have written the code down, and
        a silent overwrite would make "the code on my desk does not work" unanswerable.
        One quarantine name, deliberately: a second unusable file replaces the first
        rather than growing a pile of them in ~/.sidecrab.
        """
        keep = path.with_name(path.name + PANEL_TOKEN_UNUSABLE_SUFFIX)
        try:
            # is_file, not exists: a `panel-token` that is somehow a DIRECTORY must not
            # be renamed as though it were the operator's code.
            if not path.is_file():
                return
            os.replace(path, keep)
        except OSError:
            return              # an unwritable directory; the mint reports it instead
        print(f"crabd: the panel pairing file was unusable and has been kept as "
              f"{keep.name}; a new code was minted - pair the panel again",
              file=sys.stderr, flush=True)

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

    @property
    def verified_at(self) -> float | None:
        with self._lock:
            return self._verified_at or None

    def verify_code(self, presented, now: float) -> str:
        """The READINESS probe (C4): does this code match, deciding nothing.

        -> "ok" | "rejected" | "rate-limited". It has no path to the permission broker
        at all, so it cannot allow or deny; the worst a caller who reaches it can learn
        is whether a code they already hold is the right one, five times a minute.

        A match records `verified_at` and is what turns readiness from unverified into
        ready. It deliberately does NOT clear the decide lockout: the two budgets are
        independent, so neither route can spend or clear the other's.
        """
        with self._lock:
            self._probes = [t for t in self._probes
                            if now - t < PANEL_TOKEN_PROBE_WINDOW_SEC]
            if len(self._probes) >= PANEL_TOKEN_PROBE_MAX:
                return "rate-limited"
            self._probes.append(now)
            if self._code is not None and isinstance(presented, str) and \
                    hmac.compare_digest(self.normalize(presented), self._code):
                self._verified_at = now
                return "ok"
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

    WHY IT EXISTS. The panel renders on the Xeneon Edge, a surface no devtools can
    attach to, so `console.log` has nowhere to go. The open question is which input
    events actually reach the glass: a TAP is proven (panel approvals were verified live
    on 2026-08-27), while swipe, long-press and multi-touch are unknown. The only way to
    find out is for the page to say what it saw, over the same loopback port everything
    else already rides, and for a maintainer to read it.

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

    def record(self, origin, user_agent, now: float, source_hint=None) -> None:
        """origin is the raw header value (str) or None for an absent header; user_agent
        likewise. Total by construction: it is called on every GET and POST before the
        origin gate, so it must never raise into the request path. The (origin, source)
        pair is the key - source classifies the caller (browser/local/none) so the widget
        is separable from other no-Origin local processes. `source_hint` (v0.31.0) is
        "panel" for a request the served panel made - its same-origin GETs carry no Origin
        and would otherwise fold into whichever browser UA polled last."""
        origin_key = origin if isinstance(origin, str) else ORIGIN_ABSENT
        source = source_hint if isinstance(source_hint, str) and source_hint else \
            _classify_ua_source(user_agent)
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
                 models: "ModelCatalog | None" = None,
                 # ---- lane A: the three host samplers ----
                 # OPTIONAL and default None, like the v0.12.0 readers and deliberately
                 # UNLIKE HostSampler: each one owns a thread and a live resource (a
                 # kernel section, a subprocess, an open PDH query), so a
                 # default-constructed reader would put every unit test one forgotten
                 # patch away from opening them. Absent, the member is simply not served.
                 hwinfo: "HwinfoReader | None" = None,
                 gpu: "GpuReader | None" = None,
                 load: "LoadReader | None" = None) -> None:
        self.store = store
        self.hwinfo = hwinfo
        self.gpu = gpu
        self.load = load
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

    def session_project(self, session_id: str) -> tuple[str | None, str | None]:
        """lane D (v0.33.0): the (repo, cwd) crabd SERVED for this row, or (None, None).

        Read off the served document rather than re-derived from GitLookup on purpose:
        the per-session whitelist must be built from the same `repo` the widget drew the
        buttons from. A second derivation could answer differently mid-poll (the cache
        is 30 s and a branch switch or a cwd that briefly failed to read moves it), and
        the operator would see a button that 400s.

        (None, None) for an unknown id, which lands on the global whitelist - the
        existing `serving` gate below still answers 404 for it.
        """
        for row in (self.state or {}).get("sessions", []):
            if row["id"] == session_id:
                return row.get("repo"), row.get("cwd")
        return None, None

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

    def record_precompact(self, payload) -> None:
        """POST /v1/hook/precompact -> the tracker (v0.35.0). Total by construction: a
        payload with no session id is dropped, exactly as record_hook's is."""
        if not isinstance(payload, dict):
            return
        session_id = _session_id(payload)
        if session_id:
            self.hooks.note_precompact(session_id, time.time())

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
                # SCA-001 (P1): ONE main file owns this session's identity, picked by a
                # deterministic latest rule, and cwd, model, title and the title's
                # provenance all come from THAT file. A session id can own a main
                # transcript under two project directories - its cwd moved - and the
                # enumeration order over them is arbitrary, so last-writer-wins served
                # whichever the scan happened to reach last. The audit got the OLD
                # project's cwd, repo and title beside the NEW project's newest
                # transcript and live hook; and since v0.33.0 that cwd is what the
                # per-project continue allowlist is keyed on, so the old project's
                # prompts were accepted and the current project's refused with a 400.
                # The chain is TOTAL - newest record, then mtime, then path - because a
                # partial order would let two files trade places between passes and the
                # served cwd flip on alternate polls.
                rank = (facts.last_ts, facts.mtime, str(facts.path))
                if rank > row["main_rank"]:
                    row["main_rank"] = rank
                    row["main_ts"] = facts.last_ts
                    row["title"] = facts.title()
                    row["title_source"] = facts.title_source()
                    row["cwd"] = facts.last_cwd
                    row["model"] = facts.last_model
                    row["speed"] = facts.last_speed
                    row["question"] = facts.question
                    row["question_ts"] = facts.question_ts
                    # v0.35.0. These four are IDENTITY facts for SCA-001's reason: each
                    # describes what the session is doing NOW, and the identity file is
                    # the one that owns "now". A session whose cwd moved has a stale main
                    # file in the old project, and taking its mode or its last tool would
                    # be the same wrong-project answer the P1 was about.
                    row["mode"] = facts.mode
                    row["turn_tool"] = facts.turn_tool
                    row["turn_tool_calls"] = facts.turn_tool_calls
                    row["queue_depth"] = facts.queue_depth
                    row["todos"] = facts.todos
                # AGGREGATED across every main file, deliberately and unchanged: labels,
                # usage records, the context figure and the turn clock are facts about
                # the SESSION, not about which file currently owns its identity.
                # .labels()/.usage_records() hand back COPIES taken under the file's own
                # lock. Iterating the live dicts raced refresh() on another thread - the
                # half of CRB-F2 the store lock never covered (FileFacts.__init__).
                row["agent_labels"].update(facts.labels())
                # v0.35.0, AGGREGATED for the reason the line above is: which files this
                # session has edited and how often it has compacted are facts about the
                # SESSION, and a session that moved project did both halves of them. The
                # merge keeps the NEWER touch of a path so `recent` orders by last write
                # across both files.
                for touched, at in facts.files_touched.items():
                    if at >= row["files_touched"].get(touched, 0.0):
                        row["files_touched"][touched] = at
                row["compactions"] += facts.compactions
                row["compaction_ts"] = max(row["compaction_ts"], facts.compaction_ts)
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
            hook_cwd = row.get("cwd")
            # SCA-001: joined when the hook is AUTHORITATIVE - either no main transcript
            # gave a cwd, or the hook is more recent than the record the metadata came
            # from. A session that moved has its live hook in the new project while a
            # stale main file still sits in the old one, and the allowlist follows this
            # cwd. On an ordinary session the hook agrees with the transcript and this
            # writes the same string back.
            if hook_cwd and (entry["cwd"] is None
                             or row.get("at", 0.0) > entry["main_ts"]):
                entry["cwd"] = hook_cwd

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
        # C3: the newest TRANSCRIPT activity, which is evidence a session is running
        # that does not come from the hooks - so it can be used to judge them.
        last_activity = max((row["mtime"] for row in per_session.values()), default=0.0)
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
            # v0.34.0 (provisional) adds readiness and verifiedAt - see _approvals_block.
            "approvals": self._approvals_block(now),
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
        # ---- lane A: sensors / gpu / load join the same block ----
        host = self._lane_a_host(host, now)
        if host is not None:
            document["host"] = host
        # C3 / MF-008. Built from `host` rather than by re-reading the samplers, so the
        # verdict and the reading it judges can never disagree. Last, because it reads
        # what everything above produced.
        sources = self._sources_block(now, host, last_activity, limits)
        if sources:
            document["sources"] = sources
        return document

    @staticmethod
    def _source_entry(now: float, at: float | None, ok: bool, note: str | None) -> dict:
        """One `sources` member from an epoch. Absent stays absent: a source that has
        never produced carries lastAt null and ageSec null rather than a zero."""
        return {"ok": bool(ok),
                "lastAt": _utc_iso(at) if at else None,
                "ageSec": round(max(0.0, now - at), 1) if at else None,
                "note": note if not ok else None}

    def _sources_block(self, now: float, host: dict | None,
                       last_activity: float, limits: dict | None = None) -> dict:
        """`sources` - one freshness verdict per feed (C3 / MF-008).

        A fresh overall document can sit on top of a source that stopped: the builder
        runs every two seconds whatever the hooks, the status line or the sensors are
        doing, and until now `host.sensorsSource` was the only thing that said so.

        TWO RULES, and both are about not crying wolf:

          - a source crabd CANNOT JUDGE is absent from the object - never a false ok and
            never a false failure. The status line and the OTLP exporter are optional
            wiring an operator may simply not have done, and "never seen" does not tell
            that apart from "broken", so they appear only once they have spoken.
          - a source that is quiet because there is NOTHING TO REPORT is ok. Hooks, the
            status line and telemetry are event-driven: on a night with nobody working,
            silence is the correct reading, and a panel that goes amber every night is a
            panel nobody looks at. `last_activity` is the newest transcript mtime, which
            is evidence of a live session that does NOT come from the hooks, so it can
            be used to judge them.

        Replayed against the live companion before shipping - up 2 h, 7 hooks, no status
        line, no telemetry, HWiNFO publishing - this block reports hooks ok, transcripts
        ok, limitsToken ok, hwinfo ok, gpu as nvidia-smi found it, and no statusline or
        otlp key at all. Nothing in it fires on a healthy night.
        """
        sources: dict[str, dict] = {}
        expecting = bool(last_activity) and now - last_activity <= SOURCE_IDLE_SEC

        hooks = getattr(self, "hooks", None)
        if hooks is not None:
            last = getattr(hooks, "last_at", 0.0) or None
            settled = now - self.started_at >= SOURCE_HOOK_GRACE_SEC
            ok = bool(last) or not (settled and expecting)
            sources["hooks"] = self._source_entry(
                now, last, ok,
                "a session is active but no hook has arrived - check the SideCrab "
                "hooks block in settings.json")

        last_scan = getattr(self.store, "last_scan_at", None)
        if last_scan is not None:
            ok = (bool(getattr(self.store, "last_scan_ok", False))
                  and now - last_scan <= SOURCE_SCAN_FRESH_SEC)
            sources["transcripts"] = self._source_entry(
                now, last_scan, ok,
                getattr(self.store, "last_scan_note", None)
                or "the transcript scan has not run recently")

        last = getattr(self.statusline, "last_at", None) if self.statusline else None
        if last:
            ok = now - last <= STATUSLINE_PREFER_SEC or not expecting
            sources["statusline"] = self._source_entry(
                now, last, ok,
                "the status line has stopped posting; limits fall back to the usage "
                "endpoint")

        # M-01 (v0.35.0). The entry is OMITTED while the status line is serving `limits`.
        # _limits_block returns before LimitsReader.get() is reached in that case, and
        # get() is the reader's ONLY caller - so the reader stops being polled and
        # health() freezes on whatever the last OAuth fetch said. The panel then showed a
        # permanently-failed source, with an ageSec that grew forever, about a feed the
        # served document was not using and no action could fix. Measured on the live
        # companion 2026-09-22: limits.available true beside limitsToken ok false with
        # the note "SideCrab limits token rejected" and a lastAt 15 minutes old.
        #
        # Absent is this block's own answer for a source it cannot judge, and `statusline`
        # above is the entry that judges what IS serving the gauges.
        health = getattr(self.limits, "health", None)
        limits_health = health(now) if callable(health) else None
        serving = limits.get("source") if isinstance(limits, dict) else None
        if limits_health is not None and serving != LIMITS_SOURCE_STATUSLINE:
            ok = limits_health["ok"] and not limits_health["backoff"]
            # health() guarantees a note whenever this entry will be not-ok (either the
            # lockout note or the endpoint's own), so there is no fallback here: one that
            # could not fire would be a control that reports success forever.
            sources["limitsToken"] = self._source_entry(
                now, limits_health["lastAt"], ok, limits_health["note"])

        last = getattr(self.otlp, "last_at", None) if self.otlp else None
        if last:
            ok = now - last <= SOURCE_OTLP_FRESH_SEC or not expecting
            sources["otlp"] = self._source_entry(
                now, last, ok, "the telemetry exporter has stopped sending")

        block = host or {}
        sensors_source = block.get("sensorsSource")
        if isinstance(sensors_source, dict):
            ok = (bool(sensors_source.get("available"))
                  and not sensors_source.get("stale"))
            note = sensors_source.get("note")
            beat = self.hwinfo.heartbeat() if self.hwinfo is not None else None
            # The sampler's own liveness, which pollTime cannot show: a thread that died
            # and a section that stopped being written both freeze the reading.
            if ok and beat is not None and now - beat > SOURCE_HWINFO_SAMPLER_SEC:
                ok, note = False, "the HWiNFO sampler has stopped polling"
            sources["hwinfo"] = {
                "ok": ok,
                "lastAt": sensors_source.get("pollTime"),
                "ageSec": sensors_source.get("ageSec"),
                "note": note if not ok else None}

        gpu = block.get("gpu")
        if isinstance(gpu, dict):
            ok = bool(gpu.get("available"))
            at = _parse_ts(gpu.get("sampledAt"))
            sources["gpu"] = self._source_entry(
                now, at, ok, gpu.get("note") or "nvidia-smi did not answer")
        return sources

    def _approvals_block(self, now: float) -> dict:
        """`approvals` - whether a tap MAY decide, and whether this panel CAN (C4).

        The host's `hasToken` says a token file exists on the host side. That is not the
        same claim as "it matches crabd" or "approvals are enabled", and until now the
        only way to tell the difference was to send a real decide and have it refused -
        a probe with a side effect. These four values are the four distinct answers, and
        none of them reveals the code:

          off        - approvals are disabled; no tap can decide anything;
          no-token   - enabled, but crabd holds no pairing code at all (an unwritable
                       ~/.sidecrab, or a file it quarantined and could not replace);
          unverified - a code exists and nothing has yet proved this panel has it;
          ready      - a code was verified in THIS process.

        `verifiedAt` stays as it is under `off`: it is a fact about what happened, not a
        second copy of the enable flag.
        """
        enabled = self.config.panel_approvals(now)
        gate = getattr(self, "panel_token", None)
        verified_at = gate.verified_at if gate is not None else None
        if not enabled:
            readiness = "off"
        elif gate is None or not gate.status(now)["present"]:
            readiness = "no-token"
        elif verified_at is None:
            readiness = "unverified"
        else:
            readiness = "ready"
        return {"enabled": enabled, "tokenRequired": True, "readiness": readiness,
                "verifiedAt": _utc_iso(verified_at) if verified_at else None}

    # ---- lane A: the additive host members ----
    def _lane_a_host(self, host: dict | None, now: float) -> dict | None:
        """`host` with sensors / sensorsSource / gpu / load folded in.

        IT MAY CREATE THE BLOCK. A machine whose GetSystemTimes and
        GlobalMemoryStatusEx both fail can still have a readable GPU or a readable
        HWiNFO mapping, and dropping a measurement that was taken because an
        unrelated counter was not is a second failure invented from the first. Every
        member here is presence-detected on its own, so a block carrying only
        `gpu` is a shape the widget already handles.
        """
        extra: dict = {}
        if self.hwinfo is not None:
            # `now` is passed rather than left to the reader's own clock (SCA-009): the
            # served age must be measured from the same instant as generatedAt.
            sensors, source = self.hwinfo.get(now)
            extra["sensors"] = sensors
            extra["sensorsSource"] = source
        if self.gpu is not None:
            extra["gpu"] = self.gpu.get()
        if self.load is not None:
            extra["load"] = self.load.get()
        if not extra:
            return host
        if host is None:
            host = {}
        host.update(extra)
        return host

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
                # SCA-001: which main file won the identity block, and its newest
                # record's timestamp. Internal to build() - neither is served.
                "main_rank": (0.0, 0.0, ""), "main_ts": 0.0,
                # v0.19.0: newest completed model round-trip in the MAIN transcript.
                # Subagent files never reach it - a background subagent finishing is not
                # the operator answering, and folding its records in here would clear a
                # question that is genuinely still standing. 0.0 = no usage record yet,
                # which fails safe (note_activity is only ever called on a truthy value).
                "turn_ts": 0.0,
                # ---- v0.35.0. Internal; _lane_m_session_extras turns these into the
                # served members, and every one of them is ABSENT rather than zero.
                "mode": None, "turn_tool": None, "turn_tool_calls": 0,
                "files_touched": {}, "queue_depth": 0,
                "compactions": 0, "compaction_ts": 0.0, "todos": None}

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
                # lane D: this session's project prompts, or the key is ABSENT.
                **self._lane_d_session_extras(repo, cwd, now),
                # v0.35.0: the six additive members, each present only when it has
                # something to say.
                **self._lane_m_session_extras(info, hook, state),
            })
        rows.sort(key=lambda r: (order.get(r["state"], 9), -_parse_ts(r["lastActivityAt"])))
        return rows

    def _lane_d_session_extras(self, repo, cwd, now: float) -> dict:
        """`sessions[].continuePrompts` (v0.33.0, provisional), or nothing at all.

        ABSENT when this session has no project prompts, never `[]`: the empty list is
        what the TOP-LEVEL key serves, where always-present is the contract, and a
        per-session `[]` would be a claim that crabd looked and found the project
        configured with nothing. Presence is the widget's feature detection here.
        """
        extras = self.config.continue_session_extras(now, repo, cwd)
        return {"continuePrompts": extras} if extras else {}

    @staticmethod
    def _lane_m_session_extras(info: dict, hook, state: str) -> dict:
        """The six v0.35.0 members, or nothing at all. Schema stays 5: every one of them
        is presence-detected by the widget, which is why none of them may be served as a
        zero, an empty list or a null.

        THE HONESTY RULE, once, for all six: a zero here is a CLAIM. "0 files touched",
        "0 todos", "queue 0" and "never compacted" are things crabd would be asserting
        about a session it may simply not have parsed yet, and a panel that renders them
        looks equally confident when it knows and when it does not. Absent renders as
        nothing, which is the honest picture of nothing known.
        """
        out: dict = {}
        mode = info.get("mode")
        if mode:
            out["mode"] = mode
        # `activity` is the CURRENT turn's newest tool, so it is served only while the
        # session is working. On a card that has finished or gone quiet the last tool of
        # the last turn is history, and rendering it beside `done` would read as a tool
        # still running. Not cleared in the parser - the transcript is the record of what
        # happened and re-reading it must give the same answer.
        tool = info.get("turn_tool")
        if state == "working" and tool:
            out["activity"] = {"tool": tool["tool"], "detail": tool["detail"],
                               "at": _utc_iso(tool["at"]) if tool["at"] else None,
                               "callsThisTurn": info.get("turn_tool_calls", 0)}
        touched = info.get("files_touched") or {}
        if touched:
            recent = sorted(touched.items(), key=lambda kv: -kv[1])[:FILES_RECENT_CAP]
            # NEWEST FIRST, and leaves only (_path_leaf). A duplicate leaf from two
            # directories is kept as two entries: they are two files, and de-duplicating
            # the display would under-count what the panel is showing.
            out["filesTouched"] = {
                "count": len(touched),
                "recent": [leaf for leaf in (_path_leaf(p) for p, _ in recent) if leaf]}
        # Floored at zero: crabd can start reading a transcript mid-session and meet a
        # dequeue whose enqueue it never saw. A negative depth is arithmetic, not a queue.
        depth = max(0, info.get("queue_depth", 0))
        if depth:
            out["promptQueue"] = depth
        precompact = (hook or {}).get("precompact_at")
        # IN PROGRESS = a PreCompact hook arrived and the transcript has not been written
        # since. The two clocks are comparable (both are crabd's own wall clock: the hook
        # at receipt, the file from its mtime), and the compaction's own boundary record
        # is what ends it - writing the file is exactly the event being waited for.
        in_progress = bool(precompact and precompact > info.get("mtime", 0.0))
        count = info.get("compactions", 0)
        if count or in_progress:
            at = info.get("compaction_ts", 0.0)
            out["compaction"] = {"count": count,
                                 "lastAt": _utc_iso(at) if at else None,
                                 "inProgress": in_progress}
        todos = info.get("todos")
        if todos:
            out["todos"] = dict(todos)
        return out

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
    # A reflected Origin string - since v0.34.0 only this server's own origin, which is
    # what the panel page sends - lets that page read the response; None emits no ACAO
    # header at all, which is a non-browser client that needs none, or a refused
    # cross-site page that must not be handed one.
    #
    # "*" IS NO LONGER A LEGAL VALUE ANYWHERE (SEC-4, v0.16.0). It was the read
    # endpoints' setting until the audit pointed out what /v1/state actually contains:
    # cwds, session titles, the FULL question text and pendingPermission. With ACAO:*
    # any page the operator merely visited could read all of it cross-origin. The class
    # DEFAULT is now None, so a code path that forgets to set it fails closed (an
    # unreadable reply) instead of open.
    _acao: str | None = None

    def _send(self, code: int, body: bytes | None, ctype: str = "application/json") -> None:
        self.send_response(code)
        if body is None:
            self.send_header("Content-Length", "0")
        else:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
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
        if not self._host_allowed():
            self._send(421, HOST_NOT_ALLOWED)
            return
        # No preflight, on ANY path, is answered with ACAO:* any more (SEC-1 for the
        # mutating paths, SEC-4 for the reads): that header is what invites the
        # cross-origin read. A real web page's preflight gets no ACAO at all, so its
        # application/json request dies at the preflight; the widget's opaque origin is
        # reflected so its own preflight still passes.
        acao = self._preflight_acao(self.headers.get("Origin"))
        self.send_response(204)
        if acao is not None:
            self.send_header("Access-Control-Allow-Origin", acao)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _preflight_acao(self, origin) -> str | None:
        """The Access-Control-Allow-Origin for a preflight, PATH-INDEPENDENT since
        v0.16.0: reads and writes are gated alike, so the answer depends only on the
        Origin. A real web page is refused with no ACAO; the widget's opaque origin is
        reflected so its application/json preflight still succeeds - never "*"."""
        if self._refused_origin(origin):
            return None
        return origin if origin else None

    # ------------------------------------------------------- v0.31.0 the two new gates

    def _bound_port(self) -> int:
        """The port THIS server bound - read off the socket, never the PORT constant, so
        a test instance on an ephemeral port accepts its own names and not 2722's."""
        try:
            return int(self.server.server_address[1])
        except Exception:   # noqa: BLE001 - a handler without a server (a bare double)
            return PORT

    def _own_names(self) -> frozenset:
        port = self._bound_port()
        return frozenset(f"{name}:{port}" for name in PANEL_ALLOWED_HOSTNAMES)

    def _host_allowed(self) -> bool:
        """The DNS-rebinding gate. True only for a Host that names this socket: one of
        PANEL_ALLOWED_HOSTNAMES with the BOUND port, compared lower-cased. An absent Host
        is refused too - every real client of crabd (curl, the CLI's http hooks, urllib,
        PowerShell, every browser) sends one, and a request without it is not one of them."""
        host = self.headers.get("Host")
        if not isinstance(host, str):
            return False
        return host.strip().lower() in self._own_names()

    def _refused_origin(self, origin) -> bool:
        """The origin gate from v0.34.0 (provisional), CLEAN-04. TWO cases are allowed
        and everything else is refused:

          - NO Origin header at all. Every native client of crabd lands here: the CLI's
            Stop and PermissionRequest hooks, the status line command, the notifier, the
            setup scripts and curl. Measured on the live companion - `originsSeen` holds
            only `<absent>` pairs. This is not authentication and was never claimed to
            be; the pairing code and the Host allowlist are the gates that are.
          - EXACTLY one of this server's own origins, `http://127.0.0.1:<bound port>` or
            `http://localhost:<bound port>`, which is what the crabd-served panel page
            sends on its own POSTs. Nothing wider: another port, an https scheme, a
            subdomain trick (`127.0.0.1.evil.example`) or a trailing path are not this
            server's origin.

        WHAT THIS RETIRED, and why it was safe to: `null` and non-web schemes (`file:`,
        `qrc:`) used to be allowed, because the widget ran from a vendor-served file/qrc
        page inside QtWebEngine, whose cross-origin fetch serializes its Origin to
        exactly "null" - and a widget that cannot POST is a broken product. That page is
        gone with the vendor host (CLEAN-05); the panel is served by crabd itself and sends a real
        same-origin header. `null` is also the one origin a sandboxed allow-scripts
        iframe on any page the operator visits can FORGE, which is the SEC-a residual -
        so retiring the allowance closes the forged-null vector rather than merely
        bounding it with the pairing code.

        An Origin header that is PRESENT but empty is treated as absent: no browser
        emits one, so it cannot be the CSRF vector this gate exists for.

        A DNS-rebinding page never reaches here with an allowed value: its Host was
        refused first.
        """
        if not isinstance(origin, str):
            return False
        o = origin.strip().lower()
        if not o:
            return False
        if not o.startswith("http://"):
            return True
        return o[len("http://"):] not in self._own_names()

    def _do_panel(self, path: str) -> None:
        """GET /panel/... - the widget tree, served from a fixed allowlist (v0.31.0).

        `path` is the request path with the `/panel/` prefix already stripped and an
        empty string meaning the index. The lookup is a dict membership test on the
        decoded path; the filesystem path comes from the allowlist VALUE, never from the
        request, so there is no join for `..` to climb. Every answer carries
        `Content-Security-Policy: frame-ancestors 'none'`: the panel carries Approve and
        Deny, and a page that could frame it could click-jack a tap.
        """
        if path in ("", "index.html"):
            key = "index.html"
        else:
            key = path
        entry = PANEL_FILES.get(key)
        if entry is None:
            self._send(404, b'{"error":"not found"}')
            return
        rel, ctype = entry
        target = PANEL_DIR / rel
        try:
            body = target.read_bytes()
        except OSError:
            # The tree is missing (a companion-only checkout) or unreadable. 404 with a
            # named reason, never a 500: the panel host renders its own "not served" page
            # off this status.
            self._send(404, PANEL_NOT_AVAILABLE)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _record_origin(self, origin) -> None:
        """Feed the diagnostic origin recorder (ORIGIN-REC), before the origin gate so
        REFUSED web origins are counted too. Defensive: never let a diagnostic write
        break a request - a builder without a recorder (an old test double) is a no-op."""
        builder = getattr(self, "builder", None)
        recorder = getattr(builder, "origins", None) if builder else None
        if recorder is not None:
            try:
                # User-Agent classifies the SOURCE (browser/local/none) so no-Origin
                # callers are separable. ATTACKER-CONTROLLED and DIAGNOSTIC ONLY - it
                # never reaches the origin gate below. Absent header -> None -> "none".
                user_agent = self.headers.get("User-Agent")
                recorder.record(origin, user_agent, time.time(),
                                source_hint=self._panel_source_hint(origin))
            except Exception:   # noqa: BLE001 - a diagnostic must never fail a request
                pass

    def _panel_source_hint(self, origin):
        """"panel" when the request came from the crabd-served panel page (v0.31.0): its
        POSTs carry this server's own Origin, and its same-origin GETs carry no Origin
        but a Referer under /panel/. DIAGNOSTIC ONLY, like everything the recorder holds -
        the Referer is attacker-controlled and never reaches a gate."""
        names = self._own_names()
        if isinstance(origin, str) and origin.strip().lower()[len("http://"):] in names \
                and origin.strip().lower().startswith("http://"):
            return "panel"
        referer = self.headers.get("Referer")
        if isinstance(referer, str):
            ref = referer.strip().lower()
            for name in names:
                if ref.startswith(f"http://{name}/panel"):
                    return "panel"
        return None

    def do_GET(self):
        # SEC-4 (v0.16.0). The reads are gated exactly like the writes. /v1/state serves
        # cwds, session titles, the full text of the question a session is waiting on and
        # pendingPermission; under the old ACAO:* any page the operator visited could
        # read the lot cross-origin. Same predicate as the mutating gate, same 403 body:
        # a present http(s) Origin is a real visited page and is refused; absent, "null"
        # and non-web origins (the QtWebEngine widget, curl, local tools) are allowed and
        # get their own origin reflected back.
        # v0.31.0: the Host gate runs FIRST, before the recorder - a rebinding page's
        # request is not evidence of anything the recorder exists to measure.
        if not self._host_allowed():
            self._acao = None
            self._send(421, HOST_NOT_ALLOWED)
            return
        origin = self.headers.get("Origin")
        self._record_origin(origin)
        if self._refused_origin(origin):
            self._acao = None
            self._send(403, CROSS_SITE_REFUSED)
            return
        self._acao = origin if origin else None
        split = urllib.parse.urlsplit(self.path)
        path = split.path.rstrip("/") or "/"
        try:
            if path == "/panel" or split.path.startswith("/panel/"):
                # The bare /panel must redirect: the page's relative links (styles/,
                # scripts/) resolve against the directory, so served without the slash
                # they would resolve against /. Decoded once, so `%2e%2e` is `..` and
                # misses the allowlist like any other unknown key.
                if split.path == "/panel":
                    self.send_response(301)
                    self.send_header("Location", "/panel/")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self._do_panel(urllib.parse.unquote(split.path[len("/panel/"):]))
            elif path == "/v1/health":
                self._send(200, dump_state(self._health()))
            elif path == "/v1/state":
                self._do_state()
            elif path == "/v1/events":      # lane B: the push transport, gated above
                self._do_events()
            elif path == "/v1/history":
                self._do_history(split.query)
            elif path == "/v1/panel-log":
                self._do_panel_log_read()
            else:
                self._send(404, b'{"error":"not found"}')
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

    # ---- lane B: server-sent events ----

    def _do_events(self) -> None:
        """GET /v1/events - every NEW snapshot, pushed, as `text/event-stream`.

        The gates ran in do_GET, so a refused Host never reaches here and a refused
        Origin never reaches here: this route has no gate of its own to drift from
        theirs. What it adds is the subscriber cap, and the cap is the FIRST thing,
        before any header goes out - a 503 with a Content-Length is a clean answer a
        browser can read, and half an event-stream is not.
        """
        server = getattr(self, "server", None)
        slots = getattr(server, "sse_slots", None) or _SSE_FALLBACK_SLOTS
        stop = getattr(server, "sse_stop", None) or _SSE_FALLBACK_STOP
        if not slots.acquire():
            self._send(503, SSE_TOO_MANY)
            return
        try:
            self._stream_events(stop)
        finally:
            slots.release()

    def _stream_events(self, stop: threading.Event) -> None:
        # No Content-Length and no chunking: the body ends when the connection does,
        # which is what every SSE client expects and what keeps the framing honest if
        # crabd is killed mid-stream. close_connection stops BaseHTTPRequestHandler
        # trying to read a second request off a socket that is now a one-way stream.
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        # Nothing proxies loopback today, but a buffering intermediary is the single
        # failure mode that turns a push transport into a slower poll with no error.
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        acao = self._acao
        if acao is not None:
            self.send_header("Access-Control-Allow-Origin", acao)
            self.send_header("Vary", "Origin")
        self.end_headers()

        # The reconnection time the client should use if it reconnects on its own.
        if not self._sse_write(b"retry: %d\n\n" % SSE_RETRY_MS):
            return

        last_state = None
        last_gen = None
        state = self._sse_snapshot()
        if state is None:
            # Cold start. The honest answer is the one /v1/state gives, as an `error`
            # event, and then the stream WAITS: the first snapshot is seconds away and
            # closing here would send the client into its backoff for no reason.
            if not self._sse_write(sse_frame("error", STATE_NOT_BUILT)):
                return
        else:
            if not self._sse_write(sse_frame("state", dump_state(state))):
                return
            last_state, last_gen = state, state.get("generatedAt")

        next_ping = time.monotonic() + SSE_PING_SEC
        while not stop.is_set():
            # stop.wait IS the sleep, so a shutdown ends the loop within one interval
            # rather than one ping. Without it server_close() blocks on this thread for
            # up to 15 s and a test's teardown looks like a hang.
            if stop.wait(SSE_POLL_SEC):
                return
            if self._sse_peer_gone():
                return
            state = self._sse_snapshot()
            if state is not None and (state is not last_state
                                      or state.get("generatedAt") != last_gen):
                # IDENTITY OR generatedAt, not generatedAt alone: _utc_iso has one-second
                # resolution, so two builds inside the same second carry the same string
                # and a string-only test would drop the second one.
                if not self._sse_write(sse_frame("state", dump_state(state))):
                    return
                last_state, last_gen = state, state.get("generatedAt")
                next_ping = time.monotonic() + SSE_PING_SEC
                continue
            if time.monotonic() >= next_ping:
                if not self._sse_write(sse_frame("ping", b"{}")):
                    return
                next_ping = time.monotonic() + SSE_PING_SEC

    def _sse_peer_gone(self) -> bool:
        """True when the reader has closed its end.

        The loop's only other liveness signal is the WRITE, and on a quiet panel the
        next write is up to a ping away - so a page that navigates away would hold its
        slot and its thread for 15 s, and eight of them would refuse the real panel for
        that long. A closed peer shows up as a socket that is readable and yields
        nothing; an SSE client sends nothing after its request, so there is no other
        reason for this socket to be readable, and anything it did send is not part of
        this protocol and is discarded.
        """
        sock = getattr(self, "connection", None)
        if sock is None:
            return False
        try:
            ready, _, _ = select.select([sock], [], [], 0)
            if not ready:
                return False
            return sock.recv(1) == b""
        except OSError:
            return True

    def _sse_snapshot(self):
        """The current snapshot, or None. Reads the builder's `state` property, which
        takes the builder lock only long enough to copy the reference - the lock is
        never held across a socket write, so a hung subscriber cannot stall a build."""
        builder = getattr(self, "builder", None)
        if builder is None:
            return None
        try:
            return builder.state
        except Exception:       # noqa: BLE001 - a stream must never kill the builder
            return None

    def _sse_write(self, raw: bytes) -> bool:
        """-> False when the client has gone. Narrowed to OSError for the reason
        do_GET's own handler gives: BrokenPipe / ConnectionReset / ConnectionAborted /
        Timeout are all OSError and all mean "the reader hung up", which is ORDINARY on
        a stream a page closes by navigating away. Anything else is still a surprise and
        still deserves its traceback."""
        try:
            self.wfile.write(raw)
            self.wfile.flush()
            return True
        except OSError:
            self.close_connection = True
            return False

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
                # SCA-033 (v0.34.0, provisional): `framed` is true ONLY after the
                # terminating zero-size chunk and its blank line. Every other way out of
                # this loop leaves bytes on the socket whose boundary crabd can no longer
                # establish, and on HTTP/1.1 - which this handler declares - keep-alive
                # then hands those bytes to BaseHTTPRequestHandler as a NEW request line.
                # Measured by the audit on a raw socket: a malformed chunk-size line
                # followed by a second request got the 404 for the first AND dispatched
                # the second on the same connection. A framing error is not recoverable
                # by reading further, so the connection closes.
                framed = False
                while len(body) < keep:
                    size_line = self.rfile.readline(64)
                    # Not newline-terminated means truncated, or a size line longer than
                    # the 64-byte bound - either way the next bytes are not where this
                    # parser would look for them.
                    if not size_line.endswith(b"\n"):
                        break
                    line = size_line.strip()
                    if not line:
                        break
                    try:
                        size = int(line.split(b";", 1)[0], 16)
                    except ValueError:
                        break
                    if size == 0:
                        # What follows a last chunk is the trailer section ended by a
                        # blank line. crabd consumes no trailers, so an immediate CRLF is
                        # the only shape it can hand back to keep-alive intact.
                        framed = self.rfile.readline(4) == b"\r\n"
                        break
                    if size < 0 or size > keep:
                        # CLAMPED, not honoured, and not skipped either: the oversize body
                        # still has to reach the endpoint's own `len(raw) > ITS_CAP` test,
                        # which is what turns it into that endpoint's error rather than a
                        # silent truncation. A chunk header is not a licence to allocate.
                        body += self.rfile.read(keep - len(body))
                        break
                    chunk = self.rfile.read(size)
                    body += chunk
                    # A chunk shorter than its header promised, or a delimiter that is not
                    # CRLF, is the same lost boundary as a bad size line. RFC 9112 §7.1
                    # makes CRLF the delimiter; a bare LF client was already misparsed by
                    # the old blind 2-byte read, so this makes that failure honest.
                    if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                        break
                if not framed:
                    self.close_connection = True
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
    # reply. So a web page is refused here unless its Origin is exactly this server's
    # own, which is what the panel page sends. A request with NO Origin header is
    # allowed: curl-fed ingest hooks, the CLI's own Stop/PermissionRequest HTTP hooks,
    # the notifier, the setup scripts and every local tool land there.
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
        # C4: it changes `approvals.readiness`, so it belongs on the inventory of what
        # CHANGES STATE even though it can neither allow nor deny.
        "/v1/approvals/verify",
    ))

    # _is_web_origin was retired with the vendor page (CLEAN-04, v0.34.0).
    # It existed to let `null` and non-web schemes through the gate, and the reasoning
    # for that allowance - and for closing it - now lives in _refused_origin.

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if not self._host_allowed():
            # Same drain-then-refuse shape as the origin branch below, same reason.
            self._acao = None
            self._read_body()
            self._send(421, HOST_NOT_ALLOWED)
            return
        origin = self.headers.get("Origin")
        self._record_origin(origin)
        if self._refused_origin(origin):
            # Drain the body first so keep-alive framing survives, then refuse. Drained
            # on EVERY path, not just the mutating ones: a refused POST to an unknown
            # path still arrived with a body, and leaving it in the stream desynchronises
            # the next request on the connection.
            self._acao = None
            self._read_body()
            self._send(403, CROSS_SITE_REFUSED)
            return
        # Reflect a PRESENT allowed origin (the widget's "null") so its cors-mode fetch
        # can read the status - the widget rolls back its optimistic tap on an unreadable
        # reply. Never the wildcard (SEC-1/SEC-4). An absent Origin is a non-browser
        # client that needs no ACAO at all.
        self._acao = origin if origin else None
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
        elif path == "/v1/hook/precompact":
            # v0.35.0. Fire-and-forget, the shape /v1/hook has and for its reason: the
            # CLI is about to compact a large context and nothing crabd does may be in
            # front of that. The 204 goes out before the parse; the two gates above have
            # already run, so this route has none of its own to drift from theirs.
            raw = self._read_body()
            self._send(204, None)
            payload = self._json_body(raw)
            if isinstance(payload, dict):
                try:
                    self.builder.record_precompact(payload)
                except Exception:   # noqa: BLE001 - see /v1/hook; the 204 has gone out
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
        elif path == "/v1/approvals/verify":
            self._do_approvals_verify(self._read_body())
        else:
            # Drain before answering, exactly like the 403 above (v0.17.0). An unknown
            # path is still a POST that ARRIVED WITH A BODY, and a body left unread sits
            # in the socket buffer where the keep-alive connection's next request-line
            # parse will find it - the client's following request is then answered as
            # garbage, or the connection dies. The test client's connect-retry hid this;
            # framing is not something a retry may be relied on to paper over.
            self._read_body()
            self._send(404, b'{"error":"not found"}')

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
            # The answer never reached the socket, so CRB-F5 keeps the prompt for the
            # next Stop - which means it is not delivered and a cancel may still have it.
            queue = getattr(self.builder, "continues", None)
            if queue is not None:
                queue.release(session_id, prompt)
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
        # claim, not peek (MF-002): from here the prompt is on its way into the answer
        # and a cancel must be told it lost rather than silently deleting an item that
        # will be delivered anyway.
        prompt = queue.claim(session_id, now)
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
                or action not in ("ack", "reply", "queue-continue", "cancel-continue",
                                  "decide")):
            self._send(400, b'{"error":"malformed request"}')
            return
        if action == "queue-continue":
            self._do_queue_continue(session_id, body.get("prompt"))
            return
        if action == "cancel-continue":
            self._do_cancel_continue(session_id)
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
        # lane D (v0.33.0): the whitelist is now per SESSION - builtins, the global
        # extras, and the project prompts for THIS row's repo/cwd. A prompt configured
        # only for another project is refused here with the 400 an unknown prompt has
        # always had. The gate ORDER is unchanged (shape before existence), so an
        # unknown session still reads the global set and still 404s below.
        repo, cwd = self.builder.session_project(session_id)
        allowed = self.builder.config.continue_prompts_for(now, repo, cwd)
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

    def _do_cancel_continue(self, session_id: str) -> None:
        """POST /v1/action {"action":"cancel-continue"} -> 204 (MF-002, v0.34.0
        provisional).

        204 removed it, 409 says a Stop hook already has it, 404 says there was nothing
        to cancel. Replacing a queued prompt with another one was the only way to change
        your mind before this, and replacing is not cancelling: the session still gets
        told to do something.

        THE RACE IS THE FEATURE. A Stop hook can fire between the tap and this handler,
        and the two answers are not interchangeable - "cancelled" means the session will
        not act on it and "already delivered" means it will. The queue settles the order
        under one lock (claim / cancel / drain_if), so exactly one of the two wins and
        the loser is told which it was, with the time the delivery was taken.

        NO session-existence gate, unlike queue-continue, and that is deliberate: a
        prompt queued for a session that has since gone quiet is exactly the one an
        operator most wants to withdraw, and answering "unknown session" would strand it
        until the TTL. Nothing queued is already its own answer.
        """
        queue = getattr(self.builder, "continues", None)
        if queue is None:
            self._send(501, b'{"error":"continue not supported"}')
            return
        now = time.time()
        if not self.builder.config.allow_continue(now):
            self._send(403, b'{"error":"tap-to-continue is disabled"}')
            return
        verdict, prompt, at = queue.cancel(session_id, now)
        if verdict == "cancelled":
            self.builder.hooks.note_external(session_id,
                                             "continue cancelled: " + prompt,
                                             create=True)
            self._send(204, None)
            return
        if verdict == "delivered":
            self._send(409, dump_state({"error": "already delivered",
                                        "deliveredAt": _utc_iso(at)}))
            return
        self._send(404, b'{"error":"nothing queued"}')

    def _do_approvals_verify(self, raw: bytes) -> None:
        """POST /v1/approvals/verify {"code": "..."} -> 204 (C4 / MF-017).

        204 and a recorded `verifiedAt` on a match, 403 on a mismatch, 429 once five
        attempts have been made inside a minute. IT NEVER DECIDES ANYTHING: there is no
        reference to the permission broker on this path, so no body and no ordering of
        requests can turn a pairing check into an approval. It never returns the code
        either - not on success, not in an error - so a caller learns only whether the
        code it already held was right.

        The enable flag is NOT a gate here. An operator pairing the panel wants to know
        the code is right before arming approvals, and a readiness answer that needs the
        feature already on would be useless exactly when it is needed. Nothing is armed
        by verifying.

        Gate order is the same as decide's, and for the same reason: shape first (400,
        no secret consulted), then the crabd-has-no-token case (503), then the code.
        """
        body = self._json_body(raw)
        if not isinstance(body, dict):
            self._send(400, b'{"error":"malformed request"}')
            return
        code = body.get("code")
        if not isinstance(code, str) or not code.strip():
            self._send(400, b'{"error":"code required"}')
            return
        gate = getattr(self.builder, "panel_token", None)
        if gate is None:
            self._send(503, b'{"error":"panel pairing unavailable"}')
            return
        verdict = gate.verify_code(code, time.time())
        if verdict == "rate-limited":
            self._send(429, b'{"error":"too many attempts - wait a minute"}')
            return
        if verdict != "ok":
            self._send(403, b'{"error":"pairing code rejected"}')
            return
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
    # v0.34.0 (provisional), C6 / MF-001: the three continue-prompt keys join the
    # whitelist so the settings sheet can edit the vocabulary it already draws.
    # panelApprovals, allowReply, allowContinue and recapRepos stay OUT and must: the
    # first three gate security or a feature, and recapRepos points the git half at an
    # arbitrary path.
    CONFIG_WRITABLE = ("quietHours", "toast", "digest", "budget",
                       "continuePrompts", "continuePromptsByRepo",
                       "continuePromptsByPath")
    CONFIG_WRITABLE_ERROR = (b'{"error":"quietHours, toast, digest, budget, '
                             b'continuePrompts, continuePromptsByRepo and '
                             b'continuePromptsByPath are the only writable keys"}')

    def _do_config(self, raw: bytes) -> None:
        """POST /v1/config - the writable keys, and NOTHING else.

        The whitelist is exact rather than "ignore what you don't know": `allowReply`
        gates a feature, and a widget (or anything else that can reach localhost) must
        not be able to flip it by naming it in this body. Any key alone is valid, so is
        any combination of them; an unknown key anywhere is 400. An invalid body is
        rejected WHOLE - the file is never half-written from a request that failed
        validation, which is why every key is validated before the single write below.

        THE ANSWER CARRIES WHAT WAS WRITTEN (C6): 200 with {"applied", "warnings"}
        rather than the old bare 204. The continue-prompt keys are parsed the way the
        FILE parser parses them - entries that are not strings, blank, over-long,
        duplicated or past a cap are dropped rather than failing the write - and a drop
        the operator cannot see is a setting that silently did not take. `applied` is
        the normalised value now on disk, so the sheet can render what it actually got:
        "7:5" comes back as "07:05".
        """
        body = self._json_body(raw)
        if (not isinstance(body, dict) or not body
                or not set(body) <= set(self.CONFIG_WRITABLE)):
            self._send(400, self.CONFIG_WRITABLE_ERROR)
            return
        values = {}
        warnings: list[str] = []
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
        for key, validator in (("continuePrompts", self._validate_continue_prompts),
                               ("continuePromptsByRepo", self._validate_by_repo),
                               ("continuePromptsByPath", self._validate_by_path)):
            if key not in body:
                continue
            ok, normalized, said = validator(body[key])
            if not ok:
                self._send(400, dump_state(
                    {"error": f"{key} must be the shape the config file uses, or null"}))
                return
            values[key] = normalized
            warnings.extend(said)
        if not self.builder.config.set_keys(values):
            self._send(500, b'{"error":"could not write config"}')
            return
        self._send(200, dump_state({"applied": values, "warnings": warnings}))

    # ---- C6 / MF-001: the continue-prompt keys, parsed as the FILE parser parses them
    # Each returns (ok, normalized, warnings). `ok` False means the KEY's own shape is
    # wrong - not a list, not an object - which is a caller bug and a 400. Anything the
    # file parser would DROP is dropped here too and named in a warning, because the
    # sheet has to be able to show the operator that their entry did not take.

    @staticmethod
    def _clean_prompt_list(raw, where: str, cap: int, drop_builtins: bool):
        """-> (list, warnings), applying exactly the rules continue_extras and
        _project_list apply: strings only, whitespace collapsed, 1..CONTINUE_PROMPT_MAX,
        deduped, capped."""
        out: list[str] = []
        warnings: list[str] = []
        for entry in raw:
            if len(out) >= cap:
                warnings.append(f"{where}: kept the first {cap} prompts")
                break
            if not isinstance(entry, str):
                warnings.append(f"{where}: dropped an entry that is not text")
                continue
            prompt = " ".join(entry.split())
            if not prompt:
                warnings.append(f"{where}: dropped a blank entry")
            elif len(prompt) > CONTINUE_PROMPT_MAX:
                warnings.append(f"{where}: dropped an entry over "
                                f"{CONTINUE_PROMPT_MAX} characters")
            elif prompt in out:
                warnings.append(f"{where}: dropped a duplicate of {prompt!r}")
            elif drop_builtins and prompt in CONTINUE_PROMPTS_BUILTIN:
                warnings.append(f"{where}: {prompt!r} is already a builtin button")
            else:
                out.append(prompt)
                continue
        return out, warnings

    @classmethod
    def _validate_continue_prompts(cls, value):
        if value is None:
            return True, None, []
        if not isinstance(value, list):
            return False, None, []
        # drop_builtins: continue_extras refuses a duplicate of a builtin because the
        # widget draws builtins then extras, and the button would appear twice.
        out, warnings = cls._clean_prompt_list(value, "continuePrompts",
                                               CONTINUE_PROMPTS_CAP, True)
        return True, out, warnings

    @classmethod
    def _validate_by_repo(cls, value):
        if value is None:
            return True, None, []
        if not isinstance(value, dict):
            return False, None, []
        out: dict = {}
        warnings: list[str] = []
        seen: set[str] = set()
        for key, entry in list(value.items())[:CONTINUE_PROMPTS_PROJECT_KEYS]:
            name = key.strip() if isinstance(key, str) else ""
            if not name:
                warnings.append("continuePromptsByRepo: dropped a blank key")
                continue
            if name.casefold() in seen:
                # The file parser's first-wins rule, reported rather than merged.
                warnings.append(f"continuePromptsByRepo: {name!r} differs from an "
                                f"earlier key only in case - the first one is used")
                continue
            seen.add(name.casefold())
            if not isinstance(entry, list):
                warnings.append(f"continuePromptsByRepo[{name}]: dropped, not a list")
                continue
            # An EMPTY list is kept (SCA-011): it is precedence-bearing configuration,
            # not an absent key, and the sheet must be able to write one.
            prompts, said = cls._clean_prompt_list(
                entry, f"continuePromptsByRepo[{name}]",
                CONTINUE_PROMPTS_PROJECT_CAP, False)
            out[name] = prompts
            warnings.extend(said)
        if len(value) > CONTINUE_PROMPTS_PROJECT_KEYS:
            warnings.append(f"continuePromptsByRepo: kept the first "
                            f"{CONTINUE_PROMPTS_PROJECT_KEYS} projects")
        return True, out, warnings

    @classmethod
    def _validate_by_path(cls, value):
        if value is None:
            return True, None, []
        if not isinstance(value, dict):
            return False, None, []
        out: dict = {}
        warnings: list[str] = []
        for key, entry in list(value.items())[:CONTINUE_PROMPTS_PROJECT_KEYS]:
            root = key.strip() if isinstance(key, str) else ""
            if not root or not os.path.isabs(root):
                # A relative key can never match a session cwd, which is absolute, so
                # the file parser refuses it rather than keeping a key that never fires.
                warnings.append(f"continuePromptsByPath: dropped {str(key)[:64]!r}, "
                                f"not an absolute path")
                continue
            if not isinstance(entry, list):
                warnings.append(f"continuePromptsByPath[{root}]: dropped, not a list")
                continue
            prompts, said = cls._clean_prompt_list(
                entry, f"continuePromptsByPath[{root}]",
                CONTINUE_PROMPTS_PROJECT_CAP, False)
            out[root] = prompts
            warnings.extend(said)
        if len(value) > CONTINUE_PROMPTS_PROJECT_KEYS:
            warnings.append(f"continuePromptsByPath: kept the first "
                            f"{CONTINUE_PROMPTS_PROJECT_KEYS} paths")
        return True, out, warnings

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
    # Windows SO_REUSEADDR lets a SECOND crabd bind the same port and answer half the
    # requests - two instances raced during build QA. Refusing reuse turns that into
    # a loud "already running" instead of a silently split feed.
    allow_reuse_address = False
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

    # ---- lane B: the SSE stop event and subscriber cap ----
    # daemon_threads is what keeps server_close() from JOINING an open /v1/events
    # stream (_Threads.append drops daemon threads, so join() never sees them) - but a
    # dropped thread is not a stopped one: it goes on holding a subscriber slot and
    # writing pings to a socket the server has closed, for the rest of the process. The
    # event is what actually ends the loop. Both entry points set it, because a caller
    # may use either, and setting an already-set Event is free.
    def __init__(self, *args, **kwargs) -> None:
        self.sse_stop = threading.Event()
        self.sse_slots = SseSubscribers(SSE_MAX_SUBSCRIBERS)
        super().__init__(*args, **kwargs)

    def shutdown(self) -> None:
        self.sse_stop.set()
        super().shutdown()

    def server_close(self) -> None:
        self.sse_stop.set()
        super().server_close()

    def handle_error(self, request, client_address) -> None:
        """socketserver's hook for an exception that escaped a handler. It prints a
        traceback to stderr, which under the Scheduled Task goes nowhere (v0.35.0) - so
        the same traceback is written to crabd.log, where it can be read afterwards.

        stderr keeps the default output: a maintainer running crabd in a console sees
        exactly what they see today."""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError, TimeoutError)):
            # The client hung up mid-answer. ORDINARY on this host (see do_GET's own
            # narrowing) and not worth a traceback in either destination.
            return
        log_line("crabd: unhandled error answering a request", exc, stderr=False)
        super().handle_error(request, client_address)


def _refresh_loop(builder: StateBuilder, stop: threading.Event) -> None:
    """The snapshot is built here, not in the request path, so /v1/state is a dict
    dump. A stalled builder shows up honestly as a stale generatedAt."""
    while not stop.is_set():
        try:
            state = builder.build()
            with builder._lock:
                builder._state = state
        except Exception as exc:  # a bad transcript must not kill the feed
            log_line(f"crabd: refresh error: {type(exc).__name__}", exc)
        stop.wait(REFRESH_INTERVAL_SEC)


def _recap_loop(recap: RecapReader, stop: threading.Event) -> None:
    """Own thread: `git log` per repo is a subprocess, and /v1/state must never wait
    on one."""
    while not stop.is_set():
        try:
            recap.poll(time.time())
        except Exception as exc:  # a wedged repo must not kill the feed
            log_line(f"crabd: recap error: {type(exc).__name__}", exc)
        stop.wait(RECAP_POLL_SEC)


def _fleet_loop(fleet: FleetReader, stop: threading.Event) -> None:
    """Own thread: two schtasks subprocesses, and /v1/state must never wait on one."""
    while not stop.is_set():
        try:
            fleet.poll(time.time())
        except Exception as exc:  # a wedged schtasks must not kill the feed
            log_line(f"crabd: fleet error: {type(exc).__name__}", exc)
        stop.wait(FLEET_POLL_SEC)


# ---- lane A: one loop body, three threads ----
def _lane_a_sampler_loop(reader, label: str, interval: float,
                         stop: threading.Event) -> None:
    """A sampler's own thread, the shape _fleet_loop already has.

    Each of the three gets its OWN thread rather than sharing one: they block on
    different things (a kernel section, a subprocess, ~400 OpenProcess calls), and one
    that wedges must not stop the other two from dating their own readings.
    """
    while not stop.is_set():
        try:
            reader.poll(time.time())
        except Exception as exc:        # a wedged sampler must not kill the feed
            log_line(f"crabd: {label} error: {type(exc).__name__}", exc)
        stop.wait(interval)


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
            log_line(f"crabd: expiry error: {type(exc).__name__}", exc)
        stop.wait(EXPIRY_POLL_SEC)


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
    # ---- lane A: the three host samplers ----
    hwinfo, gpu, load = HwinfoReader(), GpuReader(), LoadReader()
    builder = StateBuilder(TranscriptStore(PROJECTS_DIR), hooks,
                           LimitsReader(), started, UserConfig(), recap, fleet,
                           history, statusline, otlp, continues, permissions,
                           models=ModelCatalog(),
                           hwinfo=hwinfo, gpu=gpu, load=load)
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
    # ---- lane A: the three host samplers ----
    for reader, label, interval in ((hwinfo, "hwinfo", HWINFO_POLL_SEC),
                                    (gpu, "gpu", NVIDIA_POLL_SEC),
                                    (load, "load", LOAD_POLL_SEC)):
        threading.Thread(target=_lane_a_sampler_loop,
                         args=(reader, label, interval, stop), daemon=True).start()

    try:
        server = CrabdServer((HOST, PORT), Handler)
    except OSError:
        stop.set()
        log_line(f"crabd: port {PORT} is already in use - another crabd is running "
                 f"(set CRABD_PORT to run a second instance)")
        return 1
    # The startup line is what makes crabd.log answerable at all: a log whose newest
    # line predates the current process says the process never got this far.
    log_line(f"crabd {VERSION} listening on http://{HOST}:{PORT} "
             f"(pid {os.getpid()}, projects {PROJECTS_DIR})", stderr=False)
    print(f"crabd {VERSION} listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception as exc:        # noqa: BLE001 - the last place a traceback can land
        log_line("crabd: the server loop stopped on an unhandled error", exc)
        raise
    finally:
        stop.set()
        server.server_close()
        # BEST EFFORT, and the log must not be read as though it were not: the Scheduled
        # Task stops crabd with TerminateProcess, which runs no finally block. Measured
        # 2026-09-22 on an isolated live-fire run. So a startup line with no stop line
        # before it is the ORDINARY shape of a restart, not evidence of a crash.
        log_line(f"crabd {VERSION} stopped", stderr=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
