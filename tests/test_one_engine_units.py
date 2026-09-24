#!/usr/bin/env python3
"""Every lane's unit tells systemd that it runs alone, and in what order.

Two engines on the unified pool is the livelock this box power-cycles out of. The image
unit said so with Conflicts= and no After=, and Conflicts= orders nothing: its start and
the text lane's stop ran side by side (5 to 60 s in the reference box's journal on
2026-09-23). The two text units said nothing at all to each other, so the 27B and the
flash lane could run together, or both start at boot if both were enabled.

With Conflicts= and After= on every other lane, measured on the box's systemd 255 with
throwaway units (2026-09-24): each switch finished the stop before the start, in all four
directions tried, with no ordering-cycle warning, and a transaction asking for two lanes
at once started one of them only.
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
LANES = {
    "qwen38-sglang.service": ("qwen38-sglang.service.template", "qwen38-sglang-1m.service.template"),
    "qwen38-flash.service": ("qwen38-flash.service.template",),
    "qwen38-image.service": ("qwen38-image.service.template",),
}
ALL = set(LANES) | {"qwen38-llamacpp.service"}


def unit_list(template, key):
    unit = (REPO / template).read_text().split("[Service]")[0]
    return {u for line in re.findall(rf"(?m)^{key}=(.*)$", unit) for u in line.split()}


class EveryLaneRunsAlone(unittest.TestCase):
    def test_each_lane_conflicts_with_every_other_lane(self):
        for lane, templates in LANES.items():
            for t in templates:
                with self.subTest(template=t):
                    self.assertEqual(unit_list(t, "Conflicts"), ALL - {lane})

    def test_each_lane_is_ordered_after_what_it_conflicts_with(self):
        for templates in LANES.values():
            for t in templates:
                with self.subTest(template=t):
                    self.assertEqual(unit_list(t, "Conflicts") - unit_list(t, "After"), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
