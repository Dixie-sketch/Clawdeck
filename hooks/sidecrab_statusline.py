"""SideCrab statusLine ingest — the chained status line command.

Claude Code runs this on every status-line refresh, piping the official status-line
document to stdin and rendering whatever this prints to stdout. Wired by
``setup\\Install-SideCrab.ps1`` into ``~/.claude/settings.json``::

    "statusLine": { "type": "command",
                    "command": "\\"<python.exe>\\" \\"<repo>\\hooks\\sidecrab_statusline.py\\"" }

Two jobs, in this order, and the SECOND must never be starved by the first:

1. POST the stdin document verbatim to crabd's ``/v1/statusline`` (fire-and-forget,
   sub-half-second timeout). crabd prefers this over the OAuth reach-around for limits +
   per-session context (docs\\STATE-CONTRACT.md, "v0.12.0 additions" item 1). A stopped or
   slow crabd must NEVER delay or break the prompt, so the POST is wrapped and capped hard.

2. CHAIN: if the installer saved a pre-existing status-line command (because the operator
   already had one before SideCrab took the slot), run it with the SAME stdin and pass its
   stdout straight through — the user keeps the status line they had. If none was saved,
   print a minimal SideCrab line built from the document, or nothing.

VERIFIED AGAINST THE SHIPPED CLAUDE CODE (claude.exe v2.1.246, 2026-08-26):
  - The status-line config accepts ONLY ``type:"command"`` (a ``type`` other than
    "command" is ignored); optional ``padding``/``refreshInterval`` are honoured.
  - The command receives a JSON document on stdin whose top-level keys include
    ``session_id``, ``model:{id,display_name}``, ``workspace:{current_dir,project_dir,
    added_dirs,...}``, ``version`` and cost/limit blocks. This script does not depend on
    any single field — it forwards the bytes to crabd untouched and only reads
    ``model``/``workspace`` for the minimal fallback line.
  Source: docs.claude.com/en/docs/claude-code/hooks (status line) and the shipped binary
  (``statusLine:{type:literal("command"),command,padding?,refreshInterval?}`` schema).

Zero dependencies beyond the stdlib. Silent by construction: any failure degrades to the
next tier (chain -> minimal -> nothing); a status-line command that raised would smear a
traceback across the terminal on every refresh.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from typing import Any

#: crabd's status-line ingest. Fire-and-forget: crabd answers 204 and never sends a body
#: this cares about. Fixed localhost endpoint, same as every other SideCrab client.
STATUSLINE_ENDPOINT = "http://127.0.0.1:9999/v1/statusline"

#: crabd refuses any POST that does not carry this header with 403 "panel header required".
#: Any non-empty value passes; "1" is what every SideCrab client sends.
PANEL_HEADER = "X-SideCrab-Panel"

#: Hard cap on the POST. The status line re-renders often and blocks the prompt while this
#: process runs, so a stopped-but-listening crabd (or a firewall black-hole) must not cost
#: the user more than this. A refused connection fails in microseconds; this only bounds the
#: pathological "accepts then hangs" case. Kept well under the half-second the contract asks.
POST_TIMEOUT_SEC = 0.4

#: Where the installer parks the operator's prior status-line command so this can chain to
#: it. Absent file, or ``{"statusLine": null}``, both mean "there was nothing before us".
CHAIN_PATH = os.path.join(os.path.expanduser("~"), ".sidecrab", "statusline-chain.json")

#: The chained command inherits this cap. Whatever the operator had was already trusted by
#: Claude Code with its own status-line timeout; this is only a backstop against a hang.
CHAIN_TIMEOUT_SEC = 5.0


# --------------------------------------------------------------------------------------
# Pure: no I/O, no network, no clock.
# --------------------------------------------------------------------------------------


def load_prior_command(chain_path: str) -> str | None:
    """The saved prior status-line command string, or None when there is none.

    Reads the installer-written ``{"statusLine": {"type":"command","command":"..."}}``.
    Anything malformed, a ``statusLine`` of ``null``, a non-command type, or an empty
    command all read as "no prior" — never an error, because this runs on every refresh.
    """
    try:
        with open(chain_path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    status_line = doc.get("statusLine")
    if not isinstance(status_line, dict) or status_line.get("type") != "command":
        return None
    command = status_line.get("command")
    if isinstance(command, str) and command.strip():
        return command
    return None


def minimal_status(document: bytes) -> str:
    """A small, honest SideCrab line from the status-line document, or "" on any doubt.

    Built only from fields the shipped binary is known to emit
    (``model.display_name``, ``workspace.current_dir``); a missing field drops that part
    rather than guessing. Returns no trailing newline — the caller owns output framing.
    """
    try:
        doc = json.loads(document.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return ""
    if not isinstance(doc, dict):
        return ""

    parts: list[str] = ["\U0001f980 sidecrab"]  # 🦀

    model = doc.get("model")
    if isinstance(model, dict):
        name = model.get("display_name") or model.get("id")
        if isinstance(name, str) and name.strip():
            parts.append(name.strip())

    workspace = doc.get("workspace")
    if isinstance(workspace, dict):
        cwd = workspace.get("current_dir")
        if isinstance(cwd, str) and cwd.strip():
            tail = os.path.basename(cwd.rstrip("\\/")) or cwd
            parts.append(tail)

    return " · ".join(parts)  # " · " between parts


# --------------------------------------------------------------------------------------
# Impure: POST and chain.
# --------------------------------------------------------------------------------------


def post_statusline(
    document: bytes,
    endpoint: str = STATUSLINE_ENDPOINT,
    timeout: float = POST_TIMEOUT_SEC,
    opener: Any = None,
) -> None:
    """Fire-and-forget POST of the document to crabd. NEVER raises: a status-line command
    that fails because crabd is stopped must still render the status line."""
    try:
        request = urllib.request.Request(
            endpoint,
            data=document,
            method="POST",
            headers={"Content-Type": "application/json", PANEL_HEADER: "1"},
        )
        open_url = opener or urllib.request.urlopen
        with open_url(request, timeout=timeout) as resp:  # noqa: S310 - fixed localhost
            resp.read()  # drain and close; body is ignored
    except Exception:  # noqa: BLE001 - every failure is non-fatal by design
        pass


def run_chained(command: str, document: bytes, timeout: float = CHAIN_TIMEOUT_SEC) -> str | None:
    """Run the prior status-line command with the SAME stdin and return its stdout, or None
    if it could not be run. ``shell=True`` matches how Claude Code invokes a status-line
    command (a shell string), so a ``.ps1``/node/other command that worked before still
    works when chained through here."""
    try:
        result = subprocess.run(  # noqa: S602 - operator's own trusted command, as before
            command,
            input=document,
            capture_output=True,
            shell=True,
            timeout=timeout,
        )
    except Exception:  # noqa: BLE001 - a broken prior command falls through to minimal
        return None
    return result.stdout.decode("utf-8", "replace")


def main() -> int:
    document = sys.stdin.buffer.read()

    # 1. Feed crabd first, but never let it delay the render.
    post_statusline(document)

    # 2. Chain to the operator's prior status line if the installer saved one.
    prior = load_prior_command(CHAIN_PATH)
    if prior is not None:
        chained = run_chained(prior, document)
        if chained is not None:
            sys.stdout.write(chained)
            return 0

    # 3. No prior (or it failed to run): a minimal line, or nothing.
    line = minimal_status(document)
    if line:
        sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 - no traceback may reach the status line
        raise SystemExit(0)
