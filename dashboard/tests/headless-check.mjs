// Headless functional check of the cockpit page (node >= 22, snap chromium): the session
// cookie is obtained server-side with the API key file (nothing is typed into a form),
// injected through CDP, the page loaded, console exceptions collected, the recipes table
// read back and a full-page screenshot saved.
// Usage: node dashboard/tests/headless-check.mjs [width] [height] [path] [screenshot.png]
import { spawn } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
const BASE = 'http://127.0.0.1:30090';
const key = readFileSync(`${process.env.HOME}/.config/qwen38/api-key`, 'utf8').trim();
const login = await fetch(`${BASE}/api/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
const cookie = (login.headers.get('set-cookie') || '').match(/cockpit=([^;]+)/)?.[1];
if (!cookie) { console.log('login failed', login.status); process.exit(2); }
const chrome = spawn('/snap/bin/chromium', ['--headless=new', '--remote-debugging-port=0', '--no-first-run', '--disable-gpu', '--hide-scrollbars', `--window-size=${process.argv[2] || 1440},${process.argv[3] || 2600}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
const wsUrl = await new Promise((res, rej) => { let buf = ''; chrome.stderr.on('data', d => { buf += d; const m = buf.match(/DevTools listening on (ws:\S+)/); if (m) res(m[1]); }); setTimeout(() => rej(new Error('no devtools url: ' + buf.slice(-300))), 20000); });
const ws = new WebSocket(wsUrl); await new Promise(r => ws.onopen = r);
let id = 0; const pending = new Map(); const events = [];
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } else if (m.method) events.push(m); };
const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params, sessionId })); });
const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' });
const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
await send('Network.enable', {}, sessionId); await send('Runtime.enable', {}, sessionId); await send('Page.enable', {}, sessionId);
await send('Network.setCookie', { name: 'cockpit', value: cookie, url: BASE, httpOnly: true, sameSite: 'Strict' }, sessionId);
const target = process.argv[4] || '/';
await send('Page.navigate', { url: /^(https?|file):/.test(target) ? target : BASE + target }, sessionId);
await new Promise(r => setTimeout(r, 5000));
const ev = await send('Runtime.evaluate', { returnByValue: true, expression: `JSON.stringify({
  url: location.pathname, title: document.title,
  rows: [...document.querySelectorAll('#rcptable tbody tr')].map(tr => tr.innerText.replace(/\\s+/g, ' ').slice(0, 220)),
  line: document.getElementById('rcpline')?.textContent, dir: document.getElementById('rcpdir')?.textContent,
  hero: document.getElementById('hero')?.textContent, conn: document.getElementById('connlabel')?.textContent,
  legend: document.getElementById('reslegend')?.textContent.replace(/\\s+/g, ' ').trim() })` }, sessionId);
const errors = events.filter(m => m.method === 'Runtime.exceptionThrown').map(m => m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
const cons = events.filter(m => m.method === 'Runtime.consoleAPICalled' && ['error', 'warning'].includes(m.params.type)).map(m => m.params.args.map(a => a.value || a.description).join(' '));
console.log(JSON.stringify({ page: JSON.parse(ev.result.result.value), exceptions: errors, consoleErrors: cons }, null, 1));
const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: true }, sessionId);
writeFileSync(process.argv[5] || `${process.env.HOME}/flashnext-work/cockpit-shot.png`, Buffer.from(shot.result.data, 'base64'));
ws.close(); chrome.kill('SIGTERM');
