# The video lane: MiniMax-H3 on one Spark

Text to video with joint video-and-audio, plus first/last-frame conditioning, served by
SGLang Diffusion in its own venv, on its own port, driven from the cockpit's **Video**
tab. Opt-in, because it costs about 150 GB of headroom and an hour or more:

```bash
./install.sh --with-video
# or, from nothing at all:
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | bash -s -- --with-video
```

Once installed, a plain `./install.sh` keeps it and updates it. `./install-video.sh`
installs or repairs it on its own, and `./install-video.sh --uninstall` removes the unit
and the runtime (the checkpoint stays in your HF cache); when the video lane was the one
the box booted on, the lane it replaced is enabled at boot again. The checkpoint goes to
the cache the installed unit names (`HF_HOME`), else the one the text lane mounts, else
`~/.cache/huggingface`, and `HF_CACHE=` picks another; the room it needs is measured on
that disk and on the one the runtime goes to (`VIDEO_LANE_DIR`).

## A fourth lane, switched to like the other three

Once installed, the video lane is driven exactly like the 27B, flash and image lanes,
from the same three controls at the top of the cockpit, and it obeys the same rule.

1. **Pick `MiniMax-H3`** in the switcher (it sits under its own *Video* heading)
   and press **Switch**. `switch-model.sh video` verifies the checkpoint and makes the
   video lane the one unit enabled at boot. Like every switch, it never starts or
   stops an engine.
2. **Stop** the lane that is serving.
3. **Start MiniMax-H3**. It answers in about 12 min; the lane pill, the Engines card
   and the Video tab all follow the boot in the engine's own words, and Generate turns
   on by itself.

Back is the same three moves the other way. From a terminal the switch is
`./switch-model.sh video` (or `stock`), and it prints the two commands that follow.

**Never two engines at once.** The video checkpoint (fl2va partition plus shared
components) does not fit beside any serving
lane. The cockpit refuses to start any engine while another one is busy, and says which
one to stop, for all four lanes alike. The unit also carries `Conflicts=` with the
other engine units, as a second belt for a `systemctl start` typed at a terminal.

The Engines tab's Flush cache, Abort all and Smoke are greyed out while the video lane
serves: they talk to the text engine on :30000, which is closed then.

## Why this model, and why with no flags

The SGLang cookbook verifies MiniMax-H3 on the DGX Spark itself: about 12.1 s per
denoise step steady-state, about 40 s of decode, about 12 min per warm request at
480P, with no flags at all, and adding the discrete-GPU offload flags measured 2.1x
slower on the same box. So this lane passes no placement flags: the verified recipe
is the whole recipe, and forcing any of those is how a verified recipe stops being
the verified recipe.

The unit serves one variant, `fl2va` by default: text-only requests plus first-frame,
last-frame, or first+last-frame conditioning. The `ref2va` weights (reference
image/audio/video conditioning) are the other checkpoint partition; `VIDEO_VARIANT=`
installs the lane on them instead, and the cockpit refuses keyframes it cannot serve.

Video and audio are denoised jointly in one pass and muxed into one MP4: the sound is
not dubbed afterwards, so the two stay in sync.

## The cookbook's recipe, nothing added

This lane serves the cookbook's MiniMax-H3 recipe exactly as upstream wrote it at the
pinned commit: **no local source patch**. The image lane carries two (an idle-loop wait
and a graceful-shutdown bound), and this lane deliberately does not. The honest costs
of that choice are stated here instead of patched around:

- **A stop cancels the generation in flight.** There is no abort endpoint for a
  running job, but the runtime cancels its tasks as it shuts down: all seven logged
  stops finished in under a second (journal, measured 2026-09-28), so
  `TimeoutStopSec` (60 min) is the ceiling, not the wait. The cockpit's **Cancel**
  is a restart through that stop: the video being made is lost, and the lane
  answers again in about 12 min, which is the boot, not the stop.
- **An idle lane may hold a CPU core.** The diffusion scheduler's loop never waits;
  the image lane patches that, this one does not. The installer measures the idle cost
  and prints it. It serves either way.

`sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va --host 127.0.0.1
--port 30022`, with `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` (the cookbook:
the decode sits close enough to the cap that fragmentation otherwise tips it over),
`HF_HUB_OFFLINE=1`, `PYTHONUNBUFFERED=1`, `--output-path` naming the lane's own
`outputs/` directory, and `--input-save-path ""`. No `--api-key` and no `--sleep-on-idle`: the
diffusion parser has neither, so the loopback bind is the only thing standing in front
of the lane, and the cockpit is the authenticated door.

One deliberate difference from the image lane: the lane's own `outputs/` directory
instead of an emptied one. Left empty, the server answers into a
temp dir it deletes when the request ends, and every completed video 404s on download
(measured 2026-09-25: completed in 592 s, file gone before the download). Old MP4s stay
until deleted by hand.

## The call, and what it costs

`POST /v1/videos` creates (prompt, `task` t2va/fl2va, seconds 4 to 15, size, `target`
with short edge, aspect ratio and duration, quality, `num_outputs_per_prompt`,
`num_inference_steps`, `flow_shift`, `audio_flow_shift`, seed), answers 200 with an
id in a queued state, and `GET /v1/videos` says when it is completed;
`GET /v1/videos/{id}/content` downloads the MP4. Both `task` and `target` are
required by the lane (measured 2026-09-25: absent, each 400s), so the cockpit always
sends them. Keyframes travel as `conditions` with keyframe roles, staged as files
this user owns and removed with the call.

One request at a time: the cookbook sizes one 480P request near what this box holds,
and on unified memory running out hangs the machine rather than failing the request.
The cockpit refuses a second one in under a millisecond and says why. That is caution,
not a measurement: re-measure with two overlapping calls before ever lifting it.

The page itself refuses before anything leaves the box: no prompt, a duration outside
4 to 15 s (the cookbook's band), steps outside 1 to 100, a seed that is not a whole
number, and a call whose estimate passes two hours' nine tenths. The cockpit filters
the rest instead of refusing it: more than one video per call, an
unknown task, keyframes on the `ref2va` weights, a malformed keyframe, undeclared keys
(a LoRA path, an output path, a kwargs blob are dropped, and the lane 400s what it
cannot serve), a ratio other than
16:9 or 9:16, and a canvas past 1280x720 -- the image lane's `IMAGE_MAX_PIXELS` rule,
because the largest canvas measured here peaks at 81.9 GB of 121.6 and past it the cost
is a guess and the OOM is a certain one.

The lock is held for up to two hours: one hour holding the call open, then the hand-off
watcher taking it back from the lane once the generation ends. So a call estimated past
two hours' nine tenths is refused at admission -- the tenth is slack, added 2026-09-28
when a review found the guard had none: the largest accepted estimate landed the lock's
deadline exactly on the lane's expected finish. Cost is linear in step-seconds at the
sizes measured here, 3.05 s each at 480P and 7.65 at 720P (the decodes folded in), plus
the ~10 % a keyframe conditioning measured; the tab carries the same numbers and refuses
the same calls, a parity test holds the two files equal. A call that outlives the hour of
waiting answers 504 with its video id kept: the tab shows the content URL, which serves
the MP4 the moment the lane is done. A hand-off parks its staged keyframes until the
watcher is done: the lane reads those files when the job starts, not when the POST
arrived (found in review, 2026-09-28). The same two hours is why an interrupted or long
request never lets a second generation start beside the first.

Licence: MiniMax-H3 ships under its own licence. Read it before commercial use.

## What is measured here, and what is not yet

Boot, measured on the reference box on 2026-09-25 (start 23:08:50, ready 23:20:00):
11 min, of which 33 s load the weights (text encoder 48.09 GB, DiT 61.73 GB in 13
shards, audio VAE 0.56 GB, video VAE 5.2 GB) and 10 min run three warmup requests
(~200 s each at 1344x768). First request after that runs at full speed: no JIT tax
was measured.

First text-to-video, measured the same night: 4 s at 480P in 592 s end to end
(denoise 555 s at 11.1 s/step over the 50-step default, decode 27 s), peak memory 9242 MB,
MP4 (h264 864x480) + muxed audio (aac stereo) inspected and played. That beats the
cookbook's ~12 min for the same shape. A second 4 s 480P the same night completed in
about 10 min with the download proven. The text encoder runs ~5.5 min per request
and does not warm up (cookbook figure, not measured here: the measured sum above
leaves it about 9 s, which is what the cockpit budgets).

Not yet measured here: 15 s durations, idle cores, second request while one runs
(refused until measured). A 4 s fl2va from a keyframe completed the same night in
about 11 min with the download proven, continuing the frame's scene; a 4 s 720P
completed in about 25 min (server reports 1280x704 for a 1280x720 ask), peak memory
81934 MB against 9242 MB at 480P: pixels cost far more than linear here, and 720P is
near what this box holds. The Video tab's cost table is fully measured here as of
2026-09-25, every row from this box; the cookbook figures live on as prose reference
where no row exists yet. The cost estimate extrapolates these two rows linearly
in step-seconds to the tab's caps (15 s, 100 steps); the 720P row is all-in, so its
denoise/decode split is one assumed number, and 15 s runs are not yet timed here.
