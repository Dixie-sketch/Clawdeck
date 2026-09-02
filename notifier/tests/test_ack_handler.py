"""Headless unit tests for the sidecrab-ack protocol handler.

stdlib unittest only, and no Windows: the registry is the setup lane's, and the only I/O
here is a temp log file and a fake opener standing in for urlopen.

The handler is a .pyw (it is launched by pythonw, which is what gives it no console), so it
is loaded by path rather than imported by name.

    python -m unittest discover -s notifier/tests -v
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

NOTIFIER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = NOTIFIER_DIR.parent
HANDLER_PATH = NOTIFIER_DIR / "sidecrab_ack_handler.pyw"

sys.path.insert(0, str(NOTIFIER_DIR))

import sidecrab_toast  # noqa: E402


def _load_handler():
    """.pyw is not on every platform's SOURCE_SUFFIXES, so the loader is named explicitly
    rather than inferred from the extension."""
    loader = importlib.machinery.SourceFileLoader("sidecrab_ack_handler", str(HANDLER_PATH))
    spec = importlib.util.spec_from_file_location("sidecrab_ack_handler", HANDLER_PATH, loader=loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


handler = _load_handler()

VALID_ID = "0f9c1e2a-7b34-4d51-9c8e-2a1b3c4d5e6f"


class FakeResponse:
    def __init__(self, status: int = 204) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ParseTests(unittest.TestCase):
    """The whole security boundary of this component is one regex. Exercise it as such."""

    def test_accepts_a_crabd_session_id(self) -> None:
        self.assertEqual(handler.parse_ack_uri(f"sidecrab-ack:{VALID_ID}"), VALID_ID)

    def test_accepts_a_single_character_id(self) -> None:
        self.assertEqual(handler.parse_ack_uri("sidecrab-ack:a"), "a")

    def test_accepts_exactly_64_characters(self) -> None:
        sid = "a" * 64
        self.assertEqual(handler.parse_ack_uri(f"sidecrab-ack:{sid}"), sid)

    def test_refuses_65_characters(self) -> None:
        self.assertIsNone(handler.parse_ack_uri("sidecrab-ack:" + "a" * 65))

    def test_refuses_an_empty_session_id(self) -> None:
        self.assertIsNone(handler.parse_ack_uri("sidecrab-ack:"))

    def test_refuses_a_wrong_scheme(self) -> None:
        for uri in (
            "sidecrab-acks:abc",
            "sidecrab_ack:abc",
            "sidecraback:abc",
            "xsidecrab-ack:abc",
            "http://127.0.0.1:2722/v1/action",
            "file:///C:/Windows/System32/calc.exe",
            "sidecrab-ack",
            "abc",
            "",
        ):
            with self.subTest(uri=uri):
                self.assertIsNone(handler.parse_ack_uri(uri))

    def test_scheme_match_is_case_insensitive(self) -> None:
        """F4, reversing the earlier call. URI schemes are case-insensitive by RFC 3986 and
        Windows resolves the handler key that way, so the shell can hand back a case we never
        wrote — and the old exact match refused it with a length-only log line, i.e. lost the
        operator's click silently. Only the SCHEME TOKEN folds."""
        for uri in ("SideCrab-Ack:abc", "SIDECRAB-ACK:abc", "sidecrab-ACK:abc", "SIDECRAB-ack:abc"):
            with self.subTest(uri=uri):
                self.assertEqual(handler.parse_ack_uri(uri), "abc")

    def test_the_case_fold_is_ascii_only(self) -> None:
        """re.ASCII on the scheme regex. Full-Unicode IGNORECASE lowercases U+212A KELVIN SIGN
        to "k", so `sidecrab-ac<KELVIN>:` would pass as our scheme — a homoglyph is not a case
        difference, and the shell will never produce one for a key we registered in ASCII."""
        self.assertIsNone(handler.parse_ack_uri("sidecrab-acK:abc"))
        self.assertIsNone(handler.parse_ack_uri("sİdecrab-ack:abc"))  # U+0130 -> "i" + dot

    def test_the_payload_stays_strict_whatever_the_scheme_case(self) -> None:
        """The security story: the fold buys the SCHEME latitude and nothing after the colon.
        Every charset refusal must hold identically under an uppercase scheme."""
        for tail in ("../../windows", "a b", "a;calc.exe", "a'b", 'a"b', "a\x00b", "a%2fb", "a" * 65):
            with self.subTest(tail=tail):
                self.assertIsNone(handler.parse_ack_uri(f"SIDECRAB-ACK:{tail}"))

    def test_refuses_junk_after_a_valid_scheme(self) -> None:
        for tail in (
            "../../windows/system32",
            "a b",
            "a/b",
            "a\\b",
            "a;calc.exe",
            "a&b",
            "a'b",
            'a"b',
            "a\nb",
            "a\tb",
            "a%20b",
            "a.b",
            "a_b",
            "a:b",
            "cráb",
            "abc\x00def",
        ):
            with self.subTest(tail=tail):
                self.assertIsNone(handler.parse_ack_uri(f"sidecrab-ack:{tail}"))

    def test_tolerates_one_shell_appended_slash(self) -> None:
        """Some shells normalise an opaque URI by appending a separator. Losing every ack
        to that would be silent; the charset test still runs on what remains."""
        self.assertEqual(handler.parse_ack_uri(f"sidecrab-ack:{VALID_ID}/"), VALID_ID)

    def test_does_not_tolerate_two_slashes_or_a_path(self) -> None:
        for uri in ("sidecrab-ack:abc//", "sidecrab-ack:abc/def/", "sidecrab-ack://abc"):
            with self.subTest(uri=uri):
                self.assertIsNone(handler.parse_ack_uri(uri))

    def test_tolerates_surrounding_whitespace(self) -> None:
        self.assertEqual(handler.parse_ack_uri(f"  sidecrab-ack:{VALID_ID}\r\n"), VALID_ID)

    def test_non_string_arguments_are_refused_not_raised(self) -> None:
        for value in (None, 42, [], {}, b"sidecrab-ack:abc"):
            with self.subTest(value=value):
                self.assertIsNone(handler.parse_ack_uri(value))

    def test_is_valid_session_id_agrees_with_the_parser(self) -> None:
        self.assertTrue(handler.is_valid_session_id(VALID_ID))
        for bad in ("", "a" * 65, "a b", None, 1):
            with self.subTest(value=bad):
                self.assertFalse(handler.is_valid_session_id(bad))


class PostTests(unittest.TestCase):
    def test_posts_the_contract_body_to_the_action_endpoint(self) -> None:
        seen: list = []

        def opener(request, timeout=None):
            seen.append((request, timeout))
            return FakeResponse(204)

        status = handler.post_ack(VALID_ID, opener=opener)
        request, timeout = seen[0]
        self.assertEqual(status, 204)
        self.assertEqual(request.full_url, "http://127.0.0.1:2722/v1/action")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.headers.get("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"sessionId": VALID_ID, "action": "ack"})
        self.assertEqual(timeout, 5.0)

    def test_timeout_is_five_seconds(self) -> None:
        self.assertEqual(handler.POST_TIMEOUT_SEC, 5.0)


class MainTests(unittest.TestCase):
    """main() is the only place that decides failures are silent, so every branch is here."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "logs" / "ack-handler.log"
        patcher = mock.patch.object(handler, "LOG_PATH", self.log)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_a_valid_uri_acks_and_exits_zero(self) -> None:
        with mock.patch.object(handler, "post_ack", return_value=204) as posted:
            code = handler.main([f"sidecrab-ack:{VALID_ID}"])
        self.assertEqual(code, handler.EXIT_OK)
        posted.assert_called_once_with(VALID_ID)
        self.assertIn(VALID_ID, self.log_text())

    def test_no_argument_never_posts(self) -> None:
        with mock.patch.object(handler, "post_ack") as posted:
            code = handler.main([])
        self.assertEqual(code, handler.EXIT_BAD_URI)
        posted.assert_not_called()

    def test_a_refused_uri_never_posts(self) -> None:
        for uri in ("sidecrab-ack:", "http://evil.example/x", "sidecrab-ack:a b", "sidecrab-ack:" + "a" * 65):
            with self.subTest(uri=uri), mock.patch.object(handler, "post_ack") as posted:
                self.assertEqual(handler.main([uri]), handler.EXIT_BAD_URI)
                posted.assert_not_called()

    def test_a_refused_uri_is_never_echoed_into_the_log(self) -> None:
        """The log is read by humans and by greps; unvalidated shell input does not belong
        in it. Its LENGTH distinguishes a truncation from junk, which is all that is owed."""
        hostile = "sidecrab-ack:" + "DROP-EVERYTHING\n<script>alert(1)</script>"
        with mock.patch.object(handler, "post_ack"):
            handler.main([hostile])
        text = self.log_text()
        self.assertNotIn("DROP-EVERYTHING", text)
        self.assertNotIn("<script>", text)
        self.assertIn(str(len(hostile)), text)

    def test_crabd_being_down_is_silent_and_non_zero(self) -> None:
        with mock.patch.object(handler, "post_ack", side_effect=urllib.error.URLError("refused")):
            code = handler.main([f"sidecrab-ack:{VALID_ID}"])
        self.assertEqual(code, handler.EXIT_FAILED)
        self.assertIn("URLError", self.log_text())

    def test_an_unknown_session_404s_quietly(self) -> None:
        err = urllib.error.HTTPError("http://127.0.0.1:2722/v1/action", 404, "unknown session", {}, None)
        with mock.patch.object(handler, "post_ack", side_effect=err):
            code = handler.main([f"sidecrab-ack:{VALID_ID}"])
        self.assertEqual(code, handler.EXIT_FAILED)
        self.assertIn("HTTP 404", self.log_text())

    def test_a_timeout_is_silent_and_non_zero(self) -> None:
        with mock.patch.object(handler, "post_ack", side_effect=TimeoutError("timed out")):
            self.assertEqual(handler.main([f"sidecrab-ack:{VALID_ID}"]), handler.EXIT_FAILED)

    def test_nothing_is_printed_on_any_path(self) -> None:
        """A protocol handler under pythonw has nowhere to print, but a stray print here
        would also be a stray console window under python.exe."""
        cases = [("sidecrab-ack:nope nope", None), (f"sidecrab-ack:{VALID_ID}", 204)]
        for uri, result in cases:
            with self.subTest(uri=uri):
                with mock.patch.object(handler, "post_ack", return_value=result), \
                     mock.patch("sys.stdout") as out, mock.patch("sys.stderr") as err:
                    handler.main([uri])
                out.write.assert_not_called()
                err.write.assert_not_called()


class LogTests(unittest.TestCase):
    def test_an_unwritable_log_path_never_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A file where a directory has to go: mkdir(parents=True) fails, and the whole
            # point is that the handler carries on anyway.
            blocker = Path(tmp) / "logs"
            blocker.write_text("not a directory", encoding="utf-8")
            handler.log_line("x", blocker / "ack-handler.log")

    def test_rotates_at_the_cap_and_keeps_one_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ack-handler.log"
            path.write_text("x" * (handler.LOG_MAX_BYTES + 1), encoding="utf-8")
            handler.log_line("after rotation", path)
            self.assertTrue(path.with_name("ack-handler.log.old").exists())
            self.assertIn("after rotation", path.read_text(encoding="utf-8"))

    def test_writes_its_own_file_not_the_notifier_daemons(self) -> None:
        """notifier.log is held open by the SideCrab-toast task through a rotating handler;
        a second process renaming it underneath fails on Windows."""
        self.assertNotEqual(handler.LOG_PATH.name, "notifier.log")
        self.assertEqual(handler.LOG_PATH.parent, sidecrab_toast.LOG_PATH.parent)


class ContractTests(unittest.TestCase):
    """Three files have to agree on two strings, and no single one of them can tell."""

    def test_the_scheme_matches_the_notifier(self) -> None:
        self.assertEqual(handler.ACK_SCHEME, sidecrab_toast.ACK_SCHEME)

    def test_the_session_id_pattern_matches_the_notifier(self) -> None:
        self.assertEqual(handler.SESSION_ID_PATTERN, sidecrab_toast.SESSION_ID_PATTERN)
        self.assertEqual(handler.SESSION_ID_PATTERN, r"^[A-Za-z0-9-]{1,64}$")

    def test_the_setup_script_registers_this_scheme_and_this_handler(self) -> None:
        common = (REPO_ROOT / "setup" / "SideCrab.Common.ps1").read_text(encoding="utf-8")
        self.assertIn(f"'{handler.ACK_SCHEME}'", common)
        self.assertIn(f"HKCU:\\SOFTWARE\\Classes\\{handler.ACK_SCHEME}", common)
        self.assertIn("sidecrab_ack_handler.pyw", common)

    def test_the_endpoint_is_the_contract_action_endpoint(self) -> None:
        contract = (REPO_ROOT / "docs" / "STATE-CONTRACT.md").read_text(encoding="utf-8")
        self.assertIn("/v1/action", contract)
        self.assertEqual(handler.ACTION_ENDPOINT, "http://127.0.0.1:2722/v1/action")

    def test_the_contract_declares_this_scheme_and_charset(self) -> None:
        contract = (REPO_ROOT / "docs" / "STATE-CONTRACT.md").read_text(encoding="utf-8")
        self.assertIn("sidecrab-ack:", contract)
        self.assertIn("^[A-Za-z0-9-]{1,64}$", contract)


if __name__ == "__main__":
    unittest.main(verbosity=2)
