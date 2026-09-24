#!/usr/bin/env python3
"""build-token-map.py's sizes are the ones its own shape gives.

Its docstring put the flash lane's `[248320, 2560]` BF16 head at 1.21 GiB and the saving at
2.7 GiB per engine step; the shape gives 1.18 GiB and 2.6 (found in review, 2026-09-24).
The figures are recomputed here from the shape and the row count the docstring states.
"""
import pathlib
import re
import unittest

TEXT = " ".join((pathlib.Path(__file__).resolve().parents[1] / "build-token-map.py").read_text().split())


class TheStatedSizes(unittest.TestCase):
    def test_they_follow_from_the_shape(self):
        rows, dim = map(int, re.search(r"`\[(\d+), (\d+)\]` in BF16", TEXT).groups())
        head = float(re.search(r"in BF16 is ([\d.]+) GiB", TEXT).group(1))
        kept = int(re.search(r"At ([\d,]+) rows", TEXT).group(1).replace(",", ""))
        each, instead = map(float, re.search(r"cost ([\d.]+) GiB each instead of ([\d.]+)", TEXT).groups())
        saved = float(re.search(r"([\d.]+) GiB removed from every engine step", TEXT).group(1))
        gib = 2 ** 30
        self.assertEqual(head, round(rows * dim * 2 / gib, 2))
        self.assertEqual(instead, head)
        self.assertEqual(each, round(kept * dim * 2 / gib, 2))
        self.assertEqual(saved, round(3 * (rows - kept) * dim * 2 / gib, 1))


if __name__ == "__main__":
    unittest.main()
