#!/usr/bin/env python3
"""lanes.toml is the one list of the lanes; this says where the repo agrees with it, and writes it out.

  tools/lanegen.py check     every list this knows how to read, against lanes.toml: the units'
                             Conflicts= and After=, the proxy's After=, the cockpit's sudoers lines,
                             the page's tables, switch-model.sh's guard, the CI's flag lanes.
                             Exit 1 if any disagrees; each one is named, with its file.
  tools/lanegen.py render    write the Conflicts= and After= lines of every unit template from it
  tools/lanegen.py shell     the lists as bash arrays, for a script to source instead of spelling them
  tools/lanegen.py json      the manifest as JSON, for the cockpit to serve to its page

Phase 1 of the migration: nothing reads lanes.toml at run time yet, `check` proves the manifest says
what the repo says, and each later commit replaces one hand-kept list by a read of it.
"""
import json
import pathlib
import re
import subprocess
import sys
import tomllib

HERE = pathlib.Path(__file__).resolve().parent
_top = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
REPO = pathlib.Path(_top.stdout.strip()) if _top.returncode == 0 else HERE.parent
# the repo's own lanes.toml once adopted, else the one beside this prototype
MANIFEST = REPO / "lanes.toml" if (REPO / "lanes.toml").exists() else HERE.parent / "lanes.toml"


def lanes():
    return tomllib.loads(MANIFEST.read_text())["lane"]


def units(ls, pred=lambda lane: True):
    return [lane["unit"] for lane in ls if pred(lane)]


def ordering_lines(ls, lane):
    """The Conflicts= and After= this lane's units carry: every other lane, in the manifest's order."""
    others = [u for u in units(ls) if u != lane["unit"]]
    after = ["network-online.target"] + (["docker.service"] if lane["docker"] else []) + others
    return "Conflicts=" + " ".join(others), "After=" + " ".join(after)


def sudoers_lines(ls):
    out = []
    for lane in ls:
        for verb in ("start", "stop", "restart", "enable", "disable"):
            out.append(f"__USER__ ALL=(root) NOPASSWD: /usr/bin/systemctl {verb} {lane['unit']}")
        if lane.get("stage"):
            out.append(f"__USER__ ALL=(root) NOPASSWD: /usr/bin/install -m 644 __HOME__/.config/qwen38/{lane['stage']}"
                       f" /etc/systemd/system/{lane['unit']}")
    return out


def js_table(js, name):
    """{key: value} of `const NAME = {[CONST]: value, ...}` in base.js, keys resolved to unit names."""
    consts = dict(re.findall(r"\b([A-Z][A-Z0-9_]*)\s*=\s*'(qwen38-[a-z0-9-]+\.service)'", js))
    m = re.search(rf"const {name} = \{{(.*?)\}};", js, re.S)
    if not m:
        return None
    body = m.group(1)
    out = {}
    for key, value in re.findall(r"\[([A-Z0-9_]+)\]:\s*(\[[^\]]*\]|'[^']*'|\d+)", body):
        out[consts.get(key, key)] = json.loads(value.replace("'", '"'))
    return out


def check():
    ls = lanes()
    n_lists = 0
    bad = {}                    # each list that disagrees: what is wrong with it

    def compare(where, got, want):
        nonlocal n_lists
        n_lists += 1
        if got != want:
            bad[where] = f"has  {got}\n    want {want}"

    # 1. every engine template: Conflicts= and After=
    for lane in ls:
        conflicts, after = ordering_lines(ls, lane)
        for t in lane["templates"]:
            text = (REPO / t).read_text() if (REPO / t).exists() else ""
            for want, key in ((conflicts, "Conflicts"), (after, "After")):
                got = re.search(rf"^{key}=.*$", text, re.M)
                compare(f"{t} {key}=", got and got.group(0), want)
    # 2. the proxy starts after every text lane
    got = re.search(r"^After=.*$", (REPO / "qwen38-keepalive.service.template").read_text(), re.M)
    compare("qwen38-keepalive.service.template After=", got and got.group(0),
            "After=network-online.target " + " ".join(units(ls, lambda lane: lane["kind"] == "text")))
    # 3. the cockpit's exact-argv sudoers lines, one block per lane: one list, however many lines it lacks
    sudoers = set((REPO / "dashboard/sudoers-cockpit.template").read_text().splitlines())
    compare("dashboard/sudoers-cockpit.template, the lines it lacks", [line for line in sudoers_lines(ls) if line not in sudoers], [])
    # 4. the page's tables
    js = (REPO / "dashboard/static/js/base.js").read_text()
    for name, want in (("LANE_TARGETS", {lane["unit"]: lane["targets"] for lane in ls}),
                       ("READY_DEFAULT", {lane["unit"]: lane["ready_default_s"] for lane in ls}),
                       ("LANE_INSTALL", {lane["unit"]: f"./install.sh {lane['with_flag']}" for lane in ls}),
                       ("LANE_NAME", {lane["unit"]: lane["name"] for lane in ls})):
        compare(f"dashboard/static/js/base.js {name}", js_table(js, name), want)
    consts = dict(re.findall(r"\b([A-Z][A-Z0-9_]*)\s*=\s*'(qwen38-[a-z0-9-]+\.service)'", js))
    m = re.search(r"const LANE_UNITS = \[([^\]]*)\];", js)
    compare("dashboard/static/js/base.js LANE_UNITS (the manifest's ui_order)",
            [consts.get(c.strip(), c.strip()) for c in m.group(1).split(",")] if m else None,
            [lane["unit"] for lane in sorted(ls, key=lambda lane: lane["ui_order"])])
    # 5. switch-model.sh's guard: every target, none more
    m = re.search(r'case "\$CHOICE" in ([a-z0-9|-]+)\)', (REPO / "switch-model.sh").read_text())
    compare("switch-model.sh, its target guard", sorted(m.group(1).split("|")) if m else None,
            sorted(t for lane in ls for t in lane["targets"]))
    # 6. the CI's flag lanes: every target that is not a MODEL_CHOICE
    m = re.search(r"FLAG_LANES = (\[.*?\])", (REPO / ".github/workflows/ci.yml").read_text())
    compare(".github/workflows/ci.yml FLAG_LANES", sorted(json.loads(m.group(1))) if m else None,
            sorted(t for lane in ls if not lane.get("model_choice") for t in lane["targets"]))
    for where, what in bad.items():
        print(f"DISAGREES {where}\n    {what}")
    print(f"lanes.toml: {len(ls)} lanes, {sum(len(lane['targets']) for lane in ls)} targets;"
          f" {n_lists - len(bad)} of {n_lists} lists agree with it" + (f", {len(bad)} do not" if bad else ""))
    return 1 if bad else 0


def render():
    ls = lanes()
    changed = []
    for lane in ls:
        conflicts, after = ordering_lines(ls, lane)
        for t in lane["templates"]:
            path = REPO / t
            text = path.read_text()
            new = re.sub(r"^Conflicts=.*$", conflicts, text, count=1, flags=re.M)
            new = re.sub(r"^After=.*$", after, new, count=1, flags=re.M)
            if new != text:
                path.write_text(new)
                changed.append(t)
    # the proxy starts after every text lane
    path = REPO / "qwen38-keepalive.service.template"
    text = path.read_text()
    want = "After=network-online.target " + " ".join(units(ls, lambda lane: lane["kind"] == "text"))
    new = re.sub(r"^After=.*$", want, text, count=1, flags=re.M)
    if new != text:
        path.write_text(new)
        changed.append(path.name)
    print(f"{len(changed)} templates rewritten" + (": " + ", ".join(changed) if changed else ""))


def shell():
    ls = lanes()
    fam = {"ENGINE": lambda lane: True, "TEXT": lambda lane: lane["kind"] == "text",
           "VLLM": lambda lane: "vllm" in lane["families"], "B12X": lambda lane: "b12x" in lane["families"],
           "DIFFUSION": lambda lane: lane["engine"] == "sglang-diffusion"}
    for name, pred in fam.items():
        print(f"LANES_{name}_UNITS=({' '.join(units(ls, pred))})")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        sys.exit(check())
    elif cmd == "render":
        render()
    elif cmd == "shell":
        shell()
    elif cmd == "json":
        print(json.dumps(lanes(), indent=1))
    else:
        sys.exit(__doc__)
