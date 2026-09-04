"""Shared loader and fakes for the macOS setup suite.

Not a test module (the discover pattern is ``test*.py``): it only loads
``setup/sidecrab_setup.py`` by path, the way ``hooks/tests`` loads the status-line
script, and hands out the fakes every test injects instead of touching the machine.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
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

__all__ = ["SETUP_DIR", "REPO_ROOT", "MODULE_PATH", "load", "setup", "TempHome"]


class TempHome(unittest.TestCase):
    """Every test runs against a throwaway HOME - never the developer's own."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / "hooks").mkdir(parents=True)
        (self.repo / "companion").mkdir()
        (self.repo / "notifier").mkdir()
