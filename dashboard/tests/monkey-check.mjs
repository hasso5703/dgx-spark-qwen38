// Click-storm check of the cockpit UI: a headless Chromium clicks everything, several
// times, fast, on every tab, and every assertion is checked against the page AND the
// server. It refuses to run against a cockpit that is not in dry run, so nothing it
// clicks can ever touch the real serving stack.
//
//   COCKPIT_DRY_RUN=1 COCKPIT_PORT=30091 COCKPIT_CONFIG_DIR=/tmp/x python3 dashboard/cockpit.py &
//   node dashboard/tests/monkey-check.mjs [http://127.0.0.1:30091]
//
// Exit code 0 = every check passed. Any failure prints the check that failed.
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';

const BASE = process.argv[2] || process.env.COCKPIT_BASE || 'http://127.0.0.1:30091';
const KEYFILE = process.env.COCKPIT_KEY_FILE || `${process.env.HOME}/.config/qwen38/api-key`;
const SHOT = process.env.COCKPIT_SHOT || '/tmp/cockpit-monkey.png';
const checks = [];
const ok = (name, cond, detail = '') => { checks.push({ name, ok: !!cond, detail: String(detail).slice(0, 200) }); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ── session (server side: nothing is typed into the login form) ───────────────
const key = readFileSync(KEYFILE, 'utf8').trim();
const jar = [];
async function api(path, init = {}) {
  const r = await fetch(BASE + path, { ...init, headers: { ...(init.headers || {}), cookie: jar.join('; ') } });
  const sc = r.headers.get('set-cookie'); if (sc) jar.push(sc.split(';')[0]);
  return r;
}
const login = await api('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
if (!login.ok) { console.error(`login failed: HTTP ${login.status}`); process.exit(2); }
const cookie = jar.join('; ').match(/cockpit=([^;]+)/)?.[1];

const state0 = await (await api('/api/state')).json();
const cfg = (state0.config || {}).data || {};
if (cfg.dry_run !== true) {
  console.error(`REFUSING to run: ${BASE} is not in dry run (config.dry_run=${cfg.dry_run}).`);
  console.error('Start a second cockpit with COCKPIT_DRY_RUN=1 on another port and point this test at it.');
  process.exit(3);
}

// ── browser ──────────────────────────────────────────────────────────────────
const chrome = spawn('/snap/bin/chromium', ['--headless=new', '--remote-debugging-port=0', '--no-first-run',
  '--disable-gpu', '--hide-scrollbars', '--window-size=1500,1000', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
const wsUrl = await new Promise((res, rej) => {
  let buf = ''; chrome.stderr.on('data', d => { buf += d; const m = buf.match(/DevTools listening on (ws:\S+)/); if (m) res(m[1]); });
  setTimeout(() => rej(new Error('no devtools url: ' + buf.slice(-300))), 20000);
});
const ws = new WebSocket(wsUrl); await new Promise(r => { ws.onopen = r; });
let id = 0; const pending = new Map(); let events = [];
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } else if (m.method) events.push(m); };
const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params, sessionId })); });
const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' });
const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
await send('Network.enable', {}, sessionId); await send('Runtime.enable', {}, sessionId); await send('Page.enable', {}, sessionId);
await send('Network.setCookie', { name: 'cockpit', value: cookie, url: BASE, httpOnly: true, sameSite: 'Strict' }, sessionId);

const jsErrors = () => events.filter(m => m.method === 'Runtime.exceptionThrown')
  .map(m => m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
async function evalJs(expression) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || 'eval threw');
  return r.result.result.value;
}
async function goto(path) {
  events = [];
  await send('Page.navigate', { url: BASE + path }, sessionId);
  await sleep(600);
}
async function waitFor(expression, ms = 12000, every = 200) {
  const t0 = Date.now();
  for (;;) {
    try { if (await evalJs(expression)) return true; } catch { /* page mid-navigation */ }
    if (Date.now() - t0 > ms) return false;
    await sleep(every);
  }
}
const click = sel => evalJs(`(()=>{const e=document.querySelector(${JSON.stringify(sel)}); if(!e) return 'missing'; e.click(); return 'clicked';})()`);
const BOOTED = "document.getElementById('lanename').textContent !== '...'";

// ── 1. first load, every tab reachable ───────────────────────────────────────
await goto('/');
ok('page boots and applies a first state', await waitFor(BOOTED), 'lanename still "..." after 12 s');
ok('no exception on first load', jsErrors().length === 0, jsErrors().join(' | '));

const TABS = ['overview', 'agent', 'engines', 'requests', 'machine', 'models', 'logs', 'setup'];
for (const t of TABS) {
  await click(`.rail .nav[data-tab="${t}"]`);
  await sleep(120);
  const active = await evalJs(`document.getElementById('tab-${t}').classList.contains('active') && location.hash === '#${t}'`);
  ok(`nav switches to ${t}`, active);
}
ok('no exception after visiting every tab', jsErrors().length === 0, jsErrors().join(' | '));

// ── 2. reload on every hash (a deep link must not kill the script) ───────────
for (const t of TABS) {
  await goto('/#' + t);
  const booted = await waitFor(BOOTED, 9000);
  const errs = jsErrors();
  ok(`reload on #${t} boots the app`, booted && errs.length === 0, errs.join(' | ') || (booted ? '' : 'never applied a state'));
}

// ── 3. panels say something in every state (no silent blank, no stuck skeleton) ──
await goto('/');
await waitFor(BOOTED);
const emptyish = await evalJs(`(()=>{
  const bad = [];
  document.querySelectorAll('.tab dd').forEach(d => {
    const vis = d.closest('.tab');
    if (d.textContent.trim() === '...' && d.offsetParent !== null) bad.push((d.id||'?') + ' in ' + (vis?vis.id:'?'));
  });
  return bad;
})()`);
ok('no visible field stuck on "..." after load', emptyish.length === 0, emptyish.join(', '));

// a renderer that throws must never hide: its panels say so
const renderErrs = await evalJs(`[...document.querySelectorAll('section.panel .age')]
  .map(e => e.textContent).filter(t => /render error/.test(t))`);
ok('no panel reports a render error', renderErrs.length === 0, renderErrs.join(' | '));

// ── 4. the confirm modal: opens, shows the command, Escape closes, no action runs ──
const jobsBefore = ((await (await api('/api/state')).json()).job || {}).data || {};
const nBefore = (jobsBefore.recent || []).length;
await click('[data-act="diag_bundle"]');
await sleep(200);
ok('clicking an action opens the confirm modal', await evalJs("!document.getElementById('modal').hidden"));
ok('the modal shows a command', (await evalJs("document.getElementById('margv').textContent")).length > 3);
ok('the modal explains what happens', (await evalJs("document.getElementById('mwhat').textContent")).length > 20);
await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 }, sessionId);
await sleep(200);
ok('Escape closes the modal', await evalJs("document.getElementById('modal').hidden"));
const afterEsc = ((await (await api('/api/state')).json()).job || {}).data || {};
ok('cancelling started nothing', ((afterEsc.recent || []).length) === nBefore, `${(afterEsc.recent || []).length} vs ${nBefore}`);

// ── 5. a second action while one runs is refused, and the UI says so ─────────
await click('[data-act="diag_bundle"]');
await sleep(200);
await click('#mgo');
ok('the job strip appears', await waitFor("!document.getElementById('jobstrip').hidden", 6000));
ok('the strip names the running action', /diagnostics bundle/.test(await evalJs("document.getElementById('jobwhat').textContent")));
const disabled = await waitFor("document.querySelector('[data-act=\\\"switch\\\"]').disabled", 4000);
ok('other actions are disabled while a job runs', disabled);
// force the refusal server-side too: the button is disabled, so call the API directly
const csrf = (await (await api('/api/csrf', { method: 'POST' })).json()).token;
const second = await api('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'smoke', params: {}, csrf }) });
const secondBody = await second.json();
ok('the server refuses a second job with 409 busy', second.status === 409 && secondBody.error === 'busy', `${second.status} ${JSON.stringify(secondBody).slice(0, 120)}`);
ok('the refusal names the running job', !!(secondBody.running && secondBody.running.action === 'diag_bundle'), JSON.stringify(secondBody.running || {}).slice(0, 120));
ok('the job finishes and the strip reports it', await waitFor("/done|failed/.test(document.getElementById('jobchip').textContent)", 15000),
   await evalJs("document.getElementById('jobchip').textContent"));

// ── 6. double click and click storm: never two jobs, never an exception ──────
const before = ((await (await api('/api/state')).json()).job || {}).data || {};
const nJobs0 = (before.recent || []).length;
events = [];
// a real double click on a live action button
await evalJs(`(()=>{const b=document.querySelector('[data-act="diag_bundle"]'); b.click(); b.click(); b.click(); return 1;})()`);
await sleep(300);
ok('three fast clicks open exactly one modal', (await evalJs("document.querySelectorAll('.modal:not([hidden])').length")) === 1);
await click('#mgo'); await click('#mgo'); await click('#mgo');   // confirm storm
await sleep(1500);
const during = ((await (await api('/api/state')).json()).job || {}).data || {};
ok('confirm storm starts one job, not three', !!during.current || (during.recent || []).length === nJobs0 + 1,
   `current=${during.current ? during.current.action : 'none'} recent=${(during.recent || []).length}`);
await waitFor("document.getElementById('jobstrip').className.indexOf('running') === -1", 15000);

// ── the engine gate: what needs an engine follows the engine, in both directions ──
const life = ((await (await api('/api/state')).json()).lifecycle || {}).data || {};
const serving = Object.values(life.engines || {}).some(e => ['ready', 'degraded', 'wedged'].includes(e.state));
const gated = await evalJs(`(()=>{const o={}; ['flush_cache','abort_all','smoke','diag_bundle','switch'].forEach(a=>{const b=document.querySelector('[data-act="'+a+'"]'); o[a]=b?{disabled:b.disabled,title:b.title}:null;}); return o;})()`);
if (serving) {
  ok('with an engine serving, engine actions are clickable',
     !gated.flush_cache.disabled && !gated.abort_all.disabled && !gated.smoke.disabled, JSON.stringify(gated));
} else {
  ok('with no engine, engine actions are disabled',
     gated.flush_cache.disabled && gated.abort_all.disabled && gated.smoke.disabled, JSON.stringify(gated));
  ok('and they say why', /no engine/i.test(gated.smoke.title), gated.smoke.title);
  ok('actions that need no engine stay available',
     !gated.diag_bundle.disabled && !gated.switch.disabled, JSON.stringify(gated));
  const named = await evalJs("['reqrun','reqwait','reqtok','acclen'].map(i=>document.getElementById(i).textContent).join(' | ')");
  ok('the live-request fields name the empty state instead of shimmering', !named.includes('...'), named);
}

// random clicking everywhere, including nav, rail toggle and every action
events = [];
const storm = await evalJs(`(()=>{
  const sels = ['.rail .nav', '.actbar .btn', '#railbtn', '.btn.mini', '#logbtn', '#rcpbtn', '#regbtn', '#upbtn', '#invbtn', '#joblogbtn'];
  let n = 0;
  for (let i = 0; i < 60; i++) {
    const list = document.querySelectorAll(sels[i % sels.length]);
    if (!list.length) continue;
    const e = list[i % list.length];
    if (e.disabled) continue;
    e.click(); n++;
  }
  return n;
})()`);
await sleep(1200);
// close whatever modal the storm opened
await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 }, sessionId);
await sleep(400);
ok(`click storm (${storm} clicks) raises no exception`, jsErrors().length === 0, jsErrors().join(' | '));
ok('the app still shows live data after the storm', await waitFor(BOOTED, 6000));
const afterStorm = ((await (await api('/api/state')).json()).job || {}).data || {};
ok('the click storm never queued more than one job at a time', !afterStorm.current || afterStorm.locked === true,
   JSON.stringify({ current: !!afterStorm.current, locked: afterStorm.locked }));

// ── 7. the rail collapses, remembers, and keeps its buttons reachable ────────
await click('#railbtn'); await sleep(150);
const min1 = await evalJs("document.body.classList.contains('railmin')");
await goto('/'); await waitFor(BOOTED);
const min2 = await evalJs("document.body.classList.contains('railmin')");
ok('the rail collapse survives a reload', min1 === true && min2 === true, `${min1} then ${min2}`);
await click('#railbtn'); await sleep(150);
ok('the rail expands again', (await evalJs("document.body.classList.contains('railmin')")) === false);

// ── 8. no horizontal scroll at phone width, still no exception ───────────────
await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true }, sessionId);
await sleep(600);
const noHscroll = await evalJs('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1');
ok('no horizontal scrolling at 390 px', noHscroll, await evalJs('document.documentElement.scrollWidth + " vs " + document.documentElement.clientWidth'));
await send('Emulation.clearDeviceMetricsOverride', {}, sessionId);

// ── 9. server side: every started job is in the audit, none half-recorded ────
const finalState = await (await api('/api/state')).json();
const jobData = (finalState.job || {}).data || {};
ok('the server is idle again at the end', !jobData.current && jobData.locked === false, JSON.stringify({ current: !!jobData.current, locked: jobData.locked }));
ok('no collector is in error at the end',
   Object.entries(finalState).filter(([n, w]) => w && w.data && w.data.error && n !== 'engine_info').length === 0,
   Object.entries(finalState).filter(([n, w]) => w && w.data && w.data.error).map(([n, w]) => n + ': ' + w.data.error).join(' | '));

const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true }, sessionId);
writeFileSync(SHOT, Buffer.from(shot.result.data, 'base64'));
ws.close(); chrome.kill('SIGTERM');

const failed = checks.filter(c => !c.ok);
for (const c of checks) console.log(`  ${c.ok ? 'ok  ' : 'FAIL'} ${c.name}${c.ok || !c.detail ? '' : '  <- ' + c.detail}`);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed, screenshot ${SHOT}`);
process.exit(failed.length ? 1 : 0);
