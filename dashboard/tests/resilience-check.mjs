// Resilience check: the cockpit must tell the truth when it loses its server, and come
// back on its own when the server returns. This test OWNS its cockpit: it spawns a
// dry-run instance on a free port, opens the page, kills the server, reads what the user
// would see, restarts the server and checks the page recovers without a reload.
//
//   node dashboard/tests/resilience-check.mjs
//
// Nothing it does can touch the real serving stack: the spawned cockpit is in dry run,
// on loopback, with its own config directory and a key of its own, its engine, proxy and
// image lane on a closed port, and a PATH whose docker, systemctl, journalctl, nvidia-smi,
// sudo and git answer nothing. It used to copy the box's real API key into a temporary
// directory it never removed, and its cockpit read the box it ran on (found in review,
// 2026-09-24). It runs in CI, which is why it speaks to the browser over a pipe (no
// WebSocket client needed) and takes the browser from CHROME.
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

const root = mkdtempSync(join(tmpdir(), 'cockpit-resilience-'));
process.on('exit', () => rmSync(root, { recursive: true, force: true }));
process.on('SIGINT', () => process.exit(130));
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

let server = null;
function startServer() {
  server = spawn('python3', [COCKPIT], {
    env: { ...process.env, COCKPIT_DRY_RUN: '1', COCKPIT_PORT: String(PORT), COCKPIT_CONFIG_DIR: cfgDir,
           COCKPIT_BIND: '127.0.0.1', COCKPIT_AGENT_PORT: '0', COCKPIT_AUTOHEAL: '0', COCKPIT_AUTOFIT: '0',
           COCKPIT_UPDATE_CHECK: '0', COCKPIT_ENGINE: CLOSED, COCKPIT_PROXY: CLOSED, COCKPIT_IMAGE: CLOSED,
           HOME: root, PATH: `${fence}:${process.env.PATH}` },
    stdio: ['ignore', 'ignore', 'ignore'], detached: true,
  });
}
async function waitHealth(up, ms = 25000) {
  const t0 = Date.now();
  for (;;) {
    let alive = false;
    try { const r = await fetch(`${BASE}/api/health`, { signal: AbortSignal.timeout(1500) }); alive = r.ok; } catch { alive = false; }
    if (alive === up) return true;
    if (Date.now() - t0 > ms) return false;
    await sleep(400);
  }
}
function stopServer() {
  if (!server) return;
  try { process.kill(-server.pid, 'SIGTERM'); } catch { try { server.kill('SIGTERM'); } catch { /* gone */ } }
  server = null;
}

let chrome = null;
try {
  startServer();
  if (!await waitHealth(true)) { console.error('the spawned cockpit never became healthy'); process.exit(2); }

  const login = await fetch(`${BASE}/api/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
  const cookie = (login.headers.get('set-cookie') || '').match(/cockpit=([^;]+)/)?.[1];
  if (!cookie) { console.error(`login failed: HTTP ${login.status}`); process.exit(2); }

  if (!CHROME) { console.error('no browser: set CHROME to a Chromium or Chrome binary'); process.exit(2); }
  // CDP over --remote-debugging-pipe: the browser reads commands on fd 3 and writes on fd 4,
  // each message a JSON text ended by a NUL byte.
  // A fresh profile otherwise spends the run fetching components and safe-browsing lists
  // from Google (59 name lookups and 70 connections in one run): no name resolves here
  // but the page's own address, which is a literal.
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
  const jsErrors = () => events.filter(m => m.method === 'Runtime.exceptionThrown').map(m => m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);

  await send('Page.navigate', { url: BASE + '/' }, sessionId);
  ok('the page connects to the live stream', await waitFor("document.getElementById('connlabel').textContent === 'live'"),
     await evalJs("document.getElementById('connlabel').textContent"));
  ok('data is flowing', await waitFor("document.getElementById('lanename').textContent !== '...'"));

  // ── pull the plug ─────────────────────────────────────────────────────────
  events = [];
  stopServer();
  await waitHealth(false, 15000);
  ok('the header stops claiming the stream is live',
     await waitFor("document.getElementById('connlabel').textContent !== 'live'", 15000),
     await evalJs("document.getElementById('connlabel').textContent"));
  ok('a banner says the connection is lost',
     await waitFor("[...document.querySelectorAll('#banners .banner')].some(b => /Connection lost/i.test(b.textContent))", 15000),
     await evalJs("[...document.querySelectorAll('#banners .banner')].map(b=>b.textContent.slice(0,60)).join(' | ')"));
  ok('the panels are marked stale rather than looking fresh',
     await waitFor("document.querySelectorAll('section.panel.stale').length > 0", 20000),
     await evalJs("document.querySelectorAll('section.panel.stale').length + ' stale panels'"));
  ok('every action is disabled while the cockpit is unreachable',
     await waitFor("[...document.querySelectorAll('.actbar .btn')].every(b => b.disabled)", 8000),
     await evalJs("[...document.querySelectorAll('.actbar .btn')].filter(b=>!b.disabled).map(b=>b.textContent).join(',')"));
  await evalJs("(()=>{const b=document.querySelector('[data-act=\"smoke\"]'); b.disabled=false; b.click(); return 1;})()");
  await sleep(400);
  ok('forcing a click while offline explains itself instead of doing nothing',
     await evalJs("!document.getElementById('toast').hidden && /unreachable/i.test(document.getElementById('toast').textContent)"),
     await evalJs("document.getElementById('toast').textContent"));
  ok('no modal was opened while offline', await evalJs("document.getElementById('modal').hidden"));
  ok('losing the server raises no exception', jsErrors().length === 0, jsErrors().join(' | '));

  // ── plug it back in ───────────────────────────────────────────────────────
  events = [];
  startServer();
  ok('the cockpit answers again', await waitHealth(true, 25000));
  ok('the page reconnects on its own, without a reload',
     await waitFor("['live','polling'].includes(document.getElementById('connlabel').textContent)", 30000),
     await evalJs("document.getElementById('connlabel').textContent"));
  ok('the lost-connection banner clears',
     await waitFor("![...document.querySelectorAll('#banners .banner')].some(b => /Connection lost/i.test(b.textContent))", 15000));
  ok('the panels are fresh again',
     await waitFor("document.querySelectorAll('section.panel.stale').length === 0", 25000),
     await evalJs("[...document.querySelectorAll('section.panel.stale')].map(s=>s.querySelector('h3').textContent.trim().slice(0,30)).join(' | ')"));
  ok('actions can be used again',
     await waitFor("[...document.querySelectorAll('.actbar .btn')].some(b => !b.disabled)", 12000));
  ok('it climbs back to the live stream, not just polling',
     await waitFor("document.getElementById('connlabel').textContent === 'live'", 30000),
     await evalJs("document.getElementById('connlabel').textContent"));
  ok('recovery raises no exception', jsErrors().length === 0, jsErrors().join(' | '));
} finally {
  // Both are waited for: the temporary directory goes when this process exits, and a
  // browser still shutting down writes its profile back into it after it was removed.
  const gone = child => (!child || child.exitCode !== null || child.signalCode !== null) ? null
    : Promise.race([new Promise(r => child.once('exit', r)), sleep(5000)]);
  const browser = gone(chrome);
  try { if (chrome) chrome.kill('SIGTERM'); } catch { /* gone */ }
  await browser;
  const cockpit = gone(server);
  stopServer();
  await cockpit;
}

const failed = checks.filter(c => !c.ok);
for (const c of checks) console.log(`  ${c.ok ? 'ok  ' : 'FAIL'} ${c.name}${c.ok || !c.detail ? '' : '  <- ' + c.detail}`);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
