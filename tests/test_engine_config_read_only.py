#!/usr/bin/env python3
"""The docker engines see the config directory read-only.

Every docker engine of the text lanes mounted ~/.config/qwen38 at /out read-write, as root in
its container: the 27B and its 1M unit (both with --trust-remote-code), the flash launcher,
Kolibri, both Nemotron lanes, and run.sh. They only read there (a chat template,
engine-secrets.yaml, the flash draft's token map, the vLLM lanes' api-key), but the directory
also holds what systemd runs as this user before or as a lane starts (engine-preflight.sh,
engine-secrets.sh, launch-flash.sh, launch-speech.sh): code that ran in an engine, a
checkpoint's own or a flaw of the engine's, could rewrite them and run as the box's user at
the next start, which a crash brings on. The image and video lanes were closed to that on
2026-10-16 and 17 (a read-only tmpfs over the directory); this is the same for the docker
lanes (found in a cloud review, 2026-10-18, and not yet run on the box). Read-only is not
hidden: an engine still reads every file there, so the review gives a mount of only the files
each engine reads as the next step.
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
ENGINES = ("qwen38-sglang.service.template", "qwen38-sglang-1m.service.template",
           "qwen38-flash-launch.sh.template", "qwen38-flash-fp4local-launch.sh.template",
           "qwen38-kolibri.service.template",
           "qwen38-nemotron.service.template", "qwen38-nemotron-super.service.template", "run.sh")
# -v SOURCE:/out[:OPTIONS], the source in quotes or not
MOUNT = re.compile(r"""-v\s+["']?([^\s"':]+)["']?:/out(?::([\w,]+))?(?=["'\s]|$)""", re.M)
# what an engine is given to read under /out
READS = re.compile(r"(?:--chat-template|--config|--speculative-token-map)\s+/out/|QWEN38_KEY_FILE=/out/")


def code(name):
    """The file without its comment lines."""
    return "\n".join(ln for ln in (REPO / name).read_text().splitlines() if not ln.lstrip().startswith("#"))


class TheEnginesReadTheConfigDirectory(unittest.TestCase):
    def test_every_engine_mounts_it_read_only(self):
        for name in ENGINES:
            mounts = MOUNT.findall(code(name))
            self.assertEqual(len(mounts), 1, f"{name}: {mounts}")
            self.assertIn("ro", mounts[0][1].split(","), f"{name} mounts the config directory read-write")

    def test_what_an_engine_finds_there_it_only_reads(self):
        # a flag that wrote under /out would fail on a read-only mount: each /out/ path an
        # engine is given is one it reads
        for name in ENGINES:
            text = code(name)
            self.assertEqual(text.count("/out/"), len(READS.findall(text)),
                             f"{name}: an /out/ path that is not a chat template, a config, a token map or the key")

    def test_the_draft_vocabulary_job_still_writes_its_map_there(self):
        # not an engine: build-token-map.py writes the map it is asked for into /out, a short
        # job of the installer and the switch (lanes.sh), as root: unlike its siblings in
        # switch-model.sh, prepare-lane.sh and vllm-lane.sh it passes no --user (pointed out by
        # the box's own agent, 2026-10-18)
        text = code("lanes.sh")
        job = text[text.index("docker run --rm --init --entrypoint python3"):]
        job = job[:job.index("|| return 1")]
        self.assertEqual(MOUNT.findall(job), [("$CONFIG_DIR", "")], job)
        self.assertIn('--out "/out/$name"', job)

    def test_the_shapes_are_read(self):
        # so a pattern that stopped matching would not leave the scans above green and blind
        for line, want in (('  -v __HOME__/.config/qwen38:/out \\', [("__HOME__/.config/qwen38", "")]),
                           ('  -v __HOME__/.config/qwen38:/out:ro \\', [("__HOME__/.config/qwen38", "ro")]),
                           ('  -v "$CONFIG_DIR":/out:ro \\', [("$CONFIG_DIR", "ro")]),
                           ('  -v "/h/cfg:/out:ro,z" \\', [("/h/cfg", "ro,z")]),
                           ('  -v /h/cfg:/output \\', [])):
            self.assertEqual(MOUNT.findall(line), want, line)


if __name__ == "__main__":
    unittest.main()
