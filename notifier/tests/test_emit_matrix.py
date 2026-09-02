"""The decider x failure-shape matrix for `Notifier._emit` (v0.18.0).

The question this file closes, from docs/BACKLOG.md: the v0.16.0 un-resolve fix re-arms a
spell when `show()` returns False, but only for the two live-signal deciders — and only for
that ONE failure shape. What happens to every other (decider, failure shape) pair was
unanswered, and the answer turned out to be worse than the row suggested.

MEASURED 2026-08-27 against the pre-fix code, three matured waiting questions in one poll
with the adapter raising on the first: `show()` was called ONCE, the exception escaped
`_emit` entirely, and all three spells stayed resolved. One poisoned payload silently
buried three live questions — two of which were never even attempted.

The fix is one guard, and its point is that the SHAPE stops mattering: a render that
raises is a render that failed, handled identically to one that returned False, and the
rest of the batch is attempted either way. Which deciders then RETRY is unchanged and
deliberate — see the matrix in notifier/README.md.

Stdlib unittest only, no Windows, no network.

    python -m unittest discover -s notifier/tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOTIFIER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NOTIFIER_DIR))

import sidecrab_toast  # noqa: E402
from sidecrab_toast import (  # noqa: E402
    APPROVAL_ID_PREFIX,
    APPROVAL_TITLE,
    DEFAULT_LONG_RUN_SEC,
    STALE_ACTIVITY_WINDOW_SEC,
    STALE_FEED_MAX_AGE_SEC,
    BudgetLedger,
    ConfigReader,
    DigestLedger,
    Notifier,
    RecordingToastAdapter,
    SnoozeLedger,
    ToastRequest,
)

T0 = datetime(2026, 8, 27, 9, 0, 0, tzinfo=timezone.utc)


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def request(sid: str = "s1") -> ToastRequest:
    return ToastRequest(session_id=sid, state_since=iso(T0), title="t", body="b")


# --------------------------------------------------------------------------- adapters


class RaisingAdapter:
    """An adapter that BLOWS UP rather than returning False.

    Not a hypothetical: `PowerShellToastAdapter.show` builds its XML and its script OUTSIDE
    its own try block, so a payload the builder cannot construct reaches `_emit` as an
    exception. Everything the adapter itself catches (OSError, SubprocessError, a non-zero
    PowerShell exit, LoadXml rejecting the XML) comes back as False instead.
    """

    def __init__(self, exc: BaseException | None = None, fail_first: int | None = None) -> None:
        self.exc = exc or RuntimeError("build_xml blew up")
        self.fail_first = fail_first
        self.attempted: list[str] = []
        self.shown: list[ToastRequest] = []

    def show(self, request: ToastRequest) -> bool:
        self.attempted.append(request.session_id)
        if self.fail_first is None or len(self.attempted) <= self.fail_first:
            raise self.exc
        self.shown.append(request)
        return True


class NoneReturningAdapter:
    """A third-party adapter that forgets to return. Falsy is a failure, not a success."""

    def __init__(self) -> None:
        self.attempted: list[str] = []

    def show(self, request: ToastRequest) -> None:
        self.attempted.append(request.session_id)
        return None


class SpyOwner:
    """Stands in for a decider that registered itself as a request's owner."""

    def __init__(self) -> None:
        self.rearmed: list[str] = []

    def unresolve(self, request: ToastRequest) -> None:
        self.rearmed.append(request.session_id)


# ------------------------------------------------------------- _emit, shape by shape


class EmitFailureShapeTests(unittest.TestCase):
    """`_emit` in isolation: the two failure shapes must be indistinguishable."""

    def emit(self, adapter, requests, owner=None):
        notifier = Notifier(adapter)
        owners = {id(r): owner for r in requests} if owner is not None else {}
        return notifier._emit(list(requests), owners)

    def test_a_raise_and_a_false_are_the_same_failure(self) -> None:
        """THE FIX, stated as an equivalence. Before it, the left column re-armed and the
        right column threw — the same lost toast with two different outcomes."""
        req = request()
        for adapter in (RecordingToastAdapter(succeed=False), RaisingAdapter()):
            with self.subTest(adapter=type(adapter).__name__):
                owner = SpyOwner()
                self.assertEqual(self.emit(adapter, [req], owner), [])
                self.assertEqual(owner.rearmed, ["s1"], "a failed render re-arms, however it failed")

    def test_a_raise_on_one_request_does_not_abandon_the_rest(self) -> None:
        """The collateral half, and the one the backlog row missed. Requests are independent
        spells; a poll that owes three of them must attempt three of them."""
        requests = [request("s1"), request("s2"), request("s3")]
        adapter = RaisingAdapter(fail_first=1)
        owner = SpyOwner()
        fired = self.emit(adapter, requests, owner)
        self.assertEqual(adapter.attempted, ["s1", "s2", "s3"], "every request is attempted")
        self.assertEqual([r.session_id for r in fired], ["s2", "s3"])
        self.assertEqual(owner.rearmed, ["s1"], "only the one that failed is re-armed")

    def test_every_request_fails_independently(self) -> None:
        """All three raise: three attempts, three re-arms, no escape."""
        requests = [request("s1"), request("s2"), request("s3")]
        adapter = RaisingAdapter()
        owner = SpyOwner()
        self.assertEqual(self.emit(adapter, requests, owner), [])
        self.assertEqual(adapter.attempted, ["s1", "s2", "s3"])
        self.assertEqual(owner.rearmed, ["s1", "s2", "s3"])

    def test_a_request_with_no_owner_is_never_re_armed_under_either_shape(self) -> None:
        """Consume-on-attempt is STRUCTURAL, not a branch: the digest, budget, outage and
        long-run toasts register no owner, so there is nothing for `_emit` to re-arm. This
        is the by-design half of the matrix and the fix must not have changed it — a
        periodic toast re-firing every 10 s is worse than one missed."""
        for adapter in (RecordingToastAdapter(succeed=False), RaisingAdapter(),
                        NoneReturningAdapter()):
            with self.subTest(adapter=type(adapter).__name__):
                self.assertEqual(self.emit(adapter, [request()]), [])

    def test_a_none_returning_adapter_counts_as_a_failure(self) -> None:
        """Falsy is failure. The safe direction: a retry costs one duplicate toast, a
        wrongly-believed success costs the signal."""
        owner = SpyOwner()
        self.assertEqual(self.emit(NoneReturningAdapter(), [request()], owner), [])
        self.assertEqual(owner.rearmed, ["s1"])

    def test_a_keyboard_interrupt_still_stops_the_daemon(self) -> None:
        """Exception, not BaseException. Swallowing a Ctrl-C or a SystemExit here would make
        the notifier unkillable through its own poll loop."""
        for exc in (KeyboardInterrupt(), SystemExit()):
            with self.subTest(exc=type(exc).__name__):
                with self.assertRaises(type(exc)):
                    self.emit(RaisingAdapter(exc=exc), [request()], SpyOwner())

    def test_the_failure_is_logged_with_its_traceback(self) -> None:
        """A swallowed exception with no traceback is an invisible bug. `log.exception`
        is what makes a poisoned payload diagnosable from notifier.log alone."""
        with self.assertLogs("sidecrab.notifier", level="ERROR") as captured:
            self.emit(RaisingAdapter(), [request()], SpyOwner())
        joined = "\n".join(captured.output)
        self.assertIn("toast emission raised for session=s1", joined)
        self.assertIn("RuntimeError: build_xml blew up", joined, "the cause travels with it")


# ------------------------------------------------------------------ end to end, per decider


class PollFixture(unittest.TestCase):
    """A real `poll_once` on a real config path, with the feed stubbed."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._fetch = sidecrab_toast.fetch_state
        self.addCleanup(lambda: setattr(sidecrab_toast, "fetch_state", self._fetch))
        root = Path(self.tmp.name)
        config = root / "config.json"
        config.write_text(json.dumps({"toast": {"enabled": True}}), encoding="utf-8")
        self.adapter = RaisingAdapter()
        self.notifier = Notifier(
            adapter=self.adapter,
            config_reader=ConfigReader(config),
            digest_ledger=DigestLedger(root / "toast-state.json"),
            budget_ledger=BudgetLedger(root / "toast-state.json"),
            snooze_ledger=SnoozeLedger(root / "toast-state.json"),
        )

    def serve(self, state) -> None:
        sidecrab_toast.fetch_state = lambda *a, **k: state

    def good_adapter(self) -> RecordingToastAdapter:
        self.notifier.adapter = RecordingToastAdapter()
        return self.notifier.adapter


def feed(*sessions: dict, stamped: datetime = T0) -> dict:
    return {
        "schema": 5,
        "generatedAt": iso(stamped),
        "sessions": list(sessions),
        "quiet": {"active": False, "start": "22:00", "end": "07:00"},
    }


class LiveSignalSpellsSurviveARaiseTests(PollFixture):
    """The two cells the fix changes: a live, actionable signal is RETRIED after a raise."""

    def waiting(self) -> dict:
        return {"id": "s1", "title": "the lane", "state": "needs_input",
                "stateSince": iso(T0), "lastEvent": "asked a question"}

    def parked(self) -> dict:
        return {"id": "s1", "title": "the lane", "state": "working", "stateSince": iso(T0),
                "pendingPermission": {"tool": "Bash", "summary": "git push --force",
                                      "requestedAt": iso(T0)}}

    def test_a_waiting_question_survives_a_raising_adapter(self) -> None:
        """Pre-fix this poll threw and the question was gone for good. The spell is a LIVE
        one — the operator is still being waited on — so it retries until it lands."""
        self.serve(feed(self.waiting(), stamped=at(300)))
        self.assertEqual(self.notifier.poll_once(now=at(300)), [], "no toast, and no escape")
        self.assertEqual(self.notifier.poll_once(now=at(310)), [])
        self.assertEqual(self.adapter.attempted, ["s1", "s1"], "retried, not consumed")

        good = self.good_adapter()
        self.assertEqual(len(self.notifier.poll_once(now=at(320))), 1, "it lands once it can")
        self.notifier.poll_once(now=at(330))
        self.assertEqual(len(good.shown), 1, "and is resolved once shown — no re-fire")

    def test_a_parked_permission_survives_a_raising_adapter(self) -> None:
        """The security-relevant one: a tool summary the builder chokes on used to suppress
        the 'Claude needs permission' toast permanently."""
        self.serve(feed(self.parked(), stamped=at(60)))
        self.assertEqual(self.notifier.poll_once(now=at(60)), [])
        self.assertEqual(self.notifier.poll_once(now=at(70)), [])
        expected = f"{APPROVAL_ID_PREFIX}s1"
        self.assertEqual(self.adapter.attempted, [expected, expected])

        good = self.good_adapter()
        fired = self.notifier.poll_once(now=at(80))
        self.assertEqual([r.title for r in fired], [APPROVAL_TITLE])
        self.notifier.poll_once(now=at(90))
        self.assertEqual(len(good.shown), 1)

    def test_one_poisoned_session_does_not_bury_the_others(self) -> None:
        """The measured defect, end to end: three matured questions, the first one poisoned.
        Pre-fix, `show()` ran once and all three spells were consumed."""
        rows = [{"id": f"s{n}", "title": f"lane {n}", "state": "needs_input",
                 "stateSince": iso(T0), "lastEvent": "asked a question"} for n in (1, 2, 3)]
        self.serve(feed(*rows, stamped=at(300)))
        self.notifier.adapter = RaisingAdapter(fail_first=1)
        fired = self.notifier.poll_once(now=at(300))
        self.assertEqual(self.notifier.adapter.attempted, ["s1", "s2", "s3"])
        self.assertEqual(sorted(r.session_id for r in fired), ["s2", "s3"])
        # ...and the one that failed is the only one still owed.
        good = self.good_adapter()
        self.assertEqual([r.session_id for r in self.notifier.poll_once(now=at(310))], ["s1"])
        self.assertEqual(len(good.shown), 1)


class ConsumeOnAttemptCellsAreUnchangedTests(PollFixture):
    """The by-design half. These deciders consume their spell on ATTEMPT, and a raise must
    not turn that into a retry — only into a survivable poll."""

    def test_a_long_run_toast_is_consumed_by_a_raise_as_it_is_by_a_false(self) -> None:
        """Informational, not live: the turn has already finished, so nobody is blocked on
        this toast. Re-firing a completion notice every 10 s is the worse failure. The mark
        stands, and the poll does not throw."""
        working = {"id": "s1", "title": "the lane", "state": "working",
                   "stateSince": iso(T0), "turnStartedAt": iso(T0)}
        finished = at(DEFAULT_LONG_RUN_SEC + 60)
        done = {"id": "s1", "title": "the lane", "state": "done",
                "stateSince": iso(finished), "turnStartedAt": None}

        self.serve(feed(working))
        self.notifier.poll_once(now=at(10))
        self.serve(feed(done, stamped=finished))
        self.assertEqual(self.notifier.poll_once(now=finished), [], "raised, and survived")
        self.assertEqual(len(self.adapter.attempted), 1)
        self.assertIn(iso(T0), self.notifier.long_run_decider._marks["s1"],
                      "the turn stays marked — consume-on-attempt, by design")

    def test_an_outage_toast_is_consumed_by_a_raise_and_re_arms_only_on_recovery(self) -> None:
        """Same rule for the one toast that fires when crabd is DOWN. Retrying it would put
        an outage line on screen every 10 s for as long as the outage lasts."""
        self.serve(feed({"id": "s1", "title": "the lane", "state": "working",
                         "stateSince": iso(T0)}))
        self.notifier.poll_once(now=T0)

        # Past the max feed age (so the outage is real) but still inside the activity
        # window (so someone was working and it is worth saying).
        outage = at(STALE_FEED_MAX_AGE_SEC + 60)
        self.assertLess(STALE_FEED_MAX_AGE_SEC + 70, STALE_ACTIVITY_WINDOW_SEC)
        self.serve(None)
        self.assertEqual(self.notifier.poll_once(now=outage), [], "raised, and survived")
        self.assertEqual(len(self.adapter.attempted), 1)
        self.notifier.poll_once(now=outage + timedelta(seconds=10))
        self.assertEqual(len(self.adapter.attempted), 1, "not retried — one toast per outage")


# ------------------------------------------------------------------------ the written matrix


class MatrixIsDocumentedTests(unittest.TestCase):
    """The deliverable is the ANSWER, not just the fix: this question was re-asked because
    the previous wave left the classification in a code comment and a backlog row."""

    def test_the_readme_carries_the_matrix(self) -> None:
        readme = (NOTIFIER_DIR / "README.md").read_text(encoding="utf-8")
        self.assertIn("decider", readme.lower())
        for decider in ("Waiting session", "Approval", "Long run", "Daily digest",
                        "Budget crossed", "Companion outage"):
            self.assertIn(decider, readme, decider)
        for shape in ("returns False", "raises"):
            self.assertIn(shape, readme, shape)


if __name__ == "__main__":
    unittest.main(verbosity=2)
