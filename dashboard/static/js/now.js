"use strict";
/* Now: the one screen that answers "what is the box doing?".

   The hero is the unified pool, drawn at the machine's own scale and split live by what
   holds it. The rack under it shows the four lanes as bays, one seated. Then what the
   seated lane is doing, what happened lately, and the vitals of the machine. */

const TOTAL_FALLBACK = 121.6 * GIB;
const mibOf = s => { const m = String(s || '').match(/([\d.]+)\s*MiB/); return m ? parseFloat(m[1]) * 1024 * 1024 : 0; };

// The pool, split so the parts add up to the whole:
//   lane   the serving engine's GPU allocations (not reclaimable)
//   other  everything else in use (the OS, the desktop, the cockpit, tools)
//   cache  page cache: reclaimable, and where a diffusion lane's mapped weights live
//   free   memory nobody holds
function poolParts(){
  const m = (F.machine || {}).mem || {};
  const total = m.MemTotal || TOTAL_FALLBACK;
  const avail = m.MemAvailable != null ? m.MemAvailable : null;
  if (avail == null) return null;
  const gpuBytes = ((F.gpu || {}).procs || []).reduce((a, p) => a + mibOf(p.mem), 0);
  const inUse = Math.max(0, total - avail);
  const lane = Math.min(gpuBytes, inUse);
  const cache = Math.min(m.Cached || 0, avail);
  const free = Math.max(0, avail - cache);
  const other = Math.max(0, inUse - lane);
  return {total, avail, inUse, lane, cache, free, other, swap: (m.SwapTotal || 0) - (m.SwapFree || 0)};
}
const FLOOR = () => ((F.memFloor || {}).gib || 3) * GIB;

function renderPool(){
  const p = poolParts(); if (!p) return;
  const s = servingEngine();
  const laneName = s ? laneLabel(s[0]) : null;
  setText('pool-used', (p.inUse / GIB).toFixed(1));
  setText('pool-of', `of ${(p.total / GIB).toFixed(1)} GiB in use`);
  setText('pool-free', (p.avail / GIB).toFixed(1));
  const room = p.avail - FLOOR();
  setText('pool-free-k', room > 0 ? `GiB available, ${(room / GIB).toFixed(1)} above the ${(FLOOR() / GIB).toFixed(0)} GiB floor` : 'GiB available: under the floor');
  $('pool-freebox').className = 'pool-free' + (p.avail < 4 * GIB ? ' crit' : p.avail < 9 * GIB ? ' low' : '');
  // the sentence under the number says what holds the pool, in words
  let sub = '';
  if (s && (s[1].state === 'ready' || s[1].state === 'degraded')){
    sub = s[0] === VIDEO_UNIT ? `${laneName} maps its weights from the drive and streams them at every step: they sit in the page cache, and its working memory grows with the length and size of the video.`
      : s[0] === IMAGE_UNIT ? `${laneName} holds its encoder, DiT and VAE; each image adds its working memory while it is made.`
      : `${laneName} holds its weights and its KV pool: ${fmtGiB(p.lane)} on the GPU side of the pool.`;
  } else if (s && TRANSITIONAL.has(s[1].state)) sub = `${laneName} is loading: the pool fills as it boots.`;
  else if (s && s[1].state === 'stopping') sub = `${laneName} is stopping: its memory comes back in a moment.`;
  else sub = 'No lane holds the pool. Load one from the rack below.';
  setText('pool-sub', sub);
  // the reservoir: segments in proportion, labels only where they fit
  const parts = [['seg-lane', p.lane, laneName ? `${LANE_NAME[s[0]]} ${fmtGiB(p.lane, 0)}` : ''],
                 ['seg-other', p.other, `system ${fmtGiB(p.other, 0)}`], ['seg-cache', p.cache, `cache ${fmtGiB(p.cache, 0)}`],
                 ['seg-free', p.free, `free ${fmtGiB(p.free, 0)}`]];
  parts.forEach(([id, v, lab]) => {
    const e = $(id); const pct = 100 * v / p.total;
    e.style.flexBasis = pct.toFixed(2) + '%'; e.style.flexGrow = '0'; e.style.flexShrink = '0';
    const span = e.firstChild; setText(span, pct > 9 ? lab : '');
    e.title = lab;
  });
  $('seg-lane').classList.toggle('filling', !!(s && TRANSITIONAL.has(s[1].state)));
  $('floor').style.width = (100 * FLOOR() / p.total).toFixed(2) + '%';
  $('ticks').style.setProperty('--tick', (100 * 16 * GIB / p.total).toFixed(3) + '%');
  const scale = $('pool-scale');
  if (!scale.dataset.total || scale.dataset.total !== String(Math.round(p.total))){
    scale.dataset.total = String(Math.round(p.total)); clear(scale);
    [0, 32, 64, 96].forEach(g => scale.append(el('span', null, g ? g + ' GiB' : '0')));
    scale.append(el('span', null, (p.total / GIB).toFixed(1) + ' GiB'));
  }
  $('reservoir').setAttribute('aria-label', `Unified memory: ${fmtGiB(p.inUse)} in use of ${fmtGiB(p.total)}, ${fmtGiB(p.avail)} available`);
  const lg = $('pool-legend'); clear(lg);
  const item = (color, label, v) => { const sp = el('span'); const i = el('i'); i.style.background = color; sp.append(i, label + ' '); sp.append(el('b', null, v)); lg.append(sp); };
  item('var(--gold)', laneName ? `${laneName}, GPU allocations` : 'lane, GPU allocations', fmtGiB(p.lane));
  item('var(--slate)', 'System and apps', fmtGiB(p.other));
  item('var(--steel)', 'Page cache', fmtGiB(p.cache));
  item('color-mix(in srgb, var(--cool) 45%, transparent)', 'Free', fmtGiB(p.free));
  if (p.swap > 0.05 * GIB) item('var(--ember)', 'Swap in use', fmtGiB(p.swap));
  // the spine's gauge
  setText('minipool-v', `${(p.avail / GIB).toFixed(1)} GiB free`);
  $('mp-used').style.width = (100 * p.inUse / p.total).toFixed(1) + '%';
  $('mp-cache').style.width = (100 * p.cache / p.total).toFixed(1) + '%';
  $('mp-free').style.width = (100 * p.free / p.total).toFixed(1) + '%';
}

// ── the rack: four bays ───────────────────────────────────────────────────────
const BAYS = new Map();
function bay(unit){
  let b = BAYS.get(unit); if (b) return b;
  const root = el('button', 'bay'); root.type = 'button'; root.setAttribute('role', 'listitem');
  const top = el('div', 'top'); const state = el('span', 'cap'); const kind = el('span', 'kind', LANE_META[unit].kind);
  top.append(kind, state);
  const h = el('h3', null, LANE_META[unit].title);
  const desc = el('p', 'desc', LANE_META[unit].desc);
  const boot = el('div', 'bootbar'); boot.hidden = true;
  const line = el('div', 'facts-line');
  const foot = el('div', 'foot'); const act = el('span', 'act'); const note = el('span', 'faint'); note.style.fontSize = '12px';
  foot.append(note, act);
  root.append(top, h, desc, boot, line, foot);
  b = {root, state, h, desc, boot, line, act, note, sig: ''};
  root.addEventListener('click', () => {
    const e = engines()[unit];
    if (!installed(unit)) return toast(`${LANE_NAME[unit]} is not installed on this box. Install it on the box: ${LANE_INSTALL[unit]}`, 'warn', 8000);
    const s = servingEngine();
    if (s && s[0] === unit && e && (e.state === 'ready' || e.state === 'degraded')) return showView(LANE_META[unit].view);
    if (s && s[0] === unit) return showView('lanes');
    askJourney(laneTarget(unit) || LANE_TARGETS[unit][0]);
  });
  BAYS.set(unit, b); $('rack').append(root);
  return b;
}
function renderRack(){
  const s = servingEngine();
  LANE_UNITS.forEach(unit => {
    const b = bay(unit), e = engines()[unit], has = installed(unit);
    const seated = !!(s && s[0] === unit);
    const st = e ? e.state : 'absent';
    b.root.className = 'bay' + (seated && (st === 'ready' || st === 'degraded') ? ' seated' : '') + (TRANSITIONAL.has(st) || st === 'stopping' ? ' booting' : '')
      + (st === 'failed' || st === 'wedged' || st === 'orphan' ? ' broken' : '') + (!has ? ' absent' : '');
    cap(b.state, !has ? 'not installed' : stateLabel(e), !has ? '' : stateKind(st), !has ? false : stateLive(st));
    const t = laneTarget(unit);
    setText(b.h, LANE_META[unit].title + (t && TARGET_SHORT[t] && !['image', 'video', 'flash'].includes(t) ? ` ${TARGET_SHORT[t]}` : ''));
    // the boot bar, when it boots
    if (e && TRANSITIONAL.has(st)){
      b.boot.hidden = false; clear(b.boot);
      const eta = e.eta || READY_DEFAULT[unit], pct = eta && e.elapsed ? Math.min(97, 100 * e.elapsed / eta) : 8;
      const m = el('div', 'meter'); const i = el('i'); i.style.width = pct.toFixed(1) + '%'; m.append(i); b.boot.append(m);
      b.boot.append(el('span', 'faint', e.held ? `waiting for GPU memory: ${e.held}` : `${STATE_LABEL[st]}${e.detail && e.detail !== 'ready' ? ', ' + e.detail : ''}, ${fmtDur(e.elapsed)} of about ${fmtDur(eta)}`));
    } else if (e && st === 'stopping'){
      b.boot.hidden = false; clear(b.boot);
      const m = el('div', 'meter indet'); m.append(el('i')); b.boot.append(m);
    } else b.boot.hidden = true;
    // facts: what this lane costs, measured on this box
    clear(b.line);
    const fact = (k, v) => { const sp = el('span'); sp.append(k + ' '); sp.append(el('b', null, v)); b.line.append(sp); };
    if (e) fact('boot', fmtMin(bootSeconds(unit)));
    const pool = e ? lanePool(unit, e) : null;
    if (pool) fact('KV pool', fmtK(pool));
    if (seated && e && e.elapsed && (st === 'ready' || st === 'degraded')) fact('up', fmtDur(e.elapsed));
    if (unit === VIDEO_UNIT) fact('4 s clip', 'about 11 min');
    if (unit === IMAGE_UNIT) fact('1024 image', laneTarget(IMAGE_UNIT) === 'image-turbo' ? 'about 7.5 s' : 'about 34 s');
    const en = (F.units[unit] || {}).enabled;
    setText(b.note, !has ? LANE_INSTALL[unit] : en === 'enabled' ? 'starts at boot' : en === '?' ? '' : 'manual start');
    setText(b.act, !has ? 'Not installed' : seated ? (st === 'ready' || st === 'degraded' ? `Open ${LANE_META[unit].view === 'agent' ? 'the agent' : LANE_META[unit].view}` : 'Details') : 'Load this lane');
    b.root.setAttribute('aria-label', `${LANE_META[unit].title}: ${!has ? 'not installed' : stateLabel(e)}. ${b.act.textContent}.`);
  });
}

// ── the spine's serving capsule and the lede ──────────────────────────────────
function renderServing(){
  const s = servingEngine();
  if (!s){
    $('serving-lamp').className = 'lamp';
    setText('serving-name', 'Nothing is serving');
    const u = bootUnit();
    setText('serving-sub', !F.units || !Object.keys(F.units).length ? '' : u ? `${LANE_NAME[u]} starts at boot; load a lane from Lanes`
      : bootKnown() ? 'no lane starts at boot; load one from Lanes' : 'load a lane from Lanes');
    setText('now-lede', 'No lane holds the box right now. Pick one in the rack to load it.');
    sayOnce('lane', 'nothing serving');
    return;
  }
  const [unit, e] = s, st = e.state;
  $('serving-lamp').className = 'lamp ' + (stateKind(st) || '') + (stateLive(st) === 'live' ? ' live' : '');
  setText('serving-name', `${laneLabel(unit)}, ${stateLabel(e)}`);
  let sub = '';
  const l = F.load || {};
  if ((st === 'ready' || st === 'degraded') && unit === VIDEO_UNIT) sub = VID_NOW.label || 'idle, one video at a time';
  else if ((st === 'ready' || st === 'degraded') && unit === IMAGE_UNIT) sub = IMG_NOW.label || 'idle, one image at a time';
  else if (st === 'ready' || st === 'degraded') sub = (loadWords(l) || 'idle') + (F.pool ? `, KV pool ${Math.round(100 * physTokens(l) / F.pool)} %` : '') + (e.elapsed ? `, up ${fmtDur(e.elapsed)}` : '');
  else if (TRANSITIONAL.has(st)){ const eta = e.eta || READY_DEFAULT[unit]; sub = `${fmtDur(e.elapsed)} elapsed` + (eta && e.elapsed ? `, about ${fmtDur(Math.max(0, eta - e.elapsed))} left` : ''); }
  else if (st === 'stopping') sub = `${e.state_elapsed != null ? fmtDur(e.state_elapsed) : ''} elapsed`;
  setText('serving-sub', sub);
  sayOnce('lane', `${laneLabel(unit)}: ${stateLabel(e)}`);
  // the lede: one sentence about the box, in words
  let lede;
  if (e.zombie) lede = `${laneLabel(unit)} lost its scheduler: its server answers nothing.`;
  else if (e.held) lede = `${laneLabel(unit)} is not up yet: it waits for the last engine's GPU memory to come back.`;
  else if (st === 'ready' || st === 'degraded') lede = `${laneLabel(unit)} holds the box` + (e.elapsed ? ` and has served for ${fmtDur(e.elapsed)}` : '') + '. ' +
    (unit === VIDEO_UNIT ? (VID_NOW.label ? `It is ${VID_NOW.label}.` : 'It is idle.') : unit === IMAGE_UNIT ? (IMG_NOW.label ? `It is ${IMG_NOW.label}.` : 'It is idle.')
      : (runningReqs(l) ? `${runningReqs(l)} request${runningReqs(l) > 1 ? 's are' : ' is'} running` : 'No request is running')
        + (waitingReqs(l) ? `, ${waitingReqs(l)} waiting.` : '.'));
  else if (TRANSITIONAL.has(st)) lede = `${laneLabel(unit)} is booting: ${STATE_LABEL[st]}.`;
  else if (st === 'stopping') lede = `${laneLabel(unit)} is stopping.`;
  else lede = `${laneLabel(unit)} is ${STATE_LABEL[st] || st}.`;
  setText('now-lede', lede);
}

// ── activity: what the seated lane is doing ───────────────────────────────────
function renderActivity(){
  const s = servingEngine(), fx = $('act-facts');
  const l = F.load || {};
  if (!s){
    setText('act-title', 'Activity'); setText('act-v', 'Idle'); setText('act-k', 'no lane is loaded');
    const u = bootUnit();
    facts(fx, u ? [['Boot lane', LANE_NAME[u]], ['Loads in', readyIn(u)]] : [['Boot lane', bootKnown() ? 'none' : 'unknown']]); return;
  }
  const [unit, e] = s;
  if (TRANSITIONAL.has(e.state)){
    const eta = e.eta || READY_DEFAULT[unit], pct = eta && e.elapsed ? Math.min(97, Math.round(100 * e.elapsed / eta)) : null;
    setText('act-title', `Booting ${laneLabel(unit)}`); setText('act-v', pct != null ? pct + ' %' : '…'); setText('act-k', STATE_LABEL[e.state] + (e.detail && e.detail !== 'ready' ? `, ${e.detail}` : ''));
    const stages = e.stages && e.stages.length ? e.stages : ALL_STAGES;
    facts(fx, [['Stages done', `${(e.stage_done || []).length} of ${stages.length}`], ['Elapsed', fmtDur(e.elapsed)],
               ['Left, about', eta && e.elapsed ? fmtDur(Math.max(0, eta - e.elapsed)) : 'first boot, learning'],
               ['Timed from', e.eta ? `the median of ${(e.boots || []).length} boots here` : 'the reference box'],
               e.overdue ? ['Note', 'over twice the usual time: read Logs', 'warn-t'] : [null, null]]);
    return;
  }
  if (unit === VIDEO_UNIT){
    setText('act-title', 'Video');
    const r = VID_NOW.run || {};
    if (r.phase === 'denoise' && r.step != null){ setText('act-v', `${r.step} of ${r.steps}`); setText('act-k', `denoising steps, ${r.s_per_step ? r.s_per_step.toFixed(1) + ' s each' : ''}`); }
    else if (r.phase){ setText('act-v', r.phase === 'encode' ? 'Prompt' : 'Decoding'); setText('act-k', r.label); }
    else { setText('act-v', 'Idle'); setText('act-k', 'one video at a time on this lane'); }
    facts(fx, [['Left, about', r.left_s != null ? fmtDur(r.left_s + (r.phase === 'denoise' ? 25 : 0)) : null], ['Up', e.elapsed ? fmtDur(e.elapsed) : null],
               ['Listens on', `127.0.0.1:${VID_NOW.port || 30022}, behind this cockpit`]]);
    if (r.phase === 'denoise' && r.steps) push('act', 100 * r.step / r.steps, 120);
    drawSpark($('act-spark'), 'act', cssVar('--gold'), 100, {empty: 'no video yet in this window'});
    return;
  }
  if (unit === IMAGE_UNIT){
    setText('act-title', 'Images'); setText('act-v', IMG_NOW.label ? 'Busy' : 'Idle'); setText('act-k', IMG_NOW.label || 'one image at a time on this lane');
    facts(fx, [['Up', e.elapsed ? fmtDur(e.elapsed) : null], ['Listens on', '127.0.0.1:30020, behind this cockpit']]);
    return;
  }
  // a text lane
  const n = runningReqs(l), w = waitingReqs(l);
  setText('act-title', 'Requests'); setText('act-v', String(n)); setText('act-k', (n ? 'running' : 'nothing running') + (n || w ? `, ${w} waiting` : ''));
  push('act', n, 180); drawSpark($('act-spark'), 'act', cssVar('--gold'), Math.max(4, F.maxRun || 4), {empty: 'quiet so far'});
  const held = physTokens(l);
  facts(fx, [['KV pool held', F.pool ? `${fmtN(held)} of ${fmtN(F.pool)} tokens` : 'waiting for the engine'],
             ['One prompt, at most', singleLimit() ? `about ${fmtK(singleLimit())} tokens` : null],
             ['Generation probe', F.canaryTxt || null], ['Up', e.elapsed ? fmtDur(e.elapsed) : null]]);
}
on('canary', d => {
  let txt;
  if (d.skipped && !textReady()) txt = imageServing() ? 'no text engine (the image lane serves)' : videoServing() ? 'no text engine (the video lane serves)' : 'no text engine';
  else if (d.skipped && d.last_ok == null) txt = 'not yet run';
  else if (d.fails > 0) txt = `${d.fails} failure${d.fails > 1 ? 's' : ''} in a row: ${d.last_err || ''}`;
  else if (d.last_ok) txt = `ok, ${d.latency} s` + (d.skipped ? ` (skipped this round: ${d.why || 'busy'})` : '');
  else txt = 'idle';
  F.canaryTxt = txt; F.canary = d;
});

// ── the timeline: events, a row kept once drawn ───────────────────────────────
const EVT_KIND = {state: 'gold', job: 'cool', kernel: 'ember', guard: 'ember', belt: 'clay', error: 'clay'};
function evtDot(ev){
  if (/failed|wedged|crash|killed/.test(ev.msg)) return 'clay';
  if (/→ ready|ready$/.test(ev.msg)) return 'gold';
  return EVT_KIND[ev.kind] || '';
}
// Consecutive refusals of the GPU driver are one story, told once with their count and
// their span: listed one by one they bury every state change under them.
function foldEvents(evs){
  const out = [];
  evs.forEach(ev => {
    const m = ev.kind === 'kernel' && ev.msg.match(/refused (\d+) allocation/);
    const last = out[out.length - 1];
    if (m && last && last.fold){ last.n += Number(m[1]); last.first = ev.ts; last.count++; return; }
    out.push(m ? {ts: ev.ts, first: ev.ts, kind: 'kernel', fold: true, n: Number(m[1]), count: 1, msg: ev.msg} : ev);
  });
  return out.map(e => e.fold && e.count === 1 ? {ts: e.ts, kind: 'kernel', msg: `GPU driver refused ${e.n} allocation${e.n > 1 ? 's' : ''} at the edge of memory`} : e.fold && e.count > 1 ? {ts: e.ts, kind: 'kernel', msg: `GPU driver refused ${e.n} allocations at the edge of memory, ${clockTime(e.first)} to ${clockTime(e.ts)}`, key: 'fold' + e.first} : e);
}
function renderEvents(box, n){
  if (!box) return;
  const evs = foldEvents(((F.life || {}).events || []).slice().reverse()).slice(0, n);
  if (!evs.length){ if (!box.querySelector('.none')){ clear(box); box.append(el('li', 'none faint', 'No event yet in this cockpit session.')); } return; }
  box.querySelectorAll('.none').forEach(x => x.remove());
  const seen = {}; const keys = evs.map(ev => { const k = ev.key || `${ev.ts}|${ev.kind}|${ev.msg}`; seen[k] = (seen[k] || 0) + 1; return k + '#' + seen[k]; });
  const have = new Map([...box.children].map(r => [r.dataset.ev, r]));
  have.forEach((r, k) => { if (!keys.includes(k)) r.remove(); });
  evs.forEach((ev, i) => {
    let row = have.get(keys[i]);
    if (!row){
      row = el('li'); row.dataset.ev = keys[i];
      row.append(el('time'), el('span', 'dot ' + evtDot(ev)), el('span', 'm'));
    }
    const t = clockTime(ev.ts); if (row.firstChild.textContent !== t) row.firstChild.textContent = t;
    setText(row.lastChild, ev.msg.replace(/^qwen38-/, '').replace(' \u2192 ', ' to '));
    if (box.children[i] !== row) box.insertBefore(row, box.children[i] || null);
  });
}

// ── vitals ────────────────────────────────────────────────────────────────────
function vital(id, v, s, kind){ const e = $(id); if (!e) return; setText(e.children[1], v); setText(e.children[2], s || ''); e.className = 'vital' + (kind ? ' ' + kind : ''); }
// '…' until the collector first answers; after that, a reading it could not take is 'n/a' (the
// Machine view's word), and the line under it says nothing that was not read: with
// nvidia-smi silent, the GPU vitals read '…' for good, the temperature "cool" and the
// processes "0" (found by the monkey check, 2026-10-02)
const unread = src => src ? 'n/a' : '…';
function renderVitals(){
  const m = F.machine || {}, g = F.gpu || {};
  const cpu = (m.cpu_pct || {}).cpu;
  vital('vt-cpu', cpu != null ? cpu.toFixed(0) + ' %' : unread(F.machine), (m.load || []).length ? `load ${m.load[0].toFixed(2)}` : '', cpu > 85 ? 'warn' : '');
  const procs = Array.isArray(g.procs) ? g.procs : null;
  vital('vt-gpu', g.power_w != null ? g.power_w.toFixed(0) + ' W' : unread(F.gpu), procs ? `${procs.length} process${procs.length === 1 ? '' : 'es'}` : '');
  const t = g.temp_c;
  vital('vt-temp', t != null ? t.toFixed(0) + ' °C' : unread(F.gpu), t == null ? '' : t > 85 ? 'hot: give it air' : t > 75 ? 'warm' : 'cool', t > 85 ? 'err' : t > 75 ? 'warn' : '');
  const dk = (m.disks || {}).home;
  vital('vt-disk', dk ? fmtGiB(dk.free, 0) : unread(F.machine), dk ? `of ${fmtGiB(dk.total, 0)}` : '', dk && dk.free < 60 * GIB ? 'warn' : '');
  const mem = m.mem || {}; const sw = (mem.SwapTotal || 0) - (mem.SwapFree || 0);
  vital('vt-swap', mem.SwapTotal != null ? fmtGiB(sw) : unread(F.machine), mem.SwapTotal ? `of ${fmtGiB(mem.SwapTotal, 0)}` : '', sw > 8 * GIB ? 'warn' : '');
  const k = F.kernel || {}, n = k.nvrm_oom_1h;
  vital('vt-nvrm', n != null ? String(n) : unread(F.kernel), n == null ? '' : k.nvrm_last ? `last ${String(k.nvrm_last).slice(11, 16)}` : 'none', n > 200 ? 'warn' : '');
}
on('kernel', d => { F.kernel = d; });

// the facts the spine and the video/image studios share, filled by their own views
const VID_NOW = {label: '', run: null, port: 30022};
const IMG_NOW = {label: ''};

afterApply(() => {
  renderServing(); renderPool(); renderRack(); renderActivity(); renderVitals();
  renderEvents($('now-timeline'), 8);
  const bad = Object.values(engines()).find(e => ['wedged', 'failed', 'degraded', 'orphan'].includes(e.state));
  const trans = Object.values(engines()).find(e => TRANSITIONAL.has(e.state) || e.state === 'stopping');
  badge('now', bad ? '!' : '', bad ? 'err' : '');
  badge('lanes', bad ? bad.state : trans ? (trans.state === 'stopping' ? 'stopping' : 'booting') : '', bad ? 'err' : trans ? 'warn' : '');
});
