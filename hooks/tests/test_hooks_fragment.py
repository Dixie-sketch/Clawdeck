"""Headless checks on the settings fragments the installer merges into settings.json.

Pure JSON reading, no network and no Claude Code: the fragments are data, and every
claim below is one a broken fragment would silently violate at install time.

    python3 -m unittest discover -s hooks/tests -t hooks/tests -v
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]

WINDOWS_FRAGMENT = HOOKS_DIR / "settings-hooks-fragment.json"
MACOS_FRAGMENT = HOOKS_DIR / "settings-hooks-fragment-macos.json"

#: crabd's loopback endpoint. Every hook URL in every fragment points here.
HOOK_URL_PREFIX = "http://127.0.0.1:9999/v1/hook"

#: crabd 0.31.0 and later refuses any POST that does not carry this header (403 "panel
#: header required"); an older crabd ignores it, so both fragments send it unconditionally.
PANEL_HEADER = "X-SideCrab-Panel"

#: Exactly the events SideCrab installs. crabd's state machine is defined over this set:
#: an extra event arrives as an unknown, a missing one loses a transition outright.
EVENTS = {
    "SessionStart",
    "UserPromptSubmit",
    "Notification",
    "Stop",
    "SubagentStop",
    "PermissionRequest",
    "SessionEnd",
}


def _load(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


#: The ONLY licensed difference between the two fragments: which curl binary is named and
#: how the shell quotes the header ARGUMENT. Windows gets `curl.exe` and double quotes
#: (cmd.exe has no single-quote literal); macOS gets the absolute `/usr/bin/curl`, because a
#: hook inherits no login PATH, and single quotes. Everything else must match, byte for byte.
_CURL_BINARY = re.compile(r"^(?:curl\.exe|/usr/bin/curl)(?=\s)")

#: Only a QUOTED -H argument is unquoted, and only its own quote character. Stripping every
#: quote on the line instead would also flatten a difference in --data, in a URL, or in a
#: shell-quoted path - drift the parity test exists to catch.
_HEADER_ARG = re.compile(r"-H (\"|')(?P<value>[^\"']*)\1")


def _normalise_command(command: str) -> str:
    """A command string with the platform's curl binary and header quoting flattened away."""
    return _HEADER_ARG.sub(lambda m: "-H " + m.group("value"), _CURL_BINARY.sub("curl", command))


def _normalise(fragment: dict) -> dict:
    normalised = json.loads(json.dumps(fragment))
    for _event, handler in _handlers(normalised):
        if "command" in handler:
            handler["command"] = _normalise_command(handler["command"])
    return normalised


def _handlers(fragment: dict):
    """(event, handler) for every hook handler in the fragment, in file order."""
    for event, matchers in fragment["hooks"].items():
        for matcher in matchers:
            for handler in matcher["hooks"]:
                yield event, handler


class FragmentInvariants(unittest.TestCase):
    """Every claim holds for BOTH fragments; each test drives its own subTest loop, because
    yielding from inside one loses the fragment label and adds a phantom GeneratorExit."""

    FRAGMENTS = (("windows", WINDOWS_FRAGMENT), ("macos", MACOS_FRAGMENT))

    def test_both_fragments_parse(self):
        for name, path in self.FRAGMENTS:
            with self.subTest(fragment=name):
                self.assertIsInstance(_load(path)["hooks"], dict)

    def test_every_url_points_at_crabd_on_9999(self):
        for name, path in self.FRAGMENTS:
            with self.subTest(fragment=name):
                for event, handler in _handlers(_load(path)):
                    url = handler.get("url") or handler.get("command")
                    # A handler with neither key is a malformed fragment, not a URL that
                    # fails to match: say so, rather than letting assertIn raise TypeError
                    # on None and report the shape problem as a stack trace.
                    self.assertIsNotNone(url, f"{event} handler has neither url nor command")
                    self.assertIn(HOOK_URL_PREFIX, url, f"{event} does not reach {HOOK_URL_PREFIX}")

    def test_every_post_carries_the_panel_header(self):
        # crabd 0.31.0 and later answers a POST without X-SideCrab-Panel with 403. An http
        # handler declares it in its headers map; a command handler passes it on the curl
        # line. A handler carrying no command at all fails as a shape problem, not a KeyError.
        for name, path in self.FRAGMENTS:
            with self.subTest(fragment=name):
                for event, handler in _handlers(_load(path)):
                    if handler["type"] == "http":
                        self.assertEqual(handler.get("headers", {}).get(PANEL_HEADER), "1",
                                         f"{event} http hook is missing the {PANEL_HEADER} header")
                    else:
                        command = handler.get("command")
                        self.assertIsNotNone(command, f"{event} command hook has no command")
                        self.assertIn(f"{PANEL_HEADER}: 1", command,
                                      f"{event} command hook is missing the {PANEL_HEADER} header")

    def test_carries_exactly_the_seven_sidecrab_events(self):
        for name, path in self.FRAGMENTS:
            with self.subTest(fragment=name):
                self.assertEqual(set(_load(path)["hooks"]), EVENTS)

    def test_http_timeouts_bracket_crabds_own_waits(self):
        # PermissionRequest sits past crabd's 55 s long poll; Stop bounds crabd's ~2 s answer.
        for name, path in self.FRAGMENTS:
            with self.subTest(fragment=name):
                timeouts = {event: handler["timeout"]
                            for event, handler in _handlers(_load(path))
                            if handler["type"] == "http"}
                self.assertEqual(timeouts, {"Stop": 5, "PermissionRequest": 60})

    def test_session_start_is_a_command_hook(self):
        # Claude Code skips http hooks for SessionStart; an http entry there is silently dead.
        for name, path in self.FRAGMENTS:
            with self.subTest(fragment=name):
                for event, handler in _handlers(_load(path)):
                    if event == "SessionStart":
                        self.assertEqual(handler["type"], "command")

    def test_the_two_fragments_differ_only_in_the_curl_invocation(self):
        # Two fragments are two chances to fix a bug once. Flatten the one licensed
        # difference - the curl binary and the shell's quoting - and any other drift
        # (a stale port, a dropped header, a changed timeout) shows up here.
        self.assertEqual(_normalise(_load(WINDOWS_FRAGMENT)), _normalise(_load(MACOS_FRAGMENT)))


if __name__ == "__main__":
    unittest.main()
