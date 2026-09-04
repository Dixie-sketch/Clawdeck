"""`fleet` on macOS: reading launchd, and the four outcomes it maps onto.

FleetReader is not changed by this file in substance: it still owns the caching, the
per-thread poll and the rule that a service whose state cannot be read is `unknown` and
never folded into `stopped`. What is new is the PLATFORM half - which labels exist, how
one is queried, and how launchctl's answer is read - and one line in the reader for a
component that has no service at all.

Every block quoted below was measured on 2026-09-04, macOS 26.6, uid 502, with
`launchctl print gui/<uid>/<label>`. They are pasted rather than paraphrased: the
first-level indentation is a single TAB and the nesting is what makes the parse
non-trivial, so a paraphrase would test a different string than the one launchd emits.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crabd  # noqa: E402
from _httpkeepalive import start_test_server  # noqa: E402


# --------------------------------------------------------------- module isolation

_MODULE_TMP = None


def setUpModule():
    """The same hard isolation every other companion test module takes: these globals
    name REAL files under ~, and a reader built without an explicit path would otherwise
    reach the operator's live store (the limits cache was poisoned exactly this way on
    2026-08-26)."""
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
    def test_every_global_naming_a_real_file_points_into_the_sandbox(self):
        sandbox = Path(_MODULE_TMP.name)
        for name in ("LIMITS_CACHE_FILE", "USER_CONFIG_FILE", "HISTORY_FILE",
                     "CREDENTIALS_FILE", "LIMITS_TOKEN_FILE"):
            with self.subTest(global_name=name):
                self.assertEqual(getattr(crabd, name).parent, sandbox)


# ---------------------------------------------------------------- measured fixtures

#: `launchctl print gui/502/com.apple.cloudphotod`, exit 0. The two `state = active`
#: lines are sub-objects of the service, two tabs deep, and they are the trap.
RUNNING_BLOCK = (
    "gui/502/com.apple.cloudphotod = {\n"
    "\tactive count = 4\n"
    "\tpath = /System/Library/LaunchAgents/com.apple.cloudphotod.plist\n"
    "\ttype = LaunchAgent\n"
    "\tstate = running\n"
    "\truns = 1\n"
    "\tpid = 4057\n"
    "\tendpoints = {\n"
    "\t\t\"com.apple.cloudphotod\" = {\n"
    "\t\tstate = active\n"
    "\t}\n"
    "\t}\n"
    "\tspawn statistics = {\n"
    "\t\tstate = active\n"
    "\t}\n"
    "}\n")

#: `launchctl print gui/502/com.apple.SafariHistoryServiceAgent`, exit 0: loaded, idle.
#: No pid line at all, which is why the pid is not what this reads.
NOT_RUNNING_BLOCK = (
    "gui/502/com.sidecrab.toast = {\n"
    "\tactive count = 0\n"
    "\tpath = /Users/x/Library/LaunchAgents/com.sidecrab.toast.plist\n"
    "\ttype = LaunchAgent\n"
    "\tstate = not running\n"
    "\truns = 0\n"
    "\tlast exit code = (never exited)\n"
    "}\n")

#: `launchctl print gui/502/com.sidecrab.nonexistent`: exit 113, nothing on stdout.
ABSENT_STDERR = ('Bad request.\n'
                 'Could not find service "com.sidecrab.nonexistent" in domain for '
                 'user gui: 502\n')


def block_with_state(word):
    """The not-running block with its first-level state word replaced."""
    return NOT_RUNNING_BLOCK.replace("state = not running", f"state = {word}")


class LaunchctlStatusMapTests(unittest.TestCase):
    """`service_status(code, out, err)` - launchd's answer turned into one of the
    contract's four words. The mapping is a fixed vocabulary and anything outside it is
    `unknown`, because "the notifier is not running" and "I could not find out" are
    different claims and a widget dot that guesses the first is the silent all-green
    failure the contract exists to prevent."""

    def status(self, code, out="", err=""):
        return crabd.DarwinPlatform().service_status(code, out, err)

    def test_the_recorded_running_block_reads_as_running(self):
        self.assertEqual(self.status(0, RUNNING_BLOCK), "running")

    def test_a_loaded_idle_agent_reads_as_stopped(self):
        self.assertEqual(self.status(0, NOT_RUNNING_BLOCK), "stopped")

    def test_the_other_words_launchd_uses_for_not_executing_read_as_stopped(self):
        for word in ("not running", "waiting", "spawn scheduled"):
            with self.subTest(state=word):
                self.assertEqual(self.status(0, block_with_state(word)), "stopped")

    def test_a_nested_state_line_is_not_the_services_own(self):
        """THE TRAP. launchd indents the service's own properties with one tab and its
        sub-objects deeper, and those carry their own `state = ...` lines. A parser that
        took the first `state =` anywhere - or the last - would report a stopped agent
        as running on the strength of a sub-object."""
        nested = "\tendpoints = {\n\t\tstate = running\n\t}\n"
        blocks = {
            # Above the service's own line, so "first match anywhere" reads it...
            "nested above": NOT_RUNNING_BLOCK.replace(
                "\tstate = not running\n", nested + "\tstate = not running\n"),
            # ...and below it, so "last match anywhere" does.
            "nested below": NOT_RUNNING_BLOCK.replace(
                "\tlast exit code = (never exited)\n", nested),
        }
        for order, block in blocks.items():
            with self.subTest(order=order):
                self.assertIn("\t\tstate = running", block)
                self.assertEqual(self.status(0, block), "stopped")

    def test_the_recorded_absent_stderr_reads_as_absent(self):
        self.assertEqual(self.status(113, "", ABSENT_STDERR), "absent")

    def test_a_failure_that_is_not_a_missing_service_is_unknown(self):
        """A label that exists and cannot be read is NOT the same claim as an absent
        one, so only the not-found wording earns `absent`."""
        for err in ("Could not find domain for user gui: 502\n",
                    "Bad request.\n",
                    "Permission denied\n",
                    ""):
            with self.subTest(stderr=err.strip()):
                self.assertEqual(self.status(113, "", err), "unknown")

    def test_an_exit_zero_the_parser_cannot_read_is_unknown(self):
        """Never `stopped`. An answer this code does not understand is a gap in what
        crabd knows, and saying so is the whole point of having a fourth word."""
        for out in ("",
                    block_with_state("purple"),
                    "gui/502/com.sidecrab.toast = {\n\tpid = 4057\n}\n",
                    "\x00\x01 not launchctl output at all\n",
                    "state = running\n"):            # no leading tab: not first-level
            with self.subTest(stdout=out[:24]):
                self.assertEqual(self.status(0, out), "unknown")


class FakeLaunchctl:
    """One canned (code, out, err) per target, or an exception to raise. Records every
    call, because a component with no service must produce NO call at all."""

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


class LaunchctlRunnerFailureTests(unittest.TestCase):
    """A runner that does not answer is `unknown`, whatever way it fails. FleetReader
    already owns this; pinned here against the Darwin platform because a mapping that
    ran before the catch, or a platform whose query raised something outside the catch
    list, would be a crash on the fleet thread rather than an honest gap."""

    def status(self, blows_up):
        runner = FakeLaunchctl({"com.sidecrab.toast": blows_up})
        reader = crabd.FleetReader(runner=runner, platform=crabd.DarwinPlatform())
        return reader.status("com.sidecrab.toast")

    def test_a_query_that_never_returns_is_unknown(self):
        self.assertEqual(
            self.status(subprocess.TimeoutExpired("launchctl", 10)), "unknown")

    def test_no_launchctl_on_the_box_is_unknown(self):
        self.assertEqual(self.status(FileNotFoundError(2, "no launchctl")), "unknown")

    def test_a_refused_spawn_is_unknown(self):
        self.assertEqual(self.status(PermissionError(13, "denied")), "unknown")


class LaunchctlQueryTests(unittest.TestCase):
    """What crabd actually runs, and the one component it does not run anything for."""

    @unittest.skipIf(sys.platform == "win32", "os.getuid is POSIX")
    def test_the_command_is_launchctl_print_in_this_users_gui_domain(self):
        """Pinned by argv rather than by outcome. `gui/<uid>` is the per-user domain the
        SideCrab agents are loaded into; `system/` would be the wrong domain and
        `launchctl list` a different, older answer shape.

        `crabd.subprocess` IS the stdlib module, so a plain attribute swap here patches
        `subprocess.run` for the whole process until the cleanup runs. mock.patch scopes
        it to the `with` and restores it even if an assertion raises inside.
        """
        seen = {}

        class Recorder:
            returncode = 0
            stdout = RUNNING_BLOCK.encode()
            stderr = b""

        def fake_run(argv, **kwargs):
            seen["argv"], seen["kwargs"] = argv, kwargs
            return Recorder()

        with mock.patch("crabd.subprocess.run", new=fake_run):
            result = crabd.DarwinPlatform().service_query("com.sidecrab.toast", 10)
        self.assertEqual(
            seen["argv"],
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/com.sidecrab.toast"])
        self.assertEqual(seen["kwargs"]["timeout"], 10)
        self.assertIs(seen["kwargs"]["check"], False)
        self.assertIs(seen["kwargs"]["capture_output"], True)
        self.assertEqual(result, (0, RUNNING_BLOCK, ""))

    def test_a_host_with_no_getuid_is_unknown_rather_than_a_crash(self):
        """`os.getuid` is POSIX-only and it is read OUTSIDE the subprocess call, so on a
        host without it the AttributeError comes out of service_query itself - past
        FleetReader's catch list (TimeoutExpired, OSError, ValueError) and into
        `_fleet_loop`, which is a crash on the fleet thread where an honest `unknown`
        belongs.

        DarwinPlatform is never SELECTED on such a host, but the seam lets anything build
        one and this suite does exactly that. Raising it as OSError puts it back in the
        shape the reader already answers - and it is the same shape NullPlatform's
        service_query uses to say it has no service manager.

        Patched by side effect rather than by deleting the attribute: a real Windows host
        raises at the lookup and this raises at the call, which no caller can tell apart,
        and deleting `os.getuid` process-wide would be visible to every other thread.
        """
        with mock.patch("crabd.os.getuid",
                        side_effect=AttributeError("module 'os' has no getuid")):
            with self.assertRaises(OSError):
                crabd.DarwinPlatform().service_query("com.sidecrab.toast", 10)
            reader = crabd.FleetReader(platform=crabd.DarwinPlatform())
            self.assertEqual(reader.status("com.sidecrab.toast"), "unknown")
            reader.poll(0.0)
            self.assertEqual(reader.get(), {"glow": "absent", "toast": "unknown"})

    def test_a_component_with_no_service_is_answered_without_spawning_anything(self):
        """The sentinel pair. An empty target means this platform has no service for
        that component at all - `launchctl print gui/502/` is a different question with
        a different answer, so nothing is spawned and the platform is handed a shape it
        recognises."""
        self.assertEqual(crabd.DarwinPlatform().service_query("", 10),
                         crabd.FLEET_NO_SERVICE)
        self.assertEqual(crabd.FLEET_NO_SERVICE, (None, "", ""))
        self.assertEqual(crabd.DarwinPlatform().service_status(*crabd.FLEET_NO_SERVICE),
                         "absent")

    @unittest.skipUnless(sys.platform == "darwin", "a real launchctl")
    def test_a_label_that_is_not_loaded_really_does_read_as_absent(self):
        """The live half: a real `launchctl print` for a label nothing has ever
        registered. This is the one test that would catch macOS changing the wording
        the absent marker matches on."""
        platform = crabd.DarwinPlatform()
        code, out, err = platform.service_query(
            "com.sidecrab.nonexistent.fleet.test", crabd.FLEET_TIMEOUT_SEC)
        self.assertNotEqual(code, 0)
        self.assertIn("Could not find service", err)
        self.assertEqual(platform.service_status(code, out, err), "absent")


class DarwinFleetTargetsTests(unittest.TestCase):
    """`fleet.glow` is served as `absent` on macOS, and that is not a workaround.

    There is no lighting component on a Mac - the Corsair SDK is Windows-only - so there
    is nothing to observe and `absent` is the literally true word for it. The KEY stays
    so the document's shape is identical on both platforms: a widget feature-detecting
    `fleet` renders a hollow absent dot rather than a missing row, and its rendering of
    `absent` is unchanged.
    """

    def test_glow_has_no_launchd_label_and_toast_does(self):
        self.assertEqual(crabd.DarwinPlatform().fleet_targets(),
                         (("glow", ""), ("toast", "com.sidecrab.toast")))

    def test_the_served_keys_are_the_same_two_on_every_platform(self):
        for platform in (crabd.WindowsPlatform(), crabd.DarwinPlatform(),
                         crabd.NullPlatform()):
            with self.subTest(platform=platform.name):
                self.assertEqual([name for name, _ in platform.fleet_targets()],
                                 ["glow", "toast"])

    def test_glow_is_absent_and_nothing_was_ever_asked_about_it(self):
        runner = FakeLaunchctl({"com.sidecrab.toast": (0, RUNNING_BLOCK, "")})
        fleet = crabd.FleetReader(runner=runner, platform=crabd.DarwinPlatform())
        fleet.poll(0.0)
        self.assertEqual(fleet.get(), {"glow": "absent", "toast": "running"})
        # Not "the fake happened to answer absent": it was never called at all.
        self.assertEqual([target for target, _timeout in runner.calls],
                         ["com.sidecrab.toast"])

    def test_the_toast_agents_own_state_is_what_is_served(self):
        for block, expected in ((RUNNING_BLOCK, "running"),
                                (NOT_RUNNING_BLOCK, "stopped"),
                                (None, "absent")):
            with self.subTest(expected=expected):
                answer = (113, "", ABSENT_STDERR) if block is None else (0, block, "")
                fleet = crabd.FleetReader(
                    runner=FakeLaunchctl({"com.sidecrab.toast": answer}),
                    platform=crabd.DarwinPlatform())
                fleet.poll(0.0)
                self.assertEqual(fleet.get(), {"glow": "absent", "toast": expected})

    def test_a_builder_with_no_reader_still_names_both_components_unknown(self):
        """`FleetReader.unknown()` takes its keys from the platform, and a builder with
        no reader attached must carry the same key set a reader would - with `unknown`,
        not `absent`: not having asked yet is not the same as having found nothing."""
        self.assertEqual(crabd.FleetReader.unknown(crabd.DarwinPlatform()),
                         {"glow": "unknown", "toast": "unknown"})


# ------------------------------------------------------- off the request path, served

class StubLimits:
    def get(self, now, force=False):
        return {"available": False, "note": "stub", "fiveHour": None, "weekly": None,
                "extra": [], "subscriptionType": None, "rateLimitTier": None}


class TempProjects(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.projects = Path(self._tmp.name) / "projects"
        self.projects.mkdir(parents=True)
        original = crabd.USER_CONFIG_FILE
        crabd.USER_CONFIG_FILE = Path(self._tmp.name) / "config.json"
        self.addCleanup(lambda: setattr(crabd, "USER_CONFIG_FILE", original))

    def builder_with(self, fleet):
        return crabd.StateBuilder(
            crabd.TranscriptStore(self.projects), crabd.HookTracker(), StubLimits(),
            time.time(), None, None, fleet)


class DarwinFleetOffTheRequestPathTests(TempProjects):
    """`fleet` is computed on its own thread and READ from cache, and launchctl is a
    SUBPROCESS. A build that shelled it would put a spawn in the request path and stall
    `generatedAt` - and a wedged launchctl would stall it for the timeout, every pass."""

    def blocking_fleet(self):
        """(the reader, the targets it was asked about, the gate holding its runner).

        The runner parks until the gate is set, which is a launchctl that has not
        answered yet - the state a real one is in for up to FLEET_TIMEOUT_SEC.
        """
        gate = threading.Event()
        self.addCleanup(gate.set)          # never leave a poll thread parked
        calls = []

        def blocking(target, timeout):
            calls.append(target)
            gate.wait(10)
            return (0, RUNNING_BLOCK, "")

        return (crabd.FleetReader(runner=blocking, platform=crabd.DarwinPlatform()),
                calls, gate)

    def test_a_build_spawns_nothing_however_long_the_query_would_take(self):
        """The claim is not "a build is fast", it is "a build does not ask": the empty
        call list is the whole proof, and it holds however slow the query would have
        been. `fleet` is read from cache, so three builds against a launchctl that never
        answers produce three documents and no queries at all."""
        fleet, calls, _gate = self.blocking_fleet()
        builder = self.builder_with(fleet)
        for _ in range(3):
            state = builder.build()
        self.assertEqual(calls, [])
        self.assertEqual(state["fleet"], {"glow": "unknown", "toast": "unknown"})

    def test_the_fleets_own_poll_is_what_fills_it_in(self):
        """The other half: the reading exists, it is just not the builder's to take."""
        fleet, calls, gate = self.blocking_fleet()
        builder = self.builder_with(fleet)
        poller = threading.Thread(target=fleet.poll, args=(time.time(),), daemon=True)
        poller.start()
        gate.set()
        poller.join(timeout=10)
        self.assertFalse(poller.is_alive(), "the fleet poll should have finished")
        self.assertEqual(calls, ["com.sidecrab.toast"])
        self.assertEqual(builder.build()["fleet"],
                         {"glow": "absent", "toast": "running"})

    def test_a_wedged_launchctl_leaves_the_last_reading_standing(self):
        """_fleet_loop swallows; this proves the poll is where the blast stops, so a
        timed-out launchctl gives `unknown` for that component and nothing else."""
        fleet = crabd.FleetReader(
            runner=FakeLaunchctl({"com.sidecrab.toast":
                                  subprocess.TimeoutExpired("launchctl", 10)}),
            platform=crabd.DarwinPlatform())
        fleet.poll(time.time())
        self.assertEqual(fleet.get(), {"glow": "absent", "toast": "unknown"})


class DarwinFleetServedOverASocket(TempProjects):
    """`fleet` on the wire, from a real crabd on a test port. The runner is faked - this
    is about what the DOCUMENT carries, not about this machine's own agents."""

    def setUp(self):
        super().setUp()
        self.builder = self.builder_with(crabd.FleetReader(
            runner=FakeLaunchctl({"com.sidecrab.toast": (0, NOT_RUNNING_BLOCK, "")}),
            platform=crabd.DarwinPlatform()))
        self.builder.fleet.poll(time.time())
        with self.builder._lock:
            self.builder._state = self.builder.build()
        original = crabd.Handler.builder
        self.addCleanup(lambda: setattr(crabd.Handler, "builder", original))
        crabd.Handler.builder = self.builder
        self.server, self.thread, self.port, self.client = start_test_server(
            lambda: crabd.CrabdServer(("127.0.0.1", 0), crabd.Handler))
        self.addCleanup(self.client.close)
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def test_the_served_document_carries_glow_absent_and_the_toast_agents_state(self):
        state = self.client.get("/v1/state").json()
        self.assertEqual(state["fleet"], {"glow": "absent", "toast": "stopped"})
        self.assertEqual(sorted(state["fleet"]), ["glow", "toast"])
        self.assertEqual(state["schema"], 5)      # nothing about the shape moved

    def test_the_block_survives_the_json_round_trip_unchanged(self):
        self.assertEqual(json.loads(crabd.dump_state(self.builder.build()))["fleet"],
                         {"glow": "absent", "toast": "stopped"})


if __name__ == "__main__":
    unittest.main()
