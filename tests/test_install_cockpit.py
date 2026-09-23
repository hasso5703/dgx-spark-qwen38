#!/usr/bin/env python3
"""A plain install leaves a box you can open, not a box plus a second command.

Until v1.12 install.sh installed the engine and the proxy and stopped there:
the cockpit was opt-in behind dashboard/install-dashboard.sh, which is a line
in a README that nobody reads after a 30 GB download. The one-liner is the
product, so the cockpit ships with it and the installer ends on its URL.

Off is still reachable, and it persists the way the opencode choice does, in a
marker file: an operator who said --no-cockpit once must not find a dashboard
unit back after the next upgrade. These runs stop at a later refusal on purpose
(--no-service with CONTEXT_MODE=1m), which is past the flag handling and long
before anything is downloaded or written outside the marker under test.
"""
import os
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = str(REPO / "install.sh")
# Past the cockpit block, dead before the preflight: the pair refuses because
# 1m needs the keepalive proxy service that --no-service does not install.
STOP_EARLY = ["--no-service"]
STOP_ENV = {"CONTEXT_MODE": "1m"}


def run(args=(), home=None, **extra):
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
           "HOME": home or tempfile.mkdtemp(prefix="cockpit-flag-home-")}
    env.update(STOP_ENV)
    env.update(extra)
    r = subprocess.run([INSTALL, *args], capture_output=True, text=True,
                       env=env, cwd=str(REPO), timeout=60)
    return r.returncode, r.stdout + r.stderr, env["HOME"]


class TheFlags(unittest.TestCase):
    def test_contradictory_flags_are_refused(self):
        rc, out, _ = run(["--no-cockpit", "--with-cockpit"])
        self.assertEqual(rc, 1)
        self.assertIn("contradict each other", out)

    def test_help_documents_both_flags(self):
        r = subprocess.run([INSTALL, "--help"], capture_output=True, text=True,
                           env={"PATH": "/usr/local/bin:/usr/bin:/bin",
                                "HOME": tempfile.mkdtemp()}, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--no-cockpit", r.stdout)
        self.assertIn("--with-cockpit", r.stdout)

    def test_help_says_a_plain_run_installs_the_cockpit(self):
        r = subprocess.run([INSTALL, "--help"], capture_output=True, text=True,
                           env={"PATH": "/usr/local/bin:/usr/bin:/bin",
                                "HOME": tempfile.mkdtemp()}, timeout=30)
        self.assertIn("cockpit", r.stdout)
        self.assertIn("sudo", r.stdout)


class TheMarker(unittest.TestCase):
    def test_no_cockpit_writes_the_marker(self):
        _, _, home = run(["--no-cockpit", *STOP_EARLY])
        self.assertTrue(os.path.isfile(os.path.join(home, ".config/qwen38/cockpit.off")))

    def test_the_marker_survives_a_plain_rerun_and_says_so(self):
        _, _, home = run(["--no-cockpit", *STOP_EARLY])
        _, out, _ = run(STOP_EARLY, home=home)
        self.assertIn("Keeping the cockpit off", out)
        self.assertTrue(os.path.isfile(os.path.join(home, ".config/qwen38/cockpit.off")))

    def test_with_cockpit_removes_the_marker(self):
        _, _, home = run(["--no-cockpit", *STOP_EARLY])
        _, out, _ = run(["--with-cockpit", *STOP_EARLY], home=home)
        self.assertFalse(os.path.isfile(os.path.join(home, ".config/qwen38/cockpit.off")))
        self.assertNotIn("Keeping the cockpit off", out)

    def test_a_plain_run_says_nothing_about_keeping_it_off(self):
        _, out, home = run(STOP_EARLY)
        self.assertNotIn("Keeping the cockpit off", out)
        self.assertFalse(os.path.isfile(os.path.join(home, ".config/qwen38/cockpit.off")))


class TheWiring(unittest.TestCase):
    """The flags above can all pass while the step does nothing, so the step
    itself is pinned: it calls both installers, and the summary names the URL."""

    src = pathlib.Path(INSTALL).read_text()

    def test_the_step_calls_the_cockpit_installer(self):
        self.assertIn('dashboard/install-dashboard.sh"', self.src)
        self.assertIn("step \"10/10", self.src)

    def test_the_step_calls_the_agent_installer(self):
        self.assertIn('dashboard/install-agent.sh"', self.src)

    def test_a_missing_opencode_costs_one_tab_and_not_the_install(self):
        # install-agent.sh dies without opencode on PATH, so it is only called
        # when opencode is there, and its failure is caught either way.
        self.assertIn('command -v opencode >/dev/null 2>&1', self.src)

    def test_the_summary_leads_with_the_cockpit_url(self):
        self.assertIn("OPEN THE COCKPIT", self.src)
        self.assertIn('COCKPIT_URL="http://$CK_HOST:${CK_PORT:-30090}"', self.src)

    def test_the_url_is_read_back_from_the_installed_unit(self):
        # Not assumed from what this run passed: install-agent.sh re-renders
        # that unit, and a converged re-run keeps an address this run never set.
        self.assertIn("^Environment=COCKPIT_PORT=", self.src)
        self.assertIn("^Environment=COCKPIT_BIND=", self.src)

    def test_a_wildcard_bind_is_turned_into_an_address_you_can_type(self):
        self.assertIn("0.0.0.0|::|\"[::]\")", self.src)

    def test_a_cockpit_failure_never_fails_a_serving_engine(self):
        self.assertIn("NOTE: the cockpit did not install. The engine above is up and serving.", self.src)

    def test_the_cockpit_step_runs_after_the_generation_smoke_test(self):
        self.assertLess(self.src.index("running a real generation smoke test"),
                        self.src.index('step "10/10'))



class TheSummaryComesOutOnce(unittest.TestCase):
    """install-agent.sh runs install-dashboard.sh again right after install.sh did, only
    to add the relay, and the cockpit's URL, "Bound to" and "Remove with" lines came out
    twice at the end of every install (reference box, 2026-09-23). The nested run says
    only what is new: the relay and, when it applies, the key warning."""

    def summary(self, quiet):
        text = (REPO / "dashboard" / "install-dashboard.sh").read_text()
        start = text.index("# DASH_QUIET=1:")
        body = text[start:]
        home = tempfile.mkdtemp(prefix="dash-sum-")
        script = ("set -euo pipefail\nPROBE=127.0.0.1; PORT=30090; BIND=0.0.0.0; AGENT_PORT=30091; "
                  "AGENT_BIND=tailscale; AGENT_UPSTREAM=http://127.0.0.1:4096; UNIT=qwen38-dashboard.service; "
                  f"INSTALLED=/etc/systemd/system/qwen38-dashboard.service; HOME={home}\n" + body)
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                           env={"PATH": "/usr/bin:/bin", "DASH_QUIET": quiet})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_the_first_run_prints_the_whole_summary(self):
        out = self.summary("0")
        for line in ("Spark Cockpit: http://127.0.0.1:30090", "Bound to 0.0.0.0", "Agent relay:", "Remove with:"):
            self.assertIn(line, out)

    def test_the_nested_run_prints_only_what_is_new(self):
        out = self.summary("1")
        self.assertIn("Agent relay: tailscale:30091", out)
        self.assertIn("WARNING:", out, "a missing key still has to be said")
        for line in ("Spark Cockpit:", "Bound to", "Remove with:"):
            self.assertNotIn(line, out)

    def test_the_agent_installer_asks_for_the_quiet_run(self):
        self.assertIn("DASH_QUIET=1", (REPO / "dashboard" / "install-agent.sh").read_text())

if __name__ == "__main__":
    unittest.main(verbosity=2)
