"use strict";
/* Agent: opencode's own web interface, framed from the relay behind this login.

   The relay lives on this same host (cookies ignore ports), so the frame carries the
   cockpit session and opencode never asks for its own password. The frame is created
   the first time the view opens and then kept: leaving the view must not lose a session. */

const AG = {mounted: false, wasReady: null};
// An explicit fullscreen choice wins on every device; with none stored, a hand-held
// viewport gets fullscreen, where the head above the frame is a third of the screen.
// The choice made on this page holds even where storage throws (private mode).
let agentMaxChoice = null;
function verCmp(a, b){
  const x = String(a).split('.').map(Number), y = String(b).split('.').map(Number);
  for (let i = 0; i < Math.max(x.length, y.length); i++){ const d = (x[i] || 0) - (y[i] || 0); if (d) return d; }
  return 0;
}
function agentMaxDefault(){
  if (agentMaxChoice !== null) return agentMaxChoice;
  let pref = null;
  try { pref = localStorage.getItem('cockpit.agent.max'); } catch { /* storage may be unavailable */ }
  if (pref === '1') return true;
  if (pref === '0') return false;
  return window.matchMedia('(max-width:980px)').matches;
}
const agentUrl = () => F.agent && F.agent.relay && F.agent.relay.port ? `http://${location.hostname}:${F.agent.relay.port}/` : null;
const agentReady = () => !!(F.agent && F.agent.enabled && F.agent.relay && F.agent.relay.listening && F.agent.server && F.agent.server.healthy);
function agentMessage(text, cmd){
  show('ag-msg', true); setText('ag-msg-text', text);
  const c = $('ag-cmd'); c.hidden = !cmd; if (cmd) c.textContent = cmd;
}
function mountAgent(){
  if (!agentReady()) return;
  if (!AG.mounted){
    const f = document.createElement('iframe');
    f.id = 'ag-iframe'; f.title = 'opencode'; f.src = agentUrl();
    f.setAttribute('allow', 'clipboard-read; clipboard-write'); f.setAttribute('referrerpolicy', 'no-referrer');
    $('ag-frame').append(f); $('ag-frame').classList.add('mounted'); AG.mounted = true;
    if (activeView === 'agent') fitAgentFrame();   // once, when the frame first exists: never on a refresh
  }
  show('ag-msg', false);
}
function reloadAgent(){ const f = $('ag-iframe'); if (f) f.src = agentUrl(); else mountAgent(); }
// Never cover the reason the panel is empty: fullscreen over an explanation is a blank screen.
function applyAgentMax(){
  if (!agentMaxDefault() || document.body.classList.contains('agentmax')) return;
  if (!agentReady() || !$('ag-note').hidden || !$('ag-msg').hidden) return;
  setAgentMax(true, false);
}
// The inline frame fills the screen below where it really starts, so opencode's composer,
// at its foot, is on screen. It was sized on a guess of 150 px above it, which left the
// composer under the fold on a phone, a tablet and a laptop (2026-09-30). When too little
// room is left under the view's buttons (a phone on its side), the frame is a screen tall
// and the page brings it under the head instead.
function fitAgentFrame(scroll = true){
  const f = $('ag-frame');
  if (!f || !f.classList.contains('mounted') || document.body.classList.contains('agentmax') || activeView !== 'agent') return;
  const vh = window.innerHeight, head = $('spine').getBoundingClientRect().height;
  const top = f.getBoundingClientRect().top + window.scrollY, room = Math.floor(vh - top - 16);
  f.classList.add('fitted');
  if (room >= vh * 0.6){ f.style.setProperty('--agh', room + 'px'); return; }
  f.style.setProperty('--agh', Math.max(200, Math.floor(vh - head - 32)) + 'px');
  if (scroll && f.scrollIntoView) f.scrollIntoView({block: 'start'});
}
function setAgentMax(on, persist = true){
  document.body.classList.toggle('agentmax', on);
  const b = $('ag-max'); if (b){ setText(b, on ? 'Exit fullscreen' : 'Fullscreen'); b.setAttribute('aria-pressed', String(on)); }
  if (on){ mountAgent(); agexitPlace(); } else if (activeView === 'agent') fitAgentFrame();
  if (persist){ agentMaxChoice = on; try { localStorage.setItem('cockpit.agent.max', on ? '1' : '0'); } catch { /* private mode */ } }
}
on('agent', d => {
  if (!d.enabled){
    cap('ag-cap', 'not installed', '');
    setText('ag-line', 'One install on the box adds opencode here, behind this login.');
    ['ag-open', 'ag-reload', 'ag-max', 'ag-restart'].forEach(id => show(id, false));
    show('ag-note', false);
    if (d.opencode_found === null) agentMessage(`opencode is not installed on this box, and this view runs it. On the box, re-run the installer: it installs the opencode this repo tests${d.pinned ? ` (${d.pinned})` : ''}, then this view.`,
      ((F.config || {}).terminal_only || {}).update_stack || 'cd ~/dgx-spark-qwen38 && ./install.sh');
    else agentMessage('The Agent view is not installed on this cockpit. opencode is on the box; to add the view, run on the box:', ((F.config || {}).terminal_only || {}).install_agent || 'dashboard/install-agent.sh');
    badge('agent', '', ''); return;
  }
  const r = d.relay || {}, sv = d.server || {}, u = d.unit || {};
  const ready = agentReady(), unitOn = u.active === 'active';
  const [txt, kind] = ready ? ['ready', 'ok'] : !r.listening ? ['relay waiting', 'warn'] : !unitOn ? [u.active === 'failed' ? 'server failed' : 'server stopped', 'err'] : ['server starting', 'warn'];
  cap('ag-cap', txt, kind, ready ? true : 'live');
  const parts = [];
  if (sv.version) parts.push('opencode ' + sv.version);
  if (d.binary && sv.version && d.binary !== sv.version) parts.push(`binary ${d.binary} installed, restart to serve it`);
  if (d.pinned && sv.version && sv.version !== d.pinned) parts.push(verCmp(sv.version, d.pinned) < 0 ? `this repo tests ${d.pinned}: ./install.sh brings it in line` : `newer than the ${d.pinned} this repo tests`);
  if (d.auto_live != null) parts.push(d.auto_live ? 'tool calls run without asking' : 'asks before risky tool calls');
  if (d.auto != null && d.auto_live != null && d.auto !== d.auto_live) parts.push('permission mode changed in the unit, restart to apply');
  if (!ready && unitOn && sv.error) parts.push(sv.error);
  setText('ag-line', parts.join(', '));
  show('ag-open', ready); show('ag-reload', ready); show('ag-max', ready); show('ag-restart', !!d.unit_installed);
  if (agentUrl()) $('ag-open').href = agentUrl();
  // the relay answers on one address and the session cookie lives on the host the
  // browser typed: both have to be the same name for the frame to load
  const note = $('ag-note');
  if (r.listening && r.bind && location.hostname !== r.bind){
    note.hidden = false;
    note.textContent = `You reached the cockpit as ${location.hostname}; the agent relay answers on ${r.bind} only. If the panel stays blank, open http://${r.bind}:${location.port || 80}/#agent instead.`;
  } else note.hidden = true;
  if (ready){
    if (activeView === 'agent') mountAgent();
    if (AG.wasReady === false && AG.mounted) reloadAgent();   // the server came back
    if (activeView === 'agent') applyAgentMax();
  } else {
    agentMessage(!r.listening ? `The relay is not listening yet: ${r.error || 'waiting for its address'}.`
      : !unitOn ? `The opencode server is ${u.active === 'failed' ? 'failed' : 'stopped'}. Restart it from here or read its journal in Logs.`
      : `The opencode server is not answering yet${sv.error ? ` (${sv.error})` : ''}. The panel reconnects by itself.`, null);
  }
  AG.wasReady = ready;
  badge('agent', ready ? '' : 'down', 'err');
});
onShow('agent', () => { mountAgent(); applyAgentMax(); fitAgentFrame(); });
// The back button floats where the person parks it; the place is a fraction of the
// frame, so a rotation keeps it on the same side. A drag is never a click.
const AGEXIT_KEY = 'cockpit.agent.exitpos';
function agexitPlace(){
  const b = $('ag-exit'), f = $('ag-frame'); if (!f) return;
  let p = null; try { p = JSON.parse(localStorage.getItem(AGEXIT_KEY)); } catch { /* unparseable: the default */ }
  if (!p || !(p.x >= 0 && p.x <= 1) || !(p.y >= 0 && p.y <= 1)){ b.style.left = b.style.top = b.style.right = b.style.transform = ''; return; }
  const fr = f.getBoundingClientRect(), w = b.offsetWidth || 160, h = b.offsetHeight || 32;
  b.style.transform = 'none'; b.style.right = 'auto';
  b.style.left = Math.max(6, Math.min(fr.width - w - 6, p.x * fr.width)) + 'px';
  b.style.top = Math.max(6, Math.min(fr.height - h - 6, p.y * fr.height)) + 'px';
}
function wireAgent(){
  $('ag-reload').addEventListener('click', reloadAgent);
  $('ag-max').addEventListener('click', () => setAgentMax(!document.body.classList.contains('agentmax')));
  $('ag-restart').addEventListener('click', () => askAction('unit', {verb: 'restart', unit: AGENT_UNIT},
    ['A generation in flight in the agent is lost; the panel reconnects when the server is back.']));
  let start = null, moved = false;
  const exit = $('ag-exit');
  exit.addEventListener('pointerdown', e => {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const r = exit.getBoundingClientRect(), fr = $('ag-frame').getBoundingClientRect();
    start = {x: e.clientX, y: e.clientY, ox: r.left - fr.left, oy: r.top - fr.top, w: r.width, h: r.height}; moved = false;
    exit.setPointerCapture(e.pointerId);
  });
  exit.addEventListener('pointermove', e => {
    if (!start) return;
    const dx = e.clientX - start.x, dy = e.clientY - start.y;
    if (!moved && Math.hypot(dx, dy) < 6) return;
    moved = true; const fr = $('ag-frame').getBoundingClientRect();
    exit.style.transform = 'none'; exit.style.right = 'auto';
    exit.style.left = Math.max(6, Math.min(fr.width - start.w - 6, start.ox + dx)) + 'px';
    exit.style.top = Math.max(6, Math.min(fr.height - start.h - 6, start.oy + dy)) + 'px';
  });
  const end = () => {
    if (!start) return; start = null; if (!moved) return;
    const fr = $('ag-frame').getBoundingClientRect(), r = exit.getBoundingClientRect();
    try { localStorage.setItem(AGEXIT_KEY, JSON.stringify({x: (r.left - fr.left) / fr.width, y: (r.top - fr.top) / fr.height})); } catch { /* this page only */ }
  };
  exit.addEventListener('pointerup', end); exit.addEventListener('pointercancel', end);
  exit.addEventListener('click', () => { if (moved){ moved = false; return; } setAgentMax(false); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && document.body.classList.contains('agentmax') && $('scrim').hidden) setAgentMax(false); });
  window.addEventListener('resize', () => { if (document.body.classList.contains('agentmax')) agexitPlace(); else fitAgentFrame(false); });
  // the visual viewport: on iOS the keyboard shrinks it without touching the layout
  // viewport, so a fullscreen frame sized in dvh would keep the composer under the keyboard
  const vvp = window.visualViewport;
  if (vvp){
    const sync = () => { const s = document.documentElement.style; s.setProperty('--vvh', Math.round(vvp.height) + 'px'); s.setProperty('--vvtop', Math.round(vvp.offsetTop) + 'px'); };
    vvp.addEventListener('resize', sync); vvp.addEventListener('scroll', sync); sync();
  }
}
// leaving the view always shrinks the frame back: it covers every other view
VIEWS.filter(v => v !== 'agent').forEach(v => onShow(v, () => { if (document.body.classList.contains('agentmax')) setAgentMax(false, false); }));
