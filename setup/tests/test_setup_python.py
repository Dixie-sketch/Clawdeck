"""Interpreter detection: which python3 the LaunchAgent plists are allowed to name.

    python3 -m unittest discover -s setup/tests -t setup/tests
"""

from __future__ import annotations

import unittest

from _harness import setup


class ChoosePython(unittest.TestCase):
    def test_first_candidate_at_or_above_the_floor_wins(self):
        probes = {"/opt/homebrew/bin/python3.13": (0, "3.13\n", "")}
        choice = setup.choose_python(
            ["/opt/homebrew/bin/python3.13"], lambda path: probes[path]
        )
        self.assertEqual(choice.path, "/opt/homebrew/bin/python3.13")
        self.assertEqual(choice.version, (3, 13))

    def test_the_apple_stub_and_an_old_python_are_both_refused_with_a_reason(self):
        # Measured: Xcode's /usr/bin/python3 stub prints a "No developer tools were
        # found" note on stderr and exits non-zero; a real 3.9 exits 0 and is too old.
        probes = {
            "/usr/bin/python3": (1, "", "xcode-select: note: No developer tools were found\n"),
            "/usr/local/bin/python3": (0, "3.9\n", ""),
        }
        choice = setup.choose_python(list(probes), lambda path: probes[path])
        self.assertIsNone(choice.path)
        self.assertEqual([path for path, _ in choice.rejected], list(probes))
        self.assertIn("No developer tools", choice.rejected[0][1])
        self.assertIn("3.9", choice.rejected[1][1])

    def test_the_failure_message_names_the_fix(self):
        message = setup.python_failure_message(setup.choose_python([], lambda path: (1, "", "")))
        self.assertIn("brew install python@3.13", message)
        self.assertIn("absolute", message)


class PythonCandidates(unittest.TestCase):
    def test_override_first_then_newest_name_across_path_and_the_brew_prefixes(self):
        found = {
            ("/usr/bin", "python3"): "/usr/bin/python3",
            ("/opt/homebrew/bin", "python3.13"): "/opt/homebrew/bin/python3.13",
            ("/opt/homebrew/bin", "python3"): "/opt/homebrew/bin/python3",
        }
        candidates = setup.python_candidates(
            override="/my/python3",
            path_dirs=["/usr/bin"],
            is_file=lambda p: p in found.values() or p == "/my/python3",
        )
        self.assertEqual(
            candidates,
            [
                "/my/python3",
                "/opt/homebrew/bin/python3.13",
                "/usr/bin/python3",
                "/opt/homebrew/bin/python3",
            ],
        )

    def test_no_duplicates_when_a_brew_prefix_is_also_on_path(self):
        candidates = setup.python_candidates(
            override=None,
            path_dirs=["/opt/homebrew/bin"],
            is_file=lambda p: p == "/opt/homebrew/bin/python3",
        )
        self.assertEqual(candidates, ["/opt/homebrew/bin/python3"])


if __name__ == "__main__":
    unittest.main()
