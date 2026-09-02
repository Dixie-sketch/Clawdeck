"""Thin iCUE SDK adapter — the only file in SideCrab that touches Corsair's SDK.

Everything above this file deals in plain (r, g, b) tuples, so the decision logic
and its tests run headless with no SDK, no iCUE and no hardware.

Measured against iCUE 5.49.34 / SDK server 4.0.19 / cuesdk 4.0.84:

* **The `CueSdk` object must outlive the process, even when the handshake FAILS.**
  `CueSdk.connect()` stores the ctypes CFUNCTYPE thunk on the CueSdk instance and
  hands its address to the native SDK, which then calls it about twice a second
  *forever* — there is no way to unregister it (`disconnect()` is unusable, below).
  Dropping the CueSdk therefore frees a thunk the native side still calls: a
  use-after-free that kills the interpreter with no traceback and no atexit
  handlers, ~1 s after the failed handshake. This is why `self._sdk` is assigned
  BEFORE anything in `connect()` can raise, and why a retry never constructs a
  second CueSdk.
  Proven 2026-08-26 by an A/B in one process type: construct → connect → let the
  local die → `0xC000001D` at 1.2 s; identical run holding one extra reference →
  80 callbacks, 40 s, exit 0. The exception code varies with whatever lands in the
  freed page (`0xC000001D` ILLEGAL_INSTRUCTION, `0xC0000005` ACCESS_VIOLATION,
  `0xC0000096` PRIVILEGED_INSTRUCTION) — that spread is the signature of the bug,
  not four different bugs. It has nothing to do with consoles or pythonw.
* **Never call `CueSdk.disconnect()`.** It hard-crashes the interpreter
  (0xC000001D ILLEGAL_INSTRUCTION, no traceback, no atexit handlers). Releasing
  per-device control and letting the process exit is the clean teardown — iCUE
  reclaims the lights either way. `release()` is therefore the whole shutdown path.
* `release_control(None)` raises ctypes.ArgumentError despite the Optional type
  hint — the device id is mandatory, so we release each device by id.
* `connect()` is asynchronous. It returns CE_Success immediately and the session
  only becomes usable when the callback reports CSS_Connected; reading device
  state before that returns a server version of 0.0.0 and no devices.
* The callback is a ctypes thunk: keep a strong reference (`self._cb`) or it is
  garbage-collected out from under the native side.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger("sidecrab.glow.icue")

CONNECT_TIMEOUT_SEC = 20.0

# Above iCUE's own profile so the alert wins, with headroom left for anything a
# user deliberately puts on top.
LAYER_PRIORITY = 200

# Session states that void everything acquired under the session — not just the
# connection flag. Compared by NAME so a member absent from another SDK build is a
# missed recovery at worst, never an AttributeError on the recovery path itself.
LOST_SESSION_STATES = frozenset({"CSS_Closed", "CSS_ConnectionLost"})

# set_led_colors errors meaning "you no longer hold this device", as opposed to
# "this one paint failed". Treated as control loss: the device leaves the held set
# so acquire() takes it again, instead of every frame failing forever.
NO_CONTROL_ERRORS = frozenset({"CE_NotConnected", "CE_NoControl"})


class IcueUnavailable(Exception):
    """The SDK could not be reached — the caller degrades and retries later."""


def enum_name(value):
    """Short name of a cuesdk enum value — "CE_NoControl", not the class path.

    TRAP: cuesdk's `Enumeration` is hand-rolled, NOT `enum.Enum`. It has no
    `.name` attribute at all, so `err.name` raises AttributeError on exactly the
    SDK error paths that exist to report a problem, and the `getattr(x, "name",
    str(x))` idiom silently degrades to the long `str()` form. Both were live
    here until 2026-08-26.
    """
    if value is None:
        return "none"
    name = getattr(value, "name", None)
    if isinstance(name, str):  # a real enum.Enum, should cuesdk ever ship one
        return name
    return str(value).rsplit(".", 1)[-1]


def lighting_verdict(connected, led_count, reason=None):
    """One line answering "why is my case dark?". Pure — no SDK, no hardware.

    Shared by `--selftest` and the startup log on purpose: an operator reading
    the log and an operator running the selftest must never get different
    answers about the same machine.
    """
    if not connected:
        return f"SDK unavailable: {reason or 'handshake has not succeeded'}"
    if not led_count:
        return (
            "no lightable LEDs — the SDK is connected but exposes 0 addressable "
            "LEDs (an iCUE LINK System Hub reports 0; its fans are not surfaced "
            "to SDK 4). Nothing can be pulsed until a lightable device appears."
        )
    return f"would glow — {led_count} addressable LED(s) ready to pulse"


def acquisition_verdict(connected, device_count, held_count, lost_reason=None):
    """One line answering "do we hold the lights RIGHT NOW?". Pure — no SDK.

    Deliberately a second verdict rather than more words in `lighting_verdict`,
    which answers a different question: "could this machine ever glow". After an
    iCUE control loss those two answers diverge — the SDK is reachable, the LEDs
    are paintable, and we hold nothing — and reporting only the first one is what
    let a permanently dark case read as a healthy process.
    """
    if not connected:
        return f"no session — nothing held ({lost_reason or 'handshake has not succeeded'})"
    if not device_count:
        return "session up, but no paintable device to hold"
    if not held_count:
        return (
            f"session up, control NOT held on any of {device_count} device(s) — "
            "will acquire at the next alert"
        )
    if held_count < device_count:
        return (
            f"partial control — {held_count} of {device_count} device(s) held; "
            "the rest are retried during the alert"
        )
    return f"control held on all {device_count} device(s)"


class IcueAdapter:
    """Owns the SDK session and the set of devices we paint.

    Lifecycle: connect() → acquire() → paint() … → release(). Safe to call
    release() when nothing was ever acquired.
    """

    def __init__(self):
        self._sdk = None
        self._cb = None  # strong ref: see module docstring
        self._connected = False
        self._devices = []  # [(device_id, [led_id, ...])]
        self._held = set()  # device ids we currently hold control of
        self._state_evt = threading.Event()
        self._last_state = None
        # Devices the SDK admits to, INCLUDING the zero-LED ones we cannot paint.
        # Kept apart from device_count so the selftest can say "1 device, 0 LEDs"
        # instead of the far more confusing "0 devices".
        self._sdk_device_count = 0
        # Set by the SDK callback thread, consumed by the main thread. See _reap().
        self._pending_loss = None
        self._session_lost_reason = None

    # ---------- session ----------

    @property
    def connected(self):
        return self._connected

    @property
    def held_count(self):
        return len(self._held)

    @property
    def unheld_count(self):
        """Paintable devices we do NOT hold — what a mid-alert retry is for."""
        return sum(1 for did, _ in self._devices if did not in self._held)

    @property
    def session_lost_reason(self):
        """Why control was last dropped, for the log and the selftest."""
        return self._session_lost_reason

    def _reap(self):
        """Main-thread half of control loss: drop what the dead session owned.

        Clearing `_connected` alone was the CD-22 bug: `_held` kept device ids the
        SDK no longer grants us, so the caller still believed it was acquired,
        skipped acquire() when the session came back, and every paint failed
        silently — a dark case behind a healthy-looking process.

        Deferred out of the callback rather than done there because `on_state` runs
        on the SDK's own thread while the render thread iterates `_devices`/`_held`.
        The callback publishes a reason (one store); the mutation stays
        single-threaded. Blocking inside a native callback to take a lock would be
        the worse trade in a file whose headline bug is a native-callback lifetime.
        """
        reason, self._pending_loss = self._pending_loss, None
        if reason is None:
            return False
        self._session_lost_reason = reason
        if self._devices or self._held:
            log.warning(
                "iCUE control lost (%s) — dropping %d device(s), %d held; "
                "reacquisition will be attempted at the next alert",
                reason, len(self._devices), len(self._held),
            )
        # Rebind rather than mutate: a render-thread iteration already in flight
        # finishes against the old list instead of raising.
        self._devices = []
        self._held = set()
        self._sdk_device_count = 0
        return True

    @property
    def device_count(self):
        return len(self._devices)

    @property
    def led_count(self):
        return sum(len(leds) for _, leds in self._devices)

    @property
    def sdk_device_count(self):
        """Every device the SDK enumerated, paintable or not."""
        return self._sdk_device_count

    @property
    def last_state_name(self):
        """Latest session state, for the selftest and the log."""
        if self._last_state is None:
            return "no handshake attempted"
        return enum_name(self._last_state)

    def connect(self):
        """Handshake with iCUE. Raises IcueUnavailable with a human reason.

        Safe to call repeatedly: the FIRST call registers the native session and
        its callback for the life of the process, every later call just re-reads
        the state the native side is already producing. Calling CorsairConnect a
        second time would register a second thunk we could never unregister.
        """
        self._reap()
        try:
            from cuesdk import CueSdk, CorsairError, CorsairSessionState
        except Exception as e:  # ImportError, or a broken native load
            raise IcueUnavailable(f"cuesdk import failed ({e})") from e

        terminal = {
            CorsairSessionState.CSS_Connected,
            CorsairSessionState.CSS_ConnectionRefused,
            CorsairSessionState.CSS_Timeout,
            CorsairSessionState.CSS_Closed,
            CorsairSessionState.CSS_ConnectionLost,
        }

        if self._sdk is None:
            self._state_evt.clear()
            sdk = CueSdk()

            def on_state(evt):
                state = evt.state
                self._last_state = state
                if state in terminal:
                    self._state_evt.set()
                if enum_name(state) in LOST_SESSION_STATES:
                    # Session died under us; the main loop re-handshakes. Both
                    # stores are plain assignments on purpose — see _reap().
                    self._connected = False
                    self._pending_loss = f"session state {enum_name(state)}"

            # ORDER IS LOAD-BEARING: both refs are stored before the first thing
            # that can raise. Everything below can leave without connecting, and
            # a CueSdk that goes out of scope while the native SDK still calls
            # its thunk crashes the interpreter (see the module docstring).
            self._sdk = sdk
            self._cb = on_state
            err = sdk.connect(on_state)
            if err != CorsairError.CE_Success:
                raise IcueUnavailable(f"connect returned {enum_name(err)}")

            if not self._state_evt.wait(CONNECT_TIMEOUT_SEC):
                raise IcueUnavailable(
                    f"no session state within {CONNECT_TIMEOUT_SEC:.0f}s "
                    "(is iCUE running?)"
                )
        # else: the native session is already registered and only reports on
        # CHANGE. Waiting for a fresh event on every retry would block the render
        # loop for CONNECT_TIMEOUT_SEC and — worse — would never notice a session
        # that came good while we were not looking, because that transition
        # already happened. The last state we were told IS the current state.

        if self._last_state != CorsairSessionState.CSS_Connected:
            name = enum_name(self._last_state)
            hint = ""
            if self._last_state == CorsairSessionState.CSS_ConnectionRefused:
                hint = " — enable iCUE Settings > Enable SDK / software integrations"
            elif self._last_state == CorsairSessionState.CSS_Timeout:
                # Measured 2026-08-26 on this machine: iCUE.exe was running and
                # still every handshake timed out, because nothing was listening
                # on the SDK endpoint (no Corsair SDK named pipe existed at all).
                # "iCUE is running" is therefore NOT evidence the SDK is up.
                hint = (
                    " — iCUE may be running without its SDK server; check "
                    "iCUE Settings > Enable SDK / software integrations"
                )
            raise IcueUnavailable(f"session state {name}{hint}")

        self._connected = True
        self._session_lost_reason = None
        self.refresh_devices()
        return True

    def refresh_devices(self):
        """Re-enumerate. Devices come and go (a keyboard plugged in mid-run)."""
        self._reap()
        if not self._sdk or not self._connected:
            self._devices = []
            self._held = set()
            self._sdk_device_count = 0
            return 0
        from cuesdk import CorsairDeviceFilter, CorsairDeviceType

        try:
            devs, err = self._sdk.get_devices(
                CorsairDeviceFilter(CorsairDeviceType.CDT_All)
            )
        except Exception as e:
            log.warning("get_devices failed: %s", e)
            self._devices = []
            self._sdk_device_count = 0
            return 0

        self._sdk_device_count = len(devs or [])
        found = []
        for d in devs or []:
            did = d.device_id
            try:
                leds, _ = self._sdk.get_led_positions(did)
            except Exception as e:
                log.warning("get_led_positions(%s) failed: %s", did, e)
                continue
            ids = [led.id for led in (leds or [])]
            # A device with zero addressable LEDs is kept out of the paint set —
            # it is not an error (an iCUE LINK System Hub reports 0),
            # but painting it is a no-op we should not pretend succeeded.
            if ids:
                found.append((did, ids))
        self._devices = found
        # A device that vanished between enumerations cannot be painted or
        # released, and leaving it in _held makes unheld_count under-report — the
        # mid-alert retry would then think it had everything.
        present = {did for did, _ in found}
        gone = self._held - present
        if gone:
            log.info("%d held device(s) no longer enumerated — forgetting them", len(gone))
            self._held &= present
        return len(found)

    def describe_devices(self):
        """Human inventory line for the log — includes zero-LED devices."""
        if not self._sdk or not self._connected:
            return "no session"
        from cuesdk import CorsairDeviceFilter, CorsairDeviceType

        try:
            devs, _ = self._sdk.get_devices(
                CorsairDeviceFilter(CorsairDeviceType.CDT_All)
            )
        except Exception as e:
            return f"enumeration failed: {e}"
        if not devs:
            return "0 devices"
        parts = []
        for d in devs:
            try:
                info, _ = self._sdk.get_device_info(d.device_id)
                model = getattr(info, "model", "?")
                n = getattr(info, "led_count", "?")
            except Exception:
                model, n = "?", "?"
            parts.append(f"{model} ({n} LEDs)")
        return "; ".join(parts)

    # ---------- lighting ----------

    def acquire(self):
        """Take exclusive lighting control of every paintable device.

        Idempotent and PER-DEVICE: devices already held are skipped, so calling it
        again during an alert retries only the ones that failed or arrived since.
        Returns True when at least one device is held — which is enough to light
        the room, and is why the caller must look at `unheld_count` rather than
        this return value to decide whether it is done.
        """
        self._reap()
        if not self._connected or not self._devices:
            return False
        from cuesdk import CorsairAccessLevel, CorsairError

        ok = False
        for did, _ in self._devices:
            if did in self._held:
                ok = True
                continue
            try:
                err = self._sdk.request_control(
                    did, CorsairAccessLevel.CAL_ExclusiveLightingControl
                )
            except Exception as e:
                log.warning("request_control(%s) raised: %s", did, e)
                continue
            if err == CorsairError.CE_Success:
                self._held.add(did)
                ok = True
            else:
                log.warning("request_control(%s) -> %s", did, enum_name(err))
        if ok:
            try:
                self._sdk.set_layer_priority(LAYER_PRIORITY)
            except Exception as e:
                log.warning("set_layer_priority raised: %s", e)
        return ok

    def paint(self, rgb):
        """Set every LED of every held device to one colour.

        Returns False when NOTHING was painted — the caller must act on that. A
        paint that fails on every device used to be discarded, so a revoked
        control grant produced an endless run of silent no-ops.
        """
        self._reap()
        if not self._connected or not self._held:
            return False
        from cuesdk import CorsairError, CorsairLedColor

        r, g, b = rgb
        painted = False
        lost = []
        for did, led_ids in self._devices:
            if did not in self._held:
                continue
            colors = [CorsairLedColor(id=i, r=r, g=g, b=b, a=255) for i in led_ids]
            try:
                err = self._sdk.set_led_colors(did, colors)
            except Exception as e:
                log.warning("set_led_colors(%s) raised: %s", did, e)
                continue
            if err == CorsairError.CE_Success:
                painted = True
                continue
            name = enum_name(err)
            if name in NO_CONTROL_ERRORS:
                # iCUE took the device back (a higher-priority client, a profile
                # switch, a re-plug). Holding the id would keep acquire() skipping
                # it forever, which is the CD-22 symptom one device at a time.
                lost.append(did)
                log.warning("set_led_colors(%s) -> %s; releasing our claim so it can be reacquired", did, name)
            else:
                log.debug("set_led_colors(%s) -> %s", did, name)
        for did in lost:
            self._held.discard(did)
        return painted

    def release(self):
        """Hand the lights back to iCUE. Idempotent; never raises.

        This is the whole teardown — see the module docstring on disconnect().
        """
        if not self._sdk:
            self._held.clear()
            return
        for did in list(self._held):
            try:
                self._sdk.release_control(did)
            except Exception as e:
                log.warning("release_control(%s) raised: %s", did, e)
            self._held.discard(did)


class NullAdapter:
    """Stand-in used by tests and by --dry-run: records paints, lights nothing."""

    def __init__(self, led_count=12):
        self.connected = True
        self.device_count = 1 if led_count else 0
        # Non-zero by default: it stands in for hardware, so --dry-run must
        # exercise the acquire/paint/release path rather than skip it.
        self.led_count = led_count
        self.sdk_device_count = self.device_count
        self.last_state_name = "CSS_Connected (simulated)"
        self.acquired = False
        self.released = 0
        self.paints = []
        self.session_lost_reason = None
        #: Tests set these to simulate control loss and partial acquisition
        #: without an SDK: paint_ok=False is "we hold nothing usable",
        #: unheld_count>0 is "one device is still not ours".
        self.paint_ok = True
        self.unheld_count = 0
        self.refreshes = 0
        self.acquires = 0

    @property
    def held_count(self):
        return self.device_count if self.acquired else 0

    def connect(self):
        return True

    def refresh_devices(self):
        self.refreshes += 1
        return self.device_count

    def describe_devices(self):
        return f"null adapter ({self.led_count} simulated LEDs)"

    def acquire(self):
        self.acquires += 1
        self.acquired = True
        self.unheld_count = 0
        return True

    def paint(self, rgb):
        self.paints.append(rgb)
        return self.paint_ok

    def release(self):
        self.acquired = False
        self.released += 1
