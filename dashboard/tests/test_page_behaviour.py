#!/usr/bin/env python3
"""What the cockpit page does, run: its scripts in node, on a DOM built from the page's
own markup (pagejs.py, fakedom.js).

Each class is one defect found in a review of the page (v1.18.6 on 2026-09-24, the video
lane in September 2026), reproduced before it was fixed, and carried over to the page as
it was rebuilt: the page said things that were not true, kept doing work nobody asked
for, or lost a control under a person's finger. State reaches the page the way the
server's event stream delivers it: one payload through apply().
"""
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pagejs  # noqa: E402

# Shared by the bodies below: a state payload the way the server wraps it, and text reads.
HELPERS = r"""
const wrap = o => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, {data: v, ts: Date.now() / 1000}]));
const feed = o => apply(wrap(o));
const txt = id => ($(id) ? $(id).textContent : null);
const CONFIG = {version: '1.1.2', dry_run: false, usable_frac: 0.92, periods: {}};
const UNITS = {units: {'qwen38-sglang.service': {active: 'active', enabled: 'enabled'},
                       'qwen38-flash.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-image.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-video.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-keepalive.service': {active: 'active', enabled: 'enabled'}}};
const life = (engines, extra) => Object.assign({engines, events: [], blocked: {}}, extra || {});
const eng = (state, extra) => Object.assign({state, target: 'stock', elapsed: 600}, extra || {});
const toastText = () => txt('toasts');
"""
SCRIPTS = pagejs.SCRIPTS


def run(test, body, **kw):
    kw.setdefault("scripts", SCRIPTS)
    return pagejs.run(test, HELPERS + body, **kw)


def css_rules(css=None):
    """(selector, [enclosing @media conditions], declarations) for every rule of the page's
    stylesheet, comments dropped."""
    css = css if css is not None else (pagejs.STATIC / "css" / "cockpit.css").read_text()
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out, stack, buf, i = [], [], "", 0
    while i < len(css):
        ch = css[i]
        if ch == "{":
            head = buf.strip()
            buf = ""
            if head.startswith("@media") or head.startswith("@supports"):
                stack.append(head)
            elif head.startswith("@"):
                depth, j = 1, i + 1       # @keyframes, @font-face: skip the whole block
                while depth:
                    depth += {"{": 1, "}": -1}.get(css[j], 0)
                    j += 1
                i = j - 1
            else:
                end = css.index("}", i)
                out.append((head, list(stack), css[i + 1:end].strip()))
                i = end
        elif ch == "}":
            if stack:
                stack.pop()
            buf = ""
        else:
            buf += ch
        i += 1
    return out


class AWedgeSaysWhatHappensNext(unittest.TestCase):
    """The page said "the autoheal belt restarts it after its grace period" of every wedged
    engine, and the belt is off unless COCKPIT_AUTOHEAL=1: on a default install nothing
    restarts it, and waiting for it is the wrong move."""

    BODY = r"""
    feed({config: Object.assign({}, CONFIG, %s), units: UNITS.units ? UNITS : {}, lifecycle: life({'qwen38-sglang.service': eng('wedged')})});
    report({banner: txt('banners')});
    """

    def test_off_by_default_it_does_not_promise_a_restart(self):
        r = run(self, self.BODY % "{autoheal: false}")
        self.assertNotIn("belt restarts it", r["banner"])
        self.assertIn("Nothing restarts it by itself", r["banner"])
        self.assertIn("COCKPIT_AUTOHEAL=1", r["banner"])

    def test_armed_it_says_the_belt_restarts_it(self):
        r = run(self, self.BODY % "{autoheal: true, autoheal_grace_s: 120}")
        self.assertIn("autoheal belt restarts it", r["banner"])
        self.assertIn("2 min", r["banner"])


class TheProbeLineIsNotAStaleOk(unittest.TestCase):
    """With no text engine serving (stopped, or a diffusion lane up) the canary skips, and
    the page kept its last success on screen as "ok, 0.4 s (skipped this round: busy)"."""

    def probe(self, engines, canary):
        return run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life(%s), canary: %s});
        report({line: txt('px-probe')});
        """ % (engines, canary))

    def test_no_text_engine(self):
        r = self.probe("{'qwen38-sglang.service': eng('stopped')}",
                       "{skipped: true, why: 'no text engine is ready', last_ok: Date.now() / 1000 - 600, fails: 0, latency: 0.4}")
        self.assertFalse(r["line"].startswith("ok"), r)
        self.assertIn("no text engine", r["line"])

    def test_a_diffusion_lane_serving(self):
        for unit, target, word in (("qwen38-image.service", "image", "image"), ("qwen38-video.service", "video", "video")):
            with self.subTest(unit=unit):
                r = self.probe("{'qwen38-sglang.service': eng('stopped'), '%s': eng('ready', {target: '%s'})}" % (unit, target),
                               "{skipped: true, why: 'no text engine is ready', last_ok: Date.now() / 1000 - 600, fails: 0, latency: 0.4}")
                self.assertIn("no text engine", r["line"])
                self.assertIn(word, r["line"])
                self.assertNotIn("ok,", r["line"])

    def test_a_skip_names_its_own_reason(self):
        r = self.probe("{'qwen38-sglang.service': eng('ready')}",
                       "{skipped: true, why: 'a client was active in the last minute', last_ok: Date.now() / 1000 - 60, fails: 0, latency: 0.4}")
        self.assertTrue(r["line"].startswith("ok, 0.4 s"), r)
        self.assertIn("a client was active in the last minute", r["line"])
        self.assertNotIn("busy", r["line"])


class TheFitLineSaysTheFailingPair(unittest.TestCase):
    """The limits line said "too large for this pool" and showed prompt plus answer against
    the pool, whatever the reason: a flash limit over the proxy's 250,000 ceiling read
    "730,000 asked, 827,968 servable". The failing pair is the one the server names."""

    def fit(self, fit):
        return run(self, r"""
        feed({opencode: {enabled: true, real: {present: true, default: 'flashnext/qwen3.8-flash-next', limits: {}},
                         launcher: {present: true, ours: true, cap: 200000}, follows: true, why: 'follows', fit: %s}});
        report({line: txt('oc-fit')});
        """ % fit)

    def test_over_the_proxy_ceiling(self):
        r = self.fit("{ok: false, why: 'the prompt alone exceeds what the proxy relays', asked: 548000, limit: 250000,"
                     " worst: 730000, usable: 827968, prompt_cap: 250000, pool: 899965, window: 1048576,"
                     " context: 548000, output: 182000, served: 'qwen3.8-flash-next'}")
        self.assertIn("548,000 asked, 250,000 servable", r["line"])
        self.assertNotIn("827,968", r["line"])
        self.assertNotIn("pool", r["line"])

    def test_over_the_window(self):
        r = self.fit("{ok: false, why: \"the prompt at opencode's compaction point plus the answer exceeds the engine's window\","
                     " asked: 280863, limit: 262144, worst: 237000, usable: 519203, prompt_cap: 250000, pool: 564352,"
                     " window: 262144, context: 205000, output: 32000, served: 'qwen3.8-flash-next'}")
        self.assertIn("280,863 asked, 262,144 servable", r["line"])
        self.assertNotIn("pool", r["line"])

    def test_over_the_pool(self):
        r = self.fit("{ok: false, why: 'prompt plus answer exceeds the pool', asked: 900000, limit: 827968,"
                     " worst: 900000, usable: 827968, prompt_cap: 827968, pool: 899965, window: 1048576,"
                     " context: 700000, output: 200000, served: 'qwen3.8-27b'}")
        self.assertIn("900,000 asked, 827,968 servable", r["line"])


class TheCheckpointSelectorFollowsTheLaneAgain(unittest.TestCase):
    """Touching a target selector froze it for the life of the page: a choice cancelled in
    the confirmation, or one the lane has served since, left it on that choice while the
    lanes moved under it."""

    SERVE = r"""
    const serve = (unit, target) => feed({engine_info: {served_target: target, prompt_ceiling_tokens: 0,
                     info: {served_model_name: 'x', max_total_num_tokens: 800000, context_length: 1048576}},
                   lifecycle: life({[unit]: eng('ready', {target})})});
    feed({config: CONFIG, units: UNITS});
    """

    def test_a_cancelled_choice_gives_the_selector_back(self):
        r = run(self, self.SERVE + r"""
        serve('qwen38-sglang.service', 'stock');
        const c = LCARDS.get('qwen38-sglang.service'), sel = c.tsel;
        sel.value = 'fp8'; sel.dispatchEvent({type: 'change'});
        c.load.click();
        const asked = !$('scrim').hidden;
        $('sh-cancel').click();
        serve('qwen38-sglang.service', 'stock');
        report({asked, value: sel.value});
        """)
        self.assertTrue(r["asked"], "Load opens the journey")
        self.assertEqual(r["value"], "stock")

    def test_once_the_choice_is_served_it_follows_again(self):
        r = run(self, self.SERVE + r"""
        serve('qwen38-sglang.service', 'stock');
        const sel = LCARDS.get('qwen38-sglang.service').tsel;
        sel.value = 'fp8'; sel.dispatchEvent({type: 'change'});
        serve('qwen38-sglang.service', 'stock');
        const kept = sel.value;                       // chosen, not served yet: kept
        serve('qwen38-sglang.service', 'fp8');        // the switch, then a restart
        serve('qwen38-sglang.service', 'uncensored'); // later, another checkpoint
        report({kept, value: sel.value});
        """)
        self.assertEqual(r["kept"], "fp8")
        self.assertEqual(r["value"], "uncensored")


class ControlsSurviveARefresh(unittest.TestCase):
    """The keepalive stop/start button and the job history's log buttons were rebuilt on
    every state message, up to twice a second, so a click whose press and release straddled
    a refresh landed on a node that no longer existed (25 of 40 clicks registered)."""

    def test_the_proxy_button_is_the_same_node_and_acts_on_what_it_shows(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS}); const a = $('px-btn'); const shown1 = a.textContent;
        const stopped = JSON.parse(JSON.stringify(UNITS)); stopped.units['qwen38-keepalive.service'].active = 'inactive';
        feed({units: stopped}); const b = $('px-btn');
        b.click();
        report({same: a === b, before: shown1, after: b.textContent, argv: txt('sh-cmd'), title: txt('sh-title')});
        """)
        self.assertTrue(r["same"])
        self.assertIn("Stop", r["before"])
        self.assertIn("Start", r["after"])
        self.assertIn(" start ", r["argv"] + " ")
        self.assertNotIn(" stop ", r["argv"] + " ")

    def test_the_log_buttons_are_the_same_nodes(self):
        r = run(self, r"""
        const job = (id, status, elapsed) => ({id, action: 'smoke', params: {}, status, started: Date.now() / 1000 - 100, elapsed});
        feed({job: {current: job('b', 'running', 5), recent: [job('a', 'done', 3)]}});
        const a = [...document.querySelectorAll('#job-hist button')];
        feed({job: {current: job('b', 'running', 6), recent: [job('a', 'done', 3)]}});
        const b = [...document.querySelectorAll('#job-hist button')];
        const when = [...document.querySelectorAll('#job-hist .faint')][0].textContent;
        feed({job: {current: null, recent: [job('b', 'done', 7), job('a', 'done', 3)]}});
        const c = [...document.querySelectorAll('#job-hist button')];
        report({n: [a.length, b.length, c.length], same: a.every((x, i) => x === b[i]), when, kept: c.includes(a[1])});
        """)
        self.assertEqual(r["n"], [2, 2, 2])
        self.assertTrue(r["same"])
        self.assertTrue(r["when"].endswith("6 s"), r["when"])
        self.assertTrue(r["kept"], "an unchanged job keeps its row when another one finishes")


class FullscreenCanBeLeftWithoutStorage(unittest.TestCase):
    """On a phone the Agent view opens fullscreen unless a choice to leave it was stored.
    Without localStorage (private mode) the choice was never kept, and the next state tick,
    a second later, put the frame back over the page."""

    def test_the_exit_holds_for_the_page(self):
        r = run(self, r"""
        const agent = {enabled: true, relay: {listening: true, port: 30091, bind: '127.0.0.1'},
                       server: {healthy: true, version: '1.18.32'}, unit: {active: 'active', enabled: 'enabled'},
                       unit_installed: true, pinned: '1.18.32', auto: true, auto_live: true};
        feed({agent});
        const opened = document.body.classList.contains('agentmax');
        $('ag-exit').click();
        const left = !document.body.classList.contains('agentmax');
        feed({agent}); feed({agent});
        report({opened, left, after: document.body.classList.contains('agentmax')});
        """, opts={"noStorage": True, "media": {"(max-width:980px)": True}, "location": {"hash": "#agent"}})
        self.assertTrue(r["opened"], "a phone opens the Agent view fullscreen")
        self.assertTrue(r["left"])
        self.assertFalse(r["after"], "the frame came back over the page")


class TheTimelineAnnouncesOnlyWhatIsNew(unittest.TestCase):
    """The event lists are live regions, and every lifecycle tick emptied and refilled them:
    a screen reader read the whole list again twice a second. Rows now stay; a new event is
    one addition."""

    def test_rows_are_kept_and_a_new_event_is_one_new_node(self):
        r = run(self, r"""
        const ev = (ts, msg) => ({ts, kind: 'state', msg});
        const evs = [ev(100, 'a'), ev(200, 'b'), ev(300, 'c')];
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready')}, {events: evs})});
        const before = [...$('now-timeline').children];
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready')}, {events: evs})});
        const same = [...$('now-timeline').children];
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready')}, {events: evs.concat([ev(400, 'd')])})});
        const after = [...$('now-timeline').children];
        report({n: [before.length, same.length, after.length], unchanged: before.every((x, i) => x === same[i]),
                top: after[0].textContent, kept: before.every(x => after.includes(x)),
                order: after.map(x => x.textContent.slice(-1)).join('')});
        """)
        self.assertEqual(r["n"], [3, 3, 4])
        self.assertTrue(r["unchanged"], "an unchanged list is left alone")
        self.assertTrue(r["top"].endswith("d"))
        self.assertTrue(r["kept"], "the rows already announced stay")
        self.assertEqual(r["order"], "dcba")

    def test_now_keeps_its_last_eight_and_logs_keeps_forty(self):
        r = run(self, r"""
        const evs = Array.from({length: 50}, (_, i) => ({ts: 100 + i, kind: 'state', msg: 'm' + i}));
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready')}, {events: evs})});
        showView('logs');
        report({short: $('now-timeline').children.length, top: $('now-timeline').children[0].textContent,
                long: $('log-events').children.length});
        """)
        self.assertEqual(r["short"], 8)
        self.assertTrue(r["top"].endswith("m49"))
        self.assertEqual(r["long"], 40)

    def test_driver_refusals_in_a_row_are_one_row(self):
        r = run(self, r"""
        const k = (ts, n) => ({ts, kind: 'kernel', msg: `GPU driver refused ${n} allocation(s): memory edge during a prefill`});
        const evs = [{ts: 90, kind: 'state', msg: 'qwen38-video: starting → ready'}, k(100, 3), k(110, 20), k(120, 1),
                     {ts: 130, kind: 'state', msg: 'qwen38-video: ready → stopping'}];
        feed({lifecycle: life({'qwen38-video.service': eng('stopping', {target: 'video'})}, {events: evs})});
        report([...$('now-timeline').children].map(x => x.textContent));
        """)
        self.assertEqual(len(r), 3, r)
        self.assertIn("refused 24 allocations", r[1])
        self.assertIn("video: ready to stopping", r[0])


class TheImagePollStops(unittest.TestCase):
    """Once the Image view had seen someone else's generation it asked /api/image every 2 s
    for the life of the page (each answer runs a journalctl on the box), whichever view was
    open and whether or not the page was visible."""

    BODY = r"""
    let busy = true;
    __fetch = async url => url === '/api/image'
      ? __response(200, {port: 30020, host: '127.0.0.1', available: true, installed: true,
                         progress: busy ? {kind: 'stage', stage: 'denoising', label: 'denoising'} : {}})
      : new Promise(() => {});
    feed({config: CONFIG, units: UNITS,
          lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})})});
    showView('image'); await __settle();
    const count = () => __fetches.filter(f => f.url === '/api/image').length;
    """

    def test_it_stops_when_the_other_generation_ends(self):
        r = run(self, self.BODY + r"""
        await __advance(6000); const during = count();
        busy = false;
        await __advance(4000); const ended = count();
        await __advance(60000);
        report({during, ended, later: count()});
        """)
        self.assertGreaterEqual(r["during"], 3, "it follows the other generation while it runs")
        self.assertEqual(r["later"], r["ended"], "and asks nothing more once it is over")

    def test_it_does_not_run_behind_another_view(self):
        r = run(self, self.BODY + r"""
        await __advance(4000);
        showView('now'); await __settle(); const left = count();
        await __advance(30000);
        report({left, later: count()});
        """)
        self.assertEqual(r["later"], r["left"])

    def test_it_does_not_run_in_a_hidden_page(self):
        r = run(self, self.BODY + r"""
        await __advance(4000);
        document.hidden = true; const hid = count();
        await __advance(30000);
        report({hid, later: count()});
        """)
        self.assertEqual(r["later"], r["hid"])

    def test_its_own_request_is_followed_whatever_the_view(self):
        r = run(self, self.BODY + r"""
        busy = false; await __advance(3000);
        IS.inflight = Date.now(); imgWatch(true); showView('now');
        const start = count(); await __advance(6000);
        report({grew: count() - start});
        """)
        self.assertGreaterEqual(r["grew"], 3)


class TheVideoPollStops(unittest.TestCase):
    """The same rule for the video lane, whose answer also reads its journal: another
    person's video is followed on a visible Video view only, this page's own from anywhere."""

    BODY = r"""
    let busy = true;
    __fetch = async url => url === '/api/video'
      ? __response(200, {installed: true, port: 30022, state: 'ready', available: true,
                         progress: busy ? {id: 'v1', status: 'queued', progress: 0} : {},
                         run: busy ? {phase: 'denoise', step: 3, steps: 49, s_per_step: 14.8, left_s: 680, label: 'denoising'} : {}})
      : new Promise(() => {});
    feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-video.service': eng('ready', {target: 'video'})})});
    showView('video'); await __settle();
    const count = () => __fetches.filter(f => f.url === '/api/video').length;
    """

    def test_it_stops_when_the_video_ends_and_behind_another_view(self):
        r = run(self, self.BODY + r"""
        await __advance(12000); const during = count();
        showView('traffic'); await __settle(); const left = count();
        await __advance(9000); const away = count();
        showView('video'); await __settle(); busy = false; await __advance(6000); const ended = count();
        await __advance(60000);
        report({during, left, away, ended, later: count()});
        """)
        self.assertGreaterEqual(r["during"], 3)
        self.assertLessEqual(r["away"] - r["left"], 1, "at most the ten-second ask the spine makes while the lane serves")
        self.assertLessEqual(r["later"] - r["ended"], 6, "no five-second follow once it is over")


class ACrashIsNotACancel(unittest.TestCase):
    """A lane that died under a generation came back from the cockpit as interrupted, and
    the page said "Cancelled: the lane was stopped or restarted". The cockpit tells a crash
    from a stop; the page says which."""

    def test_the_image_page_says_the_lane_crashed(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/image/generate' ? __response(502, {crashed: true, error: 'the image lane crashed while this image was being made'})
          : url === '/api/image' ? __response(200, {available: true, installed: true, progress: {}}) : new Promise(() => {});
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})})});
        $('img-prompt').value = 'a cat'; imgSync();
        await imgRun();
        report(toastText());
        """)
        self.assertIn("crashed", r)
        self.assertNotIn("Cancelled", r)

    def test_the_video_page_says_the_lane_crashed(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/video/generate' ? __response(502, {crashed: true, error: 'the video lane crashed (systemd: signal)'})
          : url === '/api/video' ? __response(200, {installed: true, available: true, state: 'ready', progress: {}, run: {}}) : new Promise(() => {});
        feed({lifecycle: life({'qwen38-video.service': eng('ready', {target: 'video'})})});
        await vidLane(); $('vid-prompt').value = 'a fox'; vidSync();
        await vidRun();
        report(toastText());
        """)
        self.assertIn("crashed", r)
        self.assertNotIn("cancelled", r.lower())


class DecideFollowsTheLane(unittest.TestCase):
    """The System One view probed once per page load: after a switch, a stop or a diffusion
    lane taking the box it went on saying it was served, and a probe that once failed left
    Ask disabled for good."""

    def test_the_view_asks_again_when_the_lane_changes(self):
        r = run(self, r"""
        let answer = {available: false, lane: '', reason: 'no text lane is serving'};
        __fetch = async url => url === '/api/systemone' ? __response(200, answer) : new Promise(() => {});
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('stopped')})});
        showView('decide'); await __settle();
        const first = {cap: txt('s1-cap'), off: $('s1-run').disabled, note: txt('s1-note')};
        answer = {available: true, lane: 'qwen3.8-27b', reason: ''};
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready')})}); await __settle();
        report({first, cap: txt('s1-cap'), off: $('s1-run').disabled, noteHidden: $('s1-note').hidden,
                probes: __fetches.filter(f => f.url === '/api/systemone').length});
        """)
        self.assertIn("not served", r["first"]["cap"])
        self.assertTrue(r["first"]["off"])
        self.assertIn("No text lane", r["first"]["note"])
        self.assertIn("qwen3.8-27b", r["cap"])
        self.assertFalse(r["off"])
        self.assertTrue(r["noteHidden"], "the old refusal is gone")
        self.assertEqual(r["probes"], 2)

    def test_a_revisit_asks_again(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/systemone' ? __response(200, {available: true, lane: 'qwen3.8-27b'}) : new Promise(() => {});
        showView('decide'); await __settle(); showView('now'); showView('decide'); await __settle();
        report({probes: __fetches.filter(f => f.url === '/api/systemone').length});
        """)
        self.assertEqual(r["probes"], 2)


class TheLogsViewOffersWhatThePageSendsYouTo(unittest.TestCase):
    """Five places send a person to "its journal in Logs", and the view offered containers
    and the proxy only: no unit journal, so neither a text lane that failed at start (its
    container is gone) nor a diffusion lane (it has no container)."""

    def source(self, engines):
        return run(self, r"""
        __fetch = async () => __response(200, {lines: []});
        feed({units: UNITS, lifecycle: life(%s)});
        showView('logs'); await __settle();
        report($('log-src').value);
        """ % engines)

    def test_a_diffusion_lane_serving_opens_its_journal(self):
        self.assertEqual(self.source("{'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})}"),
                         "qwen38-image.service")
        self.assertEqual(self.source("{'qwen38-video.service': eng('ready', {target: 'video'})}"), "qwen38-video.service")

    def test_a_failed_text_lane_opens_its_journal(self):
        self.assertEqual(self.source("{'qwen38-sglang.service': eng('failed')}"), "qwen38-sglang.service")

    def test_a_serving_text_lane_still_opens_its_container(self):
        self.assertEqual(self.source("{'qwen38-flash.service': eng('ready', {target: 'flash'})}"), "qwen38-flash")

    def test_the_journal_button_of_a_lane_opens_that_journal(self):
        r = run(self, r"""
        __fetch = async () => __response(200, {lines: []});
        feed({units: UNITS, lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'}), 'qwen38-video.service': eng('stopped', {target: 'video'})})});
        LCARDS.get('qwen38-video.service').logs.click(); await __settle();
        report({view: activeView, src: $('log-src').value});
        """)
        self.assertEqual(r, {"view": "logs", "src": "qwen38-video.service"})


class TheSessionCardDoesNotHammerADeadRelay(unittest.TestCase):
    """agent-mobile.js, the script the relay injects into opencode's page on a phone,
    fetched the session list again the moment a fetch failed: with the relay unreachable
    that is a loop, one request per failure (2,021 in 3 s in Chromium)."""

    MARKUP = ('<html><head><meta name="theme-color" content="#fff"></head>'
              '<body><div id="root"><div>opencode</div></div></body></html>')
    REFUSED = "__fetch = () => new Promise((_, no) => setTimeout(() => no(new TypeError('Failed to fetch')), 5));"
    SLOW = "__fetch = () => new Promise(ok => setTimeout(() => ok(__response(200, [])), 4000));"

    def card(self, setup, body):
        return pagejs.run(self, body, setup=setup, scripts=("agent-mobile.js",), markup=self.MARKUP,
                          opts={"media": {"(pointer: coarse)": True}})

    def test_a_failing_fetch_is_retried_later_not_at_once(self):
        r = self.card(self.REFUSED, r"""
        await __advance(3000);
        const early = __fetches.length;
        await __advance(60000);
        report({early, minute: __fetches.length});
        """)
        self.assertLessEqual(r["early"], 2, r)
        self.assertLessEqual(r["minute"], 8, r)
        self.assertGreaterEqual(r["minute"], 2, "it does try again")

    def test_a_slow_answer_is_not_asked_again_meanwhile(self):
        r = self.card(self.SLOW, r"""
        await __advance(3000);
        report({n: __fetches.length});
        """)
        self.assertEqual(r["n"], 1)


class TheEditCommandCannotRunAFileName(unittest.TestCase):
    """The curl shown for an edit put each reference's file name inside double quotes, where
    a shell still expands $(...): a reference named x$(cmd).png ran cmd for whoever pasted
    the line into a terminal."""

    def curl_for(self, name):
        return run(self, r"""
        IS.mode = 'edit'; IS.refs = [{name: NAME, dataUrl: 'data:image/png;base64,AAAA', w: 1, h: 1}];
        imgCurl();
        report(txt('img-curl'));
        """.replace("NAME", json.dumps(name)))

    def pasted(self, cmd):
        """The command as a terminal runs it, with curl replaced by a printer of its argv."""
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run(["bash", "-c", "curl(){ printf '%s\\n' \"$@\"; }\n" + cmd],
                               cwd=d, capture_output=True, text=True, timeout=20)
            made = sorted(x.name for x in pathlib.Path(d).iterdir())
        return p, made

    def test_a_command_in_a_name_is_not_run(self):
        p, made = self.pasted(self.curl_for("x$(touch PWNED)`touch PWNED2`.png"))
        self.assertEqual(made, [], "the pasted command ran something")
        args = p.stdout.splitlines()
        self.assertEqual(args[args.index("-F") + 1], 'image[]=@"x$(touch PWNED)`touch PWNED2`.png";type=image/png')

    def test_quotes_and_semicolons_stay_in_the_name(self):
        p, made = self.pasted(self.curl_for("it's \"a\"; b.png"))
        self.assertEqual((p.returncode, made), (0, []), p.stderr)
        args = p.stdout.splitlines()
        self.assertEqual(args[args.index("-F") + 1], 'image[]=@"it\'s \\"a\\"; b.png";type=image/png')


class AnEditLeavesOutWhatTheEditsEndpointDropsUnread(unittest.TestCase):
    """The editing endpoint has no flow_shift field: the page sent the Shift box's value with
    an edit, and showed it in the edit's curl, and the lane dropped it unread."""

    def payload(self, mode):
        return run(self, r"""
        IS.mode = MODE; $('img-shift').value = '3.5';
        report({payload: imgPayload(), curl: (imgCurl(), txt('img-curl'))});
        """.replace("MODE", json.dumps(mode)))

    def test_a_generation_keeps_the_shift(self):
        r = self.payload("t2i")
        self.assertEqual(r["payload"].get("flow_shift"), 3.5)
        self.assertIn("flow_shift", r["curl"])

    def test_an_edit_leaves_it_out(self):
        r = self.payload("edit")
        self.assertNotIn("flow_shift", r["payload"])
        self.assertNotIn("flow_shift", r["curl"])


class TheCollapsedRailIsADesktopThing(unittest.TestCase):
    """A rail collapsed in a wide window stayed collapsed on a narrow one, where the rail is
    a drawer: 64 px of unlabelled icons, measured at 390 and 800 px."""

    def test_every_collapsed_rail_rule_is_scoped_above_980_px(self):
        rules = [(sel, media) for sel, media, _ in css_rules() if "body.railmin" in sel]
        self.assertTrue(rules, "the page has collapsed-rail rules")
        for sel, media in rules:
            with self.subTest(sel=sel):
                self.assertTrue(any("min-width:981px" in m.replace(" ", "") for m in media), media)



class TheCockpitsProbeWearsNoAlarmColour(unittest.TestCase):
    """The request log painted the cockpit's own route probe the red of a failure."""

    def test_a_probe_row_is_not_painted_as_a_failure(self):
        out = run(self, r"""
        feed({feed: {rows: [{ts: '2026-09-30T17:04:05', peer: '127.0.0.1:56836', path: '/v1/systemone', bytes: 40, secs: 0,
                             kind: 'probe', outcome: "422 as designed: the cockpit's probe"},
                            {ts: '2026-09-30T17:04:06', peer: '100.78.198.77:1', path: '/v1/chat/completions', bytes: 9, secs: 0,
                             kind: 'fail', outcome: '503 engine unreachable'}]}});
        report([...$('feed').tBodies[0].rows].map(tr => tr.cells[5].querySelector('.tag').className.trim()));
        """)
        self.assertEqual(sorted(out), ["tag", "tag err"])


if __name__ == "__main__":
    unittest.main()
