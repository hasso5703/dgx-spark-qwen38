// Resilience check: the cockpit must tell the truth when it loses its server, and come
// back on its own when the server returns. This test OWNS its cockpit: it spawns a
// dry-run instance on a free port, opens the page, kills the server, reads what the user
// would see, restarts the server and checks the page recovers without a reload.
//
//   node dashboard/tests/resilience-check.mjs
//
// Nothing it does can touch the real serving stack: the spawned cockpit is in dry run
// and uses an isolated config directory.
import { spawn } from 'node:child_process';
import { mkdtempSync, copyFileSync, readFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import net from 'node:net';

const HERE = dirname(fileURLToPath(import.meta.url));
const COCKPIT = join(HERE, '..', 'cockpit.py');
const KEYFILE = process.env.COCKPIT_KEY_FILE || `${process.env.HOME}/.config/qwen38/api-key`;
const checks = [];
const ok = (name, cond, detail = '') => { checks.push({ name, ok: !!cond, detail: String(detail).slice(0, 180) }); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

const freePort = () => new Promise(res => { const srv = net.createServer(); srv.listen(0, '127.0.0.1', () => { const p = srv.address().port; srv.close(() => res(p)); }); });

const cfgDir = mkdtempSync(join(tmpdir(), 'cockpit-resilience-'));
copyFileSync(KEYFILE, join(cfgDir, 'api-key'));
const key = readFileSync(KEYFILE, 'utf8').trim();
const PORT = await freePort();
const BASE = `http://127.0.0.1:${PORT}`;

let server = null;
function startServer() {
  server = spawn('python3', [COCKPIT], {
    env: { ...process.env, COCKPIT_DRY_RUN: '1', COCKPIT_PORT: String(PORT), COCKPIT_CONFIG_DIR: cfgDir },
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

let ws = null, chrome = null;
try {
  startServer();
  if (!await waitHealth(true)) { console.error('the spawned cockpit never became healthy'); process.exit(2); }

  const login = await fetch(`${BASE}/api/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
  const cookie = (login.headers.get('set-cookie') || '').match(/cockpit=([^;]+)/)?.[1];
  if (!cookie) { console.error(`login failed: HTTP ${login.status}`); process.exit(2); }

  chrome = spawn('/snap/bin/chromium', ['--headless=new', '--remote-debugging-port=0', '--no-first-run', '--disable-gpu',
    '--hide-scrollbars', '--window-size=1400,900', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  const wsUrl = await new Promise((res, rej) => {
    let buf = ''; chrome.stderr.on('data', d => { buf += d; const m = buf.match(/DevTools listening on (ws:\S+)/); if (m) res(m[1]); });
    setTimeout(() => rej(new Error('no devtools url')), 20000);
  });
  ws = new WebSocket(wsUrl); await new Promise(r => { ws.onopen = r; });
  let id = 0; const pending = new Map(); let events = [];
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } else if (m.method) events.push(m); };
  const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params, sessionId })); });
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
  try { if (ws) ws.close(); } catch { /* closed */ }
  try { if (chrome) chrome.kill('SIGTERM'); } catch { /* gone */ }
  stopServer();
}

const failed = checks.filter(c => !c.ok);
for (const c of checks) console.log(`  ${c.ok ? 'ok  ' : 'FAIL'} ${c.name}${c.ok || !c.detail ? '' : '  <- ' + c.detail}`);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
