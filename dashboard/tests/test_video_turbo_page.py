#!/usr/bin/env python3
"""The Video view's Turbo switch: offered when the lane's status says the adapter is on the
box, its nine steps fixed when it is on, its cost counted as the server counts it, and the
call it sends marked turbo for the cockpit to put the adapter on. Same harness as
test_page_behaviour.py."""
import json
import pathlib
import shlex
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_page_behaviour import run  # noqa: E402

LANE = r"""
__fetch = async (url, init) => url === '/api/csrf' ? __response(200, {token: 't'})
  : url === '/api/video' ? __response(200, {installed: true, available: true, state: 'ready', progress: {}, run: {}, turbo: %s})
  : url === '/api/video/generate' ? (window.__sent = JSON.parse(init.body), __response(200, {video_id: 'v1', seconds: 120}))
  : new Promise(() => {});
feed({lifecycle: life({'qwen38-video.service': eng('ready', {target: 'video'})})});
await vidLane(); $('vid-prompt').value = 'a fox'; vidSync();
"""
pick = lambda which: f"[...document.querySelectorAll('#vid-turbo button')].find(b => b.dataset.turbo === '{which}').click();\n"


class TheTurboSwitch(unittest.TestCase):
    def test_offered_only_when_the_adapter_is_on_the_box(self):
        r = run(self, LANE % "false" + r"""
        const b = [...document.querySelectorAll('#vid-turbo button')].find(x => x.dataset.turbo === '1');
        b.click();
        report({disabled: b.disabled, turbo: vidTurbo(), steps: vidSteps(), hint: txt('vid-turbo-hint')});
        """)
        self.assertTrue(r["disabled"])
        self.assertFalse(r["turbo"])
        self.assertEqual(r["steps"], 50)
        self.assertIn("./install-video.sh fetches it", r["hint"])

    def test_a_lane_that_must_restart_first_says_so(self):
        """The status names why the Turbo waits (cockpit.py video_runtime_stale): the page
        says that, not that the adapter is missing."""
        lane = LANE.replace("turbo: %s}", "turbo: false, turbo_why: 'the Turbo waits for its next start'}")
        r = run(self, lane + r"""
        const b = [...document.querySelectorAll('#vid-turbo button')].find(x => x.dataset.turbo === '1');
        b.click();
        report({disabled: b.disabled, turbo: vidTurbo(), hint: txt('vid-turbo-hint')});
        """)
        self.assertTrue(r["disabled"])
        self.assertFalse(r["turbo"])
        self.assertEqual(r["hint"], "the Turbo waits for its next start")

    def test_on_it_fixes_nine_steps_and_says_what_it_costs(self):
        r = run(self, LANE % "true" + pick("1") + r"""
        report({turbo: vidTurbo(), steps: vidSteps(), slider: $('vid-steps').disabled, hint: txt('vid-steps-hint'),
                cost: txt('vid-cost-k'), basis: txt('vid-cost-b'), est: vidEtaSecs(4, '864x480', 9, true),
                curl: txt('vid-curl')});
        """)
        self.assertTrue(r["turbo"])
        self.assertEqual(r["steps"], 9)
        self.assertTrue(r["slider"])
        self.assertIn("fixed at 9", r["hint"])
        self.assertIn("9 steps with the Turbo", r["cost"])
        self.assertIn("2:06 to 2:17", r["basis"])
        self.assertAlmostEqual(r["est"], 3.85 * 9 * 4)
        # from a terminal the adapter goes on before the call and off after it
        self.assertLess(r["curl"].index("/v1/set_lora"), r["curl"].index("/v1/videos"))
        self.assertLess(r["curl"].index("/v1/videos"), r["curl"].index("/v1/unmerge_lora_weights"))

    def test_the_copied_command_names_the_adapter_file_the_status_gives(self):
        """Read back by a shell the way it would run it: the adapter's path, a quote and a space
        in it, reaches set_lora as the JSON the lane takes."""
        lane = LANE.replace("turbo: %s}", 'turbo: true, turbo_path: "/hf/hub/it\'s here.safetensors"}')
        r = run(self, lane + pick("1") + r"""
        report(txt('vid-curl'));
        """)
        first = r[:r.index("\ncurl -s ", 1)].replace("\\\n", " ")     # the set_lora command, joined
        argv = shlex.split(first)
        self.assertTrue(any(a.endswith("/v1/set_lora") for a in argv), argv)
        body = json.loads(argv[argv.index("-d") + 1])
        self.assertEqual(body["lora_path"], "/hf/hub/it's here.safetensors")
        self.assertEqual(body["merge_mode"], "dynamic")

    def test_the_call_is_marked_turbo_and_the_base_one_is_not(self):
        r = run(self, LANE % "true" + pick("1") + r"""
        await vidRun(); const turbo = window.__sent;
        """ + pick("0") + r"""
        await vidRun(); report({turbo, base: window.__sent});
        """)
        self.assertIs(r["turbo"]["turbo"], True)
        self.assertEqual(r["turbo"]["num_inference_steps"], 9)
        self.assertNotIn("turbo", r["base"])
        self.assertEqual(r["base"]["num_inference_steps"], 50)

    def test_measured_at_480p_only_so_far(self):
        r = run(self, LANE % "true" + pick("1") + r"""
        [...document.querySelectorAll('#vid-size button')].find(b => b.dataset.size === '1280x720').click();
        report({problem: vidProblem(), run: $('vid-run').disabled});
        """)
        self.assertIn("480p only", r["problem"])
        self.assertTrue(r["run"])

    def test_measured_at_four_seconds_only_so_far(self):
        """The dynamic adapter adds to the memory a video takes (4 s at 480P: 21 to 25 GiB
        against 9 for the base), and the base alone peaks at 76.5 GiB for 15 s."""
        r = run(self, LANE % "true" + r"""
        $('vid-secs').value = '10'; vidSync();
        """ + pick("1") + r"""
        report({max: $('vid-secs').max, secs: vidSecs(), ends: $('vid-secs').parentElement.querySelector('.ends').textContent,
                problem: vidProblem()});
        """)
        self.assertEqual(r["max"], "4")
        self.assertEqual(r["secs"], 4, "the slider kept a length the Turbo does not take")
        self.assertIn("the Turbo’s measured length", r["ends"])
        self.assertEqual(r["problem"], "")

    def test_the_thumb_says_nine_and_the_base_gets_its_setting_back(self):
        """The image view's thumb stayed on the base's setting beside the 8 it printed (seen on
        the reference box); this one does not."""
        r = run(self, LANE % "true" + r"""
        $('vid-steps').value = '33'; vidSync();
        """ + pick("1") + r"""
        const turbo = {value: $('vid-steps').value, fill: $('vid-steps').style.getPropertyValue('--pct')};
        """ + pick("0") + r"""
        report({turbo, base: $('vid-steps').value, shown: txt('vid-steps-o'), body: vidWireBody().num_inference_steps});
        """)
        self.assertEqual(r["turbo"]["value"], "9")
        self.assertEqual(r["turbo"]["fill"], "8.1%")
        self.assertEqual(r["base"], "33")
        self.assertEqual(r["shown"], "33")
        self.assertEqual(r["body"], 33)

    def test_a_reset_under_the_turbo_brings_back_the_default_not_the_kept_setting(self):
        r = run(self, LANE % "true" + r"""
        $('vid-steps').value = '33'; vidSync();
        """ + pick("1") + r"""
        $('vid-reset').click(); report({value: $('vid-steps').value, turbo: vidTurbo()});
        """)
        self.assertEqual(r, {"value": "50", "turbo": False})

    def test_reset_goes_back_to_the_base(self):
        r = run(self, LANE % "true" + pick("1") + r"""
        $('vid-reset').click();
        report({turbo: vidTurbo(), slider: $('vid-steps').disabled, toast: toastText()});
        """)
        self.assertFalse(r["turbo"])
        self.assertFalse(r["slider"])
        self.assertIn("the base", r["toast"])


if __name__ == "__main__":
    unittest.main()
