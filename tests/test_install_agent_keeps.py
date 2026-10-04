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
temporary directory, with an opencode that only knows `serve --help` and a tailscale
that fails, both first on the PATH: the box's own tailscale is never asked.
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
OPENCODE = "#!/bin/sh\ncase \"$*\" in *--help*) echo '--hostname --port';; *) echo 9.9.9;; esac\n"


def oc_unit(dirs):
    """An installed opencode-web unit whose PATH is `dirs`."""
    return ("[Service]\nEnvironment=PATH=" + ":".join(dirs) + "\n"
            "ExecStart=/x/opencode serve --hostname 127.0.0.1 --port 4100 --print-logs --log-level INFO\n")


def run_in(t, dash_unit, oc_unit=None, caller=(), **env):
    """((opencode port, relay port, relay bind, service PATH), stdout) of the script's lines,
    with the directory t standing in for /etc/systemd/system. The PATH they run with is the
    stubs' directory (t/bin, the same on every run in t), then `caller`, then /usr/bin:/bin."""
    (t / "qwen38-dashboard.service").write_text(dash_unit)
    if oc_unit is not None:
        (t / "opencode-web.service").write_text(oc_unit)
    fake = t / "bin"
    if not fake.exists():
        fake.mkdir()
        (fake / "opencode").write_text(OPENCODE)
        (fake / "tailscale").write_text("#!/bin/sh\nexit 1\n")
        for f in fake.iterdir():
            f.chmod(0o755)
    script = (HEAD.replace("/etc/systemd/system", str(t))
              + 'echo "RESULT|$OPENCODE_PORT|$AGENT_PORT|$AGENT_BIND|$SVC_PATH"\n')
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                       cwd=REPO / "dashboard",
                       env={"PATH": ":".join([str(fake), *caller, "/usr/bin", "/bin"]), "HOME": str(t), **env})
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT|")), None)
    if line is None:
        raise AssertionError(f"the script did not get to the render:\n{r.stdout}\n{r.stderr}")
    return tuple(line.split("|")[1:]), r.stdout


def run(dash_unit, oc_unit=None, caller=(), **env):
    with tempfile.TemporaryDirectory(prefix="agent-keeps-") as t:
        return run_in(pathlib.Path(t), dash_unit, oc_unit, caller, **env)[0]


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

    def test_the_callers_order_comes_first(self):
        # a newer tool first on the caller's PATH stays first for the agent: nvm's new node
        # before the one the installed unit names
        old, new = "/home/u/.nvm/versions/node/v24/bin", "/home/u/.nvm/versions/node/v26/bin"
        path = run(DASH_CUSTOM, oc_unit([old, "/usr/bin"]), caller=[new])[3].split(":")
        self.assertLess(path.index(new), path.index(old))

    def test_a_first_install_takes_the_defaults(self):
        oc, port, bind, _ = run("[Service]\nEnvironment=COCKPIT_BIND=0.0.0.0\nEnvironment=COCKPIT_AGENT_PORT=0\n")
        self.assertEqual((oc, port, bind), ("4096", "30091", "tailscale"))


H = "/home/someone"
# What an editor's session puts on its terminal's PATH, by the layouts the filter knows.
EDITOR_DIRS = [
    f"{H}/.vscode-server/cli/servers/Stable-07f806f999/server/bin/remote-cli",       # the reference box,
    f"{H}/.vscode-server/data/User/globalStorage/github.copilot-chat/debugCommand",  # 2026-10-03
    f"{H}/.vscode-server/data/User/globalStorage/github.copilot-chat/copilotCli",
    f"{H}/.vscode-server/bin/07f806f999/bin/remote-cli",
    f"{H}/.vscode/cli/servers/Stable-07f806f999/server/bin/remote-cli",
    f"{H}/.cursor-server/cli/servers/Stable-1a2b3c/server/bin/remote-cli",
    f"{H}/.positron-server/bin/1a2b3c/bin/remote-cli",
    "/usr/lib/code-server/lib/vscode/bin/remote-cli",
    f"{H}/.config/Code/User/globalStorage/github.copilot-chat/debugCommand",
    f"{H}/.vscode/extensions/ms-python.python-2026.6.0-linux-arm64/python_files/deactivate/bash",
    f"{H}/.vscode-server/extensions/ms-python.debugpy-2026.6.0-linux-arm64/bundled/scripts/noConfigScripts",
]
# A desktop build's storage under a name with spaces: the script died on its PATH.
SPACED = f"{H}/.config/Code - OSS/User/globalStorage/github.copilot-chat/copilotCli"
# Each a character or a part away from one of those layouts, and none of them an editor's.
LOOK_ALIKES = [
    f"{H}/.vscode-server-tools/bin",
    f"{H}/xvscode-server/bin",
    f"{H}/my.vscode-server/bin",
    "/opt/x/bin/remote-cli-tools",
    f"{H}/sbin/remote-cli",
    f"{H}/MyUser/globalStorage/bin",
    "/opt/extensions/my.tools/bin",
    "/opt/extensions/tools-2/bin",
]


class AnEditorsSessionStaysOutOfTheServicePath(unittest.TestCase):
    """What an editor's session puts on its terminal's PATH reached the unit: an update run
    from a VS Code terminal wrote VS Code's remote CLI (under a directory named after its
    build) and two of its extensions' directories into opencode-web's PATH, so the unit
    changed and the service restarted, and the remote CLI's directory goes at the next VS
    Code update. The installed PATH kept them for good, since it only ever grew (the reference
    box, 2026-10-03). They are known by their layout, left out of the caller's PATH and of
    the installed one, and everything else is kept as before."""

    def dirs(self, caller=(), installed=None, **env):
        (_, _, _, path), out = self.run_once(caller, installed, **env)
        return path.split(":"), out

    def run_once(self, caller=(), installed=None, **env):
        with tempfile.TemporaryDirectory(prefix="agent-editor-") as t:
            return run_in(pathlib.Path(t), DASH_CUSTOM, None if installed is None else oc_unit(installed),
                          caller, **env)

    def test_the_callers_editor_directories_are_left_out(self):
        dirs, _ = self.dirs(EDITOR_DIRS + [SPACED, "/opt/tools/bin"])
        self.assertEqual([d for d in EDITOR_DIRS + [SPACED] if d in dirs], [])
        self.assertIn("/opt/tools/bin", dirs)
        self.assertIn("/usr/bin", dirs)

    def test_a_box_that_already_has_them_loses_them_and_is_told(self):
        dirs, out = self.dirs(installed=["/opt/agent-tools/bin"] + EDITOR_DIRS + ["/usr/bin"])
        self.assertEqual([d for d in EDITOR_DIRS if d in dirs], [])
        self.assertIn("/opt/agent-tools/bin", dirs)
        note = next(ln for ln in out.splitlines() if ln.startswith("NOTE: opencode-web's PATH leaves out"))
        for d in EDITOR_DIRS:
            self.assertIn(d, note)

    def test_nothing_is_said_when_nothing_is_left_out(self):
        _, out = self.dirs(EDITOR_DIRS, installed=["/opt/agent-tools/bin", "/usr/bin"])
        self.assertNotIn("leaves out", out)

    def test_a_name_that_only_looks_like_one_is_kept(self):
        dirs, _ = self.dirs(LOOK_ALIKES)
        self.assertEqual([d for d in LOOK_ALIKES if d not in dirs], [])

    def test_relative_and_tilde_entries_stay(self):
        # bash resolves both at each command, against its working directory and the home
        kept = ["~/.local/bin", "./node_modules/.bin", "node_modules/.bin"]
        dirs, _ = self.dirs(kept + ["/opt/tools/bin"], installed=["~/bin", "/usr/bin"])
        self.assertEqual([d for d in kept + ["~/bin"] if d not in dirs], [])

    def test_agent_path_is_taken_as_given_and_the_next_run_leaves_the_editor_out(self):
        given = EDITOR_DIRS[0] + ":/usr/bin"
        (_, _, _, path), _ = self.run_once(installed=["/usr/bin"], AGENT_PATH=given)
        self.assertEqual(path, given)
        dirs, out = self.dirs(installed=given.split(":"))
        self.assertNotIn(EDITOR_DIRS[0], dirs)
        self.assertIn(EDITOR_DIRS[0], out)


class EachDirectoryOnce(unittest.TestCase):
    """The caller's PATH and the installed one are merged on every update, the installed
    one being what the run before wrote: a directory named twice in either, or in both,
    is named once. Each run here reads the unit the run before wrote."""

    def updates(self, *callers):
        with tempfile.TemporaryDirectory(prefix="agent-once-") as td:
            t, installed, seen = pathlib.Path(td), None, []
            for caller in callers:
                (_, _, _, path), _ = run_in(t, DASH_CUSTOM, installed, caller)
                installed = oc_unit(path.split(":"))
                seen.append(path)
            return seen

    def test_each_directory_is_named_once(self):
        caller = ["/x/bin", "/x/bin", "/y/bin", "/x/bin"]
        for path in self.updates(caller, caller + ["/y/bin"], caller):
            dirs = path.split(":")
            self.assertEqual(len(dirs), len(set(dirs)), dirs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
