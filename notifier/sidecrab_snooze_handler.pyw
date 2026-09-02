"""SideCrab snooze handler — the target of the ``sidecrab-snooze:`` protocol.

Windows launches this when the "Snooze 30m" button on a SideCrab toast is pressed::

    pythonw sidecrab_snooze_handler.pyw "sidecrab-snooze:<sessionId>"

registered by ``setup\\Register-SideCrabProtocol.ps1`` at
``HKCU\\SOFTWARE\\Classes\\sidecrab-snooze\\shell\\open\\command``. It writes a 30-minute
mark into the notifier's own state file and exits.

WHAT IT DOES NOT DO IS THE POINT: it never touches crabd. The ack handler POSTs
``{"action": "ack"}`` to ``/v1/action``; this one deliberately does not, because a snooze is
a statement about NOTIFICATIONS and an ack is a statement about the QUESTION. Acking here
would clear the widget's dot and the panel would stop showing a session that is still,
truthfully, waiting for an answer nobody has given. Snoozing a notification must never look
like answering a question — so the only thing that changes is when the operator is told next.

THE URI IS DATA, NOT A COMMAND. It arrives from the shell, which got it from a toast payload
— a chain SideCrab writes but does not own once it is in Action Center, where it outlives the
process that created it and can be replayed at any time. So the session id is matched against
``^[A-Za-z0-9-]{1,64}$`` BEFORE it reaches a file, a log line or a JSON key. Nothing else
about the argument is trusted, and nothing is ever executed from it. The charset is also what
makes the mark safe as a JSON KEY in a document the notifier re-reads every poll.

SILENT BY CONSTRUCTION. A protocol handler has no console (that is what .pyw and pythonw buy)
and must never raise a dialog: a traceback window appearing because the state file happened to
be locked is a worse outcome than the snooze simply not landing. Every failure — bad URI,
unwritable file, corrupt document — ends as one line in ~/.sidecrab/logs/snooze-handler.log
and a non-zero exit code that only a deliberate command-line invocation can see.

It writes its OWN log file rather than sharing notifier.log: that one is held open by the
long-running SideCrab-toast task through a RotatingFileHandler, and a second process rotating
it underneath would fail the rename on Windows.

IT IS THE ONLY WRITER OF THE ``snooze`` SECTION. The notifier reads that key and never writes
it; every writer of the file replaces only its own top-level key. That is what makes a ledger
shared by two processes safe without a lock — and why the temp file below carries this PID.

Zero dependencies beyond the stdlib, and no import of sidecrab_toast: this runs from a shell
association, where the fewer things that can be missing the better. The constants it shares
with the notifier are pinned to each other by notifier/tests/test_snooze.py.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: The registered scheme. The SCHEME TOKEN ONLY is matched case-insensitively (F4): RFC 3986
#: schemes are case-insensitive and Windows resolves the registry key that way, so the shell
#: may hand back `SIDECRAB-SNOOZE:<id>` for a key we wrote lowercase — and a case-sensitive
#: startswith() dropped that snooze with a length-only log line, i.e. silently. Absorbing the
#: case here widens NOTHING after the colon: the payload still has to pass SESSION_ID_PATTERN.
SNOOZE_SCHEME = "sidecrab-snooze"

#: re.ASCII is load-bearing, not decoration: without it re.IGNORECASE folds Unicode too, and
#: U+212A KELVIN SIGN lowercases to "k" — so a homoglyph scheme would be accepted as ours.
#: Only the 26 ASCII letters may vary.
_SNOOZE_SCHEME_RE = re.compile(re.escape(SNOOZE_SCHEME) + ":", re.IGNORECASE | re.ASCII)

#: The only session ids this handler will act on. Deliberately narrower than "any string":
#: crabd's ids are UUIDs, and this charset cannot carry a path separator, a scheme, a quote or
#: a percent-escape into the JSON key that follows.
SESSION_ID_PATTERN = r"^[A-Za-z0-9-]{1,64}$"
_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)

#: How long the mark holds. Must equal SNOOZE_SEC in sidecrab_toast.py, which is what the
#: BUTTON says out loud; a test pins the pair.
SNOOZE_SEC = 1800

STATE_PATH = Path.home() / ".sidecrab" / "toast-state.json"
SNOOZE_SECTION = "snooze"

#: Ceiling on stored marks, matching the notifier's SNOOZE_MAP_CAP. Expired entries are pruned
#: on every write, so this is the backstop rather than the mechanism: it bounds a file the
#: notifier re-reads whenever it changes.
SNOOZE_MAP_CAP = 64

LOG_PATH = Path.home() / ".sidecrab" / "logs" / "snooze-handler.log"
LOG_MAX_BYTES = 256_000

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_BAD_URI = 2
EXIT_UNEXPECTED = 3


# --------------------------------------------------------------------------------------
# Pure: parsing and the new document. No I/O, no clock of its own.
# --------------------------------------------------------------------------------------


def is_valid_session_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_SESSION_ID_RE.match(value))


def parse_snooze_uri(argument: Any) -> str | None:
    """``sidecrab-snooze:<sessionId>`` → the session id. Anything else → None, never an error.

    Tolerates exactly three things and no more: surrounding whitespace, ONE trailing slash,
    and the CASE OF THE SCHEME. The slash is the shell's, not ours — an opaque URI is normally
    handed back verbatim (the mailto: precedent), but a shell that decides to normalise it
    appends a separator, and losing every snooze to that would be silent. The case is the
    shell's too (F4). The charset test below is applied to what remains in every case, so none
    of the three widens what can reach the state file.
    """
    if not isinstance(argument, str):
        return None
    text = argument.strip()
    match = _SNOOZE_SCHEME_RE.match(text)
    if match is None:
        return None
    rest = text[match.end() :]
    if rest.endswith("/"):
        rest = rest[:-1]
    return rest if _SESSION_ID_RE.match(rest) else None


def apply_snooze(doc: Any, session_id: str, until: datetime, now: datetime) -> dict:
    """Pure: the state document + one new mark → the document to write.

    Rebuilt from what is READABLE, not merged blindly: this file is on disk, the notifier
    parses whatever comes out of it every poll, and an entry with a junk key or an unparseable
    instant would either be silently ignored forever or — worse — read as a snooze nobody set.
    Expired marks go at the same time, which is what keeps the map bounded in practice.

    Every other top-level key is preserved untouched. The digest day, the budget day and the
    runtime stamp live in this same file and belong to the notifier process.
    """
    out = dict(doc) if isinstance(doc, dict) else {}
    existing = out.get(SNOOZE_SECTION)
    kept: dict[str, str] = {}
    if isinstance(existing, dict):
        for sid, raw in existing.items():
            if sid == session_id or not is_valid_session_id(sid) or not isinstance(raw, str):
                continue
            expiry = _parse_iso(raw)
            if expiry is not None and expiry > now:
                kept[sid] = raw

    # Oldest-expiring first, so the cap sheds the marks closest to running out anyway. The new
    # mark is appended last and is therefore never the one dropped — the operator's most recent
    # press must be the one that takes effect.
    ordered = sorted(kept.items(), key=lambda kv: kv[1])[-(SNOOZE_MAP_CAP - 1) :]
    out[SNOOZE_SECTION] = dict(ordered) | {session_id: until.isoformat()}
    return out


def _parse_iso(value: Any) -> datetime | None:
    """ISO-8601 (incl. trailing Z) → aware UTC datetime. Unparseable → None, never raises."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------------------
# Impure: log and write
# --------------------------------------------------------------------------------------


def log_line(message: str, log_path: Path | None = None) -> None:
    """One line, best effort. NEVER raises — a handler that dies logging is worse than one
    that snoozes without a trace.

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


def write_snooze(
    session_id: str,
    until: datetime,
    now: datetime,
    state_path: Path | None = None,
) -> None:
    """Persist the mark. Raises on any failure — ``main`` is the one place that decides
    failures are silent.

    Read-modify-write onto a PID-suffixed temp, then os.replace (atomic on Windows too). The
    PID matters: the notifier writes this same document for the digest day, the budget day and
    the runtime stamp, and a shared temp name would let the two processes interleave into one
    file and then rename a hybrid of both into place.
    """
    state_path = state_path or STATE_PATH
    try:
        doc = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing is the ordinary first-snooze case; corrupt is rarer and handled the same
        # way — a snooze the operator just asked for beats preserving an unreadable document.
        doc = {}

    payload = json.dumps(apply_snooze(doc, session_id, until, now), indent=2)
    tmp = state_path.with_suffix(f"{state_path.suffix}.{os.getpid()}.tmp")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, state_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        log_line(f"refused: no argument (expected {SNOOZE_SCHEME}:<sessionId>)")
        return EXIT_BAD_URI

    session_id = parse_snooze_uri(argv[0])
    if session_id is None:
        # The rejected URI is NOT echoed: it is unvalidated shell input, and a log file is
        # read by humans and by greps. Its length is enough to tell a truncation from junk.
        log_line(f"refused: argument ({len(argv[0])} chars) is not {SNOOZE_SCHEME}:{SESSION_ID_PATTERN}")
        return EXIT_BAD_URI

    now = datetime.now(timezone.utc)
    until = now + timedelta(seconds=SNOOZE_SEC)
    try:
        write_snooze(session_id, until, now)
    except (OSError, ValueError) as exc:
        log_line(f"snooze {session_id}: {type(exc).__name__}: {exc}")
        return EXIT_FAILED

    log_line(f"snooze {session_id}: until {until.isoformat()}")
    return EXIT_OK


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001 - no traceback may ever reach a user's screen
        code = EXIT_UNEXPECTED
    raise SystemExit(code)
