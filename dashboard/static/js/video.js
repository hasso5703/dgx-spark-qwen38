"use strict";
/* Video: MiniMax-H3, video and sound made together in one pass.

   One request at a time on this lane. The page's own call is held by the cockpit to
   completed (or to the hour, then parked) and answers a video id; the player streams it
   back through /api/video/content. What the lane is doing meanwhile comes from its own
   journal (lc.parse_video_run): the phase, the step, the lane's seconds per step. Its API
   says "queued, 0" from the POST to the end, so it is not what this page draws. */

const VS = {mode: 't2v', frames: {first: null, last: null}, size: '864x480', inflight: null, parked: null, watching: null,
            busy: false, available: false, run: null, progress: {}, port: 30022, host: '127.0.0.1', model: 'MiniMaxAI/MiniMax-H3',
            variant: 'fl2va', history: [], shown: null, foreign: null, lastRunAt: 0};
// The same admission budget the server runs (cockpit.py video_call): linear in
// step-seconds at the two measured sizes, 3.05 s each at 480P (4 s in 592 s at the
// 50-step default, the encode and decode folded in) and 7.65 at 720P (4 s in about
// 25 min), keyframe conditioning at its measured 10 %. A call with no tenth of slack
// under the lock's two hours is refused here too; dashboard/tests/test_video_routes.py
// holds these numbers equal to the server's.
const VID_BUDGET_S = 2 * 3600;
function vidEtaSecs(secs, size, steps){
  const [sw, sh] = String(size).split('x').map(Number);
  if (!(sw > 0 && sh > 0)) return 0;
  const perStep = sw * sh <= 864 * 480 ? 3.05 : 7.65;
  return perStep * (steps || 50) * secs * (VS.mode === 'fl2v' ? 1.1 : 1);
}
const VID_SEED_MAX = Number.MAX_SAFE_INTEGER;   // past it a browser rounds the number it sends
// What this box was measured at, and nothing past it (cockpit.py refuses the same): a
// video's memory grows far faster than its length, 9.6 GB for 4 s at 480p and 78.3 GB
// for 15 s, and 4 s at 720p already peaks at 82 GB of the box's 121.6.
const VID_LONG_MAX_PIXELS = 864 * 480, VID_LARGE_MAX_SECONDS = 4;
const vidMaxSecs = size => { const [w, h] = String(size).split('x').map(Number); return w * h > VID_LONG_MAX_PIXELS ? VID_LARGE_MAX_SECONDS : 15; };
const VID_EXAMPLES = {
  'Fox at dawn': 'A red fox trots across a snowy forest clearing at dawn, breath steaming, soft golden light through the pines, gentle wind and birdsong.',
  'Harbour aerial': 'A slow aerial shot over a coastal village at golden hour: waves roll onto a pebble beach, gulls circle a small harbour, church bells ring in the distance.',
  'Rain in the city': 'Night in a narrow city street after rain, neon signs reflected in puddles, a tram passes with a bell, footsteps and distant traffic.',
  'Workshop': 'Close-up of hands turning a brass part on a small lathe, sparks and shavings, the steady hum of the motor, warm workshop light.'};
const vidVal = id => ($(id) ? $(id).value.trim() : '');
const vidSecs = () => parseInt(vidVal('vid-secs'), 10) || 4;
const vidSteps = () => parseInt(vidVal('vid-steps'), 10) || 50;
// A parked video (the page stopped waiting at the hour, the lane did not) is this
// person's: its id is kept in the browser, so a reload still says "your video" and shows
// it when it is done. A convenience only: without storage it is shown as another's.
const PARKED_KEY = 'cockpit.video.parked';
const parkedGet = () => { try { return localStorage.getItem(PARKED_KEY); } catch { return null; } };
const parkedSet = id => { try { if (id) localStorage.setItem(PARKED_KEY, id); else localStorage.removeItem(PARKED_KEY); } catch { /* storage may be unavailable */ } };
function vidProblem(){
  const s = vidSecs(), steps = vidSteps();
  if (!(s >= 4 && s <= 15)) return 'The length is a whole number of seconds, from 4 to 15.';
  if (!vidVal('vid-prompt')) return 'Write a prompt: what happens, and what it sounds like.';
  if (VS.mode === 'fl2v' && !VS.frames.first && !VS.frames.last) return 'Add a first frame, a last frame, or both.';
  if (!(steps >= 1 && steps <= 100)) return 'Steps run from 1 to 100.';
  if (s > vidMaxSecs(VS.size)) return `720p is measured here at ${VID_LARGE_MAX_SECONDS} s only (82 GB at its peak, of 121.6): a longer video at this size would outgrow the box's memory, which hangs it. Choose ${VID_LARGE_MAX_SECONDS} s, or 480p for up to 15 s.`;
  const seed = vidVal('vid-seed');
  if (seed && !/^\d+$/.test(seed)) return 'The seed is a whole number, or empty for a random one.';
  if (seed && Number(seed) > VID_SEED_MAX) return `The seed goes up to ${fmtN(VID_SEED_MAX)}: past that a browser rounds the number it sends.`;
  const est = vidEtaSecs(s, VS.size, steps);
  if (est * 1.1 > VID_BUDGET_S) return `About ${fmtMin(est)} at this size: no slack under the two hours the cockpit holds a call, and the next one could start beside it. Ask for fewer steps, a shorter video, or 480p.`;
  return '';
}
// What the lane receives: the cockpit forwards this body (cockpit.py video_call). The
// lane requires an explicit task and a target, and reads keyframes by file:// URI.
function vidWireBody(){
  const secs = vidSecs(), [sw, sh] = VS.size.split('x').map(Number);
  const body = {model: 'MiniMax-H3', prompt: vidVal('vid-prompt'), seconds: secs, size: VS.size, task: VS.mode === 'fl2v' ? 'fl2va' : 't2va',
                target: {short_edge: Math.min(sw, sh), aspect_ratio: sw >= sh ? '16:9' : '9:16', duration_seconds: secs},
                num_outputs_per_prompt: 1, num_inference_steps: vidSteps()};
  const seed = vidVal('vid-seed'); if (seed) body.seed = Number(seed);
  if (VS.mode === 'fl2v') body.conditions = [['first', 0], ['last', -1]].filter(([t]) => VS.frames[t])
    .map(([t, idx]) => ({type: 'image', uri: `file:///tmp/qwen38-${t}-frame.png`, role: 'keyframe', frame_index: idx}));
  return body;
}
function vidSync(){
  // the length's end is the longest this size was measured at: a 720p slider that goes to
  // 15 s offers what the box cannot hold
  const secsEl = $('vid-secs');
  if (secsEl){ const mx = vidMaxSecs(VS.size); secsEl.max = String(mx); if (Number(secsEl.value) > mx) secsEl.value = String(mx);
    const ends = secsEl.parentElement.querySelector('.ends'); if (ends && ends.lastChild) setText(ends.lastChild, mx + ' s' + (mx < 15 ? ', the most 720p holds' : '')); }
  // sliders say their value and fill to it
  const secs = vidSecs(), steps = vidSteps();
  setText('vid-secs-o', secs + ' s'); setText('vid-steps-o', String(steps));
  [['vid-secs', 4, vidMaxSecs(VS.size)], ['vid-steps', 1, 100]].forEach(([id, a, b]) => { const e = $(id); if (e) e.style.setProperty('--pct', (100 * (Number(e.value) - a) / Math.max(1, b - a)).toFixed(1) + '%'); });
  document.querySelectorAll('#vid-size button').forEach(b => b.setAttribute('aria-checked', String(b.dataset.size === VS.size)));
  document.querySelectorAll('#vid-mode button').forEach(b => b.setAttribute('aria-checked', String(b.dataset.mode === VS.mode)));
  show('vid-framebox', VS.mode === 'fl2v');
  // the cost, measured, before anything is asked
  const est = vidEtaSecs(secs, VS.size, steps);
  setText('vid-cost-v', 'about ' + fmtMin(est)); setText('vid-cost-k', `for ${secs} s at ${VS.size === '864x480' ? '480p' : '720p'}, ${steps} steps${VS.mode === 'fl2v' ? ', from keyframes' : ''}`);
  setText('vid-cost-b', 'Timed from runs on this box: 4 s at 480p took 9:52 to 12:40 depending on how hot the NVMe runs.');
  const bud = $('vid-budget'); const pct = Math.min(100, 100 * est * 1.1 / VID_BUDGET_S);
  bud.firstChild.style.width = pct.toFixed(1) + '%'; bud.className = 'meter' + (pct > 100 ? ' err' : pct > 70 ? ' warn' : '');
  // the problem is said once the person has touched the form, never on an empty page
  const p = vidProblem();
  const touched = VS.touched || !!vidVal('vid-prompt');
  show('vid-problem', !!p && touched && !(p.startsWith('Write a prompt') && !VS.touched)); setText('vid-problem', p);
  $('vid-seed').setAttribute('aria-invalid', String(!!vidVal('vid-seed') && /seed/i.test(p)));
  // the button, and the reason when it cannot run
  const why = !VS.available ? vidLaneWhy() : VS.inflight ? 'Your video is being made.' : VS.busy ? 'Someone else’s video is being made: one at a time on this lane.' : p;
  $('vid-run').disabled = !!why;
  show('vid-why', !!why && why !== p); setText('vid-why', why);
  setText('vid-curl-note', 'Run it on the box: the lane listens on loopback and checks no key. Keyframes must first be saved at the file:// paths shown.');
  setText('vid-curl', `curl -s http://127.0.0.1:${VS.port}/v1/videos \\\n  -H 'Content-Type: application/json' \\\n  -d ${shq(JSON.stringify(vidWireBody()))}\n# then poll GET /v1/videos to completed, and download GET /v1/videos/<id>/content`);
}
function vidLaneWhy(){
  const e = engines()[VIDEO_UNIT];
  if (!F.life) return 'Checking the lane…';
  if (!e) return 'The video lane is not installed on this box.';
  if (TRANSITIONAL.has(e.state)) return `The lane is booting: Generate turns on by itself when it answers.`;
  if (e.state === 'stopping') return 'The lane is stopping.';
  return 'The video lane is not serving. Load it first.';
}

// ── the lane callout: what to do when it is not serving ───────────────────────
function vidRenderLane(){
  const box = $('vid-lane'), e = engines()[VIDEO_UNIT], s = servingEngine();
  const ready = !!e && (e.state === 'ready' || e.state === 'degraded');
  VS.available = ready;
  // a lane that is not serving makes no video: whatever was last read of it is over
  // (a stop cancels the job in flight), so the badge, the spine and the Now view let it go
  if (!ready){ VS.busy = false; VS.foreign = null; if (!VS.inflight){ VS.run = null; VID_NOW.run = null; VID_NOW.label = ''; } }
  let sig, build;
  if (!F.life){ sig = 'wait'; build = () => {}; }
  else if (!e){ sig = 'absent'; build = () => {
    const b = el('div', 'banner info'); b.append(el('span', 'lamp'));
    const d = el('div'); d.append(el('b', null, 'The video lane is not installed. '), el('span', null, 'On the box: ./install.sh --with-video. It needs about 150 GB of disk and an hour.'));
    b.append(d); box.append(b); }; }
  else if (ready){ sig = 'ready'; build = () => {}; }
  else if (TRANSITIONAL.has(e.state)){
    sig = 'boot'; build = () => {
      const card = el('div', 'progress-card'); card.style.marginBottom = 'var(--s-5)';
      const top = el('div', 'top'); top.append(el('b', null, 'Loading MiniMax-H3'), el('span', 'eta', ''));
      const m = el('div', 'meter'); m.append(el('i')); card.append(top, m, el('span', 'help', '')); box.append(card); };
  } else {
    sig = 'down:' + (s ? s[0] : ''); build = () => {
      const card = el('div', 'banner info'); card.style.alignItems = 'center';
      card.append(el('span', 'lamp' + (e.state === 'failed' ? ' err' : '')));
      const d = el('div'); d.style.display = 'flex'; d.style.flexWrap = 'wrap'; d.style.alignItems = 'center'; d.style.gap = 'var(--s-3)';
      const t = el('span', null, e.state === 'failed' ? 'The video lane failed. Its journal is in Logs.' : s ? `${laneLabel(s[0])} holds the box. Loading the video lane stops it first.` : 'The video lane is stopped.');
      t.style.flex = '1'; t.style.minWidth = '240px';
      const b = el('button', 'btn primary', 'Load MiniMax-H3'); b.type = 'button'; b.addEventListener('click', () => askJourney('video'));
      d.append(t, b); card.append(d); box.append(card); };
  }
  if (box.dataset.sig !== sig){ box.dataset.sig = sig; clear(box); build(); }
  if (sig === 'boot'){
    const card = box.firstChild, eta = e.eta || READY_DEFAULT[VIDEO_UNIT], pct = eta && e.elapsed ? Math.min(97, 100 * e.elapsed / eta) : 6;
    card.querySelector('.meter i').style.width = pct.toFixed(1) + '%';
    setText(card.querySelector('.eta'), e.elapsed && eta ? `about ${fmtDur(Math.max(0, eta - e.elapsed))} left` : '');
    setText(card.querySelector('.help'), `${STATE_LABEL[e.state]}${e.detail && e.detail !== 'ready' ? ', ' + e.detail : ''}, ${fmtDur(e.elapsed)} elapsed`);
  }
  cap('vid-cap', !e ? 'not installed' : ready ? (VS.busy || VS.inflight ? 'making a video' : 'ready') : STATE_LABEL[e.state] || e.state,
      !e ? '' : ready ? (VS.busy || VS.inflight ? 'warn' : 'ok') : stateKind(e.state), ready && (VS.busy || VS.inflight) ? 'live' : e ? stateLive(e.state) : false);
  badge('video', VS.inflight || VS.busy ? '●' : '', 'live');
}

// ── the stage: empty, working, or the video ───────────────────────────────────
function vidScreenEmpty(){
  const sc = $('vid-screen'); if (sc.dataset.mode === 'empty') return; sc.dataset.mode = 'empty'; clear(sc);
  const d = el('div', 'empty'); d.append(el('b', null, 'Your video appears here'), el('span', null, 'A 4 s clip at 480p takes about ten minutes on this box.')); sc.append(d);
}
// While the lane works, the screen shows where it is: a ring filled to the step, the
// number of the step, and the phase in words. The lane cannot show a frame before the end.
function ringInto(sc){
  const ring = el('div', 'ring');
  const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg'); svg.setAttribute('viewBox', '0 0 100 100');
  const defs = document.createElementNS(NS, 'defs'), g = document.createElementNS(NS, 'linearGradient'); g.setAttribute('id', 'ringg');
  [['0', '#f1dcaa'], ['1', '#9d7f45']].forEach(([o, c]) => { const st = document.createElementNS(NS, 'stop'); st.setAttribute('offset', o); st.setAttribute('stop-color', c); g.append(st); });
  defs.append(g); svg.append(defs);
  ['track', 'fill'].forEach(k => { const c = document.createElementNS(NS, 'circle'); c.setAttribute('cx', '50'); c.setAttribute('cy', '50'); c.setAttribute('r', '44');
    c.setAttribute('class', k); c.setAttribute('pathLength', '100'); if (k === 'fill'){ c.setAttribute('stroke-dasharray', '100'); c.setAttribute('stroke-dashoffset', '100'); } svg.append(c); });
  const inner = el('div', 'in'); inner.append(el('b'), el('span'));
  ring.append(svg, inner); sc.append(el('div', 'scan'), ring, el('p', 'worklab'));
  return ring;
}
function ringSet(sc, pct, big, small, label){
  const ring = sc.querySelector('.ring'); if (!ring) return;
  ring.classList.toggle('indet', pct == null);
  const f = ring.querySelector('.fill'); f.setAttribute('stroke-dashoffset', pct == null ? '72' : String(Math.max(0, 100 - pct)));
  setText(ring.querySelector('.in b'), big); setText(ring.querySelector('.in span'), small); setText(sc.querySelector('.worklab'), label);
}
function vidScreenWorking(label, pct, big, small){
  const sc = $('vid-screen');
  if (sc.dataset.mode !== 'work'){ sc.dataset.mode = 'work'; clear(sc); ringInto(sc); }
  ringSet(sc, pct, big != null ? big : '…', small || '', label);
}
function vidScreenPlay(id){
  const sc = $('vid-screen'); sc.dataset.mode = 'play:' + id; clear(sc);
  const v = document.createElement('video'); v.controls = true; v.preload = 'metadata'; v.playsInline = true;
  v.src = '/api/video/content?id=' + encodeURIComponent(id); sc.append(v); VS.shown = id;
  return v;
}
function vidDrawProgress(){
  const card = $('vid-prog'), r = VS.run || {};
  const mine = !!VS.inflight, other = VS.busy && !mine, parked = !!VS.parked;
  const active = mine || other || parked;
  card.hidden = !active;
  // a card put away says nothing: its last words are not read out, nor found again later
  if (!active){ ['vid-prog-lab', 'vid-prog-eta', 'vid-prog-note'].forEach(id => setText(id, '')); return; }
  const phase = r.phase || (mine ? 'encode' : null);
  let lab, eta = '', pct = null;
  if (phase === 'denoise' && r.step != null){
    lab = `Denoising, step ${r.step} of ${r.steps}`;
    pct = 5 + 88 * r.step / r.steps;
    const decode = 6 * vidSecsOf() + 5;   // the decode measured 22.5 s for 4 s; it grows with the frames
    if (r.left_s != null) eta = `about ${fmtDur(r.left_s + decode)} left` + (r.s_per_step ? `, ${r.s_per_step.toFixed(1)} s per step` : '');
  } else if (phase === 'decode'){ lab = 'Decoding video and sound'; pct = 95; eta = 'almost there'; }
  else if (phase === 'encode'){ lab = 'Reading the prompt'; pct = 3; }
  else { lab = other ? 'Another video is being made' : 'Waiting for the lane'; }
  if (other) lab = 'Someone else’s video: ' + lab.charAt(0).toLowerCase() + lab.slice(1);
  if (parked && !mine) lab = 'Still making your video: ' + lab.charAt(0).toLowerCase() + lab.slice(1);
  setText('vid-prog-lab', lab); setText('vid-prog-eta', eta);
  const bar = $('vid-prog-bar'); bar.className = 'meter' + (pct == null ? ' indet' : ''); bar.firstChild.style.width = (pct == null ? 40 : pct).toFixed(1) + '%';
  document.querySelectorAll('#vid-phases span').forEach(sp => {
    const order = ['encode', 'denoise', 'decode'], i = order.indexOf(sp.dataset.p), k = order.indexOf(phase);
    sp.className = k < 0 ? '' : i < k ? 'done' : i === k ? 'now' : '';
  });
  const elapsed = mine ? (Date.now() - VS.inflight) / 1000 : null;
  setText('vid-prog-note', mine ? `${fmtDur(elapsed)} since you asked. The lane cannot abort a video: Cancel restarts it.` : other ? 'Generate turns on when it is done.' : 'This page stopped waiting at one hour; the lane did not.');
  show('vid-cancel', true);
  if (phase === 'denoise' && r.step != null) vidScreenWorking(other ? 'Someone else\u2019s video' : parked ? 'Still making your video' : 'Making your video', pct, `${r.step}/${r.steps}`, 'denoising steps');
  else vidScreenWorking(lab, phase === 'decode' ? 96 : null, phase === 'decode' ? 'Last step' : phase === 'encode' ? 'Prompt' : '…', phase === 'decode' ? 'decoding video and sound' : '');
  VID_NOW.label = phase === 'denoise' && r.step != null ? `making a video, step ${r.step} of ${r.steps}` : 'making a video';
}
const vidSecsOf = () => (VS.req && VS.req.seconds) || vidSecs();
function vidDone(id, took, req){
  const v = vidScreenPlay(id);
  const meta = $('vid-meta'); clear(meta);
  const a = el('a', 'btn primary', 'Download MP4'); a.href = '/api/video/content?id=' + encodeURIComponent(id); a.download = 'minimax-h3-' + id.slice(0, 8) + '.mp4';
  meta.append(a);
  if (took) meta.append(el('span', 'tag gold', `made in ${fmtDur(took)}`));
  if (req) meta.append(el('span', 'tag', `${req.seconds} s, ${req.size === '864x480' ? '480p' : '720p'}, ${req.num_inference_steps} steps` + (req.seed != null ? `, seed ${req.seed}` : '')));
  // a thumbnail for the session's history, taken from the video itself
  const entry = {id, took, req: req || null, thumb: null};
  VS.history = [entry].concat(VS.history.filter(h => h.id !== id)).slice(0, 12);
  v.addEventListener('loadeddata', () => {
    try{ v.currentTime = Math.min(1, (v.duration || 2) / 2); }catch{ /* keep the first frame */ }
  }, {once: true});
  v.addEventListener('seeked', () => {
    try{ const c = document.createElement('canvas'); c.width = 320; c.height = Math.round(320 * (v.videoHeight || 9) / (v.videoWidth || 16));
      c.getContext('2d').drawImage(v, 0, 0, c.width, c.height); entry.thumb = c.toDataURL('image/jpeg', .7); vidHistory(); }catch{ /* no thumbnail */ }
  }, {once: true});
  vidHistory();
  VID_NOW.label = '';
}
function vidHistory(){
  const box = $('vid-history'); clear(box); $('vid-history-box').hidden = !VS.history.length;
  VS.history.forEach(h => {
    const b = el('button'); b.type = 'button'; b.setAttribute('aria-pressed', String(VS.shown === h.id));
    b.setAttribute('aria-label', 'Play ' + (h.req ? h.req.prompt.slice(0, 60) : h.id));
    if (h.thumb){ const im = el('img'); im.src = h.thumb; im.alt = ''; b.append(im); }
    b.append(el('span', null, h.req ? `${h.req.seconds} s` : h.id.slice(0, 6)));
    b.addEventListener('click', () => { vidDone(h.id, h.took, h.req); });
    box.append(b);
  });
}

// ── talking to the lane ───────────────────────────────────────────────────────
async function vidLane(){
  try{
    const r = await fetch('/api/video'); if (r.status === 401) return;
    const d = await r.json();
    VS.port = d.port || 30022; VS.host = d.host || '127.0.0.1'; VS.model = d.model || VS.model; VS.variant = d.variant || 'fl2va';
    VID_NOW.port = VS.port;
    const p = d.progress || {};
    VS.progress = p; VS.run = d.run && d.run.phase ? d.run : null; VID_NOW.run = VS.run;
    // someone else generating counts: the lane serves one at a time
    const running = !!VS.run || (!!p.id && p.status !== 'completed');
    VS.busy = !VS.inflight && running;
    // a video that was being made elsewhere, or this page's parked one, is shown when it ends
    if (running && p.id && !VS.inflight){ if (parkedGet() === p.id) VS.parked = p.id; else VS.foreign = VS.foreign || p.id; }
    if (!running && (VS.parked || VS.foreign)){
      const id = VS.parked || VS.foreign; const wasParked = !!VS.parked; VS.parked = null; VS.foreign = null; if (wasParked) parkedSet(null);
      if (wasParked){ vidDone(id, null, VS.req); toast('Your video is ready.', 'ok', 6000); }
      else { const meta = $('vid-meta'); clear(meta); const a = el('button', 'btn', 'Play the video that just finished'); a.type = 'button';
        a.addEventListener('click', () => vidDone(id, null, null)); meta.append(a); }
    }
    if (!running && !VS.inflight){ VID_NOW.label = ''; if (($('vid-screen').dataset.mode || '') === 'work') vidScreenEmpty(); }
    vidDrawProgress(); vidRenderLane(); vidSync();
    vidWatch(running || !!VS.inflight);
  }catch{ cap('vid-cap', 'unknown', 'warn'); }
}
// Each answer runs a journalctl on the box: someone else's video is followed on a visible
// Video view only; this page's own call is followed from anywhere.
function vidWatch(on){
  const want = on && (VS.inflight || (!document.hidden && activeView === 'video'));
  if (want && VS.watching) return;
  if (VS.watching){ clearInterval(VS.watching); VS.watching = null; }
  if (want) VS.watching = setInterval(() => {
    if (!VS.inflight && (document.hidden || activeView !== 'video')){ clearInterval(VS.watching); VS.watching = null; return; }
    vidLane();
  }, 5000);
}
function vidErr(out, status){
  const v = out.error || out.refused || status;
  const s = typeof v === 'string' ? v : JSON.stringify(v);   // an object + '' is "[object Object]"
  return (s || String(status)).slice(0, 400);
}
async function vidRun(){
  VS.touched = true; vidSync();
  if (VS.inflight || VS.busy) return toast('The lane is already making a video; one at a time.', 'warn');
  const p = vidProblem(); if (p) return toast(p, 'warn');
  if (!VS.available) return toast(vidLaneWhy(), 'warn');
  const payload = {prompt: vidVal('vid-prompt'), seconds: vidSecs(), size: VS.size, num_inference_steps: vidSteps()};
  const seed = vidVal('vid-seed'); if (seed) payload.seed = Number(seed);
  if (VS.mode === 'fl2v'){ if (VS.frames.first) payload.first_frame = VS.frames.first.dataUrl; if (VS.frames.last) payload.last_frame = VS.frames.last.dataUrl; }
  const t0 = Date.now(); VS.inflight = t0; VS.req = {...payload}; delete VS.req.first_frame; delete VS.req.last_frame;
  clear($('vid-meta')); VS.run = null; vidDrawProgress(); vidSync(); vidRenderLane(); vidWatch(true);
  try{
    const {status, ok, out} = await postJSON('/api/video/generate', payload);
    const took = Math.round((Date.now() - t0) / 1000);
    if (!ok && out.crashed) return toast('No video: the lane crashed while making it. ' + out.error, 'err', 9000);
    if (!ok && (out.interrupted || VID_INTERRUPTED_AT.t > t0)) return toast('Video cancelled: the lane was stopped or restarted.', 'warn');
    if (!ok && status === 504 && out.video_id){ VS.parked = out.video_id; parkedSet(out.video_id); toast('This page waited an hour; the lane goes on, and the video appears here when it is done.', 'warn', 9000); return; }
    if (!ok) return toast(status === 409 ? out.error : 'Could not make a video: ' + vidErr(out, status), status === 409 ? 'warn' : 'err', 8000);
    if (!out.video_id) return toast('The lane answered with no video in it.', 'err');
    vidDone(out.video_id, took, VS.req);
    toast(`Video ready, made in ${fmtDur(took)}.`, 'ok', 5000);
  }catch(e){ toast('Could not make a video: ' + e.message, 'err'); }
  finally{ VS.inflight = null; vidLane(); }
}
function vidCancel(){
  const other = VS.busy && !VS.inflight;
  askAction('unit', {verb: 'restart', unit: VIDEO_UNIT},
    [(other ? 'This video is not yours: someone else started it. ' : '') + `The runtime cannot abort a video, so cancelling restarts the lane: the video being made is lost, and the lane answers again in ${readyIn(VIDEO_UNIT)}.`], {danger: true});
}
// Keyframes are re-encoded to PNG and capped on the long side before they leave the
// browser: a phone photo would be megabytes spent against the 10 MiB the cockpit reads.
function vidReadFile(tag, file){
  if (!file) return;
  const r = new FileReader();
  r.onload = () => {
    const im = new Image();
    im.onload = () => {
      const scale = Math.min(1, 1280 / Math.max(im.width, im.height));
      const c = document.createElement('canvas'); c.width = Math.round(im.width * scale); c.height = Math.round(im.height * scale);
      c.getContext('2d').drawImage(im, 0, 0, c.width, c.height);
      VS.frames[tag] = {name: file.name, dataUrl: c.toDataURL('image/png')}; vidDrawFrames(); vidSync();
    };
    im.onerror = () => toast(`${file.name} is not an image the browser can read.`, 'err');
    im.src = r.result;
  };
  r.readAsDataURL(file);
}
function vidDrawFrames(){
  ['first', 'last'].forEach(tag => {
    const zone = $(`vid-${tag}-drop`), f = VS.frames[tag];
    zone.querySelectorAll('img, .x').forEach(n => n.remove());
    setText(zone.querySelector('span'), f ? '' : 'Drop or pick an image');
    if (f){
      const im = el('img'); im.src = f.dataUrl; im.alt = `${tag} frame: ${f.name}`; zone.append(im);
      const x = el('button', 'btn sm x', 'Remove'); x.type = 'button';
      x.addEventListener('click', ev => { ev.preventDefault(); ev.stopPropagation(); VS.frames[tag] = null; vidDrawFrames(); vidSync(); });
      zone.append(x);
    }
  });
}
function wireVideo(){
  document.querySelectorAll('#vid-mode button').forEach(b => b.addEventListener('click', () => { VS.mode = b.dataset.mode; vidSync(); }));
  document.querySelectorAll('#vid-size button').forEach(b => b.addEventListener('click', () => { VS.size = b.dataset.size; vidSync(); }));
  Object.entries(VID_EXAMPLES).forEach(([name, text]) => {
    const b = el('button', null, name); b.type = 'button'; b.setAttribute('aria-pressed', 'false');
    b.addEventListener('click', () => { $('vid-prompt').value = text; document.querySelectorAll('#vid-examples button').forEach(x => x.setAttribute('aria-pressed', String(x === b))); vidSync(); });
    $('vid-examples').append(b);
  });
  ['vid-prompt', 'vid-secs', 'vid-steps', 'vid-seed'].forEach(id => $(id).addEventListener('input', () => { if (id !== 'vid-prompt') VS.touched = true; vidSync(); }));
  $('vid-prompt').addEventListener('blur', () => { VS.touched = true; vidSync(); });
  $('vid-dice').addEventListener('click', () => { $('vid-seed').value = String(Math.floor(Math.random() * 2147483647)); vidSync(); });
  ['first', 'last'].forEach(tag => {
    const zone = $(`vid-${tag}-drop`), input = $(`vid-${tag}`);
    input.addEventListener('change', e => { vidReadFile(tag, e.target.files[0]); e.target.value = ''; });
    zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('over'); });
    zone.addEventListener('dragleave', () => zone.classList.remove('over'));
    zone.addEventListener('drop', e => { e.preventDefault(); zone.classList.remove('over'); vidReadFile(tag, e.dataTransfer.files[0]); });
  });
  $('vid-run').addEventListener('click', vidRun);
  $('vid-cancel').addEventListener('click', vidCancel);
  $('vid-reset').addEventListener('click', () => {
    $('vid-secs').value = 4; $('vid-steps').value = 50; $('vid-seed').value = ''; VS.size = '864x480'; VS.frames = {first: null, last: null};
    vidDrawFrames(); VS.touched = false; vidSync(); toast('Back to the defaults: 4 s, 480p, 50 steps, a random seed.', 'ok', 2600);
  });
  $('vid-copy').addEventListener('click', () => copyText($('vid-curl').textContent));
  document.addEventListener('visibilitychange', () => { if (!document.hidden && activeView === 'video') vidLane(); });
  vidSync();
}
onShow('video', () => { vidLane(); });
afterApply(() => { vidRenderLane(); if (activeView === 'video') vidSync(); });
// The Now view and the spine read the lane's run too, whatever view is open: while the
// video lane serves, the page asks at most every 10 s (the Video view asks every 5 s).
let vidAskedAt = 0;
afterApply(() => {
  if (!videoServing() || VS.inflight || VS.watching || document.hidden) return;
  if (Date.now() - vidAskedAt < 10000) return;
  vidAskedAt = Date.now(); vidLane();
});
