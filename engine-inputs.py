#!/usr/bin/env python3
"""What an engine reads when it starts, and whether the running one already has it.

Usage: engine-inputs.py fingerprint <unit file> <config dir> <hf cache> [REPO@REV ...]
       engine-inputs.py running     <unit file> <config dir> <hf cache> [REPO@REV ...]

install.sh restarted the engine on every run, so an update that changed nothing the engine
reads still cost a full boot (8 min on the 27B, 12 on flash, reference box 2026-09-23).
It now restarts it only when this says it has to.

The inputs are found, not listed: the unit and its drop-ins; any script of the config dir
the unit runs (the flash lane's launcher); every file of the config dir those name, directly
or through the /out mount (chat template, token map, API key); the config.json of each
checkpoint given as REPO@REV (install.sh patches YaRN into it in place); and the image ID
of every image they name, since a local tag can move.

fingerprint prints a sha256 over all of it, by content: a file rewritten identically does
not change it. running prints "yes" when the unit is active, started after every input was
last written, and its container runs one of the images the inputs name; otherwise "no: "
and why. Both always exit 0 on a readable unit, so a shell can read the answer.
"""
import glob
import hashlib
import os
import re
import subprocess
import sys


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def inputs(unit, config_dir, hf_cache, ckpts):
    """(files, image refs, container names) the engine of this unit reads at start."""
    config_dir = config_dir.rstrip("/")
    files = [unit] + sorted(glob.glob(unit + ".d/*.conf"))
    texts = [_read(f) for f in files]
    cfg = re.escape(config_dir)
    for script in sorted(set(re.findall(cfg + r"/[A-Za-z0-9._-]+\.sh", "\n".join(texts)))):
        if os.path.isfile(script):
            files.append(script)
            texts.append(_read(script))
    body = "\n".join(texts)
    for name in sorted(set(re.findall(r"(?:/out|" + cfg + r")/([A-Za-z0-9._-]+)", body))):
        path = f"{config_dir}/{name}"
        if os.path.isfile(path) and path not in files:
            files.append(path)
    for ck in ckpts:
        repo, _, rev = ck.partition("@")
        if not repo or not rev:
            continue
        path = os.path.join(hf_cache, "hub", "models--" + repo.replace("/", "--"), "snapshots", rev, "config.json")
        if os.path.exists(path):
            files.append(path)
    images = sorted(set(re.findall(r"[a-z0-9][a-z0-9./_-]*@sha256:[0-9a-f]{64}", body))
                    | set(re.findall(r"\bqwen38-[a-z0-9-]+:[A-Za-z0-9._-]+", body)))
    containers = sorted(set(re.findall(r"--name[ =]([A-Za-z0-9._-]+)", body)))
    return files, images, containers


def _image_id(ref):
    r = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", ref],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else "absent"


def fingerprint(unit, config_dir, hf_cache, ckpts):
    files, images, _ = inputs(unit, config_dir, hf_cache, ckpts)
    h = hashlib.sha256()
    for f in files:
        try:
            with open(f, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError:
            digest = "unreadable"
        h.update(f"{f}\0{digest}\n".encode())
    for ref in images:
        h.update(f"{ref}\0{_image_id(ref)}\n".encode())
    return h.hexdigest()


def running(unit, config_dir, hf_cache, ckpts):
    name = os.path.basename(unit)
    r = subprocess.run(["systemctl", "show", name, "--timestamp=unix",
                        "-p", "ActiveState", "-p", "ExecMainStartTimestamp"],
                       capture_output=True, text=True)
    props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    if props.get("ActiveState") != "active":
        return f"no: {name} is {props.get('ActiveState') or 'unknown'}"
    started = props.get("ExecMainStartTimestamp", "").lstrip("@")
    if not started.isdigit():
        return f"no: {name} has no start time"
    files, images, containers = inputs(unit, config_dir, hf_cache, ckpts)
    for f in files:
        try:
            if os.stat(f).st_mtime > int(started):
                return f"no: {f} changed after {name} started"
        except OSError:
            return f"no: {f} is unreadable"
    ids = {_image_id(ref) for ref in images} - {"absent"}
    for c in containers:
        r = subprocess.run(["docker", "inspect", "--format", "{{.Image}}", c], capture_output=True, text=True)
        if r.returncode != 0:
            return f"no: container {c} is not running"
        if ids and r.stdout.strip() not in ids:
            return f"no: container {c} runs another image than the one its unit names"
    if not containers:
        return "no: found no container name to check the image against"
    return "yes"


def main(argv):
    if len(argv) < 5 or argv[1] not in ("fingerprint", "running"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    mode, unit, config_dir, hf_cache, ckpts = argv[1], argv[2], argv[3], argv[4], argv[5:]
    if not os.path.isfile(unit):
        print(f"no unit at {unit}", file=sys.stderr)
        return 1
    if mode == "fingerprint":
        print(fingerprint(unit, config_dir, hf_cache, ckpts))
    else:
        print(running(unit, config_dir, hf_cache, ckpts))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
