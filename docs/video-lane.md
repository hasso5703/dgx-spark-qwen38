# The video lane: MiniMax-H3 on one Spark

Text to video with joint video-and-audio, plus first/last-frame conditioning, served by
SGLang Diffusion in its own venv, on its own port, driven from the cockpit's **Video**
view. Every plain install includes it since v1.20 (about 165 GB of headroom: 135 GiB of
checkpoint and the Turbo adapter's 0.73, 11 of runtime, 16 for the runtime's own caches), and
proves it with one 4 s video the first time. `--no-video` leaves it out, and later runs
remember that; `--with-video` brings it back:

```bash
./install.sh --with-video
# or, from nothing at all, with every other lane:
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | bash
```

Once installed, a plain `./install.sh` keeps it and updates it. `./install-video.sh`
installs or repairs it on its own, and `./install-video.sh --uninstall` removes the unit
and the runtime (the checkpoint stays in your HF cache); when the video lane was the one
the box booted on, the lane it replaced is enabled at boot again. The checkpoint goes to
the cache the installed unit names (`HF_HOME`), else the one the text lane mounts, else
`~/.cache/huggingface`, and `HF_CACHE=` picks another; the room it needs is measured on
that disk, on the one the runtime goes to (`VIDEO_LANE_DIR`) and on the one under `$HOME`
where the runtime keeps its caches, summed for the parts that share a disk. Those caches
are written at the lane's first start and reused after: copies of 52 transformed DiT
weights under `~/.cache/sglang/diffusion/host_spill` (11.2 GiB) and the video decoder's
weights in its decode dtype under `~/.cache/sgl_diffusion/decode_dtype_store` (4.5 GiB).
`uninstall.sh` lists both.

## A fourth lane, switched to like the other three

Once installed, the video lane is driven exactly like the 27B, flash and image lanes,
from the same three controls at the top of the cockpit, and it obeys the same rule.

1. **Load it.** On the cockpit's Lanes view pick `MiniMax-H3` and press **Load**: one
   journey that switches the boot, stops the serving lane and starts the video lane,
   each step its own job, asking first and showing the exact command. Like every
   switch, `switch-model.sh video` itself verifies the checkpoint and never starts or
   stops an engine.
2. It answers in about 12 min; the lane pill, the Lanes view and the Video view all
   follow the boot in the engine's own words, and Generate turns on by itself.

Back is the same three moves the other way. From a terminal the switch is
`./switch-model.sh video` (or `stock`), and it prints the two commands that follow.

**Never two engines at once.** The video checkpoint (fl2va partition plus shared
components) does not fit beside any serving
lane. The cockpit refuses to start any engine while another one is busy, and says which
one to stop, for all four lanes alike. The unit also carries `Conflicts=` with the
other engine units, as a second belt for a `systemctl start` typed at a terminal.

The engine actions menu's Flush cache, Abort all and Smoke are greyed out while the video lane
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

## The cookbook's recipe, and one local change

This lane serves the cookbook's MiniMax-H3 recipe as upstream wrote it at the pinned
commit, the v0.5.21 release's as the image lane's (main's `ddebc52` until v1.25), with
**one local source patch**: the image lane's idle-loop wait
(`image-sglang/scheduler-idle-poll.patch`, one file for both lanes: the loop it patches is
the same at both lanes' pinned commits, which CI checks against the real files). The diffusion scheduler's loop never waits, so a lane with nothing to do
held one CPU core at 100%, which kept the box's hottest zone near 61 °C at rest (43 °C
with no lane loaded) and its fans loud. Measured on the reference box (2026-10-06), 3 min
after the lane was ready: 1.05 cores without the patch, 0.05 with it, the hottest zone 67
°C against 54 °C; a 4 s 480P request was picked up in the second it arrived and ran in
691 s, and a stop at rest took 0.24 s. The image lane's other patch, the
graceful-shutdown bound, stays out, and the honest costs of that are stated here instead
of patched around:

- **A stop cancels the generation in flight.** There is no abort endpoint for a
  running job, but the runtime cancels its tasks as it shuts down: all seven logged
  stops finished in under a second (journal, measured 2026-09-28), so
  `TimeoutStopSec` (60 min) is the ceiling, not the wait. The cockpit's **Cancel**
  is a restart through that stop: the video being made is lost, and the lane
  answers again in about 12 min, which is the boot, not the stop.
- **An idle lane holds about 0.05 of a CPU core.** The installer measures it after its
  test video and prints it, with a note past half a core: the patch is then not in
  effect, for example on a new pin it no longer fits. The lane serves either way.

`sglang serve --model-path MiniMaxAI/MiniMax-H3 --model-variant fl2va --host 127.0.0.1
--port 30022`, with `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True` (the cookbook:
the decode sits close enough to the cap that fragmentation otherwise tips it over),
`HF_HUB_OFFLINE=1`, `PYTHONUNBUFFERED=1`, `--output-path` naming the lane's own
`outputs/` directory, and `--input-save-path ""`. No `--api-key` and no `--sleep-on-idle`: the
diffusion parser has neither, so the loopback bind is the only thing standing in front
of the lane's API, and the cockpit is the authenticated door. The process's own
coordination ports are another matter: PyTorch's store listens on `*:30005` and gloo on
four ports of the LAN address (measured 2026-09-29 and 2026-09-30), with no setting to narrow them, like
every engine here; SECURITY.md says what they expose and how to firewall them.

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
The cockpit refuses a second one in under a millisecond and says why. Sent straight to
the lane, a second call waits in the lane's own queue and starts when the first ends
(measured 2026-09-29): the runtime never runs two at once, and the cockpit's refusal
tells the person at once instead of leaving them ten minutes or more behind an
unannounced first call.

While the lane runs its three warm-up requests, about 10 min after it loads, it holds a
new call until they are done. The cockpit's call gives up after a minute and says the
lane is warming up or busy; it said to start the lane until 2026-09-30, which it was.

The page itself refuses before anything leaves the box: no prompt, a duration outside
4 to 15 s (the cookbook's band), steps outside 1 to 100, a seed that is not a whole
number, and a call whose estimate passes two hours' nine tenths. The cockpit filters
the rest instead of refusing it: more than one video per call, an
unknown task, keyframes on the `ref2va` weights, a malformed keyframe, undeclared keys
(a LoRA path, an output path, a kwargs blob are dropped, and the lane 400s what it
cannot serve), a ratio other than
16:9 or 9:16, and a canvas past 1280x720 -- the image lane's `IMAGE_MAX_PIXELS` rule,
because the largest canvas measured here peaks at 80.0 GiB of 121.6 and past it the cost
is a guess and the OOM is a certain one. The page and the cockpit also refuse a canvas
larger than 864x480 for longer than 4 s: 720P is measured at 4 s only, at 80 GiB, and the
time budget alone would have admitted it up to 15 s (found in the validation of
2026-09-29).

The lock is held for up to two hours: one hour holding the call open, then the hand-off
watcher taking it back from the lane once the generation ends. So a call estimated past
two hours' nine tenths is refused at admission -- the tenth is slack, added 2026-09-28
when a review found the guard had none: the largest accepted estimate landed the lock's
deadline exactly on the lane's expected finish. Cost is linear in step-seconds at the
sizes measured here, 3.05 s each at 480P (3.8 with the NVMe hot, 2026-09-29: the
largest call admitted still ends inside the lock) and 7.65 at 720P (the decodes folded in), plus
the ~10 % a keyframe conditioning measured; the view carries the same numbers and refuses
the same calls, a parity test holds the two files equal. A call that outlives the hour of
waiting answers 504 with its video id kept: the view shows the content URL, which serves
the MP4 the moment the lane is done. A hand-off parks its staged keyframes until the
watcher is done: the lane reads those files when the job starts, not when the POST
arrived (found in review, 2026-09-28). The same two hours is why an interrupted or long
request never lets a second generation start beside the first.

Licence: MiniMax-H3 ships under its own licence. Read it before commercial use.

## The Turbo switch

The Video view has a **Speed** choice beside the steps: the base, or the **Turbo**, the
cookbook's recommended speed and quality adapter for these weights, larryvrh's MiniMax-H3
Turbo LoRA (`minimax_h3_turbo_v4_step600_ema.safetensors`, Apache-2.0, 0.78 GB, fetched by
`install-video.sh` at its pinned revision, one file of a 112 GB repository). Its schedule
is its own, eight denoiser evaluations, which the view shows fixed as 9 steps: on this
lane's v0.5.21 runtime the request's `num_inference_steps` counts the points of the sigma
grid, the terminal zero among them. SGLang's main counts evaluations since
sgl-project/sglang#38671 (2026-10-10), which is why the cookbook now asks this adapter for
8 and says that older versions ran N-1 updates for the same request; a pin past that change
sends 8, and `tests/test_video_lane.py` holds the pin and the 9 together.

The cockpit puts the adapter on for a call that asks for it and takes it off after, under
the lane's lock, through the lane's own control routes (`/v1/list_loras`, `/v1/set_lora` in
SGLang's dynamic mode, `/v1/unmerge_lora_weights`); a browser never names it, and the
cockpit's next call meets the base: a base call does not start while the lane cannot say the
adapter the cockpit put on is off. A request sent straight to the lane while a Turbo video
runs is another matter: the lane queues it and starts it the moment the Turbo video ends,
before the cockpit takes the adapter off, so it runs with the adapter on, past the sizes the
Turbo is admitted at if it asks for them. A lane that runs an adapter merged into its weights
is left alone: this cockpit never merges one, and taking one out restores 62 GiB of weights
in memory. From a terminal the same three calls go around the video call; the view's copied
command shows them, with the adapter's path on the box (in the lane's HF cache, under
`hub/models--larryvrh--MiniMax-H3-Turbo-Lora/snapshots/43a74557ac3f6539db8e0f2a959d03feb7a81480/`).

An update does not restart a lane that is serving: it puts the new runtime in, notes beside
it the commit its source holds and the wheel under it (`sglang-source`, `sglang-wheel`, each
rewritten only when it changes), and the lane runs it from its next start. The cockpit puts
an adapter on only on a lane started after both notes and running the commit the Turbo was
measured on, the installer's pin. On any other lane (one that served through the update, one
pinned elsewhere with `SGLANG_DIFFUSION_PIN` or by a rollback, one whose start systemd does
not say) it offers no Turbo and puts no adapter on, and the view says why; an adapter it put
on before, on the same run of the lane, it still takes off. The runtime this lane ran before
v1.25 (main's `ddebc52`) puts an adapter on a model it offloads layer by layer by loading the
whole DiT, 62 GiB, into memory, where v0.5.21 leaves the offload alone.

Measured on the reference box on 2026-10-10, on the v0.5.21 runtime, 4 s at 480P, seed
20261010, three prompts (a dog on a beach with gulls, a woman speaking in a kitchen, a tram
at night in the rain):

| | base, 50 steps | Turbo, 9 steps |
|---|---:|---:|
| the server's time, end to end | 622.8 to 632.5 s | 126.2 to 127.9 s |
| of which text encoding | none, or 9.3 s | none |
| denoising | 598.6 to 599.3 s | 102.5 to 104.1 s |
| per denoiser evaluation | 12.2 s | 12.8 to 13.0 s |
| video decode | 22.8 to 23.0 s | 22.8 to 23.0 s |
| peak memory | 8.1 to 9.0 GiB | 20.8 to 25.3 GiB |

- Text encoding runs once per new prompt and is reused after: the base's two longer times
  include it, and the Turbo's prompts had been encoded by the base runs. The adapter holds
  DiT weights only, so a new prompt costs the Turbo the same 9.3 s. The same prompt, both
  encoded already: 127.3 and 127.9 s against 622.8 s. Two more, made from the Video view after
  the box's update to v1.25.0 with new prompts: 136.5 and 137.9 s on the server, peaks of 17.6
  and 20.2 GiB.
- On in 5.1 s the first time (the adapter read from the cache) and 0.02 s after; off in
  0.01 s.
- The base's next video, after an adapter went on and off, is identical to the byte to one
  made before it, twice (after this adapter, then after LightX2V's); the same Turbo request
  gives the same bytes three times, the third after LightX2V's adapter went on and off in
  between.
- What the woman says ("Good morning, the coffee is ready.") comes out word for word with
  both, transcribed by Whisper large-v3-turbo; the pictures come out as sharp as the base's
  at 50 steps, the same scenes though not the same frames (the kitchen prompt gives another
  woman), where the base at 8 steps comes out blurred.
- The table's Turbo runs had this adapter loaded alone, as the cockpit leaves the lane. A
  fifth run, made after LightX2V's adapter had been loaded beside it for the study (a lane
  keeps every adapter it loaded, active or not), took 131.8 s and peaked at 37.2 GiB.
- The adapter's own memory is the reason the Turbo is admitted at 480P and 4 s only: it adds
  12 to 17 GiB at the peak of a 4 s video, more as the video grows, and the base alone already
  peaks at 75.4 GiB at 8 s (the box's MemAvailable down to 28.4 GiB, measured 2026-10-10),
  76.5 GiB at 15 s and 80.0 GiB at 720P. Past 4 s the adapter would leave the box too little,
  and running out of unified memory hangs it. The view and the cockpit refuse the rest, and
  say why.

Studied and not offered:

- **The same adapter merged at startup** (`--lora-merge-mode auto`, the cookbook's launch):
  it denoised faster, 11.7 to 11.9 s per evaluation against 12.8 to 13.0 (about 9 s off a
  4 s video), and peaked at 8.9 to 11.0 GiB like the base, but the lane took 27 min to answer
  instead of 14 the same day (17 min of merge into a 62 GiB cache under
  `~/.cache/sgl_diffusion/lora_merge_cache`), switching back to the base would cost a
  restart, and its videos came out softer than the dynamic adapter's, the first prompt
  drifting back toward the base's framing. Upstream measured why: this adapter's update sits
  below a bf16 step of most weights, and a merge drops 77 to 97 % of it in typical layers.
  Since sgl-project/sglang#43385 (main, 2026-10-10) `auto` keeps such an adapter unmerged;
  v0.5.21 merges it.
- **LightX2V's 4-step adapter** (`lightx2v/Minimax-h3-Turbo`, the cookbook's most
  aggressive recipe, 5 steps with `lora_alpha` 8): 80.7 to 82.3 s, the same sentence spoken, but
  a more saturated picture and a first prompt that ends in a blurred close-up, at 31.7 to
  35.8 GiB of peak memory.

## What is measured here, and what is not yet

Boot, measured on the reference box on 2026-09-25 (start 23:08:50, ready 23:20:00):
11 min, of which 33 s load the weights (text encoder 48.09 GiB, DiT 61.73 GiB in 13
shards, audio VAE 0.56 GiB, video VAE 5.2 GiB) and 10 min run three warmup requests
(~200 s each at 1344x768). First request after that runs at full speed: no JIT tax
was measured.

First text-to-video, measured the same night: 4 s at 480P in 592 s end to end
(denoise 555 s at 11.1 s/step over the 50-step default, decode 27 s), peak memory 9.0 GiB,
MP4 (h264 864x480) + muxed audio (aac stereo) inspected and played. That beats the
cookbook's ~12 min for the same shape. A second 4 s 480P the same night completed in
about 10 min with the download proven. The text encoder runs ~5.5 min per request
and does not warm up (cookbook figure, not measured here: the measured sum above
leaves it about 9 s, which is what the cockpit budgets).

A 4 s fl2va from a keyframe completed the same night in about 11 min with the
download proven, continuing the frame's scene; a 4 s 720P completed in about 25 min
(server reports 1280x704 for a 1280x720 ask), peak memory 80.0 GiB against 9.0 GiB at
480P: pixels cost far more than linear here, and 720P is near what this box holds.

The move to the v0.5.21 release, 2026-10-10, on the same box, both runtimes on the same
requests one after the other, the box otherwise idle: a 4 s 480P video at 8 steps and at the
50-step default came out **identical to the byte** on both (the MP4 and every decoded
frame), the new runtime 2.5 % faster at 50 steps (622.8 s against 638.8, the server's own
times; the denoiser 599.1 s against 605.3), at the same peak memory at 50 steps (8.1 GiB;
9.0 against 9.3 GiB at 8 steps). Each of the three warm-up requests of their start, about
8 min in all, takes the box's MemAvailable down to 27 to 30 GiB on both, and gives it back
after (98 to 100 GiB free with the lane idle).

The validation of 2026-09-29, on the same box:

- **4 s at 480P took 759.6 s**, against 592 s on 2026-09-25: the DiT was read back from
  the NVMe at every step (4.36 GB/s, the drive at 76 °C). A request costs 9:52 to 12:40
  depending on how hot the NVMe runs, and the view says so.
- **15 s at 480P took 2,830 s** (47 min), peak memory 76.5 GiB, the box's MemAvailable
  never under 25.2 GiB.
- **An idle lane held 1.04 cores**, the diffusion scheduler's loop that never waits;
  0.05 since the image lane's patch went in (2026-10-06).
- **A second request sent straight to the lane queues** and runs after the first.
- A keyframe call removed its staged PNGs; a seed past a 64-bit integer is refused by
  the lane itself; a stop during a generation ends it as `cancelled`. The Video view's cost table is fully measured here as of
2026-09-25, every row from this box; the cookbook figures live on as prose reference
where no row exists yet. The cost estimate extrapolates these two rows linearly
in step-seconds to the view's caps (15 s, 100 steps); the 720P row is all-in, so its
denoise/decode split is one assumed number, and 15 s runs are not yet timed here.
