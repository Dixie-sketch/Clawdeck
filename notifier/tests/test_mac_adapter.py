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

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOTIFIER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NOTIFIER_DIR))

import sidecrab_toast  # noqa: E402
from sidecrab_toast import (  # noqa: E402
    APPROVAL_HINT,
    BODY_TRIM,
    MAC_OSASCRIPT,
    MAC_SCRIPT_DISPLAY_LINE,
    MAC_SCRIPT_END_RUN,
    MAC_SCRIPT_ON_RUN,
    MAC_TITLE_TRIM,
    TITLE_TRIM,
    MacNotificationAdapter,
    PendingPermission,
    PowerShellToastAdapter,
    RecordingToastAdapter,
    ToastRequest,
    build_approval_request,
    build_long_run_request,
    build_request,
    pick_adapter,
    strip_control,
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


#: The bands the notifier has stripped since the F1 fix: 0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F.
ILLEGAL = "".join(chr(c) for c in list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)))

#: The three that are real text and must survive: tab, newline, carriage return.
LEGAL_WHITESPACE = "\t\n\r"


class ControlCharacterTests(unittest.TestCase):
    """One character class for both adapters. The Windows lane strips these because an
    XML-1.0-illegal byte makes LoadXml reject the whole document (F1); this lane strips them
    because a NUL in an argv element makes subprocess raise, and because a control byte is
    not something a notification can render either way."""

    def test_strip_control_drops_the_illegal_bands(self) -> None:
        self.assertEqual(strip_control(f"a{ILLEGAL}b"), "ab")

    def test_strip_control_keeps_tab_newline_and_return(self) -> None:
        """0x09/0x0A/0x0D are content. A question written across two lines stays two lines."""
        self.assertEqual(strip_control(f"a{LEGAL_WHITESPACE}b"), f"a{LEGAL_WHITESPACE}b")

    def test_strip_control_keeps_every_metacharacter(self) -> None:
        """It is a control-byte strip and nothing else — it must not become a sanitiser that
        quietly mangles the text the operator is meant to read."""
        self.assertEqual(strip_control(HOSTILE_MULTILINE), HOSTILE_MULTILINE)

    def test_strip_control_never_raises_on_a_non_string(self) -> None:
        self.assertEqual(strip_control(None), "None")

    def test_every_argument_reaches_argv_stripped(self) -> None:
        runner = RecordingRunner()
        MacNotificationAdapter(runner=runner).show(
            ToastRequest("s1", "t", f"ti{ILLEGAL}tle", f"bo{ILLEGAL}dy")
        )
        for argument in runner.argv[8:]:
            for bad in ILLEGAL:
                self.assertNotIn(bad, argument, repr(bad))
        self.assertEqual(runner.argv[8], "body")
        self.assertEqual(runner.argv[9], "title")

    def test_a_multiline_question_still_reaches_argv_with_its_newline(self) -> None:
        runner = RecordingRunner()
        MacNotificationAdapter(runner=runner).show(ToastRequest("s1", "t", "t", "one\ntwo"))
        self.assertEqual(runner.argv[8], "one\ntwo")

    def test_a_null_byte_is_what_makes_the_strip_load_bearing(self) -> None:
        """Not cosmetic. subprocess rejects an embedded NUL with ValueError — raised while
        converting the arguments, before any process starts — and ValueError is not one of the
        failures show() turns into False, so an unstripped NUL would reach the daemon as a
        raise where the contract promises a bool. Measured with a path that does not exist:
        the NUL is refused first, the missing executable second."""
        with self.assertRaises(ValueError):
            subprocess.run(["/nonexistent-sidecrab-probe", "a\x00b"], capture_output=True)


class ByteBudgetTests(unittest.TestCase):
    """The arguments are bounded here as well as upstream. `build_request` trims what IT
    composes; a request assembled any other way (a future decider, a `--test-*` flag) would
    otherwise hand osascript an unbounded argument."""

    def argv_for(self, request: ToastRequest) -> list[str]:
        runner = RecordingRunner()
        MacNotificationAdapter(runner=runner).show(request)
        return runner.argv

    def test_a_four_kilobyte_question_is_trimmed_the_way_build_request_trims(self) -> None:
        argv = self.argv_for(
            build_request({"id": "s1", "title": "the lane", "question": "x" * 4096}, "t")
        )
        self.assertEqual(len(argv[8]), BODY_TRIM)
        self.assertTrue(argv[8].endswith("…"), "the existing ellipsis, not a hard cut")

    def test_a_four_kilobyte_body_that_skipped_build_request_is_still_capped(self) -> None:
        """The non-vacuous half: this request never passed through `trim`."""
        argv = self.argv_for(ToastRequest("s1", "t", "title", "y" * 4096))
        self.assertEqual(len(argv[8]), BODY_TRIM)

    def test_the_approval_hint_survives_at_the_end_of_the_body(self) -> None:
        """APPROVAL_BODY_TRIM reserves the hint out of the budget on purpose: the tool
        summary is what gets cut, never the line telling the operator where to act."""
        pending = PendingPermission(
            session_id="s1", tool="Bash", summary="git push --force " + "z" * 400,
            requested_at="2026-09-04T09:00:00Z",
        )
        argv = self.argv_for(build_approval_request(pending))
        self.assertTrue(argv[8].endswith(APPROVAL_HINT), argv[8])
        self.assertLessEqual(len(argv[8]), BODY_TRIM)

    def test_a_long_title_is_capped(self) -> None:
        argv = self.argv_for(ToastRequest("s1", "t", "T" * 4096, "body"))
        self.assertEqual(len(argv[9]), MAC_TITLE_TRIM)

    def test_a_composed_title_at_the_existing_label_budget_is_never_cut(self) -> None:
        """Why the title budget is not TITLE_TRIM. That one caps the session LABEL, and the
        notification title is "Claude is waiting — <label>" — capping the composed line at 48
        would eat the very label the notification exists to name."""
        label = "L" * TITLE_TRIM
        argv = self.argv_for(build_request({"id": "s1", "title": label, "question": "q"}, "t"))
        self.assertEqual(argv[9], f"Claude is waiting — {label}")
        self.assertNotIn("…", argv[9])

    def test_a_long_run_title_at_the_same_budget_is_never_cut(self) -> None:
        """The longest prefix this file composes, so the budget is proven against the worst
        real case rather than the common one."""
        started = datetime(2026, 9, 4, 1, 0, 0, tzinfo=timezone.utc)
        finished = started + timedelta(hours=12, minutes=34)
        argv = self.argv_for(
            build_long_run_request({"title": "L" * TITLE_TRIM}, "s1", started, finished)
        )
        self.assertNotIn("…", argv[9])
        self.assertLess(len(argv[9]), MAC_TITLE_TRIM)


class RaisingRunner:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def __call__(self, argv: list[str], timeout: float) -> tuple[int, str, str]:
        raise self.exc


class FailureTests(unittest.TestCase):
    """`show` returns a bool and NEVER raises. The daemon's re-arm path (Notifier._emit)
    reads that bool, and a raise from here is the shape that once buried three live
    questions in one poll — see test_emit_matrix.py."""

    def show(self, runner) -> bool:
        return MacNotificationAdapter(runner=runner).show(request())

    def test_a_non_zero_exit_is_a_failure(self) -> None:
        self.assertFalse(self.show(RecordingRunner(returncode=1, stderr="execution error")))

    def test_a_non_zero_exit_logs_the_code_and_the_stderr(self) -> None:
        with self.assertLogs("sidecrab.notifier", level="WARNING") as captured:
            self.show(RecordingRunner(returncode=2, stderr="execution error: -1743"))
        joined = "\n".join(captured.output)
        self.assertEqual(len(captured.output), 1, "one line per failure, not one per argument")
        self.assertIn("rc=2", joined)
        self.assertIn("-1743", joined)

    def test_the_failure_line_never_carries_the_notification_text(self) -> None:
        """The log is a file on disk that outlives the notification. A question the operator
        asked in private does not belong in it — the exit code and osascript's own complaint
        are what diagnose a broken notification path."""
        with self.assertLogs("sidecrab.notifier", level="WARNING") as captured:
            MacNotificationAdapter(runner=RecordingRunner(returncode=1, stderr="boom")).show(
                request(title="SECRETTITLE", body="SECRETBODY")
            )
        joined = "\n".join(captured.output)
        self.assertNotIn("SECRETTITLE", joined)
        self.assertNotIn("SECRETBODY", joined)

    def test_a_long_stderr_is_cut_at_120_characters(self) -> None:
        with self.assertLogs("sidecrab.notifier", level="WARNING") as captured:
            self.show(RecordingRunner(returncode=1, stderr="E" * 500))
        self.assertIn("E" * 120, captured.output[0])
        self.assertNotIn("E" * 121, captured.output[0])

    def test_every_subprocess_failure_shape_is_false_and_never_raises(self) -> None:
        """A timeout (osascript wedged behind a permission dialog), a missing binary, and the
        SubprocessError family. Each is a failed notification, not a dead daemon."""
        for exc in (
            subprocess.TimeoutExpired(cmd=[MAC_OSASCRIPT], timeout=10.0),
            FileNotFoundError(2, "No such file or directory: '/usr/bin/osascript'"),
            PermissionError(13, "Permission denied"),
            subprocess.SubprocessError("something else went wrong"),
        ):
            with self.subTest(exc=type(exc).__name__):
                with self.assertLogs("sidecrab.notifier", level="ERROR"):
                    self.assertFalse(self.show(RaisingRunner(exc)))

    def test_a_clean_exit_logs_nothing(self) -> None:
        """A working notifier fires one of these every few minutes; a line per success would
        bury the ones that matter."""
        with self.assertNoLogs("sidecrab.notifier", level="WARNING"):
            self.assertTrue(self.show(RecordingRunner()))


class PickAdapterTests(unittest.TestCase):
    """The one construction site that used to make this file Windows-only at runtime, now a
    pure function so the decision is testable from either platform."""

    def test_darwin_gets_the_notification_adapter(self) -> None:
        self.assertIsInstance(pick_adapter("darwin", None), MacNotificationAdapter)

    def test_win32_gets_the_powershell_adapter_with_its_icon(self) -> None:
        icon = Path("C:/Dev/sidecrab/notifier/sidecrab.png")
        adapter = pick_adapter("win32", icon)
        self.assertIsInstance(adapter, PowerShellToastAdapter)
        self.assertEqual(adapter.icon_path, icon)

    def test_linux_gets_the_powershell_adapter_and_will_simply_fail(self) -> None:
        """There is no Linux route. The honest shape is the Windows adapter returning False
        on every show — a failure the log names — rather than a third branch pretending to
        post a notification nobody will see."""
        self.assertIsInstance(pick_adapter("linux", None), PowerShellToastAdapter)

    def test_main_selects_the_adapter_from_sys_platform(self) -> None:
        """`main` must ask this function rather than naming a class, or every `--test-*` flag
        on macOS would fire through the Windows adapter."""
        seen: list[tuple] = []

        def spy(platform: str, icon: Path | None):
            seen.append((platform, icon))
            return RecordingToastAdapter()

        with unittest.mock.patch.object(sidecrab_toast, "pick_adapter", spy), \
             unittest.mock.patch.object(sidecrab_toast, "setup_logging", lambda *a, **k: None):
            code = sidecrab_toast.main(["--test-toast"])

        self.assertEqual([call[0] for call in seen], [sys.platform])
        self.assertEqual(code, 0)

    def test_dry_run_still_records_instead_of_showing(self) -> None:
        with unittest.mock.patch.object(sidecrab_toast, "setup_logging", lambda *a, **k: None), \
             unittest.mock.patch.object(sidecrab_toast, "pick_adapter", self.fail_if_called):
            self.assertEqual(sidecrab_toast.main(["--dry-run", "--test-toast"]), 0)

    def fail_if_called(self, *args, **kwargs):
        # --dry-run must not even construct the real adapter: on a box with no osascript the
        # constructor is harmless, but the flag's promise is that nothing platform-specific runs.
        return RecordingToastAdapter()


T0 = datetime(2026, 9, 4, 9, 0, 0, tzinfo=timezone.utc)


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class DaemonThroughTheMacAdapterTests(unittest.TestCase):
    """`Notifier._emit`'s failure and re-arm contract is adapter-agnostic — test_emit_matrix.py
    proves it with stand-in adapters. This proves the real macOS adapter satisfies it, which is
    the half a stub cannot: a `show` that raised, or that returned True on a failed osascript,
    would consume a live question forever."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        config = root / "config.json"
        config.write_text(json.dumps({"toast": {"enabled": True}}), encoding="utf-8")

        real_fetch = sidecrab_toast.fetch_state
        self.addCleanup(lambda: setattr(sidecrab_toast, "fetch_state", real_fetch))

        self.runner = RecordingRunner()
        self.notifier = sidecrab_toast.Notifier(
            adapter=MacNotificationAdapter(runner=self.runner),
            config_reader=sidecrab_toast.ConfigReader(config),
            digest_ledger=sidecrab_toast.DigestLedger(root / "toast-state.json"),
            budget_ledger=sidecrab_toast.BudgetLedger(root / "toast-state.json"),
            snooze_ledger=sidecrab_toast.SnoozeLedger(root / "toast-state.json"),
        )

    def serve(self, *sessions: dict) -> None:
        self.sessions = list(sessions)

    def poll(self, seconds: int) -> list:
        """One poll, with the feed stamped AT the poll instant — otherwise the stale-feed
        decider joins in five minutes after T0 and every count in here is off by one."""
        now = T0 + timedelta(seconds=seconds)
        state = {
            "schema": 5,
            "generatedAt": iso(now),
            "sessions": self.sessions,
            "quiet": {"active": False, "start": "22:00", "end": "07:00"},
        }
        sidecrab_toast.fetch_state = lambda *a, **k: state
        return self.notifier.poll_once(now=now)

    def waiting(self, sid: str, title: str) -> dict:
        return {"id": sid, "title": title, "state": "needs_input", "stateSince": iso(T0),
                "question": f"question from {sid}"}

    def titles_posted(self) -> list[str]:
        return [argv[9] for argv, _timeout in self.runner.calls]

    def test_one_osascript_call_per_owed_notification(self) -> None:
        self.serve(self.waiting("s1", "lane one"), self.waiting("s2", "lane two"),
                   self.waiting("s3", "lane three"))
        fired = self.poll(300)
        self.assertEqual(len(fired), 3)
        self.assertEqual(
            self.titles_posted(),
            ["Claude is waiting — lane one", "Claude is waiting — lane two",
             "Claude is waiting — lane three"],
        )

    def test_a_shown_notification_is_not_repeated_on_the_next_poll(self) -> None:
        self.serve(self.waiting("s1", "lane one"))
        self.poll(300)
        self.poll(310)
        self.assertEqual(len(self.runner.calls), 1, "one notification per waiting spell")

    def test_a_non_zero_osascript_re_arms_the_waiting_question(self) -> None:
        """The F1 contract, through this adapter: a notification that did not land must not
        consume the spell, or the operator is never told about a question that is still live."""
        self.runner.returncode = 1
        self.serve(self.waiting("s1", "lane one"))
        self.assertEqual(self.poll(300), [])
        self.assertEqual(self.poll(310), [])
        self.assertEqual(len(self.runner.calls), 2, "retried, not consumed")

        self.runner.returncode = 0
        fired = self.poll(320)
        self.assertEqual([r.session_id for r in fired], ["s1"], "it lands once osascript can")

    def test_a_missing_osascript_is_survivable_and_still_re_arms(self) -> None:
        """The other failure shape at the same seam: no binary at all."""
        adapter = MacNotificationAdapter(
            runner=RaisingRunner(FileNotFoundError(2, "No such file or directory"))
        )
        self.notifier.adapter = adapter
        self.serve(self.waiting("s1", "lane one"))
        with self.assertLogs("sidecrab.notifier", level="ERROR"):
            self.assertEqual(self.poll(300), [])

        self.notifier.adapter = MacNotificationAdapter(runner=self.runner)
        fired = self.poll(310)
        self.assertEqual([r.session_id for r in fired], ["s1"])


class ModuleImportsAnywhereTests(unittest.TestCase):
    """The module is imported by tests, by the installer and by the daemon on both platforms.
    A platform call at import time — `import winreg`, a `/usr/bin` probe — turns a supported
    OS into an ImportError, which is why the AUMID probe imports winreg inside its function."""

    def load_under(self, platform: str):
        spec = importlib.util.spec_from_file_location(
            f"sidecrab_toast_as_{platform}", NOTIFIER_DIR / "sidecrab_toast.py"
        )
        module = importlib.util.module_from_spec(spec)
        # In sys.modules for the duration of exec_module: @dataclass resolves its annotations
        # through sys.modules[cls.__module__], and a module that is not there yet fails with
        # "'NoneType' object has no attribute '__dict__'". Removed again so this fresh copy
        # cannot be picked up as the real module by anything else.
        sys.modules[spec.name] = module
        try:
            with unittest.mock.patch.object(sys, "platform", platform):
                spec.loader.exec_module(module)
        finally:
            sys.modules.pop(spec.name, None)
        return module

    def test_it_imports_as_darwin_and_as_win32(self) -> None:
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform):
                module = self.load_under(platform)
                self.assertIsInstance(
                    module.pick_adapter("darwin", None), module.MacNotificationAdapter
                )
                self.assertIsInstance(
                    module.pick_adapter("win32", None), module.PowerShellToastAdapter
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
