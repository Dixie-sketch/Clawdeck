"""Headless unit tests for the long-run completion toast (v0.16.0).

The turn duration does not exist in any single feed document — crabd clears ``turnStartedAt``
on Stop — so every test here is really about the ONE-POLL MEMORY: what the decider remembers,
what it refuses to guess, and what it stays silent about.

Stdlib unittest only, no Windows, no clock of its own.

    python -m unittest discover -s notifier/tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sidecrab_toast  # noqa: E402
from sidecrab_toast import (  # noqa: E402
    DEFAULT_LONG_RUN_SEC,
    LONG_RUN_ID_PREFIX,
    LONG_RUN_PER_SESSION_CAP,
    LONG_RUN_PRUNE_GRACE,
    BudgetLedger,
    ConfigReader,
    DigestLedger,
    LongRunDecider,
    Notifier,
    RecordingToastAdapter,
    SnoozeLedger,
    ToastConfig,
    build_long_run_request,
    format_duration,
    parse_toast_config,
    read_turn,
)

T0 = datetime(2026, 8, 27, 9, 0, 0, tzinfo=timezone.utc)


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def working(turn_at: datetime | None = T0, *, sid: str = "s1", title: str = "the lane") -> dict:
    """A session mid-turn. ``turn_at=None`` is crabd's reactivation path — MEASURED on the live
    feed 2026-08-27: a `working` row whose turn was already cleared by an earlier Stop."""
    return {
        "id": sid,
        "title": title,
        "state": "working",
        "stateSince": iso(T0),
        "turnStartedAt": iso(turn_at) if turn_at is not None else None,
    }


def done(at: datetime, *, sid: str = "s1", title: str = "the lane") -> dict:
    """A finished session. turnStartedAt is null by contract — that is the whole problem."""
    return {
        "id": sid,
        "title": title,
        "state": "done",
        "stateSince": iso(at),
        "turnStartedAt": None,
    }


def feed(*sessions: dict, quiet: bool = False) -> dict:
    return {
        "schema": 5,
        "generatedAt": iso(T0),
        "sessions": list(sessions),
        "quiet": {"active": quiet, "start": "22:00", "end": "07:00"},
    }


LONG = T0 + timedelta(seconds=DEFAULT_LONG_RUN_SEC + 60)
SHORT = T0 + timedelta(seconds=DEFAULT_LONG_RUN_SEC - 60)


# ------------------------------------------------------------------------------- config


class ConfigTests(unittest.TestCase):
    def test_the_default_is_fifteen_minutes(self) -> None:
        self.assertEqual(ToastConfig().long_run_sec, 900)
        self.assertEqual(DEFAULT_LONG_RUN_SEC, 900)

    def test_a_configured_value_is_read(self) -> None:
        self.assertEqual(parse_toast_config({"toast": {"longRunSec": 300}}).long_run_sec, 300)

    def test_zero_is_kept_because_zero_means_off(self) -> None:
        """Not clamped to the default: 0 is the switch, and the decider honours it."""
        self.assertEqual(parse_toast_config({"toast": {"longRunSec": 0}}).long_run_sec, 0)

    def test_a_negative_value_falls_back_to_the_default(self) -> None:
        self.assertEqual(parse_toast_config({"toast": {"longRunSec": -1}}).long_run_sec, 900)

    def test_a_string_falls_back_to_the_default(self) -> None:
        self.assertEqual(parse_toast_config({"toast": {"longRunSec": "900"}}).long_run_sec, 900)

    def test_true_is_a_typo_not_a_threshold(self) -> None:
        self.assertEqual(parse_toast_config({"toast": {"longRunSec": True}}).long_run_sec, 900)

    def test_a_bad_long_run_does_not_poison_the_other_thresholds(self) -> None:
        config = parse_toast_config({"toast": {"longRunSec": "nope", "thresholdSec": 30}})
        self.assertEqual(config.long_run_sec, 900)
        self.assertEqual(config.threshold_sec, 30)


# -------------------------------------------------------------------------- reading a row


class ReadTurnTests(unittest.TestCase):
    def test_a_running_turn_is_read(self) -> None:
        sid, observation = read_turn(working())
        self.assertEqual(sid, "s1")
        self.assertEqual(observation.state, "working")
        self.assertEqual(observation.turn_started_at, iso(T0))

    def test_a_cleared_turn_reads_as_none(self) -> None:
        _, observation = read_turn(done(LONG))
        self.assertIsNone(observation.turn_started_at)

    def test_an_empty_string_turn_reads_as_none(self) -> None:
        _, observation = read_turn(working(sid="s1") | {"turnStartedAt": "   "})
        self.assertIsNone(observation.turn_started_at)

    def test_a_row_with_no_id_is_dropped(self) -> None:
        self.assertIsNone(read_turn({"state": "done", "stateSince": iso(LONG)}))

    def test_a_row_with_a_non_string_state_is_dropped(self) -> None:
        self.assertIsNone(read_turn({"id": "s1", "state": 3}))

    def test_a_non_dict_is_dropped(self) -> None:
        self.assertIsNone(read_turn("session"))


# ------------------------------------------------------------------------------ duration


class DurationTests(unittest.TestCase):
    def test_minutes_below_an_hour(self) -> None:
        self.assertEqual(format_duration(22 * 60 + 14), "22m")

    def test_whole_hours_drop_the_minutes(self) -> None:
        self.assertEqual(format_duration(7200), "2h")

    def test_hours_and_minutes(self) -> None:
        self.assertEqual(format_duration(3600 + 22 * 60), "1h 22m")

    def test_it_truncates_rather_than_rounds_up(self) -> None:
        """59 s is not a minute. A toast that says '1m' about a 59 s turn is a small lie in
        the one number the toast exists to report."""
        self.assertEqual(format_duration(59), "0m")

    def test_a_negative_duration_never_renders_a_negative_number(self) -> None:
        self.assertEqual(format_duration(-10), "0m")


# ---------------------------------------------------------------------------- the request


class BuildTests(unittest.TestCase):
    def test_the_title_carries_the_duration_and_the_session(self) -> None:
        request = build_long_run_request(working(title="the widget lane"), "s1", T0, LONG)
        self.assertEqual(request.title, "Finished after 16m \u2014 the widget lane")

    def test_a_titleless_session_is_never_invented(self) -> None:
        request = build_long_run_request({"id": "s1"}, "s1", T0, LONG)
        self.assertIn("a Claude session", request.title)

    def test_a_long_title_is_trimmed(self) -> None:
        request = build_long_run_request(working(title="x" * 200), "s1", T0, LONG)
        self.assertIn("\u2026", request.title)
        self.assertLessEqual(len(request.title.split("\u2014")[1].strip()), 48)

    def test_the_body_is_the_two_clock_times(self) -> None:
        request = build_long_run_request(working(), "s1", T0, LONG)
        self.assertRegex(request.body, r"^Started \d\d:\d\d, finished \d\d:\d\d\.$")

    def test_it_gets_its_own_action_center_slot(self) -> None:
        """A completion must never replace a question that is still waiting."""
        request = build_long_run_request(working(), "s1", T0, LONG)
        self.assertTrue(request.session_id.startswith(LONG_RUN_ID_PREFIX))
        self.assertNotEqual(request.session_id, "s1")

    def test_it_carries_no_buttons(self) -> None:
        """Nothing to acknowledge and nothing to snooze — the turn is over."""
        request = build_long_run_request(working(), "s1", T0, LONG)
        self.assertFalse(request.actionable)
        xml = sidecrab_toast.PowerShellToastAdapter(aumid="x").build_xml(request)
        self.assertNotIn("<actions>", xml)


# ----------------------------------------------------------------------------- the gates


class DeciderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = LongRunDecider()
        self.config = ToastConfig()

    def run_polls(self, *states: dict, config: ToastConfig | None = None) -> list:
        """Feed the decider a sequence of polls; return what the LAST one owed."""
        owed: list = []
        for state in states:
            owed = self.decider.evaluate(state, T0, config or self.config)
        return owed

    # -- it fires ----------------------------------------------------------------------

    def test_a_long_turn_finishing_toasts_once(self) -> None:
        owed = self.run_polls(feed(working()), feed(done(LONG)))
        self.assertEqual(len(owed), 1)
        self.assertEqual(owed[0].title, "Finished after 16m \u2014 the lane")

    def test_the_duration_comes_from_the_previous_poll_not_from_now(self) -> None:
        """The whole design in one assertion: the done row has no turnStartedAt, so a decider
        without the memory could only have guessed."""
        self.decider.evaluate(feed(working(T0)), T0, self.config)
        owed = self.decider.evaluate(feed(done(T0 + timedelta(seconds=5400))), T0, self.config)
        self.assertEqual(owed[0].title, "Finished after 1h 30m \u2014 the lane")

    def test_two_sessions_finishing_in_one_poll_get_one_toast_each(self) -> None:
        self.decider.evaluate(feed(working(sid="a"), working(sid="b")), T0, self.config)
        owed = self.decider.evaluate(feed(done(LONG, sid="a"), done(LONG, sid="b")), T0, self.config)
        self.assertEqual(len(owed), 2)

    # -- it stays silent ---------------------------------------------------------------

    def test_a_short_turn_is_silent(self) -> None:
        self.assertEqual(self.run_polls(feed(working()), feed(done(SHORT))), [])

    def test_a_turn_exactly_at_the_threshold_fires(self) -> None:
        """>= is the boundary, matching every other threshold in this file."""
        exact = T0 + timedelta(seconds=DEFAULT_LONG_RUN_SEC)
        self.assertEqual(len(self.run_polls(feed(working()), feed(done(exact)))), 1)

    def test_a_session_still_working_is_silent(self) -> None:
        self.assertEqual(self.run_polls(feed(working()), feed(working())), [])

    def test_the_first_poll_of_a_fresh_notifier_is_silent(self) -> None:
        """Started mid-turn: no previous observation, so no honest duration exists."""
        self.assertEqual(self.run_polls(feed(done(LONG))), [])

    def test_a_working_row_with_no_turn_is_silent(self) -> None:
        """crabd's reactivation path — `done` -> `working` because the transcript moved, with
        the turn already cleared. There is no start instant, so there is no toast."""
        self.assertEqual(self.run_polls(feed(working(None)), feed(done(LONG))), [])

    def test_a_needs_input_spell_is_not_counted_as_running(self) -> None:
        """turnStartedAt survives working -> needs_input, so this row LOOKS usable. Counting it
        would report the operator's own thinking time back to him as compute."""
        waiting = working() | {"state": "needs_input"}
        self.assertEqual(self.run_polls(feed(waiting), feed(done(LONG))), [])

    def test_an_unparseable_finish_stamp_is_refused_not_approximated(self) -> None:
        broken = done(LONG) | {"stateSince": "whenever"}
        self.assertEqual(self.run_polls(feed(working()), feed(broken)), [])

    def test_an_unparseable_start_stamp_is_refused(self) -> None:
        broken = working() | {"turnStartedAt": "whenever"}
        self.assertEqual(self.run_polls(feed(broken), feed(done(LONG))), [])

    def test_a_finish_before_its_start_is_refused(self) -> None:
        backwards = done(T0 - timedelta(seconds=5000))
        self.assertEqual(self.run_polls(feed(working()), feed(backwards)), [])

    def test_a_non_dict_state_is_survived(self) -> None:
        self.assertEqual(self.decider.evaluate("nope", T0, self.config), [])

    def test_a_missing_sessions_array_is_survived(self) -> None:
        self.assertEqual(self.decider.evaluate({"schema": 5}, T0, self.config), [])

    # -- dedupe ------------------------------------------------------------------------

    def test_a_done_row_lingering_does_not_re_toast(self) -> None:
        """crabd holds a done row for many polls. One toast, not one every ten seconds."""
        self.decider.evaluate(feed(working()), T0, self.config)
        first = self.decider.evaluate(feed(done(LONG)), T0, self.config)
        self.assertEqual(len(first), 1)
        for _ in range(5):
            self.assertEqual(self.decider.evaluate(feed(done(LONG)), T0, self.config), [])

    def test_a_flap_back_to_working_and_done_does_not_re_toast_the_same_turn(self) -> None:
        self.decider.evaluate(feed(working()), T0, self.config)
        self.assertEqual(len(self.decider.evaluate(feed(done(LONG)), T0, self.config)), 1)
        self.decider.evaluate(feed(working(T0)), T0, self.config)
        self.assertEqual(self.decider.evaluate(feed(done(LONG)), T0, self.config), [])

    def test_a_second_long_turn_is_a_second_toast(self) -> None:
        """A NEW turnStartedAt is a new turn — that is the only thing that re-arms."""
        self.run_polls(feed(working(T0)), feed(done(LONG)))
        later = T0 + timedelta(hours=2)
        owed = self.run_polls(feed(working(later)), feed(done(later + timedelta(seconds=1000))))
        self.assertEqual(len(owed), 1)

    def test_the_mark_ledger_is_capped_per_session(self) -> None:
        for i in range(LONG_RUN_PER_SESSION_CAP + 10):
            start = T0 + timedelta(hours=i)
            self.decider.evaluate(feed(working(start)), T0, self.config)
            self.decider.evaluate(feed(done(start + timedelta(seconds=1000))), T0, self.config)
        self.assertLessEqual(len(self.decider._marks["s1"]), LONG_RUN_PER_SESSION_CAP)

    def test_a_vanished_session_is_pruned_only_after_the_grace(self) -> None:
        self.run_polls(feed(working()), feed(done(LONG)))
        for _ in range(LONG_RUN_PRUNE_GRACE - 1):
            self.decider.evaluate(feed(), T0, self.config)
        self.assertIn("s1", self.decider._marks)
        self.decider.evaluate(feed(), T0, self.config)
        self.assertNotIn("s1", self.decider._marks)

    # -- the switches ------------------------------------------------------------------

    def test_quiet_hours_suppress_the_toast(self) -> None:
        self.decider.evaluate(feed(working(), quiet=True), T0, self.config)
        self.assertEqual(self.decider.evaluate(feed(done(LONG), quiet=True), T0, self.config), [])

    def test_quiet_hours_MARK_the_turn_so_it_cannot_arrive_late(self) -> None:
        """The mark is what makes quiet SILENT rather than DEFERRED, and proving it needs the
        one path that revisits a decided turn: crabd's reactivation flap, where a done row goes
        back to `working` because the transcript moved and then finishes again on the SAME
        turnStartedAt. Without the mark that flap delivers the 03:00 run at 07:00.

        Written this way after a mutation run: the obvious version of this test (quiet poll,
        then a loud poll on the same done row) passes with the mark deleted, because the
        working -> done EDGE has already been consumed. It proved nothing."""
        self.decider.evaluate(feed(working(T0), quiet=True), T0, self.config)
        self.assertEqual(self.decider.evaluate(feed(done(LONG), quiet=True), T0, self.config), [])
        # Quiet lifts, and the flap re-presents the very same turn.
        self.decider.evaluate(feed(working(T0)), T0, self.config)
        self.assertEqual(self.decider.evaluate(feed(done(LONG)), T0, self.config), [])

    def test_zero_is_off_not_toast_everything(self) -> None:
        """The 'would this fire on a healthy night?' answer. A 0 threshold reading as 'every
        completed turn' would toast on every prompt on the box."""
        off = ToastConfig(long_run_sec=0)
        self.assertEqual(self.run_polls(feed(working()), feed(done(LONG)), config=off), [])
        self.assertEqual(self.run_polls(feed(working()), feed(done(SHORT)), config=off), [])

    def test_disabled_toasts_nothing(self) -> None:
        off = ToastConfig(enabled=False)
        self.assertEqual(self.run_polls(feed(working()), feed(done(LONG)), config=off), [])

    def test_the_observation_is_taken_even_while_disabled(self) -> None:
        """The reading is unconditional; only the DECISION is gated. Skipping the observation
        while off would leave the memory holding a turn start from whenever it was last on, and
        the first completion after a re-enable would be measured against it — a confidently
        wrong duration in the one toast that is nothing but a duration. So enabling mid-turn
        delivers that turn, which is true and current."""
        off = ToastConfig(enabled=False)
        self.decider.evaluate(feed(working()), T0, off)
        owed = self.decider.evaluate(feed(done(LONG)), T0, self.config)
        self.assertEqual(len(owed), 1)

    def test_a_stale_observation_can_never_outlive_a_disabled_spell(self) -> None:
        """The failure the unconditional swap exists to prevent: a turn observed while off,
        then a long quiet spell, then a done row. The duration must come from the poll
        immediately before the done, never from the last poll the feature happened to be on."""
        off = ToastConfig(enabled=False)
        self.decider.evaluate(feed(working(T0)), T0, off)
        for _ in range(3):
            self.decider.evaluate(feed(working(None)), T0, off)  # reactivation path, no turn
        self.assertEqual(self.decider.evaluate(feed(done(LONG)), T0, self.config), [])


# ------------------------------------------------------------------------ through Notifier


class NotifierTests(unittest.TestCase):
    """The wiring: the decider is reached by a real poll, on the real config path."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._fetch = sidecrab_toast.fetch_state
        self.addCleanup(lambda: setattr(sidecrab_toast, "fetch_state", self._fetch))

    def build(self, config_doc: dict | None = None):
        root = Path(self.tmp.name)
        config = root / "config.json"
        if config_doc is not None:
            config.write_text(json.dumps(config_doc), encoding="utf-8")
        adapter = RecordingToastAdapter()
        notifier = Notifier(
            adapter=adapter,
            config_reader=ConfigReader(config),
            digest_ledger=DigestLedger(root / "toast-state.json"),
            budget_ledger=BudgetLedger(root / "toast-state.json"),
            snooze_ledger=SnoozeLedger(root / "toast-state.json"),
        )
        return notifier, adapter

    def serve(self, state) -> None:
        sidecrab_toast.fetch_state = lambda *a, **k: state

    def test_a_long_turn_reaches_the_adapter(self) -> None:
        notifier, adapter = self.build()
        self.serve(feed(working()))
        notifier.poll_once(now=T0)
        self.serve(feed(done(LONG)))
        fired = notifier.poll_once(now=LONG)
        self.assertEqual(len(fired), 1)
        self.assertTrue(adapter.shown[0].title.startswith("Finished after 16m"))

    def test_the_configured_threshold_is_the_one_used(self) -> None:
        notifier, adapter = self.build({"toast": {"longRunSec": 60}})
        self.serve(feed(working()))
        notifier.poll_once(now=T0)
        self.serve(feed(done(T0 + timedelta(seconds=120))))
        self.assertEqual(len(notifier.poll_once(now=T0)), 1)
        self.assertEqual(adapter.shown[0].title, "Finished after 2m \u2014 the lane")

    def test_an_ordinary_short_turn_is_silent_on_the_real_path(self) -> None:
        """The 'would this fire on a healthy night?' replay. Almost every turn on this box is
        seconds long; if the threshold were ever bypassed, the notifier would toast on every
        prompt the operator submits."""
        notifier, adapter = self.build()
        self.serve(feed(working()))
        notifier.poll_once(now=T0)
        self.serve(feed(done(T0 + timedelta(seconds=8))))
        self.assertEqual(notifier.poll_once(now=T0), [])
        self.assertEqual(adapter.shown, [])

    def test_an_unreachable_crabd_does_not_forget_the_running_turn(self) -> None:
        """A crabd restart is a blip, not the end of a turn. The memory must survive it, or a
        25-minute run that spanned one costs its toast."""
        notifier, _ = self.build()
        self.serve(feed(working()))
        notifier.poll_once(now=T0)
        self.serve(None)
        notifier.poll_once(now=T0)
        self.serve(feed(done(LONG)))
        self.assertEqual(len(notifier.poll_once(now=LONG)), 1)


if __name__ == "__main__":
    unittest.main()
