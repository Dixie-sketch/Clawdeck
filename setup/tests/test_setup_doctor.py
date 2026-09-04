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


if __name__ == "__main__":
    unittest.main()
