# The image lane: Qwen-Image 2.1 on one Spark

Text to image, image editing and native RGBA, served by SGLang Diffusion in its own
venv, on its own port, driven from the cockpit's **Image** view. Every plain install
includes it since v1.20 (40 GB: 31 checkpoint, 9 runtime), and proves it with one image the
first time. `--no-image` leaves it out, and later runs remember that; `--with-image` brings
it back:

```bash
./install.sh --with-image
# or, from nothing at all, with every other lane:
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | bash
```

Once installed, a plain `./install.sh` keeps it and updates it. `./install-image.sh`
installs or repairs it on its own, and `./install-image.sh --uninstall` removes the unit
and the runtime (the checkpoint stays in your HF cache); when the image lane was the one
the box booted on, the text lane it replaced is enabled at boot again. The checkpoint goes
to the cache the installed unit names (`HF_HOME`), else the one the text lane mounts, else
`~/.cache/huggingface`, and `HF_CACHE=` picks another; the room it needs is measured on
that disk and on the one the runtime goes to (`IMAGE_LANE_DIR`).

## A third lane, switched to like the other two

Once installed, the image lane is driven exactly like the 27B and flash lanes, from the
same single Load control, and it obeys the same rule.

1. **Load it.** On the cockpit's Lanes view pick `Qwen-Image 2.1` and press **Load**: one
   journey that switches the boot, stops the serving lane and starts the image lane,
   each step its own job, asking first and showing the exact command. Like every
   switch, `switch-model.sh image` itself verifies the checkpoint and never starts or
   stops an engine.
2. It answers in about 70 seconds; the lane pill, the Lanes view and the Image view
   all follow the boot in the engine's own words, and Generate turns on by itself.

Back to text is the same three moves the other way. From a terminal the switch is
`./switch-model.sh image` (or `stock`), and it prints the two commands that follow.

**Two checkpoints, one lane.** The Lanes card offers `Qwen-Image 2.1` and `Qwen-Image 2.1
Turbo`, Qwen's eight-step distillation of the same model (`./switch-model.sh image-turbo` from
a terminal). The base stays the default. The Turbo is about four and a half times faster for
the same uses, and its first switch downloads 32.5 GB; switching between the two rewrites the
unit's `--model-path` and nothing else, then restarts the lane (about 80 s). See
[The Turbo checkpoint](#the-turbo-checkpoint) below.

**Never two engines at once.** 31 GB of weights do not fit beside a serving LLM, and a
request takes the lane to 34.8 GiB at 1024x1024 (45.4 GiB at 2048x2048). The cockpit refuses to start any engine while
another one is busy, and says which one to stop, for all three lanes alike: starting the
image lane while the 27B serves comes back `409 blocked`, and so does starting the 27B
while the image lane loads. That gate used to pick "the other engine" with `[0]`, which
with three of them checked one neighbour in two.

The unit also carries `Conflicts=` with every text unit, as a second belt for a
`systemctl start` typed at a terminal, which the cockpit's gate never sees. Through the
cockpit it is never reached, because the start is refused first.

The Image view has no start or stop of its own. The first version had one, and it started
this lane by a path none of the others use, stopping the text lane silently through
`Conflicts=` where every other lane is refused with "stop it first". The view now says
which of the three moves is next, naming the buttons as they read on screen.

The engine actions menu's Flush cache, Abort all and Smoke are greyed out while the image lane
serves: they talk to the text engine on :30000, which is closed then. They used to test
"is an engine ready", which the image lane is.

## Why a venv and not the docker image

The cookbook is explicit for this model: *"This integration currently uses the
Python/source command; no published Docker image is verified."* So the lane runs a pinned
source checkout: the `v0.5.21` release (its tag's commit,
`e00930c5489053f26d86b179cee0d087f846acbb`), the first that carries Qwen-Image 2.1, with the
local patches below. From 2026-09-22 to 2026-10-10 it ran the `main` commit `ddebc52f237a`,
which no release held yet. The checkpoints are pinned too, at the revisions the lane was
measured with (`790c92633540aa0cb11d9abf19eb46d861714758` for the base, `d65dbc9a7e8f` for the
Turbo; `IMAGE_MODEL_REV` overrides them), and `./check-pins.sh` asks upstream daily whether
both checkpoints, the commit and the `0.5.21` wheel still resolve.

The released wheel still goes in **first**, and the order is not cosmetic: it carries
`sglang-kernel` built for aarch64, which a source tree does not build. Source first
leaves a runtime with no native kernels that dies on the first request. The wheel also sets
the versions of everything the runtime imports, so a pin that moves to a new release takes
that release's wheel too: the installer writes down the wheel a venv came from
(`sglang-wheel` in the lane's folder) and installs the new one over a venv that has another,
or no note at all. Until v1.24.0 it only checked that `sglang` imported, which would have
left a v0.5.20 venv's `cache-dit` 1.3.0 under v0.5.21, which asks for 1.5.1. The editable
overlay then goes on with `--no-deps`, because letting the source tree resolve again
pulls a `transformers` that breaks the encoder this model needs.

> The model card asks for `transformers>=5.17`. That applies to the diffusers pipeline,
> not to this one: SGLang has its own native encoder, and the cookbook says to keep its
> installed dependencies. The lane runs 5.12.1 and is right to.

### The local changes to the pinned source

Five, all applied by `install-image.sh` from `image-sglang/`, all checked against the real
upstream files at the pin by a CI step, and all skipped with a note if a future pin no longer
fits them. The first two are not about images, and the lane serves either way. The other
three are [sgl-project/sglang#43391](https://github.com/sgl-project/sglang/pull/43391), merged
upstream on 2026-10-10 and in no release yet, one file each, which the Turbo cannot do
without (the installer refuses to serve it on a runtime they did not reach); the next pin
that carries #43391 drops them.

#### An idle lane no longer holds a CPU core

The diffusion scheduler's loop never waits. `recv_reqs()` reads its socket with
`zmq.NOBLOCK`, and when the queue is empty the loop goes straight round again, so a lane
with nothing to do spins one core for as long as it serves. The text lanes avoid the same
thing with `--sleep-on-idle`; this runtime has no such flag (see below), and upstream
`main` has the same loop as the pin.

`image-sglang/scheduler-idle-poll.patch` adds one branch: with nothing queued, wait on the
request socket for up to a second, which is exactly what the LLM scheduler's own
`IdleSleeper` does behind `--sleep-on-idle`. `poll()` returns the moment a request lands,
so it costs no latency. Measured on the reference box, from the unit's cgroup:

| at rest, over 30 s | CPU |
|---|---:|
| without the patch (twice) | 1.047 / 1.046 cores, one thread at 101.5 % |
| with it (three times) | 0.045 / 0.046 / 0.046 cores |
| for comparison, the 27B lane with `--sleep-on-idle` | 0.029 cores |

The same fixed-seed request (768x768, 20 steps, seed 42, CPU generator) gave the same PNG,
byte for byte, with and without it: 10.2 to 10.7 s either way.

`install-image.sh` applies it after the checkout and recognises it on a re-run; it takes it
off before checking out a new pin, since git refuses to check out over a local edit of a
file the new commit changes. If a future pin changes that loop, the patch is skipped with a
note and the smoke test prints what the lane costs at rest, so nothing is hidden either way;
CI fetches the scheduler at the pin and fails when the patch no longer applies, which is
the signal to drop it (upstream fixed it) or refresh it.

#### A Stop during a generation stops in 5 s, not in a failed unit

The diffusion runtime starts its HTTP server with `uvicorn.run()` and no
`timeout_graceful_shutdown`, so on shutdown uvicorn waits for every open connection to
close, and a generation holds one for as long as it runs. On 2026-09-23 a Stop sent during
a call for ten 2048x2048 images at 60 steps (about 47 minutes of work) logged "Waiting for
connections to close", sat in "stopping" for the unit's 60 s, and systemd then killed it and
marked the unit failed (`Result=timeout`). Nothing was wrong with the lane; the page still
said "failed, read its journal".

`image-sglang/http-graceful-timeout.patch` passes `timeout_graceful_shutdown=5`: five
seconds for a request about to finish, then the requests in flight are cancelled and the
server exits. Measured with uvicorn 0.53 (the runtime's own) in a transient systemd unit
holding a ten-minute request: without the setting, "stop-sigterm timed out, Killing",
`Result=timeout`; with it, stopped in 5.2 s, `Result=success`.

## What it serves

The DGX Spark recipe from the cookbook, and nothing forced on top:

```bash
sglang serve --model-path Qwen/Qwen-Image-2.1 --performance-mode speed
```

One GPU, every component resident, full-image VAE decode, eager execution, batch 1. The
attention backend is deliberately **not** named: the runtime logs
`Defaulting to Torch SDPA backend on SM12.x` by itself, and writing it into the unit is
how a verified recipe quietly stops being one.

The unit adds `--output-path ""` with `--input-save-path ""`. By default this server
writes every image it makes **and every reference anyone uploads** under its working
directory, forever: 103 MB accumulated in one afternoon of testing. Empty string is read
back as `None` (`runtime/server_args/server_args.py:851` at the pin), the cockpit and the API take their pixels from the
response, and nothing needs the copy. Point them at a directory if you want an archive,
and watch it grow.

### There is no API key on this lane, and that is why it is loopback

The diffusion runtime is a **different argument parser** from the LLM one. It has no
`--api-key` and no `--sleep-on-idle`, and passing either kills the unit at startup:

```
error: unrecognized arguments: --sleep-on-idle --api-key ...
```

`sglang serve --help` lists both, which is the trap: with no resolvable diffusion model it
prints the LLM parser. The help text is not the evidence; the unit failing to start is.
This was found by the installer's own smoke test, on the first install.

So the lane cannot authenticate anyone. It binds **127.0.0.1**, and the cockpit is the
authenticated door in front of it. That bind does not follow `ENGINE_BIND`, which belongs
to the lane that does have a key; `IMAGE_BIND=<addr>` overrides it, and the installer says
out loud what that costs.

## The Turbo checkpoint

[`Qwen/Qwen-Image-2.1-Turbo`](https://huggingface.co/Qwen/Qwen-Image-2.1-Turbo), published by
Qwen on 2026-10-09 under the same Qwen Research licence as the base, is an eight-step
distillation of the same model. Compared file by file with the base: the transformer is the
distilled one (14.2 GB, same architecture and same index), the text encoder is the base's saved
again as one file (17.5 GB; only its `transformers_version` and a `pad_token_id: null` differ in
its config), the VAE comes in bf16 (0.7 GB against 1.35), and `model_index.json` carries an
eight-value `sample_sigmas` grid with `use_dynamic_shifting` off in the scheduler's config.

That grid is the whole point. A request runs on it whatever `num_inference_steps` says: on the
reference box `num_inference_steps: 2` gave the same PNG as leaving the field out. Without
sgl-project/sglang#43391 the runtime loads the Turbo's weights all the same and samples them on
a uniform schedule built from `num_inference_steps`, which is not what they were trained for;
with it (the three patches above) the grid reaches the sampler, and the base, which has no grid,
samples exactly as before.

**Measured on the reference box on 2026-10-10**, both checkpoints on the same runtime (v0.5.21
and the five patches), seed 42, CPU generator: see the table below. Same seed twice gave the
same bytes. The quality was looked at on eight prompts with one seed each, the Turbo against the
base at its 40 steps and at eight: a photograph, a portrait, a shop sign with French text and
prices, a counting prompt (three apples, two pears), a landscape, a watercolour, an interior
and an infographic. The Turbo is close to the base at 40 steps on the photographs, the portrait,
the illustrations and the short text (`Boulangerie Margot`, `Croissant 2,10 €` and
`Baguette 1,30 €` exact in both); both miscount the fruit (two apples and two pears for the
Turbo, four and three for the base); both garble the infographic's body text, the Turbo with
invented English words under unnumbered panels, the base with invented Chinese-looking
characters under its four numbered steps. The base at eight steps, for comparison, is visibly
unfinished on every prompt: what the distillation buys. A sample to look at, not a benchmark.

Transparency works the same way, asked for by the prompt: 70.9 % of the Turbo's pixels came
back under alpha 16 for a perfume bottle, 74.3 % of the base's for the same prompt and seed.
An edit with one reference takes 10.3 s.

The cockpit knows which checkpoint the unit names. Under the Turbo the Image view's step slider
shows 8 and is disabled, the request leaves `num_inference_steps` out (as the cookbook says),
and the estimate counts eight steps. Loading it from the Lanes card is the same journey as any
lane's: the switch rewrites `--model-path`, then the lane restarts (about 80 s).

## What it costs, measured here

Every number is a request made against this lane on a DGX Spark, not a figure from the
cookbook: the server's own time and peak memory, on the v0.5.21 runtime and its five patches,
2026-10-10, the median of the calibration runs: three for each size of the base, four of the
Turbo, five at 2048x2048, where one Turbo run took 52.0 s with a slow VAE decode against 38.9
to 41.0 s for the others. All fifteen requests of the base at 1024x1024 that day give 34.4 s,
all eighteen of the Turbo 7.5 s. The transparent and edit rows are one request each, the base
at 8 steps eight.

| request | base, 40 steps (the defaults) | Turbo, its 8 steps | peak, either |
|---|---:|---:|---:|
| 512x512 | 7.8 s | 1.8 s | 32.1 GiB |
| 768x768 | 19.5 s | 4.3 s | 33.3 GiB |
| **1024x1024** | **34.3 s** | **7.5 s** | 34.8 GiB |
| 1664x928 | 54.6 s | 11.9 s | 36.6 GiB |
| 2048x2048 (the model card's own size) | 182.8 s | 40.6 s | 45.4 GiB |
| 2752x1536 (the largest call admitted) | 185.2 s | 40.5 s | 45.5 GiB |
| transparent 1024x1024 | 34.9 s | 7.6 s | 34.8 GiB |
| edit, one reference | 41.9 s | 10.3 s | 34.9 GiB |
| the base at 8 steps, 1024x1024 | 7.5 s | | 34.8 GiB |

The cookbook publishes 35.36 s for this machine at 1024x1024/40; 34.3 s here. Cost is linear
in steps and worse than linear in pixels: four times the 1024x1024 pixels cost about five and
a third times the time (5.3 for the base, 5.4 for the Turbo). The cockpit's estimate is `0.42 + 0.25 x px^2.08 + steps x 0.883 x px^1.16`
seconds with `px` the pixels over 1024^2, a part per image (the text encoding and the VAE
decode) and a part per step (the denoiser), within 4.9 % of all twelve medians of both
checkpoints; an edit adds about `1.6 + 0.15 x steps` seconds per reference (one reference
measured, at 8 and at 40 steps). The GPU throttles itself on heat (`SW thermal slowdown`, seen
at 86 °C during the 40-step runs at 2048x2048), which is part of the spread between runs.
Startup is 60 to 90 s from a warm page cache, its one warm-up request included.

On the runtime before (the `main` commit `ddebc52f237a`, 2026-09-22), 1024x1024 at 40 steps
took 38.2 s; measured again the morning of 2026-10-10 beside the new one, 36.08 s against
34.40 s (median of three on each), the same twelve requests giving the same bytes on both, for
0.8 GiB more at the peak on the new one. Also measured on that runtime only: ten references at
20 steps in 69.6 s, two images in one call at 8 steps in 16.8 s.

Same seed, twice: byte-identical. The base at 8 steps is visibly unfinished and not worth its
7.5 s; the Turbo's 8 steps are finished (see above).

## One image at a time, and what happens if you ignore that

The diffusion scheduler has no admission cap. `batching_max_size` is about batching and
is already 1, so overlapping requests do not queue politely: each gets its own working
set. Measured here on 2026-09-22:

| | held by the process | free on the box |
|---|---:|---:|
| after boot, idle | 31.2 GB | 80 GB |
| eight generations, one after another | **31.2 GB, flat** | 80 GB |
| two generations at once | **90.5 GB** | 20 GB |

The second row is the important one: this lane does **not** leak. Eight sequential
1024x1024 generations held exactly the same memory as the first, and the engine reported
the same 33.2 GiB peak for every one of them.

The third row is what bites. On a box whose 121.6 GiB is shared between the CPU and the
GPU, two concurrent requests is most of it, and the engine that reached that state stopped
answering entirely: a direct curl to its port timed out, no request appeared in its log,
and `systemctl restart` was the only way back. Two browser tabs are enough to do it.

So the cockpit serializes. A second request while one is running comes back **HTTP 409 in
under a millisecond** with the reason, rather than queueing and holding one of the
browser's six connections to that origin. The Image view also turns its own button off when
the lane is busy with somebody else's request, which it can see because the engine names
its current stage in its log.

Nothing enforces this below the cockpit. A script that posts twice to port 30020 will
still do what the numbers above describe.

**The images of one call are one batch.** Ten 2048x2048 images in one call took 42 s per
denoising step on 2026-09-23, where one image takes 4.6: the pipeline runs them together,
and the memory that needs grows with them. Where it ends for a call that size has not been
measured, and on this box running out of unified memory hangs the machine instead of
failing the request. So the cockpit refuses a call whose images add up to more pixels than
the largest call measured here: one 2752x1536 image (4.2 megapixels, 45.5 GiB at its peak on
the v0.5.21 runtime with either checkpoint; 43.8 GiB on the commit before).
Four 1024x1024 images fit under it, ten 512x512 too; two 2048x2048 do not. The Image view
says so before sending, and the server refuses the same with the numbers.

**What is not detected.** When the engine wedged that afternoon, its `/health` kept
answering `200` the whole time, while a generation request timed out and nothing reached
its log. The lifecycle derives "ready" from `/health`, so a wedge like that one would read
as ready. The text lanes have a generation canary for exactly this (health fine, nothing
generated); this lane does not yet. The one-at-a-time rule removes the one cause that was
measured. If the Image view ever waits far past its estimate on a lane that reads ready,
press **Cancel** (below).

## Cancelling a generation

SGLang Diffusion cannot abort a request: its own video API carries "TODO: support aborting
a job", the image API has nothing, and a client that disconnects leaves the GPU working on
the call to the end. The only way to end a generation early is to restart the lane.

So that is what the Image view's **Cancel** does. It shows only while a generation runs,
this page's or another tab's, asks first like every action, and restarts the lane through
the same action API, gates and sudoers line as the lane's card in the Lanes view: the image
being made is lost, and the lane answers again in about a minute. It starts and stops
nothing else; the lane's own Load and Stop stay on that card. A request cut this way, or by
the lane's Stop, or by a restart from its card, reads as **cancelled** in the view rather
than as a lane that failed to answer.

The result shows in its own shape. The screen takes the image's proportions, as wide as the
column allows and no taller than most of the window, so a portrait or a wide image is shown
whole and as large as the page allows; until v1.25.1 the screen kept a 16:9 box, a portrait sat
small in its middle and a gallery of several images was cut at its bottom. A click on the
image, or **Enlarge**, opens it over the whole window, fitted to it; **100 %** draws it at its
own pixels, one image pixel per screen pixel whatever the display's density, and scrolls when
it is larger than the window; Escape, Close or a click beside the picture closes it. The
session's thumbnails show each image whole. The Video view's screen takes its video's shape the
same way, and a video goes full screen from its own controls.

## Three refusals worth knowing before a client hits them

**Width and height must be multiples of 32.** `1328x1328` comes back `HTTP 500` with an
empty body; the reason is only in the engine's own log
(`Qwen-Image 2.1 height and width must be divisible by 32`). All seven aspect ratios Qwen
publishes already are.

The cockpit reads the size of a call the way the lane does (`build_sampling_params`): an
explicit `width` or `height` first, axis by axis, then `size` lower-cased with its spaces
dropped, then 1024. It refuses a size the lane would refuse or 500 on, and budgets the pixels
on those same numbers. Until v1.18.7 it read `size` only when both axes were missing, and
case-sensitively, so a `width` alone or `"2048X2048"` went through unbudgeted.
`num_inference_steps` is 1 to 100, the range the page offers.

**A call the cockpit stops waiting for keeps the lane busy.** After 30 minutes the cockpit
answers 504, "still generating": the runtime has no abort and goes on, so the next request
is refused until this run's journal shows the request ended, the lane restarts (Cancel), or
another 30 minutes pass. Until v1.18.7 the lock was given back at the timeout and a second
generation could start beside the first, the 90.5 GB case that wedged the engine.

**An output format must be sent, always.** Left out, `choose_output_image_ext` falls back
to `jpg` when the background is not transparent. This model returns RGBA for *everything*
it makes, PIL refuses to write RGBA as JPEG, and the request 500s. The plainest possible
request fails for that reason alone:

```
{"prompt":"a red cube"}                              -> HTTP 500
{"prompt":"a red cube","output_format":"png"}        -> HTTP 200
{"prompt":"a red cube","background":"transparent"}   -> HTTP 200  (the fallback picks png)
{"prompt":"a red cube","output_format":"jpeg"}       -> HTTP 500
```

Qwen-Image 2.1 does not override `default_image_output_format`, where Cosmos3 in the same
directory returns `"png"`. The cockpit sends a format on every call, and offers PNG and
WebP only.

**CFG needs both halves.** `schedule_batch.py:399` turns classifier-free guidance on only
when the scale is above 1 **and** a negative prompt is present. Either alone is ignored
byte for byte: three requests differing only in `true_cfg_scale` produced the same SHA-256.
With both, the request genuinely takes twice as long (88.9 s against 45.4 s), which is the
second forward pass. On the editing endpoint `flow_shift` is accepted and ignored: five
values, one output, and its route declares no such field (nor `max_sequence_length`), so
the cockpit leaves both out of an edit.

## Transparency works, and it is the prompt that asks for it

`background: "transparent"` picks the file extension. What produces an alpha channel is
the **prompt**, in the wording Qwen's own card uses ("This is an RGBA image with
transparency... the background is transparent").

Measured: an ordinary generation is RGBA with alpha 251 to 253 everywhere, so nominally
transparent and actually opaque. A generation that asks for transparency comes back with
**68.2% of its pixels fully transparent**, alpha minimum 0. Editing an RGBA source with
`background: "transparent"` preserves it: 68.0%.

## Editing redraws, it does not retouch

The semantic edit is correct. Ask for a blue book cover and the book cover is blue; ask
for ten references combined and they are combined. But the whole picture comes back
re-rendered with about **twice the high-frequency detail of the source**: Laplacian energy
2.16x, on every reference tested (model-generated and re-encoded, RGB, RGBA and JPEG),
every prompt, every step count from 20 to 80, every guidance setting, and every
`flow_shift`. An edit that asks for *nothing to change* does it too.

Generation never does this. It is specific to the condition-image path, and it was
measured rather than inferred: a first pass using standard deviation as the instrument
missed it entirely on a bright reference (73.5 to 78.3, which reads as noise) and the
zoom did not. Expect a re-rendered image, not a retouched one.

## The cockpit view

Every parameter the model has, at the model's own defaults, read out of
`configs/sample/qwenimage21.py` rather than chosen: 1024x1024, 40 steps, one image, CFG
off, RNG on the CPU. **Reset** puts all of them back.

Prompts to try are prefilled (text rendering, transparent cutout, transparent sticker,
local edit, combining two pictures), references can be uploaded or generated on the spot
with **Use a sample**, and any output can be sent back as a reference in one click, which
is what multi-round editing is. The preview sits on a checkerboard, because a transparent
cutout on a white card is indistinguishable from an opaque one.

The serving key never reaches the browser. The page describes the call and the cockpit
makes it, with an allowlist of fields: a page cannot name an upscaler path, a LoRA, a perf
dump target or a diffusers kwargs blob just because the protocol has a field for it.

## Licence

Qwen Research License: research and evaluation, **not commercial use**. It is on the view
where the people using the box will read it.
