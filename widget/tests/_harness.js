/* SideCrab widget — the shared test harness.
 *
 * Indent is 2 spaces here and in test_panel.js, per .editorconfig; test_ordering.js
 * predates that and keeps its tabs, so it requires this file rather than being
 * reindented in the same commit as a behaviour change.
 *
 * WHY A VM AND NOT A MODULE. scripts/sidecrab.js is a flat browser script that ends
 * by calling init(); it has no exports and this repo ships no bundler, and a second
 * copy of any of its rules in a test file would be a copy that can disagree with the
 * panel. So the SHIPPING file is loaded whole into a vm context with a document stub
 * whose readyState is 'loading' — the same branch a real browser takes before
 * DOMContentLoaded, which parks init() on a listener nobody fires. Nothing renders,
 * and the functions under test are the ones on the glass.
 *
 * If a suite ever stops loading, the cause is new TOP-LEVEL work in sidecrab.js
 * (everything else lives inside a function): add the stub it needs HERE, so both
 * suites get it, and do not fork the logic.
 */
'use strict';

var fs = require('fs');
var path = require('path');
var vm = require('vm');

var dom = require('./_dom.js');

var SRC = path.join(__dirname, '..', 'scripts', 'sidecrab.js');
var SOURCE = fs.readFileSync(SRC, 'utf8');
var HTML = path.join(__dirname, '..', 'index.html');

/* No location supplied is the iCUE case: a page loaded off the filesystem. */
var DEFAULT_LOCATION = { protocol: 'file:', search: '', href: 'file:///C:/widget/index.html' };

/* opts.location   the page's location. ABSENT means DEFAULT_LOCATION; an explicit
                   null or undefined is passed through, because a webview that
                   exposes no location at all is a real case the panel must survive
                   — hasOwnProperty, not `||`, is what keeps those distinguishable.
   opts.fetch      the fetch stub every request lands in
   opts.props      iCUE widget properties, injected as globals the way iCUE does
   opts.storage    window.localStorage. ABSENT means none at all, which is the
                   locked-down profile the panel has always had to survive; a
                   stub that THROWS is the other half of that case
   opts.console    where logLine lands, for a suite that asserts on it
   opts.dom        build the shipping index.html into the light DOM shim
                   (_dom.js) instead of the no-DOM stub, and run init() — the
                   only way to drive the sheets, the card grid or the keyboard
   opts.domReason  what createElement throws, naming the suite that called it */
/* setVar() reads a custom property back before writing it, so the stub has to
   answer both halves or every colour write throws. */
function cssStyleStub() {
  var vars = {};
  return {
    getPropertyValue: function (n) { return Object.prototype.hasOwnProperty.call(vars, n) ? vars[n] : ''; },
    setProperty: function (n, v) { vars[n] = String(v); },
    removeProperty: function (n) { delete vars[n]; }
  };
}

function loadWidget(opts) {
  opts = opts || {};
  var listeners = 0;
  var reason = opts.domReason || 'this suite builds no DOM';
  var doc = {
    readyState: 'loading',
    addEventListener: function () { listeners++; },
    documentElement: { style: cssStyleStub() },
    body: { classList: { toggle: function () {}, add: function () {}, remove: function () {}, contains: function () { return false; } } },
    getElementById: function () { return null; },
    querySelector: function () { return null; },
    createElement: function () { throw new Error(reason); }
  };
  /* opts.dom swaps the whole stub for the shipping markup, parsed. readyState is
     'complete', so the file's own bootstrap calls init() — the panel wires itself
     up exactly as it does in a browser and the test drives the real thing. */
  if (opts.dom) {
    doc = dom.makeDocument(fs.readFileSync(HTML, 'utf8'));
    doc.readyState = 'complete';
  }
  var sandbox = { document: doc, console: opts.console || console };
  if (Object.prototype.hasOwnProperty.call(opts, 'storage')) sandbox.localStorage = opts.storage;
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.location = Object.prototype.hasOwnProperty.call(opts, 'location') ? opts.location : DEFAULT_LOCATION;
  if (sandbox.location && sandbox.location.search === undefined) sandbox.location.search = '';
  sandbox.navigator = opts.navigator || { userAgent: 'node', platform: 'MacIntel' };
  /* Timers are inert by default: this panel runs two of them for the life of the
     page and a suite that let them fire would be racing itself. A suite that
     needs one calls the function directly (tick(), poll()). */
  sandbox.timers = [];
  sandbox.setTimeout = function (fn, ms) { sandbox.timers.push({ fn: fn, ms: ms }); return sandbox.timers.length; };
  sandbox.clearTimeout = function () {};
  sandbox.setInterval = function () { return 0; };
  sandbox.clearInterval = function () {};
  sandbox.listeners = {};
  sandbox.addEventListener = function (t, fn) { (sandbox.listeners[t] = sandbox.listeners[t] || []).push(fn); };
  sandbox.removeEventListener = function () {};
  sandbox.getComputedStyle = opts.getComputedStyle || function () {
    return {
      display: 'block', visibility: 'visible', opacity: '1',
      getPropertyValue: function () { return ''; }
    };
  };
  sandbox.fetch = opts.fetch || function () {
    return Promise.resolve({ ok: true, status: 204, json: function () { return Promise.resolve({}); } });
  };
  /* iCUE injects each widget property as a same-named global; the widget reads them
     back through getIcueProperty, which probes window first. */
  var props = opts.props || {};
  Object.keys(props).forEach(function (k) { sandbox[k] = props[k]; });
  var ctx = vm.createContext(sandbox);
  vm.runInContext(SOURCE, ctx, { filename: 'sidecrab.js' });
  if (!opts.dom && !listeners) throw new Error('init() ran: the document stub was not in the loading state');
  if (opts.dom && !ctx.ui.ready) throw new Error('init() did not run: the DOM document was not complete');
  return ctx;
}

/* PIN THE CLOCK. The panel calls Date.now() directly, and the vm's Date lives on
   that context's own global — a host-side `ctx.Date` reads the sandbox OBJECT,
   which never had one — so the override has to be installed from inside. After
   this, moving the clock is `ctx.__now = <ms>`; setting it back to null hands the
   real clock back.
   Explicit clocks, never a patched global that outlives the test: the same rule
   the companion suites keep with their `now=` arguments. */
function setNow(ctx, ms) {
  ctx.__now = ms;
  vm.runInContext(
    'if (!Date.__realNow) Date.__realNow = Date.now;' +
    'Date.now = function () { return typeof __now === "number" ? __now : Date.__realNow(); };',
    ctx);
}

module.exports = { loadWidget: loadWidget, setNow: setNow, DEFAULT_LOCATION: DEFAULT_LOCATION, SRC: SRC };
