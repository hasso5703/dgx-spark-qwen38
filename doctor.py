#!/usr/bin/env python3
"""What this box is, and what is known about it: a read-only check of a GB10 machine.

Usage: doctor.py             the full report
       doctor.py --brief     the findings only (what install.sh prints)
       doctor.py --json      the facts and the findings, as JSON
       doctor.py --report    anonymised Markdown to paste into an issue
       doctor.py --matrix    the boxes reported so far, as the table of docs/platforms.md
       doctor.py --why FILE  what is known about an engine crash in a pasted journal (- reads stdin)

Every GB10 box runs this repo's engines in the same containers, pinned by digest, so what
differs from one box to the next is underneath them: the maker's firmware (BIOS, embedded
controller), the DGX OS release (OTA), the kernel, the NVIDIA driver and its GSP firmware,
Docker and the NVIDIA Container Toolkit. Issue #26 is that difference: the same repo and the
same images, a driver (580.159.03) that killed engines, and no such death after the update.
NVIDIA's own update guide covers the Founders Edition only: "Devices from other manufacturers
might have different update procedures" (OS and Component Update Guide, 2026-09-10).

It reads and never writes: the DMI fields any user can read (the serial numbers are root-only
and are not read), /etc/dgx-release by a list of keys (that file also holds the box's serial
number), /etc/os-release, /proc/meminfo, the kernel's core_pattern, and what nvidia-smi,
docker, nvidia-ctk, apt-mark and fwupdmgr print. Every command is bounded in time; one that is
missing or does not answer leaves its fact unknown, never guessed. It needs no sudo, sends
nothing anywhere, and exits 0 whatever it finds: it warns, it never blocks.

The known issues and the boxes reported so far live in platforms.json, each with its
evidence; docs/platforms.md shows the same matrix to people.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "platforms.json"

# /etc/dgx-release also carries DGX_SERIAL_NUMBER: only these keys are ever read from it
DGX_KEYS = ("DGX_NAME", "DGX_PRETTY_NAME", "DGX_SWBUILD_VERSION", "DGX_SWBUILD_DATE",
            "DGX_PLATFORM", "DGX_OTA_VERSION", "DGX_OTA_DATE")
# readable by any user on DGX OS; product_serial, board_serial and chassis_serial are root-only
DMI_KEYS = ("sys_vendor", "product_name", "product_version", "board_name",
            "bios_version", "bios_date")
# the firmware fwupd reports that differs between makers; never a serial
FIRMWARE_NAMES = ("Embedded Controller", "UEFI Device Firmware", "System Firmware")
LEVELS = ("ok", "info", "warn", "fail")


def _run(argv, timeout):
    """(stdout, error) of a read-only command; error is None when it exited 0."""
    exe = shutil.which(argv[0])
    if not exe:
        return None, "not installed"
    try:
        r = subprocess.run([exe] + list(argv[1:]), capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL,
                           env={**os.environ, "LC_ALL": "C"})
    except subprocess.TimeoutExpired:
        return None, f"no answer in {timeout} s"
    except OSError as e:
        return None, str(e)
    if r.returncode != 0:
        said = (r.stderr.strip() or r.stdout.strip()).splitlines()
        return r.stdout, (said[-1] if said else f"exit {r.returncode}")[:200]
    return r.stdout, None


class Box:
    """Where the facts come from: this machine, or a fixture in the tests. `root` prefixes every
    path read, `run` answers every command, `release` is the kernel release."""

    def __init__(self, root="/", run=_run, release=None, home=None):
        self.root = Path(root)
        self.run = run
        self.release = release if release is not None else os.uname().release
        self.home = Path(home) if home else Path.home()

    def read(self, path, limit=1 << 20):
        try:
            with open(self.root / path.lstrip("/"), encoding="utf-8", errors="replace") as f:
                return f.read(limit)
        except OSError:
            return None

    def free_gib(self, path):
        try:
            st = os.statvfs(self.root / str(path).lstrip("/"))
        except OSError:
            return None
        return round(st.f_frsize * st.f_bavail / 2**30, 1)


# ---- facts --------------------------------------------------------------------------------

def read_machine(box):
    out = {}
    for key in DMI_KEYS:
        text = box.read(f"/sys/class/dmi/id/{key}", limit=256)
        out[key] = text.strip() if text and text.strip() else None
    return out


def read_dgx(box):
    """The DGX OS release. The file keeps one DGX_OTA_VERSION line per OTA ever applied (four
    on the reference box: 7.3.1 to 7.6.0); the last one is what runs."""
    text = box.read("/etc/dgx-release")
    if text is None:
        return {"present": False}
    seen = {}
    otas = []
    for line in text.splitlines():
        m = re.match(r"\s*([A-Z_]+)\s*=\s*\"?([^\"]*)\"?\s*$", line)
        if not m or m.group(1) not in DGX_KEYS:
            continue
        key, value = m.group(1), m.group(2).strip()
        seen[key] = value
        if key == "DGX_OTA_VERSION":
            otas.append(value)
    return {"present": True, "platform": seen.get("DGX_PLATFORM"),
            "name": seen.get("DGX_PRETTY_NAME") or seen.get("DGX_NAME"),
            "swbuild": seen.get("DGX_SWBUILD_VERSION"),
            "ota": otas[-1] if otas else None, "ota_date": seen.get("DGX_OTA_DATE"),
            "ota_history": otas}


def read_os(box):
    text = box.read("/etc/os-release") or ""
    vals = dict(re.findall(r"^([A-Z_]+)=\"?([^\"\n]*)\"?$", text, re.M))
    return {"pretty": vals.get("PRETTY_NAME"), "version_id": vals.get("VERSION_ID")}


def read_kernel(box):
    out, err = box.run(["apt-mark", "showhold"], 15)
    holds = sorted(ln.strip() for ln in (out or "").splitlines() if ln.strip()) if err is None else None
    return {"release": box.release, "holds": holds}


def read_memory(box):
    text = box.read("/proc/meminfo") or ""
    kb = {k: int(v) for k, v in re.findall(r"^(\w+):\s+(\d+) kB$", text, re.M)}
    return {"total_kb": kb.get("MemTotal"), "available_kb": kb.get("MemAvailable"),
            "cma_total_kb": kb.get("CmaTotal"), "cma_free_kb": kb.get("CmaFree")}


def gib(kb):
    return None if kb is None else round(kb / 2**20, 1)


def read_gpu(box):
    out, err = box.run(["nvidia-smi", "--query-gpu=name,driver_version",
                        "--format=csv,noheader"], 20)
    gpu = {"name": None, "driver": None, "gsp": None, "cuda": None, "error": None}
    line = (out or "").strip().splitlines()[0] if (out or "").strip() else ""
    if err is not None or "," not in line:
        # nvidia-smi says "No devices were found" when the driver cannot bring the GPU up
        gpu["error"] = err or (line[:200] if line else "no answer")
        return gpu
    gpu["name"], gpu["driver"] = (part.strip() for part in line.split(",", 1))
    q, qerr = box.run(["nvidia-smi", "-q"], 30)
    if qerr is None and q:
        m = re.search(r"^\s*GSP Firmware Version\s*:\s*(\S+)", q, re.M)
        gpu["gsp"] = m.group(1) if m else None
        m = re.search(r"^\s*CUDA Version\s*:\s*(\S+)", q, re.M)
        gpu["cuda"] = m.group(1) if m else None
    return gpu


def read_containers(box):
    out, err = box.run(["docker", "version", "--format", "{{.Server.Version}}"], 20)
    docker = {"server": out.strip() if err is None and out and out.strip() else None,
              "error": None if err is None else err}
    out, err = box.run(["nvidia-ctk", "--version"], 10)
    m = re.search(r"version\s+(\S+)", out or "") if err is None else None
    return {"docker": docker, "toolkit": m.group(1) if m else None,
            "toolkit_error": None if m else (err or "no version line")}


def read_firmware(box):
    out, err = box.run(["fwupdmgr", "get-devices", "--json"], 30)
    if err is not None or not out:
        return {"devices": None, "error": err or "no answer"}
    try:
        devices = json.loads(out).get("Devices") or []
    except ValueError:
        return {"devices": None, "error": "fwupdmgr printed no JSON"}
    keep = [{"name": d.get("Name"), "version": d.get("Version"), "vendor": d.get("Vendor")}
            for d in devices if d.get("Name") in FIRMWARE_NAMES]
    return {"devices": keep, "error": None}


def read_disk(box):
    return {"home_free_gib": box.free_gib(box.home), "var_lib_free_gib": box.free_gib("/var/lib")}


def read_crash_reports(box):
    pattern = (box.read("/proc/sys/kernel/core_pattern", limit=512) or "").strip()
    return {"apport": pattern.startswith("|") and "apport" in pattern if pattern else None}


def read_repo(box):
    out, err = box.run(["git", "-C", str(HERE), "describe", "--tags", "--always"], 10)
    return {"version": out.strip() if err is None and out and out.strip() else None}


def collect(box=None):
    box = box or Box()
    return {"machine": read_machine(box), "dgx": read_dgx(box), "os": read_os(box),
            "kernel": read_kernel(box), "memory": read_memory(box), "gpu": read_gpu(box),
            "containers": read_containers(box), "firmware": read_firmware(box),
            "disk": read_disk(box), "crash_reports": read_crash_reports(box),
            "repo": read_repo(box)}


# ---- what is known ------------------------------------------------------------------------

def load_data(path=DATA):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _matches(rule, facts):
    """Whether one known issue applies to these facts. Every kind is a measured fact, and an
    unknown fact never matches: a box is not warned about what could not be read."""
    kind, p = rule["kind"], rule.get("params", {})
    gpu, mem = facts["gpu"], facts["memory"]
    if kind == "driver_is":
        return gpu.get("driver") in p["versions"]
    if kind == "kernel_starts":
        rel = facts["kernel"].get("release") or ""
        return any(rel.startswith(x) for x in p["prefixes"])
    if kind == "cma_reserved_uncounted":
        return mem.get("cma_total_kb") == 0 and (mem.get("cma_free_kb") or 0) > 0
    if kind == "gpu_unreachable":
        return gpu.get("error") is not None
    if kind == "gpu_not":
        return gpu.get("name") is not None and p["contains"] not in gpu["name"]
    if kind == "memory_below_gib":
        # the installer's own arithmetic: MemTotal in whole GiB, rounded down (install.sh)
        return mem.get("total_kb") is not None and mem["total_kb"] // 1048576 < p["gib"]
    if kind == "docker_unreachable":
        return facts["containers"]["docker"].get("error") is not None
    if kind == "toolkit_missing":
        return facts["containers"].get("toolkit") is None
    if kind == "apport_active":
        return facts["crash_reports"].get("apport") is True
    raise ValueError(f"unknown rule kind {kind!r}")


def platform_of(facts, data):
    """The matrix entry this box belongs to, by DGX OS's own platform name or the DMI model."""
    plat = facts["dgx"].get("platform")
    product = facts["machine"].get("product_name")
    for entry in data["platforms"]:
        m = entry["match"]
        if plat and plat in m.get("dgx_platform", []):
            return entry
        if product and product in m.get("dmi_product", []):
            return entry
    return None


def evaluate(facts, data):
    found = []
    for rule in data["known_issues"]:
        if _matches(rule, facts):
            found.append({k: rule[k] for k in ("id", "level", "title", "detail", "action", "evidence")})
    entry = platform_of(facts, data)
    if entry is None:
        found.append({"id": "platform-not-reported", "level": "info",
                      "title": "This make and model is not in the matrix yet",
                      "detail": "The engines run in the same pinned containers on every GB10 box, so it "
                                "should work; nobody has reported this box yet.",
                      "action": "Run ./selftest.py once the install is done, and share its result with "
                                "./doctor.py --report in an issue: it becomes a line of docs/platforms.md.",
                      "evidence": []})
    found.sort(key=lambda f: -LEVELS.index(f["level"]))
    return found, entry


XID = re.compile(r"NVRM: Xid \([^)]*\): (\d+)")


def crash_cause(lines, kernel_lines, data):
    """What is known about why an engine run ended: the first known signature of
    platforms.json found in that run's own lines (the newest line of it), and the Xid numbers
    the driver logged in the kernel's lines of the same minutes. None when nothing known
    matched: a crash with no known cause is said to have none, never given one."""
    found = None
    for sig in data["crash_signatures"]:
        rx = re.compile(sig["pattern"])
        hit = next((ln for ln in reversed(lines) if rx.search(ln)), None)
        if hit is not None:
            found = {k: sig[k] for k in ("id", "title", "meaning", "action", "evidence")}
            found["line"] = hit.strip()[-300:]
            break
    xids = sorted({int(m.group(1)) for ln in kernel_lines for m in [XID.search(ln)] if m})
    if found is None and not xids:
        return None
    known, catalog = data["xids"]["known"], data["xids"]["catalog"]
    said = [f"Xid {x}: {known[str(x)]}" if str(x) in known else f"Xid {x}: see NVIDIA's Xid catalog"
            for x in xids]
    if found is None:
        found = {"id": "xid", "title": "The GPU driver logged an Xid as the engine stopped",
                 "meaning": "No known line in the engine's own output; the kernel log has the driver's.",
                 "action": "Report the engine's journal and the kernel log of that minute with ./doctor.py --report.",
                 "evidence": [], "line": None}
    found["xids"], found["xid_said"] = xids, said
    if xids and catalog not in found["evidence"]:
        found["evidence"] = found["evidence"] + [catalog]
    return found


def render_why(cause):
    if cause is None:
        return ("Nothing known in these lines: no signature of platforms.json and no Xid. That is no "
                "proof of anything; report them with ./doctor.py --report.\n")
    out = [f"Known: {cause['title']}"]
    if cause.get("line"):
        out.append(f"  the line: {cause['line']}")
    out += [f"  {x}" for x in cause["xid_said"]]
    out.append(f"  what it means: {cause['meaning']}")
    out.append(f"  what to do: {cause['action']}")
    if cause["evidence"]:
        out.append("  evidence: " + " ".join(cause["evidence"]))
    return "\n".join(out) + "\n"


# ---- output -------------------------------------------------------------------------------

def _v(x, unit=""):
    return "unknown" if x is None else f"{x}{unit}"


def summary_lines(facts):
    m, d, g, c = facts["machine"], facts["dgx"], facts["gpu"], facts["containers"]
    mem, k = facts["memory"], facts["kernel"]
    maker = " ".join(x for x in (m.get("sys_vendor"), m.get("product_name")) if x) or "unknown"
    bios = f" (BIOS {m['bios_version']}, {_v(m.get('bios_date'))})" if m.get("bios_version") else ""
    if d.get("present"):
        dgx = f"{_v(d.get('platform'))}, OTA {_v(d.get('ota'))} (build {_v(d.get('swbuild'))})"
    else:
        dgx = "no /etc/dgx-release (not DGX OS?)"
    holds = k.get("holds")
    held = "" if holds is None else (f" ({len(holds)} package(s) held)" if holds else " (nothing held)")
    if g.get("error"):
        gpu = f"not reachable: {g['error']}"
    else:
        gpu = f"{_v(g.get('name'))}, driver {_v(g.get('driver'))}, GSP {_v(g.get('gsp'))}, CUDA {_v(g.get('cuda'))}"
    cma = mem.get("cma_total_kb")
    memory = (f"{_v(gib(mem.get('total_kb')), ' GiB')} total, {_v(gib(mem.get('available_kb')), ' GiB')} available"
              + ("" if cma is None else f", CMA {cma // 1024} MiB"))
    dk = c["docker"]
    containers = (f"docker {dk['server']}" if dk.get("server") else f"docker: {dk.get('error')}") + \
                 (f", NVIDIA Container Toolkit {c['toolkit']}" if c.get("toolkit") else ", no NVIDIA Container Toolkit CLI")
    fw = facts["firmware"]
    if fw.get("devices"):
        firmware = ", ".join(f"{x['name']} {_v(x.get('version'))}" for x in fw["devices"])
    else:
        firmware = f"unknown ({fw.get('error') or 'nothing reported'})"
    disk = facts["disk"]
    return [("maker / model", maker + bios),
            ("DGX OS", dgx + (f", {facts['os']['pretty']}" if facts["os"].get("pretty") else "")),
            ("kernel", _v(k.get("release")) + held),
            ("GPU", gpu), ("memory", memory), ("containers", containers),
            ("firmware", firmware),
            ("free disk", f"home {_v(disk.get('home_free_gib'), ' GiB')}, /var/lib {_v(disk.get('var_lib_free_gib'), ' GiB')}"),
            ("this repo", _v(facts["repo"].get("version")))]


def render_text(facts, findings, entry, brief=False):
    out = []
    if not brief:
        out.append("Platform check (doctor.py: read-only, nothing was changed)")
        out.append("")
        out += [f"  {k:<14} {v}" for k, v in summary_lines(facts)]
        if entry:
            out.append(f"  {'matrix':<14} {entry['name']}: {len(entry['reports'])} report(s) in docs/platforms.md")
        out.append("")
    shown = [f for f in findings if not brief or f["level"] in ("warn", "fail")]
    if not shown:
        out.append("  ok    nothing known against this box" + ("" if not brief else " (./doctor.py for the details)"))
    for f in shown:
        out.append(f"  {f['level']:<5} {f['title']}")
        if not brief:
            out.append(f"        {f['detail']}")
        out.append(f"        what to do: {f['action']}")
        if f["evidence"] and not brief:
            out.append("        evidence: " + " ".join(f["evidence"]))
    return "\n".join(out) + "\n"


def render_report(facts, findings, entry):
    """Markdown for an issue. Nothing identifying is in the facts to begin with (no hostname, no
    address, no serial number is ever read); the home directory is written as ~."""
    lines = ["<details><summary>doctor.py --report</summary>", "", "| | |", "|---|---|"]
    lines += [f"| {k} | {v.replace('|', '/')} |" for k, v in summary_lines(facts)]
    lines.append(f"| matrix | {entry['name'] if entry else 'not reported yet'} |")
    lines.append(f"| findings | {', '.join(f['id'] for f in findings) or 'none'} |")
    lines += ["", "</details>", ""]
    text = "\n".join(lines)
    home = str(Path.home())
    return text.replace(home, "~") if home not in ("/", "") else text


def render_issues(data):
    rows = ["| id | level | what it checks |", "|---|---|---|"]
    rows += [f"| `{r['id']}` | {r['level']} | {r['title']} |" for r in data["known_issues"]]
    return "\n".join(rows) + "\n"


def render_signatures(data):
    rows = ["| id | a line that contains | what it is |", "|---|---|---|"]
    rows += [f"| `{x['id']}` | `{x['pattern'].replace('|', ' or ')}` | {x['title']} |"
             for x in data["crash_signatures"]]
    return "\n".join(rows) + "\n"


def render_matrix(data):
    rows = ["| make and model | DGX OS (OTA) | kernel | driver | lanes | result | date | source |",
            "|---|---|---|---|---|---|---|---|"]
    for entry in data["platforms"]:
        for r in entry["reports"]:
            rows.append(f"| {entry['name']} | {r['ota']} | {r['kernel']} | {r['driver']} | {r['lanes']} | "
                        f"{r['result']} | {r['date']} | {r['source']} |")
    return "\n".join(rows) + "\n"


def main(argv=None, box=None, data_path=DATA, out=sys.stdout):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--why":
        data = load_data(data_path)
        try:
            src = sys.stdin if args[1] == "-" else open(args[1], encoding="utf-8", errors="replace")
        except OSError as e:
            out.write(f"cannot read {args[1]}: {e.strerror}\n")
            return 2
        with src:
            lines = src.read().splitlines()
        out.write(render_why(crash_cause(lines, [ln for ln in lines if "NVRM: Xid" in ln], data)))
        return 0
    known = {"--brief", "--json", "--report", "--matrix", "-h", "--help"}
    if any(a not in known for a in args) or len(args) > 1:
        out.write(__doc__.split("\n\n", 1)[0] + "\n")
        return 2
    if args and args[0] in ("-h", "--help"):
        out.write(__doc__)
        return 0
    data = load_data(data_path)
    if args == ["--matrix"]:
        out.write(render_matrix(data))
        return 0
    facts = collect(box)
    findings, entry = evaluate(facts, data)
    if args == ["--json"]:
        out.write(json.dumps({"facts": facts, "findings": findings,
                              "platform": entry["name"] if entry else None}, indent=1) + "\n")
    elif args == ["--report"]:
        out.write(render_report(facts, findings, entry))
    else:
        out.write(render_text(facts, findings, entry, brief=args == ["--brief"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
