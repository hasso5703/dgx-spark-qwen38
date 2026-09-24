#!/usr/bin/env python3
"""run.sh serves the engine where the service does: on loopback, unless ENGINE_BIND says.

run.sh runs the engine in the foreground with no proxy in front, and it passed
`--host 0.0.0.0`: the engine, without the proxy's guards against the fields it dies on,
was open to anyone on the network holding the key, while SECURITY.md said it is not on
the network at all (found in review, 2026-09-24). This runs the real run.sh in a
throwaway HOME, with docker, systemctl, ss and nvidia-smi replaced by stubs, and reads
the `docker run` it execs."""
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = (REPO / "install.sh").read_text()


def pin(name):
    return re.search(rf'^{name}="(?:\$\{{{name}:-)?([^"}}]+)', INSTALL, re.M).group(1)


STUBS = {
    # docker: images exist, nothing runs, and `run` records its argv
    "docker": '#!/bin/sh\ncase "$1" in\n  run) printf "%s\\n" "$@" > "$HOME/docker-run.argv"; exit 0 ;;\n'
              '  *) exit 0 ;;\nesac\n',
    "systemctl": "#!/bin/sh\nexit 3\n",
    "ss": "#!/bin/sh\nexit 0\n",
    "nvidia-smi": "#!/bin/sh\nexit 0\n",
}


class TheEngineBind(unittest.TestCase):
    def setUp(self):
        self.home = pathlib.Path(tempfile.mkdtemp(prefix="run-bind-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        cfg = self.home / ".config" / "qwen38"
        cfg.mkdir(parents=True)
        (cfg / "api-key").write_text("k\n")
        (cfg / "chat-template-sglang.jinja").write_text("t\n")
        hub = self.home / ".cache" / "huggingface" / "hub"
        for repo, rev in ((pin("STOCK_REPO"), pin("STOCK_REV")), (pin("DRAFT2_REPO"), pin("DRAFT2_REV"))):
            snap = hub / f"models--{repo.replace('/', '--')}" / "snapshots" / rev
            snap.mkdir(parents=True)
            (snap / "model.safetensors").write_text("w")
            (snap / "config.json").write_text('{"architectures": ["X"]}')
        self.bin = self.home / "bin"
        self.bin.mkdir()
        for name, body in STUBS.items():
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    def run_sh(self, **env):
        r = subprocess.run(["bash", str(REPO / "run.sh")], capture_output=True, text=True, timeout=60,
                           env={"PATH": f"{self.bin}:/usr/bin:/bin", "HOME": str(self.home), **env})
        argv = self.home / "docker-run.argv"
        return r, argv.read_text().split("\n") if argv.exists() else []

    def host_of(self, argv):
        self.assertIn("--host", argv)
        return argv[argv.index("--host") + 1]

    def test_loopback_by_default(self):
        r, argv = self.run_sh()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.host_of(argv), "127.0.0.1")
        self.assertIn("127.0.0.1:30000, with no proxy in front", r.stdout)

    def test_engine_bind_opens_it(self):
        r, argv = self.run_sh(ENGINE_BIND="0.0.0.0")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.host_of(argv), "0.0.0.0")

    def test_a_bind_that_is_not_an_address_is_refused(self):
        r, argv = self.run_sh(ENGINE_BIND="0.0.0.0 --api-key x")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ENGINE_BIND takes an IPv4 address", r.stderr)
        self.assertEqual(argv, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
