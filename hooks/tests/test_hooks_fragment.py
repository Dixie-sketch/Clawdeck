"""Headless checks on the settings fragments the installer merges into settings.json.

Pure JSON reading, no network and no Claude Code: the fragments are data, and every
claim below is one a broken fragment would silently violate at install time.

    python3 -m unittest discover -s hooks/tests -t hooks/tests -v
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]

WINDOWS_FRAGMENT = HOOKS_DIR / "settings-hooks-fragment.json"

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


def _handlers(fragment: dict):
    """(event, handler) for every hook handler in the fragment, in file order."""
    for event, matchers in fragment["hooks"].items():
        for matcher in matchers:
            for handler in matcher["hooks"]:
                yield event, handler


class WindowsFragment(unittest.TestCase):
    def setUp(self):
        self.fragment = _load(WINDOWS_FRAGMENT)

    def test_every_url_points_at_crabd_on_9999(self):
        for event, handler in _handlers(self.fragment):
            url = handler.get("url") or handler.get("command")
            self.assertIn(HOOK_URL_PREFIX, url, f"{event} does not reach {HOOK_URL_PREFIX}")

    def test_every_post_carries_the_panel_header(self):
        # crabd answers a POST without X-SideCrab-Panel with 403. An http handler declares
        # the header in its headers map; a command handler passes it on the curl line.
        for event, handler in _handlers(self.fragment):
            if handler["type"] == "http":
                self.assertEqual(handler.get("headers", {}).get(PANEL_HEADER), "1",
                                 f"{event} http hook is missing the {PANEL_HEADER} header")
            else:
                self.assertIn(f"{PANEL_HEADER}: 1", handler["command"],
                              f"{event} command hook is missing the {PANEL_HEADER} header")

    def test_carries_exactly_the_seven_sidecrab_events(self):
        self.assertEqual(set(self.fragment["hooks"]), EVENTS)

    def test_http_timeouts_bracket_crabds_own_waits(self):
        # PermissionRequest sits past crabd's 55 s long poll; Stop bounds crabd's ~2 s answer.
        timeouts = {event: handler["timeout"]
                    for event, handler in _handlers(self.fragment)
                    if handler["type"] == "http"}
        self.assertEqual(timeouts, {"Stop": 5, "PermissionRequest": 60})

    def test_session_start_is_a_command_hook(self):
        # Claude Code skips http hooks for SessionStart; an http entry there is silently dead.
        for event, handler in _handlers(self.fragment):
            if event == "SessionStart":
                self.assertEqual(handler["type"], "command")


if __name__ == "__main__":
    unittest.main()
