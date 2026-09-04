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

import subprocess
import sys
import unittest
import unittest.mock
from pathlib import Path

NOTIFIER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NOTIFIER_DIR))

from sidecrab_toast import (  # noqa: E402
    MAC_OSASCRIPT,
    MAC_SCRIPT_DISPLAY_LINE,
    MAC_SCRIPT_END_RUN,
    MAC_SCRIPT_ON_RUN,
    TITLE_TRIM,
    MacNotificationAdapter,
    ToastRequest,
    build_request,
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


#: Every metacharacter that means something to AppleScript, to `sh`, or to both. A question,
#: a session title and a tool summary are all arbitrary operator/model text, so each of these
#: WILL arrive one day.
HOSTILE = "\" ' \\ $(touch /tmp/x) `id` & ; rm -rf"

#: The same, with a newline — separated out because `trim()` collapses whitespace, so the
#: composed-request path can only be asserted on the single-line form.
HOSTILE_MULTILINE = HOSTILE + "\nand a second line"


class InjectionTests(unittest.TestCase):
    """THE test. Everything else in this file is scaffolding for it.

    A notification body is a question the model wrote and a session title is a directory name;
    neither is trustworthy, and both used to cross an interpreter boundary on Windows only
    because base64 stood in the way. Here nothing is escaped, because nothing is interpolated:
    the hostile string is one argv element and the script that reads it is a constant.
    """

    def argv_for(self, request: ToastRequest) -> list[str]:
        runner = RecordingRunner()
        MacNotificationAdapter(runner=runner).show(request)
        return runner.argv

    def test_a_hostile_title_and_body_arrive_verbatim_as_one_element_each(self) -> None:
        argv = self.argv_for(
            ToastRequest("s1", "2026-09-04T09:00:00Z", HOSTILE_MULTILINE, HOSTILE_MULTILINE)
        )
        self.assertEqual(argv[8], HOSTILE_MULTILINE, "body")
        self.assertEqual(argv[9], HOSTILE_MULTILINE, "title")
        self.assertEqual(argv.count(HOSTILE_MULTILINE), 2, "split into pieces, or joined")

    def test_a_hostile_session_label_survives_the_composed_request(self) -> None:
        """Through the real `build_request`, because that is where a session title becomes a
        notification title: the label is inside the title, not a field of its own."""
        self.assertLessEqual(len(HOSTILE), TITLE_TRIM, "the probe must not be trimmed away")
        argv = self.argv_for(
            build_request(
                {"id": "s1", "title": HOSTILE, "question": HOSTILE}, "2026-09-04T09:00:00Z"
            )
        )
        self.assertEqual(argv[8], HOSTILE, "body")
        self.assertEqual(argv[9], f"Claude is waiting — {HOSTILE}", "title")

    def test_the_script_is_byte_identical_to_a_plain_request_s(self) -> None:
        """The mutation gate. Interpolating any of this text into the display line changes
        these three strings, and this is the assertion that notices."""
        plain = self.argv_for(request())[:8]
        hostile = self.argv_for(
            ToastRequest("s1", "t", HOSTILE_MULTILINE, HOSTILE_MULTILINE)
        )[:8]
        self.assertEqual(plain, hostile)
        self.assertEqual(
            plain[1:8],
            ["-e", MAC_SCRIPT_ON_RUN, "-e", MAC_SCRIPT_DISPLAY_LINE, "-e", MAC_SCRIPT_END_RUN, "--"],
        )

    def test_the_subprocess_is_handed_a_list_and_never_a_shell(self) -> None:
        """The other half of the argument. A constant script is worthless if the argv is
        joined into one string for `sh -c`, which is what `shell=True` would mean."""
        captured: list[tuple[tuple, dict]] = []

        def fake_run(*args, **kwargs):
            captured.append((args, kwargs))
            return subprocess.CompletedProcess(args[0], 0, "", "")

        with unittest.mock.patch("subprocess.run", fake_run):
            # No runner injected: this exercises the shipping default path.
            ok = MacNotificationAdapter().show(
                ToastRequest("s1", "t", HOSTILE_MULTILINE, HOSTILE_MULTILINE)
            )

        self.assertTrue(ok)
        (args, kwargs) = captured[0]
        self.assertIsInstance(args[0], list, "the command must be a list, never a string")
        self.assertNotIn("shell", kwargs, "shell= must not be set at all")
        self.assertIn(HOSTILE_MULTILINE, args[0], "and the text is still one element")
        self.assertEqual(args[0][0], MAC_OSASCRIPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
