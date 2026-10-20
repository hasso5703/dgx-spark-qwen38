#!/usr/bin/env python3
"""What this release recommends for a box, and which values only an older release made the default.

An update kept, as if the operator had chosen them, values that were nothing but the default
of the release that first installed the box: the engine on every interface (the default until
v1.17.0), the native 262144 window (until v1.12.1), the flash lane's context tier (until
v1.19.0) and the BF16 draft at depth 8 (until v1.9.0). A unit cannot say which is which, and
"Keeping the installed engine bind: 0.0.0.0" read as a choice on a box where nobody had ever
made one (found while cataloguing every way a box departs from a fresh one, 2026-10-16).

This module holds the facts that tell them apart, for install.sh and the cockpit (since
v1.36.0, 2026-10-17): what a box installed today gets, read from install.sh itself so it
cannot drift from it, and the four old defaults with the instant their release moved them.

Standard library only: install.sh runs it with the system python3, and the cockpit loads it
the way it loads engine-inputs.py.
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
UNITS = "/etc/systemd/system"
# What a completed install.sh run writes in the config folder (~/.config/qwen38), and the first
# release that writes it: a box without one was last installed by an older release.
FILE = "installed.json"
SCHEMA = 1
FIRST_RELEASE = "v1.36.0"

# Every setting this module compares, by the name an operator passes to set it, so that the way
# to keep a value is always NAME=value ./install.sh. A lane's target is not a variable of its
# own (MODEL_CHOICE picks the lane too), hence target.27b and target.flash; DRAFT2 is the 27B
# drafter as a whole (repository, quantization, depth), because the old default is the three
# together: the same repository at depth 16 was somebody's choice. The side lanes' checkpoints
# are compared too (2026-10-17): install.sh hands KOLIBRI_VARIANT, IMAGE_MODEL and VIDEO_VARIANT
# to their installers, which keep the unit's otherwise.
KNOBS = ("target.27b", "target.flash", "DRAFT2", "CONTEXT_MODE", "ENGINE_BIND", "PROXY_BIND",
         "PORT", "PROXY_PORT", "HF_CACHE", "PLE_DIR", "FLASH_TIER", "SPEC_TOKEN_MAP_SIZE",
         "FLASH_REPLAYSSM_SPEC", "KOLIBRI_VARIANT", "IMAGE_MODEL", "VIDEO_VARIANT")
# Settings that belong to the site rather than to a release: where the checkpoints live, which
# ports clients were given. Listed apart and never moved by an update (decided 2026-10-16).
SITE = ("PORT", "PROXY_PORT", "HF_CACHE", "PLE_DIR")

# A default an older release installed, and the instant the release that moved it was made.
# The instant is the committer date of the tagged commit, not its day: v1.16.0 and v1.17.0
# shipped 18 minutes apart, v1.12.0 and v1.12.1 54, and a box installed between them must not
# be classed on the wrong side. Checked 2026-10-16 with
#   git for-each-ref --format='%(*committerdate:iso-strict)%(committerdate:iso-strict)' refs/tags/<tag>
# `says` is the CHANGELOG's own words for the change, in that release's section
# (tests/test_conformity_recommended.py holds each row to it).
OldDefault = namedtuple("OldDefault", "knob value tag instant says")
OLD_DEFAULTS = (
    # z-lab's BF16 DFlash2 at depth 8: the drafter of v1.2.3 to v1.8.6 (their unit template)
    OldDefault("DRAFT2", "z-lab/Qwen3.8-27B-DFlash2 (unquant, D=8)", "v1.9.0",
               "2026-09-11T13:06:45+02:00", "the 27B lane drafts from a calibrated NVFP4 head, 16 deep"),
    # CONTEXT_MODE:-native until v1.12.0; empty since, which a 27B service install reads as 1m
    OldDefault("CONTEXT_MODE", "native", "v1.12.1",
               "2026-09-14T12:13:42+02:00", "the 1M window by default"),
    # --host 0.0.0.0 in the templates until v1.15.x, ENGINE_BIND:-0.0.0.0 in v1.16.0
    OldDefault("ENGINE_BIND", "0.0.0.0", "v1.17.0",
               "2026-09-22T13:19:42+02:00", "the engine answers on localhost only, and that is the default"),
    # FLASH_TIER:-context until v1.18.7
    OldDefault("FLASH_TIER", "context", "v1.19.0",
               "2026-10-01T02:07:06+02:00", "`FLASH_TIER` defaults to `concurrency`"),
)


def old_default_of(knob):
    """The old default row of a setting, or None when no release ever installed another default."""
    return next((o for o in OLD_DEFAULTS if o.knob == knob), None)


def old_default(knob, value):
    """The old default row when `value` is what an older release installed for `knob`, else None."""
    o = old_default_of(knob)
    return o if o is not None and o.value == value else None


def drafter(repo, quant, tokens):
    """The 27B drafter as one value, the way install.sh reads it back from a unit."""
    return f"{repo} ({quant}, D={tokens})"


def _install_text(repo_dir):
    return (Path(repo_dir) / "install.sh").read_text(encoding="utf-8")


def installer_defaults(repo_dir=REPO):
    """{VAR: default} for each `VAR="${VAR:-default}"` line of install.sh, the first one of each
    variable: the line a default is written on."""
    out = {}
    for m in re.finditer(r'^([A-Z][A-Z0-9_]*)="\$\{\1:-([^}]*)\}"', _install_text(repo_dir), re.M):
        out.setdefault(m.group(1), m.group(2))
    return out


def targets(repo_dir=REPO):
    """{target: repository} for every MODEL_CHOICE install.sh accepts, from its own case block."""
    text = _install_text(repo_dir)
    repos = dict(re.findall(r'^([A-Z][A-Z0-9_]*_REPO)="([^"$]+)"', text, re.M))
    return {name: repos[var] for name, var in
            re.findall(r'^ {2}([a-z0-9-]+)\)\s+MODEL_REPO="\$([A-Z][A-Z0-9_]*_REPO)"', text, re.M)}


def _side(repo_dir, script, pattern):
    """A value a side lane's installer writes (its default, a pinned repository), read from the
    line that writes it: KeyError when the installer no longer writes it that way."""
    m = re.search(pattern, _read(Path(repo_dir) / script) or "", re.M)
    if not m:
        raise KeyError(f"{script}: {pattern}")
    return m.group(1)


# The lines of the side lanes' installers that write their defaults and their two Kolibri
# checkpoints (tests/test_conformity_side_lanes.py runs the installers' own lines against them).
SIDE = {
    "KOLIBRI_VARIANT": ("install-kolibri.sh", r'^\s*""\)\s*VARIANT=([a-z0-9]+);'),
    "IMAGE_MODEL": ("install-image.sh", r'^IMAGE_MODEL_PIN="([^"]+)"'),
    "VIDEO_VARIANT": ("install-video.sh", r'VARIANT="\$\{VARIANT:-([a-z0-9]+)\}"'),
    "fp8": ("install-kolibri.sh", r'^KOLIBRI_FP8_REPO="([^"]+)"'),
    "nvfp4": ("install-kolibri.sh", r'^KOLIBRI_NVFP4_REPO="([^"]+)"'),
}


def recommended(repo_dir=REPO, home=None):
    """{knob: value} a box installed today with no variable set ends up with. A default install.sh
    no longer writes the way this reads raises KeyError rather than recommend nothing."""
    home = str(home or Path.home())
    d = installer_defaults(repo_dir)

    def val(name):
        return d[name].replace("$HOME", home)

    port = val("PORT")
    proxy_port = val("PROXY_PORT")
    if proxy_port == "$((PORT+1))":
        proxy_port = str(int(port) + 1)
    flash_target = re.search(r"SECOND_LANE=flash;.*\bSECOND_TARGET=([a-z0-9-]+)$", _install_text(repo_dir), re.M)
    if not flash_target:
        raise KeyError("SECOND_TARGET of the flash lane")
    return {
        "target.27b": val("MODEL_CHOICE"),
        "target.flash": flash_target.group(1),
        "DRAFT2": drafter(val("DRAFT2_REPO"), val("DRAFT2_QUANT"), val("DRAFT2_TOKENS")),
        # empty in install.sh, and resolved to 1m on a 27B service install once the flags are
        # read (the test runs install.sh to check that this is what it resolves)
        "CONTEXT_MODE": "1m",
        "ENGINE_BIND": val("ENGINE_BIND"),
        "PROXY_BIND": val("PROXY_BIND"),
        "PORT": port,
        "PROXY_PORT": proxy_port,
        "HF_CACHE": val("HF_CACHE"),
        "PLE_DIR": val("PLE_DIR"),
        "FLASH_TIER": val("FLASH_TIER"),
        "SPEC_TOKEN_MAP_SIZE": val("SPEC_TOKEN_MAP_SIZE"),
        "FLASH_REPLAYSSM_SPEC": val("FLASH_REPLAYSSM_SPEC"),
        **{knob: _side(repo_dir, *SIDE[knob]) for knob in ("KOLIBRI_VARIANT", "IMAGE_MODEL", "VIDEO_VARIANT")},
    }


# ── What the box serves ──────────────────────────────────────────────────────────────────────
# Read from its units the way install.sh reads them back when it converges (install.sh, the
# block "Converge on the operator's installed choices"), so that what is classed here is what
# the next update would keep.
def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _first(pattern, text):
    m = re.search(pattern, text)
    return m.group(1) if m else None


def _engine_place(text, out):
    """The bind, port and cache of the lane that serves: both text lanes carry them, and
    install.sh reads them from the one it serves (the other gets them from that run)."""
    for knob, pattern in (("ENGINE_BIND", r"--host ([0-9.]+)"), ("PORT", r"--port ([0-9]+)"),
                          ("HF_CACHE", r"-v ([^ :]+):/root/\.cache/huggingface")):
        v = _first(pattern, text)
        if v:
            out[knob] = v


def observe(units_dir=UNITS, config_dir=None, lane="27b", repo_dir=REPO):
    """{knob: value} the installed units serve; a lane that is not installed says nothing.
    lane: the text lane that serves (27b or flash), whose unit gives the bind, port and cache."""
    config_dir = Path(config_dir or Path.home() / ".config/qwen38")
    by_repo = {repo: name for name, repo in targets(repo_dir).items()}
    out = {}
    sgl = _read(Path(units_dir) / "qwen38-sglang.service")
    if sgl is not None:
        model = _first(r"--model-path ([^ ]+)", sgl)
        if model:
            out["target.27b"] = by_repo.get(model, model)
        draft = _first(r"--speculative-draft-model-path ([^ ]+)", sgl)
        if draft:
            out["DRAFT2"] = drafter(draft, _first(r"--speculative-draft-model-quantization ([^ ]+)", sgl) or "",
                                    _first(r"--speculative-num-draft-tokens ([0-9]+)", sgl) or "")
        out["CONTEXT_MODE"] = "1m" if "--context-length 1010000" in sgl else "native"
        if lane == "27b":
            _engine_place(sgl, out)
    launch = None
    if (Path(units_dir) / "qwen38-flash.service").exists():
        # Two launchers for the flash lane: the unit's ExecStart says which config the
        # box serves (b12x vLLM or SGLang); without a unit that says, whichever exists.
        unit_txt = _read(Path(units_dir) / "qwen38-flash.service") or ""
        if "launch-flash-fp4local" in unit_txt:
            launch = _read(config_dir / "launch-flash-fp4local.sh")
        else:
            launch = _read(config_dir / "launch-flash.sh")
            if launch is None:
                launch = _read(config_dir / "launch-flash-fp4local.sh")
    if launch is not None:
        # three spellings, as in lanes.sh launcher_model: the FP4-Local launcher names
        # its checkpoint as vllm's own serve argument
        model = _first(r"--model-path ([^ ]+)", launch) or _first(r"--model ([^ ]+)", launch) \
            or _first(r"\bvllm serve ([^ ]+)", launch)
        if model:
            out["target.flash"] = by_repo.get(model, model)
        # the last tier whose concurrency the launcher names, as install.sh's loop decides it
        for tier, mrr in (("context", "4"), ("concurrency", "8"), ("throughput", "24")):
            if f"--max-running-requests {mrr}" in launch:
                out["FLASH_TIER"] = tier
        # the two speculative-path knobs mean something only on a tier that speculates, and
        # only on a launcher rendered after each existed (the markers are template comments)
        if "--speculative-algorithm" in launch:
            if "Engine since v1.8" in launch:
                out["SPEC_TOKEN_MAP_SIZE"] = _first(r"--speculative-token-map /out/token-map-([0-9]+)\.pt", launch) or "0"
            if "No --allow-auto-truncate" in launch:
                out["FLASH_REPLAYSSM_SPEC"] = "1" if "--enable-linear-replayssm-spec" in launch else "0"
        ple = _first(r"-v ([^ :]+):/ple", launch)
        if ple:
            out["PLE_DIR"] = ple
        if lane == "flash" and ("sglang.launch_server" in launch or "vllm serve" in launch):
            _engine_place(launch, out)
    proxy = _read(Path(units_dir) / "qwen38-keepalive.service")
    if proxy is not None:
        for knob, pattern in (("PROXY_BIND", r"PROXY_BIND=([0-9.]+)"), ("PROXY_PORT", r"keepalive-proxy\.py ([0-9]+)")):
            v = _first(pattern, proxy)
            if v:
                out[knob] = v
    # The side lanes' checkpoints, read back as their installers read them: Kolibri's by the
    # repository before its --revision, the image's by --model-path, the video's by
    # --model-variant (fl2va, the installer's default, when the unit names none).
    kolibri = _read(Path(units_dir) / "qwen38-kolibri.service")
    if kolibri is not None:
        for variant in ("fp8", "nvfp4"):
            if f"{_side(repo_dir, *SIDE[variant])} --revision" in kolibri:
                out["KOLIBRI_VARIANT"] = variant
    image = _read(Path(units_dir) / "qwen38-image.service")
    if image is not None and _first(r"--model-path ([^ ]+)", image):
        out["IMAGE_MODEL"] = _first(r"--model-path ([^ ]+)", image)
    video = _read(Path(units_dir) / "qwen38-video.service")
    if video is not None:
        out["VIDEO_VARIANT"] = _first(r"--model-variant ([a-z0-9]+)", video) or _side(repo_dir, *SIDE["VIDEO_VARIANT"])
    return out


# ── What the box recorded ────────────────────────────────────────────────────────────────────
def load_record(config_dir):
    """(record, note): installed.json as the last run wrote it, or None with a note saying why
    it is not used. A file that is cut short, edited into another shape, or written by a newer
    schema is no record at all (never a partial one): the box is then classed by its age, which
    keeps more rather than less."""
    path = Path(config_dir) / FILE
    raw = _read(path)
    if raw is None:
        return None, None
    try:
        rec = json.loads(raw)
        ok = (isinstance(rec, dict) and rec.get("schema") == SCHEMA and isinstance(rec.get("choices"), dict)
              and all(isinstance(e, dict) and isinstance(e.get("value"), str) for e in rec["choices"].values()))
    except ValueError:
        ok = False
    if not ok:
        return None, (f"NOTE: {path} is unreadable (cut short or edited by hand); this run treats the box as "
                      f"one no run of {FIRST_RELEASE} or later recorded, and writes it again at its end.")
    return rec, None


def _instant(text):
    try:
        t = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else None


def box_since(config_dir, record):
    """(when, where from): when this box was first installed. A record carries it; without one,
    the API key's mtime says it, since install.sh writes the key once, at the first install
    that reaches step 6 (it is stat()ed here, never read). (None, "unknown") when neither can."""
    if record and _instant(record.get("box_since")):
        return record["box_since"], "record"
    try:
        t = (Path(config_dir) / "api-key").stat().st_mtime
    except OSError:
        return None, "unknown"
    return datetime.fromtimestamp(int(t), timezone.utc).isoformat(timespec="seconds"), "api-key"


Verdict = namedtuple("Verdict", "cls why old")


def classify(knob, value, rec_value, record, since):
    """Whether a value that is not the recommended one is a choice (kept, and said) or an old
    default (moved to the recommended value by the next update), and why:
      site          a setting of the site (port, cache, folders): always the operator's
      recorded      installed.json records this very value as a choice
      never-default no release ever installed it by default, so somebody chose it
      not-recorded  an old default that no run since installed.json exists has recorded
      box-before    no installed.json, and the box is older than the release that moved it
      box-after     no installed.json, and the box is younger: it was chosen
      box-unknown   no installed.json and no age: kept, since what cannot be classed is not moved
    `record` is a loaded installed.json or None, `since` box_since()'s date."""
    if value == rec_value:
        return Verdict("conform", "recommended", None)
    if knob in SITE:
        return Verdict("choice", "site", None)
    entry = (record or {}).get("choices", {}).get(knob)
    if entry and entry.get("value") == value:
        return Verdict("choice", "recorded", None)
    old = old_default(knob, value)
    if old is None:
        return Verdict("choice", "never-default", None)
    if record is not None:
        return Verdict("old-default", "not-recorded", old)
    born = _instant(since)
    if born is None:
        return Verdict("choice", "box-unknown", old)
    if born < _instant(old.instant):
        return Verdict("old-default", "box-before", old)
    return Verdict("choice", "box-after", old)


def _recommended_beside(rec, seen):
    """The recommended values for this box: its proxy port is recommended at PORT+1 of the port
    it serves on, as install.sh derives it."""
    rec = dict(rec)
    if seen.get("PORT"):
        rec["PROXY_PORT"] = str(int(seen["PORT"]) + 1)
    return rec


def deviations(units_dir=UNITS, config_dir=None, lane="27b", home=None, repo_dir=REPO):
    """[{knob, value, recommended, class, why, old}] for each value the box serves that is not
    the recommended one, in the order of KNOBS. `old` is the release that moved an old default."""
    config_dir = config_dir or Path(home or Path.home()) / ".config/qwen38"
    record, _ = load_record(config_dir)
    since, _ = box_since(config_dir, record)
    seen = observe(units_dir, config_dir, lane=lane, repo_dir=repo_dir)
    rec = _recommended_beside(recommended(repo_dir, home), seen)
    out = []
    for knob in KNOBS:
        if knob not in seen or seen[knob] == rec[knob]:
            continue
        v = classify(knob, seen[knob], rec[knob], record, since)
        out.append({"knob": knob, "value": seen[knob], "recommended": rec[knob], "class": v.cls,
                    "why": v.why, "old": v.old.tag if v.old else None})
    return out


def verdict(config_dir, knob, value):
    """What an update does with a value the unit serves, for install.sh's convergence: an old
    default this box never chose moves to today's default, anything else stays. The value is
    given as install.sh read it, so nothing here depends on reading the units the same way."""
    if old_default(knob, value) is None:
        return Verdict("choice", "never-default", None)
    record, note = load_record(config_dir)
    if note:
        print(note, file=sys.stderr)
    since, _ = box_since(config_dir, record)
    return classify(knob, value, None, record, since)


# What a run says of each setting it keeps or moves, by the reason classify() gives.
PLAN_WHY = {
    "recorded": "recorded as your choice",
    "never-default": "no release installed it by default",
    "box-after": "this box was installed after {old} moved the default, so it was chosen",
    "box-unknown": "the age of this box cannot be told, so it stays",
    "env": "set on this command line",
    "site": "a setting of this site, which updates never move",
}


def plan(config_dir, lane, chose=None, repo_dir=REPO, units_dir=UNITS, home=None, recommended_run=False):
    """[{group, knob, value, recommended, why, old}]: each setting that differs from the
    recommended one once this run is done, in the order of KNOBS (2026-10-17). group:
      moves  an old default this box never chose: the run moves it to the recommended value
      yours  kept: recorded, set on this command line, or never a default (why says which)
      site   a setting of the site, kept
    chose: {knob: value} set on this command line, which the run serves whatever the units say.
    recommended_run: the run is ./install.sh --recommended, which moves every setting but the
    site's to the recommended value, the operator's choices among them (why: recommended).
    The classing is classify()'s, the one install.sh's convergence asks verdict() for."""
    config_dir = Path(config_dir)
    record, _ = load_record(config_dir)
    since, _ = box_since(config_dir, record)
    seen = observe(units_dir, config_dir, lane=lane, repo_dir=repo_dir)
    chose = dict(chose or {})
    rec = _recommended_beside(recommended(repo_dir, home), {**seen, **chose})
    out = []
    for knob in KNOBS:
        if knob in chose:
            if chose[knob] != rec[knob]:
                out.append({"group": "site" if knob in SITE else "yours", "knob": knob, "value": chose[knob],
                            "recommended": rec[knob], "why": "env", "old": None})
            continue
        if knob not in seen or seen[knob] == rec[knob]:
            continue
        if recommended_run and knob not in SITE:
            out.append({"group": "moves", "knob": knob, "value": seen[knob], "recommended": rec[knob],
                        "why": "recommended", "old": None})
            continue
        v = classify(knob, seen[knob], rec[knob], record, since)
        group = "moves" if v.cls == "old-default" else "site" if v.why == "site" else "yours"
        out.append({"group": group, "knob": knob, "value": seen[knob], "recommended": rec[knob],
                    "why": v.why, "old": v.old.tag if v.old else None})
    return out


# The settings that are no variable of their own, as the list names them.
PLAN_NAMES = {"target.27b": "27B target", "target.flash": "flash target", "DRAFT2": "27B drafter",
              "KOLIBRI_VARIANT": "Kolibri checkpoint", "IMAGE_MODEL": "image model", "VIDEO_VARIANT": "video variant"}


def plan_lines(items, release):
    """The list a run prints before its first step: one line when nothing differs."""
    head = f"This box against what {release or 'this release'} recommends"
    if not items:
        return [f"{head}: the same, nothing of yours differs."]
    lines = [f"{head}:"]
    for group, label in (("moves", "moves now"), ("yours", "stays yours"), ("site", "site setting")):
        for it in (i for i in items if i["group"] == group):
            if it["why"] == "recommended":
                text = (f"{PLAN_NAMES.get(it['knob'], it['knob'])} {it['value']}, to {it['recommended']}: "
                        "--recommended moves it, a choice of yours or not")
            elif group == "moves":
                keep = ("the line above gives the variables that keep it" if it["knob"] == "DRAFT2"
                        else f"to keep it: {it['knob']}={it['value']} ./install.sh")
                text = (f"{PLAN_NAMES.get(it['knob'], it['knob'])} {it['value']}, to {it['recommended']}: the default "
                        f"since {it['old']}, and nothing records it as chosen here ({keep})")
            else:
                text = (f"{PLAN_NAMES.get(it['knob'], it['knob'])} {it['value']} (recommended: {it['recommended']}), "
                        + PLAN_WHY[it["why"]].format(old=it["old"]))
            lines.append(f"  {label:<13}{text}")
    return lines


def next_choices(record, seen, rec, chose, since, now):
    """The choices a run records: {knob: {value, since, how}}.
    chose: {knob: value} the operator passed explicitly on this run (how=env). Passing the
    recommended value erases the entry, so the box follows the defaults again, future ones
    included. An entry is kept only while the unit still serves its value: a switch or an edit
    since makes it say nothing, and what is served is classed afresh. A value classed as a
    choice that no entry names yet is recorded (how=kept: the first run of a box that predates
    installed.json, or a value no release ever installed); an old default never is."""
    rec = _recommended_beside(rec, seen)
    current = dict((record or {}).get("choices", {}))
    out = {}
    for knob, value in chose.items():
        if value == rec.get(knob) or seen.get(knob) != value:
            continue                       # erased, or it landed in no unit this run rendered
        prev = current.get(knob)
        out[knob] = {"value": value, "since": prev["since"] if prev and prev.get("value") == value
                     and prev.get("since") else now, "how": "env"}
    for knob, entry in current.items():
        if knob not in out and knob not in chose and seen.get(knob) == entry["value"] != rec.get(knob):
            out[knob] = entry
    for knob in KNOBS:
        if knob in out or knob in chose or knob not in seen or seen[knob] == rec[knob]:
            continue
        if classify(knob, seen[knob], rec[knob], record, since).cls == "choice":
            out[knob] = {"value": seen[knob], "since": now, "how": "kept"}
    return out


def release_lag(record, clone_commit):
    """Why the services run older code than their own clone, or None when they do not:
    no-record  no run of FIRST_RELEASE or later finished on this box
    commit     the clone moved on since the last run (a git pull with no ./install.sh)
    no-start   the last run stopped at --no-start (no side lane, proxy or cockpit updated)"""
    if record is None:
        return "no-record"
    if clone_commit and record.get("commit") and record["commit"] != clone_commit:
        return "commit"
    if record.get("path") == "no-start":
        return "no-start"
    return None


# ── What a run writes ────────────────────────────────────────────────────────────────────────
def clone_release(repo_dir=REPO):
    """The release a clone is, as its CHANGELOG.md's first heading names it (None when it names none)."""
    try:
        with open(Path(repo_dir) / "CHANGELOG.md", encoding="utf-8") as f:
            for line in f:
                m = re.match(r"^## (v\d+\.\d+(?:\.\d+)?)\b", line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def repo_state(repo_dir=REPO):
    """{release, commit, branch, modified} of a clone; what git cannot say is None."""
    def git(*args):
        try:
            r = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout.strip() if r.returncode == 0 else None
    head = git("rev-parse", "--verify", "-q", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no")
    return {"release": clone_release(repo_dir),
            "commit": head if head and re.fullmatch(r"[0-9a-f]{40}", head) else None,
            "branch": git("symbolic-ref", "--short", "-q", "HEAD") or None,
            "modified": bool(status) if status is not None else None}


def write_record(config_dir, record):
    """Whole or not at all: a reader (the cockpit, the next run) sees the old file or the new
    one, never half of one. 0644 whatever the umask: it holds no secret, and the cockpit reads it."""
    tmp = Path(config_dir) / f".{FILE}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, Path(config_dir) / FILE)
    finally:
        if tmp.exists():
            tmp.unlink()


def record(config_dir, path, lane, chose=None, repo_dir=REPO, units_dir=UNITS, home=None, now=None):
    """Write installed.json for a run that ended well (install.sh's record_install), under a
    lock: an update in a terminal and a Load from the cockpit may both write it. Returns it."""
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    with open(config_dir / f"{FILE}.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        old, note = load_record(config_dir)
        if note:
            print(note, file=sys.stderr)
        since, since_from = box_since(config_dir, old)
        seen = observe(units_dir, config_dir, lane=lane, repo_dir=repo_dir)
        now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
        new = {"schema": SCHEMA, **repo_state(repo_dir), "repo_dir": str(repo_dir), "finished": now,
               "path": path, "lane": lane, "box_since": since, "box_since_from": since_from,
               "choices": next_choices(old, seen, recommended(repo_dir, home), chose or {}, since, now)}
        write_record(config_dir, new)
    return new


def choose(config_dir, knob, value, how="switch", repo_dir=REPO, home=None, now=None):
    """Record one choice made outside install.sh, in an installed.json that exists: a switch,
    the cockpit's Load among them, is the operator choosing a target (2026-10-17). The value
    the release recommends erases the entry instead, so the box follows the defaults again,
    future ones included. A box no run of FIRST_RELEASE or later recorded gets no file: its next
    ./install.sh classes what the unit serves. Returns (record, what changed), (None, None)
    when there is nothing to write in."""
    config_dir = Path(config_dir)
    if not (config_dir / FILE).exists():
        return None, None
    with open(config_dir / f"{FILE}.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rec, note = load_record(config_dir)
        if rec is None:
            if note:
                print(note, file=sys.stderr)
            return None, None
        choices = dict(rec["choices"])
        prev = choices.get(knob)
        if value == recommended(repo_dir, home).get(knob):
            if prev is None:
                return rec, None
            del choices[knob]
            changed = "erased"
        else:
            now = now or datetime.now(timezone.utc).isoformat(timespec="seconds")
            same = prev is not None and prev.get("value") == value
            choices[knob] = {"value": value, "since": prev["since"] if same and prev.get("since") else now,
                             "how": prev["how"] if same and prev.get("how") else how}
            changed = None if same else "recorded"
        rec["choices"] = choices
        write_record(config_dir, rec)
    return rec, changed


def main(argv=None):
    ap = argparse.ArgumentParser(prog="conformity.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="write installed.json for a run of install.sh that ended well")
    r.add_argument("--config-dir", required=True)
    r.add_argument("--repo-dir", default=str(REPO))
    r.add_argument("--units", default=UNITS)
    r.add_argument("--lane", required=True, choices=("27b", "flash"))
    r.add_argument("--path", required=True, choices=("full", "side-boot", "no-start", "no-service"))
    r.add_argument("--chose", action="append", default=[], metavar="KNOB=VALUE",
                   help="a value the operator set explicitly on this run")
    c = sub.add_parser("choose", help="record a choice made outside install.sh (a switch), in an installed.json that exists")
    c.add_argument("--config-dir", required=True)
    c.add_argument("--repo-dir", default=str(REPO))
    c.add_argument("--how", default="switch", choices=("switch", "env", "flag"))
    c.add_argument("knob", choices=KNOBS)
    c.add_argument("value")
    pl = sub.add_parser("plan", help="list what a run of install.sh keeps and moves, against the recommended state")
    pl.add_argument("--config-dir", required=True)
    pl.add_argument("--repo-dir", default=str(REPO))
    pl.add_argument("--units", default=UNITS)
    pl.add_argument("--lane", required=True, choices=("27b", "flash"))
    pl.add_argument("--chose", action="append", default=[], metavar="KNOB=VALUE",
                    help="a value the operator set explicitly on this run")
    pl.add_argument("--recommended", action="store_true",
                    help="the run is ./install.sh --recommended: every setting but the site's moves")
    v = sub.add_parser("verdict", help="converge or keep: what an update does with a value an older release made the default")
    v.add_argument("--config-dir", required=True)
    v.add_argument("knob", choices=KNOBS)
    v.add_argument("value")
    a = ap.parse_args(argv)
    if a.cmd == "verdict":
        # Never a failure: install.sh keeps the value on anything but "converge", which is what
        # every update did before v1.36.0.
        try:
            got = verdict(a.config_dir, a.knob, a.value)
        except Exception as e:  # noqa: BLE001
            print(f"NOTE: could not tell whether {a.knob}={a.value} was chosen ({e}); it stays.", file=sys.stderr)
            print("keep error")
            return 0
        print(f"converge {got.old.tag}" if got.cls == "old-default" else f"keep {got.why}")
        return 0
    if a.cmd == "choose":
        # Never a failure either: the switch it follows is done, and the next ./install.sh
        # classes what the unit serves whatever this wrote.
        try:
            _, changed = choose(a.config_dir, a.knob, a.value, how=a.how, repo_dir=a.repo_dir)
        except Exception as e:  # noqa: BLE001
            print(f"NOTE: could not record {a.knob}={a.value} in {Path(a.config_dir) / FILE} ({e}).", file=sys.stderr)
            return 0
        if changed == "recorded":
            print(f"  Recorded   : {a.knob}={a.value} as your choice, which updates keep "
                  f"({Path(a.config_dir) / FILE})")
        elif changed == "erased":
            print(f"  Recorded   : {a.knob} back to the recommended {a.value}, which updates follow "
                  f"({Path(a.config_dir) / FILE})")
        return 0
    chose = {}
    for kv in a.chose:
        knob, sep, value = kv.partition("=")
        if not sep or knob not in KNOBS:
            ap.error(f"--chose {kv}: not one of {', '.join(KNOBS)}")
        chose[knob] = value
    if a.cmd == "plan":
        print("\n".join(plan_lines(plan(a.config_dir, a.lane, chose, repo_dir=a.repo_dir, units_dir=a.units,
                                         recommended_run=a.recommended), clone_release(a.repo_dir))))
        return 0
    new = record(a.config_dir, a.path, a.lane, chose, repo_dir=a.repo_dir, units_dir=a.units)
    mine = ", ".join(f"{k}={e['value']}" for k, e in new["choices"].items())
    print(f"  Recorded   : {Path(a.config_dir) / FILE} ({new['release'] or 'no release named'}"
          f"{' at ' + new['commit'][:7] if new['commit'] else ''}; "
          + (f"your choices: {mine})" if mine else "nothing of yours differs from the recommended state)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
