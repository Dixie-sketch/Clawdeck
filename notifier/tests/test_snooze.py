"""Headless unit tests for Snooze 30m (v0.16.0) — the button, the handler, the suppression.

Three things are pinned here, and they live in three different files:

  the BUTTON        sidecrab_toast.py       writes sidecrab-snooze:<id> into the toast payload
  the HANDLER       sidecrab_snooze_handler.pyw   validates it and writes the mark
  the SUPPRESSION   sidecrab_toast.py       reads the mark and holds the toast

The handler is a .pyw (it is launched by pythonw, which is what gives it no console), so it is
loaded by path rather than imported by name — same as test_ack_handler.py.

Stdlib unittest only, no Windows, no network: the handler here writes to a temp state file and
the log is redirected to a temp dir, so nothing touches the operator's own ~/.sidecrab.

    python -m unittest discover -s notifier/tests -v
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

NOTIFIER_DIR = Path(__file__).resolve().parents[1]
HANDLER_PATH = NOTIFIER_DIR / "sidecrab_snooze_handler.pyw"

sys.path.insert(0, str(NOTIFIER_DIR))

import sidecrab_toast  # noqa: E402
from sidecrab_toast import (  # noqa: E402
    ACK_SCHEME,
    SESSION_ID_PATTERN,
    SNOOZE_BUTTON_CONTENT,
    SNOOZE_MAP_CAP,
    SNOOZE_SCHEME,
    SNOOZE_SEC,
    SNOOZE_SECTION,
    BudgetLedger,
    ConfigReader,
    DigestLedger,
    Notifier,
    PowerShellToastAdapter,
    RecordingToastAdapter,
    SnoozeLedger,
    ToastConfig,
    ToastDecider,
    ToastRequest,
    build_request,
    parse_snooze_map,
    snooze_uri,
)


def _load_handler():
    """.pyw is not on every platform's SOURCE_SUFFIXES, so the loader is named explicitly."""
    loader = importlib.machinery.SourceFileLoader("sidecrab_snooze_handler", str(HANDLER_PATH))
    spec = importlib.util.spec_from_file_location("sidecrab_snooze_handler", HANDLER_PATH, loader=loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


handler = _load_handler()

VALID_ID = "0f9c1e2a-7b34-4d51-9c8e-2a1b3c4d5e6f"
T0 = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def waiting_feed(since: datetime, *, sid: str = VALID_ID, quiet: bool = False) -> dict:
    return {
        "schema": 5,
        "generatedAt": iso(since),
        "sessions": [
            {
                "id": sid,
                "title": "the lane",
                "state": "needs_input",
                "stateSince": iso(since),
                "question": "Ship it?",
            }
        ],
        "quiet": {"active": quiet, "start": "22:00", "end": "07:00"},
    }


# ------------------------------------------------------------- the three files agree


class ContractTests(unittest.TestCase):
    """These strings are written by one file, registered by a second and parsed by a third. A
    drift in any of them is silent: the button renders, the shell routes nothing, and no toast
    is ever held back."""

    def test_the_scheme_matches_the_handler(self) -> None:
        self.assertEqual(SNOOZE_SCHEME, handler.SNOOZE_SCHEME)

    def test_the_session_id_pattern_matches_the_handler(self) -> None:
        self.assertEqual(SESSION_ID_PATTERN, handler.SESSION_ID_PATTERN)

    def test_the_duration_matches_the_handler(self) -> None:
        self.assertEqual(SNOOZE_SEC, handler.SNOOZE_SEC)

    def test_the_button_label_says_what_the_handler_does(self) -> None:
        """A button that says 30m and a handler that holds for an hour is worse than no
        button — and the label is the one part of the contract the operator can read."""
        self.assertIn(f"{SNOOZE_SEC // 60}m", SNOOZE_BUTTON_CONTENT)

    def test_the_ledger_section_matches_the_handler(self) -> None:
        self.assertEqual(SNOOZE_SECTION, handler.SNOOZE_SECTION)

    def test_the_cap_matches_the_handler(self) -> None:
        self.assertEqual(SNOOZE_MAP_CAP, handler.SNOOZE_MAP_CAP)

    def test_snooze_is_a_separate_scheme_from_ack(self) -> None:
        """Two schemes, two handlers, one regex each — rather than an action word inside one
        URI, which would turn a charset test into a parser."""
        self.assertNotEqual(SNOOZE_SCHEME, ACK_SCHEME)

    def test_the_handler_never_posts_to_crabd(self) -> None:
        """The structural half of 'snoozing must never look like answering'. A comment is not a
        guarantee; the absence of any way to reach the network is.

        Read off the AST rather than the text, so the docstring may go on EXPLAINING that this
        handler does not POST to /v1/action without the explanation tripping the test.
        """
        tree = ast.parse(HANDLER_PATH.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & {"urllib", "http", "socket", "requests"}, set())

        # Docstring nodes are excluded by IDENTITY, not by value: ast.get_docstring returns a
        # cleaned copy that never equals the raw Constant it came from.
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        reachable = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        self.assertFalse([s for s in reachable if "/v1/" in s or "http" in s])


# ------------------------------------------------------------------------- the URI


class UriTests(unittest.TestCase):
    def test_a_crabd_session_id_is_embedded(self) -> None:
        self.assertEqual(snooze_uri(VALID_ID), f"sidecrab-snooze:{VALID_ID}")

    def test_exactly_64_characters_is_allowed(self) -> None:
        self.assertIsNotNone(snooze_uri("a" * 64))

    def test_65_characters_is_refused(self) -> None:
        self.assertIsNone(snooze_uri("a" * 65))

    def test_an_empty_id_is_refused(self) -> None:
        self.assertIsNone(snooze_uri(""))

    def test_a_quote_that_would_escape_the_attribute_is_refused(self) -> None:
        self.assertIsNone(snooze_uri("a'/><script>"))

    def test_a_nested_scheme_is_refused(self) -> None:
        self.assertIsNone(snooze_uri("file:///c:/windows"))

    def test_a_non_string_is_refused(self) -> None:
        self.assertIsNone(snooze_uri(None))
        self.assertIsNone(snooze_uri(12))


# --------------------------------------------------------------------- the toast XML


class XmlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = PowerShellToastAdapter(aumid="test")

    def waiting(self, sid: str = VALID_ID) -> ToastRequest:
        return build_request({"id": sid, "title": "the lane", "question": "Ship it?"}, iso(T0))

    def test_a_waiting_toast_carries_the_snooze_button(self) -> None:
        xml = self.adapter.build_xml(self.waiting())
        self.assertIn(f"content='{SNOOZE_BUTTON_CONTENT}'", xml)
        self.assertIn(f"arguments='sidecrab-snooze:{VALID_ID}'", xml)

    def test_it_activates_the_protocol_not_this_process(self) -> None:
        """A toast outlives the notifier in Action Center; only the shell can still route."""
        xml = self.adapter.build_xml(self.waiting())
        self.assertEqual(xml.count("activationType='protocol'"), 2)

    def test_acknowledge_stays_first(self) -> None:
        """The leftmost button is the one hit from a lock screen by someone barely reading, and
        that one should be the answer, not the deferral."""
        xml = self.adapter.build_xml(self.waiting())
        self.assertLess(xml.index("Acknowledge"), xml.index(SNOOZE_BUTTON_CONTENT))

    def test_a_bad_id_drops_both_buttons_and_keeps_the_toast(self) -> None:
        """A question waiting on the operator still has to reach him."""
        xml = self.adapter.build_xml(self.waiting("bad id!"))
        self.assertNotIn("<actions>", xml)
        self.assertIn("Ship it?", xml)

    def test_a_non_actionable_toast_gets_no_snooze_button(self) -> None:
        """Digest, budget, approval and outage toasts have no session behind them."""
        request = ToastRequest(session_id="digest-2026-08-26", state_since="x", title="t", body="b", actionable=False)
        self.assertNotIn(SNOOZE_SCHEME, self.adapter.build_xml(request))


# ------------------------------------------------------------------- the handler parse


class ParseTests(unittest.TestCase):
    """The whole security boundary of this component is one regex. Exercise it as such."""

    def test_it_accepts_a_crabd_session_id(self) -> None:
        self.assertEqual(handler.parse_snooze_uri(f"sidecrab-snooze:{VALID_ID}"), VALID_ID)

    def test_it_tolerates_one_trailing_slash(self) -> None:
        self.assertEqual(handler.parse_snooze_uri(f"sidecrab-snooze:{VALID_ID}/"), VALID_ID)

    def test_it_tolerates_surrounding_whitespace(self) -> None:
        self.assertEqual(handler.parse_snooze_uri(f"  sidecrab-snooze:{VALID_ID}  "), VALID_ID)

    def test_the_scheme_case_is_absorbed(self) -> None:
        """F4: Windows resolves the handler key case-insensitively, so the shell can hand back
        a case we never wrote. The old exact match dropped the snooze with a length-only log
        line — silent — and the operator's toast then re-fired minutes later as if unclicked."""
        for uri in (f"SIDECRAB-SNOOZE:{VALID_ID}", f"SideCrab-Snooze:{VALID_ID}", f"sidecrab-SNOOZE:{VALID_ID}"):
            with self.subTest(uri=uri):
                self.assertEqual(handler.parse_snooze_uri(uri), VALID_ID)

    def test_the_case_fold_is_ascii_only(self) -> None:
        """re.ASCII on the scheme regex. Full-Unicode IGNORECASE folds homoglyphs too, and a
        homoglyph is not a case difference: U+017F LATIN SMALL LETTER LONG S folds onto "s",
        so without the flag a lookalike scheme would be accepted as ours. Both s-positions.
        (The ack handler's scheme has the same problem with U+212A KELVIN SIGN and "k".)"""
        for uri in (f"ſidecrab-snooze:{VALID_ID}", f"sidecrab-ſnooze:{VALID_ID}"):
            with self.subTest(uri=uri):
                self.assertIsNone(handler.parse_snooze_uri(uri))

    def test_the_payload_stays_strict_whatever_the_scheme_case(self) -> None:
        """The fold buys the scheme token latitude and nothing after the colon."""
        for tail in ("../../windows", "a b", "a;calc.exe", "a'b", 'a"b', "a\x00b", "%2e%2e%2f"):
            with self.subTest(tail=tail):
                self.assertIsNone(handler.parse_snooze_uri(f"SIDECRAB-SNOOZE:{tail}"))

    def test_the_ack_scheme_is_refused(self) -> None:
        """Each handler answers for its own scheme only."""
        self.assertIsNone(handler.parse_snooze_uri(f"sidecrab-ack:{VALID_ID}"))

    def test_a_path_traversal_is_refused(self) -> None:
        self.assertIsNone(handler.parse_snooze_uri("sidecrab-snooze:../../windows/system32"))

    def test_a_percent_escape_is_refused(self) -> None:
        self.assertIsNone(handler.parse_snooze_uri("sidecrab-snooze:%2e%2e%2f"))

    def test_a_quote_is_refused(self) -> None:
        self.assertIsNone(handler.parse_snooze_uri("sidecrab-snooze:a\"b"))

    def test_a_json_injection_key_is_refused(self) -> None:
        """The id becomes a JSON KEY in a document the notifier re-reads every poll."""
        self.assertIsNone(handler.parse_snooze_uri('sidecrab-snooze:a": "x", "b'))

    def test_an_empty_id_is_refused(self) -> None:
        self.assertIsNone(handler.parse_snooze_uri("sidecrab-snooze:"))

    def test_65_characters_is_refused(self) -> None:
        self.assertIsNone(handler.parse_snooze_uri("sidecrab-snooze:" + "a" * 65))

    def test_two_trailing_slashes_are_refused(self) -> None:
        self.assertIsNone(handler.parse_snooze_uri(f"sidecrab-snooze:{VALID_ID}//"))

    def test_a_non_string_is_refused(self) -> None:
        self.assertIsNone(handler.parse_snooze_uri(None))


# ------------------------------------------------------------------- the handler write


class HandlerWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "toast-state.json"

    def snooze(self, sid: str = VALID_ID, now: datetime = T0) -> dict:
        handler.write_snooze(sid, now + timedelta(seconds=SNOOZE_SEC), now, self.state)
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_it_writes_a_mark_thirty_minutes_out(self) -> None:
        doc = self.snooze()
        until = handler._parse_iso(doc[SNOOZE_SECTION][VALID_ID])
        self.assertEqual((until - T0).total_seconds(), 1800)

    def test_it_creates_the_file_when_there_is_none(self) -> None:
        self.assertFalse(self.state.exists())
        self.snooze()
        self.assertTrue(self.state.exists())

    def test_it_preserves_the_notifier_own_sections(self) -> None:
        """The digest day, the budget day and the runtime stamp live in this same file. A
        writer that serialised only its own section would erase them, and the symptom would be
        a duplicate digest toast after a restart that nobody traces back to a snooze."""
        self.state.write_text(
            json.dumps({"digest": {"lastDay": "2026-08-26"}, "budget": {"lastDay": "2026-08-26"}}),
            encoding="utf-8",
        )
        doc = self.snooze()
        self.assertEqual(doc["digest"], {"lastDay": "2026-08-26"})
        self.assertEqual(doc["budget"], {"lastDay": "2026-08-26"})

    def test_a_corrupt_document_does_not_lose_the_snooze(self) -> None:
        self.state.write_text("{not json", encoding="utf-8")
        self.assertIn(VALID_ID, self.snooze()[SNOOZE_SECTION])

    def test_a_second_snooze_replaces_the_first(self) -> None:
        self.snooze()
        doc = self.snooze(now=T0 + timedelta(minutes=10))
        self.assertEqual(len(doc[SNOOZE_SECTION]), 1)
        until = handler._parse_iso(doc[SNOOZE_SECTION][VALID_ID])
        self.assertEqual((until - T0).total_seconds(), 600 + 1800)

    def test_expired_marks_are_pruned_on_write(self) -> None:
        self.snooze(sid="old")
        doc = self.snooze(sid="new", now=T0 + timedelta(hours=2))
        self.assertNotIn("old", doc[SNOOZE_SECTION])
        self.assertIn("new", doc[SNOOZE_SECTION])

    def test_a_live_mark_for_another_session_survives(self) -> None:
        self.snooze(sid="other")
        self.assertIn("other", self.snooze()[SNOOZE_SECTION])

    def test_a_junk_key_already_in_the_file_is_dropped(self) -> None:
        self.state.write_text(json.dumps({SNOOZE_SECTION: {"../evil": iso(T0 + timedelta(hours=1))}}), encoding="utf-8")
        self.assertNotIn("../evil", self.snooze()[SNOOZE_SECTION])

    def test_an_unparseable_instant_already_in_the_file_is_dropped(self) -> None:
        self.state.write_text(json.dumps({SNOOZE_SECTION: {"other": "forever"}}), encoding="utf-8")
        self.assertNotIn("other", self.snooze()[SNOOZE_SECTION])

    def test_the_map_is_capped_and_the_newest_press_always_survives(self) -> None:
        block = {f"s{i}": iso(T0 + timedelta(hours=1, minutes=i)) for i in range(SNOOZE_MAP_CAP + 20)}
        self.state.write_text(json.dumps({SNOOZE_SECTION: block}), encoding="utf-8")
        marks = self.snooze()[SNOOZE_SECTION]
        self.assertLessEqual(len(marks), SNOOZE_MAP_CAP)
        self.assertIn(VALID_ID, marks)

    def test_main_exits_ok_and_logs_one_line(self) -> None:
        log = Path(self.tmp.name) / "snooze.log"
        real_log, real_state = handler.LOG_PATH, handler.STATE_PATH
        handler.LOG_PATH, handler.STATE_PATH = log, self.state
        try:
            self.assertEqual(handler.main([f"sidecrab-snooze:{VALID_ID}"]), handler.EXIT_OK)
        finally:
            handler.LOG_PATH, handler.STATE_PATH = real_log, real_state
        self.assertIn(VALID_ID, log.read_text(encoding="utf-8"))
        self.assertIn(VALID_ID, json.loads(self.state.read_text(encoding="utf-8"))[SNOOZE_SECTION])

    def test_main_refuses_a_bad_uri_without_echoing_it(self) -> None:
        log = Path(self.tmp.name) / "snooze.log"
        real_log, real_state = handler.LOG_PATH, handler.STATE_PATH
        handler.LOG_PATH, handler.STATE_PATH = log, self.state
        try:
            self.assertEqual(handler.main(["sidecrab-snooze:../../etc/passwd"]), handler.EXIT_BAD_URI)
        finally:
            handler.LOG_PATH, handler.STATE_PATH = real_log, real_state
        text = log.read_text(encoding="utf-8")
        self.assertIn("refused", text)
        self.assertNotIn("passwd", text)
        self.assertFalse(self.state.exists())

    def test_main_refuses_no_argument(self) -> None:
        log = Path(self.tmp.name) / "snooze.log"
        real_log = handler.LOG_PATH
        handler.LOG_PATH = log
        try:
            self.assertEqual(handler.main([]), handler.EXIT_BAD_URI)
        finally:
            handler.LOG_PATH = real_log


# -------------------------------------------------------------------- reading the ledger


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "toast-state.json"

    def write(self, block: dict) -> None:
        self.state.write_text(json.dumps({SNOOZE_SECTION: block}), encoding="utf-8")

    def test_a_missing_file_reads_as_no_snoozes(self) -> None:
        self.assertEqual(SnoozeLedger(self.state).read(), {})

    def test_a_corrupt_file_reads_as_no_snoozes(self) -> None:
        self.state.write_text("{not json", encoding="utf-8")
        self.assertEqual(SnoozeLedger(self.state).read(), {})

    def test_a_mark_is_read(self) -> None:
        self.write({VALID_ID: iso(T0)})
        self.assertEqual(SnoozeLedger(self.state).read()[VALID_ID], T0)

    def test_a_junk_key_is_dropped(self) -> None:
        """A file on disk is written by whatever can write files. What comes out is validated
        against the same charset the button was allowed to embed."""
        self.write({"../evil": iso(T0), VALID_ID: iso(T0)})
        self.assertEqual(list(SnoozeLedger(self.state).read()), [VALID_ID])

    def test_an_unparseable_instant_is_dropped_not_guessed(self) -> None:
        """Neither 'snoozed forever' nor 'snoozed until now' is a decision the operator made,
        and the first of those silences a waiting question permanently."""
        self.write({VALID_ID: "whenever"})
        self.assertEqual(SnoozeLedger(self.state).read(), {})

    def test_a_non_dict_section_is_survived(self) -> None:
        self.state.write_text(json.dumps({SNOOZE_SECTION: "nope"}), encoding="utf-8")
        self.assertEqual(SnoozeLedger(self.state).read(), {})

    def test_it_re_reads_when_the_file_changes(self) -> None:
        """The mark is written by a SEPARATE PROCESS. A value cached for the notifier's
        lifetime would make the button do nothing until the next restart."""
        ledger = SnoozeLedger(self.state)
        self.assertEqual(ledger.read(), {})
        handler.write_snooze(VALID_ID, T0 + timedelta(seconds=SNOOZE_SEC), T0, self.state)
        self.assertIn(VALID_ID, ledger.read())

    def test_parse_ignores_a_document_that_is_not_a_dict(self) -> None:
        self.assertEqual(parse_snooze_map(["nope"]), {})

    def test_the_ledger_never_writes(self) -> None:
        SnoozeLedger(self.state).read()
        self.assertFalse(self.state.exists())


# ---------------------------------------------------------------------- the suppression


class SuppressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = ToastDecider()
        self.config = ToastConfig(threshold_sec=120)
        self.matured = T0 + timedelta(seconds=300)

    def evaluate(self, at: datetime, snoozes: dict | None = None) -> list:
        return self.decider.evaluate(waiting_feed(T0), at, self.config, snoozes)

    def test_a_matured_question_toasts_without_a_snooze(self) -> None:
        self.assertEqual(len(self.evaluate(self.matured)), 1)

    def test_a_live_snooze_holds_the_toast(self) -> None:
        until = self.matured + timedelta(minutes=30)
        self.assertEqual(self.evaluate(self.matured, {VALID_ID: until}), [])

    def test_the_toast_arrives_once_the_snooze_expires(self) -> None:
        """SNOOZE DEFERS. Every other suppression here marks the spell consumed; a reminder
        that never comes back is not a reminder."""
        until = self.matured + timedelta(minutes=30)
        self.assertEqual(self.evaluate(self.matured, {VALID_ID: until}), [])
        owed = self.evaluate(until + timedelta(seconds=10), {VALID_ID: until})
        self.assertEqual(len(owed), 1)
        self.assertIn("Ship it?", owed[0].body)

    def test_the_deferred_toast_is_the_same_spell_not_a_new_one(self) -> None:
        until = self.matured + timedelta(minutes=30)
        self.evaluate(self.matured, {VALID_ID: until})
        owed = self.evaluate(until + timedelta(seconds=10), {VALID_ID: until})
        self.assertEqual(owed[0].state_since, iso(T0))

    def test_it_fires_only_once_after_the_snooze_expires(self) -> None:
        until = self.matured + timedelta(minutes=30)
        self.evaluate(self.matured, {VALID_ID: until})
        after = until + timedelta(seconds=10)
        self.assertEqual(len(self.evaluate(after, {VALID_ID: until})), 1)
        self.assertEqual(self.evaluate(after + timedelta(seconds=10), {VALID_ID: until}), [])

    def test_a_snooze_at_its_exact_expiry_no_longer_holds(self) -> None:
        until = self.matured
        self.assertEqual(len(self.evaluate(self.matured, {VALID_ID: until})), 1)

    def test_a_snooze_for_another_session_does_not_hold_this_one(self) -> None:
        until = self.matured + timedelta(minutes=30)
        self.assertEqual(len(self.evaluate(self.matured, {"someone-else": until})), 1)

    def test_snoozing_does_not_ack(self) -> None:
        """The structural half of the promise. The decider produces no acked flag, touches no
        endpoint, and the session is still needs_input on the next poll — the panel keeps
        showing it waiting, because it IS waiting."""
        state = waiting_feed(T0)
        until = self.matured + timedelta(minutes=30)
        self.decider.evaluate(state, self.matured, self.config, {VALID_ID: until})
        self.assertEqual(state["sessions"][0]["state"], "needs_input")
        self.assertNotIn("acked", state["sessions"][0])

    def test_an_ack_still_resolves_a_snoozed_spell_permanently(self) -> None:
        """Acking from the widget while a snooze is live must win: the question was answered,
        and the snooze was only ever about when to ask again."""
        state = waiting_feed(T0)
        state["sessions"][0]["acked"] = True
        until = self.matured + timedelta(minutes=30)
        self.assertEqual(self.decider.evaluate(state, self.matured, self.config, {VALID_ID: until}), [])
        self.assertEqual(self.decider.evaluate(waiting_feed(T0), until + timedelta(minutes=1), self.config), [])

    def test_a_new_question_during_a_snooze_is_still_held(self) -> None:
        """The mark is per SESSION, not per spell — 'stop telling me about this session'."""
        later = T0 + timedelta(minutes=5)
        until = later + timedelta(minutes=30)
        self.assertEqual(
            self.decider.evaluate(waiting_feed(later), later + timedelta(seconds=300), self.config, {VALID_ID: until}),
            [],
        )

    def test_the_default_argument_keeps_every_existing_call_site_working(self) -> None:
        self.assertEqual(len(self.decider.evaluate(waiting_feed(T0), self.matured, self.config)), 1)

    def test_quiet_hours_still_mark_rather_than_defer(self) -> None:
        """Snooze is the ONE deferral. Quiet must not have quietly become one too."""
        state = waiting_feed(T0, quiet=True)
        self.assertEqual(self.decider.evaluate(state, self.matured, self.config, {}), [])
        self.assertEqual(self.decider.evaluate(waiting_feed(T0), self.matured, self.config, {}), [])


# ------------------------------------------------------------------------ through Notifier


class NotifierTests(unittest.TestCase):
    """Button pressed -> handler writes -> notifier holds. The whole path, no Windows."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "toast-state.json"
        self._fetch = sidecrab_toast.fetch_state
        self.addCleanup(lambda: setattr(sidecrab_toast, "fetch_state", self._fetch))
        (self.root / "config.json").write_text(json.dumps({"toast": {"thresholdSec": 120}}), encoding="utf-8")

        self.adapter = RecordingToastAdapter()
        self.notifier = Notifier(
            adapter=self.adapter,
            config_reader=ConfigReader(self.root / "config.json"),
            digest_ledger=DigestLedger(self.state),
            budget_ledger=BudgetLedger(self.state),
            snooze_ledger=SnoozeLedger(self.state),
        )

    def test_pressing_snooze_holds_the_next_toast_and_expiry_releases_it(self) -> None:
        sidecrab_toast.fetch_state = lambda *a, **k: waiting_feed(T0)
        matured = T0 + timedelta(seconds=300)

        # The operator presses the button: the real handler, writing the real ledger format.
        handler.write_snooze(VALID_ID, matured + timedelta(seconds=SNOOZE_SEC), matured, self.state)
        self.assertEqual(self.notifier.poll_once(now=matured), [])

        released = matured + timedelta(seconds=SNOOZE_SEC + 10)
        fired = self.notifier.poll_once(now=released)
        self.assertEqual(len(fired), 1)
        self.assertIn("the lane", fired[0].title)

    def test_a_failed_waiting_toast_retries_then_resolves_once_shown(self) -> None:
        """F1, the primary case: a waiting question whose toast FAILS to render — a control
        byte in the title that trips LoadXml, a dead PowerShell — must NOT be consumed. The
        mark is deferred past the render: a failed show re-arms the spell (unresolve) so the
        next poll retries, and only a successful show resolves it. Without this, one poisoned
        title silently buries a real question forever."""
        # generatedAt tracks each poll so the stale-feed decider stays quiet — this test is
        # about the WAITING toast's retry, in isolation.
        matured = T0 + timedelta(seconds=300)
        sidecrab_toast.fetch_state = lambda *a, **k: {
            **waiting_feed(T0), "generatedAt": iso(matured)
        }

        def waiting_shown(adapter):
            return [r for r in adapter.shown if r.session_id == VALID_ID]

        self.notifier.adapter = RecordingToastAdapter(succeed=False)
        self.assertEqual(self.notifier.poll_once(now=matured), [])
        self.assertEqual(self.notifier.poll_once(now=matured + timedelta(seconds=10)), [])
        self.assertEqual(len(waiting_shown(self.notifier.adapter)), 2, "a failed render is retried, not consumed")

        good = RecordingToastAdapter()
        self.notifier.adapter = good
        self.assertEqual(len(self.notifier.poll_once(now=matured + timedelta(seconds=20))), 1)
        self.notifier.poll_once(now=matured + timedelta(seconds=30))
        self.assertEqual(len(waiting_shown(good)), 1, "resolved once shown — no re-fire")

    def test_a_snooze_survives_a_notifier_restart(self) -> None:
        """The reason the mark is on disk at all: the ledger outlives the process, so a task
        restart cannot turn a 30-minute snooze into an immediate toast."""
        sidecrab_toast.fetch_state = lambda *a, **k: waiting_feed(T0)
        matured = T0 + timedelta(seconds=300)
        handler.write_snooze(VALID_ID, matured + timedelta(seconds=SNOOZE_SEC), matured, self.state)

        fresh = Notifier(
            adapter=RecordingToastAdapter(),
            config_reader=ConfigReader(self.root / "config.json"),
            digest_ledger=DigestLedger(self.state),
            budget_ledger=BudgetLedger(self.state),
            snooze_ledger=SnoozeLedger(self.state),
        )
        self.assertEqual(fresh.poll_once(now=matured), [])


if __name__ == "__main__":
    unittest.main()
