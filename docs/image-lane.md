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
and the runtime (the checkpoint stays in your HF cache).

## One engine at a time

31 GB of weights do not fit beside a serving LLM, and a request peaks at 34.8 GB on top
of that. The unit says so rather than a README:

```ini
Conflicts=qwen38-sglang.service qwen38-flash.service qwen38-llamacpp.service
```

Starting the image lane stops the text lane. Starting a text lane stops the image lane.
Nothing has to be remembered, and nothing can be bypassed by typing `systemctl start`.
The image unit is **not enabled at boot**: a box that reboots comes back the way its
owner left it.

The Image tab has the buttons, through the same confirmation modal, job strip and
sudoers allowlist as every other unit on this box. The modal says what `Conflicts=` is
about to do before it does it, and the tab keeps asking while the 31 GB load, because
the unit reads `active` and `/health` answers 503 for that whole minute and a bit.

From a terminal, if you prefer one:

```bash
sudo systemctl start qwen38-image.service     # images, text lane stops
sudo systemctl start qwen38-sglang.service    # text, image lane stops
```

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
| 1024x1024 at 8 / 20 / 60 steps | 8.4 / 19.8 / 57.3 s | 31.9 GB |
| transparent 1024x1024 | 39.8 s | 34.8 GB |
| edit, one reference, 40 steps | 44.6 s | 34.8 GB |
| edit, ten references, 20 steps | 69.6 s | 34.8 GB |
| two images in one call, 8 steps | 16.8 s | 31.9 GB |

The cookbook publishes 35.36 s for this machine at 1024x1024/40; 38.2 s here. Cost is
very nearly linear in pixels and in steps: a step at 1024x1024 is 1.03 s, on top of 2.9 s
of encode and VAE decode that a smaller image does not avoid. Startup is about 77 s from
a warm page cache.

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

## Three refusals worth knowing before a client hits them

**Width and height must be multiples of 32.** `1328x1328` comes back `HTTP 500` with an
empty body; the reason is only in the engine's own log
(`Qwen-Image 2.1 height and width must be divisible by 32`). All seven aspect ratios Qwen
publishes already are.

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
values, one output.

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
