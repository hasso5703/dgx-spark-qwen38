'use strict';
/* A small DOM for running the cockpit's page scripts under node, without a browser.

   Built from the page's own markup (pagejs.py parses index.html and hands the tree over),
   so an id, a class or an <option> the tests rely on is the one the page really has. It
   covers what app.js and agent-mobile.js touch: elements, text, attributes and dataset,
   classList, tables, the selectors they use (descendant, tag, #id, .class, [attr],
   [attr="v"], comma lists), events, storage, matchMedia, fetch and timers on a virtual
   clock. Layout does not exist here: every box measures 0. A test that needs geometry
   belongs in a real browser (dashboard/tests/*.mjs).

   node fakedom.js job.json runs one job: {tree, opts, setup, scripts, body}. `setup` runs
   before the page scripts, `body` after them, both in the page's own global scope, and
   `body` ends with report(value); the value is printed as the last line of stdout. */
const fs = require('fs');
const vm = require('vm');

const VOID = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta',
                      'source', 'track', 'wbr']);
const REFLECT = {id: 'id', title: 'title', href: 'href', src: 'src', type: 'type', name: 'name',
                 placeholder: 'placeholder', alt: 'alt', rel: 'rel', target: 'target', htmlFor: 'for',
                 accept: 'accept', min: 'min', max: 'max', step: 'step', role: 'role'};
const BOOL = ['hidden', 'disabled', 'open', 'multiple', 'readOnly'];

const kebab = k => k.replace(/[A-Z]/g, c => '-' + c.toLowerCase());
const camel = k => k.replace(/-([a-z])/g, (_, c) => c.toUpperCase());

// ── selectors ────────────────────────────────────────────────────────────────
function splitOutside(s, sep){
  const out = []; let cur = '', depth = 0, quote = '';
  for (const ch of s){
    if (quote){ cur += ch; if (ch === quote) quote = ''; continue; }
    if (ch === '"' || ch === "'"){ quote = ch; cur += ch; continue; }
    if (ch === '[') depth++;
    if (ch === ']') depth--;
    if (depth === 0 && (sep === ' ' ? /\s/.test(ch) : ch === sep)){ if (cur.trim()) out.push(cur.trim()); cur = ''; continue; }
    cur += ch;
  }
  if (cur.trim()) out.push(cur.trim());
  return out;
}
function parseCompound(s){
  const m = s.match(/^([a-zA-Z][\w-]*|\*)?(.*)$/);
  const c = {tag: m[1] && m[1] !== '*' ? m[1].toLowerCase() : null, ids: [], classes: [], attrs: []};
  const re = /#([\w-]+)|\.([\w-]+)|\[\s*([\w-]+)\s*(?:=\s*(?:"([^"]*)"|'([^']*)'|([^\]\s]+)))?\s*\]/g;
  let rest = m[2], x, used = 0;
  while ((x = re.exec(rest))){
    if (x.index !== used) throw new Error('fakedom: unsupported selector ' + JSON.stringify(s));
    used = re.lastIndex;
    if (x[1]) c.ids.push(x[1]);
    else if (x[2]) c.classes.push(x[2]);
    else c.attrs.push({name: x[3].toLowerCase(), value: x[4] ?? x[5] ?? x[6] ?? null});
  }
  if (used !== rest.length) throw new Error('fakedom: unsupported selector ' + JSON.stringify(s));
  return c;
}
const CACHE = new Map();
function parseSelector(sel){
  if (!CACHE.has(sel)) CACHE.set(sel, splitOutside(sel, ',').map(part => splitOutside(part, ' ').map(parseCompound)));
  return CACHE.get(sel);
}
function matchCompound(el, c){
  if (!el || el.nodeType !== 1) return false;
  if (c.tag && el.localName !== c.tag) return false;
  for (const id of c.ids) if (el.getAttribute('id') !== id) return false;
  const cls = (el.getAttribute('class') || '').split(/\s+/);
  for (const k of c.classes) if (!cls.includes(k)) return false;
  for (const a of c.attrs){
    if (!el.hasAttribute(a.name)) return false;
    if (a.value !== null && el.getAttribute(a.name) !== a.value) return false;
  }
  return true;
}
function matchComplex(el, chain){
  if (!matchCompound(el, chain[chain.length - 1])) return false;
  let i = chain.length - 2, cur = el.parentElement;
  while (i >= 0 && cur){ if (matchCompound(cur, chain[i])) i--; cur = cur.parentElement; }
  return i < 0;
}
const matchesSel = (el, sel) => parseSelector(sel).some(chain => matchComplex(el, chain));

// ── nodes ─────────────────────────────────────────────────────────────────────
class FNode {
  constructor(doc){ this.ownerDocument = doc; this.parentNode = null; this.childNodes = []; }
  get parentElement(){ return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  get firstChild(){ return this.childNodes[0] || null; }
  get lastChild(){ return this.childNodes[this.childNodes.length - 1] || null; }
  get nextSibling(){ const p = this.parentNode; return p ? p.childNodes[p.childNodes.indexOf(this) + 1] || null : null; }
  remove(){ if (this.parentNode) this.parentNode.removeChild(this); }
}
class FText extends FNode {
  constructor(doc, data){ super(doc); this.nodeType = 3; this.data = String(data); }
  get textContent(){ return this.data; }
  set textContent(v){ this.data = String(v); }
}
function makeStyle(){
  const props = {};
  return new Proxy(props, {
    get(t, k){
      if (k === 'setProperty') return (n, v) => { t[n] = String(v); };
      if (k === 'getPropertyValue') return n => t[n] ?? '';
      if (k === 'removeProperty') return n => { delete t[n]; };
      return t[k] ?? '';
    },
    set(t, k, v){ t[k] = String(v); return true; },
  });
}
class FElement extends FNode {
  constructor(doc, tag, attrs){
    super(doc);
    this.nodeType = 1; this.localName = tag.toLowerCase(); this.tagName = tag.toUpperCase();
    this._attrs = new Map(attrs || []);
    this.style = makeStyle(); this._listeners = {}; this.scrollTop = 0; this.scrollLeft = 0;
    this.dataset = new Proxy({}, {
      get: (t, k) => typeof k === 'string' ? this._attrs.get('data-' + kebab(k)) : undefined,
      set: (t, k, v) => { this._attrs.set('data-' + kebab(k), String(v)); return true; },
      deleteProperty: (t, k) => { this._attrs.delete('data-' + kebab(k)); return true; },
      has: (t, k) => this._attrs.has('data-' + kebab(k)),
      ownKeys: () => [...this._attrs.keys()].filter(n => n.startsWith('data-')).map(n => camel(n.slice(5))),
      getOwnPropertyDescriptor: (t, k) => this._attrs.has('data-' + kebab(k))
        ? {value: this._attrs.get('data-' + kebab(k)), enumerable: true, configurable: true} : undefined,
    });
    if (this.localName === 'input' || this.localName === 'textarea'){
      this._value = this._attrs.get('value') ?? '';
      this.checked = this._attrs.has('checked');
    }
    if (this.localName === 'canvas'){ this.width = 300; this.height = 150; }
  }
  getAttribute(n){ n = n.toLowerCase(); return this._attrs.has(n) ? this._attrs.get(n) : null; }
  setAttribute(n, v){ this._attrs.set(n.toLowerCase(), String(v)); }
  removeAttribute(n){ this._attrs.delete(n.toLowerCase()); }
  hasAttribute(n){ return this._attrs.has(n.toLowerCase()); }
  get attributes(){ return [...this._attrs].map(([name, value]) => ({name, value})); }
  get className(){ return this.getAttribute('class') || ''; }
  set className(v){ this.setAttribute('class', v); }
  get classList(){
    const self = this, list = () => self.className.split(/\s+/).filter(Boolean);
    const put = a => { self.className = a.join(' '); };
    return {
      add(...c){ const a = list(); c.forEach(x => { if (!a.includes(x)) a.push(x); }); put(a); },
      remove(...c){ put(list().filter(x => !c.includes(x))); },
      toggle(c, force){ const has = list().includes(c); const on = force === undefined ? !has : !!force;
        if (on && !has) this.add(c); if (!on && has) this.remove(c); return on; },
      contains(c){ return list().includes(c); },
      get length(){ return list().length; },
    };
  }
  get children(){ return this.childNodes.filter(n => n.nodeType === 1); }
  get childElementCount(){ return this.children.length; }
  get firstElementChild(){ return this.children[0] || null; }
  get nextElementSibling(){ const p = this.parentNode; if (!p) return null;
    const sib = p.children; return sib[sib.indexOf(this) + 1] || null; }
  get previousElementSibling(){ const p = this.parentNode; if (!p) return null;
    const sib = p.children; return sib[sib.indexOf(this) - 1] || null; }
  get textContent(){ return this.childNodes.map(n => n.textContent).join(''); }
  set textContent(v){ this.childNodes.forEach(n => { n.parentNode = null; }); this.childNodes = [];
    if (v !== '' && v != null) this.appendChild(new FText(this.ownerDocument, v)); }
  get innerText(){ return this.textContent; }
  set innerText(v){ this.textContent = v; }
  get innerHTML(){ return this._html ?? this.textContent; }
  set innerHTML(v){ this.textContent = ''; this._html = String(v); }
  _adopt(n){ if (typeof n === 'string') n = new FText(this.ownerDocument, n);
    if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; return n; }
  appendChild(n){ n = this._adopt(n); this.childNodes.push(n); return n; }
  append(...ns){ ns.forEach(n => this.appendChild(n)); }
  prepend(...ns){ ns.reverse().forEach(n => this.insertBefore(n, this.firstChild)); }
  insertBefore(n, ref){ n = this._adopt(n); const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i < 0) this.childNodes.push(n); else this.childNodes.splice(i, 0, n); return n; }
  removeChild(n){ const i = this.childNodes.indexOf(n); if (i >= 0){ this.childNodes.splice(i, 1); n.parentNode = null; } return n; }
  replaceChildren(...ns){ this.textContent = ''; this.append(...ns); }
  before(...ns){ const p = this.parentNode; if (p) ns.forEach(n => p.insertBefore(n, this)); }
  contains(n){ for (let c = n; c; c = c.parentNode) if (c === this) return true; return false; }
  *_walk(){ for (const c of this.childNodes) if (c.nodeType === 1){ yield c; yield* c._walk(); } }
  querySelectorAll(sel){ return [...this._walk()].filter(e => matchesSel(e, sel)); }
  querySelector(sel){ for (const e of this._walk()) if (matchesSel(e, sel)) return e; return null; }
  matches(sel){ return matchesSel(this, sel); }
  closest(sel){ for (let e = this; e && e.nodeType === 1; e = e.parentNode) if (matchesSel(e, sel)) return e; return null; }
  addEventListener(type, fn){ (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener(type, fn){ this._listeners[type] = (this._listeners[type] || []).filter(f => f !== fn); }
  dispatchEvent(ev){
    ev.target = ev.target || this;
    let stop = false; ev.stopPropagation = () => { stop = true; }; ev.preventDefault = ev.preventDefault || (() => {});
    // the target, then its ancestors up to the document (no capture phase: nothing here
    // depends on the order)
    for (let n = this; n && !stop; n = n.parentNode){
      ev.currentTarget = n;
      (n._listeners[ev.type] || []).slice().forEach(f => f.call(n, ev));
      if (ev.type === 'click' && n === this && typeof n.onclick === 'function') n.onclick(ev);
    }
    return true;
  }
  click(){ if (this.disabled) return; this.dispatchEvent({type: 'click', bubbles: true}); }
  focus(){ if (this.ownerDocument) this.ownerDocument.activeElement = this; }
  blur(){}
  select(){}
  setSelectionRange(){}
  scrollTo(){}
  scrollIntoView(){}
  setPointerCapture(){}
  getBoundingClientRect(){ return {top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0}; }
  get offsetWidth(){ return 0; } get offsetHeight(){ return 0; } get offsetLeft(){ return 0; }
  get clientWidth(){ return 0; } get clientHeight(){ return 0; } get scrollWidth(){ return 0; }
  get scrollHeight(){ return 0; } get offsetParent(){ return null; }
  // form controls
  get value(){
    if (this.localName === 'select'){
      const opts = this.querySelectorAll('option');
      const sel = opts.find(o => o._selected) || (this._none ? null : opts.find(o => o.hasAttribute('selected')) || opts[0]);
      return sel ? sel.value : '';
    }
    if (this.localName === 'option') return this.hasAttribute('value') ? this.getAttribute('value') : this.textContent;
    return this._value ?? '';
  }
  set value(v){
    v = String(v);
    if (this.localName === 'select'){
      const opts = this.querySelectorAll('option');
      opts.forEach(o => { o._selected = false; });
      const hit = opts.find(o => o.value === v);
      if (hit) hit._selected = true;
      this._none = !hit;
      return;
    }
    if (this.localName === 'option'){ this.setAttribute('value', v); return; }
    this._value = v;
  }
  get options(){ return this.querySelectorAll('option'); }
  // tables
  get tBodies(){ return this.children.filter(c => c.localName === 'tbody'); }
  get tHead(){ return this.children.find(c => c.localName === 'thead') || null; }
  get rows(){ return this.localName === 'table' ? this.querySelectorAll('tr') : this.children.filter(c => c.localName === 'tr'); }
  get cells(){ return this.children.filter(c => c.localName === 'td' || c.localName === 'th'); }
  insertRow(){ const tr = this.ownerDocument.createElement('tr'); this.appendChild(tr); return tr; }
  insertCell(){ const td = this.ownerDocument.createElement('td'); this.appendChild(td); return td; }
  get colSpan(){ return Number(this.getAttribute('colspan') || 1); }
  set colSpan(v){ this.setAttribute('colspan', v); }
  // canvas
  getContext(){
    const noop = () => {};
    return new Proxy({}, {get: (t, k) => k in t ? t[k] : (k === 'createLinearGradient' ? () => ({addColorStop: noop}) : noop),
                          set: (t, k, v) => { t[k] = v; return true; }});
  }
}
for (const [prop, attr] of Object.entries(REFLECT)){
  Object.defineProperty(FElement.prototype, prop, {
    get(){ return this.getAttribute(attr) ?? ''; },
    set(v){ this.setAttribute(attr, v); }, configurable: true});
}
for (const prop of BOOL){
  Object.defineProperty(FElement.prototype, prop, {
    get(){ return this.hasAttribute(prop.toLowerCase()); },
    set(v){ if (v) this.setAttribute(prop.toLowerCase(), ''); else this.removeAttribute(prop.toLowerCase()); },
    configurable: true});
}

class FDocument extends FElement {
  constructor(){ super(null, '#document'); this.ownerDocument = this; this.nodeType = 9;
    this.readyState = 'interactive'; this.hidden = false; this.activeElement = null; this.visibilityState = 'visible'; }
  createElement(tag){ return new FElement(this, tag); }
  createTextNode(s){ return new FText(this, s); }
  getElementById(id){ for (const e of this._walk()) if (e.getAttribute('id') === id) return e; return null; }
  get documentElement(){ return this.children[0]; }
  get body(){ return this.documentElement.querySelector('body'); }
  get head(){ return this.documentElement.querySelector('head'); }
  dispatchEvent(ev){ ev.target = ev.target || this; (this._listeners[ev.type] || []).slice().forEach(f => f.call(this, ev)); return true; }
}
function build(doc, parent, node){
  if (typeof node === 'string'){ parent.appendChild(new FText(doc, node)); return; }
  const [tag, attrs, kids] = node;
  const e = new FElement(doc, tag, attrs);
  parent.appendChild(e);
  (kids || []).forEach(k => build(doc, e, k));
}

// ── the window ────────────────────────────────────────────────────────────────
function makeWindow(tree, opts = {}){
  const doc = new FDocument();
  build(doc, doc, tree);
  const clock = {now: opts.now || Date.UTC(2026, 8, 24, 10, 0, 0), seq: 0, timers: new Map()};
  const addTimer = (fn, ms, every) => { const id = ++clock.seq;
    clock.timers.set(id, {fn, at: clock.now + Math.max(0, Number(ms) || 0), every: every ? Math.max(1, Number(ms) || 0) : 0}); return id; };
  const RealDate = Date;
  class FakeDate extends RealDate {
    constructor(...a){ if (a.length) super(...a); else super(clock.now); }
    static now(){ return clock.now; }
  }
  const store = () => { const m = new Map(); return {
    getItem: k => m.has(String(k)) ? m.get(String(k)) : null, setItem: (k, v) => { m.set(String(k), String(v)); },
    removeItem: k => { m.delete(String(k)); }, clear: () => m.clear(), key: i => [...m.keys()][i] ?? null,
    get length(){ return m.size; } }; };
  const refusing = {getItem(){ throw new Error('SecurityError: storage is disabled'); },
                    setItem(){ throw new Error('SecurityError: storage is disabled'); },
                    removeItem(){ throw new Error('SecurityError: storage is disabled'); }};
  const media = opts.media || {};
  const fetches = [];
  const win = {
    document: doc, console, JSON, Math, Promise, Object, Array, String, Number, Boolean, RegExp, Error,
    TypeError, Map, Set, WeakMap, Symbol, Proxy, Reflect, Uint8Array, Uint32Array, ArrayBuffer, DataView,
    parseInt, parseFloat, isNaN, isFinite, encodeURIComponent, decodeURIComponent, structuredClone,
    Date: FakeDate, Intl,
    setTimeout: (fn, ms) => addTimer(fn, ms, false), setInterval: (fn, ms) => addTimer(fn, ms, true),
    clearTimeout: id => { clock.timers.delete(id); }, clearInterval: id => { clock.timers.delete(id); },
    queueMicrotask,
    location: Object.assign({hash: '', hostname: '127.0.0.1', port: '30090', pathname: '/', protocol: 'http:',
                             origin: 'http://127.0.0.1:30090', href: 'http://127.0.0.1:30090/', search: ''}, opts.location || {}),
    history: {replaceState(s, t, url){ if (typeof url === 'string' && url.startsWith('#')) win.location.hash = url; },
              pushState(){}},
    matchMedia: q => ({matches: !!media[q], media: q, addEventListener(){}, removeEventListener(){}, addListener(){}}),
    localStorage: opts.noStorage ? refusing : store(), sessionStorage: opts.noStorage ? refusing : store(),
    getComputedStyle: () => ({getPropertyValue: () => ''}),
    innerWidth: opts.width || 1280, innerHeight: opts.height || 800, devicePixelRatio: 1, scrollY: 0,
    scrollTo(){}, addEventListener(type, fn){ (win._listeners[type] = win._listeners[type] || []).push(fn); },
    removeEventListener(){}, _listeners: {},
    navigator: {clipboard: {writeText: async () => {}}, userAgent: 'fakedom'},
    isSecureContext: true, crypto: {subtle: {}},
    btoa: s => Buffer.from(String(s), 'binary').toString('base64'),
    atob: s => Buffer.from(String(s), 'base64').toString('binary'),
    EventSource: class { constructor(url){ this.url = url; (win.__eventsources = win.__eventsources || []).push(this); } close(){ this.closed = true; } },
    Image: class { set src(v){ this._src = v; } get src(){ return this._src; } },
    FileReader: class { readAsDataURL(){} },
    Path2D: class { moveTo(){} lineTo(){} },
    // every request the page makes is recorded; a test answers them by setting __fetch
    fetch: (url, init) => { fetches.push({url: String(url), init: init || {}, at: clock.now});
      return win.__fetch ? Promise.resolve().then(() => win.__fetch(String(url), init || {})) : new Promise(() => {}); },
    __fetches: fetches, __clock: clock,
    // a fetch Response the way the page reads one
    __response: (status, body) => ({status, ok: status >= 200 && status < 300,
                                    json: async () => (typeof body === 'string' ? JSON.parse(body) : body),
                                    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
                                    headers: {get: () => null}}),
    // run every timer due in the next `ms` of virtual time, in order, letting the
    // promises each one starts settle before the next
    __advance: async ms => {
      const end = clock.now + ms;
      for (;;){
        let next = null;
        for (const [id, t] of clock.timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
        if (!next) break;
        const [id, t] = next;
        clock.now = Math.max(clock.now, t.at);
        if (t.every) t.at = clock.now + t.every; else clock.timers.delete(id);
        try { t.fn(); } catch (e) { (win.__errors = win.__errors || []).push(String(e && e.stack || e)); }
        await new Promise(r => setImmediate(r));
      }
      clock.now = end;
      await new Promise(r => setImmediate(r));
    },
    __settle: () => new Promise(r => setImmediate(() => setImmediate(r))),
  };
  win.window = win; win.self = win; win.globalThis = win;
  doc.defaultView = win;
  return win;
}

function runJob(job){
  const win = makeWindow(job.tree, job.opts || {});
  const ctx = vm.createContext(win);
  let result, reported = false;
  win.report = v => { result = v; reported = true; };
  if (job.setup) vm.runInContext(job.setup, ctx, {filename: 'setup.js'});
  // deferred scripts run while the document is 'interactive', the way the page loads app.js
  for (const s of job.scripts || []) vm.runInContext(fs.readFileSync(s, 'utf8'), ctx, {filename: s});
  win.document.readyState = 'complete';
  const body = vm.runInContext('(async () => {\n' + (job.body || '') + '\n})()', ctx, {filename: 'body.js'});
  return Promise.resolve(body).then(() => {
    if (!reported) throw new Error('the test body never called report()');
    return {result, errors: win.__errors || []};
  });
}

if (require.main === module){
  const job = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  runJob(job).then(out => { process.stdout.write('\n__RESULT__' + JSON.stringify(out) + '\n'); process.exit(0); },
                   e => { process.stdout.write('\n__ERROR__' + JSON.stringify(String(e && e.stack || e)) + '\n'); process.exit(3); });
}
module.exports = {makeWindow, runJob, matchesSel};
