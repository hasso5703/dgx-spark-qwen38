"use strict";
/* Spark cockpit, the base every view stands on.

   One state payload (a server-sent event stream, with polling behind it) drives every
   view through renderers that write text nodes only, never HTML built from data. Each
   renderer is registered against the collector it reads (on()), runs isolated (one that
   throws marks its own panels and nothing else), and every panel says how fresh its
   sources are. Every action goes through one sheet that shows its exact command, one
   server-side job lock, and one dock visible from every view and every browser.

   The files load in order: base, the views, then boot.js starts the transport. */

// ── small tools ─────────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
const GIB = 1024 ** 3;
const gib = b => b == null || isNaN(b) ? null : b / GIB;
// gibibytes, said as such: 1024 cubed is a GiB, not a GB
const fmtGiB = (b, d = 1) => b == null || isNaN(b) ? '…' : (b / GIB).toFixed(d) + ' GiB';
const fmtN = n => n == null || isNaN(n) ? '…' : Number(n).toLocaleString('en');
const fmtK = n => n == null ? '?' : n >= 1e6 ? (n / 1e6).toFixed(2).replace(/\.?0+$/, '') + 'M' : Math.round(n / 1000) + 'K';
// rounded once, before it is split: minutes floored and seconds rounded read "8 min 60"
const fmtDur = s => {
  if (s == null || isNaN(s)) return '?';
  const r = Math.max(0, Math.round(s));
  return r < 90 ? r + ' s' : r < 3600 ? Math.floor(r / 60) + ' min ' + String(r % 60).padStart(2, '0')
    : Math.floor(r / 3600) + ' h ' + String(Math.floor((r % 3600) / 60)).padStart(2, '0');
};
// minutes for a plan, not a stopwatch: "about 13 min"
const fmtMin = s => s == null ? '?' : s < 90 ? Math.round(s) + ' s' : s < 3600 ? Math.round(s / 60) + ' min'
  : Math.floor(s / 3600) + ' h ' + String(Math.round((s % 3600) / 60)).padStart(2, '0');
const fmtClock = s => { const r = Math.max(0, Math.round(s || 0)); const h = Math.floor(r / 3600), m = Math.floor((r % 3600) / 60);
  return (h ? h + ':' + String(m).padStart(2, '0') : String(m)) + ':' + String(r % 60).padStart(2, '0'); };
// a time of day is ambiguous once the page reloads events written on another day
const clockTime = ts => {
  const d = new Date(ts * 1000), now = new Date();
  const hm = d.toLocaleTimeString([], {hour12: false, hour: '2-digit', minute: '2-digit'});
  if (d.toDateString() === now.toDateString()) return hm;
  return d.toLocaleDateString([], {day: '2-digit', month: '2-digit'}) + ' ' + hm;
};
const el = (tag, cls, txt) => { const n = document.createElement(tag); if (cls) n.className = cls; if (txt != null) n.textContent = txt; return n; };
const clear = n => { if (n) while (n.firstChild) n.removeChild(n.firstChild); };
const setText = (id, txt) => { const e = typeof id === 'string' ? $(id) : id; if (e && e.textContent !== String(txt)) e.textContent = txt; };
const setTitle = (id, txt) => { const e = typeof id === 'string' ? $(id) : id; if (e) e.title = txt || ''; };
const show = (id, on) => { const e = typeof id === 'string' ? $(id) : id; if (e) e.hidden = !on; };
const cssVar = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
// quoted for a shell: an apostrophe in a prompt must not end the string halfway
const shq = s => "'" + String(s).replace(/'/g, `'\\''`) + "'";
function cap(id, text, kind, lamp){
  const e = typeof id === 'string' ? $(id) : id; if (!e) return;
  clear(e); e.className = 'cap' + (kind ? ' ' + kind : '');
  if (lamp !== false){ const l = el('span', 'lamp' + (kind === 'ok' ? ' ok' : kind === 'warn' ? ' warn' : kind === 'err' ? ' err' : kind === 'cool' ? ' cool' : '') + (lamp === 'live' ? ' live' : '')); e.append(l); }
  e.append(document.createTextNode(text));
}
function facts(dl, rows){
  clear(dl);
  rows.forEach(([k, v, cls]) => { if (v == null) return; dl.append(el('dt', null, k)); const dd = el('dd', cls || null, v); dl.append(dd); });
}

// ── vocabulary: one word per engine state, everywhere ─────────────────────────
const STATE_LABEL = {
  stopped: 'stopped', failed: 'failed', starting: 'starting', 'loading-weights': 'loading weights',
  'loading-draft': 'loading the draft head', 'allocating-kv': 'allocating the KV pool',
  'capturing-graphs': 'capturing CUDA graphs', 'warming-up': 'warming up', ready: 'ready',
  degraded: 'ready but not answering', stopping: 'stopping', wedged: 'wedged: no generation',
  orphan: 'running outside systemd'};
const STATE_KIND = {ready: 'ok', degraded: 'warn', failed: 'err', stopped: '', stopping: 'warn', wedged: 'err', orphan: 'err'};
const TRANSITIONAL = new Set(['starting', 'loading-weights', 'loading-draft', 'allocating-kv', 'capturing-graphs', 'warming-up']);
// a unit that is not running, whatever its container does: its button offers Start
const UNIT_DOWN = new Set(['stopped', 'failed', 'orphan']);
const stateKind = st => STATE_KIND[st] ?? 'warn';
const stateLive = st => st === 'stopping' || TRANSITIONAL.has(st) ? 'live' : true;
const STAGE_LABEL = {'init': 'init', 'loading-weights': 'weights', 'loading-draft': 'draft', 'allocating-kv': 'KV pool',
                     'capturing-graphs': 'graphs', 'warming-up': 'warmup'};
const ALL_STAGES = Object.keys(STAGE_LABEL);

const U27 = 'qwen38-sglang.service', UFLASH = 'qwen38-flash.service', IMAGE_UNIT = 'qwen38-image.service',
      VIDEO_UNIT = 'qwen38-video.service', PROXY_UNIT = 'qwen38-keepalive.service', AGENT_UNIT = 'opencode-web.service';
const LANE_UNITS = [U27, UFLASH, IMAGE_UNIT, VIDEO_UNIT];
const LANE_NAME = {[U27]: '27B', [UFLASH]: 'flash 176B', [IMAGE_UNIT]: 'Qwen-Image', [VIDEO_UNIT]: 'MiniMax-H3'};
// What each lane is, for people: the kind of work it does and the one line that says so.
const LANE_META = {
  [U27]: {title: 'Qwen3.8 27B', kind: 'Text, up to 1M tokens of context', desc: 'Dense 27B in NVFP4 with a DFlash2 draft. The long-context lane.', view: 'agent'},
  [UFLASH]: {title: 'Flash 176B', kind: 'Text, 262K tokens of context', desc: 'Mixture of experts, 176B in NVFP4 with its own draft head. The fast lane for agents.', view: 'agent'},
  [IMAGE_UNIT]: {title: 'Qwen-Image 2.1', kind: 'Images', desc: 'Create, edit with up to ten references, cut out with a real alpha channel.', view: 'image'},
  [VIDEO_UNIT]: {title: 'MiniMax-H3', kind: 'Video with sound', desc: 'Video and audio made together, from a prompt or from keyframes.', view: 'video'}};
// How long each lane takes to answer after a start, before the box has seen one of its
// boots (measured on the reference box). Once a lane booted in front of the cockpit, the
// median of its own boots replaces these.
const READY_DEFAULT = {[U27]: 540, [UFLASH]: 780, [IMAGE_UNIT]: 70, [VIDEO_UNIT]: 720};
const TARGET_SHORT = {stock: 'stock', uncensored: 'uncensored', fp8: 'FP8', 'uncensored-fp8': 'FP8 uncensored',
                      flash: '', 'flash-uncensored': 'uncensored', 'flash-nvda': 'NVIDIA export', image: '2.1', video: 'H3'};
const TARGET_UNIT = t => t === 'image' ? IMAGE_UNIT : t === 'video' ? VIDEO_UNIT : String(t).startsWith('flash') ? UFLASH : U27;
const LANE_TARGETS = {[U27]: ['stock', 'uncensored', 'fp8', 'uncensored-fp8'], [UFLASH]: ['flash', 'flash-uncensored', 'flash-nvda'],
                      [IMAGE_UNIT]: ['image'], [VIDEO_UNIT]: ['video']};
const TARGET_NAME = {stock: '27B stock, NVFP4', uncensored: '27B uncensored, NVFP4', fp8: '27B FP8, Qwen official',
                     'uncensored-fp8': '27B FP8 uncensored', flash: 'flash 176B, NVFP4', 'flash-uncensored': 'flash 176B uncensored',
                     'flash-nvda': 'flash 176B, NVIDIA export', image: 'Qwen-Image 2.1', video: 'MiniMax-H3'};
const TARGET_NOTE = {
  stock: 'The NVFP4 quantization this repo pins by default: smallest and fastest of the 27B targets.',
  uncensored: 'The abliterated NVFP4 checkpoint: same size and speed as stock, refusals removed.',
  fp8: 'Qwen’s own FP8 release: the most faithful weights of this lane, and the heaviest. 30.9 GB against 21 GB for NVFP4, taken out of the KV pool: around 200,000 fewer tokens of context and a slower decode. The first switch downloads about 31 GB.',
  'uncensored-fp8': 'The abliterated weights in Qwen’s FP8 format: the same 30.9 GB and the same cost in pool and speed as FP8. The first switch downloads about 31 GB.',
  flash: 'The 176B Flash-Next lane: its own unit, its own image, and a 47.7 GiB N-gram table on the NVMe that the server rewrites at every boot.',
  'flash-uncensored': 'The abliterated build of the same 176B tree, byte-identical in layout, so every serving flag is the same. The first switch downloads about 126 GB.',
  'flash-nvda': 'NVIDIA’s own mixed-precision export of the same model, served with a pinned MoE runner. The first switch downloads about 124 GB.',
  image: 'Text to image, editing with up to ten references, native RGBA. 31 GB of weights that do not fit beside a text lane.',
  video: 'Text to video with joint audio, plus first and last frame conditioning. The checkpoint does not fit beside a text lane.'};
const LANE_INSTALL = {[U27]: './install.sh', [UFLASH]: 'MODEL_CHOICE=flash ./install.sh', [IMAGE_UNIT]: './install.sh --with-image',
                      [VIDEO_UNIT]: './install.sh --with-video'};

// ── the facts every view shares (each guarded against missing data) ──────────
const F = {life: null, units: {}, config: {}, job: null, load: {}, pool: null, window: 262144, ceiling: 0, usable: 0.92,
           maxRun: null, target: null, containers: {}, proxy: null, agent: null, update: null, memFloor: null, machine: null,
           gpu: null, ocfit: null};
const engines = () => (F.life && F.life.engines) || {};
function servingEngine(){ return Object.entries(engines()).find(([, e]) => !UNIT_DOWN.has(e.state) || e.restarting) || null; }
function enabledUnit(){
  const e = Object.entries(F.units).find(([n, u]) => LANE_UNITS.includes(n) && u.enabled === 'enabled');
  return e ? e[0] : U27;
}
function servingReady(){ const s = servingEngine(); return !!(s && ['ready', 'degraded', 'wedged'].includes(s[1].state)); }
// A TEXT engine is up: the pool, the context window, flush, abort and smoke all talk to
// the text engine on :30000, and with a diffusion lane serving that port is closed.
function textReady(){ const s = servingEngine(); return servingReady() && !!s && s[0] !== IMAGE_UNIT && s[0] !== VIDEO_UNIT; }
const imageServing = () => { const s = servingEngine(); return !!s && s[0] === IMAGE_UNIT; };
const videoServing = () => { const s = servingEngine(); return !!s && s[0] === VIDEO_UNIT; };
function bootSeconds(unit){
  const b = ((engines()[unit] || {}).boots || []).slice().sort((a, c) => a - c);
  return b.length ? b[Math.floor(b.length / 2)] : (READY_DEFAULT[unit] || 540);
}
const readyIn = unit => 'about ' + fmtDur(bootSeconds(unit));
// the text engine's served target, when it is one of this unit's
const ownTarget = unit => F.target && TARGET_UNIT(F.target) === unit ? F.target : null;
function laneTarget(unit){
  const serving = servingEngine();
  // A target of another lane is the previous engine's, read before this one answered.
  return (serving && serving[0] === unit && unit !== IMAGE_UNIT && unit !== VIDEO_UNIT && ownTarget(unit)) || (engines()[unit] || {}).target || null;
}
function laneLabel(unit){
  if (unit === AGENT_UNIT) return 'the opencode web server';
  if (unit === PROXY_UNIT) return 'the keepalive proxy';
  const base = LANE_NAME[unit] || unit.replace('.service', '');
  const t = laneTarget(unit);
  // a lane named after its checkpoint (MiniMax-H3) does not repeat it: "MiniMax-H3 H3"
  const s = t && TARGET_SHORT[t] && !base.endsWith(TARGET_SHORT[t]) ? ' ' + TARGET_SHORT[t] : '';
  return base + s;
}
const installed = unit => F.units[unit] ? F.units[unit].enabled !== '' : !!engines()[unit];
const blockedFor = key => ((F.life || {}).blocked || {})[key] || null;

// ── the bus: renderers per collector, isolated ────────────────────────────────
const HANDLERS = {};       // collector -> [fn]
const AFTER = [];          // hooks after every payload
function on(src, fn){ (HANDLERS[src] = HANDLERS[src] || []).push(fn); }
function afterApply(fn){ AFTER.push(fn); }
let lastMsgAt = 0, lastState = null, lastAges = {}, lastErrors = {}, offline = false;
const lastGood = {}, renderErr = {}, warned = {};
function warnOnce(name, msg){ if (warned[name] === msg) return; warned[name] = msg; console.warn(name, msg); }
// order matters a little: config, units and engine info feed the others
const ORDER = ['config', 'units', 'repo', 'engine_info', 'engine_fast', 'lifecycle', 'machine', 'gpu'];
function apply(state){
  lastState = state; lastMsgAt = Date.now();
  const serverNow = Math.max(0, ...Object.values(state).map(w => w && w.ts ? w.ts : 0));
  const errors = {}, ages = {};
  for (const [name, wrap] of Object.entries(state)){
    const bad = !!(wrap && wrap.data && wrap.data.error);
    if (bad) errors[name] = wrap.data.error;
    // a collector that keeps failing must not look fresh just because it keeps trying
    if (wrap && wrap.ts && !bad) lastGood[name] = wrap.ts;
    ages[name] = lastGood[name] != null ? Math.max(0, serverNow - lastGood[name]) : null;
  }
  const names = ORDER.filter(n => n in state).concat(Object.keys(state).filter(n => !ORDER.includes(n)));
  for (const name of names){
    const fns = HANDLERS[name]; if (!fns) continue;
    const wrap = state[name] || {};
    for (const fn of fns){
      try{
        if (errors[name]) throw new Error(errors[name]);
        fn(wrap.data || {});
        delete renderErr[name]; delete warned[name];
      }catch(e){
        if (!errors[name]) renderErr[name] = e.message;   // a bug here, not a dead source
        warnOnce(name, e.message);
        if (name === 'engine_info') try { (HANDLERS.engine_info_down || []).forEach(f => f()); } catch { /* keep going */ }
      }
    }
  }
  lastAges = ages; lastErrors = errors;
  freshness();
  AFTER.forEach(fn => { try { fn(state, errors); } catch(e){ warnOnce('after', e.message); } });
}
function freshness(){
  // age = how old the server said the sample was, PLUS how long we have had no payload
  const drift = lastMsgAt ? Math.max(0, (Date.now() - lastMsgAt) / 1000) : 0;
  const periods = (F.config && F.config.periods) || {};
  document.querySelectorAll('[data-src]').forEach(sec => {
    const srcs = sec.dataset.src.split(',');
    let worst = 0, stale = false, err = null;
    srcs.forEach(s => {
      const a = lastAges[s], p = periods[s] || 5;
      if (a != null){ const t = a + drift; worst = Math.max(worst, t); if (t > 3 * p + 3) stale = true; }
      if (renderErr[s]) err = err || `${s} render error: ${renderErr[s]}`;
      else if (lastErrors[s] && s !== 'engine_info') err = err || `${s}: ${lastErrors[s]}`;
    });
    const head = [...sec.children].find(c => c.localName === 'header');
    const age = head && head.querySelector('.age');
    if (age){ setText(age, err ? 'no data: ' + err.slice(0, 60) : stale ? `stale, ${fmtDur(worst)} old` : worst > 4 ? `${Math.round(worst)} s ago` : 'live'); }
    sec.classList.toggle('stale', stale || !!err);
  });
}

// ── the transport: SSE, polling behind it, an offline watch ───────────────────
let es = null, pollTimer = null, sseUp = false, lastSseTry = 0;
function login(){ location.href = '/login'; }
function setConn(onl, label){
  const l = $('conn-lamp'); if (l) l.className = 'lamp' + (onl ? ' ok live' : offline ? ' err' : ' warn');
  setText('conn-lbl', label);
}
function startPolling(){
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try{
      const r = await fetch('/api/state'); if (r.status === 401) return login();
      apply(await r.json()); setConn(true, 'polling');
      if (!sseUp && Date.now() - lastSseTry > 15000) connect();   // climb back to the live stream
    }catch{ setConn(false, 'offline'); }
  }, 2000);
}
function connect(){
  lastSseTry = Date.now();
  if (es){ try { es.close(); } catch { /* already gone */ } }   // never two streams at once
  // handlers hold their OWN stream: a late event from a replaced one must not flip the state
  const src = new EventSource('/api/stream');
  es = src;
  src.onopen = () => { if (es !== src) return; sseUp = true; setConn(true, 'live'); if (pollTimer){ clearInterval(pollTimer); pollTimer = null; } };
  src.onmessage = ev => { if (es !== src) return; try { apply(JSON.parse(ev.data)); setConn(true, 'live'); } catch(e){ console.warn('bad frame', e.message); } };
  src.onerror = () => {
    if (es !== src){ try { src.close(); } catch { /* gone */ } return; }
    sseUp = false; setConn(false, 'reconnecting'); src.close(); startPolling(); setTimeout(connect, 3000);
  };
}
function startTransport(){
  fetch('/api/state').then(r => { if (r.status === 401){ login(); return null; } return r.json(); })
    .then(s => { if (s){ apply(s); connect(); } })
    .catch(() => { setConn(false, 'offline'); startPolling(); });
  // offline watch, on the client's clock
  setInterval(() => {
    const was = offline; offline = !!lastMsgAt && Date.now() - lastMsgAt > 6000;
    if (offline) setConn(false, 'no data for ' + fmtDur((Date.now() - lastMsgAt) / 1000));
    if (lastState) freshness();
    if (was !== offline){ if (lastState) renderBanners(lastState, lastErrors); applyBusy(); }
  }, 1000);
}
async function csrf(){
  const t = await fetch('/api/csrf', {method: 'POST'});
  if (t.status === 401){ login(); throw new Error('signed out'); }
  return (await t.json()).token;
}
async function postJSON(path, body){
  const tok = await csrf();
  const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...body, csrf: tok})});
  if (r.status === 401){ login(); throw new Error('signed out'); }
  let out = {};
  try { out = await r.json(); } catch { /* an empty answer */ }
  return {status: r.status, ok: r.ok, out};
}

// ── router: views, the rail, the phone menu ───────────────────────────────────
const VIEWS = ['now', 'lanes', 'traffic', 'machine', 'agent', 'decide', 'image', 'video', 'library', 'logs', 'settings'];
// the old tab names still land: links in the docs and bookmarks say #agent, #engines
const ALIAS = {overview: 'now', engines: 'lanes', requests: 'traffic', systemone: 'decide', models: 'library', setup: 'settings'};
const ON_SHOW = {};
let activeView = 'now';
function onShow(view, fn){ (ON_SHOW[view] = ON_SHOW[view] || []).push(fn); }
function showView(name, push = true){
  name = ALIAS[name] || name;
  if (!VIEWS.includes(name)) name = 'now';
  const was = activeView; activeView = name; document.body.dataset.view = name;
  VIEWS.forEach(v => { const s = $('view-' + v); if (s) s.classList.toggle('active', v === name); });
  document.querySelectorAll('.rail .nav').forEach(b => {
    if (b.dataset.view === name) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  if (push && location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  setRailOpen(false);
  if (was !== name) window.scrollTo({top: 0});
  (ON_SHOW[name] || []).forEach(fn => { try { fn(); } catch(e){ warnOnce('show ' + name, e.message); } });
}
function setRailOpen(open){
  document.body.classList.toggle('railopen', open);
  const b = $('menubtn'); if (b) b.setAttribute('aria-expanded', String(open));
}
function setRailMin(min){
  document.body.classList.toggle('railmin', min);
  const b = $('railbtn'); if (b){ b.setAttribute('aria-expanded', String(!min)); setText(b.querySelector('span'), min ? 'Expand' : 'Collapse'); }
  try { localStorage.setItem('cockpit.rail', min ? 'min' : 'open'); } catch { /* storage may be unavailable */ }
}
function badge(view, txt, kind){ const b = $('bdg-' + view); if (b){ setText(b, txt || ''); b.className = 'bdg' + (kind ? ' ' + kind : ''); } }

// ── toasts ────────────────────────────────────────────────────────────────────
function toast(text, kind, ms = 4200){
  const box = $('toasts'); if (!box) return;
  const t = el('div', 'toast' + (kind ? ' ' + kind : ''), text);
  box.append(t);
  while (box.children.length > 3) box.firstChild.remove();
  setTimeout(() => t.remove(), ms);
}
async function copyText(txt){
  try { await navigator.clipboard.writeText(txt); toast('Copied.', 'ok', 1800); }
  catch { toast('The browser refused the clipboard; select the text instead.', 'warn'); }
}

// ── actions: one sheet, the exact command, the warnings, one job at a time ─────
// Same vocabulary as the server's event feed: an action reads as a sentence.
const ACTION_PHRASE = {
  unit: p => `${p.verb || 'act on'} ${laneLabel(String(p.unit || ''))}`,
  switch: p => `point the boot at ${TARGET_NAME[p.target] || p.target || 'a target'}`,
  flush_cache: () => 'flush the engine cache', abort_all: () => 'abort every generation in flight',
  smoke: () => 'run a smoke generation', diag_bundle: () => 'write a diagnostics bundle',
  fit_opencode: () => 'fit the opencode limits to this engine'};
function actionPhrase(action, params){
  const f = ACTION_PHRASE[action]; if (f) return f(params || {});
  const p = params || {}; return action + (Object.keys(p).length ? ' ' + Object.entries(p).map(([k, v]) => `${k} ${v}`).join(', ') : '');
}
const cap1 = s => String(s).charAt(0).toUpperCase() + String(s).slice(1);
// these three talk to the text engine: without one they can only fail
const NEEDS_ENGINE = new Set(['flush_cache', 'abort_all', 'smoke']);
function noEngineWhy(){
  if (textReady()) return '';
  if (imageServing()) return 'The image lane is serving: this talks to the text engine, which is not running.';
  if (videoServing()) return 'The video lane is serving: this talks to the text engine, which is not running.';
  return `No text engine is serving. Start one first (it answers in ${readyIn(enabledUnit())}).`;
}
function busyWhy(){
  if (offline) return 'The cockpit is unreachable: actions wait until the connection is back.';
  if (F.job && F.job.current) return `Another action is running (${actionPhrase(F.job.current.action, F.job.current.params)}). Wait for it to finish.`;
  return '';
}
const EXPLAIN = {
  unit: p => {
    const lane = laneLabel(p.unit);
    if (p.unit === AGENT_UNIT) return {stop: 'systemd stops opencode serve: the Agent view goes dark until the server is started again.',
      start: 'systemd starts opencode serve on loopback; the Agent view is back within seconds.',
      restart: 'systemd restarts opencode serve, which picks up an upgraded binary; the Agent view reconnects by itself.'}[p.verb] || '';
    if (p.unit === PROXY_UNIT) return p.verb === 'stop' ? 'systemd stops the keepalive proxy. The engine keeps running, but agent clients on :30001 lose their door until it is back.' : 'systemd starts the keepalive proxy on :30001.';
    if (p.verb === 'stop') return `systemd stops ${lane}. Its memory comes back at once; no other lane starts by itself.`;
    if (p.verb === 'restart') return `systemd restarts ${lane}; it answers again in ${readyIn(p.unit)}.`;
    return `systemd starts ${lane}: it loads its weights and answers in ${readyIn(p.unit)}. Follow the boot in the dock and on the Now view.`;
  },
  switch: p => (TARGET_NOTE[p.target] ? TARGET_NOTE[p.target] + '\n\n' : '') +
    'switch-model.sh verifies the checkpoint, makes this lane the one enabled at boot and, for a text target, rewrites its unit, the proxy ceiling and the opencode default model. It never restarts anything.',
  flush_cache: () => 'Empties the radix cache. Harmless; the engine refuses it while requests run.',
  abort_all: () => 'Every running or queued generation ends now; the clients see their stream end.',
  smoke: () => 'One real 200-token generation through the proxy, the way a client uses it.',
  diag_bundle: () => 'Collects logs, state and versions into a tarball in your home; the API key is masked everywhere.',
  fit_opencode: () => 'Reads the KV pool of the engine serving now and rewrites only opencode’s context and output limits, so a conversation can never outgrow it. A dated backup is written first. If the Agent server runs, it restarts to read them: a reply it is writing then is cut short.'};
const TITLE = {unit: p => `${cap1(p.verb)} ${laneLabel(p.unit)}`, switch: p => `Point the boot at ${TARGET_NAME[p.target] || p.target}`,
  flush_cache: () => 'Flush the engine cache', abort_all: () => 'Abort every generation in flight', smoke: () => 'Run a smoke generation',
  diag_bundle: () => 'Write a diagnostics bundle', fit_opencode: () => 'Fit the opencode limits'};
const VERB = {unit: p => cap1(p.verb) + ' ' + (LANE_NAME[p.unit] || laneLabel(p.unit)), switch: () => 'Point the boot', flush_cache: () => 'Flush',
  abort_all: () => 'Abort all', smoke: () => 'Run the probe', diag_bundle: () => 'Write the bundle', fit_opencode: () => 'Fit the limits'};
const argvFor = (name, p) => name === 'unit' ? ['sudo', '-n', '/usr/bin/systemctl', p.verb, p.unit]
  : name === 'switch' ? ['bash', 'switch-model.sh', p.target] : ['cockpit', name];

let SHEET = null;   // {mode: 'one'|'journey', ...}
function openSheet(){ $('scrim').hidden = false; setTimeout(() => $('sh-go').focus(), 0); }
function closeSheet(){ if (SHEET && SHEET.running) return; $('scrim').hidden = true; const s = SHEET; SHEET = null; if (s && s.onClose) s.onClose(); }
function askAction(name, params, warns, opts = {}){
  if (offline) return toast('The cockpit is unreachable right now: nothing can be started.', 'err');
  if (NEEDS_ENGINE.has(name) && !textReady()) return toast(noEngineWhy(), 'warn');
  if (F.job && F.job.current) return toast(busyWhy(), 'warn');
  if (!$('scrim').hidden) return;
  const p = params || {};
  setText('sh-title', (TITLE[name] ? TITLE[name](p) : name));
  setText('sh-lede', (EXPLAIN[name] ? EXPLAIN[name](p) : '') + (F.config.dry_run ? '\n\nDry run: nothing will really be executed.' : ''));
  const w = $('sh-warns'); clear(w); (warns || []).forEach(x => w.append(el('p', null, x))); w.hidden = !(warns && warns.length);
  $('sh-journey').hidden = true; show('sh-cmd-lbl', true); show('sh-cmd', true);
  setText('sh-cmd', argvFor(name, p).join(' '));
  setText('sh-status', ''); $('sh-status').className = 'status';
  const go = $('sh-go'); go.disabled = false; setText(go, VERB[name] ? VERB[name](p) : 'Run it');
  go.className = 'btn ' + (opts.danger || (name === 'unit' && p.verb !== 'start') || name === 'abort_all' ? 'danger solid' : 'primary');
  SHEET = {mode: 'one', name, params: p, onClose: opts.onClose};
  openSheet();
}
// Run one action; resolves {ok, status, out}. The sheet shows refusals where they happened.
async function runAction(name, params){
  const {status, out} = await postJSON('/api/action', {name, params});
  return {ok: status === 202, status, out};
}
async function sheetGo(){
  if (!SHEET || SHEET.running) return;
  if (SHEET.mode === 'journey') return runJourney();
  SHEET.running = true; const go = $('sh-go'); go.disabled = true; $('sh-cancel').disabled = true; setText('sh-status', 'starting…');
  const {name, params} = SHEET;
  try{
    const r = await runAction(name, params);
    if (r.ok){
      if (name === 'unit' && params.unit === IMAGE_UNIT && params.verb !== 'start') IMG_INTERRUPTED_AT.t = Date.now();
      if (name === 'unit' && params.unit === VIDEO_UNIT && params.verb !== 'start') VID_INTERRUPTED_AT.t = Date.now();
      SHEET.running = false; closeSheet(!0);
      toast(`${cap1(actionPhrase(name, params))}: started${r.out.dry_run ? ' (dry run)' : ''}. The dock follows it.`, 'ok');
      dockPinned = false; return;
    }
    if (r.status === 409 && r.out.reasons){ $('sh-status').className = 'status err'; setText('sh-status', 'Blocked: ' + r.out.reasons.join('; ')); }
    else if (r.status === 409){ SHEET.running = false; closeSheet(); toast(r.out.message || 'Another action is already running.', 'warn'); return; }
    else { $('sh-status').className = 'status err'; setText('sh-status', `Refused (${r.status}): ` + (r.out.error || JSON.stringify(r.out))); }
  }catch(e){ $('sh-status').className = 'status err'; setText('sh-status', 'The request failed: ' + e.message); }
  finally{ if (SHEET){ SHEET.running = false; } go.disabled = false; $('sh-cancel').disabled = false; }
}
const IMG_INTERRUPTED_AT = {t: 0}, VID_INTERRUPTED_AT = {t: 0};

// ── the lane journey: loading a lane is one path, each step its own job ───────
// The server knows three moves (switch, stop, start) and refuses them out of order; the
// page chains them, one job after the other, and says each command before it runs.
function laneJourneySteps(target){
  const unit = TARGET_UNIT(target);
  const steps = [];
  const serving = servingEngine();
  const enabled = enabledUnit();
  const curTarget = (engines()[unit] || {}).target;
  if (enabled !== unit || (curTarget && curTarget !== target) || (!curTarget && LANE_TARGETS[unit].length > 1 && target !== LANE_TARGETS[unit][0]))
    steps.push({name: 'switch', params: {target}, title: `Point the boot at ${TARGET_NAME[target]}`,
      desc: 'Verifies the checkpoint and makes this lane the one enabled at boot. Nothing restarts.'});
  if (serving && serving[0] !== unit)
    steps.push({name: 'unit', params: {verb: 'stop', unit: serving[0]}, title: `Stop ${laneLabel(serving[0])}`,
      desc: serving[0] === IMAGE_UNIT || serving[0] === VIDEO_UNIT ? 'A generation in flight is lost.' : 'Clients on :30001 see the engine unavailable until the next one answers.'});
  if (serving && serving[0] === unit && steps.length)
    steps.push({name: 'unit', params: {verb: 'restart', unit}, title: `Restart ${LANE_NAME[unit]} on the new checkpoint`, desc: `It answers again in ${readyIn(unit)}.`});
  else if (!serving || serving[0] !== unit)
    steps.push({name: 'unit', params: {verb: 'start', unit}, title: `Start ${LANE_NAME[unit]}`, desc: `It loads and answers in ${readyIn(unit)}.`});
  return steps;
}
function askJourney(target, opts = {}){
  if (offline) return toast('The cockpit is unreachable right now: nothing can be started.', 'err');
  if (F.job && F.job.current) return toast(busyWhy(), 'warn');
  if (!$('scrim').hidden) return;
  const unit = TARGET_UNIT(target);
  if (!installed(unit)) return toast(`${LANE_NAME[unit]} is not installed on this box: ${LANE_INSTALL[unit]}`, 'warn', 7000);
  const steps = laneJourneySteps(target);
  if (!steps.length) return toast(`${TARGET_NAME[target]} is already serving.`, 'ok');
  setText('sh-title', `Load ${TARGET_NAME[target]}`);
  setText('sh-lede', (TARGET_NOTE[target] || '') + `\n\nThe page runs these steps one after the other, each as its own job, and stops at the first that fails. Closing the page stops the chain after the step that is running.` + (F.config.dry_run ? '\n\nDry run: nothing will really be executed, the state on the box never moves, and a gate can refuse a later step of the walk-through because it still reads the box as it was.' : ''));
  const w = $('sh-warns'); clear(w);
  const serving = servingEngine();
  const warns = [];
  if (serving && serving[0] === UFLASH && TRANSITIONAL.has(serving[1].state)) warns.push('Flash is booting: stopping now throws that boot away, and the next start takes a whole boot again.');
  if (serving && serving[0] !== unit && (serving[0] === U27 || serving[0] === UFLASH)) warns.push('Agent clients on :30001 lose their engine until a text lane answers again.');
  if (serving && (serving[0] === IMAGE_UNIT || serving[0] === VIDEO_UNIT) && serving[0] !== unit) warns.push('A generation in flight on ' + laneLabel(serving[0]) + ' is lost.');
  warns.forEach(x => w.append(el('p', null, x))); w.hidden = !warns.length;
  const j = $('sh-journey'); clear(j); j.hidden = false;
  steps.forEach((s, i) => {
    const li = el('li'); li.dataset.i = i;
    const n = el('span', 'n', String(i + 1));
    const body = el('div'); body.append(el('div', 't', s.title), el('div', 'd', s.desc));
    const c = el('pre', 'cmd', argvFor(s.name, s.params).join(' ')); body.append(c);
    li.append(n, body); j.append(li);
  });
  show('sh-cmd-lbl', false); show('sh-cmd', false);
  setText('sh-status', `${steps.length} step${steps.length > 1 ? 's' : ''}, about ${fmtMin(bootSeconds(unit) + 15)} in all`);
  $('sh-status').className = 'status';
  const go = $('sh-go'); go.disabled = false; go.className = 'btn primary'; setText(go, `Load ${LANE_NAME[unit]}`);
  SHEET = {mode: 'journey', target, steps, idx: 0, onClose: opts.onClose};
  openSheet();
}
function jStep(i, cls){ const li = $('sh-journey').children[i]; if (li) li.className = cls; }
async function runJourney(){
  const S = SHEET; S.running = true;
  $('sh-go').disabled = true; $('sh-cancel').disabled = true;
  for (let i = S.idx; i < S.steps.length; i++){
    const st = S.steps[i]; jStep(i, 'run');
    setText('sh-status', `Step ${i + 1} of ${S.steps.length}: ${st.title}…`);
    let r;
    try { r = await runAction(st.name, st.params); } catch(e){ r = {ok: false, status: 0, out: {error: e.message}}; }
    if (!r.ok){
      jStep(i, 'fail'); S.running = false; S.idx = i;
      $('sh-status').className = 'status err';
      const why = r.out.reasons ? r.out.reasons.join('; ') : r.out.error || r.out.message || 'HTTP ' + r.status;
      setText('sh-status', `Step ${i + 1} refused: ` + why + (F.config.dry_run && r.out.reasons ? ' (dry run: the steps above changed nothing on the box, so the gate still reads the state as it was)' : ''));
      $('sh-go').disabled = false; $('sh-cancel').disabled = false; setText($('sh-go'), 'Retry from here'); return;
    }
    const id = r.out.job || r.out.id;
    const res = await waitJob(id);
    if (!res.ok){
      jStep(i, 'fail'); S.running = false; S.idx = i;
      $('sh-status').className = 'status err'; setText('sh-status', `Step ${i + 1} ${res.why}. Its log is in the dock.`);
      $('sh-go').disabled = false; $('sh-cancel').disabled = false; setText($('sh-go'), 'Retry from here'); return;
    }
    jStep(i, 'done');
    if (st.name === 'unit' && st.params.verb !== 'start'){
      if (st.params.unit === IMAGE_UNIT) IMG_INTERRUPTED_AT.t = Date.now();
      if (st.params.unit === VIDEO_UNIT) VID_INTERRUPTED_AT.t = Date.now();
      // the next start is refused until the stopped lane reads down: wait for it.
      // A dry run never moves the engine state, so the wait would burn its whole
      // budget on every stop step and read as a stuck journey.
      if (!F.config.dry_run) await waitFor(() => { const e = engines()[st.params.unit]; return !e || UNIT_DOWN.has(e.state); }, 90000);
    }
  }
  S.running = false;
  setText('sh-status', 'Done. The lane is booting: the Now view and the dock follow it.');
  setTimeout(() => { if (SHEET === S) closeSheet(); showView('now'); }, 900);
}
// A job is done when the job collector shows it finished. Resolves {ok, why}.
function waitJob(id, timeoutMs = 20 * 60 * 1000){
  const t0 = Date.now();
  return new Promise(resolve => {
    const tick = () => {
      const j = F.job || {};
      const cur = j.current, found = (j.recent || []).find(x => x.id === id);
      if (found && (!cur || cur.id !== id)){
        return resolve(found.status === 'done' ? {ok: true} : {ok: false, why: `failed${found.rc != null ? ' with exit code ' + found.rc : ''}`});
      }
      if (Date.now() - t0 > timeoutMs) return resolve({ok: false, why: 'did not finish in time'});
      setTimeout(tick, 500);
    };
    tick();
  });
}
function waitFor(pred, timeoutMs){
  const t0 = Date.now();
  return new Promise(resolve => { const tick = () => { if (pred() || Date.now() - t0 > timeoutMs) return resolve(); setTimeout(tick, 500); }; tick(); });
}

// ── the dock: the job running now, from every view and every browser ─────────
let dockPinned = false, dockHidden = null, lastFinished;
const JOBLINES = {id: null, lines: [], final: null};
function describeJob(j){ return cap1(actionPhrase(j.action, j.params)) + (j.origin === 'autoheal' ? ', by the autoheal belt' : '') + (j.dry_run ? ' (dry run)' : ''); }
function renderJob(d){
  F.job = d;
  const dock = $('dock'), cur = d.current, recent = (d.recent || [])[0];
  const now = Date.now() / 1000;
  let showIt = false;
  if (cur){
    showIt = dockHidden !== cur.id;
    dock.className = 'dock'; $('dock-lamp').className = 'lamp warn live';
    setText('dock-what', describeJob(cur)); setText('dock-time', fmtDur(cur.elapsed));
    show('dock-bar', true);
    if (JOBLINES.id !== cur.id){ JOBLINES.id = cur.id; JOBLINES.lines = []; }
    if (cur.lines && cur.lines.length) JOBLINES.lines = cur.lines;
    setText('dock-last', JOBLINES.lines.length ? JOBLINES.lines[JOBLINES.lines.length - 1] : 'starting…');
    const pip = $('jobpip'); if (pip){ pip.hidden = false; setText('jobpip-what', describeJob(cur)); }
  } else {
    const pip = $('jobpip'); if (pip) pip.hidden = true;
    if (recent && (now - (recent.ended || recent.started) < 60 || dockPinned) && dockHidden !== recent.id){
      showIt = true;
      const ok = recent.status === 'done';
      dock.className = 'dock ' + (ok ? 'done' : 'failed'); $('dock-lamp').className = 'lamp ' + (ok ? 'ok' : 'err');
      setText('dock-what', describeJob(recent)); setText('dock-time', fmtDur(recent.elapsed)); show('dock-bar', false);
      const r = recent.result || {};
      setText('dock-last', r.reply ? `reply: ${r.reply}` : r.path ? `written: ${r.path}` : r.reason === 'busy' ? 'the engine refused: requests still running'
        : ok ? (recent.argv ? `finished, exit code ${recent.rc}` : 'finished') : `failed${recent.rc != null ? ` (exit code ${recent.rc})` : ''}: open the log`);
      if (JOBLINES.id !== recent.id){ JOBLINES.id = recent.id; JOBLINES.lines = []; }
      // its last lines are fetched once it ended: the running snapshot stops short of the end
      if (JOBLINES.final !== recent.id){ JOBLINES.final = recent.id; fetchJobLines(recent.id); }
    }
    if (recent){
      if (lastFinished === undefined) lastFinished = recent.id;   // first sight: adopt, do not act
      else if (lastFinished !== recent.id){ lastFinished = recent.id; (HANDLERS.job_finished || []).forEach(f => { try { f(recent); } catch { /* isolated */ } }); }
    }
  }
  dock.hidden = !showIt;
  setText('dock-pre', JOBLINES.lines.join('\n') || '(no output yet)');
  const pre = $('dock-pre'); if (!pre.hidden) pre.scrollTop = pre.scrollHeight;
  const say = cur ? `${describeJob(cur)}: running` : recent ? `${describeJob(recent)}: ${recent.status}` : '';
  say && sayOnce('job', say);
  applyBusy();
}
async function fetchJobLines(id){
  try{ const r = await fetch('/api/jobs/' + id); if (r.status === 401) return login(); if (!r.ok) return;
    const j = await r.json(); if (JOBLINES.id === id){ JOBLINES.lines = j.lines || []; setText('dock-pre', JOBLINES.lines.join('\n') || '(no output)'); } }
  catch { /* the dock keeps what it has */ }
}
// what a screen reader hears when a lane or a job changes state, once per change
const SAID = {};
function sayOnce(key, txt){ if (SAID[key] === txt) return; SAID[key] = txt; setText('sayer', txt); }

// Every control that runs an action is disabled, with its reason, while it cannot.
function applyBusy(){
  const why = busyWhy(), eng = noEngineWhy();
  document.querySelectorAll('[data-act]').forEach(b => {
    const need = NEEDS_ENGINE.has(b.dataset.act) ? eng : '';
    const own = b.dataset.blocked || '';
    b.disabled = !!(why || need || own);
    b.title = why || need || own || b.dataset.title || '';
  });
  (HANDLERS.busy || []).forEach(f => { try { f(why); } catch { /* isolated */ } });
}

// ── banners: what you did not go looking for ──────────────────────────────────
function wedgeNext(){
  const c = F.config || {};
  if (c.autoheal) return 'The autoheal belt restarts it' + (c.autoheal_grace_s ? ` after its ${fmtDur(c.autoheal_grace_s)} grace period` : '');
  return 'Nothing restarts it by itself (the autoheal belt is off; COCKPIT_AUTOHEAL=1 arms it): stop it and start it again from Lanes';
}
function renderBanners(state, errors){
  const box = $('banners'); if (!box) return;
  const want = [];
  const add = (kind, strong, text) => want.push([kind, strong, text]);
  if (offline) add('err', 'Connection lost.', `No data from the cockpit for ${fmtDur((Date.now() - lastMsgAt) / 1000)}: what is on screen is frozen and actions wait.`);
  if (F.config && F.config.dry_run) add('info', 'Dry run.', 'Every action is confirmed, logged and audited as usual, but nothing is executed. This instance exists for tests.');
  Object.entries(engines()).forEach(([n, e]) => {
    const name = LANE_NAME[n] || n;
    if (e.state === 'wedged') add('err', `${name} is wedged.`, `It answers health checks but generates nothing. ${wedgeNext()}. The Logs view has the scheduler forensics.`);
    if (e.state === 'failed' && e.restarting) add('err', `${name} keeps crashing.`, `It dies during startup and systemd relaunches it every 15 s${e.restarts ? ` (${e.restarts} so far)` : ''}. Stop it from Lanes to end the loop; its journal is in Logs.`);
    else if (e.state === 'failed' && e.result === 'timeout') add('warn', `${name} was killed while stopping.`, 'It did not exit within its stop timeout. Nothing broke while it served: start it again when you need it.');
    else if (e.state === 'failed') add('err', `${name} failed.`, 'systemd reports the unit failed. Read its journal in Logs, then start it again from Lanes.');
    if (e.state === 'degraded') add('warn', `${name} stopped answering.`, 'It was serving; health probes retry every 2 s. If it stays here, Logs says why.');
  });
  if (F.memFloor && F.memFloor.aborts && F.memFloor.last_abort && Date.now() / 1000 - F.memFloor.last_abort < 600)
    add('warn', 'The memory floor fired.', `Memory fell under ${F.memFloor.gib} GiB with requests running: every generation was aborted ${fmtDur(Date.now() / 1000 - F.memFloor.last_abort)} ago to keep the box out of a livelock.`);
  const upd = F.update || {};
  if (upd.behind) add('info', `Version ${upd.latest} is out; this box runs ${upd.installed}.`, `Update with: cd ${(F.config || {}).repo_dir || '~/dgx-spark-qwen38'} && git pull && ./install.sh. It keeps your target, context mode, port and cache.`);
  if ((upd.stale_code || []).length) add('warn', 'This cockpit runs older code than the files on disk.', `${upd.stale_code.join(', ')} changed under it. Restart it: sudo systemctl restart qwen38-dashboard.service`);
  const ocf = F.ocfit;
  if (ocf && !ocf.ok) add('warn', 'opencode asks for more than this engine can hold.', `${ocf.why}: opencode declares ${fmtN(ocf.asked)} tokens, and ${ocf.served} can serve ${fmtN(ocf.limit)}. A session would break when the proxy refuses the prompt. ` + (ocf.autofit === 'started' ? 'The cockpit is fitting the limits now.' : 'Settings has "Fit the limits to this engine".'));
  ((F.life || {}).orphans || []).forEach(o => add('warn', `${LANE_NAME[o.unit] || o.unit} is running outside systemd.`,
    `The container ${o.container} serves, but its unit is not running, so no other engine may start and a reboot will not bring it back. Start it from Lanes to replace it, or remove it: docker rm -f ${o.container}.`));
  if (errors.lifecycle) add('warn', 'Engine state unknown.', 'The lifecycle collector failed: ' + String(errors.lifecycle).slice(0, 120));
  // drawn only when it changed: a live region re-read twice a second is noise
  const sig = JSON.stringify(want.map(w => [w[0], w[1]]));
  if (box.dataset.sig === sig){
    [...box.children].forEach((b, i) => { const t = b.querySelector('.t'); if (t) setText(t, want[i][2]); });
    return;
  }
  box.dataset.sig = sig; clear(box);
  want.forEach(([kind, strong, text]) => {
    const b = el('div', 'banner ' + kind);
    b.append(el('span', 'lamp ' + (kind === 'err' ? 'err' : kind === 'warn' ? 'warn' : '')));
    const p = el('div'); p.append(el('b', null, strong + ' '), el('span', 't', text)); b.append(p); box.append(b);
  });
}
afterApply(renderBanners);

// ── sparklines: canvas, bounded series, device resolution ─────────────────────
const SERIES = {};
function push(name, v, max = 180){ (SERIES[name] = SERIES[name] || []).push(v); if (SERIES[name].length > max) SERIES[name].shift(); }
function drawSpark(canvas, name, color, yMax, opts = {}){
  if (!canvas || !canvas.getContext) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth || 600, h = canvas.clientHeight || 64;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)){ canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
  const c = canvas.getContext('2d'); if (!c) return;
  c.setTransform(dpr, 0, 0, dpr, 0, 0); c.clearRect(0, 0, w, h);
  const data = SERIES[name] || [];
  c.strokeStyle = 'rgba(255,255,255,.06)'; c.lineWidth = 1;
  [0.25, 0.5, 0.75].forEach(f => { c.beginPath(); c.moveTo(0, Math.round(h * f) + .5); c.lineTo(w, Math.round(h * f) + .5); c.stroke(); });
  if (data.length < 2){ c.fillStyle = cssVar('--fg-3'); c.font = '12px Archivo, system-ui'; c.textAlign = 'center'; c.fillText(opts.empty || 'collecting…', w / 2, h / 2 + 4); return; }
  const m = yMax || Math.max(...data, 1e-9);
  const at = (v, i) => [i * (w / (data.length - 1)), h - Math.min(v / m, 1) * (h - 8) - 4];
  const g = c.createLinearGradient(0, 0, 0, h); g.addColorStop(0, color); g.addColorStop(1, 'rgba(0,0,0,0)');
  c.beginPath(); data.forEach((v, i) => { const [x, y] = at(v, i); i ? c.lineTo(x, y) : c.moveTo(x, y); });
  c.lineTo(w, h); c.lineTo(0, h); c.closePath(); c.globalAlpha = .25; c.fillStyle = g; c.fill(); c.globalAlpha = 1;
  c.beginPath(); data.forEach((v, i) => { const [x, y] = at(v, i); i ? c.lineTo(x, y) : c.moveTo(x, y); });
  c.strokeStyle = color; c.lineWidth = 2; c.lineJoin = 'round'; c.stroke();
  const [lx, ly] = at(data[data.length - 1], data.length - 1);
  c.fillStyle = color; c.beginPath(); c.arc(lx - 2, ly, 3, 0, 6.284); c.fill();
}

// ── collectors every view shares ───────────────────────────────────────────────
on('config', d => {
  F.config = d; F.usable = d.usable_frac || F.usable;
  setText('ver', 'v' + (d.version || '?') + (d.dry_run ? ', dry run' : ''));
  show('drytag', !!d.dry_run);
});
on('units', d => { F.units = d.units || {}; });
on('lifecycle', d => { F.life = d; });
on('repo', d => { F.proxy = d.proxy || null; });
on('update', d => { F.update = d || {}; });
on('agent', d => { F.agent = d; });
on('containers', d => { F.containers = d.containers || {}; });
on('job', renderJob);
on('engine_info', d => {
  if (d.prompt_ceiling_tokens != null) F.ceiling = d.prompt_ceiling_tokens;
  F.target = d.served_target || null;
  const i = d.info || {};
  if (i.max_total_num_tokens) F.pool = i.max_total_num_tokens;
  if (i.context_length) F.window = i.context_length;
  if (i.max_running_requests) F.maxRun = i.max_running_requests;
});
on('engine_fast', d => { F.load = (d.load || [])[0] || {}; F.noEngine = !d.load; F.memFloor = d.mem_floor || F.memFloor; });
on('machine', d => { F.machine = d; });
on('gpu', d => { F.gpu = d; });
on('opencode', d => { F.ocfit = d.fit || null; });
const singleLimit = () => F.pool ? (F.ceiling > 0 ? Math.min(Math.round(F.pool * F.usable), F.ceiling) : Math.round(F.pool * F.usable)) : null;
// physical pool occupancy: the engine dedupes shared prefixes, so the requests' summed
// lengths ("logical") can read past the pool while the pool never overflows
const physTokens = l => (l.num_used_tokens != null ? l.num_used_tokens : l.num_tokens) || 0;

// ── wiring the shell ──────────────────────────────────────────────────────────
function wireShell(){
  document.querySelectorAll('.rail .nav').forEach(b => b.addEventListener('click', () => showView(b.dataset.view)));
  window.addEventListener('hashchange', () => showView(location.hash.slice(1) || 'now', false));
  $('menubtn').addEventListener('click', () => setRailOpen(!document.body.classList.contains('railopen')));
  document.addEventListener('click', e => { if (document.body.classList.contains('railopen') && !e.target.closest('.rail') && !e.target.closest('#menubtn')) setRailOpen(false); });
  try { setRailMin(localStorage.getItem('cockpit.rail') === 'min'); } catch { setRailMin(false); }
  $('railbtn').addEventListener('click', () => setRailMin(!document.body.classList.contains('railmin')));
  $('serving').addEventListener('click', () => showView('lanes'));
  $('jobpip').addEventListener('click', () => { dockHidden = null; dockPinned = true; $('dock').hidden = false; });
  // the sheet
  $('sh-cancel').addEventListener('click', () => closeSheet());
  $('sh-go').addEventListener('click', sheetGo);
  $('scrim').addEventListener('click', e => { if (e.target === $('scrim')) closeSheet(); });
  document.addEventListener('keydown', e => {
    if (!$('scrim').hidden){
      if (e.key === 'Escape') closeSheet();
      if (e.key === 'Tab'){   // focus stays inside the sheet
        const f = [...$('sheet').querySelectorAll('button:not([disabled])')];
        if (!f.length) return;
        const i = f.indexOf(document.activeElement);
        e.preventDefault(); f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
      }
      return;
    }
    if (!$('menu').hidden && e.key === 'Escape') closeMenu();
  });
  // the dock
  $('dock-log').addEventListener('click', () => {
    const pre = $('dock-pre'), open = pre.hidden; pre.hidden = !open; dockPinned = open;
    $('dock-log').setAttribute('aria-expanded', String(open)); setText($('dock-log'), open ? 'Hide log' : 'Log');
    if (open && JOBLINES.id) fetchJobLines(JOBLINES.id);
  });
  $('dock-close').addEventListener('click', () => {
    const j = F.job || {}; dockHidden = (j.current || (j.recent || [])[0] || {}).id || null; dockPinned = false; $('dock').hidden = true;
  });
  // the actions menu
  $('actbtn').addEventListener('click', openMenu);
  $('menu-close').addEventListener('click', closeMenu);
  $('menu').addEventListener('click', e => { if (e.target === $('menu')) closeMenu(); });
}
const MENU_ITEMS = [
  ['smoke', 'Run a smoke generation', 'One real generation through the proxy, the way a client uses it.'],
  ['flush_cache', 'Flush the engine cache', 'Empties the radix cache; harmless, refused while requests run.'],
  ['abort_all', 'Abort every generation', 'Every running or queued generation ends now.'],
  ['diag_bundle', 'Write a diagnostics bundle', 'Logs, state and versions in a tarball in your home, key masked.'],
  ['fit_opencode', 'Fit the opencode limits', 'Match opencode to the pool this boot got.']];
function openMenu(){
  const box = $('menu-list'); clear(box);
  MENU_ITEMS.forEach(([act, t, d]) => {
    const b = el('button', 'btn block'); b.style.cssText = 'justify-content:flex-start; flex-direction:column; align-items:flex-start; min-height:58px; padding:10px 14px; gap:2px';
    b.dataset.act = act; b.dataset.title = d;
    b.append(el('span', null, t)); const s = el('span', 'faint', d); s.style.cssText = 'font-weight:450; font-size:13px; white-space:normal; text-align:left'; b.append(s);
    b.addEventListener('click', () => { closeMenu(); askAction(act, {}, []); });
    box.append(b);
  });
  $('menu').hidden = false; applyBusy();
}
function closeMenu(){ $('menu').hidden = true; }
