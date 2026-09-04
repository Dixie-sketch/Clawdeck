"""Interpreter detection: which python3 the LaunchAgent plists are allowed to name.

    python3 -m unittest discover -s setup/tests -t setup/tests
"""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest import mock

from _harness import SETUP_DIR, setup


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


class TheTwoSearchesAgree(unittest.TestCase):
    """sidecrab_python.sh is a second copy of the rule, so it is read and compared.

    It has to be a copy - it runs before any SideCrab Python exists - and a copy that
    nothing checks is a copy that drifts. Source text, not behaviour: these are the
    three facts an operator would notice diverging.
    """

    def setUp(self):
        self.shell = (SETUP_DIR / "sidecrab_python.sh").read_text(encoding="utf-8")

    def test_the_name_list_matches(self):
        match = re.search(r"SIDECRAB_PY_NAMES='([^']*)'", self.shell)
        self.assertIsNotNone(match, self.shell[:400])
        self.assertEqual(tuple(match.group(1).split()), setup.PYTHON_NAMES)

    def test_the_floor_matches(self):
        major = re.search(r"SIDECRAB_PY_MIN_MAJOR=(\d+)", self.shell)
        minor = re.search(r"SIDECRAB_PY_MIN_MINOR=(\d+)", self.shell)
        self.assertEqual((int(major.group(1)), int(minor.group(1))), setup.PYTHON_MIN)

    def test_the_extra_directories_match(self):
        match = re.search(r'SIDECRAB_PYTHON_DIRS=([^}"\n]*)\}', self.shell)
        self.assertIsNotNone(match, self.shell[:400])
        self.assertEqual(tuple(match.group(1).split(":")), setup.PYTHON_EXTRA_DIRS)


class ExtraDirectoriesOverride(unittest.TestCase):
    def test_an_empty_value_means_no_extra_dirs_not_the_defaults(self):
        # The shell honours an empty SIDECRAB_PYTHON_DIRS as "none". Python appending
        # PYTHON_EXTRA_DIRS regardless would make the same value mean two things.
        self.assertEqual(
            setup.python_candidates(None, ["/bin"], lambda p: True, extra_dirs=()),
            ["/bin/python3.14", "/bin/python3.13", "/bin/python3"],
        )

    def test_the_default_still_carries_the_brew_prefixes(self):
        found = setup.python_candidates(None, [], lambda p: True)
        self.assertIn("/opt/homebrew/bin/python3.13", found)
        self.assertIn("/usr/local/bin/python3", found)

    def test_the_environment_wiring_honours_an_empty_value_too(self):
        # Not just python_candidates(): the env var has to reach it, or the shell and
        # the module read the same empty value two different ways.
        offered = []

        def is_file(path):
            offered.append(path)
            return path == "/bin/python3"

        environment = setup.Environment(
            home=Path("/nowhere"), repo_root=Path("/nowhere"), uid=0, user="t",
            now=lambda: None, run=lambda *a, **k: (0, "", ""),
            http_get=lambda *a, **k: (0, ""), http_post=lambda *a, **k: (0, ""),
            python_probe=lambda path: (0, "3.13\n", ""),
            path_dirs=("/bin",), is_file=is_file,
        )
        with mock.patch.dict(os.environ, {"SIDECRAB_PYTHON_DIRS": ""}):
            self.assertEqual(environment.resolve_python(), "/bin/python3")
        self.assertEqual([p for p in offered if "homebrew" in p or "usr/local" in p], [])


class RelativeOverride(unittest.TestCase):
    """A bare name or ./python3 in SIDECRAB_PYTHON must end up absolute, or be refused.

    The plist stores the interpreter path and a LaunchAgent has no useful working
    directory, so a relative one would produce an agent that never starts.
    """

    def test_a_bare_name_is_looked_up_on_path(self):
        self.assertEqual(
            setup.absolute_override("python3.13", ["/nope", "/opt/bin"], lambda p: p == "/opt/bin/python3.13"),
            "/opt/bin/python3.13",
        )

    def test_a_relative_path_is_made_absolute_against_the_working_directory(self):
        self.assertEqual(
            setup.absolute_override("./python3", [], lambda p: True, cwd="/work"),
            "/work/python3",
        )

    def test_an_absolute_path_is_returned_unchanged(self):
        self.assertEqual(setup.absolute_override("/opt/py", [], lambda p: True), "/opt/py")

    def test_a_name_that_is_nowhere_is_refused_by_name(self):
        with self.assertRaises(setup.SetupError) as caught:
            setup.absolute_override("nosuchpython", ["/nope"], lambda p: False)
        self.assertIn("SIDECRAB_PYTHON", str(caught.exception))
        self.assertIn("nosuchpython", str(caught.exception))

    def test_nothing_set_is_not_an_error(self):
        self.assertIsNone(setup.absolute_override(None, [], lambda p: True))


if __name__ == "__main__":
    unittest.main()
