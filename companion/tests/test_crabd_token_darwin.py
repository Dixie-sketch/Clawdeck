"""The two secrets macOS keeps in the login Keychain, and the seam crabd reads them
through.

They are two DIFFERENT problems and this file never conflates them:

  - `Claude Code-credentials` is the CLI's OWN credential. On this Mac (measured
    2026-09-04, Claude Code 2.1.260) `~/.claude/.credentials.json` does not exist at all
    and the login Keychain holds the item instead, so a crabd that only knows about the
    file reads "no Claude credentials" forever on an account that is perfectly logged in.
  - `SideCrab limits token` is SIDECRAB'S own store for a long-lived `claude setup-token`
    value - the macOS answer to `~/.sidecrab/limits-token.dpapi`, which is a DPAPI blob
    and therefore a Windows fact.

THE TOKENS ARE SECRETS, and the tests below are the enforcement: neither may appear in
`/v1/state`, `/v1/health`, a log line, an exception message or a process argument list.
`ps` is world-readable on macOS, so a secret in argv is a secret handed to every user on
the machine - which is why the store command travels on `security -i`'s STDIN and only
the reads (which carry no secret at all) use an argv.

Every test here is pure and runs on any OS: `/usr/bin/security` is behind an injected
seam, and the module kill switch `KEYCHAIN_CREDENTIALS_ENABLED` is set False for the
whole module so nothing can reach the operator's real Keychain by accident. The one
exception is the live test at the bottom, which is opt-in and says why.
"""

import ast
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crabd  # noqa: E402
from _httpkeepalive import start_test_server  # noqa: E402


# --------------------------------------------------------------- module isolation

_MODULE_TMP = None


def setUpModule():
    """The same hard isolation every other companion module takes, plus the Keychain.

    The five path globals name REAL files under ~ (the limits cache was poisoned exactly
    this way on 2026-08-26). KEYCHAIN_CREDENTIALS_ENABLED is the same rule one layer out:
    it is the kill switch on the Keychain reads, and with it False a DarwinPlatform built
    without an injected `security` runner cannot reach `/usr/bin/security` at all - so no
    test in this suite can raise a Keychain prompt on the operator's desktop or read a
    secret it has no business seeing. The tests that exercise the Keychain path turn it
    on locally, with the fake.
    """
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    root = Path(_MODULE_TMP.name)
    setUpModule.originals = (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
                             crabd.HISTORY_FILE, crabd.CREDENTIALS_FILE,
                             crabd.LIMITS_TOKEN_FILE,
                             crabd.KEYCHAIN_CREDENTIALS_ENABLED)
    crabd.LIMITS_CACHE_FILE = root / "limits-cache.json"
    crabd.USER_CONFIG_FILE = root / "config.json"
    crabd.HISTORY_FILE = root / "history.jsonl"
    crabd.CREDENTIALS_FILE = root / "no-such-credentials.json"
    crabd.LIMITS_TOKEN_FILE = root / "no-such-limits-token.dpapi"
    crabd.KEYCHAIN_CREDENTIALS_ENABLED = False


def tearDownModule():
    (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE, crabd.HISTORY_FILE,
     crabd.CREDENTIALS_FILE, crabd.LIMITS_TOKEN_FILE,
     crabd.KEYCHAIN_CREDENTIALS_ENABLED) = setUpModule.originals
    _MODULE_TMP.cleanup()


class ModuleIsolationTests(unittest.TestCase):
    def test_every_global_naming_a_real_file_points_into_the_sandbox(self):
        sandbox = Path(_MODULE_TMP.name)
        for name in ("LIMITS_CACHE_FILE", "USER_CONFIG_FILE", "HISTORY_FILE",
                     "CREDENTIALS_FILE", "LIMITS_TOKEN_FILE"):
            with self.subTest(global_name=name):
                self.assertEqual(getattr(crabd, name).parent, sandbox)

    def test_the_keychain_is_off_for_the_whole_module(self):
        self.assertIs(crabd.KEYCHAIN_CREDENTIALS_ENABLED, False)


# ------------------------------------------------------------------ the fake tool

#: What the real tool printed for an item that is not there, MEASURED 2026-09-04:
#: exit 44 on the argv form and on `-i` alike.
NOT_FOUND_STDERR = ("security: SecKeychainSearchCopyNext: The specified item could not "
                    "be found in the keychain.\n")


class FakeSecurity:
    """`/usr/bin/security` as a callable, over a small in-memory item store.

    Answers the two commands crabd sends and records every one of them, because WHERE
    the secret travelled is the thing under test: `calls` holds (argv, stdin, timeout)
    per call, and the argv-leak test reads it.

    The `-i` command is tokenised with shlex rather than split on spaces, because that is
    the question the real tool's own tokenizer answers: the service name has spaces in it
    and has to survive as ONE argument. Measured against the real tool on 2026-09-04 -
    `find-generic-password -s "SideCrab quoting probe (no such item)" -a probe -w` fed to
    `security -i` answered "could not be found", not a usage error, so the quotes are
    read the way this fake reads them.
    """

    def __init__(self, items=None, answer=None):
        self.items = dict(items or {})
        self.answer = answer          # forced (code, out, err), or an exception to raise
        self.calls = []

    def __call__(self, argv, stdin_text, timeout):
        self.calls.append((list(argv), stdin_text, timeout))
        if self.answer is not None:
            if isinstance(self.answer, BaseException) or (
                    isinstance(self.answer, type)
                    and issubclass(self.answer, BaseException)):
                raise self.answer
            return self.answer
        words = shlex.split(stdin_text or "") if list(argv) == ["-i"] else list(argv)
        return self.run(words)

    def run(self, words):
        if not words:
            return (1, "", "security: no command\n")
        flags = self.flags(words[1:])
        if words[0] == "find-generic-password":
            secret = self.items.get((flags.get("-s"), flags.get("-a")))
            if secret is None:
                return (44, "", NOT_FOUND_STDERR)
            return (0, secret + "\n", "")
        if words[0] == "add-generic-password":
            self.items[(flags.get("-s"), flags.get("-a"))] = bytes.fromhex(
                flags["-X"]).decode("utf-8")
            return (0, "", "")
        return (1, "", f"security: unknown command {words[0]}\n")

    @staticmethod
    def flags(words) -> dict:
        """`-s VALUE` pairs; a bare switch such as `-w` or `-U` maps to True."""
        out, index = {}, 0
        while index < len(words):
            word = words[index]
            if word in ("-w", "-U", "-g"):
                out[word] = True
                index += 1
                continue
            out[word] = words[index + 1] if index + 1 < len(words) else None
            index += 2
        return out


CLI_SERVICE = "Claude Code-credentials"

#: A credential document of the shape the FILE has - which is the shape the Keychain
#: payload was documented to have. It was NOT read while this was written: the item holds
#: the operator's live OAuth token, so the payload is assumed to be this shape and the
#: code refuses to guess when it is not (see the two "unreadable" tests).
def credential_document(token="keychain-token", expires_in_ms=3_600_000) -> str:
    import time as _time
    return json.dumps({"claudeAiOauth": {
        "accessToken": token,
        "expiresAt": int(_time.time() * 1000) + expires_in_ms,
        "subscriptionType": "max", "rateLimitTier": "t"}})


class KeychainCase(unittest.TestCase):
    """A LimitsReader over a DarwinPlatform whose `security` is a fake, with the kill
    switch on for the length of the test and urlopen stubbed so nothing reaches the
    network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.creds = self.root / "credentials.json"      # deliberately absent by default
        original = (crabd.CREDENTIALS_FILE, crabd.KEYCHAIN_CREDENTIALS_ENABLED,
                    crabd.urllib.request.urlopen, set(crabd._LOG_ONCE_SEEN))

        def restore():
            (crabd.CREDENTIALS_FILE, crabd.KEYCHAIN_CREDENTIALS_ENABLED,
             crabd.urllib.request.urlopen) = original[:3]
            crabd._LOG_ONCE_SEEN.clear()
            crabd._LOG_ONCE_SEEN.update(original[3])

        self.addCleanup(restore)
        crabd.CREDENTIALS_FILE = self.creds
        crabd.KEYCHAIN_CREDENTIALS_ENABLED = True
        crabd._LOG_ONCE_SEEN.discard(crabd.CLI_CREDENTIALS_LOG_KEY)
        crabd._LOG_ONCE_SEEN.discard(crabd.LIMITS_TOKEN_LOG_KEY)
        self.seen = []
        body = ('{"five_hour": {"utilization": 0.25, "resets_at": 1800000000},'
                ' "seven_day": {"utilization": 0.5, "resets_at": 1800500000}}').encode()

        class _Resp:
            def __init__(self, payload): self._b = payload
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(request, timeout=None):
            self.seen.append(request.get_header("Authorization"))
            return _Resp(body)

        crabd.urllib.request.urlopen = fake_urlopen

    def platform(self, fake, **kwargs):
        return crabd.DarwinPlatform(security=fake, **kwargs)

    def reader(self, fake, **kwargs):
        return crabd.LimitsReader(cache_file=self.root / "cache.json",
                                  platform=self.platform(fake, **kwargs))

    @staticmethod
    def capture(call):
        """(what `call` returned, what it printed to stderr)."""
        original, sys.stderr = sys.stderr, io.StringIO()
        try:
            return call(), sys.stderr.getvalue()
        finally:
            sys.stderr = original

    def account(self) -> str:
        return crabd._login_account()

    def with_item(self, service, payload):
        return FakeSecurity({(service, self.account()): payload})


# ------------------------------------------- problem one: the long-lived token store

#: A `claude setup-token` value's shape. Never a real one, and never printed.
GOOD_TOKEN = "sk-ant-oat01-" + "a" * 40


class LimitsTokenStoreTests(KeychainCase):
    """`~/.sidecrab/limits-token.dpapi` is a DPAPI blob, which is a WINDOWS fact. The
    macOS store is a generic-password item in the login Keychain, service `SideCrab
    limits token`, account the login user - the same pair setup/sidecrab_setup.py probes
    by exit code when it prints the status row."""

    def store(self, fake, token=GOOD_TOKEN):
        return self.platform(fake).store_limits_token(token)

    def test_a_token_stored_is_a_token_read_back(self):
        fake = FakeSecurity()
        self.assertIs(self.store(fake), True)
        self.assertEqual(self.platform(fake).read_limits_token(None), GOOD_TOKEN)

    def test_the_item_is_named_the_way_the_installer_names_it(self):
        fake = FakeSecurity()
        self.store(fake)
        self.assertEqual(sorted(fake.items),
                         [(crabd.KEYCHAIN_LIMITS_SERVICE, self.account())])
        self.assertEqual(crabd.KEYCHAIN_LIMITS_SERVICE, "SideCrab limits token")

    def test_the_store_updates_an_existing_item_rather_than_failing_on_it(self):
        """`-U`. Without it, storing a second token is an error on an operator who has
        simply minted a new one - and the old, rejected token stays in the Keychain."""
        fake = FakeSecurity()
        self.store(fake)
        self.assertIn(" -U", fake.calls[0][1])
        self.assertIs(self.store(fake, "sk-ant-oat01-" + "b" * 40), True)
        self.assertEqual(self.platform(fake).read_limits_token(None),
                         "sk-ant-oat01-" + "b" * 40)

    def test_the_secret_never_appears_in_an_argument_list(self):
        """THE ARGV TEST, and it is the reason the store goes through `security -i`.

        `ps` is world-readable on macOS: a secret in an argument list is a secret handed
        to every user on the machine for as long as the process lives, and to anything
        sampling `ps` for ever after. So the store's argv is exactly ["-i"], the command
        that carries the value travels on STDIN, and the value is hex-encoded there so no
        quoting question can arise about it. The read needs no secret in either place -
        it names the item and the value comes back on stdout.
        """
        fake = FakeSecurity()
        self.store(fake)
        self.platform(fake).read_limits_token(None)
        for argv, stdin, _timeout in fake.calls:
            for word in argv:
                self.assertNotIn(GOOD_TOKEN, word)
                for start in range(0, len(GOOD_TOKEN) - 12):
                    self.assertNotIn(GOOD_TOKEN[start:start + 12], word)
        self.assertEqual(fake.calls[0][0], ["-i"])
        self.assertIn(GOOD_TOKEN.encode("utf-8").hex(), fake.calls[0][1])
        self.assertIsNone(fake.calls[1][1])

    def test_a_value_that_is_not_a_token_is_refused_before_anything_is_spawned(self):
        """Shape only - crabd never decodes the token - and the refusal happens BEFORE
        the seam, so a value with a quote or a newline in it never reaches `security -i`'s
        own tokenizer. That is the injection this closes: everything after the newline
        would be a second command, in a tool that writes the Keychain.
        """
        fake = FakeSecurity()
        for bad in ("has space", "", "x" * 10, "sk-ant-oat01-" + "a" * 600,
                    'sk-ant-"quote"-token-aaaaaaaaaaaaa',
                    "sk-ant-oat01-aaaaaaaaaaaaaaaaaaaa\nadd-generic-password -a x",
                    None, 12345):
            with self.subTest(token=repr(bad)[:40]):
                self.assertIs(self.store(fake, bad), False)
        self.assertEqual(fake.calls, [])


class LimitsTokenReadFailureTests(KeychainCase):
    """Absence is silent; every other failure says so once and says nothing else."""

    def test_no_item_reads_as_none_with_nothing_on_stderr(self):
        """The ordinary state of a machine whose operator has never run
        `--limits-token`, on every single limits poll."""
        fake = FakeSecurity()
        out, noise = self.capture(lambda: self.platform(fake).read_limits_token(None))
        self.assertIsNone(out)
        self.assertEqual(noise, "")

    def test_a_failure_that_is_not_absence_is_one_line_over_three_reads(self):
        refused = (1, "", "security: User interaction is not allowed.\n")
        platform = self.platform(FakeSecurity(answer=refused))
        out, noise = self.capture(
            lambda: [platform.read_limits_token(None) for _ in range(3)])
        self.assertEqual(out, [None, None, None])
        self.assertEqual(noise.count("would not hand over the limits token"), 1, noise)
        self.assertIn("exit 1", noise)
        self.assertNotIn(GOOD_TOKEN, noise)
        self.assertNotIn("User interaction", noise)     # the tool's output, not ours

    def test_a_timeout_is_the_same_refusal(self):
        """`security` can block on a locked Keychain. The limits poll is on the builder's
        cadence and must not wait for it: the seam has its own timeout, and a timed-out
        read is an absent token, not an exception on a daemon thread."""
        platform = self.platform(
            FakeSecurity(answer=subprocess.TimeoutExpired("security", 5)))
        out, noise = self.capture(
            lambda: [platform.read_limits_token(None) for _ in range(3)])
        self.assertEqual(out, [None, None, None])
        self.assertEqual(noise.count("would not hand over the limits token"), 1, noise)
        self.assertIn("TimeoutExpired", noise)


class WindowsTokenStoreTests(unittest.TestCase):
    """The other half of the same surface: Windows stores the token DPAPI-protected in
    the file it has always read, and every other host refuses rather than pretending."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = (crabd.LIMITS_TOKEN_FILE, set(crabd._LOG_ONCE_SEEN))

        def restore():
            crabd.LIMITS_TOKEN_FILE = original[0]
            crabd._LOG_ONCE_SEEN.clear()
            crabd._LOG_ONCE_SEEN.update(original[1])

        self.addCleanup(restore)
        crabd.LIMITS_TOKEN_FILE = Path(self.tmp.name) / "limits-token.dpapi"
        crabd._LOG_ONCE_SEEN.discard(crabd.LIMITS_TOKEN_LOG_KEY)

    @unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
    def test_a_token_stored_here_round_trips_through_the_real_dpapi(self):
        """What the store writes is what the reader reads, through CryptProtectData and
        CryptUnprotectData - the same pair PowerShell's [ProtectedData] uses, which is
        what wrote this file before crabd could."""
        platform = crabd.WindowsPlatform()
        self.assertIs(platform.store_limits_token(GOOD_TOKEN), True)
        self.assertNotIn(GOOD_TOKEN.encode(), crabd.LIMITS_TOKEN_FILE.read_bytes())
        self.assertEqual(platform.read_limits_token(crabd.LIMITS_TOKEN_FILE), GOOD_TOKEN)

    @unittest.skipIf(sys.platform == "win32", "this is the NOT-Windows answer")
    def test_off_windows_it_refuses_once_and_writes_nothing(self):
        """`ctypes.windll` does not exist here, so there is no way to protect the value -
        and storing it unprotected would be worse than refusing: a bearer token in a
        plain file that every later read would hand out as though it had been encrypted.
        """
        platform = crabd.WindowsPlatform()
        out, noise = self.capture(
            lambda: [platform.store_limits_token(GOOD_TOKEN) for _ in range(3)])
        self.assertEqual(out, [False, False, False])
        self.assertFalse(crabd.LIMITS_TOKEN_FILE.exists())
        self.assertEqual(noise.count("nothing was stored"), 1, noise)
        self.assertNotIn(GOOD_TOKEN, noise)

    def test_a_value_that_is_not_a_token_is_refused_on_every_platform(self):
        for platform in (crabd.WindowsPlatform(), crabd.NullPlatform()):
            with self.subTest(platform=platform.name):
                self.assertIs(platform.store_limits_token("has space"), False)
        self.assertFalse(crabd.LIMITS_TOKEN_FILE.exists())

    def test_a_platform_with_no_store_at_all_says_so_by_answering_false(self):
        """NullPlatform: no DPAPI, no Keychain. False is the honest answer and the
        installer reads it as "nothing is confirmed stored"."""
        self.assertIs(crabd.NullPlatform().store_limits_token(GOOD_TOKEN), False)
        self.assertIsNone(crabd.NullPlatform().read_limits_token(
            crabd.LIMITS_TOKEN_FILE))

    @staticmethod
    def capture(call):
        original, sys.stderr = sys.stderr, io.StringIO()
        try:
            return call(), sys.stderr.getvalue()
        finally:
            sys.stderr = original


# ------------------------------------------------- problem two: the CLI credential

class CredentialsFileWinsTests(KeychainCase):
    """The FILE first, always. It is the CLI's own documented fallback - written when the
    Keychain write fails - so where both exist the file is the one that was written last
    by the thing that owns both. Asking the Keychain anyway would also raise a prompt on
    the operator's desktop for an answer crabd already has."""

    def test_a_credentials_file_is_read_and_the_keychain_is_never_asked(self):
        self.creds.write_text(credential_document("file-token"), encoding="utf-8")
        fake = self.with_item(CLI_SERVICE, credential_document("keychain-token"))
        out = self.reader(fake).get(1_800_000_000.0, force=True)
        self.assertTrue(out["available"])
        self.assertEqual(self.seen, ["Bearer file-token"])
        self.assertEqual(fake.calls, [])


class KeychainCredentialsTests(KeychainCase):
    """No file, an item: the document comes out of the login Keychain and everything
    downstream is unchanged - which is the whole point. `_fetch` does its own JSON
    parsing, so the Keychain is a SOURCE of the same text, not a second code path."""

    def test_the_keychain_document_feeds_the_limits_reader(self):
        fake = self.with_item(CLI_SERVICE, credential_document("keychain-token"))
        out = self.reader(fake).get(1_800_000_000.0, force=True)
        self.assertTrue(out["available"])
        self.assertEqual(out["tokenSource"], "cli")
        self.assertEqual(self.seen, ["Bearer keychain-token"])
        self.assertEqual(len(fake.calls), 1)

    def test_the_keychain_token_is_nowhere_in_the_served_document(self):
        fake = self.with_item(CLI_SERVICE, credential_document("keychain-token"))
        out = self.reader(fake).get(1_800_000_000.0, force=True)
        self.assertNotIn("keychain-token", crabd.dump_state(out).decode())

    def test_the_read_carries_no_secret_in_its_argv_and_nothing_on_stdin(self):
        """The read is the direction that needs no `-i`: the secret comes back on
        STDOUT, and the argv names only the item."""
        fake = self.with_item(CLI_SERVICE, credential_document("keychain-token"))
        self.reader(fake).get(1_800_000_000.0, force=True)
        argv, stdin, timeout = fake.calls[0]
        self.assertEqual(argv, ["find-generic-password", "-s", CLI_SERVICE,
                                "-a", self.account(), "-w"])
        self.assertIsNone(stdin)
        self.assertEqual(timeout, crabd.KEYCHAIN_TIMEOUT_SEC)


class NoKeychainItemTests(KeychainCase):
    """Exit 44 is ABSENCE, not failure: no file and no item is the same "nothing is
    logged in here" crabd has always reported, and it must stay silent - a line every
    poll on a machine nobody has run `claude` on yet is noise, not observability."""

    def test_no_item_reads_as_the_unchanged_no_credentials_note(self):
        fake = FakeSecurity()
        out, noise = self.capture(
            lambda: self.reader(fake).get(1_800_000_000.0, force=True))
        self.assertFalse(out["available"])
        self.assertIn("no Claude credentials", out["note"])
        self.assertEqual(noise, "")
        self.assertEqual(len(fake.calls), 1)


class KeychainRefusedTests(KeychainCase):
    """The failure a LaunchAgent meets: the item is there and this process is not on its
    access list, so the read needs a dialog. In a GUI session that is one prompt ("Always
    Allow" ends it); in a session with no UI it is exit 36, "User interaction is not
    allowed".

    That is a DIFFERENT claim from "no credentials" and it gets different words, because
    the two have different actions attached: one says log in, the other says approve the
    prompt. An operator told "run /login" for a Keychain the daemon simply could not open
    would do it, watch nothing change, and have no next move.
    """

    REFUSED = (36, "", "security: SecKeychainSearchCopyNext: User interaction is not "
                       "allowed.\n")

    def test_a_refused_read_is_its_own_note_and_never_the_missing_one(self):
        refused = self.reader(FakeSecurity(answer=self.REFUSED))
        out, _noise = self.capture(lambda: refused.get(1_800_000_000.0, force=True))
        self.assertFalse(out["available"])
        self.assertIn("Keychain", out["note"])
        self.assertIn("Always Allow", out["note"])
        missing = self.reader(FakeSecurity()).get(1_800_000_000.0, force=True)
        self.assertNotEqual(out["note"], missing["note"])

    def test_the_refusal_is_logged_once_over_three_polls_and_names_the_exit_code(self):
        reader = self.reader(FakeSecurity(answer=self.REFUSED))
        _out, noise = self.capture(
            lambda: [reader.get(1_800_000_000.0, force=True) for _ in range(3)])
        self.assertEqual(noise.count("would not hand over"), 1, noise)
        self.assertIn("exit 36", noise)
        # The tool's own stderr is never echoed: it is output, and output is the thing
        # that could one day carry a value.
        self.assertNotIn("SecKeychainSearchCopyNext", noise)

    def test_a_seam_that_cannot_be_run_at_all_is_the_same_refusal(self):
        """A missing binary, a refused spawn, a `security` that never returns. All of
        them are "there may be credentials and crabd could not get at them"."""
        for blows_up in (FileNotFoundError(2, "no security"),
                         PermissionError(13, "denied"),
                         subprocess.TimeoutExpired("security", 5)):
            with self.subTest(failure=type(blows_up).__name__):
                crabd._LOG_ONCE_SEEN.discard(crabd.CLI_CREDENTIALS_LOG_KEY)
                reader = self.reader(FakeSecurity(answer=blows_up))
                out, noise = self.capture(
                    lambda: reader.get(1_800_000_000.0, force=True))
                self.assertFalse(out["available"])
                self.assertIn("Keychain", out["note"])
                self.assertEqual(noise.count("would not hand over"), 1, noise)


class KeychainKillSwitchTests(KeychainCase):
    """Two ways the Keychain is not consulted at all, both silent absence."""

    def test_the_kill_switch_stops_the_keychain_being_asked(self):
        crabd.KEYCHAIN_CREDENTIALS_ENABLED = False
        fake = self.with_item(CLI_SERVICE, credential_document())
        out = self.reader(fake).get(1_800_000_000.0, force=True)
        self.assertFalse(out["available"])
        self.assertIn("no Claude credentials", out["note"])
        self.assertEqual(fake.calls, [])

    def test_a_custom_claude_home_never_consults_the_keychain(self):
        """CRABD_CLAUDE_HOME points crabd at a different config dir, and the CLI keys a
        DIFFERENT Keychain entry for one - a name crabd cannot compose. Asking about the
        default item would answer about the wrong login, confidently."""
        fake = self.with_item(CLI_SERVICE, credential_document())
        out = self.reader(fake, custom_claude_home=True).get(1_800_000_000.0, force=True)
        self.assertFalse(out["available"])
        self.assertIn("no Claude credentials", out["note"])
        self.assertEqual(fake.calls, [])

    def test_the_default_answer_to_that_question_is_read_from_the_environment(self):
        """A SOURCE-TEXT test, because the rule is an IMPORT-TIME one: the flag is
        computed once from CRABD_CLAUDE_HOME, and a behavioural test would only prove
        whatever the environment running the suite happens to hold."""
        lines = [line for line in Path(crabd.__file__).read_text(
            encoding="utf-8").splitlines()
            if line.startswith("CUSTOM_CLAUDE_HOME")]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("CRABD_CLAUDE_HOME", lines[0])


class KeychainPayloadShapeTests(KeychainCase):
    """The payload was NEVER READ while this was written, so it is parsed as the file's
    shape and nothing is guessed. A payload that is not that shape falls onto the notes
    `_fetch` already had - which is the honest answer, and never a token."""

    def test_a_payload_that_is_not_json_is_the_unreadable_note(self):
        fake = self.with_item(CLI_SERVICE, "not json at all")
        out = self.reader(fake).get(1_800_000_000.0, force=True)
        self.assertFalse(out["available"])
        self.assertIn("unreadable", out["note"])
        self.assertEqual(self.seen, [])

    def test_json_without_the_access_token_is_the_no_token_note(self):
        fake = self.with_item(CLI_SERVICE, json.dumps({"claudeAiOauth": {}}))
        out = self.reader(fake).get(1_800_000_000.0, force=True)
        self.assertFalse(out["available"])
        self.assertIn("no Claude access token", out["note"])
        self.assertEqual(self.seen, [])


# ------------------------------------------- the notes, and what the panel is told

class LimitsTokenHintTests(unittest.TestCase):
    """The command an operator has to run is a PLATFORM answer, like the port-holder
    hint before it. Three notes name it, and until now all three named a PowerShell
    script - on a Mac, an instruction to run something that is not there and cannot be
    installed. A note whose whole job is to say what to do next has to be runnable."""

    def test_each_platform_names_its_own_command(self):
        self.assertEqual(crabd.WindowsPlatform().limits_token_hint(),
                         "Install-SideCrab.ps1 -LimitsToken")
        self.assertEqual(crabd.DarwinPlatform().limits_token_hint(),
                         "setup/install.sh --limits-token")

    def test_a_platform_with_no_store_says_so_instead_of_naming_a_command(self):
        """NullPlatform's store_limits_token answers False, so there is no command to
        give - and inventing one would send an operator to a tool that cannot work."""
        hint = crabd.NullPlatform().limits_token_hint()
        self.assertEqual(hint, "(no long-lived token store on this platform)")
        self.assertIs(crabd.NullPlatform().store_limits_token(GOOD_TOKEN), False)


class LimitsPrecedenceThroughTheSeamTests(KeychainCase):
    """v0.30.0's precedence, unchanged, with both sources coming out of the Keychain.

    The rule is CLI-when-fresh, else the long-lived token: the CLI token is the one whose
    scopes are proven against this endpoint every day, and the stored one is what keeps
    the gauges alive for the hours (or days) the CLI credential sits expired. Nothing
    about that moved on macOS - only where the two values are read from.
    """

    def documents(self, expires_in_ms, token=None):
        """A fake holding the CLI credential item, and optionally the SideCrab one."""
        items = {(CLI_SERVICE, self.account()):
                 credential_document("cli-token", expires_in_ms)}
        if token is not None:
            items[(crabd.KEYCHAIN_LIMITS_SERVICE, self.account())] = token
        return FakeSecurity(items)

    def services_asked(self, fake):
        return [argv[2] for argv, _stdin, _timeout in fake.calls
                if argv and argv[0] == "find-generic-password"]

    def test_a_fresh_cli_credential_wins_and_the_stored_token_is_never_fetched(self):
        fake = self.documents(3_600_000, GOOD_TOKEN)
        out = self.reader(fake).get(1_800_000_000.0, force=True)
        self.assertTrue(out["available"])
        self.assertEqual(out["tokenSource"], "cli")
        self.assertEqual(self.seen, ["Bearer cli-token"])
        self.assertEqual(self.services_asked(fake), [CLI_SERVICE])

    def test_an_expired_cli_credential_falls_back_to_the_stored_token(self):
        fake = self.documents(-1000, GOOD_TOKEN)
        out = self.reader(fake).get(1_800_000_000.0, force=True)
        self.assertTrue(out["available"])
        self.assertEqual(out["tokenSource"], "sidecrab")
        self.assertEqual(self.seen, ["Bearer " + GOOD_TOKEN])
        self.assertEqual(self.services_asked(fake),
                         [CLI_SERVICE, crabd.KEYCHAIN_LIMITS_SERVICE])

    def test_expired_with_nothing_stored_names_the_command_for_this_platform(self):
        out = self.reader(self.documents(-1000)).get(1_800_000_000.0, force=True)
        self.assertFalse(out["available"])
        self.assertIn("expired", out["note"])
        self.assertIn("setup/install.sh --limits-token", out["note"])
        self.assertNotIn("Install-SideCrab", out["note"])
        self.assertEqual(self.seen, [])          # the endpoint was never reached

    def test_a_rejected_stored_token_names_itself_and_the_same_command(self):
        """401 while the STORED token is in use is its own failure: the CLI credential is
        fine and the long-lived one has been revoked or has expired, so "run /login"
        would be advice about the wrong secret."""
        fake = self.documents(-1000, GOOD_TOKEN)
        rejecting = []

        def refusing(request, timeout=None):
            rejecting.append(request.get_header("Authorization"))
            raise crabd.urllib.error.HTTPError(request.full_url, 401, "no", {}, None)

        crabd.urllib.request.urlopen = refusing
        out = self.reader(fake).get(1_800_000_000.0, force=True)
        self.assertFalse(out["available"])
        self.assertIn("SideCrab limits token rejected", out["note"])
        self.assertIn("claude setup-token", out["note"])
        self.assertIn("setup/install.sh --limits-token", out["note"])
        self.assertEqual(rejecting, ["Bearer " + GOOD_TOKEN])


class NeitherSecretReachesTheWireTests(KeychainCase):
    """The rule both problems share, asserted on the bytes crabd actually serves.

    The tokens are read, used as a request header and dropped: never logged, never
    cached, never in `/v1/state` and never in `/v1/health`. This is the test that would
    catch a future `note` composed from an exception, or a diagnostic block that grew a
    field it should not have.
    """

    def served(self, fake):
        """(the /v1/state bytes, the /v1/health bytes) from a real crabd on a test port,
        built over a limits reader that has just used the stored token."""
        root = self.root / "projects"
        root.mkdir(exist_ok=True)
        reader = self.reader(fake)
        reader.get(1_800_000_000.0, force=True)
        builder = crabd.StateBuilder(crabd.TranscriptStore(root), crabd.HookTracker(),
                                     reader, 1_800_000_000.0)
        with builder._lock:
            builder._state = builder.build()
        original = crabd.Handler.builder
        self.addCleanup(lambda: setattr(crabd.Handler, "builder", original))
        crabd.Handler.builder = builder
        server, thread, _port, client = start_test_server(
            lambda: crabd.CrabdServer(("127.0.0.1", 0), crabd.Handler))
        self.addCleanup(client.close)

        def stop():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.addCleanup(stop)
        return (client.get("/v1/state").body, client.get("/v1/health").body)

    def test_the_stored_token_is_in_neither_served_document(self):
        fake = FakeSecurity({
            (CLI_SERVICE, self.account()): credential_document("cli-token", -1000),
            (crabd.KEYCHAIN_LIMITS_SERVICE, self.account()): GOOD_TOKEN})
        state, health = self.served(fake)
        self.assertIn(b'"tokenSource": "sidecrab"', state)   # it really was used
        for body in (state, health):
            self.assertNotIn(GOOD_TOKEN.encode(), body)
            self.assertNotIn(b"cli-token", body)


class ModelCatalogThroughTheKeychainTests(KeychainCase):
    """The ctx-fill bar's DENOMINATOR comes from the same credential.

    ModelCatalog read `~/.claude/.credentials.json` itself, which on a Keychain-only Mac
    is a file that is not there - so every session card lost its context bar as well as
    the limit gauges, and for the same reason. It goes through the platform reader now.

    The injected `credentials_file` still wins and is still read directly: it is the seam
    the catalog's own tests are built on, and it is what keeps a suite that constructs a
    real catalog from reaching the operator's token and then the network.
    """

    CATALOG = json.dumps({"data": [{"id": "claude-fable-5",
                                    "max_input_tokens": 1_000_000}]})

    def catalog_http(self, body):
        """A urlopen stub recording the Authorization header it was handed."""
        seen = []

        class _Resp:
            def __init__(self, payload): self._b = payload.encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(request, timeout=None):
            seen.append(request.get_header("Authorization"))
            return _Resp(body)

        crabd.urllib.request.urlopen = fake
        return seen

    def test_the_window_is_fetched_with_the_token_from_the_keychain(self):
        seen = self.catalog_http(self.CATALOG)
        fake = self.with_item(CLI_SERVICE, credential_document("keychain-token"))
        catalog = crabd.ModelCatalog(platform=self.platform(fake))
        self.assertEqual(catalog.window("claude-fable-5", 1_800_000_000.0), 1_000_000)
        self.assertEqual(seen, ["Bearer keychain-token"])

    def test_an_injected_file_is_still_read_directly_and_the_keychain_is_not_asked(self):
        seen = self.catalog_http(self.CATALOG)
        self.creds.write_text(credential_document("file-token"), encoding="utf-8")
        fake = self.with_item(CLI_SERVICE, credential_document("keychain-token"))
        catalog = crabd.ModelCatalog(credentials_file=self.creds,
                                     platform=self.platform(fake))
        self.assertEqual(catalog.window("claude-fable-5", 1_800_000_000.0), 1_000_000)
        self.assertEqual(seen, ["Bearer file-token"])
        self.assertEqual(fake.calls, [])

    def test_a_refused_keychain_is_no_window_and_one_line_not_a_crash(self):
        """The catalog's failure rule is one answer for everything: no entry, so
        `contextWindowTokens` is null and the bar does not draw. A PermissionError out of
        the platform must land there too - this runs inside build(), which may not raise.
        """
        seen = self.catalog_http(self.CATALOG)
        refused = FakeSecurity(answer=(36, "", "User interaction is not allowed.\n"))
        catalog = crabd.ModelCatalog(platform=self.platform(refused))
        out, noise = self.capture(
            lambda: [catalog.window("claude-fable-5", 1_800_000_000.0 + n * 100_000)
                     for n in range(3)])
        self.assertEqual(out, [None, None, None])
        self.assertEqual(seen, [])
        self.assertEqual(noise.count("would not hand over"), 1, noise)


# ------------------------------------------------------- the suite cannot reach it

class EveryModuleDisablesTheKeychainTests(unittest.TestCase):
    """Read against the SOURCE TEXT of every companion test module.

    The kill switch is only a guarantee if every module sets it, and a module added later
    is exactly the one that would forget. Named the same way the four path globals are
    named in each setUpModule - and for the same reason: the failure it prevents is a
    suite run that raises a Keychain prompt on the operator's desktop, or reads a secret
    it has no business seeing.
    """

    def test_every_test_module_names_the_kill_switch_in_its_setupmodule(self):
        modules = sorted(Path(__file__).resolve().parent.glob("test_*.py"))
        self.assertGreaterEqual(len(modules), 9, modules)
        for path in modules:
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                setups = [node for node in tree.body
                          if isinstance(node, ast.FunctionDef)
                          and node.name == "setUpModule"]
                self.assertEqual(len(setups), 1, path.name)
                self.assertIn("KEYCHAIN_CREDENTIALS_ENABLED",
                              ast.unparse(setups[0]), path.name)


class NoTestReachesTheRealSecurityBinaryTests(KeychainCase):
    """The kill switch, proved as an ISOLATION guarantee rather than as a feature.

    A DarwinPlatform built with no injected runner is what production uses, and several
    tests in this suite build one (the fleet and host modules do). With the switch off -
    which is how every module leaves it - such a platform cannot reach `/usr/bin/security`
    even with the credentials file missing, which is the state a developer's Mac is in.
    """

    class Reached(Exception):
        """Not an OSError, so the platform's own catch cannot swallow it."""

    def rig(self):
        """subprocess.run replaced by one that explodes on a `security` argv."""
        original = crabd.subprocess.run
        self.addCleanup(lambda: setattr(crabd.subprocess, "run", original))

        def refusing(argv, **kwargs):
            if any("security" in str(word) for word in argv):
                raise self.Reached(argv[0])
            return original(argv, **kwargs)

        crabd.subprocess.run = refusing

    def test_with_the_switch_off_a_default_platform_never_spawns_it(self):
        crabd.KEYCHAIN_CREDENTIALS_ENABLED = False
        self.rig()
        self.assertFalse(self.creds.exists())
        self.assertIsNone(crabd.DarwinPlatform().cli_credentials())

    def test_and_the_rig_really_would_have_caught_it(self):
        """The negative above is worth nothing without this: with the switch ON, the
        same platform does reach for the tool."""
        self.rig()
        with self.assertRaises(self.Reached):
            crabd.DarwinPlatform(custom_claude_home=False).cli_credentials()


if __name__ == "__main__":
    unittest.main()
