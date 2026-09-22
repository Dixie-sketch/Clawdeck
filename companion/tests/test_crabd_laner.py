"""Lane R (contract v0.36.0, provisional label): the hook surface.

Four features and one drift guard, all of them about what Claude Code POSTs to crabd:

  - B1, the per-event `type: "http"` routes, and the compatibility route the old
    fragment on another machine still uses;
  - B3, `notification_type` off the payload instead of a message-string reading;
  - B4, `StopFailure` -> `state: "failed"` and `sessions[].failure`;
  - B5, `SubagentStart`/`SubagentStop` paired by `agent_id`, replacing CD-29's
    nearest-last-write heuristic where ids exist and leaving it alone where they do not;
  - the `MUTATING_PATHS` drift test, which derives the real POST route table from the
    dispatcher's own AST rather than from a hand list.

WHAT IS VERIFIED HERE AND WHAT IS NOT. Everything below is a crabd unit test: it proves
what crabd does with a payload, never that Claude Code sends that payload. The payload
shapes, the `type: "http"` skip on SessionStart, and the fail-open behaviour of an
unreachable endpoint were read out of the shipped `claude.exe` 2.1.278 and the reference
at code.claude.com/docs/en/hooks on 2026-09-22; `docs/notes/lane-r-dev.md` says which
claim came from which. Fixtures are fictional and no test reads the operator's own home.
"""

import ast
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crabd  # noqa: E402
from _httpkeepalive import settle, start_test_server  # noqa: E402
from test_crabd import StubLimits, TempProjects  # noqa: E402


# --------------------------------------------------------------- module isolation

_MODULE_TMP = None


def setUpModule():
    """The same hard isolation lane M's module states: nothing here may read or write
    the operator's own ~/.sidecrab, and a fixture `at` from 1970 in the real limits
    cache makes every age computation on the panel meaningless."""
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    root = Path(_MODULE_TMP.name)
    setUpModule.originals = (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
                             crabd.HISTORY_FILE, crabd.CREDENTIALS_FILE,
                             crabd.CRABD_LOG_FILE)
    crabd.LIMITS_CACHE_FILE = root / "limits-cache.json"
    crabd.USER_CONFIG_FILE = root / "config.json"
    crabd.HISTORY_FILE = root / "history.jsonl"
    crabd.CREDENTIALS_FILE = root / "no-such-credentials.json"
    crabd.CRABD_LOG_FILE = root / "crabd.log"


def tearDownModule():
    (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
     crabd.HISTORY_FILE, crabd.CREDENTIALS_FILE,
     crabd.CRABD_LOG_FILE) = setUpModule.originals
    crabd.Handler.builder = None
    _MODULE_TMP.cleanup()


CRABD_PATH = Path(crabd.__file__)
SID = "1a1a1a1a-0000-0000-0000-00000000000r".replace("r", "9")


def hook(event, session_id=SID, **extra):
    return {"session_id": session_id, "hook_event_name": event,
            "cwd": "D:\\work\\acme", **extra}


# =============================================================== B1: the HTTP routes

class HookRoutes(unittest.TestCase):
    """The per-event ingest routes, over a real socket on a test port.

    Never 2722: that port is production and the Scheduled Task owns it.
    """

    # 100 ms is the brief's ceiling and is two orders of magnitude above what these do -
    # a body read, a 204, and a dict write under a lock. The assertion is not a
    # benchmark; it is the guard on the "answer first, parse after" ORDER. Invert that
    # order and a slow builder pass lands in front of the operator's own session.
    BUDGET_SEC = 0.100

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        projects = Path(self._tmp.name) / "projects"
        projects.mkdir(parents=True)
        self.hooks = crabd.HookTracker()
        self.builder = crabd.StateBuilder(
            crabd.TranscriptStore(projects), self.hooks, StubLimits(), time.time())
        crabd.Handler.builder = self.builder
        self.server, self.thread, self.port, self.client = start_test_server(
            lambda: crabd.CrabdServer(("127.0.0.1", 0), crabd.Handler))
        self.assertNotEqual(self.port, 2722)
        self.addCleanup(self._stop)
        self.addCleanup(self.client.close)

    def _stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def post(self, path, payload):
        import json
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return self.client.post(path, body, timeout=5)

    def test_every_ingest_route_answers_204(self):
        for path in sorted(crabd.Handler.HOOK_INGEST_PATHS):
            with self.subTest(path=path):
                self.assertEqual(self.post(path, hook("SessionStart")).status, 204)

    def test_the_precompact_route_still_answers_204(self):
        self.assertEqual(self.post("/v1/hook/precompact", hook("PreCompact")).status, 204)

    def test_every_hook_route_answers_well_inside_the_budget(self):
        for path in sorted(crabd.Handler.HOOK_INGEST_PATHS | {"/v1/hook/precompact"}):
            started = time.monotonic()
            reply = self.post(path, hook("UserPromptSubmit"))
            elapsed = time.monotonic() - started
            with self.subTest(path=path):
                self.assertEqual(reply.status, 204)
                self.assertLess(elapsed, self.BUDGET_SEC)

    def test_a_malformed_body_is_204_and_never_500(self):
        """DELIBERATELY NOT 400, and this is the one place to say why.

        The ingest routes answer BEFORE they parse, which is what stops a hook holding
        Claude Code open, so a 400 is not reachable without giving that up. It would
        also be worse than useless: measured in the shipped binary, an http hook treats
        any non-2xx as a failed hook and writes `[SideCrab]: ... it answered HTTP 400;
        retry` into the operator's own session. crabd would be painting a warning in
        front of the person for a payload only crabd cares about.
        """
        for body in (b"", b"not json at all", b"[1,2,3]", b"{", b'{"a":' + b"1" * 5000):
            for path in ("/v1/hook", "/v1/hook/prompt", "/v1/hook/precompact"):
                with self.subTest(path=path, body=body[:12]):
                    self.assertEqual(self.post(path, body).status, 204)

    def test_the_compatibility_route_still_feeds_the_state_machine(self):
        """A fragment installed on another machine before v0.36.0 posts every event to
        the bare `/v1/hook`. A 404 there would silently stop that panel's state machine,
        which is why the route is kept rather than deprecated."""
        self.post("/v1/hook", hook("UserPromptSubmit"))
        # settle, not an assertion on the next line: the route answers BEFORE it parses,
        # so the 204 landing does not mean the state machine has moved yet.
        state = settle(lambda: self.hooks.snapshot().get(SID, {}).get("state"),
                       what="the compatibility route's hook record")
        self.assertEqual(state, "working")

    def test_the_payload_decides_the_event_not_the_route(self):
        """The URL is for legibility in a capture and in a log. `hook_event_name` stays
        the single source of truth, so the two can never disagree."""
        self.post("/v1/hook/prompt", hook("SessionEnd"))
        state = settle(lambda: self.hooks.snapshot().get(SID, {}).get("state"),
                       what="the prompt route's hook record")
        self.assertEqual(state, "gone")

    def test_an_unknown_hook_route_is_still_404(self):
        self.assertEqual(self.post("/v1/hook/not-a-route", hook("Stop")).status, 404)


# ============================================ the MUTATING_PATHS drift test (backlog)

def _post_routes_from_source() -> set:
    """Every path `do_POST` dispatches on, derived from ITS OWN AST.

    The backlog row this closes: `MUTATING_PATHS` was documentation, and nothing
    asserted it matched the dispatcher, so a future write path could be added with no
    entry and no test would notice. (It had already drifted - `/v1/hook/precompact`
    shipped in v0.35.0 and was never added.)

    Derived, not listed: a hand list in the test is the same document the set already
    is, and two copies of a list agree with each other rather than with the code. Every
    comparison against the local `path` contributes, whatever shape it takes - a
    constant, a tuple, or a name resolved off the Handler class.
    """
    tree = ast.parse(CRABD_PATH.read_text(encoding="utf-8"))
    do_post = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "do_POST")
    found = set()
    for node in ast.walk(do_post):
        if not isinstance(node, ast.Compare):
            continue
        if not (isinstance(node.left, ast.Name) and node.left.id == "path"):
            continue
        for comparator in node.comparators:
            found |= _strings(comparator)
    return found


def _strings(node) -> set:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        out = set()
        for element in node.elts:
            out |= _strings(element)
        return out
    name = node.attr if isinstance(node, ast.Attribute) else (
        node.id if isinstance(node, ast.Name) else None)
    # A name in the test is a class-level set of paths (HOOK_INGEST_PATHS). Resolved off
    # the live class rather than re-parsed, so the test reads the same object the
    # dispatcher does.
    value = getattr(crabd.Handler, name, None) if name else None
    if isinstance(value, (frozenset, set, tuple, list)):
        return {v for v in value if isinstance(v, str)}
    return set()


class MutatingPathsDrift(unittest.TestCase):
    def test_the_documented_set_is_the_real_post_route_table(self):
        self.assertEqual(_post_routes_from_source(),
                         set(crabd.Handler.MUTATING_PATHS))

    def test_the_derivation_actually_finds_routes(self):
        """The guard on the guard. An AST walk that silently matched nothing would make
        the test above compare two empty sets and pass forever - the failure mode §3.4
        calls worse than a false alarm, because it reports success."""
        routes = _post_routes_from_source()
        self.assertGreater(len(routes), 10)
        for expected in ("/v1/hook", "/v1/action", "/v1/hook/precompact"):
            self.assertIn(expected, routes)


# ================================================== B3: the notification type

class NotificationType(unittest.TestCase):
    """The type comes off the payload (`notification_type`), which the CLI sets whether
    or not a `matcher` is declared. See docs/notes/lane-r-dev.md for why the fragment
    declares none."""

    def setUp(self):
        self.hooks = crabd.HookTracker()

    def send(self, event, **extra):
        self.hooks.record(hook(event, **extra))

    def row(self):
        return self.hooks.snapshot()[SID]

    # Every value in the CLI's live notification-type enum, measured 2026-09-22, and
    # what crabd does with it. The FYI five are the only demotions.
    CASES = {
        "permission_prompt": "needs_input",
        "worker_permission_prompt": "needs_input",
        "idle_prompt": "needs_input",
        "agent_needs_input": "needs_input",
        "elicitation_dialog": "needs_input",
        "elicitation_url_dialog": "needs_input",
        "push_notification": "needs_input",
        "model_refusal_fallback": "needs_input",
        "quota_auto_resume_stale": "needs_input",
        "quota_auto_resume_disabled": "needs_input",
        "agent_completed": "working",
        "auth_success": "working",
        "quota_auto_resume_fired": "working",
        "computer_use_enter": "working",
        "computer_use_exit": "working",
    }

    def test_one_case_per_live_notification_type(self):
        for ntype, expected in self.CASES.items():
            with self.subTest(type=ntype):
                self.hooks = crabd.HookTracker()
                self.send("UserPromptSubmit")
                self.send("Notification", notification_type=ntype,
                          message="Claude needs your permission to use Bash")
                self.assertEqual(self.row()["state"], expected)

    def test_the_demotions_are_exactly_the_fyi_set(self):
        """Pinned against the constant so the two cannot drift apart, and stated as a
        set so ADDING a demotion is a deliberate edit in two places."""
        demoted = {t for t, state in self.CASES.items() if state != "needs_input"}
        self.assertEqual(demoted, set(crabd.NOTIFICATION_FYI))

    def test_an_old_fragment_with_no_type_still_raises_the_alert(self):
        """The fallback, and the reason the FYI list is a DEMOTION list. A fragment from
        before v0.36.0, or a CLI that sends no type, must behave exactly as it did."""
        self.send("UserPromptSubmit")
        self.send("Notification", message="Claude is waiting for your input")
        self.assertEqual(self.row()["state"], "needs_input")
        self.assertEqual(self.row()["question"], "Claude is waiting for your input")

    def test_a_type_this_build_has_never_heard_of_still_raises_the_alert(self):
        self.send("UserPromptSubmit")
        self.send("Notification", notification_type="some_future_type_2030",
                  message="something new")
        self.assertEqual(self.row()["state"], "needs_input")

    def test_an_fyi_leaves_the_state_the_question_and_the_clock_alone(self):
        self.send("UserPromptSubmit")
        since = self.row()["since"]
        self.send("Notification", notification_type="agent_completed",
                  message="Claude is done")
        row = self.row()
        self.assertEqual(row["state"], "working")
        self.assertIsNone(row["question"])
        self.assertEqual(row["since"], since)

    def test_an_fyi_does_not_clear_a_standing_question(self):
        """The direction that would cost the operator an alert: a card genuinely waiting
        must not be stood down by an unrelated "signed in" notification."""
        self.send("Notification", message="Claude needs your permission to use Bash")
        self.send("Notification", notification_type="auth_success", message="Signed in")
        self.assertEqual(self.row()["state"], "needs_input")
        self.assertEqual(self.row()["question"],
                         "Claude needs your permission to use Bash")

    def test_an_fyi_still_lands_in_the_ring_with_its_own_text(self):
        """"asked a question" would be a lie on every one of them."""
        self.send("Notification", notification_type="agent_completed", message="done")
        self.assertEqual(self.row()["events"][0]["text"], "agent finished")

    def test_an_fyi_type_with_no_ring_text_of_its_own_reads_notified(self):
        original = dict(crabd.NOTIFICATION_EVENT_TEXT)
        crabd.NOTIFICATION_EVENT_TEXT.pop("auth_success")
        self.addCleanup(lambda: crabd.NOTIFICATION_EVENT_TEXT.update(original))
        self.send("Notification", notification_type="auth_success", message="hi")
        self.assertEqual(self.row()["events"][0]["text"], "notified")

    def test_a_waiting_type_keeps_the_question_ring_text(self):
        self.send("Notification", notification_type="idle_prompt", message="still there?")
        self.assertEqual(self.row()["events"][0]["text"], "asked a question")

    def test_a_non_string_type_is_treated_as_absent(self):
        self.send("Notification", notification_type={"not": "a string"}, message="x")
        self.assertEqual(self.row()["state"], "needs_input")


# ========================================================= B4: StopFailure

class StopFailureTracker(unittest.TestCase):
    def setUp(self):
        self.hooks = crabd.HookTracker()

    def send(self, event, **extra):
        self.hooks.record(hook(event, **extra))

    def row(self):
        return self.hooks.snapshot()[SID]

    def test_a_failed_turn_is_failed_and_not_done(self):
        self.send("UserPromptSubmit")
        self.send("StopFailure", error="rate_limit")
        row = self.row()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["failure"]["errorType"], "rate_limit")
        self.assertIsNone(row["turn_started"])

    def test_a_failed_turn_is_not_counted_as_done_today(self):
        """The ledger is `doneToday`. A turn that died on a 429 did not finish, and
        counting it would flatter the recap on exactly the worst day."""
        self.send("UserPromptSubmit")
        self.send("StopFailure", error="overloaded")
        self.assertEqual(self.hooks.done_today(), 0)

    def test_both_key_spellings_are_read(self):
        """The binary builds the payload with `error`; the published reference documents
        `error_type`. Reading one and not the other would serve `unknown` on every
        failure if the other is what ships."""
        for key in ("error", "error_type"):
            with self.subTest(key=key):
                self.hooks = crabd.HookTracker()
                self.send("StopFailure", **{key: "billing_error"})
                self.assertEqual(self.row()["failure"]["errorType"], "billing_error")

    def test_error_wins_when_both_are_present_and_disagree(self):
        self.send("StopFailure", error="rate_limit", error_type="server_error")
        self.assertEqual(self.row()["failure"]["errorType"], "rate_limit")

    def test_every_measured_error_type_is_carried_through(self):
        for value in sorted(crabd.STOP_FAILURE_ERRORS):
            with self.subTest(error=value):
                self.hooks = crabd.HookTracker()
                self.send("StopFailure", error=value)
                self.assertEqual(self.row()["failure"]["errorType"], value)

    def test_an_unrecognised_error_becomes_unknown(self):
        """The member is rendered on a screen, so the enum is what bounds it."""
        for value in ("teapot", "", 17, None, "x" * 5000):
            with self.subTest(error=value):
                self.hooks = crabd.HookTracker()
                self.send("StopFailure", error=value)
                self.assertEqual(self.row()["failure"]["errorType"], "unknown")

    def test_a_payload_with_no_error_at_all_is_unknown_not_a_crash(self):
        self.send("StopFailure")
        self.assertEqual(self.row()["failure"]["errorType"], "unknown")

    def test_the_optional_message_is_carried_and_capped(self):
        self.send("StopFailure", error="server_error", error_details="upstream 529")
        self.assertEqual(self.row()["failure"]["message"], "upstream 529")
        self.hooks = crabd.HookTracker()
        self.send("StopFailure", error="server_error", error_details="d" * 4000)
        self.assertLessEqual(len(self.row()["failure"]["message"]),
                             crabd.FAILURE_MESSAGE_MAX)

    def test_the_message_is_absent_when_there_is_none(self):
        self.send("StopFailure", error="rate_limit")
        self.assertNotIn("message", self.row()["failure"])

    def test_the_label_names_the_error(self):
        self.send("StopFailure", error="rate_limit")
        self.assertEqual(self.row()["last_event"], "stopped: rate limit")

    def test_the_next_prompt_clears_the_failure(self):
        self.send("StopFailure", error="rate_limit")
        self.send("UserPromptSubmit")
        self.assertEqual(self.row()["state"], "working")
        self.assertIsNone(self.row()["failure"])

    def test_a_session_start_clears_the_failure(self):
        self.send("StopFailure", error="rate_limit")
        self.send("SessionStart")
        self.assertEqual(self.row()["state"], "idle")
        self.assertIsNone(self.row()["failure"])

    def test_a_stop_after_a_failure_is_the_normal_done(self):
        self.send("StopFailure", error="overloaded")
        self.send("Stop")
        self.assertEqual(self.row()["state"], "done")
        self.assertIsNone(self.row()["failure"])
        self.assertEqual(self.hooks.done_today(), 1)

    def test_a_second_failure_re_dates_the_clock(self):
        """failed -> failed moves nothing through `entered`, and the card would sit on
        the FIRST failure's age through every retry - CD-06's shape, on this state."""
        self.send("StopFailure", error="rate_limit")
        since = self.row()["since"]
        time.sleep(0.01)
        self.send("StopFailure", error="rate_limit")
        self.assertGreater(self.row()["since"], since)

    def test_the_ring_carries_the_failure(self):
        self.send("StopFailure", error="rate_limit")
        self.assertEqual(self.row()["events"][0]["text"], "turn failed")


class StopFailureReplay(unittest.TestCase):
    def test_a_replayed_failure_restores_failed_and_not_working(self):
        """Leaving `turn failed` unmapped would hand _resolve a None it resolves to
        `working` - the failed turn resurrected as a live one, which is the CD-07
        defect verbatim."""
        hooks = crabd.HookTracker()
        now = time.time()
        hooks.replay([(now - 60, "turn failed", SID, "acme")])
        row = hooks.snapshot()[SID]
        self.assertEqual(row["state"], "failed")
        self.assertIsNone(row["failure"])

    def test_a_prompt_after_the_replayed_failure_undoes_the_restore(self):
        hooks = crabd.HookTracker()
        now = time.time()
        hooks.replay([(now - 60, "turn failed", SID, "acme"),
                      (now - 30, "prompt submitted", SID, "acme")])
        self.assertIsNone(hooks.snapshot()[SID]["state"])


class FailedAging(unittest.TestCase):
    """StateBuilder._resolve - `failed` ages exactly as `done` does."""

    NOW = 1_800_000_000.0

    def resolve(self, since, mtime=None, now=None):
        hook_row = {"state": "failed", "since": since, "at": since,
                    "last_event": None, "cwd": None, "stops": []}
        mtime = since if mtime is None else mtime
        return crabd.StateBuilder._resolve(hook_row, mtime, max(mtime, since),
                                           now or self.NOW)

    def test_a_fresh_failure_reads_failed(self):
        state, _ = self.resolve(self.NOW - 30)
        self.assertEqual(state, "failed")

    def test_a_failure_past_the_drop_horizon_goes(self):
        state, _ = self.resolve(self.NOW - (crabd.DONE_DROP_SEC + 60))
        self.assertEqual(state, "gone")

    def test_a_transcript_write_past_the_grace_reactivates_it(self):
        """A rate-limited turn is the one most likely to be retried, and crabd sees the
        retry in the transcript before any hook tells it."""
        since = self.NOW - 60
        state, _ = self.resolve(
            since, mtime=since + crabd.DONE_REACTIVATION_GRACE_SEC + 5)
        self.assertEqual(state, "working")

    def test_it_ages_on_the_same_horizon_as_done(self):
        for offset in (5, crabd.DONE_DROP_SEC - 5, crabd.DONE_DROP_SEC + 5):
            with self.subTest(offset=offset):
                since = self.NOW - offset
                failed, _ = self.resolve(since)
                done, _ = crabd.StateBuilder._resolve(
                    {"state": "done", "since": since, "at": since, "stops": []},
                    since, since, self.NOW)
                self.assertEqual(failed == "gone", done == "gone")


class FailedOnTheWire(TempProjects):
    """`sessions[].failure` as served, and the honesty rule around it."""

    def served(self, hooks, now=None):
        _builder, state = self.build(now=now, hooks=hooks)
        return {row["id"]: row for row in state["sessions"]}

    def test_the_failed_row_carries_the_failure_member(self):
        hooks = crabd.HookTracker()
        hooks.record(hook("StopFailure", error="rate_limit",
                          error_details="retry after 60s"))
        row = self.served(hooks)[SID]
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["failure"]["errorType"], "rate_limit")
        self.assertEqual(row["failure"]["message"], "retry after 60s")
        self.assertTrue(row["failure"]["at"].endswith("Z"))

    def test_failure_is_ABSENT_on_every_other_state(self):
        """The contract's honesty rule: a `failure` key is a claim that this turn died,
        and a null one is that claim made without evidence."""
        for event in ("SessionStart", "UserPromptSubmit", "Notification", "Stop"):
            with self.subTest(event=event):
                hooks = crabd.HookTracker()
                hooks.record(hook("StopFailure", error="rate_limit"))
                hooks.record(hook(event, message="waiting"))
                row = self.served(hooks)[SID]
                self.assertNotEqual(row["state"], "failed")
                self.assertNotIn("failure", row)

    def test_a_restored_failed_row_serves_no_failure_member(self):
        """history.jsonl holds a kind and a title, never the error, so a `failed` row
        that survived a crabd restart says "it failed" and nothing more. The member is
        optional for exactly this case."""
        hooks = crabd.HookTracker()
        hooks.replay([(time.time() - 30, "turn failed", SID, "acme")])
        row = self.served(hooks)[SID]
        self.assertEqual(row["state"], "failed")
        self.assertNotIn("failure", row)
        self.assertEqual(row["lastEvent"], "stopped on an API error")

    def test_failed_sorts_after_needs_input_and_ahead_of_working(self):
        hooks = crabd.HookTracker()
        hooks.record(hook("StopFailure", session_id="s-failed", error="rate_limit"))
        hooks.record(hook("Notification", session_id="s-waiting", message="answer me"))
        hooks.record(hook("UserPromptSubmit", session_id="s-working"))
        order = [row["id"] for row in self.build(hooks=hooks)[1]["sessions"]]
        self.assertEqual(order[:3], ["s-waiting", "s-failed", "s-working"])

    def test_the_schema_is_still_five(self):
        self.assertEqual(self.build()[1]["schema"], 5)


# ================================================ B5: exact subagent counts

class SubagentPairing(unittest.TestCase):
    def setUp(self):
        self.hooks = crabd.HookTracker()

    def send(self, event, **extra):
        self.hooks.record(hook(event, **extra))

    def row(self):
        return self.hooks.snapshot()[SID]

    def test_a_start_is_recorded_with_its_type(self):
        self.send("SubagentStart", agent_id="a1", agent_type="Explore")
        row = self.row()
        self.assertEqual(row["subagent_ids"]["a1"][0], "Explore")
        self.assertTrue(row["subagent_started"])

    def test_a_start_does_not_move_the_state_machine(self):
        self.send("UserPromptSubmit")
        self.send("SubagentStart", agent_id="a1", agent_type="Explore")
        self.assertEqual(self.row()["state"], "working")

    def test_a_start_writes_NO_ring_event(self):
        """Found in this wave's own recheck. The ring holds eight entries and persists to
        history.jsonl; a fan-out turn launching four subagents would write four starts
        beside its four stops and evict the "prompt submitted" that says what the turn
        is. The start is in `subagents.named` with its id, type and time instead."""
        self.send("UserPromptSubmit")
        self.send("SubagentStart", agent_id="a1", agent_type="Explore")
        self.assertEqual([e["text"] for e in self.row()["events"]], ["prompt submitted"])
        # ...and the STOP still writes one, unchanged.
        self.send("SubagentStop", agent_id="a1")
        self.assertEqual(self.row()["events"][0]["text"], "subagent finished")

    def test_a_stop_with_an_id_pairs_exactly(self):
        self.send("SubagentStart", agent_id="a1", agent_type="Explore")
        self.send("SubagentStart", agent_id="a2", agent_type="Plan")
        self.send("SubagentStop", agent_id="a1")
        self.assertEqual(list(self.row()["subagent_ids"]), ["a2"])

    def test_a_stop_for_an_unknown_id_is_harmless(self):
        self.send("SubagentStart", agent_id="a1", agent_type="Explore")
        self.send("SubagentStop", agent_id="never-started")
        self.assertEqual(list(self.row()["subagent_ids"]), ["a1"])

    def test_a_stop_still_feeds_the_file_claiming_list_on_both_paths(self):
        """`stops` is what _subagent_detail claims FILES with. Dropping the paired ones
        there re-opens CD-29 from the other side: a stopped subagent has the NEWEST
        mtime, so it is the one the panel would name as running."""
        self.send("SubagentStart", agent_id="a1", agent_type="Explore")
        self.send("SubagentStop", agent_id="a1")
        self.send("SubagentStop")
        self.assertEqual(len(self.row()["stops"]), 2)

    def test_a_start_with_no_id_does_not_latch_the_id_path(self):
        """An id-less start proves nothing about pairing, and latching on it would make
        `running` read 0 for every subagent in the session."""
        self.send("SubagentStart", agent_type="Explore")
        self.assertFalse(self.row()["subagent_started"])

    def test_an_orphan_start_ages_out(self):
        self.send("SubagentStart", agent_id="a1", agent_type="Explore")
        self.hooks.sessions[SID]["subagent_ids"]["a1"] = (
            "Explore", time.time() - crabd.SUBAGENT_ORPHAN_SEC - 60)
        self.hooks.prune(time.time())
        self.assertEqual(self.row()["subagent_ids"], {})
        # The LATCH survives: this session pairs, and one abandoned turn must not send
        # it back to the heuristic for the rest of the day.
        self.assertTrue(self.row()["subagent_started"])

    def test_a_live_start_is_not_pruned(self):
        self.send("SubagentStart", agent_id="a1", agent_type="Explore")
        self.hooks.prune(time.time())
        self.assertEqual(list(self.row()["subagent_ids"]), ["a1"])

    def test_the_snapshot_copies_the_ids(self):
        """A build on another thread iterates the snapshot while a hook POST mutates the
        live row - the CRB-F2 shape."""
        self.send("SubagentStart", agent_id="a1", agent_type="Explore")
        snap = self.hooks.snapshot()
        self.assertIsNot(snap[SID]["subagent_ids"],
                         self.hooks.sessions[SID]["subagent_ids"])

    def test_concurrent_starts_and_stops_do_not_race(self):
        def work(base):
            for i in range(50):
                self.send("SubagentStart", agent_id=f"{base}-{i}", agent_type="Explore")
                self.send("SubagentStop", agent_id=f"{base}-{i}")
        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(self.row()["subagent_ids"], {})


class SubagentsServed(TempProjects):
    """`subagents.running` and `subagents.named` as served."""

    def row(self, hooks):
        return {r["id"]: r for r in self.build(hooks=hooks)[1]["sessions"]}[SID]

    def test_running_is_exact_once_ids_are_in_play(self):
        hooks = crabd.HookTracker()
        for i, kind in enumerate(("Explore", "Plan", "claude")):
            hooks.record(hook("SubagentStart", agent_id=f"a{i}", agent_type=kind))
        hooks.record(hook("SubagentStop", agent_id="a1"))
        self.assertEqual(self.row(hooks)["subagents"]["running"], 2)

    def test_named_carries_the_id_type_and_start_time(self):
        hooks = crabd.HookTracker()
        hooks.record(hook("SubagentStart", agent_id="a1", agent_type="Explore"))
        named = self.row(hooks)["subagents"]["named"]
        self.assertEqual(len(named), 1)
        self.assertEqual(named[0]["id"], "a1")
        self.assertEqual(named[0]["type"], "Explore")
        self.assertTrue(named[0]["startedAt"].endswith("Z"))

    def test_a_start_with_no_type_still_names_something(self):
        hooks = crabd.HookTracker()
        hooks.record(hook("SubagentStart", agent_id="a1"))
        self.assertEqual(self.row(hooks)["subagents"]["named"][0]["type"], "agent")

    def test_named_is_newest_first_and_capped(self):
        hooks = crabd.HookTracker()
        for i in range(crabd.SUBAGENTS_NAMED_CAP + 4):
            hooks.record(hook("SubagentStart", agent_id=f"a{i}", agent_type="Explore"))
        named = self.row(hooks)["subagents"]["named"]
        self.assertEqual(len(named), crabd.SUBAGENTS_NAMED_CAP)
        self.assertGreaterEqual(named[0]["startedAt"], named[-1]["startedAt"])

    def test_named_is_ABSENT_when_no_id_is_known(self):
        """Presence is the feature detection. An empty list would be crabd claiming it
        looked and found no subagents, on a session it may simply not pair for."""
        hooks = crabd.HookTracker()
        hooks.record(hook("UserPromptSubmit"))
        self.assertNotIn("named", self.row(hooks)["subagents"])

    def test_named_is_ABSENT_once_the_last_subagent_stops(self):
        hooks = crabd.HookTracker()
        hooks.record(hook("SubagentStart", agent_id="a1", agent_type="Explore"))
        hooks.record(hook("SubagentStop", agent_id="a1"))
        row = self.row(hooks)
        self.assertEqual(row["subagents"]["running"], 0)
        self.assertNotIn("named", row["subagents"])

    def test_the_heuristic_is_untouched_for_a_session_with_no_start(self):
        """CD-29's row said REPLACE the heuristic where ids exist, never tune it. A CLI
        that sends no SubagentStart, and a crabd that started mid-subagent, both land
        here and must get exactly the pre-v0.36.0 answer."""
        hooks = crabd.HookTracker()
        hooks.record(hook("SubagentStop"))
        hooks.record(hook("SubagentStop"))
        info = dict(crabd.StateBuilder._blank_session(), sub_active=3, sub_total=3)
        running, named = crabd.StateBuilder._subagents(
            info, hooks.snapshot()[SID], time.time())
        self.assertEqual(running, 1)
        self.assertEqual(named, [])

    def test_an_id_carrying_stop_alone_does_not_switch_the_source(self):
        """THE JUDGEMENT CALL. crabd restarted between a subagent's start and its stop
        holds the stop and never saw the start. Trusting ids there would serve 0 for a
        subagent that is still running - the one direction of wrong the badge must not
        have - so the LATCH is the start, never the stop."""
        hooks = crabd.HookTracker()
        hooks.record(hook("SubagentStop", agent_id="started-before-crabd"))
        info = dict(crabd.StateBuilder._blank_session(), sub_active=2, sub_total=2)
        running, _named = crabd.StateBuilder._subagents(
            info, hooks.snapshot()[SID], time.time())
        self.assertEqual(running, 1)

    def test_a_session_with_no_hook_row_at_all_keeps_the_transcript_count(self):
        info = dict(crabd.StateBuilder._blank_session(), sub_active=2, sub_total=2)
        self.assertEqual(crabd.StateBuilder._subagents(info, None, time.time()),
                         (2, []))

    def test_total_still_comes_from_the_transcript(self):
        """`total` is every subagent file this session owns; only `running` moved."""
        hooks = crabd.HookTracker()
        hooks.record(hook("SubagentStart", agent_id="a1", agent_type="Explore"))
        self.assertEqual(self.row(hooks)["subagents"]["total"], 0)


# ============================================================ the suite's own hygiene

class SuiteHygiene(unittest.TestCase):
    def test_no_companion_test_module_compiles_with_a_syntax_warning(self):
        """A `SyntaxWarning: invalid escape sequence` is a string that does not say what
        it reads like it says, and it printed on every run of this suite until
        2026-09-22 (test_crabd_livefire.py line 860, a Windows path in a non-raw
        string). Python 3.12 warns; a later one makes it an error."""
        for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
            with self.subTest(module=path.name):
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always")
                    compile(path.read_text(encoding="utf-8"), str(path), "exec")
                self.assertEqual(
                    [str(w.message) for w in caught
                     if issubclass(w.category, SyntaxWarning)], [])


if __name__ == "__main__":
    unittest.main()
