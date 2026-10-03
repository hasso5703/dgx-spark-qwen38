# Platforms: one repo, every GB10 box

Every box this repo runs on has the same GB10 chip and 128 GB of unified memory, and every lane
runs in the same container, pinned by digest: SGLang, PyTorch, CUDA and cuBLAS are identical,
byte for byte, on all of them. What differs is underneath the containers, and it is the box's
own:

- the maker's firmware: BIOS, embedded controller (the reference box is an ASUS Ascent GX10;
  HP, Dell, Lenovo, MSI, Acer, Gigabyte and NVIDIA's Founders Edition are other GB10 boxes);
- the DGX OS release (OTA), the kernel, and the NVIDIA driver with its GSP firmware: the
  driver's own CUDA library is the one the containers use;
- Docker and the NVIDIA Container Toolkit.

Issue [#26](https://github.com/hasso5703/dgx-spark-qwen38/issues/26) is that difference: the
same repo and the same images, a driver (580.159.03) under which engines died, and none of
those deaths after its owner updated to 580.178.04.

A repo cannot give every box the same kernel, driver and firmware, and should not try: they
come from NVIDIA and from the box's maker, and changing them is the owner's decision. NVIDIA's
own [update guide](https://docs.nvidia.com/dgx/dgx-spark/os-and-component-update.html)
(2026-09-10) "strongly recommend[s] using the DGX Dashboard for all system updates", gives
`sudo apt update`, `sudo apt dist-upgrade` and `fwupdmgr` as the manual path, and says of
itself: "Founders Edition Only: The update information in this guide applies only to the DGX
Spark Founders Edition. Devices from other manufacturers might have different update
procedures." What the repo can do is tell a box what is known about its combination, with the
evidence, and collect what each box reports.

## doctor.py: what this box is, and what is known about it

```bash
./doctor.py            # the full report
./doctor.py --brief    # the findings only (what install.sh prints)
./doctor.py --report   # anonymised Markdown to paste into an issue
./doctor.py --json     # the same, for a program
```

It reads and never writes, needs no sudo, sends nothing anywhere, and bounds every command
in time; a fact it cannot read is "unknown", never guessed. It reads the DMI fields any user
can read (the serial numbers are root-only and are not read), `/etc/dgx-release` by a list of
keys (that file, readable by every user, also holds the box's serial number, which no output
carries), `/etc/os-release`, `/proc/meminfo`, and what `nvidia-smi`, `docker`, `nvidia-ctk`,
`apt-mark showhold` and `fwupdmgr get-devices` print. It exits 0 whatever it finds: it warns,
it never blocks.

The known issues live in [platforms.json](../platforms.json), each one a fact a box can be
checked for, with its evidence:

<!-- issues:start -->
| id | level | what it checks |
|---|---|---|
| `gpu-unreachable` | fail | nvidia-smi does not reach the GPU |
| `memory-below-110` | fail | Less than 110 GiB of memory |
| `docker-unreachable` | fail | Docker does not answer this user |
| `driver-580.159.03` | warn | Driver 580.159.03, the one every report of engines dying with "operation not permitted" ran |
| `kernel-7.0.0-1019` | warn | Kernel 7.0.0-1019, which NVIDIA asked to hold off |
| `cma-reserved-uncounted` | warn | Memory the kernel reserves without counting it (CmaTotal 0 with CmaFree above 0) |
| `gpu-not-gb10` | warn | The GPU is not a GB10 |
| `toolkit-missing` | warn | No NVIDIA Container Toolkit (nvidia-ctk not found) |
| `apport-active` | info | Crash reports are on (apport) |
<!-- issues:end -->

## selftest.py: does this box serve?

```bash
./selftest.py            # seven real requests through the keepalive proxy (:30001)
./selftest.py --report   # the same, as Markdown for an issue
```

The model list, one answer, one streamed answer that ends with `[DONE]`, one tool call, a
passphrase found in about 8,000 tokens of filler, four questions at once, and one answer in
the Anthropic dialect (the one Claude Code speaks), each with what it got. On the reference
box, through the flash lane while it also served an audit's three requests, the seven passed
in 51 s (one answer waited 29 s behind the audit's own prompts). It refuses to run
while the engine serves
anything, since a test would then slow real work and be slowed by it (`--force` runs it
anyway), and exits 0 when every check passed, 1 when one failed, 3 when it refused. It reads
the API key from `~/.config/qwen38/api-key` and never prints it. The deeper instruments are
the repo's own: `tools-check.py` (15 tool cases), `needle.sh` (retrieval up to the window),
`conc-check.py` (40 and 80 exact answers) and `bench.sh`.

## The boxes reported so far

<!-- matrix:start -->
| make and model | DGX OS (OTA) | kernel | driver | lanes | result | date | source |
|---|---|---|---|---|---|---|---|
| ASUS Ascent GX10 | 7.6.0 | 6.17.0-1032-nvidia | 580.178.04 | 27B (1M), flash, image, video | works: this repo's reference box, every lane measured | 2026-10-03 | reference box |
| HP ZGX Nano G1n | unknown | unknown | 580.159.03 | 27B (1M) | engine deaths, "operation not permitted"; systemd brought the lane back | 2026-09-30 | [#26](https://github.com/hasso5703/dgx-spark-qwen38/issues/26) |
| HP ZGX Nano G1n | 7.5.0 | unknown | 580.178.04 | 27B (1M) | no "operation not permitted" since; one CUBLAS_STATUS_INTERNAL_ERROR crash after 2 h 35, under investigation | 2026-10-02 | [#26](https://github.com/hasso5703/dgx-spark-qwen38/issues/26#issuecomment-5962195541) |
<!-- matrix:end -->

To add yours: once the install is done, run `./selftest.py --report` and `./doctor.py --report`,
and open an issue with both outputs and a word on what you serve and how it went. The table is printed by
`./doctor.py --matrix` from `platforms.json`, and a test keeps both tables of this page equal to it.

[Back to the README](../README.md)
