// Click-storm check of the cockpit UI: a headless Chromium clicks everything, several
// times, fast, on every view, and every assertion is checked against the page AND the
// server. It refuses to run against a cockpit that is not in dry run, so nothing it
// clicks can ever touch the real serving stack.
//
//   node dashboard/tests/monkey-check.mjs
//
// It OWNS its cockpit, like resilience-check.mjs: a dry-run instance on loopback with a key
// of its own, its engine, proxy and image lane on a closed port, its update check off and a
// PATH whose box commands answer nothing, so it runs in CI. To drive a dry-run cockpit you
// started yourself instead (one that reads the box's units, say), name it and its key:
//
//   COCKPIT_KEY_FILE=/tmp/x/api-key node dashboard/tests/monkey-check.mjs http://127.0.0.1:30095
//
// (COCKPIT_BASE works for the address too.)
// The invocation this used to give, COCKPIT_PORT=30091, is the port a box's own cockpit
// gives its agent relay, and it ran nowhere to be noticed (found 2026-10-02).
// Exit code 0 = every check passed. Any failure prints the check that failed.
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync, mkdtempSync, mkdirSync, chmodSync, existsSync, rmSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import net from 'node:net';

const HERE = dirname(fileURLToPath(import.meta.url));
const CHROME = process.env.CHROME || ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  '/snap/bin/chromium'].find(existsSync);
const checks = [];
const ok = (name, cond, detail = '') => { checks.push({ name, ok: !!cond, detail: String(detail).slice(0, 200) }); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const root = mkdtempSync(join(tmpdir(), 'cockpit-monkey-'));
let server = null, chrome = null;
// process.exit() skips every finally: an early exit (the cockpit never healthy, the login
// refused, no browser) left the cockpit this spawned running on a port nobody owned. The
// exit signals what is still alive, the browser with SIGKILL so it writes no profile back.
process.on('exit', () => {
  try { if (chrome && chrome.exitCode === null && chrome.signalCode === null) chrome.kill('SIGKILL'); } catch { /* gone */ }
  try { if (server && server.exitCode === null && server.signalCode === null) process.kill(-server.pid, 'SIGTERM'); } catch { /* gone */ }
  rmSync(root, { recursive: true, force: true });
});
process.on('SIGINT', () => process.exit(130));
const SHOT = process.env.COCKPIT_SHOT || join(tmpdir(), 'cockpit-monkey.png');
let BASE = process.argv[2] || process.env.COCKPIT_BASE || '';
let key;
if (BASE) {
  key = readFileSync(process.env.COCKPIT_KEY_FILE || `${process.env.HOME}/.config/qwen38/api-key`, 'utf8').trim();
} else {
  const cfgDir = join(root, 'config'), fence = join(root, 'bin');
  mkdirSync(cfgDir); mkdirSync(fence);
  key = randomBytes(24).toString('hex');
  writeFileSync(join(cfgDir, 'api-key'), key + '\n', { mode: 0o600 });
  for (const cmd of ['docker', 'systemctl', 'journalctl', 'nvidia-smi', 'sudo', 'git', 'curl', 'tailscale', 'opencode']) {
    writeFileSync(join(fence, cmd), '#!/bin/sh\nexit 1\n'); chmodSync(join(fence, cmd), 0o755);
  }
  const port = await new Promise(res => { const srv = net.createServer(); srv.listen(0, '127.0.0.1', () => { const p = srv.address().port; srv.close(() => res(p)); }); });
  BASE = `http://127.0.0.1:${port}`;
  const CLOSED = 'http://127.0.0.1:1';
  server = spawn('python3', [join(HERE, '..', 'cockpit.py')], {
    env: { ...process.env, COCKPIT_DRY_RUN: '1', COCKPIT_PORT: String(port), COCKPIT_CONFIG_DIR: cfgDir,
           COCKPIT_BIND: '127.0.0.1', COCKPIT_AGENT_PORT: '0', COCKPIT_AUTOHEAL: '0', COCKPIT_AUTOFIT: '0',
           COCKPIT_UPDATE_CHECK: '0', COCKPIT_ENGINE: CLOSED, COCKPIT_PROXY: CLOSED, COCKPIT_IMAGE: CLOSED,
           HOME: root, PATH: `${fence}:${process.env.PATH}` },
    stdio: ['ignore', 'ignore', 'ignore'], detached: true,
  });
  const t0 = Date.now();
  for (;;) {
    try { const r = await fetch(`${BASE}/api/health`, { signal: AbortSignal.timeout(1500) }); if (r.ok) break; } catch { /* booting */ }
    if (Date.now() - t0 > 25000) { console.error('the cockpit did not come up'); process.exit(2); }
    await sleep(400);
  }
}
const stopAll = async () => {
  const gone = child => (!child || child.exitCode !== null || child.signalCode !== null) ? null
    : Promise.race([new Promise(r => child.once('exit', r)), sleep(5000)]);
  const browser = gone(chrome);
  try { if (chrome) chrome.kill('SIGTERM'); } catch { /* gone */ }
  await browser;
  const cockpit = gone(server);
  if (server) { try { process.kill(-server.pid, 'SIGTERM'); } catch { try { server.kill('SIGTERM'); } catch { /* gone */ } } }
  await cockpit;
};

// ── session (server side: nothing is typed into the login form) ───────────────
const jar = [];
async function api(path, init = {}) {
  const r = await fetch(BASE + path, { ...init, headers: { ...(init.headers || {}), cookie: jar.join('; ') } });
  const sc = r.headers.get('set-cookie'); if (sc) jar.push(sc.split(';')[0]);
  return r;
}
const login = await api('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
if (!login.ok) { console.error(`login failed: HTTP ${login.status}`); await stopAll(); process.exit(2); }
const cookie = jar.join('; ').match(/cockpit=([^;]+)/)?.[1];

const state0 = await (await api('/api/state')).json();
const cfg = (state0.config || {}).data || {};
if (cfg.dry_run !== true) {
  console.error(`REFUSING to run: ${BASE} is not in dry run (config.dry_run=${cfg.dry_run}).`);
  console.error('Start a second cockpit with COCKPIT_DRY_RUN=1 on another port and point this test at it.');
  await stopAll(); process.exit(3);
}

// ── browser, over a pipe (no network, no name resolved but the page's own address) ──
if (!CHROME) { console.error('no browser: set CHROME to a Chromium or Chrome binary'); await stopAll(); process.exit(2); }
chrome = spawn(CHROME, ['--headless=new', '--remote-debugging-pipe', '--no-first-run', '--disable-gpu',
  '--no-sandbox', `--user-data-dir=${join(root, 'chrome')}`, '--hide-scrollbars', '--window-size=1500,1000',
  '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-extensions',
  '--disable-default-apps', '--no-default-browser-check', '--disable-domain-reliability', '--no-pings',
  '--disable-client-side-phishing-detection', '--disable-features=OptimizationHints,Translate,MediaRouter',
  '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1', 'about:blank'], { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'] });
let id = 0; const pending = new Map(); let events = [];
let inbox = '';
chrome.stdio[4].on('data', d => {
  inbox += d.toString();
  let cut;
  while ((cut = inbox.indexOf('\0')) >= 0) {
    const m = JSON.parse(inbox.slice(0, cut)); inbox = inbox.slice(cut + 1);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } else if (m.method) events.push(m);
  }
});
const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; pending.set(i, r); chrome.stdio[3].write(JSON.stringify({ id: i, method, params, sessionId }) + '\0'); });
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
const BOOTED = "!document.getElementById('serving-name').textContent.startsWith('Reading')";
const ESC = () => send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Escape', code: 'Escape', windowsVirtualKeyCode: 27 }, sessionId);
const openAction = async act => { await click('#actbtn'); await sleep(120); return click(`#menu-list [data-act="${act}"]`); };

// ── 1. first load, every view reachable ──────────────────────────────────────
await goto('/');
ok('page boots and applies a first state', await waitFor(BOOTED), 'the spine still reads "Reading the box" after 12 s');
ok('no exception on first load', jsErrors().length === 0, jsErrors().join(' | '));

const VIEWS = ['now', 'lanes', 'traffic', 'machine', 'agent', 'decide', 'image', 'video', 'library', 'logs', 'settings'];
for (const t of VIEWS) {
  await click(`.rail .nav[data-view="${t}"]`);
  await sleep(160);
  const active = await evalJs(`document.getElementById('view-${t}').classList.contains('active') && location.hash === '#${t}'`);
  ok(`nav switches to ${t}`, active);
}
ok('no exception after visiting every view', jsErrors().length === 0, jsErrors().join(' | '));

// ── 2. reload on every hash, the old tab names included (links in the docs) ──
for (const t of VIEWS.concat(['overview', 'engines', 'requests', 'systemone', 'models', 'setup'])) {
  await goto('/#' + t);
  const booted = await waitFor(BOOTED, 9000);
  const errs = jsErrors();
  const landed = await evalJs("document.querySelectorAll('.view.active').length === 1");
  ok(`reload on #${t} boots the app on one view`, booted && landed && errs.length === 0, errs.join(' | ') || (booted ? 'no single active view' : 'never applied a state'));
}

// ── 3. views say something in every state (no silent blank, no stuck placeholder) ──
await goto('/');
await waitFor(BOOTED);
await sleep(1500);
const stuck = await evalJs(`(()=>{
  const bad = [];
  for (const v of ${JSON.stringify(VIEWS)}){
    showView(v);
    document.querySelectorAll('#view-' + v + ' dd, #view-' + v + ' .readout .v, #view-' + v + ' .vital .v').forEach(d => {
      if (d.textContent.trim() === '…' && d.offsetParent !== null) bad.push((d.id || d.className || '?') + ' in ' + v);
    });
  }
  showView('now');
  return bad;
})()`);
ok('no visible field stuck on "…" after load', stuck.length === 0, stuck.join(', '));
const renderErrs = await evalJs(`[...document.querySelectorAll('[data-src] .age')].map(e => e.textContent).filter(t => /render error/.test(t))`);
ok('no panel reports a render error', renderErrs.length === 0, renderErrs.join(' | '));

// Jobs are told apart by id. The server's history keeps the last 5, so a count stood still
// on a cockpit with five behind it, and /api/state is sampled once a second: read once,
// 1.5 s after a click, it missed a job that had started (seen on a cockpit reading the box,
// 2026-10-02). A job is looked for until it shows, and "nothing started" is read a sampler
// period after the clicks.
const jobIds = async () => { const j = ((await (await api('/api/state')).json()).job || {}).data || {};
  return new Set([...(j.current ? [j.current.id] : []), ...(j.recent || []).map(r => r.id)]); };
const newJobs = (before, now) => [...now].filter(id => !before.has(id));
const waitIdle = async () => { for (let i = 0; i < 60; i++) { const st = await (await api('/api/state')).json(); if (!((st.job || {}).data || {}).current) break; await sleep(500); } };

// ── 4. the sheet: opens, shows the command, Escape closes, no action runs ─────
const jobsBefore = await jobIds();
await openAction('diag_bundle');
await sleep(200);
ok('an action opens the sheet', await evalJs("!document.getElementById('scrim').hidden"));
ok('the sheet shows the exact command', (await evalJs("document.getElementById('sh-cmd').textContent")).length > 3);
ok('the sheet explains what happens', (await evalJs("document.getElementById('sh-lede').textContent")).length > 20);
ok('the sheet names its verb', /bundle/i.test(await evalJs("document.getElementById('sh-go').textContent")), await evalJs("document.getElementById('sh-go').textContent"));
await ESC(); await sleep(200);
ok('Escape closes the sheet', await evalJs("document.getElementById('scrim').hidden"));
await sleep(1500);
const startedByCancel = newJobs(jobsBefore, await jobIds());
ok('cancelling started nothing', startedByCancel.length === 0, `${startedByCancel.length} new job(s)`);

// ── 5. a second action while one runs is refused, and the page says so ───────
await openAction('diag_bundle'); await sleep(200);
await click('#sh-go');
ok('the dock appears', await waitFor("!document.getElementById('dock').hidden", 6000));
ok('the dock names the running action', /diagnostics bundle/i.test(await evalJs("document.getElementById('dock-what').textContent")),
   await evalJs("document.getElementById('dock-what').textContent"));
await click('#actbtn'); await sleep(150);
const disabled = await waitFor("[...document.querySelectorAll('#menu-list [data-act]')].every(b => b.disabled)", 4000);
ok('other actions are disabled while a job runs, and say why', disabled && /another action is running/i.test(await evalJs("document.querySelector('#menu-list [data-act]').title")),
   await evalJs("document.querySelector('#menu-list [data-act]').title"));
await ESC(); await sleep(150);
const csrf = (await (await api('/api/csrf', { method: 'POST' })).json()).token;
const second = await api('/api/action', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: 'diag_bundle', params: {}, csrf }) });
const secondBody = await second.json();
ok('the server refuses a second job with 409 busy', second.status === 409 && secondBody.error === 'busy', `${second.status} ${JSON.stringify(secondBody).slice(0, 120)}`);
ok('the job finishes and the dock reports it', await waitFor("!/warn/.test(document.getElementById('dock-lamp').className) || document.getElementById('dock').hidden", 15000),
   await evalJs("document.getElementById('dock-lamp').className"));

// ── 6. double click and click storm: never two jobs, never an exception ──────
await waitIdle();
await sleep(1500);
const before = await jobIds();
events = [];
await click('#actbtn'); await sleep(120);
await evalJs(`(()=>{const b=document.querySelector('#menu-list [data-act="diag_bundle"]'); b.click(); b.click(); b.click(); return 1;})()`);
await sleep(300);
ok('three fast clicks open exactly one sheet', (await evalJs("[...document.querySelectorAll('.scrim')].filter(s => !s.hidden).length")) === 1);
await click('#sh-go'); await click('#sh-go'); await click('#sh-go');
for (let i = 0; i < 60 && !newJobs(before, await jobIds()).length; i++) await sleep(250);
await waitIdle();
await sleep(1500);
const stormJobs = newJobs(before, await jobIds());
ok('a confirm storm starts one job, not three', stormJobs.length === 1, `${stormJobs.length} new job(s)`);

// ── the engine gate: what needs a text engine follows it, in both directions ──
const life = ((await (await api('/api/state')).json()).lifecycle || {}).data || {};
const textUp = Object.entries(life.engines || {}).some(([u, e]) => (u.includes('sglang') || u.includes('flash')) && ['ready', 'degraded', 'wedged'].includes(e.state));
await click('#actbtn'); await sleep(200);
const gated = await evalJs(`(()=>{const o={}; ['flush_cache','abort_all','smoke','diag_bundle'].forEach(a=>{const b=document.querySelector('#menu-list [data-act="'+a+'"]'); o[a]=b?{disabled:b.disabled,title:b.title}:null;}); return o;})()`);
await ESC(); await sleep(150);
if (textUp) {
  ok('with a text engine serving, engine actions are clickable', !gated.flush_cache.disabled && !gated.abort_all.disabled && !gated.smoke.disabled, JSON.stringify(gated));
} else {
  ok('with no text engine, engine actions are disabled', gated.flush_cache.disabled && gated.abort_all.disabled && gated.smoke.disabled, JSON.stringify(gated));
  ok('and they say why', /text engine|lane is serving/i.test(gated.smoke.title), gated.smoke.title);
  ok('an action that needs no engine stays available', !gated.diag_bundle.disabled, JSON.stringify(gated.diag_bundle));
  const named = await evalJs("['tr-run','tr-wait','tr-tok','tr-acc'].map(i=>document.getElementById(i).textContent).join(' | ')");
  ok('the live-request fields name the empty state instead of a placeholder', !named.includes('…'), named);
}

// ── 7. a click storm everywhere, the lane rack and the studios included ───────
const beforeStorm = await jobIds();
events = [];
const storm = await evalJs(`(()=>{
  const sels = ['.rail .nav', '#railbtn', '.bay', '#rcp-btn', '#reg-btn', '#up-btn', '#inv-btn', '#log-btn', '#actbtn', '#menu-list [data-act]',
                '.examples button', '.seg button', '#vid-dice', '#vid-reset', '#img-reset', '#dock-log', '#serving', '[data-addq]'];
  let n = 0;
  for (let i = 0; i < 90; i++) {
    const list = document.querySelectorAll(sels[i % sels.length]);
    if (!list.length) continue;
    const e = list[i % list.length];
    if (e.disabled) continue;
    e.click(); n++;
    if (!document.getElementById('scrim').hidden) closeSheet();
    if (!document.getElementById('menu').hidden && i % 3) closeMenu();
  }
  return n;
})()`);
await sleep(1200);
await ESC(); await sleep(300); await ESC(); await sleep(300);
ok(`click storm (${storm} clicks) raises no exception`, jsErrors().length === 0, jsErrors().join(' | '));
ok('the app still shows live data after the storm', await waitFor(BOOTED, 6000));
await sleep(1500);
const stormStarted = newJobs(beforeStorm, await jobIds());
ok('the click storm started no job (nothing was confirmed)', stormStarted.length === 0, `${stormStarted.length} new job(s)`);

// ── 8. the rail collapses, remembers, and its buttons keep their names ────────
await goto('/'); await waitFor(BOOTED);
// from a known state: the storm clicked the collapse button an unknown number of times
if (await evalJs("document.body.classList.contains('railmin')")) { await click('#railbtn'); await sleep(150); }
await click('#railbtn'); await sleep(150);
const min1 = await evalJs("document.body.classList.contains('railmin')");
await goto('/'); await waitFor(BOOTED);
const min2 = await evalJs("document.body.classList.contains('railmin')");
ok('the rail collapse survives a reload', min1 === true && min2 === true, `${min1} then ${min2}`);
ok('collapsed, every rail button keeps an accessible name', await evalJs("[...document.querySelectorAll('.rail .nav')].every(b => (b.getAttribute('aria-label') || '').length > 2)"));
await click('#railbtn'); await sleep(150);
ok('the rail expands again', (await evalJs("document.body.classList.contains('railmin')")) === false);

// ── 9. no horizontal scroll at phone width, on any view, still no exception ───
await send('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true }, sessionId);
await sleep(600);
const hs = [];
for (const v of VIEWS) { await evalJs(`showView('${v}')`); await sleep(250);
  if (!await evalJs('document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1')) hs.push(v); }
ok('no horizontal scrolling at 390 px on any view', hs.length === 0, hs.join(', '));
ok('the menu button opens the rail as a drawer', await evalJs("(document.getElementById('menubtn').click(), document.body.classList.contains('railopen'))"));
await send('Emulation.clearDeviceMetricsOverride', {}, sessionId);
await sleep(400);   // the drawer's slide (0.3 s) ends: the screenshot caught the rail mid-way
ok('no exception at phone width', jsErrors().length === 0, jsErrors().join(' | '));

// ── 10. server side: idle at the end, no collector in error ───────────────────
const finalState = await (await api('/api/state')).json();
const jobData = (finalState.job || {}).data || {};
ok('the server is idle again at the end', !jobData.current && jobData.locked === false, JSON.stringify({ current: !!jobData.current, locked: jobData.locked }));
ok('no collector is in error at the end',
   Object.entries(finalState).filter(([n, w]) => w && w.data && w.data.error && n !== 'engine_info').length === 0,
   Object.entries(finalState).filter(([n, w]) => w && w.data && w.data.error).map(([n, w]) => n + ': ' + w.data.error).join(' | '));

const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true }, sessionId);
writeFileSync(SHOT, Buffer.from(shot.result.data, 'base64'));
await stopAll();

const failed = checks.filter(c => !c.ok);
for (const c of checks) console.log(`  ${c.ok ? 'ok  ' : 'FAIL'} ${c.name}${c.ok || !c.detail ? '' : '  <- ' + c.detail}`);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed, screenshot ${SHOT}`);
process.exit(failed.length ? 1 : 0);
