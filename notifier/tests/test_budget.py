"""Headless unit tests for the burn-budget toast (v0.10.0).

Same rules as test_digest.py: stdlib unittest only, no Windows, no clock of its own — the
decider is handed a local `now` and a ledger value, so every case here is deterministic.

    python -m unittest discover -s notifier/tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sidecrab_toast  # noqa: E402
from sidecrab_toast import (  # noqa: E402
    BUDGET_TITLE,
    BudgetDecider,
    BudgetLedger,
    BudgetReading,
    ConfigReader,
    DigestLedger,
    Notifier,
    PowerShellToastAdapter,
    RecordingToastAdapter,
    SnoozeLedger,
    build_budget_request,
    read_budget,
)

DAY = "2026-08-26"


def local(hour: int = 12, minute: int = 0, day: int = 26) -> datetime:
    """A LOCAL wall-clock stamp — the decider only reads .date()."""
    return datetime(2026, 8, day, hour, minute)


def budget_feed(
    *,
    daily: object = 5_000_000,
    pct: object = 1.34,
    used: object = 6_700_000,
    quiet: bool = False,
    budget: object = "default",
) -> dict:
    block = {"dailyOutputTokens": daily, "todayPct": pct} if budget == "default" else budget
    burn: dict = {"today": {"outputTokens": used, "messages": 5255}}
    if block is not None or budget is not None:
        burn["budget"] = block
    return {
        "schema": 5,
        "sessions": [],
        "quiet": {"active": quiet, "start": "22:00", "end": "07:00"},
        "burn": burn,
    }


# ---------------------------------------------------------------------------- reading the feed


class BudgetReadTests(unittest.TestCase):
    def test_a_well_formed_block_reads(self) -> None:
        reading = read_budget(budget_feed())
        self.assertEqual(reading.daily_output_tokens, 5_000_000)
        self.assertEqual(reading.today_pct, 1.34)
        self.assertEqual(reading.used_output_tokens, 6_700_000)
        self.assertTrue(reading.crossed)

    def test_an_integer_pct_is_accepted(self) -> None:
        """crabd may serialise a whole-number ratio as 1, not 1.0."""
        self.assertTrue(read_budget(budget_feed(pct=1)).crossed)

    def test_absent_budget_reads_as_nothing(self) -> None:
        for state in (
            {"schema": 5},                                    # no burn at all
            {"schema": 5, "burn": None},                      # burn null
            {"schema": 5, "burn": {"today": {}}},             # older crabd: no budget key
            {"schema": 5, "burn": {"budget": None}},          # budget cleared
            {"schema": 5, "burn": {"budget": "5000000"}},     # malformed block
            {"schema": 5, "burn": {"budget": []}},
            None,
            [],
            "x",
        ):
            with self.subTest(state=state):
                self.assertIsNone(read_budget(state))

    def test_a_malformed_daily_reads_as_nothing(self) -> None:
        for daily in ("5000000", None, True, 0, -1, 5.0, [5]):
            with self.subTest(daily=daily):
                self.assertIsNone(read_budget(budget_feed(daily=daily)))

    def test_a_malformed_pct_reads_as_nothing(self) -> None:
        for pct in ("1.34", None, True, -0.1, [1.34], {}):
            with self.subTest(pct=pct):
                self.assertIsNone(read_budget(budget_feed(pct=pct)))

    def test_the_measured_token_count_is_preferred_over_the_derived_one(self) -> None:
        """todayPct is capped at 9.99; burn.today.outputTokens is not, so it wins."""
        reading = read_budget(budget_feed(daily=1_000_000, pct=9.99, used=40_000_000))
        self.assertEqual(reading.used_output_tokens, 40_000_000)

    def test_a_missing_token_count_is_derived_from_the_pct(self) -> None:
        for used in (None, "6700000", True, 0):
            with self.subTest(used=used):
                reading = read_budget(budget_feed(used=used))
                self.assertEqual(reading.used_output_tokens, 6_700_000)

    def test_a_missing_today_block_still_derives(self) -> None:
        state = {"schema": 5, "burn": {"budget": {"dailyOutputTokens": 5_000_000, "todayPct": 0.5}}}
        self.assertEqual(read_budget(state).used_output_tokens, 2_500_000)


# ---------------------------------------------------------------------------- the decision


class BudgetDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = BudgetDecider()

    #: Distinct from None, which is itself a state under test (a non-dict feed).
    DEFAULT = object()

    def decide(self, state=DEFAULT, now=None, last=None):
        return self.decider.evaluate(budget_feed() if state is self.DEFAULT else state, now or local(), last)

    def test_a_crossing_fires_and_marks_the_day(self) -> None:
        d = self.decide()
        self.assertIsNotNone(d.request)
        self.assertEqual(d.day, DAY)

    def test_exactly_one_hundred_percent_fires(self) -> None:
        """The contract says >= 1.0. Exactly at the line is crossed."""
        d = self.decide(budget_feed(pct=1.0, used=5_000_000))
        self.assertIsNotNone(d.request)
        self.assertEqual(d.request.body, "5.0M of 5.0M output tokens (100%)")

    def test_just_under_does_not_fire_and_does_not_mark(self) -> None:
        for pct in (0.99, 0.999, 0.0):
            with self.subTest(pct=pct):
                d = self.decide(budget_feed(pct=pct))
                self.assertIsNone(d.request)
                self.assertIsNone(d.day, "an under-budget poll must leave the day armed")

    def test_a_marked_day_never_fires_again(self) -> None:
        d = self.decide(last=DAY)
        self.assertIsNone(d.request)
        self.assertIsNone(d.day)

    def test_the_poll_after_firing_is_silent(self) -> None:
        first = self.decide()
        second = self.decide(now=local(12, 1), last=first.day)
        self.assertIsNone(second.request)
        self.assertIsNone(second.day)

    def test_the_next_day_re_arms(self) -> None:
        d = self.decide(now=local(day=27), last=DAY)
        self.assertIsNotNone(d.request)
        self.assertEqual(d.day, "2026-08-27")
        self.assertIn("2026-08-27", d.request.session_id)

    def test_a_budget_that_appears_at_noon_still_fires_today(self) -> None:
        """The morning's absent-budget polls must not have consumed the day."""
        morning = self.decide(budget_feed(budget=None), now=local(8, 0))
        self.assertIsNone(morning.day)
        noon = self.decide(now=local(12, 0), last=morning.day)
        self.assertIsNotNone(noon.request)

    def test_an_absent_budget_is_silence_not_a_mark(self) -> None:
        for state in (budget_feed(budget=None), {"schema": 5}, None):
            with self.subTest(state=state):
                d = self.decide(state)
                self.assertIsNone(d.request)
                self.assertIsNone(d.day)


class BudgetQuietTests(unittest.TestCase):
    def test_quiet_marks_the_day_instead_of_deferring(self) -> None:
        d = BudgetDecider().evaluate(budget_feed(quiet=True), local(2, 0), None)
        self.assertIsNone(d.request, "quiet hours are silent, not queued")
        self.assertEqual(d.day, DAY, "and the day is consumed, so nothing fires when quiet ends")

    def test_a_quiet_skip_is_not_retried_after_quiet_lifts(self) -> None:
        first = BudgetDecider().evaluate(budget_feed(quiet=True), local(2, 0), None)
        second = BudgetDecider().evaluate(budget_feed(quiet=False), local(8, 0), first.day)
        self.assertIsNone(second.request)
        self.assertIsNone(second.day)

    def test_quiet_under_budget_still_does_not_consume_the_day(self) -> None:
        """Only a real crossing may be quiet-suppressed — otherwise a quiet night eats the day."""
        d = BudgetDecider().evaluate(budget_feed(pct=0.4, quiet=True), local(2, 0), None)
        self.assertIsNone(d.day)


# ---------------------------------------------------------------------------- content


class BudgetContentTests(unittest.TestCase):
    def test_title_and_body_match_the_contract(self) -> None:
        d = BudgetDecider().evaluate(budget_feed(), local(), None)
        self.assertEqual(d.request.title, BUDGET_TITLE)
        self.assertEqual(d.request.title, "Daily token budget crossed")
        self.assertEqual(d.request.body, "6.7M of 5.0M output tokens (134%)")

    def test_the_percent_is_rounded_not_truncated(self) -> None:
        """1.34 * 100 is 133.99999999999998 in binary floating point."""
        req = build_budget_request(BudgetReading(5_000_000, 1.34, 6_700_000), DAY)
        self.assertIn("(134%)", req.body)

    def test_the_capped_pct_renders_as_999(self) -> None:
        req = build_budget_request(BudgetReading(1_000_000, 9.99, 40_000_000), DAY)
        self.assertEqual(req.body, "40.0M of 1.0M output tokens (999%)")

    def test_a_small_budget_still_renders_in_millions(self) -> None:
        """The contract's unit is millions even at the 100k floor — one decimal, no unit-switching."""
        req = build_budget_request(BudgetReading(100_000, 1.5, 150_000), DAY)
        self.assertEqual(req.body, "0.1M of 0.1M output tokens (150%)")

    def test_the_body_fits_the_toast_trim(self) -> None:
        req = build_budget_request(BudgetReading(100_000_000, 9.99, 999_000_000), DAY)
        self.assertLessEqual(len(req.body), sidecrab_toast.BODY_TRIM)

    def test_it_carries_no_acknowledge_button(self) -> None:
        """There is no session to ack — a button POSTing a made-up id would only 404."""
        d = BudgetDecider().evaluate(budget_feed(), local(), None)
        xml = PowerShellToastAdapter(icon_path=None).build_xml(d.request)
        self.assertFalse(d.request.actionable)
        self.assertNotIn("<actions>", xml)
        self.assertNotIn("sidecrab-ack", xml)
        self.assertIn("SideCrab", xml)

    def test_it_gets_its_own_action_center_tag(self) -> None:
        """A shared Tag would let it replace a question that is still waiting."""
        adapter = PowerShellToastAdapter(icon_path=None, aumid="X")
        d = BudgetDecider().evaluate(budget_feed(), local(), None)
        self.assertIn(f"$toast.Tag = 'budget-{DAY}'", adapter.build_script(d.request))

    def test_its_tag_differs_from_the_digests(self) -> None:
        self.assertNotEqual(sidecrab_toast.BUDGET_ID_PREFIX, sidecrab_toast.DIGEST_ID_PREFIX)


# ---------------------------------------------------------------------------- ledger


class BudgetLedgerTests(unittest.TestCase):
    def test_missing_file_reads_as_unmarked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(BudgetLedger(Path(tmp) / "nope.json").last_day())

    def test_round_trips_through_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toast-state.json"
            BudgetLedger(path).mark(DAY)
            self.assertEqual(BudgetLedger(path).last_day(), DAY)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"budget": {"lastDay": DAY}})

    def test_the_two_ledgers_do_not_erase_each_other(self) -> None:
        """Both live in one file. A whole-file rewrite would drop the other's mark, and the
        dropped one would re-toast on the next restart."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toast-state.json"
            DigestLedger(path).mark("2026-08-25")
            BudgetLedger(path).mark(DAY)
            self.assertEqual(DigestLedger(path).last_day(), "2026-08-25")
            self.assertEqual(BudgetLedger(path).last_day(), DAY)
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"digest": {"lastDay": "2026-08-25"}, "budget": {"lastDay": DAY}},
            )

    def test_a_digest_mark_is_not_a_budget_mark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toast-state.json"
            DigestLedger(path).mark(DAY)
            self.assertIsNone(BudgetLedger(path).last_day())

    def test_corrupt_state_reads_as_unmarked_rather_than_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toast-state.json"
            for junk in ("{ half-written", "[]", '{"budget": "yes"}', '{"budget": {"lastDay": 4}}'):
                with self.subTest(junk=junk):
                    path.write_text(junk, encoding="utf-8")
                    self.assertIsNone(BudgetLedger(path).last_day())

    def test_a_restart_mid_day_does_not_re_toast(self) -> None:
        """The whole reason the mark is on disk: SideCrab-toast restarts are routine."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toast-state.json"
            before = BudgetLedger(path)
            first = BudgetDecider().evaluate(budget_feed(), local(12, 0), before.last_day())
            self.assertIsNotNone(first.request)
            before.mark(first.day)

            # A brand new process: fresh decider, fresh ledger object, same file.
            after = BudgetLedger(path)
            self.assertEqual(after.last_day(), DAY)
            second = BudgetDecider().evaluate(budget_feed(), local(12, 5), after.last_day())
            self.assertIsNone(second.request)
            self.assertIsNone(second.day)

    def test_an_unwritable_ledger_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocked"
            blocker.write_text("i am a file, not a directory", encoding="utf-8")
            ledger = BudgetLedger(blocker / "toast-state.json")
            ledger.mark(DAY)  # must not raise
            self.assertEqual(ledger.last_day(), DAY, "in-memory mark still holds for this process")


# ---------------------------------------------------------------------------- poll loop


class BudgetPollTests(unittest.TestCase):
    """The budget toast riding the existing 10 s poll — no thread, no timer."""

    def setUp(self) -> None:
        self._real_fetch = sidecrab_toast.fetch_state
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, sidecrab_toast, "fetch_state", self._real_fetch)

    def build(self, state, digest=False):
        sidecrab_toast.fetch_state = lambda *a, **k: state
        root = Path(self.tmp.name)
        config = root / "config.json"
        config.write_text(
            json.dumps({"toast": {"enabled": True}, "digest": {"enabled": digest, "time": "09:00"}}),
            encoding="utf-8",
        )
        adapter = RecordingToastAdapter()
        notifier = Notifier(
            adapter=adapter,
            config_reader=ConfigReader(config),
            digest_ledger=DigestLedger(root / "toast-state.json"),
            budget_ledger=BudgetLedger(root / "toast-state.json"),
            snooze_ledger=SnoozeLedger(root / "toast-state.json"),
        )
        return notifier, adapter

    def when(self, hour: int = 12, minute: int = 0) -> datetime:
        # Naive -> .astimezone() reads it as LOCAL, which is what the budget day keys on.
        return datetime(2026, 8, 26, hour, minute).astimezone()

    def test_a_crossing_reaches_the_adapter(self) -> None:
        notifier, adapter = self.build(budget_feed())
        fired = notifier.poll_once(now=self.when())
        self.assertEqual(len(fired), 1)
        self.assertEqual(adapter.shown[0].title, BUDGET_TITLE)
        self.assertEqual(adapter.shown[0].body, "6.7M of 5.0M output tokens (134%)")

    def test_the_second_poll_of_the_day_is_silent(self) -> None:
        notifier, adapter = self.build(budget_feed())
        notifier.poll_once(now=self.when())
        notifier.poll_once(now=self.when(12, 1))
        self.assertEqual(len(adapter.shown), 1)

    def test_an_absent_budget_fires_nothing(self) -> None:
        notifier, adapter = self.build(budget_feed(budget=None))
        notifier.poll_once(now=self.when())
        self.assertEqual(adapter.shown, [])
        self.assertIsNone(notifier.budget_ledger.last_day())

    def test_crabd_unreachable_fires_nothing_and_keeps_the_day(self) -> None:
        notifier, adapter = self.build(None)
        notifier.poll_once(now=self.when())
        self.assertEqual(adapter.shown, [])
        self.assertIsNone(notifier.budget_ledger.last_day())

        sidecrab_toast.fetch_state = lambda *a, **k: budget_feed()
        self.assertEqual(len(notifier.poll_once(now=self.when(12, 1))), 1)

    def test_an_unsupported_schema_stands_down_from_the_budget_too(self) -> None:
        notifier, adapter = self.build({**budget_feed(), "schema": 99})
        notifier.poll_once(now=self.when())
        self.assertEqual(adapter.shown, [])
        self.assertIsNone(notifier.budget_ledger.last_day())

    def test_a_failed_budget_toast_still_consumes_the_day(self) -> None:
        """Otherwise a broken toast path retries every 10 s until midnight."""
        notifier, _ = self.build(budget_feed())
        notifier.adapter = RecordingToastAdapter(succeed=False)
        self.assertEqual(notifier.poll_once(now=self.when()), [])
        self.assertEqual(notifier.budget_ledger.last_day(), DAY)
        notifier.poll_once(now=self.when(12, 1))
        self.assertEqual(len(notifier.adapter.shown), 1)

    def test_a_restart_after_firing_does_not_re_toast(self) -> None:
        """End to end through the poll loop, not just the ledger unit."""
        notifier, adapter = self.build(budget_feed())
        self.assertEqual(len(notifier.poll_once(now=self.when())), 1)

        restarted, adapter2 = self.build(budget_feed())  # same state file
        self.assertEqual(restarted.poll_once(now=self.when(12, 5)), [])
        self.assertEqual(adapter2.shown, [])

    def test_all_three_toasts_can_fire_in_one_poll(self) -> None:
        state = budget_feed()
        state["recap"] = {"week": [{"day": "2026-08-25", "done": 3, "commits": 40}]}
        state["sessions"] = [{
            "id": "s1", "title": "lane", "state": "needs_input",
            "stateSince": "2026-08-26T00:00:00Z", "question": "go?", "acked": False,
        }]
        notifier, adapter = self.build(state, digest=True)
        fired = notifier.poll_once(now=self.when(9, 30))
        self.assertEqual([r.title for r in fired][1:], ["SideCrab — yesterday", BUDGET_TITLE])
        self.assertTrue(fired[0].actionable, "the waiting-session toast keeps its Acknowledge button")

    def test_the_budget_toast_does_not_write_config(self) -> None:
        """Structural: crabd owns config.json, and two writers on it is how it gets corrupted."""
        notifier, _ = self.build(budget_feed())
        config = Path(self.tmp.name) / "config.json"
        before = config.read_text(encoding="utf-8")
        notifier.poll_once(now=self.when())
        self.assertEqual(config.read_text(encoding="utf-8"), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
