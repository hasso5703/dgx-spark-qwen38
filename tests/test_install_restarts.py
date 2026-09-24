#!/usr/bin/env python3
"""A run of ./install.sh that changes nothing restarts nothing.

v1.18.5 kept the engine when nothing it reads changed, and everything around it went on
restarting at every run: the proxy (every request in flight through it cut), opencode-web
up to three times (the Agent tab's turn ended) and the cockpit twice. On the reference box
on 2026-09-23: 49 starts of opencode-web and 36 of the cockpit. And on a 1m box step 7
wrote the table's bounds over the pair the end-of-install fit had set, which the fit then
wrote back, a backup each time: 79 in ~/.config/opencode by 2026-09-24 (found in review).

Each service is now restarted when it is not running, when this run changed what it runs,
or when it started before the last change of a file it reads; and those files are written
only when their content changes, so their date means something.
"""
import json
import os
import pathlib
import re
import subprocess
import tempfile
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = (REPO / "install.sh").read_text()
SCRIPTS = {name: (REPO / name).read_text() for name in
           ("install.sh", "dashboard/install-dashboard.sh", "dashboard/install-agent.sh")}


def helper(text):
    start = text.index("stale_since(){")
    return text[start:text.index("\n}\n", start) + 3]


def stale(text, active="active", started=None, files=()):
    d = pathlib.Path(tempfile.mkdtemp(prefix="stale-"))
    (d / "systemctl").write_text(
        "#!/bin/sh\ncase \"$*\" in *ActiveState*) echo \"$FAKE_ACTIVE\";; "
        "*ExecMainStartTimestamp*) [ -n \"$FAKE_STARTED\" ] && echo \"@$FAKE_STARTED\";; esac\n")
    (d / "systemctl").chmod(0o755)
    paths = []
    for i, age in enumerate(files):
        f = d / f"f{i}"
        f.write_text("x")
        os.utime(f, (time.time() - age, time.time() - age))
        paths.append(str(f))
    script = helper(text) + "if stale_since unit.service " + " ".join(paths) + "; then echo RESTART; else echo KEEP; fi\n"
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": f"{d}:/usr/bin:/bin", "FAKE_ACTIVE": active,
                            "FAKE_STARTED": "" if started is None else str(int(started))})
    return r.stdout.strip()


class StaleSince(unittest.TestCase):
    def test_every_copy_says_the_same(self):
        now = time.time()
        for name, text in SCRIPTS.items():
            with self.subTest(name):
                self.assertEqual(stale(text, active="inactive"), "RESTART")
                self.assertEqual(stale(text, started=now - 100, files=(500, 300)), "KEEP")
                self.assertEqual(stale(text, started=now - 100, files=(500, 10)), "RESTART")
                self.assertEqual(stale(text, started=None, files=(500,)), "RESTART")


class EachRestartHasAReason(unittest.TestCase):
    def test_opencode_web_after_step_7_only_when_a_config_changed(self):
        i = INSTALL.index('sudo systemctl restart opencode-web.service \\\n      && echo "opencode-web.service restarted')
        guard = INSTALL[INSTALL.rfind("\n  if ", 0, i):i]
        self.assertIn('"$(oc_configs_sum)" != "$OC_SUM_BEFORE"', guard)
        self.assertLess(INSTALL.index('OC_SUM_BEFORE="$(oc_configs_sum)"'), INSTALL.index("python3 - <<'PYEOF' || die \"could not write the opencode provider config\""))

    def test_the_proxy_only_for_a_new_engine_new_code_or_a_new_unit(self):
        for m in re.finditer(r'sudo systemctl restart "\$KEEPALIVE_UNIT"', INSTALL):
            guard = INSTALL[INSTALL.rfind("if ", 0, m.start()):m.start()]
            self.assertIn("stale_since \"$KEEPALIVE_UNIT\"", guard, INSTALL[m.start() - 200:m.start()])
            self.assertIn('"$KA_CHANGED" -eq 1', guard)
        for write in ('install -m 755 "$REPO_DIR/keepalive-proxy.py" "$CONFIG_DIR/keepalive-proxy.py"; KA_CHANGED=1',
                      'sudo install -m 644 "$TMP_KA" "/etc/systemd/system/$KEEPALIVE_UNIT"; KA_CHANGED=1'):
            self.assertIn(write, INSTALL)

    def test_the_cockpit_and_opencode_web_installers(self):
        for name, marker in (("dashboard/install-dashboard.sh", 'DASH_CHANGED'),
                             ("dashboard/install-agent.sh", 'AGENT_CHANGED')):
            text = SCRIPTS[name]
            i = text.index('sudo systemctl try-restart "$UNIT"')
            guard = text[text.rfind("\nif ", 0, i):i]
            self.assertIn(f'"${marker}" -eq 1', guard, name)
            self.assertIn("stale_since", guard, name)


class TheGeneratedConfigKeepsItsFit(unittest.TestCase):
    """The generator of ~/.config/qwen38/opencode.json, run twice in one config dir with the
    environment install.sh gives it: once as a fresh install, then again after the
    end-of-install fit wrote the pool's pair into the file."""

    BODY = None

    def run_generator(self, d, mode):
        start = INSTALL.index("OC_CONFIG_DIR=\"$CONFIG_DIR\" python3 - <<'PYEOF'")
        body = INSTALL[INSTALL.index("\n", start) + 1:INSTALL.index("\nPYEOF\n", start)]
        ctx, out = ("700000", "200000") if mode == "1m" else ("173000", "64000")
        env = dict(os.environ, OC_LANE="27b", OC_27B="1", OC_FLASH="0", OC_PORT="30001", OC_CTX=ctx,
                   OC_OUT=out, OC_LABEL="local, 1M", OC_CONTEXT_MODE=mode, OC_27B_CTX=ctx, OC_27B_OUT=out,
                   OC_KEEP="38000", OC_PIN="1", OC_CONFIG_DIR=str(d))
        r = subprocess.run(["python3", "-c", body], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        f = d / "opencode.json"
        lim = json.loads(f.read_text())["provider"]["qwen38"]["models"]["qwen3.8-27b"]["limit"]
        return (lim["context"], lim["output"]), os.stat(f).st_mtime, f.read_text()

    def fitted_dir(self, ctx, out):
        d = pathlib.Path(tempfile.mkdtemp(prefix="oc-gen-"))
        self.run_generator(d, "1m")
        f = d / "opencode.json"
        f.write_text(f.read_text().replace('"context": 700000', f'"context": {ctx}').replace(
            '"input": 700000', f'"input": {ctx}').replace('"output": 200000', f'"output": {out}'))
        os.utime(f, (1000, 1000))
        return d

    def test_a_fit_under_the_1m_bounds_is_kept(self):
        pair, _, _ = self.run_generator(self.fitted_dir(559000, 186000), "1m")
        self.assertEqual(pair, (559000, 186000))

    def test_the_same_config_is_not_rewritten(self):
        d = self.fitted_dir(559000, 186000)
        before = (d / "opencode.json").read_text()
        _, mtime, text = self.run_generator(d, "1m")
        self.assertEqual(text, before)
        self.assertEqual(mtime, 1000, "an identical config was written again, and its date moved")

    def test_a_native_install_writes_the_table(self):
        pair, _, _ = self.run_generator(self.fitted_dir(559000, 186000), "native")
        self.assertEqual(pair, (173000, 64000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
