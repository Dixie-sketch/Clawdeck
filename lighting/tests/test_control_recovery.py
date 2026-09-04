"""Control loss, hot-plug and partial acquisition — CD-22 and CD-23.

Two seams, because the bug spans two files and each half is invisible from the
other:

* the ADAPTER seam (`icue.IcueAdapter` against a fake `cuesdk`) — does a dead
  session drop the devices it granted, and does a revoked control grant release
  our claim on that device?
* the LOOP seam (`sidecrab_glow.Glow` against `NullAdapter`) — does the render
  loop notice, stop believing it holds the lights, and try again?

The shipped bug lived exactly in the gap between them: the adapter cleared its
connection flag, the loop kept `acquired = True`, and every subsequent paint
failed into a return value nobody read. Both halves were individually
defensible, and the case stayed dark until the process was restarted.

Headless like the rest of the suite. No SDK, no iCUE, no hardware — this machine
exposes zero lightable LEDs, so none of this is observable on it any other way.
"""

import logging
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import icue  # noqa: E402
import sidecrab_glow  # noqa: E402
from decision import GlowDecision  # noqa: E402
from icue import IcueAdapter, NullAdapter, acquisition_verdict  # noqa: E402
from test_sdk_lifetime import (  # noqa: E402
    FakeEnumValue,
    FakeEvent,
    FakeSessionState,
)


_LOG = logging.getLogger("sidecrab")
_SAVED_PROPAGATE = _LOG.propagate


def setUpModule():
    """Most of these tests deliberately drive warning paths. Park the logger so
    the suite's output stays readable — `assertLogs` still works, it installs its
    own handler on this logger and restores both fields afterwards."""
    _LOG.addHandler(logging.NullHandler())
    _LOG.propagate = False


def tearDownModule():
    _LOG.propagate = _SAVED_PROPAGATE


# ------------------------------------------------------- a fake with a paint path

class FakeError:
    CE_Success = FakeEnumValue("CorsairError", "CE_Success", 0)
    CE_NotConnected = FakeEnumValue("CorsairError", "CE_NotConnected", 1)
    CE_NoControl = FakeEnumValue("CorsairError", "CE_NoControl", 2)
    CE_InvalidArguments = FakeEnumValue("CorsairError", "CE_InvalidArguments", 3)


class FakeLed:
    def __init__(self, led_id):
        self.id = led_id


class FakeDevice:
    def __init__(self, device_id):
        self.device_id = device_id


class FakeSdk:
    """Enough of CueSdk to drive acquire/paint/release, with per-device knobs."""

    def __init__(self):
        self.handler = None
        self.devices = ["dev-a"]
        self.leds = {"dev-a": [1, 2, 3]}
        #: device_id -> error returned by request_control (absent = success)
        self.control_errors = {}
        #: device_id -> error returned by set_led_colors (absent = success)
        self.paint_errors = {}
        self.control_calls = []
        self.released = []
        self.painted = []

    def connect(self, on_state_changed):
        self.handler = on_state_changed
        on_state_changed(FakeEvent(FakeSessionState.CSS_Connected))
        return FakeError.CE_Success

    def lose_session(self, state=None):
        self.handler(FakeEvent(state or FakeSessionState.CSS_ConnectionLost))

    def get_devices(self, _filter):
        return ([FakeDevice(d) for d in self.devices], FakeError.CE_Success)

    def get_led_positions(self, device_id):
        return ([FakeLed(i) for i in self.leds.get(device_id, [])], FakeError.CE_Success)

    def request_control(self, device_id, _level):
        self.control_calls.append(device_id)
        return self.control_errors.get(device_id, FakeError.CE_Success)

    def set_layer_priority(self, _priority):
        return FakeError.CE_Success

    def set_led_colors(self, device_id, colors):
        err = self.paint_errors.get(device_id, FakeError.CE_Success)
        if err == FakeError.CE_Success:
            self.painted.append((device_id, len(colors)))
        return err

    def release_control(self, device_id):
        self.released.append(device_id)
        return FakeError.CE_Success


class FakeLedColor:
    def __init__(self, id, r, g, b, a):  # noqa: A002 - mirrors the SDK's own name
        self.id, self.r, self.g, self.b, self.a = id, r, g, b, a


class AdapterTestCase(unittest.TestCase):
    """An IcueAdapter wired to one FakeSdk instance, connected and enumerated."""

    def setUp(self):
        sdk = self.sdk = FakeSdk()

        class Module:
            CueSdk = staticmethod(lambda: sdk)
            CorsairError = FakeError
            CorsairSessionState = FakeSessionState
            CorsairDeviceFilter = staticmethod(lambda t: t)
            CorsairDeviceType = type("CorsairDeviceType", (), {"CDT_All": 0xFFFF})
            CorsairAccessLevel = type(
                "CorsairAccessLevel", (), {"CAL_ExclusiveLightingControl": 3}
            )
            CorsairLedColor = FakeLedColor

        self._saved = sys.modules.get("cuesdk")
        sys.modules["cuesdk"] = Module
        self._saved_timeout = icue.CONNECT_TIMEOUT_SEC
        icue.CONNECT_TIMEOUT_SEC = 0.2

    def tearDown(self):
        icue.CONNECT_TIMEOUT_SEC = self._saved_timeout
        if self._saved is None:
            sys.modules.pop("cuesdk", None)
        else:
            sys.modules["cuesdk"] = self._saved

    def connected_adapter(self):
        a = IcueAdapter()
        a.connect()
        return a


# ------------------------------------------------------------------------ CD-22

class SessionLossDropsWhatItGranted(AdapterTestCase):
    """The shipped behaviour cleared `connected` and kept `_held` — so the next
    alert skipped acquire() and painted into a session that was gone."""

    def test_a_lost_session_drops_the_devices_and_the_claims(self):
        a = self.connected_adapter()
        self.assertTrue(a.acquire())
        self.assertEqual(a.held_count, 1)

        self.sdk.lose_session()
        a.refresh_devices()  # any main-thread entry point reaps

        self.assertFalse(a.connected)
        self.assertEqual(a.held_count, 0, "a dead session cannot still grant control")
        self.assertEqual(a.device_count, 0)
        self.assertIn("CSS_ConnectionLost", a.session_lost_reason)

    def test_css_closed_is_a_loss_too(self):
        a = self.connected_adapter()
        a.acquire()
        self.sdk.lose_session(FakeSessionState.CSS_Closed)
        a.refresh_devices()
        self.assertEqual(a.held_count, 0)
        self.assertIn("CSS_Closed", a.session_lost_reason)

    def test_the_reap_happens_on_paint_too(self):
        """paint() is the call the render loop makes 20x a second; it must not be
        the one path that keeps painting a corpse."""
        a = self.connected_adapter()
        a.acquire()
        self.sdk.lose_session()
        self.assertFalse(a.paint((10, 20, 30)))
        self.assertEqual(a.held_count, 0)

    def test_reacquisition_once_the_session_returns(self):
        a = self.connected_adapter()
        a.acquire()
        self.sdk.lose_session()
        a.refresh_devices()

        self.sdk.handler(FakeEvent(FakeSessionState.CSS_Connected))
        a.connect()

        self.assertTrue(a.connected)
        self.assertEqual(a.device_count, 1)
        self.assertIsNone(a.session_lost_reason, "a good handshake clears the reason")
        self.assertTrue(a.acquire())
        self.assertEqual(a.held_count, 1)
        self.assertTrue(a.paint((10, 20, 30)))


class RevokedControlReleasesTheClaim(AdapterTestCase):
    """iCUE can take one device back without the session dying at all."""

    def test_a_no_control_paint_error_gives_the_device_back(self):
        a = self.connected_adapter()
        a.acquire()
        self.sdk.paint_errors["dev-a"] = FakeError.CE_NoControl

        self.assertFalse(a.paint((10, 20, 30)))
        self.assertEqual(a.held_count, 0)
        self.assertEqual(a.unheld_count, 1, "it must show up as needing reacquisition")

    def test_not_connected_counts_as_control_loss(self):
        a = self.connected_adapter()
        a.acquire()
        self.sdk.paint_errors["dev-a"] = FakeError.CE_NotConnected
        a.paint((10, 20, 30))
        self.assertEqual(a.held_count, 0)

    def test_an_ordinary_paint_error_keeps_the_claim(self):
        """The discriminating case. Dropping the claim on EVERY error would churn
        acquire() once a second on a device that is simply rejecting a colour."""
        a = self.connected_adapter()
        a.acquire()
        self.sdk.paint_errors["dev-a"] = FakeError.CE_InvalidArguments
        self.assertFalse(a.paint((10, 20, 30)))
        self.assertEqual(a.held_count, 1)

    def test_the_device_is_retaken_by_the_next_acquire(self):
        a = self.connected_adapter()
        a.acquire()
        self.sdk.paint_errors["dev-a"] = FakeError.CE_NoControl
        a.paint((10, 20, 30))

        self.sdk.paint_errors.clear()
        self.assertTrue(a.acquire())
        self.assertTrue(a.paint((10, 20, 30)))


# ------------------------------------------------------------------------ CD-23

class PerDeviceAcquisition(AdapterTestCase):
    """"any one device succeeded" was treated as "acquisition complete"."""

    def two_devices(self):
        self.sdk.devices = ["dev-a", "dev-b"]
        self.sdk.leds = {"dev-a": [1, 2], "dev-b": [7]}
        return self.connected_adapter()

    def test_a_partial_acquisition_is_visible_as_partial(self):
        self.sdk.control_errors = {"dev-b": FakeError.CE_NotConnected}
        a = self.two_devices()
        self.sdk.control_errors = {"dev-b": FakeError.CE_NotConnected}

        self.assertTrue(a.acquire(), "one device is enough to light the room")
        self.assertEqual(a.held_count, 1)
        self.assertEqual(a.unheld_count, 1, "the failure must not read as complete")

    def test_a_retry_asks_only_the_device_that_failed(self):
        a = self.two_devices()
        self.sdk.control_errors = {"dev-b": FakeError.CE_NotConnected}
        a.acquire()
        self.sdk.control_calls.clear()

        self.sdk.control_errors.clear()
        a.acquire()

        self.assertEqual(self.sdk.control_calls, ["dev-b"], "dev-a was already held")
        self.assertEqual(a.held_count, 2)
        self.assertEqual(a.unheld_count, 0)

    def test_a_device_plugged_in_mid_run_is_enumerated_and_unheld(self):
        a = self.connected_adapter()
        a.acquire()
        self.assertEqual(a.unheld_count, 0)

        self.sdk.devices.append("dev-b")
        self.sdk.leds["dev-b"] = [9, 10]
        a.refresh_devices()

        self.assertEqual(a.device_count, 2)
        self.assertEqual(a.unheld_count, 1, "the new device is not held yet")
        self.assertTrue(a.acquire())
        self.assertEqual(a.held_count, 2)

    def test_a_device_that_vanished_is_forgotten(self):
        a = self.two_devices()
        a.acquire()
        self.assertEqual(a.held_count, 2)

        self.sdk.devices.remove("dev-b")
        a.refresh_devices()

        self.assertEqual(a.held_count, 1, "an unplugged device cannot still be held")
        self.assertEqual(a.unheld_count, 0, "and must not read as owed a retry")


# ---------------------------------------------------------------- the loop seam

def alert(level="normal"):
    return GlowDecision(True, level, "waiting", alert_count=1, oldest_age_sec=30.0)


DARK = GlowDecision(False, "none", "feed-no-sessions")


class LoopNoticesControlLoss(unittest.TestCase):
    """`Glow.acquired` is the flag that decides whether acquire() is ever called
    again. Leaving it true across a control loss is the whole CD-22 symptom."""

    def _glow(self, led_count=12):
        adapter = NullAdapter(led_count=led_count)
        return adapter, sidecrab_glow.Glow(adapter, None)

    def _run(self, glow, frames, start=100.0, decision=None):
        t = start
        for _ in range(frames):
            glow.tick(0.05, decision or alert(), t)
            t += 0.05
        return t

    def test_a_run_of_failed_paints_drops_control(self):
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        self.assertTrue(glow.acquired)

        adapter.paint_ok = False
        self._run(glow, sidecrab_glow.PAINT_FAIL_LIMIT, start=100.05)
        self.assertFalse(glow.acquired, "paint failures were being discarded")

    def test_one_bad_frame_does_not_drop_control(self):
        """The discriminating case: a single lost frame is a blip, not a loss."""
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        adapter.paint_ok = False
        glow.tick(0.05, alert(), 100.05)
        adapter.paint_ok = True
        glow.tick(0.05, alert(), 100.10)
        self.assertTrue(glow.acquired)

    def test_control_is_retaken_after_the_retry_delay(self):
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        adapter.paint_ok = False
        t = self._run(glow, sidecrab_glow.PAINT_FAIL_LIMIT, start=100.05)
        self.assertFalse(glow.acquired)

        adapter.paint_ok = True
        acquires = adapter.acquires
        glow.tick(0.05, alert(), t + sidecrab_glow.ACQUIRE_RETRY_SEC + 0.1)

        self.assertTrue(glow.acquired, "the alert is still up; it must light again")
        self.assertEqual(adapter.acquires, acquires + 1)

    def test_the_reacquire_attempt_is_throttled_not_per_frame(self):
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        adapter.paint_ok = False
        t = self._run(glow, sidecrab_glow.PAINT_FAIL_LIMIT, start=100.05)
        acquires = adapter.acquires

        for i in range(20):  # 1 s of frames, inside the retry delay
            glow.tick(0.05, alert(), t + i * 0.05)

        self.assertEqual(adapter.acquires, acquires, "acquire() was retried per frame")

    def test_a_disconnected_adapter_forgets_control(self):
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        adapter.connected = False
        adapter.session_lost_reason = "session state CSS_ConnectionLost"
        glow.tick(0.05, alert(), 100.05)
        self.assertFalse(glow.acquired)

    def test_losing_every_paintable_device_forgets_control(self):
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        adapter.led_count = 0
        glow.tick(0.05, alert(), 100.05)
        self.assertFalse(glow.acquired)

    def test_dropping_control_does_not_call_release(self):
        """release_control against a session that is gone can only fail, and the
        adapter has already dropped the ids."""
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        adapter.connected = False
        glow.tick(0.05, alert(), 100.05)
        self.assertEqual(adapter.released, 0)

    def test_an_ordinary_dark_decision_still_releases(self):
        """The guard above must not have cost the normal teardown."""
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        glow.tick(0.05, DARK, 100.05)
        self.assertEqual(adapter.released, 1)


class LoopRescansDuringAnAlert(unittest.TestCase):
    """Devices used to be re-enumerated only when the LED total was zero, so a
    second device plugged in next to a working one was never seen."""

    def _glow(self):
        adapter = NullAdapter(led_count=12)
        return adapter, sidecrab_glow.Glow(adapter, None)

    def test_the_rescan_is_bounded_not_per_frame(self):
        adapter, glow = self._glow()
        for i in range(20):  # 1 s of frames
            glow.tick(0.05, alert(), 100.0 + i * 0.05)
        self.assertEqual(adapter.refreshes, 0, "get_devices per frame is 1200/min")

    def test_devices_are_rescanned_once_the_timer_expires(self):
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        glow.tick(0.05, alert(), 100.0 + sidecrab_glow.RESCAN_SEC + 0.1)
        self.assertEqual(adapter.refreshes, 1)

    def test_a_device_that_arrives_mid_alert_is_acquired(self):
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        acquires = adapter.acquires

        adapter.unheld_count = 1  # a keyboard appeared and is not ours yet
        glow.tick(0.05, alert(), 100.0 + sidecrab_glow.RESCAN_SEC + 0.1)

        self.assertEqual(adapter.acquires, acquires + 1)
        self.assertEqual(adapter.unheld_count, 0)

    def test_nothing_unheld_means_no_extra_acquire(self):
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        acquires = adapter.acquires
        glow.tick(0.05, alert(), 100.0 + sidecrab_glow.RESCAN_SEC + 0.1)
        self.assertEqual(adapter.acquires, acquires)

    def test_the_rescan_timer_restarts_with_each_acquisition(self):
        """Otherwise a long dark spell leaves the timer expired and the first
        frame of the next alert pays an SDK round-trip."""
        adapter, glow = self._glow()
        glow.tick(0.05, alert(), 100.0)
        glow.tick(0.05, DARK, 200.0)
        glow.tick(0.05, alert(), 300.0)
        self.assertEqual(adapter.refreshes, 0)


# -------------------------------------------------------------- the state line

class AcquisitionVerdict(unittest.TestCase):
    def test_the_five_states_are_distinct(self):
        vs = {
            acquisition_verdict(False, 0, 0, "session state CSS_ConnectionLost"),
            acquisition_verdict(True, 0, 0),
            acquisition_verdict(True, 2, 0),
            acquisition_verdict(True, 2, 1),
            acquisition_verdict(True, 2, 2),
        }
        self.assertEqual(len(vs), 5)

    def test_a_lost_session_names_the_reason(self):
        v = acquisition_verdict(False, 0, 0, "session state CSS_ConnectionLost")
        self.assertIn("CSS_ConnectionLost", v)

    def test_partial_control_says_how_partial(self):
        v = acquisition_verdict(True, 3, 1)
        self.assertIn("partial", v)
        self.assertIn("1 of 3", v)

    def test_connected_and_holding_nothing_is_not_reported_as_healthy(self):
        """The exact shape CD-22 produced: reachable, paintable, holding nothing."""
        v = acquisition_verdict(True, 2, 0)
        self.assertIn("NOT held", v)

    def test_every_verdict_survives_cp1252(self):
        for v in (
            acquisition_verdict(False, 0, 0, "x"),
            acquisition_verdict(True, 0, 0),
            acquisition_verdict(True, 2, 0),
            acquisition_verdict(True, 2, 1),
            acquisition_verdict(True, 2, 2),
        ):
            v.encode("cp1252")  # raises UnicodeEncodeError on a legacy console

    def test_the_selftest_reports_control_separately_from_the_verdict(self):
        facts = sidecrab_glow.selftest_facts(NullAdapter(led_count=12))
        text = sidecrab_glow.format_selftest(facts)
        self.assertIn("CONTROL", text)
        self.assertIn("devices held", text)
        self.assertNotEqual(facts["control"], facts["verdict"])

    def test_the_log_says_which_state_it_is_in_when_control_is_lost(self):
        """Without this line the operator sees a healthy process and a dark case,
        with nothing in the log between the two."""
        adapter = NullAdapter(led_count=12)
        glow = sidecrab_glow.Glow(adapter, None)
        glow.tick(0.05, alert(), 100.0)
        adapter.connected = False
        adapter.session_lost_reason = "session state CSS_ConnectionLost"
        with self.assertLogs("sidecrab", "WARNING") as captured:
            glow.tick(0.05, alert(), 100.05)
        line = "\n".join(captured.output)
        self.assertIn("control dropped", line)
        self.assertIn("CSS_ConnectionLost", line)
        self.assertIn("reacquire", line)

    def test_the_log_line_and_the_selftest_agree(self):
        adapter = NullAdapter(led_count=12)
        glow = sidecrab_glow.Glow(adapter, None)
        self.assertEqual(
            glow.control_line(), sidecrab_glow.selftest_facts(adapter)["control"]
        )


class FeedEndpointTests(unittest.TestCase):
    """The glow's one link to crabd. Lives here because this is the lighting module that
    already loads sidecrab_glow, and an unpinned port is exactly how this parked component
    would go dark without anyone noticing."""

    def test_the_default_url_is_crabds_state_feed(self):
        # A GET: crabd's X-SideCrab-Panel gate guards POSTs only, and the glow never writes.
        self.assertEqual(sidecrab_glow.DEFAULT_URL, "http://127.0.0.1:9999/v1/state")


if __name__ == "__main__":
    unittest.main()
