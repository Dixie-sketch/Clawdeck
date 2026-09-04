"""The LaunchAgents: the plists, the load sequence, and the restart that refuses to guess.

launchctl, lsof and every HTTP read go through the Environment seam, so nothing here
loads an agent, opens a socket or touches ~/Library/LaunchAgents outside a temp HOME.
"""

from __future__ import annotations

import plistlib
import unittest

from _harness import RecordingHttp, RecordingRunner, TempHome, setup

#: Measured shape of launchctl's answer for a label that is not loaded.
ABSENT = (113, "", 'Could not find service "com.sidecrab.crabd" in domain for user gui: 501\n')

RUNNING = (
    0,
    "com.sidecrab.crabd = {\n\tactive count = 1\n\tstate = running\n\tpid = 4242\n}\n",
    "",
)


class PlistDocument(TempHome):
    def test_every_key_the_agent_needs_and_nothing_it_does_not(self):
        spec = setup.agent_spec("crabd")
        doc = setup.plist_document(spec, "/fake/bin/python3.13", self.repo, self.home / ".sidecrab" / "logs")
        self.assertEqual(doc["Label"], "com.sidecrab.crabd")
        self.assertEqual(
            doc["ProgramArguments"],
            ["/fake/bin/python3.13", str(self.repo / "companion" / "crabd.py")],
        )
        self.assertEqual(doc["WorkingDirectory"], str(self.repo))
        self.assertIs(doc["RunAtLoad"], True)
        self.assertIs(doc["KeepAlive"], True)
        self.assertEqual(doc["StandardOutPath"], str(self.home / ".sidecrab/logs/com.sidecrab.crabd.log"))
        self.assertEqual(doc["StandardErrorPath"], doc["StandardOutPath"])
        self.assertEqual(doc["EnvironmentVariables"], {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"})
        # No ProcessType: whether App Nap touches a KeepAlive agent is unmeasured, and a
        # guessed value here would be a claim this repo has not earned.
        self.assertNotIn("ProcessType", doc)

    def test_the_toast_agent_names_the_notifier(self):
        doc = setup.plist_document(
            setup.agent_spec("toast"), "/fake/py", self.repo, self.home / "logs"
        )
        self.assertEqual(doc["Label"], "com.sidecrab.toast")
        self.assertEqual(doc["ProgramArguments"][1], str(self.repo / "notifier" / "sidecrab_toast.py"))


class ParseDisabled(unittest.TestCase):
    def test_only_the_labels_marked_true_are_disabled(self):
        text = (
            "disabled services = {\n"
            '\t"com.sidecrab.crabd" => true\n'
            '\t"com.sidecrab.toast" => false\n'
            '\t"com.other" => true\n'
            "}\n"
        )
        self.assertEqual(setup.parse_disabled(text), {"com.sidecrab.crabd", "com.other"})

    def test_an_empty_or_unreadable_answer_disables_nothing(self):
        self.assertEqual(setup.parse_disabled(""), set())


class ParseAgentState(unittest.TestCase):
    def test_a_running_agent_reports_its_pid(self):
        state = setup.parse_agent_state("com.sidecrab.crabd", *RUNNING)
        self.assertTrue(state.loaded)
        self.assertEqual(state.pid, 4242)
        self.assertEqual(state.state, "running")

    def test_the_measured_absent_answer_is_a_state_not_an_error(self):
        state = setup.parse_agent_state("com.sidecrab.crabd", *ABSENT)
        self.assertFalse(state.loaded)
        self.assertIsNone(state.pid)
        self.assertEqual(state.state, "absent")

    def test_a_loaded_but_stopped_agent_is_not_running(self):
        out = "com.sidecrab.crabd = {\n\tstate = not running\n\tlast exit code = 1\n}\n"
        state = setup.parse_agent_state("com.sidecrab.crabd", 0, out, "")
        self.assertTrue(state.loaded)
        self.assertEqual(state.state, "not running")
        self.assertIsNone(state.pid)


class LoadAgents(TempHome):
    def install(self, *args, **kwargs):
        return setup.main(["install", "--yes", *args], env=self.env(**kwargs))

    def plist(self, label="com.sidecrab.crabd"):
        return self.home / "Library" / "LaunchAgents" / f"{label}.plist"

    def test_bootout_precedes_bootstrap_and_the_plist_is_written_first(self):
        self.install()
        self.assertTrue(self.plist().exists())
        verbs = [c[1] for c in self.runner.calls if c[0] == "launchctl"]
        self.assertEqual(verbs.count("bootout"), 1)
        self.assertEqual(verbs.count("bootstrap"), 1)
        self.assertLess(verbs.index("bootout"), verbs.index("bootstrap"))
        boot = self.runner.argv_for("bootstrap")[0]
        self.assertEqual(boot, ["launchctl", "bootstrap", "gui/501", str(self.plist())])

    def test_the_log_directory_is_created_private(self):
        self.install()
        logs = self.home / ".sidecrab" / "logs"
        self.assertTrue(logs.is_dir())
        self.assertEqual(logs.stat().st_mode & 0o777, 0o700)

    def test_the_toast_agent_is_only_loaded_when_asked_for(self):
        self.install()
        self.assertFalse(self.plist("com.sidecrab.toast").exists())
        self.assertEqual(self.runner.argv_for("com.sidecrab.toast"), [])

        self.runner = RecordingRunner()
        self.install("--with-toast")
        self.assertTrue(self.plist("com.sidecrab.toast").exists())

    def test_a_second_install_writes_the_same_bytes_and_makes_the_same_calls(self):
        self.install()
        first_bytes = self.plist().read_bytes()
        first_calls = list(self.runner.calls)

        self.runner = RecordingRunner()
        self.install()
        self.assertEqual(self.plist().read_bytes(), first_bytes)
        self.assertEqual(self.runner.calls, first_calls)
        self.assertEqual(len(list(self.plist().parent.iterdir())), 1)

    def test_a_disabled_label_is_re_registered_and_left_alone(self):
        runner = RecordingRunner(
            {"print-disabled": (0, 'disabled services = {\n\t"com.sidecrab.crabd" => true\n}\n', "")}
        )
        self.runner = runner
        self.install()

        self.assertTrue(self.plist().exists())
        self.assertEqual(runner.argv_for("bootstrap"), [])
        self.assertEqual(runner.argv_for("enable"), [])
        self.assertIn("DISABLED", self.output)
        self.assertIn("--force-enable", self.output)

    def test_force_enable_is_the_only_override(self):
        runner = RecordingRunner(
            {"print-disabled": (0, 'disabled services = {\n\t"com.sidecrab.crabd" => true\n}\n', "")}
        )
        self.runner = runner
        self.install("--force-enable")

        enable = runner.argv_for("launchctl enable")
        self.assertEqual(enable, [["launchctl", "enable", "gui/501/com.sidecrab.crabd"]])
        verbs = [c[1] for c in runner.calls if c[0] == "launchctl"]
        self.assertLess(verbs.index("enable"), verbs.index("bootstrap"))


class EnableDecision(unittest.TestCase):
    def test_the_table(self):
        rows = [
            # registered, disabled, force -> action, start
            (False, False, False, "load", True),
            (True, False, False, "load", True),
            (True, True, False, "leave-disabled", False),
            (True, True, True, "force-enabled", True),
            # A label the operator disabled and then lost the plist for is still a no.
            (False, True, False, "leave-disabled", False),
        ]
        for registered, disabled, force, action, start in rows:
            with self.subTest(registered=registered, disabled=disabled, force=force):
                decision = setup.task_enable_decision(registered, disabled, force)
                self.assertEqual(decision.action, action)
                self.assertEqual(decision.changed, start)


class InstallRefusesAForeignHolder(TempHome):
    """A first install onto a machine where something else already holds 9999.

    KeepAlive is what makes this worse than update's case: crabd loses the bind race,
    exits, launchd restarts it, and it exits again - forever, in the log, serving nothing.
    """

    LSOF = "COMMAND  PID USER\nnode  777 someone   6u  IPv4 TCP 127.0.0.1:9999 (LISTEN)\n"

    def test_the_install_stops_before_bootstrapping_and_names_the_holder(self):
        runner = RecordingRunner({"print gui": ABSENT, "lsof": (0, self.LSOF, "")})
        self.runner = runner
        code = setup.main(["install", "--yes"], env=self.env())

        self.assertEqual(code, 1)
        self.assertEqual(runner.argv_for("bootstrap"), [])
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())
        self.assertIn("777", self.output)
        self.assertIn("node", self.output)

    def test_nothing_at_all_is_written_when_the_port_is_held(self):
        # The refusal is the FIRST thing install does. Merging hooks and taking the
        # status-line slot for an install that then refuses leaves the operator half
        # configured, with a crabd that was never loaded.
        path = self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"model": "opus"}', encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        runner = RecordingRunner({"print gui": ABSENT, "lsof": (0, self.LSOF, "")})
        self.runner = runner
        self.assertEqual(setup.main(["install", "--yes"], env=self.env()), 1)

        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertEqual(list(path.parent.glob("*.sidecrab-bak-*")), [])
        self.assertFalse((self.home / ".sidecrab").exists())

    def test_the_refusal_says_started_for_an_install_and_restarted_for_an_update(self):
        for command, verb in (("install", "started"), ("update", "restarted")):
            with self.subTest(command=command):
                self.printed.clear()
                self.runner = RecordingRunner({"print gui": ABSENT, "lsof": (0, self.LSOF, "")})
                args = [command, "--yes"] if command == "install" else [command]
                self.assertEqual(setup.main(args, env=self.env()), 1)
                self.assertIn(f"NOT {verb}", self.output)

    def test_our_own_running_agent_holding_the_port_is_not_a_refusal(self):
        lsof = "COMMAND  PID USER\nPython  4242 me   6u  IPv4 TCP 127.0.0.1:9999 (LISTEN)\n"
        runner = RecordingRunner(
            {
                "print gui": (0, "com.sidecrab.crabd = {\n\tstate = running\n\tpid = 4242\n}\n", ""),
                "lsof": (0, lsof, ""),
            }
        )
        self.runner = runner
        self.assertEqual(setup.main(["install", "--yes"], env=self.env()), 0)
        self.assertEqual(len(runner.argv_for("bootstrap")), 1)

    def test_a_free_port_installs_normally(self):
        runner = RecordingRunner({"print gui": ABSENT})
        self.runner = runner
        self.assertEqual(setup.main(["install", "--yes"], env=self.env()), 0)
        self.assertEqual(len(runner.argv_for("bootstrap")), 1)


class UnloadAgents(TempHome):
    def plist(self, label="com.sidecrab.crabd"):
        return self.home / "Library" / "LaunchAgents" / f"{label}.plist"

    def test_both_agents_are_booted_out_and_their_plists_removed(self):
        setup.main(["install", "--yes", "--with-toast"], env=self.env())
        self.runner = RecordingRunner()
        self.assertEqual(setup.main(["uninstall", "--yes"], env=self.env()), 0)

        self.assertFalse(self.plist().exists())
        self.assertFalse(self.plist("com.sidecrab.toast").exists())
        booted = [c[2] for c in self.runner.argv_for("bootout")]
        self.assertEqual(booted, ["gui/501/com.sidecrab.crabd", "gui/501/com.sidecrab.toast"])

    def test_an_agent_that_was_never_installed_is_not_an_error(self):
        self.assertEqual(setup.main(["uninstall", "--yes"], env=self.env()), 0)


class ServiceVerdict(unittest.TestCase):
    """Health alone cannot say WHO answered - the two readings together can."""

    def test_the_four_cases(self):
        holder = [setup.PortHolder(pid=999, command="node")]
        rows = [
            (True, "running", [], "ok", True),
            (True, "not running", holder, "foreign-answerer", False),
            (True, "absent", holder, "foreign-answerer", False),
            (False, "running", [], "not-answering", False),
            (False, "absent", [], "down", False),
        ]
        for health_ok, state, holders, expected, ok in rows:
            with self.subTest(health=health_ok, state=state):
                verdict = setup.service_verdict(health_ok, state, holders, setup.PORT)
                self.assertEqual(verdict.verdict, expected)
                self.assertEqual(verdict.ok, ok)

    def test_the_foreign_answerer_names_the_pid_and_the_command(self):
        verdict = setup.service_verdict(
            True, "not running", [setup.PortHolder(pid=4242, command="node")], setup.PORT
        )
        self.assertIn("4242", verdict.reason)
        self.assertIn("node", verdict.reason)


class PortHolders(unittest.TestCase):
    LSOF = (
        "COMMAND   PID    USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME\n"
        "Python  63212 rumeton    6u  IPv4 0x1234567890abcdef      0t0  TCP 127.0.0.1:9999 (LISTEN)\n"
    )

    def test_a_listener_is_read_off_lsof(self):
        holders = setup.parse_port_holders(self.LSOF)
        self.assertEqual(holders, [setup.PortHolder(pid=63212, command="Python")])

    def test_no_listener_is_a_state_not_an_error(self):
        # lsof exits 1 with no output when nothing matches.
        self.assertEqual(setup.parse_port_holders(""), [])


class UpdateRestart(TempHome):
    def setUp(self):
        super().setUp()
        (self.repo / "companion" / "crabd.py").write_text('VERSION = "0.31.0"\n', encoding="utf-8")

    def env_with(self, health_versions, runner=None):
        answers = iter(health_versions)

        def http_get(url, body=None, headers=None, timeout=None):
            try:
                version = next(answers)
            except StopIteration:
                version = health_versions[-1]
            if version is None:
                return (0, "connection refused")
            return (200, '{"ok": true, "version": "%s"}' % version)

        return self.env(http_get=http_get, run=runner or self.runner)

    def test_the_wait_ends_when_the_new_version_answers(self):
        runner = RecordingRunner({"print gui": RUNNING})
        self.runner = runner
        code = setup.main(["update"], env=self.env_with([None, "0.30.0", "0.31.0"], runner))
        self.assertEqual(code, 0)
        self.assertEqual(
            runner.argv_for("kickstart"),
            [["launchctl", "kickstart", "-k", "gui/501/com.sidecrab.crabd"]],
        )
        self.assertIn("0.31.0", self.output)

    def test_a_foreign_holder_stops_the_restart_and_names_the_pid(self):
        lsof = "COMMAND   PID USER\nnode  777 someone   6u  IPv4 TCP 127.0.0.1:9999 (LISTEN)\n"
        runner = RecordingRunner({"print gui": ABSENT, "lsof": (0, lsof, "")})
        self.runner = runner
        code = setup.main(["update"], env=self.env_with([None], runner))

        self.assertEqual(code, 1)
        self.assertEqual(runner.argv_for("kickstart"), [])
        self.assertIn("777", self.output)
        self.assertIn("node", self.output)

    def test_our_own_agent_holding_the_port_is_not_foreign(self):
        lsof = "COMMAND   PID USER\nPython  4242 me   6u  IPv4 TCP 127.0.0.1:9999 (LISTEN)\n"
        runner = RecordingRunner({"print gui": RUNNING, "lsof": (0, lsof, "")})
        self.runner = runner
        code = setup.main(["update"], env=self.env_with(["0.31.0"], runner))
        self.assertEqual(code, 0)
        self.assertEqual(len(runner.argv_for("kickstart")), 1)

    def test_a_version_that_never_arrives_is_reported_not_claimed(self):
        runner = RecordingRunner({"print gui": RUNNING})
        self.runner = runner
        code = setup.main(["update"], env=self.env_with(["0.30.0"], runner))
        self.assertEqual(code, 1)
        self.assertIn("0.30.0", self.output)
        self.assertIn("0.31.0", self.output)


if __name__ == "__main__":
    unittest.main()
