#!/usr/bin/env python3
"""The facts the cockpit server hands its page, where the page used to guess.

Each class is one defect found in the review of v1.18.6 (2026-09-24): the page promised a
restart nobody would make, named a lane that was not serving, showed a stale probe result
as current, and sent people to journals it did not offer. The module is imported with its
environment pointed at a throwaway box, and nothing here reaches the network.
"""
import html.parser
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

# every module hands the next one the environment it found (test_env_isolation.py)
ENV_BEFORE = dict(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)


HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
REPO = HERE.parents[2]


def load_cockpit(config_dir: Path, **env):
    os.environ.update({"COCKPIT_DRY_RUN": "1", "COCKPIT_CONFIG_DIR": str(config_dir),
                       "COCKPIT_REPO_DIR": str(REPO), "COCKPIT_PORT": "0", "COCKPIT_AGENT_PORT": "0",
                       "COCKPIT_AUTOHEAL": "0", **env})
    sys.path.insert(0, str(DASH))
    spec = importlib.util.spec_from_file_location("cockpit_facts_under_test", DASH / "cockpit.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-facts-"))
        (cls.tmp / "api-key").write_text("test-key-not-a-real-one\n")
        cls.ck = load_cockpit(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def states(self, **by_unit):
        with self.ck.LIFE_LOCK:
            self.ck.LIFE["states"] = {f"qwen38-{k}.service": v for k, v in by_unit.items()}


class ThePageKnowsWhetherTheAutohealIsArmed(Base):
    """The page said a wedged engine would be restarted by the autoheal belt, which is off
    unless COCKPIT_AUTOHEAL=1. Whether it is armed, and its grace, travel with the config."""

    def test_off_by_default(self):
        cfg = self.ck.STATE["config"]["data"]
        self.assertIs(cfg["autoheal"], False)
        self.assertEqual(cfg["autoheal_grace_s"], self.ck.AUTOHEAL_GRACE)

    def test_armed(self):
        tmp = Path(tempfile.mkdtemp(prefix="cockpit-facts-armed-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "api-key").write_text("k\n")
        try:
            ck = load_cockpit(tmp, COCKPIT_AUTOHEAL="1", COCKPIT_AUTOHEAL_GRACE="120")
        finally:
            os.environ["COCKPIT_AUTOHEAL"] = "0"
            os.environ.pop("COCKPIT_AUTOHEAL_GRACE", None)
        cfg = ck.STATE["config"]["data"]
        self.assertEqual((cfg["autoheal"], cfg["autoheal_grace_s"]), (True, 120.0))


class ASkippedProbeSaysWhy(Base):
    """The generation canary skips for five reasons and the page printed one of them,
    "engine busy", for all five, next to the last success, however old."""

    def setUp(self):
        self.saved = (self.ck.DRY_RUN, dict(self.ck.LAST_PROGRESS))
        self.addCleanup(self.restore)
        # every case below must skip: a probe that got through would be a generation on
        # whatever engine answers :30000, so the network is shut rather than trusted
        orig = self.ck.urllib.request.urlopen
        self.addCleanup(setattr, self.ck.urllib.request, "urlopen", orig)

        def never(req, timeout=None):
            raise AssertionError(f"the probe reached {req.full_url}")

        self.ck.urllib.request.urlopen = never
        self.ck.DRY_RUN = False
        self.ck.LAST_PROGRESS["ts"] = None
        with self.ck.STATE_LOCK:
            self.ck.STATE["engine_fast"] = {"data": {"load": [{"num_reqs": 0, "num_waiting_reqs": 0}]}, "ts": 0}

    def restore(self):
        self.ck.DRY_RUN = self.saved[0]
        self.ck.LAST_PROGRESS.clear()
        self.ck.LAST_PROGRESS.update(self.saved[1])

    def test_no_text_engine(self):
        self.states(sglang="stopped", image="ready")
        out = self.ck.collect_canary()
        self.assertTrue(out["skipped"])
        self.assertEqual(out["why"], "no text engine is ready")

    def test_a_client_is_active(self):
        self.states(sglang="ready")
        self.ck.LAST_PROGRESS["ts"] = self.ck.time.time()
        out = self.ck.collect_canary()
        self.assertEqual(out["why"], "a client was active in the last minute")

    def test_requests_are_running(self):
        self.states(sglang="ready")
        with self.ck.STATE_LOCK:
            self.ck.STATE["engine_fast"] = {"data": {"load": [{"num_reqs": 1}]}, "ts": 0}
        self.assertEqual(self.ck.collect_canary()["why"], "requests are running")

    def test_a_dry_run(self):
        self.ck.DRY_RUN = True
        self.states(sglang="ready")
        self.assertEqual(self.ck.collect_canary()["why"], "this cockpit is a dry run")


class SystemOneIsAnsweredByATextLane(Base):
    """The probe asks the proxy a question it refuses at the schema, which it does with or
    without an engine behind it, and the lane name fell back to "qwen3.8-27b" whenever the
    engine gave none: with nothing serving, or the image lane up, the tab read "serving
    qwen3.8-27b", and it kept whatever it read for a minute across a switch."""

    def setUp(self):
        self.asked = []
        self._orig = self.ck.urllib.request.urlopen

        def refuse_at_the_schema(req, timeout=None):
            self.asked.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 422, "Unprocessable", {}, None)

        self.ck.urllib.request.urlopen = refuse_at_the_schema
        self.addCleanup(setattr, self.ck.urllib.request, "urlopen", self._orig)
        self.ck.SYSTEMONE_CACHE.update(data=None, ts=0.0)
        with self.ck.STATE_LOCK:
            self.ck.STATE["engine_info"] = {"data": {"info": {}}, "ts": 0}

    def test_nothing_serving(self):
        self.states(sglang="stopped", flash="stopped")
        out = self.ck.systemone_available(max_age=0.0)
        self.assertFalse(out["available"])
        self.assertEqual(out["lane"], "")
        self.assertIn("no text lane", out["reason"])
        self.assertEqual(self.asked, [], "no probe is worth sending without a lane")

    def test_the_image_lane_serving(self):
        self.states(sglang="stopped", image="ready")
        out = self.ck.systemone_available(max_age=0.0)
        self.assertFalse(out["available"])
        self.assertEqual(out["lane"], "")

    def test_a_text_lane_that_is_not_ready_says_so(self):
        self.states(sglang="loading-weights")
        out = self.ck.systemone_available(max_age=0.0)
        self.assertFalse(out["available"])
        self.assertIn("loading-weights", out["reason"])

    def test_a_ready_lane_is_probed_and_named(self):
        self.states(flash="ready")
        with self.ck.STATE_LOCK:
            self.ck.STATE["engine_info"] = {"data": {"info": {"served_model_name": "qwen3.8-flash-next"}}, "ts": 0}
        out = self.ck.systemone_available(max_age=0.0)
        self.assertTrue(out["available"])
        self.assertEqual(out["lane"], "qwen3.8-flash-next")
        self.assertEqual(len(self.asked), 1)

    def test_the_minute_of_cache_ends_with_the_lane(self):
        self.states(sglang="ready")
        with self.ck.STATE_LOCK:
            self.ck.STATE["engine_info"] = {"data": {"info": {"served_model_name": "qwen3.8-27b"}}, "ts": 0}
        self.assertTrue(self.ck.systemone_available(max_age=60.0)["available"])
        self.states(sglang="stopped")
        out = self.ck.systemone_available(max_age=60.0)
        self.assertFalse(out["available"], "a cached answer outlived its lane")


class Options(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside, self.values = False, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "select" and a.get("id") == "logsel":
            self.inside = True
        elif tag == "option" and self.inside:
            self.values.append(a.get("value"))

    def handle_endtag(self, tag):
        if tag == "select":
            self.inside = False


class TheLogsTabOffersTheJournalsItNames(Base):
    """"Read its journal in the Logs tab", five times over the page, and the tab offered the
    two containers, the proxy and opencode, and no lane's journal at all."""

    def offered(self):
        p = Options()
        p.feed((DASH / "static/index.html").read_text())
        return p.values

    def test_every_lane_journal_is_offered(self):
        for unit in ("qwen38-sglang.service", "qwen38-flash.service", "qwen38-image.service"):
            with self.subTest(unit=unit):
                self.assertIn(unit, self.offered())

    def test_every_option_is_one_the_server_serves(self):
        allowed = set(self.ck.CONTAINERS) | set(self.ck.JOURNAL_UNITS)
        for v in self.offered():
            with self.subTest(v=v):
                self.assertIn(v, allowed)


if __name__ == "__main__":
    unittest.main()
