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


if __name__ == "__main__":
    unittest.main()
