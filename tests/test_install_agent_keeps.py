#!/usr/bin/env python3
"""A re-run of install-agent.sh keeps the Agent tab as it was installed.

install.sh re-runs dashboard/install-agent.sh on every update. The script took
OPENCODE_PORT, AGENT_PORT and AGENT_BIND from the environment or its defaults (4096,
30091, and an address derived from the cockpit's), and handed them to
install-dashboard.sh, which writes them into the cockpit's unit: a box whose relay was
installed on its own port or address was put back on the defaults at the next update,
and without tailscale its Agent tab went dark. The service PATH was the caller's, so an
update from a shell with a shorter PATH took tools away from the agent (found in review,
2026-09-24). Each is read back from the installed units now; the environment still wins.

The script's own lines run here, up to the unit render, against installed units in a
temporary directory, with an opencode that only knows `serve --help`.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "dashboard/install-agent.sh").read_text()
HEAD = TEXT[:TEXT.index("# ── render and install the unit")]

DASH_CUSTOM = ("[Service]\nEnvironment=COCKPIT_BIND=0.0.0.0\nEnvironment=COCKPIT_PORT=30090\n"
               "Environment=COCKPIT_AGENT_PORT=30095\nEnvironment=COCKPIT_AGENT_BIND=192.168.1.10\n"
               "Environment=COCKPIT_AGENT_UPSTREAM=http://127.0.0.1:4100\n")
OC_CUSTOM = ("[Service]\nEnvironment=PATH=/opt/agent-tools/bin:/usr/bin\n"
             "ExecStart=/x/opencode serve --hostname 127.0.0.1 --port 4100 --print-logs --log-level INFO\n")


def run(dash_unit, oc_unit=None, **env):
    t = pathlib.Path(tempfile.mkdtemp(prefix="agent-keeps-"))
    (t / "qwen38-dashboard.service").write_text(dash_unit)
    if oc_unit is not None:
        (t / "opencode-web.service").write_text(oc_unit)
    fake = t / "bin"
    fake.mkdir()
    (fake / "opencode").write_text("#!/bin/sh\ncase \"$*\" in *--help*) echo '--hostname --port';; *) echo 9.9.9;; esac\n")
    (fake / "tailscale").write_text("#!/bin/sh\nexit 1\n")
    for f in fake.iterdir():
        f.chmod(0o755)
    script = (HEAD.replace("/etc/systemd/system", str(t))
              + 'echo "RESULT|$OPENCODE_PORT|$AGENT_PORT|$AGENT_BIND|$SVC_PATH"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                       cwd=REPO / "dashboard",
                       env={"PATH": f"{fake}:/usr/bin:/bin", "HOME": str(t), **env})
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT|")), None)
    if line is None:
        raise AssertionError(f"the script did not get to the render:\n{r.stdout}\n{r.stderr}")
    _, oc, port, bind, path = line.split("|")
    return oc, port, bind, path


class AReRunKeepsTheInstalledTab(unittest.TestCase):
    def test_the_installed_ports_and_address_are_kept(self):
        oc, port, bind, path = run(DASH_CUSTOM, OC_CUSTOM)
        self.assertEqual((oc, port, bind), ("4100", "30095", "192.168.1.10"))

    def test_the_installed_service_path_is_not_lost(self):
        _, _, _, path = run(DASH_CUSTOM, OC_CUSTOM)
        self.assertIn("/opt/agent-tools/bin", path.split(":"))
        self.assertIn("/usr/bin", path.split(":"))

    def test_the_environment_still_wins(self):
        oc, port, bind, path = run(DASH_CUSTOM, OC_CUSTOM, OPENCODE_PORT="4200", AGENT_PORT="30099",
                                   AGENT_BIND="127.0.0.1", AGENT_PATH="/only/this")
        self.assertEqual((oc, port, bind, path), ("4200", "30099", "127.0.0.1", "/only/this"))

    def test_a_first_install_takes_the_defaults(self):
        oc, port, bind, _ = run("[Service]\nEnvironment=COCKPIT_BIND=0.0.0.0\nEnvironment=COCKPIT_AGENT_PORT=0\n")
        self.assertEqual((oc, port, bind), ("4096", "30091", "tailscale"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
