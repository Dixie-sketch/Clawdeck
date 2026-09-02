"""`toast.enabled: false` is a GLOBAL mute — CD-26.

The switch is labelled "Desktop Toast Alerts" in the panel. Before v0.19.0 it was
checked inside three of the six deciders (waiting, approval, long-run), so the
digest, the budget crossing and the companion-outage toast kept firing under a
switch the operator had turned off. This file pins the promise the label makes:
**all six, or the label is a lie.**

The other half of the promise is that muting must not quietly SPEND anything. The
rule, per decider, is the one already in README.md's failure matrix — a live
signal re-arms, a periodic consumes — and the "muted" column there is what these
tests read like prose. The distinction matters because it decides what the
operator sees when the switch comes back on: the question that is still open, and
not yesterday's digest.

    python -m unittest discover -s notifier/tests -t notifier/tests
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
    APPROVAL_TITLE,
    BUDGET_TITLE,
    DIGEST_TITLE,
    STALE_ID,
    STALE_TITLE,
    TOAST_KINDS,
    ConfigReader,
    BudgetLedger,
    DigestLedger,
    Notifier,
    RecordingToastAdapter,
    SnoozeLedger,
    ToastConfig,
    ToastRequest,
    toast_kind,
)

#: A local Wednesday at 09:30 — past the 09:00 digest time, so the digest is due.
LOCAL_NOW = datetime(2026, 8, 26, 9, 30).astimezone()
UTC_NOW = LOCAL_NOW.astimezone(timezone.utc)


def iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(seconds: float) -> str:
    return iso(UTC_NOW - timedelta(seconds=seconds))


def waiting_session(sid: str = "s1") -> dict:
    """A question that matured 10 minutes ago — well past thresholdSec."""
    return {"id": sid, "title": "the lane", "state": "needs_input",
            "stateSince": ago(600), "acked": False, "question": "ship it?"}


def approval_session(sid: str = "s2") -> dict:
    """A permission parked 10 minutes ago — well past approvalThresholdSec."""
    return {"id": sid, "title": "the lane", "state": "working", "stateSince": ago(600),
            "pendingPermission": {"tool": "Bash", "summary": "git push", "requestedAt": ago(600)}}


def working_session(sid: str = "s3") -> dict:
    return {"id": sid, "title": "the lane", "state": "working",
            "stateSince": ago(3600), "turnStartedAt": ago(3600)}


def done_session(sid: str = "s3") -> dict:
    """The same session, one poll later: a 60-minute turn, past longRunSec."""
    return {"id": sid, "title": "the lane", "state": "done",
            "stateSince": ago(0), "turnStartedAt": None}


def feed(*sessions: dict, generated: str | None = None, budget_pct: float = 1.34) -> dict:
    """Every signal at once: a waiting question, a parked permission, a finished
    long turn, yesterday's recap row and a crossed budget."""
    return {
        "schema": 5,
        "generatedAt": generated or iso(UTC_NOW),
        "sessions": list(sessions),
        "quiet": {"active": False, "start": "22:00", "end": "07:00"},
        "recap": {"week": [{"day": "2026-08-25", "done": 3, "commits": 40}]},
        "burn": {
            "today": {"outputTokens": 6_700_000, "messages": 5255},
            "budget": {"dailyOutputTokens": 5_000_000, "todayPct": budget_pct},
        },
    }


class MuteHarness(unittest.TestCase):
    """A real Notifier over a temp config whose switch this test can flip."""

    def setUp(self) -> None:
        self._real_fetch = sidecrab_toast.fetch_state
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, sidecrab_toast, "fetch_state", self._real_fetch)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config.json"
        self.adapter = RecordingToastAdapter()
        self.notifier = Notifier(
            adapter=self.adapter,
            config_reader=ConfigReader(self.config),
            digest_ledger=DigestLedger(self.root / "toast-state.json"),
            budget_ledger=BudgetLedger(self.root / "toast-state.json"),
            snooze_ledger=SnoozeLedger(self.root / "toast-state.json"),
        )
        self.switch(True)

    def switch(self, enabled: bool) -> None:
        """Flip `toast.enabled`. Bumps mtime, which is what ConfigReader watches."""
        self.config.write_text(
            json.dumps({"toast": {"enabled": enabled},
                        "digest": {"enabled": True, "time": "09:00"}}),
            encoding="utf-8",
        )
        # ConfigReader keys on (mtime_ns, size), and two writes inside one filesystem
        # tick can collide on both. Force a distinct stamp rather than sleep.
        stat = self.config.stat()
        import os
        os.utime(self.config, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000 * (1 + enabled)))

    def serve(self, state) -> None:
        sidecrab_toast.fetch_state = lambda *a, **k: state

    def poll(self, when: datetime | None = None) -> list[ToastRequest]:
        return self.notifier.poll_once(now=when or UTC_NOW)

    def titles(self) -> list[str]:
        return [r.title for r in self.adapter.shown]


# --------------------------------------------------------------- the switch means all six


class TheSwitchMutesEveryKind(MuteHarness):
    def test_enabled_true_is_the_control(self) -> None:
        """Proves the feed really does owe all five session/periodic toasts — a
        mute test that passes because nothing was owed proves nothing."""
        self.serve(feed(waiting_session(), approval_session(), working_session()))
        self.poll()
        self.serve(feed(waiting_session(), approval_session(), done_session()))
        self.poll()
        kinds = {toast_kind(r) for r in self.adapter.shown}
        self.assertEqual(kinds, {"waiting", "approval", "longrun", "digest", "budget"})

    def test_muted_fires_nothing_at_all(self) -> None:
        self.switch(False)
        self.serve(feed(waiting_session(), approval_session(), working_session()))
        self.assertEqual(self.poll(), [])
        self.serve(feed(waiting_session(), approval_session(), done_session()))
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.adapter.shown, [], "the switch is labelled global")

    def test_the_three_that_used_to_escape_are_named(self) -> None:
        """The regression, stated as the finding stated it. Digest, budget and
        outage were governed by their own conditions and nothing else."""
        self.switch(False)
        self.serve(feed())
        self.poll()
        self.assertEqual(
            [t for t in self.titles() if t in (DIGEST_TITLE, BUDGET_TITLE, STALE_TITLE)],
            [],
        )

    def test_the_outage_toast_is_muted_too(self) -> None:
        """It fires on the one poll where crabd is unreachable — a path that used
        to return before the config was even read."""
        self.serve(feed(waiting_session()))
        self.poll()  # someone was recently active, which arms the outage decider
        self.adapter.shown.clear()
        self.switch(False)
        self.serve(None)
        self.assertEqual(self.poll(UTC_NOW + timedelta(minutes=10)), [])
        self.assertEqual(self.adapter.shown, [])

    def test_an_unsupported_schema_poll_is_muted_too(self) -> None:
        self.serve(feed(waiting_session()))
        self.poll()
        self.adapter.shown.clear()
        self.switch(False)
        self.serve({"schema": 99})
        self.assertEqual(self.poll(UTC_NOW + timedelta(minutes=10)), [])


# ------------------------------------------------- consumption: live signals must NOT spend


class MutedLiveSignalsReArm(MuteHarness):
    """A question is still open when the switch comes back on. Losing it silently
    is the bug the whole notifier exists to prevent — muting must defer it, not
    spend it."""

    def test_a_waiting_question_survives_the_mute(self) -> None:
        self.switch(False)
        self.serve(feed(waiting_session()))
        self.poll()
        self.assertEqual(self.adapter.shown, [])

        self.switch(True)
        self.poll()
        self.assertEqual([toast_kind(r) for r in self.adapter.shown], ["waiting"])

    def test_a_parked_permission_survives_the_mute(self) -> None:
        self.switch(False)
        self.serve(feed(approval_session()))
        self.poll()
        self.assertEqual(self.adapter.shown, [])

        self.switch(True)
        self.poll()
        self.assertEqual([r.title for r in self.adapter.shown], [APPROVAL_TITLE])

    def test_muting_writes_no_waiting_ledger_entry(self) -> None:
        """The mechanism, not just the outcome: the spell must be unmarked, so a
        fresh look at the same feed still owes the toast."""
        self.switch(False)
        self.serve(feed(waiting_session()))
        self.poll()
        self.assertFalse(
            self.notifier.decider._resolved("s1", ago(600)),
            "a muted question was marked as already toasted",
        )


# --------------------------------------------- consumption: periodics spend, as designed


class MutedPeriodicsStillConsume(MuteHarness):
    """Unchanged from the shipped rule, and deliberately so: this is the same
    "suppress AND mark, never defer" quiet hours has always followed. A digest
    that queued up while muted would arrive as yesterday's news at an arbitrary
    hour, which is the failure the rule exists to prevent."""

    def test_the_digest_day_is_spent_while_muted(self) -> None:
        self.switch(False)
        self.serve(feed())
        self.poll()
        self.assertEqual(self.adapter.shown, [])

        self.switch(True)
        self.poll(LOCAL_NOW.astimezone(timezone.utc) + timedelta(hours=1))
        self.assertNotIn(DIGEST_TITLE, self.titles(), "the day was already marked")

    def test_the_budget_day_is_spent_while_muted(self) -> None:
        self.switch(False)
        self.serve(feed())
        self.poll()
        self.switch(True)
        self.poll(UTC_NOW + timedelta(hours=1))
        self.assertNotIn(BUDGET_TITLE, self.titles())

    def test_the_outage_is_spent_until_a_recovery(self) -> None:
        self.serve(feed(waiting_session()))
        self.poll()
        self.switch(False)
        self.serve(None)
        self.poll(UTC_NOW + timedelta(minutes=10))

        self.switch(True)
        self.assertEqual(self.poll(UTC_NOW + timedelta(minutes=11)), [],
                         "one toast per outage, re-armed only by a recovery")

    def test_a_recovery_re_arms_the_outage_after_a_mute(self) -> None:
        """The other side of the same rule — otherwise "consumed" would mean
        "silenced forever", which is a different and much worse behaviour."""
        self.serve(feed(waiting_session()))
        self.poll()
        self.switch(False)
        self.serve(None)
        self.poll(UTC_NOW + timedelta(minutes=10))

        self.switch(True)
        self.serve(feed(waiting_session(), generated=iso(UTC_NOW + timedelta(minutes=20))))
        self.poll(UTC_NOW + timedelta(minutes=20))  # healthy: re-arms
        self.serve(None)
        self.poll(UTC_NOW + timedelta(minutes=30))
        self.assertIn(STALE_TITLE, self.titles())


# ----------------------------------------- consumption: long-run spends its EDGE while muted


class MutedLongRunConsumesTheEdge(MuteHarness):
    """long-run is edge-triggered, not persistent, so it belongs with the consume set,
    not the re-arm set — the missing row in the matrix waiting/approval/digest/budget
    already pin.

    `LongRunDecider.evaluate` does its observation swap (`prev, self._prev = self._prev,
    current`) UNCONDITIONALLY, ahead of the `enabled` gate. So a `working -> done` that
    COMPLETES while the switch is off advances `_prev` to done and consumes the edge; on
    unmute the feed shows `done == _prev`, there is no edge left, and the completion is
    never toasted. That is intended (README muted column, row 3): a run that finished
    during the mute is stale, and a duration measured against a turn start from whenever
    the switch was last on would be confidently wrong."""

    def test_a_completion_finished_while_muted_does_not_toast_on_unmute(self) -> None:
        self.switch(False)
        self.serve(feed(working_session()))
        self.poll()                        # muted: records `working`
        self.serve(feed(done_session()))
        self.poll()                        # muted: the working->done edge is consumed HERE
        self.assertEqual(self.adapter.shown, [])

        self.switch(True)
        self.serve(feed(done_session()))   # still done, but _prev is already done — no edge
        self.poll(UTC_NOW + timedelta(hours=1))
        self.assertNotIn(
            "longrun", [toast_kind(r) for r in self.adapter.shown],
            "a turn that finished while muted must not re-surface on unmute",
        )

    def test_the_same_edge_observed_while_enabled_still_fires(self) -> None:
        """The control for the test above: the identical working->done edge, observed
        with the switch ON throughout, DOES toast. Proves the consume test passes
        because of the mute, not because this edge simply never toasts."""
        self.serve(feed(working_session()))
        self.poll()
        self.serve(feed(done_session()))
        self.poll()
        self.assertIn("longrun", [toast_kind(r) for r in self.adapter.shown])


# ------------------------------------------------------------------ the once-a-day log line


class TheMutedLineIsRationed(MuteHarness):
    """Once per KIND per day. The notifier polls every 10 s, so a switch left off
    for a day is 8,640 polls: per-attempt logging would bury the log it is
    supposed to explain.

    Two shapes of line, because the mute happens at two depths. The three
    session-keyed deciders stand down inside themselves (that is what keeps their
    spell unmarked), so they never reach the emit seam and cannot be named there —
    the aggregate line covers them. The three that DO reach it are named
    individually.
    """

    def muted_lines(self, captured) -> list[str]:
        return [m for m in captured.output if "toast.enabled=false" in m]

    def test_one_line_per_kind_however_many_polls(self) -> None:
        self.switch(False)
        self.serve(feed(waiting_session(), approval_session()))
        with self.assertLogs("sidecrab.notifier", "INFO") as captured:
            for _ in range(5):
                self.poll()
        lines = self.muted_lines(captured)
        self.assertEqual(len(lines), 3, lines)  # the switch line, digest, budget

    def test_the_aggregate_line_says_the_mute_is_global(self) -> None:
        """The line an operator actually needs: the three that never reach the
        emit seam are the three whose silence is otherwise unexplained."""
        self.switch(False)
        self.serve(feed(waiting_session()))
        with self.assertLogs("sidecrab.notifier", "INFO") as captured:
            self.poll()
        line = next(m for m in self.muted_lines(captured) if "ALL toast kinds" in m)
        for kind in ("waiting", "approval", "longrun", "digest", "budget", "outage"):
            self.assertIn(kind, line)

    def test_the_per_kind_line_names_the_kind(self) -> None:
        self.switch(False)
        self.serve(feed())
        with self.assertLogs("sidecrab.notifier", "INFO") as captured:
            self.poll()
        self.assertTrue(
            any("digest toast suppressed" in m for m in captured.output), captured.output
        )

    def test_a_new_day_says_it_again(self) -> None:
        """A line rationed per PROCESS would go silent on a box that never
        restarts, which is the shape this notifier actually runs in."""
        self.switch(False)
        self.serve(feed(waiting_session()))
        with self.assertLogs("sidecrab.notifier", "INFO") as captured:
            self.poll()
            self.poll(UTC_NOW + timedelta(days=1))
        lines = [m for m in self.muted_lines(captured) if "ALL toast kinds" in m]
        self.assertEqual(len(lines), 2)

    def test_an_enabled_switch_logs_no_mute_line(self) -> None:
        self.serve(feed(waiting_session()))
        with self.assertLogs("sidecrab.notifier", "INFO") as captured:
            self.poll()
        self.assertEqual(self.muted_lines(captured), [])


# ------------------------------------------------------------------------- the _emit seam


class EmitLevelMute(unittest.TestCase):
    """`_emit` in isolation — the single point every toast passes through, which
    is what makes "all six" structural rather than a list to keep extending."""

    class SpyOwner:
        def __init__(self) -> None:
            self.rearmed: list[str] = []

        def unresolve(self, request: ToastRequest) -> None:
            self.rearmed.append(request.session_id)

    def emit(self, requests, config, owner=None):
        adapter = RecordingToastAdapter()
        notifier = Notifier(adapter)
        owners = {id(r): owner for r in requests} if owner is not None else {}
        fired = notifier._emit(list(requests), owners, config, UTC_NOW)
        return fired, adapter

    def request(self, sid: str = "s1") -> ToastRequest:
        return ToastRequest(session_id=sid, state_since=ago(600), title="t", body="b")

    def test_a_disabled_config_shows_nothing(self) -> None:
        fired, adapter = self.emit([self.request()], ToastConfig(enabled=False))
        self.assertEqual(fired, [])
        self.assertEqual(adapter.shown, [])

    def test_an_owned_request_is_re_armed_rather_than_spent(self) -> None:
        owner = self.SpyOwner()
        self.emit([self.request()], ToastConfig(enabled=False), owner)
        self.assertEqual(owner.rearmed, ["s1"])

    def test_an_unowned_request_is_simply_dropped(self) -> None:
        fired, adapter = self.emit(
            [ToastRequest(session_id=STALE_ID, state_since=ago(600), title="t", body="b")],
            ToastConfig(enabled=False),
        )
        self.assertEqual((fired, adapter.shown), ([], []))

    def test_an_enabled_config_shows_normally(self) -> None:
        fired, adapter = self.emit([self.request()], ToastConfig(enabled=True))
        self.assertEqual(len(fired), 1)
        self.assertEqual(len(adapter.shown), 1)

    def test_a_raising_owner_does_not_abandon_the_rest_of_the_batch(self) -> None:
        """The v0.18.0 guarantee, carried onto the muted path: one poisoned
        request costs its own bookkeeping and nothing else. Without the guard the
        first raise would re-arm some spells and silently spend the others."""

        class Poison:
            def unresolve(self, request: ToastRequest) -> None:
                raise RuntimeError("unresolve blew up")

        good = self.SpyOwner()
        first, second = self.request("s1"), self.request("s2")
        adapter = RecordingToastAdapter()
        notifier = Notifier(adapter)
        owners = {id(first): Poison(), id(second): good}
        with self.assertLogs("sidecrab.notifier", "ERROR"):
            fired = notifier._emit([first, second], owners, ToastConfig(enabled=False), UTC_NOW)
        self.assertEqual(fired, [])
        self.assertEqual(good.rearmed, ["s2"], "the second request was abandoned")

    def test_no_config_never_mutes(self) -> None:
        """The direct-call test seam. A default that could suppress would make a
        forgotten argument look like a working notifier that says nothing."""
        fired, adapter = self.emit([self.request()], None)
        self.assertEqual(len(fired), 1)
        self.assertEqual(len(adapter.shown), 1)


# ------------------------------------------------------------------------- kind derivation


class ToastKind(unittest.TestCase):
    """The kind is read off the id prefix the deciders already key their ledgers
    on, rather than carried as a second field that could disagree with the first."""

    def kind_of(self, sid: str) -> str:
        return toast_kind(ToastRequest(session_id=sid, state_since="x", title="t", body="b"))

    def test_every_id_shape_maps(self) -> None:
        self.assertEqual(self.kind_of("abc-123"), "waiting")
        self.assertEqual(self.kind_of("approval-abc"), "approval")
        self.assertEqual(self.kind_of("longrun-abc"), "longrun")
        self.assertEqual(self.kind_of("digest-2026-08-26"), "digest")
        self.assertEqual(self.kind_of("budget-2026-08-26"), "budget")
        self.assertEqual(self.kind_of(STALE_ID), "outage")

    def test_the_six_kinds_are_the_six_toasts(self) -> None:
        self.assertEqual(len(TOAST_KINDS), 6)
        self.assertEqual(len(set(TOAST_KINDS)), 6)

    def test_every_derived_kind_is_a_declared_kind(self) -> None:
        for sid in ("abc", "approval-a", "longrun-a", "digest-d", "budget-d", STALE_ID):
            self.assertIn(self.kind_of(sid), TOAST_KINDS)

    def test_a_non_string_id_never_raises(self) -> None:
        self.assertEqual(
            toast_kind(ToastRequest(session_id=None, state_since="x", title="t", body="b")),
            "waiting",
        )


if __name__ == "__main__":
    unittest.main()
