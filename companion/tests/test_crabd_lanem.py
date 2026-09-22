"""Lane M, part 2: `activity`, `mode`, `filesTouched`, `promptQueue`, `compaction`
and `todos` (contract v0.35.0, provisional label).

Schema stays 5 and all six are presence-gated, so the HONESTY cases carry as much weight
as the happy ones: a zero, an empty list or a null in any of these is a claim crabd is not
entitled to make, and there is a test for each of them being ABSENT instead. The moved-
session class is the SCA-001 P1's regression guard.

The findings are in test_crabd_lanem_findings.py. Fixtures are fictional; nothing here
reads the operator's own transcripts, and no command text ever reaches a served member.
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crabd  # noqa: E402
from test_crabd import (StubHost, StubLimits, TempProjects, assistant_line,  # noqa: E402
                        iso, user_line, write_jsonl)


# --------------------------------------------------------------- module isolation

_MODULE_TMP = None


def setUpModule():
    """The same hard isolation the other modules state, plus CRABD_LOG_FILE: v0.35.0
    gives crabd its own log and several tests here make it write."""
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


# ------------------------------------------------------------------- shared fixtures

def tool_use_line(request_id, ts, name, inp, cwd="D:\\work\\acme", text=None):
    """An assistant record carrying ONE tool_use block. The shapes are the ones measured
    on a real install: Bash/PowerShell/Agent take `description`, Edit/Write/Read take
    `file_path`, Grep/Glob take `pattern`, TodoWrite takes `todos`."""
    line = assistant_line(request_id, ts, cwd=cwd)
    blocks = [{"type": "tool_use", "id": f"toolu_{request_id}", "name": name,
               "input": inp}]
    if text is not None:
        blocks.insert(0, {"type": "text", "text": text})
    line["message"]["content"] = blocks
    return line


def mode_line(value, session_id="s-mode"):
    """Measured shape: three keys and NO timestamp."""
    return {"type": "mode", "mode": value, "sessionId": session_id}


def queue_line(operation, ts, session_id="s-queue", content=None):
    line = {"type": "queue-operation", "operation": operation, "timestamp": iso(ts),
            "sessionId": session_id}
    if content is not None:
        line["content"] = content
    return line


def compact_boundary_line(ts, cwd="D:\\work\\acme"):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": iso(ts),
            "cwd": cwd, "compactMetadata": {"trigger": "auto", "preTokens": 172000}}


def todo_write_line(request_id, ts, todos):
    return tool_use_line(request_id, ts, "TodoWrite", {"todos": todos})


class LaneMFacts(TempProjects):
    """Parse one synthetic transcript into a FileFacts."""

    def facts(self, objects, session_id="s-lane-m", project="P--acme", mtime=None):
        path = self.projects / project / f"{session_id}.jsonl"
        write_jsonl(path, objects, mtime=mtime)
        facts = crabd.FileFacts(path, session_id, False)
        facts.refresh()
        return facts


# ======================================================= PART 2: the six new members

class ActivityTests(LaneMFacts):
    """`sessions[].activity` - the newest tool_use of the CURRENT turn."""

    def test_the_detail_is_the_description_for_bash(self):
        now = time.time()
        facts = self.facts([
            user_line("run the suite", now - 60),
            tool_use_line("req_1", now - 30, "Bash",
                          {"command": "export TOKEN=hunter2 && pytest -q",
                           "description": "run the tests", "timeout": 120000})])
        self.assertEqual(facts.turn_tool["tool"], "Bash")
        self.assertEqual(facts.turn_tool["detail"], "run the tests")

    def test_the_command_text_is_never_served(self):
        """⚠ THE POINT OF THE ALLOWLIST. A shell command can carry a secret, and this
        member is rendered on a screen on a desk. A Bash block with NO description serves
        a null detail; it never falls back to the command."""
        now = time.time()
        facts = self.facts([
            user_line("go", now - 60),
            tool_use_line("req_1", now - 30, "Bash",
                          {"command": "curl -H 'Authorization: Bearer sk-secret' x",
                           "timeout": 5000})])
        self.assertEqual(facts.turn_tool["tool"], "Bash")
        self.assertIsNone(facts.turn_tool["detail"])

    def test_the_detail_is_the_leaf_for_the_file_tools(self):
        now = time.time()
        for name in ("Edit", "Write", "Read"):
            with self.subTest(tool=name):
                facts = self.facts(
                    [user_line("go", now - 60),
                     tool_use_line("req_1", now - 30, name,
                                   {"file_path": "D:\\work\\acme\\src\\widget.py",
                                    "old_string": "a", "new_string": "b"})],
                    session_id=f"s-{name}")
                self.assertEqual(facts.turn_tool["detail"], "widget.py")

    def test_the_detail_is_the_pattern_for_the_search_tools(self):
        now = time.time()
        for name in ("Grep", "Glob"):
            with self.subTest(tool=name):
                facts = self.facts(
                    [user_line("go", now - 60),
                     tool_use_line("req_1", now - 30, name,
                                   {"pattern": "def handle_.*", "output_mode": "content"})],
                    session_id=f"s-{name}")
                self.assertEqual(facts.turn_tool["detail"], "def handle_.*")

    def test_an_unknown_tool_serves_a_null_detail_and_still_counts(self):
        now = time.time()
        facts = self.facts([
            user_line("go", now - 60),
            tool_use_line("req_1", now - 30, "SomeFutureTool", {"anything": "at all"})])
        self.assertEqual(facts.turn_tool["tool"], "SomeFutureTool")
        self.assertIsNone(facts.turn_tool["detail"])
        self.assertEqual(facts.turn_tool_calls, 1)

    def test_the_detail_is_capped_at_eighty(self):
        now = time.time()
        facts = self.facts([
            user_line("go", now - 60),
            tool_use_line("req_1", now - 30, "Bash",
                          {"command": "x", "description": "d" * 400})])
        self.assertLessEqual(len(facts.turn_tool["detail"]), crabd.ACTIVITY_DETAIL_MAX)

    def test_the_counter_resets_at_the_next_prompt(self):
        """`callsThisTurn` is this TURN, not this session. A counter that only grows is a
        session total wearing a turn's name."""
        now = time.time()
        facts = self.facts([
            user_line("first", now - 300),
            tool_use_line("req_1", now - 290, "Bash", {"description": "one"}),
            tool_use_line("req_2", now - 280, "Bash", {"description": "two"}),
            tool_use_line("req_3", now - 270, "Bash", {"description": "three"}),
            user_line("second", now - 60),
            tool_use_line("req_4", now - 30, "Read", {"file_path": "/srv/app/main.py"})])
        self.assertEqual(facts.turn_tool_calls, 1)
        self.assertEqual(facts.turn_tool["detail"], "main.py")

    def test_a_tool_result_does_not_reset_the_turn(self):
        """A tool_result arrives as a user record with LIST content. Treating it as a
        prompt would reset the counter after every single call."""
        now = time.time()
        facts = self.facts([
            user_line("go", now - 300),
            tool_use_line("req_1", now - 290, "Bash", {"description": "one"}),
            {"type": "user", "timestamp": iso(now - 285), "cwd": "D:\\work\\acme",
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_req_1", "content": "ok"}]}},
            tool_use_line("req_2", now - 280, "Bash", {"description": "two"})])
        self.assertEqual(facts.turn_tool_calls, 2)

    def test_served_only_while_working(self):
        now = time.time()
        info = {"turn_tool": {"tool": "Bash", "detail": "run the tests", "at": now},
                "turn_tool_calls": 7, "mtime": now}
        row = crabd.StateBuilder._lane_m_session_extras(info, None, "working")
        self.assertEqual(row["activity"]["tool"], "Bash")
        self.assertEqual(row["activity"]["callsThisTurn"], 7)
        for state in ("idle", "done", "needs_input"):
            with self.subTest(state=state):
                self.assertNotIn("activity", crabd.StateBuilder._lane_m_session_extras(
                    info, None, state))

    def test_absent_when_the_turn_has_used_no_tool(self):
        row = crabd.StateBuilder._lane_m_session_extras(
            {"turn_tool": None, "turn_tool_calls": 0, "mtime": 0.0}, None, "working")
        self.assertNotIn("activity", row)


class ModeTests(LaneMFacts):
    """`sessions[].mode` - the latest `mode` record, passed through lower-cased."""

    def test_the_newest_record_wins(self):
        facts = self.facts([mode_line("normal"), mode_line("plan"),
                            mode_line("acceptEdits")])
        self.assertEqual(facts.mode, "acceptedits")

    def test_a_mode_this_build_has_never_heard_of_is_passed_through(self):
        """A whitelist here would silently drop a mode a later CLI introduces, and the
        panel would show nothing while the session was in it."""
        facts = self.facts([mode_line("someFutureMode")])
        self.assertEqual(facts.mode, "somefuturemode")

    def test_absent_when_no_mode_record_exists(self):
        now = time.time()
        facts = self.facts([assistant_line("req_1", now - 10)])
        self.assertIsNone(facts.mode)
        self.assertNotIn("mode", crabd.StateBuilder._lane_m_session_extras(
            {"mode": None, "mtime": 0.0}, None, "working"))

    def test_a_junk_mode_value_is_ignored(self):
        facts = self.facts([mode_line("plan"), {"type": "mode", "mode": 7},
                            {"type": "mode", "mode": "   "}])
        self.assertEqual(facts.mode, "plan")


class FilesTouchedTests(LaneMFacts):
    """`sessions[].filesTouched` - distinct Edit and Write paths, newest five leaves."""

    def test_distinct_paths_are_counted_once(self):
        now = time.time()
        facts = self.facts([
            user_line("go", now - 100),
            tool_use_line("r1", now - 90, "Edit", {"file_path": "D:\\work\\acme\\a.py"}),
            tool_use_line("r2", now - 80, "Edit", {"file_path": "D:\\work\\acme\\a.py"}),
            tool_use_line("r3", now - 70, "Write", {"file_path": "D:\\work\\acme\\b.py"})])
        self.assertEqual(len(facts.files_touched), 2)

    def test_read_does_not_count_as_touched(self):
        """`filesTouched` answers 'what has this session CHANGED'. Folding reads in would
        make every card claim a hundred files on a session that changed none."""
        now = time.time()
        facts = self.facts([
            user_line("go", now - 100),
            tool_use_line("r1", now - 90, "Read", {"file_path": "D:\\work\\acme\\a.py"})])
        self.assertEqual(facts.files_touched, {})

    def test_recent_is_the_last_five_leaves_newest_first(self):
        now = time.time()
        objects = [user_line("go", now - 200)]
        for n in range(8):
            objects.append(tool_use_line(f"r{n}", now - 100 + n, "Edit",
                                         {"file_path": f"D:\\work\\acme\\f{n}.py"}))
        facts = self.facts(objects)
        info = {"files_touched": dict(facts.files_touched), "mtime": now}
        row = crabd.StateBuilder._lane_m_session_extras(info, None, "working")
        self.assertEqual(row["filesTouched"]["count"], 8)
        self.assertEqual(row["filesTouched"]["recent"],
                         ["f7.py", "f6.py", "f5.py", "f4.py", "f3.py"])

    def test_two_files_with_the_same_leaf_are_two_files(self):
        now = time.time()
        facts = self.facts([
            user_line("go", now - 100),
            tool_use_line("r1", now - 90, "Edit",
                          {"file_path": "D:\\work\\acme\\api\\config.py"}),
            tool_use_line("r2", now - 80, "Edit",
                          {"file_path": "D:\\work\\acme\\web\\config.py"})])
        info = {"files_touched": dict(facts.files_touched), "mtime": now}
        row = crabd.StateBuilder._lane_m_session_extras(info, None, "working")
        self.assertEqual(row["filesTouched"]["count"], 2)
        self.assertEqual(row["filesTouched"]["recent"], ["config.py", "config.py"])

    def test_absent_when_nothing_was_touched(self):
        row = crabd.StateBuilder._lane_m_session_extras(
            {"files_touched": {}, "mtime": 0.0}, None, "working")
        self.assertNotIn("filesTouched", row)


class PromptQueueTests(LaneMFacts):
    """`sessions[].promptQueue` - Claude Code's own typed-ahead depth."""

    def test_enqueue_minus_dequeue_and_remove(self):
        now = time.time()
        facts = self.facts([queue_line("enqueue", now - 50), queue_line("enqueue", now - 40),
                            queue_line("enqueue", now - 30), queue_line("dequeue", now - 20),
                            queue_line("remove", now - 10)])
        self.assertEqual(facts.queue_depth, 1)

    def test_a_negative_depth_is_floored_at_the_serve(self):
        """crabd can start reading a transcript mid-session and meet a dequeue whose
        enqueue it never saw. A negative depth is arithmetic, not a queue."""
        now = time.time()
        facts = self.facts([queue_line("dequeue", now - 20), queue_line("dequeue", now - 10)])
        self.assertLess(facts.queue_depth, 0)
        row = crabd.StateBuilder._lane_m_session_extras(
            {"queue_depth": facts.queue_depth, "mtime": now}, None, "working")
        self.assertNotIn("promptQueue", row)

    def test_an_unknown_operation_moves_nothing(self):
        now = time.time()
        facts = self.facts([queue_line("enqueue", now - 30),
                            queue_line("reorder", now - 20)])
        self.assertEqual(facts.queue_depth, 1)

    def test_absent_when_the_queue_is_empty(self):
        row = crabd.StateBuilder._lane_m_session_extras(
            {"queue_depth": 0, "mtime": 0.0}, None, "working")
        self.assertNotIn("promptQueue", row)


class CompactionTests(LaneMFacts):
    """`sessions[].compaction` - the boundary records plus the PreCompact hold."""

    def test_boundaries_are_counted_and_dated(self):
        now = time.time()
        facts = self.facts([compact_boundary_line(now - 3600),
                            assistant_line("r1", now - 1800),
                            compact_boundary_line(now - 600)])
        self.assertEqual(facts.compactions, 2)
        self.assertEqual(facts.compaction_ts, crabd._parse_ts(iso(now - 600)))

    def test_in_progress_while_the_hook_is_newer_than_the_transcript(self):
        now = time.time()
        hook = {"precompact_at": now - 5}
        row = crabd.StateBuilder._lane_m_session_extras(
            {"compactions": 0, "compaction_ts": 0.0, "mtime": now - 60}, hook, "working")
        self.assertTrue(row["compaction"]["inProgress"])
        self.assertEqual(row["compaction"]["count"], 0)
        self.assertIsNone(row["compaction"]["lastAt"])

    def test_the_hold_ends_when_the_transcript_is_written(self):
        now = time.time()
        hook = {"precompact_at": now - 60}
        row = crabd.StateBuilder._lane_m_session_extras(
            {"compactions": 1, "compaction_ts": now - 5, "mtime": now - 5},
            hook, "working")
        self.assertFalse(row["compaction"]["inProgress"])
        self.assertEqual(row["compaction"]["count"], 1)

    def test_absent_when_never_compacted_and_not_in_progress(self):
        row = crabd.StateBuilder._lane_m_session_extras(
            {"compactions": 0, "compaction_ts": 0.0, "mtime": time.time()},
            {"precompact_at": None}, "working")
        self.assertNotIn("compaction", row)

    def test_the_hook_records_the_time_without_moving_the_state(self):
        hooks = crabd.HookTracker()
        hooks.record({"session_id": "s-1", "hook_event_name": "UserPromptSubmit"})
        before = hooks.snapshot()["s-1"]
        now = time.time()
        self.assertTrue(hooks.note_precompact("s-1", now))
        after = hooks.snapshot()["s-1"]
        self.assertEqual(after["precompact_at"], now)
        # A compaction moves no state, dates no `since`, writes no event and does not
        # count as activity.
        for key in ("state", "since", "at", "events", "question", "turn_started"):
            with self.subTest(key=key):
                self.assertEqual(after[key], before[key])

    def test_a_payload_with_no_session_id_is_dropped(self):
        hooks = crabd.HookTracker()
        self.assertFalse(hooks.note_precompact("", time.time()))
        self.assertEqual(hooks.sessions, {})


class TodosTests(LaneMFacts):
    """`sessions[].todos` - the latest TodoWrite, reduced at parse time."""

    TODOS = [{"content": "read the audit", "status": "completed",
              "activeForm": "Reading the audit"},
             {"content": "fix the limits source", "status": "in_progress",
              "activeForm": "Fixing the limits source"},
             {"content": "write the notes", "status": "pending",
              "activeForm": "Writing the notes"}]

    def test_done_total_and_current(self):
        now = time.time()
        facts = self.facts([user_line("go", now - 100),
                            todo_write_line("r1", now - 50, self.TODOS)])
        self.assertEqual(facts.todos, {"done": 1, "total": 3,
                                       "current": "fix the limits source"})

    def test_the_newest_write_wins(self):
        now = time.time()
        later = [dict(t, status="completed") for t in self.TODOS]
        facts = self.facts([user_line("go", now - 100),
                            todo_write_line("r1", now - 50, self.TODOS),
                            todo_write_line("r2", now - 10, later)])
        self.assertEqual(facts.todos["done"], 3)
        self.assertIsNone(facts.todos["current"])

    def test_active_form_is_the_fallback_for_current(self):
        now = time.time()
        facts = self.facts([user_line("go", now - 100),
                            todo_write_line("r1", now - 50, [
                                {"status": "in_progress",
                                 "activeForm": "Running the suite"}])])
        self.assertEqual(facts.todos["current"], "Running the suite")

    def test_current_is_capped_at_eighty(self):
        now = time.time()
        facts = self.facts([user_line("go", now - 100),
                            todo_write_line("r1", now - 50, [
                                {"content": "c" * 300, "status": "in_progress"}])])
        self.assertLessEqual(len(facts.todos["current"]), crabd.TODO_CURRENT_MAX)

    def test_an_empty_list_clears_rather_than_serving_zero_of_zero(self):
        now = time.time()
        facts = self.facts([user_line("go", now - 100),
                            todo_write_line("r1", now - 50, self.TODOS),
                            todo_write_line("r2", now - 10, [])])
        self.assertIsNone(facts.todos)

    def test_absent_when_no_todo_write_exists(self):
        row = crabd.StateBuilder._lane_m_session_extras(
            {"todos": None, "mtime": 0.0}, None, "working")
        self.assertNotIn("todos", row)


class MovedSessionTests(TempProjects):
    """The SCA-001 P1 must not regress. A session id can own a main transcript under TWO
    project directories - its cwd moved - and the enumeration order over them is
    arbitrary. The four members that describe what the session is doing NOW come from the
    identity file; the two that are session history are aggregated across both.
    """

    SID = "moved-session-0001"

    def build(self, now):
        old = self.projects / "P--old" / f"{self.SID}.jsonl"
        new = self.projects / "P--new" / f"{self.SID}.jsonl"
        write_jsonl(old, [
            user_line("the old project", now - 4000, cwd="D:\\work\\old"),
            mode_line("plan", self.SID),
            compact_boundary_line(now - 3900, cwd="D:\\work\\old"),
            tool_use_line("old_1", now - 3800, "Edit",
                          {"file_path": "D:\\work\\old\\stale.py"}, cwd="D:\\work\\old"),
            queue_line("enqueue", now - 3700, self.SID),
        ], mtime=now - 3600)
        write_jsonl(new, [
            user_line("the new project", now - 120, cwd="D:\\work\\new"),
            mode_line("acceptEdits", self.SID),
            compact_boundary_line(now - 100, cwd="D:\\work\\new"),
            tool_use_line("new_1", now - 60, "Bash",
                          {"command": "x", "description": "run the new suite"},
                          cwd="D:\\work\\new"),
            tool_use_line("new_2", now - 30, "Write",
                          {"file_path": "D:\\work\\new\\fresh.py"}, cwd="D:\\work\\new"),
        ], mtime=now - 10)
        hooks = crabd.HookTracker()
        hooks.record({"session_id": self.SID, "hook_event_name": "UserPromptSubmit",
                      "cwd": "D:\\work\\new"})
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects), hooks,
                                     StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path),
                                     host=StubHost())
        return builder.build(now=now)

    def row(self, now):
        state = self.build(now)
        rows = [r for r in state["sessions"] if r["id"] == self.SID]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_the_new_project_wins_the_identity_members(self):
        now = time.time()
        row = self.row(now)
        self.assertEqual(row["cwd"], "D:\\work\\new")
        self.assertEqual(row["mode"], "acceptedits")
        self.assertEqual(row["activity"]["detail"], "fresh.py")

    def test_the_stale_project_does_not_contribute_the_queue(self):
        """The old file's enqueue is history, not a prompt waiting now."""
        now = time.time()
        self.assertNotIn("promptQueue", self.row(now))

    def test_the_session_history_members_aggregate_across_both(self):
        now = time.time()
        row = self.row(now)
        self.assertEqual(row["compaction"]["count"], 2)
        self.assertEqual(row["filesTouched"]["count"], 2)
        # Newest first, so the file the session is editing NOW leads.
        self.assertEqual(row["filesTouched"]["recent"][0], "fresh.py")


class ServedShapeTests(TempProjects):
    """The members as they reach /v1/state: schema 5, JSON-serialisable, and nothing
    present that has nothing to say."""

    SID = "served-shape-0001"

    def test_a_plain_session_carries_none_of_the_six(self):
        """THE HONESTY CASE. A session crabd has only usage records for must serve no
        activity, no mode, no filesTouched, no promptQueue, no compaction and no todos -
        absent, never a zero."""
        now = time.time()
        write_jsonl(self.session_path(self.SID, project="P--acme"),
                    [user_line("go", now - 60), assistant_line("r1", now - 30)],
                    mtime=now - 5)
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path),
                                     host=StubHost())
        state = builder.build(now=now)
        row = [r for r in state["sessions"] if r["id"] == self.SID][0]
        for key in ("activity", "mode", "filesTouched", "promptQueue", "compaction",
                    "todos"):
            with self.subTest(key=key):
                self.assertNotIn(key, row)
        self.assertEqual(state["schema"], 5)

    def test_a_busy_session_serves_them_and_the_document_serialises(self):
        now = time.time()
        write_jsonl(self.session_path(self.SID, project="P--acme"), [
            user_line("do the work", now - 120),
            mode_line("plan", self.SID),
            queue_line("enqueue", now - 110, self.SID),
            compact_boundary_line(now - 100),
            tool_use_line("r1", now - 60, "Edit",
                          {"file_path": "D:\\work\\acme\\main.py"}),
            todo_write_line("r2", now - 40, TodosTests.TODOS),
            tool_use_line("r3", now - 20, "Bash",
                          {"command": "secret", "description": "run the tests"}),
        ], mtime=now - 5)
        hooks = crabd.HookTracker()
        hooks.record({"session_id": self.SID, "hook_event_name": "UserPromptSubmit",
                      "cwd": "D:\\work\\acme"})
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects), hooks,
                                     StubLimits(), time.time(),
                                     crabd.UserConfig(self.config_path),
                                     host=StubHost())
        state = builder.build(now=now)
        row = [r for r in state["sessions"] if r["id"] == self.SID][0]
        self.assertEqual(row["state"], "working")
        self.assertEqual(row["mode"], "plan")
        self.assertEqual(row["activity"]["tool"], "Bash")
        self.assertEqual(row["activity"]["detail"], "run the tests")
        self.assertEqual(row["activity"]["callsThisTurn"], 3)
        self.assertEqual(row["filesTouched"], {"count": 1, "recent": ["main.py"]})
        self.assertEqual(row["promptQueue"], 1)
        self.assertEqual(row["compaction"]["count"], 1)
        self.assertEqual(row["todos"]["total"], 3)
        # dump_state is what /v1/state actually calls; a member it cannot serialise is a
        # 500 on the read path.
        round_trip = json.loads(crabd.dump_state(state).decode("utf-8"))
        self.assertEqual(round_trip["sessions"][0]["activity"]["detail"],
                         "run the tests")
        self.assertNotIn("secret", crabd.dump_state(state).decode("utf-8"))


class PathLeafTests(unittest.TestCase):
    """_path_leaf - the Windows path edge cases, since `file_path` is whatever the tool
    was handed."""

    def test_windows_posix_and_unc_all_read_the_same(self):
        for path, leaf in (("D:\\work\\acme\\main.py", "main.py"),
                           ("/home/user/acme/main.py", "main.py"),
                           ("\\\\fileserver\\share\\acme\\main.py", "main.py"),
                           ("main.py", "main.py"),
                           ("D:/work/acme/main.py", "main.py")):
            with self.subTest(path=path):
                self.assertEqual(crabd._path_leaf(path), leaf)

    def test_a_root_or_a_bare_share_has_no_leaf(self):
        for path in ("D:\\", "/", "\\\\fileserver\\share", "", "   "):
            with self.subTest(path=path):
                self.assertIsNone(crabd._path_leaf(path))

    def test_a_trailing_separator_gives_the_directory_name(self):
        """MEASURED, not assumed: PureWindowsPath strips a trailing separator, so
        'D:\\work\\' reads as 'work'. A tool's `file_path` is a file and never ends in a
        separator, so this is documented rather than special-cased."""
        self.assertEqual(crabd._path_leaf("D:\\work\\"), "work")

    def test_a_non_string_is_none(self):
        for value in (None, 7, ["a"], {"a": 1}):
            with self.subTest(value=value):
                self.assertIsNone(crabd._path_leaf(value))


if __name__ == "__main__":
    unittest.main()
