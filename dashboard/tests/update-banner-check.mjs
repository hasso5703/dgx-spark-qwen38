// Does the cockpit actually TELL you a newer version exists? The check behind it is
// server-side and unit-tested; what a browser has to prove is that the banner appears when
// the box is behind, names the command, disappears when it is not, that a cockpit running
// older code than its files says so with the file and the remedy, and that the Setup line
// reads correctly behind, offline and with the check switched off.
//
//   node dashboard/tests/update-banner-check.mjs
//
// It OWNS its cockpit, like resilience-check.mjs: a dry-run instance on loopback with a key
// of its own, its engine, proxy and image lane on a closed port, its own update check off,
// and a PATH whose box commands answer nothing. Each state is put into the page and read
// back in one evaluation, so the page's own stream cannot draw over it in between. It used
// to drive a live cockpit by functions the redesign of 2026-09-30 renamed (banners(),
// rUpdate(), #upd), so 7 of its 9 checks failed on every cockpit since, and it ran nowhere
// to say so (found 2026-10-02).
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync, writeFileSync, chmodSync, existsSync, rmSync } from 'node:fs';
import { randomBytes } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import net from 'node:net';
const HERE = dirname(fileURLToPath(import.meta.url));
const COCKPIT = join(HERE, '..', 'cockpit.py');
const CHROME = process.env.CHROME || ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
  '/snap/bin/chromium'].find(existsSync);
const checks = [];
const ok = (name, cond, detail = '') => { checks.push({ name, ok: !!cond, detail: String(detail).slice(0, 180) }); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const freePort = () => new Promise(res => { const srv = net.createServer(); srv.listen(0, '127.0.0.1', () => { const p = srv.address().port; srv.close(() => res(p)); }); });
const root = mkdtempSync(join(tmpdir(), 'cockpit-update-banner-'));
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
// and SIGTERM, which a CI stopped through its process group sends: it ended node without
// 'exit', and the cockpit, in a session of its own, kept its port (found 2026-10-07)
process.on('SIGTERM', () => process.exit(143));
const cfgDir = join(root, 'config');
const fence = join(root, 'bin');
mkdirSync(cfgDir); mkdirSync(fence);
const key = randomBytes(24).toString('hex');
writeFileSync(join(cfgDir, 'api-key'), key + '\n', { mode: 0o600 });
for (const cmd of ['docker', 'systemctl', 'journalctl', 'nvidia-smi', 'sudo', 'git', 'curl', 'tailscale', 'opencode']) {
  writeFileSync(join(fence, cmd), '#!/bin/sh\nexit 1\n'); chmodSync(join(fence, cmd), 0o755);
}
const PORT = await freePort();
const BASE = `http://127.0.0.1:${PORT}`;
const CLOSED = 'http://127.0.0.1:1';
server = spawn('python3', [COCKPIT], {
  env: { ...process.env, COCKPIT_DRY_RUN: '1', COCKPIT_PORT: String(PORT), COCKPIT_CONFIG_DIR: cfgDir,
         COCKPIT_BIND: '127.0.0.1', COCKPIT_AGENT_PORT: '0', COCKPIT_AUTOHEAL: '0', COCKPIT_AUTOFIT: '0',
         COCKPIT_UPDATE_CHECK: '0', COCKPIT_ENGINE: CLOSED, COCKPIT_PROXY: CLOSED, COCKPIT_IMAGE: CLOSED,
         HOME: root, PATH: `${fence}:${process.env.PATH}` },
  stdio: ['ignore', 'ignore', 'ignore'], detached: true,
});
try {
  const t0 = Date.now();
  for (;;) {
    try { const r = await fetch(`${BASE}/api/health`, { signal: AbortSignal.timeout(1500) }); if (r.ok) break; } catch { /* booting */ }
    if (Date.now() - t0 > 25000) { console.error('the cockpit did not come up'); process.exit(2); }
    await sleep(400);
  }
  const login = await fetch(`${BASE}/api/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                  body: JSON.stringify({ key }) });
  const cookie = (login.headers.get('set-cookie') || '').match(/cockpit=([^;]+)/)?.[1];
  if (!cookie) { console.error(`login failed: HTTP ${login.status}`); process.exit(2); }
  if (!CHROME) { console.error('no browser: set CHROME to a Chromium or Chrome binary'); process.exit(2); }
  chrome = spawn(CHROME, ['--headless=new', '--remote-debugging-pipe', '--no-first-run', '--disable-gpu',
    '--no-sandbox', `--user-data-dir=${join(root, 'chrome')}`, '--hide-scrollbars', '--window-size=1400,900',
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
  await send('Runtime.enable', {}, sessionId); await send('Page.enable', {}, sessionId); await send('Network.enable', {}, sessionId);
  await send('Network.setCookie', { name: 'cockpit', value: cookie, url: BASE, httpOnly: true, sameSite: 'Strict' }, sessionId);
  const evalJs = async expression => {
    const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
    if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || 'eval threw');
    return r.result.result.value;
  };
  const waitFor = async (expression, ms = 20000) => {
    const t0 = Date.now();
    for (;;) {
      try { if (await evalJs(expression)) return true; } catch { /* navigating */ }
      if (Date.now() - t0 > ms) return false;
      await sleep(300);
    }
  };
  const jsErrors = () => events.filter(m => m.method === 'Runtime.exceptionThrown').map(m => m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text || 'exception');
  await send('Page.navigate', { url: BASE + '/' }, sessionId);
  ok('the page is live', await waitFor("document.getElementById('conn-lbl').textContent === 'live'"),
     await evalJs("document.getElementById('conn-lbl').textContent"));
  // one evaluation per state: set it, draw, read
  const banners = upd => evalJs(`(() => { F.update = ${JSON.stringify(upd)}; renderBanners(lastState || {}, lastErrors || {});
    return [...document.querySelectorAll('#banners .banner')].map(e => e.textContent).join(' | '); })()`);
  const setupLine = upd => evalJs(`(() => { (HANDLERS.update || []).forEach(f => f(${JSON.stringify(upd)}));
    return document.getElementById('rp-upd').textContent; })()`);

  let s = await banners({ installed: 'v1.15.1', latest: 'v1.15.1', behind: false });
  ok('up to date: no update banner', !/is out; this box runs/.test(s), s);
  s = await banners({ installed: 'v1.15.0', latest: 'v1.99.0', behind: true });
  ok('behind: the banner names both versions', /Version v1\.99\.0 is out; this box runs v1\.15\.0/.test(s), s);
  ok('and gives the command', /git pull && \.\/install\.sh/.test(s), s);
  s = await banners({ installed: 'v1.15.1', stale_code: ['static/js/base.js'] });
  ok('older code than its files: the banner says so', /runs older code than the files on disk/.test(s), s);
  ok('and names the file and the remedy', /static\/js\/base\.js/.test(s) && /systemctl restart qwen38-dashboard/.test(s), s);
  s = await banners({ installed: 'v1.15.1', latest: 'v1.15.1', behind: false });
  ok('up to date again: the banner goes', !/is out; this box runs/.test(s) && !/older code/.test(s), s);
  let line = await setupLine({ installed: 'v1.15.0', latest: 'v1.99.0', behind: true });
  ok('Setup line, behind', /v1\.15\.0/.test(line) && /v1\.99\.0 is out/.test(line), line);
  line = await setupLine({ installed: 'v1.15.1', latest: null });
  ok('Setup line, offline', /unknown/.test(line), line);
  line = await setupLine({ installed: 'v1.15.1', checked: false });
  ok('Setup line, check switched off', /check is off/.test(line), line);
  ok('no exception', jsErrors().length === 0, jsErrors().join(' | '));
} finally {
  const gone = child => (!child || child.exitCode !== null || child.signalCode !== null) ? null
    : Promise.race([new Promise(r => child.once('exit', r)), sleep(5000)]);
  const browser = gone(chrome);
  try { if (chrome) chrome.kill('SIGTERM'); } catch { /* gone */ }
  await browser;
  const cockpit = gone(server);
  try { process.kill(-server.pid, 'SIGTERM'); } catch { try { server.kill('SIGTERM'); } catch { /* gone */ } }
  await cockpit;
}

const failed = checks.filter(c => !c.ok);
for (const c of checks) console.log(`  ${c.ok ? 'ok  ' : 'FAIL'} ${c.name}${c.ok || !c.detail ? '' : '  <- ' + c.detail}`);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
