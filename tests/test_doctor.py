#!/usr/bin/env python3
"""doctor.py: what a GB10 box is, read without changing anything, and what is known about it.

The facts are the reference box's own, as its tools printed them on 2026-10-03 (an ASUS Ascent
GX10), and the HP ZGX Nano G1n of issue #26 as its owner reported it. /etc/dgx-release is
world-readable and holds the box's serial number: no output may ever carry it."""
import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("doctor", REPO / "doctor.py")
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)
DATA = doctor.load_data()

SERIAL = "TAMSAG002940X2C"
DGX_RELEASE = f'''DGX_NAME="DGX Spark"
DGX_PRETTY_NAME="NVIDIA DGX Spark"
DGX_SWBUILD_DATE="2025-09-10-13-50-03"
DGX_SWBUILD_VERSION="7.2.3"
DGX_COMMIT_ID="833b4a7"
DGX_PLATFORM="GX10"
DGX_SERIAL_NUMBER="{SERIAL}"

DGX_OTA_VERSION="7.3.1"
DGX_OTA_DATE="Thu Jan 15 19:07:36 CET 2026"

DGX_OTA_VERSION="7.4.0"
DGX_OTA_DATE="Mon Feb  2 19:34:58 CET 2026"

DGX_OTA_VERSION="7.5.0"
DGX_OTA_DATE="Sat Apr  4 15:11:50 CEST 2026"

DGX_OTA_VERSION="7.6.0"
DGX_OTA_DATE="mer. 16 sept. 2026 17:08:05 CEST"
'''
HP_RELEASE = '''DGX_NAME=DGX Spark
DGX_PRETTY_NAME=NVIDIA DGX Spark
DGX_SWBUILD_VERSION=7.2.3
DGX_COMMIT_ID=03dc741
DGX_PLATFORM=HP ZGX Nano G1n AI Station
DGX_OTA_VERSION=7.5.0
DGX_OTA_DATE=Fri Jun 19 14:17:10 EDT 2026
'''
MEMINFO = """MemTotal:       127535084 kB
MemFree:         1234567 kB
MemAvailable:   14386092 kB
SwapTotal:      33554428 kB
CmaTotal:         131072 kB
CmaFree:           69320 kB
"""
SMI_Q = """==============NVSMI LOG==============
Driver Version                                         : 580.178.04
CUDA Version                                           : 13.0
GPU 0000000F:01:00.0
    Product Name                                       : NVIDIA GB10
    VBIOS Version                                      : 9A.0B.25.00.00
    GSP Firmware Version                               : 580.178.04
"""
FWUPD = json.dumps({"Devices": [
    {"Name": "Embedded Controller", "Version": "0x02000005", "Vendor": "Asus", "Serial": "EC-SERIAL-1"},
    {"Name": "UEFI Device Firmware", "Version": "0x03000007", "Vendor": "Asus"},
    {"Name": "UEFI dbx", "Version": "20230501", "Vendor": "Microsoft"}]})
HOLDS = "\n".join(["linux-headers-nvidia-hwe-24.04", "linux-image-nvidia-hwe-24.04",
                   "linux-modules-nvidia-580-open-nvidia-hwe-24.04", "linux-modules-nvidia-fs-nvidia-hwe-24.04",
                   "linux-nvidia-hwe-24.04", "linux-tools-nvidia-hwe-24.04"]) + "\n"
ANSWERS = {
    "nvidia-smi --query-gpu=name,driver_version --format=csv,noheader": ("NVIDIA GB10, 580.178.04\n", None),
    "nvidia-smi -q": (SMI_Q, None),
    "docker version --format {{.Server.Version}}": ("29.6.2\n", None),
    "nvidia-ctk --version": ("NVIDIA Container Toolkit CLI version 1.20.1\ncommit: dffc40b4\n", None),
    "apt-mark showhold": (HOLDS, None),
    "fwupdmgr get-devices --json": (FWUPD, None),
}


class Fixture:
    """A root holding the files a box has, and the answers its commands give."""

    def __init__(self, test, dgx=DGX_RELEASE, meminfo=MEMINFO, release="6.17.0-1032-nvidia",
                 answers=None, dmi=None, core_pattern="|/usr/share/apport/apport -p%p -s%s -- %E\n"):
        self.root = Path(tempfile.mkdtemp(prefix="doctor-root-"))
        test.addCleanup(__import__("shutil").rmtree, self.root, True)
        dmi = {"sys_vendor": "ASUSTeK COMPUTER INC.", "product_name": "GX10", "product_version": "5.36_GX10DGX",
               "board_name": "GX10", "bios_version": "GX10DGX.0105.2026.0505.1153", "bios_date": "05/05/2026",
               "product_serial": "DMI-SERIAL-SHOULD-NEVER-BE-READ"} if dmi is None else dmi
        for key, value in dmi.items():
            self.write(f"sys/class/dmi/id/{key}", value + "\n")
        if dgx is not None:
            self.write("etc/dgx-release", dgx)
        self.write("etc/os-release", 'PRETTY_NAME="Ubuntu 24.04.5 LTS"\nVERSION_ID="24.04"\nID=ubuntu\n')
        if meminfo is not None:
            self.write("proc/meminfo", meminfo)
        self.write("proc/sys/kernel/core_pattern", core_pattern)
        (self.root / "home/u").mkdir(parents=True)
        (self.root / "var/lib").mkdir(parents=True)
        self.answers = dict(ANSWERS, **(answers or {}))
        self.asked, self.read = [], []
        self.box = doctor.Box(root=self.root, run=self.run, release=release, home="/home/u")
        real_read = self.box.read
        self.box.read = lambda path, limit=1 << 20: (self.read.append(path), real_read(path, limit))[1]

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def run(self, argv, timeout):
        key = " ".join(argv)
        self.asked.append((key, timeout))
        if key.startswith("git -C "):
            return "v1.22.5\n", None
        return self.answers.get(key, (None, "not installed"))

    def outputs(self):
        facts = doctor.collect(self.box)
        findings, entry = doctor.evaluate(facts, DATA)
        return facts, findings, entry


def ids(findings):
    return [f["id"] for f in findings]


class TheReferenceBox(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(self)
        self.facts, self.findings, self.entry = self.fx.outputs()

    def test_its_facts_are_read_as_its_tools_print_them(self):
        f = self.facts
        self.assertEqual((f["dgx"]["platform"], f["dgx"]["ota"], f["dgx"]["swbuild"]), ("GX10", "7.6.0", "7.2.3"))
        self.assertEqual(f["dgx"]["ota_history"], ["7.3.1", "7.4.0", "7.5.0", "7.6.0"], "the last OTA is what runs")
        self.assertEqual(f["dgx"]["ota_date"], "mer. 16 sept. 2026 17:08:05 CEST")
        self.assertEqual((f["gpu"]["name"], f["gpu"]["driver"], f["gpu"]["gsp"], f["gpu"]["cuda"]),
                         ("NVIDIA GB10", "580.178.04", "580.178.04", "13.0"))
        self.assertEqual(f["containers"]["docker"]["server"], "29.6.2")
        self.assertEqual(f["containers"]["toolkit"], "1.20.1")
        self.assertEqual(len(f["kernel"]["holds"]), 6)
        self.assertEqual((f["memory"]["total_kb"], f["memory"]["cma_total_kb"], f["memory"]["cma_free_kb"]),
                         (127535084, 131072, 69320))
        self.assertEqual([d["name"] for d in f["firmware"]["devices"]], ["Embedded Controller", "UEFI Device Firmware"])
        self.assertEqual(f["machine"]["product_name"], "GX10")
        self.assertIs(f["crash_reports"]["apport"], True)
        self.assertEqual(f["os"]["pretty"], "Ubuntu 24.04.5 LTS")

    def test_it_is_in_the_matrix_and_only_the_apport_note_applies(self):
        self.assertEqual(self.entry["name"], "ASUS Ascent GX10")
        self.assertEqual(ids(self.findings), ["apport-active"])

    def test_every_command_is_bounded_in_time(self):
        self.assertTrue(self.fx.asked)
        for cmd, timeout in self.fx.asked:
            self.assertTrue(0 < timeout <= 30, cmd)

    def test_the_text_says_what_the_box_is(self):
        text = doctor.render_text(self.facts, self.findings, self.entry)
        for want in ("ASUSTeK COMPUTER INC. GX10", "OTA 7.6.0", "driver 580.178.04, GSP 580.178.04",
                     "6 package(s) held", "121.6 GiB total", "NVIDIA Container Toolkit 1.20.1",
                     "Embedded Controller 0x02000005", "ASUS Ascent GX10: 1 report(s)", "v1.22.5"):
            self.assertIn(want, text)

    def test_brief_shows_only_what_needs_doing(self):
        brief = doctor.render_text(self.facts, self.findings, self.entry, brief=True)
        self.assertIn("nothing known against this box", brief)
        self.assertNotIn("apport", brief, "an info line is not for the installer's output")


class NothingIdentifyingLeaves(unittest.TestCase):
    """The serial numbers: /etc/dgx-release carries the box's, DMI has root-only ones, fwupd
    can print a device's. None of them is in any output, in any mode."""

    def test_no_output_carries_a_serial(self):
        fx = Fixture(self)
        for mode in ([], ["--brief"], ["--json"], ["--report"]):
            out = io.StringIO()
            self.assertEqual(doctor.main(mode, box=fx.box, out=out), 0)
            for secret in (SERIAL, "DMI-SERIAL-SHOULD-NEVER-BE-READ", "EC-SERIAL-1"):
                self.assertNotIn(secret, out.getvalue(), (mode, secret))

    def test_no_serial_file_is_even_opened(self):
        fx = Fixture(self)
        fx.outputs()
        self.assertFalse([p for p in fx.read if "serial" in p], fx.read)

    def test_an_unquoted_serial_line_is_skipped_too(self):
        fx = Fixture(self, dgx=f"DGX_PLATFORM=GX10\nDGX_SERIAL_NUMBER={SERIAL}\nDGX_OTA_VERSION=7.6.0\n")
        facts, _, _ = fx.outputs()
        self.assertNotIn(SERIAL, json.dumps(facts))
        self.assertEqual(facts["dgx"]["ota"], "7.6.0")

    def test_the_report_writes_the_home_directory_as_a_tilde(self):
        fx = Fixture(self, answers={"docker version --format {{.Server.Version}}":
                                    (None, f"permission denied at {Path.home()}/.docker/config.json")})
        facts, findings, entry = fx.outputs()
        report = doctor.render_report(facts, findings, entry)
        self.assertIn("~/.docker/config.json", report)
        self.assertNotIn(str(Path.home()) + "/", report)


class TheIssue26Box(unittest.TestCase):
    def test_the_hp_box_on_580_159_03_is_warned_about_its_driver(self):
        fx = Fixture(self, dgx=HP_RELEASE, dmi={"sys_vendor": "HP", "product_name": "unknown to us"},
                     answers={"nvidia-smi --query-gpu=name,driver_version --format=csv,noheader":
                              ("NVIDIA GB10, 580.159.03\n", None)})
        facts, findings, entry = fx.outputs()
        self.assertEqual(entry["name"], "HP ZGX Nano G1n", "matched on DGX OS's own platform name")
        self.assertEqual(facts["dgx"]["ota"], "7.5.0", "an unquoted file is read too")
        driver = next(f for f in findings if f["id"] == "driver-580.159.03")
        self.assertEqual(driver["level"], "warn")
        self.assertTrue(any("issues/26" in e for e in driver["evidence"]))
        self.assertTrue(any("40948" in e for e in driver["evidence"]))

    def test_after_its_update_that_warning_is_gone(self):
        fx = Fixture(self, dgx=HP_RELEASE)
        _, findings, _ = fx.outputs()
        self.assertNotIn("driver-580.159.03", ids(findings))


class TheKnownIssues(unittest.TestCase):
    def test_kernel_7_0_0_1019_and_its_memory_signature(self):
        sig = MEMINFO.replace("CmaTotal:         131072 kB", "CmaTotal:              0 kB") \
                     .replace("CmaFree:           69320 kB", "CmaFree:          220924 kB")
        fx = Fixture(self, release="7.0.0-1019-nvidia", meminfo=sig)
        found = ids(fx.outputs()[1])
        self.assertIn("kernel-7.0.0-1019", found)
        self.assertIn("cma-reserved-uncounted", found)

    def test_a_box_without_cma_at_all_is_not_that_signature(self):
        none = MEMINFO.replace("CmaTotal:         131072 kB", "CmaTotal:              0 kB") \
                      .replace("CmaFree:           69320 kB", "CmaFree:               0 kB")
        self.assertNotIn("cma-reserved-uncounted", ids(Fixture(self, meminfo=none).outputs()[1]))

    def test_a_later_7_0_kernel_is_not_the_one_nvidia_named(self):
        self.assertNotIn("kernel-7.0.0-1019", ids(Fixture(self, release="7.0.0-10190-nvidia").outputs()[1]))
        self.assertNotIn("kernel-7.0.0-1019", ids(Fixture(self, release="7.0.0-1020-nvidia").outputs()[1]))

    def test_a_gpu_the_driver_cannot_bring_up_is_a_failure(self):
        fx = Fixture(self, answers={"nvidia-smi --query-gpu=name,driver_version --format=csv,noheader":
                                    ("No devices were found\n", "exit 6")})
        facts, findings, _ = fx.outputs()
        self.assertEqual(findings[0]["id"], "gpu-unreachable", "failures come first")
        self.assertEqual(findings[0]["level"], "fail")
        self.assertIn("378200", " ".join(findings[0]["evidence"]))
        self.assertIn("not reachable: exit 6", doctor.render_text(facts, findings, None))

    def test_nvidia_smi_printing_no_device_with_exit_0_is_unreachable_too(self):
        fx = Fixture(self, answers={"nvidia-smi --query-gpu=name,driver_version --format=csv,noheader":
                                    ("No devices were found\n", None)})
        facts, findings, _ = fx.outputs()
        self.assertEqual(facts["gpu"]["error"], "No devices were found")
        self.assertIn("gpu-unreachable", ids(findings))

    def test_no_nvidia_smi_at_all(self):
        fx = Fixture(self, answers={"nvidia-smi --query-gpu=name,driver_version --format=csv,noheader":
                                    (None, "not installed")})
        facts, findings, _ = fx.outputs()
        self.assertEqual(facts["gpu"]["error"], "not installed")
        self.assertIn("gpu-unreachable", ids(findings))

    def test_the_memory_floor_is_the_installers_own_arithmetic(self):
        """install.sh: TOTAL_GB=int(MemTotal kB / 1048576), refused under 110."""
        at = MEMINFO.replace("127535084", str(110 * 1048576))
        under = MEMINFO.replace("127535084", str(110 * 1048576 - 1))
        self.assertNotIn("memory-below-110", ids(Fixture(self, meminfo=at).outputs()[1]))
        self.assertIn("memory-below-110", ids(Fixture(self, meminfo=under).outputs()[1]))
        install = (REPO / "install.sh").read_text()
        self.assertIn("int($2/1048576)", install)
        self.assertIn('[ "$TOTAL_GB" -ge 110 ]', install)

    def test_docker_this_user_cannot_reach(self):
        fx = Fixture(self, answers={"docker version --format {{.Server.Version}}":
                                    ("", "permission denied while trying to connect to the docker API")})
        facts, findings, _ = fx.outputs()
        self.assertIn("docker-unreachable", ids(findings))
        self.assertIn("docker: permission denied", doctor.render_text(facts, findings, None))

    def test_no_container_toolkit(self):
        fx = Fixture(self, answers={"nvidia-ctk --version": (None, "not installed")})
        self.assertIn("toolkit-missing", ids(fx.outputs()[1]))

    def test_a_gpu_that_is_not_a_gb10(self):
        fx = Fixture(self, answers={"nvidia-smi --query-gpu=name,driver_version --format=csv,noheader":
                                    ("NVIDIA RTX PRO 6000, 580.178.04\n", None)})
        self.assertIn("gpu-not-gb10", ids(fx.outputs()[1]))

    def test_a_box_nobody_reported_yet(self):
        fx = Fixture(self, dgx='DGX_PLATFORM="Some Future Box"\nDGX_OTA_VERSION="7.7.0"\n',
                     dmi={"sys_vendor": "Someone", "product_name": "Box"})
        facts, findings, entry = fx.outputs()
        self.assertIsNone(entry)
        self.assertEqual(ids(findings)[-1], "platform-not-reported")
        self.assertIn("selftest.py", findings[-1]["action"])


class WhatCouldNotBeRead(unittest.TestCase):
    """A fact that could not be read is unknown, and an unknown fact matches no known issue."""

    def test_a_bare_machine(self):
        fx = Fixture(self, dgx=None, meminfo=None, dmi={}, core_pattern="",
                     answers={k: (None, "not installed") for k in ANSWERS})
        facts, findings, entry = fx.outputs()
        self.assertEqual(facts["dgx"], {"present": False})
        self.assertIsNone(facts["memory"]["total_kb"])
        self.assertIsNone(facts["crash_reports"]["apport"])
        self.assertIsNone(facts["kernel"]["holds"])
        self.assertEqual(facts["firmware"], {"devices": None, "error": "not installed"})
        found = ids(findings)
        self.assertNotIn("memory-below-110", found, "an unread memory size is not a small one")
        self.assertNotIn("gpu-not-gb10", found)
        self.assertIn("gpu-unreachable", found)
        text = doctor.render_text(facts, findings, entry)
        self.assertIn("no /etc/dgx-release (not DGX OS?)", text)
        self.assertIn("unknown total", text)
        self.assertIn("firmware       unknown (not installed)", text)

    def test_fwupd_printing_something_that_is_not_json(self):
        fx = Fixture(self, answers={"fwupdmgr get-devices --json": ("WARNING: something\n", None)})
        self.assertEqual(fx.outputs()[0]["firmware"]["error"], "fwupdmgr printed no JSON")

    def test_nvidia_smi_q_that_does_not_answer_leaves_gsp_unknown(self):
        fx = Fixture(self, answers={"nvidia-smi -q": (None, "no answer in 30 s")})
        facts, findings, _ = fx.outputs()
        self.assertEqual((facts["gpu"]["driver"], facts["gpu"]["gsp"]), ("580.178.04", None))
        self.assertNotIn("gpu-unreachable", ids(findings))


class TheCommandLine(unittest.TestCase):
    def test_it_always_exits_0_on_what_it_finds(self):
        fx = Fixture(self, answers={"nvidia-smi --query-gpu=name,driver_version --format=csv,noheader": (None, "x")})
        out = io.StringIO()
        self.assertEqual(doctor.main([], box=fx.box, out=out), 0, "a failure found is still a report")
        self.assertIn("fail  nvidia-smi does not reach the GPU", out.getvalue())

    def test_json_is_json(self):
        out = io.StringIO()
        self.assertEqual(doctor.main(["--json"], box=Fixture(self).box, out=out), 0)
        j = json.loads(out.getvalue())
        self.assertEqual(j["platform"], "ASUS Ascent GX10")
        self.assertEqual([f["id"] for f in j["findings"]], ["apport-active"])

    def test_help_and_a_wrong_argument(self):
        out = io.StringIO()
        self.assertEqual(doctor.main(["--help"], out=out), 0)
        self.assertIn("--report", out.getvalue())
        out = io.StringIO()
        self.assertEqual(doctor.main(["--frobnicate"], out=out), 2)
        self.assertEqual(doctor.main(["--json", "--report"], out=io.StringIO()), 2)

    def test_the_real_runner(self):
        self.assertEqual(doctor._run(["no-such-command-anywhere"], 5), (None, "not installed"))
        out, err = doctor._run(["sh", "-c", "echo hi; echo why >&2; exit 3"], 5)
        self.assertEqual((out, err), ("hi\n", "why"))
        self.assertEqual(doctor._run(["sh", "-c", "exit 4"], 5), ("", "exit 4"))
        self.assertEqual(doctor._run(["sleep", "5"], 1), (None, "no answer in 1 s"))
        self.assertEqual(doctor._run(["sh", "-c", "echo ok"], 5), ("ok\n", None))

    def test_the_script_runs_as_a_program(self):
        r = subprocess.run([sys.executable, str(REPO / "doctor.py"), "--matrix"], capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("| ASUS Ascent GX10 |", r.stdout)


class TheData(unittest.TestCase):
    """platforms.json: every known issue is checkable and sourced, and the doc shows the matrix."""

    def test_every_rule_is_a_kind_the_doctor_checks(self):
        facts = doctor.collect(Fixture(self).box)
        for rule in DATA["known_issues"]:
            doctor._matches(rule, facts)          # raises on a kind nobody implemented
        with self.assertRaises(ValueError):
            doctor._matches({"kind": "nobody-checks-this"}, facts)

    def test_ids_levels_texts_and_evidence(self):
        seen = set()
        for rule in DATA["known_issues"]:
            self.assertNotIn(rule["id"], seen); seen.add(rule["id"])
            self.assertIn(rule["level"], doctor.LEVELS)
            for k in ("title", "detail", "action"):
                self.assertTrue(rule[k].strip(), (rule["id"], k))
            for url in rule["evidence"]:
                self.assertTrue(url.startswith("https://"), url)
            if rule["level"] in ("warn",) and rule["id"] not in ("gpu-not-gb10", "toolkit-missing"):
                self.assertTrue(rule["evidence"], f"{rule['id']}: a warning about a version needs its evidence")

    def test_every_platform_report_is_complete(self):
        for entry in DATA["platforms"]:
            self.assertTrue(entry["match"].get("dgx_platform") or entry["match"].get("dmi_product"))
            for r in entry["reports"]:
                for k in ("date", "ota", "kernel", "driver", "lanes", "result", "source"):
                    self.assertTrue(str(r[k]).strip(), (entry["name"], k))
                self.assertRegex(r["date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_the_doc_lists_the_known_issues_platforms_json_holds(self):
        doc = (REPO / "docs/platforms.md").read_text()
        m = re.search(r"<!-- issues:start -->\n(.*?)<!-- issues:end -->", doc, re.S)
        self.assertIsNotNone(m, "docs/platforms.md has no known-issues block")
        self.assertEqual(m.group(1), doctor.render_issues(DATA))

    def test_the_doc_shows_the_matrix_doctor_prints(self):
        doc = (REPO / "docs/platforms.md").read_text()
        m = re.search(r"<!-- matrix:start -->\n(.*?)<!-- matrix:end -->", doc, re.S)
        self.assertIsNotNone(m, "docs/platforms.md has no matrix block")
        self.assertEqual(m.group(1), doctor.render_matrix(DATA),
                         "docs/platforms.md and platforms.json disagree: paste ./doctor.py --matrix")


class TheInstallerAsksIt(unittest.TestCase):
    """install.sh prints the doctor's findings at the end of its preflight and goes on, whatever
    they are and even when the doctor itself fails."""
    INSTALL = (REPO / "install.sh").read_text()

    def test_it_runs_after_the_preflight_and_is_bounded(self):
        ok = self.INSTALL.index('echo "OK (aarch64, ${TOTAL_GB} GB RAM, ${FREE_DISK_GB} GB free)"')
        call = self.INSTALL.index('timeout 180 python3 "$REPO_DIR/doctor.py" --brief')
        self.assertLess(ok, call, "after the checks that refuse, never before them")
        self.assertLess(call, self.INSTALL.index('step "2/10'), "before anything is pulled")

    def test_a_doctor_that_fails_does_not_stop_the_install(self):
        line = next(ln for ln in self.INSTALL.splitlines() if "doctor.py\" --brief" in ln)
        d = Path(tempfile.mkdtemp(prefix="doctor-install-"))
        self.addCleanup(__import__("shutil").rmtree, d, True)
        (d / "doctor.py").write_text("import sys\nprint('broken')\nsys.exit(1)\n")
        script = f'set -euo pipefail\nREPO_DIR="{d}"\n{line}\necho "STEP 2 REACHED"\n'
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("doctor.py did not answer; the install goes on", r.stdout)
        self.assertIn("STEP 2 REACHED", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
