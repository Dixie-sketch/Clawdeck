"""The platform seam: one object per OS, selected once, injected everywhere.

crabd is a single module that used to reach for `ctypes.windll` and `schtasks` in the
middle of the readers that need them. This file pins the seam that replaced those
reaches: three classes with one public surface, chosen by `select_platform`, and the
rule that INJECTION still beats the platform so every reader stays testable on any host.
"""

import inspect
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
    these globals name REAL files under ~, and a reader built without an explicit path
    would otherwise reach the operator's live store (the limits cache was poisoned
    exactly this way on 2026-08-26).

    LIMITS_TOKEN_FILE is here for a sharper reason than the other four: it is the
    operator's long-lived usage token, and this module builds real LimitsReaders whose
    fallback path reads it by default.
    """
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    root = Path(_MODULE_TMP.name)
    setUpModule.originals = (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
                             crabd.HISTORY_FILE, crabd.CREDENTIALS_FILE,
                             crabd.LIMITS_TOKEN_FILE)
    crabd.LIMITS_CACHE_FILE = root / "limits-cache.json"
    crabd.USER_CONFIG_FILE = root / "config.json"
    crabd.HISTORY_FILE = root / "history.jsonl"
    crabd.CREDENTIALS_FILE = root / "no-such-credentials.json"
    crabd.LIMITS_TOKEN_FILE = root / "no-such-limits-token.dpapi"
    # The Keychain kill switch, for the same reason as the paths above: with it
    # False, nothing in this module can reach the operator's login Keychain - no
    # prompt on their desktop, and no secret this suite has any business seeing.
    setUpModule.keychain = crabd.KEYCHAIN_CREDENTIALS_ENABLED
    crabd.KEYCHAIN_CREDENTIALS_ENABLED = False


def tearDownModule():
    (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE, crabd.HISTORY_FILE,
     crabd.CREDENTIALS_FILE, crabd.LIMITS_TOKEN_FILE) = setUpModule.originals
    _MODULE_TMP.cleanup()
    crabd.KEYCHAIN_CREDENTIALS_ENABLED = setUpModule.keychain


class ModuleIsolationTests(unittest.TestCase):
    """Asserted, not assumed. Every global below names a real file under ~, and this
    module builds real LimitsReaders; the limits cache was poisoned by exactly this
    oversight on 2026-08-26, and LIMITS_TOKEN_FILE names the operator's token store."""

    def test_every_global_naming_a_real_file_points_into_the_sandbox(self):
        sandbox = Path(_MODULE_TMP.name)
        for name in ("LIMITS_CACHE_FILE", "USER_CONFIG_FILE", "HISTORY_FILE",
                     "CREDENTIALS_FILE", "LIMITS_TOKEN_FILE"):
            with self.subTest(global_name=name):
                self.assertEqual(getattr(crabd, name).parent, sandbox)


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
               "service_status", "read_limits_token", "store_limits_token",
               "limits_token_hint", "cli_credentials", "server_reuse_address",
               "port_holder_hint"}
    PLATFORMS = (crabd.WindowsPlatform, crabd.DarwinPlatform, crabd.NullPlatform)

    @staticmethod
    def surface(cls):
        return {n for n in dir(cls)
                if not n.startswith("_") and callable(getattr(cls, n))}

    def test_all_three_platforms_expose_exactly_the_same_methods(self):
        for cls in self.PLATFORMS:
            with self.subTest(platform=cls.__name__):
                self.assertEqual(self.surface(cls), self.SURFACE)

    def test_every_method_takes_the_same_arguments_on_all_three(self):
        """Matching NAMES are not a seam. A reader calls one of these with positional
        arguments it chose once; a platform that renamed or reordered a parameter would
        pass every name test here and fail at the call, on one OS only."""
        for name in sorted(self.SURFACE):
            with self.subTest(method=name):
                signatures = {cls.__name__: str(inspect.signature(getattr(cls, name)))
                              for cls in self.PLATFORMS}
                self.assertEqual(len(set(signatures.values())), 1, signatures)

    def test_every_method_is_bound_the_same_way_on_all_three(self):
        """staticmethod on two and classmethod on the third is the same call today and
        stops being one the moment anything subclasses or rebinds. The promise is that
        the three are INTERCHANGEABLE, not that they merely answer alike right now.

        Today the split is: `cpu_times`, `memory`, `cli_credentials`,
        `read_limits_token`, `store_limits_token` and `limits_token_hint` are plain
        instance methods on all three, everything else is static. Only DarwinPlatform needs the instance (the
        32-bit mach counters are unwrapped against state it keeps, and the Keychain
        runner and the item's service name are seams on it); the other two carry no state
        and are instance methods anyway, so that this test keeps passing."""
        for name in sorted(self.SURFACE):
            with self.subTest(method=name):
                kinds = {cls.__name__: type(inspect.getattr_static(cls, name)).__name__
                         for cls in self.PLATFORMS}
                self.assertEqual(len(set(kinds.values())), 1, kinds)


class SocketReusePlatformTests(unittest.TestCase):
    """SO_REUSEADDR means two different things, so it is a platform ANSWER.

    On Windows it lets a SECOND process bind a port that is already being listened on -
    two crabds then answer half the requests each, which is the split feed that was
    measured during build QA. Refusing reuse there turns that into a loud "already
    running".

    On BSD and Linux it does NOT admit a second listener; all it does is let a fresh
    listener take a port that a CLOSED connection still holds in TIME_WAIT. Refusing it
    there buys no safety at all and costs the ordinary restart: crabd stopped and started
    again inside the TIME_WAIT window cannot bind, and prints the "another process is
    holding it" message about its own dead connection.
    """

    def test_windows_refuses_reuse_and_the_other_two_take_it(self):
        self.assertIs(crabd.WindowsPlatform().server_reuse_address(), False)
        self.assertIs(crabd.DarwinPlatform().server_reuse_address(), True)
        self.assertIs(crabd.NullPlatform().server_reuse_address(), True)


class PortHolderHintTests(unittest.TestCase):
    """"Find out what is holding the port" is a different command on every OS.

    The old collision message hard-coded `lsof`, which is not on a Windows box at all -
    so the one message whose entire job is to tell the operator what to do next told
    half of them to run something they do not have.
    """

    def test_windows_names_a_powershell_command(self):
        hint = crabd.WindowsPlatform().port_holder_hint(9999)
        self.assertIn("Get-NetTCPConnection", hint)
        self.assertIn("-LocalPort 9999", hint)
        self.assertIn("-State Listen", hint)
        self.assertIn("OwningProcess", hint)
        self.assertNotIn("lsof", hint)

    def test_darwin_and_null_name_lsof(self):
        """Null gets the POSIX answer rather than an empty one: Linux is the platform
        that lands here, and `lsof` is the command there too."""
        for platform in (crabd.DarwinPlatform(), crabd.NullPlatform()):
            with self.subTest(platform=platform.name):
                hint = platform.port_holder_hint(9999)
                self.assertEqual(hint, "lsof -nP -iTCP:9999 -sTCP:LISTEN")

    def test_the_port_is_interpolated_not_assumed(self):
        for platform in PlatformSurfaceTests.PLATFORMS:
            with self.subTest(platform=platform.__name__):
                self.assertIn("2722", platform.port_holder_hint(2722))


class CliCredentialsAreOneReaderTests(unittest.TestCase):
    """All three platforms read the CLI credential FILE through one body, delegated to -
    three copies is three places for the per-call CREDENTIALS_FILE lookup to be quietly
    bound at import in a later edit.

    Since v0.34.0 macOS has a SECOND source behind that one (the login Keychain), and
    this pins the order from the other end: with the file answering, all three give the
    same answer and Darwin never gets as far as its Keychain seam.
    """

    def test_all_three_delegate_to_the_one_module_level_reader(self):
        original = crabd._read_cli_credentials
        self.addCleanup(lambda: setattr(crabd, "_read_cli_credentials", original))
        crabd._read_cli_credentials = lambda: "sentinel"
        for cls in PlatformSurfaceTests.PLATFORMS:
            with self.subTest(platform=cls.__name__):
                self.assertEqual(cls().cli_credentials(), "sentinel")


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
    def owners_of(cls, needle: str) -> list[str]:
        """Who owns each line of CODE containing `needle`, in file order.

        Comment lines are skipped: this file's whole subject is where the platform
        decision lives, so the prose that explains the rule names the same literals the
        rule forbids. A test that cannot tell an explanation from a branch punishes
        writing the explanation down.
        """
        return [cls.owner(i) for i, line in enumerate(cls.LINES)
                if needle in line and not line.strip().startswith("#")]

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

    def test_sys_platform_is_read_exactly_once_and_at_module_level(self):
        """One reading, owned by nothing - the selector call. Counted by owner rather
        than matched against the line's text, so wrapping the call does not break it."""
        self.assertEqual(self.owners_of("sys.platform"), ["<module>"])

    def test_windll_appears_only_in_the_windows_platform_and_the_dpapi_helper(self):
        self.assertLessEqual(set(self.owners_of("windll")),
                             {"WindowsPlatform", "_dpapi_protect", "_dpapi_unprotect"})

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
        self.assertIsNone(crabd.HostSampler(platform=crabd.NullPlatform()).sample())

    @unittest.skipIf(sys.platform == "darwin",
                     "the mach counters answer for real on macOS - see "
                     "test_crabd_host_darwin.py")
    def test_darwin_selected_off_a_mac_also_serves_no_block(self):
        """The counters DarwinPlatform reaches for (mach host_statistics, sysctlbyname)
        are not on the host running this, so the reader takes its failure path and the
        answer is the same absence NullPlatform gives - never a fabricated zero."""
        self.assertIsNone(crabd.HostSampler(platform=crabd.DarwinPlatform()).sample())

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
        not running" and "I could not find out" are different claims.

        NullPlatform only: since v0.33.0 Darwin HAS a service manager (launchd) and
        answers about it - what a Mac serves is pinned in test_crabd_fleet_darwin.py.
        """
        fleet = crabd.FleetReader(platform=crabd.NullPlatform())
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

    def test_the_no_service_sentinel_is_answered_on_purpose_by_all_three(self):
        """FLEET_NO_SERVICE is a QUESTION PUT TO THE PLATFORM, not a value with one
        meaning: "you named a component you have no service for - what is that?" Each
        answers in its own terms, and each answer is now an explicit branch rather than
        whatever its ordinary parse happens to make of a None code.

        Windows and Darwin say `absent`: there is no service, so there is nothing to be
        running or stopped, and that is a fact about the machine.

        Null says `unknown`, and that is not an oversight. NullPlatform is the platform
        with NO SERVICE MANAGER - Linux CI - so it has no way to observe anything at all;
        "there is no notifier here" is a claim it cannot make. "I could not find out" is
        the true one, and it is the same word this platform gives every other question.

        Before this, Windows fell through its `code != 0` branch and scanned an empty
        blob for a not-found marker: `unknown` by accident, and `absent` by accident on
        the day some stderr carried one of those words.
        """
        for cls, expected in ((crabd.WindowsPlatform, "absent"),
                              (crabd.DarwinPlatform, "absent"),
                              (crabd.NullPlatform, "unknown")):
            with self.subTest(platform=cls.__name__):
                self.assertEqual(cls().service_status(*crabd.FLEET_NO_SERVICE), expected)

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

    A platform that cannot read that FILE answers None rather than handing the raw bytes
    on as a bearer token - it is ciphertext everywhere, and "I could not decrypt it" is
    not "here it is".

    Since v0.34.0 macOS HAS a token store, but it is not this file: it is a login
    Keychain item, and what a Mac reads out of it is pinned in
    test_crabd_token_darwin.py. Here it answers None because the module kill switch is
    off, which is exactly the guarantee this module wants - a reader built with no
    injected seam cannot reach the operator's Keychain.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "limits-token.dpapi"
        self.path.write_bytes(b"  gnol-10tao-tna-ks  ")   # reversed, with padding
        original = crabd._dpapi_unprotect
        self.addCleanup(lambda: setattr(crabd, "_dpapi_unprotect", original))

    def test_a_platform_that_does_not_read_this_file_reads_nothing_from_it(self):
        """Null has no store at all; Darwin has one that is not a file, and with the
        Keychain switched off for this module it reaches nothing. Neither hands the
        undecrypted bytes on."""
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

    @unittest.skipIf(sys.platform in ("win32", "darwin"),
                     "the Win32 and the mach counters really do answer on those two - "
                     "what a Mac serves is pinned in test_crabd_host_darwin.py")
    def test_a_host_with_no_counters_still_serves_no_host_key(self):
        state = self.builder.build()
        self.assertNotIn("host", state)
        self.assertEqual(state["schema"], 5)     # nothing else moved


if __name__ == "__main__":
    unittest.main()
