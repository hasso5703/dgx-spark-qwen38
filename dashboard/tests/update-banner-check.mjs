// Does the cockpit actually TELL you a newer version exists? The check behind it is
// server-side and unit-tested; what a browser has to prove is that the banner appears
// when the box is behind, names the command, disappears when it is not, and that the
// Setup line reads correctly offline and when the check is switched off.
// Usage: COCKPIT_BASE=http://127.0.0.1:30090 node dashboard/tests/update-banner-check.mjs
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
const BASE = process.env.COCKPIT_BASE || "http://127.0.0.1:30090";
const key = readFileSync(`${process.env.HOME}/.config/qwen38/api-key`, 'utf8').trim();
const login = await fetch(`${BASE}/api/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
const cookie = (login.headers.get('set-cookie') || '').match(/cockpit=([^;]+)/)?.[1];
if (!cookie) { console.log('login echoue', login.status); process.exit(2); }
const chrome = spawn('/snap/bin/chromium', ['--headless=new', '--remote-debugging-port=0', '--no-first-run', '--disable-gpu', '--hide-scrollbars', '--window-size=1440,1400', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
const wsUrl = await new Promise((res, rej) => { let buf = ''; chrome.stderr.on('data', d => { buf += d; const m = buf.match(/DevTools listening on (ws:\S+)/); if (m) res(m[1]); }); setTimeout(() => rej(new Error('pas d url devtools')), 20000); });
const ws = new WebSocket(wsUrl); await new Promise(r => ws.onopen = r);
let id = 0; const pending = new Map();
ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params, sessionId })); });
const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' });
const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
await send('Network.enable', {}, sessionId); await send('Runtime.enable', {}, sessionId); await send('Page.enable', {}, sessionId);
await send('Network.setCookie', { name: 'cockpit', value: cookie, url: BASE, httpOnly: true, sameSite: 'Strict' }, sessionId);
await send('Page.navigate', { url: BASE + '/' }, sessionId);
await new Promise(r => setTimeout(r, 7000));
const js = async expression => (await send('Runtime.evaluate', { expression, returnByValue: true }, sessionId)).result?.result?.value;
const strip = () => js("[...document.querySelectorAll('#banners .banner')].map(e=>e.textContent).join(' | ')");

let ok = 0, ko = 0;
const check = (name, cond, extra='') => { (cond ? ok++ : ko++); console.log(`  [${cond?' ok ':'FAIL'}] ${name}${extra?'  '+extra:''}`); };

check('a jour: aucun bandeau de mise a jour', !/is out; this box runs/.test(await strip() || ''));
await js("F.update={installed:'v1.15.0',latest:'v1.99.0',behind:true}; banners(lastState||{}, lastErrors||{});");
let s = await strip() || '';
check('en retard: le bandeau apparait', /Version v1\.99\.0 is out; this box runs v1\.15\.0/.test(s));
check('il donne la commande', /git pull && \.\/install\.sh/.test(s));
await js("F.update={installed:'v1.15.1',stale_code:['static/app.js']}; banners(lastState||{}, lastErrors||{});");
s = await strip() || '';
check('code perime: le bandeau apparait', /running older code than the files on disk/.test(s));
check('il nomme le fichier et le remede', /static\/app\.js/.test(s) && /systemctl restart qwen38-dashboard/.test(s));
await js("F.update={installed:'v1.15.1',latest:'v1.15.1',behind:false}; banners(lastState||{}, lastErrors||{});");
check('a jour a nouveau: le bandeau disparait', !/is out; this box runs/.test(await strip() || ''));

await js("rUpdate({installed:'v1.15.0',latest:'v1.99.0',behind:true})");
let line = await js("document.getElementById('upd').textContent");
check('ligne Setup, en retard', /v1\.15\.0/.test(line) && /v1\.99\.0 is out/.test(line), JSON.stringify(line));
await js("rUpdate({installed:'v1.15.1',latest:null})");
line = await js("document.getElementById('upd').textContent");
check('ligne Setup, hors ligne', /unknown/.test(line), JSON.stringify(line));
await js("rUpdate({installed:'v1.15.1',checked:false})");
line = await js("document.getElementById('upd').textContent");
check('ligne Setup, desactive', /check off/.test(line), JSON.stringify(line));

console.log(`\n${ok} verifications passees, ${ko} echouees`);
ws.close(); chrome.kill('SIGTERM');
process.exit(ko ? 1 : 0);
