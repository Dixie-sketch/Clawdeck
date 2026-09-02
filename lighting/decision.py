"""Pure alert-decision logic for sidecrab-glow.

No SDK, no network, no clock of its own: `decide()` takes a parsed `/v1/state`
document (or None when the fetch failed) plus an explicit `now`, and returns what
the lights should do. That is what makes it unit-testable headless.

Contract fields consumed (docs/STATE-CONTRACT.md): `schema`, `generatedAt`,
`sessions[].state`, `sessions[].acked`, `sessions[].stateSince`, `quiet.active`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

# Schema 1, 2 and 3 all carry what we need; anything else is a dead feed, not a
# feed we half-understand. (v1 has no `acked`/`quiet` — absent reads as v1
# behavior: nothing acked, no quiet hours.)
# Every published schema is additive; the number marks the last BREAKING shape (see
# docs/STATE-CONTRACT.md "VERSIONING REWORK"). This consumer reads only fields common to
# all schemas. A set that lags the contract goes silently dark - the notifier shipped that
# exact bug (Running task, standing down every poll), so the test pins this against the
# contract document itself.
ACCEPTED_SCHEMAS = frozenset({1, 2, 3, 4, 5})

# The contract's own dead-feed rule: the widget dims at generatedAt older than 30 s.
# A frozen crabd must not leave the room pulsing forever, so we use the same line.
STALE_AFTER_SEC = 30.0

# "Subtle escalation": an unacked question older than this brightens the pulse.
ESCALATE_AFTER_SEC = 300.0

LEVEL_NONE = "none"
LEVEL_NORMAL = "normal"
LEVEL_ESCALATED = "escalated"


@dataclass(frozen=True)
class GlowDecision:
    should_glow: bool
    level: str
    reason: str
    alert_count: int = 0
    oldest_age_sec: float = 0.0


def _parse_iso(value):
    """ISO-8601 (incl. a trailing Z) → aware UTC datetime, or None. Never raises on junk."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    # datetime.fromisoformat only learned to parse a literal `Z` in 3.11, and crabd emits
    # EVERY timestamp with one. On an older interpreter the bare fromisoformat below would
    # raise on every feed stamp -> None -> "feed-no-timestamp", leaving the glow permanently
    # dark against a perfectly healthy feed. Strip it the way the notifier's parse_iso does,
    # so the light is robust to the runtime Python rather than silently version-gated.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _age_sec(value, now):
    dt = _parse_iso(value)
    if dt is None:
        return None
    return (now - dt).total_seconds()


def decide(doc, now=None):
    """Decide whether to glow, and how hard.

    `doc` is the parsed /v1/state document, or None when the poll failed.
    Order matters: dead feed beats quiet hours beats alerts. A no-glow decision
    always means "release the lights back to iCUE", never "hold the last frame".
    """
    if now is None:
        now = datetime.now(timezone.utc)

    if doc is None:
        return GlowDecision(False, LEVEL_NONE, "feed-unreachable")

    if not isinstance(doc, dict):
        return GlowDecision(False, LEVEL_NONE, "feed-malformed")

    if doc.get("schema") not in ACCEPTED_SCHEMAS:
        return GlowDecision(False, LEVEL_NONE, "feed-schema-unsupported")

    # A served-but-frozen feed is a dead feed. Missing/unparseable generatedAt is
    # treated the same way rather than trusted — silence must not read as green.
    age = _age_sec(doc.get("generatedAt"), now)
    if age is None:
        return GlowDecision(False, LEVEL_NONE, "feed-no-timestamp")
    if age > STALE_AFTER_SEC:
        return GlowDecision(False, LEVEL_NONE, "feed-stale")

    quiet = doc.get("quiet")
    if isinstance(quiet, dict) and quiet.get("active"):
        # Quiet hours suppress the light entirely — the question keeps waiting,
        # the room does not shout about it.
        return GlowDecision(False, LEVEL_NONE, "quiet-hours")

    sessions = doc.get("sessions")
    if not isinstance(sessions, list):
        return GlowDecision(False, LEVEL_NONE, "feed-no-sessions")

    ages = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        if s.get("state") != "needs_input":
            continue
        if s.get("acked"):  # absent/None/False all mean unacked
            continue
        a = _age_sec(s.get("stateSince"), now)
        ages.append(max(0.0, a) if a is not None else 0.0)

    if not ages:
        return GlowDecision(False, LEVEL_NONE, "no-unacked-needs-input")

    oldest = max(ages)
    level = LEVEL_ESCALATED if oldest >= ESCALATE_AFTER_SEC else LEVEL_NORMAL
    return GlowDecision(True, level, "needs-input", len(ages), oldest)
