"use strict";
/* Spark Cockpit UI. One state payload (SSE or polling) drives every panel through
   idempotent renderers that write text nodes only (never HTML built from data).
   Every panel knows how fresh its sources are; every action goes through one modal,
   one server-side job lock, and one job strip visible from every tab and browser. */
const $ = id => document.getElementById(id);
const GB = 1024 ** 3;
const fmtB = b => b == null || isNaN(b) ? '...' : (b / GB).toFixed(1) + ' GB';
const fmtN = n => n == null || isNaN(n) ? '...' : Number(n).toLocaleString('en');
const fmtK = n => n == null ? '?' : Math.round(n / 1000) + 'K';
// a time of day is ambiguous once the cockpit reloads events written on another day
const clockTime = ts => {
  const d = new Date(ts * 1000), now = new Date();
  const hm = d.toLocaleTimeString([], {hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'});
  if (d.toDateString() === now.toDateString()) return hm;
  return d.toLocaleDateString([], {day: '2-digit', month: '2-digit'}) + ' ' + hm.slice(0, 5);
};
const fmtDur = s => s == null ? '?' : s < 90 ? Math.round(s) + ' s'
  : s < 3600 ? Math.floor(s / 60) + ' min ' + String(Math.round(s % 60)).padStart(2, '0')
  : Math.floor(s / 3600) + ' h ' + String(Math.floor((s % 3600) / 60)).padStart(2, '0');
const setText = (id, txt) => { const e = $(id); if (e) { e.textContent = txt; e.classList.remove('skel'); } };
// Short in the column, complete on hover: a definition list stops reading like a
// definition list once a value wraps over four ragged right-aligned lines.
const setShort = (id, txt, full) => { const e = $(id); if (e){ e.textContent = txt; e.title = full || txt; e.classList.remove('skel'); } };
const note = (id, txt) => { const e = $(id); if (e) { e.textContent = txt || ''; e.hidden = !txt; e.classList.remove('skel'); } };
const setChip = (id, txt, cls) => { const e = $(id); if (e) { e.textContent = txt; e.className = 'chip ' + (cls || ''); } };
const el = (tag, cls, txt) => { const n = document.createElement(tag); if (cls) n.className = cls; if (txt != null) n.textContent = txt; return n; };
const clear = n => { while (n.firstChild) n.removeChild(n.firstChild); };
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

// ── vocabulary: one word per engine state, everywhere ─────────────────────────
const STATE_LABEL = {
  stopped: 'stopped', failed: 'failed', starting: 'starting', 'loading-weights': 'loading weights',
  'loading-draft': 'loading the draft head', 'allocating-kv': 'allocating the KV pool',
  'capturing-graphs': 'capturing CUDA graphs', 'warming-up': 'warming up', ready: 'ready',
  degraded: 'ready but not answering', stopping: 'stopping', wedged: 'wedged: no generation',
  orphan: 'running outside systemd'};
const STATE_CHIP = {ready: 'ok', degraded: 'warn', failed: 'err', stopped: '', stopping: 'warn', wedged: 'err', orphan: 'err'};
// The rail badge sits next to a nav label in 236 px: one short word, never a state sentence.
const STATE_BADGE = {failed: 'failed', degraded: 'degraded', wedged: 'wedged', stopping: 'stopping', orphan: 'orphan'};
// A unit that is not running, whatever its container does: its button offers Start, and for
// an orphan that is the recovery (ExecStartPre removes the stray container first).
const UNIT_DOWN = new Set(['stopped', 'failed', 'orphan']);
const TRANSITIONAL = new Set(['starting', 'loading-weights', 'loading-draft', 'allocating-kv', 'capturing-graphs', 'warming-up']);
const STAGE_LABEL = {'init': 'init', 'loading-weights': 'weights', 'loading-draft': 'draft', 'allocating-kv': 'KV', 'capturing-graphs': 'graphs', 'warming-up': 'warmup'};
const ALL_STAGES = Object.keys(STAGE_LABEL);
const LANE_NAME = {'qwen38-sglang.service': '27B', 'qwen38-flash.service': 'flash 176B',
                  'qwen38-image.service': 'Qwen-Image'};
// How long each lane takes to answer after a start, before the box has seen one of its
// boots. Measured on the reference box: the text lanes load, compile and capture graphs
// for about nine minutes; the image lane loads 31 GB and warms up in about 70 seconds.
// Once a lane has booted in front of the cockpit, its own median replaces these.
const READY_DEFAULT = {'qwen38-sglang.service': 540, 'qwen38-flash.service': 780,
                       'qwen38-image.service': 70};
function readyIn(unit){
  const e = ((F.life || {}).engines || {})[unit] || {};
  const b = (e.boots || []).slice().sort((a, c) => a - c);
  const s = b.length ? b[Math.floor(b.length / 2)] : (READY_DEFAULT[unit] || 540);
  return 'about ' + fmtDur(s);
}
const IMAGE_UNIT = 'qwen38-image.service';
// The image lane's live facts. Declared up here, not beside the Image tab's code: the
// lane pill and the action bar read them too, and a const read before its line has run
// is a ReferenceError that takes the whole page down with it.
const IMG_STATE = {port: 30020, host: '127.0.0.1', available: false, busy: false};
let imgInflight = null;    // when this page's own request started, or null
let IMG_RUN = null;        // the shape of that request: {w, h, steps, n, editing}
let IMG_RUNPROG = null;    // the run bar, held by reference (it moves into the frame)
// When a stop or a restart of the image lane was last accepted from this page. SGLang
// Diffusion cannot abort a request, so that is the only way a generation ends early, and
// the request it cut must read as cancelled, not as a lane that failed to answer.
let IMG_INTERRUPTED = 0;
// The most pixels one call may ask for, all its images together: the largest call measured
// on this box (one 2752x1536 image, 44.8 GB at its peak). The images of a call run as one
// batch, and on unified memory running out hangs the machine. cockpit.py refuses the same.
const IMG_MAX_PIXELS = 2752 * 1536;
function imgRunProg(){ return IMG_RUNPROG || (IMG_RUNPROG = $('imgrunprog')); }
const AGENT_UNIT = 'opencode-web.service';
// Both lanes have several targets sharing one unit, so the lane name alone ("27B",
// "flash") does not say which checkpoint is loaded. Every control that names a lane
// says the checkpoint too. The plain "flash" target maps to the empty string on
// purpose: it is that lane's default and the lane name already reads right.
const TARGET_SHORT = {stock: 'stock', uncensored: 'uncensored', fp8: 'FP8',
                      'uncensored-fp8': 'FP8 uncensored', flash: '',
                      'flash-uncensored': 'uncensored', 'flash-nvda': 'NVIDIA export',
                      image: '2.1'};
function laneLabel(unit){
  if (unit === AGENT_UNIT) return 'the opencode web server';
  const base = LANE_NAME[unit] || unit.replace('.service', '');
  // the unit file says what it is configured to serve, and it can be read while the
  // engine is still loading; the live engine only confirms it once it answers
  const cfg = ((F.life || {}).engines || {})[unit] || {};
  const serving = servingEngine();
  // F.target comes from the TEXT engine's /server_info and outlives it: a page left
  // open across a switch to images kept "stock" there and labelled the lane "Qwen-Image
  // stock". The image lane's target is always its unit's.
  const target = (serving && serving[0] === unit && unit !== IMAGE_UNIT && F.target) || cfg.target;
  const t = target && TARGET_SHORT[target] ? ' ' + TARGET_SHORT[target] : '';
  return base + t;
}
const laneCls = name => name.includes('flash') ? 'flash' : name === IMAGE_UNIT ? 'laneimg' : 'lane27';
const stateChipCls = st => (STATE_CHIP[st] ?? 'warn') + (st === 'stopping' || TRANSITIONAL.has(st) ? ' live' : '');

// ── tabs and the collapsible rail ─────────────────────────────────────────────
// declared here, not next to its loader: showTab() runs at parse time and reads it
const loaded = {recipes: false, systemone: false};
const TABS = ['overview', 'agent', 'engines', 'requests', 'machine', 'models', 'systemone', 'image', 'video', 'logs', 'setup'];
let activeTab = 'overview';
function showTab(name, push = true){
  if (!TABS.includes(name)) name = 'overview';
  activeTab = name; document.body.dataset.tab = name;
  TABS.forEach(t => { const p = $('tab-' + t); if (p) p.classList.toggle('active', t === name); });
  document.querySelectorAll('.rail .nav').forEach(b => {
    if (b.dataset.tab === name) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  if (push && location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  // the maximised frame covers every other tab: leaving the tab always shrinks it back
  if (name !== 'agent' && document.body.classList.contains('agentmax')) setAgentMax(false, false);
  if (name === 'models' && !loaded.recipes) loadRecipes();
  // the probe costs one refused request to the proxy, so it runs when the tab is
  // opened rather than on every state tick
  if (name === 'systemone' && !loaded.systemone){ loaded.systemone = true; s1Probe(); }
  // the lane can be started and stopped from elsewhere in this page, so unlike the
  // System One probe this one re-reads on every visit rather than once
  if (name === 'image' && typeof imgLane === 'function') imgLane();
  // at parse time the agent helpers below are not initialised yet; the first state
  // tick mounts the frame in that case, a later click mounts it at once
  if (name === 'agent' && document.readyState === 'complete'){ mountAgent(); applyAgentMax(); }
  revealNav(name);
}
// On a narrow screen the rail is one horizontal scroller: the section you are on
// has to be inside it, or the current tab sits off-screen behind a swipe.
function revealNav(name){
  const b = document.querySelector(`.rail .nav[data-tab="${name}"]`);
  const rail = b && b.parentElement;
  if (!rail || rail.scrollWidth <= rail.clientWidth + 1) return;   // not scrolling: nothing to reveal
  const left = b.offsetLeft - (rail.clientWidth - b.offsetWidth) / 2;
  const smooth = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  rail.scrollTo({left: Math.max(0, left), behavior: smooth ? 'smooth' : 'auto'});
}
document.querySelectorAll('.rail .nav').forEach(b => b.addEventListener('click', () => showTab(b.dataset.tab)));
window.addEventListener('hashchange', () => showTab(location.hash.slice(1) || 'overview', false));
showTab(location.hash.slice(1) || 'overview', false);
function setRail(min){
  document.body.classList.toggle('railmin', min);
  $('railbtn').setAttribute('aria-expanded', String(!min));
  try { localStorage.setItem('cockpit.rail', min ? 'min' : 'open'); } catch { /* storage may be unavailable */ }
}
try { setRail(localStorage.getItem('cockpit.rail') === 'min'); } catch { setRail(false); }
$('railbtn').addEventListener('click', () => setRail(!document.body.classList.contains('railmin')));

// The rail and the job strip hang off --top, so --top has to be the height the top bar
// actually has. It normally stays one row (the CSS shrinks the pill, then the selector,
// then the lane button), but a narrow window or a page zoom can still push it to two:
// without this the second row is drawn above the viewport and the strip sits under it.
const topbar = document.querySelector('header.top');
if (topbar && window.ResizeObserver){
  new ResizeObserver(() => {
    const h = Math.round(topbar.getBoundingClientRect().height);
    if (h) document.documentElement.style.setProperty('--top', h + 'px');
  }).observe(topbar);
}

// The visual viewport is the part of the page a person can actually see. On iOS
// the software keyboard shrinks it and offsets it without touching the layout
// viewport, so a fullscreen frame sized in dvh would keep opencode's composer
// under the keyboard. --vvh and --vvtop follow it; the CSS falls back to 100dvh
// and 0 when the API is absent, which is every engine that never had the problem.
const vvp = window.visualViewport;
if (vvp){
  const syncVV = () => {
    const s = document.documentElement.style;
    s.setProperty('--vvh', Math.round(vvp.height) + 'px');
    s.setProperty('--vvtop', Math.round(vvp.offsetTop) + 'px');
  };
  vvp.addEventListener('resize', syncVV);
  vvp.addEventListener('scroll', syncVV);
  syncVV();
}

// ── sparklines (canvas, bounded series) ───────────────────────────────────────
const series = {};
function push(name, v, max = 120){ (series[name] = series[name] || []).push(v); if (series[name].length > max) series[name].shift(); }
function drawSpark(canvas, name, color, yMax){
  if (!canvas) return;
  // Draw at device resolution: a 600 px bitmap stretched over a 400 px box is the
  // difference between a chart and a smudge.
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const cssW = canvas.clientWidth || 600, cssH = canvas.clientHeight || 46;
  if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)){
    canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr);
  }
  const c = canvas.getContext('2d'), w = cssW, h = cssH;
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  const data = series[name] || [];
  c.clearRect(0, 0, w, h);
  // A midline, so a flat series reads as "steady at half" instead of as an empty box.
  c.strokeStyle = css('--line'); c.lineWidth = 1;
  c.beginPath(); c.moveTo(0, h / 2 + .5); c.lineTo(w, h / 2 + .5); c.stroke();
  if (data.length < 2) return;
  if (Math.max(...data) === 0){
    c.fillStyle = css('--mut'); c.font = '11px system-ui, sans-serif'; c.textAlign = 'center';
    c.fillText('nothing yet in this window', w / 2, h / 2 + 4);
    return;
  }
  const m = yMax || Math.max(...data, 1e-9);
  const at = (v, i) => [i * (w / (data.length - 1)), h - Math.min(v / m, 1) * (h - 10) - 6];
  c.beginPath();
  data.forEach((v, i) => { const [x, y] = at(v, i); i ? c.lineTo(x, y) : c.moveTo(x, y); });
  const line = new Path2D(); data.forEach((v, i) => { const [x, y] = at(v, i); i ? line.lineTo(x, y) : line.moveTo(x, y); });
  c.lineTo(w, h); c.lineTo(0, h); c.closePath();
  const g = c.createLinearGradient(0, 0, 0, h);
  g.addColorStop(0, color); g.addColorStop(1, 'transparent');
  c.globalAlpha = .22; c.fillStyle = g; c.fill(); c.globalAlpha = 1;
  c.strokeStyle = color; c.lineWidth = 2; c.lineJoin = 'round'; c.stroke(line);
  const [lx, ly] = at(data[data.length - 1], data.length - 1);   // where it is now
  c.fillStyle = color; c.beginPath(); c.arc(lx - 1.5, ly, 2.5, 0, 6.284); c.fill();
}

// ── shared facts between renderers (each guarded against missing data) ────────
const F = {pool: null, usable: 0.92, ceiling: 0, window: 262144, maxRun: null, load: {}, life: null,
           units: {}, containers: {}, proxy: null, config: {}, job: null, health: null};
const singleLimit = () => F.pool ? (F.ceiling > 0 ? Math.min(Math.round(F.pool * F.usable), F.ceiling) : Math.round(F.pool * F.usable)) : null;

// ── renderers ─────────────────────────────────────────────────────────────────
function rMachine(d){
  const m = d.mem || {};
  if (m.MemTotal){
    const used = m.MemTotal - m.MemAvailable, pct = 100 * used / m.MemTotal;
    setText('memlab', fmtB(used) + ' / ' + fmtB(m.MemTotal));
    $('memfill').style.width = pct.toFixed(1) + '%';
    // The gauge turns by the same rule as the chip: headroom in gibibytes, not a
    // percentage. A loaded lane sits past 90 % of the pool by design - the model is
    // simply resident - so the resting state is calm champagne, and only real
    // danger (a prefill eating the floor) lights bronze, then clay. An alarm that
    // rings while everything is fine teaches people to ignore the colour.
    const cls = m.MemAvailable < 4 * GB ? 'err' : m.MemAvailable < 9 * GB ? 'warn' : 'ok';
    $('memgauge').className = 'gauge' + (cls === 'err' ? ' crit' : cls === 'warn' ? ' warn' : '');
    setChip('memchip', fmtB(m.MemAvailable) + ' free', cls); setChip('memchip2', fmtB(m.MemAvailable) + ' free', cls);
    setText('memavail', fmtB(m.MemAvailable)); setText('memavail2', fmtB(m.MemAvailable)); setText('memused2', fmtB(used));
    setText('memcache', fmtB(m.Cached)); setText('swap', fmtB((m.SwapTotal || 0) - (m.SwapFree || 0)));
    push('mem', used / GB); drawSpark($('memspark'), 'mem', css('--acc'), m.MemTotal / GB);
    badge('machine', m.MemAvailable < 4 * GB ? fmtB(m.MemAvailable) : '', m.MemAvailable < 4 * GB ? 'err' : '');
  }
  const cpu = d.cpu_pct || {};
  setChip('cpuchip', (cpu.cpu ?? 0).toFixed(0) + ' %', (cpu.cpu || 0) > 85 ? 'warn' : 'ok');
  setText('loads', (d.load || []).map(x => x.toFixed(2)).join(' / '));
  setText('cores', String(Math.max(0, Object.keys(cpu).length - 1)));
  push('cpu', cpu.cpu || 0); drawSpark($('cpuspark'), 'cpu', css('--lane27'), 100);
  const dk = d.disks || {};
  setText('diskhome', dk.home ? fmtB(dk.home.free) + ' of ' + fmtB(dk.home.total) : 'n/a');
  setText('diskdocker', dk.docker ? fmtB(dk.docker.free) + ' of ' + fmtB(dk.docker.total) : 'n/a');
}
function rGpu(d){
  setText('gpupower', d.power_w != null ? d.power_w.toFixed(1) + ' W' : 'n/a');
  setText('gputemp', d.temp_c != null ? d.temp_c.toFixed(0) + ' °C' : 'n/a');
  setChip('gpuchip', (d.procs || []).length + ' proc', d.temp_c > 85 ? 'err' : d.temp_c > 75 ? 'warn' : 'ok');
  push('pow', d.power_w || 0); drawSpark($('powspark'), 'pow', css('--warn'));
  const tb = $('gpuprocs').tBodies[0]; clear(tb);
  (d.procs || []).slice(0, 6).forEach(p => {
    const tr = tb.insertRow(); tr.insertCell().textContent = p.name || '?'; tr.insertCell().textContent = p.pid;
    const c = tr.insertCell(); c.textContent = p.mem; c.className = 'r num';
  });
  if (!tb.rows.length){ const tr = tb.insertRow(); const c = tr.insertCell(); c.colSpan = 3; c.className = 'empty'; c.textContent = 'no process on the GPU'; }
}
function rEngineInfo(d){
  if (d.prompt_ceiling_tokens != null) F.ceiling = d.prompt_ceiling_tokens;
  // the selector shows what is serving, so "Switch" always reads as a change
  F.target = d.served_target || null;
  syncSelector();
  const i = d.info || {};
  showEngineFacts(true);
  setShort('engmodel', (i.model_path || '...').split('/').pop(), i.model_path || '');
  setText('engrev', (i.revision || '').slice(0, 12) || 'n/a');
  setText('engquant', i.quantization ?? 'n/a');
  setText('engctx', i.context_length ? fmtN(i.context_length) + ' tokens' : '...');
  setText('engpool', i.max_total_num_tokens ? fmtN(i.max_total_num_tokens) + ' tokens' : '...');
  setText('engspec', i.speculative_algorithm ? `${i.speculative_algorithm} ${i.speculative_num_steps}/${i.speculative_num_draft_tokens}` : 'none');
  setText('engattn', i.prefill_attention_backend ? `${i.prefill_attention_backend} / ${i.decode_attention_backend}` : (i.attention_backend ?? 'n/a'));
  setText('engradix', i.mamba_radix_cache_strategy ?? 'n/a');
  setShort('engver', String(i.version ?? 'n/a').split('+')[0], i.version ?? 'n/a');
  setShort('ovmodel', (i.model_path || '...').split('/').pop(), i.model_path || '');
  setText('ovrev', (i.revision || '').slice(0, 10) || 'n/a');
  setText('ovctx', i.context_length ? fmtN(i.context_length) : '...');
  setText('ovpool', i.max_total_num_tokens ? fmtN(i.max_total_num_tokens) : '...');
  setText('ovspec', i.speculative_algorithm ? `${i.speculative_algorithm} ${i.speculative_num_steps}/${i.speculative_num_draft_tokens}` : 'none');
  setText('ckceiling', F.ceiling > 0 ? fmtN(F.ceiling) + ' tokens (proxy)' : 'none: pool share only');
  if (i.max_total_num_tokens) F.pool = i.max_total_num_tokens;
  if (i.context_length) F.window = i.context_length;
  if (i.max_running_requests) F.maxRun = i.max_running_requests;
  rPool(); rReservoir();
}
const ENG_FIELDS = ['engmodel', 'engrev', 'engquant', 'engctx', 'engpool', 'engspec', 'engattn', 'engradix', 'engver'];
function rEngineInfoDown(reason){
  // No lane is serving, so the facts of the PREVIOUS one must not survive (30/08: flash
  // model and pool shown during the 27B boot). One line saying why, rather than the same
  // sentence copied down ten rows of a table that has no values to show.
  ENG_FIELDS.forEach(id => setText(id, '...'));
  ['ovmodel', 'ovrev', 'ovctx', 'ovpool', 'ovspec'].forEach(id => setText(id, 'no engine'));
  showEngineFacts(false, reason || 'no engine');
  // the served target is a fact about the text engine, and it is gone with it
  F.pool = null; F.maxRun = null; F.target = null; rPool(); rReservoir();
}
function showEngineFacts(on, reason){
  const kv = $('engkv'), down = $('engdown');
  if (!kv || !down) return;
  kv.hidden = !on; down.hidden = !!on;
  if (!on) down.textContent = reason + ': these facts arrive from the engine itself, so they are blank until it answers.';
}
function servingReady(){
  const s = servingEngine();
  return !!(s && ['ready', 'degraded', 'wedged'].includes(s[1].state));
}
// A TEXT engine is up. Not the same question once the image lane exists: the pool,
// the context window, Flush cache, Abort all and Smoke all talk to the text engine on
// :30000, and with the image lane serving that port is closed. Asking servingReady()
// there enabled three buttons that would hit a dead port and left the previous LLM's
// facts on screen under an image engine.
function textReady(){
  const s = servingEngine();
  return servingReady() && !!s && s[0] !== IMAGE_UNIT;
}
const imageServing = () => { const s = servingEngine(); return !!s && s[0] === IMAGE_UNIT; };
function rPool(){
  const l = F.load || {};
  if (!F.pool){ setText('poollab', 'waiting for the engine'); $('poolfill').style.width = '0%'; setText('poolnote', 'the pool size arrives with the engine (max_total_num_tokens at boot)'); return; }
  const held = l.num_tokens || 0, pct = 100 * held / F.pool;
  setText('poollab', fmtN(held) + ' / ' + fmtN(F.pool) + ' tokens');
  $('poolfill').style.width = Math.min(100, pct).toFixed(1) + '%';
  $('poolgauge').className = 'gauge' + (pct > 90 ? ' crit' : pct > 70 ? ' warn' : '');
  setText('poolnote', pct > 70
    ? 'one more large context will not fit: the scheduler queues it' + (F.maxRun ? ` (max-running-requests ${F.maxRun})` : '')
    : 'a single prompt tops out near ' + fmtK(singleLimit()) + ' tokens on this lane');
}
function rReservoir(){
  const l = F.load || {};
  if (!F.pool){
    setText('resbig', 'no pool'); $('restick').hidden = true;
    $('reslevel').style.width = '0%'; $('resghost').style.width = '0%';
    const lg = $('reslegend'); clear(lg); lg.append(el('span', null, 'the pool size arrives with the engine (max_total_num_tokens at boot)'));
    return;
  }
  const held = l.num_tokens || 0, single = singleLimit(), scale = Math.max(F.window, F.pool), pct = 100 * held / F.pool;
  const big = $('resbig'); clear(big); big.classList.remove('skel');
  big.append(fmtN(held)); big.append(el('small', null, `of ${fmtN(F.pool)} tokens`));
  $('rescapzone').style.width = (100 * F.pool / scale).toFixed(2) + '%';
  $('reslevel').style.width = (100 * Math.min(held, F.pool) / scale).toFixed(2) + '%';
  $('reslevel').className = 'level' + (pct > 70 ? ' hot' : '');
  $('restick').hidden = false; $('restick').style.left = (100 * single / scale).toFixed(2) + '%';
  $('resghost').style.width = (100 * Math.max(0, F.window - F.pool) / scale).toFixed(2) + '%';
  const lg = $('reslegend'); clear(lg);
  const span = (a, b, c) => { const s = el('span'); if (a) s.append(a); s.append(el('b', null, b)); s.append(c); return s; };
  lg.append(span('', pct.toFixed(0) + '%', ' held' + ((l.num_reqs || 0) ? ` by ${l.num_reqs} request${l.num_reqs > 1 ? 's' : ''}` : '')));
  lg.append(span('one prompt tops out near ', fmtK(single), ' (tick)'));
  lg.append(span('capacity ', fmtK(F.pool), ' at these memory settings'));
  lg.append(F.window > F.pool ? span('model window ', fmtK(F.window), ': the hatched zone never fits')
                              : span('model window ', fmtK(F.window), ': it fits in the pool'));
}
function rEngineFast(d){
  F.health = d.healthy;
  if (d.mem_floor){
    const f = d.mem_floor;
    const txt = `abort under ${f.gib} GiB` + (f.aborts ? ` · fired ${f.aborts} time${f.aborts > 1 ? 's' : ''}` : ' · quiet');
    setText('memfloor', txt); setText('memfloor2', txt);
    [$('memfloor'), $('memfloor2')].forEach(e => { if (e) e.style.color = f.aborts ? 'var(--warn)' : ''; });
    F.memFloor = f;
  }
  const noEngine = !d.load;
  const l = (d.load || [])[0] || {};
  F.load = l; rPool(); rReservoir(); rLanePill();
  setText('reqrun', noEngine ? 'no engine' : (l.num_reqs ?? '...'));
  setText('reqwait', noEngine ? 'no engine' : (l.num_waiting_reqs ?? '...'));
  setText('reqtok', noEngine ? 'no engine' : fmtN(l.num_tokens ?? 0));
  setChip('loadchip', noEngine ? 'no engine' : (l.num_reqs || 0) > 0 ? `${l.num_reqs} running` : 'idle',
          noEngine ? '' : (l.num_reqs || 0) > 0 ? 'flash live' : '');
  badge('requests', (l.num_reqs || 0) > 0 ? String(l.num_reqs) : '', '');
  if (noEngine){   // a flat line at zero reads as "quiet", not as "there is nothing here"
    series.req = [];
    const c = $('reqspark'); if (c) c.getContext('2d').clearRect(0, 0, c.width, c.height);
  } else {
    push('req', l.num_reqs || 0); drawSpark($('reqspark'), 'req', css('--acc'), 4);
  }
  const serving = servingEngine();
  setChip('engchip', serving ? STATE_LABEL[serving[1].state] || serving[1].state : 'no engine', serving ? stateChipCls(serving[1].state) : '');
}
function rDecode(d){
  const t = d.decode, u = d.usage || {};
  const none = !d.lane;   // no serving container at all
  if (none){
    ['acclen', 'acclen2', 'kvusage', 'mambausage'].forEach(i => setText(i, 'no engine'));
    return;
  }
  const acc = t ? t.accept_len.toFixed(2) + ' tokens per step' : 'idle';
  setText('acclen', acc); setText('acclen2', acc);
  setText('kvusage', t ? (100 * t.token_usage).toFixed(1) + ' %' : (u.tokens ? (100 * u.tokens).toFixed(0) + ' % (last seen)' : 'idle'));
  setText('mambausage', u.mamba ? (100 * u.mamba).toFixed(0) + ' %' + (u.mamba >= 0.5 ? ' (guard flushes when idle)' : '') : 'idle');
}
function rCanary(d){
  let txt, cls = '';
  if (d.skipped && d.last_ok == null) txt = 'not yet run';
  else if (d.fails > 0){ txt = `${d.fails} consecutive failure${d.fails > 1 ? 's' : ''}: ${d.last_err || ''}`; cls = 'var(--err)'; }
  else if (d.last_ok) txt = `ok, ${d.latency} s` + (d.skipped ? ' (skipped this round: engine busy)' : '');
  else txt = 'idle';
  const why = d.skipped && d.last_ok == null ? 'The probe waits for an engine that is ready and idle.' : txt;
  setShort('canary', txt, why); setShort('canary2', txt, why);
  [$('canary'), $('canary2')].forEach(e => { if (e) e.style.color = cls; });
  setText('canarylast', d.last_ok ? clockTime(d.last_ok) : 'never in this cockpit life');
}
function rKernel(d){
  const txt = d.nvrm_oom_1h ? `${d.nvrm_oom_1h} (last ${(d.nvrm_last || '').slice(11, 19)})` : 'none';
  setText('nvrm', txt); setText('nvrm2', txt);
  [$('nvrm'), $('nvrm2')].forEach(e => { if (e) e.style.color = d.nvrm_oom_1h ? 'var(--warn)' : ''; });
}
function rUnits(d){
  F.units = d.units || {};
  const sel = $('switchsel'); if (sel) markInstalled(sel);
  const box = $('unitlist'); clear(box);
  Object.entries(F.units).filter(([n]) => n.includes('keepalive')).forEach(([name, u]) => {
    const row = el('div', 'eng'); const top = el('div', 'row');
    const on = u.active === 'active';
    top.append(el('span', 'chip ' + (on ? 'ok' : u.active === 'failed' ? 'err' : ''), on ? 'running' : u.active));
    top.append(el('span', 'name', 'keepalive proxy :30001'));
    top.append(el('span', 'chip', u.enabled === 'enabled' ? 'starts at boot' : u.enabled));
    if (F.proxy && F.proxy.version) top.append(el('span', 'chip ' + (F.proxy.same_as_repo === false ? 'warn' : ''), F.proxy.version + (F.proxy.same_as_repo === false ? ' · not the repo copy' : '')));
    top.append(el('span', 'since', fmtSince(u.since)));
    const btn = el('button', 'btn mini' + (on ? ' danger' : ' low'), on ? 'stop' : 'start');
    btn.dataset.act = 'unit'; btn.dataset.unit = name; btn.dataset.verb = on ? 'stop' : 'start';
    btn.addEventListener('click', () => askAction('unit', {verb: on ? 'stop' : 'start', unit: name},
      ['sudo', '-n', '/usr/bin/systemctl', on ? 'stop' : 'start', name],
      on ? ['agent clients on :30001 lose the proxy until it is back (the engine itself keeps running)'] : []));
    top.append(btn); row.append(top);
    row.append(el('div', 'why', 'Fronts the engine for agent clients: keeps streams alive, refuses prompts the lane cannot serve, aborts orphan generations.'));
    box.append(row);
  });
  applyBusy();
}
function fmtSince(s){
  const m = (s || '').match(/(\d{4})-(\d{2})-(\d{2}) (\d{2}:\d{2})/); if (!m) return '';
  const now = new Date(), today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  return 'since ' + (today === `${m[1]}-${m[2]}-${m[3]}` ? m[4] : `${m[2]}-${m[3]} ${m[4]}`);
}
function rContainers(d){
  F.containers = d.containers || {};
  const serving = Object.entries(F.containers).find(([n, c]) => c.image);
  setText('engimage', serving ? serving[1].image : 'no serving container');
  setText('ovimage', serving ? serving[1].image : 'no serving container');
  const tb = $('ctable').tBodies[0]; clear(tb);
  Object.entries(F.containers).forEach(([n, c]) => {
    const tr = tb.insertRow();
    const c0 = tr.insertCell(); c0.textContent = n;
    if (c.image){ const im = el('div', 'num'); im.style.cssText = 'font-size:10.5px;color:var(--mut)'; im.textContent = c.image; c0.append(im); }
    const a = tr.insertCell(); a.textContent = c.cpu || ''; a.className = 'r num';
    const b = tr.insertCell(); b.textContent = c.mem || ''; b.className = 'r num';
  });
  if (!tb.rows.length){ const tr = tb.insertRow(); const c = tr.insertCell(); c.colSpan = 3; c.className = 'empty'; c.textContent = 'no serving container running'; }
}
function feedTime(ts){
  if (!ts || ts.length < 19) return ts || '';
  const n = new Date();
  const today = `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, '0')}-${String(n.getDate()).padStart(2, '0')}`;
  return ts.slice(0, 10) === today ? ts.slice(11, 19) : `${ts.slice(8, 10)}/${ts.slice(5, 7)} ${ts.slice(11, 16)}`;
}
function rFeed(d){
  const tb = $('feedtable').tBodies[0]; clear(tb);
  const rows = (d.rows || []).slice().reverse();
  rows.forEach(r => {
    const tr = tb.insertRow();
    tr.insertCell().textContent = feedTime(r.ts);
    const c1 = tr.insertCell(); c1.className = 'num';
    // Every client so far is this box talking to itself through the proxy; only the
    // port tells the connections apart. A remote client (an iPhone on the LAN) still
    // shows its full address. The full peer stays in the tooltip.
    c1.textContent = r.peer.startsWith('127.0.0.1:') ? ':' + r.peer.split(':').pop() : r.peer;
    c1.title = r.peer;
    tr.insertCell().textContent = r.path;
    const c2 = tr.insertCell(); c2.textContent = r.bytes >= 1024 ? (r.bytes / 1024).toFixed(0) + ' KB' : r.bytes + ' B'; c2.className = 'r num';
    const c3 = tr.insertCell(); c3.textContent = r.secs != null ? r.secs.toFixed(1) + ' s' : ''; c3.className = 'r num';
    // The kind comes from the server (lifecycle.outcome_kind), so the UI never
    // matches outcome strings itself: it used to, and it painted a client that
    // walked away the same red as a lane that failed.
    // An ok request is the resting state - dozens of them a minute - so it wears no
    // colour: graphite. Only bronze (client left) and clay (it broke) light the lamp.
    const cls = {ok: '', gone: 'warn', fail: 'err', live: 'flash live', unknown: ''}[r.kind] ?? 'err';
    const c4 = tr.insertCell(); c4.append(el('span', 'chip ' + cls, r.outcome));
    // The detail comes from the proxy's own words; the cell shows it compactly.
    if (r.detail){ const t = r.detail.replace(' tokens counted, fits (', ' tok fits ').replace(' usable)', '')
      .replace(' prompt tokens (counted by the engine), limit ', ' tok over limit ');
      const dv = el('div', 'num', t); dv.style.cssText = 'font-size:10px;color:var(--mut);margin-top:4px;letter-spacing:0'; c4.append(dv); }
  });
  const inflight = rows.filter(r => r.outcome === 'in flight').length;
  setChip('feedchip', inflight ? inflight + ' in flight' : rows.length ? 'idle' : 'no request yet', inflight ? 'flash live' : '');
  if (!rows.length){ const tr = tb.insertRow(); const c = tr.insertCell(); c.colSpan = 6; c.className = 'empty'; c.textContent = 'no request has gone through the proxy yet (agent clients use :30001)'; }
}
function rGuard(d){
  const g = d.guard || {}, z = d.zombies || {};
  setChip('zgchip', d.state === 'err' ? 'leaking' : d.state === 'warn' ? 'check' : 'holding', d.state);
  const verdict = (d.verdict || '').replace(/^./, c => c.toUpperCase());
  setText('zgverdict', `${verdict} (last ${d.window || '10m'}${d.lane ? ', ' + d.lane : ''})`);
  // The RUNNING proxy is the one that matters: the file in the repo says nothing
  // about the process systemd started.
  const v = g.version, old = g.predates_abort === true;   // compared server-side, part by part
  setText('zgversion', v ? 'v' + v : 'no banner');
  $('zgversion').style.color = old ? 'var(--warn)' : '';
  note('zgversionnote', old ? 'Below v6.14 a client that gives up during prefill leaves a generation the proxy cannot name.'
    : !v ? 'No startup banner in the journal, so the running version is unknown.' : '');
  setText('zgoverride', d.override === true ? 'yes' : d.override === false ? 'no' : d.lane ? 'unknown (docker did not answer)' : 'no engine');
  note('zgoverridenote', d.override === false
    ? 'Without SGLANG_ENABLE_REQUEST_HEADER_OVERRIDES the engine keeps its own request id, so an abandoned answer is read to its end instead of aborted. The next engine restart picks the flag up.' : '');
  const acted = [];
  if (g.aborted) acted.push(`${g.aborted} aborted`);
  if (g.drained) acted.push(`${g.drained} drained${g.drain_max_s != null ? `, longest ${g.drain_max_s.toFixed(0)} s` : ''}`);
  if (g.abort_failed) acted.push(`${g.abort_failed} abort unanswered`);
  if (g.ceiling) acted.push(`${g.ceiling} hit the drain ceiling`);
  setText('zgacted', acted.length ? acted.join(' \u00b7 ') : 'none');
  setText('zgflood', z.lines ? `${fmtN(z.lines)} from ${z.distinct}` : '0');
  $('zgflood').style.color = z.lines ? 'var(--err)' : '';
  const rows = z.requests || [];
  $('zgtablewrap').hidden = !rows.length;
  const tb = $('zgtable').tBodies[0]; clear(tb);
  rows.forEach(r => {
    const tr = tb.insertRow();
    const c0 = tr.insertCell(); c0.textContent = r.rid.slice(0, 12); c0.className = 'num';
    const c1 = tr.insertCell(); c1.textContent = fmtN(r.lines); c1.className = 'r num';
    const c2 = tr.insertCell(); c2.textContent = r.secs != null ? `${r.secs.toFixed(0)} s` : ''; c2.className = 'r num';
  });
}
function rOpencode(d){
  F.ocfit = d.fit || null;
  const fmtLim = l => l && l.context ? `${fmtN(l.context)} ctx / ${fmtN(l.output || 0)} out` : 'not declared';
  setText('oclim27', fmtLim(d.real.limits['qwen38/qwen3.8-27b'])); setText('oclimflash', fmtLim(d.real.limits['flashnext/qwen3.8-flash-next']));
  if (!d.enabled){
    setChip('occhip', 'off', '');
    setText('ocstate', 'off (installed with --no-opencode)' + (d.off_note ? ` · ${d.off_note}` : ''));
    setText('ocdefault', d.real.present ? (d.real.default || 'none') + ' (your own config, never touched)' : 'no opencode config on this box');
    setText('oclauncher', d.launcher.present ? (d.launcher.ours ? 'this repo’s oc is still there (stale, remove it)' : 'a foreign oc, not ours') : 'none');
    $('occmd').hidden = true;
    setText('ocnote', 'The switch leaves your opencode default model alone. Turn the integration back on with: ./install.sh --with-opencode');
    badge('setup', '', ''); return;
  }
  const ok = d.follows;
  setChip('occhip', ok === true ? 'follows the lane' : ok === false ? 'differs' : 'on', ok === true ? 'ok' : ok === false ? 'warn' : '');
  setText('ocstate', 'on · config ' + (d.real.present ? 'present' : 'missing (copy ~/.config/qwen38/opencode.json to ~/.config/opencode/)'));
  setText('ocdefault', (d.real.default || 'none') + ' · ' + d.why);
  $('ocdefault').style.color = ok === false ? 'var(--warn)' : '';
  setText('oclauncher', d.launcher.present
    ? (d.launcher.ours ? `oc, output cap ${d.launcher.cap ? fmtN(d.launcher.cap) : '?'}` : 'a foreign oc command, launcher not installed')
    : 'missing (re-run ./install.sh)');
  const f = d.fit;
  setShort('ocfit', !f ? 'unknown until an engine is serving' : f.ok ? 'fits this pool' : 'too large for this pool',
    !f ? 'The pool size arrives with the engine.'
       : f.ok ? `Prompt up to ${fmtN(f.context)} of ${fmtN(f.prompt_cap)}; worst case ${fmtN(f.worst)} of ${fmtN(f.usable)}.`
       : `${f.why}: ${fmtN(f.worst)} asked, ${fmtN(f.usable)} servable.`);
  const fe = $('ocfit'); if (fe) fe.style.color = f && !f.ok ? 'var(--warn)' : '';
  $('occmd').hidden = false;
  setText('ocnote', 'One command: the launcher lifts the output cap and auto-approves permissions. The default model follows every switch; existing sessions keep the model they started with.');
  badge('setup', ok === false ? 'opencode' : '', 'warn');
}
function rRepo(d){
  F.proxy = d.proxy || null;
  setText('repotag', d.tag || 'n/a'); setText('repobranch', d.branch || 'n/a');
  const head = d.head || '';
  setShort('repohead', head.split(' ')[0] || 'n/a', head || 'n/a');
  setText('repodirty', d.dirty == null ? 'unknown (git did not answer)'
    : d.dirty ? 'modified (uncommitted changes)'
    : d.untracked ? `clean (${d.untracked} untracked file${d.untracked > 1 ? 's' : ''})`
    : 'clean');
  setText('proxyver', F.proxy && F.proxy.version ? F.proxy.version + (F.proxy.same_as_repo === false ? ' · deployed file differs from the repo copy' : ' · repo copy') : 'unknown');
}
function rConfig(d){
  F.config = d; F.usable = d.usable_frac || F.usable;
  setText('ckver', d.version || '?'); $('verbadge').textContent = (d.version || '').includes('beta') ? 'BETA' : 'v' + (d.version || '');
  setShort('ckmode', d.dry_run ? 'dry run' : 'live',
           d.dry_run ? 'Every action is logged and audited, but nothing is executed.'
                     : 'Actions execute after your confirmation.');
  $('drybadge').hidden = !d.dry_run;
  setText('ckusable', Math.round(F.usable * 100) + ' % of the KV pool for one prompt');
  const per = d.periods || {}, pv = Object.values(per);
  setShort('ckperiods', pv.length ? `${pv.length} collectors, ${Math.min(...pv)} s to ${Math.max(...pv)} s` : '...',
           Object.entries(per).map(([k, v]) => `${k} ${v} s`).join(', '));
  setText('updatecmd', (d.terminal_only || {}).update_stack || 'cd <repo> && ./install.sh');
}

// ── lifecycle: engine cards (updated in place), lane pill, events, badges ─────
const CARDS = new Map();
function servingEngine(){
  const eng = (F.life && F.life.engines) || {};
  return Object.entries(eng).find(([n, e]) => !UNIT_DOWN.has(e.state) || e.restarting) || null;
}
function enabledUnit(){
  const e = Object.entries(F.units).find(([n, u]) => n !== 'qwen38-keepalive.service' && u.enabled === 'enabled');
  return e ? e[0] : 'qwen38-sglang.service';
}
function rLanePill(){
  const s = servingEngine();
  if (!s){
    setChip('lanestate', 'no engine', 'err'); setText('lanename', 'nothing serving');
    setText('lanesub', F.units && Object.keys(F.units).length ? `start ${LANE_NAME[enabledUnit()] || 'an engine'} from the actions` : '');
    return;
  }
  const [name, e] = s;
  setChip('lanestate', STATE_LABEL[e.state] || e.state, stateChipCls(e.state));
  setText('lanename', laneLabel(name));
  const l = F.load || {};
  let sub = '';
  if ((e.state === 'ready' || e.state === 'degraded') && name === IMAGE_UNIT){
    // no pool and no running count: the image lane serves one request at a time, and
    // what is worth a glance is whether it is working on one right now
    const busy = IMG_STATE.busy || imgInflight;
    sub = (busy ? 'generating' : 'idle, one image at a time') + (e.elapsed ? ` · up ${fmtDur(e.elapsed)}` : '');
  } else if (e.state === 'ready' || e.state === 'degraded'){
    sub = (F.pool ? `pool ${Math.round(100 * (l.num_tokens || 0) / F.pool)} %` : '') + ((l.num_reqs || 0) ? ` · ${l.num_reqs} running` : '') + (e.elapsed ? ` · up ${fmtDur(e.elapsed)}` : '');
  } else if (TRANSITIONAL.has(e.state)){
    const eta = e.eta && e.elapsed ? Math.max(0, e.eta - e.elapsed) : null;
    sub = `${fmtDur(e.elapsed)} elapsed` + (eta != null ? ` · about ${fmtDur(eta)} left` : ' · first boot, learning the duration');
  } else if (e.state === 'stopping'){
    sub = `${e.state_elapsed != null ? fmtDur(e.state_elapsed) : ''} elapsed`;
  }
  setText('lanesub', sub);
}
function bootBlock(e, unit){
  // Each engine names the stages it walks: the image lane has no draft, no KV pool
  // and no CUDA graphs, and drawing those as pending would say it is stuck on them.
  const stagesOf = e.stages && e.stages.length ? e.stages : ALL_STAGES;
  const doneN = (e.stage_done || []).length, stage = stagesOf[doneN] || 'init';
  // Before the cockpit has watched a boot of this lane, the duration measured on the
  // reference box stands in: "learning the duration" is true and tells nobody anything.
  const learned = !!e.eta;
  const etaTotal = e.eta || (unit ? READY_DEFAULT[unit] : null);
  const pct = etaTotal && e.elapsed ? Math.min(97, 100 * e.elapsed / etaTotal) : Math.min(95, 8 + doneN * (84 / stagesOf.length));
  const eta = etaTotal && e.elapsed ? Math.max(0, etaTotal - e.elapsed) : null;
  const boot = el('div', 'boot');
  const bar = el('div', 'bbar'); const fill = el('div', 'bfill'); fill.style.width = pct.toFixed(1) + '%'; bar.append(fill); boot.append(bar);
  const stages = el('div', 'stages');
  stagesOf.forEach((st, i) => stages.append(el('span', 'stage ' + (i < doneN ? 'done' : st === stage ? 'now' : ''), STAGE_LABEL[st])));
  boot.append(stages);
  const lab = el('div', 'blab');
  // what it is loading right now, when the engine says: "loading weights" for half a
  // minute says less than "the 13.3 GB DiT"
  const doing = e.detail && e.detail !== 'ready' ? ` (${e.detail})` : '';
  lab.append(el('span', null, `${STATE_LABEL[e.state] || stage}${doing} · ${fmtDur(e.elapsed)} elapsed`));
  lab.append(el('span', null, eta == null ? 'first boot: learning the duration'
    : learned ? `about ${fmtDur(eta)} left (median of ${(e.boots || []).length} boots)`
    : `about ${fmtDur(eta)} left (measured on the reference box; this box learns its own)`));
  boot.append(lab);
  if (e.rebuild) boot.append(el('div', 'why warn', 'the flash PLE table is being rebuilt: this boot takes about 12 minutes instead of 9'));
  if (e.overdue) boot.append(el('div', 'why warn', 'this boot is taking more than twice the usual time: check the Logs tab'));
  return boot;
}
function stoppingBlock(e){
  const boot = el('div', 'boot');
  const bar = el('div', 'bbar'); bar.append(el('div', 'bfill indet')); boot.append(bar);
  const lab = el('div', 'blab');
  lab.append(el('span', null, `stopping · ${e.state_elapsed != null ? fmtDur(e.state_elapsed) : '…'} elapsed`));
  lab.append(el('span', null, e.kind === 'image'
    ? 'systemd stops the process (SIGTERM; a generation in flight is cut after 5 s)'
    : 'systemd stops the container (SIGTERM, usually under 30 s)'));
  boot.append(lab); return boot;
}
function engineCard(name){
  let c = CARDS.get(name);
  if (c) return c;
  const root = el('div', 'eng'); const row = el('div', 'row');
  const chip = el('span', 'chip'); const nameEl = el('span', 'name', name.replace('.service', ''));
  const enabled = el('span', 'chip ' + laneCls(name)); const since = el('span', 'since');
  const btn = el('button', 'btn mini'); btn.dataset.act = 'unit'; btn.dataset.unit = name;
  row.append(chip, nameEl, enabled, since, btn); root.append(row);
  const why = el('div', 'why'); root.append(why);
  const hist = el('div', 'why'); root.append(hist);
  const extra = el('div'); root.append(extra);
  c = {root, chip, nameEl, enabled, since, btn, why, hist, extra, sig: ''};
  btn.addEventListener('click', () => {
    const e = ((F.life || {}).engines || {})[name]; if (!e) return;
    // a crash loop is failed between attempts, yet only a stop ends it
    const on = !UNIT_DOWN.has(e.state) || !!e.restarting;
    const warns = [];
    if (on && TRANSITIONAL.has(e.state) && name.includes('flash')) warns.push('this boot is thrown away: every flash boot writes its 47.7 GiB PLE table from scratch, so the next start takes the full 12 to 15 min again');
    if (on && e.state === 'ready') warns.push(name === IMAGE_UNIT
      ? 'an image being generated right now is lost, and the Image tab has nothing to talk to until this lane is back (' + readyIn(name) + ' after a start)'
      : 'clients on :30001 get "engine unavailable" until an engine is back (' + readyIn(name) + ' after a start)');
    askAction('unit', {verb: on ? 'stop' : 'start', unit: name}, ['sudo', '-n', '/usr/bin/systemctl', on ? 'stop' : 'start', name], warns);
  });
  CARDS.set(name, c); $('enginelist').append(root);
  return c;
}
function rLifecycle(d){
  F.life = d; syncSelector(); rLanePill(); renderLaneAction();
  if (typeof imgRenderLane === 'function') imgRenderLane();
  const g = d.pool_guard;
  if (g){
    F.poolGuard = g;
    setText('poolguard', !g.enabled ? 'off (COCKPIT_POOL_GUARD=0)'
      : g.fails ? `cannot flush since ${clockTime(g.last_fail)}: ${g.last_err}`
      : g.flushes ? `${g.flushes} flush${g.flushes > 1 ? 'es' : ''}, last ${clockTime(g.last)}, above ${Math.round(g.threshold * 100)} % held`
      : 'armed, never fired');
    const e = $('poolguard'); if (e) e.style.color = g.fails ? 'var(--warn)' : '';
  }
  const units = F.units || {};
  Object.entries(d.engines || {}).forEach(([name, e]) => {
    const c = engineCard(name);
    c.chip.textContent = STATE_LABEL[e.state] || e.state; c.chip.className = 'chip ' + stateChipCls(e.state);
    c.nameEl.textContent = `${name.replace('.service', '')} · ${laneLabel(name)}`;
    const en = (units[name] || {}).enabled;
    c.enabled.textContent = en === 'enabled' ? 'starts at boot' : en === 'disabled' ? 'manual start only' : en || '';
    c.since.textContent = e.state === 'ready' && e.elapsed ? 'up ' + fmtDur(e.elapsed) : '';
    const on = !UNIT_DOWN.has(e.state) || !!e.restarting;
    const blocked = !on && (d.blocked || {})[`unit:start:${name}`];
    c.btn.textContent = on ? (e.state === 'stopping' ? 'stopping…' : 'stop') : 'start';
    c.btn.className = 'btn mini ' + (on ? 'danger' : 'low');
    c.btn.dataset.verb = on ? 'stop' : 'start';
    c.btn.dataset.blocked = blocked ? blocked[0] : '';
    c.btn.disabled = e.state === 'stopping' || !!blocked;
    c.why.textContent = blocked ? 'start blocked: ' + blocked[0]
      : e.state === 'orphan' ? 'its container runs outside systemd: start replaces it with the unit\u2019s own'
      : e.state === 'failed' ? 'the unit failed: read its journal in the Logs tab, then start it again' : '';
    c.why.className = 'why' + (blocked || e.state === 'failed' || e.state === 'orphan' ? ' warn' : '');
    const boots = (e.boots || []).slice().reverse().map(fmtDur).join(', ');
    let hist = !TRANSITIONAL.has(e.state) && boots ? 'last boots: ' + boots + ((e.boots_rebuild || []).length ? ` (with table rebuild: ${e.boots_rebuild.slice().reverse().map(fmtDur).join(', ')})` : '') : '';
    // The KV pool this target won, boot after boot. It is a lottery and it sets
    // what the declared opencode limits can actually be served, so the spread is
    // the number to watch, not any single boot's.
    if (!TRANSITIONAL.has(e.state) && e.pools && e.pools.last){
      const p = e.pools;
      hist += (hist ? ' · ' : '') + 'KV pool ' + fmtK(p.last)
        + (p.n > 1 ? ` (${fmtK(p.min)}-${fmtK(p.max)}, ${p.spread_pct}% spread over ${p.n} boots)` : ' (first boot recorded)');
    }
    c.hist.textContent = hist;
    // the animated block is rebuilt only when its shape changes, its numbers every tick
    const sig = TRANSITIONAL.has(e.state) ? 'boot' : e.state === 'stopping' ? 'stop' : 'none';
    if (sig !== c.sig || sig !== 'none'){ clear(c.extra); if (sig === 'boot') c.extra.append(bootBlock(e, name)); if (sig === 'stop') c.extra.append(stoppingBlock(e)); c.sig = sig; }
  });
  // overview lane card: the serving engine's card, mirrored
  const ov = $('ovlane'); clear(ov);
  const s = servingEngine();
  // engine facts belong to a lane that is actually up: while it boots, stops or is gone,
  // say so rather than showing the previous lane's model, pool and percentages
  if (!textReady()) rEngineInfoDown(imageServing() ? 'the image lane is serving, so there is no text engine: its facts are in the Image tab'
                                    : s ? 'engine ' + (STATE_LABEL[s[1].state] || s[1].state) : 'no engine running');
  if (!s){
    const p = el('p', 'empty', `No engine is serving. Start ${laneLabel(enabledUnit())} from the action bar (${readyIn(enabledUnit())} to ready), or switch the target first.`);
    ov.append(p);
  } else {
    const [name, e] = s; const row = el('div', 'row');
    row.append(el('span', 'chip ' + stateChipCls(e.state), STATE_LABEL[e.state] || e.state), el('span', 'name', laneLabel(name)),
               el('span', 'chip ' + laneCls(name), name.replace('.service', '')), el('span', 'since', e.state === 'ready' && e.elapsed ? 'up ' + fmtDur(e.elapsed) : ''));
    ov.append(row);
    if (TRANSITIONAL.has(e.state)) ov.append(bootBlock(e, name));
    else if (e.state === 'stopping') ov.append(stoppingBlock(e));
    else if (e.state === 'wedged') ov.append(el('div', 'why warn', 'the engine answers health checks but generates nothing: the autoheal belt restarts it after its grace period (Engines tab, Logs tab for the forensics)'));
    else if (e.state === 'degraded') ov.append(el('div', 'why warn', 'the engine was serving and stopped answering: probes retry every 2 s'));
  }
  // events, twice (overview short, logs long)
  const evs = (d.events || []).slice().reverse();
  // Overview keeps the last few: it is the alarm, not the archive (the Logs tab holds 30).
  [[$('evtlist'), 7], [$('evtlist2'), 30]].forEach(([box, n]) => {
    if (!box) return; clear(box);
    if (!evs.length){ box.append(el('p', 'empty', 'no events yet in this cockpit session')); return; }
    evs.slice(0, n).forEach(ev => {
      const row = el('div', 'evt'); row.append(el('time', null, clockTime(ev.ts)), el('span', 'k', ev.kind), el('span', null, ev.msg)); box.append(row);
    });
  });
  // badges: what needs eyes
  const states = Object.values(d.engines || {}).map(e => e.state);
  const bad = states.find(st => st === 'wedged' || st === 'failed' || st === 'degraded' || st === 'orphan');
  const trans = states.find(st => TRANSITIONAL.has(st) || st === 'stopping');
  badge('engines', bad ? (STATE_BADGE[bad] || 'check') : trans ? 'booting' : '', bad ? 'err' : trans ? 'warn' : '');
  applyBusy();
}
// Which unit serves a target, and how that lane is installed when it is not.
const TARGET_UNIT = t => t === 'image' ? IMAGE_UNIT : t.startsWith('flash') ? 'qwen38-flash.service' : 'qwen38-sglang.service';
const LANE_INSTALL = {'qwen38-sglang.service': './install.sh', 'qwen38-flash.service': 'MODEL_CHOICE=flash ./install.sh',
                      'qwen38-image.service': './install.sh --with-image'};
// An option whose lane has no unit file on this box is shown as such and cannot be
// picked. Picking it used to be allowed, and the switch then failed a second later
// with "not installed": the image lane is opt-in, so that was the default on most boxes.
function markInstalled(sel){
  const units = F.units || {};
  if (!Object.keys(units).length) return;         // no facts yet: leave it as it is
  sel.querySelectorAll('option').forEach(o => {
    if (!o.dataset.label) o.dataset.label = o.textContent;
    const u = TARGET_UNIT(o.value);
    const missing = (units[u] || {}).enabled === '';
    o.disabled = missing;
    o.textContent = o.dataset.label + (missing ? '  (not installed)' : '');
    o.title = missing ? `this lane is not installed on this box: ${LANE_INSTALL[u]}` : '';
  });
}
function syncSelector(){
  // show the target of the lane that is serving, booting, or enabled: never just the
  // first option, which read as "stock" through nine minutes of an FP8 boot
  const sel = $('switchsel');
  if (sel) markInstalled(sel);
  if (!sel || sel.dataset.touched) return;
  const eng = (F.life || {}).engines || {};
  const s = servingEngine();
  const unit = s ? s[0] : enabledUnit();
  const target = (s && s[0] !== IMAGE_UNIT && F.target) || (eng[unit] || {}).target;
  if (target && sel.value !== target) sel.value = target;
}
function badge(tab, txt, cls){ const b = $('bdg-' + tab); if (b){ b.textContent = txt; b.className = 'bdg ' + (cls || ''); } }

// ── jobs: the strip everybody sees, the history, the busy lock in the UI ─────
const JOBLINES = {id: null, lines: []};
// Same vocabulary as the server's event feed: an action reads as a sentence, never
// as the parameter dict that happens to be its wire format.
const ACTION_PHRASE = {
  unit: p => `${p.verb || 'act on'} ${String(p.unit || '').replace('.service', '')}`,
  switch: p => `switch to ${p.target === 'image' ? 'Qwen-Image 2.1' : (TARGET_SHORT[p.target] || p.target || 'flash 176B')}`,
  flush_cache: () => 'flush the radix cache',
  abort_all: () => 'abort every generation in flight',
  smoke: () => 'smoke probe through the proxy',
  diag_bundle: () => 'write a diagnostics bundle',
  fit_opencode: () => 'fit the opencode limits to this engine'};
function actionPhrase(action, params){
  const f = ACTION_PHRASE[action];
  if (f) return f(params || {});
  const p = params || {};
  return action + (Object.keys(p).length ? ' ' + Object.entries(p).map(([k, v]) => `${k} ${v}`).join(', ') : '');
}

let stripPinned = false, lastFinished;
function rJob(d){
  F.job = d;
  const strip = $('jobstrip'), cur = d.current, recent = (d.recent || [])[0];
  const now = Date.now() / 1000;
  const describe = j => actionPhrase(j.action, j.params) + (j.origin === 'autoheal' ? ' (started by the autoheal belt)' : '') + (j.dry_run ? ' [dry run]' : '');
  if (cur){
    strip.hidden = false; strip.className = 'jobstrip running';
    setChip('jobchip', 'running', 'warn live');
    setText('jobwhat', describe(cur)); setText('jobelapsed', fmtDur(cur.elapsed)); $('jobbar').hidden = false;
    if (JOBLINES.id !== cur.id){ JOBLINES.id = cur.id; JOBLINES.lines = []; }
    if (cur.lines && cur.lines.length) JOBLINES.lines = cur.lines;
    setText('joblast', JOBLINES.lines.length ? JOBLINES.lines[JOBLINES.lines.length - 1] : 'starting…');
  } else if (recent && (now - (recent.ended || recent.started) < 90 || stripPinned)){
    strip.hidden = false; strip.className = 'jobstrip ' + (recent.status === 'done' ? 'done' : 'failed');
    setChip('jobchip', recent.status === 'done' ? 'done' : 'failed', recent.status === 'done' ? 'ok' : 'err');
    setText('jobwhat', describe(recent)); setText('jobelapsed', fmtDur(recent.elapsed)); $('jobbar').hidden = true;
    const r = recent.result || {};
    setText('joblast', r.reply ? `reply: ${r.reply}` : r.path ? `written: ${r.path}` : r.reason === 'busy' ? 'the engine refused: requests still running'
      : recent.status === 'done' ? (recent.argv ? `finished with exit code ${recent.rc}` : 'finished') : `failed${recent.rc != null ? ` (exit code ${recent.rc})` : ''}: open the log`);
    if (JOBLINES.id !== recent.id){ JOBLINES.id = recent.id; JOBLINES.lines = []; fetchJobLines(recent.id); }
    if (lastFinished === undefined) lastFinished = recent.id;        // first sight: adopt, do not act
    else if (lastFinished !== recent.id){
      lastFinished = recent.id;
      if (recent.action === 'switch' || recent.action === 'unit'){   // the lane may have changed
        loaded.recipes = false;
        if (activeTab === 'models') loadRecipes();
      }
    }
  } else if (!stripPinned){
    strip.hidden = true;
  }
  $('joblog').textContent = JOBLINES.lines.join('\n') || '(no output yet)';
  if (!$('joblog').hidden) $('joblog').scrollTop = $('joblog').scrollHeight;
  // history panel
  const hist = $('jobhist'); clear(hist);
  const all = (cur ? [cur] : []).concat(d.recent || []);
  if (!all.length) hist.append(el('p', 'empty', 'no job run since the cockpit started'));
  all.forEach(j => {
    const row = el('div', 'hist');
    row.append(el('span', 'chip ' + (j.status === 'running' ? 'warn live' : j.status === 'done' ? 'ok' : 'err'), j.status));
    row.append(el('span', null, describe(j)));
    row.append(el('span', 'm num', clockTime(j.started) + ' · ' + fmtDur(j.elapsed)));
    const b = el('button', 'btn mini ghost', 'log'); b.addEventListener('click', () => showJobLog(j.id)); row.append(b);
    hist.append(row);
  });
  applyBusy();
}
async function fetchJobLines(id){
  try{ const r = await fetch('/api/jobs/' + id); if (r.status === 401) return login(); if (!r.ok) return;
    const j = await r.json(); if (JOBLINES.id === id){ JOBLINES.lines = j.lines || []; $('joblog').textContent = JOBLINES.lines.join('\n') || '(no output)'; } }
  catch { /* the strip keeps what it has */ }
}
async function showJobLog(id){
  const v = $('jobhistlog'); v.hidden = false; v.textContent = 'loading…';
  try{ const r = await fetch('/api/jobs/' + id); if (r.status === 401) return login();
    if (r.status === 404){ v.textContent = 'this job is no longer in memory (the cockpit restarted); the audit log has its outcome'; return; }
    const j = await r.json(); v.textContent = `$ ${j.action} ${JSON.stringify(j.params || {})}\n` + (j.argv ? j.argv.join(' ') + '\n' : '') + (j.lines || []).join('\n') + `\n[${j.status}${j.rc != null ? ', exit code ' + j.rc : ''}, ${fmtDur(j.elapsed)}]`; }
  catch(e){ v.textContent = 'could not load the job: ' + e.message; }
}
$('joblogbtn').addEventListener('click', () => {
  const open = $('joblog').hidden; $('joblog').hidden = !open; stripPinned = open;
  $('joblogbtn').textContent = open ? 'hide log' : 'show log'; $('joblogbtn').setAttribute('aria-expanded', String(open));
  if (open && JOBLINES.id) fetchJobLines(JOBLINES.id);
});
let offline = false;
document.querySelectorAll('[data-act][title]').forEach(b => b.setAttribute('data-title', b.title));
// these three talk to the engine itself: without one they can only fail
const NEEDS_ENGINE = new Set(['flush_cache', 'abort_all', 'smoke']);
function applyBusy(){
  const busy = !!(F.job && F.job.current);
  const why = offline ? 'the cockpit is unreachable: actions are disabled until the connection is back'
            : busy ? `another action is running (${F.job.current.action}); wait for it to finish` : '';
  const engineUp = textReady();
  const noEngineWhy = engineUp ? '' : imageServing()
    ? 'the image lane is serving: this acts on the text engine, which is not running'
    : `no engine is serving: start one first (it answers in ${readyIn(enabledUnit())})`;
  document.querySelectorAll('[data-act]').forEach(b => {
    if (b.id === 'lanebtn') return;
    if (b.dataset.act === 'unit'){
      const blockedWhy = b.dataset.blocked || '';
      const stopping = b.textContent.startsWith('stopping');
      b.disabled = !!(why || blockedWhy || stopping); b.title = why || blockedWhy || '';
    } else {
      const need = NEEDS_ENGINE.has(b.dataset.act) ? noEngineWhy : '';
      b.disabled = !!(why || need);
      b.title = why || need || b.getAttribute('data-title') || '';
    }
  });
  const sw = $('switchsel'); if (sw){ const bl = ((F.life || {}).blocked || {}).switch; sw.disabled = !!why; const sb = document.querySelector('[data-act="switch"]'); if (sb){ sb.disabled = !!(why || bl); sb.title = why || (bl ? bl.join('; ') : 'Change the model the serving unit runs (never restarts anything by itself)'); } }
  renderLaneAction(why);
}
function renderLaneAction(why){
  const b = $('lanebtn'); if (!b) return;
  const s = servingEngine();
  if (s){
    const [name, e] = s; const stopping = e.state === 'stopping';
    // The lane pill sits right next to this button and names the checkpoint; what the
    // button has to name is the unit it stops, which is the lane. The full label stays
    // in the tooltip and in the confirmation.
    const lane = LANE_NAME[name] || name.replace('.service', '');
    b.textContent = stopping ? `${lane} stopping…` : `Stop ${lane}`;
    b.className = 'btn mini danger'; b.disabled = !!(why || stopping);
    b.title = why || `Stop ${laneLabel(name)} (systemctl stop ${name})`;
    b.onclick = () => CARDS.get(name) ? CARDS.get(name).btn.click() : null;
  } else {
    const name = enabledUnit(); const bl = ((F.life || {}).blocked || {})[`unit:start:${name}`];
    b.textContent = `Start ${LANE_NAME[name] || name.replace('.service', '')}`; b.className = 'btn mini low';
    b.disabled = !!(why || bl || !F.life);
    b.title = why || (bl ? bl[0] : `Start ${laneLabel(name)} (systemctl start ${name}, ${readyIn(name)} to ready)`);
    b.onclick = () => askAction('unit', {verb: 'start', unit: name}, ['sudo', '-n', '/usr/bin/systemctl', 'start', name], []);
  }
}

// ── agent tab: opencode's web interface framed from the relay ─────────────────
// The relay lives on this same host (cookies ignore ports), so the frame carries the
// cockpit session and opencode never asks for its own password. The frame is created
// the first time the tab opens and then kept: hiding a tab must not lose a session.
const AG = {mounted: false, wasReady: null};
// Whether the frame opens covering the window. An explicit choice wins on every
// device; with no choice stored, a hand-held viewport gets fullscreen, because
// there the head above the frame is a third of the screen and the tab IS the
// frame. Exiting is one tap on the corner chip, and that choice is remembered.
// dotted versions, numerically: "1.18.9" < "1.18.27"
function verCmp(a, b){
  const x = String(a).split('.').map(Number), y = String(b).split('.').map(Number);
  for (let i = 0; i < Math.max(x.length, y.length); i++){ const d = (x[i] || 0) - (y[i] || 0); if (d) return d; }
  return 0;
}
function agentMaxDefault(){
  let pref = null;
  try { pref = localStorage.getItem('cockpit.agent.max'); } catch { /* storage may be unavailable */ }
  if (pref === '1') return true;
  if (pref === '0') return false;
  return window.matchMedia('(max-width:980px)').matches;
}
function applyAgentMax(){
  if (!agentMaxDefault() || document.body.classList.contains('agentmax')) return;
  // Never cover the reason the panel is empty. A relay bound to another address,
  // a stopped server or a server still starting all have their explanation on
  // this page, and the frame has none: fullscreen would be a blank screen with a
  // chip in the corner. Opening it by hand still works in every one of those states.
  if (!agentReady() || !$('agnote').hidden || !$('agmsg').hidden) return;
  setAgentMax(true, false);
}
// Fullscreen: the frame covers the window; the corner button or Escape brings the cockpit
// back. Remembered, so a reload on #agent comes back the way it was left.
function setAgentMax(on, persist = true){
  document.body.classList.toggle('agentmax', on);
  const b = $('agmax'); if (b){ b.textContent = on ? 'Exit fullscreen' : 'Fullscreen'; b.setAttribute('aria-pressed', String(on)); }
  if (on){ mountAgent(); agexitPlace(); }   // the parked place only measures once the button can be seen
  if (persist){ try { localStorage.setItem('cockpit.agent.max', on ? '1' : '0'); } catch { /* storage may be unavailable */ } }
}
const agentUrl = () => F.agent && F.agent.relay && F.agent.relay.port ? `http://${location.hostname}:${F.agent.relay.port}/` : null;
const agentReady = () => !!(F.agent && F.agent.enabled && F.agent.relay && F.agent.relay.listening && F.agent.server && F.agent.server.healthy);
function agentMessage(text, cmd){
  $('agmsg').hidden = false; setText('agmsgtext', text);
  const c = $('agcmd'); c.hidden = !cmd; if (cmd) c.textContent = cmd;
}
function mountAgent(){
  if (!agentReady()) return;
  if (!AG.mounted){
    const f = document.createElement('iframe');
    f.id = 'agiframe'; f.title = 'opencode'; f.src = agentUrl();
    f.setAttribute('allow', 'clipboard-read; clipboard-write'); f.setAttribute('referrerpolicy', 'no-referrer');
    $('agframe').append(f); AG.mounted = true;
  }
  $('agmsg').hidden = true;
}
function reloadAgent(){ const f = $('agiframe'); if (f) f.src = agentUrl(); else mountAgent(); }
function rAgent(d){
  F.agent = d;
  const opt = $('logopt-agent'); if (opt) opt.hidden = !d.enabled;
  if (!d.enabled){
    setChip('agchip', 'not installed', '');
    setText('agline', 'one install on the box adds opencode here, behind this login');
    ['agopen', 'agreload', 'agrestart'].forEach(id => { $(id).hidden = true; });
    $('agnote').hidden = true;
    // No opencode at all is the first-time case: install.sh installs the pinned one and
    // then this tab. With opencode there, only the tab is missing.
    if (d.opencode_found === null)
      agentMessage(`opencode is not installed on this box, and this tab runs it. On the box, re-run the installer: `
                   + `it installs the opencode this repo tests${d.pinned ? ` (${d.pinned})` : ''}, then this tab.`,
                   'cd ~/dgx-spark-qwen38 && ./install.sh');
    else
      agentMessage('The Agent tab is not installed on this cockpit. opencode is on the box; to add the tab, run on the box:',
                   ((F.config || {}).terminal_only || {}).install_agent || 'dashboard/install-agent.sh');
    badge('agent', '', ''); return;
  }
  const r = d.relay || {}, sv = d.server || {}, u = d.unit || {};
  const ready = agentReady(), unitOn = u.active === 'active';
  const [chip, cls] = ready ? ['ready', 'ok'] : !r.listening ? ['relay waiting', 'warn']
                    : !unitOn ? [u.active === 'failed' ? 'server failed' : 'server stopped', 'err'] : ['server starting', 'warn live'];
  setChip('agchip', chip, cls);
  const parts = [];
  if (sv.version) parts.push('opencode ' + sv.version);
  if (r.listening) parts.push(`relay ${r.bind}:${r.port}`); else if (r.error) parts.push(r.error);
  if (d.binary && sv.version && d.binary !== sv.version) parts.push(`binary ${d.binary} installed, restart to serve it`);
  // install.sh pins the version the repo's opencode tuning was measured on; say when this one is another
  if (d.pinned && sv.version && sv.version !== d.pinned)
    parts.push(verCmp(sv.version, d.pinned) < 0 ? `this repo tests ${d.pinned}: ./install.sh brings it in line`
                                                : `newer than the ${d.pinned} this repo tests`);
  else if (unitOn && u.enabled) parts.push(u.enabled === 'enabled' ? 'starts at boot' : u.enabled);
  if (d.auto_live != null) parts.push(d.auto_live ? 'auto-approve: every tool call runs' : 'asks before risky tool calls');
  if (d.auto != null && d.auto_live != null && d.auto !== d.auto_live) parts.push('permission mode changed in the unit, restart to apply');
  if (!ready && unitOn && sv.error) parts.push(sv.error);
  setText('agline', parts.join(' · '));
  $('agopen').hidden = !ready; $('agreload').hidden = !ready; $('agmax').hidden = !ready; $('agrestart').hidden = !d.unit_installed;
  if (agentUrl()) $('agopen').href = agentUrl();
  // the relay answers on one address and the session cookie lives on the host the
  // browser typed: both have to be the same name for the frame to load
  const note = $('agnote');
  if (r.listening && r.bind && location.hostname !== r.bind){
    note.hidden = false;
    note.textContent = `You reached the cockpit as ${location.hostname}; the agent relay answers on ${r.bind} only. If the panel stays blank, open http://${r.bind}:${location.port || 80}/#agent instead.`;
  } else note.hidden = true;
  if (ready){
    if (activeTab === 'agent') mountAgent();
    if (AG.wasReady === false && AG.mounted) reloadAgent();   // the server came back: reconnect the panel
    if (activeTab === 'agent') applyAgentMax();   // a deep link to #agent lands here first
  } else {
    agentMessage(!r.listening ? `The relay is not listening yet: ${r.error || 'waiting for its address'}.`
               : !unitOn ? `The opencode server is ${u.active === 'failed' ? 'failed' : 'stopped'}. Start it from a terminal (sudo systemctl start ${AGENT_UNIT}) or read its journal in the Logs tab.`
               : `The opencode server is not answering yet${sv.error ? ` (${sv.error})` : ''}. The panel reconnects by itself.`, null);
  }
  AG.wasReady = ready;
  badge('agent', ready ? '' : 'down', 'err');
}
$('agreload').addEventListener('click', reloadAgent);
$('agmax').addEventListener('click', () => setAgentMax(!document.body.classList.contains('agentmax')));
// The back button floats and the person parks it where it covers nothing.
// The place is a fraction of the frame (not pixels): a rotation or a window
// resize puts it back on the same side, at the same height. A drag is never a
// click - releasing after a move must not close the frame.
const AGEXIT_KEY = 'cockpit.agent.exitpos';
const agexitFrame = () => $('agexit').closest('.agentframe');
function agexitPlace(){
  const b = $('agexit'), f = agexitFrame(); if (!f) return;
  let p = null; try { p = JSON.parse(localStorage.getItem(AGEXIT_KEY)); } catch { /* unparseable: rest at the default */ }
  if (!p || !(p.x >= 0 && p.x <= 1) || !(p.y >= 0 && p.y <= 1)){ b.style.left = b.style.top = b.style.right = b.style.transform = ''; return; }
  const fr = f.getBoundingClientRect(), w = b.offsetWidth || 140, h = b.offsetHeight || 32;
  b.style.transform = 'none'; b.style.right = 'auto';
  b.style.left = Math.max(6, Math.min(fr.width - w - 6, p.x * fr.width)) + 'px';
  b.style.top = Math.max(6, Math.min(fr.height - h - 6, p.y * fr.height)) + 'px';
}
window.addEventListener('resize', () => { if (document.body.classList.contains('agentmax')) agexitPlace(); });
let agexitStart = null, agexitMoved = false;
$('agexit').addEventListener('pointerdown', e => {
  const f = agexitFrame(); if (!f) return;
  if (e.pointerType === 'mouse' && e.button !== 0) return;
  const r = $('agexit').getBoundingClientRect(), fr = f.getBoundingClientRect();
  agexitStart = { x: e.clientX, y: e.clientY, ox: r.left - fr.left, oy: r.top - fr.top, w: r.width, h: r.height };
  agexitMoved = false;
  $('agexit').setPointerCapture(e.pointerId);
});
$('agexit').addEventListener('pointermove', e => {
  if (!agexitStart) return;
  const dx = e.clientX - agexitStart.x, dy = e.clientY - agexitStart.y;
  if (!agexitMoved && Math.hypot(dx, dy) < 6) return;
  agexitMoved = true;
  const b = $('agexit'), fr = agexitFrame().getBoundingClientRect();
  b.classList.add('dragging');
  b.style.transform = 'none'; b.style.right = 'auto';
  b.style.left = Math.max(6, Math.min(fr.width - agexitStart.w - 6, agexitStart.ox + dx)) + 'px';
  b.style.top = Math.max(6, Math.min(fr.height - agexitStart.h - 6, agexitStart.oy + dy)) + 'px';
});
function agexitEnd(){
  const b = $('agexit'); b.classList.remove('dragging');
  if (!agexitStart) return;
  agexitStart = null;
  if (!agexitMoved) return;
  const f = agexitFrame(), fr = f.getBoundingClientRect(), r = b.getBoundingClientRect();
  try { localStorage.setItem(AGEXIT_KEY, JSON.stringify({ x: (r.left - fr.left) / fr.width, y: (r.top - fr.top) / fr.height })); } catch { /* private mode: parked only for this page */ }
}
$('agexit').addEventListener('pointerup', agexitEnd);
$('agexit').addEventListener('pointercancel', agexitEnd);
$('agexit').addEventListener('click', () => {
  if (agexitMoved){ agexitMoved = false; return; }   // the finger moved the button, it did not press it
  setAgentMax(false);
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.body.classList.contains('agentmax') && $('modal').hidden) setAgentMax(false);
});
$('agrestart').addEventListener('click', () => askAction('unit', {verb: 'restart', unit: AGENT_UNIT},
  ['sudo', '-n', '/usr/bin/systemctl', 'restart', AGENT_UNIT],
  ['a generation in flight in the agent is lost; the panel reconnects when the server is back']));

// The update check has no panel of its own: it feeds the banner strip and one line of
// the Setup tab, so its renderer only has to put the fact where both can read it.
function rUpdate(d){
  F.update = d || {};
  const parts = [];
  if (d.installed) parts.push(d.installed);
  if (d.checked === false) parts.push('update check off (COCKPIT_UPDATE_CHECK=0)');
  else if (!d.latest) parts.push('latest release unknown (no network, or GitHub unreachable)');
  else if (d.behind) parts.push(`${d.latest} is out: git pull && ./install.sh`);
  else parts.push('up to date');
  setText('upd', parts.join(' \u00b7 '));
  const e = $('upd'); if (e) e.classList.toggle('warn', !!d.behind);
}

// ── apply: freshness, banners, isolation ──────────────────────────────────────
const RENDER = {machine: rMachine, gpu: rGpu, engine_info: rEngineInfo, engine_fast: rEngineFast, decode: rDecode,
                canary: rCanary, kernel: rKernel, units: rUnits, containers: rContainers, repo: rRepo,
                lifecycle: rLifecycle, feed: rFeed, reqguard: rGuard, opencode: rOpencode, config: rConfig, job: rJob, agent: rAgent, update: rUpdate};
document.querySelectorAll('dd, .chip, .num').forEach(e => { if (e.textContent.trim() === '...') e.classList.add('skel'); });
let lastMsgAt = 0, lastState = null, lastAges = {}, lastErrors = {};
const lastGood = {};   // per collector: the last sample that was NOT an error
const warned = {};     // one console line per distinct failure, not one per second
const renderErr = {};  // collector -> last renderer exception, shown on its panels
function warnOnce(name, msg){
  if (warned[name] === msg) return;
  warned[name] = msg; console.warn(name, msg);
}
// A table showing nothing but its own header reads as broken. Every table that can
// legitimately be empty says so in a row, in the words of what would fill it.
function emptyStates(){
  document.querySelectorAll('table[data-empty]').forEach(t => {
    const body = t.tBodies[0]; if (!body) return;
    const rows = [...body.rows];
    const placeholder = rows.length === 1 && rows[0].dataset.placeholder;
    if (rows.length && !placeholder) return;
    if (placeholder) return;
    const tr = body.insertRow();
    tr.dataset.placeholder = '1';
    const td = tr.insertCell();
    td.colSpan = t.tHead ? t.tHead.rows[0].cells.length : 1;
    td.className = 'empty'; td.textContent = t.dataset.empty;
  });
}

function apply(state){
  lastState = state; lastMsgAt = Date.now();
  const serverNow = Math.max(...Object.values(state).map(w => w && w.ts ? w.ts : 0));
  const errors = {}, ages = {};
  for (const [name, wrap] of Object.entries(state)){
    const bad = !!(wrap && wrap.data && wrap.data.error);
    if (bad) errors[name] = wrap.data.error;
    // a collector that keeps failing must not look fresh just because it keeps trying
    if (wrap && wrap.ts && !bad) lastGood[name] = wrap.ts;
    ages[name] = lastGood[name] != null ? Math.max(0, serverNow - lastGood[name]) : null;
  }
  // order matters a little: config, units and engine_info feed the others
  const order = ['config', 'units', 'repo', 'engine_info', 'engine_fast', 'lifecycle'];
  const names = order.filter(n => n in state).concat(Object.keys(state).filter(n => !order.includes(n)));
  for (const name of names){
    const fn = RENDER[name], wrap = state[name]; if (!fn) continue;
    try{
      if (errors[name]) throw new Error(errors[name]);
      fn(wrap.data || {});
      delete warned[name]; delete renderErr[name];
    }catch(e){
      if (!errors[name]) renderErr[name] = e.message;   // a bug here, not a dead source
      // a timeout on a busy engine is not a lane change: keep the last known facts and
      // let the panel age visibly. Only lifecycle decides that a lane is gone.
      if (name === 'engine_info' && !textReady()) rEngineInfoDown();
      warnOnce(name, e.message);
    }
  }
  emptyStates();
  lastAges = ages; lastErrors = errors;
  freshness();
  banners(state, errors);
  document.querySelectorAll('.skel').forEach(e => { if (e.textContent.trim() !== '...') e.classList.remove('skel'); });
}
function freshness(){
  // age = how old the server said the sample was, PLUS how long we have had no payload
  const drift = lastMsgAt ? Math.max(0, (Date.now() - lastMsgAt) / 1000) : 0;
  const periods = (F.config && F.config.periods) || {};
  document.querySelectorAll('section.panel[data-src]').forEach(sec => {
    const srcs = sec.dataset.src.split(',');
    let worst = 0, stale = false, err = null;
    srcs.forEach(s => { const a = lastAges[s]; const p = periods[s] || 5; if (a != null){ const t = a + drift; worst = Math.max(worst, t); if (t > 3 * p + 3) stale = true; }
      if (renderErr[s]) err = err || `${s} render error: ${renderErr[s]}`;
      else if (lastErrors[s] && s !== 'engine_info') err = err || `${s}: ${lastErrors[s]}`; });
    const ageEl = sec.querySelector('.age');
    if (ageEl){ ageEl.textContent = err ? 'no data: ' + err.slice(0, 70) : stale ? `stale, ${fmtDur(worst)} old` : worst > 4 ? `${Math.round(worst)} s ago` : ''; ageEl.className = 'age' + (stale || err ? ' stale' : ''); }
    sec.classList.toggle('stale', stale || !!err);
  });
}
function banners(state, errors){
  const box = $('banners'); clear(box);
  const add = (cls, strong, text) => { const b = el('div', 'banner ' + cls); if (strong) b.append(el('b', null, strong + ' ')); b.append(text); box.append(b); };
  if (offline) add('err', 'Connection lost.', `No data from the cockpit for ${fmtDur((Date.now() - lastMsgAt) / 1000)}: the values on screen are frozen and actions are disabled until it is back.`);
  if (F.config && F.config.dry_run) add('info', 'Dry run.', 'Every action is confirmed, logged and audited exactly as usual, but nothing is executed. This instance exists for tests.');
  const eng = (F.life && F.life.engines) || {};
  Object.entries(eng).forEach(([n, e]) => {
    if (e.state === 'wedged') add('err', `${LANE_NAME[n] || n} is wedged.`, 'It answers health checks but generates nothing. The autoheal belt restarts it after its grace period; the Logs tab has the scheduler forensics.');
    if (e.state === 'failed' && e.restarting) add('err', `${LANE_NAME[n] || n} keeps crashing.`,
      `It dies during startup and systemd relaunches it every 15 s (Restart=always${e.restarts ? `, ${e.restarts} relaunches so far` : ''}): the Logs tab has its journal. Stop it from the action bar to end the loop.`);
    else if (e.state === 'failed' && e.result === 'timeout') add('warn', `${LANE_NAME[n] || n} was killed while stopping.`,
      'It did not exit within its stop timeout, so systemd killed it and marks the unit failed. Nothing broke while it was serving: start it again from the action bar when you need it.');
    else if (e.state === 'failed') add('err', `${LANE_NAME[n] || n} failed.`, 'systemd reports the unit failed. Read its journal in the Logs tab, then start it again from the action bar.');
    if (e.state === 'degraded') add('warn', `${LANE_NAME[n] || n} stopped answering.`, 'It was serving; health probes retry every 2 s. If it stays here, the Logs tab tells why.');
  });
  if (F.memFloor && F.memFloor.aborts && F.memFloor.last_abort && Date.now() / 1000 - F.memFloor.last_abort < 600)
    add('warn', 'Memory floor fired.', `Host memory fell under ${F.memFloor.gib} GiB with requests running: every generation was aborted ${fmtDur(Date.now() / 1000 - F.memFloor.last_abort)} ago to keep the box out of a livelock.`);
  // An update nobody is told about is an update nobody installs. The Models tab has
  // carried this answer since v1.5, behind a button: this says it where the cockpit
  // already says what you did not go looking for.
  const upd = F.update || {};
  if (upd.behind) add('info', `Version ${upd.latest} is out; this box runs ${upd.installed}.`,
    'Update with: cd ~/dgx-spark-qwen38 && git pull && ./install.sh. It keeps your target, context mode, port and cache, and restarts the engine once. The release notes are on GitHub.');
  if ((upd.stale_code || []).length) add('warn', 'This cockpit is running older code than the files on disk.',
    `${upd.stale_code.join(', ')} changed under it, so its controls and its checks no longer agree. Restart it: sudo systemctl restart qwen38-dashboard.service`);
  const ocf = F.ocfit;
  // The limit is the usable share of the pool (the proxy keeps 8% back), which is a
  // number printed nowhere else on the page: said alone, "823,193" beside a panel reading
  // "KV pool 894,775" reads as the cockpit contradicting itself. So both are named.
  if (ocf && !ocf.ok) add('warn', 'opencode asks for more than this engine can hold.',
    `${ocf.why}: opencode declares ${fmtN(ocf.asked)} tokens, and ${ocf.served} can serve ${fmtN(ocf.limit)}`
    + (ocf.pool && ocf.limit === ocf.usable ? ` (the ${fmtN(ocf.pool)}-token pool this boot got, less the 8% the proxy keeps back)` : '')
    + `. The session would break mid-conversation when the proxy refuses the prompt. `
    + (ocf.autofit === 'started' ? 'The cockpit is fitting them to this boot\u2019s pool now.' : 'Setup tab, "Fit the limits to this engine".'));
  ((F.life || {}).orphans || []).forEach(o => add('warn',
    `${LANE_NAME[o.unit] || o.unit} is running outside systemd.`,
    `The container ${o.container} is serving${o.image ? ` from ${o.image}` : ''}, but its unit is not running, so no other engine may start and a reboot will not bring it back. Start ${LANE_NAME[o.unit] || o.unit} to replace it with the unit\u2019s own, or remove it from a terminal (docker rm -f ${o.container}).`));
  if (errors.lifecycle) add('warn', 'Engine state unknown.', 'The lifecycle collector failed: ' + errors.lifecycle.slice(0, 120));
}
// offline watch: client clock, one second
setInterval(() => {
  const was = offline; offline = lastMsgAt && Date.now() - lastMsgAt > 6000;
  if (offline) setConn(false, 'no data ' + fmtDur((Date.now() - lastMsgAt) / 1000));
  if (lastState) freshness();
  if (was !== offline){ if (lastState) banners(lastState, lastErrors); applyBusy(); }
}, 1000);

// ── transport: SSE with polling fallback ─────────────────────────────────────
let es = null, pollTimer = null, sseUp = false, lastSseTry = 0;
function setConn(on, label){ $('conndot').className = 'dot' + (on ? ' on' : offline ? '' : ' warn'); $('connlabel').textContent = label; }
function login(){ location.href = '/login'; }
function startPolling(){
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try{
      const r = await fetch('/api/state'); if (r.status === 401) return login();
      apply(await r.json()); setConn(true, 'polling');
      if (!sseUp && Date.now() - lastSseTry > 15000) connect();   // climb back to the live stream
    }
    catch { setConn(false, 'offline'); }
  }, 2000);
}
function connect(){
  lastSseTry = Date.now();
  if (es){ try { es.close(); } catch { /* already gone */ } }   // never two streams at once
  // handlers hold their OWN stream: a late event from a replaced EventSource must not
  // flip the connection state of the current one
  const src = new EventSource('/api/stream');
  es = src;
  src.onopen = () => { if (es !== src) return; sseUp = true; setConn(true, 'live'); if (pollTimer){ clearInterval(pollTimer); pollTimer = null; } };
  src.onmessage = ev => { if (es !== src) return; try { apply(JSON.parse(ev.data)); setConn(true, 'live'); } catch(e){ console.warn('bad frame', e.message); } };
  src.onerror = () => {
    if (es !== src) { try { src.close(); } catch { /* gone */ } return; }
    sseUp = false; setConn(false, 'reconnecting'); src.close(); startPolling(); setTimeout(connect, 3000);
  };
}
fetch('/api/state').then(r => { if (r.status === 401){ login(); return null; } return r.json(); })
  .then(s => { if (s){ apply(s); connect(); } })
  .catch(() => { setConn(false, 'offline'); startPolling(); });

// ── actions: one modal, exact command, warnings, one job at a time ───────────
let pending = null, inflight = false;
function toast(text, cls, ms = 4000){
  const t = $('toast'); t.textContent = text; t.className = 'toast ' + (cls || ''); t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, ms);
}
function askAction(name, params, argv, warns){
  if (offline){ toast('The cockpit is unreachable right now: nothing can be started.', 'err'); return; }
  if (NEEDS_ENGINE.has(name) && !textReady()){ toast(imageServing()
      ? 'The image lane is serving: this action talks to the text engine, which is not running.'
      : 'No engine is serving: start one first, then this action has something to talk to.', 'warn'); return; }
  if (F.job && F.job.current){ toast(`Another action is running (${F.job.current.action}). Wait for the job strip to finish.`, 'warn'); return; }
  if (!$('modal').hidden) return;
  const TARGET_NAME = {image: 'Qwen-Image 2.1 (images)',
                       stock: 'stock 27B (NVFP4)', uncensored: 'uncensored 27B (NVFP4)',
                       fp8: 'FP8 27B (Qwen official)', 'uncensored-fp8': 'FP8 27B abliterated',
                       flash: 'flash 176B (NVFP4)',
                       'flash-uncensored': 'flash 176B uncensored (NVFP4)',
                       'flash-nvda': 'flash 176B, NVIDIA export'};
  const TARGET_NOTE = {
    image: 'The image lane: text to image, editing with up to ten references, and native RGBA. '
         + 'It is a third lane with its own unit and its own venv, and like the other two it '
         + 'takes the box alone: 31 GB of weights do not fit beside a serving LLM. It answers in '
         + 'about 70 seconds after a start. The proxy on :30001 and opencode are text clients '
         + 'and are left exactly as they are.',
    fp8: 'Qwen\u2019s own FP8 release: the most faithful weights of this lane, and the heaviest. '
       + '30.9 GB against 21 GB for NVFP4, and SGLang takes that out of the KV pool: expect around '
       + '200,000 fewer tokens of context and a slower decode, because this box is bandwidth bound. '
       + 'The first switch downloads about 31 GB.',
    stock: 'The NVFP4 quantization this repo pins by default: smallest and fastest of the 27B targets.',
    uncensored: 'The abliterated NVFP4 checkpoint: same size and speed as stock, refusals removed.',
    'uncensored-fp8': 'The abliterated weights in Qwen\u2019s own FP8 format: the refusals of the FP8 target removed, '
       + 'at the same 30.9 GB and the same cost in pool and speed. The first switch downloads about 31 GB.',
    flash: 'The 176B Flash-Next lane. It has its own unit, its own image, and a 47.7 GiB N-gram '
       + 'table served from a file on the NVMe that the server rewrites on every boot.',
    'flash-uncensored': 'The abliterated build of the same 176B tree: 205 of its 206 shards are '
       + 'byte-identical in size to the stock export, so every serving flag is the same one. '
       + 'Refusals removed. The first switch downloads about 126 GB.',
    'flash-nvda': 'NVIDIA\u2019s own mixed-precision export of the same 176B model: NVFP4 experts, '
       + 'an FP8 N-gram table and FP8 block-scaled MTP experts. Served with no --quantization and '
       + 'a pinned MoE runner, which switch-model.sh handles. The first switch downloads about 124 GB.'};
  const TITLES = {unit: p => `${p.verb} ${laneLabel(p.unit)}`,
                  switch: p => `switch the target model to ${TARGET_NAME[p.target] || p.target}`,
                  flush_cache: () => 'flush the engine cache', abort_all: () => 'abort every in-flight generation', smoke: () => 'run a smoke generation through the proxy', diag_bundle: () => 'write a diagnostics bundle',
                  fit_opencode: () => 'fit the opencode limits to this engine'};
  const IMAGE_EXPLAIN = {
    start: `systemd starts the image lane: it loads 31 GB (the Qwen3-VL encoder, the DiT, the VAE) and answers in ${readyIn(IMAGE_UNIT)}. `
         + 'Like every lane it takes the box alone, so this is only offered once no other engine is running.',
    stop: 'systemd stops the image lane and the 31 GB come back at once. It does not bring a text lane back by itself: start the one you want.',
    restart: `systemd restarts the image lane; it reloads 31 GB and answers again in ${readyIn(IMAGE_UNIT)}.`};
  const AGENT_EXPLAIN = {stop: 'systemd stops opencode serve: the Agent tab goes dark until the server is started again.',
                         start: 'systemd starts opencode serve on loopback; the Agent tab is back within seconds.',
                         restart: 'systemd restarts opencode serve, which picks up an upgraded binary; the Agent tab reconnects by itself within seconds.'};
  const EXPLAIN = {unit: p => p.unit === AGENT_UNIT ? AGENT_EXPLAIN[p.verb] || ''
                     : p.unit === IMAGE_UNIT ? IMAGE_EXPLAIN[p.verb] || ''
                     : p.verb === 'stop' ? 'systemd stops the unit; the container gets SIGTERM and disappears in seconds.' : `systemd starts the unit; the engine loads its weights and is ready in ${readyIn(p.unit)} (watch the boot bar).`,
                   switch: p => (TARGET_NOTE[p.target] ? TARGET_NOTE[p.target] + '\n\n' : '')
                     + (p.target === 'image'
                        ? 'switch-model.sh verifies the checkpoint and makes the image lane the one unit enabled at boot. It never restarts anything: stop the serving lane, then start this one.'
                        : 'switch-model.sh rewrites the unit for the chosen target, updates the boot enablement, the proxy ceiling and the opencode default model. It never restarts anything: stop and start the engines afterwards.'),
                   flush_cache: () => 'Empties the radix cache. Harmless; refused by the engine if requests are running.',
                   abort_all: () => 'Every running or queued generation ends now; the clients see their stream end.',
                   smoke: () => 'One real 200-token generation through the proxy, the way a client uses it (up to a few minutes while a boot finishes).',
                   diag_bundle: () => 'Collects logs, state and versions into a tarball in your home; the API key is masked everywhere.',
                   fit_opencode: () => 'Reads the KV pool of the engine that is serving right now and rewrites only opencode\u2019s context and output limits so a conversation can never outgrow it. Your other opencode settings, providers and the default model are untouched, and a dated backup is written first. When the limits change and the Agent tab\u2019s opencode server is running, it is restarted so it reads them (it reads its config only at startup): a reply it is writing at that moment is cut short.'};
  $('mtitle').textContent = 'Confirm: ' + (TITLES[name] ? TITLES[name](params) : name);
  $('mwhat').textContent = (EXPLAIN[name] ? EXPLAIN[name](params) : '') + (F.config.dry_run ? '\nDry run: nothing will really be executed.' : '');
  const w = $('mwarn'); w.hidden = !(warns && warns.length); w.textContent = (warns || []).map(x => '⚠ ' + x).join('\n');
  $('margv').textContent = argv.join(' ');
  $('mstatus').textContent = ''; $('mgo').disabled = false;
  $('modal').hidden = false; pending = {name, params};
  setTimeout(() => $('mgo').focus(), 0);
}
function closeModal(){ $('modal').hidden = true; pending = null; }
$('mcancel').addEventListener('click', closeModal);
$('modal').addEventListener('click', e => { if (e.target === $('modal') && !inflight) closeModal(); });
document.addEventListener('keydown', e => {
  if ($('modal').hidden) return;
  if (e.key === 'Escape' && !inflight) closeModal();
  if (e.key === 'Tab'){ // focus trap: cancel <-> run
    const f = [$('mcancel'), $('mgo')]; const i = f.indexOf(document.activeElement);
    e.preventDefault(); f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
  }
});
$('mgo').addEventListener('click', async () => {
  if (!pending || inflight) return;
  inflight = true; $('mgo').disabled = true; $('mcancel').disabled = true; $('mstatus').textContent = 'starting…';
  const {name, params} = pending;
  try{
    const t = await fetch('/api/csrf', {method: 'POST'}); if (t.status === 401) return login();
    const tok = (await t.json()).token;
    const r = await fetch('/api/action', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({name, params, csrf: tok})});
    if (r.status === 401) return login();
    const out = await r.json();
    if (r.status === 202){
      if (name === 'unit' && params.unit === IMAGE_UNIT && params.verb !== 'start') IMG_INTERRUPTED = Date.now();
      closeModal(); toast(`${name} started` + (out.dry_run ? ' (dry run)' : '') + '. Follow it in the strip under the top bar.', 'ok'); stripPinned = false; return;
    }
    if (r.status === 409 && out.reasons){ $('mstatus').textContent = 'blocked: ' + out.reasons.join('; '); return; }
    if (r.status === 409){ closeModal(); toast(out.message || 'Another action is already running.', 'warn'); return; }
    $('mstatus').textContent = `refused (${r.status}): ` + (out.error || JSON.stringify(out));
  }catch(e){ $('mstatus').textContent = 'request failed: ' + e.message; }
  finally{ inflight = false; $('mgo').disabled = false; $('mcancel').disabled = false; }
});
$('switchsel').addEventListener('change', e => { e.target.dataset.touched = '1'; });
document.querySelectorAll('.actbar [data-act]').forEach(b => {
  const act = b.dataset.act; if (act === 'lane') return;
  b.addEventListener('click', () => {
    if (act === 'switch'){ const target = $('switchsel').value; askAction('switch', {target}, ['bash', 'switch-model.sh', target], ((F.life || {}).blocked || {}).switch || []); }
    else askAction(act, {}, ['cockpit', act], []);
  });
});
// The Setup tab's own button sits outside the action bar, and the loop above only binds
// the bar: it had no listener at all since 5d08667, while the banner sends people to it
// when the limits do not fit (found in review, 2026-09-24).
document.querySelectorAll('#tab-setup [data-act="fit_opencode"]').forEach(b =>
  b.addEventListener('click', () => askAction('fit_opencode', {}, ['cockpit', 'fit_opencode'], [])));

// ── on-demand loaders (buttons say what happened) ─────────────────────────────
function chip(text, cls){ return el('span', 'chip' + (cls ? ' ' + cls : ''), text); }
function cellChips(tr, items){ const td = tr.insertCell(); td.className = 'chips'; items.forEach(it => td.append(chip(it[0], it[1]))); return td; }
function fmtServe(sv){
  const parts = [];
  if (sv.context_length) parts.push('ctx ' + fmtN(sv.context_length));
  if (sv.mem_fraction != null) parts.push('mem ' + sv.mem_fraction);
  if (sv.max_running_requests) parts.push('run ' + sv.max_running_requests);
  if (sv.max_total_tokens) parts.push('pool ' + fmtN(sv.max_total_tokens));
  if (sv.chunked_prefill) parts.push('chunk ' + sv.chunked_prefill);
  const attn = sv.attention_backend || [sv.prefill_attention, sv.decode_attention].filter(Boolean).join('/');
  if (attn) parts.push(attn);
  return parts.join(' · ');
}
function recipeRow(tb, row){
  const r = row.recipe, tr = tb.insertRow(); const c0 = tr.insertCell();
  if (r){
    c0.append(chip(r.lane === 'flash' ? 'flash' : '27B', r.lane === 'flash' ? 'flash' : 'lane27'), ' ', el('strong', null, r.id));
    if (row.installed) c0.append(' ', chip('installed', 'ok'));
    if (!r.builtin) c0.append(' ', chip(row.file || 'custom'));
  } else c0.append(chip(row.file || '?', 'err'));
  if (row.errors && row.errors.length){ const td = tr.insertCell(); td.colSpan = 6; td.append(chip('invalid', 'err'), ' ', row.errors.join('; ')); return; }
  const c1 = tr.insertCell(); c1.textContent = r.engine.image; c1.className = 'num';
  const c2 = tr.insertCell(); c2.textContent = r.model.repo.split('/').pop() + ' '; c2.append(el('span', 'num', r.model.revision.slice(0, 10)));
  const d = r.drafter || {};
  tr.insertCell().textContent = d.algorithm === 'none' || !d.algorithm ? 'none' : d.algorithm + (d.repo ? ' ' + d.repo.split('/').pop() : ' (own head)') + (d.draft_tokens ? ' ×' + d.draft_tokens : '');
  tr.insertCell().textContent = fmtServe(r.serve || {});
  const p = row.presence || {};
  const cells = [['image', p.image], ['model', p.model], ['drafter', p.drafter]].map(([k, v]) =>
    [v === true ? k : v === false ? k + ' missing' : k + ' n/a', v === true ? '' : v === false ? 'err' : '']);
  if (p.downloading) cells[1] = ['model downloading', 'warn'];   // blobs still arriving
  cellChips(tr, cells);
  const cd = tr.insertCell();
  if (row.drift == null) cd.append(chip('lane not installed'));
  else if (!row.drift.length) cd.append(chip('matches installed', 'ok'));
  else {
    const det = el('details'); const sum = el('summary'); sum.style.cursor = 'pointer'; sum.append(chip(row.drift.length + ' differ', row.installed ? 'warn' : '')); det.append(sum);
    const ul = el('ul'); ul.style.cssText = 'margin:6px 0 0 14px;padding:0;font-size:11.5px';
    row.drift.forEach(x => ul.append(el('li', 'num', `${x.key}: recipe ${JSON.stringify(x.recipe)}, installed ${JSON.stringify(x.installed)}`)));
    det.append(ul); cd.append(det);
  }
}
async function loadRecipes(){
  $('rcpbtn').textContent = 'loading…'; $('rcpbtn').disabled = true;
  try{
    const r = await fetch('/api/recipes?refresh=1'); if (r.status === 401) return login();
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json(); loaded.recipes = true;
    const tb = $('rcptable').tBodies[0]; clear(tb);
    (d.builtin || []).forEach(row => recipeRow(tb, row)); (d.custom || []).forEach(row => recipeRow(tb, row));
    setText('rcpdir', d.custom_dir || '');
    const installed = (d.builtin || []).concat(d.custom || []).filter(x => x.installed).map(x => x.recipe.id);
    const drifting = (d.builtin || []).filter(x => x.installed && x.drift && x.drift.length).length;
    setText('rcpline', (installed.length ? 'installed: ' + installed.join(', ') : 'no lane matches a recipe') + (drifting ? `; ${drifting} installed lane differs from its recipe (open the drift)` : '') + ((d.custom || []).length ? `; ${d.custom.length} custom` : '; no custom recipe yet'));
    setText('rcpage', 'read ' + new Date().toLocaleTimeString()); badge('models', drifting ? `${drifting} drift` : '', 'warn');
    $('rcpbtn').textContent = 'reload';
  }catch(e){ setText('rcpline', 'could not load the recipes: ' + e.message); $('rcpbtn').textContent = 'retry'; }
  finally{ $('rcpbtn').disabled = false; }
}
$('rcpbtn').addEventListener('click', loadRecipes);
const fmtGiB = b => (b / 1024 ** 3).toFixed(1) + ' GiB';
$('regbtn').addEventListener('click', async () => {
  const b = $('regbtn'); b.textContent = 'scanning…'; b.disabled = true;
  try{
    const r = await fetch('/api/registry?refresh=1'); if (r.status === 401) return login(); if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const tb = $('regmodels').tBodies[0]; clear(tb);
    (d.models || []).forEach(m => (m.revisions || []).forEach((rv, i) => {
      const tr = tb.insertRow(); tr.insertCell().textContent = i ? '' : m.repo_id.split('/').pop();
      const c1 = tr.insertCell(); c1.textContent = rv.rev.slice(0, 10); c1.className = 'num';
      const c2 = tr.insertCell(); c2.textContent = fmtGiB(rv.bytes); c2.className = 'r num';
      const c3 = tr.insertCell(); c3.textContent = i ? '' : fmtGiB(m.disk_bytes); c3.className = 'r num';
      const c4 = tr.insertCell(); c4.append(chip(rv.status, rv.status === 'pinned' ? 'ok' : rv.status === 'stray' ? 'warn' : '')); if (rv.pin) c4.append(' ', chip(rv.pin));
    }));
    if (!tb.rows.length){ const tr = tb.insertRow(); const c = tr.insertCell(); c.colSpan = 5; c.className = 'empty'; c.textContent = 'no managed model in the Hugging Face cache'; }
    const ti = $('regimages').tBodies[0]; clear(ti);
    (d.images || []).forEach(im => { const tr = ti.insertRow(); tr.insertCell().textContent = im.ref; const a = tr.insertCell(); a.textContent = im.size; a.className = 'r num'; const c = tr.insertCell(); c.textContent = im.id; c.className = 'r num'; });
    const to = $('othertable').tBodies[0]; clear(to);
    const others = (d.other_models || []).slice().sort((a, b2) => b2.disk_bytes - a.disk_bytes);
    others.forEach(m => { const tr = to.insertRow(); tr.insertCell().textContent = m.repo_id; const c = tr.insertCell(); c.textContent = fmtGiB(m.disk_bytes); c.className = 'r num'; });
    if (!others.length){ const tr = to.insertRow(); const c = tr.insertCell(); c.colSpan = 2; c.className = 'empty'; c.textContent = 'none'; }
    setChip('otherchip', others.length ? fmtGiB(others.reduce((a, m) => a + m.disk_bytes, 0)) + ' in ' + others.length + ' models' : 'none', '');
    const strays = (d.models || []).flatMap(m => m.revisions).filter(rv => rv.status === 'stray');
    b.textContent = strays.length ? `rescan (${strays.length} stray revision${strays.length > 1 ? 's' : ''})` : 'rescan (clean)';
    setText('regage', 'scanned ' + new Date().toLocaleTimeString());
  }catch(e){ b.textContent = 'retry'; setText('regage', 'scan failed: ' + e.message); }
  finally{ b.disabled = false; }
});
$('upbtn').addEventListener('click', async () => {
  const b = $('upbtn'); b.textContent = 'checking…'; b.disabled = true;
  try{
    const r = await fetch('/api/upstream?refresh=1'); if (r.status === 401) return login(); if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const tb = $('uptable').tBodies[0]; clear(tb);
    (d.models || []).forEach(m => {
      const tr = tb.insertRow(); tr.insertCell().textContent = m.model.split('/').pop();
      const a1 = tr.insertCell(); a1.textContent = m.pin; a1.className = 'num';
      const a2 = tr.insertCell(); a2.textContent = m.upstream || '?'; a2.className = 'num';
      tr.insertCell().append(chip(m.status, m.status === 'same' ? 'ok' : m.status === 'moved' ? 'warn' : 'err'));
    });
    const rel = d.release || {};
    // A running cockpit serves html from disk against python it imported at
    // start, so a repo updated underneath it shows new controls the old action
    // layer refuses. Say it where the release line already lives.
    const stale = rel.stale_code || [];
    const relLine = rel.latest ? `repo release: local ${rel.local}, latest published ${rel.latest}` + (rel.latest === rel.local ? ' (up to date)' : ' (update available)') : 'release check offline (no network or GitHub unreachable)';
    setText('upline', stale.length
      ? `this cockpit is running code older than the repo (${stale.join(', ')} changed on disk): restart it with sudo systemctl restart qwen38-dashboard.service, or its controls and its checks disagree. ${relLine}`
      : relLine);
    const up = $('upline'); if (up) up.classList.toggle('warn', stale.length > 0);
    const moved = (d.models || []).filter(m => m.status === 'moved').length;
    b.textContent = moved ? `recheck (${moved} moved)` : 'recheck (all same)';
  }catch(e){ b.textContent = 'retry'; setText('upline', 'check failed: ' + e.message); }
  finally{ b.disabled = false; }
});
$('invbtn').addEventListener('click', async () => {
  const b = $('invbtn'); b.textContent = 'scanning…'; b.disabled = true;
  try{
    const r = await fetch('/api/inventory'); if (r.status === 401) return login(); if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json(); const tb = $('invtable').tBodies[0]; clear(tb);
    (d.items || []).forEach(i => { const tr = tb.insertRow(); tr.insertCell().append(chip(i.kind)); tr.insertCell().textContent = i.what; });
    if (!(d.items || []).length){ const tr = tb.insertRow(); const c = tr.insertCell(); c.colSpan = 2; c.className = 'empty'; c.textContent = 'nothing found (is the repo installed on this box?)'; }
    b.textContent = 'rescan'; setText('invline', `${(d.items || []).length} items, scanned ${new Date().toLocaleTimeString()}`);
  }catch(e){ b.textContent = 'retry'; setText('invline', 'scan failed: ' + e.message); }
  finally{ b.disabled = false; }
});
// live logs: manual tail, optional follow every 3 s while the Logs tab is visible
let followTimer = null;
async function tailLog(){
  const v = $('logview'), src = $('logsel').value;
  try{
    const r = await fetch('/api/logs/' + src); if (r.status === 401) return login(); if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json(); v.textContent = (d.lines || []).join('\n') || `(${src} has no output yet)`; v.scrollTop = v.scrollHeight;
  }catch(e){ v.textContent = 'could not read ' + src + ': ' + e.message; }
}
$('logbtn').addEventListener('click', () => { $('logview').textContent = 'loading…'; tailLog(); });
$('logfollow').addEventListener('change', () => {
  if (followTimer){ clearInterval(followTimer); followTimer = null; }
  if ($('logfollow').checked){ tailLog(); followTimer = setInterval(() => { if (activeTab === 'logs' && !document.hidden) tailLog(); }, 3000); }
});
// default log source: the container that is serving, unless the user already picked one
let logTouched = false;
$('logsel').addEventListener('change', () => { logTouched = true; });
setTimeout(() => {
  if (logTouched) return;
  const s = servingEngine(); const u = s ? s[0] : enabledUnit();
  $('logsel').value = u.includes('flash') ? 'qwen38-flash' : 'qwen38-sglang';
}, 3000);

// ── System One: the console ─────────────────────────────────────────────────
// Everything a typed decision can be, from a browser: the three question types, the
// answer drawn as the distribution it is, and the same call as a curl anyone can paste.
// The serving key never reaches this page; the cockpit holds it and forwards the call.
const S1_EXAMPLES = {
  'Support ticket': {
    state: 'Hi, I have been trying to connect my Stripe account for three days and it keeps failing.\nI am losing sales every hour. Please help as soon as you can.',
    questions: [
      {id: 'department', type: 'choice', instructions: 'Which team should handle this',
       criteria: [['billing', 'Payment or subscription issues'], ['technical', 'Bugs or integration problems'], ['sales', 'Pricing or account questions']]},
      {id: 'frustration', type: 'score', instructions: 'How frustrated the customer appears',
       levels: ['Calm, just stating facts', 'Frustrated but civil', 'Very angry, strong language']},
      {id: 'is_urgent', type: 'noul', instructions: 'The message conveys urgency or time-sensitivity'}
    ]},
  'Routing an agent': {
    state: 'User asked: "read the last 200 lines of the proxy log and tell me why the stream cut"',
    questions: [
      {id: 'tool', type: 'choice', instructions: 'Which tool this turn needs first',
       criteria: [['read_file', 'open a file on disk'], ['run_command', 'execute something and read its output'], ['answer', 'no tool needed, answer from what is known']]},
      {id: 'needs_root', type: 'noul', instructions: 'Carrying this out requires root'}
    ]},
  'Moderation': {
    state: 'Comment posted on the forum: "honestly this release is garbage and whoever shipped it should be fired"',
    questions: [
      {id: 'severity', type: 'score', instructions: 'How severe this is as a policy matter',
       levels: ['Harmless opinion', 'Rude but allowed', 'Personal attack', 'Requires removal']},
      {id: 'is_attack', type: 'noul', instructions: 'This targets a person rather than the work'}
    ]},
  'Extraction': {
    state: 'Invoice 2026-0417, dated 12 September 2026, from Maurienne AI SARL, total 4,820.00 EUR, payable within 30 days, marked OVERDUE.',
    questions: [
      {id: 'currency', type: 'choice', instructions: 'The currency of the total',
       criteria: [['EUR', 'euro'], ['USD', 'US dollar'], ['GBP', 'pound sterling'], ['other', 'anything else']]},
      {id: 'overdue', type: 'noul', instructions: 'The invoice is past its due date'}
    ]},
  'Twenty options': {
    state: 'The engine log ends with: "RuntimeError: selected index k out of range" inside the sampler, then the scheduler exits.',
    questions: [
      {id: 'cause', type: 'choice', instructions: 'The most likely cause',
       criteria: [['bad_request', 'a request field the engine does not bound'], ['oom', 'out of memory'], ['driver', 'a GPU driver fault'],
                  ['disk', 'a full disk'], ['network', 'a network failure'], ['config', 'a misconfiguration at start-up'],
                  ['model', 'a corrupt checkpoint'], ['upstream_bug', 'a known upstream defect']]}
    ]}
};
let s1Questions = [];

function s1Chip(id, txt, cls){ const e = $(id); if (e){ e.textContent = txt; e.className = 'chip ' + (cls || ''); } }

function s1RenderQuestions(){
  const box = $('s1questions'); if (!box) return;
  box.textContent = '';
  s1Questions.forEach((q, i) => {
    const card = el('div', 'q');
    const top = el('div', 'qtop');
    const kind = el('span', 'chip', q.type);
    const id = el('input'); id.type = 'text'; id.value = q.id; id.placeholder = 'question id';
    id.setAttribute('aria-label', 'question id');
    id.addEventListener('input', () => { q.id = id.value; s1Curl(); });
    const ins = el('input'); ins.type = 'text'; ins.value = q.instructions; ins.placeholder = 'what to judge';
    ins.setAttribute('aria-label', 'instructions');
    ins.addEventListener('input', () => { q.instructions = ins.value; s1Curl(); });
    const del = el('button', 'del', '×'); del.title = 'remove this question';
    del.addEventListener('click', () => { s1Questions.splice(i, 1); s1RenderQuestions(); s1Curl(); });
    top.append(kind, id, del);
    card.append(top, ins);
    if (q.type === 'choice' || q.type === 'score'){
      const crit = el('div', 'crit'); crit.style.marginTop = '7px';
      const rows = q.type === 'choice' ? q.criteria : q.levels.map(l => [null, l]);
      rows.forEach((row, j) => {
        const r = el('div', 'row');
        if (q.type === 'choice'){
          const name = el('input'); name.type = 'text'; name.value = row[0]; name.placeholder = 'option';
          name.setAttribute('aria-label', 'option name');
          name.addEventListener('input', () => { q.criteria[j][0] = name.value; s1Curl(); });
          r.append(name);
        }
        const desc = el('input'); desc.type = 'text'; desc.value = row[1] || '';
        desc.placeholder = q.type === 'choice' ? 'what it means' : 'this level';
        desc.setAttribute('aria-label', q.type === 'choice' ? 'option description' : 'level');
        desc.addEventListener('input', () => {
          if (q.type === 'choice') q.criteria[j][1] = desc.value; else q.levels[j] = desc.value;
          s1Curl();
        });
        const x = el('button', 'del', '×'); x.title = 'remove';
        x.addEventListener('click', () => {
          if (q.type === 'choice') q.criteria.splice(j, 1); else q.levels.splice(j, 1);
          s1RenderQuestions(); s1Curl();
        });
        r.append(desc, x); crit.append(r);
      });
      const add = el('button', 'btn mini ghost', q.type === 'choice' ? '+ option' : '+ level');
      add.style.alignSelf = 'flex-start';
      add.addEventListener('click', () => {
        if (q.type === 'choice') q.criteria.push(['option' + (q.criteria.length + 1), '']);
        else q.levels.push('level ' + (q.levels.length + 1));
        s1RenderQuestions(); s1Curl();
      });
      crit.append(add); card.append(crit);
    }
    box.append(card);
  });
  if (!s1Questions.length) box.append(el('p', 'note', 'No question yet: add a noul, a choice or a score.'));
}

function s1Payload(){
  const questions = {};
  s1Questions.forEach(q => {
    const id = (q.id || '').trim(); if (!id) return;
    const out = {type: q.type, instructions: q.instructions || ''};
    if (q.type === 'choice'){
      out.criteria = {};
      q.criteria.forEach(([name, desc]) => { if ((name || '').trim()) out.criteria[name.trim()] = desc || null; });
    } else if (q.type === 'score'){
      out.criteria = q.levels.filter(l => (l || '').trim());
    }
    questions[id] = out;
  });
  return {state: $('s1state').value, model: 'jev-latest', questions};
}

function s1Curl(){
  const box = $('s1curl'); if (!box) return;
  const body = JSON.stringify(s1Payload(), null, 2).split('\n').map((l, i) => i ? '  ' + l : l).join('\n');
  box.textContent = "curl -s http://127.0.0.1:30001/v1/systemone \\\n"
    + "  -H \"Authorization: Bearer $(cat ~/.config/qwen38/api-key)\" \\\n"
    + "  -H 'Content-Type: application/json' -d '" + body + "'";
}

function s1Load(name){
  const ex = S1_EXAMPLES[name]; if (!ex) return;
  $('s1state').value = ex.state;
  s1Questions = JSON.parse(JSON.stringify(ex.questions));
  document.querySelectorAll('#s1examples .btn').forEach(b => b.classList.toggle('low', b.textContent !== name));
  s1RenderQuestions(); s1Curl();
}

function s1DrawAnswer(id, ans){
  const wrap = el('div', 'ans');
  wrap.append(el('h4', null, id));
  const verdict = el('div', 'verdict');
  let rows = [];
  if (ans.type === 'noul'){
    const p = ans.noul;
    verdict.textContent = p >= 0.5 ? 'yes' : 'no';
    const s = el('small', null, (p * 100).toFixed(1) + '% yes'); verdict.append(s);
    rows = [['yes', p], ['no', 1 - p]];
  } else if (ans.type === 'choice'){
    verdict.textContent = ans.choice;
    verdict.append(el('small', null, 'confidence ' + (ans.confidence * 100).toFixed(0) + '%'));
    rows = Object.entries(ans.probabilities || {});
  } else {
    const legend = ans.legend || {};
    const n = Object.keys(legend).length;
    verdict.textContent = ans.score.toFixed(2) + ' / ' + Math.max(0, n - 1);
    verdict.append(el('small', null, (legend[String(Math.round(ans.score))] || '') + '  ·  confidence ' + (ans.confidence * 100).toFixed(0) + '%'));
    rows = Object.entries(ans.probabilities || {}).map(([k, v]) => [legend[k] || k, v]);
  }
  const top = Math.max(...rows.map(r => r[1]), 0);
  rows.forEach(([name, p]) => {
    const r = el('div', 'pr' + (p >= top && top > 0 ? ' top' : ''));
    r.append(el('span', 'nm', name === '' ? '(empty name)' : name));
    const tr = el('span', 'tr'); const fl = el('span', 'fl'); fl.style.width = (p * 100).toFixed(2) + '%';
    tr.append(fl); r.append(tr);
    r.append(el('span', 'pv', (p * 100).toFixed(1) + '%'));
    wrap.append(r);
  });
  wrap.insertBefore(verdict, wrap.children[1]);
  return wrap;
}

async function s1Run(){
  const btn = $('s1run'); const payload = s1Payload();
  if (!payload.state.trim()) { toast('A state is required: that is what the questions are asked about.', 'warn'); return; }
  if (!Object.keys(payload.questions).length) { toast('Add at least one question.', 'warn'); return; }
  btn.disabled = true; $('s1status').textContent = 'asking the lane...';
  $('s1answers').textContent = ''; $('s1meta').textContent = ''; s1Chip('s1time', '');
  try{
    const t = await fetch('/api/csrf', {method: 'POST'}); if (t.status === 401) return login();
    const tok = (await t.json()).token;
    const r = await fetch('/api/systemone', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                             body: JSON.stringify({...payload, csrf: tok})});
    if (r.status === 401) return login();
    const out = await r.json();
    if (!r.ok){
      const why = out.refused ? JSON.stringify(out.refused.detail ?? out.refused) : (out.error || ('HTTP ' + r.status));
      const p = el('p', 'note'); p.textContent = 'Refused with HTTP ' + r.status + ': ' + why;
      $('s1answers').append(p);
      s1Chip('s1time', r.status + ' refused', 'err');
      return;
    }
    const answers = (out.answer || {}).answers || {};
    Object.entries(answers).forEach(([id, a]) => $('s1answers').append(s1DrawAnswer(id, a)));
    if (!Object.keys(answers).length) $('s1answers').append(el('p', 'note', 'The call was answered with no answers.'));
    s1Chip('s1time', out.seconds + ' s', 'ok');
    const usage = (out.answer || {}).usage || {};
    const meta = $('s1meta');
    const chips = [['model', (out.answer || {}).model],
                   ['input tokens', usage.input_tokens], ['output tokens', usage.output_tokens],
                   ['branches', (out.headers || {})['x-systemone-branches']],
                   ['label mass', (out.headers || {})['x-systemone-label-mass']],
                   ['cached tokens', (out.headers || {})['x-systemone-cached-tokens']]];
    chips.forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') meta.append(el('span', 'chip', k + ' ' + v)); });
  } catch (e){
    $('s1answers').append(el('p', 'note', 'The cockpit could not reach the proxy: ' + e));
    s1Chip('s1time', 'failed', 'err');
  } finally {
    btn.disabled = false; $('s1status').textContent = '';
  }
}

async function s1Probe(){
  try{
    const r = await fetch('/api/systemone'); if (r.status === 401) return login();
    const d = await r.json();
    setText('s1lane', d.lane || '...');
    if (d.available){
      s1Chip('s1chip', 'serving', 'ok');
    } else {
      s1Chip('s1chip', 'not served here', 'err');
      note('s1note', d.reason || 'the proxy in front of this box does not answer /v1/systemone');
      const b = $('s1run'); if (b) b.disabled = true;
    }
  } catch (e){ s1Chip('s1chip', 'unknown', 'warn'); }
}

if ($('s1examples')){
  Object.keys(S1_EXAMPLES).forEach((name, i) => {
    const b = el('button', 'btn mini' + (i ? ' low' : ''), name);
    b.addEventListener('click', () => s1Load(name));
    $('s1examples').append(b);
  });
  document.querySelectorAll('[data-addq]').forEach(b => b.addEventListener('click', () => {
    const t = b.dataset.addq;
    const q = {id: t + (s1Questions.length + 1), type: t, instructions: ''};
    if (t === 'choice') q.criteria = [['yes', ''], ['no', '']];
    if (t === 'score') q.levels = ['low', 'high'];
    s1Questions.push(q); s1RenderQuestions(); s1Curl();
  }));
  $('s1run').addEventListener('click', s1Run);
  $('s1reset').addEventListener('click', () => s1Load(Object.keys(S1_EXAMPLES)[0]));
  $('s1state').addEventListener('input', s1Curl);
  $('s1copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('s1curl').textContent); toast('Copied.', 'ok', 1800); }
    catch (e) { toast('The browser refused the clipboard; select the text instead.', 'warn'); }
  });
  s1Load(Object.keys(S1_EXAMPLES)[0]);
}

// ── Image: Qwen-Image 2.1, every control it actually has ──────────────────────
// Defaults are the model's own, read out of its config rather than chosen here:
// 1024x1024, 40 steps, one image, CFG off, the RNG on the CPU (qwen_image21.py).
// Reset puts every field back to exactly this.
const IMG_DEFAULTS = {mode: 't2i', size: '1024x1024', w: 1024, h: 1024, steps: 40, n: 1,
                      bg: 'auto', fmt: 'png', seed: '', cfg: '', shift: '', dev: 'cpu',
                      neg: '', prompt: ''};
// Qwen publishes seven aspect ratios at a 2048 base. Every one is already a multiple of
// 32, and each is offered twice: at the cookbook's verified 1024-equivalent area, and at
// the size the model card itself prints. The second costs four times the first.
const IMG_SIZES = [
  ['1024x1024', '1:1  1024x1024  default'],
  ['1184x896', '4:3  1184x896'],
  ['896x1184', '3:4  896x1184'],
  ['1248x832', '3:2  1248x832'],
  ['832x1248', '2:3  832x1248'],
  ['1376x768', '16:9  1376x768'],
  ['768x1376', '9:16  768x1376'],
  ['512x512', '1:1  512x512  quick'],
  ['768x768', '1:1  768x768'],
  ['2048x2048', "1:1  2048x2048  Qwen's own, 4x the cost"],
  ['2400x1792', "4:3  2400x1792  Qwen's own"],
  ['2752x1536', "16:9  2752x1536  Qwen's own"],
  ['custom', 'custom, any multiple of 32'],
];
// Prompts worth trying, each chosen because it exercises something this model is
// specifically claimed to do. The transparent ones follow the wording Qwen's own card
// uses, because the alpha is conditioned by the prompt, not by the background field.
const IMG_EXAMPLES = {
  'Capybara': {mode: 't2i', prompt: 'A capybara reading a book by candlelight'},
  'Text rendering': {mode: 't2i', size: '1376x768',
    prompt: 'A neon shop sign that reads "QWEN IMAGE 2.1", rainy night, reflections on wet pavement'},
  'Transparent cutout': {mode: 't2i', bg: 'transparent',
    prompt: 'This is an RGBA image with transparency. A single fluffy orange cat sitting, full body, '
          + 'isolated on a transparent background. The image has an alpha channel and the background is '
          + 'transparent. A clean cutout, no floor, no shadow, no background.'},
  'Transparent sticker': {mode: 't2i', bg: 'transparent',
    prompt: 'This is an RGBA image with transparency. A cute cartoon dragon sticker. The image has an '
          + 'alpha channel and the background is transparent.'},
  // the cookbook's own edit example, word for word, and the subject of "Use a sample"
  'Local edit': {mode: 'edit',
    prompt: 'Change the red teapot to blue, keeping its shape, table, window, and lighting unchanged.'},
  'Combine two': {mode: 'edit',
    prompt: 'Combine the subjects from Picture 1 and Picture 2 into one coherent scene, preserving their appearance.'},
};
let imgRefs = [];          // [{name, dataUrl, w, h}], in the order the model labels them


const imgVal = id => ($(id) ? $(id).value.trim() : '');
function imgSize(){
  if ($('imgsize').value !== 'custom') {
    const [w, h] = $('imgsize').value.split('x').map(Number);
    return {w, h};
  }
  return {w: Number($('imgw').value) || 0, h: Number($('imgh').value) || 0};
}

// Height and width must be positive multiples of 32 (the cookbook says so, and the
// engine proves it: 1328x1328 comes back 500 with "must be divisible by 32" in its log).
// Refusing here costs nothing; refusing there costs a 500 with no explanation in the body.
function imgProblem(){
  const {w, h} = imgSize();
  if (!w || !h) return 'Set a width and a height.';
  if (w % 32 || h % 32) return `${w}x${h} is not a multiple of 32, and the engine refuses those with a bare `
    + `HTTP 500. Nearest: ${Math.max(32, Math.round(w / 32) * 32)}x${Math.max(32, Math.round(h / 32) * 32)}.`;
  const steps = Number($('imgsteps').value);
  if (!(steps >= 1 && steps <= 100)) return 'Steps run from 1 to 100. The model default is 40.';
  const n = Number($('imgn').value);
  if (!(n >= 1 && n <= 10)) return 'Between 1 and 10 images per call.';
  if (n * w * h > IMG_MAX_PIXELS) return `${n} image${n > 1 ? 's' : ''} of ${w}x${h} is `
    + `${(n * w * h / 1e6).toFixed(1)} megapixels in one call, and the largest call measured on this box is `
    + `${(IMG_MAX_PIXELS / 1e6).toFixed(1)}: one 2752x1536 image, 44.8 GB at its peak. The images of a call `
    + 'run as one batch, so its memory grows with their total size, and on this box running out hangs the '
    + 'machine. Ask for fewer or smaller images, or make several calls.';
  const cfg = Number(imgVal('imgcfg') || 1);
  if (cfg > 1 && !imgVal('imgneg')) return 'A CFG scale above 1 does nothing without a negative prompt: '
    + 'the engine needs both, and ignores the scale alone byte for byte.';
  if (imgVal('imgneg') && cfg <= 1) return 'A negative prompt does nothing without a CFG scale above 1: '
    + 'the engine needs both.';
  if ($('imgmode-edit').getAttribute('aria-pressed') === 'true' && !imgRefs.length)
    return 'Editing needs at least one reference image.';
  return '';
}

// Fitted to this box, end to end, on six measured sizes at 40 steps: 512x512 9.0 s,
// 768x768 22.6, 1024x1024 36.6-38.2, 1664x928 60.4, 2048x2048 190.9, 2752x1536 193.1.
// Not linear in pixels: four times the pixels of 1024x1024 cost 5.2 times the time,
// because attention grows faster than the token count. A power law fits all six within
// 6%: t = 1 + steps x 0.97 x (pixels / 1024^2)^1.14. Linear in steps, measured at 1024
// (8/20/40/60 -> 8.4/19.8/38.2/57.3). Editing pays for one more encode: 44.6 s where the
// same generation costs 38.2.
function imgEstimate(req){
  const q = req || imgFormRequest();
  if (!q.w || !q.h || !q.steps) return null;
  const px = (q.w * q.h) / (1024 * 1024);
  return (1 + q.steps * 0.97 * Math.pow(px, 1.14) + (q.editing ? 6 : 0)) * (q.n || 1);
}
// What the form would send. The request in flight keeps its own copy (IMG_RUN), because
// the form can change under it, and the sample reference is a request of its own.
function imgFormRequest(){
  const {w, h} = imgSize();
  return {w, h, steps: Number($('imgsteps').value) || 0, n: Number($('imgn').value) || 1,
          editing: $('imgmode-edit').getAttribute('aria-pressed') === 'true'};
}

function imgCost(){
  const secs = imgEstimate();
  const {w, h} = imgSize();
  const bits = [];
  if (secs) bits.push('about ' + fmtDur(secs) + ' on this box');
  if (w && h) bits.push((w * h / 1e6).toFixed(2) + ' megapixels');
  if (w * h >= 3.5e6) bits.push('peaks at 44.8 GB instead of 34: four times the pixels, five times the time');
  setText('imgcost', bits.join(' · '));
}

function imgPayload(){
  const {w, h} = imgSize();
  const p = {prompt: imgVal('imgprompt'), width: w, height: h,
             num_inference_steps: Number($('imgsteps').value),
             n: Number($('imgn').value),
             // Always explicit. Left out, the engine falls back to JPEG, and this model
             // returns RGBA for everything, so PIL refuses and the request 500s. The
             // barest possible request fails for that reason alone.
             output_format: $('imgfmt').value,
             response_format: 'b64_json',
             generator_device: $('imgdev').value};
  if ($('imgbg').value !== 'auto') p.background = $('imgbg').value;
  if (imgVal('imgseed') !== '') p.seed = Number(imgVal('imgseed'));
  if (imgVal('imgcfg') !== '') p.true_cfg_scale = Number(imgVal('imgcfg'));
  if (imgVal('imgshift') !== '') p.flow_shift = Number(imgVal('imgshift'));
  if (imgVal('imgneg') !== '') p.negative_prompt = imgVal('imgneg');
  return p;
}

// No Authorization header: this lane has no --api-key to check one against. And the host
// is the lane's own bind, read back from the installed unit, not the address this browser
// happens to be on: a cockpit opened over Tailscale would otherwise print a URL that
// cannot work, because the lane listens on loopback.
const shq = s => "'" + String(s).replace(/'/g, `'\\''`) + "'";   // survives an apostrophe in a prompt
function imgCurl(){
  const editing = $('imgmode-edit').getAttribute('aria-pressed') === 'true';
  const p = imgPayload();
  const base = `http://${IMG_STATE.host || '127.0.0.1'}:${IMG_STATE.port || 30020}/v1/images`;
  const where = IMG_STATE.host && IMG_STATE.host !== '127.0.0.1' ? ''
    : '# run this ON the box: the image lane listens on loopback and has no API key\n';
  let text;
  if (!editing){
    text = where + `curl -sS ${base}/generations \\\n`
         + `  -H 'Content-Type: application/json' \\\n`
         + `  -d ${shq(JSON.stringify(p, null, 2))}`;
  } else {
    const fields = Object.entries(p).filter(([k]) => k !== 'width' && k !== 'height')
      .map(([k, v]) => `  --form-string ${shq(k + '=' + v)}`);
    fields.unshift(`  --form-string ${shq('size=' + p.width + 'x' + p.height)}`);
    const refs = (imgRefs.length ? imgRefs : [{name: 'input.png'}])
      .map(r => `  -F "image[]=@${r.name};type=image/png"`);
    text = where + `curl -sS ${base}/edits \\\n` + [...fields, ...refs].join(' \\\n');
  }
  setText('imgcurl', text);
}

function imgSync(){
  const custom = $('imgsize').value === 'custom';
  $('imgwbox').hidden = !custom; $('imghbox').hidden = !custom;
  if (!custom){
    const [w, h] = $('imgsize').value.split('x');
    $('imgw').value = w; $('imgh').value = h;
  }
  const editing = $('imgmode-edit').getAttribute('aria-pressed') === 'true';
  $('imgrefbox').hidden = !editing;
  $('imgrun').textContent = editing ? 'Edit' : 'Generate';
  const problem = imgProblem();
  note('imgwarn', problem);
  // IMG_STATE.available belongs in here too: without it, typing one character in the
  // prompt re-enabled the button on a lane that is not serving, and so did the finally
  // of a failed run.
  $('imgrun').disabled = !IMG_STATE.available || IMG_STATE.busy || !!imgInflight || !!problem || !imgVal('imgprompt');
  imgCancelSync();
  imgCost(); imgCurl();
}

function imgReset(){
  const d = IMG_DEFAULTS;
  imgMode(d.mode);
  $('imgsize').value = d.size; $('imgw').value = d.w; $('imgh').value = d.h;
  $('imgsteps').value = d.steps; $('imgn').value = d.n;
  $('imgbg').value = d.bg; $('imgfmt').value = d.fmt; $('imgdev').value = d.dev;
  $('imgseed').value = d.seed; $('imgcfg').value = d.cfg; $('imgshift').value = d.shift;
  $('imgneg').value = d.neg; $('imgprompt').value = d.prompt;
  $('imgadv').open = false;
  imgSync();
}

function imgMode(mode){
  $('imgmode-t2i').setAttribute('aria-pressed', String(mode === 't2i'));
  $('imgmode-edit').setAttribute('aria-pressed', String(mode === 'edit'));
}

function imgLoadExample(name){
  const ex = IMG_EXAMPLES[name]; if (!ex) return;
  imgReset();
  imgMode(ex.mode);
  $('imgprompt').value = ex.prompt;
  if (ex.size) $('imgsize').value = ex.size;
  if (ex.bg) $('imgbg').value = ex.bg;
  imgSync();
}

// References are re-encoded to PNG and capped on the long side before they leave the
// browser. The model resizes them to roughly the output area anyway, so a 12-megapixel
// phone photo would be megabytes spent to be thrown away, and PNG is what keeps an alpha
// channel that the model is documented to read.
const IMG_REF_MAX = 1280;
function imgAddFiles(files){
  const room = 10 - imgRefs.length;
  if (files.length > room) toast(`Ten references is the model's maximum; taking the first ${room}.`, 'warn');
  [...files].slice(0, Math.max(0, room)).forEach(f => {
    const fr = new FileReader();
    fr.onload = () => {
      const im = new Image();
      im.onload = () => {
        // The count is re-read HERE, not before the callbacks: picking a second batch
        // while the first is still decoding made two calls compute room from the same
        // length and queue twelve references for a model that takes ten.
        if (imgRefs.length >= 10){ toast('Ten references is the maximum.', 'warn'); return; }
        const scale = Math.min(1, IMG_REF_MAX / Math.max(im.width, im.height));
        const c = document.createElement('canvas');
        c.width = Math.round(im.width * scale); c.height = Math.round(im.height * scale);
        c.getContext('2d').drawImage(im, 0, 0, c.width, c.height);
        imgRefs.push({name: f.name, dataUrl: c.toDataURL('image/png'), w: c.width, h: c.height});
        imgDrawRefs();
      };
      im.onerror = () => toast(`${f.name} is not an image the browser can read.`, 'err');
      im.src = fr.result;
    };
    fr.readAsDataURL(f);
  });
}

function imgDrawRefs(){
  const box = $('imgrefs'); clear(box);
  imgRefs.forEach((r, i) => {
    const d = el('div', 'ref');
    const im = el('img'); im.src = r.dataUrl; im.alt = r.name; im.title = `${r.name}, ${r.w}x${r.h}`;
    const x = el('button', '', '×'); x.title = 'remove';
    x.addEventListener('click', () => { imgRefs.splice(i, 1); imgDrawRefs(); });
    d.append(im, x, el('span', 'n', 'Picture ' + (i + 1)));
    box.append(d);
  });
  setText('imgrefcount', imgRefs.length + ' of 10');
  imgSync();
}

function imgDraw(out, fmt){
  // The format is the one this answer was REQUESTED with, captured by the caller. Read
  // off the select at click time instead, changing it without regenerating saved PNG
  // bytes under a .webp name.
  imgParkBar();                      // before the frame holding it is emptied
  const box = $('imgout'); clear(box);
  const imgs = (out.data || []);
  const shots = el('div', 'shots' + (imgs.length > 1 ? ' multi' : ''));
  imgs.forEach((d, i) => {
    const w = el('div', 'shot');
    const im = el('img');
    im.src = 'data:image/' + (fmt === 'webp' ? 'webp' : 'png') + ';base64,' + d.b64_json;
    im.alt = 'generated image ' + (i + 1);
    w.append(im); shots.append(w);
  });
  box.append(shots);
  const meta = $('imgmeta'); clear(meta);
  const bytes = imgs.reduce((a, d) => a + (d.b64_json || '').length * 0.75, 0);
  [['images', imgs.length], ['size', (bytes / 1e6).toFixed(1) + ' MB'],
   ['peak memory', out.peak_memory_mb ? (out.peak_memory_mb / 1024).toFixed(1) + ' GB' : null],
   ['engine time', out.inference_time_s ? out.inference_time_s.toFixed(1) + ' s' : null],
  ].forEach(([k, v]) => { if (v != null) meta.append(el('span', 'chip', k + ': ' + v)); });
  const dl = el('button', 'btn mini low', 'Download');
  dl.addEventListener('click', () => imgs.forEach((d, i) => {
    const a = document.createElement('a');
    a.href = 'data:application/octet-stream;base64,' + d.b64_json;
    a.download = `qwen-image-${Date.now()}${imgs.length > 1 ? '-' + i : ''}.${fmt}`;
    a.click();
  }));
  meta.append(dl);
  if (imgs.length){
    const again = el('button', 'btn mini low', 'Send the first one back as a reference');
    again.addEventListener('click', () => {
      if (imgRefs.length >= 10) return toast('Ten references is the maximum.', 'warn');
      imgRefs.push({name: 'previous-output.png', dataUrl: 'data:image/png;base64,' + imgs[0].b64_json, w: 0, h: 0});
      imgMode('edit'); imgDrawRefs();
      toast('Added as Picture ' + imgRefs.length + '. Multi-round editing is chaining these.', 'ok');
    });
    meta.append(again);
  }
}

// The frame is the shape of the answer: a 16:9 wait should not look like a square one,
// and an empty panel for 38 seconds looks like nothing is happening.
function imgFrame(n){
  const {w, h} = imgSize();
  imgParkBar();                      // before the frame holding it is emptied
  const box = $('imgout'); clear(box);
  const wrap = el('div', 'shots' + (n > 1 ? ' multi' : ''));
  for (let i = 0; i < Math.min(n, 2); i++){
    const f = el('div', 'frame');
    f.style.aspectRatio = `${w} / ${h}`;
    // The progress goes inside the frame of the answer it is producing, where the eye
    // already is. Below it, a 1024 square pushed it off a laptop screen.
    if (i === 0) f.append(imgRunProg());
    else f.append(el('span', null, `image ${i + 1} of ${n}`));
    wrap.append(f);
  }
  box.append(wrap);
  // On a phone the result panel sits under the form: bring it on screen, or the wait
  // happens somewhere the user cannot see.
  const panel = box.closest('.panel');
  const r = panel && panel.getBoundingClientRect();
  if (r && (r.top > window.innerHeight * 0.6 || r.bottom < 0)){
    // Offset by the sticky bars as they are right now: on a phone the top bar wraps onto
    // two or three rows, so a fixed margin would park the panel's own header under it.
    const cover = [...document.querySelectorAll('.top, .jobstrip')]
      .reduce((h, e) => h + (e.offsetParent !== null ? e.getBoundingClientRect().height : 0), 0);
    window.scrollTo({top: window.scrollY + r.top - cover - 8, behavior: 'smooth'});
  }
}
// The bar has a home under the output, and leaves it only while a request runs. It is
// held by reference because it travels INTO the frame, and emptying the output panel
// (a finished image, a refusal) detaches it: getElementById does not find a detached
// node, so the next generation would have appended null.
function imgParkBar(){
  const p = imgRunProg(); const meta = $('imgmeta');
  if (p && meta && p.nextElementSibling !== meta) meta.before(p);
  if (p) p.hidden = true;
}

async function imgRun(){
  const problem = imgProblem();
  if (problem) return toast(problem, 'warn');
  const editing = $('imgmode-edit').getAttribute('aria-pressed') === 'true';
  const btn = $('imgrun'); btn.disabled = true;
  const est = imgEstimate();
  setText('imgstatus', editing ? 'editing...' : 'generating...');
  setChip('imgtime', est ? '~' + fmtDur(est) : '');
  const t0 = Date.now();
  imgInflight = t0; IMG_RUN = imgFormRequest(); imgCancelSync();
  imgFrame(Number($('imgn').value) || 1);
  clear($('imgmeta'));
  imgStageAt = {stage: '', at: 0};
  imgBar(IMG_RUN_BAR, {label: 'sending the request'}, '', null);
  // One poll drives the bar off the engine's log; the chip keeps the wall clock.
  imgWatch(true, 1500);
  const tick = setInterval(() => setChip('imgtime', fmtDur((Date.now() - t0) / 1000)
    + (est ? ' of ~' + fmtDur(est) : '')), 1000);
  try{
    const t = await fetch('/api/csrf', {method: 'POST'}); if (t.status === 401) return login();
    const tok = (await t.json()).token;
    const body = {...imgPayload(), csrf: tok};
    if (editing) body.images = imgRefs.map(r => r.dataUrl);
    const r = await fetch(editing ? '/api/image/edit' : '/api/image/generate',
                          {method: 'POST', headers: {'Content-Type': 'application/json'},
                           body: JSON.stringify(body)});
    if (r.status === 401) return login();
    const out = await r.json();
    if (!r.ok && (IMG_INTERRUPTED > t0 || out.interrupted)){
      imgParkBar(); clear($('imgout'));
      $('imgout').append(el('p', 'note', 'Cancelled: the lane was stopped or restarted while this image was '
        + 'being made, which is the only way this runtime can end a generation early. Nothing was kept.'));
      setChip('imgtime', 'cancelled', 'warn');
      return;
    }
    if (!r.ok){
      const why = out.error || (out.refused ? JSON.stringify(out.refused).slice(0, 300) : 'HTTP ' + r.status);
      if (r.status === 409){ toast(why, 'warn', 7000); setChip('imgtime', 'lane busy', 'warn'); return; }
      // 504: this page stopped waiting and the lane goes on (it has no abort); not a refusal
      if (r.status === 504){ imgParkBar(); clear($('imgout')); $('imgout').append(el('p', 'note', why)); setChip('imgtime', 'still generating', 'warn'); return; }
      imgParkBar(); clear($('imgout')); $('imgout').append(el('p', 'note', 'Refused with HTTP ' + r.status + ': ' + why));
      setChip('imgtime', r.status + ' refused', 'err');
      return;
    }
    imgDraw(out.image || out, body.output_format);
    setChip('imgtime', (out.seconds != null ? out.seconds.toFixed(1) + ' s' : 'done'), 'ok');
  } catch (e){
    if (IMG_INTERRUPTED > t0){ setChip('imgtime', 'cancelled', 'warn'); }
    else { toast('The cockpit could not reach the image lane: ' + e.message, 'err'); setChip('imgtime', 'failed', 'err'); }
  } finally {
    clearInterval(tick); imgInflight = null; IMG_RUN = null;
    imgParkBar();
    imgWatch(false);
    btn.disabled = false; setText('imgstatus', ''); imgSync();
    imgLane();                     // the lane may have gone away under the request
  }
}

let imgPoll = null;
function imgWatch(on, ms = 2500){
  // A request in flight is its own reason to keep asking: the lane answering /health is
  // not the same as this page having nothing left to draw.
  if (!on && imgInflight){ on = true; ms = 1500; }
  // While it loads its 31 GB the unit reads `active` and /health answers 503, and while a
  // request runs nothing comes back until it is done, so the only honest progress signal
  // is asking again.
  if (on && imgPoll && imgPoll.ms === ms) return;
  if (imgPoll){ clearInterval(imgPoll.id); imgPoll = null; }
  if (on) imgPoll = {id: setInterval(imgLane, ms), ms};
}

// One drawer for both bars: the phase name on the left, the number on the right.
// The NAME is the engine's own and exact. The number beside a request is this page's
// clock against the measured cost, and says "about" because the engine gives no live
// per-step signal: tqdm writes with carriage returns, journald only breaks on newlines,
// /metrics is not served and /stats is empty.
function imgBar(ids, prog, right, pct){
  const box = ids.box === 'imgrunprog' ? imgRunProg() : $(ids.box);
  if (!prog || !prog.label){ box.hidden = true; return; }
  box.hidden = false;
  box.classList.toggle('indet', pct == null);
  const lab = $(ids.lab); clear(lab);
  lab.append(el('b', '', prog.label));
  setText(ids.pct, right || '');
  // An indeterminate bar owns the whole track and slides inside it; a determinate one
  // is a width. Going from a full track to 2% would read as progress running backwards.
  $(ids.bar).style.width = pct == null ? '100%' : Math.max(2, Math.min(100, pct)) + '%';
}
const IMG_RUN_BAR = {box: 'imgrunprog', lab: 'imgrunlab', pct: 'imgrunpct', bar: 'imgrunbar'};

// When the engine entered the stage it is in now. The page times the bar from here
// rather than from when the request was sent, so a long prompt encode does not eat the
// denoise's budget.
let imgStageAt = {stage: '', at: 0};
function imgDrawRunBar(p){
  if (!p || !p.label) return imgBar(IMG_RUN_BAR, {label: 'waiting for the lane'}, '', null);
  if (p.stage !== imgStageAt.stage) imgStageAt = {stage: p.stage, at: Date.now()};
  const secs = (Date.now() - imgStageAt.at) / 1000;
  if (p.stage !== 'denoising')
    return imgBar(IMG_RUN_BAR, p, fmtDur(secs), null);   // seconds, no honest fraction
  // Denoising is nearly all of the cost and is linear in steps, measured on this box.
  // Timed against the request that is actually running, not the form as it reads now.
  const req = IMG_RUN || imgFormRequest();
  const steps = req.steps || 40;
  const budget = Math.max(1, imgEstimate(req) - 3);
  const frac = Math.min(secs / budget, 0.99);
  imgBar(IMG_RUN_BAR, {label: `denoising, ${steps} steps`},
         `about ${Math.round(frac * 100)}%, ~${fmtDur(Math.max(0, budget - secs))} left`,
         frac * 100);
}

// The Image tab reads the SAME lifecycle as the action bar, the lane pill and the
// Engines tab, so the four can never disagree about whether this lane is up. It has no
// start or stop of its own: the image lane is switched to and started exactly like the
// two text lanes, from the one switcher at the top.
async function imgLane(){
  try{
    const r = await fetch('/api/image');
    if (r.status === 401) return;
    const d = await r.json();
    IMG_STATE.port = d.port || 30020;
    IMG_STATE.host = d.host || '127.0.0.1';
    IMG_STATE.available = !!d.available;
    IMG_STATE.installed = !!d.installed;
    IMG_STATE.model = d.model || 'Qwen/Qwen-Image-2.1';
    const p = d.progress || {};
    if (imgInflight) imgDrawRunBar(p.kind === 'boot' ? null : p);
    // Someone else generating counts: this lane serves one at a time, and a second
    // request does not queue politely, it holds another 60 GB of a 122 GB box.
    IMG_STATE.busy = !imgInflight && p.kind === 'stage';
    IMG_STATE.busyLabel = p.label || '';
    if (IMG_STATE.busy) imgWatch(true, 2000);
    imgRenderLane();
  } catch (e){ setChip('imgchip', 'unknown', 'warn'); }
}

// Render only, no request: called from imgLane() and from every lifecycle tick, so a
// boot moves here at the same pace it moves in the Engines tab.
function imgRenderLane(){
  if (!$('imgctl')) return;
  setText('imgmodel', IMG_STATE.model || 'Qwen/Qwen-Image-2.1');
  const e = ((F.life || {}).engines || {})[IMAGE_UNIT];
  const ctl = $('imgctl'); clear(ctl);
  const boot = $('imgboot'); clear(boot);
  const guide = $('imgsteps-guide'); clear(guide); guide.hidden = true;
  const say = txt => ctl.append(el('span', 'chip', txt));
  const serving = servingEngine();
  const other = serving && serving[0] !== IMAGE_UNIT ? serving[0] : null;
  const otherName = other ? laneLabel(other) : '';
  // The lifecycle only carries this unit when it is installed, so its presence is the
  // answer, once there IS a lifecycle. Before its first snapshot (the page just opened)
  // nothing is known, and "install it with" on a box that has the lane is a false fact.
  if (!F.life){
    setChip('imgchip', 'checking', '');
  } else if (!e){
    setChip('imgchip', 'not installed', 'warn');
    say('install it with:  ./install.sh --with-image');
    say('38 GB, about 25 min, one command');
  } else if (e.state === 'ready' || e.state === 'degraded'){
    setChip('imgchip', IMG_STATE.busy ? 'busy: ' + IMG_STATE.busyLabel : 'serving on :' + IMG_STATE.port,
            IMG_STATE.busy ? 'warn' : 'ok');
    say(IMG_STATE.busy ? 'someone is generating: one image at a time on this lane'
                       : 'stop it from the action bar at the top, like any lane');
  } else if (TRANSITIONAL.has(e.state)){
    setChip('imgchip', STATE_LABEL[e.state] || e.state, 'warn');
    boot.append(bootBlock(e, IMAGE_UNIT));
    say('Generate turns on by itself the moment it answers');
  } else if (e.state === 'stopping'){
    setChip('imgchip', 'stopping', 'warn');
    boot.append(stoppingBlock(e));
  } else {
    setChip('imgchip', e.state === 'failed' ? 'failed' : 'stopped', e.state === 'failed' ? 'err' : '');
    if (e.state === 'failed') say('the unit failed: its journal is in the Logs tab');
    // The same three moves as for any lane, in the action bar at the top, named exactly
    // as its buttons read. Only the steps still to do are shown, the first one marked.
    // Switch comes first, as in the README and the installer: stopped first, the lane
    // button offers "Start 27B" (the unit still enabled), one click from a 7-minute boot
    // nobody asked for; switched first, it goes from "Stop 27B" straight to "Start
    // Qwen-Image".
    const steps = [];
    if (enabledUnit() !== IMAGE_UNIT) steps.push('Pick Qwen-Image 2.1 in the switcher, then press Switch');
    if (other) steps.push(`Stop ${LANE_NAME[other] || otherName}: ${otherName} is serving, and two engines never run at once`);
    steps.push(`Press Start Qwen-Image (${readyIn(IMAGE_UNIT)} to ready)`);
    guide.hidden = false;
    steps.forEach((s, i) => guide.append(el('li', i === 0 ? 'now' : '', s)));
  }
  // The lifecycle derives ready from the lane's own /health answering 200, every two
  // seconds. Trusting it here, rather than the tab's last fetch, is what lets Generate
  // turn on by itself at the end of a boot the tab watched from its first second.
  const ready = !!e && (e.state === 'ready' || e.state === 'degraded');
  IMG_STATE.available = ready;
  // This page's own request in flight keeps it off too: IMG_STATE.busy is only about
  // SOMEONE ELSE's request, so without imgInflight here every two-second lifecycle tick
  // turned Generate back on mid-run, and a second click raced the first to a 409.
  $('imgrun').disabled = !ready || IMG_STATE.busy || !!imgInflight || !!imgProblem() || !imgVal('imgprompt');
  imgCancelSync();
}

// Cancel is there whenever a generation runs, this page's or another tab's: the runtime
// cannot abort a request, so the only way to end one is to restart the lane, and a
// 47-minute call (ten 2048x2048 images at 60 steps) had no way out but the lane's Stop.
function imgCancelSync(){
  const b = $('imgcancel'); if (b) b.hidden = !(imgInflight || IMG_STATE.busy);
}
function imgCancel(){
  askAction('unit', {verb: 'restart', unit: IMAGE_UNIT}, ['sudo', '-n', '/usr/bin/systemctl', 'restart', IMAGE_UNIT],
    ['SGLang Diffusion cannot abort a request, so cancelling restarts the lane: the image being made is lost, '
     + `and the lane answers again in ${readyIn(IMAGE_UNIT)}.`]);
}

function imgInit(){
  if (!$('imgsize')) return;
  IMG_SIZES.forEach(([v, label]) => {
    const o = document.createElement('option'); o.value = v; o.textContent = label;
    $('imgsize').append(o);
  });
  Object.keys(IMG_EXAMPLES).forEach((name, i) => {
    const b = el('button', 'btn mini' + (i ? ' low' : ''), name);
    b.addEventListener('click', () => imgLoadExample(name));
    $('imgexamples').append(b);
  });
  ['imgsize', 'imgw', 'imgh', 'imgsteps', 'imgn', 'imgbg', 'imgfmt', 'imgdev',
   'imgseed', 'imgcfg', 'imgshift', 'imgneg', 'imgprompt'].forEach(id => {
    const e = $(id); if (e) { e.addEventListener('input', imgSync); e.addEventListener('change', imgSync); }
  });
  ['imgmode-t2i', 'imgmode-edit'].forEach(id => $(id).addEventListener('click', () => {
    imgMode($(id).dataset.mode); imgSync();
  }));
  $('imgreset').addEventListener('click', () => { imgRefs = []; imgDrawRefs(); imgReset();
    toast('Back to the model’s own defaults: 1024×1024, 40 steps, one image, CFG off.', 'ok', 2600); });
  $('imgrefadd').addEventListener('click', () => $('imgreffile').click());
  $('imgreffile').addEventListener('change', e => { imgAddFiles(e.target.files); e.target.value = ''; });
  // The sample is a generation like any other, so it waits like one: the same frame,
  // the same bar in it, the same lock on both buttons. It used to run for twenty seconds
  // behind a one-line status, with both buttons still live, so a second click was a
  // second request the cockpit refused with 409 and a toast that read like a failure.
  // The subject is the cookbook's own edit example, a red teapot by a window, so the
  // "Local edit" prompt (also the cookbook's) actually describes what is in the picture.
  $('imgrefsample').addEventListener('click', async () => {
    if (imgRefs.length >= 10) return toast('Ten references is the maximum.', 'warn');
    if (imgInflight || IMG_STATE.busy) return toast('The lane is already generating; one image at a time.', 'warn');
    const btns = [$('imgrefsample'), $('imgrun')];
    btns.forEach(b => { b.disabled = true; });
    const t0 = Date.now(); imgInflight = t0; imgCancelSync();
    IMG_RUN = {w: 1024, h: 1024, steps: 20, n: 1, editing: false};   // what the sample asks for
    setText('imgstatus', 'making a sample reference: a red teapot by a window');
    imgFrame(1); imgStageAt = {stage: '', at: 0};
    imgBar(IMG_RUN_BAR, {label: 'making the sample reference'}, '', null);
    imgWatch(true, 1500);
    try{
      const t = await fetch('/api/csrf', {method: 'POST'}); if (t.status === 401) return login();
      const tok = (await t.json()).token;
      const r = await fetch('/api/image/generate', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({csrf: tok, prompt: 'A bright daylight photograph of a red teapot on a wooden table '
          + 'next to a window, even natural light', width: 1024, height: 1024, num_inference_steps: 20, n: 1,
          output_format: 'png', response_format: 'b64_json', generator_device: 'cpu', seed: 42})});
      const out = await r.json();
      if (!r.ok && (IMG_INTERRUPTED > t0 || out.interrupted)) return toast('Sample cancelled: the lane was stopped or restarted.', 'warn');
      if (!r.ok) return toast(r.status === 409 ? out.error : 'Could not make a sample: ' + (out.error || r.status),
                              r.status === 409 ? 'warn' : 'err', 7000);
      const first = ((out.image || out).data || [])[0];
      if (!first || !first.b64_json)
        return toast('The lane answered 200 with no image in it.', 'err');
      imgRefs.push({name: 'sample-teapot.png', dataUrl: 'data:image/png;base64,' + first.b64_json,
                    w: 1024, h: 1024});
      imgDraw(out.image || out, 'png');
      imgDrawRefs();
      toast('Sample added as Picture ' + imgRefs.length + ': a red teapot. The "Local edit" example turns it blue.', 'ok', 5000);
    } catch (e){
      toast('Could not make a sample: ' + e.message, 'err');
    } finally {
      imgInflight = null; IMG_RUN = null; imgParkBar(); imgWatch(false);
      setText('imgstatus', ''); btns.forEach(b => { b.disabled = false; }); imgSync();
    }
  });
  $('imgrun').addEventListener('click', imgRun);
  $('imgcancel').addEventListener('click', imgCancel);
  $('imgcopy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('imgcurl').textContent); toast('Copied.', 'ok', 1800); }
    catch (e) { toast('The browser refused the clipboard; select the text instead.', 'warn'); }
  });
  imgReset();
  imgLoadExample(Object.keys(IMG_EXAMPLES)[0]);
  imgLane();
}

imgInit();
