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
const setChip = (id, txt, cls) => { const e = $(id); if (e) { e.textContent = txt; e.className = 'chip ' + (cls || ''); } };
const el = (tag, cls, txt) => { const n = document.createElement(tag); if (cls) n.className = cls; if (txt != null) n.textContent = txt; return n; };
const clear = n => { while (n.firstChild) n.removeChild(n.firstChild); };
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

// ── vocabulary: one word per engine state, everywhere ─────────────────────────
const STATE_LABEL = {
  stopped: 'stopped', failed: 'failed', starting: 'starting', 'loading-weights': 'loading weights',
  'loading-draft': 'loading the draft head', 'allocating-kv': 'allocating the KV pool',
  'capturing-graphs': 'capturing CUDA graphs', 'warming-up': 'warming up', ready: 'ready',
  degraded: 'ready but not answering', stopping: 'stopping', wedged: 'wedged: no generation'};
const STATE_CHIP = {ready: 'ok', degraded: 'warn', failed: 'err', stopped: '', stopping: 'warn', wedged: 'err'};
// The rail badge sits next to a nav label in 236 px: one short word, never a state sentence.
const STATE_BADGE = {failed: 'failed', degraded: 'degraded', wedged: 'wedged', stopping: 'stopping'};
const TRANSITIONAL = new Set(['starting', 'loading-weights', 'loading-draft', 'allocating-kv', 'capturing-graphs', 'warming-up']);
const STAGE_LABEL = {'init': 'init', 'loading-weights': 'weights', 'loading-draft': 'draft', 'allocating-kv': 'KV', 'capturing-graphs': 'graphs', 'warming-up': 'warmup'};
const ALL_STAGES = Object.keys(STAGE_LABEL);
const LANE_NAME = {'qwen38-sglang.service': '27B', 'qwen38-flash.service': 'flash 176B'};
// The three 27B targets share one unit, so the lane name alone ("27B") does not say
// which checkpoint is loaded. Every control that names the lane says the checkpoint too.
const TARGET_SHORT = {stock: 'stock', uncensored: 'uncensored', fp8: 'FP8',
                      'uncensored-fp8': 'FP8 uncensored', flash: ''};
function laneLabel(unit){
  const base = LANE_NAME[unit] || unit.replace('.service', '');
  // the unit file says what it is configured to serve, and it can be read while the
  // engine is still loading; the live engine only confirms it once it answers
  const cfg = ((F.life || {}).engines || {})[unit] || {};
  const serving = servingEngine();
  const target = (serving && serving[0] === unit && F.target) || cfg.target;
  const t = target && TARGET_SHORT[target] ? ' ' + TARGET_SHORT[target] : '';
  return base + t;
}
const laneCls = name => name.includes('flash') ? 'flash' : 'lane27';
const stateChipCls = st => (STATE_CHIP[st] ?? 'warn') + (st === 'stopping' || TRANSITIONAL.has(st) ? ' live' : '');

// ── tabs and the collapsible rail ─────────────────────────────────────────────
// declared here, not next to its loader: showTab() runs at parse time and reads it
const loaded = {recipes: false};
const TABS = ['overview', 'engines', 'requests', 'machine', 'models', 'logs', 'setup'];
let activeTab = 'overview';
function showTab(name, push = true){
  if (!TABS.includes(name)) name = 'overview';
  activeTab = name;
  TABS.forEach(t => { const p = $('tab-' + t); if (p) p.classList.toggle('active', t === name); });
  document.querySelectorAll('.rail .nav').forEach(b => {
    if (b.dataset.tab === name) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  if (push && location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
  if (name === 'models' && !loaded.recipes) loadRecipes();
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
    $('memgauge').className = 'gauge' + (pct > 90 ? ' crit' : pct > 80 ? ' warn' : '');
    const cls = m.MemAvailable < 8 * GB ? 'err' : m.MemAvailable < 15 * GB ? 'warn' : 'ok';
    setChip('memchip', fmtB(m.MemAvailable) + ' free', cls); setChip('memchip2', fmtB(m.MemAvailable) + ' free', cls);
    setText('memavail', fmtB(m.MemAvailable)); setText('memavail2', fmtB(m.MemAvailable)); setText('memused2', fmtB(used));
    setText('memcache', fmtB(m.Cached)); setText('swap', fmtB((m.SwapTotal || 0) - (m.SwapFree || 0)));
    push('mem', used / GB); drawSpark($('memspark'), 'mem', css('--acc'), m.MemTotal / GB);
    badge('machine', m.MemAvailable < 8 * GB ? fmtB(m.MemAvailable) : '', m.MemAvailable < 8 * GB ? 'err' : '');
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
  F.pool = null; F.maxRun = null; rPool(); rReservoir();
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
    const c1 = tr.insertCell(); c1.textContent = r.peer; c1.className = 'num';
    tr.insertCell().textContent = r.path;
    const c2 = tr.insertCell(); c2.textContent = r.bytes >= 1024 ? (r.bytes / 1024).toFixed(0) + ' KB' : r.bytes + ' B'; c2.className = 'r num';
    const c3 = tr.insertCell(); c3.textContent = r.secs != null ? r.secs.toFixed(1) + ' s' : ''; c3.className = 'r num';
    const cls = r.outcome.startsWith('ok') ? 'ok' : r.outcome === 'in flight' ? 'flash live' : r.outcome === 'no end logged' ? '' : 'err';
    const c4 = tr.insertCell(); c4.append(el('span', 'chip ' + cls, r.outcome));
    if (r.detail){ const dv = el('div', 'num', r.detail); dv.style.cssText = 'font-size:10.5px;color:var(--mut);margin-top:3px'; c4.append(dv); }
  });
  const inflight = rows.filter(r => r.outcome === 'in flight').length;
  setChip('feedchip', inflight ? inflight + ' in flight' : rows.length ? 'idle' : 'no request yet', inflight ? 'flash live' : '');
  if (!rows.length){ const tr = tb.insertRow(); const c = tr.insertCell(); c.colSpan = 6; c.className = 'empty'; c.textContent = 'no request has gone through the proxy yet (agent clients use :30001)'; }
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
  setText('repodirty', d.dirty ? 'modified (uncommitted changes)' : 'clean');
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
  return Object.entries(eng).find(([n, e]) => e.state !== 'stopped' && e.state !== 'failed') || null;
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
  if (e.state === 'ready' || e.state === 'degraded'){
    sub = (F.pool ? `pool ${Math.round(100 * (l.num_tokens || 0) / F.pool)} %` : '') + ((l.num_reqs || 0) ? ` · ${l.num_reqs} running` : '') + (e.elapsed ? ` · up ${fmtDur(e.elapsed)}` : '');
  } else if (TRANSITIONAL.has(e.state)){
    const eta = e.eta && e.elapsed ? Math.max(0, e.eta - e.elapsed) : null;
    sub = `${fmtDur(e.elapsed)} elapsed` + (eta != null ? ` · about ${fmtDur(eta)} left` : ' · first boot, learning the duration');
  } else if (e.state === 'stopping'){
    sub = `${e.state_elapsed != null ? fmtDur(e.state_elapsed) : ''} elapsed`;
  }
  setText('lanesub', sub);
}
function bootBlock(e){
  const doneN = (e.stage_done || []).length, stage = ALL_STAGES[doneN] || 'init';
  const pct = e.eta && e.elapsed ? Math.min(97, 100 * e.elapsed / e.eta) : Math.min(95, 8 + doneN * (84 / ALL_STAGES.length));
  const eta = e.eta && e.elapsed ? Math.max(0, e.eta - e.elapsed) : null;
  const boot = el('div', 'boot');
  const bar = el('div', 'bbar'); const fill = el('div', 'bfill'); fill.style.width = pct.toFixed(1) + '%'; bar.append(fill); boot.append(bar);
  const stages = el('div', 'stages');
  ALL_STAGES.forEach((st, i) => stages.append(el('span', 'stage ' + (i < doneN ? 'done' : st === stage ? 'now' : ''), STAGE_LABEL[st])));
  boot.append(stages);
  const lab = el('div', 'blab');
  lab.append(el('span', null, `${STATE_LABEL[e.state] || stage} · ${fmtDur(e.elapsed)} elapsed`));
  lab.append(el('span', null, eta != null ? `about ${fmtDur(eta)} left (median of ${(e.boots || []).length} boots)` : 'first boot: learning the duration'));
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
  lab.append(el('span', null, 'systemd stops the container (SIGTERM, usually under 30 s)'));
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
    const on = e.state !== 'stopped' && e.state !== 'failed';
    const warns = [];
    if (on && TRANSITIONAL.has(e.state) && name.includes('flash')) warns.push('stopping the flash lane mid-boot marks the PLE table dirty: the NEXT boot rebuilds it (about 12 min)');
    if (on && e.state === 'ready') warns.push('clients on :30001 get "engine unavailable" until an engine is back (about 9 min after a start)');
    askAction('unit', {verb: on ? 'stop' : 'start', unit: name}, ['sudo', '-n', '/usr/bin/systemctl', on ? 'stop' : 'start', name], warns);
  });
  CARDS.set(name, c); $('enginelist').append(root);
  return c;
}
function rLifecycle(d){
  F.life = d; syncSelector(); rLanePill(); renderLaneAction();
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
    const on = e.state !== 'stopped' && e.state !== 'failed';
    const blocked = !on && (d.blocked || {})[`unit:start:${name}`];
    c.btn.textContent = on ? (e.state === 'stopping' ? 'stopping…' : 'stop') : 'start';
    c.btn.className = 'btn mini ' + (on ? 'danger' : 'low');
    c.btn.dataset.verb = on ? 'stop' : 'start';
    c.btn.dataset.blocked = blocked ? blocked[0] : '';
    c.btn.disabled = e.state === 'stopping' || !!blocked;
    c.why.textContent = blocked ? 'start blocked: ' + blocked[0] : (e.state === 'failed' ? 'the unit failed: read its journal in the Logs tab, then start it again' : '');
    c.why.className = 'why' + (blocked || e.state === 'failed' ? ' warn' : '');
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
    if (sig !== c.sig || sig !== 'none'){ clear(c.extra); if (sig === 'boot') c.extra.append(bootBlock(e)); if (sig === 'stop') c.extra.append(stoppingBlock(e)); c.sig = sig; }
  });
  // overview lane card: the serving engine's card, mirrored
  const ov = $('ovlane'); clear(ov);
  const s = servingEngine();
  // engine facts belong to a lane that is actually up: while it boots, stops or is gone,
  // say so rather than showing the previous lane's model, pool and percentages
  if (!servingReady()) rEngineInfoDown(s ? 'engine ' + (STATE_LABEL[s[1].state] || s[1].state) : 'no engine running');
  if (!s){
    const p = el('p', 'empty', 'No engine is serving. Start the enabled lane from the action bar (about 9 minutes to ready), or switch the target first.');
    ov.append(p);
  } else {
    const [name, e] = s; const row = el('div', 'row');
    row.append(el('span', 'chip ' + stateChipCls(e.state), STATE_LABEL[e.state] || e.state), el('span', 'name', laneLabel(name)),
               el('span', 'chip ' + laneCls(name), name.replace('.service', '')), el('span', 'since', e.state === 'ready' && e.elapsed ? 'up ' + fmtDur(e.elapsed) : ''));
    ov.append(row);
    if (TRANSITIONAL.has(e.state)) ov.append(bootBlock(e));
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
  const bad = states.find(st => st === 'wedged' || st === 'failed' || st === 'degraded');
  const trans = states.find(st => TRANSITIONAL.has(st) || st === 'stopping');
  badge('engines', bad ? (STATE_BADGE[bad] || 'check') : trans ? 'booting' : '', bad ? 'err' : trans ? 'warn' : '');
  applyBusy();
}
function syncSelector(){
  // show the target of the lane that is serving, booting, or enabled: never just the
  // first option, which read as "stock" through nine minutes of an FP8 boot
  const sel = $('switchsel');
  if (!sel || sel.dataset.touched) return;
  const eng = (F.life || {}).engines || {};
  const s = servingEngine();
  const unit = s ? s[0] : enabledUnit();
  const target = (s && F.target) || (eng[unit] || {}).target;
  if (target && sel.value !== target) sel.value = target;
}
function badge(tab, txt, cls){ const b = $('bdg-' + tab); if (b){ b.textContent = txt; b.className = 'bdg ' + (cls || ''); } }

// ── jobs: the strip everybody sees, the history, the busy lock in the UI ─────
const JOBLINES = {id: null, lines: []};
// Same vocabulary as the server's event feed: an action reads as a sentence, never
// as the parameter dict that happens to be its wire format.
const ACTION_PHRASE = {
  unit: p => `${p.verb || 'act on'} ${String(p.unit || '').replace('.service', '')}`,
  switch: p => `switch the 27B lane to ${TARGET_SHORT[p.target] || p.target || ''}`,
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
  const engineUp = servingReady();
  const noEngineWhy = engineUp ? '' : 'no engine is serving: start one first (it answers in about 9 minutes)';
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
    b.title = why || (bl ? bl[0] : `Start ${laneLabel(name)} (systemctl start ${name}, about 9 minutes to ready)`);
    b.onclick = () => askAction('unit', {verb: 'start', unit: name}, ['sudo', '-n', '/usr/bin/systemctl', 'start', name], []);
  }
}

// ── apply: freshness, banners, isolation ──────────────────────────────────────
const RENDER = {machine: rMachine, gpu: rGpu, engine_info: rEngineInfo, engine_fast: rEngineFast, decode: rDecode,
                canary: rCanary, kernel: rKernel, units: rUnits, containers: rContainers, repo: rRepo,
                lifecycle: rLifecycle, feed: rFeed, opencode: rOpencode, config: rConfig, job: rJob};
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
      if (name === 'engine_info' && !servingReady()) rEngineInfoDown();
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
    if (e.state === 'failed') add('err', `${LANE_NAME[n] || n} failed.`, 'systemd reports the unit failed. Read its journal in the Logs tab, then start it again from the action bar.');
    if (e.state === 'degraded') add('warn', `${LANE_NAME[n] || n} stopped answering.`, 'It was serving; health probes retry every 2 s. If it stays here, the Logs tab tells why.');
  });
  if (F.memFloor && F.memFloor.aborts && F.memFloor.last_abort && Date.now() / 1000 - F.memFloor.last_abort < 600)
    add('warn', 'Memory floor fired.', `Host memory fell under ${F.memFloor.gib} GiB with requests running: every generation was aborted ${fmtDur(Date.now() / 1000 - F.memFloor.last_abort)} ago to keep the box out of a livelock.`);
  const ocf = F.ocfit;
  if (ocf && !ocf.ok) add('warn', 'opencode asks for more than this engine can hold.',
    `${ocf.why} (${fmtN(ocf.worst)} asked, ${fmtN(ocf.usable)} servable on ${ocf.served}): the session would break mid-conversation when the proxy refuses the prompt. Setup tab, "Fit the limits to this engine".`);
  ((F.life || {}).orphans || []).forEach(o => add('warn',
    `${LANE_NAME[o.unit] || o.unit} is running outside systemd.`,
    `The container ${o.container} is serving${o.image ? ` from ${o.image}` : ''}, but its unit is stopped, so the buttons here cannot manage it and a reboot will not bring it back. Stop it from a terminal (docker rm -f ${o.container}) and start the unit instead.`));
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
  if (NEEDS_ENGINE.has(name) && !servingReady()){ toast('No engine is serving: start one first, then this action has something to talk to.', 'warn'); return; }
  if (F.job && F.job.current){ toast(`Another action is running (${F.job.current.action}). Wait for the job strip to finish.`, 'warn'); return; }
  if (!$('modal').hidden) return;
  const TARGET_NAME = {stock: 'stock 27B (NVFP4)', uncensored: 'uncensored 27B (NVFP4)',
                       fp8: 'FP8 27B (Qwen official)', 'uncensored-fp8': 'FP8 27B abliterated',
                       flash: 'flash 176B'};
  const TARGET_NOTE = {
    fp8: 'Qwen\u2019s own FP8 release: the most faithful weights of this lane, and the heaviest. '
       + '30.9 GB against 21 GB for NVFP4, and SGLang takes that out of the KV pool: expect around '
       + '200,000 fewer tokens of context and a slower decode, because this box is bandwidth bound. '
       + 'The first switch downloads about 31 GB.',
    stock: 'The NVFP4 quantization this repo pins by default: smallest and fastest of the 27B targets.',
    uncensored: 'The abliterated NVFP4 checkpoint: same size and speed as stock, refusals removed.',
    'uncensored-fp8': 'The abliterated weights in Qwen\u2019s own FP8 format: the refusals of the FP8 target removed, '
       + 'at the same 30.9 GB and the same cost in pool and speed. The first switch downloads about 31 GB.',
    flash: 'The 176B Flash-Next lane. It has its own unit, its own image and its own 48 GB PLE table.'};
  const TITLES = {unit: p => `${p.verb} ${laneLabel(p.unit)}`,
                  switch: p => `switch the target model to ${TARGET_NAME[p.target] || p.target}`,
                  flush_cache: () => 'flush the engine cache', abort_all: () => 'abort every in-flight generation', smoke: () => 'run a smoke generation through the proxy', diag_bundle: () => 'write a diagnostics bundle'};
  const EXPLAIN = {unit: p => p.verb === 'stop' ? 'systemd stops the unit; the container gets SIGTERM and disappears in seconds.' : 'systemd starts the unit; the engine loads its weights and is ready in about 9 minutes (watch the boot bar).',
                   switch: p => (TARGET_NOTE[p.target] ? TARGET_NOTE[p.target] + '\n\n' : '')
                     + 'switch-model.sh rewrites the unit for the chosen target, updates the boot enablement, the proxy ceiling and the opencode default model. It never restarts anything: stop and start the engines afterwards.',
                   flush_cache: () => 'Empties the radix cache. Harmless; refused by the engine if requests are running.',
                   abort_all: () => 'Every running or queued generation ends now; the clients see their stream end.',
                   smoke: () => 'One real 200-token generation through the proxy, the way a client uses it (up to a few minutes while a boot finishes).',
                   diag_bundle: () => 'Collects logs, state and versions into a tarball in your home; the API key is masked everywhere.',
                   fit_opencode: () => 'Reads the KV pool of the engine that is serving right now and rewrites only opencode\u2019s context and output limits so a conversation can never outgrow it. Your other opencode settings, providers and the default model are untouched, and a dated backup is written first. Restart opencode to pick the new limits up.'};
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
    if (r.status === 202){ closeModal(); toast(`${name} started` + (out.dry_run ? ' (dry run)' : '') + '. Follow it in the strip under the top bar.', 'ok'); stripPinned = false; return; }
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
    [v === true ? k : v === false ? k + ' missing' : k + ' n/a', v === true ? 'ok' : v === false ? 'err' : '']);
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
    setText('upline', rel.latest ? `repo release: local ${rel.local}, latest published ${rel.latest}` + (rel.latest === rel.local ? ' (up to date)' : ' (update available)') : 'release check offline (no network or GitHub unreachable)');
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
