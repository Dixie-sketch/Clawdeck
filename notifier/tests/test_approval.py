"""Headless unit tests for the approval toast (v0.15.0).

Same rules as test_budget.py: stdlib unittest only, no Windows, no clock of its own — the
decider is handed a `now`, so every case here is deterministic.

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
    APPROVAL_HINT,
    APPROVAL_TITLE,
    BODY_TRIM,
    DEFAULT_APPROVAL_THRESHOLD_SEC,
    ApprovalDecider,
    BudgetLedger,
    ConfigReader,
    DigestLedger,
    Notifier,
    PendingPermission,
    PowerShellToastAdapter,
    RecordingToastAdapter,
    SnoozeLedger,
    ToastConfig,
    build_approval_request,
    parse_toast_config,
    read_pending_permission,
)

REQUESTED = "2026-08-26T18:00:00Z"


def utc(seconds: int = 0) -> datetime:
    """An aware UTC stamp, `seconds` after the request in the fixtures below."""
    return datetime(2026, 8, 26, 18, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def session(
    *,
    sid: str = "s1",
    requested: object = REQUESTED,
    tool: object = "Bash",
    summary: object = "git push --force origin master",
    block: object = "default",
    state: str = "working",
) -> dict:
    pending = (
        {"tool": tool, "summary": summary, "requestedAt": requested} if block == "default" else block
    )
    return {
        "id": sid,
        "title": "the lane",
        "state": state,
        "stateSince": REQUESTED,
        "pendingPermission": pending,
    }


def feed(*sessions: dict, quiet: bool = False) -> dict:
    return {
        "schema": 5,
        "generatedAt": REQUESTED,
        "sessions": list(sessions) or [session()],
        "quiet": {"active": quiet, "start": "22:00", "end": "07:00"},
    }


ARMED = ToastConfig()


# ---------------------------------------------------------------------------- reading the feed


class PendingPermissionReadTests(unittest.TestCase):
    def test_a_well_formed_block_reads(self) -> None:
        pending = read_pending_permission(session())
        self.assertEqual(pending.session_id, "s1")
        self.assertEqual(pending.tool, "Bash")
        self.assertEqual(pending.summary, "git push --force origin master")
        self.assertEqual(pending.requested_at, REQUESTED)

    def test_nothing_parked_reads_as_nothing(self) -> None:
        for block in (None, "Bash", [], 5, {}):
            with self.subTest(block=block):
                self.assertIsNone(read_pending_permission(session(block=block)))

    def test_a_row_with_no_key_at_all_reads_as_nothing(self) -> None:
        """An older crabd never emits the field."""
        self.assertIsNone(read_pending_permission({"id": "s1", "state": "working"}))

    def test_a_malformed_row_reads_as_nothing(self) -> None:
        for row in (None, [], "s1", {"pendingPermission": {"tool": "Bash", "requestedAt": REQUESTED}}):
            with self.subTest(row=row):
                self.assertIsNone(read_pending_permission(row))

    def test_a_missing_requested_at_is_REFUSED_not_defaulted(self) -> None:
        """requestedAt IS the dedupe key. Without it there is no promise of one toast, so the
        request is dropped rather than toasted on a made-up key every 10 s."""
        for requested in (None, "", "   ", 1756231200, True, [REQUESTED]):
            with self.subTest(requested=requested):
                self.assertIsNone(read_pending_permission(session(requested=requested)))

    def test_a_missing_tool_falls_back_rather_than_dropping_the_request(self) -> None:
        for tool in (None, "", "  ", 5, []):
            with self.subTest(tool=tool):
                self.assertEqual(read_pending_permission(session(tool=tool)).tool, "a tool")

    def test_a_null_summary_stays_none_and_is_never_invented(self) -> None:
        for summary in (None, "", "   ", 5, {}):
            with self.subTest(summary=summary):
                self.assertIsNone(read_pending_permission(session(summary=summary)).summary)

    def test_a_long_tool_name_is_trimmed(self) -> None:
        pending = read_pending_permission(session(tool="mcp__" + "x" * 200))
        self.assertLessEqual(len(pending.tool), sidecrab_toast.TOOL_TRIM)


# ---------------------------------------------------------------------------- the toast text


class ApprovalRequestTests(unittest.TestCase):
    def build(self, **kw) -> sidecrab_toast.ToastRequest:
        return build_approval_request(read_pending_permission(session(**kw)))

    def test_the_title_is_the_contract_wording(self) -> None:
        self.assertEqual(self.build().title, "Claude needs permission")

    def test_the_body_is_tool_then_summary(self) -> None:
        self.assertTrue(self.build().body.startswith("Bash — git push --force origin master"))

    def test_the_body_ends_with_the_panel_hint(self) -> None:
        self.assertTrue(self.build().body.endswith(APPROVAL_HINT))

    def test_a_summary_less_request_still_names_the_tool(self) -> None:
        self.assertEqual(self.build(summary=None).body, f"Bash {APPROVAL_HINT}")

    def test_a_long_summary_is_trimmed_but_the_hint_survives(self) -> None:
        """The instruction telling the operator WHERE to decide must never be the thing cut."""
        body = self.build(summary="rm -rf " + "deep/" * 200).body
        self.assertTrue(body.endswith(APPROVAL_HINT))
        self.assertLessEqual(len(body), BODY_TRIM)
        self.assertIn("…", body, "the summary, not the hint, absorbed the trim")

    def test_it_carries_NO_action_buttons(self) -> None:
        """THE security gate. Approving from a notification is one click from a lock screen or
        from a toast the shell replays out of Action Center hours later — the decision belongs
        on the panel, where crabd's /v1/action decide tap is the only door."""
        xml = PowerShellToastAdapter(aumid="x").build_xml(self.build())
        self.assertNotIn("<actions>", xml)
        self.assertNotIn("Approve", xml)
        self.assertNotIn("Deny", xml)

    def test_it_carries_no_acknowledge_button_either(self) -> None:
        """A pendingPermission card is a hard stop, not an ack-able question — the widget's
        ack-all skips it for the same reason."""
        request = self.build()
        self.assertFalse(request.actionable)
        self.assertNotIn(sidecrab_toast.ACK_BUTTON_CONTENT, PowerShellToastAdapter(aumid="x").build_xml(request))

    def test_it_gets_its_own_action_center_slot(self) -> None:
        """A shared Tag would let the approval toast REPLACE that session's waiting question."""
        adapter = PowerShellToastAdapter(aumid="x")
        self.assertIn("$toast.Tag = 'approval-s1'", adapter.build_script(self.build()))

    def test_its_prefix_differs_from_every_other_toasts(self) -> None:
        prefixes = {
            sidecrab_toast.APPROVAL_ID_PREFIX,
            sidecrab_toast.DIGEST_ID_PREFIX,
            sidecrab_toast.BUDGET_ID_PREFIX,
        }
        self.assertEqual(len(prefixes), 3)

    def test_a_hostile_summary_cannot_escape_the_xml(self) -> None:
        xml = PowerShellToastAdapter(aumid="x").build_xml(self.build(summary="</text><script>x</script>"))
        self.assertNotIn("<script>", xml)


# ---------------------------------------------------------------------------- config


class ApprovalConfigTests(unittest.TestCase):
    def test_the_default_threshold_is_twenty_seconds(self) -> None:
        self.assertEqual(ToastConfig().approval_threshold_sec, 20)
        self.assertEqual(DEFAULT_APPROVAL_THRESHOLD_SEC, 20)

    def test_it_is_much_shorter_than_the_waiting_threshold(self) -> None:
        """crabd holds the hook 55 s. A threshold near 120 s could only ever toast about
        requests that had already fallen through to the terminal dialog."""
        self.assertLess(ToastConfig().approval_threshold_sec, 55)

    def test_a_configured_threshold_is_read(self) -> None:
        config = parse_toast_config({"toast": {"approvalThresholdSec": 45}})
        self.assertEqual(config.approval_threshold_sec, 45)
        self.assertEqual(config.threshold_sec, 120, "the waiting threshold is untouched")

    def test_zero_is_a_valid_threshold(self) -> None:
        self.assertEqual(parse_toast_config({"toast": {"approvalThresholdSec": 0}}).approval_threshold_sec, 0)

    def test_a_bad_threshold_falls_back_per_field(self) -> None:
        for raw in ("20", None, True, -5, [20], {}):
            with self.subTest(raw=raw):
                config = parse_toast_config({"toast": {"approvalThresholdSec": raw, "thresholdSec": 60}})
                self.assertEqual(config.approval_threshold_sec, 20)
                self.assertEqual(config.threshold_sec, 60, "one bad field does not poison the other")

    def test_a_float_threshold_is_truncated_not_rejected(self) -> None:
        self.assertEqual(parse_toast_config({"toast": {"approvalThresholdSec": 30.7}}).approval_threshold_sec, 30)


# ---------------------------------------------------------------------------- the decision


class ApprovalDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = ApprovalDecider()

    def test_a_matured_request_is_owed_a_toast(self) -> None:
        owed = self.decider.evaluate(feed(), utc(20), ARMED)
        self.assertEqual(len(owed), 1)
        self.assertEqual(owed[0].title, APPROVAL_TITLE)

    def test_a_fresh_request_is_not_yet_due(self) -> None:
        self.assertEqual(self.decider.evaluate(feed(), utc(19), ARMED), [])

    def test_a_not_yet_due_request_still_toasts_when_it_matures(self) -> None:
        """The early poll must not consume the request — that is the bug the ledger has to
        avoid, not the one it exists for."""
        self.assertEqual(self.decider.evaluate(feed(), utc(5), ARMED), [])
        self.assertEqual(len(self.decider.evaluate(feed(), utc(25), ARMED)), 1)

    def test_the_threshold_is_configurable(self) -> None:
        config = ToastConfig(approval_threshold_sec=60)
        self.assertEqual(self.decider.evaluate(feed(), utc(30), config), [])
        self.assertEqual(len(self.decider.evaluate(feed(), utc(60), config)), 1)

    def test_it_toasts_once_per_request_not_once_per_poll(self) -> None:
        self.assertEqual(len(self.decider.evaluate(feed(), utc(20), ARMED)), 1)
        for extra in (30, 40, 50, 300):
            self.assertEqual(self.decider.evaluate(feed(), utc(extra), ARMED), [])

    def test_a_new_requested_at_re_arms(self) -> None:
        """The next tool call is a new decision, and gets its own toast."""
        self.assertEqual(len(self.decider.evaluate(feed(), utc(20), ARMED), ), 1)
        later = feed(session(requested="2026-08-26T18:02:00Z"))
        owed = self.decider.evaluate(later, utc(140), ARMED)
        self.assertEqual(len(owed), 1)
        self.assertEqual(owed[0].state_since, "2026-08-26T18:02:00Z")

    def test_a_resolved_request_fires_nothing(self) -> None:
        """Decided on the panel inside the threshold: crabd drops the block, and the operator
        never hears about a decision they already made."""
        self.assertEqual(self.decider.evaluate(feed(), utc(10), ARMED), [])
        resolved = feed({"id": "s1", "state": "working", "stateSince": REQUESTED, "pendingPermission": None})
        self.assertEqual(self.decider.evaluate(resolved, utc(30), ARMED), [])
        self.assertEqual(self.decider.evaluate(resolved, utc(300), ARMED), [])

    def test_a_resolved_request_clears_its_mark_without_re_toasting(self) -> None:
        """resolve-clears: the ledger drops the entry so it stays bounded, and the request
        that was resolved cannot come back — crabd never re-serves a decided requestedAt."""
        self.assertEqual(len(self.decider.evaluate(feed(), utc(20), ARMED)), 1)
        self.assertIn("s1", self.decider._marks)
        gone = {"schema": 5, "generatedAt": REQUESTED, "sessions": [{"id": "s1", "state": "working"}]}
        for tick in range(sidecrab_toast.APPROVAL_CLEAR_GRACE):
            self.assertEqual(self.decider.evaluate(gone, utc(30 + tick), ARMED), [])
        self.assertNotIn("s1", self.decider._marks, "the mark is cleared once the request has resolved")

    def test_a_one_poll_flicker_does_NOT_clear_the_mark(self) -> None:
        """The mark is the only thing stopping a re-toast, so it survives a blip."""
        self.assertEqual(len(self.decider.evaluate(feed(), utc(20), ARMED)), 1)
        gone = {"schema": 5, "generatedAt": REQUESTED, "sessions": [{"id": "s1", "state": "working"}]}
        self.decider.evaluate(gone, utc(30), ARMED)
        self.assertEqual(self.decider.evaluate(feed(), utc(40), ARMED), [], "same request, still deduped")

    def test_quiet_hours_suppress_AND_mark(self) -> None:
        """Silent, not deferred. A request that matured in quiet hours has long since fallen
        through to the terminal dialog; toasting when quiet lifts would be a lie."""
        self.assertEqual(self.decider.evaluate(feed(quiet=True), utc(20), ARMED), [])
        self.assertEqual(self.decider.evaluate(feed(), utc(30), ARMED), [], "it does not burst when quiet lifts")

    def test_quiet_before_the_threshold_does_not_consume_the_request(self) -> None:
        self.assertEqual(self.decider.evaluate(feed(quiet=True), utc(5), ARMED), [])
        self.assertEqual(len(self.decider.evaluate(feed(), utc(25), ARMED)), 1)

    def test_a_null_quiet_block_is_not_quiet(self) -> None:
        """Production crabd serves quiet: null when quiet hours are unconfigured."""
        state = feed()
        state["quiet"] = None
        self.assertEqual(len(self.decider.evaluate(state, utc(20), ARMED)), 1)

    def test_disabled_toasts_nothing_and_records_nothing(self) -> None:
        off = ToastConfig(enabled=False)
        self.assertEqual(self.decider.evaluate(feed(), utc(20), off), [])
        self.assertEqual(self.decider.evaluate(feed(), utc(30), off), [])
        self.assertEqual(len(self.decider.evaluate(feed(), utc(40), ARMED)), 1,
                         "turning it back on surfaces what is genuinely still parked")

    def test_an_unparseable_requested_at_is_skipped_not_toasted(self) -> None:
        self.assertEqual(self.decider.evaluate(feed(session(requested="yesterday")), utc(300), ARMED), [])

    def test_a_request_from_the_future_is_not_yet_due(self) -> None:
        """Clock skew must not make a request instantly mature."""
        self.assertEqual(self.decider.evaluate(feed(session(requested="2026-08-26T19:00:00Z")), utc(20), ARMED), [])

    def test_two_sessions_each_get_their_own_toast(self) -> None:
        owed = self.decider.evaluate(feed(session(sid="s1"), session(sid="s2", tool="Write")), utc(20), ARMED)
        self.assertEqual([r.session_id for r in owed], ["approval-s1", "approval-s2"])

    def test_it_does_not_require_needs_input(self) -> None:
        """crabd registers the pending entry off the PermissionRequest hook and does NOT move
        the state machine — needs_input arrives separately, via Notification, unordered."""
        for state in ("working", "needs_input", "done", "idle"):
            with self.subTest(state=state):
                owed = ApprovalDecider().evaluate(feed(session(state=state)), utc(20), ARMED)
                self.assertEqual(len(owed), 1)

    def test_a_malformed_feed_never_raises(self) -> None:
        for state in (None, [], "x", {}, {"sessions": "no"}, {"sessions": [None, 5, "s"]}):
            with self.subTest(state=state):
                self.assertEqual(self.decider.evaluate(state, utc(20), ARMED), [])


# ---------------------------------------------------------------------------- poll loop


class ApprovalPollTests(unittest.TestCase):
    def setUp(self) -> None:
        self._real_fetch = sidecrab_toast.fetch_state
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, sidecrab_toast, "fetch_state", self._real_fetch)

    def build(self, state, toast_config: dict | None = None):
        sidecrab_toast.fetch_state = lambda *a, **k: state
        root = Path(self.tmp.name)
        config = root / "config.json"
        config.write_text(json.dumps({"toast": toast_config or {"enabled": True}}), encoding="utf-8")
        adapter = RecordingToastAdapter()
        notifier = Notifier(
            adapter=adapter,
            config_reader=ConfigReader(config),
            digest_ledger=DigestLedger(root / "toast-state.json"),
            budget_ledger=BudgetLedger(root / "toast-state.json"),
            snooze_ledger=SnoozeLedger(root / "toast-state.json"),
        )
        return notifier, adapter

    def test_a_matured_request_reaches_the_adapter(self) -> None:
        notifier, adapter = self.build(feed())
        fired = notifier.poll_once(now=utc(25))
        self.assertEqual(len(fired), 1)
        self.assertEqual(adapter.shown[0].title, APPROVAL_TITLE)
        self.assertEqual(adapter.shown[0].body, "Bash — git push --force origin master Decide on the panel.")

    def test_the_second_poll_is_silent(self) -> None:
        notifier, adapter = self.build(feed())
        notifier.poll_once(now=utc(25))
        notifier.poll_once(now=utc(35))
        self.assertEqual(len(adapter.shown), 1)

    def test_the_configured_threshold_reaches_the_decider(self) -> None:
        notifier, adapter = self.build(feed(), {"enabled": True, "approvalThresholdSec": 90})
        notifier.poll_once(now=utc(25))
        self.assertEqual(adapter.shown, [])
        notifier.poll_once(now=utc(95))
        self.assertEqual(len(adapter.shown), 1)

    def test_an_unsupported_schema_stands_down_from_approvals_too(self) -> None:
        notifier, adapter = self.build({**feed(), "schema": 99})
        notifier.poll_once(now=utc(25))
        self.assertEqual(adapter.shown, [])

    def test_the_approval_toast_does_not_write_config(self) -> None:
        notifier, _ = self.build(feed())
        config = Path(self.tmp.name) / "config.json"
        before = config.read_text(encoding="utf-8")
        notifier.poll_once(now=utc(25))
        self.assertEqual(config.read_text(encoding="utf-8"), before)

    def test_a_failed_approval_toast_retries_then_stops_once_it_lands(self) -> None:
        # F1 (security-relevant): a live permission request whose toast FAILS to render — an
        # illegal-XML summary that trips LoadXml, a dead PowerShell — must not be consumed. The
        # panel toast is the out-of-view alert that a Bash call is parked; silently suppressing
        # it forever is the bug. So a failed render re-arms the request (unresolve), and the
        # next poll retries — until the show lands, after which the mark holds and it is silent.
        notifier, _ = self.build(feed())
        notifier.adapter = RecordingToastAdapter(succeed=False)
        self.assertEqual(notifier.poll_once(now=utc(25)), [])
        self.assertEqual(notifier.poll_once(now=utc(35)), [])
        self.assertEqual(len(notifier.adapter.shown), 2, "a failed render is retried, not consumed")

        good = RecordingToastAdapter()
        notifier.adapter = good
        self.assertEqual(len(notifier.poll_once(now=utc(45))), 1, "it lands once the render succeeds")
        notifier.poll_once(now=utc(55))
        self.assertEqual(len(good.shown), 1, "and is resolved once shown — no re-fire")

    def test_the_waiting_toast_and_the_approval_toast_are_independent(self) -> None:
        """Both can be owed for one session: the approval at 20 s, and — if the request fell
        through to the terminal and the question is STILL unanswered — the ordinary one at
        120 s. They say different things, and the second is a real escalation."""
        notifier, adapter = self.build(feed(session(state="needs_input")))
        notifier.poll_once(now=utc(25))
        notifier.poll_once(now=utc(130))
        self.assertEqual([r.title for r in adapter.shown],
                         [APPROVAL_TITLE, "Claude is waiting — the lane"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
