"""The transport: which port crabd listens on, which address it binds, and what it does
when the port is already held.

Held apart from the endpoint suites because none of it is about a document. The port
moved from 2722 to 9999 when the panel became something a browser opens, and the three
rules that came with the move are the ones a later edit is most likely to relax by
accident: the bind address is a LITERAL and nothing may widen it, a collision fails
LOUDLY rather than drifting to another port, and socket reuse is a per-platform answer
rather than a constant.
"""

import contextlib
import http.client
import io
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import crabd  # noqa: E402


# --------------------------------------------------------------- module isolation

_MODULE_TMP = None


def setUpModule():
    """The same hard isolation the other companion modules take. Nothing in here builds
    a reader, but `import crabd` alone resolves these globals under ~, and a later test
    added to this module must not be the one that reaches the operator's files."""
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    root = Path(_MODULE_TMP.name)
    setUpModule.originals = (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
                             crabd.HISTORY_FILE, crabd.PANEL_TOKEN_FILE,
                             crabd.CREDENTIALS_FILE, crabd.LIMITS_TOKEN_FILE,
                             crabd.PROJECTS_DIR)
    crabd.LIMITS_CACHE_FILE = root / "limits-cache.json"
    crabd.USER_CONFIG_FILE = root / "config.json"
    crabd.HISTORY_FILE = root / "history.jsonl"
    crabd.PANEL_TOKEN_FILE = root / "panel-token"
    crabd.CREDENTIALS_FILE = root / "no-such-credentials.json"
    crabd.LIMITS_TOKEN_FILE = root / "no-such-limits-token.dpapi"
    # main() builds a real TranscriptStore over this. Repointed at an empty tree so the
    # one test that runs main() cannot walk the operator's transcripts.
    crabd.PROJECTS_DIR = root / "projects"
    crabd.PROJECTS_DIR.mkdir()
    # The Keychain kill switch, for the same reason as the paths above: with it
    # False, nothing in this module can reach the operator's login Keychain - no
    # prompt on their desktop, and no secret this suite has any business seeing.
    setUpModule.keychain = crabd.KEYCHAIN_CREDENTIALS_ENABLED
    crabd.KEYCHAIN_CREDENTIALS_ENABLED = False


def tearDownModule():
    (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE, crabd.HISTORY_FILE,
     crabd.PANEL_TOKEN_FILE, crabd.CREDENTIALS_FILE, crabd.LIMITS_TOKEN_FILE,
     crabd.PROJECTS_DIR) = setUpModule.originals
    # main() leaves a builder on the Handler CLASS, and a builder outliving this module
    # points at a TemporaryDirectory that is about to be deleted.
    crabd.Handler.builder = None
    _MODULE_TMP.cleanup()
    crabd.KEYCHAIN_CREDENTIALS_ENABLED = setUpModule.keychain


SOURCE = Path(crabd.__file__).read_text(encoding="utf-8")
ALL_LINES = SOURCE.splitlines()
#: Comment lines are dropped before every source-text assertion below. This module's
#: whole subject is which literals may appear, so the prose that explains a rule names
#: the same strings the rule forbids - and a test that cannot tell an explanation from
#: a branch punishes writing the explanation down. (Same reasoning, same shape, as
#: test_crabd_platform.OnePlatformDecisionTests.)
CODE_LINES = [line for line in ALL_LINES if not line.strip().startswith("#")]


def body_of(func_name: str) -> list[str]:
    """The indented lines of one top-level `def`, comments dropped."""
    out, inside = [], False
    for line in ALL_LINES:
        if line.startswith(f"def {func_name}("):
            inside = True
            continue
        if inside:
            if line and not line[:1].isspace():
                break
            if line.strip() and not line.strip().startswith("#"):
                out.append(line)
    return out


def import_crabd_with(env_overrides):
    """crabd's PORT, read out of a FRESH interpreter. -> the printed value.

    PORT is resolved at import, so an in-process os.environ patch could never move it;
    only a new interpreter answers the question this asks.
    """
    env = dict(os.environ)
    env.pop("CRABD_PORT", None)
    env.update(env_overrides)
    env["PYTHONPATH"] = str(Path(crabd.__file__).resolve().parent)
    out = subprocess.run(
        [sys.executable, "-c", "import crabd; print(crabd.PORT)"],
        capture_output=True, text=True, env=env, check=True, timeout=60)
    return out.stdout.strip()


# ------------------------------------------------------------------- A1: the port

class DefaultPortTests(unittest.TestCase):
    """9999, and one named constant that says so.

    2722 was C-R-A-B on a phone keypad and a fine choice while the only client was a
    widget the operator configured once. The panel is now a page a browser opens, so the
    port is something a person types; the literal lived in five places and this is the
    one.
    """

    def test_the_default_port_is_a_named_constant(self):
        self.assertEqual(crabd.DEFAULT_PORT, 9999)

    def test_an_unconfigured_crabd_listens_on_the_default(self):
        self.assertEqual(import_crabd_with({}), "9999")

    def test_crabd_port_still_moves_a_second_instance(self):
        """The one override, unchanged: a test instance runs against the real ~/.claude
        without racing the live service."""
        self.assertEqual(import_crabd_with({"CRABD_PORT": "9998"}), "9998")

    def test_the_port_line_reads_the_constant_not_a_literal(self):
        """The constant IS the default. A second literal beside it is a second answer,
        and the one that moves is never the one the operator read."""
        lines = [line for line in CODE_LINES if line.startswith("PORT")]
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("DEFAULT_PORT", lines[0])

    def test_the_harness_guard_names_the_constant_not_a_number(self):
        """`start_test_server` refuses to bind production. Read against the SOURCE
        because the guard only fires on a port a test never draws: a behavioural test
        would have to bind 9999 on the developer's machine to see it, which is exactly
        what the guard exists to prevent. A literal here is a guard that stopped
        guarding the day the port moved - which it just did."""
        text = (Path(__file__).resolve().parent / "_httpkeepalive.py").read_text(
            encoding="utf-8")
        guards = [line for line in text.splitlines()
                  if "assert port !=" in line]
        self.assertEqual(len(guards), 1, guards)
        self.assertIn("crabd.DEFAULT_PORT", guards[0])


# ------------------------------------------------------------ A2: the bind address

class LoopbackOnlyTests(unittest.TestCase):
    """The bind address is a LITERAL, and nothing may widen it.

    Loopback binding is the whole of crabd's access-control story: it reads ~/.claude,
    serves session titles, cwds and the full text of a question a session is waiting on,
    and has no authentication on the read paths at all. A configurable host is one
    typo - or one helpful "expose it to my other machine" issue - away from putting all
    of that on a LAN interface, and the change would look like a feature.

    Asserted against the SOURCE TEXT because there is nothing to observe: a crabd that
    read CRABD_HOST would still bind 127.0.0.1 on every machine where nobody set it,
    which is every machine the suite runs on.
    """

    def test_the_bind_address_is_one_module_level_literal(self):
        assignments = [line for line in CODE_LINES if re.match(r"^\s*HOST\s*=", line)]
        self.assertEqual(assignments, ['HOST = "127.0.0.1"'])

    def test_no_wildcard_bind_address_appears_anywhere(self):
        """`0.0.0.0` is the string that turns a localhost service into a network one."""
        self.assertEqual([line.strip() for line in CODE_LINES if "0.0.0.0" in line], [])

    def test_the_host_is_never_read_from_the_environment(self):
        """CRABD_PORT exists so a second instance can run; there is deliberately no
        CRABD_HOST, and no other environment name reaches the bind."""
        self.assertEqual([line.strip() for line in CODE_LINES if "CRABD_HOST" in line],
                         [])
        for line in CODE_LINES:
            if "os.environ" in line:
                self.assertNotIn("HOST", line, line)

    def test_the_host_is_never_read_from_the_config(self):
        """`config.json` is operator-writable and is read back into a live daemon. A
        bind address that could be set from it would be a listening-address change
        anything on the machine could make."""
        self.assertNotIn("HOST", " ".join(crabd.Handler.CONFIG_WRITABLE))
        for line in CODE_LINES:
            if "config" in line.lower():
                self.assertNotIn("HOST", line, line)

    def test_exactly_one_server_construction_and_it_binds_a_named_host(self):
        """One place binds a socket, and the address it binds is a NAME - never an
        inline string, which is how `("", PORT)` (every interface) gets written."""
        sites = [line.strip() for line in CODE_LINES if "CrabdServer((" in line]
        self.assertEqual(len(sites), 1, sites)
        self.assertRegex(sites[0], r"CrabdServer\(\((HOST|host), ")

    def test_main_only_ever_names_the_host_beside_the_module_port(self):
        """Whatever the bind is factored into, the address main() works with is the
        module constant paired with the module port - never one it composed itself, and
        never a second address that could disagree with the banner it prints."""
        naming_host = [line.strip() for line in body_of("main") if "HOST" in line]
        self.assertTrue(naming_host)
        for line in naming_host:
            self.assertIn("PORT", line, line)


# ------------------------------------------------------------- A4: the collision

class PortCollisionTests(unittest.TestCase):
    """A port already held is a LOUD stop, and never a quiet move to another one.

    The failure this shape refuses: crabd finds 9999 busy, binds 10000 instead, and
    reports success. Every hook, the status line command and the panel are still
    addressing 9999 - so the daemon is up, the feed is empty, and nothing anywhere says
    why. The old message named the port and guessed the holder was another crabd; on a
    machine where 9999 is a popular number that guess is usually wrong, so the message
    now says how to find out.
    """

    def held_port(self) -> int:
        """A port with a real listener on it, released at teardown."""
        holder = socket.socket()
        holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        self.addCleanup(holder.close)
        return holder.getsockname()[1]

    def test_a_held_port_answers_with_no_server_and_a_message(self):
        server, message = crabd._bind_server("127.0.0.1", self.held_port())
        self.assertIsNone(server)
        self.assertIsInstance(message, str)

    def test_the_message_names_the_port_and_how_to_find_what_holds_it(self):
        """Every part of it earns its place: the port, so the operator knows which
        number is contended; THIS PLATFORM's command for naming the holder, so they can
        name the process instead of guessing it is another crabd; CRABD_PORT, so there
        is a way forward that is not killing something."""
        port = self.held_port()
        _, message = crabd._bind_server("127.0.0.1", port)
        self.assertIn(str(port), message)
        self.assertIn(crabd.PLATFORM.port_holder_hint(port), message)
        self.assertIn("CRABD_PORT", message)

    def test_the_command_it_suggests_is_the_one_this_platform_has(self):
        """The old message hard-coded `lsof`, which does not exist on Windows - so the
        one message whose whole job is to say what to do next said it to half the
        operators. Patched rather than injected because the message is composed for the
        HOST crabd is running on, and that is the fact under test."""
        port = self.held_port()
        for platform, expected in ((crabd.WindowsPlatform(), "Get-NetTCPConnection"),
                                   (crabd.DarwinPlatform(), "lsof"),
                                   (crabd.NullPlatform(), "lsof")):
            with self.subTest(platform=platform.name):
                original = crabd.PLATFORM
                crabd.PLATFORM = platform
                self.addCleanup(lambda p=original: setattr(crabd, "PLATFORM", p))
                _, message = crabd._bind_server("127.0.0.1", port)
                crabd.PLATFORM = original
                self.assertIn(expected, message, platform.name)
                self.assertIn(platform.port_holder_hint(port), message, platform.name)

    def test_the_message_carries_what_the_operating_system_said(self):
        """The exception TEXT, not its class name. "OSError" tells the operator
        nothing; "[Errno 48] Address already in use" is the sentence they can search
        for, and it is the one that distinguishes a busy port from a permission
        refusal on a privileged one."""
        port = self.held_port()
        _, message = crabd._bind_server("127.0.0.1", port)
        self.assertIn("Address already in use", message)
        self.assertNotIn("(OSError)", message)

    def test_the_message_does_not_claim_a_cause_it_did_not_verify(self):
        """Quoting the OS made the old wording false. `{exc}` can be "Permission
        denied" - a privileged port, nothing holding it at all - as easily as "Address
        already in use", and the message asserted "another process is already holding
        it" in both cases. That sends an operator looking for a process that is not
        there. The command is now offered CONDITIONALLY."""
        def refusing(address, handler):
            raise PermissionError(13, "Permission denied")

        original = crabd.CrabdServer
        crabd.CrabdServer = refusing
        self.addCleanup(lambda: setattr(crabd, "CrabdServer", original))
        _, message = crabd._bind_server("127.0.0.1", 80)
        self.assertIn("Permission denied", message)
        self.assertNotIn("already holding it", message)
        self.assertIn(crabd.PLATFORM.port_holder_hint(80), message)

    def test_a_free_port_returns_a_server_and_no_message(self):
        """Port 0 rather than "find a free port, close it, hope it is still free": that
        dance has a real window in which something else takes the port, and the failure
        would look like _bind_server being wrong. The kernel hands out a port that IS
        free, atomically. Which port was asked for is a different claim, pinned by
        test_the_bind_is_attempted_once_on_exactly_the_port_it_was_given."""
        server, message = crabd._bind_server("127.0.0.1", 0)
        self.assertIsNone(message)
        self.assertIsInstance(server, crabd.CrabdServer)
        self.addCleanup(server.server_close)
        self.assertGreater(server.server_address[1], 0)

    def test_the_bind_is_attempted_once_on_exactly_the_port_it_was_given(self):
        """The proof there is no fallback: a counted factory, on a port that FAILS.
        A retry loop would show up here as a second attempt, and a drift to another
        port as a different number in the tuple."""
        attempts = []

        def counting(address, handler):
            attempts.append(address)
            raise OSError(48, "Address already in use")

        original = crabd.CrabdServer
        crabd.CrabdServer = counting
        self.addCleanup(lambda: setattr(crabd, "CrabdServer", original))
        server, message = crabd._bind_server("127.0.0.1", crabd.DEFAULT_PORT)
        self.assertIsNone(server)
        self.assertIsNotNone(message)
        self.assertEqual(attempts, [("127.0.0.1", crabd.DEFAULT_PORT)])

    def test_main_says_when_there_is_no_panel_directory_to_serve(self):
        """Not fatal - crabd without a panel is still the feed the notifier, the glow
        and an iCUE widget all live on - but said out loud, and BEFORE the bind so it is
        the first thing on stderr rather than something to scroll back for."""
        original_dir = crabd.PANEL_DIR
        crabd.PANEL_DIR = Path(_MODULE_TMP.name) / "no-such-panel"
        self.addCleanup(lambda: setattr(crabd, "PANEL_DIR", original_dir))
        original = crabd._bind_server
        crabd._bind_server = lambda host, port: (None, "crabd: bind refused")
        self.addCleanup(lambda: setattr(crabd, "_bind_server", original))
        self.addCleanup(lambda: setattr(crabd.Handler, "builder", None))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            crabd.main()
        noise = err.getvalue()
        self.assertIn("no-such-panel", noise)
        self.assertIn("CRABD_PANEL_DIR", noise)
        self.assertLess(noise.index("no-such-panel"), noise.index("bind refused"))

    def test_main_prints_the_message_to_stderr_and_returns_one(self):
        """The operator-visible half. A message composed and then swallowed is the
        silent failure this whole shape exists to remove."""
        original = crabd._bind_server
        crabd._bind_server = lambda host, port: (None, "crabd: 9999 is held by pid 4")
        self.addCleanup(lambda: setattr(crabd, "_bind_server", original))
        self.addCleanup(lambda: setattr(crabd.Handler, "builder", None))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = crabd.main()
        self.assertEqual(code, 1)
        self.assertIn("crabd: 9999 is held by pid 4", err.getvalue())


# --------------------------------------------------------------- A5: socket reuse

class SocketReuseTests(unittest.TestCase):
    """The server takes its reuse answer from the platform, and a collision is still
    loud on every one of them."""

    def test_the_server_asks_the_platform_rather_than_deciding_itself(self):
        """Read ONCE, at import, onto the class attribute - so a test that swaps
        crabd.PLATFORM has to set CrabdServer.allow_reuse_address too, or it is
        measuring the platform it replaced."""
        self.assertIs(crabd.CrabdServer.allow_reuse_address,
                      crabd.PLATFORM.server_reuse_address())

    def test_two_servers_on_one_port_still_collide(self):
        """The safety the Windows answer was protecting, asserted on whatever host is
        running this. SO_REUSEADDR on BSD and Linux does not admit a second listener, so
        turning it on there cannot bring the split feed back."""
        first = crabd.CrabdServer(("127.0.0.1", 0), crabd.Handler)
        self.addCleanup(first.server_close)
        with self.assertRaises(OSError):
            second = crabd.CrabdServer(("127.0.0.1", first.server_address[1]),
                                       crabd.Handler)
            second.server_close()

    @unittest.skipUnless(sys.platform == "darwin",
                         "the TIME_WAIT rebind is the BSD/macOS answer's whole point")
    def test_a_restart_inside_time_wait_binds_the_same_port_again(self):
        """The ordinary restart. One request answered with `Connection: close` makes
        crabd the side that closes first, which is the side that holds the 4-tuple in
        TIME_WAIT; without reuse the very next start cannot have its own port back, and
        the operator reads a message about another process holding it."""
        server = crabd.CrabdServer(("127.0.0.1", 0), crabd.Handler)
        port = server.server_address[1]
        # 0.05 for the same reason _httpkeepalive uses it: shutdown() waits out the
        # current select(), and socketserver's default makes that half a second.
        thread = threading.Thread(target=lambda: server.serve_forever(0.05),
                                  daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/v1/health", headers={"Connection": "close"})
            response = conn.getresponse()
            response.read()
            conn.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        again = crabd.CrabdServer(("127.0.0.1", port), crabd.Handler)
        again.server_close()


if __name__ == "__main__":
    unittest.main()
