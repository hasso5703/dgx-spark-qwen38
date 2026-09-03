// Headless check of the Agent tab against a LIVE cockpit with the relay installed:
// the session is obtained server-side with the API key, injected through CDP for the
// host the relay binds (cookies are per host), the cockpit page is opened on #agent
// and the frame must hold opencode's interface; then the relay is opened directly
// and its network is read (event stream up, no 4xx/5xx, no console error). Two
// screenshots land in /tmp. Exit 0 = every check passed.
//   node dashboard/tests/agent-check.mjs [http://127.0.0.1:30090]
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';

const START = process.argv[2] || process.env.COCKPIT_BASE || 'http://127.0.0.1:30090';
const KEYFILE = process.env.COCKPIT_KEY_FILE || `${process.env.HOME}/.config/qwen38/api-key`;
const CHROME = process.env.CHROME || '/usr/bin/google-chrome';
const checks = [];
const ok = (name, cond, detail = '') => { checks.push({ name, ok: !!cond, detail: String(detail).slice(0, 220) }); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const key = readFileSync(KEYFILE, 'utf8').trim();

async function login(base) {
  const r = await fetch(`${base}/api/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
  const cookie = (r.headers.get('set-cookie') || '').match(/cockpit=([^;]+)/)?.[1];
  if (!cookie) throw new Error(`login failed at ${base}: HTTP ${r.status}`);
  return cookie;
}

// ── where does the relay live? ask the cockpit, then log in through that host ─
const c0 = await login(START);
const state = await (await fetch(`${START}/api/state`, { headers: { cookie: `cockpit=${c0}` } })).json();
const agent = (state.agent || {}).data || {};
ok('agent collector present', !!state.agent);
ok('agent enabled on this cockpit', agent.enabled, JSON.stringify(agent).slice(0, 200));
ok('relay listening', agent.relay && agent.relay.listening, agent.relay && agent.relay.error);
ok('opencode server healthy', agent.server && agent.server.healthy, agent.server && agent.server.error);
if (!(agent.enabled && agent.relay && agent.relay.listening)) { report(); process.exit(1); }
const cockpitPort = new URL(START).port || '80';
const BASE = `http://${agent.relay.bind}:${cockpitPort}`;
const RELAY = `http://${agent.relay.bind}:${agent.relay.port}`;
const cookie = await login(BASE);

// ── the relay's gate, from node (no browser involved) ────────────────────────
ok('relay: 401 without a session', (await fetch(`${RELAY}/global/health`)).status === 401);
ok('relay: 403 with a foreign origin', (await fetch(`${RELAY}/global/health`, { headers: { cookie: `cockpit=${cookie}`, origin: 'http://evil.example' } })).status === 403);
const h = await fetch(`${RELAY}/global/health`, { headers: { cookie: `cockpit=${cookie}` } });
ok('relay: 200 with the session', h.status === 200, h.status);
ok('relay: opencode healthy through it', (await h.json()).healthy === true);
ok('relay: frame-ancestors names the cockpit', (h.headers.get('content-security-policy') || '').includes(`frame-ancestors 'self' http://${agent.relay.bind}:${cockpitPort}`), h.headers.get('content-security-policy'));

// ── browser ──────────────────────────────────────────────────────────────────
const chrome = spawn(CHROME, ['--headless=new', '--no-sandbox', '--remote-debugging-port=0', '--no-first-run', '--disable-gpu', '--hide-scrollbars', '--window-size=1440,900', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
const wsUrl = await new Promise((res, rej) => { let buf = ''; chrome.stderr.on('data', d => { buf += d; const m = buf.match(/DevTools listening on (ws:\S+)/); if (m) res(m[1]); }); setTimeout(() => rej(new Error('no devtools url: ' + buf.slice(-300))), 20000); });
const ws = new WebSocket(wsUrl); await new Promise(r => ws.onopen = r);
let id = 0; const pending = new Map(); const net = []; const consoleErrors = [];
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); return; }
  if (m.method === 'Network.responseReceived') net.push({ url: m.params.response.url, status: m.params.response.status, type: m.params.response.mimeType });
  if (m.method === 'Runtime.exceptionThrown') consoleErrors.push((m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text || '').slice(0, 200));
  if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') consoleErrors.push(m.params.args.map(a => a.value ?? a.description ?? '').join(' ').slice(0, 200));
};
const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params, sessionId })); });
const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' });
const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
await send('Network.enable', {}, sessionId); await send('Runtime.enable', {}, sessionId); await send('Page.enable', {}, sessionId);
await send('Network.setCookie', { name: 'cockpit', value: cookie, url: BASE, httpOnly: true, sameSite: 'Strict' }, sessionId);
const shot = async (file) => { const s = await send('Page.captureScreenshot', { format: 'png' }, sessionId); writeFileSync(file, Buffer.from(s.result.data, 'base64')); };
const evalJs = async (expr) => (await send('Runtime.evaluate', { returnByValue: true, expression: expr }, sessionId)).result?.result?.value;

// 1. the cockpit's Agent tab
await send('Page.navigate', { url: `${BASE}/#agent` }, sessionId);
await sleep(12000);
const tab = await evalJs(`JSON.stringify({
  active: document.querySelector('#tab-agent').classList.contains('active'),
  chip: document.getElementById('agchip').textContent,
  line: document.getElementById('agline').textContent,
  iframe: !!document.getElementById('agiframe'),
  src: document.getElementById('agiframe')?.src || null,
  msgHidden: document.getElementById('agmsg').hidden,
  noteHidden: document.getElementById('agnote').hidden,
  frameH: document.getElementById('agframe').getBoundingClientRect().height,
  railBadge: document.getElementById('bdg-agent').textContent })`);
const t = JSON.parse(tab || '{}');
ok('tab: #agent is the active tab', t.active, tab);
ok('tab: chip says ready', t.chip === 'ready', t.chip);
ok('tab: the frame exists and points at the relay on this host', t.iframe && t.src === `${RELAY}/`, t.src);
ok('tab: the placeholder message is hidden', t.msgHidden);
ok('tab: no host mismatch note', t.noteHidden);
ok('tab: the frame takes the viewport (>= 440px)', t.frameH >= 440, t.frameH);
ok('tab: no rail badge', t.railBadge === '', t.railBadge);
const frameNet = net.filter(n => n.url.startsWith(RELAY));
ok('tab: the frame loaded opencode through the relay (html + assets)', frameNet.some(n => n.status === 200 && n.type === 'text/html') && frameNet.some(n => n.type === 'text/javascript'), `${frameNet.length} relay responses`);
ok('tab: the frame opened the event stream', frameNet.some(n => n.url.endsWith('/global/event') && n.status === 200), frameNet.filter(n => n.url.includes('event')).map(n => n.status).join(','));
ok('tab: no 4xx/5xx from the relay', !frameNet.some(n => n.status >= 400), frameNet.filter(n => n.status >= 400).map(n => `${n.status} ${n.url}`).join(' | '));
await shot('/tmp/cockpit-agent-tab.png');

// 2. the relay opened directly (the "Open in a tab" button)
net.length = 0; consoleErrors.length = 0;
await send('Page.navigate', { url: `${RELAY}/` }, sessionId);
await sleep(10000);
const direct = JSON.parse(await evalJs(`JSON.stringify({ title: document.title, text: document.body.innerText.replace(/\\s+/g, ' ').slice(0, 300) })`) || '{}');
ok('direct: title is OpenCode', direct.title === 'OpenCode', direct.title);
ok('direct: the interface rendered (projects or sessions visible)', /Projets|Projects|session/i.test(direct.text), direct.text);
ok('direct: event stream up', net.some(n => n.url.endsWith('/global/event') && n.status === 200));
ok('direct: no 4xx/5xx', !net.some(n => n.status >= 400 && n.url.startsWith(RELAY)), net.filter(n => n.status >= 400).map(n => `${n.status} ${n.url}`).join(' | '));
ok('direct: no console errors', consoleErrors.length === 0, consoleErrors.join(' || '));
await shot('/tmp/cockpit-agent-direct.png');
chrome.kill('SIGKILL');
report();
process.exit(checks.every(c => c.ok) ? 0 : 1);

function report() {
  for (const c of checks) console.log(`  ${c.ok ? 'ok  ' : 'FAIL'} ${c.name}${c.ok || !c.detail ? '' : `  (${c.detail})`}`);
  console.log(`\n${checks.filter(c => c.ok).length}/${checks.length} checks passed`);
}
