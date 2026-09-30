"use strict";
/* Lanes: the four lanes as cards, each with what it serves, what it costs, and the one
   verb that makes sense now. Loading a lane is a journey (base.js, askJourney); stop and
   start stay single actions for when that is all you want. */

const LCARDS = new Map();
function laneCard(unit){
  let c = LCARDS.get(unit); if (c) return c;
  const root = el('section', 'panel'); root.dataset.src = 'lifecycle,units';
  const head = el('header'); const h = el('h2'); const state = el('span', 'cap'); head.append(h, state);
  const grid = el('div', 'grid g-7-5');
  const left = el('div', 'stack'); left.style.gap = 'var(--s-3)';
  const desc = el('p', 'muted');
  const boot = el('div', 'stack'); boot.style.gap = '8px'; boot.hidden = true;
  const why = el('p', 'why-off'); why.hidden = true;
  const actions = el('div', 'btn-row');
  const tsel = el('select', 'input'); tsel.style.maxWidth = '280px'; tsel.setAttribute('aria-label', 'Checkpoint');
  LANE_TARGETS[unit].forEach(t => { const o = el('option', null, TARGET_NAME[t]); o.value = t; tsel.append(o); });
  const load = el('button', 'btn primary'); load.type = 'button';
  const stop = el('button', 'btn danger'); stop.type = 'button';
  const start = el('button', 'btn'); start.type = 'button';
  const logs = el('button', 'btn ghost', 'Journal'); logs.type = 'button';
  if (LANE_TARGETS[unit].length > 1) actions.append(tsel);
  actions.append(load, start, stop, logs);
  left.append(desc, boot, actions, why);
  const right = el('dl', 'facts');
  grid.append(left, right);
  const hist = el('div'); hist.style.marginTop = 'var(--s-4)';
  root.append(head, grid, hist);
  c = {root, h, state, desc, boot, why, tsel, load, stop, start, logs, right, hist, touched: false};
  tsel.addEventListener('change', () => { c.touched = true; renderLanes(); });
  // a choice holds the selector until the lane serves it; a journey not gone through
  // gives the selector back to the lane
  load.addEventListener('click', () => askJourney(c.tsel.value || LANE_TARGETS[unit][0],
    {onClose: () => { if (!(SHEET && SHEET.running)) { c.touched = false; renderLanes(); } }}));
  stop.addEventListener('click', () => {
    const e = engines()[unit] || {};
    const warns = [];
    if (TRANSITIONAL.has(e.state) && unit === UFLASH) warns.push('This boot is thrown away: every flash boot writes its 47.7 GiB table from scratch, so the next start takes a whole boot again (' + readyIn(unit) + ').');
    if (e.state === 'ready') warns.push(unit === IMAGE_UNIT ? 'An image being made right now is lost.' : unit === VIDEO_UNIT ? 'A video being made right now is lost: the runtime cancels it as it stops.'
      : 'Clients on :30001 get "engine unavailable" until an engine answers again.');
    askAction('unit', {verb: 'stop', unit}, warns);
  });
  start.addEventListener('click', () => askAction('unit', {verb: 'start', unit}, []));
  logs.addEventListener('click', () => { LOGS_WANT.src = unit; showView('logs'); });
  LCARDS.set(unit, c); $('lanes-list').append(root);
  return c;
}
function bootChart(box, e, unit){
  clear(box);
  const boots = (e.boots || []);
  if (!boots.length){ box.append(el('p', 'help', 'No boot of this lane seen by the cockpit yet: estimates use the reference box.')); return; }
  const wrap = el('div'); wrap.style.cssText = 'display:flex; align-items:flex-end; gap:6px; height:54px';
  const mx = Math.max(...boots, READY_DEFAULT[unit] || 0);
  boots.forEach(b => { const bar = el('span'); bar.title = fmtDur(b); bar.style.cssText = `flex:0 0 18px; border-radius:4px 4px 2px 2px; height:${Math.max(6, 50 * b / mx).toFixed(0)}px; background:linear-gradient(180deg, var(--gold-hi), var(--gold-lo)); opacity:.85`; wrap.append(bar); });
  const lab = el('p', 'help'); lab.textContent = `Last ${boots.length} boots: ${boots.map(fmtDur).join(', ')}. Median ${fmtDur(bootSeconds(unit))}.`;
  const row = el('div'); row.style.cssText = 'display:flex; gap:var(--s-4); align-items:flex-end; flex-wrap:wrap'; row.append(wrap, lab);
  box.append(row);
  if (e.pools && e.pools.last){
    const seatedNow = (servingEngine() || [])[0] === unit;
    box.append(el('p', 'help', `KV pool ${seatedNow ? 'this' : 'last'} boot: ${fmtN(e.pools.last)} tokens` + (e.pools.n > 1 ? `, between ${fmtN(e.pools.min)} and ${fmtN(e.pools.max)} over ${e.pools.n} boots (${e.pools.spread_pct} % spread). The pool is decided at boot by the memory free at that moment.` : '.')));
  }
}
function renderLanes(){
  const s = servingEngine(), why = busyWhy();
  LANE_UNITS.forEach(unit => {
    const c = laneCard(unit), e = engines()[unit], has = installed(unit);
    const st = e ? e.state : null, seated = !!(s && s[0] === unit);
    setText(c.h, LANE_META[unit].title + (seated ? ', serving' : ''));
    cap(c.state, !has ? 'not installed' : STATE_LABEL[st] || st || 'unknown', has ? stateKind(st) : '', has ? stateLive(st) : false);
    setText(c.desc, LANE_META[unit].desc + ' ' + LANE_META[unit].kind + '.');
    const served = laneTarget(unit);
    if (c.touched && served && served === c.tsel.value && (engines()[unit] || {}).state === 'ready') c.touched = false;
    if (!c.touched && served && c.tsel.value !== served) c.tsel.value = served;
    const target = c.tsel.value || LANE_TARGETS[unit][0];
    const on = !!e && (!UNIT_DOWN.has(st) || !!e.restarting);
    const sameTarget = !laneTarget(unit) || laneTarget(unit) === target;
    // the verbs that make sense now
    c.load.hidden = !has || (seated && sameTarget);
    setText(c.load, seated ? `Load ${TARGET_NAME[target]}` : `Load ${LANE_NAME[unit]}`);
    c.start.hidden = !has || on || !!(s && s[0] !== unit);
    setText(c.start, `Start ${LANE_NAME[unit]} as installed`);
    c.stop.hidden = !has || !on;
    setText(c.stop, st === 'stopping' ? 'Stopping…' : `Stop ${LANE_NAME[unit]}`);
    const blocked = on ? null : blockedFor(`unit:start:${unit}`);
    [c.load, c.start, c.stop].forEach(b => { b.disabled = !!why || st === 'stopping'; b.title = why; });
    c.start.disabled = c.start.disabled || !!blocked;
    c.why.hidden = !(blocked && !c.start.hidden) && !(st === 'failed' || st === 'orphan');
    setText(c.why, st === 'orphan' ? 'Its container runs outside systemd: Start replaces it with the unit’s own.'
      : st === 'failed' ? 'The unit failed: read its journal, then start it again.' : blocked ? blocked[0] : '');
    c.logs.hidden = !has;
    // boot or stop in progress
    if (e && TRANSITIONAL.has(st)){
      c.boot.hidden = false; clear(c.boot);
      const stages = e.stages && e.stages.length ? e.stages : ALL_STAGES, done = (e.stage_done || []).length;
      const eta = e.eta || READY_DEFAULT[unit], pct = eta && e.elapsed ? Math.min(97, 100 * e.elapsed / eta) : 8 + done * 80 / stages.length;
      const m = el('div', 'meter'); const i = el('i'); i.style.width = pct.toFixed(1) + '%'; m.append(i); c.boot.append(m);
      const ph = el('div', 'phases'); stages.forEach((sg, k) => ph.append(el('span', k < done ? 'done' : k === done ? 'now' : '', STAGE_LABEL[sg] || sg))); c.boot.append(ph);
      c.boot.append(el('p', 'help', `${STATE_LABEL[st]}${e.detail && e.detail !== 'ready' ? ' (' + e.detail + ')' : ''}, ${fmtDur(e.elapsed)} elapsed, ` +
        (e.elapsed && eta ? `about ${fmtDur(Math.max(0, eta - e.elapsed))} left` : 'learning the duration') + (e.eta ? ` (median of ${(e.boots || []).length} boots)` : ' (reference box)')));
      if (e.overdue) c.boot.append(el('p', 'help warn-t', 'This boot takes more than twice the usual time: read its journal.'));
    } else if (e && st === 'stopping'){
      c.boot.hidden = false; clear(c.boot);
      const m = el('div', 'meter indet'); m.append(el('i')); c.boot.append(m);
      c.boot.append(el('p', 'help', `stopping, ${e.state_elapsed != null ? fmtDur(e.state_elapsed) : '…'} elapsed`));
    } else c.boot.hidden = true;
    // the right column: the facts
    const en = (F.units[unit] || {}).enabled;
    facts(c.right, [
      ['Checkpoint', e && e.model ? e.model.split('/').pop() : has ? '…' : 'not installed'],
      ['Target', laneTarget(unit) ? TARGET_NAME[laneTarget(unit)] : null],
      ['At boot', has ? (en === 'enabled' ? 'starts' : 'manual start') : null],
      ['Boot takes', has ? readyIn(unit) : null],
      ['Up for', seated && e && e.elapsed && (st === 'ready' || st === 'degraded') ? fmtDur(e.elapsed) : null],
      ['Install', !has ? LANE_INSTALL[unit] : null, 'code']]);
    c.root.classList.toggle('seated-card', seated);
    c.root.style.borderColor = seated && (st === 'ready' || st === 'degraded') ? 'color-mix(in srgb, var(--gold) 45%, transparent)' : '';
    if (e) bootChart(c.hist, e, unit); else clear(c.hist);
  });
}
function renderEngineFacts(){
  const on = textReady();
  show('eng-facts', on);
  const s = servingEngine();
  show('eng-down', !on);
  setText('eng-down', on ? '' : imageServing() ? 'The image lane is serving, so there is no text engine. Its facts are on the Image view.'
    : videoServing() ? 'The video lane is serving, so there is no text engine. Its facts are on the Video view.'
    : s ? `The engine is ${STATE_LABEL[s[1].state] || s[1].state}: these facts arrive from the engine itself, once it answers.`
    : 'No engine is running.');
}
on('engine_info', d => {
  const i = d.info || {};
  setText('eng-model', (i.model_path || '…').split('/').pop()); setTitle('eng-model', i.model_path || '');
  setText('eng-rev', (i.revision || '').slice(0, 12) || 'n/a');
  setText('eng-quant', i.quantization ?? 'n/a');
  setText('eng-ctx', i.context_length ? fmtN(i.context_length) + ' tokens' : '…');
  setText('eng-pool', i.max_total_num_tokens ? fmtN(i.max_total_num_tokens) + ' tokens' : '…');
  setText('eng-single', singleLimit() ? `about ${fmtN(singleLimit())} tokens` + (F.ceiling > 0 ? ' (proxy ceiling)' : '') : '…');
  setText('eng-spec', i.speculative_algorithm ? `${i.speculative_algorithm}, ${i.speculative_num_steps} steps, ${i.speculative_num_draft_tokens} draft tokens` : 'none');
  setText('eng-attn', i.prefill_attention_backend ? `${i.prefill_attention_backend} / ${i.decode_attention_backend}` : (i.attention_backend ?? 'n/a'));
  setText('eng-radix', i.mamba_radix_cache_strategy ?? 'n/a');
  setText('eng-ver', String(i.version ?? 'n/a').split('+')[0]);
});
on('containers', d => {
  const serving = Object.entries(d.containers || {}).find(([, c]) => c.image);
  setText('eng-image', serving ? serving[1].image.replace(/@sha256:([0-9a-f]{12})[0-9a-f]+/, '@$1…') : 'no serving container');
  setTitle('eng-image', serving ? serving[1].image : '');
  setText('px-cont', serving ? serving[0] : 'none (a diffusion lane runs natively)');
});
on('canary', d => {
  setText('px-probe', F.canaryTxt || '…');
  setText('px-last', d.last_ok ? clockTime(d.last_ok) : 'never in this cockpit’s life');
});
function renderProxy(){
  const u = F.units[PROXY_UNIT] || {};
  const onl = u.active === 'active';
  cap('px-state', onl ? 'running' : u.active || 'unknown', onl ? 'ok' : u.active === 'failed' ? 'err' : '');
  const v = F.proxy && F.proxy.version, other = !!v && F.proxy.same_as_repo === false;
  setText('px-ver', v ? v + (other ? ', not the repo’s copy' : ', the repo’s copy') : 'unknown');
  const b = $('px-btn'); setText(b, onl ? 'Stop the proxy' : 'Start the proxy');
  b.className = 'btn sm ' + (onl ? 'danger' : '');
  b.disabled = !!busyWhy(); b.title = busyWhy();
  b.onclick = () => askAction('unit', {verb: onl ? 'stop' : 'start', unit: PROXY_UNIT},
    onl ? ['Agent clients on :30001 lose the proxy until it is back; the engine itself keeps running.'] : []);
}
afterApply(() => { renderLanes(); renderEngineFacts(); renderProxy(); });
on('busy', () => { if (LCARDS.size) renderLanes(); });
