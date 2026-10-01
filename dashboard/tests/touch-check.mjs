// Touch check: the cockpit page under a finger. A headless Chromium emulates phones and a
// tablet (device metrics, touch) and drives the page with REAL input events at the place a
// finger lands, then reads what the page did. Every other browser check switched views
// with element.click() from script, which ignores what covers the element: on a phone the
// drawer's own veil covered every rail item, a tap only closed the drawer, and all of them
// stayed green (found on the reference box's phone, 2026-09-30).
//
//   node dashboard/tests/touch-check.mjs
//
// It OWNS its cockpit, like resilience-check.mjs: a dry-run instance on loopback with a key
// of its own, its engine, proxy and image lane on a closed port, and a PATH whose box
// commands answer nothing. Nothing it taps can reach the real serving stack. It runs in CI,
// so it speaks to the browser over a pipe and takes the browser from CHROME.
//
// What it holds, per device:
//   * the drawer opens under a tap, every rail item is the element under its own centre,
//     and a tap on each one shows that view, marks it current and closes the drawer;
//   * a tap on the veil closes the drawer and changes nothing else; Escape does too and
//     gives the focus back to the menu button;
//   * on every view, no visible control is covered by another element where a finger
//     would land (a control scrolled under the sticky head or folded in a closed
//     <details> is not covered: it is not shown);
//   * the Agent's fullscreen frame covers the head, and its exit button can be tapped;
//   * on a phone, the request log and the Library's wide tables fit the screen, and each
//     request's outcome is on screen;
//   * with an iPhone's safe-area insets, the head, the toasts and the fullscreen frame keep
//     clear of the notch and the home indicator.
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
const ok = (name, cond, detail = '') => { checks.push({ name, ok: !!cond, detail: String(detail).slice(0, 400) }); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
const freePort = () => new Promise(res => { const srv = net.createServer(); srv.listen(0, '127.0.0.1', () => { const p = srv.address().port; srv.close(() => res(p)); }); });

const VIEWS = ['now', 'lanes', 'traffic', 'machine', 'agent', 'decide', 'image', 'video', 'library', 'logs', 'settings'];
const PHONES = [
  { name: 'iPhone SE', w: 375, h: 667, dpr: 2 },
  { name: 'iPhone 15', w: 393, h: 852, dpr: 3 },
  { name: 'iPhone 15 Pro Max', w: 430, h: 932, dpr: 3 },
  { name: 'iPhone 15 landscape', w: 852, h: 393, dpr: 3 },
  { name: 'iPad portrait', w: 820, h: 1180, dpr: 2 },
];
const DESKTOP = { name: 'laptop', w: 1366, h: 768, dpr: 1 };

const root = mkdtempSync(join(tmpdir(), 'cockpit-touch-'));
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

let server = null, chrome = null;
const gone = child => (!child || child.exitCode !== null || child.signalCode !== null) ? null
  : Promise.race([new Promise(r => child.once('exit', r)), sleep(5000)]);
try {
  server = spawn('python3', [COCKPIT], {
    env: { ...process.env, COCKPIT_DRY_RUN: '1', COCKPIT_PORT: String(PORT), COCKPIT_CONFIG_DIR: cfgDir,
           COCKPIT_BIND: '127.0.0.1', COCKPIT_AGENT_PORT: '0', COCKPIT_AUTOHEAL: '0', COCKPIT_AUTOFIT: '0',
           COCKPIT_UPDATE_CHECK: '0', COCKPIT_ENGINE: CLOSED, COCKPIT_PROXY: CLOSED, COCKPIT_IMAGE: CLOSED,
           HOME: root, PATH: `${fence}:${process.env.PATH}` },
    stdio: ['ignore', 'ignore', 'ignore'], detached: true,
  });
  const t0 = Date.now();
  for (;;) {
    let up = false;
    try { up = (await fetch(`${BASE}/api/health`, { signal: AbortSignal.timeout(1500) })).ok; } catch { up = false; }
    if (up) break;
    if (Date.now() - t0 > 25000) { console.error('the spawned cockpit never became healthy'); process.exit(2); }
    await sleep(300);
  }
  const login = await fetch(`${BASE}/api/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }) });
  const cookie = (login.headers.get('set-cookie') || '').match(/cockpit=([^;]+)/)?.[1];
  if (!cookie) { console.error(`login failed: HTTP ${login.status}`); process.exit(2); }
  if (!CHROME) { console.error('no browser: set CHROME to a Chromium or Chrome binary'); process.exit(2); }

  // CDP over --remote-debugging-pipe (fd 3 in, fd 4 out, NUL-terminated JSON), and a
  // profile that asks nothing of the network: only the page's own literal address resolves.
  chrome = spawn(CHROME, ['--headless=new', '--remote-debugging-pipe', '--no-first-run', '--disable-gpu',
    '--no-sandbox', `--user-data-dir=${join(root, 'chrome')}`, '--hide-scrollbars', '--window-size=1400,900',
    '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-extensions',
    '--disable-default-apps', '--no-default-browser-check', '--disable-domain-reliability', '--no-pings',
    '--disable-client-side-phishing-detection', '--disable-features=OptimizationHints,Translate,MediaRouter',
    '--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1', 'about:blank'], { stdio: ['ignore', 'ignore', 'ignore', 'pipe', 'pipe'] });
  let id = 0; const pending = new Map(); const events = []; let inbox = '';
  chrome.stdio[4].on('data', d => {
    inbox += d.toString(); let cut;
    while ((cut = inbox.indexOf('\0')) >= 0) {
      const m = JSON.parse(inbox.slice(0, cut)); inbox = inbox.slice(cut + 1);
      if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } else if (m.method) events.push(m);
    }
  });
  const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; pending.set(i, r); chrome.stdio[3].write(JSON.stringify({ id: i, method, params, sessionId }) + '\0'); });
  const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' });
  const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
  const cdp = (m, p = {}) => send(m, p, sessionId);
  await cdp('Runtime.enable'); await cdp('Page.enable'); await cdp('Network.enable');
  await cdp('Network.setCookie', { name: 'cockpit', value: cookie, url: BASE, httpOnly: true, sameSite: 'Strict' });

  const evalJs = async expression => {
    const r = await cdp('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || 'eval threw');
    return r.result.result.value;
  };
  const waitFor = async (expression, ms = 8000) => {
    const t = Date.now();
    for (;;) {
      try { if (await evalJs(expression)) return true; } catch { /* navigating */ }
      if (Date.now() - t > ms) return false;
      await sleep(100);
    }
  };
  const jsErrors = () => events.filter(m => m.method === 'Runtime.exceptionThrown')
    .map(m => m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text);
  const device = async (d, touch) => {
    await cdp('Emulation.setDeviceMetricsOverride', { width: d.w, height: d.h, deviceScaleFactor: d.dpr, mobile: touch });
    await cdp('Emulation.setTouchEmulationEnabled', { enabled: touch, maxTouchPoints: touch ? 5 : 0 });
    await cdp('Emulation.setEmitTouchEventsForMouse', { enabled: touch, configuration: touch ? 'mobile' : 'desktop' });
  };
  const tapXY = async (x, y) => {
    await cdp('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y, radiusX: 3, radiusY: 3, force: 1 }] });
    await sleep(40);
    await cdp('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  };
  const clickXY = async (x, y) => {
    await cdp('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y });
    await cdp('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
    await cdp('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
  };
  const key_ = async k => {
    await cdp('Input.dispatchKeyEvent', { type: 'keyDown', key: k, code: k, windowsVirtualKeyCode: k === 'Escape' ? 27 : 0 });
    await cdp('Input.dispatchKeyEvent', { type: 'keyUp', key: k, code: k, windowsVirtualKeyCode: k === 'Escape' ? 27 : 0 });
  };
  // where a finger lands on an element, after bringing it on screen; and what is really there
  const aim = sel => evalJs(`(() => { const e = document.querySelector(${JSON.stringify(sel)}); if (!e) return null;
    e.scrollIntoView({block: 'center', inline: 'nearest'});
    const r = e.getBoundingClientRect(); if (!r.width || !r.height) return null;
    const x = r.left + r.width / 2, y = r.top + r.height / 2, t = document.elementFromPoint(x, y);
    const who = t ? (t.id ? '#' + t.id : t.tagName.toLowerCase() + (typeof t.className === 'string' && t.className.trim() ? '.' + t.className.trim().split(/\\s+/).join('.') : '')) : 'nothing';
    return { x, y, self: !!t && (t === e || e.contains(t)), who }; })()`);
  const state = () => evalJs(`JSON.stringify({ view: document.body.dataset.view, hash: location.hash,
    open: document.body.classList.contains('railopen'), current: (document.querySelector('.rail .nav[aria-current="page"]') || {}).dataset?.view || null,
    shown: [...document.querySelectorAll('.view.active')].map(s => s.id) })`).then(JSON.parse);

  // Every visible control whose centre is on screen must be the element there. Exempt: a
  // control under the sticky head (it scrolled there), one inside a closed <details> or
  // clipped by an ancestor that hides its overflow (not shown at all), and, while the
  // drawer is open, anything but the drawer (the veil is meant to cover the page).
  const AUDIT = `(() => {
    const vw = innerWidth, vh = innerHeight, head = document.getElementById('spine').getBoundingClientRect().bottom;
    const drawer = document.body.classList.contains('railopen');
    const vis = el => { const cs = getComputedStyle(el); if (cs.display === 'none' || cs.visibility === 'hidden' || +cs.opacity === 0) return false;
      const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const name = t => t ? (t.id ? '#' + t.id : t.tagName.toLowerCase() + (typeof t.className === 'string' && t.className.trim() ? '.' + t.className.trim().split(/\\s+/).slice(0, 2).join('.') : '')) : 'nothing';
    const out = [];
    for (const el of document.querySelectorAll('button, a[href], select, input:not([type=hidden]), textarea, summary, [role=button]')) {
      if (!vis(el) || el.closest('[hidden]')) continue;
      if (drawer && !el.closest('.rail')) continue;
      const shut = el.closest('details:not([open])'); if (shut && !(el.tagName === 'SUMMARY' && el.parentElement === shut)) continue;
      const r = el.getBoundingClientRect(), x = r.left + r.width / 2, y = r.top + r.height / 2;
      if (x < 1 || y < 1 || x > vw - 1 || y > vh - 1) continue;
      if (y < head && !el.closest('#spine') && !document.body.classList.contains('agentmax')) continue;
      let clipped = false;
      for (let a = el.parentElement; a && a !== document.body; a = a.parentElement) {
        const cs = getComputedStyle(a);
        if (/(hidden|auto|scroll|clip)/.test(cs.overflowX + cs.overflowY)) {
          const ar = a.getBoundingClientRect(); if (x < ar.left || x > ar.right || y < ar.top || y > ar.bottom) { clipped = true; break; }
        }
      }
      if (clipped) continue;
      const t = document.elementFromPoint(x, y);
      if (!t || t === el || el.contains(t)) continue;
      const lab = el.closest('label'); if (lab && lab.contains(t)) continue;
      if (el.labels && [...el.labels].some(l => l.contains(t))) continue;
      out.push(name(el) + ' <- ' + name(t));
    }
    return out; })()`;
  const auditView = async () => {
    const found = new Set();
    const h = await evalJs('document.documentElement.scrollHeight'), vh = await evalJs('innerHeight');
    for (let y = 0; y < h; y += Math.max(200, Math.round(vh * 0.7))) {
      await evalJs(`window.scrollTo(0, ${y})`); await sleep(60);
      for (const f of await evalJs(AUDIT)) found.add(f);
    }
    await evalJs('window.scrollTo(0, 0)');
    return [...found];
  };

  await device(PHONES[0], true);
  await cdp('Page.navigate', { url: BASE + '/#now' });
  ok('the page loads and renders', await waitFor("typeof showView === 'function' && document.body.dataset.view === 'now'", 20000));
  await waitFor("!document.getElementById('serving-name').textContent.startsWith('Reading')", 15000);

  // ── phones and a tablet: the drawer, under a finger ─────────────────────────────
  for (const d of PHONES) {
    await device(d, true);
    await evalJs("(setRailOpen(false), showView('now'), 1)"); await sleep(300);
    const hasDrawer = await evalJs("getComputedStyle(document.getElementById('menubtn')).display !== 'none'");
    ok(`${d.name}: the drawer is how this width navigates`, hasDrawer);
    if (!hasDrawer) continue;
    const m = await aim('#menubtn');
    ok(`${d.name}: the menu button is the element under a finger`, m && m.self, m && m.who);
    await tapXY(m.x, m.y);
    ok(`${d.name}: a tap opens the drawer`, await waitFor("document.body.classList.contains('railopen')", 3000));
    await sleep(350);   // the drawer's slide-in, so the items are where a finger lands
    for (const v of VIEWS) {
      if (!(await state()).open) { const mb = await aim('#menubtn'); await tapXY(mb.x, mb.y); await waitFor("document.body.classList.contains('railopen')", 3000); await sleep(350); }
      const a = await aim(`.rail .nav[data-view="${v}"]`);
      ok(`${d.name}: the ${v} item is the element under a finger`, a && a.self, a && a.who);
      if (!a) continue;
      await tapXY(a.x, a.y);
      await waitFor(`document.body.dataset.view === '${v}' && !document.body.classList.contains('railopen')`, 3000);
      const s = await state();
      ok(`${d.name}: a tap on ${v} shows ${v}, marks it current and closes the drawer`,
        s.view === v && s.hash === '#' + v && s.current === v && !s.open && s.shown.length === 1 && s.shown[0] === 'view-' + v, JSON.stringify(s));
    }
    // the veil: a tap beside the drawer closes it, and nothing else moves
    await evalJs("(showView('lanes'), 1)"); await sleep(200);
    const mb = await aim('#menubtn'); await tapXY(mb.x, mb.y); await sleep(450);
    const railRight = await evalJs("document.getElementById('rail').getBoundingClientRect().right");
    await tapXY(Math.min(d.w - 8, railRight + (d.w - railRight) / 2), d.h * 0.6); await sleep(450);
    const sv = await state();
    ok(`${d.name}: a tap on the veil closes the drawer and keeps the view`, !sv.open && sv.view === 'lanes', JSON.stringify(sv));
    // the keyboard: Escape closes it and the focus returns to the menu button
    await tapXY(mb.x, mb.y); await sleep(450);
    await key_('Escape'); await sleep(200);
    const esc = await evalJs("JSON.stringify({open: document.body.classList.contains('railopen'), focus: document.activeElement && document.activeElement.id})").then(JSON.parse);
    ok(`${d.name}: Escape closes the drawer and gives the focus back`, !esc.open && esc.focus === 'menubtn', JSON.stringify(esc));
  }

  // ── every view, every device: nothing covers a control ─────────────────────────
  for (const d of [...PHONES, DESKTOP]) {
    const touch = d !== DESKTOP;
    await device(d, touch);
    await evalJs("(setRailOpen(false), 1)");
    for (const v of VIEWS) {
      await evalJs(`(showView('${v}'), 1)`); await sleep(350);
      const covered = await auditView();
      ok(`${d.name} / ${v}: no control is covered where a finger lands`, covered.length === 0, covered.slice(0, 6).join(' | '));
    }
    // the fullscreen Agent frame (forced: this cockpit has no relay)
    await evalJs("(showView('agent'), setAgentMax(true, false), 1)"); await sleep(300);
    const top = await evalJs("(() => { const t = document.elementFromPoint(innerWidth / 2, 3); return !!t && !!t.closest('#ag-frame'); })()");
    ok(`${d.name}: the fullscreen frame covers the head`, top);
    const ex = await aim('#ag-exit');
    ok(`${d.name}: the fullscreen exit button is the element under a finger`, ex && ex.self, ex && ex.who);
    if (ex) { if (touch) await tapXY(ex.x, ex.y); else await clickXY(ex.x, ex.y); await sleep(300); }
    ok(`${d.name}: the exit button leaves fullscreen`, !(await evalJs("document.body.classList.contains('agentmax')")));
  }

  // ── the desktop rail, under a mouse ─────────────────────────────────────────────
  await device(DESKTOP, false);
  await evalJs("(setRailMin(false), showView('now'), 1)"); await sleep(200);
  for (const v of VIEWS) {
    const a = await aim(`.rail .nav[data-view="${v}"]`);
    if (a) { await clickXY(a.x, a.y); await sleep(250); }
    const s = await state();
    ok(`${DESKTOP.name}: a click on ${v} shows ${v}`, a && a.self && s.view === v && s.current === v, a ? a.who + ' ' + JSON.stringify(s) : 'not rendered');
  }
  const rb = await aim('#railbtn');
  if (rb) { await clickXY(rb.x, rb.y); await sleep(250); }
  ok(`${DESKTOP.name}: the collapse button collapses the rail`, await evalJs("document.body.classList.contains('railmin')"));
  // Collapsed, the footer is a box the width of an icon: its button showed the word "Expand"
  // spilling past the box and the rail's edge, off centre (seen on the reference box,
  // 2026-10-01). Everything visible in it stays inside, and the button's content is centred.
  const foot = await evalJs(`(() => { const f = document.querySelector('.rail .foot').getBoundingClientRect();
    const shown = [...document.querySelectorAll('.rail .foot *')].map(e => ({e, r: e.getBoundingClientRect(), cs: getComputedStyle(e)}))
      .filter(({r, cs}) => r.width > 1 && r.height > 1 && cs.visibility !== 'hidden' && cs.display !== 'none');
    const out = shown.filter(({r}) => r.left < f.left - 0.5 || r.right > f.right + 0.5).map(({e}) => e.tagName + (e.textContent ? ':' + e.textContent.trim() : ''));
    const inner = shown.filter(({e}) => e.parentElement && e.parentElement.id === 'railbtn').map(({r}) => r);
    const l = Math.min(...inner.map(r => r.left)), r = Math.max(...inner.map(r => r.right));
    return JSON.stringify({ out, off: Math.round(Math.abs((l + r) / 2 - (f.left + f.right) / 2) * 10) / 10, footW: Math.round(f.width),
      title: document.getElementById('railbtn').title }); })()`).then(JSON.parse);
  ok(`${DESKTOP.name}: collapsed, nothing in the footer spills out of its box`, foot.out.length === 0, JSON.stringify(foot));
  ok(`${DESKTOP.name}: collapsed, the expand button is centred in its box`, foot.off <= 1.5, JSON.stringify(foot));
  ok(`${DESKTOP.name}: collapsed, the expand button still says what it does`, foot.title === 'Expand', JSON.stringify(foot));
  if (rb) { const r2 = await aim('#railbtn'); if (r2) await clickXY(r2.x, r2.y); await sleep(200); }
  await evalJs("(setRailMin(false), 1)");

  // ── phone tables: the request log and the Library ───────────────────────────────
  const phone = PHONES[1];
  await device(phone, true);
  // Injected and measured in one evaluation: the page's own stream delivers this cockpit's
  // real (empty) log every few seconds, and nothing can run between the two here.
  const feed = await evalJs(`(() => { const now = new Date(), p = n => String(n).padStart(2, '0');
    const ts = s => now.getFullYear() + '-' + p(now.getMonth() + 1) + '-' + p(now.getDate()) + 'T' + p(now.getHours()) + ':' + p(now.getMinutes()) + ':' + p(s);
    const rows = [
      {ts: ts(1), peer: '100.78.198.77:51555', path: '/v1/chat/completions', bytes: 184320, secs: 42.7, kind: 'ok', outcome: '200 ok, 13,912 tokens out in 42.7 s', detail: ''},
      {ts: ts(2), peer: '127.0.0.1:41314', path: '/v1/systemone', bytes: 40, secs: 0.0, kind: 'fail', outcome: '422 systemone refused', detail: 'the probe that tells the Decide view the route is served'},
      {ts: ts(3), peer: '100.78.198.77:51529', path: '/v1/messages?beta=true', bytes: 2048, secs: 3.1, kind: 'gone', outcome: 'client left after 3.1 s, generation aborted', detail: ''}];
    showView('traffic');
    apply({feed: {data: {rows}, ts: Date.now() / 1000}});
    const vw = innerWidth, trs = [...document.querySelectorAll('#feed tbody tr')].filter(tr => tr.cells.length === 6);
    return JSON.stringify({ n: trs.length, overflow: document.documentElement.scrollWidth - vw,
      outOfScreen: trs.map(tr => tr.cells[5]).filter(td => { const r = td.getBoundingClientRect(); return r.left < 0 || r.right > vw + 1; }).length,
      tableW: Math.round(document.getElementById('feed').getBoundingClientRect().width) }); })()`).then(JSON.parse);
  ok('phone: the request log renders its rows', feed.n === 3, JSON.stringify(feed));
  ok('phone: the request log fits the screen', feed.overflow <= 1 && feed.tableW <= phone.w, JSON.stringify(feed));
  ok("phone: each request's outcome is on screen", feed.outOfScreen === 0, JSON.stringify(feed));
  await evalJs("(showView('library'), 1)");
  const loaded = await waitFor("document.querySelectorAll('#rcp-table tbody tr').length > 0 && !document.querySelector('#rcp-table tbody tr.empty')", 15000);
  ok('phone: the recipes load in this cockpit', loaded);
  if (loaded) {
    const rc = await evalJs(`(() => { const vw = innerWidth, t = document.getElementById('rcp-table');
      const cells = [...t.querySelectorAll('tbody td')];
      return JSON.stringify({ overflow: document.documentElement.scrollWidth - vw, tableW: Math.round(t.getBoundingClientRect().width),
        unlabelled: cells.filter((c, i) => c.cellIndex > 0 && c.colSpan === 1 && !c.dataset.label).length,
        head: getComputedStyle(t.tHead).display }); })()`).then(JSON.parse);
    ok('phone: the recipes stack into cards that fit the screen', rc.overflow <= 1 && rc.tableW <= phone.w && rc.head === 'none', JSON.stringify(rc));
    ok('phone: every stacked line carries its column name', rc.unlabelled === 0, JSON.stringify(rc));
  }

  // ── an iPhone's safe areas: the notch in landscape, the home indicator at the foot ──
  const inset = await cdp('Emulation.setSafeAreaInsetsOverride', { insets: { top: 0, left: 59, right: 59, bottom: 21 } });
  ok('this browser can emulate safe-area insets', !inset.error, JSON.stringify(inset.error || ''));
  if (!inset.error) {
    await device(PHONES[3], true);
    await evalJs("(setAgentMax(false, false), showView('now'), toast('a toast to measure', 'ok', 60000), 1)"); await sleep(400);
    const sa = await evalJs(`(() => { const vw = innerWidth, vh = innerHeight;
      const ctrls = [...document.querySelectorAll('#spine button, #spine a')].filter(e => { const r = e.getBoundingClientRect(); return r.width && r.height && getComputedStyle(e).display !== 'none'; });
      const outside = ctrls.filter(e => { const r = e.getBoundingClientRect(); return r.left < 59 - 0.5 || r.right > vw - 59 + 0.5; }).map(e => e.id || e.className);
      const t = document.querySelector('#toasts .toast'), tr = t ? t.getBoundingClientRect() : null;
      return JSON.stringify({ n: ctrls.length, outside, toastBottom: tr ? Math.round(vh - tr.bottom) : null }); })()`).then(JSON.parse);
    ok('landscape iPhone: every control of the head keeps clear of the notch', sa.n > 0 && sa.outside.length === 0, JSON.stringify(sa));
    ok('landscape iPhone: a toast keeps clear of the home indicator', sa.toastBottom !== null && sa.toastBottom >= 21, JSON.stringify(sa));
    await evalJs("(showView('agent'), setAgentMax(true, false), 1)"); await sleep(300);
    const fr = await evalJs(`(() => { const f = document.getElementById('ag-frame'), cs = getComputedStyle(f), r = f.getBoundingClientRect();
      const ex = document.getElementById('ag-exit').getBoundingClientRect();
      return JSON.stringify({ l: cs.paddingLeft, r: cs.paddingRight, b: cs.paddingBottom, full: Math.round(r.width) === innerWidth, exitLeft: Math.round(ex.left), exitRight: Math.round(innerWidth - ex.right) }); })()`).then(JSON.parse);
    ok('landscape iPhone: the fullscreen frame covers the screen and keeps opencode inside the safe area',
      fr.full && fr.l === '59px' && fr.r === '59px' && fr.b === '21px', JSON.stringify(fr));
    await evalJs("(setAgentMax(false, false), 1)");
    await cdp('Emulation.setSafeAreaInsetsOverride', { insets: { top: 0, left: 0, right: 0, bottom: 34 } });
    await device(PHONES[1], true);
    await evalJs("(showView('now'), 1)"); await sleep(300);
    const pb = await evalJs("parseFloat(getComputedStyle(document.getElementById('stage')).paddingBottom)");
    ok('portrait iPhone: the page ends above the home indicator', pb >= 34 + 56, `stage padding-bottom ${pb}px`);
    await cdp('Emulation.setSafeAreaInsetsOverride', { insets: { top: 0, left: 0, right: 0, bottom: 0 } });
  }

  ok('no page exception under any of it', jsErrors().length === 0, jsErrors().slice(0, 3).join(' | '));
} finally {
  const browser = gone(chrome);
  try { if (chrome) chrome.kill('SIGTERM'); } catch { /* gone */ }
  await browser;
  const cockpit = gone(server);
  try { if (server) process.kill(-server.pid, 'SIGTERM'); } catch { try { server.kill('SIGTERM'); } catch { /* gone */ } }
  await cockpit;
}

const failed = checks.filter(c => !c.ok);
for (const c of checks) if (!c.ok || process.env.TOUCH_VERBOSE === '1') console.log(`  ${c.ok ? 'ok  ' : 'FAIL'} ${c.name}${c.ok || !c.detail ? '' : '  <- ' + c.detail}`);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length ? 1 : 0);
