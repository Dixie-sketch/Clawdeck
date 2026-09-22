# Lane B: the additive contract text

Written in `docs/STATE-CONTRACT.md` house style, for the orchestrator to fold in under a new
version heading. Nothing here changes `/v1/state`: `schema` stays **5** and no field is added to
or removed from the document. Two additive surfaces, both transport.

---

## §1. `GET /v1/events` — the state document, pushed

A `text/event-stream` that carries the same document `/v1/state` serves, one frame per NEW
snapshot. The widget polled every 3 s, so a question the operator was standing in front of could
sit unlit for that long; this route closes that gap and leaves the poll as the fallback.

**It sits behind the SAME two gates as every other route, in the same order**, and reuses the same
predicates rather than carrying copies of them:

| Request | Answer |
|---|---|
| `Host` that is not `127.0.0.1:<bound port>` or `localhost:<bound port>` | `421 {"error":"host not allowed"}` |
| a present `http(s)` `Origin` that is not one of this socket's own | `403 {"error":"cross-site request refused"}` |
| this socket's own origin | streamed, with that origin reflected in `Access-Control-Allow-Origin` + `Vary: Origin` |
| `null`, absent, and non-web origins | streamed, unchanged |
| a ninth concurrent subscriber | `503 {"error":"too many subscribers"}` |

A long-lived readable stream of `/v1/state` is the widest read surface crabd has (cwds, titles,
the full question text, `pendingPermission`), and an `EventSource` a visited page opened would go
on delivering all of it. The gates run BEFORE the subscriber cap, so a refused request never takes
a slot.

**Headers on a served stream:**

```
Content-Type: text/event-stream
Cache-Control: no-store
X-Accel-Buffering: no
Connection: close
```

No `Content-Length` and no chunking: the body ends when the connection does. `X-Accel-Buffering`
is there because a buffering intermediary is the one failure mode that turns a push transport into
a slower poll with no error anywhere.

**The frames, in order:**

| Frame | When | Body |
|---|---|---|
| `retry: 3000` | on connect | the reconnection time a client that reconnects on its own should use |
| `event: state` | immediately | `data: ` + the full `/v1/state` JSON, ONE line |
| `event: error` | instead of the first state frame, when no snapshot has been built yet | `data: {"error":"state not built yet"}`, and the stream STAYS OPEN |
| `event: state` | every time the builder publishes a NEW snapshot | as above |
| `event: ping` | every 15 s of quiet | `data: {}` |

**"New" is object identity OR `generatedAt`.** `generatedAt` has one-second resolution, so two
builds inside the same second carry the same string and a string-only test would drop the second
one. The same snapshot is never sent twice.

**Cold start is an `error` event and not a close.** `/v1/state` answers `503` and hangs up; the
stream says the same thing and then waits, because the first build is seconds away and closing
would send the client into its backoff for nothing.

**What it does not do.** It never blocks the builder: detection is a 250 ms read of the builder's
`state` property, which takes the builder lock only long enough to copy a reference and never
across a socket write. A client hang-up is detected on the socket rather than on the next write,
so a slot comes back in 250 ms instead of up to a ping; a stop event ends every open stream when
the server shuts down.

## §2. The widget's transport, standalone only (widget)

In the standalone host — and only there — the widget subscribes to `baseUrl() + '/v1/events'` when
`EventSource` exists. Inside iCUE it polls exactly as before: the widget's origin there is `null`,
and an `EventSource` is one more thing to go wrong on a surface with no devtools for a saving of
two and a half seconds.

- Every `state` frame goes through **the same `acceptDoc`** the poll uses — the same schema check,
  the same `generatedAt` check, the same dead-feed latch. A frame that is not JSON is a dead feed,
  exactly as an unparseable poll body is.
- The 3 s poll **skips while the stream is open** and takes over the moment it is not.
- On a transport error the page closes the stream itself, polls at once, and retries the stream on
  **3 / 6 / 12 / 24 / 30 s**, reset by the next `state` frame. The browser's own reconnect is not
  used: it would reconnect on a fixed clock for ever.
- **The stale and dead-feed rendering is unchanged in every mode.** `generatedAt` older than 30 s,
  or a failed poll, is the stale state; a `ping` is liveness only and touches nothing the stale
  logic reads.
- Diagnostic: the page exposes `window.__sidecrabTransport = {mode: 'sse' | 'poll', lastEventAt}`
  and logs one line on each switch.

## §3. The panel host web-message bridge (panel host)

The standalone host is the only surface that can SAVE a setting: iCUE owns its own property sheet
and a widget cannot write it back, and a plain browser at `/panel/` has no file to write. So the
host accepts exactly two messages from the page it loaded, and `IsWebMessageEnabled` is on while
`AreHostObjectsAllowed` stays off — a JSON channel, never a live .NET surface.

**Every message is checked on `Source` first**, against the same navigation lock the window
already uses: a message from any frame that is not `http://127.0.0.1:<port>/panel/…` is logged and
dropped.

| Page sends | Host answers |
|---|---|
| `{"type":"host-info"}` | `{"type":"host-info", "version", "settingsPath", "hasToken": bool}` — the presence of the pairing code, never the code |
| `{"type":"settings", "props": {...}}` | writes the file, then `{"type":"settings-saved", "props": {...}}` carrying what was ACTUALLY stored |

**The props a page may write, and nothing else:**

| Key | Type | Rule |
|---|---|---|
| `clock24`, `alertFlash`, `crabStyle`, `touchDiag`, `chime` | bool | the JSON literals only; a string `"true"` or a `1` is dropped |
| `textColor`, `accentColor`, `backgroundColor` | string | `#RRGGBB` exactly, upper-cased; three digits, a name, an alpha channel or a CSS function is dropped |
| `transparency`, `chimeVolume` | number | rounded and clamped to 0..100 |

Unknown keys are dropped. Wrong types are dropped and never coerced — a coerced setting is a value
nobody chose being written to the operator's file. **If nothing survives, nothing is written.**

`panelToken`, `crabdPort` and `display` are deliberately absent from that list: the pairing code is
the one secret a visited page must never read back or replace, and the port and the display are how
the host finds the companion and the glass, so a page that could move either could point the window
elsewhere or hide it.

The file is merged and written atomically (same-directory temp, then a rename), so `crabdPort`,
`display`, `devtoolsPort` and any prop this sheet does not edit all survive a save. The host's own
write does not trigger its watcher's page reload: the page applied the props live the moment it got
`settings-saved`.

## §4. Audio in the standalone host

`--autoplay-policy=no-user-gesture-required` joins the WebView2 browser arguments. The window never
activates (`WS_EX_NOACTIVATE`) and the alert the chime answers arrives while nobody is touching the
glass, so Chromium's default policy leaves the `AudioContext` suspended and the chime is silent with
no error anywhere.
