"""The Logs view says the engine's times in the box's own time.

The serving containers run in UTC, and their lines read two hours behind every other time on
the page in Luxembourg: 16:37 shown at 18:37 (found 2026-10-02). Each line's instant comes from
its transport (docker's --timestamps, the journal's __REALTIME_TIMESTAMP), and a leading
[YYYY-MM-DD HH:MM:SS] that is that instant written in UTC is rewritten in local time; a stamp
that is not stays as it was. The box's zone is pinned here to Europe/Luxembourg, the reference
box's, and given back after."""
import http.client
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

# every module hands the next one the environment it found (test_env_isolation.py)
ENV_BEFORE = dict(os.environ)


def tearDownModule():
    for name in set(os.environ) - set(ENV_BEFORE):
        del os.environ[name]
    os.environ.update(ENV_BEFORE)
    time.tzset()


HERE = Path(__file__).resolve()
DASH = HERE.parents[1]
KEY = "k-" + "Q" * 40
# 2026-10-02 16:37:16 UTC, 18:37:16 in Luxembourg (CEST)
AT = 1790959036
UTC_STAMP, LOCAL_STAMP = "[2026-10-02 16:37:16]", "[2026-10-02 18:37:16]"


def load():
    spec = importlib.util.spec_from_file_location("cockpit_logs_local", DASH / "cockpit.py")
    cp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cp)
    return cp


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="cockpit-logs-tz-"))
        (cls.tmp / "api-key").write_text(KEY + "\n")
        os.environ.update(COCKPIT_DRY_RUN="1", COCKPIT_CONFIG_DIR=str(cls.tmp), COCKPIT_REPO_DIR=str(DASH.parent),
                          COCKPIT_PORT="0", COCKPIT_AGENT_PORT="0", COCKPIT_AUTOHEAL="0", TZ="Europe/Luxembourg")
        time.tzset()
        sys.path.insert(0, str(DASH))
        cls.cp = load()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TheTransports(Base):
    def test_docker_gives_each_line_its_instant_and_its_own_text(self):
        got = self.cp.docker_log_entries(
            f"2026-10-02T16:37:16.218804766Z {UTC_STAMP} INFO: ready\n"
            "2026-10-02T16:37:17Z   File \"x.py\", line 3\n"
            "Error response from daemon: No such container: qwen38-flash\n")
        self.assertEqual(got, [(AT, f"{UTC_STAMP} INFO: ready"), (AT + 1, '  File "x.py", line 3'),
                               (None, "Error response from daemon: No such container: qwen38-flash")])

    def test_the_journal_gives_every_line_of_an_entry_its_instant(self):
        entries = [{"MESSAGE": f"{UTC_STAMP} INFO: one", "__REALTIME_TIMESTAMP": str(AT * 10**6 + 522683)},
                   {"MESSAGE": "Traceback:\n  line two", "__REALTIME_TIMESTAMP": str((AT + 5) * 10**6)},
                   {"MESSAGE": list("caf\xe9".encode("latin-1")), "__REALTIME_TIMESTAMP": str(AT * 10**6)},
                   {"MESSAGE": None, "__REALTIME_TIMESTAMP": str(AT * 10**6)},
                   {"MESSAGE": "no instant"}]
        txt = "\n".join(json.dumps(e) for e in entries) + "\nnot an entry at all\n"
        got = self.cp.journal_log_entries(txt)
        self.assertEqual([g[1] for g in got], [f"{UTC_STAMP} INFO: one", "Traceback:", "  line two", "caf�",
                                               "no instant", "not an entry at all"])
        self.assertAlmostEqual(got[0][0], AT + 0.522683, places=5)
        self.assertEqual((got[1][0], got[2][0]), (AT + 5, AT + 5))
        self.assertEqual((got[4][0], got[5][0]), (None, None))


class TheStamp(Base):
    def test_a_utc_stamp_of_the_lines_own_instant_reads_in_local_time(self):
        line, rewritten = self.cp.local_log_stamp(f"{UTC_STAMP} INFO: ready", AT + 0.4)
        self.assertEqual((line, rewritten), (f"{LOCAL_STAMP} INFO: ready", True))

    def test_a_stamp_that_is_local_already_stays(self):
        """A lane that runs natively writes local time: its stamp is two hours off the instant
        read as UTC, so it is not rewritten."""
        self.assertEqual(self.cp.local_log_stamp(f"{LOCAL_STAMP} x", AT), (f"{LOCAL_STAMP} x", False))

    def test_another_time_or_no_time_stays(self):
        for line in ("[2026-09-30 08:00:00] replayed from an older boot", "INFO: no stamp", "[not a time] x", ""):
            with self.subTest(line=line):
                self.assertEqual(self.cp.local_log_stamp(line, AT), (line, False))
        self.assertEqual(self.cp.local_log_stamp(f"{UTC_STAMP} x", None), (f"{UTC_STAMP} x", False))

    def test_a_box_in_utc_rewrites_nothing(self):
        os.environ["TZ"] = "UTC"
        time.tzset()
        try:
            self.assertEqual(self.cp.local_log_stamp(f"{UTC_STAMP} x", AT), (f"{UTC_STAMP} x", False))
        finally:
            os.environ["TZ"] = "Europe/Luxembourg"
            time.tzset()


class TheEndpoint(Base):
    """The whole route: the escapes go first (a coloured line starts with one), the stamp is
    rewritten, and the key is still masked, on what the page receives."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        docker = (f"2026-10-02T16:37:16.1Z \x1b[32m{UTC_STAMP}\x1b[0m server_args api_key='{KEY}'\n"
                  f"2026-10-02T16:37:17.9Z [2026-10-02 16:37:17] INFO: ready\n")
        journal = json.dumps({"MESSAGE": f"{UTC_STAMP} key {KEY[:3]}\x1b[31m{KEY[3:]}",
                              "__REALTIME_TIMESTAMP": str(AT * 10**6)}) + "\n"
        cls.calls = []

        def fake(argv, timeout=5.0, merge_err=False):
            cls.calls.append(list(argv))
            return docker if argv[0] == "docker" else journal
        cls.cp.run = fake
        cls.srv = cls.cp.Server(("127.0.0.1", 0), cls.cp.Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        super().tearDownClass()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        try:
            c.request("GET", path, headers={"Cookie": "cockpit=" + self.cp.make_token("sess")})
            r = c.getresponse()
            return r.status, json.loads(r.read().decode())
        finally:
            c.close()

    def test_a_container_reads_in_local_time_and_masked(self):
        st, d = self.get("/api/logs/qwen38-flash")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["lines"], [f"{LOCAL_STAMP} server_args api_key='<masked>'",
                                      "[2026-10-02 18:37:17] INFO: ready"])
        self.assertEqual((d["local_stamps"], d["tz"]), (2, "CEST"))
        self.assertIn("--timestamps", next(c for c in self.calls if c[0] == "docker"))

    def test_a_journal_reads_in_local_time_and_masked(self):
        st, d = self.get("/api/logs/qwen38-flash.service")
        self.assertEqual(st, 200, d)
        self.assertEqual(d["lines"], [f"{LOCAL_STAMP} key <masked>"])
        self.assertEqual(d["local_stamps"], 1)
        argv = next(c for c in self.calls if c[0] == "journalctl")
        self.assertEqual(argv[argv.index("-o") + 1], "json")
        self.assertIn("--output-fields=MESSAGE", argv)


if __name__ == "__main__":
    unittest.main(verbosity=2)
