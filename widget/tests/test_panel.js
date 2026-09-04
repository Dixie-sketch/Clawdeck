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

function served(props) {
  return loadWidget({
    location: { protocol: 'http:', host: 'localhost:9999', href: 'http://localhost:9999/', search: '' },
    props: props
  });
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

/* ---------------------------------------------------------------------- done */

console.log((failures ? 'FAILED' : 'ok') + '  ' + (checks - failures) + '/' + checks + ' checks');
process.exit(failures ? 1 : 0);
