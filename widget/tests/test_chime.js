/* SideCrab widget — the chime gates (lane B).
 *
 *   node widget/tests/test_chime.js
 *
 * WHY A VM AND NOT A MODULE, and why the decision is a pure function at all.
 * scripts/sidecrab.js is a flat browser script with no exports, and a second copy
 * of these gates in a test file would be a copy that can disagree with the panel —
 * so the SHIPPING file is loaded whole into a vm context with a document stub whose
 * readyState is 'loading', the branch a real browser takes before DOMContentLoaded,
 * which parks init() on a listener nobody fires. The same harness test_ordering.js
 * uses, and the same rule: if this file stops loading, the cause is new TOP-LEVEL
 * work in sidecrab.js, so add the stub it needs rather than forking the logic.
 *
 * The audio itself is deliberately NOT under test here: node has no AudioContext,
 * playChime feature-detects it and returns false, and a synthesized two-note chime
 * is something to listen to, not to assert on. What IS under test is every reason
 * the panel may NOT make a noise, because each of those is a promise to somebody
 * standing in a quiet room.
 *
 * MUTATION PROOF. The last section runs the NAIVE decision — "any needs_input row
 * is a chime" — against the same documents and shows it firing where the shipping
 * one is silent. A gate whose test cannot fail is a gate that reports success
 * forever.
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
		createElement: function () { throw new Error('the chime tests build no DOM'); }
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

function sess(id, state) { return { id: id, state: state }; }

function map(rows) {
	var out = {};
	for (var i = 0; i < rows.length; i++) out[rows[i].id] = rows[i].state;
	return out;
}

var NOW = 1774000000000;   /* any fixed instant; nothing here reads the wall clock */
var LONG_AGO = NOW - 600000;

/* The shipping decision with the two switches ON and quiet OFF, which is the
   default panel — so each test below changes exactly one thing. */
function decide(prev, next, opts) {
	opts = opts || {};
	return W.chimeDecision(
		prev,
		next,
		opts.quiet === undefined ? false : opts.quiet,
		opts.enabled === undefined ? true : opts.enabled,
		opts.now === undefined ? NOW : opts.now,
		opts.last === undefined ? LONG_AGO : opts.last);
}

/* ---------------------------------------------------------------- the edge */

var working = [sess('a', 'working'), sess('b', 'idle')];
var asked = [sess('a', 'needs_input'), sess('b', 'idle')];

ok(decide(map(working), asked) === true,
	'working -> needs_input chimes');
ok(decide(map(asked), asked) === false,
	'the same question still waiting does not chime again (re-render is not news)');
ok(decide(map([sess('a', 'done'), sess('b', 'idle')]), asked) === true,
	'done -> needs_input chimes: a finished session asking again is a new question');
ok(decide(map(working), working) === false,
	'a document with nothing waiting chimes nothing');
ok(decide(map([sess('a', 'needs_input')]), [sess('a', 'working')]) === false,
	'LEAVING needs_input never chimes');

/* Two rows, one already waiting and one arriving: the arrival is the news. */
ok(decide(map([sess('a', 'needs_input'), sess('b', 'working')]),
	[sess('a', 'needs_input'), sess('b', 'needs_input')]) === true,
	'a second session starting to wait chimes even while the first still waits');

/* --------------------------------------------------------------- the boot */

ok(decide(null, asked) === false,
	'BOOT: a panel that starts up while a session is already waiting is silent');
ok(decide(null, [sess('a', 'needs_input'), sess('b', 'needs_input'), sess('c', 'needs_input')]) === false,
	'BOOT: three waiting sessions are three chimes a panel must not play');
ok(decide({}, asked) === true,
	'a session unknown to the LAST document (not to the page) is a new alert');

/* --------------------------------------------------------- the smoke test */

ok(decide(map([sess('smoke-test', 'working')]), [sess('smoke-test', 'needs_input')]) === false,
	'the smoke test manufactures its own needs_input row and must never ring');
ok(decide(map([sess('smoke-test', 'working'), sess('a', 'working')]),
	[sess('smoke-test', 'needs_input'), sess('a', 'needs_input')]) === true,
	'a real session beside the smoke test still chimes');
ok(decide(null, [sess('smoke-test', 'needs_input')]) === false,
	'the smoke test does not ring at boot either');

/* ------------------------------------------------------------ the switches */

ok(decide(map(working), asked, { enabled: false }) === false,
	'the chime prop OFF is a hard off');
ok(decide(map(working), asked, { quiet: true }) === false,
	'quiet hours are a hard off');
ok(decide(map(working), asked, { quiet: false }) === true,
	'quiet ABSENT reads as false: crabd omits the block when no quiet hours are set');

/* ------------------------------------------------------------ the cooldown */

ok(decide(map(working), asked, { last: NOW - 1000 }) === false,
	'inside the 5 s cooldown, a second transition is silent');
ok(decide(map(working), asked, { last: NOW - 4999 }) === false,
	'4999 ms after the last chime is still inside the cooldown');
ok(decide(map(working), asked, { last: NOW - 5000 }) === true,
	'5000 ms after the last chime the cooldown has expired');
ok(W.CHIME_COOLDOWN_MS === 5000, 'the cooldown is the 5 s the contract states');
ok(W.CHIME_VOLUME_DEFAULT === 60, 'the default volume is 60');

/* -------------------------------------------------------------- total-ness */

ok(decide(map(working), null) === false, 'a document with no sessions array is silent, not an error');
ok(decide(map(working), [null, undefined, {}]) === false, 'malformed rows are skipped, not thrown on');
ok(decide(map(working), [{ state: 'needs_input' }]) === false, 'a row with no id cannot be tracked, so it cannot chime');

/* ------------------------------------------------------- the mutation proof */

/* The decision anyone would write first: ring for any waiting row. Every gate
   above exists because this one is wrong somewhere, so it must DISAGREE with the
   shipping function on each of them — if it ever stops disagreeing, the gate it
   was proving has gone. */
function naive(prev, next) {
	var rows = Array.isArray(next) ? next : [];
	for (var i = 0; i < rows.length; i++) {
		if (rows[i] && rows[i].state === 'needs_input') return true;
	}
	return false;
}

var MUTANT_CASES = [
	['boot with a session already waiting', null, asked, {}],
	['the same question re-rendered', map(asked), asked, {}],
	['the smoke test', map([sess('smoke-test', 'working')]), [sess('smoke-test', 'needs_input')], {}],
	['quiet hours', map(working), asked, { quiet: true }],
	['the chime switched off', map(working), asked, { enabled: false }],
	['inside the cooldown', map(working), asked, { last: NOW - 1000 }]
];

MUTANT_CASES.forEach(function (row) {
	var shipping = decide(row[1], row[2], row[3]);
	var mutant = naive(row[1], row[2]);
	ok(shipping === false && mutant === true,
		'MUTANT rings and the shipping gate does not: ' + row[0]);
});

/* ---------------------------------------------------------------------- done */

console.log((failures ? 'FAILED' : 'ok') + '  ' + (checks - failures) + '/' + checks + ' checks');
process.exit(failures ? 1 : 0);
