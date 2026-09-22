"""Lane M, part 1: the v0.35.0 findings (M-01 .. M-07).

Every one of them was reproduced before it was fixed - M-01 and M-02 against the
companion running on this machine on 2026-09-22, the rest here. A class whose name starts
with a finding id IS that finding's reproduction, so it has to fail against the code as it
was; several assert the shape that used to be wrong rather than merely the shape that is
now right, and the ones marked MUTATION GUARD exist because the obvious wrong fix would
have passed the test above them.

The six additive session members are in test_crabd_lanem.py. Fixtures are fictional;
nothing here reads the operator's own transcripts.
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
from test_crabd import (TempProjects, assistant_line, user_line,  # noqa: E402
                        write_jsonl)


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


class LaneMFacts(TempProjects):
    """Parse one synthetic transcript into a FileFacts."""

    def facts(self, objects, session_id="s-lane-m", project="P--acme", mtime=None):
        path = self.projects / project / f"{session_id}.jsonl"
        write_jsonl(path, objects, mtime=mtime)
        facts = crabd.FileFacts(path, session_id, False)
        facts.refresh()
        return facts


class M01LimitsTokenSourceTests(unittest.TestCase):
    """M-01: `sources.limitsToken` judged a reader the served `limits` was not using.

    _limits_block returns before LimitsReader.get() whenever the status line is serving,
    and get() is the reader's only caller - so the reader stops being polled and health()
    freezes on its last verdict. Measured on the live companion 2026-09-22: limits
    available true from the status line, beside limitsToken ok false with a lastAt 15
    minutes old that no action could clear.
    """

    class FrozenLimits:
        """A LimitsReader whose last fetch failed and which is never called again."""

        def __init__(self):
            self.gets = 0

        def get(self, now, force=False):
            self.gets += 1
            return {"available": False, "note": "SideCrab limits token rejected",
                    "fiveHour": None, "weekly": None, "extra": [],
                    "subscriptionType": None, "rateLimitTier": None}

        def health(self, now):
            return {"ok": False, "lastAt": now - 900,
                    "note": "SideCrab limits token rejected", "backoff": False}

    def builder(self, statusline):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        projects = Path(tmp.name) / "projects"
        projects.mkdir(parents=True)
        self.limits = self.FrozenLimits()
        return crabd.StateBuilder(crabd.TranscriptStore(projects),
                                  crabd.HookTracker(), self.limits, time.time(),
                                  crabd.UserConfig(Path(tmp.name) / "config.json"),
                                  statusline=statusline)

    class ServingStatusLine:
        last_at = time.time()

        def limits(self, now):
            return {"available": True, "note": None,
                    "fiveHour": {"utilization": 0.4, "resetsAt": None},
                    "weekly": None, "extra": [], "subscriptionType": "max",
                    "rateLimitTier": None}

        def context(self, sid, now, floor):
            return False, None

        def context_window(self, sid, now, floor):
            return None

        def prune(self, now):
            pass

    def test_absent_while_the_status_line_serves_the_gauges(self):
        state = self.builder(self.ServingStatusLine()).build()
        self.assertEqual(state["limits"]["source"], crabd.LIMITS_SOURCE_STATUSLINE)
        self.assertTrue(state["limits"]["available"])
        # The old behaviour served ok:false here, forever, about a feed nothing read.
        self.assertNotIn("limitsToken", state.get("sources", {}))

    def test_the_reader_is_not_even_polled_when_the_status_line_serves(self):
        """The mechanism, asserted directly: this is WHY the frozen verdict was
        unfixable. If a later change starts polling the reader again, this fails and the
        omission above should be revisited rather than quietly kept."""
        self.builder(self.ServingStatusLine()).build()
        self.assertEqual(self.limits.gets, 0)

    def test_still_judged_when_oauth_is_the_serving_source(self):
        """The guard must not turn into 'never report limitsToken'. With no status line
        the OAuth reader IS what fills the gauges, and its failure is the operator's to
        see."""
        state = self.builder(None).build()
        self.assertEqual(state["limits"]["source"], crabd.LIMITS_SOURCE_OAUTH)
        entry = state["sources"]["limitsToken"]
        self.assertFalse(entry["ok"])
        self.assertEqual(entry["note"], "SideCrab limits token rejected")


class M02BackoffNoteTests(unittest.TestCase):
    """M-02: during a 429 lockout the source note was the last-good CAVEAT.

    Live evidence 2026-09-22: sources.limitsToken {ok: false, ageSec: 3228.2, note:
    "limits as of 11:30 PM"} - a sentence that describes a healthy reading, served as the
    explanation for a source marked not-ok. The one line that names the lockout sat in an
    `or` fallback that could never fire, because the caveat was never empty.
    """

    def reader(self, now):
        reader = crabd.LimitsReader(cache_file=Path(crabd.LIMITS_CACHE_FILE))
        good = dict(crabd.LimitsReader._unavailable("x"))
        good.update({"available": True, "note": None})
        reader._last_good = good
        reader._last_good_at = now - (crabd.LIMITS_NOTE_STALE_SEC + 600)
        reader._cached = reader._aged(now)
        reader._fetched_at = now - 10
        reader._backoff_until = now + 300
        return reader

    def test_the_lockout_is_named_not_the_caveat(self):
        now = time.time()
        health = self.reader(now).health(now)
        self.assertTrue(health["backoff"])
        self.assertEqual(health["note"], crabd.LIMITS_BACKOFF_NOTE)
        self.assertNotIn("limits as of", health["note"])

    def test_the_served_limits_block_keeps_its_own_caveat(self):
        """The two notes answer different questions and must not be merged: the block's
        is a qualification beside lit gauges, the source's is a diagnosis."""
        now = time.time()
        served = self.reader(now)._cached
        self.assertTrue(served["available"])
        self.assertIn("limits as of", served["note"])

    def test_a_plain_failure_still_reports_its_own_note(self):
        """MUTATION GUARD: a fix that returned the lockout note unconditionally would
        pass the first test and hide every other failure the endpoint has."""
        now = time.time()
        reader = crabd.LimitsReader(cache_file=Path(crabd.LIMITS_CACHE_FILE))
        reader._cached = crabd.LimitsReader._unavailable("no Claude credentials on this "
                                                         "machine - run /login")
        reader._fetched_at = now
        health = reader.health(now)
        self.assertFalse(health["backoff"])
        self.assertIn("no Claude credentials", health["note"])


class M03CorruptConfigTests(unittest.TestCase):
    """M-03: a hand-edited config.json that does not parse was silently replaced.

    Reproduced 2026-09-22 with one trailing comma and one quiet tap: quietHours, budget,
    digest, panelApprovals and every continue prompt became
    {"quietHours": null, "allowReply": false} with nothing anywhere to say so.
    """

    HAND_EDITED = (
        '{\n'
        '  "quietHours": {"start": "22:00", "end": "07:00"},\n'
        '  "budget": {"dailyOutputTokens": 400000},\n'
        '  "panelApprovals": {"enabled": true},\n'   # the trailing comma below is the edit
        '  "continuePrompts": ["Ship it"],\n'
        '}\n'
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.json"

    def test_the_write_is_refused_and_the_file_is_untouched(self):
        self.path.write_text(self.HAND_EDITED, encoding="utf-8")
        config = crabd.UserConfig(self.path)
        self.assertFalse(config.set_keys({"quietHours": {"start": "09:00", "end": "10:00"}}))
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.HAND_EDITED)

    def test_a_missing_file_still_writes(self):
        """The refusal is for a file that EXISTS and cannot be read. A missing one has
        nothing to lose and must still take the write, or a first-ever quiet tap fails."""
        config = crabd.UserConfig(self.path)
        self.assertTrue(config.set_keys({"quietHours": {"start": "09:00", "end": "10:00"}}))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["quietHours"],
                         {"start": "09:00", "end": "10:00"})

    def test_an_empty_file_still_writes(self):
        """MUTATION GUARD, and the trap the fix had to avoid: an EMPTY config.json is the
        residue of the pre-A-03 truncating writer. Refusing there would wedge the operator
        out of their own settings permanently, with no way back through the panel."""
        self.path.write_text("", encoding="utf-8")
        config = crabd.UserConfig(self.path)
        self.assertTrue(config.set_keys({"quietHours": None}))
        self.assertTrue(self.path.exists())

    def test_a_valid_file_is_merged_not_replaced(self):
        self.path.write_text(json.dumps({"budget": {"dailyOutputTokens": 400000}}),
                             encoding="utf-8")
        config = crabd.UserConfig(self.path)
        self.assertTrue(config.set_keys({"quietHours": {"start": "09:00", "end": "10:00"}}))
        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["budget"], {"dailyOutputTokens": 400000})
        self.assertEqual(on_disk["quietHours"], {"start": "09:00", "end": "10:00"})


class M04EmptyTranscriptTests(LaneMFacts):
    """M-04: an EMPTY transcript was re-read on every pass and always reported changed.

    The no-change short-circuit tested `self.offset`, which is 0 for an empty file - so
    the one file that has nothing to give was the one re-opened every 2 s, forever.
    """

    def test_an_empty_file_is_read_once(self):
        path = self.projects / "P--acme" / "s-empty.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        facts = crabd.FileFacts(path, "s-empty", False)
        self.assertTrue(facts.refresh())     # the first read is a read
        self.assertFalse(facts.refresh())    # and every one after it is not
        self.assertFalse(facts.refresh())

    def test_a_file_that_grows_from_empty_is_still_picked_up(self):
        """MUTATION GUARD: 'read it once and never again' would pass the test above."""
        now = time.time()
        path = self.projects / "P--acme" / "s-grows.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")
        facts = crabd.FileFacts(path, "s-grows", False)
        facts.refresh()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(assistant_line("req_1", now)) + "\n")
        self.assertTrue(facts.refresh())
        self.assertIn("req_1", facts.requests)


class M05UnparseableRecordTests(LaneMFacts):
    """M-05: a record that does not parse was dropped with NO evidence at all.

    `skipped` is the only thing that makes "the parser dropped something" answerable, and
    the JSON-parse failure - a byte-order mark, a half-flushed record - returned without
    touching it.
    """

    def test_a_bom_costs_one_record_and_is_counted(self):
        now = time.time()
        path = self.projects / "P--acme" / "s-bom.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n".join(json.dumps(o) for o in
                         [assistant_line("req_1", now - 20),
                          assistant_line("req_2", now - 10)]) + "\n"
        path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        facts = crabd.FileFacts(path, "s-bom", False)
        facts.refresh()
        self.assertEqual(facts.skipped, 1)
        self.assertNotIn("req_1", facts.requests)     # the BOM'd line is the lost one
        self.assertIn("req_2", facts.requests)        # and the rest of the file is read

    def test_a_clean_file_counts_nothing(self):
        """MUTATION GUARD: a counter that increments on every record would make the
        number meaningless in the other direction."""
        now = time.time()
        facts = self.facts([assistant_line("req_1", now - 10),
                            user_line("go", now - 20)])
        self.assertEqual(facts.skipped, 0)


class M06ModuleIsolationTests(unittest.TestCase):
    """M-06: test_crabd_lanef.py had no module isolation while the other three had it.

    Latent rather than reproduced damage - nothing in that module happens to write those
    files today - but the limits cache under ~ was poisoned in exactly this shape on
    2026-08-26, and the guarantee is what stops the next test from being the one that
    does it. The assertion is on the module, not on a behaviour, because the defect IS
    the missing hook.
    """

    def test_every_companion_test_module_redirects_the_real_files(self):
        import test_crabd
        import test_crabd_datalane
        import test_crabd_lanef
        import test_crabd_livefire
        for module in (test_crabd, test_crabd_datalane, test_crabd_lanef,
                       test_crabd_livefire, sys.modules[__name__]):
            with self.subTest(module=module.__name__):
                self.assertTrue(callable(getattr(module, "setUpModule", None)),
                                f"{module.__name__} has no setUpModule")
                self.assertTrue(callable(getattr(module, "tearDownModule", None)),
                                f"{module.__name__} has no tearDownModule")


class M07CrabdLogTests(unittest.TestCase):
    """Part 3: crabd's own rotating log.

    The enabling defect behind M-01 and M-02: crabd had no log file, so a traceback under
    the Scheduled Task went to a stderr handle nobody owns.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "logs" / "crabd.log"

    def test_a_line_is_stamped_and_the_directory_is_created(self):
        crabd.CrabdLog(self.path).write("crabd: hello")
        line = self.path.read_text(encoding="utf-8").strip()
        self.assertRegex(line, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ crabd: hello$")

    def test_a_traceback_is_written_whole(self):
        try:
            raise ValueError("a wedged sampler")
        except ValueError as exc:
            crabd.CrabdLog(self.path).write("crabd: hwinfo error: ValueError", exc)
        body = self.path.read_text(encoding="utf-8")
        self.assertIn("crabd: hwinfo error: ValueError", body)
        self.assertIn("Traceback (most recent call last)", body)
        self.assertIn("a wedged sampler", body)

    def test_it_rotates_and_keeps_the_generations(self):
        log = crabd.CrabdLog(self.path)
        original = crabd.CRABD_LOG_MAX_BYTES
        crabd.CRABD_LOG_MAX_BYTES = 200
        self.addCleanup(lambda: setattr(crabd, "CRABD_LOG_MAX_BYTES", original))
        for n in range(60):
            log.write(f"crabd: line {n}")
        self.assertTrue(self.path.exists())
        self.assertTrue(self.path.with_name("crabd.log.1").exists())
        # The generation cap holds: nothing past it is kept.
        self.assertFalse(self.path.with_name(
            f"crabd.log.{crabd.CRABD_LOG_GENERATIONS + 1}").exists())

    def test_an_unwritable_target_never_raises(self):
        """It is called from inside the exception handlers whose whole job is to keep a
        failure away from the operator. A logger that can throw turns a swallowed error
        into a crashed thread."""
        log = crabd.CrabdLog(Path(self._tmp.name) / "logs")   # a DIRECTORY, not a file
        Path(self._tmp.name, "logs").mkdir()
        log.write("crabd: this cannot be written")            # must not raise
        log.write("crabd: nor this one")

    def test_log_line_also_reaches_stderr_by_default(self):
        import io as _io
        import contextlib
        original = crabd.CRABD_LOG_FILE
        crabd.CRABD_LOG_FILE = self.path
        self.addCleanup(lambda: setattr(crabd, "CRABD_LOG_FILE", original))
        buf = _io.StringIO()
        with contextlib.redirect_stderr(buf):
            crabd.log_line("crabd: both destinations")
        self.assertIn("crabd: both destinations", buf.getvalue())
        self.assertIn("crabd: both destinations",
                      self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
