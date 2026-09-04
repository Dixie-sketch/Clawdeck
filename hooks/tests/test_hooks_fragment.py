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

#: crabd refuses any POST that does not carry this header (403 "panel header required").
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
#: how the shell quotes the header. Windows gets `curl.exe` and double quotes (cmd.exe has no
#: single-quote literal); macOS gets the absolute `/usr/bin/curl`, because a hook inherits no
#: login PATH, and single quotes. Everything else must match, byte for byte.
_CURL_BINARY = re.compile(r"^(?:curl\.exe|/usr/bin/curl)(?=\s)")


def _normalise_command(command: str) -> str:
    """A command string with the platform's curl binary and quoting flattened away."""
    return _CURL_BINARY.sub("curl", command).replace('"', "").replace("'", "")


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
    """Every claim here holds for BOTH fragments; the platform split is the curl call only."""

    FRAGMENTS = {"windows": WINDOWS_FRAGMENT, "macos": MACOS_FRAGMENT}

    def each(self):
        for name, path in self.FRAGMENTS.items():
            with self.subTest(fragment=name):
                yield _load(path)

    def test_both_fragments_parse(self):
        for fragment in self.each():
            self.assertIsInstance(fragment["hooks"], dict)

    def test_every_url_points_at_crabd_on_9999(self):
        for fragment in self.each():
            for event, handler in _handlers(fragment):
                url = handler.get("url") or handler.get("command")
                self.assertIn(HOOK_URL_PREFIX, url, f"{event} does not reach {HOOK_URL_PREFIX}")

    def test_every_post_carries_the_panel_header(self):
        # crabd answers a POST without X-SideCrab-Panel with 403. An http handler declares
        # the header in its headers map; a command handler passes it on the curl line.
        for fragment in self.each():
            for event, handler in _handlers(fragment):
                if handler["type"] == "http":
                    self.assertEqual(handler.get("headers", {}).get(PANEL_HEADER), "1",
                                     f"{event} http hook is missing the {PANEL_HEADER} header")
                else:
                    self.assertIn(f"{PANEL_HEADER}: 1", handler["command"],
                                  f"{event} command hook is missing the {PANEL_HEADER} header")

    def test_carries_exactly_the_seven_sidecrab_events(self):
        for fragment in self.each():
            self.assertEqual(set(fragment["hooks"]), EVENTS)

    def test_http_timeouts_bracket_crabds_own_waits(self):
        # PermissionRequest sits past crabd's 55 s long poll; Stop bounds crabd's ~2 s answer.
        for fragment in self.each():
            timeouts = {event: handler["timeout"]
                        for event, handler in _handlers(fragment)
                        if handler["type"] == "http"}
            self.assertEqual(timeouts, {"Stop": 5, "PermissionRequest": 60})

    def test_session_start_is_a_command_hook(self):
        # Claude Code skips http hooks for SessionStart; an http entry there is silently dead.
        for fragment in self.each():
            for event, handler in _handlers(fragment):
                if event == "SessionStart":
                    self.assertEqual(handler["type"], "command")

    def test_the_two_fragments_differ_only_in_the_curl_invocation(self):
        # Two fragments are two chances to fix a bug once. Flatten the one licensed
        # difference - the curl binary and the shell's quoting - and any other drift
        # (a stale port, a dropped header, a changed timeout) shows up here.
        self.assertEqual(_normalise(_load(WINDOWS_FRAGMENT)), _normalise(_load(MACOS_FRAGMENT)))


if __name__ == "__main__":
    unittest.main()
