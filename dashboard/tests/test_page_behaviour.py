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



def z_index(sel, media_frag=None):
    """The z-index a selector declares, in the @media whose condition holds media_frag
    (None for a rule outside any @media), or None."""
    for s, media, decl in css_rules():
        if sel not in [x.strip() for x in s.split(",")]:
            continue
        cond = "".join(media).replace(" ", "")
        if (media_frag is None and not media) or (media_frag and media_frag.replace(" ", "") in cond):
            m = re.search(r"z-index:\s*(-?\d+)", decl)
            if m:
                return int(m.group(1))
    return None


class TheDrawerIsNotTrappedUnderItsVeil(unittest.TestCase):
    """On a phone no rail item could be tapped: .shell carried z-index:1, which made it a
    stacking context, so the drawer inside it (z-index 60) stacked as a whole at 1, under
    the veil its own opening draws (body::after, z-index 55). Every tap landed on the veil,
    whose click closes the drawer: the view never changed (2026-09-30). The same trap held
    the Agent's fullscreen frame under the head, over its own exit button. The browser
    check (touch-check.mjs) taps for real; these hold the mechanism in the stylesheet."""

    def test_the_shell_is_no_stacking_context(self):
        decl = [d for s, media, d in css_rules() if s.strip() == ".shell" and not media]
        self.assertTrue(decl, "the page has a .shell rule")
        self.assertNotRegex(decl[0], r"z-index", "a z-index here traps the drawer and the fullscreen frame")

    def test_no_view_keeps_a_filling_animation(self):
        """An animation still filling on opacity keeps the view a stacking context after it
        ends, which held the fullscreen frame under the head with .shell fixed."""
        for s, media, d in css_rules():
            if s.strip() == ".view":
                self.assertNotRegex(d, r"animation:[^;]*\b(both|forwards)\b", d)
                return
        self.fail("no .view rule")

    def test_on_a_phone_the_drawer_is_over_its_veil_and_both_over_the_dock(self):
        rail = z_index(".rail", "max-width:980px")
        veil = z_index("body.railopen::after", "max-width:980px")
        dock = z_index(".dock")
        self.assertIsNotNone(rail); self.assertIsNotNone(veil); self.assertIsNotNone(dock)
        self.assertGreater(rail, veil)
        self.assertGreater(veil, dock, "the dock is under the veil while the drawer is open")

    def test_the_fullscreen_frame_is_over_the_head_and_under_the_dialogs(self):
        frame = z_index("body.agentmax .agentframe")
        self.assertGreater(frame, z_index(".spine"))
        self.assertGreater(frame, z_index(".rail", "max-width:980px"))
        self.assertLess(frame, z_index(".scrim"))


class TheDrawerAndTheDialogsHandTheFocusBack(unittest.TestCase):
    """The drawer had no Escape and a closed dialog left the focus on the body: a keyboard
    started again from the top of the page every time."""

    def test_the_drawer_takes_the_focus_to_where_you_are_and_escape_gives_it_back(self):
        out = run(self, r"""
        showView('lanes');
        $('menubtn').click(); await __advance(10);
        const opened = document.body.classList.contains('railopen');
        const inside = document.activeElement && document.activeElement.dataset.view;
        document.dispatchEvent({type: 'keydown', key: 'Escape'});
        report({opened, inside, closed: !document.body.classList.contains('railopen'),
                back: document.activeElement === $('menubtn')});
        """)
        self.assertEqual(out, {"opened": True, "inside": "lanes", "closed": True, "back": True})

    def test_the_actions_menu_takes_the_focus_and_gives_it_back(self):
        out = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        $('actbtn').focus(); $('actbtn').click(); await __advance(10);
        const first = document.activeElement && document.activeElement.dataset.act;
        document.dispatchEvent({type: 'keydown', key: 'Escape'});
        report({first, hidden: $('menu').hidden, back: document.activeElement === $('actbtn')});
        """)
        self.assertEqual(out["hidden"], True)
        self.assertEqual(out["back"], True)
        self.assertTrue(out["first"], "the first action has the focus while the menu is open")


class ATableCellCarriesItsColumnName(unittest.TestCase):
    """On a phone the Library's wide tables stack each row into a card, and a line without
    its column's name is a value nobody can read. A cell spanning columns has no one name."""

    def test_each_cell_is_labelled_by_its_column_and_a_span_is_not(self):
        out = run(self, r"""
        const t = $('rcp-table'), tb = t.tBodies[0];
        const a = tb.insertRow(); ['r', 'img', 'ckpt', 'drafter', 'serve', 'disk', 'lane'].forEach(v => { a.insertCell().textContent = v; });
        const b = tb.insertRow(); b.insertCell().textContent = 'bad'; const w = b.insertCell(); w.colSpan = 6; w.textContent = 'invalid';
        labelCells(t);
        report({a: [...a.cells].map(c => c.dataset.label || null), b: [...b.cells].map(c => c.dataset.label || null)});
        """)
        self.assertEqual(out["a"], ["Recipe", "Engine image", "Checkpoint", "Drafter", "Serving", "On disk", "Against the lane"])
        self.assertEqual(out["b"], ["Recipe", None])


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


class AVideoNeverGoesBackAPhase(unittest.TestCase):
    """Once the decode was done, the lane's journal named no phase while the file was
    written, and the Video view read "Reading the prompt" for the last seconds of every
    run (the reference box, 2026-09-30)."""

    def test_after_the_steps_it_reads_decoding_not_reading_the_prompt(self):
        out = run(self, r"""
        VS.inflight = Date.now(); VS.seen = null;
        VS.run = {phase: 'denoise', step: 48, steps: 49, s_per_step: 12.8, left_s: 13}; vidDrawProgress();
        const during = txt('vid-prog-lab');
        VS.run = null; vidDrawProgress();
        const after = txt('vid-prog-lab');
        VS.inflight = Date.now(); VS.seen = null; VS.run = null; vidDrawProgress();
        report({during, after, fresh: txt('vid-prog-lab')});
        """)
        self.assertTrue(out["during"].startswith("Denoising"), out)
        self.assertEqual(out["after"], "Decoding video and sound")
        self.assertEqual(out["fresh"], "Reading the prompt", "a new request starts from the prompt")


class TheServingLanesPoolIsTheLiveOne(unittest.TestCase):
    """The rack and the Lanes view read the pool from the boots this cockpit watched: one
    restarted after the serving lane booted said "KV pool this boot: 400,384 tokens" while
    the engine reported 491,136 (2026-09-30)."""

    def test_the_serving_text_lane_shows_what_its_engine_reports(self):
        out = run(self, r"""
        const flash = eng('ready', {target: 'flash', pools: {n: 3, last: 400384, min: 400384, max: 575744, spread_pct: 30.5}, boots: [650, 660, 670]});
        const u27 = eng('stopped', {pools: {n: 2, last: 926495, min: 880417, max: 926495, spread_pct: 5}, boots: [460]});
        const units = {units: Object.assign({}, UNITS.units, {'qwen38-flash.service': {active: 'active', enabled: 'enabled'},
                                                          'qwen38-sglang.service': {active: 'inactive', enabled: 'disabled'}})};
        feed({config: CONFIG, units, engine_info: {info: {max_total_num_tokens: 491136}},
              lifecycle: life({'qwen38-flash.service': flash, 'qwen38-sglang.service': u27})});
        report({flash: lanePool('qwen38-flash.service', flash), u27: lanePool('qwen38-sglang.service', u27)});
        """)
        self.assertEqual(out, {"flash": 491136, "u27": 926495})

    def test_right_after_a_switch_the_other_lanes_pool_is_not_shown_as_this_boots(self):
        """The engine read refreshes every 30 s: just after a switch it still holds the
        previous lane's pool, and says which target it read."""
        out = run(self, r"""
        const flash = eng('ready', {target: 'flash', pools: {n: 1, last: 400384}, boots: [650]});
        const units = {units: Object.assign({}, UNITS.units, {'qwen38-flash.service': {active: 'active', enabled: 'enabled'},
                                                          'qwen38-sglang.service': {active: 'inactive', enabled: 'disabled'}})};
        feed({config: CONFIG, units, engine_info: {served_target: 'uncensored', info: {max_total_num_tokens: 926495}},
              lifecycle: life({'qwen38-flash.service': flash})});
        report(lanePool('qwen38-flash.service', flash));
        """)
        self.assertEqual(out, 400384)


class RunningIsWhatTheEngineRuns(unittest.TestCase):
    """The engine's load counts a queued request in num_reqs too (the /get_load shape the
    cockpit projects: running plus waiting, the waiting ones again in num_waiting_reqs). The
    page wrote num_reqs under "Running" beside "Waiting": eight long prompts on a lane that
    ran two and queued three read "5 Running, 3 Waiting" (found live, 2026-10-01)."""

    BODY = r"""
    feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('ready')}),
          engine_fast: {load: [{num_reqs: %d, num_waiting_reqs: %d, num_tokens: 1000, num_used_tokens: 1000}]}});
    report({run: txt('tr-run'), wait: txt('tr-wait'), cap: txt('tr-cap'), lede: txt('now-lede'),
            sub: txt('serving-sub'), act: txt('act-v'), actk: txt('act-k'), series: SERIES.req.slice(-1)[0]});
    """

    def test_running_and_waiting_add_up_to_what_the_engine_holds(self):
        r = run(self, self.BODY % (5, 3))
        self.assertEqual((r["run"], r["wait"]), ("2", "3"), r)
        self.assertEqual(r["cap"], "2 running, 3 waiting", r)
        self.assertIn("2 requests are running, 3 waiting", r["lede"])
        self.assertTrue(r["sub"].startswith("2 running, 3 waiting"), r)
        self.assertEqual((r["act"], r["actk"]), ("2", "running, 3 waiting"), r)
        self.assertEqual(r["series"], 2, r)

    def test_a_queue_with_nothing_running_says_so(self):
        """Every request queued, none admitted yet: the page said "3 running"."""
        r = run(self, self.BODY % (3, 3))
        self.assertEqual((r["run"], r["wait"]), ("0", "3"), r)
        self.assertEqual(r["cap"], "3 waiting", r)
        self.assertIn("No request is running, 3 waiting", r["lede"])
        self.assertEqual((r["act"], r["actk"]), ("0", "nothing running, 3 waiting"), r)

    def test_one_running_reads_singular_and_idle_reads_idle(self):
        r = run(self, self.BODY % (1, 0))
        self.assertEqual((r["run"], r["cap"]), ("1", "1 running"), r)
        self.assertIn("1 request is running.", r["lede"])
        r = run(self, self.BODY % (0, 0))
        self.assertEqual((r["run"], r["wait"], r["cap"]), ("0", "0", "idle"), r)
        self.assertIn("No request is running.", r["lede"])
        self.assertEqual((r["act"], r["actk"]), ("0", "nothing running"), r)



class TheStartGuardAndALostSchedulerAreSaidAsTheyAre(unittest.TestCase):
    """A lane the start guard held read "starting", one it refused "keeps crashing: it dies
    during startup", and a server left without its scheduler only "stopped answering",
    which says it may come back (issue #26: none of the three does by waiting)."""
    U = 'qwen38-sglang.service'
    HELD = 'what is left of an engine still holds 16 GiB of GPU memory: 1756018 sglang::stuck 16578 MiB'

    def page(self, engine, config="{}"):
        return run(self, r"""
        const units = {units: Object.assign({}, UNITS.units, {'qwen38-sglang.service': {active: 'activating', enabled: 'enabled'}})};
        feed({config: Object.assign({}, CONFIG, %s), units, lifecycle: life({'qwen38-sglang.service': %s})});
        report({banner: txt('banners'), card: laneCard('qwen38-sglang.service').state.textContent,
                boot: laneCard('qwen38-sglang.service').boot.textContent, bay: bay('qwen38-sglang.service').state.textContent});
        """ % (config, engine))

    def test_a_held_start_says_what_holds_it(self):
        r = self.page("eng('starting', {held: '%s', elapsed: 20})" % self.HELD)
        self.assertIn("waits for GPU memory.", r["banner"])
        self.assertIn("What is left of an engine still holds 16 GiB", r["banner"])
        self.assertIn("It starts by itself once that memory is back", r["banner"])
        self.assertNotIn("crashing", r["banner"])
        self.assertEqual((r["card"], r["bay"]), ("waiting for GPU memory", "waiting for GPU memory"))
        self.assertIn("Waiting for GPU memory: " + self.HELD, r["boot"])

    def test_a_refused_start_is_no_crash_loop(self):
        r = self.page("eng('failed', {restarting: true, restarts: 4, refused: 'the GPU driver holds 50.9 GiB that no process accounts for'})")
        self.assertIn("is not starting.", r["banner"])
        self.assertIn("The GPU driver holds 50.9 GiB that no process accounts for.", r["banner"])
        self.assertIn("systemd checks again every 15 s", r["banner"])
        self.assertNotIn("keeps crashing", r["banner"])
        self.assertEqual(r["card"], "not starting: GPU memory held")

    def test_a_crash_while_serving_is_no_crash_loop(self):
        r = self.page("eng('failed', {restarting: true, restarts: 1, crashed_serving: true})")
        self.assertIn("stopped while it was serving and is starting again.", r["banner"])
        self.assertIn("comes back by itself", r["banner"])
        self.assertNotIn("keeps crashing", r["banner"])
        self.assertEqual(r["card"], "restarting after a crash")

    CRASH = ("{id: 'cublas-internal-error', title: 'cuBLAS internal error in a GEMM', "
             "meaning: 'It can be the first report of a fault in another kernel.', "
             "action: 'Look for an Xid line in the kernel log at the same minute.', "
             "xid_said: ['Xid 31: a GPU memory page fault (MMU fault)'], evidence: ['https://example.org/26']}")

    def test_a_known_crash_cause_is_said_with_what_to_do(self):
        r = self.page("eng('failed', {restarting: true, restarts: 1, crashed_serving: true, crash: %s, crash_age: 30})" % self.CRASH)
        self.assertIn("stopped while it was serving and is starting again.", r["banner"])
        self.assertIn("stopped 30 s ago: cuBLAS internal error in a GEMM.", r["banner"])
        self.assertIn("Xid 31: a GPU memory page fault (MMU fault)", r["banner"])
        self.assertIn("It can be the first report of a fault in another kernel.", r["banner"])
        self.assertIn("What to do: Look for an Xid line in the kernel log at the same minute.", r["banner"])
        self.assertIn("Evidence: https://example.org/26", r["banner"])

    def test_no_cause_known_no_cause_banner(self):
        r = self.page("eng('failed', {restarting: true, restarts: 1, crashed_serving: true})")
        self.assertNotIn(" ago: ", r["banner"])

    def test_the_cause_stays_once_the_lane_is_back(self):
        r = self.page("eng('ready', {crash: %s, crash_age: 600})" % self.CRASH)
        self.assertIn("stopped 10 min 00 ago: cuBLAS internal error in a GEMM.", r["banner"])
        self.assertNotIn("is starting again", r["banner"])

    def test_a_real_crash_loop_still_says_so(self):
        r = self.page("eng('failed', {restarting: true, restarts: 4})")
        self.assertIn("keeps crashing.", r["banner"])
        self.assertNotIn("not starting", r["banner"])

    def test_a_server_without_its_scheduler_says_who_restarts_it(self):
        r = self.page("eng('degraded', {zombie: true, zombie_in: 90})", "{zombie_restart: true}")
        self.assertIn("lost its scheduler.", r["banner"])
        self.assertIn("would never come back by itself", r["banner"])
        self.assertIn("The cockpit restarts it in 1 min 30", r["banner"])
        self.assertNotIn("stopped answering", r["banner"])
        self.assertEqual(r["card"], "lost its scheduler")
        r = self.page("eng('degraded', {zombie: true, zombie_in: 0})", "{zombie_restart: true}")
        self.assertIn("The cockpit restarts it now.", r["banner"])
        r = self.page("eng('degraded', {zombie: true, zombie_in: 0})", "{zombie_restart: false}")
        self.assertIn("Restart it from Lanes", r["banner"])
        self.assertNotIn("The cockpit restarts it", r["banner"])

    def test_a_plain_degraded_engine_keeps_its_own_words(self):
        r = self.page("eng('degraded')")
        self.assertIn("stopped answering.", r["banner"])
        self.assertNotIn("scheduler", r["banner"])


class AnEmptyStateIsSaidNotPending(unittest.TestCase):
    """'…' is a value on its way. With no text engine serving, the Traffic counters read it
    for good; with nvidia-smi silent, so did the GPU vitals, beside a temperature called
    "cool", "0 processes" nobody had counted and "No process on the GPU" (found by the
    monkey check, 2026-10-02)."""

    def test_the_traffic_counters_say_there_is_no_engine(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-sglang.service': eng('stopped')}),
              engine_fast: {load: null}, decode: {lane: null}});
        report(['tr-run', 'tr-wait', 'tr-tok', 'tr-acc', 'tr-kv', 'tr-cap'].map(txt));
        """)
        self.assertEqual(r[:5], ["none"] * 5, r)
        self.assertEqual(r[5], "no text engine", r)

    def test_a_lane_whose_launch_file_names_no_checkpoint_says_unknown(self):
        r = run(self, r"""
        const cps = () => [...document.querySelectorAll('#view-lanes dt')].filter(d => d.textContent === 'Checkpoint')
                                                                    .map(d => d.nextSibling.textContent);
        feed({config: CONFIG, units: UNITS});
        showView('lanes');
        const before = cps();
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped', {target: 'uncensored'}),
                               'qwen38-flash.service': eng('ready', {target: 'flash', model: '/models/x/Qwen3.8-Flash-NVFP4'})})});
        report({before, after: cps()});
        """)
        self.assertTrue(r["before"] and all(c == "…" for c in r["before"]), r)
        self.assertIn("unknown", r["after"], r)
        self.assertIn("Qwen3.8-Flash-NVFP4", r["after"], r)

    def test_a_box_whose_systemctl_did_not_answer_lists_what_the_lifecycle_lists(self):
        """A CI runner: systemctl answers nothing and no diffusion unit file exists, so the
        lifecycle lists the two text lanes alone. Read as installed, systemd's '?' showed the
        image and video lanes too, their checkpoint "…" for good (GitHub Actions, 2026-10-02)."""
        r = run(self, r"""
        const silent = {units: Object.fromEntries(Object.keys(UNITS.units).map(u => [u, {active: '?', enabled: '?'}]))};
        feed({config: CONFIG, units: silent, lifecycle: life({'qwen38-sglang.service': eng('stopped'),
                                                             'qwen38-flash.service': eng('stopped', {target: 'flash'})})});
        showView('lanes');
        const cps = [...document.querySelectorAll('#view-lanes dt')].filter(d => d.textContent === 'Checkpoint')
                                                                  .map(d => d.nextSibling.textContent);
        report({cps, image: installed('qwen38-image.service'), video: installed('qwen38-video.service'),
                u27: installed('qwen38-sglang.service'), flash: installed('qwen38-flash.service')});
        """)
        self.assertNotIn("…", r["cps"], r)
        self.assertEqual(sorted(r["cps"]), ["not installed", "not installed", "unknown", "unknown"], r)
        self.assertEqual((r["image"], r["video"], r["u27"], r["flash"]), (False, False, True, True), r)

    def test_a_gpu_that_did_not_answer_reads_no_reading_and_nothing_made_up(self):
        r = run(self, r"""
        const v = id => [$(id).children[1].textContent, $(id).children[2].textContent];
        const procs = () => $('gpu-procs').tBodies[0].textContent;
        feed({config: CONFIG, units: UNITS, lifecycle: life({})});
        const before = v('vt-gpu');
        feed({gpu: {node_id: 'local', power_w: null, temp_c: null, procs: null},
              kernel: {node_id: 'local', nvrm_oom_1h: null, nvrm_last: null}});
        const silent = {gpu: v('vt-gpu'), temp: v('vt-temp'), nvrm: v('vt-nvrm'), procs: procs(),
                        cap: txt('gpu-cap'), lamp: $('gpu-cap').className, dk: txt('dk-nvrm')};
        feed({gpu: {node_id: 'local', power_w: 31.4, temp_c: 52, procs: []},
              kernel: {node_id: 'local', nvrm_oom_1h: 0, nvrm_last: null}});
        const read = {gpu: v('vt-gpu'), temp: v('vt-temp'), nvrm: v('vt-nvrm'), procs: procs(), cap: txt('gpu-cap')};
        report({before, silent, read});
        """)
        self.assertEqual(r["before"][0], "…", "before the first answer the value is on its way")
        self.assertEqual(r["silent"]["gpu"], ["n/a", ""], r)
        self.assertEqual(r["silent"]["temp"], ["n/a", ""], r)
        self.assertEqual(r["silent"]["nvrm"], ["n/a", ""], r)
        self.assertEqual(r["silent"]["procs"], "No reading: nvidia-smi did not answer.", r)
        self.assertEqual((r["silent"]["cap"], r["silent"]["lamp"]), ("n/a", "cap"), r)
        self.assertTrue(r["silent"]["dk"].startswith("n/a"), r)
        self.assertEqual(r["read"]["gpu"], ["31 W", "0 processes"], r)
        self.assertEqual(r["read"]["temp"], ["52 °C", "cool"], r)
        self.assertEqual(r["read"]["nvrm"], ["0", "none"], r)
        self.assertEqual((r["read"]["procs"], r["read"]["cap"]), ("No process on the GPU.", "52 °C"), r)


class TheBootLaneIsSaidOnlyWhenOneIs(unittest.TestCase):
    """With no lane enabled at boot, the spine said "27B starts at boot" (the page's default
    for which logs to open, said as a fact), and Load left out the step that makes the lane
    the boot one (found 2026-10-02)."""

    BODY = r"""
    const units = JSON.parse(JSON.stringify(UNITS));
    for (const u of ['qwen38-sglang.service', 'qwen38-flash.service', 'qwen38-image.service', 'qwen38-video.service'])
      if (%s !== null) units.units[u].enabled = %s;
    feed({config: CONFIG, units, lifecycle: life({'qwen38-sglang.service': eng('stopped', {target: 'uncensored'}),
                                                 'qwen38-flash.service': eng('stopped', {target: 'flash'})})});
    askJourney('uncensored');
    const steps = [...$('sh-journey').children].map(li => li.textContent);
    closeSheet();
    showView('lanes');
    const atBoot = [...document.querySelectorAll('#view-lanes dt')].filter(d => d.textContent === 'At boot').map(d => d.nextSibling.textContent);
    const notes = [...BAYS.values()].map(b => b.note.textContent);
    report({sub: txt('serving-sub'), facts: txt('act-facts'), steps, atBoot, notes});
    """

    def page(self, enabled):
        return run(self, self.BODY % (enabled, enabled))

    def test_no_lane_enabled_says_so_and_load_makes_the_lane_the_boot_one(self):
        r = self.page("'disabled'")
        self.assertEqual(r["sub"], "no lane starts at boot; load one from Lanes", r)
        self.assertTrue(r["atBoot"] and all(a == "manual start" for a in r["atBoot"]), r["atBoot"])
        self.assertEqual(r["notes"], ["manual start"] * 4, r["notes"])
        self.assertIn("Boot lanenone", r["facts"])
        self.assertEqual(len(r["steps"]), 2, r["steps"])
        self.assertIn("switch-model.sh uncensored", r["steps"][0])
        self.assertIn("systemctl start qwen38-sglang.service", r["steps"][1])

    def test_a_systemctl_that_did_not_answer_claims_nothing(self):
        r = self.page("'?'")
        self.assertEqual(r["sub"], "load a lane from Lanes", r)
        self.assertIn("Boot laneunknown", r["facts"])
        self.assertTrue(r["atBoot"] and all(a == "unknown" for a in r["atBoot"]), r["atBoot"])
        # the text lanes say nothing of the boot; the diffusion lanes, which this lifecycle
        # does not list, say how to install them
        self.assertEqual(r["notes"][:2], ["", ""], r["notes"])
        self.assertNotIn("manual start", r["notes"], r["notes"])

    def test_the_lane_enabled_at_boot_is_named_and_not_switched_to_again(self):
        r = self.page("null")
        self.assertEqual(r["sub"], "27B starts at boot; load a lane from Lanes", r)
        self.assertIn("Boot lane27B", r["facts"])
        self.assertEqual(len(r["steps"]), 1, r["steps"])
        self.assertIn("systemctl start qwen38-sglang.service", r["steps"][0])


class TheLogsSayTheBoxsTimeAndDimTheBoxsOwnPolling(unittest.TestCase):
    """The cockpit asks the engine its load every second, and those lines filled the whole
    engine log while only /health was dimmed; and the engine writes its times in UTC, which
    the server now rewrites in the box's own time and the page says so (2026-10-02)."""

    BODY = r"""
    let answer = null;
    __fetch = async url => url.startsWith('/api/logs/') ? __response(200, answer) : new Promise(() => {});
    feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'})})});
    const read = async a => { answer = a; await tailLog(); await __settle();
      return {cls: [...$('log-view').children].map(c => c.className || ''), note: txt('log-note'), hidden: $('log-note').hidden}; };
    const lines = ['[2026-10-02 18:37:16] INFO:     127.0.0.1:1 - "GET /v1/loads?include=core HTTP/1.1" 200 OK',
                   '[2026-10-02 18:37:17] INFO:     127.0.0.1:2 - "GET /server_info HTTP/1.1" 200 OK',
                   '[2026-10-02 18:37:18] INFO:     127.0.0.1:3 - "POST /v1/chat/completions HTTP/1.1" 200 OK'];
    report({converted: await read({name: 'qwen38-flash', tz: 'CEST', local_stamps: 3, lines}),
            native: await read({name: 'qwen38-flash', tz: 'CEST', local_stamps: 0, lines})});
    """

    def test_the_polling_is_dimmed_and_a_conversion_is_said(self):
        r = run(self, self.BODY)
        self.assertEqual(r["converted"]["cls"], ["l-dim", "l-dim", ""], r)
        self.assertFalse(r["converted"]["hidden"], r)
        self.assertIn("local time (CEST)", r["converted"]["note"])
        self.assertIn("UTC", r["converted"]["note"])
        self.assertTrue(r["native"]["hidden"], "no line was rewritten: nothing to say")


class TheTrafficBadgeCountsEveryRequestInFlight(unittest.TestCase):
    """The badge counts the feed's rows in flight, and the server now sends every request
    still in flight, however old, beside the 25 newest: eight agents read "8 running"
    beside a badge that fell to 2 (2026-10-02)."""

    def test_an_old_request_in_flight_is_in_the_badge_and_the_table(self):
        r = run(self, r"""
        const row = (ts, peer, outcome, kind, secs) => ({ts, peer, method: 'POST', path: '/v1/chat/completions', bytes: 1000,
                                                        outcome, kind, secs, detail: null});
        const rows = [row('2026-10-02T23:16:01', '127.0.0.1:49398', 'in flight', 'live', null)];
        for (let i = 0; i < 25; i++) rows.push(row('2026-10-02T23:40:' + String(i).padStart(2, '0'), '127.0.0.1:' + (41000 + i), 'ok', 'ok', 1.0));
        rows.push(row('2026-10-02T23:41:00', '127.0.0.1:42000', 'in flight', 'live', null));
        feed({config: CONFIG, units: UNITS, feed: {rows}});
        const live = [...$('feed').tBodies[0].children].filter(tr => tr.textContent.includes('in flight')).length;
        report({badge: txt('bdg-traffic'), live, rows: $('feed').tBodies[0].children.length});
        """)
        self.assertEqual(r["badge"], "2", r)
        self.assertEqual((r["live"], r["rows"]), (2, 27), r)


if __name__ == "__main__":
    unittest.main()
