"""The cockpit says which release it is.

VERSION was a constant, "1.1.2", from v1.7.2 on: the badge, the Server header and the
User-Agent of the update check all named a release two years of changes old (found in
review, 2026-09-24). It is read from CHANGELOG.md's first heading now."""
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve()
DASH = HERE.parents[1]


class TheVersion(unittest.TestCase):
    def test_it_is_the_changelogs_newest_release(self):
        tmp = Path(tempfile.mkdtemp(prefix="cockpit-ver-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "api-key").write_text("k\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(tmp), COCKPIT_REPO_DIR=str(DASH.parent),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0")
        sys.path.insert(0, str(DASH))
        spec = importlib.util.spec_from_file_location("cockpit_version", DASH / "cockpit.py")
        cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cp)
        first = re.search(r"^## v(\d+\.\d+(?:\.\d+)?)\b", (DASH.parent / "CHANGELOG.md").read_text(), re.M).group(1)
        self.assertEqual(cp.VERSION, first)
        self.assertEqual(cp.Handler.server_version, "SparkCockpit/" + first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
