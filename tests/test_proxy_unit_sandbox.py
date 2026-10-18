#!/usr/bin/env python3
"""The proxy's unit gives it no more of the machine than it uses (2026-10-18).

The proxy is what a client's bytes reach first, on every address the box has, and it ran with
all of this user's rights: the docker group, with docker's socket one connect away, and the
cockpit's sudo commands. Its unit now runs it with no new privileges, the system and the homes
read-only, no capabilities, the system calls of a service, and docker's socket out of reach
(found in a cloud review; not yet run on the box). Run under systemd 255 in a cloud container,
the unit started and served, systemctl answered it, and the probe it ran could neither write,
nor reach the socket, nor sudo.

The lines hold because of what the proxy does, which this checks in its code: it runs
systemctl and nothing else, and writes nothing on disk. A proxy that needs more says so here
first, and in its unit (ReadWritePaths=, a wider filter).
"""
import ast
import pathlib
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
UNIT = (REPO / "qwen38-keepalive.service.template").read_text()
PROXY = ast.parse((REPO / "keepalive-proxy.py").read_text(), "keepalive-proxy.py")

SANDBOX = {
    "NoNewPrivileges": "yes", "ProtectSystem": "strict", "ProtectHome": "read-only",
    "PrivateTmp": "yes", "PrivateDevices": "yes", "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes", "ProtectKernelLogs": "yes", "ProtectControlGroups": "yes",
    "ProtectClock": "yes", "ProtectHostname": "yes", "RestrictNamespaces": "yes",
    "RestrictRealtime": "yes", "RestrictSUIDSGID": "yes", "LockPersonality": "yes",
    "CapabilityBoundingSet": "", "SystemCallArchitectures": "native",
    "SystemCallFilter": "@system-service", "SystemCallErrorNumber": "EPERM",
    "RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6 AF_NETLINK",
    "InaccessiblePaths": "-/run/docker.sock -/var/run/docker.sock", "UMask": "0077",
}
# what writes on disk: os's calls by their full name, shutil's and tempfile's all, and the
# methods only a path has (a list has a remove, a dict a copy)
OS_WRITERS = {"os.open", "os.remove", "os.unlink", "os.rename", "os.replace", "os.mkdir", "os.makedirs",
              "os.rmdir", "os.removedirs", "os.symlink", "os.link", "os.chmod", "os.chown", "os.truncate",
              "os.mkfifo", "os.mknod"}
PATH_WRITERS = {"write_text", "write_bytes", "touch", "mkdir", "unlink", "rmdir", "rename", "symlink_to",
                "hardlink_to", "chmod"}


def service_lines():
    """{directive: [value, ...]} of the template's [Service] section."""
    section, found = None, {}
    for line in UNIT.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        elif section == "[Service]" and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            found.setdefault(key, []).append(value)
    return found


def called(node):
    """The dotted name a call is made by (subprocess.run, os.open, open), or ""."""
    f, parts = node.func, []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


class TheUnitHoldsTheProxyToWhatItUses(unittest.TestCase):
    def test_the_unit_carries_its_sandbox(self):
        found = service_lines()
        for key, value in SANDBOX.items():
            self.assertEqual(found.get(key), [value], key)
        # sudo for the cockpit's commands: no new privileges is what stops it, not the sudoers
        self.assertNotIn("AmbientCapabilities", found)

    def test_the_proxy_runs_systemctl_and_nothing_else(self):
        runs = []
        for node in ast.walk(PROXY):
            if not isinstance(node, ast.Call):
                continue
            name = called(node)
            self.assertFalse(name.startswith(("os.system", "os.exec", "os.spawn", "os.popen")), name)
            if name.startswith("subprocess."):
                argv = node.args[0] if node.args else None
                self.assertIsInstance(argv, ast.List, f"line {node.lineno}: a command this test cannot read")
                self.assertEqual(getattr(argv.elts[0], "value", None), "systemctl", f"line {node.lineno}")
                runs.append(node.lineno)
        self.assertTrue(runs, "no systemctl call found: the walk looks in the wrong place")

    def test_the_proxy_writes_nothing_on_disk(self):
        for node in ast.walk(PROXY):
            if not isinstance(node, ast.Call):
                continue
            name = called(node)
            self.assertNotIn(name, OS_WRITERS, f"line {node.lineno}")
            self.assertFalse(name.startswith(("shutil.", "tempfile.")), f"line {node.lineno}: {name}")
            if not name.startswith("os."):
                self.assertNotIn(name.rsplit(".", 1)[-1], PATH_WRITERS, f"line {node.lineno}: {name}")
            if name == "open":
                mode = node.args[1] if len(node.args) > 1 else next(
                    (k.value for k in node.keywords if k.arg == "mode"), None)
                text = getattr(mode, "value", "r")
                self.assertIsInstance(text, str, f"line {node.lineno}: a mode this test cannot read")
                self.assertFalse(set(text) & set("wax+"), f"line {node.lineno}: open(..., {text!r})")

    def test_the_readers_see_the_shapes(self):
        # so a walk that stopped seeing them would not leave the two above green and blind
        shape = ast.parse("import subprocess, os\nsubprocess.run(['docker', 'ps'])\nopen(p, 'w')\n"
                          "os.replace(a, b)\npath.write_text(t)\nPath(p).touch()\n")
        calls = [called(n) for n in ast.walk(shape) if isinstance(n, ast.Call)]
        self.assertEqual(sorted(calls), ["Path", "open", "os.replace", "path.write_text", "subprocess.run", "touch"])


if __name__ == "__main__":
    unittest.main()
