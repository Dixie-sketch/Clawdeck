/* SideCrab widget — the transport tests.
 *
 *   node widget/tests/test_panel.js
 *
 * WHY A VM AND NOT A MODULE — the same reason test_ordering.js gives: scripts/
 * sidecrab.js is a flat browser script with no exports, and a second copy of the
 * URL rules in a test file would be a copy that can disagree with the panel. The
 * SHIPPING file is loaded whole into a vm context whose document stub reports
 * readyState 'loading', so init() parks on a listener nobody fires.
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
var vm = require('vm');

var SRC = path.join(__dirname, '..', 'scripts', 'sidecrab.js');
var SOURCE = fs.readFileSync(SRC, 'utf8');

/* opts.location  the page's location (protocol/href/search)
   opts.fetch     the fetch stub every request lands in
   opts.props     iCUE widget properties, injected as globals the way iCUE does */
function loadWidget(opts) {
  opts = opts || {};
  var listeners = 0;
  var doc = {
    readyState: 'loading',
    addEventListener: function () { listeners++; },
    documentElement: { style: { setProperty: function () {} } },
    body: { classList: { toggle: function () {}, add: function () {}, remove: function () {}, contains: function () { return false; } } },
    getElementById: function () { return null; },
    querySelector: function () { return null; },
    createElement: function () { throw new Error('the transport tests build no DOM'); }
  };
  var sandbox = { document: doc, console: console };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.location = opts.location || { protocol: 'file:', search: '', href: 'file:///C:/widget/index.html' };
  if (sandbox.location.search === undefined) sandbox.location.search = '';
  sandbox.navigator = { userAgent: 'node' };
  sandbox.setTimeout = function () { return 0; };
  sandbox.clearTimeout = function () {};
  sandbox.setInterval = function () { return 0; };
  sandbox.clearInterval = function () {};
  sandbox.fetch = opts.fetch || function () { return Promise.resolve({ ok: true, status: 204, json: function () { return Promise.resolve({}); } }); };
  /* iCUE injects each widget property as a same-named global; the widget reads
     them back through getIcueProperty, which probes window first. */
  var props = opts.props || {};
  Object.keys(props).forEach(function (k) { sandbox[k] = props[k]; });
  var ctx = vm.createContext(sandbox);
  vm.runInContext(SOURCE, ctx, { filename: 'sidecrab.js' });
  if (!listeners) throw new Error('init() ran: the document stub was not in the loading state');
  return ctx;
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

/* Header lookup is case-insensitive: a plain object is what fetch is handed here,
   but the wire name is, and a test that only found the exact spelling would pass
   a widget that sent something no server matches. */
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

/* ---------------------------------------------------------------------- done */

Promise.all(pending).then(function () {
  console.log((failures ? 'FAILED' : 'ok') + '  ' + (checks - failures) + '/' + checks + ' checks');
  process.exit(failures ? 1 : 0);
}, function (err) {
  console.log('FAILED  an async check threw: ' + (err && err.stack || err));
  process.exit(1);
});
