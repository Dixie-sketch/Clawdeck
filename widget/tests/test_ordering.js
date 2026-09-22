/* SideCrab widget — the ordering tests (v0.26.0, AUD-F5 + the fmtNum boundary).
 *
 *   node widget/tests/test_ordering.js
 *
 * WHY A VM AND NOT A MODULE. scripts/sidecrab.js is a flat browser script that
 * ends by calling init(); it has no exports and this repo ships no bundler, and a
 * second copy of the ordering rules in a test file would be a copy that can
 * disagree with the panel. So the SHIPPING file is loaded whole into a vm context
 * with a document stub whose readyState is 'loading' — the same branch a real
 * browser takes before DOMContentLoaded, which parks init() on a listener nobody
 * fires. Nothing renders, and the functions under test are the ones on the glass.
 * If this file ever stops loading, the cause is new TOP-LEVEL work in sidecrab.js
 * (everything else lives inside a function): add the stub it needs, do not fork
 * the logic.
 *
 * WHAT IS PINNED. The compact grid's "+N more" tile is acceptable only while a
 * WAITING (needs_input) card can never be the row it swallows. Until v0.26.0 the
 * widget held that by inheriting crabd's pre-sort — see clampGrid's comment — so
 * the mis-ordered case below is the one that used to fail, and the last test
 * proves it still fails against the old bare slice. A test that cannot fail is
 * worse than no test: it reports success forever.
 */
'use strict';

var fs = require('fs');
var path = require('path');
var vm = require('vm');

var SRC = path.join(__dirname, '..', 'scripts', 'sidecrab.js');

function loadWidget() {
	var listeners = 0;
	var doc = {
		readyState: 'loading',
		addEventListener: function () { listeners++; },
		documentElement: { style: { setProperty: function () {} } },
		body: { classList: { toggle: function () {}, add: function () {}, remove: function () {}, contains: function () { return false; } } },
		getElementById: function () { return null; },
		querySelector: function () { return null; },
		createElement: function () { throw new Error('the ordering tests build no DOM'); }
	};
	var sandbox = { document: doc, console: console };
	sandbox.window = sandbox;
	sandbox.self = sandbox;
	sandbox.location = { search: '', href: 'http://127.0.0.1/index.html' };
	sandbox.navigator = { userAgent: 'node' };
	sandbox.setTimeout = function () { return 0; };
	sandbox.clearTimeout = function () {};
	sandbox.setInterval = function () { return 0; };
	sandbox.clearInterval = function () {};
	var ctx = vm.createContext(sandbox);
	vm.runInContext(fs.readFileSync(SRC, 'utf8'), ctx, { filename: 'sidecrab.js' });
	if (!listeners) throw new Error('init() ran: the document stub was not in the loading state');
	return ctx;
}

var W = loadWidget();

/* ------------------------------------------------------------------ harness */

var failures = 0, checks = 0;

function ok(cond, what) {
	checks++;
	if (!cond) { failures++; console.log('FAIL  ' + what); }
}

function eq(actual, expected, what) {
	ok(JSON.stringify(actual) === JSON.stringify(expected),
		what + '  (got ' + JSON.stringify(actual) + ', want ' + JSON.stringify(expected) + ')');
}

function ids(list) { return list.map(function (s) { return s.id; }); }

function sess(id, state) { return { id: id, state: state, stateSince: '2026-08-28T12:00:00Z' }; }

/* The feed as the contract describes it: needs_input, then working, then done,
   then idle (docs/STATE-CONTRACT.md, the sessions[] comment). */
function contractFeed(waiting, working, done, idle) {
	var out = [], i;
	for (i = 0; i < waiting; i++) out.push(sess('w' + i, 'needs_input'));
	for (i = 0; i < working; i++) out.push(sess('r' + i, 'working'));
	for (i = 0; i < done; i++) out.push(sess('d' + i, 'done'));
	for (i = 0; i < idle; i++) out.push(sess('i' + i, 'idle'));
	return out;
}

/* THE INVARIANT, stated once: no waiting row is cut while any row that is not
   waiting keeps a cell. */
function invariantHolds(res) {
	var cutWaiting = res.rest.some(function (s) { return s.state === 'needs_input'; });
	var keptOther = res.visible.some(function (s) { return s.state !== 'needs_input'; });
	return !(cutWaiting && keptOther);
}

/* The clamp exactly as it stood before v0.26.0 — kept here as the MUTANT, so the
   invariant test above is proven able to fail. */
function bareSliceClamp(list, capacity) {
	if (!(capacity >= 1) || list.length <= capacity) return { visible: list, rest: [], chipText: null };
	return { visible: list.slice(0, capacity - 1), rest: list.slice(capacity - 1), chipText: 'x' };
}

/* ------------------------------------------------------- the clamp invariant */

/* 1. A contract-ordered feed, every capacity the stylesheet can produce
      (gridCapacity is cols x rows, 2x2 through 4x3). */
[4, 6, 8, 9, 12].forEach(function (cap) {
	[[2, 6, 3, 3], [0, 14, 0, 0], [5, 5, 5, 5], [1, 0, 0, 13], [14, 0, 0, 0]].forEach(function (shape) {
		var feed = contractFeed(shape[0], shape[1], shape[2], shape[3]);
		var res = W.clampGrid(feed, cap);
		ok(invariantHolds(res), 'invariant, contract feed ' + shape.join('/') + ' at capacity ' + cap);
		ok(res.visible.length <= cap, 'the grid is never overfilled (' + shape.join('/') + ' at ' + cap + ')');
		/* the ORDER of both lists is the order it arrived in */
		eq(ids(res.visible).concat(ids(res.rest)).sort(), ids(feed).sort(),
			'every row is in exactly one list (' + shape.join('/') + ' at ' + cap + ')');
		eq(ids(res.visible), ids(bareSliceClamp(feed, cap).visible),
			'byte-for-byte the old slice on a contract feed (' + shape.join('/') + ' at ' + cap + ')');
	});
});

/* 2. The case the widget used to get wrong: a feed that is NOT pre-sorted. */
var messy = contractFeed(0, 0, 12, 0).concat([sess('LATE', 'needs_input')]).concat(contractFeed(0, 0, 0, 2));
var messyRes = W.clampGrid(messy, 8);
ok(invariantHolds(messyRes), 'invariant holds on a feed that is not pre-sorted');
ok(ids(messyRes.visible).indexOf('LATE') !== -1, 'the waiting row survives the clamp on a mis-ordered feed');
ok(ids(messyRes.rest).indexOf('LATE') === -1, 'the waiting row is not in the "+N more" tail');
/* order is preserved: LATE is still after the done rows it arrived behind */
ok(ids(messyRes.visible).indexOf('LATE') === messyRes.visible.length - 1,
	'the clamp promotes nothing: LATE keeps its place in the visible order');

/* 3. THE MUTATION PROOF — the same feed through the pre-0.26.0 clamp must FAIL,
      or the two tests above are decoration. */
ok(!invariantHolds(bareSliceClamp(messy, 8)),
	'MUTATION: the bare slice violates the invariant on the mis-ordered feed');

/* 4. More waiting rows than cells: waiting rows are cut, but only by waiting
      rows — the tile is CD-14's route to them. */
var allWaiting = W.clampGrid(contractFeed(14, 0, 0, 0), 8);
eq(allWaiting.visible.length, 7, 'seven cells of cards and one tile at capacity 8');
ok(allWaiting.rest.every(function (s) { return s.state === 'needs_input'; }),
	'with 14 waiting rows the tail is waiting rows only');
eq(allWaiting.chipText, '+7 more', 'the tile counts what it hides');

/* 5. The tile's wording, which CD-14 keys on the tail's states. */
eq(W.clampGrid(contractFeed(1, 0, 7, 6), 8).chipText, '+7 idle', 'a done/idle-only tail reads "idle"');
eq(W.clampGrid(contractFeed(1, 8, 1, 0), 8).chipText, '+3 more', 'a tail with a working row reads "more"');

/* 6. Nothing to clamp, and the degenerate capacities. */
var few = contractFeed(1, 2, 0, 0);
var noClamp = W.clampGrid(few, 8);
ok(noClamp.visible === few, 'a list inside capacity is passed through untouched');
eq(noClamp.rest, [], 'nothing is cut');
eq(noClamp.chipText, null, 'no tile');
eq(W.clampGrid(contractFeed(0, 4, 0, 0), 1).visible.length, 0, 'capacity 1 is the tile alone');
eq(W.clampGrid(contractFeed(0, 4, 0, 0), 0).rest, [], 'capacity 0 clamps nothing rather than throwing');

/* ------------------------------------------------------------ pins + filters */

/* A pin must never lift a done card over a waiting one: sortPinned sorts pinned
   first WITHIN a band and never across one. Pinning is the vendor-store map the
   widget keeps, so the test writes it the way togglePin does. */
W.pinned['d2'] = Date.now();
var pinnedOrder = W.sortPinned(contractFeed(2, 2, 3, 0));
eq(ids(pinnedOrder), ['w0', 'w1', 'r0', 'r1', 'd2', 'd0', 'd1'],
	'a pinned done row rises inside its own band only');
var pinnedClamp = W.clampGrid(pinnedOrder, 4);
ok(invariantHolds(pinnedClamp), 'invariant survives a pin');
ok(ids(pinnedClamp.visible).indexOf('w0') !== -1 && ids(pinnedClamp.visible).indexOf('w1') !== -1,
	'both waiting rows keep their cells with a done row pinned');
delete W.pinned['d2'];

/* The filter narrows the list and must not reorder it. */
var feed = contractFeed(2, 3, 2, 1);
W.filterIdx = 0;
eq(ids(W.filterSessions(feed)), ids(feed), 'the All filter is the identity');
for (var f = 0; f < W.FILTERS.length; f++) {
	W.filterIdx = f;
	var got = W.filterSessions(feed);
	var order = ids(feed).filter(function (id) { return ids(got).indexOf(id) !== -1; });
	eq(ids(got), order, 'the ' + W.FILTERS[f].key + ' filter preserves order');
	ok(invariantHolds(W.clampGrid(got, 4)), 'invariant under the ' + W.FILTERS[f].key + ' filter');
}
W.filterIdx = 0;

/* ------------------------------------------------- fmtNum's boundary (AUD-F6) */

/* 999,999 painted "1000k" until v0.26.0: Math.round(999999 / 1e3) is 1000, a
   four-digit k. The M branch starts where the k branch's own rounding reaches it. */
eq(W.fmtNum(999499), '999k', 'below the boundary the k branch still rounds');
eq(W.fmtNum(999500), '1.0M', 'the k branch would round this to 1000k, so M owns it');
eq(W.fmtNum(999999), '1.0M', 'AUD-F6: no "1000k"');
eq(W.fmtNum(1000000), '1.0M', 'a million is unchanged');
eq(W.fmtNum(-999999), '-1.0M', 'the boundary is on the magnitude');
eq(W.fmtNum(1954200), '2.0M', 'the ?mock=hot context figure is unchanged');
eq(W.fmtNum(19640000), '19.6M', 'the ?mock=rework CACHE RD figure is unchanged');
eq(W.fmtNum(10000), '10k', 'the k branch is unchanged');
eq(W.fmtNum(999), '999', 'small numbers are printed whole');
eq(W.fmtNum(null), '—', 'a non-number is an em-dash, never a zero');
eq(W.fmtNum(NaN), '—', 'NaN is an em-dash');
/* The diag chip's five-character budget: it clamps ABOVE DIAG_COUNT_SHOWN_MAX, so
   the widest string fmtNum can hand it is the boundary value's. */
ok(W.fmtNum(W.DIAG_COUNT_SHOWN_MAX).length <= 5,
	'the diag counter stays inside its five-character width budget');
eq(W.DIAG_COUNT_SHOWN_MAX, 999999, 'the diag clamp is still the value the width budget was measured on');

/* ================================================= lane C: the view switcher */

/* WHAT IS PINNED HERE. An alert must never hide behind a view, and the badge on
   the Sessions chip is the whole of that promise: nothing force-switches the
   glass, so if the badge is wrong the operator is looking at a burn chart with a
   question waiting behind it and no sign of it anywhere.
   It counts the EDGE and never the value, and the mutant at the end of the block
   is a value count - which badges a panel that booted beside two waiting rows
   and has watched nothing happen. */

function laneCFeed(states) {
	return states.map(function (st, i) { return sess('s' + i, st); });
}
function laneCDoc(list) { W.lastGoodDoc = { sessions: list }; return list; }
function laneCView(key) { W.viewIdx = W.prefIndexOrNone(W.VIEWS, key); }
function laneCReset(key, seeded) {
	laneCView(key);
	W.viewAlerts = {};
	W.viewPrevState = {};
	W.viewPrevSeeded = !!seeded;
	W.everHadData = true;
}

/* The mutant: the badge as a count of what is waiting NOW. */
function valueBadge(list) {
	var n = 0;
	for (var i = 0; i < list.length; i++) {
		if (list[i].state === 'needs_input' && !W.effectiveAcked(list[i])) n++;
	}
	return n;
}

/* 1. A panel that boots straight into another view has watched nothing happen. */
laneCReset('burn', false);
var lcFeed = laneCDoc(laneCFeed(['needs_input', 'needs_input', 'working']));
W.trackViewAlerts(lcFeed);
eq(W.viewAlertCount(), 0, 'a boot into another view beside two waiting rows badges nothing');

/* 2. A session that STARTS waiting while that view is up is counted, and only it. */
lcFeed[2].state = 'needs_input';
W.trackViewAlerts(lcFeed);
eq(W.viewAlertCount(), 1, 'a session that starts waiting behind another view is counted');
ok(W.viewAlerts['s2'] === 1, 'the row that flipped is the row that is counted');
ok(W.viewAlerts['s0'] === undefined && W.viewAlerts['s1'] === undefined,
	'the rows that were already waiting are not counted a second time');

/* 3. A session brand new to the panel and already waiting is a new alert. */
lcFeed.push(sess('sNEW', 'needs_input'));
W.trackViewAlerts(lcFeed);
eq(W.viewAlertCount(), 2, 'a session that arrives already waiting is an alert too');

/* 4. Answered ANYWHERE - at the keyboard, by the crab tap, by the session moving
      on - and it stops counting here. */
lcFeed[2].acked = true;
lcFeed[3].state = 'working';
W.trackViewAlerts(lcFeed);
eq(W.viewAlertCount(), 0, 'an alert answered anywhere stops counting on the chip');

/* 5. On the Sessions view there is nothing to badge: the cards are on the glass. */
laneCReset('sessions', true);
var lcSeen = laneCDoc(laneCFeed(['working']));
W.trackViewAlerts(lcSeen);
lcSeen[0].state = 'needs_input';
W.trackViewAlerts(lcSeen);
eq(W.viewAlertCount(), 0, 'the Sessions view badges nothing: the card is already showing');

/* 6. THE MUTATION PROOF. */
laneCReset('burn', false);
var lcBoot = laneCDoc(laneCFeed(['needs_input', 'needs_input', 'working']));
W.trackViewAlerts(lcBoot);
eq(W.viewAlertCount(), 0, 'the edge test badges a boot with nothing');
ok(valueBadge(lcBoot) > 0,
	'MUTATION: a count of the VALUE badges a panel that has watched nothing happen');

/* ------------------------------------ the view preference, through the vendor store */

/* Same object, same key discipline, same round-trip of a value this build does
   not know (v0.16.0, audit F2): an untouched save must leave a NEWER build's
   view exactly as it found it. */
var lcStore = {};
W.localStorage = {
	getItem: function (k) { return Object.prototype.hasOwnProperty.call(lcStore, k) ? lcStore[k] : null; },
	setItem: function (k, v) { lcStore[k] = String(v); }
};
lcStore['lane-c'] = JSON.stringify({ gridView: 'constellation', sessionFilter: 'all', density: 'comfortable' });
W.mockName = 'rework';
W.devUidOverride = 'lane-c';
W.loadPrefs();
eq(W.viewIdx, 0, 'a stored view this build does not know is the default mode, not an error');
eq(W.viewStoredUnknown, 'constellation', 'and it is remembered so it can be written back');
W.savePrefs();
eq(JSON.parse(lcStore['lane-c']).gridView, 'constellation',
	"an untouched save leaves a newer build's view exactly as it found it");
/* A TAP is the operator overriding that. */
W.viewIdx = W.prefIndexOrNone(W.VIEWS, 'week');
W.viewStoredUnknown = null;
W.savePrefs();
eq(JSON.parse(lcStore['lane-c']).gridView, 'week', 'a tap writes this build`s own key');
lcStore['lane-c'] = JSON.stringify({ gridView: 'detail' });
W.loadPrefs();
eq(W.VIEWS[W.viewIdx].key, 'detail', 'a stored view this build knows is restored');
W.mockName = null;
W.devUidOverride = null;
W.viewIdx = 0;
W.viewStoredUnknown = null;

/* ------------------------------------------- where a decide or a continue goes */

W.sheetSessionId = 'from-the-sheet';
laneCView('detail');
W.detailSessionId = 'from-the-page';
eq(W.laneCActionSessionId(), 'from-the-sheet', 'a sheet is a modal over the page and wins');
W.sheetSessionId = null;
eq(W.laneCActionSessionId(), 'from-the-page', 'with no sheet the Detail page is the target');
laneCView('sessions');
eq(W.laneCActionSessionId(), null, 'no sheet and no Detail page is not a target');

/* The page opens on the row that wants a human when nothing has been chosen. */
laneCDoc(laneCFeed(['idle', 'working', 'needs_input']));
W.detailSessionId = null;
eq(W.detailTarget().state, 'needs_input', 'with nothing chosen the page opens on the waiting row');
W.detailSessionId = 's1';
eq(W.detailTarget().id, 's1', 'a chosen session that is still in the feed is kept');
W.detailSessionId = 'gone';
eq(W.detailTarget().state, 'needs_input', 'a chosen session that has gone falls back, it does not blank');
W.detailSessionId = null;
W.lastGoodDoc = null;
eq(W.detailTarget(), null, 'an empty feed has no page to show, and says so rather than throwing');

/* --------------------------------------------------- the canvas crab's timing */

/* The ease is the curve a limb moves on: both ends exact, symmetric about the
   middle, and monotonic - a pose that went backwards mid-sweep would read as a
   stutter rather than as a wave. */
eq(W.crabEase(0), 0, 'the ease starts where the pose starts');
eq(W.crabEase(1), 1, 'and ends where it ends');
eq(Math.round(W.crabEase(0.5) * 1000) / 1000, 0.5, 'and is symmetric about the middle');
var lcPrev = -1, lcMono = true;
for (var lcI = 0; lcI <= 100; lcI++) {
	var lcV = W.crabEase(lcI / 100);
	if (lcV < lcPrev) lcMono = false;
	lcPrev = lcV;
}
ok(lcMono, 'the ease never goes backwards');

/* A class that outlives its own animation holds the last frame rather than
   restarting - what the CSS does at the end of an iteration count, and what a
   bare modulo would have got wrong. */
W.crabMotion = { dance: 1000 };
eq(W.crabPhase('dance', 390, 4, 1000), 0, 'a motion starts at the top of its first iteration');
ok(W.crabPhase('dance', 390, 4, 1000 + 390 * 4 - 1) !== null, 'the last iteration still runs');
eq(W.crabPhase('dance', 390, 4, 1000 + 390 * 4), null, 'and the motion is over when its iterations are');
eq(W.crabPhase('snap', 260, 2, 1000), null, 'a motion that is not running has no phase at all');
W.crabMotion = {};

/* The hour label is cut out of the contract's own string: a bare local date-time
   is parsed as LOCAL by the spec and as UTC by some engines, and a reading that
   depends on which one arrived slides by the offset. */
eq(W.hourLabel('2026-08-25T13:00:00'), '13', 'the hour comes off the string');
eq(W.hourLabel('2026-08-25T00:00:00Z'), '00', 'midnight is not falsy');
eq(W.hourLabel(null), '', 'a missing bucket labels nothing');
eq(W.hourLabel('nonsense'), '', 'and so does a string with no hour in it');

/* ------------------------------------------ lane E: bring a session to the front */

/* THE GATE, and it moved in v0.32.0. A bridge being present is no longer enough:
   what a host CAN do is a thing only the host can state, so the control waits for
   the host-info handshake to say focusSession. A browser preview never gets one. */
eq(W.laneEAvailable(), false, 'with no host bridge the control does not exist');
eq(W.laneETarget(), null, 'and with no sheet and no Detail page there is nothing to focus');

/* A stub bridge is the standalone shape. It is installed and removed around the
   check so nothing after this point runs against a fake host. */
var leSent = [];
var leListener = null;
W.chrome = { webview: {
	postMessage: function (m) { leSent.push(m); },
	addEventListener: function (type, fn) { if (type === 'message') leListener = fn; }
} };
eq(W.laneEAvailable(), false, 'a bridge that has not answered the handshake promises nothing');

W.bridgeInit();
eq(leSent.length, 1, 'boot asks the host what it is');
eq(leSent[0].type, 'host-info', 'with host-info');
ok(typeof leSent[0].requestId === 'string' && leSent[0].requestId.length > 0 &&
	leSent[0].requestId.length <= 64,
	'carrying a page-generated requestId of at most 64 characters');

/* A LATE OR UNSOLICITED REPLY IS DROPPED. This is the half that stops an answer to
   a superseded request from landing on the newer one, and it is proved with a
   reply that would otherwise have granted every capability. */
leListener({ data: { type: 'host-info', requestId: 'not-a-request-this-page-made',
	version: '9.9.9', capabilities: { saveSettings: true, focusSession: true } } });
eq(W.laneEAvailable(), false, 'a reply this page never asked for grants nothing');

leListener({ data: { type: 'host-info', requestId: leSent[0].requestId, version: '0.4.0',
	pid: 4242, startedAt: '2026-09-21T09:00:00Z', settingsPath: 'D:\\panel\\panel-settings.json',
	hasToken: true, capabilities: { saveSettings: true, focusSession: true, pickDisplay: false } } });
ok(W.laneEAvailable(), 'once the host says it can focus a window, the control exists');
eq(W.hostCan('pickDisplay'), false, 'a capability the host declined stays declined');
eq(W.hostCan('somethingElse'), false, 'and one it never mentioned is not invented');

/* Four facts and NEVER a window handle: the host ranks the windows it enumerated
   itself, and a handle from the page would be a window picker aimed by the page. */
W.lastGoodDoc = { sessions: [{ id: 's1', state: 'needs_input', stateSince: '2026-09-21T12:00:00Z',
	title: 'SideCrab Panel Windows app', cwd: 'D:\\panel\\app', repo: 'sidecrab' }] };
W.sheetSessionId = 's1';
W.laneESendFocus();
eq(leSent.length, 2, 'a tap sends exactly one message');
var leFocus = leSent[1];
eq(leFocus.type, 'focus-session', 'and it is the focus-session type');
eq(leFocus.sessionId, 's1', 'carrying the session id');
eq(leFocus.title, 'SideCrab Panel Windows app', 'the title');
eq(leFocus.cwd, 'D:\\panel\\app', 'the cwd');
eq(leFocus.repo, 'sidecrab', 'and the repo');
ok(typeof leFocus.requestId === 'string' && leFocus.requestId !== leSent[0].requestId,
	'and its own requestId, which is not the handshake\'s');
eq(Object.keys(leFocus).length, 6, 'and nothing else at all — no handle, no command');
eq(W.laneEStatusText, 'bringing it to the front', 'the line says the ask is in flight');

/* The host's answer is what moves the line, and only for the request just made: a
   reply for a requestId this page is not waiting for is a late answer to something
   the operator has moved past. */
leListener({ data: { type: 'focus-result', requestId: 'stale-request', sessionId: 's1', ok: true, reason: 'focused' } });
eq(W.laneEStatusText, 'bringing it to the front', 'a reply for a superseded request is dropped');
leListener({ data: { type: 'focus-result', requestId: leFocus.requestId, sessionId: 's1', ok: true, reason: 'focused' } });
eq(W.laneEStatusText, 'brought to front', 'the request that was made is reported');

/* Two different true answers. The desktop app holds every session that has no window
   of its own in ONE window, so the host found the APP and not this session's window;
   saying "brought to front" there would be a claim the host never made. */
W.laneEPending = 's1';
W.laneEOnFocusResult({ type: 'focus-result', sessionId: 's1', ok: true, reason: 'desktop-app' });
eq(W.laneEStatusText, 'brought the Claude app to the front', 'the fallback says it was the app');

W.laneEPending = 's1';
W.laneEOnFocusResult({ type: 'focus-result', sessionId: 's1', ok: false, reason: 'no-match' });
eq(W.laneEStatusText, 'no window found for this session', 'no match says so plainly');
W.laneEPending = 's1';
W.laneEOnFocusResult({ type: 'focus-result', sessionId: 's1', ok: false, reason: 'ambiguous' });
eq(W.laneEStatusText, 'more than one window could be this session', 'a tie is named, not guessed at');
W.laneEPending = 's1';
/* A reason this build has never heard of still says something an operator can act
   on, rather than printing the host's wire word at them. */
W.laneEOnFocusResult({ type: 'focus-result', sessionId: 's1', ok: false, reason: 'something-new' });
eq(W.laneEStatusText, 'no window found for this session', 'an unknown reason falls back to the plain one');

/* ok must be the literal true. A truthy string from a future host is a host this
   build does not understand, and "focused" must not be read as success. */
W.laneEPending = 's1';
W.laneEOnFocusResult({ type: 'focus-result', sessionId: 's1', ok: 'yes' });
eq(W.laneEStatusText, 'no window found for this session', 'only a real true counts as brought forward');

/* SCA-021: a result about a DIFFERENT session ends the pending state rather than
   leaving it running. Every pending state has a terminal end. */
W.laneEPending = 's1';
W.laneEOnFocusResult({ type: 'focus-result', sessionId: 'another', ok: true, reason: 'focused' });
eq(W.laneEStatusText, 'the host answered about a different session', 'a mismatched session is said out loud');
eq(W.laneEPending, null, 'and the pending state ends');

delete W.chrome;
W.hostInfo = null;
eq(W.laneEAvailable(), false, 'and the gate closes again with the bridge gone');
W.sheetSessionId = null;
W.lastGoodDoc = null;
W.laneEPending = null;

/* -------------------------------- lane D: the continue vocabulary per repo */

/* The builder both the session sheet and the Detail view draw from. Order is the
   drawing order - builtins, the feed's global extras, then this session's own -
   and the mutant at the end is the flattened list the widget must NOT build. */

function contSess(id, own) {
	var s = { id: id, state: 'working', stateSince: '2026-09-21T12:00:00Z' };
	if (own !== undefined) s.continuePrompts = own;
	return s;
}
function prompts(list) { return list.map(function (b) { return b.prompt; }); }
function labels(list) { return list.map(function (b) { return b.label; }); }

var CONT_DEFAULTS = prompts(W.CONTINUE_DEFAULTS);

/* 1. No feed and no session list: the three builtins, and nothing invented. */
W.lastGoodDoc = null;
eq(prompts(W.continueButtons(contSess('a'))), CONT_DEFAULTS,
	'with no document at all the builtins are the whole set');
eq(labels(W.continueButtons(contSess('a'))), ['Continue', 'Run the tests', 'Commit + push'],
	'the builtin faces are the short labels, not the wire prompts');

/* 2. ABSENT IS NOT EMPTY. crabd omits sessions[].continuePrompts for a session with
      no project prompts, and an older crabd omits it always: same row either way. */
W.lastGoodDoc = { continuePrompts: ['Open a pull request for this work.'] };
var noOwn = prompts(W.continueButtons(contSess('a')));
eq(noOwn, CONT_DEFAULTS.concat(['Open a pull request for this work.']),
	'a session with no project prompts gets the builtins plus the global extras');
eq(prompts(W.continueButtons(contSess('a', []))), noOwn,
	'an empty per-session list renders exactly what an absent one does');
eq(prompts(W.continueButtons(contSess('a', 'not an array'))), noOwn,
	'a per-session value that is not an array is ignored, not rendered');

/* 3. The session's own prompts come LAST, in the order crabd served them. */
var own = ['Run the ordering tests', 'Take the screenshots'];
eq(prompts(W.continueButtons(contSess('a', own))),
	CONT_DEFAULTS.concat(['Open a pull request for this work.'], own),
	'builtins, then the global extras, then this session\'s project prompts');
eq(labels(W.continueButtons(contSess('a', own))).slice(-2), own,
	'a config-fed prompt is its own button face');

/* 4. TWO SESSIONS IN DIFFERENT REPOS get different sets from the same document -
      the whole feature, at the level the widget owns. */
var sideSet = prompts(W.continueButtons(contSess('side', ['Take the screenshots'])));
var acmeSet = prompts(W.continueButtons(contSess('acme', ['Rebuild the report'])));
ok(sideSet.indexOf('Take the screenshots') !== -1 && sideSet.indexOf('Rebuild the report') === -1,
	'the sidecrab session sees only its own project prompt');
ok(acmeSet.indexOf('Rebuild the report') !== -1 && acmeSet.indexOf('Take the screenshots') === -1,
	'the acme-api session sees only its own project prompt');

/* 5. Defensive on a hand-editable feed: blanks, non-strings and duplicates are
      dropped, and a duplicate of a builtin or a global is dropped as well - the
      same rule crabd applies, kept here because a stale crabd predates it. */
eq(prompts(W.continueButtons(contSess('a', ['  Run the ordering tests  ', null, 7, '',
	'   ', 'Run the ordering tests', 'Open a pull request for this work.', 'Continue']))),
	CONT_DEFAULTS.concat(['Open a pull request for this work.', 'Run the ordering tests']),
	'blanks, non-strings and every kind of duplicate are dropped');

/* 6. THE PROTOTYPE TRAP. A plain {} for the seen-set inherits Object.prototype, so
      seen['constructor'] reads truthy and a prompt with that text would vanish. */
eq(prompts(W.continueButtons(contSess('a', ['constructor', 'toString', 'hasOwnProperty']))),
	CONT_DEFAULTS.concat(['Open a pull request for this work.', 'constructor', 'toString',
		'hasOwnProperty']),
	'a prompt named after an Object.prototype member is not eaten as a duplicate');

/* 7. THE MUTATION PROOF for check 4: the flattened builder - one list for every
      session, which is what "per repo" looks like when the per-session half is
      dropped - must fail it, or check 4 reports success forever. */
function flattenedButtons(s, everyProjectPrompt) {
	var list = W.CONTINUE_DEFAULTS.slice();
	everyProjectPrompt.forEach(function (t) { list.push({ label: t, prompt: t }); });
	return list;
}
var flat = prompts(flattenedButtons(contSess('side', ['Take the screenshots']),
	['Take the screenshots', 'Rebuild the report']));
ok(flat.indexOf('Rebuild the report') !== -1,
	'MUTATION: a flattened builder puts another project\'s prompt on this sheet');
W.lastGoodDoc = null;

/* ---------------------------------------------------------------------- done */

console.log((failures ? 'FAILED' : 'ok') + '  ' + (checks - failures) + '/' + checks + ' checks');
process.exit(failures ? 1 : 0);
