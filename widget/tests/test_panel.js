/* SideCrab widget — the transport tests.
 *
 *   node widget/tests/test_panel.js
 *
 * WHY A VM AND NOT A MODULE, and the document stub that makes it work, both live
 * in _harness.js — shared with test_ordering.js.
 *
 * WHAT IS PINNED. The panel now runs in two places: inside iCUE, loaded off the
 * filesystem, where it must name crabd's host and port outright; and served by
 * crabd itself over http, where it must use SAME-ORIGIN relative paths — an
 * absolute http://127.0.0.1:9999 from a page served as http://localhost:9999
 * is a CROSS-ORIGIN request, and crabd's Origin gate answers those with 403.
 * Every POST additionally carries X-SideCrab-Panel, without which crabd answers
 * 403 "panel header required" — including on the text/plain retry, which is the
 * attempt an operator's browser is most likely to actually make.
 *
 * loadWidget takes the location and a fetch stub, because the whole point is
 * that the same file behaves differently under different origins.
 */
'use strict';

var fs = require('fs');
var path = require('path');

var harness = require('./_harness.js');

function loadWidget(opts) {
  opts = opts || {};
  if (!Object.prototype.hasOwnProperty.call(opts, 'domReason')) opts.domReason = 'the transport tests build no DOM';
  return harness.loadWidget(opts);
}

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

function served(props, fetchStub) {
  return loadWidget({
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '' },
    props: props,
    fetch: fetchStub
  });
}

/* Header lookup is case-insensitive. What fetch is handed here is a plain object,
   whose keys are case-SENSITIVE, but HTTP header names are not — so a browser, and
   crabd, would match a differently-cased spelling that an exact-key lookup here
   would report as missing. Fold the case and the test answers the wire's question. */
function headerOf(init, name) {
  var h = (init && init.headers) || {};
  var keys = Object.keys(h), i;
  for (i = 0; i < keys.length; i++) if (keys[i].toLowerCase() === name.toLowerCase()) return h[keys[i]];
  return undefined;
}

/* ------------------------------------------------------------------ baseUrl */

eq(served().baseUrl(), '',
  'served over http the base is empty, so every path is same-origin and relative');

eq(loadWidget({ location: { protocol: 'https:', host: 'localhost:9999', href: 'https://localhost:9999/', search: '' } }).baseUrl(), '',
  'https is served too');

eq(loadWidget().baseUrl(), 'http://127.0.0.1:9999',
  'off the filesystem the iCUE case names crabd on the default port');

eq(loadWidget({ props: { crabdPort: '1234' } }).baseUrl(), 'http://127.0.0.1:1234',
  'crabdPort still moves the iCUE case');

eq(loadWidget({ location: { search: '', href: 'about:blank' } }).baseUrl(), 'http://127.0.0.1:9999',
  'a location that reports no protocol reads as the iCUE case, not as served');

/* No location OBJECT at all, not merely no protocol on one. baseUrl() guards this
   with `window.location && ...` inside a try/catch; the harness distinguishes an
   absent `location` key from an explicit null so this case is reachable, because a
   panel that only works when someone supplies a location would pass every other
   check here and then go dark in a webview that exposes none. */
eq(loadWidget({ location: null }).baseUrl(), 'http://127.0.0.1:9999',
  'a null location still reaches crabd rather than throwing or going relative');

eq(loadWidget({ props: { crabdPort: 'nope' } }).baseUrl(), 'http://127.0.0.1:9999',
  'a crabdPort with no digits falls back rather than building a broken URL');

eq(served({ crabdPort: '1234' }).baseUrl(), '',
  'crabdPort cannot break the served case: the origin decides, not the property');

/* -------------------------------------------------------------- endpointUrl */

eq(served().endpointUrl(), '/v1/state', 'the served state feed is a relative path');
eq(loadWidget().endpointUrl(), 'http://127.0.0.1:9999/v1/state', 'the iCUE state feed is absolute');

/* ------------------------------------------- the crabdPort default, stated twice */

/* index.html's meta and the strProp fallback in baseUrl() both state it, and iCUE
   uses the meta while a plain browser uses the fallback — so a disagreement is a
   panel that reaches a different port depending on where it is running. */
(function () {
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var m = /name="x-icue-property"\s+content="crabdPort"[^>]*data-default="'(\d+)'"/.exec(html);
  ok(!!m, 'index.html declares a crabdPort default');
  eq(m && m[1], '9999', 'the declared crabdPort default is crabd\'s port');
  eq(loadWidget().baseUrl(), 'http://127.0.0.1:' + (m && m[1]),
    'the JS fallback and the meta default name the same port');
})();

/* ------------------------------------------------------ postJson (async) */

var pending = [];

/* Every POST carries X-SideCrab-Panel. crabd answers a POST without it with 403
   "panel header required", and the panel's own 403 handling reads that as a wrong
   pairing code — so a dropped header would show up as a lie, not as an outage. */
(function () {
  var calls = [];
  var ctx = served(null, function (url, init) {
    calls.push({ url: url, init: init });
    return Promise.resolve({ status: 204 });
  });
  pending.push(ctx.postJson('/v1/action', '{"action":"ack"}').then(function () {
    eq(calls.length, 1, 'the first attempt is the only one when it resolves');
    eq(calls[0].url, '/v1/action', 'the served POST is a relative path');
    eq(headerOf(calls[0].init, 'X-SideCrab-Panel'), '1', 'the POST carries the panel header');
    eq(headerOf(calls[0].init, 'Content-Type'), 'application/json', 'the JSON content type is unchanged');
  }));
})();

/* The text/plain retry is the attempt a real browser is most likely to make: the
   JSON content type makes the POST preflighted. The header rides both. */
(function () {
  var calls = [];
  var ctx = served(null, function (url, init) {
    calls.push({ url: url, init: init });
    return calls.length === 1 ? Promise.reject(new Error('preflight refused')) : Promise.resolve({ status: 204 });
  });
  pending.push(ctx.postJson('/v1/action', '{"action":"ack"}').then(function () {
    eq(calls.length, 2, 'a rejected JSON attempt falls back once');
    eq(headerOf(calls[1].init, 'Content-Type'), 'text/plain;charset=UTF-8', 'the fallback content type is unchanged');
    eq(headerOf(calls[1].init, 'X-SideCrab-Panel'), '1', 'the fallback attempt carries the panel header too');
  }));
})();

/* --------------------------------------------------------- fetchHistory (async) */

(function () {
  var calls = [];
  var ctx = served(null, function (url) {
    calls.push(url);
    return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ events: [] }); } });
  });
  pending.push(ctx.fetchHistory('2026-09-01').then(function () {
    eq(calls[0], '/v1/history?day=2026-09-01', 'the served day fetch is relative too');
  }));
})();

(function () {
  var calls = [];
  var ctx = loadWidget({
    fetch: function (url) {
      calls.push(url);
      return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ events: [] }); } });
    }
  });
  pending.push(ctx.fetchHistory('2026-09-01').then(function () {
    eq(calls[0], 'http://127.0.0.1:9999/v1/history?day=2026-09-01', 'the iCUE day fetch names crabd');
  }));
})();

/* ------------------------------------------------- the settings adapter (A) */

/* WHAT REPLACED THE iCUE PROPERTY BRIDGE. Inside iCUE every property arrives as
   a same-named injected global and getIcueProperty reads it back. Served in a
   browser there is no bridge at all, so the same chokepoint reads ONE namespaced
   object in localStorage under the fixed key "sidecrab" — the vendor's
   one-object-per-widget shape, kept for the same reason it was adopted: a panel
   that scattered bare keys across its origin would be sharing a namespace with
   anything else served from it, and the read-modify-write is what lets an older
   build save a pin without deleting a newer build's settings.
   "Inside iCUE" is decided ONCE, off the uniqueId probe, so a browser can never
   be talked into the injected-global path and vice versa. */

/* A localStorage stand-in. `mode` decides how it misbehaves: 'ok' stores,
   'throwGet' throws on every access (the private-browsing shape), 'throwSet'
   stores nothing and throws on the write (the quota shape). */
function fakeStorage(mode, seed) {
  var data = seed || {};
  return {
    data: data,
    getItem: function (k) {
      if (mode === 'throwGet') throw new Error('storage refused the read');
      return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null;
    },
    setItem: function (k, v) {
      if (mode === 'throwGet' || mode === 'throwSet') throw new Error('storage refused the write');
      data[k] = String(v);
    }
  };
}

function panel(opts) {
  opts = opts || {};
  return loadWidget({
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: opts.search || '' },
    props: opts.props,
    storage: Object.prototype.hasOwnProperty.call(opts, 'storage') ? opts.storage : fakeStorage('ok', opts.seed),
    console: opts.console
  });
}

/* One round trip per property KIND, because each one arrives as a different
   JavaScript type and the readers tolerate strings for all of them. */
(function () {
  var st = fakeStorage('ok');
  var ctx = panel({ storage: st });
  ctx.writePanelProperty('clock24', true);
  ctx.writePanelProperty('quietStart', '23:30');
  ctx.writePanelProperty('toastThreshold', 240);
  ctx.writePanelProperty('accentColor', '#123456');
  eq(ctx.use24Clock(), true, 'a switch round-trips through the settings adapter');
  eq(ctx.strProp('quietStart', '22:00'), '23:30', 'a textfield round-trips');
  eq(ctx.desiredToastConfig().toast.thresholdSec, 240, 'a slider round-trips as a number');
  eq(ctx.strProp('accentColor', '#BE7E6E'), '#123456', 'a colour round-trips');
  var stored = JSON.parse(st.data.sidecrab || '{}');
  eq(stored.clock24, true, 'every property lives inside ONE object under the fixed key "sidecrab"');
  eq(Object.keys(st.data).sort(), ['sidecrab'], 'nothing is scattered across the origin as a bare key');
})();

/* audit-F2, one wave on: the read-modify-write already protected unknown KEYS
   and unknown VALUES of a known key against a pin save. A property write is the
   third writer of the same object and has to keep the same promise. */
(function () {
  var st = fakeStorage('ok', {
    sidecrab: JSON.stringify({ futureThing: 7, sessionFilter: 'archived', clock24: true })
  });
  var ctx = panel({ storage: st });
  ctx.writePanelProperty('alertFlash', false);
  var props = JSON.parse(st.data.sidecrab);
  eq(props.futureThing, 7, 'audit-F2: an unknown top-level key survives a property write');
  eq(props.sessionFilter, 'archived', 'audit-F2: an unknown VALUE of a known key survives a property write');
  eq(props.clock24, true, 'a property this write did not touch survives it');
  eq(props.alertFlash, false, 'the written property lands');
})();

/* A pin save must not delete the settings either — the same object, the other
   writer. */
(function () {
  var st = fakeStorage('ok', { sidecrab: JSON.stringify({ clock24: true, futureThing: 'keep me' }) });
  var ctx = panel({ storage: st });
  ctx.loadPrefs();
  ctx.togglePin('sess-1');
  var props = JSON.parse(st.data.sidecrab);
  eq(props.clock24, true, 'a pin save leaves the settings in the object it shares');
  eq(props.futureThing, 'keep me', 'and leaves a future build\'s key alone');
  ok(props.pinnedSessions && props.pinnedSessions['sess-1'], 'the pin lands beside them');
})();

/* Private browsing and a full quota both THROW. A lost setting is a nuisance;
   a panel that stopped rendering over one would be the failure. */
(function () {
  var lines = [];
  var ctx = panel({ storage: fakeStorage('throwGet'), console: { log: function (m) { lines.push(String(m)); } } });
  var threw = null;
  try { ctx.writePanelProperty('clock24', true); ctx.render(); } catch (e) { threw = e; }
  ok(!threw, 'a throwing localStorage never breaks a write or a render  (' + (threw && threw.message) + ')');
  eq(ctx.use24Clock(), true, 'the setting is kept in memory for the session instead');
  ok(lines.some(function (l) { return /memory/i.test(l); }),
    'one console line says so, and nothing goes on the glass');
})();

(function () {
  var lines = [];
  var st = fakeStorage('throwSet');
  var ctx = panel({ storage: st, console: { log: function (m) { lines.push(String(m)); } } });
  ctx.writePanelProperty('clock24', true);
  eq(ctx.use24Clock(), true, 'a storage that reads but refuses the write degrades the same way');
  eq(st.data.sidecrab, undefined, 'and nothing was written');
})();

/* The pairing code is read LIVE on every tap — never cached in a variable — so
   a code pasted into the settings sheet is in force for the next Approve. */
(function () {
  var st = fakeStorage('ok', { sidecrab: JSON.stringify({ panelToken: 'AAAA-BBBB' }) });
  var ctx = panel({ storage: st });
  eq(ctx.pairingCode(), 'AAAA-BBBB', 'the pairing code is read from the panel store');
  st.data.sidecrab = JSON.stringify({ panelToken: 'CCCC-DDDD' });
  eq(ctx.pairingCode(), 'CCCC-DDDD', 'read LIVE: a value changed between two reads is seen');
})();

/* The injected-global path is untouched, and the store is not consulted at all
   when the host is supplying properties of its own. */
(function () {
  var st = fakeStorage('ok', { sidecrab: JSON.stringify({ clock24: true, panelToken: 'BROWSER' }) });
  var ctx = loadWidget({ props: { uniqueId: 'abc-123', clock24: false }, storage: st });
  ok(ctx.insideIcue(), 'an injected uniqueId is what "inside iCUE" means');
  eq(ctx.use24Clock(), false, 'inside iCUE the injected property wins');
  eq(ctx.pairingCode(), '', 'inside iCUE the browser store is not consulted at all');
  ctx.loadPrefs();
  eq(ctx.prefsStoreKey, 'abc-123', 'inside iCUE the prefs key is still the host-injected uniqueId');
})();

(function () {
  var ctx = panel();
  ok(!ctx.insideIcue(), 'a served page with no injected uniqueId is not inside iCUE');
  ctx.loadPrefs();
  eq(ctx.prefsStoreKey, 'sidecrab', 'outside iCUE the prefs key is the origin\'s one object');
})();

/* THE ACCENT DEFAULT IS STATED THREE TIMES and they move together: :root in the
   stylesheet, the property meta, and the strProp fallback that wins at runtime.
   A drift renders one colour in a browser and another on the glass. */
(function () {
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var css = fs.readFileSync(path.join(__dirname, '..', 'styles', 'sidecrab.css'), 'utf8');
  var js = fs.readFileSync(harness.SRC, 'utf8');
  var meta = /name="x-icue-property"\s+content="accentColor"[^>]*data-default="'(#[0-9A-Fa-f]{6})'"/.exec(html);
  var root = /\n\t--accent:\s*(#[0-9A-Fa-f]{6});/.exec(css);
  var fallback = /strProp\('accentColor',\s*'(#[0-9A-Fa-f]{6})'\)/.exec(js);
  ok(!!meta, 'index.html states the accent default');
  ok(!!root, ':root in sidecrab.css states the accent default');
  ok(!!fallback, 'the strProp fallback states the accent default');
  eq([meta && meta[1], root && root[1]], [fallback && fallback[1], fallback && fallback[1]],
    'the meta and :root both state the accent the JS fallback wins with');
})();

/* --------------------------------------------------- the settings sheet (B) */

/* THE SPEC IS index.html, NOT A SECOND LIST. The <meta name="x-icue-property">
   tags declare every setting with its type, range and default, and the
   <script id="x-icue-groups"> block declares the titles, the ordering and the
   help prose. Both were written and reviewed for the iCUE console; the browser
   sheet reads the same declarations at runtime, so a property added to
   index.html is in the sheet without a second edit — and this suite drives the
   shipping markup, so it says the same thing. */

function settingsPanel(opts) {
  opts = opts || {};
  return loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: opts.search || '' },
    props: opts.props,
    storage: Object.prototype.hasOwnProperty.call(opts, 'storage') ? opts.storage : fakeStorage('ok', opts.seed)
  });
}

function control(ctx, name) {
  return ctx.ui.sheetSettings ? ctx.ui.sheetSettings.querySelector('[data-prop=' + name + ']') : null;
}

function setControl(ctx, name, value) {
  var el = control(ctx, name);
  if (!el) return null;
  if (el.getAttribute('type') === 'checkbox') el.checked = !!value;
  else el.value = String(value);
  el.dispatch('change', { target: el });
  return el;
}

/* The three that have no meaning outside iCUE: two sensor combo boxes with no
   bridge to populate them, and a touch recorder built to find out what the iCUE
   webview forwards. */
var SETTINGS_NOT_IN_BROWSER = ['cpuTempSensor', 'gpuTempSensor', 'touchDiag'];

(function () {
  var ctx = settingsPanel();
  ctx.openSettingsSheet();
  var metas = ctx.document.querySelectorAll('meta[name=x-icue-property]');
  ok(metas.length >= 18, 'index.html declares the properties this sheet renders  (' + metas.length + ')');
  for (var i = 0; i < metas.length; i++) {
    var name = metas[i].getAttribute('content');
    var raw = metas[i].getAttribute('data-default');
    var el = control(ctx, name);
    if (SETTINGS_NOT_IN_BROWSER.indexOf(name) !== -1) {
      ok(!el, name + ' is not rendered outside iCUE');
      continue;
    }
    if (!el) { ok(false, name + ' has a control in the settings sheet'); continue; }
    /* The declared default, as index.html writes it: quoted for a string,
       bare for a number or a switch. */
    var want = /^'(.*)'$/.test(raw) ? raw.slice(1, -1) : raw;
    if (el.getAttribute('type') === 'checkbox') {
      eq(el.checked, want === 'true', name + ' opens on its declared default');
    } else if (name === 'crabStyle') {
      eq(el.value, want === 'true' ? 'auto' : 'plain', 'crabStyle is a real enum control on its declared default');
    } else {
      eq(el.value, want, name + ' opens on its declared default');
    }
  }
  /* The slider metas carry a range, and a control that ignored it would let a
     value crabd rejects be typed in. */
  var toast = control(ctx, 'toastThreshold');
  eq([toast.getAttribute('type'), toast.getAttribute('min'), toast.getAttribute('max'), toast.getAttribute('step')],
    ['range', '30', '600', '10'], 'a slider carries the meta\'s own min, max and step');
  ok(/\bs\b/.test(ctx.ui.sheetSettings.textContent), 'and its unit label');
})();

/* The groups block is the spec for the ordering, and the two groups that are
   empty outside iCUE must not render as empty headings. */
(function () {
  var ctx = settingsPanel();
  ctx.openSettingsSheet();
  var titles = [];
  var heads = ctx.ui.sheetSettings.querySelectorAll('.set-group-title');
  for (var i = 0; i < heads.length; i++) titles.push(heads[i].textContent);
  eq(titles, ['SideCrab', 'Approvals', 'Widget Personalization'],
    'the sheet renders the x-icue-groups block in its own order, minus the groups iCUE owns');
  /* A property no group claims renders NOWHERE, in the console and in this
     sheet alike — so the invariant is that every declared property has a home,
     and it is asserted here rather than papered over with a fallback group. */
  var claimed = {};
  JSON.parse(ctx.document.getElementById('x-icue-groups').textContent).forEach(function (g) {
    (g.properties || []).forEach(function (n) { claimed[n] = (claimed[n] || 0) + 1; });
  });
  var metas = ctx.document.querySelectorAll('meta[name=x-icue-property]');
  for (var j = 0; j < metas.length; j++) {
    var nm = metas[j].getAttribute('content');
    eq(claimed[nm], 1, nm + ' is claimed by exactly one group');
  }
})();

/* Every change writes through the adapter, and applyProperties() is what iCUE's
   onDataUpdated used to call — so the colour actually lands on :root. */
(function () {
  var st = fakeStorage('ok');
  var ctx = settingsPanel({ storage: st });
  ctx.openSettingsSheet();
  setControl(ctx, 'clock24', true);
  eq(ctx.use24Clock(), true, 'a switch change writes the property');
  eq(JSON.parse(st.data.sidecrab).clock24, true, 'and persists it');
  setControl(ctx, 'accentColor', '#123456');
  eq(ctx.document.documentElement.style.getPropertyValue('--accent'), '#123456',
    'a change calls applyProperties, so the accent lands on :root');
  setControl(ctx, 'crabStyle', 'plain');
  eq(ctx.crabPlain(), true, 'the crabStyle enum feeds the reader that already accepted the words');
})();

/* THE WRITE-ONLY-WHEN-MOVED RULE (v0.16.0) survives the new control. crabd
   PRESERVES toast.approvalThresholdSec when a write omits it, so a sheet that
   sent its default on open would delete a hand-edited config.json value. */
(function () {
  var ctx = settingsPanel();
  ctx.openSettingsSheet();
  var body = ctx.desiredToastConfig();
  ok(!Object.prototype.hasOwnProperty.call(body.toast, 'approvalThresholdSec'),
    'a settings sheet that was only OPENED does not send the approval threshold');
  setControl(ctx, 'approvalThreshold', 45);
  eq(ctx.desiredToastConfig().toast.approvalThresholdSec, 45,
    'once the control has been moved the key rides every toast write');
})();

/* The pairing code is a secret typed into a panel anyone can walk up to: masked,
   with a deliberate reveal, and it may not touch any other key. */
(function () {
  var st = fakeStorage('ok', { sidecrab: JSON.stringify({ clock24: true, futureThing: 1 }) });
  var ctx = settingsPanel({ storage: st });
  ctx.openSettingsSheet();
  var el = control(ctx, 'panelToken');
  eq(el.getAttribute('type'), 'password', 'the pairing code is masked');
  setControl(ctx, 'panelToken', 'ABCD-1234');
  var after = JSON.parse(st.data.sidecrab);
  eq(after.panelToken, 'ABCD-1234', 'the pairing input writes panelToken');
  eq(after.clock24, true, 'and touches no other key');
  eq(after.futureThing, 1, 'including a key it has never heard of');
  eq(ctx.pairingCode(), 'ABCD-1234', 'the code is in force for the next Approve');
  var show = ctx.ui.sheetSettings.querySelector('#settingsTokenShow');
  ok(!!show, 'there is a show toggle');
  show.click();
  eq(el.getAttribute('type'), 'text', 'which reveals it');
  show.click();
  eq(el.getAttribute('type'), 'password', 'and hides it again');
  ok(/install\.sh --pairing-code/.test(ctx.ui.sheetSettings.textContent),
    'the help text says how to print the code');
})();

/* Two ways in, both routed to the one opener. */
(function () {
  var ctx = settingsPanel();
  ctx.ui.settingsChip.click();
  eq(ctx.sheetMode, 'settings', 'the gear beside the filter chips opens the settings sheet');
  ctx.closeSheet();
  ctx.document.dispatch('keydown', { key: 's' });
  eq(ctx.sheetMode, 'settings', 'and so does the s key');
  /* A rebuild would throw away a half-typed value, so the proof that the key is
     inert with the sheet open is a value the STORE has never seen surviving it. */
  var token = control(ctx, 'panelToken');
  token.value = 'HALF-TYPED';
  ctx.document.dispatch('keydown', { key: 's' });
  eq(control(ctx, 'panelToken').value, 'HALF-TYPED', 's while a sheet is open does not reopen or rebuild it');
  ctx.closeSheet();
  /* And a letter typed into a field is a character, not a command. */
  ctx.document.activeElement = control(ctx, 'panelToken');
  ctx.document.dispatch('keydown', { key: 's' });
  eq(ctx.sheetMode, null, 's while an input has focus is a character, not a shortcut');
  ctx.document.activeElement = ctx.document.body;
})();

/* The Windows toast surface is not what a macOS panel notifies through, and the
   label was already the one place a narrowed mute goes wrong silently. */
(function () {
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var tr = fs.readFileSync(path.join(__dirname, '..', 'translation.json'), 'utf8');
  ok(html.indexOf('Desktop Toast Alerts') === -1, 'index.html no longer says "Desktop Toast Alerts"');
  ok(tr.indexOf('Desktop Toast Alerts') === -1, 'translation.json no longer says it either');
  ok(html.indexOf('Desktop Notifications') !== -1, 'the master switch is "Desktop Notifications"');
  ok(tr.indexOf('Desktop Notifications') !== -1, 'and the catalogue agrees');
})();

/* ---------------------------------------------------------------------- done */

Promise.all(pending).then(function () {
  console.log((failures ? 'FAILED' : 'ok') + '  ' + (checks - failures) + '/' + checks + ' checks');
  process.exit(failures ? 1 : 0);
}, function (err) {
  console.log('FAILED  an async check threw: ' + (err && err.stack || err));
  process.exit(1);
});
