"""Headless unit tests for the macOS notification adapter (v0.22.0).

The Windows adapter builds a PowerShell script and hides the payload inside base64 so no
quote, brace or backtick in a question can escape into source. The macOS adapter needs the
same property against a different interpreter, and gets it a stronger way: the AppleScript is
three CONSTANT strings and the text rides in ``argv``, so there is no interpolation to escape
from in the first place.

MEASURED on this Mac (macOS 26.6, /usr/bin/osascript), and the reason the design is what it is:
``osascript -e 'on run argv' -e '<script>' -e 'end run' -- <arg>`` hands every argument to the
script byte for byte. A probe string carrying ``"``, ``\\``, a newline, ``$(touch ...)``,
backticks, ``&`` and ``; rm -rf /`` came back identical, exit 0, and the command substitution
did not run.

Every test here is pure and runs on any OS: the subprocess is an injected runner.

    python -m unittest discover -s notifier/tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

NOTIFIER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NOTIFIER_DIR))

from sidecrab_toast import (  # noqa: E402
    MAC_OSASCRIPT,
    MAC_SCRIPT_DISPLAY_LINE,
    MAC_SCRIPT_END_RUN,
    MAC_SCRIPT_ON_RUN,
    MacNotificationAdapter,
    ToastRequest,
)


class RecordingRunner:
    """Stands in for the subprocess. Records the argv it was handed, verbatim."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.calls: list[tuple[list[str], float]] = []
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        self.calls.append((list(argv), timeout))
        return self.returncode, self.stdout, self.stderr

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][0]


def request(title: str = "Claude is waiting — the lane", body: str = "Should I overwrite it?") -> ToastRequest:
    return ToastRequest(session_id="s1", state_since="2026-09-04T09:00:00Z", title=title, body=body)


class ArgvShapeTests(unittest.TestCase):
    """The whole call, asserted as one list. Its shape IS the security argument."""

    def test_the_argv_is_the_three_script_constants_then_the_three_arguments(self) -> None:
        runner = RecordingRunner()
        adapter = MacNotificationAdapter(runner=runner)
        adapter.show(request())

        argv = runner.argv
        self.assertEqual(argv[0], MAC_OSASCRIPT)
        self.assertEqual(
            argv[1:8],
            ["-e", MAC_SCRIPT_ON_RUN, "-e", MAC_SCRIPT_DISPLAY_LINE, "-e", MAC_SCRIPT_END_RUN, "--"],
        )
        self.assertEqual(len(argv), 11, "three -e script strings, the separator, three arguments")

    def test_the_script_strings_carry_no_text_from_the_request(self) -> None:
        """The one property everything else rests on: the script is a CONSTANT. Anything from
        the request appearing in an ``-e`` argument is an interpolation, and an interpolation
        is an injection waiting for the right question text."""
        runner = RecordingRunner()
        MacNotificationAdapter(runner=runner).show(request(title="TITLEMARK", body="BODYMARK"))
        script = " ".join(runner.argv[:8])
        self.assertNotIn("TITLEMARK", script)
        self.assertNotIn("BODYMARK", script)

    def test_the_display_line_reads_all_three_arguments_out_of_argv(self) -> None:
        """Vacuity guard: a display line that never reads ``argv`` would pass every
        "no request text in the script" assertion by saying nothing at all."""
        for item in ("item 1 of argv", "item 2 of argv", "item 3 of argv"):
            self.assertIn(item, MAC_SCRIPT_DISPLAY_LINE, item)
        self.assertIn("display notification", MAC_SCRIPT_DISPLAY_LINE)

    def test_a_clean_exit_is_a_shown_notification(self) -> None:
        self.assertTrue(MacNotificationAdapter(runner=RecordingRunner()).show(request()))

    def test_the_timeout_travels_with_the_call(self) -> None:
        runner = RecordingRunner()
        MacNotificationAdapter(timeout=3.5, runner=runner).show(request())
        self.assertEqual(runner.calls[-1][1], 3.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
