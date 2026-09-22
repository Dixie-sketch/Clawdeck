/* SideCrab widget — the standalone behaviour tests (CLEAN-09, v0.32.0).
 *
 *   node widget/tests/test_standalone.js
 *
 * ONE CHECK PER ACCEPTED FINDING, written as the audit's own acceptance test and
 * run against the shipping file through the two explicit fixtures in fixture.js -
 * the native panel-host page and the served browser preview. The older suites are
 * kept and still run: test_ordering.js owns the pure ordering and preference rules,
 * test_chime.js owns the chime gates. What was missing was a fixture that boots the
 * app that actually ships, and behaviour tests against the transport, the settings,
 * the receipts, the views and the hardware row.
 *
 * MUTATION PROOFS. Several sections end with the pre-fix behaviour run against the
 * same input, to show the check can fail. A test that cannot fail reports success
 * for ever, which is worse than no test.
 */
'use strict';

var fs = require('fs');
var path = require('path');
var F = require('./fixture.js');

var failures = 0, checks = 0;

function ok(cond, what) {
	checks++;
	if (!cond) { failures++; console.log('FAIL  ' + what); }
}

function eq(actual, expected, what) {
	ok(JSON.stringify(actual) === JSON.stringify(expected),
		what + '  (got ' + JSON.stringify(actual) + ', want ' + JSON.stringify(expected) + ')');
}

function iso(ms) { return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z'); }

function stateDoc(atMs, sessions, extra) {
	var d = {
		schema: 5,
		generatedAt: iso(atMs),
		crabd: { version: '0.33.0', startedAt: '2026-09-21T09:00:00Z' },
		sessions: sessions || []
	};
	for (var k in extra) { if (Object.prototype.hasOwnProperty.call(extra, k)) d[k] = extra[k]; }
	return d;
}

function sess(id, state, extra) {
	var s = { id: id, state: state, stateSince: '2026-09-21T11:00:00Z' };
	for (var k in extra) { if (Object.prototype.hasOwnProperty.call(extra, k)) s[k] = extra[k]; }
	return s;
}

/* Peripherals that draw. Stubbed so a test exercises the logic under it rather than
   the whole render tree; every function named here is covered by its own section or
   by one of the other two suites. */
function quiet(ctx, extra) {
	var w = ctx.w;
	var names = ['render', 'sampleHost', 'detectCelebration', 'detectTricks', 'maybeAutoGesture',
		'maybeAutoOpenSheet', 'syncDiag'].concat(extra || []);
	for (var i = 0; i < names.length; i++) w[names[i]] = function () {};
	return w;
}

/* ============================================================ the two fixtures */

(function fixtureContract() {
	var n = F.nativePage();
	eq(n.w.baseUrl(), F.ORIGIN, 'the native page talks to the origin that served it');
	eq(n.w.window.location.href.indexOf(F.PANEL_URL), 0, 'and it is the real /panel/ URL, not /index.html');
	ok(n.w.hostCan('saveSettings'), 'the host said it can save settings');
	ok(n.w.hostCan('focusSession'), 'and that it can focus a session');
	eq(n.w.hostCan('pickDisplay'), false, 'a capability it declined stays declined');

	var p = F.previewPage();
	eq(p.w.baseUrl(), F.ORIGIN, 'the preview talks to the same origin');
	eq(p.w.hostCan('saveSettings'), false, 'and has no native save');
	eq(p.w.hostCan('focusSession'), false, 'and no native focus');
	eq(p.w.laneEAvailable(), false, 'so the bring-to-front control does not exist there');

	/* CLEAN-01. The retired probes read a property off a same-named window global,
	   and failing that off a Function('return NAME') evaluation. Neither may come
	   back: a page global that happens to share a setting's name is not a setting. */
	var g = F.previewPage();
	g.w.clock24 = true;
	g.w.accentColor = '#FF0000';
	eq(g.w.boolProp('clock24', false), false, 'a window global cannot become a setting');
	eq(g.w.strProp('accentColor', '#6F94CC'), '#6F94CC', 'nor can one shadow a colour');

	/* Malformed host metadata fabricates nothing. */
	var bad = F.nativePage({ host: { kind: 'standalone', props: 'not-an-object' }, handshake: false });
	eq(bad.w.strProp('accentColor', '#6F94CC'), '#6F94CC', 'a malformed props object reads as empty');
	eq(bad.w.hostCan('saveSettings'), false, 'and an unanswered handshake grants nothing');
	var wrong = F.nativePage({ host: { kind: 'something-else', props: { clock24: true } }, handshake: false });
	eq(wrong.w.boolProp('clock24', false), false, 'a boot object of the wrong kind is not read');
})();

/* ================================================== SCA-020: snapshot ordering */

(function ordering() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	var t0 = c.now();

	w.acceptDoc(stateDoc(t0, [sess('a', 'working')]));
	eq(w.lastGoodDoc.sessions[0].state, 'working', 'the first document lands');

	/* The startup GET resolves AFTER a newer pushed frame and two seconds behind it. */
	w.acceptDoc(stateDoc(t0 - 2000, [sess('a', 'needs_input')]));
	eq(w.lastGoodDoc.sessions[0].state, 'working', 'a strictly older snapshot never replaces a newer one');
	eq(w.lastGoodAtMs, t0, 'and the freshness stamp does not roll backwards');

	/* EQUAL generatedAt is kept: the companion's timestamps are one-second and it
	   publishes a changed document inside one second. */
	w.acceptDoc(stateDoc(t0, [sess('a', 'done')]));
	eq(w.lastGoodDoc.sessions[0].state, 'done', 'a distinct document at the same generatedAt is kept');

	/* A RESTART may legitimately move the clock backwards. */
	var older = stateDoc(t0 - 60000, [sess('a', 'idle')]);
	older.crabd = { version: '0.33.0', startedAt: '2026-09-21T12:00:00Z' };
	w.acceptDoc(older);
	eq(w.lastGoodDoc.sessions[0].state, 'idle', 'a companion restart resets the ordering baseline');

	/* A CLOCK RESET with no restart: rejected while the panel is fresh, taken once
	   it has been stale for the whole horizon, so the rule cannot freeze a panel. */
	var d = F.nativePage();
	var w2 = quiet(d, ['detectChime']);
	var u0 = d.now();
	w2.acceptDoc(stateDoc(u0, [sess('a', 'working')]));
	w2.acceptDoc(stateDoc(u0 - 90000, [sess('a', 'needs_input')]));
	eq(w2.lastGoodDoc.sessions[0].state, 'working', 'a backwards clock is refused at first');
	d.advance(31000);
	w2.acceptDoc(stateDoc(u0 - 90000, [sess('a', 'needs_input')]));
	eq(w2.lastGoodDoc.sessions[0].state, 'needs_input',
		'and taken once the panel has been stale for the whole horizon, so nothing freezes');

	/* A SUPERSEDED QUESTION NEVER CHIMES. The chime rides acceptDoc, so a document
	   that is dropped cannot reach it. */
	var e = F.nativePage();
	var w3 = quiet(e);
	var rings = 0;
	w3.playChime = function () { rings++; return true; };
	var v0 = e.now();
	w3.acceptDoc(stateDoc(v0, [sess('a', 'working')]));
	w3.acceptDoc(stateDoc(v0 - 2000, [sess('a', 'needs_input')]));
	eq(rings, 0, 'an older waiting snapshot does not chime');
	e.advance(6000);
	w3.acceptDoc(stateDoc(e.now(), [sess('a', 'needs_input')]));
	eq(rings, 1, 'and a genuinely new question still does');

	/* MUTATION: the pre-fix acceptDoc took whatever arrived last. */
	ok(stateDoc(t0 - 2000, []).generatedAt < stateDoc(t0, []).generatedAt,
		'MUTATION: last-write-wins would have taken the older document, which is the defect');
})();

/* ============================== SCA-019: stream liveness and deliberate refresh */

(function liveness() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime', 'showNotice']);
	var gets = 0, closes = 0;
	w.fetch = function () {
		gets++;
		return Promise.resolve({ ok: true, json: function () { return Promise.resolve(stateDoc(c.now(), [sess('a', 'working')])); } });
	};
	w.sseSource = { readyState: 1, close: function () { closes++; } };
	w.transportDiag();
	w.noteTransportEvent();

	ok(w.sseDelivering(), 'a stream that has just spoken is delivering');
	w.poll();
	eq(gets, 0, 'and the fallback poll stays out of its way');

	/* A HEALTHY STREAM STILL ANSWERS A DELIBERATE REFRESH. */
	w.forceRefresh();
	eq(gets, 1, 'a pull against a healthy stream fetches exactly once');

	/* Past the deadline the stream is no longer believed. The in-flight guard has to
	   settle first: a poll already on the wire is deliberately not duplicated. */
	return new Promise(function (r) { setImmediate(r); }).then(function () {
	c.advance(46000);
	eq(w.sseDelivering(), false, 'an OPEN stream that has said nothing past the deadline is not delivering');
	w.poll();
	eq(gets, 2, 'so the fallback poll resumes');
	w.sseLivenessCheck();
	eq(closes, 1, 'the tick closes the stream it stopped believing');
	ok(c.pendingTimers() > 0, 'and schedules a paced reconnect rather than reconnecting at once');

	/* WOULD IT FIRE ON A HEALTHY NIGHT? The companion publishes every 2 s and pings
	   at 15 s, so three consecutive missed pings are needed. Replayed here at the
	   measured cadence over a full ten minutes. */
	var h = F.nativePage();
	var wh = quiet(h, ['detectChime']);
	wh.sseSource = { readyState: 1, close: function () {} };
	wh.transportDiag();
	var fired = 0;
	wh.sseFellBack = function () { fired++; };
	for (var i = 0; i < 300; i++) {
		wh.noteTransportEvent();
		h.advance(2000);
		wh.sseLivenessCheck();
	}
	eq(fired, 0, 'a healthy night at the measured 2 s cadence never trips the deadline');
	/* And a 15 s ping-only night does not either. */
	for (var j = 0; j < 40; j++) {
		wh.noteTransportEvent();
		h.advance(15000);
		wh.sseLivenessCheck();
	}
	eq(fired, 0, 'nor does a quiet night carried by the 15 s ping alone');

	/* A REAL TRANSPORT ERROR still falls back, without duplicating the request. */
	var e = F.nativePage();
	var we = quiet(e, ['detectChime', 'showNotice']);
	var egets = 0;
	we.fetch = function () {
		egets++;
		return Promise.resolve({ ok: true, json: function () { return Promise.resolve(stateDoc(e.now(), [])); } });
	};
	we.sseSource = { readyState: 1, close: function () {} };
	we.transportDiag();
	we.noteTransportEvent();
	we.onSseError({ type: 'error' });
	eq(egets, 1, 'a real error falls back with exactly one request');

	/* SINGLE FLIGHT IS NOT BYPASSABLE. */
	var s = F.nativePage();
	var ws = quiet(s, ['detectChime', 'showNotice']);
	var sgets = 0;
	ws.fetch = function () { sgets++; return new Promise(function () {}); };
	ws.forceRefresh();
	ws.forceRefresh();
	ws.poll(true);
	eq(sgets, 1, 'a poll already on the wire is the answer to the next pull');
	});
})();

/* ==================== SCA-006: a receipt belongs to the surface that started it */

(function receipts() {
	var c = F.nativePage();
	var w = quiet(c, ['fireSnap', 'showNotice']);
	var resolveAck = null, closed = 0;
	w.lastGoodDoc = { sessions: [sess('a', 'needs_input'), sess('b', 'needs_input')] };
	w.postAction = function () { return new Promise(function (r) { resolveAck = r; }); };
	w.closeSheet = function () { closed++; };

	w.sheetSessionId = 'a';
	w.onSheetAction('ack');
	/* The operator switches to B before the answer lands. openSheet is what moves
	   the generation in the app; the fixture moves it the same way. */
	w.sheetGen++;
	w.sheetSessionId = 'b';
	w.setSheetStatus('', '');
	resolveAck({ status: 204, body: null });

	return Promise.resolve().then(function () {
		eq(w.ui.sheetStatus.textContent, '', 'a delayed receipt for A writes nothing onto B');
		c.advance(2000);
		eq(closed, 0, 'and cannot close B by scheduling A\'s close');

		/* The same session, closed and reopened inside one round trip. The id alone
		   would match; the generation is what refuses it. */
		var d = F.nativePage();
		var wd = quiet(d, ['fireSnap', 'showNotice']);
		var resolveCont = null;
		wd.lastGoodDoc = { sessions: [sess('a', 'working')] };
		wd.postAction = function () { return new Promise(function (r) { resolveCont = r; }); };
		wd.sheetSessionId = 'a';
		wd.onSheetContinue('Keep going.', 'Continue');
		wd.sheetGen++;              /* closed */
		wd.sheetGen++;              /* and reopened on the same session */
		wd.setContinueStatus('', '');
		resolveCont({ status: 404, body: null });
		return Promise.resolve().then(function () {
			eq(wd.ui.sheetContinueStatus.textContent, '',
				'a receipt from an earlier visit does not write into the second one');

			/* The positive control: the same receipt, on the surface that asked. */
			var e = F.nativePage();
			var we = quiet(e, ['fireSnap', 'showNotice']);
			var resolveOk = null;
			we.lastGoodDoc = { sessions: [sess('a', 'working')] };
			we.postAction = function () { return new Promise(function (r) { resolveOk = r; }); };
			we.sheetSessionId = 'a';
			we.onSheetContinue('Keep going.', 'Continue');
			resolveOk({ status: 204, body: null });
			return Promise.resolve().then(function () {
				eq(we.ui.sheetContinueStatus.textContent, 'queued: Continue',
					'and the surface that started it does get its answer');

				/* The ack ROLLBACK is session state and must survive the switch: a card
				   left silenced by a write that never landed is the worse failure. */
				var f = F.nativePage();
				var wf = quiet(f, ['fireSnap', 'showNotice']);
				var resolveFail = null;
				wf.lastGoodDoc = { sessions: [sess('a', 'needs_input'), sess('b', 'needs_input')] };
				wf.postAction = function () { return new Promise(function (r) { resolveFail = r; }); };
				wf.sheetSessionId = 'a';
				wf.onSheetAction('ack');
				ok(wf.ackOptimistic.a !== undefined, 'the tap silences the card at once');
				wf.sheetGen++;
				wf.sheetSessionId = 'b';
				resolveFail({ status: 500, body: null });
				return Promise.resolve().then(function () {
					eq(wf.ackOptimistic.a, undefined,
						'a failed ack is rolled back even though the operator has moved on');
				});
			});
		});
	});
})().then(function () {

/* ================================= SCA-007: the visible view owns the tab order */

(function views() {
	var c = F.nativePage();
	var w = quiet(c);
	w.laneCViewsInit = null;

	/* aria-hidden is only WRITTEN when it has to change, so a region that has never
	   been hidden carries no attribute at all - which is the markup's own state for
	   the card grid. "Not hidden" is therefore the absence of both marks. */
	function hidden(el) { return el.getAttribute('aria-hidden') === 'true' && el.hasAttribute('inert'); }
	function shown(el) { return el.getAttribute('aria-hidden') !== 'true' && !el.hasAttribute('inert'); }

	w.viewIdx = 0;
	w.syncViewVisibility();
	ok(shown(w.ui.cards), 'on Sessions the card grid is in the tree and the tab order');
	ok(hidden(w.ui.viewBurn) && hidden(w.ui.viewWeek) && hidden(w.ui.viewDetail),
		'and the three alternate views are out of both');

	w.viewIdx = 1;
	w.syncViewVisibility();
	ok(shown(w.ui.viewBurn), 'a visible Burn page is in the accessibility tree');
	ok(hidden(w.ui.cards), 'and the invisible card grid is out of the tab order');
	ok(hidden(w.ui.gridEmpty), 'including its empty-state line');
	ok(hidden(w.ui.viewWeek) && hidden(w.ui.viewDetail), 'the other two stay hidden');

	w.viewIdx = 3;
	w.syncViewVisibility();
	ok(shown(w.ui.viewDetail) && hidden(w.ui.viewBurn), 'Detail swaps with Burn');

	/* THE NARROW FALLBACK. Below the stylesheet's breakpoint the cards come back
	   whatever the stored view says, so the tab order has to agree with the
	   stylesheet and not with the preference. */
	c.w.innerWidth = 1000;
	w.syncViewVisibility();
	ok(shown(w.ui.cards), 'at a narrow slot the visible view is the cards');
	ok(hidden(w.ui.viewDetail), 'and the stored Detail page is out of the tab order');
	c.w.innerWidth = 2560;
	w.syncViewVisibility();
	ok(shown(w.ui.viewDetail), 'widening restores the stored view');
})();

/* ======================= SCA-025: the header swipe is gated like the switcher */

(function narrowSwipe() {
	function swipe(width, from) {
		var c = F.nativePage({ innerWidth: width });
		var w = quiet(c);
		w.prefsStoreKey = 'fixture';
		w.viewIdx = from;
		w.onHeadPointerDown({ pointerId: 1, clientX: 400, clientY: 20, target: new F.Element('div') });
		w.onHeadPointerMove({ pointerId: 1, clientX: 200, clientY: 20 });
		w.onHeadPointerUp({ pointerId: 1 });
		var saved = c.w.localStorage.getItem('fixture');
		return { view: w.currentView().key, saved: saved ? JSON.parse(saved).gridView : null };
	}
	eq(swipe(1000, 0).view, 'sessions', 'at 1000 px a header swipe does not change the view');
	eq(swipe(1000, 0).saved, null, 'and writes nothing to storage');
	eq(swipe(1660, 1).view, 'burn', 'at the breakpoint itself the stored view survives');
	eq(swipe(1661, 0).view, 'burn', 'at 1661 px the swipe cycles forward');
	eq(swipe(2560, 0).view, 'burn', 'and at 2560 px it still does');
	eq(swipe(2560, 0).saved, 'burn', 'and the choice is persisted');

	/* MUTATION: the ungated handler, which is what shipped. */
	var m = F.nativePage({ innerWidth: 1000 });
	quiet(m);
	m.w.viewIdx = 0;
	m.w.stepGridView(1);
	eq(m.w.currentView().key, 'burn',
		'MUTATION: the ungated step does change the view at a narrow slot, which is the defect');
})();

/* ====================================== SCA-008: the settings controls are named */

(function settingsNames() {
	var c = F.nativePage();
	var w = quiet(c);
	w.settingsDraft = { clock24: false, chime: true, chimeVolume: 60, accentColor: '#6F94CC' };

	var row = w.settingsToggleRow('clock24', '24-hour clock', w.settingsDraft);
	var label = row.children[0], btn = row.children[1];
	eq(label.textContent, '24-hour clock', 'the row shows its label');
	ok(label.id && btn.id, 'both carry ids');
	eq(btn.getAttribute('aria-labelledby'), label.id + ' ' + btn.id,
		'and the toggle is named by its label and its state face');
	eq(btn.textContent, 'Off', 'the face is still the state');
	eq(btn.getAttribute('aria-pressed'), 'false', 'and the state is on aria-pressed too');

	/* Two rows must not share an id, or one label would name both controls. */
	var second = w.settingsToggleRow('chime', 'Chime on a new question', w.settingsDraft);
	ok(second.children[0].id !== label.id, 'a second row gets its own label id');

	var swatch = w.settingsColorRow(['accentColor', 'Accent', '#6F94CC', ['#6F94CC']], w.settingsDraft);
	var sw = swatch.children[1].children[0];
	eq(sw.getAttribute('aria-label'), 'Accent #6F94CC', 'a swatch is named with the visible label');

	/* The dialog is named for the mode it is in. */
	var panel = new F.Element('div');
	panel.classList.add('sheet-panel');
	w.ui.sheet.appendChild(panel);
	w.sheetMode = 'settings';
	w.setSheetLabel();
	eq(panel.getAttribute('aria-label'), 'Panel settings', 'the settings sheet announces Panel settings');
	w.sheetMode = 'session';
	w.setSheetLabel();
	eq(panel.getAttribute('aria-label'), 'Session actions', 'and a session sheet its own name');
	w.sheetMode = 'host';
	w.setSheetLabel();
	eq(panel.getAttribute('aria-label'), "This PC's hardware history", 'and the hardware sheet its own');
})();

/* ============================ SCA-012: the ten-minute hardware history horizon */

(function ring() {
	function run(stepMs, spanMs) {
		var c = F.nativePage();
		/* sampleHost is the subject here, so it is NOT among the stubs. It renders
		   nothing: hostPct and the lane A readers are pure. */
		var w = c.w;
		w.render = function () {};
		var n = Math.round(spanMs / stepMs);
		for (var i = 0; i <= n; i++) {
			w.sampleHost({ host: { cpuPct: 10, memPct: 50, gpu: { available: true, utilPct: 10, tempC: 50 },
				load: { diskReadBps: 1, diskWriteBps: 1, netRxBps: 1, netTxBps: 1 } } });
			c.advance(stepMs);
		}
		var ring = w.hostRing;
		return {
			count: ring.length,
			spanSec: (ring[ring.length - 1].t - ring[0].t) / 1000,
			laneCount: w.laneARing.length,
			laneSpanSec: (w.laneARing[w.laneARing.length - 1].t - w.laneARing[0].t) / 1000
		};
	}
	var fast = run(2000, 600000);
	ok(Math.abs(fast.spanSec - 600) <= 2, 'at the 2 s stream cadence the ring holds ten minutes (' + fast.spanSec + 's)');
	ok(Math.abs(fast.laneSpanSec - 600) <= 2, 'and so does the second ring (' + fast.laneSpanSec + 's)');
	var slow = run(3000, 600000);
	ok(Math.abs(slow.spanSec - 600) <= 3, 'at the 3 s poll it holds ten minutes too (' + slow.spanSec + 's)');

	/* The memory cap is a backstop and THINS rather than truncating, so a cadence
	   faster than any documented one loses resolution and not the horizon. */
	var quick = run(500, 600000);
	ok(quick.count <= 400, 'a faster-than-documented cadence is still bounded (' + quick.count + ' samples)');
	ok(quick.spanSec >= 560, 'and keeps very nearly the whole horizon (' + quick.spanSec + 's)');

	/* MUTATION: the pre-fix trim dropped the oldest sample to satisfy the count. */
	var c = F.nativePage();
	var w = c.w;
	var mut = [];
	for (var i = 0; i <= 300; i++) { mut.push({ t: c.now() + i * 2000, cpu: 10, mem: 50 }); }
	while (mut.length && (mut[0].t < c.now() + 600000 - 600000 || mut.length > 260)) mut.shift();
	var mutSpan = (mut[mut.length - 1].t - mut[0].t) / 1000;
	ok(mutSpan < 560, 'MUTATION: the old count-first trim held ' + mutSpan + 's of a ten-minute chart');
})();

/* ============================== SCA-013: the Burn view repaints on every field */

(function burnCache() {
	function paint(mutate) {
		var c = F.nativePage();
		var w = quiet(c);
		var doc = {
			sessions: [sess('a', 'working', { title: 'Old title', model: 'claude-opus-5', todayOutputTokens: 10 })],
			burn: { today: { outputTokens: 10, inputTokens: 100, messages: 1 }, hourly: [], byModel: [] }
		};
		w.renderBurnView(doc);
		var before = w.ui.viewBurn.textContent;
		mutate(doc);
		w.renderBurnView(doc);
		return { before: before, after: w.ui.viewBurn.textContent };
	}
	var title = paint(function (d) { d.sessions[0].title = 'New title'; });
	ok(title.after !== title.before && title.after.indexOf('New title') >= 0,
		'a changed session title repaints the Burn view');
	var input = paint(function (d) { d.burn.today.inputTokens = 999999; });
	ok(input.after !== input.before, "a changed input total repaints it");
	var out = paint(function (d) { d.burn.today.outputTokens = 777; });
	ok(out.after !== out.before, 'and so does a changed output total');
	var msgs = paint(function (d) { d.burn.today.messages = 42; });
	ok(msgs.after !== msgs.before, 'and the message count');
	var none = paint(function () {});
	eq(none.after, none.before, 'an unchanged document still costs no rebuild');
})();

/* ================== SCA-018: an unavailable feed is not an empty session list */

(function absentFeed() {
	['burn', 'week', 'detail'].forEach(function (key) {
		var c = F.nativePage();
		var w = quiet(c, ['trackViewAlerts', 'syncViewChips', 'syncViewVisibility']);
		w.viewIdx = w.prefIndexOrNone(w.VIEWS, key);
		w.everHadData = false;
		w.laneCRenderViews(null, [], 'connecting', false);
		var el = w.ui[key === 'burn' ? 'viewBurn' : key === 'week' ? 'viewWeek' : 'viewDetail'];
		var text = el.textContent;
		ok(text.indexOf('No active Claude sessions') < 0,
			'a saved ' + key + ' view never claims an empty session list on an absent feed');
		ok(text.indexOf('companion') >= 0, 'it names the companion instead (' + key + ')');
	});

	/* An UNSUPPORTED schema is a different sentence from an absent companion. */
	var u = F.nativePage();
	var wu = quiet(u, ['trackViewAlerts', 'syncViewChips', 'syncViewVisibility']);
	wu.acceptDoc({ schema: 99, generatedAt: iso(u.now()) });
	ok(wu.feedUnreadable, 'a schema above the ceiling is recorded as unreadable');
	wu.viewIdx = 3;
	wu.laneCRenderViews(null, [], 'connecting', false);
	ok(wu.ui.viewDetail.textContent.indexOf('cannot read') >= 0,
		'and the view says the feed cannot be read, not that there are no sessions');

	/* RECOVERY. A valid empty feed does say the sessions are empty. */
	var r = F.nativePage();
	var wr = quiet(r, ['trackViewAlerts', 'syncViewChips', 'syncViewVisibility', 'detectChime']);
	wr.viewIdx = 3;
	wr.laneCRenderViews(null, [], 'connecting', false);
	wr.acceptDoc(stateDoc(r.now(), []));
	eq(wr.feedUnreadable, false, 'a readable document clears the unreadable latch');
	wr.laneCRenderViews(wr.lastGoodDoc, [], 'live', false);
	ok(wr.ui.viewDetail.textContent.indexOf('No active Claude sessions') >= 0,
		'and a genuinely empty feed is allowed to say so');

	/* The timeline is inert while nothing has arrived, so a stray tap on the
	   mostly-empty connecting header cannot open a day nobody measured. */
	var t = F.nativePage();
	var wt = quiet(t);
	wt.everHadData = false;
	wt.openTimelineSheet();
	eq(wt.sheetMode, null, 'the timeline does not open before a document has arrived');
})();

/* ============================================ SCA-026: muted is not a broken host */

(function testChime() {
	function press(volume, withAudio) {
		var c = F.nativePage();
		var w = quiet(c);
		var notes = 0;
		w.chimeAudio = function () { return withAudio ? { currentTime: 0 } : null; };
		w.chimeNote = function () { notes++; };
		w.settingsDraft = { chimeVolume: volume };
		var actions = w.settingsActions();
		actions.children[0].fire('click');
		return { text: w.ui.setStatus.textContent, kind: w.ui.setStatus.getAttribute('data-kind'), notes: notes };
	}
	var zero = press(0, true);
	ok(zero.text.indexOf('muted') >= 0, 'zero volume reports muted');
	ok(zero.text.indexOf('no audio') < 0, 'and never claims the host has no audio');
	eq(zero.kind, 'note', 'it is a note, not an error');
	eq(zero.notes, 0, 'and nothing is scheduled');

	var on = press(20, true);
	ok(on.text.indexOf('played at 20%') >= 0, 'a positive volume attempts playback');
	eq(on.notes, 2, 'and schedules the two notes');

	var deaf = press(20, false);
	ok(deaf.text.indexOf('no audio output on this host') >= 0,
		'an unavailable context keeps its own specific message');

	/* Zero on a host with no audio is still MUTED: that is the fact the operator set. */
	var both = press(0, false);
	ok(both.text.indexOf('muted') >= 0, 'zero volume wins over an absent device');
})();

/* ======================================= SCA-027: a requested override is pending */

(function pendingQuiet() {
	var c = F.nativePage();
	var w = quiet(c, ['fireSnap', 'showNotice']);
	var rings = 0;
	w.playChime = function () { rings++; return true; };
	w.postAction = function () { return new Promise(function () {}); };   /* held open */
	w.everHadData = true;
	w.lastGoodAtMs = c.now();
	w.lastGoodDoc = { quiet: { active: false }, sessions: [sess('one', 'working')] };
	w.chimePrevStates = { one: 'working' };

	w.sendQuietOverride('on');
	var st = w.quietState();
	eq(st.pending, true, 'the requested override reads as pending');
	w.renderMoonChip('live');
	eq(w.ui.moonMode.textContent, 'pending', 'and the chip says so rather than claiming quiet');
	eq(w.ui.moonChip.getAttribute('data-quiet'), 'pending', 'including in its attribute');
	ok(String(w.ui.moonChip.getAttribute('aria-label')).indexOf('requested') >= 0,
		'the label says what was asked for');

	/* THE CONSERVATIVE HALF: a pending QUIET request holds the chime. */
	c.advance(100);
	w.detectChime({ quiet: { active: false }, sessions: [sess('one', 'needs_input')] });
	eq(rings, 0, 'an alert while quiet is pending is held rather than sounded');

	/* And there is no optimistic UNMUTE against a confirmed quiet period. */
	var d = F.nativePage();
	var wd = quiet(d, ['fireSnap', 'showNotice']);
	var dRings = 0;
	wd.playChime = function () { dRings++; return true; };
	wd.postAction = function () { return new Promise(function () {}); };
	wd.everHadData = true;
	wd.lastGoodAtMs = d.now();
	wd.lastGoodDoc = { quiet: { active: true }, sessions: [sess('one', 'working')] };
	wd.chimePrevStates = { one: 'working' };
	wd.sendQuietOverride('off');
	wd.detectChime({ quiet: { active: true }, sessions: [sess('one', 'needs_input')] });
	eq(dRings, 0, 'a pending awake request does not unmute a confirmed quiet period');

	/* The feed settles it either way. */
	var e = F.nativePage();
	var we = quiet(e, ['fireSnap', 'showNotice']);
	we.postAction = function () { return Promise.resolve({ status: 204, body: null }); };
	we.everHadData = true;
	we.lastGoodAtMs = e.now();
	we.lastGoodDoc = { quiet: { active: false }, sessions: [] };
	we.sendQuietOverride('on');
	we.lastGoodAtMs = e.now() + 1000;
	we.lastGoodDoc = { quiet: { active: true, override: { mode: 'on', until: iso(e.now() + 3600000) } }, sessions: [] };
	var settled = we.quietState();
	eq(settled.pending, false, 'a newer document settles the request');
	eq(settled.mode, 'on', 'and the feed states the mode');
})();

/* ============================= SCA-021 + C2: every pending bridge state ends */

(function bridge() {
	var c = F.nativePage();
	var w = quiet(c);
	w.hostInfo = w.hostInfo;   /* set by the fixture's handshake */

	/* A SAVE THE HOST NEVER ANSWERS ends by itself. */
	w.settingsDraft = { clock24: true };
	w.onSettingsSave();
	eq(w.ui.setStatus.textContent, 'saving', 'the save says it is in flight');
	c.advance(9000);
	eq(w.ui.setStatus.textContent, 'saving', 'and is still in flight inside the deadline');
	c.advance(2000);
	ok(w.ui.setStatus.textContent.indexOf('no answer from the host') >= 0,
		'past the deadline it says the host did not answer');
	ok(w.ui.setStatus.textContent.indexOf('nothing was saved') >= 0,
		'and that nothing was saved');

	/* AN EXPLICIT FAILURE is a different sentence again. */
	var d = F.nativePage();
	var wd = quiet(d);
	wd.settingsDraft = { clock24: true };
	wd.onSettingsSave();
	var ask = d.sent[d.sent.length - 1];
	eq(ask.type, 'settings', 'the save goes as a settings message');
	ok(typeof ask.requestId === 'string' && ask.requestId.length <= 64, 'with a requestId of at most 64 characters');
	d.reply({ type: 'settings-result', requestId: ask.requestId, ok: false, error: 'access denied' });
	ok(wd.ui.setStatus.textContent.indexOf('access denied') >= 0, 'a failed write is said out loud');
	ok(wd.ui.setStatus.textContent.indexOf('not saved') >= 0, 'and is not reported as saved');

	/* A LATE PRIOR RESULT MUST NOT REPLACE THE NEXT ATTEMPT. */
	var e = F.nativePage();
	var we = quiet(e);
	we.settingsDraft = { clock24: true };
	we.onSettingsSave();
	var first = e.sent[e.sent.length - 1];
	we.onSettingsSave();
	var second = e.sent[e.sent.length - 1];
	ok(first.requestId !== second.requestId, 'a second attempt carries its own requestId');
	e.reply({ type: 'settings-result', requestId: first.requestId, ok: false, error: 'stale failure' });
	ok(we.ui.setStatus.textContent.indexOf('stale failure') < 0,
		'a late answer to the superseded attempt is dropped');
	eq(we.ui.setStatus.textContent, 'saving', 'and the newer attempt is still in flight');
	e.reply({ type: 'settings-result', requestId: second.requestId, ok: true, props: { clock24: true } });
	eq(we.ui.setStatus.textContent, 'saved', 'while its own answer lands');

	/* A SUCCESSFUL SAVE MOVES THE LIVE SETTINGS, and only what the host echoed. */
	var f = F.nativePage({ props: { clock24: false } });
	var wf = quiet(f);
	eq(wf.boolProp('clock24', false), false, 'the boot setting is what the host injected');
	wf.settingsDraft = { clock24: true, chime: false };
	wf.onSettingsSave();
	var ask2 = f.sent[f.sent.length - 1];
	f.reply({ type: 'settings-result', requestId: ask2.requestId, ok: true, props: { clock24: true } });
	eq(wf.boolProp('clock24', false), true, 'what the host stored is what the page now reads');
	eq(wf.boolProp('chime', true), true, 'and a key the host did not echo is not assumed');

	/* A FOCUS REQUEST WITH NO REPLY ends too. */
	var g = F.nativePage();
	var wg = quiet(g);
	wg.lastGoodDoc = { sessions: [sess('s1', 'working', { title: 'A session' })] };
	wg.sheetSessionId = 's1';
	wg.laneESendFocus();
	eq(wg.laneEStatusText, 'bringing it to the front', 'the focus line says it is in flight');
	g.advance(11000);
	eq(wg.laneEStatusText, 'no answer from the host', 'and ends at the deadline');
	eq(wg.laneEPending, null, 'with no pending state left behind');

	/* THE BOOT ORDER, which the recheck of this diff caught: the focus row is built
	   at boot AND again when the handshake lands, because at boot the host has not
	   yet said it can focus a window. Without the second call the control would
	   never exist at all. */
	var r = F.nativePage({ handshake: false });
	var wr = quiet(r);
	wr.ui.sheetPin = new F.Element('button');
	var pinRow = new F.Element('div');
	var pinHolder = new F.Element('div');
	pinHolder.appendChild(pinRow);
	pinRow.appendChild(wr.ui.sheetPin);
	eq(wr.laneEAvailable(), false, 'before the handshake there is no focus capability');
	wr.laneEInit();
	eq(pinHolder.children.length, 1, 'so boot builds no focus row');
	r.bridgeBoot();
	eq(pinHolder.children.length, 2, 'and the handshake landing builds it');
	wr.laneEInit();
	eq(pinHolder.children.length, 2, 'a second call builds nothing twice');

	/* THE PREVIEW SAYS SO INSTEAD OF OFFERING. */
	var p = F.previewPage();
	var wp = quiet(p);
	wp.settingsDraft = { clock24: true };
	wp.onSettingsSave();
	ok(wp.ui.setStatus.textContent.indexOf('needs the SideCrab panel host') >= 0,
		'a browser preview says saving needs the panel host');
	wp.renderSettingsFoot();
	ok(wp.ui.setFoot.textContent.indexOf('browser preview') >= 0,
		'and the sheet foot names the surface it is on');

	/* A HOST THAT HAS NOT ANSWERED YET is a third state and not the same sentence. */
	var q = F.nativePage({ handshake: false });
	var wq = quiet(q);
	wq.renderSettingsFoot();
	eq(wq.ui.setFoot.textContent, 'waiting for the panel host',
		'a bridge with no handshake says it is waiting, not that there is no host');
	wq.settingsDraft = { clock24: true };
	wq.onSettingsSave();
	ok(wq.ui.setStatus.textContent.indexOf('has not answered') >= 0,
		'and a save before the handshake is refused with that reason');
})();

/* ======================================== the hardware row: five honest states */

(function hardware() {
	function row(host) {
		var c = F.nativePage();
		var w = quiet(c);
		w.renderHost(host);
		w.renderHostExtras(host);
		w.syncSensorRow();
		return {
			cpuShown: w.ui.sensorCpu.classList.contains('shown'),
			gpuShown: w.ui.sensorGpu.classList.contains('shown'),
			memShown: w.ui.hostMem.classList.contains('shown'),
			cpuText: w.ui.sensorCpuVal.textContent,
			gpuText: w.ui.sensorGpuVal.textContent,
			gpuStale: w.ui.sensorGpuVal.classList.contains('stale'),
			rowShown: w.ui.sensors.classList.contains('shown')
		};
	}
	var absent = row(null);
	eq(absent.rowShown, false, 'no host block means no row at all');
	eq(absent.cpuText, '', 'and no number invented for it');

	var nulls = row({ cpuPct: null, memPct: null, gpu: { available: false } });
	eq(nulls.cpuShown, false, 'a null cpuPct hides the cell rather than printing 0%');
	eq(nulls.memShown, false, 'and a null memPct hides memory');

	var present = row({ cpuPct: 31, memPct: 58, memUsedGB: 18.5, memTotalGB: 32,
		gpu: { available: true, tempC: 61, utilPct: 22, name: 'A GPU', sampledAt: iso(1789984800000) } });
	eq(present.cpuShown, true, 'a present cpuPct shows the CPU cell');
	eq(present.memShown, true, 'and a present memPct the memory cell');
	eq(present.gpuShown, true, 'and an available GPU its own');
	eq(present.gpuText, '61°', 'the GPU temperature paints in Celsius without the letter');
	eq(present.gpuStale, false, 'and a fresh sample is not dimmed');

	var stale = row({ cpuPct: 31, memPct: 58,
		gpu: { available: true, tempC: 61, utilPct: 22, name: 'A GPU', sampledAt: iso(1789984800000 - 120000) } });
	eq(stale.gpuStale, true, 'a sample older than the staleness horizon is dimmed');
	eq(stale.gpuText, '61°', 'and keeps its last reading rather than blanking');

	/* RECOVERED: the cell comes back and the dimming goes with it. */
	var c = F.nativePage();
	var w = quiet(c);
	w.renderHostExtras({ gpu: { available: true, tempC: 61, utilPct: 22, sampledAt: iso(c.now() - 120000) } });
	w.syncSensorRow();
	ok(w.ui.sensorGpuVal.classList.contains('stale'), 'a stale GPU reading is dimmed');
	w.renderHostExtras({ gpu: { available: true, tempC: 55, utilPct: 18, sampledAt: iso(c.now()) } });
	w.syncSensorRow();
	eq(w.ui.sensorGpuVal.classList.contains('stale'), false, 'and a fresh one clears the dimming');
	eq(w.ui.sensorGpuVal.textContent, '55°', 'with the new reading');

	/* GONE: what this block wrote, this block clears. */
	w.renderHostExtras({ gpu: { available: false } });
	w.syncSensorRow();
	eq(w.ui.sensorGpuVal.textContent, '', 'a GPU that goes away takes its number with it');
})();

/* ======================================================= MF-002: cancel a queue */

(function cancel() {
	function press(status, body) {
		var c = F.nativePage();
		var w = quiet(c);
		w.lastGoodDoc = { sessions: [sess('a', 'working', { queuedContinue: { prompt: 'Keep going.' } })] };
		w.sheetSessionId = 'a';
		var sent = null;
		w.postAction = function (id, action) { sent = { id: id, action: action }; return Promise.resolve({ status: status, body: body }); };
		w.onCancelContinue();
		return Promise.resolve().then(function () {
			return { sent: sent, text: w.ui.sheetContinueStatus.textContent };
		});
	}
	return press(204, null).then(function (r) {
		eq(r.sent, { id: 'a', action: 'cancel-continue' }, 'Cancel posts cancel-continue for that session');
		eq(r.text, 'cancelled', 'and 204 says it was removed');
		return press(409, { error: 'already delivered', deliveredAt: '2026-09-21T13:45:00Z' });
	}).then(function (r) {
		ok(r.text.indexOf('already sent at') >= 0, '409 says it had already been delivered');
		ok(/\d{1,2}:\d{2}/.test(r.text), 'and names the time');
		return press(404, null);
	}).then(function (r) {
		eq(r.text, 'nothing queued', '404 says there was nothing queued');
	});
})().then(function () {

/* ================================================== MF-008 and MF-017: presence */

(function sourcesAndReadiness() {
	var c = F.nativePage();
	var w = quiet(c);
	w.lastGoodDoc = stateDoc(c.now(), [], {
		sources: {
			hooks: { ok: true, ageSec: 3 },
			hwinfo: { ok: false, ageSec: 400, note: 'HWiNFO shared memory is not open' }
		}
	});
	w.appendSourcesBlock();
	var text = w.ui.sheetHost.textContent;
	ok(text.indexOf('Sources') >= 0, 'the sources block renders when the feed carries one');
	ok(text.indexOf('hooks') >= 0 && text.indexOf('HWiNFO shared memory is not open') >= 0,
		"and shows the companion's own note verbatim");
	ok(text.indexOf('statusline') < 0, 'a key the block omits is not invented');

	var none = F.nativePage();
	var wn = quiet(none);
	wn.lastGoodDoc = stateDoc(none.now(), []);
	wn.appendSourcesBlock();
	eq(wn.ui.sheetHost.textContent, '', 'a companion that serves no sources renders nothing');

	/* MF-017. Four states, four sentences, and nothing at all when the companion
	   does not state a readiness. */
	function ready(state) {
		var f = F.nativePage();
		var wf = quiet(f);
		wf.lastGoodDoc = stateDoc(f.now(), [], { approvals: { enabled: true, tokenRequired: true, readiness: state } });
		wf.renderApprovalReadiness();
		return { text: wf.ui.sheetApprovalReady.textContent, shown: wf.ui.sheetApprovalReady.classList.contains('shown') };
	}
	ok(ready('no-token').text.indexOf('installer') >= 0, 'no-token sends the operator to the installer');
	ok(ready('unverified').text.indexOf('not paired') >= 0, 'unverified says the panel is not paired');
	ok(ready('off').text.indexOf('off') >= 0, 'off says approvals are off');
	eq(ready('ready').shown, false, 'a ready panel needs no line about itself');
	var absent = F.nativePage();
	var wa = quiet(absent);
	wa.lastGoodDoc = stateDoc(absent.now(), []);
	wa.renderApprovalReadiness();
	eq(wa.ui.sheetApprovalReady.classList.contains('shown'), false,
		'and a companion that states no readiness gets no line');

	/* The pairing code is VERIFIED once per boot and NEVER rendered. */
	var v = F.nativePage({ pairingCode: 'SECRET-CODE' });
	var wv = quiet(v, ['detectChime']);
	var posts = [];
	wv.postJson = function (p, body) { posts.push({ path: p, body: body }); return Promise.resolve({ status: 204, body: null }); };
	wv.acceptDoc(stateDoc(v.now(), [], { approvals: { enabled: true, tokenRequired: true, readiness: 'unverified' } }));
	eq(posts.length, 1, 'an unverified readiness verifies once');
	eq(posts[0].path, '/v1/approvals/verify', 'against the verify route');
	eq(JSON.parse(posts[0].body).code, 'SECRET-CODE', 'carrying the host-injected code');
	wv.acceptDoc(stateDoc(v.now() + 2000, [], { approvals: { enabled: true, tokenRequired: true, readiness: 'unverified' } }));
	eq(posts.length, 1, 'and never again in the same page load');

	var never = F.nativePage({ pairingCode: 'SECRET-CODE' });
	var wnv = quiet(never, ['detectChime']);
	var nposts = 0;
	wnv.postJson = function () { nposts++; return Promise.resolve({ status: 204, body: null }); };
	wnv.acceptDoc(stateDoc(never.now(), [], { approvals: { enabled: true, tokenRequired: true, readiness: 'ready' } }));
	eq(nposts, 0, 'a companion that is already ready is not asked to verify');
})();

/* ========================================= MF-001: only the changed keys are sent */

(function config() {
	function sheet(feedExtra) {
		var c = F.nativePage();
		var w = quiet(c);
		w.lastGoodDoc = stateDoc(c.now(), [], feedExtra || {});
		w.everHadData = true;
		w.cfgDraft = null;
		w.cfgTouched = null;
		return w;
	}
	var w = sheet({ quiet: { start: '22:00', end: '07:00', active: false },
		toast: { enabled: true, thresholdSec: 120, approvalThresholdSec: 15 } });
	var seeded = w.configValues();
	eq(seeded.quietStart, '22:00', 'the controls seed from the feed');
	eq(seeded.toastSec, 120, 'including the toast threshold');

	eq(w.configPayload().any, false, 'a sheet nobody touched sends nothing at all');

	w.configSet('toastEnabled', false);
	var body = w.configPayload().body;
	eq(Object.keys(body), ['toast'], 'one changed key sends one key');
	eq(body.toast, { thresholdSec: 120, enabled: false },
		'and both required toast members ride, because the contract requires both');
	ok(!Object.prototype.hasOwnProperty.call(body.toast, 'approvalThresholdSec'),
		'an untouched approvalThresholdSec is OMITTED, so the companion preserves what is on disk');

	w.configSet('approvalSec', 45);
	eq(w.configPayload().body.toast.approvalThresholdSec, 45,
		'and it rides once it has actually been moved');

	/* Turning quiet hours off CLEARS the key, which is a different statement from
	   not sending it. */
	var q = sheet({ quiet: { start: '22:00', end: '07:00', active: false } });
	q.configSet('quietEnabled', false);
	eq(q.configPayload().body.quietHours, null, 'switching quiet hours off clears the key');

	var q2 = sheet({});
	q2.configSet('quietEnabled', true);
	q2.configSet('quietStart', '9:05');
	eq(q2.configPayload().body.quietHours, { start: '09:05', end: '07:00' },
		'a single-digit hour is padded rather than refused');
	q2.configSet('quietStart', 'later');
	var bad = q2.configPayload();
	eq(bad.warnings.length, 1, 'an unparseable time is a warning');
	ok(!bad.body.quietHours, 'and nothing is sent for that key');

	var b = sheet({});
	b.configSet('budgetEnabled', true);
	b.configSet('budgetK', 5000);
	eq(b.configPayload().body.budget, { dailyOutputTokens: 5000000 }, 'the budget slider is in thousands');
	b.configSet('budgetEnabled', false);
	eq(b.configPayload().body.budget, null, 'and switching it off clears it');

	var d = sheet({});
	d.configSet('digestEnabled', true);
	d.configSet('digestTime', '08:30');
	eq(d.configPayload().body.digest, { enabled: true, time: '08:30' }, 'the digest sends both members');

	/* THE REPLY is shown, warnings and all. */
	var r = sheet({ toast: { enabled: true, thresholdSec: 120 } });
	r.configSet('toastEnabled', false);
	r.postConfig = function () {
		return Promise.resolve({ status: 200, body: { applied: { toast: { enabled: false } }, warnings: ['digest ignored: unknown key'] } });
	};
	r.onConfigSave();
	return Promise.resolve().then(function () {
		ok(r.ui.cfgStatus.textContent.indexOf('saved') >= 0, 'an accepted write says so');
		ok(r.ui.cfgStatus.textContent.indexOf('digest ignored') >= 0,
			"and repeats the companion's own warning rather than swallowing it");
	});
})().then(function () {

/* ============================================ the page itself (CLEAN-07/09) */

(function html() {
	var src = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');

	/* THE TITLE IS STATIC AND CORRECT BEFORE ANY SCRIPT RUNS. The retired host
	   wanted a tr() call here and the real title was written by script after first
	   paint, so a browser tab read the source string until it ran. */
	var title = /<title>([^<]*)<\/title>/.exec(src);
	ok(!!title, 'the page has a title');
	eq(title[1], 'SideCrab', 'and it is SideCrab before any script runs');

	/* The vendor checks run against the MARKUP, with comments stripped: the head
	   comment explains what was retired, and naming a thing is not shipping it. */
	var markup = src.replace(/<!--[\s\S]*?-->/g, '');
	eq(/vendor metadata/.test(markup), false, 'no vendor metadata is left in the markup');
	eq(/\btr\('/.test(markup), false, 'and no vendor translation wrappers');
	eq(/the old packaging toolApiWrapper|the vendor API wrapper|window\.plugins/.test(markup), false,
		'and no inline vendor API wrappers');
	eq(/CDATA/.test(markup), false, 'and no CDATA section, which existed only for the strict-XML parse');
	eq(/<script\b(?![^>]*\bsrc=)/i.test(markup), false, 'and no inline script at all');
	eq(fs.existsSync(path.join(__dirname, '..', 'manifest.json')), false, 'the widget manifest is gone');
	eq(fs.existsSync(path.join(__dirname, '..', 'translation.json')), false, 'and the vendor translation file');

	/* THE VERSION IS A FILE OF ITS OWN (C1). */
	var v = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'version.json'), 'utf8'));
	ok(/^\d+\.\d+\.\d+$/.test(v.version), 'version.json states a version (' + v.version + ')');

	/* AN HTML PARSE TEST, replacing the strict-XML constraint it retires. Comments
	   and the contents of script and style elements are removed first, then every
	   remaining element is pushed and popped: a mismatch is an unclosed or
	   misnested tag, which is the one thing the old XML gate was worth. */
	var body = src
		.replace(/<!--[\s\S]*?-->/g, '')
		.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '')
		.replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, '')
		.replace(/<!DOCTYPE[^>]*>/i, '');
	var VOID = { area: 1, base: 1, br: 1, col: 1, embed: 1, hr: 1, img: 1, input: 1,
		link: 1, meta: 1, param: 1, source: 1, track: 1, wbr: 1 };
	var stack = [], mismatch = null, re = /<\/?([a-zA-Z][a-zA-Z0-9-]*)\b[^>]*?(\/?)>/g, m;
	while ((m = re.exec(body)) !== null) {
		var name = m[1].toLowerCase();
		var closing = m[0].charAt(1) === '/';
		if (VOID[name] || m[2] === '/') continue;
		if (!closing) { stack.push(name); continue; }
		if (stack[stack.length - 1] !== name) { mismatch = name + ' closed while ' + stack[stack.length - 1] + ' was open'; break; }
		stack.pop();
	}
	eq(mismatch, null, 'every element in the page is closed in the order it was opened');
	eq(stack, [], 'and nothing is left open at the end of the document');

	/* The page still references the runtime assets the companion allowlists. */
	ok(/scripts\/sidecrab\.js/.test(src), 'the page loads the runtime script');
	ok(/styles\/sidecrab\.css/.test(src), 'and the stylesheet');
	ok(/resources\/icon\.svg/.test(src), 'and keeps the product icon');
})();

console.log((failures ? 'FAILED' : 'ok') + '  ' + (checks - failures) + '/' + checks + ' checks');
process.exit(failures ? 1 : 0);

}); }); });
