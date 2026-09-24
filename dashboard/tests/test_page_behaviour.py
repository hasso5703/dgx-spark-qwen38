#!/usr/bin/env python3
"""What the cockpit page does, run: app.js and agent-mobile.js in node, on a DOM built from
the page's own markup (pagejs.py, fakedom.js).

Each class is one defect found in the review of v1.18.6 (2026-09-24), reproduced here
before it was fixed: the page said things that were not true, kept doing work nobody
asked for, or lost a control under a person's finger.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pagejs  # noqa: E402

# Shared by the bodies below: a state payload the way the server wraps it, and text reads.
HELPERS = r"""
const wrap = o => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, {data: v, ts: Date.now() / 1000}]));
const txt = id => ($(id) ? $(id).textContent : null);
const CONFIG = {version: '1.1.2', dry_run: false, usable_frac: 0.92, periods: {}};
const UNITS = {units: {'qwen38-sglang.service': {active: 'active', enabled: 'enabled'},
                       'qwen38-flash.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-image.service': {active: 'inactive', enabled: 'disabled'},
                       'qwen38-keepalive.service': {active: 'active', enabled: 'enabled'}}};
const life = (engines, extra) => Object.assign({engines, events: [], blocked: {}}, extra || {});
const eng = (state, extra) => Object.assign({state, target: 'stock', elapsed: 600}, extra || {});
"""


def run(test, body, **kw):
    return pagejs.run(test, HELPERS + body, **kw)


class AWedgeSaysWhatHappensNext(unittest.TestCase):
    """The page said "the autoheal belt restarts it after its grace period" of every wedged
    engine, and the belt is off unless COCKPIT_AUTOHEAL=1: on a default install nothing
    restarts it, and waiting for it is the wrong move."""

    BODY = r"""
    rConfig(Object.assign({}, CONFIG, %s)); rUnits(UNITS);
    rLifecycle(life({'qwen38-sglang.service': eng('wedged')}));
    banners({}, {});
    report({banner: txt('banners'), card: txt('ovlane')});
    """

    def test_off_by_default_it_does_not_promise_a_restart(self):
        r = run(self, self.BODY % "{autoheal: false}")
        for where in ("banner", "card"):
            with self.subTest(where=where):
                self.assertNotIn("restarts it", r[where])
                self.assertIn("COCKPIT_AUTOHEAL=1", r[where])

    def test_armed_it_says_the_belt_restarts_it(self):
        r = run(self, self.BODY % "{autoheal: true, autoheal_grace_s: 120}")
        for where in ("banner", "card"):
            with self.subTest(where=where):
                self.assertIn("autoheal belt restarts it", r[where])
                self.assertIn("2 min", r[where])


class TheProbeLineIsNotAStaleOk(unittest.TestCase):
    """With no text engine serving (stopped, or the image lane up) the canary skips, and the
    page kept its last success on screen as "ok, 0.4 s (skipped this round: engine busy)"."""

    def probe(self, engines, canary):
        return run(self, r"""
        rConfig(CONFIG); rUnits(UNITS); rLifecycle(life(%s));
        rCanary(%s);
        report({line: txt('canary'), tip: $('canary').title, again: txt('canary2')});
        """ % (engines, canary))

    def test_no_text_engine(self):
        r = self.probe("{'qwen38-sglang.service': eng('stopped')}",
                       "{skipped: true, why: 'no text engine is ready', last_ok: Date.now() / 1000 - 600, fails: 0, latency: 0.4}")
        self.assertFalse(r["line"].startswith("ok"), r)
        self.assertIn("no text engine", r["line"])
        self.assertEqual(r["line"], r["again"])

    def test_the_image_lane_serving(self):
        r = self.probe("{'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})}",
                       "{skipped: true, why: 'no text engine is ready', last_ok: Date.now() / 1000 - 600, fails: 0, latency: 0.4}")
        self.assertIn("no text engine", r["line"])
        self.assertNotIn("ok,", r["line"])

    def test_a_skip_names_its_own_reason(self):
        r = self.probe("{'qwen38-sglang.service': eng('ready')}",
                       "{skipped: true, why: 'a client was active in the last minute', last_ok: Date.now() / 1000 - 60, fails: 0, latency: 0.4}")
        self.assertTrue(r["line"].startswith("ok, 0.4 s"), r)
        self.assertIn("a client was active in the last minute", r["line"])
        self.assertNotIn("engine busy", r["line"])


class TheFitLineAgreesWithItsTooltip(unittest.TestCase):
    """The Setup tab's limits line said "too large for this pool" and its
    tooltip showed prompt plus answer against the pool, whatever the reason: a flash limit
    over the proxy's 250,000 ceiling read "730,000 asked, 827,968 servable" under a line
    saying it was too large. The failing pair is the one the server names."""

    def fit(self, fit):
        return run(self, r"""
        rOpencode({enabled: true, real: {present: true, default: 'flashnext/qwen3.8-flash-next', limits: {}},
                   launcher: {present: true, ours: true, cap: 200000}, follows: true, why: 'follows', fit: %s});
        report({line: txt('ocfit'), tip: $('ocfit').title});
        """ % fit)

    def test_over_the_proxy_ceiling(self):
        r = self.fit("{ok: false, why: 'the prompt alone exceeds what the proxy relays', asked: 548000, limit: 250000,"
                     " worst: 730000, usable: 827968, prompt_cap: 250000, pool: 899965, window: 1048576,"
                     " context: 548000, output: 182000, served: 'qwen3.8-flash-next'}")
        self.assertIn("548,000 asked, 250,000 servable", r["tip"])
        self.assertNotIn("827,968", r["tip"])
        self.assertNotIn("pool", r["line"])

    def test_over_the_window(self):
        r = self.fit("{ok: false, why: \"the prompt at opencode's compaction point plus the answer exceeds the engine's window\","
                     " asked: 280863, limit: 262144, worst: 237000, usable: 519203, prompt_cap: 250000, pool: 564352,"
                     " window: 262144, context: 205000, output: 32000, served: 'qwen3.8-flash-next'}")
        self.assertIn("280,863 asked, 262,144 servable", r["tip"])
        self.assertNotIn("pool", r["line"])

    def test_over_the_pool(self):
        r = self.fit("{ok: false, why: 'prompt plus answer exceeds the pool', asked: 900000, limit: 827968,"
                     " worst: 900000, usable: 827968, prompt_cap: 827968, pool: 899965, window: 1048576,"
                     " context: 700000, output: 200000, served: 'qwen3.8-27b'}")
        self.assertIn("900,000 asked, 827,968 servable", r["tip"])


class TheSelectorFollowsTheLaneAgain(unittest.TestCase):
    """Touching the target selector froze it for the life of the page: a choice cancelled
    in the confirmation, or one the lane has served since, left it on that choice while the
    lanes moved under it."""

    SERVE = r"""
    const serve = (unit, target) => {
      rEngineInfo({served_target: unit === 'qwen38-image.service' ? null : target, prompt_ceiling_tokens: 0,
                   info: {served_model_name: 'x', max_total_num_tokens: 800000, context_length: 1048576}});
      rLifecycle(life({[unit]: eng('ready', {target})}));
    };
    rConfig(CONFIG); rUnits(UNITS);
    """

    def test_a_cancelled_choice_gives_the_selector_back(self):
        r = run(self, self.SERVE + r"""
        serve('qwen38-sglang.service', 'stock');
        const sel = $('switchsel');
        sel.value = 'fp8'; sel.dispatchEvent({type: 'change'});
        document.querySelector('.actbar [data-act="switch"]').click();
        const asked = !$('modal').hidden;
        $('mcancel').click();
        serve('qwen38-sglang.service', 'stock');
        report({asked, value: sel.value});
        """)
        self.assertTrue(r["asked"], "the Switch button opens the confirmation")
        self.assertEqual(r["value"], "stock")

    def test_once_the_choice_is_served_it_follows_again(self):
        r = run(self, self.SERVE + r"""
        serve('qwen38-sglang.service', 'stock');
        const sel = $('switchsel');
        sel.value = 'fp8'; sel.dispatchEvent({type: 'change'});
        serve('qwen38-sglang.service', 'stock');
        const kept = sel.value;                       // chosen, not served yet: kept
        serve('qwen38-sglang.service', 'fp8');        // the switch, then a restart
        serve('qwen38-flash.service', 'flash');       // later, another lane
        report({kept, value: sel.value});
        """)
        self.assertEqual(r["kept"], "fp8")
        self.assertEqual(r["value"], "flash")


class ControlsSurviveARefresh(unittest.TestCase):
    """The keepalive stop/start button and the job history's log buttons were rebuilt on
    every state message, up to twice a second, so a click whose press and release straddled
    a refresh landed on a node that no longer existed (the review counted 25 of 40 clicks
    registered)."""

    def test_the_proxy_button_is_the_same_node(self):
        r = run(self, r"""
        rUnits(UNITS); const a = document.querySelector('#unitlist button');
        rUnits(UNITS); const b = document.querySelector('#unitlist button');
        const stopped = JSON.parse(JSON.stringify(UNITS)); stopped.units['qwen38-keepalive.service'].active = 'inactive';
        rUnits(stopped); const c = document.querySelector('#unitlist button');
        report({same: a === b, label: c.textContent, verb: c.dataset.verb, stillSame: a === c,
                rows: document.querySelectorAll('#unitlist .eng').length});
        """)
        self.assertTrue(r["same"])
        self.assertEqual((r["label"], r["verb"], r["rows"]), ("start", "start", 1))
        self.assertTrue(r["stillSame"], "a state change updates the button, it does not replace it")

    def test_the_proxy_button_acts_on_what_it_shows(self):
        r = run(self, r"""
        rConfig(CONFIG); rUnits(UNITS);
        const stopped = JSON.parse(JSON.stringify(UNITS)); stopped.units['qwen38-keepalive.service'].active = 'inactive';
        rUnits(stopped);
        document.querySelector('#unitlist button').click();
        report({title: txt('mtitle'), argv: txt('margv')});
        """)
        self.assertIn("start", r["argv"])
        self.assertNotIn("stop", r["argv"])

    def test_the_log_buttons_are_the_same_nodes(self):
        r = run(self, r"""
        const job = (id, status, elapsed) => ({id, action: 'smoke', params: {}, status, started: Date.now() / 1000 - 100, elapsed});
        rJob({current: job('b', 'running', 5), recent: [job('a', 'done', 3)]});
        const a = [...document.querySelectorAll('#jobhist button')];
        rJob({current: job('b', 'running', 6), recent: [job('a', 'done', 3)]});
        const b = [...document.querySelectorAll('#jobhist button')];
        const elapsed = document.querySelector('#jobhist .hist .m').textContent;
        rJob({current: null, recent: [job('b', 'done', 7), job('a', 'done', 3)]});
        const c = [...document.querySelectorAll('#jobhist button')];
        report({n: [a.length, b.length, c.length], same: a.every((x, i) => x === b[i]), elapsed,
                kept: c.includes(a[1]), status: document.querySelector('#jobhist .hist .chip').textContent});
        """)
        self.assertEqual(r["n"], [2, 2, 2])
        self.assertTrue(r["same"])
        self.assertTrue(r["elapsed"].endswith("6 s"), r["elapsed"])
        self.assertTrue(r["kept"], "an unchanged job keeps its row when another one finishes")
        self.assertEqual(r["status"], "done")


class FullscreenCanBeLeftWithoutStorage(unittest.TestCase):
    """On a phone the Agent tab opens fullscreen unless a choice to leave it was stored.
    Without localStorage (private mode, blocked site data) the choice was never kept, and
    the next state tick, a second later, put the frame back over the page."""

    def test_the_exit_holds_for_the_page(self):
        r = run(self, r"""
        const agent = {enabled: true, relay: {listening: true, port: 30091, bind: '127.0.0.1'},
                       server: {healthy: true, version: '1.18.32'}, unit: {active: 'active', enabled: 'enabled'},
                       unit_installed: true, pinned: '1.18.32', auto: true, auto_live: true};
        rAgent(agent);
        const opened = document.body.classList.contains('agentmax');
        $('agexit').click();
        const left = !document.body.classList.contains('agentmax');
        rAgent(agent); rAgent(agent);
        report({opened, left, after: document.body.classList.contains('agentmax')});
        """, opts={"noStorage": True, "media": {"(max-width:980px)": True}, "location": {"hash": "#agent"}})
        self.assertTrue(r["opened"], "a phone opens the Agent tab fullscreen")
        self.assertTrue(r["left"])
        self.assertFalse(r["after"], "the frame came back over the page")


class TheEventLogAnnouncesOnlyWhatIsNew(unittest.TestCase):
    """The event lists are live regions (role=log, aria-relevant=additions), and every
    lifecycle tick emptied and refilled them: a screen reader read the whole list again
    twice a second. Rows now stay; a new event is one addition."""

    def test_rows_are_kept_and_a_new_event_is_one_new_node(self):
        r = run(self, r"""
        const ev = (ts, msg) => ({ts, kind: 'state', msg});
        const evs = [ev(100, 'a'), ev(200, 'b'), ev(300, 'c')];
        rLifecycle(life({'qwen38-sglang.service': eng('ready')}, {events: evs}));
        const before = [...$('evtlist').children];
        rLifecycle(life({'qwen38-sglang.service': eng('ready')}, {events: evs}));
        const same = [...$('evtlist').children];
        rLifecycle(life({'qwen38-sglang.service': eng('ready')}, {events: evs.concat([ev(400, 'd')])}));
        const after = [...$('evtlist').children];
        const logs = [...$('evtlist2').children];
        report({n: [before.length, same.length, after.length], unchanged: before.every((x, i) => x === same[i]),
                top: after[0].textContent, kept: before.every(x => after.includes(x)), long: logs.length,
                order: after.map(x => x.textContent.slice(-1)).join('')});
        """)
        self.assertEqual(r["n"], [3, 3, 4])
        self.assertTrue(r["unchanged"], "an unchanged list is left alone")
        self.assertTrue(r["top"].endswith("d"))
        self.assertTrue(r["kept"], "the rows already announced stay")
        self.assertEqual(r["order"], "dcba")
        self.assertEqual(r["long"], 4)

    def test_the_overview_keeps_its_last_seven(self):
        r = run(self, r"""
        const evs = Array.from({length: 12}, (_, i) => ({ts: 100 + i, kind: 'state', msg: 'm' + i}));
        rLifecycle(life({'qwen38-sglang.service': eng('ready')}, {events: evs.slice(0, 10)}));
        rLifecycle(life({'qwen38-sglang.service': eng('ready')}, {events: evs}));
        report({short: $('evtlist').children.length, top: $('evtlist').children[0].textContent,
                long: $('evtlist2').children.length});
        """)
        self.assertEqual(r["short"], 7)
        self.assertTrue(r["top"].endswith("m11"))
        self.assertEqual(r["long"], 12)


class TheImagePollStops(unittest.TestCase):
    """Once the Image tab had seen someone else's generation it asked /api/image every 2 s
    for the life of the page (each answer runs a journalctl on the box), whichever tab was
    open and whether or not the page was visible."""

    BODY = r"""
    let busy = true;
    __fetch = async url => url === '/api/image'
      ? __response(200, {port: 30020, host: '127.0.0.1', available: true, installed: true,
                         progress: busy ? {kind: 'stage', stage: 'denoising', label: 'denoising'} : {}})
      : new Promise(() => {});
    rConfig(CONFIG); rUnits(UNITS);
    rLifecycle(life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})}));
    showTab('image'); await __settle();
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

    def test_it_does_not_run_behind_another_tab(self):
        r = run(self, self.BODY + r"""
        await __advance(4000);
        showTab('overview'); await __settle(); const left = count();
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

    def test_its_own_request_is_followed_whatever_the_tab(self):
        r = run(self, self.BODY + r"""
        busy = false; await __advance(3000);
        imgInflight = Date.now(); imgWatch(true, 1500); showTab('overview');
        const start = count(); await __advance(6000);
        report({grew: count() - start});
        """)
        self.assertGreaterEqual(r["grew"], 3)


class ACrashIsNotACancel(unittest.TestCase):
    """A lane that died under a generation came back from the cockpit as interrupted, and
    the page said "Cancelled: the lane was stopped or restarted". The cockpit now tells a
    crash from a stop; the page says which."""

    def test_the_page_says_the_lane_crashed(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/image/generate' ? __response(502, {crashed: true, error: 'the image lane crashed while this image was being made'})
          : url === '/api/image' ? __response(200, {available: true, installed: true, progress: {}}) : new Promise(() => {});
        rLifecycle(life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})}));
        $('imgprompt').value = 'a cat'; imgSync();
        await imgRun();
        report({out: txt('imgout'), chip: txt('imgtime')});
        """)
        self.assertIn("crashed", r["out"])
        self.assertNotIn("Cancelled", r["out"])
        self.assertIn("crash", r["chip"])


class SystemOneFollowsTheLane(unittest.TestCase):
    """The System One tab probed once per page load: after a switch, a stop or the image
    lane taking the box it went on saying "serving qwen3.8-27b", and a probe that once
    failed left Ask disabled for good."""

    def test_the_tab_asks_again_when_the_lane_changes(self):
        r = run(self, r"""
        let answer = {available: false, lane: '', reason: 'no text lane is serving'};
        __fetch = async url => url === '/api/systemone' ? __response(200, answer) : new Promise(() => {});
        rConfig(CONFIG); rUnits(UNITS);
        rLifecycle(life({'qwen38-sglang.service': eng('stopped')}));
        showTab('systemone'); await __settle();
        const first = {chip: txt('s1chip'), lane: txt('s1lane'), off: $('s1run').disabled};
        answer = {available: true, lane: 'qwen3.8-27b', reason: ''};
        rLifecycle(life({'qwen38-sglang.service': eng('ready')})); await __settle();
        report({first, chip: txt('s1chip'), lane: txt('s1lane'), off: $('s1run').disabled,
                note: txt('s1note'), probes: __fetches.filter(f => f.url === '/api/systemone').length});
        """)
        self.assertEqual(r["first"]["chip"], "not served here")
        self.assertTrue(r["first"]["off"])
        self.assertEqual((r["chip"], r["lane"], r["off"]), ("serving", "qwen3.8-27b", False))
        self.assertNotIn("no text lane", r["note"], "the old refusal is gone")
        self.assertIn("one chat completion", r["note"], "and the explanation is back")

    def test_a_revisit_asks_again(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/systemone' ? __response(200, {available: true, lane: 'qwen3.8-27b'}) : new Promise(() => {});
        showTab('systemone'); await __settle(); showTab('overview'); showTab('systemone'); await __settle();
        report({probes: __fetches.filter(f => f.url === '/api/systemone').length});
        """)
        self.assertEqual(r["probes"], 2)


class TheLogsTabOffersWhatThePageSendsYouTo(unittest.TestCase):
    """Five places send a person to "its journal in the Logs tab", and the tab offered the
    two containers, the proxy and opencode: no unit journal at all, so neither a text lane
    that failed at start (its container is gone) nor the image lane (it has no container)."""

    def test_the_image_lane_serving_opens_its_journal(self):
        r = run(self, r"""
        rUnits(UNITS);
        rLifecycle(life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})}));
        await __advance(3500);
        report({value: $('logsel').value});
        """)
        self.assertEqual(r["value"], "qwen38-image.service")

    def test_a_failed_text_lane_opens_its_journal(self):
        r = run(self, r"""
        rUnits(UNITS);
        rLifecycle(life({'qwen38-sglang.service': eng('failed')}));
        await __advance(3500);
        report({value: $('logsel').value});
        """)
        self.assertEqual(r["value"], "qwen38-sglang.service")

    def test_a_serving_text_lane_still_opens_its_container(self):
        r = run(self, r"""
        rUnits(UNITS);
        rLifecycle(life({'qwen38-flash.service': eng('ready', {target: 'flash'})}));
        await __advance(3500);
        report({value: $('logsel').value});
        """)
        self.assertEqual(r["value"], "qwen38-flash")


class TheSessionCardDoesNotHammerADeadRelay(unittest.TestCase):
    """agent-mobile.js, the script the relay injects into opencode's page on a phone,
    fetched the session list again the moment a fetch failed: with the relay unreachable
    that is a loop, one request per failure (the review counted 2,021 in 3 s in Chromium)."""

    MARKUP = ('<html><head><meta name="theme-color" content="#fff"></head>'
              '<body><div id="root"><div>opencode</div></div></body></html>')

    # the relay's answer is set before the script loads: its first request goes out at once
    REFUSED = "__fetch = () => new Promise((_, no) => setTimeout(() => no(new TypeError('Failed to fetch')), 5));"
    SLOW = "__fetch = () => new Promise(ok => setTimeout(() => ok(__response(200, [])), 4000));"

    def card(self, setup, body):
        return run(self, body, setup=setup, scripts=("agent-mobile.js",), markup=self.MARKUP,
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
        imgMode('edit'); imgRefs = [{name: NAME, dataUrl: 'data:image/png;base64,AAAA', w: 1, h: 1}];
        imgCurl();
        report(txt('imgcurl'));
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
        # curl's own -F syntax: the name in double quotes, \ and " escaped inside it, so a
        # ; or a , in the name is not read as the start of the next field
        self.assertEqual(args[args.index("-F") + 1], 'image[]=@"it\'s \\"a\\"; b.png";type=image/png')


class AnEditLeavesOutWhatTheEditsEndpointDropsUnread(unittest.TestCase):
    """The editing endpoint has no flow_shift field: the page sent the Shift box's value with
    an edit, and showed it in the edit's curl, and the lane dropped it unread (found in
    review, 2026-09-24)."""

    def payload(self, mode):
        return run(self, r"""
        imgMode(MODE); $('imgshift').value = '3.5';
        report({payload: imgPayload(), curl: (imgCurl(), txt('imgcurl'))});
        """.replace("MODE", json.dumps(mode)))

    def test_a_generation_keeps_the_shift(self):
        r = self.payload("t2i")
        self.assertEqual(r["payload"].get("flow_shift"), 3.5)
        self.assertIn("flow_shift", r["curl"])

    def test_an_edit_leaves_it_out(self):
        r = self.payload("edit")
        self.assertNotIn("flow_shift", r["payload"])
        self.assertNotIn("flow_shift", r["curl"])


def css_rules(markup):
    """(selector, [enclosing @media conditions], declarations) for every rule of the page's
    <style>, comments dropped."""
    import re
    css = re.sub(r"/\*.*?\*/", "", markup[markup.index("<style>") + 7:markup.index("</style>")], flags=re.S)
    out, stack, buf, i = [], [], "", 0
    while i < len(css):
        ch = css[i]
        if ch == "{":
            head = buf.strip()
            buf = ""
            if head.startswith("@"):
                stack.append(head)
            else:
                end = css.index("}", i)
                out.append((head, list(stack), css[i + 1:end].strip()))
                i = end
        elif ch == "}":
            stack.pop()
            buf = ""
        else:
            buf += ch
        i += 1
    return out


class TheTopBarMakesRoomInsteadOfOverlapping(unittest.TestCase):
    """Between 981 px and the widths where one row holds everything, the actions were shrunk
    inside the bar and slid under the connection lamp: measured in headless Chrome, 38 px
    at 1024 with a mouse and 68 px with a touch screen's buttons, and still 6 px at 1366
    (an iPad Pro in landscape). The page now measures the row and gives the actions one of
    their own when they do not fit."""

    GEOMETRY = r"""
    const bar = document.querySelector('header.top'), act = $('actbar'), pill = $('lanepill');
    let room = 1100, pillShown = false;
    Object.defineProperty(bar, 'clientWidth', {get: () => room});
    const width = (e, w) => { e.getBoundingClientRect = () => ({width: w, height: 30, top: 0, left: 0, right: w, bottom: 30}); };
    width($('railbtn'), 34); width(document.querySelector('.top .brand'), 250); width(document.querySelector('.top .conn'), 100);
    [...act.children].forEach((g, i) => width(g, [300, 120, 330][i]));
    getComputedStyle = e => ({display: e === pill && !pillShown ? 'none' : 'flex', columnGap: e === act ? '8px' : '12px',
                              paddingLeft: '12px', paddingRight: '16px', minWidth: e === pill ? '144px' : '0px',
                              getPropertyValue: () => ''});
    const wrapped = () => bar.classList.contains('topwrap');
    // brand, lamp and menu button 384, their three gaps 36, the actions 750 and two gaps 16,
    // the padding 28: 1,214 px without the pill, 1,370 with it at its minimum and its gap
    """

    def test_it_wraps_when_the_row_cannot_hold_the_actions(self):
        r = run(self, self.GEOMETRY + r"""
        const seen = [];
        for (const w of [1100, 1214, 1213, 1210, 1226, 1300, 1212]){ room = w; fitTopbar(); seen.push([w, wrapped()]); }
        report(seen);
        """)
        self.assertEqual(r, [[1100, True], [1214, True], [1213, True], [1210, True], [1226, False],
                             [1300, False], [1212, True]])

    def test_the_pill_counts_at_its_minimum(self):
        r = run(self, self.GEOMETRY + r"""
        pillShown = true; room = 1300; fitTopbar(); const a = wrapped();
        room = 1400; fitTopbar(); report([a, wrapped()]);
        """)
        self.assertEqual(r, [True, False])

    def test_a_phone_keeps_its_own_layout(self):
        r = run(self, self.GEOMETRY + r"""
        room = 1000; fitTopbar(); const a = wrapped();
        document.documentElement.style.setProperty('--top', '120px');
        room = 1500; fitTopbar();
        report([a, wrapped(), document.documentElement.style.getPropertyValue('--top')]);
        """)
        self.assertEqual(r[:2], [True, False])
        self.assertEqual(r[2], "", "a bar back on one row does not keep the height of two")
        r = run(self, self.GEOMETRY + r"""
        room = 700; fitTopbar(); report(wrapped());
        """, opts={"media": {"(max-width:980px)": True}})
        self.assertFalse(r)

    def test_the_wrapped_bar_puts_the_actions_on_their_own_row(self):
        rules = {sel: (media, decl) for sel, media, decl in css_rules((pagejs.STATIC / "index.html").read_text())}
        self.assertIn("flex-wrap:wrap", rules[".top.topwrap"][1].replace(" ", ""))
        self.assertIn("flex:1 0 100%", rules[".top.topwrap .actbar"][1])
        self.assertIn("order:3", rules[".top.topwrap .actbar"][1].replace(" ", ""))


class TheCollapsedRailIsADesktopThing(unittest.TestCase):
    """A rail collapsed in a wide window stayed collapsed on a narrow one, where the rail is
    a row of tabs and its button is hidden: 64 px of unlabelled icons and no way to open
    them, measured at 390 and 800 px in headless Chrome."""

    def test_every_collapsed_rail_rule_is_scoped_above_980_px(self):
        rules = [(sel, media) for sel, media, _ in css_rules((pagejs.STATIC / "index.html").read_text())
                 if "body.railmin" in sel]
        self.assertTrue(rules, "the page has collapsed-rail rules")
        for sel, media in rules:
            with self.subTest(sel=sel):
                self.assertTrue(any("min-width:981px" in m.replace(" ", "") for m in media), media)


if __name__ == "__main__":
    unittest.main()
