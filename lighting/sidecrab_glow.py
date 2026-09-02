"""sidecrab-glow — pulse your Corsair lighting terracotta when a Claude session
is waiting on you.

A standalone, read-only consumer of the crabd feed: it polls GET /v1/state and
paints. It never writes to crabd, never touches iCUE settings, and hands the
lights straight back to your own iCUE profile the moment the alert clears or the
process exits.

    python sidecrab_glow.py                 # normal run
    python sidecrab_glow.py --selftest      # why is my case dark? read this first
    python sidecrab_glow.py --dry-run       # no SDK; logs the decision each poll
    python sidecrab_glow.py --pulse-test 5  # 5 s visible pulse, then release

Degrades loudly but never dies: no iCUE, no devices or no crabd all reduce to
"log it, keep polling, retry the handshake every 60 s".
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import math
import os
import signal
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from decision import (  # noqa: E402
    LEVEL_ESCALATED,
    GlowDecision,
    decide,
)
from icue import (  # noqa: E402
    IcueAdapter,
    IcueUnavailable,
    NullAdapter,
    acquisition_verdict,
    lighting_verdict,
)

DEFAULT_URL = os.environ.get("SIDECRAB_URL", "http://127.0.0.1:2722/v1/state")

POLL_INTERVAL_SEC = 3.0
FETCH_TIMEOUT_SEC = 2.0
RENDER_TICK_SEC = 0.05  # 20 fps — smooth breathing without burning a core
SDK_RETRY_SEC = 60.0

# Re-enumerate and re-acquire DURING an alert. Bounded on purpose: get_devices is
# an SDK round-trip and this loop runs at 20 fps, so doing it per frame would put
# ~1200 enumerations a minute through the SDK to catch a keyboard being plugged in.
RESCAN_SEC = 30.0

# How long to wait before trying acquire() again after it failed. Without it the
# retry ran at 20 fps and wrote a warning per frame.
ACQUIRE_RETRY_SEC = 2.0

# Consecutive whole-frame paint failures before we conclude the control grant is
# gone rather than that one frame lost a race. 5 frames = 0.25 s: fast enough that
# nobody sees the gap, slow enough that a single blip does not churn the SDK.
PAINT_FAIL_LIMIT = 5

# Claude's terracotta.
TERRACOTTA = (0xD9, 0x77, 0x57)

# (period_sec, min_brightness, max_brightness). Escalated is brighter and a touch
# faster — noticeable from the corner of an eye without becoming a strobe.
PULSE_NORMAL = (3.6, 0.22, 0.80)
PULSE_ESCALATED = (2.4, 0.45, 1.00)

log = logging.getLogger("sidecrab.glow")


def setup_logging(level=logging.INFO, logfile=None):
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger("sidecrab")
    root.setLevel(level)
    root.handlers.clear()

    # Under pythonw there is no stdout AND no stderr, so StreamHandler(None) ends
    # up with self.stream = None and every single record raises inside emit() and
    # is swallowed. Skip the handler entirely there; the file log is the record.
    if sys.stdout is not None:
        # A legacy Windows console is cp1252, and one non-cp1252 character in a
        # message (an SDK hint, a device model name) turns the diagnostic into a
        # UnicodeEncodeError traceback — worst possible failure for the tool an
        # operator reaches for when confused. Degrade the glyph, not the message.
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        root.addHandler(stream)

    if logfile is None:
        logfile = Path.home() / ".sidecrab" / "glow.log"
    try:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            logfile, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as e:
        # A log we cannot write is not a reason to refuse to light the room.
        root.warning("file logging disabled (%s)", e)


def fetch_state(url, timeout=FETCH_TIMEOUT_SEC):
    """GET the feed. Returns the parsed doc, or None for any failure at all."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def brightness_at(phase, level):
    """Cosine breathing curve in 0..1 for a continuous phase (in cycles)."""
    period, lo, hi = PULSE_ESCALATED if level == LEVEL_ESCALATED else PULSE_NORMAL
    wave = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase)
    return lo + (hi - lo) * wave


def period_for(level):
    return (PULSE_ESCALATED if level == LEVEL_ESCALATED else PULSE_NORMAL)[0]


def scale(rgb, brightness):
    b = max(0.0, min(1.0, brightness))
    return tuple(int(round(c * b)) for c in rgb)


class Poller(threading.Thread):
    """Polls crabd off the render thread so HTTP latency never stutters the pulse."""

    daemon = True

    def __init__(self, url, stop_evt, interval=POLL_INTERVAL_SEC):
        super().__init__(name="sidecrab-glow-poll")
        self.url = url
        self.stop_evt = stop_evt
        self.interval = interval
        self._lock = threading.Lock()
        self._decision = GlowDecision(False, "none", "startup")

    @property
    def decision(self):
        with self._lock:
            return self._decision

    def poll_once(self):
        doc = fetch_state(self.url)
        d = decide(doc, datetime.now(timezone.utc))
        with self._lock:
            prev, self._decision = self._decision, d
        if (prev.should_glow, prev.level, prev.reason) != (
            d.should_glow,
            d.level,
            d.reason,
        ):
            log.info(
                "state: %s (%s)%s",
                "GLOW " + d.level if d.should_glow else "dark",
                d.reason,
                f" — {d.alert_count} waiting, oldest {d.oldest_age_sec:.0f}s"
                if d.should_glow
                else "",
            )
        return d

    def run(self):
        while not self.stop_evt.is_set():
            try:
                self.poll_once()
            except Exception:  # a poll bug must not kill the process
                log.exception("poll failed")
            self.stop_evt.wait(self.interval)


class Glow:
    def __init__(self, adapter, poller):
        self.adapter = adapter
        self.poller = poller
        self.phase = 0.0
        self.acquired = False
        self._next_sdk_attempt = 0.0
        self._next_rescan = 0.0
        self._next_acquire_attempt = 0.0
        self._paint_failures = 0
        self._sdk_warned = False
        self._acquire_warned = False
        self._no_led_warned = False
        # Why the last handshake failed, kept for the verdict line. Without it the
        # startup log says "no lighting" and leaves the operator guessing.
        self.last_sdk_error = None

    def _forget_control(self, reason):
        """Drop our belief that we hold the lights, WITHOUT calling release().

        release_control against a session that is gone can only fail, and the
        adapter has already dropped the ids. What matters is that `acquired` goes
        false, because `tick` reads it as "no need to acquire" — leaving it true
        across a control loss is precisely how the case stayed dark while the
        process kept reporting a healthy handshake (CD-22).
        """
        if not self.acquired:
            return
        self.acquired = False
        self.phase = 0.0
        self._paint_failures = 0
        log.warning("lighting control dropped (%s) — will reacquire at the next alert", reason)

    def ensure_sdk(self, now):
        """Handshake, or retry it every 60 s. Returns True when paintable."""
        # Read ONCE: the SDK's callback thread can flip this between two reads, and
        # a tick that saw both answers would forget control and then act as though
        # it still held it.
        connected = bool(getattr(self.adapter, "connected", False))
        if not connected:
            self._forget_control(
                getattr(self.adapter, "session_lost_reason", None) or "session not connected"
            )
        else:
            if self.adapter.led_count == 0:
                # Every paintable device went away mid-alert. Same reasoning as a
                # session loss: hold `acquired` true and the device coming back is
                # never acquired.
                self._forget_control("no paintable devices")
            if self.adapter.led_count == 0 and now >= self._next_sdk_attempt:
                # Devices can appear later (a keyboard plugged in mid-run).
                self._next_sdk_attempt = now + SDK_RETRY_SEC
                self.adapter.refresh_devices()
                if self.adapter.led_count and self._no_led_warned:
                    log.info(
                        "iCUE devices now addressable: %s",
                        self.adapter.describe_devices(),
                    )
                    self._no_led_warned = False
            return self.adapter.led_count > 0

        if now < self._next_sdk_attempt:
            return False
        self._next_sdk_attempt = now + SDK_RETRY_SEC
        try:
            self.adapter.connect()
        except IcueUnavailable as e:
            self.last_sdk_error = str(e)
            if not self._sdk_warned:
                log.warning("iCUE SDK unavailable: %s", e)
                log.warning("retrying the handshake every %.0f s", SDK_RETRY_SEC)
                self._sdk_warned = True
            else:
                log.debug("iCUE SDK still unavailable: %s", e)
            return False
        except Exception as e:
            self.last_sdk_error = f"unexpected error ({e})"
            if not self._sdk_warned:
                log.warning("iCUE SDK unavailable: unexpected error (%s)", e)
                self._sdk_warned = True
            return False

        self._sdk_warned = False
        self.last_sdk_error = None
        log.info("iCUE SDK connected — devices: %s", self.adapter.describe_devices())
        if self.adapter.led_count == 0:
            if not self._no_led_warned:
                log.warning(
                    "iCUE SDK connected but no addressable LEDs are exposed — "
                    "nothing to pulse; re-checking every %.0f s",
                    SDK_RETRY_SEC,
                )
                self._no_led_warned = True
            return False
        log.info(
            "paintable: %d device(s), %d LEDs",
            self.adapter.device_count,
            self.adapter.led_count,
        )
        return True

    def release(self):
        if self.acquired:
            self.adapter.release()
            self.acquired = False
            self.phase = 0.0
            log.info("lighting released back to iCUE")

    def tick(self, dt, decision, now):
        if not decision.should_glow:
            self.release()
            return

        if not self.ensure_sdk(now):
            return

        if not self.acquired:
            if now < self._next_acquire_attempt:
                return
            self._next_acquire_attempt = now + ACQUIRE_RETRY_SEC
            if not self.adapter.acquire():
                if not self._acquire_warned:
                    log.warning(
                        "could not take lighting control; retrying every %.0f s",
                        ACQUIRE_RETRY_SEC,
                    )
                    self._acquire_warned = True
                return
            self._acquire_warned = False
            self.acquired = True
            self._paint_failures = 0
            self._next_rescan = now + RESCAN_SEC
            log.info(
                "lighting control acquired (%s pulse) — %s",
                decision.level,
                self.control_line(),
            )
        elif now >= self._next_rescan:
            # CD-23: devices arrive and leave mid-alert, and request_control can
            # fail for ONE device while another succeeds. Re-enumerating only when
            # the LED total hit zero meant a second device plugged in next to a
            # working one was never picked up, and a device that refused control
            # was never asked again. acquire() is per-device and skips what we
            # already hold, so this is a retry, not a re-take.
            self._next_rescan = now + RESCAN_SEC
            before = self.adapter.led_count
            self.adapter.refresh_devices()
            if self.adapter.led_count != before:
                log.info(
                    "iCUE inventory changed mid-alert: %d LED(s) paintable (was %d)",
                    self.adapter.led_count, before,
                )
            if getattr(self.adapter, "unheld_count", 0):
                self.adapter.acquire()
                log.info("mid-alert reacquire — %s", self.control_line())

        # Continuous phase: escalating changes the period without a visible jump.
        self.phase = (self.phase + dt / period_for(decision.level)) % 1.0
        if self.adapter.paint(scale(TERRACOTTA, brightness_at(self.phase, decision.level))):
            self._paint_failures = 0
            return
        # Nothing took the colour. Ignoring this return value is what let a revoked
        # control grant run silently until the process was restarted.
        self._paint_failures += 1
        if self._paint_failures >= PAINT_FAIL_LIMIT:
            self._forget_control(f"{PAINT_FAIL_LIMIT} consecutive paint failures")
            self._next_acquire_attempt = now + ACQUIRE_RETRY_SEC

    def control_line(self):
        """The acquisition state in one line, for the log — same wording the
        selftest prints, so the log and `--selftest` cannot disagree."""
        return acquisition_verdict(
            bool(getattr(self.adapter, "connected", False)),
            getattr(self.adapter, "device_count", 0) or 0,
            getattr(self.adapter, "held_count", 0) or 0,
            getattr(self.adapter, "session_lost_reason", None),
        )

    def run(self, stop_evt):
        last = time.monotonic()
        while not stop_evt.is_set():
            now = time.monotonic()
            dt, last = now - last, now
            try:
                self.tick(dt, self.poller.decision, now)
            except Exception:
                log.exception("render tick failed")
                try:
                    self.release()
                except Exception:
                    pass
            stop_evt.wait(RENDER_TICK_SEC)


def selftest_facts(adapter, sdk_error=None):
    """Everything the selftest reports, as a dict. No printing, no SDK calls of
    its own — the handshake has already happened by the time this is called."""
    connected = bool(getattr(adapter, "connected", False))
    led_count = getattr(adapter, "led_count", 0) or 0
    return {
        "sdk_reachable": connected,
        "session_state": getattr(adapter, "last_state_name", "unknown"),
        "sdk_devices": getattr(adapter, "sdk_device_count", 0) or 0,
        "paintable_devices": getattr(adapter, "device_count", 0) or 0,
        "lightable_leds": led_count,
        "inventory": adapter.describe_devices() if connected else "no session",
        "devices_held": getattr(adapter, "held_count", 0) or 0,
        # A second verdict because "could this glow" and "is it holding the lights
        # right now" are different questions with different answers after a control
        # loss — reporting only the first is what made a dark case look healthy.
        "control": acquisition_verdict(
            connected,
            getattr(adapter, "device_count", 0) or 0,
            getattr(adapter, "held_count", 0) or 0,
            getattr(adapter, "session_lost_reason", None) or sdk_error,
        ),
        "verdict": lighting_verdict(connected, led_count, sdk_error),
    }


def format_selftest(facts):
    return "\n".join(
        [
            "sidecrab-glow selftest",
            f"  SDK reachable         : {'yes' if facts['sdk_reachable'] else 'no'}",
            f"  session state         : {facts['session_state']}",
            f"  devices seen by SDK   : {facts['sdk_devices']}",
            f"  devices with LEDs     : {facts['paintable_devices']}",
            f"  TOTAL lightable LEDs  : {facts['lightable_leds']}",
            f"  device inventory      : {facts['inventory']}",
            f"  devices held          : {facts['devices_held']}",
            "",
            f"  CONTROL: {facts['control']}",
            f"  VERDICT: {facts['verdict']}",
        ]
    )


def selftest(adapter):
    """Answer "why is my case dark?" without a debugger.

    Exit codes are the answer in machine-readable form, for Test-SideCrab.ps1 and
    anything else that wants to assert on it:
      0 = would glow · 1 = connected but no lightable LEDs · 2 = SDK unavailable
    """
    glow = Glow(adapter, None)
    glow.ensure_sdk(time.monotonic())
    facts = selftest_facts(adapter, glow.last_sdk_error)
    print(format_selftest(facts))
    log.info("selftest verdict: %s", facts["verdict"])
    if not facts["sdk_reachable"]:
        return 2
    return 0 if facts["lightable_leds"] else 1


def pulse_test(adapter, seconds, level="normal"):
    """Visible smoke test: pulse for N seconds, then release. No feed involved."""
    glow = Glow(adapter, None)
    if not glow.ensure_sdk(time.monotonic()):
        log.error("pulse test: nothing paintable — aborting")
        return 1
    if not adapter.acquire():
        log.error("pulse test: could not take lighting control")
        return 1
    glow.acquired = True
    log.info("pulse test: %.1f s of %s terracotta on %d LEDs",
             seconds, level, adapter.led_count)
    end = time.monotonic() + seconds
    last = time.monotonic()
    try:
        while time.monotonic() < end:
            now = time.monotonic()
            dt, last = now - last, now
            glow.phase = (glow.phase + dt / period_for(level)) % 1.0
            adapter.paint(scale(TERRACOTTA, brightness_at(glow.phase, level)))
            time.sleep(RENDER_TICK_SEC)
    finally:
        glow.release()
    log.info("pulse test complete")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Pulse Corsair RGB when Claude needs input.")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"crabd state feed (default {DEFAULT_URL})")
    ap.add_argument("--dry-run", action="store_true", help="no SDK; just log decisions")
    ap.add_argument("--pulse-test", type=float, metavar="SECONDS",
                    help="pulse for N seconds then release, ignoring the feed")
    ap.add_argument("--selftest", action="store_true",
                    help="report SDK/session/device/LED state and a verdict, then exit")
    ap.add_argument("--once", action="store_true", help="poll once, log the decision, exit")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)

    adapter = NullAdapter() if args.dry_run else IcueAdapter()

    if args.selftest:
        try:
            return selftest(adapter)
        finally:
            adapter.release()

    if args.pulse_test:
        try:
            return pulse_test(adapter, args.pulse_test)
        finally:
            adapter.release()

    stop_evt = threading.Event()
    poller = Poller(args.url, stop_evt)

    if args.once:
        d = poller.poll_once()
        log.info("decision: %s", d)
        return 0

    def _stop(signum, _frame):
        log.info("signal %s — shutting down", signum)
        stop_evt.set()

    for sig in ("SIGINT", "SIGTERM", "SIGBREAK"):
        s = getattr(signal, sig, None)
        if s is not None:
            try:
                signal.signal(s, _stop)
            except (ValueError, OSError):
                pass

    log.info("sidecrab-glow starting — feed %s, poll %.0fs", args.url, POLL_INTERVAL_SEC)
    poller.start()
    glow = Glow(adapter, poller)
    # Probe the SDK once up front so its status is on the record immediately,
    # rather than only being discovered at the first alert hours later.
    paintable = glow.ensure_sdk(time.monotonic())
    # The same line `--selftest` prints, on the record at startup: the log has to
    # answer "why is my case dark?" on its own, months later, with no debugger.
    log.info("verdict: %s", lighting_verdict(
        bool(getattr(adapter, "connected", False)),
        getattr(adapter, "led_count", 0) or 0,
        glow.last_sdk_error,
    ))
    if not paintable:
        log.info("polling anyway — the handshake retries every %.0f s", SDK_RETRY_SEC)
    try:
        glow.run(stop_evt)
    except KeyboardInterrupt:
        stop_evt.set()
    finally:
        # The lights must go back to the user's iCUE profile no matter how we leave.
        try:
            glow.release()
        finally:
            adapter.release()
    log.info("sidecrab-glow stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
