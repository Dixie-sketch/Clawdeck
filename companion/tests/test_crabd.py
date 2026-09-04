"""Offline unit tests for crabd. `python -m unittest discover companion/tests`

Everything here runs against fixture transcripts in a temp dir and a stubbed limits
reader; no network, and the real ~/.claude is never read.
"""

import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# ...and this directory, so `_httpkeepalive` imports whether the suite is run
# by `unittest discover companion/tests` (which adds it) or by module path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crabd  # noqa: E402
from _httpkeepalive import quiesce, settle, start_test_server  # noqa: E402


# --------------------------------------------------------------- module isolation

_MODULE_TMP = None


def setUpModule():
    """HARD ISOLATION for the three module globals that name REAL files under ~.

    LIMITS_CACHE_FILE is written by LimitsReader on every successful fetch, and several
    tests build a real reader with a stubbed `_fetch`. Patching it per-test is one
    forgotten line away from writing the operator's live store - and that is not
    hypothetical: `test_429_backoff_serves_last_good_and_stops_calling` did exactly
    that, leaving production's ~/.sidecrab/limits-cache.json holding this file's
    fixture payload dated at=1000.0 (measured 2026-08-26). With last-good poisoned and
    the usage endpoint 429ing, the panel showed em-dashes.

    HISTORY_FILE (v0.7.0) is patched here for exactly that lesson, before any test
    could learn it the expensive way: a HookTracker with a HistoryLog attached writes on
    every hook, and a suite that fired fixture hooks at the real file would hand the
    operator's next crabd a replayed day that never happened.

    CREDENTIALS_FILE (v0.28.0) is patched for the same reason one move earlier: it is
    the operator's live OAuth token, and ModelCatalog reads it and then makes a NETWORK
    request. Pointed at a path that does not exist, so a catalog built without an
    injected credentials_file stops at "no credentials" and never reaches the wire -
    this suite must not depend on the operator being logged out, or on the network
    being down, to stay offline.

    Patched here, at MODULE scope, so the guarantee does not depend on any test class
    remembering: nothing in this suite can reach any of the four real files.
    """
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    root = Path(_MODULE_TMP.name)
    setUpModule.originals = (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
                             crabd.HISTORY_FILE, crabd.CREDENTIALS_FILE)
    crabd.LIMITS_CACHE_FILE = root / "limits-cache.json"
    crabd.USER_CONFIG_FILE = root / "config.json"
    crabd.HISTORY_FILE = root / "history.jsonl"
    crabd.CREDENTIALS_FILE = root / "no-such-credentials.json"


def tearDownModule():
    (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
     crabd.HISTORY_FILE, crabd.CREDENTIALS_FILE) = setUpModule.originals
    # SELF-ISOLATING (2026-08-27): the fixtures leave a builder on the Handler CLASS, and
    # a builder outliving its module points at a TemporaryDirectory that is about to be
    # deleted. unittest happens to run these modules one after another; pytest gives no
    # such guarantee, and a stale class attribute is the kind of leak that shows up as
    # another module's test failing. Cleared here so the module hands back what it found.
    crabd.Handler.builder = None
    _MODULE_TMP.cleanup()


MOCK_LIMITS = {
    "available": True, "note": None,
    "fiveHour": {"utilization": 0.42, "resetsAt": "2026-08-26T21:00:00Z"},
    "weekly": {"utilization": 0.18, "resetsAt": "2026-08-30T00:00:00Z"},
    "extra": [], "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x",
}


class StubLimits:
    def __init__(self, payload=MOCK_LIMITS):
        self.payload = payload

    def get(self, now, force=False):
        return self.payload


class StubHost:
    """A HostSampler stand-in with no kernel counters behind it (v0.22.0).

    The served `host` block is FIXED, so the contract-shape assertions do not move with
    whatever the real machine happens to be doing while the suite runs - and so they
    give the same answer off Windows, where the real sampler serves no block at all.
    The arithmetic those numbers come from is proven separately, against scripted
    FILETIMEs, in HostSamplerCpuMathTests.
    """
    BLOCK = {"cpuPct": 34.2, "memPct": 58.1, "memUsedGB": 18.6, "memTotalGB": 32.0}

    def sample(self):
        return dict(self.BLOCK)      # a copy: a caller must not edit the next sample


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def assistant_line(request_id, ts, output=100, model="claude-fable-5",
                   speed="standard", cwd="C:\\IT", cache_read=7, cache_create=9,
                   inp=2):
    return {
        "type": "assistant", "requestId": request_id, "timestamp": iso(ts),
        "cwd": cwd, "gitBranch": "master", "effort": "max", "isSidechain": False,
        "message": {
            "role": "assistant", "model": model,
            "usage": {
                "input_tokens": inp, "output_tokens": output,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
                "speed": speed,
            },
        },
    }


def user_line(text, ts, cwd="C:\\IT"):
    return {"type": "user", "timestamp": iso(ts), "cwd": cwd, "isSidechain": False,
            "promptSource": "sdk", "message": {"role": "user", "content": text}}


def tool_result_line(ts, payload_size=40000, cwd="C:\\IT"):
    return {"type": "user", "timestamp": iso(ts), "cwd": cwd, "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "x" * payload_size}]}}


def write_jsonl(path, objects, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for obj in objects:
            fh.write(json.dumps(obj) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def ask_line(ts, questions, cwd="C:\\IT", tool_id="toolu_ask1"):
    """Measured 2026-08-26 across 227 real AskUserQuestion blocks in
    ~/.claude/projects/**: the tool_use input has exactly one key, `questions`, a list
    of {question, header, multiSelect, options[{label, description}]}."""
    line = assistant_line("req_ask", ts, cwd=cwd)
    line["message"]["content"] = [
        {"type": "tool_use", "id": tool_id, "name": "AskUserQuestion", "input": {
            "questions": [
                {"question": q, "header": "Pick", "multiSelect": False,
                 "options": [{"label": "Yes", "description": "do it"},
                             {"label": "No", "description": "don't"}]}
                for q in questions
            ]}}]
    return line


def assistant_text_line(request_id, ts, text, cwd="C:\\IT"):
    line = assistant_line(request_id, ts, cwd=cwd)
    line["message"]["content"] = [{"type": "text", "text": text}]
    return line


def agent_launch_lines(ts, description, agent_id, tool_id="toolu_agent1"):
    """The Agent tool_use that names a subagent, and the tool_result that ties that
    name to the subagent's transcript file (agent-<agentId>.jsonl). Measured shape."""
    launch = assistant_line("req_agent", ts)
    launch["message"]["content"] = [
        {"type": "tool_use", "id": tool_id, "name": "Agent",
         "input": {"description": description, "subagent_type": "general-purpose",
                   "prompt": "a very long brief that must never become the label"}}]
    result = {"type": "user", "timestamp": iso(ts + 1), "cwd": "C:\\IT",
              "message": {"role": "user", "content": [
                  {"tool_use_id": tool_id, "type": "tool_result", "content": [
                      {"type": "text",
                       "text": "Async agent launched successfully.\n"
                               f"agentId: {agent_id} (internal ID - do not mention)"}]}]}}
    return [launch, result]


class TempProjects(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.projects = Path(self._tmp.name) / "projects"
        self.projects.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)
        # Never read or create the real ~/.sidecrab/config.json from a test run.
        self.config_path = Path(self._tmp.name) / "config.json"
        original = crabd.USER_CONFIG_FILE
        crabd.USER_CONFIG_FILE = self.config_path
        self.addCleanup(lambda: setattr(crabd, "USER_CONFIG_FILE", original))

    def session_path(self, session_id, project="C--IT"):
        return self.projects / project / f"{session_id}.jsonl"

    def build(self, now=None, hooks=None):
        builder = crabd.StateBuilder(
            crabd.TranscriptStore(self.projects), hooks or crabd.HookTracker(),
            StubLimits(), time.time())
        return builder, builder.build(now=now)


# ------------------------------------------------------------------ transcript facts

class TitlePrecedenceTests(TempProjects):
    def _facts(self, objects):
        path = self.session_path("s-title")
        write_jsonl(path, objects)
        facts = crabd.FileFacts(path, "s-title", False)
        facts.refresh()
        return facts

    def test_custom_title_wins_over_everything(self):
        now = time.time()
        facts = self._facts([
            user_line("the very first prompt", now),
            {"type": "ai-title", "aiTitle": "an ai title"},
            {"type": "last-prompt", "lastPrompt": "a later prompt"},
            {"type": "custom-title", "customTitle": "the operator's own title"},
        ])
        self.assertEqual(facts.title(), "the operator's own title")

    def test_ai_title_wins_over_prompts(self):
        now = time.time()
        facts = self._facts([
            user_line("the very first prompt", now),
            {"type": "last-prompt", "lastPrompt": "a later prompt"},
            {"type": "ai-title", "aiTitle": "an ai title"},
        ])
        self.assertEqual(facts.title(), "an ai title")

    def test_first_prompt_excerpt_when_no_titles(self):
        now = time.time()
        facts = self._facts([
            user_line("the very first prompt", now),
            user_line("a second prompt", now + 1),
        ])
        self.assertEqual(facts.title(), "the very first prompt")

    def test_tool_results_are_never_mistaken_for_a_prompt(self):
        now = time.time()
        facts = self._facts([
            tool_result_line(now, payload_size=200),
            user_line("the real first prompt", now + 1),
        ])
        self.assertEqual(facts.title(), "the real first prompt")

    def test_last_prompt_is_the_final_fallback(self):
        facts = self._facts([{"type": "last-prompt", "lastPrompt": "only a last prompt"}])
        self.assertEqual(facts.title(), "only a last prompt")

    def test_long_title_is_trimmed(self):
        facts = self._facts([{"type": "custom-title", "customTitle": "z" * 400}])
        self.assertLessEqual(len(facts.title()), crabd.TITLE_MAX)


class CwdTitleTests(unittest.TestCase):
    """The v0.11.0 last-resort tier: a session with no title of any kind is named after
    the tail of its cwd. Pure function, so the matrix is cheap and exhaustive."""

    def test_plain_tail_is_the_last_component(self):
        for cwd, expected in (
                ("C:\\Users\\x\\Documents\\budget-tracker", "budget-tracker"),
                ("C:\\Dev\\sidecrab", "sidecrab"),
                ("/home/joe/budget-tracker", "budget-tracker"),
                ("C:/Dev/sidecrab", "sidecrab"),
        ):
            with self.subTest(cwd=cwd):
                self.assertEqual(crabd._cwd_title(cwd), expected)

    def test_a_generic_tail_takes_its_parent_too(self):
        for cwd, expected in (
                ("C:\\Dev\\acme\\src", "acme/src"),
                ("C:\\Dev\\acme\\main", "acme/main"),
                ("C:/Dev/acme/app", "acme/app"),
                ("/home/joe/work", "joe/work"),
                ("C:\\Dev\\acme\\tmp", "acme/tmp"),
        ):
            with self.subTest(cwd=cwd):
                self.assertEqual(crabd._cwd_title(cwd), expected)

    def test_generic_match_ignores_case(self):
        self.assertEqual(crabd._cwd_title("C:\\Dev\\acme\\SRC"), "acme/SRC")

    def test_a_generic_tail_with_no_parent_stays_a_plain_tail(self):
        """C:\\src has nothing to join to - serving "/src" or dropping the row would
        both be worse than the bare word."""
        self.assertEqual(crabd._cwd_title("C:\\src"), "src")

    def test_a_trailing_separator_does_not_blank_the_tail(self):
        self.assertEqual(crabd._cwd_title("C:\\Dev\\sidecrab\\"), "sidecrab")

    def test_unc_paths_read_like_any_other(self):
        self.assertEqual(crabd._cwd_title("\\\\server\\share\\proj"), "proj")
        self.assertEqual(crabd._cwd_title("\\\\server\\share\\acme\\src"), "acme/src")

    def test_roots_and_non_paths_yield_nothing(self):
        for cwd in ("C:\\", "C:", "/", "\\\\server\\share", "", "   ", None, 5, []):
            with self.subTest(cwd=cwd):
                self.assertIsNone(crabd._cwd_title(cwd))

    def test_a_very_long_tail_is_trimmed_like_any_title(self):
        title = crabd._cwd_title("C:\\Dev\\" + "z" * 400)
        self.assertLessEqual(len(title), crabd.TITLE_MAX)


class TitleSourceTests(TempProjects):
    """`titleSource` names the tier that produced `title`. It must be read off the SAME
    precedence title() uses, or the panel styles a title by a tier that did not make it."""

    def _facts(self, objects):
        path = self.session_path("s-source")
        write_jsonl(path, objects)
        facts = crabd.FileFacts(path, "s-source", False)
        facts.refresh()
        return facts

    def test_custom(self):
        facts = self._facts([{"type": "custom-title", "customTitle": "operator's own"},
                             {"type": "ai-title", "aiTitle": "an ai title"}])
        self.assertEqual(facts.title_source(), "custom")

    def test_ai(self):
        facts = self._facts([user_line("a prompt", time.time()),
                             {"type": "ai-title", "aiTitle": "an ai title"}])
        self.assertEqual(facts.title_source(), "ai")

    def test_first_prompt_reports_prompt(self):
        facts = self._facts([user_line("the very first prompt", time.time())])
        self.assertEqual(facts.title_source(), "prompt")

    def test_last_prompt_also_reports_prompt(self):
        facts = self._facts([{"type": "last-prompt", "lastPrompt": "only a last prompt"}])
        self.assertEqual(facts.title_source(), "prompt")

    def test_nothing_at_all_reports_none(self):
        facts = self._facts([assistant_line("req_1", time.time())])
        self.assertIsNone(facts.title())
        self.assertIsNone(facts.title_source())

    def test_source_never_disagrees_with_title(self):
        """Mutation guard: every tier combination is checked BOTH ways at once, so a
        future edit to one method and not the other fails here rather than on glass."""
        now = time.time()
        custom = {"type": "custom-title", "customTitle": "operator's own"}
        ai = {"type": "ai-title", "aiTitle": "an ai title"}
        last = {"type": "last-prompt", "lastPrompt": "a later prompt"}
        first = user_line("the very first prompt", now)
        for lines, title, source in (
                ([assistant_line("r", now)], None, None),
                ([last], "a later prompt", "prompt"),
                ([first, last], "the very first prompt", "prompt"),
                ([first, last, ai], "an ai title", "ai"),
                ([first, last, ai, custom], "operator's own", "custom"),
                ([custom], "operator's own", "custom"),
        ):
            with self.subTest(source=source):
                facts = self._facts(lines)
                self.assertEqual(facts.title(), title)
                self.assertEqual(facts.title_source(), source)


class ServedTitleFallbackTests(TempProjects):
    """The served row: which tier wins, what titleSource carries, and that the cwd tier
    fires ONLY when the existing chain gave nothing."""

    def _row(self, lines, session_id="s-fallback", hooks=None, mtime_offset=-20):
        now = time.time()
        if lines is not None:
            write_jsonl(self.session_path(session_id), lines, mtime=now + mtime_offset)
        _builder, state = self.build(hooks=hooks)
        rows = [r for r in state["sessions"] if r["id"] == session_id]
        self.assertEqual(len(rows), 1, state["sessions"])
        return rows[0]

    def _hooks(self, session_id, **extra):
        hooks = crabd.HookTracker()
        hooks.record({"session_id": session_id, "hook_event_name": "SessionStart",
                      **extra})
        return hooks

    def test_a_titleless_session_is_named_after_its_cwd(self):
        row = self._row([assistant_line("req_1", time.time(),
                                        cwd="C:\\Users\\x\\Documents\\budget-tracker")])
        self.assertEqual(row["title"], "budget-tracker")
        self.assertEqual(row["titleSource"], "cwd")

    def test_a_generic_cwd_tail_serves_two_components(self):
        row = self._row([assistant_line("req_1", time.time(), cwd="C:\\Dev\\acme\\src")])
        self.assertEqual(row["title"], "acme/src")
        self.assertEqual(row["titleSource"], "cwd")

    def test_the_cwd_tier_never_outranks_a_real_title(self):
        """The bug this guards: a cwd tier that runs first would rename every session
        on the panel after its directory."""
        now = time.time()
        for extra, title, source in (
                ({"type": "custom-title", "customTitle": "operator's own"},
                 "operator's own", "custom"),
                ({"type": "ai-title", "aiTitle": "an ai title"}, "an ai title", "ai"),
                ({"type": "last-prompt", "lastPrompt": "a later prompt"},
                 "a later prompt", "prompt"),
        ):
            with self.subTest(source=source):
                row = self._row([assistant_line("req_1", now, cwd="C:\\Dev\\acme"), extra],
                                session_id=f"s-precede-{source}")
                self.assertEqual(row["title"], title)
                self.assertEqual(row["titleSource"], source)

    def test_the_cwd_can_come_from_the_hook_when_the_transcript_has_none(self):
        """A session that has fired SessionStart but written no cwd line is exactly the
        row with no title, so the tier must read the RESOLVED cwd, not facts.last_cwd."""
        session_id = "s-hookcwd"
        hooks = self._hooks(session_id, cwd="C:\\Users\\x\\Documents\\budget-tracker")
        row = self._row(None, session_id=session_id, hooks=hooks)
        self.assertEqual(row["title"], "budget-tracker")
        self.assertEqual(row["titleSource"], "cwd")

    def test_no_cwd_anywhere_is_the_placeholder_with_a_null_source(self):
        """Measured on the live instance 2026-08-26: bare "session" rows are hook POSTs
        carrying no cwd. They stay "session" - and titleSource stays null, so the widget
        cannot style a placeholder as a derived title."""
        session_id = "s-nocwd"
        row = self._row(None, session_id=session_id, hooks=self._hooks(session_id))
        self.assertEqual(row["title"], "session")
        self.assertIsNone(row["titleSource"])

    def test_a_cwd_with_no_component_falls_back_to_the_id_stub_as_before(self):
        session_id = "aaaaaaaa-1111-2222-3333-444444444444"
        row = self._row([assistant_line("req_1", time.time(), cwd="C:\\")],
                        session_id=session_id)
        self.assertEqual(row["title"], session_id[:8])
        self.assertIsNone(row["titleSource"])

    def test_titleSource_is_present_on_every_served_row(self):
        now = time.time()
        write_jsonl(self.session_path("s-titled"),
                    [user_line("a typed prompt", now)], mtime=now - 5)
        write_jsonl(self.session_path("s-derived"),
                    [assistant_line("req_1", now, cwd="C:\\Dev\\acme")], mtime=now - 5)
        _builder, state = self.build()
        self.assertEqual(len(state["sessions"]), 2)
        for row in state["sessions"]:
            self.assertIn("titleSource", row)
        by_id = {r["id"]: r for r in state["sessions"]}
        self.assertEqual(by_id["s-titled"]["titleSource"], "prompt")
        self.assertEqual(by_id["s-derived"]["titleSource"], "cwd")


class RequestDedupeTests(TempProjects):
    def test_streamed_repeats_of_one_request_count_once(self):
        now = time.time()
        path = self.session_path("s-dedupe")
        # Measured: assistant usage is re-emitted per streamed line with the SAME
        # requestId. Summing lines instead of requests multiplies burn ~4x.
        write_jsonl(path, [assistant_line("req_A", now, output=500)] * 4 +
                          [assistant_line("req_B", now, output=250)] * 3)
        facts = crabd.FileFacts(path, "s-dedupe", False)
        facts.refresh()
        self.assertEqual(len(facts.requests), 2)
        self.assertEqual(sum(r[1] for r in facts.requests.values()), 750)

    def test_incremental_append_keeps_totals_correct(self):
        now = time.time()
        path = self.session_path("s-append")
        write_jsonl(path, [assistant_line("req_A", now, output=100)])
        facts = crabd.FileFacts(path, "s-append", False)
        facts.refresh()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(assistant_line("req_A", now, output=100)) + "\n")
            fh.write(json.dumps(assistant_line("req_B", now, output=40)) + "\n")
        os.utime(path, (now + 5, now + 5))
        facts.refresh()
        self.assertEqual(len(facts.requests), 2)
        self.assertEqual(sum(r[1] for r in facts.requests.values()), 140)

    def test_partial_trailing_line_is_completed_on_the_next_pass(self):
        now = time.time()
        path = self.session_path("s-partial")
        path.parent.mkdir(parents=True, exist_ok=True)
        record = json.dumps(assistant_line("req_A", now, output=99))
        path.write_text(record[:20], encoding="utf-8")  # writer caught mid-line
        facts = crabd.FileFacts(path, "s-partial", False)
        facts.refresh()
        self.assertEqual(len(facts.requests), 0)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(record[20:] + "\n")
        os.utime(path, (now + 5, now + 5))
        facts.refresh()
        self.assertEqual(sum(r[1] for r in facts.requests.values()), 99)

    def test_truncated_file_is_reparsed_from_zero(self):
        now = time.time()
        path = self.session_path("s-trunc")
        write_jsonl(path, [assistant_line("req_A", now, output=100)])
        facts = crabd.FileFacts(path, "s-trunc", False)
        facts.refresh()
        write_jsonl(path, [assistant_line("req_C", now, output=7)], mtime=now + 9)
        facts.refresh()
        self.assertEqual(list(facts.requests), ["req_C"])

    def test_huge_tool_result_lines_are_skipped_without_losing_usage(self):
        now = time.time()
        path = self.session_path("s-big")
        write_jsonl(path, [
            tool_result_line(now, payload_size=crabd.BIG_LINE_BYTES * 2),
            assistant_line("req_A", now, output=11),
        ])
        facts = crabd.FileFacts(path, "s-big", False)
        facts.refresh()
        self.assertEqual(sum(r[1] for r in facts.requests.values()), 11)


# ------------------------- v0.20.0: /v1/state never 500s for a data-shape reason

# The ten record shapes MEASURED on 2026-08-27 as crashing the pre-v0.20.0 parser. Each
# aborted store.scan() and therefore the whole build, so ONE unreadable line under
# ~/.claude/projects took every session's card down with it - which is what the cold-start
# 500 looked like from the operator's side. Kept as data, one subTest each, so a
# regression names the shape that came back.
POISON_RECORDS = [
    ("message is a string",
     {"type": "assistant", "requestId": "p1", "message": "a bare string"}),
    ("message is a list",
     {"type": "assistant", "requestId": "p2", "message": ["a", "list"]}),
    ("usage is a string",
     {"type": "assistant", "requestId": "p3",
      "message": {"role": "assistant", "usage": "nope"}}),
    ("usage is a list",
     {"type": "assistant", "requestId": "p4",
      "message": {"role": "assistant", "usage": [1, 2]}}),
    ("a counter is a dict",
     {"type": "assistant", "requestId": "p5", "message": {
         "usage": {"input_tokens": {"a": 1}, "output_tokens": 3}}}),
    ("a counter is a word",
     {"type": "assistant", "requestId": "p6",
      "message": {"usage": {"output_tokens": "twelve"}}}),
    ("a counter is a list",
     {"type": "assistant", "requestId": "p7",
      "message": {"usage": {"output_tokens": [5]}}}),
    ("a counter is Infinity",
     {"type": "assistant", "requestId": "p8",
      "message": {"usage": {"output_tokens": float("inf")}}}),
    ("a counter is NaN",
     {"type": "assistant", "requestId": "p9",
      "message": {"usage": {"output_tokens": float("nan")}}}),
    ("a user message is a string",
     {"type": "user", "message": "bare"}),
]


class ExplodingRecord(dict):
    """A record that defeats every isinstance guard by raising when it is READ. Stands in
    for the shape nobody has met yet - the reason _consume is total and not just careful."""

    def get(self, *args, **kwargs):
        raise RuntimeError("a shape from the future")


class PoisonedRecordTests(TempProjects):
    """A record crabd cannot read is SKIPPED. It is never a 500, and it never costs the
    rest of the file, the rest of the scan, or any other session its card."""

    def facts_for(self, objects, session_id="s-poison"):
        path = self.session_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for obj in objects:
                fh.write(json.dumps(obj) + "\n")
        facts = crabd.FileFacts(path, session_id, False)
        facts.refresh()
        return facts

    def test_every_measured_poison_shape_is_HANDLED_not_merely_caught(self):
        """`skipped == 0` is the whole point of this test, and it is what separates the
        guards from the backstop. A shape the guards cover is READ - it simply carries no
        usable counter. If the isinstance guards were removed, the total wrapper would
        still keep the feed alive, but every one of these would become a swallowed
        exception - and a parser whose ordinary path is its error path is one that has
        stopped telling anyone when something is genuinely new."""
        for name, record in POISON_RECORDS:
            with self.subTest(shape=name):
                facts = crabd.FileFacts(self.session_path("x"), "x", False)
                facts._consume(json.dumps(record).encode("utf-8"))
                self.assertEqual(facts.skipped, 0)

    def test_a_poisoned_record_does_not_cost_the_records_around_it(self):
        """THE honest-failure property, stated as a number: the good records on either
        side of the whole poison set are still parsed, and their burn is exact."""
        now = time.time()
        objects = [assistant_line("req_before", now - 10, output=100)]
        objects += [record for _, record in POISON_RECORDS]
        objects += [assistant_line("req_after", now - 5, output=40)]
        facts = self.facts_for(objects)
        self.assertIn("req_before", facts.requests)
        self.assertIn("req_after", facts.requests)
        self.assertEqual(facts.requests["req_before"][1] + facts.requests["req_after"][1],
                         140)

    def test_an_unreadable_counter_reads_as_zero_not_as_a_crash(self):
        """A shape that is READABLE as a record but not as a number stays a record. It
        under-reports burn by that counter, which is the honest trade against serving
        nothing at all."""
        now = time.time()
        line = assistant_line("req_odd", now, output=100)
        line["message"]["usage"]["output_tokens"] = "twelve"
        line["message"]["usage"]["input_tokens"] = float("inf")
        facts = self.facts_for([line])
        self.assertEqual(facts.requests["req_odd"][1], 0)
        self.assertEqual(facts.requests["req_odd"][2], 0)

    def test_an_unreadable_record_is_counted_and_logged_once(self):
        """A swallowed exception that says nothing anywhere is the failure mode the rule
        exists to forbid. It is counted every time and printed the first time."""
        crabd._LOG_ONCE_SEEN.discard(crabd.TRANSCRIPT_SKIP_LOG_KEY)
        self.addCleanup(crabd._LOG_ONCE_SEEN.discard, crabd.TRANSCRIPT_SKIP_LOG_KEY)
        facts = crabd.FileFacts(self.session_path("x"), "x", False)
        stderr, sys.stderr = sys.stderr, io.StringIO()
        original = crabd.json.loads
        crabd.json.loads = lambda *a, **k: ExplodingRecord()
        try:
            for _ in range(3):
                facts._consume(b'{"type":"assistant"}')
            printed = sys.stderr.getvalue()
        finally:
            crabd.json.loads = original
            sys.stderr = stderr
        self.assertEqual(facts.skipped, 3)
        self.assertEqual(printed.count("skipped an unreadable transcript record"), 1)

    def test_one_poisoned_session_does_not_take_the_others_down(self):
        """The blast radius, measured through build(): before v0.20.0 this raised out of
        scan() and the OTHER session's card went with it."""
        now = time.time()
        write_jsonl(self.session_path("s-good"),
                    [assistant_line("req_good", now - 30, output=77)], mtime=now - 30)
        self.facts_for([record for _, record in POISON_RECORDS], session_id="s-bad")
        os.utime(self.session_path("s-bad"), (now - 20, now - 20))
        _, state = self.build(now=now)
        good = next((r for r in state["sessions"] if r["id"] == "s-good"), None)
        self.assertIsNotNone(good)
        self.assertEqual(good["todayOutputTokens"], 77)

    def test_a_shape_nobody_has_met_yet_is_still_only_a_skipped_line(self):
        """MUTATION CHECK for the total wrapper: a record that defeats every isinstance
        guard by exploding when it is READ must still cost one line and no more."""
        facts = crabd.FileFacts(self.session_path("x"), "x", False)
        original, stderr = crabd.json.loads, sys.stderr
        crabd.json.loads = lambda *a, **k: ExplodingRecord()
        sys.stderr = io.StringIO()
        try:
            facts._consume(b'{"type":"assistant"}')
        finally:
            crabd.json.loads = original
            sys.stderr = stderr
        self.assertEqual(facts.skipped, 1)


class StateSerializationTests(unittest.TestCase):
    """dump_state - the ONE serializer for a served document."""

    def setUp(self):
        # The sanitising pass logs once per crabd lifetime; these tests trip it on
        # purpose, so the line is captured rather than left on the suite's stderr.
        self._stderr, sys.stderr = sys.stderr, io.StringIO()
        self.addCleanup(lambda: setattr(sys, "stderr", self._stderr))

    def printed(self):
        return sys.stderr.getvalue()

    def test_an_ordinary_document_is_plain_json(self):
        payload = {"schema": 5, "sessions": [{"id": "a", "acked": False}], "x": None}
        self.assertEqual(json.loads(crabd.dump_state(payload)), payload)

    def test_a_poisoned_document_still_serves(self):
        """The requirement in one test: a value JSON cannot express costs THAT VALUE and
        nothing else. Every other key is still on the wire."""
        crabd._LOG_ONCE_SEEN.discard(crabd.STATE_SERIALIZE_LOG_KEY)
        self.addCleanup(crabd._LOG_ONCE_SEEN.discard, crabd.STATE_SERIALIZE_LOG_KEY)
        payload = {"schema": 5, "generatedAt": "2026-08-27T00:00:00Z",
                   "sessions": [{"id": "a", "contextTokens": 12,
                                 "lock": threading.Lock()}],
                   "burn": {"today": {"outputTokens": 5}}}
        served = json.loads(crabd.dump_state(payload))
        printed = self.printed()
        self.assertEqual(served["schema"], 5)
        self.assertEqual(served["burn"]["today"]["outputTokens"], 5)
        self.assertEqual(served["sessions"][0]["contextTokens"], 12)
        self.assertIsInstance(served["sessions"][0]["lock"], str)
        self.assertIn("JSON cannot express", printed)

    def test_a_non_finite_number_is_null_not_the_bare_NaN_token(self):
        """json.dumps' DEFAULT is to emit bare NaN / Infinity, which is not JSON - the
        widget's JSON.parse dead-feeds the panel on it, silently. null is the honest
        answer and every parser downstream accepts it."""
        body = crabd.dump_state({"limits": {"fiveHour": {"utilization": float("nan"),
                                                   "resetsAt": None},
                                      "weekly": {"utilization": float("inf")}}})
        self.assertNotIn(b"NaN", body)
        self.assertNotIn(b"Infinity", body)
        served = json.loads(body)
        self.assertIsNone(served["limits"]["fiveHour"]["utilization"])
        self.assertIsNone(served["limits"]["weekly"]["utilization"])

    def test_a_self_referential_document_cannot_recurse_the_daemon_to_death(self):
        payload = {"schema": 5}
        payload["self"] = payload
        self.assertEqual(json.loads(crabd.dump_state(payload))["schema"], 5)


class FileFactsConcurrencyTests(TempProjects):
    """CRB-F2's SECOND HALF. The store lock made `files` safe to iterate; it never
    covered the mutable state INSIDE a FileFacts, which build()'s session loop reads
    while another thread's scan() is writing it. PROVEN at the object level before the
    fix: "dictionary changed size during iteration" in under a second."""

    def test_reading_the_usage_records_while_a_refresh_writes_them_is_safe(self):
        facts = crabd.FileFacts(self.session_path("s-race"), "s-race", False)
        for i in range(4000):
            facts.requests[f"seed{i}"] = (1.0, 1, 1, 1, 1, "m")
        stop = threading.Event()
        errors = []

        def read():
            while not stop.is_set():
                try:
                    for _rid, _rec in facts.usage_records().items():
                        pass
                    {}.update(facts.labels())
                except RuntimeError as exc:      # what the unguarded read raised
                    errors.append(repr(exc))
                    stop.set()

        def write():
            k = 0
            while not stop.is_set():
                with facts._lock:                # exactly what refresh() holds
                    facts.requests[f"n{k}"] = (1.0, 1, 1, 1, 1, "m")
                    facts.agent_labels[f"a{k}"] = "label"
                    if k % 300 == 299:
                        facts.reset()            # the truncated-file path
                        for i in range(2000):
                            facts.requests[f"seed{i}"] = (1.0, 1, 1, 1, 1, "m")
                k += 1

        threads = [threading.Thread(target=read), threading.Thread(target=read),
                   threading.Thread(target=write)]
        for thread in threads:
            thread.start()
        time.sleep(2.0)
        stop.set()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])

    def test_the_readers_hand_back_copies_not_the_live_dicts(self):
        """MUTATION CHECK. If either accessor ever returns the live dict again, the lock
        it takes buys nothing - the caller walks away holding the object the writer
        mutates."""
        facts = crabd.FileFacts(self.session_path("s-copy"), "s-copy", False)
        facts.requests["a"] = (1.0, 1, 1, 1, 1, "m")
        facts.agent_labels["x"] = "label"
        self.assertIsNot(facts.usage_records(), facts.requests)
        self.assertIsNot(facts.labels(), facts.agent_labels)
        facts.usage_records()["b"] = (2.0, 1, 1, 1, 1, "m")
        facts.labels()["y"] = "other"
        self.assertEqual(list(facts.requests), ["a"])
        self.assertEqual(list(facts.agent_labels), ["x"])


# -------------------------------------------------------------------- state machine

class HookTrackerTests(unittest.TestCase):
    def setUp(self):
        self.hooks = crabd.HookTracker()

    def send(self, event, session_id="s1", **extra):
        self.hooks.record({"session_id": session_id, "hook_event_name": event,
                           "cwd": "C:\\IT", **extra})

    def test_transitions(self):
        # SessionStart -> idle since v0.28.2: opening a session is not a turn (a click
        # into an old session put an amber WORKING card on the glass, 2026-09-01).
        for event, expected in (("SessionStart", "idle"),
                                ("UserPromptSubmit", "working"),
                                ("Notification", "needs_input"),
                                ("Stop", "done"),
                                ("SessionEnd", "gone")):
            self.send(event)
            self.assertEqual(self.hooks.snapshot()["s1"]["state"], expected, event)

    def test_notification_message_becomes_the_last_event(self):
        self.send("Notification", message="Claude needs your permission to use Bash")
        row = self.hooks.snapshot()["s1"]
        self.assertEqual(row["last_event"], "Claude needs your permission to use Bash")

    def test_notification_without_a_message_still_reads_as_waiting(self):
        self.send("Notification")
        self.assertEqual(self.hooks.snapshot()["s1"]["last_event"], "waiting on you")

    def test_subagent_stop_does_not_change_state(self):
        self.send("UserPromptSubmit")
        self.send("SubagentStop")
        row = self.hooks.snapshot()["s1"]
        self.assertEqual(row["state"], "working")
        self.assertEqual(row["subagent_stops"], 1)

    def test_since_only_moves_on_a_real_transition(self):
        self.send("UserPromptSubmit")
        since = self.hooks.snapshot()["s1"]["since"]
        time.sleep(0.01)
        self.send("UserPromptSubmit")  # working -> working
        self.assertEqual(self.hooks.snapshot()["s1"]["since"], since)
        self.send("Stop")
        self.assertGreater(self.hooks.snapshot()["s1"]["since"], since)

    def test_malformed_payloads_are_ignored(self):
        self.hooks.record({"hook_event_name": "Stop"})
        self.hooks.record({"session_id": "s1"})
        self.hooks.record({"session_id": 5, "hook_event_name": "Stop"})
        self.assertEqual(self.hooks.snapshot(), {})

    def test_unknown_event_names_do_not_change_state(self):
        self.send("UserPromptSubmit")
        self.send("PreToolUse")
        self.assertEqual(self.hooks.snapshot()["s1"]["state"], "working")


class ResolveTests(unittest.TestCase):
    """StateBuilder._resolve is the aging half of the state machine."""

    NOW = 1_800_000_000.0

    def resolve(self, hook, mtime, last_activity=None, now=None):
        now = now or self.NOW
        last_activity = last_activity if last_activity is not None else mtime
        return crabd.StateBuilder._resolve(hook, mtime, last_activity, now)

    @staticmethod
    def hook(state, since, at=None):
        return {"state": state, "since": since, "at": at if at is not None else since,
                "last_event": None, "cwd": None, "stops": [], "subagent_stops": 0}

    def test_fresh_transcript_without_hooks_reads_as_working(self):
        state, _ = self.resolve(None, self.NOW - 60)
        self.assertEqual(state, "working")

    def test_quiet_for_15_minutes_ages_to_idle(self):
        state, since = self.resolve(None, self.NOW - (crabd.IDLE_AFTER_SEC + 60))
        self.assertEqual(state, "idle")
        self.assertLess(since, self.NOW)

    def test_quiet_for_two_hours_ages_to_gone(self):
        state, _ = self.resolve(None, self.NOW - (crabd.GONE_AFTER_SEC + 60))
        self.assertEqual(state, "gone")

    def test_working_hook_ages_to_idle_when_the_transcript_goes_quiet(self):
        old = self.NOW - (crabd.IDLE_AFTER_SEC + 60)
        state, _ = self.resolve(self.hook("working", old), old)
        self.assertEqual(state, "idle")

    def test_needs_input_survives_idle_aging(self):
        old = self.NOW - (crabd.IDLE_AFTER_SEC + 600)
        state, _ = self.resolve(self.hook("needs_input", old), old)
        self.assertEqual(state, "needs_input")

    def test_needs_input_survives_gone_aging(self):
        """A question keeps waiting even when the transcript has been quiet for days."""
        old = self.NOW - (crabd.GONE_AFTER_SEC * 12)
        state, since = self.resolve(self.hook("needs_input", old), old)
        self.assertEqual(state, "needs_input")
        self.assertEqual(since, old)

    def test_session_end_is_gone_immediately(self):
        state, _ = self.resolve(self.hook("gone", self.NOW - 1), self.NOW - 1)
        self.assertEqual(state, "gone")

    def test_done_lingers_then_drops(self):
        recent = self.NOW - 60
        self.assertEqual(self.resolve(self.hook("done", recent), recent - 10)[0], "done")
        stale = self.NOW - (crabd.DONE_DROP_SEC + 60)
        self.assertEqual(self.resolve(self.hook("done", stale), stale - 10)[0], "gone")

    def test_done_reactivates_when_the_transcript_moves_again(self):
        # A FRESH write past the grace reads as work resumed; last_activity carries the
        # transcript clock, so the aging block serves working, not the done early-return.
        stopped = self.NOW - 300
        moved = stopped + crabd.DONE_REACTIVATION_GRACE_SEC + 60
        state, since = self.resolve(self.hook("done", stopped), moved)
        self.assertEqual(state, "working")
        self.assertEqual(since, moved)

    def test_a_reactivated_done_row_still_ages_it_is_not_a_zombie(self):
        """v0.28.2, measured live 2026-09-01: the reactivation early-return skipped the
        aging block, so ONE late transcript write (an async ai-title, a subagent
        straggler) pinned `working` on a finished session until the 2h prune - the card
        read `working · quiet 33m`. Reactivation now falls through to aging."""
        stopped = self.NOW - 3600
        moved = stopped + 600            # a write 10 min after the Stop...
        state, _ = self.resolve(self.hook("done", stopped), moved)
        self.assertEqual(state, "idle")  # ...50 min quiet since: idle, never working
        state, _ = self.resolve(self.hook("done", stopped), moved,
                                now=moved + crabd.GONE_AFTER_SEC + 1)
        self.assertEqual(state, "gone")  # and the 2h horizon still retires it

    def test_the_done_reactivation_grace_is_the_named_constant(self):
        """v0.20.0 named `+ 2`; v0.28.2 widened it to 120, measured: the CLI keeps
        writing AFTER the Stop hook (last-prompt/custom-title records, the ASYNC
        ai-title, subagent stragglers), and 2 s flipped real finished sessions back to
        `working`. A hook-lost resume pays at most two minutes of `done` - a real
        resume re-arms `working` via UserPromptSubmit and never waits on this."""
        self.assertEqual(crabd.DONE_REACTIVATION_GRACE_SEC, 120)
        stopped = self.NOW - 300
        inside = stopped + crabd.DONE_REACTIVATION_GRACE_SEC
        self.assertEqual(self.resolve(self.hook("done", stopped), inside)[0], "done")
        self.assertEqual(
            self.resolve(self.hook("done", stopped), inside + 0.5)[0], "working")

    def test_resolve_still_never_ages_a_needs_input_away(self):
        """v0.19.0 changed WHO clears needs_input, not whether TIME can. The clear is a
        real transition written by HookTracker.note_activity on evidence the model ran
        again; _resolve is still forbidden from turning silence into an answer."""
        old = self.NOW - (crabd.GONE_AFTER_SEC * 12)
        state, _ = self.resolve(self.hook("needs_input", old), self.NOW - 1)
        self.assertEqual(state, "needs_input")


class NoteActivityTests(unittest.TestCase):
    """HookTracker.note_activity - v0.19.0, the operator answered IN THE APP.

    The gap: a Notification sets needs_input for BOTH shapes of waiting, and the two
    commonest in-app answers (Allow/Deny on the terminal dialog, a pick on an
    AskUserQuestion sheet) fire no hook at all. The clearing evidence is a completed
    model round-trip in the session's own main transcript.
    """

    SID = "s1"

    def setUp(self):
        self.hooks = crabd.HookTracker()
        # Real clock, deliberately: record() stamps with time.time(), and the re-raise
        # test needs a hook that fires AFTER the clear to be later on the same clock.
        self.NOW = time.time()

    def ask(self, at=None, message="Claude needs your permission to use Bash"):
        """Put the session into needs_input as of `at` (default: NOW - 300)."""
        self.hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                           "cwd": "C:\\IT", "message": message})
        row = self.hooks.sessions[self.SID]
        row["since"] = self.NOW - 300 if at is None else at
        row["at"] = row["since"]
        return row["since"]

    def row(self):
        return self.hooks.snapshot()[self.SID]

    def test_a_later_round_trip_clears_the_question(self):
        self.ask()
        self.assertTrue(self.hooks.note_activity(self.SID, self.NOW - 10))
        row = self.row()
        self.assertEqual(row["state"], "working")
        self.assertIsNone(row["question"])
        self.assertEqual(row["since"], self.NOW - 10)

    def test_the_cleared_row_carries_no_stale_event_label(self):
        """last_event is the QUESTION while needs_input stands. Left in place it would
        render as the card's current activity on a session that is working again."""
        self.ask()
        self.hooks.note_activity(self.SID, self.NOW - 10)
        self.assertIsNone(self.row()["last_event"])

    def test_the_clear_writes_a_ring_event(self):
        self.ask()
        self.hooks.note_activity(self.SID, self.NOW - 10)
        self.assertEqual(self.row()["events"][0]["text"],
                         crabd.NEEDS_INPUT_CLEARED_EVENT)

    def test_a_round_trip_from_before_the_question_never_clears_it(self):
        """The record that CAUSED the question is older than the question. Clearing on
        it would mean no needs_input ever survived one build pass."""
        since = self.ask()
        self.assertFalse(self.hooks.note_activity(self.SID, since - 1))
        self.assertEqual(self.row()["state"], "needs_input")

    def test_activity_inside_the_grace_never_clears_it(self):
        since = self.ask()
        self.assertFalse(self.hooks.note_activity(
            self.SID, since + crabd.NEEDS_INPUT_ACTIVITY_GRACE_SEC))
        self.assertEqual(self.row()["state"], "needs_input")

    def test_an_unknown_session_is_a_no_op(self):
        self.assertFalse(self.hooks.note_activity("nobody", self.NOW))
        self.assertEqual(self.hooks.snapshot(), {})

    def test_a_malformed_session_id_is_a_no_op(self):
        for bad in (None, "", 5, ["s1"]):
            self.assertFalse(self.hooks.note_activity(bad, self.NOW))

    def test_no_other_state_is_touched(self):
        """Only needs_input -> working. A `done` row moved by transcript activity is
        _resolve's job and has its own rule (and its own DONE_DROP_SEC)."""
        for event, state in (("SessionStart", "idle"), ("Stop", "done"),
                             ("SessionEnd", "gone")):
            self.hooks.record({"session_id": self.SID, "hook_event_name": event,
                               "cwd": "C:\\IT"})
            since = self.hooks.snapshot()[self.SID]["since"]
            self.assertFalse(self.hooks.note_activity(self.SID, self.NOW + 3600), event)
            self.assertEqual(self.row()["state"], state, event)
            self.assertEqual(self.row()["since"], since, event)

    def test_it_is_idempotent(self):
        """The build loop calls it on every pass. The second call must find `working`
        and change nothing - not re-stamp `since` on every poll."""
        self.ask()
        self.assertTrue(self.hooks.note_activity(self.SID, self.NOW - 10))
        self.assertFalse(self.hooks.note_activity(self.SID, self.NOW - 5))
        self.assertEqual(self.row()["since"], self.NOW - 10)

    def test_a_future_round_trip_timestamp_is_not_written_into_since(self):
        """A-12 (v0.26.0). A transcript record dated AHEAD of crabd's own clock (an NTP
        step, a transcript copied in from another host) is a real answer and still clears -
        the freshness gate uses its own value - but that future timestamp must NOT be
        written into `since`/`at`, or the widget computes a negative age and prune is
        postponed by the skew. The written clock is clamped to now. Mutation check: writing
        `at` unclamped puts `since` ~an hour ahead of now."""
        self.ask()                                   # needs_input as of NOW - 300
        before = time.time()
        self.assertTrue(self.hooks.note_activity(self.SID, self.NOW + 3600))
        after = time.time()
        row = self.row()
        self.assertEqual(row["state"], "working")
        self.assertGreaterEqual(row["since"], before)   # moved forward, not into the past
        self.assertLessEqual(row["since"], after)        # ...but NEVER ahead of now
        self.assertLessEqual(row["at"], after)

    def test_the_ack_is_cleared_so_the_next_question_can_alert(self):
        self.ask()
        self.hooks.ack(self.SID)
        self.assertTrue(self.row()["acked"])
        self.hooks.note_activity(self.SID, self.NOW - 10)
        self.assertFalse(self.row()["acked"])

    def test_the_next_question_re_raises_at_full_strength(self):
        """THE reason the clear is a write-back and not a served-row overlay. Left on
        needs_input in the tracker, the next Notification would find `state == state`,
        so `since` would not move and `acked` would not clear - the second question of a
        turn would land pre-silenced on a card already escalated to red."""
        first = self.ask()
        self.hooks.ack(self.SID)
        self.hooks.note_activity(self.SID, self.NOW - 10)
        self.hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                           "cwd": "C:\\IT", "message": "and now this one?"})
        row = self.row()
        self.assertEqual(row["state"], "needs_input")
        self.assertFalse(row["acked"])
        self.assertGreater(row["since"], first)
        self.assertEqual(row["question"], "and now this one?")

    def test_another_sessions_activity_cannot_clear_it(self):
        self.ask()
        self.hooks.record({"session_id": "s2", "hook_event_name": "Notification",
                           "cwd": "C:\\IT", "message": "a different session"})
        self.hooks.note_activity("s2", self.NOW)
        self.assertEqual(self.row()["state"], "needs_input")
        self.assertEqual(self.row()["question"],
                         "Claude needs your permission to use Bash")


# ---------------------------------------------------------------------------- burn

class BurnTests(unittest.TestCase):
    def setUp(self):
        # Anchor at local noon so "today" and the hourly window are unambiguous
        # regardless of when the suite runs.
        today = time.localtime()
        self.now = time.mktime((today.tm_year, today.tm_mon, today.tm_mday,
                                12, 30, 0, 0, 0, -1))

    def burn(self, requests, owners=None):
        owners = owners or {rid: "s1" for rid in requests}
        return crabd.StateBuilder._burn(requests, owners, self.now)

    def test_twenty_four_buckets_oldest_first(self):
        burn, _ = self.burn({})
        hourly = burn["hourly"]
        self.assertEqual(len(hourly), 24)
        starts = [h["hourStart"] for h in hourly]
        self.assertEqual(starts, sorted(starts))
        self.assertTrue(all(h["outputTokens"] == 0 for h in hourly))

    def test_tokens_land_in_the_hour_they_were_spent(self):
        requests = {
            "r_now": (self.now - 60, 100, 1, 2, 3, "claude-fable-5"),
            "r_2h": (self.now - 2 * 3600, 200, 1, 2, 3, "claude-fable-5"),
        }
        burn, _ = self.burn(requests)
        by_hour = {h["hourStart"]: h["outputTokens"] for h in burn["hourly"]}
        self.assertEqual(sum(by_hour.values()), 300)
        current = crabd._local_iso(crabd._local_hour_start(self.now))
        two_ago = crabd._local_iso(crabd._local_hour_start(self.now - 2 * 3600))
        self.assertEqual(by_hour[current], 100)
        self.assertEqual(by_hour[two_ago], 200)

    def test_today_totals_ignore_anything_before_local_midnight(self):
        requests = {
            "r_today": (self.now - 3600, 100, 10, 20, 30, "claude-fable-5"),
            "r_yesterday": (self.now - 30 * 3600, 999, 99, 99, 99, "claude-fable-5"),
        }
        burn, _ = self.burn(requests)
        self.assertEqual(burn["today"], {"inputTokens": 10, "outputTokens": 100,
                                         "cacheReadTokens": 20, "cacheCreationTokens": 30,
                                         "messages": 1})

    def test_records_older_than_24h_are_in_no_bucket(self):
        burn, _ = self.burn({"r_old": (self.now - 30 * 3600, 999, 1, 1, 1, "claude-fable-5")})
        self.assertEqual(sum(h["outputTokens"] for h in burn["hourly"]), 0)

    def test_per_session_output_is_attributed_by_owner(self):
        requests = {"r1": (self.now - 60, 10, 0, 0, 0, "claude-fable-5"),
                    "r2": (self.now - 60, 5, 0, 0, 0, "claude-fable-5"),
                    "r3": (self.now - 30 * 3600, 500, 0, 0, 0, "claude-fable-5")}
        _, per_session = self.burn(requests, {"r1": "sA", "r2": "sB", "r3": "sA"})
        self.assertEqual(per_session, {"sA": 10, "sB": 5})


# -------------------------------------------------------------------------- limits

class LimitsMappingTests(unittest.TestCase):
    def test_documented_shape_maps_to_the_contract(self):
        payload = {
            "five_hour": {"utilization": 0.42, "resets_at": "2026-08-26T21:00:00Z"},
            "seven_day": {"utilization": 0.18, "resets_at": "2026-08-30T00:00:00Z"},
            "seven_day_opus": {"utilization": 0.05, "resets_at": "2026-08-30T00:00:00Z"},
        }
        out = crabd.LimitsReader.map_payload(payload, "max", "default_claude_max_20x")
        self.assertTrue(out["available"])
        self.assertEqual(out["fiveHour"]["utilization"], 0.42)
        self.assertEqual(out["weekly"]["utilization"], 0.18)
        self.assertEqual([e["label"] for e in out["extra"]], ["opus weekly"])
        self.assertEqual(out["subscriptionType"], "max")

    def test_last_good_survives_restart_via_disk_cache(self):
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            orig = crabd.LIMITS_CACHE_FILE
            crabd.LIMITS_CACHE_FILE = pathlib.Path(td) / "limits-cache.json"
            try:
                good = {"available": True, "note": None,
                        "fiveHour": {"utilization": 0.12, "resetsAt": None}, "weekly": None,
                        "extra": [], "subscriptionType": "max", "rateLimitTier": "t"}
                r1 = crabd.LimitsReader()
                r1._last_good = None
                r1._fetch = lambda: good
                import time as _t
                r1.get(now=_t.time())
                r2 = crabd.LimitsReader()          # "restart"
                self.assertIsNotNone(r2._last_good)
                self.assertEqual(r2._last_good["fiveHour"]["utilization"], 0.12)
            finally:
                crabd.LIMITS_CACHE_FILE = orig

    def test_consecutive_429s_double_the_backoff(self):
        r = crabd.LimitsReader()
        r._last_good = None; r._last_good_at = 0.0
        bad = {"available": False, "note": "usage endpoint returned HTTP 429", "fiveHour": None,
               "weekly": None, "extra": [], "subscriptionType": None, "rateLimitTier": None}
        r._fetch = lambda: dict(bad)
        r.get(now=0.0)
        first = r._backoff_until
        r.get(now=r._backoff_until + crabd.LIMITS_TTL_SEC + 1)
        self.assertGreater(r._backoff_until - (first + crabd.LIMITS_TTL_SEC + 1), first * 1.5)

    def test_429_backoff_serves_last_good_and_stops_calling(self):
        """On-glass 2026-08-26: the endpoint 429s at sub-minute cadence. A 429 must
        (a) serve the last good reading, (b) open a backoff window with no fetches."""
        r = crabd.LimitsReader()
        r._last_good = None; r._last_good_at = 0.0
        good = {"available": True, "note": None, "fiveHour": {"utilization": 0.12, "resetsAt": None},
                "weekly": None, "extra": [], "subscriptionType": "max", "rateLimitTier": "t"}
        calls = []
        r._fetch = lambda: calls.append(1) or good
        self.assertTrue(r.get(now=1000.0)["available"])
        bad = {"available": False, "note": "usage endpoint returned HTTP 429", "fiveHour": None,
               "weekly": None, "extra": [], "subscriptionType": "max", "rateLimitTier": "t"}
        r._fetch = lambda: calls.append(1) or dict(bad)
        out = r.get(now=1000.0 + crabd.LIMITS_TTL_SEC + 1)
        self.assertTrue(out["available"])          # last good served, not em-dashes
        self.assertEqual(out["fiveHour"]["utilization"], 0.12)
        n = len(calls)
        out2 = r.get(now=1000.0 + crabd.LIMITS_TTL_SEC + 2)
        self.assertEqual(len(calls), n)            # inside backoff: no endpoint call
        self.assertTrue(out2["available"])

    def test_429_with_stale_last_good_admits_unavailability(self):
        r = crabd.LimitsReader()
        bad = {"available": False, "note": "usage endpoint returned HTTP 429", "fiveHour": None,
               "weekly": None, "extra": [], "subscriptionType": None, "rateLimitTier": None}
        r._last_good = {"available": True}
        r._last_good_at = 0.0
        r._fetch = lambda: dict(bad)
        out = r.get(now=crabd.LIMITS_LAST_GOOD_MAX_AGE + 10.0)
        # Past LIMITS_LAST_GOOD_MAX_AGE a reading is no longer worth qualifying - it is
        # withheld. Below it, it is served with a note (see LimitsAgeNoteTests).
        self.assertFalse(out["available"])

    def test_retry_after_extends_the_backoff_floor(self):
        r = crabd.LimitsReader()
        r._last_good = None; r._last_good_at = 0.0
        bad = {"available": False, "note": "usage endpoint returned HTTP 429", "_retryAfter": 900.0,
               "fiveHour": None, "weekly": None, "extra": [], "subscriptionType": None, "rateLimitTier": None}
        r._fetch = lambda: dict(bad)
        r.get(now=100.0)
        self.assertEqual(r._backoff_until, 100.0 + 900.0)

    def test_measured_2026_08_26_live_shape(self):
        """Pins the real 200 measured 2026-08-26: 0-100 utilizations, limits[] scoped
        weekly, extra_usage credits gauged, junk keys (nimbus_quill) NOT gauged."""
        payload = {
            "five_hour": {"utilization": 10.0, "resets_at": "2026-08-26T22:29:59.742008+00:00",
                          "limit_dollars": None},
            "seven_day": {"utilization": 34.0, "resets_at": "2026-08-31T13:59:59.742029+00:00"},
            "seven_day_opus": None,
            "nimbus_quill": {"utilization": 0.0, "resets_at": None},
            "extra_usage": {"is_enabled": True, "utilization": 30.241, "used_credits": 30241.0},
            "limits": [
                {"kind": "session", "group": "session", "percent": 10, "is_active": False},
                {"kind": "weekly_all", "group": "weekly", "percent": 34, "is_active": True},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 3,
                 "resets_at": "2026-08-31T13:59:59.742259+00:00",
                 "scope": {"model": {"id": None, "display_name": "Fable"}}},
            ],
        }
        out = crabd.LimitsReader.map_payload(payload, "max", "default_claude_max_20x")
        self.assertTrue(out["available"])
        self.assertEqual(out["fiveHour"]["utilization"], 0.1)
        self.assertEqual(out["weekly"]["utilization"], 0.34)
        labels = [e["label"] for e in out["extra"]]
        self.assertEqual(labels, ["extra credits", "Fable weekly"])  # sorted by utilization desc
        self.assertNotIn("nimbus_quill", labels)
        self.assertEqual(out["extra"][1]["utilization"], 0.03)

    def test_percent_shaped_utilization_is_normalised(self):
        out = crabd.LimitsReader.map_payload({"five_hour": {"utilization": 42}}, None, None)
        self.assertEqual(out["fiveHour"]["utilization"], 0.42)

    def test_measured_2026_09_01_percent_one_is_one_percent_not_full(self):
        """Pins the live 200 that fired the sniff's blind spot: `seven_day.utilization:
        1.0` MEANT 1% (a Monday-fresh week) and the same document's limits[] said so
        (`weekly_all percent: 1`), but the >1.0 sniff read it as fraction-1.0 and the
        panel gauged the weekly RED at 100%. limits[] percent now outranks the sniff."""
        payload = {
            "five_hour": {"utilization": 4.0, "resets_at": "2026-09-01T13:29:59.77873-06:00"},
            "seven_day": {"utilization": 1.0, "resets_at": "2026-09-07T07:59:59.778756-06:00"},
            "limits": [
                {"kind": "session", "group": "session", "percent": 4, "is_active": True},
                {"kind": "weekly_all", "group": "weekly", "percent": 1, "is_active": False},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 1,
                 "resets_at": "2026-09-07T07:59:59.779046-06:00",
                 "scope": {"model": {"id": None, "display_name": "Fable"}}},
            ],
        }
        out = crabd.LimitsReader.map_payload(payload, "max", "default_claude_max_20x")
        self.assertEqual(out["weekly"]["utilization"], 0.01)   # not 1.0
        self.assertEqual(out["fiveHour"]["utilization"], 0.04)
        self.assertEqual(out["extra"][0]["utilization"], 0.01)

    def test_limits_percent_outranks_the_window_sniff_both_directions(self):
        # fraction-shaped window + percent row disagreeing: percent wins.
        out = crabd.LimitsReader.map_payload({
            "seven_day": {"utilization": 0.5},
            "limits": [{"kind": "weekly_all", "percent": 34}],
        }, None, None)
        self.assertEqual(out["weekly"]["utilization"], 0.34)

    def test_sniff_survives_documents_without_a_limits_array(self):
        # No limits[] -> the old behaviour stands, ambiguity and all: a bare 0.5 is a
        # fraction, a bare 42 is a percent. This branch is the fallback, not the fix.
        out = crabd.LimitsReader.map_payload({"seven_day": {"utilization": 0.5}}, None, None)
        self.assertEqual(out["weekly"]["utilization"], 0.5)

    def test_limits_row_supplies_resets_only_when_window_lacks_one(self):
        out = crabd.LimitsReader.map_payload({
            "seven_day": {"utilization": 1.0},
            "limits": [{"kind": "weekly_all", "percent": 1,
                        "resets_at": "2026-09-07T07:59:59-06:00"}],
        }, None, None)
        self.assertEqual(out["weekly"]["utilization"], 0.01)
        self.assertTrue(out["weekly"]["resetsAt"].endswith("Z"))

    def test_camel_case_and_nesting_are_tolerated(self):
        payload = {"usage": {"fiveHour": {"utilization": 0.1, "resetsAt": 1800000000}}}
        out = crabd.LimitsReader.map_payload(payload, None, None)
        self.assertTrue(out["available"])
        self.assertTrue(out["fiveHour"]["resetsAt"].endswith("Z"))

    def test_a_document_with_no_windows_is_unavailable_not_zero(self):
        out = crabd.LimitsReader.map_payload({"hello": "world"}, None, None)
        self.assertFalse(out["available"])
        self.assertIsNone(out["fiveHour"])
        self.assertIsNone(out["weekly"])
        self.assertTrue(out["note"])

    def test_expired_token_never_produces_zeros(self):
        out = crabd.LimitsReader._unavailable("Claude token expired")
        self.assertFalse(out["available"])
        self.assertIsNone(out["fiveHour"])
        self.assertEqual(out["extra"], [])


class LimitsTokenFallbackTests(unittest.TestCase):
    """v0.30.0: the long-lived token in ~/.sidecrab/limits-token.dpapi is used only when
    the CLI's own token is expired, and never leaks. urlopen is stubbed so no test talks
    to the usage endpoint; the DPAPI reader is stubbed except in the one Windows-only
    round-trip test.

    The reader names platform=WindowsPlatform() explicitly: the store is a DPAPI blob,
    so its precedence rules are a Windows claim and must keep being proven on every
    host rather than only on the one that would have selected that platform."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.creds = root / "credentials.json"
        self.token_file = root / "limits-token.dpapi"
        self.orig = (crabd.CREDENTIALS_FILE, crabd.LIMITS_TOKEN_FILE,
                     crabd.urllib.request.urlopen, crabd._dpapi_unprotect)
        crabd.CREDENTIALS_FILE = self.creds
        crabd.LIMITS_TOKEN_FILE = self.token_file
        self.seen = []
        payload = json.dumps({"five_hour": {"utilization": 0.25, "resets_at": 1800000000},
                              "seven_day": {"utilization": 0.5, "resets_at": 1800500000}}).encode()

        class _Resp:
            def __init__(self, body): self._b = body
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(request, timeout=None):
            self.seen.append(request.get_header("Authorization"))
            if self.reject:
                raise crabd.urllib.error.HTTPError(request.full_url, 401, "nope", {}, None)
            return _Resp(payload)

        self.reject = False
        crabd.urllib.request.urlopen = fake_urlopen
        crabd._dpapi_unprotect = lambda blob: blob[::-1]     # "decrypt" = reverse
        self.reader = crabd.LimitsReader(cache_file=root / "cache.json",
                                         platform=crabd.WindowsPlatform())

    def tearDown(self):
        (crabd.CREDENTIALS_FILE, crabd.LIMITS_TOKEN_FILE,
         crabd.urllib.request.urlopen, crabd._dpapi_unprotect) = self.orig

    def write_creds(self, token="cli-token", expires_in_ms=3_600_000):
        self.creds.write_text(json.dumps({"claudeAiOauth": {
            "accessToken": token, "expiresAt": int(time.time() * 1000) + expires_in_ms,
            "subscriptionType": "max", "rateLimitTier": "t"}}), encoding="utf-8")

    def store_token(self, token="sk-ant-oat01-long"):
        self.token_file.write_bytes(token.encode()[::-1])

    def test_a_fresh_cli_token_wins_even_when_a_long_lived_one_is_stored(self):
        self.write_creds(); self.store_token()
        out = self.reader.get(time.time(), force=True)
        self.assertTrue(out["available"])
        self.assertEqual(out["tokenSource"], "cli")
        self.assertEqual(self.seen, ["Bearer cli-token"])

    def test_an_expired_cli_token_falls_back_to_the_long_lived_one(self):
        self.write_creds(expires_in_ms=-1000); self.store_token()
        out = self.reader.get(time.time(), force=True)
        self.assertTrue(out["available"])
        self.assertEqual(out["tokenSource"], "sidecrab")
        self.assertEqual(self.seen, ["Bearer sk-ant-oat01-long"])
        self.assertNotIn("sk-ant-oat01-long", json.dumps(out))

    def test_a_missing_cli_file_still_uses_the_long_lived_token(self):
        self.store_token()
        out = self.reader.get(time.time(), force=True)
        self.assertFalse(out["available"])   # no file at all is the "no credentials" note
        self.assertIn("no Claude credentials", out["note"])

    def test_expired_with_no_long_lived_token_says_how_to_fix_it(self):
        self.write_creds(expires_in_ms=-1000)
        out = self.reader.get(time.time(), force=True)
        self.assertFalse(out["available"])
        self.assertIn("expired", out["note"])
        self.assertIn("-LimitsToken", out["note"])
        self.assertEqual(self.seen, [])
        self.assertIsNone(out["fiveHour"])

    def test_a_rejected_long_lived_token_names_itself(self):
        self.write_creds(expires_in_ms=-1000); self.store_token(); self.reject = True
        out = self.reader.get(time.time(), force=True)
        self.assertFalse(out["available"])
        self.assertIn("SideCrab limits token rejected", out["note"])
        self.assertIn("setup-token", out["note"])

    def test_an_unreadable_token_file_is_the_same_as_none(self):
        self.write_creds(expires_in_ms=-1000)
        crabd._dpapi_unprotect = lambda blob: None
        self.token_file.write_bytes(b"garbage")
        out = self.reader.get(time.time(), force=True)
        self.assertFalse(out["available"])
        self.assertIn("expired", out["note"])

    def test_the_token_is_read_fresh_on_every_fetch(self):
        """Stored while crabd runs -> used on the next poll, no restart."""
        self.write_creds(expires_in_ms=-1000)
        self.assertFalse(self.reader.get(time.time(), force=True)["available"])
        self.store_token()
        self.assertTrue(self.reader.get(time.time(), force=True)["available"])

    @unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
    def test_a_real_dpapi_blob_from_protecteddata_round_trips(self):
        """What the installer writes ([ProtectedData]::Protect, CurrentUser, no entropy)
        is what crabd reads. Encrypt here with CryptProtectData through the same ctypes
        surface, then read through the real reader."""
        crabd._dpapi_unprotect = self.orig[3]
        secret = b"sk-ant-oat01-roundtrip"
        crypt32 = crabd.ctypes.windll.crypt32
        buf = crabd.ctypes.create_string_buffer(secret, len(secret))
        inp = crabd._DATA_BLOB(len(secret), crabd.ctypes.cast(buf, crabd.ctypes.POINTER(crabd.ctypes.c_char)))
        out = crabd._DATA_BLOB()
        self.assertTrue(crypt32.CryptProtectData(crabd.ctypes.byref(inp), None, None, None, None, 0,
                                                 crabd.ctypes.byref(out)))
        blob = crabd.ctypes.string_at(out.pbData, out.cbData)
        crabd.ctypes.windll.kernel32.LocalFree(out.pbData)
        self.token_file.write_bytes(blob)
        self.assertEqual(crabd.read_limits_token(self.token_file), "sk-ant-oat01-roundtrip")
        self.token_file.write_bytes(blob[:-5] + b"xxxxx")   # tampered -> None, never garbage
        self.assertIsNone(crabd.read_limits_token(self.token_file))


# ------------------------------------------------- limits cache: isolation + sanity

GOOD_LIMITS = {"available": True, "note": None,
               "fiveHour": {"utilization": 0.12, "resetsAt": None}, "weekly": None,
               "extra": [], "subscriptionType": "max", "rateLimitTier": "t"}


class LimitsCacheIsolationTests(unittest.TestCase):
    """The 2026-08-26 production defect, pinned: this suite wrote the operator's real
    ~/.sidecrab/limits-cache.json with fixture data and poisoned last-good."""

    @staticmethod
    def real_paths():
        home = Path.home() / ".sidecrab"
        return home / "limits-cache.json", home / "config.json"

    def test_the_module_globals_never_name_a_real_file_during_a_test_run(self):
        real_cache, real_config = self.real_paths()
        self.assertNotEqual(crabd.LIMITS_CACHE_FILE, real_cache)
        self.assertNotEqual(crabd.USER_CONFIG_FILE, real_config)
        # v0.28.0: the token file, which ModelCatalog reads before going to the network.
        real_creds = Path.home() / ".claude" / ".credentials.json"
        self.assertNotEqual(crabd.CREDENTIALS_FILE, real_creds)
        self.assertFalse(crabd.CREDENTIALS_FILE.exists())

    def test_a_full_save_load_cycle_leaves_the_real_file_untouched(self):
        """The regression proof: a real LimitsReader, a successful fetch (which SAVES),
        and a restart (which LOADS) - with the operator's file byte-identical after."""
        real_cache, _ = self.real_paths()
        before = real_cache.read_bytes() if real_cache.exists() else None
        now = time.time()

        reader = crabd.LimitsReader()
        reader._last_good = None
        reader._fetch = lambda: dict(GOOD_LIMITS)
        self.assertTrue(reader.get(now=now)["available"])

        self.assertTrue(crabd.LIMITS_CACHE_FILE.exists())   # the temp file took the write
        restarted = crabd.LimitsReader()                    # load half of the cycle
        self.assertEqual(restarted._last_good["fiveHour"]["utilization"], 0.12)
        self.assertAlmostEqual(restarted._last_good_at, now, places=3)

        after = real_cache.read_bytes() if real_cache.exists() else None
        self.assertEqual(before, after)

    def test_an_injected_cache_path_beats_the_module_global(self):
        with tempfile.TemporaryDirectory() as td:
            mine = Path(td) / "nested" / "limits-cache.json"
            reader = crabd.LimitsReader(cache_file=mine)
            reader._last_good = None
            reader._fetch = lambda: dict(GOOD_LIMITS)
            reader.get(now=time.time())
            self.assertTrue(mine.exists())
            self.assertEqual(crabd.LimitsReader(cache_file=mine)._last_good["subscriptionType"],
                             "max")


class LimitsCacheSanityTests(unittest.TestCase):
    """Startup sanity on the on-disk `at`."""

    def reader_over(self, saved):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "limits-cache.json"
        path.write_text(json.dumps(saved), encoding="utf-8")
        return crabd.LimitsReader(cache_file=path)

    def test_the_measured_poisoned_file_is_treated_as_absent(self):
        """Byte-for-byte what production held on 2026-08-26: fixture limits, at=1000.0."""
        reader = self.reader_over({"limits": dict(GOOD_LIMITS), "at": 1000.0})
        self.assertIsNone(reader._last_good)
        self.assertEqual(reader._last_good_at, 0.0)

    def test_any_at_before_2020_is_corrupt_not_merely_stale(self):
        for at in (0.0, 1.0, 976.0, crabd.LIMITS_CACHE_MIN_EPOCH - 1):
            self.assertIsNone(self.reader_over(
                {"limits": dict(GOOD_LIMITS), "at": at})._last_good, at)

    def test_a_plausible_at_still_loads(self):
        now = time.time()
        reader = self.reader_over({"limits": dict(GOOD_LIMITS), "at": now})
        self.assertIsNotNone(reader._last_good)
        self.assertEqual(reader._last_good_at, now)

    def test_a_missing_or_junk_file_is_simply_absent(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(crabd.LimitsReader(
                cache_file=Path(td) / "nope.json")._last_good)
            junk = Path(td) / "junk.json"
            junk.write_text("{not json", encoding="utf-8")
            self.assertIsNone(crabd.LimitsReader(cache_file=junk)._last_good)

    def test_an_unavailable_reading_is_never_stored_as_last_good(self):
        reader = self.reader_over({"limits": crabd.LimitsReader._unavailable("x"),
                                   "at": time.time()})
        self.assertIsNone(reader._last_good)


class LimitsTuningTests(unittest.TestCase):
    """Retuned 2026-08-26 for a stingy quota. Pinned so a future edit is deliberate."""

    def test_the_success_cache_is_ten_minutes(self):
        self.assertEqual(crabd.LIMITS_TTL_SEC, 600)

    def test_last_good_is_trusted_for_three_hours(self):
        self.assertEqual(crabd.LIMITS_LAST_GOOD_MAX_AGE, 10800)

    def test_a_fresh_cached_serve_can_never_outlive_the_note_threshold(self):
        """If the TTL exceeded the note threshold, a reading could be served unqualified
        while older than the age the contract says must be disclosed."""
        self.assertLessEqual(crabd.LIMITS_TTL_SEC, crabd.LIMITS_NOTE_STALE_SEC)


class LimitsAgeNoteTests(unittest.TestCase):
    """Contract v0.4.0: `note` may be non-null while available stays true."""

    BAD_429 = {"available": False, "note": "usage endpoint returned HTTP 429",
               "fiveHour": None, "weekly": None, "extra": [],
               "subscriptionType": None, "rateLimitTier": None}

    def locked_out(self, last_good_at):
        reader = crabd.LimitsReader()
        reader._last_good = dict(GOOD_LIMITS)
        reader._last_good_at = last_good_at
        reader._fetch = lambda: dict(self.BAD_429)
        return reader

    def test_a_fresh_serve_carries_no_note(self):
        reader = crabd.LimitsReader()
        reader._last_good = None
        reader._fetch = lambda: dict(GOOD_LIMITS)
        out = reader.get(now=time.time())
        self.assertTrue(out["available"])
        self.assertIsNone(out["note"])

    def test_last_good_under_the_threshold_is_served_unqualified(self):
        at = time.time()
        out = self.locked_out(at).get(now=at + crabd.LIMITS_NOTE_STALE_SEC - 1)
        self.assertTrue(out["available"])
        self.assertIsNone(out["note"])

    def test_last_good_past_the_threshold_says_when_it_was_true(self):
        at = time.time() - 20 * 60
        out = self.locked_out(at).get(now=time.time())
        self.assertTrue(out["available"])                       # gauges stay lit
        self.assertEqual(out["fiveHour"]["utilization"], 0.12)  # on the last-good values
        expected = time.strftime("%I:%M %p", time.localtime(at)).lstrip("0")
        self.assertEqual(out["note"], f"limits as of {expected}")

    def test_the_note_is_an_absolute_clock_time_not_a_relative_phrase(self):
        """It is cached for up to LIMITS_TTL_SEC; 'x minutes ago' would rot inside the
        window, a wall-clock time cannot."""
        at = time.time() - 3600
        note = self.locked_out(at).get(now=time.time())["note"]
        self.assertRegex(note, r"^limits as of \d{1,2}:\d{2} (AM|PM)$")
        for word in ("ago", "minute", "hour", "stale"):
            self.assertNotIn(word, note)

    def test_qualifying_a_serve_does_not_mutate_the_stored_last_good(self):
        """Otherwise the note sticks to the reading and survives the next fresh fetch."""
        at = time.time() - 3600
        reader = self.locked_out(at)
        self.assertIsNotNone(reader.get(now=time.time())["note"])
        self.assertIsNone(reader._last_good["note"])

    def test_past_the_max_age_the_reading_is_withheld_not_qualified(self):
        at = time.time() - crabd.LIMITS_LAST_GOOD_MAX_AGE - 60
        out = self.locked_out(at).get(now=time.time())
        self.assertFalse(out["available"])
        self.assertIn("429", out["note"])

    def test_a_reading_from_a_restart_is_qualified_the_same_way(self):
        """The disk-cached path and the in-memory path must not disagree about age."""
        at = time.time() - 40 * 60
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "limits-cache.json"
            path.write_text(json.dumps({"limits": dict(GOOD_LIMITS), "at": at}),
                            encoding="utf-8")
            reader = crabd.LimitsReader(cache_file=path)
            reader._fetch = lambda: dict(self.BAD_429)
            out = reader.get(now=time.time())
        self.assertTrue(out["available"])
        self.assertTrue(out["note"].startswith("limits as of "))

    def test_the_local_clock_helper_strips_the_zero_pad(self):
        nine_am = time.mktime((2026, 8, 26, 9, 5, 0, 0, 0, -1))
        self.assertEqual(crabd._local_clock(nine_am), "9:05 AM")


# ------------------------------------------------------------- v0.13.0 forecast (unit)

class DepletionForecastTests(unittest.TestCase):
    """DepletionForecaster - limits.fiveHour/weekly[.exhaustAt] (contract v0.13.0).

    Time is injected as `now`, never slept: the forecaster records at most one sample
    per FORECAST_MIN_SAMPLE_GAP_SEC, so the two-reading cases below space their readings
    60 s apart, which clears both that gap and the 60 s minimum span in one step.
    """

    T0 = 1_756_000_000.0        # a sane 2025 epoch; far from LIMITS_CACHE_MIN_EPOCH

    def block(self, util, resets_offset=3600.0):
        """A one-window limits block the forecaster can annotate in place."""
        return {"fiveHour": {"utilization": util,
                             "resetsAt": crabd._utc_iso(self.T0 + resets_offset)},
                "weekly": None, "extra": []}

    def feed(self, fc, readings):
        """Annotate a fresh block per (dt, util) reading; return the LAST block's
        fiveHour.exhaustAt. Fresh blocks because annotate() replaces the window dict."""
        exhaust = None
        for dt, util in readings:
            blk = self.block(util)
            fc.annotate(blk, self.T0 + dt)
            exhaust = blk["fiveHour"]["exhaustAt"]
        return exhaust

    def test_rising_util_projects_a_sane_exhaustAt(self):
        fc = crabd.DepletionForecaster()
        exhaust = self.feed(fc, [(0.0, 0.50), (60.0, 0.60)])
        # rate = 0.10 / 60 s; remaining = 0.40 -> 240 s past the second reading (T0+60).
        self.assertEqual(exhaust, crabd._utc_iso(self.T0 + 60 + 240))
        # ... and it lands before the window's own reset (T0 + 3600).
        self.assertLess(crabd._parse_ts(exhaust), self.T0 + 3600)

    def test_the_key_is_always_present_even_before_a_forecast_exists(self):
        fc = crabd.DepletionForecaster()
        blk = self.block(0.5)
        fc.annotate(blk, self.T0)
        self.assertIn("exhaustAt", blk["fiveHour"])   # one sample -> present but null
        self.assertIsNone(blk["fiveHour"]["exhaustAt"])

    def test_flat_util_is_null(self):
        fc = crabd.DepletionForecaster()
        self.assertIsNone(self.feed(fc, [(0.0, 0.50), (60.0, 0.50)]))

    def test_declining_util_is_null(self):
        fc = crabd.DepletionForecaster()
        self.assertIsNone(self.feed(fc, [(0.0, 0.60), (60.0, 0.50)]))

    def test_a_single_sample_is_null(self):
        fc = crabd.DepletionForecaster()
        self.assertIsNone(self.feed(fc, [(0.0, 0.50)]))

    def test_samples_under_60s_apart_are_null(self):
        # 50 s clears the 45 s record gap so BOTH land, but is under the 60 s span the
        # slope needs - so this exercises FORECAST_MIN_SPAN_SEC, not the sample gap.
        fc = crabd.DepletionForecaster()
        self.assertIsNone(self.feed(fc, [(0.0, 0.50), (50.0, 0.60)]))

    def test_projection_past_resetsAt_is_null(self):
        fc = crabd.DepletionForecaster()
        # 0.10 -> 0.11 over 60 s projects ~5340 s out; the window resets in 1000 s first.
        blk1 = self.block(0.10, resets_offset=1000.0)
        fc.annotate(blk1, self.T0)
        blk2 = self.block(0.11, resets_offset=1000.0)
        fc.annotate(blk2, self.T0 + 60)
        self.assertIsNone(blk2["fiveHour"]["exhaustAt"])

    def creeping(self, resets, span=900.0):
        """Two readings a 15-min span apart whose utilization creeps by one 4dp step -
        the smallest genuine move the served rounding can produce. Returns the second
        block's exhaustAt. The slope is real but tiny, so the projection lands ~93 days
        out (MEASURED: the pre-fix code served 2025-11-25T19:46:40Z for exactly this
        input) - and a slope an order smaller runs into _utc_iso's year-3000 ceiling,
        which is where the audit's headline number came from. Either way it is a date
        the window's own reset would have vetoed if there had been one to read."""
        fc = crabd.DepletionForecaster()
        for dt, util in ((0.0, 0.10), (span, 0.1001)):
            blk = {"fiveHour": {"utilization": util, "resetsAt": resets},
                   "weekly": None, "extra": []}
            fc.annotate(blk, self.T0 + dt)
        return blk["fiveHour"]["exhaustAt"]

    def test_a_window_with_no_parseable_resetsAt_is_null_not_a_far_future_date(self):
        """AUDIT F6 (fixed v0.17.0). The contract says exhaustAt is never extrapolated
        past the window's own resetsAt - and with no parseable reset that cap was simply
        SKIPPED, so the raw projection went out (see `creeping` for the measured date).
        A number nothing capped is a date crabd made up, which the repo's unknown-is-null
        rule forbids - so the answer is null, for every shape a reset can fail to be."""
        for resets in (None, "", "not a date", 1e30, {}, [], True, "0000-13-45"):
            self.assertIsNone(self.creeping(resets), resets)

    def test_the_null_is_the_missing_reset_and_not_a_flat_slope(self):
        """The control for the test above: the SAME two readings, with a reset far enough
        out to leave the projection inside the window, still forecast. Without this the
        test above would pass just as well if the forecaster had stopped working."""
        exhaust = self.creeping(crabd._utc_iso(self.T0 + 40_000_000))
        self.assertIsNotNone(exhaust)
        self.assertLess(crabd._parse_ts(exhaust), self.T0 + 40_000_000)

    def test_a_util_drop_resets_history(self):
        fc = crabd.DepletionForecaster()
        # Build a live forecast, then drop hard (a reset or a statusline<->oauth flip).
        self.assertIsNotNone(self.feed(fc, [(0.0, 0.50), (60.0, 0.60)]))
        drop = self.block(0.20)
        fc.annotate(drop, self.T0 + 120)
        # The pre-drop samples are gone: one lone sample remains, so no forecast.
        self.assertIsNone(drop["fiveHour"]["exhaustAt"])
        # A fresh rising pair AFTER the drop forecasts off the NEW trend alone (0.20 ->
        # 0.30 over 60 s), proving the old 0.50/0.60 slope did not bleed through.
        after = self.block(0.30)
        fc.annotate(after, self.T0 + 180)
        self.assertEqual(after["fiveHour"]["exhaustAt"],
                         crabd._utc_iso(self.T0 + 180 + 420))

    def test_weekly_and_extras_are_forecast_independently(self):
        fc = crabd.DepletionForecaster()
        for dt, five, week in ((0.0, 0.50, 0.20), (60.0, 0.60, 0.20)):
            blk = {"fiveHour": {"utilization": five,
                                "resetsAt": crabd._utc_iso(self.T0 + 3600)},
                   "weekly": {"utilization": week,
                              "resetsAt": crabd._utc_iso(self.T0 + 3600)},
                   "extra": []}
            fc.annotate(blk, self.T0 + dt)
        # fiveHour is rising -> a forecast; weekly is flat -> null. Independent histories.
        self.assertIsNotNone(blk["fiveHour"]["exhaustAt"])
        self.assertIsNone(blk["weekly"]["exhaustAt"])

    def _flood_block(self, n, tag, five=0.5, week=0.2):
        return {"fiveHour": {"utilization": five, "resetsAt": crabd._utc_iso(self.T0 + 3600)},
                "weekly": {"utilization": week, "resetsAt": crabd._utc_iso(self.T0 + 3600)},
                "extra": [{"label": f"{tag}_{i}", "utilization": 0.1,
                           "resetsAt": crabd._utc_iso(self.T0 + 3600)} for i in range(n)]}

    def test_extra_label_keyspace_is_bounded(self):
        """F1: `extra:` labels are attacker-influenced through the unauthenticated
        /v1/statusline (each `seven_day_*` key mints one). Without a key cap _history grows
        without bound - a slow OOM. Reproduce the audit's 5000-label flood and assert the
        tracked keyspace stays bounded, then keep flooding with FRESH labels (the real leak
        vector) and assert it never drifts up."""
        fc = crabd.DepletionForecaster()
        fc.annotate(self._flood_block(5000, "junk"), self.T0)
        self.assertLessEqual(len(fc._history), crabd.FORECAST_MAX_KEYS)
        for build in range(1, 6):
            fc.annotate(self._flood_block(5000, f"b{build}"), self.T0 + build * 60)
            self.assertLessEqual(len(fc._history), crabd.FORECAST_MAX_KEYS)

    def test_named_windows_survive_an_extra_flood_and_still_forecast(self):
        """The two contract windows are never the ones evicted - they are the forecasts that
        matter. A flood of extras pushes them toward the front of the recency order, so without
        the protected-key guard the real fiveHour history would be the first thing dropped.
        Prove fiveHour keeps both samples across two flooded builds and still projects."""
        fc = crabd.DepletionForecaster()
        blk1 = self._flood_block(5000, "j0", five=0.50)
        fc.annotate(blk1, self.T0)
        blk2 = self._flood_block(5000, "j1", five=0.60)   # rising, 60 s later, fresh labels
        fc.annotate(blk2, self.T0 + 60)
        self.assertIn("fiveHour", fc._history)
        self.assertIn("weekly", fc._history)
        self.assertEqual(len(fc._history["fiveHour"]), 2, "the real window's history survived")
        self.assertIsNotNone(blk2["fiveHour"]["exhaustAt"], "and it still forecasts")
        self.assertLessEqual(len(fc._history), crabd.FORECAST_MAX_KEYS)

    def test_a_recurring_extra_survives_normal_load(self):
        """Eviction is least-recently-UPDATED, so under NORMAL load (a handful of extras well
        under the cap) a genuinely recurring extra label is never the one dropped and its
        forecast stays correct. (A single-build flood that overflows the cap does drop history
        observed before its tail - that is deliberate: the bound is the point, an evicted extra
        re-accumulates to a harmless null, and the two named windows are protected outright.)"""
        fc = crabd.DepletionForecaster()
        keep = "extra:model-a weekly"
        for build, (dt, util) in enumerate(((0.0, 0.50), (60.0, 0.60), (120.0, 0.70))):
            blk = {"fiveHour": None, "weekly": None,
                   "extra": [{"label": "model-a weekly", "utilization": util,
                              "resetsAt": crabd._utc_iso(self.T0 + 3600)}]
                            + [{"label": f"model-{i} weekly", "utilization": 0.1,
                                "resetsAt": crabd._utc_iso(self.T0 + 3600)} for i in range(6)]}
            fc.annotate(blk, self.T0 + dt)
        self.assertIn(keep, fc._history, "the recurring extra kept its slot under normal load")
        self.assertGreaterEqual(len(fc._history[keep]), 2)
        self.assertIsNotNone(blk["extra"][0]["exhaustAt"], "and still forecasts")

    def test_annotate_does_not_mutate_a_shared_cached_window(self):
        """LimitsReader/StatusLineReader hand back dicts whose window sub-dicts alias
        their own cache; annotate must copy, never write exhaustAt into that cache."""
        fc = crabd.DepletionForecaster()
        window = {"utilization": 0.5, "resetsAt": crabd._utc_iso(self.T0 + 3600)}
        blk = {"fiveHour": window, "weekly": None, "extra": []}
        fc.annotate(blk, self.T0)
        self.assertNotIn("exhaustAt", window)          # the caller's dict is untouched
        self.assertIn("exhaustAt", blk["fiveHour"])    # the served copy carries it

    def test_none_and_unavailable_blocks_are_safe(self):
        fc = crabd.DepletionForecaster()
        fc.annotate(None, self.T0)                     # must not raise
        blk = {"available": False, "fiveHour": None, "weekly": None, "extra": []}
        fc.annotate(blk, self.T0)
        self.assertIsNone(blk["fiveHour"])             # None window left as-is


class DepletionForecastThroughBuilderTests(TempProjects):
    """The forecast is fed WHERE LIMITS ARE BUILT: StateBuilder._limits_block. Driving
    two rising readings through build(limits=...) 60 s apart lands exhaustAt on the
    served document - the same path the OAuth and statusline sources flow through."""

    T0 = 1_756_000_000.0

    def limits(self, util):
        return {"available": True, "note": None,
                "fiveHour": {"utilization": util,
                             "resetsAt": crabd._utc_iso(self.T0 + 3600)},
                "weekly": None, "extra": [],
                "subscriptionType": "max", "rateLimitTier": "default_claude_max_20x"}

    def test_two_rising_readings_land_an_exhaustAt_on_the_served_state(self):
        builder = crabd.StateBuilder(
            crabd.TranscriptStore(self.projects), crabd.HookTracker(),
            StubLimits(), self.T0)
        first = builder.build(now=self.T0, limits=self.limits(0.50))
        self.assertIsNone(first["limits"]["fiveHour"]["exhaustAt"])   # one sample
        second = builder.build(now=self.T0 + 60, limits=self.limits(0.60))
        exhaust = second["limits"]["fiveHour"]["exhaustAt"]
        self.assertIsNotNone(exhaust)
        exhaust_epoch = crabd._parse_ts(exhaust)
        self.assertGreater(exhaust_epoch, self.T0 + 60)              # after now
        self.assertLess(exhaust_epoch, self.T0 + 3600)              # before reset


# --------------------------------------------------------------------- git lookup

class GitLookupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_plain_repo(self):
        repo = self.root / "myrepo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
        (repo / "src").mkdir()
        self.assertEqual(crabd.GitLookup().get(str(repo / "src")), ("myrepo", "master"))

    def test_origin_remote_name_beats_the_folder_name(self):
        # The folder is a short local alias; `origin` carries the real repo name, and
        # a second remote must not win the tie-break.
        repo = self.root / "wk"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
        (repo / ".git" / "config").write_text(
            '[core]\n\tbare = false\n[remote "origin"]\n'
            '\turl = https://git.example.com/acme/ledger-service.git\n'
            '[remote "github"]\n\turl = git@github.com:someone/other.git\n',
            encoding="utf-8")
        self.assertEqual(crabd.GitLookup().get(str(repo)),
                         ("ledger-service", "master"))

    def test_ssh_style_remote_url(self):
        repo = self.root / "x"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (repo / ".git" / "config").write_text(
            '[remote "origin"]\n\turl = git@github.com:acme/sidecrab.git\n',
            encoding="utf-8")
        self.assertEqual(crabd.GitLookup().get(str(repo)), ("sidecrab", "main"))

    def test_detached_head_reports_a_short_sha(self):
        repo = self.root / "det"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "HEAD").write_text("a1b2c3d4e5f6a7b8c9d0\n", encoding="utf-8")
        self.assertEqual(crabd.GitLookup().get(str(repo)), ("det", "a1b2c3d4"))

    def test_worktree_gitdir_file_resolves_to_the_main_repo_name(self):
        main = self.root / "mainrepo"
        (main / ".git" / "worktrees" / "wt1").mkdir(parents=True)
        (main / ".git" / "worktrees" / "wt1" / "HEAD").write_text(
            "ref: refs/heads/feature/x\n", encoding="utf-8")
        tree = self.root / "wt1"
        tree.mkdir()
        (tree / ".git").write_text(
            f"gitdir: {main / '.git' / 'worktrees' / 'wt1'}\n", encoding="utf-8")
        self.assertEqual(crabd.GitLookup().get(str(tree)), ("mainrepo", "feature/x"))

    def test_no_repo_yields_nulls(self):
        plain = self.root / "nothing"
        plain.mkdir()
        self.assertEqual(crabd.GitLookup().get(str(plain)), (None, None))

    def test_missing_cwd_yields_nulls(self):
        self.assertEqual(crabd.GitLookup().get(None), (None, None))

    def test_a_slow_cwd_cannot_stall_the_caller_past_the_budget(self):
        """A-04 (v0.26.0). A cwd on an unreachable network path (a VPN drop, a NAS reboot,
        a stale mount) blocks every stat in _read on the SMB timeout - ~21 s - and that
        used to run ON the builder thread inside build(), re-blocking every 30 s. The probe
        now runs off the caller's critical path with a hard budget, so build() returns
        promptly and serves last-known/nulls; the worker fills the cache for the next pass.
        Bounds the OPERATION, not a path syntax - a slow local disk has the same shape.

        Mutation check: reverting get() to a synchronous self._read makes the first call
        below take the full 2 s sleep, failing the elapsed assertion."""
        lookup = crabd.GitLookup()
        cwd = "\\\\10.255.255.1\\share\\x"
        real_read = crabd.GitLookup._read

        def slow_read(path):
            time.sleep(2.0)                 # stands in for the SMB timeout / hung mount
            return ("faraway", "main")

        crabd.GitLookup._read = staticmethod(slow_read)
        try:
            start = time.time()
            result = lookup.get(cwd)
            elapsed = time.time() - start
            # The caller did NOT wait for the 2 s probe.
            self.assertLess(elapsed, crabd.GIT_READ_BUDGET_SEC + 0.6)
            self.assertEqual(result, (None, None))     # nulls now, not a stall
            # The background worker keeps running and fills the cache for the next pass.
            deadline = time.time() + 5.0
            while time.time() < deadline:
                with lookup._lock:
                    if cwd in lookup._cache:
                        break
                time.sleep(0.02)
            self.assertEqual(lookup.get(cwd), ("faraway", "main"))
        finally:
            crabd.GitLookup._read = staticmethod(real_read)

    def test_the_cache_is_a_bounded_lru(self):
        """A-06 (v0.26.0). _cache had NO eviction path: 20,000 distinct cwds -> 20,000
        entries forever, fed by the same unauthenticated hook `cwd` A-04 rides. Now a
        bounded LRU capped at GIT_CACHE_MAX. Non-repo paths resolve fast, so each get()
        completes within the budget synchronously."""
        lookup = crabd.GitLookup()
        for i in range(crabd.GIT_CACHE_MAX + 50):
            lookup.get(str(self.root / "nope" / str(i)))   # no .git anywhere -> fast miss
        with lookup._lock:
            self.assertLessEqual(len(lookup._cache), crabd.GIT_CACHE_MAX)


# ------------------------------------------------------------------- served payload

class ServeTests(TempProjects):
    """Serve /v1/state from a real socket and check it against the contract."""

    def setUp(self):
        super().setUp()
        now = time.time()
        write_jsonl(self.session_path("aaaaaaaa-0000-0000-0000-000000000001"), [
            user_line("build the widget", now - 120),
            {"type": "custom-title", "customTitle": "SideCrab build"},
            assistant_line("req_1", now - 90, output=120),
            assistant_line("req_1", now - 90, output=120),
            # The distinctive input side is the contextTokens fixture: 1000+200000+3000.
            assistant_line("req_2", now - 30, output=80,
                           inp=1000, cache_read=200000, cache_create=3000),
        ], mtime=now - 20)
        write_jsonl(self.projects / "C--IT" /
                    "aaaaaaaa-0000-0000-0000-000000000001" / "subagents" /
                    "agent-abc.jsonl",
                    # NEWER than req_2 and enormous: if a subagent's window could leak
                    # into the parent's contextTokens, this is the line that would do it.
                    [assistant_line("req_sub", now - 25, output=45,
                                    inp=7, cache_read=888888, cache_create=1)],
                    mtime=now - 20)

        self.hooks = crabd.HookTracker()
        self.builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                          self.hooks, StubLimits(), time.time(),
                                          host=StubHost())
        crabd.Handler.builder = self.builder
        # Bound, served, and PROVEN reachable before any assertion runs; ONE reused
        # connection for the whole test. Both halves are the determinism fix - see
        # _httpkeepalive for the netstat capture that motivated them.
        self.server, self.thread, self.port, self.client = start_test_server(
            lambda: crabd.CrabdServer(("127.0.0.1", 0), crabd.Handler))
        # Order matters: closing the socket before serve_forever stops raises
        # WinError 10038 out of the serving thread.
        self.addCleanup(self.stop_server)
        self.addCleanup(self.client.close)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def get(self, path):
        reply = self.client.get(path)
        return reply, reply.json()

    def post_hook(self, payload):
        return self.client.post("/v1/hook", json.dumps(payload).encode()).status

    def test_health(self):
        """`ok` and `version` keep their exact values - anything already probing health
        reads those two. The v0.14.0 counters are additive and asserted by shape."""
        response, body = self.get("/v1/health")
        self.assertEqual(response.status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["version"], crabd.VERSION)
        self.assertEqual(sorted(body),
                         ["hooksSeen", "lastStatuslineAgeSec", "ok", "originsSeen",
                          "otlpSeen", "panelToken", "statuslineSeen", "uptimeSec",
                          "version"])

    def test_state_matches_the_contract_shape(self):
        response, state = self.get("/v1/state")
        self.assertEqual(response.status, 200)
        # SEC-4 (v0.16.0): no ACAO at all for a client that sent no Origin. The
        # wildcard is gone from every response crabd emits - see Sec4ReadGateTests.
        self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
        # 5, not 6: the served number is the last BREAKING shape (STATE-CONTRACT.md,
        # "VERSIONING REWORK"). The v6-era fields below ride on it additively.
        self.assertEqual(state["schema"], 5)
        # An EXACT key set, not assertIn: the v0.9.0 REMOVAL section of
        # STATE-CONTRACT.md drops a top-level key, and a reintroduced one has to fail
        # here rather than ship to the store.
        self.assertEqual(sorted(state),
                         ["approvals", "burn", "continuePrompts", "crabd", "fleet", "generatedAt",
                          "host", "limits", "quiet", "recap", "schema", "sessions",
                          "toast"])
        # v0.22.0: the host's own CPU and memory, beside the iCUE temperature sensors.
        # PRESENCE is the feature detection and the key is OPTIONAL - this fixture
        # injects a sampler so the shape is pinned; a machine whose counters cannot be
        # read serves no `host` key at all (HostBlockThroughTheBuilderTests).
        self.assertEqual(sorted(state["host"]),
                         ["cpuPct", "memPct", "memTotalGB", "memUsedGB"])
        # v0.18.0: the toast settings, echoed for the settings sheet. Additive, so
        # `schema` stays 5. The OPTIONAL approvalThresholdSec is absent here because
        # this fixture's config never set one - see ServedToastBlockTests.
        self.assertEqual(state["toast"], {"thresholdSec": 120, "enabled": True})
        # v0.12.0: the operator's EXTRA continue buttons, always a list. The widget has
        # no way to read config.json, so the config-only key rides the feed.
        self.assertEqual(state["continuePrompts"], [])
        self.assertTrue(state["generatedAt"].endswith("Z"))
        self.assertEqual(sorted(state["crabd"]), ["hooksSeen", "startedAt", "version"])
        self.assertEqual(sorted(state["limits"]),
                         ["available", "extra", "fiveHour", "note", "rateLimitTier",
                          "source", "subscriptionType", "weekly"])
        # v0.12.0: provenance is not optional. No StatusLineReader is attached to this
        # builder, so the OAuth fallback is what produced the block and must say so.
        self.assertEqual(state["limits"]["source"], "oauth")
        self.assertEqual(sorted(state["burn"]),
                         ["byModel", "costSource", "costUSD", "daily", "hourly", "today"])
        # No OTLP receiver attached = no telemetry = null, NEVER a zeroed dollar figure.
        self.assertIsNone(state["burn"]["costUSD"])
        self.assertIsNone(state["burn"]["costSource"])
        self.assertEqual(sorted(state["burn"]["today"]),
                         ["cacheCreationTokens", "cacheReadTokens", "inputTokens",
                          "messages", "outputTokens"])
        self.assertEqual(len(state["burn"]["hourly"]), 24)
        self.assertEqual(len(state["burn"]["daily"]), 7)
        self.assertEqual(sorted(state["burn"]["daily"][0]), ["dayStart", "outputTokens"])
        # No RecapReader attached, so `recap` is null - never a zeroed
        # document that would read as "nothing happened today".
        self.assertIsNone(state["recap"])
        # No FleetReader attached is the "could not read it" case, and it serves
        # unknown for both components - never a pair of green dots.
        self.assertEqual(state["fleet"], {"glow": "unknown", "toast": "unknown"})

    def test_session_row_matches_the_contract(self):
        _, state = self.get("/v1/state")
        self.assertEqual(len(state["sessions"]), 1)
        row = state["sessions"][0]
        self.assertEqual(sorted(row),
                         ["acked", "branch", "contextSource", "contextTokens",
                          "contextWindowTokens", "cwd",
                          "events", "id", "lastActivityAt", "lastEvent", "model",
                          "pendingPermission", "question", "queuedContinue", "repo",
                          "speed", "state", "stateSince", "subagentDetail", "subagents",
                          "title", "titleSource", "todayOutputTokens", "turnStartedAt"])
        # v0.12.0: no status line has spoken, so the ctx figure is the transcript
        # arithmetic and says so; nothing is waiting on a panel approval.
        self.assertEqual(row["contextSource"], "transcript")
        self.assertIsNone(row["pendingPermission"])
        # v0.14.0: present and null, like pendingPermission. The KEY is the widget's
        # feature detection, so it rides even on a builder with no queue attached.
        self.assertIsNone(row["queuedContinue"])
        # v0.28.0, same idiom: the model id is unmarked, no status line has spoken and
        # no catalog is attached, so the denominator is unknown - PRESENT and null, so
        # the widget draws no bar rather than gauging against an invented window.
        self.assertIsNone(row["contextWindowTokens"])
        self.assertEqual(row["title"], "SideCrab build")
        self.assertEqual(row["model"], "claude-fable-5")
        self.assertEqual(row["speed"], "standard")
        self.assertEqual(row["state"], "working")
        self.assertEqual(row["subagents"], {"running": 1, "total": 1})
        # 120 + 80 from the parent (req_1 deduped) + 45 from the subagent.
        self.assertEqual(row["todayOutputTokens"], 245)
        # The newest MAIN record's input side; the newer subagent record is not it.
        self.assertEqual(row["contextTokens"], 204000)

    def test_hook_post_is_204_and_moves_the_session(self):
        session_id = "aaaaaaaa-0000-0000-0000-000000000001"
        self.assertEqual(self.post_hook({
            "session_id": session_id, "hook_event_name": "Notification",
            "cwd": "C:\\IT", "message": "Claude needs your permission to use Bash"}), 204)
        self.builder.build()
        _, state = self.get("/v1/state")
        row = next(r for r in state["sessions"] if r["id"] == session_id)
        self.assertEqual(row["state"], "needs_input")
        self.assertEqual(row["lastEvent"], "Claude needs your permission to use Bash")
        self.assertEqual(state["crabd"]["hooksSeen"], 1)

    def test_a_malformed_hook_body_is_swallowed(self):
        self.assertEqual(self.client.post("/v1/hook", b"not json").status, 204)
        # /v1/hook answers before it parses as well; a second, VALID hook is the barrier
        # - once its count lands, the malformed one has certainly been looked at.
        self.assertEqual(self.post_hook({"session_id": "aaaaaaaa-0000-0000-0000-000000000001",
                                         "hook_event_name": "SessionStart"}), 204)
        settle(lambda: self.builder.hooks.count == 1, what="the following valid hook")
        _, state = self.get("/v1/state")
        self.assertEqual(state["crabd"]["hooksSeen"], 1)

    def test_sessions_are_sorted_needs_input_then_working_then_idle(self):
        now = time.time()
        write_jsonl(self.session_path("bbbbbbbb-0000-0000-0000-000000000002"),
                    [assistant_line("req_b", now - 3000, output=5)],
                    mtime=now - (crabd.IDLE_AFTER_SEC + 300))
        write_jsonl(self.session_path("cccccccc-0000-0000-0000-000000000003"),
                    [assistant_line("req_c", now - 60, output=5)], mtime=now - 30)
        self.post_hook({"session_id": "cccccccc-0000-0000-0000-000000000003",
                        "hook_event_name": "Notification", "cwd": "C:\\IT",
                        "message": "waiting"})
        self.builder.build()
        _, state = self.get("/v1/state")
        self.assertEqual([r["state"] for r in state["sessions"]],
                         ["needs_input", "working", "idle"])

    def test_needs_input_row_is_kept_even_when_it_falls_out_of_the_window(self):
        stale_id = "dddddddd-0000-0000-0000-000000000004"
        now = time.time()
        write_jsonl(self.session_path(stale_id),
                    [assistant_line("req_d", now - 4 * 3600, output=5)],
                    mtime=now - 4 * 3600)
        self.post_hook({"session_id": stale_id, "hook_event_name": "Notification",
                        "cwd": "C:\\IT", "message": "still waiting on you"})
        self.builder.build()
        _, state = self.get("/v1/state")
        row = next(r for r in state["sessions"] if r["id"] == stale_id)
        self.assertEqual(row["state"], "needs_input")

    def test_gone_sessions_are_excluded(self):
        session_id = "aaaaaaaa-0000-0000-0000-000000000001"
        self.post_hook({"session_id": session_id, "hook_event_name": "SessionEnd",
                        "cwd": "C:\\IT"})
        self.builder.build()
        _, state = self.get("/v1/state")
        self.assertNotIn(session_id, [r["id"] for r in state["sessions"]])

    def test_no_credential_material_reaches_the_payload(self):
        """The token must never be serialisable out of crabd."""
        _, state = self.get("/v1/state")
        blob = json.dumps(state).lower()
        for forbidden in ("accesstoken", "refreshtoken", "bearer", "sk-ant", "authorization"):
            self.assertNotIn(forbidden, blob)

    def test_unknown_paths_are_404(self):
        reply = self.client.get("/v1/secrets")
        self.assertEqual(reply.status, 404)
        self.assertEqual(reply.json(), {"error": "not found"})


# ------------------------------------------------------------------ schema 2: question

class QuestionCaptureTests(TempProjects):
    """The transcript half of `question` - what FileFacts pulls out of the tail."""

    def _facts(self, objects):
        path = self.session_path("s-q")
        write_jsonl(path, objects)
        facts = crabd.FileFacts(path, "s-q", False)
        facts.refresh()
        return facts

    def test_ask_user_question_fields_are_pinned(self):
        now = time.time()
        facts = self._facts([ask_line(now, ["Should crabd ship the ack endpoint?"])])
        self.assertEqual(facts.question, "Should crabd ship the ack endpoint?")
        self.assertEqual(facts.question_rank, 2)

    def test_multiple_questions_are_joined(self):
        now = time.time()
        facts = self._facts([ask_line(now, ["First thing?", "Second thing?"])])
        self.assertEqual(facts.question, "First thing? · Second thing?")

    def test_trailing_assistant_question_is_the_last_line_only(self):
        now = time.time()
        facts = self._facts([assistant_text_line(
            "req_t", now,
            "I read the contract and measured the transcripts.\n"
            "Everything checks out so far.\n"
            "Do you want me to ship it?")])
        self.assertEqual(facts.question, "Do you want me to ship it?")

    def test_assistant_text_not_ending_in_a_question_is_ignored(self):
        now = time.time()
        facts = self._facts([assistant_text_line("req_t", now, "All done. Shipped it.")])
        self.assertIsNone(facts.question)

    def test_ask_user_question_beats_a_trailing_question_in_the_same_turn(self):
        now = time.time()
        facts = self._facts([
            assistant_text_line("req_t", now, "Which one do you want?"),
            ask_line(now, ["The real structured question?"]),
        ])
        self.assertEqual(facts.question, "The real structured question?")

    def test_a_newer_question_replaces_an_older_one(self):
        now = time.time()
        facts = self._facts([
            ask_line(now - 600, ["The old question?"]),
            ask_line(now, ["The new question?"], tool_id="toolu_ask2"),
        ])
        self.assertEqual(facts.question, "The new question?")

    def test_question_is_capped_keeping_the_question_mark(self):
        now = time.time()
        facts = self._facts([ask_line(now, ["z" * 900 + " really?"])])
        self.assertEqual(len(facts.question), crabd.QUESTION_MAX)
        self.assertTrue(facts.question.endswith("really?"))
        self.assertTrue(facts.question.startswith("…"))


class QuestionPrecedenceTests(TempProjects):
    """The served field: hook message vs transcript enrichment vs staleness."""

    SID = "eeeeeeee-0000-0000-0000-000000000005"

    def _serve(self, transcript_lines, hook_message, transcript_mtime=None):
        now = time.time()
        write_jsonl(self.session_path(self.SID), transcript_lines,
                    mtime=transcript_mtime if transcript_mtime is not None else now - 5)
        hooks = crabd.HookTracker()
        hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                      "cwd": "C:\\IT", "message": hook_message})
        _, state = self.build(now=now, hooks=hooks)
        return next(r for r in state["sessions"] if r["id"] == self.SID)

    def test_hook_message_is_kept_in_full_not_truncated_like_last_event(self):
        message = ("Claude needs your permission to use Bash to run a command that "
                   "rewrites the provisioning worker, and here is a great deal more "
                   "context than the short lastEvent line has room for at all.")
        row = self._serve([user_line("hi", time.time() - 60)], message)
        self.assertEqual(row["question"], message)
        self.assertLess(len(row["lastEvent"]), len(row["question"]))
        self.assertLessEqual(len(row["lastEvent"]), crabd.EVENT_MAX)

    def test_richer_transcript_question_wins_over_a_short_hook_message(self):
        now = time.time()
        rich = ("Which queue naming scheme should the print service use when it "
                "replaces the legacy queues on the shared copiers?")
        row = self._serve([ask_line(now - 3, [rich])], "Claude is waiting for input")
        self.assertEqual(row["question"], rich)

    def test_a_shorter_transcript_question_does_not_displace_the_hook(self):
        now = time.time()
        message = ("Claude needs your permission to use Bash: git push --force to "
                   "origin/master, which rewrites published history")
        row = self._serve([ask_line(now - 3, ["ok?"])], message)
        self.assertEqual(row["question"], message)

    def test_a_stale_transcript_question_is_not_resurrected(self):
        now = time.time()
        old = "A question from three turns ago that was already answered, at length?"
        row = self._serve([ask_line(now - crabd.QUESTION_FRESH_SEC * 4, [old])],
                          "waiting", transcript_mtime=now - 5)
        self.assertEqual(row["question"], "waiting")

    def test_question_is_null_when_the_session_is_not_waiting(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID), [ask_line(now - 3, ["anything?"])],
                    mtime=now - 5)
        hooks = crabd.HookTracker()
        hooks.record({"session_id": self.SID, "hook_event_name": "UserPromptSubmit",
                      "cwd": "C:\\IT"})
        _, state = self.build(now=now, hooks=hooks)
        row = next(r for r in state["sessions"] if r["id"] == self.SID)
        self.assertEqual(row["state"], "working")
        self.assertIsNone(row["question"])


# ------------------------------------------------------------- schema 2: turnStartedAt

class TurnStartedAtTests(TempProjects):
    SID = "ffffffff-0000-0000-0000-000000000006"

    def test_hook_lifecycle_sets_and_clears_it(self):
        hooks = crabd.HookTracker()
        send = lambda ev: hooks.record({"session_id": "s1", "hook_event_name": ev,
                                        "cwd": "C:\\IT"})
        send("SessionStart")
        self.assertIsNone(hooks.snapshot()["s1"]["turn_started"])
        send("UserPromptSubmit")
        started = hooks.snapshot()["s1"]["turn_started"]
        self.assertIsNotNone(started)
        send("Notification")
        self.assertEqual(hooks.snapshot()["s1"]["turn_started"], started)  # still this turn
        send("Stop")
        self.assertIsNone(hooks.snapshot()["s1"]["turn_started"])
        send("UserPromptSubmit")
        self.assertIsNotNone(hooks.snapshot()["s1"]["turn_started"])
        send("SessionEnd")
        self.assertIsNone(hooks.snapshot()["s1"]["turn_started"])

    def _row(self, events, mtime_age=5.0):
        now = time.time()
        write_jsonl(self.session_path(self.SID),
                    [assistant_line("req_x", now - 60)], mtime=now - mtime_age)
        hooks = crabd.HookTracker()
        for event in events:
            hooks.record({"session_id": self.SID, "hook_event_name": event,
                          "cwd": "C:\\IT"})
        # Backdate the hook clock too: a killed terminal fires nothing more, so the
        # last hook is as old as the transcript, not as old as this test run.
        row = hooks.sessions[self.SID]
        row["at"] = min(row["at"], now - mtime_age)
        row["since"] = min(row["since"], now - mtime_age)
        _, state = self.build(now=now, hooks=hooks)
        return next(r for r in state["sessions"] if r["id"] == self.SID)

    def test_served_as_iso_while_working(self):
        row = self._row(["UserPromptSubmit"])
        self.assertEqual(row["state"], "working")
        self.assertTrue(row["turnStartedAt"].endswith("Z"))

    def test_served_null_after_a_stop(self):
        row = self._row(["UserPromptSubmit", "Stop"])
        self.assertIsNone(row["turnStartedAt"])

    def test_an_aged_out_turn_is_not_still_running(self):
        """No Stop hook ever fired (killed terminal). The row ages to idle, and a
        turnStartedAt left standing would render as "working 3h" forever."""
        row = self._row(["UserPromptSubmit"], mtime_age=crabd.IDLE_AFTER_SEC + 300)
        self.assertEqual(row["state"], "idle")
        self.assertIsNone(row["turnStartedAt"])


# --------------------------------------------------------------------- schema 2: acked

class AckTests(TempProjects):
    SID = "11111111-0000-0000-0000-000000000007"

    def _hooks(self):
        hooks = crabd.HookTracker()
        hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                      "cwd": "C:\\IT", "message": "waiting on you"})
        return hooks

    def test_ack_sets_the_flag(self):
        hooks = self._hooks()
        self.assertTrue(hooks.ack(self.SID))
        self.assertTrue(hooks.snapshot()[self.SID]["acked"])

    def test_ack_of_an_unknown_session_is_refused(self):
        self.assertFalse(crabd.HookTracker().ack("nobody"))

    def test_a_state_transition_clears_the_ack(self):
        hooks = self._hooks()
        hooks.ack(self.SID)
        hooks.record({"session_id": self.SID, "hook_event_name": "Stop", "cwd": "C:\\IT"})
        self.assertFalse(hooks.snapshot()[self.SID]["acked"])

    def test_the_same_question_re_fired_does_not_clear_the_ack(self):
        """THE HEALTHY-NIGHT GUARD for v0.20.0's re-fire rule. Claude Code re-fires
        Notification for a prompt the operator has walked away from, so a rule that
        un-acked on every Notification would un-ack an acknowledged card forever."""
        hooks = self._hooks()
        hooks.ack(self.SID)
        before = hooks.snapshot()[self.SID]["since"]
        for _ in range(5):
            hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                          "cwd": "C:\\IT", "message": "waiting on you"})
        row = hooks.snapshot()[self.SID]
        self.assertTrue(row["acked"])
        self.assertEqual(row["since"], before)

    def test_acked_is_served_on_the_row(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID),
                    [assistant_line("req_a", now - 60)], mtime=now - 5)
        hooks = self._hooks()
        hooks.ack(self.SID)
        _, state = self.build(now=now, hooks=hooks)
        row = next(r for r in state["sessions"] if r["id"] == self.SID)
        self.assertTrue(row["acked"])


# ------------------------------------- v0.19.0: needs_input answered in the app

class AnsweredInTheAppTests(TempProjects):
    """The operator-reported gap, replayed end to end through build().

    The maintainer answers a waiting session in the Claude Code desktop app and the panel keeps
    alerting. Every case here is a REPLAY of a real event sequence, because the property
    that matters is not "does the clear work" but "can it ever fire while the question
    genuinely still stands".
    """

    SID = "77777777-0000-0000-0000-000000000009"
    OTHER = "88888888-0000-0000-0000-000000000010"

    def waiting(self, hooks, since, session_id=None,
                message="Claude needs your permission to use Bash"):
        """Put a session on needs_input as of `since`, the way a Notification does."""
        session_id = session_id or self.SID
        hooks.record({"session_id": session_id, "hook_event_name": "Notification",
                      "cwd": "C:\\IT", "message": message})
        row = hooks.sessions[session_id]
        row["since"] = since
        row["at"] = since
        return hooks

    def row(self, state, session_id=None):
        return next(r for r in state["sessions"] if r["id"] == (session_id or self.SID))

    def test_a_permission_allowed_in_the_app_clears_the_alert(self):
        """Notification (permission) -> the maintainer clicks Allow in the app -> the tool runs and
        the model is called again. No hook fires at decision time; the round-trip is the
        only evidence there is, and it is enough."""
        now = time.time()
        hooks = self.waiting(crabd.HookTracker(), now - 300)
        write_jsonl(self.session_path(self.SID), [
            assistant_line("req_ask", now - 310, output=20),   # the turn that asked
            assistant_line("req_next", now - 20, output=40),   # the turn after Allow
        ], mtime=now - 20)
        _, state = self.build(now=now, hooks=hooks)
        row = self.row(state)
        self.assertEqual(row["state"], "working")
        self.assertIsNone(row["question"])
        self.assertEqual(row["lastEvent"], "working")

    def test_a_question_that_still_stands_keeps_alerting(self):
        """THE healthy-night case. The transcript carries only the round-trip that ASKED,
        so nothing after it can be read as an answer - and a question waits forever."""
        now = time.time()
        hooks = self.waiting(crabd.HookTracker(), now - 1800,
                             message="Which branch should I use?")
        write_jsonl(self.session_path(self.SID),
                    [ask_line(now - 1810, ["Which branch should I use?"])],
                    mtime=now - 1805)
        _, state = self.build(now=now, hooks=hooks)
        row = self.row(state)
        self.assertEqual(row["state"], "needs_input")
        self.assertEqual(row["question"], "Which branch should I use?")

    def test_an_askuserquestion_pick_clears_it_with_no_prompt_submitted(self):
        """A pick on the AskUserQuestion sheet comes back as a tool_result, so
        UserPromptSubmit never fires. The model runs again anyway, and that is what the
        panel learns from."""
        now = time.time()
        hooks = self.waiting(crabd.HookTracker(), now - 120,
                             message="Which branch should I use?")
        write_jsonl(self.session_path(self.SID), [
            ask_line(now - 130, ["Which branch should I use?"]),
            assistant_line("req_after_pick", now - 15, output=30),
        ], mtime=now - 15)
        _, state = self.build(now=now, hooks=hooks)
        self.assertEqual(self.row(state)["state"], "working")

    def test_a_running_subagent_never_clears_the_main_sessions_question(self):
        """A background subagent writes its own transcript the whole time the operator is
        being waited on. Aggregating it would clear a question nobody answered."""
        now = time.time()
        hooks = self.waiting(crabd.HookTracker(), now - 600)
        write_jsonl(self.session_path(self.SID),
                    [assistant_line("req_ask", now - 610, output=20)], mtime=now - 605)
        write_jsonl(self.projects / "C--IT" / self.SID / "subagents" / "agent-abc.jsonl",
                    [assistant_line("req_sub", now - 5, output=99)], mtime=now - 5)
        _, state = self.build(now=now, hooks=hooks)
        row = self.row(state)
        self.assertEqual(row["state"], "needs_input")
        self.assertEqual(row["subagents"]["running"], 1)

    def test_another_sessions_activity_never_clears_this_one(self):
        now = time.time()
        hooks = self.waiting(crabd.HookTracker(), now - 600)
        write_jsonl(self.session_path(self.SID),
                    [assistant_line("req_ask", now - 610, output=20)], mtime=now - 605)
        hooks.record({"session_id": self.OTHER, "hook_event_name": "UserPromptSubmit",
                      "cwd": "C:\\IT"})
        write_jsonl(self.session_path(self.OTHER),
                    [assistant_line("req_other", now - 5, output=50)], mtime=now - 5)
        _, state = self.build(now=now, hooks=hooks)
        self.assertEqual(self.row(state)["state"], "needs_input")
        self.assertEqual(self.row(state, self.OTHER)["state"], "working")

    def test_the_clear_reaches_the_rows_this_very_build_serves(self):
        """Ordering: the clear is applied BEFORE hooks.snapshot(). Applied after, it
        would miss the copy this build serves and the panel would alert one more poll."""
        now = time.time()
        hooks = self.waiting(crabd.HookTracker(), now - 300)
        write_jsonl(self.session_path(self.SID),
                    [assistant_line("req_next", now - 20, output=40)], mtime=now - 20)
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects), hooks,
                                     StubLimits(), time.time())
        self.assertEqual(self.row(builder.build(now=now))["state"], "working")

    def test_a_session_with_no_usage_record_yet_is_left_alone(self):
        """turn_ts 0.0 is "no round-trip has ever been parsed", not "a round-trip at the
        epoch". It must never be compared against `since` as if it were activity."""
        now = time.time()
        hooks = self.waiting(crabd.HookTracker(), now - 300)
        write_jsonl(self.session_path(self.SID), [user_line("go", now - 310)],
                    mtime=now - 10)
        _, state = self.build(now=now, hooks=hooks)
        self.assertEqual(self.row(state)["state"], "needs_input")

    def test_a_new_question_after_a_clear_re_alerts(self):
        """The full round trip on one session: asked, answered in the app, asked again."""
        now = time.time()
        hooks = self.waiting(crabd.HookTracker(), now - 300)
        hooks.ack(self.SID)
        write_jsonl(self.session_path(self.SID),
                    [assistant_line("req_next", now - 60, output=40)], mtime=now - 60)
        _, state = self.build(now=now, hooks=hooks)
        self.assertEqual(self.row(state)["state"], "working")
        hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                      "cwd": "C:\\IT", "message": "and one more thing?"})
        _, state = self.build(now=now, hooks=hooks)
        row = self.row(state)
        self.assertEqual(row["state"], "needs_input")
        self.assertFalse(row["acked"])
        self.assertEqual(row["question"], "and one more thing?")


# ------------------------------- v0.20.0: a re-fired Notification on an alerting card

class ReFiredNotificationTests(unittest.TestCase):
    """A second Notification arriving while the row is ALREADY needs_input.

    The old rule was `row["state"] != state`, which is false for a re-fire - so a NEW
    question landed on a card whose `since` still dated the FIRST one and whose `acked`
    was still set. The widget escalates on `stateSince` and silences on `acked`, so the
    second question of a turn arrived pre-silenced on a card already deep red.

    The rule is the question TEXT, and the two halves are tested against each other:
    a different question is a new alert, an identical one is the CLI re-firing for a
    prompt nobody has answered yet and must change nothing.
    """

    SID = "aaaa1111-0000-0000-0000-00000000001a"

    def notify(self, hooks, message, at=None):
        hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                      "cwd": "C:\\IT", "message": message})
        if at is not None:
            hooks.sessions[self.SID]["since"] = at
        return hooks.snapshot()[self.SID]

    def test_a_new_question_resets_the_clock_and_un_acks(self):
        hooks = crabd.HookTracker()
        self.notify(hooks, "Which branch?", at=1.0)
        hooks.ack(self.SID)
        row = self.notify(hooks, "Actually - deploy to prod?")
        self.assertFalse(row["acked"])
        self.assertGreater(row["since"], 1.0)
        self.assertEqual(row["question"], "Actually - deploy to prod?")
        self.assertEqual(row["state"], "needs_input")

    def test_the_ack_after_the_new_question_still_sticks(self):
        """The other order. The reset is a property of the QUESTION arriving, not of the
        ack being older than it - an ack that lands afterwards must not be undone."""
        hooks = crabd.HookTracker()
        self.notify(hooks, "Which branch?")
        self.notify(hooks, "Actually - deploy to prod?")
        hooks.ack(self.SID)
        row = self.notify(hooks, "Actually - deploy to prod?")
        self.assertTrue(row["acked"])

    def test_a_re_fire_of_the_standing_question_changes_nothing(self):
        """THE HEALTHY-NIGHT REPLAY. Claude Code re-fires Notification for a prompt the
        operator has walked away from; on a fleet where that happens all night, a rule
        that reset on every one of them would un-ack every acknowledged card, forever."""
        hooks = crabd.HookTracker()
        self.notify(hooks, "Which branch?", at=1.0)
        hooks.ack(self.SID)
        for _ in range(20):
            row = self.notify(hooks, "Which branch?")
        self.assertTrue(row["acked"])
        self.assertEqual(row["since"], 1.0)

    def test_a_notification_with_no_message_is_not_a_new_question_every_time(self):
        """Both sides None. Absence must compare EQUAL to absence, or every message-less
        Notification would read as a fresh question."""
        hooks = crabd.HookTracker()
        hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                      "cwd": "C:\\IT"})
        hooks.sessions[self.SID]["since"] = 1.0
        hooks.ack(self.SID)
        hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                      "cwd": "C:\\IT"})
        row = hooks.snapshot()[self.SID]
        self.assertTrue(row["acked"])
        self.assertEqual(row["since"], 1.0)


class ReFiredNotificationOnTheServedRowTests(TempProjects):
    """The same rule seen where the widget sees it - stateSince and acked on the row."""

    SID = "aaaa2222-0000-0000-0000-00000000002a"

    def test_the_served_row_re_alerts_at_full_strength(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID),
                    [assistant_line("req_ask", now - 900, output=20)], mtime=now - 890)
        hooks = crabd.HookTracker()
        hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                      "cwd": "C:\\IT", "message": "Which branch?"})
        hooks.sessions[self.SID]["since"] = now - 900
        hooks.ack(self.SID)
        _, state = self.build(now=now, hooks=hooks)
        first = next(r for r in state["sessions"] if r["id"] == self.SID)
        self.assertTrue(first["acked"])

        hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                      "cwd": "C:\\IT", "message": "Deploy to prod?"})
        _, state = self.build(now=now, hooks=hooks)
        row = next(r for r in state["sessions"] if r["id"] == self.SID)
        self.assertFalse(row["acked"])
        self.assertEqual(row["question"], "Deploy to prod?")
        self.assertGreater(crabd._parse_ts(row["stateSince"]),
                           crabd._parse_ts(first["stateSince"]))


# ------------------- v0.20.0: a PermissionRequest is a session waiting on the operator

class PermissionStateMachineTests(unittest.TestCase):
    """note_permission / clear_permission, the state-machine half of the hold.

    THE GAP: `needs_input` was set by the Notification hook and by nothing else, so a
    session sitting on a live permission dialog read `working` - and the panel renders
    Approve / Deny off the needs_input sheet, so the card carrying the pendingPermission
    could be the one card not offering it.
    """

    SID = "bbbb1111-0000-0000-0000-00000000001b"
    QUESTION = crabd.PERMISSION_QUESTION % "Bash"

    def working(self):
        hooks = crabd.HookTracker()
        hooks.record({"session_id": self.SID, "hook_event_name": "UserPromptSubmit",
                      "cwd": "C:\\IT"})
        return hooks

    def test_a_permission_request_moves_a_working_session_to_needs_input(self):
        hooks = self.working()
        self.assertTrue(hooks.note_permission(self.SID, self.QUESTION, time.time()))
        row = hooks.snapshot()[self.SID]
        self.assertEqual(row["state"], "needs_input")
        self.assertEqual(row["question"], self.QUESTION)
        self.assertFalse(row["acked"])

    def test_a_session_crabd_has_no_hook_for_is_raised_too(self):
        """The caller has already gated on builder.serving, the same rule ack uses."""
        hooks = crabd.HookTracker()
        self.assertTrue(hooks.note_permission(self.SID, self.QUESTION, time.time()))
        self.assertEqual(hooks.snapshot()[self.SID]["state"], "needs_input")

    def test_the_hold_ending_stands_the_card_down(self):
        hooks = self.working()
        hooks.note_permission(self.SID, self.QUESTION, time.time())
        self.assertTrue(hooks.clear_permission(self.SID, time.time()))
        row = hooks.snapshot()[self.SID]
        self.assertEqual(row["state"], "working")
        self.assertIsNone(row["question"])
        self.assertFalse(row["permission_alert"])

    def test_a_notification_alert_is_NOT_the_holds_to_clear(self):
        """THE HEALTHY-NIGHT GUARD. A hold merely expiring is not an answer, and a
        question a Notification raised is still genuinely waiting - clearing it here
        would silence the one signal the panel exists for."""
        hooks = crabd.HookTracker()
        hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                      "cwd": "C:\\IT", "message": "Which branch?"})
        self.assertFalse(hooks.note_permission(self.SID, self.QUESTION, time.time()))
        self.assertFalse(hooks.clear_permission(self.SID, time.time()))
        row = hooks.snapshot()[self.SID]
        self.assertEqual(row["state"], "needs_input")
        self.assertEqual(row["question"], "Which branch?")

    def test_the_cli_notification_for_the_same_dialog_does_not_reset_the_clock(self):
        """The CLI fires its OWN Notification for the dialog, word for word. Two hooks,
        one prompt - the identical text must not RESET the card's clock (`since` and `acked`
        stay as they were, so one prompt does not escalate twice).

        A-02 (v0.26.0): but the Notification IS itself a genuine waiting question, so it
        TAKES OWNERSHIP of the alert - `permission_alert` is relinquished. From here the
        hold merely expiring is no longer an answer and must NOT stand the card down. This
        is the fix's whole point: it makes the outcome INDEPENDENT of which hook of the
        identical pair the CLI happened to emit first (unmeasured while approvals are off).
        The previous assertion here - permission_alert still True, so the hold clears it -
        was the A-02 defect itself (audit-0424)."""
        hooks = self.working()
        hooks.note_permission(self.SID, self.QUESTION, 1000.0)
        hooks.sessions[self.SID]["since"] = 1000.0
        hooks.sessions[self.SID]["acked"] = True
        hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                      "cwd": "C:\\IT", "message": self.QUESTION})
        row = hooks.snapshot()[self.SID]
        # Dedup preserved: the identical text moved neither the clock nor the ack.
        self.assertEqual(row["since"], 1000.0)
        self.assertTrue(row["acked"])
        # A-02: the Notification now co-owns the alert, so the hold ending leaves it standing
        # rather than clearing it. Mutation check: dropping the transfer serves 'working'.
        self.assertFalse(row["permission_alert"])
        self.assertFalse(hooks.clear_permission(self.SID, time.time()))
        self.assertEqual(hooks.snapshot()[self.SID]["state"], "needs_input")

    def test_a_different_notification_takes_the_alert_away_from_the_hold(self):
        """A real second question arriving during the hold owns the card from then on,
        so the hold expiring must leave it standing."""
        hooks = self.working()
        hooks.note_permission(self.SID, self.QUESTION, time.time())
        hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                      "cwd": "C:\\IT", "message": "And which branch?"})
        self.assertFalse(hooks.snapshot()[self.SID]["permission_alert"])
        self.assertFalse(hooks.clear_permission(self.SID, time.time()))
        self.assertEqual(hooks.snapshot()[self.SID]["state"], "needs_input")

    def test_a_finished_session_is_never_resurrected_as_alerting(self):
        """A Stop and a PermissionRequest for one session race in the wild. A dialog
        cannot be open in a turn the hooks say has ended."""
        for event, expected in (("Stop", "done"), ("SessionEnd", "gone")):
            with self.subTest(event=event):
                hooks = self.working()
                hooks.record({"session_id": self.SID, "hook_event_name": event,
                              "cwd": "C:\\IT"})
                self.assertFalse(
                    hooks.note_permission(self.SID, self.QUESTION, time.time()))
                self.assertFalse(hooks.clear_permission(self.SID, time.time()))
                self.assertEqual(hooks.snapshot()[self.SID]["state"], expected)

    def test_the_in_app_answer_clears_a_permission_raised_alert(self):
        """The third resolution path: the operator clicks Allow in the terminal, the tool
        runs, the model is called again. v0.19.0's turn clock is what sees it, and it must
        see a permission-raised alert exactly as it sees a Notification-raised one."""
        hooks = self.working()
        hooks.note_permission(self.SID, self.QUESTION, 1000.0)
        hooks.sessions[self.SID]["since"] = 1000.0
        self.assertTrue(hooks.note_activity(
            self.SID, 1000.0 + crabd.NEEDS_INPUT_ACTIVITY_GRACE_SEC + 1))
        row = hooks.snapshot()[self.SID]
        self.assertEqual(row["state"], "working")
        self.assertFalse(row["permission_alert"])

    def test_the_identical_notification_ends_the_alert_in_BOTH_hook_orders(self):
        """A-02 (P1, v0.26.0). PERMISSION_QUESTION is word-for-word the CLI's own
        Notification for the same dialog, and the two hooks fire ~a second apart. The CLI's
        actual emission order is UNMEASURED (approvals are off on the live host), so the
        outcome must not depend on it. Both orders end the same way: once the Notification
        has landed on the row, the Notification co-owns the alert, and the hold merely
        expiring is not an answer - the card stays needs_input.

        notify-first was already correct; perm-first was the A-02 defect (the identical
        Notification hit `record` with `moved` False, so the old moved-gated reset of
        permission_alert never ran, and the hold expiry then stood the card down). Both are
        pinned here so neither can regress."""
        def perm_hook(hooks):
            hooks.note_permission(self.SID, self.QUESTION, time.time())
        def notify_hook(hooks):
            hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                          "cwd": "C:\\IT", "message": self.QUESTION})

        for order_name, first, second in (("notify-first", notify_hook, perm_hook),
                                          ("perm-first", perm_hook, notify_hook)):
            with self.subTest(order=order_name):
                hooks = self.working()
                first(hooks)
                second(hooks)
                self.assertEqual(hooks.snapshot()[self.SID]["state"], "needs_input")
                self.assertFalse(hooks.snapshot()[self.SID]["permission_alert"])
                # The hold expiring is NOT this alert's to clear in either order.
                self.assertFalse(hooks.clear_permission(self.SID, time.time()))
                self.assertEqual(hooks.snapshot()[self.SID]["state"], "needs_input")

    def test_a_notification_for_a_different_session_leaves_this_row_alone(self):
        """A-02 healthy-night guard: record() operates on its OWN session's row, so a
        Notification for some other session must not relinquish THIS card's alert."""
        hooks = self.working()
        hooks.note_permission(self.SID, self.QUESTION, time.time())
        other = "cccc9999-0000-0000-0000-00000000009c"
        hooks.record({"session_id": other, "hook_event_name": "Notification",
                      "cwd": "C:\\IT", "message": "unrelated question"})
        self.assertTrue(hooks.snapshot()[self.SID]["permission_alert"])
        # ...so the hold ending still clears THIS card, untouched by the other session.
        self.assertTrue(hooks.clear_permission(self.SID, time.time()))
        self.assertEqual(hooks.snapshot()[self.SID]["state"], "working")

    def test_the_permission_stand_down_writes_a_ring_event(self):
        """A-10 (v0.26.0). The stand-down was silent, so an alert being dropped left no
        trace in `events` or history - the thing that would make an A-01/A-02-class mis-clear
        undiagnosable. Mutation check: removing the _note_event in clear_permission leaves
        the events ring unchanged across the stand-down."""
        hooks = self.working()
        hooks.note_permission(self.SID, self.QUESTION, time.time())
        before = [e["text"] for e in hooks.snapshot()[self.SID]["events"]]
        self.assertNotIn(crabd.PERMISSION_CLEARED_EVENT, before)
        self.assertTrue(hooks.clear_permission(self.SID, time.time()))
        after = [e["text"] for e in hooks.snapshot()[self.SID]["events"]]
        self.assertEqual(after[0], crabd.PERMISSION_CLEARED_EVENT)

    def test_a_permission_alert_is_not_a_served_field(self):
        """Internal bookkeeping. It rides the snapshot because snapshot copies the row,
        and it must never reach the contract."""
        hooks = self.working()
        hooks.note_permission(self.SID, self.QUESTION, time.time())
        builder = crabd.StateBuilder(crabd.TranscriptStore(Path(tempfile.gettempdir())),
                                     hooks, StubLimits(), time.time())
        for row in builder.build().get("sessions", []):
            self.assertNotIn("permission_alert", row)
            self.assertNotIn("permissionAlert", row)


# ------------------------------------------------------------ schema 2: subagentDetail

class SubagentDetailTests(TempProjects):
    SID = "22222222-0000-0000-0000-000000000008"

    def _sub(self, now, agent_id, age, text="a subagent brief that runs on and on"):
        path = (self.projects / "C--IT" / self.SID / "subagents" /
                f"agent-{agent_id}.jsonl")
        write_jsonl(path, [user_line(text, now - age)], mtime=now - age)

    def test_label_prefers_the_agent_tool_use_description(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID),
                    [user_line("go", now - 300)] +
                    agent_launch_lines(now - 200, "crabd v0.2.0 lane", "a0b4bd4afffc19fd7"),
                    mtime=now - 5)
        self._sub(now, "a0b4bd4afffc19fd7", 10)
        _, state = self.build(now=now)
        row = next(r for r in state["sessions"] if r["id"] == self.SID)
        self.assertEqual(row["subagentDetail"],
                         [{"label": "crabd v0.2.0 lane", "ageSec": 10}])

    def test_label_falls_back_to_the_launch_prompt_excerpt(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID), [user_line("go", now - 300)],
                    mtime=now - 5)
        self._sub(now, "deadbeef", 3, text="You are the widget lane and here is a long brief "
                                      "that must be ellipsized on the panel")
        _, state = self.build(now=now)
        detail = next(r for r in state["sessions"] if r["id"] == self.SID)["subagentDetail"]
        self.assertEqual(len(detail), 1)
        self.assertLessEqual(len(detail[0]["label"]), crabd.SUBAGENT_LABEL_MAX)
        self.assertTrue(detail[0]["label"].startswith("You are the widget lane"))

    def test_only_running_subagents_appear_newest_first(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID), [user_line("go", now - 300)],
                    mtime=now - 5)
        self._sub(now, "aaa", 5, text="newest lane")
        self._sub(now, "bbb", 40, text="middle lane")
        self._sub(now, "ccc", crabd.SUBAGENT_ACTIVE_SEC + 60, text="finished lane")
        _, state = self.build(now=now)
        row = next(r for r in state["sessions"] if r["id"] == self.SID)
        self.assertEqual([d["label"] for d in row["subagentDetail"]],
                         ["newest lane", "middle lane"])
        self.assertEqual(row["subagents"], {"running": 2, "total": 3})

    def test_detail_is_capped_at_five(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID), [user_line("go", now - 300)],
                    mtime=now - 5)
        for i in range(8):
            self._sub(now, "id%d" % i, i + 1, text="lane %d" % i)
        _, state = self.build(now=now)
        row = next(r for r in state["sessions"] if r["id"] == self.SID)
        self.assertEqual(len(row["subagentDetail"]), crabd.SUBAGENT_DETAIL_CAP)
        self.assertEqual(row["subagents"]["running"], 8)   # the badge still counts all
        self.assertEqual([d["label"] for d in row["subagentDetail"]],
                         ["lane 0", "lane 1", "lane 2", "lane 3", "lane 4"])

    def test_no_running_subagents_is_an_empty_list(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID), [user_line("go", now - 300)],
                    mtime=now - 5)
        _, state = self.build(now=now)
        row = next(r for r in state["sessions"] if r["id"] == self.SID)
        self.assertEqual(row["subagentDetail"], [])


# --------------------------------------------------------------------- schema 2: quiet

class QuietHoursTests(unittest.TestCase):
    @staticmethod
    def at(hour, minute=0):
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))

    def quiet(self, start, end, hour, minute=0):
        return crabd.quiet_state({"quietHours": {"start": start, "end": end}},
                                 self.at(hour, minute))

    def test_daytime_window(self):
        self.assertTrue(self.quiet("09:00", "17:00", 12)["active"])
        self.assertFalse(self.quiet("09:00", "17:00", 8)["active"])
        self.assertFalse(self.quiet("09:00", "17:00", 17)["active"])   # end exclusive
        self.assertTrue(self.quiet("09:00", "17:00", 9)["active"])     # start inclusive

    def test_overnight_window_wraps_past_midnight(self):
        for hour in (22, 23, 0, 3, 6):
            self.assertTrue(self.quiet("22:00", "07:00", hour)["active"], hour)
        for hour in (7, 12, 21):
            self.assertFalse(self.quiet("22:00", "07:00", hour)["active"], hour)

    def test_minutes_are_honoured_at_the_edges(self):
        self.assertFalse(self.quiet("22:30", "07:00", 22, 29)["active"])
        self.assertTrue(self.quiet("22:30", "07:00", 22, 30)["active"])
        self.assertTrue(self.quiet("22:00", "07:15", 7, 14)["active"])
        self.assertFalse(self.quiet("22:00", "07:15", 7, 15)["active"])

    def test_window_is_echoed_back_normalised(self):
        out = self.quiet("9:05", "17:00", 12)
        self.assertEqual((out["start"], out["end"]), ("09:05", "17:00"))

    def test_null_config_is_null_quiet_never_a_fabricated_window(self):
        self.assertIsNone(crabd.quiet_state({"quietHours": None}, time.time()))
        self.assertIsNone(crabd.quiet_state({}, time.time()))
        self.assertIsNone(crabd.quiet_state(None, time.time()))

    def test_malformed_windows_read_as_unconfigured(self):
        for hours in ({"start": "22:00"}, {"start": "25:00", "end": "07:00"},
                      {"start": "22:00", "end": "07:61"}, {"start": 22, "end": 7},
                      {"start": "10pm", "end": "7am"}, {"start": "22-00", "end": "07:00"},
                      "22:00-07:00", []):
            self.assertIsNone(crabd.quiet_state({"quietHours": hours}, time.time()), hours)

    def test_a_zero_length_window_is_never_active(self):
        self.assertFalse(self.quiet("22:00", "22:00", 22)["active"])


class UserConfigTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self.addCleanup(self._tmp.cleanup)

    def test_a_missing_file_is_created_with_the_documented_defaults(self):
        config = crabd.UserConfig(self.path)
        self.assertEqual(config.get(time.time()), {"quietHours": None, "allowReply": False})
        self.assertTrue(self.path.exists())
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")),
                         {"quietHours": None, "allowReply": False})

    def test_a_malformed_file_reads_as_defaults_and_is_left_alone(self):
        self.path.write_text("{not json", encoding="utf-8")
        config = crabd.UserConfig(self.path)
        self.assertEqual(config.get(time.time()), {"quietHours": None, "allowReply": False})
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_a_json_scalar_reads_as_defaults(self):
        self.path.write_text('"quiet"', encoding="utf-8")
        self.assertFalse(crabd.UserConfig(self.path).allow_reply(time.time()))

    def test_the_file_is_not_re_read_more_than_once_a_minute(self):
        self.path.write_text(json.dumps({"allowReply": True}), encoding="utf-8")
        config = crabd.UserConfig(self.path)
        now = time.time()
        self.assertTrue(config.allow_reply(now))
        self.path.write_text(json.dumps({"allowReply": False}), encoding="utf-8")
        os.utime(self.path, (now + 5, now + 5))
        self.assertTrue(config.allow_reply(now + 1))                       # cached
        self.assertFalse(config.allow_reply(now + crabd.CONFIG_RECHECK_SEC + 1))

    def test_an_unchanged_mtime_skips_the_reparse(self):
        """The damper is mtime, not content: an editor that rewrites the file without
        moving mtime is not re-read even after the once-a-minute window opens."""
        now = time.time()
        self.path.write_text(json.dumps({"allowReply": True}), encoding="utf-8")
        os.utime(self.path, (now, now))
        config = crabd.UserConfig(self.path)
        self.assertTrue(config.allow_reply(now))
        self.path.write_text(json.dumps({"allowReply": False}), encoding="utf-8")
        os.utime(self.path, (now, now))   # same mtime as the first read
        self.assertTrue(config.allow_reply(now + crabd.CONFIG_RECHECK_SEC + 1))

    def test_a_failed_write_leaves_the_existing_config_intact(self):
        """A-03 (v0.26.0). A write that fails after the truncate (ENOSPC, a killed process)
        used to EMPTY config.json - path.write_text opens with 'w', which truncates before
        it writes - and the next read fell back to DEFAULTS, silently losing quietHours,
        budget, panelApprovals and the rest. The write path is reached on EVERY quiet tap
        and every /v1/config save. The fix is temp-then-os.replace: the truncate only ever
        hits the temp, so a mid-write failure leaves the ORIGINAL intact.

        Mutation check: reverting _write to a direct path.write_text truncates config.json
        under this patch and the reload assertions below see DEFAULTS / an empty file."""
        original = {"allowReply": True, "quietHours": {"start": "22:00", "end": "07:00"}}
        self.path.write_text(json.dumps(original), encoding="utf-8")
        config = crabd.UserConfig(self.path)

        import pathlib
        real_write_text = pathlib.Path.write_text

        def failing(self_path, data, *args, **kwargs):
            # Model a real ENOSPC: the open('w') truncates/creates, THEN the write fails.
            real_write_text(self_path, "", *args, **kwargs)
            raise OSError(28, "No space left on device")

        pathlib.Path.write_text = failing
        try:
            landed = config.set_quiet_override("on", time.time() + 3600)
        finally:
            pathlib.Path.write_text = real_write_text

        self.assertFalse(landed)                       # the tap gets its 500
        on_disk = self.path.read_text(encoding="utf-8")
        self.assertNotEqual(on_disk.strip(), "")       # NOT emptied
        self.assertEqual(json.loads(on_disk), original)
        # A fresh reader still sees the real values, not DEFAULTS.
        self.assertTrue(crabd.UserConfig(self.path).allow_reply(time.time()))
        # And no stray temp file was left behind.
        self.assertFalse(self.path.with_name(self.path.name + ".tmp").exists())


# ------------------------------------------------------- schema 2: served over a socket

class ServedOverASocket(TempProjects):
    """A real crabd server on a test port - never DEFAULT_PORT. No tests of its own; the
    endpoint suites below inherit the fixture."""

    SID = "33333333-0000-0000-0000-000000000009"

    def setUp(self):
        super().setUp()
        now = time.time()
        write_jsonl(self.session_path(self.SID),
                    [user_line("do the thing", now - 120),
                     assistant_line("req_1", now - 60, output=10)], mtime=now - 10)
        self.hooks = crabd.HookTracker()
        self.config = crabd.UserConfig(self.config_path)
        self.builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                          self.hooks, StubLimits(), time.time(),
                                          self.config)
        self.builder.build()
        with self.builder._lock:
            self.builder._state = self.builder.build()
        crabd.Handler.builder = self.builder
        self.server, self.thread, self.port, self.client = start_test_server(
            lambda: crabd.CrabdServer(("127.0.0.1", 0), crabd.Handler))
        self.assertNotEqual(self.port, crabd.DEFAULT_PORT)
        self.addCleanup(self.stop_server)
        self.addCleanup(self.client.close)

    def stop_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def action(self, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        reply = self.client.post("/v1/action", data)
        return reply.status, reply.body

    def state(self):
        return self.client.get("/v1/state").json()


class ActionEndpointTests(ServedOverASocket):
    """POST /v1/action - ack and reply."""

    def test_ack_is_204_and_shows_up_on_the_row(self):
        self.hooks.record({"session_id": self.SID, "hook_event_name": "Notification",
                           "cwd": "C:\\IT", "message": "waiting on you"})
        status, _ = self.action({"sessionId": self.SID, "action": "ack"})
        self.assertEqual(status, 204)
        with self.builder._lock:
            self.builder._state = self.builder.build()
        row = next(r for r in self.state()["sessions"] if r["id"] == self.SID)
        self.assertTrue(row["acked"])
        self.assertEqual(row["state"], "needs_input")

    def test_ack_works_for_a_transcript_only_session_with_no_hook_row(self):
        self.assertEqual(self.hooks.snapshot(), {})
        status, _ = self.action({"sessionId": self.SID, "action": "ack"})
        self.assertEqual(status, 204)
        self.assertTrue(self.hooks.snapshot()[self.SID]["acked"])

    def test_ack_of_an_unknown_session_is_404(self):
        status, body = self.action({"sessionId": "not-a-session", "action": "ack"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "unknown session"})

    def test_malformed_bodies_are_400(self):
        for payload, raw in ((None, b"not json"),
                             (None, b""),
                             (None, b'"a string"'),
                             ({"action": "ack"}, None),
                             ({"sessionId": "", "action": "ack"}, None),
                             ({"sessionId": 5, "action": "ack"}, None),
                             ({"sessionId": "x"}, None),
                             ({"sessionId": "x", "action": "detonate"}, None)):
            status, body = self.action(payload, raw)
            self.assertEqual(status, 400, (payload, raw))
            self.assertEqual(json.loads(body), {"error": "malformed request"})

    def test_reply_is_501_even_with_allowreply_true(self):
        """The config flag gates the feature; it does not conjure a mechanism. The
        2026-08-26 spike proved none exists, so 501 is the honest answer."""
        self.config_path.write_text(json.dumps({"quietHours": None, "allowReply": True}),
                                    encoding="utf-8")
        status, body = self.action({"sessionId": self.SID, "action": "reply",
                                    "text": "Yes"})
        self.assertEqual(status, 501)
        self.assertEqual(json.loads(body), {"error": "reply not supported"})

    def test_action_refuses_a_cross_site_web_page(self):
        """QA-Audit 2026-08-27 SEC-1. A mutating endpoint no longer carries ACAO:* (that
        header invites the cross-origin read); instead a real web page is refused. Origin
        http/https -> 403 before the side effect."""
        reply = self.client.post(
            "/v1/action",
            json.dumps({"sessionId": self.SID, "action": "ack"}).encode(),
            headers={"Origin": "https://evil.example"})
        self.assertEqual(reply.status, 403)
        self.assertEqual(json.loads(reply.body), {"error": "cross-site request refused"})

    def test_action_allows_the_widget_null_origin_and_reflects_it(self):
        """SEC-1: the widget's QtWebEngine page sends Origin: null. Allowed, and the
        reply reflects that origin (never the wildcard) so the widget can read the
        status - not ACAO:*."""
        reply = self.client.post(
            "/v1/action",
            json.dumps({"sessionId": self.SID, "action": "ack"}).encode(),
            headers={"Origin": "null"})
        self.assertEqual(reply.status, 204)
        self.assertEqual(reply.headers.get("Access-Control-Allow-Origin"), "null")

    def test_action_with_no_origin_works_and_sends_no_wildcard(self):
        """SEC-1: curl hooks / the CLI's HTTP hooks / local tools send no Origin. Allowed,
        and no ACAO:* is emitted."""
        reply = self.client.post(
            "/v1/action",
            json.dumps({"sessionId": self.SID, "action": "ack"}).encode())
        self.assertEqual(reply.status, 204)
        self.assertIsNone(reply.headers.get("Access-Control-Allow-Origin"))

    def test_quiet_is_served_at_the_top_level(self):
        self.assertIsNone(self.state()["quiet"])
        self.config_path.write_text(
            json.dumps({"quietHours": {"start": "22:00", "end": "07:00"},
                        "allowReply": False}), encoding="utf-8")
        self.config._checked_at = 0.0   # skip the once-a-minute damper
        with self.builder._lock:
            self.builder._state = self.builder.build()
        quiet = self.state()["quiet"]
        self.assertEqual((quiet["start"], quiet["end"]), ("22:00", "07:00"))
        self.assertIn(quiet["active"], (True, False))

    def test_schema_is_pinned_to_the_last_breaking_shape(self):
        """docs/STATE-CONTRACT.md, "VERSIONING REWORK": `schema` names the last
        BREAKING shape, not the feature level, so it stays 5 while crabd ships v0.13.0 -
        /v1/history, the digest key, burn.budget, titleSource, the v0.12.0 control-surface
        wave (limits.source, burn.costUSD, contextSource, pendingPermission) and the
        v0.13.0 depletion forecast (limits.*.exhaustAt) and the v0.14.0 queuedContinue
        are all additive and none moves it."""
        self.assertEqual(self.state()["schema"], 5)
        self.assertEqual(crabd.SCHEMA_BREAKING, 5)
        self.assertEqual(crabd.VERSION, "0.30.0")

    def test_the_v6_fields_ride_on_schema_5_in_the_served_document(self):
        """The compat contract in ONE test: the fields the deployed v0.5.0 widget has
        never heard of (contextTokens, fleet) are present in the very document whose
        schema that widget accepts - its acceptance test is 1 <= schema <= 5, and an
        unknown KEY is ignored, never rejected. This is what lets crabd ship additive
        features without the console-bound .icuewidget import (VERSIONING REWORK)."""
        state = self.state()
        # The deployed v0.5.0 acceptance test, transcribed from widget/scripts/sidecrab.js
        # acceptDoc(): a whole number in 1..ceiling, where v0.5.0's ceiling is 5.
        self.assertTrue(1 <= state["schema"] <= 5
                        and state["schema"] == int(state["schema"]))
        self.assertEqual(sorted(state["fleet"]), ["glow", "toast"])   # v0.6.0, top level
        self.assertIn("byModel", state["burn"])                       # v0.5.0
        row = next(r for r in state["sessions"] if r["id"] == self.SID)
        self.assertIn("contextTokens", row)                           # v0.6.0, per session




# ------------------------------------------------------------------- burn.daily

class DailyBurnTests(unittest.TestCase):
    def setUp(self):
        today = time.localtime()
        self.now = time.mktime((today.tm_year, today.tm_mon, today.tm_mday,
                                12, 30, 0, 0, 0, -1))

    def daily(self, requests):
        burn, _ = crabd.StateBuilder._burn(requests, {rid: "s1" for rid in requests}, self.now)
        return burn

    def test_seven_buckets_oldest_first_ending_today(self):
        daily = self.daily({})["daily"]
        self.assertEqual(len(daily), 7)
        starts = [d["dayStart"] for d in daily]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(set(starts)), 7)
        self.assertEqual(starts[-1], time.strftime("%Y-%m-%d", time.localtime(self.now)))
        self.assertTrue(all(d["outputTokens"] == 0 for d in daily))

    def test_tokens_land_in_the_local_day_they_were_spent(self):
        requests = {"r_today": (self.now - 3600, 100, 0, 0, 0, "claude-fable-5"),
                    "r_2d": (self.now - 2 * 86400, 200, 0, 0, 0, "claude-fable-5"),
                    "r_6d": (self.now - 6 * 86400, 300, 0, 0, 0, "claude-fable-5")}
        by_day = {d["dayStart"]: d["outputTokens"] for d in self.daily(requests)["daily"]}
        self.assertEqual(sum(by_day.values()), 600)
        for offset, expected in ((0, 100), (2, 200), (6, 300)):
            label = time.strftime("%Y-%m-%d", time.localtime(self.now - offset * 86400))
            self.assertEqual(by_day[label], expected)

    def test_local_midnight_is_the_boundary_not_a_rolling_24h(self):
        # One second either side of last local midnight.
        midnight = crabd._local_midnight(self.now)
        requests = {"r_just_today": (midnight + 1, 10, 0, 0, 0, "claude-fable-5"),
                    "r_just_yesterday": (midnight - 1, 20, 0, 0, 0, "claude-fable-5")}
        by_day = {d["dayStart"]: d["outputTokens"] for d in self.daily(requests)["daily"]}
        today = time.strftime("%Y-%m-%d", time.localtime(self.now))
        yesterday = time.strftime("%Y-%m-%d", time.localtime(midnight - 1))
        self.assertEqual(by_day[today], 10)
        self.assertEqual(by_day[yesterday], 20)

    def test_todays_bucket_equals_burn_today(self):
        requests = {"a": (self.now - 60, 100, 1, 1, 1, "claude-fable-5"),
                    "b": (self.now - 7200, 50, 1, 1, 1, "claude-fable-5"),
                    "old": (self.now - 3 * 86400, 999, 1, 1, 1, "claude-fable-5")}
        burn = self.daily(requests)
        self.assertEqual(burn["daily"][-1]["outputTokens"], burn["today"]["outputTokens"])
        self.assertEqual(burn["daily"][-1]["outputTokens"], 150)

    def test_records_older_than_the_window_are_in_no_bucket(self):
        burn = self.daily({"r_old": (self.now - 30 * 86400, 999, 1, 1, 1, "claude-fable-5")})
        self.assertEqual(sum(d["outputTokens"] for d in burn["daily"]), 0)

    def test_the_hourly_series_is_untouched(self):
        burn = self.daily({"r": (self.now - 60, 100, 0, 0, 0, "claude-fable-5")})
        self.assertEqual(len(burn["hourly"]), 24)
        self.assertEqual(sum(h["outputTokens"] for h in burn["hourly"]), 100)

    def test_day_starts_survive_a_dst_shift(self):
        """Subtracting a flat 86400 across a DST boundary repeats one calendar date and
        drops another - the series would then have 7 entries and 6 days."""
        days = crabd._local_day_starts(self.now, 7)
        labels = [crabd._local_day(d) for d in days]
        self.assertEqual(len(set(labels)), 7)
        for earlier, later in zip(days, days[1:]):
            self.assertIn(round((later - earlier) / 3600), (23, 24, 25))

    def test_the_transcript_window_is_wide_enough_to_fill_seven_days(self):
        self.assertGreaterEqual(crabd.TRANSCRIPT_WINDOW_SEC,
                                crabd.BURN_DAILY_DAYS * 86400)


# ---------------------------------------------------------------- session events

class SessionEventsTests(TempProjects):
    SID = "eeeeeeee-0000-0000-0000-00000000000e"

    def hook(self, tracker, event, **extra):
        tracker.record({"session_id": self.SID, "hook_event_name": event, **extra})

    def events(self, tracker):
        return tracker.snapshot()[self.SID]["events"]

    def test_each_hook_lands_one_event_newest_first(self):
        tracker = crabd.HookTracker()
        for event in ("SessionStart", "UserPromptSubmit", "Notification", "Stop"):
            self.hook(tracker, event, message="which host?")
        self.assertEqual([e["text"] for e in self.events(tracker)],
                         ["turn finished", "asked a question", "prompt submitted",
                          "session started"])
        self.assertTrue(all(e["at"].endswith("Z") for e in self.events(tracker)))

    def test_subagentstop_is_an_event_even_though_it_is_not_a_transition(self):
        tracker = crabd.HookTracker()
        self.hook(tracker, "SubagentStop")
        self.assertEqual([e["text"] for e in self.events(tracker)], ["subagent finished"])
        self.assertIsNone(tracker.snapshot()[self.SID]["state"])

    def test_an_unknown_hook_name_adds_nothing(self):
        tracker = crabd.HookTracker()
        self.hook(tracker, "PreToolUse")
        self.assertEqual(self.events(tracker), [])

    def test_the_ring_is_capped_at_eight_and_drops_the_oldest(self):
        tracker = crabd.HookTracker()
        for _ in range(6):
            self.hook(tracker, "UserPromptSubmit")
            self.hook(tracker, "Stop")
        events = self.events(tracker)
        self.assertEqual(len(events), crabd.EVENTS_CAP)
        # 12 events in, 8 kept: the oldest four fell off the back.
        self.assertEqual(events[0]["text"], "turn finished")
        self.assertEqual(events[-1]["text"], "prompt submitted")

    def test_an_ack_is_an_event(self):
        tracker = crabd.HookTracker()
        self.hook(tracker, "Notification", message="which host?")
        tracker.ack(self.SID)
        self.assertEqual([e["text"] for e in self.events(tracker)],
                         ["acknowledged from Edge", "asked a question"])

    def test_a_snapshot_copy_cannot_mutate_the_tracker(self):
        tracker = crabd.HookTracker()
        self.hook(tracker, "Stop")
        self.events(tracker).append({"at": "x", "text": "forged"})
        self.assertEqual(len(self.events(tracker)), 1)

    def test_events_reach_the_served_session_row(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID),
                    [user_line("go", now - 60), assistant_line("r1", now - 30)],
                    mtime=now - 5)
        tracker = crabd.HookTracker()
        self.hook(tracker, "UserPromptSubmit", cwd="C:\\IT")
        _, state = self.build(now=now, hooks=tracker)
        row = next(r for r in state["sessions"] if r["id"] == self.SID)
        self.assertEqual([e["text"] for e in row["events"]], ["prompt submitted"])

    def test_a_session_with_no_hooks_serves_an_empty_ring(self):
        now = time.time()
        write_jsonl(self.session_path("ffffffff-0000-0000-0000-00000000000f"),
                    [user_line("go", now - 60), assistant_line("r2", now - 30)],
                    mtime=now - 5)
        _, state = self.build(now=now)
        self.assertEqual(state["sessions"][0]["events"], [])


# --------------------------------------------------------------- schema 4: recap

class FakeGit:
    """Stands in for `git -C <cwd> log --oneline --since=midnight`. Maps cwd -> a line
    count, an exception to raise, or None for "git said no"."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, cwd):
        self.calls.append(cwd)
        result = self.results.get(cwd)
        if isinstance(result, BaseException):
            raise result
        return result


class RecapCommitsTests(unittest.TestCase):
    """The git half of the recap, subprocess mocked."""

    @staticmethod
    def reader(results):
        return crabd.RecapReader(runner=FakeGit(results))

    def test_nothing_is_served_until_the_first_run(self):
        recap = self.reader({})
        self.assertIsNone(recap.get())
        recap.submit(3, 1, [])
        self.assertIsNone(recap.get())   # submitted, not yet computed
        recap.poll(time.time())
        self.assertIsNotNone(recap.get())

    def test_a_poll_before_any_submit_computes_nothing(self):
        """Zeroes served at startup would claim 'no sessions today' before crabd has
        looked - the null is the honest answer for that second."""
        recap = self.reader({})
        self.assertFalse(recap.poll(time.time()))
        self.assertIsNone(recap.get())

    def test_the_served_shape_is_the_contract(self):
        recap = self.reader({"C:\\Dev\\sidecrab": 12})
        recap.submit(9, 3, [("sidecrab", "C:\\Dev\\sidecrab")])
        recap.poll(time.time())
        served = recap.get()
        self.assertEqual(sorted(served),
                         ["commits", "computedAt", "doneToday", "sessionsToday", "week"])
        self.assertEqual(served["sessionsToday"], 9)
        self.assertEqual(served["doneToday"], 3)
        self.assertEqual(served["commits"], [{"repo": "sidecrab", "count": 12}])
        self.assertTrue(served["computedAt"].endswith("Z"))

    def test_commits_are_capped_at_four_by_count_desc(self):
        repos = {f"C:\\r{i}": i for i in range(1, 7)}
        recap = self.reader(repos)
        recap.submit(1, 0, [(f"r{i}", f"C:\\r{i}") for i in range(1, 7)])
        recap.poll(time.time())
        commits = recap.get()["commits"]
        self.assertEqual([c["count"] for c in commits], [6, 5, 4, 3])
        self.assertEqual([c["repo"] for c in commits], ["r6", "r5", "r4", "r3"])

    def test_ties_break_on_repo_name_so_the_cut_is_deterministic(self):
        recap = self.reader({f"C:\\{n}": 5 for n in ("delta", "alpha", "charlie", "bravo",
                                                     "echo")})
        recap.submit(1, 0, [(n, f"C:\\{n}") for n in ("delta", "alpha", "charlie",
                                                      "bravo", "echo")])
        recap.poll(time.time())
        self.assertEqual([c["repo"] for c in recap.get()["commits"]],
                         ["alpha", "bravo", "charlie", "delta"])

    def test_a_repo_with_no_commits_today_is_not_a_zero_row(self):
        recap = self.reader({"C:\\quiet": 0, "C:\\busy": 4})
        recap.submit(2, 0, [("quiet", "C:\\quiet"), ("busy", "C:\\busy")])
        recap.poll(time.time())
        self.assertEqual(recap.get()["commits"], [{"repo": "busy", "count": 4}])

    def test_a_git_timeout_skips_that_repo_and_the_rest_still_count(self):
        runner = FakeGit({"C:\\wedged": subprocess.TimeoutExpired(cmd="git", timeout=10),
                          "C:\\fine": 3})
        recap = crabd.RecapReader(runner=runner)
        recap.submit(2, 0, [("wedged", "C:\\wedged"), ("fine", "C:\\fine")])
        recap.poll(time.time())
        self.assertEqual(recap.get()["commits"], [{"repo": "fine", "count": 3}])
        self.assertEqual(runner.calls, ["C:\\wedged", "C:\\fine"])

    def test_a_missing_git_binary_skips_silently(self):
        recap = self.reader({"C:\\a": FileNotFoundError("git"), "C:\\b": 2})
        recap.submit(1, 0, [("a", "C:\\a"), ("b", "C:\\b")])
        recap.poll(time.time())
        self.assertEqual(recap.get()["commits"], [{"repo": "b", "count": 2}])

    def test_a_nonzero_git_exit_is_skipped_not_served_as_zero(self):
        """`None` is what _git_count returns for a non-zero exit (unborn HEAD, not a
        repo). It must not become a 0-commit row."""
        recap = self.reader({"C:\\notarepo": None, "C:\\real": 1})
        recap.submit(1, 0, [("notarepo", "C:\\notarepo"), ("real", "C:\\real")])
        recap.poll(time.time())
        self.assertEqual(recap.get()["commits"], [{"repo": "real", "count": 1}])

    def test_the_candidate_list_is_capped_before_the_subprocesses(self):
        runner = FakeGit({f"C:\\r{i}": 1 for i in range(30)})
        recap = crabd.RecapReader(runner=runner)
        recap.submit(1, 0, [(f"r{i}", f"C:\\r{i}") for i in range(30)])
        recap.poll(time.time())
        self.assertEqual(len(runner.calls), crabd.RECAP_REPO_SCAN_CAP)

    def test_the_whole_recap_is_cached_for_five_minutes(self):
        runner = FakeGit({"C:\\a": 1})
        recap = crabd.RecapReader(runner=runner)
        now = time.time()
        recap.submit(5, 2, [("a", "C:\\a")])
        self.assertTrue(recap.poll(now))
        first = recap.get()
        # New facts arrive every builder pass; the served document must not move until
        # the cache expires, or `computedAt` would date a git run that never happened.
        recap.submit(99, 99, [("a", "C:\\a")])
        self.assertFalse(recap.poll(now + crabd.RECAP_REFRESH_SEC - 1))
        self.assertEqual(recap.get(), first)
        self.assertEqual(len(runner.calls), 1)
        self.assertTrue(recap.poll(now + crabd.RECAP_REFRESH_SEC + 1))
        self.assertEqual(recap.get()["sessionsToday"], 99)

    def test_a_served_copy_cannot_mutate_the_cache(self):
        recap = self.reader({"C:\\a": 1})
        recap.submit(1, 0, [("a", "C:\\a")])
        recap.poll(time.time())
        served = recap.get()
        served["commits"][0]["count"] = 999
        served["sessionsToday"] = 999
        self.assertEqual(recap.get()["commits"][0]["count"], 1)
        self.assertEqual(recap.get()["sessionsToday"], 1)


class RecapDoneTodayTests(unittest.TestCase):
    SID_A = "aaaa1111-0000-0000-0000-00000000000a"
    SID_B = "bbbb2222-0000-0000-0000-00000000000b"

    @staticmethod
    def hook(tracker, sid, event, **extra):
        tracker.record({"session_id": sid, "hook_event_name": event, **extra})

    def test_a_stop_transition_counts(self):
        tracker = crabd.HookTracker()
        self.hook(tracker, self.SID_A, "UserPromptSubmit")
        self.hook(tracker, self.SID_A, "Stop")
        self.assertEqual(tracker.done_today(), 1)

    def test_a_session_that_finished_twice_counts_once(self):
        """The contract counts SESSIONS that reached done, not turns that ended."""
        tracker = crabd.HookTracker()
        for _ in range(4):
            self.hook(tracker, self.SID_A, "UserPromptSubmit")
            self.hook(tracker, self.SID_A, "Stop")
        self.assertEqual(tracker.done_today(), 1)

    def test_two_sessions_count_twice(self):
        tracker = crabd.HookTracker()
        for sid in (self.SID_A, self.SID_B):
            self.hook(tracker, sid, "Stop")
        self.assertEqual(tracker.done_today(), 2)

    def test_a_repeated_stop_without_an_intervening_transition_is_one(self):
        tracker = crabd.HookTracker()
        self.hook(tracker, self.SID_A, "Stop")
        self.hook(tracker, self.SID_A, "Stop")
        self.assertEqual(len(tracker.dones), 1)

    def test_yesterdays_finishes_do_not_count_today(self):
        tracker = crabd.HookTracker()
        self.hook(tracker, self.SID_A, "Stop")
        # Backdate the ring to just before this morning's local midnight.
        midnight = crabd._local_midnight(time.time())
        tracker.dones = [(midnight - 1, self.SID_A), (midnight + 1, self.SID_B)]
        self.assertEqual(tracker.done_today(), 1)

    def test_a_finished_session_still_counts_after_its_row_is_pruned(self):
        """A `gone` row is deleted after 2 h; the day's tally must survive that."""
        tracker = crabd.HookTracker()
        self.hook(tracker, self.SID_A, "Stop")
        self.hook(tracker, self.SID_A, "SessionEnd")
        tracker.sessions[self.SID_A]["at"] = time.time() - 3 * 3600
        tracker.prune(time.time())
        self.assertNotIn(self.SID_A, tracker.sessions)
        self.assertEqual(tracker.done_today(), 1)

    def test_prune_drops_the_ring_beyond_the_keep_window(self):
        tracker = crabd.HookTracker()
        now = time.time()
        tracker.dones = [(now - crabd.RECAP_DONE_KEEP_SEC - 10, "old"), (now, "new")]
        tracker.prune(now)
        self.assertEqual([sid for _, sid in tracker.dones], ["new"])

    def test_the_limitation_is_a_floor_never_an_invention(self):
        """A fresh tracker (crabd just restarted) reports 0, not a guess derived from
        transcripts that merely stopped growing."""
        self.assertEqual(crabd.HookTracker().done_today(), 0)


class RecapInputsTests(TempProjects):
    """The cheap half: what the builder hands the recap thread.

    `now` is pinned to today at 12:30 local (the same trick DailyBurnTests uses) so the
    midnight arithmetic is deterministic whatever hour the suite runs at; fixture mtimes
    are set relative to that pinned clock.
    """

    SID_TODAY = "11112222-0000-0000-0000-000000000001"
    SID_OLD = "33334444-0000-0000-0000-000000000002"

    def setUp(self):
        super().setUp()
        today = time.localtime()
        self.now = time.mktime((today.tm_year, today.tm_mon, today.tm_mday,
                                12, 30, 0, 0, 0, -1))
        self.midnight = crabd._local_midnight(self.now)

    def builder_with_recap(self, runner_results=None):
        recap = crabd.RecapReader(runner=FakeGit(runner_results or {}))
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path), recap)
        return builder, recap

    def test_sessions_today_counts_a_session_the_served_list_has_dropped(self):
        """A session that ended this morning is `gone` and not served, but it still
        happened today - which is the whole point of counting from the scan."""
        now = self.now
        fresh = now - 300
        long_gone = self.midnight + 30          # ~12.5 h ago: past GONE_AFTER_SEC
        write_jsonl(self.session_path(self.SID_TODAY),
                    [user_line("go", fresh), assistant_line("r1", fresh)], mtime=fresh)
        write_jsonl(self.session_path(self.SID_OLD),
                    [user_line("earlier", long_gone),
                     assistant_line("r2", long_gone)], mtime=long_gone)
        builder, recap = self.builder_with_recap()
        state = builder.build(now=now)
        served = [r["id"] for r in state["sessions"]]
        self.assertEqual(served, [self.SID_TODAY])   # the morning session aged out
        recap.poll(now)
        self.assertEqual(recap.get()["sessionsToday"], 2)

    def test_a_transcript_from_before_midnight_is_not_today(self):
        yesterday = self.midnight - 3600
        write_jsonl(self.session_path(self.SID_OLD),
                    [user_line("yesterday", yesterday),
                     assistant_line("r_y", yesterday)], mtime=yesterday)
        builder, recap = self.builder_with_recap()
        builder.build(now=self.now)
        recap.poll(self.now)
        self.assertEqual(recap.get()["sessionsToday"], 0)

    def test_the_midnight_boundary_is_local_not_a_rolling_day(self):
        write_jsonl(self.session_path(self.SID_TODAY),
                    [user_line("just after midnight", self.midnight + 1)],
                    mtime=self.midnight + 1)
        write_jsonl(self.session_path(self.SID_OLD),
                    [user_line("just before midnight", self.midnight - 1)],
                    mtime=self.midnight - 1)
        builder, recap = self.builder_with_recap()
        builder.build(now=self.now)
        recap.poll(self.now)
        self.assertEqual(recap.get()["sessionsToday"], 1)

    def test_a_subagent_file_counts_toward_its_parent_session(self):
        fresh = self.now - 120
        write_jsonl(self.session_path(self.SID_TODAY),
                    [user_line("go", fresh), assistant_line("r1", fresh)], mtime=fresh)
        sub = (self.projects / "C--IT" / self.SID_TODAY / "subagents" / "agent-abc123.jsonl")
        write_jsonl(sub, [assistant_line("r_sub", fresh)], mtime=fresh)
        builder, recap = self.builder_with_recap()
        builder.build(now=self.now)
        recap.poll(self.now)
        self.assertEqual(recap.get()["sessionsToday"], 1)

    def test_one_repo_per_cwd_family_and_non_repos_are_never_shelled_out_to(self):
        now = self.now
        fresh = now - 120
        repo_root = Path(self._tmp.name) / "arepo"
        (repo_root / ".git").mkdir(parents=True)
        (repo_root / ".git" / "HEAD").write_text("ref: refs/heads/master\n")
        (repo_root / ".git" / "config").write_text(
            '[remote "origin"]\n\turl = https://example.invalid/x/arepo.git\n')
        plain = Path(self._tmp.name) / "notarepo"
        plain.mkdir()
        write_jsonl(self.session_path(self.SID_TODAY),
                    [user_line("go", fresh, cwd=str(repo_root))], mtime=fresh)
        write_jsonl(self.session_path(self.SID_OLD),
                    [user_line("go", fresh, cwd=str(plain))], mtime=fresh)
        runner = FakeGit({str(repo_root): 7})
        recap = crabd.RecapReader(runner=runner)
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path), recap)
        builder.build(now=now)
        recap.poll(now)
        self.assertIn(str(repo_root), runner.calls)
        # The non-repo cwd has no repo NAME to key commits by, so GitLookup drops it
        # before any subprocess is spawned.
        self.assertNotIn(str(plain), runner.calls)
        self.assertEqual(recap.get()["commits"], [{"repo": "arepo", "count": 7}])

    def make_repo(self, name, remote=None):
        root = Path(self._tmp.name) / name
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/master\n")
        (root / ".git" / "config").write_text(
            f'[remote "origin"]\n\turl = https://example.invalid/x/{remote or name}.git\n')
        return root

    def write_config(self, **keys):
        self.config_path.write_text(json.dumps(keys), encoding="utf-8")

    def recap_over(self, runner_results):
        """A builder whose only session cwd is `driven`'s PARENT-less temp dir, so the
        configured repos are the only thing that can put a repo in the recap."""
        recap = crabd.RecapReader(runner=FakeGit(runner_results))
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path), recap)
        return builder, recap

    def test_a_configured_repo_appears_even_with_no_session_in_it(self):
        """The amendment's whole point: every lane runs from C:\\IT, so a repo driven
        from elsewhere has no session cwd and would otherwise be invisible."""
        driven = self.make_repo("driven")
        self.write_config(quietHours=None, allowReply=False, recapRepos=[str(driven)])
        builder, recap = self.recap_over({str(driven): 12})
        builder.build(now=self.now)
        recap.poll(self.now)
        self.assertEqual(recap.get()["commits"], [{"repo": "driven", "count": 12}])

    def test_configured_and_session_repos_are_merged(self):
        driven = self.make_repo("driven")
        worked_in = self.make_repo("worked-in")
        write_jsonl(self.session_path(self.SID_TODAY),
                    [user_line("go", self.now - 120, cwd=str(worked_in))],
                    mtime=self.now - 120)
        self.write_config(quietHours=None, recapRepos=[str(driven)])
        builder, recap = self.recap_over({str(driven): 3, str(worked_in): 9})
        builder.build(now=self.now)
        recap.poll(self.now)
        self.assertEqual(recap.get()["commits"],
                         [{"repo": "worked-in", "count": 9},
                          {"repo": "driven", "count": 3}])

    def test_a_repo_that_is_both_configured_and_a_session_cwd_is_counted_once(self):
        both = self.make_repo("both")
        write_jsonl(self.session_path(self.SID_TODAY),
                    [user_line("go", self.now - 120, cwd=str(both))], mtime=self.now - 120)
        self.write_config(recapRepos=[str(both)])
        runner = FakeGit({str(both): 5})
        recap = crabd.RecapReader(runner=runner)
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path), recap)
        builder.build(now=self.now)
        recap.poll(self.now)
        self.assertEqual(recap.get()["commits"], [{"repo": "both", "count": 5}])
        self.assertEqual(len(runner.calls), 1)

    def test_two_paths_inside_one_repo_dedupe_to_one_row(self):
        """Deduped by RESOLVED repo, not by the string in the config."""
        repo = self.make_repo("solo")
        inner = repo / "companion"
        inner.mkdir()
        self.write_config(recapRepos=[str(repo), str(inner)])
        runner = FakeGit({str(repo): 4, str(inner): 4})
        recap = crabd.RecapReader(runner=runner)
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path), recap)
        builder.build(now=self.now)
        recap.poll(self.now)
        self.assertEqual(recap.get()["commits"], [{"repo": "solo", "count": 4}])
        self.assertEqual(runner.calls, [str(repo)])

    def test_a_configured_repo_obeys_the_same_per_repo_rules(self):
        """Same cap-4-by-count-desc, same skip-on-failure, same drop-if-zero."""
        repos = [self.make_repo(f"cfg{i}") for i in range(6)]
        self.write_config(recapRepos=[str(r) for r in repos])
        results = {str(repos[0]): subprocess.TimeoutExpired(cmd="git", timeout=10),
                   str(repos[1]): 0, str(repos[2]): 8, str(repos[3]): 6,
                   str(repos[4]): 7, str(repos[5]): 5}
        builder, recap = self.recap_over(results)
        builder.build(now=self.now)
        recap.poll(self.now)
        self.assertEqual([c["repo"] for c in recap.get()["commits"]],
                         ["cfg2", "cfg4", "cfg3", "cfg5"])

    def test_a_configured_repo_is_not_crowded_out_by_a_busy_day(self):
        """Configured paths lead the candidate list, so the scan cap cuts session cwds
        rather than the repo the operator asked for by name."""
        driven = self.make_repo("driven")
        self.write_config(recapRepos=[str(driven)])
        busy = []
        for i in range(crabd.RECAP_REPO_SCAN_CAP + 4):
            root = self.make_repo(f"busy{i}")
            busy.append(root)
            write_jsonl(self.session_path(f"aaaa{i:04d}-0000-0000-0000-00000000000{i%10}"),
                        [user_line("go", self.now - 60 - i, cwd=str(root))],
                        mtime=self.now - 60 - i)
        runner = FakeGit({str(driven): 1, **{str(b): 0 for b in busy}})
        recap = crabd.RecapReader(runner=runner)
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path), recap)
        builder.build(now=self.now)
        recap.poll(self.now)
        self.assertEqual(runner.calls[0], str(driven))
        self.assertEqual(recap.get()["commits"], [{"repo": "driven", "count": 1}])

    def test_recap_repos_is_parsed_defensively(self):
        real = self.make_repo("real")
        for value in (None, "C:\\Dev\\sidecrab", 5, {"a": 1},
                      [5, None, {"p": 1}, "", "   ", str(real)],
                      [str(real), str(Path(self._tmp.name) / "does-not-exist")],
                      [str(real), str(real)]):
            self.write_config(recapRepos=value)
            config = crabd.UserConfig(self.config_path)
            self.assertEqual(config.recap_repos(self.now),
                             [str(real)] if isinstance(value, list) else [], value)

    def test_a_missing_recap_repos_key_is_simply_no_extra_repos(self):
        self.write_config(quietHours=None, allowReply=False)
        self.assertEqual(crabd.UserConfig(self.config_path).recap_repos(self.now), [])

    def test_a_configured_path_that_is_not_a_repo_is_never_shelled_out_to(self):
        plain = Path(self._tmp.name) / "plain"
        plain.mkdir()
        self.write_config(recapRepos=[str(plain)])
        runner = FakeGit({str(plain): 99})
        recap = crabd.RecapReader(runner=runner)
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path), recap)
        builder.build(now=self.now)
        recap.poll(self.now)
        self.assertNotIn(str(plain), runner.calls)
        self.assertEqual(recap.get()["commits"], [])

    def test_done_today_reaches_the_served_recap(self):
        fresh = self.now - 120
        write_jsonl(self.session_path(self.SID_TODAY),
                    [user_line("go", fresh)], mtime=fresh)
        builder, recap = self.builder_with_recap()
        builder.hooks.record({"session_id": self.SID_TODAY, "hook_event_name": "Stop"})
        builder.build(now=self.now)
        recap.poll(self.now)
        self.assertEqual(recap.get()["doneToday"], 1)


# ------------------------------------------------------- schema 4: ack-all + config

class AckAllAndConfigTests(ServedOverASocket):
    """POST /v1/action {"action":"ack-all"} and POST /v1/config over a real socket."""

    SID_2 = "44444444-0000-0000-0000-000000000010"

    def config_post(self, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        reply = self.client.post("/v1/config", data)
        return reply.status, reply.body

    def rebuild(self):
        with self.builder._lock:
            self.builder._state = self.builder.build()

    def waiting(self, *session_ids):
        now = time.time()
        for sid in session_ids:
            if sid != self.SID:
                write_jsonl(self.session_path(sid),
                            [user_line("do the thing", now - 120),
                             assistant_line(f"req_{sid[:4]}", now - 60, output=10)],
                            mtime=now - 10)
            self.hooks.record({"session_id": sid, "hook_event_name": "Notification",
                               "cwd": "C:\\IT", "message": "waiting on you"})
        self.rebuild()

    # ---- ack-all

    def test_ack_all_acks_every_waiting_session(self):
        self.waiting(self.SID, self.SID_2)
        status, _ = self.action({"action": "ack-all"})
        self.assertEqual(status, 204)
        self.rebuild()
        rows = {r["id"]: r for r in self.state()["sessions"]}
        for sid in (self.SID, self.SID_2):
            self.assertTrue(rows[sid]["acked"], sid)
            self.assertEqual(rows[sid]["state"], "needs_input")

    def test_ack_all_records_an_event_per_session(self):
        self.waiting(self.SID, self.SID_2)
        self.action({"action": "ack-all"})
        snapshot = self.hooks.snapshot()
        for sid in (self.SID, self.SID_2):
            self.assertEqual(snapshot[sid]["events"][0]["text"],
                             crabd.HookTracker.ACK_EVENT, sid)

    def test_ack_all_needs_no_session_id(self):
        status, body = self.action({"action": "ack-all"})
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")

    def test_ack_all_with_nothing_waiting_is_still_204(self):
        self.assertEqual(self.builder.ack_all(), 0)
        status, _ = self.action({"action": "ack-all"})
        self.assertEqual(status, 204)

    def test_ack_all_leaves_working_sessions_alone(self):
        """Only needs_input rows glow, so only they can be acked - acking a working
        session would write an event for a card nobody was waiting on."""
        self.rebuild()
        row = next(r for r in self.state()["sessions"] if r["id"] == self.SID)
        self.assertNotEqual(row["state"], "needs_input")
        self.action({"action": "ack-all"})
        self.assertEqual(self.hooks.snapshot(), {})

    def test_ack_all_does_not_re_ack_an_already_acked_session(self):
        self.waiting(self.SID)
        self.action({"sessionId": self.SID, "action": "ack"})
        self.rebuild()
        self.assertEqual(self.builder.ack_all(), 0)
        events = [e["text"] for e in self.hooks.snapshot()[self.SID]["events"]]
        self.assertEqual(events.count(crabd.HookTracker.ACK_EVENT), 1)

    def test_ack_all_only_rejects_a_malformed_body(self):
        for raw in (b"not json", b"", b'"a string"', b"[]"):
            status, body = self.action(None, raw)
            self.assertEqual(status, 400, raw)
            self.assertEqual(json.loads(body), {"error": "malformed request"})

    def test_reply_is_still_501_after_the_ack_all_branch(self):
        status, body = self.action({"sessionId": self.SID, "action": "reply",
                                    "text": "Yes"})
        self.assertEqual(status, 501)
        self.assertEqual(json.loads(body), {"error": "reply not supported"})

    # ---- POST /v1/config

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_a_valid_window_is_204_and_written(self):
        status, _ = self.config_post({"quietHours": {"start": "22:00", "end": "07:00"}})
        self.assertEqual(status, 204)
        self.assertEqual(self.read_config()["quietHours"],
                         {"start": "22:00", "end": "07:00"})

    def test_null_clears_the_window(self):
        self.config_post({"quietHours": {"start": "22:00", "end": "07:00"}})
        status, _ = self.config_post({"quietHours": None})
        self.assertEqual(status, 204)
        self.assertIsNone(self.read_config()["quietHours"])
        self.rebuild()
        self.assertIsNone(self.state()["quiet"])

    def test_hhmm_is_normalized_on_the_way_in(self):
        self.config_post({"quietHours": {"start": "7:5", "end": "23:9"}})
        self.assertEqual(self.read_config()["quietHours"],
                         {"start": "07:05", "end": "23:09"})

    def test_a_bad_hhmm_is_400_and_writes_nothing(self):
        self.config_path.write_text(
            json.dumps({"quietHours": None, "allowReply": False}), encoding="utf-8")
        before = self.config_path.read_text(encoding="utf-8")
        for value in ({"start": "25:00", "end": "07:00"},
                      {"start": "22:00", "end": "07:60"},
                      {"start": "22:00", "end": "7"},
                      {"start": "22:00", "end": ""},
                      {"start": "22:00", "end": None},
                      {"start": "22:00", "end": 700},
                      {"start": "22:00"},
                      {"start": "22:00", "end": "07:00", "extra": 1},
                      {},
                      "22:00-07:00",
                      5):
            status, body = self.config_post({"quietHours": value})
            self.assertEqual(status, 400, value)
            self.assertIn("error", json.loads(body))
            self.assertEqual(self.config_path.read_text(encoding="utf-8"), before, value)

    def test_a_non_quiethours_key_is_400_and_writes_nothing(self):
        """allowReply gates a feature; nothing reachable over HTTP may set it."""
        self.config_path.write_text(
            json.dumps({"quietHours": None, "allowReply": False}), encoding="utf-8")
        before = self.config_path.read_text(encoding="utf-8")
        for payload, raw in (({"allowReply": True}, None),
                             ({"quietHours": None, "allowReply": True}, None),
                             ({}, None),
                             ({"quiethours": None}, None),
                             (None, b"not json"),
                             (None, b""),
                             (None, b'"a string"'),
                             (None, b"[1,2]")):
            status, body = self.config_post(payload, raw)
            self.assertEqual(status, 400, (payload, raw))
            self.assertIn("error", json.loads(body))
            self.assertEqual(self.config_path.read_text(encoding="utf-8"), before,
                             (payload, raw))
        self.assertFalse(self.read_config()["allowReply"])

    def test_recap_repos_is_not_settable_over_http(self):
        """File-config only (contract amendment). If this were writable, anything that
        could reach localhost could point crabd's git half at an arbitrary path."""
        self.config_path.write_text(json.dumps(
            {"quietHours": None, "allowReply": False, "recapRepos": ["C:\\Dev\\sidecrab"]}),
            encoding="utf-8")
        before = self.config_path.read_text(encoding="utf-8")
        for payload in ({"recapRepos": ["C:\\Windows"]},
                        {"recapRepos": []},
                        {"quietHours": None, "recapRepos": ["C:\\Windows"]}):
            status, body = self.config_post(payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("error", json.loads(body))
            self.assertEqual(self.config_path.read_text(encoding="utf-8"), before, payload)
        self.assertEqual(self.read_config()["recapRepos"], ["C:\\Dev\\sidecrab"])

    def test_a_quiethours_write_preserves_recap_repos(self):
        self.config_path.write_text(json.dumps(
            {"quietHours": None, "allowReply": False, "recapRepos": ["C:\\Dev\\sidecrab"]}),
            encoding="utf-8")
        self.config_post({"quietHours": {"start": "22:00", "end": "07:00"}})
        after = self.read_config()
        self.assertEqual(after["recapRepos"], ["C:\\Dev\\sidecrab"])
        self.assertEqual(after["quietHours"], {"start": "22:00", "end": "07:00"})

    def test_unknown_keys_survive_the_round_trip(self):
        self.config_path.write_text(json.dumps(
            {"quietHours": None, "allowReply": True, "somethingElse": {"deep": [1, 2]}}),
            encoding="utf-8")
        self.config_post({"quietHours": {"start": "01:00", "end": "02:00"}})
        after = self.read_config()
        self.assertEqual(after["quietHours"], {"start": "01:00", "end": "02:00"})
        self.assertTrue(after["allowReply"])
        self.assertEqual(after["somethingElse"], {"deep": [1, 2]})

    def test_a_missing_config_file_is_created_with_the_defaults_plus_the_new_value(self):
        self.config_path.unlink(missing_ok=True)
        self.assertFalse(self.config_path.exists())
        status, _ = self.config_post({"quietHours": {"start": "22:00", "end": "07:00"}})
        self.assertEqual(status, 204)
        self.assertEqual(self.read_config(),
                         {"quietHours": {"start": "22:00", "end": "07:00"},
                          "allowReply": False})

    def test_the_next_state_reflects_the_write(self):
        """The once-a-minute config damper must be busted by the write, or /v1/state
        would keep serving the old quiet computation for up to 60 s."""
        self.rebuild()
        self.assertIsNone(self.state()["quiet"])
        self.assertTrue(self.config._checked_at)   # the damper is armed right now
        # A window centred on the current local minute, so `active` is True whatever
        # time the suite runs (and wraps correctly across midnight).
        local = time.localtime()
        minute = local.tm_hour * 60 + local.tm_min
        window = {"start": "%02d:%02d" % divmod((minute - 60) % 1440, 60),
                  "end": "%02d:%02d" % divmod((minute + 60) % 1440, 60)}
        self.config_post({"quietHours": window})
        self.rebuild()
        quiet = self.state()["quiet"]
        self.assertEqual((quiet["start"], quiet["end"]), (window["start"], window["end"]))
        self.assertTrue(quiet["active"])

    def test_config_refuses_a_cross_site_web_page(self):
        """SEC-1 on /v1/config, the panelApprovals/allowReply-adjacent write surface: a
        visited page cannot POST config. Origin http/https -> 403."""
        reply = self.client.post("/v1/config",
                                 json.dumps({"quietHours": None}).encode(),
                                 headers={"Origin": "http://evil.example"})
        self.assertEqual(reply.status, 403)

    def test_config_allows_the_widget_null_origin_and_reflects_it(self):
        reply = self.client.post("/v1/config",
                                 json.dumps({"quietHours": None}).encode(),
                                 headers={"Origin": "null"})
        self.assertEqual(reply.status, 204)
        self.assertEqual(reply.headers.get("Access-Control-Allow-Origin"), "null")

    def test_an_unknown_post_path_is_still_404(self):
        self.assertEqual(self.client.post("/v1/configure", b"{}").status, 404)


# ----------------------------------------------------------------- burn.byModel (v5)

class ByModelBurnTests(unittest.TestCase):
    """Contract schema 5: burn.byModel - today's output tokens per model, cap 4 desc."""

    def setUp(self):
        today = time.localtime()
        self.now = time.mktime((today.tm_year, today.tm_mon, today.tm_mday,
                                12, 30, 0, 0, 0, -1))

    def burn(self, requests):
        burn, _ = crabd.StateBuilder._burn(
            requests, {rid: "s1" for rid in requests}, self.now)
        return burn

    @staticmethod
    def record(ts, out, model):
        return (ts, out, 0, 0, 0, model)

    def test_empty_day_is_an_empty_list_not_a_zero_row(self):
        self.assertEqual(self.burn({})["byModel"], [])

    def test_shape_is_model_and_output_tokens_only(self):
        by_model = self.burn({"r": self.record(self.now - 60, 10, "claude-opus-5")})["byModel"]
        self.assertEqual(by_model, [{"model": "claude-opus-5", "outputTokens": 10}])

    def test_model_string_is_served_as_is(self):
        # No normalising, aliasing or prettifying: the widget shows what the transcript
        # said, so a new model id can never be silently mapped onto an old label.
        raw = "claude-opus-5[1m]"
        by_model = self.burn({"r": self.record(self.now - 60, 5, raw)})["byModel"]
        self.assertEqual(by_model[0]["model"], raw)

    def test_records_are_summed_per_model(self):
        by_model = self.burn({
            "r1": self.record(self.now - 60, 100, "claude-fable-5"),
            "r2": self.record(self.now - 120, 50, "claude-fable-5"),
            "r3": self.record(self.now - 180, 70, "claude-opus-5"),
        })["byModel"]
        self.assertEqual(by_model, [{"model": "claude-fable-5", "outputTokens": 150},
                                    {"model": "claude-opus-5", "outputTokens": 70}])

    def test_ordered_by_output_tokens_descending(self):
        by_model = self.burn({
            "r1": self.record(self.now - 60, 10, "m-small"),
            "r2": self.record(self.now - 60, 900, "m-big"),
            "r3": self.record(self.now - 60, 300, "m-mid"),
        })["byModel"]
        self.assertEqual([m["model"] for m in by_model], ["m-big", "m-mid", "m-small"])
        self.assertEqual([m["outputTokens"] for m in by_model], [900, 300, 10])

    def test_ties_break_by_model_name(self):
        # Without a tiebreak the order is dict-insertion order, so the same day's data
        # would render in a different order depending on which transcript parsed first.
        by_model = self.burn({
            "r1": self.record(self.now - 60, 100, "zeta"),
            "r2": self.record(self.now - 60, 100, "alpha"),
            "r3": self.record(self.now - 60, 100, "mid"),
        })["byModel"]
        self.assertEqual([m["model"] for m in by_model], ["alpha", "mid", "zeta"])

    def test_capped_at_four_keeping_the_largest(self):
        requests = {f"r{i}": self.record(self.now - 60, (i + 1) * 10, f"m{i}")
                    for i in range(7)}
        by_model = self.burn(requests)["byModel"]
        self.assertEqual(len(by_model), crabd.BURN_MODEL_CAP)
        self.assertEqual([m["model"] for m in by_model], ["m6", "m5", "m4", "m3"])

    def test_only_todays_records_count(self):
        by_model = self.burn({
            "r_today": self.record(self.now - 3600, 100, "claude-fable-5"),
            "r_yesterday": self.record(self.now - 30 * 3600, 999, "claude-opus-5"),
        })["byModel"]
        self.assertEqual(by_model, [{"model": "claude-fable-5", "outputTokens": 100}])

    def test_local_midnight_is_the_boundary(self):
        midnight = crabd._local_midnight(self.now)
        by_model = self.burn({
            "r_in": self.record(midnight + 1, 10, "in-model"),
            "r_out": self.record(midnight - 1, 20, "out-model"),
        })["byModel"]
        self.assertEqual(by_model, [{"model": "in-model", "outputTokens": 10}])

    # ---- the "unknown" bucket

    def test_missing_model_lands_in_unknown(self):
        by_model = self.burn({"r": self.record(self.now - 60, 42, None)})["byModel"]
        self.assertEqual(by_model, [{"model": "unknown", "outputTokens": 42}])
        self.assertEqual(by_model[0]["model"], crabd.BURN_MODEL_UNKNOWN)

    def test_unknown_records_aggregate_together(self):
        by_model = self.burn({
            "r1": self.record(self.now - 60, 10, None),
            "r2": self.record(self.now - 90, 5, None),
            "r3": self.record(self.now - 90, 100, "claude-fable-5"),
        })["byModel"]
        self.assertEqual(by_model, [{"model": "claude-fable-5", "outputTokens": 100},
                                    {"model": "unknown", "outputTokens": 15}])

    def test_unknown_is_omitted_when_it_carries_no_tokens(self):
        # A zero-token record with no model must not manufacture an "unknown" row -
        # that reads as an identification failure on a day where nothing was spent.
        by_model = self.burn({
            "r_blank": self.record(self.now - 60, 0, None),
            "r_real": self.record(self.now - 60, 30, "claude-fable-5"),
        })["byModel"]
        self.assertEqual(by_model, [{"model": "claude-fable-5", "outputTokens": 30}])

    def test_a_lone_zero_token_unknown_leaves_the_list_empty(self):
        self.assertEqual(self.burn({"r": self.record(self.now - 60, 0, None)})["byModel"], [])

    # ---- the invariant that ties byModel to burn.today

    def test_sum_of_all_models_equals_burn_today_output_tokens(self):
        # THE invariant: byModel is a SPLIT of burn.today.outputTokens over the same
        # deduped records, not a second count. Verified BEYOND the cap - with 9 models
        # the served list holds 4, so this asserts against the uncapped aggregation via
        # a cap-sized case plus the explicit beyond-cap case below.
        requests = {f"r{i}": self.record(self.now - 60 * (i + 1), (i + 1) * 7, f"m{i}")
                    for i in range(3)}
        burn = self.burn(requests)
        self.assertEqual(sum(m["outputTokens"] for m in burn["byModel"]),
                         burn["today"]["outputTokens"])

    def test_beyond_the_cap_the_sum_invariant_holds_on_the_uncapped_aggregation(self):
        # 9 distinct models today: the SERVED list is capped at 4, so the served sum is
        # deliberately LESS than burn.today. The invariant is on the aggregation, so it
        # is asserted by rebuilding the full split from the same records the same way.
        requests = {f"r{i}": self.record(self.now - 60, (i + 1) * 11, f"m{i:02d}")
                    for i in range(9)}
        requests["r_unknown"] = self.record(self.now - 60, 13, None)
        requests["r_yesterday"] = self.record(self.now - 30 * 3600, 9999, "m00")
        burn = self.burn(requests)

        full = {}
        for ts, out, _i, _cr, _cc, model in requests.values():
            if ts >= crabd._local_midnight(self.now):
                key = model or crabd.BURN_MODEL_UNKNOWN
                full[key] = full.get(key, 0) + out

        self.assertEqual(sum(full.values()), burn["today"]["outputTokens"])
        self.assertEqual(len(burn["byModel"]), crabd.BURN_MODEL_CAP)
        self.assertLess(sum(m["outputTokens"] for m in burn["byModel"]),
                        burn["today"]["outputTokens"])
        # And the served rows are exactly the top 4 of that same full split.
        top = sorted(full.items(), key=lambda kv: (-kv[1], kv[0]))[:crabd.BURN_MODEL_CAP]
        self.assertEqual(burn["byModel"],
                         [{"model": m, "outputTokens": t} for m, t in top])

    def test_dedupe_consistency_streamed_repeats_count_once_in_byModel(self):
        # byModel is fed by the SAME deduped requests dict as burn.today. A streamed
        # repeat arriving under one requestId must inflate neither.
        requests = {}
        for i in range(4):  # four models, each seen once (dedupe happens in FileFacts)
            requests[f"req_{i}"] = self.record(self.now - 60, 100, f"m{i}")
        burn = self.burn(requests)
        self.assertEqual(burn["today"]["outputTokens"], 400)
        self.assertEqual(sum(m["outputTokens"] for m in burn["byModel"]), 400)


class ByModelFromTranscriptsTests(TempProjects):
    """byModel end-to-end from real transcript lines, through FileFacts dedupe."""

    def facts_requests(self, session, lines):
        path = self.session_path(session)
        write_jsonl(path, lines)
        facts = crabd.FileFacts(path, session, False)
        facts.refresh()
        return facts

    def test_each_request_carries_the_model_of_its_own_message(self):
        now = time.time()
        facts = self.facts_requests("s-models", [
            assistant_line("req_A", now, output=100, model="claude-fable-5"),
            assistant_line("req_B", now + 1, output=60, model="claude-opus-5"),
        ])
        models = {rid: rec[5] for rid, rec in facts.requests.items()}
        self.assertEqual(models, {"req_A": "claude-fable-5", "req_B": "claude-opus-5"})
        # last_model is still the LAST one seen - byModel must not be built from it.
        self.assertEqual(facts.last_model, "claude-opus-5")

    def test_streamed_repeats_do_not_multiply_a_models_share(self):
        now = time.time()
        facts = self.facts_requests("s-stream", (
            [assistant_line("req_A", now, output=500, model="claude-fable-5")] * 4 +
            [assistant_line("req_B", now, output=250, model="claude-opus-5")] * 3))
        burn, _ = crabd.StateBuilder._burn(
            dict(facts.requests), {r: "s-stream" for r in facts.requests}, now)
        self.assertEqual(burn["byModel"],
                         [{"model": "claude-fable-5", "outputTokens": 500},
                          {"model": "claude-opus-5", "outputTokens": 250}])
        self.assertEqual(sum(m["outputTokens"] for m in burn["byModel"]),
                         burn["today"]["outputTokens"])

    def test_a_message_with_no_model_string_becomes_unknown(self):
        # The earlier line DOES name a model, so `last_model` is populated when the
        # model-less line is parsed. Attributing that record to last_model instead of to
        # its own message would silently fold it into claude-fable-5 and this would pass
        # with byModel == [fable: 177]. Mutation-checked 2026-08-26.
        now = time.time()
        blank = assistant_line("req_B", now + 1, output=77)
        del blank["message"]["model"]
        facts = self.facts_requests("s-nomodel", [
            assistant_line("req_A", now, output=100, model="claude-fable-5"),
            blank,
        ])
        self.assertEqual(facts.last_model, "claude-fable-5")
        self.assertIsNone(facts.requests["req_B"][5])
        burn, _ = crabd.StateBuilder._burn(
            dict(facts.requests), {r: "s-nomodel" for r in facts.requests}, now)
        self.assertEqual(burn["byModel"],
                         [{"model": "claude-fable-5", "outputTokens": 100},
                          {"model": "unknown", "outputTokens": 77}])

    def test_an_empty_model_string_is_unknown_not_an_empty_label(self):
        now = time.time()
        facts = self.facts_requests(
            "s-emptymodel", [assistant_line("req_A", time.time(), output=5, model="")])
        self.assertIsNone(facts.requests["req_A"][5])
        burn, _ = crabd.StateBuilder._burn(
            dict(facts.requests), {"req_A": "s-emptymodel"}, now)
        self.assertEqual(burn["byModel"], [{"model": "unknown", "outputTokens": 5}])

    def test_a_session_that_switches_model_mid_day_splits_both_ways(self):
        now = time.time()
        facts = self.facts_requests("s-switch", [
            assistant_line("req_A", now - 300, output=200, model="claude-sonnet-4-5"),
            assistant_line("req_B", now - 200, output=250, model="claude-opus-5"),
            assistant_line("req_C", now - 100, output=100, model="claude-sonnet-4-5"),
        ])
        burn, _ = crabd.StateBuilder._burn(
            dict(facts.requests), {r: "s-switch" for r in facts.requests}, now)
        self.assertEqual(burn["byModel"],
                         [{"model": "claude-sonnet-4-5", "outputTokens": 300},
                          {"model": "claude-opus-5", "outputTokens": 250}])
        self.assertEqual(burn["today"]["outputTokens"], 550)


# ------------------------------------------------------------ contextTokens (v6)

class ContextTokensTests(TempProjects):
    """Contract v0.6.0 (additive, served under schema 5 per the VERSIONING REWORK):
    sessions[].contextTokens - the input side (input +
    cache_read + cache_creation) of the session's NEWEST assistant usage record."""

    def facts(self, session, lines, is_sub=False):
        path = (self.session_path(session) if not is_sub else
                self.projects / "C--IT" / session / "subagents" / "agent-x.jsonl")
        write_jsonl(path, lines)
        facts = crabd.FileFacts(path, session, is_sub)
        facts.refresh()
        return facts

    def row(self, session_id, now=None):
        _builder, state = self.build(now=now)
        return next(r for r in state["sessions"] if r["id"] == session_id)

    def test_the_input_side_of_the_only_record_is_the_context_size(self):
        facts = self.facts("s-ctx1", [assistant_line(
            "req_A", time.time(), inp=1200, cache_read=140000, cache_create=6000)])
        self.assertEqual(facts.context_tokens, 147200)

    def test_the_newest_record_wins_not_the_largest(self):
        """The context window can SHRINK - /compact, or a resumed session. Serving the
        max would show a window that is no longer full and never recover."""
        now = time.time()
        facts = self.facts("s-ctx2", [
            assistant_line("req_A", now - 300, inp=10, cache_read=500000, cache_create=0),
            assistant_line("req_B", now - 60, inp=20, cache_read=9000, cache_create=100),
        ])
        self.assertEqual(facts.context_tokens, 9120)

    def test_an_out_of_order_older_record_does_not_win(self):
        """Newest is decided by TIMESTAMP, not by file position."""
        now = time.time()
        facts = self.facts("s-ctx3", [
            assistant_line("req_A", now - 30, inp=1, cache_read=70000, cache_create=0),
            assistant_line("req_B", now - 600, inp=1, cache_read=5, cache_create=0),
        ])
        self.assertEqual(facts.context_tokens, 70001)

    def test_streamed_repeats_of_one_request_do_not_change_the_answer(self):
        """The dedupe key is requestId; a repeat carries identical usage, so applying
        it again must be a no-op - and must not displace a LATER request."""
        now = time.time()
        line = assistant_line("req_A", now - 10, inp=5, cache_read=1000, cache_create=5)
        facts = self.facts("s-ctx4", [line, line, line])
        self.assertEqual(facts.context_tokens, 1010)
        self.assertEqual(len(facts.requests), 1)

    def test_the_last_request_wins_over_repeats_that_share_its_timestamp(self):
        """Same whole second, two different requests: the later LINE is the later
        request (the file is append-only), so the tie must go to it."""
        now = time.time()
        first = assistant_line("req_A", now, inp=1, cache_read=400000, cache_create=0)
        second = assistant_line("req_B", now, inp=2, cache_read=8000, cache_create=1)
        facts = self.facts("s-ctx5", [first, first, second, second])
        self.assertEqual(facts.context_tokens, 8003)

    def test_no_usage_record_is_null_not_zero(self):
        facts = self.facts("s-ctx6", [user_line("just a prompt", time.time())])
        self.assertIsNone(facts.context_tokens)

    def test_an_untimestamped_usage_record_is_skipped_not_dated_now(self):
        """A-11 (v0.26.0). A usage-bearing assistant record with no parseable `timestamp`
        of its own AND no earlier record to inherit one from used to be dated `time.time()`
        - the moment crabd happened to read the file. That fabricated clock becomes
        context_ts -> turn_ts -> note_activity, the signal that CLEARS a standing
        needs_input: a re-parse from offset 0 (an eviction + re-admit) could silence a real
        waiting question purely by WHEN the file was read. Skip-don't-guess: the record
        contributes no context and no turn clock, matching the never-500 rule.

        Mutation check: reverting to `ts or self.last_ts or time.time()` gives a non-null
        contextTokens and a now-ish context_ts here."""
        line = assistant_line("req_A", time.time(),
                              inp=1000, cache_read=200000, cache_create=3000)
        line.pop("timestamp")                       # first record, no timestamp at all
        facts = self.facts("s-ctx-a11", [line])
        self.assertIsNone(facts.context_tokens)
        self.assertEqual(facts.context_ts, 0.0)
        self.assertEqual(len(facts.requests), 0)

    def test_a_session_with_no_usage_record_serves_null(self):
        now = time.time()
        write_jsonl(self.session_path("eeeeeeee-0000-0000-0000-00000000000e"),
                    [user_line("just a prompt", now - 30)], mtime=now - 10)
        self.assertIsNone(self.row("eeeeeeee-0000-0000-0000-00000000000e")["contextTokens"])

    def test_a_subagent_record_never_becomes_the_parents_context(self):
        """Subagent transcripts hold their OWN windows. The parent card must show the
        window the operator is watching fill, so a newer, bigger subagent record - the
        shape a fan-out produces constantly - must not be picked up."""
        now = time.time()
        session = "ffffffff-0000-0000-0000-00000000000f"
        write_jsonl(self.session_path(session),
                    [assistant_line("req_main", now - 120, inp=100,
                                    cache_read=30000, cache_create=400)],
                    mtime=now - 20)
        write_jsonl(self.projects / "C--IT" / session / "subagents" / "agent-abc.jsonl",
                    [assistant_line("req_sub", now - 5, inp=9,
                                    cache_read=777777, cache_create=3)],
                    mtime=now - 5)
        self.assertEqual(self.row(session)["contextTokens"], 30500)

    def test_a_subagent_file_still_tracks_its_own_context(self):
        """The exclusion is done by the BUILDER, on is_subagent - FileFacts itself
        stays uniform, so a future subagent view has the number available."""
        facts = self.facts("s-ctx7", [assistant_line(
            "req_S", time.time(), inp=1, cache_read=42, cache_create=0)], is_sub=True)
        self.assertEqual(facts.context_tokens, 43)

    def test_a_reset_transcript_recomputes_rather_than_keeping_the_old_number(self):
        path = self.session_path("s-ctx8")
        write_jsonl(path, [assistant_line("req_A", time.time(),
                                          inp=1, cache_read=90000, cache_create=0)])
        facts = crabd.FileFacts(path, "s-ctx8", False)
        facts.refresh()
        self.assertEqual(facts.context_tokens, 90001)
        facts.reset()
        self.assertIsNone(facts.context_tokens)
        self.assertEqual(facts.context_ts, 0.0)


# -------------------------------------------------------------------- fleet (v6)

class FakeSchtasks:
    """Stands in for `schtasks /query /tn <name>`. Answers per task name; a value that
    is an exception is raised the way the real subprocess would raise it."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, task, timeout):
        self.calls.append(task)
        result = self.results[task]
        if isinstance(result, BaseException):
            raise result
        return result


def schtasks_csv(name, status):
    """The measured shape: quoted name, next-run-time, status. CRLF, no header."""
    return f'"\\{name}","N/A","{status}"\r\n'


OK_GLOW = (0, schtasks_csv("SideCrab-glow", "Running"), "")
# Measured 2026-08-26 on the Windows host against an unregistered task name.
NOT_FOUND = (1, "", "ERROR: The system cannot find the file specified.\r\r\n")


class FleetMappingTests(unittest.TestCase):
    """crabd's half of the fleet contract, with the subprocess mocked.

    `platform=WindowsPlatform()` is explicit rather than defaulted so the schtasks task
    names and the csv mapping keep being proven on EVERY host, not only on the one that
    would have selected that platform anyway.
    """

    @staticmethod
    def reader(results):
        return crabd.FleetReader(runner=FakeSchtasks(results),
                                 platform=crabd.WindowsPlatform())

    def status(self, result):
        return self.reader({"SideCrab-glow": result}).status("SideCrab-glow")

    def test_running_maps_to_running(self):
        self.assertEqual(self.status(OK_GLOW), "running")

    def test_ready_queued_and_disabled_all_map_to_stopped(self):
        for reported in ("Ready", "Queued", "Disabled"):
            with self.subTest(reported=reported):
                self.assertEqual(
                    self.status((0, schtasks_csv("SideCrab-glow", reported), "")),
                    "stopped")

    def test_an_unregistered_task_is_absent(self):
        self.assertEqual(self.status(NOT_FOUND), "absent")

    def test_the_other_not_found_wording_is_also_absent(self):
        self.assertEqual(self.status(
            (1, "", 'ERROR: The specified task name "\\SideCrab-glow" does not exist '
                    'in the system.\r\n')), "absent")

    def test_a_failure_that_is_not_a_not_found_is_unknown_not_stopped(self):
        """Access denied is the case that matters: the task may well be running, and
        calling it stopped would put an amber dot on a healthy component."""
        self.assertEqual(self.status((1, "", "ERROR: Access is denied.\r\n")), "unknown")

    def test_a_timeout_is_unknown(self):
        self.assertEqual(
            self.status(subprocess.TimeoutExpired(cmd="schtasks", timeout=10)), "unknown")

    def test_a_missing_schtasks_is_unknown(self):
        self.assertEqual(self.status(FileNotFoundError("schtasks")), "unknown")

    def test_an_unrecognised_status_word_is_unknown_not_guessed(self):
        self.assertEqual(
            self.status((0, schtasks_csv("SideCrab-glow", "Could not start"), "")),
            "unknown")

    def test_a_clean_exit_with_no_rows_is_unknown(self):
        self.assertEqual(self.status((0, "", "")), "unknown")

    def test_a_status_is_read_case_insensitively(self):
        self.assertEqual(self.status((0, '"\\SideCrab-glow","N/A","RUNNING"\r\n', "")),
                         "running")

    def test_a_task_name_containing_a_comma_does_not_shift_the_status_column(self):
        self.assertEqual(self.status((0, '"\\Side,Crab","N/A","Running"\r\n', "")),
                         "running")

    def test_both_components_are_reported_independently(self):
        fleet = self.reader({"SideCrab-glow": OK_GLOW, "SideCrab-toast": NOT_FOUND})
        fleet.poll(time.time())
        self.assertEqual(fleet.get(), {"glow": "running", "toast": "absent"})

    def test_before_the_first_poll_the_fleet_is_unknown_not_green(self):
        fleet = self.reader({"SideCrab-glow": OK_GLOW, "SideCrab-toast": OK_GLOW})
        self.assertEqual(fleet.get(), {"glow": "unknown", "toast": "unknown"})

    def test_the_query_is_cached_for_sixty_seconds(self):
        runner = FakeSchtasks({"SideCrab-glow": OK_GLOW, "SideCrab-toast": OK_GLOW})
        fleet = crabd.FleetReader(runner=runner, platform=crabd.WindowsPlatform())
        now = time.time()
        self.assertTrue(fleet.poll(now))
        self.assertFalse(fleet.poll(now + crabd.FLEET_REFRESH_SEC - 1))
        self.assertEqual(len(runner.calls), 2)          # one per task, once
        self.assertTrue(fleet.poll(now + crabd.FLEET_REFRESH_SEC + 1))
        self.assertEqual(len(runner.calls), 4)

    def test_get_hands_back_a_copy(self):
        fleet = self.reader({"SideCrab-glow": OK_GLOW, "SideCrab-toast": OK_GLOW})
        fleet.poll(time.time())
        served = fleet.get()
        served["glow"] = "tampered"
        self.assertEqual(fleet.get()["glow"], "running")


class FleetOffRequestPathTests(TempProjects):
    """The contract's other half: `fleet` is computed on its own thread and READ from
    cache. A build (and therefore a /v1/state) that shelled schtasks would put two
    subprocesses in the request path and stall `generatedAt`."""

    def test_building_the_state_never_runs_schtasks(self):
        runner = FakeSchtasks({"SideCrab-glow": OK_GLOW, "SideCrab-toast": OK_GLOW})
        fleet = crabd.FleetReader(runner=runner, platform=crabd.WindowsPlatform())
        builder = crabd.StateBuilder(
            crabd.TranscriptStore(self.projects), crabd.HookTracker(), StubLimits(),
            time.time(), None, None, fleet)
        for _ in range(3):
            state = builder.build()
        self.assertEqual(runner.calls, [])
        self.assertEqual(state["fleet"], {"glow": "unknown", "toast": "unknown"})
        # ...and once the fleet thread's poll has run, the build serves that snapshot.
        fleet.poll(time.time())
        self.assertEqual(builder.build()["fleet"],
                         {"glow": "running", "toast": "running"})
        self.assertEqual(len(runner.calls), 2)

    def test_a_fleet_reader_that_raises_cannot_kill_the_feed(self):
        """_fleet_loop swallows; this proves the poll is where the blast stops, so a
        wedged schtasks leaves the LAST reading standing rather than a dead document."""
        boom = FakeSchtasks({"SideCrab-glow": OK_GLOW,
                             "SideCrab-toast": subprocess.TimeoutExpired("schtasks", 10)})
        fleet = crabd.FleetReader(runner=boom, platform=crabd.WindowsPlatform())
        fleet.poll(time.time())
        self.assertEqual(fleet.get(), {"glow": "running", "toast": "unknown"})


class FleetServedOverASocket(ServedOverASocket):
    """`fleet` on the wire, from a real crabd on a test port."""

    def test_the_served_document_carries_the_fleet_block(self):
        runner = FakeSchtasks({"SideCrab-glow": OK_GLOW,
                               "SideCrab-toast": (0, schtasks_csv("SideCrab-toast",
                                                                  "Ready"), "")})
        self.builder.fleet = crabd.FleetReader(
            runner=runner, platform=crabd.WindowsPlatform())
        self.builder.fleet.poll(time.time())
        with self.builder._lock:
            self.builder._state = self.builder.build()
        state = self.state()
        self.assertEqual(state["fleet"], {"glow": "running", "toast": "stopped"})
        self.assertEqual(sorted(state["fleet"]), ["glow", "toast"])

    def test_the_served_session_row_carries_context_tokens(self):
        row = next(r for r in self.state()["sessions"] if r["id"] == self.SID)
        # ServedOverASocket's fixture line uses the assistant_line defaults: 2+7+9.
        self.assertEqual(row["contextTokens"], 18)


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------- v0.7.0: history persistence

class HistoryTempFile(unittest.TestCase):
    """Every HistoryLog in this suite is built on an explicit temp path. The module
    already points crabd.HISTORY_FILE at a temp file, so even a forgotten path here
    lands in the sandbox, not in ~/.sidecrab."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "history.jsonl"

    def log(self):
        return crabd.HistoryLog(self.path)

    def lines(self, path=None):
        return [json.loads(line) for line
                in (path or self.path).read_text(encoding="utf-8").splitlines()
                if line.strip()]


class HistoryAppendTests(HistoryTempFile):
    def test_a_line_carries_exactly_the_contract_fields(self):
        self.log().append(1756240000.5, "turn finished", "s-1", "fix the collector")
        entry, = self.lines()
        self.assertEqual(sorted(entry), ["kind", "sessionId", "title", "ts"])
        self.assertEqual(entry["kind"], "turn finished")
        self.assertEqual(entry["sessionId"], "s-1")
        self.assertEqual(entry["title"], "fix the collector")
        self.assertAlmostEqual(entry["ts"], 1756240000.5, places=3)

    def test_the_file_is_created_with_its_parent(self):
        nested = Path(self._tmp.name) / "deep" / "er" / "history.jsonl"
        crabd.HistoryLog(nested).append(time.time(), "session started", "s-1")
        self.assertTrue(nested.exists())

    def test_appends_accumulate_in_order(self):
        log = self.log()
        for i, kind in enumerate(("session started", "prompt submitted", "turn finished")):
            log.append(1000000000 + i, kind, "s-1", None)
        self.assertEqual([e["kind"] for e in self.lines()],
                         ["session started", "prompt submitted", "turn finished"])

    def test_a_missing_title_is_null_not_a_guess(self):
        self.log().append(time.time(), "asked a question", "s-1")
        self.assertIsNone(self.lines()[0]["title"])

    def test_an_unwritable_path_never_raises_on_the_hook_path(self):
        """A hook must not fail because the disk did. The parent is occupied by a FILE,
        so mkdir and open both fail - and append still returns."""
        blocker = Path(self._tmp.name) / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")
        crabd.HistoryLog(blocker / "history.jsonl").append(time.time(), "x", "s-1")

    def test_the_path_is_resolved_per_call_so_module_patching_works(self):
        """The isolation guarantee in one assertion: a HistoryLog built with no path
        follows crabd.HISTORY_FILE, which setUpModule has pointed at a temp file."""
        log = crabd.HistoryLog()
        self.assertEqual(log.path, crabd.HISTORY_FILE)
        self.assertNotEqual(log.path, Path.home() / ".sidecrab" / "history.jsonl")


class HistoryRotationTests(HistoryTempFile):
    def test_rotation_at_the_cap_moves_the_file_to_dot_old(self):
        log = self.log()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("x" * (crabd.HISTORY_MAX_BYTES - 10), encoding="utf-8")
        log.append(time.time(), "turn finished", "s-1", "after the roll")
        self.assertTrue(log.old_path.exists())
        self.assertEqual(len(self.lines()), 1)   # the live file restarted
        self.assertEqual(self.lines()[0]["title"], "after the roll")

    def test_only_one_generation_is_kept(self):
        log = self.log()
        log.old_path.parent.mkdir(parents=True, exist_ok=True)
        log.old_path.write_text('{"ts":1,"kind":"turn finished","sessionId":"ancient",'
                                '"title":null}\n', encoding="utf-8")
        self.path.write_text("x" * crabd.HISTORY_MAX_BYTES, encoding="utf-8")
        log.append(time.time(), "turn finished", "s-1")
        # The previous .old was overwritten, not kept as a third file.
        self.assertEqual(sorted(p.name for p in Path(self._tmp.name).iterdir()),
                         ["history.jsonl", "history.jsonl.old"])
        self.assertNotIn("ancient", log.old_path.read_text(encoding="utf-8"))

    def test_a_healthy_sized_file_is_not_rotated(self):
        """The 'would this fire on a healthy night?' question: a normal day's history is
        kilobytes, and rotating it would throw away the week the contract wants."""
        log = self.log()
        for i in range(200):
            log.append(1000000000 + i, "turn finished", f"s-{i}", "a normal title")
        self.assertFalse(log.old_path.exists())
        self.assertEqual(len(self.lines()), 200)

    def test_both_generations_are_replayed_oldest_first(self):
        log = self.log()
        log.old_path.parent.mkdir(parents=True, exist_ok=True)
        log.old_path.write_text('{"ts":100,"kind":"session started","sessionId":"s-1",'
                                '"title":null}\n', encoding="utf-8")
        log.append(200, "turn finished", "s-1")
        self.assertEqual([(e[0], e[1]) for e in log.replay()],
                         [(100.0, "session started"), (200.0, "turn finished")])


class HistoryReplayReadTests(HistoryTempFile):
    def write(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text, encoding="utf-8")

    def test_a_torn_last_line_is_skipped_and_the_rest_survive(self):
        """An append-only file truncates mid-line on power loss. A crabd that refuses to
        start over one short byte is worse than one that forgot one event."""
        self.write('{"ts":100,"kind":"turn finished","sessionId":"s-1","title":null}\n'
                   '{"ts":200,"kind":"asked a quest')
        entries = self.log().replay()
        self.assertEqual([(e[0], e[1]) for e in entries], [(100.0, "turn finished")])

    def test_nul_padding_after_a_power_loss_is_skipped(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(
            b'{"ts":100,"kind":"turn finished","sessionId":"s-1","title":null}\n'
            + b"\x00" * 512)
        self.assertEqual(len(self.log().replay()), 1)

    def test_garbage_and_blank_lines_are_skipped_silently(self):
        self.write('\n'
                   'not json at all\n'
                   '[1,2,3]\n'
                   '"a string"\n'
                   '{"ts":100,"kind":"turn finished","sessionId":"s-1","title":null}\n'
                   '   \n')
        self.assertEqual(len(self.log().replay()), 1)

    def test_records_missing_a_required_field_are_skipped(self):
        self.write('{"kind":"turn finished","sessionId":"s-1"}\n'
                   '{"ts":100,"sessionId":"s-1"}\n'
                   '{"ts":100,"kind":"turn finished"}\n'
                   '{"ts":100,"kind":"","sessionId":"s-1"}\n'
                   '{"ts":100,"kind":"turn finished","sessionId":""}\n'
                   '{"ts":"not a time","kind":"turn finished","sessionId":"s-1"}\n'
                   '{"ts":100,"kind":5,"sessionId":"s-1"}\n'
                   '{"ts":100,"kind":"turn finished","sessionId":"s-1"}\n')
        self.assertEqual(len(self.log().replay()), 1)

    def test_a_non_string_title_reads_as_no_title_not_an_error(self):
        self.write('{"ts":100,"kind":"turn finished","sessionId":"s-1","title":42}\n')
        self.assertIsNone(self.log().replay()[0][3])

    def test_a_missing_file_replays_as_nothing(self):
        self.assertEqual(self.log().replay(), [])

    def test_appending_after_a_torn_line_still_replays_the_good_records(self):
        """The real sequence: crabd died mid-write, restarted, kept appending."""
        self.write('{"ts":100,"kind":"turn finished","sessionId":"s-1","title":null}\n'
                   '{"ts":200,"kind":"asked')
        log = self.log()
        log.append(300, "session started", "s-2")
        self.assertEqual([(e[0], e[1]) for e in log.replay()],
                         [(100.0, "turn finished"), (300.0, "session started")])


class HookHistoryWriteTests(HistoryTempFile):
    SID = "aaaa0000-0000-0000-0000-0000000000a1"

    def tracker(self):
        return crabd.HookTracker(history=self.log())

    def hook(self, tracker, event, sid=None, **extra):
        tracker.record({"session_id": sid or self.SID, "hook_event_name": event, **extra})

    def test_a_bare_tracker_writes_nothing_anywhere(self):
        """No history object = no persistence. The default must never create a file."""
        tracker = crabd.HookTracker()
        self.hook(tracker, "Stop")
        self.assertFalse(self.path.exists())
        self.assertFalse(crabd.HISTORY_FILE.exists())

    def test_every_ring_event_is_persisted(self):
        tracker = self.tracker()
        for event in ("SessionStart", "UserPromptSubmit", "Notification", "SubagentStop"):
            self.hook(tracker, event, message="which host?")
        self.assertEqual([e["kind"] for e in self.lines()],
                         ["session started", "prompt submitted", "asked a question",
                          "subagent finished"])

    def test_a_done_transition_writes_its_own_line_beside_the_ring_event(self):
        tracker = self.tracker()
        self.hook(tracker, "UserPromptSubmit")
        self.hook(tracker, "Stop")
        self.assertEqual([e["kind"] for e in self.lines()],
                         ["prompt submitted", "turn finished", crabd.HISTORY_DONE_KIND])

    def test_a_repeated_stop_writes_no_second_done_line(self):
        """The ring records every Stop; only a Stop that MOVED the state is a done."""
        tracker = self.tracker()
        self.hook(tracker, "Stop")
        self.hook(tracker, "Stop")
        kinds = [e["kind"] for e in self.lines()]
        self.assertEqual(kinds.count(crabd.HISTORY_DONE_KIND), 1)
        self.assertEqual(kinds.count("turn finished"), 2)

    def test_an_unknown_hook_writes_nothing(self):
        tracker = self.tracker()
        self.hook(tracker, "PreToolUse")
        self.assertFalse(self.path.exists())

    def test_an_ack_is_persisted(self):
        tracker = self.tracker()
        self.hook(tracker, "Notification", message="which host?")
        tracker.ack(self.SID)
        self.assertEqual([e["kind"] for e in self.lines()][-1],
                         crabd.HookTracker.ACK_EVENT)

    def test_no_question_text_ever_reaches_the_file(self):
        """The contract's privacy line, asserted rather than trusted: the Notification
        message becomes `lastEvent` and `question` in memory, and NEITHER is written."""
        tracker = self.tracker()
        tracker.note_titles({self.SID: "a session"})
        self.hook(tracker, "Notification",
                  message="Should I drop the production database, yes or no?")
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("production database", raw)
        self.assertNotIn("drop", raw)
        self.assertEqual([e["kind"] for e in self.lines()], ["asked a question"])

    def test_the_title_at_the_time_rides_along(self):
        tracker = self.tracker()
        tracker.note_titles({self.SID: "wire the collector"})
        self.hook(tracker, "Stop")
        self.assertEqual(self.lines()[0]["title"], "wire the collector")

    def test_a_title_learned_later_does_not_rewrite_earlier_lines(self):
        """title-at-the-time, not title-now: the file is a log, not a view."""
        tracker = self.tracker()
        self.hook(tracker, "SessionStart")
        tracker.note_titles({self.SID: "learned afterwards"})
        self.hook(tracker, "Stop")
        titles = [e["title"] for e in self.lines()]
        self.assertIsNone(titles[0])
        self.assertEqual(titles[1], "learned afterwards")


class HookReplayTests(HistoryTempFile):
    SID_A = "aaaa1111-0000-0000-0000-00000000000a"
    SID_B = "bbbb2222-0000-0000-0000-00000000000b"

    def restart(self):
        """A NEW tracker over the same file - exactly what a crabd restart does."""
        log = self.log()
        tracker = crabd.HookTracker(history=log)
        tracker.replay(log.replay())
        return tracker

    def hook(self, tracker, event, sid, **extra):
        tracker.record({"session_id": sid, "hook_event_name": event, **extra})

    def test_done_today_survives_a_restart(self):
        first = crabd.HookTracker(history=self.log())
        self.hook(first, "Stop", self.SID_A)
        self.hook(first, "Stop", self.SID_B)
        self.assertEqual(first.done_today(), 2)
        self.assertEqual(self.restart().done_today(), 2)

    def test_the_events_ring_survives_a_restart_newest_first(self):
        first = crabd.HookTracker(history=self.log())
        for event in ("SessionStart", "UserPromptSubmit", "Notification", "Stop"):
            self.hook(first, event, self.SID_A, message="which host?")
        after = self.restart().snapshot()[self.SID_A]["events"]
        self.assertEqual([e["text"] for e in after],
                         ["turn finished", "asked a question", "prompt submitted",
                          "session started"])

    def test_the_replayed_ring_is_still_capped(self):
        first = crabd.HookTracker(history=self.log())
        for _ in range(6):
            self.hook(first, "UserPromptSubmit", self.SID_A)
            self.hook(first, "Stop", self.SID_A)
        events = self.restart().snapshot()[self.SID_A]["events"]
        self.assertEqual(len(events), crabd.EVENTS_CAP)
        self.assertEqual(events[0]["text"], "turn finished")

    def test_replay_restores_facts_but_never_state(self):
        """A 'working' row from before the restart would claim a turn is running that
        this process has no hook to finish."""
        first = crabd.HookTracker(history=self.log())
        self.hook(first, "Notification", self.SID_A, message="which host?")
        row = self.restart().snapshot()[self.SID_A]
        self.assertIsNone(row["state"])
        self.assertIsNone(row["question"])
        self.assertFalse(row["acked"])
        self.assertEqual([e["text"] for e in row["events"]], ["asked a question"])

    def test_a_replay_does_not_rewrite_the_file(self):
        """Replay-then-persist would double every event on each restart, and doneToday
        would still look right - which is exactly why this is asserted on the FILE."""
        first = crabd.HookTracker(history=self.log())
        self.hook(first, "Stop", self.SID_A)
        before = self.path.read_text(encoding="utf-8")
        self.restart()
        self.assertEqual(self.path.read_text(encoding="utf-8"), before)

    def test_restarting_repeatedly_does_not_inflate_done_today(self):
        first = crabd.HookTracker(history=self.log())
        self.hook(first, "Stop", self.SID_A)
        for _ in range(3):
            tracker = self.restart()
        self.assertEqual(tracker.done_today(), 1)

    def test_a_new_stop_after_a_restart_appends_rather_than_replacing(self):
        first = crabd.HookTracker(history=self.log())
        self.hook(first, "Stop", self.SID_A)
        second = self.restart()
        self.hook(second, "Stop", self.SID_B)
        self.assertEqual(second.done_today(), 2)
        self.assertEqual(self.restart().done_today(), 2)

    def test_yesterdays_done_lines_replay_but_do_not_count_today(self):
        log = self.log()
        yesterday = crabd._local_midnight(time.time()) - 3600
        log.append(yesterday, crabd.HISTORY_DONE_KIND, self.SID_A, None)
        tracker = self.restart()
        self.assertEqual(tracker.done_today(), 0)
        self.assertEqual(len(tracker.dones), 1)   # still there for recap.week

    def test_done_lines_past_the_keep_window_are_not_replayed(self):
        log = self.log()
        log.append(time.time() - crabd.RECAP_DONE_KEEP_SEC - 60,
                   crabd.HISTORY_DONE_KIND, self.SID_A, None)
        self.assertEqual(self.restart().dones, [])

    def test_ring_events_older_than_the_replay_window_are_dropped(self):
        """They can never reach a served row, so replaying them would only grow the
        session table with rows nothing can render."""
        log = self.log()
        log.append(time.time() - crabd.HISTORY_REPLAY_SEC - 60,
                   "turn finished", self.SID_A, None)
        self.assertEqual(self.restart().snapshot(), {})

    def test_an_unknown_kind_replays_as_a_ring_event(self):
        """Forward compatibility: a newer crabd's event text must not vanish."""
        log = self.log()
        log.append(time.time(), "reticulated the splines", self.SID_A, None)
        events = self.restart().snapshot()[self.SID_A]["events"]
        self.assertEqual([e["text"] for e in events], ["reticulated the splines"])

    def test_a_stale_hookless_row_is_pruned_rather_than_kept_forever(self):
        tracker = self.restart()
        stale = time.time() - crabd.GONE_AFTER_SEC - 60
        tracker.sessions[self.SID_A] = crabd.HookTracker._blank(stale)
        tracker.prune(time.time())
        self.assertNotIn(self.SID_A, tracker.sessions)

    def test_a_fresh_hookless_row_is_not_pruned(self):
        """The ack-on-a-transcript-only-session row: state is None and it is LIVE."""
        tracker = self.restart()
        tracker.ack(self.SID_A, create=True)
        tracker.prune(time.time())
        self.assertIn(self.SID_A, tracker.sessions)


class NeedsInputBoundTests(unittest.TestCase):
    """A-05 (v0.26.0). needs_input keeps its GONE_AFTER_SEC exemption - a question waits
    even when the transcript goes quiet - but not its old TOTAL exemption: unbounded, a
    hook flood or a pile of abandoned questions grew the tracker, `_titles` and the served
    array forever (every needs_input row is served on every poll). Two generous, oldest-
    first ceilings now trim runaway growth while NEVER evicting a genuinely recent waiting
    prompt - the healthy-night rule is the sort direction."""

    @staticmethod
    def _ni(tracker, sid, since, at):
        row = crabd.HookTracker._blank(since)
        row["state"] = "needs_input"
        row["since"] = since
        row["at"] = at
        row["question"] = "waiting on you?"
        tracker.sessions[sid] = row

    def test_a_flood_is_bounded_and_the_recent_waiting_row_survives(self):
        """Mutation check: removing the count cap leaves len(needs_input) above the ceiling."""
        tracker = crabd.HookTracker()
        now = time.time()
        n = crabd.NEEDS_INPUT_MAX_ROWS + 100
        # A flood, all recent enough to dodge the AGE ceiling, ascending `at` so the
        # oldest is unambiguous.
        for i in range(n):
            self._ni(tracker, f"flood-{i:05d}", since=now - (n - i), at=now - (n - i))
        self._ni(tracker, "fresh-2am", since=now - 1, at=now - 1)   # newest `at`
        tracker.prune(now)
        ni = [sid for sid, r in tracker.sessions.items() if r["state"] == "needs_input"]
        self.assertLessEqual(len(ni), crabd.NEEDS_INPUT_MAX_ROWS)
        self.assertIn("fresh-2am", tracker.sessions)        # the recent one is never dropped
        self.assertNotIn("flood-00000", tracker.sessions)   # the oldest by `at` went first

    def test_the_age_ceiling_drops_only_the_truly_ancient(self):
        """A row older than the age ceiling is stale; one merely past GONE_AFTER_SEC (which
        proves the exemption still holds) keeps waiting. Mutation check: removing the age
        eviction keeps `ancient`."""
        tracker = crabd.HookTracker()
        now = time.time()
        old = now - crabd.NEEDS_INPUT_MAX_AGE_SEC - 60
        self._ni(tracker, "ancient", since=old, at=old)
        # Past GONE_AFTER_SEC (would drop any OTHER state) but well within the ceiling.
        waiting = now - crabd.GONE_AFTER_SEC - 60
        self._ni(tracker, "waiting", since=waiting, at=waiting)
        tracker.prune(now)
        self.assertNotIn("ancient", tracker.sessions)
        self.assertIn("waiting", tracker.sessions)          # the exemption still holds


class ReplayFeedsTheServedDocumentTests(TempProjects):
    """End to end: a replayed history reaches recap.doneToday and sessions[].events."""

    SID = "cccc3333-0000-0000-0000-00000000000c"

    def setUp(self):
        super().setUp()
        self.history_path = Path(self._tmp.name) / "history.jsonl"

    def test_a_replayed_done_reaches_the_served_recap(self):
        log = crabd.HistoryLog(self.history_path)
        log.append(time.time() - 60, crabd.HISTORY_DONE_KIND, "some-earlier-session",
                   "this morning's work")
        hooks = crabd.HookTracker(history=log)
        hooks.replay(log.replay())
        recap = crabd.RecapReader(runner=FakeGit({}), week_runner=FakeWeekGit({}))
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects), hooks,
                                     StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path), recap)
        builder.build()
        recap.poll(time.time())
        self.assertEqual(recap.get()["doneToday"], 1)

    def test_a_replayed_events_ring_reaches_the_served_session_row(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID),
                    [user_line("go", now - 60), assistant_line("r1", now - 30)],
                    mtime=now - 5)
        log = crabd.HistoryLog(self.history_path)
        log.append(now - 40, "prompt submitted", self.SID, "the running session")
        hooks = crabd.HookTracker(history=log)
        hooks.replay(log.replay())
        _, state = self.build(now=now, hooks=hooks)
        row = next(r for r in state["sessions"] if r["id"] == self.SID)
        self.assertEqual([e["text"] for e in row["events"]], ["prompt submitted"])
        self.assertEqual(row["state"], "working")   # state is aged, never replayed

    def test_the_builder_feeds_titles_back_to_the_tracker(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID),
                    [user_line("wire the collector", now - 60),
                     assistant_line("r1", now - 30)], mtime=now - 5)
        log = crabd.HistoryLog(self.history_path)
        hooks = crabd.HookTracker(history=log)
        self.build(now=now, hooks=hooks)
        hooks.record({"session_id": self.SID, "hook_event_name": "Stop"})
        entry = json.loads(self.history_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(entry["title"], "wire the collector")


# ------------------------------------------------------------ v0.7.0: recap.week

class FakeWeekGit:
    """Stands in for the batched `git log --since --until --format=%cd`. Maps cwd -> a
    list of local day strings (one per commit), an exception, or None for 'git said no'."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    def __call__(self, cwd, since, until):
        self.calls.append((cwd, since, until))
        result = self.results.get(cwd)
        if isinstance(result, BaseException):
            raise result
        return result


class RecapWeekTests(unittest.TestCase):
    """The week half: 7 local days oldest first, done from history, commits from git."""

    @staticmethod
    def days(count=crabd.WEEK_DAYS, now=None):
        now = now if now is not None else time.time()
        return [crabd._local_day(d) for d in crabd._local_day_starts(now, count)]

    def reader(self, week_results=None):
        return crabd.RecapReader(runner=FakeGit({}),
                                 week_runner=FakeWeekGit(week_results or {}))

    def test_seven_days_oldest_first_with_the_contract_shape(self):
        days = self.days()
        recap = self.reader({"C:\\a": [days[-1]] * 3})
        recap.submit(1, 0, [("a", "C:\\a")], [(d, 0) for d in days])
        recap.poll(time.time())
        week = recap.get()["week"]
        self.assertEqual(len(week), 7)
        self.assertEqual([d["day"] for d in week], days)
        self.assertEqual(sorted(week[0]), ["commits", "day", "done"])
        self.assertEqual(week[-1]["commits"], 3)

    def test_an_empty_day_is_a_zero_row_not_a_missing_one(self):
        """Unlike `commits`, a quiet day IS the answer here - the week is a shape the
        widget draws, and a hole in it would read as missing data."""
        days = self.days()
        recap = self.reader({"C:\\a": [days[0], days[-1]]})
        recap.submit(1, 0, [("a", "C:\\a")], [(d, 0) for d in days])
        recap.poll(time.time())
        week = recap.get()["week"]
        self.assertEqual([d["commits"] for d in week], [1, 0, 0, 0, 0, 0, 1])
        self.assertEqual([d["done"] for d in week], [0] * 7)

    def test_commits_are_summed_across_every_repo_in_scope(self):
        """`commits` caps at 4 repos; the week does not - it is a day total."""
        days = self.days()
        today = days[-1]
        recap = self.reader({f"C:\\r{i}": [today] * 2 for i in range(6)})
        recap.submit(1, 0, [(f"r{i}", f"C:\\r{i}") for i in range(6)],
                     [(d, 0) for d in days])
        recap.poll(time.time())
        self.assertEqual(recap.get()["week"][-1]["commits"], 12)

    def test_one_git_call_per_repo_covers_all_seven_days(self):
        """Seven calls per repo would be seven process spawns each; the batch is the
        whole reason the week is affordable on a dozen-repo scope."""
        runner = FakeWeekGit({f"C:\\r{i}": [] for i in range(5)})
        recap = crabd.RecapReader(runner=FakeGit({}), week_runner=runner)
        days = self.days()
        recap.submit(1, 0, [(f"r{i}", f"C:\\r{i}") for i in range(5)],
                     [(d, 0) for d in days])
        recap.poll(time.time())
        self.assertEqual(len(runner.calls), 5)
        self.assertEqual([c[0] for c in runner.calls], [f"C:\\r{i}" for i in range(5)])
        # The window starts at the oldest bucket's local midnight and ends now.
        self.assertTrue(runner.calls[0][1].startswith(days[0]))
        self.assertTrue(runner.calls[0][2].startswith(days[-1]))

    def test_the_week_git_scan_is_capped_like_the_commits_one(self):
        runner = FakeWeekGit({f"C:\\r{i}": [] for i in range(30)})
        recap = crabd.RecapReader(runner=FakeGit({}), week_runner=runner)
        recap.submit(1, 0, [(f"r{i}", f"C:\\r{i}") for i in range(30)],
                     [(d, 0) for d in self.days()])
        recap.poll(time.time())
        self.assertEqual(len(runner.calls), crabd.RECAP_REPO_SCAN_CAP)

    def test_a_day_outside_the_seven_is_dropped_not_misfiled(self):
        days = self.days()
        older = crabd._local_day(crabd._local_day_starts(time.time(), 9)[0])
        recap = self.reader({"C:\\a": [older, older, days[-1]]})
        recap.submit(1, 0, [("a", "C:\\a")], [(d, 0) for d in days])
        recap.poll(time.time())
        week = recap.get()["week"]
        self.assertEqual(sum(d["commits"] for d in week), 1)
        self.assertEqual(week[-1]["commits"], 1)

    def test_a_wedged_repo_is_skipped_and_the_rest_still_count(self):
        days = self.days()
        recap = self.reader({"C:\\wedged": subprocess.TimeoutExpired(cmd="git", timeout=10),
                             "C:\\fine": [days[-1]]})
        recap.submit(1, 0, [("wedged", "C:\\wedged"), ("fine", "C:\\fine")],
                     [(d, 0) for d in days])
        recap.poll(time.time())
        self.assertEqual(recap.get()["week"][-1]["commits"], 1)

    def test_a_non_repo_is_skipped_rather_than_counted_as_zero(self):
        days = self.days()
        recap = self.reader({"C:\\notarepo": None, "C:\\real": [days[-1]]})
        recap.submit(1, 0, [("notarepo", "C:\\notarepo"), ("real", "C:\\real")],
                     [(d, 0) for d in days])
        recap.poll(time.time())
        self.assertEqual(recap.get()["week"][-1]["commits"], 1)

    def test_the_done_column_comes_straight_from_the_submitted_history(self):
        days = self.days()
        submitted = [(d, i) for i, d in enumerate(days)]
        recap = self.reader({})
        recap.submit(1, 6, [], submitted)
        recap.poll(time.time())
        self.assertEqual([(d["day"], d["done"]) for d in recap.get()["week"]], submitted)

    def test_a_served_copy_cannot_mutate_the_cached_week(self):
        days = self.days()
        recap = self.reader({"C:\\a": [days[-1]]})
        recap.submit(1, 0, [("a", "C:\\a")], [(d, 0) for d in days])
        recap.poll(time.time())
        recap.get()["week"][0]["commits"] = 999
        self.assertEqual(recap.get()["week"][0]["commits"], 0)


class DoneByDayTests(unittest.TestCase):
    """HookTracker.done_by_day - the `done` column, bucketed on the local day."""

    SID_A = "aaaa1111-0000-0000-0000-00000000000a"
    SID_B = "bbbb2222-0000-0000-0000-00000000000b"

    def test_seven_buckets_oldest_first_including_the_empty_days(self):
        tracker = crabd.HookTracker()
        buckets = tracker.done_by_day()
        self.assertEqual(len(buckets), crabd.WEEK_DAYS)
        self.assertEqual([n for _, n in buckets], [0] * 7)
        self.assertEqual([d for d, _ in buckets], sorted(d for d, _ in buckets))

    def test_todays_finishes_land_in_the_last_bucket(self):
        tracker = crabd.HookTracker()
        tracker.dones = [(time.time(), self.SID_A), (time.time(), self.SID_B)]
        self.assertEqual(tracker.done_by_day()[-1][1], 2)

    def test_a_session_that_finished_twice_in_a_day_counts_once(self):
        tracker = crabd.HookTracker()
        now = time.time()
        tracker.dones = [(now - 300, self.SID_A), (now, self.SID_A)]
        self.assertEqual(tracker.done_by_day()[-1][1], 1)

    def test_the_same_session_on_two_days_counts_on_both(self):
        tracker = crabd.HookTracker()
        now = time.time()
        yesterday = crabd._local_midnight(now) - 3600
        tracker.dones = [(yesterday, self.SID_A), (now, self.SID_A)]
        buckets = dict(tracker.done_by_day(now))
        self.assertEqual(buckets[crabd._local_day(yesterday)], 1)
        self.assertEqual(buckets[crabd._local_day(now)], 1)

    def test_the_boundary_is_local_midnight_to_the_second(self):
        now = time.time()
        midnight = crabd._local_midnight(now)
        tracker = crabd.HookTracker()
        tracker.dones = [(midnight - 1, self.SID_A), (midnight, self.SID_B)]
        buckets = dict(tracker.done_by_day(now))
        self.assertEqual(buckets[crabd._local_day(now)], 1)
        self.assertEqual(buckets[crabd._local_day(midnight - 1)], 1)

    def test_a_finish_older_than_the_week_is_in_no_bucket(self):
        tracker = crabd.HookTracker()
        now = time.time()
        tracker.dones = [(now - 30 * 86400, self.SID_A)]
        self.assertEqual([n for _, n in tracker.done_by_day(now)], [0] * 7)

    def test_the_keep_window_covers_the_whole_week(self):
        """The ring has to outlive the oldest bucket, or the week's left edge would read
        0 for a reason that has nothing to do with the operator's week."""
        now = time.time()
        oldest = crabd._local_day_starts(now, crabd.WEEK_DAYS)[0]
        self.assertLess(now - crabd.RECAP_DONE_KEEP_SEC, oldest)

    def test_the_last_bucket_agrees_with_done_today(self):
        tracker = crabd.HookTracker()
        now = time.time()
        tracker.dones = [(now, self.SID_A), (now - 60, self.SID_B),
                         (crabd._local_midnight(now) - 60, "yesterday")]
        self.assertEqual(tracker.done_by_day(now)[-1][1], tracker.done_today(now))


class WeekReachesTheServedDocumentTests(TempProjects):
    SID = "dddd4444-0000-0000-0000-00000000000d"

    def test_the_builder_submits_seven_done_buckets_and_they_are_served(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID),
                    [user_line("go", now - 60), assistant_line("r1", now - 30)],
                    mtime=now - 5)
        hooks = crabd.HookTracker()
        hooks.record({"session_id": self.SID, "hook_event_name": "Stop", "cwd": "C:\\IT"})
        recap = crabd.RecapReader(runner=FakeGit({}), week_runner=FakeWeekGit({}))
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects), hooks,
                                     StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path), recap)
        builder.build(now=now)
        recap.poll(now)
        week = recap.get()["week"]
        self.assertEqual(len(week), crabd.WEEK_DAYS)
        self.assertEqual(week[-1]["done"], 1)
        self.assertEqual(week[-1]["day"], crabd._local_day(now))
        self.assertEqual([d["commits"] for d in week], [0] * 7)

    def test_a_recap_with_no_week_input_serves_an_empty_week_not_a_fake_one(self):
        recap = crabd.RecapReader(runner=FakeGit({}), week_runner=FakeWeekGit({}))
        recap.submit(1, 0, [])
        recap.poll(time.time())
        self.assertEqual(recap.get()["week"], [])


# --------------------------------------------------- v0.7.0: POST /v1/config toast

class ConfigToastTests(AckAllAndConfigTests):
    """The second writable key. Inherits the socket fixture and the config_post helper."""

    GOOD = {"thresholdSec": 120, "enabled": True}

    def seed(self):
        self.config_path.write_text(
            json.dumps({"quietHours": None, "allowReply": False}), encoding="utf-8")
        return self.config_path.read_text(encoding="utf-8")

    def test_toast_alone_is_valid(self):
        status, _ = self.config_post({"toast": self.GOOD})
        self.assertEqual(status, 204)
        self.assertEqual(self.read_config()["toast"], self.GOOD)

    def test_both_keys_in_one_body_are_valid(self):
        status, _ = self.config_post({"quietHours": {"start": "22:00", "end": "07:00"},
                                      "toast": self.GOOD})
        self.assertEqual(status, 204)
        config = self.read_config()
        self.assertEqual(config["quietHours"], {"start": "22:00", "end": "07:00"})
        self.assertEqual(config["toast"], self.GOOD)

    def test_quiet_hours_alone_still_works_unchanged(self):
        status, _ = self.config_post({"quietHours": {"start": "22:00", "end": "07:00"}})
        self.assertEqual(status, 204)
        self.assertEqual(self.read_config()["quietHours"],
                         {"start": "22:00", "end": "07:00"})
        self.assertNotIn("toast", self.read_config())

    def test_the_threshold_range_is_inclusive_at_both_ends(self):
        for seconds in (crabd.CONFIG_TOAST_MIN_SEC, crabd.CONFIG_TOAST_MAX_SEC):
            status, _ = self.config_post(
                {"toast": {"thresholdSec": seconds, "enabled": False}})
            self.assertEqual(status, 204, seconds)
            self.assertEqual(self.read_config()["toast"]["thresholdSec"], seconds)

    def test_an_invalid_toast_is_400_and_writes_nothing(self):
        before = self.seed()
        for value in ({"thresholdSec": 29, "enabled": True},          # under the floor
                      {"thresholdSec": 3601, "enabled": True},        # over the ceiling
                      {"thresholdSec": 0, "enabled": True},
                      {"thresholdSec": -60, "enabled": True},
                      {"thresholdSec": 120},                          # missing enabled
                      {"enabled": True},                              # missing threshold
                      {"thresholdSec": 120, "enabled": True, "extra": 1},
                      {"thresholdSec": "120", "enabled": True},
                      {"thresholdSec": 120.5, "enabled": True},
                      {"thresholdSec": None, "enabled": True},
                      {"thresholdSec": True, "enabled": True},        # bool is not a time
                      {"thresholdSec": 120, "enabled": 1},            # 1 is not a bool
                      {"thresholdSec": 120, "enabled": "true"},
                      {"thresholdSec": 120, "enabled": None},
                      {},
                      None,
                      "on",
                      120,
                      [120, True]):
            status, body = self.config_post({"toast": value})
            self.assertEqual(status, 400, value)
            self.assertIn("error", json.loads(body))
            self.assertEqual(self.config_path.read_text(encoding="utf-8"), before, value)

    def test_a_bad_half_of_a_two_key_body_writes_neither_half(self):
        """Rejected WHOLE: a body that half-validates must not leave quiet hours set and
        the toast block missing - the operator could not tell which landed."""
        before = self.seed()
        for payload in ({"quietHours": {"start": "22:00", "end": "07:00"},
                         "toast": {"thresholdSec": 5, "enabled": True}},
                        {"quietHours": {"start": "99:00", "end": "07:00"},
                         "toast": self.GOOD}):
            status, _ = self.config_post(payload)
            self.assertEqual(status, 400, payload)
            self.assertEqual(self.config_path.read_text(encoding="utf-8"), before, payload)

    def test_unknown_keys_are_still_rejected_alongside_a_valid_toast(self):
        before = self.seed()
        for payload in ({"toast": self.GOOD, "allowReply": True},
                        {"toast": self.GOOD, "recapRepos": ["C:\\Windows"]},
                        {"Toast": self.GOOD},
                        {}):
            status, body = self.config_post(payload)
            self.assertEqual(status, 400, payload)
            self.assertIn("error", json.loads(body))
            self.assertEqual(self.config_path.read_text(encoding="utf-8"), before, payload)

    def test_a_toast_write_preserves_every_other_key(self):
        self.config_path.write_text(json.dumps(
            {"quietHours": {"start": "22:00", "end": "07:00"}, "allowReply": True,
             "recapRepos": ["C:\\Dev\\sidecrab"], "somethingElse": {"deep": [1, 2]}}),
            encoding="utf-8")
        self.assertEqual(self.config_post({"toast": self.GOOD})[0], 204)
        after = self.read_config()
        self.assertEqual(after["quietHours"], {"start": "22:00", "end": "07:00"})
        self.assertTrue(after["allowReply"])
        self.assertEqual(after["recapRepos"], ["C:\\Dev\\sidecrab"])
        self.assertEqual(after["somethingElse"], {"deep": [1, 2]})
        self.assertEqual(after["toast"], self.GOOD)

    def test_a_toast_write_does_not_disturb_the_served_quiet_block(self):
        self.config_post({"quietHours": {"start": "22:00", "end": "07:00"}})
        self.config_post({"toast": self.GOOD})
        self.rebuild()
        quiet = self.state()["quiet"]
        self.assertEqual((quiet["start"], quiet["end"]), ("22:00", "07:00"))

    def test_an_existing_toast_block_is_replaced_whole(self):
        self.config_post({"toast": {"thresholdSec": 300, "enabled": False}})
        self.config_post({"toast": self.GOOD})
        self.assertEqual(self.read_config()["toast"], self.GOOD)

    def test_the_notifier_reads_back_what_the_endpoint_wrote(self):
        """The two lanes meet in this file, so the round trip is asserted across it
        rather than assumed: crabd writes, the notifier's own parser reads."""
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "notifier"))
        import sidecrab_toast   # noqa: E402
        self.config_post({"toast": {"thresholdSec": 900, "enabled": False}})
        parsed = sidecrab_toast.parse_toast_config(self.read_config())
        self.assertEqual(parsed.threshold_sec, 900)
        self.assertFalse(parsed.enabled)


class ConfigApprovalThresholdTests(ServedOverASocket):
    """`toast.approvalThresholdSec` - the OPTIONAL third member (v0.16.0).

    The defect: `_validate_toast` required the block to be EXACTLY
    {thresholdSec, enabled} and set_keys writes the block back whole, so every panel
    settings save deleted a hand-edited approvalThresholdSec and the notifier silently
    fell back to its 20 s default. Nothing errored; the operator's setting was just gone.

    Two halves, and the second is the one that actually fixes it - the widget does not
    know the key exists, so ACCEPTING it is useless on its own: a write that OMITS it
    must PRESERVE what is on disk.
    """

    GOOD = {"thresholdSec": 120, "enabled": True}
    WITH_APPROVAL = {"thresholdSec": 120, "enabled": True, "approvalThresholdSec": 20}

    # The socket fixture only, NOT AckAllAndConfigTests: inheriting that class would
    # re-run its whole ack-all/config suite a third time for no new coverage.
    def config_post(self, payload):
        reply = self.client.post("/v1/config", json.dumps(payload).encode())
        return reply.status, reply.body

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def rebuild(self):
        with self.builder._lock:
            self.builder._state = self.builder.build()

    def seed_toast(self, block):
        self.config_path.write_text(json.dumps({"quietHours": None, "toast": block}),
                                    encoding="utf-8")

    # -- round trip

    def test_the_optional_key_round_trips(self):
        status, _ = self.config_post({"toast": self.WITH_APPROVAL})
        self.assertEqual(status, 204)
        self.assertEqual(self.read_config()["toast"], self.WITH_APPROVAL)

    def test_the_notifiers_shipped_default_of_20_is_accepted(self):
        """20 is BELOW CONFIG_TOAST_MIN_SEC, which is why this key could not reuse the
        waiting-toast bounds: reusing them would 400 the value the notifier ships with."""
        self.assertLess(20, crabd.CONFIG_TOAST_MIN_SEC)
        self.assertEqual(self.config_post(
            {"toast": {"thresholdSec": 120, "enabled": True,
                       "approvalThresholdSec": 20}})[0], 204)
        self.assertEqual(self.read_config()["toast"]["approvalThresholdSec"], 20)

    # -- THE FIX: a save that does not mention it must not erase it

    def test_a_widget_style_save_preserves_a_hand_edited_value(self):
        """The exact reported sequence: the operator hand-edits the key, then changes a
        toast setting from the panel. The panel sends {thresholdSec, enabled}."""
        self.seed_toast({"thresholdSec": 120, "enabled": True,
                         "approvalThresholdSec": 45})
        status, _ = self.config_post({"toast": {"thresholdSec": 300, "enabled": False}})
        self.assertEqual(status, 204)
        toast = self.read_config()["toast"]
        self.assertEqual(toast["thresholdSec"], 300)     # the save landed
        self.assertFalse(toast["enabled"])
        self.assertEqual(toast["approvalThresholdSec"], 45)   # ...and kept the edit

    def test_preservation_survives_repeated_saves(self):
        """Once is luck. A value that survives one save and dies on the third is the same
        defect with a longer fuse."""
        self.seed_toast({"thresholdSec": 120, "enabled": True,
                         "approvalThresholdSec": 45})
        for seconds in (200, 300, 400):
            self.assertEqual(self.config_post(
                {"toast": {"thresholdSec": seconds, "enabled": True}})[0], 204)
            self.assertEqual(self.read_config()["toast"]["approvalThresholdSec"], 45)

    def test_an_explicit_value_overrides_the_preserved_one(self):
        """Preservation is for SILENCE, never an override - otherwise the key becomes
        unchangeable once set."""
        self.seed_toast({"thresholdSec": 120, "enabled": True,
                         "approvalThresholdSec": 45})
        self.config_post({"toast": {"thresholdSec": 120, "enabled": True,
                                    "approvalThresholdSec": 90}})
        self.assertEqual(self.read_config()["toast"]["approvalThresholdSec"], 90)

    def test_nothing_is_invented_when_the_disk_never_had_one(self):
        """A plain {thresholdSec, enabled} write against a config with no approval key
        writes no approval key. Preservation must not become a default."""
        self.assertEqual(self.config_post({"toast": self.GOOD})[0], 204)
        self.assertNotIn("approvalThresholdSec", self.read_config()["toast"])

    def test_a_non_dict_toast_on_disk_does_not_break_the_write(self):
        """Hand-edited JSON. A `toast` that is a string, a list or null must not make the
        preservation step throw on the way to a perfectly valid write."""
        for junk in ("on", [1, 2], None, 7):
            self.config_path.write_text(json.dumps({"toast": junk}), encoding="utf-8")
            self.assertEqual(self.config_post({"toast": self.GOOD})[0], 204, junk)
            self.assertEqual(self.read_config()["toast"], self.GOOD, junk)

    # -- bounds and types

    def test_the_approval_range_is_inclusive_at_both_ends(self):
        for seconds in (crabd.CONFIG_APPROVAL_TOAST_MIN_SEC,
                        crabd.CONFIG_APPROVAL_TOAST_MAX_SEC):
            status, _ = self.config_post(
                {"toast": {"thresholdSec": 120, "enabled": True,
                           "approvalThresholdSec": seconds}})
            self.assertEqual(status, 204, seconds)
            self.assertEqual(self.read_config()["toast"]["approvalThresholdSec"],
                             seconds)

    def test_an_out_of_bounds_or_mistyped_approval_is_400_and_writes_nothing(self):
        before = json.dumps({"quietHours": None, "allowReply": False})
        for approval in (crabd.CONFIG_APPROVAL_TOAST_MIN_SEC - 1,
                         crabd.CONFIG_APPROVAL_TOAST_MAX_SEC + 1,
                         0, -20, "20", 20.5, None, True, False, [20], {"sec": 20}):
            self.config_path.write_text(before, encoding="utf-8")
            status, body = self.config_post(
                {"toast": {"thresholdSec": 120, "enabled": True,
                           "approvalThresholdSec": approval}})
            self.assertEqual(status, 400, approval)
            self.assertIn("error", json.loads(body))
            self.assertEqual(self.config_path.read_text(encoding="utf-8"), before,
                             approval)

    def test_the_two_required_members_are_still_required_alongside_it(self):
        """Optional means optional for ITSELF only. A block carrying just the approval
        key is still the partial block the contract refuses."""
        for value in ({"approvalThresholdSec": 20},
                      {"thresholdSec": 120, "approvalThresholdSec": 20},
                      {"enabled": True, "approvalThresholdSec": 20}):
            self.assertEqual(self.config_post({"toast": value})[0], 400, value)

    def test_a_fourth_member_is_still_rejected(self):
        """The whitelist grew by exactly one key, not into an ignore-what-you-don't-know
        parser."""
        for value in ({"thresholdSec": 120, "enabled": True, "extra": 1},
                      {"thresholdSec": 120, "enabled": True,
                       "approvalThresholdSec": 20, "extra": 1},
                      {"thresholdSec": 120, "enabled": True,
                       "ApprovalThresholdSec": 20}):
            self.assertEqual(self.config_post({"toast": value})[0], 400, value)

    def test_a_rejected_write_preserves_nothing_because_it_writes_nothing(self):
        """The preservation step runs inside the WRITE, so a 400 must leave the file
        byte-identical - including the key it would have carried over."""
        self.seed_toast({"thresholdSec": 120, "enabled": True,
                         "approvalThresholdSec": 45})
        before = self.config_path.read_text(encoding="utf-8")
        self.assertEqual(self.config_post(
            {"toast": {"thresholdSec": 1, "enabled": True}})[0], 400)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    # -- everything that already worked still does

    def test_a_plain_two_member_write_is_unchanged(self):
        status, _ = self.config_post({"toast": self.GOOD})
        self.assertEqual(status, 204)
        self.assertEqual(self.read_config()["toast"], self.GOOD)

    def test_preservation_does_not_leak_into_the_other_writable_keys(self):
        """Only `toast` has a preserved sub-key. A digest or budget block is still
        replaced whole, which is what their contracts say."""
        self.assertEqual(crabd.UserConfig.PRESERVED_SUBKEYS,
                         {"toast": ("approvalThresholdSec",)})
        self.config_path.write_text(json.dumps(
            {"digest": {"enabled": True, "time": "07:00", "approvalThresholdSec": 45}}),
            encoding="utf-8")
        self.assertEqual(self.config_post(
            {"digest": {"enabled": False, "time": "08:00"}})[0], 204)
        self.assertEqual(self.read_config()["digest"],
                         {"enabled": False, "time": "08:00"})

    def test_the_new_member_reaches_the_served_document(self):
        """SUPERSEDED v0.18.0. This test used to assert `toast` was ABSENT from
        /v1/state ("notifier config read from the FILE, not part of the contract"). It is
        inverted deliberately: /v1/config is POST-only, so the feed is the only channel
        by which the widget's settings sheet can ever DISPLAY a hand-edited value. The
        served block is an ECHO of the file - it changes nothing about who writes it."""
        self.config_post({"toast": self.WITH_APPROVAL})
        self.rebuild()
        self.assertEqual(self.state()["toast"], self.WITH_APPROVAL)


class ServedToastBlockTests(ServedOverASocket):
    """The top-level `toast` block on /v1/state (v0.18.0) - the toast settings echoed so
    the widget's settings sheet can SHOW what the notifier is running on.

    The whole design question is the third member. `thresholdSec` and `enabled` are
    required members of the config block, so their absence means "the notifier is on its
    shipped defaults" - a fact, servable. `approvalThresholdSec` is OPTIONAL, and an
    unset key must stay unset: v0.16.0's preserve-on-omit exists precisely because a
    round trip that materialized this key erased the operator's hand edit. A feed that
    answered 20 for an unset key would hand the widget a value to latch and write back,
    reintroducing that defect from the other end.
    """

    def rebuild(self):
        with self.builder._lock:
            self.builder._state = self.builder.build()

    def write_config(self, data):
        self.config_path.write_text(json.dumps(data), encoding="utf-8")
        self.builder.config = crabd.UserConfig(self.config_path)
        self.rebuild()

    def served(self):
        return self.state()["toast"]

    # -- present with defaults

    def test_the_block_is_present_with_the_notifiers_defaults_when_unconfigured(self):
        """No `toast` in config.json. The block is still served, because 120/true is
        what the notifier will use - not an unknown."""
        self.write_config({"quietHours": None})
        self.assertEqual(self.served(), {"thresholdSec": 120, "enabled": True})
        self.assertEqual(crabd.CONFIG_TOAST_DEFAULT_SEC, 120)
        self.assertIs(crabd.CONFIG_TOAST_DEFAULT_ENABLED, True)

    def test_the_defaults_match_what_the_notifier_actually_ships(self):
        """The one thing that makes serving a default honest rather than a guess. If the
        notifier's fallbacks move and these do not, the panel displays a number no
        process is using."""
        source = (Path(__file__).resolve().parents[2] / "notifier"
                  / "sidecrab_toast.py").read_text(encoding="utf-8")
        self.assertIn(f"DEFAULT_THRESHOLD_SEC = {crabd.CONFIG_TOAST_DEFAULT_SEC}", source)
        self.assertIn("enabled: bool = True", source)

    def test_a_junk_block_falls_back_rather_than_serving_junk(self):
        """config.json is hand-edited. The contract types are int and bool; a string or a
        null there is not a setting, and the notifier ignores it too."""
        for junk in ("on", [1, 2], None, 7, {"thresholdSec": "300", "enabled": "yes"},
                     {"thresholdSec": -1, "enabled": None},
                     {"thresholdSec": True, "enabled": 1}):
            self.write_config({"toast": junk})
            self.assertEqual(self.served(), {"thresholdSec": 120, "enabled": True}, junk)

    # -- present with the key set

    def test_a_set_approval_threshold_is_echoed(self):
        self.write_config({"toast": {"thresholdSec": 300, "enabled": False,
                                     "approvalThresholdSec": 45}})
        self.assertEqual(self.served(), {"thresholdSec": 300, "enabled": False,
                                         "approvalThresholdSec": 45})

    def test_a_hand_edited_value_outside_the_endpoint_bounds_is_still_shown(self):
        """The feed answers "what will the notifier use", not "what would /v1/config have
        accepted". The notifier honours any non-negative seconds, so a hand-edited 10 is
        live and the panel must not display 120 while the box behaves like 10."""
        self.assertLess(10, crabd.CONFIG_TOAST_MIN_SEC)
        self.write_config({"toast": {"thresholdSec": 10, "enabled": True,
                                     "approvalThresholdSec": 2}})
        self.assertEqual(self.served(), {"thresholdSec": 10, "enabled": True,
                                         "approvalThresholdSec": 2})

    # -- absent key omitted, and never invented

    def test_an_unset_approval_threshold_is_omitted_not_defaulted(self):
        """THE load-bearing assertion. 20 is the notifier's fallback; the feed must not
        claim it."""
        self.write_config({"toast": {"thresholdSec": 300, "enabled": True}})
        self.assertEqual(self.served(), {"thresholdSec": 300, "enabled": True})
        # Not just "the key is absent": no member anywhere in the block carries the
        # notifier's 20 s fallback, so nothing the widget reads can be mistaken for it.
        self.assertNotIn(20, self.served().values())

    def test_an_unusable_approval_threshold_is_omitted_too(self):
        """A string or a negative is not the operator's value either, and inventing a
        default for it is the same lie."""
        for junk in ("20", None, True, -5, [20], {"sec": 20}):
            self.write_config({"toast": {"thresholdSec": 120, "enabled": True,
                                         "approvalThresholdSec": junk}})
            self.assertNotIn("approvalThresholdSec", self.served(), junk)

    def test_no_toast_block_at_all_still_omits_the_optional_member(self):
        self.write_config({})
        self.assertNotIn("approvalThresholdSec", self.served())

    # -- it tracks a write

    def test_the_next_state_reflects_a_config_post(self):
        """The contract's "the NEXT /v1/state reflects the write". Over the socket, both
        ways, with no cache flush of our own: the panel changes a setting and reads its
        own change back."""
        self.write_config({"toast": {"thresholdSec": 120, "enabled": True,
                                     "approvalThresholdSec": 45}})
        self.assertEqual(self.served()["thresholdSec"], 120)
        reply = self.client.post("/v1/config", json.dumps(
            {"toast": {"thresholdSec": 300, "enabled": False}}).encode())
        self.assertEqual(reply.status, 204)
        self.rebuild()
        # The write landed AND the preserved hand edit is still what the panel reads -
        # the two halves of v0.16.0, now visible on the feed for the first time.
        self.assertEqual(self.served(), {"thresholdSec": 300, "enabled": False,
                                         "approvalThresholdSec": 45})

    def test_a_write_that_never_had_the_key_still_serves_no_default(self):
        self.write_config({"quietHours": None})
        self.assertEqual(self.client.post("/v1/config", json.dumps(
            {"toast": {"thresholdSec": 300, "enabled": True}}).encode()).status, 204)
        self.rebuild()
        self.assertEqual(self.served(), {"thresholdSec": 300, "enabled": True})

    # -- the pure function, off the socket

    def test_toast_block_is_a_pure_function_of_the_config_dict(self):
        self.assertEqual(crabd.toast_block(None),
                         {"thresholdSec": 120, "enabled": True})
        self.assertEqual(crabd.toast_block({}),
                         {"thresholdSec": 120, "enabled": True})
        self.assertEqual(
            crabd.toast_block({"toast": {"thresholdSec": 45.9, "enabled": False}}),
            {"thresholdSec": 45, "enabled": False})


# --------------------------------------------- v0.8.0: GET /v1/history?day=YYYY-MM-DD

class HistoryEndpointTests(ServedOverASocket):
    """The whole endpoint over a REAL socket: the contract's shape, the day filter, the
    cap, and every way a `day` can be wrong.

    The builder the socket fixture makes has no HistoryLog, so one is attached here on an
    explicit temp path (the module already redirects crabd.HISTORY_FILE, so a slip lands
    in the sandbox rather than in ~/.sidecrab)."""

    SID_H = "55555555-0000-0000-0000-000000000011"

    def setUp(self):
        super().setUp()
        self.history_path = Path(self._tmp.name) / "history.jsonl"
        self.log = crabd.HistoryLog(self.history_path)
        self.builder.history = self.log

    # ---- helpers

    def history(self, query="", path="/v1/history"):
        """(status, parsed body). `query` is the RAW query string so a test can send a
        missing, blank or duplicated `day` - which a dict-based helper cannot."""
        reply = self.client.get(path + ("?" + query if query else ""))
        return reply.status, reply.json()

    def today(self):
        return crabd._local_day(time.time())

    def write_raw(self, path, lines):
        path.write_text("".join(lines), encoding="utf-8")

    def line(self, ts, kind="turn finished", sid=None, title="fix the collector"):
        return json.dumps({"ts": round(ts, 3), "kind": kind,
                           "sessionId": sid or self.SID_H, "title": title}) + "\n"

    # ---- the happy path

    def test_a_day_with_events_carries_exactly_the_contract_shape(self):
        now = time.time()
        self.log.append(now - 30, "turn finished", self.SID_H, "the crabd lane")
        status, body = self.history(f"day={self.today()}")
        self.assertEqual(status, 200)
        self.assertEqual(sorted(body), ["count", "day", "events", "truncated"])
        self.assertEqual(body["day"], self.today())
        self.assertEqual(body["count"], 1)
        self.assertFalse(body["truncated"])
        event, = body["events"]
        self.assertEqual(sorted(event), ["kind", "sessionId", "title", "ts"])
        self.assertEqual(event["kind"], "turn finished")
        self.assertEqual(event["sessionId"], self.SID_H)
        self.assertEqual(event["title"], "the crabd lane")
        self.assertEqual(event["ts"], crabd._utc_iso(now - 30))

    def test_events_from_both_generations_are_merged_newest_first(self):
        """`.old` is the older half by construction (rotation renamed it), and the day a
        rotation lands inside is exactly the day that would otherwise lose half itself."""
        now = time.time()
        self.write_raw(self.log.old_path,
                       [self.line(now - 300, "session started"),
                        self.line(now - 240, "prompt submitted")])
        self.write_raw(self.history_path,
                       [self.line(now - 120, "asked a question"),
                        self.line(now - 60, "turn finished")])
        status, body = self.history(f"day={self.today()}")
        self.assertEqual(status, 200)
        self.assertEqual([e["kind"] for e in body["events"]],
                         ["turn finished", "asked a question",
                          "prompt submitted", "session started"])
        self.assertEqual(body["count"], 4)

    def test_the_title_may_be_null(self):
        """A session crabd never learned a title for logs `title: null` rather than a
        guess (HistoryLog's own rule) - the endpoint must carry the null through."""
        self.log.append(time.time(), "session started", self.SID_H, None)
        _, body = self.history(f"day={self.today()}")
        self.assertIsNone(body["events"][0]["title"])

    def test_count_is_the_length_of_what_was_returned(self):
        now = time.time()
        for i in range(5):
            self.log.append(now - i, "turn finished", self.SID_H, "t")
        _, body = self.history(f"day={self.today()}")
        self.assertEqual(body["count"], len(body["events"]))
        self.assertEqual(body["count"], 5)

    # ---- the day filter

    def test_only_that_local_day_comes_back(self):
        now = time.time()
        midnight = crabd._local_midnight(now)
        self.write_raw(self.history_path,
                       [self.line(midnight - 3600, "session started"),      # yesterday
                        self.line(midnight + 60, "prompt submitted"),       # today
                        self.line(midnight - 86400 * 3, "session ended")])  # 3 days back
        _, body = self.history(f"day={self.today()}")
        self.assertEqual([e["kind"] for e in body["events"]], ["prompt submitted"])
        yesterday = crabd._local_day(midnight - 3600)
        _, body = self.history(f"day={yesterday}")
        self.assertEqual([e["kind"] for e in body["events"]], ["session started"])

    def test_the_day_boundary_is_local_midnight_to_the_second(self):
        """Bucketed on the LOCAL day string, the way recap.week's done half is: one second
        either side of midnight is two different days, and an epoch-range filter using
        fixed 86400 arithmetic gets this wrong on the two DST days of the year."""
        midnight = crabd._local_midnight(time.time())
        self.write_raw(self.history_path,
                       [self.line(midnight - 1, "session ended"),
                        self.line(midnight, "session started"),
                        self.line(midnight + 1, "prompt submitted")])
        _, today = self.history(f"day={crabd._local_day(midnight)}")
        self.assertEqual([e["kind"] for e in today["events"]],
                         ["prompt submitted", "session started"])
        _, before = self.history(f"day={crabd._local_day(midnight - 1)}")
        self.assertEqual([e["kind"] for e in before["events"]], ["session ended"])

    def test_a_day_that_is_not_in_the_file_is_an_empty_200(self):
        """Absence of history is not an error - a day the operator did not work is a
        real answer, and a 404 would render on the widget as a broken endpoint."""
        self.log.append(time.time(), "turn finished", self.SID_H, "t")
        status, body = self.history("day=2019-03-14")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"day": "2019-03-14", "events": [],
                                "count": 0, "truncated": False})

    def test_a_missing_history_file_is_an_empty_200(self):
        self.assertFalse(self.history_path.exists())
        status, body = self.history(f"day={self.today()}")
        self.assertEqual(status, 200)
        self.assertEqual(body["events"], [])
        self.assertEqual(body["count"], 0)

    def test_a_builder_with_no_history_log_is_an_empty_200(self):
        """Same answer as a missing file: a crabd that is not persisting history has no
        events for any day, which is not an error condition."""
        self.builder.history = None
        status, body = self.history(f"day={self.today()}")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 0)

    # ---- the cap

    def test_the_cap_is_200_with_truncated_true_beyond_it(self):
        now = time.time()
        lines = [self.line(now - (250 - i), "turn finished", title=f"t{i}")
                 for i in range(250)]
        self.write_raw(self.history_path, lines)
        _, body = self.history(f"day={self.today()}")
        self.assertEqual(len(body["events"]), crabd.HISTORY_DAY_CAP)
        self.assertEqual(body["count"], crabd.HISTORY_DAY_CAP)
        self.assertTrue(body["truncated"])
        # The NEWEST 200, not the first 200 in the file: a truncated day must drop the
        # oldest events, or the widget's day sheet shows a stale morning and no evening.
        self.assertEqual(body["events"][0]["title"], "t249")
        self.assertEqual(body["events"][-1]["title"], "t50")

    def test_exactly_the_cap_is_not_truncated(self):
        now = time.time()
        self.write_raw(self.history_path,
                       [self.line(now - (200 - i), title=f"t{i}") for i in range(200)])
        _, body = self.history(f"day={self.today()}")
        self.assertEqual(body["count"], 200)
        self.assertFalse(body["truncated"])

    # ---- malformed days

    def test_a_date_that_is_not_real_is_400(self):
        """The regex half accepts every one of these; only the strptime half rejects
        them, which is why the contract asks for both."""
        for day in ("2026-02-30", "2026-13-01", "2026-00-10", "2026-01-32",
                    "2026-04-31", "2025-02-29", "0000-01-01"):
            status, body = self.history(f"day={day}")
            self.assertEqual(status, 400, day)
            self.assertIn("error", body)

    def test_a_day_of_the_wrong_shape_is_400(self):
        """And the mirror: strptime alone accepts "2026-2-3", so the regex half is what
        rejects these. The last three are the two gaps a plain ^...$ leaves - a trailing
        newline, and Unicode digits that bare \\d happily matches."""
        for query in ("day=2026-1-1", "day=26-01-01", "day=2026-01-1",
                      "day=2026%2F01%2F01", "day=2026-01-01T00%3A00%3A00Z",
                      "day=yesterday", "day=today", "day=2026-01-01%20",
                      "day=%202026-01-01", "day=2026-01-01%0A",
                      "day=%D9%A2%D9%A0%D9%A2%D9%A6-%D9%A0%D9%A1-%D9%A0%D9%A1"):
            status, body = self.history(query)
            self.assertEqual(status, 400, query)
            self.assertIn("error", body)

    def test_a_missing_or_blank_day_is_400(self):
        """Strict validation is about the PARAMETER's form; the 200-empty rule is about a
        well-formed day with nothing in it. Answering 200-empty here would turn a caller
        that forgot the parameter into a day that merely looks quiet."""
        for query in ("", "day=", "day", "days=2026-01-01", "d=2026-01-01"):
            status, body = self.history(query)
            self.assertEqual(status, 400, query)
            self.assertIn("error", body)

    def test_a_repeated_day_is_400_rather_than_a_silent_pick(self):
        status, _ = self.history("day=2026-01-01&day=2026-01-02")
        self.assertEqual(status, 400)

    def test_an_extra_query_parameter_is_ignored(self):
        self.log.append(time.time(), "turn finished", self.SID_H, "t")
        status, body = self.history(f"day={self.today()}&_=1756240000")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)

    # ---- torn lines and junk

    def test_a_torn_line_is_skipped_and_the_rest_still_serve(self):
        """The append-only file on a desktop that loses power: the last record can be
        half a line or NUL padding. A history endpoint that 500s on it would hand the
        operator a dead day sheet for one truncated byte range."""
        now = time.time()
        self.write_raw(self.history_path,
                       [self.line(now - 120, "session started"),
                        '{"ts": ' + str(now - 90) + ', "kind": "asked a q',   # torn
                        "\n",
                        self.line(now - 60, "turn finished"),
                        "\x00\x00\x00\n",                                    # NUL padding
                        "not json at all\n",
                        self.line(now - 30, "session ended")])
        status, body = self.history(f"day={self.today()}")
        self.assertEqual(status, 200)
        self.assertEqual([e["kind"] for e in body["events"]],
                         ["session ended", "turn finished", "session started"])

    def test_lines_missing_a_required_field_are_skipped(self):
        now = time.time()
        self.write_raw(self.history_path, [
            json.dumps({"kind": "turn finished", "sessionId": "s"}) + "\n",     # no ts
            json.dumps({"ts": now, "sessionId": "s"}) + "\n",                   # no kind
            json.dumps({"ts": now, "kind": "x", "sessionId": ""}) + "\n",       # blank sid
            json.dumps([now, "turn finished", "s"]) + "\n",                     # not an object
            self.line(now, "turn finished")])
        _, body = self.history(f"day={self.today()}")
        self.assertEqual(body["count"], 1)

    # ---- transport

    def test_history_carries_the_same_cors_as_the_other_gets(self):
        """v0.16.0: "the same CORS as the other GETs" now means the REFLECTED origin, not
        the wildcard (SEC-4). The widget's opaque `null` still gets a usable header."""
        reply = self.client.get(f"/v1/history?day={self.today()}",
                                headers={"Origin": "null"})
        self.assertEqual(reply.headers["Access-Control-Allow-Origin"], "null")
        self.assertEqual(reply.headers["Content-Type"], "application/json")

    def test_a_400_also_carries_cors(self):
        """The widget reads the STATUS to decide the day tap is inert; a 400 without CORS
        is unreadable from the iCUE origin and looks like a network failure instead."""
        reply = self.client.get("/v1/history?day=nope", headers={"Origin": "null"})
        self.assertEqual(reply.status, 400)
        self.assertEqual(reply.headers["Access-Control-Allow-Origin"], "null")

    def test_a_trailing_slash_still_routes(self):
        status, _ = self.history(f"day={self.today()}", path="/v1/history/")
        self.assertEqual(status, 200)

    def test_a_neighbouring_path_is_still_404(self):
        for path in ("/v1/histories", "/v1/history/2026-01-01", "/history"):
            status, body = self.history(f"day={self.today()}", path=path)
            self.assertEqual(status, 404, path)
            self.assertEqual(body, {"error": "not found"})

    def test_state_and_health_are_untouched_by_the_new_route(self):
        self.assertIn("schema", self.state())
        self.assertEqual(self.client.get("/v1/health").json()["version"], "0.30.0")

    def test_the_endpoint_does_not_write_to_the_history_file(self):
        """Read-only by contract. A GET that touched the file would also invalidate its
        own cache on every request."""
        self.log.append(time.time(), "turn finished", self.SID_H, "t")
        before = self.history_path.stat()
        for _ in range(3):
            self.history(f"day={self.today()}")
        after = self.history_path.stat()
        self.assertEqual((before.st_size, before.st_mtime),
                         (after.st_size, after.st_mtime))


class HistoryDayIndexTests(HistoryTempFile):
    """HistoryLog.day/day_index directly - the cache and the ordering rules."""

    def today(self):
        return crabd._local_day(time.time())

    def test_the_index_is_cached_until_the_file_changes(self):
        """MEASURED 2026-08-26 with both generations at the 2 MB rotation cap (26,190
        lines): a full warm parse is 42-50 ms, so the request path caches rather than
        re-parses. The cache is keyed on the (mtime, size) pair of BOTH files."""
        log = self.log()
        log.append(time.time(), "turn finished", "s-1", "t")
        first = log.day_index()
        self.assertIs(log.day_index(), first)          # no write: same object, no re-parse
        log.append(time.time(), "session ended", "s-1", "t")
        second = log.day_index()
        self.assertIsNot(second, first)                # the append invalidated it
        self.assertEqual(len(second[self.today()]), 2)

    def test_a_write_to_the_old_generation_also_invalidates(self):
        """Only the current file grows on an append, but a ROTATION rewrites `.old` - and
        a stamp that watched only the current file would serve the rotated-away half
        from a cache built before it existed."""
        log = self.log()
        log.append(time.time(), "turn finished", "s-1", "t")
        first = log.day_index()
        log.old_path.write_text(
            json.dumps({"ts": time.time(), "kind": "session started",
                        "sessionId": "s-0", "title": None}) + "\n", encoding="utf-8")
        self.assertIsNot(log.day_index(), first)
        self.assertEqual(len(log.day_index()[self.today()]), 2)

    def test_same_second_events_come_back_in_reverse_arrival_order(self):
        """The served `ts` is second-granularity, so two events inside one second are
        indistinguishable by it - the index sorts on the epoch it still holds."""
        at = time.time()
        log = self.log()
        for kind in ("session started", "prompt submitted", "turn finished"):
            log.append(at, kind, "s-1", "t")
        events, truncated = log.day(self.today())
        self.assertFalse(truncated)
        self.assertEqual([e["kind"] for e in events],
                         ["turn finished", "prompt submitted", "session started"])

    def test_an_out_of_order_timestamp_still_sorts_newest_first(self):
        """A clock that stepped back writes an older ts after a newer one; replay() has
        carried an explicit sort for that since v0.7.0 and this must not be laxer."""
        now = time.time()
        log = self.log()
        log.append(now - 10, "session started", "s-1", "a")
        log.append(now - 300, "prompt submitted", "s-1", "b")   # clock stepped back
        log.append(now - 5, "turn finished", "s-1", "c")
        events, _ = log.day(self.today())
        self.assertEqual([e["title"] for e in events], ["c", "a", "b"])

    def test_an_unknown_day_is_an_empty_list_not_a_keyerror(self):
        log = self.log()
        log.append(time.time(), "turn finished", "s-1", "t")
        self.assertEqual(log.day("2019-03-14"), ([], False))

    def test_replay_still_works_after_the_parse_refactor(self):
        """_read was split into _slurp + _parse for the index; replay is the v0.7.0
        caller and its contract (oldest first, both generations) is unchanged."""
        log = self.log()
        log.old_path.write_text(
            json.dumps({"ts": 1756240000.0, "kind": "session started",
                        "sessionId": "s-0", "title": "old"}) + "\n", encoding="utf-8")
        log.append(1756240600.0, "turn finished", "s-1", "new")
        self.assertEqual([(kind, title) for _, kind, _, title in log.replay()],
                         [("session started", "old"), ("turn finished", "new")])


# ------------------------------------------------- v0.8.0: the digest config key

class ConfigDigestTests(ServedOverASocket):
    """POST /v1/config's THIRD writable key, over a real socket."""

    GOOD = {"enabled": True, "time": "08:30"}

    def config_post(self, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        reply = self.client.post("/v1/config", data)
        return reply.status, reply.body

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def seed(self):
        self.config_path.write_text(
            json.dumps({"quietHours": None, "allowReply": False}), encoding="utf-8")
        return self.config_path.read_text(encoding="utf-8")

    def test_digest_alone_is_valid(self):
        status, _ = self.config_post({"digest": self.GOOD})
        self.assertEqual(status, 204)
        self.assertEqual(self.read_config()["digest"], self.GOOD)

    def test_the_time_is_normalized_like_quiet_hours(self):
        """One parser for both, so the digest hour and the quiet window cannot disagree
        about what "07:05" means - and the digest is suppressed BY quiet hours."""
        self.config_post({"digest": {"enabled": True, "time": "7:5"}})
        self.assertEqual(self.read_config()["digest"]["time"], "07:05")

    def test_midnight_and_the_last_minute_of_the_day_are_valid(self):
        for value in ("00:00", "23:59"):
            status, _ = self.config_post({"digest": {"enabled": True, "time": value}})
            self.assertEqual(status, 204, value)
            self.assertEqual(self.read_config()["digest"]["time"], value)

    def test_disabled_still_carries_a_time(self):
        """No null-clear, same rule as toast: "no digest" is {"enabled": false}, which
        still says what the hour WOULD be when the operator turns it back on."""
        status, _ = self.config_post({"digest": {"enabled": False, "time": "21:00"}})
        self.assertEqual(status, 204)
        self.assertEqual(self.read_config()["digest"],
                         {"enabled": False, "time": "21:00"})

    def test_an_invalid_digest_is_400_and_writes_nothing(self):
        before = self.seed()
        for value in ({"enabled": True},                          # missing time
                      {"time": "08:30"},                          # missing enabled
                      {"enabled": True, "time": "08:30", "extra": 1},
                      {"enabled": True, "time": "24:00"},
                      {"enabled": True, "time": "08:60"},
                      {"enabled": True, "time": "-1:00"},
                      {"enabled": True, "time": "8"},
                      {"enabled": True, "time": "08:30:00"},
                      {"enabled": True, "time": ""},
                      {"enabled": True, "time": None},
                      {"enabled": True, "time": 830},
                      {"enabled": True, "time": "8:3o"},
                      {"enabled": 1, "time": "08:30"},            # 1 is not a bool
                      {"enabled": "true", "time": "08:30"},
                      {"enabled": None, "time": "08:30"},
                      {"enabled": None, "time": None},
                      {},
                      None,
                      "08:30",
                      True,
                      ["08:30", True]):
            status, body = self.config_post({"digest": value})
            self.assertEqual(status, 400, value)
            self.assertIn("error", json.loads(body))
            self.assertEqual(self.config_path.read_text(encoding="utf-8"), before, value)

    def test_a_digest_write_preserves_every_other_key(self):
        self.config_path.write_text(json.dumps(
            {"quietHours": {"start": "22:00", "end": "07:00"}, "allowReply": True,
             "toast": {"thresholdSec": 120, "enabled": True},
             "recapRepos": ["C:\\Dev\\sidecrab"], "somethingElse": {"deep": [1, 2]}}),
            encoding="utf-8")
        self.assertEqual(self.config_post({"digest": self.GOOD})[0], 204)
        after = self.read_config()
        self.assertEqual(after["quietHours"], {"start": "22:00", "end": "07:00"})
        self.assertTrue(after["allowReply"])
        self.assertEqual(after["toast"], {"thresholdSec": 120, "enabled": True})
        self.assertEqual(after["recapRepos"], ["C:\\Dev\\sidecrab"])
        self.assertEqual(after["somethingElse"], {"deep": [1, 2]})
        self.assertEqual(after["digest"], self.GOOD)

    def test_an_existing_digest_block_is_replaced_whole(self):
        self.config_post({"digest": {"enabled": False, "time": "21:00"}})
        self.config_post({"digest": self.GOOD})
        self.assertEqual(self.read_config()["digest"], self.GOOD)

    def test_a_digest_write_does_not_disturb_the_served_quiet_block(self):
        self.config_post({"quietHours": {"start": "22:00", "end": "07:00"}})
        self.config_post({"digest": self.GOOD})
        with self.builder._lock:
            self.builder._state = self.builder.build()
        quiet = self.state()["quiet"]
        self.assertEqual((quiet["start"], quiet["end"]), ("22:00", "07:00"))


class ConfigWhitelistQuadTests(ServedOverASocket):
    """The whitelist is FOUR keys. panelApprovals was DROPPED by SEC-2 (QA-Audit
    2026-08-27): it is a security flag and must not be settable over the unauthenticated
    loopback API. So every combination of the four must write, an unknown key alongside
    any of them must reject the body WHOLE, and panelApprovals in a /v1/config body is
    now itself an unknown key (the SEC-2 regression guard below)."""

    QUIET = {"start": "22:00", "end": "07:00"}
    TOAST = {"thresholdSec": 120, "enabled": True}
    DIGEST = {"enabled": True, "time": "08:30"}
    BUDGET = {"dailyOutputTokens": 5_000_000}
    APPROVALS = {"enabled": True}

    config_post = ConfigDigestTests.config_post
    read_config = ConfigDigestTests.read_config
    seed = ConfigDigestTests.seed

    KEYS = ("quietHours", "toast", "digest", "budget")

    def all_combinations(self):
        """The 15 non-empty subsets of the four writable keys."""
        keys = (("quietHours", self.QUIET), ("toast", self.TOAST),
                ("digest", self.DIGEST), ("budget", self.BUDGET))
        for mask in range(1, 2 ** len(keys)):
            yield {name: value for i, (name, value) in enumerate(keys) if mask >> i & 1}

    def test_the_whitelist_is_exactly_these_four_keys(self):
        self.assertEqual(set(crabd.Handler.CONFIG_WRITABLE), set(self.KEYS))
        self.assertEqual(len(list(self.all_combinations())), 15)

    def test_every_combination_of_the_three_writes_all_of_its_keys(self):
        for payload in self.all_combinations():
            self.seed()
            status, _ = self.config_post(payload)
            self.assertEqual(status, 204, payload)
            after = self.read_config()
            for key, value in payload.items():
                self.assertEqual(after[key], value, (payload, key))
            # And nothing the body did not name was invented. quietHours is exempt: it
            # is one of UserConfig's own defaults, so it is in the file either way.
            for key in ("toast", "digest", "budget"):
                if key not in payload:
                    self.assertNotIn(key, after, (payload, key))

    def test_one_bad_key_rejects_the_whole_body_whatever_it_rides_with(self):
        """All-or-nothing across all four: a body that half-validates must not leave the
        operator unable to tell which half landed."""
        bad = {"quietHours": {"start": "99:00", "end": "07:00"},
               "toast": {"thresholdSec": 5, "enabled": True},
               "digest": {"enabled": True, "time": "24:00"},
               "budget": {"dailyOutputTokens": 99_999}}
        for payload in self.all_combinations():
            for broken in payload:
                before = self.seed()
                body = dict(payload)
                body[broken] = bad[broken]
                status, _ = self.config_post(body)
                self.assertEqual(status, 400, body)
                self.assertEqual(self.config_path.read_text(encoding="utf-8"),
                                 before, body)

    def test_an_unknown_key_alongside_any_combination_is_400(self):
        for payload in self.all_combinations():
            before = self.seed()
            # panelApprovals and allowContinue ride here now: neither is HTTP-writable
            # (SEC-2 / SEC-3), so a body naming either - alone or beside a valid key - is
            # rejected WHOLE like any unknown key.
            for extra in ({"allowReply": True}, {"recapRepos": ["C:\\Windows"]},
                          {"panelApprovals": self.APPROVALS}, {"allowContinue": False},
                          {"Digest": self.DIGEST}, {"schema": 6}):
                body = dict(payload)
                body.update(extra)
                status, response = self.config_post(body)
                self.assertEqual(status, 400, body)
                self.assertIn("error", json.loads(response))
                self.assertEqual(self.config_path.read_text(encoding="utf-8"),
                                 before, body)

    def test_panel_approvals_over_http_is_rejected_and_writes_nothing(self):
        """SEC-2 regression guard. The whole vector was a /v1/config POST flipping
        panelApprovals on; it is now an unknown key - 400, file untouched."""
        before = self.seed()
        status, body = self.config_post({"panelApprovals": self.APPROVALS})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_all_four_in_one_body_is_a_single_write(self):
        self.seed()
        status, _ = self.config_post({"quietHours": self.QUIET, "toast": self.TOAST,
                                      "digest": self.DIGEST, "budget": self.BUDGET})
        self.assertEqual(status, 204)
        after = self.read_config()
        self.assertEqual((after["quietHours"], after["toast"], after["digest"],
                          after["budget"]),
                         (self.QUIET, self.TOAST, self.DIGEST, self.BUDGET))
        self.assertFalse(after["allowReply"])
        self.assertNotIn("panelApprovals", after)


# ------------------------------------------------------------ burn.budget (v0.10.0)

class BudgetBlockTests(unittest.TestCase):
    """`burn.budget` as a pure function of the config block and today's output tokens.

    Contract v0.10.0: emitted ONLY when configured; todayPct = today.outputTokens /
    dailyOutputTokens, 4dp, capped at 9.99.
    """

    GOOD = {"dailyOutputTokens": 5_000_000}

    def block(self, config, output_tokens=0):
        return crabd.budget_block(config, output_tokens)

    def test_no_budget_configured_means_no_block_at_all(self):
        """Absence is the feature detection on both consumers, so an unconfigured
        budget must be a MISSING key - never a zeroed one that reads as "budget 0%"."""
        for config in ({}, {"budget": None}, {"quietHours": None, "allowReply": False},
                       None, [], "5000000", {"budget": "5000000"}, {"budget": []}):
            self.assertIsNone(self.block(config, 1234), config)

    def test_a_configured_budget_emits_exactly_the_contract_shape(self):
        block = self.block({"budget": self.GOOD}, 1_700_000)
        self.assertEqual(sorted(block), ["dailyOutputTokens", "todayPct"])
        self.assertEqual(block["dailyOutputTokens"], 5_000_000)
        self.assertEqual(block["todayPct"], 0.34)

    def test_pct_is_the_ratio_rounded_to_four_places(self):
        for spent, target, expected in ((0, 1_000_000, 0.0),
                                        (500_000, 1_000_000, 0.5),
                                        (1_000_000, 1_000_000, 1.0),
                                        (1_500_000, 1_000_000, 1.5),
                                        (123, 1_000_000, 0.0001),
                                        (1, 1_000_000, 0.0)):
            block = self.block({"budget": {"dailyOutputTokens": target}}, spent)
            self.assertEqual(block["todayPct"], expected, (spent, target))

    def test_pct_never_carries_more_than_four_decimals(self):
        """A raw float ratio serializes as 0.3333333333333333 and the panel has room
        for "33%" - the rounding is what keeps the wire honest about its precision."""
        block = self.block({"budget": {"dailyOutputTokens": 3_000_000}}, 1_000_000)
        self.assertEqual(block["todayPct"], round(1_000_000 / 3_000_000, 4))
        self.assertLessEqual(len(str(block["todayPct"]).split(".")[1]), 4)

    def test_pct_is_capped_at_999_percent(self):
        for spent in (5_000_000, 50_000_000, 100_000_000):
            block = self.block({"budget": {"dailyOutputTokens": 100_000}}, spent)
            self.assertEqual(block["todayPct"], 9.99, spent)
        self.assertEqual(crabd.BUDGET_PCT_CAP, 9.99)

    def test_the_cap_does_not_bite_below_itself(self):
        """A cap that clamped at, say, 9.0 would silently flatten real readings. 998%
        must still read 9.98 - only past the cap does the number stop moving."""
        block = self.block({"budget": {"dailyOutputTokens": 100_000}}, 998_000)
        self.assertEqual(block["todayPct"], 9.98)

    def test_zero_spend_on_a_real_budget_is_zero_not_absent(self):
        block = self.block({"budget": self.GOOD}, 0)
        self.assertEqual(block["todayPct"], 0.0)

    def test_the_range_edges_are_a_budget(self):
        for target in (crabd.CONFIG_BUDGET_MIN, crabd.CONFIG_BUDGET_MAX):
            block = self.block({"budget": {"dailyOutputTokens": target}}, 0)
            self.assertEqual(block["dailyOutputTokens"], target)
        self.assertEqual((crabd.CONFIG_BUDGET_MIN, crabd.CONFIG_BUDGET_MAX),
                         (100_000, 100_000_000))

    def test_a_hand_edited_target_outside_the_range_is_not_served(self):
        """config.json is hand-editable, so the served block has to apply the same
        bounds the endpoint does - otherwise a value POST /v1/config refuses could be
        typed into the file and served anyway. 0 is also the divide-by-zero guard."""
        for target in (0, -1, -5_000_000, 1, 99_999, 100_000_001, 10 ** 12):
            self.assertIsNone(self.block({"budget": {"dailyOutputTokens": target}}, 1),
                              target)

    def test_a_bool_is_not_a_budget(self):
        """bool subclasses int, so `true` must not become a budget of one token.

        What REJECTS it here is the range, not the isinstance guard - True is 1 and
        False is 0, both far under the 100k floor - so this test pins the OUTCOME and
        deliberately does not claim to cover that line. Mutation-checked 2026-08-26:
        deleting the isinstance guard leaves this suite green, which is why crabd.py
        says so at the guard rather than letting a reader assume otherwise.
        """
        for target in (True, False):
            self.assertIsNone(self.block({"budget": {"dailyOutputTokens": target}}, 1),
                              target)

    def test_a_non_int_target_is_not_a_budget(self):
        for target in (5_000_000.0, "5000000", None, [5_000_000], {"n": 1}):
            self.assertIsNone(self.block({"budget": {"dailyOutputTokens": target}}, 1),
                              target)

    def test_an_extra_or_missing_member_is_not_a_budget(self):
        for value in ({}, {"dailyOutputTokens": 5_000_000, "todayPct": 0.3},
                      {"dailyoutputtokens": 5_000_000}, {"daily": 5_000_000}):
            self.assertIsNone(self.block({"budget": value}, 1), value)

    def test_the_endpoint_and_the_served_block_share_one_parser(self):
        """The two halves must agree on what a budget IS. Anything the validator
        accepts (other than the null clear) must produce a block, and anything it
        rejects must produce none - one parser is how that stays true."""
        candidates = [self.GOOD, {"dailyOutputTokens": 100_000},
                      {"dailyOutputTokens": 100_000_000},
                      {"dailyOutputTokens": 99_999}, {"dailyOutputTokens": True},
                      {"dailyOutputTokens": 1.0}, {"dailyOutputTokens": "5000000"},
                      {}, {"dailyOutputTokens": 5_000_000, "x": 1}, [], "x", 5_000_000]
        for value in candidates:
            accepted = crabd.Handler._validate_budget(value)[0]
            served = self.block({"budget": value}, 1) is not None
            self.assertEqual(accepted, served, value)
        # The one deliberate asymmetry: null is a valid WRITE and serves nothing.
        self.assertTrue(crabd.Handler._validate_budget(None)[0])
        self.assertIsNone(self.block({"budget": None}, 1))


class ConfigBudgetTests(ServedOverASocket):
    """POST /v1/config's FOURTH writable key, and the burn.budget it lights up, over a
    real socket."""

    GOOD = {"dailyOutputTokens": 5_000_000}

    config_post = ConfigDigestTests.config_post
    read_config = ConfigDigestTests.read_config
    seed = ConfigDigestTests.seed

    def rebuild(self):
        with self.builder._lock:
            self.builder._state = self.builder.build()
        return self.state()

    def test_budget_alone_is_valid(self):
        status, _ = self.config_post({"budget": self.GOOD})
        self.assertEqual(status, 204)
        self.assertEqual(self.read_config()["budget"], self.GOOD)

    def test_the_range_edges_are_accepted(self):
        for target in (100_000, 100_000_000):
            status, _ = self.config_post({"budget": {"dailyOutputTokens": target}})
            self.assertEqual(status, 204, target)
            self.assertEqual(self.read_config()["budget"]["dailyOutputTokens"], target)

    def test_one_token_outside_either_edge_is_400_and_writes_nothing(self):
        before = self.seed()
        for target in (99_999, 100_000_001):
            status, body = self.config_post({"budget": {"dailyOutputTokens": target}})
            self.assertEqual(status, 400, target)
            self.assertIn("error", json.loads(body))
            self.assertEqual(self.config_path.read_text(encoding="utf-8"), before, target)

    def test_an_invalid_budget_is_400_and_writes_nothing(self):
        before = self.seed()
        for value in ({},                                        # missing the member
                      {"dailyOutputTokens": 5_000_000, "extra": 1},
                      {"DailyOutputTokens": 5_000_000},
                      {"dailyOutputTokens": True},               # bool is not a budget
                      {"dailyOutputTokens": False},
                      {"dailyOutputTokens": 5_000_000.0},        # nor is a float
                      {"dailyOutputTokens": "5000000"},
                      {"dailyOutputTokens": None},
                      {"dailyOutputTokens": 0},
                      {"dailyOutputTokens": -5_000_000},
                      {"dailyOutputTokens": [5_000_000]},
                      5_000_000,
                      "5000000",
                      True,
                      [5_000_000]):
            status, body = self.config_post({"budget": value})
            self.assertEqual(status, 400, value)
            self.assertIn("error", json.loads(body))
            self.assertEqual(self.config_path.read_text(encoding="utf-8"), before, value)

    def test_null_clears_the_budget(self):
        """Unlike toast and digest, a budget has no `enabled` member to switch off, so
        removal has to be expressible - and it drops the served block, not zeroes it."""
        self.assertEqual(self.config_post({"budget": self.GOOD})[0], 204)
        self.assertIn("budget", self.rebuild()["burn"])
        status, _ = self.config_post({"budget": None})
        self.assertEqual(status, 204)
        self.assertIsNone(self.read_config()["budget"])
        self.assertNotIn("budget", self.rebuild()["burn"])

    def test_the_block_is_absent_until_a_budget_is_configured(self):
        burn = self.state()["burn"]
        self.assertEqual(sorted(burn),
                         ["byModel", "costSource", "costUSD", "daily", "hourly", "today"])

    def test_the_block_reaches_the_served_document_with_a_real_pct(self):
        self.assertEqual(self.config_post({"budget": {"dailyOutputTokens": 100_000}})[0],
                         204)
        burn = self.rebuild()["burn"]
        spent = burn["today"]["outputTokens"]
        # A zero numerator would make the assertion below true for a broken pct too.
        self.assertGreater(spent, 0)
        self.assertEqual(burn["budget"],
                         {"dailyOutputTokens": 100_000,
                          "todayPct": round(spent / 100_000, 4)})

    def test_the_pct_moves_when_the_target_moves(self):
        """Two writes, one unchanged day: a served pct that ignored the configured
        target - or read it from a default - would pass a single-value test and fail
        here. Both readings are deliberately mid-range, so neither is a rounded 0.0
        nor a capped 9.99, and the ratio between them has to come out exactly 2."""
        now = time.time()
        write_jsonl(self.session_path("cccccccc-0000-0000-0000-00000000000c"),
                    [user_line("spend", now - 120),
                     assistant_line("req_mid", now - 60, output=30_000_000)],
                    mtime=now - 10)
        self.config_post({"budget": {"dailyOutputTokens": 100_000_000}})
        burn = self.rebuild()["burn"]
        spent = burn["today"]["outputTokens"]
        lenient = burn["budget"]["todayPct"]
        self.config_post({"budget": {"dailyOutputTokens": 50_000_000}})
        tight = self.rebuild()["burn"]["budget"]["todayPct"]
        self.assertEqual(lenient, round(spent / 100_000_000, 4))
        self.assertEqual(tight, round(spent / 50_000_000, 4))
        self.assertEqual(round(tight / lenient, 6), 2.0)
        for pct in (lenient, tight):
            self.assertLess(0.0, pct)
            self.assertLess(pct, crabd.BUDGET_PCT_CAP)

    def test_a_ruinous_day_caps_at_999_percent_end_to_end(self):
        now = time.time()
        write_jsonl(self.session_path("bbbbbbbb-0000-0000-0000-00000000000b"),
                    [user_line("burn it", now - 120),
                     assistant_line("req_big", now - 60, output=10 ** 9)],
                    mtime=now - 10)
        self.config_post({"budget": {"dailyOutputTokens": 100_000}})
        burn = self.rebuild()["burn"]
        self.assertGreaterEqual(burn["today"]["outputTokens"], 10 ** 9)
        self.assertEqual(burn["budget"]["todayPct"], 9.99)

    def test_a_budget_write_preserves_every_other_key(self):
        self.config_path.write_text(json.dumps(
            {"quietHours": {"start": "22:00", "end": "07:00"}, "allowReply": True,
             "toast": {"thresholdSec": 120, "enabled": True},
             "digest": {"enabled": True, "time": "08:30"},
             "recapRepos": ["C:\\Dev\\sidecrab"]}), encoding="utf-8")
        self.assertEqual(self.config_post({"budget": self.GOOD})[0], 204)
        after = self.read_config()
        self.assertEqual(after["quietHours"], {"start": "22:00", "end": "07:00"})
        self.assertTrue(after["allowReply"])
        self.assertEqual(after["toast"], {"thresholdSec": 120, "enabled": True})
        self.assertEqual(after["digest"], {"enabled": True, "time": "08:30"})
        self.assertEqual(after["recapRepos"], ["C:\\Dev\\sidecrab"])
        self.assertEqual(after["budget"], self.GOOD)

    def test_an_existing_budget_is_replaced_whole(self):
        self.config_post({"budget": {"dailyOutputTokens": 100_000}})
        self.config_post({"budget": self.GOOD})
        self.assertEqual(self.read_config()["budget"], self.GOOD)

    def test_a_budget_write_does_not_disturb_the_served_quiet_block(self):
        self.config_post({"quietHours": {"start": "22:00", "end": "07:00"}})
        self.config_post({"budget": self.GOOD})
        quiet = self.rebuild()["quiet"]
        self.assertEqual((quiet["start"], quiet["end"]), ("22:00", "07:00"))

    def test_the_rest_of_the_burn_document_is_untouched(self):
        self.config_post({"budget": self.GOOD})
        burn = self.rebuild()["burn"]
        self.assertEqual(sorted(burn),
                         ["budget", "byModel", "costSource", "costUSD", "daily",
                          "hourly", "today"])
        self.assertEqual(len(burn["hourly"]), 24)
        self.assertEqual(len(burn["daily"]), 7)


# =============================================================== v0.12.0: fixtures

def statusline_doc(session_id=None, five=None, seven=None, context=None,
                   rate_limits=None, **extra):
    """A statusline stdin document in the SHIPPED shape.

    Field names and units were read off the Claude Code 2.1.246 binary's own emitter on
    2026-08-26, not off a summary:

        O = { ...P.five_hour && {five_hour: {used_percentage: P.five_hour.utilization*100,
                                             resets_at: P.five_hour.resets_at}}, ... }
        ...(O.five_hour || O.seven_day) && {rate_limits: O}

    so `used_percentage` is a PERCENT and `resets_at` is epoch SECONDS, and `rate_limits`
    is absent entirely rather than empty when neither window exists.
    """
    doc = {"version": "2.1.246", "model": {"id": "claude-fable-5",
                                           "display_name": "Fable"},
           "output_style": {"name": "default"},
           "workspace": {"current_dir": "C:\\Dev\\sidecrab",
                         "project_dir": "C:\\Dev\\sidecrab", "added_dirs": []},
           "cost": {"total_cost_usd": 1.25, "total_duration_ms": 1000,
                    "total_api_duration_ms": 400, "total_lines_added": 3,
                    "total_lines_removed": 1},
           "exceeds_200k_tokens": False, "fast_mode": False,
           "thinking": {"enabled": True}}
    if session_id is not None:
        doc["session_id"] = session_id
        doc["transcript_path"] = f"C:\\Users\\x\\.claude\\projects\\p\\{session_id}.jsonl"
    if context is not None:
        doc["context_window"] = context
    windows = dict(rate_limits or {})
    if five is not None:
        windows["five_hour"] = five
    if seven is not None:
        windows["seven_day"] = seven
    if windows:
        doc["rate_limits"] = windows
    doc.update(extra)
    return doc


def context_window(total_input=549300, size=200000, usage=True, output=120):
    """`context_window`, shaped like the shipped builder's a6e():
        total_input_tokens = input + cache_creation + cache_read
    which is contract v6's contextTokens definition exactly."""
    return {"total_input_tokens": total_input, "total_output_tokens": output,
            "context_window_size": size, "used_percentage": 42.0,
            "remaining_percentage": 58.0,
            "current_usage": {"input_tokens": 12, "cache_read_input_tokens": 549000,
                              "cache_creation_input_tokens": 288,
                              "output_tokens": output} if usage else None}


def otlp_attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        # protobuf-JSON serialises 64-bit ints as STRINGS - the shape a real exporter
        # sends, and the one a naive parser drops.
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": value}}


def otlp_point(value, ts=None, attrs=None):
    point = {"attributes": [otlp_attr(k, v) for k, v in (attrs or {}).items()]}
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        point["asInt"] = value
    else:
        point["asDouble"] = value
    if ts is not None:
        point["timeUnixNano"] = str(int(ts * 1e9))
    return point


def otlp_metrics(points, name=None, temporality=None, kind="sum"):
    return {"resourceMetrics": [{
        "resource": {"attributes": [otlp_attr("service.name", "claude-code")]},
        "scopeMetrics": [{"scope": {"name": "com.anthropic.claude_code"}, "metrics": [{
            "name": name or crabd.OTLP_COST_METRIC,
            "unit": "USD",
            kind: {"aggregationTemporality": (temporality
                                              if temporality is not None
                                              else crabd.OTLP_TEMPORALITY_DELTA),
                   "isMonotonic": True,
                   "dataPoints": points},
        }]}]}]}


def otlp_logs(records):
    return {"resourceLogs": [{
        "resource": {"attributes": [otlp_attr("service.name", "claude-code")]},
        "scopeLogs": [{"scope": {"name": "com.anthropic.claude_code"},
                       "logRecords": records}]}]}


def otlp_error(session_id, status=429, attempt=1, event="api_error",
               error="rate_limit_error: please slow down"):
    attrs = {"event.name": event, "event.timestamp": "2026-08-26T18:00:00Z",
             "session.id": session_id, "model": "claude-fable-5",
             "duration_ms": 900, "attempt": attempt, "error": error}
    if status is not None:
        attrs["status_code"] = status
    return {"timeUnixNano": str(int(time.time() * 1e9)),
            "attributes": [otlp_attr(k, v) for k, v in attrs.items()]}


# ================================================= v0.12.0: status line, unit level

class StatusLineWindowTests(unittest.TestCase):
    """StatusLineReader._window - the percent/fraction trap, in isolation."""

    def test_used_percentage_is_a_percent_and_is_never_sniffed(self):
        """The bug this test exists to prevent: LimitsReader._window has to GUESS the
        scale ("is it > 1?") because the OAuth endpoint has served both 0..1 and 0..100.
        The status line never has - it is always utilization*100 - so a genuine 0.4%
        window must read as 0.004, not as the 40% a sniffing parser would infer."""
        window = crabd.StatusLineReader._window({"used_percentage": 0.4,
                                                 "resets_at": 1787793926})
        self.assertEqual(window["utilization"], 0.004)

    def test_a_whole_percent_maps_to_a_fraction(self):
        self.assertEqual(
            crabd.StatusLineReader._window({"used_percentage": 42.0})["utilization"],
            0.42)

    def test_a_full_window_is_one(self):
        self.assertEqual(
            crabd.StatusLineReader._window({"used_percentage": 100})["utilization"], 1.0)

    def test_out_of_range_percentages_are_clamped_not_served_raw(self):
        for percent, expected in ((120, 1.0), (-5, 0.0)):
            self.assertEqual(
                crabd.StatusLineReader._window({"used_percentage": percent})["utilization"],
                expected, percent)

    def test_resets_at_is_read_as_epoch_seconds(self):
        """The CLI's own consumer does Number.isFinite(x) then Math.min(...)*1000, which
        is only true of seconds. 1787793926 is 2026, not 1970 and not year 58000."""
        window = crabd.StatusLineReader._window({"used_percentage": 10,
                                                 "resets_at": 1787793926})
        self.assertEqual(window["resetsAt"], crabd._utc_iso(1787793926))

    def test_an_absent_reset_is_null_not_the_epoch(self):
        self.assertIsNone(
            crabd.StatusLineReader._window({"used_percentage": 10})["resetsAt"])

    def test_a_non_numeric_percentage_is_not_a_window(self):
        for bad in ({"used_percentage": "42"}, {"used_percentage": None},
                    {"used_percentage": True}, {"utilization": 0.42}, {}, None, []):
            self.assertIsNone(crabd.StatusLineReader._window(bad), bad)


class StatusLineContextTests(unittest.TestCase):
    """StatusLineReader._context_tokens - the same number crabd derives from
    transcripts, taken from the source instead."""

    def test_total_input_tokens_is_the_context_figure(self):
        self.assertEqual(
            crabd.StatusLineReader._context_tokens(context_window(total_input=549300)),
            549300)

    def test_before_the_first_api_call_the_context_is_unknown_not_zero(self):
        """current_usage is null before the first API call and again after a compaction.
        A 0 here would light the widget's ctx chip with a number that is not a
        measurement."""
        self.assertIsNone(crabd.StatusLineReader._context_tokens(
            context_window(total_input=0, usage=False)))

    def test_a_real_zero_with_a_usage_record_is_kept(self):
        self.assertEqual(crabd.StatusLineReader._context_tokens(
            context_window(total_input=0, usage=True)), 0)

    def test_a_missing_block_is_unknown(self):
        for bad in (None, {}, [], {"total_input_tokens": "many"},
                    {"total_input_tokens": True}):
            self.assertIsNone(crabd.StatusLineReader._context_tokens(bad), bad)


class StatusLineIngestTests(unittest.TestCase):
    """StatusLineReader.ingest / limits / context - the source-switch clock."""

    SID = "5b5b5b5b-0000-0000-0000-000000000001"

    def setUp(self):
        self.reader = crabd.StatusLineReader()
        self.now = time.time()

    def doc(self, **kwargs):
        kwargs.setdefault("session_id", self.SID)
        kwargs.setdefault("five", {"used_percentage": 42.0,
                                   "resets_at": int(self.now + 3600)})
        kwargs.setdefault("seven", {"used_percentage": 18.0,
                                    "resets_at": int(self.now + 86400)})
        return statusline_doc(**kwargs)

    def test_a_document_with_windows_becomes_the_limits_block(self):
        self.assertTrue(self.reader.ingest(self.doc(), self.now))
        limits = self.reader.limits(self.now)
        self.assertTrue(limits["available"])
        self.assertIsNone(limits["note"])
        self.assertEqual(limits["fiveHour"]["utilization"], 0.42)
        self.assertEqual(limits["weekly"]["utilization"], 0.18)

    def test_a_document_with_no_rate_limits_keeps_no_limits(self):
        """Absent rate_limits is the NORMAL case (API key / Bedrock / Vertex sessions,
        and every session before its first API response). It must leave the reader with
        nothing to serve so the builder falls back to OAuth - never zeros."""
        doc = statusline_doc(session_id=self.SID, context=context_window())
        self.assertNotIn("rate_limits", doc)
        self.reader.ingest(doc, self.now)
        self.assertIsNone(self.reader.limits(self.now))

    def test_one_window_alone_is_still_a_reading(self):
        self.reader.ingest(self.doc(seven=None), self.now)
        limits = self.reader.limits(self.now)
        self.assertEqual(limits["fiveHour"]["utilization"], 0.42)
        self.assertIsNone(limits["weekly"])

    def test_model_scoped_weeklies_become_extras_sorted_desc(self):
        self.reader.ingest(self.doc(rate_limits={
            "seven_day_opus": {"used_percentage": 12.0},
            "seven_day_sonnet": {"used_percentage": 71.0}}), self.now)
        extra = self.reader.limits(self.now)["extra"]
        self.assertEqual([e["label"] for e in extra],
                         ["sonnet weekly", "opus weekly"])
        self.assertEqual(extra[0]["utilization"], 0.71)

    def test_the_plain_weekly_is_not_duplicated_into_extras(self):
        self.reader.ingest(self.doc(), self.now)
        self.assertEqual(self.reader.limits(self.now)["extra"], [])

    def test_the_plan_name_is_not_invented_from_somewhere_else(self):
        """The status line document carries no subscription type or tier. Borrowing the
        OAuth reading's labels would put one source's name on another source's numbers."""
        self.reader.ingest(self.doc(), self.now)
        limits = self.reader.limits(self.now)
        self.assertIsNone(limits["subscriptionType"])
        self.assertIsNone(limits["rateLimitTier"])

    def test_limits_go_away_after_ten_minutes_of_silence(self):
        """Contract: OAuth is the fallback once no statusline document has arrived in
        10 min. One second before the boundary the reading still stands."""
        self.reader.ingest(self.doc(), self.now)
        self.assertIsNotNone(
            self.reader.limits(self.now + crabd.STATUSLINE_PREFER_SEC - 1))
        self.assertIsNone(
            self.reader.limits(self.now + crabd.STATUSLINE_PREFER_SEC + 1))

    def test_documents_without_windows_do_not_extend_the_reading(self):
        """Silence is measured from the last document that carried WINDOWS. A session
        posting rate_limits-free documents forever must not hold the gauges open on a
        reading nothing is refreshing."""
        self.reader.ingest(self.doc(), self.now)
        later = self.now + crabd.STATUSLINE_PREFER_SEC - 5
        self.reader.ingest(statusline_doc(session_id=self.SID,
                                          context=context_window()), later)
        self.assertIsNone(self.reader.limits(later + 10))

    def test_a_statusline_reading_is_never_served_with_a_stale_caveat(self):
        """The OAuth path qualifies an aging reading ("limits as of 2:41 PM") because it
        can serve one for up to 3 h through an endpoint lockout. This reading cannot get
        that old: it is DROPPED at STATUSLINE_PREFER_SEC (600 s), sooner than
        LIMITS_NOTE_STALE_SEC (900 s), so a caveat branch here could never fire.

        The two constants are asserted in that order deliberately - if anyone widens the
        prefer window past the stale threshold, this fails and says what to add."""
        self.assertLess(crabd.STATUSLINE_PREFER_SEC, crabd.LIMITS_NOTE_STALE_SEC)
        self.reader.ingest(self.doc(), self.now)
        for offset in (0, 60, crabd.STATUSLINE_PREFER_SEC - 1):
            self.assertIsNone(self.reader.limits(self.now + offset)["note"], offset)

    def test_the_served_block_is_a_copy_of_the_stored_reading(self):
        """A caller that wrote into the served dict (the builder stamps `source` onto
        it) must not be editing the reader's own record."""
        self.reader.ingest(self.doc(), self.now)
        served = self.reader.limits(self.now)
        served["source"] = "statusline"
        served["note"] = "scribbled on"
        self.assertNotIn("source", self.reader.limits(self.now))
        self.assertIsNone(self.reader.limits(self.now)["note"])

    def test_context_is_kept_per_session(self):
        self.reader.ingest(self.doc(context=context_window(total_input=549300)),
                           self.now)
        self.assertEqual(self.reader.context(self.SID, self.now), (True, 549300))
        self.assertEqual(self.reader.context("someone-else", self.now), (False, None))

    def test_a_known_session_with_an_unknown_context_is_not_the_same_as_unknown(self):
        """The bool is the whole point: "the status line says this session has no context
        yet" must not fall back to stale transcript arithmetic."""
        self.reader.ingest(self.doc(context=context_window(total_input=0, usage=False)),
                           self.now)
        self.assertEqual(self.reader.context(self.SID, self.now), (True, None))

    def test_context_rows_age_out(self):
        self.reader.ingest(self.doc(context=context_window()), self.now)
        old = self.now + crabd.STATUSLINE_SESSION_KEEP_SEC + 1
        self.assertEqual(self.reader.context(self.SID, old), (False, None))
        self.reader.prune(old)
        self.assertEqual(self.reader._sessions, {})

    def test_junk_is_ingested_without_raising_and_kept_out(self):
        for bad in (None, [], "a string", 42, {"rate_limits": "yes"},
                    {"rate_limits": {"five_hour": "40%"}}, {"session_id": 7}):
            self.assertFalse(self.reader.ingest(bad, self.now), bad)
        self.assertIsNone(self.reader.limits(self.now))


# ============================================ v0.12.0: status line -> served document

class StatusLineSourceTests(TempProjects):
    """limits.source and contextSource on the built document."""

    SID = "5c5c5c5c-0000-0000-0000-000000000002"

    def setUp(self):
        super().setUp()
        self.now = time.time()
        write_jsonl(self.session_path(self.SID),
                    [user_line("do the thing", self.now - 120),
                     assistant_line("req_1", self.now - 60, output=10)],
                    mtime=self.now - 10)
        self.reader = crabd.StatusLineReader()
        self.builder = crabd.StateBuilder(
            crabd.TranscriptStore(self.projects), crabd.HookTracker(), StubLimits(),
            self.now, crabd.UserConfig(self.config_path), statusline=self.reader)

    def feed(self, **kwargs):
        kwargs.setdefault("session_id", self.SID)
        self.reader.ingest(statusline_doc(**kwargs), self.now)

    def row(self, state=None):
        state = state or self.builder.build(now=self.now)
        return next(r for r in state["sessions"] if r["id"] == self.SID)

    def test_without_a_statusline_the_source_is_oauth(self):
        state = self.builder.build(now=self.now)
        self.assertEqual(state["limits"]["source"], "oauth")
        self.assertEqual(state["limits"]["fiveHour"]["utilization"], 0.42)

    def test_a_fed_statusline_wins_and_says_so(self):
        self.feed(five={"used_percentage": 77.0, "resets_at": int(self.now + 60)})
        limits = self.builder.build(now=self.now)["limits"]
        self.assertEqual(limits["source"], "statusline")
        self.assertEqual(limits["fiveHour"]["utilization"], 0.77)

    def test_the_source_switches_back_to_oauth_after_the_silence_window(self):
        """The whole point of the fallback: a status line that stops feeding must not
        freeze the gauges on its last reading forever."""
        self.feed(five={"used_percentage": 77.0})
        later = self.now + crabd.STATUSLINE_PREFER_SEC + 1
        limits = self.builder.build(now=later)["limits"]
        self.assertEqual(limits["source"], "oauth")
        self.assertEqual(limits["fiveHour"]["utilization"], 0.42)   # the stub's number

    def test_an_injected_limits_block_is_still_stamped(self):
        """There is no code path that can serve a limits block without provenance - not
        even the tests', which is how it stays true."""
        state = self.builder.build(now=self.now, limits=dict(MOCK_LIMITS))
        self.assertEqual(state["limits"]["source"], "oauth")

    def test_stamping_does_not_mutate_the_readers_cached_dict(self):
        stub = StubLimits()
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), stub, self.now,
                                     crabd.UserConfig(self.config_path))
        builder.build(now=self.now)
        self.assertNotIn("source", stub.payload)
        self.assertNotIn("source", MOCK_LIMITS)

    def test_context_source_is_transcript_when_nothing_else_speaks(self):
        row = self.row()
        self.assertEqual(row["contextTokens"], 18)      # 2 + 7 + 9 from the fixture
        self.assertEqual(row["contextSource"], "transcript")

    def test_the_statusline_context_wins_for_the_session_it_names(self):
        self.feed(context=context_window(total_input=549300))
        row = self.row()
        self.assertEqual(row["contextTokens"], 549300)
        self.assertEqual(row["contextSource"], "statusline")

    def test_a_statusline_unknown_does_not_fall_back_to_the_transcript(self):
        """After a compaction the status line reports no current usage while the
        transcript still holds the pre-compaction record. Falling back would show a
        window that was emptied minutes ago."""
        self.feed(context=context_window(total_input=0, usage=False))
        row = self.row()
        self.assertIsNone(row["contextTokens"])
        self.assertIsNone(row["contextSource"])

    def test_a_statusline_for_another_session_does_not_touch_this_row(self):
        self.reader.ingest(statusline_doc(session_id="somebody-else",
                                          context=context_window(total_input=999999)),
                           self.now)
        row = self.row()
        self.assertEqual(row["contextTokens"], 18)
        self.assertEqual(row["contextSource"], "transcript")

    def test_a_null_context_carries_no_source_label(self):
        """contextSource is null exactly when contextTokens is: provenance for an absent
        number is worse than none."""
        empty = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                   crabd.HookTracker(), StubLimits(), self.now,
                                   crabd.UserConfig(self.config_path))
        empty.hooks.record({"session_id": "hook-only", "hook_event_name": "SessionStart",
                            "cwd": "C:\\IT"})
        row = next(r for r in empty.build(now=self.now)["sessions"]
                   if r["id"] == "hook-only")
        self.assertIsNone(row["contextTokens"])
        self.assertIsNone(row["contextSource"])


# ============================================================ v0.12.0: OTLP receiver

class OtlpMetricsTests(unittest.TestCase):
    """claude_code.cost.usage -> burn.costUSD. Delta vs cumulative is the trap."""

    def setUp(self):
        self.receiver = crabd.OtlpReceiver()
        self.now = time.time()

    def test_no_telemetry_is_null_and_never_zero(self):
        """An operator with telemetry off must see "unknown". $0.00 on a working day
        would be a number crabd made up."""
        self.assertIsNone(self.receiver.cost_today(self.now))

    def test_delta_points_are_summed(self):
        self.receiver.ingest_metrics(
            otlp_metrics([otlp_point(0.25, self.now), otlp_point(0.5, self.now)]),
            self.now)
        self.receiver.ingest_metrics(otlp_metrics([otlp_point(1.0, self.now)]), self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 1.75)

    def test_a_zero_cost_export_still_switches_the_source_on(self):
        """Having SEEN telemetry is a different fact from having seen spend. Once an
        export lands, 0.0 is a measurement and must not read as "unknown"."""
        self.receiver.ingest_metrics(otlp_metrics([otlp_point(0.0, self.now)]), self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 0.0)

    def test_cumulative_points_take_the_last_value_not_the_sum(self):
        """Delta is Claude Code's default, but a collector in the middle may re-export
        cumulative - and summing a cumulative counter double-counts every export while
        looking entirely plausible."""
        for value in (1.0, 2.5, 4.0):
            self.receiver.ingest_metrics(
                otlp_metrics([otlp_point(value, self.now, {"model": "fable"})],
                             temporality=crabd.OTLP_TEMPORALITY_CUMULATIVE),
                self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 4.0)

    def test_cumulative_series_are_summed_across_attribute_sets(self):
        for model, value in (("fable", 3.0), ("sonnet", 1.5)):
            self.receiver.ingest_metrics(
                otlp_metrics([otlp_point(value, self.now, {"model": model})],
                             temporality=crabd.OTLP_TEMPORALITY_CUMULATIVE),
                self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 4.5)

    def test_an_out_of_order_cumulative_export_does_not_walk_the_total_back(self):
        for value in (5.0, 2.0):
            self.receiver.ingest_metrics(
                otlp_metrics([otlp_point(value, self.now, {"model": "fable"})],
                             temporality=crabd.OTLP_TEMPORALITY_CUMULATIVE),
                self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 5.0)

    def test_yesterdays_spend_is_not_todays(self):
        yesterday = self.now - 26 * 3600
        self.receiver.ingest_metrics(otlp_metrics([otlp_point(9.0, yesterday)]), self.now)
        self.receiver.ingest_metrics(otlp_metrics([otlp_point(1.0, self.now)]), self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 1.0)

    def test_a_point_with_no_timestamp_lands_on_arrival_day(self):
        self.receiver.ingest_metrics(otlp_metrics([otlp_point(2.0)]), self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 2.0)

    def test_a_1970_timestamp_is_treated_as_no_timestamp(self):
        """The same sanity floor the limits cache learned the hard way: an epoch from
        1970 makes every day computation meaningless, so it is not a timestamp."""
        self.receiver.ingest_metrics(otlp_metrics([otlp_point(2.0, 1000.0)]), self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 2.0)

    def test_other_metrics_are_walked_past(self):
        self.receiver.ingest_metrics(
            otlp_metrics([otlp_point(4000)], name="claude_code.token.usage"), self.now)
        self.assertIsNone(self.receiver.cost_today(self.now))

    def test_a_gauge_shaped_export_is_accepted(self):
        self.receiver.ingest_metrics(
            otlp_metrics([otlp_point(0.75, self.now)], kind="gauge"), self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 0.75)

    def test_a_string_encoded_int_is_parsed(self):
        """protobuf-JSON serialises 64-bit ints as strings; a parser that only takes
        numbers drops a real export silently."""
        self.receiver.ingest_metrics(
            otlp_metrics([{"asInt": "3", "timeUnixNano": str(int(self.now * 1e9))}]),
            self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 3.0)

    def test_impossible_values_are_dropped_rather_than_displayed(self):
        for bad in ({"asDouble": -1.0}, {"asDouble": float("inf")},
                    {"asDouble": True}, {"asDouble": None},
                    {"asInt": "not a number"}, {}):
            receiver = crabd.OtlpReceiver()
            receiver.ingest_metrics(otlp_metrics([bad]), self.now)
            self.assertIsNone(receiver.cost_today(self.now), bad)

    def test_malformed_documents_never_raise_and_take_nothing(self):
        for bad in (None, [], "text", 3, {}, {"resourceMetrics": None},
                    {"resourceMetrics": ["not a dict"]},
                    {"resourceMetrics": [{"scopeMetrics": "no"}]},
                    {"resourceMetrics": [{"scopeMetrics": [{"metrics": [None]}]}]},
                    {"resourceMetrics": [{"scopeMetrics": [{"metrics": [
                        {"name": crabd.OTLP_COST_METRIC,
                         "sum": {"dataPoints": "no"}}]}]}]}):
            self.assertEqual(self.receiver.ingest_metrics(bad, self.now), 0, bad)
        self.assertIsNone(self.receiver.cost_today(self.now))

    # ---- F4: the cumulative keyspace is bounded within a day

    def flood(self, n, tag, day_offset=0.0):
        """n cumulative points, each with a DISTINCT attribute set - so each mints its own
        series key, which is the shape the audit describes arriving over the
        unauthenticated POST /v1/metrics."""
        stamp = self.now + day_offset
        self.receiver.ingest_metrics(
            otlp_metrics([otlp_point(1.0, stamp, {tag: str(i)}) for i in range(n)],
                         temporality=crabd.OTLP_TEMPORALITY_CUMULATIVE),
            self.now)

    def test_the_cumulative_series_keyspace_is_bounded(self):
        """AUDIT F4 (fixed v0.17.0). The series key is the point's own attribute set, and
        prune() only drops whole DAYS - so within today the dict grew without bound on
        unauthenticated input. Flood it with fresh keys repeatedly and it must never
        drift up."""
        self.flood(5000, "junk")
        self.assertLessEqual(len(self.receiver._cumulative),
                             crabd.OTLP_MAX_CUMULATIVE_SERIES)
        for batch in range(1, 5):
            self.flood(5000, f"b{batch}")
            self.assertLessEqual(len(self.receiver._cumulative),
                                 crabd.OTLP_MAX_CUMULATIVE_SERIES)

    def test_a_live_series_evicted_by_a_flood_is_restored_by_its_next_export(self):
        """Why eviction is safe HERE and not merely bounded: a cumulative counter carries
        its running total, so the series comes back whole on its very next export. The
        worst case is one interval reading low, never a permanently wrong number."""
        real = otlp_metrics([otlp_point(7.5, self.now, {"model": "fable"})],
                            temporality=crabd.OTLP_TEMPORALITY_CUMULATIVE)
        self.receiver.ingest_metrics(real, self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 7.5)
        self.flood(5000, "junk")                       # the real series may be evicted
        self.receiver.ingest_metrics(real, self.now)   # ...and the next export restores it
        self.assertGreaterEqual(self.receiver.cost_today(self.now), 7.5)

    def test_the_flood_gives_up_yesterdays_keys_before_todays(self):
        """Only one day's bucket is ever served, so keys from the other day are ballast
        nothing can read - they go first, which is what keeps a flood off the live day
        for as long as possible."""
        self.flood(300, "old", day_offset=-26 * 3600)
        yesterday = crabd._local_day(self.now - 26 * 3600)
        self.assertTrue(any(k[0] == yesterday for k in self.receiver._cumulative))
        self.flood(600, "new")
        self.assertLessEqual(len(self.receiver._cumulative),
                             crabd.OTLP_MAX_CUMULATIVE_SERIES)
        self.assertFalse([k for k in self.receiver._cumulative if k[0] == yesterday],
                         "yesterday's ballast should be gone before today's series")

    def test_pruning_drops_days_that_can_never_be_served_again(self):
        old = self.now - 5 * 86400
        self.receiver.ingest_metrics(otlp_metrics([otlp_point(9.0, old)]), self.now)
        self.receiver.ingest_metrics(otlp_metrics([otlp_point(1.0, self.now)]), self.now)
        self.receiver.prune(self.now)
        self.assertEqual(len(self.receiver._delta_by_day), 1)
        self.assertEqual(self.receiver.cost_today(self.now), 1.0)

    # ---- CRB-b: the delta day-bucket keyspace is bounded within a burst

    def delta_flood(self, n, base_offset_days=1):
        """n DELTA points, each on a distinct FUTURE day (future so every stamp stays
        inside _point_time's accepted window and mints its own _delta_by_day key) - the
        shape the audit describes arriving over the unauthenticated POST /v1/metrics with
        forged timeUnixNano."""
        points = [otlp_point(1.0, self.now + (base_offset_days + i) * 86400.0)
                  for i in range(n)]
        self.receiver.ingest_metrics(otlp_metrics(points), self.now)

    def test_the_delta_day_keyspace_is_bounded(self):
        """CRB-b (fixed v0.25.0). F4 hardened only the cumulative sibling; the delta path
        folds into _delta_by_day keyed by day and prune() runs on its own clock, so a
        burst of points with forged distinct days grew the dict without bound."""
        self.delta_flood(5000)
        self.assertLessEqual(len(self.receiver._delta_by_day),
                             crabd.OTLP_MAX_DELTA_DAYS)

    def test_a_delta_flood_never_evicts_todays_bucket(self):
        """Unlike a cumulative series, a delta bucket is a running SUM with no total to
        restore it - eviction is permanent - so today's real spend must survive a flood
        of forged distinct days regardless of which day's point triggered the eviction."""
        self.receiver.ingest_metrics(otlp_metrics([otlp_point(4.0, self.now)]), self.now)
        self.assertEqual(self.receiver.cost_today(self.now), 4.0)
        self.delta_flood(5000)
        self.assertLessEqual(len(self.receiver._delta_by_day),
                             crabd.OTLP_MAX_DELTA_DAYS)
        self.assertEqual(self.receiver.cost_today(self.now), 4.0)


class OtlpLogsTests(unittest.TestCase):
    """api_error events -> the matching session's ring."""

    SID = "6a6a6a6a-0000-0000-0000-000000000003"

    def setUp(self):
        self.seen = []
        self.receiver = crabd.OtlpReceiver(
            on_event=lambda sid, text: (self.seen.append((sid, text)), True)[1])
        self.now = time.time()

    def test_an_api_error_reaches_the_session(self):
        self.receiver.ingest_logs(otlp_logs([otlp_error(self.SID, status=429)]), self.now)
        self.assertEqual(self.seen, [(self.SID, "API error 429")])

    def test_a_retry_count_is_carried_but_a_first_attempt_is_not_noise(self):
        self.receiver.ingest_logs(
            otlp_logs([otlp_error(self.SID, status=500, attempt=3)]), self.now)
        self.assertEqual(self.seen[0][1], "API error 500 (attempt 3)")

    def test_the_vendor_error_string_never_reaches_the_ring(self):
        """The ring is persisted to history.jsonl, whose rule is event kind + session id
        + title + ts. A free-form vendor message is content, and it is not something the
        operator can act on from a 480px panel anyway."""
        self.receiver.ingest_logs(
            otlp_logs([otlp_error(self.SID, error="secret-ish detail")]), self.now)
        self.assertNotIn("secret-ish", self.seen[0][1])

    def test_a_non_http_failure_has_no_status_code_and_still_reports(self):
        self.receiver.ingest_logs(
            otlp_logs([otlp_error(self.SID, status=None)]), self.now)
        self.assertEqual(self.seen[0][1], "API error")

    def test_other_events_are_ignored(self):
        for event in ("api_request", "tool_result", "user_prompt", "api_refusal"):
            self.receiver.ingest_logs(
                otlp_logs([otlp_error(self.SID, event=event)]), self.now)
        self.assertEqual(self.seen, [])

    def test_an_event_with_no_session_id_is_dropped(self):
        record = otlp_error(self.SID)
        record["attributes"] = [a for a in record["attributes"]
                                if a["key"] != "session.id"]
        self.receiver.ingest_logs(otlp_logs([record]), self.now)
        self.assertEqual(self.seen, [])

    def test_one_export_cannot_flood_a_ring(self):
        batch = [otlp_error(self.SID) for _ in range(crabd.OTLP_EVENTS_PER_EXPORT + 25)]
        self.receiver.ingest_logs(otlp_logs(batch), self.now)
        self.assertEqual(len(self.seen), crabd.OTLP_EVENTS_PER_EXPORT)

    def test_a_rejected_event_does_not_count_against_the_cap(self):
        receiver = crabd.OtlpReceiver(on_event=lambda sid, text: False)
        self.assertEqual(
            receiver.ingest_logs(otlp_logs([otlp_error(self.SID)]), self.now), 0)

    def test_malformed_log_documents_never_raise(self):
        for bad in (None, [], "text", {"resourceLogs": None},
                    {"resourceLogs": [{"scopeLogs": [{"logRecords": [None]}]}]},
                    {"resourceLogs": [{"scopeLogs": [{"logRecords": [
                        {"attributes": "no"}]}]}]}):
            self.assertEqual(self.receiver.ingest_logs(bad, self.now), 0, bad)


# ================================================== v0.12.0: the served socket, v12

class V12ServedTests(ServedOverASocket):
    """A real crabd on a test port with all four v0.12.0 readers attached."""

    config_post = ConfigDigestTests.config_post
    read_config = ConfigDigestTests.read_config

    def setUp(self):
        super().setUp()
        self.statusline = crabd.StatusLineReader()
        self.continues = crabd.ContinueQueue()
        self.permissions = crabd.PermissionBroker()
        self.otlp = crabd.OtlpReceiver(
            on_event=lambda sid, text: self.builder.note_session_event(sid, text))
        self.builder.statusline = self.statusline
        self.builder.otlp = self.otlp
        self.builder.continues = self.continues
        self.builder.permissions = self.permissions
        # v0.29.0: every served fixture carries a pairing code, the way a real crabd does.
        self.TOKEN = "K7QXM2PDAB"
        self.panel_token = crabd.PanelToken(None, self.TOKEN)
        self.builder.panel_token = self.panel_token
        self.rebuild()

    def decide_body(self, decision, token=None, request_id=None, session_id=None):
        """The v0.29.0 decide body: sessionId + decision + the pairing code + the
        pending request's id (read straight off the broker unless given)."""
        sid = session_id or self.SID
        pending = self.permissions.pending(sid)
        body = {"sessionId": sid, "action": "decide", "decision": decision,
                "token": self.TOKEN if token is None else token}
        rid = request_id if request_id is not None else (pending or {}).get("requestId")
        if rid is not None:
            body["requestId"] = rid
        return body

    def rebuild(self):
        with self.builder._lock:
            self.builder._state = self.builder.build()
        return self.builder._state

    def post(self, path, data, timeout=10):
        reply = self.client.post(path, data, timeout=timeout)
        return reply.status, reply.body

    def post_json(self, path, payload, timeout=10):
        return self.post(path, json.dumps(payload).encode(), timeout=timeout)

    def row(self):
        return next(r for r in self.state()["sessions"] if r["id"] == self.SID)

    def ring(self, session_id=None):
        return [e["text"] for e in
                self.hooks.snapshot().get(session_id or self.SID, {}).get("events", [])]


class StatusLineEndpointTests(V12ServedTests):
    """POST /v1/statusline."""

    def test_a_document_is_204_and_feeds_the_limits(self):
        status, body = self.post_json("/v1/statusline", statusline_doc(
            session_id=self.SID, five={"used_percentage": 61.0},
            context=context_window(total_input=123456)))
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        settle(lambda: self.statusline.documents, what="the statusline document")
        state = self.rebuild()
        self.assertEqual(state["limits"]["source"], "statusline")
        self.assertEqual(state["limits"]["fiveHour"]["utilization"], 0.61)
        row = self.row()
        self.assertEqual(row["contextTokens"], 123456)
        self.assertEqual(row["contextSource"], "statusline")

    def test_a_malformed_document_is_still_204(self):
        """The status line command is on the operator's own status bar and an in-flight
        one is cancelled by the next update. crabd must never be the thing that makes it
        slow or noisy."""
        for raw in (b"not json", b"", b"[1,2,3]", b'"text"', b"\x00\x01\x02"):
            status, _ = self.post("/v1/statusline", raw)
            self.assertEqual(status, 204, raw)
        self.assertEqual(self.rebuild()["limits"]["source"], "oauth")

    def test_an_oversized_document_is_dropped_not_parsed(self):
        blob = json.dumps(statusline_doc(
            session_id=self.SID, five={"used_percentage": 61.0},
            padding="x" * (crabd.STATUSLINE_MAX_BODY + 1024))).encode()
        self.assertGreater(len(blob), crabd.STATUSLINE_MAX_BODY)
        self.assertEqual(self.post("/v1/statusline", blob)[0], 204)
        self.assertEqual(self.rebuild()["limits"]["source"], "oauth")

    def test_the_endpoint_only_answers_post(self):
        self.assertEqual(self.client.get("/v1/statusline").status, 404)


class OtlpEndpointTests(V12ServedTests):
    """POST /v1/metrics + POST /v1/logs."""

    def test_metrics_are_204_and_reach_the_burn_block(self):
        status, body = self.post_json(
            "/v1/metrics", otlp_metrics([otlp_point(2.5, time.time())]))
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        # The endpoint answers BEFORE it parses (deliberately - the producer is the
        # exporter inside a live session), so the 204 is not the ingest. See
        # _httpkeepalive.settle.
        settle(lambda: self.otlp.exports, what="the cost export")
        burn = self.rebuild()["burn"]
        self.assertEqual(burn["costUSD"], 2.5)
        self.assertEqual(burn["costSource"], "otlp")

    def test_logs_are_204_and_reach_the_session_ring(self):
        self.assertEqual(
            self.post_json("/v1/logs", otlp_logs([otlp_error(self.SID, status=429)]))[0],
            204)
        settle(lambda: "API error 429" in self.ring(), what="the api_error ring entry")

    def test_telemetry_for_a_session_crabd_is_not_serving_is_dropped(self):
        """Scoped to SERVED rows like ack. A stream of 429s from a session that aged out
        an hour ago must not grow the table with entries nothing renders."""
        before = set(self.hooks.snapshot())
        self.post_json("/v1/logs", otlp_logs([otlp_error("ghost-session")]))
        # Wait for the receiver to have LOOKED at the batch, or this passes because it
        # had not got to it yet - a false pass, which is worse than a flake.
        quiesce(lambda: self.otlp.documents, 1)
        self.assertEqual(set(self.hooks.snapshot()), before)

    def test_an_error_event_does_not_move_the_state_machine(self):
        """An api_error happens INSIDE a turn. A receiver that could transition a
        session would let a telemetry batch resurrect a finished one on the panel."""
        self.hooks.record({"session_id": self.SID, "hook_event_name": "Stop",
                           "cwd": "C:\\IT"})
        self.rebuild()
        self.post_json("/v1/logs", otlp_logs([otlp_error(self.SID)]))
        quiesce(lambda: self.otlp.documents, 1)
        self.assertEqual(self.hooks.snapshot()[self.SID]["state"], "done")

    def test_nothing_a_producer_can_send_is_ever_an_error(self):
        """The contract in one test: a telemetry write must never error the producer,
        because that producer is the exporter inside the operator's live session."""
        bodies = [b"not json", b"", b"[]", b'{"resourceMetrics": "wrong"}',
                  b"\n\x0f\x08\x01\x12\x0bclaude_code",       # a protobuf body, misrouted
                  json.dumps({"resourceSpans": [{}]}).encode(),   # traces, unconsumed
                  b"{" + b'"a":1,' * 5000 + b'"b":2}']
        for path in ("/v1/metrics", "/v1/logs"):
            for raw in bodies:
                self.assertEqual(self.post(path, raw)[0], 204, (path, raw[:24]))

    def test_an_oversized_export_is_dropped_but_still_2xx(self):
        blob = b'{"resourceMetrics": [' + b'{"scopeMetrics": []},' * 250000 + b'{}]}'
        self.assertGreater(len(blob), crabd.OTLP_MAX_BODY)
        self.assertEqual(self.post("/v1/metrics", blob)[0], 204)
        self.assertIsNone(self.rebuild()["burn"]["costUSD"])

    def test_the_cost_block_is_null_until_telemetry_flows(self):
        burn = self.state()["burn"]
        self.assertIsNone(burn["costUSD"])
        self.assertIsNone(burn["costSource"])


# ================================================ v0.12.0: continue queue + Stop hook

class ContinueQueueUnitTests(unittest.TestCase):
    """ContinueQueue on its own - newest wins, one shot, ten minutes."""

    SID = "7a7a7a7a-0000-0000-0000-000000000004"

    def setUp(self):
        self.queue = crabd.ContinueQueue()
        self.now = time.time()

    def test_a_queued_prompt_drains_once(self):
        self.queue.queue(self.SID, "Continue", self.now)
        self.assertEqual(self.queue.drain(self.SID, self.now), "Continue")
        self.assertIsNone(self.queue.drain(self.SID, self.now))

    def test_newest_wins(self):
        self.queue.queue(self.SID, "Continue", self.now)
        self.queue.queue(self.SID, "Run the tests", self.now + 1)
        self.assertEqual(self.queue.drain(self.SID, self.now + 2), "Run the tests")

    def test_an_expired_item_is_not_delivered(self):
        self.queue.queue(self.SID, "Continue", self.now)
        self.assertIsNone(
            self.queue.drain(self.SID, self.now + crabd.CONTINUE_TTL_SEC + 1))

    def test_an_expired_item_is_removed_by_the_drain_that_refused_it(self):
        """Otherwise the NEXT Stop, minutes later, would deliver a prompt this drain
        already judged too old."""
        self.queue.queue(self.SID, "Continue", self.now)
        self.queue.drain(self.SID, self.now + crabd.CONTINUE_TTL_SEC + 1)
        self.assertIsNone(self.queue.peek(self.SID, self.now))

    def test_one_second_before_expiry_it_still_lands(self):
        self.queue.queue(self.SID, "Continue", self.now)
        self.assertEqual(
            self.queue.drain(self.SID, self.now + crabd.CONTINUE_TTL_SEC - 1), "Continue")

    def test_queues_are_per_session(self):
        self.queue.queue(self.SID, "Continue", self.now)
        self.assertIsNone(self.queue.drain("another", self.now))
        self.assertEqual(self.queue.drain(self.SID, self.now), "Continue")

    def test_pruning_clears_only_the_expired(self):
        self.queue.queue("old", "Continue", self.now - crabd.CONTINUE_TTL_SEC - 1)
        self.queue.queue("new", "Continue", self.now)
        self.queue.prune(self.now)
        self.assertEqual(self.queue.pending(self.now), 1)
        self.assertEqual(self.queue.drain("new", self.now), "Continue")


class ContinuePromptWhitelistTests(unittest.TestCase):
    """UserConfig.continue_prompts - the set a tap may say."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.json"
        self.config = crabd.UserConfig(self.path)

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.config = crabd.UserConfig(self.path)

    def test_the_whitelist_holds_exactly_what_the_widget_sends(self):
        """The cross-lane defect this test exists to prevent, measured against
        widget/scripts/sidecrab.js CONTINUE_DEFAULTS on 2026-08-26: the widget puts the
        short LABEL on the button face and sends the FULL INSTRUCTION on the wire. A
        whitelist holding only the labels 400s every tap and renders "not available" on
        a feature that shipped working."""
        self.write({})
        allowed = self.config.continue_prompts(time.time())
        for wire in ("Keep going with what you were doing.",
                     "Run the tests and report the results.",
                     "Commit the changes and push."):
            self.assertIn(wire, allowed, wire)
        for label in ("Continue", "Run the tests", "Commit + push"):
            self.assertIn(label, allowed, label)

    def test_the_extras_are_the_config_list_and_not_the_builtins(self):
        """The widget hardcodes the three defaults and APPENDS this list, so emitting
        the builtins here would draw every default button twice."""
        self.write({"continuePrompts": ["ship it", "Continue"]})
        self.assertEqual(self.config.continue_extras(time.time()), ["ship it"])

    def test_config_extras_join_the_whitelist(self):
        self.write({"continuePrompts": ["ship it"]})
        self.assertIn("ship it", self.config.continue_prompts(time.time()))

    def test_a_broken_config_cannot_disarm_the_builtin_buttons(self):
        """The widget is SHOWING those buttons. A config typo must not turn them into
        400s the operator cannot explain."""
        for bad in ("not a list", 42, None, {"a": 1}, [1, 2, None]):
            self.write({"continuePrompts": bad})
            self.assertEqual(self.config.continue_prompts(time.time()),
                             crabd.CONTINUE_PROMPTS_BUILTIN, bad)
            self.assertEqual(self.config.continue_extras(time.time()), [], bad)

    def test_junk_entries_are_dropped_without_taking_the_rest(self):
        self.write({"continuePrompts": [None, "", "   ", 7, "ship it", "ship it"]})
        self.assertEqual(self.config.continue_extras(time.time()), ["ship it"])

    def test_an_over_long_extra_is_refused(self):
        self.write({"continuePrompts": ["x" * (crabd.CONTINUE_PROMPT_MAX + 1)]})
        self.assertEqual(self.config.continue_prompts(time.time()),
                         crabd.CONTINUE_PROMPTS_BUILTIN)

    def test_the_extras_list_is_bounded(self):
        self.write({"continuePrompts": [f"prompt {i}" for i in range(500)]})
        self.assertEqual(len(self.config.continue_extras(time.time())),
                         crabd.CONTINUE_PROMPTS_CAP)


class ContinueEndpointTests(V12ServedTests):
    """POST /v1/action queue-continue, and POST /v1/hook/stop draining it."""

    def stop_hook(self, session_id=None, timeout=10):
        started = time.time()
        status, body = self.post_json(
            "/v1/hook/stop",
            {"session_id": session_id or self.SID, "hook_event_name": "Stop",
             "cwd": "C:\\IT"}, timeout=timeout)
        return status, json.loads(body), time.time() - started

    def test_a_queued_continue_is_204(self):
        status, _ = self.action({"sessionId": self.SID, "action": "queue-continue",
                                 "prompt": "Continue"})
        self.assertEqual(status, 204)
        self.assertEqual(self.continues.peek(self.SID, time.time()), "Continue")

    def test_a_tap_into_a_ghost_session_is_409_and_not_queued(self):
        """GHOST-a (v0.28.1), measured live 2026-09-01: a session the app killed
        without SessionEnd read `working` (the state-None fallback) and three operator
        taps queued prompts no Stop hook would ever drain. No hook-grounded state in
        THIS process + a transcript quiet past IDLE_AFTER_SEC = nobody is listening."""
        old = time.time() - crabd.IDLE_AFTER_SEC - 60
        os.utime(self.session_path(self.SID), (old, old))
        self.rebuild()
        self.assertIsNone(self.hooks.live_state(self.SID))
        status, body = self.action({"sessionId": self.SID, "action": "queue-continue",
                                    "prompt": "Continue"})
        self.assertEqual(status, 409)
        self.assertIn(b"quiet", body)
        self.assertIsNone(self.continues.peek(self.SID, time.time()))

    def test_hook_grounded_state_overrides_a_quiet_transcript(self):
        """The healthy-night case the 409 must NOT fire on: a live long turn writes
        nothing to the transcript for many minutes, but its UserPromptSubmit hook gave
        this process real state - the queue stays open. The hook is seeded in-process
        (hooks.record), not over HTTP: this test pins the GUARD's branch, and an HTTP
        hook's ingest is not synchronous with its 204 on this host's loopback (the
        SYN-ACK quirk in BACKLOG "Host / environment") - it flaked 2-in-4 that way."""
        self.hooks.record({"hook_event_name": "UserPromptSubmit",
                           "session_id": self.SID, "cwd": "C:\\IT"})
        self.assertEqual(self.hooks.live_state(self.SID), "working")
        old = time.time() - crabd.IDLE_AFTER_SEC - 60
        os.utime(self.session_path(self.SID), (old, old))
        self.rebuild()
        status, _ = self.action({"sessionId": self.SID, "action": "queue-continue",
                                 "prompt": "Continue"})
        self.assertEqual(status, 204)
        self.assertEqual(self.continues.peek(self.SID, time.time()), "Continue")

    def test_the_stop_hook_answers_the_pinned_continue_shape(self):
        """PINNED against the SHIPPED CLI 2.1.246, not against the docs. Two shapes in
        that binary's hook-output schema continue the session; this is the one that is
        not labelled an error. `continuationPrompt` appears NOWHERE in the binary and
        `continueConversation` only as an SDK spawn option, so the spike's shape would
        have been silently ignored. The evidence is asserted directly by
        StopContinueShapeBinaryPinTests below."""
        self.action({"sessionId": self.SID, "action": "queue-continue",
                     "prompt": "Run the tests"})
        status, answer, _ = self.stop_hook()
        self.assertEqual(status, 200)
        self.assertEqual(answer, {"hookSpecificOutput": {
            "hookEventName": "Stop", "additionalContext": "Run the tests"}})

    def test_a_replacement_queued_mid_delivery_survives_the_stop_hook(self):
        """CD-30 (v0.21.0), end to end through the real endpoint - the unit test on
        ContinueQueue proves drain_if, this proves the HANDLER calls it.

        The race is real but sub-millisecond over a loopback socket, so it is made
        deterministic where it actually happens: _send_stop_answer is the last thing
        between the peek and the consume, so a replacement queued from inside it lands
        in exactly the window CD-30 describes.
        """
        self.action({"sessionId": self.SID, "action": "queue-continue",
                     "prompt": "Continue"})
        original = crabd.Handler._send_stop_answer
        queue = self.continues
        sid = self.SID

        def racing(handler, body):
            sent = original(handler, body)
            queue.queue(sid, "Run the tests", time.time())   # the operator taps again
            return sent

        crabd.Handler._send_stop_answer = racing
        self.addCleanup(setattr, crabd.Handler, "_send_stop_answer", original)

        status, answer, _ = self.stop_hook()
        self.assertEqual(status, 200)
        # The prompt that was actually delivered is the one that was peeked...
        self.assertEqual(answer["hookSpecificOutput"]["additionalContext"], "Continue")
        # ...and the replacement is still queued for the next Stop, not deleted
        # undelivered. `settle` because the consume deliberately runs AFTER the answer
        # reaches the socket (CRB-F5), so the 200 landing does not mean it has run -
        # asserting on the next line would be asserting a race, and it read "Continue"
        # roughly half the time before this barrier went in.
        settle(lambda: self.continues.peek(self.SID, time.time()) == "Run the tests",
               what="the replacement surviving the drain")
        self.assertEqual(self.continues.peek(self.SID, time.time()), "Run the tests")

    def test_a_delivered_continue_is_spent_when_nothing_replaced_it(self):
        """The mutation guard for the test above: a handler that never consumed would
        re-deliver the same prompt on every Stop for ten minutes."""
        self.action({"sessionId": self.SID, "action": "queue-continue",
                     "prompt": "Continue"})
        self.stop_hook()
        settle(lambda: self.continues.peek(self.SID, time.time()) is None,
               what="the delivered prompt being consumed")
        self.assertEqual(self.stop_hook()[1], {})

    def test_the_answer_carries_no_decision_key_at_all(self):
        """The regression this whole change exists to prevent. `decision:"block"` is what
        made the CLI paint every tap as `Stop hook error occurred` and hand the model the
        nudge as a blocking error. One stray key anywhere in the body re-arms that: the
        normalizer reads top-level `decision` BEFORE it looks at hookSpecificOutput, so
        the two are additive, not alternatives."""
        self.action({"sessionId": self.SID, "action": "queue-continue",
                     "prompt": "Continue"})
        answer = self.stop_hook()[1]
        self.assertNotIn("decision", answer)
        self.assertNotIn("reason", answer)
        self.assertNotIn("continue", answer)
        self.assertEqual(list(answer), ["hookSpecificOutput"])

    def test_the_fallback_shape_is_kept_executable_and_is_not_what_ships(self):
        """The pre-v0.15.0 shape stays in the module as a live constructor so reverting is
        a one-line swap at the call site. It must NOT be what the endpoint answers - that
        is the defect - and it must still be the shape it always was, or it is not a
        fallback anyone can trust in an incident."""
        self.assertEqual(crabd.STOP_CONTINUE_DECISION, "block")
        self.assertEqual(crabd.stop_continue_body_fallback("Continue"),
                         {"decision": "block", "reason": "Continue"})
        self.assertEqual(crabd.STOP_CONTINUE_HOOK_EVENT, "Stop")
        self.assertEqual(crabd.stop_continue_body("Continue"), {"hookSpecificOutput": {
            "hookEventName": "Stop", "additionalContext": "Continue"}})
        self.action({"sessionId": self.SID, "action": "queue-continue",
                     "prompt": "Continue"})
        self.assertNotEqual(self.stop_hook()[1],
                            crabd.stop_continue_body_fallback("Continue"))

    def test_the_endpoint_answers_exactly_what_the_shared_constructor_builds(self):
        """Otherwise the pinned constant is decoration and the wire shape is whatever the
        handler happens to inline - which is how the old shape survived a rewrite of the
        comment block that already described the alternative."""
        for prompt in ("Continue", "Run the tests and report the results."):
            self.action({"sessionId": self.SID, "action": "queue-continue",
                         "prompt": prompt})
            self.assertEqual(self.stop_hook()[1], crabd.stop_continue_body(prompt))

    def test_with_nothing_queued_the_stop_hook_is_a_no_op(self):
        status, answer, _ = self.stop_hook()
        self.assertEqual(status, 200)
        self.assertEqual(answer, {})

    def test_the_stop_hook_still_moves_the_state_machine(self):
        """This endpoint REPLACES /v1/hook for Stop. Skip the record and every session
        sits on `working` forever."""
        self.stop_hook()
        self.assertEqual(self.hooks.snapshot()[self.SID]["state"], "done")
        self.rebuild()
        self.assertEqual(self.row()["state"], "done")

    def test_a_continue_is_delivered_once_and_the_next_stop_is_clean(self):
        self.action({"sessionId": self.SID, "action": "queue-continue",
                     "prompt": "Continue"})
        self.assertEqual(self.stop_hook()[1], crabd.stop_continue_body("Continue"))
        self.assertEqual(self.stop_hook()[1], {})

    def test_an_expired_continue_is_not_delivered(self):
        self.continues.queue(self.SID, "Continue",
                             time.time() - crabd.CONTINUE_TTL_SEC - 5)
        self.assertEqual(self.stop_hook()[1], {})

    def test_the_stop_hook_answers_well_inside_the_two_second_budget(self):
        """Contract: crabd answers the Stop hook within 2 s. Everything on the path is a
        dict lookup, so the measured answer should be well under it."""
        self.action({"sessionId": self.SID, "action": "queue-continue",
                     "prompt": "Continue"})
        self.assertLess(self.stop_hook()[2], crabd.STOP_HOOK_ANSWER_SEC)
        self.assertLess(self.stop_hook()[2], crabd.STOP_HOOK_ANSWER_SEC)

    def test_a_prompt_outside_the_whitelist_is_400(self):
        """The queued string is handed to the model as an instruction and any process on
        this machine can reach a loopback port. The whitelist is what bounds that."""
        for prompt in ("rm -rf /", "Continue ", "continue", "", None, 42, ["Continue"]):
            status, body = self.action({"sessionId": self.SID,
                                        "action": "queue-continue", "prompt": prompt})
            self.assertEqual(status, 400, prompt)
            self.assertIn("error", json.loads(body))
        self.assertIsNone(self.continues.peek(self.SID, time.time()))

    def test_a_config_supplied_prompt_is_accepted_and_reaches_the_widget(self):
        """Both halves of the config extra: the widget can only learn about it from the
        feed (it cannot read config.json), and the queue has to accept what it then
        sends back."""
        self.config_path.write_text(json.dumps({"continuePrompts": ["ship it"]}),
                                    encoding="utf-8")
        self.builder.config = crabd.UserConfig(self.config_path)
        self.assertEqual(self.rebuild()["continuePrompts"], ["ship it"])
        status, _ = self.action({"sessionId": self.SID, "action": "queue-continue",
                                 "prompt": "ship it"})
        self.assertEqual(status, 204)
        self.assertEqual(self.continues.peek(self.SID, time.time()), "ship it")

    def test_the_widgets_own_wire_prompt_round_trips(self):
        """The cross-lane check, over a real socket and with the exact string
        widget/scripts/sidecrab.js puts on the wire for its "Run the tests" button. The
        first version of this whitelist held the button LABELS and would have 400ed
        this - a feature broken in the seam between two lanes, green on both sides."""
        wire = "Run the tests and report the results."
        self.assertEqual(self.action({"sessionId": self.SID,
                                      "action": "queue-continue",
                                      "prompt": wire})[0], 204)
        self.assertEqual(self.stop_hook()[1], crabd.stop_continue_body(wire))

    def test_the_served_extras_are_empty_by_default(self):
        self.assertEqual(self.state()["continuePrompts"], [])

    def test_queueing_against_an_unknown_session_is_404(self):
        """Otherwise the widget is told "accepted" for a prompt that will sit in the
        queue until it expires."""
        status, body = self.action({"sessionId": "not-a-session",
                                    "action": "queue-continue", "prompt": "Continue"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "unknown session"})

    def test_both_halves_of_the_round_trip_are_in_the_history(self):
        """CRB-F5 (v0.16.0): "continue sent" is now written AFTER the answer reaches the
        socket, because before the answer lands nothing has been sent - so the client can
        be back before the line exists. Barrier, not a retry; the assertion is the same
        one."""
        self.action({"sessionId": self.SID, "action": "queue-continue",
                     "prompt": "Commit + push"})
        self.stop_hook()
        settle(lambda: "continue sent: Commit + push" in self.ring(),
               what="the continue-sent history line")
        self.assertIn("continue queued: Commit + push", self.ring())

    def test_a_stop_hook_with_a_junk_body_is_a_no_op_not_a_500(self):
        for raw in (b"not json", b"", b"[]", b'{"hook_event_name":"Stop"}'):
            status, body = self.post("/v1/hook/stop", raw)
            self.assertEqual(status, 200, raw)
            self.assertEqual(json.loads(body), {}, raw)


#: The versioned-directory layout, measured on this Mac 2026-09-04:
#: ~/.local/bin/claude -> ~/.local/share/claude/versions/2.1.260, a Mach-O executable.
_CLAUDE_VERSIONS_DIR = "versions"
#: The npm layout, the same two path segments the WinGet path below ends with.
_CLAUDE_NPM_SEGMENTS = ("@anthropic-ai", "claude-code")


def _claude_layout_version(path: Path) -> str | None:
    """The CLI version this resolved path DECLARES, or None if the path is not in a
    layout the CLI actually installs in.

    Recognising the layout is not fussiness, it is the difference between skipping and
    a false alarm: mise / nvm / asdf put a `#!/bin/sh` wrapper named `claude` on PATH,
    and streaming EVIDENCE needles at a shell script fails every one of them with a
    message that says the CLI changed under a shipping write path.
    """
    if path.parent.name == _CLAUDE_VERSIONS_DIR:
        return path.name                     # .../versions/<version>
    parts = path.parts
    if any(parts[i:i + 2] == _CLAUDE_NPM_SEGMENTS for i in range(len(parts) - 1)):
        manifest = path.parent.parent / "package.json"
        try:
            return json.loads(manifest.read_text(encoding="utf-8"))["version"]
        except Exception:   # noqa: BLE001 - a missing/renamed manifest is not the claim
            return "unknown"
    return None


def _shipped_claude_binary():
    """The shipped CLI, or None. `CRABD_CLAUDE_BINARY` overrides for a differently
    installed host.

    Two defaults, because the CLI installs two different ways. Windows: the WinGet-
    managed Node install, named in full. Elsewhere: whatever `claude` PATH points at,
    RESOLVED (the launcher is a symlink into a versioned directory) and then CHECKED
    against the layouts above - an unrecognised one is None, so the pin skips rather
    than measuring something that is not the CLI.
    """
    override = os.environ.get("CRABD_CLAUDE_BINARY")
    if override:
        path = Path(override)
        return path if path.is_file() else None
    if sys.platform == "win32":
        path = (Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" /
                "Packages" /
                "OpenJS.NodeJS.LTS_Microsoft.Winget.Source_8wekyb3d8bbwe" /
                "node-v24.16.0-win-x64" / "node_modules" / "@anthropic-ai" /
                "claude-code" / "bin" / "claude.exe")
        return path if path.is_file() else None
    found = shutil.which("claude")
    if not found:
        return None
    path = Path(found).resolve()
    if not path.is_file() or _claude_layout_version(path) is None:
        return None
    return path


@unittest.skipIf(sys.platform == "win32",
                 "the WinGet path is the Windows branch and is asserted by using it")
class ShippedClaudeBinaryPathLookupTests(unittest.TestCase):
    """Where the shape pin below LOOKS for the CLI when the host is not Windows.

    Off Windows there is no WinGet layout to name, so PATH is the answer - and the
    launcher on PATH is normally a symlink into a versioned directory, which is why the
    lookup resolves before checking. Without this the pin would skip on every non-Windows
    host, and a shape pin that never runs is a shape pin that proves nothing.
    """

    def setUp(self):
        """PATH is set to a temp bin/ and the REAL shutil.which runs over it. A stubbed
        `which` would let a test assert a shape the resolver never actually produces -
        executability and dangling links are its rules, not this file's."""
        override = os.environ.pop("CRABD_CLAUDE_BINARY", None)
        if override is not None:
            self.addCleanup(os.environ.__setitem__, "CRABD_CLAUDE_BINARY", override)
        original_path = os.environ.get("PATH", "")
        self.addCleanup(os.environ.__setitem__, "PATH", original_path)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        os.environ["PATH"] = str(self.bin)

    def launcher(self, target: Path):
        """`claude` on PATH, as a symlink - which is how every installer writes it."""
        (self.bin / "claude").symlink_to(target)

    def executable(self, path: Path, body=b"\x7fELF\x02\x01\x01"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        path.chmod(0o755)
        return path

    def test_the_versioned_directory_layout_resolves_to_the_real_file(self):
        """The layout measured on this Mac:
        ~/.local/bin/claude -> ~/.local/share/claude/versions/<version>."""
        real = self.executable(self.root / "share" / "claude" / "versions" / "2.1.260")
        self.launcher(real)
        found = _shipped_claude_binary()
        # real.resolve() rather than real: macOS's own temp root is a symlink
        # (/var/folders -> /private/var/folders), so the expectation has to be resolved
        # too or this asserts the platform's quirk instead of the lookup's behaviour.
        self.assertEqual(found, real.resolve())
        self.assertNotEqual(found, self.bin / "claude")   # the symlink is not the answer

    def test_the_npm_layout_is_accepted_too(self):
        real = self.executable(self.root / "node_modules" / "@anthropic-ai" /
                               "claude-code" / "cli.js")
        self.launcher(real)
        self.assertEqual(_shipped_claude_binary(), real.resolve())

    def test_a_shell_shim_on_path_is_refused(self):
        """THE ONE THAT MATTERS. mise / nvm / asdf put a `#!/bin/sh` wrapper named
        `claude` on PATH. Its bytes are a shell script, so every EVIDENCE needle is
        missing from it - and the shape pin would then hard-fail saying the CLI changed
        under a shipping write path, which is the exact false alarm its docstring
        forbids. An unrecognised layout must SKIP, not fail."""
        shim = self.executable(self.bin / "claude",
                               b"#!/bin/sh\nexec mise x -- claude \"$@\"\n")
        self.assertTrue(shim.is_file())          # it really is on PATH and executable
        self.assertIsNone(_shipped_claude_binary())

    def test_no_claude_on_path_is_none_rather_than_a_crash(self):
        """The CI case. None means SKIP downstream, which is the honest answer where
        there is nothing to measure."""
        self.assertIsNone(_shipped_claude_binary())

    def test_a_dangling_launcher_is_none(self):
        """shutil.which filters it (os.access follows the link and fails), and the
        is_file() check behind it is the belt to that brace."""
        self.launcher(self.root / "share" / "claude" / "versions" / "gone")
        self.assertIsNone(_shipped_claude_binary())


class ShippedClaudeVersionTests(unittest.TestCase):
    """The version NAMED in the pin's failure message. Never asserted - a CLI upgrade
    is a healthy night - but it has to be right or the message misdirects."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_the_versioned_directory_names_the_version(self):
        path = self.root / "share" / "claude" / "versions" / "2.1.260"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\x7fELF")
        self.assertEqual(StopContinueShapeBinaryPinTests._version(path), "2.1.260")

    def test_the_npm_layout_still_reads_its_manifest(self):
        pkg = self.root / "node_modules" / "@anthropic-ai" / "claude-code"
        (pkg / "bin").mkdir(parents=True)
        (pkg / "package.json").write_text('{"version": "2.1.199"}', encoding="utf-8")
        path = pkg / "bin" / "claude"
        path.write_bytes(b"\x7fELF")
        self.assertEqual(StopContinueShapeBinaryPinTests._version(path), "2.1.199")

    def test_a_layout_with_neither_says_unknown(self):
        path = self.root / "claude"
        path.write_bytes(b"\x7fELF")
        self.assertEqual(StopContinueShapeBinaryPinTests._version(path), "unknown")


class StopContinueShapeBinaryPinTests(unittest.TestCase):
    """The Stop-hook continue shape, pinned against the SHIPPED CLI rather than the docs.

    Every other test in this file can only prove crabd emits what crabd intends. This one
    asserts the intention is still true of the binary on the other end of the socket, by
    finding the exact strings the v0.15.0 shape was read off. It is the reason the switch
    away from `decision:"block"` is a measurement and not a preference.

    SKIPS when the binary is absent (CI, another host) - a shape pin that hard-fails
    where there is nothing to measure teaches people to delete it. Skipping is honest:
    the claim is untested there, not disproved.

    A failure here is NOT "fix the test". It means the CLI changed under a shipping write
    path, and the questions are, in order: does additionalContext still continue the
    session, and is STOP_CONTINUE_DECISION now the shape to revert to?
    """

    #: Read out of claude.exe 2.1.246 on 2026-08-26. Prose strings only - the authors
    #: wrote these, so they survive a re-minify; identifier-level text would not.
    EVIDENCE = {
        # 1. The Stop member of the hookSpecificOutput union exists, and the binary's own
        #    description is the continuation guarantee in the vendor's words.
        "the Stop hookSpecificOutput member and its promise":
            b"Hook-specific output for the Stop event. additionalContext is non-error "
            b"feedback delivered to the model; the conversation continues so the model "
            b"can act on it.",
        # 2. The normalizer lifts additionalContext for Stop - without this branch the
        #    field parses and is then dropped, which is the silent-no-op failure mode.
        "the normalizer branch that lifts it": b'case"Stop":case"SubagentStop":',
        # 3. The turn loop's force-another-turn predicate. Both the blocking-error and the
        #    additional-context attachments are pushed onto the SAME array this tests, so
        #    the new shape takes the identical continuation path.
        "the force-another-turn predicate": b"blockingErrors.length>0",
        # 4. Why the old shape looked broken, and that the label is kept separate.
        "the error chrome the old shape triggered": b"Stop hook error occurred",
        "the model-facing label of the old shape": b" hook blocking error from command: ",
        "the model-facing label of the new shape": b" hook additional context: ",
        "the transcript field kept out of hook_errors":
            b"Non-error feedback from hookSpecificOutput.additionalContext",
        # 5. hookEventName is validated, so the constant is load-bearing.
        "the hookEventName check": b"Hook returned incorrect event name: expected ",
    }

    #: Absent from 2.1.246 entirely. docs/spikes/reply-spike-2.md read this off the
    #: published docs; a body carrying it is silently ignored and the session stops.
    #: Kept as an assertion so a future doc-led "fix" fails here first.
    ABSENT = b"continuationPrompt"

    @classmethod
    def setUpClass(cls):
        path = _shipped_claude_binary()
        if path is None:
            raise unittest.SkipTest("shipped claude binary not on this host")
        cls.path = path
        needles = list(cls.EVIDENCE.values()) + [cls.ABSENT]
        cls.counts = cls._count(path, needles)
        cls.version = cls._version(path)

    @staticmethod
    def _version(path):
        """Named in the failure message, NOT asserted. The evidence strings are the pin;
        a CLI upgrade is a healthy night and must not redden this suite on its own.

        The versioned-directory layout names the version in the file name and carries no
        manifest beside it; the npm layout reads package.json. Same recogniser the
        lookup uses, so a path the lookup accepted always has an answer here."""
        return _claude_layout_version(path) or "unknown"

    @staticmethod
    def _count(path, needles):
        """One streamed pass, ~0.2 s over 250 MB. The overlap keeps a needle that straddles
        a chunk boundary from being missed - and from being counted twice."""
        counts = {n: 0 for n in needles}
        overlap = max(len(n) for n in needles) - 1
        with path.open("rb") as handle:
            tail = b""
            while True:
                chunk = handle.read(1 << 22)
                if not chunk:
                    break
                buf = tail + chunk
                for needle in needles:
                    counts[needle] += buf.count(needle)
                tail = buf[-overlap:] if overlap else b""
                for needle in needles:              # un-double the overlap region
                    counts[needle] -= tail.count(needle)
            for needle in needles:
                counts[needle] += tail.count(needle)
        return counts

    def test_the_binary_still_carries_every_string_the_shape_was_read_off(self):
        missing = [what for what, needle in self.EVIDENCE.items()
                   if self.counts[needle] == 0]
        self.assertEqual(missing, [], "measured against 2.1.246; this host runs "
                                      f"{self.version} and no longer carries: {missing}")

    def test_the_docs_only_shape_is_still_absent_from_the_binary(self):
        self.assertEqual(self.counts[self.ABSENT], 0,
                         f"`continuationPrompt` has APPEARED in {self.version} - "
                         "re-read the schema before trusting either shape")

    def test_the_shape_crabd_ships_is_the_one_the_evidence_describes(self):
        """Ties the measurement to the code: the event name crabd sends is the one the
        binary's Stop union member is keyed on, and the field it fills is the one the
        normalizer lifts."""
        body = crabd.stop_continue_body("Run the tests")
        specific = body["hookSpecificOutput"]
        self.assertEqual(set(body), {"hookSpecificOutput"})
        self.assertEqual(specific["hookEventName"], crabd.STOP_CONTINUE_HOOK_EVENT)
        self.assertEqual(specific["additionalContext"], "Run the tests")
        promise = self.EVIDENCE["the Stop hookSpecificOutput member and its promise"]
        self.assertIn(specific["hookEventName"].encode(), promise)
        self.assertIn(b"additionalContext", promise)
        self.assertIn(b"the conversation continues", promise)



# ================================================== v0.12.0: panel approvals

class PermissionBrokerUnitTests(unittest.TestCase):
    """PermissionBroker on its own. The load-bearing test in this file is
    test_no_sequence_of_events_produces_an_allow_without_a_tap."""

    SID = "8a8a8a8a-0000-0000-0000-000000000005"

    def setUp(self):
        self.broker = crabd.PermissionBroker()
        self.now = time.time()

    def test_a_tap_releases_the_hold(self):
        entry = self.broker.register(self.SID, "Bash", "git status", self.now)
        self.assertEqual(self.broker.decide(self.SID, "allow", self.now), "Bash")
        self.assertEqual(self.broker.wait(entry, 0.1), "allow")

    def test_a_timeout_is_none_and_never_an_allow(self):
        entry = self.broker.register(self.SID, "Bash", None, self.now)
        started = time.time()
        self.assertIsNone(self.broker.wait(entry, 0.05))
        self.assertLess(time.time() - started, 1.0)

    def test_no_sequence_of_events_produces_an_allow_without_a_tap(self):
        """The one property worth reading this class for. A companion that could allow a
        tool call on its own would be a remote-execution hole wearing a status widget,
        so this drives every path that ISN'T a decide() and asserts none of them
        produces an allow."""
        entry = self.broker.register(self.SID, "Bash", "rm -rf /", self.now)
        self.assertIsNone(self.broker.wait(entry, 0.02))              # timeout
        self.broker.release(self.SID, entry)                          # released
        self.assertIsNone(entry["decision"])
        self.assertIsNone(self.broker.decide(self.SID, "allow", self.now))  # gone
        for bogus in ("approve", "ask", "yes", True, None, "ALLOW", ""):
            other = self.broker.register("s2", "Bash", None, self.now)
            self.assertIsNone(self.broker.decide("s2", bogus, self.now), bogus)
            self.assertIsNone(other["decision"], bogus)
            self.broker.release("s2", other)

    # ---- F3: the timeout/release gap

    def test_a_tap_inside_the_timeout_release_gap_is_honoured_not_phantom(self):
        """AUDIT F3 (fixed v0.17.0). `wait` returning None and the entry being dropped
        were two separate instants, and a tap could land BETWEEN them: decide() found the
        entry undecided, so /v1/action wrote "approved from panel: Bash" and answered the
        widget 204 - while the hook handler, holding the None it had already read, passed
        through and let the terminal dialog own the call. History said approved; nothing
        was. release() is now the authority and reports the decision that actually
        applies, so the record and the answer agree."""
        entry = self.broker.register(self.SID, "Bash", None, self.now)
        self.assertIsNone(self.broker.wait(entry, 0.02))            # the hold expired
        self.assertEqual(self.broker.decide(self.SID, "allow", self.now), "Bash")
        self.assertEqual(self.broker.release(self.SID, entry), "allow")

    def test_a_tap_after_the_hold_is_closed_is_rejected(self):
        """The other side of the same window, and the answer the contract already
        specifies: once release() has run, a decide finds nothing pending -> 404, because
        by then the terminal dialog owns the decision."""
        entry = self.broker.register(self.SID, "Bash", None, self.now)
        self.assertIsNone(self.broker.wait(entry, 0.02))
        self.assertIsNone(self.broker.release(self.SID, entry))     # ordinary timeout
        self.assertIsNone(self.broker.decide(self.SID, "allow", self.now))
        self.assertIsNone(entry["decision"])
        self.assertIsNone(self.broker.pending(self.SID))

    def test_releasing_a_superseded_entry_reports_no_decision(self):
        """release() reports on ITS entry. A newer request for the same session is a
        different hold and must be neither reported on nor un-registered."""
        first = self.broker.register(self.SID, "Bash", None, self.now)
        self.broker.register(self.SID, "Write", None, self.now + 1)
        self.assertIsNone(self.broker.release(self.SID, first))
        self.assertEqual(self.broker.pending(self.SID)["tool"], "Write")

    # ---- v0.19.0: the hold whose dialog was answered in the app

    def test_stale_releases_the_hold_as_a_pass_through(self):
        """MEASURED, docs/spikes/live-verify.md 3.3: the terminal dialog is not
        suppressed, it is RACED - it renders immediately while crabd holds the poll. So
        the operator can answer it at t=2s and the card keeps offering Approve / Deny for
        another 53 s. A Stop / UserPromptSubmit / SessionEnd for that session proves the
        turn moved past the dialog, and this retires the hold."""
        entry = self.broker.register(self.SID, "Bash", "git status", self.now)
        self.assertEqual(self.broker.stale(self.SID), "Bash")
        self.assertIsNone(self.broker.pending(self.SID))
        self.assertIsNone(self.broker.wait(entry, 0.5))     # woken, still a pass-through
        self.assertIsNone(entry["decision"])

    def test_stale_never_produces_a_decision(self):
        """It is on the no-allow path with everything else that is not a tap."""
        entry = self.broker.register(self.SID, "Bash", "rm -rf /", self.now)
        self.broker.stale(self.SID)
        self.assertIsNone(entry["decision"])
        self.assertIsNone(self.broker.release(self.SID, entry))
        self.assertIsNone(self.broker.decide(self.SID, "allow", self.now))

    def test_stale_leaves_a_tap_that_already_landed_alone(self):
        entry = self.broker.register(self.SID, "Bash", None, self.now)
        self.assertEqual(self.broker.decide(self.SID, "allow", self.now), "Bash")
        self.assertIsNone(self.broker.stale(self.SID))
        self.assertEqual(self.broker.wait(entry, 0.1), "allow")

    def test_stale_with_nothing_parked_is_a_no_op(self):
        self.assertIsNone(self.broker.stale(self.SID))
        self.assertEqual(self.broker.count(), 0)

    def test_release_after_stale_does_not_unregister_a_newer_hold(self):
        first = self.broker.register(self.SID, "Bash", None, self.now)
        self.broker.stale(self.SID)
        self.broker.register(self.SID, "Write", None, self.now + 1)
        self.assertIsNone(self.broker.release(self.SID, first))
        self.assertEqual(self.broker.pending(self.SID)["tool"], "Write")

    def test_a_deny_is_a_deny(self):
        entry = self.broker.register(self.SID, "Write", None, self.now)
        self.broker.decide(self.SID, "deny", self.now)
        self.assertEqual(self.broker.wait(entry, 0.1), "deny")

    def test_a_second_decision_finds_nothing_pending(self):
        self.broker.register(self.SID, "Bash", None, self.now)
        self.assertEqual(self.broker.decide(self.SID, "allow", self.now), "Bash")
        self.assertIsNone(self.broker.decide(self.SID, "deny", self.now))

    def test_the_pending_view_is_the_contract_shape(self):
        self.broker.register(self.SID, "Bash", "git push", self.now)
        pending = self.broker.pending(self.SID)
        self.assertEqual(sorted(pending), ["requestId", "requestedAt", "summary", "tool"])
        self.assertEqual((pending["tool"], pending["summary"]), ("Bash", "git push"))
        self.assertTrue(pending["requestedAt"].endswith("Z"))

    def test_a_decided_request_stops_being_pending(self):
        self.broker.register(self.SID, "Bash", None, self.now)
        self.broker.decide(self.SID, "allow", self.now)
        self.assertIsNone(self.broker.pending(self.SID))

    def test_the_broker_is_bounded(self):
        for i in range(crabd.PERMISSION_MAX_PENDING):
            self.assertIsNotNone(self.broker.register(f"s{i}", "Bash", None, self.now))
        self.assertIsNone(self.broker.register("one-too-many", "Bash", None, self.now))

    def test_a_replacement_request_releases_the_one_it_replaced(self):
        """The older prompt is still sitting in a terminal waiting for someone. Leaving
        its holder parked would strand a live request on a panel entry nothing can
        answer."""
        first = self.broker.register(self.SID, "Bash", None, self.now)
        second = self.broker.register(self.SID, "Write", None, self.now + 1)
        self.assertIsNone(self.broker.wait(first, 0.1))    # released as pass-through
        self.assertEqual(self.broker.pending(self.SID)["tool"], "Write")
        self.broker.release(self.SID, second)

    def test_releasing_a_superseded_entry_does_not_unregister_the_live_one(self):
        first = self.broker.register(self.SID, "Bash", None, self.now)
        self.broker.register(self.SID, "Write", None, self.now + 1)
        self.broker.release(self.SID, first)
        self.assertEqual(self.broker.pending(self.SID)["tool"], "Write")

    def test_the_summary_reads_the_argument_that_matters(self):
        cases = [({"command": "git push --force"}, "git push --force"),
                 ({"file_path": "C:\\IT\\a.md", "content": "x" * 5000},
                  "C:\\IT\\a.md"),
                 ({"url": "https://example.com"}, "https://example.com"),
                 ({"weird": "blob"}, None), ({}, None), (None, None), ("no", None)]
        for tool_input, expected in cases:
            self.assertEqual(crabd.PermissionBroker.summarize(tool_input), expected,
                             tool_input)

    def test_a_long_summary_is_trimmed_for_the_panel(self):
        summary = crabd.PermissionBroker.summarize({"command": "echo " + "x" * 500})
        self.assertLessEqual(len(summary), crabd.PERMISSION_SUMMARY_MAX)


class PanelApprovalConfigTests(V12ServedTests):
    """POST /v1/config's fifth key."""

    def test_it_is_off_by_default(self):
        self.assertFalse(self.builder.config.panel_approvals(time.time()))

    def test_it_is_NOT_writable_over_http(self):
        """QA-Audit SEC-2: panelApprovals is a security flag and was removed from
        CONFIG_WRITABLE. A POST naming it - even a well-formed one - must be rejected 400
        and must NOT write the file. This is the fix for 'any local process / any visited
        web page can arm panel approvals over loopback'."""
        before = self.read_config().get("panelApprovals")
        status, body = self.config_post({"panelApprovals": {"enabled": True}})
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body))
        self.assertEqual(self.read_config().get("panelApprovals"), before)  # file untouched
        self.builder.config = crabd.UserConfig(self.config_path)
        self.assertFalse(self.builder.config.panel_approvals(time.time()))

    def test_it_cannot_ride_along_with_a_valid_key(self):
        """A body mixing a writable key with panelApprovals is rejected WHOLE - the
        quietHours half must not land while the security half is stripped."""
        status, _ = self.config_post({"quietHours": None, "panelApprovals": {"enabled": True}})
        self.assertEqual(status, 400)

    def test_a_malformed_config_reads_as_off(self):
        for bad in (True, 1, "yes", {"Enabled": True}, {"enabled": 1}, [], None):
            self.config_path.write_text(json.dumps({"panelApprovals": bad}),
                                        encoding="utf-8")
            config = crabd.UserConfig(self.config_path)
            self.assertFalse(config.panel_approvals(time.time()), bad)


class PanelTokenUnitTests(unittest.TestCase):
    """PanelToken (v0.29.0): the pairing code that makes `decide` un-forgeable."""

    def test_a_generated_code_is_ten_symbols_of_the_unambiguous_alphabet(self):
        for _ in range(50):
            code = crabd.PanelToken.generate()
            self.assertEqual(len(code), crabd.PANEL_TOKEN_LEN)
            self.assertTrue(set(code) <= set(crabd.PANEL_TOKEN_ALPHABET), code)
            self.assertFalse(set(code) & set("ILOU"))

    def test_load_or_create_mints_once_and_rereads_the_same_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "panel-token"
            first = crabd.PanelToken.load_or_create(path)
            self.assertTrue(path.is_file())
            on_disk = path.read_text(encoding="utf-8").strip()
            self.assertRegex(on_disk, r"^[0-9A-HJ-NP-TV-Z]{5}-[0-9A-HJ-NP-TV-Z]{5}$")
            self.assertEqual(first.verify(on_disk, 0.0), "ok")
            second = crabd.PanelToken.load_or_create(path)
            self.assertEqual(second.verify(on_disk, 0.0), "ok")

    def test_an_unusable_file_is_replaced_not_trusted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "panel-token"
            path.write_text("abc\n", encoding="utf-8")          # too short
            gate = crabd.PanelToken.load_or_create(path)
            self.assertEqual(gate.verify("abc", 0.0), "rejected")
            self.assertEqual(gate.verify(path.read_text(encoding="utf-8"), 0.0), "ok")

    def test_the_code_is_matched_without_case_or_hyphens(self):
        gate = crabd.PanelToken(None, "K7QXM2PDAB")
        for form in ("K7QXM-2PDAB", "k7qxm2pdab", " k7qxm 2pdab ", "K7QXM2PDAB"):
            self.assertEqual(gate.verify(form, 0.0), "ok", form)

    def test_missing_and_wrong_are_different_answers_and_neither_is_ok(self):
        gate = crabd.PanelToken(None, "K7QXM2PDAB")
        for missing in (None, "", "   ", 7, ["K7QXM2PDAB"]):
            self.assertEqual(gate.verify(missing, 0.0), "missing", repr(missing))
        for wrong in ("K7QXM2PDAC", "K7QXM2PDA", "K7QXM2PDABX", "0000000000"):
            self.assertEqual(gate.verify(wrong, 0.0), "rejected", wrong)

    def test_no_code_loaded_never_verifies(self):
        gate = crabd.PanelToken(None, None)
        self.assertEqual(gate.verify("K7QXM2PDAB", 0.0), "rejected")
        self.assertFalse(gate.status(0.0)["present"])

    def test_repeated_rejects_lock_the_gate_and_the_right_code_is_locked_too(self):
        gate = crabd.PanelToken(None, "K7QXM2PDAB")
        for i in range(crabd.PANEL_TOKEN_MAX_FAILURES - 1):
            self.assertEqual(gate.verify("WRONG", float(i)), "rejected")
        self.assertEqual(gate.verify("WRONG", 10.0), "locked")
        self.assertEqual(gate.verify("K7QXM2PDAB", 11.0), "locked")
        self.assertIsNotNone(gate.status(11.0)["lockedUntil"])
        self.assertEqual(gate.verify("K7QXM2PDAB", 11.0 + crabd.PANEL_TOKEN_LOCKOUT_SEC), "ok")

    def test_rejects_outside_the_window_do_not_accumulate(self):
        gate = crabd.PanelToken(None, "K7QXM2PDAB")
        for i in range(crabd.PANEL_TOKEN_MAX_FAILURES * 3):
            self.assertEqual(gate.verify("WRONG", i * (crabd.PANEL_TOKEN_WINDOW_SEC + 1)),
                             "rejected")

    def test_status_never_carries_the_code(self):
        gate = crabd.PanelToken(None, "K7QXM2PDAB")
        self.assertNotIn("K7QXM2PDAB", json.dumps(gate.status(0.0)))
        self.assertEqual(set(gate.status(0.0)), {"present", "rejectedRecently", "lockedUntil"})


class PermissionEndpointTests(V12ServedTests):
    """POST /v1/hook/permission - the long poll, end to end over a socket."""

    def setUp(self):
        super().setUp()
        self._poll = crabd.PERMISSION_POLL_SEC
        self.addCleanup(lambda: setattr(crabd, "PERMISSION_POLL_SEC", self._poll))
        # Registered LAST so it runs FIRST (cleanups are LIFO): every parked handler is
        # released before the server is closed and the client's sockets are dropped.
        #
        # This is the second half of the determinism fix. A test that fires a permission
        # and does not decide it leaves a handler thread parked in broker.wait() for the
        # WHOLE poll - up to 55 s with the default - holding a socket on a server the
        # next test has already replaced. Those sockets pile up across the class and are
        # what turned "one connect in a few hundred stalls" into a failure that could
        # land on any test in the file. Releasing with decision None is the same
        # pass-through a timeout produces, so nothing under test is short-circuited.
        self._fired = []
        self.addCleanup(self.release_parked_permissions)

    def release_parked_permissions(self):
        broker = getattr(self, "permissions", None)
        if broker is not None:
            with broker._lock:
                entries = list(broker._pending.values())
                broker._pending.clear()
            for entry in entries:
                entry["event"].set()        # decision stays None = pass-through
        for thread in getattr(self, "_fired", []):
            thread.join(timeout=10)

    def enable(self):
        self.config_path.write_text(json.dumps({"panelApprovals": {"enabled": True}}),
                                    encoding="utf-8")
        self.builder.config = crabd.UserConfig(self.config_path)

    def hook_payload(self, tool="Bash", tool_input=None, session_id=None):
        return {"session_id": session_id or self.SID,
                "hook_event_name": "PermissionRequest", "tool_name": tool,
                "tool_input": tool_input if tool_input is not None
                else {"command": "git push --force"},
                "cwd": "C:\\IT", "permission_mode": "default",
                "tool_use_id": "toolu_x"}

    def fire(self, **kwargs):
        """The hook in a background thread, since it is designed to BLOCK. Returns
        (results list, thread); the list fills with (status, raw body)."""
        out = []
        thread = threading.Thread(
            target=lambda: out.append(self.post_json("/v1/hook/permission",
                                                     self.hook_payload(**kwargs),
                                                     timeout=30)),
            daemon=True)
        thread.start()
        self._fired.append(thread)      # so cleanup can join it, not abandon it
        return out, thread

    def await_pending(self, session_id=None, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.permissions.pending(session_id or self.SID) is not None:
                return True
            time.sleep(0.01)
        return False

    # ---- disabled

    def test_disabled_passes_straight_through(self):
        """Default OFF. The terminal dialog appears exactly as it does with SideCrab
        uninstalled, and nothing is registered for the panel to show."""
        started = time.time()
        status, body = self.post_json("/v1/hook/permission", self.hook_payload())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})
        self.assertLess(time.time() - started, 2.0)
        self.assertEqual(self.permissions.count(), 0)
        self.assertIsNone(self.rebuild()["sessions"][0]["pendingPermission"])

    def test_a_junk_body_passes_through_even_when_enabled(self):
        self.enable()
        for raw in (b"not json", b"", b"[]", b'{"hook_event_name":"PermissionRequest"}'):
            status, body = self.post("/v1/hook/permission", raw)
            self.assertEqual(status, 200, raw)
            self.assertEqual(json.loads(body), {}, raw)
        self.assertEqual(self.permissions.count(), 0)

    # ---- enabled

    def test_a_pending_request_shows_up_on_the_row(self):
        self.enable()
        out, thread = self.fire()
        self.assertTrue(self.await_pending())
        row = next(r for r in self.rebuild()["sessions"] if r["id"] == self.SID)
        self.assertEqual(sorted(row["pendingPermission"]),
                         ["requestId", "requestedAt", "summary", "tool"])
        self.assertEqual(row["pendingPermission"]["tool"], "Bash")
        self.assertEqual(row["pendingPermission"]["summary"], "git push --force")
        self.action(self.decide_body("deny"))
        thread.join(timeout=10)
        self.assertTrue(out)

    def test_an_approval_answers_the_hook_with_the_pinned_allow_shape(self):
        """PINNED against the shipped 2.1.246 schema:
            {hookEventName:"PermissionRequest",
             decision: {behavior:"allow", ...} | {behavior:"deny", ...}}
        NOT the PreToolUse-style permissionDecision string, which PermissionRequest does
        not accept."""
        self.enable()
        out, thread = self.fire()
        self.assertTrue(self.await_pending())
        self.assertEqual(self.action(self.decide_body("allow"))[0], 204)
        thread.join(timeout=10)
        status, body = out[0]
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {
            "hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                   "decision": {"behavior": "allow"}}})

    def test_a_denial_carries_a_message_the_operator_will_recognise(self):
        self.enable()
        out, thread = self.fire(tool="Write", tool_input={"file_path": "C:\\IT\\x.md"})
        self.assertTrue(self.await_pending())
        self.action(self.decide_body("deny"))
        thread.join(timeout=10)
        self.assertEqual(json.loads(out[0][1]), {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny",
                             "message": crabd.PERMISSION_DENY_MESSAGE}}})

    def test_a_timeout_passes_through_and_is_not_a_deny(self):
        """No tap means the terminal dialog gets it. A deny here would silently refuse
        work the operator never looked at."""
        self.enable()
        crabd.PERMISSION_POLL_SEC = 0.4
        status, body = self.post_json("/v1/hook/permission", self.hook_payload())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})
        self.assertEqual(self.permissions.count(), 0)

    def test_a_timed_out_request_leaves_no_pending_entry_on_the_row(self):
        self.enable()
        crabd.PERMISSION_POLL_SEC = 0.4
        self.post_json("/v1/hook/permission", self.hook_payload())
        self.assertIsNone(self.rebuild()["sessions"][0]["pendingPermission"])

    def test_no_answer_this_endpoint_can_give_is_an_unrequested_allow(self):
        """Every early exit lands on the same pass-through object, and none of them is an
        allow: disabled, junk body, no session id, nobody tapping."""
        crabd.PERMISSION_POLL_SEC = 0.3
        answers = [self.post_json("/v1/hook/permission", self.hook_payload())[1]]
        self.enable()
        answers.append(self.post("/v1/hook/permission", b"not json")[1])
        answers.append(self.post_json("/v1/hook/permission",
                                      {"hook_event_name": "PermissionRequest"})[1])
        answers.append(self.post_json("/v1/hook/permission", self.hook_payload())[1])
        for raw in answers:
            self.assertEqual(json.loads(raw), {})
            self.assertNotIn("allow", raw.decode())

    def test_a_saturated_broker_passes_through_rather_than_parking_a_thread(self):
        self.enable()
        for i in range(crabd.PERMISSION_MAX_PENDING):
            self.permissions.register(f"filler-{i}", "Bash", None, time.time())
        status, body = self.post_json("/v1/hook/permission", self.hook_payload())
        self.assertEqual((status, json.loads(body)), (200, {}))

    # ---- history

    def test_every_decision_is_a_history_line(self):
        self.enable()
        for decision, expected in (("allow", "approved from panel: Bash"),
                                   ("deny", "denied from panel: Bash")):
            out, thread = self.fire()
            self.assertTrue(self.await_pending())
            self.action(self.decide_body(decision))
            thread.join(timeout=10)
            self.assertIn(expected, self.ring(), decision)

    def test_the_request_and_the_pass_through_are_recorded_too(self):
        """An operator has to be able to tell "I did not tap in time" from "the panel
        never saw it" - both otherwise look like a terminal dialog."""
        self.enable()
        crabd.PERMISSION_POLL_SEC = 0.3
        self.post_json("/v1/hook/permission", self.hook_payload())
        ring = self.ring()
        self.assertIn("permission requested: Bash", ring)
        self.assertIn("permission passed through: Bash", ring)

    def test_the_tool_argument_never_reaches_the_history(self):
        """The summary is served on /v1/state and NOWHERE else. history.jsonl's rule is
        event kind + session id + title + ts, and a Bash command line is content."""
        self.enable()
        crabd.PERMISSION_POLL_SEC = 0.3
        self.post_json("/v1/hook/permission",
                       self.hook_payload(tool_input={"command": "git push --force"}))
        for text in self.ring():
            self.assertNotIn("git push", text)

    # ---- the timeout/release gap and its history line (audit F3 + F7, v0.17.0)

    def wait_hook(self, during):
        """Shadow broker.wait so something can happen INSIDE the hold, deterministically.

        Both defects below live in a sub-millisecond window at the 55 s mark. A test that
        tries to hit it by timing asserts a race and passes for the wrong reason on a slow
        run; this puts the event exactly where the defect needs it."""
        real = self.permissions.wait
        self.addCleanup(lambda: setattr(self.permissions, "wait", real))

        def wait_then(entry, timeout):
            outcome = real(entry, 0.05)     # the hold expires with nobody having tapped
            during(entry)
            return outcome
        self.permissions.wait = wait_then

    def test_a_tap_in_the_timeout_gap_answers_the_hook_instead_of_a_phantom_approval(self):
        """AUDIT F3 (fixed v0.17.0). The tap lands after the hold expired but before the
        entry is dropped. It used to be recorded as an approval the hook never acted on -
        history said "approved from panel", the widget got its 204, and the operator then
        answered the TERMINAL dialog because the hook had already passed through. Now
        release() reads the decision under the lock that drops the entry, so the hook
        answers the decision that was actually made and nothing claims a pass-through."""
        self.enable()
        self.wait_hook(lambda entry: self.permissions.decide(self.SID, "allow",
                                                             time.time()))
        status, body = self.post_json("/v1/hook/permission", self.hook_payload())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {
            "hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                   "decision": {"behavior": "allow"}}})
        self.assertNotIn("permission passed through: Bash", self.ring())
        self.assertEqual(self.permissions.count(), 0)

    def test_a_tap_that_misses_the_gap_entirely_is_still_a_pass_through(self):
        """The unchanged case, asserted next to F3 so the fix cannot be read as "a late
        tap now wins". Nothing taps during the hold; the hook passes through, and the tap
        that arrives afterwards gets the contract's 404."""
        self.enable()
        crabd.PERMISSION_POLL_SEC = 0.3
        status, body = self.post_json("/v1/hook/permission", self.hook_payload())
        self.assertEqual((status, json.loads(body)), (200, {}))
        self.assertIn("permission passed through: Bash", self.ring())
        self.assertEqual(self.action(self.decide_body("allow"))[0], 404)

    def test_the_pass_through_line_survives_a_row_that_aged_out_during_the_hold(self):
        """AUDIT F7 (fixed v0.17.0). The timeout branch wrote its history line with the
        default create=False, so a session whose row aged out during the 55 s hold lost
        the line entirely - and "I did not tap in time" became indistinguishable from
        "the panel never saw it", the one distinction PERMISSION_EVENT_TIMEOUT exists for.
        The hold is only ever taken for a session crabd IS serving, so create=True here
        cannot grow the table with ids it knows nothing about."""
        self.enable()

        def age_out(entry):
            with self.hooks._lock:
                self.hooks.sessions.pop(self.SID, None)
        self.wait_hook(age_out)
        status, body = self.post_json("/v1/hook/permission", self.hook_payload())
        self.assertEqual((status, json.loads(body)), (200, {}))     # still a pass-through
        self.assertIn("permission passed through: Bash", self.ring())

    # ---- the decide action

    def test_deciding_with_nothing_pending_is_404(self):
        """A tap that lands after the hold expired must not read as an approval: by then
        the terminal dialog owns the decision."""
        status, body = self.action(self.decide_body("allow"))
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "no permission request pending"})

    def test_an_unknown_decision_is_400(self):
        self.permissions.register(self.SID, "Bash", None, time.time())
        for bad in ("approve", "ask", "", None, True, "ALLOW", ["allow"]):
            status, body = self.action({"sessionId": self.SID, "action": "decide",
                                        "decision": bad})
            self.assertEqual(status, 400, bad)
            self.assertIn("error", json.loads(body))
        self.assertIsNotNone(self.permissions.pending(self.SID))

    def test_a_decide_with_no_decision_key_is_400(self):
        self.assertEqual(self.action({"sessionId": self.SID, "action": "decide"})[0], 400)

    # ---- the starvation property

    def test_a_held_permission_does_not_starve_the_rest_of_the_daemon(self):
        """The bounded-wait requirement, measured rather than reasoned about.

        With a request parked on the long poll, /v1/state, /v1/health, /v1/hook and the
        builder must all keep answering promptly - a companion that goes deaf for 55 s
        whenever a permission prompt appears would be worse than not having the feature.

        The busy hook is SubagentStop and not UserPromptSubmit since v0.19.0: the latter
        is one of PERMISSION_STALE_EVENTS and now legitimately retires the hold, which
        would make this test measure the new behaviour instead of the responsiveness it
        exists for. SubagentStop is the one hook that deliberately does neither.
        """
        self.enable()
        out, thread = self.fire()
        self.assertTrue(self.await_pending())
        started = time.time()
        for _ in range(5):
            self.assertIn("schema", self.state())
            self.assertTrue(self.client.get("/v1/health").json()["ok"])
            self.assertEqual(self.post_json("/v1/hook", {
                "session_id": self.SID, "hook_event_name": "SubagentStop",
                "cwd": "C:\\IT"})[0], 204)
        self.assertLess(time.time() - started, 5.0)
        # And the builder is still building underneath it.
        self.assertIn("generatedAt", self.rebuild())
        self.assertIsNotNone(self.permissions.pending(self.SID))
        self.action(self.decide_body("deny"))
        thread.join(timeout=10)
        self.assertTrue(out)

    # ---- v0.19.0: the operator answered the raced dialog in the app

    def test_a_stop_retires_a_hold_whose_dialog_was_answered_in_the_app(self):
        """The terminal dialog is RACED, not suppressed (docs/spikes/live-verify.md 3.3),
        so the tool can run and the turn can FINISH while crabd is still holding. Without
        this the card offers Approve / Deny for the rest of the 55 s on a decision that
        was made in the terminal - and a tap on it is a 404 at best."""
        self.enable()
        out, thread = self.fire()
        self.assertTrue(self.await_pending())
        self.assertEqual(self.post_json("/v1/hook/stop", {
            "session_id": self.SID, "hook_event_name": "Stop", "cwd": "C:\\IT"})[0], 200)
        thread.join(timeout=10)
        self.assertIsNone(self.permissions.pending(self.SID))
        self.assertEqual(json.loads(out[0][1]), {})     # still the pass-through
        self.assertIn("permission passed through: Bash", self.ring())

    def test_a_user_prompt_submit_retires_the_hold_too(self):
        self.enable()
        out, thread = self.fire()
        self.assertTrue(self.await_pending())
        self.assertEqual(self.post_json("/v1/hook", {
            "session_id": self.SID, "hook_event_name": "UserPromptSubmit",
            "cwd": "C:\\IT"})[0], 204)
        thread.join(timeout=10)
        self.assertIsNone(self.permissions.pending(self.SID))
        self.assertEqual(json.loads(out[0][1]), {})

    def test_a_hook_for_a_DIFFERENT_session_leaves_the_hold_parked(self):
        self.enable()
        _, thread = self.fire()
        self.assertTrue(self.await_pending())
        self.assertEqual(self.post_json("/v1/hook", {
            "session_id": "cafecafe-0000-0000-0000-000000000099",
            "hook_event_name": "UserPromptSubmit", "cwd": "C:\\IT"})[0], 204)
        self.assertIsNotNone(self.permissions.pending(self.SID))
        self.action(self.decide_body("deny"))
        thread.join(timeout=10)

    def test_a_subagent_stop_leaves_the_hold_parked(self):
        """Deliberately NOT a stale event: a background subagent finishing says nothing
        about whether the main thread's dialog was answered."""
        self.enable()
        _, thread = self.fire()
        self.assertTrue(self.await_pending())
        self.assertEqual(self.post_json("/v1/hook", {
            "session_id": self.SID, "hook_event_name": "SubagentStop",
            "cwd": "C:\\IT"})[0], 204)
        self.assertIsNotNone(self.permissions.pending(self.SID))
        self.action(self.decide_body("deny"))
        thread.join(timeout=10)

    def test_a_raise_inside_the_hold_cannot_strand_the_panel_row(self):
        """v0.19.0. register() puts the Approve / Deny buttons on the card BEFORE the
        wait, and _do_hook_permission swallows anything thrown in here into the
        pass-through - so a raise between those two points used to leave a panel-visible
        pendingPermission that only a LATER request for the same session could ever
        clear. Nothing bounded it: not the hold, not the expiry sweep."""
        self.enable()

        def boom(*_args, **_kwargs):
            raise RuntimeError("history write blew up")
        original = self.builder.note_session_event
        self.builder.note_session_event = boom
        self.addCleanup(lambda: setattr(self.builder, "note_session_event", original))
        status, body = self.post_json("/v1/hook/permission", self.hook_payload())
        self.assertEqual((status, json.loads(body)), (200, {}))   # fail-open, as always
        self.assertIsNone(self.permissions.pending(self.SID))
        self.assertEqual(self.permissions.count(), 0)

    def test_two_sessions_can_be_held_at_once(self):
        self.enable()
        crabd.PERMISSION_POLL_SEC = 3.0
        other = "9c9c9c9c-0000-0000-0000-000000000006"
        # v0.14.0: the hold is scoped to SERVED rows, so the second session has to be
        # one crabd is actually serving - a request naming a session with no row now
        # passes straight through to the terminal dialog instead of parking a thread
        # the panel could never show. A hook is enough to put it on a row.
        self.hooks.record({"session_id": other, "hook_event_name": "SessionStart",
                           "cwd": "C:\\IT"})
        self.rebuild()
        threads = [self.fire()[1], self.fire(session_id=other)[1]]
        self.assertTrue(self.await_pending())
        self.assertTrue(self.await_pending(other))
        self.assertEqual(self.permissions.count(), 2)
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(self.permissions.count(), 0)


# ================================================= v0.12.0: the expiry sweep

class PanelTokenEndpointTests(PermissionEndpointTests):
    """POST /v1/action decide behind the pairing code + requestId (SEC-a / WID-a closed).
    The fixture's hooks are the same as PermissionEndpointTests'; the helpers are
    borrowed rather than re-declared."""

    # Inherit the fixture (enable / fire / await_pending / hook_payload), NOT the parent's
    # tests: a None attribute is not callable, so the loader skips it.
    for _name in [n for n in dir(PermissionEndpointTests) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def _fire(self, **kw):
        return self.fire(**kw)

    def _enable(self):
        return self.enable()

    def _await(self):
        return self.await_pending()

    def test_state_serves_the_approvals_block_and_a_request_id(self):
        self._enable()
        out, thread = self._fire()
        self.assertTrue(self._await())
        doc = self.rebuild()
        self.assertEqual(doc["approvals"], {"enabled": True, "tokenRequired": True})
        rid = self.row()["pendingPermission"]["requestId"]
        self.assertRegex(rid, r"^[0-9a-f]{16}$")
        self.assertEqual(self.action(self.decide_body("deny"))[0], 204)
        thread.join(timeout=10)

    def test_approvals_block_reads_off_while_disabled(self):
        self.assertEqual(self.state()["approvals"], {"enabled": False, "tokenRequired": True})

    def test_a_decide_without_the_code_is_403_and_the_hold_is_untouched(self):
        self._enable()
        out, thread = self._fire()
        self.assertTrue(self._await())
        status, body = self.action(self.decide_body("allow", token=""))
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"error": "pairing code required"})
        self.assertIsNotNone(self.permissions.pending(self.SID))
        self.assertEqual(self.action(self.decide_body("deny"))[0], 204)
        thread.join(timeout=10)
        self.assertEqual(json.loads(out[0][1])["hookSpecificOutput"]["decision"]["behavior"],
                         "deny")

    def test_a_forged_null_origin_with_no_code_cannot_approve(self):
        """THE SEC-a reproduction, now refused: Origin null (what a sandboxed iframe
        sends) + the sessionId and requestId harvested off /v1/state is still not enough."""
        self._enable()
        out, thread = self._fire()
        self.assertTrue(self._await())
        self.rebuild()
        rid = self.row()["pendingPermission"]["requestId"]
        body = json.dumps({"sessionId": self.SID, "action": "decide",
                           "decision": "allow", "requestId": rid}).encode()
        reply = self.client.post("/v1/action", body, headers={"Origin": "null"})
        self.assertEqual(reply.status, 403)
        self.assertIsNotNone(self.permissions.pending(self.SID))
        crabd.PERMISSION_POLL_SEC = 0.4
        self.permissions.stale(self.SID)
        thread.join(timeout=10)
        self.assertEqual(json.loads(out[0][1]), {})     # pass-through, never an allow

    def test_a_wrong_code_is_403_and_counts_toward_the_lockout(self):
        self._enable()
        out, thread = self._fire()
        self.assertTrue(self._await())
        status, body = self.action(self.decide_body("allow", token="0000000000"))
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"error": "pairing code rejected"})
        self.assertEqual(self.client.get("/v1/health").json()["panelToken"]["rejectedRecently"], 1)
        self.assertEqual(self.action(self.decide_body("deny"))[0], 204)
        thread.join(timeout=10)

    def test_ten_wrong_codes_lock_the_gate_with_429(self):
        self._enable()
        out, thread = self._fire()
        self.assertTrue(self._await())
        for _ in range(crabd.PANEL_TOKEN_MAX_FAILURES):
            self.action(self.decide_body("allow", token="0000000000"))
        status, body = self.action(self.decide_body("allow"))
        self.assertEqual(status, 429)
        self.assertIsNotNone(self.permissions.pending(self.SID))
        self.assertIsNotNone(self.client.get("/v1/health").json()["panelToken"]["lockedUntil"])
        self.permissions.stale(self.SID)
        thread.join(timeout=10)

    def test_the_right_code_with_no_request_id_is_400(self):
        self._enable()
        out, thread = self._fire()
        self.assertTrue(self._await())
        body = self.decide_body("allow"); body.pop("requestId")
        status, reply = self.action(body)
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(reply), {"error": "requestId required"})
        self.assertIsNotNone(self.permissions.pending(self.SID))
        self.assertEqual(self.action(self.decide_body("deny"))[0], 204)
        thread.join(timeout=10)

    def test_a_stale_request_id_is_409_and_decides_nothing(self):
        """WID-a: a tap aimed at the request the sheet showed must not land on the one
        that replaced it in the poll gap."""
        self._enable()
        out, thread = self._fire()
        self.assertTrue(self._await())
        self.rebuild()
        old = self.row()["pendingPermission"]["requestId"]
        out2, thread2 = self._fire(tool="Write", tool_input={"file_path": "x"})
        settle(lambda: self.permissions.pending(self.SID)["requestId"] != old,
               what="the replacing request")
        thread.join(timeout=10)                       # the first hold passed through
        status, body = self.action(self.decide_body("allow", request_id=old))
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"error": "stale permission request"})
        self.assertIsNotNone(self.permissions.pending(self.SID))
        self.assertEqual(self.action(self.decide_body("deny"))[0], 204)
        thread2.join(timeout=10)
        self.assertEqual(json.loads(out2[0][1])["hookSpecificOutput"]["decision"]["behavior"],
                         "deny")

    def test_the_code_is_accepted_in_any_written_form(self):
        self._enable()
        out, thread = self._fire()
        self.assertTrue(self._await())
        self.assertEqual(self.action(self.decide_body("allow", token="k7qxm-2pdab"))[0], 204)
        thread.join(timeout=10)
        self.assertEqual(json.loads(out[0][1])["hookSpecificOutput"]["decision"]["behavior"],
                         "allow")

    def test_a_crabd_with_no_pairing_gate_answers_503_never_204(self):
        """Never fail open: a builder without a PanelToken cannot decide anything."""
        self._enable()
        out, thread = self._fire()
        self.assertTrue(self._await())
        self.builder.panel_token = None
        try:
            status, body = self.action(self.decide_body("allow"))
        finally:
            self.builder.panel_token = self.panel_token
        self.assertEqual(status, 503)
        self.assertIsNotNone(self.permissions.pending(self.SID))
        self.assertEqual(self.action(self.decide_body("deny"))[0], 204)
        thread.join(timeout=10)

    def test_health_reports_the_gate_without_the_code(self):
        health = self.client.get("/v1/health").json()
        self.assertEqual(health["panelToken"], {"present": True, "rejectedRecently": 0,
                                                "lockedUntil": None})
        self.assertNotIn(self.TOKEN, json.dumps(health))
        self.assertNotIn(self.TOKEN, json.dumps(self.state()))


class ExpiryLoopTests(unittest.TestCase):
    """_expiry_loop - the v0.12.0 stores age out on their own clock.

    Deliberately not one more line inside build(): a wedged builder is exactly the state
    in which crabd must NOT start delivering ten-minute-old prompts to sessions.
    """

    def run_loop(self, builder, seconds=0.2):
        stop = threading.Event()
        original = crabd.EXPIRY_POLL_SEC
        crabd.EXPIRY_POLL_SEC = 0.01
        thread = threading.Thread(target=crabd._expiry_loop, args=(builder, stop),
                                  daemon=True)
        try:
            thread.start()
            time.sleep(seconds)
            alive = thread.is_alive()
        finally:
            stop.set()
            crabd.EXPIRY_POLL_SEC = original
        thread.join(timeout=5)
        return alive

    def test_one_pass_expires_a_continue_and_drops_a_stale_cost_day(self):
        builder = crabd.StateBuilder(
            crabd.TranscriptStore(Path(tempfile.gettempdir()) / "crabd-nope"),
            crabd.HookTracker(), StubLimits(), time.time(),
            continues=crabd.ContinueQueue(), otlp=crabd.OtlpReceiver(),
            statusline=crabd.StatusLineReader())
        now = time.time()
        builder.continues.queue("s", "Continue", now - crabd.CONTINUE_TTL_SEC - 1)
        builder.otlp.ingest_metrics(
            otlp_metrics([otlp_point(5.0, now - 5 * 86400)]), now)
        builder.statusline.ingest(
            statusline_doc(session_id="s", context=context_window()),
            now - crabd.STATUSLINE_SESSION_KEEP_SEC - 1)

        self.run_loop(builder)

        self.assertEqual(builder.continues.pending(now), 0)
        self.assertEqual(builder.otlp._delta_by_day, {})
        self.assertEqual(builder.statusline._sessions, {})

    def test_the_loop_survives_a_reader_that_raises(self):
        class Exploding:
            def prune(self, now):
                raise RuntimeError("boom")

        builder = crabd.StateBuilder(
            crabd.TranscriptStore(Path(tempfile.gettempdir()) / "crabd-nope"),
            crabd.HookTracker(), StubLimits(), time.time(), continues=Exploding())
        # The loop's own "crabd: expiry error" line is the behaviour under test, so it
        # is swallowed rather than printed across an otherwise quiet suite run.
        original, sys.stderr = sys.stderr, io.StringIO()
        try:
            alive = self.run_loop(builder, seconds=0.1)
            noise = sys.stderr.getvalue()
        finally:
            sys.stderr = original
        self.assertTrue(alive)
        self.assertIn("expiry error: RuntimeError", noise)


# =====================================================================================
# v0.21.0 - the crabd lane of the 2026-08-27 finding-verification wave.
#
# Every test below either PINS a fix or PINS a refutation. The two refuted findings get
# tests too, and for the reason a refutation without one is worthless: "the code already
# handles it" is a claim about today's code, and the next edit is free to un-handle it.
# =====================================================================================

class SecondQuestionIsNotClearedByTheFirstsActivityTests(unittest.TestCase):
    """CD-01, REFUTED at 291978d - and pinned here as the exact replay the finding
    describes, not as a re-statement of the re-fire rule.

    The finding: a second Notification arriving during needs_input keeps the FIRST
    question's timestamp, so transcript activity belonging to question A clears
    question B as though the operator had answered it. Both halves have to hold for
    that to happen, and 291978d broke the first: a re-fire carrying a DIFFERENT
    question now moves `since`, which puts it ahead of A's activity and out of reach
    of note_activity's grace.
    """

    def hook(self, tracker, event, sid, **extra):
        tracker.record({"session_id": sid, "hook_event_name": event, **extra})

    def setUp(self):
        self.tracker = crabd.HookTracker()
        self.hook(self.tracker, "UserPromptSubmit", "s1")
        self.hook(self.tracker, "Notification", "s1", message="Question A")
        self.since_a = self.tracker.snapshot()["s1"]["since"]
        time.sleep(0.01)
        self.hook(self.tracker, "Notification", "s1", message="Question B")
        self.row = self.tracker.snapshot()["s1"]

    def test_the_second_question_takes_its_own_timestamp(self):
        self.assertGreater(self.row["since"], self.since_a)
        self.assertEqual(self.row["question"], "Question B")
        self.assertFalse(self.row["acked"])

    def test_activity_from_the_first_questions_turn_cannot_clear_the_second(self):
        """THE FINDING ITSELF. A round-trip that finished while question A was standing
        - a late transcript flush, say - is offered as activity. It must not clear B."""
        cleared = self.tracker.note_activity("s1", self.since_a + 0.001)
        self.assertFalse(cleared)
        self.assertEqual(self.tracker.snapshot()["s1"]["state"], "needs_input")
        self.assertEqual(self.tracker.snapshot()["s1"]["question"], "Question B")

    def test_a_genuine_answer_to_the_second_question_still_clears_it(self):
        """The other half: the guard must not have bought the fix by refusing every
        clear. A round-trip past B's own `since` is a real answer."""
        at = self.row["since"] + crabd.NEEDS_INPUT_ACTIVITY_GRACE_SEC + 1
        self.assertTrue(self.tracker.note_activity("s1", at))
        self.assertEqual(self.tracker.snapshot()["s1"]["state"], "working")


class ContinuationTurnShowsItsOwnStopTests(unittest.TestCase):
    """CD-06 (v0.21.0). A session that finished, resumed and finished again must read
    `done` on the second finish - it read `working` until it aged out of the window."""

    def hook(self, tracker, event, sid, **extra):
        tracker.record({"session_id": sid, "hook_event_name": event, **extra})

    def resolve(self, row, mtime, now):
        return crabd.StateBuilder._resolve(row, mtime, mtime, now)[0]

    def test_a_second_stop_refreshes_state_since(self):
        tracker = crabd.HookTracker()
        self.hook(tracker, "Stop", "s1")
        first = tracker.snapshot()["s1"]["since"]
        time.sleep(0.01)
        self.hook(tracker, "Stop", "s1")
        self.assertGreater(tracker.snapshot()["s1"]["since"], first)

    def test_the_continuation_turns_stop_reaches_the_card_as_done(self):
        """THE WHOLE REPLAY, through _resolve, which is where the frozen `since` did its
        damage: every transcript write of the continuation turn sat after the FIRST
        Stop's `since`, so the card read `working` through the second turn and past its
        end."""
        tracker = crabd.HookTracker()
        self.hook(tracker, "Stop", "s1")
        row = tracker.snapshot()["s1"]
        since = row["since"]
        # 1. the first turn ends: its own flush is inside the grace, so `done`.
        self.assertEqual(self.resolve(row, since + 1, since + 2), "done")
        # 2. work resumes - a transcript write well past the grace is a reactivation
        #    (offsets sit past DONE_REACTIVATION_GRACE_SEC, 120 since v0.28.2).
        moved = since + crabd.DONE_REACTIVATION_GRACE_SEC + 60
        self.assertEqual(self.resolve(row, moved, moved + 1), "working")
        # 3. the continuation turn ends. THIS is what used to stay `working` forever.
        self.hook(tracker, "Stop", "s1")
        row = tracker.snapshot()["s1"]
        self.assertEqual(self.resolve(row, row["since"] + 1, row["since"] + 2), "done")

    def test_a_repeated_stop_re_dates_the_row_without_touching_the_ledger(self):
        """THE SPLIT that makes the fix safe, and the reason `entered` exists beside
        `moved`. Re-dating is what CD-06 needs; the done LEDGER must not follow, or a
        session that stops five times writes five done lines into history. Pinned from
        both sides here, because a fix that moved them together passed the test above
        and broke two long-standing pins (HookHistoryWriteTests, RecapDoneTodayTests)."""
        tracker = crabd.HookTracker()
        self.hook(tracker, "Stop", "s1")
        first = tracker.snapshot()["s1"]["since"]
        for _ in range(4):
            time.sleep(0.002)
            self.hook(tracker, "Stop", "s1")
        self.assertGreater(tracker.snapshot()["s1"]["since"], first)
        self.assertEqual(len(tracker.dones), 1)
        self.assertEqual(tracker.done_today(), 1)
        self.assertEqual(tracker.done_by_day()[-1][1], 1)

    def test_a_stop_still_clears_the_question_and_the_ack(self):
        tracker = crabd.HookTracker()
        self.hook(tracker, "Notification", "s1", message="Approve?")
        tracker.ack("s1")
        self.hook(tracker, "Stop", "s1")
        row = tracker.snapshot()["s1"]
        self.assertEqual(row["state"], "done")
        self.assertIsNone(row["question"])
        self.assertFalse(row["acked"])


class ReplayRestoresTerminalStateTests(HistoryTempFile):
    """CD-07 (v0.21.0). A restart replayed rows with state=None, and _resolve's fallback
    for a row it has no state for is `working` - so every session that had FINISHED
    before the restart came back claiming a live turn."""

    SID = "cccc3333-0000-0000-0000-00000000000c"

    def replayed(self, kinds, age=300.0):
        now = time.time()
        entries = [(now - age + i, kind, self.SID, "a title")
                   for i, kind in enumerate(kinds)]
        tracker = crabd.HookTracker()
        tracker.replay(entries)
        return tracker, tracker.snapshot()[self.SID], now

    def test_a_replayed_finished_session_is_not_working(self):
        tracker, row, now = self.replayed(
            ["session started", "prompt submitted", "turn finished"])
        self.assertEqual(row["state"], "done")
        self.assertEqual(crabd.StateBuilder._resolve(row, 0.0, row["at"], now)[0], "done")

    def test_a_replayed_ended_session_is_gone(self):
        tracker, row, now = self.replayed(["prompt submitted", "session ended"])
        self.assertEqual(row["state"], "gone")
        self.assertEqual(crabd.StateBuilder._resolve(row, 0.0, row["at"], now)[0], "gone")

    def test_a_replayed_finished_session_ages_out_on_its_own_schedule(self):
        """Restoring `done` is not "keep it forever": _resolve retires it at
        DONE_DROP_SEC exactly as a live one."""
        tracker, row, now = self.replayed(
            ["turn finished"], age=crabd.DONE_DROP_SEC + 60)
        self.assertEqual(crabd.StateBuilder._resolve(row, 0.0, row["at"], now)[0], "gone")

    def test_a_turn_that_restarted_after_finishing_is_not_restored_as_done(self):
        """The undo arm. A session that finished and was then picked up again ends its
        ring on a prompt, and that later event has to win - a restored `done` on a
        session mid-turn would drop the card off the panel ten minutes later."""
        tracker, row, now = self.replayed(
            ["turn finished", "prompt submitted"])
        self.assertIsNone(row["state"])
        self.assertEqual(crabd.StateBuilder._resolve(row, 0.0, row["at"], now)[0],
                         "working")

    def test_a_replayed_question_is_never_restored_as_needs_input(self):
        """Deliberate non-action. The history file holds no question text, and
        needs_input is the one state _resolve never ages away - a restored one would
        alert forever with nothing to say."""
        tracker, row, now = self.replayed(["asked a question"])
        self.assertIsNone(row["state"])

    def test_a_real_restart_over_the_file_restores_done(self):
        """End to end over a real HistoryLog, not hand-built tuples."""
        log = self.log()
        live = crabd.HookTracker(history=log)
        live.record({"session_id": self.SID, "hook_event_name": "UserPromptSubmit"})
        live.record({"session_id": self.SID, "hook_event_name": "Stop"})
        restarted = crabd.HookTracker(history=log)
        restarted.replay(log.replay())
        self.assertEqual(restarted.snapshot()[self.SID]["state"], "done")


class MalformedRecordKeepsTheTailTests(TempProjects):
    """CD-08, tail-loss half REFUTED at 291978d (the per-record catch in _consume).

    The offset DOES advance to EOF before any record is parsed - that part of the
    finding is a correct reading of refresh(). What does not follow is tail loss: the
    whole chunk is already in memory and split into lines, and the catch is INSIDE the
    per-line loop, so a throwing record costs its own line and nothing behind it.
    Pinned because the fragile thing is the catch's POSITION, which a refactor could
    move out of the loop without any other test noticing.
    """

    def facts_over(self, objects, poison=None):
        path = self.session_path("s-tail")
        write_jsonl(path, objects)
        facts = crabd.FileFacts(path, "s-tail", False)
        if poison is None:
            facts.refresh()
            return facts
        original = crabd.FileFacts._consume_record

        def throwing(self, raw):
            if poison in raw:
                raise RuntimeError("a shape nobody has met yet")
            return original(self, raw)

        crabd.FileFacts._consume_record = throwing
        self.addCleanup(setattr, crabd.FileFacts, "_consume_record", original)
        stderr, sys.stderr = sys.stderr, io.StringIO()
        try:
            facts.refresh()
        finally:
            sys.stderr = stderr
        return facts

    def test_a_throwing_record_loses_only_itself(self):
        now = time.time()
        facts = self.facts_over(
            [assistant_line("req_a", now - 30),
             {"type": "assistant", "poison": True},
             assistant_line("req_b", now - 10)],
            poison=b'"poison"')
        self.assertEqual(sorted(facts.requests), ["req_a", "req_b"])
        self.assertEqual(facts.skipped, 1)

    def test_a_measured_crasher_shape_loses_only_itself(self):
        """The same property against a real one of the ten shapes measured on
        2026-08-27 - an Infinity usage counter - which _as_count now absorbs entirely."""
        now = time.time()
        bad = assistant_line("req_bad", now - 20)
        bad["message"]["usage"]["output_tokens"] = float("inf")
        facts = self.facts_over(
            [assistant_line("req_a", now - 30), bad, assistant_line("req_b", now - 10)])
        self.assertEqual(sorted(facts.requests), ["req_a", "req_b", "req_bad"])
        self.assertEqual(facts.requests["req_bad"][1], 0)
        self.assertEqual(facts.skipped, 0)

    def test_the_tail_is_read_on_the_same_pass_not_a_later_one(self):
        """The finding's "loses the tail forever" wording. The offset is already at EOF,
        so if the tail were not consumed on THIS pass nothing would ever go back for it -
        a second refresh with no new bytes returns False and parses nothing."""
        now = time.time()
        facts = self.facts_over(
            [{"type": "assistant", "poison": True}, assistant_line("req_b", now - 10)],
            poison=b'"poison"')
        self.assertIn("req_b", facts.requests)
        self.assertFalse(facts.refresh())


class RetentionIsBoundedTests(TempProjects):
    """CD-09 (v0.21.0). crabd runs for weeks; nothing may be resident for its lifetime."""

    def test_a_transcript_that_ages_out_of_the_window_is_evicted(self):
        path = self.session_path("s-aged")
        write_jsonl(path, [assistant_line("req_a", time.time() - 100)])
        store = crabd.TranscriptStore(self.projects)
        store.scan(time.time())
        self.assertEqual(len(store.files), 1)
        ancient = time.time() - crabd.TRANSCRIPT_WINDOW_SEC - 86400
        os.utime(path, (ancient, ancient))
        store.scan(time.time())
        self.assertEqual(store.files, {})

    def test_a_transcript_inside_the_window_is_kept(self):
        """The mutation guard: an eviction rule that dropped everything would pass the
        test above and take the product with it."""
        path = self.session_path("s-fresh")
        write_jsonl(path, [assistant_line("req_a", time.time() - 100)])
        store = crabd.TranscriptStore(self.projects)
        store.scan(time.time())
        store.scan(time.time())
        self.assertEqual(len(store.files), 1)

    def test_an_unread_file_is_not_churned_out_of_the_store(self):
        """mtime 0.0 means "never successfully refreshed", not "ancient". Evicting it
        would re-admit and re-parse the file whole on every pass."""
        store = crabd.TranscriptStore(self.projects)
        path = self.session_path("s-unread")
        write_jsonl(path, [assistant_line("req_a", time.time() - 100)])
        store.scan(time.time())
        store.files[str(path)].mtime = 0.0
        store.scan(time.time())
        self.assertIn(str(path), store.files)

    def _row(self, tracker, sid, state, at):
        row = crabd.HookTracker._blank(at)
        row["state"] = state
        tracker.sessions[sid] = row
        tracker._titles[sid] = "a title"

    def test_abandoned_working_and_done_rows_are_pruned(self):
        """The ordinary end of a session with no SessionEnd hook - a closed terminal.
        Both were resident for the life of the daemon."""
        tracker = crabd.HookTracker()
        now = time.time()
        stale = now - crabd.GONE_AFTER_SEC - 60
        for sid, state in (("w", "working"), ("d", "done"), ("g", "gone"), ("n", None)):
            self._row(tracker, sid, state, stale)
        tracker.prune(now)
        self.assertEqual(tracker.sessions, {})
        self.assertEqual(tracker._titles, {})

    def test_a_waiting_question_is_never_pruned(self):
        """The contract's exemption, and the mutation guard for the rule above: a
        question keeps waiting even when everything else has gone quiet."""
        tracker = crabd.HookTracker()
        now = time.time()
        self._row(tracker, "q", "needs_input", now - crabd.GONE_AFTER_SEC - 3600)
        tracker.prune(now)
        self.assertIn("q", tracker.sessions)

    def test_a_live_row_survives_prune(self):
        tracker = crabd.HookTracker()
        now = time.time()
        self._row(tracker, "live", "working", now - 60)
        tracker.prune(now)
        self.assertIn("live", tracker.sessions)


class NonFiniteNumbersAreRefusedAtTheBoundaryTests(unittest.TestCase):
    """CD-10 (v0.21.0). `json.loads("1e309")` is `inf` from JSON that parses cleanly."""

    INF = float("inf")
    NAN = float("nan")

    def test_json_really_does_produce_infinity(self):
        """The premise, measured rather than asserted - the finding turns on a
        hand-edited value being VALID JSON, not on a malformed file."""
        self.assertTrue(math.isinf(json.loads("1e309")))

    def test_finite_number_refuses_bool_and_non_finite(self):
        self.assertIsNone(crabd._finite_number(True))
        self.assertIsNone(crabd._finite_number(False))
        self.assertIsNone(crabd._finite_number(self.INF))
        self.assertIsNone(crabd._finite_number(-self.INF))
        self.assertIsNone(crabd._finite_number(self.NAN))
        self.assertIsNone(crabd._finite_number("12"))
        self.assertEqual(crabd._finite_number(12), 12.0)
        self.assertEqual(crabd._finite_number(0), 0.0)

    def test_an_infinite_toast_threshold_does_not_stop_the_build(self):
        """THE FINDING. toast_block runs on every build, so this OverflowError stopped
        every state refresh - an empty document at startup, a frozen snapshot after."""
        self.assertIsNone(crabd._toast_seconds(self.INF))
        self.assertEqual(crabd.toast_block({"toast": {"thresholdSec": self.INF}}),
                         {"thresholdSec": crabd.CONFIG_TOAST_DEFAULT_SEC,
                          "enabled": crabd.CONFIG_TOAST_DEFAULT_ENABLED})

    def test_a_build_survives_an_infinite_threshold_end_to_end(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        projects = Path(tmp.name) / "projects"
        projects.mkdir()
        config = Path(tmp.name) / "config.json"
        config.write_text('{"toast": {"thresholdSec": 1e309}}', encoding="utf-8")
        original, crabd.USER_CONFIG_FILE = crabd.USER_CONFIG_FILE, config
        self.addCleanup(setattr, crabd, "USER_CONFIG_FILE", original)
        builder = crabd.StateBuilder(crabd.TranscriptStore(projects),
                                     crabd.HookTracker(), StubLimits(), time.time())
        state = builder.build()
        self.assertEqual(state["toast"]["thresholdSec"], crabd.CONFIG_TOAST_DEFAULT_SEC)
        self.assertTrue(crabd.dump_state(state))

    def test_a_true_utilization_is_not_a_full_gauge(self):
        """The 0.20.0 leftover. `isinstance(x, (int, float))` passes a bool, and
        float(True) is 1.0 - so `utilization: true` rendered the week as spent."""
        self.assertIsNone(crabd.LimitsReader._window({"utilization": True}))
        self.assertIsNone(crabd.LimitsReader._window({"utilization": False}))
        self.assertEqual(
            crabd.LimitsReader._window({"utilization": 0.5})["utilization"], 0.5)

    def test_non_finite_utilization_is_refused_not_clamped(self):
        """Clamping is total - max/min turn NaN into 0.0 and inf into 1.0 - and that is
        the problem: both render as a real reading of this operator's week."""
        for value in (self.INF, -self.INF, self.NAN):
            self.assertIsNone(crabd.LimitsReader._window({"utilization": value}))
            self.assertIsNone(
                crabd.StatusLineReader._window({"used_percentage": value}))

    def test_a_scoped_weekly_percent_is_held_to_the_same_rule(self):
        payload = {"limits": [{"kind": "weekly_scoped", "percent": True},
                              {"kind": "weekly_scoped", "percent": self.NAN}]}
        self.assertEqual(
            crabd.LimitsReader.map_payload(payload, None, None)["extra"], [])

    def test_an_infinite_context_window_is_unknown_not_a_crash(self):
        """StatusLineReader.ingest promises never to raise - it runs behind a 204 that
        has already gone out - and int(inf) was the one line in it that could."""
        self.assertIsNone(crabd.StatusLineReader._context_tokens(
            {"total_input_tokens": self.INF, "current_usage": {}}))
        reader = crabd.StatusLineReader()
        reader.ingest({"session_id": "s1",
                       "context_window": {"total_input_tokens": self.INF,
                                          "current_usage": {}}}, time.time())
        # (True, None) and not (False, None): the document DID describe this session,
        # and "the status line says the context is unknown" is the honest reading of a
        # field crabd cannot use. Only "never mentioned" falls back to the transcript.
        self.assertEqual(reader.context("s1", time.time()), (True, None))

    def test_the_history_endpoint_serves_through_the_one_serializer(self):
        """A bare json.dumps emits bare NaN/Infinity, which no JSON parser accepts."""
        body = {"day": "2026-08-27", "events": [{"at": "x", "n": self.NAN}],
                "count": 1, "truncated": False}
        self.assertEqual(json.loads(crabd.dump_state(body))["events"][0]["n"], None)


class RecapCountsAgreeTests(TempProjects):
    """CD-11 (v0.21.0). doneToday and sessionsToday came from two sources that never
    met; reproduced 2026-08-27 as sessionsToday=0 beside doneToday=1."""

    def inputs(self, tracker, now=None):
        now = now or time.time()
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects), tracker,
                                     StubLimits(), now)
        builder.build(now=now)
        sessions, done, _repos, _week = builder._recap_inputs(tracker.snapshot(), now)
        return sessions, done

    def test_a_hook_only_session_counts_toward_both(self):
        """THE FINDING: a session crabd holds no transcript for - one older than the
        window, or under a projects dir it cannot read."""
        tracker = crabd.HookTracker()
        tracker.record({"session_id": "hook-only", "hook_event_name": "UserPromptSubmit"})
        tracker.record({"session_id": "hook-only", "hook_event_name": "Stop"})
        sessions, done = self.inputs(tracker)
        self.assertEqual((sessions, done), (1, 1))

    def test_done_today_can_never_exceed_sessions_today(self):
        """The invariant, over a session whose hook row has been PRUNED away - which is
        why the union has to reach `dones` and not only the live rows."""
        tracker = crabd.HookTracker()
        now = time.time()
        tracker.dones.append((now - 60, "long-gone"))
        sessions, done = self.inputs(tracker, now)
        self.assertGreaterEqual(sessions, done)
        self.assertEqual(done, 1)

    def test_a_hook_only_session_that_has_not_finished_still_counts(self):
        """The other half of the union, and the one `dones` cannot supply: a session
        crabd has a hook row for and no transcript, still mid-turn. It is a session that
        happened today whether or not it ever reaches a Stop."""
        tracker = crabd.HookTracker()
        tracker.record({"session_id": "still-going",
                        "hook_event_name": "UserPromptSubmit"})
        sessions, done = self.inputs(tracker)
        self.assertEqual((sessions, done), (1, 0))

    def test_yesterdays_hook_row_does_not_count_today(self):
        """The mutation guard for the line above: the union is scoped to TODAY, or a
        long-lived row would inflate every following day's sessionsToday."""
        tracker = crabd.HookTracker()
        now = time.time()
        row = crabd.HookTracker._blank(crabd._local_midnight(now) - 3600)
        row["state"] = "working"
        tracker.sessions["yesterday"] = row
        sessions, done = self.inputs(tracker, now)
        self.assertEqual((sessions, done), (0, 0))

    def test_a_transcript_session_is_still_counted(self):
        """The mutation guard: the union must not have replaced the transcript half."""
        write_jsonl(self.session_path("s-live"),
                    [assistant_line("req_a", time.time() - 60)])
        sessions, done = self.inputs(crabd.HookTracker())
        self.assertEqual((sessions, done), (1, 0))

    def test_one_session_seen_both_ways_is_counted_once(self):
        sid = "s-both"
        write_jsonl(self.session_path(sid), [assistant_line("req_a", time.time() - 60)])
        tracker = crabd.HookTracker()
        tracker.record({"session_id": sid, "hook_event_name": "Stop"})
        sessions, done = self.inputs(tracker)
        self.assertEqual((sessions, done), (1, 1))


class QuestionBelongsToThisTurnTests(unittest.TestCase):
    """CD-28 (v0.21.0). QUESTION_FRESH_SEC is a 120 s lookback, and a whole turn fits
    inside it - so a richer question from the PREVIOUS turn replaced the notification
    actually on screen."""

    def question(self, enriched_ts, turn_started, since=5050.0,
                 enriched="a much longer question from another turn"):
        hook = {"question": "Approve?", "turn_started": turn_started}
        info = {"question": enriched, "question_ts": enriched_ts}
        return crabd.StateBuilder._question("needs_input", hook, info, since)

    def test_a_question_from_before_this_turn_is_refused(self):
        """THE FINDING: written 10 s before the prompt that started this turn, so well
        inside the 120 s lookback that used to be the only test."""
        self.assertEqual(self.question(4990.0, 5000.0), "Approve?")

    def test_this_turns_own_question_still_enriches(self):
        """The mutation guard. The enrichment is the feature; a fix that refused
        everything would pass the test above and blank the sheet."""
        self.assertEqual(self.question(5040.0, 5000.0),
                         "a much longer question from another turn")

    def test_the_grace_forgives_hook_latency_at_the_turn_boundary(self):
        """A question timestamped a hair before the UserPromptSubmit hook's receipt
        time is this turn's - the two are different clocks."""
        self.assertEqual(self.question(5000.0 - crabd.QUESTION_TURN_GRACE_SEC + 1,
                                       5000.0),
                         "a much longer question from another turn")

    def test_without_a_turn_started_the_window_is_still_the_test(self):
        """Every session already running when crabd started has no UserPromptSubmit."""
        self.assertEqual(self.question(4990.0, None),
                         "a much longer question from another turn")
        self.assertEqual(self.question(5050.0 - crabd.QUESTION_FRESH_SEC - 1, None),
                         "Approve?")

    def test_a_shorter_transcript_question_never_wins(self):
        self.assertEqual(self.question(5040.0, 5000.0, enriched="Hi?"), "Approve?")


class SubagentDetailNamesOnlyRunningAgentsTests(unittest.TestCase):
    """CD-29 (v0.21.0). A subagent that has just stopped has the NEWEST mtime of all of
    them, so the newest-first trim named the one agent crabd knew had finished."""

    class Fake:
        def __init__(self, mtime, name):
            self.mtime = mtime
            self._name = name

        def agent_id(self):
            return self._name

        def label(self):
            return self._name

    NOW = 10000.0

    def detail(self, files, running, stops):
        info = {"sub_files": [self.Fake(mt, name) for mt, name in files],
                "agent_labels": {}}
        return [row["label"] for row
                in crabd.StateBuilder._subagent_detail(info, running, self.NOW, stops)]

    def test_the_stopped_agent_is_not_the_one_named(self):
        """THE FINDING."""
        self.assertEqual(
            self.detail([(self.NOW - 50, "still-running"), (self.NOW - 1, "stopped")],
                        1, (self.NOW - 1,)),
            ["still-running"])

    def test_with_no_stops_the_newest_still_wins(self):
        """The mutation guard: the ordering rule is unchanged for the ordinary case."""
        self.assertEqual(
            self.detail([(self.NOW - 50, "older"), (self.NOW - 1, "newer")], 2, ()),
            ["newer", "older"])

    def test_a_stop_cannot_claim_a_file_still_being_written(self):
        """A file whose last write is well AFTER the stop belongs to another agent -
        matching it would hide a live subagent behind a stop that was not its own."""
        self.assertEqual(
            self.detail([(self.NOW - 1, "still-running")], 1,
                        (self.NOW - crabd.SUBAGENT_STOP_MATCH_SEC - 60,)),
            ["still-running"])

    def test_two_stops_retire_two_files(self):
        self.assertEqual(
            self.detail([(self.NOW - 80, "running"), (self.NOW - 3, "stopped-b"),
                         (self.NOW - 1, "stopped-a")],
                        1, (self.NOW - 3, self.NOW - 1)),
            ["running"])

    def test_nothing_running_shows_nothing(self):
        self.assertEqual(self.detail([(self.NOW - 1, "stopped")], 0, (self.NOW - 1,)), [])


class QueuedContinueReplacementSurvivesTests(unittest.TestCase):
    """CD-30 (v0.21.0). The Stop handler is peek -> send -> drain on purpose (CRB-F5);
    a prompt queued in that gap was deleted undelivered."""

    SID = "s1"

    def setUp(self):
        self.queue = crabd.ContinueQueue()
        self.now = time.time()

    def test_a_replacement_queued_mid_delivery_is_kept(self):
        """THE FINDING."""
        self.queue.queue(self.SID, "Continue", self.now)
        sent = self.queue.peek(self.SID, self.now)
        self.queue.queue(self.SID, "Run the tests", self.now + 0.1)
        self.assertIsNone(self.queue.drain_if(self.SID, sent, self.now + 0.2))
        self.assertEqual(self.queue.entry(self.SID, self.now + 0.3)["prompt"],
                         "Run the tests")

    def test_the_delivered_prompt_is_still_spent(self):
        """The mutation guard: a drain_if that never drained would deliver the same
        prompt on every Stop forever."""
        self.queue.queue(self.SID, "Continue", self.now)
        sent = self.queue.peek(self.SID, self.now)
        self.assertEqual(self.queue.drain_if(self.SID, sent, self.now), "Continue")
        self.assertIsNone(self.queue.entry(self.SID, self.now))

    def test_an_expired_entry_is_removed_and_not_returned(self):
        self.queue.queue(self.SID, "Continue", self.now)
        late = self.now + crabd.CONTINUE_TTL_SEC + 1
        self.assertIsNone(self.queue.drain_if(self.SID, "Continue", late))
        self.assertIsNone(self.queue.entry(self.SID, late))

    def test_an_unknown_session_is_a_no_op(self):
        self.assertIsNone(self.queue.drain_if("nobody", "Continue", self.now))


class StatusLineContextLosesWhenStaleTests(TempProjects):
    """CD-36 (v0.21.0). Status-line rows live STATUSLINE_SESSION_KEEP_SEC - two hours -
    and until now that retention alone won, with nothing comparing it against the other
    source. Reproduced 2026-08-27: a retained 150000 overrode a newer transcript 30000."""

    SID = "s1"

    def context(self, statusline_age, transcript_age, now=None):
        now = now or time.time()
        reader = crabd.StatusLineReader()
        reader.ingest({"session_id": self.SID,
                       "context_window": {"total_input_tokens": 150000,
                                          "current_usage": {"input_tokens": 1}}},
                      now - statusline_age)
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), now,
                                     statusline=reader)
        info = {"context_tokens": 30000, "context_ts": now - transcript_age}
        return builder._context(self.SID, info, now)

    def test_a_stale_status_line_loses_to_a_newer_transcript(self):
        """THE FINDING."""
        self.assertEqual(self.context(statusline_age=7000, transcript_age=30),
                         {"contextTokens": 30000, "contextSource": "transcript"})

    def test_a_live_status_line_still_wins(self):
        """The mutation guard, and the whole reason the precedence exists: after a
        compaction the two sources disagree by a whole window, and the status line is
        the one reading the live session."""
        self.assertEqual(self.context(statusline_age=1, transcript_age=30),
                         {"contextTokens": 150000, "contextSource": "statusline"})

    def test_a_status_line_behind_by_less_than_the_lead_still_wins(self):
        """The clock-skew allowance. `context_ts` is the CLI's record timestamp and the
        status line's is crabd's receipt clock; a live pair straddles zero."""
        self.assertEqual(
            self.context(statusline_age=crabd.CONTEXT_STATUSLINE_LEAD_SEC - 5,
                         transcript_age=0),
            {"contextTokens": 150000, "contextSource": "statusline"})

    def test_a_session_with_no_transcript_reading_still_takes_the_status_line(self):
        """context_ts 0.0 is "no usage record parsed yet", not "an ancient reading" -
        the status line is the only source there is."""
        now = time.time()
        reader = crabd.StatusLineReader()
        reader.ingest({"session_id": self.SID,
                       "context_window": {"total_input_tokens": 150000,
                                          "current_usage": {"input_tokens": 1}}},
                      now - 60)
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), now,
                                     statusline=reader)
        self.assertEqual(
            builder._context(self.SID, {"context_tokens": None, "context_ts": 0.0}, now),
            {"contextTokens": 150000, "contextSource": "statusline"})

    def test_the_reader_still_reports_a_fresh_row_without_a_floor(self):
        """`not_before` defaults off, so every other caller is unchanged."""
        now = time.time()
        reader = crabd.StatusLineReader()
        reader.ingest({"session_id": self.SID,
                       "context_window": {"total_input_tokens": 42000,
                                          "current_usage": {"input_tokens": 1}}}, now)
        self.assertEqual(reader.context(self.SID, now), (True, 42000))
        self.assertEqual(reader.context(self.SID, now, now + 60), (False, None))


# =====================================================================================
# v0.22.0 - `host`: this machine's CPU and memory, for the panel beside the iCUE
# temperature sensors. Additive, so `schema` stays 5 and presence is the detection.
#
# The CPU half is the only arithmetic in crabd that is WRONG IN A PLAUSIBLE-LOOKING WAY
# when you get it wrong: GetSystemTimes' kernel time INCLUDES idle time, so the naive
# readings of the same three counters produce 62.5% and 100% where the truth is 40%.
# Every one of those three numbers is a percentage an operator would believe. That is
# why the math is pinned against scripted FILETIMEs rather than sampled off the host,
# and why the two naive answers are named in the assertions - a test that only says
# "40.0" would still pass if someone re-derived it from a different wrong formula.
# =====================================================================================

_GB = 1024 ** 3
# 32 GiB installed, 18.6 GiB in use - the shape the contract's example carries.
_MEM_TOTAL = 32 * _GB
_MEM_USED = round(18.6 * _GB)
_MEM_READING = (_MEM_TOTAL, _MEM_TOTAL - _MEM_USED)
_TICKS_PER_SEC = 10_000_000        # a FILETIME counts 100 ns units


class ScriptedCounters:
    """Stand-ins for HostSampler's two kernel reads, driven off a list.

    Each call takes the next entry and THE LAST ENTRY STICKS, so a test that cares
    about one reading does not have to supply one per call. A `None` entry is a read
    that failed, which is how the honest-failure paths are reached.
    """

    def __init__(self, times=(), memory=(_MEM_READING,)):
        self.times = list(times)
        self.memory = list(memory)
        self.time_calls = 0
        self.memory_calls = 0

    @staticmethod
    def _next(queue):
        if not queue:
            return None
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def read_times(self):
        self.time_calls += 1
        return self._next(self.times)

    def read_memory(self):
        self.memory_calls += 1
        return self._next(self.memory)


def scripted_sampler(times=(), memory=(_MEM_READING,)):
    counters = ScriptedCounters(times, memory)
    sampler = crabd.HostSampler(times=counters.read_times, memory=counters.read_memory)
    sampler.counters = counters
    return sampler


class HostSamplerCpuMathTests(unittest.TestCase):
    """The FILETIME delta arithmetic, against counters this test writes itself."""

    # One second of wall clock on a box that spent it 40% busy, expressed the way
    # GetSystemTimes expresses it: kernel time is 4.0 s and CONTAINS the 3.0 s of idle.
    #   idle   3.0 s
    #   kernel 4.0 s  (of which 3.0 s is that same idle)
    #   user   1.0 s
    # busy = (kernel + user) - idle = 5.0 - 3.0 = 2.0 s out of a 5.0 s core-second
    # budget -> 40.0%.
    BASE = (100 * _TICKS_PER_SEC, 400 * _TICKS_PER_SEC, 90 * _TICKS_PER_SEC)
    STEP = (3 * _TICKS_PER_SEC, 4 * _TICKS_PER_SEC, 1 * _TICKS_PER_SEC)

    @classmethod
    def _advanced(cls, base, step):
        return tuple(b + s for b, s in zip(base, step))

    def test_the_first_sample_has_no_delta_so_cpu_is_null(self):
        """Null, never 0.0. "I have not measured yet" and "the machine is asleep" are
        different claims, and only the first is true two seconds after a restart."""
        sampler = scripted_sampler(times=[self.BASE])
        block = sampler.sample()
        self.assertIsNone(block["cpuPct"])
        # The read SUCCEEDED - the block is still served, and the memory half of it is
        # a single instantaneous reading that needs no history at all.
        self.assertEqual(block["memTotalGB"], 32.0)

    def test_the_second_sample_subtracts_idle_out_of_kernel_time(self):
        """THE TRAP, pinned by naming the two wrong answers it would otherwise give."""
        sampler = scripted_sampler(
            times=[self.BASE, self._advanced(self.BASE, self.STEP)])
        self.assertIsNone(sampler.sample()["cpuPct"])
        cpu = sampler.sample()["cpuPct"]
        self.assertEqual(cpu, 40.0)
        # 62.5 is (kernel + user) / (kernel + user + idle) - the reading that treats the
        # three counters as disjoint buckets. 100.0 is (kernel + user) / (kernel + user),
        # i.e. forgetting the subtraction entirely, which reports every host as pegged.
        self.assertNotEqual(cpu, 62.5)
        self.assertNotEqual(cpu, 100.0)

    def test_a_wholly_idle_host_reads_zero_not_a_hundred(self):
        """The single most damning symptom of the missing subtraction: on an idle box
        idle == kernel and user == 0, so the un-subtracted formula reports 100%."""
        step = (4 * _TICKS_PER_SEC, 4 * _TICKS_PER_SEC, 0)
        sampler = scripted_sampler(times=[self.BASE, self._advanced(self.BASE, step)])
        sampler.sample()
        self.assertEqual(sampler.sample()["cpuPct"], 0.0)

    def test_a_fully_pegged_host_reads_a_hundred(self):
        step = (0, 2 * _TICKS_PER_SEC, 2 * _TICKS_PER_SEC)
        sampler = scripted_sampler(times=[self.BASE, self._advanced(self.BASE, step)])
        sampler.sample()
        self.assertEqual(sampler.sample()["cpuPct"], 100.0)

    def test_more_idle_than_busy_time_serves_null_rather_than_a_fabricated_zero(self):
        """A-08 (v0.26.0). Idle exceeding kernel+user cannot happen on a healthy host - idle
        is a SUBSET of kernel time. A rigged reader or driver bug can break it, and
        (kernel+user - idle) then goes negative. The old code let _pct CLAMP that to a
        plausible 0.0; the contract's failure table puts an unusable reading in the NULL
        column (matching the backwards-counter branch), so it is served null, not 0.0."""
        step = (9 * _TICKS_PER_SEC, 4 * _TICKS_PER_SEC, 1 * _TICKS_PER_SEC)
        sampler = scripted_sampler(times=[self.BASE, self._advanced(self.BASE, step)])
        sampler.sample()
        # Mutation check: reverting A-08 (letting _pct clamp) serves 0.0 here.
        self.assertIsNone(sampler.sample()["cpuPct"])

    def test_one_decimal_place(self):
        # 1/3 of a core-second busy out of 3 -> 33.333...%.
        step = (2 * _TICKS_PER_SEC, 3 * _TICKS_PER_SEC, 0)
        sampler = scripted_sampler(times=[self.BASE, self._advanced(self.BASE, step)])
        sampler.sample()
        self.assertEqual(sampler.sample()["cpuPct"], 33.3)

    def test_a_sub_quantum_window_serves_null_not_a_fabricated_zero(self):
        """A-07 (v0.26.0). GetSystemTimes counters land in coarse scheduler quanta, not
        continuously. A window so short that only a quantum or two of kernel+user time
        elapsed cannot express a real busy fraction - idle and kernel moving by the same
        quantum reads as an exact 0.0 on a machine that is NOT asleep (measured: 197/300
        sub-quantum reads said idle on a box running the test loop). Below CPU_MIN_TOTAL_TICKS
        the split is quantisation noise and is served null, not a plausible 0.0. Reachable
        at cold start, where _do_state and _refresh_loop build overlapping windows.

        Mutation check: the old `total <= 0` guard (this total is positive) serves 0.0."""
        q = crabd.CPU_MIN_TOTAL_TICKS // 4          # a sub-quantum slice, well under the floor
        step = (q, q, 0)                            # idle +q, kernel +q -> total q, busy 0
        sampler = scripted_sampler(times=[self.BASE, self._advanced(self.BASE, step)])
        sampler.sample()
        self.assertIsNone(sampler.sample()["cpuPct"])
        # And a full-second window straddling the floor still reads honestly (not nulled).
        big = scripted_sampler(times=[self.BASE, self._advanced(self.BASE, self.STEP)])
        big.sample()
        self.assertEqual(big.sample()["cpuPct"], 40.0)

    def test_a_zero_delta_serves_null_without_losing_the_baseline(self):
        """A zero delta is not a measurement - but it must not COST the baseline either.

        Mutation-proven 2026-08-27: clearing `_prev` on this branch (or routing it back
        through the first-sample branch) leaves a caller that polls faster than the
        counters tick being served null forever, and this test is what catches it. The
        load-bearing act is declining to clear it, so movement accumulates against the
        surviving baseline until a real quantum lands. (A-09 correction: the earlier note
        here claimed assigning `_prev` would be a no-op "because the tuples are equal";
        that is false when idle moves while kernel+user do not - the skip is a real choice,
        justified by the accumulation above, not by tuple equality.)"""
        later = self._advanced(self.BASE, self.STEP)
        sampler = scripted_sampler(times=[self.BASE, self.BASE, later])
        self.assertIsNone(sampler.sample()["cpuPct"])
        self.assertIsNone(sampler.sample()["cpuPct"])    # identical reading
        self.assertEqual(sampler.sample()["cpuPct"], 40.0)

    def test_a_counter_that_goes_backwards_re_baselines_instead_of_serving_nonsense(self):
        earlier = tuple(v - _TICKS_PER_SEC for v in self.BASE)
        sampler = scripted_sampler(
            times=[self.BASE, earlier, self._advanced(earlier, self.STEP)])
        sampler.sample()
        self.assertIsNone(sampler.sample()["cpuPct"])    # backwards: nothing to report
        # ...and the backwards reading became the new baseline, so the pass after it is
        # a real measurement rather than another null.
        self.assertEqual(sampler.sample()["cpuPct"], 40.0)

    def test_the_previous_filetimes_are_per_sampler_not_global(self):
        """Two builders on one host must not share a baseline - the value IS the delta,
        and a shared one would report each builder's gap against the other's clock."""
        first = scripted_sampler(times=[self.BASE, self._advanced(self.BASE, self.STEP)])
        second = scripted_sampler(times=[self.BASE, self._advanced(self.BASE, self.STEP)])
        first.sample()
        first.sample()
        self.assertIsNone(second.sample()["cpuPct"])     # still ITS first sample


class HostSamplerMemoryTests(unittest.TestCase):

    def test_the_reading_becomes_percent_used_and_gibibytes(self):
        block = scripted_sampler(memory=[_MEM_READING]).sample()
        self.assertEqual(block["memTotalGB"], 32.0)
        self.assertEqual(block["memUsedGB"], 18.6)
        self.assertEqual(block["memPct"], 58.1)

    def test_more_available_than_installed_is_zero_used_not_a_negative_size(self):
        block = scripted_sampler(memory=[(8 * _GB, 9 * _GB)]).sample()
        self.assertEqual(block["memUsedGB"], 0.0)
        self.assertEqual(block["memPct"], 0.0)

    def test_a_zero_total_is_unreadable_rather_than_a_division(self):
        """Guarding this is what keeps the block finite: 100 * used / 0 is a
        ZeroDivisionError, and "installed memory is 0 bytes" is never a real reading."""
        sampler = scripted_sampler(times=[(1, 2, 3)], memory=[(0, 0)])
        block = sampler.sample()
        self.assertIsNone(block["memPct"])
        self.assertIsNone(block["memUsedGB"])
        self.assertIsNone(block["memTotalGB"])

    def test_a_non_finite_reading_is_null_not_a_clamped_hundred(self):
        """CD-10's rule, applied to the new block: `inf` must not become 100.0. A
        fabricated plausible number is the failure mode the honest-failure rule names."""
        sampler = scripted_sampler(times=[(1, 2, 3)],
                                   memory=[(float("inf"), float("nan"))])
        block = sampler.sample()
        self.assertIsNone(block["memPct"])
        self.assertIsNone(block["memTotalGB"])

    def test_a_malformed_reading_shape_is_unreadable_not_an_exception(self):
        for reading in ("not a tuple", (1,), (1, 2, 3), object()):
            with self.subTest(reading=reading):
                sampler = scripted_sampler(times=[(1, 2, 3)], memory=[reading])
                self.assertIsNone(sampler.sample()["memTotalGB"])


class HostSamplerHonestFailureTests(unittest.TestCase):
    """Three tiers of "cannot read", and they are three different served answers."""

    def setUp(self):
        # _log_once is a MODULE-GLOBAL set: without this a later test in the same
        # process would silently see an already-logged key and assert on no output.
        original = set(crabd._LOG_ONCE_SEEN)
        crabd._LOG_ONCE_SEEN.discard(crabd.HOST_CPU_LOG_KEY)
        crabd._LOG_ONCE_SEEN.discard(crabd.HOST_MEM_LOG_KEY)
        self.addCleanup(lambda: (crabd._LOG_ONCE_SEEN.clear(),
                                 crabd._LOG_ONCE_SEEN.update(original)))

    def test_neither_counter_readable_serves_no_block_at_all(self):
        """Tier 1. None, not a dict of four nulls: presence is the widget's feature
        detection, so a machine with no counters must render nothing rather than a row
        of em-dashes that looks like a broken sensor."""
        self.assertIsNone(scripted_sampler(times=[None], memory=[None]).sample())

    def test_only_the_cpu_counter_failing_leaves_the_memory_figures_intact(self):
        """Tier 2. A partial failure must not take the half that still works with it."""
        block = scripted_sampler(times=[None], memory=[_MEM_READING]).sample()
        self.assertIsNone(block["cpuPct"])
        self.assertEqual(block["memTotalGB"], 32.0)

    def test_only_the_memory_read_failing_leaves_the_cpu_figure_intact(self):
        sampler = scripted_sampler(
            times=[HostSamplerCpuMathTests.BASE,
                   HostSamplerCpuMathTests._advanced(HostSamplerCpuMathTests.BASE,
                                                     HostSamplerCpuMathTests.STEP)],
            memory=[None])
        sampler.sample()
        block = sampler.sample()
        self.assertEqual(block["cpuPct"], 40.0)
        self.assertIsNone(block["memPct"])

    def test_a_raising_counter_logs_once_and_never_propagates(self):
        """The sampler runs inside build(); an exception out of it would take the whole
        state document with it. One line, then silence - never a per-pass heartbeat."""
        def boom():
            raise RuntimeError("counter exploded")

        sampler = crabd.HostSampler(times=boom, memory=boom)
        original, sys.stderr = sys.stderr, io.StringIO()
        try:
            self.assertIsNone(sampler.sample())
            self.assertIsNone(sampler.sample())
            self.assertIsNone(sampler.sample())
            noise = sys.stderr.getvalue()
        finally:
            sys.stderr = original
        self.assertIn("RuntimeError", noise)
        self.assertEqual(noise.count("host CPU counter raised"), 1)
        self.assertEqual(noise.count("host memory read raised"), 1)

    def test_a_stale_value_is_never_re_served_as_fresh(self):
        """There is no last-good cache in the sampler, and this is what says so: a good
        reading followed by a failed one serves NULL, not the good one again."""
        sampler = scripted_sampler(
            times=[HostSamplerCpuMathTests.BASE,
                   HostSamplerCpuMathTests._advanced(HostSamplerCpuMathTests.BASE,
                                                     HostSamplerCpuMathTests.STEP),
                   None],
            memory=[_MEM_READING, _MEM_READING, None])
        sampler.sample()
        self.assertEqual(sampler.sample()["cpuPct"], 40.0)
        self.assertIsNone(sampler.sample())


class HostBlockThroughTheBuilderTests(TempProjects):
    """The wiring: sampled on the BUILDER's existing pass, no thread of its own."""

    def _builder(self, sampler):
        return crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                  crabd.HookTracker(), StubLimits(), time.time(),
                                  host=sampler)

    def test_each_build_takes_exactly_one_sample_and_the_second_carries_cpu(self):
        base = HostSamplerCpuMathTests.BASE
        sampler = scripted_sampler(
            times=[base, HostSamplerCpuMathTests._advanced(
                base, HostSamplerCpuMathTests.STEP)])
        builder = self._builder(sampler)
        self.assertIsNone(builder.build()["host"]["cpuPct"])
        self.assertEqual(builder.build()["host"]["cpuPct"], 40.0)
        # One read per build - the piggyback, stated as a count. A sampler on a thread
        # of its own would decouple these two numbers.
        self.assertEqual(sampler.counters.time_calls, 2)

    def test_an_unreadable_host_leaves_the_key_off_the_document(self):
        state = self._builder(scripted_sampler(times=[None], memory=[None])).build()
        self.assertNotIn("host", state)
        # ...and nothing else moved: the block is additive and optional.
        self.assertEqual(state["schema"], 5)
        self.assertIn("burn", state)

    def test_the_default_builder_gets_a_real_sampler_not_none(self):
        """A builder with no `host=` must sample the real machine. If this were optional
        the way the v0.12.0 readers are, every unit-test builder would be exercising the
        absent path and the live one would ship untested."""
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), time.time())
        self.assertIsInstance(builder._host, crabd.HostSampler)

    def test_the_served_document_stays_json_serializable(self):
        """dump_state refuses non-finite floats, so a bad host reading would take the
        WHOLE document down the sanitising path. Proven here for the ordinary block."""
        state = self._builder(scripted_sampler(times=[(1, 2, 3)])).build()
        parsed = json.loads(crabd.dump_state(state))
        self.assertEqual(parsed["host"]["memTotalGB"], 32.0)
        self.assertIsNone(parsed["host"]["cpuPct"])


@unittest.skipUnless(sys.platform == "win32", "GetSystemTimes / GlobalMemoryStatusEx")
class HostSamplerLiveReadTests(unittest.TestCase):
    """A read-only measurement of THIS host, bounds-checked. The healthy-night test:
    the arithmetic above is proven against numbers this file invented, and this is the
    one place it meets a real kernel."""

    def test_the_real_counters_produce_a_sane_block(self):
        sampler = crabd.HostSampler()
        block = sampler.sample()
        self.assertIsNotNone(block, "the Windows counters should be readable here")
        self.assertIsNone(block["cpuPct"])          # first sample, by construction
        self.assertGreater(block["memTotalGB"], 0.5)
        self.assertGreaterEqual(block["memPct"], 0.0)
        self.assertLessEqual(block["memPct"], 100.0)
        self.assertLessEqual(block["memUsedGB"], block["memTotalGB"])

        # Burn a little CPU so the kernel counters definitely advance past their ~15 ms
        # granularity, then take the second sample the delta needs. Retried rather than
        # slept on: a fixed sleep would be either flaky or slow.
        cpu = None
        for _ in range(20):
            deadline = time.perf_counter() + 0.05
            while time.perf_counter() < deadline:
                pass
            cpu = sampler.sample()["cpuPct"]
            if cpu is not None:
                break
        self.assertIsNotNone(cpu, "two samples 50 ms+ apart should yield a percentage")
        self.assertGreaterEqual(cpu, 0.0)
        self.assertLessEqual(cpu, 100.0)


# ------------------------------------------------- v0.23.0: the quiet-hours override

class QuietOverrideReadTests(unittest.TestCase):
    """`quiet_override` and the EFFECTIVE `quiet.active` - the pure functions, where the
    whole feature's correctness lives. Everything downstream (widget dim, glow, crab
    nightcap, the notifier's four suppression sites) reads the one boolean these produce.
    """

    WINDOW = {"start": "22:00", "end": "07:00"}

    @staticmethod
    def at(hour, minute=0):
        lt = time.localtime()
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0, 0, 0, -1))

    @staticmethod
    def override(mode, now, seconds):
        return {"mode": mode, "until": crabd._utc_iso(now + seconds)}

    def test_no_key_no_override(self):
        for config in ({}, None, {"quietHours": None}, {"quietOverride": None}):
            self.assertIsNone(crabd.quiet_override(config, time.time()), config)

    def test_a_live_override_reads_back_normalised(self):
        now = time.time()
        out = crabd.quiet_override(
            {"quietOverride": {"mode": "on", "until": crabd._utc_iso(now + 600)}}, now)
        self.assertEqual(out["mode"], "on")
        self.assertEqual(out["until"], crabd._utc_iso(now + 600))

    def test_a_hand_edited_offset_and_fraction_are_served_in_the_contract_shape(self):
        """`until` is re-formatted from the parsed epoch, never echoed: the widget reads
        one shape whatever a hand edit put in the file."""
        now = self.at(12)
        stamp = crabd.datetime.fromtimestamp(
            now + 600, crabd.timezone.utc).isoformat(timespec="milliseconds")
        self.assertTrue(stamp.endswith("+00:00"))
        out = crabd.quiet_override({"quietOverride": {"mode": "off", "until": stamp}}, now)
        self.assertEqual(out["until"], crabd._utc_iso(now + 600))

    def test_malformed_overrides_read_as_absent(self):
        now = time.time()
        for raw in ({"mode": "auto", "until": crabd._utc_iso(now + 60)},   # never stored
                    {"mode": "on"},
                    {"mode": "on", "until": "tomorrow"},
                    {"mode": "on", "until": None},
                    {"mode": True, "until": crabd._utc_iso(now + 60)},
                    {"until": crabd._utc_iso(now + 60)},
                    {"mode": "ON", "until": crabd._utc_iso(now + 60)},
                    "on", [], 5):
            self.assertIsNone(crabd.quiet_override({"quietOverride": raw}, now), raw)

    def test_an_override_never_outlives_until(self):
        """THE healthy-night boundary. `until` itself is over - half-open, exactly like
        the quiet window's exclusive `end`, so the two cannot disagree about a minute."""
        now = self.at(12)
        raw = {"quietOverride": self.override("on", now, 900)}
        self.assertIsNotNone(crabd.quiet_override(raw, now))
        self.assertIsNotNone(crabd.quiet_override(raw, now + 899))
        self.assertIsNone(crabd.quiet_override(raw, now + 900))     # the boundary
        self.assertIsNone(crabd.quiet_override(raw, now + 901))

    def test_on_forces_quiet_against_a_schedule_that_says_otherwise(self):
        now = self.at(12)                       # noon, outside a 22:00-07:00 window
        base = crabd.quiet_state({"quietHours": self.WINDOW}, now)
        self.assertFalse(base["active"])
        block = crabd.quiet_state({"quietHours": self.WINDOW,
                                   "quietOverride": self.override("on", now, 900)}, now)
        self.assertTrue(block["active"])
        self.assertEqual((block["start"], block["end"]), ("22:00", "07:00"))
        self.assertEqual(block["override"]["mode"], "on")

    def test_off_suppresses_a_live_schedule_window(self):
        """The half that is easy to drop and the half the operator notices - working
        through the night with the panel refusing to dim."""
        now = self.at(23)                       # inside the 22:00-07:00 window
        self.assertTrue(crabd.quiet_state({"quietHours": self.WINDOW}, now)["active"])
        block = crabd.quiet_state({"quietHours": self.WINDOW,
                                   "quietOverride": self.override("off", now, 900)}, now)
        self.assertFalse(block["active"])
        self.assertEqual(block["override"]["mode"], "off")

    def test_an_expired_override_hands_the_answer_back_to_the_schedule(self):
        now = self.at(23)
        config = {"quietHours": self.WINDOW,
                  "quietOverride": self.override("off", now, 900)}
        self.assertFalse(crabd.quiet_state(config, now)["active"])
        later = crabd.quiet_state(config, now + 900)
        self.assertTrue(later["active"])            # 23:15, back inside the window
        self.assertNotIn("override", later)

    def test_the_override_member_is_absent_when_there_is_none(self):
        self.assertNotIn("override",
                         crabd.quiet_state({"quietHours": self.WINDOW}, self.at(12)))

    def test_an_override_with_no_schedule_still_produces_a_block(self):
        """Otherwise the tap does visibly nothing on the install most likely to use it -
        the one that never configured quiet hours at all."""
        now = self.at(12)
        block = crabd.quiet_state({"quietOverride": self.override("on", now, 900)}, now)
        self.assertTrue(block["active"])
        self.assertEqual((block["start"], block["end"]), (None, None))
        self.assertEqual(block["override"]["until"], crabd._utc_iso(now + 900))
        off = crabd.quiet_state({"quietOverride": self.override("off", now, 900)}, now)
        self.assertFalse(off["active"])

    def test_no_schedule_and_no_override_is_still_a_null_block(self):
        now = self.at(12)
        self.assertIsNone(crabd.quiet_state({}, now))
        self.assertIsNone(crabd.quiet_state(
            {"quietOverride": self.override("on", now, -60)}, now))

    def test_a_malformed_override_cannot_disturb_the_schedule(self):
        now = self.at(23)
        block = crabd.quiet_state({"quietHours": self.WINDOW,
                                   "quietOverride": {"mode": "on"}}, now)
        self.assertTrue(block["active"])
        self.assertNotIn("override", block)


class QuietOverrideWriteTests(unittest.TestCase):
    """UserConfig.set_quiet_override - the persistence half."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "config.json"
        self.addCleanup(self._tmp.cleanup)

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_the_override_is_written_in_the_contract_shape(self):
        now = time.time()
        config = crabd.UserConfig(self.path)
        self.assertTrue(config.set_quiet_override("on", now + 7200))
        self.assertEqual(self.read()["quietOverride"],
                         {"mode": "on", "until": crabd._utc_iso(now + 7200)})

    def test_it_survives_a_crabd_restart(self):
        """Simulated the honest way: a FRESH UserConfig over the same file, holding none
        of the first one's cache. This is the difference between a persisted override and
        one that lives in a process."""
        now = time.time()
        crabd.UserConfig(self.path).set_quiet_override("on", now + 7200)
        reborn = crabd.UserConfig(self.path)
        block = crabd.quiet_state(reborn.get(now), now)
        self.assertEqual(block["override"],
                         {"mode": "on", "until": crabd._utc_iso(now + 7200)})
        self.assertTrue(block["active"])

    def test_every_other_key_is_preserved(self):
        """The v0.16.0 lesson: this writer goes through the same locked
        read-modify-write /v1/config uses, so nothing it does not name is lost."""
        self.path.write_text(json.dumps({
            "quietHours": {"start": "22:00", "end": "07:00"}, "allowReply": True,
            "toast": {"thresholdSec": 300, "enabled": True, "approvalThresholdSec": 45},
            "panelApprovals": {"enabled": True}}), encoding="utf-8")
        crabd.UserConfig(self.path).set_quiet_override("off", time.time() + 900)
        after = self.read()
        self.assertEqual(after["quietHours"], {"start": "22:00", "end": "07:00"})
        self.assertTrue(after["allowReply"])
        self.assertEqual(after["toast"]["approvalThresholdSec"], 45)
        self.assertEqual(after["panelApprovals"], {"enabled": True})

    def test_clearing_removes_the_key_and_is_idempotent(self):
        config = crabd.UserConfig(self.path)
        config.set_quiet_override("on", time.time() + 900)
        for _ in range(3):
            self.assertTrue(config.set_quiet_override(None, 0.0))
            self.assertNotIn("quietOverride", self.read())

    def test_clearing_an_override_that_was_never_set_is_still_a_success(self):
        self.assertTrue(crabd.UserConfig(self.path).set_quiet_override(None, 0.0))
        self.assertNotIn("quietOverride", self.read())

    def test_a_second_override_replaces_the_first(self):
        now = time.time()
        config = crabd.UserConfig(self.path)
        config.set_quiet_override("on", now + 900)
        config.set_quiet_override("off", now + 1800)
        self.assertEqual(self.read()["quietOverride"],
                         {"mode": "off", "until": crabd._utc_iso(now + 1800)})

    def test_an_expired_override_is_swept_on_the_next_write(self):
        """Lazily, on any write - there is deliberately no timer whose only job is to
        delete a key that already reads as absent."""
        now = time.time()
        self.path.write_text(json.dumps({
            "quietOverride": {"mode": "on", "until": crabd._utc_iso(now - 60)}}),
            encoding="utf-8")
        crabd.UserConfig(self.path).set_keys(
            {"digest": {"enabled": True, "time": "08:30"}})
        after = self.read()
        self.assertNotIn("quietOverride", after)
        self.assertEqual(after["digest"], {"enabled": True, "time": "08:30"})

    def test_a_live_override_is_not_swept_by_an_unrelated_config_write(self):
        """The mutation that matters on the sweep: a sweep that dropped every override
        would make the panel's quiet-hours save silently cancel the operator's tap."""
        now = time.time()
        config = crabd.UserConfig(self.path)
        config.set_quiet_override("on", now + 7200)
        config.set_keys({"quietHours": {"start": "22:00", "end": "07:00"}})
        self.assertEqual(self.read()["quietOverride"],
                         {"mode": "on", "until": crabd._utc_iso(now + 7200)})

    def test_a_malformed_override_is_swept_too(self):
        self.path.write_text(json.dumps({"quietOverride": {"mode": "sometimes"}}),
                             encoding="utf-8")
        crabd.UserConfig(self.path).set_quiet_override(None, 0.0)
        self.assertNotIn("quietOverride", self.read())

    def test_the_write_is_visible_to_the_next_read_despite_the_damper(self):
        """set_keys busts the once-a-minute cache; so must this one, or the panel's own
        tap would take up to 60 s to reach the document it just changed."""
        now = time.time()
        config = crabd.UserConfig(self.path)
        config.get(now)                                    # prime the cache
        config.set_quiet_override("on", now + 900)
        self.assertEqual(config.get(now)["quietOverride"]["mode"], "on")


class QuietOverrideActionTests(ServedOverASocket):
    """POST /v1/action {"action":"quiet"} over a real socket."""

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def rebuild(self):
        with self.builder._lock:
            self.builder._state = self.builder.build()

    def test_on_is_204_and_persists_the_override(self):
        before = time.time()
        status, body = self.action({"action": "quiet", "mode": "on", "minutes": 120})
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        override = self.read_config()["quietOverride"]
        self.assertEqual(override["mode"], "on")
        until = crabd._parse_ts(override["until"])
        self.assertGreaterEqual(until, before + 120 * 60 - 5)
        self.assertLessEqual(until, time.time() + 120 * 60 + 5)

    def test_the_override_reaches_the_served_document(self):
        self.action({"action": "quiet", "mode": "on", "minutes": 15})
        self.rebuild()
        quiet = self.state()["quiet"]
        self.assertTrue(quiet["active"])
        self.assertEqual(quiet["override"]["mode"], "on")

    def test_off_suppresses_a_live_quiet_window_end_to_end(self):
        """Through the endpoint, not the function: a window centred on the current
        minute (so `active` is true whatever time the suite runs), then one tap."""
        local = time.localtime()
        minute = local.tm_hour * 60 + local.tm_min
        window = {"start": "%02d:%02d" % divmod((minute - 60) % 1440, 60),
                  "end": "%02d:%02d" % divmod((minute + 60) % 1440, 60)}
        self.client.post("/v1/config", json.dumps({"quietHours": window}).encode())
        self.rebuild()
        self.assertTrue(self.state()["quiet"]["active"])
        self.assertEqual(self.action({"action": "quiet", "mode": "off",
                                      "minutes": 60})[0], 204)
        self.rebuild()
        quiet = self.state()["quiet"]
        self.assertFalse(quiet["active"])
        self.assertEqual(quiet["override"]["mode"], "off")
        self.assertEqual((quiet["start"], quiet["end"]),
                         (window["start"], window["end"]))

    def test_auto_clears_and_is_idempotent(self):
        self.action({"action": "quiet", "mode": "on", "minutes": 60})
        for _ in range(3):
            status, body = self.action({"action": "quiet", "mode": "auto"})
            self.assertEqual(status, 204)
            self.assertEqual(body, b"")
            self.assertNotIn("quietOverride", self.read_config())
        self.rebuild()
        self.assertNotIn("override", self.state()["quiet"] or {})

    def test_auto_ignores_a_minutes_it_was_sent(self):
        """Cancel must never fail - it is what the operator taps when the panel is doing
        something they did not intend."""
        self.action({"action": "quiet", "mode": "on", "minutes": 60})
        self.assertEqual(self.action({"action": "quiet", "mode": "auto",
                                      "minutes": 9999})[0], 204)
        self.assertNotIn("quietOverride", self.read_config())

    def test_a_bad_mode_is_400_and_writes_nothing(self):
        for mode in ("ON", "Auto", "quiet", "", True, 1, [], {"mode": "on"}, "MISSING"):
            body = {"action": "quiet", "minutes": 60}
            if mode != "MISSING":
                body["mode"] = mode
            status, reply = self.action(body)
            self.assertEqual(status, 400, mode)
            self.assertEqual(json.loads(reply),
                             {"error": "mode must be on, off or auto"})
            self.assertNotIn("quietOverride", self.read_config())

    def test_bad_minutes_are_400_and_write_nothing(self):
        for minutes in ("MISSING", 14, 481, 0, -60, 60.0, "60", True, False, [60],
                        10 ** 9, None):
            body = {"action": "quiet", "mode": "on"}
            if minutes != "MISSING":
                body["minutes"] = minutes
            status, reply = self.action(body)
            self.assertEqual(status, 400, minutes)
            self.assertEqual(json.loads(reply),
                             {"error": "minutes must be an integer 15..480"})
            self.assertNotIn("quietOverride", self.read_config())

    def test_the_bounds_themselves_are_accepted(self):
        for minutes in (15, 480):
            status, _ = self.action({"action": "quiet", "mode": "off",
                                     "minutes": minutes})
            self.assertEqual(status, 204, minutes)
            self.assertEqual(self.read_config()["quietOverride"]["mode"], "off")

    def test_quiet_needs_no_session_id(self):
        """It is a whole-panel gesture like ack-all, and it is handled before the
        sessionId check - a body carrying one is not required and not read."""
        status, _ = self.action({"action": "quiet", "mode": "on", "minutes": 15})
        self.assertEqual(status, 204)

    def test_quiet_refuses_a_cross_site_web_page(self):
        """SEC-1: the same Origin gate every mutating action rides. A visited page must
        not be able to dim the operator's panel."""
        reply = self.client.post(
            "/v1/action",
            json.dumps({"action": "quiet", "mode": "on", "minutes": 60}).encode(),
            headers={"Origin": "http://evil.example"})
        self.assertEqual(reply.status, 403)
        self.assertNotIn("quietOverride", self.read_config())

    def test_quiet_allows_the_widget_null_origin_and_reflects_it(self):
        reply = self.client.post(
            "/v1/action",
            json.dumps({"action": "quiet", "mode": "auto"}).encode(),
            headers={"Origin": "null"})
        self.assertEqual(reply.status, 204)
        self.assertEqual(reply.headers.get("Access-Control-Allow-Origin"), "null")

    def test_quiet_override_is_not_writable_over_v1_config(self):
        """It IS panel-writable - just not through /v1/config. The action endpoint is its
        only writer, which is what bounds the values that can reach the file."""
        before = self.config_path.read_text(encoding="utf-8")
        reply = self.client.post("/v1/config", json.dumps(
            {"quietOverride": {"mode": "on",
                               "until": "2099-01-01T00:00:00Z"}}).encode())
        self.assertEqual(reply.status, 400)
        self.assertNotIn("quietOverride", crabd.Handler.CONFIG_WRITABLE)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_quiet_override_beside_a_valid_key_is_still_400(self):
        before = self.config_path.read_text(encoding="utf-8")
        reply = self.client.post("/v1/config", json.dumps(
            {"quietHours": {"start": "22:00", "end": "07:00"},
             "quietOverride": {"mode": "on",
                               "until": "2099-01-01T00:00:00Z"}}).encode())
        self.assertEqual(reply.status, 400)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), before)

    def test_an_unknown_action_is_still_400_after_the_quiet_branch(self):
        status, body = self.action({"action": "quieten", "mode": "on", "minutes": 60})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "malformed request"})


# ------------------------------------------- v0.24.0: the panel diagnostics log channel

# The exact wire shape of the server-side prefix. Pinned as a regex rather than
# reconstructed from strftime so a change to the FORMAT (a space dropped, the marker
# renamed, milliseconds added) fails here instead of silently shipping to the widget lane
# that is building a reader against it.
PANEL_LOG_PREFIX_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \[panel\] ")


def _unprefixed(line):
    return PANEL_LOG_PREFIX_RE.sub("", line, count=1)


class PanelLogRingTests(unittest.TestCase):
    """The ring itself, off the socket. The endpoint tests below prove the wiring; these
    prove the bound, the eviction accounting and the prefix."""

    def test_a_line_comes_back_prefixed_and_otherwise_verbatim(self):
        log = crabd.PanelLog()
        log.append(["pointerdown t=12 id=0"], 1756346000.0)
        lines, dropped = log.snapshot()
        self.assertEqual(lines, ["2025-08-28T01:53:20Z [panel] pointerdown t=12 id=0"])
        self.assertEqual(dropped, 0)

    def test_one_batch_gets_one_timestamp_and_keeps_its_order(self):
        """They arrived in one request, so one receive time is the honest reading - and
        it makes intra-batch order the order the widget wrote them in."""
        log = crabd.PanelLog()
        log.append(["a", "b", "c"], 1756346000.0)
        lines, _ = log.snapshot()
        self.assertEqual([_unprefixed(ln) for ln in lines], ["a", "b", "c"])
        self.assertEqual(len({ln[:20] for ln in lines}), 1)

    def test_the_ring_evicts_oldest_first_and_counts_what_it_dropped(self):
        log = crabd.PanelLog(limit=3)
        log.append(["1", "2", "3", "4", "5"], 1756346000.0)
        lines, dropped = log.snapshot()
        self.assertEqual([_unprefixed(ln) for ln in lines], ["3", "4", "5"])
        self.assertEqual(dropped, 2)

    def test_dropped_total_accumulates_across_posts(self):
        log = crabd.PanelLog(limit=2)
        for batch in (["1", "2"], ["3"], ["4", "5"]):
            log.append(batch, 1756346000.0)
        lines, dropped = log.snapshot()
        self.assertEqual([_unprefixed(ln) for ln in lines], ["4", "5"])
        self.assertEqual(dropped, 3)

    def test_a_single_batch_larger_than_the_ring_leaves_only_its_tail(self):
        log = crabd.PanelLog(limit=2)
        log.append([str(n) for n in range(10)], 1756346000.0)
        lines, dropped = log.snapshot()
        self.assertEqual([_unprefixed(ln) for ln in lines], ["8", "9"])
        self.assertEqual(dropped, 8)

    def test_snapshot_hands_back_a_copy(self):
        """A reader iterating the result must not be tripped by a concurrent POST."""
        log = crabd.PanelLog()
        log.append(["one"], 1756346000.0)
        lines, _ = log.snapshot()
        lines.append("forged")
        self.assertEqual(len(log.snapshot()[0]), 1)

    def test_the_bounds_are_the_contract(self):
        """The ring bound IS the flood posture - there is no rate limit behind it."""
        self.assertEqual(crabd.PANEL_LOG_MAX_LINES, 500)
        self.assertEqual(crabd.PANEL_LOG_MAX_PER_POST, 50)
        self.assertEqual(crabd.PANEL_LOG_MAX_LINE_CHARS, 300)


class PanelLogNormalizerTests(unittest.TestCase):
    """_panel_log_lines - which of the three bounds is an ERROR and which is a SILENT
    reshape. That split is the contract the widget lane builds against."""

    def norm(self, value):
        return crabd._panel_log_lines(value)

    def test_a_plain_list_of_strings_passes_through(self):
        self.assertEqual(self.norm(["a", "b"]), ["a", "b"])

    def test_a_long_line_is_truncated_not_rejected(self):
        self.assertEqual(self.norm(["x" * 400]), ["x" * 300])

    def test_trim_happens_before_truncate(self):
        """The 300 is a budget on CONTENT: leading whitespace must not be able to push
        the useful half of a line off the end."""
        self.assertEqual(self.norm(["   " + "y" * 300 + "   "]), ["y" * 300])

    def test_a_line_that_trims_to_empty_is_kept(self):
        """Still evidence the widget posted. Dropping it would invent a rule the frozen
        contract does not have."""
        self.assertEqual(self.norm(["   "]), [""])

    def test_more_than_fifty_lines_keeps_the_first_fifty(self):
        out = self.norm([str(n) for n in range(120)])
        self.assertEqual(len(out), 50)
        self.assertEqual((out[0], out[-1]), ("0", "49"))

    def test_a_bad_type_past_the_cap_is_not_an_error(self):
        """Members past 50 are dropped WITHOUT being type-checked - they are not stored,
        so their type cannot matter, and 400ing on line 51 would make the cap a rejection
        after all."""
        self.assertEqual(len(self.norm(["ok"] * 50 + [None, 5, {}])), 50)

    def test_the_refusals(self):
        for value in (None, "a string", 5, {}, {"0": "a"}, [],
                      ["ok", None], [None], [5], [["nested"]], [{"a": 1}],
                      ["ok", 5, "ok"], [True], [b"bytes"]):
            self.assertIsNone(self.norm(value), value)

    def test_a_bool_is_not_a_string(self):
        """`isinstance(True, str)` is already False, so no special case is needed here -
        pinned because every numeric validator in this file DOES need one."""
        self.assertIsNone(self.norm([True]))

    # ---- SEC-d: interior control/ANSI bytes are stripped

    def test_interior_control_and_ansi_bytes_are_stripped(self):
        """SEC-d (v0.25.0). `.strip()` only trims edge whitespace; a control byte in the
        MIDDLE survived it. JSON-safe (dump_state escapes it) but a maintainer echoing the
        line to a terminal would hit the ANSI. The stored line must carry no control
        byte."""
        out = self.norm(["red \x1b[31mALERT\x1b[0m done\x07\x00 tail"])
        self.assertEqual(out, ["red [31mALERT[0m done tail"])
        self.assertFalse(any(ord(c) < 0x20 and c not in "\t\n\r"
                             or 0x7f <= ord(c) <= 0x9f
                             for c in out[0]))

    def test_ordinary_whitespace_and_unicode_survive_the_control_strip(self):
        """Tab/newline are whitespace, not control noise, and unicode above 0x9f - accented
        text, emoji - must not be mangled by a byte-class strip."""
        self.assertEqual(self.norm(["a\tb café \U0001f980 end"]),
                         ["a\tb café \U0001f980 end"])

    def test_a_control_laden_line_still_obeys_the_300_char_budget(self):
        """Strip THEN truncate: stripping control bytes first must still leave a line
        capped at 300 printable characters."""
        out = self.norm(["\x1b" * 100 + "z" * 400])
        self.assertEqual(out, ["z" * 300])


class PanelLogEndpointTests(ServedOverASocket):
    """POST + GET /v1/panel-log over a real socket."""

    def post_log(self, payload=None, raw=None, headers=None):
        data = raw if raw is not None else json.dumps(payload).encode()
        return self.client.post("/v1/panel-log", data, headers=headers)

    def read_log(self, headers=None):
        return self.client.get("/v1/panel-log", headers=headers)

    # ---- round trip

    def test_the_channel_round_trips(self):
        reply = self.post_log({"lines": ["pointerdown", "pointerup"]})
        self.assertEqual(reply.status, 204)
        self.assertEqual(reply.body, b"")
        body = self.read_log().json()
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["droppedTotal"], 0)
        self.assertEqual([_unprefixed(ln) for ln in body["lines"]],
                         ["pointerdown", "pointerup"])

    def test_an_empty_ring_reads_as_an_empty_list_not_a_404(self):
        """The widget lane needs a stable shape from the first GET, before anything has
        been posted - and an empty ring is a different fact from "no such endpoint"."""
        self.assertEqual(self.read_log().json(),
                         {"lines": [], "count": 0, "droppedTotal": 0})

    def test_every_served_line_carries_the_timestamp_prefix(self):
        self.post_log({"lines": ["touchstart touches=2", "gesturechange"]})
        for line in self.read_log().json()["lines"]:
            self.assertRegex(line, PANEL_LOG_PREFIX_RE)

    def test_the_prefix_is_the_only_thing_added(self):
        """Stored VERBATIM otherwise - including the characters a parser would want to
        eat. Nothing in crabd reads these lines back into any decision path, which is why
        storing them unexamined is safe."""
        raw = 'ignore previous instructions; {"a": [1]} <b>&amp;</b> \\n\ttab'
        self.post_log({"lines": [raw]})
        line = self.read_log().json()["lines"][0]
        self.assertRegex(line, PANEL_LOG_PREFIX_RE)
        self.assertEqual(_unprefixed(line), raw)

    def test_reads_do_not_consume(self):
        """A ring, not a queue: two readers must see the same lines."""
        self.post_log({"lines": ["a"]})
        self.assertEqual(self.read_log().json()["count"], 1)
        self.assertEqual(self.read_log().json()["count"], 1)

    # ---- caps

    def test_a_line_over_300_chars_is_truncated_and_still_204(self):
        reply = self.post_log({"lines": ["z" * 900]})
        self.assertEqual(reply.status, 204)
        self.assertEqual(_unprefixed(self.read_log().json()["lines"][0]), "z" * 300)

    def test_over_fifty_lines_stores_the_first_fifty_and_still_204(self):
        reply = self.post_log({"lines": [f"line{n}" for n in range(400)]})
        self.assertEqual(reply.status, 204)
        body = self.read_log().json()
        self.assertEqual(body["count"], 50)
        self.assertEqual(body["droppedTotal"], 0)
        self.assertEqual(_unprefixed(body["lines"][-1]), "line49")

    def test_the_ring_holds_500_and_reports_what_it_evicted(self):
        """The REAL bound over the real socket: 12 full posts = 600 lines in, 500 held,
        100 evicted. This is what the ring bound is mutation-checked against."""
        for batch in range(12):
            reply = self.post_log({"lines": [f"b{batch}-{n}" for n in range(50)]})
            self.assertEqual(reply.status, 204)
        body = self.read_log().json()
        self.assertEqual(body["count"], 500)
        self.assertEqual(len(body["lines"]), 500)
        self.assertEqual(body["droppedTotal"], 100)
        # Oldest-first: the first two batches are gone and the tail is what was last sent.
        self.assertEqual(_unprefixed(body["lines"][0]), "b2-0")
        self.assertEqual(_unprefixed(body["lines"][-1]), "b11-49")

    def test_count_is_the_length_of_what_was_returned(self):
        """The same rule /v1/history's count follows, so a reader never has to reconcile
        a count against a shorter list."""
        for _ in range(12):
            self.post_log({"lines": ["x"] * 50})
        body = self.read_log().json()
        self.assertEqual(body["count"], len(body["lines"]))

    def test_a_maximal_body_every_poll_is_answered(self):
        """Flood posture: 50 lines at 300 chars, twenty times over. The ring is the only
        bound and it holds - no rate limit, no growth past 500."""
        payload = {"lines": ["q" * 300] * 50}
        for _ in range(20):
            self.assertEqual(self.post_log(payload).status, 204)
        body = self.read_log().json()
        self.assertEqual(body["count"], 500)
        self.assertEqual(body["droppedTotal"], 500)

    # ---- 400s

    def test_the_400s(self):
        for payload, raw in ((None, b"not json"),
                             (None, b""),
                             (None, b'"a string"'),
                             (None, b"[1,2,3]"),
                             ({}, None),
                             ({"lines": None}, None),
                             ({"lines": []}, None),
                             ({"lines": "a string"}, None),
                             ({"lines": {"0": "a"}}, None),
                             ({"lines": ["ok", 5]}, None),
                             ({"lines": [None]}, None),
                             ({"lines": [["nested"]]}, None),
                             ({"lines": [{"a": 1}]}, None)):
            reply = self.post_log(payload, raw)
            self.assertEqual(reply.status, 400, (payload, raw))
            self.assertEqual(json.loads(reply.body),
                             {"error": "lines must be an array of 1..50 strings"})

    def test_a_400_stores_nothing(self):
        self.post_log({"lines": ["kept"]})
        self.post_log({"lines": ["dropped", 5]})
        body = self.read_log().json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(_unprefixed(body["lines"][0]), "kept")

    def test_a_400_carries_cors_so_the_widget_can_read_the_status(self):
        reply = self.post_log({"lines": []}, headers={"Origin": "null"})
        self.assertEqual(reply.status, 400)
        self.assertEqual(reply.headers.get("Access-Control-Allow-Origin"), "null")

    # ---- the origin gates, both directions

    def test_the_post_refuses_a_cross_site_web_page_before_storing_anything(self):
        """SEC-1. The second half is the one that matters: the gate fires BEFORE the side
        effect, so a page the operator merely visited cannot flood the ring."""
        reply = self.post_log({"lines": ["from a web page"]},
                              headers={"Origin": "https://evil.example"})
        self.assertEqual(reply.status, 403)
        self.assertEqual(json.loads(reply.body),
                         {"error": "cross-site request refused"})
        self.assertIsNone(reply.headers.get("Access-Control-Allow-Origin"))
        self.assertEqual(self.read_log().json()["count"], 0)

    def test_the_get_refuses_a_cross_site_web_page(self):
        """SEC-4. These lines describe what is on the operator's panel while they touch
        it; a visited page has no more business reading them than reading /v1/state."""
        self.post_log({"lines": ["secret-ish"]})
        reply = self.read_log(headers={"Origin": "http://evil.example"})
        self.assertEqual(reply.status, 403)
        self.assertEqual(json.loads(reply.body),
                         {"error": "cross-site request refused"})
        self.assertNotIn(b"secret-ish", reply.body)
        self.assertIsNone(reply.headers.get("Access-Control-Allow-Origin"))

    def test_both_verbs_allow_the_widget_null_origin_and_reflect_it(self):
        post = self.post_log({"lines": ["tap"]}, headers={"Origin": "null"})
        self.assertEqual(post.status, 204)
        self.assertEqual(post.headers.get("Access-Control-Allow-Origin"), "null")
        get = self.read_log(headers={"Origin": "null"})
        self.assertEqual(get.status, 200)
        self.assertEqual(get.headers.get("Access-Control-Allow-Origin"), "null")

    def test_both_verbs_work_with_no_origin_and_send_no_wildcard(self):
        """curl and the maintainer's own tooling send no Origin at all."""
        post = self.post_log({"lines": ["tap"]})
        self.assertEqual(post.status, 204)
        self.assertIsNone(post.headers.get("Access-Control-Allow-Origin"))
        get = self.read_log()
        self.assertEqual(get.status, 200)
        self.assertIsNone(get.headers.get("Access-Control-Allow-Origin"))

    # ---- routing and scope

    def test_the_path_is_in_the_mutating_inventory(self):
        """MUTATING_PATHS is the readable inventory of what CHANGES STATE and the
        security docs reason about it, so a new write path missing from it is a doc
        drift waiting to happen."""
        self.assertIn("/v1/panel-log", crabd.Handler.MUTATING_PATHS)

    def test_a_trailing_slash_still_routes_on_both_verbs(self):
        self.assertEqual(
            self.client.post("/v1/panel-log/",
                             json.dumps({"lines": ["a"]}).encode()).status, 204)
        self.assertEqual(self.client.get("/v1/panel-log/").status, 200)

    def test_a_neighbouring_path_is_still_404(self):
        for path in ("/v1/panel-logs", "/v1/panel-log/tail", "/panel-log"):
            self.assertEqual(self.client.get(path).status, 404, path)
            self.assertEqual(
                self.client.post(path, json.dumps({"lines": ["a"]}).encode()).status,
                404, path)

    def test_the_channel_is_not_in_the_state_document(self):
        """It is a SIDE channel: a widget must function fully when it 404s, so nothing in
        the served contract may depend on it."""
        self.post_log({"lines": ["a-very-distinctive-marker-string"]})
        with self.builder._lock:
            self.builder._state = self.builder.build()
        state = self.state()
        self.assertNotIn("panelLog", state)
        self.assertNotIn("a-very-distinctive-marker-string", json.dumps(state))
        self.assertEqual(state["schema"], 5)

    def test_nothing_is_persisted(self):
        """In-memory ONLY, deliberately - a scratch channel, not history. Nothing the
        widget composes may reach a file that backups pick up and somebody later reads as
        a record of what happened."""
        marker = b"a-very-distinctive-marker-string"
        self.post_log({"lines": [marker.decode()]})
        for path in Path(self._tmp.name).rglob("*"):
            if path.is_file():
                self.assertNotIn(marker, path.read_bytes(), path)

    def test_the_ring_does_not_survive_a_new_builder(self):
        """Process state, not a file. A restarted crabd starts empty and says so."""
        self.post_log({"lines": ["before"]})
        self.assertEqual(self.read_log().json()["count"], 1)
        fresh = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                   crabd.HookTracker(), StubLimits(), time.time(),
                                   self.config)
        self.assertEqual(fresh.panel_log.snapshot(), ([], 0))


# --------------------------------------------------- v0.25.0: the origin recorder (SEC-a)

class OriginRecorderTests(unittest.TestCase):
    """The recorder itself, off the socket - the SEC-a measurement enabler. Records the
    distinct Origins seen so the widget's true origin can be read from GET /v1/health."""

    # A representative browser UA (QtWebEngine widget) and a local-process UA, reused.
    BROWSER_UA = "Mozilla/5.0 (Windows) QtWebEngine/6.7 Chrome/118 Safari/537.36"
    CURL_UA = "curl/8.4.0"

    def test_distinct_origins_carry_independent_counts(self):
        rec = crabd.OriginRecorder()
        rec.record("null", self.BROWSER_UA, 1000.0)
        rec.record("null", self.BROWSER_UA, 1001.0)
        rec.record("https://a.example", self.BROWSER_UA, 1002.0)
        seen = {e["origin"]: e for e in rec.snapshot()}
        self.assertEqual(seen["null"]["count"], 2)
        self.assertEqual(seen["https://a.example"]["count"], 1)
        self.assertEqual(seen["null"]["lastSeenAt"], crabd._utc_iso(1001.0))

    def test_an_absent_origin_is_folded_to_the_literal_token(self):
        rec = crabd.OriginRecorder()
        rec.record(None, None, 1000.0)
        self.assertEqual([e["origin"] for e in rec.snapshot()], [crabd.ORIGIN_ABSENT])

    def test_a_browser_ua_classifies_as_source_browser(self):
        rec = crabd.OriginRecorder()
        rec.record("null", self.BROWSER_UA, 1000.0)
        self.assertEqual(rec.snapshot()[0]["source"], "browser")
        # Every marker in the set lands "browser", case-insensitively.
        for ua in ("Mozilla/5.0", "AppleWebKit/537", "python-Chrome-thing", "QTWEBENGINE"):
            self.assertEqual(crabd._classify_ua_source(ua), "browser", ua)

    def test_a_urllib_or_curl_ua_classifies_as_source_local(self):
        rec = crabd.OriginRecorder()
        rec.record(None, self.CURL_UA, 1000.0)
        self.assertEqual(rec.snapshot()[0]["source"], "local")
        self.assertEqual(crabd._classify_ua_source("Python-urllib/3.13"), "local")

    def test_no_ua_classifies_as_source_none(self):
        rec = crabd.OriginRecorder()
        rec.record(None, None, 1000.0)
        self.assertEqual(rec.snapshot()[0]["source"], "none")
        self.assertEqual(rec.snapshot()[0]["userAgent"], None)
        # A present-but-blank UA is "no meaningful UA" too.
        self.assertEqual(crabd._classify_ua_source(""), "none")
        self.assertEqual(crabd._classify_ua_source("   "), "none")
        self.assertEqual(crabd._classify_ua_source(None), "none")

    def test_same_origin_from_a_browser_and_a_local_process_are_two_rows(self):
        """The entire point of v0.27.0: one Origin value, two SOURCEs, two distinct rows -
        so the QtWebEngine widget is separable from a local no-Origin caller."""
        rec = crabd.OriginRecorder()
        rec.record("null", self.BROWSER_UA, 1000.0)
        rec.record("null", self.CURL_UA, 1001.0)
        rows = rec.snapshot()
        self.assertEqual(len(rows), 2)
        by_source = {r["source"]: r for r in rows}
        self.assertEqual(set(by_source), {"browser", "local"})
        self.assertEqual(by_source["browser"]["origin"], "null")
        self.assertEqual(by_source["local"]["origin"], "null")

    def test_a_browser_null_and_a_local_absent_are_cleanly_separated(self):
        """The measured failure mode: a browser sending Origin:null vs a local process
        sending no Origin must not collapse together - that separation isolates the
        widget from the notifier/curl noise."""
        rec = crabd.OriginRecorder()
        rec.record("null", self.BROWSER_UA, 1000.0)   # widget: null origin, browser UA
        rec.record(None, None, 1001.0)                # notifier: absent origin, no UA
        keyed = {(r["origin"], r["source"]) for r in rec.snapshot()}
        self.assertEqual(keyed, {("null", "browser"), (crabd.ORIGIN_ABSENT, "none")})

    def test_the_raw_ua_is_kept_but_truncated(self):
        rec = crabd.OriginRecorder()
        long_ua = "Mozilla/" + "x" * 500
        rec.record("null", long_ua, 1000.0)
        ua = rec.snapshot()[0]["userAgent"]
        self.assertEqual(len(ua), crabd.ORIGIN_UA_MAX)
        self.assertEqual(ua, long_ua[:crabd.ORIGIN_UA_MAX])

    def test_the_most_recent_raw_ua_wins_within_a_pair(self):
        """Two browser UAs at the same origin collapse to one (origin,source) row; the
        entry keeps the LAST raw UA as the freshest evidence of which build polled."""
        rec = crabd.OriginRecorder()
        rec.record("null", "Mozilla/5.0 build-A", 1000.0)
        rec.record("null", "Mozilla/5.0 build-B", 1001.0)
        rows = rec.snapshot()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 2)
        self.assertEqual(rows[0]["userAgent"], "Mozilla/5.0 build-B")

    def test_the_distinct_set_is_lru_capped_under_a_flood(self):
        """The recorder sits on the unauthenticated request path, so a page firing
        requests with random forged Origins must not balloon it - only the most-recently-
        seen ORIGIN_RECORDER_MAX survive."""
        rec = crabd.OriginRecorder(limit=32)
        for i in range(5000):
            rec.record(f"https://{i}.example", None, 1000.0 + i)
        snap = rec.snapshot()
        self.assertEqual(len(snap), 32)
        self.assertEqual(snap[-1]["origin"], "https://4999.example")   # newest survives
        self.assertEqual(snap[0]["origin"], "https://4968.example")    # oldest survivor

    def test_the_cap_counts_distinct_pairs_not_distinct_origins(self):
        """The cap is on the (origin, source) PAIR: ONE origin seen from N sources is N
        rows against the cap, which is why the cap was raised for the extra dimension."""
        rec = crabd.OriginRecorder(limit=4)
        # One origin, but browser + local + none = 3 distinct pairs, all retained.
        rec.record("null", self.BROWSER_UA, 1000.0)
        rec.record("null", self.CURL_UA, 1001.0)
        rec.record("null", None, 1002.0)
        self.assertEqual(len(rec.snapshot()), 3)
        # A fourth distinct pair fits; a fifth evicts the least-recently-seen.
        rec.record("https://a.example", None, 1003.0)
        rec.record("https://b.example", None, 1004.0)
        snap = rec.snapshot()
        self.assertEqual(len(snap), 4)
        # The browser/null pair (oldest, untouched) was evicted.
        self.assertNotIn(("null", "browser"), {(r["origin"], r["source"]) for r in snap})

    def test_a_repeat_pair_is_kept_young_and_outlives_a_flood(self):
        """The widget polls from ONE (origin, source) pair repeatedly - a per-record
        recency bump keeps it at the young end, so a flood of one-shot forged origins
        cannot evict it, which is the whole point of the channel."""
        rec = crabd.OriginRecorder(limit=4)
        rec.record("null", self.BROWSER_UA, 1000.0)
        for i in range(100):
            rec.record(f"https://{i}.example", None, 1001.0 + i)
            rec.record("null", self.BROWSER_UA, 1001.5 + i)
        seen = {(e["origin"], e["source"]): e for e in rec.snapshot()}
        self.assertEqual(len(seen), 4)
        self.assertIn(("null", "browser"), seen)
        self.assertEqual(seen[("null", "browser")]["count"], 101)

    def test_the_cap_is_forty_eight(self):
        self.assertEqual(crabd.ORIGIN_RECORDER_MAX, 48)

    def test_snapshot_hands_back_fresh_dicts(self):
        """A reader mutating the result must not reach into the recorder's own state."""
        rec = crabd.OriginRecorder()
        rec.record("null", None, 1000.0)
        snap = rec.snapshot()
        snap[0]["count"] = 999
        self.assertEqual(rec.snapshot()[0]["count"], 1)


class OriginRecorderEndpointTests(ServedOverASocket):
    """The recorder wired to the live request path: every GET and POST feeds it, it
    surfaces read-only in /v1/health.originsSeen, and it is NEVER in /v1/state."""

    BROWSER_UA = "Mozilla/5.0 (Windows) QtWebEngine/6.7 Chrome/118 Safari/537.36"

    def origins_seen(self):
        body = self.client.get("/v1/health", headers={"Origin": "null"}).json()
        return {e["origin"]: e for e in body["originsSeen"]}

    def rows(self):
        """Full list keyed by the (origin, source) PAIR - the origins_seen() helper keys
        on origin alone and would collapse the pairs this feature exists to separate."""
        body = self.client.get("/v1/health", headers={"Origin": "null"}).json()
        return {(e["origin"], e["source"]): e for e in body["originsSeen"]}

    def test_distinct_origins_are_recorded_with_counts(self):
        self.client.get("/v1/state", headers={"Origin": "null"})
        self.client.get("/v1/state", headers={"Origin": "null"})
        # a web-origin POST is refused 403 by the gate but recorded BEFORE it.
        self.client.post("/v1/panel-log", json.dumps({"lines": ["x"]}).encode(),
                         headers={"Origin": "https://widget.example"})
        seen = self.origins_seen()
        self.assertGreaterEqual(seen["null"]["count"], 2)
        self.assertEqual(seen["https://widget.example"]["count"], 1)
        self.assertRegex(seen["null"]["lastSeenAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_an_absent_origin_is_recorded_as_the_literal_token(self):
        self.client.get("/v1/state")   # no Origin header at all
        self.assertIn(crabd.ORIGIN_ABSENT, self.origins_seen())

    def test_origins_seen_is_never_part_of_the_state_document(self):
        """Health is diagnostic; the recorder must not leak into the widget-facing state
        contract."""
        self.client.get("/v1/state", headers={"Origin": "null"})
        self.assertNotIn("originsSeen", self.state())

    def test_a_browser_ua_is_recorded_with_source_browser_and_raw_ua(self):
        """The live-path proof of the v0.27.0 point: a QtWebEngine-style UA lands source
        'browser' and its raw UA is surfaced as evidence of which build polled."""
        self.client.get("/v1/state", headers={"Origin": "null",
                                              "User-Agent": self.BROWSER_UA})
        row = self.rows()[("null", "browser")]
        self.assertGreaterEqual(row["count"], 1)
        self.assertEqual(row["userAgent"], self.BROWSER_UA[:crabd.ORIGIN_UA_MAX])

    def test_the_same_origin_from_two_sources_makes_two_rows_over_the_wire(self):
        """One Origin, two SOURCEs (a browser UA and no UA at all) -> two distinct rows,
        so a no-Origin widget is separable from the no-Origin notifier/curl noise."""
        self.client.get("/v1/state", headers={"Origin": "null",
                                              "User-Agent": self.BROWSER_UA})
        self.client.get("/v1/state", headers={"Origin": "null"})   # no UA -> source none
        rows = self.rows()
        self.assertIn(("null", "browser"), rows)
        self.assertIn(("null", "none"), rows)

    def test_the_ua_classification_never_lets_a_cross_origin_request_through(self):
        """CRITICAL: source is DIAGNOSTIC ONLY and NEVER consulted by the origin gate. A
        cross-origin http(s) request carrying a browser UA is still refused 403 on both
        verbs - proving the attacker-controlled UA cannot buy a gate bypass. It is still
        RECORDED (before the gate), which is the recorder's whole job."""
        get = self.client.get("/v1/state", headers={"Origin": "https://evil.example",
                                                    "User-Agent": self.BROWSER_UA})
        self.assertEqual(get.status, 403)
        post = self.client.post("/v1/panel-log", json.dumps({"lines": ["x"]}).encode(),
                                headers={"Origin": "https://evil.example",
                                         "User-Agent": self.BROWSER_UA})
        self.assertEqual(post.status, 403)
        # Recorded before refusal, and classified 'browser' - a label on a refused row,
        # not a decision.
        self.assertIn(("https://evil.example", "browser"), self.rows())


# =====================================================================================
# v0.28.0 - `sessions[].contextWindowTokens`: the ctx-fill gauge's DENOMINATOR.
#
# THE DEFECT IT CLOSES: the widget derived this only from a [1m]/[200k] marker in the
# model id, and the live ids on this host carry none (measured 2026-08-28 off the
# transcripts: "claude-fable-5", "claude-opus-5"), so the context hairline never drew on
# a real session - a shipped feature that was invisible in production.
#
# THE RULE EVERY TEST HERE DEFENDS: unknown is null, and null draws no bar. There is no
# model-name table on either side of this wire, so a test that ever asserts a window for
# a model id its own fixture did not state is asserting an invention.
#
# NOTHING HERE TOUCHES THE NETWORK OR THE OPERATOR'S TOKEN: setUpModule points
# CREDENTIALS_FILE at a path that does not exist, every catalog is built on an injected
# credentials file, and the one suite that exercises the HTTP branch stubs urlopen.
# =====================================================================================


def models_payload(*rows):
    """The live shape, measured 2026-08-28 against GET /v1/models: a `data` array whose
    rows carry `id`, `max_input_tokens` (the window) and `max_tokens` (the OUTPUT cap)."""
    return {"data": [{"id": i, "display_name": i, "type": "model",
                      "max_input_tokens": w, "max_tokens": 128000} for i, w in rows]}


LIVE_MODELS = models_payload(("claude-opus-5", 1000000), ("claude-sonnet-5", 1000000),
                             ("claude-fable-5", 1000000), ("claude-sonnet-4-6", 1000000),
                             ("claude-haiku-4-5-20251001", 200000))


class FakeHTTP:
    """A stand-in for urllib.request.urlopen. `outcome` is a body string, or an
    exception to raise - the two shapes _fetch has to survive."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0
        self.headers = None

    def __call__(self, request, timeout=None):
        self.calls += 1
        self.headers = dict(getattr(request, "headers", {}) or {})
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self

    # context-manager + read(), which is all _fetch uses of the response
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.outcome.encode("utf-8")


class ModelMarkerTests(unittest.TestCase):
    """`_marker_window` / `_model_base_id` - crabd's half of the marker the widget has
    parsed since v0.22.0. The two must agree, or one model id gets two different
    denominators depending on which crabd is serving it."""

    def test_the_measured_marker_shapes_parse(self):
        self.assertEqual(crabd._marker_window("claude-opus-5[1m]"), 1000000)
        self.assertEqual(crabd._marker_window("claude-sonnet-4-6[200k]"), 200000)
        self.assertEqual(crabd._marker_window("claude-opus-5[1M]"), 1000000)
        self.assertEqual(crabd._marker_window("x[1.5m]"), 1500000)

    def test_an_unmarked_id_has_no_marker_window(self):
        """The whole reason this feature exists - every live id is this case."""
        for model in ("claude-fable-5", "claude-opus-5", "", None, 5, ["claude-opus-5"]):
            self.assertIsNone(crabd._marker_window(model), model)

    def test_a_zero_or_unrepresentable_marker_is_unknown_not_a_denominator(self):
        """A 0-token window would divide by zero; a 400-digit one overflows int(). Both
        are 'unknown', because this runs on the build path and must not raise."""
        self.assertIsNone(crabd._marker_window("x[0k]"))
        self.assertIsNone(crabd._marker_window("x[" + "9" * 400 + "m]"))

    def test_the_base_id_strips_the_marker_and_nothing_else(self):
        self.assertEqual(crabd._model_base_id("claude-opus-5[1m]"), "claude-opus-5")
        self.assertEqual(crabd._model_base_id("claude-fable-5"), "claude-fable-5")
        self.assertIsNone(crabd._model_base_id(""))
        self.assertIsNone(crabd._model_base_id(None))

    def test_stripping_is_a_lookup_key_and_never_written_back(self):
        """CON-b: `model` is served VERBATIM. The catalog needs an API id to look up, so
        it strips - and the caller's string must be untouched by that."""
        with tempfile.TemporaryDirectory() as td:
            catalog = crabd.ModelCatalog(credentials_file=Path(td) / "none.json")
        catalog._windows = {"claude-opus-5": 1000000}
        catalog._fetched_at = 1000.0
        model = "claude-opus-5[1m]"
        self.assertEqual(catalog.window(model, 1000.0), 1000000)
        self.assertEqual(model, "claude-opus-5[1m]")


class ModelCatalogMappingTests(unittest.TestCase):
    """`ModelCatalog.map_payload` - the API document -> {id: window}."""

    def test_the_measured_live_payload_maps(self):
        """The values are the ones the live endpoint returned on 2026-08-28. They are
        FIXTURE data here, never a table crabd may fall back on."""
        self.assertEqual(crabd.ModelCatalog.map_payload(LIVE_MODELS),
                         {"claude-opus-5": 1000000, "claude-sonnet-5": 1000000,
                          "claude-fable-5": 1000000, "claude-sonnet-4-6": 1000000,
                          "claude-haiku-4-5-20251001": 200000})

    def test_max_tokens_is_never_mistaken_for_the_window(self):
        """THE MUTATION GUARD. `max_tokens` is the OUTPUT cap - 128000 beside a 1000000
        input window - and dividing contextTokens by it would gauge every card on that
        model at roughly eight times its true fill. A wrong bar looks exactly like a
        right one, so this is asserted rather than left to the reader."""
        mapped = crabd.ModelCatalog.map_payload(LIVE_MODELS)
        self.assertEqual(mapped["claude-opus-5"], 1000000)
        self.assertNotEqual(mapped["claude-opus-5"], 128000)

    def test_a_row_with_no_usable_window_is_dropped_not_defaulted(self):
        payload = models_payload(("good", 200000))
        payload["data"].extend([
            {"id": "no-window", "max_tokens": 64000},
            {"id": "null-window", "max_input_tokens": None},
            {"id": "zero", "max_input_tokens": 0},
            {"id": "negative", "max_input_tokens": -1},
            {"id": "", "max_input_tokens": 200000},
            {"max_input_tokens": 200000},
            "not-a-dict",
        ])
        self.assertEqual(crabd.ModelCatalog.map_payload(payload), {"good": 200000})

    def test_a_bool_or_non_finite_window_is_refused_not_gauged(self):
        """CD-10 at this parse boundary: `max_input_tokens: true` would int() to a
        1-token window and pin every card on that model at 100%."""
        for bad in (True, False, float("nan"), float("inf"), "200000", 1e309):
            self.assertIsNone(crabd.ModelCatalog.map_payload(
                models_payload(("m", bad))), bad)

    def test_an_unusable_document_is_none_not_an_empty_catalog(self):
        """None and {} would behave the same today, but None is the honest one: an empty
        dict says 'the catalog has no models', which is a claim the fetch cannot make."""
        for bad in (None, [], "data", {}, {"data": None}, {"data": {}},
                    {"data": []}, {"models": [{"id": "x", "max_input_tokens": 1}]}):
            self.assertIsNone(crabd.ModelCatalog.map_payload(bad), bad)


class ModelCatalogCacheTests(unittest.TestCase):
    """TTL, the failure throttle, and what a failed refresh does to a good catalog."""

    def catalog(self, fetches):
        """`fetches` is a list of _fetch return values, consumed in order."""
        with tempfile.TemporaryDirectory() as td:
            catalog = crabd.ModelCatalog(credentials_file=Path(td) / "none.json")
        self.calls = []
        pending = list(fetches)

        def fake_fetch():
            self.calls.append(True)
            return pending.pop(0) if pending else None

        catalog._fetch = fake_fetch
        return catalog

    def test_a_hit_serves_the_window_and_an_unknown_id_serves_none(self):
        catalog = self.catalog([{"claude-opus-5": 1000000}])
        now = time.time()
        self.assertEqual(catalog.window("claude-opus-5", now), 1000000)
        self.assertIsNone(catalog.window("claude-nonesuch-9", now))
        self.assertIsNone(catalog.window(None, now))

    def test_a_hit_is_cached_for_the_ttl_and_refetched_after_it(self):
        catalog = self.catalog([{"a": 1}, {"a": 2}])
        now = time.time()
        catalog.window("a", now)
        catalog.window("a", now + crabd.MODELS_TTL_SEC - 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(catalog.window("a", now + crabd.MODELS_TTL_SEC + 1), 2)
        self.assertEqual(len(self.calls), 2)

    def test_a_failure_is_absence_and_the_retry_is_throttled(self):
        """The builder calls window() once per session row, every REFRESH_INTERVAL_SEC.
        Without the throttle an expired token would become a request flood - which is
        how the usage endpoint earned its 429 lockout on 2026-08-26."""
        catalog = self.catalog([None])
        now = time.time()
        for offset in (0, 0.01, 1, 60, crabd.MODELS_RETRY_SEC - 1):
            self.assertIsNone(catalog.window("a", now + offset), offset)
        self.assertEqual(len(self.calls), 1)

    def test_the_throttle_expires_and_the_next_attempt_runs(self):
        """A control that cannot fire is worse than none: prove the retry is delayed,
        not abandoned."""
        catalog = self.catalog([None, {"a": 7}])
        now = time.time()
        self.assertIsNone(catalog.window("a", now))
        self.assertEqual(catalog.window("a", now + crabd.MODELS_RETRY_SEC + 1), 7)

    def test_a_failed_refresh_keeps_the_last_good_catalog(self):
        """A model's window is a fixed property, not a drifting reading, so an hour-old
        catalog is the same answer - and blanking it would put every bar on the panel
        out for the length of a token expiry."""
        catalog = self.catalog([{"a": 5}, None])
        now = time.time()
        self.assertEqual(catalog.window("a", now), 5)
        later = now + crabd.MODELS_TTL_SEC + 1
        self.assertEqual(catalog.window("a", later), 5)
        self.assertEqual(len(self.calls), 2)          # it did try again

    def test_one_build_over_many_rows_makes_at_most_one_request(self):
        """window() is called per session row inside one build(), so the throttle has to
        hold WITHIN a build and not only across them."""
        catalog = self.catalog([None])
        now = time.time()
        for _ in range(14):
            catalog.window("claude-opus-5", now)
        self.assertEqual(len(self.calls), 1)


class ModelCatalogFetchTests(unittest.TestCase):
    """`_fetch` - every degradation path, offline. No token leaves this file."""

    TOKEN = "sk-fake-not-a-real-token"

    def creds(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / ".credentials.json"
        path.write_text(body if isinstance(body, str) else json.dumps(body),
                        encoding="utf-8")
        return crabd.ModelCatalog(credentials_file=path)

    def live_creds(self):
        return self.creds({"claudeAiOauth": {
            "accessToken": self.TOKEN,
            "expiresAt": (time.time() + 3600) * 1000, "subscriptionType": "max"}})

    def with_http(self, outcome):
        fake = FakeHTTP(outcome)
        original = crabd.urllib.request.urlopen
        crabd.urllib.request.urlopen = fake
        self.addCleanup(lambda: setattr(crabd.urllib.request, "urlopen", original))
        return fake

    def test_a_missing_credentials_file_is_absence_not_an_error(self):
        with tempfile.TemporaryDirectory() as td:
            catalog = crabd.ModelCatalog(credentials_file=Path(td) / "gone.json")
            self.assertIsNone(catalog._fetch())

    def test_an_unreadable_or_tokenless_credentials_file_is_absence(self):
        for body in ("{not json", {}, {"claudeAiOauth": {}},
                     {"claudeAiOauth": {"accessToken": ""}},
                     {"claudeAiOauth": {"accessToken": 12345}}):
            self.assertIsNone(self.creds(body)._fetch(), body)

    def test_an_expired_token_never_reaches_the_wire(self):
        """The same honest degradation limits.available:false gives - and it must not
        spend a request to learn what the file already says."""
        fake = self.with_http(json.dumps(LIVE_MODELS))
        catalog = self.creds({"claudeAiOauth": {
            "accessToken": self.TOKEN, "expiresAt": (time.time() - 60) * 1000}})
        self.assertIsNone(catalog._fetch())
        self.assertEqual(fake.calls, 0)

    def test_a_live_token_maps_the_reply(self):
        self.with_http(json.dumps(LIVE_MODELS))
        self.assertEqual(self.live_creds()._fetch()["claude-fable-5"], 1000000)

    def test_the_request_carries_the_headers_measured_against_the_live_endpoint(self):
        """Bearer + the oauth beta header answered 200 on 2026-08-28. An API-key header
        is NOT what this process holds, so a rewrite to x-api-key would 401 forever."""
        fake = self.with_http(json.dumps(LIVE_MODELS))
        self.live_creds()._fetch()
        headers = {k.lower(): v for k, v in fake.headers.items()}
        self.assertEqual(headers["authorization"], "Bearer " + self.TOKEN)
        self.assertEqual(headers["anthropic-beta"], crabd.USAGE_BETA)
        self.assertEqual(headers["anthropic-version"], crabd.MODELS_API_VERSION)

    def test_every_transport_failure_is_the_same_absence(self):
        errors = crabd.urllib.error
        for outcome in (errors.HTTPError(crabd.MODELS_URL, 401, "no", {}, None),
                        errors.HTTPError(crabd.MODELS_URL, 429, "slow down", {}, None),
                        errors.HTTPError(crabd.MODELS_URL, 500, "boom", {}, None),
                        errors.URLError("dns"), TimeoutError("slow"), OSError("reset")):
            self.with_http(outcome)
            self.assertIsNone(self.live_creds()._fetch(), outcome)

    def test_an_unparseable_or_unexpected_body_is_absence(self):
        for body in ("{not json", "[]", '{"data": "nope"}', '{"data": []}'):
            self.with_http(body)
            self.assertIsNone(self.live_creds()._fetch(), body)

    def test_the_catalogs_whole_vocabulary_is_ints(self):
        """LimitsReader composes its own error text because an exception's text could
        echo a request. This class has no error text AT ALL - it serves an int or an
        absence - which is why no token can ride out of it."""
        self.with_http(json.dumps(LIVE_MODELS))
        for value in self.live_creds()._fetch().values():
            self.assertIsInstance(value, int)


class ContextWindowPrecedenceTests(TempProjects):
    """StateBuilder._context_window - three sources, most specific first."""

    SID = "cw-1"

    def catalog(self, windows):
        with tempfile.TemporaryDirectory() as td:
            catalog = crabd.ModelCatalog(credentials_file=Path(td) / "none.json")
        catalog._fetch = lambda: dict(windows)
        return catalog

    def builder(self, statusline=None, models=None):
        return crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                  crabd.HookTracker(), StubLimits(), time.time(),
                                  statusline=statusline, models=models)

    def statusline(self, size, at, session=None):
        reader = crabd.StatusLineReader()
        reader.ingest({"session_id": session or self.SID,
                       "context_window": {"total_input_tokens": 5000,
                                          "current_usage": {"input_tokens": 1},
                                          "context_window_size": size}}, at)
        return reader

    def info(self, model, context_ts=0.0):
        return {"model": model, "context_tokens": 5000, "context_ts": context_ts}

    def test_nothing_known_is_none_and_never_a_default_window(self):
        """The rule the whole feature rests on. A builder with no status line, an
        unmarked model and no catalog knows nothing - and says so."""
        now = time.time()
        self.assertIsNone(self.builder()._context_window(
            self.SID, self.info("claude-fable-5"), now))

    def test_the_catalog_lights_the_bar_for_an_unmarked_live_model(self):
        """THE DEFECT, closed: this is the case that rendered no bar before 0.28.0."""
        now = time.time()
        builder = self.builder(models=self.catalog({"claude-fable-5": 1000000}))
        self.assertEqual(
            builder._context_window(self.SID, self.info("claude-fable-5"), now), 1000000)

    def test_a_model_the_catalog_has_never_heard_of_is_still_none(self):
        now = time.time()
        builder = self.builder(models=self.catalog({"claude-opus-5": 1000000}))
        self.assertIsNone(
            builder._context_window(self.SID, self.info("claude-newthing-9"), now))

    def test_the_marker_beats_the_catalog(self):
        """THE ORDERING TEST, and the one that costs a real number if it flips: the
        marker is THIS SESSION's window, the catalog is the MODEL's ceiling. Serving
        1000000 for a session the feed said is running at 200k gauges the card at a
        fifth of its true fill - and a wrong bar looks exactly like a right one."""
        now = time.time()
        builder = self.builder(models=self.catalog({"claude-sonnet-4-6": 1000000}))
        self.assertEqual(builder._context_window(
            self.SID, self.info("claude-sonnet-4-6[200k]"), now), 200000)

    def test_the_status_line_beats_the_marker(self):
        now = time.time()
        builder = self.builder(statusline=self.statusline(300000, now - 1),
                               models=self.catalog({"claude-opus-5": 1000000}))
        self.assertEqual(builder._context_window(
            self.SID, self.info("claude-opus-5[1m]", context_ts=now), now), 300000)

    def test_a_stale_status_line_falls_through_to_the_marker(self):
        """CD-36's freshness contest, applied to the size: a retained row can name a
        model the session has since left, and the marker underneath had it right."""
        now = time.time()
        builder = self.builder(statusline=self.statusline(300000, now - 7000),
                               models=self.catalog({"claude-opus-5": 1000000}))
        self.assertEqual(builder._context_window(
            self.SID, self.info("claude-opus-5[1m]", context_ts=now - 30), now), 1000000)

    def test_a_status_line_that_carries_no_size_falls_through(self):
        """A document with a context_window block but no context_window_size asserts
        nothing about the denominator - unlike contextTokens, where 'the status line says
        unknown' is itself a fact that outranks the transcript."""
        now = time.time()
        reader = crabd.StatusLineReader()
        reader.ingest({"session_id": self.SID,
                       "context_window": {"total_input_tokens": 5000,
                                          "current_usage": {"input_tokens": 1}}},
                      now - 1)
        builder = self.builder(statusline=reader,
                               models=self.catalog({"claude-opus-5": 1000000}))
        self.assertEqual(builder._context_window(
            self.SID, self.info("claude-opus-5", context_ts=now), now), 1000000)

    def test_a_zero_or_absurd_size_from_the_status_line_is_unknown(self):
        now = time.time()
        for size in (0, -1, True, "200000", float("inf")):
            builder = self.builder(statusline=self.statusline(size, now - 1))
            self.assertIsNone(builder._context_window(
                self.SID, self.info("claude-fable-5", context_ts=now), now), size)

    def test_another_sessions_status_line_never_lends_this_one_a_window(self):
        now = time.time()
        builder = self.builder(statusline=self.statusline(300000, now - 1,
                                                          session="somebody-else"))
        self.assertIsNone(builder._context_window(
            self.SID, self.info("claude-fable-5", context_ts=now), now))

    def test_the_contextTokens_pair_is_untouched_by_all_of_this(self):
        """_context and _context_window are separate on purpose: the fill and the window
        have different source priorities, and folding them would have made the marker a
        source for contextTokens."""
        now = time.time()
        builder = self.builder(models=self.catalog({"claude-fable-5": 1000000}))
        self.assertEqual(builder._context(self.SID, self.info("claude-fable-5"), now),
                         {"contextTokens": 5000, "contextSource": "transcript"})


class ContextWindowSerializationTests(TempProjects):
    """The member on a built row: always present, an int or null, never absent."""

    SID = "dddddddd-0000-0000-0000-00000000000d"

    def row(self, model, models=None, statusline=None):
        now = time.time()
        write_jsonl(self.session_path(self.SID),
                    [user_line("go", now - 60),
                     assistant_line("req_1", now - 30, output=10, model=model)],
                    mtime=now - 5)
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), now,
                                     statusline=statusline, models=models)
        state = builder.build(now=now)
        return next(r for r in state["sessions"] if r["id"] == self.SID)

    def catalog(self, windows):
        with tempfile.TemporaryDirectory() as td:
            catalog = crabd.ModelCatalog(credentials_file=Path(td) / "none.json")
        catalog._fetch = lambda: dict(windows)
        return catalog

    def test_the_key_is_present_and_null_when_unknown(self):
        """Present-and-null, like queuedContinue: the KEY is the widget's feature
        detection, so it rides even on a builder that knows nothing."""
        row = self.row("claude-fable-5")
        self.assertIn("contextWindowTokens", row)
        self.assertIsNone(row["contextWindowTokens"])

    def test_a_known_window_is_a_positive_int_beside_contextTokens(self):
        row = self.row("claude-fable-5", models=self.catalog({"claude-fable-5": 1000000}))
        self.assertEqual(row["contextWindowTokens"], 1000000)
        self.assertIsInstance(row["contextWindowTokens"], int)
        self.assertIsNotNone(row["contextTokens"])

    def test_the_model_string_is_still_served_verbatim_beside_it(self):
        """CON-b. The catalog strips the marker to build a lookup key; the served label
        must not move, or the widget's own fallback parse loses its input."""
        row = self.row("claude-opus-5[1m]",
                       models=self.catalog({"claude-opus-5": 1000000}))
        self.assertEqual(row["model"], "claude-opus-5[1m]")
        self.assertEqual(row["contextWindowTokens"], 1000000)   # the marker's own value

    def test_the_member_survives_the_json_round_trip_as_a_number(self):
        row = self.row("claude-fable-5", models=self.catalog({"claude-fable-5": 200000}))
        served = json.loads(crabd.dump_state({"sessions": [row]}).decode("utf-8"))
        self.assertEqual(served["sessions"][0]["contextWindowTokens"], 200000)
