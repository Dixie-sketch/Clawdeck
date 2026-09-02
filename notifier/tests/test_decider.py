"""Headless unit tests for the SideCrab notifier decision layer.

stdlib unittest only — same zero-dependency rule as the notifier itself, so these run on a
bare Scheduled-Task interpreter. No test here touches Windows: emission is behind an adapter.

    python -m unittest discover -s notifier/tests -v
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sidecrab_toast  # noqa: E402
from sidecrab_toast import (  # noqa: E402
    ACK_BUTTON_CONTENT,
    ACK_SCHEME,
    AUMID_REGISTRY_SUBKEY,
    AUMID_REPROBE_SEC,
    ConfigReader,
    LEDGER_PRUNE_GRACE,
    PowerShellToastAdapter,
    RecordingToastAdapter,
    SESSION_ID_PATTERN,
    SIDECRAB_AUMID,
    SUPPORTED_SCHEMAS,
    ToastConfig,
    ToastDecider,
    ToastRequest,
    ack_uri,
    build_request,
    parse_iso,
    parse_toast_config,
    probe_registered_aumid,
    registered_aumid,
    trim,
)

T0 = datetime(2026, 8, 26, 20, 0, 0, tzinfo=timezone.utc)


def iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def session(
    sid: str = "s1",
    state: str = "needs_input",
    since: datetime | str = T0,
    *,
    title: str = "Corsair widget feasibility",
    question: str | None = "Should I overwrite the existing config?",
    acked: bool = False,
    **extra,
) -> dict:
    doc = {
        "id": sid,
        "title": title,
        "state": state,
        "stateSince": since if isinstance(since, str) else iso(since),
        "question": question,
        "acked": acked,
        "lastEvent": "asked a question",
    }
    doc.update(extra)
    return doc


def feed(*sessions: dict, schema: int = 2, quiet: bool | None = False) -> dict:
    doc: dict = {"schema": schema, "sessions": list(sessions)}
    if quiet is not None:
        doc["quiet"] = {"active": quiet, "start": "22:00", "end": "07:00"}
    return doc


class ThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = ToastDecider()
        self.config = ToastConfig(enabled=True, threshold_sec=120)

    def test_below_threshold_is_silent(self) -> None:
        state = feed(session(since=T0))
        owed = self.decider.evaluate(state, T0 + timedelta(seconds=119), self.config)
        self.assertEqual(owed, [])

    def test_at_threshold_toasts(self) -> None:
        state = feed(session(since=T0))
        owed = self.decider.evaluate(state, T0 + timedelta(seconds=120), self.config)
        self.assertEqual(len(owed), 1)
        self.assertEqual(owed[0].session_id, "s1")

    def test_not_yet_due_stays_armed(self) -> None:
        """An early poll must not consume the spell — it has to fire once it matures."""
        state = feed(session(since=T0))
        self.assertEqual(self.decider.evaluate(state, T0 + timedelta(seconds=30), self.config), [])
        self.assertEqual(self.decider.evaluate(state, T0 + timedelta(seconds=90), self.config), [])
        self.assertEqual(len(self.decider.evaluate(state, T0 + timedelta(seconds=200), self.config)), 1)

    def test_threshold_is_configurable(self) -> None:
        state = feed(session(since=T0))
        loose = ToastConfig(enabled=True, threshold_sec=600)
        self.assertEqual(self.decider.evaluate(state, T0 + timedelta(seconds=300), loose), [])
        self.assertEqual(len(self.decider.evaluate(state, T0 + timedelta(seconds=700), loose)), 1)

    def test_zero_threshold_fires_immediately(self) -> None:
        state = feed(session(since=T0))
        eager = ToastConfig(enabled=True, threshold_sec=0)
        self.assertEqual(len(self.decider.evaluate(state, T0, eager)), 1)


class DedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = ToastDecider()
        self.config = ToastConfig(enabled=True, threshold_sec=120)

    def test_one_toast_per_state_since(self) -> None:
        state = feed(session(since=T0))
        first = self.decider.evaluate(state, T0 + timedelta(seconds=200), self.config)
        self.assertEqual(len(first), 1)
        for minute in range(2, 40):
            again = self.decider.evaluate(state, T0 + timedelta(minutes=minute), self.config)
            self.assertEqual(again, [], f"re-toasted the same spell at minute {minute}")

    def test_new_question_rearms(self) -> None:
        state = feed(session(since=T0))
        self.assertEqual(len(self.decider.evaluate(state, T0 + timedelta(seconds=200), self.config)), 1)

        later = T0 + timedelta(minutes=10)
        state2 = feed(session(since=later))
        self.assertEqual(self.decider.evaluate(state2, later + timedelta(seconds=60), self.config), [])
        self.assertEqual(len(self.decider.evaluate(state2, later + timedelta(seconds=200), self.config)), 1)

    def test_state_change_and_back_does_not_retoast_same_spell(self) -> None:
        """working -> needs_input with the SAME stateSince is not a new spell."""
        state = feed(session(since=T0))
        self.assertEqual(len(self.decider.evaluate(state, T0 + timedelta(seconds=200), self.config)), 1)
        self.decider.evaluate(feed(session(state="working", since=T0)), T0 + timedelta(seconds=210), self.config)
        self.assertEqual(self.decider.evaluate(state, T0 + timedelta(seconds=220), self.config), [])

    def test_sessions_are_independent(self) -> None:
        state = feed(session("a", since=T0), session("b", since=T0))
        owed = self.decider.evaluate(state, T0 + timedelta(seconds=200), self.config)
        self.assertEqual({r.session_id for r in owed}, {"a", "b"})
        self.assertEqual(self.decider.evaluate(state, T0 + timedelta(seconds=260), self.config), [])

    def test_ledger_survives_transient_empty_feed(self) -> None:
        """crabd restarting and briefly serving no sessions must not re-arm a live spell."""
        state = feed(session(since=T0))
        now = T0 + timedelta(seconds=200)
        self.assertEqual(len(self.decider.evaluate(state, now, self.config)), 1)
        self.decider.evaluate(feed(), now + timedelta(seconds=10), self.config)
        self.assertEqual(self.decider.evaluate(state, now + timedelta(seconds=20), self.config), [])

    def test_ledger_is_dropped_after_sustained_absence(self) -> None:
        state = feed(session(since=T0))
        now = T0 + timedelta(seconds=200)
        self.assertEqual(len(self.decider.evaluate(state, now, self.config)), 1)
        for i in range(LEDGER_PRUNE_GRACE):
            self.decider.evaluate(feed(), now + timedelta(seconds=10 * (i + 1)), self.config)
        # Session id reused by a brand new crabd lifetime -> allowed to toast again.
        self.assertEqual(len(self.decider.evaluate(state, now + timedelta(minutes=5), self.config)), 1)


class AckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = ToastDecider()
        self.config = ToastConfig(enabled=True, threshold_sec=120)

    def test_acked_never_toasts(self) -> None:
        state = feed(session(since=T0, acked=True))
        self.assertEqual(self.decider.evaluate(state, T0 + timedelta(seconds=600), self.config), [])

    def test_ack_cancels_a_pending_spell_permanently(self) -> None:
        """Acked below threshold, then un-acked at the same stateSince: still silent."""
        self.decider.evaluate(feed(session(since=T0, acked=True)), T0 + timedelta(seconds=30), self.config)
        owed = self.decider.evaluate(feed(session(since=T0, acked=False)), T0 + timedelta(seconds=300), self.config)
        self.assertEqual(owed, [])

    def test_ack_then_new_question_still_toasts(self) -> None:
        self.decider.evaluate(feed(session(since=T0, acked=True)), T0 + timedelta(seconds=30), self.config)
        later = T0 + timedelta(minutes=5)
        owed = self.decider.evaluate(feed(session(since=later)), later + timedelta(seconds=200), self.config)
        self.assertEqual(len(owed), 1)


class QuietTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = ToastDecider()
        self.config = ToastConfig(enabled=True, threshold_sec=120)

    def test_quiet_suppresses_everything(self) -> None:
        state = feed(session("a", since=T0), session("b", since=T0), quiet=True)
        self.assertEqual(self.decider.evaluate(state, T0 + timedelta(seconds=600), self.config), [])

    def test_quiet_end_does_not_burst(self) -> None:
        """The healthy-night test: spells that matured during quiet hours stay silent after."""
        state = feed(session("a", since=T0), session("b", since=T0), quiet=True)
        self.decider.evaluate(state, T0 + timedelta(seconds=200), self.config)
        awake = feed(session("a", since=T0), session("b", since=T0), quiet=False)
        self.assertEqual(self.decider.evaluate(awake, T0 + timedelta(seconds=260), self.config), [])

    def test_question_asked_after_quiet_ends_still_toasts(self) -> None:
        self.decider.evaluate(feed(session("a", since=T0), quiet=True), T0 + timedelta(seconds=200), self.config)
        morning = T0 + timedelta(hours=8)
        owed = self.decider.evaluate(
            feed(session("a", since=morning), quiet=False), morning + timedelta(seconds=200), self.config
        )
        self.assertEqual(len(owed), 1)

    def test_absent_quiet_key_is_not_quiet(self) -> None:
        state = feed(session(since=T0), schema=1, quiet=None)
        self.assertEqual(len(self.decider.evaluate(state, T0 + timedelta(seconds=200), self.config)), 1)

    def test_null_quiet_is_not_quiet(self) -> None:
        state = {"schema": 2, "quiet": None, "sessions": [session(since=T0)]}
        self.assertEqual(len(self.decider.evaluate(state, T0 + timedelta(seconds=200), self.config)), 1)


class DisabledTests(unittest.TestCase):
    def test_disabled_emits_nothing(self) -> None:
        decider = ToastDecider()
        off = ToastConfig(enabled=False, threshold_sec=120)
        state = feed(session(since=T0))
        self.assertEqual(decider.evaluate(state, T0 + timedelta(seconds=600), off), [])

    def test_disabled_does_not_consume_the_spell(self) -> None:
        """Turning toasts back on surfaces what is still genuinely waiting."""
        decider = ToastDecider()
        state = feed(session(since=T0))
        decider.evaluate(state, T0 + timedelta(seconds=600), ToastConfig(enabled=False))
        owed = decider.evaluate(state, T0 + timedelta(seconds=700), ToastConfig(enabled=True, threshold_sec=120))
        self.assertEqual(len(owed), 1)


class StateFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.decider = ToastDecider()
        self.config = ToastConfig(enabled=True, threshold_sec=120)
        self.now = T0 + timedelta(seconds=600)

    def test_other_states_are_ignored(self) -> None:
        for state_name in ("working", "done", "idle", "gone"):
            with self.subTest(state=state_name):
                d = ToastDecider()
                self.assertEqual(d.evaluate(feed(session(state=state_name, since=T0)), self.now, self.config), [])

    def test_malformed_sessions_do_not_crash(self) -> None:
        state = {
            "schema": 2,
            "sessions": [
                None,
                "nonsense",
                {},
                {"id": None, "state": "needs_input", "stateSince": iso(T0)},
                {"id": "ok", "state": "needs_input", "stateSince": "not-a-date"},
                {"id": "ok2", "state": "needs_input"},
                session("good", since=T0),
            ],
        }
        owed = self.decider.evaluate(state, self.now, self.config)
        self.assertEqual([r.session_id for r in owed], ["good"])

    def test_non_dict_state_is_safe(self) -> None:
        self.assertEqual(self.decider.evaluate(None, self.now, self.config), [])
        self.assertEqual(self.decider.evaluate([], self.now, self.config), [])

    def test_sessions_not_a_list_is_safe(self) -> None:
        self.assertEqual(self.decider.evaluate({"schema": 2, "sessions": {}}, self.now, self.config), [])


class TextTests(unittest.TestCase):
    def test_title_format_and_trim(self) -> None:
        req = build_request(session(title="x" * 200), iso(T0))
        self.assertTrue(req.title.startswith("Claude is waiting \u2014 "))
        label = req.title.split("\u2014 ", 1)[1]
        self.assertEqual(len(label), 48)
        self.assertTrue(label.endswith("\u2026"))

    def test_body_uses_question_and_trims(self) -> None:
        req = build_request(session(question="q" * 500), iso(T0))
        self.assertEqual(len(req.body), 140)
        self.assertTrue(req.body.endswith("\u2026"))

    def test_body_falls_back_to_last_event(self) -> None:
        req = build_request(session(question=None), iso(T0))
        self.assertEqual(req.body, "asked a question")

    def test_body_final_fallback(self) -> None:
        req = build_request({"id": "s", "question": None, "lastEvent": None}, iso(T0))
        self.assertEqual(req.body, "This session is waiting for your input.")

    def test_missing_title_has_a_label(self) -> None:
        req = build_request({"id": "s", "title": "   "}, iso(T0))
        self.assertIn("a Claude session", req.title)

    def test_trim_collapses_whitespace(self) -> None:
        self.assertEqual(trim("a\n\n  b\tc", 50), "a b c")

    def test_short_text_is_untouched(self) -> None:
        self.assertEqual(trim("hello", 50), "hello")


class ParseIsoTests(unittest.TestCase):
    def test_zulu(self) -> None:
        self.assertEqual(parse_iso("2026-08-26T20:00:00Z"), T0)

    def test_offset_is_normalised_to_utc(self) -> None:
        self.assertEqual(parse_iso("2026-08-26T14:00:00-06:00"), T0)

    def test_naive_is_treated_as_utc(self) -> None:
        self.assertEqual(parse_iso("2026-08-26T20:00:00"), T0)

    def test_garbage_is_none(self) -> None:
        for bad in ("", None, "yesterday", 12345, "2026-13-45T99:99:99Z"):
            with self.subTest(value=bad):
                self.assertIsNone(parse_iso(bad))


class ConfigTests(unittest.TestCase):
    def test_absent_block_defaults(self) -> None:
        self.assertEqual(parse_toast_config({"quietHours": None}), ToastConfig(True, 120))

    def test_values_are_read(self) -> None:
        cfg = parse_toast_config({"toast": {"enabled": False, "thresholdSec": 45}})
        self.assertEqual(cfg, ToastConfig(False, 45))

    def test_partial_block(self) -> None:
        self.assertEqual(parse_toast_config({"toast": {"thresholdSec": 300}}), ToastConfig(True, 300))

    def test_bad_types_fall_back_per_field(self) -> None:
        cfg = parse_toast_config({"toast": {"enabled": "yes", "thresholdSec": "soon"}})
        self.assertEqual(cfg, ToastConfig(True, 120))

    def test_bool_threshold_is_rejected(self) -> None:
        self.assertEqual(parse_toast_config({"toast": {"thresholdSec": True}}).threshold_sec, 120)

    def test_negative_threshold_is_rejected(self) -> None:
        self.assertEqual(parse_toast_config({"toast": {"thresholdSec": -5}}).threshold_sec, 120)

    def test_non_dict_documents(self) -> None:
        for bad in (None, [], "x", {"toast": "on"}):
            with self.subTest(value=bad):
                self.assertEqual(parse_toast_config(bad), ToastConfig(True, 120))

    def test_reader_missing_file_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ConfigReader(Path(tmp) / "nope.json").read(), ToastConfig(True, 120))

    def test_reader_reloads_on_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"toast": {"thresholdSec": 30}}), encoding="utf-8")
            reader = ConfigReader(path)
            self.assertEqual(reader.read().threshold_sec, 30)
            # 900, not 90: the reader caches on (st_mtime_ns, st_size), and a same-length
            # payload written inside one mtime tick is (correctly) not reloaded — with 90
            # this test flaked whenever both writes landed in the same tick.
            path.write_text(json.dumps({"toast": {"thresholdSec": 900}}), encoding="utf-8")
            self.assertEqual(reader.read().threshold_sec, 900)

    def test_reader_keeps_last_good_on_corrupt_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"toast": {"thresholdSec": 30}}), encoding="utf-8")
            reader = ConfigReader(path)
            self.assertEqual(reader.read().threshold_sec, 30)
            path.write_text("{ half-written", encoding="utf-8")
            self.assertEqual(reader.read().threshold_sec, 30)

    def test_reader_never_creates_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            ConfigReader(path).read()
            self.assertFalse(path.exists(), "notifier must never write the companion's config")


class AdapterTests(unittest.TestCase):
    """The Windows call is isolated; only its pure payload construction is asserted here."""

    def setUp(self) -> None:
        self.adapter = PowerShellToastAdapter(icon_path=None)

    def test_xml_escapes_hostile_text(self) -> None:
        req = ToastRequest("s", "t", "A & B <tag>", "quote \" apos ' and <script>")
        xml = self.adapter.build_xml(req)
        self.assertIn("A &amp; B &lt;tag&gt;", xml)
        self.assertNotIn("<script>", xml)
        self.assertIn("<text placement='attribution'>SideCrab</text>", xml)

    def test_a_control_char_still_builds_well_formed_xml(self) -> None:
        """F1 root fix: an XML-1.0-illegal control byte in the title or body used to survive
        trim() into build_xml and make LoadXml throw (show() -> False, spell consumed, never
        re-toast). The escape path now strips them, so the payload parses. ElementTree's expat
        rejects these bytes exactly as Windows' LoadXml does, so this is a faithful proof."""
        import xml.etree.ElementTree as ET

        # One from each illegal band: 0x00-0x08, 0x0E-0x1B (trim leaves these), plus 0x0B/0x0C.
        req = ToastRequest("s1", iso(T0), "waiting\x07 \x00title", "over\x1bwrite \x08the \x0bfile?")
        xml = self.adapter.build_xml(req)
        ET.fromstring(xml)  # must not raise — was a ParseError before the fix
        for bad in ("\x07", "\x00", "\x1b", "\x08", "\x0b"):
            self.assertNotIn(bad, xml)

    def test_a_control_char_in_an_actionable_id_path_still_parses(self) -> None:
        """The button URIs skip trim() entirely, so the strip has to live in the escape path,
        not in trim(). A valid session id carries no control byte, but prove the whole
        actionable payload (buttons included) is well-formed when the TEXT carries one."""
        import xml.etree.ElementTree as ET

        req = ToastRequest("abc-123", iso(T0), "Claude is waiting\x1b", "Should I \x07overwrite?")
        xml = self.adapter.build_xml(req)
        ET.fromstring(xml)
        self.assertIn("<actions>", xml)
        self.assertIn(f"{ACK_SCHEME}:abc-123", xml)

    def test_quotes_in_the_text_survive_intact_and_still_parse(self) -> None:
        """F3, the text half. Quotes are LEGAL in element content, so the fix must not start
        mangling them: the toast has to still SHOW `"` and `'`, not swallow or entity-ify them
        into noise. Asserted through the parser, not on the raw string, because that is what
        the reader of the toast actually gets back."""
        import xml.etree.ElementTree as ET

        title = 'Delete "C:\\Users\\joe\'s files"?'
        body = "Run `rm -rf 'don't'` & <exit>?"
        root = ET.fromstring(self.adapter.build_xml(ToastRequest("abc-123", iso(T0), title, body)))
        texts = [e.text for e in root.iter("text")]
        self.assertIn(title, texts)
        self.assertIn(body, texts)

    def test_a_quote_cannot_break_out_of_an_attribute(self) -> None:
        """F3, the attribute half — the reason the escape is split in two. saxutils.escape()
        handles &<> and NOT quotes, and build_xml single-quotes its attributes, so a value
        carrying `'` used to close the attribute early and turn the rest into markup. The
        breakout payload here is the real one: a second <action> whose protocol URI is an
        attacker's. Round-tripped through the parser, so it proves BOTH halves — no injected
        element, and the characters still come back exactly as given."""
        import xml.etree.ElementTree as ET

        hostile = "id' arguments='calc:' x='" + '"' + "quoted\"/><action content='pwn"
        root = ET.fromstring(f"<r a='{sidecrab_toast.xml_attr_escape(hostile)}'/>")
        self.assertEqual(root.get("a"), hostile)  # rendered, not swallowed
        self.assertEqual(len(list(root)), 0, "an attribute value became an element")
        self.assertNotIn("'", sidecrab_toast.xml_attr_escape(hostile))
        self.assertNotIn('"', sidecrab_toast.xml_attr_escape(hostile))

    def test_attribute_escape_still_strips_control_chars(self) -> None:
        """The quote escape is layered ON the F1 strip, not instead of it — an illegal byte in
        an attribute kills the document just as surely as one in element text."""
        self.assertEqual(sidecrab_toast.xml_attr_escape("a\x07b\x00c"), "abc")

    def test_every_attribute_value_is_routed_through_the_quote_safe_escape(self) -> None:
        """F3's REAL gate, and the reason it is written this way. Reverting build_xml's
        attribute sites to the quote-blind xml_escape breaks nothing observable today — every
        value that reaches an attribute is a constant, a ^[A-Za-z0-9-]{1,64}$ id, or a
        percent-encoded path, so no output changes and a behavioural test would report success
        forever. The defect F3 guards against is a ROUTING defect: the next edit that puts a
        title or a tool name in an attribute inheriting quote-blind escaping.

        So the routing itself is what is asserted. Both escapes are swapped for distinguishable
        markers and the payload is read back: anything landing between ='...' must have come
        from the attribute escape. Vacuity is checked too — the text marker must appear
        somewhere, or a build_xml that stopped interpolating at all would pass."""
        with tempfile.TemporaryDirectory() as tmp:
            icon = Path(tmp) / "sidecrab.png"
            icon.write_bytes(b"\x89PNG\r\n\x1a\n")
            adapter = PowerShellToastAdapter(icon_path=icon)
            with unittest.mock.patch.object(sidecrab_toast, "xml_escape", lambda v: "TEXTMARK"), \
                 unittest.mock.patch.object(sidecrab_toast, "xml_attr_escape", lambda v: "ATTRMARK"):
                # actionable, so the two <action> elements (4 attribute interpolations) are built
                xml = adapter.build_xml(ToastRequest("abc-123", iso(T0), "t", "b"))

        self.assertIn("TEXTMARK", xml, "build_xml stopped interpolating - the test went vacuous")
        self.assertIn("ATTRMARK", xml, "no attribute was interpolated - the test went vacuous")
        for value in re.findall(r"='([^']*)'", xml):
            self.assertNotEqual(
                value, "TEXTMARK", "an attribute value is escaped with the quote-blind xml_escape"
            )

    def test_a_quote_in_the_icon_path_keeps_the_payload_well_formed(self) -> None:
        """The one attribute a deployer can actually influence. `'` is legal in a Windows
        filename (`"` is not) — and `quote()` percent-encodes it to %27, which is WHY the
        reachable path is already safe and F3 is defence-in-depth. Real file on disk, because
        build_xml skips the image unless is_file() says yes."""
        import xml.etree.ElementTree as ET

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "joe's icons"
            folder.mkdir()
            icon = folder / "sidecrab.png"
            icon.write_bytes(b"\x89PNG\r\n\x1a\n")
            xml = PowerShellToastAdapter(icon_path=icon).build_xml(ToastRequest("s", "t", "a", "b"))
            root = ET.fromstring(xml)
            src = [e.get("src") for e in root.iter("image")][0]
            self.assertIn("appLogoOverride", xml)
            self.assertTrue(src.startswith("file:///"), src)

    def test_xml_omits_image_without_icon(self) -> None:
        self.assertNotIn("appLogoOverride", self.adapter.build_xml(ToastRequest("s", "t", "a", "b")))

    def test_icon_uri_keeps_the_drive_colon_unescaped(self) -> None:
        """A quoted 'C%3A' silently renders a logo-less toast — regression guard."""
        with tempfile.TemporaryDirectory() as tmp:
            icon = Path(tmp) / "sidecrab.png"
            icon.write_bytes(b"\x89PNG\r\n\x1a\n")
            xml = PowerShellToastAdapter(icon_path=icon).build_xml(ToastRequest("s", "t", "a", "b"))
            self.assertIn("appLogoOverride", xml)
            self.assertNotIn("%3A", xml)
            self.assertRegex(xml, r"src='file:///[A-Za-z]:/")

    def test_icon_uri_escapes_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "side crab"
            folder.mkdir()
            icon = folder / "sidecrab.png"
            icon.write_bytes(b"\x89PNG\r\n\x1a\n")
            xml = PowerShellToastAdapter(icon_path=icon).build_xml(ToastRequest("s", "t", "a", "b"))
            self.assertIn("%20", xml)

    def test_script_carries_xml_as_base64_only(self) -> None:
        """No question text may appear literally in PowerShell source."""
        req = ToastRequest("s1", "t", "title", "'; Remove-Item C:\\ -Recurse; '")
        script = self.adapter.build_script(req)
        self.assertNotIn("Remove-Item", script)
        self.assertIn("FromBase64String", script)

    def test_script_tag_is_sanitised(self) -> None:
        script = PowerShellToastAdapter().build_script(ToastRequest("a'b;c$d", "t", "x", "y"))
        self.assertIn("$toast.Tag = 'a-b-c-d'", script)

    def test_pinned_to_windows_powershell(self) -> None:
        self.assertTrue(PowerShellToastAdapter.POWERSHELL_EXE.lower().endswith("windowspowershell\\v1.0\\powershell.exe"))

    def test_recording_adapter_records(self) -> None:
        rec = RecordingToastAdapter()
        req = ToastRequest("s", "t", "a", "b")
        self.assertTrue(rec.show(req))
        self.assertEqual(rec.shown, [req])


class AckActionTests(unittest.TestCase):
    """The Acknowledge button. A toast payload is written once and replayed by the shell for
    as long as it sits in Action Center, so anything that escapes an attribute here is a
    STORED injection — hence the charset gate before the string is ever embedded."""

    def setUp(self) -> None:
        self.adapter = PowerShellToastAdapter(icon_path=None)

    def req(self, sid: str = "0f9c1e2a-7b34-4d51-9c8e-2a1b3c4d5e6f") -> ToastRequest:
        return ToastRequest(sid, iso(T0), "Claude is waiting", "Should I overwrite it?")

    def test_xml_carries_the_action_with_the_session_uri(self) -> None:
        xml = self.adapter.build_xml(self.req("abc-123"))
        self.assertIn("<actions>", xml)
        self.assertIn("activationType='protocol'", xml)
        self.assertIn(f"content='{ACK_BUTTON_CONTENT}'", xml)
        self.assertIn(f"arguments='{ACK_SCHEME}:abc-123'", xml)

    def test_the_toast_still_carries_the_question(self) -> None:
        """The button is an addition, not a replacement — a v0.6 toast's text is unchanged."""
        xml = self.adapter.build_xml(self.req())
        self.assertIn("<text>Claude is waiting</text>", xml)
        self.assertIn("<text>Should I overwrite it?</text>", xml)
        self.assertIn("<text placement='attribution'>SideCrab</text>", xml)

    def test_actions_sit_inside_the_toast_element_after_the_visual(self) -> None:
        xml = self.adapter.build_xml(self.req())
        self.assertLess(xml.index("</visual>"), xml.index("<actions>"))
        self.assertLess(xml.index("</actions>"), xml.index("</toast>"))

    def test_an_invalid_session_id_omits_the_button_but_keeps_the_toast(self) -> None:
        """Refusing the BUTTON, not the notification: a question waiting on the operator still has
        to reach him."""
        hostile = "s' /><action activationType='protocol' arguments='file:///C:/Windows'/><x a='"
        for sid in (hostile, "", "a" * 65, "a b", "a/b", "../x", "sid;calc"):
            with self.subTest(sid=sid):
                xml = self.adapter.build_xml(self.req(sid))
                self.assertNotIn("<actions>", xml)
                self.assertNotIn(f"{ACK_SCHEME}:", xml)
                self.assertIn("<text>Should I overwrite it?</text>", xml)

    def test_ack_uri_boundaries(self) -> None:
        self.assertEqual(ack_uri("a"), f"{ACK_SCHEME}:a")
        self.assertEqual(ack_uri("a" * 64), f"{ACK_SCHEME}:" + "a" * 64)
        for bad in ("a" * 65, "", "a b", "a.b", "a_b", None, 42):
            with self.subTest(value=bad):
                self.assertIsNone(ack_uri(bad))

    def test_the_uri_never_reaches_powershell_source_unencoded(self) -> None:
        """Same idiom as the question text: the whole payload crosses as base64."""
        script = self.adapter.build_script(self.req("abc-123"))
        self.assertNotIn(f"{ACK_SCHEME}:abc-123", script)
        self.assertIn("FromBase64String", script)

    def test_the_pattern_is_the_conservative_one_the_contract_names(self) -> None:
        self.assertEqual(SESSION_ID_PATTERN, r"^[A-Za-z0-9-]{1,64}$")


class SchemaTests(unittest.TestCase):
    """The stale-SUPPORTED_SCHEMAS trap, pinned to the contract instead of to a memory.

    A consumer that rejects the schema production serves goes silent with a Running task and
    one startup warning. It happened (crabd 4 vs notifier {1,2,3}); this makes the next bump
    fail a test instead.
    """

    CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "STATE-CONTRACT.md"

    #: Every wording the contract has used to state a live schema number. The v0.6.1
    #: versioning rework retitled the document ("schema 5-compat, feature-detected") and
    #: moved the current number into prose ("pinned at **5**"), which silently stopped the
    #: single old regex matching — the assertion below then failed on a None rather than on
    #: a schema gap. A missing number is now a test failure in its own right (see
    #: test_the_contract_still_states_a_current_schema).
    SCHEMA_PATTERNS = (
        r"crabd emits `\"schema\": (\d+)`",      # the historical per-version headers
        r"\(schema (\d+)[-\s),]",                # the document title
        r"pinned at \*\*(\d+)\*\*",              # the rework's "last BREAKING shape"
    )

    def declared_schemas(self) -> set[int]:
        text = self.CONTRACT.read_text(encoding="utf-8")
        found: set[int] = set()
        for pattern in self.SCHEMA_PATTERNS:
            found.update(int(m) for m in re.findall(pattern, text))
        return found

    def test_the_contract_still_states_a_current_schema(self) -> None:
        """Guards the guard: a retitled contract must not turn the check below into a no-op."""
        self.assertTrue(self.declared_schemas(), "could not read any schema number out of the contract")

    def test_accepts_every_schema_the_contract_declares(self) -> None:
        declared = self.declared_schemas()
        self.assertTrue(declared, "could not read any schema number out of the contract")
        missing = sorted(declared - set(SUPPORTED_SCHEMAS))
        self.assertEqual(missing, [], f"notifier would stand down on schema(s) {missing}")

    def test_is_contiguous_from_one(self) -> None:
        """Every schema so far has been additive, so a gap means someone dropped one by hand."""
        self.assertEqual(sorted(SUPPORTED_SCHEMAS), list(range(1, max(SUPPORTED_SCHEMAS) + 1)))


class AumidTests(unittest.TestCase):
    """Which app identity a toast is raised under. The registry read is always injected —
    no test here may pass or fail because of what is registered on the running machine."""

    REQ = ToastRequest("s1", "t", "title", "body")

    @staticmethod
    def counting_probe(answer: str | None):
        calls: list[int] = []

        def probe() -> str | None:
            calls.append(1)
            return answer

        return probe, calls

    def test_uses_sidecrab_aumid_when_registered(self) -> None:
        probe, _ = self.counting_probe(SIDECRAB_AUMID)
        adapter = PowerShellToastAdapter(aumid_probe=probe)
        self.assertEqual(adapter.aumid, SIDECRAB_AUMID)
        self.assertIn(f"CreateToastNotifier('{SIDECRAB_AUMID}')", adapter.build_script(self.REQ))

    def test_falls_back_to_the_borrowed_aumid_when_absent(self) -> None:
        probe, _ = self.counting_probe(None)
        adapter = PowerShellToastAdapter(aumid_probe=probe)
        self.assertEqual(adapter.aumid, PowerShellToastAdapter.BORROWED_AUMID)
        self.assertIn("WindowsPowerShell", adapter.build_script(self.REQ))

    def test_the_registry_is_read_once_not_once_per_toast(self) -> None:
        probe, calls = self.counting_probe(SIDECRAB_AUMID)
        adapter = PowerShellToastAdapter(aumid_probe=probe)
        for _ in range(3):
            adapter.build_script(self.REQ)
        self.assertEqual(len(calls), 1)

    def test_an_explicit_aumid_pins_the_choice_and_skips_the_probe(self) -> None:
        probe, calls = self.counting_probe(SIDECRAB_AUMID)
        adapter = PowerShellToastAdapter(aumid="Some.Other.Aumid", aumid_probe=probe)
        self.assertEqual(adapter.aumid, "Some.Other.Aumid")
        self.assertEqual(calls, [])

    def test_injected_probe_is_never_module_cached(self) -> None:
        """Otherwise one test's fake answer would leak into every later caller."""
        self.assertEqual(registered_aumid(probe=lambda: SIDECRAB_AUMID), SIDECRAB_AUMID)
        self.assertIsNone(registered_aumid(probe=lambda: None))

    def test_real_probe_answers_one_of_two_things_and_never_raises(self) -> None:
        """Runs against the live registry: either state is a pass, an exception is not."""
        self.assertIn(probe_registered_aumid(), (None, SIDECRAB_AUMID))

    def test_a_borrowed_answer_is_never_latched_on_the_adapter(self) -> None:
        """REGRESSION (2026-08-26). The old adapter latched `borrowed` for the process
        lifetime, so a notifier that was running when setup registered the AUMID kept
        borrowing until someone restarted the Scheduled Task — and nothing told them to.
        Now only a POSITIVE answer latches."""
        answers = [None, None, SIDECRAB_AUMID]
        adapter = PowerShellToastAdapter(aumid_probe=lambda: answers.pop(0))
        self.assertEqual(adapter.aumid, PowerShellToastAdapter.BORROWED_AUMID)
        self.assertEqual(adapter.aumid, PowerShellToastAdapter.BORROWED_AUMID)
        self.assertEqual(adapter.aumid, SIDECRAB_AUMID, "the key appeared; no restart should be needed")

    def test_a_positive_answer_stops_the_re_probing(self) -> None:
        """The other half: once found, it must not be re-read (nor lost if a read later fails)."""
        answers = [SIDECRAB_AUMID, None, None]
        adapter = PowerShellToastAdapter(aumid_probe=lambda: answers.pop(0))
        self.assertEqual([adapter.aumid, adapter.aumid, adapter.aumid], [SIDECRAB_AUMID] * 3)
        self.assertEqual(len(answers), 2, "only the first read should have happened")

    def test_the_module_cache_expires_a_negative_answer_but_not_a_positive_one(self) -> None:
        """The registry hit itself is throttled to AUMID_REPROBE_SEC while borrowed, and
        never repeated once found. `now` is injected — no test may sleep."""
        calls: list[str | None] = []

        def fake_probe() -> str | None:
            answer = None if len(calls) < 2 else SIDECRAB_AUMID
            calls.append(answer)
            return answer

        original = sidecrab_toast.probe_registered_aumid
        sidecrab_toast.probe_registered_aumid = fake_probe
        sidecrab_toast._registered_aumid_cache = None
        try:
            self.assertIsNone(registered_aumid(now=0.0))
            self.assertIsNone(registered_aumid(now=1.0), "still inside the cooldown")
            self.assertEqual(len(calls), 1)

            self.assertIsNone(registered_aumid(now=AUMID_REPROBE_SEC))
            self.assertEqual(len(calls), 2, "the cooldown expired, so it re-read")

            self.assertEqual(registered_aumid(now=2 * AUMID_REPROBE_SEC), SIDECRAB_AUMID)
            self.assertEqual(registered_aumid(now=10 * AUMID_REPROBE_SEC), SIDECRAB_AUMID)
            self.assertEqual(len(calls), 3, "a positive answer is never re-probed")
        finally:
            sidecrab_toast.probe_registered_aumid = original
            sidecrab_toast._registered_aumid_cache = None

    def test_the_probe_records_why_it_came_back_empty(self) -> None:
        """ROOT-CAUSE guard. 'borrowed' used to be indistinguishable between 'the key is not
        there' and 'the key cannot be read', which is what made the 2026-08-26 investigation
        necessary. The reason now rides the log line."""
        sidecrab_toast._last_aumid_probe_error = None
        answer = probe_registered_aumid()
        detail = sidecrab_toast.aumid_probe_detail()
        if answer is None:
            self.assertIsNotNone(detail, "an empty probe must say why")
            self.assertRegex(detail, r"winerror=|winreg unavailable")
        else:
            self.assertIsNone(detail, "a successful probe must not leave a stale reason behind")

    def test_the_probe_reads_the_key_the_setup_script_writes(self) -> None:
        """The 2026-08-26 mystery was NOT a path bug — this pins that the path stays honest,
        so the next 'borrowed' report is read as 'the key really is not there for me'."""
        self.assertEqual(AUMID_REGISTRY_SUBKEY, r"SOFTWARE\Classes\AppUserModelId\SideCrab.Notifier")
        self.assertTrue(AUMID_REGISTRY_SUBKEY.endswith(SIDECRAB_AUMID))

    def test_aumid_constants_match_the_setup_script(self) -> None:
        """Cross-lane contract: CreateToastNotifier matches the registered AUMID verbatim,
        so a rename on the PowerShell side alone silently sends toasts back to the fallback."""
        common = Path(__file__).resolve().parents[2] / "setup" / "SideCrab.Common.ps1"
        text = common.read_text(encoding="utf-8")
        self.assertIn(f"'{SIDECRAB_AUMID}'", text)
        self.assertIn(f"HKCU:\\{AUMID_REGISTRY_SUBKEY}", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
