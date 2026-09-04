"""SideCrab notifier — a native desktop alert when a Claude session has been waiting too long.

Standalone read-only consumer of the crabd feed (``/v1/state``, see docs/STATE-CONTRACT.md).
It polls, decides, and fires at most ONE toast per waiting spell. It never writes config,
never talks to crabd's POST endpoints, and never raises out of the poll loop.

Layering: everything above ``PowerShellToastAdapter`` is pure and headless-testable
(``ToastDecider`` does the deciding, the adapters do the only platform I/O there is).

TOAST MECHANISM — two routes ship, and pick_adapter() chooses one from sys.platform.

Windows route — measured on Windows 11, 2026-08-26:

  Route A (chosen): subprocess to Windows PowerShell 5.1, WinRT projection.
      `[Windows.UI.Notifications.ToastNotificationManager, ..., ContentType=WindowsRuntime]`
      loads, CreateToastNotifier() returns a live ToastNotifier, Show() returns clean.
      Zero pip dependencies — matters because this runs from a Scheduled Task at logon,
      where a missing/older interpreter's site-packages would silently break notifications.

  Route B (rejected): the `winrt-Windows.UI.Notifications` pip package. It downloads fine
      (3.2.1 cp313 wheel), but it is NOT installed here and would pull three packages
      (winrt-runtime + Windows.UI.Notifications + Windows.Data.Xml.Dom) into whichever
      interpreter the task happens to run. Bought us nothing Route A lacks.

  TRAP: pwsh 7 CANNOT do this — `Unable to find type [...ToastNotificationManager]`. The
  WinRT type projection only exists in Windows PowerShell 5.1. Measured, not assumed. So
  POWERSHELL_EXE below is pinned to System32 powershell.exe; do not "modernize" it to pwsh.

  TRAP: under 5.1 the *static* projections `ToastNotifier.Setting` and
  `ToastNotificationManager::History` come back null rather than throwing. They are not
  usable as a "did it show?" check — do not add a delivery assertion built on them.

  A toast needs a registered AppUserModelID. Two are in play, chosen at toast time:

    PREFERRED: "SideCrab.Notifier", registered by setup\\Register-SideCrabAumid.ps1 under
      HKCU\\SOFTWARE\\Classes\\AppUserModelId. Toasts then group in Action Center as
      "SideCrab" with the SideCrab icon, and Windows' per-app notification switch is ours.

    FALLBACK: Windows PowerShell's own AUMID, which Get-StartApps confirms is registered
      here. Used whenever that registry key is absent — an unregistered box still gets its
      toasts, attributed to "Windows PowerShell". Registering is an upgrade, unregistering
      a downgrade; neither can break notification delivery.

  The toast also carries an "Acknowledge" button (v0.7.0). It activates the `sidecrab-ack:`
  URL protocol rather than this process, because a toast in Action Center outlives the
  notifier — see ACK_SCHEME below and notifier\\sidecrab_ack_handler.pyw.

  This module still WRITES no registry — it only reads, and only to choose between the two.
  A POSITIVE answer is cached for the process lifetime; a negative one is re-probed on a
  cooldown, so a notifier that was running when the installer registered the key picks it up
  on its own (see AUMID_REPROBE_SEC).

macOS route — measured on macOS 26.6 with /usr/bin/osascript, 2026-09-04:

  `osascript -e 'on run argv' -e '<script>' -e 'end run' -- <body> <title> <subtitle>` passes
  every argument to the script VERBATIM: a probe carrying a double quote, a backslash, a
  newline, `$(touch …)`, backticks, `&` and `; rm -rf /` came back byte-identical, exit 0,
  with nothing substituted or executed. So the notification text never has to be interpolated
  into AppleScript source — the script is three constant strings and the text rides in argv,
  which is the same boundary PowerShell's base64 payload buys on the Windows side.
  The display line compiles (osacompile, exit 0) and one live `--test-toast` posted with it,
  exit 0 and empty stderr.

  THE SOUND NAME IS NOT A FILE. `sound name "default"` names nothing under
  /System/Library/Sounds, whose 14 entries are Basso…Tink (measured). AppleScript accepts any
  name — an invented `"no-such-sound"` compiles too — and macOS falls back to the user's alert
  sound rather than erroring, which is why the clause survives here: it asks for the default
  and cannot fail. What was NOT verified from this session: that a sound was audible.

  Nothing here reads a registry, an AUMID or an icon: there is no identity to register.

  TWO RESIDUALS, both permanent properties of this route rather than things to fix later:

    IDENTITY: notifications posted through osascript appear under Script Editor's identity, so
      the macOS per-app notification switch is Script Editor's and MAC_SUBTITLE ("SideCrab") is
      the only thing on screen naming the product.

    NO REPLACEMENT: `display notification` cannot set a replacement identifier, so a second
      outage notice STACKS beneath the first instead of replacing it. STALE_ID's fixed tag and
      the digest/budget id prefixes still keep the deciders' ledgers honest; what they no
      longer buy on this platform is the Action Center slot behaviour they were named for.

    And it carries NO BUTTONS: `display notification` has no action affordance at all. The
      Acknowledge and Snooze buttons, ACK_SCHEME/SNOOZE_SCHEME and both .pyw handlers are the
      Windows route's, and stay Windows-only — the operator acknowledges on the panel.

DIGEST (v0.8.0): a second, unrelated toast — one "yesterday" summary per calendar day at a
  configured local time, off the same 10 s poll loop (no extra thread). Its per-day ledger is
  the ONLY thing this process writes, and it writes it to its own file (STATE_PATH), never to
  config.json — crabd owns that. See DigestDecider.

BUDGET (v0.10.0): a third toast, on the same 10 s loop — ONE per calendar day the first time
  the feed's ``burn.budget.todayPct`` reaches 1.0. It has no config block of its own: crabd
  emits ``burn.budget`` only when a budget is configured, so no budget in the feed = silence.
  Its per-day mark shares STATE_PATH with the digest's under its own key. See BudgetDecider.

APPROVALS (v0.15.0): a fourth toast — a session carrying a live ``pendingPermission`` that
  nobody has decided within ``toast.approvalThresholdSec`` (default 20 s). Informational only:
  it carries NO Approve/Deny buttons, on purpose. See ApprovalDecider.

STALE FEED (v0.15.0): a fifth toast, and the only one about SideCrab itself — ONE per outage
  when crabd stops answering or its ``generatedAt`` stops moving while the operator was
  recently working. Everything else in this file goes silent when crabd is unreachable, which
  is right for questions and wrong for the panel itself: a dead panel looks identical to a
  quiet one. See StaleFeedDecider.

LONG RUN (v0.16.0): a sixth toast — ONE when a session finishes a turn that ran longer than
  ``toast.longRunSec`` (default 900). A 25-minute build/test run is exactly the turn the
  operator walked away from. The duration is DERIVED, not measured here: crabd clears
  ``turnStartedAt`` on Stop (contract), so the only place a finished turn's start still exists
  is the poll BEFORE the ``done`` — hence the one-poll memory in LongRunDecider.

SNOOZE (v0.16.0): the waiting toast gains a second button next to Acknowledge. It activates
  ``sidecrab-snooze:<sessionId>`` (same protocol mechanism, same strict validation) and the
  handler writes a 30-minute mark into STATE_PATH, which this process READS each poll. Snooze
  DEFERS where every other suppression here MARKS — that asymmetry is the feature: the
  operator asked to be told again later, and the session is still, truthfully, waiting.

VERSION (v0.16.0): ``__version__``, a startup log line, ``--version``, and a ``notifier``
  section in STATE_PATH carrying the running version and module path. The notifier could not
  previously tell anyone it was running stale code — a Scheduled Task that has not been
  restarted since the file changed looks identical to a current one, and that class of bug
  cost two separate investigations on 2026-08-26.

VERSION (v0.20.0): doc-only correction (audit NL-a) — MUTED_SWITCH_LINE no longer claims the
  long-run toast re-surfaces on unmute. It does not: LongRunDecider's observation swap is
  unconditional (ahead of the enabled gate), so a working->done that COMPLETES while muted
  consumes the edge. Behaviour unchanged; the line now matches it, and test_mute.py pins it.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol
from xml.sax.saxutils import escape as _sax_escape

#: Control characters XML 1.0 forbids even when escaped (0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F).
#: 0x09/0x0A/0x0D are legal and left alone. A conformant parser — Windows'
#: XmlDocument.LoadXml included — rejects the WHOLE document if one survives into element text
#: or an attribute, so an escape that only handles &<> is not enough.
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def xml_escape(value: Any) -> str:
    """Escape &<> for XML AND drop the control bytes that would break the document.

    Root fix for the F1 no-retoast bug: a control char (e.g. \\x07, \\x1b, \\x00) in a session
    title / question / permission summary used to survive trim() (str.split() leaves
    0x00-0x08 and 0x0E-0x1B) into build_xml, LoadXml then threw under
    $ErrorActionPreference='Stop', show() returned False, and the spell was already resolved —
    so the waiting question or permission toast was consumed and never re-surfaced. Stripping
    here, in the one path every interpolated value crosses (text AND the button URIs, which
    never see trim()), keeps the toast well-formed. Emission-side re-arming (unresolve on a
    failed show) is the belt to this fix's braces.
    """
    return _sax_escape(_XML_ILLEGAL.sub("", str(value)))


def strip_control(value: Any) -> str:
    """The control-byte strip on its own, for a payload that is NOT XML.

    Same character class as xml_escape's, deliberately: one rule for both adapters means one
    rule to remember. The macOS route has no markup to break, and a different reason to strip
    — a NUL in an argv element makes subprocess raise ValueError (measured), which is not one
    of the failures the adapter converts to False, so the daemon would see a raise where the
    contract promises a bool. Tab, newline and carriage return are content and are kept.
    """
    return _XML_ILLEGAL.sub("", str(value))


#: Quote escapes saxutils.escape() does NOT apply by default. Numeric refs, not &apos;/&quot;
#: names, because &apos; is the one XML predefined entity HTML parsers do not know and the
#: payload crosses a WinRT boundary — a numeric ref is understood by every conformant reader.
_XML_ATTR_QUOTES = {'"': "&#34;", "'": "&#39;"}


def xml_attr_escape(value: Any) -> str:
    """xml_escape() PLUS the quotes — the escape for anything landing INSIDE an attribute.

    F3 defence-in-depth. build_xml single-quotes its attribute values, so a raw ``'`` in an
    interpolated value closes the attribute early and everything after it is parsed as markup:
    that is an attribute-breakout, and one new ``<action activationType='protocol'>`` is a
    button that launches an attacker-chosen URI. Today no attacker-controlled string reaches an
    attribute (ids pass ^[A-Za-z0-9-]{1,64}$, the icon path is percent-encoded, button contents
    are constants), so this closes no live hole — it removes the trap where a future edit puts
    a title or a tool name in an attribute and inherits quote-blind escaping.

    Element TEXT keeps xml_escape: quotes are legal there, and escaping them would only make
    the stored payload harder to read.
    """
    text = _sax_escape(_XML_ILLEGAL.sub("", str(value)))
    for raw, ref in _XML_ATTR_QUOTES.items():
        text = text.replace(raw, ref)
    return text


#: The running module's version. THE POINT OF IT IS TO BE COMPARED: it is logged at startup,
#: printed by --version, and written into STATE_PATH, so "what is on disk" and "what the
#: Scheduled Task is actually executing" stop being the same unanswerable question. Bump it in
#: the same commit as any behaviour change; setup/Test-SideCrab.ps1 reads it off both sides.
__version__ = "0.22.0"

#: A GET, so the X-SideCrab-Panel gate crabd 0.31.0 added does not apply here: it guards
#: POSTs only. The ack handler, which does POST, sends the header.
DEFAULT_ENDPOINT = "http://127.0.0.1:9999/v1/state"
DEFAULT_INTERVAL_SEC = 10.0
DEFAULT_THRESHOLD_SEC = 120
FETCH_TIMEOUT_SEC = 5.0

#: How long a permission request may sit undecided before the toast fires. Much shorter than
#: DEFAULT_THRESHOLD_SEC because the window is much shorter: crabd parks the PermissionRequest
#: hook for 55 s and then passes it through to the terminal dialog, so a threshold anywhere
#: near 120 s would only ever toast about requests that had already fallen through.
DEFAULT_APPROVAL_THRESHOLD_SEC = 20

#: How long a turn must have run for its completion to be worth a toast. 900 s is chosen so
#: the answer to "would this fire on a healthy night?" is no: an ordinary turn is seconds to a
#: couple of minutes, and 15 minutes is already "I went and did something else".
#:
#: ZERO MEANS OFF, it does not mean "toast every turn". A 0 here would fire on every completed
#: turn on the box — the textbook control that trains an operator to ignore notifications — so
#: the one value nobody could sanely want is spent on the switch instead. See LongRunDecider.
DEFAULT_LONG_RUN_SEC = 900

#: Schemas this consumer understands. Anything else is a feed we must not guess at.
#:
#: THIS LIST GOING STALE IS SILENT AND TOTAL. Measured 2026-08-26: crabd had moved to schema 4
#: while this said {1, 2, 3}, so the notifier polled happily, logged one warning at startup and
#: never toasted again — a Running Scheduled Task the whole time. Every schema since 1 has been
#: ADDITIVE (docs/STATE-CONTRACT.md), and this consumer reads only the fields common to all of
#: them, so the correct move on a bump is to add the number here, in the same commit.
#: setup/Test-SideCrab.ps1 fails its "notifier schema" row when this and the live feed diverge.
SUPPORTED_SCHEMAS = frozenset({1, 2, 3, 4, 5})

TITLE_TRIM = 48
BODY_TRIM = 140

#: Per-session dedupe ledger cap. A session with more distinct waiting spells than this in
#: one crabd lifetime forgets its oldest — bounded memory beats perfect recall for a daemon
#: that runs from logon to logoff.
LEDGER_PER_SESSION_CAP = 64

#: A session id must vanish from the feed this many consecutive polls before its ledger is
#: dropped. Guards the one real re-toast path: crabd restarting and briefly serving an empty
#: sessions array would otherwise clear the ledger mid-spell and toast the same question twice.
LEDGER_PRUNE_GRACE = 3

CONFIG_PATH = Path.home() / ".sidecrab" / "config.json"
LOG_PATH = Path.home() / ".sidecrab" / "logs" / "notifier.log"

#: The notifier's OWN state — the only file this process writes. Separate from config.json on
#: purpose: config.json is crabd's to rewrite (POST /v1/config), and two writers on one file is
#: how a half-written config gets served. Holds one per-day mark per feature (digest, budget),
#: each under its own top-level key — see DayLedger.
STATE_PATH = Path.home() / ".sidecrab" / "toast-state.json"

log = logging.getLogger("sidecrab.notifier")


# --------------------------------------------------------------------------------------
# Config (READ-ONLY — the companion lane owns writing ~/.sidecrab/config.json)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToastConfig:
    """The ``toast`` block of config.json. Absent or malformed → these defaults."""

    enabled: bool = True
    threshold_sec: int = DEFAULT_THRESHOLD_SEC
    approval_threshold_sec: int = DEFAULT_APPROVAL_THRESHOLD_SEC
    long_run_sec: int = DEFAULT_LONG_RUN_SEC


def _threshold(raw: Any, default: int) -> int:
    """A non-negative seconds value, or the default. Never raises."""
    # bool is an int subclass — {"thresholdSec": true} is a typo, not a threshold.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return default
    value = int(raw)
    return default if value < 0 else value


def parse_toast_config(doc: Any) -> ToastConfig:
    """Pure: config document → ToastConfig. Never raises; bad values fall back per-field."""
    default = ToastConfig()
    if not isinstance(doc, dict):
        return default
    block = doc.get("toast")
    if not isinstance(block, dict):
        return default

    enabled = block.get("enabled", default.enabled)
    if not isinstance(enabled, bool):
        enabled = default.enabled

    return ToastConfig(
        enabled=enabled,
        threshold_sec=_threshold(block.get("thresholdSec"), default.threshold_sec),
        approval_threshold_sec=_threshold(
            block.get("approvalThresholdSec"), default.approval_threshold_sec
        ),
        long_run_sec=_threshold(block.get("longRunSec"), default.long_run_sec),
    )


#: Strict HH:MM, per the contract's "validates HH:MM strictly". 24-hour, zero-padded, nothing else.
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class DigestConfig:
    """The ``digest`` block of config.json (v0.8.0). Absent → off."""

    enabled: bool = False
    #: Local minutes past midnight. None means "no usable time was configured".
    minute_of_day: int | None = None

    @property
    def armed(self) -> bool:
        return self.enabled and self.minute_of_day is not None


def parse_digest_config(doc: Any) -> DigestConfig:
    """Pure: config document → DigestConfig. Never raises.

    Unlike ``thresholdSec``, a bad ``time`` does NOT fall back to a default. A wrong threshold
    toasts a bit early or late; a wrong TIME fires a daily notification at an hour nobody asked
    for, which reads as a bug in the notifier rather than a typo in the config. Unusable time =
    disarmed, and the caller logs it once.
    """
    default = DigestConfig()
    if not isinstance(doc, dict):
        return default
    block = doc.get("digest")
    if not isinstance(block, dict):
        return default

    enabled = block.get("enabled", default.enabled)
    if not isinstance(enabled, bool):
        enabled = default.enabled

    minute: int | None = None
    raw = block.get("time")
    if isinstance(raw, str):
        match = _HHMM_RE.match(raw.strip())
        if match:
            minute = int(match.group(1)) * 60 + int(match.group(2))

    return DigestConfig(enabled=enabled, minute_of_day=minute)


class ConfigReader:
    """Re-reads config.json when its mtime moves. Strictly read-only; open() is never 'w'.

    Two blocks are parsed from the one file read — ``read()`` for the waiting-session toast and
    ``read_digest()`` for the daily digest — so the poll loop never stats the file twice.
    """

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._cached = ToastConfig()
        self._cached_digest = DigestConfig()
        self._stamp: tuple[int, int] | None = None
        self._warned = False
        self._digest_time_warned = False

    def _refresh(self) -> None:
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            # No config yet is the normal first-run state, not an error.
            self._stamp = None
            self._cached = ToastConfig()
            self._cached_digest = DigestConfig()
            return

        if stamp == self._stamp:
            return

        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Half-written file mid-save by the companion lane: keep the last good config.
            if not self._warned:
                log.warning("config unreadable (%s); keeping %s", exc, self._cached)
                self._warned = True
            return

        self._warned = False
        self._stamp = stamp
        self._cached = parse_toast_config(doc)
        self._cached_digest = parse_digest_config(doc)
        log.info("config loaded: %s, %s", self._cached, self._cached_digest)

        if self._cached_digest.enabled and self._cached_digest.minute_of_day is None:
            if not self._digest_time_warned:
                log.warning("digest enabled but its time is missing/unparseable — digest stays off")
                self._digest_time_warned = True
        else:
            self._digest_time_warned = False

    def read(self) -> ToastConfig:
        self._refresh()
        return self._cached

    def read_digest(self) -> DigestConfig:
        self._refresh()
        return self._cached_digest


# --------------------------------------------------------------------------------------
# Decision logic — pure, no I/O, no clock of its own
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ToastRequest:
    """One toast the decider believes is owed. The adapter turns this into pixels."""

    session_id: str
    state_since: str
    title: str
    body: str
    #: False suppresses the Acknowledge button. The digest has no session to acknowledge —
    #: a button that POSTs a made-up sessionId would just 404 against /v1/action.
    actionable: bool = True


def trim(text: str, limit: int) -> str:
    """Trim to ``limit`` visible characters, ellipsis included in the budget."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "\u2026"


def parse_iso(value: Any) -> datetime | None:
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
        # crabd emits UTC; a naive stamp is UTC by contract.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_quiet(state: Any) -> bool:
    quiet = state.get("quiet") if isinstance(state, dict) else None
    return bool(quiet.get("active")) if isinstance(quiet, dict) else False


def build_request(session: dict, state_since: str) -> ToastRequest:
    """Pure: a needs_input session → the toast text. Never fabricates a question."""
    raw_title = session.get("title")
    label = trim(raw_title, TITLE_TRIM) if isinstance(raw_title, str) and raw_title.strip() else "a Claude session"

    question = session.get("question")
    if isinstance(question, str) and question.strip():
        body = trim(question, BODY_TRIM)
    else:
        # Schema 1 has no `question`; lastEvent is the contract's short human line.
        event = session.get("lastEvent")
        body = (
            trim(event, BODY_TRIM)
            if isinstance(event, str) and event.strip()
            else "This session is waiting for your input."
        )

    return ToastRequest(
        session_id=str(session.get("id")),
        state_since=state_since,
        title=f"Claude is waiting \u2014 {label}",
        body=body,
    )


class ToastDecider:
    """Decides which waiting sessions are owed a toast, and remembers what it already said.

    Dedupe key is ``(sessionId, stateSince)`` — one toast per waiting spell, forever. A new
    question moves stateSince, which re-arms. Nothing else does.
    """

    def __init__(self) -> None:
        # sid -> ordered set of stateSince values already resolved (toasted or cancelled).
        self._ledger: dict[str, dict[str, None]] = {}
        # sid -> consecutive polls in which the session was absent from the feed.
        self._misses: dict[str, int] = {}
        # (sid, until) already announced, so a 30 min snooze costs one log line, not 180.
        self._snooze_logged: set[tuple[str, str]] = set()

    # -- ledger -------------------------------------------------------------------------

    def _resolved(self, sid: str, since: str) -> bool:
        return since in self._ledger.get(sid, {})

    def _resolve(self, sid: str, since: str) -> None:
        """Mark a spell as handled. Toasting and cancelling land in the same place: both mean
        'this spell will never produce a (further) toast'."""
        spells = self._ledger.setdefault(sid, {})
        spells[since] = None
        while len(spells) > LEDGER_PER_SESSION_CAP:
            spells.pop(next(iter(spells)))

    def unresolve(self, request: ToastRequest) -> None:
        """Re-arm a spell whose toast FAILED to render, so the next poll retries it.

        evaluate() resolves a spell before it is shown (the mark is what dedupes the next
        poll). F1: a show() that returns False — an illegal-XML title that trips LoadXml, a
        dead PowerShell — must NOT then consume a waiting question forever. _emit calls this
        only on the failure path, and only for a request THIS decider produced (routed by the
        daemon), so a quiet-suppressed spell — marked but never shown — is never re-armed.
        """
        spells = self._ledger.get(request.session_id)
        if spells is not None:
            spells.pop(request.state_since, None)
            if not spells:
                self._ledger.pop(request.session_id, None)

    def _prune(self, present: Iterable[str]) -> None:
        present = set(present)
        for sid in list(self._ledger):
            if sid in present:
                self._misses.pop(sid, None)
                continue
            self._misses[sid] = self._misses.get(sid, 0) + 1
            if self._misses[sid] >= LEDGER_PRUNE_GRACE:
                self._ledger.pop(sid, None)
                self._misses.pop(sid, None)
                # The "already said this" set is dropped with the session, so it cannot
                # outlive what it describes in a daemon that runs from logon to logoff.
                self._snooze_logged = {k for k in self._snooze_logged if k[0] != sid}

    # -- the decision -------------------------------------------------------------------

    def evaluate(
        self,
        state: Any,
        now: datetime,
        config: ToastConfig,
        snoozes: dict[str, datetime] | None = None,
    ) -> list[ToastRequest]:
        """Pure w.r.t. the outside world; mutates only the internal ledger.

        ``snoozes`` is sid -> the instant the operator's Snooze runs out, read from the ledger
        by the caller (SnoozeLedger). Defaulting it to None keeps every existing call site and
        test three-argument — a snooze is an addition to the decision, not a change to it.
        """
        snoozes = snoozes or {}
        if not isinstance(state, dict):
            return []

        sessions = state.get("sessions")
        if not isinstance(sessions, list):
            sessions = []
        sessions = [s for s in sessions if isinstance(s, dict)]

        self._prune(str(s.get("id")) for s in sessions if isinstance(s.get("id"), str))

        if not config.enabled:
            # Fully off: no toasts and no ledger writes, so enabling it later behaves like a
            # fresh start rather than swallowing whatever was waiting while it was off.
            return []

        quiet = is_quiet(state)
        owed: list[ToastRequest] = []

        for session in sessions:
            sid = session.get("id")
            since = session.get("stateSince")
            if not isinstance(sid, str) or not sid:
                continue
            if session.get("state") != "needs_input":
                continue
            if not isinstance(since, str) or not since:
                continue
            if self._resolved(sid, since):
                continue

            if session.get("acked"):
                # the operator already saw it on the widget. Resolve rather than skip: if acked ever
                # flips back with the same stateSince, this spell still must not re-toast.
                self._resolve(sid, since)
                continue

            started = parse_iso(since)
            if started is None:
                log.debug("session %s has unparseable stateSince %r", sid, since)
                continue

            if (now - started).total_seconds() < config.threshold_sec:
                continue  # not yet due — deliberately NOT resolved, so it can toast later

            until = snoozes.get(sid)
            if until is not None and now < until:
                # SNOOZE DEFERS. Every other suppression in this file marks the spell consumed;
                # this one deliberately does not, and the difference is the whole feature. Quiet
                # hours mean "I was asleep, that question is stale by morning"; Snooze means "I
                # know, tell me again in half an hour", and a reminder that never comes back is
                # not a reminder. The spell is still unresolved, so the poll after the mark
                # expires toasts it — same (sessionId, stateSince), one more time.
                key = (sid, until.isoformat())
                if key not in self._snooze_logged:
                    self._snooze_logged.add(key)
                    log.info("snoozed until %s: holding the toast for %s", until.isoformat(), sid)
                continue

            self._resolve(sid, since)

            if quiet:
                # Quiet hours mean silent, not deferred. Resolving here is the point: without
                # it, every spell that matured overnight would fire the instant quiet ended —
                # a burst of stale toasts on a perfectly healthy morning. The widget still
                # renders the waiting card, which is where a slept-through question belongs.
                log.info("quiet hours: suppressed toast for %s", sid)
                continue

            owed.append(build_request(session, since))

        return owed


# --------------------------------------------------------------------------------------
# Panel approvals (v0.15.0) — one toast per undecided permission request
# --------------------------------------------------------------------------------------

APPROVAL_TITLE = "Claude needs permission"

#: Own Action Center slot, like the digest's and the budget's. Sharing the raw session id
#: would let an approval toast REPLACE that session's still-waiting question toast.
APPROVAL_ID_PREFIX = "approval-"

#: THE TOAST IS INFORMATIONAL. It deliberately carries no Approve/Deny buttons.
#:
#: Toast actions are cheap to hit — one click from the lock screen, from a notification the
#: shell replays out of Action Center hours later, by anyone standing at the machine. That is
#: an acceptable cost for "Acknowledge" (it clears a dot) and NOT an acceptable one for
#: "allow this tool call", which is the only thing standing between a queued Bash command and
#: the disk. Approving is a security decision, and a security decision needs the context the
#: panel shows and the deliberate act of opening it. crabd equally has no branch that yields
#: behavior:allow without a /v1/action decide tap (STATE-CONTRACT v0.12.0 §4: "NEVER
#: auto-allow") — a toast button would be a second, weaker door into the same decision.
#:
#: So the body ends with this, and the operator goes to the panel. The hint is reserved out of
#: the body budget rather than appended after trimming: the summary is what gets cut when a
#: tool argument is long, never the instruction telling the operator where to act.
APPROVAL_HINT = "Decide on the panel."
APPROVAL_BODY_TRIM = BODY_TRIM - len(APPROVAL_HINT) - 1

#: crabd trims the tool name before serving it; this is a second belt on the toast side.
TOOL_TRIM = 32

#: Consecutive polls a session must carry NO pending request before its dedupe mark is
#: dropped. Same idiom, and the same reason, as LEDGER_PRUNE_GRACE: the mark is what stops a
#: re-toast, so a one-poll flicker in the feed must not be able to clear it.
APPROVAL_CLEAR_GRACE = 3


@dataclass(frozen=True)
class PendingPermission:
    """The trustworthy parts of one session's ``pendingPermission`` block."""

    session_id: str
    tool: str
    summary: str | None
    requested_at: str


def read_pending_permission(session: Any) -> PendingPermission | None:
    """Pure: one session row → its live permission request, or None.

    None covers every "nothing parked" case in one place — an older crabd with no key, the
    contract's present-and-null, a malformed block, and a block with no usable ``requestedAt``.
    That last one is a refusal, not a fallback: requestedAt IS the dedupe key, and a request we
    cannot key is one we cannot promise to toast only once, so it is not toasted at all.
    """
    if not isinstance(session, dict):
        return None
    sid = session.get("id")
    if not isinstance(sid, str) or not sid:
        return None
    block = session.get("pendingPermission")
    if not isinstance(block, dict):
        return None
    requested = block.get("requestedAt")
    if not isinstance(requested, str) or not requested.strip():
        return None

    raw_tool = block.get("tool")
    tool = trim(raw_tool, TOOL_TRIM) if isinstance(raw_tool, str) and raw_tool.strip() else "a tool"
    raw_summary = block.get("summary")
    # crabd serves summary: null for a tool whose input it will not try to describe. Never
    # invent one — the toast then names the tool and stops, which is honest.
    summary = raw_summary if isinstance(raw_summary, str) and raw_summary.strip() else None

    return PendingPermission(session_id=sid, tool=tool, summary=summary, requested_at=requested.strip())


def build_approval_request(pending: PendingPermission) -> ToastRequest:
    """Pure: a parked request → the toast text."""
    detail = f"{pending.tool} — {pending.summary}" if pending.summary else pending.tool
    return ToastRequest(
        session_id=f"{APPROVAL_ID_PREFIX}{pending.session_id}",
        state_since=pending.requested_at,
        title=APPROVAL_TITLE,
        body=f"{trim(detail, APPROVAL_BODY_TRIM)} {APPROVAL_HINT}",
        # No Acknowledge button either, and for the widget's own reason: a pendingPermission
        # card is a hard stop, not an ack-able question (it is the one card ack-all skips).
        # Acking it would clear the dot while the tool call stayed parked.
        actionable=False,
    )


class ApprovalDecider:
    """Decides which parked permission requests are owed a toast.

    Dedupe key is ``(sessionId, requestedAt)`` — one toast per request, forever. The ledger is
    ONE SLOT per session rather than a set, because crabd's pending map is keyed by session id:
    a session can only have one request parked at a time, and the next one necessarily carries a
    later requestedAt.

    DELIBERATELY NOT GATED ON ``state == "needs_input"``, which is what the widget's approval
    card keys on. crabd registers the pending entry from the PermissionRequest hook and does not
    itself move the state machine — needs_input arrives separately, via the Notification hook,
    and the two are not ordered. A live pendingPermission is already proof that a real hook is
    parked on the long poll RIGHT NOW, which is the whole signal; requiring a second, racing
    field could only ever lose toasts.
    """

    def __init__(self) -> None:
        # sid -> the requestedAt this decider has finished with (toasted or quiet-suppressed).
        self._marks: dict[str, str] = {}
        # sid -> consecutive polls carrying no parked request.
        self._clear: dict[str, int] = {}

    def _sweep(self, live: dict[str, PendingPermission]) -> None:
        """Drop the mark once the request has RESOLVED — decided from the panel, denied, or
        timed out into the terminal dialog — so the ledger stays bounded by live sessions
        rather than by everything that ever asked. Guarded by the grace count, because the
        mark is the only thing stopping a re-toast of a request still on screen."""
        for sid in list(self._marks):
            if sid in live:
                self._clear.pop(sid, None)
                continue
            self._clear[sid] = self._clear.get(sid, 0) + 1
            if self._clear[sid] >= APPROVAL_CLEAR_GRACE:
                self._marks.pop(sid, None)
                self._clear.pop(sid, None)

    def unresolve(self, request: ToastRequest) -> None:
        """Re-arm a permission request whose toast FAILED to render (F1, the security-relevant
        variant: a control byte in a tool summary used to permanently suppress the 'Claude needs
        permission' toast). evaluate() marks before showing; _emit calls this on the failure
        path so the next poll retries. Guarded on the stored mark still matching, so a request
        that has since been re-marked by a later poll is left alone."""
        sid = request.session_id[len(APPROVAL_ID_PREFIX):]
        if self._marks.get(sid) == request.state_since:
            self._marks.pop(sid, None)

    def evaluate(self, state: Any, now: datetime, config: ToastConfig) -> list[ToastRequest]:
        """Pure w.r.t. the outside world; mutates only the internal ledger."""
        if not isinstance(state, dict):
            return []

        sessions = state.get("sessions")
        if not isinstance(sessions, list):
            sessions = []

        live: dict[str, PendingPermission] = {}
        for session in sessions:
            pending = read_pending_permission(session)
            if pending is not None:
                live[pending.session_id] = pending
        self._sweep(live)

        if not config.enabled:
            # Same contract as the waiting-session toast: off means no toasts AND no ledger
            # writes, so turning it back on surfaces whatever is genuinely still parked.
            return []

        quiet = is_quiet(state)
        owed: list[ToastRequest] = []

        for sid, pending in live.items():
            if self._marks.get(sid) == pending.requested_at:
                continue

            requested = parse_iso(pending.requested_at)
            if requested is None:
                log.debug("session %s has unparseable requestedAt %r", sid, pending.requested_at)
                continue

            if (now - requested).total_seconds() < config.approval_threshold_sec:
                continue  # not yet due — deliberately NOT marked, so it can toast later

            self._marks[sid] = pending.requested_at

            if quiet:
                # Silent, not deferred — the same rule the other four follow. A request that
                # matured during quiet hours has long since fallen through to the terminal
                # dialog by the time quiet lifts; toasting about it then would be a lie.
                log.info("quiet hours: suppressed approval toast for %s", sid)
                continue

            owed.append(build_approval_request(pending))

        return owed


# --------------------------------------------------------------------------------------
# Long-run completion (v0.16.0) — one toast when a long turn finishes
# --------------------------------------------------------------------------------------

#: Own Action Center slot, like every non-session toast here. A completion must never take the
#: slot of a question that is still waiting.
LONG_RUN_ID_PREFIX = "longrun-"

#: The states in which crabd populates ``turnStartedAt`` are ``working`` and ``needs_input``
#: (measured in companion/crabd.py: the field is nulled in every other state). Only ``working``
#: counts as RUNNING here.
#:
#: needs_input is excluded deliberately, and it is the difference between an honest number and
#: a flattering one: turnStartedAt survives working -> needs_input, so a turn that ran 40 s and
#: then sat 30 min waiting for the operator would report "Finished after 31m". That is the
#: operator's own thinking time, reported back to him as compute. The widget's celebration
#: (widget/scripts/sidecrab.js detectCelebration) keys on the same prev.state === 'working'.
RUNNING_STATE = "working"

#: Per-session cap on remembered finished turns, and the grace before a vanished session's
#: memory is dropped. Same idioms and the same reasons as ToastDecider's.
LONG_RUN_PER_SESSION_CAP = 32
LONG_RUN_PRUNE_GRACE = 3


@dataclass(frozen=True)
class TurnObservation:
    """What the PREVIOUS poll saw of one session's turn.

    This one-poll memory is not an optimisation, it is the only way the duration exists.
    STATE-CONTRACT: ``turnStartedAt`` is set on UserPromptSubmit and CLEARED on Stop, so the
    ``done`` row that tells us a turn finished has already forgotten when it began. crabd
    serves no turn-duration field and no finished-turn record, so the alternatives were: read
    it from the poll before (this), or approximate it from ``stateSince`` — which on a done row
    is the Stop instant, and on the reactivation path (``done`` -> ``working`` because the
    transcript moved) advances on EVERY poll and would measure nothing at all. Measured on the
    live feed 2026-08-27: three sessions, all three carrying ``turnStartedAt: null`` between
    turns, one of them reading ``working`` with a ``stateSince`` that moved every ten seconds.
    """

    state: str
    #: The feed's ``turnStartedAt``, verbatim. None means "no turn was running", which is the
    #: normal state of a session between prompts, NOT a missing measurement.
    turn_started_at: str | None


def read_turn(session: Any) -> tuple[str, TurnObservation] | None:
    """Pure: one session row → (sessionId, what we need to remember). None for unusable rows."""
    if not isinstance(session, dict):
        return None
    sid = session.get("id")
    if not isinstance(sid, str) or not sid:
        return None
    state = session.get("state")
    if not isinstance(state, str):
        return None
    turn = session.get("turnStartedAt")
    if not isinstance(turn, str) or not turn.strip():
        turn = None
    return sid, TurnObservation(state=state, turn_started_at=turn)


def format_duration(seconds: float) -> str:
    """Whole minutes below an hour, ``Nh Mm`` above it. Never seconds: this describes a run of
    at least longRunSec (900 s by default), where a trailing ``14s`` is noise."""
    minutes = max(0, int(seconds) // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h" if rest == 0 else f"{hours}h {rest}m"


def build_long_run_request(
    session: dict, sid: str, started: datetime, finished: datetime
) -> ToastRequest:
    """Pure: a finished long turn → the toast text.

    The body is the two clock times, in the operator's own zone, and nothing else. It is the
    one detail he cannot reconstruct from the title and cannot get wrong: "22m" tells him it
    was long, "started 14:31, finished 14:53" tells him WHICH run it was when three sessions
    finished while he was at lunch.
    """
    raw_title = session.get("title")
    label = (
        trim(raw_title, TITLE_TRIM)
        if isinstance(raw_title, str) and raw_title.strip()
        else "a Claude session"
    )
    elapsed = format_duration((finished - started).total_seconds())
    return ToastRequest(
        session_id=f"{LONG_RUN_ID_PREFIX}{sid}",
        state_since=started.isoformat(),
        title=f"Finished after {elapsed} — {label}",
        body=(
            f"Started {started.astimezone():%H:%M}, finished {finished.astimezone():%H:%M}."
        ),
        # Nothing to acknowledge: the turn is over, and there is no waiting card behind this
        # toast for an ack to clear. Snoozing it would be stranger still — see SnoozeLedger.
        actionable=False,
    )


class LongRunDecider:
    """One toast per long turn, keyed on ``(sessionId, turnStartedAt)``.

    Fires on the ``working -> done`` EDGE, read across two polls. Consequences worth knowing,
    because they are the honest limits of the available data rather than bugs to fix later:

    - A turn whose ``done`` the notifier never observes produces no toast. crabd holds a done
      row for DONE_DROP_SEC, which is many 10 s polls, so this needs the notifier to be
      stopped across the transition — and a notifier that was not running is exactly the one
      that should not invent history when it comes back.
    - A notifier started mid-turn has no previous observation and stays silent for that turn.
      The alternative would be to time from first sight, which reports how long the NOTIFIER
      has been up, not how long the turn ran.
    - A turn with no ``turnStartedAt`` on the poll before its ``done`` is not toasted at all.
      That covers the reactivation path (crabd serves ``working`` because the transcript moved
      after a Stop, with the turn already cleared) and any feed where the UserPromptSubmit hook
      is not installed. There is no honest duration in either case, so there is no toast.
    """

    def __init__(self) -> None:
        # sid -> what the previous poll saw. Replaced wholesale each poll.
        self._prev: dict[str, TurnObservation] = {}
        # sid -> ordered set of turnStartedAt values already reported.
        self._marks: dict[str, dict[str, None]] = {}
        # sid -> consecutive polls in which the session was absent from the feed.
        self._misses: dict[str, int] = {}

    def _mark(self, sid: str, turn: str) -> None:
        turns = self._marks.setdefault(sid, {})
        turns[turn] = None
        while len(turns) > LONG_RUN_PER_SESSION_CAP:
            turns.pop(next(iter(turns)))

    def _prune(self, present: set[str]) -> None:
        for sid in list(self._marks):
            if sid in present:
                self._misses.pop(sid, None)
                continue
            self._misses[sid] = self._misses.get(sid, 0) + 1
            if self._misses[sid] >= LONG_RUN_PRUNE_GRACE:
                self._marks.pop(sid, None)
                self._misses.pop(sid, None)

    def evaluate(self, state: Any, now: datetime, config: ToastConfig) -> list[ToastRequest]:
        """Pure w.r.t. the outside world; mutates only the internal memory.

        ``now`` is accepted for signature parity with the other deciders and is deliberately
        UNUSED: every instant this decision needs comes out of the feed, so the answer cannot
        change with how late the notifier got round to asking.
        """
        del now
        if not isinstance(state, dict):
            return []

        sessions = state.get("sessions")
        if not isinstance(sessions, list):
            sessions = []

        current: dict[str, TurnObservation] = {}
        rows: dict[str, dict] = {}
        for session in sessions:
            read = read_turn(session)
            if read is None:
                continue
            sid, observation = read
            current[sid] = observation
            rows[sid] = session

        prev, self._prev = self._prev, current
        self._prune(set(current))

        # THE OBSERVATION SWAP ABOVE IS UNCONDITIONAL, AND DELIBERATELY AHEAD OF THIS RETURN.
        # It is a reading, not a decision. Skipping it while off would leave `_prev` holding an
        # observation from whenever the feature was last enabled, and the first `done` after a
        # re-enable would then be measured against a turn start from an arbitrarily old poll —
        # a confidently wrong duration, in the one toast whose entire content is a duration.
        # The cost of getting this right is that enabling mid-turn can deliver that turn's
        # completion, which is both true and current, and is the same trade the waiting toast
        # makes when it surfaces a question that matured while it was off.
        if not config.enabled or config.long_run_sec <= 0:
            return []

        quiet = is_quiet(state)
        owed: list[ToastRequest] = []

        for sid, observation in current.items():
            if observation.state != "done":
                continue
            before = prev.get(sid)
            if before is None or before.state != RUNNING_STATE or before.turn_started_at is None:
                continue
            if before.turn_started_at in self._marks.get(sid, {}):
                continue

            started = parse_iso(before.turn_started_at)
            # The done row's stateSince IS the Stop instant (crabd sets `since` on the state
            # change), so it is the finish time and `now` is not a substitute — `now` would
            # silently add however long the notifier took to notice. An unparseable one is
            # refused rather than approximated: a made-up duration in a toast that exists to
            # report a duration is worse than no toast.
            finished = parse_iso(rows[sid].get("stateSince"))
            if started is None or finished is None:
                log.debug("session %s finished a turn with no usable timestamps — no toast", sid)
                continue

            elapsed = (finished - started).total_seconds()
            if elapsed < 0:
                # Clock skew or a feed that reordered its stamps. Marked, so a nonsense
                # reading is not retried every poll for the life of the done row.
                log.debug("session %s: turn finished %.0fs before it started — ignored", sid, -elapsed)
                self._mark(sid, before.turn_started_at)
                continue
            if elapsed < config.long_run_sec:
                # A short turn. MARKED: this turn is decided, and leaving it armed would make
                # every subsequent poll re-examine a done row that can never qualify.
                self._mark(sid, before.turn_started_at)
                continue

            self._mark(sid, before.turn_started_at)

            if quiet:
                # Suppressed and marked, like the other five. A run that finished at 03:00 is
                # not news at 07:00, and the operator was not waiting for it.
                log.info("quiet hours: suppressed the long-run toast for %s (%.0fs)", sid, elapsed)
                continue

            owed.append(build_long_run_request(rows[sid], sid, started, finished))

        return owed


# --------------------------------------------------------------------------------------
# Daily digest (v0.8.0) — one "yesterday" toast per calendar day
# --------------------------------------------------------------------------------------

DIGEST_TITLE = "SideCrab — yesterday"

#: Tag/id prefix for the digest's ToastRequest. Distinct from any session id, so the digest
#: gets its own Action Center slot instead of replacing a question that is still waiting.
DIGEST_ID_PREFIX = "digest-"


def read_state_doc(path: Path) -> dict:
    """The whole notifier state document, or {} when it is missing/corrupt. Never raises."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.debug("toast state at %s unreadable (%s); treating it as empty", path, exc)
        return {}
    return doc if isinstance(doc, dict) else {}


def write_state_section(path: Path, section: str, value: Any) -> bool:
    """Replace ONE top-level key of the state document, atomically. True when it landed.

    READ-MODIFY-WRITE, never a whole-file rewrite: five things now share this one file (the
    digest day, the budget day, the snooze marks, the runtime stamp, and whatever comes next),
    they are separate objects, and a writer that serialised only its own section would erase
    the others on every write. That failure is silent and only shows up as a duplicate toast
    after a restart, which is exactly the class of bug nobody traces back to a state file.

    The temp file carries the PID because the snooze handler is a SEPARATE PROCESS writing the
    same document: a shared ``.tmp`` name would let two writers interleave into one file and
    then os.replace a hybrid of both.
    """
    doc = read_state_doc(path)
    doc[section] = value
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        # os.replace is atomic on Windows too: a reader never sees a half-written ledger.
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


class DayLedger:
    """One "last day I fired" mark, persisted so a task restart cannot double-toast.

    This is the notifier's own file (STATE_PATH). Every operation is best-effort: a ledger we
    cannot read means "not marked" (worst case, one duplicate toast), and a ledger we cannot
    write is logged once and otherwise ignored — a daemon must not die over a state file.

    SUBCLASS PER FEATURE, sharing the one file: ``SECTION`` names the top-level key. mark() is
    read-modify-write for exactly that reason — the digest and the budget ledger are separate
    objects pointing at the same path, and a whole-file rewrite would have each silently erase
    the other's mark on every write (a restart would then re-toast whichever fired first).
    """

    #: Top-level key in STATE_PATH. Every subclass must set its own.
    SECTION = ""

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        self._day: str | None = None
        self._loaded = False
        self._write_warned = False

    def _read_doc(self) -> dict:
        """The whole state document, or {} when it is missing/corrupt."""
        return read_state_doc(self.path)

    def last_day(self) -> str | None:
        if not self._loaded:
            self._loaded = True
            block = self._read_doc().get(self.SECTION)
            day = block.get("lastDay") if isinstance(block, dict) else None
            self._day = day if isinstance(day, str) and day else None
        return self._day

    def mark(self, day: str) -> None:
        self.last_day()  # force the load, so a write never drops an unread day
        self._day = day
        if write_state_section(self.path, self.SECTION, {"lastDay": day}):
            self._write_warned = False
        elif not self._write_warned:
            log.warning("cannot persist toast state to %s — a restart may re-toast", self.path)
            self._write_warned = True


class DigestLedger(DayLedger):
    SECTION = "digest"


#: Top-level key in STATE_PATH holding ``{"<sessionId>": "<ISO instant the snooze runs out>"}``.
#: Written ONLY by notifier\\sidecrab_snooze_handler.pyw, read only here — a single writer is
#: what keeps a cross-process ledger honest without a lock.
SNOOZE_SECTION = "snooze"

#: Ceiling on remembered marks. The handler prunes expired ones on every write, so reaching
#: this needs ~64 sessions snoozed inside one 30-minute window; the cap is the backstop that
#: stops a pathological writer growing the file the notifier reads every poll.
SNOOZE_MAP_CAP = 64


def parse_snooze_map(doc: Any) -> dict[str, datetime]:
    """Pure: the state document → sid -> expiry. Unusable entries are dropped, never guessed.

    A mark whose instant will not parse is DISCARDED rather than treated as "snoozed forever"
    or "snoozed until now". Both of those are decisions the operator did not make, and one of
    them silences a waiting question permanently.
    """
    if not isinstance(doc, dict):
        return {}
    block = doc.get(SNOOZE_SECTION)
    if not isinstance(block, dict):
        return {}
    marks: dict[str, datetime] = {}
    for sid, raw in block.items():
        if not isinstance(sid, str) or not _SESSION_ID_RE.match(sid):
            # The same charset the button was allowed to embed. A ledger is a file on disk and
            # anything can write to it, so what comes back out is validated like anything else.
            continue
        until = parse_iso(raw)
        if until is not None:
            marks[sid] = until
    return marks


class SnoozeLedger:
    """The notifier's READ-ONLY view of the snooze marks the handler writes.

    Re-read when the file's mtime/size moves, the same idiom as ConfigReader — the point of
    persisting a snooze is that a separate process wrote it, so a value cached for the process
    lifetime would make the button do nothing until the next restart.

    Never writes. The handler is the only writer of ``SNOOZE_SECTION``, which is what makes a
    two-process ledger safe without a lock: the digest/budget/runtime writers here go through
    write_state_section and replace only their own key.
    """

    def __init__(self, path: Path = STATE_PATH) -> None:
        self.path = path
        self._marks: dict[str, datetime] = {}
        self._stamp: tuple[int, int] | None = None
        self._loaded = False

    def read(self) -> dict[str, datetime]:
        try:
            st = self.path.stat()
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            # No state file yet is the normal state of a box where nobody has snoozed anything.
            self._stamp = None
            self._marks = {}
            self._loaded = True
            return self._marks

        if self._loaded and stamp == self._stamp:
            return self._marks

        self._stamp = stamp
        self._loaded = True
        self._marks = parse_snooze_map(read_state_doc(self.path))
        return self._marks


@dataclass(frozen=True)
class DigestDecision:
    """What the digest decider concluded for this poll."""

    #: The calendar day to mark consumed, or None to leave the day armed for a later poll.
    day: str | None = None
    request: ToastRequest | None = None
    reason: str = "idle"


def find_week_row(state: Any, day: str) -> dict | None:
    """Pure: the recap.week row for ``day``, or None. Absent recap/week/row are all None."""
    if not isinstance(state, dict):
        return None
    recap = state.get("recap")
    if not isinstance(recap, dict):
        return None
    week = recap.get("week")
    if not isinstance(week, list):
        return None
    for row in week:
        if isinstance(row, dict) and row.get("day") == day:
            return row
    return None


def _week_count(row: dict, key: str) -> int | None:
    value = row.get(key)
    # bool is an int subclass; {"done": true} is a malformed row, not a count of 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def build_digest_request(row: dict, day: str) -> ToastRequest | None:
    """Pure: a recap.week row → the digest toast, or None when the row cannot be trusted.

    Body wording is the contract's, verbatim ("N done · M commits") — the same string the
    widget and crabd lanes were briefed on. Grammar is deliberately not "fixed" here; a
    cross-lane string that drifts is worse than "1 commits".
    """
    done = _week_count(row, "done")
    commits = _week_count(row, "commits")
    if done is None or commits is None:
        return None
    return ToastRequest(
        session_id=f"{DIGEST_ID_PREFIX}{day}",
        state_since=day,
        title=DIGEST_TITLE,
        body=f"{done} done · {commits} commits",
        actionable=False,
    )


class DigestDecider:
    """Decides whether this poll owes the daily digest. Pure — the ledger is passed in.

    The schedule rides the existing 10 s poll: fire once ``now_local`` has passed the
    configured minute and the day is not yet marked. No thread, no timer, so a machine asleep
    at the digest minute simply fires at the next poll after it wakes — same calendar day.
    """

    def evaluate(
        self,
        state: Any,
        now_local: datetime,
        config: DigestConfig,
        last_day: str | None,
    ) -> DigestDecision:
        if not config.armed:
            # Off means off, and means no ledger write either: enabling the digest later
            # behaves like a fresh start rather than inheriting a day it never ran.
            return DigestDecision(reason="disabled")

        today = now_local.date().isoformat()
        if last_day == today:
            return DigestDecision(reason="already marked today")

        assert config.minute_of_day is not None  # guarded by config.armed
        if now_local.hour * 60 + now_local.minute < config.minute_of_day:
            # Not due yet. Deliberately unmarked, so it fires later today.
            return DigestDecision(reason="not due")

        if is_quiet(state):
            # Same philosophy as the waiting-session toast: quiet hours mean SILENT, not
            # deferred. Marking here is the point — otherwise the digest would fire the
            # instant quiet ended, which is both stale and startling.
            return DigestDecision(day=today, reason="quiet hours")

        yesterday = (now_local.date() - timedelta(days=1)).isoformat()
        row = find_week_row(state, yesterday)
        request = build_digest_request(row, yesterday) if row is not None else None
        if request is None:
            # No recap, no week, no row for yesterday, or a row with non-integer counts.
            #
            # This MARKS the day: the digest is skipped silently and retried tomorrow, never
            # re-attempted later today. The trade-off was taken deliberately. Not marking
            # would make a crabd that is still warming up (recap is null for a few seconds
            # after it starts) merely delay the digest — but it would equally let a crabd
            # that was down all morning deliver a "yesterday" digest at 4pm, which is worse
            # than not delivering it. crabd being unreachable entirely never reaches here:
            # poll_once returns before the digest when the fetch fails, so the ordinary
            # restart case retries on the next poll.
            return DigestDecision(day=today, reason=f"no recap.week row for {yesterday}")

        return DigestDecision(day=today, request=request, reason="digest")


# --------------------------------------------------------------------------------------
# Burn budget (v0.10.0) — one toast per calendar day, on first crossing 100%
# --------------------------------------------------------------------------------------

BUDGET_TITLE = "Daily token budget crossed"

#: Own Action Center slot, like the digest's — a shared Tag would let the budget toast replace
#: a question that is still waiting.
BUDGET_ID_PREFIX = "budget-"


@dataclass(frozen=True)
class BudgetReading:
    """The trustworthy parts of ``burn.budget``, plus today's measured output tokens."""

    daily_output_tokens: int
    today_pct: float
    used_output_tokens: int

    @property
    def crossed(self) -> bool:
        # >= is the contract's wording ("todayPct >= 1.0"): exactly 100% has crossed.
        return self.today_pct >= 1.0


def _positive_int(value: Any) -> int | None:
    # bool is an int subclass; {"dailyOutputTokens": true} is a malformed feed, not a budget of 1.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def read_budget(state: Any) -> BudgetReading | None:
    """Pure: /v1/state → the budget reading, or None when the feed carries no usable one.

    None covers every "nothing to say" case in one place — an older crabd with no ``budget``
    key, a cleared budget, and a malformed block alike. The caller must treat all three the
    same: no toast, and no ledger write either (a feed that starts carrying a budget at noon
    must still be able to toast today).
    """
    if not isinstance(state, dict):
        return None
    burn = state.get("burn")
    if not isinstance(burn, dict):
        return None
    block = burn.get("budget")
    if not isinstance(block, dict):
        return None

    daily = _positive_int(block.get("dailyOutputTokens"))
    if daily is None:
        return None

    pct = block.get("todayPct")
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or pct < 0:
        return None

    # The measured number is preferred over pct*daily. todayPct is CAPPED at 9.99 by the
    # contract, so at 12x the derived figure would understate real spend by thousands of
    # tokens; burn.today.outputTokens is uncapped. Derived only when the feed omits it.
    today = burn.get("today")
    used = _positive_int(today.get("outputTokens")) if isinstance(today, dict) else None
    if used is None:
        used = int(round(float(pct) * daily))

    return BudgetReading(daily_output_tokens=daily, today_pct=float(pct), used_output_tokens=used)


def _millions(tokens: int) -> str:
    return f"{tokens / 1_000_000:.1f}M"


def build_budget_request(reading: BudgetReading, day: str) -> ToastRequest:
    """Pure: a crossed reading → the toast. Body wording is the contract's, verbatim."""
    # round, not int(): 1.34 * 100 is 133.99999999999998 in binary floating point, and a
    # budget crossed by 34% must not read as 133%.
    pct = round(reading.today_pct * 100)
    body = (
        f"{_millions(reading.used_output_tokens)} of {_millions(reading.daily_output_tokens)}"
        f" output tokens ({pct}%)"
    )
    return ToastRequest(
        session_id=f"{BUDGET_ID_PREFIX}{day}",
        state_since=day,
        title=BUDGET_TITLE,
        body=body,
        actionable=False,
    )


@dataclass(frozen=True)
class BudgetDecision:
    """What the budget decider concluded for this poll. Same shape as DigestDecision."""

    #: The calendar day to mark consumed, or None to leave the day armed for a later poll.
    day: str | None = None
    request: ToastRequest | None = None
    reason: str = "idle"


class BudgetDecider:
    """Decides whether this poll owes the budget-crossed toast. Pure — the ledger is passed in.

    Rides the existing 10 s poll like the digest: no thread, no timer. There is no ``enabled``
    switch and no config block of its own — crabd emits ``burn.budget`` only when the operator
    configured one, so CONFIGURING the budget is the opt-in (contract v0.10.0). ``toast.enabled``
    governs the waiting-session toast only, the same way it does not gate the digest.
    """

    def evaluate(self, state: Any, now_local: datetime, last_day: str | None) -> BudgetDecision:
        today = now_local.date().isoformat()
        if last_day == today:
            return BudgetDecision(reason="already marked today")

        reading = read_budget(state)
        if reading is None:
            # Deliberately unmarked: no budget today is not "the budget toast happened today".
            return BudgetDecision(reason="no budget in the feed")

        if not reading.crossed:
            # Not over yet. Unmarked, so the crossing later today still toasts.
            return BudgetDecision(reason=f"under budget ({reading.today_pct:.2f})")

        if is_quiet(state):
            # Quiet hours are SILENT, not deferred — and the day is consumed, so a budget
            # crossed at 02:00 does not fire the instant quiet lifts. The widget's budget
            # line carries it in the morning, which is where a slept-through number belongs.
            return BudgetDecision(day=today, reason="quiet hours")

        return BudgetDecision(day=today, request=build_budget_request(reading, today), reason="budget crossed")


class BudgetLedger(DayLedger):
    SECTION = "budget"


# --------------------------------------------------------------------------------------
# Stale feed (v0.15.0) — the notifier watching its own stack
# --------------------------------------------------------------------------------------

STALE_TITLE = "SideCrab companion not responding"

#: Fixed tag: a second outage toast REPLACES the first in Action Center rather than stacking.
#: There is only ever one outage in progress, and one line about it is the honest count.
STALE_ID = "stale-feed"

#: How old the newest data may be before the panel is lying. The SAME number does double duty:
#: it is the age at which a served ``generatedAt`` is stale, and the dwell an UNREACHABLE crabd
#: must survive before it counts as an outage. That symmetry is the point — both mean "the
#: panel has shown nothing new for five minutes". Without the dwell, every crabd restart (a
#: 10 s blip, and a routine one) would toast, which is precisely the alert that teaches an
#: operator to ignore alerts.
STALE_FEED_MAX_AGE_SEC = 300

#: How recently the operator must have had something running for an outage to be worth saying.
#: Matches crabd's own idle threshold. Without it, a laptop that sleeps overnight with crabd
#: stopped would toast on every wake — and nobody's panel was dead, because nobody was looking
#: at it. This is also what keeps a notifier that has NEVER seen a healthy feed silent: it has
#: no evidence anyone is working, so it says nothing.
STALE_ACTIVITY_WINDOW_SEC = 900

#: States that mean a human is mid-flight. "done"/"idle" are not — a finished session on the
#: panel is not a reason to be told the panel stopped updating.
ACTIVE_STATES = frozenset({"working", "needs_input"})


def has_active_session(state: Any) -> bool:
    """Pure: does this feed carry a session the operator is actually mid-flight on?"""
    sessions = state.get("sessions") if isinstance(state, dict) else None
    if not isinstance(sessions, list):
        return False
    return any(isinstance(s, dict) and s.get("state") in ACTIVE_STATES for s in sessions)


@dataclass(frozen=True)
class FeedHealth:
    """Whether this poll's feed can be believed, and how to say why if it cannot."""

    healthy: bool
    #: The feed's own stamp when it had a usable one — None when unreachable or unstamped.
    generated_at: datetime | None
    #: A finished sentence for the toast body. Generic and public-facing: no host, no port.
    detail: str


def read_feed_health(state: Any, now: datetime) -> FeedHealth:
    """Pure: this poll's fetch result → whether the panel is being told the truth."""
    if state is None:
        return FeedHealth(False, None, "The companion is not answering.")
    if not isinstance(state, dict):
        return FeedHealth(False, None, "The companion is returning something unreadable.")

    generated = parse_iso(state.get("generatedAt"))
    if generated is None:
        # A feed answering with no usable generatedAt is odd but ALIVE, and this toast is
        # about the panel going dark. Calling a responding companion "not responding" would
        # be the false alarm this whole feature exists to avoid.
        return FeedHealth(True, None, "The companion is answering without a timestamp.")

    age = (now - generated).total_seconds()
    if age > STALE_FEED_MAX_AGE_SEC:
        return FeedHealth(False, generated, f"Its data is {int(age // 60)} min old.")
    # A stamp in the future is clock skew, not staleness. Healthy — there is nothing an
    # operator can do about it and it is not what this toast means.
    return FeedHealth(True, generated, "fresh")


def build_stale_request(detail: str, since: datetime) -> ToastRequest:
    return ToastRequest(
        session_id=STALE_ID,
        state_since=since.isoformat(),
        title=STALE_TITLE,
        body=f"{detail} The panel is showing stale data until it recovers.",
        # Nothing to acknowledge: there is no session behind this, and the endpoint the
        # Acknowledge button would POST to is the one that just stopped answering.
        actionable=False,
    )


class StaleFeedDecider:
    """One toast per outage, re-armed only by a recovery.

    This is the only toast in the file that fires when crabd is DOWN, and it is the exception
    that proves the rule the others follow. A question the operator never sees is crabd's
    problem to report; a PANEL the operator never sees stop updating is nobody's — the widget
    dims and shows "data as of HH:MM", but the operator is by definition not looking at it.
    Green dots that froze five minutes ago read exactly like green dots.

    Every piece of state here is in-process by design. A restart re-arms the toast, which is
    the safe direction: at worst one extra line about an outage that is genuinely still on.
    """

    def __init__(self) -> None:
        #: Last poll at which a HEALTHY feed showed someone mid-flight. Stale feeds never
        #: update it — re-arming from frozen content is how a dead feed keeps itself alive.
        self._last_active: datetime | None = None
        #: Quiet hours as of the last healthy poll. Remembered rather than read, because the
        #: quiet block lives in the very feed this decider cannot reach.
        self._last_quiet = False
        #: The last poll that returned a believable feed. None until one does.
        self._last_healthy: datetime | None = None
        self._fired = False

    def evaluate(self, state: Any, now: datetime) -> ToastRequest | None:
        health = read_feed_health(state, now)

        if health.healthy:
            if self._fired:
                log.info("crabd feed recovered — the outage toast is re-armed")
            self._fired = False  # ONLY a recovery re-arms
            self._last_healthy = now
            self._last_quiet = is_quiet(state)
            if has_active_session(state):
                self._last_active = now
            return None

        if self._fired:
            return None

        if self._last_active is None or (now - self._last_active).total_seconds() > STALE_ACTIVITY_WINDOW_SEC:
            # Nobody was working. Deliberately NOT marked fired: if the operator turns out to
            # have been at the machine after all, this outage has not yet spent its one toast.
            log.debug("feed unhealthy (%s) but nothing was recently active — silent", health.detail)
            return None

        # THE LAST MOMENT THE PANEL HELD THE TRUTH. Both arms measure the same quantity — a
        # served-but-frozen feed carries its own stamp, and an unreachable one is timed from
        # the last healthy poll. Timing the unreachable arm from when we first NOTICED would
        # instead restart the clock on every resume from sleep, and would answer a different
        # question ("how long have I been watching?") than the operator is asking.
        since = health.generated_at if health.generated_at is not None else self._last_healthy
        if since is None or (now - since).total_seconds() < STALE_FEED_MAX_AGE_SEC:
            return None

        self._fired = True

        if self._last_quiet:
            # Suppressed AND marked, like every other toast here. The morning's first healthy
            # poll re-arms it; an outage that has since recovered is not news at 07:00.
            log.info("quiet hours: suppressed the outage toast (%s)", health.detail)
            return None

        log.warning("crabd feed outage: %s", health.detail)
        return build_stale_request(health.detail, since)


# --------------------------------------------------------------------------------------
# Toast kinds
# --------------------------------------------------------------------------------------

#: The six kinds, in the order the README's tables list them. Used for the "muted"
#: log line, which is once per KIND per day — not once per toast, and not once per
#: attempt: a switch that is off all day would otherwise write 8,640 lines a kind.
TOAST_KINDS = ("waiting", "approval", "longrun", "digest", "budget", "outage")

#: Prefix → kind. Derived from the session id rather than carried as a field on
#: ToastRequest because the id ALREADY encodes it — `ack_uri`/`snooze_uri` and the
#: deciders' ledgers all key on these same prefixes, and a second, independent
#: discriminator is one more thing that can disagree with the first.
_KIND_BY_PREFIX = (
    (APPROVAL_ID_PREFIX, "approval"),
    (LONG_RUN_ID_PREFIX, "longrun"),
    (DIGEST_ID_PREFIX, "digest"),
    (BUDGET_ID_PREFIX, "budget"),
)


#: The aggregate line, logged once a day while the switch is off. It exists because
#: the per-kind lines below can only name what REACHED the emit seam, and the three
#: session-keyed deciders stand down earlier than that (deliberately — see _suppress).
#: Without this line an operator reading the log while muted would see the digest and
#: budget suppressions named and nothing at all about the waiting question, which
#: reads as a partial mute rather than the global one it is.
MUTED_SWITCH_LINE = (
    "toast.enabled=false — ALL toast kinds are suppressed. waiting/approval stand down "
    "before their spell is marked and re-surface when it is switched back on; "
    "longrun/digest/budget/outage consume their spell while muted and do NOT re-surface "
    "(longrun's working->done observation swap is unconditional, so a completion that "
    "finished while muted is stale), as digest/budget/outage do under quiet hours"
)


def toast_kind(request: ToastRequest) -> str:
    """Which of the six toasts this request is. Pure; never raises."""
    sid = request.session_id
    if not isinstance(sid, str):
        return "waiting"
    if sid == STALE_ID:
        return "outage"
    for prefix, kind in _KIND_BY_PREFIX:
        if sid.startswith(prefix):
            return kind
    # A bare session id is the waiting-question toast — the only one that toasts
    # under an id crabd itself issued.
    return "waiting"


# --------------------------------------------------------------------------------------
# Toast emission — the only platform-touching code, behind an adapter (one per platform)
# --------------------------------------------------------------------------------------


class ToastAdapter(Protocol):
    def show(self, request: ToastRequest) -> bool:  # pragma: no cover - protocol
        ...


class RecordingToastAdapter:
    """Test / --dry-run adapter. Records instead of showing."""

    def __init__(self, succeed: bool = True) -> None:
        self.shown: list[ToastRequest] = []
        self.succeed = succeed

    def show(self, request: ToastRequest) -> bool:
        self.shown.append(request)
        return self.succeed


_TAG_SAFE = re.compile(r"[^A-Za-z0-9_-]")

#: The toast's "Acknowledge" button activates a registered URL protocol rather than calling
#: back into this process — a toast outlives the notifier (it sits in Action Center until
#: dismissed), so the only activation that still works an hour later is one the SHELL can
#: route. setup\Register-SideCrabProtocol.ps1 registers the scheme; the handler that
#: receives it is notifier\sidecrab_ack_handler.pyw. All three must agree on these two
#: strings verbatim, which notifier/tests/test_ack_handler.py pins.
ACK_SCHEME = "sidecrab-ack"
SESSION_ID_PATTERN = r"^[A-Za-z0-9-]{1,64}$"
_SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)
ACK_BUTTON_CONTENT = "Acknowledge"


def ack_uri(session_id: Any) -> str | None:
    """The URI the Acknowledge button carries, or None for an id we will not embed.

    Validated HERE as well as in the handler, and for a different reason: the handler
    guards itself against a hostile URI, this guards the toast PAYLOAD against becoming
    one. A toast is written once and replayed by the shell for as long as it sits in
    Action Center, so an id that escaped the attribute would be a stored injection, not
    a transient one.
    """
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return None
    return f"{ACK_SCHEME}:{session_id}"


#: The Snooze button (v0.16.0). A SECOND protocol, not a second argument on the first one:
#: the ack handler's whole security story is that everything after ``sidecrab-ack:`` is one
#: session id and nothing else, and adding an action word to that URI would turn a charset
#: test into a parser. Two schemes, two handlers, one regex each.
#:
#: setup registers this alongside sidecrab-ack since v0.16.0 (Get-SideCrabProtocolSpec
#: returns both schemes).
SNOOZE_SCHEME = "sidecrab-snooze"
SNOOZE_BUTTON_CONTENT = "Snooze 30m"

#: How long a snooze holds. Fixed, not configurable: it is written on the BUTTON, and a button
#: whose label and behaviour can disagree is worse than one that cannot be tuned.
SNOOZE_SEC = 1800


def snooze_uri(session_id: Any) -> str | None:
    """The URI the Snooze button carries, or None for an id we will not embed. Same validation
    as ack_uri, and for the same reason: a toast payload is stored and replayed by the shell."""
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        return None
    return f"{SNOOZE_SCHEME}:{session_id}"

#: SideCrab's own toast identity, and the HKCU key that proves it is registered. Both must
#: match setup\Register-SideCrabAumid.ps1 exactly — the AUMID string in a CreateToastNotifier
#: call is matched against the registered one verbatim.
SIDECRAB_AUMID = "SideCrab.Notifier"
AUMID_REGISTRY_SUBKEY = r"SOFTWARE\Classes\AppUserModelId\SideCrab.Notifier"

#: How long a NEGATIVE registry answer is trusted before it is re-read. A positive answer is
#: never re-read (see registered_aumid). This exists because "borrowed" used to be permanent
#: for the process: a notifier that was already running when the installer registered the key
#: kept borrowing until someone restarted SideCrab-toast, and nothing told them to.
AUMID_REPROBE_SEC = 300.0

#: (answer, monotonic stamp) or None for "not yet probed" — the tuple keeps "cached None"
#: and "never asked" distinguishable.
_registered_aumid_cache: tuple[str | None, float] | None = None

#: Why the last probe came back empty, for the log line a human actually reads. Absent and
#: access-denied are the same return value but very different problems.
_last_aumid_probe_error: str | None = None


def aumid_probe_detail() -> str | None:
    """The last probe failure, human-readable, or None if the last probe found the key."""
    return _last_aumid_probe_error


def probe_registered_aumid() -> str | None:
    """Impure: HKCU probe. SIDECRAB_AUMID when setup registered it, else None.

    Existence of the key is the whole test — its DisplayName/IconUri are the shell's
    business, and a half-written key still yields a usable notifier identity.

    ROOT CAUSE, measured 2026-08-26 (the "logged borrowed while the key existed" mystery):
    the key did NOT exist for this process, and this function was right. HKCU registry WRITES
    made from the agent/automation shell that ran setup\\Register-SideCrabAumid.ps1 land in a
    per-session virtualized overlay; the shell reads its own writes back and reports the key
    as present, while every process outside that shell — the SideCrab-toast Scheduled Task
    included — sees the real hive without it. Proven with a write/read matrix: a key created
    by the shell was invisible to a Scheduled Task run seconds later (winerror=2), a key
    created by the task was visible to both, and NtQueryKey confirmed both processes resolve
    HKCU\\SOFTWARE\\Classes to the same \\REGISTRY\\USER\\<SID>_Classes path. So "readable from
    a shell" is NOT evidence the AUMID is registered — only a read from the notifier's own
    execution context is, which is what this function does and why its answer is now logged
    with its reason attached.
    """
    global _last_aumid_probe_error
    try:
        import winreg  # noqa: PLC0415 - Windows-only, and this module must import on any OS
    except ImportError:
        _last_aumid_probe_error = "winreg unavailable (not Windows)"
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUMID_REGISTRY_SUBKEY):
            _last_aumid_probe_error = None
            return SIDECRAB_AUMID
    except OSError as exc:
        # FileNotFoundError (not registered) and PermissionError alike: fall back, never raise
        # — but keep WHICH one, because they need opposite fixes.
        _last_aumid_probe_error = f"winerror={getattr(exc, 'winerror', None)}: {exc}"
        return None


def registered_aumid(probe: Any = None, refresh: bool = False, now: float | None = None) -> str | None:
    """Cached wrapper around the probe: not one registry read per toast.

    A positive answer latches for the process lifetime (an AUMID does not un-register in
    practice, and re-reading buys nothing). A NEGATIVE answer expires after AUMID_REPROBE_SEC
    so that registering the key while the notifier runs is enough on its own.

    An injected `probe` is never cached: tests must not be able to poison the process cache
    for each other, nor read it from a previous test.
    """
    global _registered_aumid_cache
    if probe is not None:
        return probe()

    stamp = time.monotonic() if now is None else now
    if refresh or _registered_aumid_cache is None:
        _registered_aumid_cache = (probe_registered_aumid(), stamp)
        return _registered_aumid_cache[0]

    answer, probed_at = _registered_aumid_cache
    if answer is None and stamp - probed_at >= AUMID_REPROBE_SEC:
        _registered_aumid_cache = (probe_registered_aumid(), stamp)
    return _registered_aumid_cache[0]


class PowerShellToastAdapter:
    """Fires a Windows toast via Windows PowerShell 5.1's WinRT projection. See module docstring."""

    #: Pinned to System32 by design — pwsh 7 lacks the WinRT projection (measured).
    POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

    #: Windows PowerShell's own AUMID; Get-StartApps confirms it is registered on this box.
    #: Only used when SideCrab's own is not registered — see the module docstring.
    BORROWED_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

    GROUP = "sidecrab"

    def __init__(
        self,
        aumid: str | None = None,
        icon_path: Path | None = None,
        timeout: float = 20.0,
        powershell: str | None = None,
        aumid_probe: Any = None,
    ) -> None:
        #: An explicit aumid pins the choice; None means "decide at first use".
        self._pinned_aumid = aumid
        self._aumid_probe = aumid_probe
        self._aumid: str | None = None
        self._borrow_logged = False
        self.icon_path = icon_path
        self.timeout = timeout
        self.powershell = powershell or self.POWERSHELL_EXE

    @property
    def aumid(self) -> str:
        """SideCrab's AUMID when registered, the borrowed one otherwise.

        Asymmetric on purpose: a POSITIVE answer is latched here forever, but borrowing is
        never latched — it re-asks (throttled by AUMID_REPROBE_SEC inside registered_aumid),
        so registering the key on a running notifier is enough and no restart is needed. The
        old code latched both, which is how a notifier could keep reporting "borrowed" for a
        whole logon session after the key appeared.
        """
        if self._pinned_aumid:
            return self._pinned_aumid
        if self._aumid is not None:
            return self._aumid

        found = registered_aumid(probe=self._aumid_probe)
        if found:
            self._aumid = found
            log.info("toast identity: %s (SideCrab AUMID registered)", found)
            return found

        if not self._borrow_logged:
            self._borrow_logged = True
            # The reason is load-bearing: "not registered" and "cannot read the key" look
            # identical from the fallback, and only one of them is fixed by running setup.
            log.info(
                "toast identity: %s (borrowed - run setup\\Register-SideCrabAumid.ps1; HKCU\\%s %s)",
                self.BORROWED_AUMID,
                AUMID_REGISTRY_SUBKEY,
                aumid_probe_detail() or "absent",
            )
        return self.BORROWED_AUMID

    # -- pure: XML + script construction, unit-testable without Windows -------------------

    def build_xml(self, request: ToastRequest) -> str:
        """Build the ToastGeneric payload. Every interpolated value is XML-escaped — the body
        is arbitrary user question text and WILL contain <, &, and quotes.

        Two escapes, and the difference is the context, not the trust level: element text uses
        xml_escape, anything inside an attribute uses xml_attr_escape (quotes too, or a ``'``
        closes the single-quoted attribute and the rest parses as markup)."""
        parts = [
            "<toast duration='long'>",
            "<visual><binding template='ToastGeneric'>",
            f"<text>{xml_escape(request.title)}</text>",
            f"<text>{xml_escape(request.body)}</text>",
            "<text placement='attribution'>SideCrab</text>",
        ]
        if self.icon_path is not None and self.icon_path.is_file():
            # safe="/:" is load-bearing: the default quote() escapes the drive colon to
            # "C%3A", which Windows does not resolve back to a path — the toast then renders
            # with no logo and no error. Caught in the first real toast's stored payload.
            uri = "file:///" + urllib.parse.quote(str(self.icon_path.resolve()).replace("\\", "/"), safe="/:")
            parts.append(f"<image placement='appLogoOverride' src='{xml_attr_escape(uri)}'/>")
        parts.append("</binding></visual>")
        parts.append("<audio src='ms-winsoundevent:Notification.Default'/>")

        # Both buttons live or die together: they are validated by the same regex against the
        # same id, so one present and the other missing would mean the charset test disagreed
        # with itself.
        ack = ack_uri(request.session_id) if request.actionable else None
        snooze = snooze_uri(request.session_id) if request.actionable else None
        if request.actionable and (ack is None or snooze is None):
            # The BUTTONS are refused, not the toast: a question waiting on the operator still has to
            # reach him. Dropping the whole notification because an id looked odd would
            # trade a missing convenience for a missing signal.
            log.warning(
                "session id (%d chars) fails %s - action buttons omitted",
                len(str(request.session_id)),
                SESSION_ID_PATTERN,
            )
        elif ack is not None and snooze is not None:
            # activationType='protocol' hands the URI to the shell, which starts the
            # registered handler. No <input>, so Windows renders them as plain buttons.
            # Acknowledge is FIRST: it is the answer, and Snooze is the deferral. Windows
            # renders left-to-right in document order and the leftmost button is the one hit
            # from a lock screen by someone barely reading.
            parts.append(
                "<actions>"
                f"<action activationType='protocol' content='{xml_attr_escape(ACK_BUTTON_CONTENT)}'"
                f" arguments='{xml_attr_escape(ack)}'/>"
                f"<action activationType='protocol' content='{xml_attr_escape(SNOOZE_BUTTON_CONTENT)}'"
                f" arguments='{xml_attr_escape(snooze)}'/>"
                "</actions>"
            )
        parts.append("</toast>")
        return "".join(parts)

    def build_script(self, request: ToastRequest) -> str:
        """The PowerShell to run. The toast XML travels as base64 so that no quote, brace or
        backtick in a question can escape into PowerShell source — the only alphabet that
        crosses the boundary is [A-Za-z0-9+/=]."""
        xml_b64 = base64.b64encode(self.build_xml(request).encode("utf-8")).decode("ascii")
        tag = _TAG_SAFE.sub("-", request.session_id)[:40] or "session"
        return "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                "$null = [Windows.UI.Notifications.ToastNotificationManager,"
                " Windows.UI.Notifications, ContentType = WindowsRuntime]",
                "$null = [Windows.Data.Xml.Dom.XmlDocument,"
                " Windows.Data.Xml.Dom, ContentType = WindowsRuntime]",
                "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument",
                f"$xml.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{xml_b64}')))",
                "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml",
                f"$toast.Tag = '{tag}'",
                f"$toast.Group = '{self.GROUP}'",
                f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{self.aumid}')"
                ".Show($toast)",
            ]
        )

    # -- impure: run it ------------------------------------------------------------------

    def show(self, request: ToastRequest) -> bool:
        script = self.build_script(request)
        # -EncodedCommand (UTF-16LE base64) sidesteps Windows command-line quoting entirely.
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                [self.powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                creationflags=creationflags,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("toast subprocess failed: %s", exc)
            return False

        if proc.returncode != 0:
            log.error("toast failed rc=%s: %s", proc.returncode, (proc.stderr or "").strip()[:400])
            return False
        return True


# -- macOS: the same seam, a different interpreter ---------------------------------------

MAC_OSASCRIPT = "/usr/bin/osascript"

#: The AppleScript, as three CONSTANT strings handed to osascript with -e. The notification
#: text NEVER appears here: it rides in argv, past the separator, and the script reads it back
#: with `item N of argv`.
#:
#: MEASURED on macOS 26.6: `osascript -e 'on run argv' ... -- <arg>` passes every argument
#: through byte for byte — a probe carrying a double quote, a backslash, a newline,
#: `$(touch ...)`, backticks, `&` and `; rm -rf /` came back identical, exit 0, with nothing
#: substituted or executed. That is the same property PowerShell's base64 payload buys on the
#: Windows side, obtained by not building a script out of user text at all.
#:
#: TRAP: interpolating the title or the body into this line is a one-character-looking change
#: that reintroduces AppleScript injection — a `"` in a question would close the string
#: literal. notifier/tests/test_mac_adapter.py asserts these three strings are byte-identical
#: whatever the request says.
MAC_SCRIPT_ON_RUN = "on run argv"
MAC_SCRIPT_DISPLAY_LINE = (
    "display notification (item 1 of argv) with title (item 2 of argv)"
    ' subtitle (item 3 of argv) sound name "default"'
)
MAC_SCRIPT_END_RUN = "end run"

#: The subtitle every notification carries. It is the macOS seat of the Windows payload's
#: `<text placement='attribution'>SideCrab</text>`, and it earns its place here: a
#: notification posted through osascript is attributed to Script Editor, so this line is the
#: only thing on screen that says which product raised it.
MAC_SUBTITLE = "SideCrab"

#: The composed-title budget. NOT TITLE_TRIM: that one caps the session LABEL, and a
#: notification title is a composed line ("Claude is waiting — <label>", "Finished after
#: 12h 34m — <label>"), so capping the composed line at 48 would eat the label the
#: notification exists to name. Twice the label budget clears the longest prefix this file
#: builds and still bounds the argument.
MAC_TITLE_TRIM = TITLE_TRIM * 2


def _mac_argument(value: Any, limit: int) -> str:
    """One osascript argument: control bytes stripped, and capped at an existing budget.

    Under budget the text is VERBATIM — a newline or a tab in a question is content, and the
    argv boundary carries it. Over budget the existing trim() makes the cut, so an argument
    that no decider trimmed ends up worded exactly like one that did.
    """
    text = strip_control(value)
    return text if len(text) <= limit else trim(text, limit)


def notification_text(request: ToastRequest) -> tuple[str, str, str]:
    """Pure: one request → the three positional arguments, in argv order (body, title, subtitle).

    Body first because `display notification` takes the body as its direct object. The session
    label is already inside `request.title` (build_request composes "Claude is waiting — …"),
    and the approval hint is already at the end of `request.body` (APPROVAL_BODY_TRIM reserves
    it out of the budget), so neither is moved here.
    """
    return (
        _mac_argument(request.body, BODY_TRIM),
        _mac_argument(request.title, MAC_TITLE_TRIM),
        _mac_argument(MAC_SUBTITLE, MAC_TITLE_TRIM),
    )


def run_osascript(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """The default runner: a LIST argv, never `shell=True`. Impure, and the only I/O below."""
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    return proc.returncode, proc.stdout, proc.stderr


class MacNotificationAdapter:
    """Posts a macOS notification through osascript. Same one-method contract as the Windows
    adapter, and the same promise: `show` returns a bool and never raises.

    `runner` is the test seam — `(argv, timeout) -> (returncode, stdout, stderr)`.
    """

    def __init__(
        self,
        osascript: str = MAC_OSASCRIPT,
        #: Under DEFAULT_INTERVAL_SEC (10 s) on purpose: show() runs ON the poll thread, so
        #: this timeout is the poll loop's worst case. The first-run permission dialog is
        #: exactly the wedge that would otherwise hold up the poll that notices the NEXT
        #: waiting question. Half the interval leaves the cadence the loop's own.
        timeout: float = 5.0,
        runner: Any = None,
    ) -> None:
        self.osascript = osascript
        self.timeout = timeout
        self.runner = runner or run_osascript
        #: One outage, one ERROR. See _log_failure.
        self._failure_logged = False

    def _log_failure(self, message: str, *args: Any) -> None:
        """The first failure at ERROR, every repeat at DEBUG until one lands.

        Same latch idiom as PowerShellToastAdapter's borrow line, and a sharper reason for it:
        a first-run permission denial with a question already waiting fails on EVERY 10 s
        poll. Unlatched that is 8,640 lines a day into a 512 KB rotating log, and the line it
        rotates away is the FIRST one — the one that says what went wrong. A notification that
        lands re-arms it, so a second, later outage is news again.
        """
        if self._failure_logged:
            log.debug(message, *args)
            return
        self._failure_logged = True
        log.error(message, *args)

    def build_argv(self, request: ToastRequest) -> list[str]:
        """Pure: the whole command line. Three -e script constants, `--`, three arguments."""
        body, title, subtitle = notification_text(request)
        return [
            self.osascript,
            "-e",
            MAC_SCRIPT_ON_RUN,
            "-e",
            MAC_SCRIPT_DISPLAY_LINE,
            "-e",
            MAC_SCRIPT_END_RUN,
            # Everything after this is data. osascript stops reading options here, so a body
            # that starts with "-e" is text and not a fourth script line.
            "--",
            body,
            title,
            subtitle,
        ]

    def show(self, request: ToastRequest) -> bool:
        try:
            returncode, _stdout, stderr = self.runner(self.build_argv(request), self.timeout)
        except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError, ValueError) as exc:
            # TimeoutExpired is a SubprocessError; naming it first says which one is expected
            # — osascript can sit behind the operator's one-time permission dialog.
            #
            # ValueError is NOT paranoia: subprocess refuses an argument it cannot encode, and
            # both refusals are ValueError subclasses raised while converting the argv (before
            # any process starts). A NUL is stripped upstream; a LONE SURROGATE is not — a
            # JSON "\ud800" escape decodes to one, it is legal in a Python str, and crabd
            # serves whatever the transcript held. Without this clause that request reaches
            # the daemon as a raise where the contract promises a bool.
            self._log_failure("notification subprocess failed: %s", exc)
            return False

        if returncode != 0:
            # The code and osascript's own complaint, and NOT the notification text: this log
            # is a file on disk that outlives the notification, and the question the operator
            # asked belongs on his screen rather than in it.
            #
            # 120 characters: osascript's own errors are ONE line of 65-72 characters, measured
            # here across a syntax error (-2740), a runtime error and an unknown application
            # (both -1728), each ending in the OSA error number that identifies it. 120 keeps a
            # whole message with room to spare and still bounds a runaway one. (The Windows
            # adapter cuts at 400 because PowerShell exception text is multi-line.)
            self._log_failure(
                "notification failed rc=%s: %s", returncode, (stderr or "").strip()[:120]
            )
            return False

        # A landed notification re-arms the ERROR line: the latch describes one outage, and
        # the next one is news again.
        self._failure_logged = False
        return True


class UnsupportedPlatformAdapter:
    """The honest answer on a platform with no notification route.

    Handing a Linux operator the Windows adapter would fail too, but it would fail by logging
    `C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe` at him — a path describing
    a machine he does not have, which reads as a broken install rather than an unsupported
    platform. This says the true thing, names his platform, and returns the False the daemon
    already knows how to handle.

    The request is not even read: there is no route to build a payload for, so the
    notification text never leaves the process.
    """

    def __init__(self, sys_platform: str) -> None:
        self.sys_platform = sys_platform
        #: One line, then silence — the same latch, and the same reason, as
        #: MacNotificationAdapter._log_failure. This one can never clear: nothing about a
        #: missing platform route changes while the process runs.
        self._logged = False

    def show(self, request: ToastRequest) -> bool:
        del request
        if self._logged:
            log.debug("no notification route on %s", self.sys_platform)
            return False
        self._logged = True
        log.error("no notification route on %s", self.sys_platform)
        return False


def pick_adapter(sys_platform: str, icon_path: Path | None) -> ToastAdapter:
    """Pure: the platform string → the adapter that can post on it.

    Takes the platform as an ARGUMENT rather than reading sys.platform, so the decision is
    testable from either OS. `main` passes sys.platform, and every --test-* flag below it then
    fires through the right adapter for free.

    Three answers, not two: a platform with no route says so in its own terms rather than
    borrowing a Windows failure — see UnsupportedPlatformAdapter.
    """
    if sys_platform == "darwin":
        return MacNotificationAdapter()
    if sys_platform.startswith("win"):
        return PowerShellToastAdapter(icon_path=icon_path)
    return UnsupportedPlatformAdapter(sys_platform)


# --------------------------------------------------------------------------------------
# The daemon
# --------------------------------------------------------------------------------------


def fetch_state(endpoint: str, timeout: float = FETCH_TIMEOUT_SEC) -> Any | None:
    """GET the feed. crabd being down is normal and must be SILENT — log only, never toast."""
    try:
        with urllib.request.urlopen(endpoint, timeout=timeout) as resp:  # noqa: S310 - fixed localhost
            payload = resp.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.debug("crabd unreachable: %s", exc)
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        log.warning("crabd returned unparseable JSON: %s", exc)
        return None


#: Top-level key in STATE_PATH carrying which module is RUNNING. Nothing reads it to decide
#: anything — it exists so setup\\Test-SideCrab.ps1 and Repair-SideCrab can compare the running
#: version against __version__ on disk and say "restart the task", instead of a green
#: "task: Running" row over a process executing a file that changed hours ago.
RUNTIME_SECTION = "notifier"

#: How often the stamp's lastPollAt is refreshed. Not every poll: this file is read by the
#: snooze path and rewritten atomically, and a write every 10 s buys nothing over a write every
#: 15 min. What it buys at all is liveness — a version with no recent poll behind it describes
#: a process that has since died, and reporting THAT as the running version is the same lie in
#: a different direction.
RUNTIME_STAMP_REFRESH_SEC = 900.0


class RuntimeStamp:
    """Writes "this module, this version, this process, still polling" into STATE_PATH.

    Best effort throughout, like every other writer here: a daemon must not die over a state
    file, and a missing stamp degrades to exactly the situation that existed before v0.16.0.
    """

    def __init__(self, path: Path = STATE_PATH, module: Path | None = None) -> None:
        self.path = path
        self.module = module or Path(__file__).resolve()
        self._started = datetime.now(timezone.utc)
        self._last_write = 0.0
        self._warned = False

    def _payload(self, now_utc: datetime) -> dict:
        return {
            "version": __version__,
            # The PATH, not just the version. "Which file is this process executing?" is the
            # other half of the stale-code question, and a repo cloned twice answers it
            # differently from the version string alone.
            "module": str(self.module),
            "pid": os.getpid(),
            "startedAt": self._started.isoformat(),
            "lastPollAt": now_utc.isoformat(),
        }

    def touch(self, now: datetime | None = None, monotonic: float | None = None, force: bool = False) -> bool:
        """Refresh the stamp if it is due. True when it was written this call."""
        stamp = time.monotonic() if monotonic is None else monotonic
        if not force and self._last_write and stamp - self._last_write < RUNTIME_STAMP_REFRESH_SEC:
            return False
        self._last_write = stamp
        ok = write_state_section(
            self.path, RUNTIME_SECTION, self._payload(now or datetime.now(timezone.utc))
        )
        if ok:
            self._warned = False
        elif not self._warned:
            self._warned = True
            log.warning(
                "cannot record the running version in %s — Test-SideCrab cannot tell "
                "running-from-disk for this process",
                self.path,
            )
        return ok


class Notifier:
    def __init__(
        self,
        adapter: ToastAdapter,
        endpoint: str = DEFAULT_ENDPOINT,
        interval: float = DEFAULT_INTERVAL_SEC,
        config_reader: ConfigReader | None = None,
        decider: ToastDecider | None = None,
        digest_decider: DigestDecider | None = None,
        digest_ledger: DigestLedger | None = None,
        budget_decider: BudgetDecider | None = None,
        budget_ledger: BudgetLedger | None = None,
        approval_decider: ApprovalDecider | None = None,
        stale_decider: StaleFeedDecider | None = None,
        long_run_decider: LongRunDecider | None = None,
        snooze_ledger: SnoozeLedger | None = None,
        runtime_stamp: RuntimeStamp | None = None,
    ) -> None:
        self.adapter = adapter
        self.endpoint = endpoint
        self.interval = interval
        self.config_reader = config_reader or ConfigReader()
        self.decider = decider or ToastDecider()
        self.digest_decider = digest_decider or DigestDecider()
        self.digest_ledger = digest_ledger or DigestLedger()
        self.budget_decider = budget_decider or BudgetDecider()
        self.budget_ledger = budget_ledger or BudgetLedger()
        self.approval_decider = approval_decider or ApprovalDecider()
        self.stale_decider = stale_decider or StaleFeedDecider()
        self.long_run_decider = long_run_decider or LongRunDecider()
        self.snooze_ledger = snooze_ledger or SnoozeLedger()
        self.runtime_stamp = runtime_stamp or RuntimeStamp()
        self._schema_warned: set[Any] = set()
        #: kind -> the local day its "muted" line was already logged.
        self._mute_logged: dict[str, str] = {}

    def _emit(
        self,
        owed: list[ToastRequest],
        owners: dict[int, Any] | None = None,
        config: ToastConfig | None = None,
        now: datetime | None = None,
    ) -> list[ToastRequest]:
        """Show what is owed. THE global mute lives here, and only here.

        `toast.enabled` is labelled "Desktop Toast Alerts" in the panel, so it has
        to mean all of them. It used to be checked inside three of the six deciders
        (waiting, approval, long-run), which left the digest, the budget and the
        outage toast firing under a switch the operator had turned off — a switch
        that secretly exempts categories is a worse failure than one that is too
        blunt. Gating at the single point every toast passes through is what makes
        "all" structural rather than a list somebody has to remember to extend.

        The three deciders keep their own gate as well, and that is deliberate, not
        redundancy: theirs suppresses BEFORE the spell is marked, which is what
        makes a live question re-surface when the switch comes back on. This gate
        is the backstop for the other three and for anything added later.

        `config=None` means "no opinion, do not mute" — the direct-call test seam.
        It can never suppress by accident.
        """
        owners = owners or {}
        if config is not None and not config.enabled:
            return self._suppress(owed, owners, now or datetime.now(timezone.utc))
        fired: list[ToastRequest] = []
        for request in owed:
            log.info("toasting session=%s since=%s title=%r", request.session_id, request.state_since, request.title)
            # EVERY failure shape lands in one place, and the per-request guard is what makes
            # that true (v0.18.0). show() returning False is only ONE of the ways a render can
            # fail: an exception thrown by the adapter — build_xml/build_script run OUTSIDE
            # PowerShellToastAdapter.show's own try, so a payload the builder cannot construct
            # RAISES rather than returning False — used to escape this loop entirely. Measured
            # 2026-08-27, three matured waiting questions in one poll and a raise on the first:
            # zero un-resolved (all three consumed forever) AND the other two never even
            # attempted. The batch is now attempt-independent: one poisoned request costs its
            # own toast and nothing else.
            #
            # Exception, not BaseException: KeyboardInterrupt/SystemExit must still stop the
            # daemon. The guard spans the whole body so a failure in the logging or the
            # re-arm cannot abort the rest of the batch either.
            try:
                shown = self.adapter.show(request)
            except Exception:  # noqa: BLE001 - one bad render must not cost the whole poll
                log.exception("toast emission raised for session=%s", request.session_id)
                shown = False
            if shown:
                fired.append(request)
                continue
            log.error("toast emission failed for session=%s", request.session_id)
            owner = owners.get(id(request))
            if owner is not None:
                # F1: a failed render must not consume the spell. Re-arm so the next poll
                # retries. Only the waiting-question and approval deciders register an
                # owner here — the two toasts that carry a live, actionable signal whose
                # silent loss is the bug. The digest/budget/outage/long-run toasts
                # deliberately consume-on-attempt (a low-value periodic or after-the-fact
                # toast re-firing every 10 s is worse than one missed), and keep that
                # documented behaviour. The full decider x failure-shape matrix, including
                # which cells are deliberate, is in notifier/README.md.
                owner.unresolve(request)
        return fired

    def _suppress(
        self, owed: list[ToastRequest], owners: dict[int, Any], now: datetime
    ) -> list[ToastRequest]:
        """The muted path. Nothing is shown; consumption is left exactly as it was.

        Spell semantics are deliberately NOT special-cased here. A periodic that was
        going to consume its day still has — its decider marked the day before this
        point, which is the same rule quiet hours follow ("suppress AND mark, never
        defer"). A live signal re-arms, through the same `unresolve` a failed render
        uses, so the question is still owed when the switch comes back on. In
        practice the live-signal deciders never reach here (they gate themselves
        earlier and never mark at all); the re-arm is what keeps that true for any
        future decider that registers an owner.
        """
        # .astimezone() with no argument is the system local zone — the same local day
        # the digest and budget ledgers key on, so "once per day" means one day.
        for request in owed:
            # Per-request guard, for the same reason the show loop grew one in
            # v0.18.0: one poisoned request must cost its own toast and nothing
            # else. An owner's unresolve() is third-party code from this loop's
            # point of view, and a raise here would abandon the rest of the batch
            # — re-arming some spells and silently spending the others.
            try:
                kind = toast_kind(request)
                self._log_muted_once(
                    kind, now,
                    f"toast.enabled=false — {kind} toast suppressed (once per kind per day, "
                    "not per toast)",
                )
                owner = owners.get(id(request))
                if owner is not None:
                    owner.unresolve(request)
            except Exception:  # noqa: BLE001 - one bad request must not cost the poll
                log.exception("muted-toast bookkeeping raised for session=%s", request.session_id)
        return []

    def _log_muted_once(self, kind: str, now: datetime, message: str) -> None:
        """One line per kind per LOCAL DAY. The notifier polls every 10 s, so a
        switch left off for a day is 8,640 polls; per-attempt logging would bury
        the log it exists to explain. Per-day rather than per-process because this
        daemon runs for weeks — a per-process line goes silent on exactly the box
        whose behaviour needs explaining.

        `.astimezone()` with no argument is the system local zone, the same day
        the digest and budget ledgers key on, so "once a day" means one day.
        """
        day = now.astimezone().strftime("%Y-%m-%d")
        if self._mute_logged.get(kind) == day:
            return
        self._mute_logged[kind] = day
        log.info("%s", message)

    def poll_once(self, now: datetime | None = None) -> list[ToastRequest]:
        state = fetch_state(self.endpoint)
        now = now or datetime.now(timezone.utc)
        # Read BEFORE the stale decider and ahead of every early return: the outage
        # toast can fire on a poll that never gets as far as the old read site, and
        # it is one of the three the switch used not to reach.
        config = self.config_reader.read()
        if not config.enabled:
            self._log_muted_once("switch", now, MUTED_SWITCH_LINE)

        # FIRST, and ahead of every early return below: the stale-feed decider is the one
        # consumer that has something to say precisely when the fetch failed or the schema is
        # unreadable. Running it after those returns would make it unreachable in exactly the
        # cases it exists for.
        owed: list[ToastRequest] = []
        # id(request) -> the decider to re-arm if its render fails. Only the two deciders whose
        # toast carries a live actionable signal (waiting question, permission) register here.
        owners: dict[int, Any] = {}
        outage = self.stale_decider.evaluate(state, now)
        if outage is not None:
            owed.append(outage)

        if state is None:
            # crabd unreachable. The digest and the budget toast deliberately do NOT run (and
            # so do not consume their day) — a restarting crabd costs a poll, not the day's
            # notifications.
            return self._emit(owed, owners, config, now)

        schema = state.get("schema") if isinstance(state, dict) else None
        if schema not in SUPPORTED_SCHEMAS:
            if schema not in self._schema_warned:
                self._schema_warned.add(schema)
                log.warning("unsupported feed schema %r — standing down until it changes", schema)
            return self._emit(owed, owners, config, now)

        waiting = self.decider.evaluate(state, now, config, self.snooze_ledger.read())
        for request in waiting:
            owners[id(request)] = self.decider
        owed.extend(waiting)
        approvals = self.approval_decider.evaluate(state, now, config)
        for request in approvals:
            owners[id(request)] = self.approval_decider
        owed.extend(approvals)
        owed.extend(self.long_run_decider.evaluate(state, now, config))
        owed.extend(self._digest_due(state, now))
        owed.extend(self._budget_due(state, now))
        return self._emit(owed, owners, config, now)

    def _digest_due(self, state: Any, now: datetime) -> list[ToastRequest]:
        """The scheduler, riding this poll. Marks the day BEFORE showing: a toast that fails
        to render must not re-fire every 10 s for the rest of the day."""
        digest_config = self.config_reader.read_digest()
        # .astimezone() with no argument is the system local zone — the contract's "configured
        # local time", and the same zone crabd builds recap.week's day keys in.
        decision = self.digest_decider.evaluate(
            state, now.astimezone(), digest_config, self.digest_ledger.last_day()
        )
        if decision.day is None:
            return []
        self.digest_ledger.mark(decision.day)
        if decision.request is None:
            log.info("digest %s skipped: %s", decision.day, decision.reason)
            return []
        log.info("digest %s due: %r", decision.day, decision.request.body)
        return [decision.request]

    def _budget_due(self, state: Any, now: datetime) -> list[ToastRequest]:
        """The budget crossing, riding this poll. Marks the day BEFORE showing, same reason as
        the digest: a toast that fails to render must not re-fire every 10 s until midnight."""
        # .astimezone() with no argument is the system local zone — the same local day crabd
        # computes todayPct against, and the same one the digest keys on.
        decision = self.budget_decider.evaluate(state, now.astimezone(), self.budget_ledger.last_day())
        if decision.day is None:
            return []
        self.budget_ledger.mark(decision.day)
        if decision.request is None:
            log.info("budget %s skipped: %s", decision.day, decision.reason)
            return []
        log.info("budget %s crossed: %r", decision.day, decision.request.body)
        return [decision.request]

    def run(self, stop: threading.Event) -> None:
        log.info(
            "notifier started: v%s endpoint=%s interval=%.1fs", __version__, self.endpoint, self.interval
        )
        # Written BEFORE the first poll, and forced: a notifier that never fires a toast (the
        # ordinary quiet day) must still be identifiable, and the pre-v0.16.0 ledger only
        # existed once a digest or budget toast had fired. On this box it did not exist at all.
        self.runtime_stamp.touch(force=True)
        # Resolve the toast identity NOW rather than at the first toast. The 2026-08-26
        # investigation cost a day precisely because the log said nothing about which AUMID
        # this process had until something happened to toast. Safe to do eagerly only because
        # a borrowed answer no longer latches (see PowerShellToastAdapter.aumid).
        getattr(self.adapter, "aumid", None)
        while not stop.is_set():
            try:
                self.poll_once()
                # Inside the try, and after the poll: the stamp's lastPollAt is a claim that
                # this process is still polling, and it must not be able to outlive one.
                self.runtime_stamp.touch()
            except Exception:  # noqa: BLE001 - a daemon must outlive any single bad poll
                log.exception("poll failed")
            stop.wait(self.interval)
        log.info("notifier stopped")


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def setup_logging(verbose: bool, log_path: Path | None = LOG_PATH) -> None:
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")

    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=512_000, backupCount=3, encoding="utf-8"
            )
            handler.setFormatter(fmt)
            log.addHandler(handler)
        except OSError as exc:  # a hidden task with no log is still better than no notifier
            print(f"sidecrab-notifier: cannot open log {log_path}: {exc}", file=sys.stderr)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    log.addHandler(stream)


def default_icon() -> Path | None:
    icon = Path(__file__).with_name("sidecrab.png")
    return icon if icon.is_file() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SideCrab waiting-session toast notifier.")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC)
    parser.add_argument("--once", action="store_true", help="evaluate a single poll and exit")
    parser.add_argument("--dry-run", action="store_true", help="decide but never show a toast")
    parser.add_argument("--test-toast", action="store_true", help="fire one sample toast and exit")
    parser.add_argument(
        "--test-digest",
        action="store_true",
        help="fire one digest toast from the live feed and exit (ignores the schedule; never marks the ledger)",
    )
    parser.add_argument(
        "--test-budget",
        action="store_true",
        help="fire one budget-crossed toast from the live feed and exit "
        "(ignores the 100%% gate; never marks the ledger)",
    )
    parser.add_argument(
        "--test-approval",
        action="store_true",
        help="fire one approval toast from the live feed and exit "
        "(ignores the threshold; never marks the dedupe ledger)",
    )
    parser.add_argument(
        "--test-stale",
        action="store_true",
        help="fire one companion-outage toast and exit (ignores the dwell and the activity window)",
    )
    parser.add_argument(
        "--test-longrun",
        action="store_true",
        help="fire one long-run completion toast and exit (ignores the threshold and the ledger)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the running module's version and path, then exit",
    )
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    parser.add_argument(
        "--state",
        type=Path,
        default=STATE_PATH,
        help="the notifier's own state file (digest/budget day ledgers, snooze marks, runtime stamp)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    if args.version:
        # Before setup_logging: this answer goes to stdout clean, so `python sidecrab_toast.py
        # --version` is parseable by the setup lane without stripping a log preamble. Two lines
        # because "which version" and "which file" are two different stale-code questions.
        print(f"sidecrab-notifier {__version__}")
        print(f"module: {Path(__file__).resolve()}")
        return 0

    setup_logging(args.verbose)
    # EVERY invocation says which module it is, not just the daemon: a --test-toast run that
    # proves the toast path is evidence about a specific version of this file or it is evidence
    # about nothing. This line is the one that makes a stale Scheduled Task visible in the log.
    log.info("sidecrab notifier v%s from %s", __version__, Path(__file__).resolve())

    # THE one construction site. Every --test-* branch below reads `adapter`, so the platform
    # is decided once, here, and never again further down.
    real = pick_adapter(sys.platform, default_icon())
    adapter: ToastAdapter = RecordingToastAdapter() if args.dry_run else real

    if args.test_toast:
        request = ToastRequest(
            session_id="test-toast",
            state_since=datetime.now(timezone.utc).isoformat(),
            title="Claude is waiting \u2014 SideCrab notifier self-test",
            body="If you can read this, the toast path works. Nothing is waiting on you.",
        )
        ok = adapter.show(request)
        log.info("test toast %s", "SHOWN" if ok else "FAILED")
        return 0 if ok else 1

    if args.test_digest:
        # Proves the digest's XML/AUMID path end to end without waiting for the clock and
        # without consuming the day: the ledger is never touched here.
        yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
        row = find_week_row(fetch_state(args.endpoint), yesterday)
        request = build_digest_request(row, yesterday) if row is not None else None
        if request is None:
            log.warning("no recap.week row for %s — firing a placeholder body instead", yesterday)
            request = ToastRequest(
                session_id=f"{DIGEST_ID_PREFIX}{yesterday}",
                state_since=yesterday,
                title=DIGEST_TITLE,
                body="no recap data for yesterday",
                actionable=False,
            )
        log.info("test digest %s: %r", yesterday, request.body)
        ok = adapter.show(request)
        log.info("test digest %s", "SHOWN" if ok else "FAILED")
        return 0 if ok else 1

    if args.test_budget:
        # Proves the budget toast's XML/AUMID path end to end without waiting for a real
        # crossing, and without consuming the day: the ledger is never touched here.
        today = datetime.now().date().isoformat()
        state = fetch_state(args.endpoint)
        reading = read_budget(state)
        if reading is None:
            # An older crabd emits no burn.budget at all. Rather than invent both numbers,
            # keep today's MEASURED output tokens and label the budget half as a sample — the
            # toast then still proves the real formatting on real spend.
            burn = state.get("burn") if isinstance(state, dict) else None
            today_block = burn.get("today") if isinstance(burn, dict) else None
            used = _positive_int(today_block.get("outputTokens")) if isinstance(today_block, dict) else None
            used = used or 6_700_000
            daily = max(100_000, int(used / 1.34))
            log.warning("feed carries no burn.budget — using a SAMPLE budget of %d", daily)
            request = build_budget_request(
                BudgetReading(daily_output_tokens=daily, today_pct=used / daily, used_output_tokens=used), today
            )
            request = ToastRequest(
                session_id=request.session_id,
                state_since=request.state_since,
                title=request.title,
                body=f"{request.body} · sample budget",
                actionable=False,
            )
        else:
            request = build_budget_request(reading, today)
        log.info("test budget %s: %r", today, request.body)
        ok = adapter.show(request)
        log.info("test budget %s", "SHOWN" if ok else "FAILED")
        return 0 if ok else 1

    if args.test_approval:
        # Proves the approval toast's XML/AUMID path end to end without having to catch a real
        # request inside its 55 s window, and without marking the ledger.
        state = fetch_state(args.endpoint)
        sessions = state.get("sessions") if isinstance(state, dict) else None
        pending = None
        for session in sessions if isinstance(sessions, list) else []:
            pending = read_pending_permission(session)
            if pending is not None:
                break
        if pending is None:
            log.warning("no session is carrying a pendingPermission — firing a SAMPLE request instead")
            pending = PendingPermission(
                session_id="test-approval",
                tool="Bash",
                summary="git push --force origin master · sample",
                requested_at=datetime.now(timezone.utc).isoformat(),
            )
        request = build_approval_request(pending)
        log.info("test approval %s: %r", pending.session_id, request.body)
        ok = adapter.show(request)
        log.info("test approval %s", "SHOWN" if ok else "FAILED")
        return 0 if ok else 1

    if args.test_stale:
        # Deliberately does NOT go through StaleFeedDecider: reproducing a real outage would
        # mean stopping crabd, and this flag exists to prove the toast renders, not to prove
        # the gates hold (the tests do that, and can do it without breaking the operator's
        # own panel). The DETAIL is read live, so a genuinely unhealthy feed says so.
        health = read_feed_health(fetch_state(args.endpoint), datetime.now(timezone.utc))
        detail = "The companion is not answering." if health.healthy else health.detail
        request = build_stale_request(detail, datetime.now(timezone.utc))
        log.info("test stale (feed is currently %s): %r", "healthy" if health.healthy else "unhealthy", request.body)
        ok = adapter.show(request)
        log.info("test stale %s", "SHOWN" if ok else "FAILED")
        return 0 if ok else 1

    if args.test_longrun:
        # Deliberately does NOT go through LongRunDecider: reproducing a real 15-minute turn
        # would mean waiting fifteen minutes for a rendering check. The SESSION is real — the
        # first one the live feed carries — so the title trimming is proven on a real title.
        state = fetch_state(args.endpoint)
        sessions = state.get("sessions") if isinstance(state, dict) else None
        rows = [s for s in sessions if isinstance(s, dict) and isinstance(s.get("id"), str)] if isinstance(sessions, list) else []
        row = rows[0] if rows else {"id": "test-longrun", "title": "SideCrab notifier self-test"}
        if not rows:
            log.warning("the feed carries no session — firing a SAMPLE long-run toast instead")
        finished = datetime.now(timezone.utc)
        started = finished - timedelta(seconds=22 * 60 + 14)
        request = build_long_run_request(row, str(row.get("id")), started, finished)
        log.info("test long-run %s: %r / %r", row.get("id"), request.title, request.body)
        ok = adapter.show(request)
        log.info("test long-run %s", "SHOWN" if ok else "FAILED")
        return 0 if ok else 1

    notifier = Notifier(
        adapter=adapter,
        endpoint=args.endpoint,
        interval=args.interval,
        config_reader=ConfigReader(args.config),
        digest_ledger=DigestLedger(args.state),
        budget_ledger=BudgetLedger(args.state),
        snooze_ledger=SnoozeLedger(args.state),
        runtime_stamp=RuntimeStamp(args.state),
    )

    if args.once:
        fired = notifier.poll_once()
        log.info("single poll fired %d toast(s)", len(fired))
        return 0

    stop = threading.Event()
    try:
        notifier.run(stop)
    except KeyboardInterrupt:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
