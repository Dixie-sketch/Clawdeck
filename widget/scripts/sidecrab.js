/* SideCrab widget runtime — consumes /v1/state (schema 1–5) from crabd on
   loopback. docs/STATE-CONTRACT.md is authoritative; this file must not invent
   fields.

   VERSIONING (rework, v0.6.1): `schema` marks the last BREAKING shape, NOT the
   feature level. Every additive field — contextTokens, fleet, recap, byModel,
   events, daily — is found by FIELD PRESENCE and renders as its absent-behaviour
   when missing, so crabd may ship new fields under the same number and this
   widget simply lights them up. NOTHING in this file may gate behaviour on a
   schema NUMBER comparison; a number above the ceiling is a real break and stays a
   dead feed. The lesson that bought this: the companion and the page are updated
   separately, so a schema bump the page did not get bricked the glass until
   somebody stood at the desk. The page is served by the companion now, which makes
   that less likely and not impossible - the rule stays.

   Budget: two timers (3 s poll, 1 Hz clock) and no requestAnimationFrame. Every
   DOM write goes through setText/setVar, which no-op when the value is unchanged,
   because this panel runs 24/7 on a desk display. */

var POLL_MS = 3000;
var POLL_TIMEOUT_MS = 2500;    /* must stay under POLL_MS so polls cannot pile up */
var ACTION_TIMEOUT_MS = 4000;
var STALE_MS = 30000;          /* contract: generatedAt older than this = stale */
var DAY_MS = 86400000;         /* forecast >1 day out degrades from a clock time to a short date */
/* The BREAKING-shape ceiling, not a feature level. Raising this is a coordinated
   deploy by definition — it means an existing field changed meaning, so the
   fields below would silently be read wrong. Additive work never touches it. */
var SCHEMA_MAX = 5;
var GRID_ROWS = 2;             /* .cards is a fixed 2-row grid at every slot */
var GRID_COLS_DEFAULT = 4;     /* 4 columns at the 2560x720 slot; narrower slots drop to 3 then 2 */
var SPARK_BUCKETS = 24;
var SPARK_BUCKETS_7D = 7;      /* contract: burn.daily is 7 entries, oldest first */
var SUB_ROWS_MAX = 5;          /* contract caps subagentDetail at 5 */
var SUB_ROWS_MAX_Q = 1;        /* a card already carrying a 4-line question has room for one, plus the "+N more" */
var SHEET_SUB_MAX = 5;         /* the sheet shows the list whole; the cap only guards a feed that ignores its own cap */
var SHEET_EVENTS_MAX = 8;      /* contract caps events at 8, newest first */
/* The action sheet shows fewer, because the QUESTION is what that sheet is for.
   Measured at 2560x720 against the longest fixture question: eight rows below
   the buttons cut it from seven rendered lines to four, and four rows still cut
   the last line — v0.2.0 rendered it whole and must keep doing so. Three rows
   plus the "+N earlier" line leaves it whole with room to spare. The detail
   sheet has no question, so it shows the list whole. */
var SHEET_EVENTS_MAX_ACTION = 3;
var SHEET_CLOSE_MS = 900;      /* let the confirmation be read before the sheet goes */
var ESC_T1_MS = 300000;        /* 5 min unacked  -> deeper amber, stronger pulse */
var ESC_T2_MS = 900000;        /* 15 min unacked -> red-amber, arm held a cell higher */
/* The temperature thresholds, in CELSIUS. A reading in any other unit is shown
   plainly and left uncoloured rather than being called red at 80°F. */
var SENSOR_AMBER_C = 80;
var SENSOR_RED_C = 90;

/* v0.4.0 */
var BLINK_MS = 150;            /* one eye-frame; long enough to read, short enough not to be a nap */
/* v0.28.1: 8-10 s, the operator's ask ("blink more often"). It was 60-180 s on the
   theory that an idle tic on a 24/7 panel is noise; in practice a crab that blinks
   every couple of minutes reads as a still image, and a blink is the cheapest
   "alive" signal there is - one 150 ms eye-frame, calm moods only, never under
   quiet or reduced motion (scheduleBlink / blinkOnce keep those gates). */
var BLINK_MIN_MS = 8000;
var BLINK_MAX_MS = 10000;
var CELEBRATE_MS = 10000;
var CELEBRATE_MIN_TURN_MS = 1800000;  /* a turn worth celebrating is >30 min of work */
/* A textfield fires per keystroke and a slider per drag step; never POST per
   character or per pixel. Shared by every /v1/config key since v0.7.0. */
var CFG_DEBOUNCE_MS = 2000;

/* v0.5.0 */
var TIMELINE_MAX = 20;         /* display cap on the merged day timeline */
/* The cap when the week strip is in the footer, and it is a MEASUREMENT, not a
   preference. At 2560x720 the timeline region is 421 px with the strip below it
   and a row costs 24.75 px, so twenty rows overrun it by 74 px — three rows that
   scroll out of sight with no "+N earlier" line to admit they exist, which is
   the one thing this list must never do. Fifteen rows plus the tail is 16 lines,
   396 px, and leaves 25 px: a margin, not a fit-to-the-pixel (the v0.6.0 lesson
   about another browser's font metrics). Re-measure against cap + 1 lines if
   anything else is ever added to this footer. */
var TIMELINE_MAX_WEEK = 15;
var TIMELINE_TITLE_MAX = 26;   /* the session tag is a column, not the title */
var BYMODEL_MAX = 4;           /* contract caps burn.byModel at 4, desc */
/* The gauge ramp is fixed by design and does NOT follow the personalization
   accent: blue below 75%, amber from 75, red from 95. See the
   note on the gauge-blue token in sidecrab.css. */
var GAUGE_AMBER_PCT = 75;
var GAUGE_RED_PCT = 95;

/* v0.7.0 */
/* The gauge foot counts DOWN to the reset instead of naming the clock time, the
   way the Claude app's own usage panel does: "resets in 33 min" answers the
   question people actually ask of that line. Minute granularity, relabelled on
   the 1 Hz tick — a seconds counter on a limit that resets in three hours is
   precision the number does not have, and it would repaint twice a second for
   the life of the panel.
   Above 90 minutes the minute figure stops being the useful part, so the label
   switches to hours; above a day, to days. The absolute clock time does not
   disappear — it moves to the gauge's tooltip. */
var RESET_MIN_ONLY_MAX = 90;   /* minutes: above this, "in 2h 10m" */
var RESET_HOURS_ONLY_MAX = 24; /* hours: above this, "in 4d 13h" */
/* The toast properties write the SECOND key on /v1/config. Range and default are
   the property's, not the contract's: the contract allows 30..3600 s, the slider
   offers 30..600 because a toast that waits longer than ten minutes is a toast
   nobody connects to what it is about. The value is clamped to the slider range
   before it is sent, so a property that arrives out of range is corrected rather
   than 400ed. */
var TOAST_SEC_MIN = 30;
var TOAST_SEC_MAX = 600;
var TOAST_SEC_DEFAULT = 120;
/* v0.16.0 — the APPROVAL toast's own threshold, the optional third member of the
   same `toast` block: how long a permission request may sit undecided before the
   notifier toasts it. Its own bounds pair rather than a reuse of the three above,
   because the contract gives it 5..3600 and its shipped default is 20 s — BELOW
   the waiting-toast floor of 30. The two settings are not the same question: a
   pending permission is something the operator is already blocked on, a
   merely-thinking turn is not.
   These are the CONTRACT bounds, and the clamp uses them rather than the
   property's own slider range (which stops at 300 — see index.html). Clamping
   wider than the control can travel is deliberate: the clamp exists so a value
   arriving from anywhere else is corrected instead of 400ed, and a 400 here would
   be indistinguishable from the "older crabd, no approvalThresholdSec" 400. */
var APPROVAL_SEC_MIN = 5;
var APPROVAL_SEC_MAX = 3600;
var APPROVAL_SEC_DEFAULT = 20;
var WEEK_DAYS = 7;             /* contract: recap.week is the last 7 local days, oldest first */

/* v0.8.0 — pinning + the day drill */
/* The pin map is capped so a panel running for months cannot grow its stored
   properties without bound. 50 is far past any plausible number of sessions a
   person pins; the eviction is oldest-pin-first, and it exists to bound the
   value, not to police the user. */
var PIN_MAX = 50;
/* The property NAME inside this widget's local-storage JSON object. The vendor
   mechanism keys the whole object on uniqueId and expects every persisted
   property of the widget to live inside it, so this is a key in that object —
   never a localStorage key of its own. */
var PIN_PROP = 'pinnedSessions';
/* v0.15.0 — the two header chips persist through the SAME vendor object, as two
   more properties beside PIN_PROP. Not localStorage keys of their own, for the
   reason spelled out over pinStorage(): one JSON object per widget, keyed on
   uniqueId, is the whole mechanism the vendor documents. */
var FILTER_PROP = 'sessionFilter';
var DENSITY_PROP = 'density';
/* A user-initiated GET, not the poller: it may take a little longer than a poll
   without anything piling up, because a second tap is refused while one is in
   flight. Still bounded — an unsettled fetch would leave the tap dead. */
var HISTORY_TIMEOUT_MS = 4000;
/* How long the History chip keeps saying why the last tap failed (v0.19.0). Long
   enough that somebody who tapped and looked away still finds the reason, short
   enough that a redeployed crabd is not accused of being old all evening. It is
   NOT a retry gate — every tap fetches — so nothing is stranded if it is wrong. */
var HISTORY_FAIL_MS = 30000;
/* Display cap on the day view's row list, with a "+N earlier" tail exactly like
   the timeline's — and, like the timeline's, a MEASURED number rather than a
   round one. At 2560x720 (2026-08-26) a row costs 24.75 px and the sheet panel
   stops growing at 662 px (its max-height 92%), so the ladder runs: 18 rows +
   the tail = 470 px of list in a 636 px panel; 19 = 661 px, which fits to the
   pixel; 20 scrolls, and the "+N earlier" line then leaves the screen — the one
   thing this list must never do, because it is the line that admits rows exist.
   18 is the last cap with a whole row of margin, and a margin measured on one
   browser's font metrics is what the next browser's metrics eat. Re-measure
   against cap + 1 if anything is added to this view's footer. */
/* v0.15.0: this is now the CEILING, not the fit. It was measured at 2560x720
   and it was wrong everywhere else — at 840x344 the same 18 rows plus the tail
   are 234 px of list in a 216 px box, so the tail sat 18 px into overflow and
   the one line that admits rows exist was the line off the screen (measured
   2026-08-26). The slot is not the reason on its own: --touch-min has a hard
   48 px floor, so at the small slot the sheet head's controls take 48 px where
   proportionally they would take 29, and the list pays the difference. That
   makes the cap a function of the slot's px height AND of the floor, which is
   not something a constant can be. fitDayRows() measures the real box after
   the rows are in it and trims until nothing overflows; this number only stops
   the first pass being longer than any slot could want. */
var DAY_ROWS_MAX = 18;
var DAY_ROWS_MIN = 3;          /* a list trimmed below this is a fit nobody asked for */
var DAY_RE = /^\d{4}-\d{2}-\d{2}$/;

/* v0.10.0 — the burn budget */
/* The two steps where the day's spend stops being ambient. Both are carried in
   WORDS as well as in colour (see renderBudgetLine): the panel is read from
   across a room, and half its defects have been reported from a photograph. */
var BUDGET_AMBER_PCT = 100;
var BUDGET_RED_PCT = 150;
/* The budgetTokens slider is in THOUSANDS of output tokens (see the property
   declaration in index.html for why). These clamp what the property may be worth
   before it is multiplied back up and sent: a value that somehow arrives outside
   the slider's range is one to correct, not a body to have crabd 400 — and a 400
   here would be indistinguishable from the "older crabd, no budget key" 400 this
   version has to read. 100k is the contract's floor; 20M is well inside its
   100M ceiling. */
var BUDGET_K_MIN = 100;
var BUDGET_K_MAX = 20000;
var BUDGET_K_DEFAULT = 5000;
var HOURS_PER_DAY = 24;

/* v0.11.0 — hung vs thinking, and the wardrobe */
/* A working session that has not touched anything for this long gets the "quiet
   Nm" hint. 90 s is three poll intervals past a minute of silence: long enough
   that a session mid-thought does not trip it, short enough to notice on the
   walk past. The hint is TEXT, and the fresher-than-that state is the two-frame
   dot — neither one is a colour, because this panel is read from a photograph. */
var HUNG_MS = 90000;
/* The accessory hysteresis. A condition has to hold for this long before the
   crab changes clothes, so a session flickering working -> done -> working
   cannot strobe a hat on and off. Evaluated on the poll, so the real grain is
   3 s; that is the point — this is anti-flap, not a stopwatch. */
var ACC_STABLE_MS = 10000;
var PARTY_MS = 60000;          /* the hat is worn for a minute after a finish lands */
var JUGGLE_MIN_WORKING = 5;
var JUGGLE_MS = 6000;          /* must equal the .ball animation's 750ms x 8 */
var JUGGLE_COOLDOWN_MS = 600000;
var SNAP_MS = 560;             /* clawsnap 260ms x 2, plus the frame it lands on */
var BOUNCE_MS = 800;           /* crabhop 380ms x 2, likewise */
/* The finish dance (v0.28.0): a session lands working -> done and the crab does a
   four-beat shimmy in its sunglasses. Bounded three ways so a busy fleet is not a
   crab that never stops dancing: the turn must have been real work (not a one-line
   answer), one dance per cooldown, and never while anything is waiting on a human. */
var DANCE_MS = 1560;           /* crabdance 390ms x 4 */
var DANCE_MIN_TURN_MS = 20000; /* a 20 s turn is a job; a 3 s turn is a reply */
var DANCE_COOLDOWN_MS = 30000;
/* The accessory priority, highest first. One list, read in order — the ladder is
   data rather than a chain of ifs so the precedence is a thing you can read.
   THREE, not four (v0.18.0): the hard hat is retired. It fired at 3+ working,
   which sits INSIDE the state the sunglasses exist for, so the sunglasses were
   unreachable on any busy estate — the costume for "everything is going well"
   could only be seen when not much was going on.
   NIGHTCAP OUTRANKS SUNGLASSES (v0.19.0), and it is the same defect the hard hat
   had, one rung down. Quiet hours is the operator saying "night mode", and a busy
   night — every session working, limits calm — is the ordinary shape of one: that
   is precisely when a long run is left going overnight. Sunglasses first meant the
   nightcap could only ever appear on a night when the fleet was ALSO idle or
   mixed, so the costume for "it is night" was unreachable on exactly the nights
   there was something to watch. Quiet is a fact about the CLOCK and the sunglasses
   are a fact about the WORK, so the clock wins inside quiet hours and the
   sunglasses keep every hour outside it. */
var ACCESSORIES = ['party', 'nightcap', 'sunglasses'];

/* Every value data-mood is ever set to. Read ONLY by the dev-only &mood= flag, so
   a typo in a screenshot URL cannot put the crab into a mood the stylesheet has
   no rules for and paint a crab with no eyes. */
var MOODS = ['content', 'waving', 'asleep', 'worried', 'celebrating', 'sweating'];

/* v0.22.0 — the QUIET OVERRIDE.
   Quiet hours is a SCHEDULE, written to /v1/config and edited in the settings
   sheet's companion section (MF-001). This is the override on top of it — be quiet an hour early, or stay awake
   through tonight's window — and it is a different kind of statement, so it goes on
   a different wire: POST /v1/action, the endpoint for things the operator does to
   the panel now, beside ack, decide and queue-continue.

   THE VOCABULARY IS FIXED AND IT IS THREE WORDS. One tap target on an ambient panel
   cannot carry a duration picker, and a control that could set any duration would
   need a second surface to set it in. So: quiet for an hour, awake for an hour, or
   hand it back to the schedule. Anything more specific is what the property sheet
   is for. */
var QUIET_OVERRIDE_MIN = 60;   /* the "1h" in the tap cycle, well inside 15..480 */
/* The contract's own bounds. The clamp exists so a value arriving from anywhere
   else is corrected rather than 400ed — and a 400 here would be indistinguishable
   from the "older crabd, no quiet action" 400 that latches the chip away. */
var QUIET_MIN_MINUTES = 15;
var QUIET_MAX_MINUTES = 480;

/* v0.22.0 — the HOST HISTORY RING.
   Ten minutes of the `host` block, sampled once per poll and held in the page only.
   There is no endpoint for this and none is invented: crabd serves the CURRENT
   reading, and a history of it is something a panel that has been watching can
   assemble and a panel that has just booted honestly cannot. */
var HOST_WINDOW_MS = 600000;   /* the width of the plot: 10 minutes */
/* SCA-012 — A MEMORY BACKSTOP, NOT THE HORIZON, and that distinction IS the defect
   this replaces. The cap used to be enforced by dropping the OLDEST sample, so at
   the companion's 2 s publish cadence a ring capped at 260 held 518 s of a chart
   labelled ten minutes: 82 s inside the advertised horizon were discarded and the
   chart said nothing about it. Time is the horizon now (hostRingTrim), and past
   this cap the ring is THINNED rather than truncated, so the span survives and only
   the resolution inside it falls.
   THE NUMBER: ten minutes at the 2 s cadence is 301 samples, so 400 is the cap with
   a third of a window of slack. It cannot be reached at any documented cadence; it
   is a bound on memory for one that is faster than documented. */
var HOST_RING_MAX = 400;
/* Below this the sheet says "collecting" instead of drawing. Ten samples is 30 s of
   feed — enough for a line to have a shape, few enough that the wait is not a
   feature. Under it a two-point "sparkline" is not a trend, it is a slope, and
   drawing one would be the panel inventing a history it does not have. */
var HOST_MIN_SAMPLES = 10;
/* Three poll intervals. Past this the line BREAKS rather than being drawn across a
   stretch in which nothing was measured — a straight segment over a gap is an
   interpolation, and an interpolated CPU history is a reading nobody took. */
var HOST_GAP_MS = 9000;
var SVG_NS = 'http://www.w3.org/2000/svg';

/* v0.22.0 — the CONTEXT HAIRLINE's denominator when crabd does not serve one.
   Since crabd 0.28.0 `contextWindowTokens` is the first source and this is the
   FALLBACK for an older companion (see ctxWindowTokens); crabd applies the same
   marker itself, ranked above its model catalog, so the two agree by construction.
   A model id may carry its context window in the string, and crabd serves that
   string VERBATIM (companion/crabd.py: "No normalising, aliasing or prettifying:
   the widget shows what the transcript said", proved by
   test_model_string_is_served_as_is on the literal `claude-opus-5[1m]`). So a
   marked id is a window size the FEED stated, not one this widget guessed.
   There is deliberately no model-name table here: a built-in "opus means 200k"
   would be a number no document said, dividing into a figure whose scale it does
   not know, and it would go silently wrong the first time a window changed. An
   unmarked model on an un-upgraded crabd therefore still gets NO BAR — see
   ctxFillPct. Both k and m are read, so a future `[500k]` needs no code change. */
var MODEL_CTX_RE = /\[(\d+(?:\.\d+)?)\s*([kKmM])\]/;

/* v0.6.0 */
/* key = the contract's fleet field, el = the element id, label = the word for the
   tooltip and the screen reader. The g / t LETTERS are static markup in
   index.html, not here — nothing in this file writes them. */
var FLEET_PARTS = [
	{ key: 'toast', label: 'toast', el: 'fleetToast' }
];
var FLEET_STATES = { running: 1, stopped: 1, absent: 1, unknown: 1 };
/* The states whose card offers Dismiss. done since v0.4.0, idle since v0.6.0:
   both are rows nobody is waiting on, and neither can be brought back by
   dismissing it — the key is sessionId + stateSince, so any transition at all
   resurrects the card. A working or needs_input row is never dismissable. */
var DISMISSABLE = { done: 1, idle: 1 };

/* Tap-to-continue (v0.12.0). The three defaults are hardcoded: a short LABEL for
   the button face and the FULL instruction that goes on the wire as the
   queue-continue prompt. Any strings the feed carries in a top-level
   continuePrompts array are appended after these as extra buttons (presence-
   gated). Generic strings only — SideCrab reads nothing but local state. */
var CONTINUE_DEFAULTS = [
	{ label: 'Continue', prompt: 'Keep going with what you were doing.' },
	{ label: 'Run the tests', prompt: 'Run the tests and report the results.' },
	{ label: 'Commit + push', prompt: 'Commit the changes and push.' }
];
/* A permission decision and a queued continue are the two write actions added in
   v0.12.0. Neither latches: a 404/400 is handled inline (continue) or logged
   (decide) and the very next tap tries again — crabd redeploys under a live
   widget, so an unsupported answer today may be supported after the next deploy. */
var DECIDE_ALLOW = 'allow';
var DECIDE_DENY = 'deny';

/* The approval pairing code (v0.27.0, closes SEC-a). Read LIVE off the host boot
   object on every decide, never cached and never rendered: the host injects it, the
   panel sends it, and nothing puts it on a display read from across a room. Sent in
   the body rather than a header so the request stays the one application/json
   preflight the companion already answers. The companion normalises case and
   hyphens, so this only trims. */
function pairingCode() { return hostPairingCode(); }

/* crabd >= 0.29.0 says so in the document (`approvals.tokenRequired`); an older
   crabd has no `approvals` block and never asks for one. */
function tokenRequired() {
	var a = lastGoodDoc && lastGoodDoc.approvals;
	return !!(a && typeof a === 'object' && a.tokenRequired === true);
}

/* MF-017 — APPROVAL READINESS, as the companion states it. Four values and each
   sends the operator somewhere different, which is the whole reason it is not a
   boolean: `off` is a feature nobody turned on, `no-token` is a companion with no
   code to check against, `unverified` is a panel that has not proved it holds the
   code, and `ready` is the one state in which a tap on Approve will be accepted.
   PRESENCE-GATED: a companion that does not serve `readiness` gets null here, and
   every reader below renders nothing rather than guessing at a state. */
function approvalReadiness() {
	var a = lastGoodDoc && lastGoodDoc.approvals;
	if (!a || typeof a !== 'object' || Array.isArray(a)) return null;
	var r = a.readiness;
	return (r === 'off' || r === 'no-token' || r === 'unverified' || r === 'ready') ? r : null;
}

function approvalVerifiedAt() {
	var a = lastGoodDoc && lastGoodDoc.approvals;
	var t = a && typeof a === 'object' ? Date.parse(a.verifiedAt) : NaN;
	return isFinite(t) ? t : null;
}

/* The repair text, one sentence per state, and every one of them names the thing
   the operator would actually do. */
function approvalReadyText(state) {
	if (state === 'ready') {
		var at = approvalVerifiedAt();
		return 'approvals ready' + (at !== null ? ' ' + EMDASH + ' paired at ' +
			fmtTimeOfDay(new Date(at), use24Clock()) : '');
	}
	if (state === 'off') return 'approvals are off in the companion configuration';
	if (state === 'no-token') return 'the companion has no pairing code ' + EMDASH +
		' run the SideCrab installer to mint one';
	if (state === 'unverified') return panelBridge()
		? 'this panel is not paired yet ' + EMDASH + ' restart the panel host to pair it'
		: 'this panel is not paired ' + EMDASH + ' approvals need the SideCrab panel host';
	return '';
}

/* Sent ONCE per page load, and only when the companion says it is waiting for it.
   Once per boot rather than per document because the answer is a fact about this
   page: a code that was wrong at boot is wrong for as long as the page is open, and
   retrying it every three seconds is how a rate limiter gets tripped by its own
   client. The code is never rendered, never logged and never put in a URL. */
var approvalVerifySent = false;

function maybeVerifyApproval() {
	if (approvalVerifySent || mockName) return;
	if (approvalReadiness() !== 'unverified') return;
	var code = hostPairingCode();
	if (!code) return;
	approvalVerifySent = true;
	postJson('/v1/approvals/verify', JSON.stringify({ code: code })).then(function (res) {
		if (res.status === 204 || res.status === 200) { logLine('approvals: paired'); return; }
		if (res.status === 403) { logLine('approvals: the panel host holds the wrong pairing code'); return; }
		if (res.status === 429) {
			/* Backing off is the whole response. The latch above already means this
			   page will not ask again, which is what a lock-out asks for. */
			logLine('approvals: pairing is rate limited, not retrying this session');
			return;
		}
		logLine('approvals: verify failed (HTTP ' + res.status + ')');
	}).catch(function () {
		/* NOT latched open: a dead socket at boot is a fact about this moment, and
		   the next document that still says unverified may find the companion up. */
		approvalVerifySent = false;
		logLine('approvals: verify failed, crabd not reachable');
	});
}

/* v0.15.0 — the queued chip, the approval countdown, and the two header chips */

/* How long crabd holds a PermissionRequest hook open before it gives up and
   returns the pass-through that lets the terminal dialog appear. MEASURED off
   the contract (docs/STATE-CONTRACT.md v0.12.0: "holds the response up to
   55 s"), not guessed, and it is what makes the countdown mean something: past
   it a tap on Approve reaches a request crabd is no longer holding, so the
   panel has to say the decision has moved back to the keyboard rather than
   leave a button that looks live. The widget NEVER decides on the operator's
   behalf at zero — it stops claiming the tap still matters, which is a
   different thing. */
var APPROVAL_HOLD_SEC = 55;
/* A queued prompt that matches no known button is trimmed to this on the card.
   The card is one line wide and the label is a reminder of what was tapped, not
   the instruction itself — the sheet is where the full text belongs. */
var QUEUED_LABEL_MAX = 28;

/* The session filter (v0.15.0). ONE chip cycling four modes, because a row of
   four toggles on a header that is already a tap target is four ways to open the
   timeline by accident. `match` is null for "all" so the filter is genuinely the
   identity there rather than a predicate that happens to return true.
   The states are the contract's, and a state this list does not name (a crabd
   that adds a fifth) falls only into "all" — never silently into a bucket whose
   label would then be a lie. */
var FILTERS = [
	{ key: 'all', label: 'All', match: null, empty: 'No active Claude sessions' },
	{ key: 'waiting', label: 'Waiting', match: { needs_input: 1 }, empty: 'No sessions waiting on you' },
	{ key: 'working', label: 'Working', match: { working: 1 }, empty: 'No sessions working' },
	{ key: 'quiet', label: 'Done/Idle', match: { done: 1, idle: 1 }, empty: 'No finished or idle sessions' }
];
/* Comfortable is the layout every version before this one had, so it is index 0
   and an unreadable stored value degrades to it. */
var DENSITIES = [
	{ key: 'comfortable', label: 'Comfortable' },
	{ key: 'compact', label: 'Compact' }
];

/* ---------------------------------------------- touch gestures (v0.14.0) */

/* Every figure here is a DISCRIMINATION threshold, and they are ordered so no two
   gestures can claim the same pointer. TAP_SLOP is the smallest: under it nothing
   has moved and the interaction is a tap or a hold. SWIPE_ARM and PULL_ARM are
   above it, so a gesture only commits to an axis once the finger has clearly
   chosen one. Nothing re-decides after that: a swipe that drifts upward stays a
   swipe, because a gesture that changed its mind halfway would abandon a card
   mid-flight for reasons the person cannot see. */
var TAP_SLOP_PX = 10;          /* travel under this is a tap; over it, never a tap */
var SWIPE_ARM_PX = 12;         /* horizontal travel that commits the pointer to a swipe */
var SWIPE_DISMISS_PX = 60;     /* past this on release, the card is dismissed */
var SWIPE_FLY_MS = 180;        /* the snap back, and the trip off the edge */
var SWIPE_FADE = 0.55;         /* how far the card fades by the threshold, as a fraction */
var LONGPRESS_MS = 600;
var PIN_FLASH_MS = 900;        /* how long the pin confirm is held on the card */
var MULTI_TAP_MS = 700;        /* both fingers down AND up inside this = a two-finger tap */
var MULTI_SLOP_PX = 14;        /* a two-finger gesture that travels further is a drag */
var PULL_ZONE_PX = 56;         /* a pull must START within this many px of the panel top */
var PULL_ARM_PX = 20;
var PULL_REFRESH_PX = 80;      /* release past this refreshes */
var NOTICE_MS = 1400;          /* the inline confirmation line */
var SUPPRESS_CLICK_MS = 400;   /* a gesture consumed the interaction; swallow the click
                                  the browser synthesises after it. A WINDOW rather than
                                  a flag because a two-finger tap synthesises more than
                                  one click and their order is not guaranteed. */

var MOCKS = ['normal', 'attention', 'empty', 'stale', 'question', 'quiet', 'recap', 'caveat', 'hot',
	/* rework = the post-rework production shape: schema 5 carrying EVERY current
	   field. future = schema 6, otherwise a perfectly valid document — the only
	   reason it must dead-feed is the number, which is the regression that keeps
	   a real break real. */
	/* dense = fourteen sessions, which is the only way to photograph the COMPACT
	   grid full: at 2560x720 compact holds twelve and every other fixture stops
	   at ten, so the capacity would be a claim rather than a picture. */
	/* extras = rework plus a SECOND limits.extra window, the shape the contract
	   has always allowed (extras.slice(0, 2)) and that no fixture carried until
	   v0.26.0. It is a separate file rather than a second window bolted onto
	   rework so every other capture in the probe matrix stays byte-identical —
	   rework is the fixture the whole matrix is baselined on. */
	'rework', 'dense', 'future', 'extras'];
var WEEKDAY_LETTERS = ['S', 'M', 'T', 'W', 'T', 'F', 'S'];
var EMDASH = '—';

var ui = {};
var lastGoodDoc = null;
var lastGoodAtMs = 0;          /* Date.parse(generatedAt) of the newest good doc */
var everHadData = false;
var pollFailed = false;
/* SCA-018. TRUE once the companion has ANSWERED with a document this panel cannot
   read - a schema outside the ceiling, a body that is not JSON, a generatedAt that
   does not parse. It is a different fact from "nothing has answered yet" and it
   wants a different sentence on the glass: one of them is a companion that is not
   running, the other is a companion that is. Sticky until a readable document
   arrives, because a dead feed does not get fresher by being asked again. */
var feedUnreadable = false;
var prevAlert = false;
var cardSig = '';
var extraSig = '';
var mockName = null;
var inFlight = false;
var flashing = false;
var waving = false;

/* sessionId -> the stateSince the local ack was taken against. The contract has
   crabd clear `acked` on the session's next transition; mirroring that with
   stateSince means a stale optimistic ack cannot outlive the question it
   answered — a NEW needs_input (new stateSince) re-alerts normally. */
var ackOptimistic = {};

/* sessionId -> the stateSince the card was dismissed at. Same key discipline as
   ackOptimistic and for the same reason: a dismissal is an answer to ONE done
   card, so any state change at all (back to working, done again later) is a new
   card and resurrects it. Purely local — crabd is never told. */
var dismissed = {};

/* sessionId -> the ms instant the pin was taken. Deliberately NOT keyed the way
   ackOptimistic and dismissed are: those two answer one card and must die on the
   next transition, but a pin says "keep this session where I can see it", which
   is a statement about the SESSION and survives every state change it makes. A
   pinned session that disappears from the feed simply stops being drawn; the map
   entry is kept silently, so the same session coming back comes back pinned.
   The instant is the eviction order, not a display value — nothing renders it. */
var pinned = {};
/* Set once, at boot, by loadPrefs(). null means the vendor storage mechanism is
   not available (a dev browser has no uniqueId; a locked-down profile can throw
   on localStorage itself), and the pin map then lives in memory for this session
   only — a lost pin is a nuisance, not an error, so nothing is said on glass.
   Since v0.15.0 the filter and the density ride the same key, and inherit the
   same silence: a header chip that forgets its mode across a restart is the same
   size of nuisance a forgotten pin is. */
var prefsStoreKey = null;
/* Indices into FILTERS / DENSITIES. Held as indices rather than keys so the chip
   cycles by arithmetic and an out-of-range stored value clamps to 0, which is
   the mode every version before this one had. */
var filterIdx = 0;
var densityIdx = 0;
/* The stored value this build did NOT recognise, held so savePrefs can put it
   back untouched (v0.16.0, audit F2). A NEWER widget writes a mode this build has
   never heard of; this build renders index 0 for it (the clamp above is right —
   there is nothing else it could draw) but must not then persist its own default
   over the newer build's setting on the next pin or chip tap. Null means "the
   stored value was one of ours, or there was none". Cleared the moment the
   operator cycles the chip here, because from then on this build's value IS the
   operator's latest word on it. */
var filterStoredUnknown = null;
var densityStoredUnknown = null;
/* v0.17.0 — the SEED, read off /v1/state's top-level `toast` block. crabd serves
   { thresholdSec, enabled } whenever it is serving config at all, and adds
   approvalThresholdSec ONLY when the operator has set it on disk. So:
     null  = older crabd, or a crabd with nothing set     -> no seed, behave as v0.16.0
     <int> = the operator's on-disk value                 -> the effective threshold
   Presence-detected, never schema-gated. It seeds the DISPLAY and the config
   sheet's control, and seeding is NOT a touch: a value nobody moved is never sent
   back, which is what stops a save materialising a key the operator hand-edited.
   See configSeed(). */
var approvalFeedSec = null;

/* id -> { state, turn } from the PREVIOUS good document. The celebration needs
   the turn length, and the contract clears turnStartedAt on Stop — so by the time
   a session reads `done` the duration only exists in the doc before it. */
var prevSessionState = {};
var celebrateUntil = 0;
var celebrateForced = false;   /* dev-only &celebrate=1 */
var blinkTimer = null;
var blinking = false;
var blinkMinMs = BLINK_MIN_MS;
var blinkMaxMs = BLINK_MAX_MS;
var crabBusy = false;
/* crabd.version from the newest good doc. NOT a gate — nothing keys behaviour off
   it. It exists so a crabd REDEPLOY (the version string changing under a live
   widget) can clear the /v1/config unsupported latch below and let capability be
   re-detected, instead of a widget that decided "no config endpoint" at 09:00
   staying deaf to one installed at 09:05. */
/* v0.11.0 wardrobe state. accCurrent is what the crab is WEARING; accCandidate is
   what the fleet has been asking for since accCandidateAt, and it only becomes
   accCurrent once it has held for ACC_STABLE_MS. Suppression (a waiting session,
   a dead feed, the plain style) bypasses the timer in both directions — an alert
   must never wait ten seconds to take the hat off. */
var accCurrent = '';
var accCandidate = '';
var accCandidateAt = 0;
var accForced = null;          /* dev-only &crab=<accessory> */
var partyUntil = 0;
/* recap.doneToday from the previous good document, so the party hat fires on the
   INCREMENT rather than on the value. null until a document carries the field at
   all: an older crabd has no recap, and a first sighting is not a finish. */
var prevDoneToday = null;
/* The working-session count from the previous good document — the bounce fires on
   the edge down to zero, and an edge needs the frame before it. */
var prevWorkingCount = null;
var juggling = false;
var juggleLastAt = 0;
var snapping = false;
var bouncing = false;
var dancing = false;
var danceLastAt = 0;
var danceUntil = 0;            /* while set, the wardrobe wears the shades regardless of the fleet */
var trickLoop = null;          /* dev-only: re-fires a forced trick so it can be shot */
var forcedTrick = null;        /* dev-only &crab=juggle|bounce|snap */

var crabdVersionSeen = null;
/* SCA-020. The companion's own start instant, as it states it. A CHANGE of this
   string is a restart, and a restart is the one event that may legitimately move
   generatedAt backwards (a corrected system clock, a resumed VM). It is compared
   and never ordered. */
var crabdStartedSeen = null;
/* The instant the first of a consecutive run of out-of-order drops happened, or 0.
   The escape hatch under the ordering rule: see acceptDoc. */
var orderingDroppedSince = 0;
var resizeTimer = null;        /* the grid's capacity is a media query, so a slot change must re-render */

/* ---- the quiet override (v0.22.0) ----
   THE CAPABILITY LATCH, and it is the approvalThresholdSec idiom reused rather than
   a new one. There is no way to presence-detect this feature from the document: the
   `quiet` block exists on every supported crabd, and the additive `override` member
   is ABSENT on a current crabd until an override is actually set — so "no override
   member" means "no override", not "no support", and the two are indistinguishable
   from a poll. A probe POST to find out would BE the write it was probing for.
   So the widget offers the control, attempts the write, and reads the reply — the
   same "attempt-and-handle IS the capability test" argument /v1/config makes at
   length. A 400 or a 404 says this crabd does not know the action, and the chip
   goes; a network failure says nothing about capability and does NOT latch.
   Cleared when crabd.version changes, because a redeploy is what would add support
   and a widget that remembered "unsupported" would need a console import to forget
   it — the v0.6.1 rework's rule, in a fourth place. */
var quietOverrideUnsupported = false;
/* The optimistic answer, bounded by the FEED and not by a timer: the tap paints the
   new state at once (the operator is standing there and the panel has to respond to
   a fingertip), and it is dropped the moment a document GENERATED AFTER the tap
   lands — at which point crabd's answer is the true one whether it agrees or not.
   That is the ack pattern with a better clock: `acked` is pruned when the session's
   stateSince moves, and this is pruned when the daemon has demonstrably spoken
   since the question was asked. */
var quietOptimistic = null;    /* { mode, until, at } */
var quietBusy = false;
var quietForced = null;        /* dev-only &quietov=, mock mode only */
var mockQuietOv = null;        /* mock-only: the override the harness is serving */
var mockQuietUntilPin = null;  /* mock-only: see pinMockQuietUntil */

/* ---- gesture state (v0.14.0) ----
   ONE pointer map for all four gestures, because they are not four independent
   features: they compete for the same finger, and arbitration is only possible
   where every live pointer is visible in one place. */
var pointers = {};             /* pointerId -> the live per-pointer record */
var swipe = null;              /* the engaged card swipe, or null */
var longPressTimer = null;
/* The instant the open sheet's pendingPermission was requested, or 0 when there
   is nothing to count (v0.15.0). Set by syncSheet, read by the 1 Hz tick. */
var sheetApprovalAt = 0;
var pinFlashId = null;         /* the session whose pin confirm is showing */
var pinFlashOn = false;        /* pinned (glyph in) vs unpinned (glyph out) */
var pinFlashTimer = null;
var pinFlashHold = false;      /* dev-only &pinflash=, mock mode only */
/* The &pinflash= target is kept SEPARATELY from pinAuto even though the flag sets
   both. applyPinOverride consumes pinAuto on the first document (that is what
   makes &pin= a one-shot), and maybeAutoGesture runs after it — so a second
   reader of the same variable finds it already null and the confirm never fires. */
var pinFlashAuto = null;
var noticeTimer = null;
var noticeHold = false;        /* dev-only &ackflash= / &refreshflash=, mock mode only */
var pull = null;               /* the engaged pull-to-refresh, or null */
var multi = null;              /* the two-finger tap candidate, or null */
var suppressClickUntil = 0;
var swipeFreeze = null;        /* dev-only &swipe=<target>, mock mode only */
var swipeFreezePx = 0;
var ackFlashAuto = false;      /* dev-only &ackflash=1 */
var filterForced = null;       /* dev-only &filter=, mock mode only */
var densityForced = null;      /* dev-only &density=, mock mode only */
var holdOverrideSec = null;    /* dev-only &hold=, mock mode only */
var holdAnchorAt = null;       /* the pinned instant, set on first use */
var refreshFlashAuto = false;  /* dev-only &refreshflash=1 */


var sheetSessionId = null;
var sheetMode = null;          /* 'session' | 'burn' | 'timeline' | 'day' | 'forecast' | 'overflow' */
/* The sessions the capacity slice cut, written by renderSessions on every render
   and read by the overflow sheet (v0.20.0, CD-14). Held rather than recomputed so
   the sheet and the grid cannot disagree about which rows were removed. */
var overflowList = [];
var overflowSig = null;
/* The host-history sheet's signature (v0.22.0). It moves on every poll, because a
   poll is a new sample — which is exactly right: this view is the one on the panel
   that is SUPPOSED to redraw three times a second's worth of ring. */
var hostSig = null;
/* Which usage window the forecast sheet is showing: 'fiveHour', 'weekly' or
   'extra<N>'. Held as a KEY rather than as the window object, so the 3 s poll
   re-reads the live limits block and the sheet follows a utilization that moves
   while it is open — a held object would freeze at the reading the tap caught. */
var forecastWin = null;
var forecastSig = null;
/* The day drill's fetched document, held so the 3 s poll can re-sync the sheet
   without re-fetching history on every tick. Cleared when the view is left. */
var dayDoc = null;
var daySig = null;
var dayBusy = false;
/* Monotonic, so a reply that lands after the sheet has moved on is dropped
   rather than swapping the panel under a finger that has gone elsewhere. */
var dayReqId = 0;
/* WHICH SHEET IS ON THE GLASS (v0.20.0, CD-35). dayReqId alone only catches a
   SECOND day fetch superseding the first; it says nothing about the sheet having
   been closed and reopened on something else, which is the case that actually
   bit: tap a week column, close the timeline, open a session — and the history
   reply lands into that session's sheet, repainting it as a day view with the
   session's own title and accent still on it. Bumped by closeSheet and by every
   open*, captured by openDaySheet, and compared when the fetch returns. */
var sheetGen = 0;
var dayAuto = null;            /* dev-only &day=YYYY-MM-DD, mock mode only */
/* The history chip's unavailable mark (v0.19.0), and when it clears. NOT a latch:
   it is a message about the LAST tap, and the next tap fetches again regardless.
   The timer only stops a stale reason sitting on the header all evening after
   crabd has been redeployed underneath it. */
var histFailUntil = 0;
var histAuto = null;           /* dev-only &hist=rich|empty|error, mock mode only */
var devUidOverride = null;     /* dev-only &uid=, mock mode only — see loadPrefs() */
var pinAuto = null;            /* dev-only &pin=, mock mode only */
var burnSig = null;
var timelineSig = null;
var sheetBusy = false;
var sheetCloseTimer = null;
var sheetAutoId = null;        /* dev-only &sheet= target, mock mode only */
var sheetAutoDetailId = null;  /* dev-only &sheet2= target, mock mode only */
var burnAuto = false;          /* dev-only &burn=1, mock mode only */
var timelineAuto = false;      /* dev-only &timeline=1, mock mode only */
var hostAuto = false;          /* dev-only &host=1, mock mode only */
var approvalAuto = false;      /* dev-only &approval=1, mock mode only — auto-open the approval sheet */
var actionForce400 = false;    /* dev-only &action400=1, mock mode only — force the older-crabd 400 on queue-continue/decide */
var continueBtnSig = null;     /* the built continue-button set, so the row rebuilds only when it changes */
var continueStatusFor = null;  /* the session the continue status line currently belongs to */
var sheetOpenState = null;     /* the session state the sheet was opened against */
/* null, not '': an EMPTY subagent/event list signs as the empty string, so a
   '' sentinel compares equal to it and the rebuild is skipped — which showed
   the previous session's rows in a re-opened sheet. Both sigs also carry the
   session id, so two sessions that happen to sign alike still redraw. */
var sheetSubSig = null;
var sheetEventSig = null;
var actionContentType = 'application/json';

/* 24 h (burn.hourly) or 7 d (burn.daily). The toggle is disabled outright when
   the feed carries no daily series — an older crabd, where switching would only
   ever show an empty chart. Presence of burn.daily is the test, not a number. */
var sparkMode = '24h';
var sparkDailyAvailable = false;
var sparkBucketCount = 0;      /* how many bar elements currently exist */

/* Dev-only, mock mode only: force a needs_input age so the escalation tiers can
   be photographed without waiting 15 real minutes. */
var ageOverrideMin = null;
var ageOverrideAt = null;      /* the pinned instant, set on first use */
/* Dev-only, mock mode only: &budget=<percent>, so the amber and red steps can be
   photographed without a second fixture per step. */
var budgetPctOverride = null;

/* The host block from /v1/state. Held as its own state rather than read out of
   lastGoodDoc at paint time because several writers paint this row and one place
   has to own what it currently contains.
   null means "no figure", and it is the only value that ever hides a segment: a
   contract-legal null must never become 0%, which on this row would read as an idle
   machine rather than as a companion that could not measure one. */
var hostMetrics = { cpuPct: null, memPct: null, memUsedGB: null, memTotalGB: null };
/* The ten-minute host ring (v0.22.0). One entry per DOCUMENT - not per render,
   which runs on taps and on the 1 Hz tick too and would sample the same document
   many times over. Entries carry a null cpu/mem when the document landed and the
   figure did not, because "the companion answered and could not measure" is a fact
   worth having a slot for; documents that never landed leave a TIME gap instead,
   which hostRuns reads. */
var hostRing = [];
/* Dev-only, mock mode only: &mood=<content|waving|asleep|worried|celebrating>
   holds one crab mood (v0.17.0). &celebrate=1 already did this for exactly one
   mood and for exactly this reason; the other four were reachable only by picking
   a fixture that paints them, which moves every other thing on the panel too — so
   a same-pose A/B of the crab ART was not possible off-glass. Held AFTER the mood
   is derived, so nothing about the derivation changes. */
var moodForced = null;

/* ------------------------------------------------------- the host adapter (v0.32.0)

   CLEAN-01. ONE object, set by the panel host before any script runs, is
   everything this page is told about the surface it is on:

       window.__sidecrabHost = { kind: 'standalone', props: { ... }, pairingCode: '...' }

   The companion serves this page at /panel/ in both surfaces. What differs is
   what sits behind it: the panel host (WebView2) answers `settings` and
   `focus-session` over its message bridge, and a plain browser answers nothing.
   So CAPABILITIES ARE NOT READ OFF THIS OBJECT AND NOT GUESSED FROM THE ADDRESS.
   They come from the bridge's own host-info reply (hostCan), which means a page
   that cannot save says so instead of offering a Save that goes nowhere.

   WHAT WAS RETIRED WITH THE VENDOR HOST, and why none of it comes back:
   - the vendor event bus and the property-update callbacks. Settings now move on a save
     result, which is a reply to a request this page made.
   - The window-global probe and the Function('return NAME') probe. The vendor
     injected each property as a lexical global, so a reader had to look for one;
     here a page global that happens to share a setting's name is NOT a setting,
     and the Function probe made every settings read a dynamic code evaluation.
   - window.plugins and the sensor wrappers (CLEAN-03). The hardware row is fed by
     the companion's `host` block alone.
   Settings live on this one object and are read only through hostProp. */

function hostBoot() {
	if (typeof window === 'undefined') return null;
	var h;
	try { h = window.__sidecrabHost; } catch (e) { return null; }
	if (!h || typeof h !== 'object' || Array.isArray(h)) return null;
	if (h.kind !== 'standalone') return null;
	return h;
}

/* The injected settings, as an object this page owns rather than one it shares.
   A missing or malformed `props` is an EMPTY settings set, never a reason to go
   looking somewhere else for a value. */
function hostProps() {
	var h = hostBoot();
	var p = h && h.props;
	return p && typeof p === 'object' && !Array.isArray(p) ? p : {};
}

function hostProp(name) {
	var props = hostProps();
	if (Object.prototype.hasOwnProperty.call(props, name)) {
		var v = props[name];
		if (v !== undefined && v !== null && v !== '') return v;
	}
	/* The one setting with no sensible default: uniqueId keys the stored prefs
	   (pins, filter, density, view). A fixed key means the panel remembers them
	   across restarts, and a plain browser preview gets the same key, which is
	   right - it is one operator's panel either way. */
	if (name === 'uniqueId') return 'standalone';
	return undefined;
}

function boolProp(name, dflt) {
	var v = hostProp(name);
	if (v === undefined || v === null || v === '') return dflt;
	if (typeof v === 'string') return v !== 'false' && v !== '0';
	return !!v;
}

function strProp(name, dflt) {
	var v = hostProp(name);
	if (v === undefined || v === null || v === '') return dflt;
	return String(v);
}

function numProp(name, dflt) {
	var n = Number(hostProp(name));
	return isFinite(n) ? n : dflt;
}

/* The approval pairing code. Injected by the host beside the settings and never
   put on the glass: MF-017 sends it once per boot to verify, and a code the panel
   printed would be a secret on a display read from across a room. */
function hostPairingCode() {
	var h = hostBoot();
	var c = h && h.pairingCode;
	return typeof c === 'string' ? c.trim() : '';
}

function hexToRgbTriple(hex, dflt) {
	var m = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
	if (!m) return dflt;
	var n = parseInt(m[1], 16);
	return ((n >> 16) & 255) + ', ' + ((n >> 8) & 255) + ', ' + (n & 255);
}

/* Called at boot and after every accepted save. NOT on a vendor data-updated
   callback: there is no longer anything that can move a setting under this page
   except a save this page asked for. */
function applyProperties() {
	var root = document.documentElement;
	setVar(root, '--text-color', strProp('textColor', '#EDE7DF'));
	/* Second statement of the accent default (the other is :root in sidecrab.css).
	   This one WINS at runtime, so it is the one that must not drift. AUD-F1 moved
	   it #CC785C -> #BE7E6E; v0.30.1 moved it to #6F94CC. */
	setVar(root, '--accent', strProp('accentColor', '#6F94CC'));
	setVar(root, '--bg-rgb', hexToRgbTriple(strProp('backgroundColor', '#0F0E0D'), '15, 14, 13'));

	var t = Math.max(0, Math.min(100, numProp('transparency', 0)));
	setVar(root, '--bg-alpha', String(1 - t / 100));

	/* The touch-diagnostics switch can move under a running panel on a save, and
	   this is the only place that hears about it. syncDiag returns immediately
	   unless the wanted state and the installed state actually disagree, so a
	   colour change cannot tear down the capture layer. */
	syncDiag();
	/* MF-001: nothing is pushed to /v1/config from here any more. The config sheet
	   is the one writer, and it writes only what the operator moved. */
	render();
}

/* ------------------------------------------------------------------- helpers */

function setText(el, value) {
	if (!el) return;
	var s = value === null || value === undefined ? '' : String(value);
	if (el.textContent !== s) el.textContent = s;
}

function setVar(el, name, value) {
	if (!el) return;
	if (el.style.getPropertyValue(name) !== value) el.style.setProperty(name, value);
}

function pad2(n) { return (n < 10 ? '0' : '') + n; }

function fmtClock(date, use24) {
	if (!date || isNaN(date.getTime())) return EMDASH;
	var h = date.getHours();
	if (use24) return pad2(h) + ':' + pad2(date.getMinutes());
	return ((h % 12) || 12) + ':' + pad2(date.getMinutes());
}

function fmtDate(date, use24) {
	var s;
	try {
		s = date.toLocaleDateString(undefined, { weekday: 'short', day: '2-digit', month: 'short' });
	} catch (e) {
		s = date.toDateString();
	}
	if (!use24) s += '  ' + (date.getHours() < 12 ? 'AM' : 'PM');
	return s;
}

function fmtDur(seconds) {
	if (!isFinite(seconds)) return EMDASH;
	var s = Math.max(0, Math.floor(seconds));
	if (s < 60) return s + 's';
	if (s < 3600) return Math.floor(s / 60) + 'm';
	if (s < 86400) return Math.floor(s / 3600) + 'h';
	return Math.floor(s / 86400) + 'd';
}

/* THE M BOUNDARY IS 999500, NOT 1e6 (v0.26.0). The k branch ROUNDS — and
   Math.round(999999 / 1e3) is 1000, so 999,999 painted as "1000k": a four-digit
   k that the M branch exists to say. Anything from 999500 up rounds to 1000k, so
   that is where M has to start, and the reading it gives ("1.0M") is the k
   branch's own rounding rule carried one unit further rather than a second rule.
   Not floored to "999k": that would be a DIFFERENT rounding rule for one bucket,
   and the row above it (998,700 -> "999k") would then paint the same string for a
   larger number. Five characters at most either way, which is the diag chip's
   width budget — see renderDiagChip. */
function fmtNum(n) {
	if (typeof n !== 'number' || !isFinite(n)) return EMDASH;
	var a = Math.abs(n);
	if (a >= 999500) return (n / 1e6).toFixed(1) + 'M';
	if (a >= 1e4) return Math.round(n / 1e3) + 'k';
	if (a >= 1e3) return (n / 1e3).toFixed(1) + 'k';
	return String(n);
}

function shortModel(m) {
	if (!m) return null;
	return String(m).replace(/^claude-/, '').replace(/-\d{6,8}$/, '');
}

/* The 12/24-hour default lives in ONE place (v0.20.0, CD-32), and since v0.32.0
   that place is this file: the retired vendor metadata used to declare it too, and
   the two statements disagreed, so a panel with a property sheet booted on the
   12-hour clock and a browser preview booted on 24-hour. Five call sites each
   carried their own copy of the default,
   which is five chances for the pair to drift apart again. */
function use24Clock() { return boolProp('clock24', false); }

/* Wall-clock for the timeline rows. Unlike fmtClock, the 12-hour form carries
   AM/PM: the day timeline spans a whole day, so a bare "3:41" is genuinely
   ambiguous where the header clock (which is always now) is not. */
function fmtTimeOfDay(date, use24) {
	if (!date || isNaN(date.getTime())) return EMDASH;
	var h = date.getHours();
	if (use24) return pad2(h) + ':' + pad2(date.getMinutes());
	return ((h % 12) || 12) + ':' + pad2(date.getMinutes()) + ' ' + (h < 12 ? 'AM' : 'PM');
}

/* The timeline's session tag. Titles here are sentences ("build agent 3 —
   offline since 04:12"); the lead clause identifies the session and the rest is
   detail the row has no room for, so cut at the first dash-style separator and
   only then clamp. Cutting on length alone lands mid-word on most of them. */
function shortTitle(t) {
	var s = String(t === null || t === undefined ? '' : t).trim();
	if (!s) return '(untitled)';
	var m = /^(.+?)\s+[—–-]\s+/.exec(s);
	if (m && m[1].length >= 6) s = m[1];
	if (s.length > TIMELINE_TITLE_MAX) s = s.slice(0, TIMELINE_TITLE_MAX - 1).replace(/\s+$/, '') + '…';
	return s;
}

/* Read live, not cached at boot: the setting can change under a running panel,
   and matchMedia is absent in some embedded builds. */
function reducedMotion() {
	try { return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); }
	catch (e) { return false; }
}

function logLine(msg) {
	if (window.console && window.console.log) window.console.log('[sidecrab] ' + msg);
}

/* ------------------------------------------------------------------- polling */

/* CLEAN-04. The companion SERVED this page, so the companion is this page's
   origin - in the panel host and in a browser preview alike. The retired
   `crabdPort` property fallback and the file/qrc branch beside it are gone: a
   page that picked its own cross-origin port from a setting could be pointed at
   a port nobody served it from, and the CORS round trip it needed existed only
   for the vendor host's null origin. */
function baseUrl() { return window.location.origin; }

function endpointUrl() { return baseUrl() + '/v1/state'; }

/* `force` is a DELIBERATE refresh (the pull gesture), which bypasses the stream
   gate and nothing else. SCA-019: the gate used to be unqualified, so the gesture
   the panel advertises was a no-op whenever the stream was open - including when it
   was open and silent, which is the one state the operator most needs a way out of.
   THE SINGLE-FLIGHT GUARD IS NOT BYPASSABLE: a poll already on the wire is the
   answer to "is this current", and a second one would only be a duplicate. */
function poll(force) {
	/* The diagnostics flush rides the poll CYCLE, not the poll itself — above the
	   in-flight guard, because whether the state fetch is stuck says nothing about
	   whether the operator's taps should reach the companion (v0.23.0). It is a
	   no-op unless diagnostics are on and there is something to ship. */
	diagFlush();
	/* The poll is the FALLBACK while the /v1/events stream is DELIVERING. A stream
	   that is open but has said nothing past its liveness deadline is not
	   delivering, so the poll resumes there without waiting for the reconnect. */
	if (!force && sseDelivering()) return;
	if (inFlight) return;
	inFlight = true;
	var url = mockName ? './mock/mock-state-' + mockName + '.json' : endpointUrl();

	/* A fetch that never settles would leave inFlight stuck and stop the poller
	   for the rest of the session — measured: a refused loopback connect can take
	   several seconds to reject. Abort well inside one poll interval. */
	var opts = { cache: 'no-store' };
	var ctl = null, timer = null;
	if (typeof AbortController !== 'undefined') {
		ctl = new AbortController();
		opts.signal = ctl.signal;
		timer = setTimeout(function () { try { ctl.abort(); } catch (e) {} }, POLL_TIMEOUT_MS);
	}
	function done() { if (timer) clearTimeout(timer); inFlight = false; }

	fetch(url, opts)
		.then(function (r) {
			if (!r.ok) throw new Error('HTTP ' + r.status);
			return r.json();
		})
		.then(function (doc) { done(); acceptDoc(doc); })
		.catch(function () { done(); pollFailed = true; render(); });
}

function acceptDoc(doc) {
	if (mockName) doc = rebaseMock(doc, mockName === 'stale' ? 185000 : 2000);

	if (!doc || !(doc.schema >= 1 && doc.schema <= SCHEMA_MAX) || doc.schema !== Math.floor(doc.schema)) {
		/* An unreadable document is a dead feed, not fresh data. Above the ceiling
		   is a REAL break — an existing field changed meaning — and rendering it
		   would be worse than failing, because every field below would silently
		   say something else. Additive fields never arrive this way: they arrive
		   under the same number and are picked up by presence. */
		pollFailed = true;
		feedUnreadable = true;
		render();
		return;
	}
	var gen = Date.parse(doc.generatedAt);
	if (!isFinite(gen)) { pollFailed = true; feedUnreadable = true; render(); return; }

	/* SCA-020 — ORDERING. Two transports feed this one function and they overlap by
	   construction: the startup and reconnect GET is issued before the stream is
	   established, so a poll can resolve AFTER a pushed snapshot that is newer than
	   it. Accepting the older one rolled the cards, the freshness line and the alert
	   state backwards, and re-fired the chime for a question that had already been
	   answered - measured with a 300 ms poll against a 2 s-newer stream frame.
	   EQUAL generatedAt IS KEPT, deliberately, and this is the half that is easy to
	   get wrong in the other direction: the companion's timestamps have one-second
	   resolution and it publishes a changed document inside one second, so dropping
	   equal-timestamp documents would drop supported updates. Only STRICTLY older
	   is dropped.
	   THE TWO ESCAPES, because an ordering rule with no way out freezes a panel on
	   a healthy companion:
	     - a changed crabd.startedAt is a RESTART, which may legitimately have moved
	       the clock backwards. The baseline resets outright.
	     - a clock that moved backwards WITHOUT a restart would otherwise make every
	       document look older for ever. Past the staleness horizon the newest
	       document wins whatever its timestamp says - a feed this old is not one
	       this rule is protecting any more. */
	var started = doc.crabd && typeof doc.crabd.startedAt === 'string' ? doc.crabd.startedAt : null;
	if (started !== crabdStartedSeen) {
		crabdStartedSeen = started;
		orderingDroppedSince = 0;
	} else if (everHadData && gen < lastGoodAtMs) {
		if (!orderingDroppedSince) orderingDroppedSince = Date.now();
		if (Date.now() - orderingDroppedSince <= STALE_MS) {
			logLine('out of order: dropped a snapshot ' + (lastGoodAtMs - gen) +
				'ms older than the one on the glass');
			return;
		}
		logLine('out of order for ' + STALE_MS + 'ms with no restart: taking the newest document');
	}
	orderingDroppedSince = 0;

	pollFailed = false;
	feedUnreadable = false;
	lastGoodDoc = doc;
	lastGoodAtMs = gen;
	/* A crabd restart under a live widget may have ADDED /v1/config, or added a
	   key to its whitelist, so a changed version string re-opens capability
	   detection for the endpoint AND for every key. Not a gate: the value is
	   never compared or ordered, only tested for having changed. */
	var ver = doc.crabd && typeof doc.crabd.version === 'string' ? doc.crabd.version : null;
	if (ver !== crabdVersionSeen) {
		crabdVersionSeen = ver;

		/* v0.22.0: the quiet-override latch clears with the rest. A redeploy is
		   exactly what would add the action, and the alternative is a chip that stays
		   hidden until somebody reloads the panel. */
		quietOverrideUnsupported = false;
		/* v0.23.0: and the panel-log latch. A redeploy to crabd 0.24.0 is exactly
		   what ADDS the endpoint, and the alternative is a diagnostic session that
		   captures perfectly and ships nothing until somebody reloads the panel. */
		diagUnsupported = false;
	}
	everHadData = true;
	/* MF-017: the readiness that gates the pairing attempt is a field of THIS
	   document, so the attempt is made once one has landed. */
	maybeVerifyApproval();
	/* ONE SAMPLE PER POLL (v0.22.0), taken here and not in render(): render runs on
	   the 1 Hz tick and on every tap, and sampling there would record the same
	   document a dozen times and call it a history. */
	sampleHost(doc);
	applyPinOverride(doc);
	/* Before render, because the celebration is a mood render() has to pick up in
	   the same pass — a latch set after it would show a frame late. */
	detectCelebration(doc);
	/* Same discipline, same reason: the wardrobe's edges (a finish landing, the
	   last working session ending) only exist between two documents, and the hat
	   they set has to be in the render that follows, not a frame later. */
	detectTricks(doc);
	/* lane B: the chime rides the same edge, and for the same reason — a session
	   starting to wait exists only BETWEEN two documents, so it is detected here
	   and not in render(), which runs on the 1 Hz tick and on every tap. */
	detectChime(doc);
	render();
	maybeAutoGesture();
	maybeAutoOpenSheet();
}

/* ------------------------------------------------------- celebration (v0.4.0) */

/* working -> done on a turn that ran longer than CELEBRATE_MIN_TURN_MS: both arms
   up for ten seconds. Fires from the state MAP, not from a timer, so it is
   inherently one-shot per transition — the previous doc is consumed and replaced
   whether or not anything fired. The `celebrateUntil` guard is the second latch,
   for two long turns landing in one poll: one celebration, not two stacked. */
function detectCelebration(doc) {
	var sessions = doc && Array.isArray(doc.sessions) ? doc.sessions : [];
	var quiet = !!(doc && doc.quiet && doc.quiet.active === true);
	var next = {};
	var now = Date.now();
	for (var i = 0; i < sessions.length; i++) {
		var s = sessions[i];
		if (!s || !s.id) continue;
		next[s.id] = { state: s.state, turn: s.turnStartedAt || null };
		var prev = prevSessionState[s.id];
		if (quiet || !prev || prev.state !== 'working' || s.state !== 'done' || !prev.turn) continue;
		var t0 = Date.parse(prev.turn);
		/* The done row's own stateSince is when the turn ENDED. Falling back to now
		   only matters for a feed that omits it, and overstates by at most one poll. */
		var t1 = Date.parse(s.stateSince);
		if (!isFinite(t1)) t1 = now;
		if (isFinite(t0) && t1 - t0 > CELEBRATE_MIN_TURN_MS) fireCelebrate();
		/* The finish dance (v0.28.0) rides the same edge with a much lower bar: any
		   real turn, not only a half-hour one. Waiting sessions are counted off the
		   SAME document, so a landing that arrives beside an open question is a
		   quiet landing - the alert stays the only thing moving. */
		if (isFinite(t0) && t1 - t0 >= DANCE_MIN_TURN_MS && !anyWaiting(sessions)) fireDance(quiet);
	}
	prevSessionState = next;
}

function anyWaiting(sessions) {
	for (var i = 0; i < sessions.length; i++) {
		if (sessions[i] && sessions[i].state === 'needs_input') return true;
	}
	return false;
}

function fireCelebrate() {
	if (Date.now() < celebrateUntil) return;
	celebrateUntil = Date.now() + CELEBRATE_MS;
	/* The poll would drop the mood within 3 s of expiry anyway; this makes the
	   ten seconds exact rather than "ten seconds, give or take a poll". */
	setTimeout(render, CELEBRATE_MS + 50);
}

/* ---------------------------------------------- the wardrobe (v0.11.0) */

/* auto (dress for the fleet) or plain (never any accessory). The property ships
   as a SWITCH, so the value that actually arrives is a boolean or the strings
   "true"/"false" — but the WORDS are accepted too, so the day this becomes a
   proper enum control nothing in here changes. Anything unrecognised is auto:
   the default is the feature being on. */
function crabPlain() {
	var v = hostProp('crabStyle');
	if (v === undefined || v === null || v === '') return false;
	if (typeof v === 'string') {
		var s = v.trim().toLowerCase();
		return s === 'plain' || s === 'false' || s === '0' || s === 'off' || s === 'none';
	}
	return !v;
}

/* What the fleet is asking the crab to wear, before any hysteresis. Returns ''
   for "nothing", which is a real answer and not an absence: a waiting session
   takes the hat off outright.
   Read off the DOCUMENT's sessions, not off the cards — a dismissed card is
   still a session that is working, and the crab reports the fleet rather than
   the grid. */
function desiredAccessory(sessions, quiet, status) {
	/* A hat on a panel that cannot see anything is the panel lying. Stale and
	   connecting both paint their own mood (worried / asleep) and neither knows
	   what the sessions are doing any more. */
	if (status !== 'live' || crabPlain()) return '';

	var working = 0, waiting = 0, n = 0;
	for (var i = 0; i < sessions.length; i++) {
		var s = sessions[i];
		if (!s) continue;
		n++;
		if (s.state === 'working') working++;
		else if (s.state === 'needs_input') waiting++;
	}
	/* Alerts stay serious. Acked or not, quiet or not: a session is waiting on a
	   human and the crab is not wearing a party hat while that is true. */
	if (waiting > 0) return '';

	if (Date.now() < partyUntil) return 'party';
	/* QUIET FIRST (v0.19.0). Quiet hours is the operator's own declaration that it
	   is night, and a BUSY night is the ordinary kind — a long run left going
	   overnight is every session working with the limits calm, which is exactly the
	   sunglasses' condition. With sunglasses above it the nightcap could only appear
	   on a night the fleet was ALSO idle or mixed, so the costume for "it is night"
	   was unreachable on the nights there was anything to watch. See ACCESSORIES. */
	if (quiet) return 'nightcap';
	/* "Everything is cooking AND nothing is running hot" — every row working, not
	   merely one of them, and no usage window into the amber. A grid with a done
	   card in it has not earned the sunglasses, and neither has an estate three
	   percent off its weekly cap: the sunglasses are the costume for "nothing
	   needs attention", so a gauge that is ITSELF asking for attention has to
	   count. limitsCalm() reads the same limits block the gauges render from, so
	   the crab and the gauge can never disagree about whether it is calm. */
	if (n > 0 && working === n && limitsCalm()) return 'sunglasses';
	return '';
}

/* Every usage window the feed reports, below the gauges' own amber step. Absent
   limits are CALM, not hot: a panel that cannot see the limits has no business
   claiming they are a problem, and the standalone/stale cases have already been
   turned away above. */
function limitsCalm() {
	var limits = lastGoodDoc && lastGoodDoc.limits;
	if (!limits || limits.available !== true) return true;
	var wins = [limits.fiveHour, limits.weekly];
	if (Array.isArray(limits.extra)) wins = wins.concat(limits.extra);
	for (var i = 0; i < wins.length; i++) {
		var u = wins[i] && wins[i].utilization;
		if (typeof u === 'number' && isFinite(u) && u * 100 >= GAUGE_AMBER_PCT) return false;
	}
	return true;
}

/* Any usage window AT OR PAST the gauges' own red step (v0.22.0) — the trigger for
   the sweating mood. It is limitsCalm()'s mirror one step up the ramp and it is
   written beside it on purpose: both read the SAME limits block the gauges render
   from and both use the gauges' own constants, so the crab and the bar under it can
   never disagree about what red means.
   Absent limits are not red, for the reason limitsCalm() gives: a panel that cannot
   see the limits has no business having an opinion about them. */
function limitsRed() {
	var limits = lastGoodDoc && lastGoodDoc.limits;
	if (!limits || limits.available !== true) return false;
	var wins = [limits.fiveHour, limits.weekly];
	if (Array.isArray(limits.extra)) wins = wins.concat(limits.extra);
	for (var i = 0; i < wins.length; i++) {
		var u = wins[i] && wins[i].utilization;
		if (typeof u === 'number' && isFinite(u) && u * 100 >= GAUGE_RED_PCT) return true;
	}
	return false;
}

/* The hysteresis. Called from render(), so its grain is the 3 s poll — which is
   what ACC_STABLE_MS is measured in, not against.
   Two deliberate bypasses:
   - '' (nothing) applies INSTANTLY in both directions. Waiting ten seconds to
     remove a hat because a question arrived would be the exact failure the
     "alerts stay serious" rule exists to prevent, and taking one off cannot flap
     in a way anyone minds.
   - 'party' applies instantly too, because it is not a CONDITION. It is a 60 s
     latch opened by an edge that has already happened, so it cannot flap by
     construction — and holding a one-minute celebration back for a sixth of its
     life to prove something the latch already guarantees is a delay with nothing
     on the other side of it. Everything the fleet can strobe — sunglasses and
     nightcap — goes through the timer. */
function applyWardrobe(desired) {
	var now = Date.now();
	if (desired === '' || desired === 'party') {
		accCurrent = desired;
		accCandidate = desired;
		accCandidateAt = now;
	} else if (desired === accCurrent) {
		accCandidate = desired;
		accCandidateAt = now;
	} else if (desired !== accCandidate) {
		accCandidate = desired;
		accCandidateAt = now;
	} else if (now - accCandidateAt >= ACC_STABLE_MS) {
		accCurrent = desired;
	}
	var wear = accForced !== null ? accForced : accCurrent;
	/* The finish dance wears the shades for its 1.5 s, unless the wardrobe is plain
	   or a dev flag is holding a costume. It never touches accCurrent, so the
	   hysteresis timer is undisturbed and the fleet's own answer returns unchanged. */
	if (accForced === null && now < danceUntil && !crabPlain()) wear = 'sunglasses';
	if (ui.crab.getAttribute('data-acc') !== wear) ui.crab.setAttribute('data-acc', wear);
}

/* ------------------------------------------------------ tricks (v0.11.0) */

/* One-shots, latched exactly the way the flash and the wave are: a boolean that
   is cleared on a TIMER rather than on animationend, because under
   prefers-reduced-motion the animation is `none` and animationend never fires —
   an animationend-only reset latches the flag true forever and silently kills
   every later trick.
   All three are skipped outright under reduced motion and under quiet hours:
   quiet means nothing on this panel moves, and a trick is pure motion with no
   information in it. */
function fireSnap() {
	if (snapping || reducedMotion() || document.body.classList.contains('quiet')) return;
	snapping = true;
	ui.crab.classList.add('snap');
	setTimeout(function () { ui.crab.classList.remove('snap'); snapping = false; }, SNAP_MS);
}

function fireBounce(quiet) {
	if (bouncing || reducedMotion() || quiet) return;
	bouncing = true;
	ui.crab.classList.add('bounce');
	setTimeout(function () { ui.crab.classList.remove('bounce'); bouncing = false; }, BOUNCE_MS);
}

/* The finish dance (v0.28.0). Latched like the others, cleared on a timer, skipped
   under reduced motion and quiet. The shades come from `danceUntil`, which
   applyWardrobe reads: the dance is the one moment the crab wears sunglasses
   without the fleet having earned them, and it takes them off itself when the
   music stops (the render at the end re-derives the wardrobe from the document).
   `plain` wardrobe still dances - bare-shelled - because the switch is about
   costumes, not about motion. */
function fireDance(quiet, force) {
	if (dancing || reducedMotion() || quiet) return;
	var now = Date.now();
	if (!force && danceLastAt && now - danceLastAt < DANCE_COOLDOWN_MS) return;
	danceLastAt = now;
	dancing = true;
	/* +120 ms: setTimeout jitter must never take the shades off before the last beat. */
	danceUntil = now + DANCE_MS + 120;
	ui.crab.classList.add('dance');
	applyWardrobe(accCurrent);
	setTimeout(function () {
		ui.crab.classList.remove('dance');
		dancing = false;
		danceUntil = 0;
		render();
	}, DANCE_MS);
}

/* The easter egg, and the only thing on this panel with a cooldown: five sessions
   working at once is a state that can persist for an hour, and a crab that
   juggles every three seconds for that hour is a crab nobody looks at again. */
function fireJuggle(quiet, force) {
	if (juggling || reducedMotion() || quiet) return;
	var now = Date.now();
	if (!force && juggleLastAt && now - juggleLastAt < JUGGLE_COOLDOWN_MS) return;
	juggleLastAt = now;
	juggling = true;
	ui.crab.classList.add('juggling');
	setTimeout(function () { ui.crab.classList.remove('juggling'); juggling = false; }, JUGGLE_MS);
}

/* Fires the edge-triggered wardrobe events off a new document, next to
   detectCelebration and for the same reason: the previous document is the only
   place an edge exists, and it is consumed and replaced whether or not anything
   fired. */
function detectTricks(doc) {
	var sessions = doc && Array.isArray(doc.sessions) ? doc.sessions : [];
	var quiet = !!(doc && doc.quiet && doc.quiet.active === true);
	var working = 0, waiting = 0;
	for (var i = 0; i < sessions.length; i++) {
		if (!sessions[i]) continue;
		if (sessions[i].state === 'working') working++;
		else if (sessions[i].state === 'needs_input') waiting++;
	}

	/* The party hat, on the INCREMENT of the day's finished count. Never on the
	   value: a widget that boots at "4 done" has not just watched four sessions
	   land. A DECREASE is the local day rolling over at midnight, which is not a
	   finish either — the strict > is what makes both true. */
	var recap = doc && doc.recap;
	var done = recap && typeof recap.doneToday === 'number' && isFinite(recap.doneToday)
		? recap.doneToday : null;
	if (done !== null) {
		if (prevDoneToday !== null && done > prevDoneToday && !quiet) {
			partyUntil = Date.now() + PARTY_MS;
			/* The poll would drop the hat within 3 s of expiry anyway; this makes the
			   minute exact rather than "a minute, give or take a poll". */
			setTimeout(render, PARTY_MS + 50);
		}
		prevDoneToday = done;
	}

	/* The bounce: the LAST working session lands and nothing is waiting. Both
	   halves matter — a grid that still has a question in it has not finished. */
	if (prevWorkingCount !== null && prevWorkingCount > 0 && working === 0 && waiting === 0) {
		fireBounce(quiet);
	}
	prevWorkingCount = working;

	if (working >= JUGGLE_MIN_WORKING) fireJuggle(quiet, false);
}

function computeStatus() {
	if (!everHadData) return 'connecting';
	/* Contract: a failed poll OR a generatedAt older than 30 s is the stale state.
	   Silence must never render as all-green, so a single failure counts. */
	if (pollFailed) return 'stale';
	if (Date.now() - lastGoodAtMs > STALE_MS) return 'stale';
	return 'live';
}

/* ------------------------------------------------------------------ rendering */

function render() {
	if (!ui.ready) return;
	var status = computeStatus();
	var use24 = use24Clock();
	var body = document.body;

	body.classList.toggle('stale', status === 'stale');
	body.classList.toggle('connecting', status === 'connecting');

	if (status === 'connecting') {
		setText(ui.bannerText, 'connecting to crabd ' + EMDASH + ' no data yet');
	} else if (status === 'stale') {
		/* fmtTimeOfDay, not fmtClock (v0.20.0, CD-31/41): this is a moment in the
		   PAST, and the 12-hour form of fmtClock carries no meridiem — "data as of
		   6:12" on a panel read at 9 a.m. is twelve hours ambiguous in the one line
		   whose whole job is to say how old the reading is. The header clock keeps
		   fmtClock because it is always NOW and the date line beside it carries the
		   AM/PM already. */
		setText(ui.bannerText, 'crabd not responding ' + EMDASH + ' data as of ' +
			fmtTimeOfDay(new Date(lastGoodAtMs), use24));
	}

	var doc = lastGoodDoc;
	var sessions = doc && Array.isArray(doc.sessions) ? doc.sessions : [];

	var quiet = !!(doc && doc.quiet && doc.quiet.active === true);
	/* v0.20.0 (CD-42). `quiet.active` is crabd's answer, and a dead companion
	   answers nothing — so a panel that dimmed at 22:00 and lost its companion at
	   23:00 goes on rendering lastGoodDoc's `active: true` at noon the next day.
	   When the feed is STALE the window's own end is re-evaluated locally from the
	   start/end the document already carries. */
	if (quiet && status === 'stale' && quietWindowOver(doc.quiet, new Date())) quiet = false;
	body.classList.toggle('quiet', quiet);
	setText(ui.quietNote, quiet ? quietNoteText(doc.quiet) : '');

	pruneAcks(sessions);
	pruneDismissed(sessions);

	renderLimits(doc ? doc.limits : null, use24);
	renderBurn(doc ? doc.burn : null);
	renderSessions(sessions, status, quiet, doc ? doc.recap : null);
	renderFleet(doc ? doc.fleet : null);
	/* v0.21.0. Reads the document and not `status`: on a STALE feed the last good
	   host figures stay on the row exactly as the session cards and the gauges do,
	   under the same panel-wide stale treatment. A row that blanked itself while
	   everything beside it kept its last reading would be inventing a third state. */
	renderHost(doc ? doc.host : null);
	renderCoreLine(status, sessions, doc ? doc.limits : null);
	/* v0.19.0. Reads `status` and nothing else: the History chip is an offer to
	   read the companion's own file, and a panel that cannot see the companion
	   must not be making it. */
	setHistoryChip(status);
	/* v0.22.0. Reads `status` for the same reason the History chip does: this control
	   exists only to write to the companion, so a panel that cannot see one has no
	   business offering it. */
	renderMoonChip(status);
	noteApprovalSeed(doc ? doc.toast : null);
	syncSheet();

	/* An acked session contributes NOTHING to panel-level alert state — that is
	   the whole point of the ack — but it is still a waiting session, so it keeps
	   its card and still counts as "someone is here" for the crab's mood. */
	var alertNow = false;
	var anyWaiting = false;
	for (var i = 0; i < sessions.length; i++) {
		if (!sessions[i] || sessions[i].state !== 'needs_input') continue;
		anyWaiting = true;
		if (!effectiveAcked(sessions[i])) alertNow = true;
	}
	body.classList.toggle('alert', alertNow && !quiet);
	applyEscalation(Date.now(), quiet);

	/* One flash on the transition INTO the alert state, then steady (§4.4).
	   prevAlert only advances on live data, so a stale window cannot manufacture
	   a transition when the feed comes back. prevAlert advances under quiet too:
	   quiet suppresses the flash, it must not bank one for 07:00. */
	if (status === 'live') {
		if (alertNow && !prevAlert && !quiet) {
			fireWave();
			if (boolProp('alertFlash', true)) fireFlash();
		}
		prevAlert = alertNow;
	}

	/* Celebrating sits BELOW alert in the ladder: a raised-in-triumph crab while a
	   question waits would be reading the room wrong, and quiet clears it outright. */
	var celebrating = !quiet && status === 'live' && (celebrateForced || Date.now() < celebrateUntil);

	/* The wardrobe reads the same three facts the mood does, so it is computed
	   here rather than anywhere else — one pass, one answer, no chance of the
	   crab wearing a hat the mood disagrees with. */
	applyWardrobe(desiredAccessory(sessions, quiet, status));

	/* THE MOOD LADDER, and where `sweating` was put in it (v0.22.0).

	   Sweating means a usage window is at or past the gauges' RED step. It is a
	   standing fact about the account that can hold for hours, which is what decides
	   every one of its neighbours:

	   - BELOW connecting / stale. Both of those mean the panel cannot see anything,
	     and a crab reacting to a limit it read twenty minutes ago would be the panel
	     claiming a live opinion about a dead feed.
	   - BELOW quiet. Quiet hours is the operator saying "night mode", and the rule
	     everywhere else on this panel is that quiet clears everything — the glow, the
	     pulse, the tricks, the escalation. A crab sweating in a dark room is the
	     panel raising its voice in exactly the hours it was told not to. The gauge is
	     still red and still says so; the crab stops narrating it.
	   - BELOW waving. A session is waiting on a HUMAN. A limit is a fact about the
	     account and will still be true in five minutes; a question is the one thing
	     on this panel that is about the person standing in front of it.
	   - BELOW celebrating, and this one is the close call. Celebrating outranks it
	     because it is a TEN SECOND latch that clears itself, and sweating resumes the
	     moment it does — so nothing is lost. The other order loses the whole feature:
	     a red weekly window lasts hours, so sweating-over-celebrating would silently
	     delete every celebration on a busy estate, which is precisely when a
	     half-hour turn landing is worth marking.
	   - ABOVE the empty-grid asleep, deliberately. A window at 97% is true whether or
	     not anything is running right now — it is a fact about the account and not
	     about the grid — and an operator walking past an idle panel is exactly who
	     needs to know before they start the next thing. */
	var mood = status === 'connecting' ? 'asleep'
		: status === 'stale' ? 'worried'
		: quiet ? (anyWaiting ? 'content' : 'asleep')
		: alertNow ? 'waving'
		: celebrating ? 'celebrating'
		: limitsRed() ? 'sweating'
		: sessions.length === 0 ? 'asleep' : 'content';
	/* Dev-only, mock mode only: held AFTER the ladder above has run, so the flag
	   overrides the ANSWER and never the derivation. */
	if (moodForced) mood = moodForced;
	if (ui.crab.getAttribute('data-mood') !== mood) ui.crab.setAttribute('data-mood', mood);

	/* lane C: the other three views of the grid zone, and the Sessions chip's alert
	   badge. Last, because the badge counts off the grid this render has just
	   built and the Detail page reads the same `sessions` array everything above
	   it did. */
	laneCRenderViews(doc, sessions, status, quiet);
}

/* WHY THE PANEL IS QUIET, which stopped being one answer at v0.22.0.

   The line has said "quiet until 07:00" since v0.4.0, and that is the window's own
   end — correct while the SCHEDULE is what made the panel quiet. An override "on"
   ends when it ends, typically in an hour, and the window's end is then a different
   time entirely: the panel would have been claiming quiet until 07:00 on an
   override that ran out at 14:20. Two causes, so two sentences, and the override is
   the one that names itself because it is the one somebody chose. */
function quietNoteText(q) {
	var ov = quietOverrideFromFeed();
	if (ov && ov.mode === 'on') {
		var left = ov.until === null ? null : ov.until - Date.now();
		/* Unknown remaining renders the cause with no clock rather than a made-up
		   one — the rule the whole override reading keeps. */
		return left !== null && left > 0 ? 'quiet override ' + fmtDur(left / 1000) : 'quiet override';
	}
	return q && q.end ? 'quiet until ' + String(q.end) : 'quiet hours';
}

/* ------------------------------------------------------- fleet dots (v0.6.0) */

/* SideCrab observing its own Scheduled Tasks. Ambient by construction: the row
   is muted, never animates and never contributes to the alert state — a stopped
   helper is a thing to notice on the next walk past the desk.
   The whole row is hidden when the feed carries no fleet block (an older crabd),
   because two grey dots are indistinguishable from two dead services.
   A value the contract does not define is rendered as "unknown", never guessed
   into running: an unreadable task state is exactly what unknown is for. */
function renderFleet(fleet) {
	var have = !!(fleet && typeof fleet === 'object' && !Array.isArray(fleet));
	ui.fleet.classList.toggle('shown', have);
	if (!have) return;
	for (var i = 0; i < FLEET_PARTS.length; i++) {
		var part = FLEET_PARTS[i];
		var node = ui[part.el];
		if (!node) continue;
		var raw = fleet[part.key];
		var state = (typeof raw === 'string' && FLEET_STATES[raw]) ? raw : 'unknown';
		if (node.getAttribute('data-state') !== state) node.setAttribute('data-state', state);
		/* The letter is the component; the word is for the screen reader and for
		   anyone who taps and holds. Colour and shape are the on-glass cues. */
		var label = part.label + ' ' + state;
		if (node.getAttribute('title') !== label) {
			node.setAttribute('title', label);
			node.setAttribute('aria-label', label);
		}
	}
}

/* ------------------------------------------------- dismissed cards (v0.4.0) */

function isDismissed(s) {
	if (!s || !DISMISSABLE[s.state]) return false;
	return dismissed[s.id] === String(s.stateSince || '');
}

/* Same shape as pruneAcks: a dismissal whose row is gone, or has moved to a new
   stateSince, is dropped — so the map cannot grow for the life of the panel and
   a session that goes done -> working -> done comes back with a fresh card.
   The state is in the live map's KEY test only through stateSince, which is
   enough: crabd moves stateSince on every transition, so done -> idle drops the
   dismissal by itself. */
function pruneDismissed(sessions) {
	var live = {};
	for (var i = 0; i < sessions.length; i++) {
		var s = sessions[i];
		if (s && DISMISSABLE[s.state]) live[s.id] = String(s.stateSince || '');
	}
	for (var id in dismissed) {
		if (!Object.prototype.hasOwnProperty.call(dismissed, id)) continue;
		if (live[id] === undefined || live[id] !== dismissed[id]) delete dismissed[id];
	}
}

/* ------------------- persisted display state: pins, filter, density (v0.8.0) */

/* ONE OBJECT UNDER ONE KEY, and the shape is kept although the host that required
   it is retired: everything this panel persists - the pin map, the filter, the
   density, the view - is a PROPERTY INSIDE one JSON object stored under the host's
   `uniqueId`, never a localStorage key of its own. Scattering bare keys across an
   origin is how a page collides with whatever else is served from it, and the
   single object is also the thing that makes a round-trip of an unknown value
   possible (see viewStoredUnknown).
   DISPLAY STATE ONLY: no credentials, no personal data. A map of session ids the
   operator chose to keep at the front of their own panel is display state and
   nothing more.

   Feature-detected on both halves, because both can be absent:
     - `uniqueId` is injected by the panel host and is absent in a plain browser
       preview, where hostProp answers with the fixed 'standalone' key.
     - localStorage itself THROWS on access in some locked-down profiles, so
       every call is wrapped rather than tested once for existence.
   Either one missing leaves prefsStoreKey null and the map in memory for the
   session. Silent by design: a pin that does not survive a restart is a
   nuisance, and an error banner about it would be worse than the nuisance. */
function prefsStorage() {
	try {
		return window.localStorage || null;
	} catch (e) { return null; }
}

/* Read the whole properties object once. Returns null when there is nothing to
   read — no key, no storage, no stored object, or an object that no longer
   parses. Every caller treats null as "this widget has no stored state", which
   is the correct reading of all four. */
function readPrefs() {
	if (!prefsStoreKey) return null;
	var store = prefsStorage();
	if (!store) return null;
	var raw;
	try { raw = store.getItem(prefsStoreKey); } catch (e) { return null; }
	if (!raw) return null;
	var props;
	try { props = JSON.parse(raw); } catch (e) { return null; }
	if (!props || typeof props !== 'object' || Array.isArray(props)) return null;
	return props;
}

/* Find `key` in `list` and return its index, or 0. A stored key this build does
   not know (an older or newer widget wrote it) is not an error and not a reason
   to say anything on glass — it is the default mode. */
function prefIndex(list, key) {
	var i = prefIndexOrNone(list, key);
	return i < 0 ? 0 : i;
}

/* Same lookup, but -1 for "this build does not know that key" — the distinction
   prefIndex deliberately throws away and the one savePrefs needs (v0.16.0, audit
   F2). Kept as the primitive so there is exactly one key→index scan. */
function prefIndexOrNone(list, key) {
	for (var i = 0; i < list.length; i++) {
		if (list[i].key === key) return i;
	}
	return -1;
}

function loadPrefs() {
	/* Dev-only, mock mode only: &uid= stands in for the host-injected uniqueId so
	   the REAL storage path (same code, same JSON object shape) can be exercised
	   and its reload behaviour photographed off-glass. It is read FIRST because
	   hostProp answers 'standalone' for this one key rather than undefined, so a
	   fallback behind it could never be reached. */
	var key = (mockName && devUidOverride) ? devUidOverride : hostProp('uniqueId');
	if (key === undefined || key === null || key === '') { prefsStoreKey = null; return; }
	prefsStoreKey = String(key);

	var props = readPrefs();
	if (!props) return;

	/* The two chips (v0.15.0). Strings, read the same defensive way the pin map
	   is: anything that is not one of this build's own keys clamps to index 0. */
	if (typeof props[FILTER_PROP] === 'string') {
		var fi = prefIndexOrNone(FILTERS, props[FILTER_PROP]);
		filterIdx = fi < 0 ? 0 : fi;
		filterStoredUnknown = fi < 0 ? props[FILTER_PROP] : null;
	}
	if (typeof props[DENSITY_PROP] === 'string') {
		var di = prefIndexOrNone(DENSITIES, props[DENSITY_PROP]);
		densityIdx = di < 0 ? 0 : di;
		densityStoredUnknown = di < 0 ? props[DENSITY_PROP] : null;
	}
	/* lane C: the grid-zone view, read exactly the way the two chips above it are. */
	if (typeof props[VIEW_PROP] === 'string') {
		var vi = prefIndexOrNone(VIEWS, props[VIEW_PROP]);
		viewIdx = vi < 0 ? 0 : vi;
		viewStoredUnknown = vi < 0 ? props[VIEW_PROP] : null;
	}

	/* The approval-threshold touch record (v0.16.0). Read as defensively as the pin
	   map: a shape that has drifted degrades to "never touched", which is the
	   silent-and-preserving side. */

	var map = props[PIN_PROP];
	if (!map || typeof map !== 'object' || Array.isArray(map)) return;
	/* Read defensively: this object was written by an older version of this
	   widget, and a shape that has drifted must degrade to "nothing pinned"
	   rather than to a map full of NaN sort keys. */
	for (var id in map) {
		if (!Object.prototype.hasOwnProperty.call(map, id)) continue;
		var at = Number(map[id]);
		if (isFinite(at)) pinned[String(id)] = at;
	}
	evictPins();
}

/* READ-MODIFY-WRITE of the whole properties object, per the vendor pattern: the
   object may carry properties this version of the widget knows nothing about
   (or a future one will), and writing a fresh object would silently drop them.
   Every persisted property this build owns is written together, so a pin tap and
   a chip tap cannot race each other's copy of the object. */
function savePrefs() {
	if (!prefsStoreKey) return;
	var store = prefsStorage();
	if (!store) return;
	var props = readPrefs() || {};
	props[PIN_PROP] = pinned;
	/* A value this build did not recognise is ROUND-TRIPPED, not replaced (v0.16.0,
	   audit F2). The read-modify-write already preserved unknown KEYS; the gap was
	   unknown VALUES of a known key, which is what a newer widget's mode is. An
	   untouched save must leave the newer build's setting exactly as it found it —
	   a pin tap in an older build is not a statement about the filter. */
	props[FILTER_PROP] = filterStoredUnknown !== null ? filterStoredUnknown : FILTERS[filterIdx].key;
	props[DENSITY_PROP] = densityStoredUnknown !== null ? densityStoredUnknown : DENSITIES[densityIdx].key;
	/* lane C: same key, same object, same round-trip of a value this build does not know. */
	props[VIEW_PROP] = viewStoredUnknown !== null ? viewStoredUnknown : VIEWS[viewIdx].key;
	/* Written only once there is something to record, so a panel whose operator
	   never opens the property sheet does not accumulate a key either. */
	try { store.setItem(prefsStoreKey, JSON.stringify(props)); }
	catch (e) { logLine('display state save failed (storage refused the write)'); }
}

/* Oldest pin first, so the cap never evicts the pin somebody just took. */
function evictPins() {
	var ids = [];
	for (var id in pinned) {
		if (Object.prototype.hasOwnProperty.call(pinned, id)) ids.push(id);
	}
	if (ids.length <= PIN_MAX) return;
	ids.sort(function (a, b) { return pinned[a] - pinned[b]; });
	for (var i = 0; i < ids.length - PIN_MAX; i++) delete pinned[ids[i]];
}

function isPinned(id) {
	return id !== null && id !== undefined && pinned[String(id)] !== undefined;
}

function togglePin(id) {
	if (id === null || id === undefined || id === '') return;
	var key = String(id);
	if (pinned[key] !== undefined) delete pinned[key];
	else { pinned[key] = Date.now(); evictPins(); }
	savePrefs();
}

/* --------------------------------------- the header chips (v0.15.0) */

function currentFilter() { return FILTERS[filterIdx] || FILTERS[0]; }
function currentDensity() { return DENSITIES[densityIdx] || DENSITIES[0]; }

/* The filter is a VIEW, not a state the panel is in: the alert glow, the crab,
   the toast threshold and the ack-all gesture all still see every session, and
   they must — a panel that stopped glowing because the operator left it on
   "Working" would be a filter that hides the one thing this widget exists for.
   Only the CARD LIST and the count beside it narrow. */
function filterSessions(list) {
	var f = currentFilter();
	if (!f.match) return list;
	var out = [];
	for (var i = 0; i < list.length; i++) {
		/* hasOwnProperty, not a bare lookup: a feed that ever served a state named
		   `constructor` or `toString` would otherwise match EVERY bucket off the
		   prototype chain, and a filter that fails open is a filter that lies about
		   what it is showing. */
		var st = (list[i] && list[i].state) || 'idle';
		if (list[i] && Object.prototype.hasOwnProperty.call(f.match, st)) out.push(list[i]);
	}
	return out;
}

/* Density is a body class and nothing else — every number it changes (the grid's
   row count, the card's padding and type) lives in the stylesheet, so gridCapacity
   reads the result rather than a second copy of it. */
function applyDensity() {
	document.body.classList.toggle('density-compact', currentDensity().key === 'compact');
}

function cycleFilter() {
	filterIdx = (filterIdx + 1) % FILTERS.length;
	/* The tap is the operator overriding whatever a newer build had stored, so the
	   round-trip is dropped here and nowhere else (audit F2). */
	filterStoredUnknown = null;
	savePrefs();
	/* cardSig is cleared, not just moved: the filter changes WHICH rows are in the
	   grid, and the signature is built from the rows that survived it — two
	   different filters can produce the same signature (one card, same card) and
	   the grid would keep the other mode's cut. */
	cardSig = '';
	render();
}

function cycleDensity() {
	densityIdx = (densityIdx + 1) % DENSITIES.length;
	densityStoredUnknown = null;   /* same as cycleFilter — the tap is an override */
	savePrefs();
	applyDensity();
	/* Same reason as the filter, plus one of its own: capacity is read off the
	   grid's computed rows, and the class has only just changed them. */
	cardSig = '';
	render();
}

function syncHeaderChips() {
	if (ui.filterChip) {
		var f = currentFilter();
		setText(ui.filterChip, f.label);
		if (ui.filterChip.getAttribute('data-filter') !== f.key) ui.filterChip.setAttribute('data-filter', f.key);
		ui.filterChip.setAttribute('aria-label', 'Session filter: ' + f.label);
	}
	if (ui.densityChip) {
		var d = currentDensity();
		setText(ui.densityChip, d.label);
		if (ui.densityChip.getAttribute('data-density') !== d.key) ui.densityChip.setAttribute('data-density', d.key);
		ui.densityChip.setAttribute('aria-label', 'Card density: ' + d.label);
	}
}

/* Pinned first WITHIN a band, never across one: a pinned idle row must not climb
   over a session that is actually waiting on a human, which is the one ordering
   the panel exists to protect.

   The bands are read out of the order crabd already delivered (first appearance
   of each state) rather than declared here. That is deliberate: the contract
   says crabd pre-sorts needs_input, working, done, idle, and a second copy of
   that list in the widget is a copy that can disagree with the feed. With
   nothing pinned this function is the identity.

   Decorate-sort-undecorate with the original index as the last key, so the
   result does not depend on Array.prototype.sort being a stable sort. */
function sortPinned(list) {
	var band = {};
	var bands = 0;
	var i;
	for (i = 0; i < list.length; i++) {
		var st = (list[i] && list[i].state) || 'idle';
		if (band[st] === undefined) band[st] = bands++;
	}
	var dec = [];
	for (i = 0; i < list.length; i++) {
		dec.push({
			s: list[i],
			i: i,
			b: band[(list[i] && list[i].state) || 'idle'],
			p: isPinned(list[i] && list[i].id) ? 0 : 1
		});
	}
	dec.sort(function (a, b) { return (a.b - b.b) || (a.p - b.p) || (a.i - b.i); });
	var out = [];
	for (i = 0; i < dec.length; i++) out.push(dec[i].s);
	return out;
}

/* ------------------------------------------------------------ ack bookkeeping */

function effectiveAcked(s) {
	if (!s) return false;
	if (s.acked === true) return true;
	var mark = ackOptimistic[s.id];
	return mark !== undefined && mark === String(s.stateSince || '');
}

/* Drop optimistic acks whose session is gone or has moved on, so the map cannot
   grow for the life of the panel and a returning session id starts clean. */
function pruneAcks(sessions) {
	var live = {};
	for (var i = 0; i < sessions.length; i++) {
		var s = sessions[i];
		if (s && s.state === 'needs_input') live[s.id] = String(s.stateSince || '');
	}
	for (var id in ackOptimistic) {
		if (!Object.prototype.hasOwnProperty.call(ackOptimistic, id)) continue;
		if (live[id] === undefined || live[id] !== ackOptimistic[id]) delete ackOptimistic[id];
	}
}

/* ------------------------------------------------------------- escalation (v3) */

/* Tiers are read off the CARDS, not off the document, for two reasons: the
   cards already carry each session's stateSince and ack state, and this runs on
   the 1 Hz tick between polls, when lastGoodDoc has not moved. A card escalates
   on its OWN age — one question waiting 16 minutes must not make a question
   asked 20 seconds ago shout too — and the panel takes the highest tier of any
   card, because the glow is one object for the whole display.

   Quiet clears everything: an escalating panel in a dark room is exactly the
   thing quiet hours exist to prevent. */
function applyEscalation(nowMs, quiet) {
	var nodes = ui.cards ? ui.cards.children : [];
	for (var i = 0; i < nodes.length; i++) {
		var node = nodes[i];
		var tier = 0;
		if (!quiet && node.getAttribute('data-state') === 'needs_input' &&
			node.getAttribute('data-acked') !== '1') {
			var since = Number(node.getAttribute('data-state-since'));
			if (isFinite(since) && since > 0) {
				var age = nowMs - since;
				tier = age >= ESC_T2_MS ? 2 : age >= ESC_T1_MS ? 1 : 0;
			}
		}
		if (node.classList.contains('esc1') !== (tier === 1)) node.classList.toggle('esc1', tier === 1);
		if (node.classList.contains('esc2') !== (tier === 2)) node.classList.toggle('esc2', tier === 2);
		setEscBadge(node, tier, isFinite(Number(node.getAttribute('data-state-since')))
			? (nowMs - Number(node.getAttribute('data-state-since'))) / 1000 : NaN);
	}
	panelEscalation(nowMs, quiet);
}

/* THE PANEL-WIDE TIER IS READ OFF THE FEED, NOT OFF THE GRID (v0.20.0, CD-34).

   It used to be the max of the CARDS above, which quietly made the session
   filter an alert filter: with the chip left on Working, an unacked needs_input
   row is not in the DOM at all, so `top` stayed 0 and the panel's edge, glow and
   escalation tint all went out while the question stood. Measured on `dense`
   with `&filter=working&age=20`: two unacked waiting rows in the feed, a live
   pendingPermission among them, and `body.esc1`/`esc2`/`approval` all false.

   The filter narrows the CARD LIST; it must never narrow what the panel is
   allowed to shout about. Same list and same rule as `alertNow` in render() —
   the raw feed, ack state respected — so the two cannot disagree about whether
   anything is waiting. Quiet still clears everything, exactly as before. */
function panelEscalation(nowMs, quiet) {
	var sessions = lastGoodDoc && Array.isArray(lastGoodDoc.sessions) ? lastGoodDoc.sessions : [];
	var top = 0, anyApproval = false;
	for (var i = 0; i < sessions.length && !quiet; i++) {
		var s = sessions[i];
		if (!s || s.state !== 'needs_input') continue;
		/* Approval (v0.12.0) is the loudest state and it is not an age tier: a live
		   pendingPermission drives body.approval, which overrides the escalation
		   glow tint. It is not ackable, so it is read before the ack test. */
		if (s.pendingPermission && typeof s.pendingPermission === 'object' &&
			!Array.isArray(s.pendingPermission)) anyApproval = true;
		if (effectiveAcked(s)) continue;
		var since = Date.parse(s.stateSince);
		if (!isFinite(since) || since <= 0) continue;
		var age = nowMs - since;
		var tier = age >= ESC_T2_MS ? 2 : age >= ESC_T1_MS ? 1 : 0;
		if (tier > top) top = tier;
	}
	document.body.classList.toggle('esc1', top === 1);
	document.body.classList.toggle('esc2', top === 2);
	document.body.classList.toggle('approval', anyApproval);
}

/* The tier in words as well as in colour and motion — a panel read from across
   a room must not depend on telling two ambers apart. */
function setEscBadge(node, tier, ageSec) {
	var badges = node.querySelector('.card-badges');
	if (!badges) return;
	var badge = badges.querySelector('.badge-esc');
	if (tier === 0) {
		if (badge) badges.removeChild(badge);
		return;
	}
	if (!badge) {
		badge = makeBadge('', 'badge-esc');
		badges.appendChild(badge);
	}
	setText(badge, 'WAITING ' + (isFinite(ageSec) ? fmtDur(ageSec) : EMDASH));
}

/* Both one-shots clear on a TIMER, not on animationend. Under
   prefers-reduced-motion the animation is `none`, so animationend never fires —
   measured in Chromium 130 — and an animationend-only reset latches these flags
   true forever, silently killing every later alert. */
function fireFlash() {
	if (flashing) return;
	flashing = true;
	ui.flash.classList.add('fire');
	setTimeout(function () { ui.flash.classList.remove('fire'); flashing = false; }, 700);
}

/* Claw'd's two-frame arm toggle, on the transition into the alert state only.
   It settles back to the static raised arm the waving mood already paints. */
function fireWave() {
	if (waving) return;
	waving = true;
	ui.crab.classList.add('waveonce');
	setTimeout(function () { ui.crab.classList.remove('waveonce'); waving = false; }, 2100);
}

/* Fixed blue base, NOT var(--accent): a gauge is a reading of an external system
   and has to mean the same thing on every panel, so the personalization accent
   must not be able to recolour it. Colour is never the only cue — the percent
   text and the reset time sit right beside it. */
function rampColor(pct) {
	if (pct >= GAUGE_RED_PCT) return 'var(--red)';
	if (pct >= GAUGE_AMBER_PCT) return 'var(--amber)';
	return 'var(--gauge-blue)';
}

/* ------------------------------------------- reset countdowns (v0.7.0) */

/* "resets in 33 min", counted from resetsAt against the wall clock.
   Two honesty rules, both load-bearing:
   - A resetsAt in the PAST is not a negative countdown and not "in 0 min". A
     limits block can be served from a last-good reading through an endpoint
     lockout (the v0.4.0 caveat path), so a stale window is a thing that really
     happens — it falls back to the absolute clock time, which is what this line
     said before this version and is still true.
   - Under a minute reads "in <1 min", not "in 0 min": zero is a claim that it
     has already happened. */
function resetLabel(atMs, nowMs, use24) {
	var rem = atMs - nowMs;
	/* fmtTimeOfDay (v0.20.0, CD-41): the fallback is reached only for a reset that
	   is ALREADY PAST, so it is a moment somewhere in the last day rather than a
	   countdown — and a bare "6:12" beside a gauge does not say which 6:12. */
	if (!isFinite(rem) || rem <= 0) return fmtTimeOfDay(new Date(atMs), use24);
	if (rem < 60000) return 'in <1 min';
	var mins = Math.round(rem / 60000);
	if (mins < RESET_MIN_ONLY_MAX) return 'in ' + mins + ' min';
	var hours = Math.floor(mins / 60);
	if (hours < RESET_HOURS_ONLY_MAX) return 'in ' + hours + 'h ' + (mins % 60) + 'm';
	return 'in ' + Math.floor(hours / 24) + 'd ' + (hours % 24) + 'h';
}

/* The clock time the countdown replaced, kept as the gauge's tooltip. The date
   is appended when the reset is not today, because a weekly window resets four
   days out and a bare "7:00 AM" would read as tomorrow morning. */
function resetTooltip(d, use24) {
	return 'resets at ' + momentText(d, use24);
}

/* A wall-clock moment a person can read, with the DATE appended only when it is
   not today: a weekly window resets four days out and a bare "7:00 AM" would read
   as tomorrow morning. Split out of resetTooltip at v0.19.0 because the forecast
   sheet needs the same moment without the "resets at" prefix, and two copies of
   the same-day test is two chances to disagree about what "today" means. */
function momentText(d, use24) {
	var now = new Date();
	var t = fmtTimeOfDay(d, use24);
	if (d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() &&
		d.getDate() === now.getDate()) return t;
	var day;
	try { day = d.toLocaleDateString(undefined, { day: '2-digit', month: 'short' }); }
	catch (e) { day = d.toDateString(); }
	return t + ', ' + day;
}

/* The instant is parked on the span as an epoch and relabelled by the 1 Hz tick,
   the same idiom the card ages use: the poll is 3 s and a countdown that only
   moves on a poll crosses its minute boundary up to three seconds late.
   The attribute is REMOVED whenever there is no instant, so the tick cannot
   overwrite an em-dash with a label computed from a stale number. */
function setReset(gaugeEl, resetEl, resetsAt, use24) {
	var t = resetsAt ? Date.parse(resetsAt) : NaN;
	if (!isFinite(t)) {
		if (resetEl.hasAttribute('data-resets-at')) resetEl.removeAttribute('data-resets-at');
		setText(resetEl, EMDASH);
		if (gaugeEl && gaugeEl.hasAttribute('title')) gaugeEl.removeAttribute('title');
		return;
	}
	var key = String(t);
	if (resetEl.getAttribute('data-resets-at') !== key) resetEl.setAttribute('data-resets-at', key);
	setText(resetEl, resetLabel(t, Date.now(), use24));
	if (!gaugeEl) return;
	var tip = resetTooltip(new Date(t), use24);
	if (gaugeEl.getAttribute('title') !== tip) gaugeEl.setAttribute('title', tip);
}

/* Every countdown on the panel, relabelled on the tick. Queried rather than
   held in a list because the extra windows are rebuilt whenever their labels
   change; at most four nodes once a second is not a budget. */
function tickResets(nowMs, use24) {
	var nodes = document.querySelectorAll('.gauge-reset[data-resets-at]');
	for (var i = 0; i < nodes.length; i++) {
		var t = Number(nodes[i].getAttribute('data-resets-at'));
		if (!isFinite(t) || t <= 0) continue;
		setText(nodes[i], resetLabel(t, nowMs, use24));
	}
}

/* ------------------------------------------- depletion forecast (v0.13.0) */

/* "~full by 3:40 PM" — crabd's linear projection of when this window hits 100%
   (limits.<window>.exhaustAt, optional/nullable). A hint, never an alarm, so the
   honesty rules are strict and every one of them returns '' (render nothing):
   - No exhaustAt, or an unparseable one → nothing. Absence is the common case
     (flat/declining burn, or an older crabd that never emits the field).
   - An exhaustAt in the PAST → nothing. A projection whose moment has passed is
     not a forecast, and the gauge's own % already tells the true story.
   - exhaustAt at or AFTER the window's resetsAt → nothing. crabd never
     extrapolates past a reset, but the widget guards it anyway: a window resets
     before it depletes, so the honest line is no line. resetsAt unparseable →
     no reset to be "sooner than", so the guard cannot fire and the line shows.
   The "~" is mandatory — it is a projection, not a clock reading. The clock form
   matches the timeline (fmtTimeOfDay carries AM/PM) because a forecast can land
   many hours out; past a day it degrades to a short date, which a bare time
   could not disambiguate. */
function forecastLabel(exhaustAt, resetsAt, nowMs, use24) {
	var ex = exhaustAt ? Date.parse(exhaustAt) : NaN;
	if (!isFinite(ex) || ex <= nowMs) return '';
	var rs = resetsAt ? Date.parse(resetsAt) : NaN;
	if (isFinite(rs) && ex >= rs) return '';
	var d = new Date(ex);
	if (ex - nowMs > DAY_MS) {
		var day;
		try { day = d.toLocaleDateString(undefined, { day: '2-digit', month: 'short' }); }
		catch (e) { day = d.toDateString(); }
		return '~full ' + day;
	}
	return '~full by ' + fmtTimeOfDay(d, use24);
}

/* Recomputed on each poll rather than on the 1 Hz tick: the text is a fixed
   clock time, not a countdown, so it does not change second-to-second. Its only
   live transition — exhaustAt slipping into the past, or a fixture's window
   crossing its reset — is picked up at the next 3 s poll, which is soon enough
   for a hint. The element is HIDDEN, not em-dashed, when there is nothing to
   say: a forecast is optional, and an em-dash would read as a broken reading. */
function setForecast(forecastEl, win, use24) {
	if (!forecastEl) return;
	var label = win ? forecastLabel(win.exhaustAt, win.resetsAt, Date.now(), use24) : '';
	setText(forecastEl, label);
	forecastEl.classList.toggle('shown', label !== '');
}

/* The tap affordance follows the READING (v0.19.0). A gauge with no utilization has
   nothing behind it — openForecastSheet turns that tap away — and a chevron over an
   inert control is the panel promising something it will not do. The two fixed gauges
   carry `tappable` in the markup because that is their normal state; this takes it off
   for as long as the window has no number, and the class is what the cursor, the
   chevron and (through aria-disabled) a reader all read. */
function setGaugeTappable(gaugeEl, live) {
	if (!gaugeEl) return;
	if (gaugeEl.classList.contains('tappable') !== live) gaugeEl.classList.toggle('tappable', live);
	var flag = live ? 'false' : 'true';
	if (gaugeEl.getAttribute('aria-disabled') !== flag) gaugeEl.setAttribute('aria-disabled', flag);
}

function setGauge(gaugeEl, fillEl, pctEl, resetEl, win, use24, forecastEl) {
	var util = win && typeof win.utilization === 'number' && isFinite(win.utilization) ? win.utilization : null;
	setGaugeTappable(gaugeEl, util !== null);
	if (util === null) {
		/* Unknown limits render as em-dashes, never as 0% (§4.5). */
		setText(pctEl, EMDASH);
		setReset(gaugeEl, resetEl, null, use24);
		setForecast(forecastEl, null, use24);
		setVar(fillEl, '--w', '0');
		setVar(gaugeEl, '--gauge-color', 'var(--faint)');
		return;
	}
	var pct = Math.round(Math.max(0, Math.min(1, util)) * 100);
	setText(pctEl, pct + '%');
	setVar(fillEl, '--w', String(pct));
	setVar(gaugeEl, '--gauge-color', rampColor(pct));
	setReset(gaugeEl, resetEl, win.resetsAt, use24);
	setForecast(forecastEl, win, use24);
}

function renderLimits(limits, use24) {
	var available = !!(limits && limits.available === true);

	/* Provenance (v0.12.0). limits.source is "statusline" when the numbers came
	   from Claude Code's own status-line document, "oauth" for the fallback reach-
	   around, and absent on an older crabd. Only the statusline case earns the
	   "official" tag; oauth and absent both show nothing, because a tag that read
	   "oauth" would be labelling the ordinary state and adding noise to every
	   panel. Presence-detected — a source the contract does not name is not
	   "statusline", so it shows nothing rather than a guess. */
	var official = !!(limits && limits.source === 'statusline');
	if (ui.limitsSource) ui.limitsSource.classList.toggle('shown', official);

	setGauge(ui.gauge5h, ui.fill5h, ui.pct5h, ui.reset5h, available ? limits.fiveHour : null, use24, ui.forecast5h);
	setGauge(ui.gaugeWk, ui.fillWk, ui.pctWk, ui.resetWk, available ? limits.weekly : null, use24, ui.forecastWk);

	/* v0.4.0: `note` is no longer tied to available:false. It may now arrive as a
	   CAVEAT on lit gauges — "limits as of 2:41 PM" when crabd is serving a
	   last-good reading through an endpoint lockout. So any non-null note renders,
	   and only the unavailable case gets the amber failure tint plus the fallback
	   wording; a caveat on live numbers is muted, because nothing is broken. */
	var raw = limits && limits.note !== null && limits.note !== undefined ? String(limits.note) : '';
	var note = raw || (available ? '' : 'limits unavailable');
	setText(ui.limitsNote, note);
	ui.limitsNote.classList.toggle('shown', note !== '');
	ui.limitsNote.classList.toggle('caveat', note !== '' && available);

	/* Extra windows the usage endpoint reports (contract: limits.extra[]).
	   Capped at two so the zone cannot overflow at 720 px tall. */
	var extras = available && Array.isArray(limits.extra) ? limits.extra.slice(0, 2) : [];
	/* THE COLLAPSE (v0.26.0, AUD-F2). A second extra window costs 98.61 px in a
	   zone that had 20.04 px of slack, so the TODAY block gives up its sparkline
	   and drops to one stat line while one is being served — the stylesheet owns
	   what goes (body.limits-two-extras) and this line owns WHEN.
	   Driven by what the document SERVES, never by the slot: the two slots where
	   the zone renders .today are the two the overflow was measured at, and a
	   media query would collapse a one-extra panel that fits perfectly well. */
	document.body.classList.toggle('limits-two-extras', extras.length > 1);
	var sig = extras.map(function (e) { return String(e && e.label); }).join('|');
	if (sig !== extraSig) {
		extraSig = sig;
		ui.gaugeExtra.textContent = '';
		ui.extraRows = extras.map(function (e, i) { return buildGauge(String((e && e.label) || 'window'), ui.gaugeExtra, i); });
	}
	for (var i = 0; i < extras.length; i++) {
		var row = ui.extraRows[i];
		setGauge(row.root, row.fill, row.pct, row.reset, extras[i], use24, row.forecast);
	}
}

function buildGauge(label, parent, index) {
	var root = document.createElement('div');
	/* Tappable like the two fixed gauges (v0.19.0). The key is the extra's INDEX
	   in limits.extra, which is the only name it has — an extra window's label is
	   vendor text and could change between polls, and a key built from it would
	   open the wrong window the moment it did. The rows are rebuilt whenever the
	   label set changes, so the index and the row cannot drift apart. */
	root.className = 'gauge tappable';
	root.setAttribute('data-win', 'extra' + (index || 0));
	root.setAttribute('role', 'button');
	root.setAttribute('tabindex', '0');   /* v0.20.0, CD-15 — as the two fixed gauges */
	root.setAttribute('aria-label', label + ' window detail');
	var head = document.createElement('div');
	head.className = 'gauge-head';
	var name = document.createElement('span');
	name.className = 'gauge-name';
	name.textContent = label;
	var pct = document.createElement('span');
	pct.className = 'gauge-pct';
	head.appendChild(name);
	head.appendChild(pct);
	var track = document.createElement('div');
	track.className = 'gauge-track';
	var fill = document.createElement('div');
	fill.className = 'gauge-fill';
	track.appendChild(fill);
	var foot = document.createElement('div');
	foot.className = 'gauge-foot';
	foot.appendChild(document.createTextNode('resets '));
	var reset = document.createElement('span');
	/* The class is what the 1 Hz countdown tick finds; an extra window whose span
	   lacked it would freeze at the label it was built with. */
	reset.className = 'gauge-reset';
	foot.appendChild(reset);
	/* The forecast hint (v0.13.0) sits under the foot on its own line, hidden until
	   it has something to say — the same presence-gated line the fixed gauges carry
	   in the static HTML. */
	var forecast = document.createElement('div');
	forecast.className = 'gauge-forecast';
	root.appendChild(head);
	root.appendChild(track);
	root.appendChild(foot);
	root.appendChild(forecast);
	parent.appendChild(root);
	return { root: root, pct: pct, fill: fill, reset: reset, forecast: forecast };
}

function renderBurn(burn) {
	var today = burn && burn.today ? burn.today : null;
	setText(ui.statOut, today ? fmtNum(today.outputTokens) : EMDASH);
	setText(ui.statIn, today ? fmtNum(today.inputTokens) : EMDASH);
	setText(ui.statCache, today ? fmtNum(today.cacheReadTokens) : EMDASH);
	setText(ui.statMsg, today ? fmtNum(today.messages) : EMDASH);

	/* Today's dollar spend (v0.12.0). burn.costUSD is a finite number only when
	   Claude Code's OTLP telemetry is flowing; null otherwise. typeof, not
	   Number() — Number(null) is 0, and a $0.00 must never be DERIVED from an
	   absent cost. A real zero the feed reported does render, honestly, as $0.00. */
	var cost = burn && typeof burn.costUSD === 'number' && isFinite(burn.costUSD) ? burn.costUSD : null;
	if (ui.costLine) {
		setText(ui.costLine, cost === null ? '' : '$' + cost.toFixed(2) + ' today');
		ui.costLine.classList.toggle('shown', cost !== null);
	}

	var daily = burn && Array.isArray(burn.daily) ? burn.daily : [];
	/* Not every feed carries burn.daily. Without it the toggle is inert rather
	   than hidden: the "24h" chip stays on screen, greyed, so the tap that does
	   nothing has a visible reason. */
	sparkDailyAvailable = daily.length > 0;
	ui.sparkMode.classList.toggle('disabled', !sparkDailyAvailable);
	ui.sparkWrap.classList.toggle('tappable', sparkDailyAvailable);

	/* sparkMode is what was ASKED for; sevenDay is what can be drawn. Keeping
	   them separate matters because the first render runs before any poll has
	   landed — collapsing the two there would silently throw the mode away
	   before the feed had a chance to say whether it carries a daily series. */
	var sevenDay = sparkMode === '7d' && sparkDailyAvailable;
	var capacity = sevenDay ? SPARK_BUCKETS_7D : SPARK_BUCKETS;
	var hourly = burn && Array.isArray(burn.hourly) ? burn.hourly : [];
	/* Contract: both series are oldest first, 24 and 7 entries. Take the tail so
	   a longer array still shows the most recent window. */
	var tail = (sevenDay ? daily : hourly).slice(-capacity);

	var peak = 0;
	for (var i = 0; i < tail.length; i++) {
		var v = tail[i] && Number(tail[i].outputTokens);
		if (isFinite(v) && v > peak) peak = v;
	}

	/* burn.budget (v0.10.0), presence-gated like every other additive field: the
	   whole marker, the scale rule below and the line under the stats all fall
	   away together when the feed has no budget, and the chart is then byte-for-
	   byte its v0.9.0 self. */
	var budget = burn && burn.budget && typeof burn.budget === 'object' && !Array.isArray(burn.budget)
		? burn.budget : null;
	var perDay = budget && typeof budget.dailyOutputTokens === 'number' &&
		isFinite(budget.dailyOutputTokens) && budget.dailyOutputTokens > 0 ? budget.dailyOutputTokens : null;
	/* The marker is drawn in the UNITS OF THE SERIES under it, which means it says
	   two different things on the two toggle positions and both are honest:
	     - 7 day bars: one bar is one day, so the daily figure is a CEILING. A bar
	       above the line is a day that went over, full stop.
	     - 24 h bars: the daily figure spread evenly across the day, which is a
	       PACE line and NOT a ceiling. An hour above it is not an overspend — it
	       is an hour that a quieter one has to pay for. Reading it as a limit
	       would have the panel condemn every normal working hour.
	   The wording of the tooltip says which of the two is on screen. */
	var target = perDay === null ? null : (sevenDay ? perDay : perDay / HOURS_PER_DAY);
	/* A marker off the top of the chart is a marker that says nothing, so when the
	   target sits above every bar the whole chart scales to the TARGET instead of
	   to the peak. The axis still reports the data peak, which is what it has
	   always reported. With no budget in the feed this is exactly peak. */
	var scaleMax = target !== null && target > peak ? target : peak;

	ensureSparkBars(capacity);
	var offset = capacity - tail.length;
	for (var b = 0; b < capacity; b++) {
		var bar = ui.sparkBars[b];
		var item = b >= offset ? tail[b - offset] : null;
		var val = item ? Number(item.outputTokens) : NaN;
		var h = scaleMax > 0 && isFinite(val) ? Math.round((val / scaleMax) * 100) : 0;
		setVar(bar, '--h', String(h));
		bar.classList.toggle('recent', b === capacity - 1 && tail.length > 0);
	}
	renderSparkTarget(target, scaleMax, sevenDay);
	renderBudgetLine(budget);

	setText(ui.sparkLabel, sevenDay ? '7 day burn' : '24 h burn');
	setText(ui.sparkMode, sevenDay ? '7d' : '24h');
	setText(ui.sparkMax, peak > 0 ? 'peak ' + fmtNum(peak) : EMDASH);
	renderSparkLabels(sevenDay ? tail : null, offset, capacity);
}

/* The bar row is rebuilt only when the bucket COUNT changes — a toggle, not a
   poll — so the 3 s refresh keeps writing into the same elements. */
function ensureSparkBars(count) {
	if (sparkBucketCount === count) return;
	ui.spark.textContent = '';
	ui.sparkBars = [];
	for (var i = 0; i < count; i++) {
		var bar = document.createElement('div');
		bar.className = 'spark-bar';
		ui.spark.appendChild(bar);
		ui.sparkBars.push(bar);
	}
	/* The target marker lives inside this row (it is positioned against the same
	   bottom edge the bars grow from), so clearing the row takes it with it. Put
	   back LAST, so it paints over the bars rather than under them. */
	if (ui.sparkTarget) ui.spark.appendChild(ui.sparkTarget);
	sparkBucketCount = count;
}

/* ------------------------------------------------- burn budget (v0.10.0) */

function renderSparkTarget(target, scaleMax, sevenDay) {
	if (!ui.sparkTarget) return;
	if (target === null || !(scaleMax > 0)) {
		ui.sparkTarget.classList.remove('shown');
		if (ui.sparkTarget.hasAttribute('title')) ui.sparkTarget.removeAttribute('title');
		return;
	}
	setVar(ui.sparkTarget, '--t', String(Math.round((target / scaleMax) * 100)));
	ui.sparkTarget.classList.add('shown');
	var tip = sevenDay
		? 'daily budget ' + fmtNum(Math.round(target))
		: 'budget pace ' + fmtNum(Math.round(target)) + ' per hour';
	if (ui.sparkTarget.getAttribute('title') !== tip) ui.sparkTarget.setAttribute('title', tip);
}

/* "budget 34%", muted, beside the TODAY stats — and "budget 134% (over)" in
   amber from 100%, red from 150%. The words move with the colour at both steps
   deliberately: this panel gets read from across a room and reported from
   photographs, so a state that exists only as a hue is a state nobody reads.

   typeof, not Number(): Number(null) is 0, and a percentage crabd could not
   produce must render NOTHING rather than a 0% that reads as a quiet day (the
   same rule the by-model split and the week strip follow). A budget block
   carrying only dailyOutputTokens therefore draws the marker and no line, which
   is the honest pair. */
function renderBudgetLine(budget) {
	if (!ui.budgetLine) return;
	var raw = budget && typeof budget.todayPct === 'number' && isFinite(budget.todayPct) && budget.todayPct >= 0
		? budget.todayPct : null;
	if (raw === null) {
		setText(ui.budgetLine, '');
		ui.budgetLine.classList.remove('shown');
		ui.budgetLine.classList.remove('over');
		ui.budgetLine.classList.remove('far');
		return;
	}
	var pct = Math.round(raw * 100);
	var far = pct >= BUDGET_RED_PCT;
	var over = !far && pct >= BUDGET_AMBER_PCT;
	setText(ui.budgetLine, 'budget ' + pct + '%' +
		(far ? ' ' + EMDASH + ' far over' : over ? ' ' + EMDASH + ' over' : ''));
	ui.budgetLine.classList.add('shown');
	ui.budgetLine.classList.toggle('over', over);
	ui.budgetLine.classList.toggle('far', far);
}

/* Weekday letters under the 7-day bars. dayStart is a local calendar day
   ("2026-08-20"), so it is split by hand: Date.parse of a bare date is UTC
   midnight, which lands on the previous weekday for anyone west of Greenwich. */
function renderSparkLabels(tail, offset, capacity) {
	if (!tail) {
		ui.sparkLabels.classList.remove('shown');
		if (ui.sparkLabels.textContent !== '') ui.sparkLabels.textContent = '';
		/* The signature has to go with the DOM it describes (v0.20.0, CD-38). This
		   branch threw the label spans away and left sparkLabelSig holding the
		   letters it had just deleted, so the next 7-day pass computed the same
		   signature, decided nothing had changed, and skipped the rebuild: 7d ->
		   24h -> 7d lost the weekday row for the life of the panel. A cleared cache
		   must be cleared on both sides or it is not a cache, it is a lie. */
		ui.sparkLabelSig = null;
		return;
	}
	var letters = [];
	for (var i = 0; i < capacity; i++) {
		var item = i >= offset ? tail[i - offset] : null;
		letters.push(item ? weekdayLetter(item.dayStart) : '');
	}
	var sig = letters.join('');
	if (ui.sparkLabelSig !== sig) {
		ui.sparkLabelSig = sig;
		ui.sparkLabels.textContent = '';
		for (var k = 0; k < letters.length; k++) {
			var span = document.createElement('span');
			span.textContent = letters[k];
			ui.sparkLabels.appendChild(span);
		}
	}
	ui.sparkLabels.classList.add('shown');
}

function weekdayLetter(dayStart) {
	var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(dayStart || ''));
	if (!m) return EMDASH;
	var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
	if (isNaN(d.getTime())) return EMDASH;
	return WEEKDAY_LETTERS[d.getDay()];
}

function onSparkClick() {
	if (!sparkDailyAvailable) return;
	sparkMode = sparkMode === '7d' ? '24h' : '7d';
	renderBurn(lastGoodDoc ? lastGoodDoc.burn : null);
}

/* THE CORE LINE (v0.20.0, CD-33).

   At 3:2 and narrower the stylesheet hides the Limits and Sessions zones
   outright, and the panel becomes a clock with a crab on it: an unacked question
   and a five-hour window at 97% are both simply absent, with nothing on the glass
   admitting either exists. Rule 6 is "hide, never shrink", and this is the half
   of it that was missing — what gets hidden still has to be ADMITTED.

   Two facts, in the vocabulary the zones themselves use. It is rendered on every
   slot and shown by CSS only where those zones are gone, so nothing here has to
   know which slot it is on — the stylesheet owns that, exactly as gridCapacity
   leaves the breakpoints to it. A full layout for these slots is a project; this
   is the honest minimum, done properly.

   Reading rules are the panel's own: an unreadable utilization is an em-dash and
   never a 0%, and a window the feed cannot report at all is omitted rather than
   dashed — a line of two dashes says less than a line with one real figure. */
function renderCoreLine(status, sessions, limits) {
	if (!ui.coreSessions) return;
	if (status === 'connecting') {
		/* The standalone story, in one clause. The full sentence lives in the
		   sessions zone, which is exactly the zone this slot has hidden. */
		setText(ui.coreSessions, 'companion not running');
		setText(ui.coreLimits, '');
		return;
	}
	var waiting = countWaitingUnacked(sessions);
	var working = 0;
	for (var i = 0; i < sessions.length; i++) {
		if (sessions[i] && sessions[i].state === 'working') working++;
	}
	setText(ui.coreSessions, waiting + ' waiting  ·  ' + working + ' working');

	var parts = [];
	var av = !!(limits && limits.available === true);
	var five = corePct(av ? limits.fiveHour : null);
	var week = corePct(av ? limits.weekly : null);
	if (five !== null) parts.push('5h ' + five + '%');
	if (week !== null) parts.push('wk ' + week + '%');
	setText(ui.coreLimits, parts.join('  ·  '));
}

function corePct(win) {
	var util = win && typeof win.utilization === 'number' && isFinite(win.utilization) ? win.utilization : null;
	if (util === null) return null;
	return Math.round(Math.max(0, Math.min(1, util)) * 100);
}

function countWaitingUnacked(list) {
	var n = 0;
	for (var i = 0; i < list.length; i++) {
		if (list[i] && list[i].state === 'needs_input' && !effectiveAcked(list[i])) n++;
	}
	return n;
}

function renderSessions(sessions, status, quiet, recap) {
	var waiting = 0;
	var waitingUnacked = 0;
	for (var i = 0; i < sessions.length; i++) {
		if (!sessions[i] || sessions[i].state !== 'needs_input') continue;
		waiting++;
		/* The recap line counts UNACKED only: on a header that has become a day
		   summary, "waiting" has to mean "still wants you", and an acked row has
		   already been answered by a fingertip. The v3 count below is untouched
		   and still counts every needs_input row. */
		if (!effectiveAcked(sessions[i])) waitingUnacked++;
	}

	/* A dismissed card is GONE, not collapsed — it must not appear in the grid,
	   in the count, or in the "+N" tail. Filtering here, before any of the three
	   are computed, is what makes that one fact rather than three. */
	var shown = [];
	for (var d = 0; d < sessions.length; d++) {
		if (sessions[d] && !isDismissed(sessions[d])) shown.push(sessions[d]);
	}

	/* The session filter (v0.15.0). It narrows the CARD LIST only — `waiting`
	   above it, the alert glow, the crab's mood and the ack-all gesture were all
	   computed from the whole feed and stay that way. `total` is what the count
	   line compares against, so the header can say how much the chip is hiding
	   rather than quietly under-reporting the day. */
	var total = shown.length;
	syncHeaderChips();
	var preFilter = shown;
	shown = filterSessions(shown);
	var filtered = shown.length !== total;
	/* How many UNACKED waiting rows the chip is sitting on (v0.20.0, CD-34). The
	   panel-wide glow now stands regardless of the filter (panelEscalation), and
	   this is the other half of that: a glowing panel whose grid holds no waiting
	   card has to say where the card went, or the operator is looking for an alert
	   the header has quietly filed away. */
	var waitingHidden = 0;
	if (filtered) {
		waitingHidden = countWaitingUnacked(preFilter) - countWaitingUnacked(shown);
		if (waitingHidden < 0) waitingHidden = 0;
	}

	var recapText = recapLine(recap);
	/* Standalone: no companion has ever answered, so there is no day to summarise.
	   Blank, not an em-dash — a dash is the panel reporting a figure it could not
	   read, and here there is nothing to read yet, which the zone's own line says. */
	/* A FILTERED header says what it is hiding, and it outranks the day recap for
	   the length of the filter: the recap is ambient and the cut is not. It is
	   only shown when rows were actually removed — a "Working" chip on a panel
	   where every session is working hides nothing, so the header stays the
	   header it always was. */
	var showRecap = !!recapText && !filtered;
	if (status === 'connecting') { setText(ui.sessionCount, ''); ui.recapSig = null; }
	else if (filtered) {
		setText(ui.sessionCount, 'showing ' + shown.length + ' of ' + total +
			(waitingHidden ? '  ·  ' + waitingHidden + ' waiting hidden' : ''));
		ui.recapSig = null;
	}
	else if (recapText) setRecapHeader(recapText, waitingUnacked);
	else {
		setText(ui.sessionCount, shown.length + (waiting ? '  ·  ' + waiting + ' waiting' : ''));
		ui.recapSig = null;
	}
	ui.sessionCount.classList.toggle('recap', showRecap);

	/* Reset before every early return below (v0.20.0, CD-14): the overflow sheet
	   reads this list, and a stale one would offer rows the grid no longer cuts. */
	overflowList = [];
	document.body.classList.toggle('empty', shown.length === 0);
	/* The STANDALONE line. A store user installs the widget before the companion,
	   so the first thing this panel ever renders is this state — it has to read as
	   a finished display with one part not set up yet, not as a broken one. No URL:
	   the store listing carries the link, and a hardcoded one goes stale on glass
	   that nobody re-imports. */
	setText(ui.gridEmpty, status === 'connecting'
		? feedAbsentNote()
		/* A filter that emptied the grid says SO, and names the mode it emptied it
		   in. "No active Claude sessions" under a Waiting chip with four working
		   sessions behind it would be the panel reporting the filter's answer as
		   the fleet's. Only when the filter is the reason, though: total is the
		   post-dismissal count, so an empty feed still falls through to the two
		   lines below. */
		: (currentFilter().match && total > 0) ? currentFilter().empty
		/* Every card dismissed is not the same fact as no sessions, and saying the
		   second when the first is true would be the panel lying to get tidy. */
		: sessions.length > 0 ? 'All cards dismissed'
		: 'No active Claude sessions');
	if (shown.length === 0) {
		if (cardSig !== '') { cardSig = ''; ui.cards.textContent = ''; }
		return;
	}

	/* Pins reorder WITHIN crabd's bands and nowhere else (sortPinned), and they do
	   it here — before the capacity slice — because the whole point of a pin is
	   that the session survives the "+N more" cut. */
	shown = sortPinned(shown);

	var clamped = clampGrid(shown, gridCapacity());
	var visible = clamped.visible;
	var chipText = clamped.chipText;
	/* THE CUT LIST IS KEPT, NOT RECOMPUTED (v0.20.0, CD-14). The tile is a way
	   into these sessions now, and the sheet behind it must show exactly the rows
	   the clamp removed — dismissals, the filter, the pin order and the slot's
	   own capacity all decided which ones those are. A second pass through the
	   same four rules is a second pass that can disagree with this one, and the
	   disagreement would be invisible: a list that looks plausible and names the
	   wrong sessions. Written on every render, so the sheet follows the feed. */
	overflowList = clamped.rest;

	/* The signature carries only what changes a card's STRUCTURE. Ages —
	   lastActivity, the turn chip, subagent ageSec — are deliberately absent:
	   they move on every poll and would otherwise rebuild all eight cards every
	   3 s. They are relabelled in place below instead. */
	var sig = visible.map(function (s) {
		/* titleSource is card STRUCTURE, not an age: it decides whether the title
		   line is the muted-italic derived rendering, so a session whose title
		   crabd re-derives (or stops deriving) has to rebuild the card. */
		/* repoLine(), not `s.repo, s.branch`: the line FALLS BACK to cwd, so signing
		   the two fields left a repo-less session's path stale (audit F1). Signing
		   the rendered value covers every branch of that fallback at once. */
		return [s.id, s.state, s.title, s.titleSource || '', repoLine(s), s.model, s.speed,
			(s.subagents && s.subagents.running) || 0, s.lastEvent,
			s.question || '', s.turnStartedAt ? '1' : '0', effectiveAcked(s) ? '1' : '0',
			/* The pin glyph is card STRUCTURE for the same reason the ctx chip is.
			   Reordering usually moves this signature by itself, but pinning the
			   ONLY card in its band changes no order at all — and without this the
			   glyph would not appear until something else rebuilt the card. */
			isPinned(s.id) ? 'p' : '',
			/* The long-press confirm is card STRUCTURE too, and in BOTH directions:
			   an unpin draws a glyph the pin map says is not there, so without this
			   the flash would never clear and the card would carry a pin marker for
			   a session that has none until something else rebuilt it. */
			pinFlashFor(s.id),
			/* The ctx chip is card STRUCTURE, not an age: it appears and disappears
			   with the field, so it has to move the signature or the badge row goes
			   stale for the life of the session. */
			typeof s.contextTokens === 'number' ? String(s.contextTokens) : '',
			/* The ctx-fill DENOMINATOR is card STRUCTURE too (v0.25.0, and the audit
			   F1 precedent: sign the value the render uses, not one of its inputs).
			   The bar appears the moment crabd learns the window and disappears the
			   moment it stops serving it — a catalog fetch failing after a token
			   expiry — and neither event moves any other field on this row. Without
			   this the hairline would be drawn against the previous denominator for
			   the life of the card. */
			typeof s.contextWindowTokens === 'number' ? String(s.contextWindowTokens) : '',
			(s.pendingPermission && typeof s.pendingPermission === 'object' ? String(s.pendingPermission.tool) + '|' + String(s.pendingPermission.summary) : ''),
			/* The queued chip is card STRUCTURE (v0.15.0): it appears when a tap
			   queues a prompt and DISAPPEARS when crabd expires it or the Stop hook
			   consumes it, and the second half is the half that matters — without this
			   the card would go on advertising a prompt that has already been
			   delivered until something else rebuilt it. The LABEL, not the raw
			   prompt: two prompts that render the same chip are the same card. */
			queuedLabel(s) || '',
			laneNSig(s),   // lane N
			subList(s).map(function (d) { return String(d && d.label); }).join(',')].join('');
	}).join('') + '||' + (chipText || '') + '||' + (quiet ? 'q' : '');

	/* A REBUILD IS DEFERRED WHILE A FINGER IS ON A CARD (v0.14.0). The grid rebuilds
	   by throwing every card away, and a poll landing mid-swipe would take the card
	   out from under the fingertip dragging it — 3 s is well inside one gesture.
	   cardSig is deliberately NOT advanced, so the difference is still there for the
	   render that endSwipe fires when the finger lifts, and the age relabelling
	   below is skipped with it: `visible` and the surviving DOM can disagree about
	   which row is at which index, and writing one's timestamps onto the other is
	   how a card ends up aging from another session's clock. */
	if (sig !== cardSig) {
		if (gestureHoldsCards()) return;
		cardSig = sig;
		ui.cards.textContent = '';
		for (var c = 0; c < visible.length; c++) ui.cards.appendChild(buildCard(visible[c], quiet));
		if (chipText) {
			/* A REAL CONTROL since v0.20.0 (CD-14). It was an inert card: it named a
			   number of sessions and offered no way to reach any of them, so at the XL
			   slot seven sessions and at the small slot eleven were announced and then
			   unreachable — the panel telling you what it was not going to show you.
			   Same idiom as every other tap target here: .tappable for the fingertip
			   floor, role/tabindex for the keyboard, and the routing lives in
			   onCardsClick beside the card branch it sits next to. */
			var chip = document.createElement('div');
			chip.className = 'card chip tappable';
			chip.setAttribute('data-overflow', '1');
			chip.setAttribute('role', 'button');
			chip.setAttribute('tabindex', '0');
			chip.setAttribute('aria-label', chipText + ' ' + EMDASH + ' open the sessions not on the grid');
			chip.textContent = chipText;
			ui.cards.appendChild(chip);
		}
	}

	/* Ages move without the signature changing, so refresh the anchors every
	   render and let the 1 Hz tick relabel them. */
	for (var k = 0; k < visible.length; k++) {
		var node = ui.cards.children[k];
		if (!node) break;
		var since = Date.parse(visible[k].lastActivityAt);
		node.setAttribute('data-since', isFinite(since) ? String(since) : '');
		/* Escalation ages from stateSince, NOT lastActivityAt: a session that is
		   waiting on a human has no activity to age from, and the question is as
		   old as the state, not as old as the last file it wrote. */
		var stateSince = Date.parse(visible[k].stateSince);
		node.setAttribute('data-state-since', isFinite(stateSince) ? String(stateSince) : '');
		var turn = visible[k].turnStartedAt ? Date.parse(visible[k].turnStartedAt) : NaN;
		node.setAttribute('data-turn', isFinite(turn) ? String(turn) : '');
		/* The approval hold's anchor (v0.15.0), refreshed every render for the same
		   reason the three above it are: the countdown is an age, so it must not be
		   in the card signature, and the element it fills is only built on an
		   approval card. A pendingPermission with no readable requestedAt leaves
		   this empty and the countdown renders nothing at all — see
		   approvalRemaining(): unknown is not expired. */
		var req = visible[k].pendingPermission && typeof visible[k].pendingPermission === 'object'
			? Date.parse(visible[k].pendingPermission.requestedAt) : NaN;
		node.setAttribute('data-approval-at', isFinite(req) ? String(req) : '');
		refreshSubAges(node, visible[k]);
	}
	tickAges(Date.now());
}

/* THE CLAMP, and the ONE thing it guarantees (v0.26.0, AUD-F5).

   The "+N more" tile is only acceptable while a WAITING card can never be the row
   it swallows — a panel that hides the question it exists to surface is worse than
   a panel with no grid at all. Measured at HEAD before this function existed: the
   widget held that invariant only by INHERITING it. sortPinned reads its bands out
   of the order crabd delivered (first appearance of each state) and is deliberately
   the identity with nothing pinned, the filter and the dismissals both preserve
   order, and the clamp was a bare slice — so every part of the widget was correct
   and the guarantee itself lived in another process. Feed the same code a document
   whose sessions are not pre-sorted (12 done rows, then one needs_input, capacity 8)
   and the waiting card lands in the "+N more" tail: proven by test, not argued.

   So the clamp keeps it now: waiting rows survive first, everything else fills what
   is left, and the ORDER is untouched in both lists. That is not a second copy of
   crabd's band list — it names ONE state, the one the panel is for — and it is the
   discipline recapLine already keeps two screens down ("crabd sorts commits count
   desc, but the max is taken here rather than trusting position").

   On any contract-conforming feed this is byte-for-byte the old slice, which is
   the point: 65 fingerprint captures, zero layout differences.

   With more waiting rows than cells, waiting rows ARE cut — there is no cell to put
   them in — and CD-14's tile is the route to them. What cannot happen is a waiting
   row cut while a done or idle row keeps a cell. */
function clampGrid(list, capacity) {
	var out = { visible: list, rest: [], chipText: null };
	if (!(capacity >= 1) || list.length <= capacity) return out;
	var keep = capacity - 1;   /* the last cell belongs to the "+N more" tile */
	var take = {};
	var n = 0, i;
	for (i = 0; i < list.length && n < keep; i++) {
		if (list[i] && list[i].state === 'needs_input') { take[i] = 1; n++; }
	}
	for (i = 0; i < list.length && n < keep; i++) {
		if (!take[i]) { take[i] = 1; n++; }
	}
	var visible = [], rest = [];
	for (i = 0; i < list.length; i++) (take[i] ? visible : rest).push(list[i]);
	var idleOnly = rest.every(function (s) { return s && (s.state === 'idle' || s.state === 'done'); });
	out.visible = visible;
	out.rest = rest;
	out.chipText = '+' + rest.length + (idleOnly ? ' idle' : ' more');
	return out;
}

/* How many cards the grid can actually hold, READ OFF the grid rather than
   hard-coded. .cards is two fixed rows at every slot but drops from 4 columns to
   3 then 2 on the narrower dashboard_lcd sizes, and a capacity constant that
   stayed at 8 put four rows of cards into a two-row grid: the extra rows became
   implicit tracks that overflowed the zone and sliced the bottom cards in half
   (measured at 840x344, 2026-08-26). Deriving it from the computed style means
   the breakpoints live in the stylesheet only and cannot drift apart. */
function gridCapacity() {
	var cols = trackCount('grid-template-columns', GRID_COLS_DEFAULT);
	/* v0.15.0: the ROW count is read the same way, for the same reason. Compact
	   density is a third row and nothing else in JS knows that — the stylesheet
	   owns both axes and this function owns neither. A constant here would have
	   been the 840x344 bug again, one axis over. */
	var rows = trackCount('grid-template-rows', GRID_ROWS);
	return cols * rows;
}

/* One computed grid axis as a track COUNT. "none" on a display:none grid parses
   as a single track, which is why the unit test is on the string and not on the
   count: a hidden grid keeps the caller's default rather than collapsing the
   whole panel to one card. */
function trackCount(prop, dflt) {
	try {
		var tracks = window.getComputedStyle(ui.cards).getPropertyValue(prop);
		var n = String(tracks || '').trim().split(/\s+/).length;
		if (n >= 1 && n <= 12 && /px|fr|%/.test(tracks)) return n;
	} catch (e) { /* fall through to the default */ }
	return dflt;
}

/* The v4 recap replaces the bare session count in the grid header: on a panel
   that already shows every live session as a card, the day's shape is the thing
   the header can add. Returns null when the feed carries no recap block, which is
   what keeps the bare-count header rendering unchanged.

   Only the TOP repo goes in the line — the header is one line on a 1420 px zone
   and the full commits list is in the burn sheet. crabd sorts commits count desc,
   but the max is taken here rather than trusting position: a mis-sorted feed
   would otherwise silently name the wrong repo. */
function recapLine(recap) {
	if (!recap || typeof recap !== 'object') return null;
	var parts = [];
	if (typeof recap.sessionsToday === 'number') parts.push(recap.sessionsToday + ' today');
	if (typeof recap.doneToday === 'number') parts.push(recap.doneToday + ' done');
	var top = topRepo(recap);
	if (top) parts.push(top.count + ' commits@' + top.repo);
	return parts.length ? parts.join('  ·  ') : null;
}

/* The recap replaces the count, but it must never swallow the waiting figure —
   a day summary that hides "someone is still waiting" is the one thing this
   header cannot do. So the fragment is APPENDED, and it is a separate element
   because it carries the amber every other waiting cue on the panel uses; the
   recap around it stays faint. Zero unacked and the fragment is absent entirely,
   rather than rendering "0 waiting" as reassurance nobody asked for.

   Two nodes, rebuilt only when the text or the count moves — this runs on every
   poll for the life of the panel. */
function setRecapHeader(text, waitingUnacked) {
	var sig = text + '#' + waitingUnacked;
	if (ui.recapSig === sig) return;
	ui.recapSig = sig;
	ui.sessionCount.textContent = waitingUnacked > 0 ? text + '  ·  ' : text;
	if (waitingUnacked > 0) {
		var span = document.createElement('span');
		span.className = 'session-waiting';
		span.textContent = waitingUnacked + ' waiting';
		ui.sessionCount.appendChild(span);
	}
}

function topRepo(recap) {
	var commits = recap && Array.isArray(recap.commits) ? recap.commits : [];
	var best = null;
	for (var i = 0; i < commits.length; i++) {
		var c = commits[i];
		if (!c || !c.repo || typeof c.count !== 'number' || !isFinite(c.count)) continue;
		if (!best || c.count > best.count) best = { repo: String(c.repo), count: c.count };
	}
	return best;
}

/* subagentDetail is optional and crabd caps it at 5 — defend against both a
   missing field and a longer array so the "+N more" row stays truthful. */
function subList(s) {
	return s && Array.isArray(s.subagentDetail) ? s.subagentDetail : [];
}

/* What to put on a session's title line, and whether it was DERIVED rather than
   written (v0.11.0). Two sources of derived, one answer:
   - crabd says so. `titleSource: "cwd"` is its own fallback — it found no custom
     title, no AI title and no first prompt, so it named the session after the
     folder. Optional and additive: an older crabd omits it and the title renders
     exactly as it did before, which is the whole presence-detection contract.
   - the row has no title at all, which is an older crabd or a session it could
     not read one for. The REPO is the best thing left: it names the work, where
     "session" names nothing. Only when there is no repo either does a literal
     appear, and it says what it means — untitled, not "session".
   Nothing here invents a field: `titleSource` is read for the one value that
   means fallback and ignored for every other, so a crabd that adds a third
   source renders as a real title until this widget is next imported. */
function titleParts(s) {
	var t = s && s.title !== null && s.title !== undefined ? String(s.title).trim() : '';
	if (t) return { text: t, derived: String((s && s.titleSource) || '') === 'cwd' };
	var repo = s && s.repo ? String(s.repo).trim() : '';
	return { text: repo || 'untitled session', derived: true };
}

/* The repo line, in ONE place (v0.16.0, audit F1). The line falls back to `cwd`
   when there is no repo, which makes `cwd` a VISIBLE value — and the card
   signature only carried `repo`/`branch`, so a repo-less session whose cwd moved
   kept the old path until something else rebuilt the card. The signature now
   carries this function's result, so what is signed is what is drawn; keeping the
   card, the sheet and the signature on one expression is what stops them drifting
   apart again. */
function repoLine(s) {
	if (!s) return '';
	if (s.repo) return String(s.repo) + (s.branch ? '@' + String(s.branch) : '');
	return s.cwd ? String(s.cwd) : '';
}

/* The queued continue (v0.14.0 contract field, v0.15.0 on the card). Returns the
   SHORT label for a queued prompt, or null when nothing is queued.

   Presence is the whole test, deliberately. crabd re-derives freshness from
   `queuedAt` before it serves the field ("a card never advertises a prompt the
   Stop hook would no longer deliver" — STATE-CONTRACT v0.14.0), and a second
   expiry clock in the widget would be a copy of a rule that can disagree with
   it: tighter and it hides a queue that is genuinely live, looser and it shows
   one that is gone. `queuedAt` is read only as a shape check, never as a
   deadline.

   The label is the button face the prompt came from, so the card reads back what
   the finger tapped rather than the sentence that went on the wire. A prompt
   from the feed's own continuePrompts is its own label (that is how the buttons
   are built); anything else — an older widget's wording, a prompt queued by
   something that is not this panel — is trimmed. */
function queuedLabel(s) {
	var q = s && s.queuedContinue;
	if (!q || typeof q !== 'object' || Array.isArray(q)) return null;
	var prompt = typeof q.prompt === 'string' ? q.prompt.trim() : '';
	if (!prompt) return null;
	for (var i = 0; i < CONTINUE_DEFAULTS.length; i++) {
		if (CONTINUE_DEFAULTS[i].prompt === prompt) return CONTINUE_DEFAULTS[i].label;
	}
	if (prompt.length <= QUEUED_LABEL_MAX) return prompt;
	return prompt.slice(0, QUEUED_LABEL_MAX - 1).replace(/\s+$/, '') + '…';
}

/* ------------------------------------------- the context hairline (v0.22.0) */

/* The context window this session is filling, in tokens, or null.

   TWO SOURCES, SERVED ONE FIRST — and neither is a number this file made up.

   1. `contextWindowTokens` (crabd 0.28.0), found by PRESENCE like every other
      additive field. crabd resolves it from the status line document, the model
      marker, or the Models API's `max_input_tokens`, in that order, and serves null
      when none of the three knows. This is the branch the comment here used to
      promise: before it, the marker was the only source, and live model ids carry no
      marker (`claude-fable-5`, `claude-opus-5`), so NO bar ever drew on a real
      session.
   2. the `[1m]` / `[200k]` marker in the model id, for a crabd older than 0.28.0.
      Exactly the fallback it always was, so an un-upgraded companion keeps the bars
      it already drew — this widget is not redeployable on demand (see header).

   The order cannot be flipped. crabd already ranks the marker ABOVE its catalog, so
   a served number has either honoured the marker or come from something MORE
   specific than it; reading the marker first would discard the status line's own
   reading of the live session.

   There is still deliberately NO model-name table, on either side of the wire.
   "opus means 200k" would be a number no document ever said, and it is the kind of
   invention that goes wrong silently: the day a window changes, every card would
   report a fill against last year's denominator and nothing anywhere would say so.
   Unknown therefore stays null and draws no bar at all — the honest rendering of
   "this panel cannot tell you how full that is". */
function ctxWindowTokens(s) {
	var served = s && s.contextWindowTokens;
	if (typeof served === 'number' && isFinite(served) && served > 0) return served;
	var model = s && s.model;
	var m = MODEL_CTX_RE.exec(String(model === null || model === undefined ? '' : model));
	if (!m) return null;
	var n = Number(m[1]);
	if (!isFinite(n) || n <= 0) return null;
	var tokens = n * (m[2] === 'm' || m[2] === 'M' ? 1e6 : 1e3);
	return isFinite(tokens) && tokens > 0 ? tokens : null;
}

/* How full, as a whole percent, or null for every shape that is not an answer.
   typeof, not Number(): `contextTokens` is null until a usage record exists, and
   Number(null) is 0 — a bar pinned at empty on a session nobody has measured reads
   as a session with all its room left, which is the opposite of what is known. */
function ctxFillPct(s) {
	var used = s && s.contextTokens;
	if (typeof used !== 'number' || !isFinite(used) || used < 0) return null;
	var win = ctxWindowTokens(s);
	if (win === null) return null;
	return Math.round(Math.max(0, Math.min(1, used / win)) * 100);
}

/* The two STEPS are the gauges' own constants, so the card and the gauges can never
   disagree about where hot starts. The BASE is not: --faint rather than the gauges'
   blue, because blue is the usage gauges' identity on this panel and a blue rule
   under every card would read as a fourth gauge instead of as an annotation on the
   card above it. Same choice, same reason, as the ctx badge two lines up. */
function ctxColor(pct) {
	if (pct >= GAUGE_RED_PCT) return 'var(--red)';
	if (pct >= GAUGE_AMBER_PCT) return 'var(--amber)';
	return 'var(--faint)';
}

/* The approval hold, in seconds remaining, or null when there is nothing to
   count. null is NOT zero: a pendingPermission whose requestedAt is missing or
   unparseable is a hold of unknown age, and rendering that as "expired" would
   send the operator to a terminal that is still waiting on the panel. */
function approvalRemaining(requestedMs, nowMs) {
	if (!isFinite(requestedMs) || requestedMs <= 0) return null;
	var left = APPROVAL_HOLD_SEC - (nowMs - requestedMs) / 1000;
	if (!isFinite(left)) return null;
	return left > 0 ? left : 0;
}

/* The words for that number. Sub-minute throughout, so this is deliberately not
   fmtDur: a hold measured in seconds should be counted in seconds. */
function approvalText(left) {
	if (left === null) return '';
	if (left <= 0) return 'expired ' + EMDASH + ' decide in terminal';
	return Math.ceil(left) + 's to decide';
}

function buildCard(s, quiet) {
	var card = document.createElement('article');
	card.className = 'card';
	card.setAttribute('data-state', s.state || 'idle');
	card.setAttribute('data-session-id', String(s.id || ''));

	/* v0.3.0: every card opens a sheet. A needs_input card gets the action sheet
	   it has always had; everything else gets the read-only detail variant. */
	card.classList.add('tappable');
	/* A tab stop, and no role (v0.20.0, CD-15) — see onKeyDown for why the card is
	   the one control here that must not be flattened to a label. */
	card.setAttribute('tabindex', '0');
	/* v0.14.0: the dismissable states are the swipeable ones, and the class exists
	   for the STYLESHEET rather than for the handler — startSwipe re-checks the
	   live row anyway. What it buys is touch-action: pan-y on exactly those cards,
	   which is how the horizontal axis is claimed from the compositor without a
	   non-passive listener calling preventDefault on every move. */
	if (DISMISSABLE[s.state]) card.classList.add('swipeable');

	var acked = effectiveAcked(s);
	card.setAttribute('data-acked', acked ? '1' : '0');
	/* Approval (v0.12.0). A needs_input session carrying a live pendingPermission
	   renders the APPROVAL variant — the tool + summary in place of the question —
	   and is the loudest card on the panel. It cannot be acked away (onCrabTap
	   skips it and the approval sheet offers no ack), so a permission gate keeps
	   asking until a decision is made. Presence-detected: absent, or not an
	   object, and the card is an ordinary needs_input row. */
	var pend = s.state === 'needs_input' && s.pendingPermission &&
		typeof s.pendingPermission === 'object' && !Array.isArray(s.pendingPermission)
		? s.pendingPermission : null;
	card.setAttribute('data-approval', pend ? '1' : '');
	if (pend) card.classList.add('approval');
	if (s.state === 'needs_input') {
		if (acked) card.classList.add('acked');
		/* No pulse for an acked card, and none during quiet hours: the card stays
		   put, it just stops asking the room for attention. An approval card is
		   never acked, so it pulses whenever it is not quiet. */
		if ((!acked || pend) && !quiet) card.classList.add('pulse');
	}

	var top = document.createElement('div');
	top.className = 'card-top';
	var dot = document.createElement('span');
	dot.className = 'dot';
	var state = document.createElement('span');
	state.className = 'card-state';
	/* lane N: data-state on the card is untouched, so COMPACTING keeps the working
	   colour, the pulse rules and the escalation ladder exactly as they were. The
	   attribute is what the 1 Hz tick reads, so the hung hint and this word cannot
	   disagree; it is written on the card because the tick walks cards. */
	var laneNCompact = laneNCompacting(s);
	card.setAttribute('data-compacting', laneNCompact ? '1' : '');
	state.textContent = laneNCompact ? 'compacting'
		: s.state === 'needs_input' ? 'needs input' : (s.state || 'idle');
	top.appendChild(dot);
	top.appendChild(state);
	/* Only a working card gets the turn chip. A needs_input session still carries
	   turnStartedAt (the contract clears it on Stop, not on Notification) and a
	   card reading "working 14m" while it waits on a human would be a lie. */
	if (s.state === 'working' && s.turnStartedAt) {
		var turnEl = document.createElement('span');
		turnEl.className = 'card-turn';
		turnEl.textContent = 'working ' + EMDASH;
		top.appendChild(turnEl);
	}
	/* The hung-vs-thinking hint (v0.11.0), beside the turn chip. Built EMPTY for
	   every working card and filled by the 1 Hz tick, because whether a session
	   has gone quiet is an age — it moves without the card's signature moving, the
	   same way the age figure and the escalation badge do. A card that is not
	   working never gets the element at all, so nothing else on the panel can grow
	   a hint it has no rule for. */
	if (s.state === 'working') {
		var hint = document.createElement('span');
		hint.className = 'card-hint';
		top.appendChild(hint);
	}
	/* The pin marker (v0.8.0). A SHAPE — a drawn pushpin head and needle — plus a
	   title, never a colour on its own: the whole panel is read as a photograph of
	   the glass often enough that a cue which survives only in colour is not a cue.
	   It sits in the header rather than in the badges row because it says something
	   about where the CARD is, not about what the session is running, and the
	   badges row is the first thing dropped when a card gets tight. */
	/* The long-press confirm (v0.14.0). A pin animates the glyph IN, which is the
	   whole message. An UNPIN has no glyph left to say anything with, so the card
	   keeps drawing one for the length of the flash and animates it OUT — the only
	   moment on the panel where a pin marker appears on an unpinned session, and it
	   lasts PIN_FLASH_MS. Under reduced motion the animations are dropped and the
	   glyph's presence or absence is the confirm by itself. */
	var pinFlash = pinFlashFor(s.id);
	if (isPinned(s.id) || pinFlash === 'off') {
		var pin = document.createElement('span');
		pin.className = 'card-pin' + (pinFlash ? ' pin-confirm pin-confirm-' + pinFlash : '');
		pin.setAttribute('title', 'pinned');
		pin.setAttribute('aria-label', 'pinned');
		top.appendChild(pin);
	}

	var age = document.createElement('span');
	age.className = 'card-age';
	age.textContent = EMDASH;
	top.appendChild(age);

	var title = document.createElement('h3');
	var tp = titleParts(s);
	title.className = 'card-title' + (tp.derived ? ' title-derived' : '');
	title.textContent = tp.text;
	/* The tooltip is the FULL string, which is the point of it — the line above is
	   clamped with an ellipsis and a fingertip-and-hold is the only way to read
	   the rest of a long one. */
	title.setAttribute('title', tp.text);

	var repo = document.createElement('div');
	repo.className = 'card-repo';
	repo.textContent = repoLine(s);

	/* The question is the enriched full text of the same notification lastEvent
	   summarises, so it REPLACES the event line rather than stacking on top of
	   it — two renderings of one sentence would just cost the card four lines. */
	var question = s.state === 'needs_input' && s.question ? String(s.question) : null;

	var bottom = document.createElement('div');
	bottom.className = 'card-bottom';
	var event = document.createElement('div');
	if (pend) {
		/* The approval body replaces the question: the TOOL is what a grant is
		   about, so it is the prominent line, with a small label above and the
		   summary clamped below. */
		event.className = 'card-approval';
		var apLabel = document.createElement('div');
		apLabel.className = 'card-approval-label';
		apLabel.textContent = 'permission request';
		var apTool = document.createElement('div');
		apTool.className = 'card-approval-tool';
		apTool.textContent = pend.tool ? String(pend.tool) : 'a tool';
		event.appendChild(apLabel);
		event.appendChild(apTool);
		if (pend.summary) {
			var apSum = document.createElement('div');
			apSum.className = 'card-approval-summary';
			apSum.textContent = String(pend.summary);
			event.appendChild(apSum);
		}
		/* The countdown (v0.15.0). Built EMPTY and filled by the 1 Hz tick, the
		   same discipline the age figure and the hung hint keep: it moves without
		   the card's signature moving, so a card rebuilt for it every second would
		   be the grid thrown away sixty times a minute. What it answers is the one
		   question an approval card could not: crabd holds the hook ~55 s, and past
		   that a tap on this card reaches nothing — the terminal dialog is the
		   decision surface again. */
		var apLeft = document.createElement('div');
		apLeft.className = 'card-approval-left';
		event.appendChild(apLeft);
	} else if (question) {
		event.className = 'card-question';
		event.textContent = question;
	} else {
		/* lane N: the activity REPLACES the event line on a working session, the
		   same trade the question makes two branches up - two renderings of what the
		   session is doing would cost the card a line and say it twice. */
		var laneNRow = laneNCardActivity(s);
		if (laneNRow) event = laneNRow;
		else {
			event.className = 'card-event';
			event.textContent = s.lastEvent || '';
		}
	}
	var badges = document.createElement('div');
	badges.className = 'card-badges';

	var model = shortModel(s.model);
	if (model) badges.appendChild(makeBadge(model, 'badge-model'));
	/* lane N: beside the model chip, because a mode is a property of how that model
	   is being allowed to run. */
	var laneNMode = laneNModeLabel(s);
	if (laneNMode) badges.appendChild(makeBadge(laneNMode, 'badge-mode'));
	/* contextTokens is optional, and null until a usage record exists, so the chip
	   is ABSENT rather than showing a zero or an em-dash — an unknown context
	   window is not a small one. It rides next to the model because it is a
	   property of that model's last request.
	   Dropped outright on a question card: those already carry four lines of
	   question and the badges row is what wraps first, which would cost the card a
	   whole line. The chip is the first thing to go by design (v0.6.0), and the
	   small-slot media query drops it again on height. */
	if (typeof s.contextTokens === 'number' && isFinite(s.contextTokens) && !question && !pend) {
		badges.appendChild(makeBadge('ctx ' + fmtNum(s.contextTokens), 'badge-ctx'));
	}
	if (s.speed === 'fast') badges.appendChild(makeBadge('FAST', 'badge-fast'));
	var running = s.subagents && Number(s.subagents.running);
	if (isFinite(running) && running > 0) badges.appendChild(makeBadge(running + ' sub', 'badge-sub'));
	if (acked) badges.appendChild(makeBadge('ACKED', 'badge-ack'));
	/* lane N: the hairline is the glance and this is the figure behind it. In the
	   badges row rather than on the bar, because the bar is a 3 px absolute rule
	   with no room for text and the badges row is where this card's figures live. */
	var laneNTodo = laneNTodos(s);
	if (laneNTodo) badges.appendChild(makeBadge(laneNTodo.done + '/' + laneNTodo.total, 'badge-todo'));

	bottom.appendChild(event);
	/* Under the body, above the badges: the rows explain the "N sub" badge that
	   sits directly beneath them. */
	var subs = buildSubRows(subList(s), (question || pend) ? SUB_ROWS_MAX_Q : SUB_ROWS_MAX);
	if (subs) bottom.appendChild(subs);
	/* The queued chip (v0.15.0), between the body and the badges. It is card
	   STRUCTURE, not an age — it appears and disappears with the field — so it is
	   in the signature and it is built here rather than filled by the tick.
	   Its own row rather than a badge: the badges row is the first thing that
	   wraps when a card gets tight, and a queued next step is the one thing on
	   this card that says what will happen when the session stops. Before this,
	   a tap on Continue was invisible the moment the sheet closed. */
	var qLabel = queuedLabel(s);
	/* lane N: Claude Code's own typed-ahead count rides in this row when there is
	   one and takes the row itself when there is not, so the field is never
	   swallowed by the absence of an unrelated one. The two are worded apart. */
	var laneNQueued = laneNQueueNote(s);
	if (qLabel) {
		var queued = document.createElement('div');
		queued.className = 'card-queued';
		var qtext = document.createElement('span');
		qtext.className = 'card-queued-text';
		qtext.textContent = 'queued: ' + qLabel;
		qtext.setAttribute('title', 'queued: ' + qLabel);
		queued.appendChild(qtext);
		if (laneNQueued) queued.appendChild(laneNQueued);
		bottom.appendChild(queued);
	} else if (laneNQueued && !question && !pend) {
		/* lane N: A ROW OF ITS OWN COSTS A LINE, so it follows the ctx chip's rule
		   (v0.6.0) and is dropped on the two cards that have no line to give. The
		   approval card is the tightest on the panel - label, tool, summary,
		   countdown, subagent rows and badges - and measured at 2560x720 it was
		   already at its cell: adding this row pushed the badges row 15.4 px out of
		   the card and overflow:hidden cut the model chip in half. Riding INSIDE an
		   existing queued row costs nothing, so that case is not gated; and the
		   Detail page carries the count on every card. */
		var typedRow = document.createElement('div');
		typedRow.className = 'card-queued card-queued-typed';
		typedRow.appendChild(laneNQueued);
		bottom.appendChild(typedRow);
	}
	bottom.appendChild(badges);

	card.appendChild(top);
	card.appendChild(title);
	card.appendChild(repo);
	card.appendChild(bottom);

	/* The context hairline (v0.22.0). Appended LAST and absolutely positioned, so it
	   takes no part in the flex column above it and cannot cost a card a line or
	   push a badge out of its cell — the shrink discipline this whole function is
	   built around stays exactly as it was.
	   No element at all when the fill is not derivable, rather than an empty track:
	   a rule drawn along the bottom of a card with nothing in it would be the panel
	   showing a gauge for a quantity it cannot measure. All three inputs
	   (contextTokens, contextWindowTokens and model) are in the card signature, so
	   the bar appears, moves and disappears with the rebuild that any change to one
	   of them already causes. */
	var ctxPct = ctxFillPct(s);
	if (ctxPct !== null) {
		var ctx = document.createElement('div');
		ctx.className = 'card-ctx';
		setVar(ctx, '--w', String(ctxPct));
		setVar(ctx, '--ctx-color', ctxColor(ctxPct));
		/* The figure and its denominator both ride on title/aria — the bar is a
		   glance, and the numbers behind it should be recoverable without one. */
		var tip = 'context ' + ctxPct + '% of ' + fmtNum(ctxWindowTokens(s));
		ctx.setAttribute('title', tip);
		ctx.setAttribute('aria-label', tip);
		card.appendChild(ctx);
	}
	/* lane N: absolute like the ctx hairline above it, for the same reason - it must
	   cost the card's flex column nothing. Stacked one bar-height clear of it. */
	var laneNBar = laneNTodoBar(s);
	if (laneNBar) card.appendChild(laneNBar);
	return card;
}

/* The drill-down under the card body. The count badge stays — this is the
   detail behind that number, not a replacement for it. */
function buildSubRows(list, cap) {
	if (!list.length) return null;
	var wrap = document.createElement('div');
	wrap.className = 'card-subs';
	var shown = Math.min(cap, list.length);
	for (var i = 0; i < shown; i++) {
		var row = document.createElement('div');
		row.className = 'sub-row';
		var label = document.createElement('span');
		label.className = 'sub-label';
		label.textContent = (list[i] && list[i].label) ? String(list[i].label) : 'subagent';
		var age = document.createElement('span');
		age.className = 'sub-age';
		age.textContent = EMDASH;
		row.appendChild(label);
		row.appendChild(age);
		wrap.appendChild(row);
	}
	if (list.length > shown) {
		var more = document.createElement('div');
		more.className = 'sub-row sub-more';
		var moreLabel = document.createElement('span');
		moreLabel.className = 'sub-label';
		moreLabel.textContent = '+' + (list.length - shown) + ' more';
		more.appendChild(moreLabel);
		wrap.appendChild(more);
	}
	return wrap;
}

/* ageSec is a snapshot taken at generatedAt, so these relabel on each poll
   rather than on the 1 Hz tick — showing a second-by-second count off a 3 s
   sample would be inventing precision the feed does not have. */
function refreshSubAges(node, s) {
	var rows = node.querySelectorAll('.sub-age');
	var list = subList(s);
	for (var i = 0; i < rows.length && i < list.length; i++) {
		var secs = list[i] ? Number(list[i].ageSec) : NaN;
		setText(rows[i], isFinite(secs) ? fmtDur(secs) : EMDASH);
	}
}

function makeBadge(text, cls) {
	var b = document.createElement('span');
	b.className = 'badge ' + cls;
	b.textContent = text;
	return b;
}

function tickAges(nowMs) {
	var nodes = ui.cards.children;
	/* Read ONCE per tick, not per card: matchMedia is a live query and the quiet
	   class is a DOM read, and this loop runs every second for the life of the
	   panel. Both mean the same thing here — the dot holds still. */
	var still = document.body.classList.contains('quiet') || reducedMotion();
	/* The writing tick's two frames. Derived from the clock rather than from a
	   toggle we keep, so every card is on the same frame and a card rebuilt
	   mid-second joins the phase instead of starting its own. */
	var frame = Math.floor(nowMs / 1000) % 2 === 1;
	for (var i = 0; i < nodes.length; i++) {
		var node = nodes[i];
		var since = Number(node.getAttribute('data-since'));
		var age = isFinite(since) && since > 0 ? nowMs - since : NaN;
		var label = node.querySelector('.card-age');
		if (label) setText(label, isFinite(age) ? fmtDur(age / 1000) : EMDASH);

		/* Hung vs thinking (v0.11.0). The element exists on working cards only, so
		   its presence IS the state test — no second read of the card's state.
		   Past 90 s the card says "quiet Nm" in words and the dot goes steady; under
		   it the dot ticks. A card whose lastActivityAt did not parse gets neither:
		   an unknown age is not a fresh one, and it is not a hang either. */
		var hint = node.querySelector('.card-hint');
		/* lane N: never over a COMPACTING card. The hint's whole job is to tell a
		   session that is thinking from one that has hung, and a session compacting
		   its context is neither - it is busy, it touches nothing while it works,
		   and the state chip beside this already says so. A card reading
		   "COMPACTING 8m quiet 3m" was the panel raising a hang hint against its own
		   evidence. It is also 61.5 px of card-top the narrow slot does not have:
		   COMPACTING is three glyphs longer than WORKING, and measured at 840x696
		   the hint went from 25.0 px past the card to 61.5. */
		var compacting = node.getAttribute('data-compacting') === '1';
		var hung = !!hint && !compacting && isFinite(age) && age >= HUNG_MS;
		if (hint) {
			setText(hint, hung ? 'quiet ' + fmtDur(age / 1000) : '');
			/* The class is what hides the right-hand age figure, which is the SAME
			   number this hint just wrote in words. */
			node.classList.toggle('hung', hung);
			var writing = isFinite(age) && !hung;
			node.classList.toggle('writing', writing);
			node.classList.toggle('w2', writing && !still && frame);
		}

		/* The approval hold (v0.15.0). The element exists on approval cards only, so
		   its presence IS the test — no second read of the card's state. */
		var leftEl = node.querySelector('.card-approval-left');
		if (leftEl) {
			var left = approvalRemaining(Number(node.getAttribute('data-approval-at')), nowMs);
			setText(leftEl, approvalText(left));
			/* The word says it and the class only styles it. An expired hold is not
			   an error the card should shout about: the request is still real, it is
			   just no longer answerable here. */
			node.classList.toggle('approval-expired', left === 0);
		}

		/* The turn chip counts from turnStartedAt, not from the last activity —
		   a session can be 40 s quiet and 14 min into its turn. */
		var turnEl = node.querySelector('.card-turn');
		if (!turnEl) continue;
		var turn = Number(node.getAttribute('data-turn'));
		var dur = isFinite(turn) && turn > 0 ? fmtDur((nowMs - turn) / 1000) : EMDASH;
		/* The chip drops its verb on a hung card, and that is a MEASUREMENT, not
		   tidiness: at the four column slot the row has 292 px, and dot + WORKING +
		   "working 8m" + "quiet 3m" is 284 px on this machine's mono and over it on
		   the one headless Chrome falls back to — where the chip lost its number to
		   an ellipsis and the card said nothing at all about the turn. The word is
		   the part with a copy of itself two chips to the left, so the word is what
		   goes: "WORKING 8m · quiet 3m" is both figures with 60 px to spare. */
		setText(turnEl, (hung ? '' : 'working ') + dur);
	}
}

/* --------------------------------------------------- touch action sheet (v2) */

function findSession(id) {
	var sessions = lastGoodDoc && Array.isArray(lastGoodDoc.sessions) ? lastGoodDoc.sessions : [];
	for (var i = 0; i < sessions.length; i++) {
		if (sessions[i] && sessions[i].id === id) return sessions[i];
	}
	return null;
}

var STATE_COLOR_VAR = {
	working: 'var(--accent)',
	needs_input: 'var(--amber)',
	done: 'var(--green)',
	idle: 'var(--faint)'
};

/* v0.3.0: every session opens a sheet. needs_input gets the v0.2.0 action sheet
   unchanged; every other state gets the read-only detail variant, whose only
   control is close. The mode is fixed at open time and the sheet shuts if the
   session leaves that state — so a card that gets answered at the keyboard can
   never leave an Acknowledge button sitting under a finger. */
function openSheet(id) {
	var s = findSession(id);
	if (!s) return;
	sheetGen++;
	sheetSessionId = id;
	sheetMode = 'session';
	sheetOpenState = s.state || 'idle';
	sheetSubSig = null;
	sheetEventSig = null;
	clearSheetTimer();
	sheetBusy = false;
	ui.sheet.classList.remove('busy');
	setSheetStatus('', '');
	ui.sheet.setAttribute('data-mode', s.state === 'needs_input' ? 'action' : 'detail');
	/* Dismiss is a done- or idle-card control only, and the state is fixed at open
	   time — the same discipline as the mode, so a control cannot appear under a
	   finger that is already moving toward where something else was. */
	ui.sheet.setAttribute('data-detail-state', DISMISSABLE[s.state] ? s.state : '');
	/* Tap-to-continue is a working/done detail control (v0.12.0); data-approval is
	   a needs_input/pendingPermission action control. Both are (re)set here for the
	   first frame and data-approval is refreshed every syncSheet so it follows a
	   permission that arrives or is decided while the sheet is open. The continue
	   button set and its status line are reset so a re-opened sheet does not show
	   the previous session's confirmation. */
	ui.sheet.setAttribute('data-continue', (s.state === 'working' || s.state === 'done') ? '1' : '');
	var pend0 = s.state === 'needs_input' && s.pendingPermission && typeof s.pendingPermission === 'object';
	ui.sheet.setAttribute('data-approval', pend0 ? '1' : '');
	continueBtnSig = null;
	continueStatusFor = null;
	setContinueStatus('', '');
	setVar(ui.sheet, '--sheet-accent', STATE_COLOR_VAR[s.state] || 'var(--faint)');
	ui.sheet.classList.add('open');
	ui.sheet.setAttribute('aria-hidden', 'false');
	syncSheet();
	enterSheetFocus();
}

/* The burn breakdown is the same panel in a third mode: it carries no session, so
   every session-scoped sync is skipped and syncSheet routes on sheetMode. */
function openBurnSheet() {
	sheetSessionId = null;
	sheetGen++;
	sheetOpenState = null;
	sheetMode = 'burn';
	burnSig = null;
	clearSheetTimer();
	sheetBusy = false;
	ui.sheet.classList.remove('busy');
	setSheetStatus('', '');
	ui.sheet.setAttribute('data-mode', 'burn');
	ui.sheet.setAttribute('data-detail-state', '');
	ui.sheet.setAttribute('data-approval', '');
	ui.sheet.setAttribute('data-continue', '');
	setVar(ui.sheet, '--sheet-accent', 'var(--accent)');
	ui.sheet.classList.add('open');
	ui.sheet.setAttribute('aria-hidden', 'false');
	syncSheet();
	enterSheetFocus();
}

/* One usage window's detail (v0.19.0): utilization, its reset, the depletion
   forecast and today's split by model. The gauge on the panel has room for a
   percentage, a countdown and one hint line; this is where the rest of what the
   feed says about that window goes.

   INERT WHEN THERE IS NOTHING TO DETAIL. A gauge showing em-dashes has no
   utilization, no reset and no forecast, so opening a sheet of four em-dashes
   would be the panel dressing an absence up as a reading — the caller checks
   before it opens rather than the sheet rendering nothing. */
function openForecastSheet(winKey) {
	if (!winKey || !forecastWindow(winKey)) return;
	sheetSessionId = null;
	sheetGen++;
	sheetOpenState = null;
	sheetMode = 'forecast';
	forecastWin = winKey;
	forecastSig = null;
	clearSheetTimer();
	sheetBusy = false;
	ui.sheet.classList.remove('busy');
	setSheetStatus('', '');
	ui.sheet.setAttribute('data-mode', 'forecast');
	ui.sheet.setAttribute('data-detail-state', '');
	ui.sheet.setAttribute('data-approval', '');
	ui.sheet.setAttribute('data-continue', '');
	setVar(ui.sheet, '--sheet-accent', 'var(--accent)');
	ui.sheet.classList.add('open');
	ui.sheet.setAttribute('aria-hidden', 'false');
	syncSheet();
	enterSheetFocus();
}

/* The day timeline: the same panel in a fourth mode. Like burn it carries no
   session, so every session-scoped sync is skipped and syncSheet routes on
   sheetMode. */
function openTimelineSheet() {
	/* INERT WHILE NOTHING HAS ARRIVED (SCA-018): the connecting head is mostly empty
	   and a stray tap on it would open a timeline of a day nobody has been told
	   about. The rule openForecastSheet and openOverflowSheet already keep. */
	if (!everHadData) return;
	sheetSessionId = null;
	sheetGen++;
	sheetOpenState = null;
	sheetMode = 'timeline';
	timelineSig = null;
	dayDoc = null;
	daySig = null;
	clearSheetTimer();
	sheetBusy = false;
	ui.sheet.classList.remove('busy');
	setSheetStatus('', '');
	ui.sheet.setAttribute('data-mode', 'timeline');
	/* Today and a drilled day are the SAME mode as far as the panel's layout is
	   concerned — same list region, same scroll, same hidden regions — so they
	   share data-mode and differ by this one attribute. Adding a fifth data-mode
	   would have meant restating the timeline's six-selector hide list and its
	   small-slot media query, and two copies of that list is two chances for them
	   to drift. */
	ui.sheet.setAttribute('data-tl-view', 'today');
	ui.sheet.setAttribute('data-detail-state', '');
	ui.sheet.setAttribute('data-approval', '');
	ui.sheet.setAttribute('data-continue', '');
	setVar(ui.sheet, '--sheet-accent', 'var(--accent)');
	ui.sheet.classList.add('open');
	ui.sheet.setAttribute('aria-hidden', 'false');
	syncSheet();
	enterSheetFocus();
}

/* The sessions the grid had no room for (v0.20.0, CD-14) — the same panel in a
   sixth mode. Like burn and timeline it carries no session of its own, so every
   session-scoped sync is skipped and syncSheet routes on sheetMode; it renders
   into the timeline's list region, because a list of rows is what it is and a
   second scrolling region styled the same way is a second one to keep in step.

   INERT WHEN THERE IS NOTHING CUT, the rule openForecastSheet keeps: the tile
   only exists while overflowList does, but a poll can empty it between the paint
   and the fingertip, and a sheet reading "0 sessions" would be the panel
   dressing an absence up as a view. */
function openOverflowSheet() {
	if (!overflowList.length) return;
	sheetGen++;
	sheetSessionId = null;
	sheetOpenState = null;
	sheetMode = 'overflow';
	overflowSig = null;
	clearSheetTimer();
	sheetBusy = false;
	ui.sheet.classList.remove('busy');
	setSheetStatus('', '');
	ui.sheet.setAttribute('data-mode', 'overflow');
	ui.sheet.setAttribute('data-detail-state', '');
	ui.sheet.setAttribute('data-approval', '');
	ui.sheet.setAttribute('data-continue', '');
	setVar(ui.sheet, '--sheet-accent', 'var(--accent)');
	ui.sheet.classList.add('open');
	ui.sheet.setAttribute('aria-hidden', 'false');
	syncSheet();
	enterSheetFocus();
}

/* Follows the feed like every other sheet: the grid re-cuts on every poll, so
   this list moves with it, and a slot or filter change that empties the cut
   closes the sheet rather than leaving a list of rows that are back on the grid
   behind the operator. */
function syncOverflowSheet() {
	if (!overflowList.length) { closeSheet(); return; }
	var rows = overflowList;
	setText(ui.sheetTitle, 'More sessions');
	setText(ui.sheetRepo, rows.length + (rows.length === 1 ? ' session' : ' sessions') +
		' not on the grid ' + EMDASH + ' tap to open');

	var sig = rows.map(function (s) {
		return [s.id, s.state, titleParts(s).text, repoLine(s), effectiveAcked(s) ? '1' : '',
			isPinned(s.id) ? 'p' : ''].join('|');
	}).join('#');
	if (sig === overflowSig) return;
	overflowSig = sig;

	ui.sheetTimeline.textContent = '';
	for (var i = 0; i < rows.length; i++) {
		var s = rows[i];
		var row = document.createElement('div');
		/* .tl-row for the list metrics, .ov-row for the target: the timeline's rows
		   are text and these are controls, and only the second kind gets the 48 px
		   fingertip floor. */
		row.className = 'tl-row ov-row tappable';
		row.setAttribute('data-session-id', String(s.id || ''));
		row.setAttribute('data-state', s.state || 'idle');
		row.setAttribute('role', 'button');
		row.setAttribute('tabindex', '0');
		var st = document.createElement('span');
		st.className = 'ov-state';
		/* The card's own words, not a second vocabulary: a row that says "waiting"
		   here and "needs input" on the card is two names for one state. */
		st.textContent = s.state === 'needs_input' ? 'needs input' : (s.state || 'idle');
		var tag = document.createElement('span');
		tag.className = 'tl-session';
		tag.textContent = titleParts(s).text;
		var repo = document.createElement('span');
		repo.className = 'tl-text';
		repo.textContent = repoLine(s);
		row.appendChild(st);
		row.appendChild(tag);
		row.appendChild(repo);
		ui.sheetTimeline.appendChild(row);
	}
}

function closeSheet() {
	sheetGen++;
	clearSheetTimer();
	sheetSessionId = null;
	sheetOpenState = null;
	sheetMode = null;
	burnSig = null;
	timelineSig = null;
	overflowSig = null;
	hostSig = null;
	dayDoc = null;
	daySig = null;
	forecastWin = null;
	forecastSig = null;
	sheetApprovalAt = 0;
	ui.sheet.setAttribute('data-tl-view', 'today');
	sheetBusy = false;
	ui.sheet.classList.remove('busy');
	ui.sheet.classList.remove('open');
	ui.sheet.setAttribute('aria-hidden', 'true');
	exitSheetFocus();
	ui.sheet.setAttribute('data-detail-state', '');
	ui.sheet.setAttribute('data-approval', '');
	ui.sheet.setAttribute('data-continue', '');
	continueBtnSig = null;
	continueStatusFor = null;
	setContinueStatus('', '');
	setSheetStatus('', '');
}

function clearSheetTimer() {
	if (sheetCloseTimer) { clearTimeout(sheetCloseTimer); sheetCloseTimer = null; }
}

/* SCA-006 — THE SURFACE A RECEIPT BELONGS TO, as one comparable token.

   An action is started against a session on a surface, and its answer can arrive a
   round trip later on a surface that is showing something else. Until v0.32.0 the
   answer was written wherever the operator happened to be looking: acknowledging A
   and switching to B put A's "acknowledged" on B's sheet and B's close timer with
   it, so a delayed 204 for A shut B's sheet under the operator's hand. The POST
   itself was always correct - only the receipt went to the wrong place.

   THE SESSION ID ALONE IS NOT ENOUGH, which is why the two generations are in here:
   the same session can be closed and reopened inside one round trip, and a receipt
   from the first visit would then write itself into the second as though nothing
   had happened. */
function actionSurface(id) {
	return String(id) + '#' + sheetGen + '#' + detailGen;
}

/* True while the receipt for `token` still belongs to what is on the glass. */
function surfaceStillOurs(token) {
	var id = laneCActionSessionId();
	return id !== null && id !== undefined && actionSurface(id) === token;
}

/* Scoped to the surface that asked for it (SCA-006): a close scheduled for A must
   not shut B. */
function scheduleClose(token) {
	clearSheetTimer();
	sheetCloseTimer = setTimeout(function () {
		sheetCloseTimer = null;
		if (token !== undefined && !surfaceStillOurs(token)) return;
		closeSheet();
	}, SHEET_CLOSE_MS);
}

/* Called from every render: the sheet is a view of live data, so it must follow
   the session out of needs_input and shut itself rather than sit there offering
   an ack for a question that has already been answered at the keyboard. */
function syncSheet() {
	if (sheetMode === 'settings') { syncSettingsSheet(); return; }
	if (sheetMode === 'burn') { syncBurnSheet(); return; }
	if (sheetMode === 'forecast') { syncForecastSheet(); return; }
	if (sheetMode === 'timeline') { syncTimelineSheet(); return; }
	if (sheetMode === 'overflow') { syncOverflowSheet(); return; }
	if (sheetMode === 'host') { syncHostSheet(); return; }
	/* The day view renders from the ONE document its tap fetched. The poll still
	   calls through here every 3 s, and it must not turn a read of a fixed past
	   day into a GET every three seconds. */
	if (sheetMode === 'day') { syncDaySheet(); return; }
	if (!sheetSessionId) return;
	var s = findSession(sheetSessionId);
	if (!s || s.state !== sheetOpenState) { closeSheet(); return; }
	/* Same derived-title reading as the card, so a session cannot be one thing in
	   the grid and another in the sheet a tap later. */
	var tp = titleParts(s);
	setText(ui.sheetTitle, tp.text);
	ui.sheetTitle.classList.toggle('title-derived', tp.derived);
	setText(ui.sheetRepo, repoLine(s));

	if (s.state === 'needs_input') {
		/* Approval variant (v0.12.0): re-read from the LIVE row every sync, so a
		   permission that arrives or is decided elsewhere flips the sheet's variant
		   without the sheet closing (state stays needs_input). The Approve button
		   carries the tool name — approving Bash from a touchscreen must show WHAT. */
		var pend = s.pendingPermission && typeof s.pendingPermission === 'object' &&
			!Array.isArray(s.pendingPermission) ? s.pendingPermission : null;
		ui.sheet.setAttribute('data-approval', pend ? '1' : '');
		if (pend) {
			var tool = pend.tool ? String(pend.tool) : 'a tool';
			setText(ui.sheetApprovalTool, tool);
			setText(ui.sheetApprovalSummary, pend.summary ? String(pend.summary) : '');
			setText(ui.sheetApprove, 'Approve ' + tool);
			/* The hold's anchor for the 1 Hz tick (v0.15.0). Parked in a variable
			   rather than written as text here, because the poll is 3 s and a
			   countdown that jumped three seconds at a time would be a worse answer
			   than no countdown: the whole point of it is whether the tap the person
			   is about to make still lands. tickSheetApproval() writes the words. */
			sheetApprovalAt = Date.parse(pend.requestedAt);
			if (!isFinite(sheetApprovalAt)) sheetApprovalAt = 0;
			tickSheetApproval(Date.now());
			renderApprovalThreshold();
		} else {
			sheetApprovalAt = 0;
			setText(ui.sheetQuestion, s.question || s.lastEvent || 'No question text was captured for this session.');
		}
	} else {
		syncSheetMeta(s);
		syncSheetSubs(s);
		/* Tap-to-continue on a working or done detail sheet (v0.12.0). */
		if (s.state === 'working' || s.state === 'done') syncContinue(s);
	}
	syncPinButton(s);
	syncSheetEvents(s);
}

/* Build the continue-button row for a working/done detail sheet. The three
   defaults are hardcoded; any strings the feed carries in a top-level
   continuePrompts array render after them as extras (presence-gated). Rebuilt
   only when the effective button set changes, so a poll does not churn the DOM;
   the status line is cleared when the sheet moves to a different session. */
function syncContinue(s) {
	if (!ui.sheetContinueBtns) return;
	if (continueStatusFor !== s.id) { continueStatusFor = s.id; setContinueStatus('', ''); }
	syncQueuedRow(s);
	/* lane D: the button set is built in one place for this sheet and the Detail
	   view, and it is now per SESSION - the signature below therefore changes when
	   the sheet moves to a session in another repo, which is what rebuilds the row. */
	var list = continueButtons(s);
	var sig = list.map(function (b) { return b.label + '' + b.prompt; }).join('');
	if (sig === continueBtnSig) return;
	continueBtnSig = sig;
	ui.sheetContinueBtns.textContent = '';
	for (var i = 0; i < list.length; i++) {
		var btn = document.createElement('button');
		btn.type = 'button';
		btn.className = 'sheet-btn sheet-continue-btn';
		btn.setAttribute('data-continue-prompt', list[i].prompt);
		btn.setAttribute('data-continue-label', list[i].label);
		btn.textContent = list[i].label;
		ui.sheetContinueBtns.appendChild(btn);
	}
}

/* MF-002. The sheet's queued line and its Cancel control, built lazily above the
   continue buttons: the thing the operator is being offered a way out of belongs
   beside the thing that created it. The row is REMOVED rather than emptied when
   nothing is queued, so an empty line never sits in the layout claiming a queue. */
function syncQueuedRow(s) {
	if (!ui.sheetContinue) return;
	var label = queuedLabel(s);
	if (!label) {
		if (ui.sheetQueued && ui.sheetQueued.parentNode) {
			ui.sheetQueued.parentNode.removeChild(ui.sheetQueued);
			ui.sheetQueued = null;
		}
		return;
	}
	if (!ui.sheetQueued) {
		var row = document.createElement('div');
		row.className = 'sheet-queued';
		var text = document.createElement('span');
		text.className = 'sheet-queued-text';
		row.appendChild(text);
		var btn = document.createElement('button');
		btn.type = 'button';
		/* Its own attribute and no data-sheet-action, the rule every borrowed-looks
		   button in this sheet keeps: the generic branch in onSheetClick would POST
		   an action of null. */
		btn.className = 'sheet-btn sheet-btn-cancel';
		btn.setAttribute('data-cancel-continue', '1');
		btn.textContent = 'Cancel';
		row.appendChild(btn);
		ui.sheetQueued = row;
		ui.sheetQueuedText = text;
		ui.sheetContinue.insertBefore(row, ui.sheetContinueBtns);
	}
	setText(ui.sheetQueuedText, 'queued: ' + label);
}

/* The sheet's copy of the approval countdown (v0.15.0). Driven by tick(), not by
   syncSheet, for the reason in syncSheet: the poll is 3 s and this number is the
   answer to "does the button under my thumb still do anything". Zero means there
   is nothing to count — no approval sheet open, or a requestedAt that did not
   parse — and the line is then empty rather than expired. */
function tickSheetApproval(nowMs) {
	renderApprovalReadiness();   /* MF-017 */
	if (!ui.sheetApprovalLeft) return;
	if (!sheetApprovalAt || ui.sheet.getAttribute('data-approval') !== '1') {
		setText(ui.sheetApprovalLeft, '');
		ui.sheet.classList.remove('approval-expired');
		return;
	}
	var left = approvalRemaining(sheetApprovalAt, nowMs);
	setText(ui.sheetApprovalLeft, approvalText(left));
	ui.sheet.classList.toggle('approval-expired', left === 0);
}

function setContinueStatus(text, kind) {
	/* lane C: the Detail page carries the same status line, so one write reports
	   itself once, in whichever of the two surfaces is on the glass. */
	laneCMirrorContinueStatus(text, kind);
	if (!ui.sheetContinueStatus) return;
	setText(ui.sheetContinueStatus, text);
	ui.sheetContinueStatus.className = 'sheet-continue-status' + (text && kind ? ' ' + kind : '');
}

/* Pin/Unpin is offered on EVERY sheet that carries a session — action mode
   included. A question you are being asked is exactly the kind of session worth
   keeping at the front of the grid, and hiding the control on that one mode
   would make the feature look like it only worked on quiet cards.
   The label is the state, not an instruction about the state: a button reading
   "Unpin" is a pinned card, which is the same fact the card's own glyph carries. */
function syncPinButton(s) {
	if (!ui.sheetPin) return;
	var on = isPinned(s.id);
	setText(ui.sheetPin, on ? 'Unpin session' : 'Pin session');
	if (ui.sheetPin.getAttribute('data-pinned') !== (on ? '1' : '0')) {
		ui.sheetPin.setAttribute('data-pinned', on ? '1' : '0');
	}
}

/* The pin is local state, like Dismiss: nothing is sent to crabd, so there is no
   pending status, no rollback and no busy latch. Re-read off the LIVE row rather
   than off sheetSessionId alone, the same belt onSheetDismiss wears. */
function onSheetPin() {
	if (!sheetSessionId) return;
	var s = findSession(sheetSessionId);
	if (!s) return;
	togglePin(s.id);
	syncPinButton(s);
	render();
}

function syncSheetMeta(s) {
	var chips = [];
	var stateLabel = (s.state === 'needs_input' ? 'needs input' : (s.state || 'idle')).toUpperCase();
	var since = Date.parse(s.stateSince);
	chips.push({ text: stateLabel + '  ' + (isFinite(since) ? fmtDur((Date.now() - since) / 1000) : EMDASH), cls: 'sheet-chip-state' });
	var model = shortModel(s.model);
	if (model) chips.push({ text: model, cls: '' });
	if (s.speed === 'fast') chips.push({ text: 'FAST', cls: 'sheet-chip-fast' });
	var out = Number(s.todayOutputTokens);
	chips.push({ text: (isFinite(out) ? fmtNum(out) : EMDASH) + ' out today', cls: '' });

	/* Rebuilt every sync on purpose: the state chip carries a live duration, so
	   there is nothing stable to sign, and four spans is not a budget. */
	ui.sheetMeta.textContent = '';
	for (var i = 0; i < chips.length; i++) {
		var el = document.createElement('span');
		el.className = 'sheet-chip' + (chips[i].cls ? ' ' + chips[i].cls : '');
		el.textContent = chips[i].text;
		ui.sheetMeta.appendChild(el);
	}
}

/* The sheet is where the subagent list is shown WHOLE — no scroll, so the panel
   height stays a function of the cap and never of the feed. */
function syncSheetSubs(s) {
	var list = subList(s);
	var sig = sheetSessionId + '#' + list.length + '#' +
		list.map(function (d) { return String(d && d.label); }).join('|');
	if (sig !== sheetSubSig) {
		sheetSubSig = sig;
		ui.sheetSubs.textContent = '';
		var rows = buildSubRows(list, SHEET_SUB_MAX);
		if (rows) {
			while (rows.firstChild) ui.sheetSubs.appendChild(rows.firstChild);
		}
	}
	var ages = ui.sheetSubs.querySelectorAll('.sub-age');
	for (var i = 0; i < ages.length && i < list.length; i++) {
		var secs = list[i] ? Number(list[i].ageSec) : NaN;
		setText(ages[i], isFinite(secs) ? fmtDur(secs) : EMDASH);
	}
}

/* events[] is optional. Absent (or empty) it renders as a stated absence, not as
   a blank region that reads like nothing happened. */
function syncSheetEvents(s) {
	var all = Array.isArray(s.events) ? s.events.slice(0, SHEET_EVENTS_MAX) : [];
	var cap = s.state === 'needs_input' ? SHEET_EVENTS_MAX_ACTION : SHEET_EVENTS_MAX;
	var list = all.slice(0, cap);
	var hidden = all.length - list.length;
	var sig = sheetSessionId + '#' + list.length + '#' + hidden + '#' +
		list.map(function (e) { return String(e && e.at) + String(e && e.text); }).join('|');
	if (sig !== sheetEventSig) {
		sheetEventSig = sig;
		ui.sheetEvents.textContent = '';
		if (!list.length) {
			var none = document.createElement('div');
			none.className = 'sheet-events-empty';
			none.textContent = 'No events recorded since crabd started.';
			ui.sheetEvents.appendChild(none);
		}
		for (var i = 0; i < list.length; i++) {
			var row = document.createElement('div');
			row.className = 'event-row';
			row.setAttribute('data-at', String(Date.parse(list[i] && list[i].at) || ''));
			var age = document.createElement('span');
			age.className = 'event-age';
			age.textContent = EMDASH;
			var text = document.createElement('span');
			text.className = 'event-text';
			text.textContent = (list[i] && list[i].text) ? String(list[i].text) : 'event';
			row.appendChild(age);
			row.appendChild(text);
			ui.sheetEvents.appendChild(row);
		}
		if (hidden > 0) {
			var more = document.createElement('div');
			more.className = 'sheet-events-empty';
			more.textContent = '+' + hidden + ' earlier';
			ui.sheetEvents.appendChild(more);
		}
	}
	var now = Date.now();
	var rows = ui.sheetEvents.querySelectorAll('.event-row');
	for (var k = 0; k < rows.length; k++) {
		var at = Number(rows[k].getAttribute('data-at'));
		setText(rows[k].querySelector('.event-age'),
			isFinite(at) && at > 0 ? fmtDur((now - at) / 1000) + ' ago' : EMDASH);
	}
}

/* ------------------------------------------- burn by session (v0.4.0) */

/* Read-only breakdown of burn.today by session, biggest first, every state
   included — an idle session that burned 400k this morning is exactly what this
   view exists to surface.

   Honesty rule: the rows do NOT add up to burn.today.outputTokens and are not
   presented as if they might. todayOutputTokens is per LIVE session, while the
   day total also carries subagent spend and sessions that have since gone. So
   the list is labelled "live sessions" and the day total is stated separately,
   as a different number rather than a failed reconciliation. */
function syncBurnSheet() {
	var doc = lastGoodDoc;
	var sessions = doc && Array.isArray(doc.sessions) ? doc.sessions : [];
	var rows = sessions.slice().filter(function (s) { return !!s; }).sort(function (a, b) {
		return (Number(b.todayOutputTokens) || 0) - (Number(a.todayOutputTokens) || 0);
	});

	setText(ui.sheetTitle, 'Today by session');
	setText(ui.sheetRepo, 'by session (live sessions)');

	var total = doc && doc.burn && doc.burn.today ? Number(doc.burn.today.outputTokens) : NaN;
	var commits = doc && doc.recap && Array.isArray(doc.recap.commits) ? doc.recap.commits : [];
	var byModel = doc && doc.burn && Array.isArray(doc.burn.byModel) ? doc.burn.byModel.slice(0, BYMODEL_MAX) : [];
	var sig = rows.map(function (s) { return s.id + ':' + s.todayOutputTokens + ':' + s.model; }).join('|') +
		'#' + total + '#' + commits.map(function (c) { return String(c && c.repo) + (c && c.count); }).join(',') +
		'#' + byModel.map(function (m) { return String(m && m.model) + ':' + (m && m.outputTokens); }).join(',');
	if (sig === burnSig) return;
	burnSig = sig;

	ui.sheetBurn.textContent = '';
	if (!rows.length) {
		ui.sheetBurn.appendChild(burnNote('No live sessions to break down.'));
	}
	for (var i = 0; i < rows.length; i++) {
		var s = rows[i];
		var row = document.createElement('div');
		row.className = 'burn-row';
		var title = document.createElement('span');
		title.className = 'burn-title';
		/* The same derived-title reading the cards use: a session with no title of
		   its own is named by its repo here too, so one row cannot be "acme-api" in
		   the grid and "(untitled session)" in the sheet behind it. */
		title.textContent = titleParts(s).text;
		var model = document.createElement('span');
		model.className = 'burn-model';
		model.textContent = shortModel(s.model) || EMDASH;
		var tok = document.createElement('span');
		tok.className = 'burn-tokens';
		var v = Number(s.todayOutputTokens);
		tok.textContent = isFinite(v) ? fmtNum(v) : EMDASH;
		row.appendChild(title);
		row.appendChild(model);
		row.appendChild(tok);
		ui.sheetBurn.appendChild(row);
	}

	if (isFinite(total)) {
		ui.sheetBurn.appendChild(burnNote('today ' + fmtNum(total) +
			' out in total ' + EMDASH + ' includes subagent and ended-session spend not listed above'));
	}
	/* burn.byModel is optional. Absent — an older crabd, or a feed with nothing
	   to split — renders nothing at all and the sheet is exactly its pre-split
	   self. */
	appendByModel(byModel);

	/* The header line names only the top repo; the whole list lives here. */
	if (commits.length) {
		var parts = [];
		for (var c = 0; c < commits.length; c++) {
			if (!commits[c] || !commits[c].repo) continue;
			parts.push(String(commits[c].repo) + ' ' + commits[c].count);
		}
		if (parts.length) ui.sheetBurn.appendChild(burnNote('commits today ' + EMDASH + '  ' + parts.join('   ·   ')));
	}
}

function burnNote(text) {
	var el = document.createElement('div');
	el.className = 'burn-note';
	el.textContent = text;
	return el;
}

/* The model split, appended into the burn sheet above the commits line. The bar
   is proportional to the LARGEST model in the list, not to the day total: the
   contract caps byModel at 4, so the rows need not sum to anything, and scaling
   against a total the list does not cover would make every bar a stub. A single
   model therefore reads as a full bar, which is the honest picture. */
function appendByModel(list) {
	var rows = [];
	var peak = 0;
	for (var i = 0; i < list.length; i++) {
		var m = list[i];
		if (!m || !m.model) continue;
		/* typeof, not Number(): Number(null) is 0, and a model whose figure the feed
		   could not produce must be DROPPED, never drawn as a zero bar (§4.5). */
		var v = m.outputTokens;
		if (typeof v !== 'number' || !isFinite(v) || v < 0) continue;
		rows.push({ name: shortModel(m.model) || String(m.model), tokens: v });
		if (v > peak) peak = v;
	}
	if (!rows.length) return;

	var wrap = document.createElement('div');
	wrap.className = 'burn-models';
	var head = document.createElement('div');
	head.className = 'burn-models-head';
	head.textContent = 'by model';
	wrap.appendChild(head);

	for (var k = 0; k < rows.length; k++) {
		var row = document.createElement('div');
		row.className = 'burn-model-row';
		var name = document.createElement('span');
		name.className = 'bm-name';
		name.textContent = rows[k].name;
		var bar = document.createElement('span');
		bar.className = 'bm-bar';
		var fill = document.createElement('span');
		fill.className = 'bm-fill';
		/* An all-zero list would divide by zero; a zero-width bar is the truth. */
		setVar(fill, '--w', String(peak > 0 ? Math.round((rows[k].tokens / peak) * 100) : 0));
		bar.appendChild(fill);
		var tok = document.createElement('span');
		tok.className = 'bm-tokens';
		tok.textContent = fmtNum(rows[k].tokens);
		row.appendChild(name);
		row.appendChild(bar);
		row.appendChild(tok);
		wrap.appendChild(row);
	}
	ui.sheetBurn.appendChild(wrap);
}

/* ------------------------------------------- window forecast (v0.19.0) */

/* The live window behind a forecast sheet, looked up by KEY on every sync so the
   sheet tracks a utilization that moves while it is open.

   Returns null for every shape the gauge itself renders as em-dashes — limits
   unavailable, the window gone, an extra index the endpoint stopped reporting, a
   utilization that is not a finite number. That null is what makes the tap inert
   rather than opening a sheet of four em-dashes, and what closes the sheet if the
   window disappears underneath it. */
function forecastWindow(key) {
	var limits = lastGoodDoc && lastGoodDoc.limits;
	if (!limits || limits.available !== true) return null;
	var win = null;
	if (key === 'fiveHour') win = limits.fiveHour;
	else if (key === 'weekly') win = limits.weekly;
	else {
		var m = /^extra(\d+)$/.exec(String(key || ''));
		if (m && Array.isArray(limits.extra)) win = limits.extra[Number(m[1])];
	}
	if (!win || typeof win !== 'object' || Array.isArray(win)) return null;
	/* The same test setGauge uses, so the sheet and the gauge can never disagree
	   about whether this window has a reading at all. */
	if (typeof win.utilization !== 'number' || !isFinite(win.utilization)) return null;
	return win;
}

function forecastWinLabel(key, win) {
	if (key === 'fiveHour') return '5-hour window';
	if (key === 'weekly') return 'Weekly window';
	return (win && win.label ? String(win.label) : 'Usage window');
}

/* The forecast line, in WORDS, for a person who tapped to ask.

   forecastLabel() answers four different facts with the same empty string, and on
   the gauge that is right: a hint line reading "no forecast" under every calm
   window would be noise on a panel read from across a room. In here silence is
   not an answer, because the tap WAS the question — so the one branch that
   carries a distinct fact is separated out and the rest say "no forecast".

   "resets before it depletes" is that branch. crabd never extrapolates past a
   window's own reset (contract v0.13.0, tightened at v0.17.0 so an unparseable
   reset serves null rather than an invented date), and the widget guards it
   again — so an exhaustAt at or after the reset means the window turns over
   first. That is a reassurance, not an absence, and it reads as one.
   Everything else — no exhaustAt, an unparseable one, a projection whose moment
   has already passed — is honestly "no forecast". A date is NEVER manufactured
   to fill the row. */
function forecastText(win, nowMs, use24) {
	var label = forecastLabel(win.exhaustAt, win.resetsAt, nowMs, use24);
	if (label) return label;
	var ex = win.exhaustAt ? Date.parse(win.exhaustAt) : NaN;
	var rs = win.resetsAt ? Date.parse(win.resetsAt) : NaN;
	if (isFinite(ex) && ex > nowMs && isFinite(rs) && ex >= rs) return 'resets before it depletes';
	return 'no forecast';
}

/* One window, in full: what it reads, when it turns over, when the recent burn
   would fill it, and what the day went on. It renders into the burn region under
   a data-mode of its own — see index.html for why it is not a fifth tl-view. */
function syncForecastSheet() {
	var win = forecastWin ? forecastWindow(forecastWin) : null;
	/* The sheet follows its subject out, exactly as the session sheet does: a
	   window that stops being reported must not leave a stale reading on glass. */
	if (!win) { closeSheet(); return; }
	var doc = lastGoodDoc;
	var limits = doc && doc.limits;
	var use24 = use24Clock();
	var now = Date.now();

	var pct = Math.round(Math.max(0, Math.min(1, win.utilization)) * 100) + '%';
	var rs = win.resetsAt ? Date.parse(win.resetsAt) : NaN;
	var resetsIn = isFinite(rs) && rs > now ? resetLabel(rs, now, use24) : EMDASH;
	var resetsAt = isFinite(rs) ? momentText(new Date(rs), use24) : EMDASH;
	var forecast = forecastText(win, now, use24);
	var note = limits && limits.note !== null && limits.note !== undefined ? String(limits.note) : '';
	var official = !!(limits && limits.source === 'statusline');
	var byModel = doc && doc.burn && Array.isArray(doc.burn.byModel) ? doc.burn.byModel.slice(0, BYMODEL_MAX) : [];

	var sig = forecastWin + '#' + pct + '#' + resetsIn + '#' + resetsAt + '#' + forecast +
		'#' + note + '#' + (official ? '1' : '') +
		'#' + byModel.map(function (m) { return String(m && m.model) + ':' + (m && m.outputTokens); }).join(',');
	if (sig === forecastSig) return;
	forecastSig = sig;

	setText(ui.sheetTitle, forecastWinLabel(forecastWin, win));
	setText(ui.sheetRepo, 'usage window' + (official ? '  ' + EMDASH + '  official' : ''));

	ui.sheetBurn.textContent = '';
	appendForecastRow('utilization', pct);
	appendForecastRow('resets in', resetsIn);
	appendForecastRow('resets at', resetsAt);
	appendForecastRow('forecast', forecast);
	/* Why the forecast row can say "no forecast" on a window that is visibly
	   filling: it is the answer to the question the row raises, and it is a fact
	   about crabd rather than about this window. */
	ui.sheetBurn.appendChild(burnNote('forecast projects the recent burn rate, and is never carried past the reset above'));
	if (note) ui.sheetBurn.appendChild(burnNote(note));
	/* burn.byModel is TODAY across every window, not this one's split — the feed
	   carries no per-window breakdown and one is not invented here. Labelled so
	   the rows below cannot be read as this window's own. */
	if (byModel.length) {
		ui.sheetBurn.appendChild(burnNote("today's output, all windows"));
		appendByModel(byModel);
	}
}

function appendForecastRow(key, value) {
	var row = document.createElement('div');
	row.className = 'fc-row';
	var k = document.createElement('span');
	k.className = 'fc-key';
	k.textContent = key;
	var v = document.createElement('span');
	v.className = 'fc-val';
	v.textContent = value;
	row.appendChild(k);
	row.appendChild(v);
	ui.sheetBurn.appendChild(row);
	return row;
}

/* ------------------------------------------- today timeline (v0.5.0) */

/* Every session's events[], merged, tagged with the session it came from and
   sorted newest first. Sessions with no events contribute nothing rather than an
   empty heading, and an entirely empty day says so in words — a blank panel
   would read as a broken sheet, not as a quiet morning.
   The rows are built from events the RUNNING crabd observed (contract: a ring
   buffer since start-up), so the empty line names that boundary instead of
   claiming nothing happened today. */
function syncTimelineSheet() {
	var doc = lastGoodDoc;
	var sessions = doc && Array.isArray(doc.sessions) ? doc.sessions : [];
	var use24 = use24Clock();
	var merged = [];
	for (var i = 0; i < sessions.length; i++) {
		var s = sessions[i];
		if (!s || !Array.isArray(s.events)) continue;
		/* titleParts(), not the raw title (v0.20.0, CD-39). The card and the session
		   sheet both name a title-less row after its REPO and only say "untitled"
		   when there is no repo either; this list was reading `s.title` directly, so
		   an older crabd — or any session crabd could not derive a title for — put a
		   column of identical "(untitled)" tags in the one view whose whole job is
		   telling several sessions apart. shortTitle still does the clamping, so the
		   fallback goes through the same trim every other tag does. */
		var tag = shortTitle(titleParts(s).text);
		for (var e = 0; e < s.events.length; e++) {
			var ev = s.events[e];
			if (!ev) continue;
			var at = Date.parse(ev.at);
			if (!isFinite(at)) continue;
			merged.push({ at: at, tag: tag, text: ev.text ? String(ev.text) : 'event' });
		}
	}
	merged.sort(function (a, b) { return b.at - a.at; });

	/* recap.week is presence-gated like every other additive field: absent, the
	   footer stays empty, the cap stays 20 and the sheet is exactly its pre-0.7.0
	   self. */
	var week = weekRows(doc ? doc.recap : null);
	var cap = week ? TIMELINE_MAX_WEEK : TIMELINE_MAX;
	var hidden = Math.max(0, merged.length - cap);
	merged = merged.slice(0, cap);

	setText(ui.sheetTitle, 'Today');
	setText(ui.sheetRepo, 'every session, newest first');

	/* The clock property is in the signature because a 12h/24h flip changes every
	   rendered row without changing a single event. */
	var sig = (use24 ? '24' : '12') + '#' + hidden + '#' + merged.map(function (r) {
		return r.at + ':' + r.tag + ':' + r.text;
	}).join('|') + '#' + (week ? week.map(function (d) {
		/* The day string is in the signature as well as the letter: a strip that
		   rolls over midnight can carry the same seven LETTERS as the day before,
		   and the columns' tap targets would then keep pointing at last week. */
		return d.day + ':' + d.letter + ':' + d.done + ':' + d.commits;
	}).join(',') : '');
	if (sig === timelineSig) return;
	timelineSig = sig;

	renderWeekStrip(week);
	ui.sheetTimeline.textContent = '';
	if (!merged.length) {
		var none = document.createElement('div');
		none.className = 'tl-empty';
		none.textContent = 'No events recorded since crabd started.';
		ui.sheetTimeline.appendChild(none);
		return;
	}
	for (var r = 0; r < merged.length; r++) {
		var row = document.createElement('div');
		row.className = 'tl-row';
		var time = document.createElement('span');
		time.className = 'tl-time';
		time.textContent = fmtTimeOfDay(new Date(merged[r].at), use24);
		var tagEl = document.createElement('span');
		tagEl.className = 'tl-session';
		tagEl.textContent = merged[r].tag;
		var text = document.createElement('span');
		text.className = 'tl-text';
		text.textContent = merged[r].text;
		row.appendChild(time);
		row.appendChild(tagEl);
		row.appendChild(text);
		ui.sheetTimeline.appendChild(row);
	}
	if (hidden > 0) {
		var more = document.createElement('div');
		more.className = 'tl-empty';
		more.textContent = '+' + hidden + ' earlier';
		ui.sheetTimeline.appendChild(more);
	}
}

/* ------------------------------------------- week strip (v0.7.0) */

/* recap.week, normalised for rendering: the last 7 entries, oldest first, one
   object per day. Returns null when the feed carries no week at all — which is
   what keeps the timeline sheet rendering exactly as it did before this version.

   done and commits are read with typeof, not Number(): Number(null) is 0, and a
   day whose figure the feed could not produce must show an em-dash, never a
   zero. "Nobody finished anything on Tuesday" and "crabd cannot say what
   happened on Tuesday" are different facts and this strip must not merge them
   (the same rule the by-model split follows). */
function weekRows(recap) {
	var week = recap && typeof recap === 'object' && Array.isArray(recap.week) ? recap.week : null;
	if (!week) return null;
	var rows = [];
	for (var i = Math.max(0, week.length - WEEK_DAYS); i < week.length; i++) {
		var d = week[i];
		if (!d || typeof d !== 'object') continue;
		rows.push({
			letter: weekdayLetter(d.day),
			/* The day STRING is carried through in v0.8.0 because it is the drill's
			   only argument. Validated here rather than at the tap: a column with no
			   usable day gets no affordance at all, which is better than a target
			   that looks live and does nothing. */
			day: typeof d.day === 'string' && DAY_RE.test(d.day) ? d.day : null,
			done: typeof d.done === 'number' && isFinite(d.done) ? d.done : null,
			commits: typeof d.commits === 'number' && isFinite(d.commits) ? d.commits : null
		});
	}
	return rows.length ? rows : null;
}

/* Three labelled rows in one grid: the weekday letters, then done, then commits.
   Both number rows carry their own name in a left-hand column, because two
   unlabelled rows of digits under a row of letters is a puzzle, not a summary —
   and telling them apart by brightness alone would fail the same
   colour-is-never-the-only-cue rule the fleet dots follow.
   The last column is today, and is marked: the strip's whole value is reading
   the run-up to now, which needs a fixed end to read from. */
function renderWeekStrip(week) {
	ui.sheetWeek.textContent = '';
	if (!week) return;

	var head = document.createElement('div');
	head.className = 'week-head';
	head.textContent = 'last 7 days';
	ui.sheetWeek.appendChild(head);

	var grid = document.createElement('div');
	grid.className = 'week-grid';
	appendWeekRow(grid, '', week, function (d) { return d.letter; }, 'week-day');
	appendWeekRow(grid, 'done', week, function (d) { return d.done === null ? EMDASH : String(d.done); }, 'week-done');
	appendWeekRow(grid, 'commits', week, function (d) { return d.commits === null ? EMDASH : String(d.commits); }, 'week-commits');
	appendWeekHits(grid, week);
	ui.sheetWeek.appendChild(grid);
}

/* ONE hit target per DAY COLUMN (v0.14.0), spanning the three cells it covers.

   v0.8.0 put data-day on all three cells and called the column one target. It was
   not: measured 2026-08-26 at the 2560x720 slot, each cell is 115.8 x 19 px with a
   3.6 px row gap between them — three separate targets, every one of them a third
   of the 48 px fingertip floor the rest of the panel keeps, with dead air in the
   joins. It was the only control on the whole panel that failed that floor.

   The hit element is ABSOLUTELY POSITIONED inside the grid rather than being a
   grid item, and that is what stops it displacing the cells: an absolutely
   positioned child of a grid container takes its containing block from the grid
   area its grid-row/grid-column name, and takes no part in auto-placement. So the
   geometry is read off the same grid the numbers are laid out by and there is no
   second copy of the column arithmetic to drift out of step with the first.
   Appended LAST so it is on top for hit-testing; its affordance wash is the same
   0.045 the cells used to carry behind them, which at that alpha reads the same
   in front of them. */
function appendWeekHits(grid, week) {
	for (var i = 0; i < week.length; i++) {
		/* A column with no usable day gets no hit element at all, so it stays inert
		   AND unmarked by construction — the same gate the cells used to carry. */
		if (!week[i].day) continue;
		var hit = document.createElement('span');
		hit.className = 'week-hit';
		hit.setAttribute('data-day', week[i].day);
		hit.setAttribute('title', 'open ' + week[i].day);
		hit.setAttribute('role', 'button');
		hit.setAttribute('aria-label', 'open ' + week[i].day);
		/* Grid column 1 is the row-label column, so day i is column i + 2. BOTH ends
		   are named: for an absolutely positioned grid child an `auto` end line means
		   the grid container's PADDING EDGE, not "one track" — leaving the end off
		   gave every column a target running to the right edge of the strip, each one
		   overlapping all the columns after it (measured 2026-08-26). */
		hit.style.gridColumn = (i + 2) + ' / ' + (i + 3);
		hit.style.gridRow = '1 / -1';
		grid.appendChild(hit);
	}
}

function appendWeekRow(grid, label, week, pick, cls) {
	var key = document.createElement('span');
	key.className = 'week-key';
	key.textContent = label;
	grid.appendChild(key);
	for (var i = 0; i < week.length; i++) {
		var cell = document.createElement('span');
		cell.className = 'week-cell ' + cls + (i === week.length - 1 ? ' week-today' : '');
		cell.textContent = pick(week[i]);
		/* The cells are NUMBERS, not targets, since v0.14.0: data-day moved to one
		   hit element per column (appendWeekHits) because three 19 px cells with gaps
		   between them were never the single target this comment used to claim. */
		grid.appendChild(cell);
	}
}

/* ------------------------------------------- the day drill (v0.8.0) */

/* GET /v1/history?day=YYYY-MM-DD. A read, and the ONLY new network call in this
   version.

   NOT LATCHED, and the distinction is the whole design. /v1/config's 404 latch
   exists because a POST to an endpoint an older crabd does not have is a write
   the widget must stop attempting; a GET that fails is just a GET that failed.
   An older crabd 404s this and the tap is inert — but the very next tap tries
   again, because crabd redeploys under a live widget and the endpoint may exist
   by then. There is deliberately no "history unsupported" flag anywhere in this
   file: adding one would strand the whole feature until someone reloaded the panel,
   which is exactly the failure the v0.6.1 schema rework was written to stop
   repeating. */
function fetchHistory(day) {
	var url = mockName
		/* Mock mode has no crabd to answer, so each day is a canned document on
		   disk and a day with no file 404s from the static server — which is the
		   real older-crabd path, produced rather than simulated. */
		? mockHistoryUrl(day)
		: baseUrl() + '/v1/history?day=' + encodeURIComponent(day);
	var opts = { cache: 'no-store' };
	var ctl = null, timer = null;
	if (typeof AbortController !== 'undefined') {
		ctl = new AbortController();
		opts.signal = ctl.signal;
		timer = setTimeout(function () { try { ctl.abort(); } catch (e) {} }, HISTORY_TIMEOUT_MS);
	}
	return fetch(url, opts).then(function (r) {
		if (timer) clearTimeout(timer);
		if (!r.ok) throw new Error('HTTP ' + r.status);
		return r.json();
	}, function (e) {
		if (timer) clearTimeout(timer);
		throw e;
	}).then(function (doc) {
		return mockName ? rebaseMockHistory(doc, day) : doc;
	});
}

/* Mock only: move a canned day's events onto the day that was ASKED for, keeping
   each one's local clock time (v0.19.0).

   For a day named in a fixture this is a no-op — the requested day and the file's
   own day are the same string, so every ts lands back where it started, and the
   08-21 / 08-24 / 08-25 documents render exactly as they did. It exists for the
   TODAY file, which has no date in its name and must not have one baked into its
   contents either: a fixture whose events are stamped with the afternoon it was
   written reads as a day-old history the morning after, which is the same staleness
   that left `mock-history-2026-08-26.json` labelled "today" while it 404ed.
   The state loader rebases the whole document for the same reason; this is that
   rule applied to the one document it does not reach. */
function rebaseMockHistory(doc, day) {
	var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ''));
	if (!m || !doc || typeof doc !== 'object' || !Array.isArray(doc.events)) return doc;
	for (var i = 0; i < doc.events.length; i++) {
		var ev = doc.events[i];
		if (!ev || typeof ev !== 'object') continue;
		var t = Date.parse(ev.ts);
		if (!isFinite(t)) continue;
		var d = new Date(t);
		/* Built from parts, never through a parsed date string: the LOCAL clock time
		   is what is being preserved, and going via UTC would slide it by the offset. */
		ev.ts = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]),
			d.getHours(), d.getMinutes(), d.getSeconds()).toISOString();
	}
	return doc;
}

/* Which canned document a mock day tap reads (v0.19.0).

   TODAY IS NOT A DATE HERE. Every other day the week strip offers is a literal
   string out of the fixture, so a file named for it is stable — but today is read
   off the wall clock, and a fixture named `mock-history-2026-08-26.json` stopped
   being today at midnight on the 26th. (It had: the v0.8.0 table called that file
   "today" and it was 404ing by the time this was written.) So today routes to a
   name with no date in it, and the harness stays right on every future day.

   `&hist=` then picks WHICH today, because the three cases the drill has to get
   right are a rich day, an empty one and a crabd that cannot answer at all — and
   only one of them can be the default file. `error` names a file that is not
   there, so the 404 comes from the static server: the older-crabd path produced
   rather than simulated, the same discipline the missing 08-20/08-23 files keep. */
function mockHistoryUrl(day) {
	if (day !== todayKey()) return './mock/mock-history-' + day + '.json';
	if (histAuto === 'empty') return './mock/mock-history-today-empty.json';
	if (histAuto === 'error') return './mock/mock-history-today-missing.json';
	return './mock/mock-history-today.json';
}

/* A day column tap. Attempt-and-handle: the sheet only swaps once a document has
   actually landed, so a failure leaves the timeline exactly as it was and the
   tap simply did nothing — no error banner, no half-opened view. The panel must
   never explain crabd's version to somebody walking past it. */
function openDaySheet(day, onFail) {
	if (!DAY_RE.test(String(day || ''))) return;
	/* One in flight at a time. A second tap on a slow fetch would otherwise race
	   two documents into one view, and the loser could land last. */
	if (dayBusy) return;
	dayBusy = true;
	var req = ++dayReqId;
	var gen = sheetGen;
	/* v0.19.0: `fromPanel` opens the sheet from the main panel rather than from
	   inside an already-open one, so the "did the sheet move under us" guard below
	   must not require it to be open already. */
	var fromPanel = typeof onFail === 'function';
	fetchHistory(day).then(function (doc) {
		dayBusy = false;
		/* The sheet may have been closed, or moved to another view, while this was
		   in flight. Swapping the panel now would be swapping it under a finger. */
		if (req !== dayReqId) return;
		/* v0.20.0 (CD-35). The open/closed test below cannot see a sheet that was
		   closed and REOPENED on something else while this was in flight — it is
		   open again, so the reply used to repaint whatever is there now. The
		   generation counter is what "the sheet I was fetching for" means; it also
		   covers the fromPanel path, which skips the open test entirely and would
		   otherwise reopen a sheet the operator has already dismissed. */
		if (gen !== sheetGen) return;
		if (!fromPanel && !ui.sheet.classList.contains('open')) return;
		if (!doc || typeof doc !== 'object') {
			if (onFail) onFail('malformed reply');
			return;
		}
		if (onFail) onFail(null);
		showDay(day, doc);
	}).catch(function (e) {
		dayBusy = false;
		var why = e && e.message ? e.message : 'fetch failed';
		/* Console only for a week-strip column: the tap is inert and the timeline
		   behind it is untouched, which is the whole answer on glass.
		   A control that exists ONLY to open this view cannot be silent, though —
		   a tap that appears to do nothing reads as a broken panel — so the caller
		   that owns such a control passes a hook and says so in its own words. */
		logLine('history ' + day + ' unavailable (' + why + ')');
		if (onFail) onFail(why);
	});
}

/* ------------------------------- today's persisted history (v0.19.0) */

/* The Sessions header's History chip. It reads the SAME endpoint the week strip's
   day columns read, for today — which is the day the strip's own last column
   covers but which nothing on the main panel could reach in one tap.

   Why it is not the header's existing Today timeline: that view is
   `sessions[].events`, a per-session ring capped at 8 entries and rebuilt when
   crabd restarts. By mid-afternoon the approvals, denials and continues from the
   morning have all been pushed out of it. `/v1/history` is the persisted file and
   keeps them. Two different facts, two controls. */
function openTodayHistory() {
	openDaySheet(todayKey(), function (why) {
		if (!why) { markHistoryReady(); return; }
		markHistoryUnavailable(why);
	});
}

/* HONEST FAILURE, and it is the point of the control rather than a detail of it.
   An older crabd 404s /v1/history, and its day document and an unreachable one
   are indistinguishable from a tap — so opening the day view anyway would put
   "No events recorded for this day." on the glass over a day that may have been
   the busiest of the week. The sheet is therefore never opened; the chip carries
   the reason instead.

   NOT A LATCH. The next tap fetches again, exactly as the week strip's does and
   for the same reason: crabd redeploys under a live widget, and a widget that
   remembered "unsupported" would need a console import to forget it. The timer
   only clears a stale reason off the header once nobody is looking at it. */
function markHistoryUnavailable(why) {
	histFailUntil = Date.now() + HISTORY_FAIL_MS;
	paintHistoryChip();
	logLine('today history unavailable (' + String(why) + ')');
}

function markHistoryReady() {
	if (!histFailUntil) return;
	histFailUntil = 0;
	paintHistoryChip();
}

/* Hidden outright when the feed is not live. A widget with no companion has no
   history to offer, and this panel is a working clock without one — a control
   that could only ever report its own absence is worse than no control. The
   stale state hides it too: a feed that has stopped answering /v1/state will not
   answer /v1/history either, and offering the tap would be inviting the failure
   path on purpose. */
function setHistoryChip(status) {
	if (!ui.historyChip) return;
	var show = status === 'live';
	if (ui.historyChip.classList.contains('shown') !== show) ui.historyChip.classList.toggle('shown', show);
	paintHistoryChip();
}

function paintHistoryChip() {
	if (!ui.historyChip) return;
	var failed = histFailUntil > Date.now();
	if (!failed && histFailUntil) histFailUntil = 0;
	var label = failed ? 'No history' : 'History';
	var hint = failed
		? "today's history could not be read — the companion may predate 0.8.0; tap to try again"
		: "Open today's history";
	if (ui.historyChip.textContent !== label) setText(ui.historyChip, label);
	if (ui.historyChip.getAttribute('data-history') !== (failed ? 'off' : '')) {
		ui.historyChip.setAttribute('data-history', failed ? 'off' : '');
	}
	if (ui.historyChip.getAttribute('aria-label') !== hint) ui.historyChip.setAttribute('aria-label', hint);
	if (ui.historyChip.getAttribute('title') !== hint) ui.historyChip.setAttribute('title', hint);
}

function showDay(day, doc) {
	sheetSessionId = null;
	sheetOpenState = null;
	sheetMode = 'day';
	dayDoc = doc;
	daySig = null;
	dayDoc.day = day;
	clearSheetTimer();
	sheetBusy = false;
	ui.sheet.classList.remove('busy');
	setSheetStatus('', '');
	ui.sheet.setAttribute('data-mode', 'timeline');
	ui.sheet.setAttribute('data-tl-view', 'day');
	ui.sheet.setAttribute('data-detail-state', '');
	setVar(ui.sheet, '--sheet-accent', 'var(--accent)');
	ui.sheet.classList.add('open');
	ui.sheet.setAttribute('aria-hidden', 'false');
	syncSheet();
	enterSheetFocus();
}

/* ------------------------------------------- day navigation (v0.10.0) */

/* YYYY-MM-DD shifted by whole LOCAL days, and back out in the same form. Built
   from the three parts and never through Date.parse, which reads a bare date as
   UTC: day arithmetic that goes through UTC lands a day early for anyone west of
   Greenwich. Date's own month overflow does the month and year ends, so there is
   no calendar table here to get wrong. */
function shiftDay(day, delta) {
	var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ''));
	if (!m) return null;
	var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]) + delta);
	if (isNaN(d.getTime())) return null;
	return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}

/* Today as the feed spells it. Read off the wall clock, not off the document:
   the panel runs for weeks and a "today" captured at open time would let the
   next arrow walk into tomorrow after midnight. */
function todayKey() {
	var d = new Date();
	return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate());
}

/* Next stops AT today, and says so by going inert rather than by disappearing: a
   control that vanishes at the end of the week moves the two beside it under a
   finger already travelling toward them. Tomorrow has no history to read, and an
   arrow whose every press is a 404 teaches the panel is broken.
   Prev is never disabled. History thins out backwards with no boundary the
   widget can know — crabd keeps one rotated generation and nothing says where it
   ends — so a day it has nothing for is handled the way every other history miss
   is: the tap is inert, one console line, the view exactly as it was. Guessing a
   floor here would grey out days that are actually readable.
   Comparison is on the strings: YYYY-MM-DD sorts as a date by construction. */
function updateDayNav(day) {
	var next = shiftDay(day, 1);
	if (ui.sheetNextDay) ui.sheetNextDay.disabled = !next || next > todayKey();
	if (ui.sheetPrevDay) ui.sheetPrevDay.disabled = !shiftDay(day, -1);
}

function onDayStep(delta) {
	if (sheetMode !== 'day' || !dayDoc || !isFinite(delta) || !delta) return;
	var target = shiftDay(dayDoc.day, delta);
	if (!target) return;
	/* Belt to the disabled attribute: a tap landing between a render and a
	   re-disable must not fetch a day that cannot exist yet. */
	if (target > todayKey()) return;
	openDaySheet(target);
}

/* YYYY-MM-DD -> a title a person reads as a date. Parsed as LOCAL midnight from
   its three parts, never through Date.parse: a bare "2026-08-24" is parsed as
   UTC by the spec, which renders as the day before for anyone west of Greenwich.
   Same reason weekdayLetter builds its Date the long way. */
function dayTitle(day) {
	var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day || ''));
	if (!m) return String(day || '');
	var d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
	if (isNaN(d.getTime())) return String(day);
	try {
		return d.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
	} catch (e) { return d.toDateString(); }
}

/* The day's events, in the timeline's row format: time, session tag, text. The
   two lists are deliberately the same object on glass — one is today from the
   live document and the other is a past day from the persisted history, and a
   person reading the second should not have to learn a second layout.

   The contract's event is { ts, kind, sessionId, title }, so `kind` is the text
   column and `title` is the tag: the title is the session's title AT THE TIME,
   which is the only thing that makes a row from four days ago legible. */
function syncDaySheet() {
	var doc = dayDoc;
	if (!doc) return;
	/* Ahead of the signature gate: the arrows depend on the wall clock as well as
	   on the day being read, so a panel left open across midnight has to re-disable
	   next without anything in the document having moved. */
	updateDayNav(doc.day);
	var use24 = use24Clock();
	var events = Array.isArray(doc.events) ? doc.events : [];
	var rows = [];
	for (var i = 0; i < events.length; i++) {
		var ev = events[i];
		if (!ev || typeof ev !== 'object') continue;
		var at = Date.parse(ev.ts);
		if (!isFinite(at)) continue;
		rows.push({
			at: at,
			tag: shortTitle(ev.title),
			text: ev.kind ? String(ev.kind) : 'event'
		});
	}
	/* Newest first, by contract — sorted anyway rather than trusted, because the
	   whole list is one screen and the cost of being sure is nothing. */
	rows.sort(function (a, b) { return b.at - a.at; });

	var total = rows.length;
	var hidden = Math.max(0, total - DAY_ROWS_MAX);
	rows = rows.slice(0, DAY_ROWS_MAX);

	/* MEASURED off crabd (companion/crabd.py _do_history, 2026-08-26): `count` is
	   the length of what crabd RETURNED, not the day's total, and `truncated`
	   says more exist beyond it — the pair is self-consistent and the widget is
	   never asked to reconcile a count against a shorter list. So there are two
	   caps stacked here and each says so in its own words: crabd's 200 becomes
	   "(truncated)" on this line, and this view's own DAY_ROWS_MAX becomes the
	   "+N earlier" row at the foot of the list.
	   typeof, not Number(): a count crabd could not produce is an em-dash, never a
	   zero — the same rule the by-model split and the week strip follow. */
	var count = typeof doc.count === 'number' && isFinite(doc.count) ? doc.count : null;
	var truncated = doc.truncated === true;
	var foot = (count === null ? EMDASH : String(count)) + (count === 1 ? ' event' : ' events') +
		(truncated ? ' (truncated)' : '');

	/* The SLOT is part of the signature since v0.15.0. The row cap is measured off
	   the real box (fitDayRows below), so a panel that changes size with the same
	   document open has a different answer for the same input — and without this
	   the signature would say "nothing moved" and keep the other slot's fit. The
	   resize listener already re-renders; this is what makes the re-render do
	   something. */
	/* todayKey() is in the signature for the same reason updateDayNav runs ahead of
	   it: the subtitle's "today" depends on the wall clock as well as on the
	   document, so a panel left open across midnight has to stop calling yesterday
	   today without anything in the document having moved. */
	var sig = (use24 ? '24' : '12') + '#' + doc.day + '#' + todayKey() + '#' + foot + '#' + hidden + '#' +
		window.innerWidth + 'x' + window.innerHeight + '#' +
		rows.map(function (r) { return r.at + ':' + r.tag + ':' + r.text; }).join('|');
	if (sig === daySig) return;
	daySig = sig;

	setText(ui.sheetTitle, dayTitle(doc.day));
	/* Today is named as today (v0.19.0). The same view now arrives two ways — a
	   week-strip column, or the header's History chip — and on the chip's route
	   the date alone leaves the person to work out whether they are looking at the
	   day they are standing in. The word is added, the date is not removed. */
	setText(ui.sheetRepo, (doc.day === todayKey() ? 'today ' + EMDASH + ' ' : '') +
		'history ' + EMDASH + ' newest first');
	setText(ui.sheetDayFoot, foot);

	ui.sheetTimeline.textContent = '';
	if (!rows.length) {
		var none = document.createElement('div');
		none.className = 'tl-empty';
		/* An empty day is a fact, not a failure: crabd answers 200 with no events
		   for a day it has no history for, and the contract is explicit that the
		   absence of history is not an error. */
		none.textContent = 'No events recorded for this day.';
		ui.sheetTimeline.appendChild(none);
		return;
	}
	for (var r = 0; r < rows.length; r++) {
		var row = document.createElement('div');
		row.className = 'tl-row';
		var time = document.createElement('span');
		time.className = 'tl-time';
		time.textContent = fmtTimeOfDay(new Date(rows[r].at), use24);
		var tagEl = document.createElement('span');
		tagEl.className = 'tl-session';
		tagEl.textContent = rows[r].tag;
		var text = document.createElement('span');
		text.className = 'tl-text';
		text.textContent = rows[r].text;
		row.appendChild(time);
		row.appendChild(tagEl);
		row.appendChild(text);
		ui.sheetTimeline.appendChild(row);
	}
	fitDayRows(total);
}

/* Trim the day list until nothing overflows, then write the "+N earlier" tail
   (v0.15.0). The cap USED to be a constant measured at 2560x720, and at 840x344
   the same eighteen rows plus the tail were 234 px of list in a 216 px box: the
   rows scrolled and the tail — the one line that admits the rows exist — went
   out of the panel with them. The two slots do not scale together, because
   --touch-min has a hard 48 px floor: at the small slot the sheet head's
   controls take 48 px where proportionally they would take 29, and the list is
   what pays the difference. A constant cannot be a function of that.

   So the fit is MEASURED. The list is already full when this runs, and
   `scrollHeight > clientHeight` is the browser's own answer to "does this
   overflow" — rows come off the end until it says no. That makes the cap a
   function of the real box at the real slot with the real font metrics, which
   are the three things the constant kept being wrong about, and it stays right
   for anything later added to this view's footer.

   The tail is appended BEFORE the loop and measured with the rows, because the
   tail is a line too: fitting the rows and then pushing the tail out is the same
   bug one line smaller.

   Bounded by construction — the loop only removes, it stops at DAY_ROWS_MIN, and
   it runs on a tap or a resize, never on the 3 s poll (daySig gates it). */
function fitDayRows(total) {
	var list = ui.sheetTimeline;
	var shown = list.querySelectorAll('.tl-row').length;
	if (!shown) return;

	var more = document.createElement('div');
	more.className = 'tl-empty';
	list.appendChild(more);

	while (true) {
		var hidden = Math.max(0, total - shown);
		/* The tail's TEXT is emptied when nothing is hidden, but the element stays
		   in the list while the loop runs: taking it out would measure a box the
		   finished list does not have, and the next trim would only put it back. */
		more.textContent = hidden > 0 ? '+' + hidden + ' earlier' : '';
		more.style.display = hidden > 0 ? '' : 'none';
		if (list.scrollHeight <= list.clientHeight) break;
		if (shown <= DAY_ROWS_MIN) break;
		var rowEls = list.querySelectorAll('.tl-row');
		if (!rowEls.length) break;
		list.removeChild(rowEls[rowEls.length - 1]);
		shown--;
	}
	if (!more.textContent) list.removeChild(more);
}

function setSheetStatus(text, kind) {
	setText(ui.sheetStatus, text);
	ui.sheetStatus.className = 'sheet-status' + (text ? ' shown ' + kind : '');
}

function onSheetAction(action, text) {
	/* Belt to the routing braces above: an unknown or missing action never reaches
	   the network. The widget's writes are a closed set, and a button that forgot
	   its data-sheet-action must be inert, not a malformed POST. */
	if (action !== 'ack' && action !== 'reply') return;
	if (!sheetSessionId || sheetBusy) return;
	var id = sheetSessionId;
	var s = findSession(id);
	/* SCA-006: the surface this receipt may be written to, captured BEFORE the
	   request leaves. */
	var token = actionSurface(id);

	sheetBusy = true;
	ui.sheet.classList.add('busy');

	if (action === 'ack') {
		/* Optimistic on purpose: the glow is the thing the person walked over to
		   silence, so it dies on the tap, not on the round trip. A failed POST
		   below rolls it back and says so. */
		ackOptimistic[id] = String((s && s.stateSince) || '');
		setSheetStatus('acknowledged', 'ok');
		fireSnap();
		render();
	} else {
		setSheetStatus('sending ' + EMDASH + ' ' + text, 'pending');
	}

	postAction(id, action, text).then(function (res) {
		var ours = surfaceStillOurs(token);
		/* The BUSY latch belongs to the surface too. Clearing it from a stale receipt
		   would unlock a sheet that has its own action in flight. */
		if (ours) { sheetBusy = false; ui.sheet.classList.remove('busy'); }
		if (action === 'ack') {
			if (res.status === 204 || res.status === 200) {
				if (ours) { setSheetStatus('acknowledged', 'ok'); scheduleClose(token); }
				return;
			}
			/* The optimistic ack is SESSION state, not surface state: it is rolled
			   back and re-rendered whatever is on the glass, because the alternative
			   is a card left silenced by a write that never landed. */
			delete ackOptimistic[id];
			render();
			if (ours) setSheetStatus('could not acknowledge (HTTP ' + res.status + ')', 'err');
			return;
		}
		if (!ours) return;
		/* 501 is the contract's "reply-injection is not proven yet" answer. It is
		   the expected state today, not a fault: muted text, sheet stays usable. */
		if (res.status === 501) { setSheetStatus('replies not available yet', 'note'); return; }
		if (res.status === 204 || res.status === 200) { setSheetStatus('sent: ' + text, 'ok'); scheduleClose(token); return; }
		if (res.status === 404) { setSheetStatus('crabd no longer knows this session', 'err'); return; }
		setSheetStatus('reply failed (HTTP ' + res.status + ')', 'err');
	}).catch(function () {
		var ours = surfaceStillOurs(token);
		if (ours) { sheetBusy = false; ui.sheet.classList.remove('busy'); }
		if (action === 'ack') { delete ackOptimistic[id]; render(); }
		if (ours) setSheetStatus('crabd not reachable', 'err');
	});
}

/* Tap-to-continue (v0.12.0). Optimistic confirmation on the tap: the queued item
   is what the person walked over to arrange, so it reads "queued: <label>" at
   once. A 404/400/older-crabd answer renders "not available" inline and does NOT
   latch — the next tap tries again, because crabd redeploys under a live widget.
   The wire prompt is the FULL instruction; the label is the short button face. */
function onSheetContinue(prompt, label) {
	/* lane C: the Detail page's continue buttons are THESE buttons and reach this
	   one implementation; when no sheet is open the target is the page's session. */
	var id = laneCActionSessionId();
	if (!id || !prompt) return;
	/* SCA-006: this line is mirrored onto the Detail page as well as the sheet, so
	   both surfaces are in the token. */
	var token = actionSurface(id);
	setContinueStatus('queued: ' + label, 'ok');
	postAction(id, 'queue-continue', prompt).then(function (res) {
		if (!surfaceStillOurs(token)) return;
		if (res.status === 204 || res.status === 200) { setContinueStatus('queued: ' + label, 'ok'); return; }
		/* 404 (no endpoint), 400 (older crabd does not know this action), or any
		   other non-2xx: not available on this crabd. No latch. */
		setContinueStatus('not available', 'note');
	}).catch(function () {
		if (!surfaceStillOurs(token)) return;
		setContinueStatus('crabd not reachable', 'err');
	});
}

/* MF-002 — CANCEL A QUEUED CONTINUE. The queue is a promise about what happens
   when the session next stops, and until now the only way out of one was to let it
   fire. Three answers, and each is a different fact rather than three flavours of
   failure: 204 removed it, 409 says it had already been delivered and when, 404
   says there was nothing queued - which is what an operator sees when the session
   picked it up between the paint and the fingertip.
   It is NOT optimistic. A queued prompt that disappeared from the line and then
   turned out to have been delivered would be the panel telling the operator the
   session is idle when it is about to run. The line clears when the feed says so. */
function onCancelContinue() {
	var id = laneCActionSessionId();
	if (!id) return;
	var token = actionSurface(id);
	setContinueStatus('cancelling', 'pending');
	postAction(id, 'cancel-continue').then(function (res) {
		if (!surfaceStillOurs(token)) return;
		if (res.status === 204 || res.status === 200) { setContinueStatus('cancelled', 'ok'); return; }
		if (res.status === 409) {
			var at = res.body && typeof res.body.deliveredAt === 'string' ? Date.parse(res.body.deliveredAt) : NaN;
			setContinueStatus(isFinite(at)
				? 'already sent at ' + fmtTimeOfDay(new Date(at), use24Clock())
				: 'already sent', 'note');
			return;
		}
		if (res.status === 404) { setContinueStatus('nothing queued', 'note'); return; }
		setContinueStatus('not available', 'note');
	}).catch(function () {
		if (!surfaceStillOurs(token)) return;
		setContinueStatus('crabd not reachable', 'err');
	});
}

/* Panel approval decision (v0.12.0). Optimistic CLOSE: a permission is decided
   with one deliberate tap and the sheet gets out of the way immediately, exactly
   as the ack drops the glow on the tap. The decision goes on the wire behind the
   close.

   v0.20.0 (CD-13) — A FAILURE IS NOW SAID OUT LOUD. The close stays optimistic;
   what changed is that "logged, not surfaced" was the panel presenting a write
   that never happened as a completed one. Reproduced with a forced 400: the
   sheet shut, the card kept its permission, and the only trace anywhere was a
   console line on a display with no console. The sheet is gone by then, so the
   surface is the notice line — the same place the two-finger ack reports itself,
   and the reason that line exists. The wording sends the operator where the
   decision can still be made: crabd holds the hook ~55 s and then hands the
   request back to the terminal dialog, which was always the fallback. */
function onSheetDecide(decision) {
	/* lane C: the Detail page's Approve and Deny are THESE controls and reach this
	   one implementation — the pairing code, the requestId echo and the
	   403/409/429 wording are inherited rather than re-typed. The sheet wins when
	   one is open, because it is a modal over that page. */
	var id = laneCActionSessionId();
	if (!id) return;
	if (decision !== DECIDE_ALLOW && decision !== DECIDE_DENY) return;
	/* v0.27.0: not paired = nothing goes on the wire and the sheet STAYS OPEN, so the
	   operator reads why instead of finding the card still armed after a close. */
	/* MF-017: the companion's own verdict outranks the local guess. `unverified`,
	   `no-token` and `off` each get their own repair sentence; a companion that does
	   not serve readiness falls back to the local test, which is what shipped. */
	var ready = approvalReadiness();
	if (ready !== null && ready !== 'ready') {
		showNotice(approvalReadyText(ready), 'err');
		logLine('decide refused locally: readiness is ' + ready);
		return;
	}
	if (ready === null && tokenRequired() && !pairingCode()) {
		showNotice('not paired ' + EMDASH + ' the panel host holds no pairing code', 'err');
		logLine('decide refused locally: no pairing code');
		return;
	}
	/* WID-a: the id of the request the sheet is SHOWING, echoed so crabd can refuse a
	   tap that lands on a request that replaced it in the poll gap. */
	var live = findSession(id);
	var pend = live && live.pendingPermission && typeof live.pendingPermission === 'object' ? live.pendingPermission : null;
	var requestId = pend && typeof pend.requestId === 'string' ? pend.requestId : null;
	fireSnap();
	closeSheet();
	postAction(id, 'decide', null, decision, null, requestId).then(function (res) {
		if (res.status === 204 || res.status === 200) { logLine('decision sent: ' + decision); return; }
		logLine('decide failed (HTTP ' + res.status + ')');
		if (res.status === 403) { showNotice(decision + ' refused ' + EMDASH + ' pairing code wrong; check widget settings', 'err'); return; }
		if (res.status === 409) { showNotice(decision + ' not applied ' + EMDASH + ' the request changed; reopen the card', 'err'); return; }
		if (res.status === 429) { showNotice(decision + ' refused ' + EMDASH + ' pairing locked, wait a minute', 'err'); return; }
		showNotice(decision + ' not sent ' + EMDASH + ' decide in terminal', 'err');
	}).catch(function () {
		logLine('decide failed: crabd not reachable');
		showNotice(decision + ' not sent ' + EMDASH + ' crabd not reachable', 'err');
	});
}

/* The widget's only write. Mock mode never leaves the page.

   Content-type trap: application/json makes this a CORS *preflighted* request,
   so it dies at the OPTIONS if crabd only answers GET/POST. text/plain is a
   CORS-simple request and needs no preflight. We try the contract-correct
   header first and fall back once, then remember which one worked — a network
   TypeError (not an HTTP status) is exactly what a missing preflight looks
   like from here. */
function postAction(sessionId, action, text, decision, quiet, requestId) {
	/* ack-all is panel-wide by contract and carries no session: sending a
	   sessionId with it would invite a crabd that reads the first field it
	   recognises to ack exactly one of them. `quiet` (v0.22.0) is panel-wide for
	   the same reason and carries none either — an override is a statement about
	   the PANEL, and a session id on it would be a field inviting a reading. */
	var body = (action === 'ack-all' || action === 'quiet')
		? { action: action } : { sessionId: sessionId, action: action };
	if (action === 'reply') body.text = text;
	/* queue-continue carries the FULL prompt; decide carries allow/deny. Each is a
	   closed field the wire body names explicitly, never a free-form passthrough. */
	else if (action === 'queue-continue') body.prompt = text;
	else if (action === 'decide') {
		body.decision = decision;
		/* v0.27.0: the pairing code and the request id are what make a decide
		   un-forgeable from a web page (crabd 0.29.0, SEC-a / WID-a). Sent whenever
		   present; an older crabd ignores unknown keys. */
		var tok = pairingCode();
		if (tok) body.token = tok;
		if (requestId) body.requestId = requestId;
	}
	else if (action === 'quiet') { body.mode = quiet.mode; body.minutes = quiet.minutes; }
	var payload = JSON.stringify(body);

	if (mockName) {
		return new Promise(function (resolve) {
			setTimeout(function () {
				var status = mockActionStatus(action);
				logLine('mock POST /v1/action ' + payload);
				/* The harness stands in for the DAEMON, never for the widget. An
				   accepted quiet write therefore changes what the mock feed SERVES
				   from the next poll on, so the chip settles on a document exactly
				   as it does against crabd — including the panel actually dimming,
				   because crabd's effective `active` honours the override and so
				   does this. Without it the optimistic answer would be dropped by
				   the very next poll and the tap cycle could not be photographed
				   past its first frame. */
				if (action === 'quiet' && (status === 204 || status === 200)) applyMockQuietWrite(quiet);
				resolve({ status: status, body: null });
			}, 140);
		});
	}
	return postJson('/v1/action', payload);
}

/* Mock-only status for POST /v1/action. reply is 501 (the contract's
   "not proven yet"); queue-continue and decide are 204 (accepted) unless the
   older-crabd 400 is being demoed — either via the dev flag &action400=1 or a
   fixture's own `_mock.action400` list. ack / ack-all stay 204. */
function mockActionStatus(action) {
	if (action === 'reply') return 501;
	/* v0.22.0: `quiet` joins the two actions whose older-crabd 400 is demoable,
	   because that 400 is the one that LATCHES the chip away and a capability latch
	   nobody can reach off-glass is a latch nobody has watched fire. */
	/* MF-002: cancel-continue is 204 in the harness. Its 409 and 404 are demoed
	   through the same &action400 route the other write actions use, because the
	   two answers that matter are the ones a fixture cannot reach by accident. */
	if (action === 'queue-continue' || action === 'decide' || action === 'quiet' ||
		action === 'cancel-continue') {
		if (actionForce400) return 400;
		var stub = lastGoodDoc && lastGoodDoc._mock ? lastGoodDoc._mock.action400 : null;
		if (Array.isArray(stub) && stub.indexOf(action) !== -1) return 400;
	}
	return 204;
}

/* quietHours, toast and budget are the ONLY keys writable over HTTP (contract),
   and syncConfigKey is the only caller — nothing else in the widget may POST
   /v1/config, and allowReply must never become settable from here. */
function postConfig(payload) {
	if (mockName) {
		return new Promise(function (resolve) {
			setTimeout(function () {
				var status = mockConfigStatus(payload);
				logLine('mock POST /v1/config ' + payload + ' -> ' + status);
				resolve({ status: status });
			}, 140);
		});
	}
	return postJson('/v1/config', payload);
}

function postJson(path, payload) {
	var url = baseUrl() + path;
	return send(actionContentType).catch(function (err) {
		if (actionContentType !== 'application/json') throw err;
		actionContentType = 'text/plain;charset=UTF-8';
		return send(actionContentType);
	});

	function send(ctype) {
		var opts = { method: 'POST', headers: { 'Content-Type': ctype }, body: payload, cache: 'no-store' };
		var ctl = null, timer = null;
		if (typeof AbortController !== 'undefined') {
			ctl = new AbortController();
			opts.signal = ctl.signal;
			timer = setTimeout(function () { try { ctl.abort(); } catch (e) {} }, ACTION_TIMEOUT_MS);
		}
		return fetch(url, opts).then(function (r) {
			if (timer) clearTimeout(timer);
			/* THE BODY MATTERS NOW, and it did not before: /v1/config answers
			   {applied, warnings}, a 409 on cancel-continue carries the instant the
			   prompt was delivered, and /v1/approvals/verify explains a 403. Read
			   defensively - an absent body, or one that is not JSON, is null, and
			   every caller treats null as "no detail" rather than as a failure. */
			return r.text().then(function (t) {
				var body = null;
				if (t) { try { body = JSON.parse(t); } catch (e) { body = null; } }
				return { status: r.status, body: body };
			}, function () { return { status: r.status, body: null }; });
		}, function (e) {
			if (timer) clearTimeout(timer);
			throw e;
		});
	}
}

function onCardsClick(ev) {
	var card = ev.target && ev.target.closest ? ev.target.closest('.card') : null;
	if (!card || !card.classList.contains('tappable')) return;
	/* The overflow tile is matched on its own attribute and RETURNS, above the
	   session branch (v0.20.0, CD-14) — the same rule Dismiss and Pin keep in
	   onSheetClick: it wears .card for its looks and carries no session id, so
	   falling through would call openSheet(null). */
	if (card.getAttribute('data-overflow') === '1') { openOverflowSheet(); return; }
	openSheet(card.getAttribute('data-session-id'));
}

/* The sessions header (v0.15.0). It carries three targets now: the line itself
   still opens the Today timeline, and the two chips do not. Chips first, and
   they RETURN — a chip tap that fell through would cycle the filter and open a
   sheet over the result, which is the whole reason this handler exists. */
function onGridHeadClick(ev) {
	var el = ev.target && ev.target.closest ? ev.target.closest('.head-chip') : null;
	if (el === ui.filterChip) { cycleFilter(); return; }
	if (el === ui.densityChip) { cycleDensity(); return; }
	/* v0.19.0. Same rule as the two above and for the same reason: a chip tap that
	   fell through would open the ring-buffer timeline over the day view this one
	   is about to fetch, and the loser would land last. */
	if (el === ui.historyChip) { openTodayHistory(); return; }
	if (el) return;
	openTimelineSheet();
}

/* A gauge tap opens that window's forecast detail (v0.19.0). Bound to the two
   fixed gauges directly and to the extras' CONTAINER, which persists while its
   children are rebuilt whenever the label set changes — a listener on a row
   would go with the row.
   data-win is read off the closest .gauge rather than off the event target, so a
   tap on the percentage, the track or the reset countdown all reach the same
   window; the whole gauge is the target, not the parts of it. */
function onGaugeClick(ev) {
	var t = ev.target;
	var g = t && t.closest ? t.closest('.gauge') : null;
	if (!g) return;
	openForecastSheet(g.getAttribute('data-win'));
}

/* ===================================================== touch gestures (v0.14.0)

   Four gestures on one pointer stream: swipe a done/idle card away, press and
   hold a card to pin it, tap with two fingers anywhere to acknowledge everything
   waiting, and pull down from the top edge to force a refresh.

   THE CLICK IS STILL THE TAP. Nothing below replaces the existing click handlers
   — a tap on a card opens its sheet through onCardsClick exactly as it did in
   v0.13.0, and every control in the sheet is still a click. What this layer adds
   is the ability to SWALLOW that click when a gesture has already consumed the
   interaction, which is done once, in the capture phase, on the document
   (onClickCapture): a per-handler guard would have to be added to every control
   the panel ever grows, and the one somebody forgot would be the one that fired
   a sheet open under a swiped card.

   Touch-first, and that is a design constraint rather than a preference: there is
   no hover on this glass, so no gesture may depend on one and none of them has a
   discoverability story that starts with a cursor. pointerdown/move/up are used
   rather than touch events so a mouse in a dev browser drives the same code path
   the fingertip does — which is what makes the gestures testable at all.
   The listeners are PASSIVE: nothing here calls preventDefault, because the
   axis each gesture wants is claimed declaratively in CSS (touch-action) where
   the compositor can honour it without waiting on a handler. */

/* The card a pointer landed on, or null. The sheet is excluded outright: it is a
   modal, its own controls are clicks, and a card underneath it cannot be reached
   by a finger anyway. */
function gestureCard(t) {
	if (!t || !t.closest) return null;
	if (t.closest('#sheet')) return null;
	return t.closest('.card.tappable');
}

function inSheet(t) { return !!(t && t.closest && t.closest('#sheet')); }

/* A swipe holds the card DOM under a finger, so the card grid must not be rebuilt
   while one is running — see renderSessions, which defers instead. */
function gestureHoldsCards() { return swipe !== null; }

function suppressClick() { suppressClickUntil = Date.now() + SUPPRESS_CLICK_MS; }

/* DERIVED from the map, never tracked alongside it. A separate counter drifts the
   moment an up or a cancel is not delivered — a lost pointer on a window blur, a
   touch that leaves the digitizer — and a counter stuck at 2 would make every
   single-pointer gesture return early for the rest of the panel's uptime, on a
   display that runs for weeks. The map is the one source of truth and it is
   emptied by the same events that would have decremented the counter. */
function livePointers() {
	var n = 0;
	for (var id in pointers) { if (Object.prototype.hasOwnProperty.call(pointers, id)) n++; }
	return n;
}

function dropPointer(id) {
	if (pointers[id] === undefined) return null;
	var rec = pointers[id];
	delete pointers[id];
	return rec;
}

function onClickCapture(ev) {
	if (Date.now() >= suppressClickUntil) return;
	ev.stopPropagation();
	ev.preventDefault();
}

function onPointerDown(ev) {
	var rec = {
		id: ev.pointerId,
		x0: ev.clientX, y0: ev.clientY,
		t0: Date.now(),
		moved: 0,
		claimed: false,
		card: null,
		pull: false
	};
	/* A gesture that began with a pointer the browser never finished must not still
	   be armed under the next one: an empty map is a clean slate. */
	if (livePointers() === 0) multi = null;
	pointers[ev.pointerId] = rec;
	var live = livePointers();

	/* A SECOND finger cancels every single-pointer gesture outright. Two fingers is
	   a different intention, and a swipe left running under one would dismiss the
	   card the person was trying to acknowledge past. */
	if (live === 2) {
		endSwipe(false);
		cancelLongPress();
		endPull(false);
		multi = { t0: Date.now(), dead: false };
		/* A finger that had ALREADY travelled before its partner landed makes this a
		   drag with a second finger on it, not a two-finger tap. Seeded from the live
		   records rather than waiting for the next move, which may never come. */
		for (var pid in pointers) {
			if (Object.prototype.hasOwnProperty.call(pointers, pid) && pointers[pid].moved > MULTI_SLOP_PX) multi.dead = true;
		}
		return;
	}
	/* Three fingers is a palm. */
	if (live > 2) { if (multi) multi.dead = true; return; }

	/* The quiet chip's long press (v0.22.0), armed ABOVE the card branch because the
	   chip is not a card and would otherwise fall through to the pull zone — it sits
	   in the clock row, which at some slots is inside PULL_ZONE_PX of the top edge.
	   Same idiom as the card's press-and-hold, same timer, same cancellation: any
	   travel past TAP_SLOP_PX in onPointerMove ends it, so a hold and a drag cannot
	   both fire on one finger. */
	if (ev.target && ev.target.closest && ev.target.closest('#moonChip')) {
		cancelLongPress();
		longPressTimer = setTimeout(function () {
			longPressTimer = null;
			fireMoonAuto();
		}, LONGPRESS_MS);
		return;
	}

	var card = gestureCard(ev.target);
	if (card) {
		rec.card = card;
		/* Long-press arms on EVERY card, not only the dismissable ones: pinning is
		   not state-gated anywhere else either — the sheet offers Pin on both of its
		   session modes — and a hold that worked on four cards out of six would read
		   as a broken gesture rather than a scoped one. */
		cancelLongPress();
		longPressTimer = setTimeout(function () {
			longPressTimer = null;
			firePin(card);
		}, LONGPRESS_MS);
		return;
	}
	/* The pull. Armed only in the top strip and never over the sheet: a modal is
	   not a thing you pull down, and the sheet's own regions scroll. */
	if (ev.clientY <= PULL_ZONE_PX && !inSheet(ev.target)) rec.pull = true;
}

function onPointerMove(ev) {
	var rec = pointers[ev.pointerId];
	if (!rec) return;
	var dx = ev.clientX - rec.x0;
	var dy = ev.clientY - rec.y0;
	var dist = Math.max(Math.abs(dx), Math.abs(dy));
	if (dist > rec.moved) rec.moved = dist;

	if (multi) { if (rec.moved > MULTI_SLOP_PX) multi.dead = true; return; }
	if (livePointers() > 1) return;

	/* Hold-still is the entire long press, so any real travel ends it — which is
	   also what keeps a swipe and a long press from both firing on one finger. */
	if (rec.moved > TAP_SLOP_PX) cancelLongPress();

	if (swipe && swipe.id === ev.pointerId) { moveSwipe(dx); return; }
	if (pull && pull.id === ev.pointerId) { movePull(dy); return; }
	if (rec.claimed) return;

	/* Axis discrimination, decided once. Horizontal AND past the arm distance is a
	   swipe; downward AND past its own arm distance, from the top strip, is a pull. */
	if (rec.card && Math.abs(dx) >= SWIPE_ARM_PX && Math.abs(dx) > Math.abs(dy)) {
		rec.claimed = true;
		if (startSwipe(rec)) moveSwipe(dx);
		return;
	}
	if (rec.pull && dy >= PULL_ARM_PX && dy > Math.abs(dx)) {
		rec.claimed = true;
		pull = { id: ev.pointerId, dy: 0, armed: null };
		movePull(dy);
	}
}

function onPointerUp(ev) {
	var rec = dropPointer(ev.pointerId);
	if (!rec) return;
	cancelLongPress();

	/* A DRAG IS NEVER A TAP, whatever it did or did not do. This is the line that
	   makes a horizontal drag on a working card a true no-op: the card cannot be
	   dismissed, and the sheet it would otherwise open does not open either. */
	if (rec.moved > TAP_SLOP_PX) suppressClick();

	if (multi) {
		/* Resolved on the LAST finger up, so a palm has already been marked dead and
		   a slow drag has already failed the slop test. */
		if (livePointers() > 0) return;
		var ok = !multi.dead && (Date.now() - multi.t0) <= MULTI_TAP_MS;
		multi = null;
		if (ok) { suppressClick(); fireTwoFingerAck(); }
		return;
	}
	if (swipe && swipe.id === ev.pointerId) { endSwipe(true); return; }
	if (pull && pull.id === ev.pointerId) { endPull(true); return; }
}

/* A cancel is the browser taking the pointer away (the compositor started a
   scroll, the window lost focus, the touch left the digitizer). It ends every
   gesture WITHOUT committing: a card mid-swipe snaps back and a pull is dropped,
   because a gesture nobody finished is not a gesture anybody meant. */
function onPointerCancel(ev) {
	dropPointer(ev.pointerId);
	cancelLongPress();
	if (multi) { multi.dead = true; if (livePointers() === 0) multi = null; }
	if (swipe && swipe.id === ev.pointerId) endSwipe(false);
	if (pull && pull.id === ev.pointerId) endPull(false);
}

/* ------------------------------------------------------- swipe to dismiss */

function startSwipe(rec) {
	var card = rec.card;
	if (!card || !card.isConnected) return false;
	var s = findSession(card.getAttribute('data-session-id'));
	/* Checked against the LIVE row rather than the card's data-state, which is one
	   render old. A needs_input or working card has no dismissal for the gesture to
	   BE, so it simply does not move — the finger travels and nothing happens,
	   which is the honest rendering of "there is nothing here to swipe away". */
	if (!s || !DISMISSABLE[s.state]) return false;
	swipe = {
		id: rec.id,
		card: card,
		dx: 0,
		/* The done and idle cards are already dimmed by the stylesheet (0.55 and
		   0.75), so the fade has to MULTIPLY that base rather than replace it —
		   writing an absolute opacity would make a done card jump brighter the
		   instant it was touched. Read once, here, not per frame. */
		base: Number(getComputedStyle(card).opacity) || 1
	};
	card.classList.add('swiping');
	return true;
}

function moveSwipe(dx) {
	if (!swipe) return;
	swipe.dx = dx;
	paintSwipe(swipe.card, dx, swipe.base);
}

function paintSwipe(card, dx, base) {
	card.style.transform = 'translateX(' + Math.round(dx) + 'px)';
	/* The fade saturates AT the threshold and goes no further: a card that had
	   already vanished would be promising an outcome the release can still take
	   back, and under the threshold the release does take it back. */
	var f = Math.min(1, Math.abs(dx) / SWIPE_DISMISS_PX);
	card.style.opacity = String(base * (1 - SWIPE_FADE * f));
	card.classList.toggle('swipe-armed', Math.abs(dx) >= SWIPE_DISMISS_PX);
}

function endSwipe(release) {
	if (!swipe) return;
	var card = swipe.card;
	var dx = swipe.dx;
	var go = release && Math.abs(dx) >= SWIPE_DISMISS_PX;
	swipe = null;
	card.classList.remove('swipe-armed', 'swiping');
	if (!go) {
		card.classList.add('swipe-settle');
		card.style.transform = '';
		card.style.opacity = '';
		setTimeout(function () { card.classList.remove('swipe-settle'); }, SWIPE_FLY_MS + 40);
		/* The rebuild renderSessions deferred while the finger was down. */
		render();
		return;
	}
	dismissSwiped(card, dx);
}

function dismissSwiped(card, dx) {
	var s = findSession(card.getAttribute('data-session-id'));
	/* The same belt onSheetDismiss wears, for the same reason: a row that went
	   idle -> working during the drag must not be hidden. */
	if (!s || !DISMISSABLE[s.state]) {
		card.style.transform = '';
		card.style.opacity = '';
		render();
		return;
	}
	dismissed[s.id] = String(s.stateSince || '');
	if (reducedMotion()) { render(); return; }
	card.classList.add('swipe-settle');
	card.style.transform = 'translateX(' + (dx < 0 ? '-120%' : '120%') + ')';
	card.style.opacity = '0';
	setTimeout(render, SWIPE_FLY_MS);
}

/* ------------------------------------------------- long press to pin/unpin */

function cancelLongPress() {
	if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
}

function firePin(card) {
	if (!card.isConnected) return;
	var id = card.getAttribute('data-session-id');
	if (!id) return;
	/* The hold has consumed the interaction: the sheet must not also open when the
	   finger comes up, and the finger has not come up yet. */
	suppressClick();
	togglePin(id);
	firePinFlash(id, isPinned(id));
}

/* The confirm. On a PIN the glyph animates in, which is the whole message; on an
   UNPIN there is no glyph left to animate, so the card keeps drawing one for the
   length of the flash and animates it OUT. That is why the flash is render state
   rather than a class poked onto a node: buildCard has to know to draw a pin on a
   session that no longer has one. */
function firePinFlash(id, on) {
	if (pinFlashTimer) { clearTimeout(pinFlashTimer); pinFlashTimer = null; }
	pinFlashId = String(id);
	pinFlashOn = !!on;
	render();
	if (pinFlashHold) return;
	pinFlashTimer = setTimeout(function () {
		pinFlashTimer = null;
		pinFlashId = null;
		render();
	}, PIN_FLASH_MS);
}

function pinFlashFor(id) {
	if (pinFlashId === null || pinFlashId !== String(id)) return '';
	return pinFlashOn ? 'on' : 'off';
}

/* ------------------------------------------------ two-finger tap: ack-all */

/* The same write the crab tap makes, reachable without aiming at the crab. The
   crab is the biggest target on the panel but it is still a PLACE; two fingers
   anywhere is the version of that control you can hit with your eyes on the
   cards. Silent when nothing waits, exactly as the crab tap is: a huge target
   that cannot cause a write by accident is the property both of them need. */
function fireTwoFingerAck() {
	var n = ackAllWaiting();
	if (!n) return;
	showNotice('acknowledged ' + n, 'ack');
}

/* ------------------------------------------------ pull down to force refresh */

function movePull(dy) {
	if (!pull) return;
	pull.dy = dy;
	var armed = dy >= PULL_REFRESH_PX;
	if (armed === pull.armed) return;
	pull.armed = armed;
	/* Held (no timer) for as long as the finger is down: this line is the state of
	   the gesture, not a receipt for it. */
	showNotice(armed ? 'release to refresh' : 'pull down to refresh', 'pull', true);
}

function endPull(release) {
	if (!pull) return;
	var go = release && pull.dy >= PULL_REFRESH_PX;
	pull = null;
	if (!go) { hideNotice(); return; }
	forceRefresh();
}

/* SCA-019. The gesture ALWAYS does something now. It used to call an unforced
   poll, which returned immediately whenever the stream was open - so on the one
   panel where this gesture matters, a pull did nothing at all and still said
   "refreshing".
   Spinner-free on purpose. A spinner would have to keep animating until something
   answered, which on a panel whose companion may simply be gone means an animation
   that never stops, and the stale banner is already the widget's honest account of
   that. This is a flash saying the refresh was asked for; what came back is the
   panel's own job to show. */
function forceRefresh() {
	showNotice('refreshing', 'pull');
	/* A stream that is OPEN but not delivering is restarted rather than polled
	   around: the transport is the thing that is wrong, and sseFellBack closes it,
	   polls once immediately and reconnects on the paced ladder. A HEALTHY stream is
	   left alone - tearing one down on every pull would cost a reconnect for
	   nothing - and gets a single forced GET instead. */
	if (sseSource && sseSource.readyState === 1 && sseSilent()) {
		sseFellBack('refresh restarted a silent stream');
		return;
	}
	poll(true);
}

/* ------------------------------------------------------- the notice line */

/* aria-hidden tracks the CLASS, in both directions (v0.20.0, CD-31). The element
   ships aria-hidden="true" so an empty status region is not announced at boot,
   and nothing ever set it back — so the one line on this panel that exists to
   confirm a gesture ("acknowledged 2", "refreshing") was live text with
   role="status" that no accessibility API could read, for the whole of its
   second and a half. Set beside every add/remove of notice-on so the two cannot
   drift; hideNotice is the mirror. */
function showNotice(text, kind, hold) {
	if (noticeTimer) { clearTimeout(noticeTimer); noticeTimer = null; }
	setText(ui.noticeText, text);
	if (ui.notice.getAttribute('data-kind') !== kind) ui.notice.setAttribute('data-kind', kind);
	document.body.classList.add('notice-on');
	ui.notice.setAttribute('aria-hidden', 'false');
	if (hold || noticeHold) return;
	noticeTimer = setTimeout(function () {
		noticeTimer = null;
		document.body.classList.remove('notice-on');
		ui.notice.setAttribute('aria-hidden', 'true');
	}, NOTICE_MS);
}

function hideNotice() {
	if (noticeHold) return;
	if (noticeTimer) { clearTimeout(noticeTimer); noticeTimer = null; }
	document.body.classList.remove('notice-on');
	ui.notice.setAttribute('aria-hidden', 'true');
}

/* ------------------------------------------------ keyboard (v0.20.0, CD-15) */

/* WHAT THIS IS AND WHAT IT DELIBERATELY IS NOT.

   This panel ships on a wall-mounted TOUCHSCREEN with no keyboard attached, and
   inventing a full keyboard UX for a surface that cannot exercise one would be
   shipping a second interaction model nobody can test. But the store checklist
   carries an accessibility row, and every QA pass this widget has ever had —
   QtWebEngine or a browser — was driven from a machine that does have a keyboard,
   where the panel was a set of divs with click handlers: nothing reachable by
   Tab, nothing activatable by Enter, and a sheet that could be opened and then
   only closed with a pointer.

   So: the cheap, high-value subset, and no more.
     - Escape closes the sheet. One key, the one everybody already presses.
     - Tab is trapped inside an open sheet, and the panel behind it goes
       aria-hidden — a modal that leaks focus to the controls it is covering is
       worse than no focus management, because the operator ends up driving a
       button they cannot see.
     - Enter / Space activate anything already carrying role="button". Native
       <button> elements do this themselves, so they are excluded here rather
       than being clicked twice.
   Recorded as deliberately skipped: arrow-key navigation of the card grid, any
   keyboard equivalent for the four gestures (swipe-dismiss, long-press pin,
   two-finger ack-all, pull-to-refresh), and aria-live narration of state
   changes. Each is a real feature, none is a defect this wave found, and all
   three would be shipped untested against the surface they are for. */
function onKeyDown(ev) {
	var key = ev.key;
	if (!key) return;
	var open = ui.sheet && ui.sheet.classList.contains('open');
	if (key === 'Escape' || key === 'Esc') {
		if (open) { closeSheet(); ev.preventDefault(); }
		return;
	}
	if (key === 'Tab' && open) { trapTab(ev); return; }
	if (key !== 'Enter' && key !== ' ' && key !== 'Spacebar') return;
	var el = document.activeElement;
	if (!el || el === document.body) return;
	/* Native buttons already fire a click for both keys; synthesising a second one
	   here is how a single press denies a permission twice. */
	if (el.tagName === 'BUTTON') return;
	/* A session card is a tab stop but deliberately NOT role="button": the role
	   flattens an element to its label, and a card is a title, a state, a model, a
	   question and a badge row — the one place on this panel where the content is
	   the point. So it is matched on the class instead and keeps its structure. */
	if (el.getAttribute('role') !== 'button' && !el.classList.contains('card')) return;
	/* Space scrolls the page by default, and the sheet's list regions scroll. */
	ev.preventDefault();
	el.click();
}

function focusablesIn(root) {
	if (!root) return [];
	var all = root.querySelectorAll('a[href], button, input, select, textarea, [tabindex]');
	var out = [];
	for (var i = 0; i < all.length; i++) {
		var e = all[i];
		if (e.disabled) continue;
		if (e.getAttribute('tabindex') === '-1') continue;
		var cs;
		try { cs = getComputedStyle(e); } catch (err) { continue; }
		if (cs.display === 'none' || cs.visibility === 'hidden') continue;
		out.push(e);
	}
	return out;
}

/* Wraps at both ends off the sheet panel's OWN focusables, recomputed on every
   press: the sheet is six modes sharing one panel and half its controls are
   display:none at any moment, so a list captured at open time would tab to a
   button that is not on the glass. */
function trapTab(ev) {
	var panel = ui.sheet.querySelector('.sheet-panel');
	var list = focusablesIn(panel);
	if (!list.length) { ev.preventDefault(); if (panel) safeFocus(panel); return; }
	var first = list[0], last = list[list.length - 1];
	var here = document.activeElement;
	if (!panel.contains(here)) { ev.preventDefault(); safeFocus(ev.shiftKey ? last : first); return; }
	if (ev.shiftKey && here === first) { ev.preventDefault(); safeFocus(last); return; }
	if (!ev.shiftKey && here === last) { ev.preventDefault(); safeFocus(first); }
}

function safeFocus(el) {
	if (!el || !el.focus) return;
	/* preventScroll keeps a focus call from scrolling a list region under a
	   fingertip. Passed as an options object, which an engine that does not know it
	   simply ignores — there is nothing to feature-detect and nothing to fall back
	   to. */
	try { el.focus({ preventScroll: true }); } catch (e) { try { el.focus(); } catch (e2) {} }
}

/* The panel behind an open sheet is hidden from the accessibility tree and the
   previously focused element is remembered, so closing puts the operator back
   where they were rather than at the top of the document. `inert` would be the
   right primitive and is not reliably present in QtWebEngine, so this is
   aria-hidden plus the Tab trap above — the same guarantee by two mechanisms
   that are both known to exist here. */
var sheetReturnFocus = null;

/* SCA-008 — THE DIALOG IS NAMED FOR THE MODE IT IS IN. One panel serves eight
   modes and the markup can carry only one name, so the fixed "Session actions" in
   index.html announced the settings sheet, the week drill and the hardware history
   under a name none of them has. */
var SHEET_LABELS = {
	session: 'Session actions',
	burn: "Today's burn",
	forecast: 'Usage window forecast',
	timeline: "Today's timeline",
	day: 'One day in full',
	overflow: 'The sessions the grid could not fit',
	host: "This PC's hardware history",
	settings: 'Panel settings'
};

function setSheetLabel() {
	var panel = ui.sheet.querySelector('.sheet-panel');
	if (!panel) return;
	var name = SHEET_LABELS[sheetMode] || 'SideCrab';
	if (panel.getAttribute('aria-label') !== name) panel.setAttribute('aria-label', name);
}

function enterSheetFocus() {
	/* Named BEFORE focus moves into it: a dialog focused and then renamed is one an
	   assistive technology has already announced under the old name. enterSheetFocus
	   is the one path every opener goes through, and the mode is fixed by the time
	   it runs. */
	setSheetLabel();
	var active = document.activeElement;
	if (active && active !== document.body && !ui.sheet.contains(active)) sheetReturnFocus = active;
	setBackgroundHidden(true);
	var panel = ui.sheet.querySelector('.sheet-panel');
	var list = focusablesIn(panel);
	safeFocus(list.length ? list[0] : panel);
}

function exitSheetFocus() {
	setBackgroundHidden(false);
	var back = sheetReturnFocus;
	sheetReturnFocus = null;
	/* Only if it is still in the document: a card is thrown away and rebuilt on
	   every signature change, so the node a sheet was opened from routinely no
	   longer exists by the time it closes. */
	if (back && document.body.contains(back)) safeFocus(back);
}

function setBackgroundHidden(hidden) {
	var ids = ['zones', 'banner', 'notice'];
	for (var i = 0; i < ids.length; i++) {
		var el = document.getElementById(ids[i]);
		if (!el) continue;
		/* The notice line owns its own aria-hidden (CD-31) and is only ever visible
		   for a second and a half; while a sheet is open it is behind the backdrop,
		   so it is hidden with the rest and handed back to showNotice after. */
		if (hidden) el.setAttribute('aria-hidden', 'true');
		else if (ids[i] === 'notice') el.setAttribute('aria-hidden',
			document.body.classList.contains('notice-on') ? 'false' : 'true');
		else el.removeAttribute('aria-hidden');
	}
}

function onSheetClick(ev) {
	var t = ev.target;
	if (!t) return;
	/* Dismiss is tested FIRST and deliberately: it wears .sheet-btn for its looks,
	   and the generic branch below would otherwise claim it and POST an action of
	   null to crabd (caught in the browser, 2026-08-26 — the malformed body was
	   already on the wire). Any future button that borrows .sheet-btn without a
	   data-sheet-action must be routed above this line too. */
	if (t.closest && t.closest('#sheetDismiss')) { onSheetDismiss(); return; }
	/* Same rule as Dismiss, and the same reason: Pin wears .sheet-btn for its
	   looks and carries no data-sheet-action, so the generic branch below would
	   POST an action of null to crabd. Route every borrowed-looks button here. */
	if (t.closest && t.closest('#sheetPin')) { onSheetPin(); return; }
	/* Back is the day view's only navigation: it returns to the timeline the day
	   was opened from, rather than closing the panel outright — the person came to
	   read a week and tapped one column of it. */
	if (t.closest && t.closest('#sheetBack')) { openTimelineSheet(); return; }
	/* Prev / next day. Matched on data-day-step, which is a different attribute
	   from the week strip's data-day below — an attribute selector is exact, so
	   the two branches cannot claim each other's targets. Above that branch all
	   the same, so the routing reads in the order the head does. */
	var step = t.closest ? t.closest('[data-day-step]') : null;
	if (step) { onDayStep(Number(step.getAttribute('data-day-step'))); return; }
	/* A day column in the week strip. Tapped anywhere in the column: the three
	   cells are one target, because a fingertip on a wall panel does not aim at a
	   row of digits. */
	var dayCell = t.closest ? t.closest('[data-day]') : null;
	if (dayCell) { openDaySheet(dayCell.getAttribute('data-day')); return; }
	/* An overflow row (v0.20.0, CD-14): the drill-in this sheet exists for. Same
	   rule as every branch above — matched on its own attribute and returning, so
	   it can never reach the generic .sheet-btn branch. openSheet re-reads the row
	   from the live feed and returns without opening if the session has gone. */
	var ovRow = t.closest ? t.closest('.ov-row[data-session-id]') : null;
	if (ovRow) { openSheet(ovRow.getAttribute('data-session-id')); return; }
	/* Approve / Deny (v0.12.0). Matched on data-decide ABOVE the generic .sheet-btn
	   branch, exactly as Dismiss and Pin are and for the same reason: these wear
	   .sheet-btn for their looks and carry no data-sheet-action, so the generic
	   branch would POST an action of null. */
	var decideBtn = t.closest ? t.closest('[data-decide]') : null;
	if (decideBtn) { onSheetDecide(decideBtn.getAttribute('data-decide')); return; }
	/* Tap-to-continue (v0.12.0). Same rule: a continue button carries the full
	   prompt on data-continue-prompt and no data-sheet-action. */
	/* MF-002, above the generic branch for the reason every branch here is: it
	   wears .sheet-btn and carries no data-sheet-action. */
	if (t.closest && t.closest('[data-cancel-continue]')) { onCancelContinue(); return; }
	var contBtn = t.closest ? t.closest('[data-continue-prompt]') : null;
	if (contBtn) { onSheetContinue(contBtn.getAttribute('data-continue-prompt'), contBtn.getAttribute('data-continue-label') || 'Continue'); return; }
	/* lane C: Full view. Routed above the generic branch, the rule Dismiss and Pin
	   keep, because it wears .sheet-btn for its looks and carries no
	   data-sheet-action. */
	if (t.closest && t.closest('[data-full-view]')) { laneCOpenDetail(sheetSessionId); return; }
	/* lane E: Bring to front. Same rule as the four branches above it. */
	if (t.closest && t.closest('[data-focus-session]')) { laneESendFocus(); return; }
	var btn = t.closest ? t.closest('.sheet-btn') : null;
	if (btn) {
		onSheetAction(btn.getAttribute('data-sheet-action'), btn.getAttribute('data-sheet-text') || '');
		return;
	}
	/* Backdrop or the X — anywhere that is not the panel's own content. */
	if (t === ui.sheetBackdrop || (t.closest && t.closest('#sheetClose'))) closeSheet();
}

/* --------------------------------------------------- crab tap: ack-all (v4) */

/* The crab is the only control on the panel you can hit without aiming. Tapping
   it acks EVERY waiting session at once; with nothing waiting it blinks and
   sends nothing, so the huge hit target cannot cause a write by accident.

   Optimistic for the same reason the per-card ack is: the glow is what the
   person crossed the room to silence, so it dies on the tap. A failed POST puts
   back exactly the acks this tap took — never one an earlier tap owns. */
/* v0.14.0: the ack-all ITSELF, split out of the crab tap so the two-finger tap
   makes exactly the same write rather than a second copy of it that can drift.
   Returns how many acks this call took, which is what the two-finger tap's
   confirmation line counts and what the crab tap tests to decide whether to
   blink instead. */
function onCrabTap() {
	if (crabBusy) return;
	if (!ackAllWaiting()) blinkOnce();
}

function ackAllWaiting() {
	if (crabBusy) return 0;
	var sessions = lastGoodDoc && Array.isArray(lastGoodDoc.sessions) ? lastGoodDoc.sessions : [];
	var taken = [];
	for (var i = 0; i < sessions.length; i++) {
		var s = sessions[i];
		if (!s || s.state !== 'needs_input' || effectiveAcked(s)) continue;
		/* A pendingPermission card is a hard stop, not an ack-able question — the
		   crab tap must not silence a permission gate (v0.12.0). It keeps asking
		   until a decision is made on its own sheet. */
		if (s.pendingPermission && typeof s.pendingPermission === 'object') continue;
		ackOptimistic[s.id] = String(s.stateSince || '');
		taken.push(s.id);
	}
	/* The blink moved OUT to onCrabTap at v0.14.0: an eye-flicker is the CRAB's
	   answer to being tapped with nothing waiting, and the two-finger tap, which
	   may be nowhere near the crab, has no business firing it. */
	if (!taken.length) return 0;

	/* The claw click is the panel's receipt for an ack-all: the tap is a palm on
	   the largest target on the glass, and the cards going quiet is a change you
	   have to already be looking at the grid to see. Fired on the OPTIMISTIC ack,
	   with the render below — not on the reply, which is a round trip away. */
	fireSnap();
	crabBusy = true;
	render();
	postAction(null, 'ack-all', '').then(function (res) {
		crabBusy = false;
		if (res.status === 204 || res.status === 200) return;
		rollbackAcks(taken);
		logLine('ack-all failed (HTTP ' + res.status + ')');
	}).catch(function () {
		crabBusy = false;
		rollbackAcks(taken);
		logLine('ack-all failed: crabd not reachable');
	});
	return taken.length;
}

function rollbackAcks(ids) {
	for (var i = 0; i < ids.length; i++) delete ackOptimistic[ids[i]];
	render();
	/* THE RECEIPT IS CORRECTED, NOT JUST THE CARDS (v0.20.0, CD-40). The two-finger
	   tap writes "acknowledged N" the instant the gesture lands, which is right —
	   but when the POST then failed, only the cards came back. The banner sat there
	   for its full second and a half saying the acknowledgement had happened while
	   the rows it named were visibly still waiting, which is the worst of the three
	   possible states: a receipt for a write that did not occur.
	   Fired for the crab tap too, which has no line of its own: a silent rollback on
	   the biggest target on the glass is a tap that looks like it worked. */
	showNotice('could not acknowledge ' + EMDASH + ' still waiting', 'err');
}

/* ------------------------------------------------------- blink + dismiss (v4) */

/* One eye-frame, using the sleep-bar eyes the asleep mood already paints — no new
   art, no animation, no layout. Suppressed under prefers-reduced-motion: it is
   short and low-contrast, but it is still a flicker, and a fingertip tap has the
   card pulse and the sheet as its other feedback. */
function blinkOnce() {
	if (blinking || reducedMotion()) return;
	blinking = true;
	ui.crab.classList.add('blink');
	setTimeout(function () { ui.crab.classList.remove('blink'); blinking = false; }, BLINK_MS);
}

/* Rare, random, and calm-mood only: a tic on a worried or sleeping crab would
   read as a fault, and one on a waving crab would compete with the alert.
   SWEATING joins content at v0.22.0 rather than being left out of the list. It is
   an open-eyed, non-alerting mood — the same shape content is — and the blink is
   the eyes only, which the sweat art does not touch. Leaving it out would have
   frozen the crab's one idle tic for the hours a weekly window sits in the red,
   which reads as a panel that has stopped rather than one with something to say.
   (The TAP blink is not gated by this at all: blinkOnce() runs on any mood, so a
   fingertip is answered whatever the crab is doing.) */
function scheduleBlink() {
	if (blinkTimer) clearTimeout(blinkTimer);
	var span = Math.max(0, blinkMaxMs - blinkMinMs);
	blinkTimer = setTimeout(function () {
		blinkTimer = null;
		var mood = ui.crab.getAttribute('data-mood');
		if (!document.body.classList.contains('quiet') &&
			(mood === 'content' || mood === 'sweating')) blinkOnce();
		scheduleBlink();
	}, blinkMinMs + Math.random() * span);
}

function onSheetDismiss() {
	if (!sheetSessionId) return;
	var s = findSession(sheetSessionId);
	/* Re-checked against the LIVE row, not against the state the sheet opened on:
	   a card that went idle -> working between the tap and this line must not be
	   hidden. syncSheet already shuts the sheet on such a move; this is the belt. */
	if (!s || !DISMISSABLE[s.state]) return;
	dismissed[s.id] = String(s.stateSince || '');
	closeSheet();
	render();
}

/* Has the served quiet window ENDED by the wall clock? (v0.20.0, CD-42.)

   It can only ever CLEAR quiet, never assert it, and the caller only asks while
   the feed is stale. That asymmetry is the honest half: dropping a dim the
   companion can no longer vouch for is surviving without a document, while
   dimming the panel on a window nobody served would be inventing one.

   Both ends are required. `start` and `end` are served together (STATE-CONTRACT
   `quiet: {active, start, end}`), and the end alone cannot answer the question —
   a 22:00-07:00 window is outside its end at 23:00 and inside it at 06:00, and
   an end with no start cannot tell those apart. A window missing either one
   stays quiet: unknown is not over. */
/* TWO THINGS CAN HOLD THIS PANEL QUIET SINCE v0.22.0, and re-evaluating only one of
   them was a real hole. crabd 0.23.0 serves an override with NO schedule configured
   as a quiet block whose `start` and `end` are BOTH NULL — so the v0.20.0 body,
   which bailed out to "still quiet" on any missing end of the window, could never
   clear an override-only block. An override that expired while crabd was DEAD left
   the panel dimmed indefinitely: the exact failure CD-42 was written to stop, one
   cause along.

   So each holder is asked separately and the answers are combined conservatively.
   The asymmetry is unchanged and is what makes this safe: this function can only
   ever CLEAR quiet, never assert it, and the caller only asks while the feed is
   stale. Unknown is not over — on EITHER half. Quiet is over only when every reason
   on record has definitively ended, and if nothing is on record at all there is
   nothing to re-evaluate and the dim stays. */
function quietWindowOver(q, now) {
	if (!q || typeof q !== 'object' || Array.isArray(q)) return false;
	var nowMs = now.getTime();
	var sched = quietScheduleState(q, nowMs);
	var ovr = quietOverrideState(q, nowMs);
	/* Neither a schedule nor an override on record: the block says it is quiet and
	   gives no reason this widget can check. Keep the dim — re-evaluating nothing
	   and calling the result "over" would be asserting, which this function does
	   not do. */
	if (sched === 'none' && ovr === 'none') return false;
	if (sched === 'unknown' || ovr === 'unknown') return false;
	if (sched === 'inside' || ovr === 'inside') return false;
	return true;
}

/* 'none' | 'unknown' | 'inside' | 'over'.

   THREE ANSWERS WHERE THERE USED TO BE TWO. Both ends absent is now a KNOWN "there
   is no schedule" (crabd 0.23.0's override-only block) rather than the unknown a
   HALF-served window is — and the distinction matters in exactly one direction: a
   window with one end missing still cannot be evaluated and still keeps the dim,
   which is the v0.20.0 rule preserved intact.
   Safe against older crabd: before 0.23.0 an unconfigured schedule made the whole
   `quiet` key null, and a null key never reaches here — render() only consults this
   when it is already rendering quiet. */
function quietScheduleState(q, nowMs) {
	var hasStart = q.start !== null && q.start !== undefined && q.start !== '';
	var hasEnd = q.end !== null && q.end !== undefined && q.end !== '';
	if (!hasStart && !hasEnd) return 'none';
	var start = normHm(q.start), end = normHm(q.end);
	if (!start || !end) return 'unknown';
	var d = new Date(nowMs);
	var mins = d.getHours() * 60 + d.getMinutes();
	var s = hmMinutes(start), e = hmMinutes(end);
	/* A ZERO-LENGTH WINDOW IS NOT A REASON, and this reading was CORRECTED against
	   production in v0.22.0. The v0.20.0 body returned "still quiet" here on the
	   stated grounds that crabd read start == end as a 24 h window. Measured in
	   companion/crabd.py `quiet_state` (0.23.0): `if start == end: active = False`
	   — "zero-length window; always quiet is not expressible here". So crabd reads
	   it the OTHER way, and the widget agreeing with a comment instead of with the
	   daemon left one real hole: a start == end schedule plus an override that had
	   expired kept the panel dimmed forever, because this half never stopped
	   claiming to be inside a window crabd does not think exists. */
	if (s === e) return 'none';
	var inside = s < e ? (mins >= s && mins < e) : (mins >= s || mins < e);
	return inside ? 'inside' : 'over';
}

/* 'none' | 'unknown' | 'inside' | 'over'.

   Only mode "on" can be HOLDING the panel quiet, so "off" and "auto" are 'none' —
   not because they are absent, but because they are not a reason the panel is dim,
   and treating them as one would let an "awake" override keep a dim alive.
   An unparseable `until` is 'unknown' and keeps the dim: the same rule
   approvalRemaining() keeps, that an unreadable clock is not an expired one. */
function quietOverrideState(q, nowMs) {
	var ov = q.override;
	if (!ov || typeof ov !== 'object' || Array.isArray(ov)) return 'none';
	if (ov.mode !== 'on') return 'none';
	var until = ov.until ? Date.parse(ov.until) : NaN;
	if (!isFinite(until)) return 'unknown';
	return until <= nowMs ? 'over' : 'inside';
}

function hmMinutes(hm) {
	return Number(hm.slice(0, 2)) * 60 + Number(hm.slice(3, 5));
}

/* ------------------------------------------ the quiet override (v0.22.0) */

/* The feed's answer, presence-detected on the MEMBER and never on the block: the
   `quiet` block is served by every supported crabd, and `override` is additive
   inside it. Three shapes all mean "no override" and all have to land there —
   absent, null, and a mode this build does not know.

   `mode: "auto"` is deliberately in that last group. It is a legal value the tap
   cycle sends, and what it MEANS is "there is no override", so rendering it as a
   fourth state would be the chip reporting the absence of a thing as the thing.

   An unparseable `until` returns null rather than dropping the override: the mode
   is a fact the feed stated, and the remaining time is an annotation on it. Unknown
   remaining is not expired — the same rule approvalRemaining() keeps. */
function quietOverrideFromFeed() {
	var q = lastGoodDoc && lastGoodDoc.quiet;
	if (!q || typeof q !== 'object' || Array.isArray(q)) return null;
	var ov = q.override;
	if (!ov || typeof ov !== 'object' || Array.isArray(ov)) return null;
	var mode = typeof ov.mode === 'string' ? ov.mode : '';
	if (mode !== 'on' && mode !== 'off') return null;
	var until = ov.until ? Date.parse(ov.until) : NaN;
	return { mode: mode, until: isFinite(until) ? until : null };
}

/* WHAT THE CHIP SAYS, which is the feed's answer except for the moment after a tap.

   The local expiry test is the one thing here that is not read straight off the
   document, and it is the CD-42 asymmetry: it can only ever CLEAR an override,
   never assert one. Dropping an override whose own stated `until` has passed is
   arithmetic on the feed's own value; painting one nobody served would be inventing
   a state. crabd will drop it from the next document anyway — this only stops the
   chip counting "0m" for up to a poll after it ended. */
/* SCA-027 — A REQUESTED OVERRIDE IS PENDING, NOT ON. The tap used to paint the
   requested mode as though the companion had already applied it: the chip read
   "quiet", carried data-quiet="on" and announced "Quiet override on" while the feed
   still said quiet.active was false - so an alert could sound with the control
   under the operator's thumb saying the panel was silent. What is returned here is
   still the requested mode, because that is what the next tap cycles from; what is
   added is `pending`, which is the difference between a request and a fact, and
   everything that paints or gates reads it. */
function quietState() {
	if (quietOptimistic) {
		/* The first document generated AFTER the tap is the answer, whatever it says:
		   the companion applies the action on the POST, so a newer document that does
		   not carry the override is the companion declining it, not latency. */
		if (lastGoodAtMs > quietOptimistic.at) quietOptimistic = null;
		else return { mode: quietOptimistic.mode, until: quietOptimistic.until, pending: true };
	}
	var fed = quietOverrideFromFeed();
	if (!fed) return { mode: 'auto', until: null, pending: false };
	if (fed.until !== null && fed.until <= Date.now()) return { mode: 'auto', until: null, pending: false };
	return { mode: fed.mode, until: fed.until, pending: false };
}

/* THE CONSERVATIVE HALF, and it is deliberately one-sided. While a request for
   QUIET is unconfirmed the chime is held locally: the operator has just asked for
   silence, and the honest failure there is a missed chime rather than a noise in a
   room somebody has just silenced. A request for AWAKE gets no such treatment -
   there is no optimistic unmute against a quiet period the companion has confirmed,
   because that would make the panel louder on a promise it has not been given. */
function quietPendingMute() {
	return !!(quietOptimistic && quietOptimistic.mode === 'on');
}

/* The fixed vocabulary, as a function so the tap and the aria-label cannot disagree
   about what the next tap does. */
function nextQuietMode(mode) {
	return mode === 'auto' ? 'on' : mode === 'on' ? 'off' : 'auto';
}

function quietModeWord(mode) {
	return mode === 'on' ? 'quiet' : mode === 'off' ? 'awake' : 'auto';
}

function onMoonTap() {
	if (quietOverrideUnsupported) return;
	sendQuietOverride(nextQuietMode(quietState().mode));
}

/* The long press, the gesture idiom already on this panel: press and hold a card to
   pin it, press and hold the chip to hand quiet hours back to the schedule from
   whatever state it is in. It is a SHORTCUT through the cycle and never a fourth
   state — an operator two taps from auto should not have to make both of them.
   Already-auto is a genuine no-op and sends nothing: the crab tap's rule, that a
   control which cannot cause a write by accident is the property a big target
   needs. */
function fireMoonAuto() {
	suppressClick();
	if (quietOverrideUnsupported) return;
	if (quietState().mode === 'auto') return;
	sendQuietOverride('auto');
}

function sendQuietOverride(mode) {
	if (quietBusy) return;
	var minutes = Math.round(Math.max(QUIET_MIN_MINUTES, Math.min(QUIET_MAX_MINUTES, QUIET_OVERRIDE_MIN)));
	quietOptimistic = {
		mode: mode,
		/* auto has no end because it is not a state that ends. */
		until: mode === 'auto' ? null : Date.now() + minutes * 60000,
		at: Date.now()
	};
	quietBusy = true;
	fireSnap();
	render();
	/* `minutes` rides EVERY body, auto included. The contract lists it as part of
	   the action and does not mark it optional, so sending it is what conforms; for
	   auto it is meaningless and crabd is expected to ignore it. One body shape is
	   also what keeps a single 400 from meaning two different things — a shape-
	   dependent body would make "this crabd is old" and "this crabd disliked that
	   field" indistinguishable, and the latch below cannot tell them apart. */
	postAction(null, 'quiet', null, null, { mode: mode, minutes: minutes }).then(function (res) {
		quietBusy = false;
		/* 2xx says nothing about WHAT crabd recorded — only that it took the write.
		   The chip settles on the next document either way, which is the whole
		   reason the optimistic answer is bounded by the feed. */
		if (res.status === 204 || res.status === 200) return;
		quietOptimistic = null;
		if (res.status === 400 || res.status === 404) {
			quietOverrideUnsupported = true;
			logLine('quiet override unsupported by this crabd (HTTP ' + res.status + ')');
			showNotice('quiet override not available on this companion', 'err');
		} else {
			logLine('quiet override failed (HTTP ' + res.status + ')');
			showNotice('quiet override not sent (HTTP ' + res.status + ')', 'err');
		}
		render();
	}).catch(function () {
		quietBusy = false;
		quietOptimistic = null;
		/* NOT latched. A dead socket is a fact about this moment and not about this
		   crabd's version — treating a blip as "unsupported forever" would strand the
		   control until somebody re-imported the widget. */
		logLine('quiet override failed: crabd not reachable');
		showNotice('quiet override not sent ' + EMDASH + ' crabd not reachable', 'err');
		render();
	});
}

/* Hidden unless the feed is LIVE, the History chip's rule and the same reasoning:
   this control's only purpose is to write to the companion, and a panel that cannot
   see the companion must not be offering to. Hidden again once a write has proved
   the action does not exist here. */
function renderMoonChip(status) {
	if (!ui.moonChip) return;
	var show = status === 'live' && !quietOverrideUnsupported;
	if (ui.moonChip.classList.contains('shown') !== show) ui.moonChip.classList.toggle('shown', show);
	if (!show) {
		if (ui.moonChip.hasAttribute('data-until')) ui.moonChip.removeAttribute('data-until');
		return;
	}
	var st = quietState();
	/* SCA-027: the chip says what is TRUE, and while a request is unconfirmed the
	   true thing is that it was asked for. */
	var word = st.pending ? 'pending' : quietModeWord(st.mode);
	var attr = st.pending ? 'pending' : st.mode;
	if (ui.moonChip.getAttribute('data-quiet') !== attr) ui.moonChip.setAttribute('data-quiet', attr);
	setText(ui.moonMode, word);
	/* The instant is parked on the element and relabelled by the 1 Hz tick, the
	   idiom the gauge countdowns and the card ages already use: the poll is 3 s and
	   a remaining time that only moved on a poll would cross its minute boundary up
	   to three seconds late. REMOVED whenever there is nothing to count, so the tick
	   cannot write a figure computed from a stale number. */
	if (!st.pending && st.mode !== 'auto' && st.until !== null) {
		var key = String(st.until);
		if (ui.moonChip.getAttribute('data-until') !== key) ui.moonChip.setAttribute('data-until', key);
	} else if (ui.moonChip.hasAttribute('data-until')) {
		ui.moonChip.removeAttribute('data-until');
	}
	paintMoonLeft(Date.now());
	var label = st.pending
		? quietModeWord(st.mode) + ' requested ' + EMDASH + ' waiting for the companion to confirm'
		: st.mode === 'auto'
		? 'Quiet hours follow the schedule. Tap for quiet for an hour.'
		: (st.mode === 'on' ? 'Quiet override on' : 'Staying awake through quiet hours') +
		  '. Tap for ' + quietModeWord(nextQuietMode(st.mode)) + ', press and hold for the schedule.';
	if (ui.moonChip.getAttribute('aria-label') !== label) {
		ui.moonChip.setAttribute('aria-label', label);
		ui.moonChip.setAttribute('title', label);
	}
}

function paintMoonLeft(nowMs) {
	if (!ui.moonLeft) return;
	if (!ui.moonChip.hasAttribute('data-until')) { setText(ui.moonLeft, ''); return; }
	var t = Number(ui.moonChip.getAttribute('data-until'));
	if (!isFinite(t)) { setText(ui.moonLeft, ''); return; }
	var left = t - nowMs;
	/* At zero the LABEL goes rather than reading "0m", and the mode word follows on
	   the render this schedules — an override that has run out is not an override
	   that has a minute left. */
	setText(ui.moonLeft, left > 0 ? fmtDur(left / 1000) : '');
}

/* Relabel on the tick, and re-render on the edge where the override actually ends
   so the word flips back to `auto` on the second it happens rather than on the next
   poll. */
function tickMoonChip(nowMs) {
	if (!ui.moonChip || !ui.moonChip.classList.contains('shown')) return;
	var had = ui.moonChip.hasAttribute('data-until');
	paintMoonLeft(nowMs);
	if (had && Number(ui.moonChip.getAttribute('data-until')) <= nowMs) render();
}

/* ------------------------------------------------ quiet hours config (v0.4.0) */

/* Strict HH:MM, because crabd validates strictly and answers 400 — a single-digit
   hour is padded rather than rejected, since "9:05" is a typed value a person
   plainly means, but anything else is left alone and nothing is sent. */
function normHm(v) {
	var m = /^\s*(\d{1,2}):(\d{2})\s*$/.exec(String(v === undefined || v === null ? '' : v));
	if (!m) return null;
	var h = Number(m[1]), mi = Number(m[2]);
	if (!isFinite(h) || !isFinite(mi) || h < 0 || h > 23 || mi < 0 || mi > 59) return null;
	return pad2(h) + ':' + pad2(mi);
}

/* ================================================================ MF-001
   COMPANION CONFIGURATION, EDITED ON THE GLASS.

   WHAT THIS REPLACES. Until v0.32.0 quiet hours, the toast thresholds and the
   budget were derived from the vendor property sheet and pushed to /v1/config one
   key at a time on a debounce, with a whole layer of capability latches for
   companions that predated each key. That surface is retired with the vendor host,
   and the push went with it - it had been inert in the standalone panel anyway, by
   an explicit guard, precisely because a panel with no property sheet would have
   POSTed its own defaults on every boot and silently cleared a hand-edited file.

   THE RULE THAT REPLACES ALL OF IT: ONLY THE KEYS THE OPERATOR ACTUALLY MOVED ARE
   SENT. A key nobody touched is a key this panel has no opinion about, and the
   companion preserves what it already holds. That is the same discipline the old
   approvalThresholdSec sequencing was built to get, generalised to every key and
   made obvious instead of clever.

   THE CONTROLS SEED FROM THE FEED, which is the only statement of what is
   currently configured. A value the feed does not carry seeds from this panel's
   own default and is NOT sent unless it is moved, so seeding cannot write. */

/* Strict HH:MM, because the companion validates strictly and answers 400 - a
   single-digit hour is padded rather than rejected, since "9:05" is a typed value a
   person plainly means, but anything else is left alone and nothing is sent. */
function normHm(v) {
	var m = /^\s*(\d{1,2}):(\d{2})\s*$/.exec(String(v === undefined || v === null ? '' : v));
	if (!m) return null;
	var h = Number(m[1]), mi = Number(m[2]);
	if (!isFinite(h) || !isFinite(mi) || h < 0 || h > 23 || mi < 0 || mi > 59) return null;
	return pad2(h) + ':' + pad2(mi);
}

function clampApprovalSec(n) {
	return Math.round(Math.max(APPROVAL_SEC_MIN, Math.min(APPROVAL_SEC_MAX, n)));
}

function clampToastSec(n) {
	return Math.round(Math.max(TOAST_SEC_MIN, Math.min(TOAST_SEC_MAX, n)));
}

/* v0.17.0. Reads the feed's optional `toast.approvalThresholdSec` and nothing else.
   Presence-detected on the MEMBER, not on the block: an older companion sends no
   `toast` at all, and a current one sends the block WITHOUT this member until it is
   set. Both must land on null, because a default printed as a configured value is a
   figure nobody chose. */
function noteApprovalSeed(toast) {
	var have = toast && typeof toast === 'object' && !Array.isArray(toast);
	var n = have ? Number(toast.approvalThresholdSec) : NaN;
	/* hasOwnProperty rather than a truthiness test: 0 is not a legal value here, but
	   a null the contract does allow must read as absent rather than as 0. */
	var present = have &&
		Object.prototype.hasOwnProperty.call(toast, 'approvalThresholdSec') &&
		isFinite(n);
	approvalFeedSec = present ? clampApprovalSec(n) : null;
}

/* MF-017. The readiness line in the action sheet's approval block, beside the
   threshold line that is already there. Rendered only when the companion states a
   readiness AND it is not `ready`: a decision that is going to work needs no line
   about itself, and the sheet has the width for one sentence, not two. */
function renderApprovalReadiness() {
	if (!ui.sheetApprovalReady) return;
	var state = approvalReadiness();
	var show = state !== null && state !== 'ready';
	setText(ui.sheetApprovalReady, show ? approvalReadyText(state) : '');
	ui.sheetApprovalReady.classList.toggle('shown', show);
}

/* Presence-gated on the SEED: with no `toast` block in the feed there is nothing
   the panel knows that the config sheet does not already show. */
function renderApprovalThreshold() {
	if (!ui.sheetApprovalThreshold) return;
	if (approvalFeedSec === null) {
		setText(ui.sheetApprovalThreshold, '');
		ui.sheetApprovalThreshold.classList.remove('shown');
		return;
	}
	setText(ui.sheetApprovalThreshold, 'toast after ' + fmtApprovalSec(approvalFeedSec));
	ui.sheetApprovalThreshold.classList.add('shown');
}

/* Minutes ONLY for a whole number of them; everything else stays in seconds. 90 and
   135 are ordinary settings, and rounding those to minutes printed "2 min" for a
   90 s threshold - not a rounding, a wrong number on a settings line. */
function fmtApprovalSec(sec) {
	return sec >= 60 && sec % 60 === 0 ? (sec / 60) + ' min' : sec + ' s';
}

/* ---- MF-001: the draft, the baseline and what may be sent ---- */

var cfgDraft = null;
var cfgTouched = null;
var cfgBusy = false;

/* What the feed says is configured right now. Every member is presence-detected and
   falls back to this panel's own default, which is safe ONLY because a defaulted
   member is never sent unless it is moved. */
function configSeed() {
	var doc = lastGoodDoc;
	var q = doc && doc.quiet && typeof doc.quiet === 'object' && !Array.isArray(doc.quiet) ? doc.quiet : null;
	var t = doc && doc.toast && typeof doc.toast === 'object' && !Array.isArray(doc.toast) ? doc.toast : null;
	/* The digest is not in the state document today. Presence-gated on both places
	   it could arrive, so a companion that starts serving it lights these controls
	   up with no change here; until then they seed from the defaults and stay unsent
	   until moved. */
	var d = (t && t.digest && typeof t.digest === 'object' && !Array.isArray(t.digest)) ? t.digest
		: (doc && doc.digest && typeof doc.digest === 'object' && !Array.isArray(doc.digest)) ? doc.digest : null;
	var b = doc && doc.burn && doc.burn.budget && typeof doc.burn.budget === 'object' &&
		!Array.isArray(doc.burn.budget) ? doc.burn.budget : null;
	var bt = b ? Number(b.dailyOutputTokens) : NaN;
	return {
		quietEnabled: !!(q && normHm(q.start) && normHm(q.end)),
		quietStart: (q && normHm(q.start)) || '22:00',
		quietEnd: (q && normHm(q.end)) || '07:00',
		toastEnabled: t ? t.enabled !== false : true,
		toastSec: t && isFinite(Number(t.thresholdSec)) ? clampToastSec(Number(t.thresholdSec)) : TOAST_SEC_DEFAULT,
		approvalSec: approvalFeedSec !== null ? approvalFeedSec : APPROVAL_SEC_DEFAULT,
		digestEnabled: !!(d && d.enabled === true),
		digestTime: (d && normHm(d.time)) || '09:00',
		budgetEnabled: isFinite(bt),
		budgetK: isFinite(bt) ? Math.round(bt / 1000) : BUDGET_K_DEFAULT
	};
}

function configValues() {
	if (!cfgDraft) { cfgDraft = configSeed(); cfgTouched = {}; }
	return cfgDraft;
}

function configSet(key, value) {
	configValues()[key] = value;
	cfgTouched[key] = true;
	setConfigStatus('not saved yet', 'pending');
}

/* The body, built from the touched keys alone, plus any reason a touched key could
   not be made into a legal one. A warning here NEVER becomes a partial write: the
   sheet says what is wrong and sends nothing. */
function configPayload() {
	var v = configValues(), t = cfgTouched, out = {}, warn = [], any = false;
	if (t.quietEnabled || t.quietStart || t.quietEnd) {
		if (!v.quietEnabled) { out.quietHours = null; any = true; }
		else {
			var qs = normHm(v.quietStart), qe = normHm(v.quietEnd);
			if (!qs || !qe) warn.push('quiet hours need two HH:MM times');
			else { out.quietHours = { start: qs, end: qe }; any = true; }
		}
	}
	if (t.toastEnabled || t.toastSec || t.approvalSec) {
		/* thresholdSec and enabled are BOTH required by the contract, so a change to
		   either sends both. approvalThresholdSec rides only when it was MOVED: the
		   companion preserves an omitted value, and sending this panel's default
		   would delete a figure hand-edited into the config file. */
		out.toast = { thresholdSec: clampToastSec(v.toastSec), enabled: !!v.toastEnabled };
		if (t.approvalSec) out.toast.approvalThresholdSec = clampApprovalSec(v.approvalSec);
		any = true;
	}
	if (t.digestEnabled || t.digestTime) {
		var dt = normHm(v.digestTime);
		if (!dt) warn.push('the digest needs an HH:MM time');
		else { out.digest = { enabled: !!v.digestEnabled, time: dt }; any = true; }
	}
	if (t.budgetEnabled || t.budgetK) {
		out.budget = v.budgetEnabled
			? { dailyOutputTokens: Math.round(Math.max(BUDGET_K_MIN, Math.min(BUDGET_K_MAX, v.budgetK))) * 1000 }
			: null;   /* null CLEARS the key, which is a different statement from not sending it */
		any = true;
	}
	return { body: out, warnings: warn, any: any };
}

function onConfigSave() {
	if (cfgBusy) return;
	var built = configPayload();
	if (built.warnings.length) { setConfigStatus(built.warnings.join('; '), 'err'); return; }
	if (!built.any) { setConfigStatus('nothing changed', 'note'); return; }
	cfgBusy = true;
	/* lane N: SCA-006 for the settings sheet, which was the one surface it did not
	   reach. A save in flight when the sheet is closed and reopened landed on the
	   NEW sheet: setConfigStatus wrote "saved budget" onto it, and the
	   buildSettingsRows below threw away whatever the operator had typed into it
	   since. Reproduced against the shipping file - two edits on the reopened sheet,
	   cfgTouched null a tick later, with no warning anywhere.
	   sheetGen is the existing token and both openSettingsSheet and closeSheet
	   already bump it, so the surface is identified by the thing that defines it. */
	var cfgGen = sheetGen;
	setConfigStatus('saving', 'pending');
	postConfig(JSON.stringify(built.body)).then(function (res) {
		cfgBusy = false;
		if (cfgGen !== sheetGen) { logLine('config save landed after its sheet closed; not shown'); return; }
		if (res.status !== 204 && res.status !== 200) {
			setConfigStatus('not saved (HTTP ' + res.status + ')', 'err');
			return;
		}
		/* The companion's own account of what it stored. `applied` is what actually
		   went to disk after its validation, `warnings` is what it declined or
		   corrected - both are shown, because a save that quietly dropped a key is
		   the failure this reply exists to make visible. */
		var body = res.body && typeof res.body === 'object' ? res.body : null;
		var warn = body && Array.isArray(body.warnings) ? body.warnings.filter(function (w) {
			return typeof w === 'string' && w;
		}) : [];
		var applied = body && body.applied && typeof body.applied === 'object' ? body.applied : null;
		var names = [];
		if (applied) {
			for (var k in applied) { if (Object.prototype.hasOwnProperty.call(applied, k)) names.push(k); }
		}
		/* The draft is dropped so the controls re-seed from the next document: what
		   the companion stored is what they must show, not what this page sent. */
		cfgDraft = null;
		cfgTouched = null;
		if (sheetMode === 'settings') buildSettingsRows();
		setConfigStatus(warn.length
			? 'saved ' + (names.length ? names.join(', ') : '') + ' ' + EMDASH + ' ' + warn.join('; ')
			: names.length ? 'saved ' + names.join(', ') : 'saved',
			warn.length ? 'note' : 'ok');
	}).catch(function () {
		cfgBusy = false;
		if (cfgGen !== sheetGen) { logLine('config save failed after its sheet closed; not shown'); return; }   // lane N
		setConfigStatus('not saved ' + EMDASH + ' crabd not reachable', 'err');
	});
}

function setConfigStatus(text, kind) {
	if (!ui.cfgStatus) return;
	setText(ui.cfgStatus, text);
	if (ui.cfgStatus.getAttribute('data-kind') !== (kind || '')) {
		ui.cfgStatus.setAttribute('data-kind', kind || '');
	}
}

/* ------------------------------------------- the hardware row (CLEAN-03, v0.32.0)

   THE COMPANION IS THE ONLY SOURCE NOW. This row used to be assembled from two,
   on two clocks: a vendor sensor plugin reached through an inlined async wrapper
   (window.plugins, a request/response bridge with its own 5 s timeouts, units and
   name caches, failure backoffs, a read-outcome ring buffer and a staleness
   watchdog on the 1 Hz tick), and the companion's own `host` block on the poll.
   The plugin went with the vendor host, and the whole second clock went with it -
   along with the synchronous-answer ordering race that froze the row, the two
   settings that could name one sensor twice, and the cell arbitration between them.

   Everything on this row now arrives in /v1/state and is rendered by the lane A
   block near the end of this file, presence-detected member by member. THE HONESTY
   RULE IS UNCHANGED AND IS THE WHOLE POINT: an absent reading is an absent cell,
   never a zero and never a number left over from a source that stopped answering.
   A reading that is present but old is dimmed by the feed's own sampledAt, which is
   the companion stating its freshness rather than the panel guessing at it. */
/* ---- the row's one owner (v0.21.0, one source since v0.32.0) -----------------

   Visibility for the whole row is computed HERE, in one place, from all of the
   state, every time any of it moves. That rule outlived the two-source problem it
   was written for: a cell shown by one writer and hidden by another is how a row
   ends up hidden with a live figure in it, and the lane A block still paints cells
   this function has decided about. */
function syncSensorRow() {
	/* A cell is shown when it has something true to say. CPU can be lit by a
	   temperature or by the feed's utilisation, independently; GPU by either half
	   of its own pair. */
	var cpuOn = hostMetrics.cpuPct !== null || laneACpuOn();
	var gpuOn = laneAGpuOn();
	var memOn = hostMetrics.memPct !== null;
	ui.sensorCpu.classList.toggle('shown', cpuOn);
	ui.sensorGpu.classList.toggle('shown', gpuOn);
	ui.hostMem.classList.toggle('shown', memOn);
	syncLaneASensorCells();   /* lane A: the CPU and GPU cells, from the companion */

	var any = cpuOn || gpuOn || memOn || laneAAnyCell();
	ui.sensors.classList.toggle('shown', any);

	/* THE DRILL-IN (v0.22.0), decided here because this function is the row's one
	   owner and the tap is a fact about the row. It follows the READING, not the
	   markup - the discipline setGaugeTappable keeps: the row is a control only
	   while the feed is serving a host figure to have a history OF, so a panel with
	   temperatures alone offers no chevron, no pointer and no promise.
	   role and tabindex are added and REMOVED with it rather than sitting in the
	   markup, so an inert row is not a tab stop that does nothing. */
	var drill = any && hostSheetAvailable();
	if (ui.sensors.classList.contains('tappable') !== drill) ui.sensors.classList.toggle('tappable', drill);
	if (drill) {
		ui.sensors.setAttribute('role', 'button');
		ui.sensors.setAttribute('tabindex', '0');
		ui.sensors.setAttribute('aria-label', "Open this PC's CPU and memory history");
	} else {
		ui.sensors.removeAttribute('role');
		ui.sensors.removeAttribute('tabindex');
		ui.sensors.removeAttribute('aria-label');
	}

	markSensorZone(any);
}

/* The sensors row is the ONLY thing the Limits zone can show when the feed carries
   no limits, so whether it has anything to say decides whether that zone is a zone
   at all. A body class rather than a CSS :has() on the row, and the argument is the
   whole row's verdict rather than one reading. */
function markSensorZone(any) {
	document.body.classList.toggle('has-sensors', !!any);
}

/* ---- the host block from the feed (v0.21.0) ---------------------------------

   crabd 0.22.0 serves a top-level `host: {cpuPct, memPct, memUsedGB, memTotalGB}`.
   Presence-detected member by member, never on the block being truthy: an older
   crabd sends no block at all, a current one may send any member as null, and
   both have to land on "the segment is simply absent". Number and isFinite, so a
   contract-legal null cannot arrive as Number(null) === 0 and paint an idle
   machine that is actually one crabd could not measure. */
function renderHost(host) {
	var have = !!(host && typeof host === 'object' && !Array.isArray(host));
	hostMetrics.cpuPct = have ? hostPct(host.cpuPct) : null;
	hostMetrics.memPct = have ? hostPct(host.memPct) : null;
	hostMetrics.memUsedGB = have ? hostGB(host.memUsedGB) : null;
	hostMetrics.memTotalGB = have ? hostGB(host.memTotalGB) : null;

	setText(ui.hostCpuVal, hostMetrics.cpuPct === null ? '' : hostMetrics.cpuPct + '%');
	ui.hostCpuVal.classList.toggle('shown', hostMetrics.cpuPct !== null);
	/* The percent sign is what separates this from the degree sign beside it, and
	   on the glass that is the whole distinction — so the word is on the
	   accessibility tree, where there is room for it. The cell budget is 49.8 px of
	   spare width with both names rendered (measured, 2560x720): a "load" label
	   would spend it and start ellipsing the sensor NAME, which is the one thing in
	   this row that exists to be read. */
	if (hostMetrics.cpuPct === null) {
		ui.hostCpuVal.removeAttribute('title');
		ui.hostCpuVal.removeAttribute('aria-label');
	} else {
		ui.hostCpuVal.setAttribute('title', 'host CPU utilization ' + hostMetrics.cpuPct + '%');
		ui.hostCpuVal.setAttribute('aria-label', 'host CPU utilization ' + hostMetrics.cpuPct + '%');
	}
	setText(ui.hostMemVal, hostMetrics.memPct === null ? '' : hostMetrics.memPct + '%');
	ui.hostMemVal.classList.toggle('shown', hostMetrics.memPct !== null);

	/* The GB pair rides on title/aria rather than on the glass, and that is a
	   MEASURED decision: the row has 360.8 px of spare width at the 2560x720 slot
	   and the four segments plus two names spend most of it. The percentage is the
	   reading; "11.2 / 32.0 GB" is the annotation, and an annotation that pushed a
	   name off the row would have cost more than it said. Rendered only when BOTH
	   halves are readable — "11.2 GB of unknown" is not a fact worth carrying. */
	var pair = (hostMetrics.memUsedGB !== null && hostMetrics.memTotalGB !== null)
		? hostMetrics.memUsedGB.toFixed(1) + ' / ' + hostMetrics.memTotalGB.toFixed(1) + ' GB'
		: '';
	if (pair) {
		ui.hostMem.setAttribute('title', pair);
		ui.hostMem.setAttribute('aria-label', 'memory ' + (hostMetrics.memPct === null ? '' : hostMetrics.memPct + '%, ') + pair);
	} else {
		ui.hostMem.removeAttribute('title');
		ui.hostMem.removeAttribute('aria-label');
	}
	/* ---- lane A: the four additive members, read on the same pass ---- */
	renderHostExtras(host);
	syncSensorRow();
}

function hostPct(v) {
	if (typeof v !== 'number' || !isFinite(v)) return null;
	return Math.round(Math.min(100, Math.max(0, v)));
}

function hostGB(v) {
	if (typeof v !== 'number' || !isFinite(v) || v < 0) return null;
	return v;
}

/* ---- host history (v0.22.0) -------------------------------------------------

   Ten minutes of CPU and memory, from the SAME `host` member the row already
   renders, kept in a ring in the page and nowhere else. No endpoint is added and
   none is asked for: crabd serves the current reading, and a history of it is
   something a panel that has been watching can assemble honestly — and a panel that
   has just booted honestly cannot, which is what the "collecting" state is for.

   The ring survives in-page only, by design rather than by omission. Persisting it
   would mean a panel restarted at 09:00 drawing a line across the gap it was off
   for, and the one rule this chart has is that a stretch nothing was measured in is
   never drawn over. */
function sampleHost(doc) {
	var h = doc && doc.host && typeof doc.host === 'object' && !Array.isArray(doc.host) ? doc.host : null;
	var now = Date.now();
	/* hostPct is the SAME reader the row uses, so a contract-legal null cannot enter
	   the ring as a 0 and draw a floor the machine never touched. */
	hostRing.push({ t: now, cpu: h ? hostPct(h.cpuPct) : null, mem: h ? hostPct(h.memPct) : null });
	hostRingTrim(hostRing, now);
	laneASampleHost(doc);   /* lane A: the second ring, on the same document */
}

/* SCA-012. ONE trim for both rings, and TIME IS THE HORIZON: a sample inside the
   ten minutes the chart advertises is never dropped to satisfy a count. */
function hostRingTrim(ring, now) {
	var cut = now - HOST_WINDOW_MS;
	while (ring.length && ring[0].t < cut) ring.shift();
	if (ring.length <= HOST_RING_MAX) return;
	/* Past the memory cap the ring is THINNED: keep the newest sample and every
	   second one below it, and keep the oldest outright so the span moves by nothing
	   at all. HOST_GAP_MS is three intervals, so a doubled step is still inside it
	   and the line does not break where nothing was actually missed. */
	var keep = [], i;
	for (i = ring.length - 1; i >= 0; i -= 2) keep.push(ring[i]);
	if (keep[keep.length - 1] !== ring[0]) keep.push(ring[0]);
	keep.reverse();
	ring.length = 0;
	for (i = 0; i < keep.length; i++) ring.push(keep[i]);
}

/* The ring split into CONTIGUOUS runs — the segments the line may actually be drawn
   through. Two different absences break it and both are real:
   - a sample whose value is null: the poll landed and crabd could not measure.
   - a time step past HOST_GAP_MS: polls that never landed at all.
   Neither is bridged. A straight segment across a gap is an interpolation, and an
   interpolated CPU history is a reading nobody took — which on a chart is
   indistinguishable from one somebody did. */
function hostRuns(key) {
	var runs = [];
	var cur = [];
	var prevT = null;
	for (var i = 0; i < hostRing.length; i++) {
		var s = hostRing[i];
		if (s[key] === null) {
			if (cur.length) runs.push(cur);
			cur = []; prevT = null;
			continue;
		}
		if (prevT !== null && s.t - prevT > HOST_GAP_MS) {
			if (cur.length) runs.push(cur);
			cur = [];
		}
		cur.push(s);
		prevT = s.t;
	}
	if (cur.length) runs.push(cur);
	return runs;
}

function hostCount(key) {
	var n = 0;
	for (var i = 0; i < hostRing.length; i++) { if (hostRing[i][key] !== null) n++; }
	return n;
}

/* The row is a control only when it has a HISTORY to open — which is the same test
   as "the feed is serving a host figure", because the ring is fed from that member
   and nothing else. Temperatures alone do not earn the tap: they are not sampled
   into the ring, and a sheet that charted nothing would be a control that opens an
   empty view. */
function hostSheetAvailable() {
	return hostMetrics.cpuPct !== null || hostMetrics.memPct !== null ||
		laneAHostAvailable();   /* lane A */
}

function openHostSheet() {
	/* INERT WHEN THERE IS NOTHING TO SHOW, the rule openForecastSheet and
	   openOverflowSheet both keep: a poll can take the host block away between the
	   paint and the fingertip. */
	if (!hostSheetAvailable()) return;
	sheetGen++;
	sheetSessionId = null;
	sheetOpenState = null;
	sheetMode = 'host';
	hostSig = null;
	clearSheetTimer();
	sheetBusy = false;
	ui.sheet.classList.remove('busy');
	setSheetStatus('', '');
	ui.sheet.setAttribute('data-mode', 'host');
	ui.sheet.setAttribute('data-detail-state', '');
	ui.sheet.setAttribute('data-approval', '');
	ui.sheet.setAttribute('data-continue', '');
	setVar(ui.sheet, '--sheet-accent', 'var(--accent)');
	ui.sheet.classList.add('open');
	ui.sheet.setAttribute('aria-hidden', 'false');
	syncSheet();
	enterSheetFocus();
}

/* Follows the feed like every other sheet: a companion that stops serving `host`
   takes this view with it rather than leaving a ten-minute chart of a machine
   nobody is measuring any more. */
function syncHostSheet() {
	if (!hostSheetAvailable()) { closeSheet(); return; }
	setText(ui.sheetTitle, 'This PC');
	setText(ui.sheetRepo, 'last 10 minutes ' + EMDASH + ' sampled from the companion feed');

	var last = hostRing.length ? hostRing[hostRing.length - 1] : null;
	var sig = [hostRing.length, last ? last.t : 0, hostCount('cpu'), hostCount('mem'),
		hostMetrics.cpuPct, hostMetrics.memPct, hostMetrics.memUsedGB, hostMetrics.memTotalGB,
		laneAHostSig()].join('#');
	if (sig === hostSig) return;
	hostSig = sig;

	ui.sheetHost.textContent = '';
	appendHostChart('CPU', 'cpu', hostMetrics.cpuPct);
	appendHostChart('MEM', 'mem', hostMetrics.memPct);

	/* The GB pair earns a place HERE that it could not earn on the row: this sheet
	   has width the one-line row does not, and it is the view somebody opened to ask
	   about memory. */
	if (hostMetrics.memUsedGB !== null && hostMetrics.memTotalGB !== null) {
		ui.sheetHost.appendChild(hostNote('memory ' + hostMetrics.memUsedGB.toFixed(1) + ' / ' +
			hostMetrics.memTotalGB.toFixed(1) + ' GB'));
	}

	/* The temperatures are TEXT and never a third chart: they are not in the ring,
	   so there is no ten-minute history of them to draw, and drawing one from the
	   ring's timestamps would be charting one series against another's samples.
	   appendLaneAHostBlocks writes them; this line exists only to say so when there
	   are none, which is the absent state and not a zero. */
	if (!laneAHasTemps()) {
		var line = document.createElement('div');
		line.className = 'hs-temps';
		line.textContent = 'no hardware sensor reading';
		ui.sheetHost.appendChild(line);
	}
	appendLaneAHostBlocks();   /* lane A */
	appendSourcesBlock();      /* MF-008 */
}

/* MF-008 — WHERE EVERY NUMBER ON THIS PANEL COMES FROM, and how old it is.
   `sources` is presence-gated key by key: a companion that does not serve the block
   renders nothing here rather than a list of unknowns, and a key the block omits is
   a source this build has not been told about rather than one that is down. Each
   entry is {ok, lastAt, ageSec, note}; `note` is the companion's own words about a
   source that is not ok and is shown verbatim, because a panel that paraphrased it
   would be guessing at a fault it cannot see. */
var SOURCE_LABELS = {
	hooks: 'hooks', transcripts: 'transcripts', statusline: 'statusline',
	limitsToken: 'limits token', otlp: 'OTLP', hwinfo: 'HWiNFO', gpu: 'GPU'
};
var SOURCE_ORDER = ['hooks', 'transcripts', 'statusline', 'limitsToken', 'otlp', 'hwinfo', 'gpu'];

function appendSourcesBlock() {
	var src = lastGoodDoc && lastGoodDoc.sources;
	if (!src || typeof src !== 'object' || Array.isArray(src)) return;
	var rows = [];
	for (var i = 0; i < SOURCE_ORDER.length; i++) {
		var key = SOURCE_ORDER[i];
		if (!Object.prototype.hasOwnProperty.call(src, key)) continue;
		var v = src[key];
		if (!v || typeof v !== 'object' || Array.isArray(v)) continue;
		rows.push({ key: key, v: v });
	}
	if (!rows.length) return;
	var head = document.createElement('div');
	head.className = 'hs-head';
	head.textContent = 'Sources';
	ui.sheetHost.appendChild(head);
	for (var r = 0; r < rows.length; r++) {
		ui.sheetHost.appendChild(sourceRow(rows[r].key, rows[r].v));
	}
}

function sourceRow(key, v) {
	var row = document.createElement('div');
	row.className = 'hs-source' + (v.ok === true ? '' : ' bad');
	var name = document.createElement('span');
	name.className = 'hs-source-name';
	name.textContent = SOURCE_LABELS[key] || key;
	row.appendChild(name);
	var what = document.createElement('span');
	what.className = 'hs-source-state';
	/* A note wins over the word "fresh": the companion said something specific and
	   replacing it with a status word would throw the only detail away. */
	what.textContent = typeof v.note === 'string' && v.note ? v.note
		: (v.ok === true ? 'fresh' : 'not answering');
	row.appendChild(what);
	var age = document.createElement('span');
	age.className = 'hs-source-age';
	/* An absent age is an em-dash and never a zero: "0 s ago" would be the freshest
	   reading on the row, said about a source that stated nothing. */
	var sec = Number(v.ageSec);
	age.textContent = isFinite(sec) && sec >= 0 ? laneAAgeWords(sec) : EMDASH;
	row.appendChild(age);
	return row;
}

function hostNote(text) {
	var el = document.createElement('div');
	el.className = 'hs-note';
	el.textContent = text;
	return el;
}

function appendHostChart(label, key, nowPct) {
	var wrap = document.createElement('div');
	wrap.className = 'hs-chart';

	var head = document.createElement('div');
	head.className = 'hs-head';
	var name = document.createElement('span');
	name.className = 'hs-name';
	name.textContent = label;
	var now = document.createElement('span');
	now.className = 'hs-now';
	/* The CURRENT figure comes from hostMetrics, which is the same value the row is
	   painting — never from the ring's last entry, which is one poll old the instant
	   a render lands between polls. */
	now.textContent = nowPct === null ? EMDASH : nowPct + '%';
	var range = document.createElement('span');
	range.className = 'hs-range';
	range.textContent = '0-100%';
	head.appendChild(name);
	head.appendChild(now);
	head.appendChild(range);
	wrap.appendChild(head);

	var have = hostCount(key);
	if (have < HOST_MIN_SAMPLES) {
		/* THE HONEST STATE, and it is the point of the minimum rather than a
		   nicety: two points are a slope, not a trend, and a chart drawn from them
		   would be the panel presenting the shape of its own start-up as the shape
		   of this machine's last ten minutes. */
		wrap.appendChild(hostNote('collecting ' + EMDASH + ' ' + have + ' of ' +
			HOST_MIN_SAMPLES + ' samples'));
		ui.sheetHost.appendChild(wrap);
		return;
	}
	wrap.appendChild(buildHostPlot(key));

	var axis = document.createElement('div');
	axis.className = 'hs-axis';
	var l = document.createElement('span');
	l.textContent = '10 min ago';
	var r = document.createElement('span');
	r.textContent = 'now';
	axis.appendChild(l);
	axis.appendChild(r);
	wrap.appendChild(axis);
	ui.sheetHost.appendChild(wrap);
}

/* The plot. viewBox coordinates are a fixed 1000 x 100 stretched to the panel with
   preserveAspectRatio="none", so the x axis is TIME and not sample index — a run of
   missed polls therefore leaves a real hole of the right width rather than being
   squeezed out by the samples that did arrive.
   The y axis is a FIXED 0..100%, never the series' own peak: a machine that idled
   all ten minutes must read as a flat line near the floor, and auto-scaling would
   render 2% of noise as a mountain range. The head says "0-100%" so the scale is
   stated rather than assumed. */
function buildHostPlot(key) {
	var W = 1000, H = 100;
	var svg = document.createElementNS(SVG_NS, 'svg');
	svg.setAttribute('class', 'hs-plot');
	svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
	svg.setAttribute('preserveAspectRatio', 'none');
	svg.setAttribute('aria-hidden', 'true');
	svg.setAttribute('focusable', 'false');

	var half = document.createElementNS(SVG_NS, 'line');
	half.setAttribute('class', 'hs-grid');
	half.setAttribute('x1', '0');
	half.setAttribute('x2', String(W));
	half.setAttribute('y1', String(H / 2));
	half.setAttribute('y2', String(H / 2));
	svg.appendChild(half);

	var now = Date.now();
	var t0 = now - HOST_WINDOW_MS;
	function px(t) { return Math.max(0, Math.min(W, ((t - t0) / HOST_WINDOW_MS) * W)); }
	function py(v) { return H - (Math.max(0, Math.min(100, v)) / 100) * H; }

	var runs = hostRuns(key);
	for (var i = 0; i < runs.length; i++) {
		var run = runs[i];
		if (run.length === 1) {
			/* A lone sample between two gaps is still a reading somebody took, and a
			   one-point polyline paints nothing at all. */
			var dot = document.createElementNS(SVG_NS, 'circle');
			dot.setAttribute('class', 'hs-dot');
			dot.setAttribute('cx', px(run[0].t).toFixed(1));
			dot.setAttribute('cy', py(run[0][key]).toFixed(1));
			dot.setAttribute('r', '2');
			svg.appendChild(dot);
			continue;
		}
		var pts = [];
		for (var j = 0; j < run.length; j++) {
			pts.push(px(run[j].t).toFixed(1) + ',' + py(run[j][key]).toFixed(1));
		}
		var line = document.createElementNS(SVG_NS, 'polyline');
		line.setAttribute('class', 'hs-line');
		line.setAttribute('points', pts.join(' '));
		svg.appendChild(line);
	}
	return svg;
}

/* The row's tap. Routed on the ROW rather than on a cell, so a fingertip anywhere
   along it reaches the same view — the whole row is the target, the way the whole
   gauge is. */
function onSensorsClick() {
	openHostSheet();
}

/* ------------------------------------------------ touch diagnostics (v0.23.0) */

/* WHY THIS EXISTS. The operator reported that touch "doesn't seem to work" on the
   physical display — and yet panel approvals were tapped and landed live. Both of
   those are true at once only if SOMETHING arrives and something else does not. The
   retired host forwarded "widget click handling", which would make a tap a
   synthesized CLICK and leave every gesture on this panel — swipe, long press,
   two-finger tap, pull-to-refresh — reading an event stream that is not there. The
   panel host does not have that limitation, which is one of the things this capture
   layer is now for: proving it.
   THAT IS A HYPOTHESIS AND NOBODY HAS MEASURED IT — do not build on the paragraph
   above. This is the instrument; a later wave rebuilds gestures on what it records.

   THE INSTRUMENT MUST NOT PERTURB THE THING IT MEASURES, and that is the whole
   design constraint. Every listener is installed at DOCUMENT level, CAPTURE phase,
   PASSIVE. Nothing here calls preventDefault or stopPropagation, ever; nothing here
   reads back into any render path. Capture phase so a record exists even for an
   event some handler downstream consumes, passive so the browser never has to wait
   on this code before scrolling or synthesizing, document level because the panel's
   own gesture layer is already there and a second surface would be a second thing
   that can disagree about what happened.
   OFF REMOVES THE LISTENERS — it does not mute them. A muted listener is still a
   listener the compositor has to consider, and "the diagnostics were off" would
   stop being a statement about the panel's event handling. */

var DIAG_RING_MAX = 400;       /* the in-page ring, and the cap on unshipped lines */
var DIAG_POST_MAX = 50;        /* crabd 0.24.0: at most 50 lines per POST */
var DIAG_LINE_MAX = 300;       /* crabd 0.24.0: at most 300 chars per line */
/* The coalescing window for move floods. A finger dragging across this glass emits
   a pointermove per frame; at 60 Hz an unfiltered swipe is ~40 lines and buries the
   down/up that bracket it. 100 ms keeps roughly six samples a second, which is
   enough to see SHAPE (does the stream exist at all, does it move, does it stop)
   without the shape being the only thing in the log. */
var DIAG_MOVE_MS = 100;
var DIAG_PATH = '/v1/panel-log';
/* The largest count the indicator will PAINT — a width budget, not a cap on what
   is counted or logged. See renderDiagChip. */
var DIAG_COUNT_SHOWN_MAX = 999999;

/* The fixed vocabulary. One row per event type: the DOM name, the token that goes
   on the wire, and what kind of record it is — 'p' pointer, 'm' mouse, 't' touch,
   'x' plain. Written as a table rather than as a switch so the set of things this
   layer listens to is one readable list, and so install and remove iterate the
   SAME list: a remove that walked a different list from the install is how a
   listener survives an "off". */
var DIAG_EVENTS = [
	['pointerdown', 'pdown', 'p'], ['pointermove', 'pmove', 'p'],
	['pointerup', 'pup', 'p'], ['pointercancel', 'pcancel', 'p'],
	['mousedown', 'mdown', 'm'], ['mousemove', 'mmove', 'm'], ['mouseup', 'mup', 'm'],
	['click', 'click', 'm'], ['dblclick', 'dblclick', 'm'],
	['touchstart', 'tstart', 't'], ['touchmove', 'tmove', 't'],
	['touchend', 'tend', 't'], ['touchcancel', 'tcancel', 't'],
	['contextmenu', 'ctxmenu', 'm'], ['wheel', 'wheel', 'x']
];
/* The three move types, by wire token — the only ones that are coalesced. */
var DIAG_MOVES = { pmove: 1, mmove: 1, tmove: 1 };

var diagOn = false;            /* whether the capture layer is INSTALLED */
var diagBound = null;          /* [type, handler] pairs actually added, for removal */
var diagRing = [];             /* the last DIAG_RING_MAX lines, for a reader in-page */
var diagQueue = [];            /* lines not yet shipped */
var diagDropped = 0;           /* lines the queue cap threw away, reported on the next post */
var diagCount = 0;             /* EVERY input event seen, coalesced ones included */
var diagT0 = 0;                /* the instant capture started; every stamp is relative to it */
var diagStreams = null;        /* wire token + stream id -> the coalescing record */
var diagBusy = false;          /* one POST in flight at a time */
var diagUnsupported = false;   /* 404 latch: this crabd has no /v1/panel-log */
var diagForced = false;        /* dev-only &touchdiag=1, mock mode only */

/* The setting, the flag, or neither. Read live on every call - a save can move the
   switch under a running panel, and applyProperties() is what notices. */
function diagWanted() {
	if (mockName && diagForced) return true;
	return boolProp('touchDiag', false);
}

/* The reconcile. Called from applyProperties, which runs on every accepted save,
   and once at boot. Idempotent by construction: it compares the wanted
   state to the installed state and returns when they agree, so a colour change
   cannot tear down and rebuild the capture layer. */
function syncDiag() {
	var want = diagWanted();
	if (want === diagOn) return;
	if (want) installDiag(); else removeDiag();
	renderDiagChip();
}

function installDiag() {
	if (diagOn) return;
	diagOn = true;
	diagT0 = Date.now();
	diagRing = [];
	diagQueue = [];
	diagDropped = 0;
	diagCount = 0;
	diagStreams = {};
	diagBound = [];
	for (var i = 0; i < DIAG_EVENTS.length; i++) {
		(function (row) {
			var handler = function (ev) { diagRecord(row[1], row[2], ev); };
			/* capture:true AND passive:true, on both sides of the pair. The options
			   object is what removeEventListener matches on for `capture`; passing a
			   different shape to remove is the classic way a listener outlives its
			   own teardown, so the pair is written once here and reused below. */
			document.addEventListener(row[0], handler, { capture: true, passive: true });
			diagBound.push([row[0], handler]);
		})(DIAG_EVENTS[i]);
	}
	diagLine('diag on ' + DIAG_EVENTS.length + ' listeners');
	logLine('touch diagnostics ON (' + DIAG_EVENTS.length + ' capture listeners)');
}

function removeDiag() {
	if (!diagOn) return;
	/* Flush whatever the streams were holding BEFORE the listeners go, or the last
	   move of the operator's last gesture is the one sample the log never carries —
	   which is the sample that says whether the stream ended or merely stopped. */
	diagFlushStreams();
	diagLine('diag off');
	if (diagBound) {
		for (var i = 0; i < diagBound.length; i++) {
			document.removeEventListener(diagBound[i][0], diagBound[i][1], { capture: true });
		}
	}
	diagBound = null;
	diagStreams = null;
	diagOn = false;
	logLine('touch diagnostics OFF (listeners removed)');
}

/* The stamp. Relative seconds since capture started, three decimals — because the
   question this instrument answers is about INTERVALS (a 600 ms hold, a move stream
   that stops 40 ms before an up) and a wall clock makes every reader subtract. */
function diagStamp(nowMs) {
	var s = (nowMs - diagT0) / 1000;
	return '+' + (s < 0 ? 0 : s).toFixed(3);
}

function diagXY(ev) {
	var x = ev && typeof ev.clientX === 'number' ? Math.round(ev.clientX) : null;
	var y = ev && typeof ev.clientY === 'number' ? Math.round(ev.clientY) : null;
	if (x === null || y === null) return '';
	return ' (' + x + ',' + y + ')';
}

/* The whole record for one event, as one compact line. Every field is READ OFF THE
   EVENT and never inferred: an absent pointerType is left absent rather than
   guessed at, because "what did the glass actually send" is the entire question. */
function diagDescribe(token, kind, ev) {
	var s = token;
	if (kind === 'p') {
		s += ' ' + (ev.pointerType || '?');
		s += ' p' + (ev.pointerId === undefined ? '?' : ev.pointerId);
		s += diagXY(ev);
		if (ev.isPrimary) s += ' prim';
		if (typeof ev.button === 'number') s += ' b' + ev.button;
		if (typeof ev.buttons === 'number' && ev.buttons !== 0) s += ' bs' + ev.buttons;
	} else if (kind === 't') {
		/* touches.length is the two-finger question, and it is the reason the touch
		   family is captured at all beside the pointer family: a panel that sends
		   pointer events for one finger and nothing for two would look identical to
		   one that sends neither, if only the pointer stream were watched. */
		var n = ev.touches && typeof ev.touches.length === 'number' ? ev.touches.length : '?';
		var ch = ev.changedTouches && ev.changedTouches.length ? ev.changedTouches[0] : null;
		s += ' x' + n;
		if (ch && typeof ch.clientX === 'number') s += ' (' + Math.round(ch.clientX) + ',' + Math.round(ch.clientY) + ')';
	} else if (kind === 'm') {
		s += diagXY(ev);
		if (typeof ev.button === 'number') s += ' b' + ev.button;
		if (typeof ev.buttons === 'number' && ev.buttons !== 0) s += ' bs' + ev.buttons;
		if (token === 'click' && typeof ev.detail === 'number') s += ' d' + ev.detail;
	} else {
		s += diagXY(ev);
		if (typeof ev.deltaX === 'number') s += ' w' + Math.round(ev.deltaX) + ',' + Math.round(ev.deltaY);
	}
	return s;
}

/* The stream key for a move. Pointer moves are per-pointerId because two fingers
   are two streams and merging them would report one flood of double the rate;
   mouse and touch moves each have exactly one stream by definition. */
function diagStreamKey(token, ev) {
	return token === 'pmove' ? 'pmove:' + ev.pointerId : token;
}

/* Every captured event lands here. THIS FUNCTION IS THE PASSIVITY CONTRACT: it
   reads the event, appends a string, and returns. It calls nothing that renders,
   nothing that fetches, and nothing on the event but property reads. */
function diagRecord(token, kind, ev) {
	if (!diagOn) return;
	diagCount++;
	var now = Date.now();
	if (DIAG_MOVES[token]) {
		var key = diagStreamKey(token, ev);
		var st = diagStreams[key];
		if (!st) {
			/* FIRST of a stream, always emitted: whether a move stream exists at all
			   is the headline finding this instrument was built for. */
			diagStreams[key] = { at: now, n: 0, pending: null };
			diagLine(diagDescribe(token, kind, ev), now);
			return;
		}
		st.n++;
		if (now - st.at >= DIAG_MOVE_MS) {
			diagLine(diagDescribe(token, kind, ev) + ' coalesced ' + st.n, now);
			st.at = now;
			st.n = 0;
			st.pending = null;
		} else {
			/* Held, not dropped. If the stream ends before the next window opens this
			   is the LAST move, and the last move is where a gesture's release lives. */
			st.pending = { text: diagDescribe(token, kind, ev), at: now };
		}
		return;
	}
	/* A non-move on a pointer id ends that pointer's move stream. Flushed BEFORE the
	   line for the event itself, so the log reads in the order the fingertip made
	   it: …move, last move, up. */
	if (token === 'pup' || token === 'pcancel') diagFlushStream('pmove:' + ev.pointerId);
	else if (token === 'mup') diagFlushStream('mmove');
	else if (token === 'tend' || token === 'tcancel') diagFlushStream('tmove');
	diagLine(diagDescribe(token, kind, ev), now);
}

function diagFlushStream(key) {
	if (!diagStreams) return;
	var st = diagStreams[key];
	if (!st) return;
	if (st.pending) diagLine(st.pending.text + ' coalesced ' + st.n + ' last', st.pending.at);
	delete diagStreams[key];
}

function diagFlushStreams() {
	if (!diagStreams) return;
	for (var k in diagStreams) if (Object.prototype.hasOwnProperty.call(diagStreams, k)) diagFlushStream(k);
}

/* One line into the ring and the ship queue. Truncated to the contract's 300 chars
   HERE rather than at post time, so the line a reader sees in-page is the line
   crabd was sent — a log that disagreed with itself about what it recorded would be
   the one thing worse than no log. */
function diagLine(text, atMs) {
	var line = diagStamp(atMs === undefined ? Date.now() : atMs) + ' ' + text;
	if (line.length > DIAG_LINE_MAX) line = line.slice(0, DIAG_LINE_MAX - 1) + '…';
	diagRing.push(line);
	if (diagRing.length > DIAG_RING_MAX) diagRing.shift();
	diagQueue.push(line);
	/* The queue is capped for the same reason the ring is: a panel whose companion
	   is dead must not grow a buffer for as long as diagnostics are on. The OLDEST
	   go, and the count of them rides the next post — a silent drop would make a
	   gap in the log indistinguishable from a gap in the events. */
	while (diagQueue.length > DIAG_RING_MAX) { diagQueue.shift(); diagDropped++; }
	try { window.__sidecrabDiagLog = diagRing; } catch (e) {}
}

/* Shipping, once per poll cycle. Called from poll() rather than from a timer of its
   own: the flush rate is the panel's own heartbeat, and a second timer would be a
   second thing to reason about when the log arrives in the wrong order. */
function diagFlush() {
	/* DELIBERATELY NOT GATED ON diagOn. Turning diagnostics off stops CAPTURE; it
	   does not un-record what was already captured, and the lines still in the queue
	   at that moment are the last three seconds of the session plus the final flush
	   of every open move stream — which is to say, the operator's LAST gesture, the
	   one they walked back to the keyboard right after making. Gating this on diagOn
	   silently threw exactly that away. The queue drains over the polls after the
	   switch and is then a length check per poll forever. */
	if (diagBusy || diagUnsupported) return;
	if (!diagQueue.length) return;
	var batch = diagQueue.slice(0, DIAG_POST_MAX);
	if (diagDropped) {
		/* Stated in the batch it belongs to, not counted somewhere only a debugger
		   can reach. */
		batch = batch.slice(0, DIAG_POST_MAX - 1);
		batch.push(diagStamp(Date.now()) + ' diag dropped ' + diagDropped + ' unsent lines (queue full)');
	}
	var take = diagDropped ? batch.length - 1 : batch.length;
	diagBusy = true;
	postPanelLog(batch).then(function (res) {
		diagBusy = false;
		if (res.status === 204 || res.status === 200) {
			diagQueue.splice(0, take);
			diagDropped = 0;
			return;
		}
		if (res.status === 404) {
			/* THE LATCH, and it is 404 and nothing else. A 404 is the endpoint saying
			   it does not exist, which is a fact about this crabd; a 400 is this
			   widget sending something crabd disliked, which is a fact about a batch.
			   Latching on the second would hide a real capability because one line
			   was malformed, and the operator would be poking glass into a void. */
			diagUnsupported = true;
			diagQueue = [];
			diagDropped = 0;
			logLine('panel log unsupported by this crabd (HTTP 404)');
			return;
		}
		/* Any other status: drop THIS batch and keep going. Re-sending a body crabd
		   has already refused would wedge the queue behind it forever. */
		diagQueue.splice(0, take);
		diagDropped = 0;
		logLine('panel log rejected (HTTP ' + res.status + ')');
	}).catch(function () {
		/* NOT latched and NOT dropped. A dead socket is a fact about this moment;
		   the lines stay at the head of the queue and go on the next poll. */
		diagBusy = false;
	});
}

/* In mock mode the flush LOGS instead of posting — the idiom postAction and
   postConfig already keep, for the same reason: a dev browser has no crabd, and a
   flush that silently failed would look exactly like a capture layer that recorded
   nothing. */
function postPanelLog(lines) {
	var payload = JSON.stringify({ lines: lines });
	if (mockName) {
		return new Promise(function (resolve) {
			setTimeout(function () {
				logLine('mock POST ' + DIAG_PATH + ' ' + lines.length + ' lines');
				for (var i = 0; i < lines.length; i++) logLine('  ' + lines[i]);
				resolve({ status: 204 });
			}, 40);
		});
	}
	return postJson(DIAG_PATH, payload);
}

/* THE INDICATOR, and its placement is measured rather than chosen.

   The identity zone is a flex COLUMN whose .crab-wrap is flex:1 1 auto, so free
   space in that zone IS the crab — the v0.22.0 moon-chip measurement, unchanged and
   re-taken on HEAD this session: the painted crab has 0.8 px of height slack at
   2560x720 and ZERO at 2536x696, 840x344 and 840x696, where it is height-limited
   outright. So this takes no part in layout either, and it spends the one piece of
   slack that zone genuinely has: the clock row is justify-content:center, so the
   room to the LEFT of the hours mirrors the room to the right the moon chip already
   lives in. Measured on HEAD at the five slots: 56.9 / 61.3 / 54.3 / 223.8 / 90.8 px
   of clear column left of `clockHm`.
   The content is sized to the tightest of those: the word and the count are STACKED
   the way the moon chip stacks its mode and its remaining time, so the budget is a
   4-character mono string rather than a sentence — 35.2 px at 2560x720 against
   56.9 available, 16.8 against 54.3 at 840x344. `diag: N events` in full does NOT
   fit (160.9 px against 56.9) and rides on title/aria-label instead, which is the
   sensor-name idiom one zone over.
   pointer-events:none, because this is an indicator and not a control — and because
   an instrument that could swallow one of the taps it exists to record would be
   lying about the thing it measures. */
function renderDiagChip() {
	if (!ui.diagChip) return;
	if (ui.diagChip.classList.contains('shown') !== diagOn) ui.diagChip.classList.toggle('shown', diagOn);
	if (!diagOn) return;
	/* CLAMPED, and the number is measured rather than assumed. The width budget is
	   56.9 px at the authored slot and the glyphs are ~8.8 px, so five characters
	   (47.3 px, 9.6 px clear) fit and six do not — and `fmtNum` runs to six on a
	   count past a million ("999.9M") and seven past a billion. Above the clamp the
	   chip says `999k+`: at that point the exact figure is not what the operator is
	   reading anyway, the aria-label and title still carry it in full, and a chip
	   that grew past its own budget would sit on top of the clock. */
	setText(ui.diagCount, diagCount > DIAG_COUNT_SHOWN_MAX ? '999k+' : fmtNum(diagCount));
	var label = 'diag: ' + diagCount + ' events';
	if (ui.diagChip.getAttribute('aria-label') !== label) {
		ui.diagChip.setAttribute('aria-label', label);
		ui.diagChip.setAttribute('title', label);
	}
}

/* Repainted on the 1 Hz tick, never on the events themselves. A counter that
   re-rendered per pointermove would put a DOM write inside the capture path, which
   is exactly the perturbation this layer promises not to be. */
function tickDiagChip() {
	if (!diagOn) return;
	renderDiagChip();
}

/* --------------------------------------------------------------- mock harness */

var ISO_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$/;

function toLocalIso(d) {
	return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + 'T' +
		pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
}

/* Mock fixtures are contract-shaped with fixed timestamps, so every ISO string
   is shifted by one delta. Without this every fixture is instantly stale and
   only the stale state is ever reachable. */
function rebaseMock(doc, targetAgeMs) {
	var base = Date.parse(doc && doc.generatedAt);
	if (!isFinite(base)) return doc;
	var delta = (Date.now() - targetAgeMs) - base;

	function shift(v) {
		var t = Date.parse(v);
		if (!isFinite(t)) return v;
		var d = new Date(t + delta);
		return /(Z|[+-]\d{2}:?\d{2})$/.test(v) ? d.toISOString() : toLocalIso(d);
	}
	function walk(node) {
		if (Array.isArray(node)) {
			for (var i = 0; i < node.length; i++) {
				if (typeof node[i] === 'string' && ISO_RE.test(node[i])) node[i] = shift(node[i]);
				else walk(node[i]);
			}
		} else if (node && typeof node === 'object') {
			for (var k in node) {
				if (!Object.prototype.hasOwnProperty.call(node, k)) continue;
				if (typeof node[k] === 'string' && ISO_RE.test(node[k])) node[k] = shift(node[k]);
				else walk(node[k]);
			}
		}
	}
	walk(doc);
	pinMockResets(doc);
	pinMockQuietUntil(doc);
	applyAgeOverride(doc);
	applyHoldOverride(doc);
	applyBudgetOverride(doc);
	applyMockQuietOverride(doc);
	return doc;
}

/* Mock only: PIN a fixture's own `quiet.override.until`, for exactly the reason
   pinMockResets pins a reset — this is that trap in a third place. rebaseMock
   recomputes its delta from the fixture's FIXED generatedAt on every poll, so an
   `until` shifted that way is re-pinned to "now plus the fixture's offset" three
   times a second and the remaining time reads the same figure forever. The chip's
   whole job is a countdown, so a fixture that could not count down would be
   photographing a clock. */
function pinMockQuietUntil(doc) {
	var q = doc && doc.quiet;
	if (!q || typeof q !== 'object' || Array.isArray(q)) return;
	var ov = q.override;
	if (!ov || typeof ov !== 'object' || Array.isArray(ov) || !ov.until) return;
	if (mockQuietUntilPin === null) mockQuietUntilPin = ov.until;
	else ov.until = mockQuietUntilPin;
}

/* Mock only: what an accepted quiet write did to the harness's daemon. The instant
   is absolute and computed once, so it runs down in real time from here — the
   &age= / &hold= discipline, in a fourth place. */
function applyMockQuietWrite(q) {
	mockQuietUntilPin = null;
	mockQuietOv = q.mode === 'auto'
		? { mode: 'auto', until: null }
		: { mode: q.mode, until: Date.now() + q.minutes * 60000 };
}

/* Mock only: serve the override the harness is currently holding — set either by
   the dev flag or by a tap that the stub accepted. Null means the harness has
   nothing to say and the FIXTURE'S OWN value stands, which is what keeps the three
   documents that carry an `override` member rendering from their own contents. */
function applyMockQuietOverride(doc) {
	if (mockQuietOv === null) return;
	if (!doc.quiet || typeof doc.quiet !== 'object' || Array.isArray(doc.quiet)) {
		/* A fixture with no quiet block at all (schema 1) still has to be able to
		   carry an override, because an override is exactly what an operator with no
		   quiet hours configured would reach for. crabd would serve the block once
		   one existed; so does this. */
		doc.quiet = { active: false, start: null, end: null };
	}
	if (mockQuietOv.mode === 'auto' || mockQuietOv.mode === 'none') {
		doc.quiet.override = null;
		return;
	}
	doc.quiet.override = { mode: mockQuietOv.mode, until: new Date(mockQuietOv.until).toISOString() };
	/* crabd's effective answer honours the override (frozen contract), and so does
	   the harness — otherwise the panel's dim would not follow the tap and the
	   screenshot would be of a widget that does not exist. */
	doc.quiet.active = mockQuietOv.mode === 'on';
}

/* Dev-only, mock mode only: &hold=<seconds> restates every pendingPermission's
   requestedAt so the approval countdown starts with that many seconds left.
   The instant is PINNED on first use, the same discipline &age= keeps and for
   the same reason: recomputing it every poll would hold the number still and
   the thing being photographed is a countdown. From the pin it runs down in
   real time and reaches "expired" by itself, which is the second shot.
   Bounded by APPROVAL_HOLD_SEC on the way in — a hold longer than crabd's own
   would be a fixture the daemon could not have produced. */
function applyHoldOverride(doc) {
	if (holdOverrideSec === null) return;
	var want = Math.max(0, Math.min(APPROVAL_HOLD_SEC, holdOverrideSec));
	if (holdAnchorAt === null) holdAnchorAt = Date.now() - (APPROVAL_HOLD_SEC - want) * 1000;
	var iso = new Date(holdAnchorAt).toISOString();
	var sessions = doc && Array.isArray(doc.sessions) ? doc.sessions : [];
	for (var i = 0; i < sessions.length; i++) {
		var p = sessions[i] && sessions[i].pendingPermission;
		if (p && typeof p === 'object' && !Array.isArray(p)) p.requestedAt = iso;
	}
}

/* Dev-only, mock mode only: &budget=<percent> puts the day at that percentage of
   its budget, so the amber (100%) and red (150%) steps can be photographed
   without a fixture per step.
   It moves the BUDGET, not the spend — dailyOutputTokens is recomputed from the
   day's real output so the fixture stays self-consistent, exactly as &age= moves
   lastActivityAt along with stateSince. Rewriting todayPct on its own would have
   the panel report a percentage its own numbers contradict, which is the one
   thing a fixture built to prove a percentage must not do; and the marker moves
   with it, which is half of what there is to photograph.
   Recomputed rather than pinned because it is a pure function of the fixture's
   own fixed output total: rebaseMock shifts timestamps, never figures. */
function applyBudgetOverride(doc) {
	if (budgetPctOverride === null) return;
	var burn = doc && doc.burn;
	var out = burn && burn.today ? burn.today.outputTokens : null;
	if (typeof out !== 'number' || !isFinite(out) || out <= 0) return;
	var pct = budgetPctOverride / 100;
	/* Clamped to the contract's own range, so the stand-in stays a document crabd
	   could actually have served. */
	var perDay = Math.max(100000, Math.min(100000000, Math.round(out / pct)));
	burn.budget = { dailyOutputTokens: perDay, todayPct: Math.min(9.99, out / perDay) };
}

/* Mock only: PIN the rebased reset instants on the first document that carries
   them, and re-serve those instants on every later poll.

   rebaseMock recomputes its delta from the fixture's FIXED generatedAt on every
   poll, so a resetsAt shifted that way is re-pinned to "now plus the fixture's
   offset" three times a second — and a countdown built on it would read the same
   figure forever. This is the v0.3.0 age-override trap in a second place: a
   rolling fixture value silently makes the panel misreport the very behaviour
   the fixture exists to show.
   Pinning once changes nothing about what a fixture renders on the first frame;
   it only lets the clock actually run down from there, which is the only way the
   minute boundary is observable off-glass. Applied to every fixture rather than
   by name: the alternative is a fixture-name branch that decides which mocks are
   allowed to tell the truth. */
var mockResetPins = {};

function pinMockResets(doc) {
	var limits = doc && doc.limits;
	if (!limits || typeof limits !== 'object') return;
	var wins = [{ k: 'fiveHour', w: limits.fiveHour }, { k: 'weekly', w: limits.weekly }];
	var extra = Array.isArray(limits.extra) ? limits.extra : [];
	for (var i = 0; i < extra.length; i++) wins.push({ k: 'extra' + i, w: extra[i] });
	for (var j = 0; j < wins.length; j++) {
		var w = wins[j].w;
		if (!w || typeof w !== 'object' || !w.resetsAt) continue;
		if (mockResetPins[wins[j].k] === undefined) mockResetPins[wins[j].k] = w.resetsAt;
		else w.resetsAt = mockResetPins[wins[j].k];
		/* exhaustAt (v0.13.0) is pinned the same way and for the same reason: left
		   to rebaseMock it would drift forward 3 s per poll while its pinned
		   resetsAt stayed put, silently sliding a near-future forecast across the
		   reset guard the fixture exists to demonstrate. Pinned only when present —
		   a null/absent exhaustAt has nothing to freeze. */
		if (w.exhaustAt) {
			var ek = wins[j].k + '_exhaust';
			if (mockResetPins[ek] === undefined) mockResetPins[ek] = w.exhaustAt;
			else w.exhaustAt = mockResetPins[ek];
		}
	}
}

/* Mock only: the fixture's own /v1/config stub, so the per-key 400 path can be
   demoed without an older crabd to POST at. A fixture may carry
   `"_mock": { "config400": ["toast"] }` — the underscore says it is harness
   scaffolding and not contract, and nothing in the render path reads it (the
   contract's rule that unknown top-level keys are ignored is what makes that
   safe). Any key not listed answers 204, which is what proves the 400 is
   per-KEY: quiet hours still writes while toast is refused.

   v0.16.0 adds the SUB-MEMBER form, `"toast.approvalThresholdSec"`: 400 only when
   the body for that key carries that member, 204 otherwise. That is the crabd
   between 0.7.0 and 0.15.0 — it knows `toast` and has never heard of the optional
   third member — and it is the pairing an operator actually lands in, because the
   widget updates by console import while crabd updates by redeploy. Without it
   the member's fallback path had no way to be exercised off-glass. */
function mockConfigStatus(payload) {
	var stub = lastGoodDoc && lastGoodDoc._mock ? lastGoodDoc._mock.config400 : null;
	if (!Array.isArray(stub)) return 204;
	var key = null, body = null;
	try {
		body = JSON.parse(payload);
		for (var k in body) {
			if (Object.prototype.hasOwnProperty.call(body, k)) { key = k; break; }
		}
	} catch (e) { return 400; }
	if (stub.indexOf(key) !== -1) return 400;
	var member = body[key];
	if (member && typeof member === 'object' && !Array.isArray(member)) {
		for (var mk in member) {
			if (!Object.prototype.hasOwnProperty.call(member, mk)) continue;
			if (stub.indexOf(key + '.' + mk) !== -1) return 400;
		}
	}
	return 204;
}

/* Dev-only, mock mode only: &pin=<id|prefix> pre-pins one session so the sorted
   card and its glyph can be photographed without a tap, the same reason
   &celebrate=1 holds a mood and &age= back-dates a question.

   It pins IN MEMORY and never calls savePrefs: a screenshot flag that wrote to
   the vendor store would leave the operator's own pin map holding a session
   that only ever existed in a fixture. Applied once — after that the map is the
   session's own, so a later Unpin in the same page really does unpin. */
function applyPinOverride(doc) {
	if (!pinAuto) return;
	var sessions = doc && Array.isArray(doc.sessions) ? doc.sessions : [];
	if (!sessions.length) return;
	var target = pinAuto;
	pinAuto = null;
	for (var i = 0; i < sessions.length; i++) {
		var s = sessions[i];
		if (!s || !s.id) continue;
		if (target === 'first' || s.id === target || String(s.id).indexOf(target) === 0) {
			pinned[String(s.id)] = Date.now();
			return;
		}
	}
}

/* Dev-only, mock mode only: &age=<minutes> back-dates every unacked
   needs_input stateSince so the 5 / 15 minute escalation tiers can be
   photographed without waiting them out. lastActivityAt moves with it, because
   a session that has been waiting 16 minutes has not been active for 20
   seconds — an inconsistent fixture would prove the wrong thing. */
function applyAgeOverride(doc) {
	if (ageOverrideMin === null) return;
	/* Pinned once, not recomputed per poll. A rolling stateSince changes on every
	   fetch, and pruneAcks drops an optimistic ack whose stateSince has moved —
	   so the rolling version silently un-acked the card a poll after the tap and
	   made the dev flag misreport the ack path. */
	if (ageOverrideAt === null) ageOverrideAt = Date.now() - ageOverrideMin * 60000;
	var when = new Date(ageOverrideAt).toISOString();
	var sessions = doc && Array.isArray(doc.sessions) ? doc.sessions : [];
	for (var i = 0; i < sessions.length; i++) {
		if (!sessions[i] || sessions[i].state !== 'needs_input') continue;
		sessions[i].stateSince = when;
		sessions[i].lastActivityAt = when;
	}
}

/* ------------------------------------------------------------------- start-up */

function tick() {
	var now = new Date();
	var use24 = use24Clock();
	setText(ui.clockHm, fmtClock(now, use24));
	setText(ui.clockSs, pad2(now.getSeconds()));
	setText(ui.clockDate, fmtDate(now, use24));
	tickAges(now.getTime());
	/* The approval hold on the OPEN sheet. The cards' copies ride tickAges; this
	   one is a single element outside the grid, so it gets its own line. */
	tickSheetApproval(now.getTime());
	/* lane C: the Detail page's elapsed figure, its event ages and its own copy of
	   the approval hold. Same reason the line above it is here: the hold is the
	   answer to whether the button under a thumb still reaches anything. */
	laneCTickDetail(now.getTime());
	/* The gauge countdowns move between polls too, and a minute boundary crossed
	   three seconds late is the one thing a countdown must not do. */
	tickResets(now.getTime(), use24);
	/* The quiet override's remaining time, same reason (v0.22.0), plus the edge
	   where it runs out — which has to be noticed on the second it happens, or the
	   chip goes on saying "quiet" for up to a poll after the panel has stopped
	   being quiet. */
	tickMoonChip(now.getTime());
	/* The diagnostics counter, on the TICK and never on the events it counts
	   (v0.23.0): a DOM write inside the capture path would be the instrument
	   perturbing the thing it measures, which is the one thing it may not do. */
	tickDiagChip();
	/* The tiers are what make the 1 Hz tick load-bearing: a question crosses 5
	   or 15 minutes between polls, and the panel has to notice on the second it
	   happens, not on the next poll. */
	applyEscalation(now.getTime(), document.body.classList.contains('quiet'));
	/* Catch a feed that goes stale between polls without waiting for the next one. */
	if (everHadData && !document.body.classList.contains('stale') && computeStatus() === 'stale') render();
	/* SCA-019: and catch a stream that is still OPEN and has stopped saying
	   anything. Here rather than on the poll cycle because the poll is exactly what
	   an open stream suppresses. */
	sseLivenessCheck();
}

/* ==== lane B: the push transport, the settings sheet and the chime ==== */

/* ---- lane B: push transport (server-sent events) ---- */

/* crabd 0.31.0+ serves GET /v1/events, and in the STANDALONE host that stream is
   this page's transport: every new snapshot arrives as it is published instead of
   up to POLL_MS after it. The poll is not replaced, it is demoted to the fallback —
   it runs whenever the stream is not open, and every document from either route
   goes through the SAME acceptDoc, so the schema check, the generatedAt check and
   the stale/dead-feed rendering cannot fork between the two.

   The stream is wanted wherever EventSource exists and a fixture is not in play;
   a mock run is a file, not a stream. */
var SSE_PATH = '/v1/events';
var SSE_RETRY_MIN_MS = 3000;
var SSE_RETRY_MAX_MS = 30000;
/* SCA-019 — THE LIVENESS DEADLINE. How long a stream the browser still calls OPEN
   may say nothing at all before this page stops believing it. It exists because
   readyState is the BROWSER's opinion of the socket and says nothing about whether
   the companion is still publishing: a stream held open by something in the middle,
   or by a companion whose publisher thread has stopped, reads as perfectly healthy
   from in here. Before this, such a stream suppressed the fallback poll for ever
   and the panel could only go stale and stay stale.
   THE NUMBER, and the replay behind it. Measured read-only against a live
   companion over a 75 s window: 37 frames, all of them `state`, median gap 2090 ms,
   p90 2313 ms, worst 2619 ms. The companion publishes a snapshot on a 2 s cadence
   and, when a whole interval passes with nothing to publish, a `ping` at 15 s
   (SSE_PING_SEC). So the largest gap a HEALTHY companion can produce is 15 s, and
   45 s is three consecutive missed pings - it cannot fire on a healthy night, which
   is the test every gate on this panel has to pass before it ships. Raising the
   companion's ping interval is what would move this number; nothing else. */
var SSE_LIVENESS_MS = 45000;
var sseSource = null;
var sseRetryMs = SSE_RETRY_MIN_MS;
var sseRetryTimer = null;

/* The diagnostic the panel-log and a devtools session both read. NOT a bare global
   and not a property name: `__sidecrabTransport` is assigned onto window, so the
   0.27.1 collision cannot happen to it: the retired host injected every setting as
   a same-named lexical global, so a top-level declaration sharing a name was a parse
   error and a blank panel. Nothing injects globals now, and nothing may start. */
function transportDiag() {
	if (typeof window === 'undefined') return null;
	if (!window.__sidecrabTransport) window.__sidecrabTransport = { mode: 'poll', lastEventAt: null };
	return window.__sidecrabTransport;
}

function setTransportMode(mode, why) {
	var d = transportDiag();
	if (!d || d.mode === mode) return;
	d.mode = mode;
	logLine('transport: ' + mode + (why ? ' (' + why + ')' : ''));
}

function noteTransportEvent() {
	var d = transportDiag();
	if (d) d.lastEventAt = Date.now();
}

/* EventSource.OPEN is 1. Read off readyState rather than a flag of our own: the
   browser owns the connection's state and a second copy of it can disagree.
   DELIVERING IS NOT THE SAME AS OPEN (SCA-019). An open stream that has said
   nothing past SSE_LIVENESS_MS is not delivering, and saying so here is what lets
   the fallback poll resume at the next interval rather than at the next reconnect. */
function sseDelivering() {
	if (!sseSource || sseSource.readyState !== 1) return false;
	/* lane N: a ping is liveness, not delivery - see laneNStateStarved. */
	if (laneNStateStarved()) return false;
	return !sseSilent();
}

/* True once an open stream has been quiet past the deadline. `lastEventAt` is
   seeded at OPEN, so a stream that connects and never says another word is timed
   from the connection rather than from null - which would otherwise read as "no
   event yet" for ever. */
function sseSilent() {
	var d = transportDiag();
	if (!d || !d.lastEventAt) return false;
	return (Date.now() - d.lastEventAt) > SSE_LIVENESS_MS;
}

function sseWanted() {
	if (mockName) return false;          /* a fixture is a file, not a stream */
	return typeof EventSource !== 'undefined';
}

/* Called from the 1 Hz tick. sseDelivering() has already let the poll through by
   the time this runs; this is the other half - closing the stream the page has
   stopped believing and reconnecting on the same paced ladder a transport error
   uses. Separate from the poll so that a silent stream is repaired once rather
   than re-torn-down on every poll interval. */
function sseLivenessCheck() {
	if (!sseSource || sseSource.readyState !== 1) return;
	if (!sseSilent()) return;
	sseFellBack('no state or ping for ' + Math.round(SSE_LIVENESS_MS / 1000) + 's');
}

function sseStart() {
	transportDiag();
	if (!sseWanted()) { setTransportMode('poll', 'no stream in this host'); return; }
	sseConnect();
}

function sseConnect() {
	if (sseRetryTimer) { clearTimeout(sseRetryTimer); sseRetryTimer = null; }
	var es;
	try { es = new EventSource(baseUrl() + SSE_PATH); }
	catch (e) { sseFellBack('EventSource refused'); return; }
	sseSource = es;
	es.addEventListener('open', function () {
		setTransportMode('sse', 'stream open');
		/* The connection IS a liveness signal, and seeding the deadline here is what
		   makes a connect-then-silence measurable at all. */
		noteTransportEvent();
		/* lane N: and the STATE deadline is seeded here for the same reason. Without
		   it a stream that opens and only ever pings has no state deadline running,
		   so nothing measures the one case laneNStateStarved exists for. */
		laneNNoteStateFrame();
	});
	es.addEventListener('state', function (ev) { onSseState(ev); });
	/* A ping is liveness and nothing else: it carries no document, so it must not
	   touch pollFailed, lastGoodAtMs or anything else the stale logic reads. */
	es.addEventListener('ping', function () { noteTransportEvent(); });
	es.addEventListener('error', function (ev) { onSseError(ev); });
}

function onSseState(ev) {
	noteTransportEvent();
	laneNNoteStateFrame();   // lane N
	setTransportMode('sse', 'state frame');
	sseRetryMs = SSE_RETRY_MIN_MS;
	var doc;
	try { doc = JSON.parse(ev.data); }
	catch (e) {
		/* The one place the two transports could have forked. A frame that is not
		   JSON is a dead feed, exactly as an unparseable poll body is — same latch,
		   same render, not a silent drop. */
		pollFailed = true;
		render();
		return;
	}
	acceptDoc(doc);
}

function onSseError(ev) {
	/* TRAP, and the reason this is one listener and not two. EventSource dispatches
	   BOTH a server-sent `event: error` frame and its own transport failure as type
	   "error". The frame is a MessageEvent and carries `data` — crabd sends one
	   while it has no snapshot yet and then keeps the stream open — and the
	   transport failure carries none. Treating them alike would tear the stream
	   down every time crabd said "state not built yet", which is exactly the moment
	   it is about to start serving. */
	if (ev && typeof ev.data === 'string') {
		noteTransportEvent();
		logLine('sse: ' + ev.data);
		return;
	}
	sseFellBack('stream error');
}

function sseFellBack(why) {
	sseClose();
	setTransportMode('poll', why);
	/* OUR backoff, not the browser's. EventSource reconnects on its own `retry:`
	   clock for ever, so closing it here is what turns a crabd that is down into a
	   3 / 6 / 12 / 24 / 30 s ladder instead of a fixed-rate reconnect storm. */
	var wait = sseRetryMs;
	sseRetryMs = Math.min(SSE_RETRY_MAX_MS, sseRetryMs * 2);
	sseRetryTimer = setTimeout(function () { sseRetryTimer = null; sseConnect(); }, wait);
	logLine('sse retry in ' + Math.round(wait / 1000) + 's');
	/* The fallback poll runs NOW rather than at the next interval: the stream may
	   have been the only thing feeding this page, and the panel is already as stale
	   as whatever killed the stream made it. Forced, because sseClose() above has
	   already dropped the gate and a future reader should not have to prove that. */
	poll(true);
}

function sseClose() {
	if (!sseSource) return;
	try { sseSource.close(); } catch (e) { /* already gone */ }
	sseSource = null;
}

/* ---- lane B: the chime ---- */

/* One soft two-note chime when a session STARTS waiting on a human. Edge-triggered
   off the diffed document, so it says the same thing the amber card does and never
   more often. */
var CHIME_COOLDOWN_MS = 5000;
var CHIME_SMOKE_ID = 'smoke-test';
var CHIME_VOLUME_DEFAULT = 60;
/* The peak gain at volume 100. Measured by ear on the Edge's own output: a sine
   pair at 0.22 is audible across a room and is not an alarm. */
var CHIME_PEAK_AT_FULL = 0.22;
var chimePrevStates = null;     /* null means NO document yet — see chimeDecision */
var chimeLastAt = 0;
var chimeCtx = null;

function chimeOn() { return boolProp('chime', true); }

/* NAMED chimeLevel AND NOT chimeVolume, and the name is kept although its reason
   is retired: `chimeVolume` is a SETTING, and the retired host injected every
   setting into the page as a same-named global
   with `let` semantics. A function declaration sharing a property's name is a
   parse error and a blank panel - 0.27.0 shipped exactly that with `panelToken`,
   and nothing in a browser or a mock run can catch it. The reader gets a
   different name; the property keeps its own. */
function chimeLevel() {
	return Math.max(0, Math.min(100, Math.round(numProp('chimeVolume', CHIME_VOLUME_DEFAULT))));
}

/* THE DECISION, pure, so every gate is provable without an audio device
   (widget/tests/test_chime.js). `prev` is the previous document's id -> state map,
   `next` is this document's sessions array.

   The gates, and what each one is for:
     - `prev === null` is BOOT. A panel that started while three sessions were
       already waiting must not play three chimes at the sight of them; nothing on
       the glass changed, only what this page knows.
     - an id ABSENT from prev, after boot, DOES chime: a row that was not in the
       last document and is waiting in this one is a new alert (a done row is
       dropped ~10 min after it finishes, so a question in it comes back as a new
       id). This is the one branch a "must have been seen before" rule gets wrong.
     - `prev[id] === 'needs_input'` is the same question still waiting. Re-render,
       not news.
     - the smoke test's own session never chimes: it MANUFACTURES a needs_input row
       to prove the panel lights up, and a chime for it would be the instrument
       ringing at its own test.
     - quiet and the `chime` prop are hard offs.
   Reduced motion is DELIBERATELY not a gate. It is a statement about animation,
   not about sound, and the operator who turned it on did not ask for silence. */
function chimeDecision(prev, next, quietActive, chimeEnabled, now, lastChimeAt) {
	if (!chimeEnabled) return false;
	if (quietActive) return false;
	if (!prev) return false;
	if (isFinite(lastChimeAt) && isFinite(now) && now - lastChimeAt < CHIME_COOLDOWN_MS) return false;
	var rows = Array.isArray(next) ? next : [];
	for (var i = 0; i < rows.length; i++) {
		var s = rows[i];
		if (!s || !s.id || s.state !== 'needs_input') continue;
		if (s.id === CHIME_SMOKE_ID) continue;
		if (prev[s.id] === 'needs_input') continue;
		return true;
	}
	return false;
}

function detectChime(doc) {
	var rows = doc && Array.isArray(doc.sessions) ? doc.sessions : [];
	/* ABSENT quiet is false, never unknown: crabd omits the block entirely when no
	   quiet hours are configured, so a truthiness test on doc.quiet is the reading. */
	var quiet = !!(doc && doc.quiet && doc.quiet.active === true) || quietPendingMute();
	var now = Date.now();
	if (chimeDecision(chimePrevStates, rows, quiet, chimeOn(), now, chimeLastAt)) {
		chimeLastAt = now;
		playChime(chimeLevel());
	}
	var next = {};
	for (var i = 0; i < rows.length; i++) {
		if (rows[i] && rows[i].id) next[rows[i].id] = rows[i].state;
	}
	chimePrevStates = next;
}

/* One context for the life of the page. A context per chime leaks an audio device
   handle per alert, and Chromium caps them. */
function chimeAudio() {
	if (typeof window === 'undefined') return null;
	var Ctx = window.AudioContext || window.webkitAudioContext;
	if (!Ctx) return null;
	if (!chimeCtx) {
		try { chimeCtx = new Ctx(); } catch (e) { return null; }
	}
	/* Suspended is what an autoplay policy looks like from in here. The panel host
	   passes --autoplay-policy=no-user-gesture-required so this never happens on the
	   glass; in a plain browser the first resume lands on the operator's own tap. */
	if (chimeCtx.state === 'suspended') { try { chimeCtx.resume(); } catch (e) {} }
	return chimeCtx;
}

/* Two notes, ~350 ms, SYNTHESIZED. There is no audio file in this tree and adding
   one would put a binary into the shipped asset set; a pair of sine oscillators is
   four lines and no asset. A5 then D6 — RISING, because a
   falling pair reads as something finishing and this is something starting to
   wait. */
function playChime(volume) {
	var ctx = chimeAudio();
	if (!ctx) return false;
	var peak = Math.max(0, Math.min(100, Number(volume) || 0)) / 100 * CHIME_PEAK_AT_FULL;
	if (peak <= 0) return false;
	var t0 = ctx.currentTime + 0.01;
	chimeNote(ctx, 880.0, t0, 0.2, peak);
	chimeNote(ctx, 1174.66, t0 + 0.15, 0.2, peak * 0.85);
	return true;
}

function chimeNote(ctx, hz, at, dur, peak) {
	var osc, gain;
	try { osc = ctx.createOscillator(); gain = ctx.createGain(); }
	catch (e) { return; }
	osc.type = 'sine';
	osc.frequency.setValueAtTime(hz, at);
	/* A 12 ms attack and an exponential tail. A square-edged gain is an audible
	   CLICK at this level, and exponentialRampToValueAtTime throws on a zero
	   target — hence the 0.0001 floor at both ends rather than 0. */
	gain.gain.setValueAtTime(0.0001, at);
	gain.gain.exponentialRampToValueAtTime(peak, at + 0.012);
	gain.gain.exponentialRampToValueAtTime(0.0001, at + dur);
	osc.connect(gain);
	gain.connect(ctx.destination);
	osc.start(at);
	osc.stop(at + dur + 0.02);
}

/* ---- lane B: the panel-host message bridge ---- */

/* BRIDGE v2 (C2, v0.32.0). The panel host is the only surface that can save a
   setting or bring a window to the front; a browser preview at /panel/ has neither,
   and says so rather than offering controls that go nowhere.

   EVERY PAGE-TO-HOST MESSAGE CARRIES A requestId AND EVERY ACCEPTED REQUEST GETS A
   TERMINAL ANSWER (SCA-021). Before this, the host answered only on SUCCESS: a save
   that failed its file write was logged natively and nothing came back, so the sheet
   said "saving" for ever and the operator could not tell a failed write from a slow
   one. A focus request rejected before a result did the same. The page's half of the
   repair is here - a correlation id, a deadline, and one place where every pending
   state ends:
     - a reply whose requestId this page is not waiting for is DROPPED. That is how a
       late answer to a superseded attempt stops overwriting the newer one.
     - a request with no answer inside BRIDGE_TIMEOUT_MS ends by itself and says the
       host did not answer, which is a different sentence from a failure and is the
       honest one.
   CAPABILITIES COME FROM THE HANDSHAKE, never from the injected boot object and never
   from the page's address: what a host CAN do is a thing only the host can state. */
var hostInfo = null;
var BRIDGE_TIMEOUT_MS = 10000;
/* Contract: at most 64 characters. The boot instant plus a counter, so two pages open
   at once cannot collide and a reply meant for a previous page load can never match a
   request this one made. */
var bridgeBootId = String(Date.now() % 1000000);
var bridgeSeq = 0;
var bridgePending = {};

function panelBridge() {
	try {
		var w = typeof window !== 'undefined' && window.chrome && window.chrome.webview;
		return w && typeof w.postMessage === 'function' ? w : null;
	} catch (e) { return null; }
}

/* What this host has SAID it can do. False until the handshake lands, which is the
   safe direction: a control that appears once the host has answered is better than
   one that promises before it has. */
function hostCan(name) {
	var c = hostInfo && hostInfo.capabilities;
	return !!(c && typeof c === 'object' && c[name] === true);
}

function bridgeRequestId(kind) {
	return (kind + '-' + bridgeBootId + '-' + (++bridgeSeq)).slice(0, 64);
}

/* Returns the requestId, or null when the message could not be sent at all. The
   caller owns the two callbacks and this owns the deadline. */
function bridgeSend(type, fields, onTimeout) {
	var b = panelBridge();
	if (!b) return null;
	var id = bridgeRequestId(type);
	var msg = { type: type, requestId: id };
	for (var k in fields) {
		if (Object.prototype.hasOwnProperty.call(fields, k)) msg[k] = fields[k];
	}
	try { b.postMessage(msg); } catch (e) { return null; }
	/* ONE OUTSTANDING REQUEST PER KIND (SCA-021). A second Save supersedes the
	   first, and the first's answer must not land on it: the status line reports one
	   attempt, and a late failure written over a newer attempt in flight is the
	   panel reporting the wrong outcome. The superseded record is dropped WITHOUT
	   calling its timeout, because it did not time out - it was replaced. */
	for (var pid in bridgePending) {
		if (!Object.prototype.hasOwnProperty.call(bridgePending, pid)) continue;
		if (bridgePending[pid].type !== type) continue;
		clearTimeout(bridgePending[pid].timer);
		delete bridgePending[pid];
	}
	bridgePending[id] = {
		type: type,
		timer: setTimeout(function () {
			delete bridgePending[id];
			logLine('bridge: no answer to ' + type + ' within ' +
				Math.round(BRIDGE_TIMEOUT_MS / 1000) + 's');
			if (onTimeout) onTimeout();
		}, BRIDGE_TIMEOUT_MS)
	};
	return id;
}

/* True when this reply answers a request this page is still waiting for; the
   pending record is cleared either way it returns. */
function bridgeSettle(msg, type) {
	var id = typeof msg.requestId === 'string' ? msg.requestId : '';
	var rec = Object.prototype.hasOwnProperty.call(bridgePending, id) ? bridgePending[id] : null;
	if (!rec || rec.type !== type) return false;
	clearTimeout(rec.timer);
	delete bridgePending[id];
	return true;
}

function bridgeInit() {
	var b = panelBridge();
	if (!b || typeof b.addEventListener !== 'function') return;
	b.addEventListener('message', function (ev) { onHostMessage(ev); });
	/* A host that does not answer this leaves every capability false, so the sheet
	   says saving is unavailable rather than offering a Save nothing will take. */
	bridgeSend('host-info', {}, function () { renderSettingsFoot(); });
}

function onHostMessage(ev) {
	var msg = ev && ev.data;
	/* WebView2 delivers postMessage(object) on `data` as an object and
	   postMessageAsString as a string. Both shapes are read, neither is trusted:
	   every branch below tests the type it is about to use. */
	if (typeof msg === 'string') {
		try { msg = JSON.parse(msg); } catch (e) { return; }
	}
	if (!msg || typeof msg !== 'object') return;
	if (msg.type === 'host-info') {
		if (!bridgeSettle(msg, 'host-info')) return;
		hostInfo = msg;
		renderSettingsFoot();
		/* The capabilities have only just arrived, so anything gated on one is built
		   now rather than at boot. */
		laneEInit();
		/* MF-017: the pairing code can only be verified once the host has told this
		   page it is there. */
		maybeVerifyApproval();
		return;
	}
	if (msg.type === 'settings-result') {
		if (!bridgeSettle(msg, 'settings')) return;
		onSettingsResult(msg);
		return;
	}
	if (msg.type === 'focus-result') {
		if (!bridgeSettle(msg, 'focus-session')) return;
		laneEOnFocusResult(msg);
		return;
	}
	/* An unknown type is not an error and is not logged as one: the host may ship
	   ahead of the page. */
}

/* ---- lane B: the settings sheet ---- */

/* The props this sheet may edit, and NOTHING else. crabdPort, display and the
   pairing code are deliberately absent: the port and the display are how the host
   finds crabd and the glass, and a page that could move either could hide itself;
   the pairing code is the one secret a visited page must never be able to read or
   write. The host validates this list again on its own side — this copy is the UI,
   not the gate. */
var SETTINGS_TOGGLES = [
	['clock24', '24-hour clock', false],
	['alertFlash', 'Flash on new alert', true],
	['crabStyle', 'Crab accessories', true],
	['chime', 'Chime on a new question', true],
	['touchDiag', 'Touch diagnostics', false]
];
var SETTINGS_SLIDERS = [
	['transparency', 'Background transparency', 0, '%'],
	['chimeVolume', 'Chime volume', CHIME_VOLUME_DEFAULT, '']
];
/* A small fixed palette per colour, plus whatever the panel is actually set to —
   a swatch row that could not show the current value would be a control that
   cannot represent its own state. */
var SETTINGS_COLORS = [
	['textColor', 'Text', '#EDE7DF', ['#EDE7DF', '#FFFFFF', '#D9D2C7', '#B8AEA2']],
	['accentColor', 'Accent', '#6F94CC', ['#6F94CC', '#2E7FF2', '#6FBF73', '#E8A33D', '#A39C93']],
	['backgroundColor', 'Background', '#0F0E0D', ['#0F0E0D', '#000000', '#1A1816', '#12161C']]
];
var settingsDraft = null;

/* The sheet is the settings surface in both surfaces now, so the chip always
   shows. What it can DO differs: a browser preview renders the same controls and
   says plainly that saving needs the panel host (renderSettingsFoot, onSettingsSave),
   which is a truthful read-only view rather than a hidden feature. */
function gearWanted() { return true; }

function settingsCurrent() {
	var d = {};
	var i;
	for (i = 0; i < SETTINGS_TOGGLES.length; i++) {
		var t = SETTINGS_TOGGLES[i];
		d[t[0]] = t[0] === 'crabStyle' ? !crabPlain() : boolProp(t[0], t[2]);
	}
	for (i = 0; i < SETTINGS_SLIDERS.length; i++) {
		var sl = SETTINGS_SLIDERS[i];
		d[sl[0]] = Math.max(0, Math.min(100, Math.round(numProp(sl[0], sl[2]))));
	}
	for (i = 0; i < SETTINGS_COLORS.length; i++) {
		var c = SETTINGS_COLORS[i];
		d[c[0]] = normHex(strProp(c[0], c[2]), c[2]);
	}
	return d;
}

function normHex(value, dflt) {
	var m = /^#?([0-9a-f]{6})$/i.exec(String(value === null || value === undefined ? '' : value).trim());
	return m ? '#' + m[1].toUpperCase() : dflt;
}

function settingsValues() {
	if (!settingsDraft) settingsDraft = settingsCurrent();
	return settingsDraft;
}

/* MF-001. The companion half of the sheet can only exist once a document has
   arrived, and a sheet opened BEFORE the first one - which is what the boot flag
   does, and what a panel opened the instant the companion starts does - would
   otherwise show the panel half alone for as long as it stayed open. Caught on the
   glass in the browser pass, not reasoned about. Guarded on the transition rather
   than on a flag, so the rebuild happens once and not on every poll. */
var settingsHadData = null;

function syncSettingsSheet() {
	if (settingsHadData === everHadData) return;
	settingsHadData = everHadData;
	buildSettingsRows();
}

function openSettingsSheet() {
	if (!ui.sheetSettings) return;
	settingsHadData = everHadData;
	settingsDraft = settingsCurrent();
	/* MF-001: the companion half re-seeds from the current feed on every open, so a
	   sheet opened after somebody edited config.json by hand shows what is on disk
	   rather than what this page last saw. */
	cfgDraft = null;
	cfgTouched = null;
	sheetSessionId = null;
	sheetGen++;
	sheetOpenState = null;
	sheetMode = 'settings';
	clearSheetTimer();
	sheetBusy = false;
	/* lane N: a new sheet is a new surface, so its busy flag starts clear. Without
	   this a save still on the wire from the PREVIOUS sheet made Save on this one
	   silently inert - the tap sent nothing and the line did not move. */
	cfgBusy = false;
	ui.sheet.classList.remove('busy');
	setSheetStatus('', '');
	ui.sheet.setAttribute('data-mode', 'settings');
	ui.sheet.setAttribute('data-detail-state', '');
	ui.sheet.setAttribute('data-approval', '');
	ui.sheet.setAttribute('data-continue', '');
	setVar(ui.sheet, '--sheet-accent', 'var(--accent)');
	setText(ui.sheetTitle, 'Panel settings');
	ui.sheetTitle.classList.remove('title-derived');
	setText(ui.sheetRepo, panelBridge()
		? 'panel settings on this machine, companion settings in the companion'
		: 'browser preview ' + EMDASH + ' panel settings cannot be saved here');
	buildSettingsRows();
	ui.sheet.classList.add('open');
	ui.sheet.setAttribute('aria-hidden', 'false');
	enterSheetFocus();
}

/* Built in JS rather than written into index.html, for the reason the burn list and
   the card grid are: the rows depend on state that only exists at runtime, and the
   set changes with the feed. The region in the markup is one empty div. */
function buildSettingsRows() {
	var root = ui.sheetSettings;
	if (!root) return;
	var values = settingsValues();
	root.textContent = '';
	var i;
	/* TWO HALVES, SEPARATELY HEADED AND SEPARATELY SAVED, because they go to two
	   different places and a control that does not say where it writes is a control
	   the operator has to guess at. */
	root.appendChild(settingsSection('This panel', 'stored on this machine by the panel host'));
	for (i = 0; i < SETTINGS_TOGGLES.length; i++) {
		root.appendChild(settingsToggleRow(SETTINGS_TOGGLES[i][0], SETTINGS_TOGGLES[i][1], values));
	}
	for (i = 0; i < SETTINGS_COLORS.length; i++) {
		root.appendChild(settingsColorRow(SETTINGS_COLORS[i], values));
	}
	for (i = 0; i < SETTINGS_SLIDERS.length; i++) {
		root.appendChild(settingsSliderRow(SETTINGS_SLIDERS[i], values));
	}
	root.appendChild(settingsActions());

	/* MF-001. Presence-gated on having a document at all: with nothing from the
	   companion there is nothing to seed these controls from, and a sheet that
	   offered to write quiet hours to a companion it has never heard from would be
	   offering to overwrite a file it cannot read. */
	if (everHadData) {
		root.appendChild(settingsSection('Companion', 'sent to the SideCrab companion, only the keys you change'));
		root.appendChild(cfgToggleRow('Quiet hours', 'quietEnabled'));
		root.appendChild(cfgTimeRow('Quiet from', 'quietStart'));
		root.appendChild(cfgTimeRow('Quiet until', 'quietEnd'));
		root.appendChild(cfgToggleRow('Desktop toasts', 'toastEnabled'));
		root.appendChild(cfgRangeRow('Toast after', 'toastSec', TOAST_SEC_MIN, TOAST_SEC_MAX, 10, fmtApprovalSec));
		root.appendChild(cfgRangeRow('Approval toast after', 'approvalSec', APPROVAL_SEC_MIN, 300, 5, fmtApprovalSec));
		root.appendChild(cfgToggleRow('Daily digest', 'digestEnabled'));
		root.appendChild(cfgTimeRow('Digest at', 'digestTime'));
		root.appendChild(cfgToggleRow('Daily token budget', 'budgetEnabled'));
		root.appendChild(cfgRangeRow('Budget per day', 'budgetK', BUDGET_K_MIN, BUDGET_K_MAX, 100,
			function (k) { return fmtNum(k * 1000) + ' out'; }));
		root.appendChild(configActions());
		root.appendChild(configPromptsRow());
		/* MF-017: the readiness the approval sheet only shows while a permission is
		   waiting, said here where it can be read at any time. */
		var ready = approvalReadiness();
		if (ready !== null) {
			var line = document.createElement('div');
			line.className = 'set-foot set-foot-ready' + (ready === 'ready' ? '' : ' bad');
			line.textContent = approvalReadyText(ready);
			root.appendChild(line);
		}
	}
	renderSettingsFoot();
}

/* MF-001. A GENERIC toggle, bound to a getter and a setter rather than to the
   panel-settings draft, because the companion half of this sheet writes somewhere
   else entirely. The accessibility wiring is settingsToggleRow's (SCA-008) and is
   not re-derived here. */
function cfgToggleRow(label, key) {
	var row = settingsRow(label);
	var btn = document.createElement('button');
	btn.type = 'button';
	btn.className = 'set-btn set-toggle';
	btn.id = 'setToggle' + (++setIdSeq);
	btn.setAttribute('aria-labelledby', row.labelId + ' ' + btn.id);
	paintSettingsToggle(btn, !!configValues()[key]);
	btn.addEventListener('click', function () {
		var next = !configValues()[key];
		configSet(key, next);
		paintSettingsToggle(btn, next);
	});
	row.appendChild(btn);
	return row;
}

/* An HH:MM field. A text input and not <input type="time">: the panel is driven by
   a fingertip on a wall display with no system time picker to pop, and the native
   control renders a locale-dependent widget this stylesheet cannot size. normHm is
   the validator on save and the field itself is left alone while it is being typed,
   because correcting a half-typed "9:" under the operator's finger is the control
   fighting the person using it. */
function cfgTimeRow(label, key) {
	var row = settingsRow(label);
	var input = document.createElement('input');
	input.type = 'text';
	input.className = 'set-text';
	input.id = 'setText' + (++setIdSeq);
	input.setAttribute('aria-labelledby', row.labelId + ' ' + input.id);
	input.setAttribute('inputmode', 'numeric');
	input.setAttribute('placeholder', 'HH:MM');
	input.setAttribute('maxlength', '5');
	input.value = String(configValues()[key] || '');
	input.addEventListener('input', function () { configSet(key, input.value); });
	row.appendChild(input);
	return row;
}

/* A range with its own bounds and its own unit, against settingsSliderRow's fixed
   0..100. The value beside it is formatted by the caller, because "120 s" and
   "5000k out" are different enough that one formatter would be a switch. */
function cfgRangeRow(label, key, min, max, step, fmt) {
	var row = settingsRow(label);
	var input = document.createElement('input');
	input.type = 'range';
	input.className = 'set-range';
	input.id = 'setRange' + (++setIdSeq);
	input.setAttribute('aria-labelledby', row.labelId + ' ' + input.id);
	input.min = String(min);
	input.max = String(max);
	input.step = String(step);
	input.value = String(configValues()[key]);
	var out = document.createElement('span');
	out.className = 'set-value';
	out.textContent = fmt(Number(input.value));
	input.addEventListener('input', function () {
		var n = Math.max(min, Math.min(max, Math.round(Number(input.value) || min)));
		configSet(key, n);
		out.textContent = fmt(n);
	});
	row.appendChild(input);
	row.appendChild(out);
	return row;
}

/* A heading that spans both columns of the sheet's grid. */
function settingsSection(text, note) {
	var el = document.createElement('div');
	el.className = 'set-section';
	var h = document.createElement('span');
	h.className = 'set-section-head';
	h.textContent = text;
	el.appendChild(h);
	if (note) {
		var n = document.createElement('span');
		n.className = 'set-section-note';
		n.textContent = note;
		el.appendChild(n);
	}
	return el;
}

/* MF-001. The companion's own Save, with its own status line, because it writes
   somewhere else: the panel settings go to a file on this machine through the host
   bridge, and these go to the companion over HTTP. One button for both would have
   been one button with two failure modes and no way to say which had happened. */
function configActions() {
	var row = document.createElement('div');
	row.className = 'set-actions set-actions-cfg';
	var save = document.createElement('button');
	save.type = 'button';
	save.className = 'set-btn set-btn-save';
	save.textContent = 'Save to companion';
	save.addEventListener('click', onConfigSave);
	row.appendChild(save);
	var status = document.createElement('div');
	status.className = 'set-status';
	row.appendChild(status);
	ui.cfgStatus = status;
	return row;
}

/* THE PROJECT PROMPTS ARE SHOWN AND NOT EDITED, and that is a decision rather than
   an omission. The list is a per-repo vocabulary of full instruction sentences kept
   in the companion's config.json; a 48 px touch target on a wall display is the
   wrong editor for a paragraph, and an edit surface here would be a second writer
   of a file the operator already edits properly somewhere else. What the panel owes
   is the answer to "which prompts is THIS session offered, and where do they come
   from", which is what this renders.
   The file is named without a path: nothing in the feed states one, and a path this
   panel invented would be a fact it does not have. */
function configPromptsRow() {
	/* The session whose vocabulary this is. detailTarget() is deliberately NOT used:
	   it PICKS a session when none is chosen, which moves the Detail page's subject
	   and its generation, and a settings sheet must not invalidate an action receipt
	   by being opened. An already-chosen session or nothing. */
	var id = laneCActionSessionId() || detailSessionId;
	var s = id ? findSession(id) : null;
	var list = continueButtons(s || null);
	var el = document.createElement('div');
	el.className = 'set-prompts';
	var head = document.createElement('div');
	head.className = 'set-label';
	head.textContent = s && s.repo ? 'Continue prompts for ' + String(s.repo) : 'Continue prompts';
	el.appendChild(head);
	for (var i = 0; i < list.length; i++) {
		var line = document.createElement('div');
		line.className = 'set-prompt';
		line.textContent = list[i].prompt;
		el.appendChild(line);
	}
	var foot = document.createElement('div');
	foot.className = 'set-foot';
	foot.textContent = 'edited in the companion config file (continuePrompts, continuePromptsByRepo, continuePromptsByPath)';
	el.appendChild(foot);
	return el;
}

/* One id per settings control, so a label element and the control beside it can be
   associated without either of them guessing at the other's id. Rows are rebuilt on
   every save, so the counter rather than the key: a stale id left in the document
   would otherwise be pointed at by the new row. */
var setIdSeq = 0;

function settingsRow(label) {
	var row = document.createElement('div');
	row.className = 'set-row';
	var name = document.createElement('div');
	name.className = 'set-label';
	name.id = 'setLabel' + (++setIdSeq);
	name.textContent = label;
	row.appendChild(name);
	row.labelId = name.id;
	return row;
}

/* SCA-008. The BUTTON'S FACE IS THE STATE ("On"/"Off"), which made the accessible
   name of all five toggles "On" or "Off" and nothing else - five controls an
   assistive technology could not tell apart, on a sheet whose whole content is five
   controls. aria-labelledby names the row label FIRST and the button's own face
   second, so the name reads "24-hour clock Off"; aria-pressed carries the state as
   well, for a reader that uses it. The visible layout is untouched. */
function settingsToggleRow(key, label, values) {
	var row = settingsRow(label);
	var btn = document.createElement('button');
	btn.type = 'button';
	btn.className = 'set-btn set-toggle';
	btn.id = 'setToggle' + (++setIdSeq);
	btn.setAttribute('data-set-key', key);
	btn.setAttribute('aria-labelledby', row.labelId + ' ' + btn.id);
	paintSettingsToggle(btn, !!values[key]);
	btn.addEventListener('click', function () {
		var next = !settingsValues()[key];
		settingsValues()[key] = next;
		paintSettingsToggle(btn, next);
		setSettingsStatus('not saved yet', 'pending');
	});
	row.appendChild(btn);
	return row;
}

function paintSettingsToggle(btn, on) {
	btn.textContent = on ? 'On' : 'Off';
	btn.setAttribute('aria-pressed', on ? 'true' : 'false');
	btn.setAttribute('data-on', on ? '1' : '0');
}

function settingsColorRow(spec, values) {
	var key = spec[0];
	var row = settingsRow(spec[1]);
	var wrap = document.createElement('div');
	wrap.className = 'set-swatches';
	var palette = spec[3].slice();
	var current = normHex(values[key], spec[2]);
	if (palette.indexOf(current) === -1) palette.push(current);
	for (var i = 0; i < palette.length; i++) {
		/* The row's VISIBLE label, not the property name: "Accent #6F94CC" is what
		   the sheet shows, and "accentColor #6F94CC" was a name only this file uses. */
		wrap.appendChild(settingsSwatch(key, palette[i], current, spec[1]));
	}
	row.appendChild(wrap);
	return row;
}

function settingsSwatch(key, hex, current, label) {
	var b = document.createElement('button');
	b.type = 'button';
	b.className = 'set-swatch';
	b.setAttribute('data-set-key', key);
	b.setAttribute('aria-label', (label || key) + ' ' + hex);
	b.setAttribute('title', hex);
	b.style.background = hex;
	b.setAttribute('aria-pressed', hex === current ? 'true' : 'false');
	b.addEventListener('click', function () {
		settingsValues()[key] = hex;
		var sibs = b.parentNode ? b.parentNode.querySelectorAll('.set-swatch') : [];
		for (var i = 0; i < sibs.length; i++) sibs[i].setAttribute('aria-pressed', 'false');
		b.setAttribute('aria-pressed', 'true');
		setSettingsStatus('not saved yet', 'pending');
	});
	return b;
}

function settingsSliderRow(spec, values) {
	var key = spec[0];
	var row = settingsRow(spec[1]);
	var input = document.createElement('input');
	input.type = 'range';
	input.className = 'set-range';
	input.min = '0';
	input.max = '100';
	input.step = '1';
	input.value = String(values[key]);
	input.setAttribute('data-set-key', key);
	input.setAttribute('aria-label', spec[1]);
	var out = document.createElement('span');
	out.className = 'set-value';
	out.textContent = values[key] + spec[3];
	input.addEventListener('input', function () {
		var n = Math.max(0, Math.min(100, Math.round(Number(input.value) || 0)));
		settingsValues()[key] = n;
		out.textContent = n + spec[3];
		setSettingsStatus('not saved yet', 'pending');
	});
	row.appendChild(input);
	row.appendChild(out);
	return row;
}

function settingsActions() {
	var row = document.createElement('div');
	row.className = 'set-actions';

	var test = document.createElement('button');
	test.type = 'button';
	test.className = 'set-btn';
	test.id = 'setTest';
	test.textContent = 'Test chime';
	/* Plays whatever the sliders say RIGHT NOW, and plays through quiet hours: a
	   deliberate tap on a button labelled "Test chime" is the operator asking to
	   hear it, and a control that silently did nothing would be indistinguishable
	   from a broken one. The automatic chime keeps its quiet gate. */
	test.addEventListener('click', function () {
		var v = settingsValues();
		/* SCA-026 — MUTED IS NOT BROKEN. playChime returns false for a zero peak
		   exactly as it does for a host with no AudioContext, and this handler read
		   both as the second: setting the slider to 0 and pressing Test chime
		   reported "no audio output on this host", which sent the operator looking
		   for a driver fault they had caused themselves with the control above.
		   Asked FIRST, because a muted panel on a host with no audio is still muted
		   and that is the fact the operator set. The test deliberately reads the
		   UNSAVED slider value, which is what makes it a test. */
		if (!(Number(v.chimeVolume) > 0)) {
			setSettingsStatus('muted ' + EMDASH + ' chime volume is 0', 'note');
			return;
		}
		if (playChime(v.chimeVolume)) setSettingsStatus('played at ' + v.chimeVolume + '%', 'ok');
		else setSettingsStatus('no audio output on this host', 'err');
	});
	row.appendChild(test);

	var save = document.createElement('button');
	save.type = 'button';
	save.className = 'set-btn set-btn-save';
	save.id = 'setSave';
	save.textContent = 'Save';
	save.addEventListener('click', onSettingsSave);
	row.appendChild(save);

	var status = document.createElement('div');
	status.className = 'set-status';
	status.id = 'setStatus';
	row.appendChild(status);
	ui.setStatus = status;

	var foot = document.createElement('div');
	foot.className = 'set-foot';
	foot.id = 'setFoot';
	row.appendChild(foot);
	ui.setFoot = foot;
	return row;
}

function setSettingsStatus(text, kind) {
	if (!ui.setStatus) return;
	setText(ui.setStatus, text);
	if (ui.setStatus.getAttribute('data-kind') !== (kind || '')) {
		ui.setStatus.setAttribute('data-kind', kind || '');
	}
}

/* What the sheet says about WHERE a save goes, and it is honest in all four states
   a page can be in: no host at all, a host that has not answered the handshake yet,
   a host that answered and cannot save, and a host that can. */
function renderSettingsFoot() {
	if (!ui.setFoot) return;
	if (!panelBridge()) {
		setText(ui.setFoot, 'browser preview ' + EMDASH + ' saving needs the SideCrab panel host');
		return;
	}
	if (!hostInfo) { setText(ui.setFoot, 'waiting for the panel host'); return; }
	if (!hostCan('saveSettings')) {
		setText(ui.setFoot, 'this panel host does not save settings');
		return;
	}
	setText(ui.setFoot, 'panel host ' + (hostInfo.version || '') +
		(typeof hostInfo.settingsPath === 'string' ? ' ' + EMDASH + ' ' + hostInfo.settingsPath : ''));
}

function onSettingsSave() {
	if (!panelBridge()) {
		/* The honest answer, and the whole reason the sheet still renders here: this
		   page in a plain browser has no file to write and no host to write it. */
		setSettingsStatus('saving needs the SideCrab panel host', 'err');
		return;
	}
	if (!hostInfo) { setSettingsStatus('the panel host has not answered yet', 'err'); return; }
	if (!hostCan('saveSettings')) { setSettingsStatus('this panel host does not save settings', 'err'); return; }
	var sent = bridgeSend('settings', { props: settingsValues() }, function () {
		/* SCA-021: a request the host accepted and never answered ends HERE rather
		   than sitting on "saving" until somebody reloads the panel. */
		setSettingsStatus('no answer from the host ' + EMDASH + ' nothing was saved', 'err');
	});
	if (!sent) {
		setSettingsStatus('save not sent ' + EMDASH + ' the host refused the message', 'err');
		return;
	}
	setSettingsStatus('saving', 'pending');
}

/* The host's TERMINAL answer to one save, success or failure. What it echoes back
   in `props` is what it actually stored, after its own whitelist and clamps, which
   is not necessarily what this page sent - so that, and only that, is what moves
   the live settings. */
function onSettingsResult(msg) {
	if (msg.ok !== true) {
		var why = typeof msg.error === 'string' && msg.error ? msg.error : 'the host could not write it';
		setSettingsStatus('not saved ' + EMDASH + ' ' + why, 'err');
		return;
	}
	var props = msg.props;
	if (!props || typeof props !== 'object' || Array.isArray(props)) {
		setSettingsStatus('saved, but the host echoed nothing back', 'note');
		return;
	}
	var host = hostBoot();
	if (host && host.props && typeof host.props === 'object') {
		for (var k in props) {
			if (Object.prototype.hasOwnProperty.call(props, k)) host.props[k] = props[k];
		}
	}
	settingsDraft = null;
	/* Live, with no reload: a reload would throw away the open sheet under the
	   operator's hand and cost a second of blank glass for a colour change. */
	applyProperties();
	if (sheetMode === 'settings') buildSettingsRows();
	setSettingsStatus('saved', 'ok');
}

/* ---- lane B: boot ---- */

function laneBInit() {
	/* Resolved here rather than added to init()'s id list, so this lane's elements
	   are found in one place with the code that uses them. */
	ui.gearChip = document.getElementById('gearChip');
	ui.sheetSettings = document.getElementById('sheetSettings');
	transportDiag();
	if (ui.gearChip) {
		/* Whether the chip shows is fixed for the life of the page (the host and
		   the mock flag are both settled before this runs), so it is decided once
		   rather than on every render. */
		ui.gearChip.classList.toggle('shown', gearWanted());
		ui.gearChip.addEventListener('click', openSettingsSheet);
	}
	bridgeInit();
	sseStart();
	/*   &settings=1   open the settings sheet on boot, for the shot. Mock-gated like
	     every other dev flag. */
	if (mockName && /[?&]settings=1\b/.test(window.location.search)) openSettingsSheet();
}

/* ==== end lane B ==== */

function init() {
	var ids = ['flash', 'banner', 'bannerText', 'crab', 'crabWrap', 'limitsHead', 'clockHm', 'clockSs', 'clockDate',
		'quietNote', 'fleet', 'fleetToast',
		'moonChip', 'moonMode', 'moonLeft',
		'diagChip', 'diagCount',
		'limitsSource',
		'gauge5h', 'fill5h', 'pct5h', 'reset5h', 'forecast5h', 'gaugeWk', 'fillWk', 'pctWk', 'resetWk', 'forecastWk',
		'gaugeExtra', 'limitsNote', 'statOut', 'statIn', 'statCache', 'statMsg',
		'spark', 'sparkWrap', 'sparkLabel', 'sparkLabels', 'sparkMode', 'sparkMax', 'sparkTarget', 'budgetLine', 'costLine',
		'sensors', 'sensorCpu', 'sensorCpuVal', 'sensorCpuName', 'sensorGpu', 'sensorGpuVal', 'sensorGpuName',
		'sensorGpuWarn', 'sensorHint', 'sensorMore', 'hostCpuVal', 'hostMem', 'hostMemVal',
		'sessionCount', 'gridHead', 'cards', 'gridEmpty', 'filterChip', 'densityChip', 'historyChip',
		'coreLine', 'coreSessions', 'coreLimits',
		'sheet', 'sheetBackdrop', 'sheetTitle', 'sheetRepo', 'sheetQuestion', 'sheetStatus',
		'sheetMeta', 'sheetSubs', 'sheetEvents', 'sheetBurn', 'sheetTimeline', 'sheetWeek', 'sheetHost',
		'sheetPin', 'sheetBack', 'sheetDayFoot', 'sheetPrevDay', 'sheetNextDay',
		'sheetApprovalDetail', 'sheetApprovalTool', 'sheetApprovalSummary', 'sheetApprovalLeft',
		'sheetApprovalThreshold', 'sheetApprovalReady', 'sheetApprove', 'sheetDeny',
		'sheetContinue', 'sheetContinueBtns', 'sheetContinueStatus',
		'notice', 'noticeText',
		/* lane C: the view switcher's chips and containers, and the canvas crab. */
		'chipViewSessions', 'chipViewBurn', 'chipViewWeek', 'chipViewDetail', 'viewBadge',
		'viewBurn', 'viewWeek', 'viewDetail', 'sheetFullView', 'crabCanvas'];
	for (var i = 0; i < ids.length; i++) ui[ids[i]] = document.getElementById(ids[i]);
	ui.extraRows = [];
	ui.sparkBars = [];
	ui.sparkLabelSig = null;
	ui.recapSig = null;
	ensureSparkBars(SPARK_BUCKETS);

	var m = /[?&]mock=([a-z]+)/i.exec(window.location.search);
	if (m && MOCKS.indexOf(m[1].toLowerCase()) !== -1) mockName = m[1].toLowerCase();

	/* Dev-only flags, all gated on mock mode: a panel reading a live companion must
	   never be steerable by a query string somebody put in front of it.
	     &sheet=<id|prefix|first>   auto-open the ACTION sheet on a needs_input row
	     &sheet2=<id|prefix|first>  auto-open the DETAIL sheet on any other row
	     &age=<minutes>             back-date needs_input for the escalation tiers
	     &spark=7d                  start the sparkline on the 7-day series */
	if (mockName) {
		var sp = /[?&]sheet=([^&]+)/.exec(window.location.search);
		if (sp) sheetAutoId = decodeURIComponent(sp[1]);
		var sp2 = /[?&]sheet2=([^&]+)/.exec(window.location.search);
		if (sp2) sheetAutoDetailId = decodeURIComponent(sp2[1]);
		var ag = /[?&]age=(\d+)/.exec(window.location.search);
		if (ag) ageOverrideMin = Number(ag[1]);
		if (/[?&]spark=7d\b/i.test(window.location.search)) sparkMode = '7d';
		/*   &celebrate=1              hold the celebrating mood, for the screenshot
		     &blink=<seconds>          fix the idle-blink interval so it is observable */
		if (/[?&]celebrate=1\b/.test(window.location.search)) celebrateForced = true;
		if (/[?&]burn=1\b/.test(window.location.search)) burnAuto = true;
		/*   &timeline=1               auto-open the Today timeline sheet */
		if (/[?&]timeline=1\b/.test(window.location.search)) timelineAuto = true;
		/*   &host=1                   auto-open the host history sheet (v0.22.0), so
		     the charts and the "collecting" state can both be shot. It opens the
		     sheet through the SHIPPING openHostSheet, which means a fixture whose
		     host block is absent or all-null opens NOTHING — the inert case, and
		     worth a shot of its own. */
		if (/[?&]host=1\b/.test(window.location.search)) hostAuto = true;
		/*   &approval=1               auto-open the approval sheet on the first
		     needs_input session carrying a pendingPermission, for the shot */
		if (/[?&]approval=1\b/.test(window.location.search)) approvalAuto = true;
		/*   &action400=1              force the older-crabd 400 on queue-continue
		     and decide, so the no-latch inline handling is demoable without a
		     fixture edit */
		if (/[?&]action400=1\b/.test(window.location.search)) actionForce400 = true;
		var bl = /[?&]blink=(\d+)/.exec(window.location.search);
		if (bl && Number(bl[1]) > 0) { blinkMinMs = blinkMaxMs = Number(bl[1]) * 1000; }
		/*   &day=YYYY-MM-DD           auto-open that day's drill on the first document */
		var dy = /[?&]day=(\d{4}-\d{2}-\d{2})\b/.exec(window.location.search);
		if (dy) dayAuto = dy[1];
		/*   &hist=rich|empty|error    which canned document TODAY's history drill
		     reads (v0.19.0). `error` names a file the static server does not have,
		     so the 404 is produced rather than simulated — the older-crabd path,
		     which is the one branch of this feature that must never open a sheet. */
		var hs = /[?&]hist=(rich|empty|error)\b/.exec(window.location.search);
		if (hs) histAuto = hs[1];
		/*   &uid=<id>                 stand in for the host-injected uniqueId, so the
		     vendor local-storage path (and only that path) is exercisable in a dev
		     browser where the global does not exist. See loadPrefs(). */
		var uid = /[?&]uid=([A-Za-z0-9_-]{1,64})\b/.exec(window.location.search);
		if (uid) devUidOverride = uid[1];
		/*   &pin=<id|prefix|first>    pre-pin one session, in memory, for the shot */
		var pn = /[?&]pin=([^&]+)/.exec(window.location.search);
		if (pn) pinAuto = decodeURIComponent(pn[1]);
		/*   &budget=<percent>         put the day at that percentage of its budget,
		     recomputing the budget from the fixture's own output total so the
		     document stays self-consistent. See applyBudgetOverride(). */
		var bg = /[?&]budget=(\d+)/.exec(window.location.search);
		if (bg && Number(bg[1]) > 0) budgetPctOverride = Number(bg[1]);
		/*   &crab=<accessory|trick>  force one wardrobe state for the shot. An
		     accessory is HELD (it outranks the fleet's own answer and the plain
		     style, so a costume can be photographed against any fixture); a trick
		     is re-fired on a loop, because a 560 ms snap is not a window a
		     screenshot can be aimed at. `none` holds the bare crab. */
		var cr = /[?&]crab=([a-z]+)/i.exec(window.location.search);
		if (cr) {
			var want = cr[1].toLowerCase();
			if (ACCESSORIES.indexOf(want) !== -1) accForced = want;
			else if (want === 'none' || want === 'plain') accForced = '';
			else if (want === 'juggle' || want === 'bounce' || want === 'snap' || want === 'dance') forcedTrick = want;
		}
		/*   &swipe=<id|prefix|first>  freeze one dismissable card mid-swipe, at
		     &swipeX=<px> (default 90, past the 60 px threshold so the armed state is
		     in the shot). A drag is a few hundred milliseconds of moving transform
		     and is not a window a screenshot can be aimed at — the flag paints the
		     REAL transform through the real paintSwipe(), so what is photographed is
		     the rendering the finger gets and not a mock-up of it. */
		var sw = /[?&]swipe=([^&]+)/.exec(window.location.search);
		if (sw) swipeFreeze = decodeURIComponent(sw[1]);
		var swx = /[?&]swipeX=(-?\d+)/.exec(window.location.search);
		swipeFreezePx = swx ? Number(swx[1]) : 90;
		/*   &pinflash=<id|prefix|first>  pin that session and HOLD the long-press
		     confirm, so the glyph animating in can be photographed. Pins in memory
		     only, the same discipline &pin= keeps: a screenshot flag that wrote to
		     the vendor store would leave the operator's own map holding a fixture. */
		var pf = /[?&]pinflash=([^&]+)/.exec(window.location.search);
		if (pf) {
			pinAuto = pinFlashAuto = decodeURIComponent(pf[1]);
			pinFlashHold = true;
			/* The confirm is a 260 ms animation, which is not a window a screenshot
			   can be aimed at any more than a 560 ms claw snap was. Holding the flash
			   only stops the glyph being REMOVED; the class below pauses the real
			   animation on a real frame of itself, so what is photographed is the
			   rendering rather than a still life of its end state. */
			document.body.classList.add('pinflash-frozen');
		}
		/*   &ackflash=1      run the REAL two-finger ack-all on the first document and
		     hold its confirmation line
		     &refreshflash=1  hold the pull-to-refresh line instead. Both hold the
		     notice rather than drawing a fake one, so what is in the shot is the
		     line the gesture produces. */
		if (/[?&]ackflash=1\b/.test(window.location.search)) { ackFlashAuto = true; noticeHold = true; }
		if (/[?&]refreshflash=1\b/.test(window.location.search)) { refreshFlashAuto = true; noticeHold = true; }
		/*   &filter=<key>   &density=<key>   set the two header chips for the shot
		     (v0.15.0). They set the SAME variables a tap sets and nothing else, so
		     what is photographed is the real mode; they do NOT write to the vendor
		     store — the discipline &pin= keeps, because a screenshot flag that
		     persisted would leave the operator's own panel filtered. */
		var fl = /[?&]filter=([a-z_]+)/i.exec(window.location.search);
		if (fl) filterForced = fl[1].toLowerCase();
		var dn = /[?&]density=([a-z]+)/i.exec(window.location.search);
		if (dn) densityForced = dn[1].toLowerCase();
		/*   &hold=<seconds>  start every pendingPermission's hold with that many
		     seconds left, so the countdown can be aimed at. It counts DOWN from
		     there in real time and reaches "expired" on its own, which is the
		     point: a frozen number would photograph a clock, not a countdown. */
		var hd = /[?&]hold=(\d+)/.exec(window.location.search);
		if (hd) holdOverrideSec = Number(hd[1]);
		/*   &quietov=on|off|auto|none  stand in for crabd's quiet OVERRIDE (v0.22.0).
		     It seeds the harness's daemon, not the widget: applyMockQuietOverride
		     writes the member into the served document and honours it in `active`
		     exactly as crabd does, so what renders is the shipping read path on a
		     document a real companion could have sent. `none` writes an explicit
		     null — the member PRESENT and empty, which is the shape a presence test
		     gets wrong if it checks truthiness instead of type. A tap then moves the
		     same variable, which is what makes the three-state cycle demoable
		     off-glass rather than only its first frame. */
		var qov = /[?&]quietov=(on|off|auto|none)\b/i.exec(window.location.search);
		if (qov) {
			var qmode = qov[1].toLowerCase();
			quietForced = qmode;
			mockQuietOv = (qmode === 'on' || qmode === 'off')
				? { mode: qmode, until: Date.now() + QUIET_OVERRIDE_MIN * 60000 }
				: { mode: 'auto', until: null };
		}
		/*   &touchdiag=1  stand in for the `touchDiag` setting (v0.23.0), the way
		     &approvalsec= stands in for the approval slider. A dev browser has no
		     property sheet, and the capture layer's whole value is what it records on
		     a real input device — so the one place it can be exercised against a
		     KNOWN input source (a scripted mouse, a synthesized touch stream) is
		     here. It feeds diagWanted() and nothing else, so install, capture,
		     coalesce, flush and remove are all the shipping path. */
		if (/[?&]touchdiag=1\b/.test(window.location.search)) diagForced = true;
		/*   &mood=<mood>              hold one crab mood for the shot (v0.17.0) */
		var md = /[?&]mood=([a-z]+)/i.exec(window.location.search);
		if (md && MOODS.indexOf(md[1].toLowerCase()) !== -1) moodForced = md[1].toLowerCase();
	}

	/* Before the first render: a pinned session must be in its pinned position on
	   the first frame, not jump there once storage has been read. */
	loadPrefs();
	/* AFTER loadPrefs, not before: the two flags are a screenshot's answer and the
	   store's is the operator's, so the flag has to be the one that survives. With
	   &uid= in play loadPrefs reads a real stored object, which is exactly the run
	   where setting these earlier would have been silently overwritten. */
	if (filterForced) filterIdx = prefIndex(FILTERS, filterForced);
	if (densityForced) densityIdx = prefIndex(DENSITIES, densityForced);
	/* Before the first render: the compact grid is a different capacity, and
	   gridCapacity reads the class's result off the computed style. */
	applyDensity();
	syncHeaderChips();
	/* lane C. Before the first render for the reason applyDensity is above it: the
	   active view decides the card grid's box, and gridCapacity reads the result
	   off the computed style rather than a second copy of it. */
	laneCViewsInit();
	laneCCrabInit();

	ui.cards.addEventListener('click', onCardsClick);
	ui.sheet.addEventListener('click', onSheetClick);
	ui.sparkWrap.addEventListener('click', onSparkClick);
	ui.crabWrap.addEventListener('click', onCrabTap);
	ui.limitsHead.addEventListener('click', openBurnSheet);
	/* v0.22.0. The chip is its own element inside the clock row and the clock row
	   has no listener, so these cannot claim each other's taps. */
	if (ui.moonChip) ui.moonChip.addEventListener('click', onMoonTap);
	/* The sensors row opens the host history sheet. Bound unconditionally; whether
	   the tap does anything is syncSensorRow's answer and openHostSheet's guard, so
	   the affordance and the behaviour are decided in one place rather than by
	   whether a listener happens to be attached. */
	if (ui.sensors) ui.sensors.addEventListener('click', onSensorsClick);
	/* The gauges are their own targets (v0.19.0), and they are SEPARATE elements
	   from the header above — so the header's listener and these cannot claim each
	   other's taps and the burn-by-session view is reached exactly as before. */
	ui.gauge5h.addEventListener('click', onGaugeClick);
	ui.gaugeWk.addEventListener('click', onGaugeClick);
	ui.gaugeExtra.addEventListener('click', onGaugeClick);
	/* The header is no longer one target (v0.15.0): the two chips live inside it,
	   so the timeline opens only for a tap that landed on neither. Routed on the
	   CONTROL, never on coordinates — the chips are buttons and closest() is what
	   a fingertip landing on the label inside one resolves to. */
	ui.gridHead.addEventListener('click', onGridHeadClick);

	/* The gesture layer (v0.14.0). On the DOCUMENT, because a two-finger tap is
	   defined as "anywhere on the panel" and a pull starts on whatever happens to be
	   in the top strip — neither has an element to hang off. Every listener is
	   PASSIVE: nothing in here calls preventDefault, the axes are claimed in CSS
	   (touch-action), and a non-passive move handler on a 24/7 panel would put the
	   compositor behind the main thread for no gain.
	   The click swallow is the one CAPTURING listener, and it is capturing so that a
	   gesture-consumed click never reaches any handler — including controls added
	   after this was written. */
	document.addEventListener('pointerdown', onPointerDown, { passive: true });
	document.addEventListener('pointermove', onPointerMove, { passive: true });
	document.addEventListener('pointerup', onPointerUp, { passive: true });
	document.addEventListener('pointercancel', onPointerCancel, { passive: true });
	document.addEventListener('click', onClickCapture, true);
	/* v0.20.0 (CD-15). On the DOCUMENT for the same reason the gesture layer is:
	   Escape and the sheet's Tab trap are panel-wide facts, not one control's. */
	document.addEventListener('keydown', onKeyDown);

	/* The card grid's capacity now comes from the COLUMN COUNT, which is a media
	   query — so a slot change has to re-render or the panel keeps laying eight
	   cards into a four-cell grid until the next poll (up to 3 s of cards sliced
	   by the zone edge). Debounced because a drag-resize fires this continuously
	   and render() rebuilds the cards whenever the signature moves. */
	window.addEventListener('resize', function () {
		if (resizeTimer) clearTimeout(resizeTimer);
		resizeTimer = setTimeout(function () { resizeTimer = null; render(); }, 150);
	});

	ui.ready = true;
	/* CLEAN-07: the title is static in index.html. Nothing repairs it here. */
	applyProperties();
	tick();
	poll();
	setInterval(poll, POLL_MS);
	laneBInit();        /* lane B: the push transport, the gear chip and the bridge */
	laneEInit();        /* lane E: AFTER laneBInit, which is what proves the bridge */
	setInterval(tick, 1000);
	if (forcedTrick) startForcedTrick(forcedTrick);
	scheduleBlink();
}

/* Dev-only, mock mode only: hold one trick running so it can be photographed.
   The cooldown and the fleet's own conditions are bypassed for the forced juggle
   — that is what "forced" means — but reduced motion and quiet hours are NOT: a
   screenshot flag that made the panel move in a dark room would be photographing
   a widget that does not exist. */
function startForcedTrick(name) {
	if (trickLoop) clearInterval(trickLoop);
	function run() {
		if (name === 'juggle') fireJuggle(false, true);
		else if (name === 'bounce') fireBounce(false);
		else if (name === 'dance') fireDance(false, true);
		else fireSnap();
	}
	trickLoop = setInterval(run, name === 'juggle' ? JUGGLE_MS + 400 : name === 'dance' ? DANCE_MS + 600 : 1400);
	run();
}

/* Dev-only, mock mode only: hold one GESTURE'S rendering so it can be shot.

   Each of these drives the real code path and then holds its result rather than
   painting a picture of one — &swipe= goes through paintSwipe(), &pinflash= runs
   firePinFlash() with its timer suppressed, and &ackflash=1 makes the actual
   ack-all POST. A flag that drew its own approximation would be photographing a
   widget that does not exist, which is the same rule &crab= and &budget= keep. */
function maybeAutoGesture() {
	if (!mockName) return;

	if (ackFlashAuto) {
		ackFlashAuto = false;
		var n = ackAllWaiting();
		/* Holds the line the gesture would produce, INCLUDING its count — a fixture
		   with nothing waiting gets no banner, which is the same no-op a real
		   two-finger tap makes and is worth being able to photograph too. */
		if (n) showNotice('acknowledged ' + n, 'ack');
	}
	if (refreshFlashAuto) { refreshFlashAuto = false; showNotice('refreshing', 'pull'); }
	/* The pin flash needs the card in the DOM, so it waits for a document. pinAuto
	   has already put the pin in the map by now (applyPinOverride, above render). */
	if (pinFlashHold && pinFlashId === null) {
		var target = findAutoCard();
		if (target) firePinFlash(target, true);
	}

	/* LAST, and on EVERY document rather than once. The card grid is rebuilt
	   whenever its signature moves and a frozen transform lives on a node that
	   rebuild throws away — including the renders the two flags above just fired,
	   which is the whole reason this sits below them. */
	if (swipeFreeze) applySwipeFreeze();
}

function applySwipeFreeze() {
	var cards = ui.cards.querySelectorAll('.card.swipeable');
	if (!cards.length) return;
	var card = null;
	for (var i = 0; i < cards.length && !card; i++) {
		var id = cards[i].getAttribute('data-session-id');
		if (swipeFreeze === 'first' || id === swipeFreeze || id.indexOf(swipeFreeze) === 0) card = cards[i];
	}
	if (!card) return;
	card.classList.add('swiping');
	paintSwipe(card, swipeFreezePx, Number(getComputedStyle(card).opacity) || 1);
}

/* The session &pinflash= named, resolved the same way &pin= resolves it — off
   pinFlashAuto rather than pinAuto, which applyPinOverride has already spent. */
function findAutoCard() {
	if (!pinFlashAuto) return null;
	var sessions = lastGoodDoc && Array.isArray(lastGoodDoc.sessions) ? lastGoodDoc.sessions : [];
	for (var i = 0; i < sessions.length; i++) {
		var s = sessions[i];
		if (!s || !s.id) continue;
		if (pinFlashAuto === 'first' || s.id === pinFlashAuto || String(s.id).indexOf(pinFlashAuto) === 0) return s.id;
	}
	return null;
}

/* Runs once, on the first document that actually has sessions in it. */
function maybeAutoOpenSheet() {
	if (burnAuto) { burnAuto = false; openBurnSheet(); return; }
	/* The approval sheet opens on the first needs_input session carrying a live
	   pendingPermission, so the Approve/Deny variant can be photographed. */
	if (approvalAuto) {
		var sessions = lastGoodDoc && Array.isArray(lastGoodDoc.sessions) ? lastGoodDoc.sessions : [];
		for (var a = 0; a < sessions.length; a++) {
			var sa = sessions[a];
			if (sa && sa.state === 'needs_input' && sa.pendingPermission && typeof sa.pendingPermission === 'object') {
				approvalAuto = false;
				openSheet(sa.id);
				return;
			}
		}
	}
	/* The day drill opens ON TOP of the timeline it came from, exactly as a tap
	   would — so Back has somewhere to go and the flag photographs the real
	   navigation rather than a view that can only be closed. */
	if (dayAuto) { var d = dayAuto; dayAuto = null; openTimelineSheet(); openDaySheet(d); return; }
	if (timelineAuto) { timelineAuto = false; openTimelineSheet(); return; }
	/* v0.22.0. Runs through openHostSheet, so a fixture with no host figure opens
	   nothing at all — which is the inert path the flag must not paper over. */
	if (hostAuto) { hostAuto = false; openHostSheet(); return; }
	if (sheetAutoId && autoOpenMatch(sheetAutoId, true)) { sheetAutoId = null; return; }
	if (sheetAutoDetailId && autoOpenMatch(sheetAutoDetailId, false)) { sheetAutoDetailId = null; }
}

function autoOpenMatch(target, wantWaiting) {
	var sessions = lastGoodDoc && Array.isArray(lastGoodDoc.sessions) ? lastGoodDoc.sessions : [];
	for (var i = 0; i < sessions.length; i++) {
		var s = sessions[i];
		if (!s || (s.state === 'needs_input') !== wantWaiting) continue;
		if (target === 'first' || s.id === target || s.id.indexOf(target) === 0) {
			openSheet(s.id);
			return true;
		}
	}
	return false;
}


/* ---- lane A: the host sensor row, the GPU cell and the load charts ----------

   crabd serves four additive members inside `host`: `sensors` (a curated, capped
   list from HWiNFO's shared memory), `sensorsSource` (its provenance and its own
   freshness), `gpu` (nvidia-smi) and `load` (disk, network, commit, the busiest
   process). Everything below renders them, and everything below is presence-
   detected member by member, never on the block being truthy — the v0.21.0 rule,
   for the same reason: an older crabd sends none of this, a current one may send
   any member as null, and both have to land on "the segment is simply absent".

   WHICH SOURCE OWNS A TEMPERATURE. This block does, and since CLEAN-03 it is the
   only one: the vendor sensor bridge that used to own the CPU and GPU cells is
   retired, so the companion's readings are the only temperatures the panel has. */

var hostSensors = [];            /* host.sensors, as served */
var hostSensorsSource = null;    /* host.sensorsSource, or null when absent */
var hostGpu = null;              /* host.gpu when available:true, else null */
var hostLoad = null;             /* host.load */
var hostLoadStale = false;
var hostGpuStale = false;
/* The second ten-minute ring, beside hostRing and on the same poll. A ring of its
   own rather than four more members on hostRing's entries: these come from three
   samplers on a 5 s cadence against a 2 s document, so an entry here is null far
   more often than a cpu/mem one, and hostRuns' "a null breaks the line" rule would
   then be reading one sampler's silence as another's. */
var laneARing = [];
var laneACells = null;           /* built-once marker for the one span this block adds */
/* Which shared cells this block last painted. The CPU and GPU value spans belong to
   the bridge too, so "clear what I wrote" has to be a fact this block remembers
   rather than a guess from the current state. */
var laneAPaintedCpu = false;
var laneAPaintedGpu = false;
/* Freshness for the two samplers that carry their own timestamp. 30 s is the number
   the whole feed is called stale at and the number crabd's own sensorsSource uses,
   so the panel has ONE definition of old. */
var LANE_A_STALE_MS = 30000;
/* 11 characters, and it is the row's width guarantee rather than a preference: the
   CSS cap that backs it is 11 vmin (79.2 px at 2560x720), which is what pays for the
   extra cell. The number is chosen off the label that will actually paint. HWiNFO's CPU label here is "CPU (Tctl/Tdie)", which this block shortens to
   "Tctl/Tdie": 9 characters, inside the clamp with room, so the most ordinary label
   on this row is not the one that ellipses. */
var LANE_A_NAME_MAX = 11;
var LANE_A_SENSOR_CLASSES = [
	['vrm', /\bvrm\b|\bvsoc\b|\bmos\b|vcore\s*soc/i],
	['drive', /\bdrive\b|\bnvme\b|\bssd\b|\bhdd\b|\bdisk\b/i],
	['board', /motherboard|chipset|\bpch\b|\bsystem\b|ambient/i],
	['gpu', /\bgpu\b|\bvideo\b/i],
	['cpu', /\bcpu\b|\btctl\b|\btdie\b|\bccd\d*\b|\bdie\b|package/i]
];

/* One served sensor's class. Matched on the LABEL in specificity order, the same
   walk crabd ranks with — a VRM probe labelled "CPU VDDCR_VDD VRM (SVI3 TFN)" is a
   VRM temperature, and a CPU-first walk would file it as a CPU one. */
function laneASensorClass(sensor) {
	if (!sensor || typeof sensor !== 'object') return 'other';
	if (sensor.kind === 'fan') return 'fan';
	if (sensor.kind === 'power') return 'power';
	if (sensor.kind !== 'temp') return 'other';
	var label = String(sensor.name || '');
	for (var i = 0; i < LANE_A_SENSOR_CLASSES.length; i++) {
		if (LANE_A_SENSOR_CLASSES[i][1].test(label)) return LANE_A_SENSOR_CLASSES[i][0];
	}
	return 'other';
}

function laneAFirstOfClass(want) {
	for (var i = 0; i < hostSensors.length; i++) {
		if (laneASensorClass(hostSensors[i]) === want &&
			typeof hostSensors[i].value === 'number' && isFinite(hostSensors[i].value)) {
			return hostSensors[i];
		}
	}
	return null;
}

/* The fastest fan or pump, and how many there are. Compact BY MEASUREMENT: six fan
   cells do not fit the row (see DEV.md), and the one number that answers "is the
   cooling working" is the highest. The rest ride on title/aria. */
function laneAFan() {
	var best = null, count = 0, all = [];
	for (var i = 0; i < hostSensors.length; i++) {
		var s = hostSensors[i];
		if (laneASensorClass(s) !== 'fan') continue;
		if (typeof s.value !== 'number' || !isFinite(s.value)) continue;
		count++;
		all.push(laneASheetName(s) + ' ' + Math.round(s.value) + ' rpm');
		if (!best || s.value > best.value) best = s;
	}
	return best ? { sensor: best, count: count, all: all } : null;
}

/* HWiNFO's labels are already short and already name the sensor, so the shortener
   this replaced - which split on "/" to take the last segment of a vendor device
   path, and turned "CPU (Tctl/Tdie)" into "Tdie)" - was the wrong tool and went with
   that bridge. Here the cell key already says CPU or GPU, so the
   leading word is dropped, the brackets go with it, and the trailing
   "Temperature" the degree sign has already said goes too. Same 13-character
   clamp as the bridge's labels, so the row's width guarantee is one number. */
function shortHostSensorName(raw) {
	if (raw === undefined || raw === null) return '';
	var s = String(raw).replace(/\s+/g, ' ').trim();
	if (!s) return '';
	s = s.replace(/^(cpu|gpu|drive|disk)\s+/i, '');
	s = s.replace(/\s*\b(temperatures?|temps?)\b\s*$/i, '').trim();
	s = s.replace(/^\((.*)\)$/, '$1').trim();
	if (!s) return '';
	if (s.length > LANE_A_NAME_MAX) s = s.slice(0, LANE_A_NAME_MAX - 1).trim() + '…';
	return s;
}

/* A number that is a number, at the contract's own bounds. Mirrors hostPct's rule:
   a contract-legal null must never arrive as Number(null) === 0. */
function laneANum(v) {
	return (typeof v === 'number' && isFinite(v)) ? v : null;
}

function laneAAgeMs(iso, now) {
	if (!iso) return null;
	var t = Date.parse(iso);
	return isFinite(t) ? Math.max(0, now - t) : null;
}

/* Read the four members off the document. Called from renderHost, so the row and
   this share one clock and one source of truth. */
function renderHostExtras(host) {
	var have = !!(host && typeof host === 'object' && !Array.isArray(host));
	var now = Date.now();
	hostSensors = have && Array.isArray(host.sensors) ? host.sensors : [];
	hostSensorsSource = have && host.sensorsSource &&
		typeof host.sensorsSource === 'object' ? host.sensorsSource : null;
	/* `available:false` is an ANSWER — this machine has no NVIDIA card, or nvidia-smi
	   could not be run — and it is rendered as the absence of the reading, never as a
	   cell of em-dashes that reads like a broken sensor. */
	hostGpu = (have && host.gpu && typeof host.gpu === 'object' &&
		host.gpu.available === true) ? host.gpu : null;
	hostLoad = have && host.load && typeof host.load === 'object' ? host.load : null;
	var gpuAge = hostGpu ? laneAAgeMs(hostGpu.sampledAt, now) : null;
	hostGpuStale = gpuAge !== null && gpuAge > LANE_A_STALE_MS;
	var loadAge = hostLoad ? laneAAgeMs(hostLoad.sampledAt, now) : null;
	hostLoadStale = loadAge !== null && loadAge > LANE_A_STALE_MS;
}

function laneASensorsStale() {
	return !!(hostSensorsSource && hostSensorsSource.stale === true);
}

/* Does this block have anything to put on the row? Asked by syncSensorRow, which
   owns visibility for the whole row — the v0.21.0 rule that the row is assembled in
   ONE place from all of the state. */
/* Does the companion have a temperature of its own? Asked by the shipping sheet
   before it says there are none. Not laneAAnyCell: that one asks whether this block
   OWNS the row, which since CLEAN-03 it always does. */
function laneAHasTemps() {
	for (var i = 0; i < hostSensors.length; i++) {
		if (hostSensors[i] && hostSensors[i].kind === 'temp' &&
			typeof hostSensors[i].value === 'number' && isFinite(hostSensors[i].value)) {
			return true;
		}
	}
	return false;
}

function laneAAnyCell() {
	return !!(laneAGpuOn() || laneAFirstOfClass('cpu'));
}

/* The CPU cell can now be lit by a THIRD source. Until this block there were two -
   the bridge's temperature and the feed's utilization - and a machine with neither
   but with a readable HWiNFO mapping would have had its temperature computed, served,
   and then hidden by a visibility test that had never heard of it. */
function laneACpuOn() {
	return !!laneAFirstOfClass('cpu');
}

function laneAGpuOn() {
	return !!(laneAGpuTemp() !== null || (hostGpu && laneANum(hostGpu.utilPct) !== null));
}

/* The card's temperature, preferring nvidia-smi and falling back to HWiNFO's own
   GPU row. Both are real readings of the same die; nvidia-smi is preferred because
   it is the source that also carries the utilisation beside it. */
function laneAGpuTemp() {
	if (hostGpu) {
		var t = laneANum(hostGpu.tempC);
		if (t !== null) return t;
	}
	var s = laneAFirstOfClass('gpu');
	return s ? s.value : null;
}

/* The drive's friendly name, for title/aria: the part after "S.M.A.R.T.: " and
   before the bracketed serial. A serial number is not something to put on a panel. */
function laneADeviceLabel(device) {
	var s = String(device || '').replace(/^[^:]*:\s*/, '').trim();
	var cut = s.indexOf(' (');
	if (cut > 0) s = s.slice(0, cut);
	return s.replace(/\s*\[[^\]]*\]\s*$/, '').trim();
}

/* The one element this block adds to the row, built once and lazily. */
function laneABuildCells() {
	if (laneACells || !ui.sensors || !ui.hostMem) return laneACells;
	/* NO NEW CELL, and that is a measurement rather than a decision about taste.
	   This row is one line inside a zone that gives it 561.9 px at 2560x720 (the
	   v0.21.0 figure, re-measured on HEAD this session: unchanged), the cells do not
	   shrink, and the widest each can ever paint is fixed text plus the capped name:
	   CPU 228.9, GPU 145.0, MEM 95.8, three gaps 47.4 - 501.3 px, 58.7 px spare.
	   ONE more cell costs 93.1 px plus a 15.8 px gap and paints 610.3, which is
	   50.3 px PAST the zone edge (measured off the glass, not computed). So the VRM,
	   the drive, the fans and the package power go to the host sheet, which is one
	   tap away and has the width this line does not. The cells this block writes are
	   the two the row already has.
	   laneACells stays an object so the row has one owner either way. */
	laneACells = {};
	/* The GPU cell's utilisation segment, beside its temperature exactly as the CPU
	   cell's host figure sits beside its own. Appended to the SHIPPING cell rather
	   than to a new one: it is the same cell, and a second GPU cell would be the
	   panel saying there are two cards. */
	if (ui.sensorGpu && !ui.hostGpuVal) {
		var x = document.createElement('span');
		x.className = 'sensor-x';
		x.id = 'hostGpuVal';
		ui.sensorGpu.insertBefore(x, ui.sensorGpuWarn || null);
		ui.hostGpuVal = x;
	}
	return laneACells;
}

/* The threshold colouring, the bridge's rule applied to the companion's numbers:
   80/90 in Celsius, and a reading in any other unit is shown plainly rather than
   being called red at 80°F. */
function laneATempColor(value, unit) {
	var u = String(unit || '').replace(/^\s*°?/, '').toUpperCase();
	var isC = u === '' || u.charAt(0) === 'C';
	if (!isC) return 'var(--text-color)';
	return value >= SENSOR_RED_C ? 'var(--red)'
		: value >= SENSOR_AMBER_C ? 'var(--amber)'
		: 'var(--text-color)';
}

/* THE UNIT LETTER IS SPENT ONLY WHEN IT CHANGES THE MEANING. Celsius is the scale
   this row's 80/90 thresholds are in and the scale it colours against, so "60°" says
   everything "60°C" does — and the letter costs 11.0 px per cell, measured, which at
   four cells is most of a cell. A reading in any other unit KEEPS its letter, because
   there the letter is the whole difference between 140°F and a machine on fire. */
function laneAPaintTemp(el, value, unit) {
	var u = String(unit || '').replace(/^\s*°?/, '').toUpperCase();
	var bare = u === '' || u.charAt(0) === 'C';
	setText(el, Math.round(value) + (bare ? '°' : '°' + u.charAt(0)));
	setVar(el, '--sensor-color', laneATempColor(value, unit));
}

/* The row's lane A half, painted after syncSensorRow has decided the rest. */
function syncLaneASensorCells() {
	if (!laneABuildCells()) return;   /* the row is not in the DOM yet */
	var stale = laneASensorsStale();
	var cpu = laneAFirstOfClass('cpu');
	var gpuTemp = laneAGpuTemp();
	var cpuName = cpu ? shortHostSensorName(cpu.name) : '';
	/* The class carries the tighter name cap this block's labels are measured
	   against. Since CLEAN-03 it is the row's only state, so it is set once here
	   rather than switched with an owner. */
	ui.sensors.classList.add('host-sensors');

	if (cpu && ui.sensorCpuVal) {
		laneAPaintTemp(ui.sensorCpuVal, cpu.value, cpu.unit);
		ui.sensorCpuVal.classList.toggle('stale', stale);
		setText(ui.sensorCpuName, cpuName);
		ui.sensorCpuName.classList.toggle('shown', !!cpuName);
		if (cpu.name) {
			ui.sensorCpuName.setAttribute('title', String(cpu.name));
			ui.sensorCpuName.setAttribute('aria-label', String(cpu.name));
		}
		laneAPaintedCpu = true;
	} else if (laneAPaintedCpu) {
		/* WHAT THIS BLOCK WROTE, THIS BLOCK CLEARS, and the NAME goes with the value:
		   a crabd that stops serving `host.sensors` (a downgrade, or HWiNFO closing)
		   left "Tctl/Tdie" sitting beside the load percentage with no reading behind
		   it - a label for a temperature that is no longer on the glass. Guarded on
		   having painted, so a cell nothing wrote is a cell nothing clears. */
		laneAPaintedCpu = false;
		setText(ui.sensorCpuVal, '');
		ui.sensorCpuVal.classList.remove('stale');
		setText(ui.sensorCpuName, '');
		ui.sensorCpuName.classList.remove('shown');
		ui.sensorCpuName.removeAttribute('title');
		ui.sensorCpuName.removeAttribute('aria-label');
	}

	if (ui.sensorGpuVal) {
		laneAPaintedGpu = gpuTemp !== null;
		if (gpuTemp !== null) {
			var gpuUnit = hostGpu && laneANum(hostGpu.tempC) !== null ? 'C'
				: (laneAFirstOfClass('gpu') || {}).unit;
			laneAPaintTemp(ui.sensorGpuVal, gpuTemp, gpuUnit);
			ui.sensorGpuVal.classList.toggle('stale', hostGpu ? hostGpuStale : stale);
		} else {
			setText(ui.sensorGpuVal, '');
		}
		var util = hostGpu ? laneANum(hostGpu.utilPct) : null;
		if (ui.hostGpuVal) {
			setText(ui.hostGpuVal, util === null ? '' : Math.round(util) + '%');
			ui.hostGpuVal.classList.toggle('shown', util !== null);
			ui.hostGpuVal.classList.toggle('stale', hostGpuStale);
			if (util !== null && hostGpu && hostGpu.name) {
				ui.sensorGpu.setAttribute('title', String(hostGpu.name));
				ui.sensorGpu.setAttribute('aria-label', String(hostGpu.name) +
					' utilization ' + Math.round(util) + '%');
			}
		}
	}

}

/* ---- the ten-minute ring for the new series -------------------------------- */

function laneASampleHost(doc) {
	var h = doc && doc.host && typeof doc.host === 'object' && !Array.isArray(doc.host)
		? doc.host : null;
	var gpu = h && h.gpu && typeof h.gpu === 'object' && h.gpu.available === true
		? h.gpu : null;
	var load = h && h.load && typeof h.load === 'object' ? h.load : null;
	var now = Date.now();
	laneARing.push({
		t: now,
		gpu: gpu ? laneANum(gpu.utilPct) : null,
		gputemp: gpu ? laneANum(gpu.tempC) : null,
		commit: load ? laneANum(load.commitPct) : null,
		disk: laneASum(load, 'diskReadBps', 'diskWriteBps'),
		net: laneASum(load, 'netRxBps', 'netTxBps')
	});
	hostRingTrim(laneARing, now);
}

/* Two halves of one throughput figure. Null unless BOTH are readable: "1.2 MB/s of
   reads plus an unknown number of writes" is not a total, and adding a null as zero
   would draw a line that is wrong by exactly the part that could not be measured. */
function laneASum(load, a, b) {
	if (!load) return null;
	var x = laneANum(load[a]), y = laneANum(load[b]);
	return (x === null || y === null) ? null : x + y;
}

function laneARuns(key) {
	var runs = [], cur = [], prevT = null;
	for (var i = 0; i < laneARing.length; i++) {
		var s = laneARing[i];
		if (s[key] === null) {
			if (cur.length) runs.push(cur);
			cur = []; prevT = null;
			continue;
		}
		if (prevT !== null && s.t - prevT > HOST_GAP_MS) {
			if (cur.length) runs.push(cur);
			cur = [];
		}
		cur.push(s);
		prevT = s.t;
	}
	if (cur.length) runs.push(cur);
	return runs;
}

function laneACount(key) {
	var n = 0;
	for (var i = 0; i < laneARing.length; i++) { if (laneARing[i][key] !== null) n++; }
	return n;
}

function laneAMax(key) {
	var m = 0;
	for (var i = 0; i < laneARing.length; i++) {
		if (laneARing[i][key] !== null && laneARing[i][key] > m) m = laneARing[i][key];
	}
	return m;
}

/* ---- the host sheet's lane A half ------------------------------------------ */

function fmtRate(bps) {
	if (bps === null || bps === undefined) return EMDASH;
	if (bps >= 1048576) return (bps / 1048576).toFixed(1) + ' MB/s';
	if (bps >= 1024) return Math.round(bps / 1024) + ' KB/s';
	return Math.round(bps) + ' B/s';
}

/* Whether this block has anything for the sheet. Folded into hostSheetAvailable, so
   a machine whose kernel counters cannot be read but whose card can still opens. */
function laneAHostAvailable() {
	if (hostGpu) return true;
	if (hostSensors.length) return true;
	if (hostLoad) {
		var keys = ['diskReadBps', 'diskWriteBps', 'netRxBps', 'netTxBps', 'commitPct'];
		for (var i = 0; i < keys.length; i++) {
			if (laneANum(hostLoad[keys[i]]) !== null) return true;
		}
		if (hostLoad.topProcess && typeof hostLoad.topProcess === 'object') return true;
	}
	return false;
}

/* What the sheet's signature has to include, so a changed reading repaints it and
   an unchanged one does not. Same discipline as hostSig's own members. */
function laneAHostSig() {
	var top = hostLoad && hostLoad.topProcess ? hostLoad.topProcess : null;
	return [laneARing.length, hostSensors.length,
		hostSensorsSource ? hostSensorsSource.stale : null,
		hostSensorsSource ? hostSensorsSource.available : null,
		hostGpu ? hostGpu.tempC : null, hostGpu ? hostGpu.utilPct : null,
		hostGpu ? hostGpu.memUsedMB : null, hostGpu ? hostGpu.powerW : null,
		hostLoad ? hostLoad.diskReadBps : null, hostLoad ? hostLoad.netRxBps : null,
		hostLoad ? hostLoad.commitPct : null,
		top ? top.name + top.cpuPct : null].join('#');
}

function laneANote(text, stale) {
	var el = hostNote(text);
	if (stale) el.classList.add('stale');
	return el;
}

function appendLaneAHostBlocks() {
	if (!ui.sheetHost) return;
	/* THE GPU LINE. Absent when there is no card — an unavailable block is an answer,
	   and a row of em-dashes for a machine that has no NVIDIA GPU is not it. */
	if (hostGpu) {
		var bits = [];
		var used = laneANum(hostGpu.memUsedMB), total = laneANum(hostGpu.memTotalMB);
		if (used !== null && total !== null) {
			bits.push((used / 1024).toFixed(1) + ' / ' + (total / 1024).toFixed(1) +
				' GB VRAM');
		}
		var watts = laneANum(hostGpu.powerW), limit = laneANum(hostGpu.powerLimitW);
		if (watts !== null) {
			bits.push(Math.round(watts) + ' W' +
				(limit !== null ? ' of ' + Math.round(limit) + ' W' : ''));
		}
		var clock = laneANum(hostGpu.clockMHz);
		if (clock !== null) bits.push(Math.round(clock) + ' MHz');
		if (bits.length) {
			ui.sheetHost.appendChild(laneANote(
				(hostGpu.name ? String(hostGpu.name) + ' ' + EMDASH + ' ' : '') +
				bits.join(', '), hostGpuStale));
		}
	}

	/* THE LOAD LINE. Each half is independently null, so a machine whose PDH query
	   failed still gets its commit figure and its busiest process. */
	if (hostLoad) {
		var load = [];
		var dr = laneANum(hostLoad.diskReadBps), dw = laneANum(hostLoad.diskWriteBps);
		if (dr !== null || dw !== null) {
			load.push('disk ' + fmtRate(dr) + ' read, ' + fmtRate(dw) + ' write');
		}
		var rx = laneANum(hostLoad.netRxBps), tx = laneANum(hostLoad.netTxBps);
		if (rx !== null || tx !== null) {
			load.push('net ' + fmtRate(rx) + ' in, ' + fmtRate(tx) + ' out');
		}
		var commit = laneANum(hostLoad.commitPct);
		if (commit !== null) load.push('commit ' + Math.round(commit) + '%');
		var top = hostLoad.topProcess;
		if (top && typeof top === 'object' && laneANum(top.cpuPct) !== null) {
			load.push('top: ' + String(top.name || '?') + ' ' +
				top.cpuPct.toFixed(1) + '%');
		}
		if (load.length) {
			ui.sheetHost.appendChild(laneANote(load.join('     '), hostLoadStale));
		}
	}

	/* THE SENSOR PROVENANCE, said out loud. A row of temperatures with no statement
	   of where they came from or how old they are is the exact failure the v0.18.0
	   through v0.21.0 releases were about, one source along. */
	if (hostSensorsSource) {
		var src;
		if (hostSensorsSource.available !== true) {
			src = 'sensors ' + EMDASH + ' ' + (hostSensorsSource.note || 'unavailable');
		} else if (hostSensorsSource.stale === true) {
			src = 'sensors ' + EMDASH + ' ' + hostSensors.length + ' from HWiNFO, ' +
				laneAAgeWords(hostSensorsSource.ageSec) + ' old. ' +
				(hostSensorsSource.note || '');
		} else {
			src = 'sensors ' + EMDASH + ' ' + hostSensors.length + ' from HWiNFO, ' +
				laneAAgeWords(hostSensorsSource.ageSec) + ' old';
		}
		ui.sheetHost.appendChild(laneANote(src, hostSensorsSource.stale === true ||
			hostSensorsSource.available !== true));
	}

	/* THE TEMPERATURES THE ROW HAS NO WIDTH FOR. The row paints the CPU and the GPU
	   and is 58.7 px from its own edge doing it; everything else crabd curated - the
	   VRM, the drive, the board, the package power - is here, where a line can be as
	   long as the sheet is wide. Named, because a temperature with no subject is the
	   failure the v0.21.0 release was about. */
	var rest = laneAOtherReadings();
	if (rest.length) {
		ui.sheetHost.appendChild(laneANote(rest.join(', '), laneASensorsStale()));
	}

	/* THE FANS, for the same reason, and all of them: a stopped fan beside three
	   spinning ones is the reading somebody opened this view for - and a card fan at
	   0 rpm is a MEASURED zero, fan-stop at idle, not an absence. */
	var fans = laneAFan();
	if (fans) {
		ui.sheetHost.appendChild(laneANote('fans ' + EMDASH + ' ' + fans.all.join(', '),
			laneASensorsStale()));
	}

	/* TWO NEW CHARTS, AND THE COUNT IS A MEASUREMENT. This sheet's visible column is
	   541 px at 2560x720 and a chart costs 145 px of it (measured off the glass); the
	   shipped CPU and MEM charts plus their notes already spend 328. Four new charts
	   paint 1078 px and put half of this view below a fold the panel's own scrolling
	   is not proven to reach (v0.23.0). So the two series that have a SHAPE worth
	   seeing get charts - the card's utilisation, which swings, and disk throughput,
	   which spikes - and commit and network keep their numbers in the load line above,
	   where they cost one line each. */
	laneAChart('GPU', 'gpu', hostGpu ? laneANum(hostGpu.utilPct) : null, 100, '%');
	laneARateChart('DISK', 'disk', laneASum(hostLoad, 'diskReadBps', 'diskWriteBps'));
}

/* A reading's name FOR THE SHEET, which has width the row does not: the label as
   HWiNFO wrote it, minus only the trailing "Temperature" the degree sign already
   says. Deliberately not shortHostSensorName — that one strips the leading CPU/GPU
   word because the row's cell key had already said it, and here nothing has: it
   turns the integrated card's "GPU Temperature" into an empty string, which is a
   number with no subject. */
function laneASheetName(sensor) {
	var s = String((sensor && sensor.name) || '').replace(/\s+/g, ' ').trim();
	s = s.replace(/\s*(temperatures?|temps?)\s*$/i, '').trim();
	return s;
}

/* Every curated reading the row did not paint, in words: the VRM, the drive, the
   board, the package power, and any GPU probe beside the one on the row. Fans are a
   line of their own because they are the one kind whose zero means something.

   NAMES ARE DISAMBIGUATED RATHER THAN SUPPRESSED, which is the v0.24.0 rule turned
   around for a surface that can afford it: two cells showing one name name neither,
   but in a LIST the answer is to say which device each came from. Measured cause -
   this machine reports "GPU Temperature" from both the discrete card and the
   integrated one, and "Drive Temperature" three times from one SSD. */
function laneAOtherReadings() {
	var rowCpu = laneAFirstOfClass('cpu');
	var rowGpu = laneAFirstOfClass('gpu');
	var picked = [], counts = {}, i, s, name;
	for (i = 0; i < hostSensors.length; i++) {
		s = hostSensors[i];
		if (s === rowCpu || s === rowGpu) continue;
		var cls = laneASensorClass(s);
		if (cls === 'fan' || cls === 'other') continue;
		if (typeof s.value !== 'number' || !isFinite(s.value)) continue;
		name = laneASheetName(s);
		if (!name) continue;
		picked.push({ s: s, cls: cls, name: name });
		counts[name.toLowerCase()] = (counts[name.toLowerCase()] || 0) + 1;
	}
	var out = [];
	for (i = 0; i < picked.length; i++) {
		var row = picked[i];
		var unit = String(row.s.unit || '').replace(/^\s*°?/, '');
		var device = counts[row.name.toLowerCase()] > 1
			? laneADeviceLabel(row.s.device) : '';
		out.push(row.name + (device ? ' (' + device + ')' : '') + ' ' +
			(row.cls === 'power' ? Math.round(row.s.value) + ' ' + unit
				: Math.round(row.s.value) + '°' +
					(unit.toUpperCase().charAt(0) === 'C' ? '' : unit)));
	}
	return out;
}

function laneAAgeWords(sec) {
	if (typeof sec !== 'number' || !isFinite(sec)) return 'unknown age';
	if (sec < 90) return Math.round(sec) + ' s';
	if (sec < 5400) return Math.round(sec / 60) + ' min';
	return Math.round(sec / 3600) + ' h';
}

/* A percentage chart, the same shape and the same "collecting" floor appendHostChart
   uses — two points are a slope, not a trend. Written here rather than by widening
   that function because the rate charts below need a stated scale, and one function
   that is sometimes 0-100 and sometimes not is a scale nobody can read off. */
function laneAChart(label, key, nowValue, scaleMax, unit) {
	if (!laneACount(key) && nowValue === null) return;
	laneAAppendChart(label, key, nowValue === null ? EMDASH
		: Math.round(nowValue) + unit, '0-' + scaleMax + unit, scaleMax);
}

function laneARateChart(label, key, nowValue) {
	if (!laneACount(key) && nowValue === null) return;
	/* THE SCALE IS STATED, never implied. A throughput has no natural 100%, so the
	   axis is the window's own peak — and printing it in the head is what keeps that
	   honest: without it a flat idle line and a flat saturated line look identical. */
	var peak = Math.max(laneAMax(key), nowValue === null ? 0 : nowValue, 1024);
	laneAAppendChart(label, key, fmtRate(nowValue), '0-' + fmtRate(peak), peak);
}

function laneAAppendChart(label, key, nowText, rangeText, scaleMax) {
	var wrap = document.createElement('div');
	wrap.className = 'hs-chart';
	var head = document.createElement('div');
	head.className = 'hs-head';
	var name = document.createElement('span');
	name.className = 'hs-name';
	name.textContent = label;
	var now = document.createElement('span');
	now.className = 'hs-now';
	now.textContent = nowText;
	var range = document.createElement('span');
	range.className = 'hs-range';
	range.textContent = rangeText;
	head.appendChild(name);
	head.appendChild(now);
	head.appendChild(range);
	wrap.appendChild(head);

	var have = laneACount(key);
	if (have < HOST_MIN_SAMPLES) {
		wrap.appendChild(hostNote('collecting ' + EMDASH + ' ' + have + ' of ' +
			HOST_MIN_SAMPLES + ' samples'));
		ui.sheetHost.appendChild(wrap);
		return;
	}
	wrap.appendChild(laneAPlot(key, scaleMax));
	var axis = document.createElement('div');
	axis.className = 'hs-axis';
	var l = document.createElement('span');
	l.textContent = '10 min ago';
	var r = document.createElement('span');
	r.textContent = 'now';
	axis.appendChild(l);
	axis.appendChild(r);
	wrap.appendChild(axis);
	ui.sheetHost.appendChild(wrap);
}

/* buildHostPlot's mechanism against this ring and a stated scale: time on x so a run
   of missed polls leaves a hole of the right width, and nothing bridged — a straight
   segment across a gap is a reading nobody took. */
function laneAPlot(key, scaleMax) {
	var W = 1000, H = 100;
	var svg = document.createElementNS(SVG_NS, 'svg');
	svg.setAttribute('class', 'hs-plot');
	svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
	svg.setAttribute('preserveAspectRatio', 'none');
	svg.setAttribute('aria-hidden', 'true');
	svg.setAttribute('focusable', 'false');

	var half = document.createElementNS(SVG_NS, 'line');
	half.setAttribute('class', 'hs-grid');
	half.setAttribute('x1', '0');
	half.setAttribute('x2', String(W));
	half.setAttribute('y1', String(H / 2));
	half.setAttribute('y2', String(H / 2));
	svg.appendChild(half);

	var now = Date.now();
	var t0 = now - HOST_WINDOW_MS;
	var top = scaleMax > 0 ? scaleMax : 1;
	function px(t) { return Math.max(0, Math.min(W, ((t - t0) / HOST_WINDOW_MS) * W)); }
	function py(v) { return H - (Math.max(0, Math.min(top, v)) / top) * H; }

	var runs = laneARuns(key);
	for (var i = 0; i < runs.length; i++) {
		var run = runs[i];
		if (run.length === 1) {
			var dot = document.createElementNS(SVG_NS, 'circle');
			dot.setAttribute('class', 'hs-dot');
			dot.setAttribute('cx', px(run[0].t).toFixed(1));
			dot.setAttribute('cy', py(run[0][key]).toFixed(1));
			dot.setAttribute('r', '2');
			svg.appendChild(dot);
			continue;
		}
		var pts = [];
		for (var j = 0; j < run.length; j++) {
			pts.push(px(run[j].t).toFixed(1) + ',' + py(run[j][key]).toFixed(1));
		}
		var line = document.createElementNS(SVG_NS, 'polyline');
		line.setAttribute('class', 'hs-line');
		line.setAttribute('points', pts.join(' '));
		svg.appendChild(line);
	}
	return svg;
}

/* ---- lane E: bring a session to the front ---- */

/* Provisional labels: widget v0.31.0, host 0.3.0.

   The operator is standing at the Edge, a card says a session wants an answer, and
   the keyboard is two feet away in front of a display holding nine windows. This
   control asks the panel HOST to put that session's window in front. The answer is
   then typed where it was always going to be typed.

   WHAT IT DOES NOT DO, deliberately and permanently. Nothing on this page and
   nothing in the host types into a session, pastes into one, or clicks anything
   inside one. Answering a session's multiple-choice question from the glass has no
   supported path (see docs/notes/lane-e-spike.md), and the unsupported ones are all
   the same thing: synthesising input into a window on the operator's behalf, which
   is prompt injection wearing a different hat. Bringing the window forward is the
   mitigation, and the person answers.

   GATED ON THE HOST'S OWN ANSWER, not on the page's address - the rule the settings
   save keeps. A browser preview at /panel/ has no desktop to reach and no host to
   ask, and a panel host that does not offer focusSession says so. The controls are
   BUILT rather than hidden, so where the capability is absent they do not exist.

   ONE STATUS, TWO SURFACES. The sheet's shared status line is display:none in
   detail mode (.sheet[data-mode="detail"] .sheet-status), which is why the continue
   row carries its own, and why this lane carries its own twice: one line in the
   sheet and one in the Detail head, both painted from the same two variables so a
   result cannot show on one surface and not the other. */

var LANE_E_STATUS_MS = 6000;

/* What the host's refusal reasons say on the glass. Every branch is a sentence the
   operator can act on: "no window" means answer at the keyboard the ordinary way,
   "more than one" means the ranking would have had to guess. */
var LANE_E_REASONS = {
	'no-window': 'no window found for this session',
	'no-match': 'no window found for this session',
	'ambiguous': 'more than one window could be this session',
	'refused': 'Windows would not change the foreground window',
	'enumerate-failed': 'The host could not read the desktop'
};

var laneEStatusText = '';
var laneEStatusKind = '';
var laneEStatusTimer = null;
var laneEPending = null;
var laneESheetStatus = null;
var laneEDetailStatus = null;

/* The control exists when a host is there to answer it AND has said it can focus a
   window. Before the handshake lands this is false, so the button appears with the
   host rather than promising ahead of it. */
function laneEAvailable() { return panelBridge() !== null && hostCan('focusSession'); }

function laneEButton(cls, label) {
	var b = document.createElement('button');
	b.type = 'button';
	b.className = cls;
	/* Its own attribute, matched above the generic .sheet-btn branch in onSheetClick
	   exactly as Dismiss, Pin and Full view are: it wears .sheet-btn for its looks
	   and carries no data-sheet-action, and the generic branch would POST an action
	   of null to crabd. */
	b.setAttribute('data-focus-session', '1');
	b.textContent = label;
	return b;
}

/* The session a tap belongs to. The SHEET wins when one is open, the same rule
   laneCActionSessionId keeps; detailTarget() is the fallback because the Detail
   page renders a chosen session OR the one the operator would have picked, and the
   control has to follow what is on the glass. */
function laneETarget() {
	var id = laneCActionSessionId();
	var s = id ? findSession(id) : null;
	if (s) return s;
	return currentView().key === 'detail' ? detailTarget() : null;
}

function laneESendFocus() {
	var s = laneETarget();
	if (!s || !s.id) return;
	if (!laneEAvailable()) { laneESetStatus('this panel host cannot bring a window to the front', 'err'); return; }
	laneEPending = String(s.id);
	laneESetStatus('bringing it to the front', 'pending');
	/* Four FACTS about a session, and never a window handle: the host ranks the
	   windows it enumerated itself. A handle from here would be a window picker a
	   visited page could aim anywhere on the desktop. */
	var sent = bridgeSend('focus-session', {
		sessionId: String(s.id),
		title: typeof s.title === 'string' ? s.title : '',
		cwd: typeof s.cwd === 'string' ? s.cwd : '',
		repo: typeof s.repo === 'string' ? s.repo : ''
	}, function () {
		/* SCA-021: the deadline, and the one place a focus request that was accepted
		   and never answered stops reading as still running. */
		laneEPending = null;
		laneESetStatus('no answer from the host', 'err');
	});
	if (!sent) {
		laneEPending = null;
		laneESetStatus('the host refused the message', 'err');
	}
}

function laneEOnFocusResult(msg) {
	/* The requestId has already matched by the time this runs. The session is checked
	   as well because the two are meant to agree, and a host that answers about a
	   different session is one whose answer this page cannot use. */
	if (laneEPending !== null && String(msg.sessionId) !== laneEPending) {
		laneEPending = null;
		laneESetStatus('the host answered about a different session', 'err');
		return;
	}
	laneEPending = null;
	if (msg.ok === true) {
		/* Two different true answers, and the page must not blur them. The desktop app
		   holds every session that has no window of its own in ONE window, so the host
		   could not have found "this session's window" - it found the app, and the
		   session is picked in its sidebar. Saying "brought to front" there would be a
		   claim the host never made. */
		laneESetStatus(msg.reason === 'desktop-app'
			? 'brought the Claude app to the front'
			: 'brought to front', 'ok');
		return;
	}
	var reason = typeof msg.reason === 'string' ? msg.reason : '';
	laneESetStatus(LANE_E_REASONS[reason] || LANE_E_REASONS['no-window'], 'err');
}

function laneESetStatus(text, kind) {
	laneEStatusText = text || '';
	laneEStatusKind = kind || '';
	laneEPaintStatus();
	if (laneEStatusTimer) { clearTimeout(laneEStatusTimer); laneEStatusTimer = null; }
	/* A pending line stays until the host answers; a settled one clears itself, so
	   a result from five minutes ago is never read as this tap's. */
	if (laneEStatusText && laneEStatusKind !== 'pending') {
		laneEStatusTimer = setTimeout(function () {
			laneEStatusTimer = null;
			laneEStatusText = '';
			laneEStatusKind = '';
			laneEPaintStatus();
		}, LANE_E_STATUS_MS);
	}
}

function laneEPaintStatus() {
	laneEPaintStatusNode(laneESheetStatus, 'sheet-focus-status');
	laneEPaintStatusNode(laneEDetailStatus, 'dv-focus-status');
}

function laneEPaintStatusNode(el, base) {
	if (!el) return;
	setText(el, laneEStatusText);
	el.className = base + (laneEStatusKind ? ' ' + laneEStatusKind : '');
}

/* The Detail head's status node, re-made on every rebuild of that page and re-filled
   from the same two variables. renderDetailView rebuilds whenever the session's own
   facts change, which can land inside the six seconds a result is on screen. */
function laneEDetailStatusNode() {
	var el = document.createElement('div');
	laneEDetailStatus = el;
	laneEPaintStatusNode(el, 'dv-focus-status');
	return el;
}

/* BOOT ORDER, and it moved in v0.32.0. The control used to need only a bridge,
   which is known synchronously at boot; it now needs the host to have SAID it can
   focus a window, and that answer arrives a round trip later. So this runs at boot
   AND again when the handshake lands, and is idempotent - without the second call
   the row would never be built at all, which is what the recheck of this diff
   caught. The Detail page's own button is rebuilt on every render and needs no
   equivalent. */
var laneERowBuilt = false;

function laneEInit() {
	if (laneERowBuilt || !laneEAvailable()) return;
	var pinRow = ui.sheetPin ? ui.sheetPin.parentNode : null;
	if (!pinRow || !pinRow.parentNode) return;
	laneERowBuilt = true;
	/* Its OWN row, below the pin row and not in it. The pin row's own comment is the
	   argument: it carries Pin and Full view at a 48 px fingertip floor, and a third
	   control joining them moves both under a finger already travelling toward one. */
	var row = document.createElement('div');
	row.className = 'sheet-focus-actions';
	row.appendChild(laneEButton('sheet-btn sheet-btn-focus', 'Bring to front'));
	var status = document.createElement('span');
	laneESheetStatus = status;
	laneEPaintStatusNode(status, 'sheet-focus-status');
	row.appendChild(status);
	pinRow.parentNode.insertBefore(row, pinRow.nextSibling);
}

/* ======================================================================
   ---- lane C: the grid-zone view switcher ----

   The grid zone showed one thing: the session cards. It now shows four, chosen
   by the chips in its own header, and the identity and Limits zones are
   untouched by any of it.

     sessions  the card grid, exactly as before. The filter and density chips
               narrow THIS view and no other, which is why they stay where they
               were rather than joining the switcher.
     burn      today's spend, full width: by session, by model, and the 24 h
               series the Limits sparkline only has room to sketch.
     week      recap.week as a seven-day strip with the day drill INLINE.
     detail    one session as a page, with the controls its sheet already has.

   THE CARD GRID KEEPS ITS LAYOUT WHILE THE OTHER VIEWS ARE UP, and that is a
   trap rather than a tidiness preference. gridCapacity() reads the computed
   grid-template lists off #cards, and a display:none grid computes both axes to
   the single token "none" — trackCount() would fall back to its 4x2 default and
   a compact or a narrow slot would come back to the wrong capacity. The
   stylesheet therefore gives #cards a zero height instead, where both axes still
   resolve to a list of lengths and the count survives. Measured both ways; see
   docs/notes/lane-c-dev.md.

   AN ALERT NEVER HIDES BEHIND A VIEW, and it never yanks the glass out from
   under a fingertip either. A session that flips to needs_input while another
   view is up is counted on the Sessions chip and the chip pulses; nothing
   switches by itself. Forcing the view would move a control out from under a
   finger already travelling toward it, which is the failure every sheet on this
   panel is built to avoid. */

var VIEWS = [
	{ key: 'sessions', chip: 'chipViewSessions', el: null },
	{ key: 'burn',     chip: 'chipViewBurn',     el: 'viewBurn' },
	{ key: 'week',     chip: 'chipViewWeek',     el: 'viewWeek' },
	{ key: 'detail',   chip: 'chipViewDetail',   el: 'viewDetail' }
];
/* The property NAME inside the same vendor-storage object the filter, the
   density and the pin map already share (see savePrefs). One object per widget
   instance is the vendor's own pattern; a second key would be a second thing to
   keep in step. */
/* The heading each alternate view keeps while it has nothing to draw. The view's
   own name, not a shared "no data": the operator chose this page and it is still
   the page they are on. */
var VIEW_FEED_HEADS = { burn: 'Today', week: 'Last 7 days', detail: 'Session' };
var viewFeedSig = null;
var VIEW_PROP = 'gridView';
var viewIdx = 0;
var viewStoredUnknown = null;  /* a value a NEWER build wrote — round-tripped, not replaced */
var viewForced = null;         /* dev-only &view=, mock mode only, in memory only */
var detailSessionId = null;    /* never persisted: a stored id restores a page for a session that has gone */
/* SCA-006. The Detail page's half of the surface generation. sheetGen is the
   sheet's half and already moves on every open and close; this moves whenever the
   page changes its subject, including the fall-through pick in detailTarget. A
   receipt writes itself only while BOTH still read what they read when it was
   sent. */
var detailGen = 0;
var viewAlerts = {};           /* ids that started waiting while another view was up */
var viewPrevState = {};
var viewPrevSeeded = false;
var viewBadgeSig = null;
var burnViewSig = null;
var weekViewSig = null;
var detailViewSig = null;
var detailContinueSig = null;
var HEAD_SWIPE_PX = 48;        /* header travel that commits to a view change; 4x the tap slop */
var headSwipe = null;

/* The day drilled INSIDE the week view. Its own state, because the sheet's day
   drill is a different surface with its own navigation — but NOT its own fetch:
   both go through fetchHistory(), which owns the mock routing, the timeout, the
   abort and the rebase. A second fetch here would be a second copy of four
   behaviours that have each already been got wrong once. */
var weekDay = null;
var weekDayDoc = null;
var weekDayBusy = false;
var weekDayReq = 0;
var weekDayFail = null;

function currentView() { return VIEWS[viewIdx] || VIEWS[0]; }

/* Every entry point routes through here, so the body attribute, the stored
   preference, the card signature and the repaint can never disagree. cardSig is
   cleared for the reason cycleDensity clears it: capacity is read off the grid's
   computed rows and the class that decides them has only just moved. */
function setGridView(key) {
	var i = prefIndexOrNone(VIEWS, key);
	if (i < 0) return;
	viewIdx = i;
	/* The tap is the operator overriding whatever a newer build had stored — the
	   rule cycleFilter keeps, in a third place. */
	viewStoredUnknown = null;
	savePrefs();
	if (VIEWS[i].key === 'sessions') viewAlerts = {};
	applyGridView();
	burnViewSig = weekViewSig = detailViewSig = null;
	cardSig = '';
	render();
}

function applyGridView() {
	var key = currentView().key;
	if (document.body.getAttribute('data-grid-view') !== key) {
		document.body.setAttribute('data-grid-view', key);
	}
	syncViewVisibility();
}

/* Whether the stylesheet is SHOWING the view switcher at this size. Read off a
   chip's computed display rather than a breakpoint copied into JS: the stylesheet
   owns the breakpoints in this zone (the 1660 px block, and gridCapacity reads its
   track lists the same way), and a second copy of the number would drift the first
   time either moved. Defaults to "usable" where there is no computed style to read,
   because that is the wide case this panel normally runs in. */
function viewSwitcherUsable() {
	var chip = ui[VIEWS[1].chip];
	if (!chip || typeof window === 'undefined' || typeof window.getComputedStyle !== 'function') return true;
	try { return window.getComputedStyle(chip).display !== 'none'; } catch (e) { return true; }
}

/* SCA-007 — THE ACCESSIBILITY TREE AND THE TAB ORDER FOLLOW THE VISIBLE VIEW.

   The three alternate views shipped with a static aria-hidden="true" and nothing
   ever cleared it, so a visible Burn, Week or Detail page was a page a screen
   reader could not see. The card grid is the other half and the worse one: it is
   never display:none while a view is up (gridCapacity reads its computed track
   lists, and a display:none grid computes both axes to the single token "none"), so
   its cards stayed in the tab order as zero-height targets - Tab reached a card
   nobody could see and Enter opened its sheet.

   THE NARROW FALLBACK IS THE CASE THAT IS EASY TO MISS: below 1660 px the
   stylesheet hides every alternate view and brings the cards back whatever the
   stored view says, so the visible view there is the cards and this has to agree
   with the stylesheet rather than with the stored preference. */
function syncViewVisibility() {
	var shown = viewSwitcherUsable() ? currentView().key : 'sessions';
	var cardsHidden = shown !== 'sessions';
	setRegionInert(ui.cards, cardsHidden);
	setRegionInert(ui.gridEmpty, cardsHidden);
	for (var i = 0; i < VIEWS.length; i++) {
		if (!VIEWS[i].el) continue;
		setRegionInert(ui[VIEWS[i].el], VIEWS[i].key !== shown);
	}
}

/* aria-hidden AND inert, together, because they answer two different questions: one
   takes the region out of the accessibility tree, the other takes everything inside
   it out of the tab order and out of reach of a pointer.
   INERT RATHER THAN A TABINDEX SWEEP: the cards are rebuilt from scratch on every
   render and each carries its own focusable controls, so a sweep would have to run
   again after every rebuild and would be wrong for the frame in between. inert is
   inherited by whatever is inside and survives the rebuild. */
function setRegionInert(el, hidden) {
	if (!el) return;
	if ((el.getAttribute('aria-hidden') === 'true') !== hidden) {
		el.setAttribute('aria-hidden', hidden ? 'true' : 'false');
	}
	if (!el.hasAttribute || el.hasAttribute('inert') !== hidden) {
		if (hidden) el.setAttribute('inert', '');
		else el.removeAttribute('inert');
	}
}

function stepGridView(delta) {
	var n = VIEWS.length;
	setGridView(VIEWS[((viewIdx + delta) % n + n) % n].key);
}

/* Called from render(), after the cards are in the DOM: the badge counts off the
   same document the grid was just built from. */
function laneCRenderViews(doc, sessions, status, quiet) {
	trackViewAlerts(sessions);
	syncViewChips();
	/* A slot change is a media query, not an event this file hears about: render()
	   is debounced onto the resize, so re-deciding here is what keeps the narrow
	   fallback's tab order right after the panel is made narrower. */
	syncViewVisibility();
	var key = currentView().key;
	if (key === 'sessions') { viewFeedSig = null; return; }
	/* SCA-018 — AN UNAVAILABLE FEED IS NOT AN EMPTY ONE, in every view and not only
	   in the Sessions page. A panel saved on Detail and restarted against a
	   companion that was absent, or that answered with a schema this build cannot
	   read, rendered "No active Claude sessions." - a confident statement about a
	   fleet nobody had managed to ask about. Each alternate view had its own empty
	   wording and none of them knew the difference, so the state is decided ONCE
	   here and the three views are handed it. */
	if (!everHadData) {
		if (renderViewFeedState(key)) burnViewSig = weekViewSig = detailViewSig = null;
		return;
	}
	viewFeedSig = null;
	if (key === 'burn') renderBurnView(doc);
	else if (key === 'week') renderWeekView(doc);
	else if (key === 'detail') renderDetailView(sessions, quiet);
}

/* The one sentence about a feed that has told this panel nothing it can use. Two
   of them, because the two causes send the operator to different places: a
   companion that is not running is a setup step, and a companion that IS running
   and speaks a shape this build does not know is a version mismatch. Neither is
   ever phrased as a fact about the sessions. */
function feedAbsentNote() {
	return feedUnreadable
		? 'The SideCrab companion is answering with a feed this panel cannot read ' +
		  EMDASH + ' the two need to be the same release.'
		: 'Claude Code stats need the SideCrab companion ' + EMDASH +
		  ' see the setup notes for how to start it.';
}

/* Paints the shared state into ONE alternate view. Returns true when it painted, so
   the caller can drop the view's own signature and make the next good document a
   full rebuild. The view chips are untouched and stay reachable (the stylesheet's
   connecting rule keeps them), so this state always has a way out of it. */
function renderViewFeedState(key) {
	var i = prefIndexOrNone(VIEWS, key);
	var root = i >= 0 && VIEWS[i].el ? ui[VIEWS[i].el] : null;
	if (!root) return false;
	var sig = key + '#' + (feedUnreadable ? 'unreadable' : 'absent');
	if (sig === viewFeedSig) return false;
	viewFeedSig = sig;
	root.textContent = '';
	root.appendChild(viewHead(VIEW_FEED_HEADS[key] || 'SideCrab', ''));
	root.appendChild(viewNote(feedAbsentNote()));
	return true;
}

/* The EDGE, never the value — the rule detectTricks states for the party hat and
   the reason is the same one: a view opened beside three waiting sessions has not
   just been handed three alerts, and a panel that booted into the Burn view has
   watched nothing happen at all.

   THE BASELINE IS THE FIRST DOCUMENT, NOT THE FIRST RENDER, and that distinction
   was a real defect for one round: render() runs once before any poll has landed,
   so the first render seeded an EMPTY map and the first real document then read
   every waiting session as brand new. Photographed on ?mock=rework&view=burn: a
   badge of 1 on a panel that had been up for two seconds. everHadData is the
   panel's own answer to "has a document arrived", so it is what gates the seed. */
function trackViewAlerts(sessions) {
	var away = currentView().key !== 'sessions';
	var next = {};
	var i, s, id;
	for (i = 0; i < sessions.length; i++) {
		s = sessions[i];
		if (!s || !s.id) continue;
		next[String(s.id)] = s.state;
		if (away && viewPrevSeeded && s.state === 'needs_input' &&
			viewPrevState[String(s.id)] !== 'needs_input') viewAlerts[String(s.id)] = 1;
	}
	viewPrevState = next;
	if (everHadData) viewPrevSeeded = true;
	/* An alert answered ANYWHERE — at the keyboard, by the crab tap, by the session
	   moving on — stops counting here too. The badge is a count of what is still
	   waiting, not a tally of what once arrived. */
	for (id in viewAlerts) {
		if (!Object.prototype.hasOwnProperty.call(viewAlerts, id)) continue;
		var live = findSession(id);
		if (!live || live.state !== 'needs_input' || effectiveAcked(live)) delete viewAlerts[id];
	}
}

function viewAlertCount() {
	var n = 0;
	for (var id in viewAlerts) { if (Object.prototype.hasOwnProperty.call(viewAlerts, id)) n++; }
	return n;
}

function syncViewChips() {
	var key = currentView().key;
	for (var i = 0; i < VIEWS.length; i++) {
		var el = ui[VIEWS[i].chip];
		if (!el) continue;
		var on = VIEWS[i].key === key ? '1' : '';
		if (el.getAttribute('data-active') !== on) el.setAttribute('data-active', on);
	}
	var n = viewAlertCount();
	var sig = key + '#' + n;
	if (sig === viewBadgeSig) return;
	viewBadgeSig = sig;
	if (ui.viewBadge) {
		setText(ui.viewBadge, n ? String(n) : '');
		ui.viewBadge.classList.toggle('shown', n > 0);
	}
	var chip = ui[VIEWS[0].chip];
	if (!chip) return;
	chip.setAttribute('data-alert', n > 0 ? '1' : '');
	/* The WORD, not only the number: this glass is read from across a room and
	   reported from photographs, so a count that exists only as a badge is a count
	   somebody misses. It rides on aria-label, where the sensor names and the diag
	   chip already keep their long form. */
	chip.setAttribute('aria-label', n > 0
		? 'Grid view: session cards ' + EMDASH + ' ' + n + ' now waiting'
		: 'Grid view: session cards');
}

/* ---------------------------------------------------------- the burn view */

/* Today's spend at full width. Every honesty rule the Limits zone keeps applies
   here unchanged: an absent figure is an em-dash or an omitted block, never a
   zero, and the by-session rows are labelled as live sessions because they do
   NOT add up to the day total (subagent spend and ended sessions are in the
   total and cannot be in the rows). */
function renderBurnView(doc) {
	var burn = doc && doc.burn ? doc.burn : null;
	var sessions = doc && Array.isArray(doc.sessions) ? doc.sessions : [];
	var rows = [];
	for (var i = 0; i < sessions.length; i++) {
		var s = sessions[i];
		if (!s) continue;
		var v = s.todayOutputTokens;
		/* typeof, not Number(): a session crabd has no figure for is left OUT of the
		   list rather than listed at zero. A row of zeroes reads as a quiet session,
		   which is the opposite of "nobody measured this one". */
		if (typeof v !== 'number' || !isFinite(v)) continue;
		rows.push({ id: s.id, title: titleParts(s).text, model: shortModel(s.model) || EMDASH, tokens: v });
	}
	rows.sort(function (a, b) { return b.tokens - a.tokens; });

	var byModel = burn && Array.isArray(burn.byModel) ? burn.byModel.slice(0, BYMODEL_MAX) : [];
	var hourly = burn && Array.isArray(burn.hourly) ? burn.hourly.slice(-SPARK_BUCKETS) : [];
	var today = burn && burn.today ? burn.today : null;
	var cost = burn && typeof burn.costUSD === 'number' && isFinite(burn.costUSD) ? burn.costUSD : null;
	var budget = burn && burn.budget && typeof burn.budget === 'object' && !Array.isArray(burn.budget)
		? burn.budget : null;

	/* SCA-013 — EVERY RENDERED FIELD IS IN THE SIGNATURE, and that is the rule
	   rather than a list to keep in step by hand: a cache key that omits a field
	   this function paints is a field that goes stale on the glass until something
	   unrelated moves. Two were missing and both were visible. The session TITLE is
	   the first column of the by-session list, so a session renamed mid-run (a
	   resolved title arriving after the first transcript line) went on being
	   attributed to its old name. today.inputTokens is a figure in the head line,
	   so a burn that was all input - a long read with nothing written back - left
	   the head reading the same number for as long as it lasted.
	   The rows are keyed on what is DRAWN (title, model, tokens), not on the raw
	   session, so a change nothing paints still costs no rebuild. */
	var sig = rows.map(function (r) { return r.id + ':' + r.tokens + ':' + r.model + ':' + r.title; }).join('|') +
		'#' + byModel.map(function (m) { return String(m && m.model) + ':' + (m && m.outputTokens); }).join(',') +
		'#' + hourly.map(function (h) { return String(h && h.hourStart) + ':' + (h && h.outputTokens); }).join(',') +
		'#' + (today ? today.outputTokens + '/' + today.inputTokens + '/' + today.messages : '') +
		'#' + cost + '#' + (budget ? budget.dailyOutputTokens + '/' + budget.todayPct : '');
	if (sig === burnViewSig) return;
	burnViewSig = sig;

	var root = ui.viewBurn;
	root.textContent = '';
	root.appendChild(viewHead('Today', burnHeadNote(today, cost, budget)));

	var split = document.createElement('div');
	split.className = 'bv-split';
	/* Each half is PRESENCE-GATED and says so when it is missing. The feed that
	   carries neither renders two stated absences and a chart, which is an honest
	   page; a feed that carries both renders the page this view is for. */
	split.appendChild(rows.length
		? burnList('by session (live sessions)', rows.map(function (r) {
			return { a: r.title, b: r.model, c: fmtNum(r.tokens) };
		}))
		: burnAbsent('by session', 'This companion serves no per-session token figure.'));
	split.appendChild(byModel.length
		? burnList('by model', byModel.map(function (m) {
			var v = m && m.outputTokens;
			return {
				a: shortModel(m && m.model) || EMDASH,
				b: '',
				c: typeof v === 'number' && isFinite(v) ? fmtNum(v) : EMDASH
			};
		}))
		: burnAbsent('by model', 'This feed carries no by-model split.'));
	root.appendChild(split);
	root.appendChild(burnChart(hourly, budget));
}

/* The head line: the day's own totals, the dollar figure when telemetry is
   flowing, and the budget percentage when one is configured. Each clause is
   dropped entirely when its source is absent — a "$0.00" derived from an absent
   cost is the one thing this line must never print. */
function burnHeadNote(today, cost, budget) {
	var parts = [];
	parts.push((today ? fmtNum(today.outputTokens) : EMDASH) + ' out');
	parts.push((today ? fmtNum(today.inputTokens) : EMDASH) + ' in');
	parts.push((today ? fmtNum(today.messages) : EMDASH) + ' msgs');
	if (cost !== null) parts.push('$' + cost.toFixed(2));
	var pct = budget && typeof budget.todayPct === 'number' && isFinite(budget.todayPct) && budget.todayPct >= 0
		? Math.round(budget.todayPct * 100) : null;
	if (pct !== null) {
		parts.push('budget ' + pct + '%' +
			(pct >= BUDGET_RED_PCT ? ' ' + EMDASH + ' far over' : pct >= BUDGET_AMBER_PCT ? ' ' + EMDASH + ' over' : ''));
	}
	return parts.join('   ' + EMDASH + '   ');
}

function burnList(label, items) {
	var wrap = document.createElement('div');
	wrap.className = 'bv-panel';
	var head = document.createElement('div');
	head.className = 'bv-panel-head';
	head.textContent = label;
	wrap.appendChild(head);
	var list = document.createElement('div');
	list.className = 'bv-rows';
	for (var i = 0; i < items.length; i++) {
		var row = document.createElement('div');
		row.className = 'bv-row';
		var a = document.createElement('span');
		a.className = 'bv-name';
		a.textContent = items[i].a;
		a.setAttribute('title', items[i].a);
		var b = document.createElement('span');
		b.className = 'bv-model';
		b.textContent = items[i].b;
		var c = document.createElement('span');
		c.className = 'bv-tokens';
		c.textContent = items[i].c;
		row.appendChild(a);
		row.appendChild(b);
		row.appendChild(c);
		list.appendChild(row);
	}
	wrap.appendChild(list);
	return wrap;
}

function burnAbsent(label, why) {
	var wrap = document.createElement('div');
	wrap.className = 'bv-panel';
	var head = document.createElement('div');
	head.className = 'bv-panel-head';
	head.textContent = label;
	var note = document.createElement('div');
	note.className = 'bv-absent';
	note.textContent = why;
	wrap.appendChild(head);
	wrap.appendChild(note);
	return wrap;
}

/* The 24 h series at the size the Limits zone cannot give it. The scale rule is
   the Limits sparkline's own (renderSparkTarget): the marker is drawn in the
   UNITS OF THE SERIES under it, so on hourly bars the daily budget is a PACE
   line and not a ceiling, and the chart scales to the target when the target is
   above every bar — a marker off the top of a chart says nothing. */
function burnChart(hourly, budget) {
	var wrap = document.createElement('div');
	wrap.className = 'bv-chart';
	var head = document.createElement('div');
	head.className = 'bv-panel-head';

	var peak = 0, i;
	for (i = 0; i < hourly.length; i++) {
		var v = hourly[i] && Number(hourly[i].outputTokens);
		if (isFinite(v) && v > peak) peak = v;
	}
	var perDay = budget && typeof budget.dailyOutputTokens === 'number' &&
		isFinite(budget.dailyOutputTokens) && budget.dailyOutputTokens > 0 ? budget.dailyOutputTokens : null;
	var target = perDay === null ? null : perDay / HOURS_PER_DAY;
	var scaleMax = target !== null && target > peak ? target : peak;

	head.textContent = '24 h burn';
	var max = document.createElement('span');
	max.className = 'bv-chart-max';
	max.textContent = peak > 0 ? 'peak ' + fmtNum(peak) : EMDASH;
	head.appendChild(max);
	wrap.appendChild(head);

	var bars = document.createElement('div');
	bars.className = 'bv-bars';
	if (target !== null && scaleMax > 0) {
		var mark = document.createElement('div');
		mark.className = 'bv-target';
		setVar(mark, '--t', String(Math.round((target / scaleMax) * 100)));
		mark.setAttribute('title', 'budget pace ' + fmtNum(Math.round(target)) + ' per hour');
		bars.appendChild(mark);
	}
	var offset = SPARK_BUCKETS - hourly.length;
	for (i = 0; i < SPARK_BUCKETS; i++) {
		var item = i >= offset ? hourly[i - offset] : null;
		var val = item ? Number(item.outputTokens) : NaN;
		var bar = document.createElement('div');
		bar.className = 'bv-bar' + (i === SPARK_BUCKETS - 1 && hourly.length ? ' recent' : '');
		setVar(bar, '--h', String(scaleMax > 0 && isFinite(val) ? Math.round((val / scaleMax) * 100) : 0));
		bars.appendChild(bar);
	}
	wrap.appendChild(bars);

	var labels = document.createElement('div');
	labels.className = 'bv-labels';
	for (i = 0; i < SPARK_BUCKETS; i++) {
		var lab = document.createElement('span');
		var it = i >= offset ? hourly[i - offset] : null;
		/* Every third bucket, so twenty-four labels do not become a grey smear. The
		   hour is cut out of the string by hand: a bare local date-time is parsed as
		   LOCAL by the spec and as UTC by nothing, but crabd may serve either form and
		   a reading that depends on which one arrived is a reading that slides by the
		   offset. The string is what the contract states, so the string is what is
		   read. */
		lab.textContent = (it && i % 3 === 0) ? hourLabel(it.hourStart) : '';
		labels.appendChild(lab);
	}
	wrap.appendChild(labels);
	return wrap;
}

function hourLabel(hourStart) {
	var m = /T(\d{2})/.exec(String(hourStart || ''));
	return m ? m[1] : '';
}

/* ---------------------------------------------------------- the week view */

/* recap.week at full width, with the day drill INLINE rather than behind a
   sheet. The rows a tap produces are the sheet's own rows and the fetch is the
   sheet's own fetch (fetchHistory), so mock routing, the 4 s timeout, the abort
   and the today-rebase all stay in one place. What is NOT shared is the
   navigation: the sheet drill has prev/next chevrons and a Back that returns to
   the timeline, and neither has anything to return to here. */
function renderWeekView(doc) {
	var week = weekRows(doc ? doc.recap : null);
	var use24 = use24Clock();
	var sig = (use24 ? '24' : '12') + '#' + todayKey() + '#' + String(weekDay) + '#' + String(weekDayFail) +
		'#' + (week ? week.map(function (d) {
			return d.day + ':' + d.letter + ':' + d.done + ':' + d.commits;
		}).join(',') : '') +
		'#' + (weekDayDoc ? String(weekDayDoc.day) + ':' + (Array.isArray(weekDayDoc.events) ? weekDayDoc.events.length : 0) +
			':' + weekDayDoc.count + ':' + (weekDayDoc.truncated === true ? 't' : '') : '');
	if (sig === weekViewSig) return;
	weekViewSig = sig;

	var root = ui.viewWeek;
	root.textContent = '';
	root.appendChild(viewHead('Last 7 days', week ? 'tap a day to read it' : ''));
	if (!week) {
		root.appendChild(viewNote('This feed carries no weekly recap.'));
		return;
	}

	var strip = document.createElement('div');
	strip.className = 'wv-strip';
	for (var i = 0; i < week.length; i++) {
		var d = week[i];
		var col = document.createElement('div');
		col.className = 'wv-col' + (i === week.length - 1 ? ' wv-today' : '') +
			(d.day && d.day === weekDay ? ' wv-open' : '');
		/* A column with no usable day gets no affordance at all — the gate weekRows
		   already applies to the sheet's strip, kept here rather than re-derived. */
		if (d.day) {
			col.className += ' tappable';
			col.setAttribute('data-week-day', d.day);
			col.setAttribute('role', 'button');
			col.setAttribute('tabindex', '0');
			col.setAttribute('aria-label', 'open ' + d.day);
		}
		col.appendChild(wvCell('wv-letter', d.letter));
		col.appendChild(wvCell('wv-date', d.day ? d.day.slice(8) : EMDASH));
		col.appendChild(wvCell('wv-done', d.done === null ? EMDASH : String(d.done)));
		col.appendChild(wvCell('wv-k', 'done'));
		col.appendChild(wvCell('wv-commits', d.commits === null ? EMDASH : String(d.commits)));
		col.appendChild(wvCell('wv-k', 'commits'));
		strip.appendChild(col);
	}
	root.appendChild(strip);

	var pane = document.createElement('div');
	pane.className = 'wv-day';
	if (weekDayFail) {
		/* Honest failure, and NOT a latch: an older crabd 404s /v1/history and the
		   very next tap tries again, because crabd redeploys under a live widget. */
		pane.appendChild(viewNote("That day's history could not be read " + EMDASH +
			' the companion may predate 0.8.0. Tap the day again to retry.'));
	} else if (!weekDay) {
		pane.appendChild(viewNote('Tap a day above to read its history.'));
	} else if (!weekDayDoc) {
		pane.appendChild(viewNote('Reading ' + dayTitle(weekDay) + ' ' + EMDASH + ' …'));
	} else {
		pane.appendChild(weekDayRows(weekDayDoc, use24));
	}
	root.appendChild(pane);
}

function wvCell(cls, text) {
	var el = document.createElement('span');
	el.className = cls;
	el.textContent = text;
	return el;
}

function weekDayRows(doc, use24) {
	var wrap = document.createElement('div');
	wrap.className = 'wv-rows';
	var head = document.createElement('div');
	head.className = 'bv-panel-head';
	/* The contract's own pair, restated rather than reconciled: `count` is the
	   length of what crabd RETURNED and `truncated` says more exist beyond it, so
	   this line and the "+N earlier" tail are two different caps and each says so
	   in its own words. */
	var count = typeof doc.count === 'number' && isFinite(doc.count) ? doc.count : null;
	head.textContent = dayTitle(doc.day) + '   ' + EMDASH + '   ' +
		(count === null ? EMDASH : String(count)) + (count === 1 ? ' event' : ' events') +
		(doc.truncated === true ? ' (truncated)' : '');
	wrap.appendChild(head);

	var events = Array.isArray(doc.events) ? doc.events : [];
	var rows = [];
	for (var i = 0; i < events.length; i++) {
		var ev = events[i];
		if (!ev || typeof ev !== 'object') continue;
		var at = Date.parse(ev.ts);
		if (!isFinite(at)) continue;
		/* The contract's event is { ts, kind, sessionId, title }: `kind` is the text
		   column and `title` is the tag, because the title is the session's title AT
		   THE TIME and it is the only thing that makes a four-day-old row legible. */
		rows.push({ at: at, tag: shortTitle(ev.title), text: ev.kind ? String(ev.kind) : 'event' });
	}
	rows.sort(function (a, b) { return b.at - a.at; });
	var hidden = Math.max(0, rows.length - DAY_ROWS_MAX);
	rows = rows.slice(0, DAY_ROWS_MAX);

	var list = document.createElement('div');
	list.className = 'wv-list';
	if (!rows.length) {
		/* An empty day is a FACT, not a failure: crabd answers 200 with no events for
		   a day it has no history for, and the contract is explicit that the absence
		   of history is not an error. */
		list.appendChild(viewNote('No events recorded for this day.'));
	}
	for (var r = 0; r < rows.length; r++) {
		var row = document.createElement('div');
		row.className = 'tl-row';
		var time = document.createElement('span');
		time.className = 'tl-time';
		time.textContent = fmtTimeOfDay(new Date(rows[r].at), use24);
		var tag = document.createElement('span');
		tag.className = 'tl-session';
		tag.textContent = rows[r].tag;
		var text = document.createElement('span');
		text.className = 'tl-text';
		text.textContent = rows[r].text;
		row.appendChild(time);
		row.appendChild(tag);
		row.appendChild(text);
		list.appendChild(row);
	}
	if (hidden > 0) list.appendChild(viewNote('+' + hidden + ' earlier'));
	wrap.appendChild(list);
	return wrap;
}

/* The tap. One in flight at a time and a request counter, the two guards
   openDaySheet already carries: a second tap on a slow fetch would otherwise
   race two documents into one pane and the loser could land last. */
function openWeekDay(day) {
	if (!DAY_RE.test(String(day || ''))) return;
	if (weekDayBusy) return;
	weekDayBusy = true;
	weekDay = day;
	weekDayDoc = null;
	weekDayFail = null;
	weekViewSig = null;
	render();
	var req = ++weekDayReq;
	fetchHistory(day).then(function (doc) {
		weekDayBusy = false;
		if (req !== weekDayReq) return;
		if (!doc || typeof doc !== 'object') { weekDayFail = 'malformed reply'; weekViewSig = null; render(); return; }
		doc.day = day;
		weekDayDoc = doc;
		weekViewSig = null;
		render();
	}).catch(function (e) {
		weekDayBusy = false;
		if (req !== weekDayReq) return;
		weekDayFail = e && e.message ? e.message : 'fetch failed';
		logLine('history ' + day + ' unavailable (' + weekDayFail + ')');
		weekViewSig = null;
		render();
	});
}

/* -------------------------------------------------------- the detail view */

/* One session as a page. Every control on it is the sheet's own control reaching
   the sheet's own write path — onSheetDecide for Approve and Deny, onSheetContinue
   for the queued prompts — so there is exactly one implementation of each write
   in this file and the pairing code, the requestId echo, the 403/409/429 wording
   and the no-latch continue handling are inherited rather than re-typed. The two
   functions read laneCActionSessionId() instead of sheetSessionId alone, which is
   the whole of the change they needed. */

/* Which session a decide or a continue belongs to. The SHEET wins when one is
   open: it is a modal over this page, and a control the operator can actually
   see must be the one the write follows. */
function laneCActionSessionId() {
	if (sheetSessionId !== null && sheetSessionId !== undefined) return sheetSessionId;
	return currentView().key === 'detail' ? detailSessionId : null;
}

/* The session the Detail view is showing. A chosen one when it is still in the
   feed; otherwise the row the operator would have picked — waiting first, because
   that is the one thing on this panel that is about the person in front of it. */
function detailTarget() {
	var s = detailSessionId ? findSession(detailSessionId) : null;
	if (s) return s;
	var sessions = lastGoodDoc && Array.isArray(lastGoodDoc.sessions) ? lastGoodDoc.sessions : [];
	var order = ['needs_input', 'working', 'done', 'idle'];
	for (var b = 0; b < order.length; b++) {
		for (var i = 0; i < sessions.length; i++) {
			if (sessions[i] && sessions[i].state === order[b]) {
				if (detailSessionId !== String(sessions[i].id)) {
					detailSessionId = String(sessions[i].id);
					detailGen++;
				}
				return sessions[i];
			}
		}
	}
	return null;
}

function laneCOpenDetail(id) {
	if (!id) return;
	if (detailSessionId !== String(id)) detailGen++;
	detailSessionId = String(id);
	detailViewSig = null;
	/* The button lives in the sheet and the page is behind it. */
	if (sheetMode !== null) closeSheet();
	setGridView('detail');
}

function renderDetailView(sessions, quiet) {
	var s = detailTarget();
	var root = ui.viewDetail;
	if (!s) {
		if (detailViewSig === 'none') return;
		detailViewSig = 'none';
		root.textContent = '';
		root.appendChild(viewHead('Session', ''));
		root.appendChild(viewNote(sessions.length
			? 'That session has gone. Tap Sessions for the ones that are still here.'
			: 'No active Claude sessions.'));
		return;
	}

	var pend = s.state === 'needs_input' && s.pendingPermission &&
		typeof s.pendingPermission === 'object' && !Array.isArray(s.pendingPermission)
		? s.pendingPermission : null;
	var subs = subList(s);
	var events = Array.isArray(s.events) ? s.events.slice(0, SHEET_EVENTS_MAX) : [];
	var qLabel = queuedLabel(s);
	var ctxPct = ctxFillPct(s);

	/* Ages are deliberately absent from the signature, exactly as they are from the
	   card signature: they move on every poll and would rebuild the page every 3 s.
	   laneCTickDetail relabels them in place at 1 Hz. */
	/* lane D: the button set is in the signature because it is now per SESSION and
	   per config - without it, a config edit repainted the sheet's row and left this
	   page showing the old buttons until something else in the row happened to move. */
	var contSig = continueButtons(s).map(function (b) { return b.prompt; }).join('|');
	var sig = [s.id, s.state, titleParts(s).text, repoLine(s), s.model, s.speed, contSig,
		s.question || '', effectiveAcked(s) ? '1' : '',
		pend ? String(pend.tool) + '|' + String(pend.summary) : '',
		typeof s.contextTokens === 'number' ? String(s.contextTokens) : '',
		String(ctxPct), qLabel || '',
		subs.map(function (d) { return String(d && d.label); }).join(','),
		events.map(function (e) { return String(e && e.at) + String(e && e.text); }).join('|'),
		laneNSig(s),   // lane N
		quiet ? 'q' : ''].join('#');
	/* lane N: the anchors are refreshed on the short-circuit too - they are ages,
	   so they are out of the signature, and the signature is exactly the path that
	   left them stale. */
	if (sig === detailViewSig) { laneNDetailAnchors(s, pend); laneCTickDetail(Date.now()); return; }
	detailViewSig = sig;
	detailContinueSig = null;

	root.textContent = '';
	root.appendChild(detailHead(s, pend));
	if (laneEAvailable()) root.appendChild(laneEDetailStatusNode());   // lane E
	if (ctxPct !== null) root.appendChild(detailCtx(s, ctxPct));

	var body = document.createElement('div');
	body.className = 'dv-body';
	body.appendChild(detailMain(s, pend, qLabel));
	body.appendChild(detailSide(s, subs, events));
	root.appendChild(body);
	laneNDetailAnchors(s, pend);   // lane N
	laneCTickDetail(Date.now());
}

function detailHead(s, pend) {
	var head = document.createElement('div');
	head.className = 'dv-head';

	var back = document.createElement('button');
	back.type = 'button';
	back.className = 'head-chip dv-back';
	back.setAttribute('data-view-back', '1');
	back.setAttribute('aria-label', 'Back to the session cards');
	back.textContent = 'Back';
	head.appendChild(back);

	var titles = document.createElement('div');
	titles.className = 'dv-titles';
	var tp = titleParts(s);
	var title = document.createElement('div');
	title.className = 'dv-title' + (tp.derived ? ' title-derived' : '');
	title.textContent = tp.text;
	var repo = document.createElement('div');
	repo.className = 'dv-repo';
	repo.textContent = repoLine(s);
	titles.appendChild(title);
	titles.appendChild(repo);
	head.appendChild(titles);

	var chips = document.createElement('div');
	chips.className = 'dv-chips';
	var state = document.createElement('span');
	state.className = 'dv-chip dv-chip-state';
	state.setAttribute('data-state', s.state || 'idle');
	/* The card's own words, never a second vocabulary. The elapsed figure is filled
	   by the tick, so the element carries its anchor rather than a rendered age. */
	/* lane N: data-state above is untouched, so the chip keeps the working colour. */
	state.textContent = (laneNCompacting(s) ? 'compacting'
		: s.state === 'needs_input' ? 'needs input' : (s.state || 'idle')).toUpperCase() + '  ';
	var since = document.createElement('span');
	since.className = 'dv-elapsed';
	since.setAttribute('data-state-since', String(Date.parse(s.stateSince) || ''));
	since.textContent = EMDASH;
	state.appendChild(since);
	chips.appendChild(state);
	var model = shortModel(s.model);
	if (model) {
		var m = document.createElement('span');
		m.className = 'dv-chip';
		m.textContent = model;
		chips.appendChild(m);
	}
	var laneNMode = laneNModeLabel(s);   // lane N: beside the model chip, as on the card
	if (laneNMode) {
		var md = document.createElement('span');
		md.className = 'dv-chip dv-chip-mode';
		md.textContent = laneNMode;
		chips.appendChild(md);
	}
	if (s.speed === 'fast') {
		var f = document.createElement('span');
		f.className = 'dv-chip dv-chip-fast';
		f.textContent = 'FAST';
		chips.appendChild(f);
	}
	if (effectiveAcked(s)) {
		var a = document.createElement('span');
		a.className = 'dv-chip dv-chip-ack';
		a.textContent = 'ACKED';
		chips.appendChild(a);
	}
	if (pend) {
		var p = document.createElement('span');
		p.className = 'dv-chip dv-chip-pend';
		p.textContent = 'PERMISSION';
		chips.appendChild(p);
	}
	head.appendChild(chips);
	/* lane E: built only where the host says it can focus a window. Its status is NOT
	   in this row: measured at 2560 px on 2026-09-21, the head already spends
	   its width on the title, three chips and this button, and the status line ended
	   up ellipsed to "Brought t..." - a result nobody can read. It goes below. */
	if (laneEAvailable()) head.appendChild(laneEButton('head-chip dv-focus', 'Bring to front'));
	return head;
}

/* The hairline WITH its numbers, which is the one thing the card cannot give it:
   a card has room for a 3 px rule and a tooltip nobody can hover on a wall panel,
   and this page has room to print the fill, the window and the percentage. */
function detailCtx(s, pct) {
	var wrap = document.createElement('div');
	wrap.className = 'dv-ctx';
	var label = document.createElement('span');
	label.className = 'dv-ctx-k';
	label.textContent = 'context';
	var track = document.createElement('span');
	track.className = 'dv-ctx-track';
	var fill = document.createElement('span');
	fill.className = 'dv-ctx-fill';
	setVar(fill, '--w', String(pct));
	setVar(fill, '--ctx-color', ctxColor(pct));
	track.appendChild(fill);
	var num = document.createElement('span');
	num.className = 'dv-ctx-n';
	num.textContent = fmtNum(s.contextTokens) + ' of ' + fmtNum(ctxWindowTokens(s)) + '  ' + EMDASH + '  ' + pct + '%';
	wrap.appendChild(label);
	wrap.appendChild(track);
	wrap.appendChild(num);
	return wrap;
}

function detailMain(s, pend, qLabel) {
	var col = document.createElement('div');
	col.className = 'dv-col dv-main';

	/* lane N: above the body, because on a working session it is the newest fact
	   about the turn and the body below it is the question or the last event. */
	var laneNAct = laneNDetailActivity(s);
	if (laneNAct) col.appendChild(laneNAct);

	if (pend) {
		var box = document.createElement('div');
		box.className = 'dv-approval';
		var lab = document.createElement('div');
		lab.className = 'dv-approval-label';
		lab.textContent = 'permission request';
		var tool = document.createElement('div');
		tool.className = 'dv-approval-tool';
		tool.textContent = pend.tool ? String(pend.tool) : 'a tool';
		box.appendChild(lab);
		box.appendChild(tool);
		if (pend.summary) {
			var sum = document.createElement('div');
			sum.className = 'dv-approval-summary';
			sum.textContent = String(pend.summary);
			box.appendChild(sum);
		}
		/* The hold countdown, filled by the tick for the reason the sheet's copy is:
		   the poll is 3 s and this number answers whether the button under a thumb
		   still reaches anything. */
		var left = document.createElement('div');
		left.className = 'dv-approval-left';
		left.setAttribute('data-approval-at', String(Date.parse(pend.requestedAt) || ''));
		box.appendChild(left);

		var btns = document.createElement('div');
		btns.className = 'dv-approval-actions';
		/* data-decide is the SHEET's own attribute and these reach the sheet's own
		   handler: Deny first and styled as the safe default, Approve carrying the
		   tool name, because approving a shell command from a touchscreen must show
		   WHAT is being approved. */
		var deny = document.createElement('button');
		deny.type = 'button';
		deny.className = 'sheet-btn sheet-btn-deny';
		deny.setAttribute('data-decide', DECIDE_DENY);
		deny.textContent = 'Deny';
		var allow = document.createElement('button');
		allow.type = 'button';
		allow.className = 'sheet-btn sheet-btn-approve';
		allow.setAttribute('data-decide', DECIDE_ALLOW);
		allow.textContent = 'Approve ' + (pend.tool ? String(pend.tool) : 'a tool');
		btns.appendChild(deny);
		btns.appendChild(allow);
		box.appendChild(btns);
		col.appendChild(box);
	} else if (s.question) {
		var qh = document.createElement('div');
		qh.className = 'bv-panel-head';
		qh.textContent = 'question';
		var q = document.createElement('div');
		/* WHOLE, and that is the point of this page: the card clamps the question to
		   three lines and the action sheet to its own region, and this is the one
		   surface that shows all of it. The block SCROLLS rather than clamping, so
		   nothing is cut and nothing overflows the zone. */
		q.className = 'dv-question';
		q.textContent = String(s.question);
		col.appendChild(qh);
		col.appendChild(q);
	} else if (s.lastEvent) {
		var eh = document.createElement('div');
		eh.className = 'bv-panel-head';
		eh.textContent = 'latest';
		var e = document.createElement('div');
		e.className = 'dv-question';
		e.textContent = String(s.lastEvent);
		col.appendChild(eh);
		col.appendChild(e);
	}

	if (qLabel) {
		var queued = document.createElement('div');
		queued.className = 'dv-queued';
		var qt = document.createElement('span');
		qt.textContent = 'queued: ' + qLabel;
		queued.appendChild(qt);
		/* MF-002: the same control as the sheet's, carrying the same attribute, so
		   both surfaces reach one implementation of the write. */
		var cancel = document.createElement('button');
		cancel.type = 'button';
		cancel.className = 'dv-btn dv-btn-cancel';
		cancel.setAttribute('data-cancel-continue', '1');
		cancel.textContent = 'Cancel';
		queued.appendChild(cancel);
		col.appendChild(queued);
	}

	/* Tap-to-continue on a working or done session, the sheet's own rule. The
	   buttons carry the sheet's data-continue-prompt / data-continue-label, so the
	   click routes into onSheetContinue and the wire prompt is the full instruction
	   exactly as it is there. */
	if (s.state === 'working' || s.state === 'done') {
		var cont = document.createElement('div');
		cont.className = 'dv-continue';
		var ch = document.createElement('div');
		ch.className = 'bv-panel-head';
		ch.textContent = 'continue this session';
		cont.appendChild(ch);
		var row = document.createElement('div');
		row.className = 'dv-continue-btns';
		var list = continueButtons(s);   /* lane D: the sheet's own builder */
		for (var i = 0; i < list.length; i++) {
			var btn = document.createElement('button');
			btn.type = 'button';
			btn.className = 'sheet-btn dv-continue-btn';
			btn.setAttribute('data-continue-prompt', list[i].prompt);
			btn.setAttribute('data-continue-label', list[i].label);
			btn.textContent = list[i].label;
			row.appendChild(btn);
		}
		cont.appendChild(row);
		var status = document.createElement('div');
		status.className = 'dv-continue-status';
		status.id = 'dvContinueStatus';
		cont.appendChild(status);
		col.appendChild(cont);
	}
	/* lane N: last in the column, because these four annotate the session rather
	   than being the thing it is asking for. */
	var laneNRows = laneNDetailBlock(s);
	if (laneNRows) col.appendChild(laneNRows);
	return col;
}

function detailSide(s, subs, events) {
	var col = document.createElement('div');
	col.className = 'dv-col dv-side';

	var sh = document.createElement('div');
	sh.className = 'bv-panel-head';
	sh.textContent = 'subagents';
	col.appendChild(sh);
	var rows = buildSubRows(subs, SHEET_SUB_MAX);
	if (rows) col.appendChild(rows);
	else col.appendChild(viewNote('No subagents running.'));

	var eh = document.createElement('div');
	eh.className = 'bv-panel-head';
	eh.textContent = 'events';
	col.appendChild(eh);
	var list = document.createElement('div');
	list.className = 'dv-events';
	if (!events.length) {
		list.appendChild(viewNote('No events recorded since crabd started.'));
	}
	for (var i = 0; i < events.length; i++) {
		var row = document.createElement('div');
		row.className = 'event-row';
		row.setAttribute('data-at', String(Date.parse(events[i] && events[i].at) || ''));
		var age = document.createElement('span');
		age.className = 'event-age';
		age.textContent = EMDASH;
		var text = document.createElement('span');
		text.className = 'event-text';
		text.textContent = (events[i] && events[i].text) ? String(events[i].text) : 'event';
		row.appendChild(age);
		row.appendChild(text);
		list.appendChild(row);
	}
	col.appendChild(list);
	return col;
}

/* The page's ages, at 1 Hz from tick(). The approval hold is the reason it is a
   tick and not the poll: crabd holds the hook about 55 s and a countdown that
   jumped three seconds at a time would be a worse answer than no countdown. */
function laneCTickDetail(nowMs) {
	if (currentView().key !== 'detail' || !ui.viewDetail) return;
	var el = ui.viewDetail.querySelector('.dv-elapsed');
	if (el) {
		var since = Number(el.getAttribute('data-state-since'));
		setText(el, isFinite(since) && since > 0 ? fmtDur((nowMs - since) / 1000) : EMDASH);
	}
	var ap = ui.viewDetail.querySelector('.dv-approval-left');
	if (ap) {
		var at = Number(ap.getAttribute('data-approval-at'));
		var left = approvalRemaining(at, nowMs);
		setText(ap, approvalText(left));
		ap.classList.toggle('expired', left === 0);
	}
	var rows = ui.viewDetail.querySelectorAll('.event-row');
	for (var i = 0; i < rows.length; i++) {
		var eat = Number(rows[i].getAttribute('data-at'));
		setText(rows[i].querySelector('.event-age'),
			isFinite(eat) && eat > 0 ? fmtDur((nowMs - eat) / 1000) + ' ago' : EMDASH);
	}
	var subAges = ui.viewDetail.querySelectorAll('.sub-age');
	var s = detailSessionId ? findSession(detailSessionId) : null;
	var list = subList(s);
	for (var k = 0; k < subAges.length && k < list.length; k++) {
		var secs = list[k] ? Number(list[k].ageSec) : NaN;
		setText(subAges[k], isFinite(secs) ? fmtDur(secs) : EMDASH);
	}
}

/* The continue status line, mirrored out of setContinueStatus so the sheet and
   the page report one write in one place. */
function laneCMirrorContinueStatus(text, kind) {
	if (!ui.viewDetail) return;
	var el = ui.viewDetail.querySelector('.dv-continue-status');
	if (!el) return;
	setText(el, text);
	el.className = 'dv-continue-status' + (text && kind ? ' ' + kind : '');
}

/* ------------------------------------------------------- shared furniture */

function viewHead(label, note) {
	var head = document.createElement('div');
	head.className = 'gv-head';
	var h = document.createElement('h3');
	h.className = 'zone-label';
	h.textContent = label;
	head.appendChild(h);
	if (note) {
		var n = document.createElement('span');
		n.className = 'gv-note';
		n.textContent = note;
		head.appendChild(n);
	}
	return head;
}

function viewNote(text) {
	var el = document.createElement('div');
	el.className = 'gv-empty';
	el.textContent = text;
	return el;
}

/* ------------------------------------------------------------- the taps */

function onViewChipClick(ev) {
	var el = ev.target && ev.target.closest ? ev.target.closest('.view-chip') : null;
	if (!el) return;
	setGridView(el.getAttribute('data-view'));
}

/* The views' own controls. One listener per view container rather than one on
   the zone: the zone also carries the header and the card grid, both of which
   already have handlers, and a third listener over the top of them would be a
   third claim on the same taps. */
function onGridViewClick(ev) {
	var t = ev.target;
	if (!t || !t.closest) return;
	if (t.closest('[data-view-back]')) { setGridView('sessions'); return; }
	var day = t.closest('[data-week-day]');
	if (day) { openWeekDay(day.getAttribute('data-week-day')); return; }
	/* Approve / Deny and the continue buttons are the SHEET's attributes reaching
	   the SHEET's handlers — one implementation of each write, routed from a second
	   surface. Matched above nothing else, because this container has no generic
	   button branch to fall through to. */
	var decide = t.closest('[data-decide]');
	if (decide) { onSheetDecide(decide.getAttribute('data-decide')); return; }
	if (t.closest('[data-cancel-continue]')) { onCancelContinue(); return; }   /* MF-002 */
	var cont = t.closest('[data-continue-prompt]');
	if (cont) { onSheetContinue(cont.getAttribute('data-continue-prompt'), cont.getAttribute('data-continue-label') || 'Continue'); return; }
	/* lane E: the Detail head's Bring-to-front chip reaching the same one sender. */
	if (t.closest('[data-focus-session]')) { laneESendFocus(); return; }
}

/* A HORIZONTAL SWIPE ON THE HEADER ROW switches views. Deliberately not on the
   cards area: a horizontal drag on a card is already ack/dismiss, and two
   meanings for one gesture on one surface is the thing a fingertip gets wrong.

   Its own listeners on the header element rather than a branch inside the
   document-level gesture layer, because it needs nothing that layer tracks and
   the layer already does the one thing this gesture depends on: onPointerUp
   calls suppressClick() for any travel past TAP_SLOP_PX, so a swipe can never
   also open the Today timeline. The threshold here is four times that slop, so
   a committing swipe is always suppressed by the time it commits.
   A second finger abandons it outright: two fingers is the ack-all gesture and a
   view change underneath it would be a second reading of one intention. */
function onHeadPointerDown(ev) {
	if (livePointers() > 1) { headSwipe = null; return; }
	if (ev.target && ev.target.closest && ev.target.closest('.head-chip')) { headSwipe = null; return; }
	headSwipe = { id: ev.pointerId, x0: ev.clientX, y0: ev.clientY, dx: 0, dy: 0 };
}

function onHeadPointerMove(ev) {
	if (!headSwipe || headSwipe.id !== ev.pointerId) return;
	if (livePointers() > 1) { headSwipe = null; return; }
	headSwipe.dx = ev.clientX - headSwipe.x0;
	headSwipe.dy = ev.clientY - headSwipe.y0;
}

function onHeadPointerUp(ev) {
	if (!headSwipe || headSwipe.id !== ev.pointerId) return;
	var dx = headSwipe.dx, dy = headSwipe.dy;
	headSwipe = null;
	if (Math.abs(dx) < HEAD_SWIPE_PX || Math.abs(dx) <= Math.abs(dy)) return;
	/* SCA-025 — THE GESTURE IS GATED BY THE SAME THING THE CHIPS ARE. Below the
	   stylesheet's 1660 px breakpoint the switcher is hidden and the card grid comes
	   back whatever the stored view says, so a swipe there moved a view nobody could
	   see, wrote the new value to storage, and the panel came back on a different
	   page the next time it was opened wide. The stored view is meant to SURVIVE the
	   narrow slots untouched, which is what this restores. */
	if (!viewSwitcherUsable()) return;
	/* Left is forward, the direction the chips read in. It wraps, because four
	   chips in a row are a cycle and a swipe that dead-ends at Detail would be a
	   gesture that works three times out of four. */
	stepGridView(dx < 0 ? 1 : -1);
}

function onHeadPointerCancel(ev) {
	if (headSwipe && headSwipe.id === ev.pointerId) headSwipe = null;
}

/* ------------------------------------------------------------ the wiring */

function laneCViewsInit() {
	/* Dev-only, mock mode only, in memory only: the discipline &filter= and
	   &density= keep. A screenshot flag that wrote to the vendor store would leave
	   the operator's own panel on a view they never chose. Read AFTER loadPrefs so
	   the flag wins on a run that also carries &uid=. */
	if (mockName) {
		var vw = /[?&]view=([a-z]+)/i.exec(window.location.search);
		if (vw && prefIndexOrNone(VIEWS, vw[1].toLowerCase()) >= 0) viewForced = vw[1].toLowerCase();
	}
	if (viewForced) viewIdx = prefIndex(VIEWS, viewForced);
	applyGridView();
	syncViewChips();

	for (var i = 0; i < VIEWS.length; i++) {
		var chip = ui[VIEWS[i].chip];
		if (chip) chip.addEventListener('click', onViewChipClick);
	}
	var views = [ui.viewBurn, ui.viewWeek, ui.viewDetail];
	for (var v = 0; v < views.length; v++) {
		if (views[v]) views[v].addEventListener('click', onGridViewClick);
	}
	if (ui.gridHead) {
		ui.gridHead.addEventListener('pointerdown', onHeadPointerDown, { passive: true });
		ui.gridHead.addEventListener('pointermove', onHeadPointerMove, { passive: true });
		ui.gridHead.addEventListener('pointerup', onHeadPointerUp, { passive: true });
		ui.gridHead.addEventListener('pointercancel', onHeadPointerCancel, { passive: true });
	}
}

/* ======================================================================
   ---- lane C: the canvas crab ----

   Claw'd, painted to a canvas instead of held still in an SVG. Every rect below
   is transcribed from the SVG in index.html at the SAME coordinates on the SAME
   half-cell grid (viewBox 0 -4 52 44, 2 units = one half cell, 4 units = one
   cell), so the still frames are the frames that shipped; what is new is that
   the poses in between exist.

   THE SVG IS STILL THE TRUTH ABOUT STATE. This renderer reads data-mood,
   data-acc and the trick classes off #crab and the quiet/esc2 classes off body,
   and writes nothing anywhere. Nothing else in the widget knows it is here: the
   mood ladder, the wardrobe hysteresis, the dance's three gates, scheduleBlink
   and every &crab= / &mood= / &celebrate= / &blink= flag all drive the same
   attributes they always did and this follows them.

   THE SVG IS ALSO THE FALLBACK. It is hidden only once a 2D context has actually
   been obtained, so a host with no canvas renders exactly what it rendered
   before rather than an empty box.

   MOTION IS EASED IN TIME AND QUANTIZED IN SPACE, which is the pixel-art idiom
   and not a compromise: every pose lands on an integer viewBox unit, so the
   blocks stay hard-edged (the canvas equivalent of shape-rendering crispEdges),
   and the EASING decides which unit it is on at a given millisecond. A sweep
   across four units is five poses arriving on an ease-out curve, where the CSS
   keyframes had two arriving on a steps(1, end).

   THE FRAME BUDGET IS THE POINT, because this panel runs 24/7 on a desk. There
   are three scheduling states and only one of them is requestAnimationFrame:

     tricks   wave, snap, bounce, dance, juggle. All bounded (520 ms to 6 s), all
              rAF, and the loop STOPS when the last one ends.
     drip     the sweating mood, which can hold for hours. A fixed 83 ms timer,
              because an eight-step fall over 1.4 s changes pose about six times
              a second and 60 Hz would be ten wake-ups per pose.
     breath   the idle. The pose is binary and the timer is scheduled AT the next
              boundary, so it wakes about twice every 4.2 s and not at all under
              quiet or reduced motion.

   With nothing running, nothing is scheduled. Idle CPU measured both ways; see
   docs/notes/lane-c-dev.md. */

/* Timings copied from the keyframes they replace (sidecrab.css), so the canvas
   and the CSS fallback agree about how long a trick lasts. Changing one means
   changing the other AND the JS latch it is paired with (SNAP_MS and friends). */
var CRAB_WAVE_MS = 640, CRAB_WAVE_N = 3;
var CRAB_SNAP_MS = 260, CRAB_SNAP_N = 2;
var CRAB_HOP_MS = 380, CRAB_HOP_N = 2;
var CRAB_DANCE_MS = 390, CRAB_DANCE_N = 4;
var CRAB_JUGGLE_MS = 750, CRAB_JUGGLE_N = 8;
var CRAB_BREATH_MS = 4200;     /* one whole breath; the shell swells 1 unit and settles */
var CRAB_DRIP_MS = 1400;       /* a drop's fall */
var CRAB_DRIP_GAP_MS = 400;    /* and the beat before the next one forms */
var CRAB_DRIP_STEP_MS = 83;    /* ~12 Hz: an 8-unit fall changes pose ~6 times a second */
var CRAB_FRAME_RING = 120;     /* paint durations kept for window.__sidecrabCrabFrames */

/* The art. Groups in PAINT ORDER, exactly the document order of the SVG: the
   shell and its limbs, the eyes, the costume over them, the sweat, the balls. */
var CRAB_BODY = [8, 6, 36, 20];
var CRAB_CLAW_L = [0, 14, 8, 6];
var CRAB_CLAW_R = [44, 14, 8, 6];
var CRAB_LEGS = [[8, 26, 6, 8], [20, 26, 4, 8], [28, 26, 4, 8], [38, 26, 6, 8]];
var CRAB_ZZZ = [[46, 0, 6, 2], [50, 2, 2, 2], [48, 4, 2, 2], [46, 6, 6, 2]];
var CRAB_EYES_OPEN = [[16, 10, 4, 4], [32, 10, 4, 4]];
var CRAB_EYES_SLEEP = [[16, 12, 4, 2], [32, 12, 4, 2]];
var CRAB_EYES_WORRIED = [[16, 10, 2, 6], [34, 10, 2, 6]];
var CRAB_ACC = {
	sunglasses: [
		['--acc-frame', [[12, 8, 12, 8], [28, 8, 12, 8], [23, 10, 6, 2], [5, 10, 8, 2], [39, 10, 8, 2]]],
		['--acc-lens', [[13, 9, 10, 6], [29, 9, 10, 6]]],
		['--acc-glare', [[14, 10, 4, 2], [19, 12, 2, 2], [30, 10, 4, 2], [35, 12, 2, 2]]]
	],
	party: [
		['--acc-party', [[24, -2, 4, 4], [23, 2, 6, 3], [22, 5, 8, 3]]],
		['--acc-stripe', [[24, 1, 4, 1], [23, 4, 6, 1], [21, 7, 10, 1]]],
		['--acc-topper', [[24, -4, 4, 2]]]
	],
	nightcap: [
		['--acc-cap', [[11, 0, 23, 3], [9, -2, 19, 2], [4, -4, 16, 2], [3, -4, 8, 7]]],
		['--acc-band', [[9, 3, 29, 3]]],
		['--acc-pom', [[0, 1, 7, 6]]]
	]
};
/* Three drops, each a 1-unit tip over a 3x3 body with a 1-unit glint. `fall` is
   how far it may travel before it restarts, and it is CLEARANCE and not taste:
   the claws sit at y 14..20 across x 0..8 and x 44..52, and each of these three
   is in one of those columns. Drop 2 starts eight units lower than the others,
   so three is all the room it has. Measured against the claw's own top edge. */
var CRAB_SWEAT = [
	{ tip: [46, 0], box: [45, 1], fall: 8 },
	{ tip: [49, 6], box: [48, 7], fall: 3 },
	{ tip: [4, 1], box: [3, 2], fall: 8 }
];
var CRAB_SWEAT_FILL = '#8FD8E8';
var CRAB_SWEAT_GLINT = '#FFFFFF';
/* The balls' arc: out to x 1 and x 23 on the half-cell grid (a translate of
   -22 / +22 from the parked rect at x 24), and one cell above the shell at the
   apex. The reach was measured on a shot at v0.11.0: at -20 the low balls fused
   with the shell's own top corners and read as two bumps on the crab. */
var CRAB_BALL = [24, -4, 4, 4];
var CRAB_BALL_REACH = 22;
var CRAB_BALL_DROP = 6;

var crabCanvasOn = false;
var crabCtx = null;
var crabPal = null;
var crabPalAt = 0;
var crabRaf = 0;
var crabSlowTimer = null;
var crabPoseSig = null;
var crabDprW = 0, crabDprH = 0;
var crabObs = null;
var crabBodyObs = null;
var crabMotion = {};           /* class name -> the ms the motion started */
var crabFrames = [];

/* Install. Returns silently on any host that cannot give a 2D context, leaving
   the SVG to render exactly as it did. */
function laneCCrabInit() {
	var cv = ui.crabCanvas;
	if (!cv || !ui.crab || typeof cv.getContext !== 'function') return;
	try { crabCtx = cv.getContext('2d'); } catch (e) { crabCtx = null; }
	if (!crabCtx) return;
	crabCanvasOn = true;
	/* NOT the canvas element's own class name, and that is a trap with a
	   measurement behind it: the first cut called both `crab-canvas`, so the
	   element rule's `transform: translateY(1.4 vmin)` matched `body.crab-canvas`
	   as well and translated the WHOLE PANEL down 10.08 px. Every zone still
	   measured zero overflow against itself, and the page overflowed by exactly
	   that amount (2560x720, all four fixtures, all four views). A state class on
	   body and a styling class on an element must never share a name. */
	document.body.classList.add('crab-canvas-on');

	/* The attributes and classes ARE the state, so a mutation is the only event
	   this renderer needs. Two observers because the two carry different facts:
	   #crab has the mood, the costume and the trick classes; body has quiet (which
	   silences every trick) and esc2 (which lifts the waving arm a second cell). */
	if (typeof MutationObserver === 'function') {
		crabObs = new MutationObserver(onCrabMutate);
		crabObs.observe(ui.crab, { attributes: true, attributeFilter: ['data-mood', 'data-acc', 'class'] });
		crabBodyObs = new MutationObserver(onCrabMutate);
		crabBodyObs.observe(document.body, { attributes: true, attributeFilter: ['class'] });
	}
	/* The box is a flex child of a zone that reflows with the slot, so the bitmap
	   has to follow it. ResizeObserver where it exists; the window resize listener
	   init() already installs is the floor everywhere else. */
	if (typeof ResizeObserver === 'function') {
		try { new ResizeObserver(function () { crabResize(); crabPaint(true); }).observe(ui.crabWrap); }
		catch (e) { /* the window listener below is the floor */ }
	}
	window.addEventListener('resize', function () { crabResize(); crabPaint(true); });
	/* A dev reader for the frame timings, the idiom the sensor log already keeps. */
	try { window.__sidecrabCrabFrames = crabFrames; } catch (e) {}
	crabResize();
	crabSync();
}

function onCrabMutate() {
	/* The mood decides --crab-fill, so a mood change is a palette change. */
	crabPal = null;
	crabSync();
}

/* Read the CLASSES, start or stop the motions they name, repaint, reschedule.
   The class is authoritative in both directions: a trick whose latch expired has
   had its class removed by the same setTimeout that removed it before this
   renderer existed. */
function crabSync() {
	if (!crabCanvasOn) return;
	var cls = ui.crab.classList;
	var now = laneCNow();
	var names = ['waveonce', 'snap', 'bounce', 'dance', 'juggling'];
	for (var i = 0; i < names.length; i++) {
		var on = cls.contains(names[i]);
		if (on && crabMotion[names[i]] === undefined) crabMotion[names[i]] = now;
		else if (!on && crabMotion[names[i]] !== undefined) delete crabMotion[names[i]];
	}
	crabPaint(true);
}

function laneCNow() {
	try { return performance.now(); } catch (e) { return Date.now(); }
}

function crabResize() {
	if (!crabCanvasOn) return;
	var cv = ui.crabCanvas;
	var w = cv.clientWidth, h = cv.clientHeight;
	if (!(w > 0 && h > 0)) return;
	var dpr = window.devicePixelRatio || 1;
	var bw = Math.round(w * dpr), bh = Math.round(h * dpr);
	if (bw === crabDprW && bh === crabDprH) return;
	crabDprW = cv.width = bw;
	crabDprH = cv.height = bh;
	/* A bitmap resize clears the canvas and invalidates the pose cache with it. */
	crabPoseSig = null;
}

/* Every colour is read from the SVG's own computed custom properties, never
   baked in: the mood sets --crab-fill, the stylesheet sets the costume tokens
   and applyProperties writes the personalization ones onto documentElement at
   runtime, so a getComputedStyle here is what keeps a saved override winning.
   Cached for three seconds, and dropped outright on any mutation — a forced
   style read per frame would put a recalc in the animation loop. */
function crabPalette() {
	var now = Date.now();
	if (crabPal && now - crabPalAt < 3000) return crabPal;
	var cs = window.getComputedStyle(ui.crab);
	function tok(name, dflt) {
		var v = cs.getPropertyValue(name);
		v = v === null || v === undefined ? '' : String(v).trim();
		return v || dflt;
	}
	crabPal = {
		fill: tok('--crab-fill', '#E45C28'),
		eye: tok('--crab-eye', '#14120F'),
		'--acc-party': tok('--acc-party', '#F4BC45'),
		'--acc-stripe': tok('--acc-stripe', '#D4553F'),
		'--acc-topper': tok('--acc-topper', '#F7F3EC'),
		'--acc-cap': tok('--acc-cap', '#7C86A8'),
		'--acc-band': tok('--acc-band', '#F0EBE2'),
		'--acc-pom': tok('--acc-pom', '#F7F3EC'),
		'--acc-frame': tok('--acc-frame', '#0B0907'),
		'--acc-lens': tok('--acc-lens', '#1E2A33'),
		'--acc-glare': tok('--acc-glare', '#FFFFFF')
	};
	crabPalAt = now;
	return crabPal;
}

/* ---------------------------------------------------------------- the poses */

/* Where a motion is, as a fraction of ONE iteration, or null when it is not
   running. Iterations are counted rather than modulo'd forever: a class that
   outlives its own animation (a latch cleared late, a forced trick on a loop)
   must hold the last frame rather than restart, which is what the CSS does. */
function crabPhase(name, period, iterations, now) {
	var t0 = crabMotion[name];
	if (t0 === undefined) return null;
	var t = (now - t0) / period;
	if (t >= iterations) return null;
	if (t < 0) t = 0;
	return t - Math.floor(t);
}

/* ease-in-out, the curve a limb moves on. Written out rather than imported
   because a cubic-bezier evaluator would be a dependency for one line. */
function crabEase(p) {
	return p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2;
}

/* Everything the next paint needs, in viewBox UNITS and already rounded to them.
   Kept as one object so the pose can be hashed: an unchanged hash skips the
   clear and the fills entirely, which is what makes a 60 Hz loop cost about what
   a 12 Hz one does. */
function crabPose(now) {
	var crab = ui.crab;
	var mood = crab.getAttribute('data-mood') || 'content';
	var acc = crab.getAttribute('data-acc') || '';
	var cls = crab.classList;
	var body = document.body.classList;
	var quiet = body.contains('quiet');
	var esc2 = body.contains('esc2');
	var still = quiet || reducedMotion();

	var pose = {
		mood: mood,
		acc: cls.contains('juggling') ? '' : acc,
		blink: cls.contains('blink'),
		esc2: esc2,
		quiet: quiet,
		armL: 0, armR: 0, clawR: 0,
		rigX: 0, rigY: 0,
		breath: 0,
		juggle: null,
		sweat: null,
		zzz: mood === 'asleep'
	};

	/* The static mood poses, unchanged: celebrating parks both arms a cell up,
	   waving parks the right one, and tier 2 parks it a second cell higher. Quiet
	   flattens celebrating and holds waving at one cell, exactly as the stylesheet
	   does two rules apart. */
	if (mood === 'celebrating' && !quiet) { pose.armL = -4; pose.armR = -4; }
	if (mood === 'waving') pose.armR = (esc2 && !quiet) ? -8 : -4;

	if (!still) {
		/* The wave SWEEP. The CSS ran two frames on a steps(1, end); this runs the
		   same nought-to-one-cell travel on an ease, so the arm arrives instead of
		   teleporting. It REPLACES the mood's parked arm for its 1.9 s, which is what
		   an animation does to a static transform in CSS. */
		var w = crabPhase('waveonce', CRAB_WAVE_MS, CRAB_WAVE_N, now);
		if (w !== null) pose.armR = -Math.round(4 * crabEase(w < 0.5 ? w * 2 : (1 - w) * 2));

		/* The claw snap: a half cell in and back, twice. */
		var sn = crabPhase('snap', CRAB_SNAP_MS, CRAB_SNAP_N, now);
		if (sn !== null) pose.clawR = -Math.round(2 * crabEase(sn < 0.5 ? sn * 2 : (1 - sn) * 2));

		/* The hop. A sine rather than the ease, because a jump is a parabola and the
		   animal should hang at the top of it. */
		var hp = crabPhase('bounce', CRAB_HOP_MS, CRAB_HOP_N, now);
		if (hp !== null) pose.rigY = -Math.round(4 * Math.sin(Math.PI * hp));

		/* The four beats. The CSS held each of the four whole-cell positions for a
		   beat; this slides between them on the ease, so the shimmy reads as a slide
		   and a hop rather than as four stills. */
		var dn = crabPhase('dance', CRAB_DANCE_MS, CRAB_DANCE_N, now);
		if (dn !== null) {
			var beats = [[-4, 0], [0, -4], [4, 0], [0, -4], [-4, 0]];
			var total = (now - crabMotion['dance']) / CRAB_DANCE_MS;
			var beat = Math.min(CRAB_DANCE_N - 1, Math.floor(total));
			var f = crabEase(total - beat);
			pose.rigX = Math.round(beats[beat][0] + (beats[beat + 1][0] - beats[beat][0]) * f);
			pose.rigY = Math.round(beats[beat][1] + (beats[beat + 1][1] - beats[beat][1]) * f);
		}

		/* The juggle, as three arcs rather than three snapped positions. Each ball is
		   a third of a cycle behind the last, which is the same relationship the
		   animation-delays expressed. */
		var jg = crabPhase('juggling', CRAB_JUGGLE_MS, CRAB_JUGGLE_N, now);
		if (jg !== null && !quiet) {
			pose.juggle = [];
			for (var b = 0; b < 3; b++) {
				var p = (jg + b / 3) % 1;
				pose.juggle.push([
					Math.round(-CRAB_BALL_REACH + 2 * CRAB_BALL_REACH * p),
					Math.round(CRAB_BALL_DROP * (1 - Math.sin(Math.PI * p)))
				]);
			}
		}

		/* The drip. Each drop accelerates (t squared is close enough to gravity at
		   this size), falls its own clearance, then leaves a beat before the next one
		   forms — which is what makes three drops read as sweating rather than as
		   three ornaments sliding down the shell. */
		if (mood === 'sweating' && !cls.contains('juggling')) {
			pose.sweat = [];
			var cycle = CRAB_DRIP_MS + CRAB_DRIP_GAP_MS;
			for (var d = 0; d < CRAB_SWEAT.length; d++) {
				var dp = ((now / cycle) + d / CRAB_SWEAT.length) % 1;
				var ms = dp * cycle;
				pose.sweat.push(ms >= CRAB_DRIP_MS ? null
					: Math.round(CRAB_SWEAT[d].fall * Math.pow(ms / CRAB_DRIP_MS, 2)));
			}
		}

		/* The breath. The shell's TOP edge rises a unit and settles: the legs stay
		   planted and the claws stay where they are, because an animal that breathed
		   by levitating would not read as one. Binary by construction, so the
		   scheduler can wake at the boundary instead of sixty times a second. */
		pose.breath = ((now % CRAB_BREATH_MS) / CRAB_BREATH_MS) < 0.42 ? 1 : 0;
	}
	if (mood === 'sweating' && still && !cls.contains('juggling')) pose.sweat = [0, 0, 0];
	if (quiet) pose.sweat = null;   /* the belt body.quiet .crab .sweat already wears */
	return pose;
}

function crabPoseKey(p) {
	return p.mood + '|' + p.acc + '|' + (p.blink ? 'b' : '') + '|' + p.armL + ',' + p.armR + ',' +
		p.clawR + ',' + p.rigX + ',' + p.rigY + ',' + p.breath + '|' +
		(p.juggle ? p.juggle.join(';') : '') + '|' + (p.sweat ? p.sweat.join(';') : '') +
		'|' + (p.zzz ? 'z' : '') + '|' + crabDprW + 'x' + crabDprH;
}

/* ---------------------------------------------------------------- the paint */

function crabPaint(force) {
	if (!crabCanvasOn) return;
	crabResize();
	if (!(crabDprW > 0 && crabDprH > 0)) { crabSchedule(); return; }
	var now = laneCNow();
	var pose = crabPose(now);
	var key = crabPoseKey(pose);
	if (!force && key === crabPoseSig) { crabSchedule(); return; }
	crabPoseSig = key;

	var t0 = laneCNow();
	var pal = crabPalette();
	var ctx = crabCtx;
	/* preserveAspectRatio="xMidYMid meet", replicated: the SVG scales its 52x44
	   viewBox to fit and centres the remainder, so anything else here would move
	   the animal relative to the badge and the clock it was placed against. */
	var scale = Math.min(crabDprW / 52, crabDprH / 44);
	var ox = (crabDprW - 52 * scale) / 2;
	var oy = (crabDprH - 44 * scale) / 2 + 4 * scale;   /* the viewBox starts at y -4 */

	ctx.clearRect(0, 0, crabDprW, crabDprH);

	/* Whole device pixels on every edge, which is the canvas spelling of
	   shape-rendering="crispEdges": two rects that share an edge in viewBox units
	   round to the same device pixel, so the silhouette stays one solid shape
	   instead of growing seams. */
	function rect(x, y, w, h) {
		var x0 = Math.round(ox + x * scale);
		var y0 = Math.round(oy + y * scale);
		var x1 = Math.round(ox + (x + w) * scale);
		var y1 = Math.round(oy + (y + h) * scale);
		ctx.fillRect(x0, y0, x1 - x0, y1 - y0);
	}
	function rects(list, dx, dy) {
		for (var i = 0; i < list.length; i++) {
			rect(list[i][0] + (dx || 0), list[i][1] + (dy || 0), list[i][2], list[i][3]);
		}
	}

	var rx = pose.rigX, ry = pose.rigY;

	ctx.fillStyle = pal.fill;
	/* The shell, breathing: the top edge rises and the bottom stays on the legs. */
	rect(CRAB_BODY[0] + rx, CRAB_BODY[1] + ry - pose.breath, CRAB_BODY[2], CRAB_BODY[3] + pose.breath);
	rect(CRAB_CLAW_L[0] + rx, CRAB_CLAW_L[1] + ry + pose.armL, CRAB_CLAW_L[2], CRAB_CLAW_L[3]);
	rect(CRAB_CLAW_R[0] + rx + pose.clawR, CRAB_CLAW_R[1] + ry + pose.armR, CRAB_CLAW_R[2], CRAB_CLAW_R[3]);
	rects(CRAB_LEGS, rx, ry);
	if (pose.zzz) rects(CRAB_ZZZ, rx, ry);

	/* The eyes, and the blink's two directions. A blink on the sleeping crab OPENS
	   the eyes for its frame, so a tap answers whatever the mood is; the worried
	   crab is excluded outright, because its eyes are a third group and painting
	   the sleep bars beside them would put four eyes on the animal. */
	ctx.fillStyle = pal.eye;
	var eyes = CRAB_EYES_OPEN;
	if (pose.mood === 'asleep') eyes = pose.blink ? CRAB_EYES_OPEN : CRAB_EYES_SLEEP;
	else if (pose.mood === 'worried') eyes = CRAB_EYES_WORRIED;
	else if (pose.blink) eyes = CRAB_EYES_SLEEP;
	rects(eyes, rx, ry - pose.breath);

	/* The costume, over the eyes exactly as the SVG paints it, and lifted with the
	   shell it is worn on. */
	var acc = CRAB_ACC[pose.acc];
	if (acc) {
		for (var a = 0; a < acc.length; a++) {
			ctx.fillStyle = pal[acc[a][0]];
			rects(acc[a][1], rx, ry - pose.breath);
		}
	}

	if (pose.sweat) {
		for (var s = 0; s < CRAB_SWEAT.length; s++) {
			var drop = pose.sweat[s];
			if (drop === null || drop === undefined) continue;
			var d = CRAB_SWEAT[s];
			ctx.fillStyle = CRAB_SWEAT_FILL;
			rect(d.tip[0] + rx, d.tip[1] + ry + drop, 1, 1);
			rect(d.box[0] + rx, d.box[1] + ry + drop, 3, 3);
			ctx.fillStyle = CRAB_SWEAT_GLINT;
			rect(d.box[0] + rx, d.box[1] + ry + drop, 1, 1);
		}
	}

	if (pose.juggle) {
		ctx.fillStyle = pal.fill;
		for (var j = 0; j < pose.juggle.length; j++) {
			rect(CRAB_BALL[0] + rx + pose.juggle[j][0], CRAB_BALL[1] + ry + pose.juggle[j][1],
				CRAB_BALL[2], CRAB_BALL[3]);
		}
	}

	if (crabFrames.length >= CRAB_FRAME_RING) crabFrames.shift();
	crabFrames.push(Math.round((laneCNow() - t0) * 100) / 100);
	crabSchedule();
}

/* ------------------------------------------------------------ the scheduler */

/* The whole CPU story is in this function. rAF is entered only for a bounded
   trick and left the moment the last one ends; the drip gets a fixed 83 ms
   timer; the breath gets a timer aimed AT its next pose boundary; and an idle
   panel under quiet or reduced motion schedules nothing at all. */
function crabSchedule() {
	if (crabRaf) { cancelAnimationFrame(crabRaf); crabRaf = 0; }
	if (crabSlowTimer) { clearTimeout(crabSlowTimer); crabSlowTimer = null; }
	if (!crabCanvasOn) return;
	var still = document.body.classList.contains('quiet') || reducedMotion();
	if (still) return;

	var now = laneCNow();
	var tricks = ['waveonce', 'snap', 'bounce', 'dance', 'juggling'];
	var periods = [CRAB_WAVE_MS * CRAB_WAVE_N, CRAB_SNAP_MS * CRAB_SNAP_N, CRAB_HOP_MS * CRAB_HOP_N,
		CRAB_DANCE_MS * CRAB_DANCE_N, CRAB_JUGGLE_MS * CRAB_JUGGLE_N];
	for (var i = 0; i < tricks.length; i++) {
		var t0 = crabMotion[tricks[i]];
		if (t0 !== undefined && now - t0 < periods[i]) {
			crabRaf = requestAnimationFrame(function () { crabRaf = 0; crabPaint(false); });
			return;
		}
	}
	if ((ui.crab.getAttribute('data-mood') || '') === 'sweating' && !ui.crab.classList.contains('juggling')) {
		crabSlowTimer = setTimeout(function () { crabSlowTimer = null; crabPaint(false); }, CRAB_DRIP_STEP_MS);
		return;
	}
	/* The breath's next boundary, computed rather than polled: the pose flips at
	   42% and at the end of the cycle, so the wait is whichever of those is next. */
	var p = (now % CRAB_BREATH_MS) / CRAB_BREATH_MS;
	var nextAt = p < 0.42 ? 0.42 : 1;
	crabSlowTimer = setTimeout(function () { crabSlowTimer = null; crabPaint(false); },
		Math.max(16, (nextAt - p) * CRAB_BREATH_MS));
}

/* ---- lane D: continue vocabulary per repo ---- */

/* The continue buttons for ONE session, in the order they are drawn: the three
   hardcoded defaults, then the global continuePrompts the feed carries at the top
   level, then that session's own continuePrompts.

   `sessions[].continuePrompts` is v0.33.0 (provisional) and ADDITIVE: an older
   crabd sends no such key, the second pool is then empty, and the row is
   byte-for-byte the one that shipped. Absent is not [] - crabd omits the key
   entirely for a session with no project prompts rather than serving an empty
   list, so nothing here has to tell "configured with nothing" from "not
   configured".

   THE ALLOWLIST IS crabd's, NOT THIS FUNCTION'S. These strings arrive already
   filtered against the builtins, the globals and the session's own project keys,
   and queue-continue re-checks the tapped prompt server-side against the same
   per-session set. A button this function invented would 400 on the tap.

   `seen` is Object.create(null) deliberately: a plain {} inherits
   Object.prototype, so seen['constructor'] and seen['toString'] read TRUTHY and a
   prompt with either text would be dropped as a duplicate it never had. */
function continueButtons(s) {
	var list = CONTINUE_DEFAULTS.slice();
	var seen = Object.create(null);
	/* BOTH halves of each default, which is what crabd's own builtin set holds: the
	   duplicate the operator would see is a second button with the same FACE, and a
	   config prompt reading "Continue" is exactly that. */
	for (var d = 0; d < list.length; d++) { seen[list[d].prompt] = 1; seen[list[d].label] = 1; }
	var pools = [
		lastGoodDoc && Array.isArray(lastGoodDoc.continuePrompts) ? lastGoodDoc.continuePrompts : [],
		s && Array.isArray(s.continuePrompts) ? s.continuePrompts : []
	];
	for (var p = 0; p < pools.length; p++) {
		for (var i = 0; i < pools[p].length; i++) {
			var raw = pools[p][i];
			if (typeof raw !== 'string') continue;
			var txt = raw.trim();
			if (!txt || seen[txt]) continue;
			seen[txt] = 1;
			/* A config-fed prompt is one string: it is both the wire prompt and the
			   label, clamped on the button face by CSS. */
			list.push({ label: txt, prompt: txt });
		}
	}
	return list;
}


/* ======================================================================
   ---- lane N: the Claude Code activity fields, and two stale anchors ----

   Six additive, presence-gated members of `sessions[]` (contract v0.35.0), plus
   the two fixes the probe set carried in. Every renderer here returns null when
   its member is absent: an absent field is an absent element, never a zero and
   never a placeholder, which is the rule the sensors row and the ctx chip
   already keep.

   VERSION LABEL: the comments below say v0.33.0. That label is PROVISIONAL -
   widget/version.json still reads 0.32.0 and the orchestrator assigns the real
   number when the lanes merge.
   ====================================================================== */

var LANE_N_MODE_MAX = 12;      /* contract: an unknown mode is capped, not trusted */
var LANE_N_FILES_MAX = 4;      /* the Detail page's files block; the count says the rest */

/* THE TWO ANCHORS THE DETAIL PAGE WAS NOT REFRESHING (probe detail-permission-anchor).

   renderDetailView's signature is deliberately free of ages, exactly as the card
   signature is. The cards then refresh their anchors on EVERY render, outside the
   signature (see the loop at the end of renderSessions); the Detail page built its
   two anchors once, inside the rebuild, and never touched them again. So a second
   permission request in the same state, with the same tool and the same summary -
   crabd re-asking for Bash after the first request expired - moved the signature
   not at all, and the hold countdown went on counting down from the FIRST
   request's instant. Measured against the shipping file: a request at 10:01:00
   still anchored at 10:00:00, so the countdown read 5 s when 65 s were left.

   stateSince has the same shape of bug and the same cause, so both are written
   here, from the live session, on every call. */
function laneNDetailAnchors(s, pend) {
	var root = ui.viewDetail;
	if (!root || !s) return;
	var el = root.querySelector('.dv-elapsed');
	if (el) {
		var since = Date.parse(s.stateSince);
		el.setAttribute('data-state-since', isFinite(since) ? String(since) : '');
	}
	var ap = root.querySelector('.dv-approval-left');
	if (ap) {
		/* Unknown is not expired - approvalRemaining()'s own rule, and the reason
		   this writes an empty string rather than a zero. */
		var req = pend ? Date.parse(pend.requestedAt) : NaN;
		ap.setAttribute('data-approval-at', isFinite(req) ? String(req) : '');
	}
	var act = root.querySelector('.dv-activity');
	var a = s.activity;
	if (act && a && typeof a === 'object' && !Array.isArray(a)) {
		var at = Date.parse(a.at);
		act.setAttribute('data-at', isFinite(at) ? String(at) : '');
	}
}

/* A PING IS LIVENESS, NOT DELIVERY (probe sse-ping-only-gates-poll).

   sseDelivering() gates the fallback poll, and it was reading lastEventAt, which
   the `ping` listener feeds. crabd's ping is documented one screen up as carrying
   no document; a stream that pings and never sends a state frame is therefore both
   "delivering" and silent. Measured against the shipping file: 120 poll attempts
   across ten minutes of ping-only stream, 120 of them gated off, zero fetches - a
   panel that cannot repair itself, with no reason on glass for why.

   The stream is NOT torn down for this: the pings are evidence the connection is
   healthy, and reconnecting would drop a working socket. What the state deadline
   buys is the poll resuming beside it.

   TWO CONDITIONS, NOT ONE, and the second is what answers "would this fire on a
   healthy night?". The deadline alone would also fire on a night where crabd
   simply has nothing new to say, and a panel polling every 3 s beside a healthy
   stream is the cost the stream was added to remove. So the poll resumes only
   when the deadline has passed AND WHAT IS ON THE GLASS IS ALREADY STALE - the
   same STALE_MS the banner uses. A panel showing fresh data leaves the stream
   alone whatever the ping cadence; a panel showing a stale banner polls, which is
   what the poll is for. Neither number is guessed at: 45 s is the existing
   liveness deadline and 30 s is the existing staleness contract.

   Seeded at OPEN, not at the first state frame, which is the whole difference:
   `open` is when a stream owes its first document, so a connection that pings and
   never sends one starves 45 s later rather than never. Zero means no stream has
   ever opened, and a page with no stream is already polling. */
var laneNLastStateAt = 0;

function laneNNoteStateFrame() { laneNLastStateAt = Date.now(); }

function laneNStateStarved() {
	if (!laneNLastStateAt) return false;
	var now = Date.now();
	if ((now - laneNLastStateAt) <= SSE_LIVENESS_MS) return false;
	/* Never a good document at all is the worst case of stale, not an exemption. */
	if (!lastGoodAtMs) return true;
	return (now - lastGoodAtMs) > STALE_MS;
}

/* ---- mode ---------------------------------------------------------- */

var LANE_N_MODES = { plan: 'PLAN', acceptEdits: 'AUTO-EDIT', bypassPermissions: 'BYPASS' };

/* null for `normal`, for an absent member and for anything that is not a
   non-empty string. Anything else this build does not know is shown as ITSELF,
   upper-cased and capped: a newer Claude Code naming a fourth mode should put the
   word on the glass rather than be silently dropped or rendered as a guess. */
function laneNModeLabel(s) {
	var m = s && s.mode;
	if (typeof m !== 'string') return null;
	var raw = m.trim();
	if (!raw || raw === 'normal') return null;
	if (Object.prototype.hasOwnProperty.call(LANE_N_MODES, raw)) return LANE_N_MODES[raw];
	return raw.toUpperCase().slice(0, LANE_N_MODE_MAX);
}

/* ---- activity ------------------------------------------------------ */

/* The member, normalised, or null. WORKING is not tested here: the Detail page
   shows the activity in every state (it is the page for one session, and the last
   tool call is a fact about it), while the card replaces its event line only while
   the session is working - a card reading "Bash - run the tests" under a DONE chip
   would be the panel narrating a turn that has ended. */
function laneNActivity(s) {
	var a = s && s.activity;
	if (!a || typeof a !== 'object' || Array.isArray(a)) return null;
	var tool = typeof a.tool === 'string' && a.tool.trim() ? a.tool.trim() : null;
	var detail = typeof a.detail === 'string' && a.detail.trim() ? a.detail.trim() : null;
	if (!tool && !detail) return null;
	var calls = Number(a.callsThisTurn);
	return {
		tool: tool,
		detail: detail,
		at: typeof a.at === 'string' ? a.at : null,
		/* Above one, or absent. A count of 1 is what every first call reads, so
		   printing it would put a "1" on most working cards and say nothing. */
		calls: isFinite(calls) && calls > 1 ? Math.floor(calls) : null
	};
}

function laneNCardActivity(s) {
	var a = laneNActivity(s);
	if (!a || s.state !== 'working') return null;
	var row = document.createElement('div');
	row.className = 'card-event card-activity';
	if (a.tool) {
		var t = document.createElement('span');
		t.className = 'card-act-tool';
		t.textContent = a.tool;
		row.appendChild(t);
	}
	if (a.detail) {
		var d = document.createElement('span');
		d.className = 'card-act-detail';
		d.textContent = (a.tool ? '· ' : '') + a.detail;
		row.appendChild(d);
	}
	if (a.calls) {
		var c = document.createElement('span');
		c.className = 'card-act-calls';
		c.textContent = '×' + a.calls;
		c.setAttribute('title', a.calls + ' calls this turn');
		row.appendChild(c);
	}
	row.setAttribute('title', (a.tool ? a.tool + ' · ' : '') + (a.detail || ''));
	return row;
}

/* .event-row and .event-age deliberately: laneCTickDetail already relabels every
   [data-at] row on this page at 1 Hz, so the relative time costs no second tick
   and cannot drift from the events beside it. The anchor itself is refreshed by
   laneNDetailAnchors, because `at` moves without the signature moving. */
function laneNDetailActivity(s) {
	var a = laneNActivity(s);
	if (!a) return null;
	var wrap = document.createElement('div');
	wrap.className = 'dv-activity event-row';
	var at = a.at ? Date.parse(a.at) : NaN;
	wrap.setAttribute('data-at', isFinite(at) ? String(at) : '');
	var age = document.createElement('span');
	age.className = 'event-age';
	age.textContent = EMDASH;
	wrap.appendChild(age);
	var body = document.createElement('span');
	body.className = 'dv-act-body';
	if (a.tool) {
		var t = document.createElement('span');
		t.className = 'dv-act-tool';
		t.textContent = a.tool;
		body.appendChild(t);
	}
	if (a.detail) {
		/* WHOLE, the rule .dv-question keeps: this page is the surface that does not
		   clamp, and a tool call's detail is the line an operator walks over for. */
		var d = document.createElement('span');
		d.className = 'dv-act-detail';
		d.textContent = a.detail;
		body.appendChild(d);
	}
	if (a.calls) {
		var c = document.createElement('span');
		c.className = 'dv-act-calls';
		c.textContent = a.calls + ' calls this turn';
		body.appendChild(c);
	}
	wrap.appendChild(body);
	return wrap;
}

/* ---- compaction ---------------------------------------------------- */

/* WORKING ONLY, and that is the guarantee rather than a convenience: the state
   chip takes its colour from data-state, which this never touches, so a chip
   reading COMPACTING is always the working colour and can never be mistaken for
   an alert. Compaction happens inside a turn, so a session compacting in any
   other state is a feed disagreeing with itself, and the state word wins. */
function laneNCompacting(s) {
	var c = s && s.compaction;
	if (!c || typeof c !== 'object' || Array.isArray(c)) return false;
	return c.inProgress === true && s.state === 'working';
}

function laneNCompaction(s) {
	var c = s && s.compaction;
	if (!c || typeof c !== 'object' || Array.isArray(c)) return null;
	var count = Number(c.count);
	var at = typeof c.lastAt === 'string' ? Date.parse(c.lastAt) : NaN;
	var have = (isFinite(count) && count >= 0) || isFinite(at) || c.inProgress === true;
	if (!have) return null;
	return {
		count: isFinite(count) && count >= 0 ? Math.floor(count) : null,
		at: isFinite(at) ? at : null,
		inProgress: c.inProgress === true
	};
}

/* ---- todos --------------------------------------------------------- */

function laneNTodos(s) {
	var t = s && s.todos;
	if (!t || typeof t !== 'object' || Array.isArray(t)) return null;
	var done = Number(t.done), total = Number(t.total);
	/* A list of zero items is not a list. Without this the hairline would be drawn
	   empty for every session that has opened a todo tool and written nothing. */
	if (!isFinite(total) || total <= 0) return null;
	if (!isFinite(done) || done < 0) done = 0;
	if (done > total) done = total;
	return {
		done: Math.floor(done),
		total: Math.floor(total),
		pct: Math.round((done / total) * 100),
		current: typeof t.current === 'string' && t.current.trim() ? t.current.trim() : null
	};
}

/* Absolutely positioned, the .card-ctx discipline exactly: it must not join the
   card's flex column, cost a card a line, or push a badge out of its cell. It sits
   one bar-height above the ctx hairline so the two read as a stack rather than
   overprinting when a session has both. */
function laneNTodoBar(s) {
	var t = laneNTodos(s);
	if (!t) return null;
	var bar = document.createElement('div');
	bar.className = 'card-todo';
	setVar(bar, '--w', String(t.pct));
	var tip = 'todos ' + t.done + '/' + t.total + (t.current ? ' · ' + t.current : '');
	bar.setAttribute('title', tip);
	bar.setAttribute('aria-label', tip);
	return bar;
}

/* ---- the typed-ahead prompt queue ---------------------------------- */

/* TWO QUEUES, AND THE PANEL MUST NOT BLUR THEM. SideCrab's own queue is one
   prompt the operator tapped Continue on, and it is the "queued: <label>" line
   with the Cancel beside it. This is Claude Code's, counted: prompts typed at the
   terminal while a turn runs, which SideCrab neither wrote nor can cancel. So the
   Cancel stays with the line it can actually cancel, and this rides beside it as a
   count that names its own owner - on the tooltip on a card, and in full words on
   the Detail page, which has the width for them. */
function laneNPromptQueue(s) {
	var n = s && s.promptQueue;
	if (typeof n !== 'number' || !isFinite(n) || n < 1) return null;
	return Math.floor(n);
}

var LANE_N_QUEUE_TIP = 'typed ahead in Claude Code, not SideCrab’s continue queue';

function laneNQueueNote(s) {
	var n = laneNPromptQueue(s);
	if (n === null) return null;
	var el = document.createElement('span');
	el.className = 'card-typed';
	el.textContent = n + ' queued';
	el.setAttribute('title', n + ' queued — ' + LANE_N_QUEUE_TIP);
	el.setAttribute('aria-label', n + ' prompts ' + LANE_N_QUEUE_TIP);
	return el;
}

/* ---- filesTouched (the Detail page only) --------------------------- */

function laneNFiles(s) {
	var f = s && s.filesTouched;
	if (!f || typeof f !== 'object' || Array.isArray(f)) return null;
	var count = Number(f.count);
	var recent = Array.isArray(f.recent) ? f.recent.filter(function (x) {
		return typeof x === 'string' && x.trim();
	}) : [];
	if (!isFinite(count) || count < 0) {
		if (!recent.length) return null;
		count = null;
	} else count = Math.floor(count);
	if (count === 0 && !recent.length) return null;
	return { count: count, recent: recent };
}

/* The LEAF, with the whole path on the tooltip. A wall panel read from across a
   room cannot spend a line on a repository prefix that is the same on every row,
   and the path is still recoverable without one. */
function laneNLeaf(p) {
	var s = String(p).replace(/[\\/]+$/, '');
	var cut = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
	return cut >= 0 ? (s.slice(cut + 1) || s) : s;
}

function laneNFilesBlock(s) {
	var f = laneNFiles(s);
	if (!f) return null;
	var wrap = document.createElement('div');
	wrap.className = 'dv-files';
	var head = document.createElement('div');
	head.className = 'bv-panel-head';
	head.textContent = f.count === null
		? 'files touched'
		: f.count + (f.count === 1 ? ' file touched' : ' files touched');
	wrap.appendChild(head);
	var list = document.createElement('div');
	list.className = 'dv-files-list';
	for (var i = 0; i < f.recent.length && i < LANE_N_FILES_MAX; i++) {
		var row = document.createElement('div');
		row.className = 'dv-file';
		row.textContent = laneNLeaf(f.recent[i]);
		row.setAttribute('title', String(f.recent[i]));
		list.appendChild(row);
	}
	if (list.children.length) wrap.appendChild(list);
	return wrap;
}

/* ---- the Detail page's block for the four it owns ------------------ */

function laneNDetailBlock(s) {
	var rows = [];
	var t = laneNTodos(s);
	if (t) {
		var tr = document.createElement('div');
		tr.className = 'dv-todo';
		var th = document.createElement('div');
		th.className = 'bv-panel-head';
		th.textContent = 'todos ' + t.done + '/' + t.total;
		tr.appendChild(th);
		var track = document.createElement('div');
		track.className = 'dv-todo-track';
		setVar(track, '--w', String(t.pct));
		tr.appendChild(track);
		if (t.current) {
			/* NOT CLAMPED, and that is the whole point of putting it here: the card has
			   room for a hairline, and this page has room for the sentence. */
			var cur = document.createElement('div');
			cur.className = 'dv-todo-current';
			cur.textContent = t.current;
			tr.appendChild(cur);
		}
		rows.push(tr);
	}
	var n = laneNPromptQueue(s);
	if (n !== null) {
		var q = document.createElement('div');
		q.className = 'dv-typed';
		q.textContent = n + (n === 1 ? ' prompt ' : ' prompts ') + LANE_N_QUEUE_TIP;
		rows.push(q);
	}
	var c = laneNCompaction(s);
	if (c) {
		var cr = document.createElement('div');
		cr.className = 'dv-compaction';
		var parts = [];
		if (c.inProgress) parts.push('compacting now');
		if (c.count !== null) parts.push(c.count === 1 ? '1 compaction' : c.count + ' compactions');
		if (c.at !== null) parts.push('last at ' + fmtTimeOfDay(new Date(c.at), use24Clock()));
		cr.textContent = parts.join('  ·  ');
		rows.push(cr);
	}
	var files = laneNFilesBlock(s);
	if (files) rows.push(files);
	if (!rows.length) return null;
	var wrap = document.createElement('div');
	wrap.className = 'dv-lanen';
	for (var i = 0; i < rows.length; i++) wrap.appendChild(rows[i]);
	return wrap;
}

/* ---- the signature fragment ---------------------------------------- */

/* Every one of these is card STRUCTURE, not an age: each appears and DISAPPEARS
   with its member, and the second half is the half that matters - without this a
   card would go on showing a PLAN chip for a session that left plan mode, or a
   hairline for a todo list that has been cleared, until something else happened to
   rebuild it. `at` is the one exception and is deliberately absent: it moves on
   every poll and is relabelled in place. */
function laneNSig(s) {
	var a = laneNActivity(s);
	var t = laneNTodos(s);
	var c = laneNCompaction(s);
	var f = laneNFiles(s);
	return [
		laneNModeLabel(s) || '',
		a ? String(a.tool) + '|' + String(a.detail) + '|' + String(a.calls) : '',
		t ? t.done + '/' + t.total + '|' + String(t.current) : '',
		c ? (c.inProgress ? 'C' : '') + String(c.count) + '|' + String(c.at) : '',
		f ? String(f.count) + '|' + f.recent.join(',') : '',
		String(laneNPromptQueue(s))
	].join('~');
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
