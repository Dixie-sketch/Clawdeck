# Security

## Threat model, in one paragraph

Everything runs on one machine. The companion (`crabd`) listens on `127.0.0.1:9999` only, never
on a LAN interface, and as of crabd 0.31.0 it also SERVES THE PANEL itself on that port, so the
panel is an ordinary page in a browser rather than only a widget inside iCUE. It reads
`~/.claude` (session transcripts, hook payloads, the usage endpoint's OAuth token) strictly
read-only, never writes there, and never logs or transmits the token. The panel, the notifier
and the glow are read-only consumers of the same localhost feed. Nothing in this repo makes a
network request to anywhere but `127.0.0.1` and, for usage limits, Anthropic's own API with the
user's own token. There is no telemetry, no crash reporting, no update check.

The two things an attacker on this machine, or a web page you visit, could want are:

1. **read** what your sessions are doing (`/v1/state`), and
2. **write** to a session - queue a canned "continue" prompt, or approve a pending permission.

## What is enforced

- **`Host` allowlist, ahead of every other gate (crabd 0.31.0).** A present `Host` whose host
  part is not `localhost`, `127.0.0.1` or `[::1]`, or whose port part is present and is not the
  bound port, is refused `403 {"error":"host not allowed"}` with no CORS header, on `GET`, `POST`
  and `OPTIONS` alike; an absent `Host` (HTTP/1.0, a hand-rolled probe) is allowed. This is the
  DNS-rebinding gate, and it is the only one that can be: a page at `http://evil.example:9999`
  whose name re-resolves to `127.0.0.1` is SAME-ORIGIN with crabd as far as the browser is
  concerned, so its `GET` carries no `Origin` at all and the origin allowlist has nothing to
  refuse it with. `docs/STATE-CONTRACT.md` v0.31.0 §2.
- **Origin allowlist on every route, reads and writes.** The only `http(s)` origins accepted are
  the three spellings of this crabd's own bound port (`http://localhost:<port>`,
  `http://127.0.0.1:<port>`, `http://[::1]:<port>`), matched exactly and case-insensitively.
  Every other web origin - including the same host on a different port, and the same authority
  over `https` - is refused `403` with no CORS header, so a visited web page cannot read the
  state or post an action through a cross-origin fetch. `Access-Control-Allow-Origin: *` is
  illegal on every route, method and status. `docs/STATE-CONTRACT.md` v0.31.0 §3.
- **Every POST carries `X-SideCrab-Panel` (crabd 0.31.0).** Any non-empty value; a POST without
  it is refused `403 {"error":"panel header required"}`, on every path including the unknown
  ones. It is not a secret and the value is never read. What it does is make the request
  NON-SIMPLE, so a browser must preflight it - and the preflight only lists the header for an
  origin the allowlist already trusts or a non-web scheme, never for `null`.
  `docs/STATE-CONTRACT.md` v0.31.0 §4.
- **The panel crabd serves is confined to its own directory.** `GET /` serves `index.html`, and
  only a path whose first segment is `styles`, `scripts`, `resources` or `mock` serves a file;
  everything else under the panel root is `404`. One percent-decode, then a refusal of `..`
  segments, backslashes, NULs, empty segments, dot-segments and surviving `%`; the resolved
  candidate must sit under the resolved panel directory, which also refuses a symlink pointing
  out of the tree. Static replies carry `X-Content-Type-Options: nosniff` and
  `Cache-Control: no-store`, and the same origin gate as the API.
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
  *(The iCUE-property sentence is being superseded: a panel served by crabd in a browser has no
  iCUE properties to hold the code in. How the browser panel obtains it is decided in a later
  phase of the macOS port, and this paragraph will be rewritten with that section. Everything
  else about the gate - the space, the compare, the lockout, the `requestId`, the `503` - is
  unchanged.)*
- **Where the browser panel keeps the pairing code (widget 0.30.0), and how that guarantee is
  weaker.** The panel crabd serves stores the code in `localStorage` on the origin
  `http://localhost:9999`, inside the one namespaced object it keeps its settings and display
  state in. What that buys: only a page on the panel's own origin can read it, so no other page
  the operator visits can - the same-origin policy is the mechanism, and it is the browser's,
  not ours. A same-user local process can read it, but that was always true: the code is a file
  at `~/.sidecrab/panel-token` that any such process can open, which is why it is a disclosed
  residual below rather than a new one.

  **This is weaker than the iCUE-property claim it replaces, and deliberately so.** An iCUE
  property lives in a host process no web content can reach at all; `localStorage` is reachable
  by any script that runs on that origin. So an XSS in the panel itself, or a browser extension
  with storage access, could read the code - neither of which could touch the property form.
  There is no version in which **the page itself holds the secret** that avoids this: a value
  the page can send is a value the page can read.

  **There is a version in which the page does not hold it, and it was not taken.** crabd could
  mint an `HttpOnly` session cookie on the panel's own origin; the browser would attach it to
  every `decide` and no script on the page - ours, an injected one, or an extension's - could
  read it back. That is strictly stronger and it is the obvious next move. It is not this
  change because it is a different feature: it needs a cookie the daemon issues and rotates, a
  pairing flow that is a request rather than a value the operator types, and a `decide` wire
  shape without a `token` member - and this port deliberately changed neither the pairing flow
  nor the wire. Recorded as a residual below rather than done badly here.

  What is unchanged is everything the code is one factor of. A `decide` still needs the code AND
  a `requestId` that matches the request the panel is showing, checked under the lock that
  applies the decision; ten rejects a minute lock the gate for a minute; the daemon is bound to
  loopback and refuses every web origin but its own. Reading the code is not by itself a
  decision, and the vectors that could read it are vectors that could already drive the panel.
- **The optional long-lived limits token is DPAPI-protected.** `Install-SideCrab.ps1 -LimitsToken`
  stores a `claude setup-token` value in `~/.sidecrab/limits-token.dpapi`, encrypted for the
  current Windows user (no entropy); crabd decrypts it in memory per poll, sends it only to
  Anthropic's usage endpoint over HTTPS, and never logs, serves or copies it. Revoke it from
  your Anthropic account settings; delete the file to stop using it.
- **Atomic config writes, bounded caches, never-500.** A failed config write cannot empty
  `config.json`; every ring and LRU is capped so a flood cannot grow memory; malformed input is
  answered with a 4xx and a sanitised body.

## Disclosed residuals (open)

- **A same-user local process can read the pairing code.** It can also read `~/.claude`, drive
  the terminal dialog and inject keystrokes, so it was never inside the threat model of a
  localhost service; the gate exists for the web-page vector, which it closes.
- **The browser panel holds the pairing code where its own page can read it** (widget 0.30.0).
  `localStorage` on `http://localhost:9999`, so an XSS in the panel or an extension with
  storage access could read it, where an iCUE property could not. An `HttpOnly` cookie minted
  by crabd would avoid it and is the intended next move; it is a change to the pairing flow and
  the `decide` wire shape, neither of which this port touched. See "Where the browser panel
  keeps the pairing code" under **What is enforced** for the full argument and for what the
  code is only one factor of.
- **`/v1/state` is readable by a forged `null` Origin.** Still open, and still deliberate: a
  QtWebEngine build that collapses its page to an opaque origin has no other value it could
  send, so refusing `null` risks refusing the product on a build nobody has measured. (The one
  build that HAS been measured reports `file://` - ORIGIN-b.) It discloses what your sessions
  are doing, not a way to act on them.
- ~~**A forged `null` Origin can WRITE - queue-continue, and any other POST.**~~ **CLOSED in
  crabd 0.31.0 (2026-09-04)** by the panel header. A custom request header makes the POST
  non-simple, so the browser must preflight it, and `do_OPTIONS` never lists
  `X-SideCrab-Panel` for `Origin: null` - a forged-null page therefore comes back from its
  preflight without permission to send the header its POST needs, while `null` READS stay
  allowed. The iCUE build itself is unaffected because its origin was measured as `file://`,
  not `null` (ORIGIN-b, `originsSeen` 2026-09-02), and a web page cannot forge that; a build
  that reports `null` instead would keep its reads and lose its taps, which is the accepted
  trade rather than an oversight. Pinned by `companion/tests/test_crabd_panel.py`:
  `PanelPreflightTests.test_null_may_never_unlock_the_header`,
  `PanelHeaderGateTests.test_a_post_without_the_header_is_refused_with_its_own_body` and
  `.test_every_post_path_requires_it_including_the_unknown_ones`.
- **queue-continue is still on and unauthenticated for a LOCAL process.** Bounded by the
  server-side whitelist: the worst case is a canned "Continue" / "Run the tests" / "Commit +
  push" pushed into a live session by something already running as you. Not remote code
  execution; still a nudge you did not send. The browser half of this is now closed by the
  header gate above.
- ~~**A page whose own DNS re-resolves to 127.0.0.1 can read the feed.**~~ **CLOSED in crabd
  0.31.0 (2026-09-04)** by the `Host` allowlist above. Nobody had written this one down, because
  it costs nothing while the panel is a widget loaded from disk - and everything the moment
  crabd serves a page on a real web origin, which is the same release. Pinned by
  `companion/tests/test_crabd_panel.py` `HostAllowlistTests`:
  `test_a_rebound_hostname_is_refused`, `test_a_loopback_name_on_the_wrong_port_is_refused`,
  `test_the_host_gate_answers_before_the_origin_gate`.
- **The panel lives on a real web origin, reachable by any browser on the machine.** That is the
  new exposure and it is stated plainly: `http://localhost:9999` is a page anything can navigate
  to. What that buys an attacker is bounded by the three gates. A page the operator opens cannot
  READ the feed cross-origin (it is not on the origin allowlist), cannot WRITE (its preflight is
  refused, so it never obtains the header), and cannot make itself same-origin by rebinding its
  own name onto 127.0.0.1 (the `Host` gate). A same-user local process still can do all three -
  it can also read `~/.claude`, drive the terminal dialog and inject keystrokes, so it was never
  inside the threat model. Approvals additionally need the pairing code, a matching `requestId`, and
  survive the lockout - unchanged.

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
