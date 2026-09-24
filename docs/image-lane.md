# The image lane: Qwen-Image 2.1 on one Spark

Text to image, image editing and native RGBA, served by SGLang Diffusion in its own
venv, on its own port, driven from the cockpit's **Image** tab. Opt-in, because it costs
38 GB and about 25 minutes:

```bash
./install.sh --with-image
# or, from nothing at all:
curl -fsSL https://raw.githubusercontent.com/hasso5703/dgx-spark-qwen38/main/get.sh | bash -s -- --with-image
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
same three controls at the top of the cockpit, and it obeys the same rule.

1. **Pick `Qwen-Image 2.1`** in the switcher (it sits under its own *Images* heading)
   and press **Switch**. `switch-model.sh image` verifies the checkpoint and makes the
   image lane the one unit enabled at boot. Like every switch, it never starts or
   stops an engine.
2. **Stop** the lane that is serving. The action bar's lane button reads `Stop 27B`
   while the 27B serves.
3. **Start Qwen-Image**. It answers in about 70 seconds; the lane pill, the Engines card
   and the Image tab all show which component it is loading, in the engine's own words
   ("the 16.5 GB Qwen3-VL encoder", "the 13.3 GB DiT"), and Generate turns on by itself.

Back to text is the same three moves the other way. From a terminal the switch is
`./switch-model.sh image` (or `stock`), and it prints the two commands that follow.

**Never two engines at once.** 31 GB of weights do not fit beside a serving LLM, and a
request takes the lane to 34.8 GB at 1024x1024 (44.7 GB at 2048x2048). The cockpit refuses to start any engine while
another one is busy, and says which one to stop, for all three lanes alike: starting the
image lane while the 27B serves comes back `409 blocked`, and so does starting the 27B
while the image lane loads. That gate used to pick "the other engine" with `[0]`, which
with three of them checked one neighbour in two.

The unit also carries `Conflicts=` with every text unit, as a second belt for a
`systemctl start` typed at a terminal, which the cockpit's gate never sees. Through the
cockpit it is never reached, because the start is refused first.

The Image tab has no start or stop of its own. The first version had one, and it started
this lane by a path none of the others use, stopping the text lane silently through
`Conflicts=` where every other lane is refused with "stop it first". The tab now says
which of the three moves is next, naming the buttons as they read on screen.

The Engines tab's Flush cache, Abort all and Smoke are greyed out while the image lane
serves: they talk to the text engine on :30000, which is closed then. They used to test
"is an engine ready", which the image lane is.

## Why a venv and not the docker image

The cookbook is explicit for this model: *"This integration currently uses the
Python/source command; no published Docker image is verified."* Qwen-Image 2.1 is in no
SGLang release either, so the lane runs a pinned source checkout
(`ddebc52f237a1dbb56533469ab2ec2a7b856c4ab`).

The released wheel still goes in **first**, and the order is not cosmetic: it carries
`sglang-kernel` built for aarch64, which a source tree does not build. Source first
leaves a runtime with no native kernels that dies on the first request. The editable
overlay then goes on with `--no-deps`, because letting the source tree resolve again
pulls a `transformers` that breaks the encoder this model needs.

> The model card asks for `transformers>=5.17`. That applies to the diffusers pipeline,
> not to this one: SGLang has its own native encoder, and the cookbook says to keep its
> installed dependencies. The lane runs 5.12.1 and is right to.

### The local changes to the pinned source

Two, neither about images, both applied by `install-image.sh` from `image-sglang/`, both
checked against the real upstream files at the pin by a CI step, and both skipped with a
note if a future pin no longer fits them (the lane serves either way).

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
back as `None` (`server_args.py:828`), the cockpit and the API take their pixels from the
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

## What it costs, measured here

Every number is a request made against this lane on a DGX Spark on 2026-09-22, not a
figure from the cookbook. Peak memory is what the server reports for itself.

| request | time | peak |
|---|---:|---:|
| 512x512, 40 steps | 9.0 s | 31.9 GB |
| 768x768, 40 steps | 22.6 s | 31.9 GB |
| **1024x1024, 40 steps (the defaults)** | **38.2 s** | 34.8 GB |
| 1664x928, 40 steps | 60.4 s | 34.8 GB |
| 2048x2048, 40 steps (the model card's own size) | 190.9 s | 44.7 GB |
| 1024x1024 at 8 / 20 / 60 steps | 8.4 / 19.8 / 57.3 s | 31.9 GB |
| transparent 1024x1024 | 39.8 s | 34.8 GB |
| edit, one reference, 40 steps | 44.6 s | 34.8 GB |
| edit, ten references, 20 steps | 69.6 s | 34.8 GB |
| two images in one call, 8 steps | 16.8 s | 31.9 GB |

The cookbook publishes 35.36 s for this machine at 1024x1024/40; 38.2 s here. Cost is
linear in steps, and a little worse than linear in pixels: four times the 1024x1024
time would be 153 s at 2048x2048, and it takes 190.9 s. The cockpit's estimate is
`1 + steps x 0.97 x (pixels / 1024^2)^1.14` seconds, within 6.5 % of all nine measured
requests. Startup is 60 to 72 s from a warm page cache. At rest the lane costs about
0.05 CPU cores (see the idle-loop fix above).

Same seed, twice: byte-identical. 8 steps is visibly unfinished and not worth the 8.4 s.

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
the same 34.0 GB peak for every one of them.

The third row is what bites. On a box whose 121.6 GB is shared between the CPU and the
GPU, two concurrent requests is most of it, and the engine that reached that state stopped
answering entirely: a direct curl to its port timed out, no request appeared in its log,
and `systemctl restart` was the only way back. Two browser tabs are enough to do it.

So the cockpit serializes. A second request while one is running comes back **HTTP 409 in
under a millisecond** with the reason, rather than queueing and holding one of the
browser's six connections to that origin. The Image tab also turns its own button off when
the lane is busy with somebody else's request, which it can see because the engine names
its current stage in its log.

Nothing enforces this below the cockpit. A script that posts twice to port 30020 will
still do what the numbers above describe.

**The images of one call are one batch.** Ten 2048x2048 images in one call took 42 s per
denoising step on 2026-09-23, where one image takes 4.6: the pipeline runs them together,
and the memory that needs grows with them. Where it ends for a call that size has not been
measured, and on this box running out of unified memory hangs the machine instead of
failing the request. So the cockpit refuses a call whose images add up to more pixels than
the largest call measured here: one 2752x1536 image (4.2 megapixels, 44.8 GB at its peak).
Four 1024x1024 images fit under it, ten 512x512 too; two 2048x2048 do not. The Image tab
says so before sending, and the server refuses the same with the numbers.

**What is not detected.** When the engine wedged that afternoon, its `/health` kept
answering `200` the whole time, while a generation request timed out and nothing reached
its log. The lifecycle derives "ready" from `/health`, so a wedge like that one would read
as ready. The text lanes have a generation canary for exactly this (health fine, nothing
generated); this lane does not yet. The one-at-a-time rule removes the one cause that was
measured. If the Image tab ever waits far past its estimate on a lane that reads ready,
press **Cancel** (below).

## Cancelling a generation

SGLang Diffusion cannot abort a request: its own video API carries "TODO: support aborting
a job", the image API has nothing, and a client that disconnects leaves the GPU working on
the call to the end. The only way to end a generation early is to restart the lane.

So that is what the Image tab's **Cancel** does. It shows only while a generation runs,
this page's or another tab's, asks first like every action, and restarts the lane through
the same action API, gates and sudoers line as the Engines card: the image being made is
lost, and the lane answers again in about a minute. It starts and stops nothing else; the
lane's own Start and Stop stay in the action bar. A request cut this way, or by the lane's
Stop, or by a restart from the Engines card, reads as **cancelled** in the tab rather than
as a lane that failed to answer.

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

## The cockpit tab

Every parameter the model has, at the model's own defaults, read out of
`configs/sample/qwenimage21.py` rather than chosen: 1024x1024, 40 steps, one image, CFG
off, RNG on the CPU. **Reset settings** puts all of them back.

Prompts to try are prefilled (text rendering, transparent cutout, transparent sticker,
local edit, combining two pictures), references can be uploaded or generated on the spot
with **Use a sample**, and any output can be sent back as a reference in one click, which
is what multi-round editing is. The preview sits on a checkerboard, because a transparent
cutout on a white card is indistinguishable from an opaque one.

The serving key never reaches the browser. The page describes the call and the cockpit
makes it, with an allowlist of fields: a page cannot name an upscaler path, a LoRA, a perf
dump target or a diffusers kwargs blob just because the protocol has a field for it.

## Licence

Qwen Research License: research and evaluation, **not commercial use**. It is on the tab
where the people using the box will read it.
