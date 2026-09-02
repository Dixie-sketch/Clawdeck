# Security

## Threat model, in one paragraph

Everything runs on one PC. The companion (`crabd`) listens on `127.0.0.1:2722` only, never on a
LAN interface. It reads `~/.claude` (session transcripts, hook payloads, the usage endpoint's
OAuth token) strictly read-only, never writes there, and never logs or transmits the token. The
widget, the notifier and the glow are read-only consumers of the same localhost feed. Nothing in
this repo makes a network request to anywhere but `127.0.0.1` and, for usage limits, Anthropic's
own API with the user's own token. There is no telemetry, no crash reporting, no update check.

The two things an attacker on this machine, or a web page you visit, could want are:

1. **read** what your sessions are doing (`/v1/state`), and
2. **write** to a session - queue a canned "continue" prompt, or approve a pending permission.

## What is enforced

- **Origin gate on every route, reads and writes.** Any request carrying an `http(s)` `Origin`
  header is refused with `403` and no CORS header, so a visited web page cannot read the state or
  post an action through an ordinary cross-origin fetch. `docs/STATE-CONTRACT.md` "TRANSPORT".
- **Fixed vocabulary, never free text.** `POST /v1/action` accepts acknowledge, dismiss, pin,
  one of the configured continue prompts, and approve/deny. `reply` with arbitrary text answers
  `501` and will keep doing so until a supported injection mechanism exists.
- **Panel approvals ship OFF.** The installer asks. Every failure mode of the approval path
  (timeout, no tap, disabled, malformed, companion down) is a pass-through to the normal terminal
  dialog. The terminal dialog is raced, not suppressed.
- **Never auto-allow.** crabd has no code path that answers "allow" without a `decide` request
  arriving first.
- **A `decide` needs the pairing code and the request id (crabd 0.29.0).** crabd mints a
  ten-symbol code into `~/.sidecrab/panel-token` (2^50 space, constant-time compare, ten
  rejects a minute lock the gate for a minute; the code is never served, `/v1/health` reports
  presence and lockout only). The widget holds it as an iCUE property, which no web page can
  read. Each pending request carries a `requestId` the tap must echo, checked under the same
  lock that applies the decision. A companion with no gate object answers `503`, never `204`.
- **Atomic config writes, bounded caches, never-500.** A failed config write cannot empty
  `config.json`; every ring and LRU is capped so a flood cannot grow memory; malformed input is
  answered with a 4xx and a sanitised body.

## Disclosed residuals (open)

- **A same-user local process can read the pairing code.** It can also read `~/.claude`, drive
  the terminal dialog and inject keystrokes, so it was never inside the threat model of a
  localhost service; the gate exists for the web-page vector, which it closes.
- **`/v1/state` is readable by a forged `null` Origin** (the original SEC-4 read gate refuses
  `http(s)` origins only). It discloses what your sessions are doing, not a way to act on them.
- **queue-continue is always on and unauthenticated.** Bounded by the server-side whitelist: the
  worst case is a canned "Continue" / "Run the tests" / "Commit + push" pushed into a live
  session by a local process or a forged-origin page. Not remote code execution; still a nudge you
  did not send.

## Closed

- **SEC-a - forged `null` Origin could approve a pending permission** (2026-08-28 audit). The
  widget's opaque QtWebEngine origin serialises as `Origin: null`, which a sandboxed iframe can
  forge, so the origin gate alone could not tell them apart. **Closed in crabd 0.29.0 / widget
  0.27.0** by the pairing code above: the forged page has the origin but not the code.
- **WID-a - no per-request id on `decide`.** Closed in the same release: `pendingPermission.requestId`
  is echoed by the tap and a mismatch is `409`, decided under the broker's lock.

The full audit trail, including the findings that are now fixed (SEC-1 to SEC-5), is in
`docs/findings/audit-security.md` and `docs/findings/QA-Audit-2026-08-28.md`.

## Reporting a vulnerability

Open a private report through GitHub's "Report a vulnerability" on
<https://github.com/Dixie-sketch/Clawdeck/security>, or open an issue if the finding is not
exploitable. Say what you observed, how to reproduce it, and which component and version
(`widget/manifest.json` version, `crabd --version`). There is no bug bounty; there is a
maintainer who will read it.
