#!/usr/bin/env python3
"""The cockpit page's smaller defects, run: each class is one of the review's low findings
on the interface (U18 to U43, 2026-09-24), a finding of the video lane's reviews
(September 2026), or one met while the page was rebuilt, reproduced before it was fixed.
Same harness as test_page_behaviour.py."""
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


class TheVideoViewGuardsTheWire(unittest.TestCase):
    """The view refuses what the server refuses, and what the server would silently drop:
    a seed of 'abc' used to ride out as seed:null and the user got a random video (found in
    review, 2026-09-28). A seed past 2**53 is refused too: a browser rounds the number it
    sends, so the video would not be the one the seed names (found rebuilding the page).
    The tenth of slack under the lock is the other half of the same refusal
    (TheEstimateParity holds the numbers equal to the server's)."""

    def test_a_seed_that_is_not_a_whole_number_never_reaches_the_wire(self):
        r = run(self, r"""
        $('vid-prompt').value = 'a cat';
        const seed = v => { $('vid-seed').value = v; return vidProblem(); };
        report([seed('abc'), seed('e5'), seed('1.5'), seed('-1'), seed('123'), seed(''), seed('9007199254740991'), seed('9007199254740993')]);
        """)
        for bad in r[:4]:
            self.assertIn("seed is a whole number", bad.lower())
        self.assertEqual(r[4:7], ["", "", ""])
        self.assertIn("rounds", r[7])

    def test_720p_is_admitted_at_the_4_s_it_was_measured_at_and_no_longer(self):
        """A video's memory grows far faster than its length (9.4 GiB for 4 s at 480p,
        76.5 GiB for 15 s), and 4 s at 720p already peaks at 80 GiB of 121.6: the time
        budget alone let 720p run to 15 s, which cannot fit and hangs the box. The page
        refuses what the server refuses (TheMemoryCeilingOfLongVideos), and the length
        slider ends where the size was measured."""
        r = run(self, r"""
        $('vid-prompt').value = 'a cat';
        VS.size = '1280x720'; $('vid-secs').value = '8'; const long = vidProblem();
        vidSync(); const clamped = {max: $('vid-secs').max, value: $('vid-secs').value, ok: vidProblem()};
        VS.size = '864x480'; vidSync(); $('vid-secs').value = '15'; $('vid-steps').value = '100';
        const small = vidProblem(), max480 = $('vid-secs').max;
        report({long, clamped, small, max480});
        """)
        self.assertIn("720p is measured here at 4 s only", r["long"])
        self.assertIn("hangs", r["long"])
        self.assertEqual(r["clamped"], {"max": "4", "value": "4", "ok": ""})
        self.assertEqual(r["small"], "")   # 480p keeps its 15 s and its 100 steps
        self.assertEqual(r["max480"], "15")

    def test_a_missing_steps_field_does_not_throw(self):
        r = run(self, r"""
        $('vid-prompt').value = 'a cat';
        $('vid-steps').remove();
        report({problem: vidProblem(), body: vidWireBody().num_inference_steps});
        """)
        self.assertEqual(r["problem"], "")
        self.assertEqual(r["body"], 50)

    def test_the_wire_body_is_what_the_lane_requires(self):
        r = run(self, r"""
        $('vid-prompt').value = "it's a fox"; $('vid-secs').value = '6'; VS.size = '864x480'; $('vid-seed').value = '42';
        const t = vidWireBody();
        VS.mode = 'fl2v'; VS.frames.first = {name: 'a.png', dataUrl: 'data:image/png;base64,AA'};
        const k = vidWireBody();
        report({t, k});
        """)
        self.assertEqual(r["t"]["task"], "t2va")
        self.assertEqual(r["t"]["target"], {"short_edge": 480, "aspect_ratio": "16:9", "duration_seconds": 6})
        self.assertEqual(r["t"]["seed"], 42)
        self.assertEqual(r["k"]["task"], "fl2va")
        self.assertEqual([c["frame_index"] for c in r["k"]["conditions"]], [0], "only the frame that was attached")


class AProblemIsSaidOnceTheFormIsTouched(unittest.TestCase):
    """The old Video tab opened on "A prompt is required." in red under an empty form, and
    greyed Generate with no reason while the lane was stopped (seen live, 2026-09-29)."""

    def test_an_empty_form_is_quiet_and_a_stopped_lane_says_why(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'}), 'qwen38-video.service': eng('stopped', {target: 'video'})})});
        vidSync();
        const quiet = $('vid-problem').hidden;
        const why = txt('vid-why'), off = $('vid-run').disabled;
        $('vid-prompt').dispatchEvent({type: 'blur'});
        report({quiet, why, off, after: $('vid-problem').hidden, lane: txt('vid-lane')});
        """)
        self.assertTrue(r["quiet"], "no error under a form nobody touched")
        self.assertTrue(r["off"])
        self.assertIn("not serving", r["why"])
        self.assertFalse(r["after"], "once touched, the missing prompt is said")
        self.assertIn("Load MiniMax-H3", r["lane"])
        self.assertIn("flash 176B", r["lane"], "and it says which lane holds the box")


class TheVideoShowsTheLanesRealProgress(unittest.TestCase):
    """The lane's API says "queued", progress 0, from the POST to the end, and the old tab
    drew exactly that for twelve minutes (seen live, 2026-09-29). The server now reads the
    phase and the step from the lane's journal; the view draws those."""

    def test_the_step_and_the_time_left(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/video' ? __response(200, {installed: true, available: true, state: 'ready',
            progress: {id: 'v9', status: 'queued', progress: 0},
            run: {phase: 'denoise', step: 26, steps: 49, s_per_step: 14.75, left_s: 339, label: 'denoising'}}) : new Promise(() => {});
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-video.service': eng('ready', {target: 'video'})})});
        await vidLane();
        report({lab: txt('vid-prog-lab'), eta: txt('vid-prog-eta'), hidden: $('vid-prog').hidden, spine: txt('serving-sub'),
                off: $('vid-run').disabled, why: txt('vid-why'), phases: [...document.querySelectorAll('#vid-phases span')].map(s => s.className)});
        """)
        self.assertFalse(r["hidden"])
        self.assertIn("step 26 of 49", r["lab"])
        self.assertIn("Someone else", r["lab"], "a video this page did not ask for is said to be another's")
        self.assertIn("left", r["eta"])
        self.assertIn("14.8 s per step", r["eta"])
        self.assertEqual(r["phases"], ["done", "now", ""])
        self.assertTrue(r["off"])
        self.assertIn("one at a time", r["why"])


class TheParkedVideoSurvivesAReload(unittest.TestCase):
    """The 504's address only lived in one page run: an F5 during the second hour lost the
    link to a video the lane was still making (found in review, 2026-09-28). The id is kept
    in the browser, so a reload still says "your video" and shows it when it is done."""

    BODY = r"""
    let done = false;
    __fetch = async url => url === '/api/video' ? __response(200, done
        ? {installed: true, available: true, state: 'ready', progress: {}, run: {}}
        : {installed: true, available: true, state: 'ready', progress: {id: 'vid-parked-9', status: 'queued'},
           run: {phase: 'denoise', step: 40, steps: 49, s_per_step: 55, left_s: 500, label: 'denoising'}}) : new Promise(() => {});
    feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-video.service': eng('ready', {target: 'video'})})});
    """

    def test_a_parked_video_is_still_yours_after_a_reload(self):
        r = run(self, self.BODY + r"""
        localStorage.setItem('cockpit.video.parked', 'vid-parked-9');
        await vidLane(); const lab = txt('vid-prog-lab');
        done = true; await vidLane();
        const v = document.querySelector('#vid-screen video');
        report({lab, src: v ? v.src : null, kept: localStorage.getItem('cockpit.video.parked')});
        """)
        self.assertIn("Still making your video", r["lab"])
        self.assertIn("/api/video/content?id=vid-parked-9", r["src"] or "")
        self.assertIsNone(r["kept"], "the id is forgotten once the video is shown")

    def test_a_stranger_is_not_played_by_itself(self):
        r = run(self, self.BODY + r"""
        await vidLane(); done = true; await vidLane();
        report({video: !!document.querySelector('#vid-screen video'), offer: txt('vid-meta')});
        """)
        self.assertFalse(r["video"])
        self.assertIn("Play the video that just finished", r["offer"])

    def test_a_finished_video_leaves_no_stale_generating_text(self):
        """The old tab kept "c3da273b still generating" and "the lane is still making this
        video" on screen after the video was done, until a reload (seen live, 2026-09-29)."""
        r = run(self, self.BODY + r"""
        await vidLane(); done = true; await vidLane();
        report({prog: $('vid-prog').hidden, page: txt('view-video')});
        """)
        self.assertTrue(r["prog"])
        self.assertNotIn("still generating", r["page"].lower())
        self.assertNotIn("Someone else", r["page"])


class AFinishedJobsLogIsReadToItsEnd(unittest.TestCase):
    """U18: the log held the lines of the job's last running snapshot; the lines it printed
    after that were never fetched, so an open log stopped short of its end."""

    def test_the_last_lines_are_fetched_when_it_ends(self):
        r = run(self, r"""
        const job = (status, lines) => ({id: 'j1', action: 'smoke', params: {}, status, lines,
                                         started: Date.now() / 1000 - 5, elapsed: 5});
        __fetch = async url => url === '/api/jobs/j1' ? __response(200, {lines: ['1', '2', '3', 'the end']}) : new Promise(() => {});
        feed({job: {current: job('running', ['1', '2']), recent: []}});
        feed({job: {current: null, recent: [Object.assign(job('done'), {ended: Date.now() / 1000, rc: 0})]}});
        await __settle();
        report({fetched: __fetches.filter(f => f.url === '/api/jobs/j1').length, log: txt('dock-pre'), shown: !$('dock').hidden});
        """)
        self.assertEqual(r["fetched"], 1)
        self.assertTrue(r["log"].endswith("the end"), r["log"])
        self.assertTrue(r["shown"])


class ANewLaneIsNotNamedAfterTheOldCheckpoint(unittest.TestCase):
    """U19: the served target comes from the text engine's /server_info, read every 30 s,
    and outlived a change of lane: a flash lane that had just replaced the 27B read
    "flash 176B stock" until the next read."""

    def test_label_spine_and_selector(self):
        r = run(self, r"""
        feed({units: UNITS, engine_info: {served_target: 'stock', prompt_ceiling_tokens: 0,
              info: {served_model_name: 'qwen3.8-27b', max_total_num_tokens: 800000, context_length: 1048576}},
              lifecycle: life({'qwen38-sglang.service': eng('ready')})});
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-flash.service': eng('ready', {target: 'flash-uncensored'})})});
        report({label: laneLabel('qwen38-flash.service'), spine: txt('serving-name'), sel: LCARDS.get('qwen38-flash.service').tsel.value});
        """)
        self.assertEqual(r["label"], "flash 176B uncensored")
        self.assertIn("flash 176B uncensored", r["spine"])
        self.assertEqual(r["sel"], "flash-uncensored")


class ALaneThatIsNotHereSaysWhatInstallsIt(unittest.TestCase):
    """The 27B card of a flash-only box said "./install.sh", which re-runs the lane the box
    serves and never brings the 27B one, and a missing lane's card offered targets whose
    choice changed nothing (a box installed from nothing, 2026-09-30). Since v1.20 a plain
    install installs every lane, so a missing one was left out or did not fit, and
    --with-<lane> brings it back beside the lane that serves."""

    def test_the_command_brings_the_lane_back_beside_the_served_one(self):
        r = run(self, r"""
        const units = {units: Object.assign({}, UNITS.units, {
          'qwen38-sglang.service': {active: 'inactive', enabled: ''},
          'qwen38-flash.service': {active: 'active', enabled: 'enabled'}})};
        feed(Object.assign({}, units, {lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'})})}));
        const c27 = LCARDS.get('qwen38-sglang.service');
        report({c27: c27.right.textContent, picker: c27.tsel.hidden,
                cimg: LCARDS.get('qwen38-image.service').right.textContent,
                served: LCARDS.get('qwen38-flash.service').right.textContent,
                servedPicker: LCARDS.get('qwen38-flash.service').tsel.hidden});
        """)
        self.assertIn("./install.sh --with-27b", r["c27"])
        self.assertTrue(r["picker"], "no checkpoint to pick on a lane that is not here")
        self.assertIn("./install.sh --with-image", r["cimg"])
        self.assertNotIn("./install.sh", r["served"], "an installed lane shows no install command")
        self.assertFalse(r["servedPicker"])


class DurationsNeverReadSixtySeconds(unittest.TestCase):
    """U20: minutes were floored and seconds rounded, so 539.6 s read "8 min 60"."""

    def test_rounding_carries(self):
        r = run(self, "report([fmtDur(539.6), fmtDur(3599.7), fmtDur(89.6), fmtDur(61), fmtMin(759.6), fmtMin(45)]);")
        self.assertEqual(r, ["9 min 00", "1 h 00", "1 min 30", "61 s", "13 min", "45 s"])


class AStopIsNotABoot(unittest.TestCase):
    """U21: the Engines badge read "booting" while an engine stopped."""

    def test_the_badge_says_stopping(self):
        r = run(self, r"""
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopping')})});
        const stopping = txt('bdg-lanes');
        feed({lifecycle: life({'qwen38-sglang.service': eng('loading-weights')})});
        report([stopping, txt('bdg-lanes')]);
        """)
        self.assertEqual(r, ["stopping", "booting"])


class SwitchPhrasesNameTheLane(unittest.TestCase):
    """U22: the job strip and the history read "switch to uncensored" for both the 27B and
    the flash uncensored targets."""

    def test_every_target_reads_differently(self):
        r = run(self, r"""
        const ts = ['stock', 'uncensored', 'fp8', 'uncensored-fp8', 'flash', 'flash-uncensored', 'flash-nvda', 'image', 'video', 'image-turbo'];
        report(ts.map(t => actionPhrase('switch', {target: t})));
        """)
        self.assertEqual(len(set(r)), len(r), r)
        self.assertIn("27B", r[1])
        self.assertIn("flash", r[5])
        self.assertIn("MiniMax-H3", r[8])
        self.assertIn("Turbo", r[9])


class GibibytesAreSaidAsSuch(unittest.TestCase):
    """U23: bytes divided by 1024 cubed were printed "GB", and the image lane's peak, MiB
    over 1024, too."""

    def test_the_units(self):
        r = run(self, r"""
        imgShow({data: [{b64_json: 'AAAA'}], peak_memory_mb: 35635.2, inference_time_s: 38.2}, 'png');
        report({b: fmtGiB(GIB), peak: txt('img-meta')});
        """)
        self.assertEqual(r["b"], "1.0 GiB")
        self.assertIn("peak memory 34.8 GiB", r["peak"])


class OneBootEstimate(unittest.TestCase):
    """U24: a flash boot was "about 13 min" in the boot bar and "the full 12 to 15 min" in
    the warning a stop mid-boot gives. The warning reads the same estimate."""

    def test_the_stop_warning_uses_the_lanes_own_estimate(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS,
              lifecycle: life({'qwen38-flash.service': eng('loading-weights', {target: 'flash', boots: [700, 760, 800], elapsed: 60})})});
        LCARDS.get('qwen38-flash.service').stop.click();
        report({warn: txt('sh-warns'), ready: readyIn('qwen38-flash.service')});
        """)
        self.assertEqual(r["ready"], "about 12 min 40")
        self.assertIn("about 12 min 40", r["warn"])
        self.assertNotIn("12 to 15", r["warn"])


class TheTextEnginePanelKnowsADiffusionLaneIsNotOne(unittest.TestCase):
    """U25: with only the image lane serving, the text engine's panel said "no text engine"
    under a chip that read READY; the video lane is the same case."""

    def test_the_panel_says_which_lane_serves(self):
        for unit, target, word in (("qwen38-image.service", "image", "image"), ("qwen38-video.service", "video", "video")):
            with self.subTest(unit=unit):
                r = run(self, r"""
                feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), '%s': eng('ready', {target: '%s'})}),
                      engine_fast: {load: null, healthy: false}});
                report({down: txt('eng-down'), hidden: $('eng-facts').hidden, cap: txt('tr-cap')});
                """ % (unit, target))
                self.assertTrue(r["hidden"])
                self.assertIn(word + " lane is serving", r["down"])
                self.assertIn(word + " lane serves", r["cap"])
                self.assertIn("no text engine", r["cap"])


class StatesWearTheirColourEverywhere(unittest.TestCase):
    """U26: a warning colour was styled inside one component only; every state has its rule
    wherever it is drawn: capsules, tags, lamps, banners, text."""

    def test_the_rules_exist(self):
        sels = {s.strip() for sel, media, _ in css_rules() for s in sel.split(",") if not media}
        for s in (".cap.warn", ".cap.err", ".tag.warn", ".tag.err", ".lamp.warn", ".lamp.err", ".banner.err", ".warn-t", ".err-t"):
            with self.subTest(s=s):
                self.assertIn(s, sels)


class AnIndeterminateBarDoesNotReadFullWithoutMotion(unittest.TestCase):
    """U27: with reduced motion every animation stops, so an indeterminate bar was a still,
    full bar: what a finished one looks like. Without motion it pulses its opacity."""

    def test_the_bars_pulse(self):
        found = {}
        for sel, media, decl in css_rules():
            if any("prefers-reduced-motion" in m for m in media):
                for s in sel.split(","):
                    found[s.strip()] = decl
        for s in (".meter.indet > i", ".ring.indet .fill"):
            with self.subTest(s=s):
                self.assertIn(s, found)
                self.assertRegex(found[s], r"animation:\s*indetpulse[^;]*!important")
        css = (pagejs.STATIC / "css" / "cockpit.css").read_text().replace(" ", "")
        frames = re.search(r"@keyframesindetpulse\{(.*?)\}\}", css, re.S)
        self.assertTrue(frames, "the pulse has keyframes")
        self.assertEqual(set(re.findall(r"\{([a-z-]+):", frames.group(1) + "}")), {"opacity"}, "opacity only, no motion")


class TheRailsButtonsHaveNames(unittest.TestCase):
    """U29: collapsed, the rail's labels are display:none, which takes them out of the
    buttons' accessible names: eleven unnamed buttons."""

    def test_every_nav_button_is_labelled(self):
        navs = re.findall(r'<button class="nav" ([^>]*)>.*?<span class="lab">([^<]+)</span>', PAGE)
        self.assertEqual(len(navs), 11)
        for attrs, lab in navs:
            with self.subTest(lab=lab):
                self.assertIn(f'aria-label="{lab}"', attrs)


class RestartKeepsItsTooltip(unittest.TestCase):
    """U30: the busy pass gave every action button its blocked reason or nothing, and the
    Agent view's Restart lost its tooltip at the first refresh."""

    def test_the_tooltip_survives(self):
        r = run(self, "const before = $('ag-restart').title; applyBusy(); feed({config: CONFIG}); report([before, $('ag-restart').title]);")
        self.assertTrue(r[0])
        self.assertEqual(r[1], r[0])


class AnUnreadableOpencodeConfigSaysSo(unittest.TestCase):
    """U31: a config that does not parse read like an empty one: "none" for the default
    model, "not declared" for the limits."""

    def test_the_error_is_shown(self):
        r = run(self, r"""
        feed({opencode: {enabled: true, real: {present: true, default: null, limits: {}, error: 'Expecting value: line 3 column 5 (char 20)'},
                         launcher: {present: true, ours: true, cap: 200000}, follows: false, why: 'differs', fit: null}});
        report({state: txt('oc-state'), def: txt('oc-default'), lim: txt('oc-lim27')});
        """)
        self.assertIn("Expecting value", r["state"])
        self.assertNotIn("none", r["def"])
        self.assertNotIn("not declared", r["lim"])


class EveryOfferedSizeCanBeAskedFor(unittest.TestCase):
    """U32: 2400x1792 is 4.30 megapixels, over the 4.23 of the largest call measured, so the
    list offered a size the page itself always refused."""

    def test_each_size_passes(self):
        r = run(self, r"""
        $('img-prompt').value = 'a cat';
        const bad = [];
        for (const [v] of IMG_SIZES){ if (v === 'custom') continue; $('img-size').value = v; imgSync(); if (imgProblem()) bad.push(v); }
        report(bad);
        """)
        self.assertEqual(r, [])


class TheDecideCommandSurvivesAnApostrophe(unittest.TestCase):
    """U34: the state went into the curl line inside single quotes as it was typed, so
    "I'm losing sales" ended the shell string halfway."""

    def test_the_pasted_command_sends_the_payload(self):
        r = run(self, r"""
        $('s1-state').value = "I'm losing sales, it's urgent"; s1Curl();
        report({cmd: txt('s1-curl'), payload: s1Payload()});
        """)
        with tempfile.TemporaryDirectory() as d:
            p = subprocess.run(["bash", "-c", "curl(){ printf '%s\\n' \"$@\"; }\ncat(){ echo KEY; }\n" + r["cmd"]],
                               cwd=d, capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        body = p.stdout[p.stdout.index("{"):]
        self.assertEqual(json.loads(body), r["payload"])


class DecideQuestionIdsStayUnique(unittest.TestCase):
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
        $('s1-state').value = 'x';
        await s1Run();
        report({asked: __fetches.filter(f => f.url === '/api/systemone').length, toast: toastText()});
        """)
        self.assertEqual(r["asked"], 0)
        self.assertIn('"q"', r["toast"])


class ABusyLaneLeavesTheLastImageInPlace(unittest.TestCase):
    """U36: the frame of a new request replaced the last image at once, and a 409 (someone
    else generating) left the empty frame there."""

    def test_the_previous_image_comes_back(self):
        r = run(self, r"""
        __fetch = async url => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/image/generate' ? __response(409, {error: 'this lane serves one image at a time'})
          : url === '/api/image' ? __response(200, {available: true, installed: true, progress: {}}) : new Promise(() => {});
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})})});
        imgShow({data: [{b64_json: 'AAAA'}], inference_time_s: 38.2}, 'png');
        $('img-prompt').value = 'a cat'; imgSync();
        await imgRun();
        report({imgs: document.querySelectorAll('#img-screen img').length, meta: txt('img-meta')});
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
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})})});
        IS.mode = 'edit'; IS.refs = Array.from({length: 9}, (_, i) => ({name: 'r' + i, dataUrl: 'data:image/png;base64,AA', w: 1, h: 1}));
        $('img-refsample').click(); await __settle();
        IS.refs.push({name: 'late', dataUrl: 'data:image/png;base64,AA', w: 1, h: 1});
        answer(__response(200, {image: {data: [{b64_json: 'BBBB'}]}})); await __settle(); await __settle();
        report(IS.refs.length);
        """)
        self.assertLessEqual(r, 10)


class ThePhonesSessionCardIsNotRewrittenForNothing(unittest.TestCase):
    """U38: the card's markup was written again every 500 ms, and a tap on iOS that
    straddles a rewrite is lost."""

    MARKUP = ('<html><head><meta name="theme-color" content="#fff"></head>'
              '<body><div id="root"><div>opencode</div></div></body></html>')

    def test_one_write_for_one_list(self):
        r = pagejs.run(self, HELPERS + r"""
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
    references at 20 steps were announced at 26 s and measured at 69.6. Since v1.24.0 the
    estimate is the v0.5.21 runtime's, for both checkpoints (measured 2026-10-10: the base's
    edit 41.9 s and generation 34.3 s at 40 steps, the Turbo's 10.3 and 7.5 at its 8; ten
    references were measured on the runtime before, which was about 5 % slower)."""

    def test_the_measured_edits_and_generations(self):
        r = run(self, r"""
        report([imgEstimate({w: 1024, h: 1024, steps: 40, n: 1, editing: true, refs: 1}),
                imgEstimate({w: 1024, h: 1024, steps: 20, n: 1, editing: true, refs: 10}),
                imgEstimate({w: 1024, h: 1024, steps: 40, n: 1, editing: false}),
                imgEstimate({w: 1024, h: 1024, steps: 8, n: 1, editing: true, refs: 1}),
                imgEstimate({w: 1024, h: 1024, steps: 8, n: 1, editing: false}),
                imgEstimate({w: 2048, h: 2048, steps: 40, n: 1, editing: false}),
                imgEstimate({w: 512, h: 512, steps: 8, n: 1, editing: false})]);
        """)
        for got, measured in zip(r, (41.9, 69.6, 34.3, 10.3, 7.5, 182.8, 1.82)):
            self.assertAlmostEqual(got, measured, delta=measured * 0.1)


class TheUpdateCommandsNameThisCheckout(unittest.TestCase):
    """U40: the update banner and the Agent view's install hint said cd ~/dgx-spark-qwen38
    whatever the checkout; the cockpit knows its own."""

    def test_both_commands(self):
        r = run(self, r"""
        feed({config: Object.assign({}, CONFIG, {repo_dir: '/opt/qwen', terminal_only: {update_stack: 'cd /opt/qwen && ./install.sh'}}),
              update: {installed: 'v1.18.6', latest: 'v1.18.7', behind: true},
              agent: {enabled: false, opencode_found: null, pinned: '1.18.32'}});
        report({banner: txt('banners'), agent: txt('ag-cmd'), settings: txt('st-update')});
        """)
        self.assertIn("cd /opt/qwen", r["banner"])
        self.assertNotIn("~/dgx-spark-qwen38", r["banner"])
        self.assertEqual(r["agent"], "cd /opt/qwen && ./install.sh")
        self.assertEqual(r["settings"], "cd /opt/qwen && ./install.sh")


class ExampleNamesCountRight(unittest.TestCase):
    """U41: the "Twenty options" example had eight."""

    def test_counts(self):
        r = run(self, "report(Object.entries(S1_EXAMPLES).map(([n, e]) => [n, e.questions.map(q => (q.criteria || q.levels || []).length)]));")
        words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "ten": 10, "twenty": 20}
        for name, counts in r:
            m = re.match(r"(\w+) options$", name)
            if m:
                with self.subTest(name=name):
                    self.assertIn(words[m.group(1).lower()], counts)


class TheExampleShowsWhichIsLoaded(unittest.TestCase):
    """U42: the first example stayed highlighted whichever was loaded."""

    def test_the_highlight_moves(self):
        r = run(self, r"""
        const out = {};
        for (const box of ['img-examples', 's1-examples', 'vid-examples']){
          const bs = [...document.querySelectorAll('#' + box + ' button')];
          bs[2].click();
          out[box] = bs.map(b => b.getAttribute('aria-pressed'));
        }
        report(out);
        """)
        for box, pressed in r.items():
            with self.subTest(box=box):
                self.assertEqual(pressed[2], "true")
                self.assertEqual(pressed.count("true"), 1)


class TheModeControlsSayWhichIsChosen(unittest.TestCase):
    """The Video tab's "Text only" and "First + last frames" were two grey buttons: which one
    was on could not be seen (Hasan's screenshot, 2026-09-29). Each mode is a radio in a
    group, and exactly one is checked."""

    def test_one_checked_radio_per_group(self):
        r = run(self, r"""
        const out = {};
        for (const g of ['vid-mode', 'vid-size', 'img-mode']){
          const bs = [...document.querySelectorAll('#' + g + ' button')];
          bs[1].click();
          out[g] = {role: $(g).getAttribute('role'), checked: bs.map(b => b.getAttribute('aria-checked')), roles: bs.map(b => b.getAttribute('role'))};
        }
        report(out);
        """)
        for g, v in r.items():
            with self.subTest(g=g):
                self.assertEqual(v["role"], "radiogroup")
                self.assertEqual(v["checked"], ["false", "true"])
                self.assertEqual(set(v["roles"]), {"radio"})
        rules = {s.strip() for sel, media, _ in css_rules() for s in sel.split(",")}
        self.assertIn('.seg button[aria-checked="true"]', rules, "the chosen one looks chosen")


class LiveRegionsSayWhatChanged(unittest.TestCase):
    """The lane pill and the job strip were live regions whose text changed every second (a
    boot's elapsed time, a job's clock): a screen reader read them again every second. One
    quiet region says a sentence when the lane or the job changes state, and nothing on a
    tick."""

    def test_the_moving_containers_are_not_live(self):
        for anchor in ('id="serving"', 'id="dock"', 'id="minipool"'):
            with self.subTest(anchor=anchor):
                tag = PAGE[PAGE.rindex("<", 0, PAGE.index(anchor)):PAGE.index(">", PAGE.index(anchor))]
                self.assertNotIn("aria-live", tag)

    def test_the_lane_says_its_state_once(self):
        r = run(self, r"""
        const said = [];
        const tick = (state, elapsed) => { feed({lifecycle: life({'qwen38-flash.service': eng(state, {target: 'flash', elapsed})})}); said.push(txt('sayer')); };
        tick('loading-weights', 10); tick('loading-weights', 11); tick('loading-weights', 12); tick('ready', 13); tick('ready', 14);
        report({said, live: $('sayer').getAttribute('aria-live')});
        """)
        self.assertEqual(r["live"], "polite")
        self.assertEqual(len(set(r["said"])), 2, r["said"])
        self.assertIn("ready", r["said"][-1])

    def test_the_job_says_its_start_and_its_end(self):
        r = run(self, r"""
        const job = (status, elapsed, last) => ({id: 'j', action: 'smoke', params: {}, status, started: Date.now() / 1000 - elapsed,
                                                 elapsed, lines: [last]});
        const said = [];
        [1, 2, 3].forEach(s => { feed({job: {current: job('running', s, 'line ' + s), recent: []}}); said.push(txt('sayer')); });
        feed({job: {current: null, recent: [Object.assign(job('done', 4, 'end'), {ended: Date.now() / 1000, rc: 0})]}}); said.push(txt('sayer'));
        report({said});
        """)
        self.assertEqual(len(set(r["said"])), 2, r["said"])


class AVideoLaneIsNotNamedAfterItsOwnModel(unittest.TestCase):
    """QA of the live page (2026-09-28): the Engines row read "qwen38-video · MiniMax-H3 H3".
    The video unit is named after the model it serves, and the target short said it again."""

    def test_the_label_says_the_model_once(self):
        r = run(self, r"""
        feed({lifecycle: life({'qwen38-video.service': eng('ready', {target: 'video'}), 'qwen38-image.service': eng('ready', {target: 'image'})})});
        report([laneLabel('qwen38-video.service'), laneLabel('qwen38-image.service')]);
        """)
        self.assertEqual(r, ["MiniMax-H3", "Qwen-Image 2.1"])


class LoadingALaneIsOneJourney(unittest.TestCase):
    """Loading a lane took three buttons in an order the page explained in a list (switch,
    stop the serving lane, start this one), and the list stayed on screen after its steps
    were done (seen live, 2026-09-29). Load is one sheet that names each step and its exact
    command, and runs them one job after the other."""

    def test_the_steps_are_the_ones_still_to_do(self):
        r = run(self, r"""
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'}), 'qwen38-video.service': eng('stopped', {target: 'video'})})});
        askJourney('video');
        const steps = [...$('sh-journey').children].map(li => li.textContent);
        closeSheet();
        const video = JSON.parse(JSON.stringify(UNITS)); video.units['qwen38-video.service'].enabled = 'enabled'; video.units['qwen38-sglang.service'].enabled = 'disabled';
        feed({units: video, lifecycle: life({'qwen38-video.service': eng('stopped', {target: 'video'})})});
        askJourney('video');
        const after = [...$('sh-journey').children].map(li => li.textContent);
        report({steps, after});
        """)
        self.assertEqual(len(r["steps"]), 3, r["steps"])
        self.assertIn("switch-model.sh video", r["steps"][0])
        self.assertIn("systemctl stop qwen38-flash.service", r["steps"][1])
        self.assertIn("systemctl start qwen38-video.service", r["steps"][2])
        self.assertEqual(len(r["after"]), 1, "switched and nothing serving: one step left")
        self.assertIn("start qwen38-video.service", r["after"][0])

    def test_it_runs_each_step_after_the_last_one_finished(self):
        r = run(self, r"""
        const jobs = []; let n = 0;
        __fetch = async (url, init) => {
          if (url === '/api/csrf') return __response(200, {token: 't'});
          if (url === '/api/action'){ const b = JSON.parse(init.body); jobs.push(b); return __response(202, {job: 'j' + (++n)}); }
          return new Promise(() => {});
        };
        feed({config: CONFIG, units: UNITS, lifecycle: life({'qwen38-flash.service': eng('ready', {target: 'flash'}), 'qwen38-video.service': eng('stopped', {target: 'video'})})});
        askJourney('video'); $('sh-go').click(); await __settle();
        const afterFirst = jobs.length;
        feed({job: {current: null, recent: [{id: 'j1', action: 'switch', params: {target: 'video'}, status: 'done', rc: 0, started: 1, ended: 2, elapsed: 1}]}});
        await __advance(600); await __settle();
        const afterSecond = jobs.length;
        feed({job: {current: null, recent: [{id: 'j2', action: 'unit', params: {verb: 'stop', unit: 'qwen38-flash.service'}, status: 'done', rc: 0, started: 3, ended: 4, elapsed: 1}]},
              lifecycle: life({'qwen38-flash.service': eng('stopped', {target: 'flash'}), 'qwen38-video.service': eng('stopped', {target: 'video'})})});
        await __advance(1200); await __settle();
        report({afterFirst, afterSecond, names: jobs.map(j => j.name + ':' + (j.params.verb || j.params.target))});
        """)
        self.assertEqual(r["afterFirst"], 1, "the second step waits for the first job to finish")
        self.assertEqual(r["afterSecond"], 2)
        self.assertEqual(r["names"], ["switch:video", "unit:stop", "unit:start"])


if __name__ == "__main__":
    unittest.main()
