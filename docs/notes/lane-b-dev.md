# Lane B: DEV.md material

For the orchestrator to fold into `widget/DEV.md` under the version it assigns.

---

## The transport: push, with the poll as the fallback

crabd serves `GET /v1/events` as a `text/event-stream`, and in the **standalone host only** the
widget subscribes to it. The rules, and why each is what it is:

- **`sseWanted()`** is `isStandalone() && !mockName && typeof EventSource !== 'undefined'`. A
  fixture is a file, not a stream; and inside iCUE the widget's origin is `null`, so an
  `EventSource` there is one more thing to go wrong on a surface with no devtools for a saving of
  two and a half seconds.
- **Every frame goes through `acceptDoc`**, the poll's own path. That is the one place the two
  transports could have forked, and a frame that is not JSON is a dead feed exactly as an
  unparseable poll body is.
- **`poll()` returns early while `sseDelivering()`** — read off `EventSource.readyState`, because
  the browser owns the connection's state and a second copy of it can disagree. `diagFlush()` is
  deliberately ABOVE that early return: whether state is arriving by push says nothing about
  whether captured taps should reach the companion.
- **The backoff is ours, not the browser's.** On a transport error the page closes the stream,
  polls at once and reconnects on 3 / 6 / 12 / 24 / 30 s, reset by the next `state` frame. Left
  to itself `EventSource` would reconnect on its `retry:` clock for ever.
- **`window.__sidecrabTransport = {mode, lastEventAt}`** is the diagnostic, and one `logLine` goes
  out on each switch.

**TRAP — one `error` listener, two different events.** `EventSource` dispatches BOTH a server-sent
`event: error` frame and its own transport failure as type `"error"`. The frame is a `MessageEvent`
and carries `data` (crabd sends one while it has no snapshot yet and then keeps the stream open);
the transport failure carries none. Treating them alike tears the stream down at exactly the moment
crabd is about to start serving.

### Testing the stream off-glass

The `?mock=` fixtures are files, so they never exercise this path. Stand a crabd socket up
directly instead — crabd's own `main()` writes `~/.sidecrab/history.jsonl` and reads the
operator's live `~/.claude`, and neither belongs in a test:

```python
import crabd, threading, time
crabd.PANEL_DIR = pathlib.Path("widget")          # serve THIS tree at /panel/

class Stub:                                        # one dict, no files touched
    def __init__(self): self._state = {...}        # a contract-shaped document
    @property
    def state(self): return self._state
    def publish(self, d): self._state = d

crabd.Handler.builder = Stub()
server = crabd.CrabdServer(("127.0.0.1", 0), crabd.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
```

Then open `http://127.0.0.1:<port>/panel/` in headless Edge over CDP and read:

| Read | What it says |
|---|---|
| `window.__sidecrabTransport` | `{"mode":"sse","lastEventAt":…}` once the first frame lands |
| `window.sseSource.readyState` | `1` OPEN, `0` CONNECTING (the retry ladder), `2` CLOSED |
| `server.sse_slots.count` | how many subscribers crabd is holding |
| a `window.fetch` wrapper counting `/v1/state` | that the poll is genuinely skipping, rather than assumed to be |

Wrap `window.acceptDoc` from `Page.addScriptToEvaluateOnNewDocument` to stamp each document's
arrival, and the push latency is measurable end to end. **Measured on this machine, five
publishes: 105.0 / 107.8 / 118.6 / 109.6 / 229.8 ms, mean 134.2** — consistent with crabd's 250 ms
detection poll, against 3000 ms of poll interval before it.

Kill crabd with the page open and the fallback is observable in the same session: `mode` goes back
to `poll`, the `/v1/state` count starts climbing, `pollFailed` is true, `computeStatus()` is
`stale` and the banner reads *"crabd not responding — data as of 5:26 PM"* — the same dead-feed
rendering the poll has always produced.

## The settings sheet

A gear chip on the clock row opens the eighth sheet mode. It is shown when `isStandalone()` or
`mockName`, and **never inside iCUE**: the property sheet is the settings surface there and a
widget cannot write a property back, so a gear there would be a control that silently does nothing.

- `&settings=1` (mock-gated, like every other dev flag) opens the sheet on boot for a screenshot.
- The rows are built in JS into one empty `<div id="sheetSettings">`. Thirty controls of static
  markup would be thirty chances to ship an unclosed element into a file iCUE parses as strict XML.
- Every control binds its own listener on the element it creates, so `onSheetClick` needs no new
  branch — and the buttons wear `.set-btn`, not `.sheet-btn`, so the generic branch cannot claim
  them and POST an action of `null`.
- **Save needs the panel host.** In a plain browser the sheet renders and says so:
  *"saving needs the SideCrab panel host"*.

**PLACEMENT, measured at 2560x720 before it was written.** The clear column to the right of the
seconds is 86 px and the moon chip already spends 70 of them, so a second touch-floor control there
sits 40 px ON the seconds. The gear goes LEFT of the hours instead, in the 56.9 px the diag chip
already documents, out of flow like both of its neighbours so the identity column (and therefore
the crab) pays nothing. Measured after: gear `0..50.4`, hours box from `116.3` — 65.9 px clear.

**The sheet is TWO COLUMNS, and that was measured too.** Ten control rows in one column are 569 px
against the 541 px the sheet gives them, so the list scrolled and the last row sat under the
actions bar. `repeat(auto-fit, minmax(55 units, 1fr))` is two columns in 922 px of region and folds
back to one on a narrower panel; the result is 332 px with no scroll at all, and the actions row is
`position: sticky` for the case where it does.

## The chime

`chimeDecision(prev, next, quietActive, chimeEnabled, now, lastChimeAt)` is a pure function, so
every gate is provable without an audio device — `node widget/tests/test_chime.js`, 29 checks.

| Gate | Why |
|---|---|
| `prev === null` (boot) | a panel that starts while three sessions are waiting must not play three chimes; nothing on the glass changed, only what the page knows |
| an id absent from `prev`, after boot | DOES chime. A done row is dropped about ten minutes after it finishes, so a question in it comes back as an id the last document did not have, and that is a new alert |
| `prev[id] === 'needs_input'` | the same question still waiting. Re-render, not news |
| `id === 'smoke-test'` | the smoke test manufactures a `needs_input` row to prove the panel lights up; a chime for it is the instrument ringing at its own test |
| `quietActive`, `chimeEnabled` | hard offs. An absent `quiet` block reads as false — crabd omits it entirely when no quiet hours are set |
| 5 s cooldown | a fleet landing four questions at once is one chime |

**Reduced motion is deliberately NOT a gate.** It is a statement about animation, and the operator
who turned it on did not ask for silence.

The sound is two sine oscillators, A5 then D6, about 350 ms, with a 12 ms attack and an exponential
tail — rising, because a falling pair reads as something finishing and this is something starting to
wait. There is no audio file: a binary in a package iCUE validates and a store reviews, for four
lines of Web Audio, is not a trade worth making. `exponentialRampToValueAtTime` throws on a zero
target, hence the 0.0001 floor at both ends; a square-edged gain is an audible click at this level.

**Testing it off-glass.** Headless Edge has Web Audio, so `playChime(60)` returns true and
`chimeCtx.state` reads `running` under `--autoplay-policy=no-user-gesture-required`. What a
headless browser cannot tell you is whether it sounds right — that is a desk check. To prove the
GATES end to end over a real stream, wrap `window.playChime` and publish documents at the stub
builder:

```
working              -> []                       no chime
-> needs_input       -> [{v:60}]                 one
-> working -> ask    -> [{v:60}]                 still one: inside the 5 s cooldown
(wait 5 s) -> ask    -> [{v:60},{v:60}]          two
```
