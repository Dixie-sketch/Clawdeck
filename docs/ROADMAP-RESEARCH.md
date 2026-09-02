# SideCrab — integration research

*What else could sit between the coding agent and this panel, and what is worth building next.*

This is a research document, not a plan of record. It surveys the surfaces SideCrab could consume
or extend, cites the evidence for each, and ends with a shortlist. Nothing here is committed; the
[backlog](BACKLOG.md) is what is actually queued, and the [PRD](PRD.md) roadmap is what has been
decided.

**Researched:** 2026-08-26. Every claim below is sourced. Vendor documentation moves — anything
marked ⚠️ needs re-reading against the live docs, and everything needs a spike against the version
on the machine, before it becomes code. SideCrab's own rule applies to its roadmap too: measure
first, do not build against a number you did not read yourself.

**How to read the tables.** Effort: **S** ≈ under a day · **M** ≈ one wave · **L** ≈ multiple
waves. Value is a judgement about how much it moves the product, not how interesting it is.

**Where SideCrab is today**, for contrast: six hook events POSTed by `curl.exe` to `crabd`; the
OAuth usage endpoint for limits; transcript JSONL parsed for token burn; a local `git log` for the
recap; one iCUE data provider (Sensors); one device target (`dashboard_lcd`).

---

## Contents

1. [Telemetry — the OpenTelemetry pipeline](#1-telemetry--the-opentelemetry-pipeline)
2. [The status line — a third, official data channel](#2-the-status-line--a-third-official-data-channel)
3. [Hooks — the catalog against the six SideCrab uses](#3-hooks--the-catalog-against-the-six-sidecrab-uses)
4. [Packaging — SideCrab as a plugin](#4-packaging--sidecrab-as-a-plugin)
5. [The Agent SDK — SideCrab-launched jobs](#5-the-agent-sdk--sidecrab-launched-jobs)
6. [Cloud and remote surfaces](#6-cloud-and-remote-surfaces)
7. [The iCUE surface SideCrab does not use](#7-the-icue-surface-sidecrab-does-not-use)
8. [Community landscape](#8-community-landscape)
9. [Shortlist](#9-shortlist)
10. [Sources](#10-sources)

---

## 1. Telemetry — the OpenTelemetry pipeline

**What it is.** The agent CLI has a first-party OpenTelemetry exporter, off by default, enabled by
environment variable. It emits **metrics** (counters and gauges, default 60 s export interval),
**logs/events** (default 5 s), and — behind a beta flag — **traces**. It speaks OTLP over gRPC,
`http/protobuf` or `http/json`, and can also expose a Prometheus scrape endpoint or print to the
console. [[monitoring]](#s-monitoring)

This is the richest, most stable Claude-side data source that exists, and SideCrab consumes none
of it.

### 1.1 What the metrics actually carry

| Metric | Unit | Notable attributes |
|---|---|---|
| `claude_code.session.count` | count | `start_type`: `fresh`, `resume`, `continue`, `agents_view` |
| `claude_code.token.usage` | tokens | `type` (`input`/`output`/`cacheRead`/`cacheCreation`), `model`, `query_source` (`main`/`subagent`/`auxiliary`), `speed`, `effort`, `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`, `mcp_tool.name` |
| `claude_code.cost.usage` | **USD** | same attribution set as tokens |
| `claude_code.active_time.total` | seconds | `type`: `user` (keyboard) or `cli` (tool execution / model response) |
| `claude_code.lines_of_code.count` | count | `type`: `added` / `removed`, `model` |
| `claude_code.commit.count` | count | — |
| `claude_code.pull_request.count` | count | — |
| `claude_code.code_edit_tool.decision` | count | `tool_name`, `decision` (`accept`/`reject`), `source` (`config`/`hook`/`user_permanent`/`user_temporary`/`user_abort`/`user_reject`), `language` |

Events (logs) add: `user_prompt`, `assistant_response`, `tool_result` (with `duration_ms`,
`success`, `error_type`), `api_request` (with `cost_usd`, per-call token counts, `duration_ms`),
`api_error` (with `status_code`, `attempt`), `api_refusal`, `tool_decision`,
`permission_mode_changed` (`from_mode` → `to_mode`, `trigger`), `auth`, `mcp_server_connection`,
`internal_error`, `plugin_installed`, `plugin_loaded`. Every event carries `session.id`, an
`event.sequence` counter, and a `prompt.id` that ties every event from one user turn
together. [[monitoring]](#s-monitoring)

### 1.2 Why this matters to SideCrab specifically

Four things SideCrab cannot currently show, and one it computes the hard way:

- **Cost in dollars.** SideCrab has token counts and no money. `claude_code.cost.usage` is a USD
  counter, split by model and by what spent it.
- **Where the tokens went.** `query_source` separates main-thread spend from subagent spend from
  auxiliary spend; `agent.name`, `skill.name` and `mcp_server.name` attribute it further. The burn
  zone today answers "how much"; this answers "what".
- **Real working time.** `active_time.total` with `type=user` versus `type=cli` is a genuine
  "time at the keyboard versus time waiting on the machine" split. No amount of transcript parsing
  produces this.
- **Failure signal.** `api_error` with `status_code` and `attempt`, `api_refusal`,
  `mcp_server_connection` status, `internal_error` — SideCrab currently shows a session as
  "working" while it is in fact retrying against a 429.
- **Commits and lines changed**, first-party, replacing the `git log` recap read — which today has
  to be told which repositories to count because a session driving a repo from elsewhere is
  invisible to it.

### 1.3 The shape of the integration

**crabd is already an HTTP server on `127.0.0.1:2722`.** OTLP over HTTP posts to
`{endpoint}/v1/metrics`, `{endpoint}/v1/logs` and `{endpoint}/v1/traces` — paths crabd does not
use. With `OTEL_EXPORTER_OTLP_PROTOCOL=http/json` the bodies are plain JSON, parseable with no
protobuf dependency. So the integration is plausibly **crabd growing two endpoints, not SideCrab
shipping a collector**:

```
CLAUDE_CODE_ENABLE_TELEMETRY=1
OTEL_METRICS_EXPORTER=otlp
OTEL_LOGS_EXPORTER=otlp
OTEL_EXPORTER_OTLP_PROTOCOL=http/json
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:2722
OTEL_METRIC_EXPORT_INTERVAL=10000
```

⚠️ The `http/json` body shape is the protobuf-JSON mapping and must be confirmed by a spike before
anyone designs a parser around it. If it turns out awkward, the fallback is
`OTEL_METRICS_EXPORTER=prometheus` (fixed scrape at `http://localhost:9464/metrics`, metrics only,
crabd pulls) — less good, but no new parser and no new port to defend.

### 1.4 Traps, measured against the docs

- **The environment variables must exist where the CLI launches**, which for most users means the
  `env` block of a settings file rather than a shell profile. An installer that edits a hook
  fragment can edit that too — but it is now editing *behaviour*, not just a notification sink.
  That is a bigger consent ask and the installer must treat it as one.
- **`OTEL_*` exporter variables are stripped from every subprocess the CLI spawns**, hooks
  included. [[hooks-ref]](#s-hooks-ref) So a hook cannot inherit them, and crabd must not be
  launched from a hook if it is expected to see them.
- **60 s is the default metric interval** — an eternity against a 3 s panel poll. Metrics are the
  *accounting* layer, not the *liveness* layer. Logs at 5 s are closer to live. Hooks remain the
  only sub-second channel and must stay.
- **Delta temporality is the default** for OTLP metrics; a receiver that assumes cumulative
  counters will read wrong numbers and look plausible while doing it.
- **Privacy is a decision, not a default.** `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_ASSISTANT_RESPONSES`,
  `OTEL_LOG_TOOL_DETAILS` and `OTEL_LOG_RAW_API_BODIES` all default off, and SideCrab should leave
  every one of them off and say so in the listing. The panel does not need prompt text and must
  never persist it. The existing rule — history carries event kind, session id and title, never
  question text — extends here unchanged.
- **Cardinality knobs** (`OTEL_METRICS_INCLUDE_SESSION_ID` default true,
  `..._ACCOUNT_UUID` default true) are what make per-session attribution possible. Do not turn
  session id off; do consider whether crabd should discard the account attributes on receipt
  rather than hold them.

### 1.5 Verdict

**Complement, not replacement.** Telemetry replaces *transcript parsing for burn* — better numbers,
more dimensions, no JSONL format risk, and the PRD's "the usage source can shift between versions"
risk shrinks. It does **not** replace hooks: metrics arrive on a 60 s cadence and carry no
"this session is waiting on you". A SideCrab that consumed only telemetry would be a slower, richer
accounting dashboard — which is the thing SideCrab deliberately is not.

Opportunities:

| # | Opportunity | Effort | Value |
|---|---|---|---|
| A1 | crabd accepts OTLP/HTTP-JSON at `/v1/metrics` + `/v1/logs`; installer offers to set the env block, opt-in | **M** | **Very high** — the enabling step for everything else in §1 |
| A2 | Cost in USD on the panel and in the burn sheet | S (after A1) | High |
| A3 | Attribution panel: spend by `query_source` / `agent.name` / `skill.name` / `mcp_server.name` | S–M (after A1) | High |
| A4 | `active_time.total` user-vs-cli split in the recap | S (after A1) | Medium |
| A5 | First-party `commit.count` / `lines_of_code.count`, retiring the `recapRepos` workaround | S (after A1) | Medium–high |
| A6 | Error surface from `api_error` / `api_refusal` / `mcp_server_connection` / `internal_error` | S–M (after A1) | High |
| A7 | Permission posture: `code_edit_tool.decision`, `tool_decision`, `permission_mode_changed` | S (after A1) | Medium |

---

## 2. The status line — a third, official data channel

Not a hook, not telemetry. The `statusLine` setting runs a command and feeds it a **JSON session
document on stdin**, event-driven with a 300 ms debounce, plus an optional `refreshInterval` timer
(minimum 1 s). [[statusline]](#s-statusline)

That document contains, per session:

- `rate_limits.five_hour.used_percentage` and `.resets_at`, and `rate_limits.seven_day.*` — **the
  exact numbers SideCrab's limit gauges exist to show**, delivered officially, with no OAuth token
  in the picture at all.
- `context_window.used_percentage`, `.remaining_percentage`, `.context_window_size`,
  `.total_input_tokens`, `.total_output_tokens`, `.current_usage` — a real context percentage
  against a real window size, including the 1 M-token case.
- `cost.total_cost_usd`, `cost.total_duration_ms`, `cost.total_api_duration_ms`,
  `cost.total_lines_added`, `cost.total_lines_removed`.
- `workspace.current_dir`, `.project_dir`, `.added_dirs`, `.git_worktree`, and
  `workspace.repo.{host,owner,name}` parsed from the origin remote — repo identity without
  shelling out to git.
- `model.display_name`, `output_style.name`, `session_id`, `transcript_path`,
  `exceeds_200k_tokens`.

There is a sibling `subagentStatusLine` for subagent rows.

**Why this is close to as big as §1.** The PRD names "the usage/limit source can shift between
versions" as risk #1, and the companion currently mitigates it by isolating one module that reads
an OAuth endpoint. A documented stdin contract that hands over `five_hour` and `seven_day`
utilisation is a *supported* replacement for that reach-around. It also fixes the context chip,
which today is derived arithmetic over transcript usage records and does not know the window size.

**Traps:**

- **A user gets one status line.** Installing SideCrab's would silently replace whatever the user
  already runs (`ccusage statusline`, a custom script, and so on). This must be opt-in, must
  detect an existing `statusLine`, and should offer to *chain* — run the previous command and print
  its output — rather than take the slot. Getting this wrong is the single most user-hostile thing
  in this entire document.
- **`rate_limits` appears only for subscription accounts, and only after the first API response of
  a session.** Absence is normal and must render as an em-dash, never a zero. This is exactly the
  existing honest-failure rule and needs no new thinking, only discipline.
- `context_window.current_usage` is `null` before the first API call and again after a compaction.
- The event-driven triggers **go quiet while the main session is idle**; a `refreshInterval` is
  needed for anything time-based. An in-flight script is **cancelled** if a new update arrives, so
  the command must be cheap and idempotent — a one-line POST to crabd, nothing more.

| # | Opportunity | Effort | Value |
|---|---|---|---|
| C1 | Optional SideCrab status-line command → POST the session document to crabd; limits and context come from it when present | **M** | **Very high** — retires the OAuth-endpoint dependency and the PRD's top risk |
| C2 | Real context ring per card from `context_window.used_percentage` against `context_window_size` | S (after C1) | High |
| C3 | Cost, lines changed, API-wait time, repo/worktree identity from the same document | S (after C1) | Medium |
| C4 | `subagentStatusLine` feeding the subagent badges | S (after C1) | Low–medium |

---

## 3. Hooks — the catalog against the six SideCrab uses

SideCrab consumes six events. The documented catalog is roughly thirty. [[hooks-ref]](#s-hooks-ref)

### 3.1 The delivery mechanism itself is upgradeable

Hook handlers have a `type`, and `command` is only one of five. **`"type": "http"` POSTs the event
JSON straight to a URL** — no `curl.exe`, no shell, no `|| exit 0`, no per-event process spawn:

```json
{ "type": "http", "url": "http://127.0.0.1:2722/v1/hook" }
```

Headers are supported, with `$VAR` interpolation restricted to an explicit `allowedEnvVars` list.
**The endpoint's JSON response body is interpreted exactly like a command hook's stdout**, which
means crabd stops being write-only and gains a reply channel. [[hooks-guide]](#s-hooks-guide)

Everything about this is better than the current fragment: fewer moving parts, no dependency on a
`curl.exe` being on PATH, lower latency, and it works identically regardless of shell. The one
thing to check in a spike is failure behaviour when crabd is *not* running — the current fragment's
`|| exit 0` exists precisely so a stopped companion never blocks a session, and the HTTP type's
timeout path must be verified to be equally harmless.

Also available and unused: **`matcher`**, which for `Notification` matches on notification type and
for tool events on tool name, and **`if`**, which narrows a tool-event handler by permission rule.
Both are how event volume stays sane if SideCrab starts consuming tool events.

### 3.2 Events worth consuming

| Event | What SideCrab gains | Effort | Value |
|---|---|---|---|
| **`PermissionRequest`** | Fires when the CLI is about to *ask*. Today SideCrab infers "waiting on permission" from a `Notification` message string. This is the real thing, with `tool_name` and `tool_input`. And it returns a decision — see §3.3. | M | **Very high** |
| **`StopFailure`** | A turn that ended on an API error, with `error_type` (`rate_limit`, `overloaded`, `authentication_failed`, `billing_error`, `server_error`). **SideCrab cannot currently distinguish a rate-limited session from a finished one.** | S | **High** |
| **`Notification` `matcher`** | Split `permission_prompt` / `idle_prompt` / `agent_needs_input` / `agent_completed` / `elicitation_dialog` / `quota_auto_resume_*` at the hook, instead of parsing a human string. | S | High |
| **`SubagentStart`** | Carries `agent_type` and `agent_id`. Today only `SubagentStop` is consumed, so the running count is inferred and the badge cannot name the agent. Pairing start/stop makes `subagents.running` exact. | S | Medium–high |
| **`PreCompact` / `PostCompact`** | A compacting state, and the strongest available "this session is under context pressure" signal. Pairs with C2. | S | Medium |
| **`PostToolUse` / `PreToolUse`** | Per-card live activity — "editing `foo.py`", "running tests" — instead of a coarse `lastEvent`. High volume; needs a `matcher`, needs debouncing, and must never be persisted. | M | Medium |
| **`TaskCreated` / `TaskCompleted`** | A per-session task progress indicator. | M | Medium |
| **`PermissionDenied`** | Auto-mode denials with `denial_reason` — a session that is stuck and does not know it. | S | Medium |
| **`CwdChanged`** | Repo/branch accuracy without re-reading git on a timer. | S | Low–medium |
| **`SessionStart` extras** | Carries `model` and `permission_mode` — a card could show permission mode, which materially changes how much attention a session deserves. | S | Medium |
| **`Setup`, `WorktreeCreate/Remove`, `DirectoryAdded`, `ConfigChange`, `InstructionsLoaded`, `Elicitation`, `MessageDisplay`, `TeammateIdle`** | Surveyed; none earns a place on a glance panel today. `MessageDisplay` in particular fires per streaming chunk and would be a firehose for no gain. | — | Low |

### 3.3 Hooks that inject data back — and the one that unblocks v2

The mission asked whether any hook can push data *into* a session. Several can:

- `SessionStart`, `PostToolUse`, `FileChanged`, `SubagentStart` and others accept
  `additionalContext` and `systemMessage` in their JSON output.
- `UserPromptSubmit` stdout on exit 0 is added as context, and it can rewrite the prompt via
  `updatedInput`.
- `Stop` can refuse to let a turn end.
- `PreToolUse` can `allow` / `deny` / `escalate` and can rewrite `updatedInput`.

**Recommendation: SideCrab should decline nearly all of this.** A status panel that quietly injects
text into your conversation is a different, less trustworthy product, and it breaks the read-only
promise the listing makes. There is exactly one exception, and it is the important one:

> **`PermissionRequest` is the supported external send that the v2 spike went looking for and did
> not find.**

The spike concluded touch-to-act was blocked because "the bus delivers only at tool rounds, the CLI
forks a new session, and window targeting is unsafe" — all still true for *free-form replies*. But
the specific case the panel is loudest about — **a session waiting on a permission prompt** — has a
first-class mechanism. A `PermissionRequest` handler returns a decision the CLI honours in place of
the dialog, and the transcript records that a hook allowed it. Handler timeouts default to **600
seconds** for `command`, `http` and `mcp_tool` types, so a handler can legitimately *wait* while the
decision is made elsewhere.

The shape: crabd receives the `PermissionRequest` over an HTTP hook, raises the card on the panel
with the tool and its input, waits for a tap, and answers with the decision. `POST /v1/action` today
returns `501` for `reply`; this is a *different* verb — `decide` — and it does not need reply
injection to exist.

This deserves care rather than enthusiasm:

- ⚠️ **The exact response shape must be re-read from the reference before implementation.** The
  guide shows `hookSpecificOutput.decision.behavior: "allow"`; a second reading of the reference
  rendered it as `decision: "allow" | "deny" | "deferToUser"`. One of those is stale. Verify, then
  write it in the state contract.
- **`deferToUser` is the safe default and the honest one.** A handler that times out, or that finds
  the panel unreachable, must land the user back in the normal terminal dialog — never in a denial,
  and never in a silent approval.
- **A touchscreen on a desk is not an authenticated surface.** Approving a `Bash` command from
  across the room is a real security decision and the feature needs a deliberate answer: an
  allow-list of tools it will decide, a hard refusal to decide anything else, quiet hours that
  defer rather than decide, and an off-by-default setting. "Approve anything from the glass" is not
  a shippable default.
- Blocking a session for up to ten minutes on a panel tap is a heavy default. Short timeout,
  defer on expiry.

| # | Opportunity | Effort | Value |
|---|---|---|---|
| B1 | Move the hook fragment to `"type": "http"`; drop the `curl.exe` dependency | S | High |
| B2 | `PermissionRequest` → decide-from-the-panel, allow-listed, opt-in, defer-by-default | M–L | **Very high** — unblocks the v2 headline |
| B3 | `Notification` matchers replacing message-string parsing | S | High |
| B4 | `StopFailure` → an errored/rate-limited session state | S | High |
| B5 | `SubagentStart` pairing for exact subagent counts and named agents | S | Medium–high |
| B6 | `PreCompact`/`PostCompact` → compacting state and context pressure | S | Medium |
| B7 | `PostToolUse` (matched, debounced) → live per-card activity | M | Medium |
| B8 | `TaskCreated`/`TaskCompleted` → per-session progress | M | Medium |
| B9 | Deliberately **not**: general context injection via `additionalContext` / `updatedInput` | — | — |

---

## 4. Packaging — SideCrab as a plugin

The installer currently merges JSON into a user settings file, backs it up first, and matches on
the crabd URL so re-running does not duplicate. That is careful work done because there was no
better mechanism. There now is one.

A **plugin** is a directory with a manifest that can ship `hooks/hooks.json`, MCP servers, commands,
agents, skills, executables on PATH, and a `userConfig` block whose values are prompted at enable
time and exported to hook processes as `CLAUDE_PLUGIN_OPTION_<KEY>`. Plugins install from a
marketplace with `claude plugin install <name>@<marketplace>`, are enabled and disabled without
uninstalling, update in place, and expose `${CLAUDE_PLUGIN_ROOT}` and a persistent
`${CLAUDE_PLUGIN_DATA}` directory. [[plugins]](#s-plugins)

What that buys SideCrab:

- **Install and uninstall stop touching the user's settings file.** No merge, no backup, no
  match-on-URL de-duplication, no class of bug where an uninstall removes someone else's hook.
- **The port becomes a `userConfig` value** instead of a constant baked into six copies of a curl
  command.
- **`enable` / `disable` is a real off switch** — which SideCrab currently lacks entirely. Today,
  stopping crabd leaves six hooks firing at a dead port.
- A second, discoverable distribution channel alongside the widget marketplace, and the two halves
  of the product finally have symmetrical install stories.

The considerations: it is a second listing to maintain; the widget still installs separately from
the iCUE side; and the plugin route should not become the *only* route, because the hook fragment
is also the documentation of what SideCrab listens to.

| # | Opportunity | Effort | Value |
|---|---|---|---|
| D1 | Ship the hooks (and any status line) as a plugin with `userConfig` for the port; keep the fragment as documentation | M | High |

---

## 5. The Agent SDK — SideCrab-launched jobs

**What it is.** The same agent loop, tools and context management as the CLI, as a library in
TypeScript and Python. Two entry points: `query()` for one-shot work, and `ClaudeSDKClient` for a
live session you can talk to repeatedly. [[sdk-py]](#s-sdk-py)

The parts that matter for a panel:

- `ClaudeSDKClient` holds a session open; `await client.query(...)` sends a **new user message into
  the live session**, and `await client.interrupt()` stops a running task mid-flight.
- `can_use_tool` is a callback invoked when the permission flow reaches a prompt — full programmatic
  control over every tool call.
- Streaming input accepts an async iterable, so messages can be fed in as they are produced.
- `ResultMessage` carries usage and cost per turn; `AssistantMessage` streams text and tool-use
  blocks.
- `ClaudeAgentOptions` covers `cwd`, `resume`, `fork_session`, `permission_mode`, `allowed_tools`,
  programmatic `hooks` and `agents`, and `setting_sources` to control which settings files load.

**Feasibility sketch.** crabd already runs as a service and already serves a state document and an
action endpoint. A "SideCrab jobs" feature would add: a job registry (name, working directory,
prompt template) in the config file; a `POST /v1/job` that starts a `ClaudeSDKClient`; a job card in
the session grid rendered from streamed messages; `interrupt()` behind a stop control on the card;
and `can_use_tool` routed to the panel — which gives touch-to-approve *for free* on jobs SideCrab
launched, with none of §3.3's ambiguity, because SideCrab owns the session.

**The honest assessment.** This is the largest item in this document and the least aligned. SideCrab
is a panel that watches sessions you started; this makes it a client that starts them. That is a
different product with a different support surface — job definitions, failure handling, log
retention, credential scope — and the panel's whole design rests on *not* being in the critical
path of your work. Two further facts: SDK sessions are separate from the CLI sessions the panel
watches (they will not simply appear as cards without work), and SDK usage consumes plan credits or
API tokens, so a mis-fired job costs real money.

The narrow version is much more defensible: **a small number of user-defined, canned jobs, launched
by tapping a control, with output that lands as a card and a hard stop button.** "Run the tests",
"summarise today's commits". That is a panel feature. A general job runner is a separate product.

| # | Opportunity | Effort | Value |
|---|---|---|---|
| E1 | Canned jobs: a handful of config-defined prompts, launchable from the panel, streamed to a card, interruptible | L | Medium |
| E2 | `can_use_tool` → touch-to-approve for SideCrab-launched jobs only | M (after E1) | Medium |
| E3 | A general job runner / scheduler | L | **Low** — off-mission; recorded so it can be declined deliberately |

---

## 6. Cloud and remote surfaces

**The PRD's claim holds: there is no documented API for cloud session state**, and nothing found in
this pass changes that. Remote Control is the relevant surface, and it is explicitly *not* an
integration point.

**What Remote Control is.** It connects the web and mobile Claude clients to a session running on
your machine, so you can pick up a task from a phone. Execution and filesystem access stay local.
It is available on all plans (off by default on Team and Enterprise until an owner enables it),
requires a subscription login — **API keys are not supported** — and requires talking to the
Anthropic API directly, so it is unavailable behind a gateway or an alternate
`ANTHROPIC_BASE_URL`. [[remote]](#s-remote)

**Why it is not an integration point.** The local session "makes outbound HTTPS requests only and
never opens inbound ports on your machine". There is no local endpoint to observe, no documented
third-party API, and the transport is short-lived scoped credentials. Anything built against it
would be reverse-engineered and would break.

**But there is one concrete, documented, free win.** Hook processes receive:

- `CLAUDE_CODE_REMOTE` — `"true"` when running in a remote web environment;
- `CLAUDE_CODE_BRIDGE_SESSION_ID` — the Remote Control session id, present when Remote
  Control is connected. [[hooks-ref]](#s-hooks-ref)

So SideCrab can badge a card **"being driven remotely"** without touching any private surface. On a
panel whose entire purpose is "which session needs me", knowing that a session is currently being
steered from a phone is directly on-mission — it is the one state where the answer is *not you, not
here*.

One design note from the same doc: `claude remote-control` can run as a server with `--capacity`
defaulting to **32** concurrent sessions and a `--spawn worktree` mode giving each its own git
worktree. SideCrab's grid is designed for 6–8 cards with an overflow chip. A machine running that
mode will exceed it substantially, and the overflow behaviour deserves a test at that scale rather
than an assumption.

| # | Opportunity | Effort | Value |
|---|---|---|---|
| F1 | "Driven remotely" badge from `CLAUDE_CODE_REMOTE` / `CLAUDE_CODE_BRIDGE_SESSION_ID` | S | Medium |
| F2 | Grid behaviour verified at 20–32 concurrent sessions | S | Medium |
| F3 | Deliberately **not**: anything built on Remote Control's transport | — | — |

---

## 7. The iCUE surface SideCrab does not use

SideCrab declares one plugin (Sensors) and one device type (`dashboard_lcd`). The documented
platform is considerably wider. [[icue-spec]](#s-icue-spec)

### 7.1 Other device targets — a mini SideCrab

Three device types are documented: `dashboard_lcd`, `keyboard_lcd` and `pump_lcd`. A widget declares
which it supports in its manifest.

**A keyboard-LCD SideCrab is a real product.** The reference keyboard screen is a 1.9″ panel at
roughly **320 × 170** — about 1/24th the pixel area of the 2560 × 720 panel SideCrab was designed
for. [[vanguard]](#s-vanguard) Nothing about the current layout survives that, and it should not
try to. The right design is a **single-answer panel**: the crab, one number, one state colour.
"Two sessions working, none waiting" — the one question the identity zone already answers, and the
one thing worth glancing at on a keyboard.

The same argument applies to `pump_lcd`, which is smaller still and typically round.

⚠️ **Verify device support before building.** At launch, the reference keyboard family was reported
as configured through a web hub rather than iCUE, with iCUE support planned. [[vanguard]](#s-vanguard)
A widget for a device iCUE does not yet serve is a widget nobody can install. This is a
check-the-device-first item, not a design-first item.

Architecturally this is cheap, which is the appealing part: same `/v1/state` feed, same contract,
same crabd, a second manifest and a second layout. The contract was built for exactly this — the
widget and companion ship separately and additive fields are detected by presence.

### 7.2 Data providers SideCrab does not consume

| Provider | Identifier | What it gives | Fit |
|---|---|---|---|
| **FPS** | `widgetbuilder.fpsdataprovider:Fps:1.0` | `currentFps`, `fpsAvailable`, and **`currentProcess`** — the foreground application's file description, with a `processChanged` signal | **Genuinely good.** The notifier's "panel is out of view" test is currently a heuristic. Knowing the foreground app is a full-screen game is a far better answer to "should this interrupt them" — and it is the same judgement quiet hours already make |
| **Device Action** | `widgetbuilder.deviceactionprovider:DeviceAction:1.0` | `initDevice(deviceId)`, and a `dialTriggered(actionType, dialIndex)` signal for `press` and `long-press` | Acknowledge-all from a physical dial without reaching for the glass. Small, tactile, and it suits the "from across the room" story |
| **Media** | `widgetbuilder.mediadataprovider:Media:1.0` | `songName`, `artist`, and **playback control** — `triggerPlayPause()`, `triggerNextTrack()`, `triggerPreviousTrack()` | Off-mission, but it is what a large share of store widgets do, and it is a candidate for the identity zone's dead space when there are no sessions. A judgement call, not a clear yes |
| **Link** | `widgetbuilder.linkprovider:...` | Cooling-system data | No fit |
| **Stream Deck** | `widgetbuilder.streamdeck:...` | Stream Deck integration | Worth a look — a Stream Deck key that acknowledges a waiting session is the same idea as the dial, on hardware more developers own |

### 7.3 Globals and controls not used

- `iCUE.defaultTemperatureUnit()` returns `"°C"` or `"°F"` from the user's settings — the sensor row
  should honour it rather than picking one.
- `iCUE.iCUELanguage` gives the interface language. `widget/translation.json` already exists, so
  localisation is partly built and unused.
- `iCUE.isPreview` distinguishes preview from device rendering — useful for making the store
  screenshots deterministic.
- `iCUE.fpsLimit` (default 30) is the render budget the animations should be tuned against
  explicitly rather than by feel.
- Unused control types include `tab-buttons`, `slider`, `sensors-factory` and `media-selector`. The
  settings surface today is a small set of switches; `tab-buttons` in particular would let the
  panel offer layout variants without a second widget.
- **Multi-instance:** persisted widget properties live under a per-widget unique id, so two SideCrab
  instances on one device keep separate settings. That makes a "limits only" instance in a narrow
  slot alongside a full instance viable — worth confirming with a spike, since it is the cheapest
  way to serve the smaller layout slots.

| # | Opportunity | Effort | Value |
|---|---|---|---|
| G1 | `keyboard_lcd` mini variant — crab, one number, one state (device availability verified first) | M | Medium–high |
| G2 | FPS provider `currentProcess` → a real foreground/out-of-view test for the notifier | S | Medium–high |
| G3 | Device Action dial → acknowledge-all without touching the glass | S | Medium |
| G4 | Honour `defaultTemperatureUnit()`; light up `translation.json` via `iCUELanguage` | S | Medium |
| G5 | `pump_lcd` variant | M | Low–medium |
| G6 | Media provider in the identity zone when idle | S | Low–medium |
| G7 | Multi-instance / narrow-slot layout via `tab-buttons` | M | Low–medium |

---

## 8. Community landscape

A crowded field, and worth being precise about what it does and does not overlap.

**What exists.** `ccusage` is the best-known: daily / weekly / monthly / session reports, a `blocks`
report for the 5-hour billing windows with a `--live` dashboard showing burn rate and cost
projections, and a status-line integration. [[ccusage]](#s-ccusage) `Claude-Code-Usage-Monitor`
adds burn-rate depletion prediction, P90 analysis to *infer* a custom plan limit, machine-readable
snapshots, exit codes for automation, and provenance labels distinguishing official from estimated
figures. [[ccmonitor]](#s-ccmonitor) `Claude-Code-Agent-Monitor` is the closest structural
relative — hook-fed, tracking sessions, tool usage and subagent orchestration, with a Kanban status
board, notifications, a companion character, and Electron desktop
apps. [[agentmonitor]](#s-agentmonitor) Others in the same space include `ccstatusline`, `ccflare`,
`CCTracker`, `claude-usage` and `MyCCusage`. [[monitors]](#s-monitors)

**What they have that SideCrab lacks, and is worth adopting:**

- **Burn-rate depletion forecast** — "at this rate the 5-hour window is gone by 15:40". SideCrab has
  every input for this (hourly burn buckets, utilisation, reset time) and shows none of it. It is
  the single most useful derived number in the category, and it is a *panel* feature in a way that a
  CSV export is not. Credit where due: the monitor projects lead here.
- **Provenance labelling** — marking a figure official / estimated / stale. This is the honest-failure
  rule expressed one level more finely than SideCrab does it today: the panel currently distinguishes
  known from unknown, but not *measured* from *derived*. With §1 and §2 landing, SideCrab will have
  three sources for overlapping numbers, and saying which one is on screen stops being optional.
- **Weekly and monthly aggregates.** SideCrab has today and a 7-day strip. A month view is a
  reasonable ask; whether it belongs on a glance panel is a genuine question.

**What is deliberately declined:**

- **P90 inference of an unknown plan limit.** It is clever and it is exactly what SideCrab must not
  do. A statistically-guessed limit rendered as a gauge is an invented number wearing the clothes of
  a measured one, and the whole design rule is that unknown renders as an em-dash. When the limit is
  unknown, say so.
- **CSV / warehouse export, multi-agent CLI support, cost-analytics reporting.** All good features
  of a different product. SideCrab is a panel on a desk; a reporting tool it will never be the best
  at, and the tools above already are.
- **A web dashboard.** Several of these ship one. SideCrab's whole premise is that you should not
  have to open a window.

**What SideCrab has that they do not:** the glass — an always-on, out-of-band surface that needs no
window and no attention until it earns it; attention-first design rather than accounting-first; and
a much stricter line on honest failure. Those are the things to protect while adopting the two
items above.

| # | Opportunity | Effort | Value |
|---|---|---|---|
| H1 | Depletion forecast on the limit gauges — "5-hour window exhausted ~15:40 at current burn" | S–M | High |
| H2 | Provenance labelling across the three data sources | S | Medium–high |
| H3 | Weekly / monthly aggregate view | M | Low–medium |
| H4 | Deliberately **not**: P90 limit inference, exports, multi-CLI, a web dashboard | — | — |

---

## 9. Shortlist

Five for the next wave, ordered by value against effort. Each is independently shippable and none
depends on another landing first.

| # | Item | Why |
|---|---|---|
| **1** | **C1 — the status-line channel** | Retires the OAuth-endpoint reach-around for a documented stdin contract carrying `rate_limits` and real context-window percentages. Kills the PRD's number-one risk. Must be opt-in and must chain an existing status line, or it is the most user-hostile change here |
| **2** | **A1 — crabd as an OTLP receiver** | The single biggest data unlock: cost in dollars, spend attributed to subagents / skills / MCP servers, real active time, and first-party error events. crabd is already an HTTP server; the paths are free. Everything else in §1 is a small increment on top |
| **3** | **B1 + B3 + B4 — the hook-fragment refresh** | One file, three cheap wins: `"type": "http"` deletes the `curl.exe` dependency and opens a reply channel; `Notification` matchers replace string-parsing; `StopFailure` finally lets the panel tell a rate-limited session from a finished one |
| **4** | **B2 — decide-from-the-panel via `PermissionRequest`** | The supported mechanism the v2 spike concluded did not exist. Not free-form reply, but the loudest case the panel has. Needs a verified response shape, an allow-list, defer-on-timeout, and off by default |
| **5** | **H1 + H2 — forecast and provenance** | Pure companion-side, no new surface, and it completes the honest-failure story precisely when SideCrab is about to have three overlapping sources for the same numbers. The forecast is the most useful derived number in the category and SideCrab already holds every input |

**Close behind, and cheap:** G2 (foreground-process test for the notifier), F1 (remote-driven
badge), G4 (temperature unit and localisation), B5 (exact subagent counts).

**Bigger bets, deliberately not in the five:** D1 (ship as a plugin) is high value but is packaging
work that wants its own wave. G1 (the keyboard-LCD mini panel) is the most *appealing* item in this
document and should be built the moment the device is confirmed served by iCUE — check the device
first, design second. E1 (SideCrab-launched jobs) is the largest and the least aligned; the canned
form is defensible, the general form is a different product.

**Recorded as declined, with reasons:** general context injection (§3.3), anything on Remote
Control's transport (§6), P90 limit inference and exports (§8), a general job runner (§5).

---

## 10. Sources

Read 2026-08-26. Vendor documentation changes; re-verify before implementing.

- <a id="s-monitoring"></a>**[monitoring]** Monitoring and OpenTelemetry —
  https://code.claude.com/docs/en/monitoring-usage
- <a id="s-statusline"></a>**[statusline]** Status line configuration and stdin JSON —
  https://code.claude.com/docs/en/statusline
- <a id="s-hooks-ref"></a>**[hooks-ref]** Hooks reference: events, payloads, timeouts, hook
  environment variables — https://code.claude.com/docs/en/hooks
- <a id="s-hooks-guide"></a>**[hooks-guide]** Hooks guide: handler types, HTTP hooks,
  `PermissionRequest` decisions — https://code.claude.com/docs/en/hooks-guide
- <a id="s-plugins"></a>**[plugins]** Plugins reference: manifest, hooks, `userConfig`,
  installation — https://code.claude.com/docs/en/plugins-reference
- <a id="s-sdk-py"></a>**[sdk-py]** Agent SDK (Python) reference —
  https://code.claude.com/docs/en/agent-sdk/python · repository:
  https://github.com/anthropics/claude-agent-sdk-python
- <a id="s-remote"></a>**[remote]** Remote Control — https://code.claude.com/docs/en/remote-control
- <a id="s-icue-spec"></a>**[icue-spec]** iCUE widget specification, plugins and references —
  https://docs.elgato.com/icue/widgets/specification/ · https://docs.elgato.com/icue/widgets/ ·
  plugin pages under `https://docs.elgato.com/icue/widgets/references/plugins/`
- <a id="s-vanguard"></a>**[vanguard]** Keyboard-LCD device: 1.9″ 320 × 170 panel; iCUE support
  status at launch — https://www.corsair.com/us/en/explorer/gamer/keyboards/corsair-vanguard-96-wireless-everything-you-need-to-know/
  · https://help.corsair.com/hc/en-us/articles/40445525416593-Keyboard-Vanguard-96-and-Vanguard-Pro-96-and-iCUE-and-Stream-Deck-Support
- <a id="s-ccusage"></a>**[ccusage]** ccusage — https://github.com/ccusage/ccusage ·
  https://ccusage.com/guide/statusline
- <a id="s-ccmonitor"></a>**[ccmonitor]** Claude-Code-Usage-Monitor —
  https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor
- <a id="s-agentmonitor"></a>**[agentmonitor]** Claude-Code-Agent-Monitor —
  https://github.com/hoangsonww/Claude-Code-Agent-Monitor
- <a id="s-monitors"></a>**[monitors]** Others surveyed: ccstatusline
  (https://www.npmjs.com/package/ccstatusline) · CCTracker
  (https://github.com/miwidot/cctracker) · claude-usage (https://github.com/phuryn/claude-usage) ·
  MyCCusage (https://github.com/i-richardwang/MyCCusage) · cc-usage-monitor
  (https://github.com/contiamo/cc-usage-monitor)
