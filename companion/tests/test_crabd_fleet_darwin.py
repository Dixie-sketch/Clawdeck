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
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

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


def tearDownModule():
    (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE, crabd.HISTORY_FILE,
     crabd.CREDENTIALS_FILE, crabd.LIMITS_TOKEN_FILE) = setUpModule.originals
    _MODULE_TMP.cleanup()


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


if __name__ == "__main__":
    unittest.main()
