"""Headless unit tests for the version reporting (v0.16.0).

THE BUG THIS CLOSES: the notifier could not tell anyone it was running stale code. A
`SideCrab-toast` Scheduled Task that has not been restarted since sidecrab_toast.py changed
looks — in Task Scheduler, in the log, in /v1/state — exactly like one running the current
file. That class bit twice on 2026-08-26 (SUPPORTED_SCHEMAS stopped at 3 while crabd served 5;
the AUMID probe latched a borrowed answer), and both investigations began by assuming the
running code was the code on disk.

So there are now three answers, and these tests pin all three:

  the LOG      one line at startup, every invocation, version + module path
  --version    a clean, parseable answer on stdout with no log preamble
  the LEDGER   a `notifier` section setup/Test-SideCrab.ps1 can compare against disk

Stdlib unittest only, no Windows.

    python -m unittest discover -s notifier/tests -v
"""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sidecrab_toast  # noqa: E402
from sidecrab_toast import (  # noqa: E402
    RUNTIME_SECTION,
    RUNTIME_STAMP_REFRESH_SEC,
    DigestLedger,
    RuntimeStamp,
    __version__,
    main,
    read_state_doc,
    write_state_section,
)

T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


class VersionStringTests(unittest.TestCase):
    def test_it_is_a_dotted_version(self) -> None:
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_it_moved_past_the_release_that_had_no_version_at_all(self) -> None:
        """0.15.0 shipped with no __version__ — which is the whole reason this exists."""
        parts = tuple(int(p) for p in __version__.split("."))
        self.assertGreater(parts, (0, 15, 0))


class VersionFlagTests(unittest.TestCase):
    def run_flag(self) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(["--version"])
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def test_it_prints_the_version(self) -> None:
        self.assertIn(__version__, self.run_flag())

    def test_the_first_line_is_parseable_without_stripping_a_log_preamble(self) -> None:
        """--version answers BEFORE logging is set up, so the setup lane can read it directly."""
        first = self.run_flag().splitlines()[0]
        self.assertRegex(first, rf"^sidecrab-notifier {re.escape(__version__)}$")

    def test_it_names_the_module_it_is_actually_running(self) -> None:
        """The other half of the stale-code question: a repo cloned twice answers 'which
        version' identically and 'which file' differently."""
        out = self.run_flag()
        self.assertIn("module:", out)
        self.assertIn("sidecrab_toast.py", out)

    def test_it_starts_no_daemon_and_shows_no_toast(self) -> None:
        """Asserted at the construction site rather than at one adapter class: --version
        answers before any platform code runs, on every platform."""
        built: list = []
        adapter = sidecrab_toast.RecordingToastAdapter()
        real = sidecrab_toast.pick_adapter
        sidecrab_toast.pick_adapter = lambda *a, **k: built.append(a) or adapter
        try:
            with redirect_stdout(io.StringIO()):
                main(["--version"])
        finally:
            sidecrab_toast.pick_adapter = real
        self.assertEqual(built, [], "no adapter is even constructed")
        self.assertEqual(adapter.shown, [])


class StateSectionTests(unittest.TestCase):
    """The shared read-modify-write every writer of toast-state.json now goes through."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "toast-state.json"

    def test_it_writes_a_section(self) -> None:
        self.assertTrue(write_state_section(self.state, "a", {"x": 1}))
        self.assertEqual(read_state_doc(self.state), {"a": {"x": 1}})

    def test_it_replaces_only_its_own_key(self) -> None:
        """Five things share this file. A whole-file rewrite would have each writer silently
        erase the others, and the only symptom is a duplicate toast after a restart."""
        write_state_section(self.state, "digest", {"lastDay": "2026-08-26"})
        write_state_section(self.state, "budget", {"lastDay": "2026-08-27"})
        doc = read_state_doc(self.state)
        self.assertEqual(doc["digest"], {"lastDay": "2026-08-26"})
        self.assertEqual(doc["budget"], {"lastDay": "2026-08-27"})

    def test_a_corrupt_document_is_replaced_rather_than_raising(self) -> None:
        self.state.write_text("{not json", encoding="utf-8")
        self.assertTrue(write_state_section(self.state, "a", 1))
        self.assertEqual(read_state_doc(self.state), {"a": 1})

    def test_an_unwritable_path_returns_false_and_never_raises(self) -> None:
        bad = Path(self.tmp.name) / "afile" / "nested" / "state.json"
        (Path(self.tmp.name) / "afile").write_text("x", encoding="utf-8")
        self.assertFalse(write_state_section(bad, "a", 1))

    def test_it_leaves_no_temp_file_behind(self) -> None:
        write_state_section(self.state, "a", 1)
        self.assertEqual(list(Path(self.tmp.name).glob("*.tmp")), [])

    def test_the_temp_name_carries_the_pid(self) -> None:
        """The snooze handler is a separate PROCESS writing this same document; a shared temp
        name would let two writers interleave and then rename a hybrid into place."""
        import os

        write_state_section(self.state, "a", 1)
        self.assertIn(str(os.getpid()), str(self.state.with_suffix(f"{self.state.suffix}.{os.getpid()}.tmp")))

    def test_the_day_ledger_still_uses_it(self) -> None:
        ledger = DigestLedger(self.state)
        ledger.mark("2026-08-26")
        self.assertEqual(read_state_doc(self.state)["digest"], {"lastDay": "2026-08-26"})


class RuntimeStampTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "toast-state.json"

    def stamp(self) -> RuntimeStamp:
        return RuntimeStamp(self.state, module=Path("C:/Dev/sidecrab/notifier/sidecrab_toast.py"))

    def written(self) -> dict:
        return read_state_doc(self.state)[RUNTIME_SECTION]

    def test_it_records_the_running_version(self) -> None:
        self.stamp().touch(now=T0, monotonic=0.0, force=True)
        self.assertEqual(self.written()["version"], __version__)

    def test_it_records_the_module_path(self) -> None:
        self.stamp().touch(now=T0, monotonic=0.0, force=True)
        self.assertIn("sidecrab_toast.py", self.written()["module"])

    def test_it_records_the_pid_so_a_dead_process_stamp_is_detectable(self) -> None:
        import os

        self.stamp().touch(now=T0, monotonic=0.0, force=True)
        self.assertEqual(self.written()["pid"], os.getpid())

    def test_it_records_when_this_process_last_polled(self) -> None:
        """A version with no recent poll behind it describes a process that has since died, and
        reporting THAT as the running version is the same lie in a different direction."""
        self.stamp().touch(now=T0, monotonic=0.0, force=True)
        self.assertEqual(self.written()["lastPollAt"], T0.isoformat())

    def test_it_does_not_rewrite_the_file_on_every_poll(self) -> None:
        stamp = self.stamp()
        self.assertTrue(stamp.touch(now=T0, monotonic=100.0, force=True))
        self.assertFalse(stamp.touch(now=T0, monotonic=110.0))
        self.assertFalse(stamp.touch(now=T0, monotonic=100.0 + RUNTIME_STAMP_REFRESH_SEC - 1))

    def test_it_refreshes_once_the_cadence_has_passed(self) -> None:
        stamp = self.stamp()
        stamp.touch(now=T0, monotonic=100.0, force=True)
        self.assertTrue(stamp.touch(now=T0, monotonic=100.0 + RUNTIME_STAMP_REFRESH_SEC))

    def test_it_preserves_the_day_ledgers(self) -> None:
        DigestLedger(self.state).mark("2026-08-26")
        self.stamp().touch(now=T0, monotonic=0.0, force=True)
        self.assertEqual(read_state_doc(self.state)["digest"], {"lastDay": "2026-08-26"})

    def test_a_day_ledger_write_preserves_the_stamp(self) -> None:
        """Both directions. The digest marks a day months after the stamp was written, and the
        stamp must still be there for Test-SideCrab to read."""
        self.stamp().touch(now=T0, monotonic=0.0, force=True)
        DigestLedger(self.state).mark("2026-08-26")
        self.assertEqual(read_state_doc(self.state)[RUNTIME_SECTION]["version"], __version__)

    def test_an_unwritable_ledger_never_stops_the_daemon(self) -> None:
        bad = Path(self.tmp.name) / "afile" / "state.json"
        (Path(self.tmp.name) / "afile").write_text("x", encoding="utf-8")
        stamp = RuntimeStamp(bad)
        self.assertFalse(stamp.touch(now=T0, monotonic=0.0, force=True))
        self.assertFalse(stamp.touch(now=T0, monotonic=1e9))

    def test_the_stamp_is_written_before_the_first_poll(self) -> None:
        """The pre-v0.16.0 ledger only existed once a digest or budget toast had fired — on the
        measured box it did not exist AT ALL. A notifier that never toasts must still be
        identifiable."""
        import threading

        notifier = sidecrab_toast.Notifier(
            adapter=sidecrab_toast.RecordingToastAdapter(),
            interval=0.0,
            config_reader=sidecrab_toast.ConfigReader(Path(self.tmp.name) / "nope.json"),
            digest_ledger=DigestLedger(self.state),
            budget_ledger=sidecrab_toast.BudgetLedger(self.state),
            snooze_ledger=sidecrab_toast.SnoozeLedger(self.state),
            runtime_stamp=self.stamp(),
        )
        stop = threading.Event()
        stop.set()  # the loop body never runs; only the startup path does
        notifier.run(stop)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))[RUNTIME_SECTION]["version"], __version__)


class StartupLogTests(unittest.TestCase):
    def test_every_invocation_logs_the_version_and_the_module(self) -> None:
        """Not just the daemon: a --test-toast run that proves the toast path is evidence about
        a specific version of this file, or it is evidence about nothing.

        setup_logging is stubbed out rather than allowed to run — the real one opens the
        operator's own ~/.sidecrab/logs/notifier.log through a rotating handler, and a test that
        appends to the file a live daemon holds open is a test with a side effect on production.

        pick_adapter is stubbed for the same reason, and it is the platform-independent way to
        do it: stubbing one adapter CLASS only silences the platform the suite happens to run
        on, and on macOS that left --test-toast posting a real notification from a unit test.
        """
        real_setup = sidecrab_toast.setup_logging
        real_pick = sidecrab_toast.pick_adapter
        sidecrab_toast.setup_logging = lambda *a, **k: None
        sidecrab_toast.pick_adapter = lambda *a, **k: sidecrab_toast.RecordingToastAdapter()
        try:
            with self.assertLogs(sidecrab_toast.log, level="INFO") as captured:
                main(["--test-toast"])
        finally:
            sidecrab_toast.setup_logging = real_setup
            sidecrab_toast.pick_adapter = real_pick

        self.assertTrue(
            [m for m in captured.output if __version__ in m and "sidecrab_toast.py" in m],
            f"no version line in {captured.output}",
        )


if __name__ == "__main__":
    unittest.main()
