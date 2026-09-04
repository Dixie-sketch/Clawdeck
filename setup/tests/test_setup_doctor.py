"""The read-only commands: pairing-code, limits-token, status and doctor.

Everything they touch - HTTP, launchctl, the Keychain, stdin - is injected, so the whole
file runs offline against a temporary HOME.
"""

from __future__ import annotations

import json
import unittest
from datetime import timedelta

from _harness import REPO_ROOT, RecordingHttp, RecordingRunner, TempHome, setup


class PairingCode(TempHome):
    def write_token(self, raw):
        path = self.home / ".sidecrab" / "panel-token"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        return path

    def test_a_valid_code_is_printed_grouped_with_where_to_enter_it(self):
        self.write_token("ABCDE23456\n")
        self.assertEqual(setup.main(["pairing-code"], env=self.env()), 0)
        self.assertIn("ABCDE-23456", self.output)
        self.assertIn("localhost:9999", self.output)

    def test_an_absent_token_says_to_start_crabd_first(self):
        code = setup.main(["pairing-code"], env=self.env())
        self.assertEqual(code, 1)
        self.assertIn("start crabd", self.output.lower())

    def test_a_code_outside_the_alphabet_is_refused_rather_than_printed(self):
        # I, L, O and U are not in the alphabet crabd mints from: a code carrying one
        # is not a code, and printing it would send the operator to type a wrong thing.
        for raw in ("ABCDEILOU1", "SHORT", "abcde-2345-6789-0"):
            with self.subTest(raw=raw):
                self.write_token(raw)
                self.printed.clear()
                self.assertEqual(setup.main(["pairing-code"], env=self.env()), 1)
                self.assertNotIn(raw, self.output)


class LimitsToken(TempHome):
    TOKEN = "sk-ant-oat01-" + "a" * 30

    def run_with(self, secret, store=None, **kwargs):
        stored = []

        def default_store(token):
            stored.append(token)
            return True

        if store is None and "store_capable" not in kwargs:
            store = default_store
        env = self.env(read_secret=lambda prompt: secret, store_token=store, **kwargs)
        return setup.main(["limits-token"], env=env), stored

    def test_a_good_token_reaches_the_store_and_never_the_output(self):
        code, stored = self.run_with(self.TOKEN)
        self.assertEqual(code, 0)
        self.assertEqual(stored, [self.TOKEN])
        self.assertNotIn(self.TOKEN, self.output)
        self.assertNotIn(self.TOKEN[8:], self.output)

    def test_the_shape_is_checked_before_anything_is_stored(self):
        rows = [
            ("", "empty"),
            ("hello there", "sk-ant-"),
            ("sk-ant-short", "20"),
            ("sk-ant-" + "!" * 40, "characters"),
        ]
        for secret, expected in rows:
            with self.subTest(secret=secret[:12]):
                self.printed.clear()
                code, stored = self.run_with(secret)
                self.assertEqual(code, 1)
                self.assertEqual(stored, [])
                self.assertIn(expected, self.output)

    def test_an_older_crabd_without_the_store_fails_honestly(self):
        code, stored = self.run_with(self.TOKEN, store=None, store_capable=lambda: False)
        self.assertEqual(code, 1)
        self.assertEqual(stored, [])
        self.assertIn("store_limits_token", self.output)
        self.assertIn("crabd", self.output)

    def test_the_capability_is_asked_before_the_token_is_handed_over(self):
        # Not by catching AttributeError out of a working call: a store that raises
        # AttributeError from somewhere INSIDE itself would be reported as "your crabd
        # is too old", and the token may already have been written by then.
        def exploding_store(token):
            raise AttributeError("something deep inside the keychain wrapper")

        code, _ = self.run_with(self.TOKEN, store=exploding_store, store_capable=lambda: True)
        self.assertEqual(code, 1)
        self.assertNotIn("Update crabd", self.output)

    def test_a_store_that_puts_the_token_in_its_exception_does_not_print_it(self):
        def leaky_store(token):
            raise RuntimeError(f"keychain refused item with password {token}")

        code, _ = self.run_with(self.TOKEN, store=leaky_store)
        self.assertEqual(code, 1)
        self.assertNotIn(self.TOKEN, self.output)
        self.assertIn("RuntimeError", self.output)

    def test_the_token_is_read_from_stdin_and_never_from_argv(self):
        # The wrapper takes no token argument at all: there is nothing for `ps` to see.
        parser = setup.build_parser()
        args = parser.parse_args(["limits-token"])
        self.assertEqual(vars(args), {"command": "limits-token"})


RUNNING = (0, "com.sidecrab.crabd = {\n\tstate = running\n\tpid = 4242\n}\n", "")
ABSENT = (113, "", 'Could not find service "x" in domain for user gui: 501\n')


class Status(TempHome):
    def status(self, **kwargs):
        return setup.main(["status"], env=self.env(**kwargs))

    def test_it_writes_nothing_and_starts_nothing(self):
        runner = RecordingRunner({"print gui": ABSENT})
        self.assertEqual(self.status(run=runner), 0)
        # No settings.json, no config.json, no plist, no chain file.
        self.assertEqual(list(self.home.rglob("*")), [])
        for verb in ("bootstrap", "bootout", "kickstart", "enable"):
            self.assertEqual(runner.argv_for(f"launchctl {verb}"), [], verb)

    def test_the_agent_rows_report_loaded_running_disabled_and_absent(self):
        runner = RecordingRunner(
            {
                "print gui/501/com.sidecrab.crabd": RUNNING,
                "print gui/501/com.sidecrab.toast": ABSENT,
                "print-disabled": (0, 'disabled services = {\n\t"com.sidecrab.toast" => true\n}\n', ""),
            }
        )
        self.status(run=runner)
        self.assertIn("running, pid 4242", self.output)
        self.assertIn("DISABLED", self.output)

    def test_the_hook_row_counts_our_entries_out_of_seven(self):
        setup.main(["install", "--yes"], env=self.env())
        self.printed.clear()
        self.status()
        self.assertIn("7 of 7", self.output)

    def test_the_status_line_row_names_what_it_chains_to(self):
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".claude" / "settings.json").write_text(
            json.dumps({"statusLine": {"type": "command", "command": "starship prompt"}}),
            encoding="utf-8",
        )
        setup.main(["install", "--yes"], env=self.env())
        self.printed.clear()
        self.status()
        self.assertIn("chained to", self.output)
        self.assertIn("starship prompt", self.output)

    def test_the_allow_list_row_tells_the_states_apart(self):
        rows = [
            ("absent", "unset"),
            (None, "unset"),
            (["http://example/*"], "does NOT admit"),
            (list(setup.ALLOWED_HOOK_PATTERNS), "admits crabd"),
            ("http://example/*", "not a list"),
            ({"a": 1}, "not a list"),
        ]
        for patterns, expected in rows:
            with self.subTest(patterns=patterns):
                doc = {} if patterns == "absent" else {"allowedHttpHookUrls": patterns}
                (self.home / ".claude").mkdir(parents=True, exist_ok=True)
                (self.home / ".claude" / "settings.json").write_text(
                    json.dumps(doc), encoding="utf-8"
                )
                self.printed.clear()
                self.status()
                self.assertIn(expected, self.output)

    def test_the_pairing_code_is_reported_present_but_never_printed(self):
        (self.home / ".sidecrab").mkdir(parents=True)
        (self.home / ".sidecrab" / "panel-token").write_text("ABCDE23456", encoding="utf-8")
        self.status()
        self.assertIn("present", self.output)
        self.assertNotIn("ABCDE", self.output)

    def test_the_limits_token_is_probed_by_exit_code_and_never_read(self):
        runner = RecordingRunner({"find-generic-password": (0, "", "")})
        self.status(run=runner)
        probe = runner.argv_for("find-generic-password")[0]
        self.assertEqual(
            probe,
            ["security", "find-generic-password", "-s", "SideCrab limits token", "-a", "tester"],
        )
        # -w would print the secret; the exit code is the whole answer.
        self.assertNotIn("-w", probe)
        self.assertIn("stored", self.output)

    def test_an_absent_limits_token_is_a_state(self):
        runner = RecordingRunner({"find-generic-password": (44, "", "not found")})
        self.status(run=runner, store_capable=lambda: True)
        self.assertIn("none stored", self.output)

    def test_a_crabd_without_the_store_says_so_rather_than_none_stored(self):
        # "none stored" would send the operator to run --limits-token, which cannot work
        # yet. The row has to name the reason, not the symptom.
        runner = RecordingRunner({"find-generic-password": (44, "", "not found")})
        self.status(run=runner, store_capable=lambda: False)
        self.assertIn("not supported by this crabd", self.output)
        self.assertNotIn("none stored", self.output)

    def test_an_answer_with_no_running_agent_is_named_as_the_loudest_row(self):
        # Health alone cannot say WHO answered. A stray process holding 9999 answers
        # convincingly while the agent is dead - and is also what stops it binding.
        lsof = "COMMAND  PID USER\nnode  777 someone   6u  IPv4 TCP 127.0.0.1:9999 (LISTEN)\n"
        runner = RecordingRunner({"print gui": ABSENT, "lsof": (0, lsof, "")})
        answering = RecordingHttp({"/v1/health": (200, '{"ok": true, "version": "0.31.0"}')})
        self.status(run=runner, http_get=answering)
        self.assertIn("foreign", self.output)
        self.assertIn("777", self.output)
        self.assertIn("node", self.output)

    def test_the_python_row_names_the_version_as_well_as_the_path(self):
        # The path alone is not the version: /fake/bin/python3.13 could be a 3.9 that
        # somebody renamed, which is precisely what the probe exists to catch.
        self.status(python_probe=lambda path: (0, "3.14\n", ""))
        self.assertIn(f"{self.FAKE_PYTHON} (3.14)", self.output)

    def test_the_panel_url_is_the_last_word(self):
        self.status()
        self.assertIn(setup.PANEL_URL, self.output)


class DoctorCommand(TempHome):
    def test_the_command_prints_the_table_and_exits_on_a_failing_row(self):
        env = self.env(run=RecordingRunner({"print gui": ABSENT}))
        self.assertEqual(setup.main(["doctor"], env=env), 1)
        self.assertIn("FAIL", self.output)
        self.assertIn("agent crabd", self.output)


class FakeCrabd:
    """A crabd made of canned answers, with a real hook round trip.

    The hook POSTs mutate `sessions`, so the doctor's "the row appeared, then it went"
    assertions are a genuine cycle rather than three unrelated stubs.
    """

    def __init__(self, clock, version="0.31.0"):
        self.clock = clock
        self.version = version
        self.schema = 5
        self.sessions = {}
        self.lag_sec = 0
        self.panel_body = "<html><head><title>SideCrab</title></head></html>"
        self.panel_status = 200
        self.health_fails = 0
        self.unheadered_status = 403
        self.state_status = 200

    def _state(self):
        generated = self.clock() - timedelta(seconds=self.lag_sec)
        return json.dumps(
            {
                "schema": self.schema,
                "generatedAt": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "crabd": {"version": self.version},
                "limits": {"available": True, "tokenSource": "cli"},
                "burn": {},
                "sessions": [
                    {"id": sid, "state": state} for sid, state in self.sessions.items()
                ],
            }
        )

    def get(self, url, body=None, headers=None, timeout=None):
        if url.endswith("/v1/health"):
            if self.health_fails > 0:
                self.health_fails -= 1
                return (0, "connection refused")
            return (200, json.dumps({"ok": True, "version": self.version}))
        if url.endswith("/v1/state"):
            return (self.state_status, self._state() if self.state_status == 200 else "")
        if url.rstrip("/").endswith(str(setup.PORT)):
            return (self.panel_status, self.panel_body)
        return (404, "")

    def post(self, url, body=None, headers=None, timeout=None):
        if not (headers or {}).get("X-SideCrab-Panel"):
            return (self.unheadered_status, "panel header required")
        payload = json.loads(body)
        event, sid = payload["hook_event_name"], payload["session_id"]
        if event == "SessionStart":
            self.sessions[sid] = "idle"
        elif event == "Notification":
            self.sessions[sid] = "needs_input"
        elif event == "SessionEnd":
            self.sessions.pop(sid, None)
        return (204, "")


class PanelIdentity(unittest.TestCase):
    def test_the_shipping_panel_satisfies_the_row(self):
        # crabd serves widget/index.html on /. Pinned against the real file so a retitle
        # breaks a test here rather than the doctor on somebody's machine.
        index = (REPO_ROOT / "widget" / "index.html").read_text(encoding="utf-8")
        self.assertTrue(setup.looks_like_the_panel(index), index[:200])

    def test_something_else_on_the_port_does_not(self):
        for body in ("<html>nginx</html>", "", "<title>Grafana</title>", None):
            with self.subTest(body=body):
                self.assertFalse(setup.looks_like_the_panel(body))


class AgentStateFirstLevelOnly(unittest.TestCase):
    def test_a_nested_state_line_does_not_win(self):
        # launchctl print nests sub-objects that carry their own `state = active`;
        # only the first-level, single-tab line describes the agent.
        out = (
            "com.sidecrab.crabd = {\n"
            "\tstate = not running\n"
            "\tendpoints = {\n"
            "\t\t\"x\" = {\n"
            "\t\t\tstate = active\n"
            "\t\t}\n"
            "\t}\n"
            "}\n"
        )
        self.assertEqual(setup.parse_agent_state("com.sidecrab.crabd", 0, out, "").state, "not running")


class Doctor(TempHome):
    def setUp(self):
        super().setUp()
        (self.repo / "widget" / "scripts" / "sidecrab.js").write_text(
            "var SCHEMA_MAX = 5;\n", encoding="utf-8"
        )
        (self.repo / "notifier" / "sidecrab_toast.py").write_text(
            "SUPPORTED_SCHEMAS = frozenset({1, 2, 3, 4, 5})\n", encoding="utf-8"
        )
        self.crabd = FakeCrabd(lambda: self.clock)
        self.crabd.panel_body = (REPO_ROOT / "widget" / "index.html").read_text(encoding="utf-8")
        setup.main(["install", "--yes"], env=self.env())
        self.printed.clear()

    def doctor(self, runner=None, **kwargs):
        self.runner = runner or RecordingRunner({"print gui": RUNNING})
        self.doctor_env = self.env(
            run=self.runner, http_get=self.crabd.get, http_post=self.crabd.post, **kwargs
        )
        rows = setup.run_doctor(self.doctor_env)
        self.rows = {row.check: row for row in rows}
        return setup.doctor_exit_code(rows)

    def verdicts(self):
        return {check: row.verdict for check, row in self.rows.items()}

    def test_a_healthy_install_passes_every_row_it_runs(self):
        self.assertEqual(self.doctor(), 0)
        self.assertNotIn("FAIL", self.verdicts().values())
        for check in (
            "agent crabd",
            "python",
            "health",
            "state reachable",
            "state schema",
            "state freshness",
            "panel",
            "panel schema",
            "hook header",
            "hook SessionStart",
            "hook SessionEnd",
            "hook cycle",
            "config.json",
            "statusline chain",
            "limits token",
            "panel approvals",
        ):
            self.assertEqual(self.rows[check].verdict, "PASS", check)

    def test_the_row_order_is_the_documented_one(self):
        self.doctor()
        self.assertEqual(
            list(self.rows)[:8],
            [
                "agent crabd",
                "agent toast",
                "python",
                "health",
                "state reachable",
                "state schema",
                "state freshness",
                "panel",
            ],
        )

    def test_the_toast_rows_skip_when_the_notifier_is_not_installed(self):
        self.doctor()
        self.assertEqual(self.rows["agent toast"].verdict, "SKIP")
        self.assertEqual(self.rows["notifier schema"].verdict, "SKIP")

    def test_the_toast_rows_are_judged_once_the_notifier_is_installed(self):
        setup.main(["install", "--yes", "--with-toast"], env=self.env())
        runner = RecordingRunner({"print gui": RUNNING})
        self.doctor(runner)
        self.assertEqual(self.rows["agent toast"].verdict, "PASS")
        self.assertEqual(self.rows["notifier schema"].verdict, "PASS")

        # The bug class this row exists for: crabd moves on and the notifier does not,
        # so it keeps running, keeps polling and never toasts again.
        self.crabd.schema = 5
        (self.repo / "notifier" / "sidecrab_toast.py").write_text(
            "SUPPORTED_SCHEMAS = frozenset({1, 2, 3})\n", encoding="utf-8"
        )
        self.assertEqual(self.doctor(RecordingRunner({"print gui": ABSENT})), 1)
        self.assertEqual(self.rows["agent toast"].verdict, "FAIL")
        self.assertEqual(self.rows["notifier schema"].verdict, "FAIL")
        self.assertIn("never toast", self.rows["notifier schema"].detail)

    def test_health_reports_that_it_needed_the_retry(self):
        self.crabd.health_fails = 1
        self.assertEqual(self.doctor(), 0)
        self.assertEqual(self.rows["health"].verdict, "PASS")
        self.assertIn("recovered on retry", self.rows["health"].detail.lower())

    def test_a_schema_the_panel_cannot_read_fails_two_rows(self):
        self.crabd.schema = 9
        self.assertEqual(self.doctor(), 1)
        self.assertEqual(self.rows["state schema"].verdict, "FAIL")
        self.assertEqual(self.rows["panel schema"].verdict, "FAIL")
        self.assertIn("5", self.rows["panel schema"].detail)

    def test_a_stale_feed_fails_freshness_at_the_same_threshold_the_panel_uses(self):
        self.crabd.lag_sec = 31
        self.assertEqual(self.doctor(), 1)
        self.assertEqual(self.rows["state freshness"].verdict, "FAIL")
        self.assertIn("30", self.rows["state freshness"].detail)

    def test_a_panel_that_is_not_the_panel_fails(self):
        self.crabd.panel_body = "<html>nginx</html>"
        self.assertEqual(self.doctor(), 1)
        self.assertEqual(self.rows["panel"].verdict, "FAIL")

    def test_the_header_gate_row_fails_when_an_unheadered_post_is_accepted(self):
        # The gate being live is the whole point: a 204 here means crabd takes hook
        # writes from any page the browser happens to be on.
        self.crabd.unheadered_status = 204
        self.assertEqual(self.doctor(), 1)
        self.assertEqual(self.rows["hook header"].verdict, "FAIL")

    def test_the_notification_leg_skips_under_a_short_toast_threshold(self):
        config = self.home / ".sidecrab" / "config.json"
        config.write_text(json.dumps({"toast": {"thresholdSec": 30}}), encoding="utf-8")
        self.doctor()
        self.assertEqual(self.rows["hook Notification"].verdict, "SKIP")
        self.assertIn("30", self.rows["hook Notification"].detail)

    def test_a_live_smoke_session_stops_the_cycle_rather_than_disturbing_it(self):
        self.crabd.sessions["smoke-test"] = "working"
        self.assertEqual(self.doctor(), 1)
        self.assertEqual(self.rows["hook cycle"].verdict, "FAIL")
        self.assertEqual(self.rows["hook SessionStart"].verdict, "SKIP")
        # Nothing was posted, so a real session with that id is untouched.
        self.assertEqual(self.crabd.sessions, {"smoke-test": "working"})

    def test_session_end_is_posted_even_when_session_start_failed(self):
        posted = []
        original = self.crabd.post

        def post(url, body=None, headers=None, timeout=None):
            payload = json.loads(body) if body else {}
            posted.append(payload.get("hook_event_name"))
            if payload.get("hook_event_name") == "SessionStart":
                # Accepted on the wire, but crabd never shows the row: the failure the
                # finally block exists for.
                return (204, "")
            return original(url, body, headers, timeout)

        self.crabd.post = post
        self.assertEqual(self.doctor(), 1)
        self.assertEqual(self.rows["hook SessionStart"].verdict, "FAIL")
        self.assertIn("SessionStart", posted)
        self.assertEqual(posted[-1], "SessionEnd")
        self.assertEqual(self.crabd.sessions, {})

    def test_session_end_is_posted_even_when_the_cycle_raises(self):
        # The reason SessionEnd sits in a finally rather than after the last assertion:
        # a connection dropped mid-cycle would otherwise strand a phantom row in the
        # panel forever, with nothing left running to clear it.
        posted = []
        original = self.crabd.post

        def post(url, body=None, headers=None, timeout=None):
            payload = json.loads(body) if body else {}
            posted.append(payload.get("hook_event_name"))
            if payload.get("hook_event_name") == "Notification":
                raise RuntimeError("connection reset mid-cycle")
            return original(url, body, headers, timeout)

        self.crabd.post = post
        with self.assertRaises(RuntimeError):
            self.doctor()
        self.assertEqual(posted[-1], "SessionEnd")
        self.assertEqual(self.crabd.sessions, {})

    def test_a_status_line_that_is_not_an_object_does_not_crash_the_doctor(self):
        # null and a bare string are both legal JSON in that slot; .get() on either is
        # an AttributeError, and a doctor that dies mid-run leaves the smoke session live.
        for value in (None, "starship prompt", 7, []):
            with self.subTest(value=value):
                doc = json.loads((self.home / ".claude" / "settings.json").read_text("utf-8"))
                doc["statusLine"] = value
                (self.home / ".claude" / "settings.json").write_text(json.dumps(doc), "utf-8")
                self.assertEqual(self.doctor(), 1)
                self.assertEqual(self.rows["statusline chain"].verdict, "FAIL")
                self.assertEqual(self.crabd.sessions, {})

    def test_the_status_line_row_fails_for_each_way_it_can_be_wrong(self):
        settings = self.home / ".claude" / "settings.json"
        ours = json.loads(settings.read_text("utf-8"))["statusLine"]["command"]
        rows = [
            ({"type": "command", "command": "starship"}, "not SideCrab"),
            (dict(ours=None) and {"type": "command", "command": ours}, "NO chain file"),
            (
                # The subtle one: a command left behind by ANOTHER checkout matches the
                # script NAME, so "ours" passes - but every refresh runs a file this
                # install never checked, and after a repo move, one that is not there.
                {"type": "command", "command": "/other/repo/hooks/sidecrab_statusline.py"},
                "does NOT point at",
            ),
        ]
        for index, (value, expected) in enumerate(rows):
            with self.subTest(expected=expected):
                doc = json.loads(settings.read_text("utf-8"))
                doc["statusLine"] = value
                settings.write_text(json.dumps(doc), "utf-8")
                if index == 1:
                    (self.home / ".sidecrab" / "statusline-chain.json").unlink(missing_ok=True)
                self.assertEqual(self.doctor(), 1)
                self.assertEqual(self.rows["statusline chain"].verdict, "FAIL")
                self.assertIn(expected, self.rows["statusline chain"].detail)

    def test_the_python_row_fails_when_no_interpreter_is_usable(self):
        self.assertEqual(self.doctor(python_probe=lambda path: (1, "", "no developer tools")), 1)
        self.assertEqual(self.rows["python"].verdict, "FAIL")

    def test_the_config_row_fails_on_a_file_that_does_not_parse(self):
        (self.home / ".sidecrab" / "config.json").write_text("{ not json", encoding="utf-8")
        self.assertEqual(self.doctor(), 1)
        self.assertEqual(self.rows["config.json"].verdict, "FAIL")

    def test_a_session_end_that_never_clears_the_row_fails(self):
        original = self.crabd.post

        def post(url, body=None, headers=None, timeout=None):
            payload = json.loads(body) if body else {}
            if payload.get("hook_event_name") == "SessionEnd" and payload["session_id"] == "smoke-test":
                return (204, "")  # accepted on the wire, but the row never goes
            return original(url, body, headers, timeout)

        self.crabd.post = post
        self.assertEqual(self.doctor(), 1)
        self.assertEqual(self.rows["hook SessionEnd"].verdict, "FAIL")
        self.assertIn("still served", self.rows["hook SessionEnd"].detail)

    def test_the_limits_row_is_reported_and_never_judged(self):
        self.doctor(RecordingRunner({"print gui": RUNNING, "find-generic-password": (44, "", "")}))
        self.assertEqual(self.rows["limits token"].verdict, "PASS")
        self.assertIn("none stored", self.rows["limits token"].detail)

    def test_approvals_off_is_reported_and_on_is_judged(self):
        self.doctor()
        self.assertEqual(self.rows["panel approvals"].verdict, "PASS")
        self.assertIn("off", self.rows["panel approvals"].detail.lower())

        # ON with no pairing code: armed on paper, every tap 403s.
        config = self.home / ".sidecrab" / "config.json"
        config.write_text(json.dumps({"panelApprovals": {"enabled": True}}), encoding="utf-8")
        self.assertEqual(self.doctor(), 1)
        self.assertEqual(self.rows["panel approvals"].verdict, "FAIL")

    def test_an_unreachable_crabd_fails_rather_than_crashing(self):
        self.crabd.health_fails = 99
        self.crabd.state_status = 0
        self.assertEqual(self.doctor(), 1)
        self.assertEqual(self.rows["health"].verdict, "FAIL")
        self.assertEqual(self.rows["state reachable"].verdict, "FAIL")
        self.assertEqual(self.rows["state schema"].verdict, "SKIP")


if __name__ == "__main__":
    unittest.main()
