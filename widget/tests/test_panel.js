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

/* WHICH HOST IS A QUESTION ABOUT EXISTENCE, NOT ABOUT VALUE (review). The
   property reader treats '' as "unset and fall back to the default", which is
   right for a property — but iCUE injecting an EMPTY uniqueId is still iCUE, and
   reading that as "no host" would flip a widget on the glass onto the browser
   store: pins and settings written to a key the panel then stops reading, and
   the injected properties beaten by whatever a previous browser session left
   behind. The probe asks whether the identifier is DECLARED. */
(function () {
  var st = fakeStorage('ok', { sidecrab: JSON.stringify({ clock24: true, panelToken: 'BROWSER' }) });
  var ctx = loadWidget({ props: { uniqueId: '', clock24: false }, storage: st });
  ok(ctx.insideIcue(), 'an injected but empty uniqueId is still iCUE');
  eq(ctx.pairingCode(), '', 'so the browser store is still not consulted');
  eq(ctx.use24Clock(), false, 'and the injected property still wins');
  ctx.loadPrefs();
  /* An empty id is no key to store under, which is the pre-existing degrade:
     memory only, never the browser origin's object. */
  eq(ctx.prefsStoreKey, null, 'an empty id stores nowhere rather than in the browser object');
})();

(function () {
  var ctx = loadWidget({ props: { uniqueId: null }, storage: fakeStorage('ok') });
  ok(ctx.insideIcue(), 'a declared uniqueId of null is still a host that declared it');
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
  return ctx.ui.sheetSettings ? ctx.ui.sheetSettings.querySelector('[data-prop="' + name + '"]') : null;
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
var SETTINGS_NOT_IN_BROWSER = ['cpuTempSensor', 'gpuTempSensor', 'touchDiag', 'crabdPort'];

/* crabdPort joins them for a different reason than the other three (review): it
   is not unbacked, it is INERT. baseUrl() reads location.protocol first and
   returns '' on a served origin, so the property cannot move where the panel
   polls — and a control that visibly does nothing is worse than no control. It
   stays declared for the iCUE case, where the panel is loaded from disk and has
   to name crabd outright. */

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

/* A PROPERTY NAMED OFF Object.prototype MUST NOT VANISH (review). `SETTINGS_HIDDEN`
   and `SETTINGS_TIME` are plain objects used as sets, and a bare index into one
   inherits: `SETTINGS_HIDDEN['constructor']` is a function, which is truthy, so
   a property called `constructor` (or `toString`, or `valueOf`) would be skipped
   as hidden with nothing said anywhere. Demonstrated rather than argued — the
   declarations are read from the DOM, so the test adds one and looks. */
(function () {
  var ctx = settingsPanel();
  var groups = ctx.document.getElementById('x-icue-groups');
  var list = JSON.parse(groups.textContent);
  list[0].properties.push('constructor');
  groups.textContent = JSON.stringify(list);
  var head = ctx.document.querySelectorAll('meta[name="x-icue-property"]')[0].parentNode;
  var meta = head.appendChild(ctx.document.createElement('meta'));
  meta.setAttribute('name', 'x-icue-property');
  meta.setAttribute('content', 'constructor');
  meta.setAttribute('data-type', 'switch');
  meta.setAttribute('data-default', 'false');
  meta.setAttribute('data-label', "tr('Inherited Name')");
  ctx.openSettingsSheet();
  ok(!!control(ctx, 'constructor'),
    'a property whose name is on Object.prototype still gets a control');
})();

/* A SLIDER META WITH NO RANGE MUST NOT CLAMP TO ZERO (review). Number(null) is
   0, so a missing data-min would have pinned every value of that control to 0
   and sent it to crabd — the contract-legal-null trap, one layer down. */
(function () {
  var ctx = settingsPanel();
  ctx.openSettingsSheet();
  var meta = { name: 'x', type: 'slider', dflt: 42, min: null, max: null, step: null };
  var el = { value: '900', checked: false, getAttribute: function () { return null; } };
  eq(ctx.readSettingsControl(el, meta), 900, 'an absent min/max leaves the value alone');
  eq(ctx.readSettingsControl({ value: 'nonsense', getAttribute: function () { return null; } }, meta), 42,
    'and an unreadable one falls back to the declared default, not to 0');
  var bounded = { name: 'y', type: 'slider', dflt: 20, min: '5', max: '300', step: '5' };
  eq(ctx.readSettingsControl({ value: '9000', getAttribute: function () { return null; } }, bounded), 300,
    'a declared range still clamps');
  eq(ctx.readSettingsControl({ value: '1', getAttribute: function () { return null; } }, bounded), 5,
    'at both ends');
})();

/* A HALF-TYPED QUIET HOUR IS NOT SILENTLY DROPPED (review). desiredQuietConfig
   returns null for a time normHm rejects, which means "do not send" — correct
   on the wire and invisible on the glass, so the operator watched a value they
   had typed simply never take effect. */
(function () {
  var ctx = settingsPanel();
  ctx.openSettingsSheet();
  var row = control(ctx, 'quietStart').parentNode;
  setControl(ctx, 'quietEnabled', true);
  eq(row.querySelectorAll('.set-invalid.shown').length, 0, 'a valid time says nothing');
  setControl(ctx, 'quietStart', '25:00');
  eq(row.querySelectorAll('.set-invalid.shown').length, 1, 'an impossible hour is marked on the row');
  ok(/HH:MM/.test(row.textContent), 'and the line says what the field wants  (' + row.textContent + ')');
  eq(ctx.desiredQuietConfig(), null, 'nothing is sent while it is wrong');
  setControl(ctx, 'quietStart', '9:05');
  eq(row.querySelectorAll('.set-invalid.shown').length, 0, 'a value normHm can pad clears it again');
  eq(ctx.desiredQuietConfig(), { quietHours: { start: '09:05', end: '07:00' } },
    'and the padded value is what goes on the wire');
})();

/* AND IT HAS TO BE TRUE ON OPEN, not only on change (review). A value crabd
   refuses can already be in the store — typed in an earlier session, or written
   by a hand edit — and a sheet that only marked it when the control moved would
   open showing a perfectly ordinary-looking field whose value has never been
   sent and never will be. */
(function () {
  var st = fakeStorage('ok', { sidecrab: JSON.stringify({ quietEnabled: true, quietStart: 'half past nine' }) });
  var ctx = settingsPanel({ storage: st });
  ctx.openSettingsSheet();
  var row = control(ctx, 'quietStart').parentNode;
  eq(control(ctx, 'quietStart').value, 'half past nine', 'the stored value is shown as it is');
  eq(row.querySelectorAll('.set-invalid.shown').length, 1, 'and its refusal line is up the moment the sheet opens');
  eq(ctx.desiredQuietConfig(), null, 'which is the truth: nothing is being sent');
  /* The valid one beside it says nothing, so the mark is about the value and not
     about the sheet having opened. */
  var ok2 = control(ctx, 'quietEnd').parentNode;
  eq(ok2.querySelectorAll('.set-invalid.shown').length, 0, 'a valid field opens unmarked');
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
  /* ONE COPY PER SURFACE (review). index.html's group info is written for the
     iCUE console and names the PowerShell command; the panel's sheet says the
     shell one, in pairingHelp(), and SKIPS that group's info rather than
     printing an instruction for the other platform beside its own. */
  var helpText = ctx.ui.sheetSettings.querySelector('.set-help').textContent;
  /* BOTH COMMANDS, IN ONE SENTENCE (review). The browser panel is reachable on
     Windows too — crabd serves it there as readily as on a Mac — so a sheet that
     named only the shell script would send half its readers to a file they do
     not have. The iCUE group info still names the PowerShell command alone,
     because the console only ever renders on Windows. */
  ok(/Install-SideCrab\.ps1 -PairingCode/.test(helpText), 'the sheet names the PowerShell command');
  ok(/setup\/install\.sh --pairing-code/.test(helpText), 'and the shell one');
  ok(/on macOS/.test(helpText), 'saying which is which  (' + helpText + ')');
  var infos = ctx.ui.sheetSettings.querySelectorAll('.set-group-info');
  var infoText = [];
  for (var k = 0; k < infos.length; k++) infoText.push(infos[k].textContent);
  ok(!infoText.some(function (t) { return /pairing/i.test(t); }),
    'the Approvals group renders its instruction on the row, not twice');
  ok(/refused until this matches/.test(helpText),
    'and the row carries the prose the group info would have  (' + helpText + ')');
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

/* ------------------------------------------------------- the sensors row (D) */

/* NO BRIDGE, NO TEMPERATURES, AND NO PROMPT ABOUT THEM. window.plugins does not
   exist in a browser and no page can read a die temperature, so the two
   temperature cells and both of the row's iCUE hints — "pick sensors in
   settings" and "same sensor" — are simply not there. What is left is the half
   the companion feeds, and when the companion has nothing to say about this
   machine the row goes off the glass entirely rather than showing zeros or a
   line of em-dashes. */

function hostPanel(host, opts) {
  opts = opts || {};
  var ctx = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '' },
    storage: fakeStorage('ok'),
    navigator: opts.navigator
  });
  ctx.renderHost(host);
  return ctx;
}

(function () {
  var ctx = hostPanel({ cpuPct: 34.2, memPct: 58.4, memUsedGB: 18.7, memTotalGB: 32.0 });
  eq(ctx.sensorsPlugin(), null, 'there is no Sensors bridge in a browser');
  ok(!ctx.ui.sensorCpu.classList.contains('shown') || ctx.ui.hostCpuVal.classList.contains('shown'),
    'the CPU cell is on the row only for the figure the companion fed it');
  eq(ctx.ui.sensorCpuVal.textContent, '', 'no temperature is rendered');
  eq(ctx.ui.sensorGpuVal.textContent, '', 'and none in the GPU cell either');
  eq(ctx.ui.sensorHint.textContent, '', 'the "pick sensors in settings" hint is absent');
  eq(ctx.ui.sensorGpuWarn.textContent, '', 'and so is the "same sensor" warning');
  ok(!ctx.ui.sensorGpu.classList.contains('shown'), 'the GPU cell has nothing to say and is off the row');
  ok(ctx.ui.sensors.classList.contains('shown'), 'the row IS on the glass: the companion fed it');
  eq(ctx.ui.hostCpuVal.textContent, '34%', 'the host CPU figure');
  eq(ctx.ui.hostMemVal.textContent, '58%', 'the host memory figure');
  ok(ctx.document.body.classList.contains('has-sensors'), 'and the Limits zone knows the row is there');
})();

/* The ?mock=hot shape: the block is PRESENT with every member null, which is a
   companion that could not measure this machine. Never 0%. */
(function () {
  var ctx = hostPanel({ cpuPct: null, memPct: null, memUsedGB: null, memTotalGB: null });
  ok(!ctx.ui.sensors.classList.contains('shown'), 'an all-null host block hides the row entirely');
  eq(ctx.ui.hostCpuVal.textContent, '', 'no 0%');
  eq(ctx.ui.hostMemVal.textContent, '', 'in either segment');
  ok(!ctx.document.body.classList.contains('has-sensors'), 'and the Limits zone loses the row with it');
})();

(function () {
  var ctx = hostPanel(null);
  ok(!ctx.ui.sensors.classList.contains('shown'), 'no host block at all hides the row too');
})();

/* The ?mock=dense shape: one member readable beside one that is not. */
(function () {
  var ctx = hostPanel({ cpuPct: null, memPct: 61.0, memUsedGB: null, memTotalGB: null });
  ok(ctx.ui.sensors.classList.contains('shown'), 'a partially readable host block keeps the row');
  eq(ctx.ui.hostCpuVal.textContent, '', 'the unreadable member is absent');
  eq(ctx.ui.hostMemVal.textContent, '61%', 'the readable one is a figure');
})();

/* "This PC" was a Windows noun on a panel that now runs on a Mac. */
(function () {
  var mac = hostPanel({ cpuPct: 10, memPct: 20, memUsedGB: null, memTotalGB: null },
    { navigator: { userAgent: 'node', platform: 'MacIntel' } });
  mac.openHostSheet();
  eq(mac.ui.sheetTitle.textContent, 'This Mac', 'a Mac says so');
  eq(mac.ui.sensors.getAttribute('aria-label'), "Open this Mac's CPU and memory history",
    'and the row that opens it says the same');

  var other = hostPanel({ cpuPct: 10, memPct: 20, memUsedGB: null, memTotalGB: null },
    { navigator: { userAgent: 'node', platform: 'Linux x86_64' } });
  other.openHostSheet();
  eq(other.ui.sheetTitle.textContent, 'This machine', 'anything else is "This machine"');
  eq(other.ui.sensors.getAttribute('aria-label'), "Open this machine's CPU and memory history",
    'and so is the row');

  var js = fs.readFileSync(harness.SRC, 'utf8');
  ok(js.indexOf('This PC') === -1, 'the panel never says "This PC" anywhere');
})();

/* The temperatures block is omitted when no cell has ever rendered one — rather
   than the sheet printing "no hardware sensor reading" about a bridge that does
   not exist on this platform at all. */
(function () {
  var ctx = hostPanel({ cpuPct: 10, memPct: 20, memUsedGB: null, memTotalGB: null });
  ctx.openHostSheet();
  eq(ctx.ui.sheetHost.querySelectorAll('.hs-temps').length, 0,
    'no temperature has ever rendered, so the sheet says nothing about temperatures');
  ok(ctx.ui.sheetHost.querySelectorAll('.hs-chart').length === 2, 'the two host charts are still there');
})();

/* A BOUND BRIDGE THAT HAS SAID NOTHING KEEPS THE LINE (review). The latch alone
   did the opposite of what its comment claimed inside iCUE: a machine whose
   Sensors plugin is present and has never produced a reading is exactly the one
   whose operator needs "no hardware sensor reading" — and the latch never resets,
   so a bridge that went quiet after one good read kept promising temperatures it
   no longer had. The gate is the BRIDGE, not the history. */
(function () {
  var ctx = hostPanel({ cpuPct: 10, memPct: 20, memUsedGB: null, memTotalGB: null });
  /* Stand in for a bound plugin the way the panel's own dev flag does: what
     decides the line is whether a bridge exists to be quiet. */
  ctx.sensorsPlugin = function () { return {}; };
  ctx.openHostSheet();
  eq(ctx.ui.sheetHost.querySelectorAll('.hs-temps').length, 1,
    'a bound bridge that has produced nothing still says so');
  var line = ctx.ui.sheetHost.querySelector('.hs-temps');
  /* Guarded before it is read (review): a regression here would otherwise throw a
     TypeError on the next line and stop the whole run, so the one thing that went
     wrong would be reported as every remaining check never happening. */
  ok(!!line, 'the temperatures line is in the sheet to be read');
  eq(line && line.textContent, 'no hardware sensor reading', 'and says exactly that');
})();

/* Every sensor dev flag stays gated on ?mock=, so nothing on a served origin can
   manufacture a reading. */
(function () {
  var ctx = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/',
      search: '?sensors=95,84&sensornames=A|B&sensorsame=1&sensorstale=1000' },
    storage: fakeStorage('ok')
  });
  eq(ctx.sensorForced, null, '&sensors= is inert without ?mock=');
  eq(ctx.sensorForcedSame, false, '&sensorsame= is inert without ?mock=');
  eq(ctx.SENSOR_STALE_MS, 60000, '&sensorstale= cannot rewrite the tunable without ?mock=');
  eq(ctx.sensorIdFor('cpu'), '', 'and no sensor id is manufactured');
})();

/* ------------------------------------------- the dev flags stay gated (E) */

/* THE ONE SECURITY REGRESSION THE PORT INTRODUCES BY ITSELF, pinned. Every
   screenshot flag was gated on ?mock= on the stated ground that the iCUE origin
   carries no query string. That ground is gone the moment crabd serves the panel
   at an address anyone can type: &ackflash=1 performs a REAL live ack-all POST,
   &sensorstale= rewrites a module tunable, and &quietov= and &budget= rewrite the
   served document. The gate is the same one it always was — the flags live
   inside `if (mockName)` — and this is what says so out loud, from a served
   http origin with every flag in the query string at once. */

var EVERY_DEV_FLAG = '?ackflash=1&refreshflash=1&age=30&filter=waiting&density=compact' +
  '&budget=150&quietov=on&sensorstale=1000&hold=10&approvalsec=45&crab=party&mood=worried' +
  '&uid=devbox&touchdiag=1&action400=1&sheet=first&sheet2=first&pin=first&swipe=first' +
  '&pinflash=first&spark=7d&celebrate=1&blink=2&burn=1&timeline=1&day=2026-08-21&hist=error' +
  '&host=1&approval=1&sensors=95,84&sensornames=A|B&sensorsame=1&sensorfail=1&sensorlog=1';

/* name -> the value it MUST still hold. Read off the declarations in
   sidecrab.js, so a flag that gains a variable and no gate shows up here as an
   undefined rather than passing quietly. */
var DEV_FLAG_RESTING = {
  mockName: null, ackFlashAuto: false, refreshFlashAuto: false, noticeHold: false,
  ageOverrideMin: null, filterForced: null, densityForced: null, budgetPctOverride: null,
  quietForced: null, mockQuietOv: null, SENSOR_STALE_MS: 60000, holdOverrideSec: null,
  approvalForcedSec: null, accForced: null, forcedTrick: null, moodForced: null,
  devUidOverride: null, diagForced: false, actionForce400: false, sheetAutoId: null,
  sheetAutoDetailId: null, pinAuto: null, swipeFreeze: null, pinFlashAuto: null,
  pinFlashHold: false, sparkMode: '24h', celebrateForced: false, burnAuto: false,
  timelineAuto: false, dayAuto: null, histAuto: null, hostAuto: false, approvalAuto: false,
  sensorForced: null, sensorForcedSame: false, sensorForcedFail: false, sensorLogVerbose: false
};

(function () {
  var calls = [];
  var ctx = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: EVERY_DEV_FLAG },
    storage: fakeStorage('ok'),
    fetch: function (url, init) {
      calls.push({ url: url, init: init });
      return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({}); } });
    }
  });
  Object.keys(DEV_FLAG_RESTING).forEach(function (k) {
    eq(ctx[k], DEV_FLAG_RESTING[k], k + ' is untouched by a query string on a served origin');
  });
  eq(ctx.blinkMinMs, ctx.BLINK_MIN_MS, '&blink= cannot pin the idle-blink interval either');
  var posts = calls.filter(function (c) { return c.init && c.init.method === 'POST'; });
  eq(posts.length, 0, 'and NOTHING was POSTed: the ack-all flag did not fire');
  ok(ctx.document.body.className.indexOf('pinflash-frozen') === -1,
    'nor did a flag put a frozen animation class on the body');
})();

/* The same flags WITH ?mock= are the screenshot harness working as designed —
   and even then the ack-all it really performs never leaves the page. */
(function () {
  var calls = [];
  var ctx = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '?mock=normal&ackflash=1' },
    storage: fakeStorage('ok'),
    fetch: function (url, init) {
      calls.push({ url: url, init: init });
      return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve({ schema: 1, generatedAt: new Date().toISOString(), sessions: [] }); } });
    }
  });
  eq(ctx.mockName, 'normal', '?mock= turns the harness on');
  eq(ctx.ackFlashAuto, true, 'and the flag is honoured');
  var posts = calls.filter(function (c) { return c.init && c.init.method === 'POST'; });
  eq(posts.length, 0, 'the mock action stub answers in-page: no POST reaches the wire');
})();

/* --------------------------------------------------- the stale rule (F) */

/* UNCHANGED BY THE PORT, AND THAT IS THE POINT. The contract's staleness limit
   is 30 s on generatedAt, a failed poll is stale on its own, and a document that
   has never arrived is `connecting` and never `live`. Silence rendering as
   all-green is the one failure this panel exists to not have, so the rule is
   pinned against a stubbed clock rather than against a wall clock that would
   make the 29/31 pair a race. */

function clockPanel(nowMs) {
  var ctx = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '' },
    storage: fakeStorage('ok')
  });
  harness.setNow(ctx, nowMs);
  return ctx;
}

function docAt(ms) {
  return { schema: 1, generatedAt: new Date(ms).toISOString(), sessions: [], limits: { available: false } };
}

(function () {
  var NOW = Date.parse('2026-09-04T12:00:00Z');
  var ctx = clockPanel(NOW);
  eq(ctx.computeStatus(), 'connecting', 'a document that never arrived is connecting, never live');
  ok(ctx.document.body.classList.contains('connecting'), 'and the body says so');
  ok(!ctx.document.body.classList.contains('stale'), 'connecting is not stale either');

  ctx.acceptDoc(docAt(NOW - 29000));
  eq(ctx.computeStatus(), 'live', '29 s old is live');
  ok(!ctx.document.body.classList.contains('stale'), 'and the body is not stale');

  ctx.acceptDoc(docAt(NOW - 31000));
  eq(ctx.computeStatus(), 'stale', '31 s old is stale');
  ctx.render();
  ok(ctx.document.body.classList.contains('stale'), 'the body carries .stale');
  ok(/^crabd not responding . data as of \d{1,2}:\d{2}/.test(ctx.ui.bannerText.textContent),
    'the banner says how old the reading is  (' + ctx.ui.bannerText.textContent + ')');
  eq(ctx.ui.crab.getAttribute('data-mood'), 'worried', 'and the crab is worried');
})();

/* A FAILED POLL IS STALE ON ITS OWN, without waiting out the 30 s: the reading
   on the glass is the last one that landed and nothing has confirmed it since. */
(function () {
  var NOW = Date.parse('2026-09-04T12:00:00Z');
  var ctx = clockPanel(NOW);
  ctx.acceptDoc(docAt(NOW - 1000));
  eq(ctx.computeStatus(), 'live', 'a fresh document is live');
  ctx.pollFailed = true;
  eq(ctx.computeStatus(), 'stale', 'one failed poll is stale, however fresh the last document was');
  ctx.render();
  eq(ctx.ui.crab.getAttribute('data-mood'), 'worried', 'the crab is worried on a failed poll too');
})();

/* A document above the ceiling is a real break, not fresh data (?mock=future). */
(function () {
  var NOW = Date.parse('2026-09-04T12:00:00Z');
  var ctx = clockPanel(NOW);
  ctx.acceptDoc({ schema: 6, generatedAt: new Date(NOW).toISOString(), sessions: [] });
  eq(ctx.computeStatus(), 'connecting', 'a schema above the ceiling never becomes live');
  eq(ctx.pollFailed, true, 'it is a dead feed');
})();

/* ------------------------------------------------ every fixture renders (G) */

/* THE SCREENSHOT HARNESS IS THE ONLY THING THAT EVER SAW THESE DOCUMENTS. Each
   fixture exists to hold one shape the render path gets wrong when nobody is
   looking — a null beside a number, a schema above the ceiling, a member absent
   where a sibling has it — and until now the only thing that exercised them was
   a person opening a browser. So every one is driven through the SHIPPING path
   (?mock= sets the harness, acceptDoc rebases and renders) against the shipping
   markup, and each is asserted on the one fact widget/DEV.md's table says it is
   for. Structural, never pixels: the pixels are measured in a real browser and
   recorded in DEV.md. */

function fixturePanel(name) {
  var doc = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'mock', 'mock-state-' + name + '.json'), 'utf8'));
  var ctx = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '?mock=' + name },
    storage: fakeStorage('ok')
  });
  ctx.acceptDoc(doc);
  return ctx;
}

function cardCount(ctx) { return ctx.ui.cards.querySelectorAll('.card').length; }

(function () {
  var ctx = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '' },
    storage: fakeStorage('ok')
  });
  var mocks = ctx.MOCKS;
  eq(mocks.length, 13, 'thirteen fixtures, and every one of them is driven below');
  mocks.forEach(function (name) {
    var threw = null, ctx2 = null;
    try { ctx2 = fixturePanel(name); } catch (e) { threw = e; }
    ok(!threw, '?mock=' + name + ' renders without throwing  (' + (threw && threw.stack ? threw.stack.split('\n')[0] : '') + ')');
    if (!ctx2) return;

    if (name === 'future') {
      /* THE BREAK REGRESSION. Otherwise a perfectly valid document; the only
         thing wrong with it is a schema above the ceiling. */
      eq(ctx2.pollFailed, true, 'future: a dead feed');
      eq(cardCount(ctx2), 0, 'future: no session cards');
      ok(ctx2.computeStatus() !== 'live', 'future: and never live');
      /* MEASURED 2026-09-04, and it corrects the fixture table: on a COLD load
         the crab is `asleep`, not `worried`. everHadData is still false, so the
         status is `connecting` and the mood ladder's first rung takes it — which
         is right (the panel has never seen anything) but is not what DEV.md's
         row said. The worried crab is what a panel that HAD data does when a
         schema-6 document arrives, and that is asserted below. */
      eq(ctx2.ui.crab.getAttribute('data-mood'), 'asleep', 'future: cold, the crab is asleep');
    } else if (name === 'dense') {
      /* THE PRE-0.28.0 crabd FIXTURE: no contextWindowTokens anywhere, so the
         [1m] marker is parsed out of the model id itself. */
      var raw = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'mock', 'mock-state-dense.json'), 'utf8'));
      var marked = raw.sessions.filter(function (s) { return /\[1m\]/.test(String(s.model || '')); });
      ok(marked.length > 0, 'dense: it carries a [1m] row');
      ok(!raw.sessions.some(function (s) {
        return Object.prototype.hasOwnProperty.call(s, 'contextWindowTokens');
      }), 'dense: and deliberately no contextWindowTokens at all');
      eq(ctx2.ctxWindowTokens(marked[0]), 1000000, 'dense: the window falls back to the marker, byte-identically');
    } else if (name === 'hot') {
      /* THE ALL-NULL HOST FIXTURE: a companion that could not measure this
         machine. Both segments absent, never 0%. */
      eq(ctx2.ui.hostCpuVal.textContent, '', 'hot: no host CPU figure');
      eq(ctx2.ui.hostMemVal.textContent, '', 'hot: no host memory figure');
      ok(!ctx2.ui.sensors.classList.contains('shown'), 'hot: and the row is off the glass');
      eq(ctx2.ui.pct5h.textContent, '97%', 'hot: the five-hour window is at the red step');
    } else if (name === 'extras') {
      eq(ctx2.document.body.classList.contains('limits-two-extras'), true,
        'extras: the only fixture that reaches body.limits-two-extras');
    } else if (name === 'rework') {
      /* THE DEPLETION-FORECAST FIXTURE, all three branches in one document:
         fiveHour exhausts before its reset (the line renders), weekly carries a
         null exhaustAt (no line). */
      ok(ctx2.ui.forecast5h.textContent !== '', 'rework: the five-hour forecast line renders');
      eq(ctx2.ui.forecastWk.textContent, '', 'rework: a null exhaustAt renders no line');
      eq(ctx2.ui.sheetWeek !== null, true, 'rework: the week strip region is there to fill');
    } else if (name === 'quiet') {
      eq(ctx2.document.body.classList.contains('quiet'), true, 'quiet: the panel is dim');
      ok(ctx2.ui.quietNote.textContent !== '', 'quiet: and says why');
    } else if (name === 'caveat') {
      /* limits.note non-null WITH available:true — a caveat is not a failure, so
         the gauges stay lit and the note is muted rather than amber. */
      ok(ctx2.ui.limitsNote.classList.contains('shown'), 'caveat: the note renders');
      ok(!ctx2.ui.limitsNote.classList.contains('warn'), 'caveat: muted, not amber');
      eq(ctx2.ui.pct5h.textContent, '61%', 'caveat: and the gauges stay lit');
    } else if (name === 'empty') {
      eq(cardCount(ctx2), 0, 'empty: no cards');
      eq(ctx2.ui.gridEmpty.textContent, 'No active Claude sessions', 'empty: and it says so');
    } else if (name === 'stale') {
      eq(ctx2.computeStatus(), 'stale', 'stale: renders ~3 min old, which is stale');
    } else if (name === 'normal') {
      /* THE NO-burn.daily CASE: the sparkline toggle must be inert and muted. */
      ok(ctx2.ui.sparkMode.classList.contains('disabled'), 'normal: no burn.daily, so the 7d toggle is inert');
      ok(cardCount(ctx2) > 0, 'normal: and the v1 document still renders cards');
    } else if (name === 'attention') {
      var withQ = ctx2.ui.cards.querySelectorAll('.card-question').length;
      ok(withQ >= 1, 'attention: one needs_input card carries a question');
      ok(cardCount(ctx2) > withQ, 'attention: and one does not (the v1 fallback)');
    } else if (name === 'question') {
      ok(ctx2.ui.cards.querySelectorAll('.sub-more').length >= 1,
        'question: a card clamps its subagent rows with a "+N more"');
    } else if (name === 'recap') {
      ok(ctx2.ui.sessionCount.classList.contains('recap'), 'recap: the header carries the day summary');
    }
  });
})();

/* THE OTHER HALF OF THE BREAK REGRESSION: a LIVE panel handed a document above
   the ceiling. This is the case DEV.md's row describes — the cards go, the crab
   worries, and nothing from the last good document is re-served as fresh. */
(function () {
  var NOW = Date.parse('2026-09-04T12:00:00Z');
  var ctx = clockPanel(NOW);
  var good = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'mock', 'mock-state-rework.json'), 'utf8'));
  good.generatedAt = new Date(NOW - 1000).toISOString();
  ctx.acceptDoc(good);
  var had = cardCount(ctx);
  ok(had > 0, 'a live panel has cards');
  var future = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'mock', 'mock-state-future.json'), 'utf8'));
  future.generatedAt = new Date(NOW).toISOString();
  ctx.acceptDoc(future);
  eq(ctx.computeStatus(), 'stale', 'a schema above the ceiling arriving on a live panel is a dead feed');
  eq(ctx.ui.crab.getAttribute('data-mood'), 'worried', 'and THAT is the worried crab the fixture is for');
  eq(cardCount(ctx), had, 'the last good cards stay, dimmed, rather than being re-served as fresh');
})();

/* ------------------------------------- keyboard equivalents for the gestures (I) */

/* WHY THESE EXIST NOW AND NOT AT v0.20.0. The panel shipped on a wall-mounted
   touchscreen with no keyboard attached, and CD-15 recorded a keyboard
   equivalent for each of the four gestures as DELIBERATELY SKIPPED: shipping an
   interaction model that could not be exercised on the surface it was for. A
   panel with an address in a browser is exercised from a keyboard by definition,
   so the four are here, and each one calls THE SAME FUNCTION the gesture calls
   rather than a second copy of it that can drift.

   The two conditions that make a bare letter safe: never while a sheet is open,
   and never while an input has focus. */

function keyPanel(name) {
  var doc = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'mock', 'mock-state-' + (name || 'rework') + '.json'), 'utf8'));
  var ctx = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '?mock=' + (name || 'rework') },
    storage: fakeStorage('ok')
  });
  ctx.acceptDoc(doc);
  return ctx;
}

function cardOfState(ctx, state) {
  var cards = ctx.ui.cards.querySelectorAll('.card');
  for (var i = 0; i < cards.length; i++) if (cards[i].getAttribute('data-state') === state) return cards[i];
  return null;
}

(function () {
  var ctx = keyPanel();
  var calls = [];
  ctx.ackAllWaiting = function () { calls.push('ackAllWaiting'); return 2; };
  ctx.forceRefresh = function () { calls.push('forceRefresh'); };
  ctx.togglePin = function (id) { calls.push('togglePin:' + id); };
  ctx.dismissSwiped = function (card) { calls.push('dismissSwiped:' + card.getAttribute('data-session-id')); };

  ctx.document.dispatch('keydown', { key: 'a' });
  eq(calls, ['ackAllWaiting'], 'a runs the same ack-all the two-finger tap and the crab tap run');

  calls.length = 0;
  ctx.document.dispatch('keydown', { key: 'r' });
  eq(calls, ['forceRefresh'], 'r runs the same refresh the pull-down runs');

  /* p and Delete need a card to act ON, and act on nothing without one. */
  calls.length = 0;
  ctx.document.dispatch('keydown', { key: 'p' });
  ctx.document.dispatch('keydown', { key: 'Delete' });
  eq(calls, [], 'p and Delete do nothing with no card focused');

  var done = cardOfState(ctx, 'done');
  ok(!!done, 'the rework fixture has a dismissable card');
  ctx.document.activeElement = done;
  calls.length = 0;
  ctx.document.dispatch('keydown', { key: 'p' });
  eq(calls, ['togglePin:' + done.getAttribute('data-session-id')],
    'p on a focused card runs the same pin toggle the long press runs');

  calls.length = 0;
  ctx.document.dispatch('keydown', { key: 'Delete' });
  eq(calls, ['dismissSwiped:' + done.getAttribute('data-session-id')],
    'Delete on a focused dismissable card runs the same dismissal the swipe runs');
  /* Focus has already moved to the neighbour — that hand-off is its own test
     below — so put it back to assert Backspace is the same key as Delete. Looked
     up again rather than reusing the reference: the pin above rebuilt the grid,
     so the node this test started with is detached. */
  ctx.document.activeElement = cardOfState(ctx, 'done');
  calls.length = 0;
  ctx.document.dispatch('keydown', { key: 'Backspace' });
  eq(calls, ['dismissSwiped:' + done.getAttribute('data-session-id')], 'and so does Backspace');

  /* A working card has no dismissal for the key to BE, exactly as it has none
     for the swipe: the finger travels and nothing happens. */
  var working = cardOfState(ctx, 'working');
  if (working) {
    ctx.document.activeElement = working;
    calls.length = 0;
    ctx.document.dispatch('keydown', { key: 'Delete' });
    eq(calls, [], 'Delete on a card that cannot be dismissed does nothing');
  }
  ctx.document.activeElement = ctx.document.body;
})();

/* A MODIFIER MEANS THE BROWSER'S SHORTCUT, NOT OURS (review). Cmd-R and Ctrl-R
   are reload, Cmd-P is print, Cmd-A is select-all, Cmd-S is save — every one of
   the four letters this panel claimed is a browser shortcut with a modifier on
   it, and swallowing those on a page an operator lives in is the panel taking
   the browser away from them. */
(function () {
  var ctx = keyPanel();
  var calls = [];
  ctx.ackAllWaiting = function () { calls.push('ack'); return 1; };
  ctx.forceRefresh = function () { calls.push('refresh'); };
  ['metaKey', 'ctrlKey', 'altKey'].forEach(function (mod) {
    ['a', 'r', 's', 'p'].forEach(function (key) {
      var ev = { key: key };
      ev[mod] = true;
      var out = ctx.document.dispatch('keydown', ev);
      ok(!out.defaultPrevented, mod + '+' + key + ' is left to the browser');
    });
  });
  eq(calls, [], 'and nothing on the panel fired');
  eq(ctx.sheetMode, null, 'including the settings sheet');
  /* Without a modifier the same keys are still the panel's. */
  ctx.document.dispatch('keydown', { key: 'r' });
  eq(calls, ['refresh'], 'a bare r is still ours');
})();

/* A HELD KEY IS ONE COMMAND, NOT FORTY (review). Autorepeat fires keydown at the
   OS repeat rate; `a` held down would have made an ack-all POST per repeat, and
   `p` held would have toggled a pin back and forth until the finger lifted. */
(function () {
  var ctx = keyPanel();
  var calls = [];
  ctx.ackAllWaiting = function () { calls.push('ack'); return 1; };
  ctx.document.dispatch('keydown', { key: 'a' });
  ctx.document.dispatch('keydown', { key: 'a', repeat: true });
  ctx.document.dispatch('keydown', { key: 'a', repeat: true });
  eq(calls, ['ack'], 'a held key acts once');
  ctx.document.dispatch('keydown', { key: 'a' });
  eq(calls.length, 2, 'and a fresh press acts again');
})();

/* Inert while a sheet is open: the keys would fire behind it, on a grid the
   operator cannot see. */
(function () {
  var ctx = keyPanel();
  var calls = [];
  ctx.ackAllWaiting = function () { calls.push('ack'); return 1; };
  ctx.forceRefresh = function () { calls.push('refresh'); };
  ctx.openSettingsSheet();
  ['a', 'r', 'p', 'Delete', 'Backspace'].forEach(function (k) { ctx.document.dispatch('keydown', { key: k }); });
  eq(calls, [], 'every gesture key is inert while a sheet is open');
  ctx.closeSheet();
  ctx.document.dispatch('keydown', { key: 'a' });
  eq(calls, ['ack'], 'and live again once it closes');
})();

/* Inert while an input has focus: an "a" typed into the pairing code is a
   character. */
(function () {
  var ctx = keyPanel();
  var calls = [];
  ctx.ackAllWaiting = function () { calls.push('ack'); return 1; };
  ctx.openSettingsSheet();
  var token = control(ctx, 'panelToken');
  ctx.closeSheet();
  ctx.document.activeElement = token;
  ['a', 'r', 's'].forEach(function (k) { ctx.document.dispatch('keydown', { key: k }); });
  eq(calls, [], 'no gesture key fires while an input has focus');
  eq(ctx.sheetMode, null, 'and no sheet opens either');
  ctx.document.activeElement = ctx.document.body;
})();

/* CD-15's own rule, unchanged: a native <button> fires its own click for Enter
   and Space, and synthesising a second one is how a single press denies a
   permission twice. */
(function () {
  var ctx = keyPanel();
  var clicks = 0;
  var btn = ctx.document.getElementById('sheetDeny');
  btn.addEventListener('click', function () { clicks++; });
  ctx.document.activeElement = btn;
  ctx.document.dispatch('keydown', { key: 'Enter' });
  eq(clicks, 0, 'Enter on a native button is not double-activated by the panel');
  ctx.document.activeElement = ctx.document.body;
})();

/* THE HAND-OFF HAS TO SURVIVE THE REBUILD, not just the keystroke. A real
   dismissal removes the row and renderSessions throws every card node away, so
   the node focused at the moment of the key is gone a frame later — which is why
   the queue is answered where the new nodes appear and not only where it is
   set. Driven through the real dismissal with reduced motion on, which is the
   branch that renders immediately instead of on the fly-out timer. */
(function () {
  var ctx = keyPanel();
  ctx.reducedMotion = function () { return true; };
  var target = cardOfState(ctx, 'done');
  var goingId = target.getAttribute('data-session-id');
  var before = cardCount(ctx);
  ctx.document.activeElement = target;
  ctx.document.dispatch('keydown', { key: 'Delete' });
  eq(cardCount(ctx), before - 1, 'the card really left the grid');
  var now = ctx.document.activeElement;
  ok(now && now.getAttribute && now.getAttribute('data-session-id') !== goingId,
    'focus is not on the row that went');
  ok(now === ctx.ui.cards || (now.classList && now.classList.contains('card')),
    'it is on a card the rebuild produced, or on the grid  (' + (now && now.className) + ')');
  ok(ctx.ui.cards.contains(now) || now === ctx.ui.cards, 'and on a node that is actually in the document');
  /* So the next key still has something to act on, which is the whole point. */
  var calls = [];
  ctx.dismissSwiped = function (c) { calls.push(c.getAttribute('data-session-id')); };
  ctx.document.dispatch('keydown', { key: 'Delete' });
  ok(calls.length === 1 || now === ctx.ui.cards, 'and the keyboard path continues from there');
})();

/* THE ANIMATED PATH IS THE ONE THAT ORDERS WRONG. Without reduced motion the
   card flies out and the rebuild is on a timer several hundred ms later, so
   focus has to move TWICE: off the departing card now, and onto its replacement
   once the rebuild has produced one. An earlier version consumed the queue on
   the first move, which left focus on a node the rebuild then threw away — the
   keyboard dying one dismissal in, on the path an operator actually takes. */
(function () {
  var ctx = keyPanel();
  var target = cardOfState(ctx, 'done');
  var goingId = target.getAttribute('data-session-id');
  ctx.document.activeElement = target;
  ctx.timers.length = 0;
  ctx.document.dispatch('keydown', { key: 'Delete' });
  ok(ctx.document.activeElement !== target, 'focus leaves the departing card straight away');

  /* Now let the fly-out timer land, which is what rebuilds the grid. */
  var queued = ctx.timers.slice();
  ctx.timers.length = 0;
  queued.forEach(function (t) { t.fn(); });

  var now = ctx.document.activeElement;
  ok(ctx.ui.cards.contains(now) || now === ctx.ui.cards,
    'and after the rebuild it is on a node still in the document');
  ok(!now.getAttribute || now.getAttribute('data-session-id') !== goingId,
    'never back on the row that went');
  var ids = [];
  var cards = ctx.ui.cards.querySelectorAll('.card');
  for (var i = 0; i < cards.length; i++) ids.push(cards[i].getAttribute('data-session-id'));
  ok(ids.indexOf(goingId) === -1, 'the dismissed row really is off the grid  (' + ids.length + ' left)');
})();

/* The p key needed its own entry point, which moved suppressClick() — so the
   long press's own rule is pinned here. The hold has consumed the interaction
   and the sheet must not also open when the finger lifts; a key has no click
   coming and must swallow nothing, or the operator's next tap goes missing. */
(function () {
  var ctx = keyPanel();
  eq(ctx.suppressClickUntil, 0, 'nothing is suppressed to begin with');
  ctx.firePin(cardOfState(ctx, 'done'));
  ok(ctx.suppressClickUntil > Date.now(), 'a long-press pin swallows the click the finger is about to make');

  var ctx2 = keyPanel();
  ctx2.document.activeElement = cardOfState(ctx2, 'done');
  ctx2.document.dispatch('keydown', { key: 'p' });
  eq(ctx2.suppressClickUntil, 0, 'the p key swallows nothing: there is no click coming');
  ctx2.document.activeElement = ctx2.document.body;
})();

/* A key has no fingertip and no card flying off the glass, so each one narrates
   itself through the same aria-live line the two gestures with no other visible
   result already use. */
(function () {
  var ctx = keyPanel();
  ctx.ackAllWaiting = function () { return 3; };
  ctx.document.dispatch('keydown', { key: 'a' });
  eq(ctx.ui.noticeText.textContent, 'acknowledged 3', 'the ack-all key narrates its count');
  eq(ctx.ui.notice.getAttribute('aria-hidden'), 'false', 'and the line is readable by an accessibility API');
  ctx.document.dispatch('keydown', { key: 'r' });
  eq(ctx.ui.noticeText.textContent, 'refreshing', 'the refresh key narrates too');
  var done = cardOfState(ctx, 'done');
  ctx.document.activeElement = done;
  ctx.document.dispatch('keydown', { key: 'p' });
  eq(ctx.ui.noticeText.textContent, 'pinned', 'the pin key says which way it went');
  ctx.document.dispatch('keydown', { key: 'p' });
  eq(ctx.ui.noticeText.textContent, 'unpinned', 'both ways');

  /* The dismissal narrates too, and it is the one that most needs to: the card
     the operator was reading has left the glass. */
  var target = cardOfState(ctx, 'done');
  ctx.document.activeElement = target;
  ctx.document.dispatch('keydown', { key: 'Delete' });
  eq(ctx.ui.noticeText.textContent, 'dismissed', 'the dismiss key says what happened');

  /* AND FOCUS DOES NOT DIE WITH THE CARD (review). The node under focus has just
     been thrown away, so without a hand-off the keyboard path ends on the
     document and the next key acts on nothing. */
  var still = ctx.document.activeElement;
  ok(still !== target, 'focus has left the card that went');
  ok(still === ctx.ui.cards || (still.closest && still.closest('.card')),
    'and landed on another card, or on the grid itself');
  ctx.document.activeElement = ctx.document.body;
})();

/* ------------------------------------------------------- the layout unit (H) */

/* ONE BASELINE, and now a BOUNDED one. `1vmin` was calibrated for a glass panel
   that is always 2560x720, where vmin is 7.2 px; a browser window is any size at
   all, so the same declaration made every token in the file swing with the
   window — 9.0 px at 1440x900 (a 25% inflation of the whole panel) and 3.89 px at
   390x844. The clamp holds the Edge slot EXACTLY as it was (7.1875 measured at
   2560x720, inside the ceiling, so the middle term still wins) and stops both
   ends running away.
   The numbers are asserted here because the alternative is a stylesheet where
   the reference size is a comment. */
(function () {
  var css = fs.readFileSync(path.join(__dirname, '..', 'styles', 'sidecrab.css'), 'utf8');
  var m = /--layout-unit:\s*clamp\(\s*([\d.]+)px\s*,\s*1vmin\s*,\s*([\d.]+)px\s*\)/.exec(css);
  ok(!!m, 'the layout baseline is a clamped 1vmin');
  eq(m && m[2], '7.2', 'the ceiling is the 2560x720 reference, so that slot is unchanged');
  eq(m && m[1], '4.5', 'and the floor is the measured phone value');

  /* RULE 1/2, and the reason the clamp is a one-line change rather than a sweep:
     no component selector may use a raw viewport unit, so every size in the file
     derives from this one declaration. */
  /* Comments are stripped whole and replaced by their own newlines, so the line
     numbers in a failure still point at the offending declaration — half this
     file is prose about measurements, and most of that prose names a vmin. */
  var code = css.replace(/\/\*[\s\S]*?\*\//g, function (c) { return c.replace(/[^\n]/g, ' '); });
  var lines = code.split('\n');
  var offenders = [];
  for (var i = 0; i < lines.length; i++) {
    if (/[\d.]+(vmin|vmax|vw|vh)\b/.test(lines[i]) && lines[i].indexOf('--layout-unit') === -1) {
      offenders.push((i + 1) + ': ' + lines[i].trim());
    }
  }
  eq(offenders, [], 'no component selector uses a raw viewport unit');
})();

/* THE FONT STACK LEADS WITH THE SYSTEM FACE. Segoe stays, second, because the
   iCUE build is still packaged from this tree — but a stack that names a Windows
   face first is a stack that describes the wrong platform, and on macOS it
   resolved to system-ui by falling through two absent families anyway. */
(function () {
  var css = fs.readFileSync(path.join(__dirname, '..', 'styles', 'sidecrab.css'), 'utf8');
  var ui = /--font-ui:\s*([^;]+);/.exec(css);
  ok(!!ui, 'the UI stack is declared once');
  var stack = ui ? ui[1].split(',').map(function (s) { return s.trim().replace(/^"|"$/g, ''); }) : [];
  /* THE MEASURED FACE LEADS (review). Every px width comment in this stylesheet
     was measured in Segoe on the Edge, and a stack that resolves to a different
     face there would silently invalidate all of them. Both Segoe entries are
     absent on macOS, so they cost nothing and change nothing here — which is why
     the macOS measurement table does not move either way. */
  eq(stack[0], 'Segoe UI Variable Text', 'the face the widths were measured in leads');
  eq(stack[1], 'Segoe UI', 'with its non-variable fallback behind it');
  var sys = stack.indexOf('system-ui');
  ok(sys > 1, 'the system face is next, which is what macOS resolves to  (' + stack.join(' | ') + ')');
  ok(stack.indexOf('-apple-system') === sys + 1, '-apple-system beside it');
  eq(stack[stack.length - 1], 'sans-serif', 'and a generic last');
  var mono = /--font-mono:\s*([^;]+);/.exec(css);
  ok(!!mono && /ui-monospace/.test(mono[1]) && /Cascadia Mono/.test(mono[1]),
    'the mono stack keeps both platforms too');
})();

/* THE RESIZE FEEDBACK LOOP — a defect the port introduces by itself, because an
   iCUE slot never resizes and a browser window is dragged.

   gridCapacity() reads the track counts off the computed style so that JS never
   learns the slot. But a real engine reports the IMPLICIT tracks too, and a grid
   holding more cards than the new slot has cells has grown implicit rows to put
   them in — so the capacity read back is the overflow's own count, and no later
   resize can escape it. MEASURED in Chromium, ?mock=dense, 2560x720 then dragged
   to 390x844: eight cards in a one-column grid whose first three rows were
   7.69 px tall. Three unreadable slivers and five auto rows.

   The fix is to measure an EMPTY grid: the stylesheet's own answer is the only
   one that is about the slot rather than about what is already in it. */
(function () {
  var holder = {};
  var wide = true;
  var ctx = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '?mock=dense' },
    storage: fakeStorage('ok'),
    getComputedStyle: function (el) {
      return {
        display: 'block', visibility: 'visible', opacity: '1',
        getPropertyValue: function (prop) {
          if (!holder.ctx || el !== holder.ctx.ui.cards) return '';
          var n = holder.ctx.ui.cards.querySelectorAll('.card').length;
          if (prop === 'grid-template-columns') return wide ? '1fr 1fr 1fr 1fr' : '1fr';
          if (prop === 'grid-template-rows') {
            var explicit = wide ? 2 : 3;
            /* An engine reports explicit tracks PLUS one implicit track for every
               item that did not fit — which is exactly the trap. */
            var cells = explicit * (wide ? 4 : 1);
            var rows = explicit + Math.max(0, n - cells);
            return new Array(rows).join('100px ') + '100px';
          }
          return '';
        }
      };
    }
  });
  holder.ctx = ctx;
  var dense = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'mock', 'mock-state-dense.json'), 'utf8'));
  ctx.acceptDoc(dense);
  eq(cardCount(ctx), 8, 'the wide slot fills its eight cells (seven cards and the overflow tile)');

  wide = false;
  ctx.timers.length = 0;
  ctx.listeners.resize[0]();
  var queued = ctx.timers.slice();
  ctx.timers.length = 0;
  queued.forEach(function (t) { t.fn(); });

  eq(cardCount(ctx), 3, 'the narrow slot re-measures to THREE, not to the eight it was holding');
  ok(ctx.gridCapacity() <= 3, 'and the capacity read back is the stylesheet\'s, not the overflow\'s');
})();

/* ...BUT NOT WITH A FINGER ON A CARD (review). renderSessions already refuses to
   rebuild mid-swipe — `visible` and the surviving DOM can disagree about which
   row is at which index — and emptying the grid before it is asked is a stronger
   version of the same rebuild, so it has to keep the same rule. A window
   resized while a card is held would otherwise blank the grid under the finger
   and leave it blank until the finger lifted. */
(function () {
  var ctx = keyPanel();
  var before = cardCount(ctx);
  ok(before > 0, 'the grid has cards to hold');
  /* The state a swipe in progress leaves behind, set the way startSwipe sets it. */
  ctx.swipe = { id: 'x', card: cardOfState(ctx, 'done'), dx: 20, base: 1 };
  ok(ctx.gestureHoldsCards(), 'a swipe in progress holds the cards');

  ctx.timers.length = 0;
  ctx.listeners.resize[0]();
  var queued = ctx.timers.slice();
  ctx.timers.length = 0;
  queued.forEach(function (t) { t.fn(); });
  eq(cardCount(ctx), before, 'a resize mid-drag leaves the grid alone');

  /* And once the finger is up, the next resize measures as it should. */
  ctx.swipe = null;
  ctx.timers.length = 0;
  ctx.listeners.resize[0]();
  queued = ctx.timers.slice();
  ctx.timers.length = 0;
  queued.forEach(function (t) { t.fn(); });
  ok(cardCount(ctx) > 0, 'and the grid is rebuilt after it lifts');
})();

/* ----------------------------------------------- the strings, and packaging (J, K) */

/* tr() IS SUBSTITUTED BY THE iCUE IMPORTER AND BY NOTHING ELSE. A browser tab
   showed the macro text of the title verbatim, so the title is a literal. The
   wrappers stay on the property labels, which is what translation.json is a
   catalogue OF — and the manifest and the strict-XML check stay with them,
   because the iCUE build is still packaged from this tree. */
(function () {
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var title = /<title>([^<]*)<\/title>/.exec(html);
  eq(title && title[1], 'SideCrab', 'the tab title is a literal, not a tr() macro');
  ok(/name="x-icue-property"[^>]*data-label="tr\('/.test(html), 'tr() stays on the property labels');
  /* INVERTED (review): index.html is iCUE's surface. The console renders the
     group `info` and nothing else of ours, and the machine reading it is the
     Windows one — so the PowerShell command belongs there and the panel's own
     sheet, which iCUE never opens, carries the shell one. */
  ok(html.indexOf('Install-SideCrab.ps1 -PairingCode') !== -1,
    'the group info names the command the host that renders it actually has');
  ok(html.indexOf('setup/install.sh --pairing-code') === -1,
    'and index.html does not also carry the other platform\'s command');

  var manifest = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'manifest.json'), 'utf8'));
  /* Split (review): a mojibake byte and a missing real em-dash are two different
     defects, and an && reports one message for either. */
  ok(manifest.description.indexOf('\u00e2') === -1, 'the manifest description carries no mojibake byte');
  ok(manifest.description.indexOf('\u2014') !== -1, 'it carries a real em-dash');

  /* THE PROVENANCE SWEEP, AS A TEST (review) rather than an exact-version pin a
     release has to hand-edit. manifest.json is the only machine-read version;
     the tags in the JS, CSS and HTML are comments saying which release the file
     in front of you belongs to, and their whole value is being swept together.
     A pin on the literal caught nothing except itself. */
  ok(/^\d+\.\d+\.\d+$/.test(manifest.version), 'the manifest version is a version  (' + manifest.version + ')');
  var tagged = {
    'scripts/sidecrab.js': fs.readFileSync(harness.SRC, 'utf8'),
    'styles/sidecrab.css': fs.readFileSync(path.join(__dirname, '..', 'styles', 'sidecrab.css'), 'utf8'),
    'index.html': html
  };
  Object.keys(tagged).forEach(function (file) {
    var tag = /SideCrab widget v(\d+\.\d+\.\d+)\b/.exec(tagged[file]);
    ok(!!tag, file + ' carries a provenance tag');
    eq(tag && tag[1], manifest.version, file + ' is tagged with the manifest version');
  });
})();

/* The standalone line, reworded. It has never carried a URL and still must not:
   the served page is already AT the address, and the iCUE build reaches a
   companion the store listing describes. What it has to name is the companion. */
(function () {
  var ctx = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '' },
    storage: fakeStorage('ok')
  });
  var line = ctx.ui.gridEmpty.textContent;
  ok(/SideCrab companion/.test(line), 'the standalone line names the companion  (' + line + ')');
  ok(!/localhost|127\.0\.0\.1|http/.test(line), 'and hard-codes no address');
  ok(!/widget's description/.test(line), 'and no longer sends a browser reader to a store listing');
})();

/* "widget settings" was iCUE's noun for a surface this panel now owns. The panel
   says where the control actually IS, which is not the same place in both hosts
   — so it is read from the host rather than guessed. */
(function () {
  var served = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '' },
    storage: fakeStorage('ok')
  });
  eq(served.settingsPlace(), 'the panel settings (gear)', 'served in a browser, the gear is where it is');
  var icue = loadWidget({ dom: true, props: { uniqueId: 'abc' }, storage: fakeStorage('ok') });
  eq(icue.settingsPlace(), 'the widget settings in iCUE', 'inside iCUE it is still the console');

  var js = fs.readFileSync(harness.SRC, 'utf8');
  var hits = js.split('\n').filter(function (l) {
    return /['"][^'"]*\bwidget settings\b/.test(l) && l.indexOf('function settingsPlace') === -1 &&
      l.indexOf('insideIcue() ?') === -1;
  });
  eq(hits.length, 1, 'the only literal "widget settings" left is the sensors row, which no browser reaches');
  ok(/pick a different GPU sensor/.test(hits[0] || ''), 'and it is that one  (' + (hits[0] || '').trim() + ')');
})();

/* The two pairing refusals are the strings an operator reads at the moment a tap
   was refused, so they have to say where to go. */
(function () {
  var ctx = loadWidget({
    dom: true,
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '?mock=rework' },
    storage: fakeStorage('ok')
  });
  var doc = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'mock', 'mock-state-rework.json'), 'utf8'));
  doc.approvals = { tokenRequired: true };
  ctx.acceptDoc(doc);
  var pending = doc.sessions.filter(function (s) { return s.pendingPermission; })[0];
  ok(!!pending, 'the rework fixture carries a pending permission');
  ctx.openSheet(pending.id);
  ctx.onSheetDecide('allow');
  ok(/the panel settings \(gear\)/.test(ctx.ui.noticeText.textContent),
    'an unpaired decide says where the code goes  (' + ctx.ui.noticeText.textContent + ')');
  eq(ctx.sheetMode !== null, true, 'and the sheet stays open so the reason can be read');
})();

/* ---------------------------------------------- the shim fails loudly (review) */

/* A TEST THAT SILENTLY FINDS NOTHING REPORTS SUCCESS FOREVER — the same reason
   test_ordering.js keeps a deliberate mutant clamp. The shim supports one
   compound simple selector; anything else (a descendant combinator, a
   pseudo-class) now throws with the selector in the message rather than quietly
   matching zero elements and letting an assertion pass on an empty list. */
(function () {
  var ctx = settingsPanel();
  var threw = null;
  try { ctx.document.querySelectorAll('#cards .card'); } catch (e) { threw = e; }
  ok(!!threw, 'a descendant combinator throws rather than matching nothing');
  ok(threw && /unsupported selector/.test(threw.message) && /#cards \.card/.test(threw.message),
    'and the message names it  (' + (threw && threw.message) + ')');

  threw = null;
  try { ctx.document.querySelectorAll('.card:first-child'); } catch (e) { threw = e; }
  ok(!!threw, 'so does a pseudo-class');

  /* The supported shapes still work, including the comma list focusablesIn uses. */
  ok(ctx.ui.cards.querySelectorAll('.card').length >= 0, 'a class selector is fine');
  ok(ctx.document.querySelectorAll('a[href], button, input, select, textarea, [tabindex]').length > 0,
    'and a comma-separated list of them is what the sheet trap needs');
  ok(ctx.document.querySelectorAll('meta[name="x-icue-property"]').length > 0,
    'as is a quoted attribute value');
})();

/* --------------------------------- the v0.27.0 blank-panel trap, as a test */

/* WIDGET 0.27.0 SHIPPED BLANK ON THE EDGE. iCUE injects every widget property as
   a same-named `let` global, so a top-level declaration with a property's name is
   a redeclaration — a whole-script SyntaxError, no panel at all, and nothing on
   the glass to say why. It was found by a person standing at the display.
   The reader was renamed to pairingCode() and the rule written into three
   comments; this makes it a test, because the settings sheet has just added a
   dozen new functions to a file where that mistake costs a release. The property
   list is read from index.html, so a property added tomorrow is covered by it. */
(function () {
  var html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  var js = fs.readFileSync(harness.SRC, 'utf8');
  var props = {};
  var re = /name="x-icue-property"\s+content="(\w+)"/g, m;
  while ((m = re.exec(html))) props[m[1]] = true;
  /* The three the bridge injects or looks for that are not declared as
     properties: uniqueId is the host's own, and the two event objects must stay
     BARE assignments or the bridge cannot find them. */
  props.uniqueId = props.icueEvents = props.pluginSensorsdataproviderEvents = true;
  eq(Object.keys(props).length, 23, 'twenty properties plus the three the bridge owns');

  var declared = {};
  js.split('\n').forEach(function (line) {
    var d = /^(?:function\s+(\w+)|var\s+(\w+)|let\s+(\w+)|const\s+(\w+))/.exec(line);
    if (d) declared[d[1] || d[2] || d[3] || d[4]] = true;
  });
  var collisions = Object.keys(props).filter(function (p) { return declared[p]; });
  eq(collisions, [], 'no top-level declaration shares a name with an injected global');

  ok(/^icueEvents = /m.test(js), 'icueEvents is a bare assignment, so the bridge finds it');
  ok(/^pluginSensorsdataproviderEvents = /m.test(js), 'and so is the sensors one');
  /* Not a module and not strict: both bare assignments become a ReferenceError
     under either. */
  ok(!/^\s*['"]use strict['"]/m.test(js), 'the file is not in strict mode, which those two require');
})();

/* ---------------------------------------------------------------------- done */

Promise.all(pending).then(function () {
  console.log((failures ? 'FAILED' : 'ok') + '  ' + (checks - failures) + '/' + checks + ' checks');
  process.exit(failures ? 1 : 0);
}, function (err) {
  console.log('FAILED  an async check threw: ' + (err && err.stack || err));
  process.exit(1);
});
