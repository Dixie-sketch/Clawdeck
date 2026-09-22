"""The shipped hook fragment, checked against what crabd actually serves.

The fragment is data the installer merges into the operator's own settings.json, so a
typo in it is not a crash anywhere - it is a hook that silently never fires, or worse, an
entry the uninstaller cannot find again. Three things are asserted here and nothing else:

  - it parses, and every entry has the shape the installer's merge expects;
  - every URL carries the marker Install/Uninstall/Repair match SideCrab's own entries on
    (`127.0.0.1:2722/v1/hook`), because an entry that misses it is an entry an uninstall
    leaves behind;
  - the events registered are the events crabd has a route for.

    python -m unittest discover -s hooks/tests -v
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]
FRAGMENT_PATH = HOOKS_DIR / "settings-hooks-fragment.json"
CRABD_PATH = HOOKS_DIR.parent / "companion" / "crabd.py"

# The substring Install-SideCrab.ps1 / Uninstall-SideCrab.ps1 match our entries on.
HOOK_URL_MARKER = "127.0.0.1:2722/v1/hook"


class FragmentShape(unittest.TestCase):
    def setUp(self):
        self.fragment = json.loads(FRAGMENT_PATH.read_text(encoding="utf-8"))["hooks"]

    def test_every_event_is_a_list_of_matchers_with_a_hooks_list(self):
        for event, matchers in self.fragment.items():
            with self.subTest(event=event):
                self.assertIsInstance(matchers, list)
                self.assertTrue(matchers, f"{event} has no matcher")
                for matcher in matchers:
                    self.assertIsInstance(matcher.get("hooks"), list)
                    self.assertTrue(matcher["hooks"])

    def test_every_entry_carries_the_merge_marker(self):
        """An entry the marker misses is one the uninstaller cannot find again - it stays
        in settings.json forever, POSTing to a crabd that is no longer installed."""
        for event, matchers in self.fragment.items():
            for matcher in matchers:
                for entry in matcher["hooks"]:
                    target = entry.get("url") or entry.get("command") or ""
                    with self.subTest(event=event, target=target):
                        self.assertIn(HOOK_URL_MARKER, target)

    def test_precompact_posts_to_its_own_route(self):
        entry = self.fragment["PreCompact"][0]["hooks"][0]
        self.assertEqual(entry["type"], "command")
        self.assertIn("/v1/hook/precompact", entry["command"])
        # Fire-and-forget, like the other five command hooks: a session about to compact
        # a large context must not wait on crabd, and `|| exit 0` keeps a stopped crabd
        # from surfacing an error inside Claude Code.
        self.assertIn("|| exit 0", entry["command"])
        self.assertEqual(entry["timeout"], 3)

    def test_only_stop_and_permission_are_two_way(self):
        http_events = sorted(event for event, matchers in self.fragment.items()
                             for matcher in matchers for entry in matcher["hooks"]
                             if entry.get("type") == "http")
        self.assertEqual(http_events, ["PermissionRequest", "Stop"])

    def test_every_registered_route_exists_in_crabd(self):
        """The fragment and the dispatcher are two files that have to agree. A hook
        pointed at a path crabd does not serve is a 404 on every event, silently."""
        source = CRABD_PATH.read_text(encoding="utf-8")
        served = set(re.findall(r'path == "(/v1/hook[^"]*)"', source))
        for matchers in self.fragment.values():
            for matcher in matchers:
                for entry in matcher["hooks"]:
                    target = entry.get("url") or entry.get("command") or ""
                    # The curl entries carry a whole shell line, so the URL is matched
                    # out of it rather than sliced: `... /v1/hook || exit 0`.
                    found = re.search(r"/v1/hook(?:/[a-z]+)?", target)
                    with self.subTest(target=target):
                        self.assertIsNotNone(found)
                        self.assertIn(found.group(0), served)


if __name__ == "__main__":
    unittest.main()
