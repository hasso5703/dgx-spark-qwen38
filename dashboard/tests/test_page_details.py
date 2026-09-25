#!/usr/bin/env python3
"""The cockpit page's smaller defects, run: each class is one of the review's low findings
on the interface (U18 to U43, 2026-09-24) or the live-region chatter found beside U11,
reproduced here before it was fixed. Same harness as test_page_behaviour.py."""
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pagejs  # noqa: E402
from test_page_behaviour import HELPERS, css_rules  # noqa: E402

PAGE = (pagejs.STATIC / "index.html").read_text()


def run(test, body, **kw):
    return pagejs.run(test, HELPERS + body, **kw)


def rules():
    return css_rules(PAGE)


class AFinishedJobsLogIsReadToItsEnd(unittest.TestCase):
    """U18: the strip's log held the lines of the job's last running snapshot; the lines it
    printed after that were never fetched, so an open log stopped short of its end."""

    def test_the_last_lines_are_fetched_when_it_ends(self):
        r = run(self, r"""
        const job = (status, lines) => ({id: 'j1', action: 'smoke', params: {}, status, lines,
                                         started: Date.now() / 1000 - 5, elapsed: 5});
        __fetch = async url => url === '/api/jobs/j1' ? __response(200, {lines: ['1', '2', '3', 'the end']}) : new Promise(() => {});
        rJob({current: job('running', ['1', '2']), recent: []});
        rJob({current: null, recent: [Object.assign(job('done'), {ended: Date.now() / 1000, rc: 0})]});
        await __settle();
        report({fetched: __fetches.filter(f => f.url === '/api/jobs/j1').length, log: txt('joblog')});
        """)
        self.assertEqual(r["fetched"], 1)
        self.assertTrue(r["log"].endswith("the end"), r["log"])


class ANewLaneIsNotNamedAfterTheOldCheckpoint(unittest.TestCase):
    """U19: the served target comes from the text engine's /server_info, read every 30 s,
    and outlived a change of lane: a flash lane that had just replaced the 27B read
    "flash 176B stock" until the next read."""

    def test_label_and_selector(self):
        r = run(self, r"""
        rUnits(UNITS);
        rEngineInfo({served_target: 'stock', prompt_ceiling_tokens: 0,
                     info: {served_model_name: 'qwen3.8-27b', max_total_num_tokens: 800000, context_length: 1048576}});
        rLifecycle(life({'qwen38-sglang.service': eng('ready')}));
        rLifecycle(life({'qwen38-sglang.service': eng('stopped'), 'qwen38-flash.service': eng('ready', {target: 'flash-uncensored'})}));
        report({label: laneLabel('qwen38-flash.service'), pill: txt('lanename'), sel: $('switchsel').value});
        """)
        self.assertEqual(r["label"], "flash 176B uncensored")
        self.assertEqual(r["pill"], "flash 176B uncensored")
        self.assertEqual(r["sel"], "flash-uncensored")


class DurationsNeverReadSixtySeconds(unittest.TestCase):
    """U20: minutes were floored and seconds rounded, so 539.6 s read "8 min 60"."""

    def test_rounding_carries(self):
        r = run(self, "report([fmtDur(539.6), fmtDur(3599.7), fmtDur(89.6), fmtDur(61)]);")
        self.assertEqual(r, ["9 min 00", "1 h 00", "1 min 30", "61 s"])   # under 90 s it reads in seconds


class AStopIsNotABoot(unittest.TestCase):
    """U21: the Engines badge read "booting" while an engine stopped."""

    def test_the_badge_says_stopping(self):
        r = run(self, r"""
        rLifecycle(life({'qwen38-sglang.service': eng('stopping')}));
        const stopping = txt('bdg-engines');
        rLifecycle(life({'qwen38-sglang.service': eng('loading-weights')}));
        report([stopping, txt('bdg-engines')]);
        """)
        self.assertEqual(r, ["stopping", "booting"])


class SwitchPhrasesNameTheLane(unittest.TestCase):
    """U22: the job strip and the history read "switch to uncensored" for both the 27B and
    the flash uncensored targets."""

    def test_every_target_reads_differently(self):
        r = run(self, r"""
        const ts = ['stock', 'uncensored', 'fp8', 'uncensored-fp8', 'flash', 'flash-uncensored', 'flash-nvda', 'image'];
        report(ts.map(t => actionPhrase('switch', {target: t})));
        """)
        self.assertEqual(len(set(r)), len(r), r)
        self.assertIn("27B", r[1])
        self.assertIn("flash", r[5])


class GibibytesAreSaidAsSuch(unittest.TestCase):
    """U23: bytes divided by 1024 cubed were printed "GB" (the memory and disk gauges), and
    the image lane's peak, MiB over 1024, too."""

    def test_the_units(self):
        r = run(self, r"""
        imgDraw({data: [{b64_json: 'AAAA'}], peak_memory_mb: 35635.2, inference_time_s: 38.2}, 'png');
        report({b: fmtB(GB), peak: txt('imgmeta')});
        """)
        self.assertEqual(r["b"], "1.0 GiB")
        self.assertIn("peak memory: 34.8 GiB", r["peak"])


class OneBootEstimate(unittest.TestCase):
    """U24: a flash boot was "about 13 min" in the boot bar and the Start tooltip (the
    reference box's default, or this box's own median), and "the full 12 to 15 min" in the
    warning a stop mid-boot gives. The warning reads the same estimate now."""

    def test_the_stop_warning_uses_the_lanes_own_estimate(self):
        r = run(self, r"""
        rConfig(CONFIG); rUnits(UNITS);
        rLifecycle(life({'qwen38-flash.service': eng('loading-weights', {target: 'flash', boots: [700, 760, 800], elapsed: 60})}));
        CARDS.get('qwen38-flash.service').btn.click();
        report({warn: txt('mwarn'), ready: readyIn('qwen38-flash.service')});
        """)
        self.assertEqual(r["ready"], "about 12 min 40")
        self.assertIn("about 12 min 40", r["warn"])
        self.assertNotIn("12 to 15", r["warn"])


class TheServedEnginePanelKnowsTheImageLaneIsNotOne(unittest.TestCase):
    """U25: with only the image lane serving, the text engine's panel said "no text engine"
    under a chip that read READY."""

    def test_the_chip(self):
        r = run(self, r"""
        rLifecycle(life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})}));
        rEngineFast({load: null, healthy: false});
        report(txt('engchip'));
        """)
        self.assertNotEqual(r, "ready")
        self.assertIn("no text engine", r)


class WarningsWearTheirColourEverywhere(unittest.TestCase):
    """U26: .why.warn was styled inside an engine card only (the Overview's wedge note and
    the image tab's boot notes are not in one), and #upd.warn not at all."""

    def test_the_rules_exist(self):
        sels = {s.strip() for sel, media, _ in rules() for s in sel.split(",") if not media}
        self.assertIn(".why.warn", sels)
        self.assertIn("#upd.warn", sels)
        self.assertIn(".why", sels)


class AnIndeterminateBarDoesNotReadFullWithoutMotion(unittest.TestCase):
    """U27: with reduced motion every animation stops, so an indeterminate bar was a still,
    full bar: what a finished one looks like. Without motion it pulses its opacity."""

    def test_the_bars_pulse(self):
        found = {}
        for sel, media, decl in rules():
            if any("prefers-reduced-motion" in m for m in media):
                for s in sel.split(","):
                    found[s.strip()] = decl
        for s in (".bfill.indet", ".jobstrip .bar i", ".img .prog.indet .fl"):
            with self.subTest(s=s):
                self.assertIn(s, found)
                self.assertRegex(found[s], r"animation:\s*indetpulse[^;]*!important")
        frames = re.search(r"@keyframesindetpulse\{(.*?)\}\}", PAGE.replace(" ", ""), re.S)
        self.assertTrue(frames, "the pulse has keyframes")
        self.assertEqual(set(re.findall(r"\{([a-z-]+):", frames.group(1) + "}")), {"opacity"}, "opacity only, no motion")


class TheTopBarShrinksBack(unittest.TestCase):
    """U28: the bar's min-height is --top, and --top was set to the bar's own height: once
    two rows tall (a narrow window, a zoom, a wrap) it could never be shorter again."""

    def test_the_height_is_measured_without_the_old_one(self):
        r = run(self, r"""
        const bar = document.querySelector('header.top'); let content = 90;
        bar.getBoundingClientRect = () => {
          const min = parseFloat(document.documentElement.style.getPropertyValue('--top')) || 58;
          const h = Math.max(content, min); return {height: h, width: 1200, top: 0, left: 0, right: 1200, bottom: h};
        };
        syncTop(); const tall = document.documentElement.style.getPropertyValue('--top');
        content = 58; syncTop();
        report([tall, document.documentElement.style.getPropertyValue('--top')]);
        """)
        self.assertEqual(r, ["90px", "58px"])


class TheCollapsedRailsButtonsHaveNames(unittest.TestCase):
    """U29: collapsed, the rail's labels are display:none, which takes them out of the
    buttons' accessible names: eleven unnamed buttons."""

    def test_every_nav_button_is_labelled(self):
        navs = re.findall(r'<button class="nav" ([^>]*)>.*?<span class="lab">([^<]+)</span>', PAGE)
        self.assertEqual(len(navs), 11)
        for attrs, lab in navs:
            with self.subTest(lab=lab):
                self.assertIn(f'aria-label="{lab}"', attrs)


class RestartKeepsItsTooltip(unittest.TestCase):
    """U30: applyBusy() gave every unit button its blocked reason or nothing, and the Agent
    tab's Restart is one: its tooltip was gone at the first refresh."""

    def test_the_tooltip_survives(self):
        r = run(self, "const before = $('agrestart').title; applyBusy(); report([before, $('agrestart').title]);")
        self.assertTrue(r[0])
        self.assertEqual(r[1], r[0])


class AnUnreadableOpencodeConfigSaysSo(unittest.TestCase):
    """U31: a config that does not parse read like an empty one: "none" for the default
    model, "not declared" for the limits."""

    def test_the_error_is_shown(self):
        r = run(self, r"""
        rOpencode({enabled: true, real: {present: true, default: null, limits: {}, error: 'Expecting value: line 3 column 5 (char 20)'},
                   launcher: {present: true, ours: true, cap: 200000}, follows: false, why: 'differs', fit: null});
        report({state: txt('ocstate'), def: txt('ocdefault'), lim: txt('oclim27')});
        """)
        self.assertIn("Expecting value", r["state"])
        self.assertNotIn("none", r["def"])
        self.assertNotIn("not declared", r["lim"])


class EveryOfferedSizeCanBeAskedFor(unittest.TestCase):
    """U32: 2400x1792 is 4.30 megapixels, over the 4.23 of the largest call measured, so the
    list offered a size the page itself always refused."""

    def test_each_size_passes(self):
        r = run(self, r"""
        $('imgprompt').value = 'a cat';
        const bad = [];
        for (const [v] of IMG_SIZES){ if (v === 'custom') continue; $('imgsize').value = v; imgSync(); if (imgProblem()) bad.push(v); }
        report(bad);
        """)
        self.assertEqual(r, [])


class TheSystemOneCommandSurvivesAnApostrophe(unittest.TestCase):
    """U34: the state went into the curl line inside single quotes as it was typed, so
    "I'm losing sales" ended the shell string halfway."""

    def test_the_pasted_command_sends_the_payload(self):
        r = run(self, r"""
        $('s1state').value = "I'm losing sales, it's urgent"; s1Curl();
        report({cmd: txt('s1curl'), payload: s1Payload()});
        """)
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run(["bash", "-c", "curl(){ printf '%s\\n' \"$@\"; }\ncat(){ echo KEY; }\n" + r["cmd"]],
                               cwd=d, capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        body = p.stdout[p.stdout.index("{"):]
        self.assertEqual(json.loads(body), r["payload"])


class SystemOneQuestionIdsStayUnique(unittest.TestCase):
    """U35: a new question took its type and the list's length plus one as its id, so after
    a removal two questions could share one, and the payload, keyed by id, kept one."""

    def test_a_new_question_takes_a_free_id(self):
        r = run(self, r"""
        s1Questions = [];
        const add = t => document.querySelector(`[data-addq="${t}"]`).click();
        add('noul'); add('noul'); s1Questions.splice(0, 1); add('noul');
        report({ids: s1Questions.map(q => q.id), sent: Object.keys(s1Payload().questions).length});
        """)
        self.assertEqual(len(r["ids"]), 2, r)
        self.assertEqual(len(set(r["ids"])), 2, r)
        self.assertEqual(r["sent"], 2)

    def test_two_typed_alike_are_refused_before_sending(self):
        r = run(self, r"""
        __fetch = async () => __response(200, {token: 't'});
        s1Questions = [{id: 'q', type: 'noul', instructions: 'a'}, {id: 'q', type: 'noul', instructions: 'b'}];
        $('s1state').value = 'x';
        await s1Run();
        report({asked: __fetches.filter(f => f.url === '/api/systemone').length, toast: txt('toast')});
        """)
        self.assertEqual(r["asked"], 0)
        self.assertIn("q", r["toast"])


class ABusyLaneLeavesTheLastImageInPlace(unittest.TestCase):
    """U36: the frame of a new request replaced the last image at once, and a 409 (someone
    else generating) left the empty frame there."""

    def test_the_previous_image_comes_back(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/image/generate' ? __response(409, {error: 'this lane serves one image at a time'})
          : url === '/api/image' ? __response(200, {available: true, installed: true, progress: {}}) : new Promise(() => {});
        rLifecycle(life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})}));
        imgDraw({data: [{b64_json: 'AAAA'}], inference_time_s: 38.2}, 'png');
        $('imgprompt').value = 'a cat'; imgSync();
        await imgRun();
        report({imgs: document.querySelectorAll('#imgout img').length, meta: txt('imgmeta')});
        """)
        self.assertEqual(r["imgs"], 1)
        self.assertIn("engine time", r["meta"])


class TenReferencesAtMost(unittest.TestCase):
    """U37: "Use a sample" checked the count before its twenty-second generation and added
    the sample after it, so references added meanwhile made eleven."""

    def test_the_sample_rechecks(self):
        r = run(self, r"""
        let answer;
        __fetch = url => url === '/api/csrf' ? Promise.resolve(__response(200, {token: 't'}))
          : url === '/api/image/generate' ? new Promise(ok => { answer = ok; }) : new Promise(() => {});
        rLifecycle(life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})}));
        imgMode('edit'); imgRefs = Array.from({length: 9}, (_, i) => ({name: 'r' + i, dataUrl: 'data:image/png;base64,AA', w: 1, h: 1}));
        $('imgrefsample').click(); await __settle();
        imgRefs.push({name: 'late', dataUrl: 'data:image/png;base64,AA', w: 1, h: 1});
        answer(__response(200, {image: {data: [{b64_json: 'BBBB'}]}})); await __settle(); await __settle();
        report(imgRefs.length);
        """)
        self.assertLessEqual(r, 10)


class ThePhonesSessionCardIsNotRewrittenForNothing(unittest.TestCase):
    """U38: the card's markup was written again every 500 ms, and a tap on iOS that
    straddles a rewrite is lost."""

    MARKUP = ('<html><head><meta name="theme-color" content="#fff"></head>'
              '<body><div id="root"><div>opencode</div></div></body></html>')

    def test_one_write_for_one_list(self):
        r = run(self, r"""
        await __advance(1000);
        const card = document.getElementById('spark-sessions');
        let writes = 0; const set = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(card), 'innerHTML').set;
        Object.defineProperty(card, 'innerHTML', {set(v){ writes++; set.call(card, v); }, get(){ return card._html; }});
        await __advance(3000);
        report({card: !!card, writes});
        """, setup="__fetch = async () => __response(200, [{id: 's1', title: 'a session', directory: '/w', time: {updated: 1}}]);",
            scripts=("agent-mobile.js",), markup=self.MARKUP, opts={"media": {"(pointer: coarse)": True}})
        self.assertTrue(r["card"])
        self.assertEqual(r["writes"], 0)


class TheEditEstimateCountsItsReferences(unittest.TestCase):
    """U39: an edit cost six seconds more than a generation whatever it carried: ten
    references at 20 steps were announced at 26 s and measured at 69.6."""

    def test_the_two_measured_edits(self):
        r = run(self, r"""
        report([imgEstimate({w: 1024, h: 1024, steps: 40, n: 1, editing: true, refs: 1}),
                imgEstimate({w: 1024, h: 1024, steps: 20, n: 1, editing: true, refs: 10}),
                imgEstimate({w: 1024, h: 1024, steps: 40, n: 1, editing: false})]);
        """)
        self.assertAlmostEqual(r[0], 44.6, delta=44.6 * 0.1)
        self.assertAlmostEqual(r[1], 69.6, delta=69.6 * 0.1)
        self.assertAlmostEqual(r[2], 38.2, delta=38.2 * 0.1)


class TheUpdateCommandsNameThisCheckout(unittest.TestCase):
    """U40: the update banner and the Agent tab's install hint said cd ~/dgx-spark-qwen38
    whatever the checkout; the cockpit knows its own."""

    def test_both_commands(self):
        r = run(self, r"""
        rConfig(Object.assign({}, CONFIG, {repo_dir: '/opt/qwen', terminal_only: {update_stack: 'cd /opt/qwen && ./install.sh'}}));
        rUpdate({installed: 'v1.18.6', latest: 'v1.18.7', behind: true});
        banners({}, {});
        rAgent({enabled: false, opencode_found: null, pinned: '1.18.32'});
        report({banner: txt('banners'), agent: txt('agcmd')});
        """)
        self.assertIn("cd /opt/qwen", r["banner"])
        self.assertNotIn("~/dgx-spark-qwen38", r["banner"])
        self.assertEqual(r["agent"], "cd /opt/qwen && ./install.sh")


class ExampleNamesCountRight(unittest.TestCase):
    """U41: the "Twenty options" example has eight."""

    def test_counts(self):
        r = run(self, "report(Object.entries(S1_EXAMPLES).map(([n, e]) => [n, e.questions.map(q => (q.criteria || q.levels || []).length)]));")
        words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "ten": 10, "twenty": 20}
        for name, counts in r:
            m = re.match(r"(\w+) options$", name)
            if m:
                with self.subTest(name=name):
                    self.assertIn(words[m.group(1).lower()], counts)


class TheImageExampleShowsWhichIsLoaded(unittest.TestCase):
    """U42: the first Image example stayed highlighted whichever was loaded."""

    def test_the_highlight_moves(self):
        r = run(self, r"""
        const bs = [...document.querySelectorAll('#imgexamples .btn')];
        bs[2].click();
        report(bs.map(b => b.classList.contains('low')));
        """)
        self.assertEqual(r[2], False)
        self.assertEqual(r.count(False), 1)


class LiveRegionsSayWhatChanged(unittest.TestCase):
    """The lane pill and the job strip were live regions whose text changed every second
    (a boot's elapsed time, a job's clock and its last line): a screen reader read them
    again every second, the defect of U11 in two more places. Each now says a sentence when
    the lane or the job changes state, and nothing on a tick."""

    def test_the_containers_are_not_live(self):
        self.assertNotRegex(PAGE, r'id="lanepill"[^>]*aria-live')
        self.assertNotRegex(PAGE, r'id="jobstrip"[^>]*aria-live')

    def test_the_lane_says_its_state_once(self):
        r = run(self, r"""
        const said = [];
        const tick = (state, elapsed) => { rLifecycle(life({'qwen38-flash.service': eng(state, {target: 'flash', elapsed})})); said.push(txt('lanesay')); };
        tick('loading-weights', 10); tick('loading-weights', 11); tick('loading-weights', 12); tick('ready', 13); tick('ready', 14);
        report({said, live: $('lanesay').getAttribute('aria-live')});
        """)
        self.assertEqual(r["live"], "polite")
        self.assertEqual(len(set(r["said"])), 2, r["said"])
        self.assertIn("ready", r["said"][-1])

    def test_the_job_says_its_start_and_its_end(self):
        r = run(self, r"""
        const job = (status, elapsed, last) => ({id: 'j', action: 'smoke', params: {}, status, started: Date.now() / 1000 - elapsed,
                                                 elapsed, lines: [last]});
        const said = [];
        [1, 2, 3].forEach(s => { rJob({current: job('running', s, 'line ' + s), recent: []}); said.push(txt('jobsay')); });
        rJob({current: null, recent: [Object.assign(job('done', 4, 'end'), {ended: Date.now() / 1000, rc: 0})]}); said.push(txt('jobsay'));
        report({said, live: $('jobsay').getAttribute('aria-live')});
        """)
        self.assertEqual(r["live"], "polite")
        self.assertEqual(len(set(r["said"])), 2, r["said"])


if __name__ == "__main__":
    unittest.main()
