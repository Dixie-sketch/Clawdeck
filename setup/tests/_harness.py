"""Shared loader and fakes for the macOS setup suite.

Not a test module (the discover pattern is ``test*.py``): it only loads
``setup/sidecrab_setup.py`` by path, the way ``hooks/tests`` loads the status-line
script, and hands out the fakes every test injects instead of touching the machine.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

SETUP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SETUP_DIR.parent
MODULE_PATH = SETUP_DIR / "sidecrab_setup.py"


def load():
    spec = importlib.util.spec_from_file_location("sidecrab_setup", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which is None for a module that is not there yet.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


setup = load()


class RecordingRunner:
    """Stands in for subprocess: records every argv, answers from a canned table.

    ``answers`` maps a substring of the joined argv to ``(code, out, err)``; the first
    match wins and anything unmatched is a silent success, which is what launchctl does
    for most of its verbs.
    """

    def __init__(self, answers=None):
        self.calls = []
        self.answers = list((answers or {}).items())

    def __call__(self, argv, stdin=None, timeout=None):
        self.calls.append(list(argv))
        joined = " ".join(argv)
        for needle, answer in self.answers:
            if needle in joined:
                return answer
        return (0, "", "")

    def argv_for(self, needle):
        return [c for c in self.calls if needle in " ".join(c)]


class RecordingHttp:
    """Canned HTTP. ``routes`` maps a URL suffix to (status, body) or an Exception."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, url, body=None, headers=None, timeout=None):
        self.calls.append({"url": url, "body": body, "headers": dict(headers or {})})
        for suffix, answer in self.routes.items():
            if url.endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return (404, "")


class TempHome(unittest.TestCase):
    """Every test runs against a throwaway HOME - never the developer's own."""

    FAKE_PYTHON = "/fake/bin/python3.13"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / "hooks").mkdir(parents=True)
        (self.repo / "companion").mkdir()
        (self.repo / "notifier").mkdir()
        (self.repo / "widget" / "scripts").mkdir(parents=True)
        for name in ("hooks/sidecrab_statusline.py", "companion/crabd.py"):
            (self.repo / name).write_text("", encoding="utf-8")
        # The macOS hook fragment is read from the real checkout: the merge under test is
        # the one that ships, not a copy that can drift from it.
        (self.repo / "hooks" / "settings-hooks-fragment-macos.json").write_text(
            (REPO_ROOT / "hooks" / "settings-hooks-fragment-macos.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        self.runner = RecordingRunner()
        self.printed = []
        self.clock = datetime(2026, 9, 4, 11, 22, 33)

    def env(self, **overrides):
        kwargs = dict(
            home=self.home,
            repo_root=self.repo,
            uid=501,
            user="tester",
            now=lambda: self.clock,
            run=self.runner,
            http_get=RecordingHttp(),
            http_post=RecordingHttp(),
            python_probe=lambda path: (0, "3.13\n", ""),
            python_override=self.FAKE_PYTHON,
            is_file=lambda path: True,
            sleep=lambda seconds: None,
            emit=self.printed.append,
            ask=None,
            # The common case once crabd carries the Keychain store; the two rows that
            # care about an older crabd pass False themselves.
            store_capable=lambda: True,
        )
        kwargs.update(overrides)
        return setup.Environment(**kwargs)

    def write_settings(self, doc):
        path = self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return path

    def read_settings(self):
        return json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))

    def backups(self, path):
        return sorted(p.name for p in path.parent.glob(path.name + ".sidecrab-bak-*"))

    @property
    def output(self):
        return "\n".join(self.printed)
