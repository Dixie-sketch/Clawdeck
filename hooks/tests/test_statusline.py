"""Headless unit tests for the SideCrab status-line chain script.

stdlib unittest only, no network and no Windows registry: the only I/O is a temp chain
file, a fake opener standing in for urlopen, and a real short subprocess for the chain leg.

    python -m unittest discover -s hooks/tests -v
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
SCRIPT_PATH = HOOKS_DIR / "sidecrab_statusline.py"


def _load():
    spec = importlib.util.spec_from_file_location("sidecrab_statusline", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sl = _load()


class LoadPriorCommand(unittest.TestCase):
    def _write(self, obj) -> str:
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(obj, handle)
        handle.close()
        return handle.name

    def test_reads_a_saved_command(self):
        path = self._write({"statusLine": {"type": "command", "command": "starship prompt"}})
        self.assertEqual(sl.load_prior_command(path), "starship prompt")

    def test_null_prior_reads_as_none(self):
        self.assertIsNone(sl.load_prior_command(self._write({"statusLine": None})))

    def test_non_command_type_reads_as_none(self):
        self.assertIsNone(sl.load_prior_command(self._write({"statusLine": {"type": "x", "command": "y"}})))

    def test_empty_command_reads_as_none(self):
        self.assertIsNone(sl.load_prior_command(self._write({"statusLine": {"type": "command", "command": "  "}})))

    def test_absent_file_reads_as_none(self):
        self.assertIsNone(sl.load_prior_command(str(HOOKS_DIR / "does-not-exist.json")))

    def test_malformed_json_reads_as_none(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        handle.write("{not json")
        handle.close()
        self.assertIsNone(sl.load_prior_command(handle.name))


class MinimalStatus(unittest.TestCase):
    def test_builds_from_model_and_cwd(self):
        doc = json.dumps({"model": {"display_name": "Claude Fable 5"},
                          "workspace": {"current_dir": r"C:\Dev\sidecrab"}}).encode()
        line = sl.minimal_status(doc)
        self.assertIn("Claude Fable 5", line)
        self.assertIn("sidecrab", line)

    def test_empty_object_is_just_the_badge(self):
        self.assertEqual(sl.minimal_status(b"{}"), "\U0001f980 sidecrab")

    def test_junk_is_empty_never_raises(self):
        self.assertEqual(sl.minimal_status(b"not json at all"), "")
        self.assertEqual(sl.minimal_status(b"\xff\xfe\x00"), "")


class PostStatusline(unittest.TestCase):
    def test_endpoint_is_crabd_on_9999(self):
        self.assertEqual(sl.STATUSLINE_ENDPOINT, "http://127.0.0.1:9999/v1/statusline")

    def test_carries_the_panel_header(self):
        # crabd refuses a POST without X-SideCrab-Panel with 403; without it the status line
        # would still render but crabd would lose limits and per-session context entirely.
        seen = {}

        class _Resp:
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(request, timeout=None):
            seen.update({k.lower(): v for k, v in request.headers.items()})
            return _Resp()

        sl.post_statusline(b"{}", opener=opener)
        self.assertEqual(seen.get("x-sidecrab-panel"), "1")
        self.assertEqual(seen.get("content-type"), "application/json")

    def test_posts_the_document_verbatim(self):
        seen = {}

        class _Resp:
            def read(self): return b""
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def opener(request, timeout=None):
            seen["data"] = request.data
            seen["method"] = request.get_method()
            seen["ctype"] = request.headers.get("Content-type")
            return _Resp()

        sl.post_statusline(b'{"session_id":"abc"}', opener=opener)
        self.assertEqual(seen["data"], b'{"session_id":"abc"}')
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["ctype"], "application/json")

    def test_never_raises_when_crabd_is_down(self):
        def opener(request, timeout=None):
            raise OSError("connection refused")
        # Must not raise - a stopped crabd cannot break the status line.
        sl.post_statusline(b"{}", opener=opener)


class RunChained(unittest.TestCase):
    def test_passes_stdin_and_returns_stdout(self):
        out = sl.run_chained(
            f'"{sys.executable}" -c "import sys;sys.stdout.write(sys.stdin.read())"',
            b"hello-from-sidecrab",
        )
        self.assertEqual(out, "hello-from-sidecrab")

    def test_command_that_raises_returns_none(self):
        # When the chained command cannot be run at all (spawn error, timeout), run_chained
        # returns None so main() falls through to the minimal line instead of crashing.
        with mock.patch.object(sl.subprocess, "run", side_effect=OSError("boom")):
            self.assertIsNone(sl.run_chained("whatever", b"x"))

    def test_empty_output_command_returns_that_empty_string(self):
        # A prior command that prints nothing is honoured as printing nothing (not treated as
        # broken): its empty stdout comes back as "", not None. In the real flow an empty or
        # whitespace prior never reaches here - load_prior_command rejects it first.
        out = sl.run_chained(f'"{sys.executable}" -c "pass"', b"x")
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
