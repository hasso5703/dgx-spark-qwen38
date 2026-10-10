#!/usr/bin/env python3
"""./switch-model.sh image-turbo, and back: the image lane's two checkpoints.

The Turbo (Qwen/Qwen-Image-2.1-Turbo) is Qwen's eight-step distillation of the lane's model.
A switch between the two rewrites the unit's --model-path and nothing else, fetches the
checkpoint at the revision the box serves of it (its refs/main; the installer's pin when
there is none) and points refs/main at a revision it fetched (the unit runs offline by
name), keeps a checkpoint the unit was installed with across a round trip through the
Turbo, and refuses the Turbo on a runtime that cannot read its sigma grid: there it would
load, answer 200 and sample on a uniform schedule it was not trained for.

The script's own image block runs here, whole, against a fake unit, a fake cache, a fake
huggingface_hub and a fake runtime; sudo and systemctl are stubs that write down every call.
"""
import pathlib
import re
import subprocess
import tempfile
import textwrap
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TEXT = (REPO / "switch-model.sh").read_text()
START = TEXT.index('if [ "$CHOICE" = "image" ] || [ "$CHOICE" = "image-turbo" ]; then')
END = TEXT.index("\n  exit 0\nfi\n", START) + len("\n  exit 0\nfi\n")
BLOCK = TEXT[START:END]
_lines = TEXT.splitlines(keepends=True)
_at = next(i for i, ln in enumerate(_lines) if ln.startswith("dl_free_gb(){"))
DLFREE = "".join(_lines[_at:_at + 2])
PINS = dict(re.findall(r'^(IMAGE_MODEL_PIN|IMAGE_MODEL_PIN_REV|IMAGE_TURBO_PIN|IMAGE_TURBO_PIN_REV)="([^"]+)"',
                       (REPO / "install-image.sh").read_text(), re.M))
BASE, TURBO = PINS["IMAGE_MODEL_PIN"], PINS["IMAGE_TURBO_PIN"]

FAKE_HUB = '''
import os, pathlib
def snapshot_download(repo, revision=None, local_files_only=False, **kw):
    hub = pathlib.Path(os.environ["HF_HOME"]) / "hub" / ("models--" + repo.replace("/", "--"))
    rev = revision or "main"
    snap = hub / "snapshots" / (rev if len(rev) == 40 else "f" * 40)
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(f"hub {repo} {rev} local={local_files_only}\\n")
    if local_files_only and not snap.exists():
        raise FileNotFoundError(repo)
    snap.mkdir(parents=True, exist_ok=True)
    return str(snap)
'''
FAKE_CONSTANTS = '''
import os
HF_HUB_CACHE = os.path.join(os.environ["HF_HOME"], "hub")
'''
# The runtime's three parts of sgl-project/sglang#43391, each present when FAKE_PARTS names
# it: image-sglang/turbo-grid-check.py reads them as the real runtime has them.
FAKE_CONFIG = '''
import dataclasses, os
@dataclasses.dataclass
class QwenImage21PipelineConfig:
    vae_precision: str = "bf16"
    def prepare_sigmas(self, sigmas, n):
        return sigmas if sigmas is not None else [1.0 - i / n for i in range(n)]
if "config" in os.environ.get("FAKE_PARTS", "").split(","):
    @dataclasses.dataclass
    class QwenImage21PipelineConfig(QwenImage21PipelineConfig):
        sample_sigmas: list = None
        def prepare_sigmas(self, sigmas, n):
            return list(sigmas if sigmas is not None else self.sample_sigmas)
elif "config-field" in os.environ.get("FAKE_PARTS", "").split(","):
    # the field without the behaviour: the grid kept and never sampled on
    @dataclasses.dataclass
    class QwenImage21PipelineConfig(QwenImage21PipelineConfig):
        sample_sigmas: list = None
'''
FAKE_PART = '''
import os
if "{part}" in os.environ.get("FAKE_PARTS", "").split(","):
    from ._with import {cls}
else:
    from ._without import {cls}
'''
FAKE_PIPELINE = ("class QwenImage21Pipeline:\n    def _load_config(self):\n        return {\"sample_sigmas\": None}\n",
                 "class QwenImage21Pipeline:\n    def _load_config(self):\n        return {}\n")
FAKE_STAGE = ("class QwenImage21InputValidationStage:\n    def forward(self, batch, args):\n"
              "        return args.pipeline_config.prepare_sigmas(None, 8)\n",
              "class QwenImage21InputValidationStage:\n    def forward(self, batch, args):\n        return batch\n")


def sandbox(model, *, grid=True, cached=(), free_gb=500, enabled=(), active=(), refs=(), blobs=(), unit_hf="hf"):
    d = pathlib.Path(tempfile.mkdtemp(prefix="sw-img-"))
    fake = d / "fake"
    (fake / "huggingface_hub").mkdir(parents=True)
    (fake / "huggingface_hub" / "__init__.py").write_text(FAKE_HUB)
    (fake / "huggingface_hub" / "constants.py").write_text(FAKE_CONSTANTS)
    cfg = fake / "sglang/multimodal_gen/configs/pipeline_configs"
    cfg.mkdir(parents=True)
    for p in (fake / "sglang", fake / "sglang/multimodal_gen", fake / "sglang/multimodal_gen/configs", cfg):
        (p / "__init__.py").write_text("")
    (cfg / "qwen_image21.py").write_text(FAKE_CONFIG)
    rt = fake / "sglang/multimodal_gen/runtime"
    for part, cls, pkg, (with_, without) in (
            ("pipeline", "QwenImage21Pipeline", rt / "pipelines/qwen_image21", FAKE_PIPELINE),
            ("stage", "QwenImage21InputValidationStage",
             rt / "pipelines_core/stages/model_specific_stages/qwen_image21", FAKE_STAGE)):
        pkg.mkdir(parents=True)
        q = pkg
        while q != fake / "sglang/multimodal_gen":
            (q / "__init__.py").exists() or (q / "__init__.py").write_text("")
            q = q.parent
        (pkg / "__init__.py").write_text(FAKE_PART.format(part=part, cls=cls))
        (pkg / "_with.py").write_text(with_)
        (pkg / "_without.py").write_text(without)
    venv = d / "lane/venv/bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text(f'#!/bin/sh\nPYTHONPATH="{fake}" exec /usr/bin/python3 "$@"\n')
    (venv / "python").chmod(0o755)
    for repo, rev in cached:
        (d / unit_hf / "hub" / ("models--" + repo.replace("/", "--")) / "snapshots" / rev).mkdir(parents=True)
    for repo, ref in refs:
        r = d / unit_hf / "hub" / ("models--" + repo.replace("/", "--")) / "refs/main"
        r.parent.mkdir(parents=True, exist_ok=True)
        r.write_text(ref)
    # (repo, GiB): a sparse blob of that apparent size, what a fetch that stopped leaves
    for repo, gib in blobs:
        b = d / unit_hf / "hub" / ("models--" + repo.replace("/", "--")) / "blobs"
        b.mkdir(parents=True, exist_ok=True)
        with open(b / ("0" * 64 + ".incomplete"), "wb") as f:
            f.truncate(gib * 2**30)
    unit = d / "etc/qwen38-image.service"
    unit.parent.mkdir()
    unit.write_text(textwrap.dedent(f"""\
        [Service]
        WorkingDirectory={d}/lane
        Environment=HF_HOME={d}/{unit_hf}
        ExecStart={d}/lane/venv/bin/sglang serve \\
          --model-path {model} \\
          --performance-mode speed \\
          --host 127.0.0.1 --port 30020
        """))
    bin_ = d / "bin"
    bin_.mkdir()
    (bin_ / "sudo").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "sudo $*" >> "{d}/log"
        if [ "$1" = install ]; then cp "$4" "$5"; fi
        exit 0
        """))
    en = " ".join(enabled) or "none"
    ac = " ".join(active) or "none"
    (bin_ / "systemctl").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        case "$1" in
          is-enabled) for u in {en}; do [ "$3" = "$u" ] || [ "$2" = "$u" ] && exit 0; done; exit 1 ;;
          is-active) for u in {ac}; do [ "$3" = "$u" ] || [ "$2" = "$u" ] && exit 0; done; exit 1 ;;
        esac
        exit 0
        """))
    (bin_ / "df").write_text(f"#!/bin/sh\necho Avail; echo {free_gb}G\n")
    for f in ("sudo", "systemctl", "df"):
        (bin_ / f).chmod(0o755)
    return d, unit, grid


def run(d, unit, grid, choice):
    script = ('set -euo pipefail\ndie(){ echo "DIE: $*"; exit 1; }\n'
              f'CHOICE={choice}; REPO_DIR="{REPO}"; CONFIG_DIR="{d}/cfg"; HF_CACHE="{d}/hf"; DL_NO_BARS=1\n'
              f'IMAGE_UNIT="{unit}"; IMAGE_UNIT_NAME=qwen38-image.service\n'
              f'IMG_STAGE="$CONFIG_DIR/qwen38-image.service.switch-stage"\n'
              'mkdir -p "$CONFIG_DIR"\ndisable_rollback_lane(){ :; }\n'
              + DLFREE + BLOCK)
    parts = grid if isinstance(grid, str) else "config,pipeline,stage" if grid else ""
    env = {"PATH": f"{d}/bin:/usr/bin:/bin", "HOME": str(d), "FAKE_LOG": str(d / "log"),
           "FAKE_PARTS": parts}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env)
    log = (d / "log").read_text() if (d / "log").exists() else ""
    return r.returncode, r.stdout + r.stderr, log


def model_of(unit):
    return re.search(r"--model-path (\S+)", unit.read_text()).group(1)


class TheTurboTarget(unittest.TestCase):
    def test_the_pins_are_full_commits(self):
        for k in ("IMAGE_MODEL_PIN_REV", "IMAGE_TURBO_PIN_REV"):
            self.assertRegex(PINS[k], r"^[0-9a-f]{40}$", k)
        self.assertEqual(TURBO, "Qwen/Qwen-Image-2.1-Turbo")

    def test_base_to_turbo_rewrites_the_model_path_and_nothing_else(self):
        d, unit, g = sandbox(BASE)
        before = unit.read_text()
        rc, out, log = run(d, unit, g, "image-turbo")
        self.assertEqual(rc, 0, out)
        self.assertEqual(model_of(unit), TURBO)
        self.assertEqual(unit.read_text(), before.replace(f"--model-path {BASE} ", f"--model-path {TURBO} "))
        self.assertIn(f"sudo install -m 644 {d}/cfg/qwen38-image.service.switch-stage {unit}", log)
        self.assertIn("sudo systemctl daemon-reload", log)
        self.assertFalse((d / "cfg/qwen38-image.service.switch-stage").exists(), "the stage is left behind")

    def test_the_turbo_is_fetched_at_its_pin_and_served_by_that_ref(self):
        d, unit, g = sandbox(BASE)
        rc, out, log = run(d, unit, g, "image-turbo")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"hub {TURBO} {PINS['IMAGE_TURBO_PIN_REV']} local=True", log)
        self.assertIn(f"hub {TURBO} {PINS['IMAGE_TURBO_PIN_REV']} local=False", log)
        ref = d / "hf/hub/models--Qwen--Qwen-Image-2.1-Turbo/refs/main"
        self.assertEqual(ref.read_text(), PINS["IMAGE_TURBO_PIN_REV"])

    def test_a_cached_turbo_is_not_fetched_again(self):
        d, unit, g = sandbox(BASE, cached=[(TURBO, PINS["IMAGE_TURBO_PIN_REV"])])
        rc, out, log = run(d, unit, g, "image-turbo")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("local=False", log)

    def test_a_runtime_without_the_grid_is_refused_before_anything_changes(self):
        d, unit, _ = sandbox(BASE)
        before = unit.read_text()
        rc, out, log = run(d, unit, False, "image-turbo")
        self.assertNotEqual(rc, 0)
        self.assertIn("cannot sample on the Turbo's eight-step grid", out)
        self.assertIn("its pipeline config has no place for a sigma grid", out)
        self.assertEqual(unit.read_text(), before)
        self.assertNotIn("sudo", log)
        self.assertNotIn("hub ", log)

    def test_each_part_of_the_fix_is_needed(self):
        """A patch that does not apply to a new pin is only a note in the installer: a runtime
        with the config's field and not the rest would still sample on a uniform schedule."""
        for parts, why in (("config", "its pipeline does not read the grid from model_index.json"),
                           ("config,pipeline", "its input stage does not take the steps from the grid"),
                           ("pipeline,stage", "its pipeline config has no place for a sigma grid"),
                           ("config-field,pipeline,stage", "its pipeline config does not sample on the grid it holds")):
            d, unit, _ = sandbox(BASE)
            rc, out, log = run(d, unit, parts, "image-turbo")
            self.assertNotEqual(rc, 0, parts)
            self.assertIn(why, out, parts)
            self.assertEqual(model_of(unit), BASE, parts)

    def test_turbo_back_to_base(self):
        """The base's name is a prefix of the Turbo's: the rewrite must not leave '-Turbo'."""
        d, unit, g = sandbox(TURBO)
        rc, out, log = run(d, unit, g, "image")
        self.assertEqual(rc, 0, out)
        self.assertEqual(model_of(unit), BASE)
        self.assertNotIn("Turbo", unit.read_text())
        self.assertIn(f"hub {BASE} {PINS['IMAGE_MODEL_PIN_REV']} local=True", log)

    def test_base_to_base_writes_no_unit(self):
        d, unit, g = sandbox(BASE, cached=[(BASE, PINS["IMAGE_MODEL_PIN_REV"])])
        before = unit.read_text()
        rc, out, log = run(d, unit, g, "image")
        self.assertEqual(rc, 0, out)
        self.assertEqual(unit.read_text(), before)
        self.assertNotIn("sudo install", log)
        self.assertIn("sudo systemctl enable qwen38-image.service", log)

    def test_a_custom_checkpoint_is_kept_by_image_and_fetched_at_main(self):
        d, unit, g = sandbox("someone/finetune")
        rc, out, log = run(d, unit, g, "image")
        self.assertEqual(rc, 0, out)
        self.assertEqual(model_of(unit), "someone/finetune")
        self.assertIn("hub someone/finetune main local=True", log)
        self.assertNotIn("sudo install", log)

    def test_no_room_stops_before_the_first_byte(self):
        d, unit, g = sandbox(BASE, free_gb=20)
        before = unit.read_text()
        rc, out, log = run(d, unit, g, "image-turbo")
        self.assertNotEqual(rc, 0)
        self.assertIn("needs about 34 GB", out)
        self.assertNotIn("local=False", log)
        self.assertEqual(unit.read_text(), before)

    def test_a_serving_image_lane_is_told_to_restart(self):
        d, unit, g = sandbox(BASE, active=("qwen38-image.service",))
        rc, out, log = run(d, unit, g, "image-turbo")
        self.assertEqual(rc, 0, out)
        self.assertIn("sudo systemctl restart qwen38-image.service   (it serves the previous checkpoint until then)", out)
        self.assertNotIn("already named this checkpoint", out)

    def test_from_a_text_lane_the_text_lane_is_disabled_and_remembered(self):
        d, unit, g = sandbox(BASE, enabled=("qwen38-sglang.service",), active=("qwen38-sglang.service",))
        rc, out, log = run(d, unit, g, "image-turbo")
        self.assertEqual(rc, 0, out)
        self.assertIn("sudo systemctl disable qwen38-sglang.service", log)
        self.assertEqual((d / "cfg/lane-before-image").read_text().strip(), "qwen38-sglang.service")
        self.assertIn("sudo systemctl stop qwen38-sglang.service && sudo systemctl start qwen38-image.service", out)


class TheServedRevision(unittest.TestCase):
    """refs/main is what the offline unit serves: an install made with IMAGE_MODEL_REV= stays
    served through a switch, and the pin answers only when there is no ref to read."""
    OTHER = "a" * 40

    def test_a_ref_the_box_serves_is_kept_not_moved_to_the_pin(self):
        d, unit, g = sandbox(TURBO, cached=[(BASE, self.OTHER)], refs=[(BASE, self.OTHER)])
        rc, out, log = run(d, unit, g, "image")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"hub {BASE} {self.OTHER} local=True", log)
        self.assertNotIn(PINS["IMAGE_MODEL_PIN_REV"], log)
        self.assertNotIn("local=False", log)
        self.assertEqual((d / "hf/hub/models--Qwen--Qwen-Image-2.1/refs/main").read_text(), self.OTHER)
        self.assertEqual(model_of(unit), BASE)

    def test_the_ref_is_read_where_the_unit_keeps_its_cache(self):
        """The unit's own HF_HOME, which can be another disk than the switch's HF_CACHE."""
        d, unit, g = sandbox(TURBO, unit_hf="hf2", cached=[(BASE, self.OTHER)], refs=[(BASE, self.OTHER)])
        elsewhere = d / "hf/hub/models--Qwen--Qwen-Image-2.1/refs/main"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_text("b" * 40)
        rc, out, log = run(d, unit, g, "image")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"hub {BASE} {self.OTHER} local=True", log)
        self.assertNotIn("b" * 40, log)

    def test_the_turbo_too(self):
        d, unit, g = sandbox(BASE, cached=[(TURBO, self.OTHER)], refs=[(TURBO, self.OTHER)])
        rc, out, log = run(d, unit, g, "image-turbo")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"hub {TURBO} {self.OTHER} local=True", log)
        self.assertNotIn(PINS["IMAGE_TURBO_PIN_REV"], log)

    def test_a_ref_that_holds_no_commit_falls_back_to_the_pin_and_is_repaired(self):
        d, unit, g = sandbox(TURBO, refs=[(BASE, "main\n")])
        rc, out, log = run(d, unit, g, "image")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"hub {BASE} {PINS['IMAGE_MODEL_PIN_REV']} local=True", log)
        self.assertEqual((d / "hf/hub/models--Qwen--Qwen-Image-2.1/refs/main").read_text(),
                         PINS["IMAGE_MODEL_PIN_REV"])


class ACheckpointOfItsOwn(unittest.TestCase):
    """A unit installed with IMAGE_MODEL= serves a checkpoint that is neither the base nor the
    Turbo: image-turbo and back again brings that one back, not the base."""

    def test_the_round_trip_brings_it_back(self):
        d, unit, g = sandbox("someone/finetune")
        rc, out, log = run(d, unit, g, "image-turbo")
        self.assertEqual(rc, 0, out)
        self.assertEqual(model_of(unit), TURBO)
        self.assertEqual((d / "cfg/image-model-before-turbo").read_text().strip(), "someone/finetune")
        rc, out, log = run(d, unit, g, "image")
        self.assertEqual(rc, 0, out)
        self.assertEqual(model_of(unit), "someone/finetune")
        self.assertIn("hub someone/finetune main local=True", log)
        self.assertNotIn(f"hub {BASE} ", log)
        self.assertFalse((d / "cfg/image-model-before-turbo").exists(), "the note outlived the way back")

    def test_from_the_base_nothing_is_written_down_and_image_is_the_base(self):
        d, unit, g = sandbox(BASE)
        (d / "cfg").mkdir()
        (d / "cfg/image-model-before-turbo").write_text("someone/stale\n")
        rc, out, log = run(d, unit, g, "image-turbo")
        self.assertEqual(rc, 0, out)
        self.assertFalse((d / "cfg/image-model-before-turbo").exists(), "a stale note survived a switch from the base")
        rc, out, log = run(d, unit, g, "image")
        self.assertEqual(rc, 0, out)
        self.assertEqual(model_of(unit), BASE)


class TheRoomCheck(unittest.TestCase):
    """About 34 GB for a whole checkpoint, less what the cache already holds of it."""

    def test_a_fetch_that_stopped_needs_only_the_rest(self):
        d, unit, g = sandbox(BASE, free_gb=20, blobs=[(TURBO, 30)])
        rc, out, log = run(d, unit, g, "image-turbo")
        self.assertEqual(rc, 0, out)
        self.assertIn(f"hub {TURBO} {PINS['IMAGE_TURBO_PIN_REV']} local=False", log)

    def test_the_rest_must_still_fit(self):
        d, unit, g = sandbox(BASE, free_gb=3, blobs=[(TURBO, 30)])
        rc, out, log = run(d, unit, g, "image-turbo")
        self.assertNotEqual(rc, 0)
        self.assertIn("needs about 4 GB more", out)
        self.assertNotIn("local=False", log)
        self.assertEqual(model_of(unit), BASE)


class TheRestartHint(unittest.TestCase):
    def test_no_rewrite_no_claim_that_it_serves_the_previous_checkpoint(self):
        d, unit, g = sandbox(BASE, cached=[(BASE, PINS["IMAGE_MODEL_PIN_REV"])], active=("qwen38-image.service",))
        rc, out, log = run(d, unit, g, "image")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("serves the previous checkpoint", out)
        self.assertIn("already named this checkpoint", out)


class TheTargetIsKnownEverywhere(unittest.TestCase):
    def test_the_usage_line_and_the_cockpit_offer_it(self):
        self.assertIn("|image|image-turbo|video)", TEXT)
        cockpit = (REPO / "dashboard/cockpit.py").read_text()
        self.assertIn('"image-turbo"', cockpit)
        self.assertIn(f'IMAGE_TURBO_MODEL = "{TURBO}"', cockpit)

    def test_the_sudoers_line_is_the_stage_the_script_uses(self):
        sud = (REPO / "dashboard/sudoers-cockpit.template").read_text()
        self.assertIn("NOPASSWD: /usr/bin/install -m 644 __HOME__/.config/qwen38/qwen38-image.service.switch-stage "
                      "/etc/systemd/system/qwen38-image.service", sud)
        self.assertRegex(TEXT, r'(?m)^IMG_STAGE="\$CONFIG_DIR/qwen38-image\.service\.switch-stage"$')

    def test_uninstall_knows_the_turbo(self):
        self.assertIn(TURBO, (REPO / "uninstall.sh").read_text())


if __name__ == "__main__":
    unittest.main()
