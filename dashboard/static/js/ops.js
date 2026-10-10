"use strict";
/* Traffic, Machine, Library, Logs and Settings: the views you open to look closer. */

// ── Traffic ───────────────────────────────────────────────────────────────────
// With no text engine the counters say so: they read '…', a value on its way, for as long as
// the box served nothing (found by the monkey check, 2026-10-02; the page before the
// redesign of 2026-09-30 said "no engine")
on('engine_fast', d => {
  const none = !d.load, l = (d.load || [])[0] || {};
  setText('tr-run', none ? 'none' : String(runningReqs(l)));
  setText('tr-wait', none ? 'none' : String(waitingReqs(l)));
  setText('tr-tok', none ? 'none' : fmtN(physTokens(l)));
  if (none){ SERIES.req = []; } else push('req', runningReqs(l));
  drawSpark($('tr-spark'), 'req', cssVar('--gold'), Math.max(4, F.maxRun || 4), {empty: none ? 'no text engine' : 'quiet so far'});
});
// said after the whole payload: which lane serves is the lifecycle's, read after this one
afterApply(() => {
  const none = F.noEngine || !textReady(), n = loadWords(F.load || {});
  cap('tr-cap', none ? (imageServing() ? 'no text engine: the image lane serves' : videoServing() ? 'no text engine: the video lane serves' : 'no text engine')
      : n || 'idle', none ? '' : n ? 'ok' : '', n && !none ? 'live' : true);
});
on('decode', d => {
  const t = d.decode, u = d.usage || {};
  if (!d.lane){ setText('tr-acc', 'none'); setText('tr-kv', 'none'); return; }
  setText('tr-acc', t ? t.accept_len.toFixed(2) : 'idle');
  setText('tr-kv', t ? (100 * t.token_usage).toFixed(1) + ' %' : u.tokens ? (100 * u.tokens).toFixed(0) + ' %' : 'idle');
});
// ── the last 5 minutes, from the engine's own counters (engine_metrics.py) ──────
// Each request is counted once, when it finishes; a speed holds no idle time; the cockpit's
// canary is taken out. The window is the cockpit's, so a reload of the page loses nothing.
// No rate off the scheduler's log lines sits here: each of those is a token count over the
// time since the previous line of its kind, idle time included (the v1.22.12 notes in
// CHANGELOG.md have the measurements).
on('metrics', d => {
  if (!d.lane){
    cap('wm-cap', 'no text engine', ''); setText('wm-note', '');
    setText('wm-verdict', 'No text engine is serving: there is nothing to count.');
    facts($('wm-facts'), []); return;
  }
  if (d.requests == null){
    // nothing read yet for this engine: a read that failed is said apart from one on its way
    cap('wm-cap', d.read === false ? 'no counters' : 'first read', ''); setText('wm-note', '');
    setText('wm-verdict', d.read === false
      ? 'The engine has not answered /metrics. An engine started without --enable-metrics has no counters to show (both text lanes here start with it), and a busy one answers a later read.'
      : 'The first read of the engine’s counters is on its way.');
    facts($('wm-facts'), []); return;
  }
  // the gate counts only finished work, not the peaks: the cockpit's own canary shows up in
  // num_running_reqs while it runs, and a canary-only window must still read quiet, even
  // though the Busiest moment row says one request ran
  const w = d.watched_s || 0, any = d.requests > 0 || d.decode_tps != null || d.ttft_s != null || d.prefill_tps != null;
  const peaks = [d.pool_max ? `${Math.round(100 * d.pool_max)} % of the pool` : null,
                 d.running_max ? `${fmtN(d.running_max)} request${d.running_max > 1 ? 's' : ''} at once` : null,
                 d.queued_max ? `${fmtN(d.queued_max)} waiting` : null].filter(Boolean);
  setText('wm-note', w > 0 && w < 295 ? `watched ${fmtDur(w)}` : '');
  cap('wm-cap', d.approximate ? 'approximate' : any ? 'watching' : 'quiet', d.approximate ? 'warn' : any ? 'ok' : '');
  setText('wm-verdict', (d.read === false ? 'The last read of the engine’s counters failed: these numbers come from the one before. ' : '')
      + (d.approximate ? 'A canary ran beside a client request, so it is counted as one while it lies inside the window. ' : '')
      + 'From the engine’s own counters: each request counted once, when it finishes, and the cockpit’s canary taken out.');
  facts($('wm-facts'), [
    ['Requests finished', fmtN(d.requests)],
    ['Prompt tokens from the cache', d.reuse == null ? 'no request finished' : `${(100 * d.reuse).toFixed(1)} % of ${fmtN(d.prompt_tokens)}`],
    // the queue is what the client waits before the engine starts on it; prefill counts the
    // tokens the engine computed over its own prefill stopwatch, cache reads not in it
    ['Queued before starting', d.queue_s == null ? null : `${d.queue_s < 10 ? d.queue_s.toFixed(2) : d.queue_s.toFixed(1)} s on average`],
    ['Prefill speed', d.prefill_tps == null ? null : `${fmtN(Math.round(d.prefill_tps))} tok/s computed`],
    ['First token after', d.ttft_s == null ? null : `${d.ttft_s < 10 ? d.ttft_s.toFixed(2) : d.ttft_s.toFixed(1)} s on average`],
    ['Decode speed', d.decode_tps == null ? null : `${d.decode_tps.toFixed(1)} tok/s per request`],
    // the drafter's yield over its own verification count: not the boot average the
    // gauge holds, not the scheduler's log window
    ['Accepted per draft step', d.acc_len == null ? null : `${d.acc_len.toFixed(2)} tokens per verify step`],
    ['Generated, all clients', d.throughput_tps == null ? null : `${d.throughput_tps.toFixed(1)} tok/s, ${fmtN(d.generated_tokens)} tokens`],
    // the levels are not counters: these are the cockpit's own peaks of its reads
    ['Busiest moment', peaks.length ? peaks.join(', ') : null],
    ['Idle in the window', d.idle_share == null ? null : `${Math.round(100 * d.idle_share)} % of the time`],
    ['Abandons the engine accepted', d.aborted > 0 ? fmtN(d.aborted) : 'none']]);
});
const feedTime = ts => {
  if (!ts || ts.length < 19) return ts || '';
  const n = new Date(), today = `${n.getFullYear()}-${String(n.getMonth() + 1).padStart(2, '0')}-${String(n.getDate()).padStart(2, '0')}`;
  return ts.slice(0, 10) === today ? ts.slice(11, 19) : `${ts.slice(8, 10)}/${ts.slice(5, 7)} ${ts.slice(11, 16)}`;
};
on('feed', d => {
  const tb = $('feed').tBodies[0]; clear(tb);
  const rows = (d.rows || []).slice().reverse();
  rows.forEach(r => {
    const tr = tb.insertRow();
    tr.insertCell().textContent = feedTime(r.ts);
    // every client so far is this box talking to itself through the proxy: the port tells them apart
    const c1 = tr.insertCell(); c1.className = 'num'; c1.textContent = r.peer.startsWith('127.0.0.1:') ? ':' + r.peer.split(':').pop() : r.peer; c1.title = r.peer;
    const cp = tr.insertCell(); cp.textContent = r.path; cp.title = r.path;
    const c2 = tr.insertCell(); c2.className = 'r num'; c2.textContent = r.bytes >= 1024 ? (r.bytes / 1024).toFixed(0) + ' KB' : r.bytes + ' B';
    const c3 = tr.insertCell(); c3.className = 'r num'; c3.textContent = r.secs != null ? r.secs.toFixed(1) + ' s' : '';
    // the kind comes from the server: an ok request wears no colour, nor does the cockpit's
    // own probe; only a client who left (ember) and a failure (clay) light up
    const kind = {ok: '', gone: 'warn', fail: 'err', live: 'gold', unknown: '', probe: ''}[r.kind] ?? 'err';
    const c4 = tr.insertCell(); c4.append(el('span', 'tag ' + kind, r.outcome));
    if (r.detail) c4.append(el('div', 'help', r.detail));
  });
  const inflight = rows.filter(r => r.outcome === 'in flight').length;
  badge('traffic', inflight ? String(inflight) : '', inflight ? 'live' : '');
  if (!rows.length){ const tr = tb.insertRow(); tr.className = 'empty'; const c = tr.insertCell(); c.colSpan = 6; c.textContent = 'No request has gone through the proxy yet. Agent clients use :30001.'; }
});
on('reqguard', d => {
  const g = d.guard || {}, z = d.zombies || {};
  cap('zg-cap', d.state === 'err' ? 'leaking' : d.state === 'warn' ? 'check' : 'holding', d.state === 'err' ? 'err' : d.state === 'warn' ? 'warn' : 'ok');
  setText('zg-verdict', `${cap1(d.verdict || '')} (last ${d.window || '10m'}${d.lane ? ', ' + d.lane : ''}).`);
  setText('zg-ver', g.version ? 'v' + g.version : 'no banner in the journal');
  setText('zg-override', d.override === true ? 'yes' : d.override === false ? 'no' : d.lane ? 'unknown (docker did not answer)' : 'no engine');
  const acted = [];
  if (g.aborted) acted.push(`${g.aborted} aborted`);
  if (g.drained) acted.push(`${g.drained} drained${g.drain_max_s != null ? `, longest ${g.drain_max_s.toFixed(0)} s` : ''}`);
  if (g.abort_failed) acted.push(`${g.abort_failed} aborts unanswered`);
  if (g.ceiling) acted.push(`${g.ceiling} hit the drain ceiling`);
  setText('zg-acted', acted.length ? acted.join(', ') : 'nothing to do');
  setText('zg-flood', z.lines ? `${fmtN(z.lines)} from ${z.distinct}` : '0');
  const note = g.predates_abort ? 'Below v6.14 a client that gives up during prefill leaves a generation the proxy cannot name.'
    : d.override === false ? 'Without SGLANG_ENABLE_REQUEST_HEADER_OVERRIDES the engine keeps its own request id, so an abandoned answer is read to its end. The next engine restart picks the flag up.' : '';
  show('zg-note', !!note); setText('zg-note', note);
  const rows = z.requests || [], tb = $('zg-table').tBodies[0]; clear(tb); show('zg-table', rows.length > 0);
  rows.forEach(r => { const tr = tb.insertRow(); const a = tr.insertCell(); a.className = 'num'; a.textContent = r.rid.slice(0, 12);
    const b = tr.insertCell(); b.className = 'r num'; b.textContent = fmtN(r.lines); const c = tr.insertCell(); c.className = 'r num'; c.textContent = r.secs != null ? `${r.secs.toFixed(0)} s` : ''; });
});

// ── Machine ───────────────────────────────────────────────────────────────────
on('machine', d => {
  const m = d.mem || {};
  if (m.MemTotal){
    const used = m.MemTotal - m.MemAvailable;
    // the colour follows headroom in gibibytes, not a percentage: a loaded lane sits past
    // 90 % by design, and an alarm that rings while all is fine teaches people to ignore it
    const kind = m.MemAvailable < 4 * GIB ? 'err' : m.MemAvailable < 9 * GIB ? 'warn' : 'ok';
    cap('mm-cap', fmtGiB(m.MemAvailable) + ' available', kind);
    setText('mm-used', fmtGiB(used) + ' of ' + fmtGiB(m.MemTotal)); setText('mm-avail', fmtGiB(m.MemAvailable));
    setText('mm-cache', fmtGiB(m.Cached)); setText('mm-swap', fmtGiB((m.SwapTotal || 0) - (m.SwapFree || 0)));
    push('mem', used / GIB); drawSpark($('mm-spark'), 'mem', cssVar('--gold'), m.MemTotal / GIB);
    badge('machine', m.MemAvailable < 4 * GIB ? fmtGiB(m.MemAvailable, 0) : '', m.MemAvailable < 4 * GIB ? 'err' : '');
  }
  const cpu = d.cpu_pct || {};
  cap('cpu-cap', (cpu.cpu ?? 0).toFixed(0) + ' %', (cpu.cpu || 0) > 85 ? 'warn' : 'ok');
  push('cpu', cpu.cpu || 0); drawSpark($('cpu-spark'), 'cpu', cssVar('--cool'), 100);
  const cores = Object.keys(cpu).filter(k => k !== 'cpu').sort((a, b) => Number(a.slice(3)) - Number(b.slice(3)));
  setText('cpu-n', String(cores.length)); setText('cpu-load', (d.load || []).map(x => x.toFixed(2)).join(', '));
  const box = $('cpu-cores');
  if (box.children.length !== cores.length){ clear(box); cores.forEach(() => { const c = el('span'); c.style.cssText = 'height:26px; border-radius:5px; background:var(--ink-3); transition:background .6s'; box.append(c); }); }
  cores.forEach((k, i) => { const v = Math.min(100, cpu[k] || 0); box.children[i].style.background = `color-mix(in srgb, var(--cool) ${Math.round(v)}%, var(--ink-3))`; box.children[i].title = `${k}: ${v.toFixed(0)} %`; });
  const dk = d.disks || {};
  setText('dk-home', dk.home ? fmtGiB(dk.home.free) + ' free of ' + fmtGiB(dk.home.total) : 'n/a');
  setText('dk-docker', dk.docker ? fmtGiB(dk.docker.free) + ' free of ' + fmtGiB(dk.docker.total) : 'n/a');
});
on('engine_fast', d => {
  if (d.mem_floor){ const f = d.mem_floor; setText('mm-floor', `aborts generations under ${f.gib} GiB` + (f.aborts ? `, fired ${f.aborts} time${f.aborts > 1 ? 's' : ''}` : ', never fired')); }
});
on('lifecycle', d => {
  const g = d.pool_guard; if (!g) return;
  setText('mm-guard', !g.enabled ? 'off (COCKPIT_POOL_GUARD=0)' : g.fails ? `cannot flush since ${clockTime(g.last_fail)}: ${g.last_err}`
    : g.flushes ? `${g.flushes} flush${g.flushes > 1 ? 'es' : ''}, last ${clockTime(g.last)}, above ${Math.round(g.threshold * 100)} % held` : `armed at ${Math.round(g.threshold * 100)} % held, never fired`);
});
// nvidia-smi silent: no reading, said as such. It read a green "…", a power line falling to
// 0 W and "No process on the GPU" (found by the monkey check, 2026-10-02)
on('gpu', d => {
  const t = d.temp_c;
  setText('gpu-pow', d.power_w != null ? d.power_w.toFixed(1) + ' W' : 'n/a');
  setText('gpu-temp', t != null ? t.toFixed(0) + ' °C' : 'n/a');
  cap('gpu-cap', t != null ? t.toFixed(0) + ' °C' : 'n/a', t == null ? '' : t > 85 ? 'err' : t > 75 ? 'warn' : 'ok');
  if (d.power_w != null) push('pow', d.power_w);
  drawSpark($('gpu-spark'), 'pow', cssVar('--ember'), undefined, {empty: d.power_w == null ? 'no reading' : 'collecting…'});
  const tb = $('gpu-procs').tBodies[0]; clear(tb);
  (d.procs || []).slice(0, 6).forEach(p => { const tr = tb.insertRow(); tr.insertCell().textContent = p.name || '?';
    const a = tr.insertCell(); a.className = 'r num'; a.textContent = p.pid; const b = tr.insertCell(); b.className = 'r num'; b.textContent = p.mem; });
  if (!tb.rows.length){ const tr = tb.insertRow(); tr.className = 'empty'; const c = tr.insertCell(); c.colSpan = 3;
    c.textContent = Array.isArray(d.procs) ? 'No process on the GPU.' : 'No reading: nvidia-smi did not answer.'; }
});
on('kernel', d => { const n = d.nvrm_oom_1h;
  setText('dk-nvrm', n == null ? 'n/a (the kernel log did not answer)' : n ? `${n}, the last at ${(d.nvrm_last || '').slice(11, 19)}` : 'none'); });

// ── Library (on demand: each scan says what happened) ─────────────────────────
function tag(t, kind){ return el('span', 'tag' + (kind ? ' ' + kind : ''), t); }
function emptyRow(tb, n, txt){ const tr = tb.insertRow(); tr.className = 'empty'; const c = tr.insertCell(); c.colSpan = n; c.textContent = txt; }
function fmtServe(sv){
  const parts = [];
  if (sv.context_length) parts.push('ctx ' + fmtN(sv.context_length));
  if (sv.mem_fraction != null) parts.push('mem ' + sv.mem_fraction);
  if (sv.max_running_requests) parts.push('run ' + sv.max_running_requests);
  if (sv.max_total_tokens) parts.push('pool ' + fmtN(sv.max_total_tokens));
  if (sv.chunked_prefill) parts.push('chunk ' + sv.chunked_prefill);
  const attn = sv.attention_backend || [sv.prefill_attention, sv.decode_attention].filter(Boolean).join('/');
  if (attn) parts.push(attn);
  return parts.join(', ');
}
function recipeRow(tb, row){
  const r = row.recipe, tr = tb.insertRow(); const c0 = tr.insertCell();
  if (r){ c0.append(tag(r.lane === 'flash' ? 'flash' : '27B', 'gold'), ' ', el('strong', null, r.id)); if (row.installed) c0.append(' ', tag('installed', 'cool')); if (!r.builtin) c0.append(' ', tag(row.file || 'custom')); }
  else c0.append(tag(row.file || '?', 'err'));
  if (row.errors && row.errors.length){ const td = tr.insertCell(); td.colSpan = 6; td.append(tag('invalid', 'err'), ' ', row.errors.join('; ')); return; }
  const c1 = tr.insertCell(); c1.className = 'num'; c1.textContent = r.engine.image.replace(/@sha256:([0-9a-f]{12})[0-9a-f]+/, '@$1…'); c1.title = r.engine.image;
  const c2 = tr.insertCell(); c2.textContent = r.model.repo.split('/').pop() + ' '; c2.append(el('span', 'faint num', r.model.revision.slice(0, 10)));
  const dr = r.drafter || {};
  tr.insertCell().textContent = dr.algorithm === 'none' || !dr.algorithm ? 'none' : dr.algorithm + (dr.repo ? ' ' + dr.repo.split('/').pop() : ' (own head)') + (dr.draft_tokens ? ', ' + dr.draft_tokens + ' tokens' : '');
  tr.insertCell().textContent = fmtServe(r.serve || {});
  const p = row.presence || {}, cc = tr.insertCell();
  [['image', p.image], ['model', p.model], ['drafter', p.drafter]].forEach(([k, v]) => cc.append(tag(v === true ? k : v === false ? k + ' missing' : k + ' n/a', v === false ? 'err' : ''), ' '));
  if (p.downloading) cc.append(tag('downloading', 'warn'));
  const cd = tr.insertCell();
  if (row.drift == null) cd.append(tag('lane not installed'));
  else if (!row.drift.length) cd.append(tag('matches', 'cool'));
  else { const det = el('details'); const sum = el('summary'); sum.append(tag(row.drift.length + ' differ', row.installed ? 'warn' : '')); det.append(sum);
    const ul = el('ul'); ul.style.cssText = 'margin:6px 0 0 16px; font-size:12px';
    row.drift.forEach(x => ul.append(el('li', 'num', `${x.key}: recipe ${JSON.stringify(x.recipe)}, installed ${JSON.stringify(x.installed)}`))); det.append(ul); cd.append(det); }
}
const LOADED = {recipes: false};
async function loadRecipes(){
  const b = $('rcp-btn'); setText(b, 'Loading…'); b.disabled = true;
  try{
    const r = await fetch('/api/recipes?refresh=1'); if (r.status === 401) return login(); if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json(); LOADED.recipes = true;
    const tb = $('rcp-table').tBodies[0]; clear(tb);
    (d.builtin || []).forEach(row => recipeRow(tb, row)); (d.custom || []).forEach(row => recipeRow(tb, row));
    if (!tb.rows.length) emptyRow(tb, 7, 'No recipe found.');
    labelCells($('rcp-table'));
    setText('rcp-dir', d.custom_dir ? `Custom recipes live in ${d.custom_dir}.` : '');
    const inst = (d.builtin || []).concat(d.custom || []).filter(x => x.installed).map(x => x.recipe.id);
    const drift = (d.builtin || []).filter(x => x.installed && x.drift && x.drift.length).length;
    setText('rcp-line', (inst.length ? 'Installed: ' + inst.join(', ') + '.' : 'No lane matches a recipe.') + (drift ? ` ${drift} installed lane differs from its recipe (open the difference).` : ''));
    setText('rcp-age', 'read ' + new Date().toLocaleTimeString([], {hour12: false}));
    badge('library', drift ? `${drift} drift` : '', 'warn'); setText(b, 'Reload');
  }catch(e){ setText('rcp-line', 'Could not load the recipes: ' + e.message); setText(b, 'Retry'); }
  finally{ b.disabled = false; }
}
async function scanRegistry(){
  const b = $('reg-btn'); setText(b, 'Scanning…'); b.disabled = true;
  try{
    const r = await fetch('/api/registry?refresh=1'); if (r.status === 401) return login(); if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    const tb = $('reg-models').tBodies[0]; clear(tb);
    (d.models || []).forEach(m => (m.revisions || []).forEach((rv, i) => {
      const tr = tb.insertRow(); tr.insertCell().textContent = i ? '' : m.repo_id.split('/').pop();
      const a = tr.insertCell(); a.className = 'num'; a.textContent = rv.rev.slice(0, 10);
      const b2 = tr.insertCell(); b2.className = 'r num'; b2.textContent = fmtGiB(rv.bytes);
      const c = tr.insertCell(); c.className = 'r num'; c.textContent = i ? '' : fmtGiB(m.disk_bytes);
      const s = tr.insertCell(); s.append(tag(rv.status, rv.status === 'pinned' ? 'cool' : rv.status === 'stray' ? 'warn' : '')); if (rv.pin) s.append(' ', tag(rv.pin));
    }));
    if (!tb.rows.length) emptyRow(tb, 5, 'No managed model in the Hugging Face cache.');
    labelCells($('reg-models'));
    const ti = $('reg-images').tBodies[0]; clear(ti);
    (d.images || []).forEach(im => { const tr = ti.insertRow(); tr.insertCell().textContent = im.ref; const a = tr.insertCell(); a.className = 'r num'; a.textContent = im.size; const c = tr.insertCell(); c.className = 'r num'; c.textContent = im.id; });
    if (!ti.rows.length) emptyRow(ti, 3, 'No engine image.');
    labelCells($('reg-images'));
    const to = $('reg-other').tBodies[0]; clear(to);
    const others = (d.other_models || []).slice().sort((x, y) => y.disk_bytes - x.disk_bytes);
    others.forEach(m => { const tr = to.insertRow(); tr.insertCell().textContent = m.repo_id; const c = tr.insertCell(); c.className = 'r num'; c.textContent = fmtGiB(m.disk_bytes); });
    if (!others.length) emptyRow(to, 2, 'None.');
    setText('reg-other-tag', others.length ? fmtGiB(others.reduce((a, m) => a + m.disk_bytes, 0)) + ' in ' + others.length : '');
    const strays = (d.models || []).flatMap(m => m.revisions).filter(rv => rv.status === 'stray');
    setText(b, strays.length ? `Rescan (${strays.length} stray)` : 'Rescan'); setText('reg-age', 'scanned ' + new Date().toLocaleTimeString([], {hour12: false}));
  }catch(e){ setText(b, 'Retry'); setText('reg-age', 'scan failed: ' + e.message); }
  finally{ b.disabled = false; }
}
async function checkUpstream(){
  const b = $('up-btn'); setText(b, 'Checking…'); b.disabled = true;
  try{
    const r = await fetch('/api/upstream?refresh=1'); if (r.status === 401) return login(); if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json(), tb = $('up-table').tBodies[0]; clear(tb);
    (d.models || []).forEach(m => { const tr = tb.insertRow(); tr.insertCell().textContent = m.model.split('/').pop();
      const a = tr.insertCell(); a.className = 'num'; a.textContent = m.pin; const c = tr.insertCell(); c.className = 'num'; c.textContent = m.upstream || '?';
      tr.insertCell().append(tag(m.status, m.status === 'same' ? 'cool' : m.status === 'moved' ? 'warn' : 'err')); });
    labelCells($('up-table'));
    const rel = d.release || {}, stale = rel.stale_code || [];
    const relLine = rel.latest ? `Release: this box ${rel.local}, latest ${rel.latest}` + (rel.latest === rel.local ? ', up to date.' : ', an update is out.') : 'The release check is offline (no network, or GitHub unreachable).';
    setText('up-line', stale.length ? `This cockpit runs code older than the repo (${stale.join(', ')} changed on disk): restart it. ${relLine}` : relLine);
    const moved = (d.models || []).filter(m => m.status === 'moved').length;
    setText(b, moved ? `Recheck (${moved} moved)` : 'Recheck');
  }catch(e){ setText(b, 'Retry'); setText('up-line', 'The check failed: ' + e.message); }
  finally{ b.disabled = false; }
}
async function scanInventory(){
  const b = $('inv-btn'); setText(b, 'Scanning…'); b.disabled = true;
  try{
    const r = await fetch('/api/inventory'); if (r.status === 401) return login(); if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json(), tb = $('inv-table').tBodies[0]; clear(tb);
    (d.items || []).forEach(i => { const tr = tb.insertRow(); tr.insertCell().append(tag(i.kind)); tr.insertCell().textContent = i.what; });
    if (!(d.items || []).length) emptyRow(tb, 2, 'Nothing found. Is the repo installed on this box?');
    setText(b, 'Rescan'); setText('inv-line', `${(d.items || []).length} items, scanned ${new Date().toLocaleTimeString([], {hour12: false})}.`);
  }catch(e){ setText(b, 'Retry'); setText('inv-line', 'The scan failed: ' + e.message); }
  finally{ b.disabled = false; }
}
on('job_finished', j => { if (j.action === 'switch' || j.action === 'unit'){ LOADED.recipes = false; if (activeView === 'library') loadRecipes(); } });
onShow('library', () => { if (!LOADED.recipes) loadRecipes(); });

// ── Logs ──────────────────────────────────────────────────────────────────────
const LOGS_WANT = {src: null};
let followTimer = null, logTouched = false, logLines = [];
// What the box asks its own engine, dimmed so the engine's words stand out: the cockpit's
// load every second and its engine read every 30 s (current routes and the legacy ones),
// the health probes, the video poll. The load alone filled the whole view (2026-10-02).
const POLLED = /GET \/(health|v1\/videos|v1\/loads|server_info|get_load|get_server_info)/;
function drawLog(){
  const v = $('log-view'), q = $('log-filter').value.trim().toLowerCase();
  clear(v);
  const lines = q ? logLines.filter(l => l.toLowerCase().includes(q)) : logLines;
  if (!lines.length){ v.textContent = logLines.length ? 'No line matches the filter.' : 'This source has no output yet.'; return; }
  lines.forEach((ln, i) => {
    const cls = /error|exception|traceback|failed|fatal/i.test(ln) ? 'l-err' : /warn/i.test(ln) ? 'l-warn' : /ready to roll|started|ready\b|succeeded/i.test(ln) ? 'l-ok'
      : POLLED.test(ln) ? 'l-dim' : '';
    const sp = el('span', cls || null, ln);
    v.append(sp); if (i < lines.length - 1) v.append('\n');
  });
  v.scrollTop = v.scrollHeight;
}
async function tailLog(){
  const src = $('log-src').value;
  try{
    const r = await fetch('/api/logs/' + src); if (r.status === 401) return login(); if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json(); logLines = d.lines || []; drawLog();
    // the engines write their times in UTC; the server rewrote them in the box's own time
    const n = d.local_stamps || 0;
    show('log-note', n > 0);
    setText('log-note', n ? `Times shown in this box's local time${d.tz ? ' (' + d.tz + ')' : ''}: the engine writes them in UTC.` : '');
  }catch(e){ logLines = []; show('log-note', false); setText('log-view', `Could not read ${src}: ${e.message}`); }
}
function wireLogs(){
  $('log-btn').addEventListener('click', () => { setText('log-view', 'Reading…'); tailLog(); });
  $('log-src').addEventListener('change', () => { logTouched = true; tailLog(); });
  $('log-filter').addEventListener('input', drawLog);
  $('log-copy').addEventListener('click', () => copyText(logLines.join('\n')));
  $('log-follow').addEventListener('change', () => {
    if (followTimer){ clearInterval(followTimer); followTimer = null; }
    if ($('log-follow').checked){ tailLog(); followTimer = setInterval(() => { if (activeView === 'logs' && !document.hidden) tailLog(); }, 3000); }
  });
}
// The source the page sends you to: the lane's journal when it has no container or failed.
function defaultLogSource(){
  const s = servingEngine(); const u = LOGS_WANT.src || (s ? s[0] : enabledUnit());
  const e = engines()[u] || {};
  if (u === IMAGE_UNIT || u === VIDEO_UNIT || u === PROXY_UNIT || e.state === 'failed' || e.restarting) return u;
  return u === UFLASH ? 'qwen38-flash' : 'qwen38-sglang';
}
onShow('logs', () => {
  if (LOGS_WANT.src || !logTouched){ $('log-src').value = defaultLogSource(); LOGS_WANT.src = null; }
  tailLog();
});
// jobs history: one row per job, kept by id and updated in place
const JOB_ROWS = new Map();
on('job', d => {
  const hist = $('job-hist'); const all = (d.current ? [d.current] : []).concat(d.recent || []);
  const ids = new Set(all.map(j => j.id));
  JOB_ROWS.forEach((r, id) => { if (!ids.has(id)){ r.row.remove(); JOB_ROWS.delete(id); } });
  const none = hist.querySelector('.none');
  if (!all.length){ if (!none) hist.append(el('p', 'none faint', 'No job has run since the cockpit started.')); } else if (none) none.remove();
  all.forEach((j, i) => {
    let r = JOB_ROWS.get(j.id);
    if (!r){
      r = {row: el('div'), lamp: el('span', 'lamp'), what: el('span'), when: el('span', 'faint num'), btn: el('button', 'btn sm ghost', 'Log')};
      r.row.style.cssText = 'display:grid; grid-template-columns:auto 1fr auto auto; gap:var(--s-3); align-items:center; padding:10px 0; border-top:1px solid var(--rule-1); font-size:var(--t-14)';
      r.btn.type = 'button'; r.btn.addEventListener('click', () => showJobLog(j.id));
      r.row.append(r.lamp, r.what, r.when, r.btn); JOB_ROWS.set(j.id, r);
    }
    r.lamp.className = 'lamp ' + (j.status === 'running' ? 'warn live' : j.status === 'done' ? 'ok' : 'err');
    setText(r.what, describeJob(j)); setText(r.when, clockTime(j.started) + ', ' + fmtDur(j.elapsed));
    if (hist.children[i] !== r.row) hist.insertBefore(r.row, hist.children[i] || null);
  });
});
async function showJobLog(id){
  const v = $('job-hist-log'); v.hidden = false; v.textContent = 'Loading…';
  try{ const r = await fetch('/api/jobs/' + id); if (r.status === 401) return login();
    if (r.status === 404){ v.textContent = 'This job is no longer in memory (the cockpit restarted); the audit log has its outcome.'; return; }
    const j = await r.json(); v.textContent = `$ ${(j.argv || [j.action]).join(' ')}\n` + (j.lines || []).join('\n') + `\n[${j.status}${j.rc != null ? ', exit code ' + j.rc : ''}, ${fmtDur(j.elapsed)}]`; }
  catch(e){ v.textContent = 'Could not load the job: ' + e.message; }
}
afterApply(() => { if (activeView === 'logs') renderEvents($('log-events'), 40); });
onShow('logs', () => renderEvents($('log-events'), 40));
on('agent', d => { const o = $('log-opt-agent'); if (o) o.hidden = !d.enabled; });

// ── Settings ──────────────────────────────────────────────────────────────────
on('opencode', d => {
  const bad = d.real.error ? 'unknown: the config does not parse' : null;
  const lim = l => bad || (l && l.context ? `${fmtN(l.context)} context, ${fmtN(l.output || 0)} out` : 'not declared');
  setText('oc-lim27', lim(d.real.limits['qwen38/qwen3.8-27b'])); setText('oc-limflash', lim(d.real.limits['flashnext/qwen3.8-flash-next']));
  if (!d.enabled){
    cap('oc-cap', 'off', ''); setText('oc-state', 'off (installed with --no-opencode)' + (d.off_note ? `, ${d.off_note}` : ''));
    setText('oc-default', d.real.present ? (bad || d.real.default || 'none') + ' (your own config, never touched)' : 'no opencode config on this box');
    setText('oc-launcher', d.launcher.present ? (d.launcher.ours ? 'this repo’s oc is still there (stale)' : 'a foreign oc') : 'none');
    show('oc-cmd', false); setText('oc-note', 'Switches leave your opencode default model alone. Turn the integration back on with ./install.sh --with-opencode');
    badge('settings', '', ''); return;
  }
  const ok = d.follows;
  cap('oc-cap', ok === true ? 'follows the lane' : ok === false ? 'differs' : 'on', ok === true ? 'ok' : ok === false ? 'warn' : '');
  setText('oc-state', 'on, config ' + (d.real.error ? `present but unreadable (${d.real.error})` : d.real.present ? 'present' : 'missing (copy ~/.config/qwen38/opencode.json to ~/.config/opencode/)'));
  setText('oc-default', (bad || d.real.default || 'none') + ': ' + d.why);
  setText('oc-launcher', d.launcher.present ? (d.launcher.ours ? `oc, output cap ${d.launcher.cap ? fmtN(d.launcher.cap) : '?'}` : 'a foreign oc, launcher not installed') : 'missing (re-run ./install.sh)');
  const f = d.fit;
  setText('oc-fit', !f ? 'unknown until an engine serves' : f.ok ? `fits: prompt up to ${fmtN(f.context)} of ${fmtN(f.prompt_cap)}, with the answer ${fmtN(f.worst)} of ${fmtN(f.usable)}`
    : `too large, ${f.why}: ${fmtN(f.asked)} asked, ${fmtN(f.limit)} servable`);
  $('oc-fit').className = 'wrap' + (f && !f.ok ? ' warn-t' : '');
  show('oc-cmd', true);
  setText('oc-note', 'One command: the launcher lifts the output cap and auto-approves permissions. The default model follows every switch; running sessions keep the model they started with.');
  badge('settings', ok === false ? '!' : '', 'warn');
});
on('repo', d => {
  setText('rp-tag', d.tag || 'n/a'); setText('rp-branch', d.branch || 'n/a');
  setText('rp-head', (d.head || '').split(' ')[0] || 'n/a'); setTitle('rp-head', d.head || '');
  setText('rp-dirty', d.dirty == null ? 'unknown (git did not answer)' : d.dirty ? 'modified, uncommitted changes' : d.untracked ? `clean, ${d.untracked} untracked` : 'clean');
  setText('rp-proxy', F.proxy && F.proxy.version ? F.proxy.version + (F.proxy.same_as_repo === false ? ', differs from the repo copy' : ', the repo copy') : 'unknown');
});
on('update', d => {
  const parts = [];
  if (d.installed) parts.push(d.installed);
  if (d.checked === false) parts.push('the update check is off');
  else if (!d.latest) parts.push('latest release unknown (offline)');
  else if (d.behind) parts.push(`${d.latest} is out: git pull && ./install.sh`);
  else parts.push('up to date');
  setText('rp-upd', parts.join(', '));
});
on('config', d => {
  setText('ck-ver', d.version || '?');
  setText('ck-mode', d.dry_run ? 'dry run: logged and audited, nothing executed' : 'live: actions execute after your confirmation');
  setText('ck-usable', Math.round((d.usable_frac || F.usable) * 100) + ' % of the KV pool for one prompt');
  const per = d.periods || {}, pv = Object.values(per);
  setText('ck-periods', pv.length ? `${pv.length} collectors, every ${Math.min(...pv)} to ${Math.max(...pv)} s` : '…');
  setTitle('ck-periods', Object.entries(per).map(([k, v]) => `${k} ${v} s`).join(', '));
  setText('st-update', (d.terminal_only || {}).update_stack || 'cd <repo> && ./install.sh');
});
afterApply(() => { setText('ck-ceiling', !textReady() ? 'set when a text lane serves' : F.ceiling > 0 ? fmtN(F.ceiling) + ' tokens (proxy)' : 'none: the pool share only'); });
function wireOps(){
  $('rcp-btn').addEventListener('click', loadRecipes);
  $('reg-btn').addEventListener('click', scanRegistry);
  $('up-btn').addEventListener('click', checkUpstream);
  $('inv-btn').addEventListener('click', scanInventory);
  $('oc-fitbtn').dataset.act = 'fit_opencode';
  $('oc-fitbtn').addEventListener('click', () => askAction('fit_opencode', {}, []));
  wireLogs();
}
