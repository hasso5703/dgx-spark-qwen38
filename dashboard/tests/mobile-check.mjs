// Phone-layout check of the cockpit: a headless Chromium emulates real iPhone
// viewports (device metrics, touch, the device pixel ratio) and measures what a
// phone actually gets on every tab. It reports, per device and per tab:
//
//   * horizontal overflow of the document (the phone tell: the page slides sideways)
//   * every element wider than the viewport that is NOT inside a scroller of its
//     own (a table that scrolls inside .tablewrap is intended; a card that pokes
//     out of the screen is not)
//   * form controls under 16px, because iOS Safari zooms the whole page when one
//     of those takes focus and never zooms back out
//   * touch targets under 44x44 CSS px, the size Apple's HIG asks for
//   * on the Agent tab, whether the embedded opencode frame fits the visual
//     viewport instead of pushing the page past it (100vh on iOS includes the
//     browser chrome, so calc(100vh - x) overflows by the toolbar's height)
//
//   node dashboard/tests/mobile-check.mjs [http://127.0.0.1:30090]
//
// Point it at the address the phone actually uses (the tailnet one, where the
// agent relay answers) to exercise the Agent tab the way it behaves in a hand:
// fullscreen, with opencode really loaded in the frame.
//
// Exit 0 when every device and tab passes. Any failure prints the offenders.
import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';

const BASE = process.argv[2] || process.env.COCKPIT_BASE || 'http://127.0.0.1:30090';
const KEYFILE = process.env.COCKPIT_KEY_FILE || `${process.env.HOME}/.config/qwen38/api-key`;
const TAP_MIN = 44;          // Apple HIG minimum touch target, CSS px
const FONT_MIN = 16;         // below this iOS Safari zooms on focus
const checks = [];
const ok = (name, cond, detail = '') => checks.push({ name, ok: !!cond, detail: String(detail).slice(0, 300) });
const sleep = ms => new Promise(r => setTimeout(r, ms));

// Real iPhone CSS viewports (portrait unless named landscape).
const DEVICES = [
  { name: 'iPhone SE (375x667)', w: 375, h: 667, dpr: 2 },
  { name: 'iPhone 15 (393x852)', w: 393, h: 852, dpr: 3 },
  { name: 'iPhone 15 Pro Max (430x932)', w: 430, h: 932, dpr: 3 },
  { name: 'iPhone 15 landscape (852x393)', w: 852, h: 393, dpr: 3 },
];
const TABS = ['overview', 'agent', 'engines', 'requests', 'machine', 'models', 'logs', 'setup'];

const key = readFileSync(KEYFILE, 'utf8').trim();
const login = await fetch(`${BASE}/api/login`, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ key }),
});
if (!login.ok) { console.error(`login failed: HTTP ${login.status}`); process.exit(2); }
const cookie = (login.headers.get('set-cookie') || '').match(/cockpit=([^;]+)/)?.[1];
if (!cookie) { console.error('no session cookie'); process.exit(2); }

const chrome = spawn('/snap/bin/chromium', ['--headless=new', '--remote-debugging-port=0', '--no-first-run',
  '--disable-gpu', '--hide-scrollbars', '--window-size=500,1000', 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
const wsUrl = await new Promise((res, rej) => {
  let buf = ''; chrome.stderr.on('data', d => { buf += d; const m = buf.match(/DevTools listening on (ws:\S+)/); if (m) res(m[1]); });
  setTimeout(() => rej(new Error('no devtools url: ' + buf.slice(-300))), 20000);
});
const ws = new WebSocket(wsUrl); await new Promise(r => { ws.onopen = r; });
let id = 0; const pending = new Map(); const events = [];
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } else if (m.method) events.push(m);
};
const send = (method, params = {}, sessionId) => new Promise(r => {
  const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params, sessionId }));
});
const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank' });
const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
await send('Network.enable', {}, sessionId);
await send('Runtime.enable', {}, sessionId);
await send('Page.enable', {}, sessionId);
await send('Network.setCookie', { name: 'cockpit', value: cookie, url: BASE, httpOnly: true, sameSite: 'Strict' }, sessionId);

async function evalJs(expression) {
  const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
  if (r.result?.exceptionDetails) throw new Error(r.result.exceptionDetails.exception?.description || 'eval threw');
  return r.result?.result?.value;
}

// The audit runs in the page: one pass over the DOM per tab.
const AUDIT = `(() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const sel = el => {
    let s = el.tagName.toLowerCase();
    if (el.id) return s + '#' + el.id;
    if (el.className && typeof el.className === 'string') s += '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.');
    return s;
  };
  const scrollableX = el => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const o = getComputedStyle(p).overflowX;
      if (o === 'auto' || o === 'scroll' || o === 'hidden') return true;
    }
    return false;
  };
  const visible = el => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const all = [...document.querySelectorAll('body *')].filter(visible);
  const wide = [];
  for (const el of all) {
    const r = el.getBoundingClientRect();
    if (r.width > vw + 1 || r.right > vw + 1 || r.left < -1) {
      if (!scrollableX(el)) wide.push({ sel: sel(el), w: Math.round(r.width), left: Math.round(r.left), right: Math.round(r.right) });
    }
  }
  // Only text entry zooms iOS: a checkbox has a font size and no keyboard.
  const TYPED = ['text', 'search', 'url', 'tel', 'email', 'password', 'number', 'date', 'time', ''];
  const smallFonts = [...document.querySelectorAll('input, select, textarea')].filter(visible)
    .filter(el => el.tagName !== 'INPUT' || TYPED.includes((el.getAttribute('type') || '').toLowerCase()))
    .map(el => ({ sel: sel(el), px: parseFloat(getComputedStyle(el).fontSize) }))
    .filter(x => x.px < ${FONT_MIN} - 0.01);
  // The target is what a finger can hit, so a control wrapped in a label inherits
  // that label's box; an inline link inside a sentence is text, not a control.
  const target = el => {
    const lab = el.closest('label');
    const r = (lab && lab !== el ? lab : el).getBoundingClientRect();
    return { w: Math.round(r.width), h: Math.round(r.height) };
  };
  const taps = [...document.querySelectorAll('button, a[href], select, input:not([type=hidden]), [role=button], [tabindex]')]
    .filter(visible)
    .filter(el => !(el.tagName === 'A' && getComputedStyle(el).display === 'inline'))
    .map(el => ({ sel: sel(el), ...target(el) }))
    .filter(x => x.w < ${TAP_MIN} || x.h < ${TAP_MIN});
  const frame = document.querySelector('.agentframe');
  const fr = frame ? frame.getBoundingClientRect() : null;
  const agentmax = document.body.classList.contains('agentmax');
  const agentnote = !document.getElementById('agnote')?.hidden;
  return JSON.stringify({
    vw, vh,
    docScrollW: document.documentElement.scrollWidth,
    bodyScrollW: document.body.scrollWidth,
    overflowX: document.documentElement.scrollWidth - vw,
    wide: wide.slice(0, 12), wideCount: wide.length,
    smallFonts: smallFonts.slice(0, 12), smallFontCount: smallFonts.length,
    taps: taps.slice(0, 12), tapCount: taps.length,
    frame: fr ? { top: Math.round(fr.top), h: Math.round(fr.height), bottom: Math.round(fr.bottom) } : null,
    agentmax, agentnote,
  });
})()`;

const report = [];
for (const d of DEVICES) {
  await send('Emulation.setDeviceMetricsOverride', {
    width: d.w, height: d.h, deviceScaleFactor: d.dpr, mobile: true,
  }, sessionId);
  await send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 }, sessionId);
  await send('Page.navigate', { url: BASE + '/#overview' }, sessionId);
  await sleep(4500);
  for (const tab of TABS) {
    await evalJs(`(() => { const b = document.querySelector('.rail .nav[data-tab="${tab}"]'); if (b) b.click(); else location.hash = '#${tab}'; })()`);
    await sleep(tab === 'agent' ? 2500 : 700);
    const a = JSON.parse(await evalJs(AUDIT));
    report.push({ device: d.name, tab, ...a });
    ok(`${d.name} ${tab}: no horizontal overflow`, a.overflowX <= 1, `document scrolls ${a.overflowX}px past ${a.vw}px`);
    ok(`${d.name} ${tab}: nothing pokes out of the screen`, a.wideCount === 0,
      a.wide.map(x => `${x.sel} w=${x.w} right=${x.right}`).join(' | '));
    ok(`${d.name} ${tab}: no control under ${FONT_MIN}px (iOS zoom on focus)`, a.smallFontCount === 0,
      a.smallFonts.map(x => `${x.sel} ${x.px}px`).join(' | '));
    ok(`${d.name} ${tab}: touch targets at least ${TAP_MIN}x${TAP_MIN}`, a.tapCount === 0,
      `${a.tapCount} small: ` + a.taps.map(x => `${x.sel} ${x.w}x${x.h}`).join(' | '));
    if (tab === 'agent') {
      const pct = a.frame ? `${a.frame.h}px of ${a.vh}px (${Math.round(100 * a.frame.h / a.vh)}%)` : 'no frame';
      if (a.agentnote) {
        // This cockpit was reached on an address the relay does not answer on, so
        // the page carries the URL to use instead. Covering that explanation with a
        // frame that cannot load is the failure here, not a short frame: what has
        // to hold is that the note is visible and the frame is still usable.
        ok(`${d.name} agent: the relay note is shown, and the frame stays usable`,
          a.frame && a.frame.h >= 190, `${pct}, note shown, fullscreen=${a.agentmax}`);
      } else {
        ok(`${d.name} agent: the opencode frame fits the viewport`,
          a.frame && a.frame.bottom <= a.vh + 2 && a.frame.h > 120,
          JSON.stringify(a.frame) + ` vh=${a.vh}`);
        // Fitting is not enough: a frame squeezed into a sliver under a tall head is
        // what this check exists to catch. A phone that can load the panel opens it
        // fullscreen, so the floor is the whole screen there.
        const floor = a.agentmax ? 0.98 : 0.55;
        ok(`${d.name} agent: the frame gets ${a.agentmax ? 'the whole screen' : 'most of the screen'}`,
          a.frame && a.frame.h >= a.vh * floor, `${pct}, fullscreen=${a.agentmax}`);
      }
    }
  }
}

try { ws.close(); } catch {}
chrome.kill('SIGTERM');

const pass = checks.filter(c => c.ok).length;
const fails = checks.filter(c => !c.ok);
for (const c of checks) if (!c.ok) console.log(`  ECHEC ${c.name}\n        ${c.detail}`);
if (process.env.MOBILE_VERBOSE === '1') console.log(JSON.stringify(report, null, 1));
console.log(`\n${pass}/${checks.length} checks passed on ${DEVICES.length} iPhone viewports x ${TABS.length} tabs`);
process.exit(fails.length ? 1 : 0);
