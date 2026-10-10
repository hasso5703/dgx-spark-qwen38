#!/usr/bin/env python3
"""The image lane's Turbo target on the page: a variant of the lane like flash-nvda of the
flash, loaded the same way, and an Image view that stops offering a step count the
checkpoint ignores (its eight-step sigma grid is in its model_index.json; the cookbook says
to leave num_inference_steps out). Same harness as test_page_behaviour.py."""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_page_behaviour import run  # noqa: E402

IMG = "'qwen38-image.service'"


def lanes(image_state, image_target, text_state="stopped"):
    return (f"feed({{units: UNITS, lifecycle: life({{'qwen38-sglang.service': eng('{text_state}'), "
            f"'qwen38-flash.service': eng('stopped', {{target: 'flash'}}), "
            f"{IMG}: eng('{image_state}', {{target: '{image_target}'}})}})}});\n")


class TheTargetIsALaneVariant(unittest.TestCase):
    def test_it_belongs_to_the_image_lane_and_has_a_name_of_its_own(self):
        r = run(self, r"""
        report({unit: TARGET_UNIT('image-turbo'), base: TARGET_UNIT('image'), list: LANE_TARGETS[IMAGE_UNIT],
                name: TARGET_NAME['image-turbo'], short: TARGET_SHORT['image-turbo'], note: TARGET_NOTE['image-turbo'] || ''});
        """)
        self.assertEqual(r["unit"], "qwen38-image.service")
        self.assertEqual(r["base"], "qwen38-image.service")
        self.assertEqual(r["list"], ["image", "image-turbo"])
        self.assertEqual(r["name"], "Qwen-Image 2.1 Turbo")
        self.assertEqual(r["short"], "Turbo")
        self.assertIn("eight", r["note"])

    def test_the_switch_phrase_names_it(self):
        r = run(self, r"""
        report([actionPhrase('switch', {target: 'image'}), actionPhrase('switch', {target: 'image-turbo'})]);
        """)
        self.assertNotEqual(r[0], r[1])
        self.assertIn("Turbo", r[1])


class LoadingIt(unittest.TestCase):
    """The journey is the lanes' own: point the unit at the checkpoint, then restart it when
    it serves the other one, or stop the serving lane and start it."""

    def test_from_the_base_serving_it_is_a_switch_then_a_restart(self):
        r = run(self, lanes("ready", "image") + r"""
        report(laneJourneySteps('image-turbo').map(s => s.name + (s.params.verb ? ':' + s.params.verb : '')));
        """)
        self.assertEqual(r, ["switch", "unit:restart"])

    def test_from_a_text_lane_it_is_a_switch_a_stop_and_a_start(self):
        r = run(self, lanes("stopped", "image", text_state="ready") + r"""
        report(laneJourneySteps('image-turbo').map(s => s.name + (s.params.verb ? ':' + s.params.verb + ' ' + s.params.unit : '')));
        """)
        self.assertEqual(r, ["switch", "unit:stop qwen38-sglang.service", "unit:start qwen38-image.service"])

    def test_already_serving_it_is_nothing_to_do(self):
        r = run(self, r"""
        feed({units: {units: Object.assign({}, UNITS.units, {'qwen38-sglang.service': {active: 'inactive', enabled: 'disabled'},
                                                             'qwen38-image.service': {active: 'active', enabled: 'enabled'}})},
              lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image-turbo'})})});
        report(laneJourneySteps('image-turbo').length);
        """)
        self.assertEqual(r, 0)


class TheImageViewUnderTheTurbo(unittest.TestCase):
    def test_the_request_leaves_the_step_count_out_and_the_slider_says_why(self):
        r = run(self, lanes("ready", "image-turbo") + r"""
        $('img-prompt').value = 'a cat'; $('img-steps').value = '40'; imgSync();
        report({payload: imgPayload(), steps: imgFormRequest().steps, disabled: $('img-steps').disabled,
                shown: txt('img-steps-o'), hint: txt('img-steps-hint'), problem: imgProblem()});
        """)
        self.assertNotIn("num_inference_steps", r["payload"])
        self.assertEqual(r["steps"], 8)
        self.assertTrue(r["disabled"])
        self.assertEqual(r["shown"], "8")
        self.assertIn("fixed at 8", r["hint"])
        self.assertEqual(r["problem"], "")

    def test_the_estimate_counts_eight_steps(self):
        r = run(self, lanes("ready", "image-turbo") + r"""
        $('img-steps').value = '40'; const turbo = imgEstimate();
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})})});
        const base = imgEstimate();
        report({turbo, base});
        """)
        self.assertLess(r["turbo"], r["base"] / 4)

    def test_the_thumb_says_eight_and_the_base_gets_its_setting_back(self):
        """Seen on the reference box under the Turbo: the slider's thumb on the base's 40,
        beside the 8 it printed."""
        r = run(self, lanes("ready", "image") + r"""
        $('img-steps').value = '33'; imgSync();
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image-turbo'})})});
        imgSync(); const turbo = $('img-steps').value;
        feed({lifecycle: life({'qwen38-sglang.service': eng('stopped'), 'qwen38-image.service': eng('ready', {target: 'image'})})});
        imgSync(); report({turbo, base: $('img-steps').value, payload: imgPayload().num_inference_steps});
        """)
        self.assertEqual(r["turbo"], "8")
        self.assertEqual(r["base"], "33")
        self.assertEqual(r["payload"], 33)

    def test_a_reset_under_the_turbo_keeps_the_thumb_on_eight(self):
        r = run(self, lanes("ready", "image-turbo") + r"""
        $('img-reset').click(); report($('img-steps').value);
        """)
        self.assertEqual(r, "8")

    def test_the_base_keeps_its_slider_and_its_setting(self):
        r = run(self, lanes("ready", "image") + r"""
        $('img-prompt').value = 'a cat'; $('img-steps').value = '33'; imgSync();
        report({payload: imgPayload(), disabled: $('img-steps').disabled, shown: txt('img-steps-o'), hint: txt('img-steps-hint')});
        """)
        self.assertEqual(r["payload"]["num_inference_steps"], 33)
        self.assertFalse(r["disabled"])
        self.assertEqual(r["shown"], "33")
        self.assertEqual(r["hint"], "model default 40")

    def test_the_load_button_loads_the_checkpoint_the_unit_names(self):
        r = run(self, lanes("stopped", "image-turbo", text_state="ready") + r"""
        const asked = []; askJourney = t => asked.push(t);
        imgRenderLane();
        const b = [...document.querySelectorAll('button')].find(x => x.textContent === 'Load Qwen-Image');
        if (b) b.click();
        report(asked);
        """)
        self.assertEqual(r, ["image-turbo"])


class WhatTheEngineServes(unittest.TestCase):
    """The unit's file says what the lane's next start loads; the engine's own /model_info,
    read by the cockpit once per life of the unit (served_target), says what it serves. A
    switch from a terminal rewrites the unit under a serving engine: the page sent the
    Turbo's request to the base and found no restart owed (found in review, 2026-10-10)."""

    def served(self, unit_target, served_target, body):
        return run(self, r"""
        feed({units: {units: Object.assign({}, UNITS.units, {'qwen38-sglang.service': {active: 'inactive', enabled: 'disabled'},
                                                             'qwen38-image.service': {active: 'active', enabled: 'enabled'}})},
              lifecycle: life({'qwen38-sglang.service': eng('stopped'),
                               'qwen38-image.service': eng('ready', {target: '%s', served_target: '%s'})})});
        """ % (unit_target, served_target) + body)

    def test_a_unit_switched_under_the_engine_owes_its_restart(self):
        r = self.served("image-turbo", "image", r"""
        report({steps: laneJourneySteps('image-turbo').map(s => s.name + (s.params.verb ? ':' + s.params.verb : '')),
                banners: txt('banners'), turbo: imgTurbo()});
        """)
        self.assertEqual(r["steps"], ["unit:restart"])
        self.assertIn("Qwen-Image 2.1 is serving, and the lane's next start loads Qwen-Image 2.1 Turbo.", r["banners"])
        self.assertFalse(r["turbo"], "the page took the unit's word over the engine's")

    def test_the_page_follows_the_engine(self):
        r = self.served("image", "image-turbo", r"""
        $('img-prompt').value = 'a cat'; $('img-steps').value = '40'; imgSync();
        report({payload: imgPayload(), turbo: imgTurbo(), steps: laneJourneySteps('image').map(s => s.name + (s.params.verb ? ':' + s.params.verb : ''))});
        """)
        self.assertTrue(r["turbo"])
        self.assertNotIn("num_inference_steps", r["payload"])
        self.assertEqual(r["steps"], ["unit:restart"])

    def test_serving_what_the_unit_names_owes_nothing(self):
        r = self.served("image-turbo", "image-turbo", r"""
        report({n: laneJourneySteps('image-turbo').length, banners: txt('banners')});
        """)
        self.assertEqual(r["n"], 0)
        self.assertNotIn("next start loads", r["banners"])

    def test_pointing_the_unit_at_what_the_engine_serves_restarts_nothing(self):
        """The terminal switch the other way: the engine serves the Turbo, its unit names the
        base. Loading the Turbo points the unit back at it, and a restart would only end a
        generation in flight."""
        r = self.served("image", "image-turbo", r"""
        report(laneJourneySteps('image-turbo').map(s => s.name + (s.params.verb ? ':' + s.params.verb : '')));
        """)
        self.assertEqual(r, ["switch"])

    def test_restarting_it_on_the_other_checkpoint_says_a_generation_is_lost(self):
        r = self.served("image", "image", r"""
        askJourney('image-turbo');
        report({warns: txt('sh-warns'), steps: [...$('sh-journey').children].map(li => li.textContent)});
        """)
        self.assertIn("A generation in flight on Qwen-Image", r["warns"])
        self.assertTrue(any("A generation in flight is lost." in s for s in r["steps"]), r["steps"])


class TheSampleAndTheResetUnderTheTurbo(unittest.TestCase):
    def test_the_sample_leaves_the_steps_to_the_grid_and_the_reset_says_eight(self):
        r = run(self, lanes("ready", "image-turbo") + r"""
        let sent = null;
        __fetch = async (url, init) => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/image/generate' ? (sent = JSON.parse(init.body), __response(200, {data: [{b64_json: 'AAAA'}]}))
          : url === '/api/image' ? __response(200, {available: true, installed: true, progress: {}}) : new Promise(() => {});
        await imgSample();
        $('img-reset').click();
        report({sent, toast: toastText()});
        """)
        self.assertIsNotNone(r["sent"])
        self.assertNotIn("num_inference_steps", r["sent"])
        self.assertIn("the Turbo’s 8 steps", r["toast"])
        self.assertNotIn("40 steps", r["toast"])

    def test_the_base_sample_keeps_its_twenty_steps(self):
        r = run(self, lanes("ready", "image") + r"""
        let sent = null;
        __fetch = async (url, init) => url === '/api/csrf' ? __response(200, {token: 't'})
          : url === '/api/image/generate' ? (sent = JSON.parse(init.body), __response(200, {data: [{b64_json: 'AAAA'}]}))
          : url === '/api/image' ? __response(200, {available: true, installed: true, progress: {}}) : new Promise(() => {});
        await imgSample();
        $('img-reset').click();
        report({sent, toast: toastText()});
        """)
        self.assertEqual(r["sent"]["num_inference_steps"], 20)
        self.assertIn("40 steps", r["toast"])


class WhatThePageSaysOfEach(unittest.TestCase):
    def test_the_now_card_times_the_checkpoint_it_loads(self):
        r = run(self, lanes("stopped", "image-turbo", text_state="ready") + r"""
        renderRack(); const turbo = txt('view-now');
        feed({lifecycle: life({'qwen38-sglang.service': eng('ready'), 'qwen38-image.service': eng('stopped', {target: 'image'})})});
        renderRack(); report({turbo, base: txt('view-now')});
        """)
        self.assertIn("1024 image about 7.5 s", r["turbo"])
        self.assertIn("1024 image about 34 s", r["base"])
        self.assertNotIn("38 s", r["turbo"] + r["base"])

    def test_the_switch_says_what_it_rewrites_for_each_lane(self):
        r = run(self, r"""
        report(['stock', 'image', 'image-turbo', 'video'].map(t => EXPLAIN.switch({target: t}).split('\n\n').pop()));
        """)
        self.assertIn("rewrites its unit, the proxy ceiling and the opencode default model", r[0])
        for x in r[1:3]:
            self.assertIn("points its unit at this checkpoint", x)
            self.assertNotIn("proxy", x)
        self.assertIn("changes nothing else", r[3])


if __name__ == "__main__":
    unittest.main()
