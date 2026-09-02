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
- **Atomic config writes, bounded caches, never-500.** A failed config write cannot empty
  `config.json`; every ring and LRU is capped so a flood cannot grow memory; malformed input is
  answered with a 4xx and a sanitised body.

## Disclosed residuals (open)

- **SEC-a - forged `null` Origin while approvals are ON.** The widget runs inside iCUE's
  QtWebEngine from an opaque origin, so its requests carry `Origin: null`, and crabd must accept
  that value or the widget cannot POST. A sandboxed `allow-scripts` iframe on any page you visit,
  or any local process, can send the same header. With `panelApprovals` enabled, such a caller can
  read `/v1/state` and `decide:allow` a pending permission you never tapped. **Mitigation today:
  leave approvals off** (the default). Candidate fix: a per-request id/nonce on
  `pendingPermission` that `decide` must echo, delivered by a path a forged-origin caller cannot
  read.
- **WID-a - no per-request id on `decide`.** Within the widget's poll interval a tap could land
  on a different pending tool than the one displayed. Same fix as above.
- **queue-continue is always on and unauthenticated.** Bounded by the server-side whitelist: the
  worst case is a canned "Continue" / "Run the tests" / "Commit + push" pushed into a live
  session by a local process or a forged-origin page. Not remote code execution; still a nudge you
  did not send.

The full audit trail, including the findings that are now fixed (SEC-1 to SEC-5), is in
`docs/findings/audit-security.md` and `docs/findings/QA-Audit-2026-08-28.md`.

## Reporting a vulnerability

Open a private report through GitHub's "Report a vulnerability" on
<https://github.com/Dixie-sketch/Clawdeck/security>, or open an issue if the finding is not
exploitable. Say what you observed, how to reproduce it, and which component and version
(`widget/manifest.json` version, `crabd --version`). There is no bug bounty; there is a
maintainer who will read it.
