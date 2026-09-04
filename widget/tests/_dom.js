/* SideCrab widget — the light DOM shim.
 *
 * WHY THIS EXISTS AND WHY IT IS NOT jsdom. The panel is a flat browser script
 * with no bundler and no runtime dependency, and the suites that drive it run on
 * `node file.js` with nothing installed. A DOM here is a few hundred lines of
 * shim; a DOM from npm is a devDependency, a lockfile and an `npm ci` step in
 * CI for every contributor who only wanted to run one test.
 *
 * WHY IT PARSES index.html RATHER THAN LISTING IDS. The shipping markup is
 * strict XML — that is a CI-enforced invariant, not a hope — so a ~90 line
 * scanner reads it faithfully, and the tree the tests drive is the tree the
 * panel ships: real parents, so closest() answers the way it does on glass, and
 * real <meta name="x-icue-property"> tags, so a property added to index.html is
 * in the settings-sheet test the moment it lands. A hand-written list of element
 * ids would be a second copy of the markup, and a copy that can disagree.
 *
 * WHAT IT DELIBERATELY DOES NOT DO: layout (every rect is zeros), the cascade
 * (getComputedStyle answers a fixed shape), and selector combinators beyond a
 * comma-separated list of simple compound selectors. Anything that needs a real
 * engine is measured in a real browser and recorded in widget/DEV.md.
 */
'use strict';

var ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ' };

function decode(s) {
  return String(s).replace(/&(#x?[0-9A-Fa-f]+|[a-z]+);/g, function (all, body) {
    if (body.charAt(0) === '#') {
      var n = body.charAt(1) === 'x' || body.charAt(1) === 'X'
        ? parseInt(body.slice(2), 16) : parseInt(body.slice(1), 10);
      return isFinite(n) ? String.fromCharCode(n) : all;
    }
    return Object.prototype.hasOwnProperty.call(ENTITIES, body) ? ENTITIES[body] : all;
  });
}

function camel(s) { return String(s).replace(/-([a-z])/g, function (m, c) { return c.toUpperCase(); }); }

/* ------------------------------------------------------------------ elements */

function makeStyle() {
  var vars = {};
  return {
    getPropertyValue: function (n) { return Object.prototype.hasOwnProperty.call(vars, n) ? vars[n] : ''; },
    setProperty: function (n, v) { vars[n] = String(v); },
    removeProperty: function (n) { delete vars[n]; }
  };
}

function makeEl(doc, tag) {
  var el = {
    nodeType: 1,
    tagName: String(tag || 'div').toUpperCase(),
    children: [],
    parentNode: null,
    attributes: {},
    dataset: {},
    style: makeStyle(),
    listeners: {},
    hidden: false,
    disabled: false,
    checked: false,
    value: '',
    ownerDocument: doc,
    _classes: [],
    _text: ''
  };

  el.classList = {
    add: function () {
      for (var i = 0; i < arguments.length; i++) if (el._classes.indexOf(arguments[i]) < 0) el._classes.push(arguments[i]);
    },
    remove: function () {
      for (var i = 0; i < arguments.length; i++) {
        var j = el._classes.indexOf(arguments[i]);
        if (j >= 0) el._classes.splice(j, 1);
      }
    },
    toggle: function (c, on) {
      var has = el._classes.indexOf(c) >= 0;
      var want = arguments.length > 1 ? !!on : !has;
      if (want && !has) el._classes.push(c);
      if (!want && has) el.classList.remove(c);
      return want;
    },
    contains: function (c) { return el._classes.indexOf(c) >= 0; }
  };

  Object.defineProperty(el, 'className', {
    get: function () { return el._classes.join(' '); },
    set: function (v) { el._classes = String(v === null || v === undefined ? '' : v).split(/\s+/).filter(Boolean); }
  });

  Object.defineProperty(el, 'textContent', {
    get: function () {
      if (!el.children.length) return el._text;
      return el.children.map(function (c) { return c.textContent; }).join('');
    },
    set: function (v) {
      for (var i = 0; i < el.children.length; i++) el.children[i].parentNode = null;
      el.children = [];
      el._text = v === null || v === undefined ? '' : String(v);
    }
  });

  Object.defineProperty(el, 'firstChild', { get: function () { return el.children[0] || null; } });
  Object.defineProperty(el, 'lastChild', { get: function () { return el.children[el.children.length - 1] || null; } });
  Object.defineProperty(el, 'isConnected', {
    get: function () { var n = el; while (n.parentNode) n = n.parentNode; return n === doc.documentElement; }
  });

  el.setAttribute = function (n, v) {
    v = v === null || v === undefined ? '' : String(v);
    el.attributes[n] = v;
    if (n === 'class') el.className = v;
    else if (n === 'id') el.id = v;
    else if (n === 'value') el.value = v;
    else if (n.indexOf('data-') === 0) el.dataset[camel(n.slice(5))] = v;
  };
  el.getAttribute = function (n) {
    return Object.prototype.hasOwnProperty.call(el.attributes, n) ? el.attributes[n] : null;
  };
  el.hasAttribute = function (n) { return Object.prototype.hasOwnProperty.call(el.attributes, n); };
  el.removeAttribute = function (n) {
    delete el.attributes[n];
    if (n.indexOf('data-') === 0) delete el.dataset[camel(n.slice(5))];
  };

  el.appendChild = function (c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    c.parentNode = el;
    el._text = '';
    el.children.push(c);
    if (c.id) doc._ids[c.id] = c;
    return c;
  };
  el.removeChild = function (c) {
    var i = el.children.indexOf(c);
    if (i >= 0) { el.children.splice(i, 1); c.parentNode = null; }
    return c;
  };
  el.insertBefore = function (c, ref) {
    if (!ref) return el.appendChild(c);
    if (c.parentNode) c.parentNode.removeChild(c);
    var i = el.children.indexOf(ref);
    c.parentNode = el;
    el.children.splice(i < 0 ? el.children.length : i, 0, c);
    if (c.id) doc._ids[c.id] = c;
    return c;
  };

  el.addEventListener = function (type, fn) { (el.listeners[type] = el.listeners[type] || []).push(fn); };
  el.removeEventListener = function (type, fn) {
    var list = el.listeners[type] || [];
    var i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  };
  /* Bubbles the way a browser does, so a handler bound on #sheet hears a click on
     a control inside it — which is exactly how every sheet control is wired. */
  el.dispatch = function (type, ev) {
    ev = ev || {};
    ev.type = type;
    if (!ev.target) ev.target = el;
    if (!ev.preventDefault) ev.preventDefault = function () { ev.defaultPrevented = true; };
    if (!ev.stopPropagation) ev.stopPropagation = function () { ev._stopped = true; };
    var node = el;
    while (node) {
      var list = (node.listeners && node.listeners[type]) || [];
      for (var i = 0; i < list.slice().length; i++) list.slice()[i].call(node, ev);
      if (ev._stopped) break;
      node = node.parentNode;
    }
    return ev;
  };
  el.click = function () { return el.dispatch('click', { target: el }); };
  el.focus = function () { doc.activeElement = el; };
  el.blur = function () { if (doc.activeElement === el) doc.activeElement = doc.body; };
  el.getBoundingClientRect = function () {
    return { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
  };
  el.contains = function (o) { while (o) { if (o === el) return true; o = o.parentNode; } return false; };
  el.closest = function (sel) {
    var n = el;
    while (n && n.nodeType === 1) { if (matches(n, sel)) return n; n = n.parentNode; }
    return null;
  };
  el.querySelector = function (sel) { return queryAll(el, sel)[0] || null; };
  el.querySelectorAll = function (sel) { return queryAll(el, sel); };
  return el;
}

/* ----------------------------------------------------------------- selectors */

/* One compound simple selector: tag, #id, .class and [attr] / [attr="value"],
   in any combination. No combinators — every call site in the panel is either
   relative to the element it is searching from or a single compound. */
function matchesOne(el, sel) {
  var parts = String(sel).trim().match(/^[a-zA-Z][\w-]*|#[\w-]+|\.[\w-]+|\[[^\]]+\]/g);
  if (!parts) return false;
  if (parts.join('') !== String(sel).trim()) return false;
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i];
    if (p.charAt(0) === '#') { if (el.id !== p.slice(1)) return false; }
    else if (p.charAt(0) === '.') { if (!el.classList.contains(p.slice(1))) return false; }
    else if (p.charAt(0) === '[') {
      var m = /^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/.exec(p);
      if (!m) return false;
      if (!el.hasAttribute(m[1])) return false;
      if (m[2] !== undefined && el.getAttribute(m[1]) !== m[2]) return false;
    } else if (el.tagName !== p.toUpperCase()) return false;
  }
  return true;
}

function matches(el, sel) {
  var list = String(sel).split(',');
  for (var i = 0; i < list.length; i++) if (list[i].trim() && matchesOne(el, list[i])) return true;
  return false;
}

function queryAll(root, sel) {
  var out = [];
  (function walk(node) {
    for (var i = 0; i < node.children.length; i++) {
      var c = node.children[i];
      if (c.nodeType === 1) {
        if (matches(c, sel)) out.push(c);
        walk(c);
      }
    }
  })(root);
  return out;
}

/* -------------------------------------------------------------- the document */

/* A scanner, not a parser: index.html is strict XML by CI-enforced invariant, so
   the only shapes it has to know are comments, the doctype, CDATA, self-closed
   tags and raw-text elements. Anything else in the file is a bug the strict-XML
   check catches first. */
function parseInto(doc, parent, html) {
  var i = 0;
  var stack = [parent];
  var RAW = { SCRIPT: 1, STYLE: 1 };
  while (i < html.length) {
    var lt = html.indexOf('<', i);
    if (lt < 0) { text(html.slice(i)); break; }
    if (lt > i) text(html.slice(i, lt));
    if (html.substr(lt, 4) === '<!--') { i = html.indexOf('-->', lt) + 3; continue; }
    if (html.substr(lt, 9) === '<![CDATA[') {
      var end = html.indexOf(']]>', lt);
      text(html.slice(lt + 9, end));
      i = end + 3;
      continue;
    }
    if (html.charAt(lt + 1) === '!' || html.charAt(lt + 1) === '?') { i = html.indexOf('>', lt) + 1; continue; }
    var gt = html.indexOf('>', lt);
    if (gt < 0) break;
    var tagText = html.slice(lt + 1, gt);
    if (tagText.charAt(0) === '/') {
      if (stack.length > 1) stack.pop();
      i = gt + 1;
      continue;
    }
    var selfClose = tagText.charAt(tagText.length - 1) === '/';
    if (selfClose) tagText = tagText.slice(0, -1);
    var nm = /^([\w:-]+)/.exec(tagText);
    if (!nm) { i = gt + 1; continue; }
    var el = makeEl(doc, nm[1]);
    var attrRe = /([\w:.-]+)\s*=\s*"([^"]*)"/g, am;
    while ((am = attrRe.exec(tagText))) el.setAttribute(am[1], decode(am[2]));
    stack[stack.length - 1].appendChild(el);
    if (el.id) doc._ids[el.id] = el;
    i = gt + 1;
    if (selfClose) continue;
    if (RAW[el.tagName]) {
      var close = html.indexOf('</' + nm[1], i);
      if (close < 0) close = html.length;
      el._text = html.slice(i, close);
      i = html.indexOf('>', close) + 1;
      continue;
    }
    stack.push(el);
  }
  function text(s) {
    var t = decode(s);
    if (!/\S/.test(t)) return;
    var top = stack[stack.length - 1];
    if (!top.children.length) top._text += t;
  }
}

function makeDocument(html) {
  var doc = { _ids: {}, readyState: 'loading', listeners: {} };
  doc.createElement = function (tag) { return makeEl(doc, tag); };
  doc.createElementNS = function (ns, tag) { return makeEl(doc, tag); };
  doc.createTextNode = function (t) {
    var n = makeEl(doc, '#text');
    n.nodeType = 3;
    n.textContent = t;
    return n;
  };
  doc.documentElement = makeEl(doc, 'html');
  doc.documentElement.style = makeStyle();
  parseInto(doc, doc.documentElement, html);
  doc.head = doc.documentElement.querySelector('head') || doc.documentElement.appendChild(makeEl(doc, 'head'));
  doc.body = doc.documentElement.querySelector('body') || doc.documentElement.appendChild(makeEl(doc, 'body'));
  doc.activeElement = doc.body;
  doc.getElementById = function (id) { return doc._ids[id] || null; };
  doc.querySelector = function (sel) { return queryAll(doc.documentElement, sel)[0] || null; };
  doc.querySelectorAll = function (sel) { return queryAll(doc.documentElement, sel); };
  doc.addEventListener = function (type, fn) { (doc.listeners[type] = doc.listeners[type] || []).push(fn); };
  doc.removeEventListener = function (type, fn) {
    var list = doc.listeners[type] || [];
    var i = list.indexOf(fn);
    if (i >= 0) list.splice(i, 1);
  };
  /* Document-level listeners are how the gesture layer and the keyboard are
     bound, so a test drives them here. Capture and bubble are one list: nothing
     in the panel depends on the order between two document listeners. */
  doc.dispatch = function (type, ev) {
    ev = ev || {};
    ev.type = type;
    if (!ev.target) ev.target = doc.body;
    if (!ev.preventDefault) ev.preventDefault = function () { ev.defaultPrevented = true; };
    if (!ev.stopPropagation) ev.stopPropagation = function () { ev._stopped = true; };
    var list = (doc.listeners[type] || []).slice();
    for (var i = 0; i < list.length; i++) list[i].call(doc, ev);
    return ev;
  };
  return doc;
}

module.exports = { makeDocument: makeDocument, makeEl: makeEl, matches: matches };
