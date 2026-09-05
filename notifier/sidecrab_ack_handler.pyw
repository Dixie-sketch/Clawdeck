"""SideCrab ack handler — the target of the ``sidecrab-ack:`` protocol.

Windows launches this when the "Acknowledge" button on a SideCrab toast is pressed::

    pythonw sidecrab_ack_handler.pyw "sidecrab-ack:<sessionId>"

registered by ``setup\\Register-SideCrabProtocol.ps1`` at
``HKCU\\SOFTWARE\\Classes\\sidecrab-ack\\shell\\open\\command``. It POSTs
``{"sessionId": ..., "action": "ack"}`` to crabd's ``/v1/action`` and exits.

THE URI IS DATA, NOT A COMMAND. It arrives from the shell, which got it from a toast
payload — a chain SideCrab writes but does not own once it is in Action Center, where it
outlives the process that created it and can be replayed at any time. So the session id is
matched against ``^[A-Za-z0-9-]{1,64}$`` BEFORE it reaches a URL, a log line or a JSON body.
Nothing else about the argument is trusted, and nothing is ever executed from it.

SILENT BY CONSTRUCTION. A protocol handler has no console (that is what .pyw and pythonw
buy) and must never raise a dialog: a traceback window appearing because crabd happens to
be stopped is a worse outcome than the ack simply not landing. Every failure — bad URI,
crabd down, 404, timeout — ends as one line in ~/.sidecrab/logs/ack-handler.log and a
non-zero exit code that only a deliberate command-line invocation can see.

It writes its OWN log file rather than sharing notifier.log: that one is held open by the
long-running SideCrab-toast task through a RotatingFileHandler, and a second process
rotating it underneath would fail the rename on Windows.

Zero dependencies beyond the stdlib, and no import of sidecrab_toast: this runs from a
shell association, where the fewer things that can be missing the better. The two constants
it shares with the notifier are pinned to each other by notifier/tests/test_ack_handler.py.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: The registered scheme. The SCHEME TOKEN ONLY is matched case-insensitively (F4): RFC 3986
#: schemes are case-insensitive and Windows resolves the registry key that way, so the shell
#: may hand back `SIDECRAB-ACK:<id>` for a key we wrote lowercase — and a case-sensitive
#: startswith() dropped that ack with a length-only log line, i.e. silently. Absorbing the
#: case here widens NOTHING after the colon: the payload still has to pass SESSION_ID_PATTERN.
ACK_SCHEME = "sidecrab-ack"

#: re.ASCII is load-bearing, not decoration: without it re.IGNORECASE folds Unicode too, and
#: U+212A KELVIN SIGN lowercases to "k" — so `sidecrab-ac<KELVIN>:` would be accepted as our
#: scheme. Only the 26 ASCII letters may vary.
_ACK_SCHEME_RE = re.compile(re.escape(ACK_SCHEME) + ":", re.IGNORECASE | re.ASCII)

#: The only session ids this handler will act on. Deliberately narrower than "any string":
#: crabd's ids are UUIDs, and this charset cannot carry a path separator, a scheme, a quote
#: or a percent-escape into the request that follows.
SESSION_ID_PATTERN = r"^[A-Za-z0-9-]{1,64}$"
_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)

ACTION_ENDPOINT = "http://127.0.0.1:9999/v1/action"
POST_TIMEOUT_SEC = 5.0

#: crabd 0.31.0 and later refuses any POST that does not carry this header, with 403
#: "panel header required". Any non-empty value passes; "1" is what every SideCrab client
#: sends. An older crabd ignores it, so sending it always is safe in both directions.
PANEL_HEADER = "X-SideCrab-Panel"

LOG_PATH = Path.home() / ".sidecrab" / "logs" / "ack-handler.log"
LOG_MAX_BYTES = 256_000

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_URI = 2
EXIT_UNEXPECTED = 3


# --------------------------------------------------------------------------------------
# Pure: parsing. No I/O, no clock, no network.
# --------------------------------------------------------------------------------------


def is_valid_session_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_SESSION_ID_RE.match(value))


def parse_ack_uri(argument: Any) -> str | None:
    """``sidecrab-ack:<sessionId>`` → the session id. Anything else → None, never an error.

    Tolerates exactly three things and no more: surrounding whitespace, ONE trailing
    slash, and the CASE OF THE SCHEME. The slash is the shell's, not ours — an opaque URI is
    normally handed back verbatim (the mailto: precedent), but a shell that decides to
    normalise it appends a separator, and losing every ack to that would be silent. The case
    is the shell's too (F4). The charset test below is applied to what remains in every case,
    so none of the three widens what can reach the POST.
    """
    if not isinstance(argument, str):
        return None
    text = argument.strip()
    match = _ACK_SCHEME_RE.match(text)
    if match is None:
        return None
    rest = text[match.end() :]
    if rest.endswith("/"):
        rest = rest[:-1]
    return rest if _SESSION_ID_RE.match(rest) else None


# --------------------------------------------------------------------------------------
# Impure: log and POST
# --------------------------------------------------------------------------------------


def log_line(message: str, log_path: Path | None = None) -> None:
    """One line, best effort. NEVER raises — a handler that dies logging is worse than one
    that acks without a trace.

    The default is resolved at CALL time, not bound at def time, so a test can redirect it
    without the module's own log becoming test output.
    """
    log_path = log_path or LOG_PATH
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if log_path.stat().st_size > LOG_MAX_BYTES:
                log_path.replace(log_path.with_name(log_path.name + ".old"))
        except OSError:
            pass  # absent file, or another handler mid-rotation: neither is worth failing on
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except OSError:
        pass


def post_ack(
    session_id: str,
    endpoint: str = ACTION_ENDPOINT,
    timeout: float = POST_TIMEOUT_SEC,
    opener: Any = None,
) -> int:
    """POST the ack. Returns the HTTP status (204 on success). Raises on any failure —
    ``main`` is the one place that decides failures are silent."""
    body = json.dumps({"sessionId": session_id, "action": "ack"}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", PANEL_HEADER: "1"},
    )
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=timeout) as resp:  # noqa: S310 - fixed localhost endpoint
        return int(getattr(resp, "status", 0) or 0)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        log_line(f"refused: no argument (expected {ACK_SCHEME}:<sessionId>)")
        return EXIT_BAD_URI

    session_id = parse_ack_uri(argv[0])
    if session_id is None:
        # The rejected URI is NOT echoed: it is unvalidated shell input, and a log file is
        # read by humans and by greps. Its length is enough to tell a truncation from junk.
        log_line(f"refused: argument ({len(argv[0])} chars) is not {ACK_SCHEME}:{SESSION_ID_PATTERN}")
        return EXIT_BAD_URI

    try:
        status = post_ack(session_id)
    except urllib.error.HTTPError as exc:  # subclass of URLError - must be caught first
        log_line(f"ack {session_id}: HTTP {exc.code}")
        return EXIT_FAILED
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log_line(f"ack {session_id}: {type(exc).__name__}: {exc}")
        return EXIT_FAILED

    log_line(f"ack {session_id}: {status}")
    return EXIT_OK


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001 - no traceback may ever reach a user's screen
        code = EXIT_UNEXPECTED
    raise SystemExit(code)
