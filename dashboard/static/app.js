"use strict";
const $ = id => document.getElementById(id);
const GB = 1024**3;
const fmtB = b => b==null ? '...' : (b/GB).toFixed(1)+' GB';

// ── tiny sparkline store ────────────────────────────────────────────────────
const series = {};
function push(name, v, max=120){
  (series[name] = series[name] || []).push(v);
  if (series[name].length > max) series[name].shift();
}
function drawSpark(canvas, name, color, yMax){
  const c = canvas.getContext('2d'), w = canvas.width, h = canvas.height;
  const data = series[name] || [];
  c.clearRect(0,0,w,h);
  if (data.length < 2) return;
  const m = yMax || Math.max(...data, 1e-9);
  c.beginPath();
  data.forEach((v,i)=>{
    const x = i*(w/(data.length-1)), y = h - Math.min(v/m,1)*(h-6) - 3;
    i ? c.lineTo(x,y) : c.moveTo(x,y);
  });
  c.strokeStyle = color; c.lineWidth = 2.5; c.stroke();
  c.lineTo(w,h); c.lineTo(0,h); c.closePath();
  c.globalAlpha = .12; c.fillStyle = color; c.fill(); c.globalAlpha = 1;
}
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

// ── panel renderers (each isolated: one failure never blanks the rest) ─────
function rMachine(d){
  const m = d.mem || {};
  const used = m.MemTotal - m.MemAvailable;
  const pct = 100*used/m.MemTotal;
  $('memlab').textContent = fmtB(used)+' / '+fmtB(m.MemTotal);
  $('memfill').style.width = pct.toFixed(1)+'%';
  $('memgauge').className = 'gauge'+(pct>90?' crit':pct>80?' warn':'');
  $('memchip').textContent = fmtB(m.MemAvailable)+' free';
  $('memchip').className = 'chip '+(m.MemAvailable<8*GB?'err':m.MemAvailable<15*GB?'warn':'ok');
  $('memavail').textContent = fmtB(m.MemAvailable);
  $('memcache').textContent = fmtB(m.Cached);
  $('swap').textContent = fmtB((m.SwapTotal||0)-(m.SwapFree||0));
  push('mem', used/GB); drawSpark($('memspark'),'mem',css('--acc'), m.MemTotal/GB);
  const cpu = d.cpu_pct || {};
  $('cpuchip').textContent = (cpu.cpu??0).toFixed(0)+' %';
  $('cpuchip').className = 'chip '+((cpu.cpu||0)>85?'warn':'ok');
  $('loads').textContent = (d.load||[]).map(x=>x.toFixed(2)).join(' / ');
  $('cores').textContent = Object.keys(cpu).length-1;
  push('cpu', cpu.cpu||0); drawSpark($('cpuspark'),'cpu',css('--lane27'),100);
}
function rGpu(d){
  $('gpupower').textContent = d.power_w!=null ? d.power_w.toFixed(1)+' W' : 'n/a';
  $('gputemp').textContent = d.temp_c!=null ? d.temp_c.toFixed(0)+' °C' : 'n/a';
  $('gpuchip').textContent = (d.procs||[]).length+' proc';
  $('gpuchip').className = 'chip '+(d.temp_c>85?'err':d.temp_c>75?'warn':'ok');
  push('pow', d.power_w||0); drawSpark($('powspark'),'pow',css('--warn'));
  const tb = $('gpuprocs').tBodies[0]; tb.innerHTML='';
  (d.procs||[]).slice(0,5).forEach(p=>{
    const tr = tb.insertRow();
    tr.insertCell().textContent = p.name||'?';
    tr.insertCell().textContent = p.pid;
    const c = tr.insertCell(); c.textContent = p.mem; c.className='r num';
  });
}
function rEngineInfo(d){
  const i = d.info || {};
  $('engmodel').textContent = (i.model_path||'...').split('/').pop();
  $('engrev').textContent = (i.revision||'').slice(0,12);
  $('engquant').textContent = i.quantization ?? '...';
  $('engctx').textContent = i.context_length?.toLocaleString('en') ?? '...';
  $('engspec').textContent = i.speculative_algorithm
      ? `${i.speculative_algorithm} ${i.speculative_num_steps}/${i.speculative_num_draft_tokens}` : '...';
  $('engattn').textContent = i.prefill_attention_backend
      ? `${i.prefill_attention_backend} / ${i.decode_attention_backend}` : (i.attention_backend??'...');
  $('engradix').textContent = i.mamba_radix_cache_strategy ?? '...';
  $('engver').textContent = i.version ?? '...';
  if (i.max_total_num_tokens) POOL = i.max_total_num_tokens;
}
let POOL = null;   // max_total_num_tokens from engine info
function rPool(l){
  if (!POOL) { $('poollab').textContent = '...'; return; }
  const held = l.num_tokens || 0, pct = 100*held/POOL;
  $('poollab').textContent = held.toLocaleString('en') + ' / ' + POOL.toLocaleString('en') + ' tokens';
  $('poolfill').style.width = Math.min(100, pct).toFixed(1) + '%';
  $('poolgauge').className = 'gauge' + (pct>90 ? ' crit' : pct>70 ? ' warn' : '');
  $('poolnote').textContent = pct>70
    ? 'one more large context will not fit: the scheduler queues it (max-running-requests 1)'
    : 'a single prompt tops out near ' + Math.round(POOL*0.94/1000) + 'K tokens on this pool';
}
const WINDOW = 262144;
function rReservoir(l){
  if (!POOL){
    $('resbig').textContent = '...'; $('restick').style.display = 'none';
    $('reslegend').innerHTML = '<span>the pool size arrives with the engine (max_total_num_tokens at boot)</span>';
    return;
  }
  $('restick').style.display = '';
  const held = l.num_tokens || 0, single = Math.round(POOL*0.94);
  const scale = Math.max(WINDOW, POOL);
  const pct = 100*held/POOL;
  $('resbig').innerHTML = held.toLocaleString('en') + `<small id="rescap">of ${POOL.toLocaleString('en')} tokens</small>`;
  $('rescapzone').style.width = (100*POOL/scale).toFixed(2)+'%';
  $('reslevel').style.width = (100*Math.min(held,POOL)/scale).toFixed(2)+'%';
  $('reslevel').className = 'level' + (pct>70 ? ' hot' : '');
  $('restick').style.left = (100*single/scale).toFixed(2)+'%';
  $('resghost').style.width = (100*Math.max(0, WINDOW-POOL)/scale).toFixed(2)+'%';
  $('reslegend').innerHTML =
    `<span><b>${pct.toFixed(0)}%</b> held` + ((l.num_reqs||0) ? ` by ${l.num_reqs} request${l.num_reqs>1?'s':''}` : '') + `</span>`
    + `<span>one prompt tops out near <b>${Math.round(single/1000)}K</b> (tick)</span>`
    + `<span>capacity <b>${Math.round(POOL/1000)}K</b> at these memory settings</span>`
    + `<span>model window <b>${Math.round(WINDOW/1000)}K</b>: the hatched red zone never fits</span>`;
}
function rHero(){
  const life = window._life || {}; const eng = life.engines || {};
  const serving = Object.entries(eng).find(([n,e]) => e.state !== 'stopped' && e.state !== 'failed');
  if (!serving) { $('hero').textContent = ''; return; }
  const [name, e] = serving; const lane = name.includes('flash') ? 'flash 176B' : '27B';
  const l = window._load || {}; const pct = POOL ? Math.round(100*(l.num_tokens||0)/POOL) : null;
  $('hero').innerHTML = `<b>${lane}</b> <span class="chip ${STATE_CHIP[e.state] ?? 'warn'}">${e.state}</span>`
    + (pct!=null ? ` <span class="num">pool ${pct}%</span>` : '')
    + ((l.num_reqs||0) ? ` <span class="num">${l.num_reqs} running</span>` : '');
}
function rEngineFast(d){
  const l = (d.load||[])[0] || {};
  window._load = l; rPool(l); rReservoir(l); rHero();
  // A container burning CPU with health still down is a BOOT, not an outage
  // (weight loads are journal-silent for many minutes; field lesson).
  const life = window._life || {};
  const engs = Object.values(life.engines||{});
  const boot = engs.find(e=>TRANSITIONAL.has(e.state));
  const c = window._containers || {};
  const busy = Object.values(c).some(x => parseFloat(x.cpu) > 15);
  const state = d.healthy ? 'healthy' : boot ? boot.state : busy ? 'booting' : 'down';
  $('engchip').textContent = state;
  $('engchip').className = 'chip '+(d.healthy?'ok':(boot||busy)?'warn':'err');
  $('reqrun').textContent = l.num_reqs ?? '...';
  $('reqwait').textContent = l.num_waiting_reqs ?? '...';
  $('reqtok').textContent = (l.num_tokens??0).toLocaleString('en');
  $('loadchip').textContent = (l.num_reqs||0)>0 ? 'active' : 'idle';
  $('loadchip').className = 'chip '+((l.num_reqs||0)>0?'flash':'');
  push('req', l.num_reqs||0); drawSpark($('reqspark'),'req',css('--flash'),4);
}
function rDecode(d){
  const t = d.decode;
  $('acclen').textContent = t ? t.accept_len.toFixed(2) : 'idle';
  $('kvusage').textContent = t ? (100*t.token_usage).toFixed(1)+' %' : '...';
}
function fmtSince(s){
  // systemd: 'Sat 2026-08-29 01:34:33 CEST' -> 'since 01:34' today, else 'since 08-29 01:34'
  const m = (s||'').match(/(\d{4})-(\d{2})-(\d{2}) (\d{2}:\d{2})/); if(!m) return '';
  const today = new Date().toISOString().slice(0,10) === `${m[1]}-${m[2]}-${m[3]}`;
  return 'since ' + (today ? m[4] : `${m[2]}-${m[3]} ${m[4]}`);
}
function rUnits(d){
  window._units = d.units || {};
  const box = $('unitlist'); box.innerHTML='';
  const lane = n => n.includes('flash') ? 'flash' : n.includes('sglang') ? 'lane27' : '';
  Object.entries(d.units||{}).filter(([n])=>n.includes('keepalive')).forEach(([name,u])=>{
    const row = document.createElement('div'); row.className='unit';
    const on = u.active==='active';
    row.innerHTML =
      `<span class="chip ${on?'ok':u.active==='failed'?'err':''}">${u.active}</span>`+
      `<span class="name">${name.replace('.service','')}</span>`+
      `<span class="chip ${lane(name)}">${u.enabled}</span>`+
      `<span class="since">${fmtSince(u.since)}</span>`;
    const btn = document.createElement('button');
    btn.className = 'btn mini'+(on?' danger':'');
    btn.textContent = on ? 'stop' : 'start';
    btn.onclick = () => askAction('unit',
      {verb: on?'stop':'start', unit: name},
      ['sudo','-n','/usr/bin/systemctl', on?'stop':'start', name]);
    row.appendChild(btn);
    box.appendChild(row);
  });
}
function rContainers(d){
  window._containers = d.containers || {};
  const tb = $('ctable').tBodies[0]; tb.innerHTML='';
  Object.entries(d.containers||{}).forEach(([n,c])=>{
    const tr = tb.insertRow();
    tr.insertCell().textContent = n;
    const a = tr.insertCell(); a.textContent=c.cpu; a.className='r num';
    const b = tr.insertCell(); b.textContent=c.mem; b.className='r num';
  });
  if(!tb.rows.length){const tr=tb.insertRow();const c=tr.insertCell();c.colSpan=3;c.textContent='no serving container running';c.style.color='var(--mut)';}
}
function rFeed(d){
  const tb = $('feedtable').tBodies[0]; tb.innerHTML='';
  const rows = (d.rows||[]).slice().reverse();
  rows.forEach(r => {
    const tr = tb.insertRow();
    tr.insertCell().textContent = (r.ts||'').slice(11,19);
    const c1 = tr.insertCell(); c1.textContent = r.peer; c1.className='num';
    tr.insertCell().textContent = r.path;
    const c2 = tr.insertCell(); c2.textContent = r.bytes>=1024 ? (r.bytes/1024).toFixed(0)+' KB' : r.bytes+' B'; c2.className='r num';
    const c3 = tr.insertCell(); c3.textContent = r.secs!=null ? r.secs.toFixed(1)+' s' : ''; c3.className='r num';
    const cls = r.outcome.startsWith('ok') ? 'ok' : r.outcome==='in flight' ? 'flash' : 'err';
    tr.insertCell().innerHTML = `<span class="chip ${cls}">${r.outcome}</span>`;
  });
  const inflight = rows.filter(r=>r.outcome==='in flight').length;
  $('feedchip').textContent = inflight ? inflight+' in flight' : 'idle';
  $('feedchip').className = 'chip ' + (inflight ? 'flash' : '');
  if(!rows.length){const tr=tb.insertRow();const c=tr.insertCell();c.colSpan=6;c.textContent='no request seen yet';c.style.color='var(--mut)';}
}
function rRepo(d){
  $('repotag').textContent = d.tag||'...';
  $('repobranch').textContent = d.branch||'...';
  $('repohead').textContent = (d.head||'').slice(0,46);
  $('repodirty').textContent = d.dirty ? 'modified' : 'clean';
  $('repoline').textContent = `${d.tag||''} · ${d.branch||''}`;
}

const STAGE_LABEL = {'init':'init','loading-weights':'weights',
  'loading-draft':'draft','allocating-kv':'KV','capturing-graphs':'graphs',
  'warming-up':'warmup'};
const ALL_STAGES = Object.keys(STAGE_LABEL);
const TRANSITIONAL = new Set(['starting','loading-weights','loading-draft',
  'allocating-kv','capturing-graphs','warming-up']);
const STATE_CHIP = {ready:'ok', degraded:'warn', failed:'err', stopped:'',
                    stopping:'warn', wedged:'err'};
const fmtDur = s => s==null ? '?' : s<90 ? Math.round(s)+' s'
  : Math.floor(s/60)+' min '+String(Math.round(s%60)).padStart(2,'0');

function rLifecycle(d){
  window._life = d; rHero();
  const box = $('enginelist'); box.innerHTML='';
  const units = window._units || {};
  Object.entries(d.engines||{}).forEach(([name, e])=>{
    const lane = name.includes('flash') ? 'flash' : 'lane27';
    const row = document.createElement('div'); row.className='eng';
    const booting = TRANSITIONAL.has(e.state);
    const chipCls = STATE_CHIP[e.state] ?? 'warn';
    const top = document.createElement('div'); top.className='top';
    top.innerHTML =
      `<span class="chip ${chipCls}">${e.state}</span>`+
      (e.rebuild?'<span class="chip warn">rebuilding PLE table</span>':'')+
      (e.overdue?'<span class="chip err">overdue, check logs</span>':'')+
      `<span class="name">${name.replace('.service','')}</span>`+
      `<span class="chip ${lane}">${(units[name]||{}).enabled||''}</span>`+
      `<span class="since">${e.state==='ready'&&e.elapsed?'up '+fmtDur(e.elapsed):''}</span>`;
    const on = e.state !== 'stopped' && e.state !== 'failed';
    const btn = document.createElement('button');
    btn.className = 'btn mini'+(on?' danger':'');
    btn.textContent = on ? 'stop' : 'start';
    const blockKey = `unit:start:${name}`;
    const blocked = !on && (d.blocked||{})[blockKey];
    if (blocked) btn.disabled = true;
    btn.onclick = () => askAction('unit',
      {verb: on?'stop':'start', unit: name},
      ['sudo','-n','/usr/bin/systemctl', on?'stop':'start', name],
      on && booting && name.includes('flash')
        ? ['stopping mid-boot marks the PLE table dirty: the NEXT boot rebuilds it (~12 min)'] : []);
    top.appendChild(btn);
    row.appendChild(top);
    if (!booting && (e.boots||[]).length){
      const hist = document.createElement('div'); hist.className='blockedwhy';
      hist.textContent = 'last boots: ' + e.boots.slice().reverse().map(fmtDur).join(', ')
        + ((e.boots_rebuild||[]).length ? '  (with table rebuild: ' + e.boots_rebuild.slice().reverse().map(fmtDur).join(', ') + ')' : '');
      row.appendChild(hist);
    }
    if (blocked){
      const why = document.createElement('div'); why.className='blockedwhy';
      why.textContent = 'start blocked: ' + blocked[0];
      row.appendChild(why);
    }
    if (booting){
      const doneN = (e.stage_done||[]).length;
      const stage = ALL_STAGES[doneN] || 'init';
      const pct = e.eta && e.elapsed ? Math.min(97, 100*e.elapsed/e.eta)
                : Math.min(95, 8 + doneN*(84/ALL_STAGES.length));
      const eta = e.eta && e.elapsed ? Math.max(0, e.eta - e.elapsed) : null;
      const boot = document.createElement('div'); boot.className='boot';
      boot.innerHTML =
        `<div class="bbar"><div class="bfill" style="width:${pct.toFixed(1)}%"></div></div>`+
        `<div class="stages">`+ALL_STAGES.map((st,i)=>
          `<span class="stage ${i<doneN?'done':st===stage?'now':''}">${STAGE_LABEL[st]}</span>`).join('')+`</div>`+
        `<div class="blab"><span>${stage} · ${fmtDur(e.elapsed)} elapsed</span>`+
        `<span>${eta!=null?'~'+fmtDur(eta)+' left':'first boot: learning duration'}</span></div>`;
      row.appendChild(boot);
    }
    box.appendChild(row);
  });
  const el = $('evtlist');
  const evs = (d.events||[]).slice().reverse();
  if (evs.length){
    el.innerHTML = evs.slice(0,12).map(ev=>{
      const t = new Date(ev.ts*1000).toLocaleTimeString();
      return `<div class="evt"><time>${t}</time><span>${ev.msg}</span></div>`;
    }).join('');
  }
}

const RENDER = {machine:rMachine, gpu:rGpu, engine_info:rEngineInfo,
                engine_fast:rEngineFast, decode:rDecode, units:rUnits,
                containers:rContainers, repo:rRepo, lifecycle:rLifecycle, feed:rFeed};

// skeleton placeholders: every '...' shimmers until real data lands
document.querySelectorAll('dd, .chip, .num').forEach(el => {
  if (el.textContent.trim() === '...') el.classList.add('skel');
});
function apply(state){
  for (const [name, wrap] of Object.entries(state)){
    const fn = RENDER[name]; if(!fn) continue;
    try{
      if (wrap.data && wrap.data.error) throw new Error(wrap.data.error);
      fn(wrap.data||{});
    }catch(e){ /* isolated: leave last good render, log once */ console.warn(name, e.message); }
  }
  document.querySelectorAll('.skel').forEach(el => {
    if (el.textContent.trim() !== '...') el.classList.remove('skel');
  });
}

// ── SSE with reconnect + polling fallback ──────────────────────────────────
let es = null, pollTimer = null;
function setConn(on, label){
  $('conndot').className = 'dot'+(on?' on':'');
  $('connlabel').textContent = label;
}
function startPolling(){
  if (pollTimer) return;
  pollTimer = setInterval(async ()=>{
    try{
      const r = await fetch('/api/state');
      if (r.status===401) location.href='/login';
      apply(await r.json()); setConn(true,'polling');
    }catch{ setConn(false,'offline'); }
  }, 2000);
}
function connect(){
  es = new EventSource('/api/stream');
  es.onopen = ()=>{ setConn(true,'live'); if(pollTimer){clearInterval(pollTimer);pollTimer=null;} };
  es.onmessage = ev => apply(JSON.parse(ev.data));
  es.onerror = ()=>{ setConn(false,'reconnecting'); es.close(); startPolling(); setTimeout(connect, 3000); };
}
fetch('/api/state').then(r=>{ if(r.status===401){location.href='/login';return;}
  return r.json();}).then(s=>{ if(s) apply(s); connect(); })
  .catch(()=>{ setConn(false,'offline'); startPolling(); });

// ── actions: confirm modal with the exact command, CSRF, one job at a time ──
let pendingRun = null;
function askAction(name, params, argv, warns){
  $('mtitle').textContent = 'Confirm: ' + name;
  const what = Object.entries(params||{}).map(([k,v])=>k+' = '+v).join('   ') || 'no parameters';
  $('mwhat').textContent = what + ((warns&&warns.length)?'\n\u26a0 '+warns.join('\n\u26a0 '):'');
  $('mwhat').style.whiteSpace = 'pre-line';
  $('margv').textContent = argv.join(' ');
  $('modal').hidden = false;
  pendingRun = () => runAction(name, params);
}
$('mcancel').onclick = () => { $('modal').hidden = true; pendingRun = null; };
$('mgo').onclick = () => { $('modal').hidden = true; const f = pendingRun; pendingRun = null; if (f) f(); };
$('modal').addEventListener('click', e => { if (e.target === $('modal')) $('mcancel').onclick(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('modal').hidden) $('mcancel').onclick(); });

async function runAction(name, params){
  const chip = $('jobchip'), log = $('joblog');
  chip.textContent = 'running'; chip.className = 'chip warn';
  log.hidden = false; log.textContent = '$ ' + name + ' ' + JSON.stringify(params||{}) + '\n';
  try{
    const t = await (await fetch('/api/csrf', {method:'POST'})).json();
    const r = await fetch('/api/action', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, params, csrf: t.token})});
    const out = await r.json();
    if (r.status === 409 && out.reasons){
      log.textContent += 'BLOCKED: ' + out.reasons.join('; ') + '\n';
      chip.textContent = 'blocked'; chip.className = 'chip warn';
      return;
    }
    if (r.status === 202 && out.job){
      (out.warnings||[]).forEach(w => log.textContent += 'warning: ' + w + '\n');
      log.textContent += out.argv.join(' ') + '\n';
      return pollJob(out.job);
    }
    log.textContent += JSON.stringify(out, null, 1) + '\n';
    chip.textContent = r.ok && out.ok !== false ? 'done' : 'failed';
    chip.className = 'chip ' + (r.ok && out.ok !== false ? 'ok' : 'err');
  }catch(e){
    log.textContent += 'request failed: ' + e.message + '\n';
    chip.textContent = 'failed'; chip.className = 'chip err';
  }
}
async function pollJob(id){
  const chip = $('jobchip'), log = $('joblog');
  let shown = 0;
  for(;;){
    await new Promise(res => setTimeout(res, 1200));
    let j;
    try{ j = await (await fetch('/api/jobs/' + id)).json(); }
    catch{ continue; }
    const lines = j.lines || [];
    if (lines.length > shown){
      log.textContent += lines.slice(shown).join('\n') + '\n';
      shown = lines.length;
      log.scrollTop = log.scrollHeight;
    }
    if (j.status !== 'running'){
      chip.textContent = j.status + (j.rc!=null ? ' (rc '+j.rc+')' : '');
      chip.className = 'chip ' + (j.status === 'done' ? 'ok' : 'err');
      return;
    }
  }
}
$('logbtn').onclick = async () => {
  const v = $('logview'); v.hidden = false; v.textContent = 'loading...';
  try{
    const d = await (await fetch('/api/logs/' + $('logsel').value)).json();
    v.textContent = (d.lines||[]).join('\n') || '(empty)';
    v.scrollTop = v.scrollHeight;
  }catch(e){ v.textContent = 'failed: ' + e.message; }
};
const fmtGiB = b => (b/1024**3).toFixed(1)+' GiB';
$('upbtn').onclick = async () => {
  $('upbtn').textContent = 'checking...';
  try{
    const d = await (await fetch('/api/upstream')).json();
    const tb = $('uptable').tBodies[0]; tb.innerHTML='';
    (d.models||[]).forEach(m => {
      const tr = tb.insertRow();
      tr.insertCell().textContent = m.model.split('/').pop();
      const a1 = tr.insertCell(); a1.textContent = m.pin; a1.className='num';
      const a2 = tr.insertCell(); a2.textContent = m.upstream || '?'; a2.className='num';
      const cls = m.status==='same' ? 'ok' : m.status==='moved' ? 'warn' : 'err';
      tr.insertCell().innerHTML = `<span class="chip ${cls}">${m.status}</span>`;
    });
    const r = d.release || {};
    $('upline').textContent = r.latest
      ? `repo release: local ${r.local}, latest published ${r.latest}` +
        (r.latest === r.local ? ' (up to date)' : ' (update available)')
      : 'release check offline';
    const moved = (d.models||[]).filter(m=>m.status==='moved').length;
    $('upbtn').textContent = moved ? moved+' moved' : 'all same';
  }catch{ $('upbtn').textContent = 'check failed'; }
};
$('regbtn').onclick = async () => {
  $('regbtn').textContent = 'scanning...';
  try{
    const d = await (await fetch('/api/registry')).json();
    const tb = $('regmodels').tBodies[0]; tb.innerHTML='';
    (d.models||[]).forEach(m => (m.revisions||[]).forEach((r,i) => {
      const tr = tb.insertRow();
      tr.insertCell().textContent = i ? '' : m.repo_id.split('/').pop();
      const c1 = tr.insertCell(); c1.textContent = r.rev.slice(0,10); c1.className='num';
      const c2 = tr.insertCell(); c2.textContent = fmtGiB(r.bytes); c2.className='r num';
      const c3 = tr.insertCell(); c3.textContent = i ? '' : fmtGiB(m.disk_bytes); c3.className='r num';
      const cls = r.status==='pinned' ? 'ok' : r.status==='stray' ? 'warn' : '';
      tr.insertCell().innerHTML = `<span class="chip ${cls}">${r.status}</span>`+(r.pin?` <span class="chip">${r.pin}</span>`:'');
    }));
    const ti = $('regimages').tBodies[0]; ti.innerHTML='';
    (d.images||[]).forEach(im => {
      const tr = ti.insertRow();
      tr.insertCell().textContent = im.ref;
      const a2 = tr.insertCell(); a2.textContent = im.size; a2.className='r num';
      const b2 = tr.insertCell(); b2.textContent = im.id; b2.className='r num';
    });
    const strays = (d.models||[]).flatMap(m=>m.revisions).filter(r=>r.status==='stray');
    $('regbtn').textContent = strays.length ? strays.length+' stray rev' : 'clean';
  }catch{ $('regbtn').textContent = 'scan failed'; }
};
$('invbtn').onclick = async () => {
  $('invbtn').textContent = 'scanning...';
  try{
    const d = await (await fetch('/api/inventory')).json();
    const tb = $('invtable').tBodies[0]; tb.innerHTML='';
    (d.items||[]).forEach(i=>{
      const tr = tb.insertRow();
      const k = tr.insertCell(); k.innerHTML = `<span class="chip">${i.kind}</span>`;
      tr.insertCell().textContent = i.what;
    });
    $('invbtn').textContent = (d.items||[]).length + ' items';
  }catch{ $('invbtn').textContent = 'scan failed'; }
};
document.querySelectorAll('.btn[data-act]').forEach(b => {
  b.onclick = () => {
    const act = b.dataset.act;
    if (act === 'switch'){
      const target = $('switchsel').value;
      askAction('switch', {target},
        ['bash','switch-model.sh', target]);
    } else if (act === 'update_stack'){
      askAction('update_stack', {}, ['bash','install.sh','(converging upgrade)']);
    } else {
      askAction(act, {}, ['engine:', act]);
    }
  };
});
