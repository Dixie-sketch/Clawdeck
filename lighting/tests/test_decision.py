"""Headless unit tests for sidecrab-glow. `python -m unittest discover lighting/tests`

No SDK, no iCUE, no network, no hardware: the decision logic is pure and the
render loop is exercised through NullAdapter.
"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sidecrab_glow  # noqa: E402
from decision import (  # noqa: E402
    ESCALATE_AFTER_SEC,
    LEVEL_ESCALATED,
    LEVEL_NONE,
    LEVEL_NORMAL,
    STALE_AFTER_SEC,
    decide,
)
from icue import NullAdapter  # noqa: E402

NOW = datetime(2026, 8, 26, 20, 0, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def session(state="needs_input", acked=False, age_sec=10, sid="s1"):
    return {
        "id": sid,
        "title": "a session",
        "state": state,
        "acked": acked,
        "stateSince": iso(NOW - timedelta(seconds=age_sec)),
    }


def doc(sessions=None, quiet=None, schema=2, gen_age_sec=1):
    d = {
        "schema": schema,
        "generatedAt": iso(NOW - timedelta(seconds=gen_age_sec)),
        "sessions": sessions if sessions is not None else [],
    }
    if quiet is not None:
        d["quiet"] = quiet
    return d


class TestAlerting(unittest.TestCase):
    def test_unacked_needs_input_glows(self):
        d = decide(doc([session()]), NOW)
        self.assertTrue(d.should_glow)
        self.assertEqual(d.level, LEVEL_NORMAL)
        self.assertEqual(d.reason, "needs-input")
        self.assertEqual(d.alert_count, 1)

    def test_no_sessions_is_dark(self):
        d = decide(doc([]), NOW)
        self.assertFalse(d.should_glow)
        self.assertEqual(d.reason, "no-unacked-needs-input")

    def test_working_sessions_ignored(self):
        d = decide(doc([session(state="working"), session(state="done")]), NOW)
        self.assertFalse(d.should_glow)

    def test_counts_only_unacked(self):
        d = decide(
            doc([session(sid="a"), session(sid="b", acked=True), session(sid="c")]),
            NOW,
        )
        self.assertTrue(d.should_glow)
        self.assertEqual(d.alert_count, 2)


class TestAckSuppression(unittest.TestCase):
    def test_acked_needs_input_does_not_glow(self):
        d = decide(doc([session(acked=True)]), NOW)
        self.assertFalse(d.should_glow)
        self.assertEqual(d.reason, "no-unacked-needs-input")

    def test_all_acked_is_dark(self):
        d = decide(doc([session(sid="a", acked=True), session(sid="b", acked=True)]), NOW)
        self.assertFalse(d.should_glow)

    def test_absent_acked_reads_as_unacked(self):
        """Schema 1 has no `acked` field — absent must not suppress the alert."""
        s = session()
        del s["acked"]
        d = decide(doc([s], schema=1), NOW)
        self.assertTrue(d.should_glow)

    def test_null_acked_reads_as_unacked(self):
        d = decide(doc([session(acked=None)]), NOW)
        self.assertTrue(d.should_glow)


class TestQuietSuppression(unittest.TestCase):
    def test_quiet_active_suppresses_entirely(self):
        d = decide(doc([session()], quiet={"active": True, "start": "22:00", "end": "07:00"}), NOW)
        self.assertFalse(d.should_glow)
        self.assertEqual(d.level, LEVEL_NONE)
        self.assertEqual(d.reason, "quiet-hours")

    def test_quiet_inactive_still_glows(self):
        d = decide(doc([session()], quiet={"active": False}), NOW)
        self.assertTrue(d.should_glow)

    def test_quiet_null_still_glows(self):
        d = decide(doc([session()], quiet=None), NOW)
        self.assertTrue(d.should_glow)

    def test_quiet_beats_escalation(self):
        old = session(age_sec=ESCALATE_AFTER_SEC * 4)
        d = decide(doc([old], quiet={"active": True}), NOW)
        self.assertFalse(d.should_glow)


class TestDeadFeedRelease(unittest.TestCase):
    def test_unreachable_feed_releases(self):
        d = decide(None, NOW)
        self.assertFalse(d.should_glow)
        self.assertEqual(d.reason, "feed-unreachable")

    def test_stale_feed_releases(self):
        d = decide(doc([session()], gen_age_sec=STALE_AFTER_SEC + 5), NOW)
        self.assertFalse(d.should_glow)
        self.assertEqual(d.reason, "feed-stale")

    def test_fresh_feed_at_the_boundary_still_glows(self):
        d = decide(doc([session()], gen_age_sec=STALE_AFTER_SEC - 1), NOW)
        self.assertTrue(d.should_glow)

    def test_unsupported_schema_is_dead(self):
        d = decide(doc([session()], schema=99), NOW)
        self.assertFalse(d.should_glow)
        self.assertEqual(d.reason, "feed-schema-unsupported")

    def test_schema_1_2_3_all_accepted(self):
        for schema in (1, 2, 3):
            with self.subTest(schema=schema):
                self.assertTrue(decide(doc([session()], schema=schema), NOW).should_glow)

    def test_missing_timestamp_is_dead(self):
        d = doc([session()])
        del d["generatedAt"]
        self.assertEqual(decide(d, NOW).reason, "feed-no-timestamp")

    def test_garbage_timestamp_is_dead(self):
        d = doc([session()])
        d["generatedAt"] = "not-a-date"
        self.assertEqual(decide(d, NOW).reason, "feed-no-timestamp")

    def test_non_dict_doc_is_dead(self):
        self.assertEqual(decide([1, 2, 3], NOW).reason, "feed-malformed")

    def test_missing_sessions_is_dark(self):
        d = doc()
        del d["sessions"]
        self.assertEqual(decide(d, NOW).reason, "feed-no-sessions")

    def test_junk_session_entries_do_not_crash(self):
        d = decide(doc([None, "nope", 5, session()]), NOW)
        self.assertTrue(d.should_glow)
        self.assertEqual(d.alert_count, 1)


class TestTrailingZTimestamps(unittest.TestCase):
    """crabd emits every timestamp with a literal `Z`. `datetime.fromisoformat` only
    learned to parse that in 3.11, so before the defensive strip in `_parse_iso` an older
    interpreter turned every real feed stamp into None -> "feed-no-timestamp" and the glow
    sat permanently dark against a healthy feed. The suite's `iso()` helper already emits
    `...Z`, so the rest of the file is the 3.11+ path; these pin the strip itself."""

    def test_a_trailing_Z_generatedAt_parses_and_glows(self):
        d = doc([session()])
        self.assertTrue(d["generatedAt"].endswith("Z"))  # the shape crabd actually sends
        self.assertTrue(decide(d, NOW).should_glow)

    def test_a_trailing_Z_stateSince_drives_escalation(self):
        old = session(age_sec=ESCALATE_AFTER_SEC + 10)
        self.assertTrue(old["stateSince"].endswith("Z"))
        self.assertEqual(decide(doc([old]), NOW).level, LEVEL_ESCALATED)

    def test_an_explicit_offset_timestamp_still_parses(self):
        d = doc([session()])
        d["generatedAt"] = (NOW - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        self.assertTrue(decide(d, NOW).should_glow)

    def test_a_naive_timestamp_still_parses_as_utc(self):
        d = doc([session()])
        d["generatedAt"] = (NOW - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
        self.assertTrue(decide(d, NOW).should_glow)


class TestEscalation(unittest.TestCase):
    def test_fresh_alert_is_normal(self):
        self.assertEqual(decide(doc([session(age_sec=30)]), NOW).level, LEVEL_NORMAL)

    def test_just_under_five_minutes_is_normal(self):
        d = decide(doc([session(age_sec=ESCALATE_AFTER_SEC - 1)]), NOW)
        self.assertEqual(d.level, LEVEL_NORMAL)

    def test_five_minutes_escalates(self):
        d = decide(doc([session(age_sec=ESCALATE_AFTER_SEC)]), NOW)
        self.assertEqual(d.level, LEVEL_ESCALATED)

    def test_oldest_unacked_session_drives_escalation(self):
        d = decide(
            doc([session(sid="new", age_sec=5),
                 session(sid="old", age_sec=ESCALATE_AFTER_SEC + 60)]),
            NOW,
        )
        self.assertEqual(d.level, LEVEL_ESCALATED)
        self.assertGreaterEqual(d.oldest_age_sec, ESCALATE_AFTER_SEC)

    def test_acked_old_session_does_not_escalate_a_fresh_one(self):
        d = decide(
            doc([session(sid="old", age_sec=ESCALATE_AFTER_SEC * 3, acked=True),
                 session(sid="new", age_sec=5)]),
            NOW,
        )
        self.assertEqual(d.level, LEVEL_NORMAL)

    def test_unparseable_state_since_does_not_escalate(self):
        s = session()
        s["stateSince"] = "garbage"
        d = decide(doc([s]), NOW)
        self.assertTrue(d.should_glow)
        self.assertEqual(d.level, LEVEL_NORMAL)

    def test_future_state_since_clamps_to_zero(self):
        d = decide(doc([session(age_sec=-600)]), NOW)
        self.assertEqual(d.level, LEVEL_NORMAL)


class TestPulseMath(unittest.TestCase):
    def test_brightness_stays_inside_the_band(self):
        _, lo, hi = sidecrab_glow.PULSE_NORMAL
        for i in range(101):
            b = sidecrab_glow.brightness_at(i / 100.0, LEVEL_NORMAL)
            self.assertGreaterEqual(b, lo - 1e-9)
            self.assertLessEqual(b, hi + 1e-9)

    def test_escalated_is_brighter_and_faster(self):
        self.assertGreater(
            sidecrab_glow.brightness_at(0.5, LEVEL_ESCALATED),
            sidecrab_glow.brightness_at(0.5, LEVEL_NORMAL),
        )
        self.assertLess(
            sidecrab_glow.period_for(LEVEL_ESCALATED),
            sidecrab_glow.period_for(LEVEL_NORMAL),
        )

    def test_curve_is_continuous_at_the_wrap(self):
        self.assertAlmostEqual(
            sidecrab_glow.brightness_at(0.0, LEVEL_NORMAL),
            sidecrab_glow.brightness_at(1.0, LEVEL_NORMAL),
            places=9,
        )

    def test_scale_is_terracotta_at_full(self):
        self.assertEqual(sidecrab_glow.scale(sidecrab_glow.TERRACOTTA, 1.0), (217, 119, 87))
        self.assertEqual(sidecrab_glow.scale(sidecrab_glow.TERRACOTTA, 0.0), (0, 0, 0))

    def test_scale_clamps(self):
        self.assertEqual(sidecrab_glow.scale((10, 10, 10), 5.0), (10, 10, 10))
        self.assertEqual(sidecrab_glow.scale((10, 10, 10), -5.0), (0, 0, 0))


class TestRenderLoopRelease(unittest.TestCase):
    """The release path is the one that must never fail — it is what gives the
    user their own lighting back."""

    def _glow(self):
        adapter = NullAdapter()
        adapter.led_count = 12  # pretend it has LEDs
        return adapter, sidecrab_glow.Glow(adapter, None)

    def test_glow_then_dark_releases(self):
        adapter, glow = self._glow()
        glow.tick(0.05, decide(doc([session()]), NOW), 100.0)
        self.assertTrue(glow.acquired)
        self.assertTrue(adapter.paints)

        glow.tick(0.05, decide(None, NOW), 100.1)  # feed died
        self.assertFalse(glow.acquired)
        self.assertEqual(adapter.released, 1)

    def test_quiet_hours_releases(self):
        adapter, glow = self._glow()
        glow.tick(0.05, decide(doc([session()]), NOW), 100.0)
        glow.tick(0.05, decide(doc([session()], quiet={"active": True}), NOW), 100.1)
        self.assertFalse(glow.acquired)
        self.assertEqual(adapter.released, 1)

    def test_ack_releases(self):
        adapter, glow = self._glow()
        glow.tick(0.05, decide(doc([session()]), NOW), 100.0)
        glow.tick(0.05, decide(doc([session(acked=True)]), NOW), 100.1)
        self.assertFalse(glow.acquired)

    def test_release_is_idempotent(self):
        adapter, glow = self._glow()
        glow.release()
        glow.release()
        self.assertEqual(adapter.released, 0)  # nothing was acquired

    def test_never_acquires_when_dark(self):
        adapter, glow = self._glow()
        glow.tick(0.05, decide(doc([]), NOW), 100.0)
        self.assertFalse(glow.acquired)
        self.assertFalse(adapter.paints)

    def test_paint_colour_is_a_scaled_terracotta(self):
        adapter, glow = self._glow()
        for i in range(20):
            glow.tick(0.05, decide(doc([session()]), NOW), 100.0 + i * 0.05)
        r, g, b = adapter.paints[-1]
        self.assertTrue(r >= g >= b, f"not terracotta-shaped: {(r, g, b)}")
        self.assertLessEqual(r, 217)


class TestFetch(unittest.TestCase):
    def test_fetch_failure_returns_none(self):
        # Nothing listens on this port; must degrade to None, not raise.
        self.assertIsNone(
            sidecrab_glow.fetch_state("http://127.0.0.1:1/v1/state", timeout=0.3)
        )


class ContractSchemaPin(unittest.TestCase):
    """The notifier's silent-standdown bug, guarded here the same way: the accepted set
    must include every schema number the contract has ever declared as served."""

    def test_accepts_every_contract_declared_schema(self):
        import pathlib, re as _re
        contract = (pathlib.Path(__file__).resolve().parents[2] / "docs" / "STATE-CONTRACT.md").read_text(encoding="utf-8")
        declared = {int(m) for m in _re.findall(r'"schema":\s*(\d+)', contract)}
        declared |= {int(m) for m in _re.findall(r'schema\s+(?:stays|is pinned at|marks[^0-9]*?)\**(\d)\**', contract)}
        self.assertTrue(declared, "guard-the-guard: contract parse found no schema numbers")
        import decision as _dec
        missing = {n for n in declared if 1 <= n <= 9} - _dec.ACCEPTED_SCHEMAS
        self.assertFalse(missing, f"contract declares schemas {sorted(missing)} the glow would refuse")

    def test_schema_5_feed_glows(self):
        doc5 = doc([session()]); doc5["schema"] = 5
        d = decide(doc5, NOW)
        self.assertTrue(d.should_glow)


if __name__ == "__main__":
    unittest.main()
