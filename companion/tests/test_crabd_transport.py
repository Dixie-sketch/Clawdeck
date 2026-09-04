"""The transport: which port crabd listens on, which address it binds, and what it does
when the port is already held.

Held apart from the endpoint suites because none of it is about a document. The port
moved from 2722 to 9999 when the panel became something a browser opens, and the three
rules that came with the move are the ones a later edit is most likely to relax by
accident: the bind address is a LITERAL and nothing may widen it, a collision fails
LOUDLY rather than drifting to another port, and socket reuse is a per-platform answer
rather than a constant.
"""

import os
import re
import socket
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
    """The same hard isolation the other companion modules take. Nothing in here builds
    a reader, but `import crabd` alone resolves these globals under ~, and a later test
    added to this module must not be the one that reaches the operator's files."""
    global _MODULE_TMP
    _MODULE_TMP = tempfile.TemporaryDirectory()
    root = Path(_MODULE_TMP.name)
    setUpModule.originals = (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE,
                             crabd.HISTORY_FILE, crabd.PANEL_TOKEN_FILE)
    crabd.LIMITS_CACHE_FILE = root / "limits-cache.json"
    crabd.USER_CONFIG_FILE = root / "config.json"
    crabd.HISTORY_FILE = root / "history.jsonl"
    crabd.PANEL_TOKEN_FILE = root / "panel-token"


def tearDownModule():
    (crabd.LIMITS_CACHE_FILE, crabd.USER_CONFIG_FILE, crabd.HISTORY_FILE,
     crabd.PANEL_TOKEN_FILE) = setUpModule.originals
    _MODULE_TMP.cleanup()


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


if __name__ == "__main__":
    unittest.main()
