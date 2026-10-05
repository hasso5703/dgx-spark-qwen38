#!/usr/bin/env python3
"""The hints the scripts print name a standard target first.

The project advises the standard checkpoints: the first target a hint names is a standard
one, and the abliterated targets stay named, after every standard one, so that they are
known without ever being the default. Until v1.22.7 the "Switch back" line of
switch-model.sh named `uncensored` first after every switch to stock, and the closing line
of install.sh on a box that boots images or video named only the abliterated target a text
lane was installed with. These run the lines as written.
"""
import pathlib
import re
import subprocess
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
SWITCH = (REPO / "switch-model.sh").read_text()
INSTALL = (REPO / "install.sh").read_text()
TARGETS = ("stock", "uncensored", "fp8", "uncensored-fp8", "flash", "flash-nvda", "flash-uncensored")
ABLITERATED = ("uncensored", "uncensored-fp8", "flash-uncensored")


def block(text, start, end):
    """text from the line holding `start` to the end of the first line holding `end` after it."""
    s = text.rindex("\n", 0, text.index(start)) + 1
    return text[s:text.index("\n", text.index(end, s)) + 1]


def run(lines, choice, var):
    r = subprocess.run(["bash", "-c", f"set -euo pipefail\n{var}={choice}\n{lines}"],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError(f"{choice}: exit {r.returncode}\n{r.stdout}{r.stderr}")
    return r.stdout


def named_targets(out):
    """The targets a hint names as a switch, in the order it names them."""
    return re.findall(r"(?<![\w-])(stock|uncensored-fp8|uncensored|fp8|flash-nvda|flash-uncensored|flash|image|video)(?![\w-])", out)


class TheSwitchBackLine(unittest.TestCase):
    LINES = block(SWITCH, 'case "$CHOICE" in\n  stock)      echo "Switch back:', "esac")

    def test_every_line_names_a_standard_target_first_and_the_abliterated_ones_last(self):
        for choice in TARGETS:
            with self.subTest(choice):
                out = run(self.LINES, choice, "CHOICE")
                self.assertTrue(out.startswith("Switch back:"), out)
                named = named_targets(out)
                std = [i for i, t in enumerate(named) if t not in ABLITERATED]
                abl = [i for i, t in enumerate(named) if t in ABLITERATED]
                self.assertTrue(std and std[0] == 0, out)               # a standard target first
                self.assertTrue(not abl or min(abl) > max(std), out)    # the abliterated ones last
                self.assertNotIn(choice, named, "it names the target just switched to")

    def test_the_abliterated_targets_stay_named(self):
        named = set()
        for choice in TARGETS:
            named.update(named_targets(run(self.LINES, choice, "CHOICE")))
        self.assertTrue(set(ABLITERATED) <= named, named)

    def test_after_stock_it_names_flash_first_and_uncensored_last(self):
        self.assertEqual(named_targets(run(self.LINES, "stock", "CHOICE")),
                         ["flash", "fp8", "image", "video", "uncensored"])


class TheInstallersLineOnABoxThatBootsImagesOrVideo(unittest.TestCase):
    LINES = block(INSTALL, '  case "$MODEL_CHOICE" in\n    custom)', "  esac")

    def test_an_abliterated_lane_names_its_standard_target_first(self):
        for choice, standard in (("uncensored", "stock"), ("uncensored-fp8", "fp8"),
                                 ("flash-uncensored", "flash")):
            with self.subTest(choice):
                out = run(self.LINES, choice, "MODEL_CHOICE")
                self.assertEqual(named_targets(out), [standard, choice], out)
                self.assertNotIn("Load on that lane", out)   # the cockpit would load it again
                self.assertIn("a first switch downloads about", out)   # 21 to 126 GB

    def test_a_standard_lane_keeps_its_load_and_its_switch(self):
        for choice in ("stock", "fp8", "flash", "flash-nvda"):
            with self.subTest(choice):
                out = run(self.LINES, choice, "MODEL_CHOICE")
                self.assertIn("Load on that lane", out)
                self.assertEqual(named_targets(out), [choice], out)

    def test_a_custom_checkpoint_keeps_its_installer_line(self):
        out = run(self.LINES, "custom", "MODEL_CHOICE")
        self.assertIn("MODEL_CHOICE=<target> ./install.sh", out)
        self.assertNotIn("switch-model.sh", out)   # it rejects custom


class TheOtherTextLanesRun(unittest.TestCase):
    def test_its_last_line_names_the_target_and_says_no_load(self):
        # the serving run's summary that follows says the way back, with a standard target
        line = next(ln for ln in INSTALL.splitlines() if 'step "Done: installed as $UNIT_NAME ($MODEL_CHOICE)' in ln)
        self.assertNotIn("Load", line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
