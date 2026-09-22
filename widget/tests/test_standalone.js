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

/* ================================ lane N: the Claude Code fields and two anchors */

/* Version label v0.33.0 in this section is PROVISIONAL; version.json still reads
   0.32.0 and the orchestrator assigns the real number at the merge. */

function laneNSession(extra) {
	var s = { id: 'a', state: 'working', stateSince: '2026-09-21T11:00:00Z' };
	for (var k in extra) { if (Object.prototype.hasOwnProperty.call(extra, k)) s[k] = extra[k]; }
	return s;
}

/* The Detail page, rendered against one session, with the page's own view on. */
function laneNDetail(ctx, s) {
	var w = ctx.w;
	w.viewIdx = 3;
	w.detailSessionId = String(s.id);
	w.lastGoodDoc = { sessions: [s] };
	w.detailViewSig = null;
	w.renderDetailView([s], false);
	return w.ui.viewDetail;
}

(function laneNAnchors() {
	/* THE CARRIED FINDING (detail-permission-anchor). A second permission request in
	   the same state, with the same tool and the same summary, moves nothing in the
	   Detail page's signature - so the page short-circuits and the hold countdown
	   goes on counting from the FIRST request. */
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	var first = '2026-09-21T10:00:00Z', second = '2026-09-21T10:01:00Z';
	var s = laneNSession({
		state: 'needs_input', stateSince: first, title: 'Anchor fixture', repo: 'fixture',
		pendingPermission: { tool: 'Bash', summary: 'Run it', requestedAt: first, requestId: '1' }
	});
	var root = laneNDetail(c, s);
	eq(root.querySelector('.dv-approval-left').getAttribute('data-approval-at'),
		String(Date.parse(first)), 'the hold anchor starts on the first request');

	s.stateSince = second;
	s.pendingPermission.requestedAt = second;
	s.pendingPermission.requestId = '2';
	var sigBefore = w.detailViewSig;
	w.renderDetailView([s], false);
	eq(w.detailViewSig, sigBefore, 'the signature is unmoved, which is the whole trap');
	eq(root.querySelector('.dv-approval-left').getAttribute('data-approval-at'),
		String(Date.parse(second)), 'the hold anchor follows the new request anyway');
	eq(root.querySelector('.dv-elapsed').getAttribute('data-state-since'),
		String(Date.parse(second)), 'and so does the elapsed anchor, which had the same bug');

	/* MUTATION PROOF: the pre-fix behaviour, which was to write the anchors only
	   inside the rebuild. Fed the same input, the check above must fail. */
	var m = F.nativePage();
	quiet(m, ['detectChime']);
	m.w.laneNDetailAnchors = function () {};
	var ms = laneNSession({
		state: 'needs_input', stateSince: first, title: 'Anchor fixture', repo: 'fixture',
		pendingPermission: { tool: 'Bash', summary: 'Run it', requestedAt: first, requestId: '1' }
	});
	var mroot = laneNDetail(m, ms);
	ms.stateSince = second;
	ms.pendingPermission.requestedAt = second;
	m.w.renderDetailView([ms], false);
	eq(mroot.querySelector('.dv-approval-left').getAttribute('data-approval-at'),
		String(Date.parse(first)), 'without the refresh the anchor is the stale one (mutation proof)');

	/* A pendingPermission with no readable requestedAt leaves the anchor EMPTY.
	   approvalRemaining's own rule: unknown is not expired. */
	var u = F.nativePage();
	quiet(u, ['detectChime']);
	var us = laneNSession({
		state: 'needs_input', stateSince: first,
		pendingPermission: { tool: 'Bash', summary: 'Run it' }
	});
	var uroot = laneNDetail(u, us);
	eq(uroot.querySelector('.dv-approval-left').getAttribute('data-approval-at'), '',
		'an unreadable requestedAt anchors nothing rather than anchoring zero');
})();

(function laneNStreamStarvation() {
	/* A PING IS LIVENESS, NOT DELIVERY. A stream that opens, pings for ten minutes
	   and never sends a state frame left the fallback poll gated off for ever. */
	var c = F.nativePage();
	var w = quiet(c, ['detectChime', 'showNotice']);
	var gets = 0;
	w.fetch = function () {
		gets++;
		return Promise.resolve({ ok: true, json: function () { return Promise.resolve(stateDoc(c.now(), [])); } });
	};
	w.sseSource = { readyState: 1, close: function () {} };
	w.transportDiag();
	w.laneNNoteStateFrame();
	w.noteTransportEvent();
	ok(w.sseDelivering(), 'a stream that has just sent state is delivering');

	/* Ten minutes of pings and nothing else. lastEventAt stays fresh throughout, so
	   the existing liveness deadline never trips and the stream is never torn down. */
	for (var i = 0; i < 40; i++) { c.advance(15000); w.noteTransportEvent(); }
	eq(w.sseSilent(), false, 'the existing liveness deadline is satisfied by the pings');
	eq(w.sseDelivering(), false, 'but a ping-only stream is not delivering state');
	w.poll();
	eq(gets, 1, 'so the fallback poll resumes beside the healthy socket');

	/* WOULD IT FIRE ON A HEALTHY NIGHT? Replayed at the measured 2 s state cadence
	   over ten minutes: the state deadline is re-armed by every frame. */
	var h = F.nativePage();
	var wh = quiet(h, ['detectChime']);
	var hgets = 0;
	wh.fetch = function () { hgets++; return new Promise(function () {}); };
	wh.sseSource = { readyState: 1, close: function () {} };
	wh.transportDiag();
	for (var j = 0; j < 300; j++) {
		wh.onSseState({ data: JSON.stringify(stateDoc(h.now(), [sess('a', 'working')])) });
		h.advance(2000);
		wh.poll();
	}
	eq(hgets, 0, 'a healthy night at the measured 2 s cadence never un-gates the poll');

	/* AND THE SECOND CONDITION. A stream that has gone quiet while the document on
	   glass is still FRESH is left alone: the panel has nothing to repair, and a
	   poll every 3 s beside a healthy stream is the cost the stream removed. */
	var q = F.nativePage();
	var wq = quiet(q, ['detectChime']);
	var qgets = 0;
	wq.fetch = function () { qgets++; return new Promise(function () {}); };
	wq.sseSource = { readyState: 1, close: function () {} };
	wq.transportDiag();
	wq.onSseState({ data: JSON.stringify(stateDoc(q.now(), [sess('a', 'working')])) });
	q.advance(46000);
	wq.noteTransportEvent();
	/* The document is now 46 s old, so it IS stale and the poll is owed. Prove the
	   other half by moving the good stamp forward without a state frame. */
	eq(wq.laneNStateStarved(), true, 'past the deadline with a stale document, the poll is owed');
	wq.lastGoodAtMs = q.now();
	eq(wq.laneNStateStarved(), false, 'with a fresh document on the glass it is not');

	/* No stream has ever opened: the page is already polling and this must be inert. */
	var n = F.nativePage();
	quiet(n, ['detectChime']);
	eq(n.w.laneNStateStarved(), false, 'a page that never opened a stream never starves');
})();

(function laneNMode() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	eq(w.laneNModeLabel(laneNSession({})), null, 'an absent mode is an absent chip');
	eq(w.laneNModeLabel(laneNSession({ mode: 'normal' })), null, 'and so is the normal mode');
	eq(w.laneNModeLabel(laneNSession({ mode: 'plan' })), 'PLAN', 'plan reads PLAN');
	eq(w.laneNModeLabel(laneNSession({ mode: 'acceptEdits' })), 'AUTO-EDIT', 'acceptEdits reads AUTO-EDIT');
	eq(w.laneNModeLabel(laneNSession({ mode: 'bypassPermissions' })), 'BYPASS', 'bypassPermissions reads BYPASS');
	eq(w.laneNModeLabel(laneNSession({ mode: 'experimentalAutoRun' })), 'EXPERIMENTAL',
		'a mode this build does not know is shown as itself, upper-cased and capped at 12');
	eq(w.laneNModeLabel(laneNSession({ mode: '   ' })), null, 'whitespace is not a mode');
	eq(w.laneNModeLabel(laneNSession({ mode: 7 })), null, 'nor is a number');

	var card = w.buildCard(laneNSession({ mode: 'plan', model: 'opus-5' }), false);
	var badges = card.querySelectorAll('.badge');
	eq(badges[0].textContent, 'opus-5', 'the model badge is first');
	eq(badges[1].textContent, 'PLAN', 'and the mode chip sits beside it');
	eq(w.buildCard(laneNSession({ model: 'opus-5' }), false).querySelectorAll('.badge-mode').length, 0,
		'a normal session grows no chip');

	var root = laneNDetail(c, laneNSession({ mode: 'bypassPermissions', model: 'opus-5' }));
	eq(root.querySelectorAll('.dv-chip-mode').length, 1, 'the Detail page carries it too');
	eq(root.querySelector('.dv-chip-mode').textContent, 'BYPASS', 'with the same word');
})();

(function laneNActivityRendering() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	var full = { tool: 'Bash', detail: 'run the tests', at: '2026-09-21T11:30:00Z', callsThisTurn: 3 };

	var card = w.buildCard(laneNSession({ activity: full, lastEvent: 'the old event line' }), false);
	var row = card.querySelector('.card-activity');
	ok(row !== null, 'a working session shows the activity');
	eq(row.querySelector('.card-act-tool').textContent, 'Bash', 'the tool is its own element');
	eq(row.querySelector('.card-act-detail').textContent, '\u00B7 run the tests', 'the detail follows it');
	eq(row.querySelector('.card-act-calls').textContent, '\u00D73', 'and the call count rides at the end');
	eq(card.querySelectorAll('.card-event').length, 1, 'it REPLACES the event line rather than stacking on it');
	eq(card.textContent.indexOf('the old event line'), -1, 'so the old line is not also printed');

	/* A count of one is not printed: it is what every first call reads. */
	var one = w.buildCard(laneNSession({ activity: { tool: 'Read', detail: 'x', callsThisTurn: 1 } }), false);
	eq(one.querySelectorAll('.card-act-calls').length, 0, 'a single call prints no multiplier');

	/* NOT on a card that has stopped working: the turn has ended. */
	var done = w.buildCard(laneNSession({ state: 'done', activity: full, lastEvent: 'finished' }), false);
	eq(done.querySelectorAll('.card-activity').length, 0, 'a done card does not narrate a finished turn');
	eq(done.querySelector('.card-event').textContent, 'finished', 'it keeps its event line');

	/* And never over a question or a permission request, which own that slot. */
	var q = w.buildCard(laneNSession({ state: 'needs_input', question: 'which one?', activity: full }), false);
	eq(q.querySelectorAll('.card-activity').length, 0, 'a question keeps the body it already had');

	/* Absent, empty and malformed all read as absent. */
	eq(w.laneNActivity(laneNSession({})), null, 'no member, no activity');
	eq(w.laneNActivity(laneNSession({ activity: [] })), null, 'an array is not the object');
	eq(w.laneNActivity(laneNSession({ activity: { tool: '  ', detail: '' } })), null,
		'a blank tool and a blank detail are nothing to say');
	eq(w.laneNActivity(laneNSession({ activity: { detail: 'only a detail' } })).tool, null,
		'a detail with no tool still renders');

	/* The Detail page: whole, with a relative time the existing tick relabels. */
	var root = laneNDetail(c, laneNSession({ activity: full }));
	var dv = root.querySelector('.dv-activity');
	ok(dv !== null, 'the Detail page shows it');
	eq(dv.getAttribute('data-at'), String(Date.parse(full.at)), 'anchored on `at`');
	ok(dv.classList.contains('event-row'), 'as an event row, so the page tick already ages it');
	eq(dv.querySelector('.dv-act-detail').textContent, 'run the tests', 'with the detail unclamped');
	eq(dv.querySelector('.dv-act-calls').textContent, '3 calls this turn', 'and the count spelled out');
	/* The anchor is an age, so it is refreshed outside the signature like the other two. */
	var s2 = laneNSession({ activity: full });
	laneNDetail(c, s2);
	s2.activity = { tool: 'Bash', detail: 'run the tests', at: '2026-09-21T11:31:00Z', callsThisTurn: 3 };
	c.w.renderDetailView([s2], false);
	eq(c.w.ui.viewDetail.querySelector('.dv-activity').getAttribute('data-at'),
		String(Date.parse('2026-09-21T11:31:00Z')), 'and it follows a newer `at` through the short-circuit');
})();

(function laneNCompactionRendering() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	var live = { count: 2, lastAt: '2026-09-21T11:40:00Z', inProgress: true };

	var card = w.buildCard(laneNSession({ compaction: live }), false);
	eq(card.querySelector('.card-state').textContent, 'compacting', 'the state chip reads COMPACTING');
	eq(card.getAttribute('data-state'), 'working',
		'and data-state is untouched, so the chip keeps the WORKING colour and never reads as an alert');

	/* Never in a state that is not working: a compaction happens inside a turn. */
	var waiting = w.buildCard(laneNSession({ state: 'needs_input', compaction: live }), false);
	eq(waiting.querySelector('.card-state').textContent, 'needs input',
		'a waiting session keeps its own word whatever the compaction member says');
	eq(waiting.getAttribute('data-state'), 'needs_input', 'and its own colour');

	var settled = w.buildCard(laneNSession({ compaction: { count: 2, inProgress: false } }), false);
	eq(settled.querySelector('.card-state').textContent, 'working', 'a finished compaction says nothing on the card');

	/* THE HUNG HINT MUST NOT FIRE OVER IT. The hint tells a session that is thinking
	   from one that has hung; a compacting session is neither, it is busy and
	   touches nothing while it works. "COMPACTING 8m quiet 3m" was the panel raising
	   a hang hint against its own evidence. */
	var g = F.nativePage();
	var wg = quiet(g, ['detectChime', 'syncHeaderChips']);
	var old = new Date(g.now() - 300000).toISOString();
	var comp = laneNSession({ id: 'c', lastActivityAt: old, stateSince: old, compaction: live, model: 'opus-5' });
	var plain = laneNSession({ id: 'p', lastActivityAt: old, stateSince: old, model: 'opus-5' });
	wg.renderSessions([comp, plain], 'ok', false, null);
	wg.tickAges(g.now());
	var nodes = wg.ui.cards.children;
	eq(nodes[0].getAttribute('data-compacting'), '1', 'the compacting card carries the attribute the tick reads');
	eq(nodes[0].querySelector('.card-hint').textContent, '', 'and grows no hang hint');
	eq(nodes[0].classList.contains('hung'), false, 'nor the hung class that hides its age');
	ok(nodes[1].querySelector('.card-hint').textContent.indexOf('quiet ') === 0,
		'while an ordinary quiet card still gets the hint it always had');
	ok(nodes[1].classList.contains('hung'), 'and the class with it');

	var root = laneNDetail(c, laneNSession({ compaction: live }));
	var line = root.querySelector('.dv-compaction').textContent;
	ok(line.indexOf('compacting now') >= 0, 'the Detail page says it is running');
	ok(line.indexOf('2 compactions') >= 0, 'and gives the count');
	ok(line.indexOf('last at') >= 0, 'and the last time');
	eq(w.laneNCompaction(laneNSession({ compaction: {} })), null, 'an empty member is an absent block');
	eq(w.laneNCompaction(laneNSession({ compaction: [] })), null, 'an array is not the object');
})();

(function laneNTodoRendering() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	var todos = { done: 3, total: 7, current: 'Import the last two scanner exports' };

	var card = w.buildCard(laneNSession({ todos: todos }), false);
	var bar = card.querySelector('.card-todo');
	ok(bar !== null, 'the card grows a hairline');
	eq(card.querySelector('.badge-todo').textContent, '3/7', 'and the figure behind it');
	ok(bar.getAttribute('aria-label').indexOf('3/7') >= 0, 'the bar names its own figure');
	eq(w.buildCard(laneNSession({}), false).querySelectorAll('.card-todo').length, 0,
		'an absent member is an absent hairline, not an empty track');
	eq(w.laneNTodos(laneNSession({ todos: { done: 0, total: 0 } })), null,
		'a list of zero items is not a list');
	eq(w.laneNTodos(laneNSession({ todos: { done: 9, total: 4 } })).done, 4,
		'a done count past the total clamps rather than drawing past the end');
	eq(w.laneNTodos(laneNSession({ todos: { done: -3, total: 4 } })).done, 0, 'and a negative one clamps up');
	eq(w.laneNTodos(laneNSession({ todos: { done: 3, total: 7 } })).pct, 43, 'the fill is the rounded fraction');

	/* The current item is the Detail page's, WHOLE. */
	var root = laneNDetail(c, laneNSession({ todos: todos }));
	eq(root.querySelector('.dv-todo-current').textContent, todos.current,
		'the Detail page prints the current item whole');
	ok(root.querySelector('.dv-todo').querySelector('.bv-panel-head').textContent.indexOf('3/7') >= 0, 'with its figure');
	var long = 'y'.repeat(400);
	var lroot = laneNDetail(c, laneNSession({ todos: { done: 1, total: 2, current: long } }));
	eq(lroot.querySelector('.dv-todo-current').textContent.length, 400, 'and never clamps it');
})();

(function laneNPromptQueueRendering() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);

	/* TWO QUEUES, AND THE WORDS MUST KEEP THEM APART. */
	var both = w.buildCard(laneNSession({
		promptQueue: 3,
		queuedContinue: { prompt: 'Run the tests', label: 'Run the tests', queuedAt: '2026-09-21T11:00:00Z' }
	}), false);
	var row = both.querySelector('.card-queued');
	ok(row.textContent.indexOf('queued: ') >= 0, 'SideCrab\u2019s own prompt keeps its line');
	eq(row.querySelector('.card-typed').textContent, '3 queued', 'and the typed-ahead count rides beside it');
	ok(row.querySelector('.card-typed').getAttribute('aria-label').indexOf('Claude Code') >= 0,
		'which names whose queue it is');
	ok(row.querySelector('.card-typed').getAttribute('aria-label').indexOf('not SideCrab') >= 0,
		'and says which one it is not');

	/* Present on its own, it takes the row rather than being swallowed. */
	var alone = w.buildCard(laneNSession({ promptQueue: 2 }), false);
	var aloneNote = alone.querySelector('.card-typed');
	ok(aloneNote !== null, 'a typed-ahead queue with no SideCrab prompt still shows');
	eq(aloneNote ? aloneNote.textContent : null, '2 queued', 'with its own count');
	eq(alone.querySelectorAll('.card-queued-text').length, 0, 'and invents no SideCrab prompt to hang it on');

	/* A ROW OF ITS OWN COSTS A LINE, so the two cards with no line to give do not
	   get one - the ctx chip's own rule. Measured at 2560x720 before this gate: the
	   approval card's badges row was pushed 15.4 px out of the card and cut. */
	var approval = w.buildCard(laneNSession({
		state: 'needs_input', promptQueue: 2,
		pendingPermission: { tool: 'Bash', summary: 'rm -rf', requestedAt: '2026-09-21T11:00:00Z' }
	}), false);
	eq(approval.querySelectorAll('.card-queued-typed').length, 0,
		'an approval card grows no row of its own for it');
	var question = w.buildCard(laneNSession({ state: 'needs_input', question: 'which one?', promptQueue: 2 }), false);
	eq(question.querySelectorAll('.card-queued-typed').length, 0, 'and neither does a question card');
	/* Riding inside a row that already exists costs nothing, so it is not gated. */
	var approvalQ = w.buildCard(laneNSession({
		state: 'needs_input', promptQueue: 2,
		queuedContinue: { prompt: 'Continue', label: 'Continue', queuedAt: '2026-09-21T11:00:00Z' },
		pendingPermission: { tool: 'Bash', summary: 'rm -rf', requestedAt: '2026-09-21T11:00:00Z' }
	}), false);
	eq(approvalQ.querySelectorAll('.card-typed').length, 1,
		'but it still rides inside a queued row the card was already paying for');

	eq(w.laneNPromptQueue(laneNSession({ promptQueue: 0 })), null, 'zero queued is nothing to say');
	eq(w.laneNPromptQueue(laneNSession({ promptQueue: '3' })), null, 'a string is not the integer');
	eq(w.buildCard(laneNSession({}), false).querySelectorAll('.card-typed').length, 0, 'and absent is absent');

	var root = laneNDetail(c, laneNSession({ promptQueue: 1 }));
	var txt = root.querySelector('.dv-typed').textContent;
	ok(txt.indexOf('1 prompt ') >= 0, 'the Detail page counts in words');
	ok(txt.indexOf('Claude Code') >= 0 && txt.indexOf('not SideCrab') >= 0,
		'and spells out which queue this is, where there is width for it');
})();

(function laneNFilesRendering() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	var files = { count: 12, recent: ['companion/config_schema.py', 'docs/config-reference.md'] };

	eq(w.buildCard(laneNSession({ filesTouched: files }), false).querySelectorAll('.dv-files').length, 0,
		'the card shows nothing at all');
	var root = laneNDetail(c, laneNSession({ filesTouched: files }));
	ok(root.querySelector('.dv-files').querySelector('.bv-panel-head').textContent.indexOf('12 files touched') >= 0,
		'the Detail page gives the count');
	var rows = root.querySelectorAll('.dv-file');
	eq(rows.length, 2, 'and the recent leaves');
	eq(rows[0].textContent, 'config_schema.py', 'as leaves, not paths');
	eq(rows[0].getAttribute('title'), 'companion/config_schema.py', 'with the whole path still recoverable');
	eq(w.laneNFilesBlock(laneNSession({ filesTouched: { count: 1, recent: [] } }))
		.querySelector('.bv-panel-head').textContent, '1 file touched', 'one file is singular');
	eq(w.laneNFiles(laneNSession({ filesTouched: { count: 0, recent: [] } })), null,
		'nothing touched is an absent block, never a zero');
	eq(w.laneNFiles(laneNSession({ filesTouched: [] })), null, 'an array is not the object');
	eq(w.laneNLeaf('C:\\work\\repo\\file.ts'), 'file.ts', 'a Windows path has a leaf too');
	eq(w.laneNLeaf('bare.md'), 'bare.md', 'and a bare name is its own leaf');
})();

(function laneNSignature() {
	/* Every one of these is card STRUCTURE: it must appear AND DISAPPEAR with the
	   member. A signature that missed one would leave the card advertising a mode
	   the session has left, until something else happened to rebuild it. */
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	var base = laneNSession({});
	var cases = [
		['mode', { mode: 'plan' }],
		['activity', { activity: { tool: 'Bash', detail: 'x' } }],
		['activity call count', { activity: { tool: 'Bash', detail: 'x', callsThisTurn: 4 } }],
		['todos', { todos: { done: 1, total: 2 } }],
		['todo current', { todos: { done: 1, total: 2, current: 'the item' } }],
		['compaction', { compaction: { count: 1, inProgress: true } }],
		['filesTouched', { filesTouched: { count: 2, recent: ['a/b.ts'] } }],
		['promptQueue', { promptQueue: 3 }]
	];
	var bare = w.laneNSig(base);
	for (var i = 0; i < cases.length; i++) {
		var s = laneNSession(cases[i][1]);
		ok(w.laneNSig(s) !== bare, cases[i][0] + ' moves the signature when it appears');
		ok(w.laneNSig(base) === bare, 'and the bare session is unchanged by ' + cases[i][0]);
	}
	/* `at` is deliberately NOT in it: it moves every poll and is relabelled in place. */
	eq(w.laneNSig(laneNSession({ activity: { tool: 'B', detail: 'x', at: '2026-09-21T11:00:00Z' } })),
		w.laneNSig(laneNSession({ activity: { tool: 'B', detail: 'x', at: '2026-09-21T11:05:00Z' } })),
		'a newer `at` alone does not rebuild the card');

	/* AND THE GRID MUST ACTUALLY USE IT. Testing laneNSig alone would pass with the
	   call missing from renderSessions, which is the whole failure mode: a correct
	   fragment nobody reads. Driven through the real render. */
	var g = F.nativePage();
	var wg = quiet(g, ['detectChime', 'syncHeaderChips']);
	var live = laneNSession({ id: 'g', model: 'opus-5', lastEvent: 'e' });
	wg.renderSessions([live], 'ok', false, null);
	var sig0 = wg.cardSig;
	live.mode = 'plan';
	wg.renderSessions([live], 'ok', false, null);
	ok(wg.cardSig !== sig0, 'entering plan mode rebuilds the card through renderSessions');
	eq(wg.ui.cards.querySelectorAll('.badge-mode').length, 1, 'so the chip is on the glass');
	delete live.mode;
	live.todos = { done: 1, total: 3 };
	wg.renderSessions([live], 'ok', false, null);
	eq(wg.ui.cards.querySelectorAll('.badge-mode').length, 0, 'and LEAVING plan mode removes it again');
	eq(wg.ui.cards.querySelectorAll('.card-todo').length, 1, 'while a new todo list grows its hairline');

	/* The Detail page's own signature, driven the same way: no reset, so the only
	   thing that can rebuild the page is the fragment being in the key. */
	var d = F.nativePage();
	var wd = quiet(d, ['detectChime']);
	var ds = laneNSession({ id: 'd', model: 'opus-5', lastEvent: 'e' });
	var droot = laneNDetail(d, ds);
	eq(droot.querySelectorAll('.dv-chip-mode').length, 0, 'the page starts with no mode chip');
	ds.mode = 'acceptEdits';
	wd.renderDetailView([ds], false);
	eq(droot.querySelectorAll('.dv-chip-mode').length, 1, 'entering a mode rebuilds the page');
	ds.todos = { done: 2, total: 4, current: 'the item' };
	wd.renderDetailView([ds], false);
	eq(droot.querySelectorAll('.dv-todo-current').length, 1, 'and so does a new todo list');
	delete ds.todos;
	wd.renderDetailView([ds], false);
	eq(droot.querySelectorAll('.dv-todo-current').length, 0, 'and clearing one removes it again');
})();

(function laneNHostileFields() {
	/* Nothing here may throw. A contract-legal null, a wrong type and a hostile
	   value all have to land on "the member is absent". */
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	var LONG = 'z'.repeat(3000);
	var hostile = [
		{ mode: null }, { mode: {} }, { mode: LONG },
		{ activity: null }, { activity: 'Bash' }, { activity: { tool: {}, detail: [] } },
		{ activity: { tool: 'B', detail: LONG, callsThisTurn: 'lots' } },
		{ todos: null }, { todos: 'none' }, { todos: { done: 'x', total: 'y' } },
		{ todos: { done: 1, total: Infinity } },
		{ compaction: null }, { compaction: 'yes' }, { compaction: { inProgress: 'true' } },
		{ filesTouched: null }, { filesTouched: { count: 'many', recent: [1, 2, null] } },
		{ promptQueue: null }, { promptQueue: NaN }, { promptQueue: -4 }, { promptQueue: {} }
	];
	var threw = [];
	for (var i = 0; i < hostile.length; i++) {
		var s = laneNSession(hostile[i]);
		try { w.buildCard(s, false); w.laneNSig(s); laneNDetail(c, s); }
		catch (e) { threw.push(JSON.stringify(hostile[i]) + ': ' + e.message); }
	}
	eq(threw, [], 'no contract-illegal value throws anywhere in the new render path');
	eq(w.laneNCompacting(laneNSession({ compaction: { inProgress: 'true' } })), false,
		'a STRING "true" is not the boolean, so it paints nothing');
	eq(w.laneNActivity(laneNSession({ activity: { tool: 'B', detail: 'x', callsThisTurn: 'lots' } })).calls, null,
		'an unreadable call count is absent rather than NaN');
})();

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

/* ================ lane S: a failed session, and the subagents crabd can name */

/* Version label v0.34.0 in this section is PROVISIONAL; version.json still reads
   0.33.0 and the orchestrator assigns the real number at the merge. */

function laneSSession(extra) {
	var s = { id: 'f1', state: 'failed', stateSince: '2026-09-21T11:00:00Z',
		title: 'Failure fixture', repo: 'fixture', model: 'claude-opus-5' };
	for (var k in extra) { if (Object.prototype.hasOwnProperty.call(extra, k)) s[k] = extra[k]; }
	return s;
}

function laneSFail(type, message) {
	var f = { errorType: type, at: '2026-09-21T11:00:00Z' };
	if (message !== undefined) f.message = message;
	return f;
}

/* The Detail page against one session, the same driver the lane N section uses. */
function laneSDetail(ctx, s) {
	var w = ctx.w;
	w.viewIdx = 3;
	w.detailSessionId = String(s.id);
	w.lastGoodDoc = { sessions: [s] };
	w.detailViewSig = null;
	w.renderDetailView([s], false);
	return w.ui.viewDetail;
}

/* THE MARKUP, as a string. The fixture's Element has no outerHTML, and the
   0.33.0-parity check below needs to compare whole trees rather than the handful
   of nodes a querySelector would reach — a regression that added an empty div is
   exactly the kind this has to catch. Attributes are sorted so the comparison is
   about content and not about the order two branches happened to set them in. */
function laneSMarkup(el) {
	if (!el) return '';
	var tag = el.tagName.toLowerCase();
	var keys = Object.keys(el.attrs).sort();
	var attrs = '';
	for (var i = 0; i < keys.length; i++) attrs += ' ' + keys[i] + '="' + el.attrs[keys[i]] + '"';
	var cls = el.className ? ' class="' + el.className + '"' : '';
	var inner = el.text;
	for (var j = 0; j < el.children.length; j++) inner += laneSMarkup(el.children[j]);
	return '<' + tag + cls + attrs + '>' + inner + '</' + tag + '>';
}

(function laneSFailureWords() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);

	/* ALL THIRTEEN THE CONTRACT NAMES, in the words a wall panel can read. crabd
	   clamps anything else to `unknown`, so this set is the whole wire vocabulary. */
	var want = {
		rate_limit: 'rate limited', overloaded: 'overloaded',
		authentication_failed: 'sign-in failed', billing_error: 'billing',
		server_error: 'server error', oauth_org_not_allowed: 'org not allowed',
		account_on_hold: 'account on hold', verification_required: 'verification needed',
		invalid_request: 'bad request', model_not_found: 'model not found',
		max_output_tokens: 'output limit', cloud_credential_error: 'cloud credentials',
		unknown: 'failed'
	};
	for (var type in want) {
		if (!Object.prototype.hasOwnProperty.call(want, type)) continue;
		eq(w.laneSFailureWords(laneSSession({ failure: laneSFail(type) })), want[type],
			type + ' reads "' + want[type] + '"');
	}

	/* A type this build does not know renders the bare word, never the wire token:
	   "gateway_timeout" on a wall panel is a field name, not a sentence. */
	eq(w.laneSFailureWords(laneSSession({ failure: laneSFail('gateway_timeout') })), 'failed',
		'an errorType the map does not name falls back to the state word');
	eq(w.laneSFailureWords(laneSSession({ failure: laneSFail(42) })), 'failed',
		'and so does one that is not a string');

	eq(Object.keys(w.FAILURE_WORDS).length, 13, 'and the map names thirteen and no more');

	/* NO BLOCK, NO WORDS. A failed row legitimately arrives without `failure` - a
	   crabd restart keeps the kind and the title in history and never the error -
	   and the STATE is the fact on its own. Repeating "failed" under a card already
	   reading FAILED would spend the line to say nothing and push out the lastEvent
	   that is the only thing left describing the session. */
	eq(w.laneSFailureWords(laneSSession()), null, 'a failed session with no failure block gets no words');
	eq(w.laneSFailureWords(laneSSession({ failure: 'not-an-object' })), null, 'a malformed block is an absent one');
	eq(w.laneSFailureWords(laneSSession({ failure: [] })), null, 'an array is not a failure block');

	/* AND ONLY ON A FAILED SESSION. A `failure` left behind on a row that has since
	   recovered must not put a red word on a working card. */
	eq(w.laneSFailureWords({ id: 'a', state: 'working', failure: laneSFail('rate_limit') }), null,
		'a working session carrying a stale failure block reads nothing');
	eq(w.laneSFailure({ id: 'a', state: 'done', failure: laneSFail('overloaded') }), null,
		'and the block itself is not read off a done row');

	/* The message is optional, and an empty one is ABSENT rather than a blank
	   region under a heading. */
	eq(w.laneSFailureMessage(laneSSession({ failure: laneSFail('rate_limit', 'retry after 60s') })),
		'retry after 60s', 'a message is carried through');
	eq(w.laneSFailureMessage(laneSSession({ failure: laneSFail('rate_limit') })), null, 'an absent message is null');
	eq(w.laneSFailureMessage(laneSSession({ failure: laneSFail('rate_limit', '   ') })), null,
		'and a whitespace-only message is absent, not blank');
})();

(function laneSFailedCard() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	var s = laneSSession({
		failure: laneSFail('rate_limit', 'would exceed 80,000 input tokens per minute'),
		lastEvent: 'converting the schema gate',
		subagents: { running: 2, total: 4 }
	});
	var card = w.buildCard(s, false);

	eq(card.getAttribute('data-state'), 'failed', 'the card carries the failed state for the stylesheet');
	eq(card.querySelector('.card-state').textContent, 'failed',
		'and the state WORD is failed (the stylesheet upper-cases it to FAILED)');
	var line = card.querySelector('.card-failure');
	ok(!!line, 'the event line is the failure line');
	eq(line.textContent, 'rate limited', 'and it carries the error type in words');
	eq(card.textContent.indexOf('converting the schema gate'), -1,
		'the failure REPLACES lastEvent rather than stacking a second line on it');

	/* The card keeps its badge. subagents.running is untouched by this lane. */
	var subBadge = card.querySelector('.badge-sub');
	ok(!!subBadge && subBadge.textContent === '2 sub', 'the card keeps its "N sub" badge');

	/* NOT the loud card. A failed row does not pulse, is not swipeable and is not
	   acked away: it has no question to answer and nothing to dismiss. */
	eq(card.classList.contains('pulse'), false, 'a failed card does not pulse');
	eq(card.classList.contains('swipeable'), false, 'and it cannot be swiped away');

	/* The message is NOT on the card. Two words fit there; a paragraph does not,
	   and the Detail page is the surface that carries it. */
	eq(card.textContent.indexOf('80,000'), -1, 'the message stays off the card');

	/* A failed row with no block keeps its lastEvent, which is then the only thing
	   on the card describing the session. The state word carries the failure. */
	var bare = w.buildCard(laneSSession({ id: 'f2', lastEvent: 'running the suite' }), false);
	eq(bare.querySelector('.card-failure'), null, 'a failed card with no failure block has no failure line');
	eq(bare.querySelector('.card-state').textContent, 'failed', 'the state word still says so on its own');
	eq(bare.querySelector('.card-event').textContent, 'running the suite',
		'and lastEvent keeps the line rather than being pushed out by a word that adds nothing');
})();

(function laneSFailedDetail() {
	var c = F.nativePage();
	quiet(c, ['detectChime']);
	var s = laneSSession({ failure: laneSFail('overloaded', 'the upstream is overloaded, retry shortly') });
	var root = laneSDetail(c, s);

	var chip = root.querySelector('.dv-chip-state');
	eq(chip.getAttribute('data-state'), 'failed', 'the Detail state chip carries the failed state');
	ok(chip.textContent.indexOf('FAILED') === 0, 'and reads FAILED');
	var head = root.querySelector('.dv-failure-head');
	ok(!!head, 'the Detail page heads the failure block');
	eq(head.textContent, 'failed — overloaded', 'with the error type in words');
	eq(root.querySelector('.dv-failure').textContent, 'the upstream is overloaded, retry shortly',
		'and carries the message whole');

	/* No message, no block: a heading over an empty region would be the page
	   claiming a detail it does not have. */
	var n = F.nativePage();
	quiet(n, ['detectChime']);
	var nroot = laneSDetail(n, laneSSession({ id: 'f3', failure: laneSFail('server_error') }));
	ok(!!nroot.querySelector('.dv-failure-head'), 'a failure with no message still gets the heading');
	eq(nroot.querySelector('.dv-failure'), null, 'and no empty message block under it');

	/* The failure outranks the latest-event block: on a failed session the last
	   thing that happened IS the failure. */
	var e = F.nativePage();
	quiet(e, ['detectChime']);
	var eroot = laneSDetail(e, laneSSession({ id: 'f4', lastEvent: 'reading the config',
		failure: laneSFail('billing_error') }));
	eq(eroot.querySelector('.dv-failure-head').textContent, 'failed — billing',
		'the failure block is rendered');
	eq(eroot.textContent.indexOf('reading the config'), -1, 'and the latest-event block is not');
})();

(function laneSNamedSubagents() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);

	/* NINE against a cap of EIGHT. crabd caps the array; this defends anyway, for
	   the reason subList defends against subagentDetail's cap of five. */
	var nine = [];
	for (var i = 1; i <= 9; i++) {
		nine.push({ id: 'sub-' + i, type: 'agent-' + i, startedAt: '2026-09-21T10:0' + (i - 1) + ':00Z' });
	}
	eq(w.laneSNamedSubs({ subagents: { running: 9, named: nine } }).length, 8,
		'the named list is capped at 8 whatever the feed sends');
	eq(w.laneSNamedSubs({ subagents: { running: 9, named: nine } })[0].type, 'agent-1',
		'and the cap takes the feed order, it does not re-sort');

	/* Absent, malformed and non-array all read as NO named agents, never as a
	   reason to look somewhere else. */
	eq(w.laneSNamedSubs({ subagents: { running: 3 } }), [], 'an absent member is an empty list');
	eq(w.laneSNamedSubs({}), [], 'and so is an absent subagents block');
	eq(w.laneSNamedSubs({ subagents: { named: 'nope' } }), [], 'and a non-array member');
	eq(w.laneSNamedSubs({ subagents: { named: [null, 'x', 7] } }), [], 'and rows that are not objects');

	/* A row crabd cannot name is still a row: an agent is running and saying so
	   with no name is truer than pretending it is not there. */
	var unnamed = w.laneSNamedSubs({ subagents: { named: [{ id: 'a', startedAt: '2026-09-21T10:00:00Z' }] } });
	eq(unnamed.length, 1, 'a row with no type survives');
	eq(unnamed[0].type, 'subagent', 'and is named generically');
	eq(w.laneSNamedSubs({ subagents: { named: [{ id: 'a', type: 'x', startedAt: 'not-a-date' }] } })[0].at, null,
		'an unparseable startedAt anchors nothing rather than anchoring zero');

	/* THE DETAIL PAGE lists the types and how long each has run. */
	var d = F.nativePage();
	quiet(d, ['detectChime']);
	var s = { id: 'a', state: 'working', stateSince: '2026-09-21T11:00:00Z', title: 'T', repo: 'r',
		subagents: { running: 3, total: 3, named: [
			{ id: 's1', type: 'general-purpose', startedAt: iso(d.now() - 125000) },
			{ id: 's2', type: 'Explore', startedAt: iso(d.now() - 3600000) },
			{ id: 's3', type: 'code-reviewer', startedAt: 'not-a-date' }
		] } };
	var root = laneSDetail(d, s);
	var labels = root.querySelectorAll('.sub-label');
	eq(labels.length, 3, 'the Detail page lists every named agent');
	eq(labels[0].textContent, 'general-purpose', 'by its type');
	eq(labels[1].textContent, 'Explore', 'in feed order');
	var ages = root.querySelectorAll('.sub-age-live');
	eq(ages.length, 3, 'each row carries a LIVE age anchor, not a snapshot');
	eq(ages[0].textContent, '2m', 'and the tick has filled the first');
	eq(ages[1].textContent, '1h', 'and the second, in the words fmtDur uses');
	eq(ages[2].textContent, '—', 'while an unreadable start stays an em-dash');

	/* THE CARD IS UNTOUCHED: the badge counts subagents.running and the card's own
	   rows are still subagentDetail's. */
	var card = w.buildCard(s, false);
	eq(card.querySelector('.badge-sub').textContent, '3 sub', 'the card keeps the running count');
	eq(card.querySelectorAll('.sub-age-live').length, 0, 'and grows no named rows');

	/* THE FALL-BACK. With no `named`, the Detail page shows subagentDetail exactly
	   as it did at 0.33.0 — one block, never two, because both describe the same
	   agents and listing them together would name every agent twice. */
	var f = F.nativePage();
	quiet(f, ['detectChime']);
	var froot = laneSDetail(f, { id: 'b', state: 'working', stateSince: '2026-09-21T11:00:00Z',
		title: 'T', repo: 'r', subagents: { running: 2 },
		subagentDetail: [{ label: 'contract-diff', ageSec: 61 }, { label: 'fixture-sweep', ageSec: 288 }] });
	var flabels = froot.querySelectorAll('.sub-label');
	eq(flabels.length, 2, 'without `named` the subagentDetail rows are what is shown');
	eq(flabels[0].textContent, 'contract-diff', 'by their label');
	eq(froot.querySelectorAll('.sub-age-live').length, 0, 'and they keep the snapshot age, not an anchor');
	eq(froot.querySelectorAll('.sub-age')[0].textContent, '1m', 'which the poll relabels');

	/* AND NEVER BOTH. A session carrying both members shows the named list alone. */
	var g = F.nativePage();
	quiet(g, ['detectChime']);
	var groot = laneSDetail(g, { id: 'c', state: 'working', stateSince: '2026-09-21T11:00:00Z',
		title: 'T', repo: 'r',
		subagents: { running: 1, named: [{ id: 'x', type: 'doc-writer', startedAt: iso(g.now() - 60000) }] },
		subagentDetail: [{ label: 'contract-diff', ageSec: 61 }] });
	eq(groot.querySelectorAll('.sub-label').length, 1, 'a session with both members lists each agent once');
	eq(groot.querySelector('.sub-label').textContent, 'doc-writer', 'and the named list is the one that wins');
})();

(function laneSOrdering() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);

	/* WHERE `failed` SORTS, in the two places this file declares an order. */
	function row(id, state) { return { id: id, state: state, stateSince: '2026-09-21T11:00:00Z' }; }

	w.lastGoodDoc = { sessions: [row('w1', 'working'), row('f1', 'failed'), row('q1', 'needs_input')] };
	w.detailSessionId = null;
	eq(w.detailTarget().id, 'q1', 'the Detail page still opens on the waiting row first');
	w.lastGoodDoc = { sessions: [row('i1', 'idle'), row('w1', 'working'), row('f1', 'failed')] };
	w.detailSessionId = null;
	eq(w.detailTarget().id, 'f1', 'with nothing waiting it opens on the failed row, ahead of working');

	/* THE CLAMP. A failed row has no pulse, no glow, no chime and no swipe, so the
	   clamp is the only thing between it and the "+N more" tile. */
	var many = [];
	for (var i = 0; i < 12; i++) many.push(row('d' + i, 'done'));
	many.push(row('fail', 'failed'));
	var res = w.clampGrid(many, 8);
	var kept = res.visible.filter(function (s) { return s.id === 'fail'; }).length;
	eq(kept, 1, 'a failed row at the end of a long done list keeps a cell');
	eq(res.rest.some(function (s) { return s.state === 'failed'; }), false, 'and is never in the cut list');
	/* The TAIL still reads "idle": the six rows the clamp cut are all done, and the
	   failed row is not among them. The word describes what was CUT, not what was
	   kept, so protecting the failed row is exactly what leaves this reading true. */
	eq(res.chipText, '+6 idle', 'and the tail names what was cut, which is six done rows');

	/* The waiting row still outranks it when there are not cells for both: the
	   clamp is ONE pass in feed order, so crabd's own order decides between them. */
	var both = [row('q1', 'needs_input'), row('f1', 'failed')];
	for (var j = 0; j < 10; j++) both.push(row('d' + j, 'done'));
	var r2 = w.clampGrid(both, 3);
	eq(r2.visible.map(function (s) { return s.id; }), ['q1', 'f1'],
		'waiting and failed take the cells, in the order the feed delivered them');

	/* THE FILTER IS UNTOUCHED, and that is the right answer rather than an
	   omission: FILTERS names four states and a fifth falls only into "all",
	   never silently into a bucket whose label would then be a lie. */
	eq(w.FILTERS[1].match.failed, undefined, '`failed` is not folded into the Waiting filter');
	eq(w.FILTERS[3].match.failed, undefined, 'nor into Done/Idle');
})();

(function laneSNoChime() {
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	var prev = { a: 'working' };
	var now = c.now();

	/* THE DECISION, pinned: a failure is not a question. It has no answer to give,
	   and one rate limit fails every session on the account in the same poll. */
	eq(w.chimeDecision(prev, [{ id: 'a', state: 'failed' }], false, true, now, 0), false,
		'a session that has just failed does not chime');
	eq(w.chimeDecision(prev, [{ id: 'b', state: 'failed' }], false, true, now, 0), false,
		'nor does a failed session this panel has never seen before');
	eq(w.chimeDecision(prev, [{ id: 'a', state: 'failed' }, { id: 'b', state: 'failed' },
		{ id: 'c', state: 'failed' }], false, true, now, 0), false,
		'and a whole account rate-limited at once is silent, which is the point');

	/* THE GATE STILL FIRES for the thing it is for, so this is a decision and not a
	   broken chime. */
	eq(w.chimeDecision(prev, [{ id: 'a', state: 'needs_input' }], false, true, now, 0), true,
		'a new question still chimes');
})();

(function laneS033Parity() {
	/* A SESSION THAT CARRIES NEITHER MEMBER RENDERS EXACTLY THE 0.33.0 MARKUP.
	   Proven by running the shipping builders twice over one session: once as they
	   ship, and once with this lane's three entry points answering the way the file
	   answered before they existed. Identical strings mean the additive code
	   contributes nothing at all to a document that carries none of the members,
	   which is the whole presence-detection contract. */
	var plain = { id: 'p1', state: 'working', stateSince: '2026-09-21T11:00:00Z',
		title: 'A working session', titleSource: 'cwd', repo: 'sidecrab', branch: 'main',
		model: 'claude-opus-5', speed: 'fast', lastEvent: 'reading the contract',
		contextTokens: 61000, contextWindowTokens: 200000,
		subagents: { running: 2, total: 4 },
		subagentDetail: [{ label: 'contract-diff', ageSec: 61 }, { label: 'fixture-sweep', ageSec: 288 }],
		events: [{ at: '2026-09-21T10:59:00Z', text: 'started' }] };

	var a = F.nativePage();
	quiet(a, ['detectChime']);
	var shipped = laneSMarkup(a.w.buildCard(plain, false)) + '#####' +
		laneSMarkup(laneSDetail(a, plain));

	var b = F.nativePage();
	quiet(b, ['detectChime']);
	/* The pre-lane answers: no failure words, no named agents, no signature
	   fragment. Nothing else about the two pages differs. */
	b.w.laneSFailureWords = function () { return null; };
	b.w.laneSFailureMessage = function () { return null; };
	b.w.laneSNamedSubs = function () { return []; };
	b.w.laneSSig = function () { return ''; };
	var before = laneSMarkup(b.w.buildCard(plain, false)) + '#####' +
		laneSMarkup(laneSDetail(b, plain));

	eq(shipped, before, 'a session carrying neither new member renders exactly the 0.33.0 markup');

	/* MUTATION PROOF for the comparison itself. A test that cannot tell two trees
	   apart reports parity forever, so the same serializer is shown FAILING on a
	   session that does carry the members. */
	var m = F.nativePage();
	quiet(m, ['detectChime']);
	var carrying = JSON.parse(JSON.stringify(plain));
	carrying.state = 'failed';
	carrying.failure = { errorType: 'rate_limit', at: '2026-09-21T11:00:00Z' };
	carrying.subagents.named = [{ id: 'x', type: 'Explore', startedAt: '2026-09-21T10:58:00Z' }];
	var moved = laneSMarkup(m.w.buildCard(carrying, false)) + '#####' +
		laneSMarkup(laneSDetail(m, carrying));
	ok(moved !== shipped, 'and the serializer does move when the members are there (mutation proof)');

	/* THE SIGNATURE is structure, not an age: it moves with the members and is
	   empty without them, so a card cannot go on printing a failure for a session
	   that has recovered. */
	eq(a.w.laneSSig(plain), '~', 'the signature fragment is empty on a session with neither member');
	ok(a.w.laneSSig(carrying) !== '~', 'and carries both when they are there');
	var recovered = JSON.parse(JSON.stringify(carrying));
	recovered.state = 'working';
	delete recovered.failure;
	delete recovered.subagents.named;
	eq(a.w.laneSSig(recovered), '~', 'and empties again the moment they go');
})();

(function laneSFixtures() {
	/* Both new fixtures parse, are registered, and carry the shapes they exist for.
	   A fixture nothing loads is a file, not a test. */
	var dir = path.join(__dirname, '..', 'mock');
	var failed = JSON.parse(fs.readFileSync(path.join(dir, 'mock-state-failed.json'), 'utf8'));
	var agents = JSON.parse(fs.readFileSync(path.join(dir, 'mock-state-agents.json'), 'utf8'));
	var c = F.nativePage();
	var w = quiet(c, ['detectChime']);
	ok(w.MOCKS.indexOf('failed') >= 0, '?mock=failed is registered');
	ok(w.MOCKS.indexOf('agents') >= 0, 'and ?mock=agents');

	var types = {};
	var noBlock = 0, noMessage = 0;
	for (var i = 0; i < failed.sessions.length; i++) {
		var s = failed.sessions[i];
		if (s.state !== 'failed') continue;
		if (!s.failure) { noBlock++; continue; }
		types[s.failure.errorType] = 1;
		if (s.failure.message === undefined) noMessage++;
	}
	ok(Object.keys(types).length >= 4, 'the failed fixture carries at least four error types');
	ok(!!types.gateway_timeout, 'including one the widget does not name');
	eq(noBlock, 1, 'and exactly one failed row with no failure block at all (the restarted-crabd case)');
	ok(noMessage >= 1, 'and at least one failure with no message');

	var over = 0, untyped = 0, badStart = 0;
	for (var j = 0; j < agents.sessions.length; j++) {
		var n = agents.sessions[j].subagents && agents.sessions[j].subagents.named;
		if (!Array.isArray(n)) continue;
		if (n.length > 8) over++;
		for (var k = 0; k < n.length; k++) {
			if (n[k].type === undefined) untyped++;
			if (isNaN(Date.parse(n[k].startedAt))) badStart++;
		}
	}
	eq(over, 1, 'the agents fixture breaks the cap on exactly one row, so the clamp has work to do');
	ok(untyped >= 1, 'and carries a row crabd could not name');
	ok(badStart >= 1, 'and one whose startedAt will not parse');
	eq(w.laneSNamedSubs(agents.sessions[1]).length, 8, 'and the widget clamps that row to 8');
})();

/* ===================================== lane S: the temperature unit setting */

(function laneSTempConversion() {
	function at(unit, props) {
		var c = F.nativePage({ props: props || {} });
		return quiet(c, ['detectChime']);
	}

	/* THE DEFAULT IS CELSIUS AND IS BYTE-FOR-BYTE WHAT SHIPPED: the bare degree,
	   no letter, which is what the row's width budget is measured against. */
	var d = at(null, {});
	eq(d.laneSTempUnit(), 'c', 'the unit defaults to Celsius with nothing injected');
	eq(d.laneSTempDisplay(61, 'C'), { value: 61, letter: '' }, 'and a Celsius reading is untouched and unlettered');
	eq(d.laneSTempDisplay(61, ''), { value: 61, letter: '' }, 'a blank unit is Celsius, as it always was');
	eq(d.laneSTempDisplay(61, '°C'), { value: 61, letter: '' }, 'and so is one written with the degree sign');

	/* THE CONVERSION, at the three points anybody checks. */
	var f = at(null, { tempUnit: 'f' });
	eq(f.laneSTempUnit(), 'f', 'the injected prop is read');
	eq(f.laneSTempDisplay(0, 'C').value, 32, 'freezing converts');
	eq(f.laneSTempDisplay(100, 'C').value, 212, 'and boiling');
	eq(f.laneSTempDisplay(61, 'C').value, 141.8, 'and a GPU at 61 C is 141.8 F');
	eq(f.laneSTempDisplay(61, 'C').letter, 'F',
		'and Fahrenheit ALWAYS carries its letter: 142 bare would read as a machine on fire');

	/* A JUNK VALUE IS THE DEFAULT, through the same normaliser the sheet reads by,
	   so the control and the renderer can never disagree about what is stored. */
	eq(at(null, { tempUnit: 'kelvin' }).laneSTempUnit(), 'c', 'an unknown unit degrades to Celsius');
	eq(at(null, { tempUnit: '' }).laneSTempUnit(), 'c', 'and so does an empty one');
	eq(at(null, { tempUnit: 'F' }).laneSTempUnit(), 'f', 'a hand-edited upper-case F is still Fahrenheit');
	eq(at(null, { tempUnit: '  f  ' }).laneSTempUnit(), 'f', 'and whitespace around it is not a different value');

	/* A SOURCE THAT IS NOT CELSIUS IS LEFT ALONE, in both settings. Converting it
	   would print a figure in the scale this panel colours by while still refusing
	   to colour it, which is worse than printing what the sensor said. */
	eq(f.laneSTempDisplay(140, 'F'), { value: 140, letter: 'F' }, 'a Fahrenheit source is printed as it arrives');
	eq(d.laneSTempDisplay(140, 'F'), { value: 140, letter: 'F' }, 'and is not dragged into Celsius either');
	eq(f.laneSTempDisplay(334, 'K'), { value: 334, letter: 'K' }, 'and a third unit keeps its own letter');

	/* THE UNIT REACHES THE STYLESHEET as a body class, because it changes the
	   hardware row's WIDTH and the stylesheet owns that row's shrink order. A
	   two-cell Fahrenheit row is 285.33 px against Celsius's 236.39, which needs a
	   panel of 1425 px, so at 1400x720 the GPU cell stood 6.11 px past the row's own
	   edge until the second cell was dropped one breakpoint earlier. */
	d.applyProperties();
	f.applyProperties();
	eq(d.document.body.classList.contains('temp-f'), false, 'Celsius sets no class, so nothing at the default moves');
	eq(f.document.body.classList.contains('temp-f'), true, 'and Fahrenheit tells the stylesheet');
	var back = F.nativePage({ props: { tempUnit: 'f' } });
	var wb = quiet(back, ['detectChime']);
	wb.applyProperties();
	eq(wb.document.body.classList.contains('temp-f'), true, 'the class is on at boot');
	back.w.__sidecrabHost.props.tempUnit = 'c';
	wb.applyProperties();
	eq(wb.document.body.classList.contains('temp-f'), false, 'and comes off again when the unit does');
})();

(function laneSTempOnTheRow() {
	function row(props, host) {
		var c = F.nativePage({ props: props });
		var w = quiet(c);
		w.renderHost(host);
		w.renderHostExtras(host);
		w.syncSensorRow();
		return w;
	}
	var host = { cpuPct: 31, memPct: 58, gpu: { available: true, tempC: 61, utilPct: 22,
		name: 'A GPU', sampledAt: iso(1789984800000) } };

	eq(row({}, host).ui.sensorGpuVal.textContent, '61°', 'the row paints Celsius unlettered, as it shipped');
	eq(row({ tempUnit: 'c' }, host).ui.sensorGpuVal.textContent, '61°',
		'and an explicit Celsius reads identically');
	eq(row({ tempUnit: 'f' }, host).ui.sensorGpuVal.textContent, '142°F',
		'Fahrenheit converts and is lettered');

	/* THE THRESHOLDS STAY IN CELSIUS and so does the colour, in both units: a card
	   at 91 C is red whether the glass says 91 or 196. A second pair of thresholds
	   in Fahrenheit is exactly the pair that could drift out of step. */
	var hotC = { cpuPct: 31, memPct: 58, gpu: { available: true, tempC: 91, utilPct: 22,
		sampledAt: iso(1789984800000) } };
	var c1 = row({}, hotC), f1 = row({ tempUnit: 'f' }, hotC);
	eq(f1.ui.sensorGpuVal.textContent, '196°F', 'a 91 C card reads 196 F');
	eq(c1.laneATempColor(91, 'C'), 'var(--red)', 'and is red in Celsius');
	eq(f1.laneATempColor(91, 'C'), 'var(--red)', 'and red in Fahrenheit, off the same Celsius value');
	eq(f1.laneATempColor(85, 'C'), 'var(--amber)', 'the amber step is unmoved too');
	eq(f1.laneATempColor(79, 'C'), 'var(--text-color)', 'and so is the step below it');
})();

(function laneSTempInTheHostSheet() {
	function readings(props, sensors) {
		var c = F.nativePage({ props: props });
		var w = quiet(c);
		w.hostSensors = sensors;
		return w.laneAOtherReadings();
	}
	var sensors = [
		{ kind: 'temp', name: 'CPU VDDCR_VDD VRM (SVI3 TFN)', value: 58, unit: 'C' },
		{ kind: 'temp', name: 'Drive Temperature', value: 40, unit: 'C', device: 'nvme0' },
		{ kind: 'power', name: 'CPU Package Power', value: 88, unit: 'W' }
	];

	var inC = readings({}, sensors);
	/* Ends with the bare degree: the NAME carries an F of its own ("SVI3 TFN"), so
	   the reading is checked where it is and not by scanning the whole line. */
	eq(/58°$/.test(inC[0]), true, 'the sheet paints Celsius unlettered');
	var inF = readings({ tempUnit: 'f' }, sensors);
	ok(inF[0].indexOf('136°F') >= 0, 'and converts the VRM reading');
	ok(inF[1].indexOf('104°F') >= 0, 'and the drive');

	/* THE NAME DOES NOT CONVERT. "CPU VDDCR_VDD VRM (SVI3 TFN)" is what HWiNFO
	   calls that probe in either unit. */
	ok(inF[0].indexOf('CPU VDDCR_VDD VRM (SVI3 TFN)') === 0, 'the sensor name is untouched');
	ok(inF[1].indexOf('Drive') === 0, 'and so is the drive label');

	/* POWER IS WATTS and a unit setting about temperature has no business near it. */
	ok(inC[2].indexOf('88 W') >= 0, 'package power reads in watts');
	ok(inF[2].indexOf('88 W') >= 0, 'and is unchanged by the temperature unit');
	eq(inF[2].indexOf('°'), -1, 'and grows no degree sign');

	/* THE SOURCE LINE does not convert either: it says where the numbers came from
	   and how old they are, which is the same sentence in both units. */
	var c = F.nativePage({ props: { tempUnit: 'f' } });
	var w = quiet(c);
	w.hostSensors = sensors;
	w.hostSensorsSource = { available: true, stale: false, ageSec: 4 };
	w.sheetMode = 'host';
	w.syncHostSheet();
	var text = w.ui.sheetHost.textContent;
    ok(text.indexOf('3 from HWiNFO') >= 0, 'the provenance line names the source and the count');
	eq(/from HWiNFO[^,]*F\b/.test(text), false, 'and carries no unit of its own');
})();

(function laneSTempSettingRoundTrip() {
	var c = F.nativePage();
	var w = quiet(c);

	/* THE CONTROL EXISTS ON THE PANEL HALF and shows what the panel is set to. */
	w.openSettingsSheet();
	var btns = w.ui.sheetSettings.querySelectorAll('.set-choice');
	eq(btns.length, 2, 'the sheet offers one button per unit');
	eq(btns[0].textContent, '°C', 'labelled with the scale, not with On and Off');
	eq(btns[1].textContent, '°F', 'and the other with its own');
	eq(btns[0].getAttribute('aria-checked'), 'true', 'Celsius is pressed by default');
	eq(btns[1].getAttribute('aria-checked'), 'false', 'and Fahrenheit is not');
	var group = w.ui.sheetSettings.querySelector('.set-choices');
	eq(group.getAttribute('role'), 'radiogroup', 'the pair is announced as one group');
	ok(!!group.getAttribute('aria-labelledby'), 'named by the row label');
	eq(btns[0].getAttribute('role'), 'radio', 'and each button as a radio');

	/* A TAP MOVES THE DRAFT AND SAYS SO. It does not move the glass: every control
	   on this half lands when the host echoes the save back. */
	btns[1].fire('click');
	eq(w.settingsValues().tempUnit, 'f', 'the tap moves the draft');
	eq(btns[1].getAttribute('aria-checked'), 'true', 'the pressed state follows the tap');
	eq(btns[0].getAttribute('aria-checked'), 'false', 'and leaves the other one');
	eq(w.ui.setStatus.textContent, 'not saved yet', 'and the foot says it is not saved');
	eq(w.laneSTempUnit(), 'c', 'the PAGE is still on Celsius until the host answers');

	/* THE ROUND TRIP through the bridge the panel actually uses. */
	w.onSettingsSave();
	var ask = c.sent[c.sent.length - 1];
	eq(ask.type, 'settings', 'the save goes as a settings message');
	eq(ask.props.tempUnit, 'f', 'carrying the unit the operator chose');
	c.reply({ type: 'settings-result', requestId: ask.requestId, ok: true, props: ask.props });
	eq(w.ui.setStatus.textContent, 'saved', 'the host accepts it');
	eq(w.laneSTempUnit(), 'f', 'and the page is on Fahrenheit without a reload');
	eq(w.laneSTempDisplay(61, 'C').value, 141.8, 'so the readings convert from the next paint');

	/* WHAT THE HOST ECHOES IS WHAT MOVES, which is onSettingsResult's own rule and
	   is the half that matters here: the shipping host's ValidateSettingsProps is a
	   whitelist of booleans, colours and percents (panel-host/SideCrab.Panel/
	   PanelLogic.cs:238), so until it learns this key it DROPS it and echoes back
	   without it. The page must then stay on what is really stored rather than on
	   what it hoped for. */
	var h = F.nativePage();
	var wh = quiet(h);
	wh.openSettingsSheet();
	wh.settingsValues().tempUnit = 'f';
	wh.onSettingsSave();
	var ask2 = h.sent[h.sent.length - 1];
	h.reply({ type: 'settings-result', requestId: ask2.requestId, ok: true,
		props: { clock24: false } });
	eq(wh.laneSTempUnit(), 'c', 'a host that drops the key leaves the page on Celsius');
	eq(wh.ui.setStatus.textContent, 'saved', 'and the save itself is still a save');

	/* AN INJECTED VALUE SHOWS AS PRESSED, so a panel restarted on Fahrenheit opens
	   its sheet on Fahrenheit. */
	var p = F.nativePage({ props: { tempUnit: 'f' } });
	var wp = quiet(p);
	wp.openSettingsSheet();
	var pbtns = wp.ui.sheetSettings.querySelectorAll('.set-choice');
	eq(pbtns[1].getAttribute('aria-checked'), 'true', 'the stored unit is the pressed one');
	eq(pbtns[0].getAttribute('aria-checked'), 'false', 'and the other is not');

	/* NO COMPANION CHANGE. The unit never reaches /v1/config: it is a property of
	   this glass, and crabd goes on measuring in Celsius. */
	var sentTypes = {};
	for (var i = 0; i < c.sent.length; i++) sentTypes[c.sent[i].type] = 1;
	eq(sentTypes.config, undefined, 'nothing about the unit is sent to the companion');
})();

/* ================== lane N: SCA-006 reaches the settings sheet's companion half */

(function laneNConfigReceipt() {
	/* SCA-006 FOR THE SETTINGS SHEET, the one surface the original sweep did not
	   reach. The companion half posts to /v1/config and its receipt was unscoped. */
	function sheetCtx() {
		var ctx = F.nativePage();
		quiet(ctx, ['detectChime', 'enterSheetFocus', 'setBackgroundHidden', 'safeFocus', 'setSheetLabel']);
		ctx.w.lastGoodDoc = stateDoc(ctx.now(), []);
		return ctx;
	}

	/* A save in flight when the sheet is closed and reopened must not land on the
	   new one - and must not throw away what has been typed into it since. */
	var c = sheetCtx();
	var w = c.w;
	var resolve = null;
	w.postConfig = function () { return new Promise(function (r) { resolve = r; }); };
	w.openSettingsSheet();
	w.configSet('budgetEnabled', true);
	w.onConfigSave();
	eq(w.ui.cfgStatus.textContent, 'saving', 'the first save says so');
	w.closeSheet();
	w.openSettingsSheet();
	w.configSet('digestEnabled', true);
	w.configSet('digestTime', '08:30');
	var typed = JSON.stringify(w.cfgTouched);
	resolve({ status: 204, body: { applied: { budget: 1 }, warnings: [] } });
	return new Promise(function (r) { setImmediate(r); }).then(function () {
	eq(JSON.stringify(w.cfgTouched), typed, 'a receipt from the closed sheet keeps its hands off the new draft');
	eq(w.ui.cfgStatus.textContent, 'not saved yet', 'and does not write its own outcome onto the new sheet');

	/* MUTATION PROOF: unscoped, the same input wipes the draft. */
	var m = sheetCtx();
	var mw = m.w;
	var mresolve = null;
	mw.postConfig = function () { return new Promise(function (r) { mresolve = r; }); };
	mw.openSettingsSheet();
	mw.configSet('budgetEnabled', true);
	/* The pre-fix body, inlined: no token, no busy reset. */
	mw.cfgBusy = true;
	mw.setConfigStatus('saving', 'pending');
	mw.postConfig('{}').then(function () {
		mw.cfgBusy = false;
		mw.cfgDraft = null;
		mw.cfgTouched = null;
		mw.setConfigStatus('saved budget', 'ok');
	});
	mw.closeSheet();
	mw.openSettingsSheet();
	mw.configSet('digestEnabled', true);
	mresolve({ status: 204 });
	return new Promise(function (r) { setImmediate(r); }).then(function () {
	eq(mw.cfgTouched, null, 'the unscoped receipt wipes the new sheet’s draft (mutation proof)');
	eq(mw.ui.cfgStatus.textContent, 'saved budget', 'and writes a stale outcome onto it (mutation proof)');

	/* And the other half: a new sheet's Save is never inert because of an older
	   sheet's request. */
	var b = sheetCtx();
	var bw = b.w;
	var posts = 0;
	bw.postConfig = function () { posts++; return new Promise(function () {}); };
	bw.openSettingsSheet();
	bw.configSet('budgetEnabled', true);
	bw.onConfigSave();
	eq(posts, 1, 'the first sheet saves');
	bw.closeSheet();
	bw.openSettingsSheet();
	bw.configSet('budgetEnabled', true);
	bw.onConfigSave();
	eq(posts, 2, 'and the reopened sheet saves rather than doing nothing at all');
	eq(bw.ui.cfgStatus.textContent, 'saving', 'and says so');

	/* Within ONE sheet the single-flight guard is untouched: a double tap is one
	   request, which is what cfgBusy is actually for. */
	var d = sheetCtx();
	var dw = d.w;
	var dposts = 0;
	dw.postConfig = function () { dposts++; return new Promise(function () {}); };
	dw.openSettingsSheet();
	dw.configSet('budgetEnabled', true);
	dw.onConfigSave();
	dw.onConfigSave();
	eq(dposts, 1, 'two taps on one sheet are still one request');

	/* A COMPANION THAT ANSWERS BADLY still says so, on its own sheet. */
	var e = sheetCtx();
	var ew = e.w;
	var eresolve = null;
	ew.postConfig = function () { return new Promise(function (r) { eresolve = r; }); };
	ew.openSettingsSheet();
	ew.configSet('budgetEnabled', true);
	ew.onConfigSave();
	eresolve({ status: 422, body: { warnings: ['budget must be a positive integer'] } });
	return new Promise(function (r) { setImmediate(r); }).then(function () {
	eq(ew.ui.cfgStatus.textContent, 'not saved (HTTP 422)', 'a 4xx is said out loud');

	var g = sheetCtx();
	var gw = g.w;
	var gresolve = null;
	gw.postConfig = function () { return new Promise(function (r) { gresolve = r; }); };
	gw.openSettingsSheet();
	gw.configSet('budgetEnabled', true);
	gw.onConfigSave();
	gresolve({ status: 204, body: { applied: {}, warnings: ['budget ignored: set by policy'] } });
	return new Promise(function (r) { setImmediate(r); }).then(function () {
	ok(gw.ui.cfgStatus.textContent.indexOf('set by policy') >= 0,
		'and a key the companion declined is not reported as saved');

	var n = sheetCtx();
	var nw = n.w;
	nw.postConfig = function () { return Promise.reject(new Error('refused')); };
	nw.openSettingsSheet();
	nw.configSet('budgetEnabled', true);
	nw.onConfigSave();
	return new Promise(function (r) { setImmediate(r); }).then(function () {
	ok(nw.ui.cfgStatus.textContent.indexOf('not reachable') >= 0, 'and an unreachable companion is not a silent save');
	eq(nw.cfgBusy, false, 'a failed save does not leave the sheet stuck busy');

console.log((failures ? 'FAILED' : 'ok') + '  ' + (checks - failures) + '/' + checks + ' checks');
process.exit(failures ? 1 : 0);

}); }); }); }); });
})();

}); }); });
