"""The shipped hook fragment, checked against what crabd actually serves.

The fragment is data the installer merges into the operator's own settings.json, so a
typo in it is not a crash anywhere - it is a hook that silently never fires, or worse, an
entry the uninstaller cannot find again. Five things are asserted here and nothing else:

  - it parses, and every entry has the shape the installer's merge expects;
  - every URL carries the marker Install/Uninstall/Repair match SideCrab's own entries on
    (`127.0.0.1:2722/v1/hook`), because an entry that misses it is an entry an uninstall
    leaves behind;
  - the events registered are the events crabd has a route for;
  - `SessionStart` is the one entry that may NOT be `type: "http"` (v0.36.0, measured);
  - every other entry IS http, on its own per-event route, inside its timeout budget.

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

# MEASURED in the shipped claude.exe 2.1.278 on 2026-09-22, in the hook dispatcher
# itself: `r === "SessionStart" || r === "Setup"` filters out every `type: "http"`
# handler and logs "HTTP hooks are not supported for <event>". SideCrab registers no
# `Setup` hook, so SessionStart is the whole of the exception - and it is why B1 moved
# five of the six command hooks and not all six. A future CLI that lifts the restriction
# needs one word changed in the fragment and nothing changed in crabd.
NO_HTTP_EVENTS = frozenset({"SessionStart", "Setup"})

# The per-event routes B1 introduced, and the event each belongs to. Spelled out rather
# than derived from the fragment: a test that reads its subject's own answer back can
# only ever agree with it.
EXPECTED_ROUTE = {
    "SessionStart": "/v1/hook/session-start",
    "UserPromptSubmit": "/v1/hook/prompt",
    "Notification": "/v1/hook/notification",
    "Stop": "/v1/hook/stop",
    "StopFailure": "/v1/hook/stop-failure",
    "SubagentStart": "/v1/hook/subagent-start",
    "SubagentStop": "/v1/hook/subagent-stop",
    "PermissionRequest": "/v1/hook/permission",
    "SessionEnd": "/v1/hook/session-end",
    "PreCompact": "/v1/hook/precompact",
}

# Seconds. The two-way hooks get room for crabd's own answer (2 s for Stop, a 55 s long
# poll for PermissionRequest); every fire-and-forget entry gets 3, which is what the
# curl entries had. A hook that outruns its timeout is cancelled and its output
# discarded - harmless for a 204, which is why the budget can be this tight.
EXPECTED_TIMEOUT = {"Stop": 5, "PermissionRequest": 60}
DEFAULT_TIMEOUT = 3


def _entries(fragment):
    for event, matchers in fragment.items():
        for matcher in matchers:
            for entry in matcher["hooks"]:
                yield event, entry


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
        for event, entry in _entries(self.fragment):
            target = entry.get("url") or entry.get("command") or ""
            with self.subTest(event=event, target=target):
                self.assertIn(HOOK_URL_MARKER, target)

    def test_no_matcher_is_declared_on_any_event(self):
        """B3 (v0.36.0) deliberately did NOT add `matcher` to Notification.

        A matcher FILTERS. The CLI's live notification-type enum has 15 values and
        differs from the published reference's list in both directions, so a fragment
        that enumerates types is a fragment that silently drops the next one the CLI
        adds. crabd reads `notification_type` off the payload instead, which the CLI
        puts there whether or not a matcher is declared.
        """
        for event, matchers in self.fragment.items():
            for matcher in matchers:
                with self.subTest(event=event):
                    self.assertNotIn("matcher", matcher)

    def test_session_start_is_the_only_entry_that_is_not_http(self):
        """The CLI SKIPS http handlers on SessionStart and logs it. An http entry there
        is not a slow hook or a failing one - it never fires at all, so crabd would
        never learn that a session had opened."""
        for event, entry in _entries(self.fragment):
            with self.subTest(event=event):
                if event in NO_HTTP_EVENTS:
                    self.assertEqual(entry["type"], "command")
                    # `|| exit 0` swallows curl's exit code so a stopped crabd can never
                    # surface an error inside Claude Code.
                    self.assertIn("|| exit 0", entry["command"])
                else:
                    self.assertEqual(entry["type"], "http")

    def test_no_http_entry_carries_a_command_and_no_command_entry_a_url(self):
        """The installer's marker test concatenates `command` and `url` on the strength
        of a hook having exactly one of them (SideCrab.Common.ps1, Split-SideCrab
        HookMatcher). An entry with both would still match, but the assumption would
        have stopped being true, and this is where that is noticed."""
        for event, entry in _entries(self.fragment):
            with self.subTest(event=event):
                self.assertEqual(1, ("url" in entry) + ("command" in entry))
                if entry["type"] == "http":
                    self.assertNotIn("command", entry)
                else:
                    self.assertNotIn("url", entry)

    def test_every_event_posts_to_its_own_route(self):
        for event, entry in _entries(self.fragment):
            target = entry.get("url") or entry.get("command") or ""
            with self.subTest(event=event):
                self.assertIn(event, EXPECTED_ROUTE, f"{event} has no expected route")
                self.assertIn(EXPECTED_ROUTE[event], target)

    def test_every_route_is_distinct(self):
        """Two events sharing a route is how an ingest path stops being legible in a
        capture or a log - the whole return on B1's per-event URLs."""
        routes = sorted(EXPECTED_ROUTE.values())
        self.assertEqual(len(routes), len(set(routes)))

    def test_timeouts_are_the_budgeted_ones(self):
        for event, entry in _entries(self.fragment):
            with self.subTest(event=event):
                self.assertEqual(entry["timeout"],
                                 EXPECTED_TIMEOUT.get(event, DEFAULT_TIMEOUT))

    def test_every_registered_route_exists_in_crabd(self):
        """The fragment and the dispatcher are two files that have to agree. A hook
        pointed at a path crabd does not serve is a 404 on every event, silently.

        Both dispatch shapes are read: the `path == "..."` comparisons, and the
        HOOK_INGEST_PATHS set the ingest routes share one handler through.
        """
        source = CRABD_PATH.read_text(encoding="utf-8")
        served = set(re.findall(r'path == "(/v1/hook[^"]*)"', source))
        ingest = re.search(r"HOOK_INGEST_PATHS = frozenset\(\((.*?)\)\)", source,
                           re.S)
        self.assertIsNotNone(ingest, "HOOK_INGEST_PATHS not found in crabd.py")
        served |= set(re.findall(r'"(/v1/hook[^"]*)"', ingest.group(1)))
        for event, entry in _entries(self.fragment):
            target = entry.get("url") or entry.get("command") or ""
            # The curl entry carries a whole shell line, so the URL is matched out of it
            # rather than sliced: `... /v1/hook/session-start || exit 0`.
            found = re.search(r"/v1/hook(?:/[a-z-]+)?", target)
            with self.subTest(event=event, target=target):
                self.assertIsNotNone(found)
                self.assertIn(found.group(0), served)

    def test_the_compatibility_route_is_still_served(self):
        """No entry points at the bare `/v1/hook` any more, and it must stay anyway: a
        fragment installed on another machine before v0.36.0 posts every event there."""
        source = CRABD_PATH.read_text(encoding="utf-8")
        ingest = re.search(r"HOOK_INGEST_PATHS = frozenset\(\((.*?)\)\)", source, re.S)
        self.assertIn('"/v1/hook"', ingest.group(1))


if __name__ == "__main__":
    unittest.main()
