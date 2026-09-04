"""Headless unit tests for the stale-feed / companion-outage toast (v0.15.0).

The notifier watching its own stack. Every gate here answers one question — "would this fire
on a healthy night?" — so most of these cases are about staying SILENT.

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
    STALE_ACTIVITY_WINDOW_SEC,
    STALE_FEED_MAX_AGE_SEC,
    STALE_TITLE,
    BudgetLedger,
    ConfigReader,
    DigestLedger,
    Notifier,
    PowerShellToastAdapter,
    RecordingToastAdapter,
    SnoozeLedger,
    StaleFeedDecider,
    has_active_session,
    read_feed_health,
)

T0 = datetime(2026, 8, 26, 18, 0, 0, tzinfo=timezone.utc)


def utc(seconds: int = 0) -> datetime:
    return T0 + timedelta(seconds=seconds)


def healthy(*, at: int = 0, state: str = "working", quiet: bool = False, sessions: object = "default") -> dict:
    """A feed stamped `at` seconds after T0, carrying one session in `state`."""
    rows = [{"id": "s1", "title": "the lane", "state": state, "stateSince": T0.isoformat()}]
    return {
        "schema": 5,
        "generatedAt": utc(at).isoformat(),
        "sessions": rows if sessions == "default" else sessions,
        "quiet": {"active": quiet, "start": "22:00", "end": "07:00"},
    }


# ---------------------------------------------------------------------------- reading health


class FeedHealthTests(unittest.TestCase):
    def test_a_fresh_feed_is_healthy(self) -> None:
        self.assertTrue(read_feed_health(healthy(), utc(10)).healthy)

    def test_an_unreachable_crabd_is_unhealthy(self) -> None:
        health = read_feed_health(None, utc())
        self.assertFalse(health.healthy)
        self.assertIsNone(health.generated_at)
        self.assertIn("not answering", health.detail)

    def test_an_unreadable_feed_is_unhealthy(self) -> None:
        for state in ([], "x", 5):
            with self.subTest(state=state):
                self.assertFalse(read_feed_health(state, utc()).healthy)

    def test_a_feed_older_than_the_threshold_is_unhealthy(self) -> None:
        health = read_feed_health(healthy(), utc(STALE_FEED_MAX_AGE_SEC + 1))
        self.assertFalse(health.healthy)
        self.assertIsNotNone(health.generated_at)
        self.assertIn("5 min old", health.detail)

    def test_a_feed_exactly_at_the_threshold_is_still_healthy(self) -> None:
        self.assertTrue(read_feed_health(healthy(), utc(STALE_FEED_MAX_AGE_SEC)).healthy)

    def test_a_feed_with_no_usable_timestamp_is_ALIVE(self) -> None:
        """It is answering. Calling a responding companion 'not responding' would be the false
        alarm this whole feature exists to avoid."""
        for stamp in (None, "", "yesterday", 5, True):
            with self.subTest(stamp=stamp):
                self.assertTrue(read_feed_health({"schema": 5, "generatedAt": stamp}, utc(9999)).healthy)

    def test_a_future_timestamp_is_clock_skew_not_staleness(self) -> None:
        self.assertTrue(read_feed_health(healthy(at=3600), utc()).healthy)

    def test_the_detail_names_no_host_or_port(self) -> None:
        """Public-facing string: an operator reads it on a toast, not in a log.

        The age is 7200 s, not 9999, so the port digits can only get into the detail
        from the endpoint - never from the number of seconds this case happens to use.
        """
        for state in (None, healthy()):
            detail = read_feed_health(state, utc(7200)).detail
            self.assertNotIn("127.0.0.1", detail)
            self.assertNotIn("9999", detail)


class FeedEndpointTests(unittest.TestCase):
    """Where the notifier looks, as opposed to what it makes of what it finds.

    Its own class because it is a module constant, not a read_feed_health case: it needs
    none of that class's fixtures and would be the only test there that never calls it.
    """

    def test_the_default_endpoint_is_crabds_state_url(self) -> None:
        """A GET, so no panel header - the gate crabd 0.31.0 added guards POSTs only. But
        the port moved, and a stale default here is a notifier that reports the companion
        down while it is answering next door."""
        self.assertEqual(sidecrab_toast.DEFAULT_ENDPOINT, "http://127.0.0.1:9999/v1/state")


class ActiveSessionTests(unittest.TestCase):
    def test_working_and_needs_input_are_active(self) -> None:
        for state in ("working", "needs_input"):
            with self.subTest(state=state):
                self.assertTrue(has_active_session(healthy(state=state)))

    def test_finished_and_idle_sessions_are_not(self) -> None:
        """A finished session on the panel is not a reason to be told the panel stopped."""
        for state in ("done", "idle", "gone", "", None):
            with self.subTest(state=state):
                self.assertFalse(has_active_session(healthy(state=state)))

    def test_an_empty_or_malformed_feed_has_nothing_active(self) -> None:
        for sessions in ([], "no", None, [None, 5, "s"], [{}]):
            with self.subTest(sessions=sessions):
                self.assertFalse(has_active_session(healthy(sessions=sessions)))


# ---------------------------------------------------------------------------- the decision


class StaleDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = StaleFeedDecider()

    def prime(self, *, quiet: bool = False, state: str = "working", at: int = 0) -> None:
        """One healthy poll, so the decider has seen someone working."""
        self.assertIsNone(self.decider.evaluate(healthy(at=at, quiet=quiet, state=state), utc(at)))

    # -- the outage ---------------------------------------------------------------------

    def test_an_outage_toasts_once_the_dwell_has_passed(self) -> None:
        self.prime()
        request = self.decider.evaluate(None, utc(STALE_FEED_MAX_AGE_SEC))
        self.assertIsNotNone(request)
        self.assertEqual(request.title, STALE_TITLE)
        self.assertIn("not answering", request.body)
        self.assertIn("stale data", request.body)

    def test_a_brief_blip_is_silent(self) -> None:
        """crabd restarts are ROUTINE — a 10 s gap must never toast, or the operator learns to
        ignore the one outage that matters."""
        self.prime()
        for seconds in (1, 10, 30, 120, STALE_FEED_MAX_AGE_SEC - 1):
            with self.subTest(seconds=seconds):
                self.assertIsNone(self.decider.evaluate(None, utc(seconds)))

    def test_a_blip_that_recovers_leaves_nothing_armed(self) -> None:
        self.prime()
        self.decider.evaluate(None, utc(30))
        self.prime(at=40)
        for seconds in (41, 100, 300):
            self.assertIsNone(self.decider.evaluate(None, utc(seconds)), "the dwell restarts on recovery")
        self.assertIsNotNone(self.decider.evaluate(None, utc(40 + STALE_FEED_MAX_AGE_SEC)))

    def test_it_toasts_once_per_outage_not_once_per_poll(self) -> None:
        self.prime()
        self.assertIsNotNone(self.decider.evaluate(None, utc(300)))
        for seconds in (310, 400, 900, 5000):
            with self.subTest(seconds=seconds):
                self.assertIsNone(self.decider.evaluate(None, utc(seconds)))

    def test_only_a_RECOVERY_re_arms(self) -> None:
        self.prime()
        self.assertIsNotNone(self.decider.evaluate(None, utc(300)))
        self.assertIsNone(self.decider.evaluate(None, utc(600)))

        self.prime(at=700)  # crabd comes back
        self.assertIsNone(self.decider.evaluate(None, utc(800)), "the new outage serves its own dwell")
        self.assertIsNotNone(self.decider.evaluate(None, utc(700 + STALE_FEED_MAX_AGE_SEC)),
                             "a SECOND outage gets its own toast")

    # -- the frozen-feed arm ------------------------------------------------------------

    def test_a_frozen_feed_toasts_without_a_second_dwell(self) -> None:
        """crabd is answering but its data stopped moving. The age IS the dwell — waiting the
        threshold again would double the silence."""
        self.prime()
        request = self.decider.evaluate(healthy(at=0), utc(STALE_FEED_MAX_AGE_SEC + 60))
        self.assertIsNotNone(request)
        self.assertIn("min old", request.body)

    def test_a_frozen_feed_does_not_refresh_the_activity_latch(self) -> None:
        """Re-arming from frozen content is how a dead feed keeps itself alive forever. The
        healthy poll saw nobody working; the STALE document claims someone is. Only the
        healthy one counts, so this stays silent."""
        self.prime(state="idle")
        frozen = healthy(at=0, state="working")
        for seconds in (400, 800, 1200):
            with self.subTest(seconds=seconds):
                self.assertIsNone(self.decider.evaluate(frozen, utc(seconds)))

    def test_a_recovering_stamp_re_arms(self) -> None:
        self.prime()
        self.assertIsNotNone(self.decider.evaluate(healthy(at=0), utc(400)))
        self.assertIsNone(self.decider.evaluate(healthy(at=500), utc(500)), "fresh again")
        self.assertIsNotNone(self.decider.evaluate(healthy(at=500), utc(900)))

    # -- the activity gate --------------------------------------------------------------

    def test_a_notifier_that_never_saw_a_healthy_feed_is_SILENT(self) -> None:
        """First run against a crabd that is not installed/started must say nothing. It has no
        evidence anyone is working — and nobody's panel is dead if nobody is looking at it."""
        for seconds in (300, 900, 86400):
            with self.subTest(seconds=seconds):
                self.assertIsNone(StaleFeedDecider().evaluate(None, utc(seconds)))

    def test_an_idle_estate_is_silent(self) -> None:
        self.prime(state="idle")
        self.assertIsNone(self.decider.evaluate(None, utc(600)))

    def test_an_overnight_sleep_is_silent(self) -> None:
        """Laptop lid closed at 18:00 with a session working, opened the next morning. The
        panel was not dead — nobody was at it."""
        self.prime()
        self.assertIsNone(self.decider.evaluate(None, utc(STALE_ACTIVITY_WINDOW_SEC + 1)))
        self.assertIsNone(self.decider.evaluate(None, utc(50000)))

    def test_the_activity_window_edge_still_toasts(self) -> None:
        self.prime()
        self.assertIsNotNone(self.decider.evaluate(None, utc(STALE_ACTIVITY_WINDOW_SEC)))

    def test_a_silent_outage_does_not_SPEND_the_outages_one_toast(self) -> None:
        """Nothing was active, so nothing was said — but the toast is still available if the
        operator turns out to have been working after all."""
        self.prime(state="idle")
        self.assertIsNone(self.decider.evaluate(None, utc(600)))
        self.prime(at=700)
        self.assertIsNotNone(self.decider.evaluate(None, utc(1000)))

    # -- quiet hours --------------------------------------------------------------------

    def test_quiet_hours_suppress_AND_mark(self) -> None:
        """Quiet is REMEMBERED from the last healthy poll — it lives in the very feed this
        decider cannot reach. Marked, so nothing bursts when quiet lifts."""
        self.prime(quiet=True)
        self.assertIsNone(self.decider.evaluate(None, utc(300)))
        self.assertIsNone(self.decider.evaluate(None, utc(900)))

    def test_quiet_is_re_read_on_every_healthy_poll(self) -> None:
        self.prime(quiet=True)
        self.prime(at=10, quiet=False)
        self.assertIsNotNone(self.decider.evaluate(None, utc(310)))

    def test_a_null_quiet_block_is_not_quiet(self) -> None:
        state = healthy()
        state["quiet"] = None
        self.assertIsNone(self.decider.evaluate(state, utc()))
        self.assertIsNotNone(self.decider.evaluate(None, utc(300)))

    # -- the toast itself ---------------------------------------------------------------

    def test_it_carries_no_buttons(self) -> None:
        """Nothing to acknowledge, and the endpoint an Acknowledge button would POST to is the
        one that just stopped answering."""
        self.prime()
        request = self.decider.evaluate(None, utc(300))
        self.assertFalse(request.actionable)
        xml = PowerShellToastAdapter(aumid="x").build_xml(request)
        self.assertNotIn("<actions>", xml)

    def test_it_gets_its_own_action_center_slot(self) -> None:
        self.prime()
        request = self.decider.evaluate(None, utc(300))
        script = PowerShellToastAdapter(aumid="x").build_script(request)
        self.assertIn(f"$toast.Tag = '{sidecrab_toast.STALE_ID}'", script)

    def test_its_tag_cannot_collide_with_a_session_toast(self) -> None:
        for prefix in (sidecrab_toast.APPROVAL_ID_PREFIX, sidecrab_toast.DIGEST_ID_PREFIX,
                       sidecrab_toast.BUDGET_ID_PREFIX):
            self.assertFalse(sidecrab_toast.STALE_ID.startswith(prefix))


# ---------------------------------------------------------------------------- poll loop


class StalePollTests(unittest.TestCase):
    def setUp(self) -> None:
        self._real_fetch = sidecrab_toast.fetch_state
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, sidecrab_toast, "fetch_state", self._real_fetch)

        root = Path(self.tmp.name)
        config = root / "config.json"
        config.write_text(json.dumps({"toast": {"enabled": True}}), encoding="utf-8")
        self.adapter = RecordingToastAdapter()
        self.notifier = Notifier(
            adapter=self.adapter,
            config_reader=ConfigReader(config),
            digest_ledger=DigestLedger(root / "toast-state.json"),
            budget_ledger=BudgetLedger(root / "toast-state.json"),
            snooze_ledger=SnoozeLedger(root / "toast-state.json"),
        )

    def serve(self, state) -> None:
        sidecrab_toast.fetch_state = lambda *a, **k: state

    def test_an_outage_reaches_the_adapter_even_though_the_fetch_failed(self) -> None:
        """The one consumer that must survive the early return every other toast takes."""
        self.serve(healthy())
        self.notifier.poll_once(now=utc())
        self.serve(None)
        fired = self.notifier.poll_once(now=utc(300))
        self.assertEqual([r.title for r in fired], [STALE_TITLE])

    def test_no_other_toast_fires_during_an_outage(self) -> None:
        self.serve(healthy())
        self.notifier.poll_once(now=utc())
        self.serve(None)
        self.notifier.poll_once(now=utc(300))
        self.assertEqual(len(self.adapter.shown), 1)
        self.assertIsNone(self.notifier.digest_ledger.last_day(), "the day is not consumed by an outage")

    def test_an_unsupported_schema_is_ANSWERING_not_an_outage(self) -> None:
        """A feed we cannot read is a different problem, and it already warns once."""
        for seconds in (0, 300, 900):
            self.serve({**healthy(at=seconds), "schema": 99})
            self.notifier.poll_once(now=utc(seconds))
        self.assertEqual(self.adapter.shown, [])

    def test_a_recovery_then_a_second_outage_toasts_twice(self) -> None:
        self.serve(healthy())
        self.notifier.poll_once(now=utc())
        self.serve(None)
        self.notifier.poll_once(now=utc(300))

        self.serve(healthy(at=400))
        self.notifier.poll_once(now=utc(400))
        self.serve(None)
        self.notifier.poll_once(now=utc(700))
        self.assertEqual([r.title for r in self.adapter.shown], [STALE_TITLE, STALE_TITLE])

    def test_a_quiet_estate_never_hears_about_an_outage(self) -> None:
        self.serve(healthy(state="done"))
        self.notifier.poll_once(now=utc())
        self.serve(None)
        for seconds in (300, 900, 5000):
            self.notifier.poll_once(now=utc(seconds))
        self.assertEqual(self.adapter.shown, [])

    def test_the_outage_toast_does_not_write_config(self) -> None:
        config = Path(self.tmp.name) / "config.json"
        before = config.read_text(encoding="utf-8")
        self.serve(healthy())
        self.notifier.poll_once(now=utc())
        self.serve(None)
        self.notifier.poll_once(now=utc(300))
        self.assertEqual(config.read_text(encoding="utf-8"), before)

    def test_a_failed_outage_toast_does_not_retry_every_poll(self) -> None:
        self.serve(healthy())
        self.notifier.poll_once(now=utc())
        self.notifier.adapter = RecordingToastAdapter(succeed=False)
        self.serve(None)
        self.assertEqual(self.notifier.poll_once(now=utc(300)), [])
        self.notifier.poll_once(now=utc(400))
        self.assertEqual(len(self.notifier.adapter.shown), 1, "marked before showing, like every other toast")


if __name__ == "__main__":
    unittest.main(verbosity=2)
