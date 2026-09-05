/* SideCrab widget — the ordering tests (v0.26.0, AUD-F5 + the fmtNum boundary).
 *
 *   node widget/tests/test_ordering.js
 *
 * WHY A VM AND NOT A MODULE, and the document stub that makes it work, both live
 * in _harness.js — shared with test_panel.js, because two copies of the stub are
 * two things to keep in step and the rule here has always been "add the stub it
 * needs, do not fork the logic".
 *
 * WHAT IS PINNED. The compact grid's "+N more" tile is acceptable only while a
 * WAITING (needs_input) card can never be the row it swallows. Until v0.26.0 the
 * widget held that by inheriting crabd's pre-sort — see clampGrid's comment — so
 * the mis-ordered case below is the one that used to fail, and the last test
 * proves it still fails against the old bare slice. A test that cannot fail is
 * worse than no test: it reports success forever.
 */
'use strict';

var loadWidget = require('./_harness.js').loadWidget;

var W = loadWidget({
	location: { search: '', href: 'http://127.0.0.1/index.html' },
	domReason: 'the ordering tests build no DOM'
});

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

/* ---------------------------------------------------------------------- done */

console.log((failures ? 'FAILED' : 'ok') + '  ' + (checks - failures) + '/' + checks + ' checks');
process.exit(failures ? 1 : 0);
