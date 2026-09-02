"""The SDK-session lifetime invariant, the enum-name trap, and the dark-verdict.

Headless like the rest of the suite: `cuesdk` is replaced in sys.modules with a
fake, because the real one cannot be driven into its failure states on demand and
its failure mode is an interpreter crash rather than an exception.

The load-bearing test here is `SdkHandshakeLifetime`. It guards the bug that kept
glow parked under a Scheduled Task for a day: `IcueAdapter.connect()` used to
build the CueSdk as a LOCAL and only publish it to `self` on the SUCCESS path, so
a failed handshake dropped the object — and with it the ctypes thunk the native
SDK keeps calling ~2x/second — producing a use-after-free that killed the
interpreter with no traceback (0xC000001D / 0xC0000005 / 0xC0000096, ~1 s later).
Nothing in Python catches that, so the only place it can be caught is here.
"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import icue  # noqa: E402
import sidecrab_glow  # noqa: E402
from icue import IcueAdapter, IcueUnavailable, NullAdapter, enum_name, lighting_verdict  # noqa: E402


# ---------------------------------------------------------------- fake cuesdk

class FakeEnumValue:
    """Mimics cuesdk's hand-rolled Enumeration: NO `.name`, long `__str__`."""

    def __init__(self, cls_name, member, value):
        self._cls_name = cls_name
        self._member = member
        self.value = value

    def __str__(self):
        return f"{self._cls_name}.{self._member}"

    def __eq__(self, other):
        return isinstance(other, FakeEnumValue) and self.value == other.value

    def __hash__(self):
        return self.value


def _state(member, value):
    return FakeEnumValue("CorsairSessionState", member, value)


class FakeSessionState:
    CSS_Closed = _state("CSS_Closed", 1)
    CSS_Connecting = _state("CSS_Connecting", 2)
    CSS_Timeout = _state("CSS_Timeout", 3)
    CSS_ConnectionRefused = _state("CSS_ConnectionRefused", 4)
    CSS_ConnectionLost = _state("CSS_ConnectionLost", 5)
    CSS_Connected = _state("CSS_Connected", 6)


class FakeError:
    CE_Success = FakeEnumValue("CorsairError", "CE_Success", 0)
    CE_NotConnected = FakeEnumValue("CorsairError", "CE_NotConnected", 1)


class FakeEvent:
    def __init__(self, state):
        self.state = state


class FakeCueSdk:
    """Counts constructions, and fires whatever state the test asks for."""

    constructed = 0
    connect_calls = 0
    live = []  # every instance ever built, so a dropped one is still visible

    def __init__(self):
        type(self).constructed += 1
        type(self).live.append(self)
        self.handler = None

    def connect(self, on_state_changed):
        type(self).connect_calls += 1
        # The native side stores the thunk and starts calling it. The fake keeps
        # the reference on itself, exactly as the real CueSdk does.
        self.handler = on_state_changed
        for state in type(self).script:
            on_state_changed(FakeEvent(state))
        return type(self).connect_ret

    def get_devices(self, _filter):
        return (type(self).devices, FakeError.CE_Success)


class FakeCuesdkModule:
    CueSdk = FakeCueSdk
    CorsairError = FakeError
    CorsairSessionState = FakeSessionState
    CorsairDeviceFilter = staticmethod(lambda t: t)
    CorsairDeviceType = type("CorsairDeviceType", (), {"CDT_All": 0xFFFF})


class FakeSdkTestCase(unittest.TestCase):
    def setUp(self):
        FakeCueSdk.constructed = 0
        FakeCueSdk.connect_calls = 0
        FakeCueSdk.live = []
        FakeCueSdk.script = [FakeSessionState.CSS_Timeout]
        FakeCueSdk.connect_ret = FakeError.CE_Success
        FakeCueSdk.devices = []  # the LINK-hub shape: a session, nothing to paint
        self._saved = sys.modules.get("cuesdk")
        sys.modules["cuesdk"] = FakeCuesdkModule
        self._saved_timeout = icue.CONNECT_TIMEOUT_SEC
        icue.CONNECT_TIMEOUT_SEC = 0.2  # the fake answers synchronously

    def tearDown(self):
        icue.CONNECT_TIMEOUT_SEC = self._saved_timeout
        if self._saved is None:
            sys.modules.pop("cuesdk", None)
        else:
            sys.modules["cuesdk"] = self._saved


class SdkHandshakeLifetime(FakeSdkTestCase):
    def test_failed_handshake_still_retains_the_sdk_object(self):
        """THE regression test. If this fails, the process crashes in the field."""
        a = IcueAdapter()
        with self.assertRaises(IcueUnavailable):
            a.connect()
        self.assertIsNotNone(
            a._sdk,
            "the CueSdk was dropped on the failure path — the native SDK still "
            "calls its thunk, so this is a use-after-free, not a leak",
        )
        self.assertIsNotNone(a._cb, "the callback ref was dropped too")
        self.assertFalse(a.connected)

    def test_retry_never_constructs_a_second_sdk(self):
        a = IcueAdapter()
        for _ in range(4):
            with self.assertRaises(IcueUnavailable):
                a.connect()
        self.assertEqual(FakeCueSdk.constructed, 1)
        self.assertEqual(
            FakeCueSdk.connect_calls,
            1,
            "CorsairConnect must be called once per process: a second thunk "
            "could never be unregistered (disconnect() crashes)",
        )

    def test_retry_picks_up_a_session_that_comes_good_later(self):
        a = IcueAdapter()
        with self.assertRaises(IcueUnavailable):
            a.connect()
        # iCUE's SDK server appears; the already-registered native callback fires.
        a._sdk.handler(FakeEvent(FakeSessionState.CSS_Connected))
        a.connect()
        self.assertTrue(a.connected)
        self.assertEqual(FakeCueSdk.constructed, 1)

    def test_connect_error_return_also_retains_the_sdk(self):
        FakeCueSdk.connect_ret = FakeError.CE_NotConnected
        a = IcueAdapter()
        with self.assertRaises(IcueUnavailable) as ctx:
            a.connect()
        self.assertIsNotNone(a._sdk)
        self.assertIn("CE_NotConnected", str(ctx.exception))

    def test_timeout_message_names_the_sdk_toggle(self):
        a = IcueAdapter()
        with self.assertRaises(IcueUnavailable) as ctx:
            a.connect()
        msg = str(ctx.exception)
        self.assertIn("CSS_Timeout", msg)
        # "iCUE is running" is not evidence the SDK server is: say so.
        self.assertIn("Enable SDK", msg)


class LegacyConsoleEncodable(FakeSdkTestCase):
    """A legacy Windows console is cp1252. One character outside it turns the
    diagnostic an operator runs when confused into a UnicodeEncodeError
    traceback — which is exactly what a U+25B8 arrow in the SDK hint did on
    2026-08-26, killing `--selftest` outright."""

    def _assert_cp1252(self, text):
        try:
            text.encode("cp1252")
        except UnicodeEncodeError as e:
            self.fail(f"not cp1252-encodable ({e.reason}: {text[e.start:e.end]!r}): {text}")

    def test_session_state_hints_survive_cp1252(self):
        for state in (FakeSessionState.CSS_Timeout,
                      FakeSessionState.CSS_ConnectionRefused,
                      FakeSessionState.CSS_Closed):
            FakeCueSdk.constructed = 0
            FakeCueSdk.script = [state]
            a = IcueAdapter()
            with self.assertRaises(IcueUnavailable) as ctx:
                a.connect()
            self._assert_cp1252(str(ctx.exception))

    def test_every_verdict_survives_cp1252(self):
        for v in (lighting_verdict(False, 0, "session state CSS_Timeout"),
                  lighting_verdict(True, 0),
                  lighting_verdict(True, 12)):
            self._assert_cp1252(v)

    def test_the_whole_selftest_report_survives_cp1252(self):
        self._assert_cp1252(
            sidecrab_glow.format_selftest(
                sidecrab_glow.selftest_facts(NullAdapter(led_count=0))
            )
        )


class EnumNameTrap(unittest.TestCase):
    """cuesdk's Enumeration has no `.name`; `err.name` was a latent AttributeError."""

    def test_short_name_from_a_nameless_enumeration(self):
        self.assertEqual(enum_name(FakeError.CE_NotConnected), "CE_NotConnected")

    def test_a_real_enum_still_works(self):
        import enum

        class Real(enum.Enum):
            CSS_Connected = 6

        self.assertEqual(enum_name(Real.CSS_Connected), "CSS_Connected")

    def test_none_is_not_a_crash(self):
        self.assertEqual(enum_name(None), "none")

    def test_no_dotted_class_path_leaks_into_the_name(self):
        self.assertNotIn(".", enum_name(FakeSessionState.CSS_Timeout))


class Verdict(unittest.TestCase):
    def test_unavailable_carries_the_reason(self):
        v = lighting_verdict(False, 0, "session state CSS_Timeout")
        self.assertTrue(v.startswith("SDK unavailable:"))
        self.assertIn("CSS_Timeout", v)

    def test_unavailable_without_a_reason_is_still_a_sentence(self):
        self.assertTrue(lighting_verdict(False, 0).startswith("SDK unavailable:"))

    def test_connected_but_no_leds_is_its_own_verdict(self):
        v = lighting_verdict(True, 0)
        self.assertTrue(v.startswith("no lightable LEDs"))

    def test_paintable_would_glow(self):
        v = lighting_verdict(True, 12)
        self.assertTrue(v.startswith("would glow"))
        self.assertIn("12", v)

    def test_the_three_verdicts_are_distinct(self):
        vs = {
            lighting_verdict(False, 0, "x"),
            lighting_verdict(True, 0),
            lighting_verdict(True, 5),
        }
        self.assertEqual(len(vs), 3)


class SelfTest(unittest.TestCase):
    def test_facts_from_a_paintable_adapter(self):
        f = sidecrab_glow.selftest_facts(NullAdapter(led_count=12))
        self.assertTrue(f["sdk_reachable"])
        self.assertEqual(f["lightable_leds"], 12)
        self.assertTrue(f["verdict"].startswith("would glow"))

    def test_facts_when_connected_with_zero_leds(self):
        """This machine's actual shape once the SDK is up: a LINK hub, 0 LEDs."""
        a = NullAdapter(led_count=0)
        f = sidecrab_glow.selftest_facts(a)
        self.assertTrue(f["sdk_reachable"])
        self.assertEqual(f["lightable_leds"], 0)
        self.assertTrue(f["verdict"].startswith("no lightable LEDs"))

    def test_facts_when_the_sdk_never_connected(self):
        a = NullAdapter(led_count=0)
        a.connected = False
        f = sidecrab_glow.selftest_facts(a, "session state CSS_Timeout")
        self.assertFalse(f["sdk_reachable"])
        self.assertEqual(f["inventory"], "no session")
        self.assertIn("CSS_Timeout", f["verdict"])

    def test_report_names_every_field_an_operator_needs(self):
        text = sidecrab_glow.format_selftest(
            sidecrab_glow.selftest_facts(NullAdapter(led_count=3))
        )
        for needle in ("SDK reachable", "session state", "devices seen by SDK",
                       "TOTAL lightable LEDs", "VERDICT"):
            self.assertIn(needle, text)

    def test_exit_codes_distinguish_the_three_outcomes(self):
        dark = NullAdapter(led_count=0)
        dark.connected = False
        dark.connect = lambda: (_ for _ in ()).throw(IcueUnavailable("nope"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertLogs("sidecrab", "INFO"):
            self.assertEqual(sidecrab_glow.selftest(NullAdapter(led_count=12)), 0)
            self.assertEqual(sidecrab_glow.selftest(NullAdapter(led_count=0)), 1)
            self.assertEqual(sidecrab_glow.selftest(dark), 2)
        self.assertIn("VERDICT", buf.getvalue())

    def test_startup_log_and_selftest_give_the_same_verdict(self):
        """An operator reading the log and one running --selftest must agree."""
        a = NullAdapter(led_count=0)
        facts = sidecrab_glow.selftest_facts(a)
        self.assertEqual(
            facts["verdict"], lighting_verdict(a.connected, a.led_count, None)
        )


class LoggingUnderPythonw(unittest.TestCase):
    """pythonw has no stdout AND no stderr: StreamHandler(None) makes every
    record raise inside emit() and be swallowed. The file log is the record."""

    def test_no_stream_handler_is_added_when_stdout_is_none(self):
        import logging

        root = logging.getLogger("sidecrab")
        saved_stdout, saved_handlers = sys.stdout, list(root.handlers)
        try:
            sys.stdout = None
            sidecrab_glow.setup_logging()
            streams = [
                h for h in root.handlers
                if isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
            ]
            self.assertEqual(streams, [])
        finally:
            sys.stdout = saved_stdout
            for h in root.handlers:
                if h not in saved_handlers:
                    h.close()
            root.handlers[:] = saved_handlers


if __name__ == "__main__":
    unittest.main()
