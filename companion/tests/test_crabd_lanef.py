"""Lane F acceptance tests: the standalone audit's backend findings and features.

Every class names the finding or feature id it closes. The fixtures here are adapted
from the audit's own reproduction script, so a test that passes here is the acceptance
test the audit asked for and not a restatement of the fix. Nothing in this file reads
the real ~/.claude or binds a production port.
"""

import ctypes
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crabd  # noqa: E402
from test_crabd import (ServedOverASocket, StubHost, StubLimits, TempProjects,  # noqa: E402
                        assistant_line, shm_blob, shm_reading, user_line, write_jsonl)


# ------------------------------------------------------------------------ SCA-010

class FakePdh:
    """The audit's injected PDH: one scalar counter and one wildcard array entry, both
    answering the CStatus under test. Adapted from backend_repros.py."""

    def __init__(self, status):
        self.status = status
        self.keep = None

    def PdhCollectQueryData(self, query):
        return 0

    def PdhGetFormattedCounterValue(self, handle, fmt, typ, value):
        value._obj.CStatus = self.status
        value._obj.doubleValue = 1234.0
        return 0

    def PdhGetFormattedCounterArrayW(self, handle, fmt, size, count, buf):
        n = ctypes.sizeof(crabd._PDH_FMT_COUNTERVALUE_ITEM_W)
        size._obj.value = n
        count._obj.value = 1
        if buf is None:
            return crabd.PDH_MORE_DATA
        self.keep = (crabd._PDH_FMT_COUNTERVALUE_ITEM_W * 1)()
        self.keep[0].szName = 'Synthetic Ethernet'
        self.keep[0].FmtValue.CStatus = self.status
        self.keep[0].FmtValue.doubleValue = 5678.0
        ctypes.memmove(buf, self.keep, n)
        return 0


class PdhSuccessStatusTests(unittest.TestCase):
    """SCA-010: PDH_CSTATUS_NEW_DATA (1) is a successful reading, not a failure."""

    def rates(self, status):
        rates = crabd.PdhRates()
        rates._opened = True
        rates._query = ctypes.c_void_p(1)
        rates._pdh = FakePdh(status)
        rates._counters = [('diskReadBps', ctypes.c_void_p(2))]
        rates._net = [('netRxBps', ctypes.c_void_p(3))]
        return rates

    def test_valid_data_and_new_data_return_the_same_reading(self):
        for status in (crabd.PDH_CSTATUS_VALID_DATA, crabd.PDH_CSTATUS_NEW_DATA):
            with self.subTest(status=status):
                sample = self.rates(status).sample()
                self.assertEqual(sample['diskReadBps'], 1234.0)
                self.assertEqual(sample['netRxBps'], 5678.0)

    def test_an_unlisted_status_is_still_null_on_both_paths(self):
        """PDH_CSTATUS_INVALID_DATA on the first collect must stay a null rather than
        become a measured zero - the reason there is no baseline collect in _open."""
        sample = self.rates(0xC0000BB8).sample()
        self.assertIsNone(sample['diskReadBps'])
        self.assertIsNone(sample['netRxBps'])


if __name__ == '__main__':
    unittest.main()


# ------------------------------------------------------------------------ SCA-033

class ChunkedFramingTests(unittest.TestCase):
    """SCA-033: a lost chunk boundary closes the connection instead of letting
    BaseHTTPRequestHandler read the remaining bytes as a second request."""

    def handler(self, body: bytes):
        handler = crabd.Handler.__new__(crabd.Handler)
        handler.headers = {"Transfer-Encoding": "chunked"}
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        return handler

    def test_a_valid_chunked_body_still_keeps_the_connection(self):
        handler = self.handler(b"5\r\nhello\r\n5\r\nworld\r\n0\r\n\r\n")
        self.assertEqual(handler._read_body(), b"helloworld")
        self.assertFalse(handler.close_connection)

    def test_a_malformed_size_line_closes_and_leaves_the_tail_unparsed(self):
        trailing = b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:1\r\n\r\n"
        handler = self.handler(b"ZZZZ\r\n" + trailing)
        handler._read_body()
        self.assertTrue(handler.close_connection)

    def test_a_short_chunk_closes(self):
        handler = self.handler(b"20\r\nonly-ten!!\r\n")
        handler._read_body()
        self.assertTrue(handler.close_connection)

    def test_a_delimiter_that_is_not_crlf_closes(self):
        handler = self.handler(b"5\r\nhelloXX5\r\nworld\r\n0\r\n\r\n")
        handler._read_body()
        self.assertTrue(handler.close_connection)

    def test_an_unconsumed_trailer_section_closes(self):
        handler = self.handler(b"5\r\nhello\r\n0\r\nX-Trailer: 1\r\n\r\n")
        self.assertEqual(handler._read_body(), b"hello")
        self.assertTrue(handler.close_connection)

    def test_a_size_line_longer_than_the_read_bound_closes(self):
        handler = self.handler(b"0" * 70 + b"5\r\nhello\r\n")
        handler._read_body()
        self.assertTrue(handler.close_connection)


class ChunkedFramingOverARealSocketTests(ServedOverASocket):
    """SCA-033's acceptance test, on the raw socket the audit used. The second request
    is a health GET: harmless if it were dispatched, and unmistakable in the reply."""

    def raw_exchange(self, payload: bytes) -> bytes:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(payload)
        sock.settimeout(5)
        seen = b""
        while True:
            try:
                block = sock.recv(4096)
            except TimeoutError:
                self.fail("the server neither answered nor closed within 5 s")
            if not block:
                break
            seen += block
        return seen

    def test_a_malformed_chunk_size_does_not_dispatch_the_trailing_request(self):
        second = (b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                  % self.port)
        seen = self.raw_exchange(
            b"POST /v1/not-a-route HTTP/1.1\r\n"
            b"Host: 127.0.0.1:%d\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"ZZZZ\r\n" % self.port + second)
        # One response, and the socket reached EOF: recv returning b"" is the close.
        self.assertEqual(seen.count(b"HTTP/1.1 "), 1, seen[:400])
        self.assertIn(b"404", seen.split(b"\r\n", 1)[0])
        self.assertNotIn(b'"version"', seen)

    def test_a_valid_chunked_post_still_frames_a_second_request(self):
        """The control: valid chunked keep-alive is untouched, so the close above is
        the framing error and not a blanket hang-up on chunked bodies."""
        body = json.dumps({"sessionId": "nope", "action": "ack"}).encode()
        first = (b"POST /v1/action HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                 b"Content-Type: application/json\r\n"
                 b"Transfer-Encoding: chunked\r\n\r\n"
                 b"%x\r\n%s\r\n0\r\n\r\n" % (self.port, len(body), body))
        second = (b"GET /v1/health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                  b"Connection: close\r\n\r\n" % self.port)
        seen = self.raw_exchange(first + second)
        self.assertEqual(seen.count(b"HTTP/1.1 "), 2, seen[:400])
        self.assertIn(b'"version"', seen)


# ------------------------------------------------------------------------ SCA-011

class EmptyProjectPromptPrecedenceTests(unittest.TestCase):
    """SCA-011: a valid empty list is an instruction, not an absent key."""

    NOW = 1_800_000_000.0

    def config(self, data):
        tmp = tempfile.TemporaryDirectory(prefix='sidecrab-lanef-')
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / 'config.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return crabd.UserConfig(path)

    def test_an_empty_longest_prefix_suppresses_the_parent_path_extras(self):
        cfg = self.config({'continuePromptsByPath': {
            r'C:\Work': ['Parent prompt'], r'C:\Work\restricted': []}})
        self.assertEqual(cfg.continue_session_extras(self.NOW, None, r'C:\Work\src'),
                         ['Parent prompt'])
        self.assertEqual(
            cfg.continue_session_extras(self.NOW, None, r'C:\Work\restricted\src'), [])

    def test_the_first_empty_repo_key_wins_over_a_later_case_collision(self):
        cfg = self.config({'continuePromptsByRepo': {
            'Example': [], 'EXAMPLE': ['Unexpected fallback']}})
        with patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(cfg.continue_session_extras(self.NOW, 'example', None), [])

    def test_a_malformed_first_repo_key_still_wins_its_name(self):
        """The case only the separate seen-set covers: a first key whose value is
        malformed is stored nowhere, and keying the collision test off the stored map
        would hand the name to the later spelling."""
        cfg = self.config({'continuePromptsByRepo': {
            'Example': 'not a list', 'EXAMPLE': ['Unexpected fallback']}})
        with patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(cfg.continue_session_extras(self.NOW, 'example', None), [])

    def test_the_collision_still_reports_once(self):
        cfg = self.config({'continuePromptsByRepo': {
            'Example': [], 'EXAMPLE': ['Unexpected fallback']}})
        key = 'cfgrepo:dup:example'
        crabd._LOG_ONCE_CONFIG_SEEN.discard(key)
        self.addCleanup(crabd._LOG_ONCE_CONFIG_SEEN.discard, key)
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            cfg.continue_session_extras(self.NOW, 'example', None)
        self.assertIn('differ only in case', err.getvalue())

    def test_a_malformed_path_value_does_not_suppress_the_parent(self):
        """None from _project_list means malformed, and a malformed value carries no
        instruction - the parent's extras still apply."""
        cfg = self.config({'continuePromptsByPath': {
            r'C:\Work': ['Parent prompt'], r'C:\Work\restricted': 'not a list'}})
        with patch('sys.stderr', new_callable=io.StringIO):
            picked = cfg.continue_session_extras(self.NOW, None, r'C:\Work\restricted\src')
        self.assertEqual(picked, ['Parent prompt'])

    def test_an_empty_path_entry_leaves_the_repo_layer_alone(self):
        cfg = self.config({'continuePromptsByRepo': {'sidecrab': ['Repo prompt']},
                           'continuePromptsByPath': {r'C:\Work': []}})
        self.assertEqual(
            cfg.continue_session_extras(self.NOW, 'sidecrab', r'C:\Work\src'),
            ['Repo prompt'])

    def test_builtins_and_global_extras_survive_an_empty_entry(self):
        cfg = self.config({'continuePrompts': ['Global prompt'],
                           'continuePromptsByPath': {r'C:\Work': []}})
        allowed = cfg.continue_prompts_for(self.NOW, None, r'C:\Work\src')
        self.assertIn('Global prompt', allowed)
        for builtin in crabd.CONTINUE_PROMPTS_BUILTIN:
            self.assertIn(builtin, allowed)


# ------------------------------------------------------------------------ SCA-032

class OnceLogNamespaceTests(unittest.TestCase):
    """SCA-032: config-derived warnings have their own budget, so they cannot consume
    the one stderr line a fixed failure class gets."""

    def setUp(self):
        # Cleared here, not only restored afterwards: these are process-wide sets and
        # the rest of the suite has already spent keys in them, so a test that asserts
        # "this class still reports" has to start from a known empty state.
        for store in (crabd._LOG_ONCE_SEEN, crabd._LOG_ONCE_CONFIG_SEEN,
                      crabd._LOG_ONCE_SUPPRESSED):
            self.addCleanup(store.update, set(store))
            self.addCleanup(store.clear)
            store.clear()

    def test_a_flooded_config_namespace_leaves_the_fixed_classes_reporting(self):
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            for i in range(crabd.LOG_ONCE_MAX_CONFIG_KEYS + 20):
                crabd._log_once(f'cfgrepo:dup:repo-{i}', f'bad repo {i}', config=True)
            crabd._log_once(crabd.TRANSCRIPT_FILE_LOG_KEY, 'a transcript failed')
            crabd._log_once(crabd.LOAD_LOG_KEY, 'a sampler failed')
        written = err.getvalue()
        self.assertIn('a transcript failed', written)
        self.assertIn('a sampler failed', written)

    def test_a_full_namespace_says_so_once_and_then_stays_quiet(self):
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            for i in range(crabd.LOG_ONCE_MAX_CONFIG_KEYS + 20):
                crabd._log_once(f'cfgrepo:dup:repo-{i}', f'bad repo {i}', config=True)
        written = err.getvalue()
        self.assertEqual(written.count('are suppressed for the life of this process'), 1)
        self.assertEqual(len(crabd._LOG_ONCE_CONFIG_SEEN),
                         crabd.LOG_ONCE_MAX_CONFIG_KEYS)

    def test_the_two_namespaces_do_not_share_a_budget(self):
        with patch('sys.stderr', new_callable=io.StringIO):
            for i in range(crabd.LOG_ONCE_MAX_CONFIG_KEYS + 20):
                crabd._log_once(f'cfgrepo:dup:repo-{i}', 'x', config=True)
        self.assertEqual(len(crabd._LOG_ONCE_SEEN), 0)

    def test_a_real_config_flood_does_not_silence_a_real_transcript_failure(self):
        """The audit's own shape: 64 bad project keys parsed by the shipped parser,
        then one injected unreadable-record exception."""
        tmp = tempfile.TemporaryDirectory(prefix='sidecrab-lanef-')
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        path = root / 'config.json'
        path.write_text(json.dumps({
            'continuePromptsByRepo': {f'repo-{i}': 'not a list' for i in range(50)},
            'continuePromptsByPath': {rf'C:\P{i}': 17 for i in range(14)}}),
            encoding='utf-8')
        with patch('sys.stderr', new_callable=io.StringIO):
            crabd.UserConfig(path).continue_session_extras(1_800_000_000.0, None, None)
        store = crabd.TranscriptStore(root / 'projects')
        (root / 'projects' / 'p').mkdir(parents=True)
        victim = root / 'projects' / 'p' / ('a' * 8 + '-0000-0000-0000-000000000000.jsonl')
        victim.write_text('{}\n', encoding='utf-8')
        with patch.object(crabd.FileFacts, 'refresh', side_effect=RuntimeError('poisoned')):
            with patch('sys.stderr', new_callable=io.StringIO) as err:
                store.scan(time.time())
        self.assertIn('could not read transcript', err.getvalue())


# ------------------------------------------------------------------------ SCA-005

class PairingFileRecoveryTests(unittest.TestCase):
    """SCA-005: an unreadable pairing file must not take the companion down with it."""

    def tmpdir(self) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix='sidecrab-lanef-')
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name)

    def test_an_invalid_utf8_pairing_file_is_quarantined_and_replaced(self):
        root = self.tmpdir()
        path = root / 'panel-token'
        path.write_bytes(b'\xff\xfe\x00')
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            gate = crabd.PanelToken.load_or_create(path)
        kept = path.with_name('panel-token' + crabd.PANEL_TOKEN_UNUSABLE_SUFFIX)
        self.assertEqual(kept.read_bytes(), b'\xff\xfe\x00')
        self.assertIn('pair the panel again', err.getvalue())
        self.assertTrue(gate.status(0.0)['present'])
        self.assertEqual(gate.verify(path.read_text(encoding='utf-8'), 0.0), 'ok')

    def test_a_pairing_file_that_cannot_be_written_leaves_approvals_fail_closed(self):
        root = self.tmpdir()
        (root / 'not-a-dir').write_text('x', encoding='utf-8')
        path = root / 'not-a-dir' / 'panel-token'
        with patch('sys.stderr', new_callable=io.StringIO) as err:
            gate = crabd.PanelToken.load_or_create(path)
        self.assertIn('panel approvals are unavailable', err.getvalue())
        self.assertFalse(gate.status(0.0)['present'])
        for presented in (None, '', 'K7QXM2PDAB', crabd.PanelToken.generate()):
            self.assertNotEqual(gate.verify(presented, 0.0), 'ok', presented)

    def test_a_missing_file_quarantines_nothing(self):
        root = self.tmpdir()
        path = root / 'nested' / 'panel-token'
        crabd.PanelToken.load_or_create(path)
        self.assertFalse(
            path.with_name('panel-token' + crabd.PANEL_TOKEN_UNUSABLE_SUFFIX).exists())


class PairingFileStartupTests(ServedOverASocket):
    """The other half of SCA-005's acceptance: with pairing unusable, the feed, the
    health endpoint and the page are still served and decide is still refused."""

    def setUp(self):
        super().setUp()
        root = Path(tempfile.mkdtemp(prefix='sidecrab-lanef-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(root, ignore_errors=True))
        (root / 'not-a-dir').write_text('x', encoding='utf-8')
        with patch('sys.stderr', new_callable=io.StringIO):
            self.builder.panel_token = crabd.PanelToken.load_or_create(
                root / 'not-a-dir' / 'panel-token')
        self.builder.permissions = crabd.PermissionBroker()

    def test_state_and_health_are_served_and_decide_is_refused(self):
        self.assertEqual(self.client.get('/v1/state').status, 200)
        health = self.client.get('/v1/health').json()
        self.assertFalse(health['panelToken']['present'])
        status, body = self.action({'sessionId': self.SID, 'action': 'decide',
                                    'decision': 'allow', 'token': 'K7QXM2PDAB',
                                    'requestId': 'anything'})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {'error': 'pairing code rejected'})


# ------------------------------------------------------------------------ SCA-009

class HwinfoStalledSamplerTests(TempProjects):
    """SCA-009: a stalled sampler must show a growing age, not a frozen one."""

    NOW = 1_800_000_000.0

    def reader_and_builder(self):
        blob = shm_blob(['Synthetic CPU'], [shm_reading('CPU Package', 60, sensor=0)],
                        poll_time=int(self.NOW))
        hwinfo = crabd.HwinfoReader(opener=lambda: (blob, len(blob)))
        hwinfo.poll(self.NOW)
        builder = crabd.StateBuilder(crabd.TranscriptStore(self.projects),
                                     crabd.HookTracker(), StubLimits(), self.NOW,
                                     crabd.UserConfig(self.config_path),
                                     host=StubHost(), hwinfo=hwinfo)
        return hwinfo, builder

    def test_the_age_grows_and_goes_stale_while_the_sampler_is_frozen(self):
        hwinfo, builder = self.reader_and_builder()
        first = builder.build(now=self.NOW)['host']['sensorsSource']
        self.assertEqual(first['ageSec'], 0.0)
        self.assertFalse(first['stale'])
        # The sampler publishes nothing further; only the builder advances.
        at_31 = builder.build(now=self.NOW + 31)['host']['sensorsSource']
        self.assertEqual(at_31['ageSec'], 31.0)
        self.assertTrue(at_31['stale'])
        self.assertEqual(at_31['note'], crabd.HWINFO_NOTE_STALE)
        at_90 = builder.build(now=self.NOW + 90)
        self.assertEqual(at_90['host']['sensorsSource']['ageSec'], 90.0)
        self.assertEqual(at_90['generatedAt'], crabd._utc_iso(self.NOW + 90))
        # The unrelated feed carried on: the readings themselves are still served.
        self.assertTrue(at_90['host']['sensors'])

    def test_the_reading_recovers_when_the_sampler_resumes(self):
        hwinfo, builder = self.reader_and_builder()
        self.assertTrue(builder.build(now=self.NOW + 90)['host']['sensorsSource']['stale'])
        fresh = shm_blob(['Synthetic CPU'], [shm_reading('CPU Package', 61, sensor=0)],
                         poll_time=int(self.NOW + 90))
        hwinfo._opener = lambda: (fresh, len(fresh))
        hwinfo.poll(self.NOW + 90)
        source = builder.build(now=self.NOW + 91)['host']['sensorsSource']
        self.assertEqual(source['ageSec'], 1.0)
        self.assertFalse(source['stale'])
        self.assertIsNone(source['note'])

    def test_the_heartbeat_is_the_samplers_own_liveness(self):
        """pollTime is the READING's clock and the heartbeat is the sampler's; a stalled
        sampler and a section that stopped being written freeze the first alike."""
        hwinfo, _ = self.reader_and_builder()
        self.assertEqual(hwinfo.heartbeat(), self.NOW)
        hwinfo._opener = lambda: None
        hwinfo.poll(self.NOW + 600)
        self.assertEqual(hwinfo.heartbeat(), self.NOW + 600)
        self.assertIsNone(hwinfo.get(self.NOW + 600)[1]['ageSec'])

    def test_no_mapping_ages_nothing(self):
        """The honesty rule: an unavailable source has no reading to age, so ageSec
        stays null rather than becoming a number counted from nothing."""
        hwinfo = crabd.HwinfoReader(opener=lambda: None)
        hwinfo.poll(self.NOW)
        _, source = hwinfo.get(self.NOW + 5000)
        self.assertIsNone(source['ageSec'])
        self.assertFalse(source['stale'])
        self.assertFalse(source['available'])


# ------------------------------------------------------------------------ SCA-001

class MovedSessionMetadataTests(TempProjects):
    """SCA-001 (P1): a session whose cwd moved must be served as the project it is in
    now, in EITHER enumeration order, and must accept only that project's prompts."""

    NOW = 1_800_000_000.0
    SID = '11111111-2222-3333-4444-555555555555'
    NEW = r'C:\Work\new-project'
    OLD = r'C:\Work\old-project'

    class FixedGit:
        def get(self, cwd):
            return (cwd.split('\\')[-1], 'main') if cwd else (None, None)

    def facts_pair(self):
        """Two main transcripts for one session id: the live one in new-project and an
        older one left behind in old-project."""
        root = Path(tempfile.mkdtemp(prefix='sidecrab-lanef-'))
        self.addCleanup(lambda: __import__('shutil').rmtree(root, ignore_errors=True))
        newer = root / 'newer' / (self.SID + '.jsonl')
        older = root / 'older' / (self.SID + '.jsonl')
        write_jsonl(newer, [user_line('New project work', self.NOW - 10, cwd=self.NEW),
                            assistant_line('new-request', self.NOW - 5, cwd=self.NEW,
                                           output=7)], mtime=self.NOW - 5)
        write_jsonl(older, [user_line('Old project work', self.NOW - 100, cwd=self.OLD),
                            assistant_line('old-request', self.NOW - 90, cwd=self.OLD,
                                           output=11)], mtime=self.NOW - 90)
        pair = [crabd.FileFacts(newer, self.SID, False),
                crabd.FileFacts(older, self.SID, False)]
        for item in pair:
            item.refresh()
        return pair

    def builder_for(self, order):
        facts = self.facts_pair()

        class FixedStore:
            def scan(self, now):
                pass

            def snapshot(self):
                return list(order(facts))

        config_path = Path(tempfile.mkdtemp(prefix='sidecrab-lanef-')) / 'config.json'
        config_path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: __import__('shutil').rmtree(config_path.parent,
                                                            ignore_errors=True))
        config_path.write_text(json.dumps({'continuePromptsByRepo': {
            'new-project': ['New project task'],
            'old-project': ['Old project task']}}), encoding='utf-8')
        hooks = crabd.HookTracker()
        with patch.object(crabd.time, 'time', return_value=self.NOW):
            hooks.record({'session_id': self.SID, 'hook_event_name': 'UserPromptSubmit',
                          'cwd': self.NEW})
            builder = crabd.StateBuilder(FixedStore(), hooks, StubLimits(), self.NOW,
                                         crabd.UserConfig(config_path),
                                         host=StubHost(),
                                         continues=crabd.ContinueQueue())
            builder.git = self.FixedGit()
            with builder._lock:
                builder._state = builder.build(now=self.NOW)
        return builder

    def queue(self, builder, prompt):
        answers = []
        handler = object.__new__(crabd.Handler)
        handler.builder = builder
        handler._send = lambda status, body: answers.append(status)
        with patch.object(crabd.time, 'time', return_value=self.NOW):
            handler._do_queue_continue(self.SID, prompt)
        return answers[0]

    def test_both_enumeration_orders_serve_the_project_the_session_is_in(self):
        for name, order in (('newest first', lambda f: f),
                            ('newest last', lambda f: list(reversed(f)))):
            with self.subTest(order=name):
                builder = self.builder_for(order)
                row = builder._state['sessions'][0]
                self.assertEqual(row['cwd'], self.NEW)
                self.assertEqual(row['repo'], 'new-project')
                self.assertEqual(row['title'], 'New project work')
                self.assertEqual(row['continuePrompts'], ['New project task'])

    def test_both_orders_accept_only_the_current_projects_prompt(self):
        for name, order in (('newest first', lambda f: f),
                            ('newest last', lambda f: list(reversed(f)))):
            with self.subTest(order=name):
                builder = self.builder_for(order)
                self.assertEqual(self.queue(builder, 'Old project task'), 400)
                self.assertEqual(self.queue(builder, 'New project task'), 204)

    def test_usage_from_both_files_is_still_counted(self):
        for name, order in (('newest first', lambda f: f),
                            ('newest last', lambda f: list(reversed(f)))):
            with self.subTest(order=name):
                builder = self.builder_for(order)
                row = builder._state['sessions'][0]
                self.assertEqual(row['todayOutputTokens'], 18)

    def test_a_later_hook_cwd_wins_over_an_older_transcript(self):
        """The deliberate join: the hook is the newest evidence of where the session
        is, and the allowlist follows it."""
        facts = self.facts_pair()

        class OlderOnly:
            def scan(self, now):
                pass

            def snapshot(self):
                return [facts[1]]

        hooks = crabd.HookTracker()
        with patch.object(crabd.time, 'time', return_value=self.NOW):
            hooks.record({'session_id': self.SID, 'hook_event_name': 'UserPromptSubmit',
                          'cwd': self.NEW})
            builder = crabd.StateBuilder(OlderOnly(), hooks, StubLimits(), self.NOW,
                                         crabd.UserConfig(self.config_path),
                                         host=StubHost())
            builder.git = self.FixedGit()
            row = builder.build(now=self.NOW)['sessions'][0]
        self.assertEqual(row['cwd'], self.NEW)

    def test_an_older_hook_does_not_move_a_newer_transcripts_cwd(self):
        """The join is one-directional: a hook recorded BEFORE the newest transcript
        record has nothing newer to say."""
        facts = self.facts_pair()

        class NewerOnly:
            def scan(self, now):
                pass

            def snapshot(self):
                return [facts[0]]

        hooks = crabd.HookTracker()
        with patch.object(crabd.time, 'time', return_value=self.NOW - 300):
            hooks.record({'session_id': self.SID, 'hook_event_name': 'UserPromptSubmit',
                          'cwd': self.OLD})
        builder = crabd.StateBuilder(NewerOnly(), hooks, StubLimits(), self.NOW,
                                     crabd.UserConfig(self.config_path),
                                     host=StubHost())
        builder.git = self.FixedGit()
        row = builder.build(now=self.NOW)['sessions'][0]
        self.assertEqual(row['cwd'], self.NEW)


# ------------------------------------------------------------------- MF-002 / C5

class CancelQueuedContinueTests(ServedOverASocket):
    """MF-002: withdrawing a queued continuation, and the race with the Stop hook."""

    def setUp(self):
        super().setUp()
        self.queue = crabd.ContinueQueue()
        self.builder.continues = self.queue
        self.hooks.record({'session_id': self.SID, 'hook_event_name': 'UserPromptSubmit',
                           'cwd': str(self.projects)})

    def cancel(self):
        return self.action({'sessionId': self.SID, 'action': 'cancel-continue'})

    def queued_row(self):
        with self.builder._lock:
            self.builder._state = self.builder.build()
        row = next(r for r in self.state()['sessions'] if r['id'] == self.SID)
        return row['queuedContinue']

    def test_a_queued_prompt_is_cancelled_and_leaves_the_card(self):
        status, _ = self.action({'sessionId': self.SID, 'action': 'queue-continue',
                                 'prompt': crabd.CONTINUE_PROMPTS_BUILTIN[0]})
        self.assertEqual(status, 204)
        self.assertIsNotNone(self.queued_row())
        self.assertEqual(self.cancel()[0], 204)
        self.assertIsNone(self.queued_row())
        self.assertIsNone(self.queue.peek(self.SID, time.time()))

    def test_the_cancellation_is_a_history_line(self):
        prompt = crabd.CONTINUE_PROMPTS_BUILTIN[0]
        self.action({'sessionId': self.SID, 'action': 'queue-continue',
                     'prompt': prompt})
        self.cancel()
        events = self.hooks.snapshot()[self.SID]['events']
        self.assertIn('continue cancelled: ' + prompt, [e['text'] for e in events])

    def test_nothing_queued_is_404(self):
        status, body = self.cancel()
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {'error': 'nothing queued'})

    def test_an_expired_item_is_nothing_queued_not_a_cancellation(self):
        """The card stopped showing it at the ten-minute mark, so it is not what the
        operator is cancelling."""
        self.queue.queue(self.SID, 'Run the tests',
                         time.time() - crabd.CONTINUE_TTL_SEC - 5)
        self.assertEqual(self.cancel()[0], 404)

    def test_a_delivered_prompt_is_409_with_the_time_it_went(self):
        prompt = crabd.CONTINUE_PROMPTS_BUILTIN[0]
        at = time.time()
        self.queue.queue(self.SID, prompt, at)
        self.queue.claim(self.SID, at)
        self.queue.drain_if(self.SID, prompt, at)
        status, body = self.cancel()
        self.assertEqual(status, 409)
        answer = json.loads(body)
        self.assertEqual(answer['error'], 'already delivered')
        self.assertEqual(answer['deliveredAt'], crabd._utc_iso(at))

    def test_a_claimed_prompt_cannot_be_cancelled_out_from_under_the_stop_hook(self):
        """Claimed means the answer is being written; there is no instant at which
        crabd could take it back."""
        prompt = crabd.CONTINUE_PROMPTS_BUILTIN[0]
        self.queue.queue(self.SID, prompt, time.time())
        self.queue.claim(self.SID, time.time())
        self.assertEqual(self.cancel()[0], 409)

    def test_a_send_that_failed_releases_the_claim_so_cancel_still_works(self):
        """CRB-F5 keeps an undelivered prompt for the next Stop, so it is still the
        operator's to withdraw."""
        prompt = crabd.CONTINUE_PROMPTS_BUILTIN[0]
        self.queue.queue(self.SID, prompt, time.time())
        self.queue.claim(self.SID, time.time())
        self.queue.release(self.SID, prompt)
        self.assertEqual(self.cancel()[0], 204)

    def test_a_new_tap_clears_the_previous_delivery_record(self):
        first, second = crabd.CONTINUE_PROMPTS_BUILTIN[:2]
        at = time.time()
        self.queue.queue(self.SID, first, at)
        self.queue.claim(self.SID, at)
        self.queue.drain_if(self.SID, first, at)
        self.queue.queue(self.SID, second, at)
        self.assertEqual(self.cancel()[0], 204)

    def test_cancel_is_refused_when_tap_to_continue_is_disabled(self):
        self.config_path.write_text(json.dumps({'allowContinue': False}),
                                    encoding='utf-8')
        self.builder.config = crabd.UserConfig(self.config_path)
        status, body = self.cancel()
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {'error': 'tap-to-continue is disabled'})

    def test_exactly_one_of_the_stop_hook_and_the_cancel_wins(self):
        """The audit's race, run for real: a Stop hook and a cancel fired together,
        many times over. One of them acts and the other reports that it lost - never
        both, and never neither."""
        prompt = crabd.CONTINUE_PROMPTS_BUILTIN[0]
        outcomes = []
        for _ in range(40):
            self.queue.queue(self.SID, prompt, time.time())
            results = {}
            start = threading.Barrier(2)

            def stop_hook():
                start.wait()
                now = time.time()
                claimed = self.queue.claim(self.SID, now)
                if claimed is not None:
                    self.queue.drain_if(self.SID, claimed, now)
                results['delivered'] = claimed is not None

            def canceller():
                start.wait()
                results['cancel'] = self.cancel()[0]

            threads = [threading.Thread(target=stop_hook),
                       threading.Thread(target=canceller)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
            delivered, status = results['delivered'], results['cancel']
            # Exactly one winner: delivered means the cancel must report 409, and a
            # successful cancel (204) means the Stop hook must have found nothing.
            self.assertEqual(delivered, status == 409, results)
            self.assertIn(status, (204, 409))
            outcomes.append(status)
            self.queue.cancel(self.SID, time.time())
            self.queue._delivered.pop(self.SID, None)
        self.assertTrue(outcomes)


# ------------------------------------------------------------------ MF-017 / C4

class ApprovalReadinessTests(ServedOverASocket):
    """C4: the four readiness answers, and the verify route that can only ever say
    whether a code the caller already holds is the right one."""

    CODE = 'K7QXM2PDAB'

    def setUp(self):
        super().setUp()
        self.builder.permissions = crabd.PermissionBroker()
        self.builder.panel_token = crabd.PanelToken(None, self.CODE)
        self.enable_approvals(True)

    def enable_approvals(self, on: bool):
        self.config_path.write_text(
            json.dumps({'panelApprovals': {'enabled': on}}), encoding='utf-8')
        self.builder.config = crabd.UserConfig(self.config_path)

    def approvals(self):
        with self.builder._lock:
            self.builder._state = self.builder.build()
        return self.state()['approvals']

    def verify(self, code):
        reply = self.client.post('/v1/approvals/verify',
                                 json.dumps({'code': code}).encode())
        return reply.status, reply.body

    def test_off_when_approvals_are_disabled(self):
        self.enable_approvals(False)
        block = self.approvals()
        self.assertFalse(block['enabled'])
        self.assertEqual(block['readiness'], 'off')
        self.assertIsNone(block['verifiedAt'])

    def test_no_token_when_crabd_holds_no_pairing_code(self):
        self.builder.panel_token = crabd.PanelToken(None, None)
        self.assertEqual(self.approvals()['readiness'], 'no-token')

    def test_unverified_until_a_code_matches_then_ready(self):
        self.assertEqual(self.approvals()['readiness'], 'unverified')
        self.assertIsNone(self.approvals()['verifiedAt'])
        self.assertEqual(self.verify(self.CODE)[0], 204)
        block = self.approvals()
        self.assertEqual(block['readiness'], 'ready')
        self.assertIsNotNone(block['verifiedAt'])

    def test_a_wrong_code_is_403_and_leaves_it_unverified(self):
        status, body = self.verify('AAAAAAAAAA')
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {'error': 'pairing code rejected'})
        self.assertEqual(self.approvals()['readiness'], 'unverified')

    def test_the_sixth_attempt_in_a_minute_is_429(self):
        for _ in range(crabd.PANEL_TOKEN_PROBE_MAX):
            self.assertEqual(self.verify('AAAAAAAAAA')[0], 403)
        status, body = self.verify(self.CODE)
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body), {'error': 'too many attempts - wait a minute'})

    def test_the_probe_budget_is_not_the_decide_lockout(self):
        """A panel checking its pairing must not be able to lock the operator out of
        Approve and Deny, and vice versa."""
        gate = self.builder.panel_token
        for _ in range(crabd.PANEL_TOKEN_PROBE_MAX):
            gate.verify_code('AAAAAAAAAA', 1000.0)
        self.assertEqual(gate.verify_code(self.CODE, 1000.0), 'rate-limited')
        self.assertEqual(gate.verify('K7QXM-2PDAB', 1000.0), 'ok')
        self.assertIsNone(gate.status(1000.0)['lockedUntil'])

    def test_the_route_never_reveals_the_code(self):
        for code in ('AAAAAAAAAA', self.CODE):
            _, body = self.verify(code)
            self.assertNotIn(self.CODE.encode(), body or b'')

    def test_the_route_cannot_decide_anything(self):
        """Structural: verifying arms nothing. A pending permission is still pending
        after a successful verify, and only a decide can answer it."""
        entry = self.builder.permissions.register(self.SID, 'Bash', None, time.time())
        self.assertEqual(self.verify(self.CODE)[0], 204)
        self.assertIsNone(entry['decision'])
        self.assertIsNotNone(self.builder.permissions.pending(self.SID))

    def test_a_malformed_body_is_400_before_any_code_is_consulted(self):
        for payload in (b'not json', b'"a string"', b'{}', b'{"code": 7}',
                        b'{"code": "   "}'):
            reply = self.client.post('/v1/approvals/verify', payload)
            self.assertEqual(reply.status, 400, payload)
        # The shape gate spent none of the probe budget.
        self.assertEqual(self.verify(self.CODE)[0], 204)

    def test_verify_works_while_approvals_are_off(self):
        """Pairing is checked BEFORE arming approvals, which is exactly when the answer
        is worth having."""
        self.enable_approvals(False)
        self.assertEqual(self.verify(self.CODE)[0], 204)
        self.assertEqual(self.approvals()['readiness'], 'off')

    def test_a_cross_site_page_is_refused_the_same_as_action(self):
        reply = self.client.post('/v1/approvals/verify',
                                 json.dumps({'code': self.CODE}).encode(),
                                 headers={'Origin': 'https://evil.example'})
        self.assertEqual(reply.status, 403)
        self.assertEqual(json.loads(reply.body), json.loads(crabd.CROSS_SITE_REFUSED))
        self.assertEqual(self.approvals()['readiness'], 'unverified')

    def test_a_bad_host_header_is_refused(self):
        reply = self.client.post('/v1/approvals/verify',
                                 json.dumps({'code': self.CODE}).encode(),
                                 headers={'Host': 'evil.example'})
        self.assertEqual(reply.status, 421)


# ------------------------------------------------------------------- MF-008 / C3

class SourceHealthTests(TempProjects):
    """C3: one freshness verdict per feed. Each fake source is stopped on its own and
    must degrade only itself, then recover on its next valid sample."""

    NOW = 1_800_000_000.0
    SHAPE = {'ok', 'lastAt', 'ageSec', 'note'}

    class Limits(StubLimits):
        """StubLimits plus the health() a real LimitsReader now carries."""

        def __init__(self, ok=True, backoff=False, note=None):
            super().__init__()
            self.state = {'ok': ok, 'lastAt': 1_800_000_000.0 - 60,
                          'note': note, 'backoff': backoff}

        def health(self, now):
            return dict(self.state)

    def builder(self, **kw):
        limits = kw.pop('limits', self.Limits())
        hooks = kw.pop('hooks', crabd.HookTracker())
        started = kw.pop('started', self.NOW - 7200)
        return crabd.StateBuilder(crabd.TranscriptStore(self.projects), hooks,
                                  limits, started,
                                  crabd.UserConfig(self.config_path),
                                  host=StubHost(), **kw)

    def sources(self, builder, now=None):
        return builder.build(now=now or self.NOW).get('sources', {})

    def working_session(self, tail):
        write_jsonl(self.session_path('aaaaaaaa-0000-0000-0000-00000000000' + tail),
                    [user_line('working', self.NOW - 30)], mtime=self.NOW - 5)

    def test_every_member_has_the_contract_shape(self):
        for name, entry in self.sources(self.builder()).items():
            self.assertEqual(set(entry), self.SHAPE, name)
            self.assertIsInstance(entry['ok'], bool, name)

    def test_a_source_crabd_cannot_judge_is_absent(self):
        sources = self.sources(self.builder(statusline=crabd.StatusLineReader(),
                                            otlp=crabd.OtlpReceiver()))
        self.assertNotIn('statusline', sources)
        self.assertNotIn('otlp', sources)
        self.assertNotIn('hwinfo', sources)
        self.assertNotIn('gpu', sources)

    def test_an_unlistable_projects_directory_degrades_only_transcripts(self):
        builder = self.builder()
        builder.store = crabd.TranscriptStore(self.projects / 'does-not-exist')
        sources = self.sources(builder)
        self.assertFalse(sources['transcripts']['ok'])
        self.assertIn('could not be listed', sources['transcripts']['note'])
        self.assertTrue(sources['hooks']['ok'])
        self.assertTrue(sources['limitsToken']['ok'])

    def test_the_status_line_degrades_on_its_own_and_recovers(self):
        statusline = crabd.StatusLineReader()
        statusline.last_at = self.NOW - crabd.STATUSLINE_PREFER_SEC - 60
        builder = self.builder(statusline=statusline)
        self.working_session('1')
        sources = self.sources(builder)
        self.assertFalse(sources['statusline']['ok'])
        self.assertIn('stopped posting', sources['statusline']['note'])
        self.assertTrue(sources['limitsToken']['ok'])
        statusline.last_at = self.NOW - 5
        recovered = self.sources(builder)['statusline']
        self.assertTrue(recovered['ok'])
        self.assertIsNone(recovered['note'])
        self.assertEqual(recovered['ageSec'], 5.0)

    def test_a_quiet_status_line_on_an_idle_night_is_not_a_failure(self):
        """The healthy-night replay as a test: with nothing running there is nothing
        for an event-driven source to report, and a panel that goes amber every night
        is a panel nobody reads."""
        statusline = crabd.StatusLineReader()
        statusline.last_at = self.NOW - 4000
        self.assertTrue(self.sources(self.builder(statusline=statusline))
                        ['statusline']['ok'])

    def test_a_rate_limited_usage_endpoint_is_not_ok(self):
        sources = self.sources(self.builder(limits=self.Limits(backoff=True)))
        self.assertFalse(sources['limitsToken']['ok'])
        self.assertIn('rate-limited', sources['limitsToken']['note'])

    def test_hwinfo_degrades_when_the_reading_goes_stale(self):
        blob = shm_blob(['Synthetic CPU'], [shm_reading('CPU Package', 60, sensor=0)],
                        poll_time=int(self.NOW))
        hwinfo = crabd.HwinfoReader(opener=lambda: (blob, len(blob)))
        hwinfo.poll(self.NOW)
        builder = self.builder(hwinfo=hwinfo)
        self.assertTrue(self.sources(builder)['hwinfo']['ok'])
        stalled = self.sources(builder, now=self.NOW + 120)['hwinfo']
        self.assertFalse(stalled['ok'])
        self.assertEqual(stalled['ageSec'], 120.0)
        self.assertEqual(stalled['lastAt'], crabd._utc_iso(self.NOW))

    def test_a_stopped_sampler_is_named_even_while_its_reading_is_fresh(self):
        blob = shm_blob(['Synthetic CPU'], [shm_reading('CPU Package', 60, sensor=0)],
                        poll_time=int(self.NOW))
        hwinfo = crabd.HwinfoReader(opener=lambda: (blob, len(blob)))
        hwinfo.poll(self.NOW)
        builder = self.builder(hwinfo=hwinfo)
        with patch.object(hwinfo, 'heartbeat',
                          return_value=self.NOW - crabd.SOURCE_HWINFO_SAMPLER_SEC - 1):
            entry = self.sources(builder)['hwinfo']
        self.assertFalse(entry['ok'])
        self.assertIn('sampler has stopped', entry['note'])

    def test_an_absent_gpu_is_reported_with_its_own_reason(self):
        gpu = crabd.GpuReader(runner=lambda t: (1, '', 'no nvidia-smi'))
        gpu.poll(self.NOW)
        entry = self.sources(self.builder(gpu=gpu))['gpu']
        self.assertFalse(entry['ok'])
        self.assertIsNotNone(entry['note'])

    def test_the_hooks_verdict_needs_a_live_session_and_a_settled_crabd(self):
        self.working_session('2')
        settled = self.sources(self.builder())['hooks']
        self.assertFalse(settled['ok'])
        self.assertIsNone(settled['lastAt'])
        self.assertIn('no hook has arrived', settled['note'])
        fresh = self.sources(self.builder(started=self.NOW - 30))['hooks']
        self.assertTrue(fresh['ok'])

    def test_one_hook_settles_the_hooks_source(self):
        hooks = crabd.HookTracker()
        self.working_session('3')
        with patch.object(crabd.time, 'time', return_value=self.NOW - 3):
            hooks.record({'session_id': 'aaaaaaaa-0000-0000-0000-000000000003',
                          'hook_event_name': 'UserPromptSubmit'})
        entry = self.sources(self.builder(hooks=hooks))['hooks']
        self.assertTrue(entry['ok'])
        self.assertEqual(entry['ageSec'], 3.0)

    def test_the_otlp_exporter_degrades_only_while_a_session_is_running(self):
        otlp = crabd.OtlpReceiver()
        otlp.last_at = self.NOW - crabd.SOURCE_OTLP_FRESH_SEC - 60
        self.assertTrue(self.sources(self.builder(otlp=otlp))['otlp']['ok'])
        self.working_session('4')
        entry = self.sources(self.builder(otlp=otlp))['otlp']
        self.assertFalse(entry['ok'])
        self.assertIn('stopped sending', entry['note'])

    def test_sources_is_additive_and_the_schema_did_not_move(self):
        document = self.builder().build(now=self.NOW)
        self.assertEqual(document['schema'], 5)
        self.assertIn('sources', json.loads(crabd.dump_state(document)))


# ------------------------------------------------------------------- MF-001 / C6

class ConfigOnTheGlassTests(ServedOverASocket):
    """C6: the continue vocabulary becomes editable over /v1/config, with the same
    validation the file parser applies and an answer that says what was written."""

    def post(self, payload):
        reply = self.client.post('/v1/config', json.dumps(payload).encode())
        body = json.loads(reply.body) if reply.body else None
        return reply.status, body

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding='utf-8'))

    def test_the_reply_says_what_was_applied(self):
        status, body = self.post({'continuePrompts': ['Run the linter']})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'applied': {'continuePrompts': ['Run the linter']},
                                'warnings': []})
        self.assertEqual(self.read_config()['continuePrompts'], ['Run the linter'])

    def test_an_existing_key_still_round_trips_and_is_normalised_in_the_reply(self):
        status, body = self.post({'quietHours': {'start': '7:5', 'end': '23:9'}})
        self.assertEqual(status, 200)
        self.assertEqual(body['applied']['quietHours'],
                         {'start': '07:05', 'end': '23:09'})

    def test_only_the_keys_in_the_body_are_written(self):
        self.post({'continuePrompts': ['Run the linter'],
                   'budget': {'dailyOutputTokens': 1_000_000}})
        self.post({'continuePrompts': ['Run the tests instead']})
        after = self.read_config()
        self.assertEqual(after['continuePrompts'], ['Run the tests instead'])
        self.assertEqual(after['budget'], {'dailyOutputTokens': 1_000_000})

    def test_null_clears_a_key(self):
        self.post({'continuePromptsByRepo': {'sidecrab': ['Ship it']}})
        status, body = self.post({'continuePromptsByRepo': None})
        self.assertEqual(status, 200)
        self.assertIsNone(body['applied']['continuePromptsByRepo'])
        self.assertIsNone(self.read_config()['continuePromptsByRepo'])

    def test_unrelated_keys_survive_a_write(self):
        self.config_path.write_text(json.dumps({
            'allowReply': True, 'recapRepos': [r'C:\Work'],
            'panelApprovals': {'enabled': True},
            'somethingElse': {'deep': [1, 2]}}), encoding='utf-8')
        self.post({'continuePrompts': ['Run the linter']})
        after = self.read_config()
        self.assertTrue(after['allowReply'])
        self.assertEqual(after['recapRepos'], [r'C:\Work'])
        self.assertEqual(after['panelApprovals'], {'enabled': True})
        self.assertEqual(after['somethingElse'], {'deep': [1, 2]})

    def test_the_security_keys_are_still_refused(self):
        for extra in ({'panelApprovals': {'enabled': True}},
                      {'recapRepos': [r'C:\Windows']},
                      {'allowReply': True},
                      {'allowContinue': False}):
            body = {'continuePrompts': ['Run the linter']}
            body.update(extra)
            status, _ = self.post(body)
            self.assertEqual(status, 400, body)
            self.assertNotIn('continuePrompts', self.read_config())

    def test_a_dropped_entry_is_a_warning_not_a_silent_loss(self):
        status, body = self.post({'continuePrompts': [
            'Keep this', '', 7, 'Keep this', 'x' * (crabd.CONTINUE_PROMPT_MAX + 1)]})
        self.assertEqual(status, 200)
        self.assertEqual(body['applied']['continuePrompts'], ['Keep this'])
        joined = ' | '.join(body['warnings'])
        self.assertIn('not text', joined)
        self.assertIn('blank', joined)
        self.assertIn('duplicate', joined)
        self.assertIn('characters', joined)

    def test_a_builtin_duplicate_is_named(self):
        status, body = self.post(
            {'continuePrompts': [crabd.CONTINUE_PROMPTS_BUILTIN[0]]})
        self.assertEqual(status, 200)
        self.assertEqual(body['applied']['continuePrompts'], [])
        self.assertIn('already a builtin', ' | '.join(body['warnings']))

    def test_a_wrong_shape_for_the_key_itself_is_400_and_writes_nothing(self):
        for payload in ({'continuePrompts': 'not a list'},
                        {'continuePromptsByRepo': ['not an object']},
                        {'continuePromptsByPath': 7}):
            status, body = self.post(payload)
            self.assertEqual(status, 400, payload)
            self.assertIn('error', body)
            self.assertNotIn(list(payload)[0], self.read_config())

    def test_a_relative_path_key_is_dropped_with_a_warning(self):
        status, body = self.post({'continuePromptsByPath': {
            r'relative\path': ['Nope'], r'C:\Work': ['Yes']}})
        self.assertEqual(status, 200)
        self.assertEqual(body['applied']['continuePromptsByPath'],
                         {r'C:\Work': ['Yes']})
        self.assertIn('not an absolute path', ' | '.join(body['warnings']))

    def test_an_empty_list_is_written_because_it_is_configuration(self):
        """SCA-011: an empty list is precedence-bearing, so the sheet must be able to
        write one and get it back."""
        status, body = self.post({'continuePromptsByPath': {r'C:\Work\quiet': []}})
        self.assertEqual(status, 200)
        self.assertEqual(body['applied']['continuePromptsByPath'],
                         {r'C:\Work\quiet': []})
        self.assertEqual(self.read_config()['continuePromptsByPath'],
                         {r'C:\Work\quiet': []})

    def test_a_case_collision_is_reported_and_the_first_key_wins(self):
        status, body = self.post({'continuePromptsByRepo': {
            'Example': ['First'], 'EXAMPLE': ['Second']}})
        self.assertEqual(status, 200)
        self.assertEqual(body['applied']['continuePromptsByRepo'],
                         {'Example': ['First']})
        self.assertIn('only in case', ' | '.join(body['warnings']))

    def test_what_was_written_is_what_the_feed_then_serves(self):
        """The round trip that makes the sheet honest: write, then read the same values
        back off /v1/state and out of the session allowlist."""
        self.post({'continuePrompts': ['Run the linter']})
        with self.builder._lock:
            self.builder._state = self.builder.build()
        self.assertEqual(self.state()['continuePrompts'], ['Run the linter'])
        self.assertIn('Run the linter',
                      self.builder.config.continue_prompts_for(time.time(), None, None))


# --------------------------------------------------------------------- CLEAN-04

class StandaloneOriginPolicyTests(ServedOverASocket):
    """CLEAN-04: the previous-host era `null` and non-web origin allowance is retired. The two
    clients that remain - a native caller with no Origin header, and the panel page on
    this server's own origin - must still pass every gate."""

    def own_origin(self):
        return 'http://127.0.0.1:%d' % self.port

    def post(self, path, body, headers=None):
        return self.client.post(path, body, headers=headers)

    def test_a_native_client_with_no_origin_header_still_passes(self):
        """The notifier, the setup scripts, curl and the CLI's own hooks all send no
        Origin at all. Measured on the live companion: originsSeen holds only
        <absent> pairs."""
        hook = json.dumps({'session_id': self.SID, 'hook_event_name': 'Stop'}).encode()
        self.assertEqual(self.post('/v1/hook', hook).status, 204)
        self.assertEqual(self.post('/v1/action', json.dumps(
            {'sessionId': self.SID, 'action': 'ack'}).encode()).status, 204)
        self.assertEqual(self.post('/v1/config', json.dumps(
            {'quietHours': None}).encode()).status, 200)
        self.assertEqual(self.client.get('/v1/state').status, 200)
        self.assertEqual(self.client.get('/v1/health').status, 200)

    def test_the_same_origin_panel_page_still_passes(self):
        for name in ('127.0.0.1', 'localhost'):
            origin = 'http://%s:%d' % (name, self.port)
            reply = self.post('/v1/action',
                              json.dumps({'sessionId': self.SID,
                                          'action': 'ack'}).encode(),
                              headers={'Origin': origin})
            self.assertEqual(reply.status, 204, origin)
            self.assertEqual(reply.headers.get('Access-Control-Allow-Origin'), origin)
        page = self.client.get('/panel/', headers={'Origin': self.own_origin()})
        self.assertIn(page.status, (200, 404))
        self.assertNotEqual(page.status, 403)

    def test_an_opaque_null_origin_is_now_refused(self):
        """The one origin a sandboxed allow-scripts iframe on any visited page can
        forge. It was allowed only for the previous host's file/qrc page, which is gone."""
        for path, body in (('/v1/action',
                            json.dumps({'sessionId': self.SID,
                                        'action': 'ack'}).encode()),
                           ('/v1/config', json.dumps({'quietHours': None}).encode()),
                           ('/v1/panel-log', json.dumps({'lines': ['tap']}).encode())):
            reply = self.post(path, body, headers={'Origin': 'null'})
            self.assertEqual(reply.status, 403, path)
            self.assertIsNone(reply.headers.get('Access-Control-Allow-Origin'), path)
        read = self.client.get('/v1/state', headers={'Origin': 'null'})
        self.assertEqual(read.status, 403)

    def test_the_non_web_schemes_are_refused(self):
        for origin in ('file://', 'qrc://sidecrab', 'app://widget',
                       'chrome-extension://abcdef'):
            reply = self.post('/v1/action',
                              json.dumps({'sessionId': self.SID,
                                          'action': 'ack'}).encode(),
                              headers={'Origin': origin})
            self.assertEqual(reply.status, 403, origin)

    def test_a_foreign_or_near_miss_origin_is_refused(self):
        near = ('https://evil.example',
                'http://127.0.0.1.evil.example:%d' % self.port,
                'http://127.0.0.1:%d' % (self.port + 1),
                'https://127.0.0.1:%d' % self.port,
                'http://127.0.0.1:%d/panel/' % self.port)
        for origin in near:
            reply = self.post('/v1/action',
                              json.dumps({'sessionId': self.SID,
                                          'action': 'ack'}).encode(),
                              headers={'Origin': origin})
            self.assertEqual(reply.status, 403, origin)

    def test_a_preflight_reflects_only_our_own_origin(self):
        allowed = self.client.request('OPTIONS', '/v1/action',
                                      headers={'Origin': self.own_origin()})
        self.assertEqual(allowed.status, 204)
        self.assertEqual(allowed.headers.get('Access-Control-Allow-Origin'),
                         self.own_origin())
        for origin in ('null', 'file://', 'https://evil.example'):
            refused = self.client.request('OPTIONS', '/v1/action',
                                          headers={'Origin': origin})
            self.assertEqual(refused.status, 204, origin)
            self.assertIsNone(refused.headers.get('Access-Control-Allow-Origin'), origin)

    def test_the_host_allowlist_and_the_pairing_gate_are_unchanged(self):
        """CLEAN-04 retires an origin allowance and nothing else."""
        bad_host = self.post('/v1/action', json.dumps(
            {'sessionId': self.SID, 'action': 'ack'}).encode(),
            headers={'Host': 'evil.example'})
        self.assertEqual(bad_host.status, 421)
        self.builder.permissions = crabd.PermissionBroker()
        self.builder.panel_token = crabd.PanelToken(None, 'K7QXM2PDAB')
        status, body = self.action({'sessionId': self.SID, 'action': 'decide',
                                    'decision': 'allow', 'token': 'WRONGCODE1',
                                    'requestId': 'x'})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {'error': 'pairing code rejected'})

    def test_nothing_in_the_companion_imports_or_recommends_the_retired_sdk(self):
        """C8: no import, detection or recommendation of the Corsair SDK is left."""
        for path in (Path(crabd.__file__),
                     *(Path(crabd.__file__).parents[1] / 'hooks').glob('*.py')):
            text = path.read_text(encoding='utf-8').lower()
            for banned in ('import the vendor SDK', 'from the vendor SDK', 'the vendor SDK.', 'vendoradapter',
                           'install the previous host'):
                self.assertNotIn(banned, text, (path.name, banned))
