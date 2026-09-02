"""Headless unit tests for the daily digest (v0.8.0).

Same rules as test_decider.py: stdlib unittest only, no Windows, no clock of its own — the
decider is handed a local `now` and a ledger value, so every case here is deterministic.

    python -m unittest discover -s notifier/tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sidecrab_toast  # noqa: E402
from sidecrab_toast import (  # noqa: E402
    DIGEST_TITLE,
    ConfigReader,
    DigestConfig,
    DigestDecider,
    DigestLedger,
    Notifier,
    PowerShellToastAdapter,
    RecordingToastAdapter,
    SnoozeLedger,
    ToastConfig,
    build_digest_request,
    find_week_row,
    parse_digest_config,
)

#: A local Wednesday. Yesterday is 2026-08-25.
DAY = "2026-08-26"
YESTERDAY = "2026-08-25"


def local(hour: int, minute: int, day: int = 26) -> datetime:
    """A LOCAL wall-clock stamp — the decider only reads .date()/.hour/.minute."""
    return datetime(2026, 8, day, hour, minute)


def week_feed(*, done: int = 3, commits: int = 40, day: str = YESTERDAY, quiet: bool = False) -> dict:
    return {
        "schema": 5,
        "sessions": [],
        "quiet": {"active": quiet, "start": "22:00", "end": "07:00"},
        "recap": {"week": [{"day": "2026-08-24", "done": 9, "commits": 73},
                           {"day": day, "done": done, "commits": commits}]},
    }


ARMED = DigestConfig(enabled=True, minute_of_day=9 * 60)  # 09:00


# ---------------------------------------------------------------------------- config


class DigestConfigTests(unittest.TestCase):
    def test_absent_block_is_off(self) -> None:
        self.assertEqual(parse_digest_config({"toast": {"enabled": True}}), DigestConfig())
        self.assertFalse(parse_digest_config({}).armed)

    def test_values_are_read(self) -> None:
        cfg = parse_digest_config({"digest": {"enabled": True, "time": "07:30"}})
        self.assertTrue(cfg.armed)
        self.assertEqual(cfg.minute_of_day, 7 * 60 + 30)

    def test_midnight_and_last_minute_are_valid(self) -> None:
        self.assertEqual(parse_digest_config({"digest": {"enabled": True, "time": "00:00"}}).minute_of_day, 0)
        self.assertEqual(parse_digest_config({"digest": {"enabled": True, "time": "23:59"}}).minute_of_day, 1439)

    def test_disabled_with_a_good_time_is_not_armed(self) -> None:
        cfg = parse_digest_config({"digest": {"enabled": False, "time": "09:00"}})
        self.assertFalse(cfg.armed)
        self.assertEqual(cfg.minute_of_day, 540)  # parsed, just not armed

    def test_a_bad_time_disarms_rather_than_defaulting(self) -> None:
        """A wrong THRESHOLD is a nuisance; a wrong TIME fires a daily toast nobody asked for."""
        for bad in ("24:00", "9:00", "09:60", "0900", "09:00:00", "", "  ", "nine", None, 900, True, ["09:00"]):
            with self.subTest(value=bad):
                cfg = parse_digest_config({"digest": {"enabled": True, "time": bad}})
                self.assertIsNone(cfg.minute_of_day)
                self.assertFalse(cfg.armed)

    def test_enabled_must_be_a_real_bool(self) -> None:
        self.assertFalse(parse_digest_config({"digest": {"enabled": "yes", "time": "09:00"}}).enabled)
        self.assertFalse(parse_digest_config({"digest": {"enabled": 1, "time": "09:00"}}).enabled)

    def test_non_dict_documents_and_blocks(self) -> None:
        for bad in (None, [], "x", 3, {"digest": "09:00"}, {"digest": []}):
            with self.subTest(value=bad):
                self.assertEqual(parse_digest_config(bad), DigestConfig())

    def test_reader_serves_both_blocks_from_one_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps({"toast": {"thresholdSec": 45}, "digest": {"enabled": True, "time": "18:15"}}),
                encoding="utf-8",
            )
            reader = ConfigReader(path)
            self.assertEqual(reader.read(), ToastConfig(True, 45))
            self.assertEqual(reader.read_digest().minute_of_day, 18 * 60 + 15)

    def test_reader_reloads_the_digest_block_on_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"digest": {"enabled": False, "time": "09:00"}}), encoding="utf-8")
            reader = ConfigReader(path)
            self.assertFalse(reader.read_digest().armed)
            path.write_text(json.dumps({"digest": {"enabled": True, "time": "09:00"}}), encoding="utf-8")
            self.assertTrue(reader.read_digest().armed)

    def test_reader_still_never_creates_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            ConfigReader(path).read_digest()
            self.assertFalse(path.exists(), "notifier must never write crabd's config")


# ---------------------------------------------------------------------------- schedule


class DigestScheduleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = DigestDecider()

    def decide(self, now, state=None, config=ARMED, last=None):
        return self.decider.evaluate(week_feed() if state is None else state, now, config, last)

    def test_one_minute_early_is_not_due_and_is_not_marked(self) -> None:
        d = self.decide(local(8, 59))
        self.assertIsNone(d.request)
        self.assertIsNone(d.day, "an early poll must leave the day armed")

    def test_exactly_on_the_minute_fires(self) -> None:
        d = self.decide(local(9, 0))
        self.assertIsNotNone(d.request)
        self.assertEqual(d.day, DAY)

    def test_a_minute_late_fires(self) -> None:
        self.assertIsNotNone(self.decide(local(9, 1)).request)

    def test_a_late_start_still_delivers_the_same_day(self) -> None:
        """Logging in at 4pm with a 09:00 digest gets it once, not never and not twice."""
        d = self.decide(local(16, 0))
        self.assertIsNotNone(d.request)
        self.assertEqual(d.day, DAY)

    def test_midnight_digest_is_due_all_day(self) -> None:
        midnight = DigestConfig(enabled=True, minute_of_day=0)
        self.assertIsNotNone(self.decide(local(0, 0), config=midnight).request)

    def test_disabled_never_fires_and_never_marks(self) -> None:
        for cfg in (DigestConfig(), DigestConfig(enabled=False, minute_of_day=540)):
            with self.subTest(config=cfg):
                d = self.decide(local(12, 0), config=cfg)
                self.assertIsNone(d.request)
                self.assertIsNone(d.day, "disabling must not consume days it never ran")

    def test_enabled_without_a_usable_time_never_fires(self) -> None:
        d = self.decide(local(12, 0), config=DigestConfig(enabled=True, minute_of_day=None))
        self.assertIsNone(d.request)
        self.assertIsNone(d.day)


class DigestDedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = DigestDecider()

    def test_a_marked_day_never_fires_again(self) -> None:
        d = self.decider.evaluate(week_feed(), local(9, 0), ARMED, DAY)
        self.assertIsNone(d.request)
        self.assertIsNone(d.day)

    def test_the_poll_after_firing_is_silent(self) -> None:
        first = self.decider.evaluate(week_feed(), local(9, 0), ARMED, None)
        self.assertIsNotNone(first.request)
        second = self.decider.evaluate(week_feed(), local(9, 0, 26), ARMED, first.day)
        self.assertIsNone(second.request)

    def test_the_next_day_re_arms(self) -> None:
        d = self.decider.evaluate(week_feed(day="2026-08-26"), local(9, 0, day=27), ARMED, DAY)
        self.assertIsNotNone(d.request)
        self.assertEqual(d.day, "2026-08-27")

    def test_a_restart_mid_day_does_not_re_toast(self) -> None:
        """The whole reason the mark is on disk: SideCrab-toast restarts are routine."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toast-state.json"
            before = DigestLedger(path)
            first = DigestDecider().evaluate(week_feed(), local(9, 0), ARMED, before.last_day())
            self.assertIsNotNone(first.request)
            before.mark(first.day)

            # A brand new process: fresh decider, fresh ledger object, same file.
            after = DigestLedger(path)
            self.assertEqual(after.last_day(), DAY)
            second = DigestDecider().evaluate(week_feed(), local(9, 5), ARMED, after.last_day())
            self.assertIsNone(second.request)
            self.assertIsNone(second.day)


class DigestQuietTests(unittest.TestCase):
    def test_quiet_marks_the_day_instead_of_deferring(self) -> None:
        d = DigestDecider().evaluate(week_feed(quiet=True), local(9, 0), ARMED, None)
        self.assertIsNone(d.request, "quiet hours are silent, not queued")
        self.assertEqual(d.day, DAY, "and the day is consumed, so nothing bursts when quiet ends")

    def test_a_quiet_skip_is_not_retried_after_quiet_lifts(self) -> None:
        first = DigestDecider().evaluate(week_feed(quiet=True), local(9, 0), ARMED, None)
        second = DigestDecider().evaluate(week_feed(quiet=False), local(9, 30), ARMED, first.day)
        self.assertIsNone(second.request)


class DigestMissingRecapTests(unittest.TestCase):
    """Absent recap / week / row: skip silently, mark, retry tomorrow."""

    def assert_skipped_and_marked(self, state) -> None:
        d = DigestDecider().evaluate(state, local(9, 0), ARMED, None)
        self.assertIsNone(d.request)
        self.assertEqual(d.day, DAY)

    def test_recap_null(self) -> None:
        self.assert_skipped_and_marked({"schema": 5, "recap": None})

    def test_recap_absent(self) -> None:
        self.assert_skipped_and_marked({"schema": 5})

    def test_week_absent(self) -> None:
        self.assert_skipped_and_marked({"schema": 5, "recap": {"sessionsToday": 2}})

    def test_week_not_a_list(self) -> None:
        self.assert_skipped_and_marked({"schema": 5, "recap": {"week": {"day": YESTERDAY}}})

    def test_week_has_no_row_for_yesterday(self) -> None:
        self.assert_skipped_and_marked(week_feed(day="2026-08-20"))

    def test_row_counts_must_be_integers(self) -> None:
        for done, commits in (("3", 40), (3, "40"), (None, 40), (3, None), (True, 40), (3, False)):
            with self.subTest(done=done, commits=commits):
                self.assert_skipped_and_marked(week_feed(done=done, commits=commits))

    def test_a_non_dict_state_is_skipped(self) -> None:
        for bad in (None, [], "x"):
            with self.subTest(value=bad):
                self.assert_skipped_and_marked(bad)


# ---------------------------------------------------------------------------- content


class DigestContentTests(unittest.TestCase):
    def test_title_and_body_match_the_contract(self) -> None:
        d = DigestDecider().evaluate(week_feed(done=3, commits=40), local(9, 0), ARMED, None)
        self.assertEqual(d.request.title, DIGEST_TITLE)
        self.assertEqual(d.request.title, "SideCrab — yesterday")
        self.assertEqual(d.request.body, "3 done · 40 commits")

    def test_zero_is_reported_honestly(self) -> None:
        req = build_digest_request({"day": YESTERDAY, "done": 0, "commits": 0}, YESTERDAY)
        self.assertEqual(req.body, "0 done · 0 commits")

    def test_the_request_is_keyed_on_yesterday_not_today(self) -> None:
        d = DigestDecider().evaluate(week_feed(), local(9, 0), ARMED, None)
        self.assertEqual(d.request.state_since, YESTERDAY)
        self.assertIn(YESTERDAY, d.request.session_id)

    def test_yesterday_crosses_a_month_boundary(self) -> None:
        state = {"schema": 5, "recap": {"week": [{"day": "2026-07-31", "done": 1, "commits": 2}]}}
        d = DigestDecider().evaluate(state, datetime(2026, 8, 1, 9, 0), ARMED, None)
        self.assertEqual(d.request.state_since, "2026-07-31")

    def test_find_week_row_ignores_malformed_entries(self) -> None:
        state = {"recap": {"week": ["nope", None, 7, {"day": YESTERDAY, "done": 1, "commits": 1}]}}
        self.assertIsNotNone(find_week_row(state, YESTERDAY))

    def test_the_digest_carries_no_acknowledge_button(self) -> None:
        """There is no session to ack — a button POSTing a made-up id would only 404."""
        d = DigestDecider().evaluate(week_feed(), local(9, 0), ARMED, None)
        xml = PowerShellToastAdapter(icon_path=None).build_xml(d.request)
        self.assertFalse(d.request.actionable)
        self.assertNotIn("<actions>", xml)
        self.assertNotIn("sidecrab-ack", xml)
        self.assertIn("SideCrab", xml)

    def test_a_waiting_toast_still_carries_its_button(self) -> None:
        """Guards the guard: `actionable` must not have switched the button off for everyone."""
        from sidecrab_toast import ToastRequest

        xml = PowerShellToastAdapter(icon_path=None).build_xml(ToastRequest("s1", "t", "a", "b"))
        self.assertIn("<actions>", xml)
        self.assertIn("sidecrab-ack:s1", xml)

    def test_the_digest_gets_its_own_action_center_tag(self) -> None:
        """A shared Tag would let the digest replace a question that is still waiting."""
        adapter = PowerShellToastAdapter(icon_path=None, aumid="X")
        d = DigestDecider().evaluate(week_feed(), local(9, 0), ARMED, None)
        self.assertNotIn("$toast.Tag = 's1'", adapter.build_script(d.request))
        self.assertIn(f"$toast.Tag = 'digest-{YESTERDAY}'", adapter.build_script(d.request))


# ---------------------------------------------------------------------------- ledger


class DigestLedgerTests(unittest.TestCase):
    def test_missing_file_reads_as_unmarked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(DigestLedger(Path(tmp) / "nope.json").last_day())

    def test_round_trips_through_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toast-state.json"
            DigestLedger(path).mark(DAY)
            self.assertEqual(DigestLedger(path).last_day(), DAY)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"digest": {"lastDay": DAY}})

    def test_corrupt_state_reads_as_unmarked_rather_than_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toast-state.json"
            for junk in ("{ half-written", "[]", '{"digest": "yes"}', '{"digest": {"lastDay": 4}}'):
                with self.subTest(junk=junk):
                    path.write_text(junk, encoding="utf-8")
                    self.assertIsNone(DigestLedger(path).last_day())

    def test_mark_overwrites_the_previous_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toast-state.json"
            ledger = DigestLedger(path)
            ledger.mark("2026-08-25")
            ledger.mark(DAY)
            self.assertEqual(DigestLedger(path).last_day(), DAY)

    def test_mark_leaves_no_temp_file_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toast-state.json"
            DigestLedger(path).mark(DAY)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["toast-state.json"])

    def test_an_unwritable_ledger_never_raises(self) -> None:
        """A daemon must outlive its state file. Worst case is one duplicate digest."""
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocked"
            blocker.write_text("i am a file, not a directory", encoding="utf-8")
            ledger = DigestLedger(blocker / "toast-state.json")
            ledger.mark(DAY)  # must not raise
            self.assertEqual(ledger.last_day(), DAY, "in-memory mark still holds for this process")

    def test_the_ledger_is_not_the_config_file(self) -> None:
        """Structural: crabd owns config.json, and two writers on it is how it gets corrupted."""
        self.assertNotEqual(sidecrab_toast.STATE_PATH, sidecrab_toast.CONFIG_PATH)
        self.assertEqual(sidecrab_toast.STATE_PATH.name, "toast-state.json")


# ---------------------------------------------------------------------------- poll loop


class DigestPollTests(unittest.TestCase):
    """The digest riding the existing 10 s poll — no thread, no timer."""

    def setUp(self) -> None:
        self._real_fetch = sidecrab_toast.fetch_state
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, sidecrab_toast, "fetch_state", self._real_fetch)

    def build(self, state, digest_time="09:00"):
        sidecrab_toast.fetch_state = lambda *a, **k: state
        root = Path(self.tmp.name)
        config = root / "config.json"
        config.write_text(
            json.dumps({"toast": {"enabled": True}, "digest": {"enabled": True, "time": digest_time}}),
            encoding="utf-8",
        )
        adapter = RecordingToastAdapter()
        notifier = Notifier(
            adapter=adapter,
            config_reader=ConfigReader(config),
            digest_ledger=DigestLedger(root / "toast-state.json"),
            snooze_ledger=SnoozeLedger(root / "toast-state.json"),
        )
        return notifier, adapter

    def test_a_due_digest_reaches_the_adapter(self) -> None:
        notifier, adapter = self.build(week_feed())
        fired = notifier.poll_once(now=datetime(2026, 8, 26, 9, 30).astimezone())
        self.assertEqual(len(fired), 1)
        self.assertEqual(adapter.shown[0].title, DIGEST_TITLE)

    def test_the_second_poll_of_the_day_is_silent(self) -> None:
        notifier, adapter = self.build(week_feed())
        when = datetime(2026, 8, 26, 9, 30).astimezone()
        notifier.poll_once(now=when)
        notifier.poll_once(now=when)
        self.assertEqual(len(adapter.shown), 1)

    def test_before_the_hour_nothing_fires(self) -> None:
        notifier, adapter = self.build(week_feed(), digest_time="23:00")
        notifier.poll_once(now=datetime(2026, 8, 26, 9, 30).astimezone())
        self.assertEqual(adapter.shown, [])

    def test_crabd_unreachable_does_not_consume_the_day(self) -> None:
        """A restarting crabd costs a poll, not the digest."""
        notifier, adapter = self.build(None)
        notifier.poll_once(now=datetime(2026, 8, 26, 9, 30).astimezone())
        self.assertIsNone(notifier.digest_ledger.last_day())

        sidecrab_toast.fetch_state = lambda *a, **k: week_feed()
        fired = notifier.poll_once(now=datetime(2026, 8, 26, 9, 31).astimezone())
        self.assertEqual(len(fired), 1)

    def test_an_unsupported_schema_stands_down_from_the_digest_too(self) -> None:
        notifier, adapter = self.build({**week_feed(), "schema": 99})
        notifier.poll_once(now=datetime(2026, 8, 26, 9, 30).astimezone())
        self.assertEqual(adapter.shown, [])
        self.assertIsNone(notifier.digest_ledger.last_day())

    def test_a_failed_digest_toast_still_consumes_the_day(self) -> None:
        """Otherwise a broken toast path retries every 10 s for the rest of the day."""
        notifier, _ = self.build(week_feed())
        notifier.adapter = RecordingToastAdapter(succeed=False)
        when = datetime(2026, 8, 26, 9, 30).astimezone()
        self.assertEqual(notifier.poll_once(now=when), [])
        self.assertEqual(notifier.digest_ledger.last_day(), "2026-08-26")
        self.assertEqual(len(notifier.adapter.shown), 1)
        notifier.poll_once(now=when)
        self.assertEqual(len(notifier.adapter.shown), 1)

    def test_a_waiting_session_and_the_digest_can_both_fire_in_one_poll(self) -> None:
        state = week_feed()
        state["sessions"] = [{
            "id": "s1", "title": "lane", "state": "needs_input",
            "stateSince": "2026-08-26T00:00:00Z", "question": "go?", "acked": False,
        }]
        notifier, adapter = self.build(state)
        # Naive -> .astimezone() reads it as LOCAL 09:30, which is what the digest schedules on.
        fired = notifier.poll_once(now=datetime(2026, 8, 26, 9, 30).astimezone())
        self.assertEqual(len(fired), 2)
        self.assertEqual(fired[1].title, DIGEST_TITLE)
        self.assertTrue(fired[0].actionable, "the waiting-session toast keeps its Acknowledge button")


if __name__ == "__main__":
    unittest.main(verbosity=2)
