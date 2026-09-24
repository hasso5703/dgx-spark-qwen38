#!/usr/bin/env python3
"""Which interface the engine and the proxy answer on, and who decides it.

Both listened on every interface until v1.17. The engine does not any more, and that is a
changed default rather than a knob: seven days of journal on the reference box put
581,479 engine requests at 100% from 127.0.0.1, while the machine was reached from a
laptop and a phone on the cockpit port alone. An open port nobody uses would be an
ordinary waste; this one is the port SGLang dies on rather than refuses (sglang#40076 and
sglang#31597, both unfixed upstream, both refused at the proxy since v1.15.0), so leaving
it open leaves a door beside the one with the lock.

Nothing is lost by closing it, which is the only reason a default may move: the proxy on
PORT+1 is a full pass-through, every route and both dialects, plus the guards. A client
that pointed at :30000 from another machine points at :30001 and gets more, not less. The
proxy's own default does not move, because it is the door clients are told to use.

Four things are held here. The engine closes and the proxy stays open on a fresh install.
`ENGINE_BIND=0.0.0.0` brings the old behaviour back for a box that wants it. A bad value
is refused by name rather than written into a unit that fails to start minutes later. And
an installed choice wins over the default IN BOTH DIRECTIONS: a box that had chosen to
keep its engine on the network must not lose it to an update nobody read about, which is
the same promise that protects a box hardened before v1.17.

Every run stops at a later refusal on purpose, past the bind resolution and long before
the preflight downloads anything.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import installer_wall as wall  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]
INSTALL = str(REPO / "install.sh")
# Contradictory cockpit flags: the first refusal BELOW the bind resolution, so the
# resolution has run and whatever it decided is already on stdout.
STOP = ["--no-cockpit", "--with-cockpit"]
STOCK = {"MODEL_CHOICE": "stock"}


def run(args=(), script=None, home=None, **env_extra):
    """A walled copy of install.sh (a fresh box unless `script` is another one), with the
    commands that act on the box fenced: the refusal these runs stop at is checked to
    have fired, instead of trusted to."""
    env, record = wall.fenced_env(home=home or tempfile.mkdtemp(prefix="bind-home-"), **env_extra)
    r = subprocess.run([script or fresh_box(), *args], capture_output=True, text=True,
                       env=env, cwd=wall.cwd(), timeout=60)
    out = r.stdout + r.stderr
    assert wall.WALL not in out, f"the run went past its refusal:\n{out[-800:]}"
    assert not wall.reached(record), wall.reached(record)
    return r.returncode, out


def box_with_units(engine_host="127.0.0.1", proxy_bind=None):
    """A copy of install.sh on a box holding the units this test wrote, so the convergence
    block reads a bind this box does not actually have."""
    units = {"qwen38-sglang.service":
             "[Service]\nExecStart=/bin/true --model-path RadixArk/Qwen3.8-27B-NVFP4 "
             f"--host {engine_host} --port 30000 --context-length 1010000\n"}
    if proxy_bind is not None:
        units["qwen38-keepalive.service"] = f"[Service]\nEnvironment=PROXY_BIND={proxy_bind}\n"
    return wall.walled(units=wall.units_dir(units))


def flash_box(engine_host):
    """A flash box: its unit only points at the launcher, where the engine flags live, so
    the bind to keep is read from launch-flash.sh. Returns (install.sh copy, HOME); no lane
    is enabled on it, whatever the host running the test has."""
    home = pathlib.Path(tempfile.mkdtemp(prefix="bind-flash-"))
    cfg = home / ".config/qwen38"
    cfg.mkdir(parents=True)
    (cfg / "launch-flash.sh").write_text(
        "#!/bin/bash\nexec docker run --rm --name qwen38-flash lmsysorg/sglang@sha256:" + "a" * 64 +
        " python3 -m sglang.launch_server --model-path RadixArk/Qwen3.8-Flash-Next-NVFP4 \\\n"
        f"    --host {engine_host} --port 30000\n")
    units = wall.units_dir({"qwen38-flash.service": f"[Service]\nExecStart=/bin/bash {cfg}/launch-flash.sh\n"})
    return wall.walled(units=units), str(home)


def fresh_box():
    """No units anywhere: the default is what answers."""
    return wall.walled(units=wall.units_dir())


KEPT_ENGINE = "Keeping the installed engine bind:"
KEPT_PROXY = "Keeping the installed proxy bind:"


class TheDefaults(unittest.TestCase):
    def test_a_fresh_install_closes_the_engine_and_leaves_the_proxy_open(self):
        """The changed default of v1.17, asserted where it is decided. The engine is the
        port SGLang dies on rather than refuses and the one nothing remote was using; the
        proxy is the door clients are told to use and the one that refuses what the engine
        cannot, so only the first moves."""
        code, out = run(STOP, script=fresh_box(), **STOCK)
        self.assertNotIn(KEPT_ENGINE, out, "a fresh box has no unit to converge from")
        self.assertNotIn(KEPT_PROXY, out)
        # the resolution itself, read from the script rather than from a rendered unit,
        # because this run stops before anything is written
        text = (REPO / "install.sh").read_text()
        self.assertIn('ENGINE_BIND="${ENGINE_BIND:-127.0.0.1}"', text)
        self.assertIn('PROXY_BIND="${PROXY_BIND:-0.0.0.0}"', text)

    def test_the_old_behaviour_is_one_variable_away(self):
        """A box that wants the engine on the network says so, and is not argued with."""
        _, out = run(STOP, script=fresh_box(), ENGINE_BIND="0.0.0.0", **STOCK)
        self.assertNotIn("take an IPv4 address", out)

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
    def test_the_proxy_bind_is_kept_the_same_way(self):
        _, out = run(STOP, script=box_with_units(engine_host="0.0.0.0", proxy_bind="127.0.0.1"),
                     **STOCK)
        self.assertIn(KEPT_PROXY + " 127.0.0.1", out)

    def test_the_operator_still_overrides_what_is_installed(self):
        """Convergence keeps a choice; it does not take the choice away."""
        _, out = run(STOP, script=box_with_units(engine_host="127.0.0.1"),
                     ENGINE_BIND="0.0.0.0", **STOCK)
        self.assertNotIn(KEPT_ENGINE, out)

    def test_a_box_that_kept_the_engine_open_is_not_closed_by_an_update(self):
        """The default moved in v1.17, and convergence is what keeps that from reaching
        a box that had already chosen otherwise: an operator with a remote client on
        :30000 must not lose it to an update they did not read about."""
        _, out = run(STOP, script=box_with_units(engine_host="0.0.0.0"), **STOCK)
        self.assertIn(KEPT_ENGINE + " 0.0.0.0", out)

    def test_a_box_already_closed_says_nothing_because_nothing_changed(self):
        _, out = run(STOP, script=box_with_units(engine_host="127.0.0.1"), **STOCK)
        self.assertNotIn(KEPT_ENGINE, out)

    def test_a_flash_box_keeps_its_engine_bind_too(self):
        """On flash the bind is in launch-flash.sh, and the convergence read $UNIT_PATH,
        which only the 27B branch sets: every plain re-run printed "UNIT_PATH: unbound
        variable", carried on, and rendered the launcher with 127.0.0.1 over the box's
        0.0.0.0 (found in review, 2026-09-24)."""
        script, home = flash_box("0.0.0.0")
        _, out = run(STOP, script=script, home=home)
        self.assertNotIn("unbound variable", out)
        self.assertIn("Keeping the installed target model: flash", out, "the fixture is a flash box")
        self.assertIn(KEPT_ENGINE + " 0.0.0.0", out)
        script, home = flash_box("127.0.0.1")
        _, out = run(STOP, script=script, home=home)
        self.assertNotIn("unbound variable", out)
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
