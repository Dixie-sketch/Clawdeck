"""v0.14.0 LIVE-FIRE HARDENING tests - the control-surface paths under real traffic.

The statusline, Stop-hook and PermissionRequest endpoints were wired to a real Claude
Code install on 2026-08-26. Until then every payload crabd had ever seen came out of a
fixture in this suite, so the fixtures agreed with the code by construction. This module
covers what a REAL producer can put on those wires that no fixture did:

  - a status line document with `rate_limits` absent entirely (API-key / Bedrock /
    Vertex sessions, and any session before its first API response)
  - a document carrying keys crabd has never heard of, and values outside the range
    `datetime.fromtimestamp` can represent
  - a Stop hook body for a session crabd has never seen - which is EVERY session that
    started before this crabd did
  - a PermissionRequest naming a session that is not on a served row
  - a stop and a permission arriving concurrently for the same session
  - a body larger than expected, including a Content-Length header that lies

The governing rule, and what every test here is really asserting: **nothing may raise**.
Each of these endpoints is answering a process the operator is working in - a hook that
gets no answer is a session that waits for one, and a traceback on the socket after a
204 is a failure nobody sees until the feed goes quiet.

Also covers the two additive v0.14.0 features: `sessions[].queuedContinue` (schema stays
5) and the /v1/health counters.

Held in its own module for the same reason test_crabd_datalane.py is: concurrent authors
against one tree. `python -m unittest discover companion/tests` finds all three.
"""

import contextlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# ...and this directory, so `_httpkeepalive` imports whether the suite is run
# by `unittest discover companion/tests` (which adds it) or by module path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crabd  # noqa: E402
from _httpkeepalive import (KeepAliveClient, quiesce, settle,  # noqa: E402
                            start_test_server)


# --------------------------------------------------------------- module isolation

_MODULE_TMP = None


def setUpModule():
    """The same hard isolation the other two modules use. Not a courtesy: the real
    limits cache under ~ was poisoned with fixture data exactly this way on
    2026-08-26, and an `at` from 1970 makes every age computation meaningless."""
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    root = Path(_MODULE_TMP.name)
    setUpModule.originals = (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
                             crabd.HISTORY_FILE)
    crabd.LIMITS_CACHE_FILE = root / "limits-cache.json"
    crabd.USER_CONFIG_FILE = root / "config.json"
    crabd.HISTORY_FILE = root / "history.jsonl"


def tearDownModule():
    (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
     crabd.HISTORY_FILE) = setUpModule.originals
    # SELF-ISOLATING (2026-08-27): the fixtures leave a builder on the Handler CLASS, and
    # a builder outliving its module points at a TemporaryDirectory that is about to be
    # deleted. unittest happens to run these modules one after another; pytest gives no
    # such guarantee, and a stale class attribute is the kind of leak that shows up as
    # another module's test failing. Cleared here so the module hands back what it found.
    crabd.Handler.builder = None
    _MODULE_TMP.cleanup()


OAUTH_LIMITS = {
    "available": True, "note": None,
    "fiveHour": {"utilization": 0.42, "resetsAt": "2026-08-26T21:00:00Z"},
    "weekly": {"utilization": 0.18, "resetsAt": "2026-08-30T00:00:00Z"},
    "extra": [], "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x",
}


class StubLimits:
    def get(self, now, force=False):
        return dict(OAUTH_LIMITS)


# ============================================================ pure-function bounds

def decide_body(fixture, decision, session_id=None):
    """The v0.29.0 decide body: the pairing code + the pending request's id."""
    sid = session_id or fixture.SID
    pending = fixture.permissions.pending(sid) or {}
    body = {"sessionId": sid, "action": "decide", "decision": decision,
            "token": fixture.TOKEN}
    if pending.get("requestId"):
        body["requestId"] = pending["requestId"]
    return body


class TimestampBoundTests(unittest.TestCase):
    """_parse_ts / _utc_iso - the pair that put a traceback on a live socket.

    MEASURED 2026-08-26 against a running crabd: a status line document carrying
    `resets_at: 1e30` walked _parse_ts -> _utc_iso -> datetime.fromtimestamp and raised
    OverflowError inside the handler, AFTER the 204 had already gone out. On this
    Windows host fromtimestamp is not total in either direction - it raises OSError
    below epoch 0 and OverflowError past the platform time_t - so a plain "is it a
    number" check was never enough.
    """

    def test_an_out_of_range_epoch_is_none_not_a_clamped_number(self):
        """None, deliberately: 'the producer sent a timestamp we cannot represent' is
        the same fact as 'the producer sent no timestamp'. A clamp would put 1970 on a
        reset gauge, which the widget renders as a real reading."""
        for value in (1e30, -1e30, 1e18, 10 ** 20, -1, crabd.TS_MAX_EPOCH + 1):
            self.assertIsNone(crabd._parse_ts(value), value)

    def test_the_range_that_matters_still_parses(self):
        for value in (0, 1.0, 1756000000, 1756000000.5, crabd.TS_MAX_EPOCH):
            self.assertEqual(crabd._parse_ts(value), float(value), value)

    def test_milliseconds_are_still_detected_and_still_bounded(self):
        self.assertEqual(crabd._parse_ts(1756000000000), 1756000000.0)
        self.assertIsNone(crabd._parse_ts(1e30))

    def test_an_iso_string_outside_the_platform_range_is_none(self):
        """fromisoformat happily builds year 1 and year 9999; .timestamp() on this host
        raises for both. Bounding the PARSER covers every caller at once."""
        for text in ("0001-01-01T00:00:00+00:00", "1960-01-01T00:00:00Z",
                     "9999-12-31T23:59:59+00:00"):
            self.assertIsNone(crabd._parse_ts(text), text)
        self.assertIsNotNone(crabd._parse_ts("2026-08-26T17:39:25.954Z"))

    def test_a_bool_is_not_a_timestamp(self):
        """True is an int in Python and would parse as epoch 1."""
        self.assertIsNone(crabd._parse_ts(True))
        self.assertIsNone(crabd._parse_ts(False))

    def test_utc_iso_cannot_raise_for_any_input(self):
        """The belt under the parser. _parse_ts already refuses these, so this can only
        fire on an internal number - but every endpoint formats a timestamp somewhere
        and none of them may be the thing that raises."""
        for value in (1e30, -1e30, 0, float("nan"), float("inf"), float("-inf"),
                      10 ** 30, "not a number", None):
            self.assertRegex(crabd._utc_iso(value),
                             r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class SessionIdBoundTests(unittest.TestCase):
    """_session_id - every untrusted body that names a session goes through here,
    because every one of those ids becomes a dict key in a table that lives for hours."""

    def test_a_real_uuid_passes(self):
        sid = "7a7a7a7a-0000-0000-0000-000000000004"
        self.assertEqual(crabd._session_id({"session_id": sid}), sid)
        self.assertEqual(crabd._session_id({"sessionId": sid}), sid)

    def test_an_absurdly_long_id_is_refused(self):
        big = "x" * (crabd.SESSION_ID_MAX + 1)
        self.assertIsNone(crabd._session_id({"session_id": big}))
        self.assertEqual(crabd._session_id({"session_id": "x" * crabd.SESSION_ID_MAX}),
                         "x" * crabd.SESSION_ID_MAX)

    def test_anything_that_is_not_a_non_empty_string_is_none(self):
        for body in ({}, {"session_id": ""}, {"session_id": None},
                     {"session_id": 12}, {"session_id": {"a": 1}},
                     {"session_id": ["a"]}, [1, 2], None, "text"):
            self.assertIsNone(crabd._session_id(body), body)

    def test_a_giant_id_cannot_grow_the_statusline_table(self):
        reader = crabd.StatusLineReader()
        reader.ingest({"session_id": "x" * 5000,
                       "context_window": {"total_input_tokens": 10,
                                          "current_usage": {"input_tokens": 1}}},
                      time.time())
        self.assertEqual(reader._sessions, {})
        # The document still counted - health has to see that the feed is arriving.
        self.assertEqual(reader.documents, 1)

    def test_a_giant_id_cannot_grow_the_hook_table(self):
        hooks = crabd.HookTracker()
        hooks.record({"session_id": "x" * 5000, "hook_event_name": "SessionStart"})
        self.assertEqual(hooks.sessions, {})


# ================================================================ the body-read cap

class FakeSocketReader(io.BytesIO):
    """A BytesIO that can be told to raise partway, standing in for a socket that is
    reset or times out mid-body."""

    def __init__(self, data: bytes, raise_after: int | None = None):
        super().__init__(data)
        self.raise_after = raise_after
        self.delivered = 0

    def read(self, size=-1):
        if self.raise_after is not None and self.delivered >= self.raise_after:
            raise TimeoutError("timed out")
        block = super().read(size)
        self.delivered += len(block)
        return block


class ReadBodyCapTests(unittest.TestCase):
    """Handler._read_body - the cap is applied while READING, not after.

    MEASURED 2026-08-26 against a running crabd: `Content-Length: 900000000` on a
    /v1/hook POST made the old code try to buffer 900 MB and block, and the hook never
    got an answer at all. Exercised here without a socket so the assertions can be exact
    about how many bytes are retained and whether the connection is kept.
    """

    def handler(self, body: bytes, headers: dict, raise_after=None):
        handler = crabd.Handler.__new__(crabd.Handler)
        handler.headers = headers
        handler.rfile = FakeSocketReader(body, raise_after=raise_after)
        handler.close_connection = False
        return handler

    def test_a_normal_body_is_returned_whole_and_keeps_the_connection(self):
        raw = json.dumps({"session_id": "s", "hook_event_name": "Stop"}).encode()
        handler = self.handler(raw, {"Content-Length": str(len(raw))})
        self.assertEqual(handler._read_body(), raw)
        self.assertFalse(handler.close_connection)

    def test_at_most_the_cap_plus_one_byte_is_ever_retained(self):
        """The one extra byte is deliberate: each endpoint's own `len(raw) > ITS_CAP`
        test still has to be able to see that the body exceeded its cap."""
        raw = b"x" * (crabd.MAX_BODY_BYTES + 5000)
        handler = self.handler(raw, {"Content-Length": str(len(raw))})
        body = handler._read_body()
        self.assertEqual(len(body), crabd.MAX_BODY_BYTES + 1)

    def test_a_per_endpoint_limit_is_honoured(self):
        raw = b"x" * (crabd.STATUSLINE_MAX_BODY + 100)
        handler = self.handler(raw, {"Content-Length": str(len(raw))})
        body = handler._read_body(limit=crabd.STATUSLINE_MAX_BODY)
        self.assertEqual(len(body), crabd.STATUSLINE_MAX_BODY + 1)
        self.assertGreater(len(body), crabd.STATUSLINE_MAX_BODY)

    def test_an_over_cap_body_inside_the_drain_bound_keeps_the_connection(self):
        raw = b"x" * (crabd.MAX_BODY_BYTES + 1000)
        handler = self.handler(raw, {"Content-Length": str(len(raw))})
        handler._read_body()
        # The tail was discarded, so the next request on this connection still frames.
        self.assertFalse(handler.close_connection)
        self.assertEqual(handler.rfile.read(), b"")

    def test_a_body_past_the_drain_bound_closes_the_connection_instead(self):
        """Draining an unbounded body to be polite about keep-alive is the same cost the
        cap exists to refuse."""
        length = crabd.MAX_BODY_BYTES + crabd.BODY_DRAIN_MAX + 10
        handler = self.handler(b"x" * 200, {"Content-Length": str(length)})
        handler._read_body()
        self.assertTrue(handler.close_connection)

    def test_a_lying_content_length_does_not_raise(self):
        """900 MB announced, seven bytes sent, then the socket gives up. The caller
        still gets bytes back and still answers - a client that lies gets a
        pass-through, not a dropped connection with no response on it."""
        handler = self.handler(b'{"a":1}', {"Content-Length": "900000000"},
                               raise_after=7)
        body = handler._read_body()
        self.assertEqual(body, b'{"a":1}')
        self.assertTrue(handler.close_connection)

    def test_a_non_numeric_content_length_is_zero_not_an_error(self):
        handler = self.handler(b"ignored", {"Content-Length": "banana"})
        self.assertEqual(handler._read_body(), b"")

    def test_a_chunked_body_is_de_chunked_and_bounded(self):
        raw = b"5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n"
        handler = self.handler(raw, {"Transfer-Encoding": "chunked"})
        self.assertEqual(handler._read_body(), b"helloworld")
        self.assertFalse(handler.close_connection)

    def test_a_chunk_header_is_not_a_licence_to_allocate(self):
        """A single chunk announcing 900 MB must not be believed either. Reading past
        the cap closes the connection: the framing is gone once we stop mid-stream."""
        raw = b"35C21F00\r\n" + b"x" * 4096
        handler = self.handler(raw, {"Transfer-Encoding": "chunked"})
        body = handler._read_body(limit=1024)
        self.assertLessEqual(len(body), 4096)
        self.assertTrue(handler.close_connection)

    def test_a_chunked_stream_that_never_ends_is_bounded(self):
        chunk = b"400\r\n" + b"y" * 1024 + b"\r\n"
        handler = self.handler(chunk * 40, {"Transfer-Encoding": "chunked"})
        body = handler._read_body(limit=4096)
        self.assertLessEqual(len(body), 4096 + 1024)
        self.assertTrue(handler.close_connection)


# ================================================== a real crabd on a real socket

class LiveFireServed(unittest.TestCase):
    """A crabd with every reader wired, on a test port, with one SERVED session.

    Never DEFAULT_PORT: that port is production and the service registration owns it.
    """

    SID = "5c5c5c5c-0000-0000-0000-00000000000e"
    GHOST = "deadbeef-0000-0000-0000-000000000000"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        projects = root / "projects"
        projects.mkdir(parents=True)
        self.config_path = root / "config.json"
        self.statusline = crabd.StatusLineReader()
        self.continues = crabd.ContinueQueue()
        self.permissions = crabd.PermissionBroker()
        self.hooks = crabd.HookTracker()
        holder = {}
        self.otlp = crabd.OtlpReceiver(
            on_event=lambda sid, text: holder["b"].note_session_event(sid, text))
        self.builder = crabd.StateBuilder(
            crabd.TranscriptStore(projects), self.hooks, StubLimits(), time.time(),
            crabd.UserConfig(self.config_path), statusline=self.statusline,
            otlp=self.otlp, continues=self.continues, permissions=self.permissions)
        holder["b"] = self.builder
        # v0.29.0: a pairing code, as a real crabd carries one.
        self.TOKEN = "K7QXM2PDAB"
        self.builder.panel_token = crabd.PanelToken(None, self.TOKEN)
        crabd.Handler.builder = self.builder
        # Proven-reachable port + one reused connection - see _httpkeepalive.
        self.server, self.thread, self.port, self.client = start_test_server(
            lambda: crabd.CrabdServer(("127.0.0.1", 0), crabd.Handler))
        self.assertNotEqual(self.port, crabd.DEFAULT_PORT)
        self.addCleanup(self._stop)
        self.addCleanup(self.client.close)
        # One session on a served row - a hook alone is enough, the builder folds hook
        # rows into per_session exactly so a session with no parsed transcript still
        # renders.
        self.hooks.record({"session_id": self.SID, "hook_event_name": "SessionStart",
                           "cwd": "C:\\Dev\\sidecrab"})
        self.rebuild()
        # Registered last so it runs FIRST (cleanups are LIFO). A permission left parked
        # in broker.wait() holds a handler thread and its socket for the whole poll, on
        # a server the next test has already replaced - see the note in test_crabd.py's
        # PermissionEndpointTests for why that is what made this suite nondeterministic.
        self._fired = []
        self.addCleanup(self.release_parked)

    def release_parked(self):
        with self.permissions._lock:
            entries = list(self.permissions._pending.values())
            self.permissions._pending.clear()
        for entry in entries:
            entry["event"].set()            # decision stays None = pass-through
        for thread in self._fired:
            thread.join(timeout=10)

    def fire(self, target):
        """A request that is designed to BLOCK, run off the main thread and REGISTERED
        so cleanup joins it rather than abandoning it."""
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self._fired.append(thread)
        return thread

    def _stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    # -- helpers

    def rebuild(self):
        state = self.builder.build()
        with self.builder._lock:
            self.builder._state = state
        return state

    def post(self, path, body, timeout=15):
        """One reused connection per thread (KeepAliveClient). A fresh TCP connection
        per request is what made this suite nondeterministic on Windows - the
        measurement and the mechanism are in _httpkeepalive."""
        raw = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        reply = self.client.post(path, raw, timeout=timeout)
        return reply.status, reply.body

    def get(self, path, timeout=15):
        reply = self.client.get(path, timeout=timeout)
        return reply.status, json.loads(reply.body.decode("utf-8"))

    def health(self):
        return self.get("/v1/health")[1]

    def row(self, session_id=None):
        target = session_id or self.SID
        return next((r for r in self.rebuild()["sessions"] if r["id"] == target), None)

    def enable_panel(self):
        self.config_path.write_text(json.dumps({"panelApprovals": {"enabled": True}}),
                                    encoding="utf-8")
        self.builder.config = crabd.UserConfig(self.config_path)

    def statusline_doc(self, **overrides):
        """The shipped 2.1.246 document shape, with the fields crabd reads past kept in
        deliberately - a fixture that carries only what the code wants is exactly the
        fixture that let the resets_at crash ship."""
        doc = {
            "hook_event_name": "Status", "session_id": self.SID,
            "transcript_path": "C:\\Users\\x\\.claude\\projects\\p\\s.jsonl",
            "cwd": "C:\\Dev\\sidecrab", "version": "2.1.246",
            "model": {"id": "claude-fable-5", "display_name": "Fable"},
            "workspace": {"current_dir": "C:\\Dev\\sidecrab",
                          "project_dir": "C:\\Dev\\sidecrab", "added_dirs": []},
            "output_style": {"name": "default"},
            "cost": {"total_cost_usd": 1.25, "total_duration_ms": 1000,
                     "total_api_duration_ms": 400, "total_lines_added": 3,
                     "total_lines_removed": 1},
            "exceeds_200k_tokens": False,
        }
        doc.update(overrides)
        return doc


# ---------------------------------------------------------------- 1. the statusline

class StatusLineLiveFireTests(LiveFireServed):

    def test_a_document_with_no_rate_limits_is_normal_not_an_error(self):
        """The API-key / Bedrock / Vertex case, and every session before its first API
        response. Absence must fall back to OAuth, never render as zeros - a gauge
        sitting at 0% on a session that has burned half its window is worse than an
        em-dash, because the operator believes it."""
        status, body = self.post("/v1/statusline", self.statusline_doc(
            context_window={"total_input_tokens": 88000,
                            "current_usage": {"input_tokens": 12}}))
        self.assertEqual((status, body), (204, b""))
        # /v1/statusline answers before it parses - the 204 is not the ingest.
        settle(lambda: self.statusline.documents, what="the statusline document")
        state = self.rebuild()
        self.assertEqual(state["limits"]["source"], "oauth")
        self.assertEqual(state["limits"]["fiveHour"]["utilization"], 0.42)
        # The document was still USED - the context half of it landed.
        self.assertEqual(self.row()["contextTokens"], 88000)
        self.assertEqual(self.row()["contextSource"], "statusline")
        self.assertEqual(self.health()["statuslineSeen"], 1)

    def test_a_document_of_keys_crabd_has_never_heard_of_is_read_past(self):
        status, _ = self.post("/v1/statusline", self.statusline_doc(
            some_future_block={"nested": [1, 2, {"deep": True}]},
            rate_limits={"five_hour": {"used_percentage": 61.0,
                                       "resets_at": time.time() + 3600,
                                       "unknown_field": "ignored"},
                         "seven_day_opus": {"used_percentage": 12.0},
                         "brand_new_window": {"used_percentage": 99.0}}))
        self.assertEqual(status, 204)
        settle(lambda: self.statusline.documents, what="the statusline document")
        limits = self.rebuild()["limits"]
        self.assertEqual(limits["source"], "statusline")
        self.assertEqual(limits["fiveHour"]["utilization"], 0.61)
        # Only the seven_day_ SUFFIXED siblings become extras; brand_new_window is not
        # a weekly gauge and must not be labelled as one.
        self.assertEqual([w["label"] for w in limits["extra"]], ["opus weekly"])

    def test_an_out_of_range_resets_at_is_dropped_and_never_reaches_the_socket(self):
        """THE v0.14.0 regression. Reproduced against a live crabd on 2026-08-26: this
        exact document put an OverflowError traceback on the connection after the 204.
        The window is still served - a reset time crabd cannot represent does not make
        the utilization reading wrong - it simply carries no resetsAt."""
        noise = io.StringIO()
        original, sys.stderr = sys.stderr, noise
        try:
            status, _ = self.post("/v1/statusline", self.statusline_doc(
                rate_limits={"five_hour": {"used_percentage": 61.0, "resets_at": 1e30},
                             "seven_day": {"used_percentage": 5.0,
                                           "resets_at": -1e30}}))
            # The handler finishes AFTER the 204, so wait for it rather than sleeping -
            # a sleep that is too short turns this regression test green by accident.
            settle(lambda: self.statusline.documents, what="the statusline document")
        finally:
            sys.stderr = original
        self.assertEqual(status, 204)
        self.assertNotIn("Traceback", noise.getvalue())
        limits = self.rebuild()["limits"]
        self.assertEqual(limits["source"], "statusline")
        self.assertEqual(limits["fiveHour"]["utilization"], 0.61)
        self.assertIsNone(limits["fiveHour"]["resetsAt"])
        # And the daemon is still answering.
        self.assertTrue(self.health()["ok"])

    def test_a_document_larger_than_the_statusline_cap_is_dropped_and_still_204(self):
        blob = json.dumps(self.statusline_doc(
            rate_limits={"five_hour": {"used_percentage": 61.0}},
            padding="x" * (crabd.STATUSLINE_MAX_BODY + 4096))).encode()
        self.assertGreater(len(blob), crabd.STATUSLINE_MAX_BODY)
        self.assertEqual(self.post("/v1/statusline", blob)[0], 204)
        # An over-cap body is dropped before ingest, so there is no positive signal to
        # wait for - a following good document is the barrier that proves the oversized
        # one was already handled and discarded.
        self.post("/v1/statusline", {"session_id": self.SID})
        settle(lambda: self.statusline.documents, what="the following document")
        self.assertEqual(self.rebuild()["limits"]["source"], "oauth")
        self.assertTrue(self.health()["ok"])

    def test_every_shape_a_producer_could_send_is_204(self):
        docs = [b"not json", b"", b"[]", b"null", b'"text"', b"\x00\x01\x02",
                json.dumps({"rate_limits": "wrong type"}).encode(),
                json.dumps({"rate_limits": {"five_hour": "wrong type"}}).encode(),
                json.dumps({"rate_limits": {"five_hour": {"used_percentage": True}}}).encode(),
                json.dumps({"session_id": {"nested": 1}}).encode(),
                json.dumps({"session_id": "s", "context_window": []}).encode(),
                json.dumps({"context_window": {"total_input_tokens": "lots"}}).encode()]
        for raw in docs:
            self.assertEqual(self.post("/v1/statusline", raw)[0], 204, raw[:40])
        self.assertTrue(self.health()["ok"])


# ------------------------------------------------------------------ 2. the Stop hook

class StopHookLiveFireTests(LiveFireServed):

    def stop_body(self, session_id=None, **extra):
        body = {"session_id": session_id or self.SID, "hook_event_name": "Stop",
                "cwd": "C:\\Dev\\sidecrab", "transcript_path": "C:\\x\\s.jsonl",
                "stop_hook_active": False}
        body.update(extra)
        return body

    def test_a_stop_for_a_session_crabd_has_never_seen_passes_through(self):
        """Not an edge case - it is EVERY session that started before this crabd did,
        and every session on a machine where the hooks block was only just wired."""
        status, body = self.post("/v1/hook/stop", self.stop_body(session_id=self.GHOST))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})
        # The state machine still learned about it: skip the record and the session
        # would sit on `working` forever the moment it does become visible. (The Stop
        # endpoint records BEFORE it answers, so this one needs no barrier.)
        self.assertEqual(self.hooks.snapshot()[self.GHOST]["state"], "done")

    def test_a_stop_naming_a_session_id_of_the_wrong_shape_passes_through(self):
        for bad in ({"session_id": 12}, {"session_id": ""}, {"session_id": None},
                    {"session_id": {"a": 1}}, {"session_id": "x" * 5000}):
            body = self.stop_body(**bad)
            body.pop("session_id", None)
            body.update(bad)
            status, answer = self.post("/v1/hook/stop", body)
            self.assertEqual((status, json.loads(answer)), (200, {}), bad)

    def test_every_malformed_stop_body_still_gets_an_answer(self):
        """A Stop hook that gets no answer is a session that hangs waiting for one."""
        for raw in (b"not json", b"", b"[]", b"null", b"\x00\x01",
                    json.dumps({"hook_event_name": "Stop"}).encode(),
                    json.dumps([{"session_id": "s"}]).encode()):
            status, body = self.post("/v1/hook/stop", raw)
            self.assertEqual(status, 200, raw[:30])
            self.assertEqual(json.loads(body), {}, raw[:30])

    def test_an_oversized_stop_body_still_answers(self):
        blob = json.dumps(self.stop_body(padding="x" * (crabd.MAX_BODY_BYTES // 2)))
        status, body = self.post("/v1/hook/stop", blob.encode())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})

    def test_a_queued_continue_still_drains_for_a_session_it_was_queued_against(self):
        self.assertEqual(self.post("/v1/action", {
            "sessionId": self.SID, "action": "queue-continue",
            "prompt": "Run the tests"})[0], 204)
        status, body = self.post("/v1/hook/stop", self.stop_body())
        self.assertEqual((status, json.loads(body)),
                         (200, crabd.stop_continue_body("Run the tests")))


# ------------------------------------------------------- 3. the permission long poll

class PermissionLiveFireTests(LiveFireServed):

    def setUp(self):
        super().setUp()
        self._poll = crabd.PERMISSION_POLL_SEC
        self.addCleanup(lambda: setattr(crabd, "PERMISSION_POLL_SEC", self._poll))

    def permission_body(self, session_id=None, **extra):
        body = {"session_id": session_id or self.SID,
                "hook_event_name": "PermissionRequest", "tool_name": "Bash",
                "tool_input": {"command": "git push --force"},
                "cwd": "C:\\Dev\\sidecrab", "permission_mode": "default",
                "tool_use_id": "toolu_live"}
        body.update(extra)
        return body

    def test_a_request_for_a_session_not_on_a_served_row_passes_through_at_once(self):
        """The same scoping rule ack, queue-continue and the OTLP event route use. The
        widget renders pendingPermission off the served rows, so holding a request for a
        session that has no row parks a thread and burns one of PERMISSION_MAX_PENDING
        for 55 s while the operator waits on a terminal dialog that has not appeared."""
        self.enable_panel()
        crabd.PERMISSION_POLL_SEC = 30
        started = time.time()
        status, body = self.post("/v1/hook/permission",
                                 self.permission_body(session_id=self.GHOST))
        self.assertEqual((status, json.loads(body)), (200, {}))
        # Relative to the poll it proves we did not wait for, not a bare constant: the
        # property is "did not park on the long poll", and a fixed 2 s would also fail
        # for reasons that have nothing to do with parking.
        self.assertLess(time.time() - started, crabd.PERMISSION_POLL_SEC / 3)
        self.assertEqual(self.permissions.count(), 0)
        # And it did not conjure a row or a history line for a session nothing serves.
        self.assertNotIn(self.GHOST, self.hooks.snapshot())

    def test_a_request_for_a_served_session_is_still_held(self):
        """The gate above must not have turned the feature off."""
        self.enable_panel()
        crabd.PERMISSION_POLL_SEC = 5
        out = []
        thread = self.fire(
            lambda: out.append(self.post("/v1/hook/permission",
                                         self.permission_body(), timeout=30)))
        deadline = time.time() + 5
        while time.time() < deadline and self.permissions.pending(self.SID) is None:
            time.sleep(0.01)
        self.assertIsNotNone(self.permissions.pending(self.SID))
        self.assertEqual(self.post("/v1/action", decide_body(self, "deny"))[0], 204)
        thread.join(timeout=10)
        self.assertEqual(json.loads(out[0][1]), {
            "hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                   "decision": {"behavior": "deny",
                                                "message": crabd.PERMISSION_DENY_MESSAGE}}})

    def test_no_malformed_body_can_produce_an_allow(self):
        """The one property worth reading the code for. Every early exit in the handler
        lands on the same empty object, and an EXCEPTION inside it lands there too."""
        self.enable_panel()
        crabd.PERMISSION_POLL_SEC = 0.2
        bodies = [b"not json", b"", b"[]", b"null", b"\x00",
                  json.dumps({"hook_event_name": "PermissionRequest"}).encode(),
                  json.dumps(self.permission_body(session_id="x" * 5000)).encode(),
                  json.dumps(self.permission_body(tool_name={"a": 1})).encode(),
                  json.dumps(self.permission_body(tool_input="not a dict")).encode(),
                  json.dumps(self.permission_body(session_id=self.GHOST)).encode()]
        for raw in bodies:
            status, body = self.post("/v1/hook/permission", raw)
            self.assertEqual(status, 200, raw[:40])
            self.assertEqual(json.loads(body), {}, raw[:40])
            self.assertNotIn(b"allow", body)

    def test_a_ghost_flood_cannot_saturate_the_broker(self):
        """Before the serving gate, 8 requests naming sessions nothing serves would fill
        PERMISSION_MAX_PENDING and pass through every REAL one behind them."""
        self.enable_panel()
        crabd.PERMISSION_POLL_SEC = 30
        for i in range(crabd.PERMISSION_MAX_PENDING + 4):
            status, body = self.post("/v1/hook/permission",
                                     self.permission_body(session_id=f"ghost-{i}"))
            self.assertEqual((status, json.loads(body)), (200, {}))
        self.assertEqual(self.permissions.count(), 0)


# ----------------------------------------------------------------- 4. concurrency

class ConcurrentControlSurfaceTests(LiveFireServed):
    """A stop and a permission arriving for the SAME session at the same time, plus a
    status line document underneath both - which is exactly what a real session does at
    the end of a turn. Three endpoints, three locks, one session row."""

    def setUp(self):
        super().setUp()
        self._poll = crabd.PERMISSION_POLL_SEC
        crabd.PERMISSION_POLL_SEC = 0.3
        self.addCleanup(lambda: setattr(crabd, "PERMISSION_POLL_SEC", self._poll))

    def test_stop_and_permission_for_one_session_both_answer(self):
        self.enable_panel()
        results, errors = [], []

        def fire(path, body):
            try:
                results.append((path,) + self.post(path, body, timeout=25))
            except Exception as exc:                      # noqa: BLE001
                errors.append((path, repr(exc)))

        bodies = [
            ("/v1/hook/stop",
             {"session_id": self.SID, "hook_event_name": "Stop",
              "cwd": "C:\\Dev\\sidecrab"}),
            ("/v1/hook/permission",
             {"session_id": self.SID, "hook_event_name": "PermissionRequest",
              "tool_name": "Bash", "tool_input": {"command": "ls"}}),
            ("/v1/statusline",
             self.statusline_doc(rate_limits={"five_hour": {"used_percentage": 61.0}})),
        ]
        threads = [self.fire(lambda p=path, b=body: fire(p, b))
                   for _ in range(6) for path, body in bodies]
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), len(threads))
        for path, status, _ in results:
            self.assertIn(status, (200, 204), path)
        # Nothing was left parked, and the row is intact.
        self.assertEqual(self.permissions.count(), 0)
        self.assertEqual(self.row()["state"], "done")
        self.assertTrue(self.health()["ok"])

    def test_a_continue_queued_during_a_permission_hold_is_still_delivered_once(self):
        """The queue and the broker are different locks over the same session id. A
        continue queued while a permission is held must drain exactly once."""
        self.enable_panel()
        self.post("/v1/action", {"sessionId": self.SID, "action": "queue-continue",
                                 "prompt": "Continue"})
        permission = self.fire(
            lambda: self.post("/v1/hook/permission", {
                "session_id": self.SID, "hook_event_name": "PermissionRequest",
                "tool_name": "Bash", "tool_input": {"command": "ls"}}, timeout=25))
        answers = [json.loads(self.post("/v1/hook/stop", {
            "session_id": self.SID, "hook_event_name": "Stop"})[1]) for _ in range(3)]
        permission.join(timeout=25)
        self.assertEqual(answers[0], crabd.stop_continue_body("Continue"))
        self.assertEqual(answers[1:], [{}, {}])


# -------------------------------------- 4b. SEC-1 Origin gate + SEC-3 continue gate

class Sec1OriginGateLiveFireTests(LiveFireServed):
    """QA-Audit 2026-08-27 SEC-1. A mutating endpoint refuses a cross-site web page and
    keeps every legitimate client working: the widget's opaque QtWebEngine origin
    (Origin: null), curl-fed ingest hooks and the CLI's own HTTP hooks (no Origin), and
    local tools. Proven on /v1/action queue-continue, the live control path SEC-3 flags.

    The policy and its measured basis: the widget runs from an iCUE-served file/qrc page
    inside QtWebEngine (Chromium), whose cross-origin fetch serializes its Origin to
    exactly "null" (widget/scripts/sidecrab.js sets no Origin - the browser does, and an
    opaque origin has no other serialization). So the gate fails OPEN for absent/null and
    only refuses a PRESENT http(s) Origin - a real visited page. See
    docs/findings/audit-security.md SEC-1."""

    def qc(self, headers=None):
        body = json.dumps({"sessionId": self.SID, "action": "queue-continue",
                           "prompt": "Continue"}).encode()
        return self.client.post("/v1/action", body, headers=headers or {})

    def test_a_cross_site_https_page_is_refused_403(self):
        """Proof (a): Origin: https://evil.example on queue-continue -> 403, and the side
        effect did NOT fire (nothing queued)."""
        reply = self.qc({"Origin": "https://evil.example"})
        self.assertEqual(reply.status, 403)
        self.assertEqual(json.loads(reply.body), {"error": "cross-site request refused"})
        self.assertIsNone(self.row()["queuedContinue"])

    def test_a_cross_site_http_page_is_refused_too(self):
        self.assertEqual(self.qc({"Origin": "http://attacker.local:8080"}).status, 403)

    def test_no_origin_the_hook_and_local_tool_case_works(self):
        """Proof (b): a POST with NO Origin is allowed and queues, and carries no ACAO:*
        (the header that invites the cross-origin read is simply absent)."""
        reply = self.qc()
        self.assertEqual(reply.status, 204)
        self.assertIsNone(reply.headers.get("Access-Control-Allow-Origin"))
        self.assertEqual(self.row()["queuedContinue"]["prompt"], "Continue")

    def test_a_null_origin_the_widget_case_works_and_is_reflected(self):
        """Proof (c): the widget's Origin: null is allowed and queues, and the reply
        reflects `null` (never `*`) so the widget's cors-mode fetch can read the status
        it optimistically rolled forward - an unreadable reply rolls the tap back."""
        reply = self.qc({"Origin": "null"})
        self.assertEqual(reply.status, 204)
        self.assertEqual(reply.headers.get("Access-Control-Allow-Origin"), "null")
        self.assertNotEqual(reply.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(self.row()["queuedContinue"]["prompt"], "Continue")

    def test_options_on_a_mutating_path_never_advertises_wildcard(self):
        """Proof (d): OPTIONS no longer answers ANY preflight with ACAO:*. A web page's
        preflight gets no ACAO; the widget's null origin is reflected so its
        application/json preflight still passes. v0.16.0 (SEC-4) extends the same answer
        to the READ paths, which used to be preflighted with the wildcard."""
        evil = self.client.request("OPTIONS", "/v1/action",
                                   headers={"Origin": "https://evil.example"})
        self.assertIsNone(evil.headers.get("Access-Control-Allow-Origin"))
        widget = self.client.request("OPTIONS", "/v1/action", headers={"Origin": "null"})
        self.assertEqual(widget.headers.get("Access-Control-Allow-Origin"), "null")
        self.assertEqual(widget.headers.get("Access-Control-Allow-Methods"),
                         "GET, POST, OPTIONS")
        # v0.31.0: `null` keeps its ACAO (the iCUE build's reads) and is the one allowed
        # origin whose preflight may NOT unlock the panel header - see
        # test_crabd_panel.PanelPreflightTests for the whole rule.
        self.assertEqual(widget.headers.get("Access-Control-Allow-Headers"),
                         "Content-Type")
        state = self.client.request("OPTIONS", "/v1/state", headers={"Origin": "null"})
        self.assertEqual(state.headers.get("Access-Control-Allow-Origin"), "null")
        evil_read = self.client.request("OPTIONS", "/v1/state",
                                        headers={"Origin": "https://evil.example"})
        self.assertIsNone(evil_read.headers.get("Access-Control-Allow-Origin"))

    def test_the_config_write_surface_is_gated_the_same_way(self):
        reply = self.client.post("/v1/config", json.dumps({"quietHours": None}).encode(),
                                 headers={"Origin": "https://evil.example"})
        self.assertEqual(reply.status, 403)

    def test_a_hook_ingest_from_a_web_page_is_refused(self):
        """The fire-and-forget ingest hooks are mutating too - a visited page must not
        drive /v1/hook. curl (the real ingest) sends no Origin and is unaffected."""
        reply = self.client.post("/v1/hook", json.dumps({
            "session_id": self.SID, "hook_event_name": "Stop"}).encode(),
            headers={"Origin": "https://evil.example"})
        self.assertEqual(reply.status, 403)


class Sec3ContinueGateLiveFireTests(LiveFireServed):
    """QA-Audit 2026-08-27 SEC-3. queue-continue gains the enable gate decide (via
    panelApprovals) and reply (via allowReply) already have. DEFAULT ON: tap-to-continue
    shipped always-on in v0.12.0, so a default-OFF gate would silently 400 the widget's
    Continue / Run the tests / Commit + push buttons on every existing install. Its real
    protection is the whitelist + the SEC-1 Origin gate; the flag lets an operator turn
    the feature off entirely."""

    def qc(self):
        body = json.dumps({"sessionId": self.SID, "action": "queue-continue",
                           "prompt": "Continue"}).encode()
        return self.client.post("/v1/action", body)

    def test_default_is_on_no_config_needed(self):
        self.assertTrue(self.builder.config.allow_continue(time.time()))
        self.assertEqual(self.qc().status, 204)

    def test_an_explicit_false_disables_it_with_403(self):
        self.config_path.write_text(json.dumps({"allowContinue": False}),
                                    encoding="utf-8")
        self.builder.config = crabd.UserConfig(self.config_path)
        reply = self.qc()
        self.assertEqual(reply.status, 403)
        self.assertEqual(json.loads(reply.body),
                         {"error": "tap-to-continue is disabled"})
        self.assertIsNone(self.row()["queuedContinue"])

    def test_a_non_bool_value_stays_on(self):
        """Default-ON gate: only a literal boolean false disables. A config typo must not
        silently kill a working feature - the whitelist already bounds what it accepts."""
        self.config_path.write_text(json.dumps({"allowContinue": "no"}),
                                    encoding="utf-8")
        self.builder.config = crabd.UserConfig(self.config_path)
        self.assertEqual(self.qc().status, 204)

    def test_allowcontinue_is_not_writable_over_http(self):
        """Like allowReply / panelApprovals: naming it in /v1/config is a 400, so nothing
        over the unauthenticated API can toggle the gate."""
        reply = self.client.post("/v1/config",
                                 json.dumps({"allowContinue": False}).encode())
        self.assertEqual(reply.status, 400)


# ----------------------------------------------- 5. a body larger than expected

class OversizedBodyOverTheWireTests(LiveFireServed):

    def raw_request(self, path, headers_extra, payload, read_timeout=8.0):
        """A hand-rolled request so the Content-Length header can LIE, which is the
        shape that hung a live crabd on 2026-08-26 - urllib will not send one."""
        conn = socket.create_connection(("127.0.0.1", self.port), timeout=read_timeout)
        try:
            head = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                    f"Content-Type: application/json\r\n"
                    f"{crabd.PANEL_HEADER}: 1\r\n{headers_extra}\r\n")
            conn.sendall(head.encode() + payload)
            conn.settimeout(read_timeout)
            try:
                return conn.recv(4096)
            except (TimeoutError, OSError):
                return b""
        finally:
            conn.close()

    def test_a_content_length_that_lies_cannot_park_the_daemon(self):
        """900 MB announced, seven bytes sent, then silence. The old code tried to
        buffer 900 MB and blocked with no answer on the wire; crabd must give up and
        stay healthy for everybody else."""
        original = crabd.Handler.timeout
        crabd.Handler.timeout = 1.0
        self.addCleanup(lambda: setattr(crabd.Handler, "timeout", original))
        started = time.time()
        self.raw_request("/v1/hook", "Content-Length: 900000000\r\n", b'{"a":1}')
        self.assertLess(time.time() - started, 8.0)
        # The one that matters: the daemon is still serving everything else.
        self.assertTrue(self.health()["ok"])
        self.assertEqual(self.post("/v1/hook/stop", {
            "session_id": self.SID, "hook_event_name": "Stop"})[0], 200)

    def test_an_honestly_oversized_hook_body_is_answered_and_dropped(self):
        blob = json.dumps({"session_id": self.SID, "hook_event_name": "Stop",
                           "pad": "x" * (crabd.MAX_BODY_BYTES + 2048)}).encode()
        self.assertGreater(len(blob), crabd.MAX_BODY_BYTES)
        self.assertEqual(self.post("/v1/hook", blob)[0], 204)
        # Truncated at the cap, so it is not valid JSON any more and is dropped - but
        # the endpoint answered and the daemon is untouched.
        self.assertTrue(self.health()["ok"])

    def test_every_endpoint_survives_an_oversized_body(self):
        blob = b'{"pad":"' + b"x" * (crabd.MAX_BODY_BYTES + 1024) + b'"}'
        for path, expected in (("/v1/hook", 204), ("/v1/statusline", 204),
                               ("/v1/metrics", 204), ("/v1/logs", 204),
                               ("/v1/hook/stop", 200), ("/v1/hook/permission", 200)):
            self.assertEqual(self.post(path, blob, timeout=20)[0], expected, path)
        self.assertTrue(self.health()["ok"])


class UnknownPathFramingTests(LiveFireServed):
    """v0.17.0: do_POST drains the body on the 404 branch too.

    The 403 cross-origin branch got its drain on 2026-08-27; the unknown-path branch did
    not, so a POST-with-body to a path crabd does not serve left those bytes in the
    socket. The connection's NEXT request line is then parsed out of the leftover body -
    the following request is answered as garbage, or the connection dies under it.

    ASSERTED ON A BARE CONNECTION, deliberately. The suite's KeepAliveClient re-opens a
    dropped connection and retries the request once, which is exactly the behaviour that
    masked this: a desynchronised connection looks like a transient and the test passes.
    Here the connect is the only thing retried (the harness's rule - a SYN with no
    SYN-ACK never reaches crabd), and both requests then ride the SAME socket with no
    second chance, so the second response IS the proof the first body left the stream.
    """

    #: What a real POST carries. Without the panel header the header gate answers
    #: first and this class would be asserting the WRONG refusal's framing.
    HEAD = {"Content-Type": "application/json", crabd.PANEL_HEADER: "1"}

    def bare_connection(self):
        conn = KeepAliveClient(self.port)._connect()
        self.addCleanup(conn.close)
        return conn

    def test_a_post_with_a_body_to_an_unknown_path_does_not_desync_the_connection(self):
        conn = self.bare_connection()
        conn.request("POST", "/v1/not-a-real-path",
                     body=json.dumps({"pad": "x" * 4096}).encode(),
                     headers=self.HEAD)
        first = conn.getresponse()
        self.assertEqual(first.status, 404)
        self.assertEqual(json.loads(first.read()), {"error": "not found"})
        self.assertFalse(first.will_close,
                         "a 404 must not have to close the connection to stay in sync")
        # The same socket, no reconnect: this only parses if the body was consumed.
        conn.request("GET", "/v1/health")
        second = conn.getresponse()
        self.assertEqual(second.status, 200)
        self.assertTrue(json.loads(second.read())["ok"])

    def test_several_unknown_posts_in_a_row_stay_framed(self):
        """One leftover body desyncs everything after it, so the interesting case is a
        RUN of them - a mis-pointed exporter or a probe does not send just one."""
        conn = self.bare_connection()
        for i in range(5):
            conn.request("POST", f"/v1/nope/{i}", body=b'{"a":' + str(i).encode() + b'}',
                         headers=self.HEAD)
            response = conn.getresponse()
            self.assertEqual(response.status, 404, i)
            response.read()
        conn.request("GET", "/v1/health")
        response = conn.getresponse()
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.read())["ok"])

    def test_an_unknown_post_is_drained_not_ingested(self):
        """Drained, not INGESTED: the body of a path crabd does not serve must not touch
        any store on its way to the bin."""
        before = (self.hooks.count, self.statusline.documents, self.otlp.documents)
        self.assertEqual(self.post("/v1/statusline/nope", {"session_id": self.SID,
                                                           "rate_limits": {}})[0], 404)
        self.assertEqual((self.hooks.count, self.statusline.documents,
                          self.otlp.documents), before)


# ------------------------------------------------------ 6. queuedContinue on the row

class QueuedContinueOnTheRowTests(LiveFireServed):
    """sessions[].queuedContinue - v0.14.0, additive, schema stays 5.

    The queue was observable in aggregate and nowhere per-card, so a queued prompt was
    invisible until the operator re-opened the sheet that queued it.
    """

    def test_the_key_is_present_and_null_with_nothing_queued(self):
        """Present-and-null like pendingPermission: the KEY is the widget's feature
        detection, and a missing key and an empty one would mean the same thing to it."""
        row = self.row()
        self.assertIn("queuedContinue", row)
        self.assertIsNone(row["queuedContinue"])

    def test_a_queued_prompt_reaches_the_card(self):
        self.assertEqual(self.post("/v1/action", {
            "sessionId": self.SID, "action": "queue-continue",
            "prompt": "Run the tests and report the results."})[0], 204)
        queued = self.row()["queuedContinue"]
        self.assertEqual(sorted(queued), ["prompt", "queuedAt"])
        self.assertEqual(queued["prompt"], "Run the tests and report the results.")
        self.assertRegex(queued["queuedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_it_clears_once_the_stop_hook_has_delivered_it(self):
        """CRB-F5 (v0.16.0) moved the drain to AFTER the answer is on the socket, so the
        client can be back from its request before the handler has consumed the queue.
        The barrier is honest about that: the clearing is a side effect the test waits
        for, exactly as `settle` exists for elsewhere. What is asserted - that a
        delivered prompt leaves the card - is unchanged."""
        self.post("/v1/action", {"sessionId": self.SID, "action": "queue-continue",
                                 "prompt": "Continue"})
        self.assertIsNotNone(self.row()["queuedContinue"])
        self.post("/v1/hook/stop", {"session_id": self.SID, "hook_event_name": "Stop",
                                    "cwd": "C:\\Dev\\sidecrab"})
        settle(lambda: self.row()["queuedContinue"] is None,
               what="the delivered continue clearing off the card")

    def test_it_clears_at_the_ttl_even_if_the_expiry_sweep_has_not_run(self):
        """Re-derived from the stored `at` rather than trusting the sweep: the card must
        stop advertising a prompt the Stop hook would no longer deliver."""
        self.continues.queue(self.SID, "Continue",
                             time.time() - crabd.CONTINUE_TTL_SEC - 1)
        self.assertIsNone(self.row()["queuedContinue"])
        self.assertIsNone(self.continues.drain(self.SID, time.time()))

    def test_newest_wins_on_the_card_too(self):
        for prompt in ("Continue", "Commit the changes and push."):
            self.post("/v1/action", {"sessionId": self.SID, "action": "queue-continue",
                                     "prompt": prompt})
        self.assertEqual(self.row()["queuedContinue"]["prompt"],
                         "Commit the changes and push.")

    def test_a_builder_with_no_queue_serves_null_not_a_missing_key(self):
        self.builder.continues = None
        row = self.row()
        self.assertIn("queuedContinue", row)
        self.assertIsNone(row["queuedContinue"])

    def test_the_schema_number_did_not_move(self):
        """Additive means additive: the widget's acceptance test is 1 <= schema <= 5 and
        an unknown KEY is ignored, never rejected. A bump here costs a console-bound
        .icuewidget re-import at the operator's desk."""
        self.assertEqual(self.rebuild()["schema"], 5)
        self.assertEqual(crabd.SCHEMA_BREAKING, 5)


# ----------------------------------------------------------------- 7. /v1/health

class HealthEndpointTests(LiveFireServed):
    """GET /v1/health - is crabd up, AND are the feeds arriving.

    The observability that would have caught a feed standing down: "crabd answers 200"
    and "crabd is being fed" are different questions and only the first was askable.
    """

    def test_the_two_original_keys_are_untouched(self):
        body = self.health()
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], crabd.VERSION)
        self.assertEqual(crabd.VERSION, "0.31.0")

    def test_the_shape_is_the_full_counter_set(self):
        self.assertEqual(sorted(self.health()),
                         ["hooksSeen", "lastStatuslineAgeSec", "ok", "originsSeen",
                          "otlpSeen", "panel", "panelToken", "statuslineSeen",
                          "uptimeSec", "version"])

    def test_a_statusline_that_has_never_posted_is_null_not_zero(self):
        """The sharp one. Null means the status line has NEVER posted, which is a
        misconfiguration; a NUMBER means it posted and then went quiet, which is an idle
        operator. Zero for both would make them indistinguishable."""
        body = self.health()
        self.assertIsNone(body["lastStatuslineAgeSec"])
        self.assertEqual(body["statuslineSeen"], 0)

    def test_the_counters_move_as_the_feeds_arrive(self):
        before = self.health()
        self.post("/v1/statusline", self.statusline_doc())
        self.post("/v1/statusline", self.statusline_doc())
        self.post("/v1/hook", {"session_id": self.SID,
                               "hook_event_name": "UserPromptSubmit"})
        self.post("/v1/metrics", {"resourceMetrics": []})
        self.post("/v1/logs", {"resourceLogs": []})
        # Every one of those endpoints answers before it parses.
        settle(lambda: self.statusline.documents >= 2 and self.otlp.documents >= 2,
               what="the statusline and otlp documents")
        settle(lambda: self.hooks.count >= before["hooksSeen"] + 1, what="the hook")
        after = self.health()
        self.assertEqual(after["statuslineSeen"], 2)
        self.assertEqual(after["hooksSeen"], before["hooksSeen"] + 1)
        self.assertEqual(after["otlpSeen"], 2)
        self.assertIsInstance(after["lastStatuslineAgeSec"], int)
        self.assertLess(after["lastStatuslineAgeSec"], 30)

    def test_a_document_carrying_nothing_crabd_wants_still_counts_as_arriving(self):
        """statuslineSeen answers "is the command still chained", not "did we like the
        payload". An API-key session posts documents forever that carry no windows."""
        self.post("/v1/statusline", {"session_id": self.SID})
        settle(lambda: self.statusline.documents, what="the statusline document")
        self.assertEqual(self.health()["statuslineSeen"], 1)
        self.assertIsNotNone(self.health()["lastStatuslineAgeSec"])

    def test_a_malformed_body_does_not_count_as_a_feed(self):
        self.post("/v1/statusline", b"not json")
        # A good document behind it is the barrier: once THAT is counted, the malformed
        # one has certainly been through the handler and was not counted.
        self.post("/v1/statusline", {"session_id": self.SID})
        settle(lambda: self.statusline.documents, what="the following document")
        self.assertEqual(self.health()["statuslineSeen"], 1)

    def test_uptime_is_a_whole_number_of_seconds_and_never_negative(self):
        body = self.health()
        self.assertIsInstance(body["uptimeSec"], int)
        self.assertGreaterEqual(body["uptimeSec"], 0)

    def test_health_answers_on_a_crabd_with_no_readers_wired(self):
        """A unit-test builder, and a crabd running without the v0.12.0 features, both
        report zeros rather than failing the health check."""
        self.builder.statusline = None
        self.builder.otlp = None
        body = self.health()
        self.assertTrue(body["ok"])
        self.assertEqual(body["statuslineSeen"], 0)
        self.assertEqual(body["otlpSeen"], 0)
        self.assertIsNone(body["lastStatuslineAgeSec"])

    def test_health_stays_fast_while_a_permission_is_held(self):
        """Health is what an operator or Test-SideCrab reaches for when something looks
        stuck, so it must not be the thing that is stuck."""
        self.enable_panel()
        original = crabd.PERMISSION_POLL_SEC
        crabd.PERMISSION_POLL_SEC = 3
        self.addCleanup(lambda: setattr(crabd, "PERMISSION_POLL_SEC", original))
        thread = self.fire(
            lambda: self.post("/v1/hook/permission", {
                "session_id": self.SID, "hook_event_name": "PermissionRequest",
                "tool_name": "Bash", "tool_input": {"command": "ls"}}, timeout=25))
        started = time.time()
        for _ in range(5):
            self.assertTrue(self.health()["ok"])
        self.assertLess(time.time() - started, 5.0)
        thread.join(timeout=25)


# ------------------------------------------- 8. SEC-4: the READ endpoints are gated too

class Sec4ReadGateLiveFireTests(LiveFireServed):
    """QA-Audit 2026-08-27 SEC-4 (fixed v0.16.0). The Origin gate SEC-1 put on the
    mutating endpoints now covers the reads.

    What made this worth closing rather than accepting: /v1/state is not a status ping.
    It carries every live session's cwd, its title, the FULL text of the question it is
    waiting on and its pendingPermission - and it carried them under
    `Access-Control-Allow-Origin: *`, which is an explicit instruction to the browser to
    let ANY page the operator visited read the response. A page cannot POST here (SEC-1),
    but it could read everything, from a tab in the background, for as long as it stayed
    open.

    The gate is the SAME predicate, so the widget is unaffected: an opaque QtWebEngine
    origin serializes to exactly "null", which is not a web origin and is allowed. The
    two proofs that matter are both below - evil is refused, and the widget still works.
    """

    EVIL = {"Origin": "https://evil.example"}
    WIDGET = {"Origin": "null"}
    READS = ("/v1/state", "/v1/health")

    def day(self):
        return crabd._local_day(time.time())

    # -- the refusal

    def test_a_visited_web_page_cannot_read_state(self):
        reply = self.client.get("/v1/state", headers=self.EVIL)
        self.assertEqual(reply.status, 403)
        self.assertEqual(json.loads(reply.body), {"error": "cross-site request refused"})
        self.assertIsNone(reply.headers.get("Access-Control-Allow-Origin"))

    def test_the_refusal_leaks_nothing_from_the_document(self):
        """A 403 whose BODY still carried the state would be no fix at all. The refusal
        is the shared constant and nothing else."""
        body = self.client.get("/v1/state", headers=self.EVIL).body
        self.assertEqual(body, crabd.CROSS_SITE_REFUSED)
        for leak in (b"sessions", b"cwd", b"question", b"schema"):
            self.assertNotIn(leak, body, leak)

    def test_every_read_route_is_gated_not_just_state(self):
        """Gating only the interesting one is how the next endpoint ships ungated."""
        for path in self.READS + (f"/v1/history?day={self.day()}", "/v1/nope"):
            reply = self.client.get(path, headers=self.EVIL)
            self.assertEqual(reply.status, 403, path)
            self.assertIsNone(reply.headers.get("Access-Control-Allow-Origin"), path)

    def test_an_http_origin_is_refused_as_well_as_https(self):
        """Loopback is not a pass. `http://127.0.0.1:<not this port>` is every other
        local tool the operator has open - a dev server, a notebook, another daemon's
        UI - and each is a different origin. (v0.31.0 moved the one exception to the
        allowlist: this crabd's OWN bound port, asserted below.)"""
        for origin in ("http://attacker.local:8080", "HTTPS://Evil.Example",
                       f"http://127.0.0.1:{self.port + 1}",
                       f"http://localhost:{self.port + 1}"):
            reply = self.client.get("/v1/state", headers={"Origin": origin})
            self.assertEqual(reply.status, 403, origin)

    def test_this_crabds_own_panel_origin_is_the_one_http_origin_served(self):
        """The other half of the row above, so "refuse http origins" cannot quietly
        become "refuse the panel". Fully covered in test_crabd_panel.py."""
        for origin in (f"http://127.0.0.1:{self.port}",
                       f"http://localhost:{self.port}"):
            reply = self.client.get("/v1/state", headers={"Origin": origin})
            self.assertEqual(reply.status, 200, origin)
            self.assertEqual(reply.headers.get("Access-Control-Allow-Origin"), origin)

    # -- THE WIDGET MUST KEEP WORKING

    def test_the_widget_null_origin_still_reads_state_and_can_use_the_reply(self):
        """The proof that this fix is shippable. The widget's fetch is cors-mode from an
        opaque origin, so it needs a 200 AND an ACAO its own browser will accept - which
        for an opaque origin is the literal string `null`, never the wildcard."""
        reply = self.client.get("/v1/state", headers=self.WIDGET)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.headers.get("Access-Control-Allow-Origin"), "null")
        self.assertEqual(reply.headers.get("Vary"), "Origin")
        self.assertEqual(json.loads(reply.body)["schema"], 5)

    def test_a_widget_style_read_works_on_every_route(self):
        for path in self.READS + (f"/v1/history?day={self.day()}",):
            reply = self.client.get(path, headers=self.WIDGET)
            self.assertEqual(reply.status, 200, path)
            self.assertEqual(reply.headers.get("Access-Control-Allow-Origin"),
                             "null", path)

    def test_a_read_with_no_origin_still_works_and_gets_no_acao(self):
        """curl, Test-SideCrab, Repair-SideCrab and every local tool. A non-browser
        client needs no ACAO and must not be handed one."""
        reply = self.client.get("/v1/state")
        self.assertEqual(reply.status, 200)
        self.assertIsNone(reply.headers.get("Access-Control-Allow-Origin"))
        self.assertEqual(json.loads(reply.body)["schema"], 5)

    def test_a_non_web_scheme_origin_is_allowed_like_null(self):
        """file:// and qrc:// are what a locally-served page reports where the browser
        does not collapse it to `null`. Neither is the visited-page vector."""
        for origin in ("file://", "qrc://icue/widget"):
            reply = self.client.get("/v1/state", headers={"Origin": origin})
            self.assertEqual(reply.status, 200, origin)
            self.assertEqual(reply.headers.get("Access-Control-Allow-Origin"),
                             origin, origin)

    # -- the sweep: no wildcard survives anywhere

    def test_no_response_on_any_route_or_method_carries_a_wildcard(self):
        """The sweep the finding asked for. Every route crabd answers, every method it
        answers on, with no Origin / the widget's / a hostile one - and `*` appears in
        none of them. A single missed emitter re-opens the whole finding."""
        reads = ("/v1/state", "/v1/health", f"/v1/history?day={self.day()}", "/v1/nope")
        # /v1/hook/permission is left out on purpose: it LONG-POLLS, and a sweep is not
        # the place to hold a handler for 55 s. Its CORS is do_POST's, asserted by every
        # other row here.
        writes = ("/v1/action", "/v1/config", "/v1/hook", "/v1/statusline",
                  "/v1/metrics", "/v1/logs", "/v1/hook/stop")
        seen = []
        for headers in ({}, self.WIDGET, self.EVIL):
            for path in reads:
                seen.append(("GET", path,
                             self.client.get(path, headers=dict(headers))))
            for path in reads + writes:
                seen.append(("OPTIONS", path, self.client.request(
                    "OPTIONS", path, headers=dict(headers))))
            for path in writes:
                seen.append(("POST", path, self.client.post(
                    path, b"{}", headers=dict(headers), timeout=20)))
        self.assertEqual(len(seen), 3 * (4 + 11 + 7))
        for method, path, reply in seen:
            self.assertNotEqual(reply.headers.get("Access-Control-Allow-Origin"), "*",
                                (method, path))

    def test_the_module_emits_no_wildcard_acao_default(self):
        """Belt and braces on the class default: a handler path that forgets to set
        _acao must now fail CLOSED (no header) rather than open (`*`)."""
        self.assertIsNone(crabd.Handler._acao)


# ------------------------------------ 9. CRB-F5: a failed Stop send keeps the prompt

class StopSendFailureTests(unittest.TestCase):
    """QA-Audit 2026-08-27 CRB-F5 (fixed v0.16.0): the Stop handler used to DRAIN the
    continue queue and then send, so a send that failed destroyed the operator's queued
    prompt on its way to nobody.

    Exercised without a socket, the way ReadBodyCapTests exercises _read_body: a real
    mid-answer connection failure is not reproducible on demand over loopback, and what
    needs asserting is the handler's ORDER, not the transport. The fault is injected at
    the one place the ordering hangs on - _send raising after the peek.

    v0.17.0: the failure is now CAUGHT rather than left to walk into socketserver's
    handle_error as a traceback (backlog item, 2026-08-27). Every assertion about the
    CRB-F5 guarantee below is unchanged - the prompt survives, no "continue sent" line is
    written, the next Stop delivers it - because catching the exception must not, and
    does not, change any of that. What changed is only that _do_hook_stop returns instead
    of raising, and prints one line saying so.
    """

    SID = "5c5c5c5c-0000-0000-0000-00000000000e"
    PROMPT = "Continue"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        projects = root / "projects"
        projects.mkdir(parents=True)
        self.continues = crabd.ContinueQueue()
        self.hooks = crabd.HookTracker()
        self.builder = crabd.StateBuilder(
            crabd.TranscriptStore(projects), self.hooks, StubLimits(), time.time(),
            crabd.UserConfig(root / "config.json"), continues=self.continues)

    def handler(self, fail_on_continue=False):
        handler = crabd.Handler.__new__(crabd.Handler)
        handler.builder = self.builder
        handler.sent = []

        def _send(code, body=None, ctype="application/json"):
            if fail_on_continue and body and b"additionalContext" in body:
                raise BrokenPipeError("the hook's connection died before the answer landed")
            handler.sent.append((code, body))

        handler._send = _send
        return handler

    def stop(self, handler, session_id=None):
        """Drive one Stop hook, capturing stderr on `handler.stderr`.

        Captured rather than let through because v0.17.0 makes a failed send PRINT: the
        line is part of what is asserted, and a suite that dumps it on every run trains
        the reader to skip the output where a real problem would appear.
        """
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            handler._do_hook_stop(json.dumps({
                "session_id": session_id or self.SID, "hook_event_name": "Stop",
                "cwd": "C:\\Dev\\sidecrab"}).encode())
        handler.stderr = captured.getvalue()
        return handler

    def ring(self, session_id=None):
        row = self.hooks.snapshot().get(session_id or self.SID) or {}
        return [event["text"] for event in row.get("events", [])]

    def test_a_failed_send_leaves_the_queued_prompt_in_place(self):
        """THE regression. The tap the operator made is still on the card, and the next
        Stop still has it to deliver."""
        self.continues.queue(self.SID, self.PROMPT, time.time())
        self.stop(self.handler(fail_on_continue=True))
        self.assertEqual(self.continues.peek(self.SID, time.time()), self.PROMPT)

    def test_the_next_stop_delivers_the_prompt_the_failed_one_could_not(self):
        self.continues.queue(self.SID, self.PROMPT, time.time())
        self.stop(self.handler(fail_on_continue=True))
        handler = self.stop(self.handler())
        self.assertEqual(json.loads(handler.sent[-1][1]),
                         crabd.stop_continue_body(self.PROMPT))
        self.assertIsNone(self.continues.peek(self.SID, time.time()))

    def test_a_failed_send_writes_no_continue_sent_line(self):
        """The history says what HAPPENED. Nothing was sent, so nothing may claim it
        was - and the operator reading the timeline after a lost turn must not be told
        the prompt already went."""
        self.continues.queue(self.SID, self.PROMPT, time.time())
        self.stop(self.handler(fail_on_continue=True))
        self.assertNotIn("continue sent: " + self.PROMPT, self.ring())

    def test_the_state_machine_is_still_fed_by_a_stop_whose_send_fails(self):
        """The record happens before the answer and must stay there: skip it and the
        session sits on `working` forever, which is a worse failure than the lost
        prompt."""
        self.continues.queue(self.SID, self.PROMPT, time.time())
        self.stop(self.handler(fail_on_continue=True))
        self.assertEqual(self.hooks.snapshot()[self.SID]["state"], "done")

    # ---- v0.17.0: the failure is REPORTED, not raised

    def test_a_failed_send_does_not_escape_the_handler(self):
        """Left to propagate it reaches socketserver's handle_error, which prints a full
        traceback for the most ordinary transport event there is - the CLI's hook client
        (its own budget is ~2 s) hanging up before crabd answers. A traceback is how
        crabd reports the UNEXPECTED; this is expected."""
        self.continues.queue(self.SID, self.PROMPT, time.time())
        handler = self.handler(fail_on_continue=True)
        self.stop(handler)                       # would raise BrokenPipeError before
        self.assertEqual(handler.sent, [])       # nothing reached the socket

    def test_the_failure_is_one_honest_line_naming_what_happened(self):
        self.continues.queue(self.SID, self.PROMPT, time.time())
        handler = self.stop(self.handler(fail_on_continue=True))
        lines = [line for line in handler.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, handler.stderr)
        self.assertIn("BrokenPipeError", lines[0])
        self.assertIn("queued continue is kept", lines[0])
        self.assertNotIn("Traceback", handler.stderr)

    def test_a_successful_send_says_nothing(self):
        """The line marks a failure. Printed on the healthy path it would be noise the
        operator learns to scroll past - the same reasoning as any alert that fires on a
        healthy night."""
        self.continues.queue(self.SID, self.PROMPT, time.time())
        self.assertEqual(self.stop(self.handler()).stderr, "")
        self.assertEqual(self.stop(self.handler()).stderr, "")   # and the pass-through

    def test_the_connection_is_marked_dead_after_a_failed_send(self):
        """The socket that refused the answer is not one to read the next request from."""
        self.continues.queue(self.SID, self.PROMPT, time.time())
        handler = self.stop(self.handler(fail_on_continue=True))
        self.assertTrue(handler.close_connection)

    def test_a_successful_send_consumes_it_exactly_once(self):
        self.continues.queue(self.SID, self.PROMPT, time.time())
        first = self.stop(self.handler())
        self.assertEqual(json.loads(first.sent[-1][1]),
                         crabd.stop_continue_body(self.PROMPT))
        self.assertIn("continue sent: " + self.PROMPT, self.ring())
        second = self.stop(self.handler())
        self.assertEqual(json.loads(second.sent[-1][1]), {})

    def test_an_expired_entry_is_still_purged_by_a_stop(self):
        """peek IGNORES an expired item where drain DELETED it, so the peek-first shape
        has to purge explicitly - otherwise an item this Stop ruled too old sits there
        for the next one to rule on again."""
        self.continues.queue(self.SID, self.PROMPT,
                             time.time() - crabd.CONTINUE_TTL_SEC - 5)
        handler = self.stop(self.handler())
        self.assertEqual(json.loads(handler.sent[-1][1]), {})
        self.assertEqual(self.continues.pending(time.time()), 0)
        self.assertIsNone(self.continues.entry(self.SID, time.time()))

    def test_the_pass_through_send_is_not_wrapped_in_the_consume(self):
        """Nothing queued: one answer, no drain, no history line, no exception."""
        handler = self.stop(self.handler())
        self.assertEqual(handler.sent, [(200, b"{}")])
        self.assertEqual([t for t in self.ring() if "continue sent" in t], [])


# ------------------------- 10. CRB-F2: concurrent build() over the shared warm caches

class ColdStartBuildRaceTests(unittest.TestCase):
    """QA-Audit 2026-08-27 CRB-F2 (fixed v0.16.0). `TranscriptStore.files` was mutated by
    scan() and iterated by build(); `GitLookup._cache` likewise. At cold start more than
    one thread builds - the refresh loop, plus /v1/state building on demand because no
    snapshot exists yet - so a delete in scan()'s sweep could land inside another
    build()'s `.values()` walk. Worst case is one 500 that self-heals on the next poll,
    which is exactly why it survived fifteen waves.

    THE PROOF IS THE DETERMINISTIC PAIR, not the stress test. `test_build_does_not_walk_
    the_live_dict` was mutation-checked on 2026-08-27: reverting build() to
    `.files.values()` fails it immediately, every run. The threaded test below is a GUARD
    - it did NOT reproduce the race against the pre-fix shape in the runs tried, which is
    exactly what a rare interleaving looks like and why nobody caught this in fifteen
    waves. Do not read a green stress test as evidence of anything.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.projects = self.root / "projects"
        self.projects.mkdir(parents=True)

    def transcript(self, name: str) -> Path:
        project = self.projects / "C--Dev-sidecrab"
        project.mkdir(parents=True, exist_ok=True)
        path = project / f"{name}.jsonl"
        path.write_text(json.dumps({"type": "user", "timestamp": "2026-08-27T00:00:00Z",
                                    "cwd": "C:\\Dev\\sidecrab",
                                    "message": {"role": "user", "content": "hi"}}) + "\n",
                        encoding="utf-8")
        return path

    def store(self):
        store = crabd.TranscriptStore(self.projects)
        store.scan(time.time())
        return store

    def test_snapshot_is_a_copy_the_scan_cannot_mutate_underneath(self):
        """The deterministic one. A snapshot taken before a scan that evicts everything
        is still walkable afterwards - which is the whole property build() needs."""
        for name in ("aaaaaaaa-0000-0000-0000-00000000000a",
                     "bbbbbbbb-0000-0000-0000-00000000000b"):
            self.transcript(name)
        store = self.store()
        snapshot = store.snapshot()
        self.assertEqual(len(snapshot), 2)
        store.files.clear()                     # what the delete sweep does, at its worst
        self.assertEqual(len(snapshot), 2)      # pre-fix this was a live view
        self.assertEqual(len(list(snapshot)), 2)

    def test_build_does_not_walk_the_live_dict(self):
        """Belt and braces against a future edit putting `.files.values()` back: a store
        whose `files` raises on iteration must still build, because build() is supposed
        to be holding a list by then."""
        store = self.store()

        class Exploding(dict):
            def values(self):
                raise AssertionError("build() iterated the LIVE dict - see CRB-F2")

        builder = crabd.StateBuilder(store, crabd.HookTracker(), StubLimits(),
                                     time.time(), crabd.UserConfig(self.root / "c.json"))
        facts = store.snapshot()
        store.snapshot = lambda: list(facts)
        store.files = Exploding(store.files)
        self.assertIn("sessions", builder.build())

    def test_concurrent_builds_over_a_churning_store_never_raise(self):
        """The stress guard. Two builders on one store while a third thread creates and
        deletes transcripts - the shape that produced the 500."""
        store = self.store()
        builder = crabd.StateBuilder(store, crabd.HookTracker(), StubLimits(),
                                     time.time(), crabd.UserConfig(self.root / "c.json"))
        errors = []
        stop = threading.Event()

        def churn():
            names = [f"cccccccc-0000-0000-0000-{i:012d}" for i in range(6)]
            try:
                while not stop.is_set():
                    for name in names:
                        self.transcript(name)
                    for name in names:
                        try:
                            (self.projects / "C--Dev-sidecrab"
                             / f"{name}.jsonl").unlink(missing_ok=True)
                        except PermissionError:
                            # Windows refuses to unlink a file a builder currently has
                            # open. That is this harness racing itself, not the defect
                            # under test - the churn continues either way.
                            pass
            except Exception as exc:            # noqa: BLE001
                errors.append(("churn", repr(exc)))

        def build():
            try:
                for _ in range(40):
                    builder.build()
            except Exception as exc:            # noqa: BLE001
                errors.append(("build", repr(exc)))

        churner = threading.Thread(target=churn, daemon=True)
        churner.start()
        builders = [threading.Thread(target=build, daemon=True) for _ in range(3)]
        for thread in builders:
            thread.start()
        for thread in builders:
            thread.join(timeout=60)
        stop.set()
        churner.join(timeout=10)
        self.assertEqual(errors, [])

    def test_the_git_cache_survives_concurrent_lookups_of_the_same_cwd(self):
        """GitLookup._cache, the other half of CRB-F2. Concurrent misses on one cwd are
        allowed to read twice - both readings are of the same file - but must agree and
        must never raise."""
        lookup = crabd.GitLookup()
        cwd = str(Path(__file__).resolve().parents[2])
        results, errors = [], []

        def get():
            try:
                for _ in range(50):
                    results.append(lookup.get(cwd))
            except Exception as exc:            # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=get, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 1, set(results))


class PermissionRaisesTheCardTests(PermissionLiveFireTests):
    """v0.20.0. A live PermissionRequest is a session WAITING ON THE OPERATOR, and the
    card has to say so - the panel renders Approve / Deny off the needs_input sheet, so
    before this the card carrying the pendingPermission could be the one card not
    offering it.

    The property under test is the ROUND TRIP, not the raise: every way a permission can
    resolve must end with the card standing down. All three are here.
    """

    def held(self, timeout=5):
        """Park a permission hook off-thread and wait until the broker has it."""
        out = []
        thread = self.fire(lambda: out.append(
            self.post("/v1/hook/permission", self.permission_body(), timeout=30)))
        deadline = time.time() + timeout
        while time.time() < deadline and self.permissions.pending(self.SID) is None:
            time.sleep(0.01)
        self.assertIsNotNone(self.permissions.pending(self.SID))
        return thread, out

    def test_a_held_permission_puts_the_card_on_needs_input(self):
        self.enable_panel()
        crabd.PERMISSION_POLL_SEC = 20
        # idle, not working, since v0.28.2 (the fixture's SessionStart no longer claims
        # a turn) - which makes this test also the live proof that a permission still
        # raises needs_input FROM idle (PERMISSION_ALERT_FROM grew "idle" for exactly
        # the SDK/headless case this fixture now resembles).
        self.assertEqual(self.row()["state"], "idle")
        thread, _ = self.held()
        row = self.row()
        self.assertEqual(row["state"], "needs_input")
        self.assertEqual(row["question"], crabd.PERMISSION_QUESTION % "Bash")
        self.assertFalse(row["acked"])
        # The buttons and the sheet that renders them now agree.
        self.assertEqual(row["pendingPermission"]["tool"], "Bash")
        self.post("/v1/action", decide_body(self, "deny"))
        thread.join(timeout=15)

    def test_a_panel_tap_stands_the_card_down(self):
        for decision in ("allow", "deny"):
            with self.subTest(decision=decision):
                self.enable_panel()
                crabd.PERMISSION_POLL_SEC = 20
                thread, _ = self.held()
                self.assertEqual(self.row()["state"], "needs_input")
                self.assertEqual(self.post("/v1/action", decide_body(self, decision))[0], 204)
                thread.join(timeout=15)
                row = self.row()
                self.assertEqual(row["state"], "working")
                self.assertIsNone(row["question"])
                self.assertIsNone(row["pendingPermission"])

    def test_a_timed_out_hold_stands_the_card_down(self):
        """The pass-through. The operator never tapped, the terminal dialog owns the
        decision - and a card that goes on offering Approve / Deny for a hold that is
        gone is offering a 404."""
        self.enable_panel()
        crabd.PERMISSION_POLL_SEC = 0.3
        status, body = self.post("/v1/hook/permission", self.permission_body())
        self.assertEqual((status, json.loads(body)), (200, {}))
        row = self.row()
        self.assertEqual(row["state"], "working")
        self.assertIsNone(row["pendingPermission"])

    def test_an_in_app_answer_stands_the_card_down(self):
        """The third path, and the one v0.19.0 built: the operator clicks Allow in the
        terminal while the hold is still open. No hook fires at decision time - the
        completed model round-trip in the transcript is the only evidence there is."""
        self.enable_panel()
        crabd.PERMISSION_POLL_SEC = 20
        thread, _ = self.held()
        self.assertEqual(self.row()["state"], "needs_input")
        # The turn clock moves past the hold: the tool ran and the model was called again.
        since = self.hooks.snapshot()[self.SID]["since"]
        self.hooks.note_activity(self.SID,
                                 since + crabd.NEEDS_INPUT_ACTIVITY_GRACE_SEC + 1)
        self.assertEqual(self.row()["state"], "working")
        self.post("/v1/action", decide_body(self, "deny"))
        thread.join(timeout=15)

    def test_a_stop_during_the_hold_leaves_the_card_finished_not_working(self):
        """PERMISSION_STALE_EVENTS retires the hold, and the Stop owns the state. The
        stand-down must not overwrite a `done` the hooks just wrote."""
        self.enable_panel()
        crabd.PERMISSION_POLL_SEC = 20
        thread, _ = self.held()
        self.assertEqual(self.post("/v1/hook/stop",
                                   {"session_id": self.SID,
                                    "hook_event_name": "Stop"})[0], 200)
        thread.join(timeout=15)
        self.assertEqual(self.row()["state"], "done")
        self.assertEqual(self.permissions.count(), 0)

    def test_a_ghost_permission_never_raises_a_card(self):
        """The serving gate is upstream of the raise, so a request for a session nothing
        serves still conjures no row - it must not have become a way to create one."""
        self.enable_panel()
        crabd.PERMISSION_POLL_SEC = 5
        self.post("/v1/hook/permission", self.permission_body(session_id=self.GHOST))
        self.assertNotIn(self.GHOST, self.hooks.snapshot())

    def _held_tool(self, tool, summary_key, summary, timeout=5):
        """Park a permission hook for a SPECIFIC tool off-thread; wait until the broker's
        current entry is that tool (a replace makes the newest the current one)."""
        out = []
        body = self.permission_body(tool_name=tool, tool_input={summary_key: summary})
        thread = self.fire(lambda: out.append(
            self.post("/v1/hook/permission", body, timeout=30)))
        deadline = time.time() + timeout
        while time.time() < deadline:
            pend = self.permissions.pending(self.SID)
            if pend is not None and pend["tool"] == tool:
                break
            time.sleep(0.01)
        pend = self.permissions.pending(self.SID)
        self.assertIsNotNone(pend)
        self.assertEqual(pend["tool"], tool)
        return thread, out

    def test_a_replaced_hold_does_not_stand_down_a_card_whose_live_hold_is_parked(self):
        """A-01 (P1, v0.26.0) - THE full-stack parallel-permission case. register() is
        newest-wins, which is exactly what two parallel tool calls in one assistant message
        produce: request B replaces A and releases A as a pass-through. A's waking thread
        must NOT stand the card down while B is still parked - the row stays needs_input
        carrying B's pendingPermission, and B's own resolution is what finally clears it.

        Before the fix, A's exit called clear_permission unconditionally (permission_alert
        was still True), standing the card to `working` while it served a live Approve/Deny
        for B - the precise defect the needs_input sheet exists to prevent. Mutation check:
        dropping the `has_pending` guard in _await_permission makes the first assertion below
        read `working`."""
        self.enable_panel()
        crabd.PERMISSION_POLL_SEC = 20
        thread_a, _ = self._held_tool("Bash", "command", "git push --force")
        self.assertEqual(self.row()["state"], "needs_input")
        self.assertEqual(self.row()["pendingPermission"]["tool"], "Bash")

        # B arrives for the SAME session and replaces A. A is released as a pass-through.
        thread_b, _ = self._held_tool("Write", "file_path", "C:\\x.txt")
        thread_a.join(timeout=15)          # A's pass-through must have returned
        self.assertFalse(thread_a.is_alive())

        row = self.row()
        self.assertEqual(row["state"], "needs_input")               # <- the fix
        self.assertEqual(row["pendingPermission"]["tool"], "Write")  # B's, still live

        # B now resolves; THIS is the exit that stands the card down.
        self.assertEqual(self.post("/v1/action", decide_body(self, "deny"))[0], 204)
        thread_b.join(timeout=15)
        row = self.row()
        self.assertEqual(row["state"], "working")
        self.assertIsNone(row["pendingPermission"])
        self.assertEqual(self.permissions.count(), 0)


# --------------------------------------------------- 8. /v1/state cannot answer a 500

# The record shapes MEASURED on 2026-08-27 as crashing the pre-v0.20.0 parser. Held
# LOCALLY rather than imported from test_crabd for the reason this module exists at all
# (see the module docstring): concurrent authors against one tree.
POISON_RECORDS = [
    ("message is a string",
     {"type": "assistant", "requestId": "p1", "message": "a bare string"}),
    ("usage is a list",
     {"type": "assistant", "requestId": "p2",
      "message": {"role": "assistant", "usage": [1, 2]}}),
    ("a counter is a word",
     {"type": "assistant", "requestId": "p3",
      "message": {"usage": {"output_tokens": "twelve"}}}),
    ("a counter is Infinity",
     {"type": "assistant", "requestId": "p4",
      "message": {"usage": {"output_tokens": float("inf")}}}),
    ("a user message is a string", {"type": "user", "message": "bare"}),
]


class StateEndpointNeverFivesTests(LiveFireServed):
    """v0.20.0, from an observed production crash: the FIRST GET /v1/state ~2 s after
    crabd started raised out of the do_GET branch and the operator got a 500.

    The guarantee is upstream - a record crabd cannot read is skipped by the parser - and
    these cover the backstop behind it. `/v1/state` has exactly two honest answers when
    the builder cannot produce a document, and a traceback is neither of them.
    """

    def test_a_builder_that_cannot_build_serves_the_last_good_snapshot(self):
        """Stale and honest, with `generatedAt` saying how stale. The same signal a
        wedged refresh thread already produces."""
        good = self.rebuild()
        self.builder.build = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("a shape from the future"))
        status, body = self.get("/v1/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["generatedAt"], good["generatedAt"])

    def test_a_cold_start_with_no_snapshot_at_all_is_503_not_500(self):
        """Serving `sessions: []` here would say "you have no sessions running", which is
        an answer crabd made up. 503 is the honest one and the widget retries in 2 s."""
        with self.builder._lock:
            self.builder._state = None
        self.builder.build = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("a shape from the future"))
        with contextlib.redirect_stderr(io.StringIO()):
            status, body = self.get("/v1/state")
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": "state not built yet"})

    def test_a_poisoned_transcript_cannot_take_the_endpoint_down(self):
        """End to end on the shape that actually crashed: an unreadable record under
        ~/.claude/projects, a cold start with no snapshot, and a first GET."""
        projects = self.builder.store.projects_dir
        path = projects / "C--Dev--sidecrab" / f"{self.SID}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for _, record in POISON_RECORDS:
                fh.write(json.dumps(record) + "\n")
            fh.write('{"type":"assistant","message":{"usage":{"out\n')
            fh.write("not json at all\n")
        with self.builder._lock:
            self.builder._state = None
        status, body = self.get("/v1/state")
        self.assertEqual(status, 200)
        self.assertEqual(body["schema"], 5)
        self.assertEqual(body["crabd"]["version"], crabd.VERSION)

    def test_a_reader_that_hangs_up_mid_answer_is_not_a_traceback(self):
        """ORDINARY transport on this host, and until v0.20.0 it walked into
        socketserver's handle_error and printed a full traceback for a client that simply
        stopped listening."""
        crabd._LOG_ONCE_SEEN.discard(crabd.GET_HANGUP_LOG_KEY)
        self.addCleanup(crabd._LOG_ONCE_SEEN.discard, crabd.GET_HANGUP_LOG_KEY)
        # The hang-up is INJECTED, not raced. A real client closing its socket wins the
        # race against a small loopback response most of the time, so a test that just
        # closes early is a test that mostly does not reach the branch it names.
        original = crabd.Handler._send
        self.addCleanup(lambda: setattr(crabd.Handler, "_send", original))

        def hang_up(handler, code, body, ctype="application/json"):
            raise ConnectionAbortedError(10053, "software caused connection abort")

        crabd.Handler._send = hang_up
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with contextlib.suppress(Exception):
                self.get("/v1/state", timeout=10)
            crabd.Handler._send = original
            self.client.close()
            # The server is still healthy for everyone else, which is the real assertion.
            self.assertEqual(self.get("/v1/state")[0], 200)
        printed = captured.getvalue()
        self.assertNotIn("Traceback", printed)
        self.assertIn("hung up before its GET was answered", printed)


if __name__ == "__main__":
    unittest.main()
