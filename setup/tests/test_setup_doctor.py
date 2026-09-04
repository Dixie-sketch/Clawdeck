"""The read-only commands: pairing-code, limits-token, status and doctor.

Everything they touch - HTTP, launchctl, the Keychain, stdin - is injected, so the whole
file runs offline against a temporary HOME.
"""

from __future__ import annotations

import json
import unittest

from _harness import RecordingHttp, RecordingRunner, TempHome, setup


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

        env = self.env(read_secret=lambda prompt: secret, store_token=store or default_store, **kwargs)
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
        def missing(token):
            raise AttributeError("PLATFORM has no attribute 'store_limits_token'")

        code, _ = self.run_with(self.TOKEN, store=missing)
        self.assertEqual(code, 1)
        self.assertIn("store_limits_token", self.output)
        self.assertIn("crabd", self.output)

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

    def test_the_allow_list_row_tells_the_three_states_apart(self):
        rows = [
            (None, "unset"),
            (["http://example/*"], "does NOT admit"),
            (list(setup.ALLOWED_HOOK_PATTERNS), "admits crabd"),
        ]
        for patterns, expected in rows:
            with self.subTest(patterns=patterns):
                doc = {} if patterns is None else {"allowedHttpHookUrls": patterns}
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
        self.status(run=runner)
        self.assertIn("none stored", self.output)

    def test_the_panel_url_is_the_last_word(self):
        self.status()
        self.assertIn(setup.PANEL_URL, self.output)


if __name__ == "__main__":
    unittest.main()
