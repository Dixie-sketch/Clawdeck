/* SideCrab widget — the standalone test fixtures (CLEAN-09, v0.32.0).
 *
 * WHAT THIS REPLACES. Both older suites built one sandbox with `location.search`
 * and `href = '/index.html'` and nothing else: no protocol, no pathname, no host
 * object. The shipping code read that as "not the standalone host", so every
 * default fixture exercised the RETIRED legacy branch and none of them ever booted
 * the app that actually ships. The pure ordering and chime checks were still worth
 * having; the fixture under them was not.
 *
 * THERE ARE EXACTLY TWO SURFACES and this file constructs both explicitly:
 *
 *   nativePage()   the panel host. The page is served at the companion's own
 *                  origin, window.__sidecrabHost carries the boot settings, and
 *                  chrome.webview is a bridge stub that ANSWERS - including the
 *                  host-info handshake, which is the only thing that grants a
 *                  capability.
 *   previewPage()  the same page in a plain browser at the same URL, with no
 *                  bridge at all. Native save and focus must be absent and must be
 *                  SAID to be absent.
 *
 * Neither is a mock of the app. The shipping file is loaded whole into a vm
 * context with a document stub whose readyState is 'loading' - the branch a real
 * browser takes before DOMContentLoaded - so init() parks on a listener nobody
 * fires, and the functions under test are the ones on the glass.
 *
 * `ui` is a Proxy that mints an element the first time a test touches one. That is
 * deliberate: init() resolves about a hundred ids from a document this file does
 * not build, and a fixture that had to list them would go stale the first time the
 * markup moved. The elements are real enough to assert on - classes, attributes,
 * children and text all behave.
 *
 * THE CLOCK IS OURS. Date.now, setTimeout and setInterval are driven by advance(),
 * so a liveness deadline or a bridge timeout is reached in a test in microseconds
 * and never by waiting.
 */
'use strict';

var fs = require('fs');
var path = require('path');
var vm = require('vm');

var SRC = path.join(__dirname, '..', 'scripts', 'sidecrab.js');
var ORIGIN = 'http://127.0.0.1:2722';
var PANEL_URL = ORIGIN + '/panel/';

/* ------------------------------------------------------------------ the DOM */

function Element(tag) {
	this.tagName = String(tag || 'div').toUpperCase();
	this.attrs = {};
	this.children = [];
	this.parentNode = null;
	this.handlers = {};
	this.classes = {};
	this.className = '';
	this.value = '';
	this.type = '';
	this.id = '';
	this.text = '';
	this.style = { setProperty: function () {}, getPropertyValue: function () { return ''; } };
	var self = this;
	this.classList = {
		add: function () { for (var i = 0; i < arguments.length; i++) self.classes[arguments[i]] = 1; self.sync(); },
		remove: function () { for (var i = 0; i < arguments.length; i++) delete self.classes[arguments[i]]; self.sync(); },
		contains: function (k) { return !!self.classes[k]; },
		toggle: function (k, on) {
			var want = on === undefined ? !self.classes[k] : !!on;
			if (want) self.classes[k] = 1; else delete self.classes[k];
			self.sync();
			return want;
		}
	};
}

Element.prototype.sync = function () {
	var out = [];
	for (var k in this.classes) { if (Object.prototype.hasOwnProperty.call(this.classes, k)) out.push(k); }
	this.className = out.join(' ');
};
Element.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); };
Element.prototype.getAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null; };
Element.prototype.hasAttribute = function (k) { return Object.prototype.hasOwnProperty.call(this.attrs, k); };
Element.prototype.removeAttribute = function (k) { delete this.attrs[k]; };
Element.prototype.appendChild = function (el) { el.parentNode = this; this.children.push(el); return el; };
Element.prototype.insertBefore = function (el, ref) {
	el.parentNode = this;
	var at = ref ? this.children.indexOf(ref) : -1;
	if (at < 0) this.children.push(el); else this.children.splice(at, 0, el);
	return el;
};
Element.prototype.removeChild = function (el) {
	var at = this.children.indexOf(el);
	if (at >= 0) this.children.splice(at, 1);
	el.parentNode = null;
	return el;
};
Element.prototype.addEventListener = function (type, fn) { (this.handlers[type] = this.handlers[type] || []).push(fn); };
Element.prototype.fire = function (type, ev) {
	var list = this.handlers[type] || [];
	for (var i = 0; i < list.length; i++) list[i](ev || { target: this });
};
Element.prototype.contains = function (el) {
	if (el === this) return true;
	for (var i = 0; i < this.children.length; i++) { if (this.children[i].contains(el)) return true; }
	return false;
};
Element.prototype.closest = function (sel) {
	var node = this;
	while (node) { if (matches(node, sel)) return node; node = node.parentNode; }
	return null;
};
Element.prototype.querySelectorAll = function (sel) {
	var out = [];
	for (var i = 0; i < this.children.length; i++) {
		if (matches(this.children[i], sel)) out.push(this.children[i]);
		out = out.concat(this.children[i].querySelectorAll(sel));
	}
	return out;
};
Element.prototype.querySelector = function (sel) { return this.querySelectorAll(sel)[0] || null; };

Object.defineProperty(Element.prototype, 'textContent', {
	get: function () {
		var out = this.text;
		for (var i = 0; i < this.children.length; i++) out += this.children[i].textContent;
		return out;
	},
	set: function (v) { this.text = v === null || v === undefined ? '' : String(v); this.children = []; }
});

/* The selector subset this fixture answers: a class, an id, a bare attribute and a
   tag. Everything the shipping file asks for is one of those. */
function matches(el, sel) {
	if (!sel) return false;
	if (sel.charAt(0) === '.') return !!el.classes[sel.slice(1)];
	if (sel.charAt(0) === '#') return el.id === sel.slice(1);
	if (sel.charAt(0) === '[') return el.hasAttribute(sel.slice(1, sel.indexOf(']')).split('=')[0]);
	return el.tagName === sel.toUpperCase();
}

/* ------------------------------------------------------------ the sandboxes */

function buildContext(opts) {
	opts = opts || {};
	var now = opts.now || 1789984800000;
	var seq = 0;
	var timers = {};
	var logs = [];
	var elements = {};

	var doc = new Element('document');
	doc.readyState = 'loading';
	doc.body = new Element('body');
	doc.documentElement = new Element('html');
	doc.activeElement = null;
	doc.listeners = 0;
	doc.addEventListener = function () { doc.listeners++; };
	doc.createElement = function (t) { return new Element(t); };
	doc.createElementNS = function (ns, t) { return new Element(t); };
	doc.getElementById = function (id) {
		if (!elements[id]) { elements[id] = new Element('div'); elements[id].setAttribute('id', id); }
		return elements[id];
	};

	var w = {
		document: doc,
		console: { log: function (m) { logs.push(String(m)); } },
		navigator: { userAgent: 'sidecrab-fixture' },
		innerWidth: opts.innerWidth || 2560,
		innerHeight: opts.innerHeight || 720,
		location: {
			protocol: 'http:',
			hostname: '127.0.0.1',
			port: '2722',
			pathname: '/panel/',
			origin: ORIGIN,
			href: PANEL_URL + (opts.search || ''),
			search: opts.search || ''
		},
		Date: (function () {
			function D() { return Reflect.construct(Date, arguments, D); }
			D.prototype = Date.prototype;
			D.now = function () { return now; };
			D.parse = Date.parse;
			D.UTC = Date.UTC;
			return D;
		})(),
		setTimeout: function (fn, ms) { var id = ++seq; timers[id] = { fn: fn, at: now + (ms || 0) }; return id; },
		clearTimeout: function (id) { delete timers[id]; },
		setInterval: function () { return ++seq; },
		clearInterval: function () {},
		/* The computed style the view-switcher capability is read off. The stylesheet
		   owns the 1660 px breakpoint (SCA-025), and this reproduces its one effect:
		   below it the view chips are display:none. */
		getComputedStyle: function (el) {
			var isChip = !!(el && ((el.classes && el.classes['view-chip']) ||
				String(el.id || '').indexOf('chipView') === 0));
			var hidden = w.innerWidth <= 1660 && isChip;
			return {
				display: hidden ? 'none' : 'flex',
				getPropertyValue: function () { return ''; }
			};
		},
		localStorage: (function () {
			var store = {};
			return {
				getItem: function (k) { return Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null; },
				setItem: function (k, v) { store[k] = String(v); },
				_store: store
			};
		})()
	};
	if (opts.host !== null) {
		w.__sidecrabHost = opts.host || { kind: 'standalone', props: {}, pairingCode: '' };
	}
	w.window = w;
	w.self = w;

	vm.createContext(w);
	vm.runInContext(fs.readFileSync(SRC, 'utf8'), w, { filename: 'sidecrab.js' });
	if (!doc.listeners) throw new Error('init() ran: the document stub was not in the loading state');

	/* The element registry every shipping function writes through. Minted on first
	   touch, for the reason in the header. `ready` is a flag the app sets, not an
	   element, so it is left alone. */
	var realUi = w.ui;
	w.ui = new Proxy(realUi, {
		get: function (t, k) {
			if (k === 'ready') return t.ready;
			if (typeof k !== 'string') return t[k];
			if (!(k in t) || t[k] === null || t[k] === undefined) {
				t[k] = new Element('div');
				/* The id is the ui key, which is how it is in the page: the elements
				   are resolved by getElementById off the same names. Some checks
				   (the view-switcher capability) read it. */
				t[k].setAttribute('id', k);
			}
			return t[k];
		},
		set: function (t, k, v) { t[k] = v; return true; }
	});

	return {
		w: w,
		doc: doc,
		logs: logs,
		elements: elements,
		now: function () { return now; },
		/* Advance the clock and fire every timer that has come due, in order. */
		advance: function (ms) {
			var target = now + ms;
			for (;;) {
				var next = null, id = null;
				for (var k in timers) {
					if (!Object.prototype.hasOwnProperty.call(timers, k)) continue;
					if (timers[k].at <= target && (next === null || timers[k].at < next.at)) { next = timers[k]; id = k; }
				}
				if (!next) break;
				now = next.at;
				delete timers[id];
				next.fn();
			}
			now = target;
		},
		pendingTimers: function () { return Object.keys(timers).length; }
	};
}

/* THE NATIVE PAGE. Served origin, a boot object, and a bridge that answers. The
   handshake is completed here rather than left to each test, because a capability
   is what every native behaviour is gated on and a fixture that skipped it would be
   testing the preview by accident. */
function nativePage(opts) {
	opts = opts || {};
	var ctx = buildContext({
		search: opts.search,
		innerWidth: opts.innerWidth,
		host: opts.host !== undefined ? opts.host : {
			kind: 'standalone',
			props: opts.props || {},
			pairingCode: opts.pairingCode || 'FIXTURE-CODE'
		}
	});
	var sent = [];
	var listener = null;
	ctx.w.chrome = {
		webview: {
			postMessage: function (m) { sent.push(m); },
			addEventListener: function (type, fn) { if (type === 'message') listener = fn; }
		}
	};
	ctx.sent = sent;
	ctx.reply = function (msg) { if (listener) listener({ data: msg }); };
	ctx.bridgeBoot = function (capabilities) {
		ctx.w.bridgeInit();
		var ask = sent[sent.length - 1];
		ctx.reply({
			type: 'host-info',
			requestId: ask.requestId,
			version: '0.4.0',
			pid: 4242,
			startedAt: '2026-09-21T09:00:00Z',
			settingsPath: 'D:\\panel\\panel-settings.json',
			hasToken: true,
			capabilities: capabilities || { saveSettings: true, focusSession: true, pickDisplay: false }
		});
		return ask;
	};
	if (opts.handshake !== false) ctx.bridgeBoot(opts.capabilities);
	return ctx;
}

/* THE SERVED BROWSER PREVIEW. The same URL, no bridge, no boot object. */
function previewPage(opts) {
	opts = opts || {};
	return buildContext({ search: opts.search, innerWidth: opts.innerWidth, host: null });
}

module.exports = {
	Element: Element,
	nativePage: nativePage,
	previewPage: previewPage,
	ORIGIN: ORIGIN,
	PANEL_URL: PANEL_URL
};
