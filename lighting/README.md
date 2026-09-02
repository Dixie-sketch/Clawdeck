# sidecrab-glow — the room as the alert

When a Claude session is waiting on you, your Corsair lighting breathes terracotta
(`#D97757`). When you answer it — or ack the card in the widget, or quiet hours
start — the lights go straight back to your normal iCUE profile.

`sidecrab-glow` is a **standalone, read-only consumer** of the crabd feed. It polls
`GET http://127.0.0.1:2722/v1/state` every 3 s and nothing else: it never writes to
crabd, never changes an iCUE setting, and holds the lights only while an alert is
actually up.

## The rule it implements

Glow when **any** session has `state == "needs_input"` and `acked == false`.

Suppressed entirely by:

| Condition | Behaviour |
|---|---|
| `quiet.active == true` | dark — the question keeps waiting, the room stays quiet |
| every `needs_input` acked | dark |
| crabd unreachable, or `generatedAt` older than 30 s | **release** — no light show on a dead feed |
| `schema` above the accepted range (currently 1-5) | dark (dead feed) |

**Escalation:** after 5 minutes unacked, the pulse brightens and quickens slightly
(0.22–0.80 over 3.6 s → 0.45–1.00 over 2.4 s). The phase is continuous, so it
brightens without a visible jump.

The decision is a pure function — `decision.decide(state_doc, now)` — which is why
it can be tested without a Corsair device in the room.

## Prerequisite: the iCUE SDK toggle

iCUE must be running with **Settings > Enable SDK / software integrations** on.
On a recent iCUE install this toggle is on by default and the handshake succeeds.
If it is switched off the handshake returns `CSS_ConnectionRefused` and the
process logs one line —

```
WARNING iCUE SDK unavailable: session state CSS_ConnectionRefused — enable iCUE Settings > Enable SDK / software integrations
```

— then keeps polling and retries the handshake every 60 s. It never crash-loops and
never dies; no iCUE simply means no light.

> **Known limitation — case chassis lighting:** on some setups the SDK connects but
> reports **0 addressable LEDs**. Case lighting attached through an *iCUE LINK System
> Hub* is a common example: the hub's own `led_count` is 0 and the attached fan/RGB
> subdevices are visible in iCUE but not surfaced as lightable by SDK 4. The service
> handles this as its own degraded state — "connected but nothing to pulse" — and
> re-enumerates every 60 s, so lighting starts working the moment a device does
> appear (plug in a Corsair keyboard and it is picked up without a restart).

## Running it

```powershell
python -m pip install -r requirements.txt
python sidecrab_glow.py
```

Options:

| Flag | Purpose |
|---|---|
| `--selftest` | **read this first when nothing lights** — see below |
| `--dry-run` | no SDK at all; logs each decision against simulated hardware |
| `--once` | one poll, log the decision, exit — the quickest smoke test |
| `--pulse-test 5` | 5 s of visible pulse, then release. Ignores the feed |
| `--url URL` | point at a different feed (also `SIDECRAB_URL`) |
| `--verbose` | debug logging |

Logs go to stdout **and** `~/.sidecrab/glow.log` (1 MB × 2 rotating), which is what
you read when it runs hidden at logon. (Under `pythonw` there is no stdout at all,
so the file is the whole record — the stream handler is skipped rather than left
silently throwing on every line.)

## "Why is my case dark?" — `--selftest`

```powershell
python sidecrab_glow.py --selftest
```

It handshakes once and reports what it found, ending in a one-line verdict:

```
sidecrab-glow selftest
  SDK reachable         : no
  session state         : CSS_Timeout
  devices seen by SDK   : 0
  devices with LEDs     : 0
  TOTAL lightable LEDs  : 0
  device inventory      : no session
  devices held          : 0

  CONTROL: no session — nothing held (session state CSS_Timeout)
  VERDICT: SDK unavailable: session state CSS_Timeout — iCUE may be running
           without its SDK server; check iCUE Settings > Enable SDK / software
           integrations
```

**Two verdicts, because they answer two different questions.** `VERDICT` is "could this
machine ever glow". `CONTROL` is "does the process hold the lights right now". They agree
on a healthy box and diverge after an iCUE control loss — SDK reachable, LEDs paintable,
holding nothing — which is exactly the state that used to read as healthy while the case
stayed dark. See [Reacquisition](#reacquisition-when-icue-takes-the-lights-back).

The three verdicts, and the exit code that goes with each, so a script can assert
on it:

| Verdict | Exit | Means |
|---|---|---|
| `would glow — N addressable LED(s)…` | 0 | everything is ready; if it is still dark, the feed says no alert |
| `no lightable LEDs — …` | 1 | the SDK is connected but exposes nothing paintable (the LINK-hub case below) |
| `SDK unavailable: <reason>` | 2 | the handshake never succeeded; the reason is the session state |

**The same verdict line is written to the log at startup**, so a run that has
already ended still answers the question — no debugger, no re-run.

> Note the `devices seen by SDK` / `devices with LEDs` split. A LINK hub shows as
> `1 / 0`: reporting a flat "0 devices" for that is what makes the state look like
> a connection failure when it is not one.

**"iCUE is running" is not evidence the SDK is up.** Measured 2026-08-26 on this
machine: `iCUE.exe` alive in the interactive session, and every handshake still
`CSS_Timeout`, because nothing was listening on the SDK endpoint — there was no
Corsair SDK named pipe at all. Check the toggle, not the process list.

## Reacquisition — when iCUE takes the lights back

A session or a control grant can go away under a running process, and the process
has to notice. Three things can happen, and each has its own recovery:

| What happens | What glow does |
|---|---|
| The session dies (`CSS_ConnectionLost` / `CSS_Closed`) | every device and every claim is dropped, `acquired` goes false, and the 60 s handshake retry re-enumerates and re-acquires at the next alert |
| One device's control grant is revoked (`set_led_colors` → `CE_NoControl` / `CE_NotConnected`) | that device alone leaves the held set, so the next `acquire()` takes it again — the others keep painting |
| Every paint fails for 5 consecutive frames (0.25 s) | control is presumed gone; it is dropped and re-acquired after a 2 s back-off |

**Clearing the connection flag is not enough, and that was the bug.** Retaining the
acquired-device set across a control loss left the render loop believing it was
already acquired, so it skipped `acquire()` when the session came back and painted
into a grant that no longer existed. Every `set_led_colors` then failed into a
return value nobody read: a permanently dark case behind a process whose log said
the handshake was healthy. Fixed 2026-08-27 — the adapter drops what a dead session
granted, `paint()` distinguishes "you no longer hold this" from "that colour was
rejected", and the loop acts on a `paint()` that returns False.

> The drop happens on the MAIN thread, not in the SDK callback that detects it.
> `on_state` runs on the SDK's own thread while the render thread is iterating the
> device list; the callback publishes a reason and the next main-thread call reaps.
> Blocking inside a native callback to take a lock is the wrong trade in the one
> file whose headline bug is a native-callback lifetime.

**Hot-plug and partial acquisition.** Devices used to be re-enumerated only when the
LED total was zero, and acquisition counted as complete as soon as **any one** device
succeeded. A keyboard plugged in next to a working device was therefore never seen,
and a device that refused control was never asked again. During an active alert glow
now re-enumerates every 30 s — **bounded, not per frame**: `get_devices` is an SDK
round-trip and the render loop runs at 20 fps, so per-frame would be ~1,200
enumerations a minute. `acquire()` is per-device and skips what is already held, so
the rescan retries only what is missing. A device that disappears between
enumerations is forgotten rather than left in the held set.

## Autostart

`setup/Install-SideCrab.ps1 -WithGlow` registers the logon Scheduled Task for you.
Its shape, if you would rather wire it up by hand:

| Field | Value |
|---|---|
| Trigger | At log on (current user) |
| Action | `pythonw <repo>\lighting\glow_launcher.pyw` |
| Start in | `<repo>\lighting` |
| Window | Hidden |
| Restart | On failure, every 1 min |
| Run only when user is logged on | Yes — iCUE is a per-session user process |

It must **not** run as SYSTEM or with highest privileges: the SDK talks to the iCUE
process in the interactive session, so a SYSTEM task would never see a device.

> **The `SideCrab-glow` task was parked Disabled** because glow hard-crashed
> within seconds of starting under it. That is **fixed** — see the first trap
> below; the crash was a use-after-free in `icue.py`, not anything about
> Scheduled Tasks, consoles or `pythonw`. Measured after the fix, 2026-08-26:
> the real entry points survive 90 s in all four contexts that used to kill them,
> and 220 s across three 60 s handshake retries under `pythonw` +
> `DETACHED_PROCESS` (no console at all). Re-enabling the task is a separate,
> deliberate decision: `Enable-ScheduledTask SideCrab-glow`.
>
> It will still not light anything on this machine — `--selftest` says why — but
> it will stay up and say so, instead of crash-looping.

## Tests

```powershell
python -m unittest discover lighting/tests
```

**100 tests** — 43 decision/render + 24 SDK lifetime + 33 control recovery. No SDK,
no network, no hardware: SDK calls sit behind `icue.IcueAdapter` and the tests drive
`icue.NullAdapter` or a fake `cuesdk` instead. They cover ack suppression, quiet
suppression, dead-feed and stale-feed release, schema handling, the 5-minute
escalation boundary, malformed-input tolerance, the pulse curve, and the
acquire → paint → release lifecycle.

`tests/test_sdk_lifetime.py` covers the session lifetime, the `enum_name` trap and
the selftest verdicts. It swaps a fake into `sys.modules["cuesdk"]`, because the
real SDK cannot be driven into its failure states on demand and its failure mode
is an interpreter crash rather than an exception. Its four guards were checked by
mutation (reintroduce the bug, watch the test fail) rather than by inspection.

`tests/test_control_recovery.py` covers [reacquisition](#reacquisition-when-icue-takes-the-lights-back)
at two seams — the adapter against a fake SDK with a paint path, and the render
loop against `NullAdapter` — because the shipped bug lived in the gap between them
and neither half could see it alone. Mutation-proven 2026-08-27, five bugs
reintroduced one at a time:

| Bug reintroduced | Failures |
|---|---|
| a lost session keeps its acquired devices (the shipped bug) | 3 |
| `paint()`'s return value discarded again | 2 |
| re-enumerate only when the LED total is zero | 2 |
| acquisition complete as soon as any one device succeeds | 3 |
| `CE_NoControl` / `CE_NotConnected` not told apart from an ordinary paint error | 2 |

**This machine exposes zero lightable LEDs and has no iCUE SDK server**, so none of
the above is observable here at runtime — the tests are the only place it can be
checked, which is the same reason `test_sdk_lifetime.py` exists.

## Files

| File | Role |
|---|---|
| `decision.py` | pure alert decision — no SDK, no clock, no network |
| `icue.py` | the only file that touches the Corsair SDK, plus `NullAdapter` |
| `sidecrab_glow.py` | entry point: poll thread, 20 fps render loop, `--selftest`, signals |
| `glow_launcher.pyw` | the Scheduled Task's entry point — `.pyw` so no window flashes at logon |
| `tests/test_decision.py` | the headless decision + render suite |
| `tests/test_sdk_lifetime.py` | session lifetime, enum trap, selftest verdicts |
| `tests/test_control_recovery.py` | control loss, hot-plug, partial acquisition |

## Traps worth knowing (measured, not assumed)

* **The `CueSdk` object must outlive the process — even when the handshake
  FAILS.** `connect()` hands the native SDK a pointer to a ctypes thunk stored on
  the CueSdk instance, and the native side then calls it about twice a second
  forever; there is no way to unregister it. So letting that object be collected
  is a use-after-free, and it kills the interpreter with no traceback about a
  second later. `IcueAdapter.connect()` therefore publishes `self._sdk` *before*
  anything that can raise, and a retry never builds a second CueSdk.
  This was live until 2026-08-26 and is what kept the Scheduled Task parked. The
  symptom looked console-shaped — `pythonw` died, interactive `python` seemed
  fine — and it is not: the A/B that settles it is one process type, construct →
  connect → drop the local (`0xC000001D` at 1.2 s) versus the identical run
  holding one extra reference (80 callbacks, 40 s, exit 0). The exception code
  varies with whatever lands in the freed page — `0xC000001D`
  ILLEGAL_INSTRUCTION, `0xC0000005` ACCESS_VIOLATION, `0xC0000096`
  PRIVILEGED_INSTRUCTION. **A spread of NTSTATUS codes across variants is one
  memory bug, not four environment bugs**; reading it as an environment
  difference cost a day of console workarounds that could never have worked.
  Guarded by `tests/test_sdk_lifetime.py` — the only place it *can* be guarded,
  since an interpreter crash raises nothing to assert on.
* **`cuesdk`'s enums are not `enum.Enum`.** `Enumeration` is hand-rolled and has
  no `.name` at all, so `err.name` raises `AttributeError` on exactly the error
  paths that exist to report a problem (it was live in `acquire()` and `paint()`),
  and `getattr(x, "name", str(x))` silently degrades to the long
  `"CorsairError.CE_NoControl"` form. Use `icue.enum_name()`.
* **The session callback only fires on CHANGE.** A retry must not block waiting
  for a fresh event: if the session came good while you were not looking, that
  transition has already happened and no further event is coming. Trust the last
  state reported.
* **Never call `CueSdk.disconnect()`.** On cuesdk 4.0.84 it hard-crashes the
  interpreter (`0xC000001D`, no traceback, no atexit handlers). `release_control`
  per device plus normal process exit is the clean teardown — iCUE reclaims the
  lights either way. `icue.py` has no `disconnect()` call anywhere, on purpose.
* `release_control(None)` raises `ctypes.ArgumentError` despite its `Optional` type
  hint. The device id is mandatory.
* `connect()` is asynchronous — it returns `CE_Success` immediately, and the session
  is only usable once the callback reports `CSS_Connected`. Reading devices before
  that returns a server version of `0.0.0` and no devices.
* The session callback is a ctypes thunk: keep a strong reference or it is
  garbage-collected out from under the native side.
