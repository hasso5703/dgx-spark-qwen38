"use strict";
/* Lanes: the lanes as cards, each with what it serves, what it costs, and the one
   verb that makes sense now. Loading a lane is a journey (base.js, askJourney); stop and
   start stay single actions for when that is all you want. */

const LCARDS = new Map();
function laneCard(unit){
  let c = LCARDS.get(unit); if (c) return c;
  const root = el('section', 'panel lane-panel'); root.dataset.src = 'lifecycle,units';
  const head = el('header'); const h = el('h2'); const state = el('span', 'cap');
  const who = el('div', 'who'); who.append(makerMark(unit), h); head.append(who, state);
  const grid = el('div', 'grid g-7-5');
  const left = el('div', 'stack'); left.style.gap = 'var(--s-3)';
  const desc = el('p', 'muted');
  const boot = el('div', 'stack'); boot.style.gap = '8px'; boot.hidden = true;
  const why = el('p', 'why-off'); why.hidden = true;
  const actions = el('div', 'btn-row');
  // The versions a lane can load, all in sight: one button each, the chosen one lit. A select
  // hid them behind one line, and nobody could tell there was a choice (2026-10-11). The choice
  // is the card's own value (c.target).
  const pickBox = el('div', 'pickbox');
  const pickLab = el('p', 'pick-label', `Version (${LANE_TARGETS[unit].length})`);
  const pick = el('div', 'pick'); pick.setAttribute('role', 'radiogroup'); pick.setAttribute('aria-label', `Version of ${LANE_NAME[unit]}`);
  const opts = LANE_TARGETS[unit].map(t => {
    const b = el('button', 'opt', TARGET_SHORT_NAME[t] || TARGET_NAME[t]); b.type = 'button'; b.setAttribute('role', 'radio'); b.dataset.target = t;
    b.title = TARGET_NAME[t];
    b.addEventListener('click', () => { c.target = t; c.touched = true; renderLanes(); });
    return b;
  });
  const pickLine = el('p', 'pick-line');
  pick.append(...opts); pickBox.append(pickLab, pick, pickLine);
  // the arrow keys move the choice, as in any group of radio buttons
  pick.addEventListener('keydown', ev => {
    const step = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[ev.key];
    if (!step || ev.altKey || ev.ctrlKey || ev.metaKey) return;
    ev.preventDefault();
    const i = opts.indexOf(document.activeElement), next = opts[(Math.max(0, i) + step + opts.length) % opts.length];
    next.click(); next.focus();
  });
  const load = el('button', 'btn primary'); load.type = 'button';
  const stop = el('button', 'btn danger'); stop.type = 'button';
  const start = el('button', 'btn'); start.type = 'button';
  const logs = el('button', 'btn ghost', 'Journal'); logs.type = 'button';
  actions.append(load, start, stop, logs);
  if (LANE_TARGETS[unit].length > 1) left.append(desc, pickBox, boot, actions, why);
  else left.append(desc, boot, actions, why);
  const right = el('dl', 'facts');
  grid.append(left, right);
  const hist = el('div'); hist.style.marginTop = 'var(--s-4)';
  root.append(head, grid, hist);
  c = {root, h, state, desc, boot, why, target: LANE_TARGETS[unit][0], pickBox, opts, pickLine, load, stop, start, logs, right, hist, touched: false};
  // a choice holds the selector until the lane serves it; a journey not gone through
  // gives the selector back to the lane
  load.addEventListener('click', () => askJourney(c.target || LANE_TARGETS[unit][0],
    {onClose: () => { if (!(SHEET && SHEET.running)) { c.touched = false; renderLanes(); } }}));
  stop.addEventListener('click', () => {
    const e = engines()[unit] || {};
    const warns = [];
    // the FP4-Local config writes no table (vLLM, its AOT caches kept): the boot it throws
    // away is the boot itself (found in review, 2026-10-20)
    if (TRANSITIONAL.has(e.state) && unit === UFLASH) warns.push(e.target === 'flash-fp4-local'
      ? 'This boot is thrown away: the next start takes a whole boot again (' + readyIn(unit) + ').'
      : 'This boot is thrown away: every flash boot writes its 47.7 GiB table from scratch, so the next start takes a whole boot again (' + readyIn(unit) + ').');
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
  boots.forEach(b => { const bar = el('span'); bar.title = fmtDur(b); bar.style.cssText = `flex:0 0 18px; border-radius:4px 4px 2px 2px; height:${Math.max(6, 50 * b / mx).toFixed(0)}px; background:linear-gradient(180deg, var(--brand-hi), var(--brand-lo)); opacity:.85`; wrap.append(bar); });
  const lab = el('p', 'help'); lab.textContent = `Last ${boots.length} boots: ${boots.map(fmtDur).join(', ')}. Median ${fmtDur(bootSeconds(unit))}.`;
  const row = el('div'); row.style.cssText = 'display:flex; gap:var(--s-4); align-items:flex-end; flex-wrap:wrap'; row.append(wrap, lab);
  box.append(row);
  const pool = lanePool(unit, e);
  if (pool){
    const live = livePool(unit, e);
    box.append(el('p', 'help', `KV pool ${live ? 'this' : 'last'} boot: ${fmtN(pool)} tokens` + (e.pools && e.pools.n > 1 ? `, between ${fmtN(e.pools.min)} and ${fmtN(e.pools.max)} over the ${e.pools.n} boots this cockpit watched (${e.pools.spread_pct} % spread). The pool is decided at boot by the memory free at that moment.` : '.')));
  }
}
function renderLanes(){
  const s = servingEngine(), why = busyWhy();
  LANE_UNITS.forEach(unit => {
    const c = laneCard(unit), e = engines()[unit], has = installed(unit);
    const st = e ? e.state : null, seated = !!(s && s[0] === unit);
    setText(c.h, LANE_META[unit].title + (seated ? ', serving' : ''));
    cap(c.state, !has ? 'not installed' : stateLabel(e) || 'unknown', has ? stateKind(st) : '', has ? stateLive(st) : false);
    setText(c.desc, LANE_META[unit].desc + ' ' + LANE_META[unit].kind + '.');
    const served = laneTarget(unit);
    if (c.touched && served && served === c.target && (engines()[unit] || {}).state === 'ready') c.touched = false;
    if (!c.touched && served && c.target !== served) c.target = served;
    const target = c.target || LANE_TARGETS[unit][0];
    // a lane that is not here has no checkpoint to pick: it installs with its default one
    c.pickBox.hidden = !has;
    // the served version is marked once the lane serves for real, as its bay and its card are:
    // a boot, a stop or a wedge is not serving
    const live = seated && (st === 'ready' || st === 'degraded');
    c.opts.forEach(b => {
      const t = b.dataset.target;
      b.setAttribute('aria-checked', String(t === target)); b.tabIndex = t === target ? 0 : -1;
      b.classList.toggle('served', live && t === served);
    });
    setText(c.pickLine, `${TARGET_NAME[target]}.` + (TARGET_LINE[target] ? ` ${TARGET_LINE[target]}.` : '') + (live && target === served ? ' Serving now.' : ''));
    const on = !!e && (!UNIT_DOWN.has(st) || !!e.restarting);
    const sameTarget = !laneTarget(unit) || laneTarget(unit) === target;
    // the verbs that make sense now
    // the lane that serves what it is set to has nothing to load, unless an update is owed to it
    // the same test as the journey's (base.js, laneJourneySteps): the card names the restart
    // only when its Load is that restart
    const owedUpdate = seated && laneTarget(unit) === target && !!updateOwed(unit);
    c.load.hidden = !has || (seated && sameTarget && !owedUpdate);
    setText(c.load, owedUpdate ? `Restart ${LANE_NAME[unit]} to apply the update`
      : seated || LANE_TARGETS[unit].length > 1 ? `Load ${TARGET_NAME[target]}` : `Load ${LANE_NAME[unit]}`);
    // installed on demand and not prepared yet, or prepared and not switched to yet (its
    // checkpoint, its chat template): only its Load, which prepares and switches it, can start it
    const unbuilt = has && SIDE_LANE[unit] && (F.lanesReady || {})[SIDE_LANE[unit]] === false;
    const unstartable = has && SIDE_LANE[unit] && (F.lanesStartable || {})[SIDE_LANE[unit]] === false;
    c.start.hidden = !has || on || !!(s && s[0] !== unit) || !!unbuilt || !!unstartable;
    setText(c.start, `Start ${LANE_NAME[unit]} as installed`);
    c.stop.hidden = !has || !on;
    setText(c.stop, st === 'stopping' ? 'Stopping…' : `Stop ${LANE_NAME[unit]}`);
    const blocked = on ? null : blockedFor(`unit:start:${unit}`);
    [c.load, c.start, c.stop].forEach(b => { b.disabled = !!why || st === 'stopping'; b.title = why; });
    c.start.disabled = c.start.disabled || !!blocked;
    c.why.hidden = !(blocked && !c.start.hidden) && !(st === 'failed' || st === 'orphan');
    // a lane its GPU cannot start (issue #54): no journal to read or start to try again
    setText(c.why, st === 'orphan' ? 'Its container runs outside systemd: Start replaces it with the unit’s own.'
      : st === 'failed' && e.cannot ? 'This GPU cannot start it: its addressing mode is not ATS (docs/flash-lane.md). Load another lane.'
      : st === 'failed' ? 'The unit failed: read its journal, then start it again.' : blocked ? blocked[0] : '');
    c.logs.hidden = !has;
    // boot or stop in progress
    if (e && TRANSITIONAL.has(st)){
      c.boot.hidden = false; clear(c.boot);
      const stages = e.stages && e.stages.length ? e.stages : ALL_STAGES, done = (e.stage_done || []).length;
      const eta = e.eta || READY_DEFAULT[unit], pct = eta && e.elapsed ? Math.min(97, 100 * e.elapsed / eta) : 8 + done * 80 / stages.length;
      const m = el('div', 'meter'); const i = el('i'); i.style.width = pct.toFixed(1) + '%'; m.append(i); c.boot.append(m);
      const ph = el('div', 'phases'); stages.forEach((sg, k) => ph.append(el('span', k < done ? 'done' : k === done ? 'now' : '', STAGE_LABEL[sg] || sg))); c.boot.append(ph);
      c.boot.append(el('p', 'help', e.held ? `Waiting for GPU memory: ${e.held}. It starts by itself once that memory is back.`
        : `${STATE_LABEL[st]}${e.detail && e.detail !== 'ready' ? ' (' + e.detail + ')' : ''}, ${fmtDur(e.elapsed)} elapsed, ` +
        (e.elapsed && eta ? `about ${fmtDur(Math.max(0, eta - e.elapsed))} left` : 'learning the duration') + ((e.boots || []).length ? ` (median of ${e.boots.length} boots)` : ' (reference box)')));
      if (e.overdue) c.boot.append(el('p', 'help warn-t', 'This boot takes more than twice the usual time: read its journal.'));
    } else if (e && st === 'stopping'){
      c.boot.hidden = false; clear(c.boot);
      const m = el('div', 'meter indet'); m.append(el('i')); c.boot.append(m);
      c.boot.append(el('p', 'help', `stopping, ${e.state_elapsed != null ? fmtDur(e.state_elapsed) : '…'} elapsed`));
    } else c.boot.hidden = true;
    // the right column: the facts
    const en = (F.units[unit] || {}).enabled;
    // The unit's file says what the NEXT start loads; a switch not restarted yet makes it
    // another checkpoint than the one serving, and the card named the next one as served
    const servesNow = servedTarget(unit), pending = !!(servesNow && e && e.target && servesNow !== e.target && F.model);
    facts(c.right, [
      // '…' is a value on its way: once the lifecycle is in and names no checkpoint (a launch
      // file unreadable, a lane it does not list), it read '…' for good (found by the monkey
      // check, 2026-10-02)
      ['Checkpoint', pending ? F.model.split('/').pop() : e && e.model ? e.model.split('/').pop() : !has ? 'not installed' : e || F.life ? 'unknown' : '…'],
      ['Target', laneTarget(unit) ? TARGET_NAME[laneTarget(unit)] : null],
      ['Next start loads', pending ? TARGET_NAME[e.target] : null],
      ['At boot', has ? (en === 'enabled' ? 'starts' : en === '?' ? 'unknown' : 'manual start') : null],
      ['Boot takes', has ? readyIn(unit) : null],
      // installed on demand: its runtime is built at its first Load (LANE_RUNTIME says what)
      ['Runtime', has && SIDE_LANE[unit] && (F.lanesReady || {})[SIDE_LANE[unit]] === false ? `built at its first Load: ${LANE_RUNTIME[SIDE_LANE[unit]]}` : null],
      ['Up for', seated && e && e.elapsed && (st === 'ready' || st === 'degraded') ? fmtDur(e.elapsed) : null],
      ['Install', !has ? LANE_INSTALL[unit] : null, 'code']]);
    c.root.classList.toggle('seated-card', seated);
    c.root.style.borderColor = seated && (st === 'ready' || st === 'degraded') ? 'color-mix(in srgb, var(--ok) 45%, transparent)' : '';
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
  // SGLang names steps and draft tokens; the Nemotron lane's vLLM drafter has a draft length only
  setText('eng-spec', !i.speculative_algorithm ? 'none' : i.speculative_num_steps != null
    ? `${i.speculative_algorithm}, ${i.speculative_num_steps} steps, ${i.speculative_num_draft_tokens} draft tokens`
    : i.speculative_num_draft_tokens != null ? `${i.speculative_algorithm}, ${i.speculative_num_draft_tokens} draft tokens` : i.speculative_algorithm);
  setText('eng-attn', i.prefill_attention_backend ? `${i.prefill_attention_backend} / ${i.decode_attention_backend}` : (i.attention_backend ?? 'n/a'));
  setText('eng-radix', i.mamba_radix_cache_strategy ?? 'n/a');
  // which engine, named: SGLang gives a bare number, the Kolibri lane "vLLM 0.29.0" (the row read
  // "SGLang vLLM 0.29.0" there: review, 2026-10-14)
  const ver = String(i.version ?? 'n/a').split('+')[0];
  setText('eng-ver', /^\d/.test(ver) ? 'SGLang ' + ver : ver);
});
on('containers', d => {
  const serving = Object.entries(d.containers || {}).find(([, c]) => c.image);
  setText('eng-image', serving ? serving[1].image.replace(/@sha256:([0-9a-f]{12})[0-9a-f]+/, '@$1…') : 'no serving container');
  setTitle('eng-image', serving ? serving[1].image : '');
  setText('px-cont', serving ? serving[0] : 'none (a diffusion lane runs natively)');
});
on('canary', d => {
  setText('px-probe', F.canaryTxt || '…');
  setText('px-last', d.skipped && (servingEngine() || [])[0] === KOLIBRI_UNIT ? 'none on the Kolibri lane'
    : d.skipped && isNemotron((servingEngine() || [])[0]) ? 'none on the Nemotron lanes'
    : d.skipped && fp4Holds() ? 'none on the FP4-Local config'
    : d.last_ok ? clockTime(d.last_ok) : 'never in this cockpit’s life');
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
