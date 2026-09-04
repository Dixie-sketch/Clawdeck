"""The platform seam: one object per OS, selected once, injected everywhere.

crabd is a single module that used to reach for `ctypes.windll` and `schtasks` in the
middle of the readers that need them. This file pins the seam that replaced those
reaches: three classes with one public surface, chosen by `select_platform`, and the
rule that INJECTION still beats the platform so every reader stays testable on any host.
"""

import io
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crabd  # noqa: E402


# --------------------------------------------------------------- module isolation

_MODULE_TMP = None


def setUpModule():
    """The same hard isolation test_crabd.py takes, and for the same measured reason:
    these four module globals name REAL files under ~, and a reader built without an
    explicit path would otherwise reach the operator's live store (the limits cache was
    poisoned exactly this way on 2026-08-26)."""
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    root = Path(_MODULE_TMP.name)
    setUpModule.originals = (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
                             crabd.HISTORY_FILE, crabd.CREDENTIALS_FILE)
    crabd.LIMITS_CACHE_FILE = root / "limits-cache.json"
    crabd.USER_CONFIG_FILE = root / "config.json"
    crabd.HISTORY_FILE = root / "history.jsonl"
    crabd.CREDENTIALS_FILE = root / "no-such-credentials.json"


def tearDownModule():
    (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
     crabd.HISTORY_FILE, crabd.CREDENTIALS_FILE) = setUpModule.originals
    crabd.Handler.builder = None
    _MODULE_TMP.cleanup()


# ------------------------------------------------------------------- the selector

class SelectPlatformTests(unittest.TestCase):
    def test_each_sys_platform_string_picks_its_class(self):
        for value, cls, name in (
                ("win32", crabd.WindowsPlatform, "windows"),
                ("darwin", crabd.DarwinPlatform, "darwin"),
                ("linux", crabd.NullPlatform, "none"),
                ("freebsd14", crabd.NullPlatform, "none")):
            with self.subTest(sys_platform=value):
                chosen = crabd.select_platform(value)
                self.assertIsInstance(chosen, cls)
                self.assertEqual(chosen.name, name)

    def test_the_module_singleton_matches_this_host(self):
        """One decision, taken once at import. Every reader defaults to this object, so
        a second `sys.platform` test anywhere downstream would be a second answer."""
        self.assertIsInstance(crabd.PLATFORM,
                              type(crabd.select_platform(sys.platform)))


class PlatformSurfaceTests(unittest.TestCase):
    """The three classes are interchangeable or they are not a seam.

    A method added to one of them alone is the failure this catches: the reader that
    calls it works on the host it was written on and raises AttributeError on every
    other, which is a crash in a daemon whose whole job is to keep serving.
    """

    SURFACE = {"cpu_times", "memory", "fleet_targets", "service_query",
               "service_status", "read_limits_token", "cli_credentials"}

    @staticmethod
    def surface(cls):
        return {n for n in dir(cls)
                if not n.startswith("_") and callable(getattr(cls, n))}

    def test_all_three_platforms_expose_exactly_the_same_methods(self):
        for cls in (crabd.WindowsPlatform, crabd.DarwinPlatform, crabd.NullPlatform):
            with self.subTest(platform=cls.__name__):
                self.assertEqual(self.surface(cls), self.SURFACE)


class OnePlatformDecisionTests(unittest.TestCase):
    """Read against the SOURCE TEXT, because that is where the rule lives.

    A behavioural test cannot catch a second `sys.platform` branch: the branch is
    correct on the host that runs the suite and wrong on the one that does not, which
    is the whole failure mode. Every OS-specific syscall belongs to a platform class,
    and there is exactly one place that decides which one.
    """

    SOURCE = (Path(crabd.__file__).read_text(encoding="utf-8"))
    LINES = SOURCE.splitlines()

    @classmethod
    def owner(cls, index: int) -> str:
        """The top-level `class X` / `def x` a line sits INSIDE. A line at column 0 is
        inside nothing, which is what makes the module-level selector distinguishable
        from a branch smuggled into a reader."""
        line = cls.LINES[index]
        if line[:1] not in (" ", "\t"):
            return "<module>"
        for above in reversed(cls.LINES[:index]):
            if above.startswith("class ") or above.startswith("def "):
                return above.split("(")[0].split(":")[0].split()[1]
        return "<module>"

    def test_sys_platform_is_read_exactly_once(self):
        """One line, and it is the selector call. Named in full rather than counted:
        the claim is WHICH line, not how many."""
        self.assertEqual([line.strip() for line in self.LINES if "sys.platform" in line],
                         ["PLATFORM = select_platform(sys.platform)"])

    def test_windll_appears_only_in_the_windows_platform_and_the_dpapi_helper(self):
        owners = {self.owner(i) for i, line in enumerate(self.LINES)
                  if "windll" in line}
        self.assertLessEqual(owners, {"WindowsPlatform", "_dpapi_unprotect"})

    def test_os_name_is_never_a_platform_test(self):
        self.assertNotIn("os.name", self.SOURCE)


# ---------------------------------------------------------------- the host sampler

_TICKS_PER_SEC = 10_000_000        # a FILETIME counts 100 ns units
_MEM_READING = (32 * 1024 ** 3, 12 * 1024 ** 3)
_BASE = (100 * _TICKS_PER_SEC, 400 * _TICKS_PER_SEC, 90 * _TICKS_PER_SEC)
_STEP = (103 * _TICKS_PER_SEC, 404 * _TICKS_PER_SEC, 91 * _TICKS_PER_SEC)


class HostSamplerPlatformTests(unittest.TestCase):
    """Which reader HostSampler uses, and what a platform with no counters serves.

    The arithmetic lives in test_crabd.py; this is only about the seam - that a
    counter-less platform serves NO `host` key rather than a row of zeroes, and that an
    injected reader still outranks the platform so the arithmetic stays testable here.
    """

    def test_a_platform_with_no_counters_serves_no_block_at_all(self):
        for platform in (crabd.NullPlatform(), crabd.DarwinPlatform()):
            with self.subTest(platform=platform.name):
                self.assertIsNone(crabd.HostSampler(platform=platform).sample())

    def test_an_injected_reader_outranks_the_platform(self):
        """Injection is the primary seam and the platform is only the DEFAULT. A
        platform that won the tie would make every scripted-FILETIME test in the suite
        unreachable off Windows."""
        readings = [_BASE, _STEP]
        sampler = crabd.HostSampler(times=lambda: readings.pop(0),
                                    memory=lambda: _MEM_READING,
                                    platform=crabd.NullPlatform())
        self.assertIsNone(sampler.sample()["cpuPct"])   # first sample: no delta yet
        self.assertEqual(sampler.sample()["cpuPct"], 40.0)


@unittest.skipUnless(sys.platform != "win32",
                     "the Win32 counters answer for real on Windows")
class WindowsCountersOffWindowsTests(unittest.TestCase):
    """WindowsPlatform selected on a host that is not Windows: no block, and ONE log
    line per failure kind. Silence is the forbidden failure mode; a per-pass heartbeat
    is the other one."""

    def setUp(self):
        original = set(crabd._LOG_ONCE_SEEN)
        crabd._LOG_ONCE_SEEN.discard(crabd.HOST_CPU_LOG_KEY)
        crabd._LOG_ONCE_SEEN.discard(crabd.HOST_MEM_LOG_KEY)
        self.addCleanup(lambda: (crabd._LOG_ONCE_SEEN.clear(),
                                 crabd._LOG_ONCE_SEEN.update(original)))

    def test_three_samples_serve_nothing_and_log_each_failure_once(self):
        sampler = crabd.HostSampler(platform=crabd.WindowsPlatform())
        original, sys.stderr = sys.stderr, io.StringIO()
        try:
            for _ in range(3):
                self.assertIsNone(sampler.sample())
            noise = sys.stderr.getvalue()
        finally:
            sys.stderr = original
        self.assertEqual(noise.count("GetSystemTimes unavailable"), 1, noise)
        self.assertEqual(noise.count("GlobalMemoryStatusEx unavailable"), 1, noise)


# ----------------------------------------------------------------- the fleet reader

class FakeServiceQuery:
    """One canned (code, out, err) per target, or an exception to raise."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, target, timeout):
        self.calls.append((target, timeout))
        result = self.results[target]
        if isinstance(result, BaseException) or (
                isinstance(result, type) and issubclass(result, BaseException)):
            raise result
        return result


#: The measured Windows shape: quoted name, next-run-time, status. CRLF, no header.
RUNNING_CSV = (0, '"\\SideCrab-glow","N/A","Running"\r\n', "")


class FleetPlatformTests(unittest.TestCase):
    def test_a_platform_with_no_service_manager_reports_both_components_unknown(self):
        """`unknown`, not `stopped`, and both contract keys present. "the notifier is
        not running" and "I could not find out" are different claims."""
        for platform in (crabd.NullPlatform(), crabd.DarwinPlatform()):
            with self.subTest(platform=platform.name):
                fleet = crabd.FleetReader(platform=platform)
                self.assertEqual(fleet.get(), {"glow": "unknown", "toast": "unknown"})
                fleet.poll(0.0)
                self.assertEqual(fleet.get(), {"glow": "unknown", "toast": "unknown"})

    def test_unknown_answers_without_an_instance(self):
        """StateBuilder calls `FleetReader.unknown()` on the CLASS, for a builder with
        no reader attached. An instance method there would be a TypeError on every
        build - and `build()` is the one path that must never raise."""
        self.assertEqual(crabd.FleetReader.unknown(),
                         {"glow": "unknown", "toast": "unknown"})

    def test_unknown_takes_the_keys_from_the_platform_it_is_given(self):
        self.assertEqual(crabd.FleetReader.unknown(crabd.WindowsPlatform()),
                         {"glow": "unknown", "toast": "unknown"})

    def test_the_status_parse_belongs_to_the_platform_not_the_reader(self):
        """The SAME csv row: Windows reads Running out of it, Darwin has no parser yet
        and says so. A reader that owned the parse would claim a launchd service was
        running because a schtasks row said so."""
        runner = FakeServiceQuery({"SideCrab-glow": RUNNING_CSV})
        self.assertEqual(
            crabd.FleetReader(runner=runner,
                              platform=crabd.WindowsPlatform()).status("SideCrab-glow"),
            "running")
        self.assertEqual(
            crabd.FleetReader(runner=runner,
                              platform=crabd.DarwinPlatform()).status("SideCrab-glow"),
            "unknown")


# --------------------------------------------------------- the long-lived token store

class LimitsTokenPlatformTests(unittest.TestCase):
    """`~/.sidecrab/limits-token.dpapi` is a DPAPI blob, which is a Windows fact.

    A platform with no token store answers None rather than handing the raw bytes on as
    a bearer token - the file is ciphertext everywhere, and "I could not decrypt it" is
    not "here it is".
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "limits-token.dpapi"
        self.path.write_bytes(b"  gnol-10tao-tna-ks  ")   # reversed, with padding
        original = crabd._dpapi_unprotect
        self.addCleanup(lambda: setattr(crabd, "_dpapi_unprotect", original))

    def test_a_platform_with_no_token_store_reads_nothing_from_a_file_that_has_bytes(self):
        for platform in (crabd.NullPlatform(), crabd.DarwinPlatform()):
            with self.subTest(platform=platform.name):
                self.assertIsNone(
                    crabd.read_limits_token(self.path, platform=platform))

    def test_windows_decrypts_and_strips_through_the_module_level_helper(self):
        """The stub is installed on the MODULE after the platform class was defined, so
        this also pins that `_dpapi_unprotect` is looked up at call time - bound at
        import, every test of this path off Windows would be unreachable."""
        crabd._dpapi_unprotect = lambda blob: blob[::-1]
        self.assertEqual(
            crabd.read_limits_token(self.path, platform=crabd.WindowsPlatform()),
            "sk-ant-oat01-long")


class LimitsReaderPlatformTests(unittest.TestCase):
    """The same expired-CLI-token morning on two platforms.

    Windows has a store to fall back to and lights the gauges off it; a platform with
    none serves `available: false` and the note that says what to do. Neither fabricates
    a reading, and the fallback token never reaches the served document.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.creds = root / "credentials.json"
        self.token_file = root / "limits-token.dpapi"
        self.cache = root / "cache.json"
        original = (crabd.CREDENTIALS_FILE, crabd.LIMITS_TOKEN_FILE,
                    crabd.urllib.request.urlopen, crabd._dpapi_unprotect)

        def restore():
            (crabd.CREDENTIALS_FILE, crabd.LIMITS_TOKEN_FILE,
             crabd.urllib.request.urlopen, crabd._dpapi_unprotect) = original

        self.addCleanup(restore)
        crabd.CREDENTIALS_FILE = self.creds
        crabd.LIMITS_TOKEN_FILE = self.token_file
        crabd._dpapi_unprotect = lambda blob: blob[::-1]     # "decrypt" = reverse
        self.seen = []
        body = ('{"five_hour": {"utilization": 0.25, "resets_at": 1800000000},'
                ' "seven_day": {"utilization": 0.5, "resets_at": 1800500000}}').encode()

        class _Resp:
            def __init__(self, payload): self._b = payload
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(request, timeout=None):
            self.seen.append(request.get_header("Authorization"))
            return _Resp(body)

        crabd.urllib.request.urlopen = fake_urlopen
        # Expired 1 s ago: the CLI token is unusable, which is the whole scenario.
        import json as _json
        self.creds.write_text(_json.dumps({"claudeAiOauth": {
            "accessToken": "cli-token", "expiresAt": 1,
            "subscriptionType": "max", "rateLimitTier": "t"}}), encoding="utf-8")
        self.token_file.write_bytes(b"sk-ant-oat01-long"[::-1])

    def reader(self, platform):
        return crabd.LimitsReader(cache_file=self.cache, platform=platform)

    def test_windows_falls_back_to_the_stored_token(self):
        out = self.reader(crabd.WindowsPlatform()).get(1_800_000_000.0, force=True)
        self.assertTrue(out["available"])
        self.assertEqual(out["tokenSource"], "sidecrab")
        self.assertEqual(self.seen, ["Bearer sk-ant-oat01-long"])
        self.assertNotIn("sk-ant-oat01-long", crabd.dump_state(out).decode())

    def test_a_platform_with_no_store_says_expired_instead_of_using_the_bytes(self):
        out = self.reader(crabd.NullPlatform()).get(1_800_000_000.0, force=True)
        self.assertFalse(out["available"])
        self.assertIn("expired", out["note"])
        self.assertIsNone(out["fiveHour"])
        self.assertEqual(self.seen, [])          # never reached the endpoint

    def test_the_credential_document_is_read_through_the_platform(self):
        """cli_credentials is the one reader that is already portable, so the missing
        file note must be the same answer everywhere."""
        self.creds.unlink()
        for platform in (crabd.WindowsPlatform(), crabd.DarwinPlatform(),
                         crabd.NullPlatform()):
            with self.subTest(platform=platform.name):
                out = self.reader(platform).get(1_800_000_000.0, force=True)
                self.assertFalse(out["available"])
                self.assertIn("no Claude credentials", out["note"])


# ------------------------------------------------- what this host actually serves

class StubLimits:
    def get(self, now, force=False):
        return {"available": False, "note": "stub", "fiveHour": None, "weekly": None,
                "extra": [], "subscriptionType": None, "rateLimitTier": None}


class DefaultBuilderOnThisHostTests(unittest.TestCase):
    """The phase's no-behaviour-change claim, taken off a real document.

    Everything above proves the seam in isolation with a platform injected. This is the
    one that would catch the seam changing what a DEFAULT crabd serves here: the widget
    feature-detects `host` by presence, so a block appearing where none used to is a
    contract change, and `fleet` folding into anything but unknown is a green dot on a
    component nobody asked about.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "projects").mkdir()
        original = crabd.USER_CONFIG_FILE
        crabd.USER_CONFIG_FILE = root / "config.json"
        self.addCleanup(lambda: setattr(crabd, "USER_CONFIG_FILE", original))
        self.builder = crabd.StateBuilder(
            crabd.TranscriptStore(root / "projects"), crabd.HookTracker(),
            StubLimits(), 0.0)

    def test_a_builder_with_no_fleet_reader_serves_both_components_unknown(self):
        self.assertEqual(self.builder.build()["fleet"],
                         {"glow": "unknown", "toast": "unknown"})

    @unittest.skipUnless(sys.platform != "win32",
                         "the Win32 counters really do answer on Windows")
    def test_a_host_with_no_counters_still_serves_no_host_key(self):
        state = self.builder.build()
        self.assertNotIn("host", state)
        self.assertEqual(state["schema"], 5)     # nothing else moved


if __name__ == "__main__":
    unittest.main()
