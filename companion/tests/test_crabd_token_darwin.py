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
