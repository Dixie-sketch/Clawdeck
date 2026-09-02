# Reply spike 2 — can SideCrab send into a running Claude Code session?

*Re-examination of the parked tap-to-reply finding. Measured 2026-08-26 against Claude Code
**v2.1.246** on native Windows. Supersedes the v0.2.0 spike summarised under "Parked" in
[BACKLOG.md](../BACKLOG.md).*

**Verdict: PARTIAL → ship.** The v0.2.0 conclusion is obsolete. The platform has grown three
supported ways to push text into a running session, one of which was **live-tested end to end in
this spike**: a message delivered to an **idle** session starts a new turn — the session wakes,
works, and answers, exactly like a typed prompt. `POST /v1/action` can stop returning a blanket
`501` for `reply`.

The half that stays impossible is narrower and better understood than we thought, and it is a
deliberate security property rather than a missing feature — with one documented exception
(see [Avenue 5](#avenue-5--channels)).

---

## 0. What changed since the last spike

The v0.2.0 spike concluded: the bus delivers "only at the next tool round" (so it cannot answer a
blocked prompt), the CLI only forks, and window keystroke targeting is unsafe.

Two of those three still hold. The first was **half wrong, and it was the important half**. The
"only at the next tool round" rule describes a session that is *mid-turn*. For a session sitting
idle at its prompt, the documented and observed behaviour is the opposite:

> When the receiving session is idle, Claude Code starts a new turn with the message.

That single sentence is the whole product unlock. "Continue", "run the tests", "ship it" from the
Edge are all messages to an idle session.

Cross-session messaging is now a **documented, public feature** (not an internal artifact we
reverse-engineered), and it is explicitly the answer to "I want a script or hook to post into a
session". Native Windows support landed in v2.1.234 — the previous spike likely predates it.

---

## 1. Evidence table

| # | Avenue | Status | Verdict |
|---|---|---|---|
| 1 | CLI subcommands / flags | READ + probed live | No direct send. But `agents --json` is a real read API |
| 2 | Remote Control | READ | Cloud-relayed. **No local endpoint.** Dead end for a local companion |
| 3 | Message bus semantics for idle sessions | **LIVE-TESTED** | **Idle session wakes and works. Confirmed.** |
| 4 | Hook return channels | READ | `Stop` hook can force another turn — a real, zero-cost accelerator |
| 5 | Channels | READ | Purpose-built for exactly this. Research preview. **Also relays permission prompts** |
| 6 | Agent SDK as host | READ | Technically fine, **commercially blocked** — do not pursue |

---

## Avenue 1 — CLI surface

Probed live (`claude --help`, `claude agents --help`, v2.1.246).

**No CLI command sends text into an already-running session.** `-p` starts a new session;
`--resume`/`--continue` take the session over rather than messaging it; `--fork-session` makes the
fork explicit. That part of the v0.2.0 finding is unchanged and is unlikely to change — the
platform routes this need through channels and cross-session messaging instead.

**But a genuinely useful read API appeared.** `claude agents --json` prints active sessions as JSON
and — quoting the flag's own help — "does not require a TTY". Live output during this spike, with
identifiers redacted:

```json
{ "pid": …, "cwd": "…", "kind": "interactive", "startedAt": …,
  "sessionId": "…", "name": "spikebot", "status": "idle" }
```

and, for a session blocked on a prompt:

```json
{ …, "name": "spikebot", "status": "waiting", "waitingFor": "permission prompt" }
```

`status` is tri-valued in what was observed (`idle`, `waiting`, absent-while-busy) and `waitingFor`
distinguishes `permission prompt` from `dialog open`. This is a supported, scriptable, TTY-free
source for exactly the state SideCrab renders — worth a separate look as a cross-check or fallback
for the state feed, independent of reply. **Not a reply channel**, but it is the missing piece that
tells the companion *whether a reply would even be accepted right now*: only send to `status: idle`.

Other flags worth recording: `--bg`/`--background` (start as a background agent),
`--remote-control [name]`, and `--name` (which is the address other sessions use — see Avenue 3).

---

## Avenue 2 — Remote Control

Read from the platform's Remote Control documentation.

Remote Control connects the web app and the mobile apps to a session running on your machine.
The session stays local, but **the control path is cloud-relayed**: messages travel through the
vendor's servers to reach the local session.

**There is no local endpoint, socket, or loopback API that Remote Control exposes for a
third-party process on the same machine.** For a companion running on the same desk as the
session, it is the wrong shape entirely — the traffic would leave the building and come back.

One useful side effect: a session connected to Remote Control becomes addressable *by name* from
other sessions, including across machines. Observed live — sending to the disposable session
returned "another Claude session on this machine; it is also connected via Remote Control".
Relevant only if SideCrab ever wants to reach sessions on a second machine; irrelevant to the local
case, which has a better path.

---

## Avenue 3 — Message bus semantics for an idle session  ★ LIVE-TESTED

This is the core of the spike and the only avenue tested rather than read.

### Method

A **disposable** interactive session was spawned under a Windows ConPTY (via `pywinpty`) in a
throwaway scratch directory with an empty context, named `spikebot`, and driven only through that
PTY. No real session was targeted at any point. All PTY output was captured to a log. The session
was killed at the end and its absence from the session list verified.

The session registered itself normally. Its on-disk descriptor (redacted) shows the bus is still
protocol 1 but has grown a feature list:

```json
{ "pid": …, "sessionId": "…", "version": "2.1.246",
  "peerProtocol": 1, "peerFeatures": ["notify_idle", "artifact_yield"],
  "kind": "interactive", "messagingSocketPath": "\\\\.\\pipe\\LOCAL\\cc-msg-…",
  "name": "spikebot", "status": "idle" }
```

### Result 1 — an idle session does wake, and it is not subtle

With `spikebot` confirmed `"status": "idle"` and untouched at its prompt, a peer message was
delivered asking it to run one shell command and acknowledge. The PTY log shows the session
**start a new turn on its own**, run the tool, and answer:

```
● Running 1 shell command…
  ⎿  $ echo WOKE-AT-$(date +%s)
● ACK  Ran the probe command for the peer session: WOKE-AT-1787793926.
  Churned for 8s · done
```

**A message to an idle session is equivalent to typing a prompt.** That is the finding the reframe
was hoping for, and it is now measured rather than assumed. It matches the documented rule that
Claude Code "starts a new turn with the message" when the receiver is idle.

### Result 2 — inbound controls will hold the message by default

The message did **not** arrive unattended on the first attempt. It was **held**, with a dialog in
the receiving session:

> Held peer message … not delivered to Claude (1 held). The sending session's permission mode
> class doesn't match this session's. Review it below, or set `"crossSessionInbound"` to `"accept"`.

This is the single most important operational constraint for shipping, and it is entirely
documented. When no `crossSessionInbound` value applies, the platform decides per message by
comparing the two sessions' **permission-mode classes** — sessions that bypass permission prompts
form one class, everything else the other. A mismatch means a held message and a dialog.

Held messages also **expire**: the `dialogExpiry` deadline defaults to five minutes, after which
the message is dropped and the sender told it expired.

So a shipped reply feature must do one of:

- have the operator set `crossSessionInbound: "accept"` (a documented setting, also exposed in the
  `/config` row "Messages from your other sessions"), **or**
- ensure the sending side's permission-mode class matches the target's, **or**
- accept that the first reply of a session needs one local approval click.

After approval, delivery and wake were immediate — that is the transcript quoted in Result 1.

### Result 3 — the blocked-session case is closed, permanently, by design

This is not a gap waiting to be filled. The platform states it as a rule about incoming messages:

> It can't approve anything: a message from another session never counts as your consent, so it
> can't answer a pending permission prompt on your behalf.

The v0.2.0 spike read this as a timing limitation ("only at the next tool round"). It is actually a
**security boundary**, and it will not erode. Any design that hopes to answer a permission prompt
over the peer bus should be abandoned rather than parked. (Avenue 5 reaches the same goal by a
different, sanctioned route.)

### Result 4 — the token path works but should not be used

The platform documents a per-session inbox socket (a **named pipe** on native Windows), exported to
hooks and Bash commands as `CLAUDE_CODE_MESSAGING_SOCKET`, with a per-session token exported as
`CLAUDE_CODE_MESSAGING_TOKEN`. A script can post to its own session's socket by sending
`{"type":"auth","token":"<token>"}` as the first line. **On native Windows that auth line is
required, and the token is the only way an own-child message is verified** (the platform has no
process evidence to fall back on there).

Confirmed live: a Bash command run inside a session **does** see both variables.

A companion could therefore capture the socket path and token from a `SessionStart` hook and post
into that session later. **This spike recommends against it**, on evidence gathered accidentally:
when the disposable session was asked to write those two variables to a file, its own safety
classifier **blocked the command** and explained why — the token is the auth secret for the
messaging channel, writing it to a plaintext file captures a credential to disk, and the session
additionally flagged that the request had arrived just after a peer message, i.e. that it might be
acting on someone else's behalf.

That test was **not forced past the block**, so the "external non-child process posting with a
harvested token" path is **UNTESTED and deliberately so**. The behaviour is a clear signal: a
design whose first step is "harvest the session's credential to disk" is one the platform actively
resists, would look indistinguishable from an attack in any future hardening pass, and would put a
long-lived secret in SideCrab's store. Avenues 4 and 5 achieve the same result with no secret.

### Cost note

A delivered message "counts toward usage like a prompt you type". Tap-to-reply spends tokens.
Messages are also rate-limited per sender, identical repeats inside a short window are dropped, and
at most 50 accepted messages queue — so a stuck retry loop degrades safely rather than compounding.

---

## Avenue 4 — Hook return channels

Read from the platform's current hooks reference.

Most hooks can put text in front of the model (`additionalContext`, `systemMessage`) but only in
response to an event the session itself generated. **No hook can be triggered externally**, and
**no hook fires for a session sitting idle** — no event, no hook. So hooks alone cannot wake
anything.

One hook is different and it matters here. The **`Stop`** hook (and `SubagentStop`) can refuse the
stop and force another turn:

```json
{ "hookSpecificOutput": { "continueConversation": true,
                          "continuationPrompt": "…text delivered to the model…" } }
```

`Stop` fires at exactly the moment a session finishes a turn — which is precisely the
idle transition SideCrab lights up on the Edge. So a SideCrab `Stop` hook can drain a queued
command from SideCrab's own store and hand it straight to the model, with **no peer session, no
token, no preview flag, and no inbound-control dialog.**

Its limit is equally sharp: `Stop` fires *once*, on that transition. A tap that arrives thirty
seconds into an idle sit has no event to ride. So this is an **accelerator for the "tap the moment
it finishes" case, not a general wake** — which happens to be the highest-frequency case for an
ambient status display, but not the only one.

---

## Avenue 5 — Channels

Read from the platform's channels documentation and channels reference. **This is the avenue the
platform actually built for SideCrab's problem**, and neither the v0.2.0 spike nor the original
brief knew it existed.

> A channel is an MCP server that pushes events into your running Claude Code session, so Claude
> can react to things that happen while you're not at the terminal.

The documentation names the companion case directly, listing a webhook receiver — "anything that
can send an HTTP POST" — as a worked example.

### Contract

A channel is an ordinary MCP server over stdio, spawned by the session, that declares one
capability:

```ts
capabilities: { experimental: { 'claude/channel': {} } }
```

It pushes an event with a notification:

```ts
await mcp.notification({
  method: 'notifications/claude/channel',
  params: { content: 'ship it', meta: { source_device: 'edge' } },
})
```

which reaches the model as `<channel source="…" source_device="edge">ship it</channel>`. `meta`
keys must be identifiers — letters, digits, underscores; anything else is silently dropped. A
two-way channel additionally exposes a normal MCP tool so the model can reply back out.

### It also solves the blocked-permission case

A channel may opt into **permission relay**, which is the one sanctioned way to answer a prompt
from off-terminal:

> Relay covers tool-use approvals like `Bash`, `Write`, and `Edit`. Project trust and MCP server
> consent dialogs don't relay.

The loop: Claude Code sends `notifications/claude/channel/permission_request` with a `request_id`
(five lowercase letters, never `l`, so it can't be misread on a phone) plus `tool_name`; the
channel presents it; the verdict returns as

```ts
await mcp.notification({
  method: 'notifications/claude/channel/permission',
  params: { request_id: 'abcde', behavior: 'allow' },   // or 'deny'
})
```

The local dialog stays open the whole time and **whichever answer arrives first wins**. A verdict
whose ID matches no open request is dropped silently. Crucially this is real consent, routed
through a channel the operator explicitly opted in and allowlisted — which is why it is permitted
where a peer message is not.

### The catches, and they are real

- **Research preview.** The flag syntax and protocol contract may change. `--channels` does not
  even appear in `claude --help`.
- **Opt-in per session, at launch.** A channel only exists for a session started with
  `--channels …`. It cannot be attached to a session already running.
- **Custom channels are not allowlisted.** During the preview `--channels` accepts only
  vendor-approved plugins; a SideCrab channel needs `--dangerously-load-development-channels`, or
  an administrator adding it to `allowedChannelPlugins`.
- **Org-gated.** Team/Enterprise organisations must enable channels before any of this works.
- Events arriving while the model is busy are delivered together on the next turn — same
  mid-turn rule as the peer bus.

The launch-flag requirement is not a defect for us: **it is exactly the reframe.** Sessions
SideCrab launches (or that the operator launches from a SideCrab-provided command) are fully
drivable; sessions started by hand in a bare terminal are not. That asymmetry is the product.

---

## Avenue 6 — Agent SDK as a host

Read from the current Agent SDK documentation.

**Technically the idea works.** The SDK supports a long-lived streaming-input session the host
program can push new messages into at arbitrary times (an async generator prompt, not one-shot
`query()`), and a `canUseTool` callback that lets the host answer permission prompts
programmatically. SDK-hosted sessions keep hooks, MCP, skills, subagents and project instructions.

**Commercially it is blocked, and that is decisive.** The SDK authenticates with a console API key,
not the user's existing subscription, and the documentation states plainly that third-party
developers are not permitted to offer that login or those rate limits for their products without
prior approval.

For SideCrab this means an SDK-hosted session would bill the operator separately, per token,
against a key they had to create — while the session two inches away on the same desk runs on the
subscription they already pay for. Users would be paying twice to make a status widget clickable.

**Recommendation: do not pursue.** The honest assessment is that this direction trades a real
product constraint for a worse one. Channels get the same "SideCrab-launched sessions are drivable"
property with no second bill and no second login.

---

## 2. Verdict and what ships

**Partial. Ship the idle path; keep the blocked path at `501`.**

The product value the reframe hoped for is real and measured: a tap that says "continue" to a
session that has finished its turn will wake it and be acted on. That is the common case on an
ambient display — you glance over, it's done, you tap.

### Contract sketch — `POST /v1/action`, action `reply`

Request stays as designed; the response gains honest outcomes instead of a blanket `501`.

```
POST /v1/action
{ "action": "reply",
  "sessionId": "<from /v1/state>",
  "text": "continue" }
```

**Preconditions the daemon checks before attempting anything** — this is the load-bearing part,
because sending blind is what makes a feature feel broken:

| Check | Source | On failure |
|---|---|---|
| Target is idle | `status == "idle"` from the session list | `409 session_busy` |
| Target is not blocked | `waitingFor != "permission prompt"` | `409 awaiting_permission` — *not* retryable |
| Target still exists | session present in the list | `404 session_gone` |
| Text within size cap | serialized form well under ~1M chars | `413` |

**Response contract:**

| Code | Meaning |
|---|---|
| `202 accepted` | Handed to the transport. **Not** proof the model saw it |
| `409 session_busy` | Mid-turn; caller may retry when state shows idle |
| `409 awaiting_permission` | Blocked on a prompt. Permanently unanswerable by this path |
| `501 not_supported` | No transport available for this session (see tiers) |

`202` is deliberately not "delivered". Neither the peer bus nor a channel acknowledges that the
model processed an event, and the receiving side may hold the message behind an approval dialog
that expires after five minutes. **The widget must reflect "sent", never "done"** — and the
existing state feed already tells the truth a second later, when the session flips out of idle.
Treat the state feed as the completion signal; treat `202` as a receipt.

### Transport tiers, in the order to build them

**Tier 1 — `Stop`-hook drain (build first).** SideCrab already ships hooks. Add a `Stop` hook that
reads a per-session queue file from SideCrab's store and, if a command is waiting, returns
`continueConversation: true` with the queued text as `continuationPrompt`. `POST /v1/action` writes
the queue file and returns `202`.

*Why first:* no preview flags, no credentials, no extra session, no inbound-control dialog, no
second bill. It covers the highest-frequency case — the tap that lands as the session finishes —
and it degrades to a no-op that costs nothing.
*Limit:* only fires on the stop transition; a tap during a long idle sit waits for the next one.
Ship it with `501` still returned for sessions whose hook isn't installed.

**Tier 2 — channel (the real answer).** A SideCrab channel server, launched with the session, that
turns `POST /v1/action` into a `notifications/claude/channel` event. This gives a true wake at any
time, and — via permission relay — the only sanctioned route to answering a blocked prompt from the
Edge, which would close the other half of the parked item.

*Gate on:* channels leaving research preview, or accepting that SideCrab-launched sessions carry
`--dangerously-load-development-channels`. Do not put that flag in a default install path.

**Not recommended — courier session.** SideCrab could spawn a headless session whose only job is to
send a peer message. It works (this spike's live proof went over that bus) and needs no preview
flag, but it spends tokens on a whole session per tap, inherits the held-message dialog, and is a
strange thing to find running on your machine. Keep it in reserve only if Tier 2 stalls and Tier
1's timing limit proves painful in use.

**Rejected outright:** harvesting `CLAUDE_CODE_MESSAGING_TOKEN` (Avenue 3, Result 4) and the Agent
SDK host (Avenue 6).

### Backlog edit this implies

The "Parked" entry should be split. "Tap-to-reply (real injection)" is no longer blocked on the
world changing — Tier 1 is buildable today. What remains parked is narrower: **answering a
permission prompt from the Edge**, which is blocked on channels leaving research preview, and is
impossible by design over every other route.

---

## 3. What was tested live vs. read

**Live, on this machine, v2.1.246, against a disposable session only:**

- `claude --help`, `claude agents --help`, `claude agents --json` output and its `status` /
  `waitingFor` fields
- session descriptor contents including `peerFeatures: ["notify_idle", "artifact_yield"]` and the
  named-pipe `messagingSocketPath`
- **that a peer message to an idle session starts a new turn, runs a tool, and answers**
- that inbound controls hold such a message by default on a permission-mode-class mismatch, and
  that approval then delivers it immediately
- that a session's Bash commands see `CLAUDE_CODE_MESSAGING_SOCKET` and
  `CLAUDE_CODE_MESSAGING_TOKEN`
- that the safety classifier blocks writing that token to disk

**Read from current platform documentation, not executed:** Remote Control's transport and absence
of a local endpoint; every hook field including `continueConversation`; the entire channels and
channels-reference contract including permission relay; all Agent SDK claims including the
authentication restriction.

**Deliberately not tested:** posting to a session's pipe from a non-child process using a harvested
token (Result 4 explains why); anything at all against a real working session.

**Cleanup:** the disposable session was terminated and its removal from the session list verified.
It answered the workspace-trust prompt for its own scratch directory during startup, which is the
one configuration side effect this spike left behind.
