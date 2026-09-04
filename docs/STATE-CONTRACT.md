# SideCrab state contract — `/v1/state` (schema 5-compat, feature-detected)

> **VERSIONING REWORK (v0.6.1/crabd 0.6.1, 2026-08-26).** The strict schema whitelist coupled
> every crabd deploy to a console-bound widget import (schema N+1 bricked the on-glass widget
> until someone stood at the desk). New policy:
> - `"schema"` now marks the last **BREAKING** shape — pinned at **5** until a break actually
>   happens. Additive features (contextTokens, fleet, and everything after) are detected by
>   FIELD PRESENCE, never by schema number.
> - The widget accepts `schema` 1–5; a value above its ceiling is still a dead feed (that's
>   what a real break looks like). Unknown top-level or per-session KEYS are always ignored.
> - crabd may therefore ship new additive fields at any time without a widget import; the
>   widget lights features up whenever it is next updated. Breaks require bumping `schema`
>   AND a coordinated deploy — which is exactly why they should be rare.
> The "Schema 6" section below is retitled in place: its FIELDS are unchanged and live; only
> the schema NUMBER they ride on is now 5.

## v0.33.0 (2026-09-04 — BEHAVIOUR on macOS: fleet reads launchd; glow is absent; schema stays 5)

crabd `VERSION` → `0.33.0`. **No shape change.** `fleet` keeps exactly the two keys it has
had since v0.5.0 (`glow`, `toast`), the same four-word vocabulary
(`running` / `stopped` / `absent` / `unknown`), the same ~60 s cache and the same 10 s query
timeout. `schema` stays **5**. What changed is that on **macOS** the two values are now real
answers rather than a flat `unknown`. Windows is untouched.

### 1. The query

```
/bin/launchctl print gui/<uid>/<label>
```

run with `capture_output`, `check=False` and the contract's 10 s timeout, on the fleet's own
thread — never on the builder, because it is a subprocess and a build that spawned one would
put it in the request path and stall `generatedAt`. `gui/<uid>` is the per-user domain the
SideCrab agents are loaded into; `system/` is a different domain and `launchctl list` a
different, older answer shape.

### 2. The mapping

MEASURED 2026-09-04, macOS 26.6, uid 502.

| launchctl says | served |
|---|---|
| exit 0, first-level `state = running` | `running` |
| exit 0, first-level `state = not running` / `waiting` / `spawn scheduled` | `stopped` |
| exit 0, no first-level `state` line, or a word not in that list | `unknown` |
| non-zero exit whose output contains `Could not find service` | `absent` |
| any other non-zero exit | `unknown` |
| the query timed out, could not be spawned, or was refused | `unknown` |

**Only the first-level line counts.** `launchctl print` indents the service's own properties
with one TAB and nests sub-objects deeper, and those sub-objects carry their own
`state = active` lines — two of them under the running agent measured here. A parser taking the
first `state =` anywhere, or the last, would report a stopped agent as running on the strength
of a sub-object. A running agent also has a `pid = ` line and an idle one does not, but the
pid is *not* what this reads: `state` is the field launchd documents as the service's own.

An unrecognised word is `unknown`, never `stopped`, and only the not-found wording earns
`absent`. A label that exists and cannot be read is not the same claim as one that is not
there, and `unknown` is the word for the difference. This is the same rule the Windows
`schtasks` map has always followed.

### 3. `fleet.glow` is `absent` on macOS, and that is honest

There is no lighting component on a Mac — the Corsair SDK is Windows-only — so glow has **no
launchd label at all**. crabd does not run anything for it: an empty label short-circuits to
the same `(None, "", "")` sentinel `launchctl print` would never produce, and the platform
reads that as `absent`. Nothing is spawned, so a Mac's fleet cycle is one subprocess, not two.

`absent` is the literally true word here: the query did not fail, the component is not there.
The KEY stays so the document's shape is identical on both platforms — a panel that
feature-detects `fleet` draws a hollow absent dot rather than a missing row — and **the
panel's rendering of `absent` is unchanged**; no widget update is needed or implied.

`unknown` still means "not asked yet": a builder with no fleet reader attached serves both
components `unknown` on macOS exactly as it does on Windows, and only a completed poll turns
glow into `absent`.

---

## v0.32.0 (2026-09-04 — BEHAVIOUR on macOS: the host block is served; schema stays 5)

crabd `VERSION` → `0.32.0`. **No shape change.** `host` keeps exactly the four fields it has
had since v0.22.0 (`cpuPct`, `memPct`, `memUsedGB`, `memTotalGB`), the same units, the same
rounding and the same three failure tiers. `schema` stays **5**. What changed is that on
**macOS** the block is now PRESENT, where a Mac used to serve no `host` key at all. The widget
feature-detects `host` by presence, so this is the whole user-visible difference: the gauges
render on a Mac.

Windows is untouched — same counters, same numbers, same code path.

### 1. Where the numbers come from on macOS

| field | source |
|---|---|
| `cpuPct` | `host_statistics(mach_host_self(), HOST_CPU_LOAD_INFO, …)` from libSystem |
| `memTotalGB` | `sysctlbyname("hw.memsize")` |
| `memUsedGB`, `memPct` | `host_statistics64(mach_host_self(), HOST_VM_INFO64, …)` page counts × `sysctlbyname("vm.pagesize")` |

MEASURED 2026-09-04 on an Apple-silicon Mac, 16 cores, macOS 26.6: the CPU call returns
`kr == 0`, `count == 4` and four `uint32` ticks in CPU_STATE order — `user, system, idle,
nice` — cumulative since boot, summed across cores, in 1/`CLK_TCK` s where
`os.sysconf("SC_CLK_TCK")` is 100. One second of wall clock moved them by
`[213, 103, 1280, 0]`, sum 1596, which is about 16 cores × 100 Hz. The VM call returns
`kr == 0` and `count == 38` (the struct is 38 × 32-bit words); `vm.pagesize` is 16384 and
`hw.memsize` 137438953472 (128.0 GiB).

### 2. The tuple convention, and the two decisions inside it

`HostSampler` is unchanged and was written against Win32 `GetSystemTimes`, whose unit is the
FILETIME's 100 ns tick and whose **kernel time includes idle time**. The macOS reader adapts
to it rather than the other way round, so `CPU_MIN_TOTAL_TICKS` keeps meaning 100 ms of
aggregate core-time and every A-07/A-08/A-09 branch keeps its measured provenance:

```
scale  = 10_000_000 // CLK_TCK          (100_000 at the measured CLK_TCK 100)
idle   =  idle                  * scale
kernel = (system + idle)        * scale
user   = (user   + nice)        * scale
```

**Idle is folded into kernel.** Unfolded, `idle` exceeds `(kernel + user)` on any mostly-idle
machine, the sampler takes its A-08 branch, and `cpuPct` is null — not once, but on every
pass, on a perfectly healthy Mac. A dead gauge rather than a wrong one, which is exactly the
failure nobody reports.

**`nice` is busy time.** It counts user-priority-lowered processes actually running, so it
belongs with `user`. Left out, a machine doing background work at nice priority under-reports:
the worked example in the tests is a delta of `(user 100, system 100, idle 300, nice 100)`,
which is 50.0% with nice and 40.0% without.

### 3. The counters are 32-bit and they wrap

The mach tick counters are `natural_t` — **32 bits** — and cumulative since boot. At the
measured ~1600 ticks/s a bucket crosses 2^32 after roughly **31 days of uptime**, so a reader
that passes them on raw produces one backwards jump per bucket per month, and the sampler
answers a backwards counter by re-baselining and serving null.

crabd unwraps them into 64-bit monotonic values inside the platform reader: the last raw value
and a lap count are kept **per bucket**, and a value smaller than the last adds one 2^32 lap.
The sampler never sees the jump. A genuine backwards movement of any other kind is
indistinguishable from a wrap here and is treated as one — the honest trade, because the worst
case is a single over-large window served as a percentage (and the sampler still refuses it
if idle then exceeds the total), while the alternative is a null gauge on every long-uptime
machine.

`CLK_TCK` is read once via `os.sysconf("SC_CLK_TCK")` and must be a positive integer that
divides 10_000_000 evenly; anything else (0, negative, `True`, 3, 7, missing) serves **no
`cpuPct`** with one stderr line, because the scaling is an integer division.

### 4. `memUsedGB` is Activity Monitor's "Memory Used"

```
used      = (internal_page_count - purgeable_count + wire_count + compressor_page_count)
            × page_size
available = total - used
```

That is app memory + wired + compressed, which is the headline figure Activity Monitor shows —
66.0 GiB of 128.0 on the machine measured here. The contract's promise for this row has always
been that it matches what the OS's own monitor shows, and on a Mac there are two other
plausible answers that do not: `top`'s used is `total - free`, which reads **98.3 GiB** on the
same machine, and counting free + inactive + speculative as available reads differently again.
`free_count` and `inactive_count` therefore do **not** enter the formula.

### 5. Failure is unchanged, and it is honest

The three tiers are exactly the v0.22.0 ones: both readings failing → **no `host` key at all**
(the widget renders nothing rather than a row of em-dashes); one failing → that reading's
fields `null` and the other's intact; an unusable number → `null` via `_pct` / `_gb`. There is
still no last-good cache anywhere, which is why `cpuPct` is `null` on the first pass after a
crabd start and stays `null` rather than repeating a stale figure.

The macOS memory reader refuses, with one stderr line each and never a figure, when:
`host_statistics64` fails; the returned `count` is not the 38 words the declared struct is (a
different layout means the fields are not at the offsets the names were resolved from, and
nothing is read past that check); `vm.pagesize` is not a positive power of two; `hw.memsize`
is unreadable or not positive; or the computed `used` falls outside `0..total`. The last one
earns its place because `HostSampler` clamps an out-of-range availability back into range — an
unrefused negative `used` would reach the document as a confident `memPct: 0.0`.

---

## v0.31.0 (2026-09-04 — TRANSPORT: the panel is served by crabd on 9999; the Host and Origin allowlists; the panel header; schema stays 5)

crabd `VERSION` → `0.31.0`. **Nothing in the DOCUMENT changed.** No field was added, moved,
renamed or removed on `/v1/state`, `/v1/action` or `/v1/config`, so `schema` stays **5** and
every feature is still found by field presence. What changed is where crabd listens, who it
will talk to, and what a POST has to carry.

### 1. The port is 9999

`DEFAULT_PORT = 9999`. It was 2722 (C-R-A-B on a phone keypad), which was a fine choice while
the only client was a widget configured once at the iCUE console; the panel is now a page a
person opens in a browser, so the port is a number a person types. `CRABD_PORT` still
overrides it for a second instance running beside the live one.

The bind address is unchanged and is **not** configurable: `127.0.0.1`, a module-level
literal, with no environment variable and no config key that reaches it. There is deliberately
no `CRABD_HOST`. Loopback binding is the whole of crabd's access-control story on the read
paths, and a source-text test refuses `0.0.0.0` and any environment read of the host.

**A collision is a loud stop, never a move to another port.** crabd makes exactly one bind
attempt on exactly the port it was told to use; if that fails it prints to stderr and exits 1:

```
crabd: cannot listen on 127.0.0.1:9999 - [Errno 48] Address already in use. If another
process is holding the port, this names it: lsof -nP -iTCP:9999 -sTCP:LISTEN - then stop
it, or set CRABD_PORT to run crabd on a different port (the panel and the hooks have to
be pointed at the same number).
```

What the operating system said is quoted verbatim, because `[Errno 48] Address already in
use` and `[Errno 13] Permission denied` are different problems and the class name
`OSError` separates neither. The command is the PLATFORM's: `lsof -nP -iTCP:<port>
-sTCP:LISTEN` on macOS and Linux, `Get-NetTCPConnection -LocalPort <port> -State Listen |
Select-Object OwningProcess` on Windows, where `lsof` does not exist.

The refused alternative, recorded so it is not re-tried: "busy? bind the next port". That
produces this daemon's worst failure — crabd up on 10000 while every hook, the status line
command and the panel are still addressing 9999, so the feed is empty and nothing says why.

`SO_REUSEADDR` is now a per-platform answer (`False` on Windows, `True` on macOS and Linux)
rather than a constant. The option means two different things: on Windows it admits a SECOND
listener on a port already being listened on — two crabds answering half the requests each —
and on BSD/Linux it does not, where all it buys is a restart inside the TIME_WAIT window of
the last connection. A collision is loud on all three either way.

### 2. TRANSPORT: a `Host` allowlist, ahead of everything (DNS rebinding)

Runs FIRST, before the origin gate, on `GET`, `POST` and `OPTIONS` alike. A refusal is
**403** `{"error":"host not allowed"}` with **no** `Access-Control-Allow-Origin`; a POST
body is drained first so keep-alive framing survives.

| Request `Host` | Answer |
|---|---|
| absent (HTTP/1.0, a hand-rolled probe) | allowed - it is not a claim about anything |
| `localhost:<port>`, `127.0.0.1:<port>`, `[::1]:<port>` | allowed |
| `LOCALHOST:<port>` | allowed - the host part is compared case-insensitively |
| `localhost`, `127.0.0.1`, `[::1]` (no port part) | allowed - the port rule applies only when a port is present |
| `evil.example:<port>`, `evil.example` | **403** `{"error":"host not allowed"}` |
| `localhost:<other port>` | **403** - the port is part of the claim |
| `localhost.:<port>` (trailing dot) | **403** - the fully-qualified form resolves the same, which is exactly why it is a bypass |
| anything unparseable as `host` / `host:port` (an unbracketed IPv6 literal, an unclosed `[`) | **403** |

`<port>` is the BOUND port, as everywhere else in this section.

**Why, and why the origin gate cannot cover it.** The operator visits
`http://evil.example:9999`; its DNS record has a short TTL and re-resolves to `127.0.0.1`.
The browser now believes crabd *is* `evil.example`, so the page is **same-origin** with it -
and a same-origin `GET` carries no `Origin` header at all. The origin allowlist sees an
absent Origin, which is the ordinary shape of a hook, a curl and a plain navigation, and
allows it. Nothing else in the request distinguishes the two. `Host` does: it is taken from
the URL the page thinks it is talking to, not from the socket, so a rebound page still says
`evil.example` on every request it makes. Refusing it is not "who is asking" but "you are
not talking to who you think you are", which is why it is answered before anything about
CORS.

### 3. TRANSPORT: the Origin gate is an ALLOWLIST

Supersedes the v0.16.0 table above (§1 of that section). The rule is still identical for
`GET`, `POST` and `OPTIONS` on every path, and `Access-Control-Allow-Origin: *` is still
illegal everywhere. What changed is that crabd now serves the panel itself, so the panel HAS
a real `http://` origin, and "refuse every http origin" would refuse the product. This is
exactly the case v0.16.0's closing sentence anticipated: a panel confirmed to send a stable
non-`null` origin, allowlisted to that exact value.

`<port>` below is the port crabd is actually BOUND to, not the configured default — a second
instance on `CRABD_PORT` allowlists itself, never the production daemon beside it.

| Request `Origin` | Answer |
|---|---|
| absent (curl, the hooks, the status line command, the notifier) | handled; **no** ACAO header (a non-browser client needs none) |
| `http://localhost:<port>` | handled; ACAO echoes **exactly that origin** + `Vary: Origin` |
| `http://127.0.0.1:<port>` | same |
| `http://[::1]:<port>` | same (what `localhost` resolves to first on a dual-stack machine) |
| `HTTP://LOCALHOST:<port>` | same — the comparison is case-insensitive (scheme and host both are) |
| `http://localhost:<other port>` | **403** `{"error":"cross-site request refused"}`, no ACAO |
| `https://localhost:<port>` | **403** — nothing serves this panel over TLS |
| `http://localhost:<port>/` | **403** — a trailing slash is not a valid Origin serialisation, and a prefix match is how an allowlist gets walked past |
| `http://evil.example` (any other web origin) | **403**, no ACAO |
| `null` (the iCUE build's opaque QtWebEngine origin) | **preserved**: handled; ACAO `null` + `Vary: Origin` |
| `file://`, `qrc://icue/widget` | **preserved**: handled; the origin is reflected |

The match is EXACT, against the whole serialised origin. Not a prefix
(`http://localhost:9999.evil.example` starts with the right string), not a host test that
ignores the port (every dev server, notebook and other local UI the operator has open on
127.0.0.1 is a different origin), and not scheme-blind.

`null` stays allowed because the iCUE build has no other origin it could send — an opaque
origin serialises to exactly `null` — and a panel that cannot read is a broken product. It is
also forgeable by a sandboxed iframe, which is what §3 is for.

### 4. TRANSPORT: every POST carries `X-SideCrab-Panel`

**`PANEL_HEADER = "X-SideCrab-Panel"`**, any non-empty value. A POST without it is answered
`403 {"error":"panel header required"}` — a DISTINCT body from the cross-site refusal, so an
operator wiring up a hook can tell the two apart.

- Applies to **every** POST path: `/v1/hook`, `/v1/hook/stop`, `/v1/hook/permission`,
  `/v1/statusline`, `/v1/metrics`, `/v1/logs`, `/v1/action`, `/v1/config`, `/v1/panel-log`,
  and every unknown path. `GET` and `OPTIONS` never require it.
- **Order: the origin gate answers first.** A cross-site page is told it is cross-site, which
  it already knew, and never learns there is a header to look for.
- The body is drained before the 403, so keep-alive framing survives.
- The 403 carries whatever ACAO the origin gate computed, so a same-origin panel can READ its
  own refusal (an unreadable reply is a CORS error, not a status).
- `/v1/hook/permission` is refused **immediately**, in front of the routing, never after its
  55 s hold.
- The value is never interpreted. It is not authentication.

**An OTLP exporter has to be told.** `/v1/metrics` and `/v1/logs` are on the list above, and
nothing in SideCrab configures the exporter inside Claude Code — so a session exporting to
crabd is refused `403` until the operator sets

```
OTEL_EXPORTER_OTLP_HEADERS=X-SideCrab-Panel=1
```

alongside `OTEL_EXPORTER_OTLP_ENDPOINT`. The failure is silent from crabd's side by design
(telemetry never gets a 4xx that teaches its exporter to retry — see the OTLP contract below),
so the symptom is `burn.costUSD` staying `null`. Nothing in this repo writes that variable yet.

**What it actually buys.** A custom request header makes the POST non-simple, so a browser
must preflight it — and the preflight is where the gate is enforced:

| Preflight `Origin` | `Access-Control-Allow-Headers` |
|---|---|
| a panel origin from the table above | `Content-Type, X-SideCrab-Panel` |
| `file://`, `qrc://…` (non-web scheme) | `Content-Type, X-SideCrab-Panel` |
| `null` | `Content-Type` — **exactly as before 0.31.0** |
| absent (a non-browser client; nothing preflights without one) | no ACAO at all, so no `Access-Control-Allow-Headers` either |
| any other web origin | no ACAO at all, so no preflight answer to use |

So a page that forges `Origin: null` keeps its READS (unchanged, and still a disclosed
residual) and loses its WRITES: its preflight comes back without permission to send the
header, and the POST never leaves the browser. That is the closure of the forged-null write
vector `SECURITY.md` carried as a residual.

### 5. crabd serves the panel

`GET /` and `/index.html` serve the panel's `index.html`; a path whose first segment is
`styles`, `scripts`, `resources` or `mock` serves that file. **Nothing else under the panel
root is served** — `/manifest.json`, `/translation.json`, `/DEV.md` and `/tests/…` are `404
{"error":"not found"}`, and an unknown `/v1/…` stays the JSON 404 it has always been. The
directory is `CRABD_PANEL_DIR`, defaulting to the `widget/` tree beside `crabd.py`.

Path safety, applied after ONE percent-decode: a path is refused (404, never an exception and
never a read) when it contains `..` as a segment, a backslash, a NUL, an empty segment, a
segment starting with `.`, or a `%` that survived decoding; the resolved candidate must have
the resolved panel directory as a parent, which also refuses a symlink inside the tree that
points outside it. A directory path and a missing file are both 404. A query string never
reaches the path (`/index.html?mock=normal` serves `index.html`).

Content type by suffix — `.html` `text/html; charset=utf-8`, `.css` `text/css; charset=utf-8`,
`.js` `text/javascript; charset=utf-8`, `.json` `application/json`, `.svg` `image/svg+xml`,
`.png` `image/png`, `.ico` `image/x-icon`, `.woff2` `font/woff2`, `.txt`
`text/plain; charset=utf-8`, anything else `application/octet-stream`. `X-Content-Type-Options: nosniff` and
`Cache-Control: no-store` are on **every** response crabd sends, static or API — one rule each
for the whole daemon rather than a per-branch flag a new route can forget. `no-store` matters
here in particular: the panel now ships with crabd, and a stale script surviving an update is
the bug it avoids.

One static reply reads at most **64 MB** (`PANEL_MAX_BYTES`), checked by `stat` before
the read; a larger file is `404` plus one log line. Not a limit the shipped panel comes
near - it is a limit on a directory `CRABD_PANEL_DIR` can point anywhere.

The origin gate applies to static reads exactly as to the API: a foreign-origin `fetch` of
`/scripts/sidecrab.js` is 403, while a plain navigation (which sends no `Origin`) is served.
A static read never touches the builder's lock, so it cannot be blocked by a wedged state
build and cannot block a hook.

### 6. `/v1/health` gains `panel`

```jsonc
"panel": { "origins": ["http://127.0.0.1:9999", "http://[::1]:9999", "http://localhost:9999"],
           "headerRequired": true, "dir": "/Users/you/SideCrab/widget" }
```

Diagnostic, **not the state contract** — health has never been part of it, so no schema bump,
and the block is never in `/v1/state`. It exists because "which origins does your crabd trust"
and "which panel build is it serving" were answerable only by reading the source.

### Compatibility, stated honestly

An installed iCUE widget older than 0.29.0 **cannot POST to this crabd**: it sends no
`X-SideCrab-Panel`, so every write is refused 403. Its READS are unaffected — its origin,
whatever it is, is one this crabd allows and reflects — so it keeps rendering the panel, and
only its taps stop working. That is the same shape as the 0.29.0 `decide` change and it is safe
for the same reason: the writes that stop, stop by being refused, and every one of them has a
terminal-side fallback. (It is also polling 2722, so in practice it shows the standalone state
until it is re-imported.)

**Which origin the iCUE build sends, and the case that is NOT covered.** Measured, not assumed:
`originsSeen` recorded `origin: file://` (AppleWebKit/537.36 UA) on 2026-09-02, after the 0.27.0
import — ORIGIN-b in `docs/BACKLOG.md`. QtWebEngine did *not* collapse its file page to an
opaque origin, and a web page cannot forge `Origin: file://`, which is why §4's preflight table
unlocks the panel header for a non-web scheme. That is **one reading on one iCUE build**. A
build that reports `Origin: null` instead — a different QtWebEngine, a different iCUE — keeps
its reads and **loses its taps**, because `null` is forgeable and its preflight therefore never
unlocks the header. That is the accepted trade: widening `null` to cover it would re-open the
forged-null write for every browser on the machine. A second install's `originsSeen` is what
would settle it.

## v0.30.0 (2026-09-04 — ADDITIVE: `limits.tokenSource`; the long-lived limits token; schema stays 5)

crabd `VERSION` → `0.30.0`. One additive member, one new optional file, no wire change on any
write path.

**`limits.tokenSource`** — `"cli"` | `"sidecrab"`, present only when `limits.available` is true.
Which token answered the usage endpoint: the CLI's own access token from
`~/.claude/.credentials.json` (`cli`), or the long-lived token the operator stored with
`Install-SideCrab.ps1 -LimitsToken` (`sidecrab`). Diagnostic; an older widget ignores it.

**Why.** The CLI access token lives about six hours and is rewritten only when a terminal
`claude` makes an API call — the desktop app keeps its refreshed token elsewhere — so a panel fed
from that file read *"Claude token expired - run /login"* most mornings, and `/login` was not even
the fix (the CLI was still logged in; its file was merely stale). `claude setup-token` mints a
token that lasts about a year.

**Precedence.** CLI token when unexpired, else the stored token, else the (reworded) unavailable
notes: *"Claude token expired - run claude in a terminal to refresh it, or store a long-lived one:
Install-SideCrab.ps1 -LimitsToken"*. A `401`/`403` while the stored token is in use reads
*"SideCrab limits token rejected - mint a new one with claude setup-token and re-run
Install-SideCrab.ps1 -LimitsToken"*, so the two failure modes are never confused.

**The store.** `~/.sidecrab/limits-token.dpapi`: the token, DPAPI-protected for the current
Windows user with no entropy (`[ProtectedData]::Protect(..., CurrentUser)`), decrypted in memory by
crabd with `CryptUnprotectData` on each limits poll and dropped — never logged, never served, never
copied anywhere. Read fresh every poll, so storing it needs no restart.

## v0.29.0 (2026-09-01 — ADDITIVE fields + a TRANSPORT change on `decide`; schema stays 5)

crabd `VERSION` → `0.29.0`, widget `0.27.0`. **Closes SEC-a and WID-a.** Two additive fields, one
diagnostic, and one write path that now REQUIRES two new body members.

**`approvals`** (top level, always present):
```jsonc
"approvals": { "enabled": false, "tokenRequired": true }
```
`enabled` mirrors config `panelApprovals.enabled` (strict-true). `tokenRequired` is `true` on
every crabd from 0.29.0; a document without the block is an older crabd that never asks for one.

**`sessions[].pendingPermission.requestId`** — `string` (16 hex), minted per `register()`. A
replacing request for the same session gets a NEW id.

**`POST /v1/action {"action":"decide"}`** now takes:
```jsonc
{ "sessionId": "...", "action": "decide", "decision": "allow" | "deny",
  "token": "K7QXM-2PDAB",        // the pairing code; case- and hyphen-insensitive
  "requestId": "0f3a9c...7e" }   // pendingPermission.requestId as displayed
```
Answers, in the order the gates run: `400` decision malformed · `503` crabd has no pairing gate
(never falls open) · `429` gate locked (ten rejects inside a minute lock it for a minute; the
right code is locked too) · `403 {"error":"pairing code required"}` / `{"error":"pairing code
rejected"}` · `404` nothing pending · `400 {"error":"requestId required"}` (only when something IS
pending) · `409 {"error":"stale permission request"}` (id is not the pending one; checked under the
broker lock) · `204` applied. **A widget older than 0.27.0 sends neither member and is refused with
403 — its "decide failed … decide in terminal" notice is the honest answer, and the hold passes
through to the terminal dialog exactly as a no-tap does.** That is why this is a transport note and
not a schema bump: nothing an old widget renders changes, and its one write that stops working
stops SAFELY.

**The pairing code.** `~/.sidecrab/panel-token`, 10 symbols of `0123456789ABCDEFGHJKMNPQRSTVWXYZ`
(2^50), written atomically on first start, shown as `XXXXX-XXXXX`. Printed by
`Install-SideCrab.ps1 -PairingCode`; held by the widget as the iCUE property `panelToken`
("Approval Pairing Code"). NEVER served: `/v1/health` gains
`"panelToken": {"present": bool, "rejectedRecently": int, "lockedUntil": ISO | null}`.

**Why this and not the two candidates the SEC-a row listed.** The widget's true origin IS `null`
(originsSeen measured it), so an allowlist cannot separate it from a forged one; and a nonce
served in `/v1/state` is read by the same forged-null caller that would echo it. A secret that
lives in a widget PROPERTY is the one thing a visited page cannot reach.

## v0.28.0 (2026-08-28 — ADDITIVE: the ctx-fill DENOMINATOR; schema stays 5)

crabd `VERSION` → `0.28.0`. One new per-session member. No key moves, nothing is removed,
`schema` stays **5**, and an older widget ignores it as an unknown key — so no widget
import is needed to deploy this crabd.

**`sessions[].contextWindowTokens`** — `int > 0 | null`. The context window the session's
`contextTokens` is filling toward: the DENOMINATOR of the widget's ctx-fill hairline.

```jsonc
"contextTokens": 549300,         // int | null — how FULL the window is
"contextWindowTokens": 1000000   // int | null — how BIG it is
```

**Always present, `null` when unknown** — the same idiom as `contextTokens`,
`queuedContinue` and `pendingPermission`: the KEY is the consumer's feature detection, the
VALUE is the reading. A reader must therefore test the value's TYPE, not the key's
truthiness.

**Why it exists.** The widget derived this denominator only from a `[1m]`/`[200k]` marker
in the model id, and the live ids on this host carry none (measured 2026-08-28:
`claude-fable-5`, `claude-opus-5`), so the bar never drew on a real session — the feature
was shipped and invisible.

**Source priority — MOST SPECIFIC FIRST, and the order is load-bearing:**

| # | Source | Scope |
|---|---|---|
| 1 | the status line document's `context_window.context_window_size` | this SESSION, live |
| 2 | the `[1m]`/`[200k]` marker in the session's own `model` string | this SESSION, stated by the feed |
| 3 | `GET https://api.anthropic.com/v1/models` → that model's `max_input_tokens` | this MODEL, in general |

Rank 1 takes the same freshness contest `contextTokens` takes (CD-36: a status-line row
older than the transcript's own reading loses), because a retained row can name a model the
session has since left. Rank 2 above rank 3 is the case a served `claude-sonnet-4-6[200k]`
makes: the marker is that session's window, the catalog is the model's ceiling, and
preferring the ceiling would gauge the card at a fifth of its true fill — a wrong bar looks
exactly like a right one. `max_input_tokens`, never the sibling `max_tokens`, which is the
OUTPUT cap (measured 2026-08-28: 128000 beside a 1000000 input window).

**Failure = `null`, always.** No credentials file, no token, an expired token, 401, 429, a
timeout, a malformed document, an id absent from the catalog: every one of them serves
`null`, and the widget draws no bar. There is **no model-name table** on either side of
this wire — a built-in "opus means 200k" is a number no document said and would go silently
wrong the day a window changed. A failed catalog fetch KEEPS the last good catalog (a
model's window is a fixed property, not a drifting reading) and throttles the next attempt
by 15 min; a successful one is cached 6 h, in memory only, never to disk.

**Reader rules.** `null` = unknown = **no bar, and no denominator in the tooltip** — never a
zero, never a default window, never last week's number. A crabd below 0.28.0 omits the key
entirely; the widget then parses the marker itself, exactly as it did before, so the two
behaviours are identical wherever a marker exists.

**The token.** The catalog call carries the same OAuth bearer + `anthropic-beta:
oauth-2025-04-20` the usage endpoint takes (verified live 2026-08-28: HTTP 200). The
standing rule is unchanged — the token is read, sent, and dropped; never logged, never
persisted, never in `/v1/state`. `ModelCatalog` serves an integer or an absence and has no
`note` field at all, so no error text from it can reach the widget.

## v0.26.0 (2026-08-28 — BEHAVIOUR + robustness; schema stays 5, no field added or removed)

No shape change: not one key moves, so `schema` stays **5** and no widget import is needed.
crabd `VERSION` → `0.26.0`. This wave is the audit-0424 fixes; the observable ones are:

- **A permission stand-down now writes a ring event.** When a permission hold stands its card
  down, crabd persists a `"permission alert cleared"` entry on `sessions[].events` (and to
  `history.jsonl`), exactly as the in-app clear persists `"answered outside the panel"`. The
  stand-down used to be silent, which left an A-01/A-02-class mis-clear with no trace anywhere.
  `events` stays capped at 8, newest-first, as before.
- **A replaced permission hold no longer stands its card down while the live hold is parked
  (A-01).** Two `PermissionRequest`s for one session — what parallel tool calls in one
  assistant message produce — are newest-wins: B replaces A. A's release must not clear the
  card while B is still parked, or the card reads `working` while serving B's live
  `pendingPermission`. It now stays `needs_input` until B itself resolves. (Dormant while
  panel approvals are disabled; correct the moment they are re-enabled.)
- **An identical-text `Notification` for a live permission dialog ends the alert in either hook
  order (A-02).** The CLI's own `Notification` for a permission dialog is word-for-word
  `PERMISSION_QUESTION`; once it has landed on the row, the hold merely expiring is no longer
  an answer — regardless of which of the two hooks the CLI emitted first (unmeasured). The
  identical text still does not re-escalate (`stateSince`/`acked` unchanged).
- **`cpuPct` is served null for a sub-quantum window or an `idle > kernel+user` glitch (A-07/
  A-08)** — see the CPU failure table above; both belong in the null column, never a `0.0`.
- **`needs_input` retention is bounded (A-05)** — see §9 of v0.21.0; the exemption from
  `GONE_AFTER_SEC` holds, but count and age ceilings now trim runaway growth, oldest-first.
- Internal robustness with no served effect: crabd's own config write is now atomic (a failed
  write can no longer empty `config.json`), a `cwd` on an unreachable network path can no
  longer stall the document build, the git cache is a bounded LRU, an untimestamped usage
  record is skipped rather than dated `now`, and a future round-trip timestamp is clamped to
  crabd's clock before it is written into `stateSince`.

## v0.24.0 (2026-08-28 — a SIDE CHANNEL; schema stays 5; NOT part of this contract)

The **panel diagnostics log channel**: `POST /v1/panel-log` for the widget to say what it
saw, `GET /v1/panel-log` for a maintainer to read it back. crabd `VERSION` → `0.24.0`.

> **⚠ READ THIS FIRST — this endpoint is NOT part of the widget-facing state contract.**
> It is **OPTIONAL** in both directions. `/v1/state` is unchanged, `schema` stays **5**, and
> **a widget must function fully when `/v1/panel-log` 404s** — which is exactly what every
> crabd at 0.23.0 and below does. Nothing the widget renders may depend on this channel, no
> feature may be gated on it, and a failed POST to it must never surface to the operator.
> It is a debugging aid the widget writes to and forgets; if the write fails, the panel
> carries on as though it had never tried.

**Why it exists.** The widget is rendered by iCUE on the Xeneon Edge, a surface no devtools
can attach to — `console.log` has nowhere to go. The question this week: **which input
events does iCUE actually deliver to the widget when the operator touches the glass?** A
**tap** is proven (panel approvals were verified live with the operator on 2026-08-27);
**swipe, long-press and multi-touch are unknown**. The only way to find out is for the
widget to describe what it received, over the same loopback port everything else rides, and
for a human to read it.

### 1. `POST /v1/panel-log` — the widget writes

```jsonc
{"lines": ["pointerdown t=12 id=0 x=140 y=88", "pointerup t=131 dx=2 dy=1"]}
```

| | |
|---|---|
| **`lines`** | an **array of 1..50 strings**. No other key is read |
| **204** | on success, empty body |
| **400** | `lines` absent, not an array, empty, or **any member of the first 50 is not a string**. Body: `{"error":"lines must be an array of 1..50 strings"}`. **Nothing is stored** |
| **403** | a present `http(s)` **Origin** — the same SEC-1 gate every mutating endpoint rides, and it fires **before** anything is stored |

**Two over-limits are NOT errors, and this is the part to build against:**

- **More than 50 lines → the first 50 are kept, 204.** A widget mid-burst must not lose the
  whole batch for over-filling it: losing the tail of one burst is recoverable, losing the
  burst is not. Members past the 50th are **not even type-checked** — they are not stored,
  so their type cannot matter, and 400ing on line 51 would make the cap a rejection after
  all. (It is also what keeps a 5000-line body costing one slice rather than 5000 checks.)
- **A line longer than 300 characters → truncated to 300, 204.** The first 300 characters of
  a diagnostic line are the diagnostic. Lines are **trimmed first, then truncated** — the
  300 is a budget on content, so leading whitespace must not be able to push the useful half
  off the end. A line that trims to empty is stored as an empty line; it is still evidence
  the widget posted.

### 2. `GET /v1/panel-log` — the maintainer reads

```jsonc
{
  "lines": ["2026-08-28T02:11:04Z [panel] pointerdown t=12 id=0 x=140 y=88", "..."],
  "count": 2,
  "droppedTotal": 0
}
```

| | |
|---|---|
| **`lines`** | the ring, **oldest first**, each carrying the server-side prefix below |
| **`count`** | the length of what was **RETURNED** — the same rule `/v1/history`'s `count` follows, so it can never exceed 500 and a reader never has to reconcile it against a shorter list |
| **`droppedTotal`** | lines **evicted by the ring** since this crabd started. Ring evictions only — not lines dropped past the 50-per-post cap, and not truncated characters, both of which the caller already knew about |
| **403** | a present `http(s)` **Origin** — the SEC-4 read gate, like every other GET. These lines describe what is on the operator's panel while they touch it; a visited page has no more business reading them than reading `/v1/state` |

**Reads do not consume.** It is a ring, not a queue — two people reading see the same lines.

### 3. The prefix — the widget never timestamps

Every stored line is prefixed **server-side**, and is verbatim after that:

```
2026-08-28T02:11:04Z [panel] <the line exactly as sent, trimmed and truncated>
```

ISO-8601Z receive time, one space, the short client marker `[panel]`, one space, the line.
The widget's clock is this same machine, so a widget-side timestamp would agree — but a
uniform, server-applied prefix is what makes the **ordering** crabd's, and what makes a
second source safe to add later without renegotiating the format with whoever wrote the
first one. **One timestamp per batch**: the lines arrived in one request, so one receive
time is the honest reading, and it makes intra-batch order the order the widget wrote them
in rather than an artefact of loop speed.

### 4. In memory only, and 500 lines is the whole flood posture

**The ring is 500 lines and it is NOT persisted — deliberately.** Nothing here touches disk
and nothing survives a crabd restart. This is a scratch channel for a live debugging
session, **not history**: persisting free text the widget composes would create a file that
grows, that backups pick up, and that somebody later reads as a record of what happened.
`droppedTotal` exists precisely so a reader can tell they are looking at a tail rather than
assuming the ring is the whole story.

**There is no rate limit, because the ring IS the bound.** The worst legal body — 50 lines
at 300 characters — costs one list extend and one slice under a lock that touches neither IO
nor a build, and the memory ceiling stays fixed at 500 prefixed lines however hard the caller
pushes. Eviction is a single slice-delete rather than a pop per line, so one oversized batch
does not hold the lock 500 times.

### 5. The lines are DATA, never instructions

crabd stores them verbatim, serves them verbatim, and **nothing in the daemon reads a stored
line back into any decision path** — not the state build, not the permission broker, not the
continue queue, not a config write. That is the prompt-injection posture, and it is a
property of the **wiring** rather than of the content: the ring has exactly one reader (the
GET above) and it hands the bytes to a human. Any future caller that parses a line in here
is the change that breaks the property, so it is the change to refuse.

The corollary for whoever reads the output: **these lines are untrusted text**. They arrive
over an unauthenticated loopback port that any process on the machine can reach, and the
`[panel]` marker is a label crabd applied to the transport, not a proof of authorship.

---

## v0.23.0 (2026-08-27 — ADDITIVE; schema stays 5)

The quiet-hours **override**: one operator-tappable gesture on the panel that says "quiet, for
the next while" or "not quiet, whatever the schedule says". Additive in every direction — a new
`action` value, a new config key, a new OPTIONAL member inside the existing `quiet` block — so
`schema` stays **5** and no widget import is needed. crabd `VERSION` → `0.23.0`.

### 1. `POST /v1/action {"action": "quiet"}` — the tap

```jsonc
{"action": "quiet", "mode": "on",   "minutes": 120}   // force quiet until now+120 min
{"action": "quiet", "mode": "off",  "minutes": 60}    // force AWAKE until now+60 min
{"action": "quiet", "mode": "auto"}                   // clear the override, now
```

| | |
|---|---|
| **`mode`** | exactly `"on"`, `"off"` or `"auto"` — a **fixed vocabulary**, nothing else |
| **`minutes`** | an **integer 15..480**, REQUIRED for `on`/`off`. Not a float, not a numeric string, not a bool |
| **`sessionId`** | not required and not read — this is a whole-panel gesture, like `ack-all` |
| **204** | on success, empty body |
| **400** | a mode outside the three, or (for `on`/`off`) a missing/ill-typed/out-of-range `minutes`. Nothing is written |
| **403** | a present `http(s)` **Origin** — the same SEC-1 gate every mutating action rides. A visited web page cannot dim the operator's panel |
| **500** | the config file could not be written |

**`auto` IGNORES a `minutes` it was sent** rather than 400ing on it, and is idempotent: two taps,
two 204s, the same file. Clearing is the gesture the operator reaches for when the panel is doing
something they did not intend, and "your cancel was malformed" is the worst answer to that.

**Why a fixed vocabulary and a bounded duration** — this endpoint writes `config.json` over the
same unauthenticated loopback port every other action rides. The complete set of values that can
reach the file is `on`/`off` plus a minute count in range; `until` is computed from **crabd's own
clock**, never supplied by the caller. An attacker who reaches the port can dim a panel for at
most eight hours. The floor (15 min) is the shortest span worth a tap; the ceiling (8 h) is long
enough for a night or a working day and short enough that a forgotten override always expires on
its own — the SCHEDULE owns every other minute, and an indefinite override would be a second,
invisible schedule nobody remembers setting.

### 2. `quietOverride` in `~/.sidecrab/config.json` — the persistence

```jsonc
"quietOverride": { "mode": "on", "until": "2026-08-27T23:40:00Z" }   // ABSENT = no override
```

**It survives a crabd restart** — the tap is a file write, not process state. Written through the
**same locked read-modify-write** `/v1/config` uses (the v0.16.0 preserve-under-lock lesson), so
every other key in the file is preserved; a writer holding a whole-file rewrite outside that lock
loses whatever landed between its read and its write, and this is an endpoint an operator taps
twice in a row.

**An expired override is treated as ABSENT on read** — there is exactly one reading of it, so no
branch anywhere can honour a stale one — **and is removed from the file lazily, on the next config
write of any kind.** Deliberately no timer: the override dies of the clock, and a timer that must
fire for it to end is a timer that can fail to. Anything that reads as absent is swept, malformed
values included; a **live** override is never swept by an unrelated config write.

**`until` is half-open**: `until <= now` is EXPIRED, matching the quiet window's exclusive `end`,
so the two cannot disagree about a boundary minute. A hand-edited `until` is re-formatted from the
parsed epoch, so the served value is always the one shape above.

**`quietOverride` is NOT in the `/v1/config` whitelist** (still exactly `quietHours`, `toast`,
`digest`, `budget`). It IS panel-writable — just not through that endpoint. `/v1/action`'s quiet
branch is its only writer, which is what bounds the values that can ever reach the file: a
`/v1/config` body naming it is an unknown key → **400, nothing written**, the same posture
`panelApprovals` has for a different reason (SEC-2).

### 3. `quiet.active` is now the EFFECTIVE answer, and `quiet.override` reports why

```jsonc
"quiet": {
  "active": true,                 // EFFECTIVE: schedule with the override applied
  "start": "22:00",               // the SCHEDULE, unchanged — or NULL when none is configured
  "end":   "07:00",
  "override": {                   // OPTIONAL — present only while an UNEXPIRED override exists
    "mode":  "on",
    "until": "2026-08-27T23:40:00Z"
  }
}
```

**The override wins in both directions**: `on` → `active: true` however the schedule reads, `off`
→ `active: false` even inside a live quiet window (the "I am working through the night, stop
dimming the panel" tap — the half that is easy to drop and the half the operator notices).

**Resolved in crabd, once, in `quiet_state`.** Every consumer already reads this one boolean —
the widget's dim and glow, the crab's nightcap, and all four of the notifier's suppression sites
(waiting toast, approval toast, long-run toast, digest/budget/outage) via its `is_quiet(state)`,
which reads `state["quiet"]["active"]` off the feed and computes nothing itself. So the tap
reaches every one of them without any of them learning what an override is.

**An override with NO schedule configured still produces a block**, with `start`/`end` **null**.
"Quiet is on until 21:40, and there is no window" is a fact worth serving, and nulling the whole
block — the way an unconfigured schedule is nulled — would make the tap do visibly nothing on the
install most likely to use it. `quiet` is still `null` when there is neither a schedule nor a live
override, and `override` is **absent**, not null, when there is no override.

## v0.22.0 (2026-08-27 — ADDITIVE; schema stays 5)

One new top-level key, `host`. Additive, so `schema` stays **5**, unknown keys are ignored by
every existing reader, and no widget import is needed. crabd `VERSION` → `0.22.0`.

### 1. `host` — this machine's CPU and memory, beside the iCUE temperature sensors

```jsonc
"host": {                        // OPTIONAL top-level key — PRESENCE is the feature detection
  "cpuPct":     34.2,            // float 0..100, 1 dp, or NULL (see "the first sample" below)
  "memPct":     58.1,            // float 0..100, 1 dp, or null
  "memUsedGB":  18.6,            // float GiB, 1 dp, or null
  "memTotalGB": 32.0             // float GiB, 1 dp, or null
}
```

**Read straight off the Windows kernel, stdlib only** — `GetSystemTimes` and
`GlobalMemoryStatusEx` through `ctypes`. No perfmon counter subscription, no WMI, no
third-party package; crabd remains a single-file stdlib script.

**`GB` means GiB (1024³)**, which is the unit Task Manager shows — so the panel and the OS
agree rather than differing by 7%.

**Sampled on the builder's own pass, not on a thread of its own.** The sample is taken inside
`build()`, which runs every `REFRESH_INTERVAL_SEC` (2 s), so the effective refresh matches the
rest of the document and `generatedAt` dates these numbers as honestly as it dates the others.
An ambient gauge does not need better resolution than that, and a fifth thread would be one
more thing that can wedge while the value it feeds keeps being served.

**THE FIRST SAMPLE HAS NO `cpuPct`, and this is not a bug.** `GetSystemTimes` returns
*cumulative* counters since boot, so utilization exists only *between* two readings. crabd
holds no CPU number until its second builder pass (~2 s after start, and after any counter
glitch that forces a re-baseline). **Null there means "not measured yet" — never 0%.** A
reader must **render an em-dash OR omit the reading entirely — never a 0% gauge**: "the machine
is asleep" is a different claim and it would be a false one. (The em-dash and the omission are
equally conformant. The QtWebEngine widget OMITS the CPU reading while `cpuPct` is null; a
second consumer is free to draw an em-dash instead. The one forbidden rendering is a lit 0%
gauge — CON-a, 2026-08-28 audit.)

**The arithmetic, stated because it is wrong in a believable way if you get it wrong.**
Kernel time *includes* idle time. Over the delta between two readings:

```
busy  = (kernel + user) - idle
total = (kernel + user)
cpuPct = 100 * busy / total     clamped to 0..100, rounded to 1 dp
```

**Two windows are served NULL rather than a number (v0.26.0), because the number would be a
false one:**
- **A sub-quantum window** (`total` below ~100 ms of aggregate core-time). `GetSystemTimes`
  counters advance in coarse scheduler quanta (~31 ms lands at once), so a window that caught
  only a quantum or two cannot express a real busy fraction — idle and kernel moving by the
  same quantum reads as an exact `0.0` on a machine that is not asleep. Reachable only at cold
  start, where the request-thread build and the first `_refresh_loop` build overlap. Served
  null, never `0.0`.
- **`idle > kernel + user`.** Idle time is a subset of kernel time, so this cannot happen with
  a well-behaved counter; a rigged reader or driver bug can produce it, and `busy` then goes
  negative. Served null (the same choice the backwards-counter re-baseline makes), never
  clamped up to `0.0`.

Treating the three counters as disjoint buckets, or omitting the subtraction, yields
percentages that look entirely plausible and are not the truth — on the pinned fixture, 62.5%
and 100% against a real 40%. An un-subtracted implementation reports a **completely idle host
as ~100% busy**.

**Honest failure, in three tiers**, because "cannot read" has three different shapes:

| What happened | What is served |
|---|---|
| Neither counter readable — no `ctypes.windll` (not Windows), or both calls failed | **No `host` key at all.** Presence is the detection, so the panel renders nothing rather than a row of em-dashes that reads like a broken sensor |
| One of the two calls failed | The block is present; **that call's fields are null**, the other's are intact |
| A call returned, but what it returned is unusable — a non-finite number, a negative size, installed memory of zero bytes | Treated as that call having failed: **its fields are null**, never clamped into a plausible-looking `0.0` or `100.0` |

One `stderr` line per failure kind for the lifetime of the process (the `_log_once` rule), then
silence. **No last-good cache exists in the sampler**: a good reading followed by a failed one
serves null, never the previous number re-dated as fresh. Nothing here can raise into `build()`
or produce a non-finite float for `dump_state` to sanitise.

## v0.21.0 (2026-08-27 — BEHAVIOUR; schema stays 5, no field added or removed)

No shape change: not one key moves, so `schema` stays **5** and no widget import is needed. crabd
`VERSION` → `0.21.0`. This release is the crabd lane of a finding-verification wave — nine
confirmed defects fixed, two claims refuted and pinned. Everything below is something a reader can
observe; nothing here is a new field.

### 1. A continuation turn now shows its own `Stop` (CD-06)

**The bug.** A `Stop` arriving on a card already tracked as `done` moved nothing, so `stateSince`
stayed pinned to the session's FIRST `Stop`. `state` is resolved as "done unless the transcript was
written after `stateSince`" — and every write of the continuation turn is after that frozen
timestamp. So the card read **`working` through the second turn, through its `Stop`, and every turn
after**, until it aged out of the window without ever reading `done` again. The tap-to-continue path
is exactly this shape: crabd's own `Stop` answer forces another turn and no `UserPromptSubmit` fires.

**Now:** every `Stop` re-dates `stateSince` (and clears `question` / `acked`), whatever the row last
said. The **done ledger is deliberately not re-armed** by a `done → done` Stop — `recap.doneToday`
and `recap.week` count DISTINCT session ids, so that finish is already counted, and re-arming would
write a second `done` line into history for every repeated `Stop`.

### 2. A restart no longer resurrects finished sessions as `working` (CD-07)

Replaying `~/.sidecrab/history.jsonl` restored the events ring and the tallies but left `state`
unset — and an unset state resolves to **`working`**. So every session that had FINISHED before the
restart came back claiming a live turn, and stayed there until it aged to `idle` 15 minutes later.

**Now:** a replayed row whose newest event is `turn finished` comes back **`done`**, and
`session ended` comes back **`gone`**. Both then age out on their normal schedule. A row whose ring
ends on a later non-terminal event (the session was picked up again) is **not** restored. `asked a
question` is deliberately **never** restored to `needs_input`: history holds no question text, and
`needs_input` is the one state that is never aged away — a restored one would alert forever with
nothing to say.

### 3. `contextSource` — a stale status line no longer beats a newer transcript (CD-36)

Status-line rows are retained for two hours, and until now that retention alone decided precedence:
a reading from 90 minutes ago overrode a transcript figure from 30 seconds ago (reproduced —
`150000` over `30000`). The **precedence is unchanged** (status line wins; it reads the live window
and the transcript figure is arithmetic that disagrees by a whole window after a compaction) — it is
now conditional on the status line's reading not being the OLDER of the two, with a 120 s allowance
for the two clocks. A live status line always wins; one that has stopped feeding loses to the
transcript instead of holding the chip for two hours.

### 4. `recap.doneToday` can no longer exceed `recap.sessionsToday` (CD-11)

The two counts came from sources that never met — `sessionsToday` from the transcript scan,
`doneToday` from the hook ledger — so a session crabd holds no transcript for (older than the
7-day window, or under a projects dir it cannot read) produced `sessionsToday: 0` beside
`doneToday: 1`. `sessionsToday` now unions in every session that finished today and every hook row
that moved today, so `doneToday <= sessionsToday` holds **by construction**.

### 5. `subagentDetail` no longer names an agent that has stopped (CD-29)

`subagents.running` was already correct, but the LIST was the newest `running` transcripts by
mtime — and a subagent that has just stopped has the newest mtime of all of them (its final record
is the last thing written). So the one agent crabd knew had finished was the one named, and a
genuinely running older sibling was dropped. Each recorded `SubagentStop` now retires the file whose
last write is nearest it before the list is trimmed.

### 6. `question` is scoped to the turn that is actually waiting (CD-28)

The transcript's richer question enriched the hook's message when it fell inside a 120 s lookback —
but a whole turn fits inside 120 s, so a question from the PREVIOUS turn could replace the
notification actually on screen. The lookback is now floored at the current turn's start
(`UserPromptSubmit`), with a small allowance for hook latency. Sessions crabd saw no
`UserPromptSubmit` for — every session already running when crabd started — keep the plain window.

### 7. `queuedContinue` — a replacement tapped mid-delivery is kept (CD-30)

The `Stop` handler is peek → send → consume on purpose, so a failed send leaves the prompt intact.
But a prompt queued in that gap (the queue is newest-wins, so the tap is accepted) was then deleted
by the consume while the OLD prompt was the one delivered — neither delivered nor kept, and the card
stopped showing it. The consume now spends only the prompt that was actually sent; a different one
stays queued for the next `Stop`.

### 8. `limits` — a bool or non-finite utilization is absent, not gauged (CD-10)

`utilization: true` rendered a window **100% full** (a bool passes an `int` type test and
`float(True)` is `1.0`), and `NaN` / `Infinity` were silently clamped to an empty or a full gauge.
All four numeric parse boundaries — both `_window` mappers, the scoped-weekly `percent`, and the
status line's `context_window` — now refuse them, so the gauge reads em-dash instead of a
fabricated measurement of the operator's week.

The same class of value in `config.json` was worse than cosmetic: a hand-edited
`toast.thresholdSec: 1e309` is valid JSON that parses to `inf`, and `int(inf)` raised inside every
build — **startup served an empty document and a running crabd froze on its last snapshot**. And
`GET /v1/history` now serves through the same serializer as every other document, so a poisoned
line cannot emit bare `NaN` / `Infinity` at a reader's `JSON.parse`.

### 9. Retention is bounded (CD-09) — internal, no served change

A transcript admitted while it was fresh stayed resident, re-stat'ed and fully re-copied into every
2-second build for as long as crabd ran; a session row left on `working` or `done` (the ordinary end
of a session whose terminal was closed, with no `SessionEnd` hook) was never pruned. Both now leave
at their existing horizons — the transcript window, and `GONE_AFTER_SEC`. **`needs_input` keeps its
contract exemption**: a question waits even when everything else has gone quiet.

**`needs_input` is exempt from `GONE_AFTER_SEC`, not from every bound (v0.26.0).** The old
exemption was total — no count cap, no age ceiling — so a hook flood or a pile of abandoned
questions grew the tracker and the served `sessions` array without limit (every `needs_input`
row is served on every poll). Two generous, oldest-first ceilings now trim runaway growth
while **never** evicting a genuinely recent waiting prompt: a row past a many-hour age ceiling
of no activity is dropped, and past a max live-row count the OLDEST-by-`at` rows go first (a
fresh 2am prompt has the newest `at`, so an abandoned/acked row — which stopped moving `at`
long ago — is the one dropped). Both ceilings sit far beyond any real waiting window, so a
real question is untouched. Internal — no served shape changes.

### Refuted, and pinned by tests so they stay refuted

- **A rapid second question is not cleared while unanswered.** v0.20.0's different-question re-fire
  rule moves `stateSince` past the first question's activity, which is what the clear is compared
  against. Verified against the exact replay, not the rule.
- **One malformed transcript record does not lose the tail behind it.** The read offset does move
  to EOF before parsing — but the whole chunk is already in memory and the per-record catch is
  INSIDE the line loop, so a throwing record costs its own line and nothing else. Pinned because the
  load-bearing detail is that catch's POSITION.

## v0.20.0 (2026-08-27 — BEHAVIOUR; schema stays 5, no field added or removed)

No shape change: not one key moves, so `schema` stays **5** and no widget import is needed. crabd
`VERSION` → `0.20.0`. Three things a reader can observe change.

### 1. `/v1/state` NEVER answers a 500 for a data-shape reason

**The crash this closes (observed once in production, 2026-08-27 ~10:50).** The FIRST
`GET /v1/state` about 2 s after crabd started raised out of the handler and the widget got a 500.
Three later cold starts did not reproduce it, so the fix is not the one line that threw — it is
every seam that could produce it.

MEASURED against 0.19.0 with a repro harness: **ten distinct transcript record shapes crashed the
parser outright** — a non-dict `message` or `usage`, and a usage counter that is a dict, a list, a
word, an `Infinity` or a `NaN`. Any one of them anywhere under `~/.claude/projects` aborted the
transcript scan and therefore the whole build, so **one unreadable line in one session's transcript
took every session's card down with it**. A second seam was proven at the object level: the
per-file usage records were written by the scan and read by the build with no lock between them
(CRB-F2 covered the file *table*, never the state *inside* a file), which two near-simultaneous
cold-start builds can hit.

The guarantee now:

- **A record crabd cannot read is skipped, and the rest of the file is still read.** A counter it
  cannot read is `0`, which under-reports burn by that record — the honest trade against serving
  nothing at all.
- **A file it cannot read costs that file only.** Every other session still gets its card.
- **Nothing is swallowed silently.** Every skip is counted, and the first one prints one line to
  stderr. Once, not per poll — a poisoned transcript would otherwise print every 2 s forever.
- **The served document is always valid JSON.** A value that cannot be expressed is served as its
  string form; a non-finite number is served as `null`, never as the bare `NaN` / `Infinity` tokens
  `json.dumps` emits by default (those are not JSON, and a reader's `JSON.parse` dies on them
  silently).
- **If a build fails anyway**, `/v1/state` serves the **last good snapshot** — stale, with
  `generatedAt` saying how stale, which is the same honest signal a wedged refresh thread already
  produces. With no snapshot ever built it answers **`503 {"error":"state not built yet"}`**, a NEW
  status on this endpoint. Serving `sessions: []` there would claim the operator has no sessions
  running, and that is an answer crabd made up. The widget retries on its next poll either way.

A reader hanging up mid-answer is also no longer a traceback — ordinary transport on a loopback
that drops SYN-ACKs, logged once and dropped.

### 2. A live `PermissionRequest` puts the card on `needs_input`

**The gap:** `needs_input` was set by the `Notification` hook and by nothing else, so a session
sitting on a live permission dialog read **`working`** unless a `Notification` happened to fire
beside it. The panel renders Approve / Deny off the `needs_input` sheet — so the very card carrying
a `pendingPermission` could be the one card not offering it. crabd held the operator's decision
open for 55 s and never told them it was waiting.

The hold now moves the state machine, and **every way it can resolve ends with the card standing
down**: a panel tap, an answer given in the app (v0.19.0's turn clock), and a plain timeout.

Two refusals make it safe rather than careful:

- It raises **only** from `working` or from a session crabd has seen no state-moving hook for. A
  `done` or `gone` row is left alone — a Stop and a PermissionRequest for one session race in the
  wild, and the later of the two must not resurrect a finished card as alerting.
- The stand-down applies **only** to an alert the hold itself raised. A `needs_input` a
  `Notification` raised (or re-raised with a new question) is still a question genuinely waiting,
  and a hold merely expiring is not an answer.

The card's `question` while a hold is open is `"Claude needs your permission to use <tool>"` —
word for word the message the CLI puts on its own `Notification` for the same dialog, so the two
hooks arriving a second apart escalate one prompt once, not twice.

### 3. A NEW question on an already-alerting card re-alerts at full strength

A `Notification` arriving while the row was already `needs_input` used to move nothing: `stateSince`
still dated the FIRST question and `acked` was still set, so the second question of a turn landed
pre-silenced on a card that had already escalated to red. That is the failure v0.19.0 §2 warned a
view-only fix would cause, reachable through the hooks instead.

The test is the question **text**, and that is the healthy-night guard rather than a nicety: Claude
Code re-fires `Notification` for a prompt the operator has walked away from, and resetting on every
one of those would un-ack an acknowledged card all night.

---

## v0.19.0 (2026-08-27 — BEHAVIOUR; schema stays 5, no field added or removed)

No shape change: not one key moves, so `schema` stays **5** and no widget import is needed. What
changes is **when a `needs_input` row stops being one** — and that is a guarantee the widget
depends on, so it is written down here. crabd `VERSION` → `0.19.0`.

### 1. THE GAP — `needs_input` outlived the operator's in-app answer

Reported by the operator: the maintainer answers a waiting session **in the Claude Code desktop app** and the
Xeneon panel keeps alerting — and keeps *escalating* (the widget deepens at 5 min and again at
15 min unacked) — until the turn eventually ends.

`needs_input` is set by the **`Notification`** hook, and Claude Code fires `Notification` for
**both** shapes of waiting: an idle prompt, and a permission dialog (`"Claude needs your permission
to use Bash"` is a real measured message). Before v0.19.0 the ONLY things that moved a session out
of `needs_input` were a later `SessionStart` / `UserPromptSubmit` / `Stop` / `SessionEnd` hook —
and the two commonest in-app answers fire **none** of them:

| How the maintainer answers | What fires at decision time | Cleared before v0.19.0? |
|---|---|---|
| Types a prompt | `UserPromptSubmit` | yes — this path was always correct |
| Clicks Allow/Deny on the terminal permission dialog | **nothing** — the `PermissionRequest` hook already returned its pass-through when the dialog appeared | no, not until `Stop` (an hour of tool work away) |
| Picks an option on an `AskUserQuestion` sheet | **nothing** — the answer is a `tool_result`, not a prompt | no, not until `Stop` |

`_resolve` also never ages a `needs_input` away (by design — a question keeps waiting even when the
transcript is quiet), so there was no second chance either.

### 2. THE CLEARING SIGNAL — a completed model round-trip in the session's own MAIN transcript

crabd now clears `needs_input` when the newest **assistant usage record** in that session's **main**
transcript is newer than the moment the question was raised (plus a 5 s grace). A usage record is a
*completed model round-trip*, which is the one thing that cannot happen while the operator is still
being waited on — the model is blocked. So the guarantee runs in both directions:

- **It can never fire early.** A question that genuinely still stands writes no usage record, ever.
  Silence is not read as an answer; only a *later round-trip* is.
- **It fires for every answer path**, because all of them end in the model being called again: an
  approved tool's result, a denied tool's result, a picked option, a typed prompt.
- **Subagent transcripts are excluded.** A background subagent finishing its own work while the main
  session waits is not an answer, and folding its records in would clear a standing question.
- **It is per-session by construction** — the signal is keyed by the transcript's own session id, so
  there is no path at all by which one session's activity reaches another's row.
- **It is a real transition, not a display overlay**: `question` → null, `acked` → false,
  `stateSince` → the round-trip, `lastEvent` → `"working"`, and an `"answered outside the panel"`
  entry on `events`. That matters because an overlay would leave the tracker on `needs_input`, and
  the **next** `Notification` would then find no state change — so `stateSince` would not move and
  `acked` would not clear, and the second question of a turn would land pre-silenced on a card that
  had already escalated to red. A new question after a clear **re-alerts at full strength**.

Costs nothing new on the wire: crabd already parses these records for `burn` and `contextTokens`.

### 3. A parked `pendingPermission` is retired by the next `Stop` / `UserPromptSubmit` / `SessionEnd`

Follows from the v0.12.0 spike finding (`docs/spikes/live-verify.md` §3.3, SC-LV-2): **the terminal
dialog is not suppressed, it is RACED** — it renders immediately while crabd is still holding the
55 s long poll. So the operator can answer it at t=2 s, the tool runs, the turn finishes — and the
card goes on offering Approve / Deny for another 53 s on a decision already made, where a tap is a
404 at best. Any of those three hooks for that session proves the turn moved past the dialog, so the
hold is released as the ordinary **pass-through**. There is still no route to an `allow` that is not
a tap. `SubagentStop` is deliberately **not** in that set — a background subagent finishing says
nothing about the main thread's dialog.

Also fixed: a raise between `register()` and `release()` used to strand a **panel-visible**
`pendingPermission` forever (nothing but a later request for the same session could clear it — not
the hold, not the expiry sweep). The release is now structural.

### 4. Rejected, and why (recorded so it is not re-litigated)

- **`PreToolUse` / `PostToolUse` as activity pings.** They would close §1 precisely, but they put an
  HTTP round trip in front of **every tool call in every session** — the highest-frequency hook
  surface the product could have, on a host whose loopback drops SYN-ACKs. The transcript already
  carries the same evidence on a path crabd polls anyway. Not wired; not optional-wired either.
- **OTLP activity as a clearing signal.** MEASURED in this repo, not assumed: `setup/*.ps1` sets no
  `OTEL_*` variable, so a default install emits no OTLP at all — a clearing signal that is absent on
  the operator's machine is not a fix. And crabd resolves `session.id` at exactly **one** site
  (`OtlpReceiver.ingest_logs`, `api_error` events); cost metric points are keyed by attribute-set
  string and never mapped to a session, so per-session "token activity" does not exist here. An
  `api_error` is also evidence of a *failing* request — the opposite of the block being released.
- **`SubagentStop` as a clearing signal.** An orchestrator's background subagent finishing while the
  main session waits on a question is an ordinary, frequent event. Clearing on it would silence a
  question nobody answered — the one failure mode worse than the bug being fixed.

---

## v0.18.0 (2026-08-27 — ADDITIVE; schema stays 5)

One new top-level key. Additive, so `schema` stays **5**, unknown keys are ignored by every
existing reader, and no widget import is needed. crabd `VERSION` → `0.18.0`.

### 1. `toast` — the notifier's toast settings, echoed onto the feed

```jsonc
"toast": {
  "thresholdSec": 120,          // int, ALWAYS present
  "enabled": true,              // bool, ALWAYS present
  "approvalThresholdSec": 45    // int, PRESENT ONLY when the on-disk config sets one
}
```

**Why the feed carries config at all.** `/v1/config` is POST-only and the widget cannot
read `~/.sidecrab/config.json`, so before this there was no channel by which the settings
sheet could ever *display* an existing value — it kept a touched-latch and rendered
nothing for a setting the operator had hand-edited. Same reasoning as `continuePrompts`
(v0.12.0): a config-only key rides the feed because the feed is the only read path.

This is an **echo**, not a second write path. `/v1/config` remains the only way to change
these values, with its bounds and its 400s unchanged.

**The two required members always appear.** `thresholdSec` and `enabled` are required
members of the config block, so a missing `toast` block means the notifier is running on
its shipped defaults (`120` / `true`, `notifier/sidecrab_toast.py`
`DEFAULT_THRESHOLD_SEC` and `ToastConfig.enabled`, also documented in `README.md`). That
is a fact worth serving, not an unknown. An unusable hand-edited value (wrong type,
negative) falls back to the same defaults, which is also what the notifier does with it.

**`approvalThresholdSec` is present only when it is set on disk, and NO DEFAULT IS EVER
INVENTED FOR IT.** The asymmetry is the point. The key is optional; v0.16.0's
preserve-on-omit work (§2 of that section below) exists precisely because a round trip
that materialized this key erased the operator's hand edit. A feed answering `20` for an
unset key would hand the widget a value to latch and write back — reintroducing the very
defect from the other end. **Absent here means "not set on disk"**; what the notifier
falls back to is the notifier's business to know, not the feed's to claim. An unusable
value is omitted for the same reason: it is not the operator's value either.

**The served value is what the NOTIFIER will use, not what `/v1/config` would accept.**
The notifier honours any non-negative seconds value, so a hand-edited `thresholdSec: 10`
— below the endpoint's 30 s floor — is served as `10`. The panel must not display `120`
while the box behaves like `10`; clamping for a slider's own range is the reader's job.

The write→read guarantee is the existing one: the **next** `/v1/state` after a successful
`POST /v1/config` reflects it (the once-a-minute config damper is busted by the write).

## v0.17.0 (2026-08-27 — behaviour + a TRANSPORT note; schema stays 5)

No field is added, removed or renamed, so `schema` stays **5** and no widget import is
needed. What changed is what three existing values are allowed to SAY. crabd `VERSION` →
`0.17.0`. (Audit `docs/findings/audit-crabd.md`, items F3, F4, F6, F7, plus two backlog
items.)

### 1. `exhaustAt` is null when the window carries no parseable `resetsAt` (audit F6)

The v0.13.0 rule below — *"never extrapolated past the window's own `resetsAt`"* — was
only enforced when there WAS one. With `resetsAt` absent, null or unparseable the cap was
skipped and the raw projection was served: measured against the pre-fix code, the smallest
utilization step the served 4dp rounding can produce (1e-4 over ~900 s) yielded a date 93
days out on a five-hour window, and a slope an order smaller reached `_utc_iso`'s
year-3000 ceiling and was served as that.

**Now: no parseable `resetsAt` → `exhaustAt: null`.** A cap that cannot be applied does
not become a number crabd made up — the same *unknown is null, never a fabricated value*
rule the rest of this document runs on. Consumers see strictly more nulls and never a new
shape; every window a real source emits carries a reset, so this is an edge, not the
common path.

### 2. A permission tap in the timeout gap now answers the hook it claims to (audit F3)

`POST /v1/action {"action":"decide"}` keeps its answers exactly as documented in §4 below
— 204 when a hold was pending, **404 `{"error":"no permission request pending"}`** once
the 55 s hold has ended. What changed is a sub-millisecond window at the 55 s mark where
the two could disagree: a tap landing after the hold expired but before the entry was
dropped was accepted (204) and written to history as `"approved from panel: …"`, while the
hook had already been answered with the pass-through — so the TERMINAL dialog owned the
call and the panel's record said otherwise.

**Now the two always agree.** A tap that gets in before the hold is closed is honoured:
the hook is answered with that decision and the history line is true. A tap after it is
the documented 404, and the hook's `permission passed through` line stands. **The
never-auto-allow invariant is untouched** — a `behavior: allow` still requires a `decide`
tap and nothing else can produce one.

`permission passed through: <tool>` is also now written even when the session's row aged
out during the hold (audit F7), so *"I did not tap in time"* stays distinguishable from
*"the panel never saw it"* — the distinction that line exists for. History-only; the
served document is unaffected.

### 3. TRANSPORT: an unknown POST path drains its body before answering 404

Framing note, not a contract change: `POST` to a path crabd does not serve is still
**404 `{"error":"not found"}`**, but the request body is now read and discarded first —
matching the 403 cross-origin branch. Left in the socket, those bytes were parsed as the
next request line on a keep-alive connection, so the request AFTER an unknown POST could
be answered as garbage or kill the connection. Any client may now pipeline normally
across a 404.

(Also in this release, no observable effect: the OTLP cumulative-series keyspace is
bounded per day — audit F4 — and a Stop hook whose answer cannot be delivered logs one
line instead of a traceback. Both are memory/console hygiene; the served numbers and the
CRB-F5 queued-continue guarantee are unchanged.)

## v0.16.0 (2026-08-27 — additive + a TRANSPORT change; schema stays 5)

Nothing in the DOCUMENT changed, so `schema` stays **5** and no widget import is needed.
What changed is the transport gate, one config member, and three drifts this document had
accumulated. crabd `VERSION` → `0.16.0`.

### 1. TRANSPORT: the Origin gate now covers the READS too (QA-Audit SEC-4)

Supersedes the "permissive CORS (`Access-Control-Allow-Origin: *`)" line in **Transport**
below, and the "Same CORS as the other GETs / as /v1/action" lines throughout. **crabd no
longer emits `Access-Control-Allow-Origin: *` on any route, method or status code.**

The rule, identical for `GET`, `POST` and `OPTIONS` on every path:

| Request `Origin` | Answer |
|---|---|
| present and `http://…` / `https://…` | **403** `{"error":"cross-site request refused"}`, **no** `Access-Control-Allow-Origin` header |
| `null` (the widget's opaque QtWebEngine origin) | handled normally; `Access-Control-Allow-Origin: null` + `Vary: Origin` |
| absent (curl, the CLI's own http hooks, local tools) | handled normally; **no** ACAO header (a non-browser client needs none) |
| any non-web scheme (`file://`, `qrc://…`) | handled normally; the origin is reflected |

Why the reads and not just the writes: `/v1/state` carries every live session's `cwd`, its
`title`, the FULL text of `question`, and `pendingPermission`. Under `ACAO: *` any page the
operator merely visited could read all of it cross-origin from a background tab. SEC-1
(v0.15.0) closed the write half; this closes the read half with the same predicate.

**The widget is unaffected** — an opaque origin serializes to exactly `null`, which is
allowed and reflected, so its cors-mode `fetch` can still read every reply including the
error statuses it branches on. A widget that ever reports a stable non-`null` origin would
need that value allowlisted; `null` is the only serialization an opaque origin has.

### 2. `/v1/config` — `toast` gains an OPTIONAL third member

`toast` is now `{"thresholdSec": int 30..3600, "enabled": bool,
"approvalThresholdSec": int 5..3600 (optional)}`. The two original members stay REQUIRED;
an out-of-range or non-int `approvalThresholdSec`, or any fourth member, is still 400 with
nothing written.

`approvalThresholdSec` is the notifier's *pending-permission* threshold and needs its own
bounds: its shipped default is 20 s, which is **below** `thresholdSec`'s 30 s floor, so the
two cannot share one. A pending permission is something the operator is already blocked on;
a merely-thinking turn is not.

**A write that OMITS `approvalThresholdSec` PRESERVES whatever is on disk.** This is the
half that matters: the widget's settings sheet does not know the key exists and sends
`{thresholdSec, enabled}`, and because blocks are written whole, every panel save used to
delete a hand-edited value with no message (the notifier then fell back to its 20 s
default). An explicit value in the body always wins — preservation is for silence, never an
override. The corollary is accepted deliberately: the key cannot be *deleted* over HTTP,
only hand-edited out of the file.

### 3. Behaviour: a Stop hook whose answer fails no longer eats the queued continue

`POST /v1/hook/stop` now PEEKS the continue queue, sends the answer, and only then consumes
the item and writes the `continue sent:` history line. Before, it drained first, so a send
that failed (connection reset, CLI gone) destroyed a prompt the operator had tapped and
could see on the card. Observable consequence for anything reading crabd out of band: the
card's `queuedContinue` clears *just after* the hook's response lands, not before it. Since
v0.21.0 that clear is conditional: only the prompt actually delivered is spent, so a replacement
tapped inside that window stays queued for the next `Stop` instead of being deleted undelivered.

### 4. Contract drifts documented (all pre-existing, none new)

- **`continuePrompts` config key.** `~/.sidecrab/config.json` `"continuePrompts": ["ship
  it", …]` — the operator's EXTRA tap-to-continue buttons. File-config only; deliberately
  NOT in the `/v1/config` whitelist. Served at the TOP LEVEL of `/v1/state` as
  `"continuePrompts": [...]` (always present, `[]` when unconfigured) because the widget
  cannot read `config.json`. Also CONSUMED as the queue whitelist: `POST /v1/action
  queue-continue` accepts the builtin prompts plus these, and nothing else. Parsed
  defensively — non-list, non-strings, blanks, over-long entries, duplicates and anything
  already builtin are dropped silently, and the builtins can never be lost to a typo.
- **`contextSource: "transcript"`.** The per-session `contextSource` documented in v0.12.0
  named only `"statusline"`. It has always had a second value — `"transcript"`, the tokens
  derived from the transcript's newest usage record, which is what a session serves before
  (or without) a statusline document. The key is always PRESENT and is `null` exactly when
  `contextTokens` is null: a source label on an absent number would be a claim about
  nothing.
- **The `500` / `503` / `501` / `403` status codes crabd can answer.** Undocumented until now:
  - `POST /v1/config` → **500** `{"error":"could not write config"}` — a body that
    validated but could not be persisted. Distinct from the 400 a bad body gets, and the
    only 500 crabd emits deliberately.
  - `GET /v1/state` → **503** `{"error":"state not built yet"}` (v0.20.0) — no snapshot has
    ever been built and this request's own build failed. The ONLY non-2xx `/v1/state` has
    besides the cross-site 403, and never a 500 for a data-shape reason (v0.20.0 §1).
  - `POST /v1/action` → **501** for a feature this crabd does not carry:
    `{"error":"reply not supported"}` (injection unproven — see v2 additions),
    `{"error":"continue not supported"}` (no continue queue wired),
    `{"error":"panel approvals not supported"}` (no permission broker wired).
  - `POST /v1/action queue-continue` → **403** `{"error":"tap-to-continue is disabled"}`
    when config `allowContinue` is `false`. 403 rather than 501 on purpose: the feature is
    implemented and refused by configuration, not missing.
  - Any request carrying an http(s) `Origin` → **403** `{"error":"cross-site request
    refused"}` (§1 above).
  The widget renders every non-2xx from `/v1/action` as "not available" without latching,
  so the distinction is for operators and logs, not for widget branching.

## v0.14.0 additions (2026-08-26 — additive, schema stays 5)

**`sessions[].queuedContinue`** — `{"prompt": "...", "queuedAt": "ISO"} | null`. Always present
(the key itself is the widget's feature detection), null when nothing is queued. Freshness is
re-derived from `queuedAt` rather than trusting the expiry sweep, so a card never advertises a
prompt the Stop hook would no longer deliver.

**`GET /v1/health` counters** (health is not part of the state contract, documented here for
operators): `{"ok", "version", "uptimeSec", "hooksSeen", "statuslineSeen",
"lastStatuslineAgeSec": N|null, "otlpSeen", "originsSeen"}`. `lastStatuslineAgeSec` distinguishes
*never posted* (null — misconfigured) from *posted and went quiet* (a number — idle operator);
zero for both would make the two indistinguishable, which is the failure this counter exists to
catch.

**`originsSeen` (v0.25.0, ORIGIN-REC; v0.27.0 adds `source`/`userAgent`) — DIAGNOSTIC, and
explicitly NOT part of the widget-facing contract.** An array of
`{"origin": "...", "source": "browser"|"local"|"none", "userAgent": "..."|null, "count": N,
"lastSeenAt": "ISO"}` recording the distinct **(origin, source)** pairs seen on the request paths (a
raw absent header is folded to the literal `"<absent>"`; the set is LRU-capped at 48 so a flood of
forged origins/UAs cannot balloon it). It is the passive enabler for the SEC-a fix: the legitimate
QtWebEngine widget and a forged-`null` attacker are indistinguishable to the origin gate, so the
widget's TRUE origin has to be MEASURED before it can be allowlisted — and this lets it be read
remotely from the widget's own live polling instead of at the glass.

**Why `source` (v0.27.0).** Origin-only keying collapsed every no-Origin caller into one
uninformative `"<absent>"` bucket — the notifier polling `/v1/state`, a maintainer's curl health
checks, and possibly the widget all landed there together (measured live 2026-08-28: `originsSeen`
was only `{"origin":"<absent>"}`). `source` is a coarse bucket derived from the request's
`User-Agent` (`"browser"` when it contains `Mozilla`/`Chrome`/`QtWebEngine`/`AppleWebKit` — the
widget is QtWebEngine/Chromium; `"local"` for any other non-empty UA like python-urllib or curl;
`"none"` when there is no UA at all), and `userAgent` is that raw UA truncated to ~80 chars. Keying
on the (origin, source) pair means `null`-from-a-browser and `<absent>`-from-a-local-process are
separate rows, which is what isolates the widget. **⚠ The `User-Agent` is attacker-controlled and
this classification is DIAGNOSTIC ONLY** — it never feeds `_is_web_origin` or any decision path; the
CSRF gate stays exactly as it is (origin-based). A future SEC-a fix keying the gate on *absent vs
null* (the clean discriminator if the widget proves to send absent, not null) is a SEPARATE,
deliberate change to the gate — this recorder only MEASURES.

It lives ONLY here in `/v1/health`; it is **never** in `/v1/state`, never a `build()` input, and
never read back into any decision path.

## v0.13.0 additions (2026-08-26 — additive, schema stays 5)

**Depletion forecast.** `limits.fiveHour` / `limits.weekly` each gain an optional
`"exhaustAt": "ISO" | null` — a linear projection of when the window hits 100% at the recent
burn rate, computed by crabd from the window's utilization delta over the last ~15 min of served
readings (needs ≥2 readings spaced ≥60 s; null when flat/declining/insufficient data). Never
extrapolated past the window's own `resetsAt` (a window resets before it depletes → null) —
**and since v0.17.0 (§1 above) a window with no parseable `resetsAt` is null too**, because a
cap that cannot be applied must not become a served number. The
widget renders a muted "~full by 3:40 PM" line under the gauge when present and sooner than reset;
absent/null → nothing. Pure projection, clearly hedged ("~"), never presented as certainty.

## v0.12.0 additions (2026-08-26 — additive, schema stays 5) — "the control-surface wave"

**1. statusLine ingest (retires the OAuth reach-around).** A chained statusline command
(`hooks/sidecrab_statusline.py`, wired by the installer WITH settings backup, chaining any
pre-existing statusline) POSTs the official stdin session document to `POST /v1/statusline`
(204, fire-and-forget). crabd prefers this source for limits + per-session context:
`limits.source: "statusline" | "oauth"` (new field; widget shows a muted provenance label) and
statusline-fed `contextTokens` carries `contextSource: "statusline"`. OAuth remains the fallback
when no statusline document has arrived in 10 min. Per-session context has a second condition
since v0.21.0: the status line's reading must not be the OLDER of the two (120 s clock allowance),
or the transcript figure wins — retention alone used to decide it, for up to two hours.

**2. OTLP receiver.** `POST /v1/metrics` + `POST /v1/logs` accept OTLP http/json from Claude
Code's built-in telemetry (installer sets the env vars in the hook-carrying settings? NO — env
config documented in README, user-level opt-in). crabd aggregates: `burn.costUSD` (today, from
claude_code.cost.usage when telemetry flows; null otherwise — never derived), API error events
into sessions[].events. Malformed OTLP → 204 and dropped (a telemetry write must never error the
producer). Provenance: burn.costSource: "otlp" | null.

**3. Tap-to-continue (Tier 1).** `POST /v1/action` gains `{"sessionId","action":"queue-continue",
"prompt": "<one of the configured set>"}` → 204, one queued item per session (newest wins),
expires 10 min. The Stop hook becomes type-http pointing at `POST /v1/hook/stop` — crabd answers
within 2 s. **SHAPE REVISED after binary deep-read (v0.15.0):** `decision:"block"` DOES force another turn,
but routes through the CLI's *error* channel — the operator sees "Stop hook error occurred" and
the model receives the nudge labelled a blocking error (measured; it visibly hedges). The
sanctioned non-error channel is `{"hookSpecificOutput": {"hookEventName": "Stop",
"additionalContext": <prompt>}}` — the binary's own schema text: "non-error feedback delivered
to the model; the conversation continues so the model can act on it", and BOTH branches push
into the same continuation array, so the forced turn is guaranteed on the same code path. crabd
emits additionalContext; `decision:block` is retained as an executable fallback constant.
`continuationPrompt` does not exist (measured 0). `{}` = proceed/stop normally. Widget: done/working cards'
sheets gain Continue / Run the tests / Commit + push buttons + extras from config
`continuePrompts: ["..."]`.

**4. FULL panel approval (the maintainer's explicit choice).** PermissionRequest hook → type-http
`POST /v1/hook/permission` (long-poll): crabd registers the pending request
(sessions[].pendingPermission: {"tool","summary","requestedAt"} — additive), holds the response
up to 55 s awaiting `POST /v1/action {"sessionId","action":"decide","decision":"allow"|"deny"}`
from the widget (Approve/Deny buttons on the needs_input sheet). **VERIFIED v2.1.246 by reading the shipped zod schema (supersedes an earlier docs-based
mis-reading):** the response is `{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
"decision": {"behavior": "allow"} | {"behavior": "deny", "message": ...}}}` — NOT the PreToolUse-
style `permissionDecision` string, and there is **no `"ask"` value**. The pass-through (timeout /
no-tap / disabled / malformed) is to return **no `hookSpecificOutput` at all** (`{}`), which lets
the TERMINAL DIALOG appear as today. crabd has NO branch that yields `behavior:allow` without a
`/v1/action decide` tap having landed first. PermissionRequest
http hook timeout is set to 60 s (past crabd's 55 s poll). Note `allowedHttpHookUrls`, if the
operator has set it, must include `http://127.0.0.1:2722/*` or the http hooks are blocked. NEVER auto-allow. Config `panelApprovals: {"enabled": false}`
default OFF; the installer asks/flips per the operator's choice. Every decision logged to
history ("approved from panel: Bash", "denied from panel: ...").



**Title fallback + provenance.** The per-session `title` chain gains a final tier: the session's
cwd tail (last path component; last two joined with "/" when the last is generic — main, src,
app, repo, work, dev, tmp). New optional per-session `"titleSource"`:
`"custom" | "ai" | "prompt" | "cwd" | null` — presence-detected; the widget renders
`"cwd"`-derived titles muted-italic. Sessions with no title at all render the repo name, else
"untitled session" — never a bare "session" literal.

## v0.10.0 additions (2026-08-26 — additive, schema stays 5)

**Burn budget.** `/v1/config` fourth key: `budget` — `{"dailyOutputTokens": int 100000..100000000}`
or `null` to clear (strict; else 400 nothing written). When configured, `burn` gains:
```jsonc
"budget": { "dailyOutputTokens": 5000000, "todayPct": 0.34 }   // pct capped at 9.99
```
Absent config → no `budget` key. Consumed by: the widget (a target marker on the 24h sparkline
and a muted "budget 34%" line near TODAY; ≥100% amber, ≥150% red — text carries the state) and
the notifier (ONE toast per day on first crossing 100%, "Daily token budget crossed", deduped in
its ledger like the digest; quiet-suppressed-and-marked).

## v0.9.0 REMOVAL (2026-08-26 — publication)

**The `estate` block is REMOVED.** A private-dashboard integration existed before publication and
was removed for release: crabd no longer emits the key or carries its reader, and the widget no
longer renders the strip or offers the `estateStrip` setting. Both sides presence-gated it already,
so removal is deploy-order-free. Historical `estate` sections below are collapsed to this note.

Two rules the removal leaves behind, both still live:

- Every shipped string and fixture is generic — SideCrab reads nothing but local Claude Code state.
- **The widget must degrade to a useful standalone product without crabd** (clock + crab + iCUE
  sensors), because a store user installs the widget first and may never install the companion.

## v0.8.0 additions (2026-08-26 — additive, schema stays 5)

**`GET /v1/history?day=YYYY-MM-DD`** — read-only view over the persisted history (current file +
the one .old generation): `{"day": "...", "events": [{"ts": "ISO", "kind": "...", "sessionId":
"...", "title": "..."}], "count": N, "truncated": false}` — events of that LOCAL day, newest
first, cap 200 with `truncated: true` beyond. `day` is strictly validated (^\d{4}-\d{2}-\d{2}$
and a real date; else 400). Unknown/empty day → empty events, 200 (absence of history is not an
error). Same CORS as the other GETs.

**`/v1/config` third key: `digest`** — `{"enabled": bool, "time": "HH:MM"}`, both members, strict
(else 400 nothing written). Consumed by the notifier: when enabled, ONE toast at the configured
local time daily — "Yesterday: N done · M commits" from crabd's recap.week — deduped per calendar
day, quiet-hours-suppressed-and-skipped (not deferred), silent when crabd is unreachable.

**Widget-only in v0.8.0:** session PINNING — the detail sheet gains Pin/Unpin; pinned sessions
sort first within their state band (needs_input still outranks everything), marked with a small
pin glyph, persisted via the iCUE local-storage mechanism the vendor docs describe (falling back
to in-memory when unavailable — a lost pin is a nuisance, not an error); tapping a DAY in the
timeline-footer week strip opens that day's history via GET /v1/history (absent endpoint on an
older crabd → the day tap is inert, attempt-and-handle, no latch).

## v0.7.0 additions (2026-08-26 — additive, schema stays 5, detected by presence)

`recap.week` — the last 7 local days (oldest first), from PERSISTED history (see below) + git:
```jsonc
"week": [ { "day": "2026-08-20", "done": 3, "commits": 14 } ]   // commits = sum across recap-scope repos
```

**History persistence:** hook-derived facts (events, done transitions) now append to
`~/.sidecrab/history.jsonl` (one JSON object per line; no secrets, no question text — event kind +
session id + title-at-time + ts only), replayed at startup so doneToday/events/week survive crabd
restarts. Size-capped by rotation (~2 MB, one .old generation). The doneToday "floor" caveat is
retired for restarts after this ships; pre-persistence history remains unknowable and is never
fabricated. **A replayed row also gets its TERMINAL state back (v0.21.0)** — `turn finished` →
`done`, `session ended` → `gone` — because an unset state resolves to `working`, so the old
"restore nothing" rule had every finished session claiming a live turn after a restart. A running
state is still never restored, and `asked a question` is never restored to `needs_input`.

`POST /v1/config` whitelist grows to TWO keys: `quietHours` (as before) and
`toast` — `{"thresholdSec": int 30..3600, "enabled": bool}`, both members required, strictly
validated (else 400, nothing written). An older crabd 400s a toast write — the widget's per-key
handling must treat that as this-key-unsupported (no latch; 404 latching semantics unchanged).

**Toast action (notifier + registration):** toasts gain an "Acknowledge" button using protocol
activation — `sidecrab-ack:<sessionId>` — handled by a registered HKCU protocol
(`sidecrab-ack`) whose handler POSTs `{"sessionId": ..., "action": "ack"}` to /v1/action and
exits silently. Registration script lives in setup/ (HKCU only, idempotent, -Remove), wired into
Install/Uninstall like the AUMID. The sessionId in the URI is validated by the handler against
a conservative charset (^[A-Za-z0-9-]{1,64}$) before any HTTP call — a toast payload is data.

# Previous header (fields still accurate): schema 6 additions

> **Schema 6 (v0.6.0, 2026-08-26) is ADDITIVE over schema 5.** SUPERSEDED numbering — these
> fields now ship under `"schema": 5` per the rework above.

## v6 additions

Per session — how full the context window was on the LAST request (the input side of the
newest assistant usage record: input_tokens + cache_read_input_tokens +
cache_creation_input_tokens):
```jsonc
"contextTokens": 549300        // int | null when no usage record exists yet
```
Its denominator — how BIG that window is — arrived later, as `contextWindowTokens`; see the
v0.28.0 section at the top.

New top-level `fleet` — SideCrab observing its own components (Scheduled Task states via
schtasks query, cached ~60 s; "running" | "stopped" | "absent" | "unknown"):
```jsonc
"fleet": { "glow": "running", "toast": "running" }
```
crabd itself is omitted — if you can read this document, crabd is running. A component whose
task exists but whose last-known state can't be read is "unknown", never guessed.

Widget-only in v0.6.0: Claw'deck badge scaled up ~25%; ctx chip on session cards ("ctx 549k",
muted, next to the model chip; absent when null); fleet dots — two small labeled dots (g/t)
under the clock: green running, amber stopped, gray absent/unknown, with the not-color-alone
rule carried by the letter + a dot shape change; idle-card Dismiss (same semantics as done);
fix the small-slot (max-height 420px) question clip (clamp at 2 lines, never mid-glyph);
add a ?mock= fixture exercising the >=95% red gauge step and the timeline "+N earlier" row.



> **Schema 5 (v0.5.0, 2026-08-26) is ADDITIVE over schema 4.** crabd emits `"schema": 5`;
> the widget accepts 1–5. Anything else is a dead feed.

## v5 additions

`burn` gains a today-by-model split (from the same deduped usage records; cap 4 desc):
```jsonc
"byModel": [ { "model": "claude-fable-5", "outputTokens": 429000 } ]
```

Widget-only in v0.5.0 (no contract impact): the **Claw'deck rebrand** — the Claude Max lockup
is replaced by the Claw'deck badge (dark rounded plate #1A150F, orange border, mini pixel crab
wearing pixel sunglasses, monospace "Claw'deck" wordmark ~#F7F3EC); the main crab's fill warms
from #D97757 to the logo orange family (~#E8541F, worried-desaturation retuned to match);
tapping the RECAP header opens a today-timeline sheet built from sessions[].events merged
newest-first across sessions; the burn sheet shows the byModel split when present.



> **Schema 4 (v0.4.0, 2026-08-26) is ADDITIVE over schema 3.** crabd emits `"schema": 4`;
> the widget accepts 1–4. Anything else is a dead feed.

## v4 additions

New top-level `recap` (cached ~5 min; local read-only `git log`, never a network call):
```jsonc
"recap": {
  "sessionsToday": 9,          // sessions with any activity since local midnight — transcripts
                               // UNIONED with today's hook rows and finishes (v0.21.0), so
  "doneToday": 3,              // doneToday <= sessionsToday holds by construction
  "commits": [                 // commits since local midnight per distinct repo seen among
    { "repo": "sidecrab", "count": 12 }   // today's session cwds; cap 4, count desc
  ],
  "computedAt": "ISO"
}
```
- `recap` is `null` until the first computation completes (a few seconds after crabd starts) —
  never a zeroed document. The widget renders null as the v3 header.
- `doneToday` is a FLOOR: only Stop transitions observed by the running crabd count;
  pre-restart finishes are never reconstructed or inflated.
- Repo scope (amended 2026-08-26): session-cwd repos PLUS any paths listed in
  `~/.sidecrab/config.json` `"recapRepos": ["C:\\Dev\\sidecrab", ...]` — a session whose cwd is
  elsewhere but which DRIVES a repo would otherwise hide that repo's commits entirely. recapRepos
  is file-config only, NOT settable via /v1/config (whitelist unchanged: quietHours only).

`POST /v1/action` gains `{"action": "ack-all"}` → 204: acks EVERY unacked needs_input session
(each records an "acknowledged from Edge" event). 204 even when nothing was waiting.

New endpoint `POST /v1/config` — body `{"quietHours": {"start": "HH:MM", "end": "HH:MM"}}` or
`{"quietHours": null}` → 204: validates HH:MM strictly (else 400, nothing written), rewrites
`~/.sidecrab/config.json` PRESERVING all other keys. quietHours is the ONLY key writable over
HTTP — `allowReply` and anything else must never be settable remotely. Same CORS as /v1/action.

**limits.note semantics widened (v0.4.0):** `note` may now be non-null while `available:true` —
a caveat, not an error (e.g. "limits as of 2:41 PM" when serving a last-good reading older than
15 min through an endpoint lockout). The widget renders any non-null note, muted, regardless of
`available`; gauges stay lit on the last-good values.

Widget-only in v0.4.0 (no contract impact): tapping the CRAB = ack-all (no-op when nothing
waits); celebrating mood (both arms up ~10 s) when a session completes a turn that ran >30 min;
rare idle blink (reduced-motion-safe); done-card Dismiss (local hide until state changes);
tapping the LIMITS zone header → burn-by-session sheet from sessions[].todayOutputTokens;
quiet-hours iCUE properties that POST /v1/config on change.



> **Schema 3 (v0.3.0, 2026-08-26) is ADDITIVE over schema 2.** crabd emits `"schema": 3`;
> the widget accepts 1, 2 **or** 3. Anything else is a dead feed.

## v3 additions

*(The top-level `estate` block that shipped in v0.3.0 was the private-dashboard integration.
**Removed for publication in v0.9.0** — see the note at the top of this document. Nothing consumes
or emits it.)*

`burn` gains a 7-day series:
```jsonc
"daily": [ { "dayStart": "2026-08-20", "outputTokens": 0 } ]   // 7 entries, oldest first, local days
```

Per session:
```jsonc
"events": [ { "at": "ISO", "text": "asked a question" } ]   // cap 8, newest first: state
// transitions + hook events for THIS session since crabd started (ring buffer; empty ok)
```

Widget-only in v0.3.0 (no contract impact): escalation tiers from unacked needs_input
`stateSince` age; 24h↔7d sparkline toggle; hardware sensors row via iCUE's sensor
data-provider plugin (hidden entirely when the iCUE API is absent, e.g. dev browser).



> **Schema 2 (v0.2.0, 2026-08-26) is ADDITIVE over schema 1.** crabd emits `"schema": 2`;
> the widget accepts 1 **or** 2 (an absent v2 field renders as its v1 behavior). Anything
> other than 1 or 2 is a dead feed.

## v2 additions

Per session (all optional; `null`/absent = v1 behavior):
```jsonc
{
  "question": "string|null",       // FULL text the session is waiting on (needs_input only):
                                   // the Notification hook's message, enriched from the transcript
                                   // tail when the transcript carries a longer question
  "turnStartedAt": "ISO|null",     // set on UserPromptSubmit, cleared on Stop -> widget shows "working 14m"
  "acked": false,                  // true after POST /v1/action {action:"ack"} - widget keeps the
                                   // needs_input card visible but DROPS the panel glow/pulse for it;
                                   // cleared automatically on the session's next state transition
  "subagentDetail": [              // cap 5, running only, newest first — agents retired by a
                                   // SubagentStop are excluded, not merely trimmed away (v0.21.0)
    { "label": "string", "ageSec": 0 }
  ]
}
```

Top level:
```jsonc
"quiet": { "active": false, "start": "22:00", "end": "07:00" }   // or null when unconfigured.
// active=true -> widget dims to ambient, crab asleep, NO flash/glow/pulse; needs_input cards
// still render statically (a question keeps waiting). Config lives in ~/.sidecrab/config.json
// {"quietHours": {"start":"22:00","end":"07:00"}} - crabd computes `active`, widget only renders.
// SINCE v0.23.0 `active` is the EFFECTIVE answer (schedule + the operator's override) and the
// block carries an optional `override`; start/end may be null. See the v0.23.0 section.
```

New endpoint — touch actions:
- `POST /v1/action` body `{"sessionId": "...", "action": "ack"}` → 204. Unknown session → 404.
- `{"action": "reply", "text": "..."}` → 204 only when reply-injection is PROVEN and enabled
  (config flag `allowReply`, default false); otherwise **501** with `{"error": "reply not supported"}`.
  The widget must render the 501 path gracefully (sheet shows "not available yet").
- Same CORS as the GETs. Text is one of the widget's canned strings only — free-form input is v3.



**This document is the contract between `companion/` (crabd, the producer) and `widget/` (the consumer).
Neither side may change it unilaterally — a change lands here first, bumps `schema`, and updates both sides in one commit.**

## Transport
- crabd binds `127.0.0.1:2722` (2722 = C-R-A-B on a phone keypad), HTTP, no auth (localhost-only, read-only data).
- `GET /v1/state` → the full document below, `Content-Type: application/json`. **CORS: see the v0.16.0 §1 table above — this line's original "permissive CORS (`Access-Control-Allow-Origin: *`)" is SUPERSEDED and no longer true.** The widget still renders from iCUE's QtWebEngine origin; that origin is `null`, which is allowed and reflected.
- `GET /v1/health` → **this two-field shape is SUPERSEDED (CON-c, 2026-08-28).** The daemon serves the full 8-field diagnostic set documented under "v0.14.0 additions" above (`ok`, `version`, `uptimeSec`, `hooksSeen`, `statuslineSeen`, `lastStatuslineAgeSec`, `otlpSeen`, `originsSeen`). `ok`/`version` are unchanged, so any reader of the original two keys still works; the rest are additive and diagnostic. **Health is NOT part of the state contract** — the widget does not consume it, and nothing here bumps `schema`.
- `POST /v1/hook` → body is the raw Claude Code hook JSON from stdin (fields include `session_id`, `hook_event_name`, `cwd`, ...). Responds 204. Fire-and-forget; hooks must never block Claude Code (client timeout ≤2 s).
- The widget polls `/v1/state` every 3 s. It renders the **stale/dead-feed state** (worried crab, dimmed panel, "data as of HH:MM" banner) whenever a poll fails OR `generatedAt` is older than 30 s. Silence must never render as all-green.

## Document

```jsonc
{
  "schema": 1,
  "generatedAt": "2026-08-26T18:05:00Z",        // UTC ISO-8601; widget compares against Date.now()
  "crabd": { "version": "0.1.0", "startedAt": "ISO", "hooksSeen": 0 },

  "limits": {                                    // from the Claude OAuth usage endpoint
    "available": true,                           // false => widget shows em-dash gauges + note; NEVER zeros
    "note": null,                                // human string when available=false ("token expired — open Claude Code")
    "fiveHour":  { "utilization": 0.42, "resetsAt": "ISO" },   // utilization 0..1; NULL when available=false (CON-c) — not an always-object
    "weekly":    { "utilization": 0.18, "resetsAt": "ISO" },   // NULL when available=false (CON-c); an object otherwise (may gain exhaustAt, v0.13.0)
    "extra": [],                                 // any additional windows the endpoint reports: {label, utilization, resetsAt}
    "subscriptionType": "max",
    "rateLimitTier": "string-as-reported"
  },

  "burn": {                                      // aggregated from ~/.claude transcript JSONL (assistant.message.usage)
    "today": { "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheCreationTokens": 0, "messages": 0 },
    "hourly": [ { "hourStart": "ISO-local-hour", "outputTokens": 0 } ]   // last 24 buckets, oldest first
  },

  "sessions": [
    {
      "id": "uuid",
      "title": "string",                         // customTitle > aiTitle > first-prompt excerpt
      "cwd": "C:\\Dev\\sidecrab",
      "repo": "sidecrab",                        // null when cwd is not a git repo
      "branch": "master",                        // null likewise
      "state": "working" | "needs_input" | "done" | "idle" | "gone",
      "stateSince": "ISO",
      "lastActivityAt": "ISO",                   // max(hook event, transcript mtime)
      "lastEvent": "asked a question" ,          // short human line; for needs_input include what for
      "model": "claude-fable-5",                 // from last assistant message, SERVED VERBATIM (CON-b) — see note below
      // CON-b (2026-08-28): the `model` string is a CHANNEL, not just a label. Claude Code may append a
      //   context-window marker — e.g. "claude-fable-5[1m]" or "…[200k]" — and BOTH sides parse that
      //   marker as a ctx-fill denominator (crabd since 0.28.0, ranked above its model catalog; the
      //   widget as its fallback for a crabd below 0.28.0).
      //   INVARIANT: crabd serves `model` exactly as the transcript wrote it — it does NOT normalize,
      //   trim, or strip the marker. Two consumers depend on the marker being present; stripping it to
      //   "tidy" the label would silently break the ctx-fill denominator. crabd's own catalog lookup
      //   strips the marker to build a LOOKUP KEY and never writes that back into this field.
      //   CORRECTED 2026-08-28: an absent marker does NOT fall back to "the widget's default window
      //   size" — there has never been one, and inventing one is the thing both sides refuse. It falls
      //   through to `contextWindowTokens`, and to NO BAR when that is null.
      "speed": "standard" | "fast",
      "subagents": { "running": 0, "total": 0 },
      "todayOutputTokens": 0,
      "contextTokens": 549300,          // v0.6.0 — how full (int | null)
      "contextWindowTokens": 1000000    // v0.28.0 — how big  (int | null); see the section at the top
    }
  ],
  // sessions array is pre-sorted by crabd: needs_input, then working, then done, then idle. "gone" excluded.
  // done sessions are dropped ~10 min after stateSince unless reactivated. idle = no activity > 15 min, process may still live.
  // (A top-level "estate" block shipped here through v0.8.0 and was REMOVED in v0.9.0 — see the
  //  note at the top. It is not emitted, not read, and not reserved.)
}
```

## Session state machine (crabd owns it)
| Hook event | Transition |
|---|---|
| `SessionStart` | → `working` (new row) |
| `UserPromptSubmit` | → `working` |
| `Notification` | → `needs_input` (lastEvent from notification message). A **re-fire** carrying a **different** question is a new alert: `stateSince` moves and `acked` clears. The **same** question re-fired changes nothing (v0.20.0) |
| `Stop` | → `done`. **Always re-dates `stateSince`**, including a `done` → `done` Stop — that is how a continuation turn (tap-to-continue fires no `UserPromptSubmit`) shows its own finish. The done LEDGER is armed only by a real change INTO `done`, so a repeated Stop cannot write a second `done` line (v0.21.0) |
| `SubagentStop` | decrement `subagents.running`, and retire the stopped agent from `subagentDetail` — matched by nearest last-write, since the hook does not say which agent it was (v0.21.0) |
| `SessionEnd` | → `gone` |
| `PermissionRequest` | → `needs_input` while the hold is open, from `working` or from a session with no prior hook only (v0.20.0) |
| *(not a hook)* the `PermissionRequest` hold ending — tap, timeout or retired as stale | `needs_input` → `working`, but **only** if that alert was the hold's own (v0.20.0) |
| *(not a hook)* a completed model round-trip in the session's MAIN transcript, newer than the question | `needs_input` → `working` (v0.19.0) |

Hooks are best-effort: a killed terminal fires nothing, so crabd also ages by transcript mtime
(no writes > 15 min → `idle`; > 2 h → `gone`). A `needs_input` row is **never cleared by aging** —
a question keeps waiting however long the file is quiet. It is cleared by a newer hook event for
that session, or, since v0.19.0, by evidence the model ran again for that session (§2 above), which
is what an answer given in the app looks like from outside the CLI.

## Hard rules for both sides
- crabd reads `~/.claude` strictly read-only and **never logs, serves, or persists the OAuth token** — the token exists only in the HTTPS request to the usage endpoint.
- The widget makes exactly one kind of network call: `http://127.0.0.1:2722/v1/state` (+`/v1/health`). Everything else is bundled.
- All numbers are honest: unknown = `null`/`available:false`, never 0 or a stale value silently re-served.
