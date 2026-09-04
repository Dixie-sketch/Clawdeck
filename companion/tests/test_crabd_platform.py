"""The platform seam: one object per OS, selected once, injected everywhere.

crabd is a single module that used to reach for `ctypes.windll` and `schtasks` in the
middle of the readers that need them. This file pins the seam that replaced those
reaches: three classes with one public surface, chosen by `select_platform`, and the
rule that INJECTION still beats the platform so every reader stays testable on any host.
"""

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


if __name__ == "__main__":
    unittest.main()
