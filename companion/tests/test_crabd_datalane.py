"""v0.12.0 CRABD DATA LANE tests - statusLine ingest + OTLP receiver.

Covers the two data surfaces the "control-surface wave" adds to crabd:

  1. POST /v1/statusline - the official Claude Code stdin session document becomes
     `limits` (source "statusline", preferred over the OAuth reach-around while fresh)
     and per-session `contextTokens` (contextSource "statusline").
  2. POST /v1/metrics + /v1/logs - OTLP http/json becomes burn.costUSD (today, from
     claude_code.cost.usage) and api_error events on the session ring.

Held in a SEPARATE module from test_crabd.py deliberately: the two files were authored
concurrently against the same tree, so keeping the data-lane suite here avoids editing
the shared file from two writers at once. `python -m pytest companion/tests` discovers
both. Everything runs offline against fixtures - no network, the real ~/.claude and
~/.sidecrab are never touched (see setUpModule).

Shapes are verified against the current docs (cited inline):
  statusLine stdin JSON   https://code.claude.com/docs/en/statusline
  OTLP metrics / logs     https://code.claude.com/docs/en/monitoring-usage
"""

import json
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
from _httpkeepalive import quiesce, start_test_server  # noqa: E402


# --------------------------------------------------------------- module isolation

_MODULE_TMP = None


def setUpModule():
    """Same hard isolation test_crabd.py uses: nothing here may reach the operator's
    real limits-cache / config / history under ~. These tests never construct a real
    LimitsReader/HistoryLog/UserConfig-with-default-path, but the guard is cheap and
    removes the one-forgotten-line failure mode that once poisoned production."""
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


# OAuth block a StubLimits hands back - the FALLBACK source. Deliberately carries no
# `source` key: the builder is what stamps provenance, and a reader that pre-stamped it
# would hide a builder path that forgot to.
OAUTH_LIMITS = {
    "available": True, "note": None,
    "fiveHour": {"utilization": 0.42, "resetsAt": "2026-08-26T21:00:00Z"},
    "weekly": {"utilization": 0.18, "resetsAt": "2026-08-30T00:00:00Z"},
    "extra": [], "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x",
}


class StubLimits:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else dict(OAUTH_LIMITS)

    def get(self, now, force=False):
        return self.payload


# ------------------------------------------------------------------- doc fixtures

def statusline_doc(session_id=None, five=None, seven=None, extra_windows=None,
                   context_window="OMIT"):
    """A minimal but shaped-real statusLine stdin document.

    `five`/`seven` are (used_percentage, resets_at_epoch_seconds) or None (window
    absent). `context_window` is a dict, None, or "OMIT" (the key absent entirely).
    Verified against the doc's full JSON example (2.1.x).
    """
    doc = {}
    if session_id is not None:
        doc["session_id"] = session_id
    rate_limits = {}
    if five is not None:
        rate_limits["five_hour"] = {"used_percentage": five[0], "resets_at": five[1]}
    if seven is not None:
        rate_limits["seven_day"] = {"used_percentage": seven[0], "resets_at": seven[1]}
    if extra_windows:
        rate_limits.update(extra_windows)
    if rate_limits:
        doc["rate_limits"] = rate_limits
    if context_window != "OMIT":
        doc["context_window"] = context_window
    return doc


def _anyvalue(value):
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}   # protobuf-JSON serialises 64-bit ints as strings
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _attrs(mapping):
    return [{"key": k, "value": _anyvalue(v)} for k, v in mapping.items()]


def cost_metrics(points, temporality=None, metric_name=None, value_key="asDouble"):
    """An OTLP ExportMetricsServiceRequest carrying one cost counter.

    `points` = list of (usd, attrs_dict, time_ns_or_None). Verified against the doc's
    data-point example: resourceMetrics -> scopeMetrics -> metrics[name=cost] ->
    sum.dataPoints[asDouble, timeUnixNano, attributes].
    """
    if temporality is None:
        temporality = crabd.OTLP_TEMPORALITY_DELTA
    data_points = []
    for usd, attrs, time_ns in points:
        point = {value_key: usd}
        if time_ns is not None:
            point["timeUnixNano"] = str(time_ns)
        if attrs:
            point["attributes"] = _attrs(attrs)
        data_points.append(point)
    return {"resourceMetrics": [{
        "resource": {"attributes": _attrs({"service.name": "claude-code"})},
        "scopeMetrics": [{
            "scope": {"name": "claude-code"},
            "metrics": [{
                "name": metric_name or crabd.OTLP_COST_METRIC,
                "unit": "USD",
                "sum": {"aggregationTemporality": temporality, "isMonotonic": True,
                        "dataPoints": data_points},
            }],
        }],
    }]}


def error_logs(records, event_via_attr=True):
    """An OTLP ExportLogsServiceRequest of api_error events.

    `records` = list of attribute dicts. Verified against the doc's api_error event:
    session.id, event.name, status_code, attempt carried as log-record attributes.
    """
    log_records = []
    for rec in records:
        mapping = dict(rec)
        entry = {"timeUnixNano": str(int(time.time() * 1e9)),
                 "attributes": _attrs(mapping)}
        if not event_via_attr and "event.name" in mapping:
            # The other spelling some collectors use: eventName on the record itself.
            entry["eventName"] = mapping["event.name"]
            entry["attributes"] = _attrs({k: v for k, v in mapping.items()
                                          if k != "event.name"})
        log_records.append(entry)
    return {"resourceLogs": [{
        "resource": {"attributes": _attrs({"service.name": "claude-code"})},
        "scopeLogs": [{"scope": {"name": "claude-code"}, "logRecords": log_records}],
    }]}


# ============================================================ StatusLineReader unit

class StatusLineWindowTests(unittest.TestCase):
    """_window: {used_percentage, resets_at} -> {utilization, resetsAt}."""

    def test_used_percentage_is_a_percent_divided_by_100_unconditionally(self):
        # Doc: rate_limits.*.used_percentage is 0..100. 23.5 -> 0.235, NEVER sniffed as
        # already-0..1 the way the OAuth path has to guess.
        w = crabd.StatusLineReader._window({"used_percentage": 23.5,
                                            "resets_at": 1738425600})
        self.assertEqual(w["utilization"], 0.235)

    def test_a_sub_one_percent_is_not_read_as_forty_percent(self):
        # The exact trap the dedicated parser exists to avoid: 0.4% is nearly empty.
        w = crabd.StatusLineReader._window({"used_percentage": 0.4, "resets_at": 0})
        self.assertEqual(w["utilization"], 0.004)

    def test_resets_at_epoch_seconds_becomes_utc_iso(self):
        w = crabd.StatusLineReader._window({"used_percentage": 50,
                                            "resets_at": 1738425600})
        self.assertEqual(w["resetsAt"], crabd._utc_iso(1738425600))
        self.assertTrue(w["resetsAt"].endswith("Z"))

    def test_hundred_percent_clamps_to_one(self):
        w = crabd.StatusLineReader._window({"used_percentage": 140, "resets_at": 0})
        self.assertEqual(w["utilization"], 1.0)

    def test_absent_or_nonnumeric_percentage_is_none(self):
        for obj in (None, {}, {"used_percentage": None}, {"used_percentage": "40"},
                    {"used_percentage": True}, {"resets_at": 1738425600}, 5):
            self.assertIsNone(crabd.StatusLineReader._window(obj), obj)

    def test_missing_resets_at_leaves_resetsAt_null_not_the_epoch(self):
        w = crabd.StatusLineReader._window({"used_percentage": 10})
        self.assertEqual(w["utilization"], 0.1)
        self.assertIsNone(w["resetsAt"])


class StatusLineMapLimitsTests(unittest.TestCase):
    """_map_limits: the rate_limits object -> the contract's limits block."""

    def test_both_windows_map_and_the_block_is_contract_shaped(self):
        block = crabd.StatusLineReader._map_limits(
            statusline_doc(five=(23.5, 1738425600), seven=(41.2, 1738857600)))
        self.assertTrue(block["available"])
        self.assertIsNone(block["note"])
        self.assertEqual(block["fiveHour"]["utilization"], 0.235)
        self.assertEqual(block["weekly"]["utilization"], 0.412)
        self.assertEqual(block["extra"], [])
        # The document never carries the plan name or tier, so they are null - not
        # borrowed from the OAuth reading and mislabelled.
        self.assertIsNone(block["subscriptionType"])
        self.assertIsNone(block["rateLimitTier"])
        self.assertNotIn("source", block)   # the builder stamps provenance, not the reader

    def test_one_window_present_is_half_a_reading_not_an_em_dash_on_both(self):
        block = crabd.StatusLineReader._map_limits(
            statusline_doc(five=(30, 1738425600)))
        self.assertEqual(block["fiveHour"]["utilization"], 0.3)
        self.assertIsNone(block["weekly"])

    def test_absent_rate_limits_is_none_the_signal_to_fall_back_to_oauth(self):
        # API-key / Bedrock / Vertex / pre-first-response: rate_limits missing entirely.
        for doc in (statusline_doc(), statusline_doc(session_id="s"),
                    {"rate_limits": {}}, {"rate_limits": "nope"}):
            self.assertIsNone(crabd.StatusLineReader._map_limits(doc), doc)

    def test_seven_day_suffixed_siblings_become_sorted_extras(self):
        block = crabd.StatusLineReader._map_limits(statusline_doc(
            five=(10, 0), seven=(20, 0),
            extra_windows={"seven_day_opus": {"used_percentage": 5, "resets_at": 0},
                           "seven_day_sonnet": {"used_percentage": 60, "resets_at": 0}}))
        self.assertEqual([e["label"] for e in block["extra"]],
                         ["sonnet weekly", "opus weekly"])   # desc by utilization
        self.assertEqual(block["extra"][0]["utilization"], 0.6)


class StatusLineContextTokensTests(unittest.TestCase):
    """_context_tokens: context_window.total_input_tokens is contextTokens.

    Doc: total_input_tokens = input + cache_creation + cache_read, which is exactly
    crabd's transcript-side contextTokens definition, so the provenance flip is seamless.
    """

    def test_total_input_tokens_is_the_context_size(self):
        self.assertEqual(
            crabd.StatusLineReader._context_tokens(
                {"total_input_tokens": 15500,
                 "current_usage": {"input_tokens": 8500}}), 15500)

    def test_null_current_usage_with_zero_total_is_unknown_not_zero(self):
        # Before the first API call / just after /compact: a real 0 and "no request yet"
        # are different facts and only one lights a chip.
        self.assertIsNone(crabd.StatusLineReader._context_tokens(
            {"total_input_tokens": 0, "current_usage": None}))

    def test_a_real_zero_with_usage_present_is_zero(self):
        self.assertEqual(crabd.StatusLineReader._context_tokens(
            {"total_input_tokens": 0, "current_usage": {"input_tokens": 0}}), 0)

    def test_absent_or_nonnumeric_is_none(self):
        for block in (None, {}, "nope", {"total_input_tokens": None},
                      {"total_input_tokens": True}):
            self.assertIsNone(crabd.StatusLineReader._context_tokens(block), block)


class StatusLineLimitsFreshnessTests(unittest.TestCase):
    """ingest + limits(now): preferred while fresh, then the OAuth fallback."""

    def test_a_fresh_reading_is_served(self):
        r = crabd.StatusLineReader()
        now = time.time()
        self.assertTrue(r.ingest(statusline_doc(five=(30, 0)), now))
        self.assertEqual(r.limits(now)["fiveHour"]["utilization"], 0.3)

    def test_silence_past_ten_minutes_demotes_to_none(self):
        # Contract: OAuth is the fallback once no status line document has arrived in
        # 10 min. Precisely at the boundary it is still served; one second past it, gone.
        r = crabd.StatusLineReader()
        r.ingest(statusline_doc(five=(30, 0)), 1000.0)
        self.assertIsNotNone(r.limits(1000.0 + crabd.STATUSLINE_PREFER_SEC))
        self.assertIsNone(r.limits(1000.0 + crabd.STATUSLINE_PREFER_SEC + 1))

    def test_silence_is_measured_from_the_last_doc_that_HAD_windows(self):
        # A stream of rate_limit-less docs (an API-key session) must not hold the gauges
        # on a reading that stopped refreshing.
        r = crabd.StatusLineReader()
        r.ingest(statusline_doc(five=(30, 0)), 1000.0)
        r.ingest(statusline_doc(session_id="s"), 1000.0 + 400)   # no windows
        self.assertIsNone(r.limits(1000.0 + crabd.STATUSLINE_PREFER_SEC + 1))

    def test_a_reading_is_withheld_before_it_can_ever_grow_a_stale_note(self):
        # NOTE (reported to the lane owner): limits() returns None once the reading is
        # older than STATUSLINE_PREFER_SEC (600 s), but the stale-note branch inside it
        # only fires past LIMITS_NOTE_STALE_SEC (900 s). Since 900 > 600 the note branch
        # is UNREACHABLE - the reading is withheld first. This test pins the reachable
        # truth (withheld, not qualified); the note branch is dead code, harmless, and
        # flagged for the owning session rather than exercised here.
        r = crabd.StatusLineReader()
        r.ingest(statusline_doc(five=(30, 0)), 1000.0)
        self.assertIsNone(r.limits(1000.0 + crabd.LIMITS_NOTE_STALE_SEC + 1))
        # Everywhere it IS served (inside the prefer window) the note stays absent.
        self.assertIsNone(r.limits(1000.0 + crabd.STATUSLINE_PREFER_SEC)["note"])

    def test_a_fresh_reading_carries_no_note(self):
        r = crabd.StatusLineReader()
        r.ingest(statusline_doc(five=(30, 0)), 1000.0)
        self.assertIsNone(r.limits(1000.0 + 5)["note"])

    def test_ingest_ignores_a_non_dict_and_never_raises(self):
        r = crabd.StatusLineReader()
        for junk in (None, "x", 5, [1, 2], b"bytes"):
            self.assertFalse(r.ingest(junk, time.time()))
        self.assertIsNone(r.limits(time.time()))


class StatusLineContextFreshnessTests(unittest.TestCase):
    """context(sid, now) -> (known, tokens): the three states, and expiry."""

    def test_a_known_session_returns_its_tokens(self):
        r = crabd.StatusLineReader()
        now = time.time()
        r.ingest(statusline_doc(session_id="s1",
                                context_window={"total_input_tokens": 42000,
                                                "current_usage": {"input_tokens": 1}}), now)
        self.assertEqual(r.context("s1", now), (True, 42000))

    def test_a_never_seen_session_is_not_known(self):
        r = crabd.StatusLineReader()
        self.assertEqual(r.context("nobody", time.time()), (False, None))

    def test_known_but_unknown_is_distinct_from_never_seen(self):
        # A context_window whose current_usage is null: the status line HAS spoken about
        # this session and said "unknown". known=True, tokens=None - and the builder must
        # NOT fall back to the transcript for it (that is the point of the bool).
        r = crabd.StatusLineReader()
        now = time.time()
        r.ingest(statusline_doc(session_id="s2",
                                context_window={"total_input_tokens": 0,
                                                "current_usage": None}), now)
        self.assertEqual(r.context("s2", now), (True, None))

    def test_a_session_expires_from_the_table(self):
        r = crabd.StatusLineReader()
        r.ingest(statusline_doc(session_id="s3",
                                context_window={"total_input_tokens": 10,
                                                "current_usage": {"input_tokens": 10}}),
                 1000.0)
        self.assertEqual(r.context("s3", 1000.0 + crabd.STATUSLINE_SESSION_KEEP_SEC),
                         (True, 10))
        self.assertEqual(
            r.context("s3", 1000.0 + crabd.STATUSLINE_SESSION_KEEP_SEC + 1), (False, None))

    def test_prune_frees_expired_sessions(self):
        r = crabd.StatusLineReader()
        r.ingest(statusline_doc(session_id="old",
                                context_window={"total_input_tokens": 1,
                                                "current_usage": {"input_tokens": 1}}),
                 1000.0)
        r.prune(1000.0 + crabd.STATUSLINE_SESSION_KEEP_SEC + 1)
        self.assertEqual(r._sessions, {})


# ======================================================= OtlpReceiver metrics unit

class OtlpCostMetricsTests(unittest.TestCase):
    """ingest_metrics + cost_today: today's USD from claude_code.cost.usage."""

    def setUp(self):
        self.now = time.time()
        self.now_ns = int(self.now * 1e9)

    def test_null_before_any_telemetry_never_a_fabricated_zero(self):
        r = crabd.OtlpReceiver()
        self.assertIsNone(r.cost_today(self.now))

    def test_a_single_delta_becomes_todays_cost(self):
        r = crabd.OtlpReceiver()
        taken = r.ingest_metrics(
            cost_metrics([(0.0012, {"model": "claude-sonnet-5"}, self.now_ns)]), self.now)
        self.assertEqual(taken, 1)
        self.assertEqual(r.cost_today(self.now), 0.0012)

    def test_deltas_sum_across_models_and_sessions(self):
        r = crabd.OtlpReceiver()
        r.ingest_metrics(cost_metrics([
            (0.10, {"model": "a", "query_source": "main"}, self.now_ns),
            (0.25, {"model": "b", "query_source": "subagent"}, self.now_ns),
            (0.05, {"model": "a", "query_source": "auxiliary"}, self.now_ns),
        ]), self.now)
        self.assertEqual(r.cost_today(self.now), 0.4)

    def test_seen_but_nothing_today_is_zero_not_null(self):
        # Telemetry flowed yesterday, none today: costUSD is a real 0.0, costSource still
        # "otlp". Distinct from "telemetry off" (null).
        r = crabd.OtlpReceiver()
        two_days_ago_ns = int((self.now - 2 * 86400) * 1e9)
        r.ingest_metrics(cost_metrics([(0.5, {}, two_days_ago_ns)]), self.now)
        self.assertEqual(r.cost_today(self.now), 0.0)

    def test_a_point_is_bucketed_by_its_own_timeUnixNano(self):
        r = crabd.OtlpReceiver()
        r.ingest_metrics(cost_metrics([
            (0.30, {}, self.now_ns),
            (0.90, {}, int((self.now - 2 * 86400) * 1e9)),   # not today
        ]), self.now)
        self.assertEqual(r.cost_today(self.now), 0.30)

    def test_a_missing_timestamp_falls_back_to_arrival_and_counts_today(self):
        r = crabd.OtlpReceiver()
        r.ingest_metrics(cost_metrics([(0.07, {}, None)]), self.now)
        self.assertEqual(r.cost_today(self.now), 0.07)

    def test_asInt_string_values_are_read(self):
        # protobuf-JSON can serialise a numeric point as asInt (a STRING). A collector in
        # the middle that re-encoded a cost this way must not be silently dropped.
        r = crabd.OtlpReceiver()
        r.ingest_metrics(cost_metrics([(3, {}, self.now_ns)], value_key="asInt"), self.now)
        self.assertEqual(r.cost_today(self.now), 3.0)

    def test_negative_and_non_finite_values_are_not_costs(self):
        r = crabd.OtlpReceiver()
        r.ingest_metrics(cost_metrics([
            (-0.5, {}, self.now_ns),
            (float("inf"), {}, self.now_ns),
            (float("nan"), {}, self.now_ns),
        ]), self.now)
        self.assertIsNone(r.cost_today(self.now))   # nothing valid taken -> never seen

    def test_a_non_cost_metric_is_ignored(self):
        r = crabd.OtlpReceiver()
        taken = r.ingest_metrics(
            cost_metrics([(9.9, {}, self.now_ns)],
                         metric_name="claude_code.token.usage"), self.now)
        self.assertEqual(taken, 0)
        self.assertIsNone(r.cost_today(self.now))

    def test_cumulative_temporality_takes_the_max_per_series_not_the_sum(self):
        # The trap the research named: a cumulative counter must NOT be summed across
        # exports (that double-counts). Two exports of the same series -> the latest, not
        # the total; two DIFFERENT series -> summed at read time.
        r = crabd.OtlpReceiver()
        t = crabd.OTLP_TEMPORALITY_CUMULATIVE
        r.ingest_metrics(cost_metrics([(0.20, {"model": "a"}, self.now_ns)], temporality=t),
                         self.now)
        r.ingest_metrics(cost_metrics([(0.50, {"model": "a"}, self.now_ns)], temporality=t),
                         self.now)   # same series, grew to 0.50
        r.ingest_metrics(cost_metrics([(0.10, {"model": "b"}, self.now_ns)], temporality=t),
                         self.now)   # different series
        self.assertEqual(r.cost_today(self.now), 0.6)   # 0.50 + 0.10, not 0.80

    def test_malformed_metrics_bodies_never_raise_and_take_nothing(self):
        r = crabd.OtlpReceiver()
        for junk in (None, {}, "x", 5, {"resourceMetrics": "nope"},
                     {"resourceMetrics": [None, 5]},
                     {"resourceMetrics": [{"scopeMetrics": [{"metrics": [{}]}]}]},
                     {"resourceMetrics": [{"scopeMetrics": [{"metrics": [
                         {"name": crabd.OTLP_COST_METRIC, "sum": {"dataPoints": "bad"}}]}]}]}):
            self.assertEqual(r.ingest_metrics(junk, self.now), 0, junk)
        self.assertIsNone(r.cost_today(self.now))

    def test_prune_drops_days_that_can_no_longer_be_served(self):
        r = crabd.OtlpReceiver()
        r.ingest_metrics(cost_metrics([
            (0.4, {}, self.now_ns),
            (0.9, {}, int((self.now - 5 * 86400) * 1e9)),
        ]), self.now)
        r.prune(self.now)
        self.assertEqual(r.cost_today(self.now), 0.4)   # today survives
        self.assertEqual(len(r._delta_by_day), 1)       # the old day was freed


# ========================================================== OtlpReceiver logs unit

class OtlpErrorLogsTests(unittest.TestCase):
    """ingest_logs: api_error events routed to the session that owns them."""

    def setUp(self):
        self.now = time.time()
        self.captured = []
        self.receiver = crabd.OtlpReceiver(
            on_event=lambda sid, text: (self.captured.append((sid, text)), True)[1])

    def test_an_api_error_reaches_its_session_with_the_status_code(self):
        taken = self.receiver.ingest_logs(error_logs([
            {"event.name": "api_error", "session.id": "sess-1",
             "status_code": 429, "attempt": 1}]), self.now)
        self.assertEqual(taken, 1)
        self.assertEqual(len(self.captured), 1)
        sid, text = self.captured[0]
        self.assertEqual(sid, "sess-1")
        self.assertIn("429", text)

    def test_the_retry_attempt_is_surfaced_when_greater_than_one(self):
        self.receiver.ingest_logs(error_logs([
            {"event.name": "api_error", "session.id": "s", "status_code": 529,
             "attempt": 3}]), self.now)
        self.assertIn("attempt 3", self.captured[0][1])

    def test_the_free_form_error_message_never_reaches_the_ring(self):
        # Vendor error text is content; the ring line (and, through it, history.jsonl)
        # must carry only the status code, not the message.
        self.receiver.ingest_logs(error_logs([
            {"event.name": "api_error", "session.id": "s", "status_code": 500,
             "error": "overloaded_error: the model is temporarily unavailable"}]), self.now)
        self.assertNotIn("overloaded", self.captured[0][1])
        self.assertNotIn("unavailable", self.captured[0][1])

    def test_the_event_name_via_a_record_eventName_is_also_accepted(self):
        self.receiver.ingest_logs(error_logs(
            [{"event.name": "api_error", "session.id": "s", "status_code": 400}],
            event_via_attr=False), self.now)
        self.assertEqual(self.captured[0][0], "s")

    def test_non_api_error_events_are_ignored(self):
        taken = self.receiver.ingest_logs(error_logs([
            {"event.name": "api_request", "session.id": "s", "status_code": 200},
            {"event.name": "tool_result", "session.id": "s"}]), self.now)
        self.assertEqual(taken, 0)
        self.assertEqual(self.captured, [])

    def test_an_error_with_no_session_id_is_dropped(self):
        taken = self.receiver.ingest_logs(error_logs([
            {"event.name": "api_error", "status_code": 429}]), self.now)
        self.assertEqual(taken, 0)
        self.assertEqual(self.captured, [])

    def test_the_per_export_cap_bounds_one_pathological_batch(self):
        records = [{"event.name": "api_error", "session.id": "s", "status_code": 429}
                   for _ in range(crabd.OTLP_EVENTS_PER_EXPORT + 15)]
        taken = self.receiver.ingest_logs(error_logs(records), self.now)
        self.assertEqual(taken, crabd.OTLP_EVENTS_PER_EXPORT)

    def test_malformed_logs_bodies_never_raise(self):
        for junk in (None, {}, "x", 5, {"resourceLogs": "nope"},
                     {"resourceLogs": [None]},
                     {"resourceLogs": [{"scopeLogs": [{"logRecords": [None, "x"]}]}]}):
            self.assertEqual(self.receiver.ingest_logs(junk, self.now), 0, junk)
        self.assertEqual(self.captured, [])


# ================================================ builder integration (source wiring)

class BuilderHarness(unittest.TestCase):
    """A builder over an empty temp projects dir, with data-lane readers injectable."""

    def make_builder(self, statusline=None, otlp=None, limits=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        projects = Path(tmp.name) / "projects"
        projects.mkdir(parents=True)
        config = crabd.UserConfig(Path(tmp.name) / "config.json")
        return crabd.StateBuilder(
            crabd.TranscriptStore(projects), crabd.HookTracker(),
            StubLimits(limits), time.time(), config,
            statusline=statusline, otlp=otlp)


class LimitsSourceWiringTests(BuilderHarness):
    def test_a_fresh_statusline_reading_is_preferred_over_oauth(self):
        now = time.time()
        sl = crabd.StatusLineReader()
        sl.ingest(statusline_doc(five=(30, 0)), now)
        block = self.make_builder(statusline=sl)._limits_block(now, None)
        self.assertEqual(block["source"], "statusline")
        self.assertEqual(block["fiveHour"]["utilization"], 0.3)

    def test_oauth_is_the_fallback_when_the_statusline_is_silent(self):
        now = time.time()
        sl = crabd.StatusLineReader()
        sl.ingest(statusline_doc(five=(30, 0)), now - crabd.STATUSLINE_PREFER_SEC - 1)
        block = self.make_builder(statusline=sl)._limits_block(now, None)
        self.assertEqual(block["source"], "oauth")
        self.assertEqual(block["fiveHour"]["utilization"], 0.42)   # the OAuth reading

    def test_oauth_is_the_fallback_when_no_statusline_reader_is_wired(self):
        block = self.make_builder(statusline=None)._limits_block(time.time(), None)
        self.assertEqual(block["source"], "oauth")

    def test_oauth_is_the_fallback_when_the_statusline_never_carried_windows(self):
        now = time.time()
        sl = crabd.StatusLineReader()
        sl.ingest(statusline_doc(session_id="s"), now)   # a doc, but no rate_limits
        block = self.make_builder(statusline=sl)._limits_block(now, None)
        self.assertEqual(block["source"], "oauth")

    def test_the_oauth_cached_dict_is_not_mutated_by_stamping(self):
        # limits.get hands back its own cached object; stamping source into it would
        # poison the reader's last-good. The builder must copy first.
        stub = StubLimits()
        cached = stub.payload
        builder = crabd.StateBuilder(
            crabd.TranscriptStore(self._empty_projects()), crabd.HookTracker(),
            stub, time.time(), crabd.UserConfig(self._tmp_path("config.json")))
        builder._limits_block(time.time(), None)
        self.assertNotIn("source", cached)

    def _empty_projects(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "projects"
        p.mkdir(parents=True)
        return p

    def _tmp_path(self, name):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name) / name

    def test_served_limits_source_survives_a_full_build(self):
        now = time.time()
        sl = crabd.StatusLineReader()
        sl.ingest(statusline_doc(five=(15, 0)), now)
        state = self.make_builder(statusline=sl).build(now=now)
        self.assertEqual(state["limits"]["source"], "statusline")


class CostWiringTests(BuilderHarness):
    def test_costUSD_and_costSource_are_null_without_a_receiver(self):
        state = self.make_builder(otlp=None).build()
        self.assertIsNone(state["burn"]["costUSD"])
        self.assertIsNone(state["burn"]["costSource"])

    def test_costUSD_and_costSource_are_populated_from_the_receiver(self):
        now = time.time()
        otlp = crabd.OtlpReceiver()
        otlp.ingest_metrics(cost_metrics([(0.5, {}, int(now * 1e9))]), now)
        state = self.make_builder(otlp=otlp).build(now=now)
        self.assertEqual(state["burn"]["costUSD"], 0.5)
        self.assertEqual(state["burn"]["costSource"], "otlp")

    def test_seen_but_nothing_today_is_zero_dollars_with_otlp_provenance(self):
        now = time.time()
        otlp = crabd.OtlpReceiver()
        otlp.ingest_metrics(cost_metrics([(0.5, {}, int((now - 2 * 86400) * 1e9))]), now)
        state = self.make_builder(otlp=otlp).build(now=now)
        self.assertEqual(state["burn"]["costUSD"], 0.0)
        self.assertEqual(state["burn"]["costSource"], "otlp")


class ContextSourceWiringTests(BuilderHarness):
    """contextTokens + contextSource: statusline wins over the transcript for a session
    it has spoken about; the transcript is the fallback."""

    def _session_with_transcript_context(self, builder, sid, ts):
        # A single assistant usage record -> transcript-derived contextTokens.
        line = {"type": "assistant", "requestId": "req_ctx",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts)),
                "cwd": "C:\\IT", "gitBranch": "master", "isSidechain": False,
                "message": {"role": "assistant", "model": "claude-fable-5",
                            "usage": {"input_tokens": 10, "output_tokens": 5,
                                      "cache_read_input_tokens": 90000,
                                      "cache_creation_input_tokens": 0}}}
        path = builder.store.projects_dir / "C--IT" / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "timestamp": line["timestamp"],
                                 "cwd": "C:\\IT",
                                 "message": {"role": "user", "content": "hi"}}) + "\n")
            fh.write(json.dumps(line) + "\n")
        import os
        os.utime(path, (ts, ts))

    def test_transcript_context_carries_source_transcript(self):
        now = time.time()
        builder = self.make_builder()
        sid = "11111111-0000-0000-0000-000000000001"
        self._session_with_transcript_context(builder, sid, now - 20)
        row = self._row(builder.build(now=now), sid)
        self.assertEqual(row["contextTokens"], 90010)
        self.assertEqual(row["contextSource"], "transcript")

    def test_statusline_context_wins_and_carries_source_statusline(self):
        now = time.time()
        sl = crabd.StatusLineReader()
        sid = "22222222-0000-0000-0000-000000000002"
        sl.ingest(statusline_doc(session_id=sid,
                                 context_window={"total_input_tokens": 123456,
                                                 "current_usage": {"input_tokens": 1}}), now)
        builder = self.make_builder(statusline=sl)
        self._session_with_transcript_context(builder, sid, now - 20)
        row = self._row(builder.build(now=now), sid)
        self.assertEqual(row["contextTokens"], 123456)      # statusline, not the 90010 transcript
        self.assertEqual(row["contextSource"], "statusline")

    def test_statusline_saying_unknown_overrides_the_transcript_to_null(self):
        # known=True, tokens=None: the status line read the live window as unknown (just
        # after a compaction). It must NOT fall back to the stale transcript number.
        now = time.time()
        sl = crabd.StatusLineReader()
        sid = "33333333-0000-0000-0000-000000000003"
        sl.ingest(statusline_doc(session_id=sid,
                                 context_window={"total_input_tokens": 0,
                                                 "current_usage": None}), now)
        builder = self.make_builder(statusline=sl)
        self._session_with_transcript_context(builder, sid, now - 20)
        row = self._row(builder.build(now=now), sid)
        self.assertIsNone(row["contextTokens"])
        self.assertIsNone(row["contextSource"])

    @staticmethod
    def _row(state, sid):
        return next(r for r in state["sessions"] if r["id"] == sid)


# ==================================================== endpoints over a real socket

class DataLaneEndpointTests(unittest.TestCase):
    """/v1/statusline, /v1/metrics, /v1/logs on a real crabd server - never DEFAULT_PORT."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        projects = Path(self._tmp.name) / "projects"
        projects.mkdir(parents=True)
        config = crabd.UserConfig(Path(self._tmp.name) / "config.json")
        self.statusline = crabd.StatusLineReader()
        holder = {}
        self.otlp = crabd.OtlpReceiver(
            on_event=lambda sid, text: holder["b"].note_session_event(sid, text))
        self.builder = crabd.StateBuilder(
            crabd.TranscriptStore(projects), crabd.HookTracker(), StubLimits(),
            time.time(), config, statusline=self.statusline, otlp=self.otlp)
        holder["b"] = self.builder
        crabd.Handler.builder = self.builder
        # Proven-reachable port + one reused connection - see _httpkeepalive.
        self.server, self.thread, self.port, self.client = start_test_server(
            lambda: crabd.CrabdServer(("127.0.0.1", 0), crabd.Handler))
        self.assertNotEqual(self.port, crabd.DEFAULT_PORT)
        self.addCleanup(self._stop)
        self.addCleanup(self.client.close)

    def _stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def post(self, path, body):
        raw = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
        reply = self.client.post(path, raw)
        return reply.status, reply.body

    def rebuilt(self, now=None):
        state = self.builder.build(now=now)
        with self.builder._lock:
            self.builder._state = state
        return state

    def test_statusline_post_is_204_and_reaches_limits_and_context(self):
        """The barrier is not decoration: /v1/statusline answers BEFORE it parses (so the
        operator's status line never waits on crabd), which means the 204 landing does not
        mean the document has been ingested. Asserting the side effect on the next line
        was asserting a race - it failed on a full-suite run 2026-08-27."""
        sid = "aaaaaaaa-0000-0000-0000-00000000000a"
        now = time.time()
        status, body = self.post("/v1/statusline", statusline_doc(
            session_id=sid, five=(23.5, 1738425600),
            context_window={"total_input_tokens": 77000,
                            "current_usage": {"input_tokens": 1}}))
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        quiesce(lambda: self.statusline.documents, 1)
        state = self.rebuilt(now=now)
        self.assertEqual(state["limits"]["source"], "statusline")
        self.assertEqual(state["limits"]["fiveHour"]["utilization"], 0.235)

    def test_metrics_post_is_204_and_reaches_burn_costUSD(self):
        now = time.time()
        status, _ = self.post("/v1/metrics",
                              cost_metrics([(0.0042, {"model": "m"}, int(now * 1e9))]))
        self.assertEqual(status, 204)
        quiesce(lambda: self.otlp.documents, 1)   # answers before it parses - see above
        state = self.rebuilt(now=now)
        self.assertEqual(state["burn"]["costUSD"], 0.0042)
        self.assertEqual(state["burn"]["costSource"], "otlp")

    def test_logs_post_is_204_and_lands_an_api_error_on_the_session_ring(self):
        # A served session for the error to attach to (note_session_event is scoped to
        # served rows, like ack). Seed a hook so the row exists.
        sid = "bbbbbbbb-0000-0000-0000-00000000000b"
        self.builder.hooks.record({"session_id": sid, "hook_event_name": "SessionStart",
                                   "cwd": "C:\\IT"})
        self.rebuilt()
        status, _ = self.post("/v1/logs", error_logs([
            {"event.name": "api_error", "session.id": sid, "status_code": 429,
             "attempt": 2}]))
        self.assertEqual(status, 204)
        quiesce(lambda: self.otlp.documents, 1)   # answers before it parses - see above
        state = self.rebuilt()
        row = next(r for r in state["sessions"] if r["id"] == sid)
        texts = [e["text"] for e in row["events"]]
        self.assertTrue(any("API error 429" in t for t in texts), texts)

    def test_malformed_statusline_and_otlp_are_204_and_dropped(self):
        for path, body in (("/v1/statusline", b"not json at all"),
                           ("/v1/statusline", json.dumps([1, 2, 3]).encode()),
                           ("/v1/metrics", b"{ broken"),
                           ("/v1/metrics", json.dumps({"resourceMetrics": "x"}).encode()),
                           ("/v1/logs", b"\x00\x01\x02"),
                           ("/v1/logs", json.dumps({"nope": True}).encode())):
            status, body_out = self.post(path, body)
            self.assertEqual(status, 204, (path, body))
            self.assertEqual(body_out, b"")
        # The barrier this test asserts a NEGATIVE across (2026-08-27, the treatment its
        # three siblings already got). All six endpoints answer BEFORE they parse, so the
        # 204s above prove only that the bodies were accepted - without a barrier the
        # assertions below can pass because nothing has looked at them YET, which is a
        # false pass, not a flake.
        #
        # One well-formed but INERT document per receiver does it: an empty object is a
        # dict, so it is counted, and it carries no rate_limits and no cost point - so
        # neither assertion below is moved by the barrier itself. Same connection, so
        # once the barrier is counted every malformed body ahead of it has certainly been
        # through the handler. (The statusline counter reaches 1: only dict bodies are
        # ingested, and the two malformed statusline bodies are not dicts. OTLP reaches
        # 3 - {"resourceMetrics":"x"} and {"nope":true} ARE dicts and count.)
        self.post("/v1/statusline", b"{}")
        self.post("/v1/metrics", b"{}")
        quiesce(lambda: self.statusline.documents, 1)
        quiesce(lambda: self.otlp.documents, 3)
        # And nothing leaked into the served document.
        state = self.rebuilt()
        self.assertEqual(state["limits"]["source"], "oauth")
        self.assertIsNone(state["burn"]["costUSD"])

    def test_an_oversized_otlp_body_is_204_and_dropped(self):
        big = json.dumps({"resourceMetrics": [{"pad": "x" * (crabd.OTLP_MAX_BODY + 10)}]})
        status, _ = self.post("/v1/metrics", big.encode())
        self.assertEqual(status, 204)
        # A GOOD document behind it is the barrier for a test that asserts something did
        # NOT happen: once that one is counted, the oversized one has certainly been
        # through the handler. Without it this can pass because nothing looked yet.
        self.post("/v1/metrics", cost_metrics([]))
        quiesce(lambda: self.otlp.documents, 1)
        self.assertIsNone(self.rebuilt()["burn"]["costUSD"])

    def test_the_new_routes_do_not_disturb_health_or_state(self):
        self.assertEqual(self.client.get("/v1/health").json()["version"], crabd.VERSION)


if __name__ == "__main__":
    unittest.main()


# ============================================ v0.28.0 statusline context_window_size

class StatusLineContextWindowSizeTests(unittest.TestCase):
    """`_context_window_size` + `context_window()`: the DENOMINATOR the status line
    states, kept beside the fill it already kept.

    Measured in the 2.1.250 binary's own schema text:
        "context_window_size": number,  // Context window size for current model
                                        // (e.g., 200000)

    ⚠ NEVER OBSERVED ARRIVING ON THIS HOST - /v1/health reads statuslineSeen 0
    (measured 2026-08-28), because an app-hosted session renders no status line. These
    are fixtures against the documented shape, not a recording of live traffic, and the
    two sources UNDER this one (the model marker, then the model catalog) are what
    actually serve the number here. Said plainly so nobody reads a green suite as
    evidence the status-line path is exercised in production.
    """

    def size_doc(self, size, session_id="s-size", total=15500):
        return statusline_doc(session_id=session_id,
                              context_window={"total_input_tokens": total,
                                              "current_usage": {"input_tokens": 8500},
                                              "context_window_size": size})

    def test_the_documented_shape_is_kept(self):
        self.assertEqual(crabd.StatusLineReader._context_window_size(
            {"total_input_tokens": 15500, "context_window_size": 200000}), 200000)

    def test_a_size_with_no_fill_yet_is_still_kept(self):
        """The size and the fill are independent. A session before its first API call
        has current_usage null - so contextTokens is unknown - and the window it is
        about to fill is known anyway."""
        block = {"total_input_tokens": 0, "current_usage": None,
                 "context_window_size": 1000000}
        self.assertIsNone(crabd.StatusLineReader._context_tokens(block))
        self.assertEqual(crabd.StatusLineReader._context_window_size(block), 1000000)

    def test_an_absent_or_unusable_size_is_none_never_zero(self):
        for block in (None, {}, "nope", {"context_window_size": None},
                      {"context_window_size": True}, {"context_window_size": 0},
                      {"context_window_size": -5}, {"context_window_size": "200000"},
                      {"context_window_size": float("nan")},
                      {"context_window_size": 1e309}):
            self.assertIsNone(crabd.StatusLineReader._context_window_size(block), block)

    def test_ingest_keeps_the_size_beside_the_tokens(self):
        r = crabd.StatusLineReader()
        now = time.time()
        self.assertTrue(r.ingest(self.size_doc(200000), now))
        self.assertEqual(r.context("s-size", now), (True, 15500))
        self.assertEqual(r.context_window("s-size", now), 200000)

    def test_an_unknown_session_has_no_window(self):
        r = crabd.StatusLineReader()
        self.assertIsNone(r.context_window("never-heard-of-it", time.time()))

    def test_a_document_without_a_size_leaves_the_window_unknown(self):
        """And it must not raise or invent - the pre-0.28.0 document shape is still the
        one every older CLI posts."""
        r = crabd.StatusLineReader()
        now = time.time()
        r.ingest(statusline_doc(session_id="s-nosize",
                                context_window={"total_input_tokens": 10,
                                                "current_usage": {"input_tokens": 10}}),
                 now)
        self.assertEqual(r.context("s-nosize", now), (True, 10))
        self.assertIsNone(r.context_window("s-nosize", now))

    def test_the_window_expires_on_the_same_horizon_as_the_tokens(self):
        r = crabd.StatusLineReader()
        r.ingest(self.size_doc(200000, session_id="s-exp"), 1000.0)
        keep = 1000.0 + crabd.STATUSLINE_SESSION_KEEP_SEC
        self.assertEqual(r.context_window("s-exp", keep), 200000)
        self.assertIsNone(r.context_window("s-exp", keep + 1))

    def test_the_window_takes_the_same_not_before_contest_as_the_tokens(self):
        """CD-36's rule, applied to the size: a retained row can name a model the
        session has since left, and the sources beneath it would have had it right."""
        r = crabd.StatusLineReader()
        now = time.time()
        r.ingest(self.size_doc(200000, session_id="s-old"), now - 7000)
        self.assertEqual(r.context_window("s-old", now), 200000)   # no floor -> served
        self.assertIsNone(r.context_window("s-old", now, not_before=now - 60))

    def test_a_newer_document_replaces_the_size(self):
        r = crabd.StatusLineReader()
        now = time.time()
        r.ingest(self.size_doc(200000, session_id="s-swap"), now - 10)
        r.ingest(self.size_doc(1000000, session_id="s-swap"), now)
        self.assertEqual(r.context_window("s-swap", now), 1000000)

    def test_ingest_never_raises_on_a_hostile_context_window_block(self):
        """The endpoint answers 204 before this runs, so a traceback here is a failure
        nobody sees until the feed goes quiet."""
        r = crabd.StatusLineReader()
        now = time.time()
        for block in ({"context_window_size": 1e309}, {"context_window_size": [1]},
                      {"context_window_size": {"n": 1}}, {"context_window_size": "x"}):
            r.ingest(statusline_doc(session_id="s-hostile", context_window=block), now)
            self.assertIsNone(r.context_window("s-hostile", now), block)


class ContextWindowWiringTests(BuilderHarness):
    """The served row, through a builder with the status line wired."""

    def test_a_statusline_size_reaches_the_served_row(self):
        now = time.time()
        sl = crabd.StatusLineReader()
        sid = "44444444-0000-0000-0000-000000000004"
        sl.ingest(statusline_doc(session_id=sid,
                                 context_window={"total_input_tokens": 123456,
                                                 "current_usage": {"input_tokens": 1},
                                                 "context_window_size": 200000}), now)
        builder = self.make_builder(statusline=sl)
        row = {"contextWindowTokens": builder._context_window(
            sid, {"model": "claude-fable-5", "context_tokens": None,
                  "context_ts": now}, now)}
        self.assertEqual(row["contextWindowTokens"], 200000)

    def test_no_statusline_and_no_catalog_is_null_not_a_guess(self):
        now = time.time()
        builder = self.make_builder()
        self.assertIsNone(builder._context_window(
            "nobody", {"model": "claude-opus-5", "context_tokens": None,
                       "context_ts": now}, now))
