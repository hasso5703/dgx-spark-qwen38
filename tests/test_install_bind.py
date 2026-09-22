#!/usr/bin/env python3
"""Which interface the engine and the proxy answer on, and who decides it.

Both listened on every interface until v1.16, which is the default this repo shipped and
the one it keeps: somebody else's laptop may legitimately point at either port, and a
default must never take away something the operator did not ask to lose.

What the reference box measured is why the knob exists at all. Seven days of journal:
581,479 requests to the engine and 8,288 to the proxy, every single one from 127.0.0.1,
while the machine was reached from a MacBook and a phone on the cockpit port alone. Those
two open ports are also the ones SGLang dies on rather than refuses (sglang#40076,
sglang#31597, both unfixed upstream), so an operator who wants the proxy's guards to be
the only door can close them:

  ENGINE_BIND=127.0.0.1 PROXY_BIND=127.0.0.1 ./install.sh

Three things are held here. The default does not move. A bad value is refused by name
rather than written into a unit that then fails to start. And an installed choice wins
over the default, because a box hardened to localhost that a plain `./install.sh`
reopens would be the worst of the three outcomes: the operator would have no reason to
look, and the port would be back.

Every run stops at a later refusal on purpose, past the bind resolution and long before
the preflight downloads anything.
"""
import pathlib
import subprocess
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = str(REPO / "install.sh")
# Contradictory cockpit flags: the first refusal BELOW the bind resolution, so the
# resolution has run and whatever it decided is already on stdout.
STOP = ["--no-cockpit", "--with-cockpit"]
STOCK = {"MODEL_CHOICE": "stock"}


def run(args=(), script=None, **env_extra):
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin",
           "HOME": tempfile.mkdtemp(prefix="bind-home-")}
    env.update(env_extra)
    r = subprocess.run([script or INSTALL, *args], capture_output=True, text=True,
                       env=env, cwd=str(REPO), timeout=60)
    return r.returncode, r.stdout + r.stderr


def box_with_units(engine_host="127.0.0.1", proxy_bind=None):
    """A copy of install.sh whose unit paths point at a directory holding the units this
    test wrote, so the convergence block reads a bind this box does not actually have."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="bind-units-"))
    (d / "qwen38-sglang.service").write_text(
        "[Service]\nExecStart=/bin/true --model-path RadixArk/Qwen3.8-27B-NVFP4 "
        f"--host {engine_host} --port 30000 --context-length 1010000\n")
    text = pathlib.Path(INSTALL).read_text()
    for var, unit in (("SGL_UNIT_PATH", "qwen38-sglang.service"),
                      ("FLASH_UNIT_PATH", "qwen38-flash.service")):
        old = '%s="/etc/systemd/system/%s"' % (var, unit)
        if old not in text:
            raise AssertionError("install.sh no longer assigns %s the way this test rewrites it" % var)
        text = text.replace(old, '%s="%s/%s"' % (var, d, unit))
    if proxy_bind is not None:
        ka = d / "qwen38-keepalive.service"
        ka.write_text(f"[Service]\nEnvironment=PROXY_BIND={proxy_bind}\n")
        old = '"/etc/systemd/system/qwen38-keepalive.service"'
        if text.count(old) < 1:
            raise AssertionError("install.sh no longer reads the keepalive unit by that path")
        text = text.replace(old, '"%s"' % ka)
    copy = d / "install.sh"
    copy.write_text(text)
    copy.chmod(0o755)
    return str(copy)


def fresh_box():
    """No units anywhere: the default is what answers."""
    empty = tempfile.mkdtemp(prefix="bind-none-")
    text = pathlib.Path(INSTALL).read_text()
    for var, unit in (("SGL_UNIT_PATH", "qwen38-sglang.service"),
                      ("FLASH_UNIT_PATH", "qwen38-flash.service")):
        text = text.replace('%s="/etc/systemd/system/%s"' % (var, unit),
                            '%s="%s/%s"' % (var, empty, unit))
    copy = pathlib.Path(empty) / "install.sh"
    copy.write_text(text)
    copy.chmod(0o755)
    return str(copy)


KEPT_ENGINE = "Keeping the installed engine bind:"
KEPT_PROXY = "Keeping the installed proxy bind:"


class TheDefaultDoesNotMove(unittest.TestCase):
    def test_a_fresh_install_still_answers_on_every_interface(self):
        """The knob is for an operator who asks for it. Nobody's remote client breaks
        because this repo decided their port should close."""
        _, out = run(STOP, script=fresh_box(), **STOCK)
        self.assertNotIn(KEPT_ENGINE, out)
        self.assertNotIn(KEPT_PROXY, out)

    def test_a_value_that_is_not_an_address_is_refused_by_name(self):
        """A bad bind writes a unit that fails to start minutes later, on a box whose
        engine has just been stopped. It is refused here instead, before anything moves."""
        for bad in ("localhost", "0.0.0.0:30000", "not-an-ip", "::1"):
            code, out = run(STOP, ENGINE_BIND=bad, **STOCK)
            self.assertNotEqual(code, 0, bad)
            self.assertIn("ENGINE_BIND and PROXY_BIND take an IPv4 address", out, bad)
        code, out = run(STOP, PROXY_BIND="nope", **STOCK)
        self.assertNotEqual(code, 0)
        self.assertIn("ENGINE_BIND and PROXY_BIND take an IPv4 address", out)

    def test_an_address_is_accepted_and_the_run_continues(self):
        _, out = run(STOP, ENGINE_BIND="127.0.0.1", PROXY_BIND="127.0.0.1", **STOCK)
        self.assertNotIn("take an IPv4 address", out)


class AnInstalledChoiceWins(unittest.TestCase):
    def test_a_box_closed_to_localhost_is_not_reopened_by_a_plain_update(self):
        """The whole point. An operator who closed the port has no reason to re-read the
        unit after an update, so an update that reopened it would go unnoticed."""
        _, out = run(STOP, script=box_with_units(engine_host="127.0.0.1"), **STOCK)
        self.assertIn(KEPT_ENGINE + " 127.0.0.1", out)

    def test_the_proxy_bind_is_kept_the_same_way(self):
        _, out = run(STOP, script=box_with_units(engine_host="0.0.0.0", proxy_bind="127.0.0.1"),
                     **STOCK)
        self.assertIn(KEPT_PROXY + " 127.0.0.1", out)

    def test_the_operator_still_overrides_what_is_installed(self):
        """Convergence keeps a choice; it does not take the choice away."""
        _, out = run(STOP, script=box_with_units(engine_host="127.0.0.1"),
                     ENGINE_BIND="0.0.0.0", **STOCK)
        self.assertNotIn(KEPT_ENGINE, out)

    def test_an_open_box_stays_open_without_a_word(self):
        """Nothing is said when nothing changed: a line per unchanged setting is how a
        log stops being read."""
        _, out = run(STOP, script=box_with_units(engine_host="0.0.0.0"), **STOCK)
        self.assertNotIn(KEPT_ENGINE, out)


class TheTemplatesCarryIt(unittest.TestCase):
    def test_every_engine_template_takes_the_bind_from_the_installer(self):
        """A template that hardcodes the host makes the knob a lie on that lane."""
        for name in ("qwen38-sglang.service.template", "qwen38-sglang-1m.service.template",
                     "qwen38-flash-launch.sh.template"):
            text = (REPO / name).read_text()
            self.assertIn("--host __ENGINE_BIND__", text, name)
            self.assertNotIn("--host 0.0.0.0", text, name)

    def test_the_keepalive_unit_passes_the_bind_to_the_proxy(self):
        text = (REPO / "qwen38-keepalive.service.template").read_text()
        self.assertIn("Environment=PROXY_BIND=__PROXY_BIND__", text)

    def test_the_proxy_reads_that_variable_and_defaults_to_every_interface(self):
        text = (REPO / "keepalive-proxy.py").read_text()
        self.assertIn('os.environ.get("PROXY_BIND", "0.0.0.0")', text)
        self.assertIn("Server((BIND, port), H)", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
