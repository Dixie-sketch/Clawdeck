/* SideCrab widget runtime — consumes /v1/state (schema 1–5) from crabd on
   loopback. docs/STATE-CONTRACT.md is authoritative; this file must not invent
   fields.

   VERSIONING (rework, v0.6.1): `schema` marks the last BREAKING shape, NOT the
   feature level. Every additive field — contextTokens, fleet, recap, byModel,
   events, daily — is found by FIELD PRESENCE and renders as its absent-behaviour
   when missing, so crabd may ship new fields under the same number and this
   widget simply lights them up when it is next imported. NOTHING in this file
   may gate behaviour on a schema NUMBER comparison; a number above the ceiling
   is a real break and stays a dead feed. The lesson that bought this: crabd
   redeploys over RDP, the widget does NOT — an .icuewidget import is a
   double-click at the iCUE console, so schema N+1 bricked the glass until
   someone stood at the desk.

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
var SENSOR_REFRESH_MS = 10000; /* signal-driven; this is only the reconcile */
var SENSOR_BOOT_RETRY_MS = 120;
var SENSOR_BOOT_RETRY_MAX = 15;/* ~1.8 s grace: the plugin flag is a race, not a fact */
var SENSOR_AMBER_C = 80;
var SENSOR_RED_C = 90;
/* v0.18.0. How long every read of a sensor may FAIL before the number still on
   the glass stops being allowed to look live. Keyed on read failures and never on
   the value not changing: a temperature that sits at 47 for an hour is a normal
   idle machine, and dimming that would be the panel crying wolf at the truth. */
var SENSOR_STALE_MS = 60000;
/* Units are re-requested on this cadence rather than on every value read. The
   sensorUnitsChanged signal is the mechanism and this is the reconcile, the same
   split SENSOR_REFRESH_MS is: pairing every value with a units request doubled
   the traffic through the bridge. (It ALSO used to be what stopped a units failure
   blanking a good value — by asking less often. That was never the fix, only a
   smaller blast radius; the units leg is failure-isolated outright since v0.20.0,
   CD-12, and this constant is back to being about traffic and nothing else.) */
var SENSOR_UNITS_TTL_MS = 300000;
/* How long a FAILED units lookup is left alone (v0.20.0, CD-12). The units leg no
   longer takes the value down with it, so a bridge that cannot answer it must not
   be re-asked beside every 10 s value read either — 30 s picks the row's units up
   within a minute of the bridge recovering, and costs one extra call a minute
   while it does not. */
var SENSOR_UNITS_RETRY_MS = 30000;
/* The sensor NAME is cached exactly the way the units are (v0.21.0) and for the
   same two reasons: it changes about as often as a unit does — which is to say
   only when the operator picks a different sensor, and a selection change already
   resets the whole health record — and a name request beside every 10 s value read
   is the doubled bridge traffic SENSOR_UNITS_TTL_MS exists to avoid.
   There is no nameChanged signal in the plugin contract; sensorDataChanged is the
   nearest thing it offers and is wired to invalidate this cache. */
var SENSOR_NAME_TTL_MS = 300000;
var SENSOR_NAME_RETRY_MS = 30000;
/* Characters, and the figure is the CSS cap read back in glyphs rather than a
   round number: .sensor-n is capped at 12 vmin (86.4 px at the Edge slot), which
   is about 13 characters of --fs-meta. Clamping here first means the common long
   name ends on a word rather than mid-glyph; the CSS ellipsis is the floor under
   a name this does not catch, not a substitute for it. "CPU Package" (11) and
   "GPU Hot Spot" (12) — the two that matter — survive whole. */
var SENSOR_NAME_MAX = 13;
var SENSOR_LOG_MAX = 80;       /* the read-outcome ring buffer, window.__sidecrabSensorLog */

/* v0.4.0 */
var BLINK_MS = 150;            /* one eye-frame; long enough to read, short enough not to be a nap */
var BLINK_MIN_MS = 60000;      /* rare on purpose: an idle tic on a 24/7 panel is noise */
var BLINK_MAX_MS = 180000;
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
/* v0.16.0 — the approval-threshold TOUCH RECORD, in the same vendor object and
   for a reason nothing else in there has: `toast.approvalThresholdSec` is
   OPTIONAL on the wire and crabd PRESERVES the on-disk value when the key is
   omitted, so the widget must be able to tell "the operator moved this control"
   from "this control has never been moved and is showing its default".
   Value shape: {seen: <int seconds>, touched: <bool>}. `seen` is the last value
   this widget recorded; `touched` latches true the first time the property moves
   off it. Persisted because the distinction has to survive a panel restart — an
   in-memory-only flag would go back to silent on every reboot and the setting
   would never reach crabd again. Storage is best-effort (no uniqueId, no store):
   the degrade is back to silent, which PRESERVES whatever is on disk, so the
   failure direction is the safe one. */
var APPROVAL_PROP = 'approvalToast';
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
   Quiet hours is a SCHEDULE, written to /v1/config and owned by the iCUE property
   sheet. This is the override on top of it — be quiet an hour early, or stay awake
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
var HOST_RING_MAX = 260;       /* 200 samples of 10 min at 3 s, plus slack for a fast poll */
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
	{ key: 'glow', label: 'glow', el: 'fleetGlow' },
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

/* The approval pairing code (v0.27.0, closes SEC-a). Read LIVE off the iCUE
   property on every decide, never cached: the operator types it into the widget
   settings while a permission may already be waiting. Sent in the body, not a
   header, so the request stays the same application/json preflight crabd already
   answers. crabd normalises (case, hyphens) so this only trims. */
function panelToken() { return strProp('panelToken', '').trim(); }

/* crabd >= 0.29.0 says so in the document (`approvals.tokenRequired`); an older
   crabd has no `approvals` block and never asks for one. */
function tokenRequired() {
	var a = lastGoodDoc && lastGoodDoc.approvals;
	return !!(a && typeof a === 'object' && a.tokenRequired === true);
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
/* v0.16.0 — see APPROVAL_PROP. null seen = nothing recorded yet (the next sync
   records the current value as the baseline WITHOUT calling it a change). */
var approvalSeenSec = null;
var approvalTouched = false;
var approvalForcedSec = null;  /* dev-only &approvalsec=, mock mode only */
/* v0.17.0 — the SEED, read off /v1/state's top-level `toast` block. crabd serves
   { thresholdSec, enabled } whenever it is serving config at all, and adds
   approvalThresholdSec ONLY when the operator has set it on disk. So:
     null  = older crabd, or a crabd with nothing set     -> no seed, behave as v0.16.0
     <int> = the operator's on-disk value                 -> the effective threshold
   Presence-detected, never schema-gated. It seeds the DISPLAY only: iCUE
   properties are read-only to a widget, so the host slider cannot be moved to
   match and the panel must not pretend otherwise. It is also NOT a touch — see
   effectiveApprovalSec(). */
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
var trickLoop = null;          /* dev-only: re-fires a forced trick so it can be shot */
var forcedTrick = null;        /* dev-only &crab=juggle|bounce|snap */

var crabdVersionSeen = null;
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

/* The iCUE properties are the widget's only WRITE to configuration, and crabd
   rewrites a file for each one — so every key is gated three ways: a value worth
   sending, a debounce, and the unsupported latch. cfgSent[key] is the last
   payload sent for that key, so a property event that changes nothing sends
   nothing.

   There is deliberately NO schema gate here. /v1/config is an ENDPOINT, and the
   document cannot honestly describe it: the `quiet` KEY has been served
   null-or-object since crabd 0.2.0, but the endpoint only landed in 0.4.0 — so
   keying off `quiet` presence would POST at a 0.2.0/0.3.0 crabd and call the
   404 a surprise. Measured against companion/crabd.py: no field in the document
   is an airtight signal for this endpoint. So the widget ATTEMPTS the POST and
   reads the reply — the only honest capability test there is.

   v0.7.0 added `toast` and v0.10.0 `budget`, and all three are sent as SEPARATE
   POSTs, never one body carrying several. That is the whole point of per-key
   handling: an older crabd 400s the key it does not know, and a combined body
   would take quiet hours down with it — one unsupported key silently disabling a
   supported one. The 404 latch stays ENDPOINT-wide because a 404 is the
   endpoint's answer, not a key's. */
var cfgTimer = null;
var cfgBooted = false;
var cfgEndpointUnsupported = false;   /* set by a 404: this crabd has no /v1/config at all */
var cfgSent = { quietHours: null, toast: null, budget: null };
/* v0.16.0. `approvalThresholdSec` is an OPTIONAL member of an EXISTING key, and
   that is a shape the per-key handling above cannot express: a crabd from 0.7.0
   to 0.15.0 knows `toast` perfectly well and 400s the whole block for the one
   member it has never heard of — taking thresholdSec and enabled down with it.
   The widget updates by console import while crabd updates by redeploy, so this
   pairing is the LIKELY one, not the exotic one.
   So a 400 on a toast body that carried the optional member drops the member and
   lets the next sync send the two-member block an older crabd accepts. Same
   no-latch discipline as the key-level 400: it is cleared when crabd.version
   changes, because a redeploy is what would add support. */
var cfgApprovalUnsupported = false;

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

var sensorApi = null;
var sensorTimer = null;
var sensorBootAttempts = 0;
var sensorShown = { cpu: false, gpu: false };
/* Transition latch for the same-sensor console line. The state is re-derived on
   every reconcile, and a line written every 10 s is a line nobody reads. */
var sameWarned = false;
/* v0.18.0 per-sensor read health, the state the staleness cue is derived from.
   sensorId is carried so a SELECTION change from the settings panel resets the
   lot — a units cache and a "last good at" that belong to the sensor the operator
   just stopped watching would otherwise be applied to the one they started. */
/* `value` (v0.22.0) is the last reading that actually arrived, held so the host
   history sheet can state the temperatures in words without reading them back out
   of the DOM — the row's spans are a rendering, and a second consumer that parsed
   them would be depending on a string this file formats for the glass. */
var sensorHealth = {
	cpu: { sensorId: '', units: null, unitsAt: 0, unitsRetryAt: 0, name: null, nameAt: 0, nameRetryAt: 0, value: null, lastOkAt: 0, failsSinceOk: 0, stale: false },
	gpu: { sensorId: '', units: null, unitsAt: 0, unitsRetryAt: 0, name: null, nameAt: 0, nameRetryAt: 0, value: null, lastOkAt: 0, failsSinceOk: 0, stale: false }
};
/* The host block from /v1/state (crabd 0.22.0, v0.21.0). Held as its own state
   rather than read out of lastGoodDoc at paint time because the sensors row is
   assembled from TWO sources on different clocks — iCUE's bridge on a 10 s
   reconcile plus signals, and the feed on a 3 s poll — and one place has to own
   what the row currently contains.
   null means "no figure", and it is the only value that ever hides a segment: a
   contract-legal null must never become 0%, which on this row would read as an
   idle machine rather than as a companion that could not measure one. */
var hostMetrics = { cpuPct: null, memPct: null, memUsedGB: null, memTotalGB: null };
/* The ten-minute host ring (v0.22.0). One entry per POLL — not per render, which
   runs on taps and on the 1 Hz tick too and would sample the same document many
   times over. Entries carry a null cpu/mem when the poll landed and the figure did
   not, because "crabd answered and could not measure" is a fact worth having a slot
   for; polls that never landed leave a TIME gap instead, which hostRuns reads. */
var hostRing = [];
/* The read-outcome ring buffer. Every resolve, reject and timeout lands here with
   a timestamp, and it is mirrored to window.__sidecrabSensorLog so a debugger
   attached to the panel can read the last SENSOR_LOG_MAX outcomes without having
   been attached when they happened. The console gets the failures and the health
   transitions only; a healthy panel reads two sensors every 10 s, and logging
   every one of those is ~17,000 lines a day of "still fine". */
var sensorLog = [];
var sensorLogVerbose = false;  /* dev-only &sensorlog=1 */
var sensorForcedFail = false;  /* dev-only &sensorfail=1 */
/* Dev-only, mock mode only: &sensors=<cpu>[,<gpu>][,C|F] stands in for the iCUE
   Sensors BRIDGE (v0.17.0), the way &uid= stands in for uniqueId. window.plugins
   does not exist in any browser, so before this the sensors row was the one part
   of the Limits zone that could not be seen off-glass at all — and it is the part
   that decides whether that zone fits. It replaces the PLUGIN and nothing else:
   refreshSensors / readSensor / showSensor / markSensorZone below are the
   shipping ones, so what a screenshot catches is the row iCUE paints.
   v0.21.0 widens it to the two things the bridge now also has to answer for:
   &sensors=none  the bridge is HERE and neither property holds a sensor id —
                  the fresh-import case, and the only state the "pick sensors"
                  hint renders in. Reachable no other way off-glass, because
                  every other form of the flag manufactures ids.
   &sensornames=<cpu>|<gpu>  the names iCUE answers getSensorName with. Empty
                  segments mean a bridge that answers with nothing, which is the
                  no-label path.
   &sensorsame=1  BOTH properties resolve to one id — the operator's own defect,
                  reproduced rather than simulated: sensorIdFor returns the same
                  string for both keys, so every "are these the same sensor?"
                  test downstream is answering about real state. */
var sensorForced = null;
var sensorForcedNames = null;
var sensorForcedSame = false;
/* Dev-only, mock mode only: &mood=<content|waving|asleep|worried|celebrating>
   holds one crab mood (v0.17.0). &celebrate=1 already did this for exactly one
   mood and for exactly this reason; the other four were reachable only by picking
   a fixture that paints them, which moves every other thing on the panel too — so
   a same-pose A/B of the crab ART was not possible off-glass. Held AFTER the mood
   is derived, so nothing about the derivation changes. */
var moodForced = null;

/* ------------------------------------------------------------------ iCUE glue */

function onIcueDataUpdated() { applyProperties(); }
function onIcueInitialized() { applyProperties(); }

/* Bare assignment on purpose: a var/let/const here hides the handlers from the
   iCUE bridge, and iCUE's import validator rejects a widget that never
   references icueEvents at all. */
icueEvents = { onDataUpdated: onIcueDataUpdated, onICUEInitialized: onIcueInitialized };

function getIcueProperty(name) {
	if (typeof window !== 'undefined' && Object.prototype.hasOwnProperty.call(window, name)) {
		var value = window[name];
		if (value !== undefined && value !== null && value !== '') return value;
	}
	try {
		var v = Function('return typeof ' + name + ' !== "undefined" ? ' + name + ' : undefined')();
		if (v !== undefined && v !== null && v !== '') return v;
	} catch (e) { /* not running inside iCUE */ }
	return undefined;
}

function boolProp(name, dflt) {
	var v = getIcueProperty(name);
	if (v === undefined || v === null || v === '') return dflt;
	if (typeof v === 'string') return v !== 'false' && v !== '0';
	return !!v;
}

function strProp(name, dflt) {
	var v = getIcueProperty(name);
	if (v === undefined || v === null || v === '') return dflt;
	return String(v);
}

function hexToRgbTriple(hex, dflt) {
	var m = /^#?([0-9a-f]{6})$/i.exec(String(hex || '').trim());
	if (!m) return dflt;
	var n = parseInt(m[1], 16);
	return ((n >> 16) & 255) + ', ' + ((n >> 8) & 255) + ', ' + (n & 255);
}

function applyProperties() {
	var root = document.documentElement;
	setVar(root, '--text-color', strProp('textColor', '#EDE7DF'));
	/* Third statement of the accent default (the others: :root in sidecrab.css and
	   the accentColor property meta in index.html). This one WINS at runtime, so it
	   is the one that must not drift. AUD-F1 moved it #CC785C -> #BE7E6E. */
	setVar(root, '--accent', strProp('accentColor', '#BE7E6E'));
	setVar(root, '--bg-rgb', hexToRgbTriple(strProp('backgroundColor', '#0F0E0D'), '15, 14, 13'));

	var t = Number(getIcueProperty('transparency'));
	if (!isFinite(t)) t = 0;
	t = Math.max(0, Math.min(100, t));
	setVar(root, '--bg-alpha', String(1 - t / 100));

	/* iCUE fires onDataUpdated for ANY property, so this runs on colour changes
	   too — it is a no-op unless a config-backed property actually moved. */
	scheduleConfigSync();
	/* v0.23.0. Same shape and the same reason: the touch-diagnostics switch can
	   move under a running panel, this is the only place that hears about it, and
	   syncDiag returns immediately unless the wanted state and the installed state
	   actually disagree — so a colour change cannot tear down the capture layer. */
	syncDiag();
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

/* The 12/24-hour default lives in ONE place, and it is the MANIFEST's (v0.20.0,
   CD-32). index.html declares `clock24` with data-default="false", so an iCUE
   panel boots on the 12-hour clock — but the JS fallback said `true`, so the two
   places with no property sheet to inject a value (a dev browser and the
   standalone QA pass) booted on 24-hour and showed a different clock from every
   shipped panel. Five call sites each carried their own copy of the default,
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

function baseUrl() {
	var port = strProp('crabdPort', '2722').replace(/[^0-9]/g, '');
	if (!port) port = '2722';
	return 'http://127.0.0.1:' + port;
}

function endpointUrl() { return baseUrl() + '/v1/state'; }

function poll() {
	/* The diagnostics flush rides the poll CYCLE, not the poll itself — above the
	   in-flight guard, because whether the state fetch is stuck says nothing about
	   whether the operator's taps should reach the companion (v0.23.0). It is a
	   no-op unless diagnostics are on and there is something to ship. */
	diagFlush();
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
		render();
		return;
	}
	var gen = Date.parse(doc.generatedAt);
	if (!isFinite(gen)) { pollFailed = true; render(); return; }

	pollFailed = false;
	lastGoodDoc = doc;
	lastGoodAtMs = gen;
	/* A crabd restart under a live widget may have ADDED /v1/config, or added a
	   key to its whitelist, so a changed version string re-opens capability
	   detection for the endpoint AND for every key. Not a gate: the value is
	   never compared or ordered, only tested for having changed. */
	var ver = doc.crabd && typeof doc.crabd.version === 'string' ? doc.crabd.version : null;
	if (ver !== crabdVersionSeen) {
		crabdVersionSeen = ver;
		cfgEndpointUnsupported = false;
		cfgApprovalUnsupported = false;
		/* v0.22.0: the quiet-override latch clears with the rest. A redeploy is
		   exactly what would add the action, and the alternative is a chip that
		   stays hidden until somebody re-imports the widget at the iCUE console. */
		quietOverrideUnsupported = false;
		/* v0.23.0: and the panel-log latch. A redeploy to crabd 0.24.0 is exactly
		   what ADDS the endpoint, and the alternative is a diagnostic session that
		   captures perfectly and ships nothing until somebody re-imports the widget
		   at the iCUE console. */
		diagUnsupported = false;
		cfgSent = { quietHours: null, toast: null, budget: null };
	}
	everHadData = true;
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
	render();
	/* The iCUE properties resolve long before the first poll, so the boot-time
	   reconcile waits for a good document — not for a schema number. Whether the
	   endpoint exists is settled by the POST itself. */
	if (!cfgBooted) { cfgBooted = true; scheduleConfigSync(); }
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
	}
	prevSessionState = next;
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
	var v = getIcueProperty('crabStyle');
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

/* PERSISTENCE IS THE VENDOR'S, NOT AN INVENTION. Corsair's local-storage
   reference (skills/icue-widget-builder/references/local-storage.md) documents
   exactly one mechanism for an iCUE HTML widget: every widget has a QUuid
   exposed as the global `uniqueId`, and ONE JSON object holding all of that
   widget's persisted properties is stored in localStorage under that id. So the
   pin map is a PROPERTY INSIDE that object (PIN_PROP), never a localStorage key
   of its own — a widget that scatters bare keys across the origin is sharing a
   namespace with every other widget iCUE serves from the same file:// origin.
   The doc also says display state only: no credentials, no personal data. A map
   of session ids the operator chose to keep at the front of their own panel is
   display state and nothing more.

   Feature-detected on both halves, because both can be absent:
     - `uniqueId` does not exist in a dev browser at all (it is injected by the
       iCUE host), and referencing an undeclared identifier is a ReferenceError,
       not undefined — which is why this goes through getIcueProperty.
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
	var key = getIcueProperty('uniqueId');
	/* Dev-only, mock mode only: stand in for the host-injected uniqueId so the
	   REAL storage path (same code, same JSON object shape) can be exercised and
	   its reload behaviour photographed off-glass. Never consulted when the host
	   supplies a genuine uniqueId, and unreachable from the iCUE origin, which
	   has no query string to carry it. */
	if ((key === undefined || key === null || key === '') && mockName && devUidOverride) key = devUidOverride;
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

	/* The approval-threshold touch record (v0.16.0). Read as defensively as the pin
	   map: a shape that has drifted degrades to "never touched", which is the
	   silent-and-preserving side. */
	var at = props[APPROVAL_PROP];
	if (at && typeof at === 'object' && !Array.isArray(at)) {
		var seen = Number(at.seen);
		if (isFinite(seen)) approvalSeenSec = clampApprovalSec(seen);
		if (at.touched === true) approvalTouched = true;
	}

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
	/* Written only once there is something to record, so a panel whose operator
	   never opens the property sheet does not accumulate a key either. */
	if (approvalSeenSec !== null) props[APPROVAL_PROP] = { seen: approvalSeenSec, touched: approvalTouched };
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
		? 'Claude Code stats need the SideCrab companion ' + EMDASH +
		  " see the widget's description for setup."
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
	state.textContent = s.state === 'needs_input' ? 'needs input' : (s.state || 'idle');
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
		event.className = 'card-event';
		event.textContent = s.lastEvent || '';
	}
	var badges = document.createElement('div');
	badges.className = 'card-badges';

	var model = shortModel(s.model);
	if (model) badges.appendChild(makeBadge(model, 'badge-model'));
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
	if (qLabel) {
		var queued = document.createElement('div');
		queued.className = 'card-queued';
		queued.textContent = 'queued: ' + qLabel;
		queued.setAttribute('title', 'queued: ' + qLabel);
		bottom.appendChild(queued);
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
		var hung = !!hint && isFinite(age) && age >= HUNG_MS;
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

function scheduleClose() {
	clearSheetTimer();
	sheetCloseTimer = setTimeout(function () { sheetCloseTimer = null; closeSheet(); }, SHEET_CLOSE_MS);
}

/* Called from every render: the sheet is a view of live data, so it must follow
   the session out of needs_input and shut itself rather than sit there offering
   an ack for a question that has already been answered at the keyboard. */
function syncSheet() {
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
	var extras = lastGoodDoc && Array.isArray(lastGoodDoc.continuePrompts) ? lastGoodDoc.continuePrompts : [];
	var list = CONTINUE_DEFAULTS.slice();
	for (var e = 0; e < extras.length; e++) {
		var p = extras[e];
		if (typeof p !== 'string') continue;
		var txt = p.trim();
		if (!txt) continue;
		/* A config-fed extra is one string: it is both the wire prompt and the
		   label, clamped on the button face by CSS. */
		list.push({ label: txt, prompt: txt });
	}
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

/* The sheet's copy of the approval countdown (v0.15.0). Driven by tick(), not by
   syncSheet, for the reason in syncSheet: the poll is 3 s and this number is the
   answer to "does the button under my thumb still do anything". Zero means there
   is nothing to count — no approval sheet open, or a requestedAt that did not
   parse — and the line is then empty rather than expired. */
function tickSheetApproval(nowMs) {
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
   file: adding one would strand the whole feature until someone re-imported the
   widget at the iCUE console, which is exactly the failure the v0.6.1 schema
   rework was written to stop repeating. */
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
		sheetBusy = false;
		ui.sheet.classList.remove('busy');
		if (action === 'ack') {
			if (res.status === 204 || res.status === 200) { setSheetStatus('acknowledged', 'ok'); scheduleClose(); }
			else {
				delete ackOptimistic[id];
				setSheetStatus('could not acknowledge (HTTP ' + res.status + ')', 'err');
				render();
			}
			return;
		}
		/* 501 is the contract's "reply-injection is not proven yet" answer. It is
		   the expected state today, not a fault: muted text, sheet stays usable. */
		if (res.status === 501) { setSheetStatus('replies not available yet', 'note'); return; }
		if (res.status === 204 || res.status === 200) { setSheetStatus('sent: ' + text, 'ok'); scheduleClose(); return; }
		if (res.status === 404) { setSheetStatus('crabd no longer knows this session', 'err'); return; }
		setSheetStatus('reply failed (HTTP ' + res.status + ')', 'err');
	}).catch(function () {
		sheetBusy = false;
		ui.sheet.classList.remove('busy');
		if (action === 'ack') { delete ackOptimistic[id]; render(); }
		setSheetStatus('crabd not reachable', 'err');
	});
}

/* Tap-to-continue (v0.12.0). Optimistic confirmation on the tap: the queued item
   is what the person walked over to arrange, so it reads "queued: <label>" at
   once. A 404/400/older-crabd answer renders "not available" inline and does NOT
   latch — the next tap tries again, because crabd redeploys under a live widget.
   The wire prompt is the FULL instruction; the label is the short button face. */
function onSheetContinue(prompt, label) {
	if (!sheetSessionId || !prompt) return;
	var id = sheetSessionId;
	setContinueStatus('queued: ' + label, 'ok');
	postAction(id, 'queue-continue', prompt).then(function (res) {
		if (res.status === 204 || res.status === 200) { setContinueStatus('queued: ' + label, 'ok'); return; }
		/* 404 (no endpoint), 400 (older crabd does not know this action), or any
		   other non-2xx: not available on this crabd. No latch. */
		setContinueStatus('not available', 'note');
	}).catch(function () {
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
	if (!sheetSessionId) return;
	if (decision !== DECIDE_ALLOW && decision !== DECIDE_DENY) return;
	var id = sheetSessionId;
	/* v0.27.0: not paired = nothing goes on the wire and the sheet STAYS OPEN, so the
	   operator reads why instead of finding the card still armed after a close. */
	if (tokenRequired() && !panelToken()) {
		showNotice('not paired ' + EMDASH + ' set Approval Pairing Code in widget settings', 'err');
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
		var tok = panelToken();
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
				resolve({ status: status });
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
	if (action === 'queue-continue' || action === 'decide' || action === 'quiet') {
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
			return { status: r.status };
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

function forceRefresh() {
	/* Spinner-free on purpose. A spinner would have to keep animating until
	   something answered, which on a panel whose companion may simply be gone means
	   an animation that never stops — and the stale banner is already the widget's
	   honest account of that. This is a flash saying the poll was asked for; what
	   came back is the panel's own job to show.
	   poll() is a no-op while one is already in flight, and that is the right
	   answer rather than a missed refresh: the question a pull asks is "is this
	   current", and a poll already on the wire is the answer to it. */
	poll();
	showNotice('refreshing', 'pull');
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

function enterSheetFocus() {
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
	var contBtn = t.closest ? t.closest('[data-continue-prompt]') : null;
	if (contBtn) { onSheetContinue(contBtn.getAttribute('data-continue-prompt'), contBtn.getAttribute('data-continue-label') || 'Continue'); return; }
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
function quietState() {
	if (quietOptimistic) {
		if (lastGoodAtMs > quietOptimistic.at) quietOptimistic = null;
		else return quietOptimistic;
	}
	var fed = quietOverrideFromFeed();
	if (!fed) return { mode: 'auto', until: null };
	if (fed.until !== null && fed.until <= Date.now()) return { mode: 'auto', until: null };
	return fed;
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
	if (ui.moonChip.getAttribute('data-quiet') !== st.mode) ui.moonChip.setAttribute('data-quiet', st.mode);
	setText(ui.moonMode, quietModeWord(st.mode));
	/* The instant is parked on the element and relabelled by the 1 Hz tick, the
	   idiom the gauge countdowns and the card ages already use: the poll is 3 s and
	   a remaining time that only moved on a poll would cross its minute boundary up
	   to three seconds late. REMOVED whenever there is nothing to count, so the tick
	   cannot write a figure computed from a stale number. */
	if (st.mode !== 'auto' && st.until !== null) {
		var key = String(st.until);
		if (ui.moonChip.getAttribute('data-until') !== key) ui.moonChip.setAttribute('data-until', key);
	} else if (ui.moonChip.hasAttribute('data-until')) {
		ui.moonChip.removeAttribute('data-until');
	}
	paintMoonLeft(Date.now());
	var label = st.mode === 'auto'
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

/* null means "the properties are not in a state worth sending" — an invalid or
   half-typed time. That is deliberately NOT the same as {quietHours: null},
   which means "the switch is off, clear quiet hours". */
function desiredQuietConfig() {
	if (!boolProp('quietEnabled', false)) return { quietHours: null };
	var start = normHm(strProp('quietStart', '22:00'));
	var end = normHm(strProp('quietEnd', '07:00'));
	if (!start || !end) return null;
	return { quietHours: { start: start, end: end } };
}

/* Both toast members are required by the contract, so both are always sent. The
   threshold is clamped to the slider's own range rather than passed through: a
   property that somehow arrives outside it is a value to correct, not a body to
   have crabd reject — and a 400 there would be indistinguishable from the
   "older crabd, no toast key" 400 this version has to read. */
function desiredToastConfig() {
	var n = Number(getIcueProperty('toastThreshold'));
	if (!isFinite(n)) n = TOAST_SEC_DEFAULT;
	n = Math.round(Math.max(TOAST_SEC_MIN, Math.min(TOAST_SEC_MAX, n)));
	var block = { thresholdSec: n, enabled: boolProp('toastEnabled', true) };
	/* The THIRD member is optional and is sent only once the operator has actually
	   moved its control (v0.16.0). Recorded first, so this call is also what
	   establishes the baseline on a panel that has never seen the property. */
	var approval = approvalPropertySec();
	noteApprovalThreshold(approval);
	if (approvalTouched && !cfgApprovalUnsupported) block.approvalThresholdSec = approval;
	return { toast: block };
}

function clampApprovalSec(n) {
	return Math.round(Math.max(APPROVAL_SEC_MIN, Math.min(APPROVAL_SEC_MAX, n)));
}

/* The property's current value, clamped to the contract bounds. Absent (a dev
   browser, or an iCUE that has not injected it yet) reads as the shipped default
   so the control and the notifier agree on what "never set" means: 20 s. */
function approvalPropertySec() {
	if (approvalForcedSec !== null) return clampApprovalSec(approvalForcedSec);
	var n = Number(getIcueProperty('approvalThreshold'));
	if (!isFinite(n)) n = APPROVAL_SEC_DEFAULT;
	return clampApprovalSec(n);
}

/* THE SEQUENCING RULE, and the reason this is not just another clamp-and-send:
   crabd PRESERVES `toast.approvalThresholdSec` when a write omits it, precisely
   so a panel save cannot delete a value the operator hand-edited into
   config.json. A widget that sent its default on every save would defeat that on
   the first colour change — the key would be materialised at 20 s and the
   hand-edited value gone, with nothing said.
   So the first observation is a BASELINE, never a change: it records what the
   property reads and sends nothing. Only a value that later differs from that
   baseline is the operator having moved the control, and from then on the key
   rides every toast write (the latch is deliberate — setting it back to 20 is
   still a statement, and it has to be able to reach crabd). */
function noteApprovalThreshold(sec) {
	if (approvalSeenSec === null) { approvalSeenSec = sec; savePrefs(); return; }
	if (sec === approvalSeenSec) return;
	approvalSeenSec = sec;
	approvalTouched = true;
	savePrefs();
	/* Repaint the sheet line HERE, because this is the only place the latch is
	   ever set and it does not run on the render path: the config sync has its own
	   cadence, so an open approval sheet otherwise kept saying "45 s (saved)" for
	   up to a poll after the operator had moved the slider to 90 — measured, not
	   theorised. Safe to call with the sheet shut; it only writes text. */
	renderApprovalThreshold();
}

/* v0.17.0. Reads the feed's optional seed and NOTHING else — it does not write
   approvalSeenSec, does not set approvalTouched, and does not save prefs.
   That separation is the whole point: approvalTouched means "the operator moved
   the iCUE slider", and a value arriving from crabd is the operator having edited
   config.json instead. Letting the seed set the latch would put approvalThresholdSec
   into every subsequent toast write, which is exactly the materialise-an-unset-key
   failure v0.16.0 exists to prevent — crabd would then start receiving the
   PROPERTY's value (20 s by default) as if it had been chosen, overwriting the
   on-disk figure this seed was read from.
   Presence-detected on the member, not on the block: an older crabd sends no
   `toast` at all, and a current one sends the block WITHOUT this member until the
   operator sets it. Both must land on null. */
function noteApprovalSeed(toast) {
	var have = toast && typeof toast === 'object' && !Array.isArray(toast);
	var n = have ? Number(toast.approvalThresholdSec) : NaN;
	/* Object.prototype.hasOwnProperty rather than a truthiness test: 0 is not a
	   legal value here, but a null the contract does allow must read as absent
	   rather than as Number(null) === 0. */
	var present = have &&
		Object.prototype.hasOwnProperty.call(toast, 'approvalThresholdSec') &&
		isFinite(n);
	approvalFeedSec = present ? clampApprovalSec(n) : null;
}

/* What the panel should SAY the approval threshold is. The property wins the
   moment the operator has moved it, because from then on the panel's own control
   is the operator's latest word and it is what crabd is being sent. Until then a
   seed from the feed is a better answer than the property's untouched default,
   which is a value nobody chose. */
function effectiveApprovalSec() {
	if (!approvalTouched && approvalFeedSec !== null) return approvalFeedSec;
	return approvalPropertySec();
}

/* Presence-gated on the SEED, not on the effective value: with no `toast` block
   in the feed there is nothing the panel knows that the settings sheet does not
   already show, and a line restating the slider back at the operator is noise.
   Once a seed exists the line always renders, because "the slider is the one in
   force now" is the other half of the same fact and going silent the moment the
   operator touches the control would read as the setting having gone away. */
function renderApprovalThreshold() {
	if (!ui.sheetApprovalThreshold) return;
	if (approvalFeedSec === null) {
		setText(ui.sheetApprovalThreshold, '');
		ui.sheetApprovalThreshold.classList.remove('shown');
		return;
	}
	setText(ui.sheetApprovalThreshold, 'toast after ' + fmtApprovalSec(effectiveApprovalSec()) +
		(approvalTouched ? ' (panel)' : ' (saved)'));
	ui.sheetApprovalThreshold.classList.add('shown');
}

/* Minutes ONLY for a whole number of them; everything else stays in seconds.
   The slider steps by 5 up to 300, so 90 and 135 are ordinary settings — and
   rounding those to minutes printed "2 min" for a 90 s threshold, which is not a
   rounding, it is a wrong number on a settings line. 300 reads "5 min", 3600
   (the contract ceiling, reachable only by hand-editing config.json) reads
   "60 min", and 90 reads "90 s". */
function fmtApprovalSec(sec) {
	return sec >= 60 && sec % 60 === 0 ? (sec / 60) + ' min' : sec + ' s';
}

/* The budget is ONE member by contract, and the switch-off case is
   {budget: null} — "clear it", which is a different statement from "the
   properties are not worth sending" (that is the null this function never
   returns, because a slider cannot be half-typed the way a time field can).
   The slider is in thousands; the multiplication back to tokens happens here and
   nowhere else. */
function desiredBudgetConfig() {
	if (!boolProp('budgetEnabled', false)) return { budget: null };
	var k = Number(getIcueProperty('budgetTokens'));
	if (!isFinite(k)) k = BUDGET_K_DEFAULT;
	k = Math.round(Math.max(BUDGET_K_MIN, Math.min(BUDGET_K_MAX, k)));
	return { budget: { dailyOutputTokens: k * 1000 } };
}

function scheduleConfigSync() {
	if (cfgTimer) clearTimeout(cfgTimer);
	cfgTimer = setTimeout(function () { cfgTimer = null; syncConfig(); }, CFG_DEBOUNCE_MS);
}

/* One debounce, one POST per key. keep400 says what a 400 MEANS for that key —
   see syncConfigKey. */
function syncConfig() {
	syncConfigKey('quietHours', desiredQuietConfig(), false);
	syncConfigKey('toast', desiredToastConfig(), true);
	/* keep400, same as toast and for the same reason: a pre-0.10.0 crabd answers
	   400 to a budget write because it does not know the key. Not latched — see
	   syncConfigKey. */
	syncConfigKey('budget', desiredBudgetConfig(), true);
}

function syncConfigKey(key, want, keep400) {
	/* Attempt-and-handle IS the capability test (see cfgEndpointUnsupported
	   above). Once a 404 has proven this crabd has no /v1/config at all, stop:
	   the latch is what keeps a pre-0.4.0 crabd from being re-POSTed on every
	   property nudge. */
	if (cfgEndpointUnsupported) return;
	if (!want) return;
	var payload = JSON.stringify(want);
	if (payload === cfgSent[key]) return;
	/* Claimed before the request, so a second property event mid-flight does not
	   send the same body twice; cleared on failure so the next change retries. */
	cfgSent[key] = payload;
	postConfig(payload).then(function (res) {
		if (res.status === 204 || res.status === 200) return;
		/* 404 = this crabd predates the endpoint. That is UNSUPPORTED, not an
		   error, and it is permanent for this crabd — latch it so the widget
		   stops asking (cleared only when crabd.version changes, i.e. a
		   redeploy that may have added it). */
		if (res.status === 404) {
			cfgEndpointUnsupported = true;
			cfgSent[key] = null;
			logLine(key + ' config unsupported by this crabd (HTTP 404)');
			return;
		}
		/* 400 on a WHITELIST key means this crabd does not know the key — the
		   contract says a pre-0.7.0 crabd answers exactly that to a toast write.
		   It is NOT latched: 400 is also what a bad body gets, and a latch would
		   strand the key until the widget was re-imported at the console over
		   what may have been one malformed value. Keeping the payload marker is
		   the whole brake: the same body is never re-sent, a changed value still
		   gets a try, and a crabd redeploy clears the marker outright.
		   Keys whose 400 can only be a bad body (quietHours, which this widget
		   validates before sending) clear the marker instead and retry. */
		if (res.status === 400 && keep400) {
			/* One rung BELOW the key: a toast body that carried the optional
			   approvalThresholdSec may have been refused for that member alone, and
			   an older crabd's 400 takes the two required members down with it. Drop
			   the member and clear the payload marker so the next sync actually
			   retries — without the clear the same body is the only one that would
			   ever be built, and the whole toast key would stay dead. Tried once per
			   crabd version; a body still refused without it falls through to the
			   key-level reading below on the retry. */
			if (key === 'toast' && !cfgApprovalUnsupported &&
				payload.indexOf('approvalThresholdSec') !== -1) {
				cfgApprovalUnsupported = true;
				cfgSent[key] = null;
				logLine('toast approvalThresholdSec not supported by this crabd (HTTP 400) ' +
					EMDASH + ' retrying without it');
				scheduleConfigSync();
				return;
			}
			logLine(key + ' config not supported by this crabd (HTTP 400) ' + EMDASH +
				' this key only, no latch');
			return;
		}
		cfgSent[key] = null;
		/* Silent on glass by design, at every status: this is a settings write the
		   user made in iCUE, and the panel is not the place to render a config
		   error. Nothing here may touch pollFailed — an absent config endpoint is
		   not a dead feed. */
		logLine(key + ' config rejected (HTTP ' + res.status + ')');
	}).catch(function () {
		/* Transient by assumption — crabd restarts on every deploy. NOT latched:
		   treating a blip as "unsupported forever" would silently strand the
		   setting until the widget was re-imported at the console. */
		cfgSent[key] = null;
		logLine(key + ' config failed: crabd not reachable');
	});
}

/* ------------------------------------------------- hardware sensors (v0.3.0) */

/* iCUE's Sensors data provider, declared in manifest.json as
   "widgetbuilder.sensorsdataprovider:Sensors:1.0". The wrapper classes are
   inlined in index.html per the Corsair common-tools reference.

   The whole row is display:none until a real reading arrives. That is the
   degrade path, not a nicety: in a dev browser, in mock mode, and on any
   machine iCUE reports no temperature sensor for, window.plugins does not exist
   at all — and a row that rendered "NaN °C" or "undefined" on glass would be
   worse than no row. */
function sensorsPlugin() {
	if (typeof window === 'undefined' || !window.plugins) return null;
	return window.plugins.Sensorsdataprovider || null;
}

function onSensorsdataproviderInitialized() { initSensors(); }

/* Bare assignment, same reason as icueEvents: a var/let/const here hides the
   handler from the iCUE bridge. */
pluginSensorsdataproviderEvents = { onInitialized: onSensorsdataproviderInitialized };

function initSensors() {
	if (sensorApi) { refreshSensors(); return; }
	var plugin = sensorsPlugin();
	if (!plugin || typeof SimpleSensorApiWrapper === 'undefined') return;

	sensorApi = new SimpleSensorApiWrapper(plugin);
	noteSensorRead('bridge', 'init', 'SimpleSensorApiWrapper bound', 0);
	if (plugin.sensorValueChanged && plugin.sensorValueChanged.connect) {
		plugin.sensorValueChanged.connect(function (id, value) {
			var key = sensorKeyForId(id);
			if (!key) return;
			/* THE SIGNAL CARRIES THE NEW VALUE (sensors-data-provider.md:
			   sensorValueChanged(sensorId, value)) and until v0.18.0 this handler
			   threw it away and went back through the request path for a number it
			   had already been handed. That mattered: the request path is the thing
			   that can freeze, and this is a live reading that owes it nothing. Take
			   it when it parses; fall through to a request when it does not, so a
			   provider that emits the signal bare still refreshes. */
			if (applyPushedSensorValue(key, value)) return;
			refreshSensors();
		});
	}
	/* Units change when the operator flips iCUE between C and F. Cheap to honour,
	   and the TTL below is only the reconcile for a provider that does not emit. */
	if (plugin.sensorUnitsChanged && plugin.sensorUnitsChanged.connect) {
		plugin.sensorUnitsChanged.connect(function (id) {
			var key = sensorKeyForId(id);
			/* unitsRetryAt is cleared with the value (v0.20.0, CD-12): this signal is
			   the bridge saying the units HAVE CHANGED, which outranks a backoff set
			   by an earlier failure — the row would otherwise keep showing °C for up
			   to SENSOR_UNITS_RETRY_MS after the operator flipped iCUE to °F. */
			if (key) { sensorHealth[key].units = null; sensorHealth[key].unitsRetryAt = 0; refreshSensors(); }
		});
	}
	/* There is no nameChanged signal in the contract (sensors-data-provider.md
	   lists sensorAdded / sensorRemoved / sensorDataChanged / sensorValueChanged /
	   sensorUnitsChanged and nothing else), so sensorDataChanged is what a name
	   cache has to key on. It is the right shape for it — a sensor whose DATA
	   changed is exactly the one whose label may have — and the TTL still catches a
	   provider that never emits it. A label is the one thing on this row that must
	   not be allowed to go quietly wrong: it is the evidence the operator would use
	   to decide the number beside it belongs to the wrong sensor. */
	if (plugin.sensorDataChanged && plugin.sensorDataChanged.connect) {
		plugin.sensorDataChanged.connect(function (id) {
			var key = sensorKeyForId(id);
			if (key) { sensorHealth[key].name = null; sensorHealth[key].nameRetryAt = 0; refreshSensors(); }
		});
	}
	/* The signal does the work; this only reconciles a sensor that stopped
	   emitting and picks up a changed selection from the settings panel. */
	if (!sensorTimer) sensorTimer = setInterval(refreshSensors, SENSOR_REFRESH_MS);
	refreshSensors();
}

/* ---- the read-outcome log (v0.18.0) ----------------------------------------

   The instrumentation exists because the defect this release fixes is one this
   panel could not have reported: reads were failing and the row went on showing
   the number the first read had painted, so "frozen" and "healthy but idle"
   looked identical from the far side of a desk. Every outcome is recorded; the
   console is told about the ones that mean something. */
function noteSensorRead(key, outcome, detail, ms) {
	var rec = {
		t: new Date().toISOString(), key: key, outcome: outcome,
		detail: detail === undefined || detail === null ? '' : String(detail),
		ms: ms
	};
	sensorLog.push(rec);
	if (sensorLog.length > SENSOR_LOG_MAX) sensorLog.shift();
	try { window.__sidecrabSensorLog = sensorLog; } catch (e) {}
	if (sensorLogVerbose || outcome !== 'ok') {
		logLine('sensor ' + key + ' ' + outcome + (rec.detail ? ' ' + rec.detail : '') +
			(typeof ms === 'number' ? ' (' + ms + 'ms)' : ''));
	}
}

/* ---- key / id / element plumbing -------------------------------------------

   One place that maps a sensor key to its id and its value element, because three
   callers need it: the 10 s reconcile, the value signal, and the staleness
   watchdog on the 1 Hz tick. Everything else about a cell now goes through
   syncSensorRow, which owns the row. */
var SENSOR_KEYS = ['cpu', 'gpu'];

function sensorIdFor(key) {
	/* A dev browser has no property sheet, so the flag has to stand in for the
	   selected sensor IDs as well as for the plugin that would answer them. */
	if (sensorForced) {
		/* &sensors=none — the bridge is here and nothing is selected. Returning ''
		   is not a special case: it is the same empty string strProp gives for an
		   unset property, so the fresh-import path below runs unmodified. */
		if (sensorForced.none) return '';
		return sensorForcedSame ? 'dev:shared' : 'dev:' + key;
	}
	return strProp(key === 'gpu' ? 'gpuTempSensor' : 'cpuTempSensor', '');
}

/* THE DEFECT THIS RELEASE MAKES VISIBLE (v0.21.0). Measured out of iCUE's own
   property storage on the operator's machine: cpuTempSensor and gpuTempSensor
   both held `1ce3d9bb-…`, one sensor — almost certainly a hub or ambient probe —
   feeding both cells. Every symptom of the v0.18.0 wrapper race is reproduced by
   that selection alone (two numbers that never move, and never disagree), and
   the panel had no way to tell the two apart or to say which it was looking at.
   Both ends must be non-empty: two UNSET properties are not "the same sensor",
   they are the fresh import the hint below is for. */
function sensorsSameId() {
	var a = sensorIdFor('cpu'), b = sensorIdFor('gpu');
	return !!a && !!b && a === b;
}

/* Neither property is set AND a bridge exists to have offered a choice. The
   second half is what keeps this off every plain browser and out of the
   standalone screenshots: no plugin means no settings panel to send anyone to. */
function sensorsUnset() {
	return !!sensorApi && !sensorIdFor('cpu') && !sensorIdFor('gpu');
}

/* iCUE hands back whatever the provider calls the sensor, which ranges from
   "CPU Package" to a device-qualified path. The cell budget is one line shared
   with a temperature, a host figure and (in the CPU cell) a second one, so the
   label has to be the part that identifies the sensor and nothing else.
   Last segment first — a path names the device on the left and the sensor on the
   right, and the device is the half already implied by which cell this is. Then
   the trailing "Temperature"/"Temp", which the degree sign beside it has said
   already. The clamp is the last resort, and CSS ellipsis is the floor under it. */
function shortSensorName(raw) {
	if (raw === undefined || raw === null) return '';
	var s = String(raw).replace(/\s+/g, ' ').trim();
	if (!s) return '';
	var parts = s.split(/\s+[-–—>]\s+|\s*[|\/\\]\s*|:\s+/);
	var last = parts[parts.length - 1].trim();
	if (last) s = last;
	s = s.replace(/\s*\b(temperatures?|temps?)\b\s*$/i, '').trim();
	if (!s) return '';
	if (s.length > SENSOR_NAME_MAX) s = s.slice(0, SENSOR_NAME_MAX - 1).trim() + '…';
	return s;
}

function sensorKeyForId(id) {
	if (id === undefined || id === null || id === '') return '';
	var s = String(id);
	for (var i = 0; i < SENSOR_KEYS.length; i++) {
		if (s === sensorIdFor(SENSOR_KEYS[i])) return SENSOR_KEYS[i];
	}
	return '';
}

function sensorValueEl(key) { return key === 'gpu' ? ui.sensorGpuVal : ui.sensorCpuVal; }

function refreshSensors() {
	if (!sensorApi) return;
	for (var i = 0; i < SENSOR_KEYS.length; i++) readSensor(SENSOR_KEYS[i]);
}

function readSensor(key) {
	var valueEl = sensorValueEl(key);
	var sensorId = sensorIdFor(key);
	var h = sensorHealth[key];
	if (!sensorId) { resetSensorHealth(key); hideSensor(key); return; }
	/* BOTH PROPERTIES POINT AT ONE SENSOR (v0.21.0). The GPU cell is not read at
	   all in that state — not to save the call, but because there is no second
	   reading to take: the same request would come back with the same number, and
	   printing it twice is the misinformation this release exists to stop. The cell
	   renders its reason instead (syncSensorRow). CPU keeps the reading, because
	   one of the two properties is presumably the one the operator meant. */
	if (key === 'gpu' && sensorsSameId()) {
		resetSensorHealth(key);
		setText(valueEl, '');
		hideSensor(key);
		return;
	}
	if (h.sensorId !== sensorId) { resetSensorHealth(key); h.sensorId = sensorId; }

	var started = Date.now();
	/* THE UNITS LEG CANNOT FAIL THE READ (v0.20.0, CD-12). Both calls used to go
	   into one Promise.all, which rejects if EITHER rejects — so a units lookup
	   that failed threw away a temperature that had come back perfectly well.
	   Measured both ways: on the first read the row stayed hidden with a good 71 in
	   hand, and on a TTL refresh a fresh 88 was discarded and the last value left to
	   dim to stale. A unit string is an annotation on the number; the number is the
	   reading, and a reading is never dropped because its label is missing.
	   So the units leg resolves to an OUTCOME rather than rejecting, and the
	   Promise.all below now rejects only when the value itself does — which is the
	   one failure that really is one. */
	var needUnits = (h.units === null || (started - h.unitsAt) >= SENSOR_UNITS_TTL_MS) &&
		started >= h.unitsRetryAt;
	var unitsLeg = needUnits
		? sensorApi.getSensorUnits(sensorId).then(
			function (u) { return { ok: true, units: u }; },
			function () { return { ok: false, units: null }; })
		: Promise.resolve({ ok: false, units: h.units });
	/* THE NAME LEG IS FAILURE-ISOLATED THE SAME WAY (v0.21.0), and it is written
	   next to the units leg deliberately so the pair cannot drift apart. CD-12 is
	   the whole argument: a label that could not be fetched must never take down
	   the number it labels. It also carries the same backoff, so a bridge with no
	   getSensorName cannot put a failing request beside every value read. */
	var needName = (h.name === null || (started - h.nameAt) >= SENSOR_NAME_TTL_MS) &&
		started >= h.nameRetryAt && typeof sensorApi.getSensorName === 'function';
	var nameLeg = needName
		? sensorApi.getSensorName(sensorId).then(
			function (n) { return { ok: true, name: n }; },
			function () { return { ok: false, name: null }; })
		: Promise.resolve({ ok: false, name: h.name });
	var want = Promise.all([sensorApi.getSensorValue(sensorId), unitsLeg, nameLeg]);

	want.then(function (res) {
		var ms = Date.now() - started;
		var num = parseFloat(res[0]);
		if (!isFinite(num)) {
			/* A read that RESOLVED and had nothing readable in it is a positive
			   statement that this sensor has nothing to report — absence, not a
			   comms failure — so the row goes, exactly as it always has. Only a
			   REJECT gets the keep-then-dim treatment below. */
			noteSensorRead(key, 'empty', res[0], ms);
			resetSensorHealth(key); h.sensorId = sensorId;
			setSensorStale(key, false);
			hideSensor(key);
			return;
		}
		if (res[1].ok) {
			h.units = res[1].units === undefined || res[1].units === null ? '' : String(res[1].units);
			h.unitsAt = Date.now();
			h.unitsRetryAt = 0;
		} else if (needUnits) {
			/* The units call was ASKED and did not answer. h.units is left where it
			   was — the last good string, or null on a sensor that has never reported
			   one — and renderSensorValue draws a bare degree sign for null, which is
			   the same rendering a bridge that answers with no units already gets.
			   The retry is held off for SENSOR_UNITS_RETRY_MS so a permanently broken
			   units call cannot put a second request beside every 10 s value read,
			   which is the doubled traffic SENSOR_UNITS_TTL_MS exists to avoid. */
			h.unitsRetryAt = Date.now() + SENSOR_UNITS_RETRY_MS;
			noteSensorRead(key, 'units', 'units unavailable, value kept', ms);
		}
		if (res[2].ok) {
			/* '' is an ANSWER — a bridge saying this sensor has no name — and it is
			   cached as one, so the row stops asking. Only a rejection retries. */
			h.name = res[2].name === undefined || res[2].name === null ? '' : String(res[2].name);
			h.nameAt = Date.now();
			h.nameRetryAt = 0;
		} else if (needName) {
			h.nameRetryAt = Date.now() + SENSOR_NAME_RETRY_MS;
			noteSensorRead(key, 'name', 'name unavailable, value kept', ms);
		}
		h.lastOkAt = Date.now();
		if (h.failsSinceOk) noteSensorRead(key, 'recovered', 'after ' + h.failsSinceOk + ' failed read(s)', ms);
		h.failsSinceOk = 0;
		setSensorStale(key, false);
		renderSensorValue(key, num, h.units, valueEl);
		noteSensorRead(key, 'ok', Math.round(num) + ' ' + (h.units || ''), ms);
	})
	.catch(function (err) {
		var ms = Date.now() - started;
		h.failsSinceOk++;
		noteSensorRead(key, (err && err.code) || 'error', (err && err.message) || 'read failed', ms);
		/* Nothing has ever come back for this sensor, so there is no good number to
		   protect and the row stays hidden — the pre-v0.18.0 behaviour, kept. */
		if (!h.lastOkAt) { hideSensor(key); return; }
		/* There IS a good number on the glass. A single failed read must not blank
		   it (a blip erasing a correct reading is its own lie), and it must not go
		   on looking live forever either. Keep it, and dim it at SENSOR_STALE_MS. */
		sensorStaleCheck();
	});
}

function resetSensorHealth(key) {
	var h = sensorHealth[key];
	h.sensorId = ''; h.units = null; h.unitsAt = 0; h.unitsRetryAt = 0;
	/* The NAME goes with the rest of the record, and that is the point of resetting
	   on a selection change at all: a label left over from the sensor the operator
	   just stopped watching, sitting beside a reading from the one they started, is
	   worse than no label — it is a confident wrong answer to the exact question
	   this row was given a label to answer. */
	h.name = null; h.nameAt = 0; h.nameRetryAt = 0;
	/* The VALUE goes with the record for the same reason the name does: a reading
	   from the sensor the operator just stopped watching is not a reading of the one
	   they started. */
	h.value = null;
	h.lastOkAt = 0; h.failsSinceOk = 0;
}

/* The one place a temperature is painted, so the value signal and the request
   path cannot drift into showing it two different ways. */
function renderSensorValue(key, num, rawUnits, valueEl) {
	var units = rawUnits ? String(rawUnits).replace(/^\s*°?/, '') : '';
	sensorHealth[key].value = num;
	setText(valueEl, Math.round(num) + (units ? '°' + units : '°'));
	/* The 80/90 thresholds are Celsius. iCUE reports whatever unit the
	   user picked, so a Fahrenheit reading is shown plainly and left
	   uncoloured rather than being called red at 80°F. */
	var isC = units === '' || units.charAt(0).toUpperCase() === 'C';
	setVar(valueEl, '--sensor-color',
		!isC ? 'var(--text-color)'
			: num >= SENSOR_RED_C ? 'var(--red)'
			: num >= SENSOR_AMBER_C ? 'var(--amber)'
			: 'var(--text-color)');
	paintSensorName(key);
	showSensor(key);
}

/* The other cell's key. A two-cell row, so the sibling is the one this is not —
   the label painter reads the neighbour's current name through it. */
function siblingSensorKey(key) { return key === 'gpu' ? 'cpu' : 'gpu'; }

/* True when this cell's name would only REPEAT the sibling's (v0.24.0). Measured on
   the operator's machine: iCUE answers getSensorName with the same string ("Temp #1")
   for two genuinely different sensor ids feeding the two cells — different ids,
   different readings, one name. A label that says the same thing on both cells names
   neither; the cell position already says which is CPU and which is GPU. Compared
   AFTER shortSensorName (so it matches what would actually paint), case-insensitively,
   against the sibling's CURRENT name and re-read every paint — names can change and
   nothing here is cached. A distinctive name is not a collision, and neither is a
   sibling with no name: sensorHealth[sib].name is non-null only while that cell holds
   a present reading (resetSensorHealth/hideSensor null it), so an unset, absent or
   reset neighbour reads empty here and this returns false — one cell named beside an
   empty one still shows. This is the SELECTION/name axis only; the different-ids →
   no same-sensor-warning fact and the readings themselves are untouched. */
function sensorNameCollides(key) {
	var mine = shortSensorName(sensorHealth[key].name);
	if (!mine) return false;
	var other = shortSensorName(sensorHealth[siblingSensorKey(key)].name);
	if (!other) return false;
	return mine.toLowerCase() === other.toLowerCase();
}

var paintingSensorSibling = false;

/* The label, painted from the cached name and nowhere else. Empty is a state, not
   a blank: the span is display:none rather than an empty box, because the cell is
   a flex row with a gap and an empty span still spends one. */
function paintSensorName(key) {
	paintOneSensorName(key);
	/* A collision is only visible once BOTH names are in hand, and the two cells
	   paint from independent async reads that finish in either order. Re-evaluate
	   the sibling too, so a cell that painted its name while the neighbour's was
	   still null is corrected the instant the second, equal name lands — without
	   this the first-painted cell keeps a label the duplicate has since made
	   non-distinctive, and only the later cell hides. Guarded so the sibling's
	   own repaint does not bounce straight back in. */
	if (!paintingSensorSibling) {
		paintingSensorSibling = true;
		try { paintOneSensorName(siblingSensorKey(key)); }
		finally { paintingSensorSibling = false; }
	}
}

function paintOneSensorName(key) {
	var el = key === 'gpu' ? ui.sensorGpuName : ui.sensorCpuName;
	if (!el) return;
	/* Suppress a non-distinctive name on BOTH cells (v0.24.0): when the two cells
	   hold the same name NEITHER shows it. Display-only — the readings, the units,
	   the host CPU%/MEM% segments and the same-sensor warning are all untouched;
	   this decides only whether the name label paints. */
	var txt = sensorNameCollides(key) ? '' : shortSensorName(sensorHealth[key].name);
	setText(el, txt);
	el.classList.toggle('shown', !!txt);
	/* The full string on title/aria: the clamp above is for the glass, and a
	   truncated name that cannot be recovered anywhere is a worse answer than a
	   long one. Nothing on this panel has a pointer, so this is for the
	   accessibility tree and for anyone reading the DOM during a QA pass. A
	   suppressed name drops off here too — a hidden duplicate is not worth
	   announcing to a screen reader either. */
	var full = txt && sensorHealth[key].name ? String(sensorHealth[key].name) : '';
	if (full) { el.setAttribute('title', full); el.setAttribute('aria-label', full); }
	else { el.removeAttribute('title'); el.removeAttribute('aria-label'); }
}

/* The pushed reading from sensorValueChanged. Returns false when it cannot be
   used — no units known yet, or a value that does not parse — so the caller
   falls back to a request rather than silently dropping the tick. */
function applyPushedSensorValue(key, value) {
	var h = sensorHealth[key];
	var sensorId = sensorIdFor(key);
	if (h.sensorId !== sensorId || h.units === null) return false;
	var num = parseFloat(value);
	if (!isFinite(num)) return false;
	h.lastOkAt = Date.now();
	if (h.failsSinceOk) noteSensorRead(key, 'recovered', 'via signal after ' + h.failsSinceOk + ' failed read(s)', 0);
	h.failsSinceOk = 0;
	setSensorStale(key, false);
	renderSensorValue(key, num, h.units, sensorValueEl(key));
	noteSensorRead(key, 'ok', Math.round(num) + ' ' + (h.units || '') + ' (signal)', 0);
	return true;
}

/* THE STALENESS CUE. Rides the 1 Hz tick and not the 10 s reconcile, deliberately
   — a reconcile that has itself stopped firing is one of the ways this row
   freezes, and a cue that could only be raised by the thing that broke would
   never be raised. From the tick it needs nothing to still be working.
   The test is "no successful read for SENSOR_STALE_MS", not "the number has not
   moved": a machine sitting at one temperature is the normal case. */
function sensorStaleCheck() {
	var now = Date.now();
	for (var i = 0; i < SENSOR_KEYS.length; i++) {
		var key = SENSOR_KEYS[i], h = sensorHealth[key];
		if (!sensorShown[key] || !h.lastOkAt) { setSensorStale(key, false); continue; }
		setSensorStale(key, (now - h.lastOkAt) >= SENSOR_STALE_MS);
	}
}

function setSensorStale(key, on) {
	var h = sensorHealth[key];
	if (h.stale === !!on) return;
	h.stale = !!on;
	sensorValueEl(key).classList.toggle('stale', h.stale);
	logLine('sensor ' + key + (h.stale
		? ' STALE — no successful read for ' + Math.round(SENSOR_STALE_MS / 1000) + 's, value dimmed'
		: ' live again'));
}

/* The plugin flag is a race, not a fact (lifecycle reference): a single
   negative read at boot cannot tell "plain browser" from "iCUE, not injected
   yet". Retry for a short grace window, then leave the row hidden for good. */
function sensorBootCheck() {
	/* Dev-only, mock mode only: the flag replaces the BRIDGE, so everything below
	   this line — refreshSensors, readSensor, the threshold colouring, showSensor,
	   markSensorZone — is the shipping path running on stand-in readings. */
	if (sensorForced) {
		sensorApi = forcedSensorApi();
		/* The reconcile runs in the FORCED path too (v0.18.0). Without it the dev
		   bridge read each sensor exactly once — enough for the screenshot the flag
		   was built for in v0.17.0, and useless for the thing it has to show now,
		   which is a sensor going stale over repeated failing reads. The stated
		   contract of this flag is that everything below it is the shipping path on
		   stand-in readings, and a shipping path that polls has to poll here. */
		if (!sensorTimer) sensorTimer = setInterval(refreshSensors, SENSOR_REFRESH_MS);
		refreshSensors();
		return;
	}
	if (sensorsPlugin()) { initSensors(); return; }
	if (sensorBootAttempts >= SENSOR_BOOT_RETRY_MAX) return;
	sensorBootAttempts++;
	setTimeout(sensorBootCheck, SENSOR_BOOT_RETRY_MS);
}

/* The two-method shape readSensor actually consumes, resolved from the flag. The
   unit string is spelled the way iCUE spells it ("°C"), because readSensor strips
   a leading degree sign and a stand-in that skipped it would be exercising a
   different string than the one that ships. */
function forcedSensorApi() {
	/* &sensorfail=1 reproduces the OPERATOR'S BUG SHAPE, not a generic outage: the
	   first read of each sensor resolves and paints a number, and every read after
	   it rejects. That is what a bridge whose synchronous answers were being
	   dropped looked like on the glass — one good reading at boot, then nothing,
	   with the boot number still sitting there looking live. It is the case the
	   staleness cue exists for, so it is the case the flag makes reachable. */
	var served = {};
	function maybeFail(id) {
		if (!sensorForcedFail) return null;
		if (!served[id]) { served[id] = true; return null; }
		var err = new Error('Request timeout'); err.code = 'timeout';
		return Promise.reject(err);
	}
	return {
		getSensorValue: function (id) {
			return maybeFail(id) ||
				Promise.resolve(id === 'dev:gpu' ? sensorForced.gpu : sensorForced.cpu);
		},
		getSensorUnits: function () { return Promise.resolve('°' + sensorForced.units); },
		/* The NAMES the bridge answers with (v0.21.0). Defaults are the shape iCUE
		   actually returns on a correctly-configured machine, so the off-glass row
		   defaults to the row the operator should see; &sensornames= overrides
		   either, and an empty segment is a bridge that answers with nothing — the
		   no-label path, which is a different picture and worth its own shot. */
		getSensorName: function (id) {
			var n = id === 'dev:gpu' ? sensorForced.gpuName : sensorForced.cpuName;
			return Promise.resolve(n === undefined || n === null ? '' : n);
		}
	};
}

function showSensor(key) {
	sensorShown[key] = true;
	syncSensorRow();
}

function hideSensor(key) {
	sensorShown[key] = false;
	/* A hidden cell has no number to be stale about, and leaving the flag set
	   would have the next reading paint itself dim for a frame. */
	setSensorStale(key, false);
	/* v0.21.0 — HIDING THE CELL IS NO LONGER ENOUGH TO TAKE THE NUMBER OFF THE
	   GLASS. Until this release a hidden sensor meant a hidden cell, so the text
	   left in the value span could not be seen. The CPU cell now survives a dead
	   bridge whenever the feed is serving a host figure, and the last temperature
	   would have gone on sitting beside it with nothing dimming it and no read
	   behind it — the exact "looks live, is not" failure v0.18.0 was about. So the
	   text goes with the state that produced it. */
	setText(sensorValueEl(key), '');
	var nameEl = key === 'gpu' ? ui.sensorGpuName : ui.sensorCpuName;
	if (nameEl) { setText(nameEl, ''); nameEl.classList.remove('shown'); nameEl.removeAttribute('title'); nameEl.removeAttribute('aria-label'); }
	syncSensorRow();
}

/* ---- the row's one owner (v0.21.0) ------------------------------------------

   The row is assembled from TWO sources on two clocks: iCUE's bridge (a 10 s
   reconcile plus value/units/data signals) and crabd's feed (a 3 s poll). Before
   this release only the bridge could show or hide it, and both halves writing
   `shown` from their own callback is how a row ends up hidden with a live figure
   in it — or shown with nothing. So visibility is computed in ONE place, from all
   of the state, every time any of it moves. */
function syncSensorRow() {
	var same = sensorsSameId();
	var unset = sensorsUnset();

	/* The warning is derived from the SELECTION, not from a read: it is knowable
	   with the bridge unable to answer a single request, and that is exactly the
	   run where the operator most needs to be told which of the two faults this
	   is. Requires a bound bridge only because without one there is no row. */
	var warn = !!sensorApi && same;
	setText(ui.sensorGpuWarn, warn ? 'same sensor' : '');
	ui.sensorGpuWarn.classList.toggle('shown', warn);
	ui.sensorGpu.classList.toggle('warn', warn);
	if (warn) {
		ui.sensorGpu.setAttribute('title', 'CPU and GPU are set to the same sensor — pick a different GPU sensor in the widget settings');
		ui.sensorGpu.setAttribute('aria-label', 'GPU: same sensor as CPU, no separate reading');
	} else {
		ui.sensorGpu.removeAttribute('title');
		ui.sensorGpu.removeAttribute('aria-label');
	}
	if (warn !== sameWarned) {
		sameWarned = warn;
		logLine(warn
			? 'sensors: cpuTempSensor and gpuTempSensor hold the SAME id — GPU cell shows "same sensor"'
			: 'sensors: cpu and gpu ids differ again');
	}

	setText(ui.sensorHint, unset ? 'pick sensors in settings' : '');
	ui.sensorHint.classList.toggle('shown', unset);

	/* A cell is shown when it has something true to say, which for the CPU cell is
	   a temperature OR a host figure — the two are independent, and a bridge that
	   cannot answer must not take the companion's number off the glass with it. */
	var cpuOn = sensorShown.cpu || hostMetrics.cpuPct !== null;
	var gpuOn = sensorShown.gpu || warn;
	var memOn = hostMetrics.memPct !== null;
	ui.sensorCpu.classList.toggle('shown', cpuOn);
	ui.sensorGpu.classList.toggle('shown', gpuOn);
	ui.hostMem.classList.toggle('shown', memOn);

	var any = cpuOn || gpuOn || memOn || unset;
	ui.sensors.classList.toggle('shown', any);

	/* THE DRILL-IN (v0.22.0), decided here because this function is the row's one
	   owner and the tap is a fact about the row. It follows the READING, not the
	   markup — the discipline setGaugeTappable keeps: the row is a control only
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

/* The sensors row is the ONLY thing the Limits zone can show in the standalone
   state, so whether it has anything to say decides whether that zone is a zone at
   all. A body class rather than a CSS :has() on the row: iCUE renders in
   QtWebEngine, whose version is the console's to choose, and a layout that
   silently loses a whole column on an older engine is not a trade worth a saved
   line. Since v0.21.0 the argument is the whole row's verdict, not just a
   temperature: the "pick sensors" hint is the one thing a fresh import has in
   this zone, and a zone dropped out from under it would delete the prompt. */
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
	var cut = now - HOST_WINDOW_MS;
	while (hostRing.length && (hostRing[0].t < cut || hostRing.length > HOST_RING_MAX)) hostRing.shift();
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
   and nothing else. Temperatures alone do not earn the tap: they come from iCUE's
   bridge, they are not sampled into the ring, and a sheet that charted nothing
   would be a control that opens an empty view. */
function hostSheetAvailable() {
	return hostMetrics.cpuPct !== null || hostMetrics.memPct !== null;
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
		sensorText('cpu'), sensorText('gpu')].join('#');
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

	/* The temperatures, as TEXT and never as a third chart: they are not in the ring
	   (iCUE's bridge feeds them on its own clock, not the poll's) so there is no
	   ten-minute history of them to draw, and drawing one from the ring's timestamps
	   would be charting a series against somebody else's samples. */
	var temps = [];
	for (var i = 0; i < SENSOR_KEYS.length; i++) {
		var t = sensorText(SENSOR_KEYS[i]);
		if (t) temps.push(t);
	}
	var line = document.createElement('div');
	line.className = 'hs-temps';
	line.textContent = temps.length ? temps.join('     ') : 'no hardware sensor reading';
	ui.sheetHost.appendChild(line);
}

/* One temperature in words, or '' when the row has nothing true to say about it —
   the row's OWN verdict (sensorShown) rather than a second opinion, so the sheet
   cannot report a reading the row has already taken off the glass. */
function sensorText(key) {
	if (!sensorShown[key]) return '';
	var h = sensorHealth[key];
	if (typeof h.value !== 'number' || !isFinite(h.value)) return '';
	var units = h.units ? String(h.units).replace(/^\s*°?/, '') : '';
	var name = shortSensorName(h.name);
	return key.toUpperCase() + ' ' + Math.round(h.value) + (units ? '°' + units : '°') +
		(name ? ' ' + name : '') + (h.stale ? ' (stale)' : '');
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

/* WHY THIS EXISTS. The operator reports that touch "doesn't seem to work" on the
   physical Edge — and yet panel approvals were tapped and landed live, and the
   manifest has carried `interactive: true` since v0.2.0. Both of those are true at
   once only if SOMETHING arrives and something else does not. The spec's own words
   for what iCUE forwards are "widget click handling", which would make a tap a
   synthesized CLICK and leave every gesture on this panel — swipe, long press,
   two-finger tap, pull-to-refresh — reading an event stream that is not there.
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

/* The property, the flag, or neither. Read live on every call — an iCUE switch can
   move under a running panel, and applyProperties() is what notices. */
function diagWanted() {
	if (mockName && diagForced) return true;
	return boolProp('touchDiag', false);
}

/* The reconcile. Called from applyProperties (iCUE fires onDataUpdated for ANY
   property) and once at boot. Idempotent by construction: it compares the wanted
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
	/* The sensor staleness cue (v0.18.0). Deliberately here and not on the sensor
	   reconcile: a reconcile that has stopped firing is one of the ways the row
	   freezes, so the cue must not depend on it. */
	sensorStaleCheck();
}

function init() {
	var ids = ['flash', 'banner', 'bannerText', 'crab', 'crabWrap', 'limitsHead', 'clockHm', 'clockSs', 'clockDate',
		'quietNote', 'fleet', 'fleetGlow', 'fleetToast',
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
		'sheetApprovalThreshold', 'sheetApprove', 'sheetDeny',
		'sheetContinue', 'sheetContinueBtns', 'sheetContinueStatus',
		'notice', 'noticeText'];
	for (var i = 0; i < ids.length; i++) ui[ids[i]] = document.getElementById(ids[i]);
	ui.extraRows = [];
	ui.sparkBars = [];
	ui.sparkLabelSig = null;
	ui.recapSig = null;
	ensureSparkBars(SPARK_BUCKETS);

	var m = /[?&]mock=([a-z]+)/i.exec(window.location.search);
	if (m && MOCKS.indexOf(m[1].toLowerCase()) !== -1) mockName = m[1].toLowerCase();

	/* Dev-only flags, all gated on mock mode — they must never be reachable from
	   the iCUE origin, where there is no query string to carry them anyway.
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
			else if (want === 'juggle' || want === 'bounce' || want === 'snap') forcedTrick = want;
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
		/*   &approvalsec=<seconds>  stand in for the iCUE `approvalThreshold`
		     property, the same way &uid= stands in for uniqueId (v0.16.0). A dev
		     browser has no property sheet at all, so without this the only
		     observable state of the approval threshold off-glass is its default —
		     and the whole point of the setting is what happens when it MOVES.
		     It feeds approvalPropertySec() and nothing else, so what is exercised is
		     the real baseline/touch/POST path: boot once with no flag (the body logs
		     no approvalThresholdSec), reload with the flag on the SAME &uid= (the
		     value moved off the recorded baseline, so the key is now in the body and
		     stays there). */
		var apx = /[?&]approvalsec=(\d+)/.exec(window.location.search);
		if (apx) approvalForcedSec = Number(apx[1]);
		/*   &sensors=<cpu>[,<gpu>][,C|F]  stand in for the iCUE Sensors bridge
		     (v0.17.0). Two numbers so the amber (80) and red (90) steps are
		     reachable, and the unit letter so the Fahrenheit branch — which is
		     deliberately left uncoloured — has a shot too. */
		var sn = /[?&]sensors=(\d{1,3})(?:,(\d{1,3}))?(?:,([CF]))?\b/i.exec(window.location.search);
		if (sn) {
			sensorForced = {
				cpu: Number(sn[1]),
				gpu: sn[2] === undefined ? Number(sn[1]) : Number(sn[2]),
				units: (sn[3] || 'C').toUpperCase(),
				/* iCUE's own shape for these on a correctly-configured machine, so the
				   off-glass default is the row the operator ought to be looking at. */
				cpuName: 'CPU Package',
				gpuName: 'GPU Core'
			};
		}
		/*   &sensors=none  the bridge is HERE and neither property holds an id — the
		     fresh-import case (v0.21.0). Parsed as its own branch rather than as a
		     number, because "no sensor selected" is not a temperature and the whole
		     point of the state is that sensorIdFor returns the same empty string an
		     unset iCUE property gives. */
		if (/[?&]sensors=none\b/i.test(window.location.search)) {
			sensorForced = { none: true, cpu: 0, gpu: 0, units: 'C', cpuName: '', gpuName: '' };
		}
		/*   &sensornames=<cpu>|<gpu>  what getSensorName answers (v0.21.0). Either
		     side may be empty for the no-label path; the pipe is the separator
		     because a sensor name may well contain a comma and cannot contain one of
		     these without already being a path the shortener splits on. */
		var snm = /[?&]sensornames=([^&]*)/i.exec(window.location.search);
		if (snm && sensorForced) {
			var parts = decodeURIComponent(snm[1].replace(/\+/g, ' ')).split('|');
			sensorForced.cpuName = parts[0] === undefined ? '' : parts[0];
			sensorForced.gpuName = parts[1] === undefined ? '' : parts[1];
		}
		/*   &sensorsame=1  both properties resolve to ONE id (v0.21.0) — the
		     operator's measured defect, reproduced rather than simulated. It moves
		     sensorIdFor and nothing else, so the same-sensor test, the skipped GPU
		     read and the warning cell are all the shipping path answering about real
		     state. */
		if (/[?&]sensorsame=1\b/.test(window.location.search)) sensorForcedSame = true;
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
		/*   &touchdiag=1  stand in for the iCUE `touchDiag` switch (v0.23.0), the way
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
		/*   &sensorlog=1  every read outcome to the console, not just the failures
		     and the health transitions (v0.18.0). Off by default because a healthy
		     panel reads two sensors every 10 s and would otherwise fill the console
		     with ~17,000 lines a day saying nothing changed. The ring buffer at
		     window.__sidecrabSensorLog holds the last SENSOR_LOG_MAX either way,
		     which is what a debugger attached AFTER a freeze can read. */
		if (/[?&]sensorlog=1\b/.test(window.location.search)) sensorLogVerbose = true;
		/*   &sensorfail=1  make every sensor read reject, so the staleness cue can
		     be watched arriving (v0.18.0). Applied to the forced bridge, so what is
		     exercised is the real reject path through readSensor: keep the number
		     for SENSOR_STALE_MS, then dim it. Pair with &sensorstale=<ms> to avoid
		     waiting the full minute for a screenshot. */
		if (/[?&]sensorfail=1\b/.test(window.location.search)) sensorForcedFail = true;
		var ss = /[?&]sensorstale=(\d{1,7})/.exec(window.location.search);
		if (ss) SENSOR_STALE_MS = Number(ss[1]);
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
	applyProperties();
	tick();
	poll();
	setInterval(poll, POLL_MS);
	setInterval(tick, 1000);
	if (forcedTrick) startForcedTrick(forcedTrick);
	scheduleBlink();
	sensorBootCheck();
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
		else fireSnap();
	}
	trickLoop = setInterval(run, name === 'juggle' ? JUGGLE_MS + 400 : 1400);
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

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
